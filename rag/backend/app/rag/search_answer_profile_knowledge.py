"""検索・回答プロファイル単位の知識(ドメインキーワード等)を Oracle の JSON payload で管理する。

payload 形式と正規化・候補生成は rag_poc の ``rag_engine.knowledge`` をそのまま使う。
保存先はファイルではなく ``rag_search_answer_profile_knowledge``(検索・回答プロファイル × 種別)。
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pr_backend_core.logging import safe_exception_fields
from pr_system_settings.embedding import EMBEDDING_DIMENSIONS
from rag_engine.knowledge.approved_faq import (
    APPROVED_FAQ_APPROVED_STATUS,
    APPROVED_FAQ_IMPORT_MODES,
    DEFAULT_APPROVED_FAQ_DIRECT_MATCH_MIN_SCORE,
    DEFAULT_APPROVED_FAQ_MIN_SCORE,
    DEFAULT_APPROVED_FAQ_SUGGESTION_LIMIT,
    ApprovedFaqImportRow,
    ApprovedFaqMutationResult,
    ApprovedFaqRecord,
    ApprovedFaqSemanticIndex,
    ApprovedFaqSuggestion,
    _approved_faq_semantic_questions,
    add_approved_faq_record,
    apply_approved_faq_import_rows,
    approved_faq_records_signature,
    build_approved_faq_semantic_index,
    delete_approved_faq_records,
    load_approved_faq_excel_rows,
    load_approved_faq_payload,
    load_approved_faq_records,
    load_approved_faq_semantic_index,
    suggest_approved_faq_questions,
)
from rag_engine.knowledge.domain_keyword_candidates import (
    DomainKeywordCandidate,
    DomainKeywordSourceText,
    suggest_domain_keyword_candidates,
)
from rag_engine.knowledge.domain_keywords import (
    DOMAIN_KEYWORDS_SCHEMA_VERSION,
    MAX_DOMAIN_KEYWORDS,
    normalize_domain_keyword,
    normalize_domain_keywords,
)
from rag_engine.knowledge.runtime_knowledge import (
    MAX_RULES,
    MAX_TERMS,
    RuntimeKnowledgeContext,
    build_runtime_knowledge_context,
)
from rag_engine.knowledge.runtime_knowledge_management import (
    edit_knowledge,
    load_knowledge_snapshot,
)
from rag_engine.retrieval.text_search_tokenizer import TextSearchTokenizerConfig

from app.rag.answer_engine import AnswerScope
from app.schemas.search import PageRange, format_page_ranges
from app.schemas.search_answer_profile_knowledge import (
    ClarificationAnswer,
    ClarificationSection,
    RuleClarification,
)

logger = logging.getLogger(__name__)

DOMAIN_KEYWORDS_KIND = "domain_keywords"
DEFAULT_CANDIDATE_LIMIT = 50
DEFAULT_CANDIDATE_SOURCE_CHUNKS = 2000


class SearchAnswerProfileKnowledgeStore(Protocol):
    async def get_search_answer_profile_knowledge(
        self, search_answer_profile_id: str, kind: str
    ) -> dict[str, object] | None: ...

    async def save_search_answer_profile_knowledge(
        self, search_answer_profile_id: str, kind: str, payload: dict[str, object]
    ) -> None: ...

    async def list_search_answer_profile_chunk_texts(
        self, knowledge_base_ids: list[str], *, limit: int
    ) -> list[tuple[str, str, str]]: ...


@dataclass(frozen=True)
class DomainKeywordSuggestion:
    candidates: list[DomainKeywordCandidate]
    processed_chunk_count: int


async def load_domain_keywords(
    store: SearchAnswerProfileKnowledgeStore, search_answer_profile_id: str
) -> list[str]:
    """保存済みドメインキーワード(正規化済み)を返す。未登録は空。"""
    payload = await store.get_search_answer_profile_knowledge(
        search_answer_profile_id, DOMAIN_KEYWORDS_KIND
    )
    if payload is None:
        return []
    return normalize_domain_keywords(payload.get("keywords"))


def validate_domain_keywords(values: list[str]) -> list[str]:
    """既存の正規化で重複を除き、上限超過は切り捨てず拒否する。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        keyword = normalize_domain_keyword(value)
        key = keyword.casefold()
        if keyword and key not in seen:
            seen.add(key)
            result.append(keyword)
            if len(result) > MAX_DOMAIN_KEYWORDS:
                raise ValueError(f"ドメインキーワードは {MAX_DOMAIN_KEYWORDS} 件までです。")
    return result


