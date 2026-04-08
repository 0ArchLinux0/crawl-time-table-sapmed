"""Standard worker interface — subclass or call registered run functions."""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.config import WorkerContext


class Worker(ABC):
    """All workers implement `run` and return a process exit code (0 = success)."""

    name: str

    @abstractmethod
    def run(self, ctx: WorkerContext) -> int:
        raise NotImplementedError


class FunctionWorker(Worker):
    """Wrap a plain function for a consistent Worker API."""

    def __init__(self, name: str, fn) -> None:
        self.name = name
        self._fn = fn

    def run(self, ctx: WorkerContext) -> int:
        return int(self._fn(ctx))
