"""현재 GEMINI_API_KEY 로 호출 가능한 Gemini 모델 목록 (generateContent).

404가 나면 .env 의 모델명이 틀렸거나 키/프로젝트 스코프 문제일 수 있음.
목록에 없는 이름은 GenerativeModel 에 넣지 마세요.

실행 (저장소 루트에서 — 시스템 python 말고 가상환경):
  .\\.venv\\Scripts\\python.exe scripts\\check_models.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import google.generativeai as genai
except ModuleNotFoundError:
    vpy = ROOT / ".venv" / "Scripts" / "python.exe"
    alt = ROOT / ".venv" / "bin" / "python"
    hint = (
        "google-generativeai 가 이 Python 에 없습니다.\n"
        f"  지금 사용 중: {sys.executable}\n\n"
        "가상환경에서 설치 후 같은 인터프리터로 다시 실행하세요:\n"
    )
    if vpy.is_file():
        hint += f"  {vpy} -m pip install -r requirements.txt\n" f"  {vpy} scripts\\check_models.py\n"
    elif alt.is_file():
        hint += f"  {alt} -m pip install -r requirements.txt\n" f"  {alt} scripts/check_models.py\n"
    else:
        hint += "  python -m venv .venv\n" "  .\\.venv\\Scripts\\pip.exe install -r requirements.txt\n"
    print(hint, file=sys.stderr)
    raise SystemExit(3)

from core.config import load_env


def main() -> int:
    load_env()
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        print("GEMINI_API_KEY 가 .env 에 없습니다.", file=sys.stderr)
        return 1

    genai.configure(api_key=key)
    print("--- generateContent 지원 모델 ---")
    print("(봇/.env 예: gemini-2.0-flash — 키마다 목록이 다름, 위 목록에 있는 것만 사용)\n")
    try:
        found = False
        for m in genai.list_models():
            methods = getattr(m, "supported_generation_methods", None) or []
            if "generateContent" in methods:
                found = True
                name = getattr(m, "name", "") or ""
                prefix = "models/"
                short = name[len(prefix) :] if name.startswith(prefix) else name
                print(f"  list: {name}")
                print(f"       → GenerativeModel({short!r})")
        if not found:
            print("  (목록이 비었습니다. 키가 AI Studio에서 발급되었는지 확인하세요.)")
    except Exception as e:
        print(f"에러: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
