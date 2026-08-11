from __future__ import annotations

import asyncio
import logging
import os

import uvicorn
from dotenv import load_dotenv

from bot_app import run_bot
from telethon_sync import TelethonSyncService
from telethon_config import load_telethon_config
from web_app import app as web_app
from runtime_paths import web_port

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("lorenzo_app")


async def run_web() -> None:
    if os.getenv("WEB_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        logger.info("Web admin disabled by WEB_ENABLED")
        return
    config = uvicorn.Config(
        web_app,
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=web_port(),
        log_level=os.getenv("WEB_LOG_LEVEL", "info"),
        access_log=True,
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run_telethon_supervisor() -> None:
    last_status_log = 0.0
    while True:
        cfg = load_telethon_config()
        now = asyncio.get_running_loop().time()
        if not cfg["enabled"]:
            if now - last_status_log >= 60:
                logger.info("Telethon sync is disabled")
                last_status_log = now
            await asyncio.sleep(5)
            continue
        if cfg.get("setup_pending"):
            if now - last_status_log >= 60:
                logger.info("Telethon authorization wizard is active")
                last_status_log = now
            await asyncio.sleep(3)
            continue
        if not cfg.get("configured") or not cfg.get("has_session"):
            if now - last_status_log >= 60:
                logger.info("Telethon awaits configuration/authorization in Web Admin")
                last_status_log = now
            await asyncio.sleep(5)
            continue

        service = None
        try:
            service = TelethonSyncService()
            await service.run_forever()
        except asyncio.CancelledError:
            if service:
                await service.stop()
            raise
        except Exception as exc:
            logger.error("Telethon worker is not running: %s", exc)
            logger.info("Retrying Telethon worker in 10 seconds")
            await asyncio.sleep(10)


async def main() -> None:
    tasks = [asyncio.create_task(run_bot(), name="bot")]
    if os.getenv("WEB_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}:
        tasks.append(asyncio.create_task(run_web(), name="web"))
    tasks.append(asyncio.create_task(run_telethon_supervisor(), name="telethon"))

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for task in done:
        exc = task.exception()
        if exc:
            logger.error("Task %s failed: %s", task.get_name(), exc)
            for other in pending:
                other.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise exc


if __name__ == "__main__":
    asyncio.run(main())
