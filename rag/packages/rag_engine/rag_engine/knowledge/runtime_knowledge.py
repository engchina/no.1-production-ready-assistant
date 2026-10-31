"""実行時 glossary/rules を読み込み、質問と prompt へ反映する。"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from rag_engine.matching.analysis import normalize_text

from .rule_application import rule_version, select_rule_applications
from .rule_matching import rank_rules

logger = logging.getLogger(__name__)

RUNTIME_KNOWLEDGE_SCHEMA_VERSION = 1
RUNTIME_KNOWLEDGE_FILE_NAME = "runtime_knowledge.json"
RUNTIME_KNOWLEDGE_ACTIVE_STATUSES = {"", "approved", "stale_review_needed"}
MAX_TERMS = 1000
MAX_RULES = 1000
MAX_ALIASES_PER_TERM = 32
MAX_TRIGGERS_PER_RULE = 32
MAX_MATCHED_TERMS = 8
MAX_MATCHED_RULES = 6
MAX_TEXT_CHARS = 800

# 原質問に語を足した出どころの表示名（検索文の出どころ・検索語の元に出す）。設定の「プロファイルの知識」のタブと
# 処理の経過の段の名前にそろえる（#3451・#3490）。
TERMS_SOURCE_LABEL = "用語・同義語"
RULES_SOURCE_LABEL = "確認・回答ルール"
GUIDE_SOURCE_LABEL = "業務ガイド"
SCOPE_SOURCE_LABEL = "確認で選んだ条件"
ORIGINAL_QUESTION_SOURCE_LABEL = "原質問"


@dataclass(frozen=True)
class RuntimeTerm:
    """実行時 glossary の 1 用語と別名・説明・review 状態を保持します。"""

    term: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    source: str = ""
    tags: tuple[str, ...] = ()
    status: str = ""
    updated_at: str = ""
    reviewed_at: str = ""
    next_review_at: str = ""

    @property
    def active(self) -> bool:
        """検索や prompt へ反映する有効条件かどうかを返します。"""
        return self.status in RUNTIME_KNOWLEDGE_ACTIVE_STATUSES

    def labels(self) -> tuple[str, ...]:
        """照合に使う代表ラベルと別名を順序付きで返します。"""
        return _ordered_unique((self.term, *self.aliases))

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {
            "term": self.term,
            "aliases": list(self.aliases),
            "description": self.description,
            "source": self.source,
            "tags": list(self.tags),
            "status": self.status,
            "updated_at": self.updated_at,
            "reviewed_at": self.reviewed_at,
            "next_review_at": self.next_review_at,
        }


@dataclass(frozen=True)
class RuntimeRule:
    """質問に一致したとき prompt へ追加する実行時ルールを保持します。"""

    rule_id: str
    title: str
    triggers: tuple[str, ...] = ()
    content: str = ""
    purpose: str = "both"
    instruction_kind: str = "requirement"
    exclusive_group: str = ""
    priority: int = 0
    confirmation_signature: str = ""
    source: str = ""
    tags: tuple[str, ...] = ()
    status: str = ""
    updated_at: str = ""
    reviewed_at: str = ""
    next_review_at: str = ""

    @property
    def version(self) -> str:
        """実際に使う内容と条件の版。手順の条件解決で本文を置換した場合も更新する。"""
        return rule_version(
            {
                "id": self.rule_id,
                "title": self.title,
                "triggers": self.triggers,
                "content": self.content,
                "source": self.source,
                "tags": self.tags,
                "status": self.status,
                "updated_at": self.updated_at,
                "purpose": self.purpose,
                "instruction_kind": self.instruction_kind,
                "exclusive_group": self.exclusive_group,
                "priority": self.priority,
                "confirmation": self.confirmation_signature,
            }
        )

    @property
    def active(self) -> bool:
        """検索や prompt へ反映する有効条件かどうかを返します。"""
        return self.status in RUNTIME_KNOWLEDGE_ACTIVE_STATUSES

    def labels(self) -> tuple[str, ...]:
        """照合に使う代表ラベルと別名を順序付きで返します。"""
        return _ordered_unique((self.title, *self.triggers))

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {
            "id": self.rule_id,
            "title": self.title,
            "triggers": list(self.triggers),
            "content": self.content,
            "purpose": self.purpose,
            "instruction_kind": self.instruction_kind,
            "exclusive_group": self.exclusive_group,
            "priority": self.priority,
            "version": self.version,
            "source": self.source,
            "tags": list(self.tags),
            "status": self.status,
            "updated_at": self.updated_at,
            "reviewed_at": self.reviewed_at,
            "next_review_at": self.next_review_at,
        }


@dataclass(frozen=True)
class RuntimeKnowledge:
    """runtime knowledge JSON から読み込んだ用語・同義語と回答ルールと読込状態を保持します。"""

    path: Path
    terms: tuple[RuntimeTerm, ...] = ()
    rules: tuple[RuntimeRule, ...] = ()
    loaded: bool = False
    # 読み込み・解析の失敗の例外の型の名前（例外の文は path などを含みうるので入れない。#3284）。
    error: str = ""
    assume_unconfirmed: bool = False
    unconfirmed_conditions: tuple[dict[str, Any], ...] = ()
    # 失敗の例外（回答の診断の degraded_steps に型の名前を残すため。表示・payload には出さない）。
    failure: BaseException | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class RuntimeKnowledgeContext:
    """一致項目・拡張後 query と読込件数を保持する。件数は一致上限を適用する前の登録数。"""

    original_question: str
    expanded_question: str
    path: Path
    matched_terms: tuple[RuntimeTerm, ...] = ()
    matched_rules: tuple[RuntimeRule, ...] = ()
    loaded: bool = False
    # 読み込みの失敗の例外の型の名前（#3284）。
    error: str = ""
    assume_unconfirmed: bool = False
    unconfirmed_conditions: tuple[dict[str, Any], ...] = ()
    # 登録の数（一致の上限を当てる前）。用語・同義語と回答ルールは別の工程で示すので種類ごとに持つ。
    registered_term_count: int = 0
    active_term_count: int = 0
    registered_rule_count: int = 0
    active_rule_count: int = 0
    expansion_decisions: tuple[dict[str, Any], ...] = ()
    rule_applications: tuple[dict[str, Any], ...] = ()
    # 利用者が確認の質問で選んだ条件と対象範囲(チャットの確認。#717)。
    scope_context: str = ""
    # 業務ガイドの手順ごとの根拠(#2395)。(手順 id, 題名, 根拠の chunk_uid の列) の列。手順ごとの検索の後に入る。
    guide_steps: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    # 読み込みの失敗の例外（`RuntimeKnowledge.failure`。表示・payload には出さない）。
    failure: BaseException | None = field(default=None, repr=False, compare=False)
    # 業務ガイドの手順のうち、回答の充足で必須に数えない手順の id(条件が決まらない分岐の行き先とその後続。#2491)。
    guide_optional_steps: tuple[str, ...] = ()
    # 業務ガイドの手順ごとの検索の手がかり(手順 id, 手がかりの列)。共有の根拠の item を手順に対応付ける語(#2649)。
    guide_step_terms: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # 業務ガイドの手順のうち、手順ごとの検索を期限・回数の上限で打ち切って検索しなかった手順の id(#2706)。
    guide_unsearched_steps: tuple[str, ...] = ()
    guide_step_contracts: tuple[dict[str, Any], ...] = ()
    # 分岐を確定したガイドの回答範囲。検索の成否や引用の支持状態から推定しない。
    guide_task_scope: tuple[dict[str, Any], ...] = ()
    # 必須範囲の取得機会と原文候補。意味支持や全範囲の読了とは別の状態。
    guide_acquisition: tuple[dict[str, Any], ...] = ()
    guide_acquisition_notices: tuple[str, ...] = ()
    # 業務ガイドの題名と案内の行（窓口・引き継ぐ条件・承認。#2792）。本文の「業務ガイドの案内」の節に資料とは分けて出す。
    guide_title: str = ""
    guide_notices: tuple[str, ...] = ()
    # guide_notices のうち、分かっている条件で選んだ分岐の補足（#3769）。あれば「業務ガイドの案内」の節を要約の直後に置く。
    guide_condition_notices: tuple[str, ...] = ()
    # 確認の質問で利用者が選んだ条件のうち、検索文に足した語(with_scope)。出どころの表示に使う。
    scope_search_terms: tuple[str, ...] = ()

    def with_scope(self, context: str, search_terms: Sequence[str] = ()) -> RuntimeKnowledgeContext:
        """利用者が選んだ条件を足す。検索に足す語は補助の検索文に加える。"""
        context = str(context or "").strip()
        terms = [term.strip() for term in search_terms if term and term.strip()]
        if not context and not terms:
            return self
        expanded = self.expanded_question + ("\n" + " ".join(terms) if terms else "")
        return replace(
            self,
            expanded_question=expanded,
            scope_context=context,
            scope_search_terms=(*self.scope_search_terms, *terms),
        )

    @property
    def registered_count(self) -> int:
        """用語・同義語と回答ルールの登録の合計。"""
        return self.registered_term_count + self.registered_rule_count

    @property
    def active_count(self) -> int:
        """用語・同義語と回答ルールの有効な登録の合計。"""
        return self.active_term_count + self.active_rule_count

    @property
    def has_matches(self) -> bool:
        """質問に一致した用語またはルールがあるかを返します。"""
        return bool(self.matched_terms or self.matched_rules or self.scope_context or self.assume_unconfirmed)

    @property
    def is_active(self) -> bool:
        """UI に表示すべき runtime knowledge 状態かを返します。"""
        return self.has_matches or bool(self.error) or bool(self.rule_applications)

    @property
    def expansion_sources(self) -> tuple[str, ...]:
        """原質問に語を足した出どころの表示名（用語・同義語 / 回答ルール / 業務ガイド / 確認で選んだ条件）。

        実際に語を足したものだけを、足した順に返す。足していなければ空。
        """
        sources = _ordered_unique(
            source for _, source in _expansion_additions(self.original_question, self.matched_terms, self.rules_for_stage("generation"))
        )
        if self.scope_search_terms:
            sources.append(SCOPE_SOURCE_LABEL)
        return tuple(sources)

    @property
    def query_source(self) -> str:
        """原質問に語を足した検索文の出どころ（例: 「原質問 + 用語・同義語」）。足していなければ「原質問のみ」。"""
        if self.expanded_question == self.original_question:
            return "原質問のみ"
        return " + ".join((ORIGINAL_QUESTION_SOURCE_LABEL, *self.expansion_sources))

    def rules_for_stage(self, stage: str) -> tuple[RuntimeRule, ...]:
        """段階ごとの採用結果。古い呼出元が組み立てた文脈では用途だけで区分する。"""
        if not self.rule_applications:
            return tuple(rule for rule in self.matched_rules if rule.purpose in (stage, "both"))
        accepted = {item["id"] for item in self.rule_applications if item["stage"] == stage and item["accepted"]}
        return tuple(rule for rule in self.matched_rules if rule.rule_id in accepted)

    def prompt_context(self) -> str:
        """LLM prompt に追加する補助 context 文字列を返します。"""
        if not self.has_matches:
            return ""
        generation_rules = [rule for rule in self.rules_for_stage("generation") if rule.content]
        lines = [] if not (self.matched_terms or generation_rules) else ["[Runtime Glossary / Rules]"]
        if self.matched_terms:
            lines.append("Glossary:")
            for term in self.matched_terms:
                label = term.term
                if term.aliases:
                    label += f" (aliases: {', '.join(term.aliases)})"
                detail = f": {term.description}" if term.description else ""
                source = f" [{term.source}]" if term.source else ""
                lines.append(f"- {label}{detail}{source}")
        if generation_rules:
            lines.append("Rules:")
            for rule in generation_rules:
                label = rule.title or rule.rule_id
                triggers = f" (triggers: {', '.join(rule.triggers)})" if rule.triggers else ""
                source = f" [{rule.source}]" if rule.source else ""
                lines.append(f"- {label}{triggers} [{rule.instruction_kind}; id={rule.rule_id}]: {rule.content}{source}")
        if self.scope_context:
            lines.extend(
                [
                    "[利用者が確認の質問で選んだ条件]",
                    self.scope_context,
                    "回答はこの条件に沿って作る。対象範囲が示されていれば、その範囲の資料だけを根拠にし、"
                    "範囲の資料に記載が無ければ、その範囲には記載が見つからないと答える（範囲の外から補わない）。",
                ]
            )
        if self.assume_unconfirmed:
            lines.extend(
                [
                    "未回答の確認事項（未確定の条件。質問と選択肢のデータ）:",
                    json.dumps(list(self.unconfirmed_conditions), ensure_ascii=False),
                ]
            )
        return "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {
            "path": str(self.path),
            "loaded": self.loaded,
            "error": self.error,
            "query_source": self.query_source,
            "expanded_question": self.expanded_question,
            "matched_terms": [term.to_payload() for term in self.matched_terms],
            "matched_rules": [rule.to_payload() for rule in self.matched_rules],
            "registered_count": self.registered_count,
            "active_count": self.active_count,
            "expansion_decisions": list(self.expansion_decisions),
            "rule_applications": [
                {**item, "version": next((rule.version for rule in self.matched_rules if rule.rule_id == item["id"]), item["version"])}
                for item in self.rule_applications
            ],
            "scope_context": self.scope_context,
            "assume_unconfirmed": self.assume_unconfirmed,
            "unconfirmed_conditions": list(self.unconfirmed_conditions),
            "guide_acquisition": list(self.guide_acquisition),
        }


def runtime_knowledge_path(output_dir: str | Path, configured_path: str | Path | None = None) -> Path:
    """runtime knowledge JSON の実効保存パスを決定します。"""
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(output_dir) / RUNTIME_KNOWLEDGE_FILE_NAME


def load_runtime_knowledge(
    output_dir: str | Path,
    configured_path: str | Path | None = None,
) -> RuntimeKnowledge:
    """runtime glossary/rules JSON を読み込み、壊れた内容は error として保持します。"""
    path = runtime_knowledge_path(output_dir, configured_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RuntimeKnowledge(path=path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _load_failure(path, exc)

    try:
        terms = tuple(_parse_terms(payload)[:MAX_TERMS])
        rules = tuple(_parse_rules(payload)[:MAX_RULES])
    except Exception as exc:
        return _load_failure(path, exc)
    pending = (
        tuple(
            {
                "condition_id": _clean_text(item.get("condition_id")),
                "question": _clean_text(item.get("question")),
                "options": _text_list(item.get("options"))[:MAX_ALIASES_PER_TERM],
            }
            for item in (payload.get("unconfirmed_conditions") or [])
            if isinstance(item, dict) and _clean_text(item.get("question"))
        )
        if isinstance(payload, dict) and isinstance(payload.get("unconfirmed_conditions", []), list)
        else ()
    )
    return RuntimeKnowledge(
        path=path,
        terms=terms,
        rules=rules,
        loaded=True,
        assume_unconfirmed=isinstance(payload, dict) and payload.get("assume_unconfirmed") is True,
        unconfirmed_conditions=pending,
    )


def _load_failure(path: Path, exc: BaseException) -> RuntimeKnowledge:
    """読めない用語・同義語と回答ルール。記録には例外の型の名前だけを残し、例外の文はログだけに出す（#3284）。"""
    logger.warning("runtime knowledge load failed (%s)", type(exc).__name__, exc_info=True)
    return RuntimeKnowledge(path=path, loaded=False, error=type(exc).__name__, failure=exc)


