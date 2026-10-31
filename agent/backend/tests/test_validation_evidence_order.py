"""最終の検証に渡す根拠の並べ替え（#1451）の決定論テスト。

#1362 の deepseek-flash の全量の評価の Run の根拠の並び（新しい順の 31 件目以降に回答が使った
根拠がある）をスタブで再生し、回答が `chunk_id` を書かなくても、回答の本文に現れる ID・コード・
引用で当たった根拠が上限（30 件）の中に入ることを確かめる。
"""

from __future__ import annotations

from typing import Any

from app.features.agent.answer_validation import MAX_VALIDATION_EVIDENCE, run_evidence_groups
from app.features.agent.evidence_match import (
    EvidenceText,
    answer_features,
    matched_features,
    order_weights,
)
from app.features.agent.runtime import RunStep, StepStatus
from app.features.agent.tools import ToolCall, ToolResult

LEDGER = "システム台帳.xlsx"


def _step(name: str, arguments: dict[str, Any], output: dict[str, Any]) -> RunStep:
    return RunStep(
        run_id="run-1451",
        status=StepStatus.COMPLETED,
        tool_call=ToolCall(name=name, arguments=arguments),
        tool_result=ToolResult(name=name, success=True, output=output),
    )


def _evidence(
    document_id: str, chunk_id: str, excerpt: str, *, sheet: str | None = None
) -> dict[str, Any]:
    return {
        "evidence_id": chunk_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "excerpt": excerpt,
        "locator": {"section_path": [], "sheet_name": sheet},
    }


def _ledger_row(number: int) -> dict[str, Any]:
    names = {156: ("支払管理システム", "経理部"), 157: ("会計システム", "経理部")}
    name, department = names.get(number, (f"業務システム{number}", "情報システム部"))
    return _evidence(
        "doc-ledger",
        f"doc-ledger:set:{number}",
        f"システムID: SYS-{number} | システム名: {name} | 担当部署: {department}",
        sheet="システム台帳",
    )


def _search(evidence: list[dict[str, Any]], query: str) -> RunStep:
    return _step("rag__rag_search", {"query": query}, {"evidence": evidence})


def _chunk_ids(refs: list[dict[str, Any]]) -> list[str]:
    return [ref["chunk_id"] for ref in refs]


def test_ledger_row_named_by_its_code_enters_the_limit_without_chunk_id() -> None:
    """`br-gl-approver-deadline`（`run_70af52214f694b30baf0231d98bc446c`）の形: 1 段目の検索の
    台帳の行（SYS-157）が新しい順の 31 件目（35 件）にあり、回答は chunk_id を書かずに
    「会計システム（SYS-157）」と書いた。今までは上限の外に落ち、冒頭の結論が外れていた。"""
    first = [_ledger_row(number) for number in range(147, 162)]  # 15 件。SYS-157 は 11 件目。
    second = [
        _evidence("doc-org", f"doc-org:set:{index}", f"組織規程 第 {index} 条 部長の職務")
        for index in range(1, 11)
    ]
    third = [
        _evidence(
            "doc-approval",
            f"doc-approval:set:{index}",
            "変更申請は経理部の場合、管理本部長が受付から 5 営業日以内に承認する。"
            if index == 4
            else f"承認規程 第 {index} 条 申請の様式",
        )
        for index in range(1, 11)
    ]
    steps = [
        _search(first, "会計システム 担当部署"),
        _search(second, "経理部 承認者"),
        _search(third, "変更申請 承認 期限"),
    ]
    answer = (
        "**会計システム（SYS-157）の変更申請の承認者は「管理本部長」で、"
        "受付から 5 営業日以内に承認されます。**\n\n"
        "- 出典: システム台帳.xlsx シート「システム台帳」12 行目、承認規程 第 4 条"
    )

    [(_tool, newest)] = run_evidence_groups(steps)
    # 新しい順では 31 件目で、上限の外（再現の前提）。
    assert "doc-ledger:set:157" not in _chunk_ids(newest)

    [(tool_name, refs)] = run_evidence_groups(steps, answer)

    assert tool_name == "rag__rag_search"
    assert len(refs) == MAX_VALIDATION_EVIDENCE
    ids = _chunk_ids(refs)
    # 回答の ID で当たった台帳の行と、回答の引用（「管理本部長」）を含む承認規程が先。
    assert set(ids[:2]) == {"doc-ledger:set:157", "doc-approval:set:4"}
    # 回答が書かない別の行（SYS-156）は先にしない。
    assert ids.index("doc-ledger:set:156") > 2


