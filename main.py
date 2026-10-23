"""Lorenzo entrypoint for Bothost and regular ASGI runners.

This module intentionally exports ``app`` so it works in both modes:

* ``python main.py``
* ``uvicorn main:app`` / hosting platforms that detect a FastAPI app

Telegram Bot API polling and the Telethon worker are started as background
runtime tasks when FastAPI starts.  The web panel stays available even if the
bot runtime has a configuration/network error, so /health can show the reason.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
from typing import Any

import uvicorn

from runtime_paths import DATA_DIR, DATABASE_PATH, web_port, web_public_url
from web_app import app

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("lorenzo_runtime")

_RUNTIME_TASKS: dict[str, asyncio.Task[Any]] = {}


def _set_state(name: str, status: str, error: str = "") -> None:
    setattr(app.state, f"{name}_status", status)
    setattr(app.state, f"{name}_error", (error or "")[:500])


async def _run_bot_runtime() -> None:
    _set_state("bot", "starting")
    try:
        # Import lazily: a bad/missing bot token must not make the Web Admin
        # disappear.  Instead the reason becomes visible in /health.
        from bot_app import bot, run_bot

        # Validate the token and outbound Telegram connectivity before declaring
        # the bot healthy. This makes /health trustworthy on Bothost.
        me = await bot.get_me()
        logger.info("Telegram Bot API connected: @%s (id=%s)", me.username or "-", me.id)
        _set_state("bot", "running")
        await run_bot()
        _set_state("bot", "stopped")
    except asyncio.CancelledError:
        _set_state("bot", "stopped")
        raise
    except Exception as exc:  # keep web diagnostics alive
        logger.exception("Telegram bot runtime failed")
        _set_state("bot", "error", f"{type(exc).__name__}: {exc}")


async def _run_telethon_runtime() -> None:
    import tenancy as t
    from db import list_tracked_chat_ids
    from telethon_config import load_telethon_config
    from telethon_sync import TelethonSyncService
    workers = {}

    async def run_one(cid):
        token = t.chat_context.set(cid)
        try:
            while t.tier(cid) >= 2:
                cfg = load_telethon_config()
                if not cfg['enabled'] or not cfg['configured'] or not cfg['has_session'] or cfg['setup_pending']:
                    await asyncio.sleep(5)
                    continue
                try:
                    await TelethonSyncService(cid).run_forever()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning('Telethon worker failed for chat %s', cid)
                    await asyncio.sleep(10)
        finally:
            t.chat_context.reset(token)

    _set_state('telethon', 'running')
    try:
        while True:
            desired = {cid for cid in list_tracked_chat_ids() if t.tier(cid) >= 2}
            for cid in list(workers):
                if cid not in desired or workers[cid].done():
                    workers[cid].cancel()
                    await asyncio.gather(workers.pop(cid), return_exceptions=True)
            for cid in desired:
                if cid not in workers:
                    workers[cid] = asyncio.create_task(run_one(cid))
            await asyncio.sleep(5)
    finally:
        for task in workers.values(): task.cancel()
        await asyncio.gather(*workers.values(), return_exceptions=True)


async def _runtime_startup() -> None:
    # FastAPI can be started by an ASGI server or by python main.py.  In either
    # case these handlers guarantee that the Telegram parts are also started.
    if _RUNTIME_TASKS:
        return
    logger.info("Starting Lorenzo runtime")
    logger.info("Python %s", platform.python_version())
    logger.info("Web Admin listening on 0.0.0.0:%s", web_port())
    logger.info("Persistent data dir: %s", DATA_DIR)
    logger.info("Database: %s", DATABASE_PATH)
    logger.info("Public URL detected: %s", web_public_url() or "not set")
    logger.info("OWNER_ID configured: %s", bool((os.getenv("OWNER_ID") or "").strip()))
    logger.info("BOT_TOKEN configured: %s", bool((os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("API_TOKEN") or "").strip()))
    logger.info("WEB_ADMIN_PASSWORD configured: %s", bool((os.getenv("WEB_ADMIN_PASSWORD") or "").strip()))
    _RUNTIME_TASKS["bot"] = asyncio.create_task(_run_bot_runtime(), name="lorenzo-bot")
    _RUNTIME_TASKS["telethon"] = asyncio.create_task(_run_telethon_runtime(), name="lorenzo-telethon")


async def _runtime_shutdown() -> None:
    tasks = list(_RUNTIME_TASKS.values())
    _RUNTIME_TASKS.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# Event handlers are used deliberately here instead of putting the lifecycle in
# a separate wrapper app.  That means ``main:app`` exposes the same routes as
# web_app.py and also starts the Telegram background services.
app.add_event_handler("startup", _runtime_startup)
app.add_event_handler("shutdown", _runtime_shutdown)


def run() -> None:
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = web_port()
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.getenv("WEB_LOG_LEVEL", "info"),
        access_log=True,
    )


if __name__ == "__main__":
    run()
