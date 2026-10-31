"""既存 React viewer の表示形式への変換。"""

from dataclasses import asdict
from typing import Any

from rag_engine.parsing.categories import CATEGORY_STYLES


def viewer_payload(run) -> dict[str, Any]:
    """React viewer が必要とするページ、record、engine 情報を payload 化します。"""
    engines = []
    seen = set()
    for status in run.statuses:
        if status.engine in seen:
            continue
        seen.add(status.engine)
        engines.append(status.to_dict())
    return {
        "run_id": run.run_id,
        "pdf_name": run.pdf_name,
        "pages": [asdict(page) for page in run.pages],
        "source_page_count": run.source_page_count,
        "warnings": run.warnings,
        "classification": run.classification,
        "document_metadata": run.document_metadata,
        "records": [record.to_dict() for record in run.records],
        "engines": engines,
        "category_styles": [asdict(style) for style in CATEGORY_STYLES],
    }
