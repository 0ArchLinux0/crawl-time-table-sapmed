"""
.git / 커밋에 넣지 말 것 — .env의 TELEGRAM_* 로 테스트 메시지 1통 전송.

사용:
  .\\.venv\\Scripts\\python.exe scripts\\test_telegram.py

필요한 .env 값:
  TELEGRAM_BOT_TOKEN  … @BotFather 가 준 토큰 (예: 123456:ABC-DEF...)
  TELEGRAM_CHAT_ID    … 본인 채팅 ID (숫자 문자열, 예: 123456789)
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.config import REPO_ROOT, load_env

ENV = REPO_ROOT / ".env"


def main() -> int:
    if not ENV.is_file():
        print(f"Missing {ENV} — copy .env.example to .env and fill TELEGRAM_* .", file=sys.stderr)
        return 2

    load_env(ENV)

    import os

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

    if not token or not chat:
        print(
            "Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (see .env.example).",
            file=sys.stderr,
        )
        return 1

    text = (
        "✅ Sapmed 포털 크롤러 — 텔레그램 연결 테스트\n"
        "이 메시지가 보이면 파이프라인 실패/경고 알림도 같은 채널로 갑니다."
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat, "text": text}, timeout=25)
    except OSError as e:
        print(f"Network error: {e}", file=sys.stderr)
        return 1

    if not r.ok:
        try:
            body = r.json()
        except Exception:
            body = r.text[:500]
        print(f"Telegram API error HTTP {r.status_code}: {body}", file=sys.stderr)
        print(
            "흔한 원인: CHAT_ID 오류, 봇에게 아직 /start 안 함, 토큰 복사 실수.",
            file=sys.stderr,
        )
        return 1

    print("Telegram: sendMessage OK — 기기에서 메시지를 확인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
