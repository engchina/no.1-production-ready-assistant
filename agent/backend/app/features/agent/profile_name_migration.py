"""RAG MCP 改名の保存値移行。instructions・ユーザー本文の文字列は置換しない。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_OLD_LIST = "rag_list_business_views"
_NEW_LIST = "rag_list_search_answer_profiles"
_FIELDS = {
    "business_view_id": "search_answer_profile_id",
    "business_view_ids": "search_answer_profile_ids",
}


def _function_name(server_id: str, tool_name: str) -> str:
    raw = f"{server_id}__{tool_name}"
    name = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if len(name) <= 64:
        return name
    digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"{name[:55]}_{digest}"


def migrate_tool_name(name: str) -> str:
    if name == _OLD_LIST:
        return _NEW_LIST
    # 保存された policy / checkpoint は接続 ID の記号置換・64 文字への短縮を含む。
    from app.features.agent.config import runtime_config_store

    for config in runtime_config_store.list_mcp_servers():
        if name == _function_name(config.server_id, _OLD_LIST):
            return _function_name(config.server_id, _NEW_LIST)
    prefix, separator, base = name.rpartition("__")
    if base == _OLD_LIST:
        return _function_name(prefix, _NEW_LIST) if separator else _NEW_LIST
    return name


def migrate_rag_call(value: object) -> object:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        return value
    name = value["name"]
    migrated_name = migrate_tool_name(name)
    if migrated_name == name and name.rsplit("__", 1)[-1] not in {_NEW_LIST, "rag_search"}:
        return value
    result = dict(value)
    result["name"] = migrated_name
    arguments = value.get("arguments")
    encoded = isinstance(arguments, str)
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return result
    if isinstance(arguments, dict):
        arguments = dict(arguments)
        for old, new in _FIELDS.items():
            if old not in arguments:
                continue
            if new in arguments and arguments[new] != arguments[old]:
                raise ValueError("保存された RAG 対象の新旧指定が一致しません。")
            arguments[new] = arguments.pop(old)
        result["arguments"] = json.dumps(arguments, ensure_ascii=False) if encoded else arguments
    return result


def migrate_sdk_checkpoint(text: str) -> str:
    """SDK の function call だけを移行する。未解析の文字列は改変しない。"""
    try:
        source = json.loads(text)
    except ValueError:
        return text

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            result = {key: visit(item) for key, item in value.items()}
            if value.get("type") in {"function_call", "tool_call"}:
                return migrate_rag_call(result)
            return result
        return value

    result = visit(source)
    return text if result == source else json.dumps(result, ensure_ascii=False)
