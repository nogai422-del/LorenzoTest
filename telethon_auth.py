from __future__ import annotations

import asyncio
import time
from datetime import timezone
from typing import Any

from telethon_config import load_telethon_config, save_telethon_config, set_setup_pending


class TelethonAuthError(RuntimeError):
    pass


class TelethonAuthManager:
    """Authorization wizard backed by Telethon StringSession.

    Sensitive one-time values (phone, code hash, QR token and 2FA flow state)
    only live in process memory. The resulting StringSession is persisted by
    telethon_config after successful authorization.
    """

    def __init__(self) -> None:
        self._client = None
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        self._step = "idle"
        self._lock = asyncio.Lock()
        self._code_info: dict[str, Any] = {}
        self._resend_available_at = 0

        self._qr_login = None
        self._qr_task: asyncio.Task | None = None
        self._qr_url: str | None = None
        self._qr_expires_at = 0
        self._qr_error: str | None = None

    @property
    def step(self) -> str:
        return self._step

    @property
    def qr_url(self) -> str | None:
        return self._qr_url

    def public_state(self) -> dict[str, Any]:
        return {
            "step": self._step,
            "code_info": dict(self._code_info),
            "resend_wait_seconds": max(0, int(self._resend_available_at - time.time())),
            "qr_active": bool(self._qr_url and self._step == "qr"),
            "qr_expires_at": self._qr_expires_at or None,
            "qr_error": self._qr_error,
        }

    async def _disconnect_no_lock(self) -> None:
        task = self._qr_task
        self._qr_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

        client = self._client
        self._client = None
        self._phone = None
        self._phone_code_hash = None
        self._code_info = {}
        self._resend_available_at = 0
        self._qr_login = None
        self._qr_url = None
        self._qr_expires_at = 0
        self._qr_error = None

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
            "ApiIdPublishedFloodError": "Этот API ID был опубликован/скомпрометирован и заблокирован Telegram. Создайте новый API ID/API Hash на my.telegram.org.",
            "PhoneNumberInvalidError": "Telegram не принял номер телефона. Проверьте международный формат с кодом страны.",
            "PhoneNumberBannedError": "Этот номер заблокирован Telegram.",
            "PhoneNumberFloodError": "Слишком много запросов кода для этого номера. Не запрашивайте код повторно и попробуйте позже.",
            "PhonePasswordFloodError": "Слишком много попыток входа. Telegram временно ограничил авторизацию; попробуйте позже.",
            "PhoneNumberAppSignupForbiddenError": "Этот аккаунт нельзя зарегистрировать через стороннее приложение. Сначала войдите/создайте аккаунт в официальном Telegram.",
            "PhoneCodeInvalidError": "Неверный код Telegram.",
            "PhoneCodeExpiredError": "Код Telegram истёк. Запросите новый или используйте QR-вход.",
            "PhoneCodeHashEmptyError": "Telegram потерял контекст кода. Начните авторизацию заново.",
            "PhoneCodeEmptyError": "Код Telegram пустой.",
            "PasswordHashInvalidError": "Неверный пароль двухэтапной аутентификации.",
            "AuthKeyDuplicatedError": "Telegram отклонил эту сессию. Переподключите Telethon через мастер.",
            "AuthRestartError": "Telegram попросил перезапустить авторизацию. Нажмите «Начать заново» или используйте QR-вход.",
            "SendCodeUnavailableError": "Telegram не может отправить код доступным способом. Используйте QR-вход — он не требует доставки кода.",
            "SmsCodeCreateFailedError": "Telegram не смог создать SMS-код. Для сторонних приложений SMS может быть недоступна; используйте QR-вход.",
            "UpdateAppToLoginError": "Telegram требует более новый клиент для входа. Обновите Telethon и повторите авторизацию.",
        }
        if name == "FloodWaitError":
            seconds = getattr(exc, "seconds", None)
            return f"Telegram ограничил запросы. Повторите через {seconds} сек." if seconds else "Telegram временно ограничил запросы."
        return mapping.get(name, f"Ошибка Telegram: {name}: {str(exc)[:180]}")

    @staticmethod
    def _sent_type_label(obj: Any) -> str:
        if obj is None:
            return "не указан"
        name = obj.__class__.__name__
        labels = {
            "SentCodeTypeApp": "служебное сообщение внутри Telegram (обычно от 777000)",
            "SentCodeTypeSms": "SMS",
            "SentCodeTypeSmsWord": "SMS со словом-кодом",
            "SentCodeTypeSmsPhrase": "SMS с фразой-кодом",
            "SentCodeTypeCall": "голосовой звонок",
            "SentCodeTypeFlashCall": "flash-call",
            "SentCodeTypeMissedCall": "пропущенный звонок",
            "SentCodeTypeEmailCode": "email",
            "SentCodeTypeSetUpEmailRequired": "нужно настроить email для входа",
            "SentCodeTypeFragmentSms": "Fragment",
            "SentCodeTypeFirebaseSms": "Firebase SMS (обычно только для официальных мобильных приложений)",
            "CodeTypeSms": "SMS",
            "CodeTypeCall": "голосовой звонок",
            "CodeTypeFlashCall": "flash-call",
            "CodeTypeMissedCall": "пропущенный звонок",
            "CodeTypeFragmentSms": "Fragment",
        }
        return labels.get(name, name)

    @classmethod
    def _describe_sent_code(cls, sent: Any) -> dict[str, Any]:
        sent_type = getattr(sent, "type", None)
        next_type = getattr(sent, "next_type", None)
        timeout = getattr(sent, "timeout", None)
        length = getattr(sent_type, "length", None)
        type_name = sent_type.__class__.__name__ if sent_type is not None else ""

        if type_name == "SentCodeTypeApp":
            hint = "Откройте Telegram на устройстве, где аккаунт уже авторизован, и проверьте служебный чат Telegram (777000). SMS для такого входа обычно не используется."
        elif type_name in {"SentCodeTypeFirebaseSms", "SentCodeTypeSms", "SentCodeTypeSmsWord", "SentCodeTypeSmsPhrase"}:
            hint = "Telegram выбрал SMS-канал. Для сторонних приложений часть SMS-сценариев может быть недоступна; если код не приходит, используйте QR-вход."
        elif type_name == "SentCodeTypeEmailCode":
            email_pattern = getattr(sent_type, "email_pattern", None)
            hint = f"Проверьте почту {email_pattern}." if email_pattern else "Проверьте email, привязанный Telegram для входа."
        elif type_name == "SentCodeTypeSetUpEmailRequired":
            hint = "Telegram требует сначала настроить email для входа. Проще использовать QR-вход с уже авторизованного устройства."
        elif type_name == "SentCodeTypeFragmentSms":
            url = getattr(sent_type, "url", None)
            hint = f"Telegram направил код через Fragment: {url}" if url else "Telegram направил код через Fragment."
        else:
            hint = "Если код не появляется, не запрашивайте его много раз подряд — используйте QR-вход."

        return {
            "delivery": cls._sent_type_label(sent_type),
            "delivery_type": type_name,
            "next_delivery": cls._sent_type_label(next_type) if next_type is not None else "",
            "timeout": int(timeout) if timeout is not None else 0,
            "length": int(length) if length is not None else None,
            "hint": hint,
        }

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
        phone = (phone or "").strip().replace(" ", "")
        if len(phone) < 7 or len(phone) > 32:
            raise TelethonAuthError("Введите номер телефона с кодом страны, например +491234567890.")
        async with self._lock:
            set_setup_pending(True)
            self._qr_error = None
            try:
                client = await self._ensure_client_no_lock()
                if await client.is_user_authorized():
                    account = await self._complete_no_lock(client)
                    return {"authorized": True, "account": account}
                sent = await client.send_code_request(phone)
                if await client.is_user_authorized():
                    account = await self._complete_no_lock(client)
                    return {"authorized": True, "account": account}

                self._phone = phone
                self._phone_code_hash = getattr(sent, "phone_code_hash", None)
                if not self._phone_code_hash:
                    raise TelethonAuthError("Telegram не вернул phone_code_hash. Используйте QR-вход.")
                self._code_info = self._describe_sent_code(sent)
                timeout = int(self._code_info.get("timeout") or 0)
                self._resend_available_at = int(time.time()) + timeout if timeout else 0
                self._step = "code"
                return {"authorized": False, "step": "code", "code_info": dict(self._code_info)}
            except TelethonAuthError:
                raise
            except Exception as exc:
                raise TelethonAuthError(self._human_error(exc)) from exc

    async def resend_code(self) -> dict[str, Any]:
        async with self._lock:
            if not self._phone or not self._phone_code_hash:
                self._step = "phone"
                raise TelethonAuthError("Сначала запросите код по номеру телефона.")
            wait = max(0, int(self._resend_available_at - time.time()))
            if wait > 0:
                raise TelethonAuthError(f"Telegram разрешит повторную отправку примерно через {wait} сек. Не делайте лишних запросов.")
            try:
                from telethon import functions
                client = await self._ensure_client_no_lock()
                sent = await client(functions.auth.ResendCodeRequest(
                    phone_number=self._phone,
                    phone_code_hash=self._phone_code_hash,
                ))
                self._phone_code_hash = getattr(sent, "phone_code_hash", None) or self._phone_code_hash
                self._code_info = self._describe_sent_code(sent)
                timeout = int(self._code_info.get("timeout") or 0)
                self._resend_available_at = int(time.time()) + timeout if timeout else 0
                self._step = "code"
                return {"authorized": False, "step": "code", "code_info": dict(self._code_info)}
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

    async def _qr_waiter(self, qr_login) -> None:
        try:
            await qr_login.wait()
            async with self._lock:
                if self._client is not None and await self._client.is_user_authorized():
                    await self._complete_no_lock(self._client)
        except asyncio.CancelledError:
            return
        except asyncio.TimeoutError:
            async with self._lock:
                await self._disconnect_no_lock()
                self._step = "qr_expired"
                self._qr_error = "QR-код истёк. Создайте новый и отсканируйте его в Telegram → Настройки → Устройства."
        except Exception as exc:
            if exc.__class__.__name__ == "SessionPasswordNeededError":
                async with self._lock:
                    self._qr_task = None
                    self._qr_login = None
                    self._qr_url = None
                    self._qr_expires_at = 0
                    self._step = "password"
                return
            async with self._lock:
                message = self._human_error(exc)
                await self._disconnect_no_lock()
                self._step = "qr_error"
                self._qr_error = message

    async def start_qr(self) -> dict[str, Any]:
        async with self._lock:
            set_setup_pending(True)
            await self._disconnect_no_lock()
            self._qr_error = None
            try:
                client = await self._ensure_client_no_lock()
                if await client.is_user_authorized():
                    account = await self._complete_no_lock(client)
                    return {"authorized": True, "account": account}

                qr_login = await client.qr_login()
                self._qr_login = qr_login
                self._qr_url = str(qr_login.url)
                expires = getattr(qr_login, "expires", None)
                if expires is not None:
                    if getattr(expires, "tzinfo", None) is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    self._qr_expires_at = int(expires.timestamp())
                else:
                    self._qr_expires_at = int(time.time()) + 60
                self._step = "qr"
                self._qr_task = asyncio.create_task(self._qr_waiter(qr_login), name="telethon-qr-login")
                return {
                    "authorized": False,
                    "step": "qr",
                    "qr_url": self._qr_url,
                    "qr_expires_at": self._qr_expires_at,
                }
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
