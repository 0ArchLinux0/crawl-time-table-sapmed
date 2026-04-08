"""DeepSeek API (OpenAI-compatible) — 질문/답변 for Telegram bot."""
from __future__ import annotations

import logging
import os

import requests

from core.telegram_ai_util import Mode

log = logging.getLogger("deepseek_reply")

_DEFAULT_MODELS: dict[Mode, str] = {
    "fast": "deepseek-chat",
    "think": "deepseek-reasoner",
    "pro": "deepseek-chat",
}


def _model_name(mode: Mode) -> str:
    env = {
        "fast": "DEEPSEEK_MODEL_FAST",
        "think": "DEEPSEEK_MODEL_THINK",
        "pro": "DEEPSEEK_MODEL_PRO",
    }[mode]
    return (os.getenv(env) or "").strip() or _DEFAULT_MODELS[mode]


def _wrap_prompt(mode: Mode, question: str) -> str:
    q = (question or "").strip()
    if mode == "think":
        if (os.getenv("DEEPSEEK_THINK_USE_PROMPT_WRAP") or "0").strip().lower() in ("1", "true", "yes"):
            return (
                "다음 질문에 대해 필요하면 짧게 단계별 추론을 적고, 마지막에 결론을 한국어로 정리해 답해 주세요.\n\n"
                f"{q}"
            )
        return q
    if mode == "pro":
        return (
            "한국어로, 정확하고 충분한 근거를 두고 답해 주세요. 불확실하면 불확실함을 명시해 주세요.\n\n"
            f"{q}"
        )
    return q


def _api_url() -> str:
    base = (os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com").strip().rstrip("/")
    return f"{base}/chat/completions"


def ensure_configured() -> None:
    key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY 가 .env 에 없습니다. https://platform.deepseek.com/ 에서 발급 후 설정하세요."
        )


def generate_answer(question: str, mode: Mode) -> str:
    ensure_configured()
    model_id = _model_name(mode)
    prompt = _wrap_prompt(mode, question)
    log.info("DeepSeek request mode=%s model=%s chars=%s", mode, model_id, len(prompt))
    key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    payload: dict = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8192,
        "temperature": 0.4 if mode == "fast" else 0.6,
    }
    r = requests.post(
        _api_url(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=300,
    )
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        msg = err.get("message", r.text) if isinstance(err, dict) else (r.text or r.reason)
        raise RuntimeError(f"DeepSeek HTTP {r.status_code}: {msg}")
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices, list):
        raise RuntimeError(f"DeepSeek 응답 형식 오류: {data!r:.500}")
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    text = (content or "").strip()
    if not text:
        raise RuntimeError("DeepSeek 빈 응답 (모델·쿼터·프롬프트를 확인하세요)")
    return text


def usage_help_text() -> str:
    return (
        "DeepSeek API\n"
        "\n"
        "· /deepseek 또는 /ds <질문> — 이 채팅 기본 모드(처음은 fast)\n"
        "· /deepseek fast|think|pro <질문> — 일회 모드\n"
        "  (think → deepseek-reasoner, fast/pro → deepseek-chat)\n"
        "\n"
        ".env: DEEPSEEK_API_KEY (필수), 선택: DEEPSEEK_API_BASE, DEEPSEEK_MODEL_FAST|THINK|PRO"
    )
