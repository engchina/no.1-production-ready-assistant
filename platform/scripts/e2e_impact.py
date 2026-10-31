#!/usr/bin/env python3
"""e2e の影響分析: PR の差分に影響を受ける Playwright の spec を選ぶ（#885）。

nightly（`.github/workflows/e2e-nightly.yml`）は 3 製品の e2e を `E2E_IMPACT_DIR` 付きで
全件実行し、テストごとに「実行された関数の元の行の範囲」を記録する
（`platform/scripts/e2e-impact/fixture.ts`）。`aggregate` はそれを shard ごとに 1 つの JSON に
まとめ、artifact（`e2e-impact-<製品>-<shard>`）にする。

PR の CI（`.github/workflows/ci.yml` の `e2e-impact`）は `select` で、base との差分の行を
実行した spec を選び、選んだ spec だけを shard に分けて実行する。選び方:

- 製品の frontend の source・共有 UI（`platform/packages/{ui,system-settings}/src`）の変更した行
  - その行を含む関数を実行した spec。すべてのテストで実行される関数（画面の枠・読み込み時の
    処理）なら、そのファイルを読み込んだ spec すべて
  - 関数の外の行（import・定数）は、そのファイルの関数を実行した spec
  - TypeScript の型の宣言（`interface`・`type`・`import type` / `export type`・`declare`）の
    中の行と、コメント・空行だけの変更は選ばない（実行時に影響しない。`enum`・`const` は
    選ぶ。#1488）
- 文言の辞書（製品の `src/lib/*i18n*.ts` と、共有 UI の `src/**/messages*.ts`）は、変更した
  キーを参照する source（製品と共有 UI）の行に置き換える。古い文言をそのまま書いている spec も
  選ぶ。共有 UI の辞書で参照元が見つからないキー（組み立てたキーなど）は、辞書の変更した行で選ぶ
  （辞書を読み込んだ spec すべて。#1580）。辞書の外（関数など）の変更は行で選ぶ
- 変更・追加した spec と、変更した e2e の helper / fixture を使う spec
- coverage の記録が無い spec（新しい spec・ページを使わない spec）は常に選ぶ
- 判定できないもの（記録が無い、package.json / lock / vite / playwright の設定、CSS、
  記録の仕組み自体）は全件

選んだ spec は、nightly の実測の時間（記録が無い spec は中央値）で LPT（長い spec から、合計が
最も短い shard へ）で shard に詰め、shard ごとの spec を matrix に出す（#1488）。`fullyParallel`
の製品（NL2SQL）は、1 shard の目安より長い spec をその spec だけの Playwright の `--shard` で
テスト単位に分け、残りの spec を LPT で詰める（#1525）。記録が無ければ全体を `--shard` で分ける。

PR の CI の mobile の project は、画面幅に依存するテスト（`@responsive` など）だけを実行する
（`platform/scripts/e2e-impact/mobile-scope.ts`。#1582）。時間の見積もりは、nightly の記録の
うち PR でも実行するテストの時間（`prDurationMs`。記録の fixture の `prScope`）を使う。

PR の smoke（ci.yml の `*_E2E_SMOKE`）が全 project で実行する spec は `--skip-specs` で渡し、
選んだ spec から除く（同じ spec を 2 回実行しない。全件のときは smoke の job を飛ばす。#1525）。

nightly だけで実行する spec（`*.nightly.spec.ts`。主要な画面の axe の確認など）は
spec の一覧から除き、PR では選ばない（全件のときも。
`platform/scripts/e2e-impact/nightly-only.ts`。#1595）。

ローカルで同じ選択を見るには（nightly の記録を `gh` で取得する）:

    python3 platform/scripts/e2e_impact.py select --product rag --base origin/main --download

`check-imports` は、spec が `@playwright/test` から `test` を直接 import していないこと
（記録の fixture を通ること）を確かめる。
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAP_VERSION = 1
# PR の 1 shard の目安（nightly の所要時間で見積もる）。
SHARD_TARGET_MS = 6 * 60 * 1000
UNKNOWN_SPEC_MS = 60 * 1000


@dataclass(frozen=True)
class Product:
    name: str
    frontend: str
    test_dir: str
    # 記録の fixture を足した `test` を export するファイル（frontend からの相対パス）。
    test_module: str
    max_shards: int
    # `.github/workflows/e2e-nightly.yml` の matrix の shard の数（テストが一致を検査する）。
    nightly_shards: int
    # playwright.config の `fullyParallel`。true なら Playwright の `--shard` がテスト単位で
    # 分けるので、spec 単位の LPT より細かく均せる（NL2SQL の nl2sql-workflows.spec.ts は
    # 1 本で全体の 4 割を占める）。
    fully_parallel: bool = False

    @property
    def test_root(self) -> str:
        return f"{self.frontend}/{self.test_dir}"


PRODUCTS = {
    "rag": Product("rag", "rag/frontend", "e2e", "e2e/fixtures/test.ts", 4, 2),
    "nl2sql": Product(
        "nl2sql", "nl2sql/frontend", "tests/e2e", "tests/e2e/_helpers/test.ts", 8, 8, True
    ),
    "agent": Product("agent", "agent/frontend", "e2e", "e2e/fixtures/test.ts", 3, 2),
}
PLATFORM_FRONTEND = ("platform/packages/ui/", "platform/packages/system-settings/")
IMPACT_TOOLING = ("platform/scripts/e2e-impact/",)
CODE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".json")
UNIT_TEST = re.compile(r"(\.test\.[cm]?[jt]sx?$|/__tests__/)")
# nightly（e2e-nightly.yml）だけで実行する spec（主要な画面の axe など）。PR では選ばない（#1595）。
# playwright.config.ts も ci.yml の `E2E_NIGHTLY_ONLY=skip` で除く
# （platform/scripts/e2e-impact/nightly-only.ts）。
NIGHTLY_ONLY_SPEC = re.compile(r"\.nightly\.spec\.ts$")
FULL_RUN_FILES = re.compile(
    r"^(package\.json|package-lock\.json|index\.html|vite\.config\.[cm]?[jt]s|"
    r"playwright\.config\.[cm]?[jt]s|tsconfig[^/]*\.json)$"
)


# ---------------------------------------------------------------- git


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def git_show(ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def has_commit(ref: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{ref}^{{commit}}"], cwd=ROOT, capture_output=True, check=False
    )
    return result.returncode == 0


@dataclass(frozen=True)
class Change:
    status: str  # A / M / D / R
    old: str | None
    new: str | None

    @property
    def paths(self) -> list[str]:
        return [p for p in (self.old, self.new) if p]


def changed_files(base: str, head: str) -> list[Change]:
    out = git("diff", "--name-status", "-M", f"{base}...{head}")
    changes: list[Change] = []
    for line in out.splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        if status == "R":
            changes.append(Change("R", parts[1], parts[2]))
        elif status == "D":
            changes.append(Change("D", parts[1], None))
        elif status == "A":
            changes.append(Change("A", None, parts[1]))
        else:
            changes.append(Change("M", parts[1], parts[1]))
    return changes


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def changed_old_lines(base: str, head: str, old: str, new: str) -> set[int]:
    """差分の base 側の行番号（追加だけの位置は前後の行）。"""
    out = git("diff", "-U0", f"{base}...{head}", "--", old, new)
    lines: set[int] = set()
    for line in out.splitlines():
        match = HUNK.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        if count == 0:
            lines.update({max(start, 1), start + 1})
        else:
            lines.update(range(start, start + count))
    return lines


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def old_lines(self) -> range:
        return range(self.old_start, self.old_start + self.old_count)

    @property
    def new_lines(self) -> range:
        return range(self.new_start, self.new_start + self.new_count)


def diff_hunks(base: str, head: str, old: str, new: str) -> list[Hunk]:
    out = git("diff", "-U0", f"{base}...{head}", "--", old, new)
    hunks = []
    for line in out.splitlines():
        match = HUNK.match(line)
        if match:
            hunks.append(
                Hunk(
                    int(match.group(1)),
                    int(match.group(2) or "1"),
                    int(match.group(3)),
                    int(match.group(4) or "1"),
                )
            )
    return hunks


# ---- 実行時の影響が無い行（TypeScript の型の宣言・コメント・空行。#1488）

TYPESCRIPT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
# 型の宣言は行頭（字下げ無し）で始まるものだけを見る。関数の中の `type` や JSX の文の「type …」を
# 型の宣言とみなさないため（見落としは選びすぎる側に倒れる）。`enum`・`const enum`・`namespace` は
# 実行時のコードを出すので含めない（`declare` が付けば出さない）。
TYPE_DECLARATION = re.compile(
    r"^(?:"
    r"(?P<block>(?:export\s+(?:default\s+)?)?(?:declare\s+)?interface\s+[A-Za-z_$]"
    r"|(?:export\s+)?declare\s+(?:global\b|module\b|namespace\b|class\b|enum\b|const\s+enum\b))"
    r"|(?P<statement>(?:export\s+)?(?:declare\s+)?type\s+[A-Za-z_$][\w$]*\s*[<=]"
    r"|(?:import|export)\s+type\s+(?:\{|\*|[A-Za-z_$][\w$]*\s+from\b)"
    r"|(?:export\s+)?declare\s+)"
    r")"
)
BRACKETS = {"(": ")", "[": "]", "{": "}"}


@dataclass(frozen=True)
class LineState:
    in_template: bool  # 行の始まりがテンプレート文字列の中
    in_comment: bool  # 行の始まりが block コメントの中
    has_code: bool  # コメントの外の文字（文字列の中を含む）がある


def scan_lines(lines: list[str]) -> list[LineState]:
    """行ごとに、始まりの状態（テンプレート文字列・block コメントの中か）とコードの有無を求める。

    `'` / `"` の文字列は行をまたがないので行末で閉じる（JSX の文の `Don't` などで
    読み違えないため）。
    """
    states: list[LineState] = []
    stack: list[str] = []  # "`"（テンプレート）と、その中の `${` の "{"
    in_comment = False
    for text in lines:
        start_template = bool(stack) and stack[-1] == "`"
        start_comment = in_comment
        has_code = start_template and bool(text)
        quote: str | None = None
        position = 0
        while position < len(text):
            char = text[position]
            pair = text[position : position + 2]
            if in_comment:
                if pair == "*/":
                    in_comment = False
                    position += 2
                else:
                    position += 1
                continue
            if quote is not None or (stack and stack[-1] == "`"):
                has_code = True
                closing = quote or "`"
                if char == "\\":
                    position += 2
                    continue
                if quote is None and pair == "${":
                    stack.append("{")
                    position += 2
                    continue
                if char == closing:
                    if quote is None:
                        stack.pop()
                    quote = None
                position += 1
                continue
            if pair == "//":
                break
            if pair == "/*":
                in_comment = True
                position += 2
                continue
            if not char.isspace():
                has_code = True
            if char in "'\"":
                quote = char
            elif char == "`":
                stack.append("`")
            elif char == "{":
                stack.append("{")
            elif char == "}" and stack and stack[-1] == "{":
                stack.pop()
            position += 1
        states.append(LineState(start_template, start_comment, has_code))
    return states


def _declaration_end(lines: list[str], start: int, block: bool) -> int | None:
    """`start`（0 始まり）の行で始まる型の宣言の最後の行。読めなければ None（印を付けない）。

    文字列・コメントを飛ばし、括弧の深さで終わりを探す。文は深さ 0 の `;`、`interface` などの
    block は最初の block の `}` で終わる。`;` の無い文は、深さ 0 で次の行が行頭から始まるところで
    終わりとする。
    """
    stack: list[str] = []
    quote: str | None = None
    in_comment = False
    opened = False
    for index in range(start, len(lines)):
        text = lines[index]
        position = 0
        while position < len(text):
            char = text[position]
            pair = text[position : position + 2]
            if in_comment:
                if pair == "*/":
                    in_comment = False
                    position += 1
            elif quote is not None:
                if char == "\\":
                    position += 1
                elif quote == "`" and pair == "${":
                    stack.append("${")
                    quote = None
                    position += 1
                elif char == quote:
                    quote = None
            elif pair == "//":
                break
            elif pair == "/*":
                in_comment = True
                position += 1
            elif char in "'\"`":
                quote = char
            elif char in BRACKETS:
                stack.append(BRACKETS[char])
                opened = opened or (block and char == "{" and len(stack) == 1)
            elif char in ")]}":
                if char == "}" and stack and stack[-1] == "${":
                    stack.pop()
                    quote = "`"
                elif not stack or stack[-1] != char:
                    return None
                else:
                    stack.pop()
                    if block and opened and not stack:
                        return _end_line(lines, start, index, position + 1)
            elif char == ";" and not stack:
                return _end_line(lines, start, index, position + 1)
            position += 1
        if quote in ("'", '"'):
            return None
        if not block and not stack and quote is None and not in_comment:
            following = next((line for line in lines[index + 1 :] if line.strip()), None)
            if following is not None and not following[0].isspace():
                return index
    return None


def _end_line(lines: list[str], start: int, index: int, after: int) -> int | None:
    """宣言の終わりの後に同じ行でコードが続くなら、その行は数えない。"""
    rest = lines[index][after:].strip().lstrip(";").strip()
    if rest and not rest.startswith("//"):
        return index - 1 if index > start else None
    return index


def non_runtime_lines(text: str) -> set[int]:
    """実行時のコードを含まない行（1 始まり）: 型の宣言（`interface`・`type`・`import type` /
    `export type`・`declare`）の中の行、コメントだけの行、空行。

    標準ライブラリだけの簡易な読み方で、読めないところは実行時のコードとして扱う（選びすぎる側）。
    """
    lines = text.splitlines()
    states = scan_lines(lines)
    result: set[int] = set()
    index = 0
    while index < len(lines):
        state = states[index]
        if state.in_template:
            index += 1
            continue
        if not state.has_code:
            result.add(index + 1)
            index += 1
            continue
        match = None if state.in_comment else TYPE_DECLARATION.match(lines[index])
        if match:
            end = _declaration_end(lines, index, block=match.group("block") is not None)
            if end is not None:
                result.update(range(index + 1, end + 2))
                index = end + 1
                continue
        index += 1
    return result


def changed_runtime_lines(base: str, head: str, old: str, new: str) -> tuple[set[int], int]:
    """`changed_old_lines` と同じ base 側の行（実行時に影響する hunk だけ）と、除いた hunk の数。

    TypeScript のファイルで、hunk の base 側と head 側の行がどちらも型の宣言・コメント・空行
    だけなら、その hunk を除く（`interface` に欄を足しただけで、その型を使う画面の spec を
    すべて選ばないため）。`enum`・`const` などの実行時のコードを含む hunk は従来どおり数える。
    """
    hunks = diff_hunks(base, head, old, new)
    skipped = 0
    if old.endswith(TYPESCRIPT_SUFFIXES) and new.endswith(TYPESCRIPT_SUFFIXES) and hunks:
        point = git("merge-base", base, head).strip() or base
        before = git_show(point, old)
        after = git_show(head, new)
        if before is not None and after is not None:
            before_static = non_runtime_lines(before)
            after_static = non_runtime_lines(after)
            kept = []
            for hunk in hunks:
                if set(hunk.old_lines) <= before_static and set(hunk.new_lines) <= after_static:
                    skipped += 1
                else:
                    kept.append(hunk)
            hunks = kept
    lines: set[int] = set()
    for hunk in hunks:
        if hunk.old_count == 0:
            lines.update({max(hunk.old_start, 1), hunk.old_start + 1})
        else:
            lines.update(hunk.old_lines)
    return lines, skipped


def translate_lines(lines: Iterable[int], source: str, target: str) -> set[int]:
    """`source` の行番号を、`target` の対応する行番号に移す（変わった行は前後の行）。"""
    a = source.splitlines()
    b = target.splitlines()
    mapping: dict[int, int] = {}
    for tag, i1, i2, j1, _j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                mapping[i1 + offset + 1] = j1 + offset + 1
        else:
            for line in range(i1 + 1, i2 + 1):
                mapping[line] = max(j1, 1)
    result: set[int] = set()
    for line in lines:
        if line in mapping:
            result.add(mapping[line])
        elif mapping:
            result.add(max(1, min(line, len(b))))
    return result


# ---------------------------------------------------------------- 記録の集計（nightly）


def aggregate(records_dir: Path, product: str, commit: str) -> dict:
    """テストごとの記録を、shard の集計にする（同じテストの retry は最後のものだけ）。"""
    latest: dict[str, dict] = {}
    for path in sorted(records_dir.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        current = latest.get(record["testId"])
        if current is None or record["retry"] >= current["retry"]:
            latest[record["testId"]] = record
    specs: dict[str, dict] = {}
    files: dict[str, dict] = {}
    tests = 0
    for record in latest.values():
        spec = record["spec"]
        entry = specs.setdefault(
            spec, {"durationMs": 0, "tests": 0, "prDurationMs": 0, "prTests": 0}
        )
        entry["durationMs"] += int(record["durationMs"])
        entry["tests"] += 1
        # PR の CI でも実行するテスト（mobile の project は @responsive など。#1582）。
        # prScope の無い古い fixture の記録は、実行するものとして数える。
        if record.get("prScope", True):
            entry["prDurationMs"] += int(record["durationMs"])
            entry["prTests"] += 1
        if not record["loaded"]:
            continue
        tests += 1
        for loaded in record["loaded"]:
            info = files.setdefault(loaded, {"loadedBy": set(), "functions": {}})
            info["loadedBy"].add(spec)
        for path, ranges in record["executed"].items():
            info = files.setdefault(path, {"loadedBy": set(), "functions": {}})
            info["loadedBy"].add(spec)
            for key in ranges:
                fn = info["functions"].setdefault(key, {"specs": set(), "tests": 0})
                fn["specs"].add(spec)
                fn["tests"] += 1
    return {
        "version": MAP_VERSION,
        "product": product,
        "commit": commit,
        "tests": tests,
        "specs": specs,
        "files": {
            path: {
                "loadedBy": sorted(info["loadedBy"]),
                "functions": {
                    key: {"specs": sorted(fn["specs"]), "tests": fn["tests"]}
                    for key, fn in sorted(info["functions"].items())
                },
            }
            for path, info in sorted(files.items())
        },
    }


def merge_maps(maps: list[dict]) -> dict | None:
    """shard の集計をまとめる。製品・commit がそろわないものは捨てる。"""
    maps = [m for m in maps if m.get("version") == MAP_VERSION]
    if not maps:
        return None
    commit = maps[0]["commit"]
    maps = [m for m in maps if m["commit"] == commit]
    merged: dict = {
        "version": MAP_VERSION,
        "product": maps[0]["product"],
        "commit": commit,
        "shards": len(maps),
        "tests": 0,
        "specs": {},
        "files": {},
    }
    for m in maps:
        merged["tests"] += m["tests"]
        for spec, entry in m["specs"].items():
            target = merged["specs"].setdefault(
                spec, {"durationMs": 0, "tests": 0, "prDurationMs": 0, "prTests": 0}
            )
            target["durationMs"] += entry["durationMs"]
            target["tests"] += entry["tests"]
            # prDurationMs の無い記録（#1582 より前の nightly）は、全件を PR の時間とする
            # （過大の見積もり）。
            target["prDurationMs"] += entry.get("prDurationMs", entry["durationMs"])
            target["prTests"] += entry.get("prTests", entry["tests"])
        for path, info in m["files"].items():
            target = merged["files"].setdefault(path, {"loadedBy": set(), "functions": {}})
            target["loadedBy"].update(info["loadedBy"])
            for key, fn in info["functions"].items():
                tfn = target["functions"].setdefault(key, {"specs": set(), "tests": 0})
                tfn["specs"].update(fn["specs"])
                tfn["tests"] += fn["tests"]
    return merged


# ---------------------------------------------------------------- 選択


@dataclass
class Selection:
    product: Product
    specs: dict[str, list[str]] = field(default_factory=dict)  # spec（repo 相対）→ 理由
    full: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, specs: Iterable[str], reason: str) -> None:
        for spec in specs:
            reasons = self.specs.setdefault(spec, [])
            if reason not in reasons and len(reasons) < 5:
                reasons.append(reason)

    def need_full(self, reason: str) -> None:
        if reason not in self.full:
            self.full.append(reason)


@dataclass(frozen=True)
class Function:
    start: int
    end: int
    specs: frozenset[str]
    is_global: bool


class ImpactMap:
    def __init__(self, data: dict):
        self.commit: str = data["commit"]
        self.tests: int = data["tests"]
        self.specs: dict[str, dict] = data["specs"]
        self.shards: int = data.get("shards", 1)
        self._files: dict[str, dict] = data["files"]

    def loaded_by(self, path: str) -> set[str]:
        info = self._files.get(path)
        return set(info["loadedBy"]) if info else set()

    def knows(self, path: str) -> bool:
        return path in self._files

    def functions(self, path: str) -> list[Function]:
        info = self._files.get(path)
        if not info:
            return []
        result = []
        for key, fn in info["functions"].items():
            start, end = (int(v) for v in key.split(":"))
            result.append(
                Function(
                    start, end, frozenset(fn["specs"]), self.tests > 0 and fn["tests"] >= self.tests
                )
            )
        return result

    def specs_for_lines(self, path: str, lines: Iterable[int]) -> tuple[set[str], bool]:
        """行を実行した spec と、画面の枠など全テストで実行される処理に当たったか。"""
        functions = self.functions(path)
        loaded_by = self.loaded_by(path)
        local = (
            set().union(*(fn.specs for fn in functions if not fn.is_global)) if functions else set()
        )
        selected: set[str] = set()
        shared = False
        for line in lines:
            containing = [fn for fn in functions if fn.start <= line <= fn.end]
            if containing:
                inner = min(containing, key=lambda fn: (fn.end - fn.start, -fn.start))
                if inner.is_global:
                    selected |= loaded_by
                    shared = True
                else:
                    selected |= inner.specs
            elif local:
                selected |= local
            else:
                selected |= loaded_by
                shared = shared or bool(loaded_by)
        return selected, shared


def relative_to_frontend(product: Product, spec: str) -> str:
    return spec[len(product.frontend) + 1 :]


def list_specs(product: Product) -> list[str]:
    """PR で実行しうる spec（nightly だけの `*.nightly.spec.ts` を除く。#1595）。"""
    root = ROOT / product.test_root
    return sorted(
        str(path.relative_to(ROOT)).replace(os.sep, "/")
        for path in root.rglob("*.spec.ts")
        if not NIGHTLY_ONLY_SPEC.search(path.name)
    )


