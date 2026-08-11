from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, time as dtime
from logging.handlers import RotatingFileHandler

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

from db import (
    audit,
    bootstrap_admins,
    init_db,
    dashboard_stats,
    ensure_chat_settings,
    get_alert_candidates,
    get_chat_info,
    get_chat_member_stats,
    get_chat_settings,
    is_system_admin,
    list_admin_ids,
    list_known_chats,
    list_members_for_web,
    list_system_admins,
    record_chat,
    request_sync,
    set_admin_notifications,
    set_joined,
    set_left,
    upsert_message_activity,
    update_admin_identity,
)
from inactivity import inactivity_watcher, send_test_inactivity_alert
from settings_store import load_settings, save_settings
from telethon_config import telethon_status
from runtime_paths import LOG_PATH, PROJECT_DIR, SETTINGS_PATH, web_public_url

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("API_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not found. Add BOT_TOKEN=... to .env")
OWNER_ID_ENV = (os.getenv("OWNER_ID") or "").strip()
WEB_PUBLIC_URL = web_public_url()
LOG_FILE = str(LOG_PATH)
LEGACY_ADMINS_FILE = str(PROJECT_DIR / "admins.json")

logger = logging.getLogger("lorenzo_bot")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    try:
        os.chmod(LOG_FILE, 0o600)
    except OSError:
        pass
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


async def botlog(text: str) -> None:
    logger.info(text)


def _legacy_admin_ids() -> list[int]:
    if not os.path.exists(LEGACY_ADMINS_FILE):
        return []
    try:
        data = json.loads(open(LEGACY_ADMINS_FILE, "r", encoding="utf-8").read())
        return [int(x) for x in data]
    except Exception:
        return []


init_db()
_existing_admins = list_system_admins()
_first_admin_bootstrap = not bool(_existing_admins)
if OWNER_ID_ENV:
    OWNER_ID = int(OWNER_ID_ENV)
else:
    _existing_owners = [int(row["user_id"]) for row in _existing_admins if int(row["is_owner"])]
    if not _existing_owners:
        raise RuntimeError("OWNER_ID not found. Add OWNER_ID=... to .env for the first start")
    OWNER_ID = _existing_owners[0]
bootstrap_admins(OWNER_ID, _legacy_admin_ids() if _first_admin_bootstrap else [])
if _first_admin_bootstrap:
    try:
        legacy_settings_path = str(SETTINGS_PATH if SETTINGS_PATH.exists() else PROJECT_DIR / "settings.json")
        legacy_settings = json.loads(open(legacy_settings_path, "r", encoding="utf-8").read()) if os.path.exists(legacy_settings_path) else {}
        legacy_notify = legacy_settings.get("notify_admins")
        if isinstance(legacy_notify, list):
            notify_ids = {int(x) for x in legacy_notify}
            for admin_id in list_admin_ids():
                set_admin_notifications(admin_id, admin_id in notify_ids)
    except Exception:
        pass

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()


class TrackActivityMiddleware(BaseMiddleware):
    """Track normal user messages in groups only; Telethon uses the same dedupe table."""

    async def __call__(self, handler, event, data):
        try:
            if (
                isinstance(event, Message)
                and event.chat
                and event.chat.type in {"group", "supergroup"}
            ):
                chat_id = int(event.chat.id)
                now_ts = int(time.time())
                record_chat(
                    chat_id,
                    event.chat.title,
                    getattr(event.chat, "username", None),
                    event.chat.type,
                    now_ts,
                    tracked=True,
                    discovered_by="bot",
                )
                chat_info = get_chat_info(chat_id) or {}
                if int(chat_info.get("is_hidden") or 0):
                    return await handler(event, data)
                ensure_chat_settings(chat_id)
                is_service = bool(event.new_chat_members or event.left_chat_member)
                if event.from_user and not event.from_user.is_bot and not is_service:
                    upsert_message_activity(
                        chat_id=chat_id,
                        user_id=int(event.from_user.id),
                        user_name=event.from_user.full_name or str(event.from_user.id),
                        username=event.from_user.username,
                        now_ts=now_ts,
                        message_id=event.message_id,
                        is_bot=False,
                    )
        except Exception as exc:
            await botlog(f"activity middleware error: {exc}")
        return await handler(event, data)