def build_runtime_knowledge_context(
    question: str,
    output_dir: str | Path,
    configured_path: str | Path | None = None,
    *,
    term_limit: int = MAX_MATCHED_TERMS,
    rule_limit: int = MAX_MATCHED_RULES,
) -> RuntimeKnowledgeContext:
    """質問に一致した用語・同義語と回答ルールを検索 query と prompt context 用に抽出します。"""
    original_question = _normalize(question)
    knowledge = load_runtime_knowledge(output_dir, configured_path)
    if knowledge.error:
        return RuntimeKnowledgeContext(
            original_question=original_question,
            expanded_question=original_question,
            path=knowledge.path,
            loaded=knowledge.loaded,
            error=knowledge.error,
            failure=knowledge.failure,
        )

    matched_terms = _matched_terms(original_question, knowledge.terms, term_limit)
    rule_matches = rank_rules(question, knowledge.rules, MAX_RULES, tokens=_tokens)
    applications = select_rule_applications(rule_matches.selected, rule_limit)
    matched_rules = applications.selected
    generation_ids = {item["id"] for item in applications.decisions if item["stage"] == "generation" and item["accepted"]}
    expanded_question = _expanded_question(
        original_question, matched_terms, [rule for rule in matched_rules if rule.rule_id in generation_ids]
    )
    decisions = []
    for kind, items, selected in (("term", knowledge.terms, matched_terms),):
        for item in items:
            if not item.active:
                continue
            matches = [label for label in item.labels() if _matches(original_question, label)]
            weak = _shared_token_count(original_question, item.description if kind == "term" else item.content)
            if matches or weak:
                decisions.append(
                    {
                        "source": kind,
                        "key": item.term if kind == "term" else item.rule_id,
                        "matched_labels": matches,
                        "accepted": item in selected,
                        "reason": "explicit_label" if item in selected else "limit" if matches else "shared_words_only",
                    }
                )
    generation_decisions = {item["id"]: item for item in applications.decisions if item["stage"] == "generation"}
    rule_decisions = []
    for item in rule_matches.decisions:
        application = generation_decisions.get(item["key"])
        if application is None:
            rule_decisions.append(item)
        else:
            rule_decisions.append(
                {
                    **item,
                    "matching_eligible": item["accepted"],
                    "match_reason": item["reason"],
                    "accepted": application["accepted"],
                    "selected": application["accepted"],
                    "reason": item["reason"] if application["accepted"] else application["reason"],
                }
            )
    return RuntimeKnowledgeContext(
        original_question=original_question,
        expanded_question=expanded_question,
        path=knowledge.path,
        matched_terms=tuple(matched_terms),
        matched_rules=tuple(matched_rules),
        loaded=knowledge.loaded,
        registered_term_count=len(knowledge.terms),
        active_term_count=sum(term.active for term in knowledge.terms),
        registered_rule_count=len(knowledge.rules),
        active_rule_count=sum(rule.active for rule in knowledge.rules),
        assume_unconfirmed=knowledge.assume_unconfirmed,
        unconfirmed_conditions=knowledge.unconfirmed_conditions,
        expansion_decisions=(*decisions, *rule_decisions),
        rule_applications=applications.decisions,
    )