async def save_domain_keywords(
    store: SearchAnswerProfileKnowledgeStore,
    search_answer_profile_id: str,
    keywords: list[str],
) -> list[str]:
    """正規化(重複・空白・長さ・件数上限)して保存し、保存後の一覧を返す。"""
    normalized = validate_domain_keywords(keywords)
    await store.save_search_answer_profile_knowledge(
        search_answer_profile_id,
        DOMAIN_KEYWORDS_KIND,
        {
            "schema_version": DOMAIN_KEYWORDS_SCHEMA_VERSION,
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "keywords": normalized,
        },
    )
    return normalized


async def suggest_domain_keywords(
    store: SearchAnswerProfileKnowledgeStore,
    search_answer_profile_id: str,
    knowledge_base_ids: list[str],
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    max_source_chunks: int = DEFAULT_CANDIDATE_SOURCE_CHUNKS,
) -> DomainKeywordSuggestion:
    """参照 KB の配信中 chunk から TF-IDF でキーワード候補を作る(LLM は使わない)。"""
    rows = await store.list_search_answer_profile_chunk_texts(
        knowledge_base_ids, limit=max_source_chunks
    )
    sources = [
        DomainKeywordSourceText(chunk_id=chunk_id, document_id=document_id, text=text)
        for chunk_id, document_id, text in rows
        if text.strip()
    ]
    existing = await load_domain_keywords(store, search_answer_profile_id)
    candidates = suggest_domain_keyword_candidates(
        sources,
        existing_keywords=existing,
        tokenizer_config=TextSearchTokenizerConfig(),
        limit=limit,
    )
    return DomainKeywordSuggestion(candidates=candidates, processed_chunk_count=len(sources))


# --- Approved FAQ(類似問)-----------------------------------------------------

APPROVED_FAQ_KIND = "approved_faq"
FAQ_SEMANTIC_CACHE_KEY = "semantic_index_cache"
# (texts, input_type) -> vectors。backend の Cohere embedding(OciGenAiClient.embed)を渡す。
FaqEmbedInputType = Literal["SEARCH_DOCUMENT", "SEARCH_QUERY"]
FaqEmbedder = Callable[[list[str], FaqEmbedInputType], Awaitable[list[list[float]]]]
APPROVED_FAQ_PREVIEW_ROWS = 10


@dataclass(frozen=True)
class ApprovedFaqMutation:
    records: list[ApprovedFaqRecord]
    inserted_count: int
    deleted_count: int


FAQ_ENABLED_KEY = "enabled"


def approved_faq_enabled(payload: Mapping[str, object] | None) -> bool:
    """回答の前に類似問を提示するか(検索・回答プロファイルごと。未設定はオン。#684)。"""
    return (payload or {}).get(FAQ_ENABLED_KEY) is not False


async def load_approved_faq(
    store: SearchAnswerProfileKnowledgeStore, search_answer_profile_id: str
) -> list[ApprovedFaqRecord]:
    """検索・回答プロファイルの承認済み FAQ を返す。"""
    payload = await store.get_search_answer_profile_knowledge(
        search_answer_profile_id, APPROVED_FAQ_KIND
    )
    if payload is None:
        return []
    with _faq_file(payload) as path:
        return load_approved_faq_records(path)


async def load_approved_faq_enabled(
    store: SearchAnswerProfileKnowledgeStore, search_answer_profile_id: str
) -> bool:
    payload = await store.get_search_answer_profile_knowledge(
        search_answer_profile_id, APPROVED_FAQ_KIND
    )
    return approved_faq_enabled(payload)


async def save_approved_faq_enabled(
    store: SearchAnswerProfileKnowledgeStore, search_answer_profile_id: str, enabled: bool
) -> None:
    """類似問の提示のオン / オフを保存する(FAQ の登録内容は変えない)。"""
    payload = await store.get_search_answer_profile_knowledge(
        search_answer_profile_id, APPROVED_FAQ_KIND
    )
    with _faq_file(payload) as path:
        # 未登録なら FAQ の標準形式(空)に設定だけを持たせる。
        base = load_approved_faq_payload(path)
    await store.save_search_answer_profile_knowledge(
        search_answer_profile_id, APPROVED_FAQ_KIND, {**base, FAQ_ENABLED_KEY: enabled}
    )


