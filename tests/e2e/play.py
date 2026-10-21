"""Runs a copy of the REAL group_forwarder.py (unchanged) with the simulated Telegram server
underneath Telethon, plays one plan (a scenario) and writes its checks to results_<plan>.json.
Started by run_e2e.py: play.py <plan> <folder with the script copy> [forwarder options]"""

import asyncio
import json
import os
import runpy
import sys
import time
import traceback
from pathlib import Path

import telethon
from telegram_sim import BOB, EVIL, ROBOT, SERVER, SimClient, marked, utf16_slice
from telethon import types

PLAN, WORK = sys.argv[1], Path(sys.argv[2])
# Channel ids of the simulated chats
A, B, C, P = 1111111111, 2222222222, 3333333333, 4444444444  # Sources (P is protected)
T, T2, RO = 9999999999, 8888888888, 7777777777  # Targets (T2 is a forum, RO read-only)
LEFT_SRC, LEFT_TGT = 6666666666, 5555555555  # Chats this account has left
results = []


def check(item, name, ok, detail=""):
    results.append(
        {
            "plan": PLAN,
            "item": item,
            "name": name,
            "ok": bool(ok),
            "detail": str(detail)[:600],
        }
    )


class Tee:
    """Everything the forwarder prints goes to a file and to .lines (for the checks)."""

    def __init__(self, path):
        self.file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - open until the end
        self.lines, self.buffer = [], ""

    def write(self, text):
        self.file.write(text)
        self.file.flush()
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.lines.append(line)

    def flush(self):
        self.file.flush()

    def reconfigure(self, **kwargs):
        pass


def world():
    state = WORK / "server_state.pkl"
    if state.exists():  # Plans in the same folder continue the same world
        SERVER.load(state)
        return
    SERVER.add_chat(A, "zz Source A", "channel")
    SERVER.add_chat(B, "zz Source B", "megagroup")
    SERVER.add_chat(C, "zz Source C", "channel")
    SERVER.add_chat(P, "zz Protected", "channel", noforwards=True)
    SERVER.add_chat(T, "zz Target", "megagroup")
    SERVER.add_chat(
        T2,
        "zz Target 2",
        "megagroup",
        forum=True,
        topics={7: "zz Signals", 9: "zz News"},
    )
    SERVER.add_chat(RO, "zz Read Only", "megagroup", can_write=False)
    SERVER.add_chat(LEFT_SRC, "zz Left Source", "channel", member=False)
    SERVER.add_chat(LEFT_TGT, "zz Left Target", "megagroup", member=False)


def arrived(chat, after=0):
    """Messages the forwarder put into a chat (forwarded or copied) after message id `after`."""
    return [
        m
        for m in SERVER.chats[chat]["messages"]
        if m.get("out") and m["id"] > after and (m.get("fwd_from") or m.get("copy"))
    ]


def last_id(chat):
    return max((m["id"] for m in SERVER.chats[chat]["messages"]), default=0)


