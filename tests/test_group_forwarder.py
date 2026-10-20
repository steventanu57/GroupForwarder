"""Runs the real group_forwarder.py with Telethon's client replaced by a recorder:
no login, no network. Set GF_DIR to test a copy somewhere else (default: the repo root)."""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import pytest
import telethon
from telethon import errors, types

TARGET = -1009999999999
SOURCE_A, SOURCE_B = -1001111111111, -1002222222222
SOURCES = f"{SOURCE_A},{SOURCE_B}"
ENV = {
    "API_ID": "123",
    "API_HASH": "zz" * 16,
    "SOURCE_GROUP_IDS": SOURCES,
    "TARGET_GROUP_ID": str(TARGET),
}
CHAT_NAMES = {TARGET: "zz-Target", SOURCE_A: "zz-Source A", SOURCE_B: "zz-Source B"}


def gf_dir():
    return Path(os.environ.get("GF_DIR") or Path(__file__).resolve().parents[1])


def script_path():
    return gf_dir() / "group_forwarder.py"


class RecordingClient:
    """Stands in for TelegramClient and records what the script asks it to do.
    Printed lines are recorded too, as ("print", text), so their order can be checked."""

    unknown_peers: ClassVar[set] = set()
    # Chats this account has left: the .session file still knows them (get_input_entity works),
    # but they're not in the chat list any more
    left_chats: ClassVar[set] = set()

    def __init__(self, session, api_id, api_hash, **kwargs):
        self.calls, self.handlers, self.kwargs = [], [], kwargs
        self.loop = asyncio.new_event_loop()

    def on(self, event):
        def register(func):
            self.handlers.append((event, func))
            return func

        return register

    def start(self):
        self.calls.append("start")
        return self

    async def get_dialogs(self, *a, **k):
        self.calls.append("get_dialogs")
        return [
            SimpleNamespace(id=chat_id, name=name)
            for chat_id, name in CHAT_NAMES.items()
            if chat_id not in self.unknown_peers | self.left_chats
        ]

    async def get_input_entity(self, peer):
        self.calls.append(("get_input_entity", peer))
        if peer in self.unknown_peers:
            raise ValueError(f"Could not find the input entity for {peer}")
        return peer

    async def forward_messages(self, target, message):
        self.calls.append(("forward", target, message))

    def run_until_disconnected(self):
        self.calls.append("run_until_disconnected")

    def handler_for(self, event_type):
        return next(
            func for builder, func in self.handlers if isinstance(builder, event_type)
        )

    def printed(self):
        return [c[1] for c in self.calls if c[0] == "print"]


def run_script(unknown_peers=(), env=None, left_chats=()):
    script = script_path()
    RecordingClient.unknown_peers = set(unknown_peers)
    RecordingClient.left_chats = set(left_chats)
    created = []

    def make_client(*a, **k):
        created.append(RecordingClient(*a, **k))
        return created[-1]

    def record_print(*args, **kwargs):
        if created:
            created[-1].calls.append(("print", " ".join(str(a) for a in args)))

    with (
        mock.patch.object(telethon, "TelegramClient", make_client),
        mock.patch.dict(os.environ, {**ENV, **(env or {})}),
        mock.patch("builtins.print", record_print),
    ):
        exec(  # noqa: S102 - runs the script under test
            compile(script.read_text(encoding="utf-8"), str(script), "exec"),
            {"__file__": str(script), "__name__": "__main__"},
        )
    return created[0]


def handle(client, event_type, event):
    """Runs one handler the way Telethon would, with print() still recorded."""

    def record_print(*args, **kwargs):
        client.calls.append(("print", " ".join(str(a) for a in args)))

    with mock.patch("builtins.print", record_print):
        client.loop.run_until_complete(client.handler_for(event_type)(event))


class FakeEvent:
    def __init__(self, sender, grouped_id=None, chat_id=SOURCE_A):
        self.message, self.grouped_id, self.chat_id = "zz-message", grouped_id, chat_id
        self.sender = sender

    async def get_sender(self):
        return self.sender


class FakeAlbum(FakeEvent):
    def __init__(self, sender, size):
        super().__init__(sender, grouped_id=42)
        self.size, self.forwarded_to = size, []

    def __len__(self):
        return self.size

    async def forward_to(self, target):
        self.forwarded_to.append(target)


ALICE = types.User(id=1, first_name="zz-Alice")
NewMessage, Album = telethon.events.NewMessage, telethon.events.Album


# ---------- features that must keep working ----------


def test_new_message_is_forwarded_to_target():
    client = run_script()
    event_builder, handler = client.handlers[0]
    assert event_builder.chats == [SOURCE_A, SOURCE_B]
    client.loop.run_until_complete(handler(FakeEvent(ALICE)))
    assert ("forward", TARGET, "zz-message") in client.calls


# ---------- bug fixes ----------


def test_dialogs_are_loaded_before_listening():
    calls = run_script().calls
    assert "get_dialogs" in calls and calls.index("get_dialogs") < calls.index(
        "run_until_disconnected"
    ), calls


def test_clear_error_when_account_is_not_in_target_group():
    with pytest.raises(SystemExit, match="TARGET_GROUP_ID"):
        run_script(unknown_peers={TARGET})


def test_album_is_forwarded_in_one_piece():
    client = run_script()
    album_handler = client.handler_for(Album)  # fails if there is no album handler
    # each album item also arrives as a NewMessage: it must not be forwarded on its own
    client.loop.run_until_complete(
        client.handler_for(NewMessage)(FakeEvent(ALICE, grouped_id=42))
    )
    assert not [c for c in client.calls if c[0] == "forward"]
    album = FakeAlbum(ALICE, size=3)
    client.loop.run_until_complete(album_handler(album))
    assert album.forwarded_to == [TARGET]