async def find_approved_faq(
    store: SearchAnswerProfileKnowledgeStore, search_answer_profile_id: str, faq_id: str
) -> ApprovedFaqRecord | None:
    """利用者が選んだ類似問を、検索・回答プロファイルの承認
    済み FAQ から引き直す(提示がオンのときだけ)。"""
    payload = await store.get_search_answer_profile_knowledge(
        search_answer_profile_id, APPROVED_FAQ_KIND
    )
    if payload is None or not approved_faq_enabled(payload):
        return None
    with _faq_file(payload) as path:
        records = load_approved_faq_records(path)
    return next(
        (
            record
            for record in records
            if record.id == faq_id and record.status == APPROVED_FAQ_APPROVED_STATUS
        ),
        None,
    )


async def mutate_approved_faq(
    store: SearchAnswerProfileKnowledgeStore,
    search_answer_profile_id: str,
    operation: Callable[[Path], ApprovedFaqMutationResult],
) -> ApprovedFaqMutation:
    """rag_poc の FAQ 更新関数(ファイル前提)を一時ファイル上で実行し、結果を DB へ保存する。

    ponytail: 同時編集は後勝ち。競合検出が必要になったら revision を条件に MERGE する。
    """
    payload = await store.get_search_answer_profile_knowledge(
        search_answer_profile_id, APPROVED_FAQ_KIND
    )
    with _faq_file(payload) as path:
        result = operation(path)
        updated = load_approved_faq_payload(path)
        records = load_approved_faq_records(path)
    if payload is not None and FAQ_ENABLED_KEY in payload:
        # FAQ の更新関数は設定を知らないので、類似問の提示のオン / オフを引き継ぐ。
        updated = {**updated, FAQ_ENABLED_KEY: payload[FAQ_ENABLED_KEY]}
    await store.save_search_answer_profile_knowledge(
        search_answer_profile_id, APPROVED_FAQ_KIND, updated
    )
    return ApprovedFaqMutation(
        records=records,
        inserted_count=result.inserted_count,
        deleted_count=result.deleted_count,
    )


async def add_approved_faq(
    store: SearchAnswerProfileKnowledgeStore,
    search_answer_profile_id: str,
    *,
    question: str,
    answer: str,
) -> ApprovedFaqMutation:
    return await mutate_approved_faq(
        store,
        search_answer_profile_id,
        lambda path: add_approved_faq_record(path, question=question, approved_answer=answer),
    )


async def delete_approved_faq(
    store: SearchAnswerProfileKnowledgeStore, search_answer_profile_id: str, ids: list[str]
) -> ApprovedFaqMutation:
    return await mutate_approved_faq(
        store, search_answer_profile_id, lambda path: delete_approved_faq_records(path, ids)
    )


def read_approved_faq_excel(content: bytes, file_name: str) -> list[ApprovedFaqImportRow]:
    """QUESTION / ANSWER 列の Excel を FAQ 取込行へ読む(不正な形式は ValueError)。"""
    suffix = Path(file_name or "").suffix.lower()
    if suffix not in {".xlsx", ".xls"}:
        raise ValueError("Excel ファイル(.xlsx / .xls)を指定してください。")
    with tempfile.TemporaryDirectory(prefix="approved-faq-") as work:
        path = Path(work) / Path(file_name).name
        path.write_bytes(content)
        return load_approved_faq_excel_rows(path)


async def import_approved_faq(
    store: SearchAnswerProfileKnowledgeStore,
    search_answer_profile_id: str,
    rows: list[ApprovedFaqImportRow],
    *,
    mode: str,
) -> ApprovedFaqMutation:
    if mode not in APPROVED_FAQ_IMPORT_MODES:
        raise ValueError(f"取込モードが不正です: {mode}")
    return await mutate_approved_faq(
        store,
        search_answer_profile_id,
        lambda path: apply_approved_faq_import_rows(path, rows, mode=mode),
    )


