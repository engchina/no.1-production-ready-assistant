"""schema 駆動の構造化 field/entity 抽出(PoweRAG の LangExtract 抽出 由来)。

PoweRAG は LangExtract で entity/field/関係を抽出する。本モジュールは外部 LangExtract を
導入せず、確定スタック内で再実装する:
- **field schema store**: 抽出対象 field(name/description/value_type)を JSON 永続で定義
  (`extraction-fields.json`、env `RAG_FIELD_SCHEMA_FILE` で上書き。config.py/.env 非依存)。
  これは全体の既定で、ナレッジベースごとの定義(`rag_knowledge_bases.extraction_fields`)が
  あればそちらを使う(#548。`resolve_field_definitions`)。
  定義のファイルが無い(一度も保存していない)環境は、標準の項目(`STANDARD_FIELD_DEFINITIONS`)を
  全体の既定にする(#556)。保存した定義は空でもそのまま使う。
- **抽出**: モデルの API の structured output を注入された抽出器で呼び、`ExtractionField`
  へ Pydantic 検証して保存。検索可能な合成 element も付けて既存 chunking 経路へ流す。
  本文は入力の予算に収まる単位に分けて 1 単位ずつ呼び、候補を統合する(#2720。分け方と統合の
  規則は `app.rag.field_extraction_units`)。

抽出器は注入可能でテストは決定論。外部 LLM provider は導入しない。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pr_backend_core.bounded_calls import BoundedCallFailed, ErrorKind
from pydantic import BaseModel, Field, field_validator
from rag_parser_core.extraction import (
    EXTRACTED_FIELD_CONTENT_KIND,
    EXTRACTED_FIELD_KEY,
    FIELD_SOURCE_ELEMENT_KEY,
    is_derived_content,
)
from rag_parser_core.language import current_pack
from rag_parser_core.structured_records import record_boundary_plain_text

from app.config import BACKEND_ROOT
from app.rag.field_extraction_units import (
    FieldExtractionUnit,
    FieldValueCandidate,
    UnitCandidates,
    merge_field_candidates,
    plan_field_extraction_units,
    split_field_extraction_unit,
)
from app.schemas.extraction import (
    DocumentElement,
    ExtractionArtifactValue,
    ExtractionField,
    StructuredExtraction,
)

FIELD_SCHEMA_FILE_ENV = "RAG_FIELD_SCHEMA_FILE"
DEFAULT_FIELD_SCHEMA_FILE = "extraction-fields.json"
MAX_FIELD_DEFINITIONS = 50

FieldValueType = Literal["string", "number", "date", "bool"]

# 項目抽出の実行結果(#2481)。抽出結果の parser_artifacts に刻み、「定義なしで未実行」
# 「本文なしで未実行」「正常に終わって 0 件」を区別する。抽出の失敗は取込を止めるため刻まない。
FIELD_EXTRACTION_OUTCOME_ARTIFACT_KEY = "field_extraction_outcome"
FieldExtractionStatus = Literal["completed", "no_definitions", "no_text"]

# 失敗の分類(#2720)。画面の文言と診断のログは、この分類と件数だけで作る(本文・値・応答の
# 本文・例外の文は使わない)。
FieldExtractionFailureCategory = Literal[
    "input_too_large",
    "truncated",
    "invalid_response",
    "request_rejected",
    "safety",
    "transient",
    "unknown",
]
_FAILURE_CATEGORY_BY_KIND: dict[ErrorKind, FieldExtractionFailureCategory] = {
    ErrorKind.TOO_LARGE: "input_too_large",
    ErrorKind.TRUNCATED: "truncated",
    ErrorKind.INVALID_OUTPUT: "invalid_response",
    ErrorKind.BAD_REQUEST: "request_rejected",
    ErrorKind.NOT_SUPPORTED: "request_rejected",
    ErrorKind.SAFETY: "safety",
    ErrorKind.RATE_LIMITED: "transient",
    ErrorKind.UNAVAILABLE: "transient",
    ErrorKind.SERVER_ERROR: "transient",
    ErrorKind.NETWORK: "transient",
    ErrorKind.TIMEOUT: "transient",
}
# 入力の上限の超過で分け直す回数(1 回ごとに単位の本文の予算を半分にする)。
MAX_FIELD_EXTRACTION_SPLIT_DEPTH = 3


@dataclass(frozen=True)
class UnitExtraction:
    """1 つの単位の抽出の結果。``reused`` は保存した前の結果を使い直したとき。"""

    candidates: list[FieldValueCandidate]
    reused: bool = False


# 単位の抽出器: 1 つの単位の本文から候補を返す。失敗は実行器の例外(``BoundedCallFailed`` /
# ``BoundedCallAborted``)で出す。
UnitExtractor = Callable[[FieldExtractionUnit], Awaitable[UnitExtraction]]


@dataclass(frozen=True)
class FieldExtractionRun:
    """単位の数の記録(実行結果に刻み、失敗の文言とログにも使う)。"""

    unit_count: int = 0
    succeeded: int = 0
    reused: int = 0
    failed: int = 0
    skipped: int = 0
    resplit: int = 0

    def as_artifact(self) -> dict[str, int]:
        return {
            "unit_count": self.unit_count,
            "succeeded_unit_count": self.succeeded,
            "reused_unit_count": self.reused,
            "failed_unit_count": self.failed,
            "skipped_unit_count": self.skipped,
            "resplit_count": self.resplit,
        }


class FieldExtractionFailed(Exception):
    """一部か全部の単位の抽出が失敗した(途中の結果は公開しない。#2720)。

    ``category`` は最初に失敗した単位の分類、``kind`` / ``status`` はその実行器の分類と HTTP の
    状態。本文・値・応答の本文は持たない。
    """

    def __init__(
        self,
        category: FieldExtractionFailureCategory,
        *,
        run: FieldExtractionRun,
        kind: ErrorKind,
        status: int | None,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.run = run
        self.kind = kind
        self.status = status


class FieldExtractionResponseError(ValueError):
    """抽出器の応答が JSON の配列として読めない(正常な 0 件と区別する。#2481)。"""


class FieldDefinition(BaseModel):
    """抽出したい 1 つの field の宣言。"""

    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=500)
    value_type: FieldValueType = "string"

    @field_validator("name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field name は空にできません。")
        return cleaned


class FieldSchemaStore(BaseModel):
    """field schema 定義ファイルの schema。"""

    version: Literal[1] = 1
    fields: list[FieldDefinition] = Field(default_factory=list, max_length=MAX_FIELD_DEFINITIONS)


# 標準の項目(#556): 一度も保存していない環境の全体の既定。どの文書にも通じる項目だけにし、
# 分類・有効期間・要約など既存の仕組みと重なるものは入れない。説明は抽出の指示としてそのまま
# モデルに渡すため、取る場所・取らないもの・見つからないときの扱いまで書く。
STANDARD_FIELD_DEFINITIONS: tuple[FieldDefinition, ...] = (
    FieldDefinition(
        name="文書の種類",
        description=(
            "文書の種類を短い名詞で答える。規程・細則・手順書・マニュアル・契約書・覚書・報告書・"
            "議事録・提案書・仕様書・通知・案内・申請書・請求書・見積書のうち当てはまるものを優先し、"
            "どれにも当てはまらなければ文書の性質を表す短い名詞(例: 研修資料)にする。"
            "表題・書式・本文の書き方から判断し、判断できなければ省略する。"
        ),
        value_type="string",
    ),
    FieldDefinition(
        name="文書タイトル",
        description=(
            "表紙・1 ページ目の見出し・本文の冒頭に書かれた文書の正式な題名。書かれている表記の"
            "まま答え、要約や言い換えをしない。副題があれば題名の後ろに続ける。ファイル名・"
            "ヘッダー・フッター・章や節の見出しは題名として使わない。"
        ),
        value_type="string",
    ),
    FieldDefinition(
        name="発行日・作成日",
        description=(
            "文書が発行・制定・作成された日付。発行日・制定日・作成日・文書の日付欄の順に優先し、"
            "改訂日しか無ければ最新の改訂日を使う。施行日・有効期限・印刷日・本文で述べる出来事の"
            "日付は使わない。和暦は西暦に直す。年・月・日のどれかが分からなければ省略する。"
        ),
        value_type="date",
    ),
    FieldDefinition(
        name="発行元・作成部署",
        description=(
            "文書を発行・作成した組織名または部署名(会社名と部署名がともにあれば続けて書く)。"
            "表紙・奥付・署名欄・文書の末尾の発行者の欄から取る。宛先・契約の相手方・本文で"
            "言及されるだけの組織は使わない。個人名は含めず、組織名が無ければ省略する。"
        ),
        value_type="string",
    ),
)


def standard_field_definitions() -> list[FieldDefinition]:
    """標準の項目の複製(呼び出し側が変更しても定数を変えない)。"""
    return [field.model_copy() for field in STANDARD_FIELD_DEFINITIONS]


def _field_schema_path() -> Path:
    raw = os.environ.get(FIELD_SCHEMA_FILE_ENV, "").strip() or DEFAULT_FIELD_SCHEMA_FILE
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (BACKEND_ROOT / path).resolve()


def load_saved_field_schema() -> FieldSchemaStore | None:
    """保存した全体の既定を読む。一度も保存していない(ファイルが無い)ときは None(#556)。

    「未設定」と「意図して空」を分けるのはファイルの有無だけ。空の定義を保存したファイルは
    空の store を返す。読めない・壊れているファイルは、保存はしてあるものとして安全に空にする
    (標準の項目へ黙って切り替えない)。
    """
    path = _field_schema_path()
    try:
        data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return FieldSchemaStore()
    try:
        return FieldSchemaStore.model_validate_json(data)
    except ValueError:
        return FieldSchemaStore()


def load_field_schema() -> FieldSchemaStore:
    """全体の既定の定義。保存していなければ標準の項目(#556)。"""
    saved = load_saved_field_schema()
    return FieldSchemaStore(fields=standard_field_definitions()) if saved is None else saved


def validate_field_schema(fields: list[FieldDefinition]) -> FieldSchemaStore:
    """項目の定義を検証する(name の重複は大文字小文字を区別せず不可、上限は 50 項目)。"""
    names = [field.name.casefold() for field in fields]
    if len(names) != len(set(names)):
        raise ValueError("field name は重複できません。")
    if len(fields) > MAX_FIELD_DEFINITIONS:
        raise ValueError(f"項目は {MAX_FIELD_DEFINITIONS} 件までです。")
    return FieldSchemaStore(fields=fields)


def save_field_schema(fields: list[FieldDefinition]) -> FieldSchemaStore:
    """field schema 定義(全体の既定)を保存する(name 重複は不可)。"""
    store = validate_field_schema(fields)
    path = _field_schema_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.model_dump_json(indent=2), encoding="utf-8")
    return store


