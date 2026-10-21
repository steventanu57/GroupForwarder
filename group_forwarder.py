# Run on python 3.12 or 3.13
#
# HOW TO CONFIGURE (all files sit next to this script; restart the forwarder after a change)
#   .env                   Required: your login (API_ID, API_HASH from https://my.telegram.org/apps)
#                          and the chats: SOURCE_GROUP_IDS (forward from), TARGET_GROUP_ID (forward to).
#                          Copy .env.example to .env: it explains every value and where to find it.
#   forwarder_config.toml  Optional: filters, more routes, forum topics, copy mode, catch-up, ...
#                          Copy forwarder_config.example.toml: every setting is explained there.
#   Command-line options   --list-chats, --no-login, --log FILE (see "Command-line options" below).
# Chat IDs look like -1001234567890. See yours with "python group_forwarder.py --list-chats", or send
# /chats to your Saved Messages while the forwarder runs (/topics <chat> shows forum topic numbers).
# DEFAULT_OPTIONS and DEFAULT_FILTERS below are only the defaults: change settings in
# forwarder_config.toml, not in this file.
import asyncio
import getpass
import hashlib
import html
import json
import os
import sys
import time
import unicodedata
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, errors, events, functions, utils
from telethon.extensions import html as telegram_html
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.tl.types import MessageMediaWebPage

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# Names with emoji would crash print() when output goes to a log file on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ======== CONFIG ========
# Personal settings are read from .env (copy .env.example to .env and fill it in).
# Optional extra settings (filters, routes, copy mode, ...) are read from forwarder_config.toml.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

CONFIG_FILE = BASE_DIR / "forwarder_config.toml"
STATE_FILE = (
    BASE_DIR / "forwarder_state.json"
)  # Catch-up bookmarks: last message seen per source chat
LOCK_FILE = (
    BASE_DIR / "group_forwarder.lock"
)  # Held while running, so only one copy uses the login

# Command-line options
#   --list-chats   print your chats and their IDs, then stop (no second login needed)
#   --no-login     never ask for a phone number; stop instead (used by run_forwarder.ps1)
#   --log FILE     write everything the forwarder prints to FILE (used by run_forwarder.ps1)
ARGS = sys.argv[1:]
LIST_CHATS = "--list-chats" in ARGS
NO_LOGIN = "--no-login" in ARGS
LOG_FILE = None
for position, arg in enumerate(ARGS):
    if arg == "--log":
        if position + 1 >= len(ARGS):
            raise SystemExit("--log needs a file name, e.g. --log forwarder.log")
        LOG_FILE = ARGS[position + 1]
    elif arg not in ("--list-chats", "--no-login", "--log") and (
        position == 0 or ARGS[position - 1] != "--log"
    ):
        raise SystemExit(
            f"Unknown option {arg}. Options: --list-chats, --no-login, --log FILE"
        )
if LOG_FILE:
    # Line by line, so the log is up to date even if the forwarder is stopped suddenly
    sys.stdout = sys.stderr = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # noqa: SIM115


# What each .env setting is and where to get it, for the message when one is missing
SETTING_HELP = {
    "API_ID": "the api_id number from https://my.telegram.org/apps (log in with your phone "
    "number, open 'API development tools' and create an app; any name works)",
    "API_HASH": "the api_hash code from https://my.telegram.org/apps, next to the api_id",
    "SOURCE_GROUP_IDS": "the chats to forward from, comma-separated, like "
    "-1001234567890,-1009876543210. To see your chats and their IDs, run: "
    "python group_forwarder.py --list-chats",
    "TARGET_GROUP_ID": "the group that receives the messages, like -1001234567890. "
    "To see your chats and their IDs, run: python group_forwarder.py --list-chats",
}


def get_setting(name):
    value = os.getenv(name, "").strip()
    if not value:
        if not (BASE_DIR / ".env").exists():
            raise SystemExit(
                f"There is no .env file next to {Path(__file__).name}: copy .env.example "
                "to .env and fill it in (it explains every value)."
            )
        raise SystemExit(
            f"Missing {name} in .env: {SETTING_HELP[name]}. See .env.example."
        )
    return value


def parse_chat_id(name, value):
    # Chat IDs are numbers like -1001234567890, so a typo or a @username gets a clear message
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        raise SystemExit(
            f"{name} has '{value}', which is not a chat ID. Chat IDs are numbers: groups and "
            "channels look like -1001234567890. To see yours, run: python group_forwarder.py "
            "--list-chats (or get_groupId.py), or send /chats to your Saved Messages while "
            "the forwarder runs."
        ) from None


