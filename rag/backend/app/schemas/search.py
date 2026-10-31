"""検索（RAG）関連スキーマ。"""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal, Self

from pr_backend_core.api.validation import validation_error_messages
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from app.rag.figure_region import figure_region
from app.schemas.classification import normalize_category_value
from app.schemas.common import JsonValue
from app.search_defaults import SEARCH_TOP_K_DEFAULT as SEARCH_TOP_K_DEFAULT
from app.search_defaults import SEARCH_TOP_K_MAX as SEARCH_TOP_K_MAX
from app.search_defaults import search_top_k_default, search_top_k_schema

# PoweRAG 由来の scalar / 日付 / カテゴリ pre-filter。Oracle AI Database の JSON_VALUE 数値述語・
# TIMESTAMP 範囲・IN 述語へ再マップし、ベクトル/hybrid 検索の候補集合を事前に絞り込む。
SUPPORTED_SEARCH_NUMERIC_RANGE_FILTERS = {
    "page_number_min",
    "page_number_max",
}
SUPPORTED_SEARCH_DATE_RANGE_FILTERS = {
    "uploaded_from",
    "uploaded_to",
    "indexed_from",
    "indexed_to",
}
# 文書の分類(表記の正規化・番号の接頭辞を除いた一致。#547)と有効期間の基準日
# (rag_poc の ClassificationFilter)。
SUPPORTED_SEARCH_CLASSIFICATION_FILTERS = {
    "large_category",
    "middle_category",
    "small_category",
}
SUPPORTED_SEARCH_LIST_FILTERS = {
    "content_kinds",
}
SUPPORTED_SCALAR_SEARCH_FILTER_KEYS = (
    SUPPORTED_SEARCH_NUMERIC_RANGE_FILTERS
    | SUPPORTED_SEARCH_DATE_RANGE_FILTERS
    | SUPPORTED_SEARCH_LIST_FILTERS
)

# 項目抽出の値の条件(#549)。値は条件の JSON 配列の文字列(filters は dict[str, str] のため)。
EXTRACTION_FIELD_FILTER_KEY = "extraction_fields"
MAX_EXTRACTION_FIELD_CONDITIONS = 10

# 文書のページ範囲(章節)の条件(#717)。値は {document_id, page_start, page_end} の JSON 配列の
# 文字列で、どれかの範囲に重なる chunk に絞る(OR)。
PAGE_RANGES_FILTER_KEY = "page_ranges"
MAX_PAGE_RANGES = 20

# 新しい版に置き換えた文書(旧版)を検索に含めるか(#1248)。既定では含めない。
# 値は "true" / "false"(filters は dict[str, str] のため)。旧版・変更点を尋ねるときに
# "true" にする。
INCLUDE_SUPERSEDED_FILTER_KEY = "include_superseded"

SUPPORTED_SEARCH_FILTER_KEYS = {
    EXTRACTION_FIELD_FILTER_KEY,
    PAGE_RANGES_FILTER_KEY,
    INCLUDE_SUPERSEDED_FILTER_KEY,
    "document_id",
    "knowledge_base_id",
    "chunk_set_id",
    "file_name",
    "category_name",
    "status",
    "content_kind",
    "section_title",
    "section_path",
    "source_acl",
    "document_version",
    "as_of",
    *SUPPORTED_SEARCH_CLASSIFICATION_FILTERS,
    *SUPPORTED_SCALAR_SEARCH_FILTER_KEYS,
}
SUPPORTED_SEARCH_STATUS_FILTERS = {
    "UPLOADED",
    "INGESTING",
    "REVIEW",
    "INDEXING",
    "INDEXED",
    "ERROR",
}
SUPPORTED_CONTENT_KIND_FILTERS = {
    "text",
    "list",
    "table",
    "figure",
    "equation",
    "code",
    "email",
    "slide",
    "sheet",
    # 取込機能が生む合成 chunk(項目抽出 / 章節ナビゲーション要約)
    "field",
    "section_summary",
}


# 型ごとに使える演算子(string / bool は一致だけ、number / date は一致と範囲)。
_EXTRACTION_FIELD_OPERATORS: dict[str, frozenset[str]] = {
    "string": frozenset({"eq"}),
    "bool": frozenset({"eq"}),
    "number": frozenset({"eq", "gte", "lte"}),
    "date": frozenset({"eq", "gte", "lte"}),
}


