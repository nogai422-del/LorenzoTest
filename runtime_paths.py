from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


def _resolve_path(raw: str | None, default: Path) -> Path:
    if not raw:
        return default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


# Bothost deploys the project under /app and keeps /app/data persistent.
# Locally this resolves to <project>/data, so the same code works everywhere.
DATA_DIR = _resolve_path(os.getenv("DATA_DIR"), PROJECT_DIR / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(DATA_DIR, 0o700)
except OSError:
    pass

DATABASE_PATH = _resolve_path(
    os.getenv("DATABASE_PATH") or os.getenv("DB_PATH"),
    DATA_DIR / "bot.db",
)
SETTINGS_PATH = _resolve_path(os.getenv("SETTINGS_PATH"), DATA_DIR / "settings.json")
TELETHON_CONFIG_PATH = _resolve_path(os.getenv("TELETHON_CONFIG_PATH"), DATA_DIR / ".telethon_config.json")
LOG_PATH = _resolve_path(os.getenv("LOG_PATH"), DATA_DIR / "bot.log")
WEB_SECRET_PATH = _resolve_path(os.getenv("WEB_SECRET_PATH"), DATA_DIR / ".web_secret_key")

for path in (DATABASE_PATH, SETTINGS_PATH, TELETHON_CONFIG_PATH, LOG_PATH, WEB_SECRET_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)


def _copy_if_missing(source: Path, destination: Path) -> bool:
    """Copy old root-level state into persistent storage once.

    This keeps upgrades compatible with previous Lorenzo builds. Existing
    persistent files always win and are never overwritten by a redeploy.
    """
    if destination.exists() or not source.exists() or source.resolve() == destination.resolve():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    return True


def migrate_root_state() -> dict[str, bool]:
    return {
        "database": _copy_if_missing(PROJECT_DIR / "bot.db", DATABASE_PATH),
        "settings": _copy_if_missing(PROJECT_DIR / "settings.json", SETTINGS_PATH),
        "telethon_config": _copy_if_missing(PROJECT_DIR / ".telethon_config.json", TELETHON_CONFIG_PATH),
    }


# Migrate before db/settings modules open their files.
MIGRATED_ROOT_STATE = migrate_root_state()


def web_public_url() -> str:
    explicit = (os.getenv("WEB_PUBLIC_URL") or "").strip().rstrip("/")
    if explicit:
        if "://" not in explicit:
            return "https://" + explicit
        return explicit

    # Bothost exposes DOMAIN automatically when a domain is enabled.
    domain = (os.getenv("DOMAIN") or "").strip().strip("/")
    if domain:
        if "://" in domain:
            return domain.rstrip("/")
        return "https://" + domain
    return ""


def web_port() -> int:
    # Bothost sets PORT to the internal port selected in the hosting panel.
    raw = (os.getenv("PORT") or os.getenv("WEB_PORT") or "8080").strip()
    try:
        port = int(raw)
    except ValueError:
        port = 8080
    return port if 1 <= port <= 65535 else 8080


def load_or_create_web_secret() -> str:
    configured = (os.getenv("WEB_SECRET_KEY") or "").strip()
    if configured:
        return configured

    if WEB_SECRET_PATH.exists():
        try:
            value = WEB_SECRET_PATH.read_text(encoding="utf-8").strip()
            if len(value) >= 32:
                return value
        except OSError:
            pass

    import secrets

    value = secrets.token_urlsafe(64)
    fd = os.open(WEB_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    try:
        os.chmod(WEB_SECRET_PATH, 0o600)
    except OSError:
        pass
    return value
