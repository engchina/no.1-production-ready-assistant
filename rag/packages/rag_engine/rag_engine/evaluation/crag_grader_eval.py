"""CRAG 評価器（`_grade_crag_retrieval`）を生成を通さずに単体評価する (#984)。

ラベル集は質問ごとに、検索入力（run_id・retrieval_queries・大分類）と、候補（parent chunk）ごとの
`relevant`（true / false / null=未判定）、質問の `answerable`（資料に答えがあるか。null=未判定）を持つ。
評価は候補単位の precision / recall / F1 と、質問単位の誤拒（answerable なのに不足）・誤受理（answerable
でないのに十分）を数える。ラベルは実データなのでリポジトリ外に置き、ここには集計と草稿の組み立てだけを置く。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

LABEL_SCHEMA_VERSION = 1


def load_labels(path: str | Path) -> dict[str, Any]:
    """ラベル集を読み、最低限の形（questions の id / question / run_id / candidates）を検証する。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    questions = data.get("questions")
    if not isinstance(questions, list):
        raise ValueError("labels file must have a 'questions' list")
    for item in questions:
        for key in ("id", "question", "run_id", "candidates"):
            if key not in item:
                raise ValueError(f"question entry lacks '{key}': {item.get('id', '?')}")
        for candidate in item["candidates"]:
            if "chunk_uid" not in candidate:
                raise ValueError(f"candidate lacks 'chunk_uid' in question {item['id']}")
            if candidate.get("relevant") not in (True, False, None):
                raise ValueError(f"candidate.relevant must be true / false / null in question {item['id']}")
        if item.get("answerable") not in (True, False, None):
            raise ValueError(f"answerable must be true / false / null in question {item['id']}")
    return data


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def candidate_key(candidate: dict[str, Any]) -> tuple[str, int, str]:
    """chunk_uid が違っても同じ候補とみなす照合キー（文書名・頁・機能見出し）。

    検索は実行ごとに同じ文書の別 chunk run（uid の run 接頭辞が異なる）を返すことがあるため、ラベルと
    評価時の候補は chunk_uid → このキーの順で突き合わせる (#995)。
    """
    section = " ".join(str(candidate.get("section", "") or "").split())
    return (str(candidate.get("source", "") or ""), int(candidate.get("page") or 0), section)


