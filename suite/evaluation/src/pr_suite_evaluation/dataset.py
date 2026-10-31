"""Dataset: 評価セット（JSON）の読み込み・文書の参照の解決・内容の hash（#3240）。

評価セットの形は製品の評価と同じ JSON（`{"cases": [...], "thresholds": {...}}`。例:
`rag/evaluation/business-support/business-support.json`）。この module は suite が採点に使う項目
だけを検証して読み、ほかの項目（標準回答の本文以外の、製品の評価だけが使う設定）は読まない。
製品の Python の型は import しない（同じ JSON を自分の型で読む）。

文書の ID は評価セットでは `file:<ファイル名>` で書く。配備先の文書の ID への置き換えは、文書の
対応表（`{"documents": {"<ファイル名>": "<document_id>"}}`。RAG の `evaluation_corpus_cli` の
`--document-map-output` が取り込みの後に書く）で行う。対応表に無い名前は推測せず失敗にする。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pr_evaluation_scoring import OUTCOMES
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FILE_REFERENCE_PREFIX = "file:"
# 採点する指標（製品の評価の六指標と同じ名前・同じ意味。#591）。
METRIC_NAMES: tuple[str, ...] = (
    "required_evidence_recall",
    "mrr",
    "answer_correctness",
    "requirement_coverage",
    "claim_support_rate",
    "response_decision_accuracy",
)
MAX_TURNS = 5
MAX_CONDITIONS = 30

Outcome = Literal[
    "answered",
    "conditional",
    "needs_clarification",
    "needs_environment_data",
    "needs_human",
    "insufficient_evidence",
]
Decision = Literal["answer", "clarify", "hold"]
CaseCategory = Literal[
    "document_answerable",
    "clarification_required",
    "environment_data_required",
    "knowledge_missing",
    "conflicting_sources",
]
CaseSplit = Literal["dev", "holdout"]
ReasoningType = Literal[
    "single_hop",
    "bridge",
    "comparison",
    "intra_document_reference",
    "table_lookup",
    "section_read",
    "document_read",
]


class DatasetError(ValueError):
    """評価セット・文書の対応表を読めない（利用者へ返す失敗）。"""


def _clean_conditions(value: Mapping[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        condition_id, condition_value = key.strip(), item.strip()
        if condition_id and condition_value:
            cleaned[condition_id] = condition_value
    return cleaned


class Expectation(BaseModel):
    """往復の期待（回答・確認・保留の判断か、受け入れる対応）。"""

    model_config = ConfigDict(extra="ignore")

    expected_response_decision: Decision | None = None
    expected_outcomes: list[Outcome] = Field(default_factory=list, max_length=len(OUTCOMES))


class Turn(Expectation):
    """確認の質問への返答の 1 往復（返答の文と、返答で分かった条件）。"""

    reply: str = Field(..., min_length=1, max_length=8000)
    conditions: dict[str, str] = Field(default_factory=dict, max_length=MAX_CONDITIONS)

    @field_validator("reply")
    @classmethod
    def _strip_reply(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("返答を入力してください。")
        return cleaned

    @field_validator("conditions")
    @classmethod
    def _conditions(cls, value: dict[str, str]) -> dict[str, str]:
        return _clean_conditions(value)


class Evidence(BaseModel):
    """最後の回答に必要な根拠（書いた条件をすべて満たす引用があれば取れた）。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1, max_length=80)
    document_id: str | None = Field(default=None, max_length=200)
    text: str | None = None
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    section: str | None = None
    sheet: str | None = None
    row_start: int | None = Field(default=None, ge=1)
    row_end: int | None = Field(default=None, ge=1)
    # 行の番号の基準（RAG の根拠の locator.row_basis と同じ語彙。省くと原本の行として照合し、解析の
    # 表の格子の行〔parsed_table〕には一致させない。#3547）。
    row_basis: Literal["sheet", "csv", "word_table", "parsed_table"] | None = None

    @field_validator("document_id", "text", "section", "sheet")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        return None if value is None else value.strip() or None

    @model_validator(mode="after")
    def _has_condition(self) -> Self:
        if self.row_basis is not None and self.row_start is None:
            raise ValueError(f"根拠 {self.id} の row_basis は row_start と一緒に書いてください。")
        if all(
            item is None
            for item in (self.text, self.page_start, self.section, self.sheet, self.row_start)
        ):
            raise ValueError(
                f"根拠 {self.id} に本文の語句・ページ・章節・シート・行のどれかを書いてください。"
            )
        return self


