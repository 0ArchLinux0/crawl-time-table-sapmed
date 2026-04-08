"""
Backward-compatible entry for Task Scheduler: delegates to workers.smu_scheduler.

Prefer: python -m workers.smu_scheduler
Telegram orchestrator: python main_bot.py
"""
from __future__ import annotations

import sys

from core.config import load_env
from workers.smu_scheduler import run


if __name__ == "__main__":
    load_env()
    raise SystemExit(run())
