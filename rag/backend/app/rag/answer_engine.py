"""rag_poc の根拠付き回答フローを backend の検索・rerank・LLM で実行する。

rag_poc の ``answer_question_result``(質問ルーティング / CRAG / 親子文脈 / 生成 + 監査ラウンド)を
そのまま使い、``AnswerDependencies`` の I/O だけを差し替える。

- 検索: backend の Oracle hybrid 検索(
検索・回答プロファイルの KB フィルタとドメインキーワード付き)。

  複数の検索文は原質問主軸の重み付き RRF で融合し、同じ親の兄弟 chunk と
  親本文(``parent_text``)で親子を復元する。
  質問の理解(``inquiry_conditions``)が名指しした文書名・ページは検索条件に足し、
  profile / business_match のチャネルを RRF に加える(#546)。文書の分類は chunk の
  metadata(``document.classification``)に載せ、業務の候補の絞り込みに使う(#545)。
  文書の 1 ページ目の本文(chunk set ごとに 1 つ)も ``document.first_page_context`` に載せ、
  回答の「文書の背景」に使う(#557)。
  質問が名指しした業務(``business_domains``)は、検索範囲の文書の大分類の語の一覧から
  照合して rag_engine へ注入する(domain profile の ``business_patterns`` は使わない。#553)。
- 画面目録の連携(``RAG_SCREEN_LINKING_ENABLED``。#554): 目録は検索範囲の全文書の
  ``section_path`` から DB で作り(範囲と索引の状態ごとに cache)、選ばれた画面の chunk も DB から
  読んで rag_engine へ注入する(検索結果に出なかった画面も候補に加える)。
- rerank: backend の Cohere rerank。
- LLM: openai SDK(OCI_ENTERPRISE_AI_*)。rag_engine 内部の Responses API 経路を使う。
回答フローは同期関数のため回答の専用の worker thread で動かし、非同期 I/O はイベントループへ戻して
実行する。回答の打ち切り・取り消しは期限として thread に伝え、LLM の呼び出しと I/O を止める(#1422)。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import math
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

import httpx
from fastapi import HTTPException
from openai import APITimeoutError
from pr_backend_core.observability.tracing import ObservedSpan, SpanDisplay, observed_span
from pr_backend_core.timezones import configured_timezone_name
from rag_engine.generation.answer_envelope import ENVELOPE_SCHEMA_VERSION
from rag_engine.generation.answer_models import ANSWER_USES
from rag_engine.resources.deadline import DeadlineExceeded, check_deadline, current_deadline
from rag_engine.resources.runtime import saved_prompt_overrides
from rag_engine.resources.tracing import observe_call
from rag_engine.retrieval.article_catalog import ARTICLE_UNIT_KEY
from rag_engine.retrieval.attachment_evidence import ATTACHMENT_EVIDENCE_KEY
from rag_engine.retrieval.search_stages import (
    CONTEXT_SIBLINGS_STAGE,
    DERIVED_SOURCE_STAGE,
    DOCUMENT_INFO_STAGE,
    ENTITY_EXPANSION_STAGE,
    GRAPH_EXPANSION_STAGE,
    IMAGE_EVIDENCE_STAGE,
    REFERENCE_EXPANSION_STAGE,
)

from app.clients.oci_genai import OciGenAiClient
from app.clients.oracle import OracleClient
from app.clients.oracle_read_stats import (
    current_db_call_stats,
    read_lobs_as_values,
)
from app.config import (
    Settings,
    enterprise_ai_connection_for_model,
    enterprise_ai_default_model_id,
    enterprise_ai_judge_model_id,
    enterprise_ai_max_input_tokens,
    enterprise_ai_model_catalog,
    enterprise_ai_output_token_budget,
    enterprise_ai_reasoning_effort_to_send,
    enterprise_ai_vision_model_id,
    settings_today,
)
from app.rag.answer_metrics import score_answer_evaluation
from app.rag.answer_prompts import prompt_overrides
from app.rag.answer_provenance import answer_prompt_version
from app.rag.answer_threads import run_answer_in_thread
from app.rag.chat_attachments import is_attachment_chunk
from app.rag.chunking_small_to_big import engine_search_text
from app.rag.citation_status import citation_support_display
from app.rag.context_page_scope import CONTEXT_PAGE_SCOPE, contained, positive
from app.rag.cross_references import (
    REFERENCE_FROM_KEY,
    REFERENCE_LABEL_KEY,
    REFERENCE_RANK_KEY,
    REFERENCE_TARGETS_KEY,
    ReferenceTarget,
    SectionIndex,
    document_name_match,
    document_name_references,
    extract_references,
    reference_targets,
    resolve_reference_specs,
    same_document_title,
    section_path_within,
    split_section_path,
    title_key,
)
from app.rag.degraded_steps import DegradedSteps
from app.rag.derived_evidence import (
    derived_kind,
    derived_source_chunks,
    is_derived_chunk,
    original_chunks,
)
from app.rag.document_crop import DocumentSourceNotFoundError, crop_png, load_parsed_source
from app.rag.element_locator import chunk_element_ids
from app.rag.entity_expansion import (
    ENTITY_EXPANSION_KEY,
    ENTITY_EXPANSION_ROLE,
    ENTITY_SEED_ANCHORS,
    EntityExpansionStore,
    expansion_metadata,
    plan_entity_expansion,
)
from app.rag.field_filter_reader import merge_field_conditions
from app.rag.graph_expansion import (
    GRAPH_EXPANSION_KEY,
    GRAPH_SEED_ANCHORS,
    GraphExpansionStore,
    graph_expansion_metadata,
    plan_graph_expansion,
)
from app.rag.semantic_audit_policy import semantic_audit_is_diagnostic_only
from app.rag.span_display import ANSWER_STEP_KIND, answer_step_details, answer_step_params
from app.rag.step_details import step_details_payload
from app.rag.stored_answer import passage_mark_record, stored_evaluation_input
from app.rag.support_guide_evidence import guide_evidence_conditions, guide_evidence_plans
from app.schemas.classification import category_label, normalize_category_value
from app.schemas.search import (
    MAX_PAGE_RANGES,
    PAGE_RANGES_FILTER_KEY,
    ExtractionFieldCondition,
    PageRange,
    RetrievedChunk,
    SearchMode,
    SearchRequest,
    format_page_ranges,
    parse_page_ranges,
)

T = TypeVar("T")

logger = logging.getLogger(__name__)

ANSWER_ENGINE = "grounded"
ENGINE_SOURCE_RUN_ID = "0" * 16
# 呼び出し元が検索文を作った検索（#2651）の質問拡張の戦略（LLM を呼ばない単純検索）。
CALLER_QUERY_STRATEGY = "simple_retrieval"
# 回答フロー 1 件の I/O 待ちの上限(秒)。LLM/検索の個別 timeout は各 client が持つ。
_IO_TIMEOUT_SECONDS = 600.0
# 質問が挙げた文書名のうち、ナレッジベースにあるか確かめる数の上限(1 語ごとに SQL 1 回)。
_MAX_QUESTION_FILE_TERMS = 3
# 質問の業務名として照合する大分類の名前の最短の文字数(1 文字の名前は質問の別の語に偶然含まれる)。
_MIN_BUSINESS_NAME_CHARS = 2
# 画面目録の cache((検索条件, 索引の状態) → 目録)。索引の状態は文書の追加・再索引で変わる(#554)。
_SCREEN_CATALOG_CACHE: dict[tuple[str, str], dict[str, list[str]]] = {}
_SCREEN_CATALOG_CACHE_SIZE = 32
# section_path の見出しの区切り(chunking_small_to_big が「 > 」でつないで保存する)。
_SECTION_PATH_SEPARATOR = " > "
# 回答の thread が event loop の I/O を待つ間に、回答の期限・取り消しを確かめる間隔(秒。#1422)。
_IO_DEADLINE_POLL_SECONDS = 0.5
# 回答フローの工程の通知(進捗)1 件を待つ上限(秒)。通知は補助なので、超えたら待たずに続ける。
_STEP_NOTIFY_TIMEOUT_SECONDS = 5.0
# 交差参照(#1280): 参照先を辿る起点にする検索候補の数(RRF の上位から)。この起点の参照先は
# 上限まで必ず辿る。
_REFERENCE_SOURCE_ANCHORS = 5
# 交差参照(#1549): 上の起点の外でも、回答の文脈に入りうる統合の上位(回答の検索の ``top_k``。
# この数が上限)の候補が参照を持つなら、残りの上限まで辿る。検索文は LLM が毎回作るため、参照を
# 持つ chunk の統合の順位は回ごとに 2 位にも 8 位にもなり、上位 5 件だけでは辿るかどうかが揺れる。
_REFERENCE_EXTENDED_ANCHORS = 20
# 交差参照(#1549): 上位 5 件の外の起点で試す参照先の数の上限(解決できない参照の SQL を抑える)。
_REFERENCE_EXTENDED_TARGETS = 6
# 交差参照: 1 つの参照先の節から足す chunk の数の上限(節の先頭から。親子階層では親本文も入る)。
_REFERENCE_CHUNKS_PER_TARGET = 2
# 検索の基準日の条件の名前(文書の有効期間をこの日で判定する。YYYY-MM-DD)。
AS_OF_FILTER_KEY = "as_of"
# 交差参照: 参照先の chunk の RRF の点(起点の点に掛ける。rerank が無効なときの並びに使う)。
_REFERENCE_SCORE_DECAY = 0.5
# 交差参照: 他の文書への参照で、文書名・タイトルが合う文書を探す行の上限(文書とタイトルの組)。
_REFERENCE_DOCUMENT_CANDIDATES = 20
# 交差参照: 文書名だけの参照(「定期保守計画で確かめます」。#1400)の参照先の文書から、質問で
# 検索して rerank にかける chunk の数(このうち関連の高い順に参照先 1 つにつき
# ``_REFERENCE_CHUNKS_PER_TARGET`` 件を足す)。
_DOCUMENT_REFERENCE_CANDIDATES = 8
# 交差参照: 参照先を探す範囲に残す検索条件(ナレッジベース・旧版の扱い・基準日だけ)。文書名・
# ページ・分類・項目などの絞り込みは参照を辿るときには使わない(業務の絞り込みで正当な参照を
# 止めない。§7.2)。本文に明記された参照(「第 5 条を参照」など)は、利用者が指定した文書名の外の
# 文書へも辿る(引用を辿る一般的なやり方と同じ。#1280)。実体・関係情報の拡張(``_expansion_scope``)
# と違い、意図して利用者の絞り込みを越える(#3260)。基準日(as_of)は時点の境界なので外さない
# (主検索と同じ日で有効期間を判定する。#2602)。利用者の権限(認可の範囲)は検索条件ではなく
# SQL の述語で当たるので、ここでも外れない。
_REFERENCE_SCOPE_FILTER_KEYS = ("knowledge_base_id", "include_superseded", AS_OF_FILTER_KEY)
# 診断に出す交差参照の件数の上限。
_REFERENCE_DIAGNOSTICS_LIMIT = 10
# 診断に出す交差参照の起点の記録の、検索の回数の上限(CRAG の各回・要求ごとの再検索)。
_REFERENCE_DIAGNOSTICS_SEARCHES = 4
# 実体の 1 段の拡張(#1362)で足した chunk を置く候補の位置(先頭からこの件数の後ろ)。rag_engine は
# この chunk を rerank で並べ替えず(位置を保つ)、文書の選択で後回しにしない。
_ENTITY_INSERT_AFTER = 3
# 実体の拡張の chunk の RRF の点(起点の点に掛ける。rerank が無効なときの並びに使う)。
_ENTITY_SCORE_DECAY = 0.5
# 関係情報(#2055)で足した chunk を置く候補の位置(先頭からこの件数の後ろ。実体の拡張の後)。
# 位置は rerank が決め直す(実体の拡張と違い、位置を保つ枠は持たない)。
_GRAPH_INSERT_AFTER = 5
# 関係情報で足した chunk の RRF の点(起点の点に掛ける。rerank が無効なときの並びに使う)。
_GRAPH_SCORE_DECAY = 0.5
# 条の目録(#2254)の文字数の上限と、見出しの語の無い条に添える本文の書き出しの文字数
# (MCP の rag_outline の compact の既定と同じ)。
_ARTICLE_CATALOG_MAX_CHARS = 40000
# 条の目録で選んだ条の context の理由(rag_engine の context_builder)と、根拠の役割(#2261)。
CATALOG_ARTICLE_REASON = "catalog_article"
CATALOG_ARTICLE_ROLE = "catalog_article"
# 検索の範囲の条で構成された文書の一覧の cache((条件, 索引の状態) → 一覧。#2254)。
_ARTICLE_DOCUMENTS_CACHE: dict[tuple[str, str], list[tuple[str, str]]] = {}
_ARTICLE_CATALOG_LEAD_CHARS = 60
# 条の選択の 2 段目で見せる本文の書き出しの文字数(条の名前が質問の語から遠い条を本文で判断させる)。
_ARTICLE_REFINE_LEAD_CHARS = 120
# 診断に出す、関係情報で使った entity の数の上限。
_GRAPH_DIAGNOSTICS_ENTITIES = 10

# 回答の記録の工程(execution_steps)に残す、工程の中の SQL の計測値(#1490。DbCallStats.snapshot)。
_STEP_DB_METRIC_KEYS = frozenset(
    {
        "db_execute_count",
        "db_row_count",
        "db_lob_read_count",
        "db_round_trip_count",
        "db_seconds",
    }
)

# 回答フローの工程の通知: (工程名, "started" / "success" / "error", 経過秒)。
# 4 つ目は終わった工程の記録（詳細・LLM の回数・結果の説明。開始は空。#2373）。
type StepCallback = Callable[[str, str, float, Mapping[str, Any]], Awaitable[None]]
# 回答フローの各工程を進捗(SSE の stage)へ出すときの名前の接頭辞。後ろに工程名
# (「質問の理解」「文書検索（1回目）」など、利用者向けの日本語)を付け、
# 画面と時間切れの文言はその工程名を出す(#593)。
ANSWER_STEP_STAGE_PREFIX = "answer_step:"


def answer_step_stage(name: str) -> str:
    """回答フローの工程名を、進捗の工程の名前にする。"""
    return f"{ANSWER_STEP_STAGE_PREFIX}{name}"


# 回答の本文を根拠と照合する工程の名前（回答フローの最後の検証と MCP の
# rag_validate_answer。#3470）。
ANSWER_VALIDATION_STEP = "資料との照合"


def _answer_step_span(name: str) -> AbstractContextManager[ObservedSpan]:
    """回答フローの外で行う回答の工程（資料との照合・承認済み FAQ からの回答）の span（#3226）。

    `rag_engine` の工程（`rag.engine.stage`）と同じ名前・種類にし、処理の経過の行にする。
    """
    return observed_span(
        "rag.engine.stage",
        attributes={"pr.stage": name},
        display=SpanDisplay(kind=ANSWER_STEP_KIND, params={"name": name}),
    )


def _validation_details(validation: Mapping[str, Any]) -> dict[str, Any]:
    """資料との照合の工程の詳細（照合の結果・判定した段落の数）。"""
    return {
        "fields": [
            {"label": "照合の結果", "value": "確認済み" if validation.get("valid") else "未確認"},
            {
                "label": "照合の実行",
                "value": {
                    "completed": "完了",
                    "partial": "一部未確認",
                    "timed_out": "時間上限",
                    "error": "実行失敗",
                    "not_run": "未実行",
                    "no_evidence": "根拠なし",
                    "body_changed": "本文の変更後は未確認",
                    "input_too_large": "照合の入力上限超過",
                }.get(str(validation.get("status")), "未確認"),
            },
            {"label": "判定した段落", "value": str(len(validation.get("claim_checks") or []))},
            *(
                [
                    {
                        "label": "未照合の確認条件",
                        "value": str(validation["unassessed_premises_count"]),
                    }
                ]
                if validation.get("unassessed_premises_count")
                else []
            ),
        ],
    }


def _diagnostic_validation(validation: dict[str, Any]) -> dict[str, Any]:
    """実行結果を残し、意味の正否を未検証として区別する。"""
    return {
        **validation,
        "raw_valid": validation.get("valid"),
        "valid": None,
        "semantic_status": "unknown",
        "automatic_actions": False,
    }


@dataclass(frozen=True)
class AnswerOutcome:
    """回答の本文・引用・診断(非機密)。"""

    answer: str
    citations: list[RetrievedChunk]
    diagnostics: dict[str, Any]
    context_text: str
    # 標準回答による LLM 評価(evaluate_answer_payload)の入力。回答記録に保存する。
    evaluation_input: dict[str, Any] | None = None


# evaluate_answer_payload が読む回答 payload のキー(answer_result_payload の部分集合)。
EVALUATION_INPUT_KEYS = (
    "question",
    "answer_text",
    "reasoning_summary",
    "insufficient_reason",
    "cited_evidence",
    "external_data_required",
    "external_data_items",
    "evidence_items",
)


@dataclass
class _SearchState:
    """検索で見つけた backend chunk を chunk_id で保持し、引用へ戻すために使う。

    work_dir は rag_engine の output_dir。根拠画像を ``<work_dir>/<run>/crops/`` へ切り出す。
    """

    chunks: dict[str, RetrievedChunk] = field(default_factory=dict)
    context_reads: dict[str, list[RetrievedChunk]] = field(default_factory=dict)
    retrieval_stages: list[dict[str, Any]] = field(default_factory=list)
    # 検索の経路(chunk_id → 当たった経路。rag_engine の検索の実装と同じ形の ``vector:qN`` /
    # ``keyword:oracle_text:qN`` / ``citation`` / 質問理解の profile の経路。#3443)。根拠の記録
    # (``adb_hybrid.retrieval_channels``)と、文書の検索の工程の詳細(候補の経路)に出す。
    retrieval_channels: dict[str, set[str]] = field(default_factory=dict)
    work_dir: Path | None = None
    sources: dict[str, bytes | None] = field(default_factory=dict)
    # 根拠の画像を切り出した chunk の metadata(engine_metadata_json。画像の無い chunk は None)。
    # 同じ回答の検索で同じ chunk が何度も当たるので、切り出しと metadata の書き換えは chunk ごとに
    # 1 回にする(#2805)。
    materialized_images: dict[str, str | None] = field(default_factory=dict)
    # 質問の文書名・ページを足した検索条件(#546)。CRAG の各回で同じなので、最初の検索で
    # 1 回だけ決める。
    filters: dict[str, str] | None = None
    # 文書の分類(rag_documents.classification)を document_id ごとに保持する(#545)。
    classifications: dict[str, dict[str, object]] = field(default_factory=dict)
    loaded_classification_ids: set[str] = field(default_factory=set)
    # 画面目録(#554)。CRAG の各回で同じなので、1 回の回答で 1 回だけ作る。
    screen_catalog: dict[str, list[str]] | None = None
    # 質問から読み取った抽出項目の条件のうち検索に足したもの(#652)。
    auto_field_conditions: list[ExtractionFieldCondition] = field(default_factory=list)
    # 自動で足した検索条件を外す段(#652・#2401)。(段の名前, その段まで外した検索条件) を
    # 外す順に持つ。候補が 0 件なら、前から順に外して検索し直す(段ごとに 1 回。以降の検索も
    # 外したまま)。
    relaxations: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    # 外した段の名前(外した順)。
    relaxed: list[str] = field(default_factory=list)
    # 検索ごとの検索文の数・候補の件数・効いていた自動の条件・外した段(#2401。根拠 0 件の診断)。
    search_log: list[dict[str, object]] = field(default_factory=list)
    # 文書の 1 ページ目の本文(rag_chunk_sets.first_page_context)を chunk_set_id ごとに持つ(#557)。
    first_page_contexts: dict[str, dict[str, object]] = field(default_factory=dict)
    loaded_first_page_chunk_set_ids: set[str] = field(default_factory=set)
    # 交差参照(#1280)の参照先の chunk(参照先ごと。CRAG の各回で同じ参照先を読み直さない)と、
    # 足した chunk の記録(診断に出す。chunk_id → 起点・表記・参照先)。
    reference_chunks: dict[tuple[str, ...], list[RetrievedChunk]] = field(default_factory=dict)
    reference_expansions: dict[str, dict[str, object]] = field(default_factory=dict)
    # 交差参照で足した chunk を起点にした章節の拡張で読む章節((document_id, chunk_set_id,
    # section_id)。所有の章節から根まで)。参照先と同じ範囲(``_reference_scope``)で読む(#3325)。
    reference_sections: set[tuple[str, str, str]] = field(default_factory=set)
    # 交差参照の起点の記録(検索ごと。#1549)。参照を持つ候補の統合の順位と、辿った・辿らなかった
    # 理由(起点の外・参照先が無い・上限)を診断に出し、回答の記録から replay で確かめる。
    reference_sources: list[dict[str, object]] = field(default_factory=list)
    # 回答の検索の統合(RRF)の順位と点(chunk_id → (順位, 点)。複数回の検索では最も上の順位。
    # #1549)。根拠の木に出す。
    fused_ranks: dict[str, tuple[int, float]] = field(default_factory=dict)
    # 実体の 1 段の拡張(#1362)で足した chunk の記録(診断に出す。chunk_id → 実体・段・起点)と、
    # 同じ起点での計画(CRAG の各回で同じ起点なら SQL を読み直さない)。
    entity_expansions: dict[str, dict[str, object]] = field(default_factory=dict)
    entity_plans: dict[tuple[str, ...], list[Any]] = field(default_factory=dict)
    # 関係情報(#2055)で足した chunk の記録(診断に出す。chunk_id → entity・理由)と、同じ起点での
    # 計画(CRAG の各回で同じ起点なら SQL を読み直さない)。
    graph_expansions: dict[str, dict[str, object]] = field(default_factory=dict)
    graph_plans: dict[tuple[str, ...], list[Any]] = field(default_factory=dict)
    graph_attempts: dict[tuple[str, ...], dict[str, object]] = field(default_factory=dict)
    # 取込で参照先を解決していない chunk の参照先(#1382。chunk_id ごと)と、その解決に使う
    # 文書の見出しの列((document_id, chunk_set_id) ごと。1 回の回答で 1 回だけ読む)。
    query_reference_targets: dict[str, list[ReferenceTarget]] = field(default_factory=dict)
    reference_section_indexes: dict[tuple[str, str], SectionIndex] = field(default_factory=dict)
    # 他の文書への参照の文書名に合う文書((範囲, 文書名の比較形) ごと。当たり方の順。#1400)。
    reference_documents: dict[tuple[str, str], list[tuple[str, str]]] = field(default_factory=dict)
    # 参照の文書名の照合の DB の行(同じ key。辿る前に候補の文書名をまとめて 1 回で読む。#2137)。
    reference_document_rows: dict[tuple[str, str], list[tuple[str, str, tuple[str, ...]]]] = field(
        default_factory=dict
    )
    # 検索文ごとの検索の結果((検索文, 方式, 検索条件, 件数) ごと。#1490)。CRAG の 2 回目・要求ごとの
    # 再検索で同じ検索文を読み直さない(1 回の回答の中だけ)。
    search_hits: dict[tuple[str, str, tuple[tuple[str, str], ...], int], list[RetrievedChunk]] = (
        field(default_factory=dict)
    )
    # 章節の拡張(#1436)の章節の一覧((document_id, chunk_set_id) ごと。1 回の回答で 1 回だけ読む)。
    section_outlines: dict[tuple[str, str], Any] = field(default_factory=dict)
    # 条の目録(#2254。document_id ごと。条で構成された文書でなければ None)。1 回の回答で 1 回だけ
    # 作る。
    article_catalogs: dict[str, Any] = field(default_factory=dict)
    # 検索の範囲の条で構成された文書の一覧(#2254。1 回の回答で 1 回だけ読む)。
    article_documents: list[tuple[str, str]] | None = None
    # 目録で選んだ条(#2254。chunk_id か section_id ごと。診断の article_catalog に残す)。
    article_selections: dict[str, dict[str, object]] = field(default_factory=dict)
    # 利用者が明示した基準日(as_of。無ければ空)。検索の条件は 1 回の回答の初めに決めた基準日を
    # 使い(``_with_effective_as_of``。#2602)、回答のプロンプトと記録の分類の条件には明示した値
    # だけを渡す。
    requested_as_of: str = ""
    # 検索で当たった派生の chunk(章節の要約・RAPTOR の要約・項目抽出の値)と、置き換えた原文の
    # chunk(#2641。chunk_id → 種類・原文の chunk_id。CRAG の各回で読み直さない。診断に出す)。
    derived_hits: dict[str, dict[str, Any]] = field(default_factory=dict)
    derived_sources: dict[tuple[str, tuple[tuple[str, str], ...]], list[str]] = field(
        default_factory=dict
    )
    # 失敗して空のまま続けた補助の処理(#3259。``DegradedSteps``)。回答エンジンの前の段の失敗も
    # 同じ記録に入る(#3286)。
    degraded: DegradedSteps = field(default_factory=DegradedSteps, repr=False, compare=False)

    def record_degraded(self, kind: str, exc: BaseException) -> None:
        """補助の処理 ``kind`` が ``exc`` で失敗し、空のまま続けたことを数える(#3259)。"""
        self.degraded.record_degraded(kind, exc)

    def degraded_steps(self) -> list[dict[str, object]]:
        """失敗した補助の処理の記録(最初に失敗した順)。"""
        return self.degraded.steps()

    @property
    def auto_field_relaxed(self) -> bool:
        """読み取った抽出項目の条件を、候補 0 件で外したか(#652)。"""
        return RELAX_AUTO_FIELD in self.relaxed

    def active_relaxations(self) -> list[str]:
        """まだ外していない(今の検索条件に効いている)自動の条件の段の名前。"""
        return [name for name, _ in self.relaxations[len(self.relaxed) :]]


# 自動で足した検索条件の段(#2401)。外す順(質問から推測した度合いの強い順)に並ぶ。
RELAX_AUTO_FIELD = "auto_field"
RELAX_QUESTION_PAGE = "question_page"
RELAX_QUESTION_FILE = "question_file"
_QUESTION_PAGE_KEYS = ("page_number_min", "page_number_max")
# 根拠 0 件の診断に残す検索の記録の上限(CRAG の各回・要求ごとの再検索で増える)。
_ZERO_EVIDENCE_SEARCH_LOG_LIMIT = 12


def _relaxation_steps(
    base: Mapping[str, str], question: Mapping[str, str], full: Mapping[str, str]
) -> list[tuple[str, dict[str, str]]]:
    """自動で足した検索条件を外す段を、外す順に返す(#2401)。

    ``base`` は利用者が指定した条件(画面・検索・回答プロファイル)、``question`` は質問が名指しした
    文書名・ページを足した条件、``full`` はさらに質問から読み取った抽出項目の条件を足した条件。
    各段の検索条件は、その段までの自動の条件を外したもの。``base`` の条件は外さない。
    """
    steps: list[tuple[str, dict[str, str]]] = []
    if dict(full) != dict(question):
        steps.append((RELAX_AUTO_FIELD, dict(question)))
    # 質問が足した条件(``_question_filters`` は利用者が指定した項目には足さない)。
    added = {key for key, value in question.items() if base.get(key) != value}
    if pages := added & set(_QUESTION_PAGE_KEYS):
        steps.append(
            (
                RELAX_QUESTION_PAGE,
                {key: value for key, value in question.items() if key not in pages},
            )
        )
    if "file_name" in added:
        steps.append((RELAX_QUESTION_FILE, dict(base)))
    return steps


def _reference_scope(filters: Mapping[str, str]) -> dict[str, str]:
    """交差参照の参照先を探して読む範囲: ナレッジベース・旧版の扱い・基準日だけ(#1280・#3260)。

    ``filters`` は利用者が指定した条件(``request.filters``)。参照で足した chunk の章節を読むときも
    同じ範囲を使う(#3325)。利用者の権限は検索条件ではなく SQL の述語で当たる。
    """
    return {
        key: value
        for key, value in filters.items()
        if key in _REFERENCE_SCOPE_FILTER_KEYS and value.strip()
    }


def _section_chain(sections: Sequence[Any], owner_section_id: str) -> list[str]:
    """所有の章節から根までの章節の ID(章節の拡張が広げる列。``SectionOutline.chain`` と同じ)。"""
    nodes = {node.section_id: node for node in sections}
    chain: list[str] = []
    current = nodes.get(owner_section_id)
    while current is not None and current.section_id not in chain:
        chain.append(current.section_id)
        current = nodes.get(current.parent_section_id) if current.parent_section_id else None
    return chain


def _expansion_scope(filters: Mapping[str, str]) -> dict[str, str]:
    """実体・関係情報の拡張の範囲: 利用者が指定した検索条件をすべて残す(#3260)。

    ``filters`` は利用者が指定した条件(画面・検索・回答プロファイル・呼び出し元の
    ``request.filters``)で、ナレッジベース・旧版の扱い・基準日に加えて文書名・ページ・分類・項目の
    条件を含む。拡張は主の検索と同じ範囲の中で広げ、利用者が指定した範囲の外から黙って根拠を足さ
    ない。質問から推測して自動で足した条件(``_question_filters``・``merge_field_conditions``)は
    含まない(0 件で外す条件。#2401)。利用者の権限は検索条件ではなく SQL の述語で当たる。本文が
    明記した参照を辿る交差参照は、意図して文書名などの絞り込みを越える
    (``_REFERENCE_SCOPE_FILTER_KEYS``)。
    """
    return {key: value for key, value in filters.items() if value.strip()}


def answer_model_reads_images(settings: Settings, model_id: str) -> bool:
    """回答のモデルが画像を読めるか(モデル設定の登録モデルの ``vision_enabled``。#3444)。

    読めるモデルで答える回だけ、根拠の chunk の画像を切り出して回答の入力に添付する。回答の途中で
    別のモデル(既定の Vision モデル)へ切り替えない。
    """
    return any(
        model.model_id == model_id and model.vision_enabled
        for model in enterprise_ai_model_catalog(settings)
    )


def _attached_image_count(result: object) -> int:
    """回答のモデルへ添付した根拠の画像の件数(#3444。添付しなかった回は 0)。"""
    if getattr(result, "image_prompt_mode", "") != "vision_attachments":
        return 0
    return sum(
        1
        for item in getattr(result, "image_evidence", ()) or ()
        if isinstance(item, Mapping) and item.get("prompt_path")
    )


# 条の選択の記録(#3507)の既定の置き場所(PLATFORM_LOCAL_STORAGE_DIR の下)。
# 3 製品で同じ保存先を設定しても、RAG の名前の下に分ける。
_ARTICLE_CATALOG_CACHE_SUBDIR = Path("cache") / "rag-article-catalog"


def resolve_article_catalog_cache_dir(settings: Settings) -> Path | None:
    """条の選択の記録の置き場所(#3507)。明示の設定、無ければローカルの保存先の下。

    どちらも無ければ None(process をまたぐ記録を使わない)。
    """
    explicit = str(getattr(settings, "rag_article_catalog_cache_dir", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    root = str(getattr(settings, "local_storage_dir", "") or "").strip()
    return Path(root).expanduser() / _ARTICLE_CATALOG_CACHE_SUBDIR if root else None


def article_catalog_cache_dir(settings: Settings) -> Path | None:
    """rag_engine に渡す条の選択の記録の置き場所(テストは保存先を分けるため差し替える)。"""
    return resolve_article_catalog_cache_dir(settings)


def _reasoning_effort_override(value: str) -> str:
    """用途ごとの推論の量の設定を rag_engine へ渡す値にする(#1499。model は空 = 登録値のまま)。"""
    return "" if value == "model" else value


def _rerank_base_url_configured(settings: Settings) -> bool:
    """rag_engine の ``_rerank_configured`` と同じく、再ランクの Base URL が決まるか(#8)。

    ``rerank_base_url`` → ``embedding_base_url`` → Enterprise AI の接続の endpoint の順に見る。
    """
    names = (
        "rerank_base_url",
        "embedding_base_url",
        "oci_enterprise_ai_endpoint",
        "oci_enterprise_ai_secondary_endpoint",
        "oci_enterprise_ai_tertiary_endpoint",
    )
    return any(str(getattr(settings, name, "") or "").strip() for name in names)


def build_engine_settings(
    settings: Settings,
    *,
    output_dir: Path,
    runtime_knowledge_path: Path | None = None,
    answer_model_id: str | None = None,
    scope: AnswerScope | None = None,
) -> Any:
    """backend Settings から rag_engine の Settings を作る(env や .env は読まない)。

    ``answer_model_id`` を渡すと、回答のモデル(とその接続)をそのモデルにする。チャットの
    モデル比較で、列ごとのモデルで答えるために使う(#593)。省略時は既定のモデル。回答の実行の
    LLM の呼び出しはすべてこのモデルで行い、根拠の画像はこのモデルが読めるときだけ添付する
    (#3444)。既定の Vision モデル(``OCI_ENTERPRISE_AI_VLM_*``)は取り込みの処理(図の説明など)
    だけが使う。

    """
    from rag_engine.config import get_settings as engine_get_settings

    # 回答のモデルと Vision のモデルは、それぞれのモデルの接続で呼ぶ(#533)。
    answer_model = answer_model_id or enterprise_ai_default_model_id(settings)
    vision_model = enterprise_ai_vision_model_id(settings)
    answer = enterprise_ai_connection_for_model(settings, answer_model)
    vision = enterprise_ai_connection_for_model(settings, vision_model)
    environ = {
        # Endpoint URL は platform の関数で base URL にして渡す(#1440。rag_engine はそのまま使う)。
        "OCI_ENTERPRISE_AI_ENDPOINT": answer.base_url,
        "OCI_ENTERPRISE_AI_API_KEY": answer.api_key,
        "OCI_ENTERPRISE_AI_PROJECT_OCID": answer.project_ocid,
        "OCI_ENTERPRISE_AI_DEFAULT_MODEL": answer_model,
        # 回答のモデルが画像を読めるか(#3444)。読めるときだけ根拠の画像を回答の入力に添付する。
        "OCI_ENTERPRISE_AI_DEFAULT_MODEL_VISION": (
            "1" if answer_model_reads_images(settings, answer_model) else "0"
        ),
        "OCI_ENTERPRISE_AI_VLM_MODEL": vision_model,
        "OCI_ENTERPRISE_AI_VLM_ENDPOINT": vision.base_url,
        "OCI_ENTERPRISE_AI_VLM_API_KEY": vision.api_key,
        "OCI_ENTERPRISE_AI_VLM_PROJECT_OCID": vision.project_ocid,
        # 推論(思考)の量はモデルごとの送る値(#1425。none は空 = 送らない)。
        "OCI_ENTERPRISE_AI_REASONING_EFFORT": enterprise_ai_reasoning_effort_to_send(
            settings, answer_model
        ),
        "OCI_ENTERPRISE_AI_VLM_REASONING_EFFORT": enterprise_ai_reasoning_effort_to_send(
            settings, vision_model
        ),
        # 回答のモデルの入力の上限(tokens。#1437)。章節の拡張の根拠の入力の上限と、抽出してから
        # 統合の 1 回分の大きさを、この値の割合で決める(値の解釈は platform の 1 か所)。
        "OCI_ENTERPRISE_AI_MAX_INPUT_TOKENS": str(
            enterprise_ai_max_input_tokens(settings, answer_model)
        ),
        # 回答のモデルと Vision のモデルの出力の上限(tokens。#1719)。rag_engine は回答の上限
        # (answer_max_tokens。既定 32768)とこの値の小さい方を送り、未完了の送り直しもこの値で抑える
        # (値の解釈は platform の enterprise_ai_output_token_budget。prompt は見積もらない)。
        "OCI_ENTERPRISE_AI_MAX_OUTPUT_TOKENS": str(
            enterprise_ai_output_token_budget(settings, answer_model)
        ),
        "OCI_ENTERPRISE_AI_VISION_MAX_OUTPUT_TOKENS": str(
            enterprise_ai_output_token_budget(settings, vision_model)
        ),
        # 埋め込みと再ランクは OpenAI 互換の API を呼ぶ(#8)。Base URL を空にすると
        # rag_engine が LLM の既定の接続の endpoint から組み立てる(百煉は chat・埋め込みが
        # 同じ /compatible-mode/v1 で、再ランクだけ /compatible-api/v1)。model を渡さないと
        # RAG_RERANK_ENABLED=true でも常に「未実行」になる(#275)。
        "EMBEDDING_MODEL": settings.oci_genai_embedding_model,
        "EMBEDDING_OUTPUT_DIMENSIONS": str(int(settings.oci_genai_embedding_dim)),
        "EMBEDDING_BASE_URL": settings.embedding_base_url,
        "RERANK_MODEL": settings.oci_genai_rerank_model,
        "RERANK_BASE_URL": settings.rerank_base_url,
        "RAG_ENGINE_OUTPUT_DIR": str(output_dir),
        "RAG_ENGINE_RETRIEVAL_CANDIDATE_LIMIT": str(settings.rag_retrieval_candidate_limit),
        "RAG_ENGINE_RERANK_CANDIDATE_LIMIT": str(settings.rag_rerank_candidate_limit),
        "RAG_ENGINE_RETRIEVAL_STAGE_LIMITS": json.dumps(
            {
                "fusion": settings.rag_retrieval_candidate_limit,
                "diversity": settings.rag_candidate_diversity_max_extra
                if settings.rag_candidate_diversity_depth > 0
                else 0,
                "reference": settings.rag_reference_expansion_max_chunks
                if settings.rag_reference_expansion_enabled
                else 0,
                "entity": settings.rag_entity_expansion_max_chunks
                if settings.rag_entity_expansion_enabled
                else 0,
                "structure": settings.rag_graph_expansion_max_chunks
                if settings.rag_graph_expansion_enabled
                else 0,
            }
        ),
        # 「今日」(基準日の既定)の時間帯(共通の設定 PLATFORM_TIMEZONE。#3249)。
        "PLATFORM_TIMEZONE": configured_timezone_name(settings),
        "LLM_REQUEST_TIMEOUT_SECONDS": str(int(settings.oci_enterprise_ai_timeout_seconds)),
        "LLM_RETRIES": str(int(settings.oci_enterprise_ai_max_retries)),
        # 画面目録で操作画面を探す(#554)。rag_engine は明示した environ だけを読む。
        "RAG_ENGINE_SCREEN_LINKING": "1" if settings.rag_screen_linking_enabled else "0",
        # 条の目録から根拠の条を選ぶ段(#2254)。
        "RAG_ENGINE_ARTICLE_CATALOG": "1" if settings.rag_article_catalog_enabled else "0",
        "RAG_ENGINE_ARTICLE_CATALOG_MAX_DOCUMENTS": str(settings.rag_article_catalog_max_documents),
        "RAG_ENGINE_ARTICLE_CATALOG_MAX_ARTICLES": str(settings.rag_article_catalog_max_articles),
        "RAG_ENGINE_ARTICLE_CATALOG_CONTEXT_RECORDS": str(
            settings.rag_article_catalog_context_records
        ),
        "RAG_ENGINE_ARTICLE_CATALOG_SCOPE_DOCUMENTS": str(
            settings.rag_article_catalog_scope_documents
        ),
        "RAG_ENGINE_ARTICLE_CATALOG_ORIGINAL_QUESTION_SEARCH": (
            "1" if settings.rag_article_catalog_original_question_search else "0"
        ),
        "RAG_ENGINE_ARTICLE_CATALOG_REFINE_ARTICLES": str(
            settings.rag_article_catalog_refine_articles
        ),
        "RAG_ENGINE_ARTICLE_CATALOG_REFINE_CANDIDATES": str(
            settings.rag_article_catalog_refine_candidates
        ),
        "RAG_ENGINE_ARTICLE_CATALOG_OUTPUT_TOKEN_CAP": str(
            settings.rag_article_catalog_output_token_cap
        ),
        "RAG_ENGINE_ARTICLE_CATALOG_CALL_TIMEOUT_SECONDS": str(
            settings.rag_article_catalog_call_timeout_seconds
        ),
        "RAG_ENGINE_ARTICLE_CATALOG_REVIEW": (
            "1" if settings.rag_article_catalog_review_enabled else "0"
        ),
        # 条の選択の結果を process をまたいで使う記録(#3507)。置き場所が無ければ process の中だけ。
        "RAG_ENGINE_ARTICLE_CATALOG_CACHE_DIR": str(article_catalog_cache_dir(settings) or ""),
        "RAG_ENGINE_ARTICLE_CATALOG_CACHE_TTL_SECONDS": str(
            settings.rag_article_catalog_cache_ttl_seconds
        ),
        "RAG_ENGINE_ARTICLE_CATALOG_CACHE_MAX_ENTRIES": str(
            settings.rag_article_catalog_cache_max_entries
        ),
        # 回答の最終の検証の入力の上限。
        "RAG_ENGINE_ANSWER_VALIDATION_MAX_INPUT_BYTES": str(
            settings.rag_answer_validation_max_input_bytes
        ),
        # 根拠の無い要求だけの再検索と予算(#1279)。
        "RAG_ENGINE_REQUEST_COVERAGE_RETRIEVAL": (
            "1" if settings.rag_request_coverage_retrieval_enabled else "0"
        ),
        "RAG_ENGINE_REQUEST_COVERAGE_MAX_QUERIES": str(settings.rag_request_coverage_max_queries),
        "RAG_ENGINE_REQUEST_COVERAGE_MAX_CHUNKS": str(settings.rag_request_coverage_max_chunks),
        "RAG_ENGINE_REQUEST_COVERAGE_DEADLINE_SECONDS": str(
            settings.rag_request_coverage_deadline_seconds
        ),
        # 小問ごとの検索の上限(#2399。期限は再検索と共通)。
        "RAG_ENGINE_REQUEST_COVERAGE_MAX_SUBQUESTIONS": str(
            settings.rag_request_coverage_max_subquestions
        ),
        "RAG_ENGINE_REQUEST_COVERAGE_CHUNKS_PER_SUBQUESTION": str(
            settings.rag_request_coverage_chunks_per_subquestion
        ),
        # 追加の検索の枠と、業務ガイドの手順ごとの検索(#2395)。
        "RAG_ENGINE_EXTRA_RETRIEVAL_MAX_QUERIES": str(settings.rag_extra_retrieval_max_queries),
        # 章節の抜き出しの LLM(#2800)。
        "RAG_ENGINE_SECTION_EXTRACTION_ENABLED": "1"
        if settings.rag_section_extraction_enabled
        else "0",
        "RAG_ENGINE_GUIDE_STEP_RETRIEVAL": "1"
        if settings.rag_support_guide_enabled and settings.rag_guide_step_retrieval_enabled
        else "0",
        "RAG_ENGINE_GUIDE_STEP_MAX_QUERIES": str(settings.rag_guide_step_max_queries),
        "RAG_ENGINE_GUIDE_STEP_CHUNKS_PER_STEP": str(settings.rag_guide_step_chunks_per_step),
        "RAG_ENGINE_GUIDE_REFERENCE_MAX_CHUNKS": str(settings.rag_guide_reference_max_chunks),
        "RAG_ENGINE_GUIDE_STEP_DEADLINE_SECONDS": str(settings.rag_guide_step_deadline_seconds),
        # 当たった chunk の章節から親の章節へ段階的に広げる(#1436。検索方法の全体設定で選ぶ)。
        "RAG_ENGINE_SECTION_EXPANSION": "1" if settings.rag_section_expansion_enabled else "0",
        "RAG_ENGINE_SECTION_EXPANSION_MAX_LEVELS": str(settings.rag_section_expansion_max_levels),
        "RAG_ENGINE_SECTION_EXPANSION_INPUT_TOKEN_PERCENT": str(
            settings.rag_section_expansion_input_token_percent
        ),
        # 生成に渡す根拠の原文の文字予算(#3776。全体設定だけで決める)。
        "RAG_ENGINE_EVIDENCE_BUDGET_CHARS": str(settings.rag_evidence_budget_chars),
        # rerank の後、起点に採る同じ文書の chunk の上限(#2167。検索・回答プロファイルで選ぶ)。
        "RAG_ENGINE_MAX_ANCHORS_PER_DOCUMENT": str(settings.rag_max_anchors_per_document),
        # 用途ごとの推論の量(#1499。model は空 = モデルの登録値のまま)・CRAG の評価の省略・
        # 検索文の上限。
        "RAG_ENGINE_STRATEGY_REASONING_EFFORT": _reasoning_effort_override(
            settings.rag_strategy_reasoning_effort
        ),
        "RAG_ENGINE_JUDGE_REASONING_EFFORT": _reasoning_effort_override(
            settings.rag_judge_reasoning_effort
        ),
        "RAG_ENGINE_GENERATION_REASONING_EFFORT": _reasoning_effort_override(
            settings.rag_generation_reasoning_effort
        ),
        "RAG_ENGINE_AUDIT_REASONING_EFFORT": _reasoning_effort_override(
            settings.rag_audit_reasoning_effort
        ),
        "RAG_ENGINE_CRAG_SKIP_ON_STRONG_SIGNALS": (
            "1" if settings.rag_crag_skip_on_strong_signals else "0"
        ),
        # CRAG の評価の入力の圧縮・読む範囲まで読んだ後の評価し直しの省略(#1528)。
        "RAG_ENGINE_CRAG_COMPACT_INPUT": "1" if settings.rag_crag_compact_input else "0",
        # 詰めた入力の候補の ID を短くする(#1864)。
        "RAG_ENGINE_CRAG_SHORT_CANDIDATE_IDS": (
            "1" if settings.rag_crag_short_candidate_ids else "0"
        ),
        # 監査の通った判定の reason を空にする・是正の組の回数の上限(#1912)。
        "RAG_ENGINE_ANSWER_AUDIT_COMPACT_REASONS": (
            "1" if settings.rag_answer_audit_compact_reasons else "0"
        ),
        "RAG_ENGINE_ANSWER_MAX_CORRECTION_ROUNDS": str(settings.rag_answer_max_correction_rounds),
        # 回答の監査の型(#2795)。
        "RAG_ENGINE_ANSWER_AUDIT_MODE": settings.rag_answer_audit_mode,
        "RAG_ENGINE_ANSWER_EVIDENCE_REFINEMENT_MODE": settings.rag_answer_evidence_refinement_mode,
        # 停滞した LLM の呼び出しの打ち切りと送り直し(#1926)。
        "RAG_ENGINE_LLM_STALL_DETECTION": "1" if settings.rag_llm_stall_detection_enabled else "0",
        "RAG_ENGINE_LLM_STALL_IDLE_SECONDS": str(settings.rag_llm_stall_idle_seconds),
        "RAG_ENGINE_LLM_STALL_MIN_RATE_RATIO": str(settings.rag_llm_stall_min_rate_ratio),
        "RAG_ENGINE_LLM_STALL_RESENDS": str(settings.rag_llm_stall_resends),
        "RAG_ENGINE_LLM_RUNAWAY_DETECTION": "1"
        if settings.rag_llm_runaway_detection_enabled
        else "0",
        # 業務ガイドの手順の段のために最初の章節の拡張が残す入力の割合(#2816)。
        "RAG_ENGINE_SECTION_EXPANSION_FOLLOWUP_RESERVE_PERCENT": str(
            settings.rag_section_expansion_followup_reserve_percent
        ),
        "RAG_ENGINE_SECTION_SKIP_REGRADE_AT_SCOPE": (
            "1" if settings.rag_section_skip_regrade_at_scope else "0"
        ),
        "RAG_ENGINE_CRAG_SKIP_MIN_RERANK_SCORE": str(settings.rag_crag_skip_min_rerank_score),
        "RAG_ENGINE_QUERY_EXPANSION_MAX_QUERIES": str(settings.rag_query_expansion_max_queries),
    }
    if runtime_knowledge_path is not None:
        environ["RUNTIME_KNOWLEDGE_PATH"] = str(runtime_knowledge_path)
    engine_settings = engine_get_settings(environ=environ, dotenv_path=None)
    premises = _confirmed_premise_records(settings, scope)
    return replace(
        engine_settings,
        domain_keywords_override=tuple(settings.rag_domain_keywords),
        confirmed_premises=tuple((item["question"], item["answer"]) for item in premises),
        confirmed_premise_sources=tuple(
            (item["question"], item["answer"], item["source"]) for item in premises
        ),
        known_conditions=guide_known_conditions(
            settings.rag_support_guide if settings.rag_support_guide_enabled else {}
        ),
        evidence_conditions=guide_evidence_conditions(
            settings.rag_support_guide if settings.rag_support_guide_enabled else {}
        ),
        evidence_plans=guide_evidence_plans(
            settings.rag_support_guide if settings.rag_support_guide_enabled else {}
        ),
    )


def guide_known_conditions(guide: Mapping[str, object] | None) -> tuple[tuple[str, str], ...]:
    """業務ガイドの照合で分かっている条件(名前と値。#1492)。

    MCP の ``conditions``・質問の文で決まった条件で、回答の確認の項目のうちこれを確かめる項目を
    rag_engine が決定的に除く(分かっている条件を聞き直さない)。
    """
    known = (guide or {}).get("known_conditions")
    if not isinstance(known, list):
        return ()
    pairs: list[tuple[str, str]] = []
    for item in known:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if label and value:
            pairs.append((label, value))
    return tuple(pairs)


@dataclass(frozen=True)
class AnswerScope:
    """利用者が確認の質問で選んだ条件と対象範囲(#717)。"""

    # 回答のプロンプトの前置きに入れる説明(質問・選んだ選択肢・前提・その他・対象範囲)。
    context: str
    # 補助の検索文に足す語。
    search_terms: tuple[str, ...] = ()
    # 回答の末尾に出す対象の範囲(例: 「出張旅費規程」の「第6条」(p.2))。
    label: str = ""
    # 実際に選んだ答え・自由入力だけ。設定された指示や仮定を利用者の事実にしない。
    confirmed_premises: tuple[tuple[str, str], ...] = ()
    confirmed_premise_sources: tuple[tuple[str, str, str], ...] = ()


def _confirmed_premise_records(
    settings: Settings, scope: AnswerScope | None
) -> list[dict[str, str]]:
    """利用者の確定した前提の出所を保持する。展示用の自由文から事実を作らない。"""
    records: dict[tuple[str, str], dict[str, str]] = {}
    if scope is not None:
        sources: dict[tuple[str, str], str] = {}
        for question, answer, source in scope.confirmed_premise_sources:
            sources.setdefault((question, answer), source)
        for question, answer in scope.confirmed_premises:
            # 既存scopeの明示的な組は、確認で実際に答えた値。質問から認識した組は出所を伴う。
            source = sources.get((question, answer), "user")
            if source in {"user", "question"} and answer.strip():
                records.setdefault(
                    (question, answer), {"question": question, "answer": answer, "source": source}
                )
    known = (settings.rag_support_guide or {}).get("known_conditions")
    if settings.rag_support_guide_enabled and isinstance(known, list):
        for item in known:
            if (
                not isinstance(item, Mapping)
                or item.get("source") not in {"user", "question"}
                or item.get("state") != "known"
            ):
                continue
            question = str(item.get("label") or "").strip()
            answer = str(item.get("value") or "").strip()
            if question and answer:
                records.setdefault(
                    (question, answer),
                    {"question": question, "answer": answer, "source": str(item["source"])},
                )
    return list(records.values())


def _confirmed_validation_premises(
    settings: Settings, scope: AnswerScope | None
) -> list[dict[str, str]]:
    """最終照合の既存の質問・回答契約へ、同じ確定前提だけを写す。"""
    return [
        {"question": item["question"], "answer": item["answer"]}
        for item in _confirmed_premise_records(settings, scope)
    ]


class AnswerEngine:
    """rag_poc 回答フローを backend の I/O で駆動する。"""

    def __init__(
        self,
        settings: Settings,
        *,
        oracle: OracleClient,
        genai: OciGenAiClient,
        runtime_knowledge_payload: Mapping[str, object] | None = None,
        answer_model_id: str | None = None,
        auto_field_conditions: Sequence[ExtractionFieldCondition] = (),
        approved_faq: tuple[str, str] | None = None,
        scope: AnswerScope | None = None,
        entity_store: EntityExpansionStore | None = None,
        graph_store: GraphExpansionStore | None = None,
        attachment_chunks: Sequence[RetrievedChunk] = (),
        caller_queries: Sequence[str] | None = None,
        degraded: DegradedSteps | None = None,
        complete_evidence: bool = True,
    ) -> None:
        # 回答を作らない検索で、根拠の補い（章節の拡張・要求ごとの探し直し・CRAG）までするか
        # （#3506）。False は主の文書の検索の後で止める（業務 Agent の根拠の取得。補いは Agent が
        # MCP でする）。
        # RAG 検索の画面は True（チャットと同じ根拠を集め、回答の生成の手前で止める）。
        self._complete_evidence = complete_evidence
        # 呼び出し元（業務 Agent）が作った検索文（#2651）。None 以外なら、回答を作らない検索で、
        # 質問に応じた推論の LLM（検索戦略の判定・質問拡張・条の目録の選択・画面目録の連携）と根拠の
        # 画像の切り出しを設定に関わらず使わない。決定的な工程（規則の質問の理解・用語の別名・
        # hybrid search・rerank・親子と前後の文脈・交差参照・実体の拡張）は通常の検索と同じ。
        self._caller_queries = tuple(caller_queries) if caller_queries is not None else None
        if self._caller_queries is not None:
            settings = settings.model_copy(
                update={
                    "rag_query_strategy": CALLER_QUERY_STRATEGY,
                    "rag_article_catalog_enabled": False,
                    "rag_screen_linking_enabled": False,
                }
            )
        if not settings.rag_support_guide_enabled:
            settings = settings.model_copy(update={"rag_support_guide": {}})
        self._settings = settings
        self._oracle = oracle
        # チャットの会話の添付から選んだ根拠の候補（#2517。`app.rag.chat_attachments`）。検索の
        # 結果の先頭に足し、rerank・文脈・引用は検索の chunk と同じに扱う。
        self._attachment_chunks = list(attachment_chunks)
        # 実体の表を読む SQL(#1362)。None は Oracle(``EntityStore``)。テストは同じ規則の表を渡す。
        self._entity_store = entity_store
        # 関係情報の表を読む SQL(#2055)。None は Oracle(``OracleGraphStore``)。
        self._graph_store = graph_store
        self._genai = genai
        self._runtime_knowledge_payload = runtime_knowledge_payload
        # 回答のモデル(チャットのモデル比較の列のモデル。#593)。None は既定のモデル。
        self._answer_model_id = answer_model_id or None
        # 回答のモデルが画像を読めるなら、根拠の chunk の画像を切り出して回答の入力に添付する
        # (#3444)。回答を作らない検索(呼び出し元の検索文)は切り出さない。
        self._attach_images = self._caller_queries is None and answer_model_reads_images(
            settings, self._answer_model_id or enterprise_ai_default_model_id(settings)
        )
        # 質問から読み取った抽出項目の条件(#652)。手の条件の項目には足さない。
        self._auto_field_conditions = list(auto_field_conditions)
        # 利用者が選んだ類似の承認済み FAQ(質問・承認済みの回答)。選んだときは検索せず、質問と FAQ
        # だけから回答する(#684 / #702)。
        self._approved_faq = approved_faq
        # 利用者が確認の質問で選んだ条件と対象範囲(#717)。範囲での絞り込みは request.filters の
        # page_ranges が行い、ここでは回答の前提・検索に足す語・回答の末尾の表示に使う。
        self._scope = scope
        # 回答エンジンの前の段(会話の履歴での書き換え・抽出項目の条件・会話の添付)が失敗して
        # 縮退した記録(#3286)。回答の中の補助の処理の失敗も同じ記録に足し、回答の診断の
        # degraded_steps に出す。
        self._degraded = degraded

    async def run(
        self, request: SearchRequest, *, step_callback: StepCallback | None = None
    ) -> AnswerOutcome:
        """回答する。``step_callback`` は回答フローの各工程の開始・終了を受け取る(進捗。#593)。"""
        if self._approved_faq is not None:
            return await self._answer_from_approved_faq(request, step_callback=step_callback)
        # 回答の中の読み取りは、chunk の本文(CLOB)を行ごとに読み直さず値で受け取る(#1490)。
        with read_lobs_as_values():
            return await self._run(request, step_callback=step_callback)

    async def _run(
        self, request: SearchRequest, *, step_callback: StepCallback | None
    ) -> AnswerOutcome:
        loop = asyncio.get_running_loop()
        requested = request
        # 基準日を 1 回の回答で 1 回だけ決め、主検索と拡張の検索で同じ日を使う(#2602)。
        request = _with_effective_as_of(request)
        state = _SearchState(
            requested_as_of=requested.filters.get(AS_OF_FILTER_KEY, ""),
            degraded=self._degraded if self._degraded is not None else DegradedSteps(),
        )
        try:
            overrides = saved_prompt_overrides()
            if overrides is None:
                overrides = await self._oracle.answer_prompt_overrides()
        except Exception as exc:  # noqa: BLE001 - 編集したプロンプトは補助。既定値で回答を続ける。
            logger.warning("answer prompt overrides load failed", exc_info=True)
            state.record_degraded("prompt_overrides", exc)
            overrides = {}
        business_names = await self._business_names(request, state)
        with tempfile.TemporaryDirectory(prefix="rag-engine-answer-") as work:
            work_dir = Path(work)
            state.work_dir = work_dir
            runtime_path = None
            if self._runtime_knowledge_payload:
                runtime_path = work_dir / "runtime_knowledge.json"
                runtime_path.write_text(
                    json.dumps(self._runtime_knowledge_payload, ensure_ascii=False),
                    encoding="utf-8",
                )
            engine_settings = build_engine_settings(
                self._settings,
                output_dir=work_dir,
                runtime_knowledge_path=runtime_path,
                answer_model_id=self._answer_model_id,
                scope=self._scope,
            )
            # 回答の専用の thread で動かし、打ち切り・取り消しを thread に伝える(#1422)。
            result = await run_answer_in_thread(
                self._answer_sync,
                request,
                engine_settings,
                loop,
                state,
                overrides,
                business_names,
                step_callback,
            )
        outcome = _outcome_from_result(result, state)
        from rag_parser_core.language import current_pack

        outcome.diagnostics["answer_format"] = {
            key: value
            for key, value in current_pack().sections["answer_labels"].items()
            if isinstance(value, str)
        }
        if self._scope is not None:
            outcome.diagnostics["scope"] = {
                "label": self._scope.label,
                "context": self._scope.context,
                "page_ranges": request.filters.get("page_ranges", ""),
            }
            if self._scope.label and outcome.answer.strip():
                outcome = replace(
                    outcome, answer=f"{outcome.answer}\n\n（対象: {self._scope.label}）"
                )
        await self._validate_outcome(request, outcome, step_callback=step_callback)
        outcome.diagnostics["models"] = self._models_used(
            attached_images=_attached_image_count(result),
            rerank_failed=state.degraded.failed("rerank"),
        )
        # 回答を作ったプロンプトの版（編集した内容とコードのプロンプト。#1276）。
        outcome.diagnostics.setdefault("provenance", {})["prompt_version"] = answer_prompt_version(
            overrides
        )
        if state.entity_expansions:
            outcome.diagnostics["entity_expansion"] = {
                "added_count": len(state.entity_expansions),
                "max_chunks": self._settings.rag_entity_expansion_max_chunks,
                "chunks": list(state.entity_expansions.values())[:_REFERENCE_DIAGNOSTICS_LIMIT],
            }
        if state.article_selections:
            outcome.diagnostics["article_catalog"] = {
                "documents": [
                    catalog.file_name
                    for catalog in state.article_catalogs.values()
                    if catalog is not None
                ],
                "max_documents": self._settings.rag_article_catalog_max_documents,
                "context_records": self._settings.rag_article_catalog_context_records,
                "articles": list(state.article_selections.values()),
            }
        if self._settings.rag_graph_expansion_enabled:
            outcome.diagnostics["graph_expansion"] = _graph_expansion_diagnostics(
                state.graph_expansions,
                max_chunks=self._settings.rag_graph_expansion_max_chunks,
                attempts=list(state.graph_attempts.values()),
            )
        if state.reference_expansions or any(
            search.get("sources") for search in state.reference_sources
        ):
            outcome.diagnostics["reference_expansion"] = {
                "added_count": len(state.reference_expansions),
                "max_chunks": self._settings.rag_reference_expansion_max_chunks,
                "chunks": list(state.reference_expansions.values())[:_REFERENCE_DIAGNOSTICS_LIMIT],
                # 検索ごとの、参照を持つ候補の統合の順位と辿った結果・辿らなかった理由(#1549)。
                "searches": list(state.reference_sources),
            }
        if state.derived_hits:
            # 当たった派生の chunk と、根拠に置き換えた原文(#2641)。原文が無いものは根拠に
            # 渡していない。
            hits = list(state.derived_hits.values())
            outcome.diagnostics["derived_evidence"] = {
                "resolved_count": sum(1 for hit in hits if hit["source_chunk_ids"]),
                "unresolved_count": sum(1 for hit in hits if not hit["source_chunk_ids"]),
                "chunks": hits[:_REFERENCE_DIAGNOSTICS_LIMIT],
            }
        if state.auto_field_conditions:
            outcome.diagnostics["auto_field_filter"] = {
                "conditions": [condition.model_dump() for condition in state.auto_field_conditions],
                "relaxed": state.auto_field_relaxed,
            }
        if state.relaxed:
            # 候補 0 件で外した自動の条件の段(#2401)。
            outcome.diagnostics["filter_relaxation"] = {
                "relaxed": list(state.relaxed),
                "max_steps": self._settings.rag_filter_relaxation_max_steps,
            }
        if degraded := state.degraded_steps():
            # 失敗して空のまま続けた補助の処理(#3259)。根拠 0 件の診断にも入れる。
            outcome.diagnostics["degraded_steps"] = degraded
        if not outcome.citations and (
            zero_evidence := _zero_evidence_diagnostics(result, state, requested)
        ):
            outcome.diagnostics["zero_evidence"] = zero_evidence
        return outcome

    async def _validate_outcome(
        self,
        request: SearchRequest,
        outcome: AnswerOutcome,
        *,
        step_callback: StepCallback | None = None,
    ) -> None:
        """LLM 監査が有効な回答の最終の本文を、MCP と共通の検証で確かめる。"""
        if self._settings.rag_answer_audit_mode == "none" or not outcome.answer.strip():
            outcome.diagnostics["answer_validation"] = {
                "status": "not_run",
                "valid": False,
                "reason": "audit_disabled"
                if self._settings.rag_answer_audit_mode == "none"
                else "empty_answer",
            }
            if semantic_audit_is_diagnostic_only(outcome.diagnostics):
                outcome.diagnostics["answer_validation"] = _diagnostic_validation(
                    outcome.diagnostics["answer_validation"]
                )
            return
        # answer_validation は engine の設定変換も使うため、循環 import を避けてここで読む。
        from app.rag.answer_validation import (
            MAX_PREMISES,
            AnswerValidation,
            EvidenceRef,
            validate_answer,
        )

        name = ANSWER_VALIDATION_STEP
        started = time.monotonic()
        if step_callback is not None:
            await step_callback(name, "started", 0.0, {})
        state = "success"
        llm_usage: dict[str, Any] = {}
        diagnostic_only = semantic_audit_is_diagnostic_only(outcome.diagnostics)
        # 回答フローの工程と同じ工程の span にする（処理の経過の行。#3226）。
        with _answer_step_span(name) as span:
            try:
                premises = _confirmed_validation_premises(self._settings, self._scope)
                if len(premises) > MAX_PREMISES:
                    # 一部の確定条件を黙って省いて「確認済み」にしない。
                    result = AnswerValidation(status="input_too_large", valid=False)
                else:
                    result = await validate_answer(
                        request.original_question or request.query,
                        outcome.answer,
                        [
                            EvidenceRef(chunk.document_id, chunk.chunk_id)
                            for chunk in outcome.citations
                        ],
                        self._settings,
                        oracle=self._oracle,
                        premises=premises,
                    )
                llm_usage = dict(result.llm_usage)
                validation: dict[str, Any] = {
                    "status": result.status,
                    "valid": result.valid,
                    "reuse_context": result.reuse_context,
                    "counts": dict(result.counts),
                    "claim_checks": list(result.claim_checks),
                    # 原質問・確認の答えとの両立の比較の、段落ごとの採否（#3800。ID と code だけ）。
                    "premise_consistency_checks": list(result.premise_consistency_checks),
                    "missing_evidence_count": len(result.missing_evidence),
                    "stale_evidence_count": len(result.stale_evidence),
                    "confirmed_premises_count": len(premises),
                    "unassessed_premises_count": len(premises)
                    if len(premises) > MAX_PREMISES
                    else 0,
                }
            except Exception as exc:  # noqa: BLE001 - 検証の失敗は未確認として残し、回答の記録を失わない。
                if isinstance(exc, (DeadlineExceeded, PermissionError)) or (
                    isinstance(exc, HTTPException) and exc.status_code in {401, 403}
                ):
                    raise
                logger.warning("answer final validation failed", exc_info=True)
                validation = {
                    "status": "timed_out"
                    if isinstance(exc, (TimeoutError, httpx.TimeoutException, APITimeoutError))
                    else "error",
                    "valid": False,
                }
                state = "error"
                span.set_status("failed")
            if diagnostic_only:
                validation = _diagnostic_validation(validation)
            span.set_display(
                params=answer_step_params({"llm_calls": int(llm_usage.get("calls") or 0)}),
                details=answer_step_details(_validation_details(validation)),
            )
        if diagnostic_only:
            outcome.diagnostics["needs_human_review"] = True
        outcome.diagnostics["answer_validation"] = validation
        deterministic_failure = bool(
            validation.get("missing_evidence_count") or validation.get("stale_evidence_count")
        )
        if deterministic_failure or (not diagnostic_only and not validation["valid"]):
            outcome.diagnostics["needs_human_review"] = True
            outcome.diagnostics["confidence"] = "low"
            envelope = outcome.diagnostics.get("envelope")
            if isinstance(envelope, dict) and envelope.get("outcome") == "answered":
                envelope["outcome"] = "conditional"
            if outcome.diagnostics.get("outcome") == "answered":
                outcome.diagnostics["outcome"] = "conditional"
            reason = (
                "資料との照合で回答の裏付けを確認できていません。内容と根拠を確認してください。"
            )
            previous = str(outcome.diagnostics.get("insufficient_reason") or "")
            outcome.diagnostics["insufficient_reason"] = "\n".join(filter(None, (previous, reason)))
        elapsed = round(time.monotonic() - started, 3)
        details = _validation_details(validation)
        metrics = {
            "llm_calls": int(llm_usage.get("calls") or 0),
            "llm_usage": llm_usage,
            "details": details,
        }
        outcome.diagnostics.setdefault("execution_steps", []).append(
            {
                "name": name,
                "status": "complete" if state == "success" else "error",
                "elapsed_seconds": elapsed,
                **metrics,
            }
        )
        if step_callback is not None:
            await step_callback(name, state, elapsed, metrics)

    async def _answer_from_approved_faq(
        self, request: SearchRequest, *, step_callback: StepCallback | None = None
    ) -> AnswerOutcome:
        """利用者が選んだ類似問の承認済み FAQ だけから回答する(検索しない。#702)。"""
        from rag_engine.generation.faq_answer import answer_from_approved_faq
        from rag_engine.resources.llm_usage import bind_llm_usage

        assert self._approved_faq is not None
        faq_question, faq_answer = self._approved_faq
        name = "承認済み FAQ からの回答"
        started = time.monotonic()
        if step_callback is not None:
            await step_callback(name, "started", 0.0, {})
        status = "success"
        # 回答フローの工程と同じ工程の span にする（処理の経過の行。#3226）。
        with _answer_step_span(name) as span, bind_llm_usage() as usage:
            try:
                with tempfile.TemporaryDirectory(prefix="rag-engine-faq-") as work:
                    engine_settings = build_engine_settings(
                        self._settings,
                        output_dir=Path(work),
                        runtime_knowledge_path=None,
                        answer_model_id=self._answer_model_id,
                    )
                    answer = await run_answer_in_thread(
                        answer_from_approved_faq,
                        request.query,
                        faq_question,
                        faq_answer,
                        engine_settings,
                    )
            except BaseException:
                status = "error"
                raise
            finally:
                elapsed = time.monotonic() - started
                metrics = {"llm_calls": 1, "llm_usage": usage.snapshot()}
                span.set_display(params=answer_step_params({"llm_calls": 1}))
                if step_callback is not None:
                    await step_callback(name, status, elapsed, metrics)
        models = self._models_used()
        models["embedding"] = ""
        models["rerank"] = ""
        # 回答エンジンの前の段(会話の履歴での書き換え・抽出項目の条件)の縮退(#3286)。
        degraded = self._degraded.steps() if self._degraded is not None else []
        return AnswerOutcome(
            answer=f"{answer}\n\n（出典: 承認済み FAQ「{faq_question}」）",
            citations=[],
            diagnostics={
                "answer_source": "approved_faq",
                # 承認済みの回答をそのまま使うので、対応は「答えた」（#1235）。
                "outcome": "answered",
                "envelope": {"schema_version": ENVELOPE_SCHEMA_VERSION, "outcome": "answered"},
                "approved_faq_question": faq_question,
                # 回答した時点の承認済みの回答。FAQ が変わっても根拠の原文を出せる(#737)。
                "approved_faq_answer": faq_answer,
                "models": models,
                "execution_steps": [
                    {"name": name, "status": "complete", "elapsed_seconds": elapsed, **metrics}
                ],
                **({"degraded_steps": degraded} if degraded else {}),
            },
            # 回答の安全チェックが照合する根拠は、選んだ FAQ だけ。
            context_text=f"類似問: {faq_question}\n承認済みの回答: {faq_answer}",
        )

    def _models_used(
        self, *, attached_images: int = 0, rerank_failed: bool = False
    ) -> dict[str, Any]:
        """回答フローで使ったモデル(画面に出す。#649)。

        LLM(既定のテキストモデル。比較の列ではその列のモデル)は質問の理解・拡張・回答の生成・
        監査のすべてに使う(#3444)。``attached_images`` はそのモデルへ添付した根拠の画像の件数
        (画像を読めないモデル・画像の無い根拠は 0)。
        rerank は実際に並べたときだけモデル名を出す: 無効・未設定(モデルか Base URL が無い。#8。
        rag_engine はこのとき rerank を実行しない)なら空。rerank が失敗して取得の順で続けたとき
        (``rerank_failed``)も空にし、rerank で並べたように見せない(失敗は degraded_steps の
        ``rerank`` に出る。#3286)。
        """
        settings = self._settings
        labels = {
            model.model_id: model.display_name or model.model_id
            for model in enterprise_ai_model_catalog(settings)
        }

        def model(model_id: str) -> dict[str, str]:
            return {"model_id": model_id, "label": labels.get(model_id, model_id)}

        return {
            "llm": model(self._answer_model_id or enterprise_ai_default_model_id(settings)),
            "attached_images": attached_images,
            "embedding": settings.oci_genai_embedding_model,
            "rerank": (
                settings.oci_genai_rerank_model
                if settings.rag_rerank_enabled
                and _rerank_base_url_configured(settings)
                and not rerank_failed
                else ""
            ),
        }

    async def retrieve(self, request: SearchRequest) -> list[RetrievedChunk]:
        """回答を作らずに検索だけを行い、候補の chunk を順位順に返す(#593)。

        回答の検索(``_search``)を原質問 1 本で呼ぶ。質問の理解・質問の拡張・rerank・CRAG・回答の
        生成は行わず、LLM を呼ばない(embedding と Oracle の検索だけ)。KB の検索テストとレシピの
        検索比較で、引用の候補を確かめるために使う。件数は ``request.top_k``。
        """
        state = _SearchState()
        # 主検索と同じ基準日で読む(#2602。この経路は拡張しないが、条件の決め方をそろえる)。
        request = _with_effective_as_of(request)
        with read_lobs_as_values():
            result = await self._search(
                request,
                state,
                retrieval_queries=[request.query],
                candidate_limit=max(1, int(request.top_k)),
                # 検索だけの結果は上位 top_k 件の候補なので、交差参照の chunk は足さない(#1280)。
                expand_references=False,
            )
        return [
            state.chunks[child.chunk_uid]
            for child in result.child_chunks
            if child.chunk_uid in state.chunks
        ]

    async def _business_names(self, request: SearchRequest, state: _SearchState) -> list[str]:
        """検索範囲(``request.filters``)の文書の大分類の語の一覧を、1 回の回答で 1 回読む(#553)。

        読めないときは空(業務の絞り込みをせずに回答を続ける)。
        """
        try:
            return await self._oracle.retrieval_large_categories(dict(request.filters))
        except Exception as exc:  # noqa: BLE001 - 業務の絞り込みは補助。絞らずに回答を続ける。
            logger.warning("business names load failed", exc_info=True)
            state.record_degraded("business_names", exc)
            return []

    def _answer_sync(
        self,
        request: SearchRequest,
        engine_settings: Any,
        loop: asyncio.AbstractEventLoop,
        state: _SearchState,
        overrides: Mapping[str, str] | None = None,
        business_names: Sequence[str] = (),
        step_callback: StepCallback | None = None,
    ) -> Any:
        from rag_engine.adapters.oci import parse_multimodal_response, parse_text_response
        from rag_engine.dependencies import AnswerDependencies, bind_dependencies
        from rag_engine.generation.answering import answer_question_result
        from rag_engine.generation.execution_record import bind_step_listener, bind_step_metrics
        from rag_engine.knowledge.classification import classification_filter_from_values
        from rag_engine.retrieval.scope import RETRIEVAL_SCOPE_KNOWLEDGE_BASE

        def run_async(factory: Callable[[], Awaitable[T]]) -> T:
            return _run_on_loop(factory, loop)

        dependencies = AnswerDependencies(
            search=lambda **kwargs: run_async(
                lambda: self._search(request, state, defer_context=True, **kwargs)
            ),
            context_chunks=lambda **kwargs: run_async(
                lambda: self._context_chunks(state, **kwargs)
            ),
            record_retrieval=lambda stage: state.retrieval_stages.append(dict(stage)),
            check_ready=lambda **kwargs: None,
            parse_text=parse_text_response,
            parse_images=parse_multimodal_response,
            rerank=lambda query, documents, settings, top_n=None: run_async(
                lambda: self._rerank(query, list(documents), top_n)
            ),
            business_domains=lambda question: question_business_domains(question, business_names),
            # 画面目録の連携(#554)。rag_engine は設定が有効なときだけ呼ぶ。
            screen_catalog=lambda **kwargs: run_async(lambda: self._screen_catalog(request, state)),
            screen_chunks=lambda *, links, existing, **kwargs: run_async(
                lambda: self._screen_chunks(request, state, links, existing)
            ),
            # 章節の拡張(#1436)。章節と chunk の対応(#1438)を読む。無効なら章節で広げない。
            section_outline=(
                (
                    lambda *, chunk_uid, **kwargs: run_async(
                        lambda: self._section_outline(state, chunk_uid)
                    )
                )
                if self._settings.rag_section_expansion_enabled
                else None
            ),
            section_chunks=(
                (
                    lambda *, document_id, chunk_set_id, section_id, **kwargs: run_async(
                        lambda: self._section_chunks(
                            request, state, document_id, chunk_set_id, section_id
                        )
                    )
                )
                if self._settings.rag_section_expansion_enabled
                else None
            ),
            # 質問拡張の LLM と並行に、元の質問の検索を先に読む(#1499。結果は変えない)。
            prefetch_search=lambda **kwargs: run_async(
                lambda: self._prefetch_query_hits(request, state, **kwargs)
            ),
            # 条の目録から根拠の条を選ぶ段(#2254)。章節と chunk の対応(#1438)を読む。
            article_catalogs=lambda *, document_ids, limit, **kwargs: run_async(
                lambda: self._article_catalogs(state, document_ids, limit)
            ),
            article_chunks=lambda *, articles, **kwargs: run_async(
                lambda: self._article_chunks(request, state, articles)
            ),
            article_documents=lambda **kwargs: run_async(
                lambda: self._article_documents(request, state)
            ),
            # 条の選択の 2 段目で見せる、同じ章の条と別の章の条（選んだ条の名指しを探す）の
            # 本文の書き出し。
            article_leads=lambda *, document_id, chunk_set_id, sections, **kwargs: run_async(
                lambda: self._article_leads(document_id, chunk_set_id, sections, state=state)
            ),
            # 呼び出し元が検索文を短く言い換えたときの元の質問(#2261)。条の選択に使う(回答の要求の
            # 単位には answer_question_result の request_question で使う。#2665)。
            article_question=lambda: request.original_question,
            # 業務ガイドの手順の充足を意味の近さで決める embedding(#2649)。検索と同じ Cohere Embed。
            embedder=_LoopEmbedder(self._genai, loop),
            # rag_engine の中の補助の処理(画面の選択など)の失敗を、同じ記録に残す(#3259)。
            report_degraded=state.record_degraded,
        )

        def notify_step(name: str, outcome: str, elapsed: float, record: Mapping[str, Any]) -> None:
            # 工程の順序を保つため、イベントループで通知し終わるのを待つ(短い上限付き)。
            if step_callback is not None:
                asyncio.run_coroutine_threadsafe(
                    _call(lambda: step_callback(name, outcome, elapsed, record)), loop
                ).result(_STEP_NOTIFY_TIMEOUT_SECONDS)

        # 工程ごとの SQL の回数・行数・時間(#1490)。計測の範囲は工程の span(pipeline)が作る。
        db_stats = current_db_call_stats()
        with (
            bind_dependencies(dependencies),
            prompt_overrides(overrides or {}),
            bind_step_listener(notify_step if step_callback is not None else None),
            bind_step_metrics(db_stats.snapshot if db_stats is not None else None),
        ):
            return answer_question_result(
                request.query,
                ENGINE_SOURCE_RUN_ID,
                ["docling"],
                engine_settings,
                chunk_top_k=max(1, int(request.top_k)),
                chunk_neighbor_count=self._settings.rag_neighbor_child_count,
                query_strategy=self._settings.rag_query_strategy,
                answer_flow=self._settings.rag_answer_flow,
                rerank_enabled=self._settings.rag_rerank_enabled,
                retrieval_scope=RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
                # RAG 検索の画面は回答を作らない(#649)。根拠はチャットと同じ流れで集め、回答の生成の
                # 手前で止める(#3506)。業務 Agent の根拠の取得は主の文書の検索の後で止める。
                generate_answer=request.generate_answer,
                complete_evidence=request.generate_answer or self._complete_evidence,
                # 絞り込み自体は _search の hybrid_search(request.filters)が行う。
                # ここでは回答のプロンプトと実行記録に条件を載せるために渡す。
                classification_filter=classification_filter_from_values(
                    large_category=request.filters.get("large_category", ""),
                    middle_category=request.filters.get("middle_category", ""),
                    small_category=request.filters.get("small_category", ""),
                    # 明示した基準日だけ(検索の条件の補った今日の日付は回答の条件に出さない)。
                    as_of=state.requested_as_of,
                ),
                scope=(
                    (self._scope.context, self._scope.search_terms)
                    if self._scope is not None
                    else None
                ),
                guide_plan=guide_step_plan(self._settings.rag_support_guide),
                caller_queries=self._caller_queries,
                # 呼び出し元が検索文を言い換えたときの元の質問(#2665)。回答の要求の単位・要求ごとの
                # 根拠の確認・回答の生成と検査に使い、検索は request.query で行う。
                request_question=request.original_question,
            )

    async def _search(
        self,
        request: SearchRequest,
        state: _SearchState,
        *,
        retrieval_queries: Sequence[str],
        candidate_limit: int = 24,
        vector_only_queries: Sequence[str] = (),
        inquiry_conditions: Any = None,
        settings: Any = None,
        expand_references: bool = True,
        defer_context: bool = False,
        page_ranges: Sequence[Mapping[str, Any]] = (),
        **_: object,
    ) -> Any:
        """回答の検索。``page_ranges``(業務ガイドの手順の参照の頁。#2395)は、その頁に絞って検索する。

        頁に絞った検索は、参照の頁の外の chunk(交差参照・実体の拡張)を足さない。利用者の確認で
        検索の範囲がすでに頁に絞られていれば、その範囲と重なる頁だけを検索する(範囲を広げない)。
        """
        from rag_engine.models.storage import HybridSearchResult

        queries = [query for query in dict.fromkeys(q.strip() for q in retrieval_queries) if query]
        if not queries:
            return HybridSearchResult(child_chunks=[], all_chunks=[])
        await self._ensure_search_filters(request, state, inquiry_conditions)
        assert state.filters is not None
        if page_ranges:
            expand_references = False
            if not _with_page_ranges(state.filters, page_ranges):
                return HybridSearchResult(child_chunks=[], all_chunks=[])
        vector_only = {query.strip() for query in vector_only_queries}
        depth = self._read_depth(candidate_limit)
        embeddings = await self._genai.embed(queries, input_type="SEARCH_QUERY")
        k = float(self._settings.rag_rrf_k)
        vector_only_count = sum(1 for query in queries[1:] if query in vector_only)
        derived_weight = 1.0 / max(1, len(queries) - 1 - vector_only_count)
        vector_only_weight = 1.0 / max(1, vector_only_count)
        relaxed_before = len(state.relaxed)
        while True:
            fused: dict[str, float] = {}
            rankings: list[list[str]] = []
            # 検索文ごとの検索は 1 回の SQL で読み(#1498)、融合は検索文の順に行う。
            all_hits = await self._query_hits(
                state,
                [
                    (
                        query,
                        embedding,
                        SearchMode.VECTOR if query in vector_only else SearchMode.HYBRID,
                    )
                    for query, embedding in zip(queries, embeddings, strict=False)
                ],
                candidate_limit=depth,
                filters=_with_page_ranges(state.filters, page_ranges) or {}
                if page_ranges
                else dict(state.filters),
            )
            for index, hits in enumerate(all_hits):
                rankings.append([hit.chunk_id for hit in hits])
                # 原質問を主軸にし、派生検索文は合計で原質問 1 本分の票に抑える(rag_poc #975)。
                # vector だけの検索文(仮説文・規定の書き方の検索文)は文書の書き方に合わせた
                # 別の経路なので、他の派生検索文と票を分け合わず、合計で 1 本分の票にする(#2159)。
                weight = (
                    1.0
                    if index == 0
                    else vector_only_weight
                    if queries[index] in vector_only
                    else derived_weight
                )
                for rank, hit in enumerate(hits, start=1):
                    state.chunks.setdefault(hit.chunk_id, hit)
                    state.retrieval_channels.setdefault(hit.chunk_id, set()).update(
                        _hit_channels(hit, index)
                    )
                    fused[hit.chunk_id] = fused.get(hit.chunk_id, 0.0) + weight / (k + rank)
            # 自動で足した条件(質問から読み取った抽出項目の条件 #652・質問が名指しした文書のページと
            # 文書名 #546)で 0 件なら、その条件を段ごとに外して検索し直す(#2401。以降の検索(CRAG の
            # 補正検索など)も外したまま)。
            # 参照の頁に絞った検索(#2395)の 0 件では外さない(ほかの検索の条件を変えない)。
            if fused or page_ranges or not self._relax_filters(state):
                break
        if len(state.search_log) < _ZERO_EVIDENCE_SEARCH_LOG_LIMIT:
            state.search_log.append(
                {
                    "queries": len(queries),
                    "candidates": len(fused),
                    "automatic_filters": state.active_relaxations(),
                    "relaxed": state.relaxed[relaxed_before:],
                }
            )
        await self._load_classifications([state.chunks[chunk_id] for chunk_id in fused], state)
        if inquiry_conditions is not None:
            pool = [
                _stored_child(
                    state.chunks[chunk_id],
                    rrf_score=0.0,
                    classification=state.classifications.get(state.chunks[chunk_id].document_id),
                )
                for chunk_id in fused
            ]
            channels = _inquiry_channel_rankings(
                pool,
                inquiry_conditions,
                rankings,
                limit=candidate_limit,
                profile_enabled=bool(getattr(settings, "profile_channel_enabled", True)),
            )
            for channel, channel_weight, ranking in channels:
                for rank, (chunk_id, _score) in enumerate(ranking, start=1):
                    fused[chunk_id] = fused.get(chunk_id, 0.0) + channel_weight / (k + rank)
                    state.retrieval_channels.setdefault(chunk_id, set()).add(channel)
        ordered = sorted(fused, key=lambda chunk_id: -fused[chunk_id])
        unique: list[str] = []
        source_keys: dict[tuple[Any, ...], str] = {}
        duplicates: list[dict[str, str]] = []
        for chunk_id in ordered:
            chunk = state.chunks[chunk_id]
            refs = tuple(
                sorted(
                    {
                        str(ref.get("record_id") or "")
                        for ref in _json_list(chunk.metadata.get("source_record_refs_json"))
                        if ref.get("record_id")
                    }
                )
            )
            artifact = str(chunk.metadata.get("chunk_set_id") or "")
            key = (
                chunk.document_id,
                artifact,
                str(chunk.metadata.get("recipe_id") or ""),
                refs,
                chunk.text,
            )
            if refs and artifact and key in source_keys:
                duplicates.append(
                    {
                        "id": chunk_id,
                        "retained_id": source_keys[key],
                        "reason": "duplicate_original",
                    }
                )
                continue
            if refs and artifact:
                source_keys[key] = chunk_id
            unique.append(chunk_id)
        ordered = unique
        ranked = ordered[:candidate_limit]
        if self._settings.rag_candidate_diversity_depth > 0:
            ranked += _diverse_candidates(
                ordered[: self._settings.rag_candidate_diversity_depth],
                candidate_limit,
                lambda chunk_id: state.chunks[chunk_id].document_id,
                new_document=self._settings.rag_candidate_diversity_new_document,
                present_document=self._settings.rag_candidate_diversity_present_document,
            )[: self._settings.rag_candidate_diversity_max_extra]
        selected_fused_ids = set(ranked)
        # 当たった派生の chunk(要約・項目抽出の値)は検索の手がかり。回答の根拠には、その chunk が
        # 指す原文の chunk を同じ位置に置く(原文が読めなければ根拠に渡さない。#2641)。
        context_scope = (
            (_with_page_ranges(state.filters, page_ranges) or {}) if page_ranges else state.filters
        ).get(PAGE_RANGES_FILTER_KEY, "")
        if context_scope:
            expand_references = False
        anchors = await self._original_anchors(
            state,
            [state.chunks[chunk_id] for chunk_id in ranked],
            fused,
            filters=(_with_page_ranges(state.filters, page_ranges) or {})
            if page_ranges
            else dict(state.filters),
        )
        anchors = [
            chunk
            for chunk in anchors
            if contained(chunk.document_id, chunk.metadata, context_scope)
        ]
        ranked = [chunk.chunk_id for chunk in anchors]
        fusion_ids = set(ranked[:candidate_limit])
        for rank, chunk_id in enumerate(ranked, start=1):
            known = state.fused_ranks.get(chunk_id)
            if known is None or rank < known[0]:
                state.fused_ranks[chunk_id] = (rank, fused[chunk_id])
        siblings: list[RetrievedChunk] = []
        if not defer_context:
            with _search_stage(CONTEXT_SIBLINGS_STAGE, active=bool(anchors)):
                siblings = original_chunks(
                    await self._oracle.context_group_siblings(
                        [
                            anchor
                            for anchor in anchors
                            if anchor.metadata.get("chunk_strategy") == "small_to_big"
                            or self._settings.rag_neighbor_child_count > 0
                        ],
                        max_chunks_per_group=max(1, self._settings.rag_context_group_max_chunks),
                        neighbor_child_count=self._settings.rag_neighbor_child_count,
                    )
                )
        siblings = [
            chunk
            for chunk in siblings
            if contained(chunk.document_id, chunk.metadata, context_scope)
        ]
        for sibling in siblings:
            state.chunks.setdefault(sibling.chunk_id, sibling)
        # 上位の候補が本文で参照する節の chunk(#1280)。起点の候補の直後に置き、rerank に任せる。
        # 参照先が起点より下位の候補・前後の文脈(siblings)にあるときも、起点の直後へ移す(#1400。
        # 前後の文脈は rerank にかからず、下位の候補は文脈に入らないため)。上位の起点はそのまま。
        # 起点は統合の上位 5 件と、その外で回答の文脈に入りうる上位(top_k)の参照を持つ候補(#1549)。
        source_ids = {chunk.chunk_id for chunk in anchors[:_REFERENCE_SOURCE_ANCHORS]}
        references: dict[str, list[RetrievedChunk]] = {}
        if expand_references and self._settings.rag_reference_expansion_enabled:
            with _search_stage(REFERENCE_EXPANSION_STAGE, active=bool(anchors)):
                references = await self._reference_expansion(
                    request,
                    state,
                    anchors,
                    existing=source_ids,
                    question=(queries[0], embeddings[0]) if embeddings else None,
                    scores=fused,
                )
        referenced = [chunk for chunks in references.values() for chunk in chunks]
        promoted = {chunk.chunk_id for chunk in referenced}
        # 質問と上位の候補の実体から、実体の表との join で関連する chunk を 1 段だけ足す(#1362)。
        entity_chunks: list[RetrievedChunk] = []
        if expand_references and self._settings.rag_entity_expansion_enabled:
            with _search_stage(ENTITY_EXPANSION_STAGE, active=bool(anchors)):
                entity_chunks = await self._entity_expansion(request, state, anchors)
        entity_ids = {chunk.chunk_id for chunk in entity_chunks}
        # 関係情報(#2055)で、上位の候補と同じ狭い章節・表の chunk と、質問に見出しの語が含まれる
        # 章節の chunk を足す。候補・前後の文脈・参照先・実体の拡張にある chunk は足さない。
        graph_chunks: list[RetrievedChunk] = []
        if expand_references and self._settings.rag_graph_expansion_enabled:
            with _search_stage(GRAPH_EXPANSION_STAGE, active=bool(anchors)):
                graph_chunks = await self._graph_expansion(
                    request,
                    state,
                    anchors,
                    exclude={
                        *ranked,
                        *(chunk.chunk_id for chunk in siblings),
                        *promoted,
                        *entity_ids,
                    },
                )
        if self._attach_images and not defer_context:
            evidence = [*anchors, *siblings, *referenced, *entity_chunks, *graph_chunks]
            with _search_stage(
                IMAGE_EVIDENCE_STAGE,
                active=any(_needs_image_materialization(chunk, state) for chunk in evidence),
            ):
                for chunk in evidence:
                    await self._materialize_image_evidence(chunk, state)
            anchors = [state.chunks[chunk.chunk_id] for chunk in anchors]
            siblings = [state.chunks.get(chunk.chunk_id, chunk) for chunk in siblings]
            references = {
                anchor_id: [state.chunks.get(chunk.chunk_id, chunk) for chunk in chunks]
                for anchor_id, chunks in references.items()
            }
            entity_chunks = [state.chunks.get(chunk.chunk_id, chunk) for chunk in entity_chunks]
            graph_chunks = [state.chunks.get(chunk.chunk_id, chunk) for chunk in graph_chunks]
        added = [*referenced, *entity_chunks, *graph_chunks]
        evidence = [*anchors, *siblings, *added]
        # 文書の分類(足した根拠の文書)と文書の背景(1 ページ目の本文)は短い読み取りなので 1 つの段に
        # まとめる。読むものが無ければ段を出さない(#3464)。
        with _search_stage(
            DOCUMENT_INFO_STAGE,
            active=bool(
                _missing_classifications(added, state)
                or _missing_first_page_contexts(evidence, state)
            ),
        ):
            if added:
                await self._load_classifications(added, state)
            await self._load_first_page_contexts(evidence, state)
        classifications = state.classifications
        children = []
        for chunk in anchors:
            if chunk.chunk_id in entity_ids:
                # 実体の拡張で足す chunk は、拡張の位置に置く(下)。
                continue
            if chunk.chunk_id in promoted:
                # 参照先として起点の直後へ移した候補は、元の位置に置かない。
                continue
            children.append(
                _stored_child(
                    chunk,
                    rrf_score=fused.get(chunk.chunk_id, 0.0),
                    classification=classifications.get(chunk.document_id),
                    first_page_context=_first_page_context(chunk, state),
                    retrieval_channels=state.retrieval_channels.get(chunk.chunk_id),
                )
            )
            children.extend(
                _stored_child(
                    reference,
                    rrf_score=fused.get(chunk.chunk_id, 0.0) * _REFERENCE_SCORE_DECAY,
                    classification=classifications.get(reference.document_id),
                    first_page_context=_first_page_context(reference, state),
                    retrieval_channels=state.retrieval_channels.get(reference.chunk_id),
                )
                for reference in references.get(chunk.chunk_id, ())
                if reference.chunk_id not in entity_ids
            )
        if entity_chunks:
            insert_at = min(len(children), _ENTITY_INSERT_AFTER)
            base_score = fused.get(anchors[0].chunk_id, 0.0) if anchors else 0.0
            children[insert_at:insert_at] = [
                _stored_child(
                    chunk,
                    rrf_score=base_score * _ENTITY_SCORE_DECAY,
                    classification=classifications.get(chunk.document_id),
                    first_page_context=_first_page_context(chunk, state),
                    retrieval_channels=state.retrieval_channels.get(chunk.chunk_id),
                )
                for chunk in entity_chunks
            ]
        if graph_chunks:
            insert_at = min(len(children), _GRAPH_INSERT_AFTER + len(entity_chunks))
            base_score = fused.get(anchors[0].chunk_id, 0.0) if anchors else 0.0
            children[insert_at:insert_at] = [
                _stored_child(
                    chunk,
                    rrf_score=base_score * _GRAPH_SCORE_DECAY,
                    classification=classifications.get(chunk.document_id),
                    first_page_context=_first_page_context(chunk, state),
                    retrieval_channels=state.retrieval_channels.get(chunk.chunk_id),
                )
                for chunk in graph_chunks
            ]
        for child in children:
            stage = (
                "reference"
                if child.chunk_uid in promoted
                else "entity"
                if child.chunk_uid in entity_ids
                else "structure"
                if child.chunk_uid in {chunk.chunk_id for chunk in graph_chunks}
                else "fusion"
                if child.chunk_uid in fusion_ids
                else "diversity"
            )
            child.metadata["retrieval_admission_stage"] = stage
        all_children = {chunk.chunk_uid: chunk for chunk in children}
        for sibling in siblings:
            all_children.setdefault(
                sibling.chunk_id,
                _stored_child(
                    sibling,
                    rrf_score=0.0,
                    classification=classifications.get(sibling.document_id),
                    first_page_context=_first_page_context(sibling, state),
                    retrieval_channels=state.retrieval_channels.get(sibling.chunk_id),
                ),
            )
        if self._attachment_chunks:
            # 会話の添付の根拠の候補（#2517）を先頭に足す（何度検索しても同じ chunk を 1 回だけ）。
            base_score = max(fused.values(), default=1.0)
            attached = []
            for chunk in self._attachment_chunks:
                state.chunks.setdefault(chunk.chunk_id, chunk)
                if chunk.chunk_id in all_children:
                    continue
                stored = _stored_child(chunk, rrf_score=base_score * max(chunk.score, 0.1))
                stored.metadata["retrieval_admission_stage"] = "attachment"
                all_children[stored.chunk_uid] = stored
                attached.append(stored)
            children = [*attached, *children]
        children = [_with_context_scope(child, context_scope) for child in children]
        all_children = {
            uid: _with_context_scope(child, context_scope) for uid, child in all_children.items()
        }
        parents = [] if defer_context else _stored_parents(list(all_children.values()), state)
        state.retrieval_stages.append(
            {
                "stage": "candidate_fusion",
                "query_count": len(queries),
                "query_limit": depth,
                "fused_count": len(fused),
                "fused_ids": ordered,
                "duplicates": duplicates,
                "excluded": [
                    {"id": chunk_id, "reason": "fusion_candidate_budget"}
                    for chunk_id in ordered
                    if chunk_id not in selected_fused_ids
                ],
                "query_results": [
                    {"query_index": index, "count": len(hits)}
                    for index, hits in enumerate(all_hits)
                ],
                "base_limit": candidate_limit,
                "selected_ids": [child.chunk_uid for child in children],
                "context_deferred": defer_context,
            }
        )
        return HybridSearchResult(
            child_chunks=children, all_chunks=[*all_children.values(), *parents]
        )

    async def _context_chunks(
        self,
        state: _SearchState,
        *,
        anchors: Sequence[Any],
        settings: Any = None,
    ) -> list[Any]:
        """精排後の起点だけから、同じ有効成果物の親/近傍を読む。再構築は回答内の既読を使う。"""
        result: list[Any] = []
        # 検索ごとの範囲を起点から引き継ぐ。同じ chunk でも異なる範囲の既読を混ぜない。
        scopes: dict[str, list[Any]] = {}
        default_scope = (getattr(state, "filters", None) or {}).get(PAGE_RANGES_FILTER_KEY, "")
        for anchor in anchors:
            scope = str(anchor.metadata.get(CONTEXT_PAGE_SCOPE, default_scope))
            scopes.setdefault(scope, []).append(anchor)
        for scope, scoped_anchors in scopes.items():
            selected = [
                state.chunks[item.chunk_uid]
                for item in scoped_anchors
                if item.chunk_uid in state.chunks
            ]
            selected = [
                item for item in selected if contained(item.document_id, item.metadata, scope)
            ]

            def cache_key(item: RetrievedChunk, page_scope: str = scope) -> str:
                return json.dumps([item.chunk_id, page_scope], ensure_ascii=False)

            pending = [item for item in selected if cache_key(item) not in state.context_reads]
            if pending:
                with _search_stage(CONTEXT_SIBLINGS_STAGE):
                    siblings = original_chunks(
                        await self._oracle.context_group_siblings(
                            [
                                item
                                for item in pending
                                if item.metadata.get("chunk_strategy") == "small_to_big"
                                or self._settings.rag_neighbor_child_count > 0
                            ],
                            max_chunks_per_group=max(
                                1, self._settings.rag_context_group_max_chunks
                            ),
                            neighbor_child_count=self._settings.rag_neighbor_child_count,
                        )
                    )
                siblings = [
                    item for item in siblings if contained(item.document_id, item.metadata, scope)
                ]
                for item in pending:
                    state.context_reads[cache_key(item)] = [
                        sibling
                        for sibling in siblings
                        if sibling.document_id == item.document_id
                        and sibling.metadata.get("chunk_set_id")
                        == item.metadata.get("chunk_set_id")
                        and sibling.metadata.get("recipe_id") == item.metadata.get("recipe_id")
                    ]
                for item in siblings:
                    state.chunks.setdefault(item.chunk_id, item)
            chunks = {item.chunk_id: item for item in selected}
            for anchor in selected:
                chunks.update(
                    (item.chunk_id, item) for item in state.context_reads.get(cache_key(anchor), ())
                )
            originals = list(chunks.values())
            await self._load_classifications(originals, state)
            # 範囲外の1ページ目を文書背景としても渡さない。
            if not scope:
                await self._load_first_page_contexts(originals, state)
            if self._attach_images:
                for item in originals:
                    await self._materialize_image_evidence(item, state)
            children = [
                _with_context_scope(
                    _stored_child(
                        state.chunks.get(item.chunk_id, item),
                        rrf_score=0.0,
                        classification=state.classifications.get(item.document_id),
                        first_page_context=_first_page_context(item, state) if not scope else None,
                        retrieval_channels=state.retrieval_channels.get(item.chunk_id),
                    ),
                    scope,
                )
                for item in originals
            ]
            result.extend([*children, *_stored_parents(children, state)])
        return result

    async def _original_anchors(
        self,
        state: _SearchState,
        anchors: Sequence[RetrievedChunk],
        fused: dict[str, float],
        *,
        filters: Mapping[str, str],
    ) -> list[RetrievedChunk]:
        """検索の候補の派生の chunk を、その chunk が指す原文の chunk に置き換える(#2641)。

        派生の chunk(章節の要約・RAPTOR の要約・項目抽出の値)は検索の手がかりで、引用できる原文
        ではない。原文の chunk を派生の chunk の位置(統合の点)に置き、派生の chunk は根拠に
        渡さない。原文が読めなければ何も置かない(根拠不足として扱い、要約の文を確かめた記載に
        しない)。候補にすでにある原文は、上の位置を保つ。
        """
        if not any(is_derived_chunk(chunk) for chunk in anchors):
            return list(anchors)
        # 派生の chunk の原文を読む工程(処理の経過の段。#3464)。
        with _search_stage(DERIVED_SOURCE_STAGE):
            return await self._resolve_original_anchors(state, anchors, fused, filters=filters)

    async def _resolve_original_anchors(
        self,
        state: _SearchState,
        anchors: Sequence[RetrievedChunk],
        fused: dict[str, float],
        *,
        filters: Mapping[str, str],
    ) -> list[RetrievedChunk]:
        """``_original_anchors`` の本体(派生の chunk を原文の chunk に置き換える)。"""
        resolved: dict[str, RetrievedChunk] = {}
        for chunk in anchors:
            if not is_derived_chunk(chunk):
                resolved.setdefault(chunk.chunk_id, chunk)
                continue
            key = (chunk.chunk_id, tuple(sorted(filters.items())))
            source_ids = state.derived_sources.get(key)
            if source_ids is None:
                sources = await derived_source_chunks(self._oracle, chunk, filters=filters)
                for source in sources:
                    state.chunks.setdefault(source.chunk_id, source)
                source_ids = [source.chunk_id for source in sources]
                state.derived_sources[key] = source_ids
                state.derived_hits[chunk.chunk_id] = {
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "kind": derived_kind(chunk.metadata),
                    "source_chunk_ids": source_ids,
                }
            score = fused.pop(chunk.chunk_id, 0.0)
            channels = state.retrieval_channels.get(chunk.chunk_id, set())
            for source_id in source_ids:
                if (original := state.chunks.get(source_id)) is None:
                    continue
                fused[source_id] = max(fused.get(source_id, 0.0), score)
                # 派生の chunk の位置に置いた原文は、派生の chunk が当たった経路を引き継ぐ(#3443)。
                state.retrieval_channels.setdefault(source_id, set()).update(channels)
                resolved.setdefault(source_id, original)
        return list(resolved.values())

    async def _ensure_search_filters(
        self, request: SearchRequest, state: _SearchState, inquiry_conditions: Any
    ) -> None:
        """1 回の回答の検索条件(質問の文書名・ページと自動の条件)を、最初の検索の前に決める。

        決めた条件は ``state.filters`` に入れる(先読みと ``_search`` で同じ条件を使う。#1499)。
        """
        if state.filters is not None:
            return
        base = dict(request.filters)
        filters = await self._question_filters(dict(base), inquiry_conditions)
        state.filters, state.auto_field_conditions = merge_field_conditions(
            filters, self._auto_field_conditions
        )
        state.relaxations = _relaxation_steps(base, filters, state.filters)

    def _relax_filters(self, state: _SearchState) -> bool:
        """候補 0 件のとき、自動で足した検索条件の次の段を外す(#2401)。外せなければ False。

        外す段の数は ``rag_filter_relaxation_max_steps`` まで(0 は外さない)。
        """
        done = len(state.relaxed)
        if done >= len(state.relaxations) or done >= self._settings.rag_filter_relaxation_max_steps:
            return False
        name, filters = state.relaxations[done]
        state.filters = dict(filters)
        state.relaxed.append(name)
        return True

    async def _prefetch_query_hits(
        self,
        request: SearchRequest,
        state: _SearchState,
        *,
        query: str,
        candidate_limit: int = 24,
        inquiry_conditions: Any = None,
        **_: object,
    ) -> None:
        """元の質問の検索を、質問拡張の LLM と並行に先に読む(#1499)。

        検索文ごとの検索の結果(``state.search_hits``)に入れるだけで、融合・参照先・実体の拡張・条件の
        緩和はしない。続く ``_search`` が同じ検索文・方式・条件・件数の結果を読むので、回答の検索の
        結果は先読みしないときと同じ(条件の緩和は ``_search`` が全検索文の結果で決める)。
        """
        text = query.strip()
        if not text:
            return
        await self._ensure_search_filters(request, state, inquiry_conditions)
        assert state.filters is not None
        embeddings = await self._genai.embed([text], input_type="SEARCH_QUERY")
        if not embeddings:
            return
        await self._query_hits(
            state,
            [(text, embeddings[0], SearchMode.HYBRID)],
            candidate_limit=self._read_depth(candidate_limit),
            filters=dict(state.filters),
        )

    def _read_depth(self, candidate_limit: int) -> int:
        """検索文ごとの取得上限。融合後の候補数・多様化の有無・起点数とは独立。"""
        return max(1, self._settings.rag_retrieval_query_limit)

    async def _query_hits(
        self,
        state: _SearchState,
        searches: Sequence[tuple[str, list[float], SearchMode]],
        *,
        candidate_limit: int,
        filters: dict[str, str],
    ) -> list[list[RetrievedChunk]]:
        """検索文ごとの検索の結果を、``searches`` の順に返す(#1490)。

        まだ読んでいない検索文は 1 回の SQL でまとめて読む(``OracleClient.search_many``。結果は
        検索文ごとの ``hybrid_search`` と同じ。#1498)。同じ回答の中で同じ検索文・方式・条件・件数を
        読んだことがあれば、その結果を使う(CRAG の 2 回目と要求ごとの再検索)。
        """
        filter_key = tuple(sorted(filters.items()))
        keys = [(query, mode.value, filter_key, candidate_limit) for query, _, mode in searches]
        missing: dict[
            tuple[str, str, tuple[tuple[str, str], ...], int],
            tuple[str, list[float], SearchMode],
        ] = {}
        for key, search in zip(keys, searches, strict=True):
            if key not in state.search_hits:
                missing.setdefault(key, search)
        if missing:
            search_many = getattr(self._oracle, "search_many", None)
            if search_many is not None:
                found = await search_many(
                    list(missing.values()), candidate_limit, filters=dict(filters)
                )
            else:
                # ``hybrid_search`` だけを持つ Oracle(テストの代役など)は 1 本ずつ読む。
                found = [
                    await self._oracle.hybrid_search(
                        query, embedding, candidate_limit, mode=mode, filters=dict(filters)
                    )
                    for query, embedding, mode in missing.values()
                ]
            state.search_hits.update(zip(missing, found, strict=True))
        return [state.search_hits[key] for key in keys]

    async def _entity_expansion(
        self,
        request: SearchRequest,
        state: _SearchState,
        anchors: Sequence[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        """質問と上位の候補の実体から、関連する chunk を 1 段だけ足す(#1362)。

        実体は文書レシピで実体の抽出を選んだ文書にだけある。足す chunk は主の検索と同じ範囲
        (利用者が指定した条件と権限。``_expansion_scope``)の中だけで、合計
        ``rag_entity_expansion_max_chunks`` 件まで。
        上位の候補(起点)は足さない(すでに候補の先頭にある)。読めないとき(実体の表が無い古い
        schema など)は足さずに回答を続ける。LLM は呼ばない。
        """
        if not self._settings.rag_entity_expansion_enabled:
            # 検索方法の全体設定で選ぶ(既定 OFF)。評価では rag_overrides で比較できる。
            return []
        scope = _expansion_scope(request.filters)
        seeds = list(anchors[:ENTITY_SEED_ANCHORS])
        key = (
            request.query,
            json.dumps(scope, ensure_ascii=False, sort_keys=True),
            *(chunk.chunk_id for chunk in seeds),
        )
        plan = state.entity_plans.get(key)
        if plan is None:
            try:
                plan = await plan_entity_expansion(
                    self._entity_store_or_default(),
                    scope,
                    question=request.query,
                    seed_chunks=seeds,
                    exclude_chunk_ids={chunk.chunk_id for chunk in seeds},
                    max_chunks=self._settings.rag_entity_expansion_max_chunks,
                )
            except Exception as exc:  # noqa: BLE001 - 実体の拡張は補助。足さずに回答を続ける。
                logger.warning("entity expansion failed", exc_info=True)
                state.record_degraded("entity_expansion", exc)
                plan = []
            state.entity_plans[key] = plan
        added: list[RetrievedChunk] = []
        for item in plan:
            if is_derived_chunk(item.chunk):
                # 派生の chunk(要約・項目の値)は原文ではないので足さない(#2641)。
                continue
            marked = expansion_metadata(
                state.chunks.get(item.chunk.chunk_id, item.chunk), item.info
            )
            state.chunks[item.chunk.chunk_id] = marked
            state.entity_expansions.setdefault(
                item.chunk.chunk_id,
                {
                    "chunk_id": item.chunk.chunk_id,
                    "document_id": item.chunk.document_id,
                    **item.info,
                },
            )
            added.append(marked)
        return added

    async def _graph_expansion(
        self,
        request: SearchRequest,
        state: _SearchState,
        anchors: Sequence[RetrievedChunk],
        *,
        exclude: set[str],
    ) -> list[RetrievedChunk]:
        """関係情報(#2055)で、上位の候補の章節・表と質問の見出しの語から chunk を足す。

        関係情報は文書レシピで「文書構造索引の構築」を「構築する」にした文書にだけあり、無ければ何も
        足さない。足す chunk は主の検索と同じ範囲(利用者が指定した条件と権限。
        ``_expansion_scope``)の中だけで、合計 ``rag_graph_expansion_max_chunks`` 件まで。読めない
        ときは足さずに回答を続ける。LLM は呼ばない。
        """
        if not self._settings.rag_graph_expansion_enabled:
            # 既定は off。検索方法の画面・検索・回答プロファイル(と評価の rag_overrides)で on。
            return []
        scope = _expansion_scope(request.filters)
        seeds = list(anchors[:GRAPH_SEED_ANCHORS])
        key = (
            request.query,
            json.dumps(scope, ensure_ascii=False, sort_keys=True),
            *(chunk.chunk_id for chunk in seeds),
        )
        plan = state.graph_plans.get(key)
        if plan is None:
            try:
                attempt: dict[str, object] = {}
                state.graph_attempts[key] = attempt
                plan = await plan_graph_expansion(
                    self._graph_store_or_default(),
                    scope,
                    question=request.query,
                    seed_chunks=seeds,
                    exclude_chunk_ids=set(exclude),
                    max_chunks=self._settings.rag_graph_expansion_max_chunks,
                    diagnostics=attempt,
                )
            except Exception as exc:  # noqa: BLE001 - 関係情報の拡張は補助。足さずに回答を続ける。
                logger.warning("graph expansion failed", exc_info=True)
                state.record_degraded("graph_expansion", exc)
                state.graph_attempts[key] = {"status": "error"}
                plan = []
            state.graph_plans[key] = plan
        added: list[RetrievedChunk] = []
        for item in plan:
            if item.chunk.chunk_id in exclude or is_derived_chunk(item.chunk):
                continue
            marked = graph_expansion_metadata(
                state.chunks.get(item.chunk.chunk_id, item.chunk), item.info
            )
            state.chunks[item.chunk.chunk_id] = marked
            state.graph_expansions.setdefault(
                item.chunk.chunk_id,
                {
                    "chunk_id": item.chunk.chunk_id,
                    "document_id": item.chunk.document_id,
                    **item.info,
                },
            )
            added.append(marked)
        return added

    def _graph_store_or_default(self) -> GraphExpansionStore:
        if self._graph_store is None:
            from app.clients.oracle_graph_store import OracleGraphStore

            self._graph_store = OracleGraphStore(self._oracle)
        return self._graph_store

    def _entity_store_or_default(self) -> EntityExpansionStore:
        if self._entity_store is None:
            from app.clients.entity_store import EntityStore

            self._entity_store = EntityStore(self._oracle)
        return self._entity_store

    async def _reference_expansion(
        self,
        request: SearchRequest,
        state: _SearchState,
        anchors: Sequence[RetrievedChunk],
        *,
        existing: set[str],
        question: tuple[str, list[float]] | None = None,
        scores: Mapping[str, float] | None = None,
    ) -> dict[str, list[RetrievedChunk]]:
        """上位の候補が本文で参照する節の chunk を、上限まで集める(起点の chunk_id ごと。#1280)。

        起点は統合(RRF)の上位 ``_REFERENCE_SOURCE_ANCHORS`` 件と、その外で回答の文脈に入りうる上位
        (``request.top_k``。``_REFERENCE_EXTENDED_ANCHORS`` 件まで)の参照を持つ候補(#1549)。上位の
        起点から順に辿り、上位 5 件の外の起点では試す参照先を ``_REFERENCE_EXTENDED_TARGETS``
        件までに抑える。他の起点の参照先として足した候補は起点にしない(参照は 1 段)。起点として
        参照先を足した候補は、他の起点の参照先として移さない(移すと、その候補の参照先が根拠に
        並ばない)。

        ``existing`` の chunk(上位の起点)は足さない。それより下位の候補・前後の文脈にある参照先は
        足す(呼び出し側が起点の直後へ移す。#1400)。

        参照先は取込時に chunk の metadata(``reference_targets_json``)へ解決してある。参照の無い
        候補だけなら DB を読まない。足す chunk は 1 回の検索で合計
        ``rag_reference_expansion_max_chunks`` 件まで、参照先 1 つにつき節の先頭から
        ``_REFERENCE_CHUNKS_PER_TARGET`` 件まで。順位は rag_engine の rerank が決める(LLM は
        呼ばない)。参照先の範囲は検索範囲のナレッジベース(と利用者の権限・旧版の扱い・基準日)で、
        文書名・ページ・分類・項目の絞り込みは使わない(本文が明記した参照は、利用者が指定した文書名の
        外へも辿る。``_REFERENCE_SCOPE_FILTER_KEYS``。#3260)。読めない参照先は足さない(回答は続ける)。

        文書名だけの参照(「時間帯は定期保守計画で確かめます」。#1400)は、参照先の文書の中を
        ``question``(検索文とその埋め込み)で検索し、rerank で質問に関連の高い chunk を選ぶ
        (``_document_reference_chunks``)。

        参照を持つ候補の統合の順位と、辿った結果・辿らなかった理由は ``state.reference_sources`` に
        残す(診断。#1549)。``scores`` は統合の点(chunk_id → 点)。
        """
        if not self._settings.rag_reference_expansion_enabled:
            return {}
        budget = self._settings.rag_reference_expansion_max_chunks
        scope = _reference_scope(request.filters)
        limit = max(_REFERENCE_SOURCE_ANCHORS, min(int(request.top_k), _REFERENCE_EXTENDED_ANCHORS))
        seen = set(existing)
        added: dict[str, list[RetrievedChunk]] = {}
        promoted: set[str] = set()
        count = 0
        extended_targets = 0
        sources: list[dict[str, object]] = []
        record: dict[str, object] = {
            "candidates": len(anchors),
            "top_sources": _REFERENCE_SOURCE_ANCHORS,
            "source_limit": limit,
            "added_count": 0,
            "sources": sources,
        }
        if len(state.reference_sources) < _REFERENCE_DIAGNOSTICS_SEARCHES:
            state.reference_sources.append(record)
        await self._prefetch_reference_documents(scope, state, anchors[:limit])
        for position, anchor in enumerate(anchors, start=1):
            if position > limit or count >= budget:
                # 起点の外・上限の後の参照を持つ候補(取込で解決した印のあるものだけ。DB は
                # 読まない)。
                stored = reference_targets(anchor.metadata)
                if stored and len(sources) < _REFERENCE_DIAGNOSTICS_LIMIT:
                    outcome = "outside_sources" if position > limit else "budget"
                    sources.append(
                        _reference_source_entry(anchor, position, scores, stored, outcome)
                    )
                continue
            try:
                targets = await self._anchor_reference_targets(scope, state, anchor)
            except Exception as exc:  # noqa: BLE001 - 参照先は補助。この候補の参照を辿らずに続ける。
                logger.warning("reference target resolution failed", exc_info=True)
                state.record_degraded("reference_resolve", exc)
                continue
            if not targets:
                continue
            entry = _reference_source_entry(anchor, position, scores, targets, "none")
            if len(sources) < _REFERENCE_DIAGNOSTICS_LIMIT:
                sources.append(entry)
            if anchor.chunk_id in promoted:
                # 他の起点の参照先として足した候補(参照は 1 段)。
                entry["outcome"] = "referenced"
                continue
            extended = position > _REFERENCE_SOURCE_ANCHORS
            outcomes: list[str] = []
            entry["target_outcomes"] = outcomes
            for target in targets:
                if count >= budget:
                    outcomes.append("budget")
                    break
                if extended and extended_targets >= _REFERENCE_EXTENDED_TARGETS:
                    outcomes.append("target_limit")
                    break
                if extended:
                    extended_targets += 1
                try:
                    chunks = await self._reference_target_chunks(
                        scope, state, anchor, target, request=request, question=question
                    )
                except Exception as exc:  # noqa: BLE001 - 参照先は補助。この参照先を足さずに続ける。
                    logger.warning("reference target load failed", exc_info=True)
                    state.record_degraded("reference_load", exc)
                    outcomes.append("error")
                    continue
                fresh = [chunk for chunk in chunks if chunk.chunk_id not in seen | added.keys()]
                selected = fresh[: min(_REFERENCE_CHUNKS_PER_TARGET, budget - count)]
                if target.kind == "document":
                    # 文書名の参照で選んだ節を、同じ文書の中で参照する文(適用の条件。#1549)。
                    selected.extend(
                        _section_citing_chunks(
                            fresh, selected, limit=budget - count - len(selected)
                        )
                    )
                # 足した / 参照先がすでに上位の起点にある / 参照先が見つからない。
                outcomes.append("added" if selected else ("present" if chunks else "not_found"))
                for rank, chunk in enumerate(selected, start=1):
                    seen.add(chunk.chunk_id)
                    promoted.add(chunk.chunk_id)
                    # 下位の候補・前後の文脈として読んだ chunk にも、参照の起点の印を付ける。
                    marked = state.chunks.get(chunk.chunk_id, chunk)
                    if REFERENCE_FROM_KEY not in marked.metadata:
                        marked = marked.model_copy(
                            update={
                                "metadata": {
                                    **marked.metadata,
                                    REFERENCE_FROM_KEY: anchor.chunk_id,
                                    REFERENCE_LABEL_KEY: target.label,
                                    # 文書名だけの参照は、参照先の文書の中の関連の順(#1400)。
                                    **(
                                        {REFERENCE_RANK_KEY: rank}
                                        if target.kind == "document"
                                        else {}
                                    ),
                                }
                            }
                        )
                        state.chunks[chunk.chunk_id] = marked
                    added.setdefault(anchor.chunk_id, []).append(marked)
                    state.reference_expansions.setdefault(
                        chunk.chunk_id,
                        {
                            "chunk_id": chunk.chunk_id,
                            "document_id": chunk.document_id,
                            "from_chunk_id": anchor.chunk_id,
                            # 起点の統合の順位(#1549)。
                            "from_fused_rank": position,
                            "label": target.label,
                            "kind": target.kind,
                            "document_title": target.document_title,
                            "section_path": str(chunk.metadata.get("section_path") or ""),
                            "resolved_at": (
                                "query"
                                if anchor.chunk_id in state.query_reference_targets
                                else "ingest"
                            ),
                        },
                    )
                    count += 1
            if anchor.chunk_id in added:
                entry["outcome"] = "added"
            elif outcomes and outcomes[-1] in {"budget", "target_limit"}:
                entry["outcome"] = outcomes[-1]
        record["added_count"] = count
        return added

    async def _prefetch_reference_documents(
        self, scope: dict[str, str], state: _SearchState, anchors: Sequence[RetrievedChunk]
    ) -> None:
        """起点の候補が参照する他の文書の文書名を、まとめて 1 回で照合しておく(#2137)。

        文書名の照合は範囲の chunk を全件読むため、文書名ごとに読むと参照の多い検索で遅くなる
        (文書名 10 件で 10 回)。取込で解決した参照先(と本文の文書名だけの参照)の文書名を先に
        集め、``retrieval_reference_documents_many`` の 1 回の読み取りで照合して
        ``state.reference_document_rows`` に置く。取込で解決していない候補の参照先(#1382)は
        辿るときに 1 つずつ照合する。読めなければ辿るときに 1 つずつ照合する(回答は続ける)。
        """
        scope_key = json.dumps(scope, ensure_ascii=False, sort_keys=True)
        keys: list[str] = []
        for anchor in anchors:
            if REFERENCE_TARGETS_KEY not in anchor.metadata:
                continue
            stored = reference_targets(anchor.metadata)
            for target in [*stored, *_document_name_targets(anchor, stored)]:
                key = title_key(target.document_title or "")
                cache_key = (scope_key, key)
                if (
                    len(key) >= 2
                    and key not in keys
                    and cache_key not in state.reference_documents
                    and cache_key not in state.reference_document_rows
                ):
                    keys.append(key)
        if not keys:
            return
        try:
            found = await self._oracle.retrieval_reference_documents_many(
                scope, titles=keys, limit=_REFERENCE_DOCUMENT_CANDIDATES
            )
        except Exception as exc:  # noqa: BLE001 - 参照先は補助。辿るときに 1 つずつ照合する。
            logger.warning("reference document prefetch failed", exc_info=True)
            state.record_degraded("reference_prefetch", exc)
            return
        for key in keys:
            state.reference_document_rows[(scope_key, key)] = found.get(key, [])

    async def _anchor_reference_targets(
        self, scope: dict[str, str], state: _SearchState, anchor: RetrievedChunk
    ) -> list[ReferenceTarget]:
        """候補の chunk の参照先。取込で解決した参照先が無ければ本文から抜き出して解決する(#1382)。

        取込は参照の表記のある chunk に ``reference_targets_json`` を残す(参照が自分の節だけでも
        空の列)。印の無い chunk(抽出を広げる前に取り込んだ chunk)は、本文に参照の表記が
        あるときだけ、その文書の同じ版(chunk_set)の見出しの列を検索と同じ範囲(KB・権限・
        有効な chunk_set)で 1 回読み、取込と同じ規則で決める。RAPTOR の要約 chunk は辿らない。
        印のある chunk でも、本文の文書名だけの参照(#1400)が印に無ければ足す(#1400 より前の取込)。
        """
        metadata = anchor.metadata
        if REFERENCE_TARGETS_KEY in metadata:
            stored = reference_targets(metadata)
            extra = _document_name_targets(anchor, stored)
            if extra:
                # 本文から足した参照がある起点(診断の resolved_at は query)。
                state.query_reference_targets[anchor.chunk_id] = [*stored, *extra]
            return [*stored, *extra]
        if anchor.chunk_id in state.query_reference_targets:
            return state.query_reference_targets[anchor.chunk_id]
        specs = [] if is_derived_chunk(anchor) else extract_references(anchor.text)
        targets: list[ReferenceTarget] = []
        if specs:
            file_name = anchor.file_name or ""
            chunk_set_id = str(metadata.get("chunk_set_id") or "")
            key = (anchor.document_id, chunk_set_id)
            same_document = any(
                not spec.document_title or same_document_title(spec.document_title, file_name)
                for spec in specs
            )
            if same_document and key not in state.reference_section_indexes:
                filters = {**scope, "document_id": anchor.document_id}
                if chunk_set_id:
                    filters["chunk_set_id"] = chunk_set_id
                sections = await self._oracle.retrieval_screen_sections(filters)
                state.reference_section_indexes[key] = SectionIndex(
                    path for _name, path, _count in sections
                )
            targets = resolve_reference_specs(
                specs,
                # 他の文書への参照だけなら、この文書の見出しは読まない(参照先は検索範囲から探す)。
                state.reference_section_indexes[key] if same_document else SectionIndex(()),
                citing_path=split_section_path(metadata.get("section_path")),
                document_title=file_name,
            )
        state.query_reference_targets[anchor.chunk_id] = targets
        return targets

    async def _reference_target_chunks(
        self,
        scope: dict[str, str],
        state: _SearchState,
        anchor: RetrievedChunk,
        target: ReferenceTarget,
        *,
        request: SearchRequest | None = None,
        question: tuple[str, list[float]] | None = None,
    ) -> list[RetrievedChunk]:
        """参照先の節の chunk(読み順)。同じ文書は参照元と同じ版(chunk_set)から読む。

        文書全体への参照(文書名だけ。#1400)は、参照先の文書の質問に関連の高い順。
        """
        limit = _REFERENCE_CHUNKS_PER_TARGET + _REFERENCE_SOURCE_ANCHORS
        if target.document_title is None:
            if target.section_path is None:
                return []
            chunk_set_id = str(anchor.metadata.get("chunk_set_id") or "")
            key = ("same", anchor.document_id, chunk_set_id, target.section_path)
            if key not in state.reference_chunks:
                rows = await self._oracle.retrieval_reference_chunks(
                    scope,
                    document_id=anchor.document_id,
                    section_path=target.section_path,
                    chunk_set_id=chunk_set_id or None,
                    limit=limit,
                )
                # 参照先の節の要約・項目の値(派生の chunk)は原文ではないので足さない(#2641)。
                state.reference_chunks[key] = [
                    row
                    for row in original_chunks(rows)
                    if section_path_within(row.metadata.get("section_path"), target.section_path)
                ]
            return state.reference_chunks[key]
        if target.kind == "document":
            # 文書全体への参照は、参照先の文書の中で質問に関連の高い chunk に限る(#1400)。
            if request is None or question is None:
                return []
            key = ("document-name", title_key(target.document_title), request.query, question[0])
            if key not in state.reference_chunks:
                state.reference_chunks[key] = await self._document_reference_chunks(
                    scope, state, anchor, target, request=request, question=question
                )
            return state.reference_chunks[key]
        key = ("document", title_key(target.document_title), target.kind, target.key)
        if key not in state.reference_chunks:
            state.reference_chunks[key] = await self._other_document_reference_chunks(
                scope, state, anchor, target, limit=limit
            )
        return state.reference_chunks[key]

    async def _other_document_reference_chunks(
        self,
        scope: dict[str, str],
        state: _SearchState,
        anchor: RetrievedChunk,
        target: ReferenceTarget,
        *,
        limit: int,
    ) -> list[RetrievedChunk]:
        """他の文書への参照を検索範囲の文書から解決し、参照先の節の chunk を返す(#1280)。

        文書名・タイトルが合う文書(``_reference_documents``)のうち、当たり方の確かなもの、名前の
        短いものを選ぶ。節は、その文書の見出しの列から取込時と同じ規則で決める。版は 1 つの
        chunk_set にそろえる。
        """
        document_id = await self._referenced_document_id(scope, state, anchor, target)
        if document_id is None:
            return []
        sections = await self._oracle.retrieval_screen_sections(
            {**scope, "document_id": document_id}
        )
        section_path = SectionIndex(path for _name, path, _count in sections).resolve(target.spec)
        if section_path is None:
            return []
        rows = await self._oracle.retrieval_reference_chunks(
            scope, document_id=document_id, section_path=section_path, limit=limit
        )
        rows = [
            row
            for row in original_chunks(rows)
            if section_path_within(row.metadata.get("section_path"), section_path)
        ]
        if not rows:
            return []
        first_set = rows[0].metadata.get("chunk_set_id")
        return [row for row in rows if row.metadata.get("chunk_set_id") == first_set]

    async def _referenced_document_id(
        self,
        scope: dict[str, str],
        state: _SearchState,
        anchor: RetrievedChunk,
        target: ReferenceTarget,
    ) -> str | None:
        """参照の文書名に合う検索範囲の文書のうち、最も確かなもの(参照元の文書は除く)。"""
        documents = await self._reference_documents(scope, state, target.document_title or "")
        return next(
            (document_id for document_id, _name in documents if document_id != anchor.document_id),
            None,
        )

    async def _reference_documents(
        self, scope: dict[str, str], state: _SearchState, title: str
    ) -> list[tuple[str, str]]:
        """参照の文書名に合う、検索範囲の文書の (id, 文書名)。確かな当たり方、名前の短い順(#1400)。

        文書名(ファイル名)と文書のタイトル(番号の無い先頭の見出し)、それらの別名(年度・版・
        括弧書きを外した形)と照合する(``document_name_match``。NFKC)。範囲は検索と同じ
        (KB・利用者の権限・有効な chunk_set・旧版は既定で除く)。1 回の回答の中では cache する。
        """
        key = title_key(title)
        cache_key = (json.dumps(scope, ensure_ascii=False, sort_keys=True), key)
        if cache_key in state.reference_documents:
            return state.reference_documents[cache_key]
        documents: list[tuple[str, str]] = []
        if len(key) >= 2:
            rows = state.reference_document_rows.get(cache_key)
            if rows is None:
                rows = await self._oracle.retrieval_reference_documents(
                    scope, title=key, limit=_REFERENCE_DOCUMENT_CANDIDATES
                )
            ranked = sorted(
                (match, len(file_name), document_id, file_name)
                for document_id, file_name, headings in rows
                if (match := document_name_match(title, file_name, headings)) is not None
            )
            documents = [(document_id, file_name) for _m, _l, document_id, file_name in ranked]
        state.reference_documents[cache_key] = documents
        return documents

    async def _document_reference_chunks(
        self,
        scope: dict[str, str],
        state: _SearchState,
        anchor: RetrievedChunk,
        target: ReferenceTarget,
        *,
        request: SearchRequest,
        question: tuple[str, list[float]],
    ) -> list[RetrievedChunk]:
        """文書名だけの参照(#1400)の参照先の文書の、質問に関連の高い chunk(関連の高い順)。

        参照先の文書は検索範囲の中から文書名・タイトルで決める(参照元の文書は除く)。その文書の中を
        回答の検索と同じ検索文と埋め込みで ``_DOCUMENT_REFERENCE_CANDIDATES`` 件まで検索し(範囲は
        検索と同じ。版は 1 つの chunk_set にそろえる)、rerank が有効なら原質問で並べ替える。
        文書の前置き・目次から順に足さない(質問と関係の無い節で上限を使わない)。
        """
        document_id = await self._referenced_document_id(scope, state, anchor, target)
        if document_id is None:
            return []
        query, embedding = question
        hits = await self._oracle.hybrid_search(
            query,
            embedding,
            _DOCUMENT_REFERENCE_CANDIDATES,
            mode=SearchMode.HYBRID,
            filters={**scope, "document_id": document_id},
        )
        rows = [hit for hit in original_chunks(hits) if hit.document_id == document_id]
        if not rows:
            return []
        first_set = rows[0].metadata.get("chunk_set_id")
        rows = [row for row in rows if row.metadata.get("chunk_set_id") == first_set]
        if len(rows) < 2 or not self._settings.rag_rerank_enabled:
            return rows
        try:
            ranked = await self._genai.rerank(
                request.query,
                [_reference_rerank_document(row) for row in rows],
                len(rows),
            )
        except Exception as exc:  # noqa: BLE001 - 並べ替えは補助。検索の順のまま足す。
            logger.warning("document reference rerank failed", exc_info=True)
            state.record_degraded("reference_rerank", exc)
            return rows
        order = [index for index, _score in ranked if 0 <= index < len(rows)]
        order.extend(index for index in range(len(rows)) if index not in order)
        return [rows[index] for index in order]

    async def _question_filters(
        self, filters: dict[str, str], inquiry_conditions: Any
    ) -> dict[str, str]:
        """質問が名指しした文書名・ページを検索条件(``hybrid_search`` の filters)へ足す(#546)。

        - 画面で文書(``file_name`` / ``document_id``)を指定したときは、そちらを優先して足さない。
          ページも同じく、画面で指定したときは足さない。
        - ナレッジベースに無い文書名は足さない(0 件にしない。rag_poc の
          ``_resolved_metadata_filter``)。
          ページは文書名と組でだけ使う条件なので、文書名を足さないときは一緒に外す。
        - filters の ``file_name`` は 1 語なので、質問が複数の文書名を挙げたときは、
          最初に見つかったものを使う。
        """
        metadata_filter = getattr(inquiry_conditions, "metadata_filter", None)
        terms = [
            term.strip()
            for term in getattr(metadata_filter, "source_file_terms", ())
            if isinstance(term, str) and term.strip()
        ]
        # 確認で章節の範囲(page_ranges)を選んだときも、そちらを優先して足さない(#717)。
        if not terms or any(
            filters.get(key, "").strip() for key in ("file_name", "document_id", "page_ranges")
        ):
            return filters
        for term in terms[:_MAX_QUESTION_FILE_TERMS]:
            candidate = {**filters, "file_name": term}
            if await self._oracle.has_retrieval_chunks(candidate):
                break
        else:
            return filters
        pages = [
            page
            for page in getattr(metadata_filter, "page_numbers", ())
            if isinstance(page, int) and page > 0
        ]
        if pages and not any(
            filters.get(key, "").strip() for key in ("page_number_min", "page_number_max")
        ):
            candidate["page_number_min"] = str(min(pages))
            candidate["page_number_max"] = str(max(pages))
        return candidate

    async def _screen_catalog(
        self, request: SearchRequest, state: _SearchState
    ) -> dict[str, list[str]]:
        """検索範囲の全文書の見出しから画面目録を作る(範囲と索引の状態ごとに cache。#554)。

        1 回の回答では最初の 1 回だけ作る。読めないときは空(rag_engine は LLM を呼ばず
        画面を足さない)。
        """
        if state.screen_catalog is None:
            state.screen_catalog = await self._load_screen_catalog(request, state)
        return state.screen_catalog

    async def _load_screen_catalog(
        self, request: SearchRequest, state: _SearchState
    ) -> dict[str, list[str]]:
        from rag_engine.retrieval.screen_catalog import catalog_from_section_paths

        filters = _screen_filters(request, state)
        try:
            index_state = await self._oracle.retrieval_scope_state(filters)
            key = (json.dumps(filters, ensure_ascii=False, sort_keys=True), index_state)
            cached = _SCREEN_CATALOG_CACHE.get(key)
            if cached is not None:
                return cached
            sections = await self._oracle.retrieval_screen_sections(filters)
        except Exception as exc:  # noqa: BLE001 - 画面目録は補助。画面を足さずに回答を続ける。
            logger.warning("screen catalog load failed", exc_info=True)
            state.record_degraded("screen_catalog", exc)
            return {}
        catalog = catalog_from_section_paths(
            (file_name, _section_headings(section_path), count)
            for file_name, section_path, count in sections
        )
        if len(_SCREEN_CATALOG_CACHE) >= _SCREEN_CATALOG_CACHE_SIZE:
            _SCREEN_CATALOG_CACHE.clear()
        _SCREEN_CATALOG_CACHE[key] = catalog
        return catalog

    async def _screen_chunks(
        self,
        request: SearchRequest,
        state: _SearchState,
        links: Sequence[tuple[str, str]],
        existing: set[str],
    ) -> list[Any]:
        """選ばれた画面の child(画面ごとに上限まで)と、その親を検索範囲から読む(#554)。

        検索結果に無かった chunk も引用に戻せるよう ``state.chunks`` に入れる。分類・1 ページ目の
        本文(#557)・根拠画像は検索の候補と同じく付ける。読めない画面は足さない(回答は続ける)。
        """
        from rag_engine.retrieval.screen_catalog import MAX_CHILDREN_PER_SCREEN

        filters = _screen_filters(request, state)
        added: dict[str, RetrievedChunk] = {}
        for file_name, heading in links:
            try:
                rows = await self._oracle.retrieval_screen_chunks(
                    filters,
                    file_name=file_name,
                    heading=heading,
                    limit=MAX_CHILDREN_PER_SCREEN * 4 + len(existing),
                )
            except Exception as exc:  # noqa: BLE001 - 画面の根拠は補助。この画面を足さずに続ける。
                logger.warning("screen chunks load failed", exc_info=True)
                state.record_degraded("screen_chunks", exc)
                continue
            matched = [
                chunk
                for chunk in original_chunks(rows)
                if chunk.chunk_id not in existing
                and chunk.chunk_id not in added
                and heading in _chunk_headings(chunk)
            ]
            for chunk in matched[:MAX_CHILDREN_PER_SCREEN]:
                added[chunk.chunk_id] = state.chunks.setdefault(chunk.chunk_id, chunk)
        if not added:
            return []
        chunks = list(added.values())
        # 分類と 1 ページ目の本文は、読んでいない文書・chunk set の分だけをまとめて 1 回で読む。
        await self._load_classifications(chunks, state)
        await self._load_first_page_contexts(chunks, state)
        if self._attach_images:
            for chunk in chunks:
                await self._materialize_image_evidence(chunk, state)
            chunks = [state.chunks[chunk.chunk_id] for chunk in chunks]
        children = [
            _stored_child(
                chunk,
                rrf_score=0.0,
                classification=state.classifications.get(chunk.document_id),
                first_page_context=_first_page_context(chunk, state),
            )
            for chunk in chunks
        ]
        return [*children, *_stored_parents(children, state)]

    async def _section_outline(self, state: _SearchState, chunk_id: str) -> Any:
        """当たった chunk の文書の章節の一覧と所有の章節(#1436)。対応が無い・読めなければ None。

        対応がまだ無い文書(この機能の前に取り込んだ文書)は、第 1 段階(#1438)と同じく
        その場で作り直す。
        """
        from rag_engine.generation.section_expansion import SectionInfo, SectionOutline

        from app.rag.section_map_service import chunk_section, section_outline

        chunk = state.chunks.get(chunk_id)
        if chunk is None:
            return None
        try:
            found = await chunk_section(chunk.document_id, chunk_id, oracle=self._oracle)
            if found is None:
                return None
            chunk_set_id, owner_section_id = found
            key = (chunk.document_id, chunk_set_id)
            if key not in state.section_outlines:
                state.section_outlines[key] = await section_outline(
                    chunk.document_id, chunk_set_id=chunk_set_id, oracle=self._oracle
                )
            outline = state.section_outlines[key]
        except Exception as exc:  # noqa: BLE001 - 章節の拡張は補助。広げずに回答を続ける。
            logger.warning("section outline load failed", exc_info=True)
            state.record_degraded("section_outline", exc)
            return None
        if outline is None:
            return None
        if chunk_id in state.reference_expansions:
            # 交差参照で足した chunk の章節(所有の章節から根まで)は、参照先と同じ範囲で読む(#3325)。
            state.reference_sections.update(
                (chunk.document_id, chunk_set_id, section_id)
                for section_id in _section_chain(outline.sections, owner_section_id)
            )
        return SectionOutline(
            document_id=chunk.document_id,
            chunk_set_id=chunk_set_id,
            owner_section_id=owner_section_id,
            sections=tuple(
                SectionInfo(
                    section_id=node.section_id,
                    title=node.title,
                    level=node.level,
                    parent_section_id=node.parent_section_id,
                    page_start=node.page_start,
                    page_end=node.page_end,
                    chunk_count=node.chunk_count,
                    chars=node.chars,
                )
                for node in outline.sections
            ),
            file_name=_source_file_name(chunk),
        )

    async def _section_chunks(
        self,
        request: SearchRequest,
        state: _SearchState,
        document_id: str,
        chunk_set_id: str,
        section_id: str,
    ) -> list[Any]:
        """章節の chunk(章節の中の順の child と、その親)を回答の検索と同じ条件で読む(#1436)。

        交差参照で足した chunk を起点にした章節(``state.reference_sections``)は、参照先を読んだ
        範囲(``_reference_scope``。ナレッジベース・旧版の扱い・基準日と利用者の権限)で読む。呼び出し
        元の文書・文書名・ページなどの絞り込みの外の参照先でも、参照で足した文書の章節を読めなくしない
        (#3325)。それ以外の章節は回答の検索と同じ条件のまま。
        読んだ chunk は引用へ戻せるよう ``state.chunks`` に入れ、分類・1 ページ目の本文・根拠画像は
        検索の候補と同じく付ける。読めなければ空(広げずに回答を続ける)。
        """
        from rag_engine.generation.section_expansion import MAX_SECTION_CHUNKS, SectionRead

        from app.rag.section_map_service import read_section

        try:
            text = await read_section(
                document_id,
                section_id,
                chunk_set_id=chunk_set_id,
                limit=MAX_SECTION_CHUNKS,
                filters=(
                    _reference_scope(request.filters)
                    if (document_id, chunk_set_id, section_id) in state.reference_sections
                    else dict(state.filters if state.filters is not None else request.filters)
                ),
                oracle=self._oracle,
            )
        except Exception as exc:  # noqa: BLE001 - 章節の拡張は補助。この章節を足さずに続ける。
            logger.warning("section chunks load failed", exc_info=True)
            state.record_degraded("section_chunks", exc)
            return SectionRead(complete=False, reason="read_error")
        # #2590 より前に作った章節の対応には派生の chunk が残りうるので、原文だけを足す(#2641)。
        chunks = [
            state.chunks.setdefault(chunk.chunk_id, chunk)
            for chunk in original_chunks(row.chunk for row in text.chunks)
        ]
        if not chunks:
            return SectionRead(complete=False, reason="partial_read")
        await self._load_classifications(chunks, state)
        await self._load_first_page_contexts(chunks, state)
        if self._attach_images:
            for chunk in chunks:
                await self._materialize_image_evidence(chunk, state)
            chunks = [state.chunks[chunk.chunk_id] for chunk in chunks]
        children = [
            _stored_child(
                chunk,
                rrf_score=0.0,
                classification=state.classifications.get(chunk.document_id),
                first_page_context=_first_page_context(chunk, state),
            )
            for chunk in chunks
        ]
        return SectionRead(
            [*children, *_stored_parents(children, state)],
            complete=text.complete and len(text.chunks) >= text.section.chunk_count,
        )

    async def _article_catalogs(
        self, state: _SearchState, document_ids: Sequence[str], limit: int
    ) -> list[Any]:
        """候補の文書のうち、条で構成された文書の章節の目録を順位の順に limit 件まで返す(#2254)。

        条で構成された文書は、候補の chunk が XML ツリー JSON の引用ラベルを持つ文書。目録は
        ``rag_outline`` の compact と同じ行(``compact_section_lines``)。読めない文書は飛ばす
        (回答は続ける)。
        """
        from rag_engine.retrieval.article_catalog import ArticleCatalog

        from app.rag.section_catalog import compact_section_lines
        from app.rag.section_chunk_map import ROOT_SECTION_ID
        from app.rag.section_map_service import section_outline

        article_documents = {
            chunk.document_id
            for chunk in state.chunks.values()
            if _xml_tree_location_labels(chunk.metadata) is not None
        } | {document_id for document_id, _ in state.article_documents or ()}
        catalogs: list[Any] = []
        for document_id in document_ids:
            if len(catalogs) >= limit:
                break
            if document_id not in article_documents:
                continue
            if document_id not in state.article_catalogs:
                catalog = None
                try:
                    outline = await section_outline(document_id, oracle=self._oracle)
                    if outline is not None:
                        nodes = [
                            node for node in outline.sections if node.section_id != ROOT_SECTION_ID
                        ]
                        lines, _omitted = await compact_section_lines(
                            self._oracle,
                            outline,
                            nodes,
                            _ARTICLE_CATALOG_LEAD_CHARS,
                            max_chars=_ARTICLE_CATALOG_MAX_CHARS,
                        )
                        if lines:
                            catalog = ArticleCatalog(
                                document_id=document_id,
                                file_name=outline.chunk_set.file_name or "",
                                chunk_set_id=outline.chunk_set.chunk_set_id,
                                lines=tuple(lines),
                                section_ids=tuple(node.section_id for node in nodes[: len(lines)]),
                            )
                except Exception as exc:  # noqa: BLE001 - 条の選択は補助。この文書を飛ばして続ける。
                    logger.warning("article catalog load failed", exc_info=True)
                    state.record_degraded("article_catalog", exc)
                state.article_catalogs[document_id] = catalog
            if state.article_catalogs[document_id] is not None:
                catalogs.append(state.article_catalogs[document_id])
        return catalogs

    async def _article_leads(
        self,
        document_id: str,
        chunk_set_id: str,
        sections: Mapping[str, str],
        *,
        state: _SearchState | None = None,
    ) -> dict[str, str]:
        """章節の本文の書き出し(条の選択の 2 段目)。

        章節の最初の chunk から、名前と見出しの括弧を除く。
        読めないときは空(条の名前だけで選ばせる)。
        """
        from app.clients.section_map_store import SectionMapStore
        from app.rag.section_catalog import section_lead

        if not sections:
            return {}
        try:
            chunks = await SectionMapStore(self._oracle).section_first_chunks(
                document_id, chunk_set_id=chunk_set_id, section_ids=list(sections)
            )
        except Exception as exc:  # noqa: BLE001 - 2 段目は補助。書き出し無しで続ける。
            logger.warning("article lead load failed", exc_info=True)
            if state is not None:
                state.record_degraded("article_lead", exc)
            return {}
        leads: dict[str, str] = {}
        for section_id, chunk in chunks.items():
            text = section_lead(
                sections.get(section_id, ""), chunk.text, _ARTICLE_REFINE_LEAD_CHARS
            )
            if text:
                leads[section_id] = text
        return leads

    async def _article_documents(
        self, request: SearchRequest, state: _SearchState
    ) -> list[tuple[str, str]]:
        """検索の範囲の条で構成された文書の (document_id, 文書名)(#2254)。

        範囲と索引の状態ごとに process の中で cache する(画面目録と同じ)。読めないときは空
        (候補の文書だけを読む)。
        """
        if state.article_documents is not None:
            return state.article_documents
        filters = _screen_filters(request, state)
        try:
            index_state = await self._oracle.retrieval_scope_state(filters)
            key = (json.dumps(filters, ensure_ascii=False, sort_keys=True), index_state)
            documents = _ARTICLE_DOCUMENTS_CACHE.get(key)
            if documents is None:
                documents = await self._oracle.retrieval_article_documents(filters)
                if len(_ARTICLE_DOCUMENTS_CACHE) >= _SCREEN_CATALOG_CACHE_SIZE:
                    _ARTICLE_DOCUMENTS_CACHE.clear()
                _ARTICLE_DOCUMENTS_CACHE[key] = documents
        except Exception as exc:  # noqa: BLE001 - 一覧は補助。候補の文書だけで続ける。
            logger.warning("article documents load failed", exc_info=True)
            state.record_degraded("article_documents", exc)
            documents = []
        state.article_documents = list(documents)
        return state.article_documents

    async def _article_chunks(
        self, request: SearchRequest, state: _SearchState, articles: Sequence[Any]
    ) -> list[Any]:
        """目録で選んだ条の章節の最初の child(印を付ける)と、その親を読む(#2254)。

        読んだ chunk は引用へ戻せるよう ``state.chunks`` に入れ、分類・1 ページ目の本文・根拠画像は
        検索の候補と同じく付ける。読めない条は足さない(回答は続ける)。
        """
        from rag_engine.retrieval.article_catalog import ARTICLE_CATALOG_KEY

        from app.clients.section_map_store import SectionMapStore

        groups: dict[tuple[str, str], list[Any]] = {}
        for article in articles:
            groups.setdefault((article.document_id, article.chunk_set_id), []).append(article)
        found: list[tuple[RetrievedChunk, Any]] = []
        for (document_id, chunk_set_id), items in groups.items():
            try:
                firsts = await SectionMapStore(self._oracle).section_first_chunks(
                    document_id,
                    chunk_set_id=chunk_set_id,
                    section_ids=[item.section_id for item in items],
                )
            except Exception as exc:  # noqa: BLE001 - 条の根拠は補助。この文書の条を足さずに続ける。
                logger.warning("article chunks load failed", exc_info=True)
                state.record_degraded("article_chunks", exc)
                continue
            found.extend(
                (firsts[item.section_id], item)
                for item in items
                if item.section_id in firsts and not is_derived_chunk(firsts[item.section_id])
            )
            for item in items:
                chunk = firsts.get(item.section_id)
                key = chunk.chunk_id if chunk is not None else f"{document_id}:{item.section_id}"
                state.article_selections.setdefault(
                    key,
                    {
                        "order": item.order,
                        "document_id": document_id,
                        "document": item.file_name,
                        "title": item.title,
                        "chunk_id": chunk.chunk_id if chunk is not None else None,
                    },
                )
        if not found:
            return []
        chunks = [state.chunks.setdefault(chunk.chunk_id, chunk) for chunk, _ in found]
        await self._load_classifications(chunks, state)
        await self._load_first_page_contexts(chunks, state)
        if self._attach_images:
            for chunk in chunks:
                await self._materialize_image_evidence(chunk, state)
            chunks = [state.chunks[chunk.chunk_id] for chunk in chunks]
        children = []
        for chunk, (_, article) in zip(chunks, found, strict=True):
            child = _stored_child(
                chunk,
                rrf_score=0.0,
                classification=state.classifications.get(chunk.document_id),
                first_page_context=_first_page_context(chunk, state),
            )
            child.metadata[ARTICLE_CATALOG_KEY] = {
                "order": article.order,
                "document": article.file_name,
                "title": article.title,
            }
            children.append(child)
        return [*children, *_stored_parents(children, state)]

    async def _load_classifications(
        self, chunks: Sequence[RetrievedChunk], state: _SearchState
    ) -> None:
        """まだ読んでいない文書の分類を、ヒットした document_id でまとめて 1 回で読む(#545)。"""
        missing = _missing_classifications(chunks, state)
        if not missing:
            return
        state.classifications.update(await self._oracle.document_classifications(missing))
        state.loaded_classification_ids.update(missing)

    async def _load_first_page_contexts(
        self, chunks: Sequence[RetrievedChunk], state: _SearchState
    ) -> None:
        """まだ読んでいない chunk set の 1 ページ目の本文を、まとめて 1 回で読む(#557)。"""
        missing = _missing_first_page_contexts(chunks, state)
        if not missing:
            return
        state.first_page_contexts.update(await self._oracle.chunk_set_first_page_contexts(missing))
        state.loaded_first_page_chunk_set_ids.update(missing)

    async def _materialize_image_evidence(self, chunk: RetrievedChunk, state: _SearchState) -> None:
        """根拠 chunk の image_evidence を作業ディレクトリへ切り出し、crop_path を差し替える。

        rag_poc の answer_images は ``output_dir / source_run_id / crop_path`` を読むため、
        文書ごとの run ディレクトリへ PNG を書き、metadata(engine_metadata_json)を書き換える。
        切り出せない画像は添付しない(回答は続ける)。同じ chunk は 1 回だけ切り出し、2 回目からは
        その結果の metadata を当てる(#2805)。
        """
        if chunk.chunk_id in state.materialized_images:
            cached = state.materialized_images[chunk.chunk_id]
            if cached is not None:
                state.chunks[chunk.chunk_id] = chunk.model_copy(
                    update={"metadata": {**chunk.metadata, "engine_metadata_json": cached}}
                )
            return
        raw = chunk.metadata.get("engine_metadata_json")
        if isinstance(raw, str) and "image_evidence" not in raw:
            # 画像の根拠の無い chunk は metadata を読まない(本文の JSON を解かない)。
            state.materialized_images[chunk.chunk_id] = None
            return
        metadata = _engine_metadata(chunk)
        images = metadata.get("image_evidence")
        width = chunk.metadata.get("page_width")
        height = chunk.metadata.get("page_height")
        if not isinstance(images, list) or not images or state.work_dir is None:
            state.materialized_images[chunk.chunk_id] = None
            return
        if not isinstance(width, int | float) or not isinstance(height, int | float):
            state.materialized_images[chunk.chunk_id] = None
            return
        run_id = _document_run_id(chunk.document_id)
        crop_dir = state.work_dir / run_id / "crops"
        for image in images:
            if not isinstance(image, dict):
                continue
            bbox = image.get("bbox")
            page = _int(image.get("page"))
            image_id = str(image.get("image_id") or image.get("record_id") or "").strip()
            if not image_id or page < 1 or not isinstance(bbox, list) or len(bbox) != 4:
                continue
            name = f"{_safe_name(image_id)}.png"
            target = crop_dir / name
            if not target.is_file():
                source = await self._parsed_source(chunk.document_id, state)
                if source is None:
                    return
                try:
                    png = await asyncio.to_thread(
                        crop_png,
                        source,
                        page,
                        (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                        (float(width), float(height)),
                    )
                except ValueError:
                    continue
                crop_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(png)
            image["source_run_id"] = run_id
            image["crop_path"] = f"crops/{name}"
        encoded = json.dumps(metadata, ensure_ascii=False, default=str)
        state.materialized_images[chunk.chunk_id] = encoded
        state.chunks[chunk.chunk_id] = chunk.model_copy(
            update={"metadata": {**chunk.metadata, "engine_metadata_json": encoded}}
        )

    async def _parsed_source(self, document_id: str, state: _SearchState) -> bytes | None:
        if document_id not in state.sources:
            try:
                state.sources[document_id] = await load_parsed_source(self._oracle, document_id)
            except (DocumentSourceNotFoundError, ValueError):
                state.sources[document_id] = None
        return state.sources[document_id]

    async def _rerank(self, query: str, documents: list[str], top_n: int | None) -> list[Any]:
        from rag_engine.models.llm import RerankTextRank

        if not documents:
            return []
        ranked = await self._genai.rerank(query, documents, top_n or len(documents))
        return [RerankTextRank(index=index, relevance_score=score) for index, score in ranked]


@dataclass(frozen=True)
class _LoopEmbedder:
    """回答の thread から event loop の OCI Generative AI の embedding を呼ぶ Embedder(#2649)。

    ``rag_engine.ports.Embedder`` を満たす。業務ガイドの手順の充足で、根拠を共有する手順の
    どれを item が説明したかを語で決められないときに使う。
    """

    genai: OciGenAiClient
    loop: asyncio.AbstractEventLoop

    def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        input_type: Literal["SEARCH_QUERY", "SEARCH_DOCUMENT"] = (
            "SEARCH_QUERY" if query else "SEARCH_DOCUMENT"
        )
        return _run_on_loop(lambda: self.genai.embed(list(texts), input_type=input_type), self.loop)


def _run_on_loop[R](factory: Callable[[], Awaitable[R]], loop: asyncio.AbstractEventLoop) -> R:
    """回答の thread から、非同期 I/O を event loop で実行して待つ。

    回答の期限(#1422)を過ぎた・取り消された後は I/O を始めず、待っている途中なら loop の task を
    取り消して ``DeadlineExceeded`` にする(打ち切った回答の検索・rerank を続けない)。期限が無くても
    待つ上限は ``_IO_TIMEOUT_SECONDS``。
    """
    check_deadline()
    future = asyncio.run_coroutine_threadsafe(_call(factory), loop)
    started = time.monotonic()
    while True:
        waited = time.monotonic() - started
        if waited >= _IO_TIMEOUT_SECONDS:
            future.cancel()
            raise TimeoutError("回答フローの I/O が上限の時間内に終わりませんでした。")
        try:
            return future.result(min(_IO_DEADLINE_POLL_SECONDS, _IO_TIMEOUT_SECONDS - waited))
        except concurrent.futures.TimeoutError:
            if future.done():
                # I/O 自体が TimeoutError で失敗した(待ちの上限ではない)。
                raise
            deadline = current_deadline()
            if deadline is not None and deadline.expired:
                future.cancel()
                deadline.check()


async def _call[R](factory: Callable[[], Awaitable[R]]) -> R:
    return await factory()


def question_business_domains(question: str, large_categories: Sequence[str]) -> list[str]:
    """質問が名指しした大分類を、検索範囲の大分類の語の一覧(保存どおりの値)から返す(#553)。

    - 比べるときは #547 の正規化(NFKC・空白・``category_label`` で番号の接頭辞を外す)と
      大文字・小文字の違いを無視する。返す値は保存どおり(rag_engine の ``_same_business_records`` は
      候補の大分類と番号の接頭辞を外して完全一致で比べる)。
    - 長い名前から照合し、照合した箇所は短い名前の照合に使わない
      (「業務A」の中の「業務」を拾わない)。
    - 名指しが無ければ空(絞り込まない)。
    """
    text = (normalize_category_value(question) or "").casefold()
    values_by_label: dict[str, list[str]] = {}
    for value in large_categories:
        label = category_label(value).casefold()
        if len(label) >= _MIN_BUSINESS_NAME_CHARS:
            values_by_label.setdefault(label, []).append(value)
    matched: list[str] = []
    for label in sorted(values_by_label, key=lambda item: (-len(item), item)):
        if label in text:
            text = text.replace(label, "\0")
            matched.extend(values_by_label[label])
    return list(dict.fromkeys(matched))


def _json_list(value: object) -> list[Any]:
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except ValueError:
            return []
        return decoded if isinstance(decoded, list) else []
    return list(value) if isinstance(value, list) else []


def _search_stage(name: str, *, active: bool = True) -> AbstractContextManager[object]:
    """文書の検索の中の工程の段(``observe_call("stage", name)``。#3464)。

    DB の検索の段(``OracleClient`` のベクトル検索・全文検索)と同じ仕組みで、回答の工程の span
    (「文書の検索」)の子の段になる。行うことの無い工程(``active`` が偽)は段を出さない。
    """
    return observe_call("stage", name) if active else nullcontext()


def _missing_classifications(chunks: Sequence[RetrievedChunk], state: _SearchState) -> list[str]:
    """まだ分類を読んでいない文書の id(``_load_classifications`` が読む文書)。"""
    return sorted({chunk.document_id for chunk in chunks} - state.loaded_classification_ids)


def _missing_first_page_contexts(
    chunks: Sequence[RetrievedChunk], state: _SearchState
) -> list[str]:
    """まだ 1 ページ目の本文を読んでいない chunk set の id(``_load_first_page_contexts``)。"""
    chunk_set_ids = {str(chunk.metadata.get("chunk_set_id") or "") for chunk in chunks}
    return sorted(chunk_set_ids - {""} - state.loaded_first_page_chunk_set_ids)


def _needs_image_materialization(chunk: RetrievedChunk, state: _SearchState) -> bool:
    """根拠の図を切り出す(まだ切り出していない、図の根拠を持つ)chunk か。"""
    if chunk.chunk_id in state.materialized_images or state.work_dir is None:
        return False
    raw = chunk.metadata.get("engine_metadata_json")
    return isinstance(raw, str) and "image_evidence" in raw


def _hit_channels(hit: RetrievedChunk, query_index: int) -> set[str]:
    """検索文 1 本の当たりの経路(rag_engine の検索の実装と同じ形。#3443)。

    ``vector_rank`` / ``keyword_rank`` / ``citation_rank``(``OracleClient`` の検索の結果の
    metadata)から決める。順位の無い結果は ``retrieval_mode`` から決める。
    """
    metadata = hit.metadata
    mode = str(metadata.get("retrieval_mode") or "")
    query = f"q{query_index + 1}"
    channels: set[str] = set()
    if "vector_rank" in metadata or mode in {"vector", "hybrid"}:
        channels.add(f"vector:{query}")
    if "keyword_rank" in metadata or mode in {"keyword", "hybrid"}:
        channels.add(f"keyword:oracle_text:{query}")
    if "citation_rank" in metadata or mode == "citation":
        channels.add("citation")
    return channels


def _engine_metadata(chunk: RetrievedChunk) -> dict[str, Any]:
    raw = chunk.metadata.get("engine_metadata_json")
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            return {
                **decoded,
                **{
                    key: chunk.metadata[key]
                    for key in (
                        "context_header",
                        "context_header_enabled",
                        "html_region_role",
                        "table_caption",
                    )
                    if key in chunk.metadata
                },
            }
    # 親子階層以外の分割方式の chunk も最小の v4 metadata で扱う。
    section = str(chunk.metadata.get("section_path") or "")
    return {
        "schema_version": 4,
        "active": True,
        "atomic": False,
        "content_hash": str(chunk.metadata.get("text_sha256") or ""),
        "classification": {},
        "source_categories": ["Text"],
        "section_path": [part for part in section.split(" > ") if part],
        "section_path_sources": [],
        **{
            key: chunk.metadata[key]
            for key in (
                "context_header",
                "context_header_enabled",
                "html_region_role",
                "table_caption",
            )
            if key in chunk.metadata
        },
    }


def _document_run_id(document_id: str) -> str:
    return hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:16]


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:120]


def _reference_rerank_document(chunk: RetrievedChunk) -> str:
    """文書名だけの参照の参照先を rerank にかける本文(見出しの列を先に置く。#1400)。

    rag_engine の rerank(``_rerank_document``)と同じく見出しを先頭に置く。検索用の本文
    (``engine_search_text``)は先頭に文書名・ページの見出しが付き、同じ文書の chunk の関連度の差が
    小さくなる(dev の評価の KB で、定期保守計画の 4 つの章がどれも 0.85〜0.88 だった)。
    """
    path = str(chunk.metadata.get("section_path") or "").strip()
    return f"見出し: {path}\n\n{chunk.text}" if path else chunk.text


def _document_name_targets(
    anchor: RetrievedChunk, stored: Sequence[ReferenceTarget]
) -> list[ReferenceTarget]:
    """取込で残した参照先に無い、本文の文書名だけの参照(#1400)。

    #1400 より前に取り込んだ chunk の印(``reference_targets_json``)には文書名だけの参照が無いので、
    回答のときに本文から足す(再処理しなくても辿れる)。派生の chunk(要約・項目の値。#2641)は
    辿らない。
    """
    if is_derived_chunk(anchor):
        return []
    known = {
        title_key(target.document_title or "") for target in stored if target.kind == "document"
    }
    targets: list[ReferenceTarget] = []
    for spec in document_name_references(anchor.text):
        key = title_key(spec.document_title or "")
        if key in known or same_document_title(spec.document_title or "", anchor.file_name or ""):
            continue
        known.add(key)
        targets.append(ReferenceTarget(spec.label, spec.kind, spec.key, spec.document_title))
    return targets


def _today() -> date:
    """今日の日付(基準日の既定。共通の設定の時間帯。#3255。テストで差し替える)。"""
    return settings_today()


def _with_effective_as_of(request: SearchRequest) -> SearchRequest:
    """検索の基準日(as_of)を決めた要求(#2602)。

    基準日の無い要求は今日にする(``OracleClient`` の有効期間の判定の既定と同じ日)。主検索・先読み・
    交差参照・実体・関係情報の拡張・画面目録・章節の読み取りはこの要求の条件を使うので、回答の途中で
    日付が変わっても、すべての検索が同じ日で有効期間を判定する。明示した基準日は変えない。
    """
    if (request.filters.get(AS_OF_FILTER_KEY) or "").strip():
        return request
    return request.model_copy(
        update={"filters": {**request.filters, AS_OF_FILTER_KEY: _today().isoformat()}}
    )


def _screen_filters(request: SearchRequest, state: _SearchState) -> dict[str, str]:
    """画面目録と画面の chunk を読む条件。検索と同じ実際の条件(#2056)。

    質問から読み取った項目の条件(#652)と質問の文書名・ページ(#546)を足した ``state.filters``
    (条件で 0 件だったときは外した条件)。最初の検索の前で決まっていなければ要求の ``filters``。
    """
    return dict(state.filters if state.filters is not None else request.filters)


def _section_headings(section_path: str) -> list[str]:
    return [part.strip() for part in section_path.split(_SECTION_PATH_SEPARATOR) if part.strip()]


def _chunk_headings(chunk: RetrievedChunk) -> list[str]:
    """chunk の見出しの列。

    rag_engine の metadata の section_path。無ければ保存した文字列を分ける。
    """
    path = _engine_metadata(chunk).get("section_path")
    if isinstance(path, list):
        return [str(part).strip() for part in path]
    return _section_headings(str(chunk.metadata.get("section_path") or ""))


def _finite_float(value: object) -> float | None:
    """有限の数値なら float、それ以外(None・bool・NaN・文字列など)は None。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[call-overload,no-any-return]
    except (TypeError, ValueError):
        return default


def _group_id(chunk: RetrievedChunk) -> str:
    return str(chunk.metadata.get("chunk_group_id") or chunk.chunk_id)


def _diverse_candidates(
    ordered: Sequence[str],
    head_size: int,
    document_of: Callable[[str], str],
    *,
    new_document: int,
    present_document: int,
) -> list[str]:
    """融合の上位 ``head_size`` 件の外から、文書ごとの上位を足す候補(#2228)。

    1 つの文書が融合の上位を占めると、派生の検索文だけが当てた別の文書の根拠は上限の外に落ち、
    rerank の判断を受けない。上位に無い文書は上位 ``new_document`` 件、上位にある文書は続きの
    ``present_document`` 件を融合の順で足す。上位の候補は変えない(削ると上位の正解が落ちる)。
    """
    present = {document_of(chunk_id) for chunk_id in ordered[:head_size]}
    taken: dict[str, int] = {}
    extra: list[str] = []
    for chunk_id in ordered[head_size:]:
        document = document_of(chunk_id)
        limit = present_document if document in present else new_document
        if taken.get(document, 0) < limit:
            taken[document] = taken.get(document, 0) + 1
            extra.append(chunk_id)
    return extra


def _inquiry_channel_rankings(
    pool: Sequence[Any],
    inquiry_conditions: Any,
    rankings: Sequence[Sequence[str]],
    *,
    limit: int,
    profile_enabled: bool,
) -> list[tuple[str, float, Sequence[tuple[str, float | None]]]]:
    """質問の理解に合う候補を、RRF に足すチャネル(profile / business_match)の順位で返す(#546)。

    rag_poc の検索(``adapters/oracle/store.py``)と同じ rag_engine の関数で計算する。
    対象は今回の検索で
    見つかった候補だけで、ナレッジベースの全 chunk は読まない。文書名・ページは検索条件
    (``_question_filters``)で適用済みか、ナレッジベースに無いため外したので、ここでは使わない。
    """
    from rag_engine.adapters.oracle.store import (
        _business_match_ranking,
        _chunks_matching_metadata_filter,
    )
    from rag_engine.retrieval.inquiry_conditions import profile_channel_rankings

    metadata_filter = getattr(inquiry_conditions, "metadata_filter", None)
    if metadata_filter is not None:
        metadata_filter = replace(metadata_filter, source_file_terms=(), page_numbers=())
    matched = _chunks_matching_metadata_filter(pool, metadata_filter)
    channels: list[tuple[str, float, Sequence[tuple[str, float | None]]]] = []
    if profile_enabled:
        channels.extend(profile_channel_rankings(matched, inquiry_conditions, limit=limit))
    channels.extend(
        _business_match_ranking(
            matched,
            metadata_filter,
            [[(chunk_id, None) for chunk_id in ranking] for ranking in rankings],
            [],
            limit=limit,
        )
    )
    return channels


def _first_page_context(chunk: RetrievedChunk, state: _SearchState) -> Mapping[str, object] | None:
    return state.first_page_contexts.get(str(chunk.metadata.get("chunk_set_id") or ""))


def _section_citing_chunks(
    candidates: Sequence[RetrievedChunk],
    selected: Sequence[RetrievedChunk],
    *,
    limit: int,
) -> list[RetrievedChunk]:
    """文書名の参照で選んだ chunk の節を、同じ文書の中で参照する候補(#1549)。

    「時間帯は定期保守計画で確かめます」で定期保守計画の「第 2 章 共通の保守枠」を選んだとき、
    その節がどのシステムに当てはまるかは同じ文書の別の節の文(「この章に無いシステムは、第 2 章の
    共通の保守枠で保守します」)が書く。参照先の文書の中の検索の候補(``candidates``)のうち、取込で
    解決した参照(``reference_targets_json``)が選んだ chunk の節を指すものを、上限(``limit``)まで
    返す。SQL は増えない。
    """
    if limit <= 0:
        return []
    chosen = {chunk.chunk_id for chunk in selected}
    paths = [str(chunk.metadata.get("section_path") or "") for chunk in selected]
    citing: list[RetrievedChunk] = []
    for chunk in candidates:
        if len(citing) >= limit:
            break
        if chunk.chunk_id in chosen:
            continue
        if any(
            target.document_title is None
            and target.section_path
            and any(path and section_path_within(path, target.section_path) for path in paths)
            for target in reference_targets(chunk.metadata)
        ):
            citing.append(chunk)
    return citing


def _reference_source_entry(
    anchor: RetrievedChunk,
    position: int,
    scores: Mapping[str, float] | None,
    targets: Sequence[ReferenceTarget],
    outcome: str,
) -> dict[str, object]:
    """交差参照の起点の記録(診断。#1549)。統合の順位・点と、参照の表記・種類・結果。

    ``outcome`` は added(足した)/ none(参照先が見つからない・すでに上位にある)/ referenced
    (他の起点の参照先として足した)/ budget(上限)/ target_limit(上位 5 件の外で試す参照先の
    上限)/ outside_sources(起点の外)。
    """
    score = (scores or {}).get(anchor.chunk_id)
    return {
        "chunk_id": anchor.chunk_id,
        "file_name": anchor.file_name or "",
        "fused_rank": position,
        **({"rrf_score": round(score, 6)} if score is not None else {}),
        "targets": [{"label": target.label, "kind": target.kind} for target in targets],
        "outcome": outcome,
    }


def _stored_child(
    chunk: RetrievedChunk,
    *,
    rrf_score: float,
    classification: Mapping[str, object] | None = None,
    first_page_context: Mapping[str, object] | None = None,
    retrieval_channels: Collection[str] | None = None,
) -> Any:
    """backend の chunk を rag_engine の子 chunk にする。

    ``classification`` は文書の分類(rag_documents.classification)。rag_engine は
    ``metadata["document"]["classification"]`` で業務の候補を絞る
    (``_same_business_records``。#545)。
    ``first_page_context`` は chunk set の文書の 1 ページ目の本文。rag_engine は
    ``metadata["document"]["first_page_context"]`` を回答の「文書の背景」にする(#557)。
    chunk 自身が持つ値(#557 より前に保存した chunk)があれば、そちらを使う。
    分類と 1 ページ目の本文は chunk の保存内容(埋め込み・検索文)には入れず、回答のときにだけ付ける。
    ``retrieval_channels`` は検索で当たった経路(``_hit_channels``)。rag_engine の検索の実装と同じ
    ``adb_hybrid.retrieval_channels`` に書く(根拠の記録と、文書の検索の工程の候補の経路。#3443)。
    ``adb_hybrid`` には経路だけを書き、並べ替え・文脈の選択に効く値(``rrf_score`` など)は書かない。
    """
    from rag_engine.models.storage import StoredChunk

    metadata = _engine_metadata(chunk)
    metadata["rrf_score"] = rrf_score
    if retrieval_channels:
        metadata["adb_hybrid"] = {"retrieval_channels": sorted(retrieval_channels)}
    metadata["document_id"] = chunk.document_id
    # 現在の設定でなく、命中した成果物の方式・版を文脈組み立てへ渡す。
    for key in ("chunk_strategy", "recipe_id", "chunk_set_id"):
        metadata[key] = chunk.metadata.get(key, "")
    if classification:
        document = metadata.get("document")
        metadata["document"] = {
            **(document if isinstance(document, dict) else {}),
            "classification": dict(classification),
        }
    if first_page_context:
        document = metadata.get("document")
        document = document if isinstance(document, dict) else {}
        if "first_page_context" not in document:
            metadata["document"] = {**document, "first_page_context": dict(first_page_context)}
    if location := _sheet_location(chunk.metadata):
        # 表計算の根拠の場所(#1221)。回答の本文の出典を「シート・セル範囲」にする(#1224)。
        metadata["sheet_location"] = location
    if xml_tree_location := _xml_tree_location_labels(chunk.metadata):
        # XML ツリー JSON の段落の根拠(#2024)。回答の本文の出典を引用ラベルにする(ページの
        # 無い原本なので「p.1」にしない)。親(単位)の場所は単位のラベル。
        metadata["location_label"], metadata["parent_location_label"] = xml_tree_location
        # 条で構成された文書の条の根拠(#3526)。回答はこの根拠を出典にした説明を事実の監査に掛ける。
        metadata[ARTICLE_UNIT_KEY] = True
    if chunk.metadata.get("document_superseded") is True:
        # 新しい版に置き換えた文書(旧版)の chunk(#1248)。rag_engine は旧版の説明を今の手順として
        # 公開しない(#1492)。
        metadata["document_superseded"] = True
    if element_ids := chunk_element_ids(chunk.metadata):
        # 解析の要素の ID。複数レシピ融合で元の文書の範囲が同じ根拠を 1 つにまとめる(#1331)。
        metadata["element_ids"] = element_ids
    if isinstance(expansion := chunk.metadata.get(ENTITY_EXPANSION_KEY), dict):
        # 実体の 1 段の拡張で足した chunk(#1362)。rag_engine は rerank で位置を変えず、文書の選択で
        # 後回しにせず、文脈の親の枠を確保する。
        metadata["entity_expansion"] = dict(expansion)
    if isinstance(graph := chunk.metadata.get(GRAPH_EXPANSION_KEY), dict):
        # 関係情報で足した chunk(#2055)。rag_engine は他の候補と同じく rerank で並べる(足した理由を
        # 回答の記録の根拠に残すだけ)。
        metadata[GRAPH_EXPANSION_KEY] = dict(graph)
    if source := str(chunk.metadata.get(REFERENCE_FROM_KEY) or ""):
        # 交差参照で足した参照先(#1280・#1400)。rag_engine は rerank の後に参照元の直後へ置き、
        # 文書の選択で後回しにしない(質問で並べると橋渡しの参照先が下がり、文脈に入らなかった。
        # #1549)。
        rank = chunk.metadata.get(REFERENCE_RANK_KEY)
        metadata["reference_expansion"] = {
            "from_chunk_uid": source,
            "label": str(chunk.metadata.get(REFERENCE_LABEL_KEY) or ""),
            **({"rank": rank} if isinstance(rank, int) else {}),
        }
    if is_attachment_chunk(chunk.metadata):
        # 会話の添付から選んだ箇所(#2517)。rag_engine は文書の選択で後回しにせず、文書ごとの
        # 起点の上限で数えない(#3100)。backend の印(`chat_attachment_id`)は engine の metadata に
        # 入らないので、engine の印に写す(#3126)。
        metadata[ATTACHMENT_EVIDENCE_KEY] = True
    chunk_set_id = str(chunk.metadata.get("chunk_set_id") or "")
    artifact_id = chunk_set_id or str(chunk.metadata.get("recipe_id") or "")
    parent_uid = ":".join(
        part for part in (chunk.document_id, artifact_id, _group_id(chunk)) if part
    )
    page_start = _int(chunk.metadata.get("page_start") or chunk.metadata.get("page_number"), 1)
    return StoredChunk(
        chunk_uid=chunk.chunk_id,
        chunk_id=chunk.chunk_id,
        chunk_level="child",
        chunk_seq=_int(chunk.metadata.get("engine_chunk_seq") or chunk.metadata.get("chunk_index")),
        parent_chunk_uid=parent_uid,
        parent_chunk_id=parent_uid,
        child_chunk_ids=(),
        text=chunk.text,
        retrieval_text=engine_search_text(chunk.metadata) or chunk.text,
        source_run_id=ENGINE_SOURCE_RUN_ID,
        source_file_name=_source_file_name(chunk),
        source_engine_id="docling",
        source_engine_label="Docling",
        page_start=page_start,
        page_end=_int(chunk.metadata.get("page_end"), page_start),
        source_seq_ranges=tuple(_json_list(chunk.metadata.get("source_seq_ranges_json"))),
        source_record_refs=tuple(_json_list(chunk.metadata.get("source_record_refs_json"))),
        metadata=metadata,
    )


# 新しい版に置き換えた文書(旧版)の出典の文書名に付ける印(#1248)。
SUPERSEDED_FILE_NAME_SUFFIX = "（旧版）"


def _source_file_name(chunk: RetrievedChunk) -> str:
    """回答の出典に出す文書名。旧版の文書は「(旧版)」を付け、今の版と区別できるようにする。"""
    file_name = chunk.file_name or ""
    if file_name and chunk.metadata.get("document_superseded") is True:
        return f"{file_name}{SUPERSEDED_FILE_NAME_SUFFIX}"
    return file_name


_CELL_RANGE = re.compile(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$")


def _xml_tree_location_labels(metadata: Mapping[str, object]) -> tuple[str, str] | None:
    """XML ツリー JSON の段落の chunk の場所(子の引用ラベル, 親(単位)のラベル)(#2024)。

    算用数字の形にする。親は単位のラベル(単位の無い段落は子と同じ)。この解析でなければ None。
    """
    label = metadata.get("xml_tree_label_arabic")
    if not isinstance(label, str) or not label.strip():
        return None
    unit = metadata.get("xml_tree_parent_label_arabic")
    title = metadata.get("xml_tree_document_title")
    parent = label
    if isinstance(unit, str) and unit.strip():
        parent = unit.strip()
        if isinstance(title, str) and title and parent.startswith(f"{title} "):
            parent = parent[len(title) + 1 :]
    return label.strip(), parent


def _sheet_location(metadata: Mapping[str, object]) -> dict[str, object] | None:
    """Excel の行の記録の chunk の場所(#1221 の sheet_name・row_start・row_end・cell_range)。"""
    sheet = metadata.get("sheet_name")
    if not isinstance(sheet, str) or not sheet.strip():
        return None
    location: dict[str, object] = {"sheet_name": sheet.strip()}
    for key in ("row_start", "row_end"):
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            location[key] = value
    cell_range = metadata.get("cell_range")
    if isinstance(cell_range, str) and _CELL_RANGE.match(cell_range.strip()):
        location["cell_range"] = cell_range.strip()
    return location


def _column_number(letters: str) -> int:
    number = 0
    for char in letters:
        number = number * 26 + (ord(char) - 64)
    return number


def _merged_sheet_location(locations: list[object]) -> dict[str, object] | None:
    """親の chunk の場所(子の場所が同じシートなら、行と列の範囲をまとめる。#1224)。"""
    items = [item for item in locations if isinstance(item, dict)]
    sheets = {item.get("sheet_name") for item in items}
    if not items or len(sheets) != 1:
        return None
    ranges = [
        match.groups()
        for item in items
        if isinstance(item.get("cell_range"), str)
        and (match := _CELL_RANGE.match(str(item["cell_range"])))
    ]
    location: dict[str, object] = {"sheet_name": next(iter(sheets))}
    if ranges:
        first_column = min((r[0] for r in ranges), key=_column_number)
        last_column = max((r[2] for r in ranges), key=_column_number)
        row_start = min(int(r[1]) for r in ranges)
        row_end = max(int(r[3]) for r in ranges)
        location.update(
            row_start=row_start,
            row_end=row_end,
            cell_range=f"{first_column}{row_start}:{last_column}{row_end}",
        )
    return location


def _parent_sheet_location(metadata: Mapping[str, object]) -> dict[str, object] | None:
    """行の記録の子が持つ、親(表の一部)の場所(#1349)。無ければ None。"""
    sheet = metadata.get("sheet_name")
    cell_range = metadata.get("parent_cell_range")
    row_start = metadata.get("parent_row_start")
    row_end = metadata.get("parent_row_end")
    if (
        not isinstance(sheet, str)
        or not sheet.strip()
        or not isinstance(cell_range, str)
        or not _CELL_RANGE.match(cell_range.strip())
        or not isinstance(row_start, int)
        or not isinstance(row_end, int)
    ):
        return None
    return {
        "sheet_name": sheet.strip(),
        "row_start": row_start,
        "row_end": row_end,
        "cell_range": cell_range.strip(),
    }


def _with_context_scope(child: Any, scope: str) -> Any:
    if not scope or child.metadata.get(CONTEXT_PAGE_SCOPE) == scope:
        return child
    metadata = dict(child.metadata)
    metadata[CONTEXT_PAGE_SCOPE] = scope
    document = metadata.get("document")
    if isinstance(document, dict):
        metadata["document"] = {
            key: value for key, value in document.items() if key != "first_page_context"
        }
    suffix = hashlib.sha256(scope.encode()).hexdigest()[:16]
    return replace(
        child,
        metadata=metadata,
        parent_chunk_uid=f"{child.parent_chunk_uid}:scope:{suffix}",
        parent_chunk_id=f"{child.parent_chunk_id}:scope:{suffix}",
    )


def _stored_parents(children: list[Any], state: _SearchState) -> list[Any]:
    """子の metadata に保持した親本文から親 chunk を復元する。"""
    from rag_engine.models.storage import StoredChunk

    by_parent: dict[str, list[Any]] = {}
    for child in children:
        if child.metadata.get("chunk_strategy") == "small_to_big":
            by_parent.setdefault(child.parent_chunk_uid, []).append(child)
    parents = []
    for parent_uid, members in by_parent.items():
        members.sort(key=lambda child: child.chunk_seq)
        source = state.chunks.get(members[0].chunk_uid)
        parent_text = str((source.metadata if source else {}).get("parent_text") or "")
        if not parent_text.strip():
            continue
        source_metadata = source.metadata if source else {}
        scope = str(
            members[0].metadata.get(
                CONTEXT_PAGE_SCOPE,
                (getattr(state, "filters", None) or {}).get(PAGE_RANGES_FILTER_KEY, ""),
            )
        )
        if scope and not contained(
            str(members[0].metadata.get("document_id", "")), source_metadata, scope, parent=True
        ):
            state.retrieval_stages.append(
                {
                    "stage": "context_page_scope",
                    "id": parent_uid,
                    "reason": "parent_range_unproven_or_outside_scope",
                }
            )
            continue
        text = parent_text
        metadata = dict(members[0].metadata)
        _merge_member_metadata(metadata, members)
        _restore_parent_native_ranges(text, metadata, members, state)
        metadata.pop("rrf_score", None)
        if element_ids := list(
            dict.fromkeys(
                element_id for child in members for element_id in chunk_element_ids(child.metadata)
            )
        ):
            # 親の範囲は今回見つかった子の要素の和(#1331)。
            metadata["element_ids"] = element_ids
        if parent_text and isinstance(parent_label := metadata.get("parent_location_label"), str):
            # XML ツリー JSON の親(単位)の場所(#2024)。
            metadata["location_label"] = parent_label
        if location := (
            _parent_sheet_location(source.metadata if source else {}) if parent_text else None
        ) or _merged_sheet_location([child.metadata.get("sheet_location") for child in members]):
            # 親の本文が表の一部(行の記録の親子。#1349)なら、その本文の範囲を親の場所にする。
            metadata["sheet_location"] = location
        parents.append(
            StoredChunk(
                chunk_uid=parent_uid,
                chunk_id=parent_uid,
                chunk_level="parent",
                chunk_seq=members[0].chunk_seq,
                parent_chunk_uid="",
                parent_chunk_id="",
                child_chunk_ids=tuple(child.chunk_id for child in members),
                text=text,
                retrieval_text=text,
                source_run_id=ENGINE_SOURCE_RUN_ID,
                source_file_name=members[0].source_file_name,
                source_engine_id="docling",
                source_engine_label="Docling",
                page_start=positive(source_metadata.get("parent_page_start"))
                or min(child.page_start for child in members),
                page_end=positive(source_metadata.get("parent_page_end"))
                or max(child.page_end for child in members),
                source_seq_ranges=tuple(
                    item for child in members for item in child.source_seq_ranges
                ),
                source_record_refs=tuple(
                    item for child in members for item in child.source_record_refs
                ),
                metadata=metadata,
            )
        )
    return parents


def _restore_parent_native_ranges(
    parent_text: str, metadata: dict[str, Any], members: Sequence[Any], state: _SearchState
) -> None:
    """子の原文座標を、同じ版の親に一意に置ける場合だけ親の座標へ移す。"""
    layout = metadata.get("layout")
    if not isinstance(layout, dict) or "native_text_ranges" not in layout:
        return
    layout = dict(layout)
    layout.pop("native_text_ranges", None)
    metadata["layout"] = layout
    sources = [state.chunks.get(child.chunk_uid) for child in members]
    parent_conflict = any(
        source is not None and source.metadata.get("parent_text") != parent_text
        for source in sources
    )
    first_source = sources[0]
    translated: list[dict[str, Any]] = []
    for child, source in zip(members, sources, strict=True):
        child_layout = child.metadata.get("layout")
        if not isinstance(child_layout, dict) or "native_text_ranges" not in child_layout:
            continue
        ranges = child_layout["native_text_ranges"]
        reason = ""
        offset = -1
        if parent_conflict:
            reason = "parent_text_conflict"
        elif source is None or first_source is None:
            reason = "source_missing"
        elif (
            source.document_id != first_source.document_id
            or any(
                source.metadata.get(key, "") != first_source.metadata.get(key, "")
                for key in ("chunk_set_id", "recipe_id")
            )
            or source.text != child.text
            or source.document_id != child.metadata.get("document_id")
            or any(
                source.metadata.get(key, "") != child.metadata.get(key, "")
                for key in ("chunk_set_id", "recipe_id")
            )
        ):
            reason = "source_version_mismatch"
        else:
            offset = parent_text.find(child.text) if child.text else -1
            if offset < 0:
                reason = "child_text_missing"
            elif parent_text.find(child.text, offset + 1) >= 0:
                reason = "child_text_ambiguous"
        valid: list[dict[str, Any]] = []
        invalid = not isinstance(ranges, list)
        if not reason and isinstance(ranges, list):
            for entry in ranges:
                if not isinstance(entry, dict):
                    invalid = True
                    continue
                start, end = entry.get("start"), entry.get("end")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end <= len(child.text)
                ):
                    invalid = True
                    continue
                value = {**entry, "start": offset + start, "end": offset + end}
                if value not in translated:
                    translated.append(value)
                valid.append(value)
        state.retrieval_stages.append(
            {
                "stage": "parent_native_range_coordinates",
                "source_id": child.chunk_uid,
                "status": "unknown" if reason or invalid else "verified",
                "reason": reason or ("invalid_child_range" if invalid else "unique_child_offset"),
                "translated_range_count": len(valid),
            }
        )
    if translated:
        layout["native_text_ranges"] = translated


# 子ごとに持つ一覧の metadata。親は今回の子の全員の値を合わせる(先頭の子だけにしない。#3478)。
_MEMBER_LIST_METADATA_KEYS = ("image_evidence", "table_context", "source_categories")


def _merge_member_metadata(metadata: dict[str, Any], members: Sequence[Any]) -> None:
    """親の metadata の子ごとの一覧(画像・表・注釈の文脈・要素の種類)を、子の全員から合わせる。

    親の本文は子の全員を含むので、図の子が親の先頭でなくても、その図を親の根拠として扱う(回答に
    添付する画像は親の ``image_evidence`` から選ぶ)。並びは子の関連度(``rrf_score`` の高い順、同じ
    なら文書の順)で、関連する子の画像を先に置く。同じ画像・表・文脈は 1 回だけ。図の中の文字(OCR)の
    entry は読むときに持ち主の図にまとめる(``fold_picture_ocr_entries``)ので、そのまま残す。
    """
    if len(members) < 2:
        return
    ranked = sorted(
        members, key=lambda child: -(_finite_float(child.metadata.get("rrf_score")) or 0.0)
    )
    for key in _MEMBER_LIST_METADATA_KEYS:
        merged = _merged_metadata_lists(child.metadata.get(key) for child in ranked)
        if merged:
            metadata[key] = merged
    layouts = [child.metadata.get("layout") for child in ranked]
    layouts = [layout for layout in layouts if isinstance(layout, dict)]
    if not layouts:
        return
    layout: dict[str, Any] = {}
    for key in dict.fromkeys(key for item in layouts for key in item):
        values = [item[key] for item in layouts if key in item]
        if all(isinstance(value, list) for value in values):
            layout[key] = _merged_metadata_lists(values)
        elif all(isinstance(value, dict) for value in values):
            layout[key] = {k: v for value in reversed(values) for k, v in value.items()}
        else:
            layout[key] = values[0]
    metadata["layout"] = layout


def _merged_metadata_lists(groups: Iterable[Any]) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            key = _metadata_item_key(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _metadata_item_key(item: Any) -> str:
    """一覧の要素の同一性。画像・表は種類と ID、ID の無いものは内容。"""
    if isinstance(item, dict):
        item_id = str(item.get("image_id") or item.get("table_id") or item.get("record_id") or "")
        if item_id.strip():
            return f"{item.get('raw_type') or ''}:{item_id.strip().lower()}"
    return json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)


def _answer_use(child: Mapping[str, Any]) -> str | None:
    """回答エンジンの evidence item の使われ方（``answer_use``）。決まった値だけを返す（#2040）。"""
    value = str(child.get("answer_use") or "")
    return value if value in ANSWER_USES else None


def _outcome_from_result(result: Any, state: _SearchState) -> AnswerOutcome:
    """rag_poc の AnswerQuestionResult を backend の回答・引用・診断へ写す。"""
    support_display = citation_support_display(result.generation_trace)
    tree: list[dict[str, Any]] = []
    # (並びの順位, chunk_id, 根拠の子, 検索で当たった子の関連度の順位（無ければ 0）)。
    ordered: list[tuple[int, str, dict[str, Any], int]] = []
    # rerank の結果(chunk_id → (順位, 関連度))。rag_engine は rerank した候補の record に
    # ``metadata["rerank"]`` を載せ、根拠の child に ``rerank_rank`` / ``rerank_score`` として出す。
    # 引用へ写さないと、rerank を実行しても画面は「Rerank 未実行」になる(#662)。
    reranked: dict[str, tuple[int | None, float]] = {}
    for position, parent in enumerate(result.evidence_items or ()):
        children = []
        for child in parent.get("children") or []:
            chunk_id = str(child.get("chunk_id") or child.get("id") or "")
            used = bool(child.get("is_model_used"))
            rerank_score = _finite_float(child.get("rerank_score"))
            if rerank_score is not None and chunk_id not in reranked:
                position_rank = _int(child.get("rerank_rank"))
                reranked[chunk_id] = (position_rank if position_rank > 0 else None, rerank_score)
            fused = state.fused_ranks.get(chunk_id)
            children.append(
                {
                    "chunk_id": chunk_id,
                    "role": str(child.get("retrieval_role") or ""),
                    "reason": str(child.get("context_reason") or ""),
                    # 条の目録で選んだ条と、そのために足した親の chunk(#2261)。
                    "catalog": CATALOG_ARTICLE_REASON
                    in {child.get("context_reason"), parent.get("context_reason")},
                    "is_model_used": used,
                    # 子そのものは引用していないが、回答が親（まとまり）を参照した（#2710）。
                    "parent_used": not used and child.get("parent_model_used") is True,
                    # 回答での使われ方（confirmed=確かめた説明 / reference=参考。#2040）。
                    "answer_use": _answer_use(child) if used else None,
                    "page": child.get("page_start"),
                    # 回答の検索の統合(RRF。rerank の前)の順位と点(#1549)。検索の候補に無い
                    # chunk(前後の文脈・参照先として読んだもの)には無い。
                    **(
                        {"fused_rank": fused[0], "rrf_score": round(fused[1], 6)}
                        if fused is not None
                        else {}
                    ),
                }
            )
            rank = _int(child.get("model_usage_rank"), 10_000) if used else 10_000 + position
            # 当たった子（retrieved_anchor）の関連度の順位は rerank の後の順（#1348）。
            ordered.append((rank, chunk_id, children[-1], _int(child.get("retrieval_rank"))))
        tree.append(
            {
                "parent_id": str(parent.get("chunk_id") or parent.get("id") or ""),
                "source": str(parent.get("source") or ""),
                "page": parent.get("page_start"),
                "reason": str(parent.get("context_reason") or ""),
                "children": children,
            }
        )
    citations: list[RetrievedChunk] = []
    seen: set[str] = set()
    # 引用（AI に渡した箇所）にした根拠の子。同じ chunk が 2 つのまとまりにあれば、引用に使った方。
    cited_children: set[int] = set()
    for _, chunk_id, evidence, retrieval_rank in sorted(ordered, key=lambda item: item[0]):
        chunk = state.chunks.get(chunk_id)
        if chunk is None or chunk_id in seen:
            continue
        seen.add(chunk_id)
        cited_children.add(id(evidence))
        metadata: dict[str, Any] = {
            **chunk.metadata,
            "evidence_role": evidence["role"],
            "evidence_model_used": evidence["is_model_used"],
            **(
                {"evidence_claim_support": dict(support_display)}
                if support_display is not None
                else {}
            ),
        }
        if evidence.get("answer_use"):
            # 回答に使った箇所の使われ方（画面の件数・バッジと MCP の answer_use。#2040）。
            metadata["evidence_answer_use"] = evidence["answer_use"]
        if evidence.get("parent_used"):
            # 回答が親（まとまり）だけを参照した子（#2710）。「回答で引用」とは分けて数える。
            metadata[EVIDENCE_PARENT_USED_KEY] = True
        if evidence.get("catalog"):
            # 条の目録で選んだ条(#2261)。MCP は検索の根拠の上限に数えず、後ろに足す。
            metadata["evidence_role"] = CATALOG_ARTICLE_ROLE
        elif ENTITY_EXPANSION_KEY in chunk.metadata and evidence["role"] in {
            "retrieved_anchor",
            "",
        }:
            # 実体の 1 段の拡張で足した chunk は、検索で当たった chunk と分ける(#1362)。
            metadata["evidence_role"] = ENTITY_EXPANSION_ROLE
        if retrieval_rank > 0:
            # MCP の根拠は当たった子を関連度の順に先にする（画面の並びは変えない。#1348）。
            metadata["evidence_retrieval_rank"] = retrieval_rank
        update: dict[str, Any] = {"metadata": metadata}
        if chunk_id in reranked:
            rerank_rank, rerank_score = reranked[chunk_id]
            update["rerank_score"] = rerank_score
            if rerank_rank is not None:
                metadata["rerank_rank"] = rerank_rank
        citations.append(chunk.model_copy(update=update))
    # 根拠の木（画面の「まとまりごと」の表示）は、引用にした箇所だけを 1 回ずつ持つ。
    # 重複した chunk と引用に入らない chunk（本文を読めなかったもの）を残すと、まとまりの
    # 件数の合計が「AI に渡した N 件」とずれる（#2097）。箇所の無くなったまとまりは落とす。
    tree = [
        {**parent, "children": kept}
        for parent in tree
        if (kept := [child for child in parent["children"] if id(child) in cited_children])
    ]
    citations = _with_citation_lines(
        citations,
        result.evidence_items or (),
        (result.generation_trace or {}).get("citation_lines"),
    )
    steps = [
        {
            "name": str(step.get("name") or ""),
            "status": str(step.get("status") or ""),
            "elapsed_seconds": step.get("elapsed_seconds"),
            "llm_calls": step.get("llm_calls"),
            # API への実際の送信の回数（送り直しで呼び出しの回数より多いときだけ。#2966）。
            **(
                {"llm_sends": step["llm_sends"]}
                if isinstance(step.get("llm_sends"), int)
                and not isinstance(step.get("llm_sends"), bool)
                else {}
            ),
            # 入れ子の深さ（0 が最上位。画面の処理の経過の字下げ。#2096）。
            "depth": step.get("depth") if isinstance(step.get("depth"), int) else 0,
            # 工程の中で実行した SQL の回数・行数・LOB の読み取りの回数・時間(#1490)。
            **{
                key: value
                for key, value in step.items()
                if key in _STEP_DB_METRIC_KEYS and isinstance(value, int | float)
            },
            # LLM の token の数（入力・出力・推論）と呼び出しの秒の和(#1516)。
            **(
                {"llm_usage": dict(step["llm_usage"])}
                if isinstance(step.get("llm_usage"), Mapping)
                else {}
            ),
            # この工程だけの詳細（見出しと値・行。段を開いて見る。#2294 / #2341）。
            # 共有の形にそろえ、上限で切る。
            **(
                {"details": details}
                if (details := step_details_payload(step.get("details")))
                else {}
            ),
        }
        for step in result.execution_steps or ()
        if isinstance(step, Mapping)
    ]
    diagnostics = {
        "retrieval_stages": list(state.retrieval_stages),
        "answer_flow": result.answer_flow,
        "strategy": result.effective_strategy,
        "confidence": result.confidence,
        "needs_human_review": result.needs_human_review,
        "insufficient_reason": result.insufficient_reason,
        # 回答の構造（AnswerEnvelope。#1235）。outcome は回答の対応（answered / conditional / …）。
        "outcome": (result.envelope or {}).get("outcome"),
        "envelope": dict(result.envelope or {}),
        # 段落の印・根拠と矛盾して外した段落・回答全体の警告（#2738。形は
        # platform/contracts/answer-passages/passage-marks.json）。画面・MCP・評価が読む。
        **passage_mark_record(result.envelope),
        "reasoning_summary": result.reasoning_summary,
        # rag_poc の回答 viewer が出していた外部データの確認と問い合わせ型(#651)。
        "external_data_required": result.external_data_required,
        "external_data_items": list(result.external_data_items or ()),
        "question_type": list(result.question_type or ()),
        "generated_queries": list(result.generated_queries),
        "text_search_tokens": list(result.text_search_tokens),
        "crag_attempt_count": len(result.crag_attempts or ()),
        "crag_attempts": [dict(attempt) for attempt in result.crag_attempts or ()],
        # 要求ごとの根拠の覆域と、根拠の無い要求の再検索(#1279)。
        "request_coverage": dict((result.generation_trace or {}).get("request_coverage") or {}),
        "request_recovery": [
            dict(row["request_recovery"])
            for row in (result.generation_trace or {}).get("rounds", ())
            if isinstance(row, Mapping) and isinstance(row.get("request_recovery"), Mapping)
        ],
        # 業務ガイドの手順ごとの検索(#2395)。業務ガイドの記録(diagnostics.guide)に移す。
        **(
            {"guide_step_retrieval": dict(trace)}
            if isinstance(trace := (result.generation_trace or {}).get("guide_steps"), Mapping)
            else {}
        ),
        # 業務ガイドの手順ごとの充足(#2491)と、手順の対応付けの embedding の呼び出し(#2649)。
        # 業務ガイドの記録(diagnostics.guide)に移す(#2686)。
        **_guide_step_diagnostics(result.generation_trace),
        # 当たった chunk の章節から親の章節へ広げた記録(#1436。段・読んだ章節・止めた理由・
        # 読めなかった章節)。
        "section_expansion": _section_expansion_diagnostics(result.generation_trace),
        # 採用した草稿の構造(#1492。確認の項目の事実と規則の分け方・説明の種類と適用)。
        # LLM を呼ばずに対応の決定を replay する材料。
        "grounded_draft": dict((result.generation_trace or {}).get("grounded_draft") or {}),
        # 原文と生応答は回答の保存・権限確認に従う。計測やモデル入力へ再注入しない。
        "generation_citation_traces": [
            dict(row["generation_citation_trace"])
            for row in (result.generation_trace or {}).get("rounds", ())
            if isinstance(row, Mapping)
            and isinstance(row.get("generation_citation_trace"), Mapping)
        ],
        # 生成と監査の是正の round の時間の配分(#1516。各組の秒・見積もり・時間で打ち切った理由)。
        "round_budget": dict((result.generation_trace or {}).get("round_budget") or {}),
        # 根拠の文字予算の設定値・実際に使った値・送った根拠ブロックの文字数(#3776。数値だけ)。
        "evidence_budget": dict((result.generation_trace or {}).get("evidence_budget") or {}),
        "generation_input_budget": dict(
            (result.generation_trace or {}).get("generation_input_budget") or {}
        ),
        "evidence_refinement": dict(
            (result.generation_trace or {}).get("evidence_refinement") or {}
        ),
        # 質問に名前が出た根拠の画面の図の項目の種類と、それで決まった業務ガイドの条件(#2404)。
        "form_field_kinds": dict((result.generation_trace or {}).get("form_field_kinds") or {}),
        # 採用した草稿の監査の目的との合い方(#2402。aligned / partial / off_target)。品質評価が
        # 目的外の割合を集計する。
        "goal_alignment": adopted_goal_alignment(result.generation_trace),
        "generation_review": adopted_review_contract(result.generation_trace),
        **(
            {"semantic_audit_policy": dict(policy)}
            if isinstance(
                policy := (result.generation_trace or {}).get("semantic_audit_policy"), Mapping
            )
            else {}
        ),
        "crag_assessment": dict((result.generation_trace or {}).get("crag_assessment") or {}),
        "crag_generation_gate": dict(
            (result.generation_trace or {}).get("crag_generation_gate") or {}
        ),
        "procedure_quotes": dict((result.generation_trace or {}).get("procedure_quotes") or {}),
        "execution_steps": steps,
        "evidence_tree": tree,
    }
    context_text = "\n\n".join(chunk.text for chunk in citations)
    answer = result.answer_text or result.answer
    return AnswerOutcome(
        answer=answer,
        citations=citations,
        diagnostics=diagnostics,
        context_text=context_text,
        evaluation_input=_evaluation_input(result),
    )


_GUIDE_STEP_COVERAGE_FIELDS = ("step_id", "status", "required")
_GUIDE_STEP_EMBEDDING_FIELDS = ("calls", "texts", "elapsed_ms", "failed")


def _guide_step_diagnostics(generation_trace: object) -> dict[str, Any]:
    """回答の記録に残す業務ガイドの手順の充足と embedding の呼び出し(#2686)。

    手順は id・状態・必須かと、対応の方法・item の index・各段の根拠 ID を残す。手順の題名・
    根拠の本文・引用は記録に増やさない。業務ガイドに沿わない回答・呼ばなかった回答では空。
    """
    trace = generation_trace if isinstance(generation_trace, Mapping) else {}
    diagnostics: dict[str, Any] = {}
    coverage = trace.get("guide_step_coverage")
    if isinstance(coverage, list) and coverage:
        diagnostics["guide_step_coverage"] = [
            {
                **{key: step.get(key) for key in _GUIDE_STEP_COVERAGE_FIELDS},
                **{
                    key: step[key]
                    for key in ("assignment", "evidence_flow")
                    if isinstance(step.get(key), Mapping)
                },
            }
            for step in coverage
            if isinstance(step, Mapping)
        ]
    embedding = trace.get("guide_step_embedding")
    if isinstance(embedding, Mapping):
        diagnostics["guide_step_embedding"] = {
            key: embedding.get(key) for key in _GUIDE_STEP_EMBEDDING_FIELDS
        }
    return diagnostics


def adopted_review_contract(generation_trace: object) -> dict[str, Any]:
    """採用した草稿の引用照合と意味の監査を別々に記録する。"""
    rounds = generation_trace.get("rounds") if isinstance(generation_trace, Mapping) else None
    adopted = (
        next(
            (row for row in reversed(rounds) if isinstance(row, Mapping) and row.get("accepted")),
            None,
        )
        if isinstance(rounds, list)
        else None
    )
    if adopted is None:
        return {"execution": "not_run", "items": []}
    return {
        "execution": adopted.get("review_execution", "not_run"),
        **(
            {"dimensions": dict(adopted["validation_dimensions"])}
            if isinstance(adopted.get("validation_dimensions"), Mapping)
            else {}
        ),
        "items": [
            {
                key: row.get(key)
                for key in (
                    "evidence_id",
                    "quote_match",
                    "citation_resolution",
                    "review_assessment",
                    "removed",
                    "withdrawn",
                )
            }
            for row in adopted.get("items", [])
            if isinstance(row, Mapping)
        ],
    }


def adopted_goal_alignment(generation_trace: object) -> str | None:
    """採用した草稿（最後に採用した round）の監査の目的との合い方（#2402）。

    監査の無い round（時間の上限で監査を切った round など）・記録の無い回答は None。
    """
    if isinstance(generation_trace, Mapping) and semantic_audit_is_diagnostic_only(
        generation_trace
    ):
        return None
    rounds = generation_trace.get("rounds") if isinstance(generation_trace, Mapping) else None
    adopted: Mapping[str, Any] | None = None
    for item in rounds if isinstance(rounds, list) else ():
        if isinstance(item, Mapping) and item.get("accepted"):
            adopted = item
    audit = adopted.get("audit") if adopted is not None else None
    value = audit.get("goal_alignment") if isinstance(audit, Mapping) else None
    return value if isinstance(value, str) and value else None


def guide_step_plan(guide: Mapping[str, Any] | None) -> Any:
    """回答に使う業務ガイドの手順(#2395。手順ごとの検索の材料)。答えるときだけ。無ければ None。"""
    from rag_engine.retrieval.guide_steps import GuidePlan

    guide = guide or {}
    if guide.get("decision") not in {"answer", "branch"} or not guide.get("step_plan"):
        return None
    steps = guide.get("step_plan")
    notices = guide.get("notices")
    return GuidePlan.from_payload(
        str(guide.get("guide_id") or ""),
        int(cast(Any, guide.get("revision") or 0)),
        steps if isinstance(steps, list) else [],
        title=str(guide.get("title") or ""),
        notices=notices if isinstance(notices, Mapping) else None,
    )


def _with_page_ranges(
    filters: Mapping[str, str], page_ranges: Sequence[Mapping[str, Any]]
) -> dict[str, str] | None:
    """検索の絞り込みに頁の範囲(#2395)を足した写し。重なる範囲が無ければ None(検索しない)。

    絞り込みにすでに頁の範囲(利用者の確認の範囲)があれば、両方に重なる頁だけにする。
    """
    wanted = [PageRange.model_validate(dict(item)) for item in page_ranges]
    current = filters.get(PAGE_RANGES_FILTER_KEY)
    if current:
        wanted = [
            overlap
            for item in wanted
            for scope in parse_page_ranges(current)
            if (overlap := _page_overlap(item, scope)) is not None
        ][:MAX_PAGE_RANGES]
    if not wanted:
        return None
    return {**filters, PAGE_RANGES_FILTER_KEY: format_page_ranges(wanted[:MAX_PAGE_RANGES])}


def _page_overlap(left: PageRange, right: PageRange) -> PageRange | None:
    if left.document_id != right.document_id:
        return None
    if left.sheet_name is not None or right.sheet_name is not None:
        return _sheet_overlap(left, right)
    starts = [value for value in (left.page_start, right.page_start) if value is not None]
    ends = [value for value in (left.page_end, right.page_end) if value is not None]
    start = max(starts) if starts else None
    end = min(ends) if ends else None
    if start is not None and end is not None and start > end:
        return None
    return PageRange(document_id=left.document_id, page_start=start, page_end=end)


def _whole_document(item: PageRange) -> bool:
    return item.sheet_name is None and item.page_start is None and item.page_end is None


def _sheet_overlap(left: PageRange, right: PageRange) -> PageRange | None:
    """シートの行の範囲(#2663)の重なり。文書の全体とはそのまま、頁の範囲・別のシートとは重ならない。"""
    if _whole_document(left) or _whole_document(right):
        return right if _whole_document(left) else left
    if left.sheet_name != right.sheet_name:
        return None
    starts = [value for value in (left.row_start, right.row_start) if value is not None]
    ends = [value for value in (left.row_end, right.row_end) if value is not None]
    start = max(starts) if starts else None
    end = min(ends) if ends else None
    if start is not None and end is not None and start > end:
        return None
    return PageRange(
        document_id=left.document_id, sheet_name=left.sheet_name, row_start=start, row_end=end
    )


def _zero_evidence_diagnostics(
    result: Any, state: _SearchState, request: SearchRequest
) -> dict[str, Any]:
    """根拠 0 件で回答を作らずに終えたときの、0 件になった段と検索の記録(#2401)。

    段は rag_engine が決め(検索の候補が無い・根拠確認で全部外した・根拠の組み立て)、検索の候補が
    無く、自動で足した検索条件が効いたままだった(外す段の上限・設定で外さなかった)ときは ``filter``
    にする。検索の条件は値を出さず、項目の名前だけを残す(値は質問・文書の内容を含みうる)。
    """
    from rag_engine.generation.zero_evidence import ZERO_EVIDENCE_STAGE_LABELS

    trace = getattr(result, "generation_trace", None)
    engine = trace.get("zero_evidence") if isinstance(trace, Mapping) else None
    if not isinstance(engine, Mapping):
        return {}
    active = state.active_relaxations()
    stage = str(engine.get("stage") or "")
    if stage == "retrieval" and active:
        stage = "filter"
    return {
        **dict(engine),
        "stage": stage,
        "label": ZERO_EVIDENCE_STAGE_LABELS.get(stage, str(engine.get("label") or "")),
        # 利用者が指定した検索条件の項目(範囲。外さない)。
        "request_filter_keys": sorted(
            key for key, value in request.filters.items() if value.strip()
        ),
        # 最後の検索で効いていた自動の条件と、候補 0 件で外した自動の条件。
        "automatic_filters": active,
        "relaxed": list(state.relaxed),
        "searches": list(state.search_log),
        # 失敗して空のまま続けた補助の処理(#3259)。0 件が「当たらなかった」のか
        # 「読めなかった」のかを分ける(処理の種類・回数・例外の型の名前だけ)。
        "degraded_steps": state.degraded_steps(),
    }


def _graph_expansion_diagnostics(
    expansions: Mapping[str, Mapping[str, object]],
    *,
    max_chunks: int,
    attempts: Sequence[Mapping[str, object]] = (),
) -> dict[str, Any]:
    """関係情報で足した chunk の記録(#2055)。使った entity(名前・理由・足した数)と足した chunk。"""
    entities: dict[str, dict[str, Any]] = {}
    for item in expansions.values():
        entity_id = str(item.get("entity_id") or "")
        entry = entities.setdefault(
            entity_id,
            {
                "entity_id": entity_id,
                "entity": str(item.get("entity") or ""),
                "kind": str(item.get("kind") or ""),
                "document_id": str(item.get("document_id") or ""),
                "added_count": 0,
            },
        )
        entry["added_count"] += 1
        if item.get("match"):
            entry["match"] = str(item["match"])

    def unique_count(key: str) -> int:
        values: set[str] = set()
        for attempt in attempts:
            items = attempt.get(key)
            if isinstance(items, list):
                values.update(str(item) for item in items)
        return len(values)

    return {
        "status": "error"
        if any(item.get("status") == "error" for item in attempts)
        else "completed"
        if attempts
        else "not_run",
        "graph_available": any(item.get("graph_available") for item in attempts),
        "seed_entity_count": unique_count("seed_entity_ids"),
        "name_entity_count": unique_count("name_entity_ids"),
        "planned_count": unique_count("planned_chunk_ids"),
        "skipped_existing_count": unique_count("skipped_existing_chunk_ids"),
        "added_count": len(expansions),
        "max_chunks": max_chunks,
        "entity_count": len(entities),
        "entities": list(entities.values())[:_GRAPH_DIAGNOSTICS_ENTITIES],
        "chunks": list(expansions.values())[:_REFERENCE_DIAGNOSTICS_LIMIT],
    }


def _section_expansion_diagnostics(trace: Any) -> dict[str, Any]:
    """章節の拡張の記録(#1436)。評価器の判定の全文(grades)は除き、段ごとの判定だけを残す。"""
    raw = (trace or {}).get("section_expansion") if isinstance(trace, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    return {key: value for key, value in raw.items() if key != "grades"}


# 本文の出典行（「根拠：」）の順番（1 始まり）を、出典行が指す根拠の chunk の metadata に
# 入れる key（#1330）。画面は n 番目の出典行を、この list に n を持つ根拠に結ぶ
# （ファイル名と頁で推測しない）。番号を付けるのは、出典行の主張を支える子と確定した
# chunk だけ（#2710）。
ANSWER_CITATION_LINES_KEY = "answer_citation_lines"
# 出典行の主張を支える子が決まらなかったときの、候補の chunk の出典行の番号（#2710）。
# 画面はどれか 1 つを正しい位置として強調せず、「位置未確定」として候補を示す。
ANSWER_CITATION_CANDIDATE_LINES_KEY = "answer_citation_candidate_lines"
# 出典行の番号をどう決めたか（#2710）。``child`` は主張を支える子と確定した番号。この印の無い番号は
# 以前の回答の記録（同じ親・頁の子から並びで選んだ番号）で、画面は位置を確定したものとして扱わない。
ANSWER_CITATION_PROVENANCE_KEY = "answer_citation_provenance"
ANSWER_CITATION_PROVENANCE_CHILD = "child"
# 回答が親（まとまり）だけを参照した子の印（#2710）。
# 「回答で引用」（evidence_model_used）とは分ける。
EVIDENCE_PARENT_USED_KEY = "evidence_parent_used"


def _with_citation_lines(
    citations: list[RetrievedChunk], evidence_items: Any, refs: Any
) -> list[RetrievedChunk]:
    """出典行ごとの根拠（rag_engine の ``citation_lines``）を、引用の chunk に結ぶ（#1330）。

    出典行の根拠は親の範囲（``source_id`` / ``source_ids``）で、引用は子の chunk。
    出典行の主張を支える子が決まっていれば（``scope_source_ids``）その子に番号を付ける。
    決まっていなければ、どの子も確定の位置にしない（引用一覧の並び・回答に使った印・頁の
    一致で 1 つを選ばない。#2710）。候補（rag_engine の ``candidate_source_ids``。無ければ
    その親の子のうち出典行の頁に重なるもの）に候補の番号を付ける。
    """
    if not isinstance(refs, list) or not refs or not citations:
        return citations
    pages = {chunk.chunk_id: _chunk_page_range(chunk) for chunk in citations}
    children_by_parent: dict[str, list[str]] = {}
    for parent in evidence_items:
        if not isinstance(parent, Mapping):
            continue
        children = [
            str(child.get("chunk_id") or child.get("id") or "")
            for child in parent.get("children") or []
            if isinstance(child, Mapping)
        ]
        for key in ("chunk_uid", "id", "chunk_id"):
            if value := str(parent.get(key) or ""):
                children_by_parent.setdefault(value, children)
    lines: dict[str, list[int]] = {}
    candidate_lines: dict[str, list[int]] = {}
    for ordinal, ref in enumerate(refs, 1):
        if not isinstance(ref, Mapping):
            continue
        exact = _known_ids(ref.get("scope_source_ids"), pages)
        for chunk_id in exact:
            lines.setdefault(chunk_id, []).append(ordinal)
        candidates = [
            chunk_id
            for chunk_id in _known_ids(ref.get("candidate_source_ids"), pages)
            if chunk_id not in exact
        ]
        if not exact and not candidates:
            candidates = _page_candidates(ref, children_by_parent, pages)
        for chunk_id in candidates:
            candidate_lines.setdefault(chunk_id, []).append(ordinal)
    if not lines and not candidate_lines:
        return citations
    result: list[RetrievedChunk] = []
    for chunk in citations:
        update: dict[str, Any] = {}
        if chunk.chunk_id in lines:
            update[ANSWER_CITATION_LINES_KEY] = lines[chunk.chunk_id]
            update[ANSWER_CITATION_PROVENANCE_KEY] = ANSWER_CITATION_PROVENANCE_CHILD
        if chunk.chunk_id in candidate_lines:
            update[ANSWER_CITATION_CANDIDATE_LINES_KEY] = candidate_lines[chunk.chunk_id]
        result.append(
            chunk.model_copy(update={"metadata": {**chunk.metadata, **update}}) if update else chunk
        )
    return result


def _known_ids(values: Any, pages: Mapping[str, tuple[int, int]]) -> list[str]:
    """引用にある chunk の ID だけを、重ねずに順に。"""
    if not isinstance(values, list | tuple):
        return []
    return list(dict.fromkeys(str(item) for item in values if str(item) in pages))


def _page_candidates(
    ref: Mapping[str, Any],
    children_by_parent: Mapping[str, list[str]],
    pages: Mapping[str, tuple[int, int]],
) -> list[str]:
    """子の決まらない出典行の候補。

    出典行の親の子のうち、出典行の頁に重なるもの（無ければ親の子すべて）。
    """
    sources = ref.get("source_ids")
    parents = [str(item) for item in sources if item] if isinstance(sources, list) else []
    parents = parents or [str(ref.get("source_id") or "")]
    children = list(
        dict.fromkeys(
            chunk_id
            for parent in parents
            for chunk_id in children_by_parent.get(parent, [])
            if chunk_id in pages
        )
    )
    start = _int(ref.get("page"))
    if not start:
        return children
    end = max(start, _int(ref.get("page_end"), start))
    on_page = [
        chunk_id
        for chunk_id in children
        if pages[chunk_id][0] <= end and start <= pages[chunk_id][1]
    ]
    return on_page or children


def _chunk_page_range(chunk: RetrievedChunk) -> tuple[int, int]:
    start = _int(chunk.metadata.get("page_start") or chunk.metadata.get("page_number"), 1)
    return start, max(start, _int(chunk.metadata.get("page_end"), start))


def _evaluation_input(result: Any) -> dict[str, Any] | None:
    """rag_poc の回答 payload から、標準回答での評価に使う部分だけを取り出す。"""
    from rag_engine.generation.answer_payload import answer_result_payload

    try:
        payload = answer_result_payload(result, answer_id="", run_id=ENGINE_SOURCE_RUN_ID)
    except Exception:  # noqa: BLE001 - 評価の入力は補助。回答の返却を止めない。
        logger.warning("evaluation input build failed", exc_info=True)
        return None
    # 根拠は評価が読む項目だけにする（回答の記録にもこの形で保存する。#1371）。
    return stored_evaluation_input({key: payload.get(key) for key in EVALUATION_INPUT_KEYS})


def answer_model_of(diagnostics: Mapping[str, Any] | None) -> str:
    """回答の診断(``models.llm.model_id``)から、回答を作ったモデルの ID。無ければ空。"""
    models = (diagnostics or {}).get("models")
    llm = models.get("llm") if isinstance(models, Mapping) else None
    model_id = llm.get("model_id") if isinstance(llm, Mapping) else None
    return model_id.strip() if isinstance(model_id, str) else ""


def evaluate_answer_record(
    evaluation_input: Mapping[str, Any],
    standard_answer: str,
    settings: Settings,
    *,
    citations: Sequence[RetrievedChunk],
    judge_model_id: str | None = None,
) -> dict[str, Any]:
    """保存した回答を標準回答で評価する。

    LLM(rag_engine の evaluate_answer_payload)が標準回答の項目の照合と主張の監査を行い、
    評価の基準の指標と閾値で合否を付ける(#680)。``citations`` は回答の引用(決定的な指標に使う)。
    同期関数で、LLM を複数回呼ぶ。呼び出し側は worker thread で動かす。

    ``judge_model_id`` は判定のモデル(#3574。呼び出し側が回答を作ったモデルから
    `enterprise_ai_judge_model_id` で解決した ID)。省略時は回答のモデルが分からないものとして
    解決する。
    判定の LLM はそのモデルの接続・入力の上限・推論の量で呼び(回答のモデルの切り替えと同じ
    ``build_engine_settings``)、使ったモデルを ``judge_diagnostics.judge_model_id`` に残す。
    """
    from rag_engine.evaluation.answer_eval import evaluate_answer_payload

    judge_model = (judge_model_id or "").strip() or enterprise_ai_judge_model_id(settings)
    with tempfile.TemporaryDirectory(prefix="rag-engine-eval-") as work:
        engine_settings = build_engine_settings(
            settings, output_dir=Path(work), answer_model_id=judge_model
        )
        evaluation = evaluate_answer_payload(
            {
                **evaluation_input,
                "standard_answer": standard_answer,
                "allow_without_standard": True,
                "six_metrics": True,
            },
            engine_settings,
        )
    diagnostics = evaluation.get("judge_diagnostics")
    evaluation = {
        **evaluation,
        "judge_diagnostics": {
            **(diagnostics if isinstance(diagnostics, Mapping) else {}),
            "judge_model_id": judge_model,
        },
    }
    return score_answer_evaluation(
        evaluation,
        answer=str(evaluation_input.get("answer_text") or ""),
        citations=citations,
        insufficient_reason=str(evaluation_input.get("insufficient_reason") or ""),
        settings=settings,
    )
