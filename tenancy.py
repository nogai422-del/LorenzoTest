"""Chat boundaries. Telegram membership is checked live at every privileged action."""
from __future__ import annotations

import json
import os
import secrets
import time
from contextvars import ContextVar
from db import get_conn
from settings_store import default_settings, normalize_settings

HOME_CHAT_ID = -1001972997832
chat_context: ContextVar[int | None] = ContextVar('lorenzo_chat', default=None)


def init_tenancy():
    with get_conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS tenant_config (
            chat_id INTEGER PRIMARY KEY, tier INTEGER NOT NULL DEFAULT 0,
            settings TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS tenant_admins (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS tenant_tickets (
            token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tenant_orders (
            token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            tier INTEGER NOT NULL, amount INTEGER NOT NULL, created INTEGER NOT NULL,
            charge TEXT UNIQUE, paid INTEGER, refunded INTEGER NOT NULL DEFAULT 0);
        ''')


def tier(chat_id: int) -> int:
    if int(chat_id) == HOME_CHAT_ID:
        return 2
    with get_conn() as c:
        row = c.execute('SELECT tier FROM tenant_config WHERE chat_id=?', (int(chat_id),)).fetchone()
    return int(row['tier']) if row else 0


def settings(chat_id: int, owner_id: int = 0) -> dict:
    with get_conn() as c:
        row = c.execute('SELECT settings FROM tenant_config WHERE chat_id=?', (int(chat_id),)).fetchone()
    if row:
        return normalize_settings(json.loads(row['settings']), owner_id)
    if int(chat_id) == HOME_CHAT_ID:
        from settings_store import load_settings
        return load_settings(owner_id)
    result = default_settings(owner_id)
    result.update(level=2, work_start='00:00', work_end='23:59', reply_delay=0)
    return result


def save_chat_settings(chat_id: int, value: dict):
    if tier(chat_id) < 1:
        raise PermissionError('Требуется уровень Редактирование')
    value = normalize_settings(value, 0)
    with get_conn() as c:
        c.execute('INSERT INTO tenant_config(chat_id,settings) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET settings=excluded.settings',
                  (int(chat_id), json.dumps(value, ensure_ascii=False)))


async def is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(int(chat_id), int(user_id))
        return member.status in {'creator', 'administrator'}
    except Exception:
        return False


async def refresh_admins(bot, chat_id: int) -> list[int]:
    try:
        rows = await bot.get_chat_administrators(int(chat_id))
    except Exception:
        return []  # Fail closed; never fall back to global or stale recipients.
    ids = [int(m.user.id) for m in rows if not m.user.is_bot]
    with get_conn() as c:
        c.execute('DELETE FROM tenant_admins WHERE chat_id=?', (int(chat_id),))
        c.executemany('INSERT INTO tenant_admins(chat_id,user_id) VALUES (?,?)', [(int(chat_id), uid) for uid in ids])
    return ids


def new_ticket(chat_id: int, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with get_conn() as c:
        c.execute('INSERT INTO tenant_tickets VALUES (?,?,?,?,0)', (token, chat_id, user_id, int(time.time()) + 300))
    return token


def consume_ticket(token: str):
    with get_conn() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM tenant_tickets WHERE token=? AND used=0 AND expires>?', (token, int(time.time()))).fetchone()
        if row:
            c.execute('UPDATE tenant_tickets SET used=1 WHERE token=?', (token,))
    return dict(row) if row else None


def star_price(level: int) -> int:
    if level not in (1, 2):
        raise ValueError('Unknown tier')
    return max(1, int(os.getenv('TIER_EDIT_STARS' if level == 1 else 'TIER_PANEL_STARS', '25' if level == 1 else '50')))


def create_order(chat_id: int, user_id: int, level: int) -> tuple[str, int]:
    amount = star_price(level)
    if level == 2 and tier(chat_id) == 1:
        amount = max(1, amount - star_price(1))
    token = secrets.token_urlsafe(24)
    with get_conn() as c:
        c.execute('INSERT INTO tenant_orders(token,chat_id,user_id,tier,amount,created) VALUES (?,?,?,?,?,?)',
                  (token, chat_id, user_id, level, amount, int(time.time())))
    return token, amount


def get_order(token: str):
    with get_conn() as c:
        row = c.execute('SELECT * FROM tenant_orders WHERE token=?', (token,)).fetchone()
    return dict(row) if row else None


def confirm_payment(token: str, user_id: int, amount: int, currency: str, charge: str) -> bool:
    with get_conn() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM tenant_orders WHERE token=?', (token,)).fetchone()
        if not row or row['user_id'] != user_id or row['amount'] != amount or currency != 'XTR' or not charge:
            raise ValueError('Payment does not match order')
        if row['paid']:
            if row['charge'] != charge:
                raise ValueError('Order already paid')
            return False
        c.execute('UPDATE tenant_orders SET paid=?,charge=? WHERE token=?', (int(time.time()), charge, token))
        c.execute('INSERT INTO tenant_config(chat_id,tier) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET tier=MAX(tier,excluded.tier)',
                  (row['chat_id'], row['tier']))
    return True
