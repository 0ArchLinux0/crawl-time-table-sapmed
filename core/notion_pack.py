"""
Notion: 준비물 to-do 블록을 지정 페이지에 붙이고, 블록 ID는 artifacts JSON에 저장 (봇에서 토글).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# 「10_MEDICAL」 페이지 (사용자 요청 URL 기준); .env NOTION_PACK_PAGE_ID 로 덮어쓰기 가능
DEFAULT_PACK_PAGE_ID_RAW = "339442f6b46d4c849edb487a4db293b5"

PACK_ITEMS_DEFAULT: list[str] = [
    "안경",
    "정기권",
    "학생증",
    "연구실 키 / 집 키",
    "모다피",
    "코뚜레",
    "아이패드 / 에어팟",
    "핸드폰",
    "도시락",
]


def normalize_notion_id(raw: str) -> str:
    """32 hex → UUID with hyphens (Notion API form)."""
    s = re.sub(r"[^0-9a-fA-F]", "", (raw or "").strip())
    if len(s) != 32:
        raise ValueError(f"Notion id must be 32 hex chars, got len={len(s)}")
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"


def pack_page_id() -> str:
    env = (os.getenv("NOTION_PACK_PAGE_ID") or "").strip()
    if env:
        return normalize_notion_id(env)
    return normalize_notion_id(DEFAULT_PACK_PAGE_ID_RAW)


def pack_meta_path(repo_root: Path) -> Path:
    return repo_root / "artifacts" / "notion_pack_blocks.json"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _req_exc(resp: requests.Response, what: str) -> RuntimeError:
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:800]}
    return RuntimeError(f"{what}: HTTP {resp.status_code} {body}")


def _append_block_children(token: str, parent_block_or_page_id: str, children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    url = f"{NOTION_API_BASE}/blocks/{parent_block_or_page_id}/children"
    r = requests.patch(
        url,
        headers=_headers(token),
        json={"children": children},
        timeout=60,
    )
    if r.status_code >= 400:
        raise _req_exc(r, "append blocks")
    data = r.json()
    return list(data.get("results") or [])


def _get_block(token: str, block_id: str) -> dict[str, Any] | None:
    block_id = normalize_notion_id(block_id.replace("-", ""))
    url = f"{NOTION_API_BASE}/blocks/{block_id}"
    r = requests.get(url, headers=_headers(token), timeout=30)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise _req_exc(r, "get block")
    return r.json()


def _patch_to_do(token: str, block_id: str, *, checked: bool, rich_text: list) -> None:
    bid = normalize_notion_id(block_id.replace("-", ""))
    url = f"{NOTION_API_BASE}/blocks/{bid}"
    r = requests.patch(
        url,
        headers=_headers(token),
        json={"to_do": {"rich_text": rich_text, "checked": checked}},
        timeout=30,
    )
    if r.status_code >= 400:
        raise _req_exc(r, "patch to_do")


def _load_meta(repo_root: Path) -> dict[str, Any] | None:
    p = pack_meta_path(repo_root)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_meta(repo_root: Path, payload: dict[str, Any]) -> None:
    p = pack_meta_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _meta_items_valid(token: str, meta: dict[str, Any]) -> bool:
    items = meta.get("items")
    if not isinstance(items, list) or len(items) != len(PACK_ITEMS_DEFAULT):
        return False
    seen: list[str] = []
    for i, row in enumerate(items):
        if not isinstance(row, dict):
            return False
        lab = row.get("label")
        bid = row.get("block_id")
        if lab != PACK_ITEMS_DEFAULT[i] or not isinstance(bid, str) or not bid.strip():
            return False
        b = _get_block(token, bid)
        if not b or b.get("type") != "to_do":
            return False
        seen.append(bid)
    return True


def _bootstrap(repo_root: Path, token: str, page_id: str) -> dict[str, Any]:
    children: list[dict[str, Any]] = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [
                    {"type": "text", "text": {"content": "준비물 체크리스트 (텔레그램 봇 연동)"}}
                ]
            },
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": "봇: /pack 목록 · /pack 3 (3번 토글) · /pack clear 전체 해제 · 처음부터 다시 붙이려면 /pack setup",
                        },
                    }
                ]
            },
        },
    ]
    for label in PACK_ITEMS_DEFAULT:
        children.append(
            {
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": label}}],
                    "checked": False,
                },
            }
        )

    results = _append_block_children(token, page_id, children)
    todos = [b for b in results if b.get("type") == "to_do"]
    if len(todos) != len(PACK_ITEMS_DEFAULT):
        raise RuntimeError(
            f"노션에 to_do {len(todos)}개만 생성됨 (기대 {len(PACK_ITEMS_DEFAULT)}). 페이지 권한·본문 길이를 확인하세요."
        )
    payload = {
        "page_id": page_id,
        "items": [
            {
                "label": PACK_ITEMS_DEFAULT[i],
                "block_id": normalize_notion_id(
                    str(todos[i].get("id", "")).replace("-", "")
                ),
            }
            for i in range(len(PACK_ITEMS_DEFAULT))
        ],
    }
    _save_meta(repo_root, payload)
    return payload


def ensure_pack_state(repo_root: Path, *, force_setup: bool = False) -> dict[str, Any]:
    token = (os.getenv("NOTION_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("NOTION_TOKEN 이 .env 에 없습니다. 노션 Integration 과 페이지 공유를 확인하세요.")
    page_id = pack_page_id()
    if force_setup:
        try:
            pack_meta_path(repo_root).unlink()
        except OSError:
            pass
        return _bootstrap(repo_root, token, page_id)

    meta = _load_meta(repo_root)
    if meta and _meta_items_valid(token, meta):
        return meta
    if meta:
        try:
            pack_meta_path(repo_root).unlink()
        except OSError:
            pass
        raise RuntimeError(
            "노션 준비물 블록과 로컬 연동 정보가 맞지 않습니다. `/pack setup` 을 한 번 실행하세요."
        )
    return _bootstrap(repo_root, token, page_id)


def format_pack_list(repo_root: Path) -> str:
    token = (os.getenv("NOTION_TOKEN") or "").strip()
    if not token:
        return "NOTION_TOKEN 없음 — .env 는 있나요?"
    meta = ensure_pack_state(repo_root)
    lines = ["📋 준비물 (노션 동기화)", ""]
    for i, row in enumerate(meta["items"], start=1):
        lab = str(row["label"])
        bid = str(row["block_id"])
        b = _get_block(token, bid)
        if not b:
            lines.append(f"{i}. ❓ {lab} (노션 블록 없음 → /pack setup)")
            continue
        td = b.get("to_do") or {}
        checked = bool(td.get("checked"))
        mark = "☑" if checked else "☐"
        lines.append(f"{i}. {mark} {lab}")
    lines.append("")
    lines.append("토글: /pack 1 또는 /pack 2 4 5 …   전체 해제: /pack clear")
    return "\n".join(lines)


def _toggle_one_index(token: str, meta: dict[str, Any], one_based: int) -> str | None:
    """Flip one to_do; return error line or None."""
    if one_based < 1 or one_based > len(PACK_ITEMS_DEFAULT):
        return f"· 번호 {one_based}: 1–{len(PACK_ITEMS_DEFAULT)} 만 가능"
    row = meta["items"][one_based - 1]
    bid = str(row["block_id"])
    b = _get_block(token, bid)
    if not b or b.get("type") != "to_do":
        return f"· 번호 {one_based}: 노션 블록 없음 → /pack setup"
    td = b["to_do"]
    rich = td.get("rich_text") or []
    cur = bool(td.get("checked"))
    _patch_to_do(token, bid, checked=not cur, rich_text=rich)
    return None


def toggle_pack_indices(repo_root: Path, one_based_indices: list[int]) -> str:
    """Toggle several items (order preserved; duplicates = toggle multiple times). Always ends with fresh list."""
    token = (os.getenv("NOTION_TOKEN") or "").strip()
    if not token:
        return "NOTION_TOKEN 없음"
    if not one_based_indices:
        return format_pack_list(repo_root)
    meta = ensure_pack_state(repo_root)
    errs: list[str] = []
    for idx in one_based_indices:
        e = _toggle_one_index(token, meta, idx)
        if e:
            errs.append(e)
    suffix = format_pack_list(repo_root)
    if errs:
        return "일부 반영 실패:\n" + "\n".join(errs) + "\n\n" + suffix
    return suffix


def toggle_pack_index(repo_root: Path, one_based: int) -> str:
    return toggle_pack_indices(repo_root, [one_based])


def pack_reset_after_schedule_sync_enabled() -> bool:
    """스케줄 동기화(아침 파이프라인) 성공 후 준비물 to-do 전부 해제."""
    v = (os.getenv("PACK_RESET_ON_SCHEDULE_SYNC") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def uncheck_all_pack_items(repo_root: Path) -> tuple[int, str | None]:
    """
    노션 to-do 전부 checked=False. (patched 개수, 오류 메시지).
    오류 시 두 번째 값에 사용자에게 줄 짧은 문구(영문 가능); 정상이면 None.
    """
    token = (os.getenv("NOTION_TOKEN") or "").strip()
    if not token:
        return 0, "NOTION_TOKEN 없음"
    try:
        meta = ensure_pack_state(repo_root)
    except RuntimeError as e:
        return 0, str(e)
    n = 0
    for row in meta["items"]:
        bid = str(row["block_id"])
        b = _get_block(token, bid)
        if not b or b.get("type") != "to_do":
            continue
        td = b["to_do"]
        rich = td.get("rich_text") or []
        if td.get("checked"):
            _patch_to_do(token, bid, checked=False, rich_text=rich)
            n += 1
    return n, None


def reset_pack_after_schedule_sync(repo_root: Path) -> str:
    """
    파이프라인 끝에서 호출. 비활성화면 'disabled'.
    활성화면 언체크만 수행(목록 조회 생략) → Notion API 가벼움.
    """
    if not pack_reset_after_schedule_sync_enabled():
        return "disabled"
    n, err = uncheck_all_pack_items(repo_root)
    if err:
        return f"skip: {err}"
    return f"ok (unchecked {n})" if n else "ok (already all unchecked)"


def clear_pack_all(repo_root: Path) -> str:
    _, err = uncheck_all_pack_items(repo_root)
    if err:
        return err
    return format_pack_list(repo_root)


def force_pack_setup(repo_root: Path) -> str:
    """메타 삭제 후 새 블록을 페이지에 추가 (기존 노션 블록은 자동 삭제하지 않음 → 중복 가능)."""
    token = (os.getenv("NOTION_TOKEN") or "").strip()
    if not token:
        return "NOTION_TOKEN 없음"
    ensure_pack_state(repo_root, force_setup=True)
    return "노션 페이지에 새 섹션을 붙였습니다. 예전 중복 블록은 노션에서 정리할 수 있습니다.\n\n" + format_pack_list(
        repo_root
    )


def pack_command_help() -> str:
    return (
        "/pack — 목록\n"
        "/pack <1–9> — 한 항목 토글\n"
        "/pack 2 4 5 — 공백으로 여러 번호 동시 토글 (끝에 갱신 목록 1회)\n"
        "/pack clear — 전체 해제\n"
        "/pack setup — 노션에 다시 생성 (중복 블록 생길 수 있음)"
    )
