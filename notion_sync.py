from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
SYNC_LOG_TZ = ZoneInfo("Asia/Tokyo")

# Period upsert: select options 1–7 (covers most timetables; API accepts new names on write anyway).
_PERIOD_SELECT_OPTIONS = [{"name": str(i)} for i in range(1, 8)]


@dataclass(frozen=True)
class ResolvedSchema:
    """Property names actually used after API ensure (may differ from Korean defaults)."""

    prop_title: str
    prop_date: str
    prop_period: str
    prop_room: str


@dataclass(frozen=True)
class NotionScheduleRow:
    date: str  # YYYY-MM-DD
    period: str
    subject: str
    start: str | None = None  # HH:MM
    end: str | None = None  # HH:MM
    room: str | None = None


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def _env(name: str, *, required: bool = True) -> str | None:
    val = os.getenv(name)
    if required and (val is None or not str(val).strip()):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _notion_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _sync_log_target_page_id(token: str, db_id: str) -> str | None:
    """
    Page whose blocks we append to (bottom of page = below inline DB if the DB lives on that page).
    NOTION_SYNC_LOG_PAGE_ID overrides; else parent page of the database when parent is a page.
    """
    explicit = (os.getenv("NOTION_SYNC_LOG_PAGE_ID") or "").strip()
    if explicit:
        return explicit
    data = _fetch_database(token, db_id)
    parent = data.get("parent") or {}
    if parent.get("type") == "page_id":
        return str(parent.get("page_id") or "")
    return None


def _notion_append_block_children(token: str, block_id: str, children: list[dict[str, Any]]) -> None:
    url = f"{NOTION_API_BASE}/blocks/{block_id}/children"
    r = requests.patch(
        url,
        headers=_notion_headers(token),
        json={"children": children},
        timeout=30,
    )
    if r.status_code >= 400:
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:600]}
        raise RuntimeError(f"append blocks failed: {r.status_code} {body}")


def _write_sync_log_entry(
    token: str,
    page_id: str,
    *,
    row_count: int,
    elapsed_s: float,
    wiped: bool,
    style: str,
) -> None:
    now = datetime.now(SYNC_LOG_TZ)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    summary = f"{ts} (JST) · 일정 {row_count}건 반영 완료"
    detail = f"소요 {elapsed_s:.1f}s"
    if wiped:
        detail += " · wipe-first(기존 행 아카이브 후 재삽입)"
    detail = detail[:1900]
    summary = summary[:1900]

    def _rt(s: str) -> list[dict[str, Any]]:
        return [{"type": "text", "text": {"content": s}}]

    children: list[dict[str, Any]]
    if style == "bullet":
        line = f"{summary} — {detail}"
        children = [
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rt(line)},
            }
        ]
    else:
        # Default: toggle (제목 한 줄 + 펼치면 상세)
        children = [
            {
                "object": "block",
                "type": "toggle",
                "toggle": {
                    "rich_text": _rt(summary),
                    "children": [
                        {
                            "object": "block",
                            "type": "bulleted_list_item",
                            "bulleted_list_item": {"rich_text": _rt(detail)},
                        }
                    ],
                },
            }
        ]

    try:
        _notion_append_block_children(token, page_id, children)
    except Exception as first:
        if style != "bullet":
            logging.warning("Sync log (toggle) failed, retry as bullet: %s", first)
            _notion_append_block_children(
                token,
                page_id,
                [
                    {
                        "object": "block",
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {"rich_text": _rt(f"{summary} — {detail}")},
                    }
                ],
            )
        else:
            raise


def _date_property_payload(row: NotionScheduleRow) -> dict[str, Any]:
    """Notion `date` property: calendar date only (YYYY-MM-DD). Period/times live in other columns."""
    d = (row.date or "").strip()
    return {"date": {"start": d}}


