# Architecture

## Tech stack

- **Python 3.12 or 3.13**
- **Telethon 1.45**: Telegram client library (MTProto). It logs in as a user account, not a bot, so it can read any chat the account is a member of.
- **python-dotenv**: loads settings from `.env`

## How it works

```
 Source chats  ──new message──▶  group_forwarder.py  ──forward──▶  Target group
 (SOURCE_GROUP_IDS)              logged in as you                (TARGET_GROUP_ID)
                                 via group_forwarder.session
```

`group_forwarder.py` is a plain script with no `main()`. Everything runs top to bottom:

1. It sets stdout to UTF-8, so chat names with emoji don't crash `print()` when output goes to a log file on Windows.
2. It reads `.env` from the script's own folder, so it works from any working directory. A missing setting stops it with a message naming the setting. It also refuses to start if `TARGET_GROUP_ID` is also a source, since that would forward in an endless loop.
3. It logs in using `group_forwarder.session`. The first run asks for your phone number, the login code and, if you have one, your 2FA password.
4. It loads your chat list. A fresh login needs this before it can reach any chat; without it, forwarding fails with "Could not find the input entity". It then exits with a clear message if the account can't reach the target group.
5. It listens for `NewMessage` and `Album` events from the chats in `SOURCE_GROUP_IDS`:
   - **Single message:** forwarded unchanged with `forward_messages`. Telegram shows "Forwarded from ...".
   - **Album:** Telegram sends each photo or video separately, and each one also arrives as a `NewMessage` with a `grouped_id`. The `NewMessage` handler skips those. The `Album` handler waits about 0.5 s to collect the whole album, then forwards it as one album.
   - After each forward, it prints the sender's name: a user's first name, a channel's title, or "Unknown".
6. It runs until stopped with Ctrl+C.

`get_groupId.py` is a separate helper with its own session (`groupid.session`, its own login). It prints every chat you are in with its ID, so you can fill in `.env`.

## What gets forwarded

| Forwarded | Not forwarded |
|---|---|
| New text and media messages, including your own messages in a source chat | Messages posted while the script is not running (catch-up is off) |
| Albums, as one album | Edits and deletions of messages already forwarded |
| | Service messages (joins, pins, title changes) |
| | Anything from chats with "Restrict saving content" turned on |

Chat IDs must be in the form `get_groupId.py` prints: negative numbers, with `-100...` for channels and supergroups. A positive ID would also match a user or basic group with that number.

Each update is handled in its own task, so when many messages arrive at once their order in the target group is not strictly guaranteed.

## When something goes wrong

| Problem | What happens |
|---|---|
| A forward fails (restricted content, no permission to post in the target, an unexpected error) | Telethon logs the error to the console and the forwarder keeps running. That message is lost |
| Telegram rate-limits (`FloodWait`) for 60 s or less | Telethon waits automatically, then retries |
| Telegram rate-limits for more than 60 s | Treated as a failed forward: logged and lost |
| Short network drop | Telethon reconnects on its own. Messages sent during the drop may be missed |
| Network down for longer (5 failed reconnect attempts) | The script stops with an error. Nothing restarts it, so start it again by hand |
| The same `.session` file in use in two places | Telegram invalidates it (`AUTH_KEY_DUPLICATED`) and you have to log in again |

## Files

| File | Role |
|---|---|
| `group_forwarder.py` | The forwarder |
| `get_groupId.py` | Lists your chats and their IDs |
| `tests/` | Tests that run the forwarder with a fake Telegram client (no login, no network) |
| `.env` (git-ignored) | `API_ID`, `API_HASH`, `SOURCE_GROUP_IDS`, `TARGET_GROUP_ID` |
| `*.session` (git-ignored) | Telegram logins (SQLite). Anyone with the file can use your account |

## Setup and run

```
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # macOS/Linux: cp .env.example .env
```

1. Put your `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org) in `.env`.
2. Run `python get_groupId.py`, then copy the chat IDs you want into `.env`.
3. Run `python group_forwarder.py` and log in once. Keep it running; stop with Ctrl+C.

Run only one copy of a session at a time.

## Tests

```
pip install -r requirements-dev.txt
pytest
ruff check .
```

The tests replace `telethon.TelegramClient` with a `RecordingClient` that records what the script asks it to do, then run the real `group_forwarder.py` with fake settings. No login or network is needed. They cover: forwarding a message, loading chats before listening, the error when the account isn't in the target group, albums forwarded whole, refusing a target that is also a source, and emoji names in a log file. Set `GF_DIR` to test a copy of the script in another folder. Test data uses the `zz-` prefix.
