"""RAG 評価スキーマ。"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import AnswerFlow, EvaluationSuite, QueryStrategy, ReasoningEffortOverride
from app.schemas.search import (
    SEARCH_TOP_K_MAX,
    SearchDiagnostics,
    format_search_id_filter,
    normalize_query_text,
    normalize_search_filters,
    normalize_search_id_list,
    parse_search_id_filter,
)
from app.schemas.support_guide import (
    PROCESS_IMPORT_QUESTION_MAX_CHARS,
    PROCESS_IMPORT_TEXT_MAX_CHARS,
    SupportGuideProcessReference,
)
from app.search_defaults import search_top_k_default, search_top_k_schema

EvaluationSuiteName = EvaluationSuite

# 評価ケースの id の長さの上限(文字)。job の `current_case_id` の列(VARCHAR2(200 CHAR))。
EVALUATION_CASE_ID_MAX_CHARS = 200

# 標準回答の長さの上限(文字)。標準回答による評価の入力の予算(48,000 bytes)に収まる長さにする。
STANDARD_ANSWER_MAX_CHARS = 8000

# 評価のケースの分類（#1226。業務支援の改修 #1218 の評価の契約）。答えられるかの種類で結果を
# 分けて見る。
# - document_answerable: 資料だけで答えられる
# - clarification_required: 利用者に条件を確かめる必要がある
# - environment_data_required: 現場の値・記録（参照の道具）が要る
# - knowledge_missing: 資料に答えが無い（拒答・人への引き継ぎが正しい）
# - conflicting_sources: 資料どうしが矛盾する
EvaluationCaseCategory = Literal[
    "document_answerable",
    "clarification_required",
    "environment_data_required",
    "knowledge_missing",
    "conflicting_sources",
]
EVALUATION_UNCATEGORIZED = "uncategorized"

# 評価のケースの区分（#1284。handoff §14.1）。dev は設定・業務ガイドの調整に使ってよいケース、
# holdout は調整に使わず最後の確認だけに使うケース。同じ業務の流れ・資料の族のケースは同じ区分に
# する（言い換えの漏れを防ぐ）。区分の無いケースは内訳で unassigned にする。
EvaluationCaseSplit = Literal["dev", "holdout"]
EVALUATION_UNASSIGNED_SPLIT = "unassigned"

# 多段の質問（multi-hop）の種類（#1335）。複数の根拠をつないで初めて答えられる質問を、つなぎ方で
# 分けて見る。採点の方法は変えない。
# - single_hop: 1 つの根拠で答えられる（対照）
# - bridge: 橋渡し（A の属性で B を引き、B の属性で答える）
# - comparison: 2 つ以上の実体の属性を比べる
# - intra_document_reference: 同じ文書の中の参照（「第 3 章を参照」）をたどる
# - table_lookup: 本文の実体で表の行を引く（表の中の絞り込みを含む）
# - section_read: 複数ページの章節を読み切る（必要な根拠が同じ章節の別々のページにある。#1438）
# - document_read: 文書全体の複数の章に散らばる記載を、件数・章の範囲を示して網羅させる（#1503）
EvaluationReasoningType = Literal[
    "single_hop",
    "bridge",
    "comparison",
    "intra_document_reference",
    "table_lookup",
    "section_read",
    "document_read",
]
EVALUATION_UNSPECIFIED_REASONING = "unspecified"
# 段の数の上限と、2 段以上を求める種類（比べる・表を引くは 1 段でもよい）。
EVALUATION_MAX_HOPS = 10
EVALUATION_MULTI_HOP_REASONING_TYPES: frozenset[str] = frozenset(
    {"bridge", "intra_document_reference"}
)

# 1 ケースの往復の上限（最初の質問を除く）と、往復をまたいだ条件の数の上限
# （SearchRequest.conditions と同じ）。
EVALUATION_MAX_TURNS = 5
EVALUATION_MAX_CONDITIONS = 30

# 回答の対応（#1231。業務支援の評価）。評価のケースは受け入れる対応を複数持てる
# （例: 確認が要る質問は、確認の質問か、条件ごとに分けた回答のどちらでもよい）。
# - answered: 資料にもとづいて答えた
# - conditional: 条件・不足を示して答えた（一部に確認が要る）
# - needs_clarification: 利用者に条件を確かめた
# - needs_environment_data: 現場の値・記録が要ると示した
# - needs_human: 人への引き継ぎを示した
# - insufficient_evidence: 資料から答えられないと示した（拒答）
EvaluationOutcome = Literal[
    "answered",
    "conditional",
    "needs_clarification",
    "needs_environment_data",
    "needs_human",
    "insufficient_evidence",
]

# 失敗理由(#591)。保存済みの結果には削除した理由(content_kind_miss など)が残るため、
# 結果の model は str で受ける。
EvaluationFailureReason = Literal[
    "retrieval_miss",
    "partial_recall",
    "unexpected_answer",
    "unexpected_refusal",
    "answer_keyword_miss",
    "low_groundedness",
    "unsupported_claim",
    "claim_unassessed",
    "missing_content",
    "answer_failed",
    "answer_evaluation_error",
    "guardrail_warning",
    "unexpected_handling",
    "step_missing",
    "forbidden_action",
    "condition_missing",
    # 必要な根拠（required_evidence）の一部を最後の回答の引用で取れなかった（#1284）。
    "evidence_miss",
    # 質問・前の返答で渡した条件を、確認の質問でもう一度聞いた（#1284）。
    "known_condition_reasked",
    # 案内すべき画面（expected_screens）の一部を回答が案内しなかった（#2402）。
    "screen_missing",
    # 案内すべき画面のほかの画面を回答が案内した（#2402）。
    "wrong_screen",
    # 回答の監査が、回答は質問の目的に答えていない（goal_alignment=off_target）と判定した（#2402）。
    "off_target",
    "case_error",
]

# 評価の指標(#591)。検索・根拠・回答の 3 つの観点に整理した 9 つと、業務支援の対応の 4 つ（#1231）。
EVALUATION_METRICS_VERSION = "six_metrics_v1"
EvaluationMetricName = Literal[
    "required_evidence_recall",
    "mrr",
    "answer_correctness",
    "requirement_coverage",
    "claim_support_rate",
    "response_decision_accuracy",
]
EVALUATION_METRIC_NAMES: tuple[EvaluationMetricName, ...] = (
    "required_evidence_recall",
    "mrr",
    "answer_correctness",
    "requirement_coverage",
    "claim_support_rate",
    "response_decision_accuracy",
)
EVALUATION_REPORT_ONLY_METRIC_NAMES: tuple[str, ...] = ()


# 期待する手順の別解の上限（列の数）。
EVALUATION_MAX_STEP_ALTERNATIVES = 5


def _clean_phrase_list(value: list[str]) -> list[str]:
    """前後の空白を除き、空の語を捨てる。"""
    return [item.strip() for item in value if item.strip()]


def _clean_conditions(value: dict[str, str]) -> dict[str, str]:
    """条件の id と値の前後の空白を除き、空の id・値を捨てる。"""
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        condition_id, condition_value = key.strip(), item.strip()
        if condition_id and condition_value:
            cleaned[condition_id] = condition_value
    return cleaned


def _clean_alternatives(value: list[list[str]]) -> list[list[str]]:
    """別解の各列の語を整え、空になった列を捨てる。"""
    return [steps for steps in (_clean_phrase_list(item) for item in value) if steps]


class EvaluationHandlingExpectation(BaseModel):
    """意味による網羅の確認に使う必須要点と、応答の期待値。"""

    expected_response_decision: Literal["answer", "clarify", "hold"] | None = None
    expected_outcomes: list[EvaluationOutcome] = Field(default_factory=list, max_length=6)
    required_answer_points: list[str] = Field(default_factory=list, max_length=30)
    required_conditions: list[str] = Field(default_factory=list, max_length=30)


class EvaluationTurn(EvaluationHandlingExpectation):
    """確認の質問への返答の 1 往復（#1284。handoff §14.1「允许的澄清回应」）。

    ランナーはチャットと同じく、`reply` を次の質問にし、前の往復（質問と回答）を会話の履歴として
    渡す。`conditions` は返答で分かった条件の値で、前までの条件に足して渡す（チャットの確認の答え・
    MCP の `conditions` と同じ）。期待（対応・手順・危険な表現・条件）は、この往復の回答だけを採点
    する。
    """

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(..., min_length=1, max_length=8000)
    conditions: dict[str, str] = Field(default_factory=dict, max_length=EVALUATION_MAX_CONDITIONS)

    @field_validator("reply")
    @classmethod
    def validate_reply(cls, value: str) -> str:
        """SearchRequest の query と同じ規則で返答を正規化する。"""
        return normalize_query_text(value)

    @field_validator("conditions")
    @classmethod
    def validate_conditions(cls, value: dict[str, str]) -> dict[str, str]:
        """条件の id と値の前後の空白を除く。"""
        return _clean_conditions(value)


# 根拠の本文の抜粋・章節の見出しの長さの上限(文字)。
EVIDENCE_TEXT_MAX_CHARS = 300
EVIDENCE_SECTION_MAX_CHARS = 300
# 根拠の書いた元の文(Excel の 1 行。結果の表示に使う)の長さの上限(文字)。
EVIDENCE_LABEL_MAX_CHARS = 1000


# 必要な根拠の行の番号の基準（RAG の根拠の `locator.row_basis` と同じ語彙。#3547）。
EvidenceRowBasis = Literal["sheet", "csv", "word_table", "parsed_table"]


class EvaluationEvidence(BaseModel):
    """回答に必要な根拠（#1284。handoff §14.2 の「各必需证据召回」）。

    最後の回答の引用（chunk）のうち、次の条件をすべて満たす chunk が 1 つでもあれば、この根拠を
    取れたとみなす（#2065。書いた条件だけを確かめる）。

    - `document_id`: その文書の chunk（省略時はどの文書でもよい）。
    - `page_start`〜`page_end`: chunk のページ（`page_start`〜`page_end`、無ければ `page_number`）が
      範囲と重なる（ページの分からない chunk は一致しない）。`page_end` を省くと 1 ページ。
      PDF にしたスライドはスライドの番号がページの番号になる。
    - `sheet`: chunk のシート名（`sheet_name`）が一致する（表計算の文書。#2086）。
    - `row_start`〜`row_end`: chunk の行（`row_start`〜`row_end`）が範囲と重なる（行の分からない
      chunk は一致しない）。`row_end` を省くと 1 行。行は `row_basis`（行の番号の基準。RAG の根拠の
      `locator.row_basis` と同じ語彙: `sheet` / `csv` / `word_table` / `parsed_table`）が同じ chunk
      だけと照合する。`row_basis` を省くと原本の行（表計算のシート・CSV の記録・Word の表）として
      照合し、解析が組み立てた表の格子の行（`parsed_table`。原本の座標ではない）には一致させない
      （#3547）。
    - `section`: chunk の章節の見出しの道筋（`section_path`）が `section` を含む。
    - `text`: chunk の本文が `text` を含む。

    文字の比較は NFKC・大小文字・空白を無視する。文書だけの根拠（条件の無い根拠）は書かず、正解の
    文書（`relevant_document_ids`）に書く。文書 ID は評価セットでは `file:<ファイル名>` で書き、
    `evaluation_corpus_cli` が配備先の ID に置き換える。`label` は利用者が書いた元の文（Excel の
    「期待する根拠」の 1 行）で、採点には使わない。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=80)
    document_id: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=EVIDENCE_TEXT_MAX_CHARS)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    section: str | None = Field(default=None, max_length=EVIDENCE_SECTION_MAX_CHARS)
    sheet: str | None = Field(default=None, max_length=EVIDENCE_SECTION_MAX_CHARS)
    row_start: int | None = Field(default=None, ge=1)
    row_end: int | None = Field(default=None, ge=1)
    row_basis: EvidenceRowBasis | None = None
    label: str | None = Field(default=None, max_length=EVIDENCE_LABEL_MAX_CHARS)

    @field_validator("id")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """前後の空白を除き、空の値を拒否する。"""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("根拠の id を入力してください。")
        return cleaned

    @field_validator("document_id", "text", "section", "sheet", "label")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        """空の値は指定なしとして扱う。"""
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def validate_conditions(self) -> Self:
        """条件（本文・ページ・章節・シート・行）を 1 つ以上と、範囲の向きを確かめる。"""
        for start, end, name in (
            (self.page_start, self.page_end, "page"),
            (self.row_start, self.row_end, "row"),
        ):
            if end is not None and start is None:
                raise ValueError(f"根拠の {name}_end は {name}_start と一緒に指定してください。")
            if start is not None and end is not None and end < start:
                raise ValueError(f"根拠の {name}_end は {name}_start 以上にしてください。")
        if self.row_basis is not None and self.row_start is None:
            raise ValueError("根拠の row_basis は row_start と一緒に指定してください。")
        if (
            self.text is None
            and self.page_start is None
            and self.section is None
            and self.sheet is None
            and self.row_start is None
        ):
            raise ValueError(
                "根拠には本文の語句・ページ・章節・シート・行のどれかを指定してください。"
            )
        return self


