"""要求の回答漏れと材料不足を分け、既存の補救経路を選ぶ。"""

from collections.abc import Collection, Mapping, Sequence
from typing import Any


def request_recovery_plan(
    reviews: Sequence[Mapping[str, Any]],
    gap_ids: Sequence[str],
    evidence_ids: set[str],
    *,
    blocked_request_ids: Sequence[str] = (),
    expected_request_ids: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """実行済み監査の材料判定、または生成の明示不足だけから補救を選ぶ。"""
    by_id = {
        str(row["request_id"]): row
        for row in reviews
        if (row.get("reviewed") and row.get("status") in {"missing", "partial"}) or row.get("status") == "unknown"
    }
    result = []
    for request_id in dict.fromkeys([*by_id, *gap_ids, *blocked_request_ids]):
        row = by_id.get(request_id, {})
        status = row.get("evidence_status", "unknown")
        refs = list(dict.fromkeys(row.get("evidence_ids") or ()))
        invalid_id = expected_request_ids is not None and request_id not in expected_request_ids
        if invalid_id:
            cause, action = "invalid_request_id", "retain"
        elif request_id in blocked_request_ids or row.get("status") == "unknown":
            cause, action = "review_incomplete", "retain"
        elif status == "available":
            valid = bool(refs) and set(refs) <= evidence_ids
            cause, action = ("answer_omission", "correction") if valid else ("unverified_evidence", "retain")
        elif status == "missing" or (request_id in gap_ids and not row):
            cause, action = "evidence_missing", "retrieval"
        else:
            cause, action = "unverified_evidence", "retain"
        result.append(
            {
                "request_id": request_id,
                "status": "unknown" if invalid_id or request_id in blocked_request_ids else row.get("status", "missing"),
                "cause": cause,
                "action": action,
                "evidence_ids": refs if action == "correction" else [],
            }
        )
    return result


def relabel_recovery_reviews(reviews: Sequence[Mapping[str, Any]], spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """補写材料の安定IDだけを現在の表示IDへ写す。引用の意味支持は再判定しない。

    監査・補救の判断は安定IDを使うが、次の生成が引用するIDは現在の表示IDである。
    同じ文字列の古い表示IDや、本文が似ている別資料へ結び直さない。
    """
    labels: dict[str, list[str]] = {}
    label_counts: dict[str, int] = {}
    for span in spans:
        key, label = str(span.get("evidence_id", "")), str(span.get("label", ""))
        if key and label:
            labels.setdefault(key, []).append(label)
            label_counts[label] = label_counts.get(label, 0) + 1
    bound = {key: values[0] for key, values in labels.items() if len(values) == 1 and label_counts[values[0]] == 1}
    result = []
    for original in reviews:
        row = dict(original)
        refs = list(row.get("evidence_ids") or ())
        support = list(row.get("evidence_support") or ())
        support_ids = [ref.get("evidence_id") for ref in support if isinstance(ref, Mapping)]
        needed = [*refs, *support_ids]
        valid = len(support_ids) == len(support) and all(isinstance(key, str) and key in bound for key in needed)
        if row.get("evidence_status") == "available":
            valid = valid and bool(refs) and bool(support) and set(refs) == set(support_ids)
        if valid:
            row["evidence_ids"] = list(dict.fromkeys(bound[key] for key in refs))
            row["evidence_support"] = [{**ref, "evidence_id": bound[ref["evidence_id"]]} for ref in support]
        else:
            row["evidence_status"] = "unknown"
            row["evidence_ids"] = []
            row["evidence_support"] = []
            row["evidence_binding"] = "unavailable"
        result.append(row)
    return result
