#!/usr/bin/env python3
"""
run_with_monitor.py
===================
Thin supervisor around run_experiment.py. The runner itself already enforces
the gates (strict 3/3 canary, rolling parse-rate floor, cost ceiling) and writes
results/status.txt every response. This wrapper:

  - launches the runner as a subprocess with the right env,
  - streams the runner's stdout live (so Claude Code shows it in-terminal),
  - prints a compact heartbeat from results/status.txt,
  - reports the runner's exit code with a plain-English meaning.

Exit codes (from run_experiment.py):
  0  full run completed
  2  CANARY FAILED  (format broken; stopped before the full run -- cheap)
  3  ABORTED parse-rate floor breached mid-run
  4  ABORTED cost ceiling breached mid-run
  1  other error (e.g. missing key / deps)

Usage (Claude Code runs this):
  python harness/run_with_monitor.py

Env knobs (all optional; defaults are sensible):
  CANARY_QIDS=Q15,Q08  (canary runs all 4 arms x these questions, one model)
  PARSE_FLOOR=0.85  COST_CEILING=25
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
RESULTS = os.path.join(ROOT, "results")
STATUS = os.path.join(RESULTS, "status.txt")

EXIT_MEANING = {
    0: "OK: full run completed.",
    2: "CANARY FAILED: output format broken; stopped before the full run (cheap). "
       "Inspect results/canary.jsonl, fix the harness, re-run.",
    3: "ABORTED: rolling parse-rate fell below the floor mid-run. Something "
       "regressed; inspect results/responses.jsonl tail.",
    4: "ABORTED: running cost exceeded the ceiling. Raise COST_CEILING only if "
       "you expected this.",
    1: "ERROR: setup problem (missing ANTHROPIC_API_KEY or deps). See output above.",
}


def main():
    os.makedirs(RESULTS, exist_ok=True)
    # clear stale status
    if os.path.exists(STATUS):
        os.remove(STATUS)

    env = dict(os.environ)
    runner = os.path.join(HERE, "run_experiment.py")
    print(f"launching: {sys.executable} {runner}")
    print("(streaming runner output; heartbeat from results/status.txt)\n")

    proc = subprocess.Popen(
        [sys.executable, "-u", runner],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, bufsize=1, universal_newlines=True)

    last_status = ""
    last_beat = 0.0
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            # every few seconds, also surface the compact status line
            now = time.time()
            if now - last_beat > 5 and os.path.exists(STATUS):
                try:
                    s = open(STATUS).read().strip()
                    if s and s != last_status:
                        sys.stdout.write(f"   [status] {s}\n")
                        sys.stdout.flush()
                        last_status = s
                    last_beat = now
                except Exception:
                    pass
    except KeyboardInterrupt:
        proc.terminate()
        print("\n[monitor] interrupted by user; terminated runner.")
        sys.exit(130)

    proc.wait()
    code = proc.returncode
    print("\n" + "=" * 60)
    print(f"[monitor] runner exited with code {code}: "
          f"{EXIT_MEANING.get(code, 'unknown exit code')}")
    if os.path.exists(STATUS):
        print(f"[monitor] final status: {open(STATUS).read().strip()}")
    print("=" * 60)
    sys.exit(code)


if __name__ == "__main__":
    main()
