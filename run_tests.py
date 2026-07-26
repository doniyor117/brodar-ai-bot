"""
Runs every test_phaseN.py suite in one process and reports a single summary.

    python3 run_tests.py

Each suite still runs standalone too (`python3 test_phase5.py`) — this just
saves running them one at a time by hand, and gives one final pass/fail line
for CI or a pre-push check.
"""
import glob
import re
import subprocess
import sys


def _phase_suites():
    """Every test_phaseN.py in this directory, sorted numerically by N."""
    files = glob.glob("test_phase*.py")

    def phase_num(path):
        m = re.search(r"test_phase(\d+)\.py", path)
        return int(m.group(1)) if m else 0

    return sorted(files, key=phase_num)


def main():
    suites = _phase_suites()
    if not suites:
        print("no test_phase*.py files found.")
        sys.exit(1)

    failed = []
    for suite in suites:
        print(f"\n{'=' * 60}\n{suite}\n{'=' * 60}")
        result = subprocess.run([sys.executable, suite])
        if result.returncode != 0:
            failed.append(suite)

    print(f"\n{'=' * 60}")
    print(f"ran {len(suites)} suite(s)")
    if failed:
        print(f"{len(failed)} FAILED:")
        for f in failed:
            print(f"  - {f}")
        sys.exit(1)
    print("all suites passed")


if __name__ == "__main__":
    main()
