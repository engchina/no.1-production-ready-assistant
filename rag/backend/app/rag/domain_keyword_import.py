"""ドメインキーワードの Excel 検証と取込。プレビューでは保存しない。"""

from __future__ import annotations

import io
import unicodedata
from pathlib import Path
from typing import Literal

from rag_engine.knowledge.domain_keywords import (
    MAX_DOMAIN_KEYWORD_LENGTH,
    MAX_DOMAIN_KEYWORDS,
    normalize_domain_keyword,
)

from app.rag.search_answer_profile_knowledge import (
    SearchAnswerProfileKnowledgeStore,
    load_domain_keywords,
    save_domain_keywords,
    validate_domain_keywords,
)

KeywordImportMode = Literal["INSERT", "REPLACE"]
MAX_EXCEL_ROWS = 10000
PREVIEW_ROWS = 10


def read_keyword_excel(content: bytes, file_name: str) -> list[tuple[int, str]]:
    """先頭シートの KEYWORD 列を読み、行番号と重複を除いた語を返す。"""
    if Path(file_name).suffix.lower() not in {".xlsx", ".xls"}:
        raise ValueError("Excel ファイル(.xlsx / .xls)を指定してください。")
    import pandas as pd  # type: ignore[import-untyped]

    try:
        frame = pd.read_excel(
            io.BytesIO(content),
            header=None,
            dtype=str,
            keep_default_na=False,
            nrows=MAX_EXCEL_ROWS + 2,
        )
    except Exception as exc:
        raise ValueError("Excel ファイルを読み込めません。形式と内容を確認してください。") from exc
    if len(frame) > MAX_EXCEL_ROWS + 1:
        raise ValueError(f"Excel のデータ行は {MAX_EXCEL_ROWS} 行までです。")
    columns = (
        []
        if frame.empty
        else [
            index
            for index, column in enumerate(frame.iloc[0])
            if unicodedata.normalize("NFKC", str(column)).strip().upper() == "KEYWORD"
        ]
    )
    if len(columns) != 1:
        raise ValueError("Excel の先頭行に KEYWORD 列を 1 つ指定してください。")
    rows: list[tuple[int, str]] = []
    seen: set[str] = set()
    for number, raw in enumerate(frame.iloc[1:, columns[0]], start=2):
        if len(unicodedata.normalize("NFKC", raw).strip()) > MAX_DOMAIN_KEYWORD_LENGTH:
            raise ValueError(
                f"{number} 行目のキーワードは {MAX_DOMAIN_KEYWORD_LENGTH} 文字までです。"
            )
        keyword = normalize_domain_keyword(raw)
        if keyword and keyword.casefold() not in seen:
            seen.add(keyword.casefold())
            rows.append((number, keyword))
            if len(rows) > MAX_DOMAIN_KEYWORDS:
                raise ValueError(f"ドメインキーワードは {MAX_DOMAIN_KEYWORDS} 件までです。")
    if not rows:
        raise ValueError("KEYWORD が入力された行がありません。")
    return rows


async def import_keywords(
    store: SearchAnswerProfileKnowledgeStore,
    profile_id: str,
    keywords: list[str],
    *,
    mode: KeywordImportMode,
) -> tuple[list[str], int]:
    existing = await load_domain_keywords(store, profile_id)
    values = validate_domain_keywords([*existing, *keywords] if mode == "INSERT" else keywords)
    inserted = len({word.casefold() for word in values} - {word.casefold() for word in existing})
    saved = await save_domain_keywords(store, profile_id, values)
    return saved, inserted
