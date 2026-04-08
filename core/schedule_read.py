"""Read local schedule.json and sync marker for bot / CLI (no Playwright)."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
_WD_KO = "월화수목금토일"


def _weekday_ko(d: date) -> str:
    return _WD_KO[d.weekday()]


def _period_sort_key(period: str) -> tuple[int, str]:
    p = (period or "").strip()
    try:
        return (0, f"{int(p):05d}")
    except ValueError:
        return (1, p)


def load_schedule_rows(schedule_path: Path) -> tuple[list[dict] | None, str | None]:
    """
    Load schedule.json (array of row dicts). Returns (rows, error).
    error is set when file missing, empty, or not a JSON array.
    """
    try:
        raw = schedule_path.read_text(encoding="utf-8")
    except OSError:
        return None, "schedule.json 을 읽을 수 없습니다. `/schedule`으로 먼저 동기화하세요."
    raw = raw.lstrip("\ufeff").strip()
    if not raw:
        return [], None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"schedule.json JSON 오류: {e}"
    if not isinstance(data, list):
        return None, "schedule.json 형식이 배열이 아닙니다."
    out: list[dict] = []
    for row in data:
        if isinstance(row, dict):
            out.append(row)
    return out, None


def rows_for_date(rows: list[dict], target: date) -> list[dict]:
    iso = target.isoformat()
    picked: list[dict] = []
    for row in rows:
        d = row.get("date")
        if d == iso:
            picked.append(row)
    picked.sort(key=lambda r: _period_sort_key(str(r.get("period", ""))))
    return picked


def format_day_schedule(target: date, rows: list[dict]) -> str:
    """Human-readable block for one day (Telegram-friendly, short lines)."""
    if not rows:
        return f"{target.isoformat()} ({_weekday_ko(target)})\n수업 없음 (로컬 데이터 기준)"

    lines = [f"{target.isoformat()} ({_weekday_ko(target)}) — {len(rows)}교시"]
    for r in rows:
        p = str(r.get("period", "?"))
        subj = (r.get("subject") or "").strip() or "(제목 없음)"
        room = r.get("room")
        room_s = f" · {room}" if room else ""
        st = r.get("start")
        en = r.get("end")
        if st and en:
            lines.append(f"P{p} {st}-{en} · {subj}{room_s}")
        else:
            lines.append(f"P{p} · {subj}{room_s}")
    return "\n".join(lines)


def read_last_sync_ok(artifacts_dir: Path) -> dict | None:
    path = artifacts_dir / "last_sync_ok.json"
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def format_sync_status(repo_root: Path) -> str:
    """Summary: last sync marker + schedule row count."""
    schedule_path = repo_root / "schedule.json"
    art = repo_root / "artifacts"
    rows, err = load_schedule_rows(schedule_path)
    parts: list[str] = []

    meta = read_last_sync_ok(art)
    if meta and meta.get("ok"):
        utc_s = meta.get("utc")
        if isinstance(utc_s, str) and utc_s:
            try:
                # tolerate Z suffix
                norm = utc_s.replace("Z", "+00:00")
                dt_utc = datetime.fromisoformat(norm)
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_jst = dt_utc.astimezone(JST)
                parts.append(f"마지막 성공 동기화: {dt_jst.strftime('%Y-%m-%d %H:%M')} (JST)")
            except ValueError:
                parts.append(f"마지막 성공 동기화: {utc_s} (UTC 문자열)")
        else:
            parts.append("마지막 성공 동기화: 기록됨 (시각 없음)")
    else:
        parts.append("마지막 성공 동기화: 없음 또는 실패 상태 — `/schedule` 실행 후 확인")

    if err:
        parts.append(f"시간표 파일: {err}")
    elif rows is not None:
        parts.append(f"schedule.json 행 수: {len(rows)}")

    return "\n".join(parts)


def today_in_jst() -> date:
    return datetime.now(JST).date()
