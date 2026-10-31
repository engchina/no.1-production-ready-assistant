"""coordinates の挙動を保護するテスト。"""

import unittest

from rag_engine.parsing.coordinates import (
    clamp_bbox,
    image_top_left_to_pdf_bottom_left,
    pdf_bottom_left_to_image_top_left,
    polygon_to_bbox,
)


class CoordinateTests(unittest.TestCase):
    def test_clamp_bbox_orders_and_limits_values(self):
        self.assertEqual(clamp_bbox([120, -10, 10, 80], 100, 50), [10.0, 0.0, 100.0, 50.0])

    def test_polygon_to_bbox_scales_normalized_vertices(self):
        bbox = polygon_to_bbox(
            [
                {"x": 0.1, "y": 0.2},
                {"x": 0.4, "y": 0.2},
                {"x": 0.4, "y": 0.6},
                {"x": 0.1, "y": 0.6},
            ],
            1000,
            2000,
        )
        self.assertEqual(bbox, [100.0, 400.0, 400.0, 1200.0])

    def test_polygon_to_bbox_keeps_pixel_vertices_when_any_vertex_exceeds_one(self):
        # 正規化座標かは polygon 全体で判断する。頂点ごとに判断すると (0,0)-(1,1) px がページ全体に化ける (#790)。
        self.assertEqual(polygon_to_bbox([{"x": 0, "y": 0}, {"x": 1, "y": 1}, {"x": 5, "y": 3}], 1000, 800), [0.0, 0.0, 5.0, 3.0])
        self.assertEqual(polygon_to_bbox([{"x": 0.5, "y": 0.5}, {"x": 300, "y": 200}], 1000, 800), [0.5, 0.5, 300.0, 200.0])
        self.assertEqual(polygon_to_bbox([{"x": 0, "y": 0}, {"x": 1, "y": 1}], 1000, 800), [0.0, 0.0, 1000.0, 800.0])

    def test_pdf_bottom_left_roundtrip(self):
        image_bbox = pdf_bottom_left_to_image_top_left([10, 20, 60, 80], 100, 100, 200, 200)
        self.assertEqual(image_bbox, [20.0, 40.0, 120.0, 160.0])
        pdf_bbox = image_top_left_to_pdf_bottom_left(image_bbox, 200, 200, 100, 100)
        self.assertEqual(pdf_bbox, [10.0, 20.0, 60.0, 80.0])


if __name__ == "__main__":
    unittest.main()
