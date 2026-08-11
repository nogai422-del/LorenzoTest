from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telethon import TelegramClient, events, errors, utils
from telethon.sessions import StringSession
from telethon.tl import types

from telethon_config import load_telethon_config, save_telethon_config

from db import (
    claim_sync_requests,
    cleanup_seen_messages,
    finish_sync_request,
    get_chat_info,
    init_db,
    list_tracked_chat_ids,
    mark_missing_members_left,
    record_chat,
    requeue_running_sync_requests,
    set_left,
    update_chat_sync_status,
    upsert_member_snapshot,
    upsert_message_activity,
)

load_dotenv()
init_db()

logger = logging.getLogger("telethon_sync")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _to_ts(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return None


def status_snapshot(user, checked_at: int) -> tuple[str, int | None]:
    status = getattr(user, "status", None)
    if isinstance(status, types.UserStatusOnline):
        return "online", checked_at
    if isinstance(status, types.UserStatusOffline):
        return "offline", _to_ts(getattr(status, "was_online", None))
    if isinstance(status, types.UserStatusRecently):
        return "recently", None
    if isinstance(status, types.UserStatusLastWeek):
        return "last_week", None
    if isinstance(status, types.UserStatusLastMonth):
        return "last_month", None
    if isinstance(status, types.UserStatusEmpty) or status is None:
        return "hidden", None
    return "unknown", None


def display_name(user) -> str:
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    name = " ".join(p for p in (first, last) if p).strip()
    return name or str(getattr(user, "id", "Unknown"))


def chat_identity(entity) -> tuple[int, str, str | None, str] | None:
    if isinstance(entity, types.Chat):
        return int(utils.get_peer_id(entity)), entity.title or str(entity.id), None, "group"
    if isinstance(entity, types.Channel) and bool(getattr(entity, "megagroup", False)):
        return int(utils.get_peer_id(entity)), entity.title or str(entity.id), getattr(entity, "username", None), "supergroup"
    return None


def participant_status(user) -> str:
    participant = getattr(user, "participant", None)
    if isinstance(participant, types.ChannelParticipantCreator):
        return "creator"
    if isinstance(participant, types.ChannelParticipantAdmin):
        return "admin"
    return "member"


def participant_joined_at(user) -> int | None:
    participant = getattr(user, "participant", None)
    return _to_ts(getattr(participant, "date", None))


class TelethonSyncService:
    def __init__(self) -> None:
        config = load_telethon_config()
        if not config["enabled"]:
            raise RuntimeError("Telethon disabled")
        if config["setup_pending"]:
            raise RuntimeError("Telethon authorization is in progress in Web Admin")
        if not config["configured"]:
            raise RuntimeError("Configure Telethon in Web Admin -> Telethon or set TELETHON_API_ID/TELETHON_API_HASH")

        self.api_id = int(config["api_id"])
        self.api_hash = str(config["api_hash"])
        if not config.get("has_session"):
            raise RuntimeError("Telethon is not authorized. Open Web Admin -> Telethon and complete authorization")
        self.session_string = str(config["session_string"])
        self.sync_interval = max(60, _env_int("TELETHON_SYNC_INTERVAL_SECONDS", 300))
        self.discovery_interval = max(300, _env_int("TELETHON_DISCOVERY_INTERVAL_SECONDS", 1800))
        self.history_days = max(1, min(365, _env_int("TELETHON_HISTORY_DAYS", 30)))
        self.history_max_messages = max(100, _env_int("TELETHON_HISTORY_MAX_MESSAGES", 50000))
        self.history_overlap_seconds = max(60, _env_int("TELETHON_HISTORY_OVERLAP_SECONDS", 600))
        self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)
        self._stop = asyncio.Event()
        self._last_discovery = 0
        self._last_cleanup = 0
        self._sync_locks: dict[int, asyncio.Lock] = {}

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            save_telethon_config(account={}, session_string="")
            await self.client.disconnect()
            raise RuntimeError(
                "Telethon session is not authorized. Open Web Admin -> Telethon and complete authorization, "
                "or run `python telethon_login.py`."
            )

        recovered = requeue_running_sync_requests()
        if recovered:
            logger.warning("Requeued %s interrupted sync request(s)", recovered)

        # The web authorization wizard may have started while this worker was
        # connecting. Never overwrite setup_pending/account state in that race.
        latest_config = load_telethon_config()
        if latest_config.get("setup_pending") or not latest_config.get("enabled"):
            await self.client.disconnect()
            raise RuntimeError("Telethon worker yielded to Web Admin authorization")

        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        self.client.add_event_handler(self._on_chat_action, events.ChatAction())
        me = await self.client.get_me()
        save_telethon_config(
            account={
                "id": int(me.id),
                "display_name": display_name(me),
                "username": getattr(me, "username", None),
                "authorized_at": int(time.time()),
            },
            setup_pending=False,
            setup_started_at=0,
        )
        logger.info("Telethon connected as %s (%s)", display_name(me), getattr(me, "id", "?"))

    async def stop(self) -> None:
        self._stop.set()
        if self.client.is_connected():
            await self.client.disconnect()

    async def run_forever(self) -> None:
        await self.start()
        try:
            while not self._stop.is_set():
                runtime_config = load_telethon_config()
                if not runtime_config["enabled"]:
                    logger.info("Telethon sync disabled from Web Admin; disconnecting worker")
                    break
                if runtime_config.get("setup_pending"):
                    logger.info("Telethon authorization wizard started; disconnecting worker")
                    break
                now = int(time.time())
                if now - self._last_discovery >= self.discovery_interval:
                    await self.discover_groups()
                    self._last_discovery = now

                await self._process_manual_requests()
                await self._sync_due_chats(now)

                if now - self._last_cleanup >= 6 * 3600:
                    cleanup_seen_messages(max(45, self.history_days + 15))
                    self._last_cleanup = now

                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.stop()

    async def discover_groups(self) -> int:
        count = 0
        try:
            async for dialog in self.client.iter_dialogs():
                identity = chat_identity(dialog.entity)
                if not identity:
                    continue
                chat_id, title, username, chat_type = identity
                record_chat(
                    chat_id, title, username, chat_type,
                    int(time.time()), tracked=None, discovered_by="telethon",
                )
                count += 1
        except errors.FloodWaitError as exc:
            logger.warning("FloodWait during dialog discovery: %ss", exc.seconds)
            await asyncio.sleep(exc.seconds)
        except Exception:
            logger.exception("Telethon group discovery failed")
        return count

    async def _process_manual_requests(self) -> None:
        for request in claim_sync_requests(10):
            request_id = int(request["id"])
            chat_id = int(request["chat_id"])
            try:
                request_type = str(request["request_type"] or "full")
                await self.sync_chat(
                    chat_id,
                    sync_members=request_type in {"full", "members"},
                    sync_history=request_type in {"full", "history"},
                    force_history_backfill=request_type == "full",
                )
                finish_sync_request(request_id)
            except Exception as exc:
                logger.exception("Manual sync failed for chat %s", chat_id)
                finish_sync_request(request_id, error=str(exc))

    async def _sync_due_chats(self, now: int) -> None:
        for chat_id in list_tracked_chat_ids():
            info = get_chat_info(chat_id) or {}
            last_sync = int(info.get("last_member_sync_at") or 0)
            if now - last_sync < self.sync_interval:
                continue
            try:
                await self.sync_chat(chat_id, sync_members=True, sync_history=True)
            except errors.FloodWaitError as exc:
                update_chat_sync_status(chat_id, error=f"FloodWait {exc.seconds}s")
                logger.warning("FloodWait %ss while syncing %s", exc.seconds, chat_id)
                await asyncio.sleep(exc.seconds)
            except Exception as exc:
                update_chat_sync_status(chat_id, error=str(exc))
                logger.exception("Scheduled sync failed for chat %s", chat_id)

    async def sync_chat(
        self,
        chat_id: int,
        *,
        sync_members: bool = True,
        sync_history: bool = True,
        force_history_backfill: bool = False,
    ) -> dict:
        lock = self._sync_locks.setdefault(int(chat_id), asyncio.Lock())
        async with lock:
            current = get_chat_info(chat_id) or {}
            if int(current.get("is_hidden") or 0):
                raise ValueError("Chat is hidden by owner")
            entity = await self.client.get_entity(int(chat_id))
            identity = chat_identity(entity)
            if not identity:
                raise ValueError("Selected peer is not a Telegram group/supergroup")
            resolved_id, title, username, chat_type = identity
            if int(resolved_id) != int(chat_id):
                logger.info("Resolved chat id %s -> %s", chat_id, resolved_id)
                chat_id = resolved_id
            record_chat(chat_id, title, username, chat_type, int(time.time()), tracked=None, discovered_by="telethon")

            result = {"members": 0, "left": 0, "messages": 0}
            if sync_members:
                result.update(await self._sync_members(chat_id, entity))
            if sync_history:
                result["messages"] = await self._sync_history(chat_id, entity, force_backfill=force_history_backfill)
            update_chat_sync_status(chat_id, error="")
            return result

    async def _sync_members(self, chat_id: int, entity) -> dict:
        sync_token = uuid.uuid4().hex
        checked_at = int(time.time())
        participants = 0
        human_participants = 0
        async for user in self.client.iter_participants(entity):
            participants += 1
            is_bot = bool(getattr(user, "bot", False))
            if not is_bot:
                human_participants += 1
            status, last_seen = status_snapshot(user, checked_at)
            upsert_member_snapshot(
                chat_id=chat_id,
                user_id=int(user.id),
                user_name=display_name(user),
                username=getattr(user, "username", None),
                joined_at=participant_joined_at(user),
                telegram_last_seen_at=last_seen,
                telegram_status=status,
                telegram_status_checked_at=checked_at,
                is_bot=is_bot,
                membership_status=participant_status(user),
                sync_token=sync_token,
            )
        left = mark_missing_members_left(chat_id, sync_token, checked_at)
        update_chat_sync_status(
            chat_id,
            # Keep the chat-card count aligned with dashboard members, which
            # intentionally excludes bot accounts.
            participant_count=human_participants,
            member_synced_at=checked_at,
            error="",
        )
        return {"members": human_participants, "participants_total": participants, "left": left}

    async def _sync_history(self, chat_id: int, entity, *, force_backfill: bool = False) -> int:
        info = get_chat_info(chat_id) or {}
        last_history_sync = int(info.get("last_history_sync_at") or 0)
        now_ts = int(time.time())
        full_cutoff = now_ts - self.history_days * 86400
        if force_backfill or not last_history_sync:
            cutoff_ts = full_cutoff
        else:
            cutoff_ts = max(full_cutoff, last_history_sync - self.history_overlap_seconds)

        new_messages = 0
        scanned = 0
        async for message in self.client.iter_messages(entity, limit=self.history_max_messages):
            scanned += 1
            msg_ts = _to_ts(getattr(message, "date", None)) or 0
            if msg_ts and msg_ts < cutoff_ts:
                break
            sender_id = getattr(message, "sender_id", None)
            if not sender_id or int(sender_id) <= 0 or not getattr(message, "id", None):
                continue
            sender = getattr(message, "sender", None)
            if sender is not None and bool(getattr(sender, "bot", False)):
                continue
            if sender is not None:
                name = display_name(sender)
                username = getattr(sender, "username", None)
            else:
                name = str(sender_id)
                username = None
            if upsert_message_activity(
                chat_id=chat_id,
                user_id=int(sender_id),
                user_name=name,
                username=username,
                now_ts=msg_ts or now_ts,
                message_id=int(message.id),
                is_bot=False,
                # A historical message proves past activity, not current
                # membership. Current membership is owned by _sync_members().
                mark_active=False,
            ):
                new_messages += 1

        update_chat_sync_status(chat_id, history_synced_at=now_ts, error="")
        logger.info("History sync chat=%s scanned=%s new=%s", chat_id, scanned, new_messages)
        return new_messages

    async def _on_new_message(self, event) -> None:
        try:
            chat = await event.get_chat()
            identity = chat_identity(chat)
            if not identity:
                return
            chat_id, title, username, chat_type = identity
            record_chat(chat_id, title, username, chat_type, int(time.time()), tracked=None, discovered_by="telethon")
            info = get_chat_info(chat_id) or {}
            if not int(info.get("is_tracked") or 0):
                return

            sender = await event.get_sender()
            if not sender or not isinstance(sender, types.User) or bool(getattr(sender, "bot", False)):
                return
            msg_ts = _to_ts(getattr(event.message, "date", None)) or int(time.time())
            upsert_message_activity(
                chat_id=chat_id,
                user_id=int(sender.id),
                user_name=display_name(sender),
                username=getattr(sender, "username", None),
                now_ts=msg_ts,
                message_id=int(event.message.id),
                is_bot=False,
            )
            status, last_seen = status_snapshot(sender, int(time.time()))
            upsert_member_snapshot(
                chat_id, int(sender.id), display_name(sender), getattr(sender, "username", None),
                telegram_last_seen_at=last_seen,
                telegram_status=status,
                telegram_status_checked_at=int(time.time()),
                is_bot=False,
                preserve_membership_status=True,
            )
        except Exception:
            logger.exception("Failed to process Telethon NewMessage")

    async def _on_chat_action(self, event) -> None:
        try:
            chat = await event.get_chat()
            identity = chat_identity(chat)
            if not identity:
                return
            chat_id, title, username, chat_type = identity
            record_chat(chat_id, title, username, chat_type, int(time.time()), tracked=None, discovered_by="telethon")
            info = get_chat_info(chat_id) or {}
            if not int(info.get("is_tracked") or 0):
                return

            users = list(getattr(event, "users", None) or [])
            if not users and getattr(event, "user", None):
                users = [event.user]
            now_ts = int(time.time())
            if bool(getattr(event, "user_left", False)) or bool(getattr(event, "user_kicked", False)):
                for user in users:
                    set_left(chat_id, int(user.id), now_ts)
                return

            if bool(getattr(event, "user_joined", False)) or bool(getattr(event, "user_added", False)):
                for user in users:
                    status, last_seen = status_snapshot(user, now_ts)
                    upsert_member_snapshot(
                        chat_id, int(user.id), display_name(user), getattr(user, "username", None),
                        joined_at=now_ts,
                        telegram_last_seen_at=last_seen,
                        telegram_status=status,
                        telegram_status_checked_at=now_ts,
                        is_bot=bool(getattr(user, "bot", False)),
                    )
        except Exception:
            logger.exception("Failed to process Telethon ChatAction")


async def run_telethon_worker() -> None:
    service = TelethonSyncService()
    await service.run_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_telethon_worker())
