from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import time
from datetime import datetime
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import (
    activity_series,
    bootstrap_admins,
    add_system_admin,
    audit,
    dashboard_stats,
    get_chat_info,
    get_chat_member_stats,
    init_db,
    get_chat_settings,
    list_known_chats,
    list_hidden_chats,
    list_inactive_members_for_web,
    list_members_for_web,
    list_system_admins,
    remove_system_admin,
    request_sync,
    set_admin_notifications,
    set_chat_settings,
    set_chat_tracked,
    set_chat_hidden,
)
from settings_store import load_settings, save_settings
from telethon_auth import TelethonAuthError, TelethonAuthManager
from telethon_config import load_telethon_config, telethon_status
from telegram_identity import UsernameResolveError, resolve_user_by_username
from runtime_paths import DATA_DIR, DATABASE_PATH, load_or_create_web_secret, web_port, web_public_url

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OWNER_ID_ENV = (os.getenv("OWNER_ID") or "").strip()
WEB_USERNAME = os.getenv("WEB_ADMIN_USERNAME", "admin")
WEB_PASSWORD = os.getenv("WEB_ADMIN_PASSWORD", "")
WEB_SECRET = load_or_create_web_secret()
_PUBLIC_URL = web_public_url()
_cookie_raw = os.getenv("WEB_COOKIE_SECURE")
WEB_COOKIE_SECURE = (
    _cookie_raw.strip().lower() in {"1", "true", "yes", "on"}
    if _cookie_raw is not None
    else _PUBLIC_URL.startswith("https://")
)
SESSION_TTL = max(900, int(os.getenv("WEB_SESSION_TTL_SECONDS", str(12 * 3600))))
COOKIE_NAME = "lorenzo_admin_session"
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 10
_LOGIN_FAILURES: dict[str, list[int]] = {}


def _request_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if forwarded:
        return forwarded[:80]
    return (request.client.host if request.client else "unknown")[:80]


def _login_is_blocked(request: Request) -> bool:
    now = int(time.time())
    key = _request_ip(request)
    recent = [ts for ts in _LOGIN_FAILURES.get(key, []) if now - ts < LOGIN_WINDOW_SECONDS]
    if recent:
        _LOGIN_FAILURES[key] = recent
    else:
        _LOGIN_FAILURES.pop(key, None)
    return len(recent) >= LOGIN_MAX_FAILURES


def _record_login_failure(request: Request) -> None:
    key = _request_ip(request)
    _LOGIN_FAILURES.setdefault(key, []).append(int(time.time()))


def _clear_login_failures(request: Request) -> None:
    _LOGIN_FAILURES.pop(_request_ip(request), None)