def test_dependents(product: Product, changed: str) -> set[str]:
    """変更した e2e の helper / fixture を（推移的に）使う spec。名前（拡張子なし）で探す。"""
    root = ROOT / product.test_root
    files = {
        str(p.relative_to(ROOT)).replace(os.sep, "/"): p.read_text(
            encoding="utf-8", errors="ignore"
        )
        for p in root.rglob("*")
        if p.is_file() and p.suffix in (".ts", ".mjs", ".js", ".json")
    }
    found: set[str] = set()
    queue = [changed]
    while queue:
        current = queue.pop()
        name = Path(current).name
        stem = re.sub(r"\.(ts|mjs|js|json)$", "", name)
        pattern = re.compile(rf"[/\"']{re.escape(stem)}(\.(ts|mjs|js|json))?[\"']")
        for path, text in files.items():
            if path not in found and path != current and pattern.search(text):
                found.add(path)
                queue.append(path)
    return {path for path in found if path.endswith(".spec.ts")}


# ---- i18n の辞書

ENTRY = re.compile(r'^\s*"([A-Za-z0-9_.\-]+)"\s*:')
# 共有 UI の辞書は `title: "…"` のような識別子のキーも使う（`AUTH_MESSAGES` など。#1580）。
IDENTIFIER_ENTRY = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*:(?!:)")
# 識別子のキーは、行頭の `const X = {` から行頭の `}` までの object の中だけで読む
# （`t(key: …)` の引数や `interface` の欄を辞書のキーとみなさないため）。
OBJECT_START = re.compile(r"^(?:export\s+)?const\s+[A-Za-z_$][\w$]*(?:\s*:[^=]+)?\s*=\s*\{\s*$")
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
# 共有 UI の文言の辞書（`platform/packages/<package>/src/**/messages*.ts`。#1580）。
PLATFORM_DICTIONARY = re.compile(r"^platform/packages/[^/]+/src/(?:.+/)?messages[^/]*\.ts$")


