from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Literal

from dotenv import load_dotenv
from playwright.async_api import Browser, Page, async_playwright


PORTAL_URL = "https://cp-portal.sapmed.ac.jp/portal/#"

# SMU portal period → official slot (not scraped; overwrites cell/tooltip times for P1–P5).
PERIOD_SLOT_TIMES: dict[str, tuple[time, time]] = {
    "1": (time(9, 0), time(10, 30)),
    "2": (time(10, 40), time(12, 10)),
    "3": (time(13, 10), time(14, 40)),
    "4": (time(14, 50), time(16, 20)),
    "5": (time(16, 30), time(18, 0)),
}

# 포털에 강의실이 안 뜰 때 Notion/JSON에 넣을 고정값 (사용자 확정)
ROOM_ENGLISH1_ZENKI = "ｳｨｰﾗｰ：D402"
ROOM_ENGLISH1_ZENKI_JUNE09 = "ｳｨｰﾗｰ：C302"  # 6/9 例외
ROOM_ENGLISH1_KOUKI = "白土：D101"
ROOM_RUSSIAN = "D502（多目的演習室）"
ROOM_EIKAIWA_ZENKI_SUGARMAN = "D303"
ROOM_EIKAIWA_KOUKI = "ｹｲﾝ：D401"
ROOM_EIKAIWA_KOUKI_JUNE10 = "ｹｲﾝ：C301"


@dataclass(frozen=True)
class ScheduleItem:
    day: date
    period: str  # "1".."5" (or whatever portal shows)
    subject: str
    start: time | None = None
    end: time | None = None
    room: str | None = None
    code: str | None = None
    raw: str | None = None


def _infer_term(d: date) -> Literal["前期", "後期"]:
    # Typical Japanese university split: 前期=Apr-Sep, 後期=Oct-Mar.
    return "前期" if 4 <= d.month <= 9 else "後期"


def _subject_is_russian(subj: str) -> bool:
    """ロシア語 / 露語 / 캘린더에 '露'만 올라오는 경우 등."""
    s = unicodedata.normalize("NFKC", (subj or "").strip())
    s = s.replace("\u3000", " ")
    if "ロシア" in s:
        return True
    if "露語" in s:
        return True
    if s == "露":
        return True
    if "ロシア語" in s:
        return True
    return False


def _room_effectively_missing(room: str | None) -> bool:
    """포털이 강의실을 안 준 경우만 고정값 채움(스크랩 값이 있으면 그대로 둠)."""
    if room is None:
        return True
    s = unicodedata.normalize("NFKC", str(room).strip()).replace("\u3000", " ")
    if not s:
        return True
    if s in {":", "：", "F", "Ｆ"}:
        return True
    return False


def apply_room_overrides(items: list[ScheduleItem]) -> list[ScheduleItem]:
    """
    아래 과목만, 포털에 강의실이 비어 있을 때 규칙으로 채움. 그 외·스크랩에 룸이 있으면 파싱값 유지.
    - 医学英語1/英語1: 前期 D402(6/9만 C302), 後期 白土 D101
    - ロシア語系: D502（多目的演習室）
    - 英会話: apply_user_schedule_postprocess (같은 조건으로 룸 보정)
    """
    out: list[ScheduleItem] = []
    for it in items:
        subj = (it.subject or "").strip()
        term = _infer_term(it.day)
        room = it.room

        # English 1 (医学英語1 / 英語1)
        if re.search(r"(医学英語\s*[１1]|英語\s*[１1])", subj):
            if _room_effectively_missing(room):
                if term == "前期":
                    room = ROOM_ENGLISH1_ZENKI
                    if it.day.month == 6 and it.day.day == 9:
                        room = ROOM_ENGLISH1_ZENKI_JUNE09
                else:
                    room = ROOM_ENGLISH1_KOUKI

        # Russian — site often omits room
        elif _subject_is_russian(subj):
            if _room_effectively_missing(room):
                room = ROOM_RUSSIAN

        # 英会話は apply_user_schedule_postprocess で学期별 정리

        out.append(
            ScheduleItem(
                day=it.day,
                period=it.period,
                subject=it.subject,
                start=it.start,
                end=it.end,
                room=room,
                code=it.code,
                raw=it.raw,
            )
        )
    return out


# 選択科目で履修しないため時間表に載せない（2026年度ユーザー設定）
_EXCLUDED_ELECTIVE_MARKERS: tuple[str, ...] = (
    "言語学",
    "人類学",
)


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", (s or ""))


def apply_user_schedule_postprocess(items: list[ScheduleItem]) -> list[ScheduleItem]:
    """
    사용자 후처리:
    - 言語学・人類学: 스크랩돼도 시간표(·Notion)에서 제외
    - 英会話: 前期は シュガーマン 분만 유지, 표시 英会話(シュガーマン).
      강의실은 포털이 비었을 때만 D303 등 규칙값; 스크랩 값이 있으면 유지.
      후期は ウィーラー 분만 유지; 강의실도 동일.
    """
    out: list[ScheduleItem] = []
    for it in items:
        subj = (it.subject or "").strip()
        s = subj.replace("\u3000", " ")
        s = _nfkc(s)

        if any(marker in s for marker in _EXCLUDED_ELECTIVE_MARKERS):
            logging.info(
                "[POSTPROCESS_DROP] elective_not_taken subject=%r date=%s P%s",
                subj,
                it.day.isoformat(),
                it.period,
            )
            continue

        if "英会話" not in s:
            out.append(it)
            continue

        term = _infer_term(it.day)
        has_sug = "シュガーマン" in s
        has_wheel = "ウィーラー" in s
        named = has_sug or has_wheel or "前：" in s or "後：" in s

        if term == "前期":
            if named and not has_sug:
                logging.info(
                    "[POSTPROCESS_DROP] eikaiwa_zenki_needs_sugarman subject=%r date=%s P%s",
                    subj,
                    it.day.isoformat(),
                    it.period,
                )
                continue
            new_room = (
                ROOM_EIKAIWA_ZENKI_SUGARMAN
                if _room_effectively_missing(it.room)
                else it.room
            )
            out.append(
                ScheduleItem(
                    day=it.day,
                    period=it.period,
                    subject="英会話(シュガーマン)",
                    start=it.start,
                    end=it.end,
                    room=new_room,
                    code=it.code,
                    raw=it.raw,
                )
            )
            continue

        # 後期: ウィーラー班のみ（前期教師名のみなら除く）
        if named and has_sug and not has_wheel:
            logging.info(
                "[POSTPROCESS_DROP] eikaiwa_kouki_sugarman_only subject=%r date=%s P%s",
                subj,
                it.day.isoformat(),
                it.period,
            )
            continue
        rule_kouki = (
            ROOM_EIKAIWA_KOUKI_JUNE10 if (it.day.month == 6 and it.day.day == 10) else ROOM_EIKAIWA_KOUKI
        )
        new_room = rule_kouki if _room_effectively_missing(it.room) else it.room
        out.append(
            ScheduleItem(
                day=it.day,
                period=it.period,
                subject="英会話(ウィーラー)",
                start=it.start,
                end=it.end,
                room=new_room,
                code=it.code,
                raw=it.raw,
            )
        )

    logging.info(
        "[POSTPROCESS_SUMMARY] items_after=%s (excludes 言語学/人類学; 英会話 normalized)",
        len(out),
    )
    return out


def apply_sibling_room_fallback(items: list[ScheduleItem]) -> list[ScheduleItem]:
    """
    같은 주 결과 안에서 (과목·교시)가 동일한 다른 칸에 room이 있으면, 비어 있는 칸에 복사.
    특정 요일만 호버/툴팁이 비는 경우 보조.
    """
    from collections import Counter

    def sp_key(it: ScheduleItem) -> tuple[str, str]:
        return (
            _nfkc((it.subject or "").strip()).casefold(),
            _normalize_period_key(str(it.period)),
        )

    rooms_by: dict[tuple[str, str], list[str | None]] = {}
    for it in items:
        rooms_by.setdefault(sp_key(it), []).append(it.room)

    def majority_room(vals: list[str | None]) -> str | None:
        good = [v for v in vals if not _room_effectively_missing(v)]
        if not good:
            return None
        return Counter(good).most_common(1)[0][0]

    mode: dict[tuple[str, str], str | None] = {k: majority_room(v) for k, v in rooms_by.items()}
    out: list[ScheduleItem] = []
    filled = 0
    for it in items:
        if not _room_effectively_missing(it.room):
            out.append(it)
            continue
        mr = mode.get(sp_key(it))
        if mr:
            filled += 1
            out.append(
                ScheduleItem(
                    day=it.day,
                    period=it.period,
                    subject=it.subject,
                    start=it.start,
                    end=it.end,
                    room=mr,
                    code=it.code,
                    raw=it.raw,
                )
            )
        else:
            out.append(it)
    if filled:
        logging.info("[ROOM_SIBLING] filled %s empty slot(s) from same subject+period in week", filled)
    return out


