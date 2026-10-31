"""PDF とレンダリング画像の座標系を相互変換する。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

BBox = list[float]


def clamp_bbox(bbox: Sequence[float], width: float, height: float) -> BBox:
    """bbox をページ内へ収め、左上/右下の順序を保証する。"""
    if len(bbox) != 4:
        raise ValueError("bbox は [x1, y1, x2, y2] の 4 要素で指定してください。")
    x1, y1, x2, y2 = [float(v) for v in bbox]
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    left = min(max(left, 0.0), float(width))
    right = min(max(right, 0.0), float(width))
    top = min(max(top, 0.0), float(height))
    bottom = min(max(bottom, 0.0), float(height))
    return [left, top, right, bottom]


def polygon_to_bbox(vertices: Iterable[object], width: float, height: float) -> BBox:
    """normalized polygon または point 配列を image_top_left bbox へ変換する。

    正規化座標かどうかは polygon 全体で判断する。全頂点が 0〜1 のときだけ拡大し、1 頂点でも 1 を超えれば
    全頂点を pixel 座標として扱う。頂点ごとに判断すると、pixel 座標の (0,0)・(1,1) がページ全体に化けたり、
    混在入力で頂点ごとに別解釈になったりする (#790)。
    """
    xs: list[float] = []
    ys: list[float] = []
    for vertex in vertices:
        if isinstance(vertex, dict):
            x = vertex.get("x")
            y = vertex.get("y")
        else:
            x = getattr(vertex, "x", None)
            y = getattr(vertex, "y", None)
        if x is None or y is None:
            continue
        xs.append(float(x))
        ys.append(float(y))
    if not xs or not ys:
        raise ValueError("polygon から bbox を計算できませんでした。")
    if all(0.0 <= value <= 1.0 for value in (*xs, *ys)):
        xs = [x * float(width) for x in xs]
        ys = [y * float(height) for y in ys]
    return clamp_bbox([min(xs), min(ys), max(xs), max(ys)], width, height)


def pdf_bottom_left_to_image_top_left(
    bbox: Sequence[float],
    page_width: float,
    page_height: float,
    target_width: float,
    target_height: float,
) -> BBox:
    """PDF 座標系の bbox をレンダリング画像の左上原点座標へ変換する。"""
    x1, y1, x2, y2 = [float(v) for v in bbox]
    x_scale = float(target_width) / float(page_width)
    y_scale = float(target_height) / float(page_height)
    left = min(x1, x2) * x_scale
    right = max(x1, x2) * x_scale
    top = (float(page_height) - max(y1, y2)) * y_scale
    bottom = (float(page_height) - min(y1, y2)) * y_scale
    return clamp_bbox([left, top, right, bottom], target_width, target_height)


def image_top_left_to_pdf_bottom_left(
    bbox: Sequence[float],
    image_width: float,
    image_height: float,
    page_width: float,
    page_height: float,
) -> BBox:
    """レンダリング画像の左上原点 bbox を PDF 左下原点へ変換する。"""
    x1, y1, x2, y2 = clamp_bbox(bbox, image_width, image_height)
    x_scale = float(page_width) / float(image_width)
    y_scale = float(page_height) / float(image_height)
    left = x1 * x_scale
    right = x2 * x_scale
    bottom = (float(image_height) - y2) * y_scale
    top = (float(image_height) - y1) * y_scale
    return [left, bottom, right, top]