# Every setting the config file may contain, with the value used when it's left out.
# The defaults keep the forwarder exactly as it is without a config file.
DEFAULT_OPTIONS = {
    "skip_own_messages": False,  # Don't forward what you post yourself in a source chat
    "catch_up": False,  # On start, forward what was missed while the forwarder was off
    "catch_up_hours": 6,  # ... from this many hours back at most
    "catch_up_max_per_chat": 20,  # ... and at most this many messages per chat
    "strict_order": False,  # Handle messages one at a time, in the order they arrive
    "copy_mode": False,  # Send a copy with the source chat's name on top, instead of "Forwarded from"
    "skip_duplicates": False,  # Don't forward the same post twice to the same target
    "duplicate_window_hours": 24,  # ... within this many hours
    "commands": True,  # /status, /pause, /resume, /sources and /help in your Saved Messages
    "daily_summary": False,  # A daily "still running" message in your Saved Messages
    "daily_summary_time": "09:00",  # ... at this local time (24-hour clock)
}
DEFAULT_FILTERS = {
    "include_keywords": [],  # Only forward messages containing at least one of these
    "exclude_keywords": [],  # Never forward messages containing any of these
    "media_only": False,  # Only forward messages with a photo, video, file, ...
    "skip_bots": False,  # Don't forward messages sent by bots
    "skip_reposts": False,  # Don't forward messages that are themselves forwarded from elsewhere
}


def describe_type(default):
    if isinstance(default, bool):
        return "true or false"
    if isinstance(default, int):
        return "a whole number above 0"
    if isinstance(default, list):
        return 'a list of words in quotes, like ["btc", "eth"]'
    return "text in quotes"


def check_settings(where, values, defaults):
    """Checks one section of the config file. A typo or a wrong kind of value stops the
    forwarder with a clear message, instead of being silently ignored."""
    if not isinstance(values, dict):
        raise SystemExit(f"[{where}] in {CONFIG_FILE.name} must be a section")
    for key, value in values.items():
        if key not in defaults:
            raise SystemExit(
                f"Unknown setting '{key}' in [{where}] of {CONFIG_FILE.name}. Known settings: {', '.join(defaults)}"
            )
        default = defaults[key]
        if isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, int):
            valid = isinstance(value, int) and not isinstance(value, bool) and value > 0
        elif isinstance(default, list):
            valid = isinstance(value, list) and all(
                isinstance(word, str) and word.strip() for word in value
            )
        else:
            valid = isinstance(value, str)
        if not valid:
            raise SystemExit(
                f"'{key}' in [{where}] of {CONFIG_FILE.name} must be {describe_type(default)}, not {value!r}"
            )
    return {**defaults, **values}


def read_config():
    """The optional config file as a dict ({} when there is none)."""
    if not CONFIG_FILE.exists():
        return {}
    import tomllib  # Python's own TOML reader; only needed when there is a config file

    try:
        config = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SystemExit(f"{CONFIG_FILE.name} has a mistake: {e}") from None
    unknown = set(config) - {"options", "filters", "routes"}
    if unknown:
        raise SystemExit(
            f"Unknown section {', '.join(sorted(unknown))} in {CONFIG_FILE.name}. Sections: [options], [filters], [[routes]]"
        )
    return config


def read_routes(config, filters, default_route):
    """The .env route (SOURCE_GROUP_IDS -> TARGET_GROUP_ID) plus any [[routes]] in the config file."""
    routes = [default_route]
    extra = config.get("routes", [])
    if not isinstance(extra, list):
        raise SystemExit(f"Routes in {CONFIG_FILE.name} must be written as [[routes]]")
    for number, route in enumerate(extra, 1):
        where = f"routes #{number}"
        if not isinstance(route, dict):
            raise SystemExit(
                f"[[routes]] #{number} in {CONFIG_FILE.name} must be a section"
            )
        allowed = {"sources", "target", "topic", *DEFAULT_FILTERS}
        unknown = set(route) - allowed
        if unknown:
            raise SystemExit(
                f"Unknown setting '{min(unknown)}' in [[routes]] #{number} of {CONFIG_FILE.name}. Known settings: {', '.join(sorted(allowed))}"
            )
        sources = route.get("sources")
        if not isinstance(sources, list) or not sources or "target" not in route:
            raise SystemExit(
                f"[[routes]] #{number} in {CONFIG_FILE.name} needs sources = [chat IDs] and target = a chat ID"
            )
        topic = route.get("topic")
        if topic is not None and (
            not isinstance(topic, int) or isinstance(topic, bool) or topic <= 0
        ):
            raise SystemExit(
                f"topic in [[routes]] #{number} of {CONFIG_FILE.name} must be a forum topic's number"
            )
        own_filters = check_settings(
            where,
            {k: v for k, v in route.items() if k in DEFAULT_FILTERS},
            DEFAULT_FILTERS,
        )
        routes.append(
            {
                "name": f"[[routes]] #{number}",
                "sources": [parse_chat_id(f"sources in {where}", s) for s in sources],
                "target": parse_chat_id(f"target in {where}", route["target"]),
                "topic": topic,
                # A route's own filters replace the [filters] section, one setting at a time
                "filters": {
                    **filters,
                    **{k: own_filters[k] for k in DEFAULT_FILTERS if k in route},
                },
            }
        )
    return routes


api_id = get_setting("API_ID")  # Your own API id from my.telegram.org
if not api_id.isdigit():
    raise SystemExit(
        "API_ID in .env must be only digits: the api_id number from "
        "https://my.telegram.org/apps (API_HASH is the long code next to it)."
    )
api_id = int(api_id)
api_hash = get_setting("API_HASH")  # Your own API hash from my.telegram.org