def reset_field_schema() -> None:
    """保存した全体の既定を消し、未設定(標準の項目を使う状態)に戻す(#556)。"""
    _field_schema_path().unlink(missing_ok=True)


def field_schema_from_json(value: object) -> list[FieldDefinition] | None:
    """KB に保存した項目の定義(`FieldSchemaStore` の JSON)を読む(#548)。

    NULL は「KB の定義なし = 全体の既定に従う」で None。壊れた値も None にして既定に従う
    (取込を止めない)。空の list は「この KB では項目を抽出しない」で、既定には戻さない。
    """
    if value is None:
        return None
    try:
        if isinstance(value, str | bytes):
            return FieldSchemaStore.model_validate_json(value).fields
        return FieldSchemaStore.model_validate(value).fields
    except ValueError:
        return None


def resolve_field_definitions(
    knowledge_base_fields: Sequence[list[FieldDefinition] | None],
    default_fields: list[FieldDefinition],
) -> list[FieldDefinition]:
    """文書が属する KB の定義から、抽出する項目を決める(#548)。

    - KB に定義が無い(None)ときは全体の既定を使う。所属 KB が無いときも全体の既定。
    - 複数の KB に属する文書は、各 KB の定義(無ければ既定)の和集合にする。同じ項目名
      (大文字小文字を区別しない)は先の KB の定義を使う。KB の順は呼び出し側が決める
      (作成の古い順)。
    """
    if not knowledge_base_fields:
        return list(default_fields)
    merged: list[FieldDefinition] = []
    seen: set[str] = set()
    for fields in knowledge_base_fields:
        for field in default_fields if fields is None else fields:
            key = field.name.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(field)
    return merged


