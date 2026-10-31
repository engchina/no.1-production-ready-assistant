"""外部 SDK に依存しない既存データ契約。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ModelWrapValidatorHandler, PrivateAttr, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema


@dataclass(frozen=True)
class RerankTextRank:
    """rerank 結果の元 document index と relevance score を保持します。

    question_relevance_score は原質問だけとの関連度（検索文ごとの最高値で並べたとき。#3504）。None なら
    relevance_score が原質問の関連度。
    """

    index: int
    relevance_score: float | None = None
    question_relevance_score: float | None = None


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConditionResultPairOutput(_StrictModel):
    """LLM が返す条件ごとの判定結果ペアを表します。"""

    condition: str
    result: str
    visible_text: str


class DiagramEdgeOutput(_StrictModel):
    """図中の接続元、接続先、条件ラベルを保持します。"""

    source: str
    target: str
    label: str


class ChartSeriesOutput(_StrictModel):
    """Chart の系列名、読み取れる値、傾向を保持します。"""

    name: str
    values: list[str]
    trend: str


# 画面の項目の種類（#2404）。見た目の手がかりで決まる固定の値で、言語資源にはしない（表示の語は言語資源の
# content_labels.form_field_kinds）。判断できなければ unknown。
FORM_FIELD_KINDS = ("select", "text", "checkbox", "radio", "date", "button", "unknown")
FormFieldKind = Literal["select", "text", "checkbox", "radio", "date", "button", "unknown"]


def form_field_kind(value: object) -> str:
    """項目の種類を決まった値にする（大文字・空白の揺れはそろえ、決まった値の外と欠けは unknown）。"""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in FORM_FIELD_KINDS else "unknown"


class FormFieldOutput(_StrictModel):
    """Form の項目名、表示値、選択状態、項目の種類を保持します。"""

    name: str
    value: str
    state: str
    kind: FormFieldKind

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, value: object) -> str:
        """構造化出力を強制しない接続先の値の揺れ（大文字・空白・決まった値の外）は unknown に倒す。"""
        return form_field_kind(value)


class PictureDescriptionOutput(_StrictModel):
    """Vision 説明で得た可視情報と検索補助 text を保持します。

    生成説明の本文にも回答生成にも使われない field は持ちません (#769)。
    """

    surrounding_context: str = ""
    correction_notes: str = ""
    main_topic: str
    visual_kind: str
    diagram_title: str
    diagram_nodes: list[str]
    diagram_edges: list[DiagramEdgeOutput]
    diagram_summary: str
    chart_title: str
    chart_type: str
    chart_axes: list[str]
    chart_legends: list[str]
    chart_series: list[ChartSeriesOutput]
    chart_summary: str
    table_headers: list[str]
    table_rows: list[list[str]]
    table_summary: str
    form_fields: list[FormFieldOutput]
    form_layout: str
    menu_route: str
    visible_screen_names: list[str]
    visible_buttons: list[str]
    visible_fields: list[str]
    visible_values: list[str]
    codes_and_errors: list[str]
    operation_steps: list[str]
    condition_result_pairs: list[ConditionResultPairOutput]
    exception_or_cautions: list[str]
    answerable_questions: list[str]
    query_rewrites: list[str]
    search_keywords: list[str]
    retrieval_text: str


class QueryRoutingOutput(_StrictModel):
    """検索戦略と独立した、検索前の暫定データ確認観点を保持します。

    routing_data_required の None は旧形式または未判定です。確認項目は実測値や
    最終的な不足判定ではなく、回答時に提供済み根拠と照合する対象です。
    """

    # 戦略は schema の enum で ID に制約する。OpenAI Responses と OCI Generative AI のどちらも strict schema を
    # 強制するため、LLM の出力がこの外に出ない。answer_models の定数を import すると循環するので文字列で書き、
    # answering._ROUTABLE_QUERY_STRATEGIES との一致はテストで固定する (#919)。
    strategy: Literal["simple_retrieval", "rag_fusion", "query_decomposition", "step_back_prompting", "hyde"]
    reason: str
    routing_data_required: bool | None = None
    routing_data_items: list[str] = []
    # 選んだ戦略の検索文。戦略選択と検索文生成を1回の呼出で行う。空なら従来の拡張呼出で補う。
    # 本数は後段の MAX_GENERATED_QUERY_COUNT で切り詰める。ここで上限を課すと超過時に回答全体が中断する。
    queries: list[str] = Field(default_factory=list)
    # 根拠が規定（法令・規程の条文）にありそうな質問で、その規定の書き方で根拠を言い表した検索文（論点ごとに 0〜3 本。#2148・#2163）。
    # 創作した語を含みうるので意味検索（vector）だけに使う。
    provision_queries: list[str] = Field(default_factory=list)


class QueryExpansionOutput(_StrictModel):
    """LLM が生成した検索 query バリエーションを保持します。"""

    queries: list[str]


# CRAG 評価の出力契約は Field(description) に置く。構造化出力の JSON schema としてモデルへ渡るため、prompt に
# 出力例を載せない (#983)。判定規則そのものは CRAG_GRADE_PROMPT_TEMPLATE にある。
class CragEvidenceQuote(_StrictModel):
    """観点を判断した候補と、その候補からの直接引用。"""

    source_id: str = Field(description="原文を読んだ候補の chunk_uid。")
    quote: str = Field(description="その候補から文字を変えずに写した直接の根拠。")


class CragAnswerablePart(_StrictModel):
    """原質問への部分回答に使える、直接裏付けた操作・規則・確認方法。"""

    aspect: str = Field(description="直接答えられる required_aspects の観点。business_object だけでは回答内容にしない。")
    kind: Literal["operation", "rule", "verification", "definition"] = Field(
        description="原質問に対する操作、規則・禁止、実値の確認方法、定義のいずれか。"
    )
    claim: str = Field(description="原文が直接裏付ける部分回答。業務の概説や機能名だけを操作・規則に言い換えない。")
    evidence: list[CragEvidenceQuote] = Field(default_factory=list, description="この部分回答を直接裏付ける候補のIDと原文。")


class CragAspectCheck(_StrictModel):
    """質問の必要観点ごとの支持と、候補内の一意な出典ID。"""

    aspect: str = Field(
        description="task_contract.required_aspects の観点名（business_object / requested_result / applicability / procedure など）。"
    )
    status: Literal["supported", "missing", "unknown", "data_confirmation"] = Field(
        description="supported=直接根拠で説明できる。missing=閲覧した候補に必要な説明が無い。unknown=解釈・範囲・対応を確定できない。data_confirmation=一般規則はあり実値の照合だけが残る（requested_result / applicability のみ）。"
    )
    source_ids: list[str] = Field(
        default_factory=list, description="観点を支持する候補の chunk_uid。supported / data_confirmation では必須。"
    )
    reason: str = Field(description="判定の根拠を短く。data_confirmation では閲覧するデータと目的。")
    evidence: list[CragEvidenceQuote] = Field(
        default_factory=list, description="支持・確認方法の直接引用。複数の候補を組み合わせてよい。項目名だけを定義の根拠にしない。"
    )


class CragCandidateVerdict(_StrictModel):
    """候補（parent chunk）ごとの関連判定。CRAG の評価器と同じく文書単位の二値判定を先に行う。"""

    chunk_uid: str = Field(description="候補の chunk_uid。")
    relevant: bool = Field(
        description="候補が原質問と同じ業務・機能の種類について、要求された操作・規則・確認方法・制限（できない・機能が無い）のいずれかを説明していれば true。質問固有の値が無くてもよい。別の変更項目だけの手順や明らかな話題違いは false。エラーは明確に別で共通処理関係が無い場合だけ irrelevant とし、対応不明は relation=unknown で保持する。"
    )
    reason: str = Field(description="判定の根拠を一文で。")
    relation: Literal["relevant", "irrelevant", "unknown"] | None = Field(
        default=None, description="回答に役立つかの意味判定。不確定は unknown として保持する。省略時は relevant の互換値。"
    )
    error_relation: Literal["same", "shared_handling", "different", "unknown", "not_applicable"] = Field(
        default="not_applicable",
        description="same=同じエラー。shared_handling=原文に共通の処理関係がある。different=明確に別のエラーで共通処理なし。unknown=対応を確定できない。not_applicable=エラーの質問ではない。別番号の出現だけで different にしない。",
    )
    quote: str = Field(default="", description="エラーの対応または共通処理関係を判断した候補の直接原文。")


class CragEnumeratedItem(_StrictModel):
    """列挙・網羅の問（task_contract.enumeration）で、候補に見える該当の項目 1 つと根拠（#1503）。

    件数・章の範囲の欠けはプログラムが数える（``quote`` が根拠の本文に無い項目は数えない）。
    """

    item: str = Field(description="該当の項目を短く（例: 入室の予約）。")
    source_ids: list[str] = Field(default_factory=list, description="項目が書かれた候補の chunk_uid。")
    quote: str = Field(description="候補の本文から、項目が書かれた箇所を文字を変えずに写した原文（20 字程度）。")


# 項目の順序は生成順でもある（strict schema）。候補ごとの判定 → 列挙の項目 → 観点 → 理由 → 結論の順に置き、結論を先に書かせない。
class CragRetrievalGradeOutput(_StrictModel):
    """CRAG retrieval の十分性判定と理由を保持します。"""

    candidate_verdicts: list[CragCandidateVerdict] = Field(
        default_factory=list, description="全候補の関連判定。候補ごとに独立に判定する。relevant=true の候補を回答に使う。"
    )
    enumerated_items: list[CragEnumeratedItem] = Field(
        default_factory=list,
        description="task_contract.enumeration があるときだけ、候補に見える該当の項目を、重ねずに資料の順で enumeration.max_items 件まで挙げる。候補に無い項目を推測で足さない。enumeration が null なら空の配列。",
    )
    aspect_checks: list[CragAspectCheck] = Field(
        default_factory=list, description="required_aspects ごとの判定。relevant な候補を合わせて確認し、全観点を必ず列挙する。"
    )
    answerable_parts: list[CragAnswerablePart] = Field(
        default_factory=list,
        description="原質問の操作・規則・確認方法・定義に直接答えられる部分。業務が同じという概説だけなら空。複数資料の共同支持も可。",
    )
    reason: str = Field(description="十分・不足の理由を短く。不足なら欠けている観点と、どの候補が最も近いかを書く。")
    sufficient: bool = Field(
        description="answerable_parts に直接根拠を示せる部分があるなら true。関連候補や業務概述だけでは true にしない。全観点の充足とは区別する。"
    )
    confidence: float = Field(description="判定の確からしさ（0.0〜1.0）。")
    rewritten_query: str = Field(
        description="missing 観点を補う短い日本語の検索文。部分回答できても不足があれば返す。unknown だけなら空文字。"
    )
    recovery_action: Literal["rewrite", "neighbor_children", "parent", "neighbor_parents"] = Field(
        default="rewrite",
        description="不足時の回復手段。rewrite=別の話題・対象を探す。neighbor_children / parent / neighbor_parents=recovery_options にある文脈拡張。",
    )
    recovery_anchor_ids: list[str] = Field(
        default_factory=list, description="文脈拡張の起点にする、閲覧した適合候補の chunk_uid。rewrite なら空。"
    )


class CitedEvidenceOutput(_StrictModel):
    """回答生成で実際に参照した証拠 evidence を表します。"""

    evidence_id: str
    source: str
    page: str
    look_at: str
    visible_evidence: str


class SectionExtractionQuote(_StrictModel):
    """長い章節の 1 回分から抜き出した原文 1 つ（#1437。抽出してから統合）。"""

    chunk_ref: str = Field(description="原文を取ったブロックの見出しの番号（[C1 …] の C1）をそのまま書く。")
    location: str = Field(description="ブロックの見出しにある場所（p.5、シート「…」A3:D6 など）をそのまま書く。無ければ空文字。")
    quote: str = Field(
        description="ブロックの本文の連続した一部を、文字・記号・数字・改行を変えずにそのまま写した原文。要約・言い換え・省略・補足をしない。"
    )


class SectionExtractionOutput(_StrictModel):
    """長い章節の 1 回分から、質問に関係する原文を抜き出した結果（#1437）。"""

    quotes: list[SectionExtractionQuote] = Field(
        default_factory=list, description="質問への回答に必要な原文を、資料の順に。関係する記載が無ければ空の配列。"
    )


class TextOutput(_StrictModel):
    """text-only LLM 呼び出しの構造化出力を表します。"""

    text: str


# 生成・監査の出力契約は Field(description) に置き、構造化出力の JSON schema としてモデルへ渡す (#986)。
# 項目の順序は strict schema の生成順でもあるため、根拠（request_coverage / items、reviews）→ 結論（summary /
# confidence、goal_alignment）の順に並べ、結論を先に書かせない。行動方針は GENERATE_SYSTEM_PROMPT / AUDIT_SYSTEM_PROMPT。
class GroundedCitation(_StrictModel):
    """item の出典 1 つ（根拠の番号と引用する行）。引用の原文はシステムが取り出す（#2830）。"""

    # モデル入力ではなく、本検証で毎回再作成する抽出の記録。旧 round の記録も信用しない。
    _extraction: dict[str, str] = PrivateAttr(default_factory=dict)
    # 生成用の整数範囲からの変換記録。None は保存済みの lines 形式で、モデルの出力には含めない。
    _wire_range: tuple[int | None, int | None] | None = PrivateAttr(default=None)

    evidence_id: str = Field(description="根拠の先頭にある [E1] 形式の番号（角括弧なし）。")
    lines: str = Field(
        default="",
        max_length=20,
        description="引用する行の範囲。行番号（`3| `）の付いた根拠では必ず、text を支える行の番号か範囲（例: 3、3-5）を書く。"
        "この出典が担う操作・値を含む行にする。複数の出典で text 全体を共同して支えてよい。"
        "行番号の無い根拠は空（根拠の全文が引用になる）。",
    )
    # 引用の原文。モデルには出力させず（応答の schema に出さない）、evidence_id と lines からシステムが取り出す（#2830）。
    quote: SkipJsonSchema[str] = Field(
        default="", max_length=600, description="引用の原文（システムが evidence_id と lines から取り出す。応答の schema には出さない）。"
    )


class GroundedItem(_StrictModel):
    """回答の1主張と、それを支える出典（連続した原文の引用）の対。

    gap 以外は出典が 1 つ以上必須。1 つの主張は、同じ内容を述べた別の文書の原文（規則の条項と、それを説明する
    手順書など）を出典に並べられる（#3526。ALCE・Anthropic Citations の形）。各出典は決定的に原文照合され、
    一致しない出典は公開されない。text は利用者向けの言い換えで、監査で不支持になると印が付く。

    検査の工程（``grounded.verify`` の後）では、1 つの item は出典を 1 つだけ持ち、``evidence_id`` / ``lines`` /
    ``quote`` はその出典を読む。同じ主張の別の出典は ``CheckedItem.also`` に持つ。
    """

    request_id: str = Field(
        default="", description="この item が答える request_units の id。背景（kind=context）の要求には item を作らない。"
    )
    kind: Literal["operation", "rule", "confirmation", "gap"] = Field(
        description="operation=利用者が行う操作（実施順）。rule=規則・可否・原因の説明。confirmation=照合・閲覧の案内。gap=資料から確認できない点（何が確認できないか、閲覧する資料・データと目的）。"
    )
    text: str = Field(
        min_length=1,
        max_length=600,
        description="出典の原文が支持する範囲だけを質問の言葉で説明する。原文にない画面名・ボタン・値・効果・原因・成功保証を足さない。",
    )
    citations: list[GroundedCitation] = Field(
        default_factory=list,
        description="text を支える出典。gap 以外は 1 つ以上必須、gap は空。指定した出典の原文の集合で text 全体を支える。"
        "異なる出典が操作・対象・条件・順序の別々の部分を共同して支えてよく、各出典が text 全体を単独で支える必要はない。"
        "実際に指定していない原文で不足を補わない。"
        "同じ文書の同じ原文を重ねない。",
    )
    applies: Literal["matched", "conditional", "unverified"] = Field(
        default="matched",
        description="matched=通常。conditional=原文に適用条件・限定が明記され、質問からその成立を確認できない。unverified はシステムが付ける値で出力しない。",
    )
    condition: str = Field(default="", description="applies=conditional のとき、原文の条件の語句をそのまま写す。原文にない前提は作らない。")

    @property
    def evidence_id(self) -> str:
        """最初の出典の根拠の番号（出典が無ければ空）。"""
        return self.citations[0].evidence_id if self.citations else ""

    @property
    def lines(self) -> str:
        """最初の出典の引用する行の範囲。"""
        return self.citations[0].lines if self.citations else ""

    @property
    def quote(self) -> str:
        """最初の出典の引用の原文。"""
        return self.citations[0].quote if self.citations else ""

    def with_citation(self, **update: str) -> GroundedItem:
        """最初の出典を ``update``（evidence_id / lines / quote）で置き換えた写し。出典が無ければ作る。"""
        first = self.citations[0].model_copy(update=update) if self.citations else GroundedCitation(**{"evidence_id": "", **update})
        return self.model_copy(update={"citations": [first, *self.citations[1:]]})

    def single_citations(self) -> list[GroundedItem]:
        """出典ごとに 1 つの出典だけを持つ写し（gap・出典の無い item は自身だけ）。"""
        if len(self.citations) <= 1:
            return [self]
        return [self.model_copy(update={"citations": [citation]}) for citation in self.citations]


class GroundedApplication(_StrictModel):
    """質問に書かれた事実（前提）に、支持された規則の item を当てはめた結果（#2155）。

    前提は質問の原文の一部、規則は items の index で指し、計算は算式で書く。システムが前提の照合・規則の
    支持・算式の計算を確かめ、監査が当てはめの正しさを判定してから公開する。
    """

    request_id: str = Field(default="", description="この当てはめが答える request_units の id。")
    premises: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="当てはめに使う質問の事実（金額・日付・期間・年齢・状態）。質問の原文の連続した一部をそのまま写す。質問に無い前提は書かない。",
    )
    item_indexes: list[int] = Field(
        default_factory=list,
        max_length=6,
        description="当てはめる規則の items の index（0 始まり）。quote のある kind=rule / operation の item だけ。",
    )
    reasoning: str = Field(
        min_length=1,
        max_length=600,
        description="規則を前提にどう当てはめたか。計算は「44万円＋180万円÷12＝59万円」のように数と＋−×÷＝の算式で書く。規則と前提に無い値・条件を足さない。",
    )
    result: str = Field(min_length=1, max_length=300, description="当てはめの結果（要求への答え）。1〜2 文。")


