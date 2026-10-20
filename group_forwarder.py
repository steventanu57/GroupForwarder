# Run on python 3.12
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, errors, events, utils

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


def parse_chat_id(name, value):
    # Chat IDs are numbers like -1001234567890, so a typo or a @username gets a clear message
    try:
        return int(value)
    except ValueError:
        raise SystemExit(
            f"{name} has '{value}', which is not a chat ID. Use the numeric IDs printed by get_groupId.py"
        ) from None


api_id = int(get_setting("API_ID"))  # Your own API id from my.telegram.org
api_hash = get_setting("API_HASH")  # Your own API hash from my.telegram.org

source_group_id = [  # Source group IDs, comma-separated in .env
    parse_chat_id("SOURCE_GROUP_IDS", group_id.strip())
    for group_id in get_setting("SOURCE_GROUP_IDS").split(",")
    if group_id.strip()
]
if not source_group_id:
    raise SystemExit("SOURCE_GROUP_IDS has no chat IDs (see .env.example)")
target_group_id = parse_chat_id(
    "TARGET_GROUP_ID", get_setting("TARGET_GROUP_ID")
)  # Your own group ID

if target_group_id in source_group_id:
    raise SystemExit(
        "TARGET_GROUP_ID is also in SOURCE_GROUP_IDS, which would forward messages in an endless loop"
    )

# ======== SETUP ========
# connection_retries=None: after an internet outage, keep trying to reconnect (every 5 s)
# instead of giving up after 5 tries and stopping the script
client = TelegramClient(
    str(BASE_DIR / "group_forwarder"),
    api_id,
    api_hash,
    connection_retries=None,
    retry_delay=5,
)

chat_names = {}  # Chat ID -> chat name, filled from your chat list on startup


def now():
    return time.strftime("%H:%M:%S")  # Local time, for the log


def chat_name(chat_id):
    return chat_names.get(chat_id, str(chat_id))


def sender_name(event):
    # event.sender is what Telegram sent along with the message, so this makes no extra request
    # (event.get_sender() would ask Telegram again whenever it only has partial details)
    if event.sender is None:
        return "Unknown"
    return utils.get_display_name(event.sender) or "Unknown"


async def forward(what, event, send):
    # send() does the actual forwarding. A failed forward is reported and the forwarder keeps running.
    source = chat_name(event.chat_id)
    try:
        try:
            await send()
        except errors.FloodWaitError as e:
            # Telethon already waits out short waits (up to 60 s) by itself; this handles longer ones
            print(
                f"[{now()}] Telegram asked to wait {e.seconds} s before forwarding from {source}. Waiting, then retrying once"
            )
            await asyncio.sleep(e.seconds)
            await send()
    except errors.ChatForwardsRestrictedError:
        print(
            f"[{now()}] Could not forward {what} from {source}: that chat restricts forwarding"
        )
        return
    except (errors.RPCError, ConnectionError, ValueError) as e:
        # Any other Telegram or connection problem (anything unexpected is still logged by Telethon)
        print(
            f"[{now()}] Could not forward {what} from {source}: {type(e).__name__}: {e}"
        )
        return

    print(
        f"[{now()}] {source} -> {chat_name(target_group_id)}: {what} from {sender_name(event)}"
    )


# ======== HANDLERS ========
@client.on(events.NewMessage(chats=source_group_id))
async def handler(event):
    if (
        event.grouped_id
    ):  # Part of an album: album_handler forwards the whole album at once
        return

    await forward(
        "message",
        event,
        lambda: client.forward_messages(target_group_id, event.message),
    )


@client.on(events.Album(chats=source_group_id))
async def album_handler(event):
    await forward(
        f"album of {len(event)} items", event, lambda: event.forward_to(target_group_id)
    )


# ======== RUN ========
client.start()

# A fresh login has no chats cached yet, so forwarding would fail with "Could not find the input entity".
# Loading the dialogs fills the cache (and gives us the chat names for the log); then check the chats are really reachable.
dialogs = client.loop.run_until_complete(client.get_dialogs())
chat_names.update({dialog.id: dialog.name for dialog in dialogs})
try:
    client.loop.run_until_complete(client.get_input_entity(target_group_id))
except ValueError:
    raise SystemExit(
        f"TARGET_GROUP_ID {target_group_id} not found. Is this account a member of that group?"
    )

# A source with a typo, or a chat this account has left, would otherwise be ignored without a word.
# Check against the chat list: the .session file also remembers chats you have left,
# so looking a chat up by ID still works even though Telegram no longer sends its messages.
reachable_sources = []
for chat_id in source_group_id:
    if chat_id in chat_names:
        reachable_sources.append(chat_id)
    else:
        print(
            f"Warning: source chat {chat_id} isn't in your chat list (left, removed or deleted?). Nothing will be forwarded from it."
        )
if not reachable_sources:
    raise SystemExit(
        "None of the chats in SOURCE_GROUP_IDS are in your chat list. Is this account a member of them?"
    )

print(
    f"Forwarder is running: listening to {len(reachable_sources)} chat(s), forwarding to {chat_name(target_group_id)}"
)
for chat_id in reachable_sources:
    print(f"  - {chat_name(chat_id)}")

# ======== UNTIL STOPPED ========
client.run_until_disconnected()