def is_platform_dictionary(path: str) -> bool:
    return bool(PLATFORM_DICTIONARY.match(path)) and not (
        UNIT_TEST.search(path) or path.endswith(".d.ts")
    )


def object_lines(text: str) -> set[int]:
    """行頭の `const X = {` から行頭の `}` までの、object の中の行（1 始まり）。"""
    lines: set[int] = set()
    inside = False
    for number, line in enumerate(text.splitlines(), start=1):
        if inside:
            if line.startswith("}"):
                inside = False
            else:
                lines.add(number)
        elif OBJECT_START.match(line):
            inside = True
    return lines


def parse_dictionary(
    text: str, identifier_keys: bool = False
) -> tuple[dict[str, str], dict[int, str]]:
    """`"key": value` の辞書を読む。キー → 値の文字列、行番号 → その行が属するキー。

    `identifier_keys` なら、行頭の `const X = {` の object の中の `key: value` も読む（共有 UI の
    辞書。#1580）。
    """
    entries: dict[str, list[str]] = {}
    owner: dict[int, str] = {}
    current: str | None = None
    in_object = object_lines(text) if identifier_keys else set()
    for number, line in enumerate(text.splitlines(), start=1):
        match = ENTRY.match(line)
        if match is None and number in in_object:
            match = IDENTIFIER_ENTRY.match(line)
        stripped = line.strip()
        if match:
            current = match.group(1)
            entries[current] = [line[match.end() :]]
            owner[number] = current
        elif (
            current and stripped and not stripped.startswith("//") and not stripped.startswith("}")
        ):
            entries[current].append(line)
            owner[number] = current
            if stripped.endswith(",") and not stripped.endswith("+,"):
                current = None
        else:
            current = None if not stripped or stripped.startswith("}") else current
        if match and line.rstrip().endswith(","):
            current = None
    return {key: "\n".join(parts).strip() for key, parts in entries.items()}, owner