app = FastAPI(title="Lorenzo Admin", docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    if WEB_COOKIE_SECURE:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
init_db()
_existing_admins = list_system_admins()
if OWNER_ID_ENV:
    OWNER_ID = int(OWNER_ID_ENV)
    bootstrap_admins(OWNER_ID, [])
else:
    _owners = [int(row["user_id"]) for row in _existing_admins if int(row["is_owner"])]
    if not _owners:
        raise RuntimeError("OWNER_ID not found. Add OWNER_ID=... to .env for the first start")
    OWNER_ID = _owners[0]

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
telethon_auth = TelethonAuthManager()


def fmt_ts(value) -> str:
    try:
        if not value:
            return "—"
        return datetime.fromtimestamp(int(value)).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "—"


def fmt_username(value) -> str:
    return f"@{value}" if value else "—"


templates.env.filters["dt"] = fmt_ts
templates.env.filters["tguser"] = fmt_username


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(data: str) -> str:
    return hmac.new(WEB_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()


def _new_session(username: str) -> str:
    payload = {
        "u": username,
        "exp": int(time.time()) + SESSION_TTL,
        "csrf": secrets.token_urlsafe(24),
        # The current web credential is the owner console. Keeping the role in
        # the signed session makes owner-only routes explicit and future-proof.
        "role": "owner",
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}"


def _read_session(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw or "." not in raw:
        return None
    body, signature = raw.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(body)):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if payload.get("u") != WEB_USERNAME or int(payload.get("exp", 0)) < int(time.time()):
        return None
    # Sessions created by the previous release did not carry an explicit role.
    # The only web credential in that release was the owner console.
    payload.setdefault("role", "owner")
    return payload


def require_session(request: Request) -> dict:
    session = _read_session(request)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return session


def verify_csrf(request: Request, csrf: str) -> dict:
    session = require_session(request)
    if not csrf or not hmac.compare_digest(str(session.get("csrf", "")), str(csrf)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return session


def require_owner_session(request: Request) -> dict:
    session = require_session(request)
    if session.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Доступ только владельцу")
    return session


def render(request: Request, name: str, **context):
    session = _read_session(request)
    context.update(
        {
            "request": request,
            "session": session,
            "csrf": session.get("csrf") if session else "",
            "owner_id": OWNER_ID,
            "web_username": WEB_USERNAME,
            "web_cookie_secure": WEB_COOKIE_SECURE,
            "is_owner": bool(session and session.get("role") == "owner"),
        }
    )
    return templates.TemplateResponse(request=request, name=name, context=context)


def redirect(path: str, message: str | None = None) -> RedirectResponse:
    if message:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}msg={quote(message)}"
    return RedirectResponse(path, status_code=303)


@app.exception_handler(HTTPException)
async def auth_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return redirect("/login")
    response = render(request, "error.html", code=exc.status_code, message=exc.detail)
    response.status_code = exc.status_code
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _read_session(request):
        return redirect("/")
    return render(request, "login.html", configured=bool(WEB_PASSWORD), error=None)


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if not WEB_PASSWORD:
        return render(request, "login.html", configured=False, error="WEB_ADMIN_PASSWORD не задан в окружении")
    if _login_is_blocked(request):
        audit(f"web:{username[:80]}", "login_rate_limited", details={"ip": _request_ip(request)})
        return render(request, "login.html", configured=True, error="Слишком много неудачных попыток. Повторите позже.")
    valid_user = hmac.compare_digest(username.strip(), WEB_USERNAME)
    valid_password = hmac.compare_digest(password, WEB_PASSWORD)
    if not (valid_user and valid_password):
        _record_login_failure(request)
        audit(f"web:{username[:80]}", "login_failed", details={"ip": _request_ip(request)})
        return render(request, "login.html", configured=True, error="Неверный логин или пароль")
    _clear_login_failures(request)
    response = redirect("/")
    response.set_cookie(
        COOKIE_NAME,
        _new_session(WEB_USERNAME),
        httponly=True,
        secure=WEB_COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_TTL,
        path="/",
    )
    audit(f"web:{WEB_USERNAME}", "login")
    return response


@app.post("/logout")
async def logout(request: Request, csrf: str = Form(...)):
    verify_csrf(request, csrf)
    response = redirect("/login")
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/admin", include_in_schema=False)
async def admin_alias(request: Request):
    # Friendly alias: some admins naturally try /admin in a browser.
    if not _read_session(request):
        return redirect("/login")
    return redirect("/")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, chat_id: int | None = None, msg: str | None = None):
    require_session(request)
    chats = list_known_chats(include_untracked=False)
    visible_ids = {int(row["chat_id"]) for row in chats}
    if chat_id is not None and int(chat_id) not in visible_ids:
        chat_id = None
    selected = get_chat_info(chat_id) if chat_id is not None else None
    stats = dashboard_stats(chat_id)
    series = activity_series(chat_id, days=30)
    max_messages = max([row["messages"] for row in series] + [1])
    return render(
        request,
        "dashboard.html",
        chats=chats,
        selected=selected,
        selected_chat_id=chat_id,
        stats=stats,
        series=series,
        max_messages=max_messages,
        msg=msg,
    )


@app.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request, msg: str | None = None):
    require_session(request)
    rows = []
    for row in list_known_chats(include_untracked=True):
        item = dict(row)
        item["settings"] = get_chat_settings(int(row["chat_id"])) if int(row["is_tracked"] or 0) else None
        rows.append(item)
    return render(request, "chats.html", chats=rows, msg=msg)


