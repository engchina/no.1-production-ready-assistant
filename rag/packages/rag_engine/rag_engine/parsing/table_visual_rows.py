"""元の表行と Vision の補足を照合し、根拠位置を保持した拡張表を作る。"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from html import escape
from pathlib import Path
from typing import Any

from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.content_labels import content_labels
from rag_engine.parsing.table_structure import parse_html_table_structure


def _normalized(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text)).lower()


def enrich_table_visual_rows(
    record: LayoutRecord,
    page: PageImage,
    description: dict[str, Any],
    run_dir: Path | None = None,
) -> None:
    """一意に照合できる行だけ補足し、元 HTML を変更せず raw に拡張 HTML を保存します。

    元セルごとの類似度 0.85 以上、一意な列対応と行候補との差 0.1 以上を必須とします。
    列位置が行間で矛盾する表や複雑な結合セルは補足だけ保持し、復元を推測しません。
    PDF セル座標がある場合のみ画像領域を行に関連付けます。
    run_dir 指定時は関連画像を PNG として保存します。I/O エラーは呼び出し元へ伝播します。
    """
    # 再照合に失敗した場合に、以前の対応表や根拠を使い続けないようリセットします。
    record.raw.pop("table_enhanced_html", None)
    record.raw.pop("table_row_image_evidence", None)
    structure = parse_html_table_structure(record.text)
    original_rows = structure.get("rows", [])
    vision_rows = description.get("table_rows") or []
    matches: dict[int, dict[str, Any]] = {}
    for cells in vision_rows:
        if not isinstance(cells, list) or not all(isinstance(cell, str) for cell in cells):
            continue
        scores = []
        for row in original_rows:
            if row["row_index"] <= structure.get("header_row_count", 0):
                continue
            original = " ".join(cell["text"] for cell in row["cells"])
            normalized = _normalized(original)
            if len(normalized) < 8:
                continue
            alignment = _align_cells(row["cells"], cells)
            if alignment is not None:
                score, positions = alignment
                scores.append((score, row["row_index"], original, positions))
        scores.sort(reverse=True)
        if not scores or scores[0][0] < 0.85 or (len(scores) > 1 and scores[0][0] - scores[1][0] < 0.1):
            continue
        score, index, original, positions = scores[0]
        if index in matches:
            # 同じ元行への複数候補は曖昧なので拡張表に採用しません。
            matches[index]["ambiguous"] = True
            continue
        extras = [i for i in range(len(cells)) if i not in positions]
        supplement = " / ".join(cells[i] for i in extras if cells[i].strip())
        if supplement:
            matches[index] = {
                "row_index": index,
                "original_text": original,
                "text": supplement,
                "cells": cells,
                "match_score": round(score, 3),
                "image_regions": [],
                "original_cell_positions": positions,
                "supplement_positions": [sum(pos < i for pos in positions) for i in extras],
                "supplement_cells": [cells[i] for i in extras],
            }
    matches = {index: match for index, match in matches.items() if not match.get("ambiguous")}
    regions = record.raw.get("table_missing_picture_regions", [])
    for region in regions:
        indices = _region_row_indices(record, page, region)
        if len(indices) == 1 and indices[0] in matches:
            matches[indices[0]]["image_regions"].append(region)
    for index, match in matches.items():
        if len(match["supplement_cells"]) == 1 and match["image_regions"]:
            row = next(row for row in original_rows if row["row_index"] == index)
            position = _geometric_column_position(record, page, row, match["image_regions"])
            if position is not None:
                # 画像と説明の左右が Vision 内で逆でも、確認できる原本座標を優先します。
                match["supplement_positions"] = [position]
                match["column_position_source"] = "pdf_cell_geometry"
    record.raw["table_vision_rows"] = list(matches.values())
    record.raw["table_vision_unmatched_rows"] = len(vision_rows) - len(matches)
    if not matches:
        return
    enhanced = _enhanced_html(structure, matches)
    if enhanced:
        record.raw["table_enhanced_html"] = enhanced
    if run_dir is not None:
        _save_row_crops(record, page, run_dir, matches)


def _align_cells(original: list[dict[str, Any]], vision: list[str]) -> tuple[float, list[int]] | None:
    """元の各セルを別々に照合し、文字列と列順の両方が一意な場合だけ返します。"""
    positions, scores = [], []
    for cell in original:
        text = _normalized(cell["text"])
        if not text:
            return None
        candidates = sorted(((SequenceMatcher(None, text, _normalized(value)).ratio(), i) for i, value in enumerate(vision)), reverse=True)
        if not candidates:
            return None
        if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.1:
            return None
        score, position = candidates[0]
        if positions and position <= positions[-1]:
            return None
        positions.append(position)
        scores.append(score)
    # 閾値未満の行も次候補との差を測るため残し、最弱セルの score で全セルを保護します。
    return (min(scores), positions) if scores else None


def _enhanced_html(structure: dict[str, Any], matches: dict[int, dict[str, Any]]) -> str:
    """行間で一致する挿入位置に補足列を復元し、caption と元のセル種別を保持します。"""
    signatures = {tuple(match["supplement_positions"]) for match in matches.values()}
    if len(signatures) != 1:
        return ""
    positions = next(iter(signatures))
    rows = structure["rows"]
    # rowspan/colspan を含む表は位置の推測で既存の構造を壊さず、別の補足表示に任せます。
    if any(cell["rowspan"] != 1 or cell["colspan"] != 1 for row in rows for cell in row["cells"]):
        return ""
    if any(len(row["cells"]) != structure["column_count"] for row in rows):
        return ""
    lines = ["<table>"]
    if structure.get("caption"):
        lines.append(f"<caption>{escape(structure['caption'])}</caption>")
    lines.append("<tbody>")
    for row in rows:
        lines.append("<tr>")
        supplement = matches.get(row["row_index"], {}).get("supplement_cells", [])
        for index in range(len(row["cells"]) + 1):
            for number, position in enumerate(positions):
                if position != index:
                    continue
                header = row["row_index"] <= structure.get("header_row_count", 0)
                text = content_labels().text("table_vision_supplement") if header else (supplement[number] if supplement else "")
                tag = "th" if header else "td"
                lines.append(f"<{tag}>{escape(text)}</{tag}>")
            if index < len(row["cells"]):
                cell = row["cells"][index]
                tag = "th" if cell["is_header"] else "td"
                lines.append(f"<{tag}>{escape(cell['text'])}</{tag}>")
        lines.append("</tr>")
    lines.append("</tbody></table>")
    return "".join(lines)


def _geometric_column_position(
    record: LayoutRecord,
    page: PageImage,
    row: dict[str, Any],
    regions: list[list[float]],
) -> int | None:
    """単一補足セルの挿入位置を PDF の横座標で確認し、重なりや結合セルでは推測しません。"""
    if not page.pdf_width or any(cell["colspan"] != 1 or cell["rowspan"] != 1 for cell in row["cells"]):
        return None
    cells = [
        cell
        for cell in record.raw.get("data", {}).get("table_cells", [])
        if int(cell.get("start_row_offset_idx", -1)) + 1 == row["row_index"]
    ]
    cells.sort(key=lambda cell: int(cell.get("start_col_offset_idx", 0)))
    if len(cells) != len(row["cells"]):
        return None
    bounds = []
    for cell in cells:
        box = cell.get("bbox") or {}
        if not all(key in box for key in ("l", "r")):
            return None
        left, right = sorted((float(box["l"]), float(box["r"])))
        bounds.append((left * page.width / page.pdf_width, right * page.width / page.pdf_width))
    if any(left[1] > right[0] for left, right in zip(bounds, bounds[1:], strict=False)):
        return None
    positions = set()
    for region in regions:
        if any(min(right, region[2]) > max(left, region[0]) for left, right in bounds):
            return None
        positions.add(sum(right <= region[0] for _, right in bounds))
    return next(iter(positions)) if len(positions) == 1 else None


def _region_row_indices(record: LayoutRecord, page: PageImage, region: list[float]) -> list[int]:
    """PDF のセル本文と画像の縦方向の重なりで行を特定し、均等行高は仮定しません。"""
    if not page.pdf_height or not page.pdf_width:
        return []
    indices = set()
    for cell in record.raw.get("data", {}).get("table_cells", []):
        box = cell.get("bbox") or {}
        if not all(key in box for key in ("t", "b")) or cell.get("column_header"):
            continue
        top, bottom = float(box["t"]), float(box["b"])
        if box.get("coord_origin") == "BOTTOMLEFT":
            top, bottom = page.pdf_height - top, page.pdf_height - bottom
        top, bottom = sorted((top * page.height / page.pdf_height, bottom * page.height / page.pdf_height))
        if min(bottom, region[3]) > max(top, region[1]):
            start = int(cell.get("start_row_offset_idx", 0))
            end = int(cell.get("end_row_offset_idx", start + 1))
            indices.update(range(start + 1, end + 1))
    return sorted(indices)


def _save_row_crops(record: LayoutRecord, page: PageImage, run_dir: Path, matches: dict[int, dict[str, Any]]) -> None:
    """照合済みの表内画像だけ保存し、行番号を持つ検索根拠として登録します。"""
    from PIL import Image

    evidence = []
    crop_dir = run_dir / "docling" / "visuals"
    crop_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(page.image_path) as source:
        for index, row in matches.items():
            for number, region in enumerate(row["image_regions"], 1):
                image_id = f"{record.id}-row-{index}-image-{number}"
                # parser ID をファイルパスとして信用しません。
                filename = re.sub(r"[^a-zA-Z0-9_-]", "_", image_id) + ".png"
                path = crop_dir / filename
                box = (
                    max(0, int(region[0])),
                    max(0, int(region[1])),
                    min(source.width, int(region[2])),
                    min(source.height, int(region[3])),
                )
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                source.crop(box).save(path)
                relative = path.relative_to(run_dir).as_posix()
                evidence.append(
                    {
                        "image_id": image_id,
                        "record_id": record.id,
                        "page": record.page,
                        "seq_no": record.seq_no,
                        "bbox": list(box),
                        "row_index": index,
                        "relationship": "table_row_image",
                        "raw_type": "picture",
                        "visual_role": "content",
                        "crop_path": relative,
                        "vision_crop": relative,
                        "text_preview": row["text"],
                    }
                )
    record.raw["table_row_image_evidence"] = evidence
