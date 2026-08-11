from __future__ import annotations

import asyncio
import time
from typing import Any

from telethon_config import load_telethon_config, save_telethon_config, set_setup_pending


class TelethonAuthError(RuntimeError):
    pass


class TelethonAuthManager:
    """In-memory authorization wizard backed by Telethon StringSession.

    Phone, one-time code and 2FA password only live in process memory while the
    wizard is active. The resulting StringSession is stored in persistent data
    (or can be supplied through SESSION_STRING on Bothost).
    """

    def __init__(self) -> None:
        self._client = None
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        self._step = "idle"
        self._lock = asyncio.Lock()

    @property
    def step(self) -> str:
        return self._step

    async def _disconnect_no_lock(self) -> None:
        client = self._client
        self._client = None
        self._phone = None
        self._phone_code_hash = None
        if client is not None:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

    async def reset(self, *, clear_pending: bool = True) -> None:
        async with self._lock:
            await self._disconnect_no_lock()
            self._step = "idle"
            if clear_pending:
                set_setup_pending(False)

    async def configure(self, api_id: int, api_hash: str) -> None:
        async with self._lock:
            await self._disconnect_no_lock()
            save_telethon_config(
                api_id=int(api_id),
                api_hash=api_hash,
                enabled=True,
                setup_pending=True,
                setup_started_at=int(time.time()),
                account={},
                session_string="",
            )
            self._step = "phone"

    @staticmethod
    def _human_error(exc: Exception) -> str:
        name = exc.__class__.__name__
        mapping = {
            "ApiIdInvalidError": "Неверные API ID / API Hash.",
            "PhoneNumberInvalidError": "Telegram не принял номер телефона. Проверьте формат с кодом страны.",
            "PhoneNumberBannedError": "Этот номер заблокирован Telegram.",
            "PhoneNumberFloodError": "Слишком много попыток для этого номера. Попробуйте позже.",
            "PhoneCodeInvalidError": "Неверный код Telegram.",
            "PhoneCodeExpiredError": "Код Telegram истёк. Запросите новый.",
            "PasswordHashInvalidError": "Неверный пароль двухэтапной аутентификации.",
            "AuthKeyDuplicatedError": "Telegram отклонил эту сессию. Переподключите Telethon через мастер.",
        }
        if name == "FloodWaitError":
            seconds = getattr(exc, "seconds", None)
            return f"Telegram ограничил запросы. Повторите через {seconds} сек." if seconds else "Telegram временно ограничил запросы."
        return mapping.get(name, f"Ошибка Telegram: {name}")

    async def _ensure_client_no_lock(self):
        if self._client is not None:
            return self._client
        cfg = load_telethon_config()
        if not cfg["configured"]:
            raise TelethonAuthError("Сначала сохраните API ID и API Hash.")
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except Exception as exc:
            raise TelethonAuthError("Telethon не установлен. Выполните pip install -r requirements.txt") from exc

        self._client = TelegramClient(
            StringSession(str(cfg.get("session_string") or "")),
            int(cfg["api_id"]),
            str(cfg["api_hash"]),
        )
        try:
            await self._client.connect()
        except Exception:
            await self._disconnect_no_lock()
            raise
        return self._client

    async def _complete_no_lock(self, client) -> dict[str, Any]:
        me = await client.get_me()
        first = (getattr(me, "first_name", None) or "").strip()
        last = (getattr(me, "last_name", None) or "").strip()
        display_name = " ".join(x for x in (first, last) if x).strip() or str(getattr(me, "id", ""))
        account = {
            "id": int(me.id),
            "display_name": display_name,
            "username": getattr(me, "username", None),
            "authorized_at": int(time.time()),
        }
        session_string = client.session.save()
        if not session_string:
            raise TelethonAuthError("Telethon не смог сохранить StringSession.")
        save_telethon_config(
            setup_pending=False,
            setup_started_at=0,
            account=account,
            enabled=True,
            session_string=session_string,
        )
        await self._disconnect_no_lock()
        self._step = "idle"
        return account

    async def check_existing(self) -> dict[str, Any]:
        async with self._lock:
            cfg = load_telethon_config()
            if not cfg.get("has_session"):
                self._step = "phone"
                return {"authorized": False, "step": "phone"}
            set_setup_pending(True)
            try:
                client = await self._ensure_client_no_lock()
                if await client.is_user_authorized():
                    account = await self._complete_no_lock(client)
                    return {"authorized": True, "account": account}
                save_telethon_config(account={}, session_string="")
                self._step = "phone"
                return {"authorized": False, "step": "phone"}
            except TelethonAuthError:
                raise
            except Exception as exc:
                raise TelethonAuthError(self._human_error(exc)) from exc

    async def send_code(self, phone: str) -> dict[str, Any]:
        phone = (phone or "").strip()
        if len(phone) < 7 or len(phone) > 32:
            raise TelethonAuthError("Введите номер телефона с кодом страны, например +491234567890.")
        async with self._lock:
            set_setup_pending(True)
            try:
                client = await self._ensure_client_no_lock()
                if await client.is_user_authorized():
                    account = await self._complete_no_lock(client)
                    return {"authorized": True, "account": account}
                sent = await client.send_code_request(phone)
                self._phone = phone
                self._phone_code_hash = getattr(sent, "phone_code_hash", None)
                self._step = "code"
                return {"authorized": False, "step": "code"}
            except TelethonAuthError:
                raise
            except Exception as exc:
                raise TelethonAuthError(self._human_error(exc)) from exc

    async def submit_code(self, code: str) -> dict[str, Any]:
        code = "".join(ch for ch in (code or "") if ch.isdigit())
        if not code:
            raise TelethonAuthError("Введите код, который прислал Telegram.")
        async with self._lock:
            if not self._phone:
                self._step = "phone"
                raise TelethonAuthError("Сессия мастера истекла. Сначала снова запросите код.")
            try:
                client = await self._ensure_client_no_lock()
                try:
                    await client.sign_in(
                        phone=self._phone,
                        code=code,
                        phone_code_hash=self._phone_code_hash,
                    )
                except Exception as exc:
                    if exc.__class__.__name__ == "SessionPasswordNeededError":
                        self._step = "password"
                        return {"authorized": False, "step": "password"}
                    raise
                account = await self._complete_no_lock(client)
                return {"authorized": True, "account": account}
            except TelethonAuthError:
                raise
            except Exception as exc:
                raise TelethonAuthError(self._human_error(exc)) from exc

    async def submit_password(self, password: str) -> dict[str, Any]:
        if not password:
            raise TelethonAuthError("Введите пароль двухэтапной аутентификации Telegram.")
        async with self._lock:
            try:
                client = await self._ensure_client_no_lock()
                await client.sign_in(password=password)
                account = await self._complete_no_lock(client)
                return {"authorized": True, "account": account}
            except TelethonAuthError:
                raise
            except Exception as exc:
                raise TelethonAuthError(self._human_error(exc)) from exc