def runtime_terms_status(context: RuntimeKnowledgeContext) -> str:
    """工程「用語・同義語の確認」の結果。未登録・無効・一致なし・読込失敗を区別する。"""
    if context.error:
        return f"読込エラー: {context.error}（補助情報なしで続行）"
    if context.matched_terms:
        return "一致: " + " / ".join(term.term for term in context.matched_terms)
    return _unmatched_status(context.loaded, context.registered_term_count, context.active_term_count, "用語・同義語")


def runtime_rules_status(context: RuntimeKnowledgeContext) -> str:
    """工程「回答ルールの確認」の結果。未登録・無効・一致なし・読込失敗を区別する。"""
    if context.error:
        return f"読込エラー: {context.error}（補助情報なしで続行）"
    if any(item["reason"] == "unresolved_exclusive_conflict" for item in context.rule_applications):
        return "設定の競合があるルールを保留し、両立するルールだけを適用します"
    if context.matched_rules:
        return "一致: " + " / ".join(rule.title or rule.rule_id for rule in context.matched_rules)
    return _unmatched_status(context.loaded, context.registered_rule_count, context.active_rule_count, "回答ルール")


def _unmatched_status(loaded: bool, registered: int, active: int, tab: str) -> str:
    # 登録先は検索・回答プロファイルの「プロファイルの知識」のタブ（用語・同義語、回答ルール）。
    if not loaded or not registered:
        return f"未登録：検索・回答プロファイルの「プロファイルの知識」の「{tab}」で登録できます"
    if not active:
        return f"有効な登録なし：検索・回答プロファイルの「プロファイルの知識」の「{tab}」で各登録の「回答で使う」を確認してください"
    return f"一致なし：登録済みの有効な{tab}に一致しませんでした"