class ExtractionFieldCondition(BaseModel):
    """項目抽出の値の条件 1 件(#549)。`op` は eq(一致)・gte(以上)・lte(以下)。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    value_type: Literal["string", "number", "date", "bool"]
    op: Literal["eq", "gte", "lte"] = "eq"
    value: str = Field(min_length=1, max_length=500)

    @field_validator("name", "value")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("項目の条件の項目名と値は空にできません。")
        return cleaned

    @model_validator(mode="after")
    def _validate_value(self) -> Self:
        """型に合う演算子と値だけを受け付け、値を比較する形へそろえる。"""
        if self.op not in _EXTRACTION_FIELD_OPERATORS[self.value_type]:
            raise ValueError(f"項目の条件の演算子が型に合いません: {self.value_type} {self.op}")
        if self.value_type == "number":
            try:
                number = Decimal(self.value)
            except InvalidOperation as exc:
                raise ValueError(f"項目の条件の値は数値にしてください: {self.value}") from exc
            if not number.is_finite():
                raise ValueError(f"項目の条件の値は数値にしてください: {self.value}")
            self.value = format(number, "f")
        elif self.value_type == "date":
            try:
                self.value = date.fromisoformat(self.value).isoformat()
            except ValueError as exc:
                raise ValueError(
                    f"項目の条件の日付は YYYY-MM-DD で指定してください: {self.value}"
                ) from exc
        elif self.value_type == "bool":
            if self.value.casefold() not in {"true", "false"}:
                raise ValueError(f"項目の条件の値は true か false にしてください: {self.value}")
            self.value = self.value.casefold()
        return self


_EXTRACTION_FIELD_CONDITIONS = TypeAdapter(
    list[ExtractionFieldCondition],
)


def parse_extraction_field_filter(value: str) -> list[ExtractionFieldCondition]:
    """`filters.extraction_fields`(条件の JSON 配列)を検証して条件にする(#549)。"""
    try:
        conditions = _EXTRACTION_FIELD_CONDITIONS.validate_json(value)
    except ValidationError as exc:
        # Pydantic の英語の文は出さず、条件の位置と日本語の文にする（#1065）。
        messages = validation_error_messages(exc.errors(include_input=False))
        raise ValueError("項目の条件の形式が不正です: " + " ".join(messages)) from exc
    if len(conditions) > MAX_EXTRACTION_FIELD_CONDITIONS:
        raise ValueError(f"項目の条件は {MAX_EXTRACTION_FIELD_CONDITIONS} 件までです。")
    return conditions


def _normalize_extraction_field_filter(value: str) -> str:
    """項目の条件を検証し、そろえた JSON 文字列にする(空の配列は条件なし)。"""
    conditions = parse_extraction_field_filter(value)
    if not conditions:
        return ""
    return json.dumps(
        [condition.model_dump() for condition in conditions],
        ensure_ascii=False,
        separators=(",", ":"),
    )


class SearchMode(StrEnum):
    """Oracle AI Database の検索の種類(``OracleClient.hybrid_search`` の ``mode``)。

    回答は回答フローが hybrid(RRF)と vector を内部で使い分ける。利用者が選ぶ
    検索モードは #595 で削除した。
    """

    HYBRID = "hybrid"
    VECTOR = "vector"
    KEYWORD = "keyword"


class SearchRequest(BaseModel):
    """RAG 検索リクエスト。

    旧 standard の回答エンジンの指定(``mode``・``strategy``・``rerank_top_n``・
    ``generation_profile``)は #595 で削除した。旧クライアントが送っても 422 にせず、
    未定義の項目として読み捨てる(pydantic の既定 ``extra="ignore"``)。

    検索・回答プロファイルは 1 つだけ(``search_answer_profile
    _id``)を受ける。複数の ``search_answer_profile_ids
    `` は
    #635 で
    削除した。読み捨てると検索・回答プロファイルの範囲を
    外れて検索してしまうため、送られたら 422 にする。

    """

    model_config = ConfigDict(extra="ignore")

    # サーバーで原文照合した選択の前提。外部入力・API出力には含めない。
    _rule_selection_scope: dict[str, Any] = PrivateAttr(default_factory=dict)
    _rule_selection_state: dict[str, Any] = PrivateAttr(default_factory=dict)

    query: str = Field(..., min_length=1)
    # 利用者の元の質問(#2261)。呼び出し元(業務 Agent)が検索文 query を短く言い換えたときに渡す。
    # 条の目録から文書・条を選ぶ段だけが query と合わせて使う(検索そのものは query で行う)。
    original_question: str | None = Field(default=None, max_length=8000)
    top_k: int = Field(
        default_factory=search_top_k_default,
        ge=1,
        le=SEARCH_TOP_K_MAX,
        json_schema_extra=search_top_k_schema,
    )
    filters: dict[str, str] = Field(default_factory=dict)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=200)
    search_answer_profile_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "検索対象の検索・回答プロファイル(Search Answer Pro"
            "file)ID(1 つ。#635)。指定時は参照 KB 群を検索対象へ"
            "展開し、検索・回答プロファイルの回答の設定を適用する。"
        ),
    )
    retrieval_only: bool = Field(
        default=False,
        description=(
            "回答フローを通らずに検索だけを行う(質問拡張・条の選択・回答の生成の LLM を呼ばない)。"
            "KB の検索テストとレシピの検索比較が使う(#593)。"
        ),
    )
    auto_field_filter_excluded: list[str] = Field(
        default_factory=list,
        max_length=MAX_EXTRACTION_FIELD_CONDITIONS,
        description=(
            "質問から読み取った抽出項目の条件のうち、使わない項目の名前(画面で利用者が外した条件。"
            "#652)。"
        ),
    )
    conditions: dict[str, str] = Field(
        default_factory=dict,
        max_length=30,
        description=(
            "業務ガイドの条件の値（条件の id → 値。#1238）。利用者が確認の質問に答えた値や、"
            "Agent が聞いた値を渡す。分かっている条件は聞き直さない。"
        ),
    )
    generate_answer: bool = Field(
        default=True,
        description=(
            "False なら回答を生成しない(RAG 検索の画面。#649)。根拠を集め終えるまで(質問の理解・"
            "拡張・文書の検索・回答フロー・章節の拡張・要求ごとの根拠の確認)はチャットと同じ工程で、"
            "同じ LLM(質問拡張・条の選択・CRAG の根拠確認など)を呼ぶ。回答の生成と検査はしない"
            "(#3506)。"
        ),
    )
    model_id: str | None = Field(
        default=None,
        max_length=256,
        description=(
            "回答のモデル(RAG 検索の画面で選ぶ既定のテキストモデルか既定の Vision モデル。#675)。"
            "候補外・未指定なら既定のテキストモデル。"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def reject_search_answer_profile_ids(cls, data: object) -> object:
        """削除した ``search_answer_profile_ids`
        ` を黙って読み捨てない(検索・回答プロファイルの外を検索しない)。"""
        if isinstance(data, dict) and any(
            key in data for key in ("business_view_id", "business_view_ids")
        ):
            raise ValueError(
                "対象の旧指定は使えません。search_answer_profile_id を指定してください。"
            )
        if isinstance(data, dict) and "search_answer_profile_ids" in data:
            raise ValueError(
                "search_answer_profile_ids は使えません。"
                "検索・回答プロファイルは search_answer_profile_id で 1 つ指定してください。"
            )
        return data

    @field_validator("search_answer_profile_id")
    @classmethod
    def validate_search_answer_profile_id(cls, value: str | None) -> str | None:
        """空文字は未指定として扱う。"""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("query")
    @classmethod
    def validate_query(cls, query: str) -> str:
        """空白だけのクエリを拒否し、前後空白を落とす。"""
        return normalize_query_text(query)

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, filters: dict[str, str]) -> dict[str, str]:
        """対応済み filter key のみ許可し、値を正規化する。"""
        return normalize_search_filters(filters)

    @field_validator("knowledge_base_ids")
    @classmethod
    def validate_knowledge_base_ids(cls, values: list[str]) -> list[str]:
        """検索対象のナレッジベース ID を重複排除する。"""
        return normalize_search_id_list(values)

    @model_validator(mode="after")
    def validate_search_options(self) -> Self:
        """ナレッジベース指定の整合性を検証する。"""
        filter_knowledge_base_ids = parse_search_id_filter(self.filters.get("knowledge_base_id"))
        if (
            self.knowledge_base_ids
            and filter_knowledge_base_ids
            and self.knowledge_base_ids != filter_knowledge_base_ids
        ):
            raise ValueError(
                "knowledge_base_ids と filters.knowledge_base_id は同じ値を指定してください。"
            )
        resolved_knowledge_base_ids = self.knowledge_base_ids or filter_knowledge_base_ids
        if resolved_knowledge_base_ids:
            self.knowledge_base_ids = resolved_knowledge_base_ids
            self.filters = {
                **self.filters,
                "knowledge_base_id": format_search_id_filter(resolved_knowledge_base_ids),
            }
        return self


class RetrievedChunk(BaseModel):
    """検索でヒットしたチャンク。"""

    document_id: str
    chunk_id: str
    text: str
    score: float
    rerank_score: float | None = None
    file_name: str | None = None
    category_name: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    # 元の図の画像を切り出せるか（図の根拠で、保存した図の領域がある。#2433）。画面はこれが
    # 真のときだけ「図を開く」を出す（MCP の ``image_ref`` と同じ判定）。検索で作るときは chunk の
    # metadata から決め、保存した引用（metadata を絞る）を読み直すときは保存した値を使う。
    figure_image_available: bool = False

    @model_validator(mode="after")
    def _resolve_figure_image_available(self) -> Self:
        if "figure_image_available" not in self.model_fields_set:
            self.figure_image_available = figure_region(dict(self.metadata)) is not None
        return self


class SearchDiagnostics(BaseModel):
    """検索・回答の再現と調査に使う非機密の診断。

    旧 standard の回答エンジンの診断(検索の内訳・候補・context の件数・回答スタイルなど)は
    #595 で削除した。回答の中身の診断は ``answer`` にある。
    """

    # 回答の経路は常に回答フロー。保存済みの評価結果などの古い値も読めるよう str のままにする。
    retrieval_strategy: str = "hybrid"
    # grounded(根拠付き回答)/ retrieval_only(検索だけ)/ blocked(質問の安全チェック)
    retrieval_strategy_adapter: str = "grounded"
    answer: dict[str, JsonValue] | None = None
    guardrails_input_enabled: bool = False
    guardrails_output_enabled: bool = False
    guardrail_degraded: bool = False
    filter_keys: list[str] = Field(default_factory=list)
    knowledge_base_count: int = 0
    kb_adapter_config_applied: str | None = None
    search_answer_profile_applied: str | None = None
    config_fingerprint: str = ""


StorageFailureStep = Literal["answer_record_save", "chat_message_save"]


class StorageFailure(BaseModel):
    """作った回答を保存できなかった記録（回答は返す。#2342）。

    ``step`` は保存できなかったもの（回答の記録 / 会話の回答）、``error_code`` は
    ``ORA-03146`` などのコード（例外の本文・業務データは入れない）。
    """

    step: StorageFailureStep
    error_type: str
    error_code: str | None = None


class SearchResponse(BaseModel):
    """RAG 検索レスポンス。"""

    answer: str
    citations: list[RetrievedChunk] = Field(default_factory=list)
    trace_id: str
    guardrail_warnings: list[str] = Field(default_factory=list)
    elapsed_ms: float
    diagnostics: SearchDiagnostics = Field(default_factory=SearchDiagnostics)
    # 回答側ガードレールが本文をマスク/差し替えしたか。realtime stream 時に
    # マスク済み本文を再送(置換)するかの判定に使う内部フラグ。
    answer_replaced: bool = False
    # 作った回答を保存できなかった記録（回答は返し、画面が「保存できなかった」ことを出す。#2342）。
    storage_failures: list[StorageFailure] = Field(default_factory=list)
    # 回答の注意書き（システム設定の文。#2405）。backend が回答の確定の後に付け、
    # 回答の本文・会話の履歴・回答の検証には入れない。確認の質問・無効のときは None。
    notice: str | None = None
    # 標準回答による評価の入力(回答の記録に保存するものと同じ。#591)。応答には出さず、
    # 品質評価が同じプロセスの中で回答の根拠・引用・実行記録を受け取るためだけに使う。
    _evaluation_input: dict[str, object] | None = PrivateAttr(default=None)

    @property
    def evaluation_input(self) -> dict[str, object] | None:
        """標準回答による評価の入力(回答エンジンが作らなかったときは None)。"""
        return self._evaluation_input

    def with_evaluation_input(self, value: Mapping[str, object] | None) -> Self:
        """標準回答による評価の入力を持たせて返す(同じ object を変更する)。"""
        self._evaluation_input = dict(value) if value is not None else None
        return self

    @field_validator("guardrail_warnings")
    @classmethod
    def _dedup_warnings(cls, value: list[str]) -> list[str]:
        """クエリ側/回答側で同一文言が重複するため順序保持で重複除去する。"""
        return list(dict.fromkeys(value))


def include_superseded_filter_value(value: str | None) -> bool:
    """``include_superseded`` の値を読む("true" / "false"。大文字・小文字は問わない)。"""
    cleaned = (value or "").strip().casefold()
    if cleaned in {"", "false"}:
        return False
    if cleaned == "true":
        return True
    raise ValueError("include_superseded は true または false で指定してください。")


def normalize_search_filters(filters: dict[str, str]) -> dict[str, str]:
    """検索 filter key/value を検証・正規化する。"""
    unsupported = sorted(set(filters) - SUPPORTED_SEARCH_FILTER_KEYS)
    if unsupported:
        raise ValueError(f"未対応の検索フィルターです: {', '.join(unsupported)}")

    normalized: dict[str, str] = {}
    for key, value in filters.items():
        cleaned = value.strip()
        if not cleaned:
            continue
        if key == "status":
            normalized[key] = cleaned.upper()
        elif key == "content_kind":
            normalized[key] = cleaned.casefold()
        elif key == "knowledge_base_id":
            formatted_ids = format_search_id_filter(parse_search_id_filter(cleaned))
            if formatted_ids:
                normalized[key] = formatted_ids
        elif key in SUPPORTED_SEARCH_NUMERIC_RANGE_FILTERS:
            normalized[key] = _normalize_filter_integer(key, cleaned)
        elif key in SUPPORTED_SEARCH_DATE_RANGE_FILTERS:
            normalized[key] = _normalize_filter_date(key, cleaned)
        elif key == "as_of":
            normalized[key] = _normalize_as_of(cleaned)
        elif key in SUPPORTED_SEARCH_LIST_FILTERS:
            if formatted_kinds := _normalize_content_kind_list(cleaned):
                normalized[key] = formatted_kinds
        elif key == EXTRACTION_FIELD_FILTER_KEY:
            if formatted_conditions := _normalize_extraction_field_filter(cleaned):
                normalized[key] = formatted_conditions
        elif key == PAGE_RANGES_FILTER_KEY:
            if formatted_ranges := format_page_ranges(parse_page_ranges(cleaned)):
                normalized[key] = formatted_ranges
        elif key == INCLUDE_SUPERSEDED_FILTER_KEY:
            if include_superseded_filter_value(cleaned):
                normalized[key] = "true"
        elif key in SUPPORTED_SEARCH_CLASSIFICATION_FILTERS:
            # 保存時と同じ表記の正規化(NFKC・空白)。番号の接頭辞は残し、比較の側で外す(#547)。
            if category := normalize_category_value(cleaned):
                normalized[key] = category
        else:
            normalized[key] = cleaned

    if (status := normalized.get("status")) and status not in SUPPORTED_SEARCH_STATUS_FILTERS:
        raise ValueError(f"未対応のファイル状態フィルターです: {status}")
    if (content_kind := normalized.get("content_kind")) and (
        content_kind not in SUPPORTED_CONTENT_KIND_FILTERS
    ):
        raise ValueError(f"未対応の内容種別フィルターです: {content_kind}")
    _validate_filter_range_consistency(normalized)
    return normalized


class PageRange(BaseModel):
    """文書の範囲 1 件。ページ範囲(章節。#717)か、表計算のシートの行の範囲(#2663)。

    ページもシートも無い範囲は文書全体。シートの範囲(``sheet_name``)は chunk のシート名と行
    (``row_start``〜``row_end``)で絞り、ページとは同時に持たない。行が無ければシートの全体。
    """

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=64)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    sheet_name: str | None = Field(default=None, min_length=1, max_length=200)
    row_start: int | None = Field(default=None, ge=1)
    row_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> "PageRange":
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_start > self.page_end
        ):
            raise ValueError("ページ範囲の開始が終了より後ろです。")
        if self.sheet_name is None:
            if self.row_start is not None or self.row_end is not None:
                raise ValueError("行の範囲はシート名と一緒に指定してください。")
            return self
        if self.page_start is not None or self.page_end is not None:
            raise ValueError("シートの範囲とページの範囲は同時に指定できません。")
        if (
            self.row_start is not None
            and self.row_end is not None
            and self.row_start > self.row_end
        ):
            raise ValueError("行の範囲の開始が終了より後ろです。")
        return self


