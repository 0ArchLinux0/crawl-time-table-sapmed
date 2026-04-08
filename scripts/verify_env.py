"""Check required keys exist in repo-root .env without printing values."""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

REQUIRED = ("PORTAL_USER", "PORTAL_PASS", "NOTION_TOKEN", "NOTION_DB_ID")


def main() -> int:
    if not ENV.is_file():
        print(f"Missing {ENV} — copy .env.example to .env and fill in.", file=sys.stderr)
        return 2
    vals = dotenv_values(ENV)
    missing = [k for k in REQUIRED if not (vals.get(k) and str(vals.get(k)).strip())]
    if missing:
        print("Missing or empty in .env:", ", ".join(missing), file=sys.stderr)
        return 1
    u = str(vals.get("PORTAL_USER") or "")
    if "@" not in u and not (vals.get("PORTAL_USER_DOMAIN") and str(vals.get("PORTAL_USER_DOMAIN")).strip()):
        print(
            "PORTAL_USER has no '@' but PORTAL_USER_DOMAIN is empty — "
            "set full email in PORTAL_USER or set PORTAL_USER_DOMAIN.",
            file=sys.stderr,
        )
        return 1

    tok = vals.get("TELEGRAM_BOT_TOKEN")
    cid = vals.get("TELEGRAM_CHAT_ID")
    has_tok = bool(tok and str(tok).strip())
    has_cid = bool(cid and str(cid).strip())
    if has_tok ^ has_cid:
        print(
            "WARN: Telegram — TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set "
            "(or both empty). See .env.example.",
            file=sys.stderr,
        )
    elif has_tok and has_cid:
        print("Telegram: 알림용 토큰+CHAT_ID 있음 → python scripts/test_telegram.py 로 전송 테스트 권장.")

    print(".env: required keys present (values not shown).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