class RequestEvidenceScope(_StrictModel):
    """質問の対象と、各根拠が扱う対象の対応。"""

    evidence_id: str = Field(description="対象を照合する Evidence の id。")
    target: str = Field(description="この根拠が操作・説明する対象。原文の対象名を写す。")
    relation: Literal["direct", "linked", "unrelated"] = Field(
        description="direct=質問の対象・要求について、規則・要件・定義・手順・事実を述べる。linked=別対象だが、質問の操作の前提・後続とする関係が原文に明記されている。unrelated=別の対象・独立した操作。同じ文書や頁にあるだけでは linked にしない。"
    )
    link_quote: str = Field(
        default="", description="linked のとき、操作同士の関係を明記した連続する原文。関係が書かれていなければ unrelated とし空文字にする。"
    )


class RequestCoverage(_StrictModel):
    """要求単位ごとに、どの根拠で答えるかの計画。items を書く前に根拠を選ばせる (#986)。"""

    request_id: str = Field(description="request_units の id。")
    evidence_scope: list[RequestEvidenceScope] = Field(
        default_factory=list,
        description="候補の根拠を対象ごとに照合する。各 evidence_id を1回ずつ、質問の対象との関係を決めてから evidence_ids を選ぶ。",
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="この要求の説明に使う根拠（relation=direct）と、原文に関係が明記された前提・後続操作の evidence_id（[E1] 形式の番号）。同じ内容を述べた根拠が複数の文書にあれば（規則・要件・定義を定めた条項と、それを説明・運用する手順書など）、文書ごとに挙げる。同じ文書・頁にあるだけの別操作は含めない。items はこの計画の根拠だけで作る。無ければ空にし kind=gap にする。",
    )


