"""Gemini (Google AI Studio API) — 질문/답변 for Telegram bot."""
from __future__ import annotations

import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

from core.telegram_ai_util import Mode, mode_from_token, split_telegram_chunks

__all__ = (
    "Mode",
    "ensure_configured",
    "generate_answer",
    "usage_help_text",
    "split_telegram_chunks",
    "mode_from_token",
)

log = logging.getLogger("gemini_reply")

# 키·프로젝트마다 허용 모델이 다름. -flash-latest 별칭이 gRPC에서 오래 걸리거나 멈춘 것처럼 보일 수 있어 기본은 명시 ID.
# 무료 티어 limit:0(429) → 폴백. Pro 등은 .env 로만.
_DEFAULT_MODELS: dict[Mode, str] = {
    "fast": "gemini-2.5-flash",
    "think": "gemini-2.5-flash",
    "pro": "gemini-2.5-flash",
}


def _normalize_model_id(raw: str) -> str:
    """list_models 는 models/gemini-… 형태인데, GenerativeModel() 은 보통 gemini-… 만 사용 — 접두사 있으면 404 날 수 있음."""
    s = (raw or "").strip()
    prefix = "models/"
    if s.startswith(prefix):
        s = s[len(prefix) :].lstrip("/")
    return s


def _model_name(mode: Mode) -> str:
    env = {
        "fast": "GEMINI_MODEL_FAST",
        "think": "GEMINI_MODEL_THINK",
        "pro": "GEMINI_MODEL_PRO",
    }[mode]
    chosen = (os.getenv(env) or "").strip() or _DEFAULT_MODELS[mode]
    return _normalize_model_id(chosen)


def _wrap_prompt(mode: Mode, question: str) -> str:
    q = (question or "").strip()
    if mode == "think":
        if (os.getenv("GEMINI_THINK_USE_PROMPT_WRAP") or "1").strip().lower() in ("0", "false", "no"):
            return q
        return (
            "다음 질문에 대해 필요하면 짧게 단계별 추론을 적고, 마지막에 결론을 한국어로 정리해 답해 주세요.\n\n"
            f"{q}"
        )
    if mode == "pro":
        return (
            "한국어로, 정확하고 충분한 근거를 두고 답해 주세요. 불확실하면 불확실함을 명시해 주세요.\n\n"
            f"{q}"
        )
    return q


def ensure_configured() -> None:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY 가 .env 에 없습니다. https://aistudio.google.com/apikey 에서 발급 후 설정하세요."
        )
    genai.configure(api_key=key)


def _retry_env_int(name: str, default: int) -> int:
    try:
        return max(1, int((os.getenv(name) or str(default)).strip(), 10))
    except ValueError:
        return default


def _retry_base_seconds() -> float:
    try:
        return max(0.5, float((os.getenv("GEMINI_RETRY_BASE_SEC") or "2.0").strip()))
    except ValueError:
        return 2.0


def _is_rate_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, (google_exceptions.ResourceExhausted, google_exceptions.TooManyRequests)):
        return True
    s = str(exc).lower()
    if "429" in s:
        return True
    if "resource exhausted" in s or "resourceexhausted" in s.replace(" ", ""):
        return True
    if "quota" in s and ("exceed" in s or "exceeded" in s):
        return True
    return False


def _retry_sleep_seconds(exc: BaseException, attempt: int, base_delay: float, cap: float) -> float:
    """Google 응답의 'Please retry in Ns' 를 우선 사용, 없으면 지수 백오프."""
    m = re.search(r"retry\s+in\s+([\d.]+)\s*s", str(exc), re.I)
    if m:
        return min(float(m.group(1)) + random.uniform(0.25, 1.25), cap)
    return min(base_delay * (2**attempt) + random.uniform(0, 1.0), cap)


def _default_fallbacks(primary: str) -> list[str]:
    """GEMINI_MODEL_FALLBACKS 미설정 시. lite/별칭은 쿼터가 다른 경우가 있음."""
    if primary == "gemini-2.5-flash":
        return ["gemini-2.0-flash", "gemini-flash-latest", "gemini-2.0-flash-lite"]
    if primary == "gemini-flash-latest":
        return ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]
    if primary == "gemini-2.0-flash":
        return ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash-lite"]
    if primary == "gemini-1.5-flash":
        return ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
    return ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]