# 案内すべき画面の数・別名の数・名前の長さの上限（#2402）。
EVALUATION_MAX_SCREENS = 20
EVALUATION_MAX_SCREEN_ALIASES = 10
EVALUATION_SCREEN_NAME_MAX_CHARS = 200


class EvaluationScreen(BaseModel):
    """回答が案内すべき画面（#2402）。名前と、同じ画面の別の呼び方（別名）。

    回答の手順の見出しに名前か別名が出れば、その画面を案内したとみなす（文字の比較は NFKC・
    大小文字・空白を無視する）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=EVALUATION_SCREEN_NAME_MAX_CHARS)
    aliases: list[str] = Field(default_factory=list, max_length=EVALUATION_MAX_SCREEN_ALIASES)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """前後の空白を除き、空の名前を拒否する。"""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("画面の名前を入力してください。")
        return cleaned

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: list[str]) -> list[str]:
        """前後の空白を除き、空の別名・長すぎる別名を拒否する。"""
        cleaned = _clean_phrase_list(value)
        if any(len(item) > EVALUATION_SCREEN_NAME_MAX_CHARS for item in cleaned):
            raise ValueError(
                f"画面の別名は {EVALUATION_SCREEN_NAME_MAX_CHARS} 文字以内にしてください。"
            )
        return list(dict.fromkeys(cleaned))


class EvaluationCase(EvaluationHandlingExpectation):
    """六指標の評価入力。本文・位置の根拠、正解文書、標準回答・必須要点、応答の期待を持つ。

    複数往復では各往復の明示的な応答判断を評価し、検索と回答の意味上の評価は最後の回答を使う。
    評価情報がない指標は未評価とし、標準回答の有無から応答の期待を推定しない。
    """

    # 結果の表・job の実行中のケース（`rag_evaluation_jobs.current_case_id` は 200 文字）で
    # ケースを区別するため、空でない 200 文字までにする（#977）。
    id: str = Field(..., min_length=1, max_length=EVALUATION_CASE_ID_MAX_CHARS)
    query: str = Field(..., min_length=1)
    relevant_document_ids: list[str] = Field(default_factory=list)
    # 同じ文書とみなす別の文書の ID（正解の文書の ID → 別の文書の ID の列。#2086）。評価の Excel で
    # 同じ名前の文書が複数あるとき、どれが引用されても正解の文書・必要な根拠の文書に一致させる。
    equivalent_document_ids: dict[str, list[str]] = Field(default_factory=dict)
    standard_answer: str | None = Field(default=None, max_length=STANDARD_ANSWER_MAX_CHARS)
    answerable: bool | None = None
    # 分類（任意。#1226）。結果の分類ごとの内訳に使う。採点の方法は変えない。
    category: EvaluationCaseCategory | None = None
    # 区分（任意。#1284）。結果の区分ごとの内訳に使う。採点の方法は変えない。
    split: EvaluationCaseSplit | None = None
    # 質問と一緒に渡す既知の条件の値（条件の id → 値。#1284）。チャットの確認の答え・MCP の
    # conditions と同じく SearchRequest.conditions に渡す。業務ガイドの条件の id に合わせて書き、
    # 業務ガイドを使わない評価（検索・回答プロファイルを指定しない評価）では回答に影響しない。
    conditions: dict[str, str] = Field(default_factory=dict, max_length=EVALUATION_MAX_CONDITIONS)
    # 確認の質問への返答の列（#1284）。最初の質問の後に順に送る。
    turns: list[EvaluationTurn] = Field(default_factory=list, max_length=EVALUATION_MAX_TURNS)
    # 最後の回答に必要な根拠（#1284）。根拠ごとの再現率を求める。多段の質問（#1335）は段ごとの
    # 根拠を全部書く。
    required_evidence: list[EvaluationEvidence] = Field(default_factory=list, max_length=30)
    # 多段の質問の種類と段の数（任意。#1335）。結果の種類別・段の数別の内訳に使う。採点の方法は
    # 変えない。段の数は、根拠をたどる回数（比べる質問は 1 つの実体あたりの回数）。
    reasoning_type: EvaluationReasoningType | None = None
    hops: int | None = Field(default=None, ge=1, le=EVALUATION_MAX_HOPS)
    # 新しい版に置き換えた文書（旧版）も検索して答えるケース（版の比較など。#1366）。true なら、
    # 利用者が旧版・変更点を尋ねるときと同じく filters に include_superseded=true を足して回答する。
    include_superseded: bool = False

    def canonical_document_id(self, document_id: str) -> str:
        """同じ文書とみなす文書（`equivalent_document_ids`）を、正解の文書の ID にそろえる。"""
        for canonical, others in self.equivalent_document_ids.items():
            if document_id in others:
                return canonical
        return document_id

    @property
    def expects_answer(self) -> bool:
        """答えるべき質問か(明示が無ければ、期待する内容を 1 つでも持つか)。"""
        if self.answerable is not None:
            return self.answerable
        return bool(
            self.relevant_document_ids
            or self.standard_answer
            or self.required_evidence
            or self.required_answer_points
        )

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """前後の空白を除き、空の id を拒否する。"""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("評価ケースの id を入力してください。")
        return cleaned

    @field_validator("query")
    @classmethod
    def validate_query(cls, query: str) -> str:
        """SearchRequest と同じ規則で query を正規化する。"""
        return normalize_query_text(query)

    @field_validator("standard_answer")
    @classmethod
    def validate_standard_answer(cls, value: str | None) -> str | None:
        """空白だけの標準回答は無いものとして扱う。"""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("conditions")
    @classmethod
    def validate_conditions(cls, value: dict[str, str]) -> dict[str, str]:
        """条件の id と値の前後の空白を除く。"""
        return _clean_conditions(value)

    @model_validator(mode="after")
    def validate_case_contract(self) -> Self:
        """根拠の id の重複と、往復をまたいだ条件の数の上限（SearchRequest と同じ）を確かめる。"""
        evidence_ids = [item.id for item in self.required_evidence]
        duplicates = sorted({item for item in evidence_ids if evidence_ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"必要な根拠の id が重複しています: {', '.join(duplicates)}")
        if len(self.all_condition_ids()) > EVALUATION_MAX_CONDITIONS:
            raise ValueError(
                f"条件は往復をまたいで {EVALUATION_MAX_CONDITIONS} 個までにしてください。"
            )
        self._validate_reasoning()
        return self

    def _validate_reasoning(self) -> None:
        """多段の質問の種類と段の数の組み合わせを確かめる（#1335）。

        段の数は種類と一緒に書く。1 段の質問（single_hop）は 1 段、橋渡し・文書の中の参照は 2 段
        以上。段ごとの根拠を書くため、必要な根拠があるときは段の数より少なくしない。
        """
        if self.hops is None:
            return
        if self.reasoning_type is None:
            raise ValueError("hops は reasoning_type と一緒に指定してください。")
        if self.reasoning_type == "single_hop" and self.hops != 1:
            raise ValueError("single_hop の質問の hops は 1 にしてください。")
        if self.reasoning_type in EVALUATION_MULTI_HOP_REASONING_TYPES and self.hops < 2:
            raise ValueError(f"{self.reasoning_type} の質問の hops は 2 以上にしてください。")
        if self.required_evidence and len(self.required_evidence) < self.hops:
            raise ValueError(
                "必要な根拠は段ごとに書いてください"
                f"（hops={self.hops}、根拠 {len(self.required_evidence)} 件）。"
            )

    def all_condition_ids(self) -> set[str]:
        """質問と往復で渡す条件の id の集合。"""
        ids = set(self.conditions)
        for turn in self.turns:
            ids.update(turn.conditions)
        return ids


class EvaluationJudgeDiagnostics(BaseModel):
    """標準回答による評価の判定の診断（rag_engine の ``judge_diagnostics``）。

    ``standard_scope_reused`` は比較範囲（標準回答の比較項目）を同じ評価の job の記録から
    使い回したか、``standard_scope_retries`` / ``judge_retries`` は比較範囲の固定 / 照合・
    監査で出力の誤りを指摘して出し直させた回数、``retry_reasons`` はその段と原因の例外の型
    （「standard_scope:InvalidStructuredOutput」）。``standard_scope_dropped`` は、指した
    標準回答の単位の ID がどれも標準回答に無いので比較範囲から外した項目の数。
    ``judge_calls`` は網羅・正確性・主張の判定の LLM の呼び出しの数（再試行を含む。比較範囲の
    固定を除く）、``claim_calls`` はそのうち段落ごとの主張の判定の数、``claim_failures`` は判定を
    完了できず unassessed にした段落の数、``uncited_claims`` は出典が無くモデルを呼ばずに
    unsupported にした段落の数（#3576）。
    """

    standard_scope_reused: bool = False
    standard_scope_retries: int = Field(default=0, ge=0)
    standard_scope_dropped: int = Field(default=0, ge=0)
    judge_retries: int = Field(default=0, ge=0)
    retry_reasons: list[str] = Field(default_factory=list)
    # 判定に使ったモデルの ID（#3574。共通のモデル設定の既定の判定モデルで解決した ID）。
    judge_model_id: str | None = None
    judge_calls: int = Field(default=0, ge=0)
    claim_calls: int = Field(default=0, ge=0)
    claim_failures: int = Field(default=0, ge=0)
    uncited_claims: int = Field(default=0, ge=0)


class EvaluationAnswerJudgement(BaseModel):
    """標準回答による評価(評価の基準の指標と閾値による合否・主張の監査・必要な項目の網羅)の要約。

    詳細(指標ごとの値・主張ごとの判定)は trace_id の回答の記録に保存する(#680)。`status` は
    completed / error / input_too_large / timeout / unavailable(評価の記録が無いなど)。
    """

    status: str
    passed: bool | None = None
    claims_supported: bool | None = None
    answer_correctness: float | None = Field(default=None, ge=0, le=1)
    claim_support_rate: float | None = Field(default=None, ge=0, le=1)
    requirement_coverage: float | None = None
    # 原質問に必要な項目のうち、説明の無い項目があったか(rag_poc の「必要内容欠落」)。
    missing_content: bool | None = None
    message: str | None = None
    # 判定の診断（比較範囲を job の記録から使い回したか・出力の誤りで再試行した回数）。
    # 評価を完了できなかったケースでも残す。判定が記録を返さなかった（時間切れなど）ときは None。
    judge_diagnostics: EvaluationJudgeDiagnostics | None = None
    # 判定のモデルの ID（#3574）。判定を完了できなかった（時間切れ・失敗）ケースにも残す。
    judge_model_id: str | None = None


class EvaluationTurnResult(BaseModel):
    """往復ごとの応答の判断。"""

    turn: int
    trace_id: str
    observed_outcome: EvaluationOutcome | None = None
    outcome_source: str | None = None
    response_decision_accuracy: float | None = None
    expected_decisions: list[str] = Field(default_factory=list)
    elapsed_ms: float


class EvaluationCaseResult(BaseModel):
    """成功したケースの六指標。資料不足は None。"""

    case_id: str
    trace_id: str
    category: EvaluationCaseCategory | None = None
    split: EvaluationCaseSplit | None = None
    reasoning_type: EvaluationReasoningType | None = None
    hops: int | None = None
    status: Literal["success", "error"] = "success"
    retrieved_document_ids: list[str] = Field(default_factory=list)
    relevant_document_ids: list[str] = Field(default_factory=list)
    hit_document_ids: list[str] = Field(default_factory=list)
    required_evidence_recall: float | None = None
    mrr: float | None = None
    answer_correctness: float | None = None
    requirement_coverage: float | None = None
    claim_support_rate: float | None = None
    response_decision_accuracy: float | None = None
    expected_decisions: list[str] = Field(default_factory=list)
    observed_outcome: EvaluationOutcome | None = None
    outcome_source: str | None = None
    missing_evidence: list[str] = Field(default_factory=list)
    turn_results: list[EvaluationTurnResult] = Field(default_factory=list)
    answer_evaluation: EvaluationAnswerJudgement | None = None
    guardrail_warnings: list[str] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)
    diagnostics: SearchDiagnostics = Field(default_factory=SearchDiagnostics)
    elapsed_ms: float
    error_type: str | None = None
    error_stage: str | None = None
    error_message: str | None = None


class EvaluationThresholdFailure(BaseModel):
    """閾値を下回った aggregate metric。保存済みの結果の削除した指標も読めるよう str で受ける。"""

    metric: str
    actual: float
    threshold: float


class EvaluationCategorySummary(BaseModel):
    """ケースの分類ごとの六指標と対象数。"""

    case_count: int
    error_count: int = 0
    metrics: dict[str, float | None] = Field(default_factory=dict)
    metric_case_counts: dict[str, int] = Field(default_factory=dict)


class EvaluationReasoningSummary(BaseModel):
    """多段の質問の種類別・段の数別の結果（#1335）。

    `metrics` は根拠と回答の指標（`EVALUATION_REASONING_METRIC_NAMES`）を、そのケースだけで求めた値
    （測れなければ None）。
    """

    case_count: int
    error_count: int = 0
    metrics: dict[str, float | None] = Field(default_factory=dict)
    metric_case_counts: dict[str, int] = Field(default_factory=dict)


# 種類別・段の数別の内訳に出す指標（#1335）。
EVALUATION_REASONING_METRIC_NAMES: tuple[str, ...] = (*EVALUATION_METRIC_NAMES,)


class EvaluationSplitSummary(BaseModel):
    """区分（dev / holdout）ごとの結果（#1284）。

    `metrics` は全体と同じ指標（と参考の集計 `required_evidence_recall`）を、その区分のケースだけで
    求めた値（測れなければ None）。
    """

    case_count: int
    error_count: int = 0
    metrics: dict[str, float | None] = Field(default_factory=dict)
    metric_case_counts: dict[str, int] = Field(default_factory=dict)
    failure_reason_counts: dict[str, int] = Field(default_factory=dict)
    # 区分の中の多段の質問の種類別・段の数別の内訳（#1335。ケースに種類・段の数があるときだけ）。
    reasoning_type_breakdown: dict[str, EvaluationReasoningSummary] = Field(default_factory=dict)
    hops_breakdown: dict[str, EvaluationReasoningSummary] = Field(default_factory=dict)


class EvaluationMetrics(BaseModel):
    """検索二・回答三・応答判断一の六指標。対象外は None。"""

    metrics_version: str = EVALUATION_METRICS_VERSION
    case_count: int
    error_count: int = 0
    evaluation_suite: str = "standard"
    required_evidence_recall: float | None = None
    mrr: float | None = None
    answer_correctness: float | None = None
    requirement_coverage: float | None = None
    claim_support_rate: float | None = None
    response_decision_accuracy: float | None = None
    metric_case_counts: dict[str, int] = Field(default_factory=dict)
    passed: bool | None = None
    applied_thresholds: dict[str, float] = Field(default_factory=dict)
    threshold_failures: list[EvaluationThresholdFailure] = Field(default_factory=list)
    failure_reason_counts: dict[str, int] = Field(default_factory=dict)
    category_breakdown: dict[str, EvaluationCategorySummary] = Field(default_factory=dict)
    split_breakdown: dict[str, EvaluationSplitSummary] = Field(default_factory=dict)
    reasoning_type_breakdown: dict[str, EvaluationReasoningSummary] = Field(default_factory=dict)
    hops_breakdown: dict[str, EvaluationReasoningSummary] = Field(default_factory=dict)
    decision_breakdown: dict[str, EvaluationReasoningSummary] = Field(default_factory=dict)
    case_results: list[EvaluationCaseResult] = Field(default_factory=list)
    comparison: "EvaluationComparison | None" = None


class EvaluationComparison(BaseModel):
    """比べるために、検索・回答プロファイルの範囲で流した評価の結果（#2402）。"""

    search_answer_profile_id: str
    metrics: EvaluationMetrics


EvaluationMetrics.model_rebuild()


class EvaluationThresholds(BaseModel):
    """現行の六指標にだけ適用する最低値。"""

    model_config = ConfigDict(extra="forbid")
    required_evidence_recall: float | None = Field(default=None, ge=0, le=1)
    mrr: float | None = Field(default=None, ge=0, le=1)
    answer_correctness: float | None = Field(default=None, ge=0, le=1)
    requirement_coverage: float | None = Field(default=None, ge=0, le=1)
    claim_support_rate: float | None = Field(default=None, ge=0, le=1)
    response_decision_accuracy: float | None = Field(default=None, ge=0, le=1)


class EvaluationRagOverrides(BaseModel):
    """評価の実行に一時適用する非 secret の回答設定(#591)。

    回答エンジンの全体の既定(質問拡張戦略・回答生成フロー・近傍 child 数・rerank)と、検索の
    RRF 定数・同じ group から足す child の上限・交差参照で足す chunk(#1280)・ベクトル検索の
    target accuracy を上書きする。
    削除したキー(query_expansion_* / context_window_chars など)は受け付けない。
    """

    model_config = ConfigDict(extra="forbid")

    query_strategy: QueryStrategy | None = None
    answer_flow: AnswerFlow | None = None
    neighbor_child_count: int | None = Field(default=None, ge=0, le=20)
    rerank_enabled: bool | None = None
    rrf_k: int | None = Field(default=None, ge=1, le=1000)
    context_group_max_chunks: int | None = Field(default=None, ge=1, le=20)
    reference_expansion_enabled: bool | None = None
    reference_expansion_max_chunks: int | None = Field(default=None, ge=1, le=20)
    # 実体の 1 段の拡張(#1362)。実体の抽出を選んだ文書で、拡張の有り / 無しを比べる。
    entity_expansion_enabled: bool | None = None
    entity_expansion_max_chunks: int | None = Field(default=None, ge=1, le=20)
    # 関係情報で検索の候補を足す(#2055)。関係情報を作った文書で、有り / 無しを比べる。
    graph_expansion_enabled: bool | None = None
    graph_expansion_max_chunks: int | None = Field(default=None, ge=1, le=20)
    # 章節の拡張(#1436)。拡張の有り / 無し・上限を比べる。
    section_expansion_enabled: bool | None = None
    section_expansion_max_levels: int | None = Field(default=None, ge=1, le=8)
    section_expansion_input_token_percent: int | None = Field(default=None, ge=10, le=90)
    # rerank の後、起点に採る同じ文書の chunk の上限(#2167)。0 は上限なし。
    max_anchors_per_document: int | None = Field(default=None, ge=0, le=20)
    oracle_vector_target_accuracy: int | None = Field(default=None, ge=1, le=100)
    # 回答の LLM の時間の短縮(#1499)。質問拡張戦略 / 根拠の評価 / 生成・監査の推論の量、
    # CRAG の評価の省略、質問拡張の検索文の上限を比べる。
    strategy_reasoning_effort: ReasoningEffortOverride | None = None
    judge_reasoning_effort: ReasoningEffortOverride | None = None
    generation_reasoning_effort: ReasoningEffortOverride | None = None
    # 監査の推論の量・CRAG の評価の入力の圧縮・読む範囲まで読んだ後の評価し直しの省略(#1528)。
    audit_reasoning_effort: ReasoningEffortOverride | None = None
    crag_compact_input: bool | None = None
    # CRAG の評価の候補の短い ID(#1864)。
    crag_short_candidate_ids: bool | None = None
    # 監査の通った判定の reason を空にする・是正の組の回数の上限(#1912)。
    answer_audit_compact_reasons: bool | None = None
    answer_max_correction_rounds: int | None = Field(default=None, ge=0, le=2)
    # 回答の監査の型(#2795)。
    answer_audit_mode: Literal["none", "standard", "detailed"] | None = None
    section_skip_regrade_at_scope: bool | None = None
    crag_skip_on_strong_signals: bool | None = None
    crag_skip_min_rerank_score: float | None = Field(default=None, ge=0.0, le=1.0)
    query_expansion_max_queries: int | None = Field(default=None, ge=1, le=5)


def _reject_duplicate_case_ids(cases: list[EvaluationCase]) -> None:
    """結果のケースを id で区別できるよう、ケースの id の重複を拒否する(#977)。"""
    ids = [case.id for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise ValueError(f"評価ケースの id が重複しています: {', '.join(duplicates)}")


class EvaluationRunRequest(BaseModel):
    """評価実行リクエスト。

    検索・回答プロファイルを指定すると、その設定で評価する。検索の方式(mode)と
    rerank の件数(rerank_top_n)は回答エンジンが使わないため持たない(#591。以前の golden set の
    指定は無視する)。
    """

    cases: list[EvaluationCase] = Field(..., min_length=1)
    # 省略時はチャット・RAG 検索と同じ既定(#3512)。
    top_k: int = Field(
        default_factory=search_top_k_default,
        ge=1,
        le=SEARCH_TOP_K_MAX,
        json_schema_extra=search_top_k_schema,
    )
    filters: dict[str, str] = Field(default_factory=dict)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=200)
    thresholds: EvaluationThresholds | None = None
    suite: EvaluationSuiteName | None = None
    rag_overrides: EvaluationRagOverrides | None = None
    # 検索・回答プロファイル（任意。#1249）。指定しなければ全体の既定で評価する（#301）。
    search_answer_profile_id: str | None = Field(default=None, max_length=128)
    # 同じケースを別の検索・回答プロファイルでも評価する。
    compare_search_answer_profile_id: str | None = Field(default=None, max_length=128)

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, filters: dict[str, str]) -> dict[str, str]:
        """検索評価に使う filters を SearchRequest と同じ規則で正規化する。"""
        return normalize_search_filters(filters)

    @field_validator("knowledge_base_ids")
    @classmethod
    def validate_knowledge_base_ids(cls, values: list[str]) -> list[str]:
        """評価実行のナレッジベース ID を重複排除する。"""
        return normalize_search_id_list(values)

    @model_validator(mode="after")
    def validate_search_options(self) -> Self:
        """SearchRequest と同じ KB 指定制約を適用し、ケースの id の重複を拒否する。"""
        _reject_duplicate_case_ids(self.cases)
        if self.compare_search_answer_profile_id is not None:
            self.compare_search_answer_profile_id = (
                self.compare_search_answer_profile_id.strip() or None
            )
        if self.search_answer_profile_id is not None:
            self.search_answer_profile_id = self.search_answer_profile_id.strip() or None
        if (
            self.compare_search_answer_profile_id
            and self.compare_search_answer_profile_id == self.search_answer_profile_id
        ):
            raise ValueError("比べる検索・回答プロファイルは、評価するものと別にしてください。")
        self.filters, self.knowledge_base_ids = _sync_knowledge_base_filter(
            self.filters,
            self.knowledge_base_ids,
        )
        return self