async def until(condition, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        value = condition()
        if value:
            return value
        await asyncio.sleep(0.1)
    return condition()


def out(contains):
    return [line for line in TEE.lines if contains in line]


def replies(after):
    """The forwarder's answers in Saved Messages after message id `after`."""
    return [
        m["text"] for m in SERVER.saved if m.get("by_forwarder") and m["id"] > after
    ]


def saved_mark():
    return max((m["id"] for m in SERVER.saved), default=0)


def refused_in(title):
    return sum(1 for _, why in SERVER.refused if title in why)


# ---------------- plans ----------------
async def plan_default():
    client = SERVER.client
    await until(lambda: client.running.is_set(), 30)
    await asyncio.sleep(1)
    check(
        "G6",
        "startup lines come after the checks and name the chats",
        out("Settings: .env only")
        and out("Forwarder is running: listening to 3 chat(s), forwarding to zz Target")
        and out("send /help for the commands and setup steps"),
        TEE.lines[:8],
    )
    check(
        "G2",
        "a source you're not a member of gets a warning",
        out(f"source chat {marked(LEFT_SRC)} isn't in your chat list"),
        TEE.lines[:8],
    )
    check(
        "G1",
        "client keeps reconnecting forever (connection_retries=None)",
        SERVER.kwargs.get("connection_retries", 5) is None,
    )
    check(
        "G21",
        "the forwarder holds the single-copy lock file",
        (WORK / "group_forwarder.lock").exists(),
    )

    mark = last_id(T)
    SERVER.post(A, text="zz hello from A", post=True)
    got = await until(lambda: arrived(T, mark))
    check(
        "core",
        "a channel post is forwarded with 'Forwarded from'",
        got and got[0]["fwd_from"][0] == A and got[0]["text"] == "zz hello from A",
        got,
    )
    check(
        "G15",
        "log line names time, source, target and sender",
        any(
            line.startswith("[")
            and line.endswith("zz Source A -> zz Target: message from zz Source A")
            for line in TEE.lines
        ),
        TEE.lines[-3:],
    )

    mark = last_id(T)
    SERVER.post(B, text="zz hi from Bob", **{"from": BOB})
    got = await until(lambda: arrived(T, mark))
    check(
        "core",
        "a group member's message is forwarded, with the sender's name in the log",
        got and out("message from zz-Bob"),
    )

    mark = last_id(T)
    forwards_before = sum(1 for n, _ in SERVER.log if n == "ForwardMessagesRequest")
    SERVER.post(
        A,
        album=[
            {"text": "zz album 1", "photo": 501, "post": True},
            {"text": "", "photo": 502, "post": True},
            {"text": "", "photo": 503, "post": True},
        ],
    )
    await until(lambda: len(arrived(T, mark)) >= 3, 10)
    await asyncio.sleep(1)
    got = arrived(T, mark)
    calls = (
        sum(1 for n, _ in SERVER.log if n == "ForwardMessagesRequest") - forwards_before
    )
    check(
        "core",
        "an album arrives as one album (one forward of 3 items)",
        len(got) == 3
        and got[0].get("grouped")
        and len({m.get("grouped") for m in got}) == 1
        and calls == 1,
        f"{len(got)} messages, {calls} requests",
    )
    check("core", "the log says 'album of 3 items'", out("album of 3 items"))

    mark = last_id(T)
    SERVER.post(A, text="zz my own post", post=True, out=True)
    got = await until(lambda: arrived(T, mark))
    check("G8", "by default your own posts are forwarded too", got)

    mark = last_id(T)
    SERVER.post(A, text="zz pinned", service=True)
    SERVER.post(C, text="zz not a source", post=True)
    await asyncio.sleep(2)
    check(
        "core",
        "service messages and non-source chats are not forwarded",
        not arrived(T, mark),
    )

    lines = len(TEE.lines)
    SERVER.post(P, text="zz secret", post=True)
    await until(
        lambda: any("restricts forwarding" in line for line in TEE.lines[lines:]), 10
    )
    check(
        "G10",
        "a restricted chat is reported, and the forwarder keeps running",
        out(
            "Could not forward message from zz Protected: that chat restricts forwarding"
        ),
    )

    print("  ... waiting out a 61 s rate limit", file=sys.__stdout__, flush=True)
    SERVER.flood_once["ForwardMessagesRequest"] = 61
    mark = last_id(T)
    started = time.time()
    SERVER.post(A, text="zz after flood", post=True)
    got = await until(lambda: arrived(T, mark), 90)
    check(
        "G10",
        "a rate limit over 60 s is waited out, then the forward is retried",
        got and time.time() - started >= 60 and out("Telegram asked to wait 61 s"),
        f"{time.time() - started:.0f} s",
    )

    m0 = saved_mark()
    SERVER.saved_command("/status")
    got = await until(lambda: replies(m0), 10)
    check(
        "G16",
        "/status replies in Saved Messages with counts and problems",
        got
        and "📊 GroupForwarder" in got[0]
        and "forwarded" in got[0]
        and "zz Protected" in got[0],
        got,
    )
    m0 = saved_mark()
    SERVER.saved_command("/sources")
    got = await until(lambda: replies(m0), 10)
    check(
        "G16/G26",
        "/sources lists the route, marking the source you're not a member of",
        got
        and "zz Source A, zz Source B, zz Protected, "
        f"{marked(LEFT_SRC)} (not in your chat list) → zz Target"
        in got[0],
        got,
    )
    m0 = saved_mark()
    SERVER.saved_command("/pause")
    await until(lambda: replies(m0), 10)
    mark = last_id(T)
    SERVER.post(A, text="zz while paused", post=True)
    await asyncio.sleep(2)
    paused_ok = not arrived(T, mark)
    m0 = saved_mark()
    SERVER.saved_command("/resume")
    await until(lambda: replies(m0), 10)
    SERVER.post(A, text="zz after resume", post=True)
    got = await until(lambda: arrived(T, mark))
    check(
        "G16",
        "/pause stops forwarding, /resume starts it again",
        paused_ok and got and got[0]["text"] == "zz after resume",
        got,
    )
    m0 = saved_mark()
    SERVER.saved_command("/help")
    got = await until(lambda: replies(m0), 10)
    check(
        "G16/U2",
        "/help lists the commands and the steps to add a chat",
        got
        and "/pause" in got[0]
        and "/chats" in got[0]
        and "SOURCE_GROUP_IDS" in got[0],
        got,
    )
    m0 = saved_mark()
    SERVER.saved_command("/chats")
    got = await until(lambda: replies(m0), 10)
    check(
        "U1",
        "/chats lists your chats with IDs, marking the ones it forwards from and to",
        got
        and f"zz Source A: {marked(A)} (channel) · forwarding from it" in got[0]
        and f"zz Target: {marked(T)} (group) · forwarding to it" in got[0]
        and f"zz Target 2: {marked(T2)} (group with topics)" in got[0],
        got,
    )
    m0 = saved_mark()
    SERVER.saved_command("/chats target")
    got = await until(lambda: replies(m0), 10)
    check(
        "U1",
        "/chats <part of a name> shows only matching chats",
        got and "zz Target 2" in got[0] and "zz Source A" not in got[0],
        got,
    )
    for asked in (f"/topics {marked(T2)}", "/topics target 2"):
        m0 = saved_mark()
        SERVER.saved_command(asked)
        got = await until(lambda m0=m0: replies(m0), 10)
        check(
            "U12",
            f"{asked} lists the topic numbers (asked from Telegram)",
            got and "7: zz Signals" in got[0] and "9: zz News" in got[0],
            got,
        )
    m0 = saved_mark()
    SERVER.saved_command(f"/topics {marked(A)}")
    got = await until(lambda: replies(m0), 10)
    check(
        "U12",
        "/topics for a chat without topics says so",
        got and "has no topics" in got[0],
        got,
    )

    mark = last_id(T)
    SERVER.post(B, text="zz from a hostile name", **{"from": EVIL})
    got = await until(lambda: arrived(T, mark))
    logged = out("zz from") + out("Evil")
    check(
        "U11",
        "a name with control codes and a line break is cleaned in the log",
        got
        and not any("\x1b" in line or "\x07" in line for line in TEE.lines)
        and not any(line.startswith("[00:00:00] zz-Fake line") for line in TEE.lines)
        and any(
            "message from zz- ]0;zz-HIJACKED Evil [2J [00:00:00] zz-F" in line
            for line in logged
        ),
        logged,
    )
    m0 = saved_mark()
    SERVER.saved_command("/status now")
    await asyncio.sleep(2)
    check("G16", "'/status now' is not taken as a command", not replies(m0))

    SERVER.delay = 0.3
    mark = last_id(T)
    for i in range(8):
        SERVER.post(A, text=f"zz order {i}", post=True)
    await until(lambda: len(arrived(T, mark)) >= 8, 20)
    order = [m["text"] for m in arrived(T, mark)]
    kept = order == [f"zz order {i}" for i in range(8)]
    check(
        "info",
        f"without strict_order, 8 quick posts arrived {'in order' if kept else 'mixed'}",
        True,
        order,
    )
    SERVER.delay = 0
    await client.disconnect()


async def plan_features():
    client = SERVER.client
    summary_mark = saved_mark()
    await until(lambda: client.running.is_set(), 30)
    await asyncio.sleep(1)
    settings = out("Settings: forwarder_config.toml")
    options = (
        "copy_mode",
        "skip_own_messages",
        "skip_duplicates",
        "catch_up",
        "strict_order",
        "daily_summary",
    )
    check(
        "Config",
        "settings line lists every option switched on",
        settings and all(x in settings[0] for x in options),
        settings,
    )
    check(
        "G18",
        "the topic route is listed at startup",
        out("zz Source C -> zz Target 2 (topic 7)"),
    )
    check(
        "G11",
        "strict_order turns on sequential_updates",
        SERVER.kwargs.get("sequential_updates") is True,
    )
    check(
        "G9",
        "first start with catch-up only remembers where chats are",
        out("Catch-up done: 0") and (WORK / "forwarder_state.json").exists(),
    )

    mark = last_id(T)
    bold = types.MessageEntityBold(offset=3, length=4)
    SERVER.post(A, text="zz bold & <done>", entities=[bold], photo=601, post=True)
    got = await until(lambda: arrived(T, mark))
    ok = False
    if got:
        m = got[0]
        bolds = [
            utf16_slice(m["text"], e.offset, e.length)
            for e in m.get("entities") or []
            if isinstance(e, types.MessageEntityBold)
        ]
        ok = (
            m.get("copy")
            and m["text"] == "📣 zz Source A\n\nzz bold & <done>"
            and "zz Source A" in bolds
            and "bold" in bolds
            and m.get("photo") == 601
        )
    check(
        "G12/G20",
        "copy mode: header with the source name, formatting and photo kept, no 'Forwarded from'",
        ok,
        got,
    )
    check(
        "G12/G20",
        "the log marks it (copied)",
        any(line.endswith("(copied)") for line in TEE.lines),
    )

    mark = last_id(T)
    SERVER.post(
        A,
        album=[
            {"text": "zz cap 1", "photo": 611, "post": True},
            {"text": "zz cap 2", "photo": 612, "post": True},
        ],
    )
    await until(lambda: len(arrived(T, mark)) >= 2, 10)
    got = arrived(T, mark)
    check(
        "G12/G20",
        "copy mode keeps an album together, header on the first caption",
        len(got) == 2
        and got[0].get("grouped")
        and got[0].get("grouped") == got[1].get("grouped")
        and got[0]["text"].startswith("📣 zz Source A")
        and got[1]["text"] == "zz cap 2",
        got,
    )

    mark = last_id(T)
    SERVER.post(A, text="zz " + "x" * 4090, post=True)
    got = await until(lambda: arrived(T, mark))
    check(
        "G12/G20",
        "a copy too long with the header is forwarded instead",
        got and got[0].get("fwd_from") and out("forwarding it instead"),
        got,
    )

    lines = len(TEE.lines)
    SERVER.post(P, text="zz secret", post=True)
    await until(
        lambda: any("restricts forwarding" in line for line in TEE.lines[lines:]), 10
    )
    check(
        "G12/G20",
        "copy mode never copies a restricted chat",
        out(
            "Could not forward message from zz Protected: that chat restricts forwarding"
        )
        and not any(
            r[0] in ("SendMessageRequest", "SendMediaRequest") and "Protected" in r[1]
            for r in SERVER.refused
        ),
    )

    mark = last_id(T)
    SERVER.post(A, text="zz my post", post=True, out=True)
    await asyncio.sleep(2)
    check("G8", "skip_own_messages skips your own posts", not arrived(T, mark))

    mark = last_id(T)
    SERVER.post(A, text="zz same news", post=True)
    await until(lambda: arrived(T, mark))
    read_only_before = refused_in("zz Read Only")
    SERVER.post(B, text="zz same news", **{"from": BOB})
    await asyncio.sleep(2)
    check(
        "G19",
        "the same post from a second source is skipped",
        len(arrived(T, mark)) == 1 and out("skipped a duplicate"),
        arrived(T, mark),
    )
    check(
        "G10",
        "a target you can't post in is reported (route to zz Read Only)",
        out("Could not forward message from zz Source B: ChatWriteForbiddenError"),
    )
    check(
        "G25",
        "copy mode doesn't try a forward when the target itself refuses (1 request, no 'forwarding it instead')",
        refused_in("zz Read Only") - read_only_before == 1
        and not out("Couldn't copy message from zz Source B"),
        refused_in("zz Read Only") - read_only_before,
    )

    mark = last_id(T2)
    SERVER.post(C, text="zz BTC long", post=True)
    SERVER.post(C, text="zz eth only", post=True)
    SERVER.post(C, text="zz btc giveaway", post=True)
    await until(lambda: arrived(T2, mark), 10)
    await asyncio.sleep(2)
    got = arrived(T2, mark)
    check(
        "G17/G18",
        "route filters: only 'zz BTC long' goes to zz Target 2",
        [m["text"] for m in got] == ["📣 zz Source C\n\nzz BTC long"],
        [m["text"] for m in got],
    )
    check("G18", "it lands in topic 7", got and got[0].get("topic") == 7, got)

    mark = last_id(T)
    SERVER.post(B, text="zz from a bot", **{"from": ROBOT})
    await asyncio.sleep(2)
    check(
        "G17",
        "the bot's message is skipped by skip_bots on the main route",
        not arrived(T, mark),
    )

    SERVER.delay = 0.3
    mark = last_id(T)
    for i in range(8):
        SERVER.post(A, text=f"zz strict {i}", post=True)
    await until(lambda: len(arrived(T, mark)) >= 8, 30)
    order = [m["text"].split("\n\n")[-1] for m in arrived(T, mark)]
    check(
        "G11",
        "strict_order keeps 8 quick posts in order",
        order == [f"zz strict {i}" for i in range(8)],
        order,
    )
    SERVER.delay = 0

    print("  ... waiting for the daily summary", file=sys.__stdout__, flush=True)
    got = await until(
        lambda: [
            t
            for t in replies(summary_mark)
            if t.startswith("✅ GroupForwarder is still running")
        ],
        200,
    )
    check(
        "G22",
        "the daily summary arrives in Saved Messages at the set time",
        got and "forwarded" in got[0] and "Failed forwards" in got[0],
        got,
    )
    m0 = saved_mark()
    SERVER.saved_command("/status")
    got = await until(lambda: replies(m0), 10)
    check("G16", "/status still works with all options on", got)
    await client.disconnect()


async def plan_catchup():
    client = SERVER.client
    await until(lambda: client.running.is_set(), 60)
    await asyncio.sleep(2)
    got = arrived(T, int(os.environ["ZZ_MARK_T"]))
    texts = [m["text"].split("\n\n")[-1] for m in got]
    check(
        "G9",
        "missed messages from zz Source A are sent, oldest first",
        texts[:2] == ["zz missed 1", "zz missed 2"],
        texts,
    )
    check("G9", "older than 6 hours: not sent", "zz too old" not in texts, texts)
    albums = [m["grouped"] for m in got if m.get("grouped")]
    check(
        "G9",
        "a missed album arrives as one album",
        len(albums) == 2 and len(set(albums)) == 1,
        texts,
    )
    check("G9", "a missed 'pinned' service message is not sent", "zz pin" not in texts)
    from_b = [t for t in texts if t.startswith("zz B missed")]
    check(
        "G9",
        "at most 20 per chat: the newest 20 of 25 from zz Source B",
        from_b == [f"zz B missed {i}" for i in range(5, 25)],
        from_b[:3] + ["..."] + from_b[-2:],
    )
    check("G9", "log says 'missed message'", out("missed message from"))
    state = json.loads((WORK / "forwarder_state.json").read_text(encoding="utf-8"))
    check(
        "G9",
        "bookmarks moved past what was caught up",
        state.get(str(marked(A))) == last_id(A)
        and state.get(str(marked(B))) == last_id(B),
        state,
    )
    await client.disconnect()


async def plan_launcher1():
    client = SERVER.client
    await until(lambda: client.running.is_set(), 30)
    mark = last_id(T)
    SERVER.post(A, text="zz via launcher", post=True)
    got = await until(lambda: arrived(T, mark))
    check("launcher", "started by run_forwarder.ps1, it forwards", got)
    # Running longer than -QuickExitSeconds 8, so the launcher restarts it
    await asyncio.sleep(10)
    await client.disconnect()


async def plan_stops_at_startup():
    pass  # These plans stop during startup: the checks are made on how it stopped


async def main_plan():
    plans = {
        "default": plan_default,
        "features": plan_features,
        "catchup": plan_catchup,
        "launcher1": plan_launcher1,
    }
    plan = plans.get(PLAN, plan_stops_at_startup)
    watchdog = asyncio.get_running_loop().call_later(
        900, lambda: asyncio.ensure_future(SERVER.client.disconnect())
    )
    try:
        await plan()
    except Exception as e:  # noqa: BLE001 - reported as a failed check
        check("harness", "plan crashed", False, repr(e))
        await SERVER.client.disconnect()
    finally:
        watchdog.cancel()


def prepare():
    """Sets up the world, the environment and the forwarder's options for PLAN."""
    world()
    env = {
        "API_ID": "123",
        "API_HASH": "zz" * 16,
        "SOURCE_GROUP_IDS": f"{marked(A)},{marked(B)},{marked(P)}",
        "TARGET_GROUP_ID": str(marked(T)),
    }
    argv = []
    if PLAN == "default":
        env["SOURCE_GROUP_IDS"] += f",{marked(LEFT_SRC)}"
    elif PLAN == "list":
        env.update(SOURCE_GROUP_IDS="", TARGET_GROUP_ID="")
        argv = ["--list-chats"]
    elif PLAN == "logfile":
        argv = ["--list-chats", "--log", "zz.log"]
    elif PLAN == "nologin":
        SERVER.authorized = False
        argv = ["--no-login"]
    elif PLAN == "lefttarget":
        env["TARGET_GROUP_ID"] = str(marked(LEFT_TGT))
    elif PLAN.startswith("launcher"):
        argv = sys.argv[3:]  # Exactly what run_forwarder.ps1 passed
        SERVER.chats[A]["title"] = (
            "zz 🚀 Source A"  # An emoji name, for the log's encoding
        )
        SERVER.authorized = PLAN == "launcher1"  # The second start finds the login gone
    elif PLAN == "catchup":
        # Posted while the forwarder was off: it never gets these as live updates
        env["ZZ_MARK_T"] = str(last_id(T))
        seven_hours_ago = time.time() - 7 * 3600
        SERVER.post(
            A, deliver=False, text="zz too old", post=True, date=seven_hours_ago
        )
        SERVER.post(A, deliver=False, text="zz missed 1", post=True)
        SERVER.post(A, deliver=False, text="zz pin", service=True)
        SERVER.post(A, deliver=False, text="zz missed 2", post=True)
        album = [
            {"text": "zz missed album", "photo": 701, "post": True},
            {"text": "", "photo": 702, "post": True},
        ]
        SERVER.post(A, deliver=False, album=album)
        for i in range(25):
            SERVER.post(B, deliver=False, text=f"zz B missed {i}", **{"from": BOB})
    os.environ.update(env)
    return argv


def check_how_it_stopped(stopped):
    check(
        "crash",
        "the forwarder did not crash (no traceback at the end)",
        not isinstance(stopped, BaseException),
        repr(stopped),
    )
    message = stopped if isinstance(stopped, str) else ""
    if PLAN == "list":
        check(
            "G14/G24",
            "--list-chats prints chats with IDs and types, then stops cleanly",
            stopped in (0, None)
            and out(f"zz Source A: {marked(A)} (channel)")
            and out(f"zz Target: {marked(T)} (group)")
            and out("/chats in your Saved Messages")
            and not SERVER.client.running.is_set(),
            TEE.lines[:6],
        )
    elif PLAN == "logfile":
        log = WORK / "zz.log"
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        check(
            "G21",
            "--log FILE writes the output to that file (UTF-8)",
            "Your chats (name: ID):" in text and "zz Source A" in text,
            text[:300],
        )
    elif PLAN == "nologin":
        check(
            "G21",
            "--no-login without a login stops with a message (no phone prompt)",
            "Not logged in" in message,
            stopped,
        )
    elif PLAN == "lefttarget":
        check(
            "G23",
            "a target you've left stops the forwarder",
            "isn't in your chat list" in message and "TARGET_GROUP_ID" in message,
            stopped,
        )
    elif PLAN == "launcher2":
        check(
            "launcher",
            "the second start stops with 'Not logged in'",
            "Not logged in" in message,
            stopped,
        )
    elif PLAN == "second":
        check(
            "G21",
            "a second copy refuses to start while one is running",
            "Another copy of the forwarder" in message,
            stopped,
        )
    elif PLAN == "typo":
        check(
            "Config",
            "a misspelled setting stops it with a clear message",
            "Unknown setting 'skip_own_mesages'" in message,
            stopped,
        )
    intended = ("restricts saving content", "can't write in zz Read Only", "> 4096")
    unexpected = [
        r
        for r in SERVER.refused
        if r[1] != "flood wait" and not any(x in r[1] for x in intended)
    ]
    check(
        "TL",
        f"every request was valid for Telegram ({len(SERVER.log)} requests; "
        "only the intended ones were refused)",
        not unexpected,
        unexpected,
    )


if __name__ == "__main__":
    argv = prepare()
    TEE = Tee(WORK / f"output_{PLAN}.txt")
    sys.stdout = sys.stderr = TEE
    telethon.TelegramClient = SimClient
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(main_plan())
    script = str(WORK / "group_forwarder.py")
    sys.argv = [script, *argv]
    os.chdir(WORK)
    stopped = None  # SystemExit's code (a message or a number), or the crash
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        stopped = e.code
    except Exception as e:  # noqa: BLE001 - a crash of the real script is a finding
        traceback.print_exc()
        stopped = e
    check_how_it_stopped(stopped)
    if PLAN in ("default", "features", "catchup"):
        SERVER.save(WORK / "server_state.pkl")
    results_file = WORK / f"results_{PLAN}.json"
    results_file.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    if PLAN.startswith("launcher"):
        # Stop the way the real script would, so the launcher sees its real exit code and message
        if isinstance(stopped, BaseException):
            raise stopped
        if stopped not in (None, 0):
            raise SystemExit(stopped)
