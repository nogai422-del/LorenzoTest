from __future__ import annotations

import json
import os
import time
from typing import Any

from dotenv import load_dotenv

from runtime_paths import TELETHON_CONFIG_PATH

load_dotenv()

CONFIG_PATH = TELETHON_CONFIG_PATH
SETUP_TTL_SECONDS = 30 * 60
_UNSET = object()


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _first_env(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _read_file() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_telethon_config() -> dict[str, Any]:
    stored = _read_file()

    api_id_raw = stored.get("api_id") or _first_env("TELETHON_API_ID", "TELEGRAM_API_ID")
    try:
        api_id = int(api_id_raw) if str(api_id_raw).strip() else None
    except (TypeError, ValueError):
        api_id = None

    api_hash = str(
        stored.get("api_hash")
        or _first_env("TELETHON_API_HASH", "TELEGRAM_API_HASH")
        or ""
    ).strip()

    stored_session = str(stored.get("session_string") or "").strip()
    env_session = _first_env("TELETHON_SESSION_STRING", "SESSION_STRING")
    session_string = stored_session or env_session
    session_source = "storage" if stored_session else ("environment" if env_session else "none")

    enabled = bool(stored["enabled"]) if "enabled" in stored else _env_bool("TELETHON_ENABLED", True)
    setup_pending = bool(stored.get("setup_pending", False))
    setup_started_at = int(stored.get("setup_started_at") or 0)
    if setup_pending and setup_started_at and int(time.time()) - setup_started_at > SETUP_TTL_SECONDS:
        setup_pending = False

    account = stored.get("account") if isinstance(stored.get("account"), dict) else {}
    return {
        "enabled": enabled,
        "api_id": api_id,
        "api_hash": api_hash,
        "session_string": session_string,
        "session_source": session_source,
        "has_session": bool(session_string),
        "setup_pending": setup_pending,
        "setup_started_at": setup_started_at,
        "account": account,
        "configured": bool(api_id and api_hash),
    }


def save_telethon_config(
    *,
    api_id: int | None = None,
    api_hash: str | None = None,
    enabled: bool | None = None,
    setup_pending: bool | None = None,
    setup_started_at: int | None = None,
    account: dict[str, Any] | None = None,
    session_string: str | None | object = _UNSET,
) -> dict[str, Any]:
    current = _read_file()

    if api_id is not None:
        if int(api_id) <= 0:
            raise ValueError("API ID должен быть положительным числом")
        current["api_id"] = int(api_id)
    if api_hash is not None:
        value = str(api_hash).strip()
        if not value:
            raise ValueError("API Hash не может быть пустым")
        current["api_hash"] = value
    if enabled is not None:
        current["enabled"] = bool(enabled)
    if setup_pending is not None:
        current["setup_pending"] = bool(setup_pending)
    if setup_started_at is not None:
        current["setup_started_at"] = int(setup_started_at)
    if account is not None:
        current["account"] = account
    if session_string is not _UNSET:
        value = str(session_string or "").strip()
        if value:
            current["session_string"] = value
        else:
            current.pop("session_string", None)

    # Remove fields used by the old SQLite .session implementation.
    current.pop("session", None)

    current["updated_at"] = int(time.time())
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    payload = json.dumps(current, ensure_ascii=False, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        finally:
            raise
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return load_telethon_config()


def set_setup_pending(value: bool) -> dict[str, Any]:
    return save_telethon_config(
        setup_pending=value,
        setup_started_at=int(time.time()) if value else 0,
    )


def telethon_status() -> dict[str, Any]:
    cfg = load_telethon_config()
    account = cfg.get("account") or {}
    authorized = bool(account.get("id") and cfg.get("has_session"))

    if not cfg["enabled"]:
        label = "Выключен"
        kind = "warn"
    elif authorized:
        label = "Подключён"
        kind = "ok"
    elif cfg["configured"] and cfg["has_session"]:
        label = "Проверка авторизации"
        kind = "warn"
    elif cfg["configured"]:
        label = "Нужна авторизация"
        kind = "warn"
    else:
        label = "Не настроен"
        kind = "danger"

    # Never pass the actual API hash or StringSession into templates/status messages.
    return {
        "enabled": cfg["enabled"],
        "api_id": cfg["api_id"],
        "api_hash_set": bool(cfg["api_hash"]),
        "configured": cfg["configured"],
        "has_session": cfg["has_session"],
        "session_source": cfg["session_source"],
        "setup_pending": cfg["setup_pending"],
        "account": account,
        "authorized": authorized,
        "status_label": label,
        "status_kind": kind,
        "session": "StringSession" if cfg["has_session"] else "—",
        "storage_file": str(CONFIG_PATH),
    }
