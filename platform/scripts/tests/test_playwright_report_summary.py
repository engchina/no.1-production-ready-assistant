"""platform/scripts/playwright_report_summary.py のテスト（#1591）。

実行: python3 -m unittest discover -s platform/scripts/tests -p 'test_*.py'
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import playwright_report_summary as summary  # noqa: E402


def _test(status: str, project: str = "desktop", attempts: int = 1) -> dict:
    return {"projectName": project, "status": status, "results": [{}] * attempts}


REPORT = {
    "suites": [
        {
            "title": "chat.spec.ts",
            "file": "chat.spec.ts",
            "specs": [
                {
                    "title": "送信できる",
                    "file": "chat.spec.ts",
                    "line": 10,
                    "tests": [_test("expected")],
                },
            ],
            "suites": [
                {
                    "title": "履歴",
                    "file": "chat.spec.ts",
                    "specs": [
                        {
                            "title": "a | b を開く",
                            "file": "chat.spec.ts",
                            "line": 30,
                            "tests": [_test("flaky", "mobile", 2), _test("expected")],
                        },
                    ],
                    "suites": [],
                }
            ],
        },
        {
            "title": "runs.spec.ts",
            "file": "runs.spec.ts",
            "specs": [
                {
                    "title": "一覧",
                    "file": "runs.spec.ts",
                    "line": 5,
                    "tests": [_test("unexpected", attempts=3)],
                },
            ],
            "suites": [],
        },
    ],
    "stats": {"expected": 2, "unexpected": 1, "flaky": 1, "skipped": 0},
}


class RenderTest(unittest.TestCase):
    def test_lists_flaky_and_failed_tests(self) -> None:
        text = summary.render(REPORT, "agent (1/2)")
        self.assertIn("### Playwright: agent (1/2)", text)
        self.assertIn("成功 2・flaky 1・失敗 1・skip 0", text)
        self.assertIn("| `chat.spec.ts:30` | 履歴 › a \\| b を開く | mobile | 2 |", text)
        self.assertIn("| `runs.spec.ts:5` | 一覧 | desktop | 3 |", text)
        self.assertNotIn("送信できる", text)
        self.assertLess(text.index("flaky（retry で通った）"), text.index("#### 失敗"))

    def test_reports_clean_run(self) -> None:
        clean = {"suites": [], "stats": {"expected": 3, "unexpected": 0, "flaky": 0, "skipped": 1}}
        text = summary.render(clean, "rag")
        self.assertIn("flaky・失敗のテストはありません。", text)
        self.assertNotIn("| spec |", text)

    def test_appends_to_summary_and_tolerates_missing_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "results.json"
            report.write_text(json.dumps(REPORT), encoding="utf-8")
            out = Path(tmp) / "summary.md"
            out.write_text("前の内容\n", encoding="utf-8")
            self.assertEqual(summary.main([str(report), "--title", "x", "--summary", str(out)]), 0)
            missing = Path(tmp) / "none.json"
            self.assertEqual(summary.main([str(missing), "--title", "y", "--summary", str(out)]), 0)
            text = out.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("前の内容\n### Playwright: x"))
        self.assertIn("### Playwright: y", text)
        self.assertIn("がありません。", text)


if __name__ == "__main__":
    unittest.main()
