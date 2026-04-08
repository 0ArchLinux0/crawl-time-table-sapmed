"""수업 시작 N분 전 TELEGRAM_CHAT_ID 로 알림 (schedule.json · JST)."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Iterable

from core.config import REPO_ROOT
from core.schedule_read import JST, load_schedule_rows, rows_for_date, today_in_jst

log = logging.getLogger("class_reminders")

_SENT_PATH = REPO_ROOT / "artifacts" / "class_reminders_sent.json"


def reminders_enabled() -> bool:
    v = (os.getenv("SCHEDULE_CLASS_REMINDERS") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def reminder_minutes_before() -> int:
    try:
        return max(1, min(180, int((os.getenv("CLASS_REMINDER_MINUTES_BEFORE") or "10").strip(), 10)))
    except ValueError:
        return 10


def _row_key(row: dict) -> str | None:
    d = row.get("date")
    p = row.get("period")
    st = row.get("start")
    if not d or p is None or not st:
        return None
    return f"{d}|{p}|{st}"


def _parse_start_local(d: date, start_s: str) -> datetime | None:
    parts = (start_s or "").strip().split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, h, m, tzinfo=JST)


def _load_sent_for_day(today: date) -> set[str]:
    try:
        raw = _SENT_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    keys = data.get("keys")
    if not isinstance(keys, list):
        return set()
    out: set[str] = set()
    prefix = today.isoformat() + "|"
    for k in keys:
        if isinstance(k, str) and k.startswith(prefix):
            out.add(k)
    return out


def _save_sent_for_day(today: date, keys: set[str]) -> None:
    _SENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"day": today.isoformat(), "keys": sorted(keys)}
    _SENT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_line(row: dict, minutes_before: int) -> str:
    p = str(row.get("period", "?"))
    subj = (row.get("subject") or "").strip() or "(제목 없음)"
    room = row.get("room")
    st = row.get("start")
    en = row.get("end")
    room_s = f"\n{room}" if room else ""
    time_s = f"{st}–{en}" if st and en else (str(st) if st else "")
    return (
        f"⏳ {minutes_before}분 뒤 수업 (JST)\n"
        f"P{p} · {subj}\n"
        f"{time_s}{room_s}"
    )


def compute_due_reminders(now: datetime | None = None) -> list[tuple[str, str]]:
    """오늘·지금 창에 해당하는 미전송 알림 목록 (전송 성공 후 mark_reminders_sent 호출)."""
    if not reminders_enabled():
        return []
    now_dt = now if now is not None else datetime.now(JST)
    today = now_dt.date()
    schedule_path = REPO_ROOT / "schedule.json"
    rows, err = load_schedule_rows(schedule_path)
    if err or not rows:
        return []

    day_rows = rows_for_date(rows, today)
    if not day_rows:
        return []

    minutes_before = reminder_minutes_before()
    window = timedelta(seconds=95)
    sent = _load_sent_for_day(today)
    out: list[tuple[str, str]] = []

    for row in day_rows:
        key = _row_key(row)
        if key is None or key in sent:
            continue
        st_s = row.get("start")
        if not isinstance(st_s, str):
            continue
        class_start = _parse_start_local(today, st_s)
        if class_start is None:
            continue
        remind_at = class_start - timedelta(minutes=minutes_before)
        if remind_at <= now_dt < remind_at + window:
            text = _format_line(row, minutes_before)
            out.append((key, text))
            log.info("class reminder due key=%s subject=%s", key, (row.get("subject") or "")[:40])

    return out


def mark_reminders_sent(keys: Iterable[str]) -> None:
    if not keys:
        return
    today = today_in_jst()
    sent = _load_sent_for_day(today)
    for k in keys:
        if isinstance(k, str) and k.startswith(today.isoformat() + "|"):
            sent.add(k)
    _save_sent_for_day(today, sent)
