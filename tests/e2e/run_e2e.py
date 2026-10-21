"""End-to-end test: runs the real group_forwarder.py (a copy, unchanged) with Telethon's real client
on top of a simulated Telegram server, start after start, like a user would. No login, no network,
and nothing of yours is used. Takes about 5 minutes (a 61 s rate-limit wait and a daily summary).

    python tests/e2e/run_e2e.py

Prints PASS/FAIL per check and exits with 1 if anything failed. Its files go to a temporary
folder, deleted when everything passed (kept, and its path printed, when something failed).
Set GF_DIR to test the script (and run_forwarder.ps1) of another folder (default: this repo)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = Path(
    os.environ.get("GF_DIR") or HERE.parents[1]
)  # Where the script under test is
PY = sys.executable
C, T2, B, RO = 3333333333, 8888888888, 2222222222, 7777777777  # As in play.py
RUNS = Path(tempfile.mkdtemp(prefix="zz-gf-e2e-"))


def marked(cid):
    return int(f"-100{cid}")


def fresh(name):
    work = RUNS / name
    work.mkdir()
    # The real script, unchanged, with no .env next to it
    shutil.copy2(SOURCE / "group_forwarder.py", work / "group_forwarder.py")
    return work


def start(plan, work):
    return subprocess.Popen(
        [PY, "-B", str(HERE / "play.py"), plan, str(work)],
        cwd=HERE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def finish(plan, proc, started, timeout=900):
    try:
        harness_output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        harness_output, _ = proc.communicate()
        print(f"  {plan}: killed after {timeout} s")
    print(f"  {plan}: done in {time.time() - started:.0f} s")
    if harness_output.strip():
        print("   ", harness_output.strip()[-1500:])


def run(plan, work, timeout=900):
    finish(plan, start(plan, work), time.time(), timeout)


def features_config():
    when = time.strftime("%H:%M", time.localtime(time.time() + 150))
    return f"""[options]
skip_own_messages = true
catch_up = true
strict_order = true
copy_mode = true
skip_duplicates = true
daily_summary = true
daily_summary_time = "{when}"

[filters]
skip_bots = true

[[routes]]
sources = [{marked(C)}]
target = {marked(T2)}
topic = 7
include_keywords = ["btc"]
exclude_keywords = ["giveaway"]

[[routes]]
sources = [{marked(B)}]
target = {marked(RO)}
"""


def launcher():
    """The real run_forwarder.ps1 (a copy, unchanged, with short times) starting the real script:
    start 1 runs for over 10 seconds, so it's restarted; start 2 finds the login gone and stops
    within a few seconds, so the launcher gives up. "Quick" is 8 seconds here (60 normally):
    a stop takes a few seconds just to start Python, more on a busy computer."""
    if os.name != "nt":
        print("  launcher: skipped (run_forwarder.ps1 is for Windows)")
        return []
    work = fresh("launcher")
    shutil.copy2(SOURCE / "run_forwarder.ps1", work / "run_forwarder.ps1")
    log = work / "forwarder.log"
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    command += [str(work / "run_forwarder.ps1"), "-QuickExitSeconds", "8"]
    command += ["-RestartDelaySeconds", "1", "-Python", PY]
    command += ["-Script", str(HERE / "from_launcher.py"), "-Log", str(log)]
    try:
        proc = subprocess.run(
            command,
            env={**os.environ, "ZZ_GF_WORK": str(work)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        code, said = proc.returncode, proc.stdout[-500:] + proc.stderr[-500:]
    except subprocess.TimeoutExpired:
        code, said = (
            None,
            "the launcher was still running after 180 s (kept restarting?)",
        )
    print(f"  launcher: exit code {code}")
    try:
        text, utf8 = log.read_bytes().decode("utf-8"), True  # Strict: all valid UTF-8
    except (OSError, UnicodeDecodeError) as e:
        text, utf8 = repr(e), False
    detail = text[-1500:] + said
    checks = [
        ("the log file is valid UTF-8 (forwarder and launcher lines together)", utf8),
        (
            "the forwarder's lines are in the log, emoji names intact",
            "zz 🚀 Source A -> zz Target: message from zz 🚀 Source A" in text,
        ),
        (
            "it was started twice (restarted after a normal stop)",
            text.count("run_forwarder.ps1: starting the forwarder") == 2,
        ),
        (
            "first stop logged with exit code 0",
            "the forwarder stopped (exit code 0) after" in text,
        ),
        ("the 'Not logged in' message is in the log", "Not logged in." in text),
        (
            "second stop: exit code 1, and the launcher gives up with an explanation",
            "the forwarder stopped (exit code 1) after" in text
            and "restarting won't help" in text,
        ),
        ("the launcher itself ends with exit code 1", code == 1),
    ]
    return [
        {
            "plan": "launcher",
            "item": "launcher",
            "name": n,
            "ok": bool(ok),
            "detail": detail,
        }
        for n, ok in checks
    ]


def main():
    print(f"GroupForwarder end-to-end test of {SOURCE} (files in {RUNS})")
    results = launcher()
    for plan in ("nologin", "list", "logfile", "lefttarget"):
        run(plan, fresh(plan), 120)
    work = fresh("typo")
    (work / "forwarder_config.toml").write_text(
        "[options]\nskip_own_mesages = true\n", encoding="utf-8"
    )
    run("typo", work, 120)

    main_work = fresh(
        "main"
    )  # default, features and catchup share one folder and world
    print("  default: running (includes a 61 s rate-limit wait)")
    started, proc = time.time(), start("default", main_work)
    output = main_work / "output_default.txt"
    end = time.time() + 60
    while time.time() < end and not (
        output.exists() and "Forwarder is running" in output.read_text(encoding="utf-8")
    ):
        time.sleep(0.5)
    run(
        "second", main_work, 120
    )  # A second copy in the same folder while the first runs
    finish("default", proc, started)

    (main_work / "forwarder_config.toml").write_text(
        features_config(), encoding="utf-8"
    )
    print("  features: running (waits for the daily summary, about 2-3 minutes)")
    run("features", main_work)
    run("catchup", main_work, 300)

    problems = []
    for path in sorted(RUNS.rglob("results_*.json")):
        results += json.loads(path.read_text(encoding="utf-8"))
    for path in sorted(RUNS.rglob("output_*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in (
            "Traceback",
            "Unhandled exception",
            "Task exception was never retrieved",
        ):
            if marker in text:
                at = text.index(marker)
                problems.append(
                    f"{path.parent.name}/{path.name}: {text[at : at + 1500]}"
                )
    expected = {"default", "features", "catchup", "second", "typo"}
    expected |= {"nologin", "list", "logfile", "lefttarget"}
    if os.name == "nt":
        expected |= {"launcher1", "launcher2"}
    missing = expected - {r["plan"] for r in results}

    print()
    for r in results:
        line = (
            f"{'PASS' if r['ok'] else 'FAIL'}  [{r['plan']}] {r['item']}: {r['name']}"
        )
        print(line + ("" if r["ok"] else f"\n      {r['detail']}"))
    passed = sum(r["ok"] for r in results)
    print(f"\n{passed}/{len(results)} checks passed")
    if missing:
        print("Plans with no results (crashed?):", ", ".join(sorted(missing)))
    for problem in problems:
        print("\nProblem in the output:", problem)
    if passed == len(results) and not missing and not problems:
        shutil.rmtree(RUNS, ignore_errors=True)
        return 0
    print(f"\nFiles kept for a look: {RUNS}")
    return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