def _date_pattern() -> re.Pattern[str]:
    """日付（「2024-04-01」「2024年4月1日」）。年・月・日の語は言語資源（labels）。"""
    pack = current_pack()
    year, month, day = (re.escape(pack.text("labels", key)) for key in ("year", "month", "day"))
    return re.compile(
        rf"^(\d{{4}})\s*(?:[-/.]|{year})\s*(\d{{1,2}})\s*(?:[-/.]|{month})\s*(\d{{1,2}})\s*(?:{day})?"
    )


def normalize_field_value(value_type: FieldValueType, raw: str) -> str:
    """抽出値を型ごとの比較できる形へ寄せる(#549。検索の絞り込みで型どおりに比べるため)。

    number は桁区切り・空白を除いた数字、date は YYYY-MM-DD、bool は true / false にする。
    寄せられない値はそのまま返す(検索の文としては使えるが、範囲の絞り込みでは一致しない)。
    """
    text = unicodedata.normalize("NFKC", raw).strip()
    if value_type == "number":
        candidate = re.sub(r"[,\s]", "", text)
        try:
            number = Decimal(candidate)
        except InvalidOperation:
            return raw
        return format(number, "f") if number.is_finite() else raw
    if value_type == "date":
        match = _date_pattern().match(text)
        if match is None:
            return raw
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return raw
    if value_type == "bool":
        folded = text.casefold()
        if folded in current_pack().words("field_value_cues", "true_values"):
            return "true"
        if folded in current_pack().words("field_value_cues", "false_values"):
            return "false"
    return raw


