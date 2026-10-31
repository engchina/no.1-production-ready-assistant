"""図形・テキストボックス・フォームコントロールの文字の読み取り（#2526・#2537）。

openpyxl はシートの描画（drawing）の図形の文字を読まない。帳票は説明・押印欄・注意事項を
テキストボックスに置き、選択肢をフォームコントロールのチェックボックスにすることが多いので、
`.xlsx` の zip から次の順にたどって直接読む。

- `xl/workbook.xml` のシート名 → `xl/_rels/workbook.xml.rels` のシートの XML
  → `xl/worksheets/_rels/sheetN.xml.rels` の drawing → `xl/drawings/drawingN.xml`。
- アンカーの位置を、文字を置くセルの範囲にする。`twoCellAnchor` は from / to の行・列（to の位置が
  セルの端（オフセット 0）なら、そのセルは範囲に入れない）。`oneCellAnchor`（from + 大きさ）と
  `absoluteAnchor`（シートの左上からの位置 + 大きさ）は、大きさ（EMU）を ``ShapeText.size`` に持ち、
  読む側がシートの列幅・行の高さでセルの範囲に換算する（`Grid.anchor_box`）。
- 文字は図形（グループの中の図形も）の `txBody` の段落を改行でつなぐ。`mc:AlternateContent` は
  `mc:Choice` だけを読む（`mc:Fallback` は同じ図形の代わりの表現。Excel はフォームコントロールの
  アンカーを wsDr の直下の `mc:AlternateContent` に書く）。非表示の図形（`cNvPr` の
  ``hidden="1"``）は読まない（フォームコントロールの図形は除く。表示は VML が受け持つため非表示）。
- フォームコントロール（#2537）: シートの `<controls>` の `control`（`shapeId`・`r:id`）と、
  `r:id` が指す `xl/ctrlProps/ctrlPropN.xml` の `objectType`・`checked` を読む。チェックボックスは
  オン `☑` / オフ `☐`、オプションボタンは `●` / `○` の選択の記号を、図形（`cNvPr` の `id` =
  `shapeId`）の文字の前に付ける。図形の無いコントロールは `controlPr` の `anchor` の位置に記号だけを
  置く。記号と選択肢は帳票の値の選択の記号の読み方（`choices`）で値になる。

`.xls` の図形は読まない。xlrd が図形を読まないので、シートの記録（BIFF）のテキストオブジェクト
（TXO）の数からセルのメモ（NOTE）の数を引き、図形の文字があるときに診断に残す。
"""

from __future__ import annotations

import io
import posixpath
import struct
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_DRAWING_REL_TYPE = "/drawing"
_CONTROL_REL_TYPE = "/ctrlProp"
_ANCHORS = {f"{_XDR}twoCellAnchor", f"{_XDR}oneCellAnchor", f"{_XDR}absoluteAnchor"}
# フォームコントロールの種類 → 選んだ・選んでいない記号（`choices` の記号の集合にある記号）。
_CONTROL_MARKS = {"CheckBox": ("☑", "☐"), "Radio": ("●", "○")}
# 読む XML の大きさの上限（展開後。極端に大きい drawing で時間・メモリを使わない）。
_MAX_XML_BYTES = 16 * 1024 * 1024
# .xls の BIFF の記録の種類（BOF・EOF・BOUNDSHEET・TXO・NOTE）。
_XLS_BOF = 0x0809
_XLS_EOF = 0x000A
_XLS_BOUNDSHEET = 0x0085
_XLS_TXO = 0x01B6
_XLS_NOTE = 0x001C


@dataclass(frozen=True)
class ShapeText:
    """図形の文字と、置くセルの範囲（1 始まりの上・左・下・右）。

    ``size`` があるとき（`oneCellAnchor` / `absoluteAnchor`）は、``top`` / ``left`` のセルからの
    オフセットと大きさ（行のオフセット・列のオフセット・高さ・幅。EMU）で、``bottom`` / ``right``
    は読む側が列幅・行の高さで決める。
    """

    top: int
    left: int
    bottom: int
    right: int
    text: str
    size: tuple[int, int, int, int] | None = None


@dataclass
class SheetShapes:
    """1 シートの図形の文字。"""

    shapes: list[ShapeText]


def xlsx_sheet_shapes(source_bytes: bytes) -> dict[str, SheetShapes]:
    """シート名 → 図形の文字（読めないときは空。セルの読み取りは止めない）。"""
    try:
        package = zipfile.ZipFile(io.BytesIO(source_bytes))
    except (zipfile.BadZipFile, OSError):
        return {}
    with package:
        try:
            return _sheet_shapes(package)
        except (KeyError, ValueError, SyntaxError, OSError, zipfile.BadZipFile):
            return {}


def _read_xml(package: zipfile.ZipFile, name: str) -> Any | None:
    try:
        info = package.getinfo(name)
    except KeyError:
        return None
    if info.file_size > _MAX_XML_BYTES:
        return None
    # openpyxl の XML の読み方（defusedxml があれば使う）にそろえる。使う側が openpyxl を入れる。
    from openpyxl.xml.functions import fromstring

    return fromstring(package.read(info))


