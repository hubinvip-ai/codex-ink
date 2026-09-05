"""Run each Python suite; plain root discovery does not recurse into these folders."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    suites = sorted(p for p in (ROOT / "Tests").iterdir() if p.is_dir() and any(p.glob("test_*.py")))
    if not suites:
        raise SystemExit("No test suites found")
    failed = []
    for suite in suites:
        print(f"Running {suite.name}", flush=True)
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(suite), "-v"], cwd=ROOT)
        if result.returncode:
            failed.append(suite.name)
    if failed:
        raise SystemExit("Failed suites: " + ", ".join(failed))
    print(f"PASS {len(suites)} suites", flush=True)

if __name__ == "__main__":
    main()
