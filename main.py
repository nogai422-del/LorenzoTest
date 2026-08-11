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
from typing import Any

import uvicorn

from runtime_paths import web_port
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
        from bot_app import run_bot

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
    _set_state("telethon", "starting")
    try:
        from telethon_config import load_telethon_config
        from telethon_sync import TelethonSyncService

        last_status_log = 0.0
        _set_state("telethon", "waiting")
        while True:
            cfg = load_telethon_config()
            now = asyncio.get_running_loop().time()

            if not cfg.get("enabled"):
                _set_state("telethon", "disabled")
                if now - last_status_log >= 60:
                    logger.info("Telethon sync is disabled")
                    last_status_log = now
                await asyncio.sleep(5)
                continue

            if cfg.get("setup_pending"):
                _set_state("telethon", "setup")
                await asyncio.sleep(3)
                continue

            if not cfg.get("configured") or not cfg.get("has_session"):
                _set_state("telethon", "waiting")
                if now - last_status_log >= 60:
                    logger.info("Telethon awaits configuration/authorization in Web Admin")
                    last_status_log = now
                await asyncio.sleep(5)
                continue

            service = None
            try:
                _set_state("telethon", "running")
                service = TelethonSyncService()
                await service.run_forever()
            except asyncio.CancelledError:
                if service:
                    await service.stop()
                _set_state("telethon", "stopped")
                raise
            except Exception as exc:
                logger.exception("Telethon worker failed")
                _set_state("telethon", "error", f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(10)
    except asyncio.CancelledError:
        _set_state("telethon", "stopped")
        raise
    except Exception as exc:
        logger.exception("Telethon runtime failed before supervisor loop")
        _set_state("telethon", "error", f"{type(exc).__name__}: {exc}")


async def _runtime_startup() -> None:
    # FastAPI can be started by an ASGI server or by python main.py.  In either
    # case these handlers guarantee that the Telegram parts are also started.
    if _RUNTIME_TASKS:
        return
    logger.info("Starting Lorenzo runtime")
    logger.info("Web Admin listening on 0.0.0.0:%s", web_port())
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