def _read_rows(path: str | None) -> list[NotionScheduleRow]:
    raw = ""
    if path:
        # Tolerate UTF-16LE BOM on Windows redirects.
        b = open(path, "rb").read()
        if b.startswith(b"\xff\xfe"):
            raw = b.decode("utf-16le")
        elif b.startswith(b"\xfe\xff"):
            raw = b.decode("utf-16be")
        else:
            raw = b.decode("utf-8")
        raw = raw.lstrip("\ufeff")
    else:
        raw = sys.stdin.read()

    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("Expected JSON array from scraper.py")

    rows: list[NotionScheduleRow] = []
    for obj in data:
        if not isinstance(obj, dict):
            continue
        rows.append(
            NotionScheduleRow(
                date=str(obj.get("date") or ""),
                period=str(obj.get("period") or ""),
                subject=str(obj.get("subject") or ""),
                start=obj.get("start"),
                end=obj.get("end"),
                room=obj.get("room"),
            )
        )
    rows = [r for r in rows if r.date and r.period and r.subject]
    return rows


def fetch_database_properties(token: str, db_id: str) -> dict[str, str]:
    """Returns property name -> Notion type (date, select, number, rich_text, title, ...)."""
    url = f"{NOTION_API_BASE}/databases/{db_id}"
    r = requests.get(url, headers=_notion_headers(token), timeout=30)
    if r.status_code == 404:
        raise RuntimeError(
            "Notion database not found (404). "
            "Check NOTION_DB_ID format and that the integration has access to the database."
        )
    r.raise_for_status()
    props = r.json().get("properties") or {}
    return {name: (meta.get("type") or "") for name, meta in props.items()}


def _fetch_database(token: str, db_id: str) -> dict[str, Any]:
    url = f"{NOTION_API_BASE}/databases/{db_id}"
    r = requests.get(url, headers=_notion_headers(token), timeout=30)
    if r.status_code == 404:
        raise RuntimeError(
            "Notion database not found (404). "
            "Check NOTION_DB_ID format and that the integration has access to the database."
        )
    r.raise_for_status()
    return r.json()


def _patch_database_properties(token: str, db_id: str, properties: dict[str, Any]) -> None:
    url = f"{NOTION_API_BASE}/databases/{db_id}"
    r = requests.patch(
        url,
        headers=_notion_headers(token),
        json={"properties": properties},
        timeout=30,
    )
    if r.status_code >= 400:
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:800]}
        logging.error("Notion database PATCH failed: status=%s body=%s", r.status_code, body)
        raise RuntimeError(
            f"Could not add/update database properties ({r.status_code}). "
            f"Integration needs 'Update content capabilities' on the database. Notion says: {body}"
        ) from None
    r.raise_for_status()


