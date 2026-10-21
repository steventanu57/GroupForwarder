# Lists every chat you're in with its ID (the numbers that go into .env), with its own login,
# so it also works while group_forwarder.py runs. Needs only API_ID and API_HASH in .env.
import getpass
import os
import sys
import unicodedata
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
    raise SystemExit(
        "Missing API_ID or API_HASH in .env: get them at https://my.telegram.org/apps "
        "(log in, open 'API development tools' and create an app). See .env.example."
    )
if not api_id.isdigit():
    raise SystemExit(
        "API_ID in .env must be only digits: the api_id from my.telegram.org/apps"
    )

client = TelegramClient(str(BASE_DIR / "groupid"), int(api_id), api_hash)


def clean(text, limit=60):
    # Chat names are set by other people: no control codes or line breaks in your terminal
    text = " ".join(
        "".join(
            " " if unicodedata.category(c) == "Cc" else c for c in str(text)
        ).split()
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def main():
    print("Fetching all dialogs...\n")
    async for dialog in client.iter_dialogs():
        print(f"{clean(dialog.name)}: {dialog.id}")
    print(
        "\nPut the IDs you need in .env: SOURCE_GROUP_IDS (forward from), TARGET_GROUP_ID (to)."
    )


# The first run asks for your login in the terminal; later runs use groupid.session
client.start(
    phone=lambda: input(
        "Your phone number, with the country code (e.g. +44 7700 900123): "
    ),
    code_callback=lambda: input(
        "The login code Telegram just sent you (look in your Telegram app): "
    ),
    password=lambda: getpass.getpass(
        "Your Telegram two-step verification password (it isn't shown as you type): "
    ),
)
try:
    client.loop.run_until_complete(main())
finally:
    client.disconnect()  # With no event loop running, Telethon finishes this by itself
