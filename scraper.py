from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time
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


def apply_room_overrides(items: list[ScheduleItem]) -> list[ScheduleItem]:
    """
    Apply known room exception rules provided by user.
    - 英語1: 前期 기본 ｳｨｰﾗｰ：D402, 단 6/9만 C302. 後기 白土：D101
    - 英会話: 전기 ﾘｰﾑｽﾄ：D502（多目的演習室）, 후기 ｹｲﾝ：D401, 단 6/10만 C301
    """
    out: list[ScheduleItem] = []
    for it in items:
        subj = (it.subject or "").strip()
        term = _infer_term(it.day)
        room = it.room

        # English 1 (医学英語1 / 英語1)
        if re.search(r"(医学英語\s*[１1]|英語\s*[１1])", subj):
            if term == "前期":
                # Default
                room = "ｳｨｰﾗｰ：D402"
                if it.day.month == 6 and it.day.day == 9:
                    room = "ｳｨｰﾗｰ：C302"
            else:
                room = "白土：D101"

        # English conversation
        if "英会話" in subj:
            if term == "前期":
                room = "ﾘｰﾑｽﾄ：D502（多目的演習室）"
            else:
                room = "ｹｲﾝ：D401"
            if it.day.month == 6 and it.day.day == 10:
                room = "ｹｲﾝ：C301"

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
            email = page.locator('input[type="email"], input[name="loginfmt"], input[id="i0116"]')
            await email.wait_for(state="visible", timeout=30_000)
            await email.fill(identifier)
            # Next
            next_btn = page.get_by_role("button", name=re.compile(r"^(次へ|Next)$"))
            if not await next_btn.first.is_visible(timeout=1500):
                next_btn = page.locator('input[type="submit"], button[type="submit"], #idSIButton9')
            await next_btn.first.click()

            # Success heuristic: password field becomes visible shortly after Next.
            pw = page.locator('input[type="password"], input[name="passwd"], #i0118')
            try:
                await pw.wait_for(state="visible", timeout=4000)
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

        ok = False
        for ident in identifiers:
            try:
                ok = await try_identifier(ident)
            except Exception:
                ok = False
            if ok:
                break
        if not ok:
            raise RuntimeError(
                "Microsoft sign-in rejected the provided username/UPN. "
                "Set PORTAL_USER to the full UPN/email, or set PORTAL_USER_DOMAIN to append a domain."
            )

        # Password step
        pw = page.locator('input[type="password"], input[name="passwd"], #i0118')
        await pw.wait_for(state="visible", timeout=30_000)
        await pw.fill(password)

        signin = page.get_by_role("button", name=re.compile(r"^(サインイン|Sign in)$"))
        if not await signin.first.is_visible(timeout=1500):
            signin = page.locator('input[type="submit"], button[type="submit"], #idSIButton9')
        await signin.first.click()

        # "Stay signed in?" prompt (企業/学校アカウント)
        try:
            no_btn = page.get_by_role("button", name=re.compile(r"^(いいえ|No)$", re.I))
            yes_btn = page.get_by_role("button", name=re.compile(r"^(はい|Yes)$", re.I))
            if await no_btn.first.is_visible(timeout=5000):
                await no_btn.first.click()
            elif await yes_btn.first.is_visible(timeout=1200):
                await yes_btn.first.click()
        except Exception:
            pass

        # Wait until redirected back to the portal; if MFA/interaction is required, fail fast with a clear message.
        async def wait_portal() -> None:
            await page.wait_for_url(re.compile(r"https://cp-portal\.sapmed\.ac\.jp/.*"), timeout=90_000)

        async def wait_mfa() -> None:
            # Broad keyword net for common AAD step-up screens.
            await page.wait_for_selector(
                "text=追加の情報, text=承認, text=Authenticator, text=確認コード, text=コードを入力, text=MFA, text=Verify",
                timeout=30_000,
            )
            raise RuntimeError("Microsoft sign-in requires additional interactive verification (MFA/step-up).")

        done, pending = await asyncio.wait(
            {asyncio.create_task(wait_portal()), asyncio.create_task(wait_mfa())},
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


def _parse_tooltip_details(text: str) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Parses tooltip text and returns (subject, period, room, code) if present.
    Accepts Japanese labels like:
      - 科目名 / 講義名
      - 時限
      - 教室 / 場所
      - 科目コード / コード
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
    # Room tends to be on the last line like: "教室：教研3F C301・C302"
    m = re.search(r"(?:教室|場所)\s*[:：]\s*([^\n]+)", s)
    if m:
        room = m.group(1).strip()
    else:
        m = re.search(r"(?:教室|場所)\s*[:：]?\s*([^,;]+)", one_line)
        if m:
            room = m.group(1).strip()

    # Normalize empty-ish room strings (e.g. tooltip has '教室：' with no value)
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
              return {
                title: e.title || '',
                startDate,
                description: e.description || e.biko || e.note || '',
                room: e.room || e.classroom || e.place || '',
                code: e.code || e.subjectCode || '',
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
            t_subject, t_period, t_room, t_code = _parse_tooltip_details(desc)

            room = (str(e.get("room") or "").strip() or t_room or None)
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
            return out

    # Be strict: only the clickable event anchors (blue blocks).
    events = page.locator("#calendar a.fc-day-grid-event.fc-event")
    await events.first.wait_for(state="visible", timeout=30_000)
    logging.info("[BLOCK_SCAN] selector=%s count=%s", "#calendar a.fc-day-grid-event.fc-event", await events.count())

    day_cells = page.locator("#calendar .fc-bg td.fc-day[data-date]")
    day_count = await day_cells.count()
    if day_count == 0:
        raise RuntimeError("Could not locate calendar day columns (fc-day[data-date])")

    # DOM-based date mapping is more reliable than bounding boxes (works in headless).
    async def event_date_iso(ev) -> str | None:
        handle = await ev.element_handle()
        if not handle:
            return None
        js = r"""
        (el) => {
          const td = el.closest('td');
          if (!td) return null;
          const idx = td.cellIndex; // includes leading blank/time label column (usually 0)
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

    items: list[ScheduleItem] = []
    ev_count = await events.count()
    for i in range(ev_count):
        ev = events.nth(i)
        date_str = await event_date_iso(ev)  # fallback only; tooltip date overrides if present

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
    logging.info("[PARSE_SUMMARY] extracted_items=%s non_empty_cells=%s", len(items), non_empty_cells)

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
            items = await parse_weekly_schedule(page, details_mode=details_mode)
            items = apply_room_overrides(items)
            items = apply_period_slot_times(items)

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

