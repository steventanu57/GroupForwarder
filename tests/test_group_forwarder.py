"""Runs the real group_forwarder.py with Telethon's client replaced by a recorder:
no login, no network. Set GF_DIR to test a copy somewhere else (default: the repo root)."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar
from unittest import mock

import pytest
import telethon
from telethon import types

TARGET = -1009999999999
SOURCES = "-1001111111111,-1002222222222"
ENV = {
    "API_ID": "123",
    "API_HASH": "zz" * 16,
    "SOURCE_GROUP_IDS": SOURCES,
    "TARGET_GROUP_ID": str(TARGET),
}


def script_path():
    return (
        Path(os.environ.get("GF_DIR") or Path(__file__).resolve().parents[1])
        / "group_forwarder.py"
    )


class RecordingClient:
    """Stands in for TelegramClient and records what the script asks it to do."""

    unknown_peers: ClassVar[set] = set()

    def __init__(self, session, api_id, api_hash):
        self.calls, self.handlers = [], []
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
        return []

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


def run_script(unknown_peers=(), env=None):
    script = script_path()
    RecordingClient.unknown_peers = set(unknown_peers)
    created = []

    def make_client(*a, **k):
        created.append(RecordingClient(*a, **k))
        return created[-1]

    with (
        mock.patch.object(telethon, "TelegramClient", make_client),
        mock.patch.dict(os.environ, {**ENV, **(env or {})}),
    ):
        exec(  # noqa: S102 - runs the script under test
            compile(script.read_text(encoding="utf-8"), str(script), "exec"),
            {"__file__": str(script), "__name__": "__main__"},
        )
    return created[0]


class FakeEvent:
    def __init__(self, sender, grouped_id=None):
        self.message, self.grouped_id, self._sender = "zz-message", grouped_id, sender

    async def get_sender(self):
        return self._sender


class FakeAlbum(FakeEvent):
    def __init__(self, sender, size):
        super().__init__(sender, grouped_id=42)
        self.size, self.forwarded_to = size, []

    def __len__(self):
        return self.size

    async def forward_to(self, target):
        self.forwarded_to.append(target)


ALICE = types.User(id=1, first_name="zz-Alice")


# ---------- features that must keep working ----------


def test_new_message_is_forwarded_to_target():
    client = run_script()
    event_builder, handler = client.handlers[0]
    assert event_builder.chats == [-1001111111111, -1002222222222]
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
    album_handler = client.handler_for(
        telethon.events.Album
    )  # fails if there is no album handler
    # each album item also arrives as a NewMessage: it must not be forwarded on its own
    client.loop.run_until_complete(
        client.handler_for(telethon.events.NewMessage)(FakeEvent(ALICE, grouped_id=42))
    )
    assert not [c for c in client.calls if c[0] == "forward"]
    album = FakeAlbum(ALICE, size=3)
    client.loop.run_until_complete(album_handler(album))
    assert album.forwarded_to == [TARGET]


def test_target_in_sources_is_refused():
    with pytest.raises(SystemExit, match="loop"):
        run_script(env={"SOURCE_GROUP_IDS": f"{SOURCES},{TARGET}"})


def test_emoji_name_printed_to_a_log_file(tmp_path):
    config_part = (
        script_path().read_text(encoding="utf-8").split("# ======== SETUP ========")[0]
    )
    probe = tmp_path / "zz_probe.py"
    probe.write_text(
        config_part + "\nprint('Forwarded message from zz-\\U0001f680 Channel')\n",
        encoding="utf-8",
    )
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