def _relationships(package: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """部品の関係（Id → 種類・部品の名前）。"""
    folder, name = posixpath.split(part)
    root = _read_xml(package, posixpath.join(folder, "_rels", f"{name}.rels"))
    if root is None:
        return {}
    result: dict[str, tuple[str, str]] = {}
    for item in root.iter(f"{_PKG_REL}Relationship"):
        if item.get("TargetMode") == "External":
            continue
        target = item.get("Target") or ""
        path = target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target)
        result[item.get("Id") or ""] = (item.get("Type") or "", posixpath.normpath(path))
    return result


def _sheet_shapes(package: zipfile.ZipFile) -> dict[str, SheetShapes]:
    workbook = _read_xml(package, "xl/workbook.xml")
    if workbook is None:
        return {}
    workbook_rels = _relationships(package, "xl/workbook.xml")
    result: dict[str, SheetShapes] = {}
    for sheet in workbook.iter(f"{_MAIN}sheet"):
        name = sheet.get("name")
        part = workbook_rels.get(sheet.get(f"{_REL}id") or "")
        if not name or part is None:
            continue
        shapes = SheetShapes(shapes=[])
        rels = _relationships(package, part[1])
        controls = _sheet_controls(package, part[1], rels)
        for kind, drawing in rels.values():
            if not kind.endswith(_DRAWING_REL_TYPE):
                continue
            root = _read_xml(package, drawing)
            if root is not None:
                _read_drawing(root, shapes, controls)
        # 図形の無いコントロール: コントロールの位置に記号だけを置く。
        for mark, box in controls.values():
            if box is not None:
                shapes.shapes.append(ShapeText(*box, mark))
        if shapes.shapes:
            result[name] = shapes
    return result


def _sheet_controls(
    package: zipfile.ZipFile, part: str, rels: dict[str, tuple[str, str]]
) -> dict[str, tuple[str, tuple[int, int, int, int] | None]]:
    """チェックボックス・オプションボタンの図形の ID → 選択の記号と、コントロールの位置。"""
    props = {key: path for key, (kind, path) in rels.items() if kind.endswith(_CONTROL_REL_TYPE)}
    if not props:
        return {}
    root = _read_xml(package, part)
    if root is None:
        return {}
    controls: dict[str, tuple[str, tuple[int, int, int, int] | None]] = {}
    for control in root.iter(f"{_MAIN}control"):
        shape_id = control.get("shapeId") or ""
        path = props.get(control.get(f"{_REL}id") or "")
        prop = _read_xml(package, path) if path else None
        marks = _CONTROL_MARKS.get(prop.get("objectType") or "") if prop is not None else None
        if not shape_id or marks is None or prop is None:
            continue
        mark = marks[0] if prop.get("checked") == "Checked" else marks[1]
        anchor = control.find(f"{_MAIN}controlPr/{_MAIN}anchor")
        box = None
        if anchor is not None:
            start = _marker(anchor.find(f"{_MAIN}from"))
            end = _marker(anchor.find(f"{_MAIN}to"))
            if start is not None and end is not None:
                box = _cell_box(start, end)
        if box is not None or shape_id not in controls:
            controls[shape_id] = (mark, box)
    return controls


def _anchors(root: Any) -> Iterator[Any]:
    """wsDr の直下のアンカー（``mc:AlternateContent`` は ``mc:Choice`` の中）。"""
    for child in root:
        if child.tag in _ANCHORS:
            yield child
        elif child.tag == f"{_MC}AlternateContent":
            choice = child.find(f"{_MC}Choice")
            if choice is not None:
                yield from _anchors(choice)


def _shape_properties(element: Any) -> Any | None:
    """アンカーの最初の図形の ``cNvPr``（``mc:Fallback`` は見ない）。"""
    if element.tag == f"{_MC}Fallback":
        return None
    if element.tag == f"{_XDR}cNvPr":
        return element
    for child in element:
        found = _shape_properties(child)
        if found is not None:
            return found
    return None


def _read_drawing(
    root: Any,
    shapes: SheetShapes,
    controls: dict[str, tuple[str, tuple[int, int, int, int] | None]],
) -> None:
    for anchor in _anchors(root):
        properties = _shape_properties(anchor)
        shape_id = properties.get("id") if properties is not None else None
        control = controls.pop(shape_id, None) if shape_id else None
        if control is None and properties is not None and properties.get("hidden") in {"1", "true"}:
            continue
        text = "\n".join(_texts(anchor)).strip()
        if control is not None:
            text = f"{control[0]} {text}".strip()
        if not text:
            continue
        shape = _anchor_shape(anchor, text)
        if shape is not None:
            shapes.shapes.append(shape)


