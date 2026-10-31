"""検索戦略の選択と検索文生成のプロンプト、生成された検索文の正規化。LLM は呼ばない。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from rag_engine.config import Settings
from rag_engine.generation.answer_images import _trim_generated_text
from rag_engine.generation.answer_models import (
    HYDE_LABEL,
    HYDE_STRATEGY,
    QUERY_DECOMPOSITION_LABEL,
    QUERY_DECOMPOSITION_STRATEGY,
    RAG_FUSION_LABEL,
    RAG_FUSION_STRATEGY,
    SIMPLE_RETRIEVAL_LABEL,
    SIMPLE_RETRIEVAL_STRATEGY,
    STEP_BACK_PROMPTING_LABEL,
    STEP_BACK_PROMPTING_STRATEGY,
    QueryExpansionResult,
    _dedupe_queries,
    _dedupe_query_key,
    query_strategy_id,
    query_strategy_label,
)
from rag_engine.generation.crag_support import MAX_GENERATED_QUERY_CHARS
from rag_engine.knowledge.classification import ClassificationFilter
from rag_engine.knowledge.prompt_files import render_prompt_template
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext
from rag_engine.resources.runtime import resolve_prompt
from rag_engine.retrieval.inquiry_conditions import InquiryConditionParse
from rag_engine.retrieval.task_contract import task_contract

MAX_GENERATED_QUERY_COUNT = 5


def _text_search_query_source(
    runtime_knowledge: RuntimeKnowledgeContext,
    expansion: QueryExpansionResult,
    *,
    crag_rewrites: Sequence[str] = (),
) -> str:
    parts = ["原質問"]
    if runtime_knowledge.expanded_question != runtime_knowledge.original_question:
        parts.extend(runtime_knowledge.expansion_sources)
    if expansion.generated_queries:
        parts.append("拡張検索文")
    if crag_rewrites:
        parts.append("CRAG 再検索文")
    return " + ".join(parts)


def _retrieval_metadata_filter(
    inquiry_conditions: InquiryConditionParse | None,
    classification_filter: ClassificationFilter,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    classification_payload = classification_filter.to_metadata()
    if classification_payload:
        payload["classification"] = classification_payload
    if inquiry_conditions is not None:
        inquiry_filter = inquiry_conditions.metadata_filter.to_payload()
        if inquiry_filter.get("active"):
            payload["inquiry"] = inquiry_filter
    return payload


def _limited_text_search_query_variants(
    question: str,
    query_variants: Sequence[str] | None,
    settings: Settings,
) -> tuple[str, ...]:
    raw_variants = query_variants if query_variants is not None else (question,)
    variants = _retrieval_queries(question, raw_variants)
    limit = max(1, int(getattr(settings, "text_search_query_variant_limit", 6) or 6))
    return tuple(variants[:limit])


def _query_expansion_details(expansion: QueryExpansionResult, *, original_query_weighting: bool = True) -> list[str]:
    """使った戦略と理由を示す。検索文そのものは、最終の一覧を示す次の工程に任せる。"""
    details = [
        f"結果: {query_strategy_label(expansion.effective_strategy)}"
        + (f"（{len(expansion.generated_queries)} 本の検索文を追加）" if expansion.generated_queries else "（検索文の追加なし）")
    ]
    if expansion.routing_reason:
        details.append(f"      理由: {expansion.routing_reason}")
    if expansion.routing_data_required:
        details.append("      実データの確認が必要になりそうな点（暫定。回答時に根拠を見て判断し直します）:")
        details.extend(f"      ・{item}" for item in expansion.routing_data_items)
    if expansion.generated_queries:
        details.append(
            "影響: 追加した検索文は補助として使います。"
            + (
                "順位の融合では原質問の検索結果を優先します。"
                if original_query_weighting
                else "順位の融合では全検索文を同じ重みで扱います（RETRIEVAL_ORIGINAL_QUERY_WEIGHTING=false）。"
            )
        )
    if expansion.vector_only_queries:
        details.append("      仮説文は創作した語を含むため、意味検索（vector）だけに使い、全文検索（Oracle Text）には使いません。")
    return details


# 検索文生成のプロンプトへ渡す契約の項目。原質問の写し（original_question / current_request / request_units /
# user_assertions）はプロンプトの「原質問」と重複し、intent_parts / required_aspects は回答生成・監査用なので渡さない (#917)。
_PROMPT_CONTRACT_KEYS = (
    "asks_cause",
    "requested_actions",
    "action_targets",
    "business_objects",
    "error_messages",
    "output_formats",
    "changed_fields",
    "explicit_conditions",
    "hypotheses",
    "requester_mentions",
    "missing_choice",
)


def _prompt_task_contract(question: str) -> str:
    """検索文生成に必要な契約項目だけを JSON にします。空の項目は省き、定義項目はラベルだけにします。

    「以前…が、今回…」のように経緯と今回の要求が分かれる質問では、どちらが今回の要求かを示す
    historical_context / current_request も渡します。
    """
    contract = task_contract(question)
    selected = {key: contract[key] for key in _PROMPT_CONTRACT_KEYS if contract.get(key)}
    labels = [target["label"] for target in contract["definition_targets"]]
    if labels:
        selected["definition_targets"] = labels
    if contract["historical_context"]:
        selected["historical_context"] = contract["historical_context"]
        selected["current_request"] = contract["current_request"]
    return json.dumps(selected, ensure_ascii=False)


_EXPANSION_INSTRUCTIONS = {
    RAG_FUSION_STRATEGY: (
        "RAGフュージョンとして、複数検索質問用の検索質問を3〜5個作ってください。"
        "利用者語、正式な画面語、項目名、表記ゆれ、同義語、具体/抽象の粒度違いを混ぜます。"
    ),
    QUERY_DECOMPOSITION_STRATEGY: (
        "質問分解として、問い合わせを独立して検索できる子質問に2〜5個へ分解してください。各子質問は単独で意味が通る形にし、"
        "原質問の判断に効く条件（対象の属性・年齢・期間・資格や権利の有無・状態など）を落とさず含めます。"
    ),
    STEP_BACK_PROMPTING_STRATEGY: (
        "ステップバックプロンプトとして、原質問の前提・上位概念（生成条件・除外条件・設定の適用規則）を探す抽象化した質問を1〜3個作ってください。"
        "帳票名・項目番号・エラーコード・対象年月は保持し、別帳票や別制度へ一般化しすぎず、個案の原因や未提供データの値を仮定しないでください。"
    ),
    HYDE_STRATEGY: (
        "仮説文生成（HyDE）として、文書内に存在しそうな仮想回答文または説明文を1〜3個作ってください。"
        "ADB hybrid search の検索入力として使える、短い自然文にします。仮想文は検索の補助であり、回答の事実根拠にはしません。"
    ),
}

_EXPANSION_CONSTRAINTS = (
    "- 原質問の意図から外れない。\n"
    "- 標準回答や文書にない実施済み事実を作らない。\n"
    "- 日本語の問い合わせ検索に使いやすい短い文にする。\n"
    "- 原質問そのものは返さない。\n"
)

# ルーティングの選択肢。strategy は構造化出力の schema と同じ戦略 ID で、label は利用者向けの表示名 (#919)。
_ROUTING_STRATEGIES = [
    {
        "strategy": SIMPLE_RETRIEVAL_STRATEGY,
        "label": SIMPLE_RETRIEVAL_LABEL,
        "use_when": "主要な語（画面名・項目名・エラーコード・完全なメッセージ）が文書の正式な用語のまま書かれ、論点が1つだけの場合。実データ確認が必要でも選べる。",
    },
    {
        "strategy": RAG_FUSION_STRATEGY,
        "label": RAG_FUSION_LABEL,
        "use_when": "利用者の言い回し・略称・日常語が混じり、表記ゆれ・同義語・文書の用語への言い換えが要りそうな場合。迷ったらこれを選ぶ。",
        "generate": _EXPANSION_INSTRUCTIONS[RAG_FUSION_STRATEGY],
    },
    {
        "strategy": QUERY_DECOMPOSITION_STRATEGY,
        "label": QUERY_DECOMPOSITION_LABEL,
        "use_when": "質問に複数の条件、複数の対象、複数のエラー文、理由と手順などが混在する場合。",
        "generate": _EXPANSION_INSTRUCTIONS[QUERY_DECOMPOSITION_STRATEGY],
    },
    {
        "strategy": STEP_BACK_PROMPTING_STRATEGY,
        "label": STEP_BACK_PROMPTING_LABEL,
        "use_when": "個別症状から生成条件・除外条件・適用規則などの上位概念を補助検索する必要がある場合。具体的な質問・実データ不足という理由だけでは選ばない。",
        "generate": _EXPANSION_INSTRUCTIONS[STEP_BACK_PROMPTING_STRATEGY],
    },
    {
        "strategy": HYDE_STRATEGY,
        "label": HYDE_LABEL,
        "use_when": "質問が短い、断片的、または文書にありそうな回答文から探す方がよい場合。",
        "generate": _EXPANSION_INSTRUCTIONS[HYDE_STRATEGY],
    },
]

# 静的な指示は Step 形式のテンプレートに置き、実行時の値は {{...}} で差し込む。UI はこの定数を読み取り専用で示す (#934)。
QUERY_ROUTING_PROMPT_TEMPLATE = (
    """## Step 1: 原質問と保持する条件を読む
