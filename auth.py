import asyncio
import os
import sys
from pathlib import Path
from telethon import TelegramClient

from config import (
    BASE_DIR,
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    TELEGRAM_SESSION_NAME,
)


async def authenticate():
    print("=" * 65)
    print("  Telegram MT5 Copier - One-Time Telegram Authorization")
    print("=" * 65)
    print(f"Using App api_id   : {TELEGRAM_API_ID}")
    print(f"Using App api_hash : {TELEGRAM_API_HASH}")
    print("-" * 65)

    if TELEGRAM_API_ID in (0, 12345678):
        print("\n[!] WARNING: TELEGRAM_API_ID is set to placeholder (12345678).")
        print("    Please ensure you set your numeric App api_id in .env from https://my.telegram.org\n")

    session_path = str(BASE_DIR / TELEGRAM_SESSION_NAME)
    client = TelegramClient(session_path, TELEGRAM_API_ID, TELEGRAM_API_HASH)

    try:
        await client.start()
        me = await client.get_me()
        print("\n" + "=" * 65)
        print(f"  SUCCESS! Authenticated as: {me.first_name} {me.last_name or ''} (@{me.username or me.phone})")
        print(f"  Session saved to: {session_path}.session")
        print("  You can now start the bot using: python main.py")
        print("=" * 65)
    except Exception as e:
        print(f"\n[X] Authentication failed: {type(e).__name__}: {e}")
        print("    Make sure your TELEGRAM_API_ID (numeric) and TELEGRAM_API_HASH match on https://my.telegram.org")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(authenticate())
    except (KeyboardInterrupt, SystemExit):
        print("\nAuthentication cancelled.")