async def suggest_approved_faq(
    store: SearchAnswerProfileKnowledgeStore,
    search_answer_profile_id: str,
    question: str,
    *,
    limit: int = DEFAULT_APPROVED_FAQ_SUGGESTION_LIMIT,
    min_score: float | None = None,
    embed: FaqEmbedder | None = None,
    embedding_model: str = "",
    embedding_dimensions: int = EMBEDDING_DIMENSIONS,
) -> list[ApprovedFaqSuggestion]:
    """質問に近い承認済み FAQ を返す。

    文字列類似度に加え、embed 指定時は rag_poc の意味照合(FAQ 質問の embedding index)を使う。
    index は FAQ payload の ``semantic_index_cache`` に保持し、FAQ 集合の署名が変われば作り直す。
    embedding に失敗しても文字列照合で続ける。
    """
    payload = await store.get_search_answer_profile_knowledge(
        search_answer_profile_id, APPROVED_FAQ_KIND
    )
    if payload is None or not question.strip() or not approved_faq_enabled(payload):
        return []
    with _faq_file(payload) as path:
        records = load_approved_faq_records(path)
    if not records:
        return []
    semantic_index: ApprovedFaqSemanticIndex | None = None
    query_embedding: list[float] | None = None
    if embed is not None:
        try:
            semantic_index, cache = await _faq_semantic_index(
                payload, records, embed, model=embedding_model, dimensions=embedding_dimensions
            )
            if cache is not None:
                await store.save_search_answer_profile_knowledge(
                    search_answer_profile_id,
                    APPROVED_FAQ_KIND,
                    {**payload, FAQ_SEMANTIC_CACHE_KEY: cache},
                )
            query_embedding = (await embed([question], "SEARCH_QUERY"))[0]
        except Exception as exc:  # 意味照合は補助。文字列照合で続ける。
            logger.warning(
                "approved faq semantic matching failed", extra={**safe_exception_fields(exc)}
            )
            semantic_index, query_embedding = None, None
    return suggest_approved_faq_questions(
        question,
        records,
        semantic_index=semantic_index,
        semantic_query_embedding=query_embedding,
        limit=limit,
        min_score=DEFAULT_APPROVED_FAQ_MIN_SCORE if min_score is None else min_score,
    )


async def _faq_semantic_index(
    payload: Mapping[str, object],
    records: list[ApprovedFaqRecord],
    embed: FaqEmbedder,
    *,
    model: str,
    dimensions: int,
) -> tuple[ApprovedFaqSemanticIndex, dict[str, object] | None]:
    """保存済み index を再利用し、無効なら FAQ 質問を embedding して作り直す(新 cache を返す)。"""
    with tempfile.TemporaryDirectory(prefix="approved-faq-semantic-") as work:
        cache_path = Path(work) / "semantic_index.json"
        cached = payload.get(FAQ_SEMANTIC_CACHE_KEY)
        if isinstance(cached, dict):
            cache_path.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
            index = load_approved_faq_semantic_index(
                cache_path,
                model=model,
                dimensions=dimensions,
                records_signature=approved_faq_records_signature(records),
            )
            if index is not None:
                return index, None
        questions = [question for _, question in _approved_faq_semantic_questions(records)]
        vectors = await embed(questions, "SEARCH_DOCUMENT") if questions else []
        index = build_approved_faq_semantic_index(
            cache_path,
            records,
            model=model,
            dimensions=dimensions,
            embedder=lambda texts, settings: vectors,
            settings=None,
        )
        return index, json.loads(cache_path.read_text(encoding="utf-8"))


def is_direct_faq_match(suggestion: ApprovedFaqSuggestion) -> bool:
    """rag_poc と同じく 0.92 以上を FAQ 回答をそのまま使える候補とみなす。"""
    return suggestion.score >= DEFAULT_APPROVED_FAQ_DIRECT_MATCH_MIN_SCORE


@contextmanager
def _faq_file(payload: Mapping[str, object] | None) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="approved-faq-") as work:
        path = Path(work) / "approved_faq.json"
        if payload is not None:
            path.write_text(json.dumps(dict(payload), ensure_ascii=False), encoding="utf-8")
        yield path


# --- 用語・同義語と回答ルール(runtime knowledge)-------------------------------------------

RUNTIME_KNOWLEDGE_KIND = "runtime_knowledge"


