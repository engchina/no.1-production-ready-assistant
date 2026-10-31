"""domain keywords の挙動を保護するテスト。"""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_engine.knowledge.domain_keywords import (
    add_domain_keywords,
    domain_keywords_path,
    extract_domain_keywords,
    format_domain_keywords_text,
    load_domain_keywords,
    load_domain_keywords_text,
    parse_domain_keywords_text,
    remove_domain_keywords,
    save_domain_keywords,
)


class DomainKeywordsTests(unittest.TestCase):
    def test_load_missing_file_returns_empty_keywords(self):
        with TemporaryDirectory() as tmp:
            keywords = load_domain_keywords(tmp)

        self.assertEqual(keywords, [])

    def test_parse_text_splits_commas_comments_and_dedupes(self):
        text = "契約区分\nDiscQtyRule, 出庫伝票 # comment\ndiscqtyrule\n\n"

        keywords = parse_domain_keywords_text(text)

        self.assertEqual(keywords, ["契約区分", "DiscQtyRule", "出庫伝票"])

    def test_save_and_load_keywords_with_backup(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            first_time = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)
            second_time = datetime(2026, 9, 3, 1, 2, 4, tzinfo=UTC)

            first = save_domain_keywords(output_dir, ["契約区分", "出庫伝票"], now=first_time)
            second = save_domain_keywords(output_dir, ["契約区分"], now=second_time)
            payload = json.loads(domain_keywords_path(output_dir).read_text(encoding="utf-8"))

        self.assertEqual(first.keyword_count, 2)
        self.assertIsNone(first.backup_path)
        self.assertEqual(second.keyword_count, 1)
        self.assertIsNotNone(second.backup_path)
        self.assertTrue(second.backup_path.name.startswith("domain_keywords.20260903T010204Z"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["keywords"], ["契約区分"])

    def test_load_domain_keywords_text_formats_one_keyword_per_line(self):
        with TemporaryDirectory() as tmp:
            save_domain_keywords(tmp, ["契約区分", "出庫伝票"])
            text = load_domain_keywords_text(tmp)

        self.assertEqual(text, "契約区分\n出庫伝票")

    def test_add_and_remove_domain_keywords(self):
        keywords = add_domain_keywords(["契約区分"], ["出庫伝票", "契約区分"])
        updated = remove_domain_keywords(keywords, ["契約区分"])

        self.assertEqual(keywords, ["契約区分", "出庫伝票"])
        self.assertEqual(updated, ["出庫伝票"])
        self.assertEqual(format_domain_keywords_text(updated), "出庫伝票")

    def test_extract_domain_keywords_prefers_longest_and_uses_ascii_boundaries(self):
        keywords = ["割引", "数量割引額", "DiscQtyRule"]

        matches = extract_domain_keywords("キャンペーン設定 DiscQtyRule 03 数量割引額", keywords)
        ascii_boundary_miss = extract_domain_keywords("xxDiscQtyRuleyy", keywords)

        self.assertEqual(matches, ["DiscQtyRule", "数量割引額"])
        self.assertEqual(ascii_boundary_miss, [])


if __name__ == "__main__":
    unittest.main()
