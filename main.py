"""Bothost-friendly entrypoint.

Bothost auto-detects main.py, so this launcher starts all Lorenzo components:
Telegram Bot API polling, Web Admin and the Telethon synchronization worker.
"""
from __future__ import annotations

import asyncio

from app import main as run_application


if __name__ == "__main__":
    asyncio.run(run_application())
