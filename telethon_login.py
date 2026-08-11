"""Fallback interactive Telethon authorization for local/recovery use.

On Bothost use Web Admin -> Telethon. The hosting container has no interactive
terminal, so this script is mainly for local recovery/testing.
"""
from __future__ import annotations

import asyncio
import time

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from telethon_config import load_telethon_config, save_telethon_config

load_dotenv()


async def main() -> None:
    config = load_telethon_config()
    api_id = config.get("api_id")
    api_hash = config.get("api_hash")
    if not api_id or not api_hash:
        raise RuntimeError("Configure API ID/API Hash in Web Admin -> Telethon or environment first")

    client = TelegramClient(
        StringSession(str(config.get("session_string") or "")),
        int(api_id),
        str(api_hash),
    )
    await client.start()
    me = await client.get_me()
    first = (getattr(me, "first_name", None) or "").strip()
    last = (getattr(me, "last_name", None) or "").strip()
    display_name = " ".join(x for x in (first, last) if x).strip() or str(me.id)
    save_telethon_config(
        enabled=True,
        setup_pending=False,
        setup_started_at=0,
        session_string=client.session.save(),
        account={
            "id": int(me.id),
            "display_name": display_name,
            "username": getattr(me, "username", None),
            "authorized_at": int(time.time()),
        },
    )
    print(f"Telethon StringSession authorized for user_id={me.id}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