if LIST_CHATS:  # Listing your chats needs only the login, so it also works before the IDs are filled in
    config, OPTIONS, routes = {}, DEFAULT_OPTIONS, []
else:
    config = read_config()
    OPTIONS = check_settings("options", config.get("options", {}), DEFAULT_OPTIONS)
    FILTERS = check_settings("filters", config.get("filters", {}), DEFAULT_FILTERS)
    hour, _, minute = OPTIONS["daily_summary_time"].partition(":")
    if not (
        hour.isdigit() and minute.isdigit() and int(hour) < 24 and int(minute) < 60
    ):
        raise SystemExit(
            f'daily_summary_time in {CONFIG_FILE.name} must be a time like "09:00"'
        )

    source_group_ids = [  # Source group IDs, comma-separated in .env
        parse_chat_id("SOURCE_GROUP_IDS", group_id.strip())
        for group_id in get_setting("SOURCE_GROUP_IDS").split(",")
        if group_id.strip()
    ]
    if not source_group_ids:
        raise SystemExit("SOURCE_GROUP_IDS has no chat IDs (see .env.example)")
    target_group_id = parse_chat_id(
        "TARGET_GROUP_ID", get_setting("TARGET_GROUP_ID")
    )  # Your own group ID

    if target_group_id in source_group_ids:
        raise SystemExit(
            "TARGET_GROUP_ID is also in SOURCE_GROUP_IDS, which would forward messages in an endless loop"
        )
    routes = read_routes(
        config,
        FILTERS,
        {
            "name": "TARGET_GROUP_ID",
            "sources": source_group_ids,
            "target": target_group_id,
            "topic": None,
            "filters": FILTERS,
        },
    )
    # Our own forwards are new messages in the target. If a target were also a source,
    # they would be forwarded again and again.
    all_sources = {chat for route in routes for chat in route["sources"]}
    for route in routes:
        if route["target"] in all_sources:
            raise SystemExit(
                f"Chat {route['target']} ({route['name']}) is both a source and a target, which would forward messages in an endless loop"
            )

# Every chat any route listens to, in the order they were written
SOURCES = list(dict.fromkeys(chat for route in routes for chat in route["sources"]))

# ======== SETUP ========
# connection_retries=None: after an internet outage, keep trying to reconnect (every 5 s)
# instead of giving up after 5 tries and stopping the script.
# sequential_updates=True (strict_order): handle one message at a time, in arrival order.
client = TelegramClient(
    str(BASE_DIR / "group_forwarder"),
    api_id,
    api_hash,
    connection_retries=None,
    retry_delay=5,
    sequential_updates=OPTIONS["strict_order"],
)

chat_names = {}  # Chat ID -> chat name, filled from your chat list on startup
chat_entities = {}  # Chat ID -> chat, to see "Restrict saving content"
status = {"paused": False, "started": time.time(), "last_forward": None}
COUNTERS = ("forwarded", "filtered", "duplicates", "failed", "paused")
totals = dict.fromkeys(COUNTERS, 0)  # Since the forwarder started (for /status)
period = dict.fromkeys(COUNTERS, 0)  # Since the last daily summary
failures = deque(
    maxlen=20
)  # The latest failed forwards: (time, source, target, reason)
seen_posts = {}  # Target -> {post fingerprint: time forwarded}, for skip_duplicates
handled = deque(
    maxlen=1000
)  # (chat, message ID) already handled, so catch-up never sends twice
bookmarks = {}  # Chat ID (text) -> last message ID seen, for catch-up


def now():
    return time.strftime("%H:%M:%S")  # Local time, for the log


# Characters that turn text around on screen (could make a log line read differently)
DIRECTION_MARKS = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def clean(text, limit=60):
    """Text set by other people (chat titles, names), made safe to show: control codes and line
    breaks become spaces, so nobody can change your terminal or fake a line of the log, and it's
    shortened to `limit` characters."""
    text = "".join(
        " " if unicodedata.category(c) == "Cc" or c in DIRECTION_MARKS else c
        for c in str(text)
    )
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chat_name(chat_id):
    return chat_names.get(chat_id, str(chat_id))


def sender_name(sender):
    # The sender Telegram sent along with the message, so this makes no extra request
    # (event.get_sender() would ask Telegram again whenever it only has partial details)
    if sender is None:
        return "Unknown"
    return clean(utils.get_display_name(sender)) or "Unknown"


def count(what):
    totals[what] += 1
    period[what] += 1


class Incoming:
    """One message or album from a source chat: arrived live, or caught up after a restart."""

    def __init__(self, chat_id, messages, sender, album_event=None):
        self.chat_id, self.messages, self.sender = chat_id, messages, sender
        self.album_event = album_event  # The live Album event, which can forward itself

    @property
    def is_album(self):
        return self.album_event is not None or len(self.messages) > 1

    def describe(self):
        return f"album of {len(self.messages)} items" if self.is_album else "message"


# ======== FILTERS AND DUPLICATES ========
def has_media(message):
    return message.media is not None and not isinstance(
        message.media, MessageMediaWebPage
    )