def test_target_in_sources_is_refused():
    with pytest.raises(SystemExit, match="loop"):
        run_script(env={"SOURCE_GROUP_IDS": f"{SOURCES},{TARGET}"})


def emoji_survives_log_file(probe, tmp_path):
    env = {
        k: v
        for k, v in {**os.environ, **ENV}.items()
        if k not in ("PYTHONIOENCODING", "PYTHONUTF8")
    }
    with open(tmp_path / "zz-log.txt", "w") as log:
        result = subprocess.run(
            [sys.executable, "-B", str(probe)],
            stdout=log,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
    assert result.returncode == 0, (
        result.stderr.decode(errors="replace").strip().splitlines()[-1]
    )
    return (tmp_path / "zz-log.txt").read_text(encoding="utf-8")


def test_emoji_name_printed_to_a_log_file(tmp_path):
    config_part = (
        script_path().read_text(encoding="utf-8").split("# ======== SETUP ========")[0]
    )
    probe = tmp_path / "zz_probe.py"
    probe.write_text(
        config_part + "\nprint('Forwarded message from zz-\\U0001f680 Channel')\n",
        encoding="utf-8",
    )
    emoji_survives_log_file(probe, tmp_path)


def test_get_groupid_emoji_name_printed_to_a_log_file(tmp_path):
    # get_groupId.py > chats.txt used to stop at the first chat with an emoji in its name
    probe = tmp_path / "zz_probe.py"
    probe.write_text(
        "import asyncio, types, telethon\n"
        "class Fake:\n"
        "    def __init__(self, *a, **k): self.loop = asyncio.new_event_loop()\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *e): return False\n"
        "    async def iter_dialogs(self):\n"
        "        for name, i in [('zz-\\U0001f680 Calls', -1001), ('zz-After', -1002)]:\n"
        "            yield types.SimpleNamespace(name=name, id=i)\n"
        "telethon.TelegramClient = Fake\n"
        f"path = {str(gf_dir() / 'get_groupId.py')!r}\n"
        "exec(compile(open(path, encoding='utf-8').read(), path, 'exec'), {'__file__': path, '__name__': '__main__'})\n",
        encoding="utf-8",
    )
    log = emoji_survives_log_file(probe, tmp_path)
    assert "zz-After: -1002" in log, log


def test_keeps_reconnecting_after_an_outage():
    # Telethon's default gives up after 5 failed reconnects and the script stops for good
    assert run_script().kwargs.get("connection_retries", 5) is None


def test_unreachable_source_is_reported():
    printed = run_script(unknown_peers={SOURCE_A}).printed()
    assert any(
        str(SOURCE_A) in line and "isn't in your chat list" in line for line in printed
    ), printed


def test_source_chat_you_have_left_is_reported():
    # The .session file still remembers a chat you left, so looking it up works,
    # but Telegram no longer sends its messages
    printed = run_script(left_chats={SOURCE_A}).printed()
    assert any(
        str(SOURCE_A) in line and "isn't in your chat list" in line for line in printed
    ), printed
    assert not any(line.strip() == f"- {SOURCE_A}" for line in printed), printed


def test_no_reachable_source_is_refused():
    with pytest.raises(SystemExit, match="SOURCE_GROUP_IDS"):
        run_script(unknown_peers={SOURCE_A, SOURCE_B})


def test_bad_chat_id_gives_a_clear_message():
    with pytest.raises(SystemExit, match="get_groupId"):
        run_script(env={"SOURCE_GROUP_IDS": f"{SOURCE_A},@zz_channel"})


def test_empty_source_list_is_refused():
    with pytest.raises(SystemExit, match="SOURCE_GROUP_IDS"):
        run_script(env={"SOURCE_GROUP_IDS": ","})


def test_running_message_comes_after_the_checks():
    calls = run_script().calls
    running = next(
        i
        for i, c in enumerate(calls)
        if c[0] == "print" and "Forwarder is running" in c[1]
    )
    assert running > calls.index("get_dialogs"), calls


def test_failed_forward_is_reported_and_does_not_crash_the_handler():
    client = run_script()

    async def restricted(*a, **k):
        raise errors.ChatForwardsRestrictedError(request=None)

    client.forward_messages = restricted
    handle(client, NewMessage, FakeEvent(ALICE))
    assert any("restricts forwarding" in line for line in client.printed()), (
        client.printed()
    )


def test_long_flood_wait_is_waited_out_then_retried():
    client = run_script()
    attempts = []

    async def flood_once(*a, **k):
        attempts.append(a)
        if len(attempts) == 1:
            raise errors.FloodWaitError(request=None, capture=0)

    client.forward_messages = flood_once
    handle(client, NewMessage, FakeEvent(ALICE))
    assert len(attempts) == 2, client.printed()


def test_log_line_names_time_source_and_target():
    client = run_script()
    handle(client, NewMessage, FakeEvent(ALICE))
    assert any(
        re.fullmatch(
            r"\[\d\d:\d\d:\d\d\] zz-Source A -> zz-Target: message from zz-Alice", line
        )
        for line in client.printed()
    ), client.printed()


def test_sender_name_needs_no_extra_request():
    client = run_script()
    event = FakeEvent(ALICE)

    async def would_ask_telegram():
        raise AssertionError("get_sender() can make an extra request to Telegram")

    event.get_sender = would_ask_telegram
    handle(client, NewMessage, event)
    assert ("forward", TARGET, "zz-message") in client.calls
    assert not any("Could not forward" in line for line in client.printed())