def string_value(raw: str) -> str:
    return "".join(json.loads(f'"{part}"') for part in STRING.findall(raw))


def dictionary_changes(
    old: str, new: str, identifier_keys: bool = False
) -> tuple[set[str], dict[str, str], bool]:
    """変わったキー、そのキーの古い文言、辞書の外（関数など）が変わったか。"""
    old_entries, old_owner = parse_dictionary(old, identifier_keys)
    new_entries, new_owner = parse_dictionary(new, identifier_keys)
    keys = {
        key
        for key in set(old_entries) | set(new_entries)
        if old_entries.get(key) != new_entries.get(key)
    }

    def outside(text: str, owner: dict[int, str]) -> list[str]:
        result = []
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if number in owner or not stripped or stripped.startswith("//"):
                continue
            result.append(stripped)
        return result

    structural = outside(old, old_owner) != outside(new, new_owner)
    old_values = {key: string_value(old_entries[key]) for key in keys if key in old_entries}
    return keys, old_values, structural


def source_texts(product: Product, dictionaries: set[str]) -> dict[str, list[str]]:
    """キーを探す source（製品と共有 UI の `src` の .ts / .tsx。辞書と単体テストを除く）の行。"""
    roots = [ROOT / product.frontend / "src", *(ROOT / p / "src" for p in PLATFORM_FRONTEND)]
    texts: dict[str, list[str]] = {}
    for src in roots:
        for path in sorted(src.rglob("*")) if src.is_dir() else []:
            if path.suffix not in (".ts", ".tsx") or UNIT_TEST.search(path.name):
                continue
            rel = str(path.relative_to(ROOT)).replace(os.sep, "/")
            if rel in dictionaries:
                continue
            texts[rel] = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return texts


