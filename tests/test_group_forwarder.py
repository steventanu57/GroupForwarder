"""Runs the real group_forwarder.py with Telethon's client replaced by a recorder:
no login, no network. Each run uses a copy of the script in a new temporary folder, so the
tests never read your .env or forwarder_config.toml, and never touch the files of a forwarder
that is running. Set GF_DIR to test a copy somewhere else (default: the repo root)."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import pytest
import telethon
from telethon import errors, types

TARGET, TARGET2 = -1009999999999, -1008888888888
SOURCE_A, SOURCE_B, SOURCE_C = -1001111111111, -1002222222222, -1003333333333
SOURCES = f"{SOURCE_A},{SOURCE_B}"
ENV = {
    "API_ID": "123",
    "API_HASH": "zz" * 16,
    "SOURCE_GROUP_IDS": SOURCES,
    "TARGET_GROUP_ID": str(TARGET),
}
CHAT_NAMES = {
    TARGET: "zz-Target",
    TARGET2: "zz-Target 2",
    SOURCE_A: "zz-Source A",
    SOURCE_B: "zz-Source B",
    SOURCE_C: "zz-Source C",
}


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
    protected: ClassVar[set] = set()  # Chats with "Restrict saving content"
    authorized: ClassVar[bool] = True
    history: ClassVar[dict] = {}  # Chat ID -> messages, oldest first (for catch-up)
    created: ClassVar[list] = []  # Every client made, also when the script stops early

    def __init__(self, session, api_id, api_hash, **kwargs):
        self.calls, self.handlers, self.kwargs = [], [], kwargs
        self.loop = asyncio.new_event_loop()
        RecordingClient.created.append(self)

    def on(self, event):
        def register(func):
            self.handlers.append((event, func))
            return func

        return register

    async def connect(self):
        self.calls.append("connect")

    async def is_user_authorized(self):
        return self.authorized

    def start(self):
        self.calls.append("start")
        return self

    async def disconnect(self):
        self.calls.append("disconnect")

    async def get_dialogs(self, *a, **k):
        self.calls.append("get_dialogs")
        return [
            SimpleNamespace(
                id=chat_id,
                name=name,
                entity=SimpleNamespace(noforwards=chat_id in self.protected),
                is_channel=True,
                is_group=chat_id == TARGET,
                is_user=False,
            )
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

    async def send_message(self, entity, message=None, **kwargs):
        self.calls.append(("send", entity, message, kwargs))

    async def send_file(self, entity, file, **kwargs):
        self.calls.append(("send_file", entity, file, kwargs))

    async def __call__(self, request):
        self.calls.append(("raw", request))

    async def get_messages(self, chat, limit=1):
        return list(reversed(self.history.get(chat, [])))[:limit]

    async def iter_messages(self, chat, min_id=0, limit=None):
        newest_first = [
            m for m in reversed(self.history.get(chat, [])) if m.id > min_id
        ]
        for message in newest_first[:limit]:
            yield message

    def run_until_disconnected(self):
        self.calls.append("run_until_disconnected")

    def handler_for(self, event_type):
        return next(
            func
            for builder, func in self.handlers
            if isinstance(builder, event_type) and builder.chats != "me"
        )

    def command(self, text):
        """Runs the Saved Messages command handler as Telethon would for this text."""
        builder, func = next((b, f) for b, f in self.handlers if b.chats == "me")
        event = SimpleNamespace(pattern_match=builder.pattern(text))
        assert event.pattern_match, f"{text} is not a command"
        handle(self, func, event)

    def printed(self):
        return [c[1] for c in self.calls if c[0] == "print"]

    def forwards(self):
        """Everything sent to a target (forwards, copies, raw forward requests)."""
        return [
            c
            for c in self.calls
            if c[0] in ("forward", "send_file", "raw")
            or (c[0] == "send" and c[1] != "me")
        ]

    def saved_messages(self):
        return [c[2] for c in self.calls if c[0] == "send" and c[1] == "me"]

    def script(self, name):
        # A function or value of the script under test (the handlers keep its globals)
        return self.handler_for(telethon.events.NewMessage).__globals__[name]


FOLDERS = []  # Temporary folders of this test, deleted when it ends


@pytest.fixture(autouse=True)
def delete_temporary_folders():
    yield
    while FOLDERS:
        shutil.rmtree(FOLDERS.pop(), ignore_errors=True)


def run_script(
    unknown_peers=(),
    env=None,
    left_chats=(),
    config=None,
    argv=(),
    files=None,
    authorized=True,
    history=None,
    protected=(),
    lock_held=False,
):
    """Runs a fresh copy of the script in a new temporary folder (its BASE_DIR) and returns the client.
    config: text for forwarder_config.toml; files: {name: text} for other files in that folder."""
    folder = Path(tempfile.mkdtemp(prefix="zz-gf-"))
    FOLDERS.append(folder)
    script = folder / "group_forwarder.py"
    shutil.copy(script_path(), script)
    if config is not None:
        (folder / "forwarder_config.toml").write_text(config, encoding="utf-8")
    for name, text in (files or {}).items():
        (folder / name).write_text(text, encoding="utf-8")
    RecordingClient.unknown_peers = set(unknown_peers)
    RecordingClient.left_chats = set(left_chats)
    RecordingClient.protected = set(protected)
    RecordingClient.authorized = authorized
    RecordingClient.history = history or {}
    RecordingClient.created = []
    lock = hold_lock(folder / "group_forwarder.lock") if lock_held else None

    def record_print(*args, **kwargs):
        if RecordingClient.created:
            RecordingClient.created[-1].calls.append(
                ("print", " ".join(str(a) for a in args))
            )

    namespace = {"__file__": str(script), "__name__": "__main__"}
    try:
        with (
            mock.patch.object(telethon, "TelegramClient", RecordingClient),
            mock.patch.dict(os.environ, {**ENV, **(env or {})}),
            mock.patch("builtins.print", record_print),
            mock.patch.object(sys, "argv", [str(script), *argv]),
        ):
            exec(  # noqa: S102 - runs the script under test
                compile(script.read_text(encoding="utf-8"), str(script), "exec"),
                namespace,
            )
    finally:
        for held in (lock, namespace.get("single_copy")):
            if held:
                held.close()  # So the folder can be deleted (Windows keeps locked files)
    client = RecordingClient.created[0]
    client.folder = folder
    return client


def last_client():
    return RecordingClient.created[0]


def hold_lock(path):
    # Like another copy of the forwarder holding the lock
    handle_file = open(path, "a+")  # noqa: SIM115
    if os.name == "nt":
        import msvcrt

        handle_file.seek(0)
        msvcrt.locking(handle_file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle_file


def handle(client, func, event):
    """Runs one handler the way Telethon would, with print() still recorded."""

    def record_print(*args, **kwargs):
        client.calls.append(("print", " ".join(str(a) for a in args)))

    with mock.patch("builtins.print", record_print):
        client.loop.run_until_complete(func(event))


def send(client, event):
    handle(client, client.handler_for(NewMessage), event)


class FakeMessage:
    def __init__(
        self,
        text="zz-message",
        message_id=1,
        media=None,
        out=False,
        noforwards=False,
        fwd_from=None,
        grouped_id=None,
        date=None,
        entities=None,
        sender=None,
        action=None,
    ):
        self.action = (
            action  # Set for service messages ("joined", "pinned a message", ...)
        )
        self.message, self.id, self.media, self.out = text, message_id, media, out
        self.noforwards, self.fwd_from = noforwards, fwd_from
        self.grouped_id = grouped_id
        self.date = date or datetime.now(UTC)
        self.entities, self.sender = entities or [], sender


class FakeEvent:
    def __init__(self, sender, grouped_id=None, chat_id=SOURCE_A, message="zz-message"):
        self.message, self.grouped_id, self.chat_id = message, grouped_id, chat_id
        self.sender = sender

    async def get_sender(self):
        return self.sender


class FakeAlbum(FakeEvent):
    def __init__(self, sender, size, chat_id=SOURCE_A):
        super().__init__(sender, grouped_id=42, chat_id=chat_id)
        self.size, self.forwarded_to = size, []
        self.messages = [
            FakeMessage(f"zz-item {i}", message_id=i, grouped_id=42)
            for i in range(1, size + 1)
        ]

    def __len__(self):
        return self.size

    async def forward_to(self, target):
        self.forwarded_to.append(target)


ALICE = types.User(id=1, first_name="zz-Alice")
ROBOT = types.User(id=2, first_name="zz-Robot", bot=True)
NewMessage, Album = telethon.events.NewMessage, telethon.events.Album
PHOTO = types.MessageMediaPhoto(photo=types.Photo(1, 2, b"", datetime.now(UTC), [], 2))


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


def config_part():
    return (
        script_path().read_text(encoding="utf-8").split("# ======== SETUP ========")[0]
    )


def test_emoji_name_printed_to_a_log_file(tmp_path):
    probe = tmp_path / "zz_probe.py"
    probe.write_text(
        config_part() + "\nprint('Forwarded message from zz-\\U0001f680 Channel')\n",
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
    send(client, FakeEvent(ALICE))
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
    send(client, FakeEvent(ALICE))
    assert len(attempts) == 2, client.printed()


def test_log_line_names_time_source_and_target():
    client = run_script()
    send(client, FakeEvent(ALICE))
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
    send(client, event)
    assert ("forward", TARGET, "zz-message") in client.calls
    assert not any("Could not forward" in line for line in client.printed())


# ---------- phase 3: config file, routes, filters, copy mode, catch-up, commands, auto-start ----------


def msg_event(text="zz-hello", chat_id=SOURCE_A, sender=ALICE, **message_kwargs):
    return FakeEvent(
        sender,
        chat_id=chat_id,
        message=FakeMessage(text, sender=sender, **message_kwargs),
    )


def targets_of(client):
    return [c[1] for c in client.forwards()]


def texts_forwarded(client):
    return [c[2].message for c in client.forwards()]


def test_without_a_config_file_everything_is_as_before():  # Config file
    client = run_script()
    assert any("Settings: .env only" in line for line in client.printed())
    assert client.kwargs.get("sequential_updates") is False
    send(client, msg_event(out=True))  # Your own messages are still forwarded
    assert targets_of(client) == [TARGET]


def test_config_typo_is_refused_with_a_clear_message():  # Config file
    with pytest.raises(SystemExit, match="Unknown setting 'skip_own_mesages'"):
        run_script(config="[options]\nskip_own_mesages = true\n")


def test_config_wrong_kind_of_value_is_refused():  # Config file
    with pytest.raises(SystemExit, match="must be true or false"):
        run_script(config='[options]\ncatch_up = "yes"\n')
    with pytest.raises(SystemExit, match="whole number above 0"):
        run_script(config="[options]\ncatch_up_hours = 0\n")


def test_config_with_a_syntax_mistake_is_refused():  # Config file
    with pytest.raises(SystemExit, match="forwarder_config.toml has a mistake"):
        run_script(config="[options]\ncopy_mode = tru\n")


def test_config_daily_summary_time_is_checked():  # Config file
    with pytest.raises(SystemExit, match="daily_summary_time"):
        run_script(config='[options]\ndaily_summary_time = "25:00"\n')


def test_example_config_changes_nothing():  # Config file
    example = (gf_dir() / "forwarder_config.example.toml").read_text(encoding="utf-8")
    client = run_script(config=example)
    assert any("nothing changed from the defaults" in line for line in client.printed())
    assert len(client.script("routes")) == 1


def test_target_you_have_left_is_refused():  # G23
    with pytest.raises(SystemExit, match="isn't in your chat list"):
        run_script(left_chats={TARGET})


def test_extra_route_sends_its_sources_to_its_target():  # G18
    client = run_script(
        config=f"[[routes]]\nsources = [{SOURCE_C}]\ntarget = {TARGET2}\n"
    )
    assert client.handlers[0][0].chats == [SOURCE_A, SOURCE_B, SOURCE_C]
    send(client, msg_event(chat_id=SOURCE_C))
    send(client, msg_event(chat_id=SOURCE_A))
    assert targets_of(client) == [TARGET2, TARGET]


def test_route_into_a_forum_topic():  # G18
    client = run_script(
        config=f"[[routes]]\nsources = [{SOURCE_C}]\ntarget = {TARGET2}\ntopic = 7\n"
    )
    send(client, msg_event(chat_id=SOURCE_C, message_id=55))
    (raw,) = [c[1] for c in client.calls if c[0] == "raw"]
    assert (raw.top_msg_id, raw.id, raw.to_peer, raw.from_peer) == (
        7,
        [55],
        TARGET2,
        SOURCE_C,
    )


def test_route_target_that_is_a_source_is_refused():  # G18
    with pytest.raises(SystemExit, match="endless loop"):
        run_script(config=f"[[routes]]\nsources = [{SOURCE_C}]\ntarget = {SOURCE_A}\n")


def test_route_target_not_in_your_chats_is_refused():  # G18 + G23
    with pytest.raises(SystemExit, match=r"\[\[routes\]\] #1"):
        run_script(
            config=f"[[routes]]\nsources = [{SOURCE_C}]\ntarget = -1007777777777\n"
        )


def test_keyword_filters():  # G17
    client = run_script(
        config='[filters]\ninclude_keywords = ["btc"]\nexclude_keywords = ["giveaway"]\n'
    )
    for text in ["zz #BTCUSD long", "zz eth only", "zz btc giveaway"]:
        send(client, msg_event(text))
    assert texts_forwarded(client) == ["zz #BTCUSD long"]


def test_media_bot_and_repost_filters():  # G17
    client = run_script(
        config="[filters]\nmedia_only = true\nskip_bots = true\nskip_reposts = true\n"
    )
    repost = types.MessageFwdHeader(date=datetime.now(UTC))
    send(client, msg_event("zz text only"))
    send(client, msg_event("zz photo by a bot", sender=ROBOT, media=PHOTO))
    send(client, msg_event("zz repost", media=PHOTO, fwd_from=repost))
    send(client, msg_event("zz photo", media=PHOTO))
    assert texts_forwarded(client) == ["zz photo"]


def test_a_route_can_have_its_own_filters():  # G17 + G18
    client = run_script(
        config='[filters]\ninclude_keywords = ["btc"]\n'
        f"[[routes]]\nsources = [{SOURCE_A}]\ntarget = {TARGET2}\n"
        'include_keywords = ["eth"]\n'
    )
    send(client, msg_event("zz eth news"))
    assert targets_of(client) == [TARGET2]


def test_own_messages_can_be_skipped():  # G8
    client = run_script(config="[options]\nskip_own_messages = true\n")
    send(client, msg_event("zz mine", out=True))
    send(client, msg_event("zz theirs"))
    assert texts_forwarded(client) == ["zz theirs"]


def test_strict_order_option():  # G11
    client = run_script(config="[options]\nstrict_order = true\n")
    assert client.kwargs["sequential_updates"] is True


def test_copy_mode_sends_a_copy_with_the_source_name():  # G12/G20
    client = run_script(config="[options]\ncopy_mode = true\n")
    bold = types.MessageEntityBold(offset=3, length=4)
    send(client, msg_event("zz bold & <done>", entities=[bold], media=PHOTO))
    ((kind, target, text, kwargs),) = client.forwards()
    assert (kind, target) == ("send", TARGET)
    assert (
        text == "📣 <b>zz-Source A</b>\n\nzz <strong>bold</strong> &amp; &lt;done&gt;"
    )
    assert kwargs["file"] is PHOTO and kwargs["parse_mode"] == "html"
    assert any(line.endswith("(copied)") for line in client.printed())


def test_copy_mode_keeps_albums_together():  # G12/G20
    client = run_script(config="[options]\ncopy_mode = true\n")
    handle(client, client.handler_for(Album), FakeAlbum(ALICE, size=2))
    ((kind, target, files, kwargs),) = client.forwards()
    assert (kind, target, len(files)) == ("send_file", TARGET, 2)
    assert kwargs["caption"] == ["📣 <b>zz-Source A</b>\n\nzz-item 1", "zz-item 2"]


def test_copy_mode_never_copies_protected_chats():  # G12/G20
    client = run_script(config="[options]\ncopy_mode = true\n", protected={SOURCE_A})
    send(client, msg_event())
    assert client.forwards() == []
    assert any("restricts forwarding" in line for line in client.printed())


def test_copy_that_fails_is_forwarded_instead():  # G12/G20
    client = run_script(config="[options]\ncopy_mode = true\n")

    async def too_long(*a, **k):
        raise errors.MediaCaptionTooLongError(request=None)

    client.send_message = too_long
    send(client, msg_event())
    assert [c[0] for c in client.forwards()] == ["forward"]
    assert any("forwarding it instead" in line for line in client.printed())


def test_copy_into_a_topic():  # G12/G20 + G18
    client = run_script(
        config="[options]\ncopy_mode = true\n"
        f"[[routes]]\nsources = [{SOURCE_C}]\ntarget = {TARGET2}\ntopic = 7\n"
    )
    send(client, msg_event(chat_id=SOURCE_C))
    ((_, target, _, kwargs),) = client.forwards()
    assert (target, kwargs["reply_to"]) == (TARGET2, 7)


def test_duplicate_posts_are_skipped():  # G19
    client = run_script(config="[options]\nskip_duplicates = true\n")
    send(client, msg_event("zz same news", chat_id=SOURCE_A))
    send(client, msg_event("zz same news", chat_id=SOURCE_B))
    send(client, msg_event("zz other news", chat_id=SOURCE_B))
    assert texts_forwarded(client) == ["zz same news", "zz other news"]
    assert any("skipped a duplicate" in line for line in client.printed())


def test_duplicates_are_only_skipped_within_the_window():  # G19
    client = run_script(
        config="[options]\nskip_duplicates = true\nduplicate_window_hours = 1\n"
    )
    send(client, msg_event("zz same news"))
    send(
        client, msg_event("zz same news", chat_id=SOURCE_B)
    )  # Within the hour: skipped
    assert len(client.forwards()) == 1
    later = time.time() + 3601
    with mock.patch.object(time, "time", lambda: later):
        send(client, msg_event("zz same news", chat_id=SOURCE_B))  # An hour later: sent
    assert len(client.forwards()) == 2


def test_duplicates_are_forwarded_without_the_option():  # G19
    client = run_script()
    send(client, msg_event("zz same news"))
    send(client, msg_event("zz same news", chat_id=SOURCE_B))
    assert len(client.forwards()) == 2


CATCH_UP = "[options]\ncatch_up = true\n"


def old_message(message_id, hours_ago, **kwargs):
    when = datetime.fromtimestamp(time.time() - hours_ago * 3600, UTC)
    return FakeMessage(
        f"zz-{message_id}", message_id=message_id, date=when, sender=ALICE, **kwargs
    )


def bookmark_file(client):
    return json.loads((client.folder / "forwarder_state.json").read_text())


def test_catch_up_first_run_only_remembers_where_chats_are():  # G9
    client = run_script(config=CATCH_UP, history={SOURCE_A: [old_message(50, 1)]})
    assert client.forwards() == []
    assert bookmark_file(client) == {str(SOURCE_A): 50}


def test_catch_up_forwards_missed_messages():  # G9
    # 30 missed messages, one every 15 minutes (all within 6 h of the newest 20):
    # only the newest 20 are fetched, and they are sent oldest first
    history = [old_message(100 + i, (30 - i) * 0.25) for i in range(1, 31)]
    client = run_script(
        config=CATCH_UP,
        history={SOURCE_A: history},
        files={"forwarder_state.json": json.dumps({str(SOURCE_A): 100})},
    )
    assert [c[2].id for c in client.forwards()] == list(range(111, 131))
    assert any("missed message from zz-Alice" in line for line in client.printed())
    assert bookmark_file(client)[str(SOURCE_A)] == 130


def test_catch_up_skips_messages_older_than_the_limit():  # G9
    client = run_script(
        config=CATCH_UP,
        history={SOURCE_A: [old_message(101, 7), old_message(102, 1)]},
        files={"forwarder_state.json": json.dumps({str(SOURCE_A): 100})},
    )
    assert [c[2].id for c in client.forwards()] == [102]
    assert bookmark_file(client)[str(SOURCE_A)] == 102


def test_catch_up_keeps_albums_together():  # G9
    history = [old_message(101, 1, grouped_id=9), old_message(102, 1, grouped_id=9)]
    client = run_script(
        config=CATCH_UP,
        history={SOURCE_A: history},
        files={"forwarder_state.json": json.dumps({str(SOURCE_A): 100})},
    )
    ((_, _, messages),) = client.forwards()
    assert [m.id for m in messages] == [101, 102]


def test_message_seen_in_catch_up_is_not_sent_again_live():  # G9
    client = run_script(
        config=CATCH_UP,
        history={SOURCE_A: [old_message(101, 1)]},
        files={"forwarder_state.json": json.dumps({str(SOURCE_A): 100})},
    )
    assert [c[2].id for c in client.forwards()] == [101]  # Sent by catch-up
    send(client, msg_event("zz-101", message_id=101))  # ... then it also arrives live
    assert len(client.forwards()) == 1


def test_live_messages_move_the_catch_up_bookmark():  # G9
    client = run_script(config=CATCH_UP, history={SOURCE_A: [old_message(50, 1)]})
    send(client, msg_event(message_id=200))
    assert bookmark_file(client)[str(SOURCE_A)] == 200


def test_status_command():  # G16
    client = run_script()
    send(client, msg_event())
    client.command("/status")
    (reply,) = client.saved_messages()
    assert "1 forwarded" in reply and "▶️ forwarding" in reply


def test_pause_and_resume():  # G16
    client = run_script()
    client.command("/pause")
    send(client, msg_event("zz while paused"))
    client.command("/resume")
    send(client, msg_event("zz after resume"))
    assert texts_forwarded(client) == ["zz after resume"]


def test_sources_command_lists_routes():  # G16
    client = run_script(
        config=f"[[routes]]\nsources = [{SOURCE_C}]\ntarget = {TARGET2}\n"
        'include_keywords = ["btc"]\n'
    )
    client.command("/sources")
    (reply,) = client.saved_messages()
    assert "zz-Source A, zz-Source B → zz-Target" in reply
    assert "zz-Source C → zz-Target 2" in reply and "include_keywords" in reply


def test_commands_can_be_switched_off():  # G16
    assert [b for b, _ in run_script().handlers if b.chats == "me"]  # On by default
    client = run_script(config="[options]\ncommands = false\n")
    assert not [b for b, _ in client.handlers if b.chats == "me"]


def test_daily_summary_reports_and_starts_a_new_period():  # G22
    client = run_script()
    send(client, msg_event())

    async def no_rights(*a, **k):
        raise errors.ChatWriteForbiddenError(request=None)

    working = client.forward_messages
    client.forward_messages = no_rights
    send(client, msg_event("zz fails"))
    client.forward_messages = working
    for _ in range(2):
        handle(client, lambda _: client.script("send_summary")(), None)
    first, second = client.saved_messages()
    assert "1 forwarded" in first and "1 failed" in first
    assert "ChatWriteForbiddenError" in first
    assert "0 forwarded" in second and "0 failed" in second


def test_seconds_until_the_next_summary():  # G22
    seconds_until = run_script().script("seconds_until")
    start = time.mktime((2026, 10, 20, 8, 30, 0, 0, 0, -1))  # 08:30 local time
    assert seconds_until("09:00", start) == 1800
    assert seconds_until("08:00", start) == 23.5 * 3600  # Tomorrow


def test_daily_summary_runs_only_when_switched_on():  # G22
    assert not asyncio.all_tasks(run_script().loop)
    client = run_script(config="[options]\ndaily_summary = true\n")
    tasks = asyncio.all_tasks(client.loop)
    assert len(tasks) == 1
    for task in tasks:
        task.cancel()
    client.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))


def test_list_chats_prints_ids_and_stops():  # G14
    # Works before SOURCE_GROUP_IDS and TARGET_GROUP_ID are filled in
    with pytest.raises(SystemExit) as stopped:
        run_script(
            argv=["--list-chats"], env={"SOURCE_GROUP_IDS": "", "TARGET_GROUP_ID": ""}
        )
    assert stopped.value.code == 0
    printed = last_client().printed()
    assert f"zz-Source A: {SOURCE_A} (channel)" in printed
    assert f"zz-Target: {TARGET} (group)" in printed
    assert "run_until_disconnected" not in last_client().calls


def test_second_copy_is_refused():  # G21
    with pytest.raises(SystemExit, match="Another copy"):
        run_script(lock_held=True)


def test_no_login_never_asks_for_a_phone_number():  # G21
    with pytest.raises(SystemExit, match="Not logged in"):
        run_script(argv=["--no-login"], authorized=False)
    assert (
        "start" not in last_client().calls
    )  # start() is what asks for the phone number


def test_unknown_option_is_refused():  # G21
    with pytest.raises(SystemExit, match="Unknown option --list-chat"):
        run_script(argv=["--list-chat"])


def test_log_option_writes_output_to_a_file(tmp_path):  # G21
    log = tmp_path / "zz-forwarder.log"
    probe = tmp_path / "zz_probe.py"
    probe.write_text(
        config_part() + "\nprint('zz-hello \\U0001f680')\n", encoding="utf-8"
    )
    subprocess.run(
        [sys.executable, "-B", str(probe), "--log", str(log)],
        env={**os.environ, **ENV},
        check=True,
    )
    assert "zz-hello 🚀" in log.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="run_forwarder.ps1 is for Windows")
def test_run_forwarder_ps1_restarts_after_a_crash_but_not_after_a_quick_stop(
    tmp_path,
):  # G21
    dummy = tmp_path / "zz_dummy.py"
    dummy.write_text(
        "import sys, time, pathlib\n"
        "runs = pathlib.Path(__file__).with_name('runs.txt')\n"
        "n = int(runs.read_text()) + 1 if runs.exists() else 1\n"
        "runs.write_text(str(n))\n"
        # 1st run: a crash after a while (restarted); 2nd: stops at once (not restarted)
        "time.sleep(3 if n == 1 else 0)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    log = tmp_path / "zz-forwarder.log"
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    command += [str(gf_dir() / "run_forwarder.ps1"), "-QuickExitSeconds", "2"]
    command += ["-RestartDelaySeconds", "1", "-Python", sys.executable]
    command += ["-Script", str(dummy), "-Log", str(log)]
    result = subprocess.run(command, check=False, timeout=60)
    assert (tmp_path / "runs.txt").read_text() == "2"  # Restarted once
    assert "restarting won't help" in log.read_text(encoding="utf-8")
    assert result.returncode == 1


def test_catch_up_skips_service_messages():  # G9
    # "X joined" or "pinned a message" entries come back from iter_messages too, but can't be
    # forwarded (and live forwarding never sees them)
    pinned = types.MessageActionPinMessage()
    history = [old_message(101, 1, action=pinned), old_message(102, 1)]
    client = run_script(
        config=CATCH_UP,
        history={SOURCE_A: history},
        files={"forwarder_state.json": json.dumps({str(SOURCE_A): 100})},
    )
    assert [c[2].id for c in client.forwards()] == [102]
    assert not any("Could not forward" in line for line in client.printed())


def test_log_option_without_a_file_is_refused():  # G21
    with pytest.raises(SystemExit, match="--log needs a file name"):
        run_script(argv=["--log"])


def test_command_reply_that_fails_is_reported_not_raised():  # G16
    client = run_script()

    async def too_long(*a, **k):
        raise errors.MessageTooLongError(request=None)

    client.send_message = too_long
    client.command("/sources")
    assert any("Couldn't answer /sources" in line for line in client.printed())


def test_empty_config_file_is_reported_as_in_use():  # Config file
    client = run_script(config="")
    assert any(
        "Settings: forwarder_config.toml (nothing changed" in line
        for line in client.printed()
    ), client.printed()
