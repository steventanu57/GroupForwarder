# Architecture

## Tech stack

- **Python 3.12 or 3.13** (the optional config file is read with the built-in `tomllib`)
- **Telethon 1.45**: Telegram client library (MTProto). It logs in as a user account, not a bot, so it can read any chat the account is a member of.
- **python-dotenv**: loads settings from `.env`
- **PowerShell** (Windows): `run_forwarder.ps1` keeps the forwarder running when started with Windows

## How it works

```
 Source chats  ──new message──▶  group_forwarder.py  ──forward or copy──▶  Target group(s)
 (SOURCE_GROUP_IDS,              logged in as you                         (TARGET_GROUP_ID,
  [[routes]] sources)            via group_forwarder.session               [[routes]] targets / topics)
                                     │            ▲
                  forwarder_config.toml           Saved Messages: /status, /pause, /resume, /sources;
                  forwarder_state.json            daily summary
```

`group_forwarder.py` is a plain script with no `main()`. Everything runs top to bottom:

1. It sets stdout to UTF-8, so chat names with emoji don't crash `print()` when output goes to a log file on Windows.
2. **Command-line options:** `--list-chats`, `--no-login` and `--log FILE`. An unknown option stops it with a message. `--log` sends all output (also errors) to the file, line by line.
3. **Settings:** it reads `.env` and the optional `forwarder_config.toml`, both from the script's own folder, so it works from any working directory.
   - It stops with a message naming the setting if:
     - a setting is missing
     - a chat ID isn't a number (a typo or a `@username`)
     - `SOURCE_GROUP_IDS` is empty
     - the config file has a syntax mistake, an unknown section or setting (a typo), or a wrong kind of value
   - **Routes:** they are the `.env` route plus any `[[routes]]`. It refuses to start if any target is also a source anywhere, since our own forwards would then be forwarded again in an endless loop.
4. **One copy only:** it locks `group_forwarder.lock` (`msvcrt` on Windows, `fcntl` elsewhere) and stops with "Another copy … is already running" if another copy holds it. Two processes using the same `.session` get it cancelled (`AUTH_KEY_DUPLICATED`). The operating system releases the lock when the process ends, also after a crash.
5. **Login:**
   - It connects using `group_forwarder.session`. With `--no-login` and no valid login, it stops instead of waiting for a phone number.
   - Otherwise `client.start()` logs in. The first run asks for your phone number, the login code and, if you have one, your 2FA password.
6. **`--list-chats`:** prints every chat as `name: ID (channel / group / private chat)`, disconnects and stops (exit code 0).
7. **Chat list:** it loads your chat list. A fresh login needs this before it can reach any chat; without it, forwarding fails with "Could not find the input entity". The chat list also gives the chat names for the log, and each chat's "Restrict saving content" flag. Then it checks the chats against that list (not by looking IDs up, because the `.session` file also remembers chats you have left):
   - Every route's target must be in the list, or it stops.
   - It warns about each source that isn't, and stops if none is.
8. **Startup lines:** it prints which settings file is used (and what is switched on), then `Forwarder is running: listening to N chat(s), forwarding to <targets>` and each route. These lines only appear once all the checks have passed.
9. **Catch-up** (if on): it forwards what was missed while it was off (see below).
10. **Daily summary** (if on): it starts the daily summary task.
11. **Listening:** it listens for `NewMessage` and `Album` events from every route's sources, and (if `commands` is on) for commands in your Saved Messages. It runs until stopped with Ctrl+C.

`get_groupId.py` is a separate helper with its own session (`groupid.session`, its own login). It prints every chat you are in with its ID. Unlike `--list-chats`, it also works while the forwarder is running.

### Handling a message

`handle()` takes an `Incoming`: one message or one album, from a live event or from catch-up. In this order:

1. **Catch-up bookkeeping** (only with `catch_up` on): skip it if this message ID was already handled (it can arrive live while catch-up is running), and move the chat's bookmark in `forwarder_state.json`.
2. **Own messages:** with `skip_own_messages`, your own messages (`out`) are skipped.
3. **Pause:** while paused with `/pause`, the message is skipped and counted.
4. **Routes:** for each route that lists this chat:
   - Skip it if the route's filters say so (`filtered_out()`).
   - With `skip_duplicates`, skip it if the same post went to this target within the window (`fingerprint()`).
   - Otherwise `deliver()` it.

`deliver()` forwards, or copies with `copy_mode`, and handles failures:

- **Forward:** `client.forward_messages`. A live album uses its own `forward_to`, so it stays one album. A route with a `topic` uses Telegram's `ForwardMessagesRequest` with `top_msg_id`, because Telethon's `forward_messages` can't post into a forum topic. Telegram shows "Forwarded from …".
- **Copy:** `send_message` / `send_file`, with `📣 <b>source chat name</b>` on top and the original text's formatting kept (converted to HTML with Telethon's `html.unparse`). A topic route uses `reply_to=topic`.
  - Messages from chats or messages with "Restrict saving content" are never copied; they are reported as failed.
  - A message that can't be copied (for example, a caption that gets too long with the header) is forwarded instead, and the log says so.
