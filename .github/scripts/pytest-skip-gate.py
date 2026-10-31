#!/usr/bin/env python3
"""実 Oracle の結合テストの skip の監視（#1595）。

実 Oracle のテストは DB に届かない・準備が足りないと skip する。CI でそのまま成功にすると、DB の準備が
壊れても「全部 skip で成功」になり、テストが壊れたまま気づかれない（#1595 で RAG の 13 件が壊れていた）。
pytest の JUnit XML（`--junitxml`）を読み、次のときに失敗する:
  - 実行したテストが 0 件。
  - 許可の一覧（--allow。test の `<classname>::<name>` に含まれる文字列）に無いテストが skip された。

使い方: pytest-skip-gate.py <junit.xml> [--allow <文字列>]...
標準ライブラリだけを使う（runner の python3 で動く）。
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET  # nosec B405 - 自分の CI の pytest が書いた XML だけを読む


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("junit_xml")
    parser.add_argument("--allow", action="append", default=[], help="skip を許すテスト（部分一致）")
    args = parser.parse_args(argv)

    root = ET.parse(args.junit_xml).getroot()  # nosec B314 - 同上
    cases = list(root.iter("testcase"))
    unexpected: list[str] = []
    allowed: list[str] = []
    for case in cases:
        skipped = case.find("skipped")
        if skipped is None:
            continue
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        reason = (skipped.get("message") or skipped.text or "").strip()
        line = f"{test_id}（{reason}）"
        if any(pattern in test_id for pattern in args.allow):
            allowed.append(line)
        else:
            unexpected.append(line)

    print(f"テスト {len(cases)} 件、許可した skip {len(allowed)} 件、許可していない skip {len(unexpected)} 件")
    for line in allowed:
        print(f"  許可した skip: {line}")
    if not cases:
        print("::error::実行したテストが 0 件です（対象のファイル・opt-in の環境変数を確かめる）。")
        return 1
    if unexpected:
        for line in unexpected:
            print(f"::error::許可していない skip: {line}")
        print("実 Oracle の準備（接続・schema・権限）か opt-in の環境変数が足りない可能性があります。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
