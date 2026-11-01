"""回答の最終の検証（#1246、handoff §12）。

呼び出し側（Agent など）が渡した根拠の参照（document_id・chunk_id）を、今の利用者の権限と今の版で
読み直し（呼び出し側の本文は信じない）、回答の主張を rag_engine の監査（モデル 1 回）で確かめる。

呼び出し側が AnswerEnvelope の要求や業務ガイドの参照を渡したときは、決定的な検査（`answer_checks`。
モデルは呼ばない）も行い、結果を findings として主張の監査と並べて返す（#1276）。業務ガイドは
呼び出し側の内容を信じず、今の利用者の権限で検索・回答プロファイルを確かめ、公開の版をストアから読み直す。
error の finding があれば valid にしない。

呼び出し側が `rag_search` の回答の記録（trace_id）を渡したときは、その記録を今の利用者の範囲で
読み、監査を通った説明と本文が同じ段落は、根拠を今の権限と版で読み直して本文が変わって
いなければ、その判定を使ってモデルに渡さない（#1527。すべての段落が再利用できればモデルを
呼ばない）。

呼び出し側が利用者の前提（回答の前の確認の質問と答え。#1898）を渡したときは、資料ではない前提として根拠と並べて
監査に渡し、答えをそのまま述べた段落と、資料の規則を答えに当てはめた結論を確かめる（資料の内容の裏付けには使わない）。

呼び出し側が段落ごとの出典（`passage_citations`。段落の位置と chunk_id。#104）を渡したときは、
本文に出典（根拠の ID・出典の行）が無い段落だけ、その根拠を段落の出典として監査する（本文が
出典を書いた段落は本文の出典を使い、渡した出典で広げない。根拠に無い chunk_id は使わない）。
根拠を本文とは別に示す業務 Agent の回答が、出典が無いためにすべて unassessed になるのを防ぐ。

時間の上限（#2700）: 検証の全体を `rag_answer_validation_timeout_seconds`（呼び出し側の
待ち時間より短い）で打ち切る。監査は段落を組に分けて並行に呼び
（`rag_answer_validation_batch_passages`・`rag_answer_validation_concurrency`）、上限までに
返らなかった組の段落は確かめ終わらなかった段落（unassessed）として、確かめ終わった段落の
判定と一緒に返す（status=partial。どの段落も返らなければ timed_out）。上限を過ぎたら LLM を
呼び続けない（呼び出し側が待つのをやめた後に、同じ LLM を使う他の処理を遅くしない）。
検証は回答フローとは別の executor で動かす。
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rag_engine.evaluation.answer_validation import MAX_DATA_RESULTS, MAX_PREMISES, is_premise_id
from rag_engine.generation.table_records import TABLE_RECORD_KEY
from rag_engine.resources.deadline import remaining_seconds
from rag_engine.resources.runtime import bind_prompt_overrides, saved_prompt_overrides
from rag_engine.resources.tracing import bind_trace_observer, observe_call
from rag_parser_core.sheet_records import (
    ROW_BASIS_CSV,
    ROW_BASIS_KEY,
    ROW_BASIS_SHEET,
    TABLE_CELLS_KEY,
    TABLE_INDEX_KEY,
    TABLE_ROW_KEY,
)

from app.clients.oracle import OracleClient
from app.clients.support_guide_store import SupportGuideStore
from app.config import (
    Settings,
    enterprise_ai_connection_for_model,
    enterprise_ai_default_model_id,
    enterprise_ai_reasoning_effort_to_send,
)
from app.rag.answer_checks import (
    AnswerFinding,
    check_impact,
    check_requests,
)
from app.rag.answer_engine import (
    ANSWER_CITATION_LINES_KEY,
    _sheet_location,
    build_engine_settings,
)
from app.rag.answer_provenance import _sha256, answer_prompt_version
from app.rag.answer_threads import (
    ANSWER_DEADLINE_GRACE_SECONDS,
    run_answer_in_thread,
    validation_executor,
)
from app.rag.figure_region import figure_evidence_type
from app.rag.guide_step_validation import guide_step_embed, guide_step_findings
from app.rag.semantic_audit_policy import semantic_audit_is_diagnostic_only
from app.schemas.support_guide import SupportGuideContent

logger = logging.getLogger(__name__)

# 1 行の記録の根拠を同じ表に組み直す行の番号の基準（#2594）。本文が「列名: 値」の 1 行の
# 記録の表計算・CSV だけ（Word の表はまとまりで、解析の表の行は本文が表の形なので、検査が
# 本文の表をそのまま読む）。
_TABLE_RECORD_BASES = frozenset({ROW_BASIS_SHEET, ROW_BASIS_CSV})

# 期限の後、thread が確かめ終わった組の判定をまとめて返すまでに待つ秒数（#2700。
# LLM は呼ばない処理だけ）。業務ガイドの手順の意味の判定の embedding（1 回）は、期限の残りが
# これより短ければ呼ばない。
VALIDATION_FINALIZE_SECONDS = 5.0
# どの段落も監査し終わらなかった（`rag_engine` の STATUS_TIMED_OUT と同じ値）。
STATUS_TIMED_OUT = "timed_out"
# 監査の LLM の usage のうち、ログに出す項目（#2700。秒は呼び出しの往復の和で、
# 並行の呼び出しは重なる）。
_USAGE_LOG_KEYS = ("calls", "seconds", "output_tokens", "reasoning_tokens", "stall_resends")

# 主張の監査で「検証に通った」とみなさない判定。図の見た目だけで決まる主張（figure_only）は根拠の
# 本文では確かめられないので通さない（図の画像を読んで確かめたかは呼び出し側が決める。#2678）。
_BLOCKING_STATUSES = frozenset(
    {"unsupported", "contradicted", "citation_error", "unassessed", "figure_only"}
)


@dataclass(frozen=True)
class EvidenceRef:
    document_id: str
    chunk_id: str


@dataclass(frozen=True)
class GuideCheckRef:
    """回答が沿った業務ガイド（検索・回答プロファイル・guide_id・公開の版・分かっている条件）。"""

    search_answer_profile_id: str
    guide_id: str
    revision: int
    conditions: Mapping[str, str] = field(default_factory=dict)


class GuideProfileNotFoundError(LookupError):
    """業務ガイドの検索・回答プロファイルが無いか、利用できる範囲の外。"""


@dataclass
class AnswerValidation:
    status: str
    valid: bool
    claim_checks: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    missing_evidence: list[EvidenceRef] = field(default_factory=list)
    stale_evidence: list[EvidenceRef] = field(default_factory=list)
    evidence_truncated: bool = False
    # 決定的な検査（#1276）。checks は行った検査（requests / guide_steps / impact）。
    findings: list[AnswerFinding] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    # 検査に使った業務ガイドの公開の版（業務ガイドを渡さなければ None）。
    guide_revision: int | None = None
    # RAG の回答の監査の判定を再利用した段落の数（#1527。モデルに渡さなかった段落）。
    reused_claims: int = 0
    # 時間の上限までに監査し終わらなかった段落の数（#2700。status=partial のときの
    # unassessed の段落）。
    timed_out_claims: int = 0
    # 業務ガイドの手順ごとの充足（#2708。`guide_step_validation`）と、意味の判定の embedding の
    # 呼び出し。
    guide_steps: list[dict[str, Any]] = field(default_factory=list)
    guide_step_embedding: dict[str, Any] | None = None
    llm_usage: dict[str, Any] = field(default_factory=dict)
    reuse_context: str = ""
    # 原質問・確認の答えとの両立の比較（#3800。`premise_consistency_checks`）の、段落ごとの採否。
    # 段落の ID・実行の状態・判定・採否の code だけを残す（モデルが書いた制約・推奨・原文は
    # 残さない）。
    premise_consistency_checks: list[dict[str, Any]] = field(default_factory=list)


# 両立の比較の記録に残す項目（#3800）。値はすべて ID・状態・語彙の code で、自由記述を含まない。
PREMISE_CHECK_FIELDS = (
    "answer_passage_id",
    "status",
    "decision",
    "adopted",
    "fit",
    "constraint_scope",
    "passage_mode",
)


def premise_check_records(checks: object) -> list[dict[str, Any]]:
    """engine の両立の比較の診断から、記録に残す項目だけを取り出す（#3800）。"""
    if not isinstance(checks, list):
        return []
    return [
        {key: item[key] for key in PREMISE_CHECK_FIELDS if key in item}
        for item in checks
        if isinstance(item, Mapping)
    ]