- **Log line:** each delivery prints a line like `[12:04:31] Crypto Calls -> My Feed: message from Alice` (with ` (copied)` for copies and `missed …` for catch-up). The time is local time, and the names come from the chat list and from the message itself, so no extra request is made.
- **Albums:** Telegram sends each photo or video separately, and each one also arrives as a `NewMessage` with a `grouped_id`. The `NewMessage` handler skips those. The `Album` handler waits about 0.5 s to collect the whole album, then handles it as one.

### Filters

`[filters]` applies to every route. A `[[routes]]` entry can give any filter setting itself, which replaces that one setting for the route.

- **Keywords:** these ignore case and match inside words, in the text or captions.
- **`media_only`:** a link preview doesn't count as media.
- **`skip_bots`:** uses the sender's `bot` flag.
- **`skip_reposts`:** uses the message's `fwd_from`.

### Duplicates

`fingerprint()` is the same for the same post seen in two chats:
- **A forwarded channel post:** its original channel and post number.
- **Anything else:** a hash of the text, plus the photo or file ID.

Messages with neither (for example, polls) are never treated as duplicates. Fingerprints are kept per target, in memory, for `duplicate_window_hours`, and are forgotten on restart.

### Catch-up

`forwarder_state.json` holds, per source chat, the newest message ID the forwarder has seen (its "bookmark"). It is written whenever a bookmark moves, only while `catch_up` is on.

- **First start with catch-up on:** a chat without a bookmark gets one at its newest message, and nothing is sent.
- **Later starts:**
  - It fetches the newest `catch_up_max_per_chat` messages after the bookmark (`iter_messages(min_id=…)`).
  - It drops those older than `catch_up_hours` and service messages ("joined", "pinned …"), which can't be forwarded and which live forwarding never sees.
  - It keeps albums together and sends them oldest first through `handle()`, then moves the bookmark past everything fetched.

### Saved Messages commands and the daily summary

- **Commands:** a `NewMessage` handler with `chats="me"`, `outgoing=True` and a pattern answers `/status`, `/pause`, `/resume`, `/sources` and `/help` in your Saved Messages. The pause is not saved, so a restart resumes forwarding.
- **Counters:**
  - `totals` counts since start (for `/status`).
  - `period` counts since the last daily summary.
  - `failures` keeps the latest 20 failed forwards.
- **Daily summary:** `daily_summary_loop()` sleeps until `daily_summary_time` (local time; `seconds_until()` uses `time.mktime`, so month ends and daylight saving work), sends `summary_text()` to Saved Messages, then starts a new period.

## What gets forwarded

| Forwarded | Not forwarded |
|---|---|
| New text and media messages, including your own messages in a source chat (unless `skip_own_messages`) | Messages posted while the script is not running (unless `catch_up`) |
| Albums, as one album | Edits and deletions of messages already forwarded |
| | Service messages (joins, pins, title changes) |
| | Anything from chats with "Restrict saving content" turned on |
| | Messages your filters, pause or duplicate check skip |

Chat IDs must be in the form `--list-chats` or `get_groupId.py` prints: negative numbers, with `-100...` for channels and supergroups, and positive numbers for private chats with people.

Each update is handled in its own task, so when many messages arrive at once their order in the target group is not strictly guaranteed. With `strict_order` on, the client handles one update at a time (`sequential_updates=True`).

## When something goes wrong

| Problem | What happens |
|---|---|
| A forward fails (restricted content, no permission to post in the target, a Telegram or connection error) | It prints `Could not forward ... from <chat>: <reason>` and keeps running. That message is lost. It appears in `/status` and the daily summary |
| Telegram rate-limits (`FloodWait`) for 60 s or less | Telethon waits automatically, then retries |
| Telegram rate-limits for more than 60 s | It prints how long Telegram asked it to wait, waits that long, then retries once. If that fails too, it is reported like any failed forward |
| Network drop, short or long | Telethon keeps trying to reconnect, every 5 s, until the internet is back (`connection_retries=None`). Messages sent while disconnected may be missed, unless catch-up picks them up at the next start |
| A source chat ID has a typo, or the account left that chat | A warning at startup names the chat ID. No source reachable at all: the script stops with a message |
| A target isn't in your chat list | The script stops with a message naming the setting (`TARGET_GROUP_ID` or `[[routes]] #n`) |
| A mistake in `forwarder_config.toml` | The script stops with a message naming the setting and what it should be |
| A second copy is started | It stops at once with "Another copy … is already running" |
| The same `.session` file in use in two places anyway (e.g. on two computers) | Telegram invalidates it (`AUTH_KEY_DUPLICATED`) and you have to log in again |