def parse_page_ranges(value: str) -> list[PageRange]:
    """``page_ranges`` の値(JSON 配列の文字列)を読む。形式が違えば ValueError。"""
    try:
        raw = json.loads(value)
    except ValueError as exc:
        raise ValueError("page_ranges は JSON の配列で指定してください。") from exc
    if not isinstance(raw, list):
        raise ValueError("page_ranges は JSON の配列で指定してください。")
    if len(raw) > MAX_PAGE_RANGES:
        raise ValueError(f"page_ranges は {MAX_PAGE_RANGES} 件までです。")
    return [PageRange.model_validate(item) for item in raw]


_SHEET_RANGE_FIELDS = frozenset({"sheet_name", "row_start", "row_end"})


def format_page_ranges(ranges: list[PageRange]) -> str:
    if not ranges:
        return ""
    # シートの範囲でない項目は、ページの範囲の項目だけを書く(従来の形のまま)。
    items = [
        item.model_dump(exclude=None if item.sheet_name is not None else set(_SHEET_RANGE_FIELDS))
        for item in ranges
    ]
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _normalize_as_of(value: str) -> str:
    """有効期間の基準日を YYYY-MM-DD として検証する。"""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"基準日は YYYY-MM-DD で指定してください: {value}") from exc


def _normalize_filter_integer(key: str, value: str) -> str:
    """数値 range filter を整数として検証し、正規化した文字列を返す。"""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"数値フィルターの形式が不正です: {key}={value}") from exc
    if parsed < 0:
        raise ValueError(f"数値フィルターは 0 以上にしてください: {key}={value}")
    return str(parsed)


