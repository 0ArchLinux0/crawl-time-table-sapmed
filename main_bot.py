"""
Telegram orchestrator: routes commands to workers via subprocess (non-blocking I/O thread).

Run from repo root:
  .\\.venv\\Scripts\\python.exe main_bot.py

Requires TELEGRAM_BOT_TOKEN in .env (same bot as alerts, or a dedicated bot token).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from core.bot_command_catalog import telegram_full_command_help
from core.config import REPO_ROOT, load_env
from core.logging_config import setup_bot_logging
from core import class_reminders, deepseek_reply, gemini_prefs, gemini_reply, notion_pack
from core.telegram_ai_util import mode_from_token, split_telegram_chunks
from core.schedule_read import (
    format_day_schedule,
    format_sync_status,
    load_schedule_rows,
    rows_for_date,
    today_in_jst,
)

log = logging.getLogger("main_bot")


def _gemini_command_timeout_sec() -> float:
    """Gemini: 폴백 모델 여러 개 × GEMINI_PER_CALL_DEADLINE_SEC 를 감안해 넉넉히."""
    try:
        return max(90.0, float((os.getenv("GEMINI_BOT_COMMAND_TIMEOUT_SEC") or "300").strip()))
    except ValueError:
        return 300.0


def _help_message() -> str:
    return (
        "Sapmed Orchestrator\n"
        "/schedule — SMU 포털 → 노션 동기화 (서브프로세스)\n"
        "/today — 오늘 시간표 (로컬 schedule.json, JST)\n"
        "/tomorrow — 내일 시간표\n"
        "/syncstatus — 마지막 동기화·행 개수\n"
        "/pack — 노션 준비물 체크리스트 (목록·토글)\n"
        "/gemini — Gemini · /deepseek — DeepSeek · /g /ask — 둘 중 기본( /provider )\n"
        "/commands — 명령 전체 목록\n"
        "/ping — 응답 확인\n"
        "/help — 이 도움말\n"
        "수업 N분 전 알림: `.env` SCHEDULE_CLASS_REMINDERS=1 + TELEGRAM_CHAT_ID (봇 동작 중)\n"
        "BotFather 붙여넣기: BOT_COMMANDS.md"
    )


async def _class_reminder_loop(application: Application) -> None:
    await asyncio.sleep(25)
    while True:
        try:
            if class_reminders.reminders_enabled():
                chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
                if chat:
                    try:
                        chat_id = int(chat)
                    except ValueError:
                        chat_id = None
                    if chat_id is not None:
                        pairs = await asyncio.to_thread(class_reminders.compute_due_reminders)
                        sent_ok: list[str] = []
                        for key, text in pairs:
                            try:
                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text=text[:4000],
                                )
                                sent_ok.append(key)
                            except Exception:
                                log.exception("class reminder send failed key=%s", key)
                        if sent_ok:
                            await asyncio.to_thread(class_reminders.mark_reminders_sent, sent_ok)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("class reminder loop tick")
        await asyncio.sleep(60)


async def _post_init(application: Application) -> None:
    asyncio.create_task(_class_reminder_loop(application))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_help_message())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = telegram_full_command_help()
    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"
    await update.message.reply_text(text)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    schedule_path = REPO_ROOT / "schedule.json"
    rows, err = load_schedule_rows(schedule_path)
    if err:
        await update.message.reply_text(err)
        return
    assert rows is not None
    d = today_in_jst()
    day_rows = rows_for_date(rows, d)
    text = format_day_schedule(d, day_rows)
    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"
    await update.message.reply_text(text)


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    schedule_path = REPO_ROOT / "schedule.json"
    rows, err = load_schedule_rows(schedule_path)
    if err:
        await update.message.reply_text(err)
        return
    assert rows is not None
    d = today_in_jst() + timedelta(days=1)
    day_rows = rows_for_date(rows, d)
    text = format_day_schedule(d, day_rows)
    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"
    await update.message.reply_text(text)


async def cmd_syncstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_sync_status(REPO_ROOT)
    await update.message.reply_text(text)


async def cmd_pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = [a.strip() for a in (context.args or [])]
    try:
        if not args:
            text = await asyncio.to_thread(notion_pack.format_pack_list, REPO_ROOT)
        elif args[0].lower() in ("help", "h", "?"):
            text = notion_pack.pack_command_help()
        elif args[0].lower() == "clear":
            text = await asyncio.to_thread(notion_pack.clear_pack_all, REPO_ROOT)
        elif args[0].lower() == "setup":
            text = await asyncio.to_thread(notion_pack.force_pack_setup, REPO_ROOT)
        elif all(x.isdigit() for x in args):
            nums = [int(x) for x in args]
            text = await asyncio.to_thread(notion_pack.toggle_pack_indices, REPO_ROOT, nums)
        else:
            text = "알 수 없는 인자입니다.\n" + notion_pack.pack_command_help()
    except RuntimeError as e:
        text = str(e)
    except Exception as e:
        log.exception("pack command failed")
        text = f"오류: {e}"
    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"
    await update.message.reply_text(text)


def _parse_ai_args(args: list[str], default_mode: gemini_prefs.Mode):
    """Returns (mode, question) or (mode, None) if mode-only / (None, None) if empty."""
    if not args:
        return None, None
    mode = default_mode
    i = 0
    mt = mode_from_token(args[0])
    if mt is not None:
        mode = mt
        i = 1
    if i >= len(args):
        return mode, None
    return mode, " ".join(args[i:])


def _ai_combined_help() -> str:
    return (
        "질문 명령\n"
        "\n"
        + gemini_reply.usage_help_text()
        + "\n\n"
        + deepseek_reply.usage_help_text()
        + "\n\n"
        "· /g · /ask — 이 채팅 기본 백엔드로 질문 ( /provider 로 gemini | deepseek )\n"
        "· /provider — 기본 백엔드 보기·바꾸기 (/p)"
    )


async def _send_ai_reply(update: Update, text: str) -> None:
    for part in split_telegram_chunks(text):
        if not part:
            continue
        await update.message.reply_text(part[:4096])


async def cmd_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = [a.strip() for a in (context.args or [])]
    chat_id = update.effective_chat.id
    if not args:
        await update.message.reply_text(gemini_reply.usage_help_text())
        return
    mode, question = _parse_ai_args(args, gemini_prefs.get_default_mode(chat_id))
    if question is None:
        await update.message.reply_text(
            "모드만 있고 질문이 없어요.\n예: /gemini 수컷 쥐의 유전자형은?\n예: /gemini pro 이 논문 요약해줘"
        )
        return
    await update.message.reply_text(f"Gemini ({mode}) …")
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(gemini_reply.generate_answer, question, mode),
            timeout=_gemini_command_timeout_sec(),
        )
    except asyncio.TimeoutError:
        log.warning("gemini command timed out after %s s", _gemini_command_timeout_sec())
        await update.message.reply_text(
            "Gemini 응답 시간 초과(API 지연·쿼터 재시도 등). 잠시 뒤 다시 시도하거나 "
            "`.env`의 GEMINI_MODEL_* / GEMINI_MODEL_FALLBACKS 를 확인하세요."
        )
        return
    except Exception as e:
        log.exception("gemini command failed")
        await update.message.reply_text(f"오류: {e}")
        return
    await _send_ai_reply(update, text)


async def cmd_deepseek(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = [a.strip() for a in (context.args or [])]
    chat_id = update.effective_chat.id
    if not args:
        await update.message.reply_text(deepseek_reply.usage_help_text())
        return
    mode, question = _parse_ai_args(args, gemini_prefs.get_default_mode(chat_id))
    if question is None:
        await update.message.reply_text(
            "모드만 있고 질문이 없어요.\n예: /deepseek 요약해줘\n예: /ds pro 장문 설명"
        )
        return
    await update.message.reply_text(f"DeepSeek ({mode}) …")
    try:
        text = await asyncio.to_thread(deepseek_reply.generate_answer, question, mode)
    except Exception as e:
        log.exception("deepseek command failed")
        await update.message.reply_text(f"오류: {e}")
        return
    await _send_ai_reply(update, text)


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = [a.strip() for a in (context.args or [])]
    chat_id = update.effective_chat.id
    if not args:
        await update.message.reply_text(_ai_combined_help())
        return
    mode, question = _parse_ai_args(args, gemini_prefs.get_default_mode(chat_id))
    if question is None:
        await update.message.reply_text(
            "모드만 있고 질문이 없어요.\n예: /ask 설명해 줘\n기본 엔진: /provider"
        )
        return
    prov = gemini_prefs.get_default_provider(chat_id)
    if prov == "deepseek":
        await update.message.reply_text(f"DeepSeek ({mode}, 기본) …")
        try:
            text = await asyncio.to_thread(deepseek_reply.generate_answer, question, mode)
        except Exception as e:
            log.exception("ask/deepseek failed")
            await update.message.reply_text(f"오류: {e}")
            return
        await _send_ai_reply(update, text)
    else:
        await update.message.reply_text(f"Gemini ({mode}, 기본) …")
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(gemini_reply.generate_answer, question, mode),
                timeout=_gemini_command_timeout_sec(),
            )
        except asyncio.TimeoutError:
            log.warning("ask/gemini timed out")
            await update.message.reply_text(
                "Gemini 응답 시간 초과. 잠시 뒤 다시 시도하거나 GEMINI_MODEL_* 를 확인하세요."
            )
            return
        except Exception as e:
            log.exception("ask/gemini failed")
            await update.message.reply_text(f"오류: {e}")
            return
        await _send_ai_reply(update, text)


async def cmd_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = [a.strip() for a in (context.args or [])]
    chat_id = update.effective_chat.id
    cur = gemini_prefs.get_default_provider(chat_id)
    if not args:
        await update.message.reply_text(
            f"이 채팅 기본 AI: {cur}\n"
            "· /g · /ask 가 여기로 연결됩니다.\n"
            "바꾸기: /provider gemini 또는 /provider deepseek (/ds 는 질문 명령)\n"
            "모드(fast·think·pro): /gemini_default"
        )
        return
    t = args[0].lower()
    if t in ("gemini", "g", "google", "지미니"):
        gemini_prefs.set_default_provider(chat_id, "gemini")
        await update.message.reply_text("기본 AI를 Gemini 로 저장했습니다.")
        return
    if t in ("deepseek", "ds", "딥시크", "d"):
        gemini_prefs.set_default_provider(chat_id, "deepseek")
        await update.message.reply_text("기본 AI를 DeepSeek 로 저장했습니다.")
        return
    await update.message.reply_text("gemini 또는 deepseek 만 지원합니다. 예: /provider deepseek")


async def cmd_gemini_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = [a.strip() for a in (context.args or [])]
    chat_id = update.effective_chat.id
    cur = gemini_prefs.get_default_mode(chat_id)
    if not args:
        await update.message.reply_text(
            f"이 채팅 기본 모드: {cur}\n"
            "· fast — 빠른 답 (기본)\n"
            "· think — 사고(프롬프트로 단계 유도, 동일 모델이면 아래 GEMINI_MODEL_THINK)\n"
            "· pro — 긴·신중한 답 (기본 모델은 Flash 계열, .env 로 Pro 지정 가능)\n\n"
            "바꾸기: /gemini_default think\n"
            "질문: /gemini 또는 /deepseek 또는 /ask (기본: /provider)\n"
            f"지금 이 채팅 기본 백엔드: {gemini_prefs.get_default_provider(chat_id)}"
        )
        return
    m = mode_from_token(args[0])
    if m is None:
        await update.message.reply_text("fast | think | pro (또는 빠름·사고·프로) 중 하나를 적어 주세요.")
        return
    gemini_prefs.set_default_mode(chat_id, m)
    await update.message.reply_text(f"기본 모드를 {m}(으)로 저장했습니다. 다음부터 /gemini 만 써도 이 모드로 갑니다.")


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("스케줄 워커 실행 중… (브라우저·노션 때문에 1–3분 걸릴 수 있음)")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "workers.smu_scheduler",
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    out_b, _ = await proc.communicate()
    text = out_b.decode("utf-8", errors="replace").strip()
    if len(text) > 3500:
        text = text[:3490] + "\n…(truncated)"
    code = proc.returncode
    await update.message.reply_text(f"exit={code}\n\n{text or '(no output)'}")


def main() -> None:
    load_env()
    setup_bot_logging()
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN missing in .env")
        sys.exit(1)

    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("commands", cmd_commands))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("syncstatus", cmd_syncstatus))
    app.add_handler(CommandHandler("pack", cmd_pack))
    app.add_handler(CommandHandler("gemini", cmd_gemini))
    app.add_handler(CommandHandler("deepseek", cmd_deepseek))
    app.add_handler(CommandHandler("ds", cmd_deepseek))
    app.add_handler(CommandHandler("g", cmd_ask))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("provider", cmd_provider))
    app.add_handler(CommandHandler("p", cmd_provider))
    app.add_handler(CommandHandler("gemini_default", cmd_gemini_default))
    app.add_handler(CommandHandler("gset", cmd_gemini_default))
    app.add_handler(CommandHandler("schedule", cmd_schedule))

    log.info("Starting Telegram polling (repo=%s)", REPO_ROOT)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
