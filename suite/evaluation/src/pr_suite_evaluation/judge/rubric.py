"""判定の基準（rubric）: LLM による判定の観点・ラベル・指示（版と hash を記録する。#3418）。

rubric は JSON のファイルで、判定の指示（`instructions`）・観点（`aspects`）・ラベル（`labels`）を
持つ。既定は platform の `pr_evaluation_scoring` に同梱の `rubrics/answer_quality_v1.json`（RAG の
判定の校正の画面と同じ基準。#3578）。内容の sha256 を判定の結果と校正の結果に記録し、
rubric を変えたら校正をやり直す（hash が違う校正の結果では、判定を合否に使わない）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

# ラベルは platform の共有の定義（記入用のブック・RAG の画面と同じ。#3578）。
from pr_evaluation_scoring.labeling import CANNOT_JUDGE as CANNOT_JUDGE
from pr_evaluation_scoring.labeling import FAIL as FAIL
from pr_evaluation_scoring.labeling import PASS as PASS
from pr_evaluation_scoring.rubric import DEFAULT_RUBRIC, load_rubric_data
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pr_suite_evaluation.dataset import METRIC_NAMES, canonical_sha256

REQUIRED_LABELS = (PASS, FAIL, CANNOT_JUDGE)

Requirement = Literal["standard_answer"]


class RubricAspect(BaseModel):
    """判定の観点（`metric` は製品の評価の指標の名前。結果の表で並べる名前）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=80)
    metric: str
    requires: list[Requirement] = Field(default_factory=list)


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=80)
    version: str = Field(..., min_length=1, max_length=40)
    labels: list[str]
    instructions: str = Field(..., min_length=1)
    aspects: list[RubricAspect] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if tuple(self.labels) != REQUIRED_LABELS:
            raise ValueError(f"labels は {list(REQUIRED_LABELS)} にしてください。")
        ids = [aspect.id for aspect in self.aspects]
        if len(set(ids)) != len(ids):
            raise ValueError("観点の id が重複しています。")
        unknown = sorted({aspect.metric for aspect in self.aspects} - set(METRIC_NAMES))
        if unknown:
            raise ValueError(f"知らない指標の名前です: {', '.join(unknown)}")
        return self

    @property
    def aspect_ids(self) -> list[str]:
        return [aspect.id for aspect in self.aspects]

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    def output_schema(self) -> dict[str, Any]:
        """判定の出力の JSON schema（構造化出力に渡し、Pydantic でも確かめる）。"""
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["aspects"],
            "properties": {
                "aspects": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["aspect_id", "label", "reason"],
                        "properties": {
                            "aspect_id": {"type": "string", "enum": self.aspect_ids},
                            "label": {"type": "string", "enum": list(self.labels)},
                            "reason": {"type": "string"},
                        },
                    },
                }
            },
        }

    def provenance(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "sha256": self.sha256}


def load_rubric(path: Path | None = None) -> Rubric:
    """rubric を読む（省略時は既定の rubric）。"""
    if path is None:
        raw: Mapping[str, Any] = load_rubric_data(DEFAULT_RUBRIC)
    else:
        raw = json.loads(path.read_text(encoding="utf-8"))
    return Rubric.model_validate(raw)