def match_candidates(labeled: Sequence[dict[str, Any]], presented: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """評価時に提示された候補の chunk_uid → ラベル候補。chunk_uid で一致しなければ candidate_key で照合する。"""
    by_uid = {c["chunk_uid"]: c for c in labeled}
    by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    for c in labeled:
        by_key.setdefault(candidate_key(c), c)
    matched = {}
    for candidate in presented:
        uid = str(candidate.get("chunk_uid") or "")
        label = by_uid.get(uid) or by_key.get(candidate_key(candidate))
        if label is not None:
            matched[uid] = label
    return matched


def summarize_grader_runs(labels: dict[str, Any], runs: dict[str, Sequence[dict[str, Any]]]) -> dict[str, Any]:
    """ラベルと評価器の attempt payload（`CragRetrievalAttempt.to_payload()`）を突き合わせて集計する。

    候補単位: 各 attempt の `candidates`（評価時に提示された候補。`collect_runs` が付ける）のうちラベルが
    true / false のものだけを数える。提示された候補は chunk_uid → (source, page, section) の順でラベルと照合し、
    ラベルにあるが今回提示されなかった候補は `absent` として数え miss にしない（評価器は見ていない候補を
    判定できない）。評価器が verdict を返さなかった提示済み候補は relevant=false とみなす。attempt に
    `candidates` が無い旧形式は、ラベルの全候補が提示されたものとして扱う。
    質問単位: answerable が null の質問は誤拒・誤受理に数えない。
    """
    tp = fp = fn = tn = absent_total = unlabeled_total = 0
    false_refusals = false_accepts = graded = 0
    rows: list[dict[str, Any]] = []
    for question in labels["questions"]:
        labeled = [c for c in question["candidates"] if c.get("relevant") is not None]
        attempts = list(runs.get(question["id"], ()))
        sufficient_count = 0
        hit = miss = extra = absent = unlabeled = 0
        for attempt in attempts:
            graded += 1
            presented = attempt.get("candidates")
            if presented is None:
                presented = [{"chunk_uid": c["chunk_uid"], **{k: c.get(k) for k in ("source", "page", "section")}} for c in labeled]
            matched = match_candidates(labeled, presented)
            unlabeled += len(presented) - len(matched)
            shown = {id(label) for label in matched.values()}
            absent += sum(1 for c in labeled if id(c) not in shown)
            verdicts = {v.get("chunk_uid"): bool(v.get("relevant")) for v in attempt.get("candidate_verdicts") or []}
            truth = {uid: bool(label["relevant"]) for uid, label in matched.items()}
            for uid, relevant in truth.items():
                predicted = verdicts.get(uid, False)
                if relevant and predicted:
                    tp += 1
                    hit += 1
                elif relevant and not predicted:
                    fn += 1
                    miss += 1
                elif not relevant and predicted:
                    fp += 1
                    extra += 1
                else:
                    tn += 1
            sufficient = bool(attempt.get("sufficient"))
            sufficient_count += sufficient
            if question.get("answerable") is True and not sufficient:
                false_refusals += 1
            if question.get("answerable") is False and sufficient:
                false_accepts += 1
        absent_total += absent
        unlabeled_total += unlabeled
        rows.append(
            {
                "id": question["id"],
                "answerable": question.get("answerable"),
                "labeled": len(labeled),
                "relevant_labels": sum(1 for c in labeled if c["relevant"]),
                "attempts": len(attempts),
                "sufficient": sufficient_count,
                "hit": hit,
                "miss": miss,
                "extra": extra,
                "absent": absent,
                "unlabeled": unlabeled,
            }
        )
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = (
        (2 * precision * recall / (precision + recall))
        if precision and recall
        else (0.0 if precision is not None and recall is not None else None)
    )
    return {
        "graded_attempts": graded,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_refusals": false_refusals,
        "false_accepts": false_accepts,
        "absent": absent_total,
        "unlabeled": unlabeled_total,
        "answerable_attempts": sum(r["attempts"] for r in rows if r["answerable"] is True),
        "unanswerable_attempts": sum(r["attempts"] for r in rows if r["answerable"] is False),
        "questions": rows,
    }


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def format_grader_summary(summary: dict[str, Any]) -> str:
    """集計を Markdown（全体 1 行 + 質問ごとの表）にする。PR の検証結果へそのまま貼れる形。"""
    lines = [
        "| 評価回数 | precision | recall | F1 | 誤拒（answerable なのに不足） | 誤受理（answerable でないのに十分） |",
        "|---|---|---|---|---|---|",
        f"| {summary['graded_attempts']} | {_fmt(summary['precision'])} ({summary['tp']}/{summary['tp'] + summary['fp']}) "
        f"| {_fmt(summary['recall'])} ({summary['tp']}/{summary['tp'] + summary['fn']}) | {_fmt(summary['f1'])} "
        f"| {summary['false_refusals']}/{summary['answerable_attempts']} | {summary['false_accepts']}/{summary['unanswerable_attempts']} |",
        "",
        f"ラベルにあるが提示されなかった候補（absent）: {summary['absent']} / "
        f"提示されたがラベルに無い候補（unlabeled）: {summary['unlabeled']}",
        "",
        "| id | answerable | ラベル数（relevant） | 評価回数 | 十分 | hit | miss | extra | absent | unlabeled |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary["questions"]:
        answerable = {True: "true", False: "false"}.get(row["answerable"], "—")
        lines.append(
            f"| {row['id']} | {answerable} | {row['labeled']} ({row['relevant_labels']}) | {row['attempts']} "
            f"| {row['sufficient']} | {row['hit']} | {row['miss']} | {row['extra']} | {row['absent']} | {row['unlabeled']} |"
        )
    return "\n".join(lines)


def draft_question_labels(
    question_input: dict[str, Any], candidates: Sequence[dict[str, Any]], attempt: dict[str, Any], *, excerpt_chars: int = 200
) -> dict[str, Any]:
    """評価器 1 回の verdict を草稿ラベルにする。人が `relevant` / `answerable` を見直す前提の下書き。

    candidates は `_crag_grade_candidates` の出力（chunk_uid / source / page / section / text）。verdict の無い候補は
    relevant=null にし、見直しで埋める。
    """
    verdicts = {v.get("chunk_uid"): v for v in attempt.get("candidate_verdicts") or []}
    drafted = []
    for candidate in candidates:
        uid = candidate.get("chunk_uid") or ""
        verdict = verdicts.get(uid)
        drafted.append(
            {
                "chunk_uid": uid,
                "source": candidate.get("source", ""),
                "page": candidate.get("page", 0),
                "section": candidate.get("section", ""),
                "excerpt": str(candidate.get("text", ""))[:excerpt_chars],
                "relevant": bool(verdict["relevant"]) if verdict else None,
                "note": str(verdict.get("reason", "")) if verdict else "",
            }
        )
    return {
        **{k: question_input[k] for k in ("id", "question", "run_id", "retrieval_queries", "large_category") if k in question_input},
        "answerable": None,
        "draft_sufficient": bool(attempt.get("sufficient")),
        "candidates": drafted,
    }


def format_review_sheet(labels: dict[str, Any]) -> str:
    """見直し用の Markdown。候補ごとに評価器の下書きと空欄（人の判定）を並べる。"""
    lines = []
    for question in labels["questions"]:
        lines += [
            f"## {question['id']}",
            "",
            f"質問: {question['question']}",
            "",
            f"answerable（資料に答えがあるか。true / false を記入）: {question.get('answerable')}",
            f"評価器の下書き sufficient: {question.get('draft_sufficient')}",
            "",
            "| # | chunk_uid | 出典 / 頁 / 見出し | 抜粋 | 下書き relevant | 人の判定 | 理由 |",
            "|---|---|---|---|---|---|---|",
        ]
        for number, candidate in enumerate(question["candidates"], 1):
            excerpt = " ".join(str(candidate.get("excerpt", "")).split())[:120]
            lines.append(
                f"| {number} | `{candidate['chunk_uid']}` "
                f"| {candidate.get('source', '')} p.{candidate.get('page', '')} {candidate.get('section', '')} "
                f"| {excerpt} | {candidate.get('relevant')} |  | {candidate.get('note', '')} |"
            )
        lines.append("")
    return "\n".join(lines)


def collect_runs(labels: dict[str, Any], grade: Callable[[dict[str, Any]], Any], repeat: int) -> dict[str, list[dict[str, Any]]]:
    """質問ごとに評価器を repeat 回呼び、attempt payload を集める。

    grade は 1 回分の payload、または (payload, candidates) を返す。candidates（`_crag_grade_candidates` の出力）が
    あれば、提示された候補の chunk_uid / source / page / section を payload の `candidates` に記録し、集計で
    ラベルとの照合と absent の判定に使う (#995)。
    """
    runs: dict[str, list[dict[str, Any]]] = {}
    for question in labels["questions"]:
        attempts = []
        for _ in range(max(1, repeat)):
            result = grade(question)
            payload, candidates = result if isinstance(result, tuple) else (result, None)
            if candidates is not None:
                payload = {
                    **payload,
                    "candidates": [
                        {
                            "chunk_uid": c.get("chunk_uid", ""),
                            "source": c.get("source", ""),
                            "page": c.get("page", 0),
                            "section": c.get("section", ""),
                        }
                        for c in candidates
                    ],
                }
            attempts.append(payload)
        runs[question["id"]] = attempts
    return runs
