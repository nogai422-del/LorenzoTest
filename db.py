from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from runtime_paths import DATA_DIR, DATABASE_PATH, PROJECT_DIR

DB_FILE = DATABASE_PATH
VALID_CHAT_TYPES = {"group", "supergroup"}

DEFAULT_SETTINGS = {
    "inactivity_days": 3,
    "check_interval_minutes": 60,
    "repeat_alert_hours": 24,
    "min_message_count": 0,
    "enabled": 1,
    "last_check_at": 0,
}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    try:
        os.chmod(DB_FILE, 0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, name: str, sql_type_and_default: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type_and_default}")


def init_db() -> None:
    now_ts = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                chat_type TEXT,
                last_seen_at INTEGER NOT NULL DEFAULT 0,
                is_tracked INTEGER NOT NULL DEFAULT 0,
                is_hidden INTEGER NOT NULL DEFAULT 0,
                discovered_by TEXT,
                participant_count INTEGER NOT NULL DEFAULT 0,
                last_member_sync_at INTEGER NOT NULL DEFAULT 0,
                last_history_sync_at INTEGER NOT NULL DEFAULT 0,
                sync_error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                username TEXT,
                joined_at INTEGER,
                left_at INTEGER,
                first_seen_at INTEGER,
                updated_at INTEGER,
                last_message_at INTEGER,
                last_message_id INTEGER,
                telegram_last_seen_at INTEGER,
                telegram_status TEXT,
                telegram_status_checked_at INTEGER,
                total_message_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_bot INTEGER NOT NULL DEFAULT 0,
                membership_status TEXT NOT NULL DEFAULT 'member',
                sync_token TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS member_daily_activity (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                last_message_at INTEGER,
                PRIMARY KEY (chat_id, user_id, day)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                sent_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                inactivity_days INTEGER NOT NULL DEFAULT 3,
                check_interval_minutes INTEGER NOT NULL DEFAULT 60,
                repeat_alert_hours INTEGER NOT NULL DEFAULT 24,
                min_message_count INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_check_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inactivity_alerts (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                alert_type TEXT NOT NULL DEFAULT 'inactivity',
                last_alerted_at INTEGER,
                PRIMARY KEY (chat_id, user_id, alert_type)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_admins (
                user_id INTEGER PRIMARY KEY,
                display_name TEXT,
                username TEXT,
                is_owner INTEGER NOT NULL DEFAULT 0,
                notifications_enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                request_type TEXT NOT NULL DEFAULT 'full',
                requested_by TEXT,
                requested_at INTEGER NOT NULL,
                completed_at INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                details TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )

        # Migrations from the original project.
        _add_column(conn, "chats", "is_tracked", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "chats", "is_hidden", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "chats", "discovered_by", "TEXT")
        _add_column(conn, "chats", "participant_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "chats", "last_member_sync_at", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "chats", "last_history_sync_at", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "chats", "sync_error", "TEXT")

        _add_column(conn, "chat_members", "username", "TEXT")
        _add_column(conn, "chat_members", "last_message_id", "INTEGER")
        _add_column(conn, "chat_members", "telegram_last_seen_at", "INTEGER")
        _add_column(conn, "chat_members", "telegram_status", "TEXT")
        _add_column(conn, "chat_members", "telegram_status_checked_at", "INTEGER")
        _add_column(conn, "chat_members", "first_seen_at", "INTEGER")
        _add_column(conn, "chat_members", "updated_at", "INTEGER")
        _add_column(conn, "chat_members", "is_bot", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "chat_members", "membership_status", "TEXT NOT NULL DEFAULT 'member'")
        _add_column(conn, "chat_members", "sync_token", "TEXT")

        _add_column(conn, "chat_settings", "repeat_alert_hours", "INTEGER NOT NULL DEFAULT 24")
        _add_column(conn, "chat_settings", "min_message_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "chat_settings", "enabled", "INTEGER NOT NULL DEFAULT 1")
        _add_column(conn, "chat_settings", "last_check_at", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "inactivity_alerts", "alert_type", "TEXT NOT NULL DEFAULT 'inactivity'")

        alert_pk = [row["name"] for row in conn.execute("PRAGMA table_info(inactivity_alerts)") if row["pk"]]
        if alert_pk != ["chat_id", "user_id", "alert_type"]:
            conn.execute(
                """
                CREATE TABLE inactivity_alerts_new (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    alert_type TEXT NOT NULL DEFAULT 'inactivity',
                    last_alerted_at INTEGER,
                    PRIMARY KEY (chat_id, user_id, alert_type)
                )
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO inactivity_alerts_new(chat_id, user_id, alert_type, last_alerted_at)
                SELECT chat_id, user_id, COALESCE(alert_type, 'inactivity'), last_alerted_at
                FROM inactivity_alerts
                """
            )
            conn.execute("DROP TABLE inactivity_alerts")
            conn.execute("ALTER TABLE inactivity_alerts_new RENAME TO inactivity_alerts")

        # Recover group metadata from the legacy members_panel.sqlite3 if present.
        legacy_candidates = [DATA_DIR / "members_panel.sqlite3", PROJECT_DIR / "members_panel.sqlite3"]
        legacy_db = next((path for path in legacy_candidates if path.exists()), None)
        if legacy_db is not None:
            try:
                legacy = sqlite3.connect(legacy_db)
                legacy.row_factory = sqlite3.Row
                legacy_tables = {r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "known_chats" in legacy_tables:
                    for row in legacy.execute("SELECT * FROM known_chats"):
                        chat_type = str(row["type"] or "")
                        chat_id = int(row["chat_id"])
                        if chat_id < 0 and chat_type in VALID_CHAT_TYPES:
                            conn.execute(
                                """
                                INSERT INTO chats(chat_id, title, username, chat_type, last_seen_at, is_tracked, discovered_by)
                                VALUES (?, ?, ?, ?, ?, 1, 'legacy')
                                ON CONFLICT(chat_id) DO UPDATE SET
                                    title=COALESCE(chats.title, excluded.title),
                                    username=COALESCE(chats.username, excluded.username),
                                    chat_type=excluded.chat_type,
                                    last_seen_at=MAX(chats.last_seen_at, excluded.last_seen_at)
                                """,
                                (chat_id, row["title"], row["username"], chat_type, int(row["last_seen_ts"] or 0)),
                            )
                if "known_members" in legacy_tables:
                    for row in legacy.execute("SELECT * FROM known_members"):
                        chat_id = int(row["chat_id"])
                        if chat_id >= 0:
                            continue
                        exists = conn.execute(
                            "SELECT 1 FROM chats WHERE chat_id=? AND chat_type IN ('group','supergroup')",
                            (chat_id,),
                        ).fetchone()
                        if not exists:
                            continue
                        conn.execute(
                            """
                            INSERT INTO chat_members(
                                chat_id, user_id, user_name, username, joined_at, left_at, first_seen_at, updated_at,
                                last_message_at, total_message_count, is_active, is_bot, membership_status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'member')
                            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                                user_name=COALESCE(chat_members.user_name, excluded.user_name),
                                username=COALESCE(chat_members.username, excluded.username),
                                first_seen_at=COALESCE(chat_members.first_seen_at, excluded.first_seen_at),
                                updated_at=MAX(COALESCE(chat_members.updated_at,0), excluded.updated_at),
                                total_message_count=MAX(chat_members.total_message_count, excluded.total_message_count),
                                is_bot=MAX(chat_members.is_bot, excluded.is_bot)
                            """,
                            (
                                chat_id, int(row["user_id"]), row["name"], row["username"],
                                int(row["joined_at"]) if row["joined_at"] else None,
                                int(row["left_at"]) if row["left_at"] else None,
                                int(row["first_seen_ts"] or 0), int(row["last_seen_ts"] or now_ts),
                                int(row["last_seen_ts"] or 0) if int(row["message_count"] or 0) > 0 else None,
                                int(row["message_count"] or 0),
                                0 if row["left_at"] else 1, int(row["is_bot"] or 0),
                            ),
                        )
                legacy.close()
            except Exception:
                # Legacy import is best-effort; the new database remains usable without it.
                pass

        # Recover orphaned negative group settings/members from older bot.db revisions.
        orphan_group_ids = {
            int(r[0]) for r in conn.execute(
                """
                SELECT chat_id FROM chat_settings WHERE chat_id < 0
                UNION
                SELECT chat_id FROM chat_members WHERE chat_id < 0
                """
            )
        }
        for chat_id in orphan_group_ids:
            exists = conn.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
            if not exists:
                inferred_type = "supergroup" if str(chat_id).startswith("-100") else "group"
                conn.execute(
                    """
                    INSERT INTO chats(chat_id, title, username, chat_type, last_seen_at, is_tracked, discovered_by)
                    VALUES (?, ?, NULL, ?, ?, 1, 'legacy-inferred')
                    """,
                    (chat_id, str(chat_id), inferred_type, now_ts),
                )

        # Personal chats have positive Bot API IDs. They must never participate in tracking/statistics.
        conn.execute("DELETE FROM chat_settings WHERE chat_id > 0")
        conn.execute("DELETE FROM inactivity_alerts WHERE chat_id > 0")
        conn.execute("DELETE FROM member_daily_activity WHERE chat_id > 0")
        conn.execute("DELETE FROM seen_messages WHERE chat_id > 0")
        conn.execute("DELETE FROM chat_members WHERE chat_id > 0")
        conn.execute("DELETE FROM chats WHERE chat_id > 0 OR COALESCE(chat_type,'') NOT IN ('group','supergroup')")
        conn.execute(
            "UPDATE chat_members SET first_seen_at=COALESCE(first_seen_at, joined_at, last_message_at, ?), updated_at=COALESCE(updated_at, ?)",
            (now_ts, now_ts),
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_tracked ON chats(is_tracked, chat_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_members_chat_active_last ON chat_members(chat_id, is_active, last_message_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_members_sync_token ON chat_members(chat_id, sync_token)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_chat_day ON member_daily_activity(chat_id, day)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_messages_sent ON seen_messages(sent_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_last ON inactivity_alerts(chat_id, user_id, last_alerted_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_requests_status ON sync_requests(status, requested_at)")
        conn.commit()


def is_valid_group_type(chat_type: str | None) -> bool:
    return str(chat_type or "").lower() in VALID_CHAT_TYPES


def record_chat(
    chat_id: int,
    title: str | None,
    username: str | None,
    chat_type: str | None,
    now_ts: int | None = None,
    *,
    tracked: bool | None = None,
    discovered_by: str | None = None,
) -> bool:
    """Record groups only. Returns False for users/private chats/channels."""
    if not is_valid_group_type(chat_type) or int(chat_id) >= 0:
        return False
    now_ts = int(now_ts or time.time())
    with get_conn() as conn:
        previous = conn.execute("SELECT is_tracked, is_hidden FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()
        is_hidden = bool(previous and int(previous["is_hidden"] or 0))
        if is_hidden:
            tracked_value = 0
        elif tracked is None:
            tracked_value = int(previous["is_tracked"]) if previous else 0
        else:
            tracked_value = 1 if tracked else 0
        conn.execute(
            """
            INSERT INTO chats(chat_id, title, username, chat_type, last_seen_at, is_tracked, discovered_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=COALESCE(excluded.title, chats.title),
                username=COALESCE(excluded.username, chats.username),
                chat_type=excluded.chat_type,
                last_seen_at=MAX(chats.last_seen_at, excluded.last_seen_at),
                is_tracked=CASE
                    WHEN COALESCE(chats.is_hidden,0)=1 THEN 0
                    WHEN ? IS NULL THEN chats.is_tracked
                    ELSE excluded.is_tracked
                END,
                discovered_by=COALESCE(excluded.discovered_by, chats.discovered_by)
            """,
            (int(chat_id), title, username, chat_type, now_ts, tracked_value, discovered_by, tracked),
        )
        conn.commit()
    return True


def set_chat_tracked(chat_id: int, tracked: bool) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT chat_type, is_hidden FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()
        if not row or int(chat_id) >= 0 or not is_valid_group_type(row["chat_type"]):
            return False
        # Hidden chats are owner-archived and cannot silently re-enter monitoring.
        if tracked and int(row["is_hidden"] or 0):
            return False
        conn.execute("UPDATE chats SET is_tracked=? WHERE chat_id=?", (1 if tracked else 0, int(chat_id)))
        if tracked:
            conn.execute("INSERT OR IGNORE INTO chat_settings(chat_id) VALUES (?)", (int(chat_id),))
        conn.commit()
    return True


def set_chat_hidden(chat_id: int, hidden: bool) -> bool:
    """Owner-only archive flag. Hidden chats are removed from monitoring and alerts."""
    with get_conn() as conn:
        row = conn.execute("SELECT chat_type FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()
        if not row or int(chat_id) >= 0 or not is_valid_group_type(row["chat_type"]):
            return False
        conn.execute(
            "UPDATE chats SET is_hidden=?, is_tracked=CASE WHEN ? THEN 0 ELSE is_tracked END WHERE chat_id=?",
            (1 if hidden else 0, 1 if hidden else 0, int(chat_id)),
        )
        if hidden:
            conn.execute("UPDATE chat_settings SET enabled=0 WHERE chat_id=?", (int(chat_id),))
            conn.execute(
                "UPDATE sync_requests SET status='cancelled', completed_at=?, error='Chat hidden by owner' "
                "WHERE chat_id=? AND status IN ('pending','running')",
                (int(time.time()), int(chat_id)),
            )
        conn.commit()
    return True


def list_known_chats(*, include_untracked: bool = True, include_hidden: bool = False) -> list[sqlite3.Row]:
    where = "c.chat_type IN ('group','supergroup')"
    if not include_hidden:
        where += " AND COALESCE(c.is_hidden,0)=0"
    if not include_untracked:
        where += " AND c.is_tracked=1"
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT c.*, COALESCE(s.enabled, 1) AS alerts_enabled
            FROM chats c
            LEFT JOIN chat_settings s ON s.chat_id=c.chat_id
            WHERE {where}
            ORDER BY c.is_hidden ASC, c.is_tracked DESC, c.last_seen_at DESC, c.title COLLATE NOCASE
            """
        ).fetchall()


def list_hidden_chats() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT c.*, COALESCE(s.enabled, 0) AS alerts_enabled
            FROM chats c
            LEFT JOIN chat_settings s ON s.chat_id=c.chat_id
            WHERE c.chat_type IN ('group','supergroup') AND COALESCE(c.is_hidden,0)=1
            ORDER BY c.last_seen_at DESC, c.title COLLATE NOCASE
            """
        ).fetchall()


def list_tracked_chat_ids() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM chats WHERE is_tracked=1 AND COALESCE(is_hidden,0)=0 AND chat_type IN ('group','supergroup') ORDER BY chat_id"
        ).fetchall()
    return [int(r["chat_id"]) for r in rows]


def get_chat_info(chat_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()
    return dict(row) if row else None


def update_chat_sync_status(
    chat_id: int,
    *,
    participant_count: int | None = None,
    member_synced_at: int | None = None,
    history_synced_at: int | None = None,
    error: str | None = None,
) -> None:
    fields: list[str] = []
    values: list[object] = []
    if participant_count is not None:
        fields.append("participant_count=?")
        values.append(max(0, int(participant_count)))
    if member_synced_at is not None:
        fields.append("last_member_sync_at=?")
        values.append(max(0, int(member_synced_at)))
    if history_synced_at is not None:
        fields.append("last_history_sync_at=?")
        values.append(max(0, int(history_synced_at)))
    if error is not None:
        fields.append("sync_error=?")
        values.append(error[:1000] if error else None)
    if not fields:
        return
    values.append(int(chat_id))
    with get_conn() as conn:
        conn.execute(f"UPDATE chats SET {', '.join(fields)} WHERE chat_id=?", values)
        conn.commit()


def ensure_chat_settings(chat_id: int) -> None:
    info = get_chat_info(chat_id)
    if info and not is_valid_group_type(info.get("chat_type")):
        return
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO chat_settings(chat_id) VALUES (?)", (int(chat_id),))
        conn.commit()


def get_chat_settings(chat_id: int) -> dict:
    ensure_chat_settings(chat_id)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_settings WHERE chat_id=?", (int(chat_id),)).fetchone()
    result = dict(DEFAULT_SETTINGS)
    if row:
        result.update(dict(row))
    result["chat_id"] = int(chat_id)
    return result


def set_chat_settings(chat_id: int, **changes) -> None:
    current = get_chat_settings(chat_id)
    values = {
        "inactivity_days": max(1, min(3650, int(changes.get("inactivity_days", current["inactivity_days"])))),
        "check_interval_minutes": max(1, min(10080, int(changes.get("check_interval_minutes", current["check_interval_minutes"])))),
        "repeat_alert_hours": max(1, min(8760, int(changes.get("repeat_alert_hours", current["repeat_alert_hours"])))),
        "min_message_count": max(0, min(1_000_000, int(changes.get("min_message_count", current["min_message_count"])))),
        "enabled": 1 if bool(changes.get("enabled", current["enabled"])) else 0,
        "last_check_at": max(0, int(changes.get("last_check_at", current["last_check_at"]))),
    }
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO chat_settings(
                chat_id, inactivity_days, check_interval_minutes, repeat_alert_hours,
                min_message_count, enabled, last_check_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                inactivity_days=excluded.inactivity_days,
                check_interval_minutes=excluded.check_interval_minutes,
                repeat_alert_hours=excluded.repeat_alert_hours,
                min_message_count=excluded.min_message_count,
                enabled=excluded.enabled,
                last_check_at=excluded.last_check_at
            """,
            (
                int(chat_id), values["inactivity_days"], values["check_interval_minutes"],
                values["repeat_alert_hours"], values["min_message_count"], values["enabled"], values["last_check_at"],
            ),
        )
        conn.commit()


def mark_chat_checked(chat_id: int, now_ts: int) -> None:
    set_chat_settings(chat_id, last_check_at=now_ts)


def _day_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()


def upsert_message_activity(
    chat_id: int,
    user_id: int,
    user_name: str,
    now_ts: int,
    message_id: int | None = None,
    username: str | None = None,
    *,
    is_bot: bool = False,
    mark_active: bool = True,
) -> bool:
    """Record one message exactly once.

    ``mark_active=False`` is used for historical backfills: old messages update
    activity counters but must never resurrect a user who has already left.
    """
    now_ts = int(now_ts)
    message_id = int(message_id) if message_id is not None else None
    with get_conn() as conn:
        if message_id is not None:
            cur = conn.execute(
                "INSERT OR IGNORE INTO seen_messages(chat_id, message_id, user_id, sent_at) VALUES (?, ?, ?, ?)",
                (int(chat_id), message_id, int(user_id), now_ts),
            )
            if cur.rowcount == 0:
                return False
        first_seen = now_ts
        conn.execute(
            """
            INSERT INTO chat_members(
                chat_id, user_id, user_name, username, joined_at, left_at,
                first_seen_at, updated_at, last_message_at, last_message_id,
                total_message_count, is_active, is_bot, membership_status
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                user_name=CASE
                    WHEN chat_members.user_name IS NULL OR chat_members.user_name=CAST(chat_members.user_id AS TEXT)
                    THEN COALESCE(excluded.user_name, chat_members.user_name)
                    WHEN excluded.user_name=CAST(excluded.user_id AS TEXT) THEN chat_members.user_name
                    ELSE COALESCE(excluded.user_name, chat_members.user_name)
                END,
                username=COALESCE(excluded.username, chat_members.username),
                updated_at=excluded.updated_at,
                last_message_at=MAX(COALESCE(chat_members.last_message_at, 0), excluded.last_message_at),
                last_message_id=CASE WHEN excluded.last_message_at >= COALESCE(chat_members.last_message_at, 0)
                                     THEN excluded.last_message_id ELSE chat_members.last_message_id END,
                total_message_count=chat_members.total_message_count+1,
                is_active=CASE WHEN ?=1 THEN 1 ELSE chat_members.is_active END,
                is_bot=excluded.is_bot,
                membership_status=CASE
                    WHEN ?=0 THEN chat_members.membership_status
                    WHEN chat_members.membership_status IN ('admin','creator') THEN chat_members.membership_status
                    ELSE 'member'
                END,
                left_at=CASE WHEN ?=1 THEN NULL ELSE chat_members.left_at END
            """,
            (
                int(chat_id), int(user_id), user_name, username, first_seen,
                now_ts, now_ts, message_id,
                1 if mark_active else 0,
                1 if is_bot else 0,
                "member" if mark_active else "left",
                1 if mark_active else 0,
                1 if mark_active else 0,
                1 if mark_active else 0,
            ),
        )
        day = _day_from_ts(now_ts)
        conn.execute(
            """
            INSERT INTO member_daily_activity(chat_id, user_id, day, message_count, last_message_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
                message_count=member_daily_activity.message_count+1,
                last_message_at=MAX(COALESCE(member_daily_activity.last_message_at, 0), excluded.last_message_at)
            """,
            (int(chat_id), int(user_id), day, now_ts),
        )
        conn.execute("DELETE FROM inactivity_alerts WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        conn.commit()
    return True


def upsert_member_snapshot(
    chat_id: int,
    user_id: int,
    user_name: str | None,
    username: str | None,
    *,
    joined_at: int | None = None,
    telegram_last_seen_at: int | None = None,
    telegram_status: str | None = None,
    telegram_status_checked_at: int | None = None,
    is_bot: bool = False,
    membership_status: str = "member",
    sync_token: str | None = None,
    preserve_membership_status: bool = False,
) -> None:
    now_ts = int(time.time())
    first_seen = int(joined_at or now_ts)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO chat_members(
                chat_id, user_id, user_name, username, joined_at, left_at,
                first_seen_at, updated_at, telegram_last_seen_at, telegram_status,
                telegram_status_checked_at, total_message_count, is_active, is_bot,
                membership_status, sync_token
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                user_name=COALESCE(excluded.user_name, chat_members.user_name),
                username=excluded.username,
                joined_at=COALESCE(chat_members.joined_at, excluded.joined_at),
                first_seen_at=COALESCE(chat_members.first_seen_at, excluded.first_seen_at),
                updated_at=excluded.updated_at,
                telegram_last_seen_at=CASE
                    WHEN excluded.telegram_last_seen_at IS NULL THEN chat_members.telegram_last_seen_at
                    ELSE excluded.telegram_last_seen_at
                END,
                telegram_status=COALESCE(excluded.telegram_status, chat_members.telegram_status),
                telegram_status_checked_at=COALESCE(excluded.telegram_status_checked_at, chat_members.telegram_status_checked_at),
                is_active=1,
                is_bot=excluded.is_bot,
                membership_status=CASE WHEN ? THEN chat_members.membership_status ELSE excluded.membership_status END,
                sync_token=COALESCE(excluded.sync_token, chat_members.sync_token),
                left_at=NULL
            """,
            (
                int(chat_id), int(user_id), user_name, username,
                int(joined_at) if joined_at else None, first_seen, now_ts,
                int(telegram_last_seen_at) if telegram_last_seen_at else None,
                telegram_status, int(telegram_status_checked_at) if telegram_status_checked_at else None,
                1 if is_bot else 0, membership_status, sync_token,
                1 if preserve_membership_status else 0,
            ),
        )
        conn.commit()


def import_member(
    chat_id: int,
    user_id: int,
    user_name: str | None,
    username: str | None,
    joined_at: int,
    message_count: int = 0,
    telegram_last_seen_at: int | None = None,
    telegram_status: str | None = None,
    telegram_status_checked_at: int | None = None,
) -> None:
    upsert_member_snapshot(
        chat_id, user_id, user_name, username,
        joined_at=joined_at,
        telegram_last_seen_at=telegram_last_seen_at,
        telegram_status=telegram_status,
        telegram_status_checked_at=telegram_status_checked_at,
    )
    if message_count:
        with get_conn() as conn:
            conn.execute(
                "UPDATE chat_members SET total_message_count=MAX(total_message_count, ?) WHERE chat_id=? AND user_id=?",
                (max(0, int(message_count)), int(chat_id), int(user_id)),
            )
            conn.commit()


def set_joined(chat_id: int, user_id: int, user_name: str, now_ts: int, username: str | None = None) -> None:
    upsert_member_snapshot(chat_id, user_id, user_name, username, joined_at=int(now_ts), membership_status="member")
    with get_conn() as conn:
        conn.execute("DELETE FROM inactivity_alerts WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        conn.commit()


def set_left(chat_id: int, user_id: int, now_ts: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE chat_members
            SET is_active=0, left_at=?, updated_at=?, membership_status='left', sync_token=NULL
            WHERE chat_id=? AND user_id=?
            """,
            (int(now_ts), int(now_ts), int(chat_id), int(user_id)),
        )
        conn.execute("DELETE FROM inactivity_alerts WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        conn.commit()


def remove_member(chat_id: int, user_id: int) -> None:
    """Compatibility alias: keep history but mark the member as left."""
    set_left(chat_id, user_id, int(time.time()))


def mark_missing_members_left(chat_id: int, sync_token: str, now_ts: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE chat_members
            SET is_active=0, left_at=?, updated_at=?, membership_status='left'
            WHERE chat_id=? AND is_active=1 AND COALESCE(sync_token,'')<>?
            """,
            (int(now_ts), int(now_ts), int(chat_id), sync_token),
        )
        conn.execute(
            "DELETE FROM inactivity_alerts WHERE chat_id=? AND user_id IN (SELECT user_id FROM chat_members WHERE chat_id=? AND is_active=0)",
            (int(chat_id), int(chat_id)),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def list_active_members(chat_id: int, limit: int = 100000) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM chat_members
            WHERE chat_id=? AND is_active=1 AND is_bot=0
            ORDER BY user_id
            LIMIT ?
            """,
            (int(chat_id), int(limit)),
        ).fetchall()


def count_active_members(chat_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM chat_members WHERE chat_id=? AND is_active=1 AND is_bot=0",
            (int(chat_id),),
        ).fetchone()
    return int(row["count"] or 0)


def get_alert_candidates(chat_id: int, inactivity_days: int, min_message_count: int, now_ts: int, limit: int = 500):
    threshold = int(now_ts) - int(inactivity_days) * 86400
    day_from = datetime.fromtimestamp(threshold, tz=timezone.utc).date().isoformat()
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT m.*,
                   COALESCE((SELECT SUM(a.message_count)
                             FROM member_daily_activity a
                             WHERE a.chat_id=m.chat_id AND a.user_id=m.user_id AND a.day>=?), 0) AS period_message_count
            FROM chat_members m
            WHERE m.chat_id=? AND m.is_active=1 AND m.is_bot=0
              AND (
                    COALESCE(m.last_message_at, m.joined_at, m.first_seen_at, 0) <= ?
                 OR (? > 0 AND COALESCE((SELECT SUM(a2.message_count)
                                         FROM member_daily_activity a2
                                         WHERE a2.chat_id=m.chat_id AND a2.user_id=m.user_id AND a2.day>=?), 0) < ?
                     AND COALESCE(m.joined_at, m.first_seen_at, 0) <= ?)
              )
            ORDER BY COALESCE(m.last_message_at, m.joined_at, m.first_seen_at, 0) ASC
            LIMIT ?
            """,
            (
                day_from, int(chat_id), threshold,
                int(min_message_count), day_from, int(min_message_count), threshold, int(limit),
            ),
        ).fetchall()


def get_inactive_members(chat_id: int, inactivity_days: int, now_ts: int, limit: int = 200):
    return get_alert_candidates(chat_id, inactivity_days, 0, now_ts, limit)


def should_alert(chat_id: int, user_id: int, alert_type: str, now_ts: int, repeat_hours: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_alerted_at FROM inactivity_alerts WHERE chat_id=? AND user_id=? AND alert_type=?",
            (int(chat_id), int(user_id), alert_type),
        ).fetchone()
    return row is None or int(now_ts) - int(row["last_alerted_at"] or 0) >= int(repeat_hours) * 3600


def mark_alert(chat_id: int, user_id: int, alert_type: str, now_ts: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO inactivity_alerts(chat_id, user_id, alert_type, last_alerted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id, alert_type)
            DO UPDATE SET last_alerted_at=excluded.last_alerted_at
            """,
            (int(chat_id), int(user_id), alert_type, int(now_ts)),
        )
        conn.commit()


def clear_alerts_for_chat(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM inactivity_alerts WHERE chat_id=?", (int(chat_id),))
        conn.commit()


def list_chat_ids_with_settings() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.chat_id
            FROM chat_settings s
            JOIN chats c ON c.chat_id=s.chat_id
            WHERE c.is_tracked=1 AND COALESCE(c.is_hidden,0)=0 AND c.chat_type IN ('group','supergroup')
            ORDER BY s.chat_id
            """
        ).fetchall()
    return [int(r["chat_id"]) for r in rows]


def get_chat_member_stats(chat_id: int, user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_members WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id))).fetchone()
    return dict(row) if row else None


def find_known_user_by_username(username: str) -> Optional[dict]:
    """Resolve a username from already-synchronised member data.

    Usernames can change, so this is a fast local fallback. Web admin creation
    prefers a live Telethon lookup when an authorised session is available.
    """
    normalized = str(username or "").strip().lstrip("@").lower()
    if not normalized:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT user_id, user_name, username, MAX(updated_at) AS last_updated
            FROM chat_members
            WHERE is_bot=0 AND LOWER(COALESCE(username,''))=?
            GROUP BY user_id, user_name, username
            ORDER BY last_updated DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
    return dict(row) if row else None


# ---------- Admins ----------

def bootstrap_admins(owner_id: int, legacy_admin_ids: Iterable[int] = ()) -> None:
    now_ts = int(time.time())
    ids = {int(owner_id), *(int(x) for x in legacy_admin_ids)}
    with get_conn() as conn:
        for uid in ids:
            conn.execute(
                """
                INSERT INTO system_admins(user_id, is_owner, notifications_enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    is_owner=MAX(system_admins.is_owner, excluded.is_owner),
                    updated_at=excluded.updated_at
                """,
                (uid, 1 if uid == int(owner_id) else 0, now_ts, now_ts),
            )
        conn.commit()


def is_system_admin(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM system_admins WHERE user_id=?", (int(user_id),)).fetchone()
    return bool(row)


def list_system_admins() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM system_admins ORDER BY is_owner DESC, created_at, user_id").fetchall()


def list_admin_ids(*, notifications_only: bool = False) -> list[int]:
    sql = "SELECT user_id FROM system_admins"
    if notifications_only:
        sql += " WHERE notifications_enabled=1"
    sql += " ORDER BY is_owner DESC, user_id"
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [int(r["user_id"]) for r in rows]


def add_system_admin(user_id: int, *, display_name: str | None = None, username: str | None = None) -> None:
    now_ts = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO system_admins(user_id, display_name, username, is_owner, notifications_enabled, created_at, updated_at)
            VALUES (?, ?, ?, 0, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name=COALESCE(excluded.display_name, system_admins.display_name),
                username=COALESCE(excluded.username, system_admins.username),
                updated_at=excluded.updated_at
            """,
            (int(user_id), display_name, username, now_ts, now_ts),
        )
        conn.commit()


def remove_system_admin(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT is_owner FROM system_admins WHERE user_id=?", (int(user_id),)).fetchone()
        if not row or int(row["is_owner"]):
            return False
        conn.execute("DELETE FROM system_admins WHERE user_id=?", (int(user_id),))
        conn.commit()
    return True


def set_admin_notifications(user_id: int, enabled: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE system_admins SET notifications_enabled=?, updated_at=? WHERE user_id=?",
            (1 if enabled else 0, int(time.time()), int(user_id)),
        )
        conn.commit()
        return bool(cur.rowcount)


def update_admin_identity(user_id: int, display_name: str | None, username: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE system_admins SET display_name=?, username=?, updated_at=? WHERE user_id=?",
            (display_name, username, int(time.time()), int(user_id)),
        )
        conn.commit()


# ---------- Sync queue ----------

def request_sync(chat_id: int, requested_by: str, request_type: str = "full") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sync_requests(chat_id, request_type, requested_by, requested_at, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (int(chat_id), request_type, requested_by[:200], int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)


def requeue_running_sync_requests() -> int:
    """Return interrupted manual sync requests to the queue after a worker restart."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sync_requests SET status='pending', error=NULL WHERE status='running'"
        )
        conn.commit()
        return int(cur.rowcount)


def claim_sync_requests(limit: int = 10) -> list[sqlite3.Row]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_requests WHERE status='pending' ORDER BY requested_at LIMIT ?",
            (int(limit),),
        ).fetchall()
        if rows:
            conn.executemany(
                "UPDATE sync_requests SET status='running' WHERE id=?",
                [(int(r["id"]),) for r in rows],
            )
            conn.commit()
        return rows


def finish_sync_request(request_id: int, *, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_requests SET status=?, completed_at=?, error=? WHERE id=?",
            ("error" if error else "done", int(time.time()), error[:1000] if error else None, int(request_id)),
        )
        conn.commit()


# ---------- Web/dashboard queries ----------

def list_members_for_web(
    chat_id: int | None = None,
    *,
    search: str = "",
    active: str = "active",
    limit: int = 200,
    offset: int = 0,
) -> list[sqlite3.Row]:
    now = datetime.now(timezone.utc).date()
    d7 = (now - timedelta(days=6)).isoformat()
    d30 = (now - timedelta(days=29)).isoformat()
    clauses = ["m.is_bot=0"]
    args: list[object] = [d7, d30]
    if chat_id is not None:
        clauses.append("m.chat_id=?")
        args.append(int(chat_id))
    if active == "active":
        clauses.append("m.is_active=1")
    elif active == "silent":
        clauses.append("m.is_active=1")
        clauses.append("m.last_message_at IS NULL")
        clauses.append("COALESCE(m.total_message_count,0)=0")
    elif active == "left":
        clauses.append("m.is_active=0")
    if search.strip():
        q = f"%{search.strip().lower()}%"
        clauses.append("(LOWER(COALESCE(m.user_name,'')) LIKE ? OR LOWER(COALESCE(m.username,'')) LIKE ? OR CAST(m.user_id AS TEXT) LIKE ?)")
        args.extend([q, q, q])
    args.extend([max(1, min(1000, int(limit))), max(0, int(offset))])
    where = " AND ".join(clauses)
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT m.*, c.title AS chat_title,
                   COALESCE((SELECT SUM(a.message_count) FROM member_daily_activity a
                             WHERE a.chat_id=m.chat_id AND a.user_id=m.user_id AND a.day>=?), 0) AS messages_7d,
                   COALESCE((SELECT SUM(a.message_count) FROM member_daily_activity a
                             WHERE a.chat_id=m.chat_id AND a.user_id=m.user_id AND a.day>=?), 0) AS messages_30d
            FROM chat_members m
            JOIN chats c ON c.chat_id=m.chat_id
            WHERE {where}
            ORDER BY m.is_active DESC, COALESCE(m.last_message_at, m.joined_at, m.first_seen_at, 0) DESC
            LIMIT ? OFFSET ?
            """,
            args,
        ).fetchall()


def list_inactive_members_for_web(chat_id: int, limit: int = 300) -> list[sqlite3.Row]:
    settings = get_chat_settings(chat_id)
    candidates = get_alert_candidates(
        chat_id, settings["inactivity_days"], settings["min_message_count"], int(time.time()), min(500, max(1, int(limit)))
    )
    ids = [int(row["user_id"]) for row in candidates]
    if not ids:
        return []
    now = datetime.now(timezone.utc).date()
    d7 = (now - timedelta(days=6)).isoformat()
    d30 = (now - timedelta(days=29)).isoformat()
    placeholders = ",".join("?" for _ in ids)
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT m.*, c.title AS chat_title,
                   COALESCE((SELECT SUM(a.message_count) FROM member_daily_activity a
                             WHERE a.chat_id=m.chat_id AND a.user_id=m.user_id AND a.day>=?), 0) AS messages_7d,
                   COALESCE((SELECT SUM(a.message_count) FROM member_daily_activity a
                             WHERE a.chat_id=m.chat_id AND a.user_id=m.user_id AND a.day>=?), 0) AS messages_30d
            FROM chat_members m
            JOIN chats c ON c.chat_id=m.chat_id
            WHERE m.chat_id=? AND m.user_id IN ({placeholders}) AND m.is_active=1 AND m.is_bot=0
            ORDER BY COALESCE(m.last_message_at, m.joined_at, m.first_seen_at, 0) ASC
            """,
            [d7, d30, int(chat_id), *ids],
        ).fetchall()


def dashboard_stats(chat_id: int | None = None) -> dict:
    now_ts = int(time.time())
    last_24h = now_ts - 86400
    last_7d = now_ts - 7 * 86400
    clauses = ["m.is_bot=0"]
    args: list[object] = []
    if chat_id is not None:
        clauses.append("m.chat_id=?")
        args.append(int(chat_id))
    where = " AND ".join(clauses)
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN m.is_active=1 THEN 1 ELSE 0 END) AS members,
              SUM(CASE WHEN m.is_active=1 AND m.last_message_at>=? THEN 1 ELSE 0 END) AS active_24h,
              SUM(CASE WHEN m.is_active=1 AND m.last_message_at>=? THEN 1 ELSE 0 END) AS active_7d,
              SUM(CASE WHEN m.is_active=1 AND COALESCE(m.last_message_at, m.joined_at, m.first_seen_at, 0)<? THEN 1 ELSE 0 END) AS inactive_7d,
              SUM(CASE WHEN m.is_active=1 AND m.last_message_at IS NULL AND COALESCE(m.total_message_count,0)=0 THEN 1 ELSE 0 END) AS never_wrote,
              SUM(CASE WHEN m.joined_at IS NOT NULL AND m.joined_at>=? THEN 1 ELSE 0 END) AS joined_7d,
              SUM(CASE WHEN m.left_at>=? THEN 1 ELSE 0 END) AS left_7d
            FROM chat_members m
            JOIN chats c ON c.chat_id=m.chat_id AND c.is_tracked=1 AND COALESCE(c.is_hidden,0)=0 AND c.chat_type IN ('group','supergroup')
            WHERE {where}
            """,
            [last_24h, last_7d, last_7d, last_7d, last_7d, *args],
        ).fetchone()
        tracked = conn.execute(
            "SELECT COUNT(*) AS n FROM chats WHERE is_tracked=1 AND COALESCE(is_hidden,0)=0 AND chat_type IN ('group','supergroup')"
        ).fetchone()["n"]
    return {
        "members": int(row["members"] or 0),
        "active_24h": int(row["active_24h"] or 0),
        "active_7d": int(row["active_7d"] or 0),
        "inactive_7d": int(row["inactive_7d"] or 0),
        "never_wrote": int(row["never_wrote"] or 0),
        "joined_7d": int(row["joined_7d"] or 0),
        "left_7d": int(row["left_7d"] or 0),
        "tracked_chats": int(tracked or 0),
    }


def activity_series(chat_id: int | None = None, days: int = 30) -> list[dict]:
    days = max(1, min(365, int(days)))
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    where = "day>=?"
    args: list[object] = [start.isoformat()]
    if chat_id is not None:
        where += " AND chat_id=?"
        args.append(int(chat_id))
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT a.day, SUM(a.message_count) AS messages, COUNT(DISTINCT a.user_id) AS active_users
            FROM member_daily_activity a
            JOIN chats c ON c.chat_id=a.chat_id AND c.is_tracked=1 AND COALESCE(c.is_hidden,0)=0 AND c.chat_type IN ('group','supergroup')
            WHERE {where.replace('day', 'a.day').replace('chat_id', 'a.chat_id')}
            GROUP BY a.day ORDER BY a.day
            """,
            args,
        ).fetchall()
    indexed = {r["day"]: {"messages": int(r["messages"] or 0), "active_users": int(r["active_users"] or 0)} for r in rows}
    result = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        values = indexed.get(day, {"messages": 0, "active_users": 0})
        result.append({"day": day, **values})
    return result


def cleanup_seen_messages(retention_days: int = 45) -> int:
    threshold = int(time.time()) - max(7, int(retention_days)) * 86400
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM seen_messages WHERE sent_at<?", (threshold,))
        conn.commit()
        return int(cur.rowcount or 0)


def audit(actor: str, action: str, target: str | None = None, details: dict | str | None = None) -> None:
    if isinstance(details, dict):
        details = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log(actor, action, target, details, created_at) VALUES (?, ?, ?, ?, ?)",
            (actor[:200], action[:200], target[:500] if target else None, details[:4000] if details else None, int(time.time())),
        )
        conn.commit()