def key_references(
    product: Product,
    key: str,
    dictionaries: set[str],
    texts: dict[str, list[str]] | None = None,
) -> dict[str, set[int]]:
    """キーを参照する source の行（HEAD の行番号）。製品と共有 UI の `src` を探す（#1580）。

    `a.b.c` のキーは、文字列のキーが無ければ動的なキーとして接頭辞で探す。識別子のキー
    （`title`。共有 UI の辞書）は、語として現れる行（`m.title`・`{ title }`・`"title"`）を探す。
    """
    if texts is None:
        texts = source_texts(product, dictionaries)
    candidates = [re.compile(rf"[\"'`]{re.escape(key)}[\"'`]")]
    if re.fullmatch(r"[A-Za-z_$][\w$]*", key):
        candidates = [re.compile(rf"(?<![\w$]){re.escape(key)}(?![\w$])")]
    prefix = key.rsplit(".", 1)[0] if "." in key else None
    if prefix:
        candidates.append(re.compile(rf"[\"'`]{re.escape(prefix)}\.(\$\{{|[\"'`])"))
        candidates.append(re.compile(rf"[\"'`]{re.escape(prefix)}\."))
    for pattern in candidates:
        found: dict[str, set[int]] = {}
        for rel, lines in texts.items():
            for number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    found.setdefault(rel, set()).add(number)
        if found:
            return found
    return {}


def specs_mentioning(product: Product, text: str) -> set[str]:
    if len(text) < 2:
        return set()
    fragments = [f for f in re.split(r"\{[^}]*\}", text) if len(f.strip()) >= 2]
    if not fragments:
        return set()
    needle = max(fragments, key=len).strip()
    result = set()
    for spec in list_specs(product):
        if needle in (ROOT / spec).read_text(encoding="utf-8", errors="ignore"):
            result.add(spec)
    return result


# ---- 本体


def is_relevant_source(product: Product, path: str) -> bool:
    return path.startswith(f"{product.frontend}/src/") or (
        path.startswith(PLATFORM_FRONTEND) and "/src/" in path
    )