@app.get("/chats/hidden", response_class=HTMLResponse)
async def hidden_chats_page(request: Request, msg: str | None = None):
    require_owner_session(request)
    return render(request, "hidden_chats.html", chats=[dict(row) for row in list_hidden_chats()], msg=msg)


@app.post("/chats/{chat_id}/hide")
async def chat_hide(request: Request, chat_id: int, csrf: str = Form(...)):
    require_owner_session(request)
    verify_csrf(request, csrf)
    if not set_chat_hidden(chat_id, True):
        raise HTTPException(status_code=400, detail="Некорректный чат")
    audit(f"web:{WEB_USERNAME}", "chat_hidden", str(chat_id))
    return redirect("/chats", "Чат скрыт. Отслеживание и алерты отключены.")


@app.post("/chats/{chat_id}/restore")
async def chat_restore(request: Request, chat_id: int, csrf: str = Form(...)):
    require_owner_session(request)
    verify_csrf(request, csrf)
    if not set_chat_hidden(chat_id, False):
        raise HTTPException(status_code=400, detail="Некорректный чат")
    audit(f"web:{WEB_USERNAME}", "chat_restored", str(chat_id))
    return redirect("/chats/hidden", "Чат возвращён в список доступных. Отслеживание остаётся выключенным до ручного включения.")


@app.post("/chats/{chat_id}/track")
async def chat_track(request: Request, chat_id: int, csrf: str = Form(...), enabled: int = Form(...)):
    verify_csrf(request, csrf)
    if not set_chat_tracked(chat_id, bool(enabled)):
        raise HTTPException(status_code=400, detail="Можно отслеживать только group/supergroup")
    audit(f"web:{WEB_USERNAME}", "chat_tracking", str(chat_id), {"enabled": bool(enabled)})
    if enabled:
        request_sync(chat_id, f"web:{WEB_USERNAME}", "full")
    return redirect("/chats", "Настройки отслеживания сохранены")


@app.post("/chats/{chat_id}/sync")
async def chat_sync(request: Request, chat_id: int, csrf: str = Form(...)):
    verify_csrf(request, csrf)
    info = get_chat_info(chat_id)
    if not info or info.get("chat_type") not in {"group", "supergroup"} or int(info.get("is_hidden") or 0):
        raise HTTPException(status_code=400, detail="Некорректный или скрытый чат")
    request_sync(chat_id, f"web:{WEB_USERNAME}", "full")
    audit(f"web:{WEB_USERNAME}", "sync_requested", str(chat_id))
    return redirect("/chats", "Синхронизация поставлена в очередь")


@app.post("/chats/{chat_id}/settings")
async def chat_settings_submit(
    request: Request,
    chat_id: int,
    csrf: str = Form(...),
    inactivity_days: int = Form(...),
    min_message_count: int = Form(...),
    repeat_alert_hours: int = Form(...),
    check_interval_minutes: int = Form(...),
    alerts_enabled: int = Form(0),
):
    verify_csrf(request, csrf)
    info = get_chat_info(chat_id)
    if not info or info.get("chat_type") not in {"group", "supergroup"} or int(info.get("is_hidden") or 0):
        raise HTTPException(status_code=400, detail="Некорректный или скрытый чат")
    set_chat_settings(
        chat_id,
        inactivity_days=inactivity_days,
        min_message_count=min_message_count,
        repeat_alert_hours=repeat_alert_hours,
        check_interval_minutes=check_interval_minutes,
        enabled=bool(alerts_enabled),
    )
    audit(f"web:{WEB_USERNAME}", "chat_settings", str(chat_id))
    return redirect("/chats", "Настройки активности сохранены")