def apply_optional_room_hints(items: list[ScheduleItem]) -> list[ScheduleItem]:
    """
    저장소 루트의 room_hints.json — 과목명 부분 문자열 → 강의실 (포털이 안 줄 때만).
    예: {"初年次セミナー": "教研1F D101"}  （room_hints.example.json 참고）
    """
    p = Path(__file__).resolve().parent / "room_hints.json"
    if not p.is_file():
        return items
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logging.warning("[ROOM_HINTS] skip %s: %s", p, e)
        return items
    if not isinstance(data, dict):
        return items
    out: list[ScheduleItem] = []
    applied = 0
    for it in items:
        if not _room_effectively_missing(it.room):
            out.append(it)
            continue
        subj_nf = _nfkc(it.subject or "").strip()
        hint_room: str | None = None
        for key, val in data.items():
            if not isinstance(key, str) or not isinstance(val, str):
                continue
            k = _nfkc(key).strip()
            vs = val.strip()
            if not k or not vs:
                continue
            if k in subj_nf or k.casefold() in subj_nf.casefold():
                hint_room = vs
                break
        if hint_room:
            applied += 1
            out.append(
                ScheduleItem(
                    day=it.day,
                    period=it.period,
                    subject=it.subject,
                    start=it.start,
                    end=it.end,
                    room=hint_room,
                    code=it.code,
                    raw=it.raw,
                )
            )
        else:
            out.append(it)
    if applied:
        logging.info("[ROOM_HINTS] filled %s row(s) from room_hints.json", applied)
    return out


def collect_room_conflicts(
    parsed_items: list[ScheduleItem],
    final_items: list[ScheduleItem],
) -> list[dict[str, str]]:
    """
    포털 원본(parsed)에 강의실 문자열이 있는데, 후처리 후 최종 값과 다르면 충돌로 본다.
    (빈 스크랩 + 규칙으로 채운 경우는 제외 — 의도된 동작.)
    """

    def sig(r: str | None) -> str:
        if not r or not str(r).strip():
            return ""
        return unicodedata.normalize("NFKC", str(r).strip()).casefold()

    before_map: dict[tuple[str, str], tuple[str, str]] = {}
    for it in parsed_items:
        k = (it.day.isoformat(), str(it.period).strip())
        before_map[k] = (it.subject or "", it.room or "")

    out: list[dict[str, str]] = []
    for it in final_items:
        k = (it.day.isoformat(), str(it.period).strip())
        if k not in before_map:
            continue
        osubj, oroom = before_map[k]
        nsubj = it.subject or ""
        nroom = it.room or ""
        if sig(oroom) and sig(nroom) and sig(oroom) != sig(nroom):
            out.append(
                {
                    "date": k[0],
                    "period": k[1],
                    "scraped_subject": osubj,
                    "final_subject": nsubj,
                    "scraped_room": oroom,
                    "final_room": nroom,
                }
            )
    if out:
        logging.warning(
            "[ROOM_CONFLICT] scraped vs rule-based room differ for %s slot(s); see artifacts/room_conflicts.json",
            len(out),
        )
    return out


def write_room_conflicts_artifact(conflicts: list[dict[str, str]]) -> None:
    os.makedirs("artifacts", exist_ok=True)
    path = os.path.join("artifacts", "room_conflicts.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"checked_at": datetime.now(timezone.utc).isoformat(), "conflicts": conflicts},
            f,
            ensure_ascii=False,
            indent=2,
        )


def _normalize_period_key(period: str) -> str:
    """e.g. '１', '1限' -> '1' for slot lookup."""
    p = unicodedata.normalize("NFKC", (period or "").strip())
    m = re.match(r"^(\d+)", p)
    return m.group(1) if m else ""


def apply_period_slot_times(items: list[ScheduleItem]) -> list[ScheduleItem]:
    """Set start/end from official timetable for periods 1–5; leave other periods unchanged."""
    out: list[ScheduleItem] = []
    for it in items:
        key = _normalize_period_key(it.period)
        slot = PERIOD_SLOT_TIMES.get(key)
        if slot:
            st, en = slot
            out.append(
                ScheduleItem(
                    day=it.day,
                    period=it.period,
                    subject=it.subject,
                    start=st,
                    end=en,
                    room=it.room,
                    code=it.code,
                    raw=it.raw,
                )
            )
        else:
            out.append(it)
    return out


def _env(name: str, *, required: bool = True) -> str | None:
    val = os.getenv(name)
    if required and (val is None or not str(val).strip()):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


async def _dismiss_portal_warnings(page: Page) -> None:
    """
    Best-effort dismissal for common portal blocking UIs:
    - session expired messages
    - multi-tab / multiple login warnings
    - modal dialogs with OK/閉じる
    """
    texts = [
        "セッション",
        "期限",
        "失効",
        "多重",
        "別のタブ",
        "他のタブ",
        "ログイン",
        "エラー",
        "警告",
        "Session",
        "expired",
        "multi",
        "tab",
    ]

    async def click_if_visible(locator) -> bool:
        try:
            if await locator.first.is_visible(timeout=800):
                await locator.first.click(timeout=1500)
                return True
        except Exception:
            return False
        return False

    # Try a few quick passes; these warnings often appear after navigation.
    for _ in range(5):
        dismissed_any = False
        # Generic dialog buttons.
        for name in ["OK", "Ok", "閉じる", "閉じます", "はい", "確認", "続行", "再ログイン"]:
            dismissed_any |= await click_if_visible(page.get_by_role("button", name=name))

        # Any visible button in a dialog containing warning keywords.
        for t in texts:
            dialog = page.locator(f"text={t}").first
            try:
                if await dialog.is_visible(timeout=400):
                    # Click the nearest "OK"/close-ish button inside the same modal region if possible.
                    modal = dialog.locator("xpath=ancestor-or-self::*[self::div or self::section][.//button]").first
                    btn = modal.get_by_role("button").filter(has_text=re.compile(r"(OK|閉じ|はい|確認|続行)", re.I))
                    if await click_if_visible(btn):
                        dismissed_any = True
                        break
            except Exception:
                pass

        if not dismissed_any:
            break
        await page.wait_for_timeout(300)


def _mask_login_identifier(ident: str) -> str:
    """Log-safe: never print full UPN in logs (still enough to see domain / typo class)."""
    s = (ident or "").strip()
    if not s:
        return "(empty)"
    if "@" not in s:
        return f"{s[:2]}***" if len(s) > 2 else "***"
    local, _, domain = s.partition("@")
    return f"{local[:2]}***@{domain}" if len(local) > 2 else f"***@{domain}"


async def _microsoft_stay_signed_in_kmsi(page: Page) -> None:
    """
    Microsoft 「サインインの状態を維持しますか？」 / Stay signed in?
    Prefer はい + 「今後このメッセージを表示しない」 so automated runs hit SSO less often.
    """
    try:
        heading = page.get_by_text(re.compile(r"(サインインの状態を維持|Stay signed in)", re.I))
        if not await heading.first.is_visible(timeout=15_000):
            return
    except Exception:
        return

    try:
        for label_re in (
            re.compile(r"今後このメッセージを表示しない"),
            re.compile(r"Don't show this again", re.I),
        ):
            cb = page.get_by_role("checkbox", name=label_re)
            if await cb.count() and await cb.first.is_visible(timeout=2000):
                if not await cb.first.is_checked():
                    await cb.first.check()
                break
        else:
            generic = page.locator("input[type='checkbox']").first
            if await generic.is_visible(timeout=1500) and not await generic.is_checked():
                await generic.check()
    except Exception:
        pass

    try:
        yes = page.get_by_role("button", name=re.compile(r"^(はい|Yes)$", re.I))
        if await yes.first.is_visible(timeout=8000):
            await yes.first.click()
            await page.wait_for_timeout(600)
            logging.info("Microsoft login: confirmed 'Stay signed in' (はい) for longer-lived session")
            return
    except Exception:
        pass

    try:
        no_btn = page.get_by_role("button", name=re.compile(r"^(いいえ|No)$", re.I))
        if await no_btn.first.is_visible(timeout=3000):
            await no_btn.first.click()
            await page.wait_for_timeout(400)
            logging.info("Microsoft login: clicked いいえ on Stay signed in (fallback)")
    except Exception:
        pass


async def _microsoft_bypass_account_picker(page: Page) -> None:
    """
    Azure AD sometimes opens on 'pick an account' (saved sessions) with no email <input> yet.
    We must open the real identifier form — usually 'Use another account' / 別のアカウントを使用する.
    """
    try:
        other = page.get_by_text(
            re.compile(r"(別のアカウントを使用する|別のワークまたは\s*学校アカウント|Use another account)", re.I)
        )
        if await other.first.is_visible(timeout=2500):
            await other.first.click(timeout=5000)
            await page.wait_for_timeout(600)
            logging.info("Microsoft login: clicked 'use another account' to reach email form")
            return
    except Exception:
        pass
    # Tile / link variants
    for pat in [
        re.compile(r"^別のアカウントを使用する$"),
        re.compile(r"^Use another account$", re.I),
    ]:
        try:
            loc = page.get_by_role("link", name=pat)
            if await loc.first.is_visible(timeout=800):
                await loc.first.click(timeout=5000)
                await page.wait_for_timeout(600)
                logging.info("Microsoft login: bypassed account picker (link)")
                return
        except Exception:
            continue