def format_runtime_knowledge_context(context: RuntimeKnowledgeContext) -> list[str]:
    """runtime knowledge の読込状態と一致内容を UI 表示行へ整形します。"""
    lines = [f"パス: {context.path}"]
    if context.error:
        lines.append(f"エラー: {context.error}")
        return lines
    if not context.loaded:
        lines.append("読込状態: 未読込")
        return lines
    lines.append("読込状態: 読込済み")
    lines.append(f"検索質問ソース: {context.query_source}")
    if context.matched_terms:
        lines.append("用語: " + " / ".join(term.term for term in context.matched_terms))
    if context.matched_rules:
        lines.append("ルール: " + " / ".join(rule.title or rule.rule_id for rule in context.matched_rules))
    if not context.has_matches:
        lines.append("一致: なし")
    return lines


def runtime_retrieval_queries(
    original_question: str,
    expanded_question: str,
    retrieval_queries: Sequence[str],
) -> tuple[str, ...]:
    """runtime knowledge 拡張後の質問を retrieval query 列へ統合します。"""
    return tuple(_ordered_unique((original_question, expanded_question, *retrieval_queries)))


def parse_runtime_terms(payload: Any) -> list[RuntimeTerm]:
    """runtime knowledge の payload の用語（別名つき。上限 ``MAX_TERMS``）を返します。"""
    return _parse_terms(payload)[:MAX_TERMS]


