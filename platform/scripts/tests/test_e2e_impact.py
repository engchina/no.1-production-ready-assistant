"""platform/scripts/e2e_impact.py のテスト（#885）。

実行: python3 -m unittest discover -s platform/scripts/tests -p 'test_*.py'
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import e2e_impact as impact  # noqa: E402


def _map(files: dict, tests: int = 4, specs: dict | None = None, commit: str = "c0") -> dict:
    return {
        "version": impact.MAP_VERSION,
        "product": "rag",
        "commit": commit,
        "tests": tests,
        "specs": specs or {},
        "files": files,
    }


API_TS = """export interface Config {
  /** 説明 */
  a?: string;
}

export const LIMIT = 10;

export enum Mode {
  A = "a",
}

// 読み込み
export function load() {
  return LIMIT;
}
"""


class NonRuntimeLinesTest(unittest.TestCase):
    def _marked(self, text: str) -> list[str]:
        lines = text.splitlines()
        return [lines[n - 1] for n in sorted(impact.non_runtime_lines(text))]

    def test_interface_type_and_comments_are_static(self) -> None:
        text = (
            'import type { A } from "./a";\n'  # 1
            'export type { B, C } from "./b";\n'  # 2
            "/**\n"  # 3
            " * 説明\n"  # 4
            " */\n"  # 5
            'export interface Props extends Omit<A, "x"> {\n'  # 6
            "  // 欄\n"  # 7
            "  onChange: (value: { a: string }) => void;\n"  # 8
            "}\n"  # 9
            "\n"  # 10
            "export type Union =\n"  # 11
            '  | "a"\n'  # 12
            "  | { b: `x-${string}` };\n"  # 13
            "declare global {\n"  # 14
            "  interface Window { x: number }\n"  # 15
            "}\n"  # 16
        )
        self.assertEqual(impact.non_runtime_lines(text), set(range(1, 17)))

    def test_runtime_code_is_not_static(self) -> None:
        text = (
            "export const LIMIT = 10;\n"  # 1
            "export enum Mode {\n"  # 2
            '  A = "a",\n'  # 3
            "}\n"  # 4
            "const enum Flag { On }\n"  # 5
            "export function f(): number {\n"  # 6
            "  type Local = string;\n"  # 7（関数の中は見ない）
            "  return 1; // コメント\n"  # 8
            "}\n"  # 9
            'import { type A, b } from "./a";\n'  # 10
            "export class K implements A {}\n"  # 11
        )
        self.assertEqual(impact.non_runtime_lines(text), set())

    def test_type_without_semicolon_ends_before_next_statement(self) -> None:
        text = 'export type A = "a" | "b"\nexport const b = 1;\n'
        self.assertEqual(self._marked(text), ['export type A = "a" | "b"'])

    def test_template_literal_lines_are_runtime(self) -> None:
        text = "const sql = `\n\n// 例\ninterface X {}\n`;\n"
        self.assertEqual(impact.non_runtime_lines(text), set())

    def test_code_after_declaration_on_same_line_is_runtime(self) -> None:
        text = "interface A {\n  a: string;\n} export const x = 1;\n/* a */ const y = 2;\n"
        self.assertEqual(self._marked(text), ["interface A {", "  a: string;"])


class DictionaryTest(unittest.TestCase):
    OLD = """export const ja = {
  "a.title": "タイトル",
  // コメント
  "a.long":
    "長い文言の前半" +
    "後半",
  "b.label": "ラベル {name}",
};

export function t(key: string) {
  return key;
}
"""

    def test_changed_keys_and_old_values(self) -> None:
        new = self.OLD.replace('"ラベル {name}"', '"新しいラベル {name}"').replace("後半", "後ろ")
        keys, old_values, structural = impact.dictionary_changes(self.OLD, new)
        self.assertEqual(keys, {"a.long", "b.label"})
        self.assertEqual(old_values["a.long"], "長い文言の前半後半")
        self.assertEqual(old_values["b.label"], "ラベル {name}")
        self.assertFalse(structural)

    def test_added_key_and_comment_only_change(self) -> None:
        new = self.OLD.replace("  // コメント\n", '  // 別のコメント\n  "c.new": "追加",\n')
        keys, old_values, structural = impact.dictionary_changes(self.OLD, new)
        self.assertEqual(keys, {"c.new"})
        self.assertEqual(old_values, {})
        self.assertFalse(structural)

    def test_function_change_is_structural(self) -> None:
        new = self.OLD.replace("return key;", "return key.trim();")
        keys, _, structural = impact.dictionary_changes(self.OLD, new)
        self.assertEqual(keys, set())
        self.assertTrue(structural)

    SHARED = """/** 共有 UI の辞書（#1580）。 */