async def load_runtime_knowledge_payload(
    store: SearchAnswerProfileKnowledgeStore, search_answer_profile_id: str
) -> dict[str, object]:
    """検索・回答プロファイルの用語・同義語と回答ルールの payload を返す(未登録は空の標準形式)。"""
    payload = await store.get_search_answer_profile_knowledge(
        search_answer_profile_id, RUNTIME_KNOWLEDGE_KIND
    )
    return payload or {"schema_version": 1, "terms": [], "rules": []}


async def edit_runtime_knowledge(
    store: SearchAnswerProfileKnowledgeStore,
    search_answer_profile_id: str,
    *,
    kind: str,
    selected: str | None,
    name: str = "",
    title: str = "",
    labels: str = "",
    content: str = "",
    source: str = "",
    purpose: str | None = None,
    instruction_kind: str | None = None,
    exclusive_group: str | None = None,
    priority: int | None = None,
    enabled: bool = True,
    delete: bool = False,
) -> dict[str, object]:
    """rag_poc の edit_knowledge を一時ファイル上で実行し、結果を DB へ保存する。"""
    payload = await load_runtime_knowledge_payload(store, search_answer_profile_id)
    with _runtime_knowledge_dir(payload) as (work_dir, path):
        snapshot = load_knowledge_snapshot(work_dir, path)
        updated, _ = edit_knowledge(
            snapshot,
            kind,
            selected,
            name=name,
            title=title,
            labels=labels,
            content=content,
            source=source,
            purpose=purpose,
            instruction_kind=instruction_kind,
            exclusive_group=exclusive_group,
            priority=priority,
            enabled=enabled,
            delete=delete,
            confirmed=delete,
        )
    await store.save_search_answer_profile_knowledge(
        search_answer_profile_id, RUNTIME_KNOWLEDGE_KIND, dict(updated.payload)
    )
    return dict(updated.payload)


def preview_runtime_knowledge(
    payload: Mapping[str, object], question: str
) -> RuntimeKnowledgeContext:
    """質問に一致する用語・同義語と回答ルールと拡張後の検索文を返す(保存しない)。"""
    with _runtime_knowledge_dir(payload) as (work_dir, path):
        return build_runtime_knowledge_context(question, work_dir, path)


def match_runtime_knowledge(
    payload: Mapping[str, object], question: str
) -> RuntimeKnowledgeContext:
    """質問に当たる用語と、用途・排他・明示優先度で採用した確認・回答ルールを返す（保存しない）。

    照合・有効状態・用途と排他の判定は回答生成と同じ。生成側は rules_for_stage で取り出す。
    件数・字数で切って省いた数を示すのは呼び出し元(MCP の根拠の取得。#2701)。
    """
    with _runtime_knowledge_dir(payload) as (work_dir, path):
        return build_runtime_knowledge_context(
            question, work_dir, path, term_limit=MAX_TERMS, rule_limit=MAX_RULES
        )


@contextmanager
def _runtime_knowledge_dir(payload: Mapping[str, object]) -> Iterator[tuple[Path, Path]]:
    with tempfile.TemporaryDirectory(prefix="runtime-knowledge-") as work:
        work_dir = Path(work)
        path = work_dir / "runtime_knowledge.json"
        path.write_text(json.dumps(dict(payload), ensure_ascii=False), encoding="utf-8")
        yield work_dir, path


# --- ルールの確認の質問(チャットの確認。#717)-------------------------------------------


def _rules(payload: Mapping[str, object]) -> list[dict[str, object]]:
    rules = payload.get("rules")
    return [rule for rule in rules if isinstance(rule, dict)] if isinstance(rules, list) else []


def rule_clarification(rule: Mapping[str, object]) -> RuleClarification | None:
    """ルールの確認(無い・壊れているときは None)。"""
    if rule.get("purpose", "both") not in ("confirmation", "both"):
        return None
    raw = rule.get("clarification")
    if not isinstance(raw, Mapping):
        return None
    try:
        return RuleClarification.model_validate(dict(raw))
    except ValueError:
        return None


