"""What the real run_forwarder.ps1 starts in the launcher test (its -Script): play.py around the
real group_forwarder.py, with the options exactly as the launcher passed them (--no-login --log FILE).
Each start is numbered: start 1 runs normally, start 2 finds the login gone."""

import os
import runpy
import sys
from pathlib import Path

work = Path(os.environ["ZZ_GF_WORK"])
counter = work / "launch_count.txt"
number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(number))
play = str(Path(__file__).with_name("play.py"))
sys.argv = [play, f"launcher{number}", str(work), *sys.argv[1:]]
runpy.run_path(play, run_name="__main__")
