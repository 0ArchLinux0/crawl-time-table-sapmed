"""Shared helpers for Telegram bot AI commands (Gemini / DeepSeek)."""
from __future__ import annotations

import re
from typing import Literal

Mode = Literal["fast", "think", "pro"]


def split_telegram_chunks(text: str, limit: int = 3900) -> list[str]:
    """텔레그램 메시지 상한(4096) 아래로 분할."""
    t = (text or "").strip()
    if not t:
        return ["(빈 응답)"]
    if len(t) <= limit:
        return [t]
    parts: list[str] = []
    paras = re.split(r"(\n{2,})", t)
    buf = ""
    for chunk in paras:
        if len(buf) + len(chunk) <= limit:
            buf += chunk
        else:
            if buf:
                parts.append(buf.strip())
            if len(chunk) <= limit:
                buf = chunk
            else:
                for i in range(0, len(chunk), limit):
                    parts.append(chunk[i : i + limit])
                buf = ""
    if buf.strip():
        parts.append(buf.strip())
    return [p for p in parts if p]


def mode_from_token(token: str) -> Mode | None:
    t = token.lower()
    if t in ("fast", "빠름", "quick", "f"):
        return "fast"
    if t in ("think", "thinking", "사고", "심층", "t"):
        return "think"
    if t in ("pro", "프로", "p"):
        return "pro"
    return None