def test_quoted_sentence_matches_full_width_evidence() -> None:
    """`br-wms-link-department`（`run_6c89cf2188e74e37b91964addd148af9`）の形: 運用要領 第 22 章の
    「ＯＭＳから受注データを受け取ります」が新しい順の 37 件目（47 件）。回答は半角で引用した。"""
    old = [
        _evidence(
            "doc-ops",
            f"doc-ops:set:{index}",
            "第 22 章 ＷＭＳ連携\nＯＭＳから受注データを受け取ります。担当は営業企画部。"
            if index == 3
            else f"運用要領 第 {index} 章 一般事項",
        )
        for index in range(1, 12)
    ]
    newer = [
        [
            _evidence(f"doc-{call}", f"doc-{call}:set:{index}", f"資料 {call} の {index} 番目")
            for index in range(1, 13)
        ]
        for call in (1, 2, 3)
    ]
    steps = [_search(old, "WMS 連携"), *(_search(items, "段") for items in newer)]
    answer = (
        "WMS の連携先の部署は営業企画部です。運用要領 第 22 章に"
        "「OMSから受注データを受け取ります」とあります。"
    )

    [(_tool, refs)] = run_evidence_groups(steps, answer)

    assert _chunk_ids(refs)[0] == "doc-ops:set:3"


def test_unmatched_chunks_of_a_whole_document_read_do_not_push_out_search_evidence() -> None:
    """`tl-security-severity-a`（`run_b29a1de3914045c3bdcaa33c532e95db`）の形: 検索の後に
    rag_read_document で台帳の全行（45 件）を読んだ。今までは読んだ chunk が上限を埋めて、組織規程の
    検索の根拠が検証に入らなかった。回答に当たった行だけを先にし、残りの行は検索の根拠の後ろ。"""
    search = [
        _evidence("doc-org", f"doc-org:set:{index}", f"組織規程 第 {index} 条 重大度の区分")
        for index in range(1, 21)
    ]
    text_parts: list[str] = []
    chunks: list[dict[str, Any]] = []
    position = 0
    for number in range(101, 146):
        row = f"SYS-{number} | 業務システム{number} | 重大度 {'A' if number == 128 else 'B'}"
        chunks.append(
            {
                "chunk_id": f"doc-ledger:set:{number}",
                "section_path": ["システム台帳"],
                "start": position,
                "end": position + len(row),
            }
        )
        text_parts.append(row)
        position += len(row) + 1
    read = {
        "document_id": "doc-ledger",
        "file_name": LEDGER,
        "text": "\n".join(text_parts),
        "chunks": chunks,
    }
    steps = [
        _search(search, "重大度 A の区分"),
        _step("rag__rag_read_document", {"document_id": "doc-ledger"}, read),
    ]
    # 回答はすべての行が持つ見出し（「システム台帳」）も書くが、それでは行を見分けない。
    answer = (
        "「システム台帳」で重大度 A のシステムは SYS-128 です。組織規程 第 3 条の区分によります。"
    )

    [(tool_name, refs)] = run_evidence_groups(steps, answer)

    assert tool_name == "rag__rag_read_document"
    ids = _chunk_ids(refs)
    assert len(ids) == MAX_VALIDATION_EVIDENCE
    assert ids[0] == "doc-ledger:set:128"
    # 検索の根拠 20 件はすべて入り、読んだ残りの行は後ろの 9 件だけ。
    assert {f"doc-org:set:{index}" for index in range(1, 21)} <= set(ids)
    assert all(chunk_id.startswith("doc-ledger:") for chunk_id in ids[21:])


def test_matched_features_ignore_number_only_headings_and_code_prefixes() -> None:
    features = answer_features("SYS-15 の担当は第 1 章と「経理部の承認」によります。")

    def matched(text: str, labels: list[object]) -> frozenset[str]:
        evidence = EvidenceText()
        evidence.add(text, labels)
        return matched_features(features, evidence)

    # SYS-157 は SYS-15 と別のコード。番号だけの見出し（第 1 章）は数えない。
    assert matched("SYS-157 の担当", ["第 1 章"]) == frozenset()
    assert matched("SYS-15 は経理部の承認が要る", []) == {"code:SYS-15", "quote:経理部の承認"}
    # 多くの根拠が共有する特徴は、回答に固有の特徴にしない。
    shared = frozenset({"label:システム台帳"})
    weights = order_weights([shared] * 6 + [frozenset({"code:SYS-15"}) | shared])
    assert [distinct for distinct, _weight in weights] == [False] * 6 + [True]