class Case(Expectation):
    """評価の 1 ケース。"""

    id: str = Field(..., min_length=1, max_length=200)
    query: str = Field(..., min_length=1)
    relevant_document_ids: list[str] = Field(default_factory=list)
    equivalent_document_ids: dict[str, list[str]] = Field(default_factory=dict)
    # 標準回答（suite は本文を採点に使わない。回答の指標を「評価不能」にするかの判断だけに使う）。
    standard_answer: str | None = None
    category: CaseCategory | None = None
    split: CaseSplit | None = None
    conditions: dict[str, str] = Field(default_factory=dict, max_length=MAX_CONDITIONS)
    turns: list[Turn] = Field(default_factory=list, max_length=MAX_TURNS)
    required_evidence: list[Evidence] = Field(default_factory=list, max_length=30)
    reasoning_type: ReasoningType | None = None
    hops: int | None = Field(default=None, ge=1, le=10)
    include_superseded: bool = False

    @field_validator("id", "query")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("空にできません。")
        return cleaned

    @field_validator("standard_answer")
    @classmethod
    def _standard_answer(cls, value: str | None) -> str | None:
        return None if value is None else value.strip() or None

    @field_validator("conditions")
    @classmethod
    def _conditions(cls, value: dict[str, str]) -> dict[str, str]:
        return _clean_conditions(value)

    @model_validator(mode="after")
    def _unique_evidence(self) -> Self:
        ids = [item.id for item in self.required_evidence]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"必要な根拠の id が重複しています: {', '.join(duplicates)}")
        return self

    def canonical_document_id(self, document_id: str) -> str:
        """同じ文書とみなす文書（`equivalent_document_ids`）を、正解の文書の ID にそろえる。"""
        for canonical, others in self.equivalent_document_ids.items():
            if document_id in others:
                return canonical
        return document_id

    def expectations(self) -> list[Expectation]:
        """往復ごとの期待（最初の質問・返答の順）。"""
        return [self, *self.turns]


class Dataset(BaseModel):
    """評価セット（ケースの並びと、指標の最低閾値）。"""

    model_config = ConfigDict(extra="ignore")

    cases: list[Case] = Field(..., min_length=1)
    thresholds: dict[str, float] = Field(default_factory=dict)

    @field_validator("thresholds", mode="before")
    @classmethod
    def _thresholds(cls, value: object) -> object:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return {key: item for key, item in value.items() if item is not None}
        return value

    @model_validator(mode="after")
    def _check(self) -> Self:
        unknown = sorted(set(self.thresholds) - set(METRIC_NAMES))
        if unknown:
            raise ValueError(f"知らない指標の閾値です: {', '.join(unknown)}")
        if any(not 0 <= value <= 1 for value in self.thresholds.values()):
            raise ValueError("閾値は 0 から 1 の値にしてください。")
        ids = [case.id for case in self.cases]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"ケースの id が重複しています: {', '.join(duplicates)}")
        return self

    def select(self, case_ids: Sequence[str] = (), split: str | None = None) -> Dataset:
        """流すケースだけの評価セット（id・区分で選ぶ）。"""
        cases = [
            case
            for case in self.cases
            if (not case_ids or case.id in case_ids) and (split is None or case.split == split)
        ]
        if not cases:
            raise DatasetError("流すケースがありません（--case / --split を確かめてください）。")
        return self.model_copy(update={"cases": cases})


@dataclass(frozen=True)
class LoadedDataset:
    """読んだ評価セットと、その内容の記録（名前・hash）。"""

    dataset: Dataset
    name: str
    sha256: str

    def provenance(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "case_ids": [case.id for case in self.dataset.cases],
        }


