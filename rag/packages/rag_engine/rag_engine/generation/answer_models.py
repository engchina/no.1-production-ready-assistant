"""回答フローで受け渡すデータ型と、設定値・表示名の正規化。I/O は行わない。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from rag_engine.chunking import DEFAULT_RETRIEVAL_TOP_K
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext
from rag_engine.retrieval.context_builder import ContextEvidence, ContextParentEvidence
from rag_engine.retrieval.inquiry_conditions import InquiryConditionParse
from rag_engine.retrieval.question_planning import QuestionPlan
from rag_engine.retrieval.scope import RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN, RETRIEVAL_SCOPE_KNOWLEDGE_BASE, normalize_retrieval_scope
from rag_engine.retrieval.task_contract import goal_retrieval_queries

# 回答で引用した箇所の使われ方（#2040）。confirmed は「資料により確認できた事項」・手順の節の説明（質問の条件への該当を
# 監査で確認した。原文の条件つきを含む）で使用した箇所、reference は「参考情報」の節（降格した原文・該当が
# 未確認の説明）と「資料から確認できなかった事項」の説明で使用した箇所。画面の件数とバッジ、MCP の evidence が使う。
ANSWER_USE_CONFIRMED = "confirmed"
ANSWER_USE_REFERENCE = "reference"
ANSWER_USES = (ANSWER_USE_CONFIRMED, ANSWER_USE_REFERENCE)


def merge_answer_use(current: str | None, new: str | None) -> str | None:
    """同じ箇所を複数の item が使ったときの使われ方。1 つでも確かめた説明で使えば confirmed。"""
    if ANSWER_USE_CONFIRMED in (current, new):
        return ANSWER_USE_CONFIRMED
    return current or new


QUESTION_DISPLAY_METADATA_SEPARATOR = "--- 回答生成の実行記録 ---"

ANSWER_FLOW_DISPLAY_SEPARATOR = "--- 回答生成フロー ---"

QUERY_STRATEGY_DISPLAY_SEPARATOR = "--- 質問拡張戦略 ---"

QUERY_EXPANSION_SEPARATOR = "--- 生成された検索質問 / 検索文 ---"

QUESTION_TEXT_SEARCH_SEPARATOR = "--- Oracle Text 検索語 ---"

CRAG_RETRIEVAL_DISPLAY_SEPARATOR = "--- CRAG 検索補正 ---"

QUERY_UNDERSTANDING_SEPARATOR = "--- 細粒度クエリ理解（Intent Classification / Slot Filling） ---"

QUESTION_PLAN_SEPARATOR = "--- 質問処理計画 ---"

# 旧形式の質問の表示から元の質問を取り出すための区切り（今の表示には書かない）。旧形式の記録と一致させるため
# 文字列は変えない（表示の名前は「用語・同義語」「回答ルール」。#3490）。
RUNTIME_KNOWLEDGE_SEPARATOR = "--- 実行時用語・ルール ---"

INQUIRY_CONDITIONS_SEPARATOR = QUERY_UNDERSTANDING_SEPARATOR

LEGACY_QUESTION_METADATA_SEPARATORS = (
    "--- 回答生成メモ ---",
    "--- Oracle Text keyword tokens（原質問） ---",
    "--- CRAG retrieval correction ---",
    "--- Question Plan ---",
    "--- Runtime Glossary / Rules ---",
    "--- Inquiry Conditions ---",
    "--- 問い合わせ条件 ---",
)

AUTO_ROUTING_STRATEGY = "auto_routing"

SIMPLE_RETRIEVAL_STRATEGY = "simple_retrieval"

RAG_FUSION_STRATEGY = "rag_fusion"

QUERY_DECOMPOSITION_STRATEGY = "query_decomposition"

STEP_BACK_PROMPTING_STRATEGY = "step_back_prompting"

HYDE_STRATEGY = "hyde"

AUTO_ROUTING_LABEL = "自動ルーティング（自動選択）"

SIMPLE_RETRIEVAL_LABEL = "単純検索（拡張なし）"

RAG_FUSION_LABEL = "RAGフュージョン（複数検索質問 + 相互順位融合）"

QUERY_DECOMPOSITION_LABEL = "質問分解"

STEP_BACK_PROMPTING_LABEL = "ステップバックプロンプト"

HYDE_LABEL = "仮説文生成（HyDE）"

STANDARD_ANSWER_FLOW = "standard_rag"

CRAG_ANSWER_FLOW = "crag"

STANDARD_ANSWER_FLOW_LABEL = "通常RAG（検索して回答生成 / 補正なし）"

# CRAG が検索（初回 + 補正検索）を行う最大回数。UI の表示ラベルもこの値から作る。
CRAG_MAX_RETRIEVAL_ATTEMPTS = 3

CRAG_ANSWER_FLOW_LABEL = f"補正RAG（CRAG：検索結果を評価し、必要なら補正検索 / 最大{CRAG_MAX_RETRIEVAL_ATTEMPTS}回）"

ANSWER_FLOWS = (
    (STANDARD_ANSWER_FLOW, STANDARD_ANSWER_FLOW_LABEL),
    (CRAG_ANSWER_FLOW, CRAG_ANSWER_FLOW_LABEL),
)

LEGACY_ANSWER_FLOW_LABELS = (
    (STANDARD_ANSWER_FLOW, "Retrieve-then-Generate（通常RAG：検索して回答生成 / 補正なし）"),
    (CRAG_ANSWER_FLOW, "Corrective RAG（CRAG：検索結果を評価し、必要なら補正検索 / 最大3回）"),
)

# 既定は標準 RAG。CRAG は根拠の評価の LLM 呼び出しと補正検索が増えるので、明示して選んだときだけ使う（#3509）。
DEFAULT_ANSWER_FLOW = STANDARD_ANSWER_FLOW

DEFAULT_ANSWER_FLOW_LABEL = STANDARD_ANSWER_FLOW_LABEL

_ANSWER_FLOW_LABEL_BY_ID = dict(ANSWER_FLOWS)

_ANSWER_FLOW_ID_BY_LABEL = {label: flow_id for flow_id, label in ANSWER_FLOWS}

_ANSWER_FLOW_ID_BY_LEGACY_LABEL = {label: flow_id for flow_id, label in LEGACY_ANSWER_FLOW_LABELS}

# 初期 CRAG 実装を呼び出していた既存コード向けの後方互換 alias。
CRAG_STRATEGY = CRAG_ANSWER_FLOW

CRAG_LABEL = CRAG_ANSWER_FLOW_LABEL

QUERY_STRATEGIES = (
    (AUTO_ROUTING_STRATEGY, AUTO_ROUTING_LABEL),
    (SIMPLE_RETRIEVAL_STRATEGY, SIMPLE_RETRIEVAL_LABEL),
    (RAG_FUSION_STRATEGY, RAG_FUSION_LABEL),
    (QUERY_DECOMPOSITION_STRATEGY, QUERY_DECOMPOSITION_LABEL),
    (STEP_BACK_PROMPTING_STRATEGY, STEP_BACK_PROMPTING_LABEL),
    (HYDE_STRATEGY, HYDE_LABEL),
)

LEGACY_QUERY_STRATEGY_LABELS = (
    (AUTO_ROUTING_STRATEGY, "Auto Routing（自動選択）"),
    (SIMPLE_RETRIEVAL_STRATEGY, "Simple Retrieval（拡張なし）"),
    (RAG_FUSION_STRATEGY, "RAG-Fusion (Multi-Query Retrieval + Reciprocal Rank Fusion)"),
    (QUERY_DECOMPOSITION_STRATEGY, "Query Decomposition"),
    (STEP_BACK_PROMPTING_STRATEGY, "Step-Back Prompting"),
    (HYDE_STRATEGY, "Hypothetical Document Embeddings (HyDE)"),
)

DEFAULT_QUERY_STRATEGY = AUTO_ROUTING_STRATEGY

DEFAULT_QUERY_STRATEGY_LABEL = AUTO_ROUTING_LABEL

_QUERY_STRATEGY_LABEL_BY_ID = dict(QUERY_STRATEGIES)

_QUERY_STRATEGY_ID_BY_LABEL = {label: strategy_id for strategy_id, label in QUERY_STRATEGIES}

_QUERY_STRATEGY_ID_BY_LEGACY_LABEL = {label: strategy_id for strategy_id, label in LEGACY_QUERY_STRATEGY_LABELS}


@dataclass(frozen=True)
class AnswerRecord:
    """回答生成 context に投入する解析レコードまたはチャンクを表します。"""

    id: str
    engine: str
    engine_label: str
    page: int
    seq_no: int
    category: str
    text: str
    source: str = ""
    source_run_id: str = ""
    chunk_id: str = ""
    chunk_uid: str = ""
    chunk_level: str = "record"
    chunk_seq: int = 0
    parent_chunk_id: str = ""
    parent_chunk_uid: str = ""
    child_chunk_ids: tuple[str, ...] = ()
    page_end: int | None = None
    source_seq_ranges: tuple[dict[str, int], ...] = field(default_factory=tuple)
    source_record_refs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    # 検索用 metadata を含まない元本文。空の場合は従来の record.text を使用する。
    body_text: str = ""

    @property
    def citation(self) -> str:
        """回答根拠欄に表示する短い引用文字列を返します。"""
        if self.chunk_id:
            seq_ranges = _format_seq_ranges(self.source_seq_ranges)
            pages = _page_span(self.page, self.page_end or self.page)
            parent = f" / {self.parent_chunk_id}" if self.parent_chunk_id else ""
            return f"{self.chunk_id}{parent} / {pages} / {seq_ranges}"
        return f"p.{self.page} #{self.seq_no}"


@dataclass(frozen=True)
class AnswerContext:
    """回答に使う根拠レコードと検索 trace を保持します。

    `text` は `ContextBundle.text` と同じ可読 trace で、回答 prompt には渡さない（prompt は
    `evidence_spans` が `records` から切り出す）。空判定・評価ツール・SDK が読む。
    """

    records: list[AnswerRecord]
    text: str
    evidence: tuple[ContextEvidence, ...] = ()
    evidence_tree: tuple[ContextParentEvidence, ...] = ()
    status: str = "ready"
    insufficient_reason: str = ""
    preferred_child_ids: tuple[str, ...] = ()
    # 評価後の局所拡張用。生成 prompt へは選択済み根拠だけを渡す。
    expansion_records: tuple[AnswerRecord, ...] = ()
    # 文書の選択 (#1028): rerank 後の全候補 child（順位順）、後回しにした文書の child、選択の trace。
    # 不足時はこれらから後回しの文書を戻して再評価・再生成する。
    ranked_candidates: tuple[AnswerRecord, ...] = ()
    deferred_records: tuple[AnswerRecord, ...] = ()
    document_selection: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnswerResponse:
    """LLM から得た回答本文と参照レコードを保持します。"""

    answer_text: str
    confidence: str = ""
    question_type: tuple[str, ...] = ()
    cited_evidence: tuple[dict[str, Any], ...] = ()
    reasoning_summary: str = ""
    insufficient_reason: str = ""
    needs_human_review: bool | None = None
    external_data_required: bool | None = None
    external_data_items: tuple[str, ...] = ()
    raw_text: str = ""
    evidence_facts: tuple[dict[str, str], ...] = ()
    generation_trace: dict[str, Any] = field(default_factory=dict)
    # 回答の構造（AnswerEnvelope。#1235）。grounded の確定の段で組み立てる。無い経路は結果で補う。
    envelope: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryExpansionResult:
    """検索 query、生成元 strategy、検索前の暫定データ確認観点を保持します。"""

    original_question: str
    selected_strategy: str
    effective_strategy: str
    generated_queries: tuple[str, ...] = ()
    routing_reason: str = ""
    routing_data_required: bool | None = None
    routing_data_items: tuple[str, ...] = ()
    rejected_queries: tuple[dict[str, str], ...] = ()
    # 規定（法令・規程の条文）の書き方で根拠を言い表した検索文。vector だけに使う (#2148)。
    provision_queries: tuple[str, ...] = ()

    @property
    def retrieval_queries(self) -> tuple[str, ...]:
        """検索へ投入する query を重複なく返します。"""
        return _dedupe_queries(
            (self.original_question, *goal_retrieval_queries(self.original_question), *self.generated_queries, *self.provision_queries)
        )

    @property
    def vector_only_queries(self) -> tuple[str, ...]:
        """意味検索（vector）だけに使い、全文検索には使わない検索文を返します。

        HyDE の仮説文は文書にありそうな文を創作したもので、含まれる語は事実でなくてよい。
        創作した語をキーワード検索の条件にすると無関係な chunk を拾うため、embedding だけに使う (#911)。
        """
        hypotheses = self.generated_queries if self.effective_strategy == HYDE_STRATEGY else ()
        return (*hypotheses, *self.provision_queries)


@dataclass(frozen=True)
class QuestionTextSearchInfo:
    """Oracle Text 検索に使う質問文字列、token、query を保持します。"""

    tokenizer: str = ""
    tokenizer_label: str = ""
    tokenizer_fingerprint: str = ""
    tokens: tuple[str, ...] = ()
    oracle_text_query: str = ""
    oracle_text_queries: tuple[str, ...] = ()
    query_variants: tuple[str, ...] = ()
    query_source: str = "原質問のみ"
    tokenization_traces: tuple[dict[str, Any], ...] = ()
    domain_keyword_expansion: dict[str, Any] = field(default_factory=dict)
    target_text_queries: tuple[str, ...] = ()  # 対象語の部分語の Oracle Text フレーズ検索式 (#730)
    error: str = ""


@dataclass(frozen=True)
class RetrievalQueryPlan:
    """複数 query の生成元と検索投入時の役割を保持します。"""

    original_question: str
    runtime_expanded_question: str
    vector_queries: tuple[str, ...] = ()
    text_query_variants: tuple[str, ...] = ()
    oracle_text_queries: tuple[str, ...] = ()
    text_search_tokens: tuple[str, ...] = ()
    text_search_tokenization_traces: tuple[dict[str, Any], ...] = ()
    llm_expansion_queries: tuple[str, ...] = ()
    lexical_queries: tuple[str, ...] = ()
    target_text_queries: tuple[str, ...] = ()  # 対象語の部分語の Oracle Text フレーズ検索式 (#730)
    crag_rewrites: tuple[str, ...] = ()
    retrieval_scope: str = RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN
    top_k: int = DEFAULT_RETRIEVAL_TOP_K
    candidate_limit: int = 0  # 実行時に `_retrieval_candidate_limit(top_k)` を入れる。trace 表示用
    metadata_filter: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {
            "original_question": self.original_question,
            "runtime_expanded_question": self.runtime_expanded_question,
            "vector_queries": list(self.vector_queries),
            "text_query_variants": list(self.text_query_variants),
            "oracle_text_queries": list(self.oracle_text_queries),
            "text_search_tokens": list(self.text_search_tokens),
            "text_search_tokenization_traces": list(self.text_search_tokenization_traces),
            "llm_expansion_queries": list(self.llm_expansion_queries),
            "lexical_queries": list(self.lexical_queries),
            "target_text_queries": list(self.target_text_queries),
            "crag_rewrites": list(self.crag_rewrites),
            "retrieval_scope": self.retrieval_scope,
            "top_k": self.top_k,
            "candidate_limit": self.candidate_limit,
            "metadata_filter": dict(self.metadata_filter),
        }


@dataclass(frozen=True)
class CragRetrievalAttempt:
    """CRAG の各 retrieval/rewrite attempt の判定結果を保持します。"""

    attempt: int
    query: str
    retrieval_queries: tuple[str, ...]
    sufficient: bool
    confidence: float | None = None
    relevant_chunk_ids: tuple[str, ...] = ()
    rewritten_query: str = ""
    reason: str = ""
    grade_error: str = ""
    grade_failure: dict[str, Any] = field(default_factory=dict)
    aspect_checks: tuple[dict[str, Any], ...] = ()
    # 候補ごとの関連判定（chunk_uid / relevant / reason）。trace と UI で「なぜ不足か」を候補単位で追える (#983)。
    candidate_verdicts: tuple[dict[str, Any], ...] = ()
    # 評価器が relevant=false と明示した候補（親 uid と、その検索命中子の uid）。生成用 context と回復用 pool から
    # 除外する (#1010)。未評価の候補は含めない（relevant_chunk_ids の補集合ではない）。
    rejected_chunk_ids: tuple[str, ...] = ()
    # 明示的に missing と判定した必須観点。未検証は unknown_aspects と分け、回復と生成へ申し送る。
    missing_aspects: tuple[str, ...] = ()
    new_evidence_count: int = 0
    stop_reason: str = ""
    executed_queries: tuple[str, ...] = ()
    recovery_action: str = "rewrite"
    recovery_anchor_ids: tuple[str, ...] = ()
    recovery_trace: dict[str, Any] = field(default_factory=dict)
    # 文書の選択の trace（selected / deferred / restored。#1028）。選択が無効なら enabled=false だけ。
    document_selection: dict[str, Any] = field(default_factory=dict)

    generation_usable: bool | None = None
    answerable_parts: tuple[dict[str, Any], ...] = ()
    context_fingerprint: str = ""
    required_aspect_coverage: str = "unknown"
    unknown_aspects: tuple[str, ...] = ()
    grade_execution: str = "not_run"
    rule_signals: dict[str, Any] = field(default_factory=dict)
    # 回復を選んだ原因（``crag_recovery_cause`` の値。#3755）。確かめた不足だけに書き換え検索・追加の検索の枠を使う。
    recovery_cause: str = ""

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        payload = {
            "attempt": self.attempt,
            "query": self.query,
            "retrieval_queries": list(self.retrieval_queries),
            "executed_queries": list(self.executed_queries),
            "sufficient": self.sufficient,
            "generation_usable": self.sufficient if self.generation_usable is None else self.generation_usable,
            "answerable_parts": list(self.answerable_parts),
            "context_fingerprint": self.context_fingerprint,
            "required_aspect_coverage": self.required_aspect_coverage,
            "unknown_aspects": list(self.unknown_aspects),
            "grade_execution": "error" if self.grade_error else self.grade_execution,
            "rule_signals": self.rule_signals,
            "confidence": self.confidence,
            "relevant_chunk_ids": list(self.relevant_chunk_ids),
            "rewritten_query": self.rewritten_query,
            "reason": self.reason,
            "aspect_checks": list(self.aspect_checks),
            "candidate_verdicts": list(self.candidate_verdicts),
            "rejected_chunk_ids": list(self.rejected_chunk_ids),
            "missing_aspects": list(self.missing_aspects),
            "new_evidence_count": self.new_evidence_count,
            "stop_reason": self.stop_reason,
            "recovery_action": self.recovery_action,
            "recovery_anchor_ids": list(self.recovery_anchor_ids),
            "recovery_trace": self.recovery_trace,
            "recovery_cause": self.recovery_cause,
            "document_selection": self.document_selection,
        }
        if self.grade_error:
            payload["grade_error"] = self.grade_error
        if self.grade_failure:
            payload["grade_failure"] = dict(self.grade_failure)
        return payload


# CRAG の回復の原因（#3755）。評価の結果の種類で回復を分ける（Corrective RAG の correct / ambiguous / incorrect）。
CRAG_CAUSE_SUFFICIENT = "sufficient"  # 生成に使え、必須の観点に不足・未確認が無い
CRAG_CAUSE_VERIFIED_SHORTFALL = "verified_shortfall"  # 評価器が不足と判定した観点がある（missing）
CRAG_CAUSE_UNVERIFIED = "unverified"  # 不足と確かめていない未確認の観点だけ（写しの照合の失敗・unknown・判定の欠け）
CRAG_CAUSE_NO_RELEVANT = "no_relevant_evidence"  # 生成に使えず、関係ありと判定した候補が無い
CRAG_CAUSE_GENERATION_UNUSABLE = "generation_unusable"  # 関係する候補はあるが、生成に使える根拠を確かめられない
CRAG_CAUSE_GRADE_ERROR = "grade_error"  # 評価の実行に失敗した
CRAG_CAUSE_GRADE_SKIPPED = "grade_skipped"  # 規則の信号で評価を飛ばした
CRAG_CAUSE_NO_NEW_EVIDENCE = "no_new_evidence"  # 探し直しで新しい本文が得られなかった


def crag_recovery_cause(attempt: CragRetrievalAttempt) -> str:
    """評価の結果から回復の原因を決める（#3755）。

    ``missing``（確かめた不足）を ``unknown``（未確認）より先に見る。未確認だけを不足に変えない。
    """
    if attempt.grade_error or attempt.grade_execution == "error":
        return CRAG_CAUSE_GRADE_ERROR
    if attempt.missing_aspects:
        return CRAG_CAUSE_VERIFIED_SHORTFALL
    if attempt.sufficient:
        return CRAG_CAUSE_UNVERIFIED if attempt.unknown_aspects else CRAG_CAUSE_SUFFICIENT
    if not attempt.relevant_chunk_ids:
        return CRAG_CAUSE_NO_RELEVANT
    if attempt.unknown_aspects or attempt.grade_execution == "partial":
        return CRAG_CAUSE_UNVERIFIED
    return CRAG_CAUSE_GENERATION_UNUSABLE


@dataclass(frozen=True)
class AnswerQuestionResult:
    """回答生成全体の trace、根拠、出力 payload をまとめます。"""

    answer: str
    question_display: str
    original_question: str
    selected_strategy: str
    effective_strategy: str
    answer_flow: str = DEFAULT_ANSWER_FLOW
    retrieval_scope: str = RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN
    classification_filter: dict[str, str] = field(default_factory=dict)
    generated_queries: tuple[str, ...] = ()
    routing_reason: str = ""
    answer_text: str = ""
    confidence: str = ""
    question_type: tuple[str, ...] = ()
    cited_evidence_ids: tuple[str, ...] = ()
    cited_evidence: tuple[dict[str, Any], ...] = ()
    image_evidence: tuple[dict[str, Any], ...] = ()
    image_prompt_mode: str = "text_only"
    primary_source_run_id: str = ""
    reasoning_summary: str = ""
    insufficient_reason: str = ""
    needs_human_review: bool | None = None
    external_data_required: bool | None = None
    external_data_items: tuple[str, ...] = ()
    evidence_items: tuple[dict[str, Any], ...] = ()
    text_search_tokenizer: str = ""
    text_search_tokenizer_label: str = ""
    text_search_tokenizer_fingerprint: str = ""
    text_search_tokens: tuple[str, ...] = ()
    text_search_query: str = ""
    text_search_queries: tuple[str, ...] = ()
    text_search_query_source: str = ""
    text_search_tokenization_error: str = ""
    crag_attempts: tuple[dict[str, Any], ...] = ()
    prompt_injection_risk: bool = False
    prompt_injection_warnings: tuple[str, ...] = ()
    rerank_scores: tuple[dict[str, Any], ...] = ()
    retrieval_query_plan: dict[str, Any] = field(default_factory=dict)
    execution_steps: tuple[dict[str, Any], ...] = ()
    question_plan: QuestionPlan | None = None
    runtime_knowledge: RuntimeKnowledgeContext | None = None
    inquiry_conditions: InquiryConditionParse | None = None
    routing_data_required: bool | None = None
    routing_data_items: tuple[str, ...] = ()
    evidence_facts: tuple[dict[str, str], ...] = ()
    generation_trace: dict[str, Any] = field(default_factory=dict)
    task_contract: dict[str, Any] = field(default_factory=dict)
    rejected_queries: tuple[dict[str, str], ...] = ()
    # 回答の構造（AnswerEnvelope。#1235）。
    envelope: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GroundedAnswer:
    """是正で文脈や画像が変わり得るため、回答と、それを生成した入力を一緒に返す。"""

    response: AnswerResponse
    context: AnswerContext
    image_evidence: tuple[dict[str, Any], ...]
    image_prompt_mode: str


def extract_original_question(question: Any) -> str:
    """再実行時に表示文字列から元の質問だけを取り出します。"""
    text = str(question or "").replace("\r\n", "\n").strip()
    for separator in (
        QUESTION_DISPLAY_METADATA_SEPARATOR,
        ANSWER_FLOW_DISPLAY_SEPARATOR,
        QUERY_STRATEGY_DISPLAY_SEPARATOR,
        QUERY_EXPANSION_SEPARATOR,
        QUESTION_TEXT_SEARCH_SEPARATOR,
        CRAG_RETRIEVAL_DISPLAY_SEPARATOR,
        QUESTION_PLAN_SEPARATOR,
        RUNTIME_KNOWLEDGE_SEPARATOR,
        INQUIRY_CONDITIONS_SEPARATOR,
        *LEGACY_QUESTION_METADATA_SEPARATORS,
    ):
        if separator in text:
            text = text.split(separator, 1)[0].rstrip()
    return text.strip()


def answer_flow_id(value: Any) -> str:
    """UI 入力値を既知の回答 flow ID へ正規化します。"""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_ANSWER_FLOW
    if text in _ANSWER_FLOW_LABEL_BY_ID:
        return text
    if text in _ANSWER_FLOW_ID_BY_LABEL:
        return _ANSWER_FLOW_ID_BY_LABEL[text]
    if text in _ANSWER_FLOW_ID_BY_LEGACY_LABEL:
        return _ANSWER_FLOW_ID_BY_LEGACY_LABEL[text]

    normalized = _normalize_text(text)
    aliases = {
        "standard": STANDARD_ANSWER_FLOW,
        "standard rag": STANDARD_ANSWER_FLOW,
        "rag": STANDARD_ANSWER_FLOW,
        "retrieve-then-generate": STANDARD_ANSWER_FLOW,
        "retrieve then generate": STANDARD_ANSWER_FLOW,
        "crag": CRAG_ANSWER_FLOW,
        "corrective rag": CRAG_ANSWER_FLOW,
        "crag corrective rag max 3": CRAG_ANSWER_FLOW,
        "crag(corrective rag / max 3)": CRAG_ANSWER_FLOW,
        "crag (corrective rag / max 3)": CRAG_ANSWER_FLOW,
        "corrective rag(crag:検索結果を評価して補正検索 / 最大3回)": CRAG_ANSWER_FLOW,
        "corrective rag(crag:検索結果を評価し、必要なら補正検索 / 最大3回)": CRAG_ANSWER_FLOW,
    }
    if normalized in aliases:
        return aliases[normalized]
    for flow_id, label in ANSWER_FLOWS:
        if normalized in {_normalize_text(flow_id), _normalize_text(label)}:
            return flow_id
    return DEFAULT_ANSWER_FLOW


def answer_flow_label(flow_id: str) -> str:
    """回答 flow ID を UI 表示ラベルへ変換します。"""
    return _ANSWER_FLOW_LABEL_BY_ID.get(flow_id, DEFAULT_ANSWER_FLOW_LABEL)


def retrieval_scope_label(scope_id: str) -> str:
    """検索範囲 ID を UI 表示ラベルへ変換します。"""
    scope = normalize_retrieval_scope(scope_id)
    if scope == RETRIEVAL_SCOPE_KNOWLEDGE_BASE:
        return "Knowledge Base（全登録ファイル）"
    return "現在のファイル"


def _legacy_crag_query_strategy(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = _normalize_text(text)
    return normalized in {
        _normalize_text(CRAG_STRATEGY),
        _normalize_text(CRAG_LABEL),
        "crag(corrective rag / max 3)",
        "crag (corrective rag / max 3)",
        "corrective rag(crag:検索結果を評価して補正検索 / 最大3回)",
        "corrective rag(crag:検索結果を評価し、必要なら補正検索 / 最大3回)",
        "crag",
        "corrective rag",
    }


def query_strategy_id(value: Any) -> str:
    """UI 入力値を既知の質問拡張 strategy ID へ正規化します。"""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_QUERY_STRATEGY
    if text in _QUERY_STRATEGY_LABEL_BY_ID:
        return text
    if text in _QUERY_STRATEGY_ID_BY_LABEL:
        return _QUERY_STRATEGY_ID_BY_LABEL[text]
    if text in _QUERY_STRATEGY_ID_BY_LEGACY_LABEL:
        return _QUERY_STRATEGY_ID_BY_LEGACY_LABEL[text]

    normalized = _normalize_text(text)
    aliases = {
        "auto": AUTO_ROUTING_STRATEGY,
        "auto routing": AUTO_ROUTING_STRATEGY,
        "simple": SIMPLE_RETRIEVAL_STRATEGY,
        "simple retrieval": SIMPLE_RETRIEVAL_STRATEGY,
        "rag fusion": RAG_FUSION_STRATEGY,
        "rag-fusion": RAG_FUSION_STRATEGY,
        "multi-query retrieval": RAG_FUSION_STRATEGY,
        "multi query retrieval": RAG_FUSION_STRATEGY,
        "query decomposition": QUERY_DECOMPOSITION_STRATEGY,
        "step-back prompting": STEP_BACK_PROMPTING_STRATEGY,
        "step back prompting": STEP_BACK_PROMPTING_STRATEGY,
        "hyde": HYDE_STRATEGY,
        "hypothetical document embeddings": HYDE_STRATEGY,
        # 自動ルーティングの LLM は label を短く返すことがある。解決できないと単純検索へ落ちる。
        "decomposition": QUERY_DECOMPOSITION_STRATEGY,
        "step-back": STEP_BACK_PROMPTING_STRATEGY,
        "step back": STEP_BACK_PROMPTING_STRATEGY,
        "step_back": STEP_BACK_PROMPTING_STRATEGY,
        "ステップバック": STEP_BACK_PROMPTING_STRATEGY,
        "仮説文生成": HYDE_STRATEGY,
        "フュージョン": RAG_FUSION_STRATEGY,
    }
    # 「HyDE（仮説文生成）」のように括弧書きを足した入力は、括弧の前で解決する。
    for candidate in (normalized, _label_head(normalized)):
        if candidate in aliases:
            return aliases[candidate]
        for strategy_id, label in QUERY_STRATEGIES:
            if candidate in {_normalize_text(strategy_id), _normalize_text(label), _label_head(_normalize_text(label))}:
                return strategy_id
    return DEFAULT_QUERY_STRATEGY


def _label_head(normalized: str) -> str:
    """NFKC 済みの label から、括弧書きの前の部分を返します。"""
    return normalized.split("(", 1)[0].strip()


def query_strategy_label(strategy_id: str) -> str:
    """質問拡張 strategy ID を UI 表示ラベルへ変換します。"""
    return _QUERY_STRATEGY_LABEL_BY_ID.get(strategy_id, DEFAULT_QUERY_STRATEGY_LABEL)


def _dedupe_queries(queries: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    selected: list[str] = []
    for query in queries:
        text = str(query or "").strip()
        key = _dedupe_query_key(text)
        if not key or key in seen:
            continue
        selected.append(text)
        seen.add(key)
    return tuple(selected)


def _dedupe_query_key(value: str) -> str:
    return re.sub(r"\s+", "", _normalize_text(str(value or "")))


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold()


def _page_span(page_start: int, page_end: int) -> str:
    if page_start == page_end:
        return f"p.{page_start}"
    return f"p.{page_start}-{page_end}"


def _format_seq_ranges(ranges: Sequence[dict[str, int]]) -> str:
    pieces = []
    for item in ranges:
        page = item.get("page")
        start = item.get("seq_start")
        end = item.get("seq_end")
        if start == end:
            pieces.append(f"p.{page} #{start}")
        else:
            pieces.append(f"p.{page} #{start}-{end}")
    return ", ".join(pieces)
