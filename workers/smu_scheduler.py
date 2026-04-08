"""
SMU portal → schedule.json → Notion sync (formerly run_pipeline body).
Run:  python -m workers.smu_scheduler
      또는 Worker 스케줄러가 subprocess로 호출.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from core.config import WorkerContext, ensure_runtime_dirs, load_env
from core.telegram_log import (
    alerts_configured,
    format_room_conflicts_for_notify,
    send_pipeline_alert,
    send_room_conflict_notice,
)

LOCK_MAX_AGE_SEC = 45 * 60


def _python(ctx: WorkerContext) -> str:
    return sys.executable


def _schedule_row_count(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return -1
    raw = raw.lstrip("\ufeff").strip()
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return -1
    if isinstance(data, list):
        return len(data)
    return -1


def _fail(
    *,
    kind: str,
    message: str,
    log_path: Path | None,
    exit_code: int,
) -> int:
    load_env()
    if alerts_configured():
        sent = send_pipeline_alert(kind=kind, message=message, log_path=str(log_path) if log_path else None)
        if not sent:
            print("smu_scheduler: alert channels configured but no delivery succeeded", file=sys.stderr)
    else:
        print(
            "smu_scheduler: no alert env (set TELEGRAM_* and/or SLACK_WEBHOOK_URL)",
            file=sys.stderr,
        )
    return exit_code


def run(ctx: WorkerContext | None = None) -> int:
    """
    Execute scraper → notion_sync → optional room-conflict warnings.
    Returns shell exit code for Task Scheduler / bot.
    """
    ctx = ctx or WorkerContext.default()
    root = ctx.repo_root
    load_env(root / ".env")
    ensure_runtime_dirs()

    art = root / "artifacts"
    logs = art / "logs"
    lock = art / "pipeline.lock"
    schedule = root / "schedule.json"
    storage = art / "storage_state.json"

    if lock.exists():
        age = time.time() - lock.stat().st_mtime
        if age < LOCK_MAX_AGE_SEC:
            print(f"smu_scheduler: skip (lock age {age:.0f}s < {LOCK_MAX_AGE_SEC}s)", file=sys.stderr)
            return 0
        try:
            lock.unlink()
        except OSError:
            pass

    lock.write_text(str(os.getpid()), encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1"}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = logs / f"pipeline-{ts}.log"
    py = _python(ctx)

    try:
        scraper_args = [
            py,
            str(root / "scraper.py"),
            "--headless",
            "--output",
            "json",
            "--details-mode",
            "js",
            "--storage-state",
            str(storage),
            "--save-storage-state",
            str(storage),
        ]
        with log_path.open("w", encoding="utf-8") as lf, schedule.open("w", encoding="utf-8") as sf:
            lf.write(f"=== scraper {datetime.now().isoformat()} ===\n")
            lf.flush()
            p1 = subprocess.run(scraper_args, cwd=str(root), stdout=sf, stderr=lf, env=env)
        if p1.returncode != 0:
            print(f"smu_scheduler: scraper exit {p1.returncode}", file=sys.stderr)
            return _fail(
                kind="scraper_failed",
                message=f"scraper exit code {p1.returncode}",
                log_path=log_path,
                exit_code=p1.returncode,
            )

        n = _schedule_row_count(schedule)
        if n < 0:
            print("smu_scheduler: schedule.json missing or invalid JSON after scraper", file=sys.stderr)
            return _fail(
                kind="schedule_json_invalid",
                message="schedule.json could not be parsed as a JSON array",
                log_path=log_path,
                exit_code=1,
            )
        if n == 0:
            print("smu_scheduler: extracted_items=0 (empty schedule)", file=sys.stderr)
            return _fail(
                kind="empty_schedule",
                message="scraper produced 0 rows — possible login/parse failure or empty timetable",
                log_path=log_path,
                exit_code=1,
            )

        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(f"\n=== notion_sync {datetime.now().isoformat()} ===\n")
            lf.flush()
            p2 = subprocess.run(
                [py, str(root / "notion_sync.py"), "--input", str(schedule)],
                cwd=str(root),
                stdout=lf,
                stderr=subprocess.STDOUT,
                env=env,
            )
        if p2.returncode != 0:
            print(f"smu_scheduler: notion_sync exit {p2.returncode}", file=sys.stderr)
            return _fail(
                kind="notion_sync_failed",
                message=f"notion_sync exit code {p2.returncode} (rows={n})",
                log_path=log_path,
                exit_code=p2.returncode,
            )

        conflicts: list = []
        conf_path = art / "room_conflicts.json"
        try:
            if conf_path.is_file():
                conf_data = json.loads(conf_path.read_text(encoding="utf-8"))
                if isinstance(conf_data, dict):
                    conflicts = list(conf_data.get("conflicts") or [])
        except (json.JSONDecodeError, OSError):
            conflicts = []

        if conflicts:
            msg = format_room_conflicts_for_notify(conflicts)
            print(f"smu_scheduler: room scrape vs rules differ for {len(conflicts)} slot(s)", file=sys.stderr)
            warn_file = art / "room_conflict_notify.txt"
            warn_file.write_text(msg, encoding="utf-8")
            if alerts_configured():
                if not send_room_conflict_notice(message=msg, log_path=str(log_path)):
                    print("smu_scheduler: room conflict notice not delivered", file=sys.stderr)
            with log_path.open("a", encoding="utf-8") as lf:
                lf.write(f"\n=== notion_sync append_sync_warning {datetime.now().isoformat()} ===\n")
                lf.flush()
                p3 = subprocess.run(
                    [py, str(root / "notion_sync.py"), "--append-sync-warning-file", str(warn_file)],
                    cwd=str(root),
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
            if p3.returncode != 0:
                print(
                    f"smu_scheduler: notion_sync --append-sync-warning-file exit {p3.returncode}",
                    file=sys.stderr,
                )

        meta = {"ok": True, "utc": datetime.now(timezone.utc).isoformat()}
        (art / "last_sync_ok.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        try:
            from core import notion_pack

            pr = notion_pack.reset_pack_after_schedule_sync(root)
            if pr != "disabled":
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(f"\n=== pack_reset {datetime.now().isoformat()} ===\n{pr}\n")
                print(f"smu_scheduler: pack_reset {pr}", file=sys.stderr)
        except Exception as e:
            print(f"smu_scheduler: pack_reset error: {e}", file=sys.stderr)

        print("smu_scheduler: ok", schedule)
        return 0
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(run(WorkerContext.default()))