def filtered_out(incoming, rules):
    """Why a route's filters skip this message, or None to forward it."""
    if rules == DEFAULT_FILTERS:
        return None  # No filters set
    text = " ".join(message.message or "" for message in incoming.messages).lower()
    if rules["skip_bots"] and getattr(incoming.sender, "bot", False):
        return "sent by a bot"
    if rules["skip_reposts"] and any(m.fwd_from for m in incoming.messages):
        return "a repost"
    if rules["media_only"] and not any(has_media(m) for m in incoming.messages):
        return "no media"
    if rules["include_keywords"] and not any(
        word.lower() in text for word in rules["include_keywords"]
    ):
        return "no wanted keyword"
    if any(word.lower() in text for word in rules["exclude_keywords"]):
        return "an unwanted keyword"
    return None


def fingerprint(incoming):
    """Something that is the same for the same post seen in two chats, or None if unknown."""
    parts = []
    for message in incoming.messages:
        repost = message.fwd_from
        if repost and repost.from_id and repost.channel_post:
            # The same channel post, forwarded into several source chats
            parts.append(
                f"post:{utils.get_peer_id(repost.from_id)}:{repost.channel_post}"
            )
            continue
        media = message.media
        media_id = getattr(getattr(media, "photo", None), "id", None) or getattr(
            getattr(media, "document", None), "id", None
        )
        text = (message.message or "").strip()
        if not text and media_id is None:
            return None  # Nothing to compare (e.g. a poll)
        parts.append(f"{hashlib.sha256(text.encode()).hexdigest()}:{media_id}")
    return "|".join(parts)


def already_sent(key, target):
    if key is None:
        return False
    sent = seen_posts.setdefault(target, {})
    cutoff = time.time() - OPTIONS["duplicate_window_hours"] * 3600
    for old in [k for k, when in sent.items() if when < cutoff]:
        del sent[old]
    return key in sent


# ======== FORWARDING ========
def is_protected(incoming):
    # "Restrict saving content": never copied, whatever the settings
    return any(getattr(m, "noforwards", False) for m in incoming.messages) or getattr(
        chat_entities.get(incoming.chat_id), "noforwards", False
    )


async def forward_message(incoming, target, topic):
    if topic:
        # Telethon's forward_messages can't post into a forum topic, so use Telegram's request directly
        await client(
            ForwardMessagesRequest(
                from_peer=await client.get_input_entity(incoming.chat_id),
                id=[message.id for message in incoming.messages],
                to_peer=await client.get_input_entity(target),
                top_msg_id=topic,
            )
        )
    elif incoming.album_event is not None:
        await incoming.album_event.forward_to(target)
    elif incoming.is_album:
        await client.forward_messages(target, incoming.messages)
    else:
        await client.forward_messages(target, incoming.messages[0])


async def copy_message(incoming, target, topic):
    # A copy with the source chat's name on top, instead of Telegram's "Forwarded from" header.
    # Text formatting is kept by turning it into HTML and back.
    header = f"📣 <b>{html.escape(chat_name(incoming.chat_id))}</b>"
    captions = [
        telegram_html.unparse(m.message or "", m.entities or [])
        for m in incoming.messages
    ]
    first = header + (f"\n\n{captions[0]}" if captions[0] else "")
    if incoming.is_album:
        await client.send_file(
            target,
            [message.media for message in incoming.messages],
            caption=[first, *captions[1:]],
            parse_mode="html",
            reply_to=topic,
        )
        return
    message = incoming.messages[0]
    web_page = isinstance(message.media, MessageMediaWebPage)
    await client.send_message(
        target,
        first,
        file=None if web_page else message.media,
        parse_mode="html",
        link_preview=web_page,
        reply_to=topic,
    )


async def attempt(send, source):
    # One try, and one more after a long wait that Telegram asks for
    try:
        await send()
    except errors.FloodWaitError as e:
        # Telethon already waits out short waits (up to 60 s) by itself; this handles longer ones
        print(
            f"[{now()}] Telegram asked to wait {e.seconds} s before forwarding from {source}. Waiting, then retrying once"
        )
        await asyncio.sleep(e.seconds)
        await send()


# The target itself refuses the post (no rights, banned, removed, topic deleted):
# forwarding instead of copying would fail the same way
TARGET_REFUSED = (
    errors.ChatWriteForbiddenError,
    errors.ChatAdminRequiredError,
    errors.ChannelPrivateError,
    errors.UserBannedInChannelError,
    errors.ChatRestrictedError,
    errors.ChatGuestSendForbiddenError,
    errors.TopicDeletedError,
)


def report_failure(what, source, target, reason):
    count("failed")
    failures.append((now(), source, chat_name(target), reason))
    print(f"[{now()}] Could not forward {what} from {source}: {reason}")