async def save_rule_clarification(
    store: SearchAnswerProfileKnowledgeStore,
    search_answer_profile_id: str,
    rule_id: str,
    clarification: RuleClarification | None,
) -> dict[str, object]:
    """ルールに確認の質問を保存する(None は外す)。ルールが無ければ KeyError。"""
    payload = await load_runtime_knowledge_payload(store, search_answer_profile_id)
    rules = _rules(payload)
    rule = next((item for item in rules if str(item.get("id", "")) == rule_id), None)
    if rule is None:
        raise KeyError(rule_id)
    if clarification is None:
        rule.pop("clarification", None)
    else:
        rule["clarification"] = clarification.model_dump()
    updated = {**payload, "rules": rules}
    await store.save_search_answer_profile_knowledge(
        search_answer_profile_id, RUNTIME_KNOWLEDGE_KIND, updated
    )
    return updated


def _already_answered(clarification: RuleClarification, answer: ClarificationAnswer | None) -> bool:
    """語の出現ではなく、現在の選択肢に対する明示的な選択だけを確定する。"""
    if answer is None or not answer.option_ids or answer.other_text.strip():
        return False
    selected = set(answer.option_ids)
    return (
        len(selected) == len(answer.option_ids)
        and selected <= {option.id for option in clarification.options}
        and (clarification.multiple or len(selected) == 1)
    )


def suggest_clarification(
    payload: Mapping[str, object], question: str, *, answers: Sequence[ClarificationAnswer] = ()
) -> tuple[Mapping[str, object], RuleClarification] | None:
    """質問に一致したルールのうち、確認を持つ最初の 1 件(質問で既に答えているものは除く)。"""
    found = unanswered_rule_clarifications(payload, question, answers=answers)
    return found[0] if found else None


def unanswered_rule_clarifications(
    payload: Mapping[str, object], question: str, *, answers: Sequence[ClarificationAnswer] = ()
) -> list[tuple[Mapping[str, object], RuleClarification]]:
    """一致したルールの未回答の確認事項。確認の提示と前提の明示で同じ判定を使う。"""
    found: list[tuple[Mapping[str, object], RuleClarification]] = []
    by_id = {str(rule.get("id", "")): rule for rule in _rules(payload)}
    for matched in preview_runtime_knowledge(payload, question).rules_for_stage("confirmation"):
        rule = by_id.get(matched.rule_id)
        if rule is None:
            continue
        clarification = rule_clarification(rule)
        selections = [answer for answer in answers if answer.rule_id == matched.rule_id]
        answer = selections[0] if len(selections) == 1 else None
        if clarification is not None and not _already_answered(clarification, answer):
            found.append((rule, clarification))
    return found


def rule_matches(payload: Mapping[str, object], question: str, rule_id: str) -> bool:
    """質問がそのルールに一致するか（MCP の答えを、一致する質問にだけ当てる。#2117）。"""
    return any(
        matched.rule_id == rule_id
        for matched in preview_runtime_knowledge(payload, question).matched_rules
    )


def rule_answer(
    payload: Mapping[str, object], rule_id: str, value: str
) -> ClarificationAnswer | None:
    """MCP の ``conditions`` の値（選んだ選択肢の表示名と自由入力を「、」で並べた文）を答えにする。

    Agent は確認の答えを表示名で返す（#2117）。表示名と同じ部分を選択肢、残りを自由入力にする。
    ルール・確認が無い、値が空なら None。
    """
    rule = next((item for item in _rules(payload) if str(item.get("id", "")) == rule_id), None)
    clarification = rule_clarification(rule) if rule is not None else None
    parts = [part.strip() for part in value.split("、") if part.strip()]
    if clarification is None or not parts:
        return None
    # ponytail: 「、」を含む表示名は自由入力として渡る（前提の文には残る）。
    by_label = {option.label: option.id for option in clarification.options}
    picked = list(dict.fromkeys(by_label[part] for part in parts if part in by_label))
    if not clarification.multiple:
        picked = picked[:1]
    other = "、".join(part for part in parts if by_label.get(part) not in picked)
    return ClarificationAnswer(rule_id=rule_id, option_ids=picked, other_text=other[:500])


SectionPages = Mapping[tuple[str, str], tuple[int | None, int | None]]


def _answer_clarification(
    payload: Mapping[str, object], answer: ClarificationAnswer
) -> RuleClarification | None:
    rule = next(
        (item for item in _rules(payload) if str(item.get("id", "")) == answer.rule_id), None
    )
    return rule_clarification(rule) if rule is not None else None