def _sync_knowledge_base_filter(
    filters: dict[str, str],
    knowledge_base_ids: list[str],
) -> tuple[dict[str, str], list[str]]:
    """評価 request の明示 KB 指定を既存 filters 経路へ同期する。"""
    filter_knowledge_base_ids = parse_search_id_filter(filters.get("knowledge_base_id"))
    if (
        knowledge_base_ids
        and filter_knowledge_base_ids
        and knowledge_base_ids != filter_knowledge_base_ids
    ):
        raise ValueError(
            "knowledge_base_ids と filters.knowledge_base_id は同じ値を指定してください。"
        )
    resolved_knowledge_base_ids = knowledge_base_ids or filter_knowledge_base_ids
    if not resolved_knowledge_base_ids:
        return filters, []
    return (
        {
            **filters,
            "knowledge_base_id": format_search_id_filter(resolved_knowledge_base_ids),
        },
        resolved_knowledge_base_ids,
    )


# run: 品質評価（#390）。calibration: 判定の校正（人手の判定と評価器の一致。#3578。
# 結果は `rag_evaluation_calibrations` に書き、`run_result` は持たない）。
EvaluationJobKind = Literal["run", "calibration"]
EvaluationJobStatus = Literal["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]


class EvaluationJob(BaseModel):
    """品質評価の job（非同期の実行・進捗・取り消し。#390）。

    進捗は、終わったケースの数と今のケースで示す。結果は、成功（`SUCCEEDED`）のときだけ
    `run_result` で返す。
    時間の上限に達して打ち切ったケースは、結果の中で失敗のケースとして返す。query の本文は持たない。
    """

    job_id: str
    kind: EvaluationJobKind
    status: EvaluationJobStatus
    total_cases: int
    completed_cases: int
    current_case_id: str | None = None
    current_case_started_at: datetime | None = None
    # job 全体の時間の上限（秒）。上限に達したら、残りのケースは実行せずに失敗として記録する。
    time_limit_seconds: int
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    run_result: EvaluationMetrics | None = None


class EvaluationImportIssue(BaseModel):
    """評価リクエストの Excel の誤り（#2065）。行・列はファイル全体の誤りでは None。"""

    row: int | None = None
    column: str | None = None
    message: str


class EvaluationImportRow(BaseModel):
    """評価リクエストの Excel の 1 行のプレビュー（#2065）。

    標準回答は長いため、プレビューには先頭だけを返す（評価に使う全文は `cases` に入る）。
    """

    row: int
    case_id: str
    question: str
    evidence: list[str] = Field(default_factory=list)
    standard_answer_preview: str | None = None
    has_standard_answer: bool = False
    expected_response_decision: Literal["answer", "clarify", "hold"] | None = None
    required_answer_points: list[str] = Field(default_factory=list)
    # 回答の過程（#2402）。期待の下書きを作るときに、この全文を下書きの API に渡す。
    answer_process: str | None = None


# 1 回の下書きの API で構造化する行の数の上限（#2402。画面は行を分けて順に送る）。
EVALUATION_PROCESS_MAX_REQUEST_ROWS = 10


class EvaluationProcessRow(BaseModel):
    """期待の下書きを作る評価のケースの 1 行（#2402）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    case_id: str = Field(..., min_length=1, max_length=EVALUATION_CASE_ID_MAX_CHARS)
    row: int = Field(..., ge=1)
    question: str = Field(..., min_length=1, max_length=PROCESS_IMPORT_QUESTION_MAX_CHARS)
    answer_process: str = Field(..., min_length=1, max_length=PROCESS_IMPORT_TEXT_MAX_CHARS)
    standard_answer: str = Field(default="", max_length=PROCESS_IMPORT_TEXT_MAX_CHARS)


class EvaluationProcessDraftRequest(BaseModel):
    """回答の過程から期待の下書きを作る request（#2402）。

    資料の名前は選んだ KB の文書で解決する。
    """

    model_config = ConfigDict(extra="forbid")

    knowledge_base_ids: list[str] = Field(..., min_length=1, max_length=200)
    rows: list[EvaluationProcessRow] = Field(
        ..., min_length=1, max_length=EVALUATION_PROCESS_MAX_REQUEST_ROWS
    )

    @field_validator("knowledge_base_ids")
    @classmethod
    def validate_knowledge_base_ids(cls, values: list[str]) -> list[str]:
        """ナレッジベース ID を重複排除する。"""
        return normalize_search_id_list(values)


class EvaluationProcessDraft(BaseModel):
    """1 行の回答の過程から作った評価の期待の下書き（#2402。保存しない）。

    画面で確かめてから、ケースの期待（手順・条件・対応・正解の文書・必要な根拠）に足す。`references`
    は回答の過程に書いた資料と文書の対応、`warnings` は解決できない資料など、`error` は下書きを
    作れなかった理由（そのときは期待は空）。
    """

    case_id: str
    row: int
    required_answer_points: list[str] = Field(default_factory=list)
    required_conditions: list[str] = Field(default_factory=list)
    expected_outcomes: list[EvaluationOutcome] = Field(default_factory=list)
    relevant_document_ids: list[str] = Field(default_factory=list)
    required_evidence: list[EvaluationEvidence] = Field(default_factory=list)
    references: list[SupportGuideProcessReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class EvaluationProcessDraftData(BaseModel):
    drafts: list[EvaluationProcessDraft]


class EvaluationImportResult(BaseModel):
    """評価リクエストの Excel の検証の結果（#2065。保存しない）。

    誤り（`errors`）が 1 つでもあれば `cases` は空で、評価を実行できない。`error_count` は誤りの
    総数（`errors` は先頭の一部）。警告（`warnings`）があっても評価は実行できる。`document_names` は
    根拠に書いた文書の ID → ファイル名（結果の表示用）。
    """

    file_name: str
    row_count: int
    cases: list[EvaluationCase] = Field(default_factory=list)
    rows: list[EvaluationImportRow] = Field(default_factory=list)
    errors: list[EvaluationImportIssue] = Field(default_factory=list)
    error_count: int = 0
    # 警告（#2086）。評価は実行できる（形式に合わない位置の条件・同じ名前の文書など）。
    warnings: list[EvaluationImportIssue] = Field(default_factory=list)
    warning_count: int = 0
    search_answer_profile_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list)
    document_names: dict[str, str] = Field(default_factory=dict)
    max_rows: int
    max_bytes: int


class EvaluationResultExportRequest(BaseModel):
    """実行時に画面へ保持した入力。指標は所有者を検証した job から取得する。"""

    rows: list[EvaluationImportRow] = Field(default_factory=list, max_length=200)
    cases: list[EvaluationCase] = Field(default_factory=list, max_length=200)
    profile_name: str | None = Field(default=None, max_length=256)
    compare_profile_name: str | None = Field(default=None, max_length=256)
