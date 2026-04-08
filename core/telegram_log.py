"""
Central Telegram / Slack notifications for workers and pipeline.
Configure: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, optional SLACK_WEBHOOK_URL
"""
from __future__ import annotations

import os
import socket
from datetime import datetime

import requests

# Appended so recipients understand post-processing rules
_ALERT_POLICY_FOOTER = (
    "\n---\n"
    "【時間表ルール】履修しない選択: 言語学・人類学は反映しません。哲学・社会学は通常どおり。\n"
    "【教室固定】英語1=前期D402(6/9はC302)/後期白土D101 · 露(ロシア語)=D502(多目的演習室) · "
    "英会話前期(シュガーマン)=D303 · 英会話後期=ケインD401(6/10はC301)。"
)


def alerts_configured() -> bool:
    tg = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() and (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    slack = (os.getenv("SLACK_WEBHOOK_URL") or "").strip()
    return bool(tg or slack)


def format_room_conflicts_for_notify(conflicts: list[dict]) -> str:
    lines = [
        "스크랩된 강의실과 로컬 수동 규칙으로 덮어쓴 값이 다릅니다.",
        "날짜·교시는 포털 스크랩을 신뢰하고, Notion에는 규칙 반영본이 들어갔을 수 있습니다. 직접 확인해 주세요.",
        "",
    ]
    for c in conflicts[:25]:
        if not isinstance(c, dict):
            continue
        date_s = c.get("date", "")
        per = c.get("period", "")
        sr = c.get("scraped_room", "")
        fr = c.get("final_room", "")
        ss = (c.get("scraped_subject") or "")[:50]
        fs = (c.get("final_subject") or "")[:50]
        lines.append(f"· {date_s} P{per}: scrap「{sr}」→ 적용「{fr}」")
        if ss or fs:
            lines.append(f"  ({ss} → {fs})")
    if len(conflicts) > 25:
        lines.append(f"... 외 {len(conflicts) - 25}건 (artifacts/room_conflicts.json)")
    return "\n".join(lines)


def _trim(s: str, max_len: int = 3500) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 25] + "\n…(truncated)"


def send_pipeline_alert(*, kind: str, message: str, log_path: str | None = None) -> bool:
    return _send_channels(
        title_tag="[Sapmed pipeline ALERT]",
        kind=kind,
        message=message,
        log_path=log_path,
        extra_footer=_ALERT_POLICY_FOOTER,
    )


def send_room_conflict_notice(*, message: str, log_path: str | None = None) -> bool:
    extra = (
        "\n---\n"
        "[확인] 예외일·고정 교실은 예전 시간표 기준이며, 스크랩 날짜가 더 정확할 수 있습니다. "
        "불일치 시 포털·노션을 대조하세요."
    )
    return _send_channels(
        title_tag="[Sapmed WARNING — 강의실 불일치]",
        kind="room_scrape_conflict",
        message=message,
        log_path=log_path,
        extra_footer=extra + _ALERT_POLICY_FOOTER,
    )


def send_bot_info(*, title: str, message: str) -> bool:
    """Short status line from the orchestrator (no long policy footer)."""
    return _send_channels(
        title_tag=f"[Sapmed Bot] {title}",
        kind="bot",
        message=message,
        log_path=None,
        extra_footer="",
    )


def send_worker_failure(*, worker_name: str, message: str, log_path: str | None = None) -> bool:
    return _send_channels(
        title_tag=f"[Worker FAILED] {worker_name}",
        kind="worker_error",
        message=message,
        log_path=log_path,
        extra_footer=_ALERT_POLICY_FOOTER,
    )


def _send_channels(
    *,
    title_tag: str,
    kind: str,
    message: str,
    log_path: str | None,
    extra_footer: str,
) -> bool:
    host = socket.gethostname() or "unknown-host"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"{title_tag}\n{ts}\nhost={host}\nkind={kind}\n{message}"
    if log_path:
        text += f"\nlog={log_path}"
    text += extra_footer
    text = _trim(text)

    ok_any = False
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if token and chat:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text},
                timeout=25,
            )
            if r.ok:
                ok_any = True
        except OSError:
            pass

    hook = (os.getenv("SLACK_WEBHOOK_URL") or "").strip()
    if hook:
        try:
            r = requests.post(hook, json={"text": text}, timeout=25)
            if r.status_code < 400:
                ok_any = True
        except OSError:
            pass

    return ok_any