def _uniq_preserve(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _norm_prop_key(name: str) -> str:
    """Match Notion column names even if spacing or Unicode normalization differs from our literals."""
    return unicodedata.normalize("NFC", (name or "").strip())


def _canonical_prop_name(props: dict[str, Any], wanted: str) -> str | None:
    """Return the exact database property key for a human-intended name."""
    nw = _norm_prop_key(wanted)
    if not nw:
        return None
    for name in props:
        if _norm_prop_key(name) == nw:
            return name
    return None


def _first_free_name(candidates: list[str], props: dict[str, Any], pending: dict[str, Any]) -> str:
    taken = set(props.keys()) | set(pending.keys())
    for c in candidates:
        if c not in taken:
            return c
    for i in range(1, 100):
        c = f"SMU_field_{i}"
        if c not in taken:
            return c
    raise RuntimeError("Could not find a free Notion property name")


def _first_title_property(props: dict[str, Any]) -> str:
    for name, meta in props.items():
        if (meta.get("type") or "") == "title":
            return name
    raise RuntimeError("No Title property found in the database (Notion requires exactly one).")


def _get_property_plain(page: dict[str, Any], prop: str, ptype: str) -> str:
    t = (ptype or "").lower()
    if t == "title":
        return _get_title(page, prop)
    if t == "rich_text":
        return _get_rich_text(page, prop)
    props = page.get("properties") or {}
    p = props.get(prop) or {}
    for key in ("select", "number", "multi_select", "date", "status"):
        if key in p and p[key] is not None:
            v = p[key]
            if isinstance(v, dict) and "name" in v:
                return str(v.get("name") or "")
            if isinstance(v, list) and v:
                return ",".join(str(x.get("name", x)) for x in v if isinstance(x, dict))
            return str(v)
    return ""


def ensure_schedule_schema(
    token: str,
    db_id: str,
    *,
    pref_title: str,
    pref_date: str,
    pref_period: str,
    pref_room: str,
    no_auto: bool,
) -> tuple[ResolvedSchema, dict[str, str]]:
    """
    When no_auto is False: GET DB, pick a valid Title column, ensure Date / Period / Room columns
    exist with API-compatible types (PATCH new properties if needed), then return resolved names.
    When no_auto is True: use pref_* as-is and only return property types (no PATCH).
    """
    data = _fetch_database(token, db_id)
    props: dict[str, Any] = dict(data.get("properties") or {})

    if no_auto:
        pt = {n: (m.get("type") or "") for n, m in props.items()}
        return ResolvedSchema(pref_title, pref_date, pref_period, pref_room), pt

    title_name = _first_title_property(props)
    if pref_title != title_name:
        logging.info(
            "Using DB Title column %r (not %r); Notion allows only one Title property.",
            title_name,
            pref_title,
        )

    patch: dict[str, Any] = {}

    date_candidates = _uniq_preserve(
        [pref_date, "날짜", "Date", "Day", "SMU_수업일", "수업일", "스케줄_날짜"]
    )
    date_name: str | None = None
    for c in date_candidates:
        canon = _canonical_prop_name(props, c)
        if not canon:
            continue
        t = props[canon].get("type")
        if t == "date":
            date_name = canon
            break
        logging.warning(
            "Property %r exists but type=%r (need date). Trying another name or will add one.",
            canon,
            t,
        )
    if date_name is None:
        date_name = _first_free_name(["날짜", "SMU_수업일", "스케줄_날짜"], props, patch)
        patch[date_name] = {"date": {}}
        logging.info("Adding Date property %r via Notion API.", date_name)

    period_ok = {"select", "number", "multi_select"}
    period_candidates = _uniq_preserve([pref_period, "교시", "Period", "SMU_교시"])
    period_name: str | None = None
    for c in period_candidates:
        canon = _canonical_prop_name(props, c)
        if not canon:
            continue
        t = props[canon].get("type")
        if t in period_ok:
            period_name = canon
            break
        logging.warning(
            "Property %r exists but type=%r (need select|number|multi_select). Trying another name.",
            canon,
            t,
        )
    if period_name is None:
        period_name = _first_free_name(["교시", "SMU_교시"], props, patch)
        patch[period_name] = {"select": {"options": list(_PERIOD_SELECT_OPTIONS)}}
        logging.info("Adding period property %r (select) via Notion API.", period_name)

    room_ok = {"rich_text", "title"}
    room_candidates = _uniq_preserve([pref_room, "강의실", "Room", "Classroom", "SMU_강의실"])
    room_name: str | None = None
    for c in room_candidates:
        canon = _canonical_prop_name(props, c)
        if not canon:
            continue
        t = props[canon].get("type")
        if t in room_ok:
            room_name = canon
            break
        logging.warning(
            "Property %r exists but type=%r (need rich_text or title). Trying another name.",
            canon,
            t,
        )
    if room_name is None:
        room_name = _first_free_name(["강의실", "SMU_강의실"], props, patch)
        patch[room_name] = {"rich_text": {}}
        logging.info("Adding room property %r (rich_text) via Notion API.", room_name)

    if patch:
        _patch_database_properties(token, db_id, patch)
        data = _fetch_database(token, db_id)
        props = dict(data.get("properties") or {})

    pt = {n: (m.get("type") or "") for n, m in props.items()}
    rs = ResolvedSchema(title_name, date_name, period_name, room_name)
    for logical, name in (
        ("date", rs.prop_date),
        ("period", rs.prop_period),
        ("room", rs.prop_room),
    ):
        t = (pt.get(name) or "").lower()
        if logical == "date" and t != "date":
            raise RuntimeError(f"Resolved date property {name!r} has wrong type {t!r}")
        if logical == "period" and t not in period_ok:
            raise RuntimeError(f"Resolved period property {name!r} has wrong type {t!r}")
        if logical == "room" and t not in room_ok:
            raise RuntimeError(f"Resolved room property {name!r} has wrong type {t!r}")

    logging.info(
        "Resolved Notion schema — title=%r date=%r period=%r room=%r",
        rs.prop_title,
        rs.prop_date,
        rs.prop_period,
        rs.prop_room,
    )
    return rs, pt


def _period_filter_clause(prop_period: str, period: str, ptype: str) -> dict[str, Any]:
    """Build query filter clause for 교시 according to actual DB property type."""
    if ptype == "number":
        try:
            n = int(period)
        except ValueError:
            n = int(float(period))
        return {"property": prop_period, "number": {"equals": n}}
    if ptype == "select":
        return {"property": prop_period, "select": {"equals": period}}
    if ptype == "multi_select":
        return {"property": prop_period, "multi_select": {"contains": period}}
    if ptype == "rich_text":
        return {"property": prop_period, "rich_text": {"equals": period}}
    if ptype == "title":
        return {"property": prop_period, "title": {"equals": period}}
    # Fallback: try select (common); API will 400 if wrong — caller should fix DB or flags
    return {"property": prop_period, "select": {"equals": period}}


def archive_all_pages_in_database(token: str, db_id: str) -> int:
    """Archive every non-archived row in the database (Notion trash / restore possible). Returns count."""
    qurl = f"{NOTION_API_BASE}/databases/{db_id}/query"
    archived = 0
    cursor: str | None = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(qurl, headers=_notion_headers(token), json=body, timeout=60)
        if r.status_code >= 400:
            try:
                logging.error("Notion query (wipe) failed: %s", r.json())
            except Exception:
                logging.error("Notion query (wipe) failed: %s", r.text[:800])
            r.raise_for_status()
        data = r.json()
        for page in data.get("results") or []:
            if page.get("archived"):
                continue
            pid = page.get("id")
            if not pid:
                continue
            pr = requests.patch(
                f"{NOTION_API_BASE}/pages/{pid}",
                headers=_notion_headers(token),
                json={"archived": True},
                timeout=30,
            )
            if pr.status_code >= 400:
                try:
                    logging.error("Archive page failed: %s", pr.json())
                except Exception:
                    logging.error("Archive page failed: %s", pr.text[:800])
                pr.raise_for_status()
            archived += 1
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return archived


def notion_query_existing(
    *,
    token: str,
    db_id: str,
    date_str: str,
    period: str,
    prop_date: str,
    prop_period: str,
    prop_types: dict[str, str],
) -> dict[str, Any] | None:
    url = f"{NOTION_API_BASE}/databases/{db_id}/query"
    date_type = (prop_types.get(prop_date) or "date").lower()
    period_type = (prop_types.get(prop_period) or "select").lower()

    if date_type != "date":
        raise RuntimeError(
            f"Property '{prop_date}' must be a Notion **Date** column for upsert (Date + Period key). "
            f"Your DB reports type={date_type!r}. Rename --prop-date to match the Date column, or change the column type in Notion."
        )
    date_clause: dict[str, Any] = {"property": prop_date, "date": {"equals": date_str}}

    period_clause = _period_filter_clause(prop_period, period, period_type)

    payload: dict[str, Any] = {
        "page_size": 5,
        "filter": {"and": [date_clause, period_clause]},
    }
    r = requests.post(url, headers=_notion_headers(token), json=payload, timeout=30)
    if r.status_code == 404:
        raise RuntimeError(
            "Notion database not found (404). "
            "Check NOTION_DB_ID format and that the integration has access to the database."
        )
    if r.status_code >= 400:
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:500]}
        logging.error("Notion query failed: status=%s body=%s", r.status_code, body)
        raise RuntimeError(
            f"Notion database query failed ({r.status_code}). "
            f"Usually: property names must match DB exactly, and filter types must match "
            f"(e.g. 교시 as Number needs number filter, not select). Notion says: {body}"
        ) from None
    res = r.json()
    results = res.get("results") or []
    if not results:
        return None
    return results[0]


