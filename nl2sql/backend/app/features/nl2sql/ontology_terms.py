"""オントロジーの用語の訳の対応表（`ontology_terms.json`）。

記述は日本語を主にし、専門用語は「日本語（英語）」で併記する（#1729）。Markdown の生成・診断の
文・画面の i18n（`nl2sql/frontend/src/lib/i18n.ts` の `ontologyResults.kind.*` と
`ontologyResults.field.*`）が同じ訳を使う。一致は
`tests/test_nl2sql_ontology_unify_links.py` が確かめる。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_TERMS = json.loads(Path(__file__).with_name("ontology_terms.json").read_text(encoding="utf-8"))

CONCEPT_LABELS: dict[str, str] = dict(_TERMS["kinds"])
FIELD_LABELS: dict[str, str] = dict(_TERMS["fields"])
VALUE_LABELS: dict[str, str] = dict(_TERMS["values"])


def plain_label(field: str) -> str:
    """文の中で使う項目名（訳から英語の併記を除く。例「主キー（Primary Key）」→「主キー」）。"""

    return re.sub(r"（[^）]*）$", "", FIELD_LABELS.get(field, field))


def plain_kind(kind: str) -> str:
    """文の中で使う種類の名前（例「オブジェクト型（Object Type）」→「オブジェクト型」）。"""

    return re.sub(r"（[^）]*）$", "", CONCEPT_LABELS.get(kind, kind))