async def deliver(incoming, route, what):
    """Forwards (or copies) to one route's target. A failure is reported and the forwarder keeps running."""
    source, target, topic = chat_name(incoming.chat_id), route["target"], route["topic"]
    note = ""
    try:
        if OPTIONS["copy_mode"]:
            if is_protected(incoming):
                raise errors.ChatForwardsRestrictedError(request=None)
            try:
                await attempt(lambda: copy_message(incoming, target, topic), source)
                note = " (copied)"
            except (errors.ChatForwardsRestrictedError, *TARGET_REFUSED):
                raise
            except (errors.RPCError, TypeError, ValueError) as e:
                # Some messages can't be copied (e.g. a caption too long with the header): forward instead
                print(
                    f"[{now()}] Couldn't copy {what} from {source} ({type(e).__name__}), forwarding it instead"
                )
                await attempt(lambda: forward_message(incoming, target, topic), source)
        else:
            await attempt(lambda: forward_message(incoming, target, topic), source)
    except errors.ChatForwardsRestrictedError:
        report_failure(what, source, target, "that chat restricts forwarding")
        return False
    except (errors.RPCError, ConnectionError, ValueError) as e:
        # Any other Telegram or connection problem (anything unexpected is still logged by Telethon)
        report_failure(what, source, target, f"{type(e).__name__}: {e}")
        return False

    count("forwarded")
    status["last_forward"] = time.time()
    print(
        f"[{now()}] {source} -> {chat_name(target)}: {what} from {sender_name(incoming.sender)}{note}"
    )
    return True


async def handle(incoming, caught_up=False):
    """Sends one message or album along every route that listens to its chat."""
    if OPTIONS["catch_up"]:
        keys = [(incoming.chat_id, message.id) for message in incoming.messages]
        if any(key in handled for key in keys):
            return  # Already sent (it arrived live while catch-up was running)
        handled.extend(keys)
        remember_position(incoming.chat_id, max(m.id for m in incoming.messages))
    if OPTIONS["skip_own_messages"] and any(
        getattr(m, "out", False) for m in incoming.messages
    ):
        return
    if status["paused"]:
        count("paused")
        return

    what = ("missed " if caught_up else "") + incoming.describe()
    for route in routes:
        if incoming.chat_id not in route["sources"]:
            continue
        if filtered_out(incoming, route["filters"]):
            count("filtered")
            continue
        key = fingerprint(incoming) if OPTIONS["skip_duplicates"] else None
        if already_sent(key, route["target"]):
            count("duplicates")
            print(
                f"[{now()}] {chat_name(incoming.chat_id)} -> {chat_name(route['target'])}: skipped a duplicate {what}"
            )
            continue
        if await deliver(incoming, route, what) and key is not None:
            seen_posts[route["target"]][key] = time.time()


# ======== CATCH-UP ========
def load_bookmarks():
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (ValueError, OSError) as e:
        print(
            f"Warning: couldn't read {STATE_FILE.name} ({e}), so there's nothing to catch up this time"
        )
        return
    if isinstance(saved, dict):
        bookmarks.update(
            {
                k: v
                for k, v in saved.items()
                if isinstance(v, int) and not isinstance(v, bool)
            }
        )


def remember_position(chat_id, message_id):
    if message_id > bookmarks.get(str(chat_id), 0):
        bookmarks[str(chat_id)] = message_id
        STATE_FILE.write_text(json.dumps(bookmarks), encoding="utf-8")


async def catch_up(chat_id):
    """Forwards what this chat posted while the forwarder was off: the newest messages after the
    bookmark, at most catch_up_max_per_chat of them and none older than catch_up_hours."""
    last = bookmarks.get(str(chat_id))
    if last is None:
        # First run with catch-up on: remember where the chat is now, there's nothing to catch up yet
        newest = await client.get_messages(chat_id, limit=1)
        if newest:
            remember_position(chat_id, newest[0].id)
        return 0
    oldest_allowed = time.time() - OPTIONS["catch_up_hours"] * 3600
    newest_first = [
        message
        async for message in client.iter_messages(
            chat_id, min_id=last, limit=OPTIONS["catch_up_max_per_chat"]
        )
    ]
    if not newest_first:
        return 0
    groups = []  # Albums stay together
    for message in reversed(newest_first):
        if message.date is None or message.date.timestamp() < oldest_allowed:
            continue
        if getattr(message, "action", None):
            continue  # A service message ("joined", "pinned a message"): live forwarding skips these too
        if (
            message.grouped_id
            and groups
            and groups[-1][0].grouped_id == message.grouped_id
        ):
            groups[-1].append(message)
        else:
            groups.append([message])
    for group in groups:
        await handle(Incoming(chat_id, group, group[0].sender), caught_up=True)
    remember_position(chat_id, newest_first[0].id)  # Also past anything too old to send
    return len(groups)


