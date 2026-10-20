import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

# Names with emoji would crash print() when output goes to a file (python get_groupId.py > chats.txt) on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Only API_ID and API_HASH are needed here, so run this before filling in the group IDs
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

api_id = os.getenv("API_ID", "").strip()  # your api_id, set in .env
api_hash = os.getenv("API_HASH", "").strip()  # your api_hash, set in .env
if not api_id or not api_hash:
    raise SystemExit("Missing API_ID or API_HASH in .env (see .env.example)")

client = TelegramClient(str(BASE_DIR / "groupid"), int(api_id), api_hash)


async def main():
    print("Fetching all dialogs...\n")
    async for dialog in client.iter_dialogs():
        print(f"{dialog.name}: {dialog.id}")


with client:
    client.loop.run_until_complete(main())