def label_matches(text: str, label: str) -> bool:
    """用語・別名が質問に出ているか（英数字の語は語の途中に当てない。検索文の拡張と同じ判定）。"""
    return _matches(text, label)


def _parse_terms(payload: Any) -> list[RuntimeTerm]:
    raw_terms = _items(payload, "terms", "glossary", "glossaries")
    terms: list[RuntimeTerm] = []
    for item in raw_terms:
        if isinstance(item, str):
            term = _clean_text(item)
            if term:
                terms.append(RuntimeTerm(term=term))
            continue
        if not isinstance(item, dict):
            continue
        term = _first_text(item, "term", "name", "canonical", "formal", "label")
        aliases = _text_list(
            item.get("aliases") or item.get("synonyms") or item.get("abbreviations") or item.get("abbrev") or item.get("alias")
        )[:MAX_ALIASES_PER_TERM]
        description = _first_text(item, "description", "definition", "body", "note")
        source = _first_text(item, "source", "document", "path")
        tags = _text_list(item.get("tags"))
        status = _first_text(item, "status", "lifecycle_status", max_chars=80)
        updated_at = _first_text(item, "updated_at", max_chars=80)
        reviewed_at = _first_text(item, "reviewed_at", max_chars=80)
        next_review_at = _first_text(item, "next_review_at", max_chars=80)
        if term:
            terms.append(
                RuntimeTerm(
                    term=term,
                    aliases=tuple(aliases),
                    description=description,
                    source=source,
                    tags=tuple(tags),
                    status=status,
                    updated_at=updated_at,
                    reviewed_at=reviewed_at,
                    next_review_at=next_review_at,
                )
            )
    return _dedupe_terms(terms)


