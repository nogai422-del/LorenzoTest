from __future__ import annotations

from typing import Any

from db import find_known_user_by_username
from telethon_config import load_telethon_config


class UsernameResolveError(RuntimeError):
    pass


def _normalized(username: str) -> str:
    value = str(username or "").strip().lstrip("@").strip()
    if not value:
        raise UsernameResolveError("Введите @username пользователя.")
    if len(value) > 64 or any(ch.isspace() for ch in value):
        raise UsernameResolveError("Некорректный @username.")
    return value


async def resolve_user_by_username(username: str) -> dict[str, Any]:
    """Resolve a Telegram user by username without exposing Telethon secrets.

    Prefer a live lookup when an authorised StringSession is available. If
    Telethon is not connected, fall back to the latest synced member identity.
    """
    value = _normalized(username)
    cfg = load_telethon_config()

    if cfg.get("configured") and cfg.get("has_session"):
        try:
            from telethon import TelegramClient, types
            from telethon.sessions import StringSession

            client = TelegramClient(
                StringSession(str(cfg["session_string"])),
                int(cfg["api_id"]),
                str(cfg["api_hash"]),
            )
            await client.connect()
            try:
                if not await client.is_user_authorized():
                    raise UsernameResolveError("Telethon-сессия не авторизована. Переподключите Telethon в панели.")
                entity = await client.get_entity(value)
                if not isinstance(entity, types.User):
                    raise UsernameResolveError("Этот @username принадлежит не пользователю Telegram.")
                if bool(getattr(entity, "bot", False)):
                    raise UsernameResolveError("Бота нельзя назначить системным администратором.")
                first = str(getattr(entity, "first_name", "") or "").strip()
                last = str(getattr(entity, "last_name", "") or "").strip()
                display_name = " ".join(x for x in (first, last) if x).strip() or str(entity.id)
                return {
                    "user_id": int(entity.id),
                    "display_name": display_name,
                    "username": str(getattr(entity, "username", "") or value).lstrip("@"),
                    "source": "telethon",
                }
            finally:
                await client.disconnect()
        except UsernameResolveError:
            raise
        except Exception as exc:
            # A transient Telegram lookup error should not block a known,
            # synchronised group participant from being assigned.
            local = find_known_user_by_username(value)
            if local:
                return {
                    "user_id": int(local["user_id"]),
                    "display_name": str(local.get("user_name") or local["user_id"]),
                    "username": str(local.get("username") or value).lstrip("@"),
                    "source": "database",
                }
            raise UsernameResolveError(f"Не удалось найти @{value} через Telegram: {str(exc)[:180]}") from exc

    local = find_known_user_by_username(value)
    if local:
        return {
            "user_id": int(local["user_id"]),
            "display_name": str(local.get("user_name") or local["user_id"]),
            "username": str(local.get("username") or value).lstrip("@"),
            "source": "database",
        }
    raise UsernameResolveError(
        "Пользователь не найден среди синхронизированных участников. Подключите Telethon и повторите поиск по @username."
    )