def _anchor_shape(anchor: Any, text: str) -> ShapeText | None:
    """アンカーの位置の図形の文字（位置が読めなければ None）。"""
    extent = _extent(anchor.find(f"{_XDR}ext"))
    if anchor.tag == f"{_XDR}absoluteAnchor":
        position = anchor.find(f"{_XDR}pos")
        if position is None or extent is None:
            return None
        try:
            x, y = int(position.get("x") or 0), int(position.get("y") or 0)
        except ValueError:
            return None
        return ShapeText(1, 1, 1, 1, text, (y, x, extent[1], extent[0]))
    start = _marker(anchor.find(f"{_XDR}from"))
    if start is None:
        return None
    if anchor.tag == f"{_XDR}oneCellAnchor":
        if extent is None:
            return ShapeText(start[0] + 1, start[1] + 1, start[0] + 1, start[1] + 1, text)
        size = (start[2], start[3], extent[1], extent[0])
        return ShapeText(start[0] + 1, start[1] + 1, start[0] + 1, start[1] + 1, text, size)
    end = _marker(anchor.find(f"{_XDR}to"))
    if end is None:
        return ShapeText(start[0] + 1, start[1] + 1, start[0] + 1, start[1] + 1, text)
    return ShapeText(*_cell_box(start, end), text)


def _cell_box(
    start: tuple[int, int, int, int], end: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """from / to（0 始まり）のセルの範囲（1 始まり）。

    to のオフセットが 0 なら、図形はそのセルの上端・左端で終わる（そのセルは入れない）。
    """
    top, left = start[0] + 1, start[1] + 1
    bottom = max(top, end[0] + 1 if end[2] > 0 else end[0])
    right = max(left, end[1] + 1 if end[3] > 0 else end[1])
    return top, left, bottom, right


def _extent(element: Any | None) -> tuple[int, int] | None:
    """図形の大きさ（幅・高さ。EMU）。"""
    if element is None:
        return None
    try:
        return int(element.get("cx") or 0), int(element.get("cy") or 0)
    except ValueError:
        return None


def _marker(element: Any | None) -> tuple[int, int, int, int] | None:
    """アンカーの位置（0 始まりの行・列と、行・列の中のオフセット）。"""
    if element is None:
        return None
    try:
        return (
            int(element.findtext(f"{_XDR}row") or 0),
            int(element.findtext(f"{_XDR}col") or 0),
            int(element.findtext(f"{_XDR}rowOff") or 0),
            int(element.findtext(f"{_XDR}colOff") or 0),
        )
    except ValueError:
        return None


def _texts(element: Any) -> list[str]:
    """図形（グループの中も）の文字の段落（``mc:Fallback`` は読まない）。"""
    if element.tag == f"{_MC}Fallback":
        return []
    if element.tag == f"{_XDR}txBody":
        return [text for paragraph in element.iter(f"{_A}p") if (text := _paragraph(paragraph))]
    if element.tag == f"{_MC}AlternateContent":
        choice = element.find(f"{_MC}Choice")
        return _texts(choice) if choice is not None else []
    return [text for child in element for text in _texts(child)]


def _paragraph(paragraph: Any) -> str:
    parts: list[str] = []
    for item in paragraph.iter():
        if item.tag == f"{_A}t" and item.text:
            parts.append(item.text)
        elif item.tag == f"{_A}br":
            parts.append("\n")
    return "".join(parts).strip()


def xls_shape_text_counts(source_bytes: bytes) -> list[int] | None:
    """`.xls` のシートの順の、図形の文字（セルのメモを除くテキストオブジェクト）の数。

    読めないとき（暗号化・壊れた記録・BIFF 8 でない）は None。
    """
    try:
        from xlrd import compdoc

        document = compdoc.CompDoc(source_bytes)
        stream = None
        for name in ("Workbook", "Book"):
            stream = document.get_named_stream(name)
            if stream:
                break
        if not stream:
            return None
        # xlrd と同じく、ワークシート（シートの種類 0）だけを順に数える。
        positions = [
            struct.unpack("<I", data[:4])[0]
            for kind, data in _records(stream, 0)
            if kind == _XLS_BOUNDSHEET and len(data) >= 6 and data[5] == 0
        ]
        return [_sheet_shape_count(stream, position) for position in positions]
    except Exception:
        return None


def _records(stream: bytes, position: int) -> Any:
    """BIFF の記録（種類・中身）を ``position`` から EOF まで（埋め込みの BOF の中も）。"""
    depth = 0
    while position + 4 <= len(stream):
        kind, length = struct.unpack("<HH", stream[position : position + 4])
        data = stream[position + 4 : position + 4 + length]
        position += 4 + length
        if kind == _XLS_BOF:
            depth += 1
        yield kind, data
        if kind == _XLS_EOF:
            depth -= 1
            if depth <= 0:
                return


def _sheet_shape_count(stream: bytes, position: int) -> int:
    texts = notes = 0
    for kind, _ in _records(stream, position):
        if kind == _XLS_TXO:
            texts += 1
        elif kind == _XLS_NOTE:
            notes += 1
    return max(texts - notes, 0)