class RequestScopePlan(_StrictModel):
    """章節情報が欠けた候補を、本文の生成とは分けて照合する。"""

    request_coverage: list[RequestCoverage] = Field(max_length=64, description="質問契約の要求ごとに使用する Evidence の計画。")


class CatalogClaimSupport(_StrictModel):
    """条の根拠が支える説明 1 つ（#3526）。"""

    item_index: int = Field(description="items の index（0 始まり）。")
    lines: str = Field(
        default="",
        max_length=20,
        description="text を支える行の番号か範囲（例: 3、3-5）。行番号（`3| `）の付いた根拠では必ず書き、行番号の無い根拠は空。",
    )


class CatalogEvidenceReview(_StrictModel):
    """条の目録で選んだ条の根拠 1 つの検討の結果（#3526）。"""

    evidence_id: str = Field(description="evidence の evidence_id。")
    supports: list[CatalogClaimSupport] = Field(
        default_factory=list,
        max_length=4,
        description="この根拠の原文がそれだけで text の全体を支える items。原文が text の一部だけを述べる、別の対象・場面の規定なら選ばない。"
        "支える item が無ければ空。",
    )


class CatalogEvidenceReviews(_StrictModel):
    """条の目録で選んだ条の根拠を、回答の説明の出典に使えるか 1 つずつ検討する（#3526）。"""

    reviews: list[CatalogEvidenceReview] = Field(max_length=16, description="evidence の各根拠を 1 回ずつ判定する。")


