"""根拠ごとの完全な出典。本文・引用照合とは分離して扱う。"""

from __future__ import annotations

import json
from typing import Any


def source_context(record: Any) -> dict[str, Any]:
    metadata = getattr(record, "metadata", {}) or {}
    path = metadata.get("section_path")
    if isinstance(path, str):
        path = [part.strip() for part in path.split(" > ") if part.strip()]
    context: dict[str, Any] = {"document_name": str(getattr(record, "source", "") or "")}
    if isinstance(path, (list, tuple)):
        context["section_path"] = [str(part) for part in path]
    header = metadata.get("context_header")
    if isinstance(header, str) and header.strip():
        context["search_context_header"] = header
    enabled = metadata.get("context_header_enabled")
    if isinstance(enabled, bool):
        context["search_context_header_enabled"] = enabled
    caption = metadata.get("table_caption")
    if isinstance(caption, str) and caption.strip():
        context["table_caption"] = caption
    role = metadata.get("html_region_role")
    if isinstance(role, str) and role in {"header", "footer", "caption"}:
        context["html_region"] = {"role": role, "text": str(getattr(record, "text", "") or "")}
    return context


def source_context_text(context: dict[str, Any]) -> str:
    return "出典情報: " + json.dumps(context, ensure_ascii=False)


def _derived_header(context: dict[str, Any]) -> str:
    """取込が索引用に作るヘッダ（``文書名 > section_path``）と同じ形。"""
    path = " > ".join(str(part) for part in context.get("section_path") or ())
    return " > ".join(part for part in (str(context.get("document_name") or "").strip(), path) if part)


def _shown_fields(context: dict[str, Any], *, document_name: str, section_path: Any) -> dict[str, Any]:
    shown: dict[str, Any] = {}
    for key, value in context.items():
        if key in {"source_id", "search_context_header_enabled", "children"}:
            continue  # chunk の uid と索引の設定は、モデルが引用・判断に使えない。
        if key == "document_name" and str(value or "") == document_name:
            continue
        if key == "section_path" and section_path is not None and list(value or ()) == list(section_path):
            continue
        if key == "search_context_header" and str(value or "").strip() == _derived_header(context):
            continue
        shown[key] = value
    return shown


def prompt_source_context(context: dict[str, Any], *, document_name: str = "") -> dict[str, Any]:
    """モデルへ示す出典情報。保存・照合用の完全な出典（``source_context``）から、繰り返しだけを除く（#3831）。

    document_name は根拠の機能の見出し（文書名 / 版 / 機能見出し）に出る文書名で、同じなら各根拠では省く。
    索引用のヘッダは文書名と章節から決まる形なら省き、子は chunk の uid を出さずブロックと違う値だけを残す。
    各根拠の完全な ``section_path`` は省かない（#3615）。保存する ``source_context`` は変えない。
    """
    shown = _shown_fields(context, document_name=document_name, section_path=None)
    children = []
    for child in context.get("children") or ():
        if not isinstance(child, dict):
            continue
        # 子の文書名は親と同じなら省く。子の章節はブロックの章節と同じなら省く。
        own = _shown_fields(child, document_name=str(context.get("document_name") or ""), section_path=context.get("section_path"))
        if own and own not in children:
            children.append(own)
    if children:
        shown["children"] = children
    return shown


def prompt_source_context_text(context: dict[str, Any], *, document_name: str = "") -> str:
    """``prompt_source_context`` の描画。示す値が無ければ空。予算の計算と生成の入力で同じ文字列を使う。"""
    shown = prompt_source_context(context, document_name=document_name)
    return source_context_text(shown) if shown else ""
