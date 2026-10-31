"""platform/scripts/ci_frontend_paths.py のテスト（#1531）。

実行: python3 -m unittest discover -s platform/scripts/tests -p 'test_*.py'
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ci_frontend_paths as paths  # noqa: E402

CI = """jobs:
  changes:
    steps:
      - name: Filter paths
        with:
          filters: |
            ci:
              - ".github/workflows/ci.yml"
            # コメント
            platform_frontend:
              - "platform/packages/ui/**"
            platform_other:
              - "platform/**"
              - "!platform/packages/ui/**"
              - "!platform/**/*.md"
            nl2sql_frontend_contract:
              - "platform/x.py"
            {name}_frontend:
              - "{name}/frontend/**"
{extra}
            rag_frontend_dummy:
              - "x"
  next-job:
    steps: []
"""


class ParseAndMatchTest(unittest.TestCase):
    def test_parses_filters_until_the_block_ends(self) -> None:
        filters = paths.parse_filters(CI.format(name="rag", extra=""))
        self.assertEqual(filters["platform_frontend"], ["platform/packages/ui/**"])
        self.assertEqual(filters["rag_frontend"], ["rag/frontend/**"])
        self.assertNotIn("next-job", filters)

    def test_matches_with_excludes(self) -> None:
        other = ["platform/**", "!platform/packages/ui/**", "!platform/**/*.md"]
        self.assertTrue(paths.matches("platform/.env.example", other))
        self.assertFalse(paths.matches("platform/packages/ui/src/a.ts", other))
        self.assertFalse(paths.matches("platform/docs/a.md", other))
        self.assertTrue(paths.matches("rag/frontend/src/a.ts", ["rag/frontend/**"]))
        self.assertFalse(paths.matches("rag/backend/a.py", ["rag/frontend/**"]))
        self.assertTrue(paths.matches("a/b/c.pdf", ["a/*/*.pdf"]))
        self.assertFalse(paths.matches("a/b/d/c.pdf", ["a/*/*.pdf"]))


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for product in paths.PRODUCTS:
            (self.root / product / "frontend" / "src").mkdir(parents=True)
        self._write("rag/backend/app/permissions.py", "X = 1\n")
        self._write("agent/backend/app/router.py", "X = 1\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> None:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def _ci(self, rag: list[str] = (), agent: list[str] = ()) -> None:
        lines = ["            rag_frontend:", '              - "rag/frontend/**"']
        lines += [f'              - "{p}"' for p in rag]
        lines += ["            agent_frontend:", '              - "agent/frontend/**"']
        lines += [f'              - "{p}"' for p in agent]
        text = CI.format(name="nl2sql", extra="\n".join(lines))
        self._write(paths.CI_WORKFLOW, text)

    def test_backend_file_read_by_frontend_test_must_be_in_filter(self) -> None:
        self._write(
            "rag/frontend/src/lib/a.test.ts",
            'readFileSync(resolve(__dirname, "../../../backend/app/permissions.py"));\n'
            # frontend の中・存在しないファイル・ディレクトリは数えない。
            'import x from "../b";\nconst env = "../../../backend/.env";\nlocator("../..");\n',
        )
        self._ci()
        errors = paths.check(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("rag/backend/app/permissions.py", errors[0])
        self.assertIn("rag/frontend/src/lib/a.test.ts", errors[0])
        self._ci(rag=["rag/backend/app/permissions.py"])
        self.assertEqual(paths.check(self.root), [])

    def test_url_base_reads_are_resolved(self) -> None:
        self._write(
            "agent/frontend/e2e/mock.ts",
            'const APP = new URL("../../backend/app/", import.meta.url);\n'
            'readFileSync(new URL("router.py", APP), "utf-8");\n',
        )
        self._ci()
        errors = paths.check(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("agent/backend/app/router.py", errors[0])
        self._ci(agent=["agent/backend/app/router.py"])
        self.assertEqual(paths.check(self.root), [])

    def test_platform_files_are_covered_by_shared_filters(self) -> None:
        self._write("platform/packages/ui/src/a.tsx", "x\n")
        self._write("platform/.env.example", "x\n")
        self._write(
            "nl2sql/frontend/tests/a.test.ts",
            'read("../../../platform/packages/ui/src/a.tsx");\n'
            'read("../../../platform/.env.example");\n',
        )
        self._ci()
        self.assertEqual(paths.check(self.root), [])

    def test_missing_filter_is_reported(self) -> None:
        self._write(paths.CI_WORKFLOW, CI.format(name="nl2sql", extra=""))
        errors = paths.check(self.root)
        self.assertTrue(any("rag_frontend" in error for error in errors))

    def test_repository_ci_filters_cover_frontend_reads(self) -> None:
        self.assertEqual(paths.check(), [])


class CheckBackendTest(unittest.TestCase):
    """backend のテストが読む frontend のファイルの filter（#1592）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for product in paths.PRODUCTS:
            (self.root / product / "frontend" / "src" / "lib").mkdir(parents=True)
        self._write("rag/frontend/src/lib/i18n.ts", "x\n")
        self._write("rag/frontend/src/lib/a.fixture.json", "{}\n")
        self._write("rag/frontend/package.json", "{}\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, text: str) -> None:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def _ci(self, rag_reads: list[str] = ()) -> None:
        lines = []
        for product in paths.PRODUCTS:
            lines += [
                f"            {product}_backend:",
                f'              - "{product}/**"',
                f'              - "!{product}/frontend/**"',
                f"            {product}_backend_frontend:",
                '              - "none"',
            ]
            if product == "rag":
                lines += [f'              - "{p}"' for p in rag_reads]
        text = CI.format(name="nl2sql", extra="\n".join(lines)).replace(
            "            platform_other:",
            "            platform_backend:\n"
            '              - "platform/packages/backend_core/**"\n'
            "            platform_other:",
        )
        self._write(paths.CI_WORKFLOW, text)

    def test_frontend_file_read_by_backend_test_must_be_in_filter(self) -> None:
        self._write(
            "rag/backend/tests/test_nav.py",
            'FRONTEND_SRC = ROOT / "frontend" / "src"\n'
            'text = (FRONTEND_SRC / "lib/i18n.ts").read_text()\n'
            'fixture = ROOT / "frontend/src/lib/a.fixture.json"\n'
            # `/` の無い名前・存在しないファイルは数えない。
            'other = tmp / "package.json"\nmissing = FRONTEND_SRC / "lib/none.ts"\n',
        )
        # "frontend" を含まないファイルの文字列は数えない。
        self._write("rag/backend/app/x.py", 'PATH = "lib/i18n.ts"\n')
        self._ci()
        errors = paths.check_backend(self.root)
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("rag/frontend/src/lib/a.fixture.json", errors[0])
        self.assertIn("rag/frontend/src/lib/i18n.ts", errors[1])
        self.assertIn("rag/backend/tests/test_nav.py", errors[1])
        self._ci(rag_reads=["rag/frontend/src/lib/i18n.ts", "rag/frontend/src/lib/a.fixture.json"])
        self.assertEqual(paths.check_backend(self.root), [])

    def test_frontend_and_venv_python_files_are_not_scanned(self) -> None:
        self._write("rag/frontend/scripts/gen.py", 'x = "frontend"\ny = "src/lib/i18n.ts"\n')
        self._write(
            "rag/backend/.venv/lib/a.py", 'x = "frontend"\ny = "frontend/src/lib/i18n.ts"\n'
        )
        self._ci()
        self.assertEqual(paths.check_backend(self.root), [])

    def test_missing_filter_is_reported(self) -> None:
        self._write(paths.CI_WORKFLOW, CI.format(name="nl2sql", extra=""))
        errors = paths.check_backend(self.root)
        self.assertTrue(any("rag_backend" in error for error in errors))

    def test_repository_ci_filters_cover_backend_reads(self) -> None:
        self.assertEqual(paths.check_backend(), [])


if __name__ == "__main__":
    unittest.main()