class ExternalDataItem(_StrictModel):
    """回答の確定に確かめる点 1 件と、観察できる事実 (#1453、#1492)。

    モデルには分類（どの対応にするか）を答えさせず、項目ごとに観察できる事実だけを答えさせる: 誰が答えられるか
    （who_can_answer）、すでに分かっているか（already_known）、結論がその値で分かれるか（decides）。対応は
    `answer_envelope.settle_confirmation_items` の規則で決める（system だけが持つ値 → needs_environment_data、
    利用者が答えられる条件 → conditional、別の資料 → 資料の確認）。
    """

    text: str = Field(description="確かめる点と照合の目的（例: 「対象の取引先の支払区分の設定値（どの手順に当たるかを決める）」）。")
    who_can_answer: Literal["user", "system", "document"] = Field(
        description="この点の値を答えられるもの。3 つは排他で、値の出どころで決める。"
        "user=質問した利用者本人が自分の状況として答えられる・決められる（自分のロール・権限を持つか、承認を得たか、"
        "どのシステム・申請・アカウントのことか、対象のアカウントの種類（検証用か・共用か）、付与先を個別とグループの"
        "どちらにするか、何をしたいか）。"
        "document=KB の資料に載っている事実（規程・マニュアル・コード表の規則・定義、台帳・一覧・表の値、どの版が"
        "有効か・廃止されたか）。取得した根拠に該当の行・記載があれば値は分かるので挙げない。"
        "system=稼働中のシステムにだけあり、資料にも利用者の答えにも無い値（今の設定値、エラーの時刻のログの行、"
        "処理の状態・実行の結果、件数・実績のデータ）。"
        "反例: 自分がロールを持つかは画面で確かめられても user。付与対象のアカウントが検証用かは利用者が知っているので user。"
        "対象のシステムの台帳の担当部署は台帳が資料なので document（根拠に台帳の行があれば挙げない）。"
        "どの版が有効かは利用者の環境ではなく資料が決めるので document。"
        "資料の例示値・記載時点の値が今の環境でも同じかは system。"
    )
    already_known: bool = Field(
        description="true=質問・会話・「分かっている条件」・取得した根拠の原文から、値がすでに分かる"
        "（例: 利用者が「承認はもらっています」と書いた承認の有無、根拠が「第1版は廃止」と書いている適用の版）。"
        "false=まだ分からない。"
    )
    decides: bool = Field(
        description="true=値によって回答の結論・選ぶ手順・可否が変わる（例: ロールの有無で自分で操作するか依頼するかが変わる）。"
        "false=値に関わらず同じ手順・結論で、念のための確認・補足にとどまる。"
    )

    @model_validator(mode="before")
    @classmethod
    def _facts_from_legacy_kind(cls, value: Any) -> Any:
        # 構造化出力を強制できない接続の応答・#1492 より前の記録は、文字列だけか種類（kind）だけを持つ。欠けた事実は
        # 保守的に埋める（answer_envelope.confirmation_item_facts と同じ写し）: kind から誰が答えられるかを写し
        # （種類も無ければシステムの値）、未確認・結論を分ける項目とする。モデルが返した値はそのまま検証する。
        from rag_engine.generation.answer_envelope import confirmation_item_facts

        if isinstance(value, str):
            return confirmation_item_facts(value)
        if isinstance(value, dict):
            facts = confirmation_item_facts(value)
            return {
                **{key: facts[key] for key in ("who_can_answer", "already_known", "decides")},
                **{key: item for key, item in value.items() if key != "kind"},
            }
        return value


