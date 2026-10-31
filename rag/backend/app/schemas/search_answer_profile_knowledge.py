"""検索・回答プロファイル単位の知識(ドメインキーワード等)の API schema。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.schemas.common import JsonValue


class DomainKeywordsData(BaseModel):
    """検索・回答プロファイルのドメインキーワード。"""

    search_answer_profile_id: str
    keywords: list[str] = Field(default_factory=list)


class DomainKeywordsUpdate(BaseModel):
    """ドメインキーワードの全置換。空白・重複・長すぎる語は保存時に除く。"""

    keywords: list[str] = Field(default_factory=list, max_length=2000)


class DomainKeywordImportRowData(BaseModel):
    row: int
    keyword: str


class DomainKeywordImportPreviewData(BaseModel):
    total: int
    rows: list[DomainKeywordImportRowData]


class DomainKeywordImportData(DomainKeywordsData):
    inserted_count: int


class DomainKeywordCandidateData(BaseModel):
    keyword: str
    score: float
    frequency: int
    chunk_count: int
    document_count: int


class DomainKeywordSuggestionData(BaseModel):
    """参照 KB のチャンクから抽出したキーワード候補(登録済みは除く)。"""

    candidates: list[DomainKeywordCandidateData] = Field(default_factory=list)
    processed_chunk_count: int = 0


class ApprovedFaqRecordData(BaseModel):
    """承認済み FAQ 1 件(rag_poc ApprovedFaqRecord の表示用射影)。"""

    id: str
    question: str
    answer: str
    alternate_questions: list[str] = Field(default_factory=list)
    status: str = ""


class ApprovedFaqListData(BaseModel):
    search_answer_profile_id: str
    records: list[ApprovedFaqRecordData] = Field(default_factory=list)
    # 回答の前に類似問を提示するか(検索・回答プロファイルごと。未設定はオン。#684)。
    enabled: bool = True


class ApprovedFaqSettingsRequest(BaseModel):
    enabled: bool


class ApprovedFaqAddRequest(BaseModel):
    # 空・空白だけは画面の欄の下と同じ文言で拒否する（#541）。前後の空白は画面と同じく除く。
    question: str = Field(max_length=1000)
    answer: str = Field(max_length=20000)

    @field_validator("question", "answer")
    @classmethod
    def _require_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        if not cleaned:
            label = "質問" if info.field_name == "question" else "回答"
            raise ValueError(f"{label}を入力してください。")
        return cleaned


class ApprovedFaqDeleteRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)


class ApprovedFaqMutationData(ApprovedFaqListData):
    inserted_count: int = 0
    deleted_count: int = 0


class ApprovedFaqImportRowData(BaseModel):
    question: str
    answer: str
    row: int = 0


class ApprovedFaqImportPreviewData(BaseModel):
    """Excel 取込のプレビュー(先頭 10 件)と有効行数。"""

    total: int
    rows: list[ApprovedFaqImportRowData] = Field(default_factory=list)


class ApprovedFaqSuggestRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)
    # chat はチャットの提示(一致度の下限 RAG_APPROVED_FAQ_SUGGESTION_MIN_SCORE。#684)。
    purpose: Literal["search", "chat"] = "search"


class ApprovedFaqSuggestionData(BaseModel):
    """類似問候補。direct=true は FAQ 回答をそのまま使える一致度(0.92 以上)。"""

    id: str
    question: str
    matched_question: str
    answer: str
    score: float
    direct: bool


class ApprovedFaqSuggestionsData(BaseModel):
    suggestions: list[ApprovedFaqSuggestionData] = Field(default_factory=list)


class RuntimeKnowledgeData(BaseModel):
    """検索・回答プロファイルの用語・同義語と回答ルール(rag_poc runtime knowledge の標準形式)。"""

    search_answer_profile_id: str
    terms: list[dict[str, JsonValue]] = Field(default_factory=list)
    rules: list[dict[str, JsonValue]] = Field(default_factory=list)


class RuntimeKnowledgeEditRequest(BaseModel):
    """1 行の追加・更新・削除。selected 未指定は追加。"""

    kind: Literal["terms", "rules"]
    selected: str | None = Field(default=None, max_length=160)
    name: str = Field(
        default="",
        max_length=160,
        description="用語、またはルール ID（ルール追加時に省略すると自動採番）",
    )
    title: str = Field(default="", max_length=160, description="ルール名(rules のみ)")
    labels: str = Field(
        default="", max_length=8000, description="別名 / 照合キーワード(改行・読点区切り)"
    )
    content: str = Field(default="", max_length=8000, description="説明 / ルール内容")
    source: str = Field(default="", max_length=800)
    purpose: Literal["confirmation", "generation", "both"] | None = None
    instruction_kind: Literal["presentation", "requirement", "verification"] | None = None
    exclusive_group: str | None = Field(default=None, max_length=160)
    priority: int | None = Field(default=None, ge=0, le=100, strict=True)
    enabled: bool = True
    delete: bool = False


class RuntimeKnowledgePreviewRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class RuntimeKnowledgePreviewData(BaseModel):
    """照合テスト: 一致した用語・同義語と回答ルールと拡張後の検索文。"""

    expanded_question: str
    matched_terms: list[str] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    conflicting_rules: list[str] = Field(default_factory=list)


class ClarificationSection(BaseModel):
    """確認の選択肢が指す文書の章節。ページは保存したときの章節のページ範囲。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=64)
    document_name: str = Field(default="", max_length=512)
    section_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)