dp.message.outer_middleware(TrackActivityMiddleware())


class Onboarding(StatesGroup):
    waiting_for_age = State()
    waiting_for_consent = State()


def is_admin(user_id: int) -> bool:
    # OWNER_ID from the current environment is always authoritative.
    # The database remains the source for all other assigned admins.
    return int(user_id) == int(OWNER_ID) or is_system_admin(int(user_id))


def get_notify_admins() -> list[int]:
    return list_admin_ids(notifications_only=True)


def current_settings() -> dict:
    return load_settings(OWNER_ID)


def parse_hhmm(value: str) -> dtime:
    match = re.fullmatch(r"(\d{2}):(\d{2})", (value or "").strip())
    if not match:
        raise ValueError("Expected HH:MM")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Invalid time")
    return dtime(hour, minute)


def is_time_active_now() -> bool:
    settings = current_settings()
    if not settings.get("is_active", True):
        return False
    try:
        start = parse_hhmm(settings["work_start"])
        end = parse_hhmm(settings["work_end"])
    except Exception:
        return False
    now = datetime.now().time()
    return start <= now <= end if start <= end else (now >= start or now <= end)


def level_value() -> int:
    return int(current_settings().get("level", 1))


async def typed_delay() -> None:
    delay = int(current_settings().get("reply_delay", 0))
    if delay > 0:
        await asyncio.sleep(delay)


def user_profile_link(user_id: int, full_name: str | None) -> str:
    label = html.escape(full_name or str(user_id))
    return f'<a href="tg://user?id={user_id}">{label}</a>'


async def build_message_link_safe(message: Message) -> str:
    try:
        if getattr(message.chat, "username", None):
            return f"https://t.me/{message.chat.username}/{message.message_id}"
        raw = str(message.chat.id)
        if raw.startswith("-100"):
            return f"https://t.me/c/{raw[4:]}/{message.message_id}"
    except Exception:
        pass
    return "Нет ссылки"


def is_refusal(text: str | None) -> bool:
    if not text:
        return False
    refusals = ["не хочу", "не буду", "отказываюсь", "нет", "не согласен", "против", "отказ"]
    lowered = text.lower()
    return any(part in lowered for part in refusals)


async def notify_admins(text: str) -> None:
    for admin_id in get_notify_admins():
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            await botlog(f"notify admin={admin_id} failed: {exc}")


def build_nudge_text(member: dict) -> str:
    settings = current_settings()
    template = str((settings.get("texts") or {}).get("nudge_text") or "{mention}, что молчишь? 🙂")
    user_id = int(member["user_id"])
    name = str(member.get("user_name") or user_id)
    username = str(member.get("username") or "")
    safe_template = html.escape(template)
    replacements = {
        "{mention}": f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>',
        "{name}": html.escape(name),
        "{username}": html.escape("@" + username if username else ""),
        "{user_id}": str(user_id),
    }
    for key, value in replacements.items():
        safe_template = safe_template.replace(key, value)
    return safe_template