class GuideRequirementCoverage(_StrictModel):
    requirement_id: str = Field(description="guide_contracts の要件 id。")
    evidence_ids: list[str] = Field(
        max_length=8, description="この要件の説明を支える Evidence の id。ガイドの要件そのものは文書の仕様を示す根拠にしない。"
    )
    gap: str = Field(description="根拠が不足するとき、確認できない操作・条件と確認先。根拠があれば空文字。")


class GroundedDraft(_StrictModel):
    """主張と引用の対で構成する回答草稿。本文の体裁は render が決定的に組み立てる。"""

    # SDK が返した本文。モデルの出力schemaや通常のmodel_dumpには入れない。
    _provider_output_text: str | None = PrivateAttr(default=None)
    # 検証の前に最初の 1 件へ寄せた、同じ内容の entry の数（欄ごと。#3849）。原文は _provider_output_text に残る。
    _collapsed_duplicates: dict[str, int] = PrivateAttr(default_factory=dict)

    request_coverage: list[RequestCoverage] = Field(
        default_factory=list, max_length=64, description="要求単位ごとに使う根拠の計画。items より先に書く。"
    )
    guide_coverage: list[GuideRequirementCoverage] = Field(
        default_factory=list,
        max_length=200,
        description="guide_contracts の各要件に使う根拠の計画。全 id を1回ずつ、前提の手順から並べる。kind=evidence は引用元と主張の対応を検査する要件で、その指示の復唱は不要。kind=completion は items で説明する操作・条件・除外・確認先。どちらも足りなければ gap に残す。",
    )
    items: list[GroundedItem] = Field(max_length=40, description="主張と原文引用の対。機能・前提ごとに分け、見出しは書かない。")
    applications: list[GroundedApplication] = Field(
        default_factory=list,
        max_length=6,
        description="質問に書かれた事実（金額・日付・期間・条件）に items の規則を当てはめて答える要求の当てはめ。規則と、規則が要る質問の事実がそろうときだけ書く。",
    )
    external_data_required: bool = Field(
        default=False,
        description="システムだけが持つ値（who_can_answer=system）に依存する結論が残るなら true。項目を external_data_items に挙げたときは、システムは項目の事実から決める。",
    )
    external_data_items: list[ExternalDataItem] = Field(
        default_factory=list,
        max_length=10,
        description="回答の確定に確かめる点と照合の目的。項目ごとに誰が答えられるか・すでに分かっているか・結論がその値で分かれるかを書く。資料名は reference_materials に書く。",
    )
    reference_materials: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="回答の確定に閲覧が必要な別の資料（規程・コード定義表・マスタ一覧・別マニュアルなど）と照合の目的。資料名は根拠の原文か質問に出た名前だけを「」で囲んで書き、無ければ資料の種類で書く。",
    )
    question_type: list[str] = Field(default_factory=list, description="質問の種類の分類語（任意）。")
    summary: str = Field(
        description="items で裏付けた結論だけを1〜2文で。引用できる根拠が無ければ空文字。見出し・番号・出典・Markdown は書かない。"
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="high=主要な説明と適用条件に直接の根拠がある。medium=一部に不確実性がある。low=案内自体に根拠が乏しい。"
    )

    @model_validator(mode="wrap")
    @classmethod
    def _collapse_duplicate_entries(cls, data: Any, handler: ModelWrapValidatorHandler[GroundedDraft]) -> GroundedDraft:
        """同じ内容の計画・説明の繰り返しを最初の 1 件に寄せてから検証する（#3849）。

        長い出力では、モデルが同じ要求の計画を内容を変えずに上限まで繰り返すことがある。寄せるのは内容がすべて
        同じ entry だけで、内容の違う entry は 1 件も落とさない（件数の上限は寄せた後の件数で判定するので、内容の
        違う entry が上限を超える出力は従来どおり検証で不合格になる）。items を寄せたら、当てはめが指す index を
        寄せた先に付け替える。寄せた数は ``_collapsed_duplicates`` に残す。
        """
        collapsed: dict[str, int] = {}
        if isinstance(data, Mapping):
            data = dict(data)
            for field in ("request_coverage", "guide_coverage"):
                kept, _index, removed = _collapse_identical(data.get(field))
                if removed:
                    data[field] = kept
                    collapsed[field] = removed
            kept, index, removed = _collapse_identical(data.get("items"))
            if removed:
                data["items"] = kept
                collapsed["items"] = removed
                data["applications"] = _reindex_applications(data.get("applications"), index)
        draft = handler(data)
        if collapsed:
            draft._collapsed_duplicates = collapsed
        return draft

    def confirmation_items(self) -> list[dict[str, Any]]:
        """確認の項目の事実（`answer_envelope.settle_confirmation_items` の入力。#1492）。"""
        return [item.model_dump() for item in self.external_data_items]


