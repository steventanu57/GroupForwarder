# Group Forwarder

A Telegram userbot that watches a list of groups and channels and forwards every new message into one group of your choice. Useful for collecting several crypto or news channels into a single feed.

Built with [Telethon](https://docs.telethon.dev/).

## How it works

The script logs in as **your own Telegram account** (not a bot account), so it can read any group or channel you are a member of. While it is running, each new message from a source chat is forwarded to your target group. Messages posted while it is not running are not forwarded.

## Setup

1. Install Python 3.12 and the dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Get an API ID and API hash for your account at [my.telegram.org](https://my.telegram.org) (API development tools).
3. Copy `.env.example` to `.env` and fill in `API_ID` and `API_HASH`.
4. Find the IDs of your chats:

   ```
   python get_groupId.py
   ```

   It prints every chat you are in with its ID. The first run asks for your phone number and the login code Telegram sends you.

5. In `.env`, put the IDs of the chats to forward from in `SOURCE_GROUP_IDS` (comma-separated), and the ID of your own group in `TARGET_GROUP_ID`.
6. Start the forwarder (it asks you to log in once on its first run):

   ```
   python group_forwarder.py
   ```

## Configuration

All settings live in `.env`:

| Setting | What it is |
|---|---|
| `API_ID` | Your API ID from my.telegram.org |
| `API_HASH` | Your API hash from my.telegram.org |
| `SOURCE_GROUP_IDS` | Chats to forward from, comma-separated |
| `TARGET_GROUP_ID` | The group that receives the forwarded messages |

## Files

| File | What it does |
|---|---|
| `group_forwarder.py` | The forwarder |
| `get_groupId.py` | Lists your chats and their IDs |
| `.env.example` | Template for your `.env` |

## Keep these private

`.env` and the `*.session` files are ignored by git. A `.session` file is a logged-in Telegram session: anyone who has a copy can use your account, so never share or commit it. If one leaks, end the session in Telegram under Settings > Devices.

## Notes

- Chats that have "Restrict saving content" turned on cannot be forwarded from.
- The script acts as your personal account, so use it within [Telegram's Terms of Service](https://telegram.org/tos) and respect the owners of the content you forward.

## License

MIT, see [LICENSE](LICENSE).
