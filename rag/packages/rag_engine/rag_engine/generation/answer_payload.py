"""回答結果を保存・表示用の payload と根拠 item へ変換する。I/O は行わない。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from rag_engine.generation.answer_labels import answer_labels
from rag_engine.generation.answer_models import (
    ANSWER_USES,
    AnswerQuestionResult,
    AnswerRecord,
    AnswerResponse,
    _normalize_text,
    answer_flow_label,
    extract_original_question,
    merge_answer_use,
    query_strategy_label,
    retrieval_scope_label,
)
from rag_engine.parsing.picture_text import fold_picture_ocr_entries
from rag_engine.retrieval.context_builder import ContextParentEvidence, record_context_key


def _records_for_prompt_injection_scan(
    records: Sequence[AnswerRecord],
    evidence_tree: Sequence[ContextParentEvidence],
) -> list[AnswerRecord]:
    selected: list[AnswerRecord] = []
    seen: set[str] = set()

    def add(record: AnswerRecord) -> None:
        key = record_context_key(record) or str(getattr(record, "id", "") or "")
        if key and key in seen:
            return
        if key:
            seen.add(key)
        selected.append(record)

    for record in records:
        add(record)
    for parent in evidence_tree:
        add(parent.record)
        for child in parent.children:
            add(child.record)
    return selected


def format_answer_response(content: str) -> str:
    """回答本文を JSON 文字列形式の AnswerResponse に整形します。"""
    return _format_answer_response(parse_answer_response(content))


def parse_answer_response(content: str) -> AnswerResponse:
    """LLM の JSON または生 text 応答を AnswerResponse に変換します。"""
    raw = str(content or "").strip()
    payload = _json_response_object(raw)
    if not payload or "answer" not in payload:
        return _normalize_answer_response(AnswerResponse(answer_text=raw, raw_text=raw))

    cited_evidence = tuple(_cited_evidence_entries(payload.get("cited_evidence")))
    needs_review = payload.get("needs_human_review")
    return _normalize_answer_response(
        AnswerResponse(
            answer_text=str(payload.get("answer") or "").strip(),
            confidence=str(payload.get("confidence") or "").strip(),
            question_type=tuple(_string_list(payload.get("question_type"))),
            cited_evidence=cited_evidence,
            reasoning_summary=str(payload.get("reasoning_summary") or "").strip(),
            insufficient_reason=str(payload.get("insufficient_reason") or "").strip(),
            needs_human_review=needs_review if isinstance(needs_review, bool) else None,
            external_data_required=payload.get("external_data_required")
            if isinstance(payload.get("external_data_required"), bool)
            else None,
            external_data_items=tuple(_string_list(payload.get("external_data_items"))),
            raw_text=raw,
        )
    )


def _normalize_answer_response(response: AnswerResponse) -> AnswerResponse:
    """旧式の短い棄権を根拠不足の説明へ補正し、不足時の人手確認を保証する。

    完全一致した日本語・英語・中国語の定型句のみ置換する。部分回答や引用中の
    同じ語は保持し、元のモデル出力は raw_text に残す。意味的な根拠判定は行わない。
    """
    bare_answer = response.answer_text.strip().strip("。.!！?？ ").casefold()
    fallback = ""
    reason = ""
    if bare_answer in {"わかりません", "分かりません", "判りません", "不明です"}:
        fallback = answer_labels().insufficient_evidence_summary + answer_labels().abstention_guidance
        reason = "検索された資料では、質問への回答を裏付ける根拠が不足しています。"
    elif bare_answer in {"i don't know", "i don’t know", "i do not know"}:
        fallback = (
            "The retrieved documents do not provide sufficient evidence to answer this question. "
            "Consult the relevant operation manual to check the procedure and applicable conditions, or "
            "clarify the question or search scope."
        )
        reason = "The retrieved documents contain insufficient evidence for this question."
    elif bare_answer in {"不知道", "我不知道", "不清楚", "无法确定"}:
        fallback = (
            "检索到的资料缺乏足够的依据，无法回答此问题。请查看相关操作手册以确认操作方法和适用条件，并明确问题的对象、条件及检索范围。"
        )
        reason = "检索到的资料不足以支持对该问题的回答。"
    if response.external_data_items:
        response = replace(response, external_data_required=True, needs_human_review=True)
    if fallback:
        return replace(
            response,
            answer_text=fallback,
            confidence="low",
            insufficient_reason=response.insufficient_reason or reason,
            needs_human_review=True,
        )
    if (response.insufficient_reason or response.external_data_required) and response.needs_human_review is not True:
        return replace(response, needs_human_review=True)
    return response


def _format_answer_response(response: AnswerResponse) -> str:
    lines: list[str] = []
    if response.answer_text:
        lines.append(response.answer_text)

    meta_lines = []
    if response.confidence:
        meta_lines.append(f"信頼度: {response.confidence}")
    if response.question_type:
        meta_lines.append(f"問い合わせ型: {', '.join(response.question_type)}")
    if meta_lines:
        lines.append("\n".join(meta_lines))

    cited_evidence = _format_cited_evidence(response.cited_evidence)
    if cited_evidence:
        lines.append("引用証拠:\n" + cited_evidence)

    if response.reasoning_summary:
        lines.append(f"判断理由:\n{response.reasoning_summary}")

    if response.insufficient_reason:
        lines.append(f"不足情報:\n{response.insufficient_reason}")

    if response.needs_human_review is not None:
        lines.append(f"人手確認: {'必要' if response.needs_human_review else '不要'}")

    return "\n\n".join(lines).strip() or response.raw_text


def _published_reference_records(records: Sequence[AnswerRecord], trace: dict[str, Any] | None) -> list[AnswerRecord]:
    """公開引用だけを最終本文の根拠へ絞る。検索一覧・入力recordsは変更しない。"""
    trace = trace or {}
    finalization = trace.get("finalization", {})
    if not (finalization.get("filtered") or finalization.get("citations_synchronized")):
        return list(records)
    retained = set(finalization.get("retained_evidence_ids", []))
    selected: dict[str, list[str]] = {}
    pages: dict[str, set[int]] = {}
    for span in trace.get("selected_evidence", []):
        if span["evidence_id"] in retained:
            selected.setdefault(span["source_id"], []).append(span.get("text", ""))
            if span.get("page"):
                pages.setdefault(span["source_id"], set()).add(span["page"])
    # parent先頭は別項目の場合があるため、引用欄の抜粋も採用した原文から作る。
    return [
        replace(
            r,
            text="\n".join(selected[getattr(r, "chunk_uid", "") or r.id]),
            **(
                {
                    "page": next(iter(pages[r.chunk_uid or r.id])),
                    "page_end": next(iter(pages[r.chunk_uid or r.id])),
                    "source_seq_ranges": tuple(v for v in r.source_seq_ranges if v.get("page") in pages[r.chunk_uid or r.id]),
                }
                if finalization.get("citations_synchronized") and len(pages.get(r.chunk_uid or r.id, set())) == 1
                else {}
            ),
        )
        for r in records
        if (getattr(r, "chunk_uid", "") or r.id) in selected
    ]


def format_references(records: Sequence[AnswerRecord], limit: int = 5, *, include_source: bool = False) -> str:
    """最大limit件の参照を整形する。include_source時は資料名も明示する。"""
    lines = []
    for record in records[:limit]:
        active_status = " / active=true" if record.chunk_id else ""
        source = f"{record.source} / " if include_source and record.source else ""
        lines.append(f"- {source}{record.citation}{active_status}: {_trim_for_context(record.text, 90)}")
    return "\n".join(lines)


def _metadata_image_evidence(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    raw_images = metadata.get("image_evidence")
    if isinstance(raw_images, list):
        # 図の中の文字（OCR）の entry は持ち主の図にまとめる（同じ図を 2 回数えず、OCR だけの図も数える。#3455）。
        images.extend(fold_picture_ocr_entries([raw for raw in raw_images if isinstance(raw, dict)]))

    table_context = metadata.get("table_context")
    if not isinstance(table_context, list):
        return images
    for table in table_context:
        if not isinstance(table, dict):
            continue
        raw_visuals = table.get("visual_evidence")
        if not isinstance(raw_visuals, list):
            continue
        table_id = str(table.get("table_id") or table.get("record_id") or "")
        for raw in fold_picture_ocr_entries([raw for raw in raw_visuals if isinstance(raw, dict)]):
            image = dict(raw)
            image.setdefault("image_id", str(raw.get("record_id") or ""))
            image.setdefault("page", table.get("page"))
            image.setdefault("seq_no", raw.get("seq_no") or table.get("seq_no"))
            if not str(image.get("crop_path") or "").strip():
                image["crop_path"] = str(raw.get("vision_crop") or "")
            if not str(image.get("context_crop_path") or "").strip():
                image["context_crop_path"] = str(raw.get("vision_context_crop") or "")
            image.setdefault("relationship", str(raw.get("relationship") or "contained_in_table"))
            if table_id:
                image.setdefault("table_id", table_id)
            images.append(image)
    return images


def _json_response_object(content: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    fence_match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.I)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            payload = json.loads(cleaned, strict=False)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    return payload


def answer_result_payload(
    result: AnswerQuestionResult,
    *,
    answer_id: str,
    run_id: str,
    question: str = "",
    standard_answer: str = "",
) -> dict[str, Any]:
    """回答結果を保存・UI 表示用の JSON payload に変換します。"""
    return {
        "schema_version": 1,
        "answer_id": answer_id,
        "run_id": run_id,
        "question": extract_original_question(question) or result.original_question,
        "execution_steps": list(result.execution_steps),
        "question_display": result.question_display,
        "standard_answer": str(standard_answer or ""),
        "answer": result.answer,
        "answer_text": result.answer_text or result.answer,
        "confidence": result.confidence,
        "question_type": list(result.question_type),
        "cited_evidence_ids": list(result.cited_evidence_ids),
        "cited_evidence": list(result.cited_evidence),
        "image_evidence": list(result.image_evidence),
        "image_prompt_mode": result.image_prompt_mode,
        "evidence_facts": list(result.evidence_facts),
        "generation_trace": result.generation_trace,
        "task_contract": result.task_contract,
        # 回答の構造（AnswerEnvelope。#1235）。
        "envelope": result.envelope,
        "rejected_queries": list(result.rejected_queries),
        "primary_source_run_id": result.primary_source_run_id,
        "reasoning_summary": result.reasoning_summary,
        "insufficient_reason": result.insufficient_reason,
        "needs_human_review": result.needs_human_review,
        "external_data_required": result.external_data_required,
        "external_data_items": list(result.external_data_items),
        "answer_flow": result.answer_flow,
        "answer_flow_label": answer_flow_label(result.answer_flow),
        "retrieval_scope": result.retrieval_scope,
        "retrieval_scope_label": retrieval_scope_label(result.retrieval_scope),
        "classification_filter": dict(result.classification_filter),
        "selected_strategy": result.selected_strategy,
        "effective_strategy": result.effective_strategy,
        "selected_strategy_label": query_strategy_label(result.selected_strategy),
        "effective_strategy_label": query_strategy_label(result.effective_strategy),
        "generated_queries": list(result.generated_queries),
        "query_expansion_summary": _query_expansion_summary(result.generated_queries),
        "routing_reason": result.routing_reason,
        "routing_data_required": result.routing_data_required,
        "routing_data_items": list(result.routing_data_items),
        "question_plan": result.question_plan.to_payload() if result.question_plan is not None else {},
        "runtime_knowledge": result.runtime_knowledge.to_payload() if result.runtime_knowledge is not None else {},
        "query_understanding": result.inquiry_conditions.to_payload() if result.inquiry_conditions is not None else {},
        "inquiry_conditions": result.inquiry_conditions.to_payload() if result.inquiry_conditions is not None else {},
        "text_search_tokenizer": result.text_search_tokenizer,
        "text_search_tokenizer_label": result.text_search_tokenizer_label,
        "text_search_tokenizer_fingerprint": result.text_search_tokenizer_fingerprint,
        "text_search_tokens": list(result.text_search_tokens),
        "text_search_query": result.text_search_query,
        "text_search_queries": list(result.text_search_queries),
        "text_search_query_source": result.text_search_query_source,
        "text_search_tokenization_error": result.text_search_tokenization_error,
        "crag_attempts": list(result.crag_attempts),
        "prompt_injection_risk": result.prompt_injection_risk,
        "prompt_injection_warnings": list(result.prompt_injection_warnings),
        "rerank_scores": list(result.rerank_scores),
        "retrieval_query_plan": dict(result.retrieval_query_plan),
        "evidence_items": list(result.evidence_items),
    }


def _query_expansion_summary(generated_queries: Sequence[str]) -> str:
    count = len(generated_queries)
    return f"{count} 件の検索文を追加" if count else "検索文追加なし"


def answer_evidence_items(
    records: Sequence[AnswerRecord],
    cited_evidence_ids: Sequence[str] = (),
    *,
    evidence_tree: Sequence[ContextParentEvidence] = (),
    answer_uses: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """回答根拠 tree を viewer が選択・highlight できる item 列へ変換します。

    answer_uses は回答に使った箇所の ID（``_evidence_id_key``）ごとの使われ方（``_cited_evidence_uses``）。回答に使った
    item の ``answer_use`` に載せる（確かめた説明 ``confirmed`` / 参考 ``reference``。分からなければ None。#2040）。
    """
    used_ids = {_evidence_id_key(evidence_id) for evidence_id in cited_evidence_ids if _evidence_id_key(evidence_id)}
    usage_order = _model_usage_order(cited_evidence_ids)
    uses = dict(answer_uses or {})
    if evidence_tree:
        items: list[dict[str, Any]] = []
        for parent in evidence_tree:
            parent_item = _answer_record_evidence_item(parent.record, used_ids, usage_order, uses)
            child_items: list[dict[str, Any]] = []
            anchor_refs: list[dict[str, Any]] = []
            # 回答が親（まとまり）の ID だけで参照した（引用を支える子が決まらなかった）か（#2710）。
            parent_referenced = bool(used_ids & _answer_record_own_keys(parent.record))
            for child in parent.children:
                child_item = _answer_record_evidence_item(child.record, used_ids, usage_order, uses)
                child_item.update(
                    {
                        "retrieval_role": child.role,
                        "context_reason": child.reason,
                        "retrieval_rank": child.retrieval_rank,
                        # 子そのものは引用していないが、親を参照した（位置の決まらない参照の候補）。「回答で引用」とは
                        # 分ける（親の参照で兄弟の子すべてを引用にしない。#2710）。
                        "parent_model_used": parent_referenced and not child_item["is_model_used"],
                        "children": [],
                    }
                )
                child_items.append(child_item)
                if child.role == "retrieved_anchor":
                    anchor_refs.extend(child.record.source_record_refs)
            child_usage_ranks = [
                _finite_float(child.get("model_usage_rank"))
                for child in child_items
                if child.get("is_model_used") or _finite_float(child.get("model_usage_rank")) is not None
            ]
            parent_item.update(
                {
                    "retrieval_role": parent.role,
                    "context_reason": parent.reason,
                    "retrieval_rank": None,
                    "anchor_child_chunk_ids": list(parent.anchor_child_ids),
                    "anchor_source_record_refs": [ref for ref in (_evidence_source_ref(item) for item in anchor_refs) if ref],
                    "anchor_regions": _display_regions_from_refs(anchor_refs),
                    "parent_regions": parent_item.get("display_regions") or [],
                    "children": child_items,
                }
            )
            if child_usage_ranks:
                parent_item["is_model_used"] = True
                parent_item["model_usage_rank"] = int(min(child_usage_ranks))
                for child_item in child_items:
                    parent_item["answer_use"] = merge_answer_use(parent_item.get("answer_use"), child_item.get("answer_use"))
            items.append(parent_item)
        return items

    items: list[dict[str, Any]] = []
    for record in records:
        item = _answer_record_evidence_item(record, used_ids, usage_order, uses)
        item["children"] = []
        items.append(item)
    return [item for _, item in sorted(enumerate(items), key=lambda pair: _evidence_item_sort_key(pair, rerank_first=True))]


def _answer_record_evidence_item(
    record: AnswerRecord,
    used_ids: set[str],
    usage_order: dict[str, int],
    answer_uses: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    hybrid_metadata = record.metadata.get("adb_hybrid") if isinstance(record.metadata, dict) else {}
    if not isinstance(hybrid_metadata, dict):
        hybrid_metadata = {}
    source_refs = [_evidence_source_ref(ref) for ref in record.source_record_refs]
    source_refs = [ref for ref in source_refs if ref]
    if not source_refs:
        source_refs = [_source_ref_from_answer_record(record)]
    item_id = record.chunk_id or record.id
    identity_keys = _answer_record_identity_keys(record, item_id)
    model_usage_rank = _model_usage_rank(identity_keys, usage_order)
    model_used = bool(used_ids & identity_keys)
    answer_use: str | None = None
    if model_used:
        for key in identity_keys:
            answer_use = merge_answer_use(answer_use, (answer_uses or {}).get(key))
    rerank_metadata = _record_rerank_metadata(record)
    return {
        "id": item_id,
        "kind": "chunk" if record.chunk_id else "record",
        "record_id": record.id,
        "engine": record.engine,
        "engine_label": record.engine_label,
        "category": record.category,
        "citation": record.citation,
        "source": record.source,
        "source_run_id": record.source_run_id,
        "chunk_uid": record.chunk_uid,
        "chunk_id": record.chunk_id,
        "chunk_level": record.chunk_level,
        "chunk_seq": record.chunk_seq,
        "parent_chunk_uid": record.parent_chunk_uid,
        "parent_chunk_id": record.parent_chunk_id,
        "child_chunk_ids": list(record.child_chunk_ids),
        "page_start": record.page,
        "page_end": record.page_end or record.page,
        "source_seq_ranges": [_evidence_seq_range(item) for item in record.source_seq_ranges],
        "source_record_refs": source_refs,
        "display_regions": _display_regions_for_record(record, source_refs),
        "text_preview": _preview_lines(record.text),
        # 採点に使う根拠は UI 用の280文字 preview で切り詰めない。
        "text": record.body_text or record.text,
        "is_model_used": model_used,
        "model_usage_rank": model_usage_rank,
        # 回答での使われ方（confirmed / reference。分からなければ None。#2040）。
        "answer_use": answer_use,
        "hybrid_score": hybrid_metadata.get("rrf_score"),
        "vector_rank": hybrid_metadata.get("vector_rank"),
        "text_rank": hybrid_metadata.get("text_rank"),
        "profile_rank": hybrid_metadata.get("profile_rank"),
        "vector_distance": hybrid_metadata.get("vector_distance"),
        "text_score": hybrid_metadata.get("text_score"),
        "profile_score": hybrid_metadata.get("profile_score"),
        "rerank_rank": rerank_metadata.get("rank"),
        "rerank_score": rerank_metadata.get("relevance_score"),
        "retrieval_channels": list(hybrid_metadata.get("retrieval_channels") or []),
        "channel_ranks": dict(hybrid_metadata.get("channel_ranks") or {}),
        "channel_scores": dict(hybrid_metadata.get("channel_scores") or {}),
    }


def _record_rerank_metadata(record: AnswerRecord) -> dict[str, Any]:
    metadata = record.metadata.get("rerank") if isinstance(record.metadata, dict) else {}
    return metadata if isinstance(metadata, dict) else {}


def _answer_record_own_keys(record: AnswerRecord) -> set[str]:
    """record そのものの ID（子・親の ID は含めない。#2710）。"""
    values = [record.id, record.chunk_id, record.chunk_uid]
    return {_evidence_id_key(value) for value in values if _evidence_id_key(value)}