def _is_quota_limit_zero(exc: BaseException) -> bool:
    """무료 티어 한도 소진 등으로 같은 모델 재시도·장시간 sleep 이 무의미한 경우."""
    s = str(exc).lower()
    return "limit: 0" in s or "limit:0" in s


def _model_chain(mode: Mode) -> list[str]:
    primary = _model_name(mode)
    raw = (os.getenv("GEMINI_MODEL_FALLBACKS") or "").strip()
    if raw:
        rest = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        rest = _default_fallbacks(primary)
    seen: set[str] = {primary}
    chain = [primary]
    for m in rest:
        if m not in seen:
            seen.add(m)
            chain.append(m)
    return chain


def _quota_user_hint(exc_msg: str) -> str:
    if "limit: 0" not in exc_msg.lower():
        return ""
    return (
        "\n\n[안내] 에러에 limit: 0 이 보이면 이 API 키(프로젝트)에 해당 모델 무료 쿼터가 아직 안 붙었거나 차단된 상태일 수 있습니다. "
        "https://aistudio.google.com → Plan / API 설정, 또는 Google Cloud Console에서 Generative Language API·결제 프로필을 확인하세요. "
        "또는 .env 의 GEMINI_MODEL_* / GEMINI_MODEL_FALLBACKS 를 list_models()에 나온 이름으로 맞추세요."
    )


def _is_model_not_found_error(exc: BaseException) -> bool:
    if isinstance(exc, google_exceptions.NotFound):
        return True
    s = str(exc).lower()
    if "is not found for api version" in s:
        return True
    if "404" in s and ("not found" in s or "is not found" in s):
        return True
    return False


def _not_found_user_hint(exc_msg: str) -> str:
    s = (exc_msg or "").lower()
    if "is not found for api version" not in s and not ("404" in s and "not found" in s):
        return ""
    return (
        "\n\n[안내] 이 API 키에 해당 모델이 없습니다. "
        "`scripts/check_models.py` 로 허용 목록을 확인한 뒤 `.env`에 GEMINI_MODEL_FAST 등을 그중 하나로 지정하세요."
    )


def _response_to_text(resp: object, model_id: str) -> str:
    text = ""
    try:
        text = ((getattr(resp, "text", None) or "") or "").strip()
    except ValueError:
        text = ""
    if not text and getattr(resp, "candidates", None):
        parts = []
        for c in resp.candidates:
            content = getattr(c, "content", None)
            if content and getattr(content, "parts", None):
                for p in content.parts:
                    if hasattr(p, "text") and p.text:
                        parts.append(p.text)
            fr = getattr(c, "finish_reason", None)
            if fr and str(fr) != "STOP" and not parts:
                parts.append(f"(finish_reason={fr})")
        text = "\n".join(parts).strip()
    if not text:
        pf = getattr(resp, "prompt_feedback", None)
        raise RuntimeError(
            f"Gemini 응답 없음 또는 차단. model={model_id} feedback={pf} "
            "(프롬프트·안전 필터·쿼터를 확인하세요)"
        )
    return text


def _http_timeout_sec() -> float | None:
    try:
        v = float((os.getenv("GEMINI_HTTP_TIMEOUT_SEC") or "60").strip())
        return v if v > 0 else None
    except ValueError:
        return 60.0


def _per_call_deadline_sec() -> float:
    """gRPC 경로는 request_options 를 무시할 수 있어, 스레드 단위로 전체 호출 상한을 둠."""
    try:
        return max(20.0, float((os.getenv("GEMINI_PER_CALL_DEADLINE_SEC") or "55").strip()))
    except ValueError:
        return 55.0


def _generate_with_deadline(model_id: str, prompt: str, mode: Mode) -> str:
    deadline = _per_call_deadline_sec()
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_generate_once, model_id, prompt, mode)
        try:
            return fut.result(timeout=deadline)
        except FuturesTimeoutError as e:
            raise TimeoutError(
                f"Gemini 호출이 {deadline:.0f}s 안에 끝나지 않았습니다 (model={model_id}). "
                "네트워크/SDK 지연이거나 모델 응답 지연입니다. GEMINI_PER_CALL_DEADLINE_SEC 를 늘리거나 다른 모델을 시도하세요."
            ) from e