def _normalize_filter_date(key: str, value: str) -> str:
    """日付 range filter を ISO 8601 として検証し、入力表現を保持して返す。

    Oracle 側で date-only か datetime かを判別して TIMESTAMP へ束ねるため、ここでは
    解析可能性のみ検証し、trim した入力をそのまま保持する。
    """
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"日付フィルターの形式が不正です: {key}={value}") from exc
    return value


def _normalize_content_kind_list(value: str) -> str:
    """content_kinds の list membership filter を正規化する。"""
    seen: set[str] = set()
    kinds: list[str] = []
    for part in value.split(","):
        cleaned = part.strip().casefold()
        if not cleaned or cleaned in seen:
            continue
        if cleaned not in SUPPORTED_CONTENT_KIND_FILTERS:
            raise ValueError(f"未対応の内容種別フィルターです: {cleaned}")
        seen.add(cleaned)
        kinds.append(cleaned)
    return ",".join(kinds)


def _validate_filter_range_consistency(filters: dict[str, str]) -> None:
    """min/max・from/to の範囲が逆転していないか検証する。"""
    low = filters.get("page_number_min")
    high = filters.get("page_number_max")
    if low and high and int(low) > int(high):
        raise ValueError("page_number_min は page_number_max 以下にしてください。")
    for from_key, to_key in (
        ("uploaded_from", "uploaded_to"),
        ("indexed_from", "indexed_to"),
    ):
        start = filters.get(from_key)
        end = filters.get(to_key)
        if start and end and _filter_date_sort_key(start) > _filter_date_sort_key(end):
            raise ValueError(f"{from_key} は {to_key} 以前にしてください。")


