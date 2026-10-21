# Group Forwarder

A Telegram userbot that watches a list of groups and channels and forwards every new message into one group of your choice. Useful for collecting several crypto or news channels into a single feed. Optional settings add filters, more routes (other targets or forum topics), copies without "Forwarded from", catch-up after a restart, duplicate skipping and a daily summary.

Built with [Telethon](https://docs.telethon.dev/).

## How it works

The script logs in as **your own Telegram account** (not a bot account), so it can read any group or channel you are a member of. While it is running, each new message from a source chat is forwarded to your target group, and albums arrive as one album. Messages posted while it is not running are not forwarded, unless you switch on catch-up. See [ARCHITECTURE.md](ARCHITECTURE.md) for details.

## Setup

1. Install Python 3.12 or 3.13 and the dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Get an API ID and API hash for your account at [my.telegram.org/apps](https://my.telegram.org/apps): log in with your phone number, open **API development tools**, create an app (any name works), and copy its `api_id` (a number) and `api_hash` (a long code).
3. Copy `.env.example` to `.env` and fill in `API_ID` and `API_HASH`. The example file explains every value.
4. Find the IDs of your chats (see [Finding chat IDs](#finding-chat-ids-and-topic-numbers)):

   ```
   python group_forwarder.py --list-chats
   ```

   It prints every chat you are in with its ID, then stops. The first run logs you in: it asks for your phone number with the country code (e.g. `+44 7700 900123`), the login code Telegram sends to your Telegram app, and your two-step verification password if you have one.

5. In `.env`, put the IDs of the chats to forward from in `SOURCE_GROUP_IDS` (comma-separated), and the ID of your own group in `TARGET_GROUP_ID`.
6. Start the forwarder:

   ```
   python group_forwarder.py
   ```

   If a setting is missing or wrong, it stops with a message saying what to fix and where to find the right value.

## Where to change what

| What | Where |
|---|---|
| Your login (`API_ID`, `API_HASH`) | `.env` |
| Which chats to forward from and to (`SOURCE_GROUP_IDS`, `TARGET_GROUP_ID`) | `.env` |
| More targets, forum topics, filters, copy mode, catch-up, duplicates, daily summary | `forwarder_config.toml` (copy `forwarder_config.example.toml`) |
| How it's started (list chats, no login prompt, log file) | [Command-line options](#command-line-options) |
| Start with Windows, restart delays | `run_forwarder.ps1` (see [Start automatically with Windows](#start-automatically-with-windows)) |

All files sit next to `group_forwarder.py`. Restart the forwarder after a change. Don't change the settings inside `group_forwarder.py`: the values there are only the defaults.

## Finding chat IDs and topic numbers

A chat ID is a number. Groups and channels look like `-1001234567890` (`-100` and then digits). There are several ways to see them:

- **Before the forwarder runs:** `python group_forwarder.py --list-chats`.
- **While it runs:** send `/chats` to your **Saved Messages**. It marks the chats you already forward from or to. `/chats crypto` shows only chats with "crypto" in their name.
- **Any time:** `python get_groupId.py`, which has its own login.
- **From a message link:** in a private group or channel, a message link like `t.me/c/1234567890/45` belongs to chat `-1001234567890`. Public channels' links use their @name instead, so use one of the ways above for those.

**Topic numbers** (for `topic =` in a `[[routes]]` section, when the target is a group with Topics switched on):
- Send `/topics` and the group's ID or part of its name to your Saved Messages, e.g. `/topics -1001234567890`.
- Or copy a message link from inside that topic: in `t.me/c/1234567890/45/678`, the topic is `45`.

## Configuration

### `.env` (required)

| Setting | What it is |
|---|---|
| `API_ID` | Your `api_id` from [my.telegram.org/apps](https://my.telegram.org/apps) (a number) |
| `API_HASH` | Your `api_hash` from the same page (a long code) |
| `SOURCE_GROUP_IDS` | Chats to forward from, comma-separated, e.g. `-1001111111111,-1002222222222` |
| `TARGET_GROUP_ID` | The group that receives the forwarded messages, e.g. `-1003333333333` |

### `forwarder_config.toml` (optional)

Everything else is in **`forwarder_config.toml`**, in the same folder as `group_forwarder.py`. Without this file the forwarder works as described above.

To use it, copy **`forwarder_config.example.toml`** to `forwarder_config.toml`, change what you want, and restart the forwarder. The example explains every setting, and all its values are the defaults, so copying it as it is changes nothing. A misspelled setting stops the forwarder with a message saying which one. On start, the forwarder prints which settings file it uses and what is switched on.

| Section | Setting | Default | What it does |
|---|---|---|---|
| `[options]` | `skip_own_messages` | `false` | Don't forward what you post yourself in a source chat |
| | `catch_up` | `false` | On start, forward what was missed while the forwarder was off… |
| | `catch_up_hours` | `6` | …from the last this many hours… |
| | `catch_up_max_per_chat` | `20` | …and at most this many messages per chat |
| | `strict_order` | `false` | Handle messages one at a time, in arrival order (slower in bursts) |
| | `copy_mode` | `false` | Send a copy with the source chat's name on top, instead of "Forwarded from" |
| | `skip_duplicates` | `false` | Don't send the same post twice to the same target… |
| | `duplicate_window_hours` | `24` | …within this many hours |
| | `commands` | `true` | Control it from your Saved Messages (see below) |
| | `daily_summary` | `false` | A daily "still running" message in your Saved Messages… |
| | `daily_summary_time` | `"09:00"` | …at this local time |
| `[filters]` | `include_keywords` | `[]` | Only forward messages containing one of these words |
| | `exclude_keywords` | `[]` | Never forward messages containing one of these words |
| | `media_only` | `false` | Only forward messages with a photo, video, file, … |
| | `skip_bots` | `false` | Don't forward messages sent by bots |
| | `skip_reposts` | `false` | Don't forward messages that are forwards themselves |
| `[[routes]]` | `sources`, `target` | none | Extra routes, on top of the `.env` one: these chats go to that target |
| | `topic` | none | Send this route into a forum topic of the target |
| | any `[filters]` setting | from `[filters]` | This route's own filter |

- **Catch-up:** the first start with `catch_up` on only remembers where each chat is (in `forwarder_state.json`); from the next start on, missed messages are sent, oldest first.
- **Topic numbers:** send `/topics <chat ID>` to your Saved Messages, or see [Finding chat IDs and topic numbers](#finding-chat-ids-and-topic-numbers).
- **Never copied:** chats with "Restrict saving content" are never copied or forwarded.
- **No loops:** a chat can't be both a source and a target, because that would forward messages in an endless loop.

### Command-line options

| Option | What it does |
|---|---|
| `--list-chats` | Print your chats and their IDs, then stop |
| `--no-login` | Never ask for a phone number: stop with a message if not logged in (used by `run_forwarder.ps1`) |
| `--log FILE` | Append everything the forwarder prints to `FILE` (used by `run_forwarder.ps1`) |

## Commands in your Saved Messages

While the forwarder runs, send these to yourself in **Saved Messages** (switch them off with `commands = false`):

| Command | What it does |
|---|---|
| `/status` | Running since when, what was forwarded, filtered, skipped and failed, and the latest problems |
| `/pause` | Stop forwarding until `/resume` (messages in between are skipped) |
| `/resume` | Start forwarding again |
| `/sources` | Which chats go where, with each route's filters (a source chat you're not in is marked) |
| `/chats` | Your chats and their IDs, marking the ones it forwards from or to. `/chats crypto`: only names with "crypto" |
| `/topics <chat ID or name>` | The topic numbers of a group with Topics, for `topic =` in a route |
| `/help` | The list of commands, and the steps to forward from another chat |

When the forwarder starts, its last line reminds you to send `/help`.

## Start automatically with Windows

`run_forwarder.ps1` keeps the forwarder running:
- **After a crash:** it restarts the forwarder 30 seconds later.
- **Problems a restart can't fix:** if the forwarder stops within a minute of starting (a settings mistake, not logged in, or another copy already running), it stops too.
- **Log:** everything goes to `forwarder.log`.

1. Start the forwarder once in a terminal as usual, so it is logged in. Then stop it with Ctrl+C.
2. In PowerShell, in this folder, register a task that runs it when you log in to Windows:

   ```powershell
   $here = (Get-Location).Path
   $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$here\run_forwarder.ps1`""
   $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
   $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
   Register-ScheduledTask -TaskName "GroupForwarder" -Action $action -Trigger $trigger -Settings $settings
   ```

3. Start it now without logging out: `Start-ScheduledTask -TaskName "GroupForwarder"`, then check `forwarder.log`.

To remove it: `Unregister-ScheduledTask -TaskName "GroupForwarder"`, then end the `python.exe` of the forwarder in Task Manager if it is still running.

Only one copy can run at a time: a second copy (from a terminal, the task or `--list-chats`) stops with "Another copy of the forwarder … is already running". Two copies would get the login cancelled.

## Files

| File | What it does |
|---|---|
| `group_forwarder.py` | The forwarder |
| `get_groupId.py` | Lists your chats and their IDs (with its own login) |
| `forwarder_config.example.toml` | Template for your `forwarder_config.toml` |
| `run_forwarder.ps1` | Keeps the forwarder running, for starting it with Windows |
| `.env.example` | Template for your `.env` |

## Keep these private

`.env`, `forwarder_config.toml` (your chat IDs), the `*.session` files and the files the forwarder writes while running (`forwarder_state.json`, `forwarder.log`, `group_forwarder.lock`) are ignored by git. A `.session` file is a logged-in Telegram session: anyone who has a copy can use your account, so never share or commit it. If one leaks, end the session in Telegram under Settings > Devices.

## Notes

- On start, the forwarder lists which chats go where. It warns about any source chat that isn't in your chat list, for example a typo in the ID or a chat you have left. Rejoin the chat and restart the forwarder to forward from it again. If a target isn't in your chat list, it doesn't start.
- Each forwarded message is logged with the time, the source chat, the target group and the sender. If a forward fails, the log says why, and the forwarder keeps running.
- Names in the log are set by other people, so they're cleaned first: control codes and line breaks are removed (nobody can change your terminal or fake a log line), and long names are shortened to 60 characters. Message text is never printed.
- If your internet drops, it keeps trying to reconnect until it's back. Messages posted while it was disconnected may be missed.
- Chats that have "Restrict saving content" turned on cannot be forwarded from.
- The script acts as your personal account, so use it within [Telegram's Terms of Service](https://telegram.org/tos) and respect the owners of the content you forward.

## License

MIT, see [LICENSE](LICENSE).