def select(product: Product, impact: ImpactMap | None, base: str, head: str = "HEAD") -> Selection:
    selection = Selection(product)
    all_specs = set(list_specs(product))
    dictionaries: set[str] = {
        str(p.relative_to(ROOT)).replace(os.sep, "/")
        for p in (ROOT / product.frontend / "src" / "lib").glob("*i18n*.ts")
        if not UNIT_TEST.search(p.name)
    }
    # 共有 UI の文言の辞書（#1580）。module の先頭で評価されるため、行で選ぶと全画面の共通の処理に
    # なる（全 spec を選ぶ）。製品の辞書と同じくキー単位で参照元に置き換える。
    for package in PLATFORM_FRONTEND:
        src = ROOT / package / "src"
        for p in src.rglob("messages*.ts") if src.is_dir() else []:
            rel = str(p.relative_to(ROOT)).replace(os.sep, "/")
            if is_platform_dictionary(rel):
                dictionaries.add(rel)
    texts: dict[str, list[str]] | None = None

    def source_lines(path: str, lines: set[int], reason: str, ref: str) -> None:
        """`ref` の行番号の `lines` を、記録の commit の行番号に移して spec を選ぶ。"""
        assert impact is not None
        if not impact.knows(path):
            selection.notes.append(f"{path}: どの spec も実行していない（記録に無い）")
            return
        mapped = lines
        if ref != impact.commit:
            before = git_show(ref, path)
            recorded = git_show(impact.commit, path) if has_commit(impact.commit) else None
            if before is not None and recorded is not None:
                mapped = translate_lines(lines, before, recorded) if before != recorded else lines
            elif recorded is None:
                # 記録の commit が無い（取得できない）ときは、ファイルを実行した spec すべてにする。
                mapped = {fn.start for fn in impact.functions(path)} or lines
        specs, shared = impact.specs_for_lines(path, mapped)
        label = f"{reason}（全画面の共通の処理）" if shared else reason
        if not specs:
            selection.notes.append(f"{path}: 変更した行を実行した spec が無い")
        selection.add(specs, label)

    for change in changed_files(base, head):
        paths = change.paths
        primary = change.new or change.old or ""
        if primary.startswith(IMPACT_TOOLING) or any(
            p == "platform/scripts/e2e_impact.py" for p in paths
        ):
            if primary.startswith(IMPACT_TOOLING):
                selection.need_full(f"{primary}（記録の仕組み）")
            continue

        # e2e の spec / helper
        if any(p.startswith(product.test_root + "/") for p in paths):
            if change.new and change.new.endswith(".spec.ts") and change.new in all_specs:
                selection.add([change.new], "変更した spec")
                continue
            if change.new == f"{product.frontend}/{product.test_module}":
                selection.need_full(f"{change.new}（全 spec の fixture）")
                continue
            for path in paths:
                if not path.endswith(".spec.ts"):
                    selection.add(test_dependents(product, path), f"helper の変更: {path}")
            continue

        # 製品の frontend の設定・依存
        for path in paths:
            if path.startswith(product.frontend + "/") and not path.startswith(
                f"{product.frontend}/src/"
            ):
                rest = path[len(product.frontend) + 1 :]
                if FULL_RUN_FILES.match(rest) or rest.startswith("public/"):
                    selection.need_full(f"{path}（frontend の設定・依存）")
            elif path.startswith(PLATFORM_FRONTEND) and "/src/" not in path:
                rest = path.split("/", 3)[3] if path.count("/") >= 3 else path
                if FULL_RUN_FILES.match(rest):
                    selection.need_full(f"{path}（共有 UI の設定・依存）")
            elif path in ("platform/package.json", "platform/package-lock.json"):
                selection.need_full(f"{path}（共有 UI の依存）")

        sources = [p for p in paths if is_relevant_source(product, p)]
        if not sources:
            continue
        if any(p.endswith(".css") for p in sources):
            selection.need_full(f"{primary}（CSS）")
            continue
        if all(UNIT_TEST.search(p) or p.endswith(".d.ts") for p in sources):
            continue
        if not any(p.endswith(CODE_SUFFIXES) for p in sources):
            selection.need_full(f"{primary}（画像などの資産）")
            continue
        if impact is None:
            selection.need_full("nightly の記録（coverage map）が無い")
            continue

        if change.status == "A":
            selection.notes.append(f"{primary}: 新しいファイル（import する側の変更で選ぶ）")
            continue

        old = change.old
        assert old is not None
        if old in dictionaries and change.new:
            before = git_show(base, old) or ""
            after = (ROOT / change.new).read_text(encoding="utf-8")
            shared_dictionary = is_platform_dictionary(old)
            keys, old_values, structural = dictionary_changes(before, after, shared_dictionary)
            if structural:
                source_lines(
                    old, changed_old_lines(base, head, old, change.new), "i18n の処理の変更", base
                )
            if texts is None:
                texts = source_texts(product, dictionaries)
            unresolved: list[str] = []
            for key in sorted(keys):
                refs = key_references(product, key, dictionaries, texts)
                if not refs:
                    selection.notes.append(f"{old}: キー {key} を参照する source が無い")
                    unresolved.append(key)
                for ref_path, lines in refs.items():
                    source_lines(ref_path, lines, f"文言 {key}", head)
                if key in old_values:
                    selection.add(
                        specs_mentioning(product, old_values[key]), f"古い文言 {key} を書いた spec"
                    )
            if shared_dictionary and unresolved and not structural:
                # 共有 UI のキーは、製品が組み立てて使う（`Object.keys(…_MESSAGES)`）ことが
                # ある。参照元が見つからなければ、辞書の変更した行で選ぶ（辞書を読み込んだ spec
                # すべて）。
                source_lines(
                    old,
                    changed_old_lines(base, head, old, change.new),
                    f"文言 {unresolved[0]}（参照元が見つからない）",
                    base,
                )
            continue

        if change.status == "D":
            lines = {fn.start for fn in impact.functions(old)} or {1}
        else:
            # 型の宣言・コメントだけの hunk は実行時に影響しないので、選択の理由にしない（#1488）。
            lines, skipped = changed_runtime_lines(base, head, old, change.new or old)
            if not lines:
                if skipped:
                    selection.notes.append(
                        f"{old}: 型の宣言・コメントだけの変更（spec を選ばない）"
                    )
                continue
        source_lines(old, lines, f"変更: {old}", base)

    if impact is not None:
        unrecorded = sorted(spec for spec in all_specs if spec not in impact.specs)
        if unrecorded and selection.specs:
            selection.add(unrecorded, "coverage の記録が無い spec（常に実行）")
    selection.specs = {spec: r for spec, r in selection.specs.items() if spec in all_specs}
    return selection


def pr_duration(entry: dict) -> int:
    """PR の CI で実行するテストの時間（mobile の project は @responsive などだけ。#1582）。"""
    return int(entry.get("prDurationMs", entry["durationMs"]))


def pr_tests(entry: dict) -> int:
    return int(entry.get("prTests", entry.get("tests", 0)))