async def send_member_nudge(chat_id: int, user_id: int, actor: str) -> Message:
    info = get_chat_info(int(chat_id)) or {}
    if (
        info.get("chat_type") not in {"group", "supergroup"}
        or not int(info.get("is_tracked") or 0)
        or int(info.get("is_hidden") or 0)
    ):
        raise ValueError("Чат не отслеживается или скрыт")
    member = get_chat_member_stats(int(chat_id), int(user_id))
    if not member or not int(member.get("is_active") or 0) or int(member.get("is_bot") or 0):
        raise ValueError("Участник больше не состоит в выбранной группе")
    text = build_nudge_text(member)
    sent = await bot.send_message(
        int(chat_id),
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    audit(
        actor,
        "member_nudge_sent",
        f"{int(chat_id)}:{int(user_id)}",
        {"message_id": int(sent.message_id), "template": str((current_settings().get("texts") or {}).get("nudge_text") or "")[:500]},
    )
    return sent


async def send_test_alert(recipient_id: int, chat_id: int, actor: str) -> None:
    await send_test_inactivity_alert(bot, int(recipient_id), int(chat_id))
    audit(actor, "test_notification_sent", f"{int(chat_id)}:{int(recipient_id)}")


async def do_ban(message: Message, reason: str) -> None:
    await botlog(f"BAN user_id={message.from_user.id} reason={reason}")
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=message.from_user.id)
    except Exception as exc:
        await botlog(f"ban failed: {exc}")
        return
    await notify_admins(
        "🚫 <b>Бан</b>\n"
        f"Юзер: {user_profile_link(message.from_user.id, message.from_user.full_name)}\n"
        f"Причина: {html.escape(reason)}\n"
        f"Сообщение: {await build_message_link_safe(message)}"
    )


async def maybe_ban_on_suspicious_links(message: Message) -> bool:
    text = ((message.text or "") + " " + (message.caption or "")).lower()
    has_username = bool(re.search(r"(^|\s)@[\w_]{5,32}($|\s)", text))
    if "http://" in text or "https://" in text or "t.me/" in text or has_username:
        await do_ban(message, "Подозрительные ссылки/@")
        return True
    return False


async def handle_user_failure(message: Message, state: FSMContext, reason: str) -> None:
    await notify_admins(
        "⚠️ <b>Прервано</b>\n"
        f"Причина: {html.escape(reason)}\n"
        f"Юзер: {user_profile_link(message.from_user.id, message.from_user.full_name)}\n"
        f"Сообщение: {await build_message_link_safe(message)}"
    )
    await state.clear()


@router.message(F.new_chat_members)
async def welcome_new_member(message: Message) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        return
    now_ts = int(time.time())
    chat_id = int(message.chat.id)
    record_chat(
        chat_id,
        message.chat.title,
        getattr(message.chat, "username", None),
        message.chat.type,
        now_ts,
        tracked=True,
        discovered_by="bot",
    )
    chat_info = get_chat_info(chat_id) or {}
    if int(chat_info.get("is_hidden") or 0):
        return
    ensure_chat_settings(chat_id)

    settings = current_settings()
    for new_member in message.new_chat_members:
        if new_member.id == bot.id:
            continue
        set_joined(chat_id, int(new_member.id), new_member.full_name, now_ts, username=new_member.username)
        if not is_time_active_now() or level_value() not in (1, 2, 3):
            continue
        await typed_delay()
        welcome_text = settings["texts"]["welcome_text"].format(
            name=f'<a href="tg://user?id={new_member.id}">{html.escape(new_member.first_name)}</a>'
        )
        user_state = FSMContext(
            storage=dp.storage,
            key=StorageKey(bot_id=bot.id, chat_id=message.chat.id, user_id=new_member.id),
        )
        await user_state.set_state(Onboarding.waiting_for_age)
        await message.reply(welcome_text)


@router.chat_member()
async def chat_member_updated_handler(event: ChatMemberUpdated) -> None:
    if event.chat.type not in {"group", "supergroup"}:
        return
    try:
        status_value = str(event.new_chat_member.status)
        chat_id = int(event.chat.id)
        chat_info = get_chat_info(chat_id) or {}
        if int(chat_info.get("is_hidden") or 0):
            return
        user = event.new_chat_member.user
        if status_value in {"left", "kicked", "banned"}:
            set_left(chat_id, int(user.id), int(time.time()))
        elif status_value in {"member", "administrator", "creator", "restricted"}:
            set_joined(chat_id, int(user.id), user.full_name, int(time.time()), username=user.username)
    except Exception as exc:
        await botlog(f"chat_member update error: {exc}")


