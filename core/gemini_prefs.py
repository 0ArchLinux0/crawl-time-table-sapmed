"""Per-Telegram-chat defaults: AI provider (gemini / deepseek) and mode (fast / think / pro)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

Mode = Literal["fast", "think", "pro"]
Provider = Literal["gemini", "deepseek"]

_PREFS_PATH = Path(__file__).resolve().parent.parent / "artifacts" / "gemini_chat_prefs.json"


def _normalize_entry(v: object) -> dict[str, str]:
    if isinstance(v, str):
        mode = v if v in ("think", "pro") else "fast"
        return {"mode": mode, "provider": "gemini"}
    if isinstance(v, dict):
        mode = v.get("mode", "fast")
        if mode not in ("think", "pro"):
            mode = "fast"
        prov = v.get("provider", "gemini")
        if prov not in ("gemini", "deepseek"):
            prov = "gemini"
        return {"mode": mode, "provider": prov}
    return {"mode": "fast", "provider": "gemini"}


def _load_raw() -> dict:
    try:
        data = json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_raw(data: dict) -> None:
    _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PREFS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _entry(chat_id: int) -> dict[str, str]:
    key = str(int(chat_id))
    raw = _load_raw()
    return _normalize_entry(raw.get(key, {}))


def get_default_mode(chat_id: int) -> Mode:
    e = _entry(chat_id)
    m = e["mode"]
    return m  # type: ignore[return-value]


def set_default_mode(chat_id: int, mode: Mode) -> None:
    raw = _load_raw()
    key = str(int(chat_id))
    cur = _normalize_entry(raw.get(key, {}))
    cur["mode"] = mode
    raw[key] = cur
    _save_raw(raw)


def get_default_provider(chat_id: int) -> Provider:
    e = _entry(chat_id)
    p = e["provider"]
    return p  # type: ignore[return-value]


def set_default_provider(chat_id: int, provider: Provider) -> None:
    raw = _load_raw()
    key = str(int(chat_id))
    cur = _normalize_entry(raw.get(key, {}))
    cur["provider"] = provider
    raw[key] = cur
    _save_raw(raw)
