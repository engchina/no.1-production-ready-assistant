"""回答の出どころの版（#1276、handoff §12 の「配置版本」）。

回答の記録（diagnostics の answer の ``provenance``）に、回答を作った検索・回答プロファイルの版と
プロンプトの版を残す。検索・回答プロファイルは版の番号を持たないため、更新時刻（``updated_at``）と
設定の内容の sha256 を版とする。プロンプトの版は、実行で使うプロンプトの内容（編集した
回答生成のテンプレート・画像検索のプロンプトと、コードで管理する取込から評価までの各段の
プロンプト。#2054）の sha256 の先頭で、内容が変われば変わる。どちらも秘密を含まない。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from app.rag.answer_prompts import EDITABLE_PROMPT_KEYS, default_prompt, readonly_prompt_stages

# プロンプトの版の接頭辞と、sha256 の何文字を使うか。
PROMPT_VERSION_PREFIX = "prompt-"
_PROMPT_VERSION_CHARS = 16


def _sha256(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _code_prompts() -> tuple[tuple[str, str], ...]:
    """コードで管理する各段のプロンプト（process の中では変わらない）。"""
    prompts: list[tuple[str, str]] = []
    for stage in readonly_prompt_stages():
        items = stage.get("prompts")
        for prompt in items if isinstance(items, list) else []:
            if isinstance(prompt, dict):
                prompts.append((f"{stage['id']}.{prompt['id']}", str(prompt["content"])))
    return tuple(prompts)


def answer_prompt_version(overrides: Mapping[str, str]) -> str:
    """回答フローが使うプロンプトの版（``prompt-`` + 内容の sha256 の先頭 16 文字）。"""
    editable = {key: overrides.get(key) or default_prompt(key) for key in EDITABLE_PROMPT_KEYS}
    digest = _sha256({"editable": editable, "code": dict(_code_prompts())})
    return PROMPT_VERSION_PREFIX + digest[:_PROMPT_VERSION_CHARS]


def search_answer_profile_revision(view: Any) -> dict[str, object]:
    """検索・回答プロファイルの版（id・更新時刻・設定の sha256）。"""
    updated_at = getattr(view, "updated_at", None)
    config = getattr(view, "config", None)
    dump = config.model_dump(mode="json") if isinstance(config, BaseModel) else {}
    return {
        "id": str(view.id),
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
        "config_sha256": _sha256(dump),
    }


__all__ = [
    "PROMPT_VERSION_PREFIX",
    "answer_prompt_version",
    "search_answer_profile_revision",
]