## Starting with Windows (`run_forwarder.ps1`)

The README shows how to register a scheduled task that runs `run_forwarder.ps1` at logon. The script loops:

1. Start `.venv\Scripts\python.exe group_forwarder.py --no-login --log forwarder.log`.
2. When it stops, log the exit code and how long it ran.
3. If it ran less than `QuickExitSeconds` (60), stop with exit code 1: an exit that quick is a setup problem a restart can't fix, such as a settings mistake, not being logged in, or another copy running. Otherwise wait `RestartDelaySeconds` (30) and start it again.

Its own lines in `forwarder.log` start with `run_forwarder.ps1:` and are written as UTF-8. The parameters (`-QuickExitSeconds`, `-RestartDelaySeconds`, `-Python`, `-Script`, `-Log`) exist so the tests can use short times and a dummy script.

## Files

| File | Role |
|---|---|
| `group_forwarder.py` | The forwarder |
| `get_groupId.py` | Lists your chats and their IDs (separate login) |
| `forwarder_config.example.toml` | Commented template for `forwarder_config.toml`; every value is the default |
| `run_forwarder.ps1` | Restart loop for the Windows auto-start task |
| `tests/` | Tests that run the forwarder with a fake Telegram client (no login, no network) |
| `.env` (git-ignored) | `API_ID`, `API_HASH`, `SOURCE_GROUP_IDS`, `TARGET_GROUP_ID` |
| `forwarder_config.toml` (git-ignored) | Your optional settings (holds chat IDs) |
| `forwarder_state.json` (git-ignored) | Catch-up bookmarks, written while running with `catch_up` on |
| `forwarder.log` (git-ignored) | Output when started by `run_forwarder.ps1` |
| `group_forwarder.lock` (git-ignored) | Held while the forwarder runs, so only one copy uses the login |
| `*.session` (git-ignored) | Telegram logins (SQLite). Anyone with the file can use your account |

## Setup and run

```
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # macOS/Linux: cp .env.example .env
```

1. Put your `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org) in `.env`.
2. Run `python group_forwarder.py --list-chats`, then copy the chat IDs you want into `.env`.
3. Optionally, copy `forwarder_config.example.toml` to `forwarder_config.toml` and change settings.
4. Run `python group_forwarder.py` and log in once. Keep it running; stop with Ctrl+C. Or set it up to start with Windows (README).

## Tests

```
pip install -r requirements-dev.txt
pytest
ruff check .
```

The tests replace `telethon.TelegramClient` with a `RecordingClient`, which records what the script asks it to do, then run the real `group_forwarder.py`.
- **Isolation:** each run uses **a copy of the script in a new temporary folder** (its `BASE_DIR`), deleted when the test ends. So the tests never read your `.env` or `forwarder_config.toml`, and never touch the lock, bookmarks or log of a forwarder that is running.
- **No login or network** is needed.
- **`run_script()` options:**
  - `config=`: text for `forwarder_config.toml`.
  - `files=`: other files in that folder, e.g. bookmarks.
  - `argv=`: command-line options.
  - `history=`: messages for catch-up.
  - `protected=`: chats with "Restrict saving content".
  - `left_chats=` / `unknown_peers=`: chats you've left / that don't exist.
  - `authorized=`: whether the login is valid.
  - `lock_held=`: another copy holds the lock.

They cover:
- forwarding a message, and albums forwarded whole
- loading chats before listening
- startup checks: the target or a route's target not in the chat list, a source not in the chat list (also one the `.session` file still remembers after you left it), no source in the chat list, a chat ID that isn't a number, an empty source list, a target that is also a source
- the "running" line appearing only after the checks
- reconnecting forever after an outage
- a failed forward reported without crashing, and a long rate limit waited out then retried
- the log line with time, source and target, and the sender's name taken without an extra request
- emoji names in a log file, for both `group_forwarder.py` and `get_groupId.py`
- the config file: no file, the example file, typos, wrong values, syntax mistakes, an empty file
- routes, forum topics, filters (also per route), skipping your own messages, strict order
- copy mode: header and formatting, albums, protected chats, falling back to a forward, topics
- duplicates within and after the window
- catch-up: first run, the newest N within the hours limit, albums, service messages, no double sending, the bookmark following live messages
- Saved Messages commands, a failed command reply, the daily summary and its timing
- `--list-chats`, the single-copy lock, `--no-login`, unknown options, `--log`
- `run_forwarder.ps1` restarting after a crash but not after a quick stop (Windows only)

`RecordingClient` also records the client settings (`kwargs`) and every printed line, as `("print", text)`, so tests can check the order of output and calls. Set `GF_DIR` to test a copy of the script in another folder. Test data uses the `zz-` prefix.
