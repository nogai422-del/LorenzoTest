from __future__ import annotations

import json
import os
import threading
from typing import Any

from runtime_paths import SETTINGS_PATH

SETTINGS_FILE = SETTINGS_PATH
_LOCK = threading.RLock()

DEFAULT_TEXTS = {
    "welcome_text": "Привет, {name}! Добро пожаловать к нам. 😊\nПодскажи, сколько тебе лет?",
    "consent_text": "Отлично! {age} — прекрасный возраст.\n\nЧтобы мы могли добавить тебя в списки и дать доступ, готов(а) заполнить небольшую анкету?",
}


def default_settings(owner_id: int) -> dict[str, Any]:
    return {
        "level": 1,
        "reply_delay": 1,
        "work_start": "07:00",
        "work_end": "19:00",
        "is_active": True,
        "allow_admins_edit": True,
        "texts": dict(DEFAULT_TEXTS),
    }


def normalize_settings(data: dict[str, Any] | None, owner_id: int) -> dict[str, Any]:
    result = default_settings(owner_id)
    result.update(data or {})
    try:
        level = int(result.get("level", 1))
    except (TypeError, ValueError):
        level = 1
    result["level"] = level if level in (1, 2, 3) else 1
    try:
        result["reply_delay"] = max(0, min(360, int(result.get("reply_delay", 0))))
    except (TypeError, ValueError):
        result["reply_delay"] = 0
    result["is_active"] = bool(result.get("is_active", True))
    result["allow_admins_edit"] = bool(result.get("allow_admins_edit", True))
    result["work_start"] = str(result.get("work_start", "07:00"))
    result["work_end"] = str(result.get("work_end", "19:00"))
    texts = result.get("texts")
    if not isinstance(texts, dict):
        texts = {}
    merged_texts = dict(DEFAULT_TEXTS)
    merged_texts.update({k: str(v) for k, v in texts.items() if k in DEFAULT_TEXTS})
    result["texts"] = merged_texts
    # notify_admins was used by older versions. Notification recipients now live in system_admins.
    result.pop("notify_admins", None)
    return result


def load_settings(owner_id: int) -> dict[str, Any]:
    with _LOCK:
        if not SETTINGS_FILE.exists():
            result = default_settings(owner_id)
            save_settings(result, owner_id)
            return result
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        return normalize_settings(data, owner_id)


def save_settings(settings: dict[str, Any], owner_id: int) -> dict[str, Any]:
    normalized = normalize_settings(settings, owner_id)
    with _LOCK:
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
        try:
            os.chmod(SETTINGS_FILE, 0o600)
        except OSError:
            pass
    return normalized


def update_settings(owner_id: int, **changes: Any) -> dict[str, Any]:
    current = load_settings(owner_id)
    current.update(changes)
    return save_settings(current, owner_id)
