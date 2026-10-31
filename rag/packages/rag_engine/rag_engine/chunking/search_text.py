"""child chunk の検索文（retrieval_text）の組み立て。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from rag_engine.chunking.constants import (
    SOURCE_TABLE_CROP_RELATIONSHIP,
    ChunkingConfig,
    DocumentChunk,
    _clean_search_text,
    _context_preview,
    _int_value,
    _metadata_classification,
    _ordered_nonempty,
    _page_span,
    _trim_search_text,
)
from rag_engine.parsing.content_labels import content_labels
from rag_engine.parsing.picture_text import picture_ocr_text_label
from rag_engine.parsing.vision_prompt_rules import without_vision_screen_values

# 画像の生成説明にある画面キャプチャの例示値の文。Vision prompt は例示値に「画面例では」「入力例として」
# （英語では input example / display example）を付ける規則なので、その文を検索用テキストから除く。文書の説明では
# ない値が質問の語と字面で一致して別機能の画面が最上位候補になるのを防ぐ (#753)。回答用の text は変えない。
# 終端に " / " を含めるのは、visible_values 等が 1 行へ " / " 連結されるため。句点で終わらない行で行末まで
# 削ると、同じ行の後続項目（コード・項目名）まで検索対象から失われる (#766)。
# 手がかりの語は言語資源（content_labels.screen_example_cues。#2181）で、正規表現は content_labels().screen_example。

# 例示値だけを削った跡に残る連続した区切り。
_REPEATED_SEPARATOR_PATTERN = re.compile(r"(?:\s*/\s*){2,}")

# 検索文の中で child 本文の始まりを示す行頭ラベル。検索文は文脈行の後にこのラベルを置き、以降を本文とする。
# 文脈行は全 chunk 共通の定型なので、本文だけを対象にしたい処理はこのラベルで切り出す (#1035)。
CHILD_SEARCH_TEXT_HEADER = "Child text:"


def _without_screen_examples(text: str) -> str:
    """生成説明から画面の値を除いた検索用テキストを返す。

    行ラベルで判別できる値の行（`値:` / `フォームの項目:`）をまず落とし、`回答用本文` のように
    文章の中へ書かれた例示値は接頭辞付きの文として落とす (#778)。
    """
    stripped = content_labels().screen_example.sub("", without_vision_screen_values(text or ""))
    stripped = _REPEATED_SEPARATOR_PATTERN.sub(" / ", stripped)
    return re.sub(r"[ \t]+\n", "\n", stripped).strip()


def _child_search_text(
    child: DocumentChunk,
    *,
    parent: DocumentChunk,
    config: ChunkingConfig,
    body: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    """元本文を保持して残余予算に補足を詰めます。分割不能な本文は文字数目標を超えます。

    `body` は検索文の土台にする本文で、省略時は `child.text`。builder は Vision 成功時の OCR を
    外した本文（`_join_records_text(for_retrieval=True)`）を渡します (#900)。
    """
    child_text = _without_screen_examples(child.text if body is None else body)
    if not config.contextual_search_text_enabled:
        return child_text, ("child_text",)

    components: list[tuple[str, str, str]] = []
    # UI の一時 upload directory やランダム ID は意味情報ではないため basename のみ使う。
    source_name = re.split(r"[\\/]", child.source_file_name)[-1]
    _append_search_component(components, "source_file", "Source file", source_name)
    # 文書の有効期間は embedding に混ぜない。検索 filter と回答 context で扱う (#893)。
    classification_text = _classification_search_text(_metadata_classification(child.metadata))
    _append_search_component(components, "classification", "Classification", classification_text)
    _append_search_component(components, "page_span", "Pages", _chunk_page_context_text(child))
    _append_search_component(components, "section_path", "Section path", " > ".join(child.metadata.get("section_path") or []))
    # 親 ID は追跡専用。意味情報を持たず、検索文脈の文字数予算を消費するため含めない。
    # 親の retrieval_text は画面例の値と Vision 成功時の OCR を外した本文 (#778, #900)。
    _append_search_component(components, "parent_blurb", "Parent summary", _context_preview(parent.retrieval_text, 360))
    _append_search_component(components, "content_signals", "Content signals", _content_signal_text(child.metadata))
    _append_search_component(components, "table_context", "Table context", _table_search_text(child.metadata.get("table_context")))
    _append_search_component(components, "visual_context", "Visual context", _visual_search_text(child, body=child_text))
    layout = child.metadata.get("layout") if isinstance(child.metadata.get("layout"), dict) else {}
    _append_search_component(components, "form_context", "Form context", _form_search_text(layout.get("form_context")))
    _append_search_component(components, "formula_context", "Formula context", _formula_search_text(layout.get("formula_context")))
    _append_search_component(components, "keywords", "Keywords", _child_search_keywords(child, parent, body=child_text))

    child_header = CHILD_SEARCH_TEXT_HEADER
    # 文字数は目標値とし、分割できない表の行や図の本文を metadata のために切り捨てない。
    context_budget = max(
        0,
        min(config.search_text_context_max_chars, config.child_search_text_max_chars - len(child_text) - len(child_header) - 2),
    )
    context_text, included = _pack_search_context(
        components,
        max_chars=context_budget,
    )
    pieces = [context_text, child_header, child_text] if context_text else [child_header, child_text]
    return "\n".join(piece for piece in pieces if piece).strip(), (*included, "child_text")


def _append_search_component(
    components: list[tuple[str, str, str]],
    name: str,
    label: str,
    value: Any,
) -> None:
    text = _clean_search_text(value)
    if text:
        components.append((name, label, text))


def _pack_search_context(
    components: Sequence[tuple[str, str, str]],
    *,
    max_chars: int,
) -> tuple[str, tuple[str, ...]]:
    lines: list[str] = []
    included: list[str] = []
    used_chars = 0
    for name, label, value in components:
        line = f"{label}: {value}"
        separator_len = 1 if lines else 0
        remaining = max_chars - used_chars - separator_len
        if remaining <= 40:
            break
        if len(line) > remaining:
            line = _trim_search_text(line, remaining)
        lines.append(line)
        included.append(name)
        used_chars += len(line) + separator_len
    return "\n".join(lines).strip(), tuple(included)


def _classification_search_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    pieces = [
        str(value.get("large_category") or ""),
        str(value.get("middle_category") or ""),
        str(value.get("small_category") or ""),
    ]
    return " / ".join(_ordered_nonempty(pieces))


def _chunk_page_context_text(chunk: DocumentChunk) -> str:
    pages = _page_span(chunk.page_start, chunk.page_end)
    labels = (chunk.metadata.get("layout") or {}).get("logical_page_labels") if isinstance(chunk.metadata, dict) else {}
    if not isinstance(labels, dict) or not labels:
        return pages
    # key は JSON 保存のため文字列です。文字列順では p.10 が p.9 より前に並びます。
    logical = " / ".join(f"p.{page}=logical {label}" for page, label in sorted(labels.items(), key=lambda item: _int_value(item[0]) or 0))
    return f"{pages}; {logical}"


def _content_signal_text(metadata: dict[str, Any]) -> str:
    categories = metadata.get("source_categories") if isinstance(metadata.get("source_categories"), list) else []
    enabled_flags = []
    layout = metadata.get("layout") if isinstance(metadata.get("layout"), dict) else {}
    derived = {
        "contains_table": "Table" in categories,
        "contains_picture": "Picture" in categories,
        "contains_image_evidence": bool(metadata.get("image_evidence")),
        "contains_structured_table": bool(metadata.get("table_context")),
        "contains_form_field": bool(layout.get("form_context")),
        "contains_list": bool(layout.get("list_context")),
        "contains_formula": bool(layout.get("formula_context")),
    }
    enabled_flags.extend(key for key, enabled in derived.items() if enabled)
    return " / ".join(_ordered_nonempty([*categories, *enabled_flags]))


def _table_search_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    pieces: list[str] = []
    for table in value[:3]:
        if not isinstance(table, dict):
            continue
        table_id = str(table.get("table_id") or table.get("record_id") or "").strip()
        structure = table.get("structure") if isinstance(table.get("structure"), dict) else {}
        row_group = table.get("row_group") if isinstance(table.get("row_group"), dict) else {}
        visual_evidence = table.get("visual_evidence") if isinstance(table.get("visual_evidence"), list) else []
        # JSON を文字数で切ると key と値の途中で切れて検索の役に立たない。平文で列見出しと行の範囲を示す。
        headers = " / ".join(
            dict.fromkeys(
                str(item.get("text") or "").strip()
                for item in structure.get("column_headers") or []
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            )
        )
        labels = content_labels()
        parts = [f"{labels.text('table')} {table_id}" if table_id else labels.text("table")]
        if structure.get("caption"):
            parts.append(f"{labels.text('table_caption')} {structure['caption']}")
        if headers:
            parts.append(f"{labels.text('column_headers')} {headers}")
        if row_group.get("row_count"):
            parts.append(
                labels.text("row_range").format(
                    start=row_group.get("row_start"), end=row_group.get("row_end"), count=row_group["row_count"]
                )
            )
        # 表自身の切り出し画像は表の複製なので数えない。表内の Picture だけを示す。
        pictures = [
            str(item.get("image_id") or item.get("record_id") or "")
            for item in visual_evidence
            if isinstance(item, dict) and str(item.get("relationship") or "") != SOURCE_TABLE_CROP_RELATIONSHIP
        ]
        if pictures:
            parts.append(f"{labels.text('table_pictures')} " + ", ".join(p for p in pictures if p))
        pieces.append("。".join(parts))
    return " | ".join(pieces)


def _visual_search_text(child: DocumentChunk, *, body: str | None = None) -> str:
    """図の説明と画像 metadata を検索語にします。`body` は検索用本文（省略時は child.text）。"""
    pieces: list[str] = []
    refs = child.source_record_refs if isinstance(child.source_record_refs, list) else []
    # 検索用本文から OCR を外した child（Vision 成功時。#900）では、OCR record の text_preview も入れない。
    # ponytail: 本文に OCR ラベルが残っているかで判定する。1 child に Vision 成功と失敗の図が混在すると
    # 成功側の OCR も残るが、図の child は 1 図ずつ atomic なので実害はない。
    keep_ocr_refs = picture_ocr_text_label() in (child.text if body is None else body)
    for ref in refs:
        if str(ref.get("category") or "") != "Picture":
            continue
        if str(ref.get("raw_type") or "") == "picture_ocr_text" and not keep_ocr_refs:
            continue
        text = str(ref.get("text_preview") or "").strip()
        if text:
            pieces.append(text)
    images = child.metadata.get("image_evidence") if isinstance(child.metadata, dict) else []
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict):
                continue
            # JSON の key と ID は検索語にならない。画像の種類と役割を語として入れる。
            kind = str(image.get("visual_kind") or "").strip()
            role = str(image.get("visual_role") or "").strip()
            structure = image.get("visual_structure")
            words = [w for w in (kind, role) if w]
            if isinstance(structure, dict):
                for value in structure.values():
                    if isinstance(value, str) and value.strip():
                        words.append(value.strip())
                    elif isinstance(value, list):
                        # 表の見出し（["区分", "件数"]）と行（[["A", "10"]]）を語として並べる。
                        words.extend(" ".join(str(x) for x in (v if isinstance(v, list) else [v])) for v in value)
            elif isinstance(structure, str) and structure.strip():
                words.append(structure.strip())
            if words:
                pieces.append("画像 " + " ".join(words))
    return _context_preview(" | ".join(_ordered_nonempty(pieces)), 500)


def _form_search_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    fields: list[str] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        fields.append(
            " ".join(
                _ordered_nonempty(
                    [
                        item.get("key"),
                        item.get("value"),
                        item.get("selection_status"),
                        item.get("raw_type"),
                    ]
                )
            )
        )
    return " / ".join(_ordered_nonempty(fields))


def _formula_search_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return " / ".join(_ordered_nonempty(item.get("expression") for item in value[:5] if isinstance(item, dict)))


def _child_search_keywords(child: DocumentChunk, parent: DocumentChunk, *, body: str | None = None) -> str:
    """検索語の候補。`body` は検索用本文（省略時は child.text）で、親も検索文（OCR 除外済み）から取る (#900)。"""
    sources = [
        child.source_file_name,
        " ".join(child.metadata.get("section_path") or []),
        _classification_search_text(_metadata_classification(child.metadata)),
        child.text if body is None else body,
        parent.retrieval_text[:500],
    ]
    terms: list[str] = []
    for text in sources:
        for term in re.findall(r"[0-9A-Za-z_]{2,}|[ぁ-んァ-ン一-龯々ー]{2,}", str(text or "")):
            if len(term) > 60:
                continue
            terms.append(term)
    return " / ".join(_ordered_nonempty(terms)[:24])