def _section_evidence(
    document_id: str, title: str, chapter: int, heading: str, body: str
) -> dict[str, Any]:
    item = _evidence(
        document_id, f"{document_id}:set:{chapter}", f"第 {chapter} 章 {heading}\n{body}"
    )
    item["locator"] = {"section_path": [title, f"第 {chapter} 章 {heading}", None]}
    item["file_name"] = f"{document_id}.pdf"
    return item


def test_section_reference_by_document_and_chapter_number_enters_the_limit() -> None:
    """`cmp-mes-vs-qms-deadline`（e1467、`run_cddfe2d1dc364df898ccf1d100a3d3e4`）の形: 回答は
    「承認者が不在のときの代理（組織規程 第 4 章）」と文書名と章の番号だけで参照し、見出しの名前・
    ID・引用を書かなかった。組織規程 第 4 章「承認者の代理」の chunk は根拠の候補 42 件の 35 番目
    で、30 件に入らず「第 4 章の内容が根拠に無い」で unsupported になっていた。"""
    org_title = "組織規程（システムの担当と承認者）"
    first = [
        _section_evidence(
            "doc-org", org_title, 3, "部署の承認者", "製造部の承認者は生産本部長です。"
        ),
        _section_evidence(
            "doc-org",
            org_title,
            4,
            "承認者の代理",
            "承認者が 3 営業日以上不在のときは、その部署が属する本部の本部長が代理で承認します。",
        ),
        # 別の会社の同じ名前の規程（題名の前に会社名が付く）の第 4 章は、回答の参照と別の文書。
        _section_evidence(
            "doc-org-logistics",
            "サンプル物流社 組織規程（システムの担当と承認者）",
            4,
            "承認者の代理",
            "課長が代理します。",
        ),
    ]
    newer = [
        [
            _evidence(f"doc-{call}", f"doc-{call}:set:{index}", f"資料 {call} の {index} 番目")
            for index in range(1, 14)
        ]
        for call in (1, 2, 3)
    ]
    steps = [_search(first, "承認者 代理"), *(_search(items, "段") for items in newer)]
    answer = (
        "製造実行システムの方が承認の期限は長いです（組織規程 第 3 章 部署の承認者）。\n\n"
        "## 確かめられなかった点\n"
        "承認者が不在のときの代理（組織規程 第 4 章）が適用される場合の期限は、"
        "通常の期限とは別の扱いになります。"
    )

    [(_tool, newest)] = run_evidence_groups(steps)
    # 新しい順では 41 件目で、上限の外（再現の前提）。
    assert "doc-org:set:4" not in _chunk_ids(newest)

    [(_tool, refs)] = run_evidence_groups(steps, answer)

    ids = _chunk_ids(refs)
    assert len(ids) == MAX_VALIDATION_EVIDENCE
    assert set(ids[:2]) == {"doc-org:set:3", "doc-org:set:4"}
    # 別の会社の規程の第 4 章は、回答の参照に当たらない（先にしない）。
    assert "doc-org-logistics:set:4" not in ids[:2]


def _section_matched(answer: str, labels: list[object], names: list[object] | None = None) -> bool:
    evidence = EvidenceText()
    evidence.add("本文", labels, names)
    return any(
        key.startswith("section:") for key in matched_features(answer_features(answer), evidence)
    )


def test_section_reference_needs_the_document_name_just_before_the_number() -> None:
    labels: list[object] = ["組織規程（システムの担当と承認者）", "第 4 章 承認者の代理"]
    # 括弧の補足を除いた名前・「の」をはさむ書き方・全角の数字・漢数字でも当たる。
    assert _section_matched("代理は組織規程 第 4 章によります。", labels)
    assert _section_matched("詳しくは「組織規程」の第４章を参照。", labels)
    assert _section_matched("組織規程（システムの担当と承認者）第四章", labels)
    # 「第 3 章・第 4 章」は前の参照の文書名を引き継ぐ。
    assert _section_matched("組織規程 第 3 章・第 4 章", labels)
    # ファイル名でも当たる。
    assert _section_matched("org-rules.pdf の第 4 章", labels, ["org-rules.pdf"])
    # 番号だけの参照・別の番号・別の文書の同じ番号・文書名から離れた番号は当てない。
    assert not _section_matched("詳しくは第 4 章を参照してください。", labels)
    assert not _section_matched("組織規程 第 14 章", labels)
    assert not _section_matched("承認規程 第 4 章", labels)
    assert not _section_matched("組織規程 第 2 章の部署と、承認規程の第 4 章", labels)
    assert not _section_matched("組織規程に書かれた代理の扱いは第 4 章", labels)
