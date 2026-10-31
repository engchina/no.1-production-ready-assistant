"""精排入力の予算と、同一成果物の同じ原文の重複除去。関連度は再判定しない。"""

from collections.abc import Mapping, Sequence
from typing import Any


def record_id(record: Any) -> str:
    return str(record.chunk_uid or record.id)


def artifact_key(record: Any) -> tuple[str, ...]:
    metadata = record.metadata or {}
    return (
        str(metadata.get("document_id") or record.source),
        str(record.source_run_id or ""),
        str(metadata.get("recipe_id") or ""),
        str(metadata.get("chunk_set_id") or ""),
        str(metadata.get("artifact_id") or ""),
    )


def select_candidates(
    records: Sequence[Any], limit: int, *, stage_limits: Mapping[str, int] | None = None
) -> tuple[list[Any], dict[str, Any]]:
    """既存の順位を保ち、同じ成果物の同文だけを統合してから合計上限を適用する。

    別文書・版・レシピの同文は統合しない。範囲の異なる部分一致も、同じ事実との推測では消さない。
    """
    seen_ids: set[tuple[Any, ...]] = set()
    originals: dict[tuple[Any, ...], str] = {}
    unique: list[Any] = []
    excluded: list[dict[str, str]] = []
    for record in records:
        uid = record_id(record)
        text = str(record.text or "").strip()
        refs = tuple(sorted({str(ref.get("record_id") or "") for ref in record.source_record_refs if ref.get("record_id")}))
        artifact = artifact_key(record)
        # 同文でも別ページの繰り返しは同じ根拠ではない。成果物と抽出元が分からない場合は ID だけで比較する。
        source = refs if refs and (record.source_run_id or record.metadata.get("chunk_set_id")) else (uid,)
        key = (*artifact, source, text)
        identity = (*artifact, uid)
        if identity in seen_ids or (text and key in originals):
            excluded.append({"id": uid, "reason": "duplicate_original", "retained_id": originals.get(key, uid)})
            continue
        seen_ids.add(identity)
        if text:
            originals[key] = uid
        unique.append(record)
    limits = dict(stage_limits if stage_limits is not None else {"fusion": 150})
    effective_limit = max(1, limit) if limit > 0 else sum(limits.values())
    selected: list[Any] = []
    admitted: dict[str, int] = {}
    for record in unique:
        stage = str(record.metadata.get("retrieval_admission_stage") or "fusion")
        if admitted.get(stage, 0) >= limits.get(stage, 0):
            excluded.append({"id": record_id(record), "reason": "stage_admission_budget", "stage": stage})
        elif len(selected) >= effective_limit:
            excluded.append({"id": record_id(record), "reason": "rerank_input_budget"})
        else:
            selected.append(record)
            admitted[stage] = admitted.get(stage, 0) + 1
    return selected, {
        "input_count": len(records),
        "configured_limit": limit,
        "limit": effective_limit,
        "limit_policy": "total_cap" if limit > 0 else "sum_of_stage_limits",
        "stage_limits": limits,
        "admitted": admitted,
        "selected_ids": [record_id(record) for record in selected],
        "excluded": excluded,
    }


def stage_limits(settings: Any, records: Sequence[Any]) -> dict[str, int]:
    """既存各段の上限の和。付加された件数を最適な予算と見なさない。"""
    limits = dict(getattr(settings, "retrieval_stage_limits", {}) or {})
    limits.setdefault("fusion", max(1, int(getattr(settings, "retrieval_candidate_limit", 150))))
    if getattr(settings, "screen_linking_enabled", False):
        from rag_engine.retrieval.screen_catalog import MAX_CHILDREN_PER_SCREEN, MAX_LINKED_SCREENS

        limits["screen"] = MAX_CHILDREN_PER_SCREEN * MAX_LINKED_SCREENS
    if getattr(settings, "article_catalog_enabled", False):
        documents = int(getattr(settings, "article_catalog_max_documents", 16)) + int(
            getattr(settings, "article_catalog_scope_documents", 2)
        )
        articles = int(getattr(settings, "article_catalog_max_articles", 8)) + int(getattr(settings, "article_catalog_refine_articles", 6))
        limits["article"] = max(0, documents * articles)
    # 添付の読取件数は呼出元の既存制約で確定している。今回渡された明示入力だけに枠を割り当てる。
    limits["attachment"] = sum(record.metadata.get("retrieval_admission_stage") == "attachment" for record in records)
    return limits