@router.message(F.left_chat_member)
async def left_chat_member_handler(message: Message) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        return
    chat_info = get_chat_info(int(message.chat.id)) or {}
    if int(chat_info.get("is_hidden") or 0):
        return
    set_left(int(message.chat.id), int(message.left_chat_member.id), int(time.time()))
    try:
        await message.delete()
    except Exception:
        pass


@router.message(Onboarding.waiting_for_age)
async def process_age(message: Message, state: FSMContext) -> None:
    if not is_time_active_now():
        await state.clear()
        return
    if await maybe_ban_on_suspicious_links(message):
        await state.clear()
        return
    if is_refusal(message.text):
        await handle_user_failure(message, state, "Отказ назвать возраст")
        return
    match = re.search(r"\b(\d{1,3})\b", (message.text or "").strip())
    if not match:
        return
    age = int(match.group(1))
    if age < 18 or age >= 70:
        await handle_user_failure(message, state, f"Возраст вне диапазона: {age}")
        return
    if level_value() == 1:
        await state.clear()
        return
    await typed_delay()
    await message.reply(current_settings()["texts"]["consent_text"].format(age=age))
    await state.set_state(Onboarding.waiting_for_consent)


@router.message(Onboarding.waiting_for_consent)
async def process_consent(message: Message, state: FSMContext) -> None:
    if not is_time_active_now():
        await state.clear()
        return
    if await maybe_ban_on_suspicious_links(message):
        await state.clear()
        return
    text = (message.text or "").lower().strip()
    positive_words = {"да", "давай", "ок", "окей", "хочу", "конечно", "готов", "+"}
    if not any(word in text for word in positive_words):
        await handle_user_failure(message, state, "Отказ от анкеты")
        return
    await notify_admins(
        "✅ <b>Согласие на анкету</b>\n"
        f"Юзер: {user_profile_link(message.from_user.id, message.from_user.full_name)}\n"
        f"Сообщение: {await build_message_link_safe(message)}"
    )
    await state.clear()


# ------------------------- Admin UI -------------------------

def admin_main_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"), InlineKeyboardButton(text="👥 Участники", callback_data="adm:members")],
        [InlineKeyboardButton(text="⚠️ Неактивные", callback_data="adm:inactive"), InlineKeyboardButton(text="🤐 Не писали", callback_data="adm:silent")],
        [InlineKeyboardButton(text="🔄 Синхронизация", callback_data="adm:sync"), InlineKeyboardButton(text="🔔 Оповещения", callback_data="adm:notifs")],
        [InlineKeyboardButton(text="⚙️ Бот", callback_data="adm:bot")],
    ]
    if WEB_PUBLIC_URL:
        base_url = WEB_PUBLIC_URL.rstrip("/")
        if int(user_id) == int(OWNER_ID):
            rows.append([
                InlineKeyboardButton(text="🔐 Telethon", url=base_url + "/telethon"),
                InlineKeyboardButton(text="🌐 Web-панель", url=base_url),
            ])
        else:
            rows.append([InlineKeyboardButton(text="🌐 Web-панель", url=base_url)])
    elif int(user_id) == int(OWNER_ID):
        rows.append([InlineKeyboardButton(text="🔐 Telethon", callback_data="adm:telethon")])
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="adm:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm:home")]])


