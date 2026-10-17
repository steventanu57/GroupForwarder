# Architecture

## Tech stack

- **Python 3.12**
- **Telethon 1.45**: Telegram client library (MTProto). It logs in as a user account, not a bot.
- **python-dotenv**: loads settings from `.env`

## How it works

```
 Source chats  ──new message──▶  group_forwarder.py  ──forward──▶  Target group
 (SOURCE_GROUP_IDS)              logged in as you                (TARGET_GROUP_ID)
                                 via group_forwarder.session
```

1. On start, the script reads `.env`. It refuses to start if `TARGET_GROUP_ID` is also a source, since that would forward in an endless loop.
2. It logs in using `group_forwarder.session`. The first run asks for your phone number and the login code.
3. It loads your chat list, which a fresh login needs before it can reach any chat, and exits with a clear message if the target group can't be found.
4. It listens for new messages (`NewMessage`) and albums (`Album`) from the chats in `SOURCE_GROUP_IDS`.
5. Each new message is forwarded unchanged to `TARGET_GROUP_ID` (Telegram shows "Forwarded from ..."), albums are forwarded as one album, and the sender's name is printed.
6. It runs until stopped. Messages posted while it is not running are skipped.

`get_groupId.py` is a separate helper with its own session (`groupid.session`). It prints every chat you are in with its ID, so you can fill in `.env`.

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

Run only one copy of a session at a time. If the same `.session` file is used from two places at once, Telegram invalidates it (`AUTH_KEY_DUPLICATED`) and you have to log in again.

## Tests

```
pip install -r requirements-dev.txt
pytest
ruff check .
```
