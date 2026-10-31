"""pages の挙動を保護するテスト。"""

import unittest

from rag_engine.parsing.pages import parse_page_range


class PageRangeTests(unittest.TestCase):
    def test_parse_page_range(self):
        self.assertEqual(parse_page_range("1,3-5", 10), [1, 3, 4, 5])

    def test_parse_all_in_japanese(self):
        self.assertEqual(parse_page_range("すべて", 3), [1, 2, 3])

    def test_ignores_out_of_range_pages(self):
        self.assertEqual(parse_page_range("0,2,9", 3), [2])

    def test_accepts_full_width_digits_and_range_marks(self):
        for value in ("1〜3", "１－３", "1～3", "１，２、３"):
            with self.subTest(value=value):
                self.assertEqual(parse_page_range(value, 10), [1, 2, 3])

    def test_huge_range_is_clamped_before_expansion(self):
        # 絞り込み前に展開すると約 10 億要素の set を作り、メモリを使い切る。
        self.assertEqual(parse_page_range("2-999999999", 4), [2, 3, 4])

    def test_rejects_uninterpretable_parts_with_japanese_guidance(self):
        for value in ("abc", "1-", "-3", "1-2-3"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "解釈できません"):
                parse_page_range(value, 10)

    def test_rejects_when_no_page_is_in_range(self):
        with self.assertRaisesRegex(ValueError, "有効なページ番号がありません"):
            parse_page_range("20-30", 10)


if __name__ == "__main__":
    unittest.main()
