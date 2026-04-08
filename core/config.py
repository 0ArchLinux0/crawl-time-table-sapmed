"""Repository paths and .env loading (single place for DRY)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# sapmed-portal-crawler/ (parent of core/)
REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
ARTIFACTS_DIR = REPO_ROOT / "artifacts"


@dataclass(frozen=True)
class WorkerContext:
    """Passed into workers for paths and future DI (DB, mocks)."""

    repo_root: Path

    @staticmethod
    def default() -> WorkerContext:
        return WorkerContext(repo_root=REPO_ROOT)


def load_env(path: Path | None = None) -> None:
    """Load `.env` from repo root unless an alternate path is given."""
    load_dotenv(path or (REPO_ROOT / ".env"))


def ensure_runtime_dirs() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS_DIR / "logs").mkdir(parents=True, exist_ok=True)
