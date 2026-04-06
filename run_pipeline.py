"""
Run portal scraper → schedule.json → Notion sync.
Intended for Windows Task Scheduler (5:00 / 7:00) and watchdog retries.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ART = ROOT / "artifacts"
LOGS = ART / "logs"
LOCK = ART / "pipeline.lock"
SCHEDULE = ROOT / "schedule.json"
STORAGE = ART / "storage_state.json"

# Skip if another run is still active or finished very recently (minutes).
LOCK_MAX_AGE_SEC = 45 * 60


def _python() -> str:
    return sys.executable


def main() -> int:
    ART.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    if LOCK.exists():
        age = time.time() - LOCK.stat().st_mtime
        if age < LOCK_MAX_AGE_SEC:
            print(f"pipeline: skip (lock age {age:.0f}s < {LOCK_MAX_AGE_SEC}s)", file=sys.stderr)
            return 0
        try:
            LOCK.unlink()
        except OSError:
            pass

    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1"}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOGS / f"pipeline-{ts}.log"
    py = _python()

    try:
        scraper_args = [
            py,
            str(ROOT / "scraper.py"),
            "--headless",
            "--output",
            "json",
            "--details-mode",
            "hover",
            "--storage-state",
            str(STORAGE),
            "--save-storage-state",
            str(STORAGE),
        ]
        with log_path.open("w", encoding="utf-8") as lf, SCHEDULE.open("w", encoding="utf-8") as sf:
            lf.write(f"=== scraper {datetime.now().isoformat()} ===\n")
            lf.flush()
            p1 = subprocess.run(scraper_args, cwd=ROOT, stdout=sf, stderr=lf, env=env)
        if p1.returncode != 0:
            print(f"pipeline: scraper exit {p1.returncode}", file=sys.stderr)
            return p1.returncode

        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(f"\n=== notion_sync {datetime.now().isoformat()} ===\n")
            lf.flush()
            p2 = subprocess.run(
                [py, str(ROOT / "notion_sync.py"), "--input", str(SCHEDULE)],
                cwd=ROOT,
                stdout=lf,
                stderr=subprocess.STDOUT,
                env=env,
            )
        if p2.returncode != 0:
            print(f"pipeline: notion_sync exit {p2.returncode}", file=sys.stderr)
            return p2.returncode

        meta = {"ok": True, "utc": datetime.now(timezone.utc).isoformat()}
        (ART / "last_sync_ok.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print("pipeline: ok", SCHEDULE)
        return 0
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