def _parse_rules(payload: Any) -> list[RuntimeRule]:
    raw_rules = _items(payload, "rules", "rulebook", "rulebooks")
    rules: list[RuntimeRule] = []
    for index, item in enumerate(raw_rules, start=1):
        if isinstance(item, str):
            content = _clean_text(item, MAX_TEXT_CHARS)
            if content:
                rules.append(RuntimeRule(rule_id=f"rule-{index}", title=f"rule-{index}", content=content))
            continue
        if not isinstance(item, dict):
            continue
        rule_id = _first_text(item, "id", "rule_id", "key") or f"rule-{index}"
        title = _first_text(item, "title", "name", "label") or rule_id
        triggers = _text_list(item.get("triggers") or item.get("terms") or item.get("keywords") or item.get("aliases") or item.get("when"))[
            :MAX_TRIGGERS_PER_RULE
        ]
        tags = _text_list(item.get("tags"))
        # 業務ガイドのルールはシステムが組み立てる文で、長さはガイドの項目の上限で決まる。利用者が登録する
        # ルールの上限（MAX_TEXT_CHARS）で切ると、末尾の完了・引き継ぐ条件・制約の行が落ちるので切らない（#3838）。
        content_limit = None if SUPPORT_GUIDE_RULE_TAG in tags else MAX_TEXT_CHARS
        content = _first_text(item, "content", "body", "text", "rule", max_chars=content_limit)
        source = _first_text(item, "source", "document", "path")
        status = _first_text(item, "status", "lifecycle_status", max_chars=80)
        updated_at = _first_text(item, "updated_at", max_chars=80)
        reviewed_at = _first_text(item, "reviewed_at", max_chars=80)
        next_review_at = _first_text(item, "next_review_at", max_chars=80)
        if content or title:
            rules.append(
                RuntimeRule(
                    rule_id=rule_id,
                    title=title,
                    triggers=tuple(triggers),
                    content=content,
                    purpose=str(item.get("purpose", "both")),
                    instruction_kind=str(item.get("instruction_kind", "requirement")),
                    exclusive_group=_clean_text(item.get("exclusive_group"), 160),
                    priority=item.get("priority", 0) if type(item.get("priority", 0)) is int else 0,
                    confirmation_signature=json.dumps(item["clarification"], ensure_ascii=False, sort_keys=True)
                    if isinstance(item.get("clarification"), dict)
                    else "",
                    source=source,
                    tags=tuple(tags),
                    status=status,
                    updated_at=updated_at,
                    reviewed_at=reviewed_at,
                    next_review_at=next_review_at,
                )
            )
    return _dedupe_rules(rules)