def canonical_sha256(value: object) -> str:
    """JSON の値の hash（キーの順・空白に依らない形）。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_references(raw: Mapping[str, Any]) -> list[str]:
    """評価セットが `file:` で参照するファイル名（出てきた順・重複なし）。"""
    names: list[str] = []
    for value in _document_references(raw):
        if value.startswith(FILE_REFERENCE_PREFIX):
            name = value.removeprefix(FILE_REFERENCE_PREFIX)
            if name and name not in names:
                names.append(name)
    return names


def _document_references(raw: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for case in raw.get("cases") or []:
        if not isinstance(case, Mapping):
            continue
        values.extend(str(item) for item in case.get("relevant_document_ids") or [])
        for evidence in case.get("required_evidence") or []:
            if isinstance(evidence, Mapping) and evidence.get("document_id"):
                values.append(str(evidence["document_id"]))
        for canonical, others in (case.get("equivalent_document_ids") or {}).items():
            values.append(str(canonical))
            values.extend(str(item) for item in others or [])
    return values


def resolve_file_references(raw: Mapping[str, Any], documents: Mapping[str, str]) -> dict[str, Any]:
    """`file:<ファイル名>` を配備先の文書の ID に置き換えた評価セット（対応表に無ければ失敗）。"""
    missing = [name for name in file_references(raw) if name not in documents]
    if missing:
        raise DatasetError(
            "文書の対応表に無いファイルを参照しています（--document-map を確かめてください）: "
            + ", ".join(missing[:10])
        )

    def resolve(value: object) -> object:
        if isinstance(value, str) and value.startswith(FILE_REFERENCE_PREFIX):
            return documents[value.removeprefix(FILE_REFERENCE_PREFIX)]
        return value

    resolved: dict[str, Any] = json.loads(json.dumps(dict(raw), ensure_ascii=False))
    for case in resolved.get("cases") or []:
        if not isinstance(case, dict):
            continue
        case["relevant_document_ids"] = [
            resolve(item) for item in case.get("relevant_document_ids") or []
        ]
        for evidence in case.get("required_evidence") or []:
            if isinstance(evidence, dict) and "document_id" in evidence:
                evidence["document_id"] = resolve(evidence["document_id"])
        if case.get("equivalent_document_ids"):
            case["equivalent_document_ids"] = {
                str(resolve(canonical)): [resolve(item) for item in others or []]
                for canonical, others in case["equivalent_document_ids"].items()
            }
    return resolved


def load_document_map(path: Path) -> dict[str, str]:
    """文書の対応表（`{"documents": {"<ファイル名>": "<document_id>"}}`）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetError(f"文書の対応表を読めません: {path.name}") from error
    documents = payload.get("documents") if isinstance(payload, Mapping) else None
    if not isinstance(documents, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) and key and value
        for key, value in documents.items()
    ):
        raise DatasetError(
            '文書の対応表の形が不正です（{"documents": {"<ファイル名>": "<ID>"}}）: ' + path.name
        )
    return dict(documents)


def load_dataset(path: Path, *, documents: Mapping[str, str] | None = None) -> LoadedDataset:
    """評価セットを読み、`file:` の参照を解決して検証する。hash は解決した後の内容で取る。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetError(f"評価セットを読めません: {path.name}") from error
    if not isinstance(raw, Mapping):
        raise DatasetError(f"評価セットの形が不正です: {path.name}")
    if documents is not None:
        raw = resolve_file_references(raw, documents)
    unresolved = file_references(raw)
    if unresolved:
        raise DatasetError(
            "文書の参照（file:）を配備先の ID にしていません。--document-map で文書の対応表を"
            "渡してください: " + ", ".join(unresolved[:10])
        )
    try:
        dataset = Dataset.model_validate(raw)
    except ValueError as error:
        raise DatasetError(f"評価セットの形が不正です: {error}") from error
    return LoadedDataset(
        dataset=dataset,
        name=path.name,
        sha256=canonical_sha256(dataset.model_dump(mode="json")),
    )


def condition_labels(guides: Mapping[str, Any]) -> dict[str, str]:
    """業務ガイド（`{"guides": [...]}`）の条件の id → 名前（既知の条件を質問の文に書くため）。"""
    labels: dict[str, str] = {}
    for guide in guides.get("guides") or []:
        if not isinstance(guide, Mapping):
            continue
        for condition in guide.get("conditions") or []:
            if isinstance(condition, Mapping) and condition.get("id") and condition.get("label"):
                labels[str(condition["id"])] = str(condition["label"])
    return labels
