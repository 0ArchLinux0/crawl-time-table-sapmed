"""Placeholder worker for future finance features."""
from __future__ import annotations

from core.config import WorkerContext


def run(ctx: WorkerContext | None = None) -> int:
    _ = ctx or WorkerContext.default()
    # Not implemented — returns success so registry can list it.
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