# ======== SAVED MESSAGES: COMMANDS AND DAILY SUMMARY ========
def duration(seconds):
    minutes = int(seconds // 60)
    return (
        f"{minutes // 1440} d {minutes // 60 % 24} h"
        if minutes >= 1440
        else f"{minutes // 60} h {minutes % 60} min"
    )


def counts_text(numbers):
    return (
        f"{numbers['forwarded']} forwarded · {numbers['filtered']} filtered out · "
        f"{numbers['duplicates']} duplicates skipped · {numbers['failed']} failed · "
        f"{numbers['paused']} skipped while paused"
    )


def routes_text():
    lines = []
    for route in routes:
        names = ", ".join(
            chat_name(chat) if chat in chat_names else f"{chat} (not in your chat list)"
            for chat in route["sources"]
        )
        line = f"• {names} → {chat_name(route['target'])}"
        if route["topic"]:
            line += f" (topic {route['topic']})"
        rules = route["filters"]
        active = [f"{k}={v}" for k, v in rules.items() if v != DEFAULT_FILTERS[k]]
        if active:
            line += f" · filters: {', '.join(active)}"
        lines.append(line)
    return "\n".join(lines)


def status_text():
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(status["started"]))
    state = "⏸ paused (send /resume)" if status["paused"] else "▶️ forwarding"
    last = status["last_forward"]
    lines = [
        "📊 GroupForwarder",
        f"Running since {started} ({duration(time.time() - status['started'])}) · {state}",
        f"Since start: {counts_text(totals)}",
        f"Last forward: {time.strftime('%H:%M:%S', time.localtime(last)) if last else 'none yet'}",
        f"Routes: {len(routes)} (send /sources)",
    ]
    if failures:
        lines.append("Latest problems:")
        lines += [
            f"• {when} {source} → {target}: {reason}"
            for when, source, target, reason in list(failures)[-5:]
        ]
    return "\n".join(lines)


HELP_TEXT = (
    "GroupForwarder commands (send them here, in your Saved Messages):\n"
    "/status - is it running, and what has it forwarded\n"
    "/pause - stop forwarding until /resume\n"
    "/resume - start forwarding again\n"
    "/sources - which chats go where\n"
    '/chats - your chats and their IDs (/chats crypto: only chats with "crypto" in the name)\n'
    "/topics - the topic numbers of a group with topics (/topics <chat ID or name>)\n"
    "/help - this list\n"
    "\n"
    "To forward from another chat:\n"
    "1. Send /chats and copy the chat's ID (it looks like -1001234567890).\n"
    "2. Add it to SOURCE_GROUP_IDS in the .env file next to group_forwarder.py "
    "(IDs separated by commas).\n"
    "3. Then restart the forwarder.\n"
    "Where messages go is TARGET_GROUP_ID in .env. Filters, more routes, forum topics and "
    "the other options are in forwarder_config.toml (see forwarder_config.example.toml)."
)
# Telegram allows 4096 characters per message; longer replies are split below this
SAVED_MESSAGE_LIMIT = 4000


