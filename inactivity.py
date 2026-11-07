from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime,timezone,timedelta
from typing import Callable, Iterable

from db import (
    get_conn,
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


async def update_digest(bot,chat_id,recipient_id,text,create=True):
    from aiogram.exceptions import TelegramBadRequest
    with get_conn() as c:row=c.execute('SELECT message_id FROM inactivity_cards WHERE chat_id=? AND user_id=?',(chat_id,recipient_id)).fetchone()
    if row:
        try:
            await bot.edit_message_text(text,chat_id=recipient_id,message_id=row['message_id'],parse_mode='HTML',disable_web_page_preview=True)
            return True
        except TelegramBadRequest as exc:
            error=exc.message.lower()
            if 'message is not modified' in error:return True
            if 'not found' not in error and "can't be edited" not in error:raise
    if not create:return False
    message=await bot.send_message(recipient_id,text,parse_mode='HTML',disable_web_page_preview=True)
    with get_conn() as c:c.execute('INSERT INTO inactivity_cards VALUES (?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET message_id=excluded.message_id',(chat_id,recipient_id,message.message_id))
    return True


async def check_chat_now(bot,chat_id,admin_user_ids,botlog,force=False):
    import tenancy
    from bot_permissions import has_admin_rights,warn
    if tenancy.tier(chat_id)<2:return 0
    now_ts=int(time.time())
    info=get_chat_info(chat_id) or {}
    if info.get('chat_type') not in {'group','supergroup'} or not int(info.get('is_tracked') or 0):return 0
    if not await has_admin_rights(bot,chat_id):
        await warn(bot,chat_id);return 0
    settings=get_chat_settings(chat_id)
    if not settings['enabled'] and not force:return 0
    if not force and now_ts<int(settings['last_check_at'])+int(settings['check_interval_minutes'])*60:return 0
    allowed=set(await tenancy.notification_recipients(bot,chat_id))
    recipients=sorted(allowed.intersection(admin_user_ids) if admin_user_ids is not None else allowed)
    rows=get_alert_candidates(chat_id,settings['inactivity_days'],settings['min_message_count'],now_ts)
    threshold=now_ts-int(settings['inactivity_days'])*86400
    visible=[];due=[]
    for row in rows:
        uid=int(row['user_id']);last=group_activity_ts(row)
        types=[]
        if last and last<=threshold:types.append('inactivity')
        if int(settings['min_message_count'])>0 and int(row['period_message_count'] or 0)<int(settings['min_message_count']) and int(row['joined_at'] or row['first_seen_at'] or 0)<=threshold:types.append('message_count')
        if not types:continue
        visible.append(row)
        due_types=[kind for kind in types if should_alert(chat_id,uid,kind,now_ts,settings['repeat_alert_hours'])]
        if due_types:due.append((uid,due_types))
    name=html.escape((info.get('title') or str(chat_id))[:100])
    when=datetime.fromtimestamp(now_ts,timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')
    text=f'🔔 <b>Сводка неактивности · {name}</b>\nОбновлено: {when}\n'
    text+=f'Период: {settings["inactivity_days"]} дн. · минимум сообщений: {settings["min_message_count"]}\n'
    text+=f'Участников по этим условиям: <b>{len(visible)}</b>\n'
    for n,row in enumerate(visible[:15],1):
        uid=int(row['user_id']);label=html.escape((row['user_name'] or str(uid))[:40])
        days=max(0,(now_ts-group_activity_ts(row))//86400)
        text+=f'\n{n}. <a href="tg://user?id={uid}">{label}</a> — {days} дн., сообщений за период: {int(row["period_message_count"] or 0)}'
    if len(visible)>15:text+=f'\n… ещё {len(visible)-15}. Полный список — /admin → чат → Участники → Неактивные.'
    if not visible:text+='\nНеактивных по заданным условиям сейчас нет.'
    text+='\n\nЭто одно обновляемое сообщение для этого чата. Подробности и настройки — /admin.'
    delivered=False
    for uid in recipients:
        try:delivered=await update_digest(bot,chat_id,uid,text,create=bool(visible)) or delivered
        except Exception as exc:await botlog(f'Cannot update inactivity digest chat={chat_id} recipient={uid}: {type(exc).__name__}')
    if delivered:
        for uid,types in due:
            for kind in types:mark_alert(chat_id,uid,kind,now_ts)
    mark_chat_checked(chat_id,now_ts)
    return len(due) if delivered else 0


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
