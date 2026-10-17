# Run on python 3.12
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events, types

# Names with emoji would crash print() when output goes to a log file on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ======== CONFIG ========
# Personal settings are read from .env (copy .env.example to .env and fill it in)
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

def get_setting(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing {name} in .env (see .env.example)")
    return value

api_id = int(get_setting("API_ID"))     # Your own API id from my.telegram.org
api_hash = get_setting("API_HASH")      # Your own API hash from my.telegram.org

source_group_id = [     # Source group IDs, comma-separated in .env
    int(group_id) for group_id in get_setting("SOURCE_GROUP_IDS").split(",") if group_id.strip()
    ]
target_group_id = int(get_setting("TARGET_GROUP_ID"))      # Your own group ID

if target_group_id in source_group_id:
    raise SystemExit("TARGET_GROUP_ID is also in SOURCE_GROUP_IDS, which would forward messages in an endless loop")

# ======== SETUP ========
client = TelegramClient(str(BASE_DIR / 'group_forwarder'), api_id, api_hash)

async def sender_name(event):
    # Safely get sender name
    sender = await event.get_sender()
    if isinstance(sender, types.User):
        return sender.first_name
    elif isinstance(sender, types.Channel):
        return sender.title
    return "Unknown"

# ======== HANDLERS ========
@client.on(events.NewMessage(chats=source_group_id))
async def handler(event):
    if event.grouped_id:  # Part of an album: album_handler forwards the whole album at once
        return

    await client.forward_messages(target_group_id, event.message)
    print(f"Forwarded message from {await sender_name(event)}")

@client.on(events.Album(chats=source_group_id))
async def album_handler(event):
    await event.forward_to(target_group_id)
    print(f"Forwarded an album of {len(event)} items from {await sender_name(event)}")

# ======== RUN ========
print("Forwarder is running...")
client.start()

# A fresh login has no chats cached yet, so forwarding would fail with "Could not find the input entity".
# Loading the dialogs fills the cache; then check the target group is really reachable.
client.loop.run_until_complete(client.get_dialogs())
try:
    client.loop.run_until_complete(client.get_input_entity(target_group_id))
except ValueError:
    raise SystemExit(f"TARGET_GROUP_ID {target_group_id} not found. Is this account a member of that group?")

# ======== UNTIL STOPPED ========
client.run_until_disconnected()