def _items(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _matched_terms(question: str, terms: Sequence[RuntimeTerm], limit: int) -> list[RuntimeTerm]:
    scored: list[tuple[int, int, RuntimeTerm]] = []
    for index, term in enumerate(terms):
        if not term.active:
            continue
        score = 0
        for label in term.labels():
            if _matches(question, label):
                score += 3 if label == term.term else 2
        if score and term.description and _shared_token_count(question, term.description):
            score += 1
        if score:
            scored.append((score, index, term))
    return [term for _, _, term in sorted(scored, key=lambda item: (-item[0], item[1]))[: max(1, limit)]]


# 呼び出し元が質問に合うと決めて渡したルール（業務ガイド。#1238）。語の一致に関係なく必ず使う。
PINNED_RULE_TAG = "pinned"
# 業務ガイドから作ったルールの印（pinned と一緒に付く）。
SUPPORT_GUIDE_RULE_TAG = "support_guide"


def _matched_rules(question: str, rules: Sequence[RuntimeRule], limit: int) -> list[RuntimeRule]:
    return list(rank_rules(question, rules, limit, tokens=_tokens).selected)


def _expansion_additions(
    question: str,
    matched_terms: Sequence[RuntimeTerm],
    matched_rules: Sequence[RuntimeRule],
) -> list[tuple[str, str]]:
    """原質問に足す語と、その出どころの表示名の組（重複は先に足した方だけ）。"""
    additions: list[tuple[str, str]] = []
    for term in matched_terms:
        additions.extend((label, TERMS_SOURCE_LABEL) for label in term.labels())
    for rule in matched_rules:
        if PINNED_RULE_TAG in rule.tags:
            # 業務ガイドの trigger は手順の検索の手がかり。検索文に足す（#1238）。
            source = GUIDE_SOURCE_LABEL if SUPPORT_GUIDE_RULE_TAG in rule.tags else RULES_SOURCE_LABEL
            additions.extend((trigger, source) for trigger in rule.triggers)
            continue
        # trigger は別名ではない。ある条件の一致だけで他条件や業務IDを検索へ流さない。
        additions.extend((label, RULES_SOURCE_LABEL) for label in rule.labels() if _matches(question, label))
        if any(_matches(rule.title, label) for label in rule.labels() if _matches(question, label)) and not re.search(
            r"[A-Za-z]{2,}[\w-]*\d", rule.title
        ):
            additions.append((rule.title, RULES_SOURCE_LABEL))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for word, source in additions:
        text = _clean_text(word)
        key = _key(text)
        if not text or key in seen:
            continue
        out.append((text, source))
        seen.add(key)
    return out


def _expanded_question(
    question: str,
    matched_terms: Sequence[RuntimeTerm],
    matched_rules: Sequence[RuntimeRule],
) -> str:
    additions = [word for word, _ in _expansion_additions(question, matched_terms, matched_rules)]
    if not additions:
        return question
    return f"{question}\n" + " ".join(additions)


def _shared_token_count(left: str, right: str) -> int:
    left_tokens = set(_tokens(left))
    if not left_tokens:
        return 0
    return len(left_tokens & set(_tokens(right)))


def _tokens(value: str) -> list[str]:
    return re.findall(r"[0-9a-zA-Z_]{2,}|[ぁ-んァ-ン一-龯々ー]{2,}", _normalize(value))


def _matches(text: str, label: str) -> bool:
    normalized_text = _key(text)
    normalized_label = _key(label)
    if not normalized_text or not normalized_label:
        return False
    if re.fullmatch(r"[0-9a-z_]+", normalized_label):
        pattern = rf"(?<![0-9a-z_]){re.escape(normalized_label)}(?![0-9a-z_])"
        return re.search(pattern, normalized_text) is not None
    return normalized_label in normalized_text


def _first_text(payload: dict[str, Any], *keys: str, max_chars: int | None = MAX_TEXT_CHARS) -> str:
    for key in keys:
        value = _clean_text(payload.get(key), max_chars)
        if value:
            return value
    return ""


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = re.split(r"[,、/\n]", value)
    elif isinstance(value, Sequence):
        raw = value
    else:
        raw = []
    return _ordered_unique(_clean_text(item) for item in raw)


def _dedupe_terms(terms: Sequence[RuntimeTerm]) -> list[RuntimeTerm]:
    out: list[RuntimeTerm] = []
    seen: set[str] = set()
    for term in terms:
        key = _key(term.term)
        if not key or key in seen:
            continue
        out.append(term)
        seen.add(key)
    return out


def _dedupe_rules(rules: Sequence[RuntimeRule]) -> list[RuntimeRule]:
    out: list[RuntimeRule] = []
    seen: set[str] = set()
    for rule in rules:
        key = _key(rule.rule_id or rule.title)
        if not key or key in seen:
            continue
        out.append(rule)
        seen.add(key)
    return out


def _ordered_unique(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = _key(text)
        if not text or key in seen:
            continue
        out.append(text)
        seen.add(key)
    return out


def _clean_text(value: Any, max_chars: int | None = 160) -> str:
    """正規化して空白を畳み、``max_chars`` で切る（None は切らない）。"""
    text = _normalize(str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _key(value: Any) -> str:
    return normalize_text(str(value or ""))