def _answer_record_identity_keys(record: AnswerRecord, item_id: str) -> set[str]:
    """回答に使った ID と照らす record の ID。

    子の chunk には親の ID（``parent_chunk_uid``）を含めない。含めると、親の ID で参照した回答で同じ親の兄弟の
    子がすべて「回答で引用」になり、主張を支える子を見分けられない（#2710）。親は子の ID を含める（子を引用
    すれば、その親のまとまりも回答に使った）。
    """
    values: list[str] = [
        record.id,
        record.chunk_id,
        record.chunk_uid,
        item_id,
        record.citation,
        *record.child_chunk_ids,
    ]
    values.extend(str(ref.get("record_id") or "") for ref in record.source_record_refs if isinstance(ref, dict))
    # 行画像は親 Table と異なる ID でモデルに渡るため、所有 chunk の利用判定に含めます。
    values.extend(str(image.get("image_id") or image.get("record_id") or "") for image in _metadata_image_evidence(record.metadata or {}))
    return {_evidence_id_key(value) for value in values if _evidence_id_key(value)}


def _display_regions_for_record(
    record: AnswerRecord,
    source_refs: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    display_regions = _display_regions_from_metadata(metadata)
    if display_regions:
        return display_regions
    return _display_regions_from_refs(source_refs)


def _display_regions_from_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    layout = metadata.get("layout") if isinstance(metadata.get("layout"), dict) else {}
    raw = layout.get("display_regions")
    return _normalize_display_regions(raw)


def _display_regions_from_refs(refs: Sequence[Any]) -> list[dict[str, Any]]:
    by_page: dict[int, list[dict[str, Any]]] = {}
    for raw_ref in refs:
        ref = _evidence_source_ref(raw_ref)
        bbox = _bbox_values(ref.get("bbox"))
        page = _int_value(ref.get("page")) or 0
        if page <= 0 or not bbox:
            continue
        box = {
            "record_id": str(ref.get("record_id") or ""),
            "seq_no": _int_value(ref.get("seq_no")) or 0,
            "category": str(ref.get("category") or ""),
            "raw_type": str(ref.get("raw_type") or ""),
            "bbox": bbox,
        }
        by_page.setdefault(page, []).append(box)
    return [
        {
            "page": page,
            "boxes": sorted(
                boxes,
                key=lambda box: (int(box.get("seq_no") or 0), str(box.get("record_id") or "")),
            ),
        }
        for page, boxes in sorted(by_page.items())
    ]


def _normalize_display_regions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    regions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        page = _int_value(item.get("page")) or 0
        boxes = [box for box in (_normalize_display_box(raw_box) for raw_box in item.get("boxes") or []) if box]
        if page > 0 and boxes:
            regions.append({"page": page, "boxes": boxes})
    return regions


def _normalize_display_box(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    bbox = _bbox_values(raw.get("bbox"))
    if not bbox:
        return {}
    return {
        "record_id": str(raw.get("record_id") or ""),
        "seq_no": _int_value(raw.get("seq_no")) or 0,
        "category": str(raw.get("category") or ""),
        "raw_type": str(raw.get("raw_type") or ""),
        "bbox": bbox,
        "text_preview": str(raw.get("text_preview") or ""),
        "visual_role": str(raw.get("visual_role") or ""),
    }


def _model_usage_order(cited_evidence_ids: Sequence[str]) -> dict[str, int]:
    usage_order: dict[str, int] = {}
    for evidence_id in cited_evidence_ids:
        key = _evidence_id_key(evidence_id)
        if key and key not in usage_order:
            usage_order[key] = len(usage_order) + 1
    return usage_order


def _model_usage_rank(identity_keys: set[str], usage_order: dict[str, int]) -> int | None:
    ranks = [usage_order[key] for key in identity_keys if key in usage_order]
    return min(ranks) if ranks else None


def _evidence_item_sort_key(index_and_item: tuple[int, dict[str, Any]], *, rerank_first: bool = True) -> tuple[Any, ...]:
    source_index, item = index_and_item
    model_used = bool(item.get("is_model_used")) or _finite_float(item.get("model_usage_rank")) is not None

    return (
        0 if model_used else 1,
        _ascending_number_sort_key(item.get("model_usage_rank")) if model_used else (1, 0.0),
        # rerank の点数を主キーにし、RRF・経路の点数・語の一致は同点の順と rerank の無い根拠の順だけ（#3504）。
        _descending_number_sort_key(item.get("rerank_score") if rerank_first else None),
        _descending_number_sort_key(item.get("hybrid_score")),
        _descending_number_sort_key(_best_mapping_number(item.get("channel_scores"))),
        _descending_number_sort_key(item.get("profile_score")),
        _descending_number_sort_key(item.get("text_score")),
        _ascending_number_sort_key(_best_evidence_rank(item)),
        _ascending_number_sort_key(item.get("vector_distance")),
        source_index,
    )


def _descending_number_sort_key(value: Any) -> tuple[int, float]:
    number = _finite_float(value)
    return (0, -number) if number is not None else (1, 0.0)


def _ascending_number_sort_key(value: Any) -> tuple[int, float]:
    number = _finite_float(value)
    return (0, number) if number is not None else (1, 0.0)


def _best_mapping_number(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    numbers = [_finite_float(item) for item in value.values()]
    numbers = [number for number in numbers if number is not None]
    return max(numbers) if numbers else None


def _best_evidence_rank(item: dict[str, Any]) -> float | None:
    ranks = [_finite_float(item.get(key)) for key in ("vector_rank", "text_rank", "profile_rank")]
    channel_ranks = item.get("channel_ranks")
    if isinstance(channel_ranks, dict):
        ranks.extend(_finite_float(value) for value in channel_ranks.values())
    ranks = [rank for rank in ranks if rank is not None and rank > 0]
    return min(ranks) if ranks else None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _rerank_scores_from_evidence(
    records: Sequence[AnswerRecord],
    evidence_tree: Sequence[ContextParentEvidence],
) -> tuple[dict[str, Any], ...]:
    scores: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in _records_for_prompt_injection_scan(records, evidence_tree):
        metadata = _record_rerank_metadata(record)
        if not metadata:
            continue
        item_id = record.chunk_id or record.id
        key = _evidence_id_key(record.chunk_uid) or _evidence_id_key(item_id)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        scores.append(
            {
                "id": item_id,
                "chunk_uid": record.chunk_uid,
                "candidate_index": metadata.get("candidate_index"),
                "rank": metadata.get("rank"),
                "relevance_score": metadata.get("relevance_score"),
                "question_relevance_score": metadata.get("question_relevance_score"),
                "min_relevance_score": metadata.get("min_relevance_score"),
                "filtered": bool(metadata.get("filtered")),
                "skipped": bool(metadata.get("skipped")),
                "skip_reason": str(metadata.get("skip_reason") or ""),
            }
        )
    return tuple(scores)


def _cited_evidence_entries(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    entries: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entry = {
            "evidence_id": str(item.get("evidence_id") or "").strip(),
            "source": str(item.get("source") or "").strip(),
            "source_run_id": str(item.get("source_run_id") or "").strip(),
            "page": str(item.get("page") or "").strip(),
            "look_at": str(item.get("look_at") or "").strip(),
            "visible_evidence": str(item.get("visible_evidence") or "").strip(),
            "crop_path": str(item.get("crop_path") or "").strip(),
        }
        # 回答での使われ方（#2040）。決まった値だけを残す（モデルの出力の欄ではない）。
        if (answer_use := str(item.get("answer_use") or "").strip()) in ANSWER_USES:
            entry["answer_use"] = answer_use
        if any(entry.values()):
            entries.append(entry)
    return entries


def _primary_source_run_id(records: Sequence[AnswerRecord]) -> str:
    for record in records:
        if record.source_run_id:
            return record.source_run_id
    return ""


def _cited_evidence_ids(entries: Sequence[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        evidence_id = str(entry.get("evidence_id") or entry.get("id") or "").strip()
        key = _evidence_id_key(evidence_id)
        if not key or key in seen:
            continue
        ids.append(evidence_id)
        seen.add(key)
    return ids


def _cited_evidence_uses(entries: Sequence[dict[str, Any]]) -> dict[str, str]:
    """回答に使った箇所の ID（``_evidence_id_key``）ごとの使われ方（#2040）。同じ箇所は confirmed を優先する。"""
    uses: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = _evidence_id_key(str(entry.get("evidence_id") or entry.get("id") or "").strip())
        use = str(entry.get("answer_use") or "")
        if key and use in ANSWER_USES:
            uses[key] = merge_answer_use(uses.get(key), use) or use
    return uses


def _format_cited_evidence(value: Any) -> str:
    entries = _cited_evidence_entries(value) if not isinstance(value, tuple) else list(value)
    lines = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        source = str(item.get("source") or "").strip()
        page = str(item.get("page") or "").strip()
        look_at = str(item.get("look_at") or "").strip()
        evidence = str(item.get("visible_evidence") or "").strip()
        parts = []
        if evidence_id:
            parts.append(evidence_id)
        if source:
            parts.append(source)
        if page:
            parts.append(f"p.{page}")
        if look_at:
            parts.append(look_at)
        header = " / ".join(parts) or "証拠"
        if evidence:
            lines.append(f"- {header}: {evidence}")
        else:
            lines.append(f"- {header}")
    return "\n".join(lines)


def _evidence_id_key(value: Any) -> str:
    return _normalize_text(str(value or "").strip())


def _evidence_source_ref(ref: Any) -> dict[str, Any]:
    if not isinstance(ref, dict):
        return {}
    return {
        "record_id": str(ref.get("record_id") or ref.get("id") or ""),
        "page": _int_value(ref.get("page")) or 0,
        "seq_no": _int_value(ref.get("seq_no")) or 0,
        "category": str(ref.get("category") or ""),
        "raw_type": str(ref.get("raw_type") or ""),
        "bbox": _bbox_values(ref.get("bbox")),
        "vision_crop": str(ref.get("vision_crop") or ""),
        "vision_context_crop": str(ref.get("vision_context_crop") or ""),
    }


def _source_ref_from_answer_record(record: AnswerRecord) -> dict[str, Any]:
    return {
        "record_id": record.id,
        "page": record.page,
        "seq_no": record.seq_no,
        "category": record.category,
        "raw_type": "",
        "bbox": [],
    }


def _evidence_seq_range(item: Any) -> dict[str, int]:
    if not isinstance(item, dict):
        return {"page": 0, "seq_start": 0, "seq_end": 0}
    page = _int_value(item.get("page")) or 0
    start = _int_value(item.get("seq_start")) or 0
    end = _int_value(item.get("seq_end")) or start
    return {"page": page, "seq_start": start, "seq_end": end}


def _bbox_values(value: Any) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        return []
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return []


def _preview_lines(value: str, max_length: int = 280) -> str:
    lines = [line.strip() for line in str(value or "").replace("\r\n", "\n").split("\n") if line.strip()]
    preview = "\n".join(lines[:2]) if lines else _trim_for_context(value, max_length)
    if len(preview) <= max_length:
        return preview
    return f"{preview[: max(0, max_length - 1)]}…"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _trim_for_context(value: str, max_length: int) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max(0, max_length - 1)]}…"
