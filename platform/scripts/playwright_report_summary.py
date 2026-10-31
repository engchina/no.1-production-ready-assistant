"""Playwright の json reporter の結果から、flaky と失敗のテストの一覧を Markdown にする（#1591）。

nightly（`.github/workflows/e2e-nightly.yml`）が step summary に出す。retries 2 で通ったテスト
（flaky）は job を赤にしないため、log の「N flaky」だけでは、どのテストが不安定か分からなかった。

使い方:
  python3 platform/scripts/playwright_report_summary.py <results.json> --title "rag (1/2)" \
    [--summary "$GITHUB_STEP_SUMMARY"]

json が無い（Playwright が始まる前に止まった等）ときは、その旨を出して終了 0 にする
（step を赤にしない）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TestOutcome:
    file: str
    line: int
    title: str
    project: str
    status: str  # expected / unexpected / flaky / skipped
    attempts: int


def iter_outcomes(report: dict) -> Iterator[TestOutcome]:
    """report の suite の木をたどる。最上位の suite は spec のファイル、その下は describe。"""

    def walk(suite: dict, describes: list[str]) -> Iterator[TestOutcome]:
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                yield TestOutcome(
                    file=spec.get("file") or suite.get("file", ""),
                    line=int(spec.get("line", 0)),
                    title=" › ".join([*describes, spec.get("title", "")]),
                    project=test.get("projectName", ""),
                    status=test.get("status", ""),
                    attempts=len(test.get("results", [])),
                )
        for child in suite.get("suites", []):
            yield from walk(child, [*describes, child.get("title", "")])

    for suite in report.get("suites", []):
        yield from walk(suite, [])


def render(report: dict, title: str) -> str:
    outcomes = list(iter_outcomes(report))
    flaky = [o for o in outcomes if o.status == "flaky"]
    failed = [o for o in outcomes if o.status == "unexpected"]
    stats = report.get("stats", {})
    lines = [
        f"### Playwright: {title}",
        "",
        f"- 成功 {stats.get('expected', 0)}・flaky {stats.get('flaky', len(flaky))}・"
        f"失敗 {stats.get('unexpected', len(failed))}・skip {stats.get('skipped', 0)}",
    ]
    for label, items in (("flaky（retry で通った）", flaky), ("失敗", failed)):
        if not items:
            continue
        lines += [
            "",
            f"#### {label}",
            "",
            "| spec | テスト | project | 試行 |",
            "|---|---|---|---|",
        ]
        for item in sorted(items, key=lambda o: (o.file, o.line, o.project)):
            name = item.title.replace("|", "\\|")
            lines.append(
                f"| `{item.file}:{item.line}` | {name} | {item.project} | {item.attempts} |"
            )
    if not flaky and not failed:
        lines += ["", "flaky・失敗のテストはありません。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path)
    parser.add_argument("--title", default="E2E")
    parser.add_argument(
        "--summary", default="", help="追記する Markdown のファイル（GITHUB_STEP_SUMMARY）"
    )
    args = parser.parse_args(argv)
    if args.report.is_file():
        text = render(json.loads(args.report.read_text(encoding="utf-8")), args.title)
    else:
        text = f"### Playwright: {args.title}\n\n結果（{args.report}）がありません。\n"
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