export const AUTH_MESSAGES = {
  // 見出し
  title: "ログイン",
  subtitle:
    "説明の文言",
  "a.b": "ドット区切り",
} as const;

export interface Extra {
  title: string;
}

export function t(
  key: string,
  params?: Record<string, string>,
): string {
  return key;
}
"""

    def test_identifier_keys_only_inside_object(self) -> None:
        entries, _ = impact.parse_dictionary(self.SHARED, identifier_keys=True)
        self.assertEqual(set(entries), {"title", "subtitle", "a.b"})
        # 製品の辞書の読み方（識別子のキーを読まない）は変えない。
        self.assertEqual(set(impact.parse_dictionary(self.SHARED)[0]), {"a.b"})

    def test_identifier_key_change_is_not_structural(self) -> None:
        new = self.SHARED.replace('"説明の文言"', '"新しい説明"')
        keys, old_values, structural = impact.dictionary_changes(self.SHARED, new, True)
        self.assertEqual(keys, {"subtitle"})
        self.assertEqual(old_values["subtitle"], "説明の文言")
        self.assertFalse(structural)

    def test_interface_or_function_change_in_shared_dictionary_is_structural(self) -> None:
        for old, new in (("  title: string;", "  title?: string;"), ("return key;", "return 1;")):
            keys, _, structural = impact.dictionary_changes(
                self.SHARED, self.SHARED.replace(old, new), True
            )
            self.assertEqual(keys, set(), old)
            self.assertTrue(structural, old)

    def test_platform_dictionary_paths(self) -> None:
        self.assertTrue(
            impact.is_platform_dictionary("platform/packages/system-settings/src/model/messages.ts")
        )
        self.assertTrue(impact.is_platform_dictionary("platform/packages/ui/src/messages.ts"))
        for path in (
            "platform/packages/system-settings/src/model/messages.test.ts",
            "platform/packages/system-settings/tests/messages.ts",
            "platform/packages/system-settings/src/model/ModelSettingsPage.tsx",
            "rag/frontend/src/lib/messages.ts",
        ):
            self.assertFalse(impact.is_platform_dictionary(path), path)


class TranslateLinesTest(unittest.TestCase):
    def test_moves_lines_after_insertion(self) -> None:
        source = "a\nb\nc\nd\n"
        target = "x\ny\na\nb\nc\nd\n"
        self.assertEqual(impact.translate_lines({1, 4}, source, target), {3, 6})

    def test_changed_line_maps_near(self) -> None:
        source = "a\nb\nc\n"
        target = "a\nB\nc\n"
        self.assertEqual(impact.translate_lines({2}, source, target), {1})


class ImpactMapTest(unittest.TestCase):
    def setUp(self) -> None:
        files = {
            "rag/frontend/src/Page.tsx": {
                "loadedBy": ["s1", "s2", "s3", "s4"],
                "functions": {
                    # 読み込み時に全テストで実行される関数
                    "3:3": {"specs": ["s1", "s2", "s3", "s4"], "tests": 4},
                    # 画面の component（s1 だけ）と、その中の handler（s1 だけ）
                    "10:40": {"specs": ["s1"], "tests": 1},
                    "20:25": {"specs": ["s1"], "tests": 1},
                },
            },
            "rag/frontend/src/Shell.tsx": {
                "loadedBy": ["s1", "s2", "s3", "s4"],
                "functions": {"5:30": {"specs": ["s1", "s2", "s3", "s4"], "tests": 4}},
            },
        }
        self.map = impact.ImpactMap(_map(files))

    def test_line_in_function_selects_its_specs(self) -> None:
        specs, shared = self.map.specs_for_lines("rag/frontend/src/Page.tsx", {22})
        self.assertEqual(specs, {"s1"})
        self.assertFalse(shared)

    def test_line_in_global_function_selects_all_loaders(self) -> None:
        specs, shared = self.map.specs_for_lines("rag/frontend/src/Page.tsx", {3})
        self.assertEqual(specs, {"s1", "s2", "s3", "s4"})
        self.assertTrue(shared)

    def test_top_level_line_selects_local_specs(self) -> None:
        specs, _ = self.map.specs_for_lines("rag/frontend/src/Page.tsx", {1})
        self.assertEqual(specs, {"s1"})

    def test_top_level_of_shell_selects_all(self) -> None:
        specs, shared = self.map.specs_for_lines("rag/frontend/src/Shell.tsx", {1})
        self.assertEqual(specs, {"s1", "s2", "s3", "s4"})
        self.assertTrue(shared)


class AggregateTest(unittest.TestCase):
    def _record(self, spec: str, test_id: str, retry: int, executed: dict) -> dict:
        return {
            "spec": spec,
            "testId": test_id,
            "title": test_id,
            "project": "desktop",
            "retry": retry,
            "durationMs": 1000,
            "executed": executed,
            "loaded": sorted(executed),
        }

    def test_aggregate_keeps_last_retry_and_merges_shards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shard1 = Path(tmp, "1")
            shard2 = Path(tmp, "2")
            shard1.mkdir()
            shard2.mkdir()
            records = [
                (shard1, self._record("a.spec.ts", "t1", 0, {"x.tsx": ["1:9", "2:3"]})),
                (shard1, self._record("a.spec.ts", "t1", 1, {"x.tsx": ["1:9"]})),
                (shard2, self._record("b.spec.ts", "t2", 0, {"x.tsx": ["1:9", "5:6"]})),
            ]
            for index, (directory, record) in enumerate(records):
                Path(directory, f"{index}.json").write_text(json.dumps(record))
            maps = [
                impact.aggregate(shard1, "rag", "c0"),
                impact.aggregate(shard2, "rag", "c0"),
            ]
        merged = impact.merge_maps(maps)
        assert merged is not None
        self.assertEqual(merged["tests"], 2)
        self.assertEqual(merged["specs"]["a.spec.ts"]["tests"], 1)
        functions = impact.ImpactMap(merged).functions("x.tsx")
        by_key = {(fn.start, fn.end): fn for fn in functions}
        self.assertTrue(by_key[(1, 9)].is_global)
        self.assertNotIn((2, 3), by_key)
        self.assertEqual(by_key[(5, 6)].specs, {"b.spec.ts"})

    def test_aggregate_counts_pr_scope_separately(self) -> None:
        # PR の CI の mobile の project は @responsive などだけを実行する（#1582）。
        # prScope の無い古い記録は、PR でも実行するものとして数える。
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            records = [
                self._record("a.spec.ts", "desktop", 0, {}) | {"prScope": True},
                self._record("a.spec.ts", "mobile", 0, {})
                | {"project": "mobile", "prScope": False},
                self._record("a.spec.ts", "old", 0, {}),
            ]
            for index, record in enumerate(records):
                Path(directory, f"{index}.json").write_text(json.dumps(record))
            data = impact.aggregate(directory, "rag", "c0")
        entry = data["specs"]["a.spec.ts"]
        self.assertEqual((entry["durationMs"], entry["tests"]), (3000, 3))
        self.assertEqual((entry["prDurationMs"], entry["prTests"]), (2000, 2))

    def test_merge_without_pr_scope_uses_full_duration(self) -> None:
        new = _map(
            {}, specs={"a": {"durationMs": 300, "tests": 3, "prDurationMs": 100, "prTests": 1}}
        )
        old = _map({}, specs={"a": {"durationMs": 200, "tests": 2}})
        merged = impact.merge_maps([new, old])
        assert merged is not None
        self.assertEqual(merged["specs"]["a"]["prDurationMs"], 300)
        self.assertEqual(merged["specs"]["a"]["prTests"], 3)

    def test_merge_ignores_other_commits(self) -> None:
        a = _map({}, tests=1, commit="c1")
        b = _map({}, tests=2, commit="c2")
        merged = impact.merge_maps([a, b])
        assert merged is not None
        self.assertEqual(merged["tests"], 1)


class PlanShardsTest(unittest.TestCase):
    def test_lpt_balances_by_duration(self) -> None:
        durations = {"a": 700, "b": 500, "c": 400, "d": 300, "e": 200, "f": 100}
        shards = impact.plan_shards(durations, 3)
        totals = sorted(shard.estimated_ms for shard in shards)
        self.assertEqual(totals, [700, 700, 800])
        self.assertEqual(sorted(s for shard in shards for s in shard.specs), sorted(durations))

    def test_no_empty_shards(self) -> None:
        shards = impact.plan_shards({"a": 1, "b": 2}, 4)
        self.assertEqual(len(shards), 2)
        self.assertEqual(impact.plan_shards({}, 3), [])

    def test_unknown_spec_uses_median(self) -> None:
        recorded = impact.ImpactMap(
            _map(
                {},
                specs={
                    "a": {"durationMs": 100},
                    "b": {"durationMs": 300},
                    "c": {"durationMs": 900},
                },
            )
        )
        self.assertEqual(impact.spec_durations(["a", "new"], recorded), {"a": 100, "new": 300})
        self.assertEqual(impact.spec_durations(["x"], None), {"x": impact.UNKNOWN_SPEC_MS})

    def test_durations_use_pr_scope(self) -> None:
        # PR の見積もりは、PR でも実行するテストの時間（prDurationMs）を使う（#1582）。
        recorded = impact.ImpactMap(
            _map(
                {},
                specs={
                    "a": {"durationMs": 1000, "prDurationMs": 600},
                    "b": {"durationMs": 300},
                    "c": {"durationMs": 900, "prDurationMs": 500},
                },
            )
        )
        self.assertEqual(
            impact.spec_durations(["a", "b", "new"], recorded), {"a": 600, "b": 300, "new": 500}
        )

    def test_matrix_lists_specs_per_shard(self) -> None:
        product = impact.PRODUCTS["rag"]
        selection = impact.Selection(product)
        long = {f"rag/frontend/e2e/s{i}.spec.ts": {"durationMs": 4 * 60 * 1000} for i in range(3)}
        selection.add(long, "変更")
        matrix, shards = impact.shard_matrix(
            product, selection, impact.ImpactMap(_map({}, specs=long))
        )
        self.assertEqual(len(matrix), 2)
        self.assertEqual([entry["shard"] for entry in matrix], [1, 2])
        self.assertEqual({entry["playwright_shard"] for entry in matrix}, {"1/1"})
        listed = " ".join(entry["specs"] for entry in matrix).split()
        self.assertEqual(sorted(listed), [f"e2e/s{i}.spec.ts" for i in range(3)])
        self.assertEqual(len(shards), 2)

    def test_fully_parallel_product_shards_by_test(self) -> None:
        product = impact.PRODUCTS["nl2sql"]
        selection = impact.Selection(product)
        selection.need_full("package.json")
        matrix, shards = impact.shard_matrix(product, selection, None)
        self.assertEqual(len(matrix), product.max_shards)
        self.assertEqual(
            matrix[0], {"shard": 1, "specs": "", "playwright_shard": f"1/{product.max_shards}"}
        )
        self.assertEqual(shards, [])

    def test_split_long_spec_into_test_level_parts(self) -> None:
        # 合計 1200・4 shard（目安 300）。a（600）は a だけの 2 shard に分け、
        # 残りを 2 shard に詰める。
        durations = {"a": 600, "b": 250, "c": 200, "d": 100, "e": 50}
        shards = impact.plan_split_shards(durations, 4)
        self.assertEqual(
            [(s.specs, s.part) for s in shards[:2]], [(("a",), (1, 2)), (("a",), (2, 2))]
        )
        self.assertEqual(len(shards), 4)
        self.assertEqual([s.playwright_shard for s in shards], ["1/2", "2/2", "1/1", "1/1"])
        whole = sorted(spec for s in shards if s.part is None for spec in s.specs)
        self.assertEqual(whole, ["b", "c", "d", "e"])
        self.assertLessEqual(max(s.estimated_ms for s in shards), 300)

    def test_split_leaves_a_shard_for_the_rest(self) -> None:
        # a だけで 3 shard 分あっても、残りの spec に 1 shard を残す。
        shards = impact.plan_split_shards({"a": 900, "b": 10}, 3)
        self.assertEqual([s.part for s in shards], [(1, 2), (2, 2), None])
        self.assertEqual(shards[-1].specs, ("b",))

    def test_split_does_not_exceed_test_count(self) -> None:
        shards = impact.plan_split_shards({"a": 900, "b": 10, "c": 10}, 4, {"a": 2})
        self.assertEqual([s.part for s in shards if s.part], [(1, 2), (2, 2)])
        self.assertEqual(sorted(spec for s in shards if not s.part for spec in s.specs), ["b", "c"])

    def test_split_with_one_shard_runs_everything_together(self) -> None:
        shards = impact.plan_split_shards({"a": 900, "b": 10}, 1)
        self.assertEqual(len(shards), 1)
        self.assertEqual((shards[0].specs, shards[0].part), (("a", "b"), None))

    def test_fully_parallel_product_with_map_splits_long_spec(self) -> None:
        product = impact.PRODUCTS["nl2sql"]
        selection = impact.Selection(product)
        root = "nl2sql/frontend/tests/e2e"
        specs = {f"{root}/long.spec.ts": {"durationMs": 6000, "tests": 30}}
        specs |= {f"{root}/s{i}.spec.ts": {"durationMs": 1000, "tests": 5} for i in range(6)}
        selection.add(specs, "変更")
        recorded = impact.ImpactMap(_map({}, specs=specs))
        with mock.patch.object(impact, "SHARD_TARGET_MS", 3000):
            matrix, shards = impact.shard_matrix(product, selection, recorded)
        self.assertEqual(len(matrix), 4)
        self.assertEqual(
            [(m["specs"], m["playwright_shard"]) for m in matrix[:2]],
            [("tests/e2e/long.spec.ts", "1/2"), ("tests/e2e/long.spec.ts", "2/2")],
        )
        rest = " ".join(m["specs"] for m in matrix[2:]).split()
        self.assertEqual(sorted(rest), [f"tests/e2e/s{i}.spec.ts" for i in range(6)])
        self.assertEqual({m["playwright_shard"] for m in matrix[2:]}, {"1/1"})


class SkipSmokeSpecsTest(unittest.TestCase):
    def test_removes_smoke_specs_from_selection(self) -> None:
        selection = impact.Selection(impact.PRODUCTS["rag"])
        selection.add(["rag/frontend/e2e/a.spec.ts", "rag/frontend/e2e/b.spec.ts"], "変更")
        impact.skip_smoke_specs(selection, ["e2e/a.spec.ts", "e2e/c.spec.ts"])
        self.assertEqual(list(selection.specs), ["rag/frontend/e2e/b.spec.ts"])
        self.assertTrue(any("smoke" in note for note in selection.notes))

    def test_keeps_full_run(self) -> None:
        selection = impact.Selection(impact.PRODUCTS["rag"])
        selection.need_full("package.json")
        selection.add(["rag/frontend/e2e/a.spec.ts"], "変更")
        impact.skip_smoke_specs(selection, ["e2e/a.spec.ts"])
        self.assertIn("rag/frontend/e2e/a.spec.ts", selection.specs)


MODEL_MESSAGES = "platform/packages/system-settings/src/model/messages.ts"
AUTH_MESSAGES = "platform/packages/system-settings/src/auth/messages.ts"


class SelectIntegrationTest(unittest.TestCase):
    """一時的な git repo で select を通しで確かめる。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "test")
        files = {
            "rag/frontend/package.json": "{}\n",
            "rag/frontend/src/Page.tsx": "import x from './x';\n\nexport function Page() {\n"
            "  const label = t('page.title');\n  return label;\n}\n",
            "rag/frontend/src/Other.tsx": (
                'export function Other() {\n  return t("other.title");\n}\n'
            ),
            "rag/frontend/src/lib/i18n.ts": 'export const ja = {\n  "page.title": "ページ",\n'
            '  "other.title": "ほかの画面",\n};\n',
            "rag/frontend/src/lib/api.ts": API_TS,
            "rag/frontend/e2e/fixtures/test.ts": "export const test = 1;\n",
            "rag/frontend/e2e/fixtures/helper.ts": "export const helper = 1;\n",
            "rag/frontend/e2e/page.spec.ts": "import { test } from './fixtures/test';\n",
            "rag/frontend/e2e/other.spec.ts": "import { helper } from './fixtures/helper';\n"
            "// ほかの画面\n// 乱数の種の説明\n",
            "rag/frontend/e2e/node.spec.ts": "import { test } from './fixtures/test';\n",
            "rag/frontend/e2e/model.spec.ts": "import { test } from './fixtures/test';\n",
            # 共有 UI の文言の辞書（#1580）
            MODEL_MESSAGES: "export const MODEL_MESSAGES = {\n"  # 1
            '  "settings.model.seed.help":\n'  # 2
            '    "乱数の種の説明",\n'  # 3
            '  "orphan.label": "組み立てて使う",\n'  # 4
            "} as const;\n"  # 5
            "\n"  # 6
            "export function t(key: string): string {\n"  # 7
            "  return MODEL_MESSAGES[key] ?? key;\n"  # 8
            "}\n",  # 9
            "platform/packages/system-settings/src/model/ModelSettingsPage.tsx": (
                'import { t } from "./messages";\n\nexport function ModelSettingsPage() {\n'
                '  return t("settings.model.seed.help");\n}\n'
            ),
            AUTH_MESSAGES: 'export const AUTH_MESSAGES = {\n  loginTitle: "ログイン",\n'
            '  loginHint: "説明",\n} as const;\n',
            "platform/packages/system-settings/src/auth/AuthPages.tsx": (
                "export function LoginPage(m) {\n  return m.loginTitle;\n}\n"
            ),
        }
        for path, text in files.items():
            self._write(path, text)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "base")
        self.base = self._git("rev-parse", "HEAD").strip()
        self._git("checkout", "-q", "-b", "feature")
        page = "rag/frontend/e2e/page.spec.ts"
        other = "rag/frontend/e2e/other.spec.ts"
        node = "rag/frontend/e2e/node.spec.ts"
        model = "rag/frontend/e2e/model.spec.ts"
        self.map = impact.ImpactMap(
            {
                **_map(
                    {
                        "rag/frontend/src/Page.tsx": {
                            "loadedBy": [page, other],
                            "functions": {"3:6": {"specs": [page], "tests": 1}},
                        },
                        "rag/frontend/src/Other.tsx": {
                            "loadedBy": [page, other],
                            "functions": {"1:3": {"specs": [other], "tests": 1}},
                        },
                        "rag/frontend/src/lib/api.ts": {
                            "loadedBy": [page, other],
                            "functions": {"14:16": {"specs": [page], "tests": 1}},
                        },
                        # 辞書は module の先頭（全テストで実行される）で評価される。
                        MODEL_MESSAGES: {
                            "loadedBy": [page, other, model],
                            "functions": {
                                "1:9": {"specs": [page, other, model], "tests": 3},
                                "7:9": {"specs": [model], "tests": 1},
                            },
                        },
                        "platform/packages/system-settings/src/model/ModelSettingsPage.tsx": {
                            "loadedBy": [page, other, model],
                            "functions": {"3:5": {"specs": [model], "tests": 1}},
                        },
                        AUTH_MESSAGES: {
                            "loadedBy": [page, other, model],
                            "functions": {"1:4": {"specs": [page, other, model], "tests": 3}},
                        },
                        "platform/packages/system-settings/src/auth/AuthPages.tsx": {
                            "loadedBy": [page, other, model],
                            "functions": {"1:3": {"specs": [other], "tests": 1}},
                        },
                    },
                    tests=3,
                    specs={
                        page: {"durationMs": 1000, "tests": 1},
                        other: {"durationMs": 1000},
                        model: {"durationMs": 1000},
                    },
                    commit=self.base,
                ),
            }
        )
        self.node = node
        self.page = page
        self.other = other
        self.model = model
        self._patch = mock.patch.object(impact, "ROOT", self.root)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        ).stdout

    def _write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def _commit_and_select(self) -> impact.Selection:
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "change")
        return impact.select(impact.PRODUCTS["rag"], self.map, self.base)

    def test_changed_function_selects_spec_and_unrecorded_spec(self) -> None:
        self._write(
            "rag/frontend/src/Page.tsx",
            (self.root / "rag/frontend/src/Page.tsx")
            .read_text()
            .replace("return label;", "return label + 1;"),
        )
        selection = self._commit_and_select()
        self.assertEqual(selection.full, [])
        # 記録の無い spec（node.spec.ts）は常に選ぶ。
        self.assertEqual(set(selection.specs), {self.page, self.node})

    def _edit_api(self, old: str, new: str) -> impact.Selection:
        text = (self.root / "rag/frontend/src/lib/api.ts").read_text()
        self.assertIn(old, text)
        self._write("rag/frontend/src/lib/api.ts", text.replace(old, new))
        return self._commit_and_select()

    def test_interface_only_change_selects_nothing(self) -> None:
        selection = self._edit_api(
            "  a?: string;\n", "  a?: string;\n  /** 追加 */\n  b?: number;\n"
        )
        self.assertEqual(selection.specs, {})
        self.assertEqual(selection.full, [])
        self.assertTrue(any("型の宣言・コメントだけ" in note for note in selection.notes))

    def test_comment_only_change_selects_nothing(self) -> None:
        selection = self._edit_api("// 読み込み\n", "// 一覧を読み込む\n")
        self.assertEqual(selection.specs, {})

    def test_const_change_selects_specs(self) -> None:
        selection = self._edit_api("LIMIT = 10", "LIMIT = 20")
        self.assertIn(self.page, selection.specs)

    def test_enum_change_selects_specs(self) -> None:
        selection = self._edit_api('  A = "a",\n', '  A = "a",\n  B = "b",\n')
        self.assertIn(self.page, selection.specs)

    def test_function_body_change_selects_specs(self) -> None:
        selection = self._edit_api("return LIMIT;", "return LIMIT + 1;")
        self.assertEqual(set(selection.specs), {self.page, self.node})

    def test_i18n_key_selects_referencing_screen_and_old_text_spec(self) -> None:
        text = (self.root / "rag/frontend/src/lib/i18n.ts").read_text()
        self._write("rag/frontend/src/lib/i18n.ts", text.replace("ほかの画面", "別の画面"))
        selection = self._commit_and_select()
        self.assertIn(self.other, selection.specs)
        self.assertNotIn(self.page, selection.specs)

    def _edit(self, path: str, old: str, new: str) -> impact.Selection:
        text = (self.root / path).read_text()
        self.assertIn(old, text)
        self._write(path, text.replace(old, new))
        return self._commit_and_select()

    def test_platform_message_selects_referencing_screen_and_old_text_spec(self) -> None:
        # #1569: 共有 UI の文言を 1 つ変えただけで、全 spec を選んでいた（#1580）。
        selection = self._edit(MODEL_MESSAGES, "乱数の種の説明", "短い説明")
        self.assertEqual(selection.full, [])
        self.assertEqual(set(selection.specs), {self.model, self.other, self.node})
        self.assertEqual(selection.specs[self.model], ["文言 settings.model.seed.help"])
        self.assertEqual(
            selection.specs[self.other], ["古い文言 settings.model.seed.help を書いた spec"]
        )

    def test_platform_identifier_key_selects_referencing_screen(self) -> None:
        selection = self._edit(AUTH_MESSAGES, '"ログイン"', '"サインイン"')
        self.assertEqual(set(selection.specs), {self.other, self.node})
        self.assertEqual(selection.specs[self.other], ["文言 loginTitle"])

    def test_platform_key_without_reference_selects_dictionary_users(self) -> None:
        # 組み立てて使うキー（参照元が見つからない）は、辞書を読み込んだ spec すべてにする。
        selection = self._edit(MODEL_MESSAGES, '"組み立てて使う"', '"別の文言"')
        self.assertEqual(set(selection.specs), {self.page, self.other, self.model, self.node})
        self.assertIn(
            "文言 orphan.label（参照元が見つからない）（全画面の共通の処理）",
            selection.specs[self.page],
        )

    def test_platform_dictionary_function_change_selects_by_lines(self) -> None:
        selection = self._edit(MODEL_MESSAGES, "?? key;", "?? `[${key}]`;")
        self.assertEqual(set(selection.specs), {self.model, self.node})
        self.assertEqual(selection.specs[self.model], ["i18n の処理の変更"])

    def test_helper_change_selects_dependent_spec(self) -> None:
        self._write("rag/frontend/e2e/fixtures/helper.ts", "export const helper = 2;\n")
        selection = self._commit_and_select()
        self.assertIn(self.other, selection.specs)
        self.assertNotIn(self.page, selection.specs)

    def test_package_json_runs_everything(self) -> None:
        self._write("rag/frontend/package.json", '{"name": "x"}\n')
        selection = self._commit_and_select()
        self.assertTrue(selection.full)

    def test_without_map_runs_everything(self) -> None:
        self._write("rag/frontend/src/Other.tsx", "export function Other() {\n  return 1;\n}\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "change")
        selection = impact.select(impact.PRODUCTS["rag"], None, self.base)
        self.assertTrue(selection.full)

    # 主要な画面の axe などの nightly だけの spec（*.nightly.spec.ts）は、記録が無くても、変えても、
    # 全件でも選ばない（#1595）。
    NIGHTLY = "rag/frontend/e2e/a11y.nightly.spec.ts"

    def test_unrecorded_nightly_only_spec_is_not_selected(self) -> None:
        self._write(self.NIGHTLY, "import { test } from './fixtures/test';\n")
        selection = self._edit("rag/frontend/src/Page.tsx", "return label;", "return label + 1;")
        self.assertEqual(set(selection.specs), {self.page, self.node})
        self.assertNotIn(self.NIGHTLY, impact.list_specs(impact.PRODUCTS["rag"]))

    def test_changed_nightly_only_spec_is_not_selected(self) -> None:
        self._write(self.NIGHTLY, "import { test } from './fixtures/test';\n// x\n")
        selection = self._commit_and_select()
        self.assertEqual(selection.specs, {})
        self.assertEqual(selection.full, [])

    def test_full_run_excludes_nightly_only_spec(self) -> None:
        self._write(self.NIGHTLY, "import { test } from './fixtures/test';\n")
        self._write("rag/frontend/package.json", '{"name": "x"}\n')
        selection = self._commit_and_select()
        self.assertTrue(selection.full)
        matrix, _ = impact.shard_matrix(impact.PRODUCTS["rag"], selection, self.map)
        specs = " ".join(row["specs"] for row in matrix)
        self.assertIn("e2e/page.spec.ts", specs)
        self.assertNotIn("nightly", specs)

    def test_backend_change_selects_nothing(self) -> None:
        self._write("rag/backend/app.py", "x = 1\n")
        selection = self._commit_and_select()
        self.assertEqual(selection.specs, {})
        self.assertEqual(selection.full, [])

    def _run_select(self, *extra: str) -> tuple[mock.MagicMock, str]:
        out = self.root / "github-output.txt"
        with (
            mock.patch.object(impact, "download_maps", return_value=[]) as download,
            mock.patch("sys.stdout"),
        ):
            impact.main(
                [
                    "select", "--product", "rag", "--base", self.base, "--download",
                    "--github-output", str(out), "--step-summary", "", *extra,
                ]
            )  # fmt: skip
        return download, out.read_text(encoding="utf-8")

    def test_select_does_not_download_without_frontend_change(self) -> None:
        self._write("rag/backend/app.py", "x = 1\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "change")
        download, output = self._run_select()
        download.assert_not_called()
        self.assertIn("rag_mode=none", output)

    def test_select_downloads_and_skips_smoke_specs(self) -> None:
        spec = "import { test } from './fixtures/test';\n// x\n"
        self._write("rag/frontend/e2e/page.spec.ts", spec)
        self._write("rag/frontend/e2e/node.spec.ts", spec)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "change")
        download, output = self._run_select("--skip-specs", "e2e/page.spec.ts")
        download.assert_called_once()
        self.assertIn("rag_mode=selected", output)
        self.assertIn("e2e/node.spec.ts", output)
        self.assertNotIn("e2e/page.spec.ts", output)

    def test_check_imports_rejects_direct_test_import(self) -> None:
        self._write(
            "rag/frontend/e2e/bad.spec.ts",
            'import { expect, test } from "@playwright/test";\n',
        )
        self._write(
            "rag/frontend/e2e/ok.spec.ts",
            'import type { Page } from "@playwright/test";\n'
            'import { expect } from "@playwright/test";\n',
        )
        errors = impact.check_imports([impact.PRODUCTS["rag"]])
        self.assertEqual(len(errors), 1)
        self.assertIn("bad.spec.ts", errors[0])
        self.assertIn("./fixtures/test", errors[0])


class NightlyShardsTest(unittest.TestCase):
    """nightly の shard の数と、記録がそろった run だけを使うこと（#1599）。"""

    def test_nightly_matrix_matches_products(self) -> None:
        workflow = (impact.ROOT / ".github/workflows/e2e-nightly.yml").read_text(encoding="utf-8")
        rows = re.findall(r"\{ product: (\w+),.*?shard: (\d+), total: (\d+) \}", workflow)
        self.assertTrue(rows)
        for name, product in impact.PRODUCTS.items():
            shards = sorted(int(shard) for row, shard, _ in rows if row == name)
            totals = {int(total) for row, _, total in rows if row == name}
            self.assertEqual(shards, list(range(1, product.nightly_shards + 1)), name)
            self.assertEqual(totals, {product.nightly_shards}, name)

    def _download(self, runs: list[tuple[int, int]]) -> list[Path]:
        """runs は (run の id, その run の記録の数)。新しい順。"""
        product = impact.PRODUCTS["agent"]
        counts = dict(runs)

        def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess:
            if args[:3] == ["gh", "run", "list"]:
                body = json.dumps([{"databaseId": run} for run, _ in runs])
                return subprocess.CompletedProcess(args, 0, body, "")
            run = int(args[3])
            target = Path(args[args.index("--dir") + 1])
            for shard in range(1, counts[run] + 1):
                path = target / f"e2e-impact-agent-{shard}" / f"agent-{shard}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(impact.subprocess, "run", side_effect=fake_run),
                mock.patch("sys.stderr"),
            ):
                files = impact.download_maps(product, Path(tmp))
            return [path.relative_to(tmp) for path in files]

    def test_download_skips_run_with_missing_shard(self) -> None:
        shards = impact.PRODUCTS["agent"].nightly_shards
        files = self._download([(2, shards - 1), (1, shards)])
        self.assertEqual(len(files), shards)
        self.assertTrue(all(path.parts[0] == "1" for path in files))

    def test_download_returns_nothing_without_complete_run(self) -> None:
        shards = impact.PRODUCTS["agent"].nightly_shards
        self.assertEqual(self._download([(2, shards - 1), (1, 0)]), [])


if __name__ == "__main__":
    unittest.main()
