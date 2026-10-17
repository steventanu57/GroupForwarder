# Run on python 3.12
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events, types

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

# ======== SETUP ========
client = TelegramClient(str(BASE_DIR / 'group_forwarder'), api_id, api_hash)

# ======== HANDLER ========
@client.on(events.NewMessage(chats=source_group_id))
async def handler(event):
    sender = await event.get_sender()

    await client.forward_messages(target_group_id, event.message)

    # Safely get sender name
    if isinstance(sender, types.User):
        name = sender.first_name
    elif isinstance(sender, types.Channel):
        name = sender.title
    else:
        name = "Unknown"
        
    print(f"Forwarded message from {name}")

# ======== RUN ========
print(f"Forwarder is running...")
client.start()

# ======== UNTIL STOPPED ========
client.run_until_disconnected()