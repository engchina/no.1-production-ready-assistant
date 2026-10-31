"""解析実行、ページ画像、レイアウトレコードの共有データモデル。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

CoordSystem = Literal["image_top_left", "pdf_bottom_left", "normalized_top_left"]


@dataclass
class PageImage:
    """解析・ビューアで共有するページ画像と元 PDF 座標情報を保持します。"""

    page: int
    width: int
    height: int
    pdf_width: float
    pdf_height: float
    image_path: str
    image_url: str = ""


@dataclass
class LayoutRecord:
    """解析エンジンが検出した 1 つの bbox、カテゴリ、本文を保持します。"""

    id: str
    engine: str
    page: int
    seq_no: int
    bbox: list[float]
    coord_system: CoordSystem
    page_width: float
    page_height: float
    category: str
    text: str = ""
    confidence: float | None = None
    raw_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON 保存に使う dict 表現へ変換します。"""
        return asdict(self)


@dataclass
class EngineStatus:
    """解析エンジンごとの利用可否、所要時間、保存状態を保持します。"""

    engine: str
    label: str
    available: bool
    message: str
    elapsed_seconds: float | None = None
    count: int = 0
    saved_input_path: str | None = None
    use_docling_vision: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON 保存に使う dict 表現へ変換します。"""
        return asdict(self)


@dataclass
class AnalysisRun:
    """1 回の解析実行で作成されたページ、record、保存パスをまとめます。"""

    run_id: str
    pdf_name: str
    pdf_path: str
    pages: list[PageImage]
    records: list[LayoutRecord]
    statuses: list[EngineStatus]
    output_dir: str
    json_path: str
    jsonl_path: str
    viewer_data_path: str
    source_page_count: int = 1
    warnings: list[str] = field(default_factory=list)
    classification: dict[str, Any] = field(default_factory=dict)
    document_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON 保存に使う dict 表現へ変換します。"""
        return {
            "run_id": self.run_id,
            "pdf_name": self.pdf_name,
            "pdf_path": self.pdf_path,
            "pages": [asdict(page) for page in self.pages],
            "source_page_count": self.source_page_count,
            "warnings": self.warnings,
            "classification": self.classification,
            "document_metadata": self.document_metadata,
            "records": [record.to_dict() for record in self.records],
            "statuses": [status.to_dict() for status in self.statuses],
            "output_dir": self.output_dir,
            "json_path": self.json_path,
            "jsonl_path": self.jsonl_path,
            "viewer_data_path": self.viewer_data_path,
        }

    def viewer_payload(self) -> dict[str, Any]:
        """互換 viewer データを遅延変換する。"""
        from rag_engine.parsing.presentation import viewer_payload

        return viewer_payload(self)


def write_json(path: str | Path, payload: Any) -> None:
    """payload を UTF-8 JSON として整形保存します。"""
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