async def reply_in_saved(text):
    """Sends text to your Saved Messages as plain text (so names show exactly as they are),
    split into several messages when it's too long for one."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if (
            current
            and utf16_length(current) + utf16_length(line) + 1 > SAVED_MESSAGE_LIMIT
        ):
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    chunks.append(current)
    for chunk in chunks:
        await client.send_message("me", chunk, parse_mode=None)


def utf16_length(text):
    # Telegram counts message length in UTF-16 units (most emoji count as 2)
    return len(text.encode("utf-16-le")) // 2


def chat_kind(dialog):
    if getattr(dialog, "is_group", False):
        forum = getattr(getattr(dialog, "entity", None), "forum", False)
        return "group with topics" if forum else "group"
    return "channel" if getattr(dialog, "is_channel", False) else "private chat"


def chats_text(dialogs, wanted):
    """/chats: your chats with their IDs, and which ones the forwarder uses."""
    shown = clean(wanted, 40)
    found = [d for d in dialogs if wanted.lower() in clean(d.name).lower()]
    if not found:
        return f'No chat has "{shown}" in its name. Send /chats to see them all.'
    targets = {route["target"] for route in routes}
    title = f'Your chats with "{shown}" in the name' if wanted else "Your chats"
    lines = [f"{title} (name: ID):"]
    for dialog in found:
        line = f"{clean(dialog.name)}: {dialog.id} ({chat_kind(dialog)})"
        if dialog.id in SOURCES:
            line += " · forwarding from it"
        if dialog.id in targets:
            line += " · forwarding to it"
        lines.append(line)
    lines.append("")
    lines.append(
        "To forward from a chat, add its ID to SOURCE_GROUP_IDS in .env (comma-separated), "
        "then restart the forwarder. Send /help for all the steps."
    )
    return "\n".join(lines)


TOPICS_SHOWN = 100


def forum_topics_request(entity):
    # Telegram moved this request: Telethon 1.45 has it under messages, older versions under channels
    asked = {
        "offset_date": None,
        "offset_id": 0,
        "offset_topic": 0,
        "limit": TOPICS_SHOWN,
    }
    if hasattr(functions.messages, "GetForumTopicsRequest"):
        return functions.messages.GetForumTopicsRequest(peer=entity, **asked)
    return functions.channels.GetForumTopicsRequest(channel=entity, **asked)


async def topics_text(dialogs, wanted):
    """/topics <chat ID or part of a name>: the topic numbers of a group with topics,
    for topic = ... in a [[routes]] section."""
    if not wanted:
        return (
            "Send /topics followed by a group's ID or part of its name, e.g. "
            '/topics -1001234567890. In /chats, groups with topics say "group with topics".'
        )
    shown = clean(wanted, 40)
    try:
        matches = [d for d in dialogs if d.id == int(wanted)]
    except ValueError:
        matches = [d for d in dialogs if wanted.lower() in clean(d.name).lower()]
        with_topics = [d for d in matches if getattr(d.entity, "forum", False)]
        matches = with_topics or matches
    if not matches:
        return (
            f'No chat matches "{shown}". Send /chats to see your chats and their IDs.'
        )
    if len(matches) > 1:
        lines = [f'Several chats match "{shown}". Send /topics with one of these IDs:']
        lines += [f"{clean(d.name)}: {d.id}" for d in matches[:20]]
        return "\n".join(lines)
    dialog = matches[0]
    if not getattr(dialog.entity, "forum", False):
        return f"{clean(dialog.name)} has no topics (Topics is switched off in its settings)."
    result = await client(forum_topics_request(dialog.entity))
    # Deleted topics come back without a title
    topics = [t for t in result.topics if getattr(t, "title", None)]
    lines = [f"Topics in {clean(dialog.name)} (topic number: name):"]
    lines += [f"{topic.id}: {clean(topic.title)}" for topic in topics]
    if result.count > len(result.topics):
        lines.append(f"(the first {len(result.topics)} of {result.count})")
    lines.append("")
    lines.append(
        "To send a route into a topic, add topic = <number> to its [[routes]] section in "
        "forwarder_config.toml."
    )
    return "\n".join(lines)


async def lookup_command(event):
    """/chats and /topics: the IDs and topic numbers that go into .env and
    forwarder_config.toml, without leaving Telegram."""
    word = event.pattern_match.group(1).lower()
    wanted = (event.pattern_match.group(2) or "").strip()
    print(f"[{now()}] Command /{word} from your Saved Messages")
    try:
        dialogs = await client.get_dialogs()
        if word == "chats":
            reply = chats_text(dialogs, wanted)
        else:
            reply = await topics_text(dialogs, wanted)
        await reply_in_saved(reply)
    except (errors.RPCError, ConnectionError) as e:
        print(f"[{now()}] Couldn't answer /{word}: {type(e).__name__}: {e}")


async def command(event):
    word = event.pattern_match.group(1).lower()
    if word == "pause":
        status["paused"] = True
        reply = "⏸ Paused. New messages are skipped until you send /resume."
    elif word == "resume":
        status["paused"] = False
        reply = "▶️ Forwarding again."
    elif word == "sources":
        reply = "Routes:\n" + routes_text()
    elif word == "status":
        reply = status_text()
    else:
        reply = HELP_TEXT
    print(f"[{now()}] Command /{word} from your Saved Messages")
    try:
        await reply_in_saved(reply)
    except (errors.RPCError, ConnectionError) as e:
        print(f"[{now()}] Couldn't answer /{word}: {type(e).__name__}: {e}")


def seconds_until(clock, now_seconds=None):
    """Seconds from now until the next time the local clock shows clock ("HH:MM")."""
    now_seconds = time.time() if now_seconds is None else now_seconds
    hour, minute = (int(part) for part in clock.split(":"))
    day = time.localtime(now_seconds)
    target = time.mktime(
        (day.tm_year, day.tm_mon, day.tm_mday, hour, minute, 0, 0, 0, -1)
    )
    if (
        target <= now_seconds
    ):  # Already past today: tomorrow (mktime handles month ends)
        target = time.mktime(
            (day.tm_year, day.tm_mon, day.tm_mday + 1, hour, minute, 0, 0, 0, -1)
        )
    return target - now_seconds


def summary_text():
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(status["started"]))
    lines = [
        f"✅ GroupForwarder is still running (since {started}){' · ⏸ paused' if status['paused'] else ''}",
        f"Since the last summary: {counts_text(period)}",
    ]
    if period["failed"]:
        recent = list(failures)[-min(period["failed"], 10) :]
        lines.append("Failed forwards:")
        lines += [
            f"• {when} {source} → {target}: {reason}"
            for when, source, target, reason in recent
        ]
    return "\n".join(lines)


async def send_summary():
    text = summary_text()
    try:
        await reply_in_saved(text)
    except (errors.RPCError, ConnectionError) as e:
        print(f"[{now()}] Couldn't send the daily summary: {type(e).__name__}: {e}")
        return
    for what in COUNTERS:
        period[what] = 0
    print(f"[{now()}] Sent the daily summary to your Saved Messages")


async def daily_summary_loop():
    while True:
        await asyncio.sleep(seconds_until(OPTIONS["daily_summary_time"]))
        await send_summary()


# ======== HANDLERS ========
async def handler(event):
    if (
        event.grouped_id
    ):  # Part of an album: album_handler forwards the whole album at once
        return
    await handle(Incoming(event.chat_id, [event.message], event.sender))


async def album_handler(event):
    await handle(
        Incoming(event.chat_id, list(event.messages), event.sender, album_event=event)
    )


client.on(events.NewMessage(chats=SOURCES))(handler)
client.on(events.Album(chats=SOURCES))(album_handler)
if OPTIONS["commands"]:
    # Only your own messages in your own Saved Messages
    client.on(
        events.NewMessage(
            chats="me",
            outgoing=True,
            pattern=r"(?i)^/(status|pause|resume|sources|help)\s*$",
        )
    )(command)
    client.on(
        events.NewMessage(
            chats="me",
            outgoing=True,
            pattern=r"(?i)^/(chats|topics)(?:\s+(.*\S))?\s*$",
        )
    )(lookup_command)


def lock_single_copy():
    """Only one copy may use the login at a time: two copies get it cancelled (AUTH_KEY_DUPLICATED).
    The lock lasts until this forwarder stops, also when it crashes."""
    handle_file = open(LOCK_FILE, "a+")  # noqa: SIM115 - stays open on purpose
    try:
        if os.name == "nt":
            handle_file.seek(0)
            msvcrt.locking(handle_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle_file.close()
        raise SystemExit(
            "Another copy of the forwarder (or --list-chats) is already running. "
            "Stop it first: two copies would get the login cancelled."
        ) from None
    return handle_file


# ======== RUN ========
single_copy = lock_single_copy()

client.loop.run_until_complete(client.connect())
if NO_LOGIN and not client.loop.run_until_complete(client.is_user_authorized()):
    raise SystemExit(
        "Not logged in. Run 'python group_forwarder.py' once in a terminal to log in, then start it again."
    )
# The first start asks for your login in the terminal; later starts use the saved .session file
LOGIN_QUESTIONS = {
    "phone": lambda: input(
        "Your phone number, with the country code (e.g. +44 7700 900123): "
    ),
    "code_callback": lambda: input(
        "The login code Telegram just sent you (look in your Telegram app): "
    ),
    "password": lambda: getpass.getpass(
        "Your Telegram two-step verification password (it isn't shown as you type): "
    ),
}
client.start(**LOGIN_QUESTIONS)

if LIST_CHATS:
    print("Your chats (name: ID):")
    for dialog in client.loop.run_until_complete(client.get_dialogs()):
        print(f"{clean(dialog.name)}: {dialog.id} ({chat_kind(dialog)})")
    print(
        "Put the IDs you need in .env (SOURCE_GROUP_IDS, TARGET_GROUP_ID) or forwarder_config.toml. "
        "While the forwarder runs, /chats in your Saved Messages shows this list too."
    )
    client.disconnect()  # With no event loop running, Telethon finishes this by itself
    raise SystemExit(0)

# A fresh login has no chats cached yet, so forwarding would fail with "Could not find the input entity".
# Loading the dialogs fills the cache (and gives us the chat names for the log); then check the chats are really reachable.
dialogs = client.loop.run_until_complete(client.get_dialogs())
chat_names.update({dialog.id: clean(dialog.name) for dialog in dialogs})
chat_entities.update({dialog.id: getattr(dialog, "entity", None) for dialog in dialogs})

# Check against the chat list: the .session file also remembers chats you have left,
# so looking a chat up by ID still works even though Telegram no longer lets you post there.
for route in routes:
    if route["target"] not in chat_names:
        raise SystemExit(
            f"{route['name']} {route['target']} isn't in your chat list (left, removed or deleted?). "
            "Is this account still a member of that group? To see your chats, run: "
            "python group_forwarder.py --list-chats"
        )

# A source with a typo, or a chat this account has left, would otherwise be ignored without a word
reachable_sources = []
for chat_id in SOURCES:
    if chat_id in chat_names:
        reachable_sources.append(chat_id)
    else:
        print(
            f"Warning: source chat {chat_id} isn't in your chat list (left, removed or deleted?). Nothing will be forwarded from it."
        )
if not reachable_sources:
    raise SystemExit(
        "None of the source chats (SOURCE_GROUP_IDS and routes) are in your chat list. Is this account a member of them?"
    )

if CONFIG_FILE.exists():
    active = [k for k, v in OPTIONS.items() if v != DEFAULT_OPTIONS[k]]
    print(
        f"Settings: {CONFIG_FILE.name} ({', '.join(active) or 'nothing changed from the defaults'})"
    )
else:
    print(f"Settings: .env only (no {CONFIG_FILE.name} next to the script)")
targets = list(dict.fromkeys(chat_name(route["target"]) for route in routes))
print(
    f"Forwarder is running: listening to {len(reachable_sources)} chat(s), forwarding to {', '.join(targets)}"
)
for route in routes:
    names = ", ".join(
        chat_name(chat) for chat in route["sources"] if chat in chat_names
    )
    if names:
        print(
            f"  - {names} -> {chat_name(route['target'])}"
            + (f" (topic {route['topic']})" if route["topic"] else "")
        )
if OPTIONS["commands"]:
    print(
        "In your Saved Messages, send /help for the commands and setup steps "
        "(/chats and /topics find chat IDs and topic numbers)."
    )

if OPTIONS["catch_up"]:
    load_bookmarks()

    async def catch_up_all():
        caught = 0
        for chat_id in reachable_sources:
            caught += await catch_up(chat_id)
        print(f"Catch-up done: {caught} missed message(s) or album(s) handled")

    client.loop.run_until_complete(catch_up_all())

if OPTIONS["daily_summary"]:
    client.loop.create_task(daily_summary_loop())

# ======== UNTIL STOPPED ========
client.run_until_disconnected()