def _filter_date_sort_key(value: str) -> datetime:
    """検証済み日付文字列を比較用の tz-aware datetime へ変換する。"""
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def normalize_search_id_list(values: Sequence[str]) -> list[str]:
    """ID リストの前後空白と重複を取り除く。"""
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def parse_search_id_filter(value: str | None) -> list[str]:
    """カンマ区切り filter 値を ID リストへ戻す。"""
    if value is None:
        return []
    return normalize_search_id_list(value.split(","))


def format_search_id_filter(values: Sequence[str]) -> str:
    """ID リストを既存 filters 経路へ渡すための表現へ変換する。"""
    return ",".join(normalize_search_id_list(values))


def normalize_query_text(query: str) -> str:
    """検索・評価に使う自然言語の質問を正規化する。"""
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("質問を入力してください。")
    return cleaned


class AnswerRecordSummary(BaseModel):
    """保存された回答の一覧行(本文・根拠は含めない)。"""

    trace_id: str
    search_answer_profile_id: str | None = None
    surface: Literal["search", "chat"]
    answer_engine: str
    question: str
    rewritten_question: str | None = None
    confidence: str | None = None
    created_at: datetime


class AnswerRecordDetail(AnswerRecordSummary):
    """保存された回答(本文・引用・根拠と実行記録)。"""

    answer: str
    citations: list[RetrievedChunk] = Field(default_factory=list)
    # 保存した引用のうち、見る人が今の権限で見られない根拠の数（抜粋・文書は返さない。#3252）。
    withheld_evidence_count: int = 0
    answer_diagnostics: dict[str, JsonValue] = Field(default_factory=dict)
    # 回答に付けた注意書き（#2405。この機能より前の回答・確認の質問は None）。
    notice: str | None = None