原質問:
{{question}}

変更してはいけない目的・条件:
{{task_contract}}

## Step 2: 戦略を選び検索文を作る
次の問い合わせに対して、検索前処理として最適な戦略を1つだけ選んでください。strategy には選んだ選択肢の strategy の値（ID）をそのまま返し、label は返さない。

選択肢:
"""
    f"{json.dumps(_ROUTING_STRATEGIES, ensure_ascii=False, indent=2)}"
    """

## Step 3: 戦略と実データの要否を別々に点検する
- 戦略は選択肢の use_when で判断し、実データ確認とは独立に決める。エラーコードや完全なメッセージが1つだけなら単純検索、エラー文・対象・論点が2つ以上あれば質問分解。質問が明確・具体的というだけでは単純検索を選ばず、主要な語が文書の正式な用語と一致しているかで判断し、迷ったら RAG フュージョンを選ぶ。
- routing_data_required は、個案の原因・値・処理分岐の確定に実際の日付・設定・CSV・ログ・履歴などの照合が必要かの暫定判定。必要なら routing_data_items に確認するフィールドと目的を列挙し、値や原因は推測しない。操作方法・出力方法・エラーの意味・規則の説明、関連文書の不足、質問内に全必要値がある場合は false と空配列（検索後に回答生成で再判定する）。
- 単純検索以外では、その選択肢の generate の指示に従って検索文を queries に返す（単純検索では空配列）。
- 戦略とは別に、根拠が規定（法令・規程の条文）にありそうな質問のときだけ、根拠になる条文そのものを想定して、その条文の一文の形（「〜の場合において、〜は、〜とする。」「〜は、〜しなければならない。」のような規定の用語と文体）で書いた検索文を provision_queries に返す。質問の判断に効く論点（定義・要件・期限・効果）ごとに 1 つ、最大 3 つ。1 つの検索文に別の論点を混ぜず、その論点の要件の語（資格・権利・状態・期間など）を落とさない。「〜に関する条文」「〜の規定」のような条文を探す言い方にしない。規定に無関係な質問では空配列。
"""
    + _EXPANSION_CONSTRAINTS
    + (
        "- 利用者の仮説は事実に変えず、問い合わせ元を検索条件にしない。\n"
        "\n"
        "## Step 4: 指定した JSON だけを返す\n"
        "以下のJSONだけを返してください。JSON の外に Step 番号・説明・分析を書かないでください。\n"
        '{"strategy": "選んだ戦略の ID", "reason"'
        ': "検索戦略を選んだ短い理由", "routing_data_re'
        'quired": true|false, "routing_data'
        '_items": ["確認対象と目的（不要なら空配列）"], "qu'
        'eries": ["追加の検索質問または検索文"], "provis'
        'ion_queries": ["規定の書き方の検索文（無ければ空配列'
        '）"]}'
    )
)

QUERY_EXPANSION_PROMPT_TEMPLATE = (
    (
        "## Step 1: 原質問と保持する条件を読む\n"
        "原質問:\n{{question}}\n\n"
        "変更してはいけない目的・条件:\n{{task_contract}}\n\n"
        "## Step 2: 指定した戦略で検索文を作る\n"
        "戦略「{{strategy_label}}」の指示に従い、検索に使う"
        "追加質問または検索文を作ってください。一覧にない戦略の場合は、検索に"
        "有効な追加質問を作ってください。\n"
        "\n"
        "戦略ごとの指示:\n"
    )
    + "".join(f"- {query_strategy_label(strategy)}: {instruction}\n" for strategy, instruction in _EXPANSION_INSTRUCTIONS.items())
    + ("\n## Step 3: 意図・条件・仮説の扱いを点検する\n")
    + _EXPANSION_CONSTRAINTS
    + (
        "- 利用者の仮説は事実に変えず、問い合わせ元を検索条件にしない。\n"
        "\n"
        "## Step 4: 指定した JSON だけを返す\n"
        "以下のJSONだけを返してください。JSON の外に Step 番号・説明・分析を書かないでください。\n"
        '{"queries": ["追加の検索質問または検索文"]}'
    )
)


def _build_query_routing_prompt(question: str) -> str:
    """検索方法と実データ依存を混同しない二軸のルーティング指示を作成します。"""
    return render_prompt_template(
        resolve_prompt("routing.template", QUERY_ROUTING_PROMPT_TEMPLATE),
        {
            "question": question.strip(),
            "task_contract": _prompt_task_contract(question),
        },
    )


def _build_query_expansion_prompt(question: str, strategy: str) -> str:
    """選択済みの戦略で検索文を作る指示を作成します（自動ルーティングが検索文を返さないときの補助呼出にも使う）。"""
    return render_prompt_template(
        resolve_prompt("expansion.template", QUERY_EXPANSION_PROMPT_TEMPLATE),
        {
            "strategy_label": query_strategy_label(strategy),
            "question": question.strip(),
            "task_contract": _prompt_task_contract(question),
        },
    )


def _generated_queries_from_payload(
    payload: dict[str, Any] | None, original_question: str, *, limit: int | None = MAX_GENERATED_QUERY_COUNT
) -> tuple[str, ...]:
    if not payload:
        return ()

    candidates: list[str] = []
    for key in (
        "queries",
        "generated_queries",
        "questions",
        "sub_questions",
        "step_back_questions",
        "hypothetical_documents",
        "retrieval_texts",
        "items",
        "query",
        "question",
        "text",
    ):
        candidates.extend(_coerce_text_values(payload.get(key)))
    return _dedupe_generated_queries(original_question, candidates, limit=limit)


def _coerce_text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for key in (
            "query",
            "question",
            "sub_question",
            "step_back_question",
            "hypothetical_document",
            "retrieval_text",
            "text",
        ):
            values.extend(_coerce_text_values(value.get(key)))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_coerce_text_values(item))
        return values
    return []


def _dedupe_generated_queries(
    original_question: str, queries: Sequence[str], *, limit: int | None = MAX_GENERATED_QUERY_COUNT
) -> tuple[str, ...]:
    """原質問と重複を除き、先頭 limit 本を返します。limit=None は切り詰めません。"""
    original_key = _dedupe_query_key(original_question)
    seen = {original_key} if original_key else set()
    selected: list[str] = []
    for query in queries:
        text = _trim_generated_text(query, MAX_GENERATED_QUERY_CHARS)
        key = _dedupe_query_key(text)
        if not key or key in seen:
            continue
        selected.append(text)
        seen.add(key)
        if limit is not None and len(selected) >= limit:
            break
    return tuple(selected)


def _merge_retrieval_queries(*queries: str) -> tuple[str, ...]:
    """検索文を渡した順に並べ、空と重複を除く。

    各検索文は NFKC・制御文字の除去・空白の正規化をした形にする（元の質問の先読みの検索と文書検索の先頭の検索文を
    同じ形にし、検索の結果の cache に当てる）。
    """
    merged: list[str] = []
    seen: set[str] = set()
    for query in queries:
        text = _normalized_query_text(query)
        key = text.casefold()
        if not text or key in seen:
            continue
        merged.append(text)
        seen.add(key)
    return tuple(merged)


def _normalized_query_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = "".join(character for character in normalized if unicodedata.category(character) not in {"Cc", "Cf"})
    return re.sub(r"\s+", " ", normalized).strip()


def _retrieval_queries(question: str, retrieval_queries: Sequence[str] | None) -> tuple[str, ...]:
    if retrieval_queries is None:
        return (question,)
    queries = _dedupe_queries(retrieval_queries)
    if not queries:
        return (question,)
    if _dedupe_query_key(question) != _dedupe_query_key(queries[0]):
        return _dedupe_queries((question, *queries))
    return queries


def _base_query_expansion(question: str, query_strategy: str) -> QueryExpansionResult:
    selected_strategy = query_strategy_id(query_strategy)
    return QueryExpansionResult(
        original_question=question,
        selected_strategy=selected_strategy,
        effective_strategy=selected_strategy,
    )