@app.get("/members", response_class=HTMLResponse)
async def members_page(
    request: Request,
    chat_id: int | None = None,
    q: str = "",
    state: str = "active",
    msg: str | None = None,
):
    require_session(request)
    chats = list_known_chats(include_untracked=False)
    visible_ids = {int(row["chat_id"]) for row in chats}
    if chat_id is not None and int(chat_id) not in visible_ids:
        chat_id = None
    if chat_id is None and chats:
        chat_id = int(chats[0]["chat_id"])
    if chat_id is None:
        members = []
    elif state == "inactive":
        members = list_inactive_members_for_web(chat_id, limit=300)
        if q.strip():
            ql = q.strip().lower()
            members = [m for m in members if ql in str(m["user_id"]).lower() or ql in str(m["user_name"] or "").lower() or ql in str(m["username"] or "").lower()]
    else:
        members = list_members_for_web(chat_id, search=q, active=state, limit=300)
    return render(
        request,
        "members.html",
        chats=chats,
        chat_id=chat_id,
        members=members,
        q=q,
        state=state,
        msg=msg,
    )


@app.get("/admins", response_class=HTMLResponse)
async def admins_page(request: Request, msg: str | None = None):
    require_session(request)
    return render(request, "admins.html", admins=list_system_admins(), msg=msg)