def clarification_record(
    payload: Mapping[str, object], rule_id: str, answer: ClarificationAnswer | None
) -> dict[str, object] | None:
    """会話に残す「回答前に確認した内容」（#1839）。``answer`` が None なら飛ばした記録。

    確認事項と選んだ選択肢の名前は保存済みのルールから引き直す。ルールが見つからなければ None。
    """
    rule = next((item for item in _rules(payload) if str(item.get("id", "")) == rule_id), None)
    clarification = rule_clarification(rule) if rule is not None else None
    if clarification is None:
        return None
    if answer is None:
        return {"outcome": "skipped", "entries": [{"prompt": clarification.question}]}
    labels = [option.label for option in clarification.options if option.id in answer.option_ids]
    entry: dict[str, object] = {
        "prompt": clarification.question,
        "selected": labels,
        "other_text": answer.other_text.strip(),
    }
    if not clarification.options:
        # 選択肢の無い確認事項の答えに「その他: 」を付けない（#3410）。
        entry["free_text_only"] = True
    return {"outcome": "answered", "entries": [entry]}


def clarification_document_ids(
    payload: Mapping[str, object], answer: ClarificationAnswer
) -> set[str]:
    """利用者が選んだ選択肢の章節がある文書(今のページを読み直す対象。#721)。"""
    clarification = _answer_clarification(payload, answer)
    if clarification is None:
        return set()
    return {
        section.document_id
        for option in clarification.options
        if option.id in answer.option_ids
        for section in option.sections
    }


def resolve_clarification(
    payload: Mapping[str, object],
    answer: ClarificationAnswer,
    current_pages: SectionPages | None = None,
) -> tuple[AnswerScope, str] | None:
    """利用者の確認の回答を、回答の前提(AnswerScope)と page_ranges の値にする。

    ルール・選択肢は保存済みの payload から引き直す(画面が送った範囲をそのまま信じない)。
    章節のページは ``current_pages``((文書 ID, 章節 ID) → 今のページ)を使い、章節が今は無い
    ときだけ確認を保存したときのページを使う(#721)。ルール・選択肢が見つからなければ None。
    """
    clarification = _answer_clarification(payload, answer)
    if clarification is None:
        return None
    options = [option for option in clarification.options if option.id in answer.option_ids]
    if len(options) != len(set(answer.option_ids)) or (
        len(options) > 1 and not clarification.multiple
    ):
        return None
    # 「その他（自由入力）」は 3 製品で常に出す（#1839）。`allow_other` は古い保存の値で、読まない。
    other = answer.other_text.strip()
    if not options and not other:
        return None
    pages = current_pages or {}

    def current(section: ClarificationSection) -> ClarificationSection:
        found = pages.get((section.document_id, section.section_id))
        if found is None:
            return section
        return section.model_copy(update={"page_start": found[0], "page_end": found[1]})

    sections = [current(section) for option in options for section in option.sections]
    labels = [
        f"「{section.document_name or section.document_id}」の「{section.title}」"
        + (_pages_label(section.page_start, section.page_end))
        for section in sections
    ]
    lines = [f"確認の質問: {clarification.question}"]
    if options:
        lines.append("選んだ答え: " + "、".join(option.label for option in options))
    lines.extend(f"前提: {option.premise}" for option in options if option.premise.strip())
    if other:
        lines.append(f"その他（利用者の入力）: {other}")
    if labels:
        lines.append("対象範囲: " + "、".join(labels))
    page_ranges = format_page_ranges(
        [
            PageRange(
                document_id=section.document_id,
                page_start=section.page_start,
                page_end=section.page_end,
            )
            for section in sections
        ]
    )
    scope = AnswerScope(
        context="\n".join(lines),
        search_terms=tuple(term for option in options for term in option.search_terms),
        label="、".join(labels),
        confirmed_premises=(
            (
                clarification.question,
                "、".join([*(option.label for option in options), *([other] if other else [])]),
            ),
        ),
    )
    return scope, page_ranges


def _pages_label(start: int | None, end: int | None) -> str:
    if start is None:
        return ""
    return f"（p.{start}）" if end is None or end == start else f"（p.{start}–{end}）"