class ClarificationOption(BaseModel):
    """確認の選択肢 1 件。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(max_length=80)
    description: str = Field(default="", max_length=200)
    # 選んだとき補助の検索文に足す語。
    search_terms: list[str] = Field(default_factory=list, max_length=10)
    # 選んだとき回答に渡す前提(例: 「利用者は経費精算画面について質問している」)。
    premise: str = Field(default="", max_length=300)
    # 選んだとき検索を絞る章節(複数可)。
    sections: list[ClarificationSection] = Field(default_factory=list, max_length=20)

    @field_validator("label")
    @classmethod
    def _require_label(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("選択肢の表示名を入力してください。")
        return cleaned

    @field_validator("search_terms")
    @classmethod
    def _clean_terms(cls, values: list[str]) -> list[str]:
        return [value.strip()[:50] for value in values if value.strip()]


class RuleClarification(BaseModel):
    """確認の質問。自由入力のみ、または選択肢2〜8件。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=200)
    multiple: bool = True
    allow_other: bool = True
    options: list[ClarificationOption] = Field(max_length=8)

    @field_validator("question")
    @classmethod
    def _require_question(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("確認の質問を入力してください。")
        return cleaned

    @model_validator(mode="after")
    def _unique_options(self) -> "RuleClarification":
        if len(self.options) == 1 or (not self.options and not self.allow_other):
            raise ValueError("選択肢は2つ以上、または自由入力だけの確認にしてください。")
        ids = [option.id for option in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("選択肢の id が重複しています。")
        return self


class RuleClarificationRequest(BaseModel):
    """ルールの確認の保存。None は確認を外す。"""

    clarification: RuleClarification | None = None


class ClarificationSuggestRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=200)


class ClarificationSuggestionData(BaseModel):
    rule_id: str
    rule_title: str
    clarification: RuleClarification


class ClarificationSuggestionsData(BaseModel):
    """質問に出す確認(無ければ None)。1 つの質問で聞き返すのは 1 回だけ。"""

    suggestion: ClarificationSuggestionData | None = None
    question_context_token: str | None = None


class ClarificationAnswer(BaseModel):
    """チャットで利用者が確認に答えた内容。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=128)
    option_ids: list[str] = Field(default_factory=list, max_length=8)
    other_text: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _require_choice(self) -> "ClarificationAnswer":
        if not self.option_ids and not self.other_text.strip():
            raise ValueError("選択肢を選ぶか、その他を入力してください。")
        return self
