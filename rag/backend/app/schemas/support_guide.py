"""業務ガイド（SupportGuide。#1237、handoff §6.2）の内容と API の型。

業務ガイドは、答えそのものではなく「どの条件で・どの資料を・どの順で確かめれば答えに届くか」を示す
宣言的な内容。検索・回答プロファイルの知識として持ち、下書きを検証して公開した版だけを回答に使う。
分岐は宣言的な条件（equals / in / unknown）に限り、式や script は評価しない。実在の顧客の内容は
配備先で登録し、リポジトリには架空の例だけを置く。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.search import RetrievedChunk

SUPPORT_GUIDE_SCHEMA_VERSION: Literal[2] = 2
# 使ってよい道具（handoff §10。手順の allowed_tools はこの中から選ぶ）。
SUPPORT_GUIDE_TOOLS: tuple[str, ...] = (
    "rag_search",
    "rag_read_source",
    "rag_retrieve_evidence",
    "nl2sql_query",
)
# 1 つの資料の頁の範囲・1 つの手順の参照の数の上限（#2395）。
MAX_REFERENCE_PAGE_RANGES = 10
MAX_STEP_REFERENCES = 10
MAX_PAGE_NUMBER = 100_000
# 表計算の行の番号の上限（Excel のシートの行の数）と、セルの範囲の形（A1 形式。#2663）。
MAX_SHEET_ROW = 1_048_576
MAX_SHEET_COLUMN = 16_384
SUPPORT_GUIDE_BOOLEAN_VALUES = {
    "true": "はい",
    "yes": "はい",
    "はい": "はい",
    "false": "いいえ",
    "no": "いいえ",
    "いいえ": "いいえ",
}
CELL_RANGE_PATTERN = re.compile(r"^([A-Z]{1,3})([1-9]\d{0,6})(?::([A-Z]{1,3})([1-9]\d{0,6}))?$")
_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
_SHORT = 200
_TEXT = 2000

SupportGuideStatus = Literal["active", "archived"]
ConditionType = Literal["enum", "text", "boolean"]
ConditionSource = Literal["user", "document", "tool"]
UnknownHandling = Literal["ask", "branch", "handoff"]
ImpactScope = Literal["individual", "group", "all"]
BranchOperator = Literal["equals", "in", "unknown"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _clean_list(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class SupportGuideGoal(_Strict):
    expected_result: str = Field(min_length=1, max_length=_TEXT, description="期待する結果。")
    intent_examples: list[str] = Field(
        default_factory=list, max_length=20, description="この業務ガイドに当たる質問の例。"
    )
    match_terms: list[str] = Field(
        default_factory=list, max_length=30, description="照合の手がかりの語。"
    )

    _clean = field_validator("intent_examples", "match_terms")(_clean_list)


class SupportGuideApplicability(_Strict):
    """適用範囲。空の項目は「このプロファイルの中」を意味し、全範囲を意味しない。"""

    business_domains: list[str] = Field(default_factory=list, max_length=20)
    object_types: list[str] = Field(default_factory=list, max_length=20)
    versions: list[str] = Field(
        default_factory=list, max_length=20, description="資料・システムの版の条件。"
    )
    effective_from: date | None = None
    effective_to: date | None = None

    _clean = field_validator("business_domains", "object_types", "versions")(_clean_list)

    @model_validator(mode="after")
    def _valid_period(self) -> SupportGuideApplicability:
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("有効期間の終わりは始まりより後の日付にしてください。")
        return self


class SupportGuideCondition(_Strict):
    id: str = Field(pattern=_ID_PATTERN)
    label: str = Field(min_length=1, max_length=_SHORT)
    type: ConditionType = "enum"
    allowed_values: list[str] = Field(default_factory=list, max_length=20)
    required: bool = True
    source: ConditionSource = Field(default="user", description="値の出所（利用者・資料・道具）。")
    unknown_handling: UnknownHandling = Field(
        default="ask", description="分からないとき: ask=確かめる / branch=分岐 / handoff=人へ。"
    )
    question: str = Field(default="", max_length=_SHORT, description="利用者に確かめる問い。")
    value_aliases: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "選択肢の言い換え（選択肢 → 質問に出る語）。例: 個別 → 検証用アカウント。質問にこの語が"
            "出れば、その選択肢が分かっているとみなし、聞き直さない。"
        ),
    )

    _clean = field_validator("allowed_values")(_clean_list)

    @field_validator("value_aliases")
    @classmethod
    def _clean_aliases(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        cleaned = {
            key.strip(): _clean_list(words)
            for key, words in value.items()
            if key.strip() and _clean_list(words)
        }
        if len(cleaned) > 20 or any(len(words) > 20 for words in cleaned.values()):
            raise ValueError("言い換えは選択肢ごとに 20 語まで、20 個の選択肢までです。")
        return cleaned

    @model_validator(mode="after")
    def _valid_values(self) -> SupportGuideCondition:
        if self.type == "enum" and len(self.allowed_values) < 2:
            raise ValueError(f"条件「{self.label}」は選択肢を 2 つ以上入れてください。")
        if self.type != "enum" and self.allowed_values:
            raise ValueError(
                f"条件「{self.label}」の選択肢は、型が選択（enum）のときだけ使えます。"
            )
        if self.unknown_handling == "ask" and self.source == "user" and not self.question:
            raise ValueError(f"条件「{self.label}」に確かめる問いを入れてください。")
        unknown = [key for key in self.value_aliases if key not in self.allowed_values]
        if unknown:
            raise ValueError(
                f"条件「{self.label}」の言い換え「{'、'.join(unknown)}」は選択肢にありません。"
            )
        return self


class SupportGuideBranchWhen(_Strict):
    condition_id: str = Field(pattern=_ID_PATTERN)
    operator: BranchOperator = "equals"
    values: list[str] = Field(default_factory=list, max_length=20)

    _clean = field_validator("values")(_clean_list)

    @model_validator(mode="after")
    def _valid_operator(self) -> SupportGuideBranchWhen:
        if self.operator == "unknown" and self.values:
            raise ValueError("unknown の分岐には値を入れません。")
        if self.operator == "equals" and len(self.values) != 1:
            raise ValueError("equals の分岐の値は 1 つです。")
        if self.operator == "in" and not self.values:
            raise ValueError("in の分岐には値を 1 つ以上入れてください。")
        return self


class SupportGuideBranch(_Strict):
    id: str = Field(pattern=_ID_PATTERN)
    when: SupportGuideBranchWhen
    goto_step: str = Field(pattern=_ID_PATTERN)
    note: str = Field(default="", max_length=_SHORT)


class SupportGuidePageRange(_Strict):
    """参照する資料の頁の範囲（両端を含む）。``page_end`` を省けば 1 頁。"""

    page_start: int = Field(ge=1, le=MAX_PAGE_NUMBER)
    page_end: int | None = Field(default=None, ge=1, le=MAX_PAGE_NUMBER)

    @model_validator(mode="after")
    def _ordered(self) -> SupportGuidePageRange:
        if self.page_end is None:
            self.page_end = self.page_start
        if self.page_start > self.page_end:
            raise ValueError("頁の範囲の終わりは始まり以降の頁にしてください。")
        return self


def _cell_rows(cell_range: str) -> tuple[int, int] | None:
    match = CELL_RANGE_PATTERN.match(cell_range)
    if match is None:
        return None
    start = int(match.group(2))
    end = int(match.group(4) or start)
    columns = (match.group(1), match.group(3) or match.group(1))
    column_numbers = []
    for column in columns:
        number = 0
        for char in column:
            number = number * 26 + ord(char) - ord("A") + 1
        if number > MAX_SHEET_COLUMN:
            return None
        column_numbers.append(number)
    if start > end or column_numbers[0] > column_numbers[1]:
        return None
    if max(start, end) > MAX_SHEET_ROW:
        return None
    return min(start, end), max(start, end)


class SupportGuideSheetRange(_Strict):
    """参照する表計算の資料のシートと行の範囲（両端を含む。#2663）。

    行を省けばシートの全体。``cell_range``（A1 形式。例: ``B3:D10``）を書けば、行はセルの範囲の行に
    する。頁（``SupportGuidePageRange``）とは別の位置で、行の番号を頁として扱わない。
    """

    sheet_name: str = Field(min_length=1, max_length=_SHORT)
    row_start: int | None = Field(default=None, ge=1, le=MAX_SHEET_ROW)
    row_end: int | None = Field(default=None, ge=1, le=MAX_SHEET_ROW)
    cell_range: str = Field(default="", max_length=32, description="A1 形式のセルの範囲。")

    @model_validator(mode="after")
    def _ordered(self) -> SupportGuideSheetRange:
        if self.cell_range:
            self.cell_range = unicodedata.normalize("NFKC", self.cell_range).upper().strip()
            rows = _cell_rows(self.cell_range)
            if rows is None:
                raise ValueError(
                    "セルの範囲は A1 形式（例: B3:D10）で、"
                    "左上から右下の順で Excel の行・列の上限内にしてください。"
                )
            if self.row_start is None:
                self.row_start, self.row_end = rows
            elif (self.row_start, self.row_end or self.row_start) != rows:
                raise ValueError("行の範囲をセルの範囲の行に合わせてください。")
        if self.row_start is None:
            if self.row_end is not None:
                raise ValueError("行の範囲は始まりの行から書いてください。")
            return self
        if self.row_end is None:
            self.row_end = self.row_start
        if self.row_start > self.row_end:
            raise ValueError("行の範囲の終わりは始まり以降の行にしてください。")
        return self


class SupportGuideReference(_Strict):
    document_id: str = Field(min_length=1, max_length=64)
    title: str = Field(
        default="", max_length=_SHORT, description="表示名（参照の正本は document_id）。"
    )
    section_path: list[str] = Field(default_factory=list, max_length=10)
    version: str = Field(default="", max_length=64)
    page_ranges: list[SupportGuidePageRange] = Field(
        default_factory=list,
        max_length=MAX_REFERENCE_PAGE_RANGES,
        description=(
            "根拠にする頁の範囲（#2395）。空なら文書の全体。回答では手順ごとの検索をこの頁に絞り、"
            "当たった頁の根拠を順位に関係なく回答の材料に入れる。"
        ),
    )
    sheet_ranges: list[SupportGuideSheetRange] = Field(
        default_factory=list,
        max_length=MAX_REFERENCE_PAGE_RANGES,
        description=(
            "表計算の資料の根拠にするシートと行の範囲（#2663）。頁の範囲とは同時に書かない。"
            "回答では手順ごとの検索をこのシートの行に絞る。"
        ),
    )

    @model_validator(mode="after")
    def _one_location_kind(self) -> SupportGuideReference:
        if self.page_ranges and self.sheet_ranges:
            raise ValueError(
                "1 つの資料には、頁の範囲か表計算のシートの行の範囲のどちらかだけを書いてください。"
            )
        return self


class SupportGuideStep(_Strict):
    id: str = Field(pattern=_ID_PATTERN)
    title: str = Field(min_length=1, max_length=_SHORT)
    purpose: str = Field(default="", max_length=_TEXT)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    retrieval_hints: list[str] = Field(default_factory=list, max_length=10)
    evidence_requirements: list[str] = Field(
        default_factory=list, max_length=10, description="この手順で示すべき根拠。"
    )
    allowed_tools: list[str] = Field(default_factory=list, max_length=len(SUPPORT_GUIDE_TOOLS))
    done_when: str = Field(default="", max_length=_TEXT, description="この手順の終わりの条件。")
    references: list[SupportGuideReference] = Field(
        default_factory=list,
        max_length=MAX_STEP_REFERENCES,
        description=(
            "この手順が参照する資料と頁（#2395）。ガイドの references は全手順の共通の資料で、"
            "ここには手順だけの資料を書く。"
        ),
    )

    _clean = field_validator(
        "depends_on", "retrieval_hints", "evidence_requirements", "allowed_tools"
    )(_clean_list)


class SupportGuideCompletion(_Strict):
    id: str = Field(pattern=_ID_PATTERN)
    description: str = Field(min_length=1, max_length=_TEXT)
    check_method: str = Field(default="", max_length=_TEXT)


class SupportGuideImpact(_Strict):
    scope: ImpactScope = "individual"
    approval_required: bool = False
    approval_note: str = Field(default="", max_length=_TEXT)
    steps: list[str] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "影響範囲と承認が係る手順の id（#1320）。空なら業務ガイドのすべての場合に係る。"
            "分かっている条件でこの手順がすべて当たらない（外れた分岐の手順だけ）なら、"
            "影響範囲・承認を求めない。例: グループに付与する分岐の手順だけに承認が要る。"
        ),
    )

    _clean = field_validator("steps")(_clean_list)


class SupportGuideHandoff(_Strict):
    conditions: list[str] = Field(
        default_factory=list, max_length=20, description="人へ引き継ぐ条件。"
    )
    contact: str = Field(default="", max_length=_SHORT, description="引き継ぎ先（窓口の名前）。")

    _clean = field_validator("conditions")(_clean_list)


class SupportGuideContent(_Strict):
    """業務ガイドの内容（下書き・公開の版で同じ形）。"""

    schema_version: Literal[2] = SUPPORT_GUIDE_SCHEMA_VERSION
    title: str = Field(min_length=1, max_length=_SHORT)
    description: str = Field(default="", max_length=_TEXT)
    goal: SupportGuideGoal
    applicability: SupportGuideApplicability = Field(default_factory=SupportGuideApplicability)
    conditions: list[SupportGuideCondition] = Field(default_factory=list, max_length=30)
    steps: list[SupportGuideStep] = Field(default_factory=list, max_length=50)
    branches: list[SupportGuideBranch] = Field(default_factory=list, max_length=50)
    references: list[SupportGuideReference] = Field(default_factory=list, max_length=30)
    completion: list[SupportGuideCompletion] = Field(default_factory=list, max_length=20)
    impact: SupportGuideImpact = Field(default_factory=SupportGuideImpact)
    handoff: SupportGuideHandoff = Field(default_factory=SupportGuideHandoff)


class SupportGuideIssue(BaseModel):
    """検証で見つけた問題。severity=error は公開できない。"""

    severity: Literal["error", "warning"]
    code: str
    path: str = Field(description="問題のある項目（例: steps[1].depends_on）。")
    message: str


class SupportGuideRevisionSummary(BaseModel):
    revision: int
    title: str
    content_sha256: str
    published_at: datetime
    published_by: str | None = None
    rollback_from: int | None = None


class SupportGuideRevision(SupportGuideRevisionSummary):
    content: SupportGuideContent


class SupportGuideSummary(BaseModel):
    guide_id: str
    search_answer_profile_id: str
    status: SupportGuideStatus
    title: str
    draft_revision: int
    published_revision: int | None = None
    # 下書きが公開の版と違うか（公開していなければ True）。
    has_unpublished_changes: bool
    updated_at: datetime
    updated_by: str | None = None


class SupportGuideDetail(SupportGuideSummary):
    draft: SupportGuideContent
    published: SupportGuideContent | None = None
    revisions: list[SupportGuideRevisionSummary] = Field(default_factory=list)
    # 公開の履歴に残す版の数（画面の公開の履歴の案内に出す。#2356）。
    max_published_revisions: int = 5
    issues: list[SupportGuideIssue] = Field(default_factory=list)


class SupportGuideCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: SupportGuideContent


class SupportGuideDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: SupportGuideContent
    base_revision: int = Field(ge=1, description="読み込んだときの draft_revision。")


class SupportGuidePublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=1, description="公開する下書きの draft_revision。")


class SupportGuideRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1, description="戻す公開の版。新しい版として公開する。")
    base_revision: int = Field(
        ge=1,
        description=(
            "読み込んだときの draft_revision（下書きを戻す版の内容で置き換えるため照合する）。"
        ),
    )


class SupportGuideValidationData(BaseModel):
    valid: bool
    issues: list[SupportGuideIssue]


class SupportGuideImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guides: list[dict[str, object]] = Field(min_length=1, max_length=100)


# 取込の差分（#1288）の節。basic = 題名・説明。
SupportGuideDiffSection = Literal[
    "basic",
    "goal",
    "applicability",
    "conditions",
    "steps",
    "branches",
    "references",
    "completion",
    "impact",
    "handoff",
]
SupportGuideChangeKind = Literal["added", "removed", "changed"]


class SupportGuideChange(BaseModel):
    """既存のガイドと取り込むガイドの違い 1 つ。"""

    section: SupportGuideDiffSection
    kind: SupportGuideChangeKind
    key: str = Field(default="", description="行の id（資料は document_id）。行の無い節は空。")
    label: str = Field(default="", description="表示名（条件の名前・手順の題名など）。")
    fields: list[str] = Field(
        default_factory=list, description="変わった項目（changed のときだけ）。"
    )


class SupportGuideImportDiff(BaseModel):
    """取り込むガイドと同じ id・名前の既存のガイドとの差分（#1288）。

    取り込みは既存のガイドを書き換えず、別の下書きとして作る。差分は比べるための表示だけ。
    """

    guide_id: str
    title: str
    matched_by: Literal["id", "title"]
    # 比べた既存の内容（公開の版があれば公開の版、無ければ下書き）。
    base: Literal["published", "draft"]
    revision: int
    status: SupportGuideStatus
    changes: list[SupportGuideChange]


class SupportGuideImportItem(BaseModel):
    index: int
    title: str | None = None
    valid: bool
    issues: list[SupportGuideIssue]
    existing: SupportGuideImportDiff | None = None


class SupportGuideImportPreviewData(BaseModel):
    items: list[SupportGuideImportItem]
    importable_count: int


class SupportGuideImportData(BaseModel):
    created: list[SupportGuideSummary]


class SupportGuideDraftTryRequest(BaseModel):
    """下書きで試す（#1288）。保存した下書きの版を指定し、公開の版を変えずに回答を作る。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=2000)
    draft_revision: int = Field(ge=1, description="試す下書きの draft_revision。")
    conditions: dict[str, str] = Field(
        default_factory=dict, description="分かっている条件の値（条件 id → 値）。"
    )

    @field_validator("conditions")
    @classmethod
    def _limit_conditions(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 30:
            raise ValueError("条件は 30 個までです。")
        return value


# 業務ガイドの手順の充足（#2491。rag_engine の guide_step_coverage の状態）。
SupportGuideStepStatus = Literal[
    "explained", "partial", "unknown", "reference_only", "unused", "no_evidence", "not_searched"
]
SUPPORT_GUIDE_STEP_STATUSES: tuple[str, ...] = get_args(SupportGuideStepStatus)


class SupportGuideDraftTryStep(BaseModel):
    """下書きで試した回答の、業務ガイドの手順ごとの充足（#2686）。"""

    step_id: str
    title: str
    status: SupportGuideStepStatus = Field(
        description=(
            "explained=手順の必須要件すべてを根拠で裏付けた説明として確認した / "
            "partial=手順の一部だけ説明を確認した / unknown=説明の充足を検証できていない / "
            "reference_only=根拠を原文のまま示しただけ / "
            "unused=根拠は回答の材料にあるが説明に引いていない / no_evidence=根拠が材料に無い / "
            "not_searched=手順ごとの検索を時間・回数の上限で打ち切り、検索しなかった。"
        )
    )
    required: bool = Field(
        description="回答の充足で必須の手順か（条件が決まらない分岐の手順は False）。"
    )


class SupportGuideDraftTryData(BaseModel):
    """下書きで試した回答。

    回答の記録には ``guide_preview`` を残し、利用者の履歴・評価に混ぜない。記録は
    ``GET /api/search/answers/{trace_id}`` で読める（回答の履歴の一覧には出ない）。
    """

    trace_id: str
    guide_id: str
    draft_revision: int
    published_revision: int | None = None
    # この下書きのガイドが回答に使われたか（別のガイド・ガイドなしで答えたときは False）。
    guide_used: bool
    # 回答に使った業務ガイドの要約（guide_id・版・判断・既知 / 不明の条件）。無ければ None。
    guide: dict[str, Any] | None = None
    outcome: str | None = None
    notice: str | None = None
    answer: str
    citations: list[RetrievedChunk] = Field(default_factory=list)
    clarifications: list[dict[str, Any]] = Field(default_factory=list)
    # この下書きの業務ガイドの手順ごとの充足（#2686）。ガイドを使わなかった・手順で答えなかった
    # （確認・引き継ぎ）ときは空。
    steps: list[SupportGuideDraftTryStep] = Field(default_factory=list)
    elapsed_ms: float


class SupportGuideExportData(BaseModel):
    schema_version: Literal[2] = SUPPORT_GUIDE_SCHEMA_VERSION
    guides: list[SupportGuideContent]


# ---- 回答の過程の表からの下書き（#2397）----------------------------------------------------

PROCESS_IMPORT_QUESTION_MAX_CHARS = 2000
PROCESS_IMPORT_TEXT_MAX_CHARS = 8000
PROCESS_IMPORT_MAX_REQUEST_ROWS = 100


class SupportGuideProcessRow(BaseModel):
    """回答の過程の表の 1 行（Excel の行か、貼り付けたテキスト）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    row: int = Field(ge=1, description="表の行の番号（貼り付けたテキストは 1）。")
    question: str = Field(min_length=1, max_length=PROCESS_IMPORT_QUESTION_MAX_CHARS)
    process: str = Field(
        min_length=1, max_length=PROCESS_IMPORT_TEXT_MAX_CHARS, description="回答の過程。"
    )
    standard_answer: str = Field(default="", max_length=PROCESS_IMPORT_TEXT_MAX_CHARS)


class SupportGuideProcessColumn(BaseModel):
    index: int = Field(description="列の位置（0 始まり）。")
    letter: str = Field(description="Excel の列の記号（A・B…）。")
    header: str = Field(description="1 行目の見出し（空なら空）。")


class SupportGuideProcessSheet(BaseModel):
    name: str
    columns: list[SupportGuideProcessColumn]
    row_count: int = Field(description="2 行目からの、空でない行の数。")


class SupportGuideProcessWorkbookData(BaseModel):
    """Excel のシートと列（列を選ぶ前）と、選んだ列で読んだ行（列を選んだとき）。"""

    file_name: str
    sheets: list[SupportGuideProcessSheet]
    sheet: str | None = None
    rows: list[SupportGuideProcessRow] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list, description="読めなかった行と理由。")
    max_rows: int = Field(description="1 回で下書きにする行の数の上限。")
    # 上限より多い行があり、開始の行から上限の数だけ読んだ。
    truncated: bool = False
    next_start_row: int | None = Field(
        default=None, description="truncated のとき、続きを読む開始の行。"
    )


class SupportGuideProcessPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[SupportGuideProcessRow] = Field(
        min_length=1, max_length=PROCESS_IMPORT_MAX_REQUEST_ROWS
    )


class SupportGuideProcessReference(BaseModel):
    """回答の過程に書いた資料と、ナレッジベースの文書との対応。"""

    name: str = Field(description="回答の過程に書いた資料の名前。")
    pages: str = Field(default="", description="回答の過程に書いた頁（書いたまま）。")
    sheet_name: str = Field(default="", description="回答の過程に書いたシート名（書いたまま）。")
    rows: str = Field(default="", description="回答の過程に書いた表計算の行（書いたまま）。")
    cells: str = Field(default="", description="回答の過程に書いたセルの範囲（書いたまま）。")
    section: str = Field(default="", description="回答の過程に書いた章節（書いたまま）。")
    page_ranges: list[SupportGuidePageRange] = Field(
        default_factory=list, description="頁から読んだ頁の範囲（解決した資料だけ）。"
    )
    sheet_ranges: list[SupportGuideSheetRange] = Field(
        default_factory=list,
        description="シート・行・セルから読んだ表計算の範囲（解決した表計算の資料だけ。#2663）。",
    )
    step_ids: list[str] = Field(
        default_factory=list,
        description="この資料を根拠にする手順の id（空なら全手順に共通の資料。#2395）。",
    )
    status: Literal["resolved", "unresolved", "ambiguous"]
    document_id: str | None = None
    file_name: str | None = None
    candidates: list[str] = Field(
        default_factory=list, description="ambiguous のとき、当たった文書の名前。"
    )


class SupportGuideRuleCandidate(BaseModel):
    """補足から作ったルールの下書きの候補（質問の種類 → 案内・言い切らない・区別の規則）。"""

    title: str = Field(max_length=_SHORT)
    triggers: list[str] = Field(default_factory=list, description="ルールに当てる語。")
    content: str = Field(max_length=_TEXT)


class SupportGuideProcessItem(BaseModel):
    """1 行から作った業務ガイドの下書き（取り込めるか・資料の対応・ルールの候補）。"""

    row: int
    question: str
    valid: bool
    content: SupportGuideContent | None = None
    issues: list[SupportGuideIssue] = Field(default_factory=list)
    references: list[SupportGuideProcessReference] = Field(default_factory=list)
    rule_candidates: list[SupportGuideRuleCandidate] = Field(default_factory=list)


class SupportGuideProcessPreviewData(BaseModel):
    items: list[SupportGuideProcessItem]
    importable_count: int