def _get_title(page: dict[str, Any], prop_title: str) -> str:
    props = page.get("properties") or {}
    p = props.get(prop_title) or {}
    title = p.get("title") or []
    if not title:
        return ""
    return "".join((t.get("plain_text") or "") for t in title).strip()


def _get_rich_text(page: dict[str, Any], prop: str) -> str:
    props = page.get("properties") or {}
    p = props.get(prop) or {}
    rt = p.get("rich_text") or []
    if not rt:
        return ""
    return "".join((t.get("plain_text") or "") for t in rt).strip()


def _period_property_value(period: str, ptype: str) -> dict[str, Any]:
    if ptype == "number":
        try:
            n = int(period)
        except ValueError:
            n = int(float(period))
        return {"number": n}
    if ptype == "multi_select":
        return {"multi_select": [{"name": period}]}
    return {"select": {"name": period}}


def _room_property_value(room: str | None, rtype: str) -> dict[str, Any]:
    val = room or ""
    if rtype == "rich_text":
        return {"rich_text": [{"text": {"content": val}}]}
    if rtype == "title":
        return {"title": [{"text": {"content": val}}]}
    return {"rich_text": [{"text": {"content": val}}]}


def notion_create_or_update(
    *,
    token: str,
    db_id: str,
    row: NotionScheduleRow,
    prop_title: str,
    prop_date: str,
    prop_period: str,
    prop_room: str,
    prop_types: dict[str, str],
) -> None:
    existing = notion_query_existing(
        token=token,
        db_id=db_id,
        date_str=row.date,
        period=row.period,
        prop_date=prop_date,
        prop_period=prop_period,
        prop_types=prop_types,
    )

    title_text = f"[{row.room}] {row.subject}" if (row.room and row.room.strip()) else row.subject

    period_type = (prop_types.get(prop_period) or "select").lower()
    room_type = (prop_types.get(prop_room) or "rich_text").lower()

    props: dict[str, Any] = {
        prop_title: {"title": [{"text": {"content": title_text}}]},
        prop_period: _period_property_value(row.period, period_type),
        prop_date: _date_property_payload(row),
        prop_room: _room_property_value(row.room, room_type),
    }

    if existing:
        page_id = existing["id"]
        old_title = _get_title(existing, prop_title)
        old_room = _get_property_plain(existing, prop_room, room_type)
        if (old_title and old_title != title_text) or ((old_room or "") != (row.room or "")):
            logging.warning(
                "[ALERT] %s P%s changed: title '%s'->'%s', room '%s'->'%s'",
                row.date,
                row.period,
                old_title,
                title_text,
                old_room,
                row.room or "",
            )

        url = f"{NOTION_API_BASE}/pages/{page_id}"
        r = requests.patch(url, headers=_notion_headers(token), json={"properties": props}, timeout=30)
        if r.status_code >= 400:
            try:
                logging.error("Notion patch failed: %s", r.json())
            except Exception:
                logging.error("Notion patch failed: %s", r.text[:800])
        r.raise_for_status()
        logging.info("Updated %s P%s (%s)", row.date, row.period, page_id)
        return

    url = f"{NOTION_API_BASE}/pages"
    payload = {"parent": {"database_id": db_id}, "properties": props}
    r = requests.post(url, headers=_notion_headers(token), json=payload, timeout=30)
    if r.status_code >= 400:
        try:
            logging.error("Notion create failed: %s", r.json())
        except Exception:
            logging.error("Notion create failed: %s", r.text[:800])
    r.raise_for_status()
    page_id = r.json().get("id", "")
    logging.info("Created %s P%s (%s)", row.date, row.period, page_id)