def spec_durations(specs: Iterable[str], impact: ImpactMap | None) -> dict[str, int]:
    """spec ごとの見積もりの時間（nightly の実測のうち、PR の CI でも実行するテストの合計。記録が
    無い spec は、記録のある spec の中央値）。"""
    known = sorted(pr_duration(entry) for entry in (impact.specs.values() if impact else []))
    default = known[len(known) // 2] if known else UNKNOWN_SPEC_MS
    recorded = impact.specs if impact else {}
    return {spec: pr_duration(recorded[spec]) if spec in recorded else default for spec in specs}


def shard_count(product: Product, selection: Selection, impact: ImpactMap | None) -> int:
    if selection.full:
        return product.max_shards
    if not selection.specs:
        return 0
    total = sum(spec_durations(selection.specs, impact).values())
    return max(1, min(product.max_shards, math.ceil(total / SHARD_TARGET_MS)))


@dataclass(frozen=True)
class Shard:
    specs: tuple[str, ...]
    estimated_ms: int
    # 1 つの spec を Playwright の `--shard` でテスト単位に分けた部分（(i, k) は `--shard i/k`）。
    # None なら specs のテストをすべて実行する。
    part: tuple[int, int] | None = None

    @property
    def playwright_shard(self) -> str:
        return f"{self.part[0]}/{self.part[1]}" if self.part else "1/1"


def plan_shards(durations: dict[str, int], count: int) -> list[Shard]:
    """spec を時間で shard に詰める（LPT: 長い spec から、見積もりの合計が最も短い shard へ。
    #1488）。

    `--shard` は件数で分けて時間を見ないため、最大と最小の shard に 2 倍の差が出ていた。
    空の shard は作らない。
    """
    count = min(count, len(durations))
    if count <= 0:
        return []
    bins: list[list[str]] = [[] for _ in range(count)]
    totals = [0] * count
    for spec in sorted(durations, key=lambda s: (-durations[s], s)):
        target = min(range(count), key=lambda i: (totals[i], i))
        bins[target].append(spec)
        totals[target] += durations[spec]
    return [Shard(tuple(sorted(specs)), total) for specs, total in zip(bins, totals, strict=True)]


def plan_split_shards(
    durations: dict[str, int], count: int, tests: dict[str, int] | None = None
) -> list[Shard]:
    """`fullyParallel` の製品の shard（#1525）。

    1 shard の目安（合計 / shard 数）より長い spec は、その spec だけを Playwright の `--shard i/k`
    でテスト単位に k 個（目安で割って丸めた数。テストの数を超えない）に分け、それぞれを 1 つの
    shard にする。残りの spec は LPT で残りの shard に詰める。どのテストも 1 回だけ実行する。

    全 spec を 1 回の `--shard` で分けると、件数で前から区切るため重いテストが固まった shard だけが
    倍近く遅くなっていた（NL2SQL の全件の 8 shard で、1 つだけ 13 分・他は 6〜9 分）。
    """
    # 分けられる数の上限はテストの数（記録の無い spec は上限を設けない）。
    capacity = {spec: max(1, (tests or {}).get(spec, count)) for spec in durations}
    count = min(count, sum(capacity.values()))
    if count <= 1 or not durations:
        return plan_shards(durations, count)
    target = sum(durations.values()) / count
    parts: dict[str, int] = {}
    for spec, ms in durations.items():
        k = min(round(ms / target), capacity[spec], count)
        if k >= 2:
            parts[spec] = k
    rest = [spec for spec in durations if spec not in parts]
    # 分けた部分で shard を使い切らない（残りの spec に少なくとも 1 shard を残す）。
    while parts and sum(parts.values()) > count - (1 if rest else 0):
        largest = max(parts, key=lambda s: (parts[s], durations[s], s))
        parts[largest] -= 1
        if parts[largest] < 2:
            del parts[largest]
            rest.append(largest)
    shards = [
        Shard((spec,), durations[spec] // k, (i, k))
        for spec, k in sorted(parts.items())
        for i in range(1, k + 1)
    ]
    shards += plan_shards({spec: durations[spec] for spec in rest}, count - len(shards))
    return shards


def shard_matrix(
    product: Product, selection: Selection, impact: ImpactMap | None
) -> tuple[list[dict], list[Shard]]:
    """CI の matrix の include と、spec を時間で詰めた shard（`fully_parallel` の製品は空）。

    include の各要素は `shard`（番号）・`specs`（その shard で実行する spec。frontend からの
    相対パスを空白区切り。空なら全 spec）・`playwright_shard`（`playwright test --shard` の値）。
    """
    count = shard_count(product, selection, impact)
    if count == 0:
        return [], []
    if product.fully_parallel and impact is None:
        # 時間の記録が無いので、全体を Playwright の `--shard` でテスト単位に分ける。
        specs = (
            ""
            if selection.full
            else " ".join(relative_to_frontend(product, spec) for spec in sorted(selection.specs))
        )
        matrix = [
            {"shard": index, "specs": specs, "playwright_shard": f"{index}/{count}"}
            for index in range(1, count + 1)
        ]
        return matrix, []
    targets = list_specs(product) if selection.full else sorted(selection.specs)
    durations = spec_durations(targets, impact)
    if product.fully_parallel:
        assert impact is not None
        tests = {
            spec: pr_tests(impact.specs[spec]) if spec in impact.specs else 0 for spec in targets
        }
        shards = plan_split_shards(durations, count, {s: n for s, n in tests.items() if n})
    else:
        shards = plan_shards(durations, count)
    matrix = [
        {
            "shard": index,
            "specs": " ".join(relative_to_frontend(product, spec) for spec in shard.specs),
            "playwright_shard": shard.playwright_shard,
        }
        for index, shard in enumerate(shards, start=1)
    ]
    return matrix, shards


def skip_smoke_specs(selection: Selection, specs: Iterable[str]) -> None:
    """PR の smoke が全 project で実行する spec（frontend からの相対パス）を選択から除く（#1525）。

    全件のときは除かない（全件を実行する PR では smoke の job を飛ばす）。
    """
    if selection.full:
        return
    product = selection.product
    for spec in specs:
        path = f"{product.frontend}/{spec}"
        if selection.specs.pop(path, None) is not None:
            selection.notes.append(f"{spec}: PR の smoke が実行する（impact では実行しない）")


# ---------------------------------------------------------------- nightly の記録の取得


def download_maps(product: Product, dest: Path, repo: str | None = None) -> list[Path]:
    """最近の nightly（main）の run から、製品の記録の artifact を取得する。

    shard の記録が `nightly_shards` 個そろった run だけを使う（#1599）。時間切れで cancel された
    shard は記録を残さないので、そろわない run の記録は spec の一部が欠け、欠けた spec の変更を
    「影響なし」と見誤る。そろわない run は飛ばして、1 つ前の run を見る。
    """
    repo_args = ["--repo", repo] if repo else []
    out = subprocess.run(
        [
            "gh", "run", "list", "--workflow", "e2e-nightly.yml", "--branch", "main",
            "--status", "completed", "--limit", "10", "--json", "databaseId", *repo_args,
        ],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if out.returncode != 0:
        print(f"[e2e-impact] nightly の run を取得できない: {out.stderr.strip()}", file=sys.stderr)
        return []
    for run in json.loads(out.stdout or "[]"):
        target = dest / str(run["databaseId"])
        result = subprocess.run(
            [
                "gh", "run", "download", str(run["databaseId"]), "--pattern",
                f"e2e-impact-{product.name}-*", "--dir", str(target), *repo_args,
            ],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )  # fmt: skip
        files = sorted(target.rglob("*.json")) if target.exists() else []
        if result.returncode == 0 and len(files) == product.nightly_shards:
            return files
        if files:
            print(
                f"[e2e-impact] nightly の run {run['databaseId']} の記録は {len(files)} / "
                f"{product.nightly_shards} shard しか無いので使わない",
                file=sys.stderr,
            )
    return []


def load_impact(paths: Iterable[Path], product: Product) -> ImpactMap | None:
    maps = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("product") == product.name:
            maps.append(data)
    merged = merge_maps(maps)
    return ImpactMap(merged) if merged else None


# ---------------------------------------------------------------- import の検査

DIRECT_TEST_IMPORT = re.compile(
    r"import\s*\{([^}]*)\}\s*from\s*[\"']@playwright/test[\"']", re.MULTILINE
)


def check_imports(products: Iterable[Product]) -> list[str]:
    errors = []
    for product in products:
        root = ROOT / product.test_root
        allowed = ROOT / product.frontend / product.test_module
        for path in sorted(root.rglob("*.ts")):
            if path == allowed:
                continue
            for match in DIRECT_TEST_IMPORT.finditer(path.read_text(encoding="utf-8")):
                names = [n.strip() for n in match.group(1).split(",")]
                values = [n for n in names if n and not n.startswith("type ")]
                if any(re.match(r"^test(\s+as\s+\w+)?$", n) for n in values):
                    rel = path.relative_to(ROOT)
                    module = os.path.relpath(allowed.with_suffix(""), path.parent)
                    module = module if module.startswith(".") else f"./{module}"
                    errors.append(
                        f"{rel}: `test` は `@playwright/test` ではなく `{module}` から import する"
                        "（nightly の影響分析の記録を通すため。#885）"
                    )
    return errors


# ---------------------------------------------------------------- CLI


def write_output(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def summary(
    selection: Selection,
    mode: str,
    count: int,
    shards: list[Shard],
    impact: ImpactMap | None,
) -> str:
    product = selection.product
    lines = [f"### e2e の影響分析: {product.name}", ""]
    if impact:
        lines.append(f"- 記録: nightly の commit `{impact.commit[:10]}`（{impact.shards} shard）")
    else:
        lines.append("- 記録: なし")
    if mode == "full":
        lines.append(f"- **全件**（{count} shard）: " + "、".join(selection.full))
    elif mode == "none":
        lines.append("- 影響を受ける spec はありません")
    else:
        lines.append(f"- {len(selection.specs)} spec（{count} shard）")
        for spec, reasons in sorted(selection.specs.items()):
            lines.append(f"  - `{relative_to_frontend(product, spec)}`: {' / '.join(reasons)}")
    if shards:

        def label(shard: Shard) -> str:
            if shard.part:
                name = relative_to_frontend(product, shard.specs[0]).rsplit("/", 1)[-1]
                return f"{name} のテスト単位の {shard.playwright_shard}"
            return f"{len(shard.specs)} spec"

        estimates = "、".join(
            f"{index} は {label(shard)}・約 {math.ceil(shard.estimated_ms / 1000)} 秒"
            for index, shard in enumerate(shards, start=1)
        )
        lines.append(f"- shard の見積もり（nightly の実測。LPT で詰める）: {estimates}")
    elif count:
        lines.append("- shard はテスト単位（Playwright の `--shard`。`fullyParallel`）")
    for note in list(dict.fromkeys(selection.notes))[:30]:
        lines.append(f"- 注: {note}")
    return "\n".join(lines) + "\n"


def command_select(args: argparse.Namespace) -> int:
    product = PRODUCTS[args.product]
    paths: list[Path] = []
    if args.maps:
        paths = sorted(Path(args.maps).rglob("*.json"))
    elif args.download:
        # 記録が無くても何も選ばない差分（製品の frontend・共有 UI に触れない PR）なら、取得しない
        # （ci.yml は製品の変更を待たずに 3 製品とも select する。#1525）。
        dry = select(product, None, args.base, args.head)
        if dry.full or dry.specs:
            paths = download_maps(product, Path(tempfile.mkdtemp(prefix="e2e-impact-")), args.repo)
    impact = load_impact(paths, product)
    selection = select(product, impact, args.base, args.head)
    skip_smoke_specs(selection, (args.skip_specs or "").split())
    mode = "full" if selection.full else ("selected" if selection.specs else "none")
    matrix, shards = shard_matrix(product, selection, impact)
    text = summary(selection, mode, len(matrix), shards, impact)
    print(text)
    if args.step_summary:
        with open(args.step_summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    write_output(
        args.github_output,
        {
            f"{product.name}_mode": mode,
            f"{product.name}_matrix": json.dumps(matrix, ensure_ascii=False),
            f"{product.name}_total": str(len(matrix)),
        },
    )
    return 0


def command_aggregate(args: argparse.Namespace) -> int:
    data = aggregate(Path(args.records), args.product, args.commit)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"[e2e-impact] {args.product}: {data['tests']} tests, {len(data['files'])} files")
    return 0


def command_check_imports(args: argparse.Namespace) -> int:
    products = [PRODUCTS[name] for name in (args.product or PRODUCTS)]
    errors = check_imports(products)
    for error in errors:
        print(f"::error::{error}" if os.environ.get("GITHUB_ACTIONS") else error)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_select = sub.add_parser("select", help="差分に影響を受ける spec を選ぶ")
    p_select.add_argument("--product", required=True, choices=sorted(PRODUCTS))
    p_select.add_argument("--base", required=True, help="比べる base（例: origin/main）")
    p_select.add_argument("--head", default="HEAD")
    p_select.add_argument("--maps", help="記録（aggregate の JSON）のディレクトリ")
    p_select.add_argument("--download", action="store_true", help="nightly の記録を gh で取得する")
    p_select.add_argument("--repo", help="gh の --repo（省略時は現在の repo）")
    p_select.add_argument(
        "--skip-specs",
        help="PR の smoke が全 project で実行する spec（frontend からの相対パス、空白区切り）。"
        "全件でなければ選択から除く",
    )
    p_select.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    p_select.add_argument("--step-summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    p_select.set_defaults(func=command_select)

    p_aggregate = sub.add_parser("aggregate", help="テストごとの記録を shard の集計にする")
    p_aggregate.add_argument("--product", required=True, choices=sorted(PRODUCTS))
    p_aggregate.add_argument("--records", required=True)
    p_aggregate.add_argument("--out", required=True)
    p_aggregate.add_argument("--commit", required=True)
    p_aggregate.set_defaults(func=command_aggregate)

    p_check = sub.add_parser("check-imports", help="spec が記録の fixture を通ることを確かめる")
    p_check.add_argument("--product", action="append", choices=sorted(PRODUCTS))
    p_check.set_defaults(func=command_check_imports)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