async def _microsoft_login_page_diagnostics(page: Page) -> str:
    """
    Best-effort: surface Azure AD / Microsoft sign-in error text that our heuristic skipped.
    The generic RuntimeError used to mean only "password field did not appear".
    """
    parts: list[str] = []
    selectors = [
        "#usernameError",
        "#passwordError",
        '[id*="usernameError" i]',
        '[id*="PasswordError" i]',
        '[id*="LoginMessage" i]',
        '[data-bind*="error" i]',
        'div[role="alert"]',
        ".error.pageLevel",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=400):
                t = re.sub(r"\s+", " ", (await loc.inner_text()).strip())
                if t and t not in parts:
                    parts.append(f"{sel} → {t[:400]}")
        except Exception:
            continue
    try:
        title = await page.title()
        if title:
            parts.append(f"title={title.strip()[:120]}")
    except Exception:
        pass
    return " | ".join(parts) if parts else "(no known error nodes matched)"


async def login(page: Page, username: str, password: str) -> None:
    page.set_default_timeout(30_000)
    page.set_default_navigation_timeout(45_000)

    await page.goto(PORTAL_URL, wait_until="domcontentloaded")
    # Some deployments render the actual login UI after hydration.
    await page.wait_for_timeout(1200)
    await _dismiss_portal_warnings(page)

    async def already_logged_in() -> bool:
        # Heuristics for a logged-in portal shell.
        for loc in [
            page.locator('a[href*="/portal/Profile" i]'),
            page.get_by_role("link", name=re.compile(r"プロフィール")),
            page.get_by_text(re.compile(r"前回ログイン")),
            page.locator("#CPLoginIdPortal"),
            page.get_by_text(re.compile(r"(さん)$")),
        ]:
            try:
                if await loc.first.is_visible(timeout=800):
                    return True
            except Exception:
                continue
        return False

    # If a storage_state is used (or session still valid), the portal opens already authenticated.
    if await already_logged_in():
        return

    async def login_via_microsoft() -> None:
        """
        Handles Azure AD (Microsoft) sign-in pages.
        Portal redirects to `login.microsoftonline.com/.../saml2`.
        """

        async def try_identifier(identifier: str) -> bool:
            await _microsoft_bypass_account_picker(page)
            email = page.locator('input[type="email"], input[name="loginfmt"], input[id="i0116"]')
            await email.wait_for(state="visible", timeout=30_000)
            await email.fill(identifier)
            # Next
            next_btn = page.get_by_role("button", name=re.compile(r"^(次へ|Next)$"))
            if not await next_btn.first.is_visible(timeout=1500):
                next_btn = page.locator('input[type="submit"], button[type="submit"], #idSIButton9')
            await next_btn.first.click()

            # Success heuristic: password field becomes visible shortly after Next.
            # Note: this is NOT "account rejected" — many states keep you on the email step
            # (unknown user, federated redirect, throttling, UX change, slow network).
            pw = page.locator('input[type="password"], input[name="passwd"], #i0118')
            try:
                await pw.wait_for(state="visible", timeout=12_000)
                return True
            except Exception:
                return False

        # Some tenants prefill; clear and proceed.
        identifiers: list[str] = [username]
        if "@" not in username:
            # Best-effort: common UPN patterns for university tenants.
            domains = [
                os.getenv("PORTAL_USER_DOMAIN"),
                "sapmed.ac.jp",
                "stu.sapmed.ac.jp",
                "student.sapmed.ac.jp",
                "g.sapmed.ac.jp",
            ]
            for d in [d for d in domains if d]:
                identifiers.append(f"{username}@{d}")

        # First paint may be account chooser, not the email field.
        await _microsoft_bypass_account_picker(page)

        ok = False
        for ident in identifiers:
            last_exc: str | None = None
            try:
                ok = await try_identifier(ident)
            except Exception as e:
                ok = False
                last_exc = f"{type(e).__name__}: {e}"
            if not ok:
                hint = await _microsoft_login_page_diagnostics(page)
                logging.warning(
                    "Microsoft login: identifier did not reach password step (masked=%s). url=%s last_exc=%s hints=%s",
                    _mask_login_identifier(ident),
                    page.url,
                    last_exc or "",
                    hint,
                )
            else:
                logging.info("Microsoft login: reached password step with masked identifier %s", _mask_login_identifier(ident))
                break
            last_exc = None
        if not ok:
            hints = await _microsoft_login_page_diagnostics(page)
            logging.error(
                "Microsoft login: exhausted identifier list; never saw password field. url=%s hints=%s",
                page.url,
                hints,
            )
            raise RuntimeError(
                "Microsoft login: could not reach the password step after trying UPN variants. "
                "This usually means (1) PORTAL_USER must be the full school email/UPN, "
                "(2) wrong/typo domain → set PORTAL_USER_DOMAIN, "
                "(3) account requires a different sign-in flow (federated/MFA/device prompt), "
                "or (4) AAD showed an error that blocked progression. "
                f"page_hints={hints}"
            )

        # Password step
        pw = page.locator('input[type="password"], input[name="passwd"], #i0118')
        await pw.wait_for(state="visible", timeout=30_000)
        await pw.fill(password)

        signin = page.get_by_role("button", name=re.compile(r"^(サインイン|Sign in)$"))
        if not await signin.first.is_visible(timeout=1500):
            signin = page.locator('input[type="submit"], button[type="submit"], #idSIButton9')
        await signin.first.click()
        await page.wait_for_timeout(900)
        await _microsoft_stay_signed_in_kmsi(page)

        # Wait until redirected back to the portal. MFA detection runs in parallel but must NOT time out
        # when no MFA UI exists (the old wait_for_selector(30s) "lost" the race and aborted a good login).
        async def wait_portal() -> None:
            await page.wait_for_url(re.compile(r"https://cp-portal\.sapmed\.ac\.jp/.*"), timeout=90_000)

        async def wait_mfa_poll() -> None:
            needles = [
                re.compile(r"追加の情報"),
                re.compile(r"Authenticator"),
                re.compile(r"確認コード"),
                re.compile(r"コードを入力"),
                re.compile(r"\bMFA\b", re.I),
                re.compile(r"\bVerify\b"),
                # Japanese-aware (avoid a single fragile comma-separated selector)
                re.compile(r"承認"),
            ]
            while True:
                for rx in needles:
                    try:
                        if await page.get_by_text(rx).first.is_visible(timeout=120):
                            raise RuntimeError(
                                "Microsoft sign-in requires additional interactive verification (MFA/step-up)."
                            )
                    except RuntimeError:
                        raise
                    except Exception:
                        continue
                await asyncio.sleep(0.35)

        done, pending = await asyncio.wait(
            {asyncio.create_task(wait_portal()), asyncio.create_task(wait_mfa_poll())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        # Re-raise exceptions if any task failed.
        for t in done:
            exc = t.exception()
            if exc:
                raise exc

    # If the portal immediately redirects to Microsoft sign-in, handle it.
    if "login.microsoftonline.com" in page.url:
        await login_via_microsoft()
        await _dismiss_portal_warnings(page)
        return

    # Sometimes the portal redirects to Microsoft after a brief delay.
    try:
        await page.wait_for_url(re.compile(r".*login\.microsoftonline\.com/.*"), timeout=4_000)
        await login_via_microsoft()
        await _dismiss_portal_warnings(page)
        return
    except Exception:
        pass

    if await already_logged_in():
        return

    # If there's an entry link/button to open the login form, click it.
    for entry in [
        page.get_by_role("link", name=re.compile(r"(ログイン|Login|Sign in)", re.I)),
        page.get_by_role("button", name=re.compile(r"(ログイン|Login|Sign in)", re.I)),
        page.get_by_text(re.compile(r"(ログイン|Login|Sign in)", re.I)),
    ]:
        try:
            if await entry.first.is_visible(timeout=1200):
                await entry.first.click(timeout=3000)
                await page.wait_for_timeout(800)
                break
        except Exception:
            continue

    if await already_logged_in():
        return

    async def fill_across_frames(kind: Literal["user", "pass"], value: str) -> None:
        """
        Try to find and fill login inputs across main page and iframes.
        """
        last_err: Exception | None = None

        def candidates_for(root) -> list[Any]:
            if kind == "user":
                return [
                    root.get_by_role("textbox", name=re.compile(r"(ユーザ|ユーザー|User|ID|学籍|ログイン)", re.I)),
                    root.locator('input[autocomplete="username"]'),
                    root.locator('input[name*="user" i], input[id*="user" i], input[name*="login" i], input[id*="login" i]'),
                    root.locator('input[type="text"], input:not([type])'),
                ]
            return [
                root.get_by_role("textbox", name=re.compile(r"(パスワード|Password)", re.I)),
                root.locator('input[autocomplete="current-password"]'),
                root.locator('input[name*="pass" i], input[id*="pass" i]'),
                root.locator('input[type="password"]'),
            ]

        # Multiple attempts: SPAs/iframes can attach late.
        for _ in range(8):
            for root in [page, *page.frames]:
                for loc in candidates_for(root):
                    try:
                        if await loc.first.is_visible(timeout=900):
                            await loc.first.fill(value)
                            return
                    except Exception as e:
                        last_err = e
            await page.wait_for_timeout(500)
            await _dismiss_portal_warnings(page)

        raise RuntimeError("Could not find a visible login input to fill") from last_err

    await fill_across_frames("user", username)
    await fill_across_frames("pass", password)

    login_btn_candidates = [
        page.get_by_role("button", name=re.compile(r"(ログイン|Login|Sign in)", re.I)),
        page.get_by_role("link", name=re.compile(r"(ログイン|Login|Sign in)", re.I)),
        page.locator('button[type="submit"]'),
        page.locator('input[type="submit"]'),
    ]

    clicked = False
    for btn in login_btn_candidates:
        try:
            if await btn.first.is_visible(timeout=2500):
                await btn.first.click()
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        # Last resort: press Enter on any visible password input across frames.
        pressed = False
        for root in [page, *page.frames]:
            try:
                pw_loc = root.locator('input[type="password"], input[autocomplete="current-password"]')
                if await pw_loc.first.is_visible(timeout=800):
                    await pw_loc.first.press("Enter")
                    pressed = True
                    break
            except Exception:
                continue
        if not pressed:
            raise RuntimeError("Could not submit login form")

    # Post-login: portal may SPA-navigate; wait for something that indicates dashboard.
    await page.wait_for_timeout(800)
    await _dismiss_portal_warnings(page)

    # Either weekly schedule heading or any portal shell appears.
    await page.wait_for_selector(
        "text=週間スケジュール, text=Weekly, text=ポータル, nav, #app",
        state="visible",
        timeout=60_000,
    )
    await _dismiss_portal_warnings(page)


async def _extract_table_grid(page: Page, table_selector: str) -> list[list[str]]:
    """
    Returns a rectangular grid of table cell texts with rowspan/colspan expanded.
    """
    js = """
    (sel) => {
      const table = document.querySelector(sel);
      if (!table) return null;
      const rows = Array.from(table.querySelectorAll('tr'));
      const grid = [];
      const spans = []; // spans[col] = {left, text}

      const cellText = (cell) => {
        const t = (cell.innerText || cell.textContent || '').replace(/\\u00a0/g, ' ');
        return t.replace(/[\\s\\r\\n\\t]+/g, ' ').trim();
      };

      for (let r = 0; r < rows.length; r++) {
        const row = [];
        let col = 0;

        // apply pending rowspans
        while (spans[col]) {
          row[col] = spans[col].text;
          spans[col].left -= 1;
          if (spans[col].left <= 0) spans[col] = null;
          col++;
        }

        const cells = Array.from(rows[r].children).filter(el => el.tagName === 'TD' || el.tagName === 'TH');
        for (const cell of cells) {
          while (row[col] !== undefined) col++;
          const txt = cellText(cell);
          const rs = parseInt(cell.getAttribute('rowspan') || '1', 10);
          const cs = parseInt(cell.getAttribute('colspan') || '1', 10);
          for (let i = 0; i < cs; i++) {
            row[col + i] = txt;
            if (rs > 1) {
              spans[col + i] = { left: rs - 1, text: txt };
            }
          }
          col += cs;
        }

        // finalize: fill any remaining active spans to keep rectangularity
        let c = 0;
        while (spans[c]) {
          if (row[c] === undefined) {
            row[c] = spans[c].text;
            spans[c].left -= 1;
            if (spans[c].left <= 0) spans[c] = null;
          }
          c++;
        }

        grid.push(row.map(v => (v === undefined ? '' : v)));
      }

      // normalize width
      const width = Math.max(...grid.map(r => r.length));
      return grid.map(r => {
        const rr = r.slice();
        while (rr.length < width) rr.push('');
        return rr;
      });
    }
    """
    grid = await page.evaluate(js, table_selector)
    if not grid:
        raise RuntimeError("Failed to extract schedule table grid")
    return grid


def _parse_header_dates(header_row: list[str]) -> list[date | None]:
    """
    Attempts to parse the header cells (Mon-Fri with date) into actual dates.
    Returns list aligned to columns; non-date columns become None.
    """
    out: list[date | None] = []
    year = datetime.now().year

    for cell in header_row:
        s = cell.strip()
        if not s:
            out.append(None)
            continue

        # common formats: "4/6(月)" "04/06" "4月6日(月)" etc.
        m = re.search(r"(?P<m>\d{1,2})\s*[\/月]\s*(?P<d>\d{1,2})\s*(?:日)?", s)
        if m:
            mm = int(m.group("m"))
            dd = int(m.group("d"))
            try:
                out.append(date(year, mm, dd))
                continue
            except ValueError:
                pass
        out.append(None)
    return out


def _parse_cell(text: str) -> tuple[str, time | None, time | None, str | None]:
    """
    Best-effort parse: [Subject, Time, Room] from a single table cell.
    Accepts layouts like:
      "内科 09:00-10:30 3F-201"
      "内科\\n09:00〜10:30\\n3F-201"
    """
    s = re.sub(r"\s+", " ", (text or "").strip())
    if not s:
        return ("", None, None, None)

    # Extract time range first.
    time_pat = re.compile(
        r"(?P<s>\d{1,2}:\d{2})\s*(?:-|～|〜|to)\s*(?P<e>\d{1,2}:\d{2})",
        re.IGNORECASE,
    )
    m = time_pat.search(s)
    start_t = end_t = None
    if m:
        start_t = datetime.strptime(m.group("s"), "%H:%M").time()
        end_t = datetime.strptime(m.group("e"), "%H:%M").time()
        s_wo_time = (s[: m.start()] + " " + s[m.end() :]).strip()
        s_wo_time = re.sub(r"\s+", " ", s_wo_time)
    else:
        s_wo_time = s

    parts = [p.strip() for p in re.split(r"[|／/]", s_wo_time) if p.strip()]
    if len(parts) >= 2:
        subject = parts[0]
        room = parts[-1]
        return (subject, start_t, end_t, room)

    # Heuristic: first token(s) subject, last token room (if looks like room).
    tokens = s_wo_time.split(" ")
    room = None
    if len(tokens) >= 2 and re.search(r"(\d{1,3}[A-Za-z-]?\d{0,3}|教室|講義室|Room|階)", tokens[-1]):
        room = tokens[-1]
        subject = " ".join(tokens[:-1]).strip()
    else:
        subject = s_wo_time
    return (subject, start_t, end_t, room)


def _infer_room_from_free_text(text: str) -> str | None:
    """
    When tooltip omits the 教室： label (or uses only inline patterns), still extract SMU-style rooms.
    Examples: 白土：D101, D402, D502（多目的演習室）, ｳｨｰﾗｰ：D402, 3F-201
    """
    if not text or not str(text).strip():
        return None
    s = unicodedata.normalize("NFKC", text)
    s = s.replace("\u3000", " ")

    # Instructor/building prefix + room (数学などがこの形式で出ることが多い)
    m = re.search(
        r"(?:ｳｨｰﾗｰ|ウィーラー|白土|ｹｲﾝ|ケイン)\s*[:：]\s*([A-Za-z0-9（）・\-]+)",
        s,
    )
    if m:
        r0 = m.group(1).strip()
        if r0 and r0 not in {":", "："}:
            return r0

    # 教研3F C301・C302 / 教研1F D101（教室ラベルなし本文のみのとき）
    m = re.search(r"(教研\d+F\s+[^\n\r]+)", s)
    if m:
        r0 = m.group(1).strip()
        if r0 and len(r0) >= 3:
            return r0

    # D### + optional Japanese suffix in parentheses
    m = re.search(r"(D\d{3}(?:（[^）\n]+）)?)", s)
    if m:
        return m.group(1).strip()

    # Room letter + digits (C302, D303)
    m = re.search(r"\b([CD]\d{3,4})\b", s)
    if m:
        return m.group(1).strip()

    m = re.search(r"\b(\d+F[-‐]?\d{2,4})\b", s, re.I)
    if m:
        return m.group(1).strip()

    return None


def _parse_tooltip_details(text: str) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Parses tooltip text and returns (subject, period, room, code) if present.
    Accepts Japanese labels like:
      - 科目名 / 講義名
      - 時限
      - 教室 / 場所
      - 科目コード / コード
    Falls back to _infer_room_from_free_text when 教室 is missing (e.g. only 白土：D101).
    """
    s = re.sub(r"[\t\r]+", "\n", (text or "")).strip()
    s = re.sub(r"\n{2,}", "\n", s)
    one_line = re.sub(r"\s+", " ", s)

    code = None
    m = re.search(r"\b(\d{8,12})\b", one_line)
    if m:
        code = m.group(1)

    period = None
    m = re.search(r"(?:時限|時限数|コマ|限)\s*[:：]?\s*(\d{1,2})", one_line)
    if m:
        period = m.group(1)

    room = None
    m = re.search(r"(?:教室|場所)\s*[:：]\s*([^\n]+)", s)
    if m:
        room = m.group(1).strip()
    else:
        m = re.search(r"(?:教室|場所)\s*[:：]?\s*([^,;]+)", one_line)
        if m:
            room = m.group(1).strip()

    if room is not None:
        room = room.strip()
        if room in {"", ":", "：", "F", "Ｆ"}:
            room = None

    subject = None
    for pat in [
        r"(?:科目名|講義名|授業名)\s*[:：]\s*([^\n]+)",
        r"(?:科目|講義)\s*[:：]\s*([^\n]+)",
    ]:
        m = re.search(pat, s)
        if m:
            subject = m.group(1).strip()
            break

    if room is None:
        room = _infer_room_from_free_text(s) or _infer_room_from_free_text(one_line)

    return subject, period, room, code


def _parse_tooltip_date(text: str) -> date | None:
    """
    Tooltip top lines often include the lecture date.
    Accepts formats like:
      - 2026-04-07
      - 2026/4/7
      - 2026年4月7日
      - 4/7 (火)   (assumes current year)
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = "\n".join(lines[:2])  # per user: date appears in the top first two lines
    head_one = re.sub(r"\s+", " ", head)

    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", head_one)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", head_one)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # If year is omitted.
    m = re.search(r"(\d{1,2})/(\d{1,2})\s*(?:\(|（)?[月火水木金土日]?(?:\)|）)?", head_one)
    if m:
        yy = datetime.now().year
        try:
            return date(yy, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None

    return None


async def _read_visible_tooltip_text(page: Page) -> str | None:
    """
    Best-effort: return the most informative currently-visible tooltip/modal text.
    Designed for hover-driven tooltips.
    """
    candidates = [
        page.locator(".tooltip.show:visible"),
        page.locator(".tooltip.in:visible"),
        page.locator(".tooltip-inner:visible"),
        page.locator(".tooltip:visible"),
        page.locator(".ui-tooltip:visible"),
        page.locator(".qtip:visible"),
        page.locator('[role="tooltip"]:visible'),
        page.locator('[role="dialog"]:visible'),
    ]

    best = ""
    for loc in candidates:
        try:
            if await loc.first.is_visible(timeout=250):
                txt = (await loc.first.inner_text(timeout=800)).strip()
                if txt and len(txt) > len(best):
                    best = txt
        except Exception:
            continue

    # Portal-specific fallback: the tooltip is a plain white box without standard classes.
    # Anchor on distinctive labels that appear inside the box.
    if not best:
        anchors = ["教室", "科目コード", "コード", "時限"]
        for a in anchors:
            try:
                hit = page.get_by_text(a, exact=False).first
                if await hit.is_visible(timeout=250):
                    box = hit.locator("xpath=ancestor-or-self::div[.//text()[contains(., '教室') or contains(., 'コード') or contains(., '時限')]][1]").first
                    if await box.is_visible(timeout=250):
                        txt = (await box.inner_text(timeout=800)).strip()
                        if txt and len(txt) > len(best):
                            best = txt
                            break
            except Exception:
                continue

    return best or None


async def _calendar_event_date_iso(page: Page, ev) -> str | None:
    """Map a FullCalendar fc-event anchor to YYYY-MM-DD via its table column."""
    handle = await ev.element_handle()
    if not handle:
        return None
    js = r"""
    (el) => {
      const td = el.closest('td');
      if (!td) return null;
      const idx = td.cellIndex;
      const dates = Array.from(document.querySelectorAll('#calendar .fc-bg td.fc-day[data-date]'))
        .map(x => x.getAttribute('data-date'))
        .filter(Boolean);
      const dayIdx = idx - 1;
      if (dayIdx >= 0 && dayIdx < dates.length) return dates[dayIdx];
      return null;
    }
    """
    try:
        return await page.evaluate(js, handle)
    except Exception:
        return None


async def _hover_tooltip_for_fc_event(page: Page, ev, *, post_hover_ms: int = 450) -> str | None:
    """Hover an fc-event and read portal tooltip (教室: line). Matches hover-mode robustness."""
    try:
        await ev.scroll_into_view_if_needed()
    except Exception:
        pass
    tooltip_text: str | None = None
    try:
        try:
            await page.mouse.move(0, 0)
        except Exception:
            pass
        target = ev.locator(".fc-content").first
        if await target.count():
            await target.hover()
        else:
            await ev.hover()
        await page.wait_for_timeout(post_hover_ms)
        tooltip_text = await _read_visible_tooltip_text(page)
        if not tooltip_text:
            await page.wait_for_timeout(max(350, post_hover_ms))
            tooltip_text = await _read_visible_tooltip_text(page)
    except Exception:
        tooltip_text = None

    if not tooltip_text:
        for attr in [
            "data-original-title",
            "data-bs-original-title",
            "data-title",
            "title",
            "aria-label",
            "data-content",
        ]:
            try:
                v = await ev.get_attribute(attr)
                if v and v.strip():
                    tooltip_text = v.strip()
                    break
            except Exception:
                continue

    if not tooltip_text:
        try:
            tid = await ev.get_attribute("aria-describedby")
            if tid:
                t = page.locator(f"#{tid}")
                if await t.first.is_visible(timeout=500) or await t.first.count():
                    tooltip_text = (await t.first.inner_text(timeout=800)).strip()
        except Exception:
            pass

    return tooltip_text


def _subject_tokens_overlap(a: str, b: str) -> bool:
    """Loose match for calendar title vs Notion subject (NFKC, casefold)."""
    x = _nfkc(a).strip().casefold().replace("\u3000", " ")
    y = _nfkc(b).strip().casefold().replace("\u3000", " ")
    if not x or not y:
        return False
    if x in y or y in x:
        return True
    n = min(8, len(x), len(y))
    return n >= 4 and x[:n] == y[:n]


async def _build_room_map_from_calendar_hover(
    page: Page,
) -> dict[tuple[str, str], list[tuple[str | None, str]]]:
    """(date_iso, period) -> [(room, title_subj_hint), ...] from each fc-event (multi if collisions)."""
    from collections import defaultdict

    events = page.locator("#calendar a.fc-day-grid-event.fc-event")
    await events.first.wait_for(state="visible", timeout=30_000)
    ev_count = await events.count()
    room_lists: dict[tuple[str, str], list[tuple[str | None, str]]] = defaultdict(list)

    for i in range(ev_count):
        if i >= max(0, ev_count - 5):
            await page.wait_for_timeout(280)

        ev = events.nth(i)
        date_str = await _calendar_event_date_iso(page, ev)

        title_loc = ev.locator(".fc-title")
        title = (
            (await title_loc.first.inner_text(timeout=1500)).strip()
            if await title_loc.count()
            else (await ev.inner_text()).strip()
        )
        title = re.sub(r"\s+", " ", title)
        m = re.match(r"^\D*(\d{1,2})\s*(.*)$", title)
        if not m:
            continue
        period = m.group(1)
        title_subj = (m.group(2) or "").strip() or title
        subj_hint = _nfkc(title_subj).strip().casefold()

        tooltip_text = await _hover_tooltip_for_fc_event(page, ev)
        t_s, t_period, room, _code = _parse_tooltip_details(tooltip_text or "")
        tip_subj = _nfkc(t_s or "").strip().casefold()
        hint = tip_subj if tip_subj else subj_hint

        final_period = _normalize_period_key(str(t_period or period or ""))
        if not final_period:
            continue

        tooltip_day = _parse_tooltip_date(tooltip_text or "")
        if tooltip_day:
            final_day = tooltip_day
        elif date_str:
            final_day = datetime.strptime(date_str, "%Y-%m-%d").date()
        else:
            continue

        slot_key = (final_day.isoformat(), final_period)
        room_lists[slot_key].append((room, hint))

    logging.info("[ROOM_ENRICH] hover map slot_keys=%s (events=%s)", len(room_lists), ev_count)
    return dict(room_lists)


def _merge_rooms_from_hover_map(
    items: list[ScheduleItem],
    room_map: dict[tuple[str, str], list[tuple[str | None, str]]],
) -> list[ScheduleItem]:
    """Fill empty rooms using hover candidates; prefer row where title hint matches subject."""
    out: list[ScheduleItem] = []
    filled = 0
    for it in items:
        key = (it.day.isoformat(), _normalize_period_key(str(it.period)))
        if _room_effectively_missing(it.room) and key in room_map:
            cands = room_map[key]
            it_sub = _nfkc(it.subject or "").strip().casefold()
            chosen: str | None = None
            for room, hint in cands:
                if _room_effectively_missing(room):
                    continue
                if hint and it_sub and _subject_tokens_overlap(it.subject or "", hint):
                    chosen = room
                    break
            if chosen is None:
                non_empty = [(r, h) for r, h in cands if not _room_effectively_missing(r)]
                if len(non_empty) == 1:
                    chosen = non_empty[0][0]
                elif non_empty:
                    for room, hint in non_empty:
                        if hint and it_sub and (it_sub in hint or hint in it_sub):
                            chosen = room
                            break
                    if chosen is None:
                        chosen = non_empty[0][0]
            if chosen is not None:
                filled += 1
                out.append(
                    ScheduleItem(
                        day=it.day,
                        period=it.period,
                        subject=it.subject,
                        start=it.start,
                        end=it.end,
                        room=chosen,
                        code=it.code,
                        raw=it.raw,
                    )
                )
                continue
        out.append(it)
    if filled:
        logging.info("[ROOM_ENRICH] filled %s empty room(s) from hover tooltips", filled)
    return out


async def _retry_hover_rooms_for_missing(page: Page, items: list[ScheduleItem]) -> list[ScheduleItem]:
    """Slow hover + 추론: 일괄 호버에서 빠진 슬롯(例: 初年次セミナー)만 다시 시도."""
    out = list(items)
    missing_idx = [i for i, it in enumerate(out) if _room_effectively_missing(it.room)]
    if not missing_idx:
        return out
    events = page.locator("#calendar a.fc-day-grid-event.fc-event")
    try:
        await events.first.wait_for(state="visible", timeout=20_000)
    except Exception:
        return out
    n = await events.count()
    for mi in missing_idx:
        it = out[mi]
        want_day = it.day.isoformat()
        want_per = _normalize_period_key(str(it.period))
        want_sub = _nfkc(it.subject or "").strip().casefold()
        for _j in range(n):
            ev = events.nth(_j)
            date_str = await _calendar_event_date_iso(page, ev)
            if date_str != want_day:
                continue
            title_loc = ev.locator(".fc-title")
            title = (
                (await title_loc.first.inner_text(timeout=1500)).strip()
                if await title_loc.count()
                else (await ev.inner_text()).strip()
            )
            title_one = re.sub(r"\s+", " ", title)
            m = re.match(r"^\D*(\d{1,2})\s*(.*)$", title_one)
            if not m:
                continue
            if _normalize_period_key(m.group(1)) != want_per:
                continue
            t_sub = _nfkc((m.group(2) or "").strip()).casefold()
            if not (
                want_sub in t_sub
                or t_sub in want_sub
                or _subject_tokens_overlap(it.subject or "", t_sub)
            ):
                continue
            await page.wait_for_timeout(350)
            tooltip_text = await _hover_tooltip_for_fc_event(page, ev, post_hover_ms=900)
            _ts, _tp, room, _c = _parse_tooltip_details(tooltip_text or "")
            if _room_effectively_missing(room) and tooltip_text:
                room = _infer_room_from_free_text(tooltip_text)
            if not _room_effectively_missing(room):
                out[mi] = ScheduleItem(
                    day=it.day,
                    period=it.period,
                    subject=it.subject,
                    start=it.start,
                    end=it.end,
                    room=room,
                    code=it.code,
                    raw=(tooltip_text or it.raw)[:1900] if tooltip_text else it.raw,
                )
                logging.info("[ROOM_RETRY] filled room %s for %s P%s", room, want_day, want_per)
                break
    return out


async def _goto_timetable(page: Page) -> None:
    for loc in [
        page.get_by_role("link", name=re.compile(r"履修時間割")),
        page.locator('a[href*="/portal/TimeTable" i]'),
        page.get_by_text(re.compile(r"履修時間割")),
    ]:
        try:
            if await loc.first.is_visible(timeout=1500):
                await loc.first.click()
                await page.wait_for_timeout(1200)
                await _dismiss_portal_warnings(page)
                return
        except Exception:
            continue


async def _goto_home(page: Page) -> None:
    for loc in [
        page.get_by_role("link", name=re.compile(r"^ホーム$")),
        page.get_by_text(re.compile(r"^ホーム$")),
        page.locator('a[href*="/portal/#" i]'),
        page.locator('a[href*="/portal/Home" i]'),
    ]:
        try:
            if await loc.first.is_visible(timeout=1500):
                await loc.first.click()
                await page.wait_for_timeout(1200)
                await _dismiss_portal_warnings(page)
                return
        except Exception:
            continue


async def parse_weekly_blocks(page: Page) -> list[ScheduleItem]:
    """
    FullCalendar-based timetable extraction:
      - Find individual `.fc-event` blocks
      - Hover each block to reveal tooltip (if needed)
      - Map date by matching event x-center to `.fc-day[data-date]` column bounding boxes
    """
    await _dismiss_portal_warnings(page)

    # The weekly "blue blocks" live on the home calendar (`#calendar`), not `/portal/TimeTable`.
    if "/TimeTable" in page.url:
        await _goto_home(page)

    await page.wait_for_selector("#calendar", timeout=30_000)

    # Headless-safe / most reliable: query FullCalendar event objects in JS memory.
    js_events = await page.evaluate(
        r"""() => {
          try {
            const $ = window.jQuery;
            if (!$) return null;
            const cal = $('#calendar');
            if (!cal || !cal.fullCalendar) return null;
            const evs = cal.fullCalendar('clientEvents') || [];
            return evs.map(e => {
              let startDate = null;
              try {
                if (e.start && typeof e.start.format === 'function') startDate = e.start.format('YYYY-MM-DD');
                else if (e.start && e.start._d) startDate = new Date(e.start._d).toISOString().slice(0, 10);
              } catch (_) {}
              const xp = (e.extendedProps && typeof e.extendedProps === 'object') ? e.extendedProps : {};
              const xpRoom = xp.room || xp.classroom || xp.place || xp.kyoushitsu || xp.Kyoushitsu || xp.busho || '';
              const xpDesc = [xp.description, xp.biko, xp.note, xp.memo, xp.detail].filter(Boolean).join('\\n');
              return {
                title: e.title || '',
                startDate,
                description: [e.description, e.biko, e.note, xpDesc].filter(Boolean).join('\\n'),
                room: (e.room || e.classroom || e.place || xpRoom || ''),
                code: e.code || e.subjectCode || xp.code || '',
                extendedProps: xp,
              };
            });
          } catch (e) {
            return null;
          }
        }""",
    )

    if isinstance(js_events, list) and js_events:
        out: list[ScheduleItem] = []
        for e in js_events:
            if not isinstance(e, dict):
                continue
            title = re.sub(r"\s+", " ", str(e.get("title") or "").strip())
            start_date = str(e.get("startDate") or "").strip()
            if not title or not start_date:
                continue

            m = re.match(r"^\D*(\d{1,2})\s*(.*)$", title)
            if not m:
                continue
            period = m.group(1)
            subject = (m.group(2) or "").strip() or title

            desc = str(e.get("description") or "").strip()
            xp = e.get("extendedProps")
            if isinstance(xp, dict):
                for k, v in xp.items():
                    if v is None or isinstance(v, (dict, list)):
                        continue
                    vs = str(v).strip()
                    if vs:
                        desc = f"{desc}\n{k}: {vs}".strip()

            t_subject, t_period, t_room, t_code = _parse_tooltip_details(desc)

            room = (str(e.get("room") or "").strip() or t_room or None)
            if room is None:
                room = _infer_room_from_free_text(subject) or _infer_room_from_free_text(title)
            code = (str(e.get("code") or "").strip() or t_code or None)

            out.append(
                ScheduleItem(
                    day=datetime.strptime(start_date, "%Y-%m-%d").date(),
                    period=str(t_period or period),
                    subject=t_subject or subject,
                    room=room,
                    code=code,
                    raw=desc or title,
                )
            )

        if out:
            # clientEvents() often omits room; same cells show 教室: in hover — fill gaps.
            try:
                room_map = await _build_room_map_from_calendar_hover(page)
                out = _merge_rooms_from_hover_map(out, room_map)
                out = await _retry_hover_rooms_for_missing(page, out)
            except Exception as e:
                logging.warning("[ROOM_ENRICH] failed, keeping clientEvents rooms only: %s", e)
            return out

    # Be strict: only the clickable event anchors (blue blocks).
    events = page.locator("#calendar a.fc-day-grid-event.fc-event")
    await events.first.wait_for(state="visible", timeout=30_000)
    logging.info("[BLOCK_SCAN] selector=%s count=%s", "#calendar a.fc-day-grid-event.fc-event", await events.count())

    day_cells = page.locator("#calendar .fc-bg td.fc-day[data-date]")
    day_count = await day_cells.count()
    if day_count == 0:
        raise RuntimeError("Could not locate calendar day columns (fc-day[data-date])")

    items: list[ScheduleItem] = []
    ev_count = await events.count()
    for i in range(ev_count):
        ev = events.nth(i)
        date_str = await _calendar_event_date_iso(page, ev)  # fallback only; tooltip date overrides if present

        # Title text usually includes leading period number, e.g. "1 医療倫理学"
        title_loc = ev.locator(".fc-title")
        title = (await title_loc.first.inner_text(timeout=1500)).strip() if await title_loc.count() else (await ev.inner_text()).strip()
        title = re.sub(r"\s+", " ", title)
        if i < 5:
            logging.info("[BLOCK_ITEM] i=%s date=%s title=%s", i, date_str, title)
        if i == 0:
            logging.info("[BLOCK_TITLE_REPR] %r", title)
        period = None
        subject = title
        # Some builds use NBSP or include invisible prefix characters; be lenient.
        # Use \\D so we don't accidentally consume fullwidth/unicode digits.
        m = re.match(r"^\D*(\d{1,2})\s*(.*)$", title)
        if m:
            period = m.group(1)
            subject = (m.group(2) or "").strip() or title

        # Preferred: hover and read tooltip (user-confirmed it contains date + room).
        tooltip_text = None
        try:
            await ev.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            # Move mouse away to avoid reusing an old tooltip.
            try:
                await page.mouse.move(0, 0)
            except Exception:
                pass
            await ev.hover()
            await page.wait_for_timeout(150)
            tooltip_text = await _read_visible_tooltip_text(page)
        except Exception:
            tooltip_text = None

        t_subject, t_period, room, code = _parse_tooltip_details(tooltip_text or "")
        final_subject = t_subject or subject
        final_period = t_period or period or ""

        # Skip if we still can't determine period (rare)
        if not final_period:
            logging.info("[BLOCK_SKIP] i=%s reason=no_period title=%s tooltip=%s", i, title, (tooltip_text or "")[:80])
            continue

        # Date override from tooltip (per user: first two lines).
        tooltip_day = _parse_tooltip_date(tooltip_text or "")
        if tooltip_day:
            final_day = tooltip_day
        else:
            if not date_str:
                continue
            final_day = datetime.strptime(date_str, "%Y-%m-%d").date()

        items.append(
            ScheduleItem(
                day=final_day,
                period=str(final_period),
                subject=final_subject,
                room=room,
                code=code,
                raw=tooltip_text or title,
            )
        )

    return items


async def parse_weekly_schedule(page: Page, *, details_mode: Literal["js", "hover"]) -> list[ScheduleItem]:
    await _dismiss_portal_warnings(page)

    # Strategy 1: individual blocks via FullCalendar in-memory events (best for correct dates).
    try:
        items = await parse_weekly_blocks(page) if details_mode == "js" else []
        if items:
            # Minimal validation
            logging.info("[PARSE_SUMMARY] extracted_items=%s non_empty_cells=%s", len(items), len(items))
            return items
    except Exception as e:
        logging.warning("JS-event parsing failed; falling back. err=%s", e)

    # Strategy 2: hover tooltip (for room/code) — user confirmed it contains date (top 1-2 lines) and room at '教室:'.
    if details_mode == "hover":
        items = await _parse_weekly_blocks_hover(page)
        logging.info("[PARSE_SUMMARY] extracted_items=%s non_empty_cells=%s", len(items), len(items))
        return items

    async def find_weekly_heading() -> Any:
        # Portal labels vary; accept broader timetable/schedule headings.
        for key in ["週間スケジュール", "週", "時間割", "スケジュール", "Schedule"]:
            h = page.get_by_text(key, exact=False).first
            try:
                if await h.is_visible(timeout=1200):
                    return h
            except Exception:
                continue
        return page.get_by_text("週間スケジュール", exact=False).first

    heading = await find_weekly_heading()
    try:
        await heading.wait_for(state="visible", timeout=8_000)
    except Exception:
        # Try navigating to timetable page, then retry.
        await _goto_timetable(page)
        heading = await find_weekly_heading()
        try:
            await heading.wait_for(state="visible", timeout=12_000)
        except Exception:
            # fallback: sometimes the widget is in an iframe
            frame = None
            for f in page.frames:
                try:
                    if await f.get_by_text("週間スケジュール", exact=False).first.is_visible(timeout=1200):
                        frame = f
                        break
                    if await f.get_by_text("時間割", exact=False).first.is_visible(timeout=1200):
                        frame = f
                        break
                except Exception:
                    continue
            if frame is None:
                raise RuntimeError('Could not locate weekly schedule/timetable section after login')

            # Try to find a table inside the matching frame.
            table = frame.locator("table").first
            await table.wait_for(state="visible", timeout=20_000)
            # Use a unique selector by assigning an attribute.
            await page.evaluate(
                """(el) => { el.setAttribute('data-smu-weekly', '1'); }""",
                await table.element_handle(),
            )
            table_sel = "table[data-smu-weekly='1']"
            grid = await _extract_table_grid(page, table_sel)
            return _grid_to_items(grid)

    # Find the nearest table to the heading.
    # Prefer a table within the same "card"/section; fallback to the first following table.
    section = heading.locator("xpath=ancestor-or-self::*[self::section or self::div][.//table][1]").first
    table = section.locator("table").first
    if not await table.is_visible(timeout=2500):
        table = heading.locator("xpath=following::table[1]")
    await table.wait_for(state="visible", timeout=30_000)

    await page.evaluate(
        """(el) => { el.setAttribute('data-smu-weekly', '1'); }""",
        await table.element_handle(),
    )
    table_sel = "table[data-smu-weekly='1']"
    grid = await _extract_table_grid(page, table_sel)
    return _grid_to_items(grid)


async def _parse_weekly_blocks_hover(page: Page) -> list[ScheduleItem]:
    """
    Hover-based extraction (more accurate for room/code when tooltip is reliable).
    This reads the tooltip content and parses:
      - date from tooltip top lines
      - room from '教室:' label line
      - code if present
    """
    await _dismiss_portal_warnings(page)
    if "/TimeTable" in page.url:
        await _goto_home(page)

    await page.wait_for_selector("#calendar", timeout=30_000)
    events = page.locator("#calendar a.fc-day-grid-event.fc-event")
    await events.first.wait_for(state="visible", timeout=30_000)
    logging.info("[BLOCK_SCAN] selector=%s count=%s", "#calendar a.fc-day-grid-event.fc-event", await events.count())

    items: list[ScheduleItem] = []
    ev_count = await events.count()
    for i in range(ev_count):
        ev = events.nth(i)

        # Title
        title_loc = ev.locator(".fc-title")
        title = (await title_loc.first.inner_text(timeout=1500)).strip() if await title_loc.count() else (await ev.inner_text()).strip()
        title = re.sub(r"\s+", " ", title)

        m = re.match(r"^\D*(\d{1,2})\s*(.*)$", title)
        if not m:
            continue
        period = m.group(1)
        subject = (m.group(2) or "").strip() or title

        tooltip_text = None
        try:
            await ev.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            try:
                await page.mouse.move(0, 0)
            except Exception:
                pass
            # Some tooltips bind to inner content, not the anchor itself.
            target = ev.locator(".fc-content").first
            if await target.count():
                await target.hover()
            else:
                await ev.hover()

            # Tooltips are async; wait a bit longer and retry.
            await page.wait_for_timeout(450)
            tooltip_text = await _read_visible_tooltip_text(page)
            if not tooltip_text:
                await page.wait_for_timeout(450)
                tooltip_text = await _read_visible_tooltip_text(page)
        except Exception:
            tooltip_text = None

        # If tooltip is implemented via attributes (native/title-based or bootstrap data attrs),
        # it may not appear as a visible DOM node. Read common attributes after hover.
        if not tooltip_text:
            for attr in [
                "data-original-title",
                "data-bs-original-title",
                "data-title",
                "title",
                "aria-label",
                "data-content",
            ]:
                try:
                    v = await ev.get_attribute(attr)
                    if v and v.strip():
                        tooltip_text = v.strip()
                        break
                except Exception:
                    continue

        # aria-describedby -> referenced tooltip element id
        if not tooltip_text:
            try:
                tid = await ev.get_attribute("aria-describedby")
                if tid:
                    t = page.locator(f"#{tid}")
                    if await t.first.is_visible(timeout=500) or await t.first.count():
                        tooltip_text = (await t.first.inner_text(timeout=800)).strip()
            except Exception:
                pass

        # Parse tooltip for override fields
        t_subject, t_period, room, code = _parse_tooltip_details(tooltip_text or "")
        final_subject = t_subject or subject
        final_period = t_period or period

        if i < 2:
            logging.info("[HOVER_TOOLTIP] i=%s tooltip_present=%s preview=%r", i, bool(tooltip_text), (tooltip_text or "")[:120])
            if not tooltip_text:
                try:
                    html = await page.evaluate("(el) => el.outerHTML", await ev.element_handle())
                    logging.info("[HOVER_OUTERHTML] i=%s %r", i, (html or "")[:220])
                except Exception:
                    pass
                if i == 0:
                    try:
                        os.makedirs("artifacts", exist_ok=True)
                        await page.screenshot(path=os.path.join("artifacts", "hover_debug.png"), full_page=True)
                        logging.info("[HOVER_DEBUG] saved artifacts/hover_debug.png")
                    except Exception:
                        pass

        tooltip_day = _parse_tooltip_date(tooltip_text or "")
        if not tooltip_day:
            # Fallback: use DOM-mapped date if tooltip date is unavailable.
            try:
                date_iso = await page.evaluate(
                    r"""(el) => {
                      const td = el.closest('td');
                      if (!td) return null;
                      const idx = td.cellIndex;
                      const dates = Array.from(document.querySelectorAll('#calendar .fc-bg td.fc-day[data-date]'))
                        .map(x => x.getAttribute('data-date'))
                        .filter(Boolean);
                      const dayIdx = idx - 1;
                      return (dayIdx >= 0 && dayIdx < dates.length) ? dates[dayIdx] : null;
                    }""",
                    await ev.element_handle(),
                )
                if date_iso:
                    tooltip_day = datetime.strptime(date_iso, "%Y-%m-%d").date()
            except Exception:
                tooltip_day = None
        if not tooltip_day:
            continue

        items.append(
            ScheduleItem(
                day=tooltip_day,
                period=str(final_period),
                subject=final_subject,
                room=room,
                code=code,
                raw=tooltip_text or title,
            )
        )

    return items


def _grid_to_items(grid: list[list[str]]) -> list[ScheduleItem]:
    """
    Converts expanded grid into schedule items.
    Assumptions (robust to extra header rows):
    - A header row exists that contains weekday/date labels (Mon-Fri)
    - A first column exists for period labels (1..5)
    """
    # Pick a header row: the first row with >=3 date-like cells.
    header_idx = 0
    best_score = -1
    for i, row in enumerate(grid[:6]):  # header usually near top
        dates = _parse_header_dates(row)
        score = sum(1 for d in dates if d is not None)
        if score > best_score:
            best_score = score
            header_idx = i
    header = grid[header_idx]
    col_dates = _parse_header_dates(header)

    # Determine day columns (Mon-Fri) as those with dates parsed.
    day_cols = [idx for idx, d in enumerate(col_dates) if d is not None]
    if not day_cols:
        raise RuntimeError("Could not parse any dates from the weekly schedule header")

    items: list[ScheduleItem] = []
    non_empty_cells = 0

    # Data rows come after header; find rows that look like periods.
    for row in grid[header_idx + 1 :]:
        if not row:
            continue
        period_cell = (row[0] or "").strip()
        m = re.match(r"^(\d{1,2})", period_cell)
        if not m:
            continue
        period = m.group(1)

        for c in day_cols:
            d = col_dates[c]
            if d is None:
                continue
            cell_text = (row[c] if c < len(row) else "").strip()
            if not cell_text:
                continue
            non_empty_cells += 1

            subject, start_t, end_t, room = _parse_cell(cell_text)
            if not subject:
                continue
            items.append(
                ScheduleItem(
                    day=d,
                    period=period,
                    subject=subject,
                    start=start_t,
                    end=end_t,
                    room=room,
                    raw=cell_text,
                )
            )

    # Lightweight validation to catch missing/shifted indices (especially with merged cells).
    # With rowspan-expanded grids, many lectures will appear multiple times (same text across periods),
    # but a large mismatch often indicates parsing issues.
    if non_empty_cells and len(items) < max(1, non_empty_cells // 3):
        logging.warning(
            "[VALIDATION_WARNING] extracted_items=%s non_empty_cells=%s (possible table parse mismatch)",
            len(items),
            non_empty_cells,
        )
    logging.info(
        "[PARSE_SUMMARY] extracted_items=%s non_empty_cells=%s (pre-postprocess)",
        len(items),
        non_empty_cells,
    )

    return items


async def run(
    *,
    headless: bool,
    slow_mo_ms: int,
    log_level: str,
    output: Literal["json", "pretty"],
    storage_state: str | None,
    save_storage_state: str | None,
    details_mode: Literal["js", "hover"],
) -> int:
    _setup_logging(log_level)
    load_dotenv()

    user = _env("PORTAL_USER") or ""
    pw = _env("PORTAL_PASS") or ""

    logging.info("Launching browser (headless=%s)...", headless)
    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        context_kwargs: dict[str, Any] = {"locale": "ja-JP"}
        if storage_state and os.path.exists(storage_state):
            context_kwargs["storage_state"] = storage_state
            logging.info("Using storage_state from %s", storage_state)
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        try:
            logging.info("Logging into SMU portal...")
            # If storage_state already contains a valid session, this should be a no-op
            # (portal will redirect straight to dashboard).
            await login(page, user, pw)
            logging.info("Parsing weekly schedule...")
            items_parsed = await parse_weekly_schedule(page, details_mode=details_mode)
            items = apply_room_overrides(items_parsed)
            items = apply_period_slot_times(items)
            items = apply_user_schedule_postprocess(items)
            items = apply_sibling_room_fallback(items)
            items = apply_optional_room_hints(items)
            conflicts = collect_room_conflicts(items_parsed, items)
            write_room_conflicts_artifact(conflicts)
            logging.info("[PIPELINE_ITEMS] final_rows=%s (after user postprocess)", len(items))

            if save_storage_state:
                os.makedirs(os.path.dirname(save_storage_state) or ".", exist_ok=True)
                await context.storage_state(path=save_storage_state)
                logging.info("Saved storage_state to %s", save_storage_state)
        except Exception:
            # Debug artifacts help when the portal markup differs from heuristics.
            try:
                os.makedirs("artifacts", exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                await page.screenshot(path=os.path.join("artifacts", f"failure_{ts}.png"), full_page=True)
                with open(os.path.join("artifacts", f"failure_{ts}.txt"), "w", encoding="utf-8") as f:
                    try:
                        title = await page.title()
                    except Exception:
                        title = ""
                    f.write(f"url={page.url}\n")
                    f.write(f"title={title}\n")
                html = await page.content()
                with open(os.path.join("artifacts", f"failure_{ts}.html"), "w", encoding="utf-8") as f:
                    f.write(html)
                logging.error("Saved debug artifacts to ./artifacts (timestamp=%s)", ts)
            except Exception as art_err:
                logging.error("Failed saving debug artifacts: %s", art_err)
            raise
        finally:
            await context.close()
            await browser.close()

    if output == "json":
        print(
            json.dumps(
                [
                    {
                        "date": it.day.isoformat(),
                        "period": it.period,
                        "subject": it.subject,
                        "start": it.start.strftime("%H:%M") if it.start else None,
                        "end": it.end.strftime("%H:%M") if it.end else None,
                        "room": it.room,
                        "code": it.code,
                        "raw": it.raw,
                    }
                    for it in items
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for it in items:
            t = ""
            if it.start and it.end:
                t = f"{it.start.strftime('%H:%M')}-{it.end.strftime('%H:%M')} "
            r = f" @ {it.room}" if it.room else ""
            print(f"{it.day.isoformat()} P{it.period} {t}{it.subject}{r}")

    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="SMU Portal weekly schedule scraper")
    ap.add_argument("--headless", action="store_true", help="Run browser headless")
    ap.add_argument("--slowmo", type=int, default=0, help="Slow motion ms for debugging")
    ap.add_argument("--log-level", default="INFO", help="DEBUG|INFO|WARNING|ERROR")
    ap.add_argument("--output", choices=["json", "pretty"], default="pretty")
    ap.add_argument(
        "--details-mode",
        choices=["js", "hover"],
        default="js",
        help="Parsing mode: js=FullCalendar clientEvents (best dates), hover=tooltip hover (best room/code; run headful recommended)",
    )
    ap.add_argument(
        "--storage-state",
        default=None,
        help="Path to Playwright storage_state.json to reuse existing auth/session",
    )
    ap.add_argument(
        "--save-storage-state",
        default=None,
        help="Path to save Playwright storage_state.json after a successful run",
    )
    args = ap.parse_args()

    raise SystemExit(
        asyncio.run(
            run(
                headless=args.headless,
                slow_mo_ms=args.slowmo,
                log_level=args.log_level,
                output=args.output,
                storage_state=args.storage_state,
                save_storage_state=args.save_storage_state,
                details_mode=args.details_mode,
            )
        )
    )


if __name__ == "__main__":
    main()

