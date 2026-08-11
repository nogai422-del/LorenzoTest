from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime
from typing import Callable, Iterable

from db import (
    get_alert_candidates,
    get_chat_info,
    get_chat_settings,
    list_admin_ids,
    list_chat_ids_with_settings,
    mark_alert,
    mark_chat_checked,
    should_alert,
)


TELEGRAM_STATUS_LABELS = {
    "online": "сейчас онлайн",
    "offline": "точное время",
    "recently": "был(а) недавно",
    "last_week": "был(а) на этой неделе",
    "last_month": "был(а) в этом месяце",
    "long_ago": "был(а) давно",
    "hidden": "скрыто",
    "unknown": "неизвестно",
}


def format_dt(ts: int | None) -> str:
    if not ts:
        return "сообщений ещё не было"
    return datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y %H:%M")


def group_activity_ts(row) -> int:
    """Group activity is based on messages, with join/first-seen as a grace baseline."""
    return int(row["last_message_at"] or row["joined_at"] or row["first_seen_at"] or 0)


def format_telegram_status(row) -> str:
    status = str(row["telegram_status"] or "unknown")
    label = TELEGRAM_STATUS_LABELS.get(status, status)
    ts = int(row["telegram_last_seen_at"] or 0)
    if ts:
        return f"{label}: {format_dt(ts)}"
    return label


def build_message_url(chat_id: int, chat_username: str | None, message_id: int | None) -> str | None:
    if not message_id:
        return None
    if chat_username:
        return f"https://t.me/{chat_username}/{int(message_id)}"
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{int(message_id)}"
    return None


async def check_chat_now(
    bot,
    chat_id: int,
    admin_user_ids: Iterable[int] | None,
    botlog: Callable,
    force: bool = False,
) -> int:
    now_ts = int(time.time())
    info = get_chat_info(chat_id) or {}
    if info.get("chat_type") not in {"group", "supergroup"} or not int(info.get("is_tracked") or 0):
        return 0

    settings = get_chat_settings(chat_id)
    if not settings["enabled"] and not force:
        return 0

    if not force:
        due_at = int(settings["last_check_at"]) + int(settings["check_interval_minutes"]) * 60
        if now_ts < due_at:
            return 0

    chat_title = info.get("title") or (f"@{info['username']}" if info.get("username") else str(chat_id))
    chat_username = info.get("username")

    rows = get_alert_candidates(
        chat_id=chat_id,
        inactivity_days=settings["inactivity_days"],
        min_message_count=settings["min_message_count"],
        now_ts=now_ts,
    )
    sent = 0
    threshold = now_ts - int(settings["inactivity_days"]) * 86400
    recipients = list(admin_user_ids) if admin_user_ids is not None else list_admin_ids(notifications_only=True)

    for row in rows:
        user_id = int(row["user_id"])
        last_group_activity = group_activity_ts(row)
        inactive = bool(last_group_activity and last_group_activity <= threshold)
        period_messages = int(row["period_message_count"] or 0)
        low_messages = bool(
            int(settings["min_message_count"]) > 0
            and period_messages < int(settings["min_message_count"])
            and int(row["joined_at"] or row["first_seen_at"] or 0) <= threshold
        )

        alert_types: list[str] = []
        if inactive:
            alert_types.append("inactivity")
        if low_messages:
            alert_types.append("message_count")
        if not alert_types:
            continue

        due_types = [
            alert_type
            for alert_type in alert_types
            if should_alert(chat_id, user_id, alert_type, now_ts, settings["repeat_alert_hours"])
        ]
        if not due_types:
            continue

        name = html.escape(row["user_name"] or str(user_id))
        user_link = f'<a href="tg://user?id={user_id}">{name}</a>'
        reasons: list[str] = []
        if "inactivity" in due_types:
            days = max(0, (now_ts - last_group_activity) // 86400)
            if row["last_message_at"]:
                reasons.append(f"не писал(а) в группе: <b>{days} дн.</b>")
            else:
                reasons.append(f"нет сообщений после вступления: <b>{days} дн.</b>")
        if "message_count" in due_types:
            reasons.append(
                f"сообщений за {int(settings['inactivity_days'])} дн.: "
                f"<b>{period_messages}</b> из <b>{int(settings['min_message_count'])}</b>"
            )

        msg_url = build_message_url(chat_id, chat_username, row["last_message_id"])
        last_line = format_dt(row["last_message_at"])
        if msg_url:
            last_line = f'<a href="{msg_url}">{last_line}</a>'

        text = (
            "⚠️ <b>Проверка активности</b>\n"
            f"Чат: <b>{html.escape(chat_title)}</b>\n"
            f"Участник: {user_link}\n"
            f"Причина: {'; '.join(reasons)}\n"
            f"Последнее сообщение в группе: {last_line}\n"
            f"Telegram presence: {html.escape(format_telegram_status(row))}\n\n"
            "<i>Статус неактивности считается по активности в этой группе; Telegram last seen показан отдельно.</i>"
        )

        delivered = False
        for admin_id in recipients:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML", disable_web_page_preview=True)
                delivered = True
            except Exception as exc:
                await botlog(f"Cannot send inactivity alert to {admin_id}: {exc}")

        if delivered:
            for alert_type in due_types:
                mark_alert(chat_id, user_id, alert_type, now_ts)
            sent += 1

    mark_chat_checked(chat_id, now_ts)
    return sent


async def inactivity_watcher(bot, botlog, sleep_seconds: int = 30) -> None:
    """Small scheduler; each group uses its own check interval and current admin recipients."""
    while True:
        try:
            for chat_id in list_chat_ids_with_settings():
                try:
                    await check_chat_now(bot, chat_id, None, botlog)
                except Exception as exc:
                    await botlog(f"inactivity check chat={chat_id} error: {exc}")
        except Exception as exc:
            await botlog(f"inactivity_watcher error: {exc}")
        await asyncio.sleep(max(10, int(sleep_seconds)))


async def send_test_inactivity_alert(bot, recipient_id: int, chat_id: int) -> None:
    info = get_chat_info(chat_id) or {}
    chat_title = info.get("title") or (f"@{info['username']}" if info.get("username") else str(chat_id))
    text = (
        "🧪 <b>Тестовое оповещение о неактиве</b>\n"
        f"Чат: <b>{html.escape(chat_title)}</b>\n"
        "Участник: <a href=\"tg://user?id=1\">Тестовый участник</a>\n"
        "Причина: не писал(а) в группе <b>7 дн.</b>\n"
        "Последнее сообщение: сообщений ещё не было\n"
        "Telegram presence: был(а) недавно\n\n"
        "✅ Канал доставки оповещений работает. История алертов не изменена."
    )
    await bot.send_message(recipient_id, text, parse_mode="HTML", disable_web_page_preview=True)