def evidence_location(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """検証に渡す根拠の場所（節と、表計算のシート・行・セル範囲）。

    値は MCP の根拠の location と同じ（#1450）。

    段落が書いた「行23」「シート「台帳」」などの場所を、モデルが根拠の場所と照らせるようにする。
    分からない項目は入れない（頁は呼び出し側が ``page_start``・``page_end`` で渡す）。
    """
    location: dict[str, Any] = {}
    section = metadata.get("section_path")
    if isinstance(section, str):
        section = section.split(" > ")
    if isinstance(section, list):
        path = [str(item).strip() for item in section if str(item).strip()]
        if path:
            location["section_path"] = path
    sheet = _sheet_location(metadata)
    if sheet is not None:
        location.update(sheet)
    return location


def _metadata_int(metadata: Mapping[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def table_record(document_id: str, metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    """表の 1 行の記録の根拠の、同じ表の印・行の番号・値のあるセル（#2594）。

    検査が同じ表の 1 行の記録の根拠を 1 つの表に組み直すのに使う
    （``rag_engine.generation.table_records``）。同じ表は、同じ文書・処理の版（``chunk_set_id``）・
    行の番号の基準・シート・表の番号。セルの座標（``table_row``・``table_cells_json``。#3262）を
    記録した根拠だけで、記録していない根拠は None。
    """
    row = _metadata_int(metadata, TABLE_ROW_KEY)
    raw = metadata.get(TABLE_CELLS_KEY)
    if row is None or not isinstance(raw, str) or not raw:
        return None
    sheet = metadata.get("sheet_name")
    sheet_name = sheet if isinstance(sheet, str) and sheet else None
    basis = ROW_BASIS_SHEET if sheet_name else metadata.get(ROW_BASIS_KEY)
    if basis not in _TABLE_RECORD_BASES:
        return None
    try:
        cells = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(cells, list):
        return None
    table_index = _metadata_int(metadata, "sheet_table_index" if sheet_name else TABLE_INDEX_KEY)
    group = json.dumps(
        [document_id, metadata.get("chunk_set_id"), basis, sheet_name, table_index],
        ensure_ascii=False,
    )
    # セルは [列, 表頭の段]（解析の表は bbox が続く。検査は列と表頭の段だけを使う）。
    return {
        "group": group,
        "row": row,
        "cells": [cell[:2] for cell in cells if isinstance(cell, list)],
    }


def is_valid(status: str, counts: dict[str, int], *, unreadable: int) -> bool:
    """矛盾・裏付けの無い主張・読めない根拠が無く、裏付けのある主張が 1 つ以上あるか。"""
    if status != "completed" or unreadable:
        return False
    if any(counts.get(item, 0) for item in _BLOCKING_STATUSES):
        return False
    return counts.get("supported", 0) > 0


async def _guide_findings(
    oracle: OracleClient, answer: str, guide: GuideCheckRef
) -> tuple[list[AnswerFinding], list[str], int | None, SupportGuideContent | None]:
    """業務ガイドの公開の版を読み直し、影響範囲を確かめる（手順は主張の監査の後に確かめる。#2708）。

    ストアの障害は例外のまま上げる（「業務ガイドが無い」と区別する。handoff §10）。
    """
    view = await oracle.get_search_answer_profile(guide.search_answer_profile_id)
    if view is None:
        raise GuideProfileNotFoundError(guide.search_answer_profile_id)
    published = await SupportGuideStore(oracle).published_contents(view.id)
    found = next(
        ((revision, content) for gid, revision, content in published if gid == guide.guide_id),
        None,
    )
    if found is None:
        return (
            [
                AnswerFinding(
                    "guide",
                    "guide_unavailable",
                    "error",
                    "業務ガイドが公開されていないか、アーカイブされています。",
                )
            ],
            ["guide"],
            None,
            None,
        )
    revision, content = found
    findings: list[AnswerFinding] = []
    if revision != guide.revision:
        # 今の公開の版で確かめ、版が変わったことを示す（回答は作り直しが要る）。
        findings.append(
            AnswerFinding(
                "guide",
                "guide_revision_stale",
                "error",
                f"業務ガイドの版 {guide.revision} は古い版です（今の公開の版は {revision}）。",
            )
        )
    findings += check_impact(answer, content, guide.conditions)
    return findings, ["guide", "guide_steps", "impact"], revision, content


# 監査の判定を再利用する回答の記録の数の上限（契約の maxItems）。
MAX_ANSWER_TRACE_IDS = 10


def validation_reuse_context(
    question: str,
    answer: str,
    settings: Settings,
    *,
    prompt_version: str,
    evidence: Sequence[Mapping[str, Any]],
    premises: Sequence[Mapping[str, Any]],
    data_results: Sequence[Mapping[str, Any]],
    guide: GuideCheckRef | None,
    requests: Sequence[Mapping[str, str]] | None,
    gaps: Sequence[str],
) -> str:
    """依存する本文・前提・根拠・モデル・指示・判定方針が同じときだけ再利用する。

    段落間の依存を確定できないため、回答全体の変更は再監査する。保存するのは内容の digest だけ。
    """
    model_id = enterprise_ai_default_model_id(settings)
    connection = enterprise_ai_connection_for_model(settings, model_id)
    return _sha256(
        {
            "policy": "review-contract-v1",
            "question": question,
            "answer": answer,
            "prompts": prompt_version,
            "profile_revision": settings.rag_search_answer_profile_revision,
            "runtime_knowledge": settings.rag_runtime_knowledge,
            "model": model_id,
            "connection": {
                "id": connection.connection_id,
                "url": connection.base_url,
                "project": connection.project_id,
            },
            "reasoning": enterprise_ai_reasoning_effort_to_send(settings, model_id),
            "audit_settings": {
                key: value
                for key, value in settings.model_dump(mode="json").items()
                if key.startswith("rag_answer_")
                or key in {"rag_audit_reasoning_effort", "rag_judge_reasoning_effort"}
            },
            "evidence": list(evidence),
            "premises": list(premises),
            "data_results": list(data_results),
            "guide": {
                "profile": guide.search_answer_profile_id,
                "id": guide.guide_id,
                "revision": guide.revision,
                "conditions": dict(guide.conditions),
            }
            if guide
            else None,
            "requests": list(requests) if requests is not None else None,
            "gaps": list(gaps),
        }
    )


async def audited_answer_lines(
    oracle: OracleClient,
    trace_ids: Sequence[str],
    readable: Mapping[str, Mapping[str, Any]],
    *,
    reuse_context: str = "",
) -> dict[str, list[str]]:
    """回答の記録の、監査を通った説明の本文 → 根拠の chunk の id（#1527。`audited_answer_items`）。

    記録は今の利用者の範囲で読む（持ち主・利用できる検索・回答プロファイル。読めない記録は使わない）。
    根拠は ``readable``（今の権限と版で読み直した chunk。id → 検証に渡す根拠）にあり、記録の
    引用の本文と変わっていないものだけを使う。監査が時間の上限で切れた回答（#1516）の説明は
    監査されていないので使わない。
    """
    from rag_engine.evaluation.answer_validation import audited_answer_items, audited_line_key

    audited: dict[str, list[str]] = {}
    if not reuse_context:
        return audited
    for trace_id in list(dict.fromkeys(trace_ids))[:MAX_ANSWER_TRACE_IDS]:
        row = await oracle.get_answer_record(trace_id)
        if row is None:
            continue
        diagnostics = row.get("diagnostics_json")
        if not isinstance(diagnostics, Mapping):
            continue
        if semantic_audit_is_diagnostic_only(diagnostics):
            continue
        validation = diagnostics.get("answer_validation")
        if (
            not isinstance(validation, Mapping)
            or validation.get("valid") is not True
            or validation.get("status") != "completed"
            or validation.get("reuse_context") != reuse_context
        ):
            continue
        budget = diagnostics.get("round_budget")
        if isinstance(budget, Mapping) and budget.get("adopted_audit_cut"):
            continue
        # 最終監査で実際に支持した主張と出典の組に限る。複数出典・依存が不明な組は再監査する。
        supported_pairs: dict[str, set[str]] = {}
        for claim in validation.get("claim_checks") or ():
            if not isinstance(claim, Mapping) or claim.get("status") != "supported":
                continue
            if "," in str(claim.get("evidence_id") or ""):
                continue
            source_id = str(claim.get("source_id") or "")
            key = audited_line_key(str(claim.get("answer_quote") or ""))
            if key and source_id in readable:
                supported_pairs.setdefault(key, set()).add(source_id)
        envelope = diagnostics.get("envelope")
        items = envelope.get("items") if isinstance(envelope, Mapping) else None
        citations = row.get("citations_json")
        if not isinstance(items, list) or not isinstance(citations, list):
            continue
        stored: dict[str, str] = {}
        lines: dict[str, list[int]] = {}
        for citation in citations:
            if not isinstance(citation, Mapping):
                continue
            chunk_id = str(citation.get("chunk_id") or "")
            metadata = citation.get("metadata")
            numbers = (
                metadata.get(ANSWER_CITATION_LINES_KEY) if isinstance(metadata, Mapping) else None
            )
            if chunk_id and isinstance(numbers, list):
                lines[chunk_id] = [int(n) for n in numbers if isinstance(n, int)]
                stored[chunk_id] = str(citation.get("text") or "")
        for key, chunk_ids in audited_answer_items(
            str(row.get("answer") or ""), items, lines
        ).items():
            # 根拠を今読めて、記録したときから本文が変わっていない（版・権限の変化を
            # 読み直しで確かめる）。
            if (
                len(chunk_ids) == 1
                and supported_pairs.get(key) == set(chunk_ids)
                and all(
                    chunk_id in readable
                    and str(readable[chunk_id].get("text") or "") == stored.get(chunk_id)
                    for chunk_id in chunk_ids
                )
            ):
                audited.setdefault(key, chunk_ids)
    return audited


# 資料との照合の中の工程の名前（処理の経過の段。#3470）。名前は利用者向けの名詞（画面に
# そのまま出る）。
GUIDE_LOAD_STAGE = "業務ガイドの読み込み"
EVIDENCE_LOAD_STAGE = "根拠の本文の読み込み"
CLAIM_AUDIT_STAGE = "回答の段落の監査"


async def validate_answer(
    question: str,
    answer: str,
    refs: Sequence[EvidenceRef],
    settings: Settings,
    *,
    oracle: OracleClient | None = None,
    requests: Sequence[Mapping[str, str]] | None = None,
    gaps: Sequence[str] = (),
    guide: GuideCheckRef | None = None,
    answer_trace_ids: Sequence[str] = (),
    premises: Sequence[Mapping[str, str]] = (),
    data_results: Sequence[Mapping[str, str]] = (),
    passage_citations: Sequence[Mapping[str, Any]] = (),
) -> AnswerValidation:
    """回答の本文を根拠と照合する（MCP の `rag_validate_answer` と回答フローの「資料との照合」）。

    中の工程（業務ガイドの読み込み・根拠の本文の読み込み・回答の段落の監査）とモデルの呼び出し
    は、検索・回答と同じ回答エンジンの観測（`_observe_engine_call`）で span にする（#3470。MCP の
    呼び出しはパイプラインを通らないので、ここで結ぶ）。
    """
    from app.rag.pipeline import _observe_engine_call

    with bind_trace_observer(_observe_engine_call):
        return await _validate_answer(
            question,
            answer,
            refs,
            settings,
            oracle=oracle,
            requests=requests,
            gaps=gaps,
            guide=guide,
            answer_trace_ids=answer_trace_ids,
            premises=premises,
            data_results=data_results,
            passage_citations=passage_citations,
        )


async def _validate_answer(
    question: str,
    answer: str,
    refs: Sequence[EvidenceRef],
    settings: Settings,
    *,
    oracle: OracleClient | None,
    requests: Sequence[Mapping[str, str]] | None,
    gaps: Sequence[str],
    guide: GuideCheckRef | None,
    answer_trace_ids: Sequence[str],
    premises: Sequence[Mapping[str, str]],
    data_results: Sequence[Mapping[str, str]],
    passage_citations: Sequence[Mapping[str, Any]] = (),
) -> AnswerValidation:
    started = time.monotonic()
    limit = float(settings.rag_answer_validation_timeout_seconds)
    oracle = oracle or OracleClient()
    # 決定的な検査を先に行う（業務ガイドを読めなければ、モデルを呼ぶ前に止める）。
    findings: list[AnswerFinding] = []
    checks: list[str] = []
    guide_revision: int | None = None
    if requests is not None:
        findings += check_requests(answer, requests, gaps)
        checks.append("requests")
    guide_content: SupportGuideContent | None = None
    if guide is not None:
        with observe_call("stage", GUIDE_LOAD_STAGE):
            guide_items, guide_checks, guide_revision, guide_content = await _guide_findings(
                oracle, answer, guide
            )
        findings += guide_items
        checks += guide_checks
    deterministic: dict[str, Any] = {
        "findings": findings,
        "checks": checks,
        "guide_revision": guide_revision,
    }
    items: list[dict[str, Any]] = []
    evidence_versions: dict[str, dict[str, Any]] = {}
    # 根拠の chunk の id → 文書の id（業務ガイドの手順の参照の照合に使う。監査のモデルには
    # 渡さない）。
    documents: dict[str, str] = {}
    reuse_context = ""
    missing: list[EvidenceRef] = []
    stale: list[EvidenceRef] = []
    with observe_call("stage", EVIDENCE_LOAD_STAGE):
        for ref in dict.fromkeys(refs):
            chunk = await oracle.retrievable_chunk(ref.document_id, ref.chunk_id)
            if chunk is None:
                exists = await oracle.accessible_chunk_exists(ref.document_id, ref.chunk_id)
                (stale if exists else missing).append(ref)
                continue
            metadata = dict(chunk.metadata)
            evidence_versions[chunk.chunk_id] = {
                key: metadata.get(key)
                for key in (
                    "recipe_id",
                    "chunk_set_id",
                    "artifact_id",
                    "source_run_id",
                    "chunk_strategy",
                )
            }
            # 図の根拠には種類を付ける（図の見た目だけで決まる主張を本文の照合と分ける。#2678）。
            figure = figure_evidence_type(metadata)
            documents[chunk.chunk_id] = chunk.document_id
            # 表の 1 行の記録の根拠は、検査が同じ表に組み直すための位置を持つ（#2594。
            # モデルには渡さない）。
            record = table_record(chunk.document_id, metadata)
            items.append(
                {
                    "id": chunk.chunk_id,
                    "source": chunk.file_name or chunk.document_id,
                    "page_start": metadata.get("page_start"),
                    "page_end": metadata.get("page_end"),
                    **evidence_location(metadata),
                    **({"evidence_type": figure} if figure else {}),
                    **({TABLE_RECORD_KEY: record} if record else {}),
                    "text": chunk.text,
                }
            )
        # 指示の版を読めなければ再利用しない。旧記録も版が無いため再監査する。
        overrides = saved_prompt_overrides()
        try:
            if overrides is None:
                overrides = await oracle.answer_prompt_overrides()
            reuse_context = validation_reuse_context(
                question,
                answer,
                settings,
                prompt_version=answer_prompt_version(overrides),
                evidence=[
                    {**item, "artifact_version": evidence_versions[item["id"]]} for item in items
                ],
                premises=premises,
                data_results=data_results,
                guide=guide,
                requests=requests,
                gaps=gaps,
            )
        except Exception:  # noqa: BLE001 - 再利用できなくても通常の監査を続ける。
            overrides = {}
            reuse_context = ""

        audited = (
            await audited_answer_lines(
                oracle,
                answer_trace_ids,
                {item["id"]: item for item in items},
                reuse_context=reuse_context,
            )
            if answer_trace_ids and items
            else {}
        )
    conditions = dict(guide.conditions) if guide is not None else {}
    if not items:
        if guide_content is not None:
            findings += guide_step_findings(
                answer, guide_content, conditions, None, [], embed=None
            )[0]
        return AnswerValidation(
            status="no_evidence",
            valid=False,
            missing_evidence=missing,
            stale_evidence=stale,
            **deterministic,
        )

    usage: dict[str, Any] = {}
    loop = asyncio.get_running_loop()

    def run() -> dict[str, Any]:
        from rag_engine.evaluation.answer_validation import validate_answer_claims
        from rag_engine.resources.llm_usage import bind_llm_usage

        with (
            tempfile.TemporaryDirectory(prefix="rag-engine-validate-") as work,
            bind_llm_usage() as calls,
            bind_prompt_overrides(overrides or {}),
        ):
            engine_settings = build_engine_settings(settings, output_dir=Path(work))
            try:
                result = validate_answer_claims(
                    question,
                    answer,
                    items,
                    engine_settings,
                    audited=audited,
                    premises=list(premises)[:MAX_PREMISES],
                    # 業務データの照会の結果（#1950）。資料ではない根拠として並べる。
                    data_results=list(data_results)[:MAX_DATA_RESULTS],
                    # 本文に出典を書かない呼び出し側が渡した段落ごとの根拠（#104）。
                    caller_citations=list(passage_citations),
                    batch_passages=settings.rag_answer_validation_batch_passages,
                    max_concurrency=settings.rag_answer_validation_concurrency,
                )
            finally:
                usage.update(calls.snapshot())
        if guide_content is not None:
            # 業務ガイドの手順ごとの充足（#2708）。主張の監査で裏付けた段落と、手順の主の操作の
            # 文を対応付ける。意味の判定の embedding は、期限の残りが短ければ呼ばない（#2700）。
            completed = result.get("status") == "completed"
            remaining = remaining_seconds()
            room = remaining is None or remaining > VALIDATION_FINALIZE_SECONDS
            result["guide_step_results"] = guide_step_findings(
                answer,
                guide_content,
                conditions,
                list(result.get("claim_checks") or []) if completed else None,
                [{**item, "document_id": documents.get(item["id"], "")} for item in items],
                embed=guide_step_embed(settings, loop)
                if completed and room and guide_content.steps
                else None,
            )
        return result

    # 上限の残り（根拠の読み直しの分を引く）を thread の期限にし、LLM 1 回の timeout と
    # 再試行をそれに縮める。
    left = limit - (time.monotonic() - started)
    result: dict[str, Any] = {"status": STATUS_TIMED_OUT}
    if left > 0:
        # モデルの呼び出し（段落のまとまりごとの監査）はこの段の子の span になる（thread は文脈を
        # 引き継ぐ。#3470）。
        with observe_call("stage", CLAIM_AUDIT_STAGE):
            try:
                # 検証の専用の thread で動かし、呼び出し側の打ち切り・取り消しを thread に
                # 伝える(#1422・#2700)。
                async with asyncio.timeout(
                    left + ANSWER_DEADLINE_GRACE_SECONDS + VALIDATION_FINALIZE_SECONDS
                ):
                    result = await run_answer_in_thread(
                        run, timeout_seconds=left, executor=validation_executor()
                    )
            except TimeoutError:
                # thread が期限の後に確かめ終わった分をまとめられなかった（期限は取り消し済み）。
                result = {"status": STATUS_TIMED_OUT}
    counts = {str(key): int(value) for key, value in (result.get("counts") or {}).items()}
    status = str(result.get("status") or "error")
    if "guide_step_results" not in result and guide_content is not None:
        # 監査が上限までに返らなかった: 手順の名前の一致で順序だけを確かめる（モデルは呼ばない）。
        result["guide_step_results"] = guide_step_findings(
            answer, guide_content, conditions, None, [], embed=None
        )
    step_findings, guide_steps, step_embedding = result.get("guide_step_results") or ([], [], None)
    findings += step_findings
    blocking = any(item.severity == "error" for item in findings)
    timed_out = len(result.get("timed_out_passage_ids") or ())
    # 監査の応答の参照の誤りの監査し直し（#112。status と、対象・採った段落の数）。
    reaudit = result.get("reference_reaudit")
    reaudit = reaudit if isinstance(reaudit, Mapping) else {}
    logger.info(
        "rag_answer_validation",
        extra={
            "validation_status": status,
            "validation_seconds": round(time.monotonic() - started, 1),
            "validation_limit_seconds": limit,
            "validation_evidence": len(items),
            "validation_claims": len(result.get("claim_checks") or ()),
            "validation_timed_out_claims": timed_out,
            "validation_reused_claims": len(result.get("reused_passage_ids") or ()),
            "validation_reference_reaudit": str(reaudit.get("status") or "not_run"),
            "validation_reference_reaudit_targets": len(reaudit.get("target_passage_ids") or ()),
            "validation_reference_reaudit_adopted": len(reaudit.get("adopted_passage_ids") or ()),
            "validation_guide_step_embedding": step_embedding is not None,
            **{
                f"validation_llm_{key}": round(float(usage.get(key) or 0), 1)
                for key in _USAGE_LOG_KEYS
            },
        },
    )
    return AnswerValidation(
        status=status,
        valid=is_valid(status, counts, unreadable=len(missing) + len(stale)) and not blocking,
        claim_checks=list(result.get("claim_checks") or []),
        counts=counts,
        missing_evidence=missing,
        stale_evidence=stale,
        evidence_truncated=bool(result.get("evidence_truncated")),
        reused_claims=len(result.get("reused_passage_ids") or ()),
        timed_out_claims=timed_out,
        guide_steps=guide_steps,
        guide_step_embedding=step_embedding,
        llm_usage=dict(usage),
        reuse_context=reuse_context,
        premise_consistency_checks=premise_check_records(result.get("premise_consistency_checks")),
        **deterministic,
    )


__all__ = [
    "MAX_DATA_RESULTS",
    "MAX_PREMISES",
    "AnswerValidation",
    "EvidenceRef",
    "GuideCheckRef",
    "GuideProfileNotFoundError",
    "evidence_location",
    "is_premise_id",
    "is_valid",
    "premise_check_records",
    "validate_answer",
]