def main() -> None:
    ap = argparse.ArgumentParser(description="Upsert SMU schedule JSON into Notion database")
    ap.add_argument("--input", default=None, help="Path to JSON file from scraper.py (default: stdin)")
    ap.add_argument("--log-level", default="INFO", help="DEBUG|INFO|WARNING|ERROR")
    ap.add_argument("--prop-title", default="강의명", help="Preferred Title column name (auto-schema uses the DB's only Title column)")
    ap.add_argument("--prop-date", default="날짜", help="Preferred Date property name (wrong type → API adds SMU_수업일)")
    ap.add_argument("--prop-period", default="교시", help="Preferred period property (select|number|multi_select)")
    ap.add_argument("--prop-room", default="강의실", help="Preferred room property (rich_text or title)")
    ap.add_argument(
        "--no-auto-schema",
        action="store_true",
        help="Do not PATCH the database or remap properties; use --prop-* names exactly as given.",
    )
    ap.add_argument(
        "--wipe-first",
        action="store_true",
        help="Archive all rows in the target database before syncing (dedupe after date-format changes).",
    )
    ap.add_argument(
        "--no-sync-log",
        action="store_true",
        help="Do not append 갱신 이력 (toggle/bullet) to a Notion page.",
    )
    ap.add_argument(
        "--sync-log-style",
        choices=["toggle", "bullet"],
        default=None,
        help="Sync history block style (default: NOTION_SYNC_LOG_STYLE env or toggle).",
    )
    args = ap.parse_args()

    _setup_logging(args.log_level)
    load_dotenv()

    token = _env("NOTION_TOKEN") or ""
    db_id = _env("NOTION_DB_ID") or ""

    resolved, prop_types = ensure_schedule_schema(
        token,
        db_id,
        pref_title=args.prop_title,
        pref_date=args.prop_date,
        pref_period=args.prop_period,
        pref_room=args.prop_room,
        no_auto=args.no_auto_schema,
    )
    for key in (
        resolved.prop_title,
        resolved.prop_date,
        resolved.prop_period,
        resolved.prop_room,
    ):
        t = prop_types.get(key)
        if not t:
            logging.warning("DB has no property named %r — check spelling vs Notion column names.", key)
        else:
            logging.info("Notion property %r type=%s", key, t)

    rows = _read_rows(args.input)
    logging.info("Loaded %s rows from scraper JSON", len(rows))

    if args.wipe_first:
        n = archive_all_pages_in_database(token, db_id)
        logging.warning("Archived %s existing database row(s); re-inserting from JSON.", n)

    # Deterministic order makes diffs/alerts easier to read.
    rows = sorted(rows, key=lambda r: (r.date, int(r.period) if r.period.isdigit() else 999, r.subject))

    started = datetime.now()
    for row in rows:
        notion_create_or_update(
            token=token,
            db_id=db_id,
            row=row,
            prop_title=resolved.prop_title,
            prop_date=resolved.prop_date,
            prop_period=resolved.prop_period,
            prop_room=resolved.prop_room,
            prop_types=prop_types,
        )

    elapsed = (datetime.now() - started).total_seconds()
    logging.info("Done. upserted=%s elapsed_s=%.2f", len(rows), elapsed)

    if not args.no_sync_log:
        log_page = _sync_log_target_page_id(token, db_id)
        if log_page:
            style = (args.sync_log_style or os.getenv("NOTION_SYNC_LOG_STYLE") or "toggle").strip().lower()
            if style not in ("toggle", "bullet"):
                style = "toggle"
            try:
                _write_sync_log_entry(
                    token,
                    log_page,
                    row_count=len(rows),
                    elapsed_s=elapsed,
                    wiped=bool(args.wipe_first),
                    style=style,
                )
                logging.info("Notion 갱신 이력을 페이지에 추가함 (page_id=%s)", log_page)
            except Exception as e:
                logging.warning(
                    "갱신 이력 블록 추가 실패(해당 페이지를 연동에 공유했는지 확인): %s",
                    e,
                )
        else:
            logging.info(
                "갱신 이력 생략: DB가 워크스페이스 직속이거나 부모를 알 수 없음. "
                "별도 페이지에 쓰려면 .env에 NOTION_SYNC_LOG_PAGE_ID=<페이지 URL의 ID> 를 넣으세요."
            )


if __name__ == "__main__":
    main()