def find_json_array(raw: str) -> str | None:
    """LLM 出力から JSON 配列部分だけを取り出す(code fence・前後説明文に強い)。無ければ None。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: -len("```")]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + 1]


def extract_json_array(raw: str) -> str:
    """`find_json_array` の結果。配列が無ければ空の配列(補助の読み取り用)。"""
    return find_json_array(raw) or "[]"


def _safe_confidence(value: object) -> float | None:
    """confidence を 0..1 の float へ寄せる(範囲外/非数は None)。"""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= parsed <= 1.0:
        return parsed
    return None


class FieldExtractionResponseItem(BaseModel):
    """応答の 1 項目(応答の形を JSON Schema でモデルに渡す。#2720)。"""

    name: str
    value: str
    value_type: FieldValueType
    confidence: float


class FieldExtractionResponse(BaseModel):
    """項目抽出の応答の形(``{"fields": [...]}``)。"""

    fields: list[FieldExtractionResponseItem]


class _ResponseEnvelope(BaseModel):
    """応答の外側の検証(項目の中身は 1 つずつ緩く読み、合わない項目だけを捨てる)。"""

    fields: list[object]


FIELD_EXTRACTION_RESPONSE_SCHEMA = FieldExtractionResponse.model_json_schema()


def _response_items(raw: str) -> list[object]:
    """応答から項目の list を取り出す(``{"fields": [...]}``・配列・code fence・前後の説明文)。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()
    try:
        data: object = json.loads(text)
    except (ValueError, TypeError):
        array = find_json_array(text)
        if array is None:
            raise FieldExtractionResponseError("項目抽出の応答に JSON がありません。") from None
        try:
            data = json.loads(array)
        except (ValueError, TypeError) as exc:
            raise FieldExtractionResponseError("項目抽出の応答を JSON として読めません。") from exc
    if isinstance(data, list):
        return data
    try:
        return _ResponseEnvelope.model_validate(data).fields
    except ValueError as exc:
        raise FieldExtractionResponseError(
            "項目抽出の応答が項目の一覧(fields)の形ではありません。"
        ) from exc


def parse_extraction_candidates(
    raw: str, field_defs: list[FieldDefinition]
) -> list[FieldValueCandidate]:
    """LLM の応答を、許可された field 定義に照らして候補(値とモデルが返した表記)にする。

    定義外 name・同じ応答の中の重複・空 value は捨て、value_type は定義側を正とする(LLM の
    自己申告を信用しない)。応答が項目の一覧として読めなければ `FieldExtractionResponseError`
    (正常な 0 件と区別する。#2481)。
    """
    allowed = {definition.name.casefold(): definition for definition in field_defs}
    candidates: list[FieldValueCandidate] = []
    seen: set[str] = set()
    for item in _response_items(raw):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        key = name.casefold()
        if key not in allowed or key in seen:
            continue
        value = item.get("value")
        if value is None or str(value).strip() == "":
            continue
        definition = allowed[key]
        try:
            field = ExtractionField(
                name=definition.name,
                value=normalize_field_value(definition.value_type, str(value)),
                value_type=definition.value_type,
                confidence=_safe_confidence(item.get("confidence")),
            )
        except ValueError:
            continue
        seen.add(key)
        candidates.append(FieldValueCandidate(field=field, raw_value=str(value)))
    return candidates


def parse_extraction_fields(raw: str, field_defs: list[FieldDefinition]) -> list[ExtractionField]:
    """`parse_extraction_candidates` の値だけ。"""
    return [candidate.field for candidate in parse_extraction_candidates(raw, field_defs)]


def field_definitions_prompt(field_defs: list[FieldDefinition]) -> str:
    """抽出器へ渡す field 仕様の JSON 文字列(モデルの API の structured output 用)。"""
    return json.dumps(
        [
            {"name": field.name, "description": field.description, "value_type": field.value_type}
            for field in field_defs
        ],
        ensure_ascii=False,
    )


def _field_element(
    field: ExtractionField, order: int, source: DocumentElement | None
) -> DocumentElement:
    """抽出 field を検索可能な element として表現する(content_kind=field)。"""
    metadata: dict[str, object] = {
        "field_name": field.name,
        "field_value": field.value,
        "field_value_type": field.value_type,
        EXTRACTED_FIELD_KEY: True,
    }
    if field.source_element_id:
        metadata[FIELD_SOURCE_ELEMENT_KEY] = field.source_element_id
    return DocumentElement(
        kind="text",
        text=f"{field.name}: {field.value}",
        order=order,
        element_id=f"field-{order}-{field.name}"[:128],
        content_kind=EXTRACTED_FIELD_CONTENT_KIND,
        section_path=list(source.section_path) if source else [],
        page_number=source.page_number if source else field.page_number,
        bbox=list(source.bbox) if source and source.bbox else None,
        metadata=metadata,
    )


def with_field_extraction_outcome(
    extraction: StructuredExtraction,
    status: FieldExtractionStatus,
    field_count: int = 0,
    *,
    run: FieldExtractionRun | None = None,
    candidate_count: int = 0,
    conflict_fields: Sequence[str] = (),
) -> StructuredExtraction:
    """項目抽出の実行結果を parser_artifacts に刻む(#2481)。

    分けて抽出したとき(#2720)は、単位の数・使い直した単位の数・候補の数と、違う値の候補が
    残った項目の名前(値は刻まない)も刻む。
    """
    outcome: dict[str, ExtractionArtifactValue] = {"status": status, "field_count": field_count}
    if run is not None:
        outcome.update(run.as_artifact())
        outcome["candidate_count"] = candidate_count
        conflicts: list[ExtractionArtifactValue] = [*conflict_fields]
        outcome["conflict_fields"] = conflicts
    return extraction.model_copy(
        update={
            "parser_artifacts": {
                **extraction.parser_artifacts,
                FIELD_EXTRACTION_OUTCOME_ARTIFACT_KEY: outcome,
            }
        }
    )


def field_extraction_outcome(extraction: object) -> tuple[FieldExtractionStatus, int] | None:
    """保存した抽出結果(model か JSON の dict)から項目抽出の実行結果を読む。刻みが無ければ None。"""
    if isinstance(extraction, StructuredExtraction):
        artifacts: object = extraction.parser_artifacts
    elif isinstance(extraction, Mapping):
        artifacts = extraction.get("parser_artifacts")
    else:
        return None
    if not isinstance(artifacts, Mapping):
        return None
    outcome = artifacts.get(FIELD_EXTRACTION_OUTCOME_ARTIFACT_KEY)
    if not isinstance(outcome, Mapping):
        return None
    status = outcome.get("status")
    count = outcome.get("field_count")
    if status not in ("completed", "no_definitions", "no_text"):
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = 0
    return status, count


async def extract_fields_from_extraction(
    extraction: StructuredExtraction,
    field_defs: list[FieldDefinition],
    extract_unit: UnitExtractor,
    *,
    text_budget_tokens: int,
) -> StructuredExtraction:
    """本文を入力の予算に収まる単位に分けて項目を抽出し、候補を統合して付与する(#2720)。

    抽出結果は `StructuredExtraction.fields` へ保存し、検索可能な合成 element
    (content_kind=field)も追加する。実行結果(完了と件数・定義なし・本文なし・単位の数)を
    parser_artifacts に刻む。

    - 単位が入力の上限の超過(``ErrorKind.TOO_LARGE``)で拒まれたら、予算を半分にして分け直す
      (``MAX_FIELD_EXTRACTION_SPLIT_DEPTH`` 回まで)。
    - ほかの失敗は単位の失敗として記録し、残りの単位を続ける(成功した単位は抽出器が保存し、
      再実行で使い直す)。1 つも成功しないうちに失敗したら、残りは呼ばない(設定・形式の誤りを
      同じ要求でくり返さない)。
    - 1 つでも失敗した単位があれば `FieldExtractionFailed` を出す(途中の結果を完成として返さない)。
    - 実行器の停止(``BoundedCallAborted``)と取り消しは、そのまま出す。
    """
    if not field_defs:
        return with_field_extraction_outcome(extraction, "no_definitions")
    # 構造化レコード JSONL の記録の境界のマーカー(#2457)は LLM に渡さない(単位は要素の本文)。
    if not record_boundary_plain_text(extraction).strip():
        return with_field_extraction_outcome(extraction, "no_text")
    units = plan_field_extraction_units(extraction, text_budget_tokens=text_budget_tokens)
    if not units:
        return with_field_extraction_outcome(extraction, "no_text")

    pending = deque((unit, 0) for unit in units)
    results: list[UnitCandidates] = []
    failures: list[BoundedCallFailed] = []
    reused = resplit = 0
    while pending:
        unit, depth = pending.popleft()
        try:
            outcome = await extract_unit(unit)
        except BoundedCallFailed as exc:
            parts = (
                split_field_extraction_unit(
                    unit, text_budget_tokens=text_budget_tokens >> (depth + 1)
                )
                if exc.kind is ErrorKind.TOO_LARGE and depth < MAX_FIELD_EXTRACTION_SPLIT_DEPTH
                else []
            )
            if parts:
                resplit += 1
                pending.extendleft((part, depth + 1) for part in reversed(parts))
                continue
            failures.append(exc)
            if not results:
                break
            continue
        results.append(UnitCandidates(unit=unit, candidates=outcome.candidates))
        reused += int(outcome.reused)
    run = FieldExtractionRun(
        unit_count=len(results) + len(failures) + len(pending),
        succeeded=len(results),
        reused=reused,
        failed=len(failures),
        skipped=len(pending),
        resplit=resplit,
    )
    if failures:
        first = failures[0]
        raise FieldExtractionFailed(
            _FAILURE_CATEGORY_BY_KIND.get(first.kind, "unknown"),
            run=run,
            kind=first.kind,
            status=first.status,
        )

    merged = merge_field_candidates(results, [definition.name for definition in field_defs])
    fields = [field for field in merged.fields if field.name and field.value]
    if not fields:
        return with_field_extraction_outcome(
            extraction,
            "completed",
            run=run,
            candidate_count=merged.candidate_count,
            conflict_fields=merged.conflict_fields,
        )

    next_order = max((element.order for element in extraction.elements), default=0) + 1
    sources = {
        element.element_id: element
        for element in extraction.elements
        if element.element_id and not is_derived_content(element.content_kind, element.metadata)
    }
    new_elements = [
        _field_element(field, next_order + index, sources.get(field.source_element_id or ""))
        for index, field in enumerate(fields)
    ]
    extracted = extraction.model_copy(
        update={
            "fields": [*extraction.fields, *fields],
            "elements": [*extraction.elements, *new_elements],
        }
    )
    return with_field_extraction_outcome(
        extracted,
        "completed",
        len(fields),
        run=run,
        candidate_count=merged.candidate_count,
        conflict_fields=merged.conflict_fields,
    )