def chat_picker(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for chat in list_known_chats(include_untracked=False):
        title = chat["title"] or (f"@{chat['username']}" if chat["username"] else str(chat["chat_id"]))
        if len(title) > 38:
            title = title[:35] + "..."
        rows.append([InlineKeyboardButton(text=title, callback_data=f"{prefix}:{int(chat['chat_id'])}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_home_text(user_id: int) -> str:
    stats = dashboard_stats()
    settings = current_settings()
    lines = [
        "🛠️ <b>Админ-панель</b>",
        "",
        f"Отслеживаемых чатов: <b>{stats['tracked_chats']}</b>",
        f"Участников в группах: <b>{stats['members']}</b>",
        f"Активны за 24 часа: <b>{stats['active_24h']}</b>",
        f"Активны за 7 дней: <b>{stats['active_7d']}</b>",
        f"Ни разу не писали: <b>{stats['never_wrote']}</b>",
        f"Онбординг: <b>{'включён' if settings['is_active'] else 'выключен'}</b>",
    ]
    # Telethon details are infrastructure-level data and are visible only to owner.
    if int(user_id) == int(OWNER_ID):
        tstatus = telethon_status()
        telethon_icon = "✅" if tstatus["authorized"] else "⚠️"
        lines.append(f"Telethon: {telethon_icon} <b>{html.escape(tstatus['status_label'])}</b>")
    lines.extend(["", "Полное управление и графики доступны в web-панели."])
    return "\n".join(lines)


async def require_admin_call(call: CallbackQuery) -> bool:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return False
    return True




@router.message(Command("start"))
async def start_command(message: Message) -> None:
    if is_admin(message.from_user.id):
        update_admin_identity(message.from_user.id, message.from_user.full_name, message.from_user.username)
        await message.reply(await admin_home_text(message.from_user.id), reply_markup=admin_main_kb(message.from_user.id))
        return
    await message.reply(
        "Бот работает. Административные функции доступны только назначенным администраторам.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("myid"))
async def myid_command(message: Message) -> None:
    await message.reply(f"Ваш Telegram ID: <code>{int(message.from_user.id)}</code>")


@router.message(Command("admin"))
@router.message(Command("activity"))
async def admin_open(message: Message) -> None:
    if not is_admin(message.from_user.id):
        # Deliberately silent: regular users should not see an admin-panel response.
        return
    update_admin_identity(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await message.reply(await admin_home_text(message.from_user.id), reply_markup=admin_main_kb(message.from_user.id))


@router.message(Command("members"))
async def members_command(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.reply("👥 <b>Выберите группу</b>", reply_markup=chat_picker("adm:members:chat"))


@router.callback_query(F.data == "adm:home")
async def admin_home(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    await call.message.edit_text(await admin_home_text(call.from_user.id), reply_markup=admin_main_kb(call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "adm:close")
async def admin_close(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    await call.message.edit_text("Админ-панель закрыта.")
    await call.answer()


@router.callback_query(F.data == "adm:telethon")
async def admin_telethon_status(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    if int(call.from_user.id) != int(OWNER_ID):
        await call.answer("Telethon доступен только владельцу", show_alert=True)
        return
    status_info = telethon_status()
    account = status_info.get("account") or {}
    lines = [
        "🔐 <b>Telethon</b>",
        "",
        f"Статус: <b>{html.escape(status_info['status_label'])}</b>",
        f"API ID: <b>{status_info.get('api_id') or 'не задан'}</b>",
        f"Сессия: <code>{html.escape(status_info.get('session') or '—')}</code>",
    ]
    if account:
        lines.append(f"Аккаунт: <b>{html.escape(account.get('display_name') or str(account.get('id') or '—'))}</b>")
    lines.extend(["", "Авторизация выполняется в закрытой Web-панели: там безопаснее вводить телефон, код и 2FA-пароль."])
    rows = []
    if WEB_PUBLIC_URL:
        rows.append([InlineKeyboardButton(text="🌐 Открыть подключение", url=WEB_PUBLIC_URL.rstrip("/") + "/telethon")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:home")])
    await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "adm:stats")
async def admin_stats(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    stats = dashboard_stats()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Чатов: <b>{stats['tracked_chats']}</b>\n"
        f"Участников: <b>{stats['members']}</b>\n"
        f"Активны 24ч: <b>{stats['active_24h']}</b>\n"
        f"Активны 7д: <b>{stats['active_7d']}</b>\n"
        f"Неактивны 7д+: <b>{stats['inactive_7d']}</b>\n"
        f"Ни разу не писали: <b>{stats['never_wrote']}</b>"
    )
    await call.message.edit_text(text, reply_markup=back_kb())
    await call.answer()


@router.callback_query(F.data == "adm:members")
async def admin_members_picker(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    await call.message.edit_text("👥 <b>Участники — выберите группу</b>", reply_markup=chat_picker("adm:members:chat"))
    await call.answer()


@router.callback_query(F.data.startswith("adm:members:chat:"))
async def admin_members_chat(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    raw = call.data[len("adm:members:chat:"):]
    parts = raw.split(":", 1)
    chat_id = int(parts[0])
    page = max(0, int(parts[1])) if len(parts) > 1 else 0
    page_size = 8
    info = get_chat_info(chat_id) or {}
    probe = list_members_for_web(chat_id, active="active", limit=page_size + 1, offset=page * page_size)
    has_next = len(probe) > page_size
    rows = probe[:page_size]
    lines = []
    for idx, row in enumerate(rows, page * page_size + 1):
        name = html.escape(row["user_name"] or str(row["user_id"]))
        lines.append(
            f"{idx}. <a href=\"tg://user?id={int(row['user_id'])}\">{name}</a> — "
            f"7д: <b>{int(row['messages_7d'])}</b>, 30д: <b>{int(row['messages_30d'])}</b>"
        )
    title = html.escape(info.get("title") or str(chat_id))
    text = f"👥 <b>{title}</b> · стр. {page + 1}\n\n" + ("\n".join(lines) if lines else "Пока нет данных.")
    keyboard_rows = []
    for row in rows:
        button_name = str(row["user_name"] or row["username"] or row["user_id"])
        if len(button_name) > 24:
            button_name = button_name[:21] + "..."
        keyboard_rows.append([InlineKeyboardButton(text=f"💬 Пинг: {button_name}", callback_data=f"adm:nudge:{chat_id}:{int(row['user_id'])}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:members:chat:{chat_id}:{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:members:chat:{chat_id}:{page+1}"))
    if nav:
        keyboard_rows.append(nav)
    keyboard_rows.extend([
        [InlineKeyboardButton(text="🔄 Синхронизировать", callback_data=f"adm:sync:chat:{chat_id}")],
        [InlineKeyboardButton(text="◀️ К группам", callback_data="adm:members")],
    ])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
    await call.answer()


@router.callback_query(F.data == "adm:silent")
async def admin_silent_picker(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    await call.message.edit_text("🤐 <b>Ни разу не писали — выберите группу</b>", reply_markup=chat_picker("adm:silent:chat"))
    await call.answer()


@router.callback_query(F.data.startswith("adm:silent:chat:"))
async def admin_silent_chat(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    raw = call.data[len("adm:silent:chat:"):]
    parts = raw.split(":", 1)
    chat_id = int(parts[0])
    page = max(0, int(parts[1])) if len(parts) > 1 else 0
    page_size = 8
    info = get_chat_info(chat_id) or {}
    probe = list_members_for_web(chat_id, active="silent", limit=page_size + 1, offset=page * page_size)
    has_next = len(probe) > page_size
    rows = probe[:page_size]
    lines = []
    buttons = []
    for idx, row in enumerate(rows, page * page_size + 1):
        user_id = int(row["user_id"])
        name_raw = str(row["user_name"] or row["username"] or user_id)
        name = html.escape(name_raw)
        joined = int(row["joined_at"] or row["first_seen_at"] or 0)
        days = max(0, (int(time.time()) - joined) // 86400) if joined else 0
        lines.append(f"{idx}. <a href=\"tg://user?id={user_id}\">{name}</a> — в группе ~<b>{days} дн.</b>, сообщений: <b>0</b>")
        button_name = name_raw if len(name_raw) <= 24 else name_raw[:21] + "..."
        buttons.append([InlineKeyboardButton(text=f"💬 Пинг: {button_name}", callback_data=f"adm:nudge:{chat_id}:{user_id}")])
    title = html.escape(info.get("title") or str(chat_id))
    text = f"🤐 <b>{title}</b> · стр. {page + 1}" + ("\n\n" + "\n".join(lines) if lines else "\n\nТаких участников нет.")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:silent:chat:{chat_id}:{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:silent:chat:{chat_id}:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ К группам", callback_data="adm:silent")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data.startswith("adm:nudge:"))
async def admin_nudge_member(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    try:
        _, _, chat_raw, user_raw = call.data.split(":", 3)
        chat_id, user_id = int(chat_raw), int(user_raw)
        await send_member_nudge(chat_id, user_id, f"telegram:{call.from_user.id}")
    except Exception as exc:
        await call.answer(f"Не удалось отправить: {str(exc)[:120]}", show_alert=True)
        return
    await call.answer("Сообщение отправлено участнику в группу", show_alert=True)


@router.callback_query(F.data == "adm:inactive")
async def admin_inactive_picker(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    await call.message.edit_text("⚠️ <b>Неактивные — выберите группу</b>", reply_markup=chat_picker("adm:inactive:chat"))
    await call.answer()


@router.callback_query(F.data.startswith("adm:inactive:chat:"))
async def admin_inactive_chat(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    chat_id = int(call.data.rsplit(":", 1)[1])
    settings = get_chat_settings(chat_id)
    rows = get_alert_candidates(chat_id, settings["inactivity_days"], settings["min_message_count"], int(time.time()), 20)
    info = get_chat_info(chat_id) or {}
    lines = []
    for idx, row in enumerate(rows, 1):
        name = html.escape(row["user_name"] or str(row["user_id"]))
        last_ts = int(row["last_message_at"] or row["joined_at"] or row["first_seen_at"] or 0)
        days = max(0, (int(time.time()) - last_ts) // 86400) if last_ts else 0
        lines.append(f"{idx}. <a href=\"tg://user?id={int(row['user_id'])}\">{name}</a> — <b>{days} дн.</b>, сообщений за период: {int(row['period_message_count'] or 0)}")
    title = html.escape(info.get("title") or str(chat_id))
    text = f"⚠️ <b>{title}</b>\nПорог: {settings['inactivity_days']} дн.\n\n" + ("\n".join(lines) if lines else "Неактивных по текущим правилам нет.")
    await call.message.edit_text(text, reply_markup=back_kb())
    await call.answer()


@router.callback_query(F.data == "adm:sync")
async def admin_sync_picker(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    await call.message.edit_text("🔄 <b>Синхронизация — выберите группу</b>", reply_markup=chat_picker("adm:sync:chat"))
    await call.answer()


@router.callback_query(F.data.startswith("adm:sync:chat:"))
async def admin_sync_chat(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    chat_id = int(call.data.rsplit(":", 1)[1])
    request_sync(chat_id, f"telegram:{call.from_user.id}", "full")
    await call.answer("Синхронизация поставлена в очередь", show_alert=True)


@router.callback_query(F.data == "adm:notifs")
async def admin_notifications(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    rows = []
    lines = ["🔔 <b>Оповещения администраторов</b>\n"]
    for admin in list_system_admins():
        enabled = bool(admin["notifications_enabled"])
        name = html.escape(admin["display_name"] or str(admin["user_id"]))
        lines.append(f"{'🔔' if enabled else '🔕'} {name} <code>{admin['user_id']}</code>")
        if call.from_user.id == OWNER_ID or bool(current_settings().get("allow_admins_edit", True)):
            rows.append([
                InlineKeyboardButton(
                    text=f"{'🔕 Выкл.' if enabled else '🔔 Вкл.'} {name[:22]}",
                    callback_data=f"adm:notif:toggle:{int(admin['user_id'])}:{0 if enabled else 1}",
                )
            ])
    rows.append([InlineKeyboardButton(text="🧪 Тест оповещения мне", callback_data="adm:notif:test")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:home")])
    await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "adm:notif:test")
async def admin_notification_test_picker(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    await call.message.edit_text("🧪 <b>Тест оповещения — выберите группу</b>", reply_markup=chat_picker("adm:notif:test:chat"))
    await call.answer()


@router.callback_query(F.data.startswith("adm:notif:test:chat:"))
async def admin_notification_test(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    chat_id = int(call.data.rsplit(":", 1)[1])
    try:
        await send_test_alert(call.from_user.id, chat_id, f"telegram:{call.from_user.id}")
    except Exception as exc:
        await call.answer(f"Тест не отправлен: {str(exc)[:120]}", show_alert=True)
        return
    await call.answer("Тестовое оповещение отправлено вам в этот чат", show_alert=True)


@router.callback_query(F.data.startswith("adm:notif:toggle:"))
async def admin_notification_toggle(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    if call.from_user.id != OWNER_ID and not bool(current_settings().get("allow_admins_edit", True)):
        await call.answer("Изменение настроек запрещено владельцем", show_alert=True)
        return
    parts = call.data.split(":")
    user_id, enabled = int(parts[-2]), bool(int(parts[-1]))
    set_admin_notifications(user_id, enabled)
    await admin_notifications(call)


@router.callback_query(F.data == "adm:bot")
async def admin_bot_settings(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    settings = current_settings()
    editable = call.from_user.id == OWNER_ID or bool(settings.get("allow_admins_edit", True))
    rows = []
    if editable:
        rows.append([
            InlineKeyboardButton(
                text="🔴 Выключить онбординг" if settings["is_active"] else "🟢 Включить онбординг",
                callback_data=f"adm:bot:toggle:{0 if settings['is_active'] else 1}",
            )
        ])
    if WEB_PUBLIC_URL:
        rows.append([InlineKeyboardButton(text="🌐 Все настройки в Web", url=WEB_PUBLIC_URL.rstrip("/") + "/bot")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:home")])
    text = (
        "⚙️ <b>Бот</b>\n\n"
        f"Онбординг: <b>{'ON' if settings['is_active'] else 'OFF'}</b>\n"
        f"Уровень: <b>{settings['level']}</b>\n"
        f"Время: <b>{html.escape(settings['work_start'])}–{html.escape(settings['work_end'])}</b>\n"
        f"Задержка: <b>{settings['reply_delay']} сек.</b>"
    )
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("adm:bot:toggle:"))
async def admin_bot_toggle(call: CallbackQuery) -> None:
    if not await require_admin_call(call):
        return
    settings = current_settings()
    if call.from_user.id != OWNER_ID and not bool(settings.get("allow_admins_edit", True)):
        await call.answer("Изменение настроек запрещено владельцем", show_alert=True)
        return
    enabled = bool(int(call.data.rsplit(":", 1)[1]))
    settings["is_active"] = enabled
    save_settings(settings, OWNER_ID)
    await admin_bot_settings(call)


async def refresh_admin_commands() -> None:
    current: set[int] = set()
    admin_commands = [
        BotCommand(command="admin", description="Админ-панель"),
        BotCommand(command="members", description="Участники групп"),
        BotCommand(command="activity", description="Статистика активности"),
    ]
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except Exception as exc:
        await botlog(f"clear default commands failed: {exc}")

    while True:
        desired = set(list_admin_ids())
        for removed in current - desired:
            try:
                await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=removed))
            except Exception:
                pass
        configured = set(current & desired)
        for admin_id in desired - current:
            try:
                await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
                configured.add(admin_id)
            except Exception as exc:
                await botlog(f"admin command scope {admin_id} failed: {exc}")
        current = configured
        await asyncio.sleep(60)


async def run_bot() -> None:
    await botlog("BOT START")
    dp.include_router(router)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as exc:
        await botlog(f"delete_webhook failed: {exc}")
    asyncio.create_task(inactivity_watcher(bot, botlog, sleep_seconds=60))
    asyncio.create_task(refresh_admin_commands())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def main() -> None:
    await run_bot()


if __name__ == "__main__":
    asyncio.run(main())