def _generate_once(model_id: str, prompt: str, mode: Mode) -> str:
    model = genai.GenerativeModel(model_id)
    to = _http_timeout_sec()
    req_kwargs: dict = {}
    if to is not None:
        req_kwargs["request_options"] = {"timeout": int(to)}
    resp = model.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.4 if mode == "fast" else 0.6,
            max_output_tokens=8192,
        ),
        **req_kwargs,
    )
    return _response_to_text(resp, model_id)


def generate_answer(question: str, mode: Mode) -> str:
    ensure_configured()
    chain = _model_chain(mode)
    prompt = _wrap_prompt(mode, question)
    max_retries = _retry_env_int("GEMINI_MAX_RETRIES", 6)
    base_delay = _retry_base_seconds()
    cap = 120.0
    log.info(
        "Gemini request mode=%s models=%s chars=%s retries/model<=%s",
        mode,
        chain,
        len(prompt),
        max_retries,
    )

    last_err: BaseException | None = None
    for model_id in chain:
        for attempt in range(max_retries):
            try:
                return _generate_with_deadline(model_id, prompt, mode)
            except TimeoutError as e:
                last_err = e
                log.warning("Gemini deadline exceeded: %s — trying fallback", model_id)
                break
            except Exception as e:
                last_err = e
                if _is_model_not_found_error(e):
                    log.warning("Gemini 404 / 모델 없음: %s — 폴백 모델 시도", model_id)
                    break
                if not _is_rate_limit_error(e):
                    raise RuntimeError(
                        str(e) + _quota_user_hint(str(e)) + _not_found_user_hint(str(e))
                    ) from e
                if _is_quota_limit_zero(e):
                    log.warning(
                        "Gemini model=%s quota reports limit:0 — skip retry delay, try next model",
                        model_id,
                    )
                    break
                if attempt >= max_retries - 1:
                    log.warning(
                        "Gemini model=%s gave rate limit after %s attempts, trying next model if any",
                        model_id,
                        max_retries,
                    )
                    break
                sleep_s = _retry_sleep_seconds(e, attempt, base_delay, cap)
                log.warning(
                    "Gemini rate limit model=%s attempt %s/%s, sleeping %.1fs: %s",
                    model_id,
                    attempt + 1,
                    max_retries,
                    sleep_s,
                    e,
                )
                time.sleep(sleep_s)

    if last_err is not None:
        raise RuntimeError(
            str(last_err) + _quota_user_hint(str(last_err)) + _not_found_user_hint(str(last_err))
        ) from last_err
    raise RuntimeError("Gemini 호출 실패(원인 불명)")


def usage_help_text() -> str:
    return (
        "Gemini (Google AI Studio API)\n"
        "\n"
        "· /gemini <질문> — 이 채팅에 저장된 기본 모드(처음은 fast)\n"
        "· /gemini fast|빠름 <질문>  /  think|사고  /  pro|프로\n"
        "· /g 와 /ask 는 /gemini 와 동일\n"
        "\n"
        "· /gemini_default — 지금 채팅 기본 모드 보기\n"
        "· /gemini_default fast|think|pro — 기본 모드 저장\n"
        "\n"
        "· /g · /ask — 이 채팅에서 /provider 로 고른 기본 백엔드로 질문\n"
        "\n"
        ".env: GEMINI_API_KEY (필수)\n"
        "선택: GEMINI_MODEL_FAST|THINK|PRO , GEMINI_MODEL_FALLBACKS (콤마 구분; 404·429 시 다음 모델)\n"
        "기본 모델: gemini-2.5-flash (폴백·check_models.py)\n"
        "GEMINI_PER_CALL_DEADLINE_SEC — 모델 1회 호출 최대 대기(기본 55s, hang 방지)\n"
        "GEMINI_MAX_RETRIES (기본 6), GEMINI_RETRY_BASE_SEC (기본 2)\n"
        "429 limit:0 이면 다음 모델. 404면 목록에 없는 이름."
    )