@app.post("/admins/add")
async def admins_add(
    request: Request,
    csrf: str = Form(...),
    user_id: str = Form(""),
    username: str = Form(""),
    display_name: str = Form(""),
):
    require_owner_session(request)
    verify_csrf(request, csrf)
    raw_id = user_id.strip()
    raw_username = username.strip().lstrip("@")

    resolved = None
    if raw_username:
        try:
            resolved = await resolve_user_by_username(raw_username)
        except UsernameResolveError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        target_id = int(resolved["user_id"])
        target_username = str(resolved.get("username") or raw_username).lstrip("@")
        target_name = display_name.strip() or str(resolved.get("display_name") or target_id)
    elif raw_id:
        try:
            target_id = int(raw_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Telegram ID должен быть числом")
        if target_id <= 0:
            raise HTTPException(status_code=400, detail="Telegram ID должен быть положительным числом")
        target_username = None
        target_name = display_name.strip() or None
    else:
        raise HTTPException(status_code=400, detail="Введите @username или Telegram ID")

    add_system_admin(target_id, display_name=target_name, username=target_username)
    audit(
        f"web:{WEB_USERNAME}",
        "admin_added",
        str(target_id),
        {"username": target_username or "", "resolved_by": (resolved or {}).get("source", "manual_id")},
    )
    label = f"@{target_username}" if target_username else str(target_id)
    return redirect("/admins", f"Администратор {label} добавлен")


@app.post("/admins/{user_id}/remove")
async def admins_remove(request: Request, user_id: int, csrf: str = Form(...)):
    verify_csrf(request, csrf)
    if not remove_system_admin(user_id):
        raise HTTPException(status_code=400, detail="Владельца удалить нельзя")
    audit(f"web:{WEB_USERNAME}", "admin_removed", str(user_id))
    return redirect("/admins", "Администратор удалён")


@app.post("/admins/{user_id}/notifications")
async def admins_notifications(request: Request, user_id: int, csrf: str = Form(...), enabled: int = Form(...)):
    verify_csrf(request, csrf)
    if not set_admin_notifications(user_id, bool(enabled)):
        raise HTTPException(status_code=404, detail="Администратор не найден")
    audit(f"web:{WEB_USERNAME}", "admin_notifications", str(user_id), {"enabled": bool(enabled)})
    return redirect("/admins", "Оповещения обновлены")


@app.get("/bot", response_class=HTMLResponse)
async def bot_settings_page(request: Request, msg: str | None = None):
    require_session(request)
    return render(
        request,
        "bot_settings.html",
        settings=load_settings(OWNER_ID),
        chats=list_known_chats(include_untracked=False),
        admins=list_system_admins(),
        msg=msg,
    )


@app.post("/bot")
async def bot_settings_save(
    request: Request,
    csrf: str = Form(...),
    is_active: int = Form(0),
    level: int = Form(...),
    work_start: str = Form(...),
    work_end: str = Form(...),
    reply_delay: int = Form(...),
    welcome_text: str = Form(...),
    consent_text: str = Form(...),
    nudge_text: str = Form(...),
):
    verify_csrf(request, csrf)
    current = load_settings(OWNER_ID)
    current.update(
        {
            "is_active": bool(is_active),
            "level": level,
            "work_start": work_start.strip(),
            "work_end": work_end.strip(),
            "reply_delay": reply_delay,
            "texts": {
                "welcome_text": welcome_text,
                "consent_text": consent_text,
                "nudge_text": nudge_text,
            },
        }
    )
    save_settings(current, OWNER_ID)
    audit(f"web:{WEB_USERNAME}", "bot_settings")
    return redirect("/bot", "Настройки бота сохранены")


@app.post("/bot/test-notification")
async def bot_test_notification(
    request: Request,
    csrf: str = Form(...),
    chat_id: int = Form(...),
    recipient_id: int = Form(...),
):
    require_owner_session(request)
    verify_csrf(request, csrf)
    visible_chat_ids = {int(row["chat_id"]) for row in list_known_chats(include_untracked=False)}
    if int(chat_id) not in visible_chat_ids:
        raise HTTPException(status_code=400, detail="Выбранный чат не отслеживается")
    admin_ids = {int(row["user_id"]) for row in list_system_admins()}
    if int(recipient_id) not in admin_ids:
        raise HTTPException(status_code=400, detail="Получатель не является администратором")
    try:
        from bot_app import send_test_alert
        await send_test_alert(int(recipient_id), int(chat_id), f"web:{WEB_USERNAME}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось отправить тест: {str(exc)[:220]}")
    return redirect("/bot", "Тестовое оповещение отправлено")


@app.post("/members/nudge")
async def member_nudge_from_web(
    request: Request,
    csrf: str = Form(...),
    chat_id: int = Form(...),
    user_id: int = Form(...),
):
    require_session(request)
    verify_csrf(request, csrf)
    member = get_chat_member_stats(int(chat_id), int(user_id))
    if not member or not int(member.get("is_active") or 0) or int(member.get("is_bot") or 0):
        raise HTTPException(status_code=400, detail="Пользователь больше не является активным участником группы")
    try:
        from bot_app import send_member_nudge
        await send_member_nudge(int(chat_id), int(user_id), f"web:{WEB_USERNAME}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось отправить пинг: {str(exc)[:220]}")
    return redirect(f"/members?chat_id={int(chat_id)}", "Пинг участнику отправлен в группу")


def _qr_svg_data_uri(url: str | None) -> str | None:
    if not url:
        return None
    try:
        import qrcode
        import qrcode.image.svg

        image = qrcode.make(
            url,
            image_factory=qrcode.image.svg.SvgPathFillImage,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            border=3,
        )
        raw = image.to_string()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return None


def _telethon_page_response(request: Request, *, msg: str | None = None, error: str | None = None):
    status_info = telethon_status()
    auth_info = telethon_auth.public_state()
    step = telethon_auth.step
    if status_info["authorized"]:
        step = "done"
    elif not status_info["configured"]:
        step = "config"
    elif step == "idle":
        step = "phone"
    qr_url = telethon_auth.qr_url if step == "qr" else None
    return render(
        request,
        "telethon.html",
        telethon=status_info,
        auth_step=step,
        auth_info=auth_info,
        qr_image=_qr_svg_data_uri(qr_url),
        qr_deep_link=qr_url,
        msg=msg,
        error=error,
    )


@app.get("/telethon", response_class=HTMLResponse)
async def telethon_page(request: Request, msg: str | None = None):
    require_owner_session(request)
    return _telethon_page_response(request, msg=msg)


@app.post("/telethon/configure", response_class=HTMLResponse)
async def telethon_configure(
    request: Request,
    csrf: str = Form(...),
    api_id: str = Form(...),
    api_hash: str = Form(""),
):
    require_owner_session(request)
    verify_csrf(request, csrf)
    current = load_telethon_config()
    if telethon_status()["authorized"]:
        return _telethon_page_response(request, error="Telethon уже авторизован. Для обычной работы менять API-данные не требуется.")
    try:
        parsed_api_id = int(api_id.strip())
        selected_hash = api_hash.strip() or str(current.get("api_hash") or "")
        if not selected_hash:
            raise TelethonAuthError("Введите API Hash.")
        await telethon_auth.configure(parsed_api_id, selected_hash)
    except (ValueError, TelethonAuthError) as exc:
        return _telethon_page_response(request, error=str(exc))
    audit(f"web:{WEB_USERNAME}", "telethon_configured")
    return redirect("/telethon", "API-данные сохранены. Теперь введите номер Telegram.")


@app.post("/telethon/check", response_class=HTMLResponse)
async def telethon_check_existing(request: Request, csrf: str = Form(...)):
    require_owner_session(request)
    verify_csrf(request, csrf)
    try:
        result = await telethon_auth.check_existing()
    except TelethonAuthError as exc:
        return _telethon_page_response(request, error=str(exc))
    if result.get("authorized"):
        audit(f"web:{WEB_USERNAME}", "telethon_existing_session_verified")
        return redirect("/telethon", "Существующая StringSession подтверждена и готова к работе.")
    return redirect("/telethon", "StringSession отсутствует или больше не авторизована. Введите номер телефона.")


@app.post("/telethon/qr", response_class=HTMLResponse)
async def telethon_qr_start(request: Request, csrf: str = Form(...)):
    require_owner_session(request)
    verify_csrf(request, csrf)
    try:
        result = await telethon_auth.start_qr()
    except TelethonAuthError as exc:
        return _telethon_page_response(request, error=str(exc))
    if result.get("authorized"):
        audit(f"web:{WEB_USERNAME}", "telethon_authorized_existing")
        return redirect("/telethon", "Telethon уже авторизован. Сессия готова к работе.")
    audit(f"web:{WEB_USERNAME}", "telethon_qr_started")
    return redirect("/telethon", "QR-код создан. Отсканируйте его в Telegram → Настройки → Устройства → Подключить устройство.")


@app.get("/telethon/qr/status")
async def telethon_qr_status(request: Request):
    require_owner_session(request)
    status_info = telethon_status()
    auth_info = telethon_auth.public_state()
    step = "done" if status_info.get("authorized") else str(auth_info.get("step") or "idle")
    return {
        "authorized": bool(status_info.get("authorized")),
        "step": step,
        "qr_error": auth_info.get("qr_error") or "",
        "resend_wait_seconds": auth_info.get("resend_wait_seconds") or 0,
    }


@app.post("/telethon/resend", response_class=HTMLResponse)
async def telethon_resend(request: Request, csrf: str = Form(...)):
    require_owner_session(request)
    verify_csrf(request, csrf)
    try:
        result = await telethon_auth.resend_code()
    except TelethonAuthError as exc:
        return _telethon_page_response(request, error=str(exc))
    info = result.get("code_info") or {}
    delivery = info.get("delivery") or "другой доступный способ"
    audit(f"web:{WEB_USERNAME}", "telethon_code_resent", details={"delivery": delivery})
    return redirect("/telethon", f"Telegram повторно отправил код: {delivery}.")


@app.post("/telethon/phone", response_class=HTMLResponse)
async def telethon_phone(request: Request, csrf: str = Form(...), phone: str = Form(...)):
    require_owner_session(request)
    verify_csrf(request, csrf)
    try:
        result = await telethon_auth.send_code(phone)
    except TelethonAuthError as exc:
        return _telethon_page_response(request, error=str(exc))
    if result.get("authorized"):
        audit(f"web:{WEB_USERNAME}", "telethon_authorized_existing")
        return redirect("/telethon", "Telethon уже был авторизован. Сессия готова к работе.")
    info = result.get("code_info") or {}
    delivery = info.get("delivery") or "выбранный Telegram способ"
    audit(f"web:{WEB_USERNAME}", "telethon_code_requested", details={"delivery": delivery})
    return redirect("/telethon", f"Telegram принял запрос. Способ доставки: {delivery}.")


@app.post("/telethon/code", response_class=HTMLResponse)
async def telethon_code(request: Request, csrf: str = Form(...), code: str = Form(...)):
    require_owner_session(request)
    verify_csrf(request, csrf)
    try:
        result = await telethon_auth.submit_code(code)
    except TelethonAuthError as exc:
        return _telethon_page_response(request, error=str(exc))
    if result.get("step") == "password":
        return redirect("/telethon", "Для аккаунта включена двухэтапная аутентификация. Введите пароль.")
    audit(f"web:{WEB_USERNAME}", "telethon_authorized")
    return redirect("/telethon", "Telethon успешно подключён.")


@app.post("/telethon/password", response_class=HTMLResponse)
async def telethon_password(request: Request, csrf: str = Form(...), password: str = Form(...)):
    require_owner_session(request)
    verify_csrf(request, csrf)
    try:
        await telethon_auth.submit_password(password)
    except TelethonAuthError as exc:
        return _telethon_page_response(request, error=str(exc))
    audit(f"web:{WEB_USERNAME}", "telethon_authorized_2fa")
    return redirect("/telethon", "Telethon успешно подключён с 2FA.")


@app.post("/telethon/enabled")
async def telethon_enabled(request: Request, csrf: str = Form(...), enabled: int = Form(...)):
    require_owner_session(request)
    verify_csrf(request, csrf)
    status_info = telethon_status()
    if bool(enabled) and not status_info["authorized"]:
        return _telethon_page_response(request, error="Сначала завершите авторизацию Telethon.")
    from telethon_config import save_telethon_config
    save_telethon_config(enabled=bool(enabled))
    audit(f"web:{WEB_USERNAME}", "telethon_enabled", details={"enabled": bool(enabled)})
    return redirect("/telethon", "Синхронизация Telethon включена." if enabled else "Синхронизация Telethon остановлена.")


@app.post("/telethon/cancel")
async def telethon_cancel(request: Request, csrf: str = Form(...)):
    require_owner_session(request)
    verify_csrf(request, csrf)
    await telethon_auth.reset(clear_pending=True)
    audit(f"web:{WEB_USERNAME}", "telethon_setup_cancelled")
    return redirect("/telethon", "Мастер авторизации сброшен. Можно запросить новый код.")


@app.get("/health")
async def health(request: Request):
    # No secret values are returned. This endpoint is intentionally public so
    # deployment can be diagnosed without shell access on Bothost.
    bot_status = getattr(request.app.state, "bot_status", "not_started")
    telethon_runtime = getattr(request.app.state, "telethon_status", "not_started")
    return {
        "ok": True,
        "ready": bot_status == "running",
        "time": int(time.time()),
        "config": {
            "python": platform.python_version(),
            "port": web_port(),
            "domain_detected": bool((os.getenv("DOMAIN") or "").strip()),
            "bot_token_set": bool((os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("API_TOKEN") or "").strip()),
            "owner_id_set": bool((os.getenv("OWNER_ID") or "").strip()),
            "web_password_set": bool((os.getenv("WEB_ADMIN_PASSWORD") or "").strip()),
            "data_dir_exists": DATA_DIR.exists(),
            "database_exists": DATABASE_PATH.exists(),
        },
        "runtime": {
            "bot": bot_status,
            "bot_error": getattr(request.app.state, "bot_error", ""),
            "telethon": telethon_runtime,
            "telethon_error": getattr(request.app.state, "telethon_error", ""),
        },
    }
