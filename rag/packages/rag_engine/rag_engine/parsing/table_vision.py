"""独立 Picture として抽出されなかった表内 PDF 画像を検出する。"""

from __future__ import annotations

from pathlib import Path

from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.rendering import PDFIUM_LOCK, _import_pdfium


def missing_table_image_regions(
    records: list[LayoutRecord],
    pages: list[PageImage],
    pdf_path: Path,
    *,
    engine: str = "docling",
) -> dict[str, list[list[float]]]:
    """未処理画像を含む ``engine`` の Table の ID と画像領域を返します。

    PDF の画像 object（Form 内も含む）をページ画像と同じ左上原点の pixel 座標へ
    変換します。画像との重複が小さい表や、説明生成済みの Picture が画像領域の 80% 以上を
    覆う場合は除外します。全面スキャン画像も表領域に切り詰めて対象に含めます。
    多数の小さなベクター path がまとまる領域も保守的な候補とします。
    表罫線のような細い path は対象外です。PDFium は共有 lock 内で読み取り、
    失敗は呼び出し元へ伝播します。
    """
    tables = [r for r in records if r.engine == engine and r.category == "Table"]
    if not tables:
        return {}
    for table in tables:
        table.raw.pop("table_vector_fallback", None)
        table.raw.pop("table_vision_detection_error", None)
        table.raw["table_image_detection_status"] = "not_checked"
    pdfium = _import_pdfium()
    page_lookup = {page.page: page for page in pages}
    result: dict[str, list[list[float]]] = {}
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            for number in sorted({r.page for r in tables}):
                image_page = page_lookup.get(number)
                if image_page is None:
                    continue
                page = document[number - 1]
                try:
                    converter = pdfium.PdfPosConv(page, (0, 0, image_page.width, image_page.height, 0))
                    images = []
                    vectors = []
                    for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE, pdfium.raw.FPDF_PAGEOBJ_PATH]):
                        left, bottom, right, top = obj.get_bounds()
                        points = [(left, bottom), (left, top), (right, bottom), (right, top)]
                        # Form 内の object 座標には親 Form の変換を順に適用します。
                        parent = obj.container
                        while parent is not None:
                            matrix = parent.get_matrix()
                            points = [matrix.on_point(*point) for point in points]
                            parent = parent.container
                        points = [converter.to_bitmap(*point) for point in points]
                        target = images if obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE else vectors
                        target.append(
                            [min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)]
                        )
                    pictures = [
                        r.bbox
                        for r in records
                        if r.engine == engine
                        and r.page == number
                        and r.category == "Picture"
                        and r.raw_type == "picture"
                        and r.text.strip()
                        and not r.raw.get("vision_error")
                        and not r.raw.get("rag_excluded")
                        and not r.raw.get("vision_skipped")
                    ]
                    for table in (r for r in tables if r.page == number):
                        table.raw["table_image_detection_status"] = "no_uncovered_visual"
                        # raster がなくても、罫線以外の図形が集まる表内領域を Vision で確認します。
                        small_paths = [
                            box
                            for box in vectors
                            if box[2] - box[0] >= 4
                            and box[3] - box[1] >= 4
                            and 0 < _area(box) < _area(table.bbox) * 0.1
                            and _area(_intersection(table.bbox, box)) >= _area(box) * 0.95
                        ]
                        candidates = list(images)
                        if len(small_paths) >= 6:
                            region = [
                                min(b[0] for b in small_paths),
                                min(b[1] for b in small_paths),
                                max(b[2] for b in small_paths),
                                max(b[3] for b in small_paths),
                            ]
                            if 0.02 <= _area(region) / max(1, _area(table.bbox)) <= 0.6:
                                candidates.append(region)
                                table.raw["table_vector_fallback"] = True
                        for bbox in candidates:
                            overlap = _intersection(table.bbox, bbox)
                            if _area(overlap) < min(_area(table.bbox), _area(bbox)) * 0.8:
                                continue
                            if _area(overlap) <= 0 or any(
                                _area(_intersection(overlap, picture)) >= _area(overlap) * 0.8 for picture in pictures
                            ):
                                continue
                            if overlap not in result.setdefault(table.id, []):
                                result[table.id].append(overlap)
                                table.raw["table_image_detection_status"] = "detected"
                finally:
                    page.close()
        finally:
            document.close()
    return result


def _intersection(a: list[float], b: list[float]) -> list[float]:
    if len(a) != 4 or len(b) != 4:
        return []
    return [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]


def _area(bbox: list[float]) -> float:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1]) if len(bbox) == 4 else 0