def _entry_key(entry: Any) -> str | None:
    """entry の内容の比較の鍵（JSON の object だけ。比べられない値は None で、寄せない）。"""
    if not isinstance(entry, Mapping):
        return None
    try:
        return json.dumps(entry, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _collapse_identical(entries: Any) -> tuple[Any, dict[int, int], int]:
    """内容がすべて同じ entry を最初の 1 件に寄せた一覧、元の index → 寄せた後の index、寄せた数。"""
    if not isinstance(entries, list):
        return entries, {}, 0
    kept: list[Any] = []
    first: dict[str, int] = {}
    index: dict[int, int] = {}
    for position, entry in enumerate(entries):
        key = _entry_key(entry)
        if key is not None and key in first:
            index[position] = first[key]
            continue
        if key is not None:
            first[key] = len(kept)
        index[position] = len(kept)
        kept.append(entry)
    return kept, index, len(entries) - len(kept)


def _reindex_applications(applications: Any, index: Mapping[int, int]) -> Any:
    """当てはめの ``item_indexes`` を、寄せた後の items の index にする（範囲外の値はそのまま検査に回す）。"""
    if not isinstance(applications, list):
        return applications
    result: list[Any] = []
    for application in applications:
        indexes = application.get("item_indexes") if isinstance(application, Mapping) else None
        if not isinstance(indexes, list):
            result.append(application)
            continue
        moved: list[Any] = []
        for value in indexes:
            value = index.get(value, value) if type(value) is int else value
            # 寄せた 2 つの item を指していたら 1 回にする（同じ item を 2 回当てはめない）。
            if type(value) is int and value in moved:
                continue
            moved.append(value)
        result.append({**application, "item_indexes": moved})
    return result


class OutputFieldEvidence(_StrictModel):
    """出力データの項目と根拠の役割。生成時と最終検証で同じ契約を使う。"""

    # 列の主張の有無をモデルの null / object の選び方に任せない（空の object が「主張なし」と「定義なし」の両方になる。#2821）。
    asserts_output_fields: bool = Field(
        description="判定する文が、出力ファイルに含まれる列・項目・値を主張していれば true。画面の操作・抽出条件・帳票名だけなら false。"
    )
    definition_quote: str = Field(
        default="",
        description="主張している出力ファイルの列・項目・含まれる値を定義する原文をそのまま写す。画面の抽出条件や出力帳票名の一覧しかない場合は空文字。",
    )
    output_artifact_matches: bool | None = Field(
        default=None, description="definition_quoteが主張と同じ出力ファイルの項目定義ならtrue。別物ならfalse、同一性不明ならnull。"
    )

    def output_fields_unconfirmed(self) -> bool:
        """列を主張しているのに、定義の原文か成果物の一致が無い。列を主張していなければ照合しない。"""
        return self.asserts_output_fields and (not self.definition_quote.strip() or self.output_artifact_matches is not True)


class GroundedFragmentCitation(_StrictModel):
    """元の説明が実際に引用した原文の中で、局所的な支持に使う箇所。"""

    evidence_id: str
    quote: str = Field(min_length=1, max_length=600, description="このitemの実際の引用内から写した連続した支持原文。")


class GroundedSupportedFragment(_StrictModel):
    """複合した説明のうち、同じ監査で独立に支持を判定した元の説明の片。"""

    text: str = Field(min_length=1, max_length=600, description="元item.textの連続した部分をそのまま写す。書き換えない。")
    support: Literal["supported", "unknown"] = Field(
        default="unknown", description="引用集合がこの片全体を支えると確認したときだけsupported。"
    )
    citations: list[GroundedFragmentCitation] = Field(default_factory=list, max_length=4)


class GroundedItemReview(_StrictModel):
    """1 item の局所監査。引用が言い換えを支持するかと、質問への適用性を分ける。"""

    index: int = Field(description="監査対象 items の index。")
    support: Literal["supported", "unsupported", "contradicted", "unknown"] = Field(
        description="supported=実際の引用集合が主張全体を支え、この引用も寄与。unsupported=引用にない操作・対象・条件・順序を補う。contradicted=引用と逆。unknown=判定不能。"
    )
    applicability: Literal["matched", "conditional", "not_applicable", "unknown"] = Field(
        description="matched=質問の対象の種類に当てはまる（一般手順でよい）。conditional=原文の条件の成立が質問から確認できない。not_applicable=別の対象・項目・業務向け。unknown=対象の種類が同じか判断できない。"
    )
    source_applicability: Literal["matched", "conditional", "not_applicable", "unknown"] | None = Field(
        default=None,
        description="実際の引用資料の対象・前提が、この操作と確認済み条件へ適用できるか。業務ガイドで決まった必須範囲を再裁定しない。matched=適合、conditional=原文条件が未確認、not_applicable=原文が別対象へ限定、unknown=判断不能、null=未検証。",
    )
    source_condition: str = Field(default="", description="source_applicability=conditional のとき実際の引用原文にある条件。")
    business_scope: SkipJsonSchema[dict[str, Any]] = Field(
        default_factory=dict, description="コードが確定したガイド範囲。モデルの判定ではない。"
    )
    condition: str = Field(default="", description="conditional のとき原文の条件。")
    reason: str = Field(description="判定の根拠を短く。")
    support_quote: str = Field(
        default="",
        max_length=600,
        description="段落を支持する連続した原文（根拠からの抜粋）。",
    )
    supported_fragments: list[GroundedSupportedFragment] = Field(
        default_factory=list,
        max_length=40,
        description="contradictedの複合itemに、独立に支持できる元の説明の部分がある場合だけ返す。各片は元textの厳密な抜粋と、このitemが実際に引用した出典・原文で裏付ける。判断不能ならunknownまたは空。",
    )
    # 原判定を保持し、採否への利用だけを別の方針にする。モデルの応答 schema には内部方針を出さない。
    observed_support: SkipJsonSchema[str] = Field(default="", description="監査が答えた支持判定（応答の schema には出さない）。")
    observed_applicability: SkipJsonSchema[str] = Field(default="", description="監査が答えた適用性の記録（応答の schema には出さない）。")
    applicability_action: SkipJsonSchema[Literal["apply", "record_only"]] = Field(
        default="apply", description="適用性の原判定を本文へ反映するか。"
    )
    applicability_policy_reason: SkipJsonSchema[str] = Field(default="", description="原判定と本文への反映を分けた理由。")

    output_fields: OutputFieldEvidence | None = Field(
        default=None, description="上の判定理由に対応する出力ファイルの項目の照合。列の主張がない操作の説明はnull。"
    )


class GroundedApplicationReview(_StrictModel):
    """1 つの当てはめの監査（#2155）。"""

    index: int = Field(description="監査対象 applications の index。")
    valid: bool = Field(
        description="true=前提が質問の事実で、指した items の quote の規則を正しく当てはめ、計算・期間の数え方・結論が正しい。"
        "規則の適用条件が質問から成立しない、計算や数え方の誤り、規則に無い条件の追加があれば false。"
    )
    reason: str = Field(description="判定の根拠を短く。")


class ApplicationDraft(_StrictModel):
    """規則の当てはめだけを作る段の出力（#2159）。結論の文は当てはめと規則でつないだものだけ。"""

    applications: list[GroundedApplication] = Field(
        default_factory=list,
        max_length=6,
        description="質問に書かれた事実に、items の規則を当てはめた結果。当てはめられる規則と事実が無ければ空。",
    )
    summary: str = Field(
        description="items の規則と applications の結果でつないだ結論を 1〜2 文で。当てはめが無ければ空文字。見出し・出典・Markdown は書かない。"
    )


class ApplicationAudit(_StrictModel):
    """当てはめの段の監査（#2159）。"""

    application_reviews: list[GroundedApplicationReview] = Field(default_factory=list, max_length=6, description="各 application の監査。")
    summary_supported: bool = Field(
        description="summary が items の規則と valid な application の結果の範囲を超える断定をしていなければ true。"
    )


class MissingContentEvidence(_StrictModel):
    """回答の不足内容を補うための原文と出典。意味支持は要求監査が判定する。"""

    evidence_id: str = Field(description="不足内容の直接根拠の evidence_id。")
    source: str = Field(default="", description="evidence_sources または unused_evidence の source をそのまま写す。")
    quote: str = Field(default="", max_length=2000, description="不足操作・対象・条件・順序を支える入力原文。複数根拠の共同支持も可。")


class GroundedRequestReview(_StrictModel):
    """原質問の要求単位ごとの充足。"""

    request_id: str = Field(description="request_units の id。")
    status: Literal["addressed", "partial", "missing", "context", "unknown"] = Field(
        description="addressed=items が答えている。partial=一部。missing=無い。context=背景で答える対象ではない。unknown=充足を判断できない。"
    )
    reason: str = Field(description="質問と同じ言語で短く。")
    evidence_status: Literal["available", "missing", "unknown"] = Field(
        default="unknown",
        description="不足内容を evidence_support の引用集合が支える=available、直接根拠が無い=missing、判断不能=unknown。説明済み部分の根拠では代用しない。",
    )
    evidence_ids: list[str] = Field(default_factory=list, max_length=64, description="available の判定を支える入力の evidence_id。")
    missing_content: str = Field(
        default="", max_length=2400, description="まだ回答していない具体的な操作・対象・条件・順序。partial / missing の補写対象。"
    )
    evidence_support: list[MissingContentEvidence] = Field(
        default_factory=list,
        max_length=64,
        description="missing_content を共同で支える直接根拠。available では evidence_ids の全 ID と対応させ、説明済みの別操作の引用で代用しない。",
    )


class GuideRequirementReview(_StrictModel):
    answer_quote: str = Field(
        default="",
        max_length=2400,
        description="要件を説明している回答 items の text の原文。複数の item を使う場合は該当の text を改変せず改行でつなぐ。説明がなければ空文字。資料から引用しない。",
    )
    requirement_id: str = Field(description="guide_contracts の requirements の id。")
    answer_state: Literal["explained", "partly_explained", "stated_unavailable", "not_mentioned"] = Field(
        description=(
            "回答が要件の内容をどう扱ったか（観察した事実だけ。充足かはシステムが決める）。explained=items の text が要件の内容を"
            "すべて述べた。partly_explained=一部だけ述べた。stated_unavailable=要件の内容が資料に無い・確認できないことを"
            "gap_items か guide_notes で明示した。not_mentioned=触れていない。"
        )
    )
    item_indexes: list[int] = Field(
        max_length=40,
        description="要件を扱った回答の items / gap_items / guide_notes の index。",
    )
    reason: str = Field(max_length=600, description="不足している操作・条件。充足なら空文字。")


class GroundedFactAudit(_StrictModel):
    """通常の回答の監査（#2795）。主張の支持・適用、summary、目的との一致、要求と業務ガイドの要件の充足を判定する。

    未使用の根拠は列挙しない（詳しい監査 ``GroundedAudit`` の項目）。受け取った後は ``GroundedAudit`` に写して使う
    （判定しない項目は空）。
    """

    reviews: list[GroundedItemReview] = Field(max_length=40, description="各 item の局所監査。")
    guide_requirement_reviews: list[GuideRequirementReview] = Field(
        default_factory=list,
        max_length=200,
        description="guide_contracts の要件ごとの充足。全 id を1回ずつ判定する。設定の要件は回答の評価基準であり、文書の仕様の根拠ではない。",
    )
    application_reviews: list[GroundedApplicationReview] = Field(
        default_factory=list, max_length=6, description="各 application（当てはめ）の監査。"
    )
    request_reviews: list[GroundedRequestReview] = Field(default_factory=list, max_length=64, description="request_units の各 id の充足。")
    summary_output_fields: OutputFieldEvidence | None = Field(
        default=None, description="要約の出力項目について、定義の原文引用と対象の一致を返す。"
    )
    summary_basis: list[str] = Field(default_factory=list, max_length=40, description="summary を支える items の text 全文。")
    summary_supported: bool = Field(description="summary が items の範囲を超える断定をしていなければ true。")
    goal_alignment: Literal["aligned", "partial", "off_target"] = Field(
        description="aligned=items 全体が目的に答えている。partial=一部だけ。off_target=別の対象・業務・目的に答えている。"
    )


class GroundedAudit(_StrictModel):
    """監査は降格と不足の指摘だけを行い、原文一致した内容を削除しない。"""

    _wire_validation: dict[str, Any] = PrivateAttr(default_factory=dict)

    reviews: list[GroundedItemReview] = Field(max_length=40, description="各 item の局所監査。")
    guide_requirement_reviews: list[GuideRequirementReview] = Field(
        default_factory=list,
        max_length=200,
        description="guide_contracts の要件ごとの充足。全 id を1回ずつ判定する。設定の要件は回答の評価基準であり、文書の仕様の根拠ではない。",
    )
    application_reviews: list[GroundedApplicationReview] = Field(
        default_factory=list, max_length=6, description="各 application（当てはめ）の監査。"
    )
    request_reviews: list[GroundedRequestReview] = Field(default_factory=list, max_length=64, description="request_units の各 id の充足。")
    unused_evidence_ids: list[str] = Field(
        default_factory=list, max_length=8, description="回答に必要なのに items で使われていない根拠の evidence_id。重要な順。"
    )
    summary_output_fields: OutputFieldEvidence | None = Field(
        default=None, description="要約の出力項目について、定義の原文引用と対象の一致を返す。"
    )
    summary_basis: list[str] = Field(default_factory=list, max_length=40, description="summary を支える items の text 全文。")
    summary_supported: bool = Field(description="summary が items の範囲を超える断定をしていなければ true。")
    goal_alignment: Literal["aligned", "partial", "off_target"] = Field(
        description="aligned=items 全体が目的に答えている。partial=一部だけ。off_target=別の対象・業務・目的に答えている。"
    )
