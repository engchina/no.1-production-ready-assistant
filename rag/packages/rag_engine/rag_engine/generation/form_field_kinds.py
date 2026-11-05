"""根拠の画面の図の項目の種類を、回答の材料に使う（#2404）。

取込の Vision は画面の図の項目（``form_fields``）に種類（``kind``: select / text / checkbox / radio / date /
button / unknown）を付け、chunk の metadata（``image_evidence[].visual_structure.form_fields``）に残す。
回答では、質問に名前が出た項目だけを根拠から拾い、項目名と種類を引用できる画像由来の根拠にし、対応する根拠の注記を
生成の材料へ渡す（表示の語は言語資源）。

照合は決定的（LLM を呼ばない）。項目名は文字の形（NFKC・大小・記号・空白）をそろえて、質問の中に含まれるかで比べる。
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rag_engine.generation.answer_payload import _metadata_image_evidence
from rag_engine.models.llm import form_field_kind
from rag_engine.parsing.content_labels import content_labels

# 質問の中に名前を探す項目名の最短の長さ（そろえた後の文字数）。1 字の項目名は質問の別の語に当たりやすい。
MIN_FIELD_NAME_CHARS = 2
# 生成の材料に出す観測の上限（同じ項目・種類・根拠は 1 つにまとめた後）。
MAX_NOTE_OBSERVATIONS = 12


@dataclass(frozen=True)
class FieldKindObservation:
    """根拠（parent の source_id）の画面の図で、質問に名前が出た項目とその種類。"""

    source_id: str
    name: str
    kind: str
    page: int | None = None


def normalized_name(text: str) -> str:
    """照合に使う文字の形（NFKC・小文字・文字と数字だけ）。"""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(char for char in folded if unicodedata.category(char)[0] in {"L", "N"})


def _record_form_fields_with_page(record: Any) -> list[tuple[str, str, int | None]]:
    """record の metadata の画面の図の項目（名前, 種類）。種類が unknown の項目と名前の無い項目は除く。"""
    metadata = getattr(record, "metadata", None)
    if not isinstance(metadata, dict):
        return []
    fields: list[tuple[str, str, int | None]] = []
    for image in _metadata_image_evidence(metadata):
        structure = image.get("visual_structure")
        items = structure.get("form_fields") if isinstance(structure, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            kind = form_field_kind(item.get("kind"))
            if name and kind != "unknown":
                page = image.get("page")
                if not isinstance(page, int) or isinstance(page, bool) or page <= 0:
                    start = getattr(record, "page", None)
                    end = getattr(record, "page_end", None) or start
                    page = start if isinstance(start, int) and not isinstance(start, bool) and start > 0 and start == end else None
                fields.append((name, kind, page))
    return fields


def record_form_fields(record: Any) -> list[tuple[str, str]]:
    """構造化解析の項目名と種類（所在は内部の観測へ引き継ぐ）。"""
    return [(name, kind) for name, kind, _page in _record_form_fields_with_page(record)]


def _source_id(record: Any) -> str:
    return str(getattr(record, "chunk_uid", "") or getattr(record, "id", "") or "")


def question_field_kinds(question: str, context: Any) -> list[FieldKindObservation]:
    """回答の context の根拠から、質問に名前が出た画面の図の項目と種類を、根拠の順に返す。

    child の metadata の項目は、生成に渡す根拠（parent）の source_id に帰属させる。
    """
    target = normalized_name(question)
    if not target:
        return []
    tree = tuple(getattr(context, "evidence_tree", ()) or ())
    groups: list[tuple[Any, list[Any]]] = (
        [(parent.record, [parent.record, *(child.record for child in parent.children)]) for parent in tree]
        if tree
        else [(record, [record]) for record in getattr(context, "records", ()) or ()]
    )
    seen: set[tuple[str, str, str, int | None]] = set()
    observations: list[FieldKindObservation] = []
    for owner, members in groups:
        source_id = _source_id(owner)
        for member in members:
            for name, kind, page in _record_form_fields_with_page(member):
                key = normalized_name(name)
                if len(key) < MIN_FIELD_NAME_CHARS or key not in target or (source_id, key, kind, page) in seen:
                    continue
                seen.add((source_id, key, kind, page))
                observations.append(FieldKindObservation(source_id, name, kind, page))
    return observations


def with_field_kind_evidence(observations: Sequence[FieldKindObservation], spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """取得済みの画像の構造化項目を、同じ出典・頁の独立した根拠にする。"""
    result = [dict(span) for span in spans]
    names = content_labels()
    seen: set[tuple[str, str, str, int | None]] = set()
    for item in observations:
        key = (item.source_id, item.name, item.kind, item.page)
        if key in seen:
            continue
        candidates = [span for span in spans if item.source_id in (span.get("source_id"), *(span.get("source_aliases") or ()))]
        if item.page is not None:
            candidates = [span for span in candidates if span.get("page") == item.page]
        # 図の頁が分からない複数頁の parent から所在を補わない。
        pages = {span.get("page") for span in candidates}
        word = names.form_field_kind(item.kind)
        if not candidates or len(pages) != 1 or not next(iter(pages)) or not word:
            continue
        source = next((span for span in candidates if normalized_name(item.name) in normalized_name(span.get("text", ""))), candidates[0])
        text = f"{item.name} = {word}"
        identity = hashlib.sha256("\0".join(map(str, key)).encode()).hexdigest()[:20]
        evidence = {
            key: source[key]
            for key in (
                "source_id",
                "source",
                "document_scope",
                "business_scope",
                "page",
                "page_end",
                "location",
                "section_path",
                "section_path_sources",
                "source_aliases",
                "value_context",
            )
            if key in source
        }
        evidence.update(
            evidence_id=f"Efield{identity}",
            text=text,
            origin="image_extraction",
            field_kind={"source_id": item.source_id, "name": item.name, "kind": item.kind, "page": item.page},
            excerpt_only=False,
        )
        result.append(evidence)
        seen.add(key)
        if len(seen) >= MAX_NOTE_OBSERVATIONS:
            break
    return result


def prompt_note(
    observations: Sequence[FieldKindObservation],
    spans: Sequence[Mapping[str, Any]],
) -> str:
    """生成の材料に入れる根拠の注記。生成に渡す根拠（spans）に無い出典の観測は出さない。"""
    labels: dict[str, str] = {}
    for span in spans:
        span_label = str(span.get("label") or "")
        for source_id in (span.get("source_id"), *(span.get("source_aliases") or ())):
            if source_id and span_label:
                labels.setdefault(str(source_id), span_label)
    field_labels = {
        (
            str(field.get("source_id") or span.get("source_id") or ""),
            str(field.get("name") or ""),
            str(field.get("kind") or ""),
            field.get("page"),
        ): str(span.get("label") or "")
        for span in spans
        if isinstance(field := span.get("field_kind"), Mapping)
    }
    names = content_labels()
    lines = []
    for item in observations:
        label = field_labels.get((item.source_id, item.name, item.kind, item.page)) or labels.get(item.source_id)
        word = names.form_field_kind(item.kind)
        if label and word:
            lines.append(f"- {label}: {item.name} = {word}")
    lines = list(dict.fromkeys(lines))[:MAX_NOTE_OBSERVATIONS]
    if not lines:
        return ""
    return "根拠の画面の図の項目の種類（質問に名前が出た項目）:\n" + "\n".join(lines)


def trace_payload(observations: Sequence[FieldKindObservation]) -> dict[str, Any]:
    """回答の記録に残す観測。"""
    return {
        "observations": [
            {"source_id": item.source_id, "name": item.name, "kind": item.kind, **({"page": item.page} if item.page is not None else {})}
            for item in observations
        ],
    }
