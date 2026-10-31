"""回答の判定の基準（rubric）の共有の定義（#3418・#3578）。

rubric は JSON（指示 `instructions`・観点 `aspects`・ラベル `labels`）で、suite の LLM による判定
（`suite-evaluation judge` / `calibrate`）と、人手の判定の記入（suite の記入用のブック・RAG の判定の
校正の画面）が同じ観点の基準を使う。ここは同梱の JSON を読むことと、観点の基準の文・判定する観点を
取り出すことだけをする（形の検証と sha256 は suite の `Rubric`）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any

DEFAULT_RUBRIC = "answer_quality_v1.json"


def load_rubric_data(name: str = DEFAULT_RUBRIC) -> dict[str, Any]:
    """同梱の rubric の JSON を読む（既定は回答の品質の rubric）。"""
    text = resources.files("pr_evaluation_scoring").joinpath("rubrics", name)
    data: dict[str, Any] = json.loads(text.read_text(encoding="utf-8"))
    return data


def aspect_criteria(instructions: str, aspect_id: str) -> str:
    """rubric の指示の中の、観点の基準の行（`- <観点>: …`）。無ければ指示の全体。"""
    pattern = re.compile(rf"^- {re.escape(aspect_id)}:\s*(?P<text>.+)$", re.MULTILINE)
    match = pattern.search(instructions)
    return match.group("text").strip() if match else instructions


def aspect_ids(rubric: Mapping[str, Any]) -> list[str]:
    """rubric の観点の ID（並び順）。"""
    return [str(aspect["id"]) for aspect in rubric.get("aspects", [])]


def applicable_aspects(rubric: Mapping[str, Any], *, has_standard_answer: bool) -> list[str]:
    """項目で判定する観点（標準回答の要る観点は、標準回答のある項目だけ）。"""
    result: list[str] = []
    for aspect in rubric.get("aspects", []):
        requires: Sequence[str] = aspect.get("requires") or []
        if "standard_answer" in requires and not has_standard_answer:
            continue
        result.append(str(aspect["id"]))
    return result
