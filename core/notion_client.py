"""
Notion-related env access. Heavy API calls stay in notion_sync.py; this is for shared setup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class NotionCredentials:
    token: str
    database_id: str


def load_notion_credentials(*, required: bool = True) -> NotionCredentials | None:
    token = (os.getenv("NOTION_TOKEN") or "").strip()
    db_id = (os.getenv("NOTION_DB_ID") or "").strip()
    if not token or not db_id:
        if required:
            raise RuntimeError("NOTION_TOKEN and NOTION_DB_ID must be set in .env")
        return None
    return NotionCredentials(token=token, database_id=db_id)
