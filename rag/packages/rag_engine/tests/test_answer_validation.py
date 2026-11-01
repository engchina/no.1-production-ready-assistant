"""回答の最終の検証（#1246）。モデルはスタブ。標準回答を使わず主張だけを監査する。"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rag_engine.evaluation.answer_validation import (
    QUESTION_ID,
    VALIDATION_RUBRIC_VERSION,
    VALIDATION_SYSTEM_PROMPT,
    ClaimAuditOutput,
    validate_answer_claims,
)

EVIDENCE = [{"id": "c1", "source": "manual.pdf", "page_start": 1, "page_end": 1, "text": "検証用アカウントの有効期限は最長 30 日です。"}]
ANSWER = "検証用アカウントの有効期限は最長 30 日です。\n\n延長は 90 日までできます。"


def _documents(payload: dict) -> list[dict]:
    """モデルに渡した根拠のうち、質問の文（先頭に足す資料ではない根拠）を除いたもの。"""
    return [item for item in payload["evidence_items"] if item.get("id") != QUESTION_ID]


def _is_reference_reaudit(payload: dict) -> bool:
    """監査の応答の参照の誤りの監査し直し（#112）の呼び出しか。"""
    from rag_engine.evaluation.answer_validation import REFERENCE_REAUDIT_FEEDBACK

    return str(payload.get("validation_feedback") or "").startswith(REFERENCE_REAUDIT_FEEDBACK)


def _passages(inputs: str) -> list[str]:
    import json

    payload = json.loads(inputs)
    return [item["id"] for item in payload["answer_passages"]], _documents(payload)[0]["evidence_id"]


# 最終検証ではモデルの supported と実際の引用による支持を区別する。
# 無出典の資料の主張は unassessed とし、未引用の資料による結び付けだけでは昇格させない。
def test_claims_are_bound_to_passages_and_evidence() -> None:
    def parse(system, inputs, settings, schema, provider_id=None):
        ids, evidence_id = _passages(inputs)
        assert schema is ClaimAuditOutput
        assert system == VALIDATION_SYSTEM_PROMPT and system.startswith("## Step 1:")
        assert "standard_answer" not in inputs  # 標準回答は使わない
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": ids[0],
                        "status": "supported",
                        "evidence_id": evidence_id,
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "根拠に記載",
                    },
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": ids[1],
                        "status": "unsupported",
                        "evidence_id": "",
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "延長の記載が無い",
                    },
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("有効期限は？", ANSWER, EVIDENCE, settings=None)
    assert result["status"] == "completed"
    assert result["counts"] == {"unassessed": 1, "unsupported": 1}
    supported = next(c for c in result["claim_checks"] if c["audit_status"] == "supported")
    assert supported["source_id"] == "c1"
    assert supported["answer_quote"] == "検証用アカウントの有効期限は最長 30 日です。"
    assert result["evidence_ids"] == ["c1"] and result["evidence_truncated"] is False


def test_claims_violating_the_contract_on_the_last_attempt_are_dropped_not_failed() -> None:
    """最後の試行でも契約に違反した判定（無い段落の ID・回答外の引用）だけを外し、ツール全体を失敗させない (#121)。"""
    calls = []

    def parse(system, inputs, settings, schema, provider_id=None):
        ids, _ = _passages(inputs)
        calls.append(inputs)
        claim = {"status": "unsupported", "evidence_id": "", "source_id": "", "evidence_quote": "", "reason": "記載が無い"}
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {**claim, "answer_quote": "段落", "answer_passage_id": ids[1]},
                    {**claim, "answer_quote": "段落", "answer_passage_id": "P999"},
                    {**claim, "answer_quote": "回答に無い文", "answer_passage_id": ""},
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("有効期限は？", ANSWER, EVIDENCE, settings=None)
    # 最初の試行は違反を指摘して再試行し、最後の試行で違反した判定だけを外す。
    assert len(calls) == 2 and "validation_feedback" in calls[1]
    assert result["status"] == "completed"
    assert result["counts"] == {"unsupported": 1, "unassessed": 1}


def test_missing_passages_are_unassessed_and_empty_answers_are_not_audited() -> None:
    def parse(system, inputs, settings, schema, provider_id=None):
        ids, _ = _passages(inputs)
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": ids[0],
                        "status": "data_confirmation",
                        "evidence_id": "",
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "実データの確認",
                    },
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("q", ANSWER, EVIDENCE, settings=None)
    assert result["counts"] == {"data_confirmation": 1, "unassessed": 1}
    with patch("rag_engine.evaluation.answer_eval.parse_text_response") as never:
        assert validate_answer_claims("q", "", EVIDENCE, settings=None)["status"] == "no_claims"
        never.assert_not_called()


def test_headings_citations_and_questions_are_not_audited() -> None:
    """見出し・出典の行・利用者への質問は監査に渡さず、claim_checks にも出さない (#1306)。

    以前はモデルが not_a_claim にしても、is_heading に当たらない出典・質問が unassessed になり、
    Agent の最終の検証が「資料で確認できず除いた内容」に移していた。
    """
    answer = "\n".join(
        [
            "## 試用アカウントの有効期限",
            "検証用アカウントの有効期限は最長 30 日です。",
            "【portal-operations-manual.pdf, section 2. アカウント管理, page 1】",
            "出典: manual.pdf p.1",
            "所属部署の部門長の承認は得ていますか？（はい／いいえ）",
            "延長は 90 日までできます。",
        ]
    )
    seen: list[list[str]] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        import json

        payload = json.loads(inputs)
        seen.append([item["text"] for item in payload["answer_passages"]])
        evidence_id = _documents(payload)[0]["evidence_id"]
        ids = [item["id"] for item in payload["answer_passages"]]
        assert "reason も日本語で書く" in system
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": ids[0],
                        "status": "supported",
                        "evidence_id": evidence_id,
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "根拠に記載",
                    },
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": ids[1],
                        "status": "unsupported",
                        "evidence_id": "",
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "延長の記載が無い",
                    },
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("有効期限は？", answer, EVIDENCE, settings=None)
    assert seen == [["検証用アカウントの有効期限は最長 30 日です。", "延長は 90 日までできます。"]]
    assert result["counts"] == {"supported": 1, "unsupported": 1}
    # 主張が無い回答（見出し・出典・質問だけ）はモデルを呼ばない。
    with patch("rag_engine.evaluation.answer_eval.parse_text_response") as never:
        only = "**確認事項**\n付与先の部署名を教えてください。\n【manual.pdf p.3】"
        assert validate_answer_claims("q", only, EVIDENCE, settings=None)["status"] == "no_claims"
        never.assert_not_called()


def test_non_claim_passage_rules_keep_claims() -> None:
    from rag_engine.generation.operation_audit import is_non_claim_passage

    assert is_non_claim_passage("（参照: security-policy.docx p.4）")
    assert is_non_claim_passage("[運用マニュアル](https://example.com/manual.pdf)")
    assert is_non_claim_passage("削除するのはどちらのアカウントですか？")
    # 主張・操作を含む文は監査する。
    assert not is_non_claim_passage("根拠: 契約書の第 5 条により、30 日前までに申し出る必要があります")
    assert not is_non_claim_passage("削除できますが、よろしいですか？")
    assert not is_non_claim_passage("【パスワードは 90 日ごとに変更し、変更の履歴は管理画面に残ります】")
    assert not is_non_claim_passage("管理画面で「削除」を押してください。")


EVIDENCE_REF = "【証拠 ID: 03ac895f1d744a2b9eab4dd569eb68fc:b245283c90ea80e5d8aadd3283e6027f148113c0aa42c59bbf12923cc59ae01c:1】"


def test_location_lines_evidence_ids_and_table_structure_are_not_audited() -> None:
    """出典の位置のラベル・根拠の ID・表の区切りと見出しは監査に渡さない (#1317)。

    #1305 の実環境の評価で、これらがモデルに unassessed と返され、Agent の最終の検証が外して
    回答の対応を conditional / insufficient_evidence に誤らせていた（da-trial-account-expiry / -setup）。
    表の本文の行は主張のまま監査する。
    """
    answer = "\n".join(
        [
            "検証用アカウントの有効期限は **最長 30日** です。",
            "- セクション: 「サンプル業務ポータル 運用手順書 第3版」 → **2. 検証用アカウントの登録**",
            "- ページ: 1",
            "| 手順 | 操作内容 | 根拠 |",
            "|------|----------|------|",
            f"| 1. 前提確認 | 本操作は「ポータル管理者」ロールを持つ利用者のみが実施可能です。 | 「1. 前提」 （ページ1）{EVIDENCE_REF} |",
            "---",
        ]
    )
    seen: list[list[str]] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        import json

        payload = json.loads(inputs)
        seen.append([item["text"] for item in payload["answer_passages"]])
        evidence_id = _documents(payload)[0]["evidence_id"]
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": item["id"],
                        "status": "supported",
                        "evidence_id": evidence_id,
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "根拠に記載",
                    }
                    for item in payload["answer_passages"]
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("有効期限は？", answer, EVIDENCE, settings=None)
    assert seen == [
        [
            "検証用アカウントの有効期限は **最長 30日** です。",
            "| 1. 前提確認 | 本操作は「ポータル管理者」ロールを持つ利用者のみが実施可能です。",
        ]
    ]
    assert result["counts"] == {"unassessed": 2}


def test_new_non_claim_rules_keep_claims() -> None:
    from rag_engine.generation.operation_audit import is_non_claim_passage, table_header_lines

    assert is_non_claim_passage("- **根拠**：regional-report-guide-v2.pdf、2. 出力の手順（ページ1）")
    assert is_non_claim_passage("（*portal-operations-manual.pdf*、セクション「3. アクセス権限の付与」）【証拠1】")
    assert is_non_claim_passage("- 「サンプル業務ポータル 運用手順書 第3版」 6. アカウントの削除")
    assert is_non_claim_passage("| :--- | ---: |")
    assert is_non_claim_passage("**")
    assert is_non_claim_passage("権限の付与先は、個別の利用者ですか、グループですか？【clarification: target】")
    # 表の見出しは区切りの行の直前の行だけ。
    assert table_header_lines("| 手順 | 内容 |\n| 1 | 開く |") == set()
    assert table_header_lines("| 手順 | 内容 |\n|---|---|\n| 1 | 開く |") == {"| 手順 | 内容 |"}
    # 主張・操作を含む段落は監査する。
    assert not is_non_claim_passage("| 締め日 | 毎月 10 日 |")
    assert not is_non_claim_passage("- ページ: 管理画面で「削除」を押してください")
    assert not is_non_claim_passage("- 抜粋: 「…有効期限は最長 30 日です。」")
    assert not is_non_claim_passage("- 「有効期限は最長 30 日です。」")
    assert not is_non_claim_passage("- 「検証用」")
    assert not is_non_claim_passage(f"| 2. 登録 | 1. 「利用者」を開く 2. 「追加」を押す | {EVIDENCE_REF} |")


# 多段の質問の評価の Run の形（#1364。cmp-hr-vs-attendance-retention）: 台帳の行（機密区分）と規程（保管期間）。
LEDGER = {
    "id": "doc-ledger:set:3",
    "source": "system-ledger.xlsx",
    "page_start": None,
    "page_end": None,
    "text": "システムID: SYS-103 / 正式名: 人事評価システム / 機密区分: 極秘",
}
RETENTION = {
    "id": "doc-retention:set:1",
    "source": "data-retention.pdf",
    "page_start": 1,
    "page_end": 1,
    "text": "極秘のデータは 10 年間保管します。社外秘のデータは 7 年間保管します。",
}
BRIDGE_ANSWER = "人事評価システムは「極秘」扱いなので、データは **10 年間** 保管されます。"


def _bridge_validation(evidence_id: str, *, evidence_quote: str = "") -> dict:
    def parse(system, inputs, settings, schema, provider_id=None):
        import json

        payload = json.loads(inputs)
        ids = {item["id"]: item["evidence_id"] for item in _documents(payload)}
        assert "「,」で区切って evidence_id に書く" in system
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": payload["answer_passages"][0]["id"],
                        "status": "supported",
                        "evidence_id": evidence_id.format(ledger=ids[LEDGER["id"]], retention=ids[RETENTION["id"]]),
                        "source_id": "",
                        "evidence_quote": evidence_quote,
                        "reason": "台帳で極秘、規程で極秘は 10 年と確かめられる。",
                    },
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        return validate_answer_claims("人事評価システムのデータの保管期間は？", BRIDGE_ANSWER, [LEDGER, RETENTION], settings=None)


def test_claim_supported_by_two_evidence_ids_is_not_a_citation_error() -> None:
    """複数根拠の ID は結び付けるが、実際の出典を持たない結論は支持済みにしない。"""
    for written in (
        "{ledger},{retention}",
        "{ledger}, {retention}",
        "{ledger}、{retention}",
        "[{ledger} / {retention}]",
        # chunk の id（MCP の evidence_id）を並べたもの・括弧で囲んだ 1 つの ID も同じ。
        "doc-ledger:set:3, doc-retention:set:1",
        "【{ledger}】",
    ):
        result = _bridge_validation(written)
        [claim] = result["claim_checks"]
        assert claim["status"] == "unassessed", written
        assert claim["source_id"] in {LEDGER["id"], RETENTION["id"]}
        assert result["counts"] == {"unassessed": 1}
    # 結び付けた片段の ID をすべて残す（最初の根拠を出典にする）。
    [claim] = _bridge_validation("{ledger}, {retention}")["claim_checks"]
    assert claim["source_id"] == LEDGER["id"] and claim["evidence_quote"] == LEDGER["text"]
    assert len(claim["evidence_id"].split(",")) == 2


def test_unknown_or_unquoted_ids_in_a_list_stay_citation_errors() -> None:
    """並べた ID の 1 つでも根拠に無い・引用がどの根拠にも無いときは、今までどおり引用エラー (#1364)。"""
    for written, quote in (("{ledger}, E0000000000000000000", ""), ("{ledger} {retention}", "根拠に無い文"), ("E0000000000000000000", "")):
        [claim] = _bridge_validation(written, evidence_quote=quote)["claim_checks"]
        assert claim["status"] == "citation_error", written
        assert claim["reason"].startswith("未登録の原文ID。")


# 出典の行の事例（RAG と Agent の両方のテストが読む。#1370）。
CITATION_CASES = json.loads(
    (Path(__file__).resolve().parents[4] / "platform/contracts/answer-passages/citation-lines.json").read_text("utf-8")
)


# 作業の経過の文の事例（業務ガイドが見つからなかった。RAG と Agent の両方のテストが読む。#1452）。
PROCESS_CASES = json.loads(
    (Path(__file__).resolve().parents[4] / "platform/contracts/answer-passages/process-notes.json").read_text("utf-8")
)


@pytest.mark.parametrize("case", PROCESS_CASES["process_passages"], ids=lambda case: case["id"])
def test_guide_not_found_passages_are_not_claims(case: dict) -> None:
    """業務ガイドが見つからなかったことだけを述べる文は、主張として監査しない (#1452)。"""
    from rag_engine.generation.operation_audit import is_guide_not_found, is_non_claim_passage

    assert is_guide_not_found(case["passage"])
    assert is_non_claim_passage(case["passage"])


@pytest.mark.parametrize("case", PROCESS_CASES["claim_passages"], ids=lambda case: case["id"])
def test_guide_passages_with_content_stay_claims(case: dict) -> None:
    """業務ガイドの内容・条件・数量・操作を含む文と、検索の状態の文は主張のまま監査する (#1452)。"""
    from rag_engine.generation.operation_audit import is_guide_not_found, is_non_claim_passage

    assert not is_guide_not_found(case["passage"])
    assert not is_non_claim_passage(case["passage"])


@pytest.mark.parametrize("case", CITATION_CASES["citation_passages"], ids=lambda case: case["id"])
def test_citation_only_passages_are_not_claims(case: dict) -> None:
    """定位子・根拠の ID・文書名と場所だけの行は出典の行で、主張として監査しない (#1370)。"""
    from rag_engine.generation.operation_audit import is_citation_line, is_non_claim_passage

    assert is_citation_line(case["passage"])
    assert is_non_claim_passage(case["passage"])


@pytest.mark.parametrize("case", CITATION_CASES["claim_passages"], ids=lambda case: case["id"])
def test_passages_with_body_after_the_location_stay_claims(case: dict) -> None:
    """場所・ラベルの後に本文が続く行は主張のまま監査する（取りこぼさない。#1370）。"""
    from rag_engine.generation.operation_audit import is_non_claim_passage

    assert not is_non_claim_passage(case["passage"])


@pytest.mark.parametrize("case", CITATION_CASES["answers"], ids=lambda case: case["id"])
def test_citation_lines_of_the_evaluation_answers_are_not_audited(case: dict) -> None:
    """#1335 の再評価の回答の出典の行を監査に渡さず、unassessed にしない (#1370)。"""
    seen: list[list[str]] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        seen.append([item["text"] for item in payload["answer_passages"]])
        evidence_id = _documents(payload)[0]["evidence_id"]
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": item["id"],
                        "status": "supported",
                        "evidence_id": evidence_id,
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "根拠に記載",
                    }
                    for item in payload["answer_passages"]
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("質問", case["answer"], EVIDENCE, settings=None)
    [audited] = seen
    assert not set(case["citation_passages"]) & set(audited)
    assert set(case["claim_passages"]) <= set(audited)
    assert set(result["counts"]) == {"unassessed"}


def test_citation_line_marked_not_a_claim_is_not_unassessed() -> None:
    """品質評価の主張の監査でも、モデルが not_a_claim にした出典の行は unassessed にしない (#1370)。"""
    from rag_engine.evaluation.answer_eval import _bind_claims

    passages = [{"id": "A1", "text": "*Locator*: `doc:1b5b/page:3/el:12`"}, {"id": "A2", "text": "承認者は運行管理課長です。"}]
    output = ClaimAuditOutput.model_validate(
        {
            "claim_checks": [
                {
                    "answer_quote": item["text"],
                    "answer_passage_id": item["id"],
                    "status": "not_a_claim",
                    "evidence_id": "",
                    "source_id": "",
                    "evidence_quote": "",
                    "reason": "主張ではない",
                }
                for item in passages
            ]
        }
    )
    bound = _bind_claims(output, {}, passages)
    assert [claim.status for claim in bound.claim_checks] == ["not_a_claim", "unassessed"]


def test_location_line_and_closing_marked_not_a_claim_are_not_unassessed() -> None:
    """場所のラベルの行と締めくくりの定型の文は、モデルが not_a_claim にしても unassessed にしない (#1833)。

    #1833 の実機の回答では「- 場所：第 2 章「締め日と支払日」」と「以上、資料に基づく回答です。」が unassessed になり、
    Agent の回答から外されていた。値が文書の場所でない「場所：」の行と、事実を述べる文は今までどおり unassessed にする。
    """
    from rag_engine.evaluation.answer_eval import _bind_claims

    texts = [
        "- 場所：第 2 章「締め日と支払日」",
        "以上、資料に基づく回答です。",
        "- 場所：本社 3 階の会議室",
        "以上、締め日は毎月 25 日です。",
    ]
    passages = [{"id": f"A{index + 1}", "text": text} for index, text in enumerate(texts)]
    output = ClaimAuditOutput.model_validate(
        {
            "claim_checks": [
                {
                    "answer_quote": item["text"],
                    "answer_passage_id": item["id"],
                    "status": "not_a_claim",
                    "evidence_id": "",
                    "source_id": "",
                    "evidence_quote": "",
                    "reason": "主張ではない",
                }
                for item in passages
            ]
        }
    )
    bound = _bind_claims(output, {}, passages)
    assert [claim.status for claim in bound.claim_checks] == ["not_a_claim", "not_a_claim", "unassessed", "unassessed"]


# 見出しの名詞句・導入の文の事例（RAG と Agent の両方のテストが読む。#1469）。
HEADING_CASES = json.loads((Path(__file__).resolve().parents[4] / "platform/contracts/answer-passages/headings.json").read_text("utf-8"))


@pytest.mark.parametrize("case", HEADING_CASES["heading_passages"], ids=lambda case: case["id"])
def test_heading_phrases_and_lead_ins_are_not_claims(case: dict) -> None:
    """「補足（…）」・「根拠」・「…は次のとおりです。」は見出しで、主張として監査しない (#1469)。"""
    from rag_engine.generation.operation_audit import is_heading, is_non_claim_passage

    assert is_heading(case["passage"])
    assert is_non_claim_passage(case["passage"])


@pytest.mark.parametrize("case", HEADING_CASES["numbered_heading_passages"], ids=lambda case: case["id"])
def test_numbered_headings_are_decided_by_the_following_line(case: dict) -> None:
    """番号つきの見出しの行は、次の行が下の内容のときだけ見出し (#1481 / #1482)。

    終わりの語が一覧にある行（「1. 人事評価システムの重要度」）も、一覧に無い「…の…」の行も、単独の行と、番号の
    行が続く一覧の中の行は主張のまま監査する。
    """
    from rag_engine.generation.operation_audit import is_heading, is_non_claim_passage, list_heading_lines

    passage = case["passage"]
    for below in ("   - 資料の記載", "- 資料の記載", "  資料の記載", "→ 資料の記載"):
        lines = list_heading_lines(f"{passage}\n\n{below}")
        assert lines == {passage}
        assert is_non_claim_passage(passage, heading_lines=lines)
    assert not is_heading(passage)
    assert list_heading_lines(passage) == set()
    assert passage not in list_heading_lines(f"{passage}\n9. 次の項目\n   - 資料の記載")
    assert list_heading_lines(f"{passage}\n**次の節**") == set()


@pytest.mark.parametrize("case", HEADING_CASES["bullet_heading_passages"], ids=lambda case: case["id"])
def test_bullet_headings_are_decided_by_the_following_line(case: dict) -> None:
    """箇条書きの見出しの行は、次の行が下位の内容（深い字下げ・「→」）のときだけ見出し (#1486)。

    単独の行と、同じ字下げの箇条書きの行が続く一覧の項目（「ご確認いただきたい点」の「- 組織規程（ご担当部署の
    承認者）」）は、終わりの語が一覧にあっても主張のまま監査する。
    """
    from rag_engine.generation.operation_audit import is_heading, is_non_claim_passage, list_heading_lines

    passage = case["passage"]
    for below in ("   - 資料の記載", "  資料の記載", "→ 資料の記載"):
        lines = list_heading_lines(f"{passage}\n\n{below}")
        assert lines == {passage}
        assert is_non_claim_passage(passage, heading_lines=lines)
    assert not is_heading(passage)
    assert not is_non_claim_passage(passage)
    assert list_heading_lines(passage) == set()
    for sibling in ("- 次の項目", "・次の項目", "1. 次の項目", "**次の節**"):
        lines = list_heading_lines(f"{passage}\n{sibling}\n   - 資料の記載")
        assert passage not in lines
        assert not is_non_claim_passage(passage, heading_lines=lines)


@pytest.mark.parametrize("case", HEADING_CASES["claim_passages"], ids=lambda case: case["id"])
def test_short_statements_stay_claims(case: dict) -> None:
    """数・操作・言い切りを含む短い行と、値の名前だけの行は見出しにせず主張のまま監査する (#1469)。

    次の行が下の内容でも見出しにしない (#1481)。
    """
    from rag_engine.generation.operation_audit import is_heading, is_non_claim_passage, list_heading_lines

    assert not is_heading(case["passage"])
    assert not is_non_claim_passage(case["passage"])
    assert list_heading_lines(f"{case['passage']}\n   - 資料の記載") == set()


@pytest.mark.parametrize("case", HEADING_CASES["answers"], ids=lambda case: case["id"])
def test_headings_of_the_evaluation_answers_are_not_audited(case: dict) -> None:
    """#1362 の評価（e1467）の回答の見出しを監査に渡さず、unassessed にしない (#1469)。"""
    seen: list[list[str]] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        seen.append([item["text"] for item in payload["answer_passages"]])
        evidence_id = _documents(payload)[0]["evidence_id"]
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": item["id"],
                        "status": "supported",
                        "evidence_id": evidence_id,
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "根拠に記載",
                    }
                    for item in payload["answer_passages"]
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("質問", case["answer"], EVIDENCE, settings=None)
    [audited] = seen
    assert not set(case["heading_passages"]) & set(audited)
    assert set(case["claim_passages"]) <= set(audited)
    assert set(result["counts"]) == {"unassessed"}


def test_numbered_heading_with_document_location_note_is_not_audited() -> None:
    """番号つきの「…の…」の見出しに資料と章の注記が付いた行も監査しない (#1481)。

    e1481 の run_b568a0eb の段落。Agent はこの行を出典の行（citation）として本文から外すため、見出しの契約
    （headings.json）には入れず RAG の側だけで確かめる。
    """
    from rag_engine.generation.operation_audit import is_non_claim_passage, list_heading_lines

    passage = "2. 復旧の目標（障害連絡規程 incident-contact-rules.pdf 第 2 章「復旧の目標」）"
    lines = list_heading_lines(f"{passage}\n   - 重要度 A のシステムは 4 時間以内に復旧")
    assert lines == {passage}
    assert is_non_claim_passage(passage, heading_lines=lines)


@pytest.mark.parametrize(
    "passage",
    [
        "- システム台帳（system-ledger.xlsx・シート「システム台帳」）",
        "- **システム台帳**（system-ledger.xlsx、シート「システム台帳」）",
    ],
    ids=["sheet-note", "bold-document"],
)
def test_bullet_heading_with_document_file_note_follows_the_answer_lines(passage: str) -> None:
    """資料のファイル名の注記が付いた箇条書きの行も、下に字下げした行が続くときだけ監査しない (#1486)。

    e1467 の run_93a965a4・run_eff5f621 の「根拠」の行。Agent はこの行を出典の行（citation）として本文から外すため、
    見出しの契約（headings.json）には入れず両方のテストで個別に確かめる。同じ字下げの箇条書きの行が続く一覧の項目
    なら主張として監査する。
    """
    from rag_engine.generation.operation_audit import is_heading, is_non_claim_passage, list_heading_lines

    lines = list_heading_lines(f"**根拠**\n{passage}\n  - SYS-105 正式名: ドキュメントポータル / 重要度: B")
    assert lines == {passage}
    assert is_non_claim_passage(passage, heading_lines=lines)
    assert not is_heading(passage)
    lines = list_heading_lines(f"**根拠**\n{passage}\n- 組織規程 第 2 章 部署と略号（1 頁）")
    assert not is_non_claim_passage(passage, heading_lines=lines)


def test_heading_phrase_marked_not_a_claim_is_not_unassessed() -> None:
    """品質評価の主張の監査でも、モデルが not_a_claim にした見出しの名詞句・導入の文は unassessed にしない (#1469)。

    番号つきの見出しは回答の次の行で決まる（``test_numbered_heading_marked_not_a_claim_follows_the_answer_lines``。#1482）。
    """
    from rag_engine.evaluation.answer_eval import _bind_claims

    passages = [
        {"id": "A1", "text": "根拠（段ごとに確認した内容）"},
        {"id": "A2", "text": "根拠の積み上げは次のとおりです。"},
        {"id": "A3", "text": "1. **ID 管理システムの重要度は A**"},
    ]
    output = ClaimAuditOutput.model_validate(
        {
            "claim_checks": [
                {
                    "answer_quote": item["text"],
                    "answer_passage_id": item["id"],
                    "status": "not_a_claim",
                    "evidence_id": "",
                    "source_id": "",
                    "evidence_quote": "",
                    "reason": "主張ではない",
                }
                for item in passages
            ]
        }
    )
    bound = _bind_claims(output, {}, passages)
    assert [claim.status for claim in bound.claim_checks] == ["not_a_claim", "not_a_claim", "unassessed"]


def test_numbered_heading_marked_not_a_claim_follows_the_answer_lines() -> None:
    """品質評価の主張の監査で、次の行で決まる番号つきの見出しは、回答の中で見出しのときだけ not_a_claim のまま (#1481)。"""
    from rag_engine.evaluation.answer_eval import _bind_claims
    from rag_engine.generation.operation_audit import list_heading_lines

    passages = [{"id": "A1", "text": "1. 連携元のシステム"}]
    output = ClaimAuditOutput.model_validate(
        {
            "claim_checks": [
                {
                    "answer_quote": "1. 連携元のシステム",
                    "answer_passage_id": "A1",
                    "status": "not_a_claim",
                    "evidence_id": "",
                    "source_id": "",
                    "evidence_quote": "",
                    "reason": "主張ではない",
                }
            ]
        }
    )
    lines = list_heading_lines("1. 連携元のシステム\n   - 生産計画システムから生産計画を受け取ります。")
    assert [c.status for c in _bind_claims(output, {}, passages, heading_lines=lines).claim_checks] == ["not_a_claim"]
    assert [c.status for c in _bind_claims(output, {}, passages).claim_checks] == ["unassessed"]


def test_numbered_list_items_ending_with_heading_words_are_audited() -> None:
    """番号の行が続く一覧の項目は、終わりの語が見出しの語の一覧にあっても主張として監査する (#1482)。

    e1467 の run_ee5468f8 の「申請書に書く 4 項目」。#1469 の段落だけの判定では「1. 変更の内容と理由」「3. 切り戻しの
    手順」が見出しになり、監査に渡らなかった。同じ行でも、下に内容の行があれば見出し。
    """
    from rag_engine.generation.operation_audit import is_heading, is_non_claim_passage, list_heading_lines

    items = ["1. 変更の内容と理由", "2. 影響を受けるシステムと利用者", "3. 切り戻しの手順", "4. 作業の日時"]
    answer = "**申請書に書く 4 項目**\n" + "\n".join(items) + "\n\n**条件によって加わる項目**\n- 経費 Portal の変更"
    lines = list_heading_lines(answer)
    assert lines == set()
    for item in items:
        assert not is_heading(item)
        assert not is_non_claim_passage(item, heading_lines=lines)
    below = list_heading_lines("3. 切り戻しの手順\n   1. 旧版に戻す\n   2. 動作を確かめる")
    assert below == {"3. 切り戻しの手順"}
    assert is_non_claim_passage("3. 切り戻しの手順", heading_lines=below)


# #1362 の評価の Run の形（#1391）。業務支援の da-trial-account-setup: 運用手順書の章ごとの chunk と、手順の回答。
PORTAL = "d71d90b414a74ad99ccfc7a9dc4b3cb5:2e474d0310926b739eeed94bfc31012518db916cc382524721434a8baa1d629d"
PORTAL_EVIDENCE = [
    {
        "id": f"{PORTAL}:2",
        "source": "portal-operations-manual.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": "2. 検証用アカウントの登録\n1.管理画面の「利用者」を開き、「追加」を押します。\n2.利用者種別で「検証用」を選びます。",
    },
    {
        "id": f"{PORTAL}:3",
        "source": "portal-operations-manual.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": "3. アクセス権限の付与\n個別の利用者に付与する場合: 利用者の詳細画面の「権限」タブで、"
        "付与する権限を選んで「付与」を押します。",
    },
    {
        "id": f"{PORTAL}:4",
        "source": "portal-operations-manual.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": "4. 通知の設定\n1.権限を付与した後、利用者の詳細画面の「通知」タブを開きます。",
    },
]
PORTAL_ANSWER = "\n".join(
    [
        "- 管理画面の「利用者」メニューを開き「追加」ボタンを押す。",
        "- 登録した検証用利用者の詳細画面の **「権限」タブ** を開く。",
        "- 権限付与が完了したら、同じ利用者の詳細画面の **「通知」タブ** を開く。",
        "以上の順序で実施すれば、検証用アカウントの登録から権限付与、さらに通知設定まで完了します。",
    ]
)


def _portal_validation(written: list[str]) -> dict:
    """段落ごとに、根拠の片段の ID（format の {0}〜{2}）から作った evidence_id を返すモデルのスタブ。"""

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        ids = [item["evidence_id"] for item in _documents(payload)]
        assert "省かず・短くせずにそのまま書く" in system
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": passage["id"],
                        "status": "supported",
                        "evidence_id": evidence_id.format(*ids),
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "根拠文書に手順が記載されているため",
                    }
                    for passage, evidence_id in zip(payload["answer_passages"], written, strict=True)
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        return validate_answer_claims(
            "検証用アカウントを登録し、権限を付けて、通知まで設定する手順は？", PORTAL_ANSWER, PORTAL_EVIDENCE, settings=None
        )


def test_span_ids_shortened_by_the_model_are_bound_to_their_evidence() -> None:
    """モデルが片段の ID の後ろを切って書いても（「E0d7d982」）、根拠の 1 つに決まれば「未登録の原文ID」にしない (#1391)。

    #1362 の評価の da-trial-account-setup を流し直すと、モデルは 21 文字の ID を 8〜10 文字に切って返し、
    手順の段落が引用エラーで外され、回答の本文が空になった。
    """
    result = _portal_validation(["{0:.8}", "{1:.10}", "{2:.12}", "{0:.8}, {1}, {2:.9}"])
    assert result["counts"] == {"unassessed": 4}
    claims = result["claim_checks"]
    assert [claim["source_id"] for claim in claims] == [f"{PORTAL}:2", f"{PORTAL}:3", f"{PORTAL}:4", f"{PORTAL}:2"]
    # 結び付けた片段の完全な ID を残す（監査の記録から根拠を引ける）。
    assert all(len(token) == 21 for claim in claims for token in claim["evidence_id"].split(","))
    assert len(claims[3]["evidence_id"].split(",")) == 3
    # 大文字で書いた ID・ラベルを付けた ID・「…」「...」で省いた ID も同じ。
    result = _portal_validation(["{0:.10}".upper(), "evidence_id: {1:.9}", "evidence_id {2:.8}…", "{0:.8}..."])
    assert result["counts"] == {"unassessed": 4}


def test_shortened_ids_that_do_not_point_to_one_evidence_stay_citation_errors() -> None:
    """根拠に無い ID・短すぎて根拠を決められない ID は、今までどおり引用エラー (#1391)。"""
    result = _portal_validation(["E0000000", "{0:.4}", "{1:.6}…", "{0:.8}, E0000000"])
    assert result["counts"] == {"citation_error": 4}
    assert all(claim["reason"].startswith("未登録の原文ID。") for claim in result["claim_checks"])


# 多段の br-document-portal-retention: 回答が根拠の ID を途中で省いて書き（「…fffa…:1」）、検証のモデルがそれを写した。
PORTAL_LEDGER = {
    "id": "87ae9f2fc8f944a8a78edfba9d8ac8b9:a3ff18c115fc4f2bb10aece14f0cfc09a975f6a90598cd4ccaf483042efd1814:5",
    "source": "system-ledger.xlsx",
    "page_start": None,
    "page_end": None,
    "text": "システムID: SYS-105 / 正式名: ドキュメントポータル / 担当部署: 総 / 重要度: B / 機密区分: 社内限り",
}
RETENTION_SET = "af257c62c85d45cd9211a098fdca6fb4:16f11273eede4c4f508dcc4453985fffa816db12ed7c51b18f03305bee231ace"
RETENTION_RULES = [
    {
        "id": f"{RETENTION_SET}:1",
        "source": "data-retention-rules.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": "第 1 章 機密区分と保管期間\n極秘のデータは 10 年間保管します。\n社内限りのデータは 3 年間保管します。",
    },
    {
        "id": f"{RETENTION_SET}:2",
        "source": "data-retention-rules.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": "第 2 章 閲覧の記録\n極秘のデータを扱うシステムは、閲覧の記録（アクセスログ）を 3 年間保管します。",
    },
]
ABBREVIATED_CHUNK_ID = "af257c62c85d45cd9211a098fdca6fb4:16f11273eede4c4f508dcc4453985fffa…:1"
RETENTION_ANSWER = "\n".join(
    [
        "ドキュメントポータル（SYS‑105）の機密区分は「社内限り」（システム台帳）であり、社内限りのデータは **3 年間** 保管します。",
        f"- データ保管規程: 「社内限りのデータは 3 年間保管します」【evidence_id {ABBREVIATED_CHUNK_ID}】",
        "したがって、ドキュメントポータルのデータの保管期間は **3 年** です。",
    ]
)


def _retention_validation(written: list[str], rechecks: list[dict] | None = None, reaudits: list[dict] | None = None) -> dict:
    """段落ごとに written の evidence_id を返すモデルのスタブ。

    2 回目の呼び出し（ID を書かない段落の確かめ直し。#1412）は、確かめ直す段落に evidence_id を空で返し
    （出典が決まらない）、入力を rechecks に残す。監査の応答の参照の誤りの監査し直し（#112）には最初と同じ ID を返し、
    入力を reaudits に残す。
    """
    reaudits = [] if reaudits is None else reaudits
    calls = []
    first: dict[str, str] = {}

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        if _is_reference_reaudit(payload):
            # 監査し直し（#112）でも、モデルは同じ ID を返す（写し損ねを繰り返す）。
            reaudits.append(payload)
            answers = [first[passage["id"]] for passage in payload["answer_passages"]]
        else:
            calls.append(payload)
            answers = written if len(calls) == 1 else [""] * len(payload["answer_passages"])
            if len(calls) == 1:
                first.update(zip((passage["id"] for passage in payload["answer_passages"]), written, strict=True))
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": passage["id"],
                        "status": "supported",
                        "evidence_id": evidence_id,
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "台帳と規程で裏付けられます。",
                    }
                    for passage, evidence_id in zip(payload["answer_passages"], answers, strict=True)
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims(
            "ドキュメントポータルのデータの保管期間は何年ですか？", RETENTION_ANSWER, [PORTAL_LEDGER, *RETENTION_RULES], settings=None
        )
    if rechecks is not None:
        rechecks.extend(calls[1:])
    return result


def test_chunk_ids_abbreviated_in_the_answer_are_bound_when_copied_by_the_model() -> None:
    """回答に書かれた途中を省いた chunk の id をモデルが写しても、1 つの chunk に決まれば結び付ける (#1391)。"""
    both = f"{PORTAL_LEDGER['id']},{ABBREVIATED_CHUNK_ID}"
    result = _retention_validation([both, f"evidence_id {ABBREVIATED_CHUNK_ID}", both])
    assert result["counts"] == {"supported": 1, "unassessed": 2}
    assert [claim["source_id"] for claim in result["claim_checks"]] == [PORTAL_LEDGER["id"], f"{RETENTION_SET}:1", PORTAL_LEDGER["id"]]
    # 実サービスで流し直すと、モデルは「…」を落として写した（「…fffa:1」）。部分ごとに照合して結び付ける。
    dropped = ABBREVIATED_CHUNK_ID.replace("…", "")
    result = _retention_validation([f"{PORTAL_LEDGER['id']},{dropped}", dropped, f"{PORTAL_LEDGER['id'][:20]}…:5"])
    assert result["counts"] == {"supported": 1, "unassessed": 2}
    assert result["claim_checks"][1]["source_id"] == f"{RETENTION_SET}:1"
    assert result["claim_checks"][2]["source_id"] == PORTAL_LEDGER["id"]
    # 省いた所の後ろ（番号）が違えば根拠に無い chunk、番号まで省けば 2 つの chunk のどちらか決まらない。
    # 回答に書かれた ID でない語（「record_codes.values[1]」）も、今までどおり引用エラー。
    # ただし 2 つ目の段落は段落そのものが根拠の ID（「…fffa…:1」）を書いているので、その根拠に結び付ける（#1404）。
    # ID を書かない 1・3 つ目の段落は、2 つ目の段落が引いた根拠だけで確かめ直し、出典が決まらなければそのまま（#1412）。
    rechecks: list[dict] = []
    result = _retention_validation([ABBREVIATED_CHUNK_ID.replace(":1", ":9"), f"{RETENTION_SET[:40]}…", "record_codes.values[1]"], rechecks)
    assert [claim["status"] for claim in result["claim_checks"]] == ["citation_error", "supported", "citation_error"]
    assert result["claim_checks"][1]["source_id"] == f"{RETENTION_SET}:1"
    [recheck] = rechecks
    assert [passage["id"] for passage in recheck["answer_passages"]] == ["A1", "A3"]
    assert [item["id"] for item in _documents(recheck)] == [f"{RETENTION_SET}:1"]
    assert result["rechecked_passage_ids"] == []
    # 「…」を落とした ID も、番号が違う・残した部分が短い・部分の数が違うものは結び付けない。
    result = _retention_validation([dropped.replace(":1", ":9"), f"{RETENTION_SET.split(':')[0]}:16f112:1", f"{RETENTION_SET}:extra:1"])
    assert [claim["status"] for claim in result["claim_checks"]] == ["citation_error", "supported", "citation_error"]


# #1362 の再評価の Run run_f45cb461c85946459d604dd08561d626（cmp-mes-vs-qms-deadline）の形（#1404。ID は合成した値）。
# 回答は根拠の chunk の id を完全な形で「【証拠 ID 文書:chunk_set:番号】」と書き、検証に渡した根拠にも同じ id がある。
MES = "5c61673c22be4d39b075223c6de78e14:3f574968" + "0" * 56 + ":2"
QMS = "49b9784f8b60426fb3195672b40bd6b7:ed020e40" + "1" * 56 + ":3"
DEADLINE_EVIDENCE = [
    {
        "id": MES,
        "source": "mes-operation-rules.pdf",
        "page_start": 2,
        "page_end": 2,
        "text": "第 3 章 変更の申請\n製造実行システムの変更は、実施日の 5 営業日前までに申請します。",
    },
    {
        "id": QMS,
        "source": "qms-operation-rules.pdf",
        "page_start": 3,
        "page_end": 3,
        "text": "第 4 章 変更の申請\n品質管理システムの変更は、実施日の 3 営業日前までに申請します。",
    },
    {
        "id": "0a1b2c3d4e5f60718293a4b5c6d7e8f9:" + "2" * 64 + ":1",
        "source": "system-ledger.xlsx",
        "page_start": None,
        "page_end": None,
        "text": "システムID: SYS-101 / 正式名: 製造実行システム / 担当部署: 製造部",
    },
]
DEADLINE_ANSWER = "\n\n".join(
    [
        f"製造実行システムの変更は、実施日の 5 営業日前までに申請します【証拠 ID {MES}】。",
        f"品質管理システムの変更は、実施日の 3 営業日前までに申請します【証拠 ID {QMS}】。",
        f"申請の期限は、製造実行システムが 5 営業日前、品質管理システムが 3 営業日前です【証拠 ID {MES}】【証拠 ID {QMS}】。",
        "したがって、申請の期限は製造実行システムの方が長い（5 営業日）です。",
    ]
)


def _deadline_validation(
    claims: list[tuple[str, str]],
    answer: str = DEADLINE_ANSWER,
    recheck: list[tuple[str, str]] | None = None,
    calls: list[dict] | None = None,
    reaudits: list[dict] | None = None,
) -> dict:
    """段落ごとに（status, evidence_id）を返すモデルのスタブ。理由はどれも「裏付けられる」。

    2 回目の呼び出し（ID を書かない段落の確かめ直し。#1412）には recheck を返す。evidence_id の
    「{0}」「{1}」は、確かめ直しに渡した根拠の片段の ID。recheck が無ければ 2 回目の呼び出しを認めない。
    監査の応答の参照の誤りの監査し直し（#112）には最初と同じ判定を返し、入力を reaudits に残す（calls には入れない）。
    """
    seen: list[dict] = [] if calls is None else calls
    first: dict[str, tuple[str, str]] = {}

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        assert "ID の照合はシステムが行う" in system
        if _is_reference_reaudit(payload):
            # 監査し直し（#112）でも、モデルは同じ判定と ID を返す（写し損ねを繰り返す）。
            if reaudits is not None:
                reaudits.append(payload)
            answers = [first[passage["id"]] for passage in payload["answer_passages"]]
        elif not seen:
            seen.append(payload)
            answers = claims
            first.update(zip((passage["id"] for passage in payload["answer_passages"]), claims, strict=True))
        else:
            seen.append(payload)
            assert recheck is not None, "確かめ直しを想定していない"
            ids = [item["evidence_id"] for item in _documents(payload)]
            answers = [(status, evidence_id.format(*ids)) for status, evidence_id in recheck]
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": passage["id"],
                        "status": status,
                        "evidence_id": evidence_id,
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "主張は根拠で裏付けられる。",
                    }
                    for passage, (status, evidence_id) in zip(payload["answer_passages"], answers, strict=True)
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        return validate_answer_claims(
            "製造実行システムと品質管理システムでは、変更の申請の期限はどちらが長いですか？", answer, DEADLINE_EVIDENCE, settings=None
        )


def test_full_chunk_ids_written_in_the_answer_are_not_citation_errors() -> None:
    """根拠の id と完全に一致する ID を引用した段落を「未登録の原文ID」にしない (#1404)。

    モデルは evidence_id に回答の「【証拠 ID …】」をラベルごと写したり、片段の ID を写し損ねたりする。
    ラベル・ID でない語は除いて照合し、それでも決まらなければ段落そのものが書いた ID で根拠に結び付ける。
    """
    result = _deadline_validation(
        [
            ("supported", f"【証拠 ID {MES}】"),
            ("supported", "E9f9f9f9f9f9f9f9f9f9f"),  # 根拠に無い片段の ID: 段落の ID（QMS）で結び付ける。
            ("supported", f"【証拠 ID {MES}】、【証拠ID: {QMS}】"),
            ("supported", f"{MES}（第 3 章）, {QMS}"),
        ]
    )
    assert result["counts"] == {"supported": 3, "unassessed": 1}
    assert [claim["source_id"] for claim in result["claim_checks"]] == [MES, QMS, MES, MES]
    # 結び付けた片段の完全な ID を残す。
    assert all(
        token.startswith("E") and len(token) == 21 for claim in result["claim_checks"][1:] for token in claim["evidence_id"].split(",")
    )
    # evidence_id を空で返しても、段落が書いた ID（完全一致・途中を省いた形・部分ごとの前方一致）で結び付ける。
    mes_document, mes_set, _ = MES.split(":")
    answer = DEADLINE_ANSWER.replace(f"【証拠 ID {QMS}】。", f"【証拠 ID {QMS[:40]}…:3】。", 1).replace(
        f"【証拠 ID {MES}】【", f"【証拠 ID {mes_document}:{mes_set[:12]}:2】【", 1
    )
    result = _deadline_validation(
        [("supported", ""), ("supported", ""), ("supported", ""), ("supported", f"【証拠 ID {MES}】")], answer=answer
    )
    assert result["counts"] == {"supported": 3, "unassessed": 1}
    assert [claim["source_id"] for claim in result["claim_checks"]] == [MES, QMS, MES, MES]


def test_model_citation_errors_with_resolved_ids_are_not_kept() -> None:
    """モデルが citation_error を返しても、引用した ID がすべて根拠に決まれば出典の誤りにしない (#1404)。

    裏付けの判定はモデルから返っていないので、supported にはせず unassessed（確かめが終わっていない）にする。
    """
    first = [("citation_error", ""), ("citation_error", f"【証拠 ID {QMS}】"), ("supported", MES), ("citation_error", "")]
    # 4 つ目の段落は ID を書いておらず、モデルの evidence_id も空なので出典を照合できない。他の段落が引いた根拠
    # （MES・QMS）で確かめ直しても出典が決まらなければ、citation_error のまま（#1412）。
    result = _deadline_validation(first, recheck=[("citation_error", "")])
    statuses = [claim["status"] for claim in result["claim_checks"]]
    assert statuses == ["unassessed", "unassessed", "supported", "citation_error"]
    assert result["claim_checks"][0]["reason"].startswith("引用した ID は根拠に一致しますが")
    # モデルが候補で supported と判定しても、実際の出典を付けていない結論は unassessed。
    calls: list[dict] = []
    result = _deadline_validation(first, recheck=[("supported", "{0},{1}")], calls=calls)
    assert [claim["status"] for claim in result["claim_checks"]] == ["unassessed", "unassessed", "supported", "unassessed"]
    assert result["claim_checks"][3]["source_id"] == MES and result["rechecked_passage_ids"] == ["A4"]
    assert [item["id"] for item in _documents(calls[1])] == [MES, QMS]


def test_unresolved_ids_in_the_answer_stay_citation_errors() -> None:
    """段落の ID が根拠に無い・1 つでも決まらないときは、今までどおり引用エラー (#1404)。"""
    unknown = "ffffffffffffffffffffffffffffffff:" + "3" * 64 + ":9"
    answer = "\n\n".join(
        [
            f"製造実行システムの変更は、実施日の 5 営業日前までに申請します【証拠 ID {unknown}】。",
            f"品質管理システムの変更は、実施日の 3 営業日前までに申請します【証拠 ID {QMS}】【証拠 ID {unknown}】。",
            "したがって、申請の期限は製造実行システムの方が長い（5 営業日）です。",
            # 途中を省いた ID でも、番号が違えば根拠に無い chunk。
            f"両システムとも申請が要ります【証拠 ID {MES.split(':')[0][:10]}…:7】。",
        ]
    )
    # ID を書かない 3 つ目の段落は、他の段落が引いた ID がどれも根拠に決まらないので確かめ直さない（#1412）。
    calls: list[dict] = []
    reaudits: list[dict] = []
    result = _deadline_validation(
        [("supported", "E9f9f9f9f9f9f9f9f9f9f"), ("supported", "証拠"), ("supported", "第 3 章"), ("citation_error", "")],
        answer=answer,
        calls=calls,
        reaudits=reaudits,
    )
    assert result["counts"] == {"citation_error": 4} and len(calls) == 1
    assert all(claim["reason"].startswith("未登録の原文ID。") for claim in result["claim_checks"][:3])
    # 段落が書いた ID が根拠に無い段落（回答の引用の誤り）は監査し直さず、監査が返した ID だけの誤りの 3 つ目の
    # 段落だけを 1 回監査し直す（#112。同じ ID を返したので citation_error のまま）。
    assert [[passage["id"] for passage in call["answer_passages"]] for call in reaudits] == [["A3"]]
    assert [claim["reference_reaudit"] for claim in result["claim_checks"]] == ["", "", "rejected", ""]


# #1362 の確認の Run run_3b7a1c0ff119477781a0042ea2a8dd23（holdout br-customer-portal-change-window）の形
# （#1412。ID と本文は合成した値）。結論とまとめの段落は根拠の ID を書かず、「根拠」の 2 段落が完全な ID を書く。
PLAN = "815d7759aaaa4887b1b070379e2958af:a657c65a" + "4" * 56
CHANGE = "2a5ec48abbbb4c81989a580a5676476a:58fa1e8c" + "5" * 56
WINDOW_LEDGER = "95e6cc20cccc4339a8e32763c996ce5c:28fb4739" + "6" * 56 + ":28"
WINDOW_EVIDENCE = [
    {
        "id": f"{PLAN}:0",
        "source": "maintenance-plan.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": "定期保守計画 2026年度\n発行: 2026年4月1日 作成: 情シス",
    },
    {
        "id": f"{PLAN}:1",
        "source": "maintenance-plan.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": "第 1 章 定期保守の時間帯\nCustomer Portal: 毎月第 1 日曜日の 0:00〜4:00",
    },
    {
        "id": WINDOW_LEDGER,
        "source": "system-ledger.xlsx",
        "page_start": None,
        "page_end": None,
        "text": "システムID: SYS-128 / 正式名: カスタマーポータル / 略称・別表記: Customer Portal / 重要度: A",
    },
    {
        "id": f"{CHANGE}:4",
        "source": "change-procedure.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": "第 4 章 作業の時間帯\n重要度 A のシステムの変更は、定期保守の時間帯にだけ作業します。",
    },
    {
        "id": f"{CHANGE}:5",
        "source": "change-procedure.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": "第 5 章 利用者への告知\nシステムの停止を伴う変更は、作業の 5 営業日前までに告知します。",
    },
]
WINDOW_ANSWER = (
    "**回答**  \nCustomer Portal（SYS‑128）の本番環境での変更作業は、**毎月第 1 日曜日の 0:00 〜 4:00 の定期保守枠**で"
    "実施できます。\n\n**根拠**  \n"
    f"1. 【定期保守計画 2026年度】「Customer Portal: 毎月第 1 日曜日の 0:00〜4:00」​【evidence_id: {PLAN}:1】  \n"
    f"2. 【システム変更手順書】重要度 A のシステムは「定期保守の時間帯にだけ作業します」​【evidence_id: {CHANGE}:4】\n\n"
    "以上の資料に基づき、Customer Portal の本番変更は上記の保守時間帯に行うことが規定されています。"
)
# 実サービスの最初の判定の形: 結論（A2）は supported で evidence_id が空、まとめ（A6）は citation_error。
WINDOW_FIRST = {"A2": ("supported", ""), "A4": ("supported", f"{PLAN}:1"), "A5": ("supported", f"{CHANGE}:4"), "A6": ("citation_error", "")}


def _window_validation(
    first: dict[str, tuple[str, str]], recheck: dict[str, tuple[str, str]] | Exception | None, answer: str = WINDOW_ANSWER
) -> tuple[dict, list[tuple[str, dict]]]:
    """段落の ID ごとに（status, evidence_id）を返すモデルのスタブ。

    evidence_id の「@chunk の id」は、その chunk の片段の ID（その呼び出しに渡した根拠の中のもの）。
    2 回目の呼び出し（確かめ直し）には recheck を返す（例外なら送出し、None なら確かめ直しを認めない）。
    """
    calls: list[tuple[str, dict]] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        if _is_reference_reaudit(payload):
            # 監査し直し（#112）でも、モデルは同じ判定と ID を返す（写し損ねを繰り返す）。
            answers = first
        else:
            calls.append((system, payload))
            answers = first if len(calls) == 1 else recheck
        assert answers is not None, "確かめ直しを想定していない"
        if isinstance(answers, Exception):
            raise answers
        ids = {item["id"]: item["evidence_id"] for item in _documents(payload)}

        def written(passage_id: str) -> str:
            text = answers[passage_id][1]
            for chunk_id, evidence_id in ids.items():
                text = text.replace("@" + chunk_id, evidence_id)
            return text

        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": passage["id"],
                        "status": answers[passage["id"]][0],
                        "evidence_id": written(passage["id"]),
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "定期保守計画と変更手順書で根拠で裏付けられています。",
                    }
                    for passage in payload["answer_passages"]
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("Customer Portal の本番の変更は、いつ作業できますか？", answer, WINDOW_EVIDENCE, settings=None)
    return result, calls


def _cite(*chunk_ids: str) -> str:
    """スタブの evidence_id の書式（chunk の片段の ID を「,」で並べる）。"""
    return ",".join("@" + chunk_id for chunk_id in chunk_ids)


def test_unreferenced_conclusions_are_rechecked_with_evidence_cited_by_other_passages() -> None:
    """ID を書かない結論・まとめの段落の出典が決まらなければ、他の段落が引いた根拠で確かめ直す (#1412)。"""
    both = _cite(f"{PLAN}:1", f"{CHANGE}:4")
    result, calls = _window_validation(WINDOW_FIRST, {"A2": ("supported", both), "A6": ("supported", both)})
    assert result["counts"] == {"supported": 2, "unassessed": 2}
    assert [claim["answer_passage_id"] for claim in result["claim_checks"]] == ["A2", "A4", "A5", "A6"]
    assert [claim["source_id"] for claim in result["claim_checks"]] == [f"{PLAN}:1", f"{PLAN}:1", f"{CHANGE}:4", f"{PLAN}:1"]
    assert result["rechecked_passage_ids"] == ["A2", "A6"]
    # 確かめ直しには ID を書かない 2 段落と、他の段落が引いた 2 つの根拠だけを渡す（質問・回答の全文は同じ）。
    (system, first), (_, recheck) = calls
    assert "段落に ID が無いことを citation_error にしない" in system
    assert "未引用の資料を代わりに選ばない" in system
    assert [passage["id"] for passage in first["answer_passages"]] == ["A2", "A4", "A5", "A6"]
    assert [passage["id"] for passage in recheck["answer_passages"]] == ["A2", "A6"]
    assert [item["id"] for item in _documents(recheck)] == [f"{PLAN}:1", f"{CHANGE}:4"]
    assert recheck["answer_text"] == WINDOW_ANSWER and recheck["question"] == first["question"]


def test_recheck_keeps_the_first_result_unless_it_finds_support() -> None:
    """確かめ直しで出典が決まらない・否定の判定・呼び出しの失敗は、最初の判定のまま (#1412)。"""
    for recheck in (
        {"A2": ("supported", ""), "A6": ("citation_error", "")},
        # 根拠を絞った判定なので、否定の判定は採らない。
        {"A2": ("unsupported", ""), "A6": ("contradicted", _cite(f"{CHANGE}:4"))},
        # 候補でない根拠（台帳の行）・根拠に無い片段の ID は、確かめ直しでは出典が決まらない。
        {"A2": ("supported", WINDOW_LEDGER), "A6": ("supported", "E" + "0" * 20)},
        TimeoutError("確かめ直しの呼び出しが失敗"),
    ):
        result, calls = _window_validation(WINDOW_FIRST, recheck)
        assert [claim["status"] for claim in result["claim_checks"]] == ["citation_error", "supported", "supported", "citation_error"], (
            recheck
        )
        assert result["claim_checks"][0]["reason"].startswith("未登録の原文ID。")
        assert result["rechecked_passage_ids"] == [] and len(calls) == 2
    # 片方だけ裏付けられれば、その段落だけ替える。
    result, _ = _window_validation(WINDOW_FIRST, {"A2": ("supported", _cite(f"{PLAN}:1")), "A6": ("unsupported", "")})
    assert [claim["status"] for claim in result["claim_checks"]] == ["unassessed", "supported", "supported", "citation_error"]
    assert result["rechecked_passage_ids"] == ["A2"]


def test_written_ids_missing_from_the_evidence_stay_citation_errors_and_are_not_rechecked() -> None:
    """段落が書いた ID が根拠に無い citation_error は確かめ直さず、その ID を候補にもしない (#1412)。"""
    unknown = "ffffffffffffffffffffffffffffffff:" + "7" * 64 + ":9"
    answer = WINDOW_ANSWER.replace(f"{PLAN}:1】", f"{unknown}】")
    first = {**WINDOW_FIRST, "A4": ("supported", unknown)}
    result, calls = _window_validation(
        first, {"A2": ("supported", _cite(f"{CHANGE}:4")), "A6": ("supported", _cite(f"{CHANGE}:4"))}, answer=answer
    )
    assert [claim["status"] for claim in result["claim_checks"]] == ["unassessed", "citation_error", "supported", "unassessed"]
    assert result["claim_checks"][1]["reason"].startswith("未登録の原文ID。")
    # 確かめ直しの段落に A4（ID を書いた段落）は入らず、候補は A5 が引いた根拠だけ。
    recheck = calls[1][1]
    assert [passage["id"] for passage in recheck["answer_passages"]] == ["A2", "A6"]
    assert [item["id"] for item in _documents(recheck)] == [f"{CHANGE}:4"]
    # 他の段落が引いた根拠がどれも決まらなければ、確かめ直さない。
    answer = answer.replace(f"{CHANGE}:4】", f"{unknown}】")
    result, calls = _window_validation({**first, "A5": ("supported", unknown)}, None, answer=answer)
    assert result["counts"] == {"citation_error": 4} and len(calls) == 1


# #1362 の確認の Run run_5352385802954df9a521e1a10ff7d881（dev tl-logistics-oms）の形（#1413。ID と本文は合成した値）。
# 質問は「サンプル物流社のシステム台帳で」と範囲を限定し、根拠には本社の台帳の同じ略称（OMS）の行も入る。
LOGISTICS = "449fb52adddd4843f6b4783146d22de1:c2a2b3d1" + "8" * 56
HEADQUARTERS = "95e6cc20eeee4339a8e32763c996ce5c:28fb4739" + "9" * 56
OMS_EVIDENCE = [
    {
        "id": f"{LOGISTICS}:0",
        "source": "logistics-system-ledger.xlsx",
        "page_start": None,
        "page_end": None,
        "text": "サンプル物流社のシステム台帳（サンプル社の子会社）\n担当部署はサンプル物流社の部署の略号です。",
    },
    {
        "id": f"{LOGISTICS}:1",
        "source": "logistics-system-ledger.xlsx",
        "page_start": None,
        "page_end": None,
        "text": "システムID: SL-201 / 正式名: 運行管理システム / 略称・別表記: OMS / 担当部署: 運",
    },
    {
        "id": f"{HEADQUARTERS}:0",
        "source": "system-ledger.xlsx",
        "page_start": None,
        "page_end": None,
        "text": "サンプル社のシステム台帳\n担当部署は部署の略号（漢字 1 文字）で書きます。",
    },
    {
        "id": f"{HEADQUARTERS}:4",
        "source": "system-ledger.xlsx",
        "page_start": None,
        "page_end": None,
        "text": "システムID: SYS-104 / 正式名: 受発注管理システム / 略称・別表記: OMS、受発注 / 担当部署: 営",
    },
]
OMS_QUESTION = "サンプル物流社のシステム台帳で、略称が OMS のシステムの正式名は何ですか？"


def _oms_answer(name: str) -> str:
    return (
        f"サンプル物流社のシステム台帳で、略称が **OMS** のシステムの正式名は **「{name}」** です。\n\n**根拠**\n"
        "- 「システムID: SL-201 / 正式名: 運行管理システム / 略称・別表記: OMS」"
        f"(logistics-system-ledger.xlsx、シート「システム台帳」行 4)【evidence_id: {LOGISTICS}:1】"
    )


def _oms_validation(answer: str, verdict) -> tuple[dict, str]:
    """監査する段落（結論の段落だけ。見出しと出典の行は監査に渡らない）に verdict(system) の（status, chunk の id）を返すスタブ。"""
    seen: list[tuple[str, list[str]]] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        seen.append((system, [passage["text"] for passage in payload["answer_passages"]]))
        ids = {item["id"]: item["evidence_id"] for item in _documents(payload)}
        status, chunk_id = verdict(system)
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": passage["id"],
                        "status": status,
                        "evidence_id": ids[chunk_id],
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "台帳の OMS の行で判定。",
                    }
                    for passage in payload["answer_passages"]
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims(OMS_QUESTION, answer, OMS_EVIDENCE, settings=None)
    [(system, passages)] = seen
    assert passages == [answer.split("\n\n")[0]]
    return result, system


SCOPE_RULE = "その範囲の外の根拠（別の会社の台帳の同じ略称の行など）との違いを contradicted にしない"


def test_evidence_outside_the_scope_of_the_question_is_not_a_contradiction() -> None:
    """質問が限定した範囲（会社・台帳・版・年度・部署）の外の根拠との違いを contradicted にしない指示がある (#1413)。

    実サービスでは、物流社の台帳の「OMS = 運行管理システム」を、本社の台帳の「OMS = 受発注管理システム」と
    食い違うとして contradicted にし、正しい結論を本文から外した。スタブは指示に範囲の規則があるときだけ、
    範囲の中の根拠（物流社の台帳の行）で supported を返す（規則が無ければ範囲の外の行で contradicted）。
    """

    def verdict(system: str) -> tuple[str, str]:
        return ("supported", f"{LOGISTICS}:1") if SCOPE_RULE in system else ("contradicted", f"{HEADQUARTERS}:4")

    result, system = _oms_validation(_oms_answer("運行管理システム"), verdict)
    assert "範囲の中の根拠と食い違うときだけ contradicted にする" in system
    # 実サービスの再生では「同じ略称の別の行があるので唯一と断定できない」を理由に contradicted にした。
    assert "ほかに同じ略称があることを矛盾や断定できない理由にしない" in system
    assert "B 社の行を理由に contradicted にしない" in system
    assert result["counts"] == {"unassessed": 1}
    assert result["claim_checks"][0]["source_id"] == f"{LOGISTICS}:1"


def test_contradictions_with_evidence_inside_the_scope_stay_contradicted() -> None:
    """範囲の中の根拠と食い違う結論は、今までどおり contradicted (#1413)。"""
    result, _ = _oms_validation(_oms_answer("受発注管理システム"), lambda system: ("contradicted", f"{LOGISTICS}:1"))
    [claim] = result["claim_checks"]
    assert claim["status"] == "contradicted" and claim["source_id"] == f"{LOGISTICS}:1"


LEDGER_ROW = {
    "id": "ledger:23",
    "source": "system-ledger.xlsx",
    "page_start": None,
    "page_end": None,
    "sheet_name": "システム台帳",
    "row_start": 23,
    "row_end": 23,
    "cell_range": "A23:F23",
    "text": "システムID: SYS-119 / 正式名: 購買管理システム / 略称・別表記: PMS / 重要度: A",
}


def test_sheet_locations_are_passed_to_the_model_without_changing_evidence_ids() -> None:
    """表計算の根拠の場所（シート・行・セル範囲）をモデルに渡す根拠の項目に写す（#1450）。

    根拠の ID は本文と範囲から計算するので、場所を足しても変わらない。場所の無い根拠には項目を足さない。
    """
    from rag_engine.evaluation.answer_eval import _evidence_fragments

    [fragment] = list(_evidence_fragments([LEDGER_ROW]))
    assert (fragment["sheet_name"], fragment["row_start"], fragment["row_end"], fragment["cell_range"]) == (
        "システム台帳",
        23,
        23,
        "A23:F23",
    )
    bare = {key: value for key, value in LEDGER_ROW.items() if key not in {"sheet_name", "row_start", "row_end", "cell_range"}}
    [without] = list(_evidence_fragments([bare]))
    assert without["evidence_id"] == fragment["evidence_id"]
    assert "sheet_name" not in without and "section_path" not in without
    [pdf] = list(_evidence_fragments(EVIDENCE))
    assert set(pdf) == {
        "id",
        "source",
        "source_run_id",
        "page_start",
        "page_end",
        "parent_id",
        "text",
        "fragment_start",
        "fragment_end",
        "evidence_id",
    }


def test_paragraphs_that_only_write_the_location_differently_are_not_unsupported() -> None:
    """段落が書いた場所（行23）を根拠の場所（row_start 23）と照らせる入力と指示を渡す（#1450）。

    モデルはスタブで、根拠の項目に行の範囲があり段落の行と合うときだけ supported を返す（実環境のモデルは
    場所が根拠に無いと「位置情報が根拠に含まれない」で unsupported にしていた）。
    """
    answer = "- 行23: システムID: SYS-119 / 正式名: 購買管理システム / 略称・別表記: PMS / 重要度: A"
    seen: dict = {}

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        [item] = _documents(payload)
        seen["system"] = system
        located = item.get("row_start") == 23 and item.get("sheet_name") == "システム台帳"
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": payload["answer_passages"][0]["id"],
                        "status": "supported" if located else "unsupported",
                        "evidence_id": item["evidence_id"] if located else "",
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "内容と行が根拠と一致" if located else "行23 という位置情報は根拠に含まれない",
                    },
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("PMS の重要度は？", answer, [LEDGER_ROW], settings=None)
    assert result["counts"] == {"unassessed": 1}
    assert result["claim_checks"][0]["source_id"] == "ledger:23"
    assert result["rubric_version"] == VALIDATION_RUBRIC_VERSION >= 7
    # 判定の指示: 場所の表記の違いだけでは unsupported にせず、場所が食い違うときだけ unsupported にする。
    assert "場所の表記を理由に unsupported にしない" in seen["system"]
    assert "食い違う" in seen["system"] and "row_start" in seen["system"]


# 段落の分け方の事例（RAG と Agent の両方のテストが読む。#1455）。
SENTENCE_CASES = json.loads(
    (Path(__file__).resolve().parents[4] / "platform/contracts/answer-passages/sentence-splits.json").read_text("utf-8")
)


@pytest.mark.parametrize("case", SENTENCE_CASES["cases"], ids=lambda case: case["id"])
def test_passages_are_not_split_inside_brackets(case: dict) -> None:
    """括弧の中の「。」で段落を分けず、太字の印・閉じ括弧だけの断片を作らない（#1455）。"""
    from rag_engine.generation.operation_audit import answer_passages

    assert [item["text"] for item in answer_passages(case["line"])] == case["passages"]


def test_unverified_section_titles_match_the_builtin_language_pack() -> None:
    """回答の末尾の節（回答に入れられなかった範囲・省いた処理）の見出しは、共有の語彙
    （platform/contracts/answer-passages/vocabulary.json の unverified_section_titles）と同じ（#3240。
    Agent は RAG の言語資源を読まず、契約の語彙でこの節を見分ける）。"""
    from rag_parser_core.language import builtin_pack

    from rag_engine.generation.answer_passage_vocabulary import vocabulary

    pack = builtin_pack()
    titles = [pack.text("answer_labels", key) for key in ("gaps_section", "unverified_section", "deadline_section")]

    assert vocabulary()["unverified_section_titles"] == titles


def test_passage_ranges_locate_each_passage_in_the_answer() -> None:
    """段落の位置（``answer_passage_ranges``）は、回答の中のその段落の原文を指す（#3240。MCP の
    ``rag_validate_answer`` の ``answer_range``）。複数の行・行頭の空白・空の行・600 文字を超える文を含む。"""
    from rag_engine.generation.operation_audit import answer_passage_ranges, answer_passages

    long_sentence = "あ" * 650 + "。"
    answer = "\n".join(
        [
            *(case["line"] for case in SENTENCE_CASES["cases"]),
            "",
            "   見出し",
            f"  {long_sentence}続きの文です。",
            "  「引用の文です。引用の続きです。」",
        ]
    )
    passages = answer_passages(answer)
    ranges = answer_passage_ranges(answer)

    assert [passage_id for passage_id, _start, _end in ranges] == [item["id"] for item in passages]
    assert [answer[start:end] for _id, start, end in ranges] == [item["text"] for item in passages]
    assert [answer[start:end] for _id, start, end in ranges][-4:] == [
        "あ" * 600,
        "あ" * 50 + "。",
        "続きの文です。",
        "「引用の文です。引用の続きです。」",
    ]


def test_quoted_ledger_header_is_audited_as_one_passage() -> None:
    """cmp-pms-severity の「行1〜3（表頭の説明）: 「…。…。」」を 1 つの段落として監査に渡す（#1455）。"""
    line = "  - 行1〜3（表頭の説明）: 「重要度は A・B・C の 3 段階です。A が最も高く、B、C の順に低くなります。」"
    seen: list[list[str]] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        seen.append([item["text"] for item in payload["answer_passages"]])
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": item["id"],
                        "status": "supported",
                        "evidence_id": _documents(payload)[0]["evidence_id"],
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "根拠に記載",
                    }
                    for item in payload["answer_passages"]
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("重要度の段階は？", line, EVIDENCE, settings=None)
    assert seen == [[line.strip()]]
    assert result["counts"] == {"unassessed": 1}


PREMISE_ANSWER = (
    "利用者の回答により、部門長の承認は得ていません。\n\n"
    "承認が得られていないため、アカウントは削除できません。\n\n"
    "削除の承認は人事部長が行います。"
)
PREMISE_EVIDENCE = [
    {
        "id": "c1",
        "source": "manual.pdf",
        "page_start": 2,
        "page_end": 2,
        "text": "削除の前に、所属部署の部門長の承認を得てください。承認の無い削除は行いません。",
    }
]


def test_premises_support_the_user_answer_and_the_conclusion_drawn_from_it() -> None:
    """利用者が確認で答えた前提を根拠の前に渡し、答えを述べた段落と答えに規則を当てはめた結論を確かめる (#1898)。

    前提だけで裏付けた段落の出典は前提の id、資料の根拠と合わせて裏付けた段落の出典は資料の根拠。前提は検証した
    根拠（evidence_ids）に数えない。
    """
    from rag_engine.evaluation.answer_validation import (
        PREMISE_ID_PREFIX,
        PREMISE_SOURCE,
        PremiseConsistencyOutput,
        is_premise_id,
        premise_items,
    )

    seen: dict = {}

    def parse(system, inputs, settings, schema, provider_id=None):
        if schema is PremiseConsistencyOutput:
            from rag_engine.evaluation.answer_validation import PREMISE_CONSISTENCY_SYSTEM_PROMPT

            assert system == PREMISE_CONSISTENCY_SYSTEM_PROMPT and system.startswith("## Step 1:")
            # 答えと両立しない内容を勧める段落は無いので、判定を変えない（#2726）。
            return PremiseConsistencyOutput(checks=[])
        payload = json.loads(inputs)
        seen.update(payload=payload, system=system)
        premise, document = _documents(payload)
        ids = [item["id"] for item in payload["answer_passages"]]

        def check(passage_id: str, status: str, evidence_id: str) -> dict:
            return {
                "answer_quote": "段落",
                "answer_passage_id": passage_id,
                "status": status,
                "evidence_id": evidence_id,
                "source_id": "",
                "evidence_quote": "",
                "reason": "判定",
            }

        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    check(ids[0], "supported", premise["evidence_id"]),
                    check(ids[1], "supported", f"{premise['evidence_id']},{document['evidence_id']}"),
                    check(ids[2], "unsupported", ""),
                ]
            }
        )

    premises = [{"question": "所属部署の部門長の承認は得ていますか？", "answer": " いいえ "}, {"question": "空", "answer": ""}]
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("アカウントを削除する手順", PREMISE_ANSWER, PREMISE_EVIDENCE, settings=None, premises=premises)

    premise = _documents(seen["payload"])[0]
    assert (premise["id"], premise["source"]) == (f"{PREMISE_ID_PREFIX}1", PREMISE_SOURCE)
    # モデルには根拠の本文の文字の形をそろえて渡す（「？」は半角になる。#1350）。
    assert premise["text"].endswith("\n利用者の答え: いいえ")
    assert premise_items(premises)[0]["text"] == "確認の質問: 所属部署の部門長の承認は得ていますか？\n利用者の答え: いいえ"
    assert PREMISE_SOURCE in seen["system"]
    assert result["counts"] == {"supported": 1, "unassessed": 1, "unsupported": 1}
    stated, concluded, other = result["claim_checks"]
    assert (stated["status"], stated["source_id"]) == ("supported", "premise:1")
    assert (concluded["status"], concluded["source_id"]) == ("unassessed", "c1")
    assert other["status"] == "unsupported"
    assert result["evidence_ids"] == ["c1"]
    assert is_premise_id("premise:1") and not is_premise_id("c1") and not is_premise_id(None)
    # 前提の件数は上限まで、答えの無い前提は渡さない。
    assert len(premise_items([{"question": "q", "answer": "a"}] * 20)) == 10
    only_answer = premise_items([{"question": "", "answer": "はい"}])
    assert only_answer == [{"id": "premise:1", "source": PREMISE_SOURCE, "text": "利用者の答え: はい"}]


SCOPE_ANSWER = "共有グループの表示区分を変更し、保存して確定します。\n\n項目 A の所属先だけを変更します。"
SCOPE_EVIDENCE = [
    {
        "id": "c1",
        "source": "manual.pdf",
        "page_start": 3,
        "page_end": 3,
        "text": "共有グループの表示区分を変更すると、グループのすべての項目に反映されます。保存して確定します。",
    }
]


def test_paragraph_broader_than_the_users_scope_is_contradicted_by_the_premise() -> None:
    """利用者が確認で範囲を限定したのに、広い範囲の操作を勧める段落は前提と矛盾する (#2726)。

    主張の監査が資料で supported としても、答えの条件・制約と両立しない（conflicts）段落は contradicted にする。
    範囲を限定した手順の確かめられない段落は unsupported のまま残す。確認の答えが無い操作要求も、原質問の対象と比べる。
    """
    from rag_engine.evaluation.answer_validation import (
        PREMISE_CONSISTENCY_SYSTEM_PROMPT,
        SUPPORT_PREMISE_CONFLICT,
        PremiseConsistencyOutput,
        claim_mark_code,
    )

    scope_inputs: list = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        ids = [item["id"] for item in payload["answer_passages"]]
        if schema is PremiseConsistencyOutput:
            assert system == PREMISE_CONSISTENCY_SYSTEM_PROMPT
            scope_inputs.append(payload)
            return PremiseConsistencyOutput.model_validate(
                {
                    "checks": [
                        {
                            "answer_passage_id": ids[0],
                            "constraints": "項目 A だけ",
                            "recommendation": "共有グループの表示区分を変える",
                            "fit": "conflicts" if payload["user_answers"] else "fits",
                            "constraint_scope": "content",
                            "passage_mode": "execution",
                            "constraint_quote": "項目 A だけ。グループは変えない",
                            "passage_quote": payload["answer_passages"][0]["text"],
                        },
                        {
                            "answer_passage_id": ids[1],
                            "constraints": "項目 A だけ",
                            "recommendation": "項目 A の所属先を変える",
                            "fit": "fits",
                        },
                        {"answer_passage_id": "A9", "constraints": "", "recommendation": "", "fit": "conflicts"},
                    ]
                }
            )
        document = _documents(payload)[-1]

        def check(passage_id: str, status: str, evidence_id: str) -> dict:
            return {
                "answer_quote": "段落",
                "answer_passage_id": passage_id,
                "status": status,
                "evidence_id": evidence_id,
                "source_id": "",
                "evidence_quote": "",
                "reason": "判定",
            }

        return ClaimAuditOutput.model_validate(
            {"claim_checks": [check(ids[0], "supported", document["evidence_id"]), check(ids[1], "unsupported", "")]}
        )

    premises = [{"question": "変更するのは個別の項目ですか、共有グループ全体ですか？", "answer": "項目 A だけ。グループは変えない"}]
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("表示設定 A を変更したい", SCOPE_ANSWER, SCOPE_EVIDENCE, settings=None, premises=premises)
        unlimited = validate_answer_claims("表示設定 A を変更したい", SCOPE_ANSWER, SCOPE_EVIDENCE, settings=None)

    scope, question_only = scope_inputs
    assert scope["user_answers"] == [
        "確認の質問: 変更するのは個別の項目ですか、共有グループ全体ですか？\n利用者の答え: 項目 A だけ。グループは変えない"
    ]
    assert [item["text"] for item in scope["answer_passages"]] == SCOPE_ANSWER.split("\n\n")
    broader, narrow = result["claim_checks"]
    assert (broader["status"], broader["support_check"], broader["audit_status"]) == ("contradicted", SUPPORT_PREMISE_CONFLICT, "supported")
    assert claim_mark_code(broader["status"], broader["support_check"]) == "contradicted"
    assert narrow["status"] == "unsupported"
    assert result["counts"] == {"contradicted": 1, "unsupported": 1}
    # 確認の答えが無くても原質問で検査する。限定を捏造せず、両立する判定なら元の監査を保つ。
    assert question_only["user_answers"] == []
    assert question_only["question"] == "表示設定 A を変更したい"
    assert [claim["status"] for claim in unlimited["claim_checks"]] == ["unassessed", "unsupported"]


def test_premise_consistency_check_failure_keeps_the_audit_verdicts() -> None:
    """答えとの比較が失敗しても、主張の監査の判定を返す (#2726)。"""
    from rag_engine.evaluation.answer_validation import PremiseConsistencyOutput

    def parse(system, inputs, settings, schema, provider_id=None):
        if schema is PremiseConsistencyOutput:
            raise RuntimeError("synthetic failure")
        payload = json.loads(inputs)
        document = _documents(payload)[-1]
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": item["id"],
                        "status": "supported",
                        "evidence_id": document["evidence_id"],
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "判定",
                    }
                    for item in payload["answer_passages"]
                ]
            }
        )

    premises = [{"question": "範囲は？", "answer": "項目 A だけ"}]
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("表示設定 A を変更したい", SCOPE_ANSWER, SCOPE_EVIDENCE, settings=None, premises=premises)
    assert result["counts"] == {"unassessed": 2}


def test_premises_are_not_truncated_by_the_evidence_budget() -> None:
    """根拠を予算で切り詰めても、前に置いた前提は残る (#1898)。"""
    seen: list = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        seen.append(_documents(payload))
        return ClaimAuditOutput.model_validate({"claim_checks": []})

    long_evidence = [{"id": f"c{i}", "source": "m.pdf", "page_start": 1, "page_end": 1, "text": "あ" * 4000} for i in range(200)]
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims(
            "q", "承認は得ていません。", long_evidence, settings=None, premises=[{"question": "承認は？", "answer": "いいえ"}]
        )
    assert result["evidence_truncated"] is True
    assert seen[0][0]["id"] == "premise:1"


DATA_ANSWER = "部長の承認が要る範囲の受注は 6 件です。\n\n受注金額は 6 万〜24 万円なので、6 件すべてが部長の承認の範囲に入ります。"
DATA_EVIDENCE = [{"id": "c1", "source": "rules.pdf", "page_start": 1, "page_end": 1, "text": "5 万円以上 30 万円未満は部長が承認する。"}]


def test_data_results_support_values_and_conclusions_drawn_from_them() -> None:
    """業務データの照会の結果を資料ではない根拠として並べ、値の段落と規則を当てはめた結論を確かめる (#1950)。"""
    from rag_engine.evaluation.answer_validation import (
        DATA_RESULT_ID_PREFIX,
        DATA_RESULT_SOURCE,
        data_result_items,
        is_premise_id,
    )

    seen: dict = {}

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        seen.update(payload=payload, system=system)
        data, document = _documents(payload)
        ids = [item["id"] for item in payload["answer_passages"]]
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": ids[0],
                        "status": "supported",
                        "evidence_id": data["evidence_id"],
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "結果の件数",
                    },
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": ids[1],
                        "status": "supported",
                        "evidence_id": f"{data['evidence_id']},{document['evidence_id']}",
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "規則を結果に当てはめた",
                    },
                ]
            }
        )

    data_results = [
        {
            "source": "データ問い合わせ（NL2SQL） MCP_TEST_SALES",
            "question": "受注金額が5万円以上30万円未満の件数",
            "sql": "SELECT COUNT(*) FROM SALES_ORDER WHERE SALES_AMOUNT >= 50000",
            "result": '{"columns": ["COUNT"], "rows": [{"COUNT": 6}]}',
        },
        {"question": "空", "result": " "},
    ]
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("q", DATA_ANSWER, DATA_EVIDENCE, settings=None, data_results=data_results)

    data = _documents(seen["payload"])[0]
    assert (data["id"], data["source"]) == (f"{DATA_RESULT_ID_PREFIX}1", DATA_RESULT_SOURCE)
    assert "実行した SQL: SELECT COUNT(*)" in data["text"]
    assert DATA_RESULT_SOURCE in seen["system"]
    stated, concluded = result["claim_checks"]
    assert (stated["status"], stated["source_id"]) == ("supported", "data:1")
    assert (concluded["status"], concluded["source_id"]) == ("unassessed", "c1")
    # 照会の結果は検証した資料の根拠（evidence_ids）に数えない。結果の無いものは渡さない。
    assert result["evidence_ids"] == ["c1"]
    assert is_premise_id("data:1")
    assert len(data_result_items(data_results)) == 1
    assert len(data_result_items([{"result": "x"}] * 20)) == 10


QUESTION_TEXT = "年収 120 万円の配偶者は、収入の要件を満たしますか。"
QUESTION_ANSWER = (
    "配偶者の年収は 120 万円です。\n\n年収 120 万円は 130 万円未満なので、収入の要件を満たします。\n\n要件の確認は年に 2 回行います。"
)
QUESTION_EVIDENCE = [
    {"id": "c1", "source": "rules.pdf", "page_start": 1, "page_end": 1, "text": "収入の要件は年収 130 万円未満であること。"},
]


def test_question_facts_support_stated_facts_and_rules_applied_to_them() -> None:
    """質問の文を資料ではない根拠として先頭に渡し、質問の事実を述べた段落と規則を当てはめた結論を確かめる。

    質問だけで裏付けた段落の出典は質問の id、資料の根拠と合わせて裏付けた段落の出典は資料の根拠。質問は検証した
    根拠（evidence_ids）に数えない。
    """
    from rag_engine.evaluation.answer_validation import QUESTION_SOURCE, is_premise_id, question_items

    seen: dict = {}

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        seen.update(payload=payload, system=system)
        question, document = payload["evidence_items"]
        ids = [item["id"] for item in payload["answer_passages"]]

        def check(passage_id: str, status: str, evidence_id: str) -> dict:
            return {
                "answer_quote": "段落",
                "answer_passage_id": passage_id,
                "status": status,
                "evidence_id": evidence_id,
                "source_id": "",
                "evidence_quote": "",
                "reason": "判定",
            }

        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    check(ids[0], "supported", question["evidence_id"]),
                    check(ids[1], "supported", f"{document['evidence_id']},{question['evidence_id']}"),
                    check(ids[2], "unsupported", ""),
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims(QUESTION_TEXT, QUESTION_ANSWER, QUESTION_EVIDENCE, settings=None)

    question = seen["payload"]["evidence_items"][0]
    assert (question["id"], question["source"], question["text"]) == (QUESTION_ID, QUESTION_SOURCE, QUESTION_TEXT)
    assert QUESTION_SOURCE in seen["system"]
    stated, applied, other = result["claim_checks"]
    assert (stated["status"], stated["source_id"]) == ("supported", QUESTION_ID)
    assert (applied["status"], applied["source_id"]) == ("unassessed", "c1")
    assert other["status"] == "unsupported"
    assert result["evidence_ids"] == ["c1"]
    assert is_premise_id(QUESTION_ID)
    assert question_items("  ") == []


def test_validation_budget_comes_from_the_settings_and_keeps_the_question() -> None:
    """根拠の予算は設定（answer_validation_max_input_bytes）で決まり、切り詰めても先頭の質問の文は残る。"""
    from types import SimpleNamespace

    seen: list = []

    def parse(system, inputs, settings, schema, provider_id=None):
        seen.append(json.loads(inputs)["evidence_items"])
        return ClaimAuditOutput.model_validate({"claim_checks": []})

    evidence = [{"id": f"c{i}", "source": "m.pdf", "page_start": 1, "page_end": 1, "text": "あ" * 1500} for i in range(40)]
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        small = validate_answer_claims("q", "承認は得ていません。", evidence, settings=None)
        large = validate_answer_claims(
            "q", "承認は得ていません。", evidence, settings=SimpleNamespace(answer_validation_max_input_bytes=400_000)
        )
    assert small["evidence_truncated"] is True and seen[0][0]["id"] == QUESTION_ID
    assert large["evidence_truncated"] is False
    assert [item["id"] for item in seen[1]] == [QUESTION_ID, *(f"c{i}" for i in range(40))]


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("set0123456789abcdef:37", True),
        ("set0123…:37", True),
        ("set0123456789abcdef:38", False),
        ("37", False),
        ("set:37", False),
    ],
)
def test_chunk_ids_written_without_the_document_id_point_to_their_evidence(written: str, expected: bool) -> None:
    """先頭の文書の id を落とした「chunk_set:番号」の ID は、後ろの部分にそろえて照合する（番号はそのまま一致）。"""
    from rag_engine.evaluation.answer_eval import _abbreviated_id_matches

    assert _abbreviated_id_matches(written, "doc0123456789:set0123456789abcdef:37") is expected


# ---- 監査の判定の根拠の ID の照合（#2622）。設定の名前・本文・ID はすべて合成した値。----
SETTING_SET = "0f1e2d3c4b5a69788796a5b4c3d2e1f0:" + "a1" * 32
SETTING_OFF = f"{SETTING_SET}:1"
SETTING_NOTE = f"{SETTING_SET}:2"
SETTING_EVIDENCE = [
    {"id": SETTING_OFF, "source": "synthetic-settings.pdf", "page_start": 1, "page_end": 1, "text": "設定Aは無効です。"},
    {"id": SETTING_NOTE, "source": "synthetic-settings.pdf", "page_start": 1, "page_end": 1, "text": "設定Bは毎日 0 時に実行します。"},
]
SETTING_ANSWER = "設定Aは有効です。"
# catalog に無い片段の ID（監査モデルが返した値の形）。
UNKNOWN_SPAN_ID = "E123456789abcdef"


def _setting_catalog() -> dict[str, dict]:
    from rag_engine.evaluation.answer_eval import _evidence_fragments

    return {item["evidence_id"]: item for item in _evidence_fragments(SETTING_EVIDENCE)}


def _span_id(chunk_id: str) -> str:
    return next(item["evidence_id"] for item in _setting_catalog().values() if item["id"] == chunk_id)


def test_contradiction_with_an_unknown_audit_id_stays_contradicted() -> None:
    """監査モデルが catalog に無い evidence_id を返しても、矛盾の判定を citation_error に変えない (#2622)。

    以前は「未登録の原文ID。」の citation_error になり、Agent が「矛盾の判定ではない」として本文を載せていた。
    """
    calls: list[dict] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        calls.append(payload)
        assert [item["id"] for item in _documents(payload)] == [SETTING_OFF, SETTING_NOTE]
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": payload["answer_passages"][0]["id"],
                        "status": "contradicted",
                        "evidence_id": UNKNOWN_SPAN_ID,
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "合成資料は設定Aが無効と記載している。",
                    }
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("設定Aは有効ですか？", SETTING_ANSWER, SETTING_EVIDENCE, settings=None)
    assert result["counts"] == {"contradicted": 1}
    [claim] = result["claim_checks"]
    assert claim["status"] == claim["audit_status"] == "contradicted"
    assert claim["reference_check"] == "audit_id_invalid"
    # 出典は決めず、監査が返した ID はそのまま残す（変換前の応答を追える）。
    assert (claim["source_id"], claim["evidence_quote"], claim["evidence_id"]) == ("", "", UNKNOWN_SPAN_ID)
    assert claim["reason"].endswith("合成資料は設定Aが無効と記載している。")
    # 否定の判定は確かめ直さない（根拠を絞った確かめ直しで判定を替えない）。
    assert len(calls) == 1 and result["rechecked_passage_ids"] == []


def _bind_one(status: str, answer_quote: str, evidence_id: str, evidence_quote: str = "", *, passage_citations: bool = True):
    from rag_engine.evaluation.answer_eval import _bind_claims

    output = ClaimAuditOutput.model_validate(
        {
            "claim_checks": [
                {
                    "answer_quote": answer_quote,
                    "answer_passage_id": "A1",
                    "status": status,
                    "evidence_id": evidence_id,
                    "source_id": "",
                    "evidence_quote": evidence_quote,
                    "reason": "合成の理由。",
                }
            ]
        }
    )
    passages = [{"id": "A1", "text": answer_quote}]
    [claim] = _bind_claims(output, _setting_catalog(), passages, passage_citations=passage_citations).claim_checks
    return claim


REFERENCE_CASES = [
    # （名前, 段落, 監査の evidence_id, 監査の引用, reference_check）。
    ("正常な ID", SETTING_ANSWER, "{off}", "", "resolved"),
    ("監査の ID だけが無効で、回答の ID で照合", f"設定Aは有効です【証拠 ID {SETTING_OFF}】。", UNKNOWN_SPAN_ID, "", "resolved_by_answer"),
    ("回答側の無効な ID", f"設定Aは有効です【証拠 ID {SETTING_SET}:9】。", "", "", "answer_id_invalid"),
    ("回答側の無効な ID（監査の ID も無効）", f"設定Aは有効です【証拠 ID {SETTING_SET}:9】。", UNKNOWN_SPAN_ID, "", "answer_id_invalid"),
    ("監査モデルだけの無効な ID", SETTING_ANSWER, UNKNOWN_SPAN_ID, "", "audit_id_invalid"),
    ("監査モデルだけの ID でない値", SETTING_ANSWER, "第 3 章", "", "audit_id_invalid"),
    ("曖昧な省略 ID", SETTING_ANSWER, f"{SETTING_SET[:40]}…", "", "ambiguous_id"),
    ("ID の欠落", SETTING_ANSWER, "", "", "audit_id_missing"),
    ("引用が根拠に無い", SETTING_ANSWER, "{off}, {note}", "根拠に無い合成の文", "quote_unmatched"),
]


@pytest.mark.parametrize(
    ("name", "quote", "written", "evidence_quote", "expected"), REFERENCE_CASES, ids=[case[0] for case in REFERENCE_CASES]
)
@pytest.mark.parametrize("status", ["supported", "contradicted"])
def test_reference_checks_separate_audit_id_errors_from_answer_citation_errors(
    status: str, name: str, quote: str, written: str, evidence_quote: str, expected: str
) -> None:
    """根拠の ID の照合の結果を、正常・回答側の無効・監査モデルだけの無効・曖昧な省略・欠落・引用の不一致に分ける (#2622)。

    結び付けられない supported は根拠と一致したと扱わず citation_error、contradicted は矛盾のまま残す。
    """
    evidence_id = written.format(off=_span_id(SETTING_OFF), note=_span_id(SETTING_NOTE))
    claim = _bind_one(status, quote, evidence_id, evidence_quote)
    assert claim.reference_check == expected, name
    assert claim.audit_status == status
    if expected.startswith("resolved"):
        assert (claim.status, claim.source_id) == (status, SETTING_OFF)
    elif status == "supported":
        assert claim.status == "citation_error" and claim.reason.startswith("未登録の原文ID。")
    else:
        assert (claim.status, claim.source_id) == ("contradicted", "")
        # 監査が返した ID はそのまま残す。
        assert claim.evidence_id == evidence_id


def test_model_citation_errors_record_the_reference_check() -> None:
    """モデルが返した citation_error も、ID を照合した結果を残す（判定の変換を追える。#2622）。"""
    resolved = _bind_one("citation_error", f"設定Aは有効です【証拠 ID {SETTING_OFF}】。", "")
    assert (resolved.status, resolved.audit_status, resolved.reference_check) == ("unassessed", "citation_error", "resolved")
    invalid = _bind_one("citation_error", f"設定Aは有効です【証拠 ID {SETTING_SET}:9】。", "")
    assert (invalid.status, invalid.audit_status, invalid.reference_check) == ("citation_error", "citation_error", "answer_id_invalid")
    # 照合しない判定は reference_check が空（監査の判定だけを残す）。
    unsupported = _bind_one("unsupported", SETTING_ANSWER, "")
    assert (unsupported.status, unsupported.audit_status, unsupported.reference_check) == ("unsupported", "unsupported", "")


def test_quality_evaluation_keeps_its_citation_errors_but_records_the_audit_status() -> None:
    """品質評価（passage_citations なし）の判定は今までどおり citation_error にし、元の判定と照合の結果を残す (#2622)。"""
    claim = _bind_one("contradicted", SETTING_ANSWER, UNKNOWN_SPAN_ID, passage_citations=False)
    assert (claim.status, claim.audit_status, claim.reference_check) == ("citation_error", "contradicted", "audit_id_invalid")


def test_diagnostics_are_not_in_the_schema_sent_to_the_model() -> None:
    """診断（audit_status・reference_check）はモデルに渡す schema に出さず、モデルの値も使わない (#2622)。"""
    schema = json.dumps(ClaimAuditOutput.model_json_schema(), ensure_ascii=False)
    assert "audit_status" not in schema and "reference_check" not in schema
    from rag_engine.evaluation.answer_eval import _bind_claims

    output = ClaimAuditOutput.model_validate(
        {
            "claim_checks": [
                {
                    "answer_quote": SETTING_ANSWER,
                    "answer_passage_id": "A1",
                    "status": "contradicted",
                    "evidence_id": UNKNOWN_SPAN_ID,
                    "source_id": "",
                    "evidence_quote": "",
                    "reason": "合成の理由。",
                    "audit_status": "supported",
                    "reference_check": "resolved",
                }
            ]
        }
    )
    passages = [{"id": "A1", "text": SETTING_ANSWER}]
    [claim] = _bind_claims(output, _setting_catalog(), passages, passage_citations=True).claim_checks
    assert (claim.audit_status, claim.reference_check) == ("contradicted", "audit_id_invalid")


def test_unbound_contradictions_are_not_rechecked_and_other_passages_still_are() -> None:
    """ID を書かない段落の矛盾は、出典が決まらなくても確かめ直さずに残し、ほかの段落の確かめ直しは今までどおり (#2622・#1412)。"""
    both = _cite(f"{PLAN}:1", f"{CHANGE}:4")
    result, calls = _window_validation({**WINDOW_FIRST, "A2": ("contradicted", "")}, {"A6": ("supported", both)})
    assert [claim["status"] for claim in result["claim_checks"]] == ["contradicted", "supported", "supported", "unassessed"]
    assert result["claim_checks"][0]["reference_check"] == "audit_id_missing"
    assert [passage["id"] for passage in calls[1][1]["answer_passages"]] == ["A6"]
    assert result["rechecked_passage_ids"] == ["A6"]


# ---- 非主張の分類の食い違いの監査し直し（#2625） ----------------------------------------------------------
# 同じ内容（合成の「上限は最長 90 日」）を、文・箇条書き・見出しの記法・表の行・例示の形で書いた段落。
LIMIT_EVIDENCE = [{"id": "c1", "source": "manual-a.pdf", "page_start": 1, "page_end": 1, "text": "延長の上限は最長 90 日です。"}]
LIMIT_FORMS = {
    "sentence": "延長の上限は最長 90 日です。",
    "bullet": "- 延長の上限は最長 90 日です。",
    "bold": "**延長の上限は最長 90 日**",
    "markdown_heading": "## 延長の上限は最長 90 日",
    "table_row": "| 延長の上限 | 最長 90 日 |",
    "example": "例: 30 日を超えて延長するときは、最長 90 日まで延長できます。",
}
LIMIT_OWNER = "申請は所属の管理者が行います。"


def _limit_answer(form: str) -> str:
    text = LIMIT_FORMS[form]
    if form == "table_row":
        return f"| 項目 | 内容 |\n| --- | --- |\n{text}"
    return f"{text}\n\n{LIMIT_OWNER}"


def _claim_output(payload: dict, statuses: dict[str, str]) -> ClaimAuditOutput:
    """段落の原文 → 判定（無い段落は返さない）。裏付け・矛盾は最初の根拠の evidence_id を付ける。"""
    evidence_id = _documents(payload)[0]["evidence_id"]
    return ClaimAuditOutput.model_validate(
        {
            "claim_checks": [
                {
                    "answer_quote": "段落",
                    "answer_passage_id": item["id"],
                    "status": statuses[item["text"]],
                    "evidence_id": evidence_id if statuses[item["text"]] in {"supported", "contradicted"} else "",
                    "source_id": "",
                    "evidence_quote": "",
                    "reason": "合成の理由。",
                }
                for item in payload["answer_passages"]
                if item["text"] in statuses
            ]
        }
    )


@pytest.mark.parametrize("form", list(LIMIT_FORMS))
def test_content_rewritten_as_headings_tables_or_examples_is_still_audited(form: str) -> None:
    """同じ内容を見出しの記法・表の行・例示に書き換えても、内容の段落は監査に渡す (#2625)。"""
    seen: list[list[str]] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        seen.append([item["text"] for item in payload["answer_passages"]])
        return _claim_output(payload, {text: "supported" for text in seen[-1]})

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("上限は？", _limit_answer(form), LIMIT_EVIDENCE, settings=None)
    assert LIMIT_FORMS[form] in seen[0]
    assert len(seen) == 1 and result["reaudited_passage_ids"] == []
    assert {claim["classification_check"] for claim in result["claim_checks"]} == {""}


def test_passages_the_model_calls_non_claims_are_reaudited() -> None:
    """モデルが not_a_claim とした本文の段落は、その段落だけを差し戻しの文と同じ根拠で 1 回監査し直し、
    内容の判定（否定を含む）を採る (#2625)。"""
    from rag_engine.evaluation.answer_validation import REAUDIT_FEEDBACK

    bold, example = LIMIT_FORMS["bold"], LIMIT_FORMS["example"]
    answer = f"{bold}\n\n{example}\n\n{LIMIT_OWNER}"
    calls: list[dict] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        calls.append(payload)
        if len(calls) == 1:
            return _claim_output(payload, {bold: "not_a_claim", example: "not_a_claim", LIMIT_OWNER: "unsupported"})
        return _claim_output(payload, {bold: "supported", example: "unsupported"})

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("上限は？", answer, LIMIT_EVIDENCE, settings=None)

    assert len(calls) == 2
    assert "validation_feedback" not in calls[0]
    assert calls[1]["validation_feedback"] == REAUDIT_FEEDBACK
    assert [item["text"] for item in calls[1]["answer_passages"]] == [bold, example]
    assert _documents(calls[1]) == _documents(calls[0])
    claims = {claim["answer_quote"]: claim for claim in result["claim_checks"]}
    assert {quote: claim["status"] for quote, claim in claims.items()} == {
        bold: "unassessed",
        example: "unsupported",
        LIMIT_OWNER: "unsupported",
    }
    assert claims[bold]["source_id"] == "c1"
    assert {quote: claim["classification_check"] for quote, claim in claims.items()} == {
        bold: "reaudited",
        example: "reaudited",
        LIMIT_OWNER: "",
    }
    assert result["reaudited_passage_ids"] == [claims[bold]["answer_passage_id"], claims[example]["answer_passage_id"]]
    assert result["counts"] == {"unassessed": 1, "unsupported": 2}


def test_unresolved_classification_mismatch_stays_unassessed_and_is_traceable() -> None:
    """監査し直しでも内容の判定が返らない段落は unassessed のまま、分類の食い違いとして残す。再試行はしない (#2625)。"""
    bold = LIMIT_FORMS["bold"]
    answer = f"{bold}\n\n{LIMIT_OWNER}"
    calls: list[dict] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        calls.append(payload)
        if len(calls) == 1:
            return _claim_output(payload, {bold: "not_a_claim", LIMIT_OWNER: "supported"})
        return _claim_output(payload, {bold: "not_a_claim"})

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("上限は？", answer, LIMIT_EVIDENCE, settings=None)

    assert len(calls) == 2
    claims = {claim["answer_quote"]: claim for claim in result["claim_checks"]}
    assert claims[bold]["status"] == "unassessed"
    assert (claims[bold]["audit_status"], claims[bold]["classification_check"]) == ("not_a_claim", "non_claim_mismatch")
    assert claims[bold]["reason"].startswith("本文を見出しとして監査から除外できません。")
    assert result["reaudited_passage_ids"] == []
    assert claims[LIMIT_OWNER]["classification_check"] == ""

    def failing(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        if "validation_feedback" in payload:
            raise RuntimeError("合成の失敗")
        return _claim_output(payload, {bold: "not_a_claim", LIMIT_OWNER: "supported"})

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=failing):
        failed = validate_answer_claims("上限は？", answer, LIMIT_EVIDENCE, settings=None)
    assert failed["status"] == "completed"
    assert [claim["classification_check"] for claim in failed["claim_checks"]] == ["non_claim_mismatch", ""]


def test_quality_evaluation_records_the_classification_mismatch() -> None:
    """品質評価の主張の監査（監査し直しはしない）でも、分類の食い違いを残し、モデルの値は使わない (#2625)。"""
    from rag_engine.evaluation.answer_eval import _bind_claims

    passages = [{"id": "A1", "text": "## 根拠"}, {"id": "A2", "text": LIMIT_FORMS["bold"]}]
    output = ClaimAuditOutput.model_validate(
        {
            "claim_checks": [
                {
                    "answer_quote": item["text"],
                    "answer_passage_id": item["id"],
                    "status": "not_a_claim",
                    "evidence_id": "",
                    "source_id": "",
                    "evidence_quote": "",
                    "reason": "見出し",
                    "classification_check": "reaudited",
                }
                for item in passages
            ]
        }
    )
    bound = _bind_claims(output, {}, passages)
    assert [(claim.status, claim.classification_check) for claim in bound.claim_checks] == [
        ("not_a_claim", ""),
        ("unassessed", "non_claim_mismatch"),
    ]


def test_classification_check_is_not_in_the_audit_schema() -> None:
    """分類の照合の結果は監査モデルの schema に出さず、最初の監査の指示は差し戻しの文を含まない (#2625)。"""
    from rag_engine.evaluation.answer_validation import REAUDIT_FEEDBACK, VALIDATION_SYSTEM_PROMPT

    assert "classification_check" not in json.dumps(ClaimAuditOutput.model_json_schema())
    assert REAUDIT_FEEDBACK not in VALIDATION_SYSTEM_PROMPT


# ---- 図の見た目だけで決まる主張（#2678）。合成の判定フロー図の根拠（OCR の文字に矢印・配置が無い）。----

FLOW_TEXT = {"id": "t1", "source": "manual.pdf", "page_start": 1, "page_end": 1, "text": "申請は判定の後に処理する。"}
FLOW_FIGURE = {
    "id": "f1",
    "source": "manual.pdf",
    "page_start": 2,
    "page_end": 2,
    "evidence_type": "ocr",
    "text": "開始 判定 処理A 処理B 終了",
}
FLOW_ANSWER = (
    "判定の後に処理します。\n根拠：manual.pdf p.1\n\n"
    "判定の右の分岐は処理Aにつながります。\n根拠：manual.pdf p.2\n\n"
    "処理Bは左の分岐に属します。\n根拠：manual.pdf p.2"
)


def _figure_validation(statuses: dict[int, tuple[str, str]], recheck: dict[int, str] | None = None, evidence: list | None = None):
    """段落の番号 → (判定, 根拠〔t=本文 / f=図 / ""〕) の監査のスタブで検証し、結果とモデルへの入力を返す。

    ``recheck`` は図の項目だけの supported を確かめ直す呼び出しへの、段落の番号 → 判定（図の項目を根拠にする）。
    """
    seen: dict = {"calls": []}
    texts = {"t1": FLOW_TEXT["text"], "f1": FLOW_FIGURE["text"]}

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        evidence = _documents(payload)
        seen["calls"].append(payload)
        if "validation_feedback" not in payload:
            seen["evidence"] = evidence
            seen["system"] = system
            seen["passages"] = [passage["id"] for passage in payload["answer_passages"]]
        ids = {item["id"]: item["evidence_id"] for item in evidence}
        checks = []
        for passage in payload["answer_passages"]:
            index = seen["passages"].index(passage["id"])
            status, source = statuses[index] if "validation_feedback" not in payload else ((recheck or {})[index], "f")
            chunk = {"t": "t1", "f": "f1", "": ""}[source]
            checks.append(
                {
                    "answer_quote": "段落",
                    "answer_passage_id": passage["id"],
                    "status": status,
                    "evidence_id": ids.get(chunk, ""),
                    "source_id": "",
                    "evidence_quote": texts.get(chunk, "") if status == "supported" else "",
                    "claim_target": "判定の後の処理",
                    "evidence_target": "判定の後の処理",
                    "applicability": "matched",
                    "reason": "合成の理由。",
                }
            )
        return ClaimAuditOutput.model_validate({"claim_checks": checks})

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("判定の後は？", FLOW_ANSWER, evidence or [FLOW_TEXT, FLOW_FIGURE], settings=None)
    return result, seen


def test_figure_only_claims_are_bound_to_the_figure_evidence() -> None:
    """図の見た目だけで決まる主張は figure_only のまま図の根拠を出典にする（画像で確かめたかは呼び出し側。#2678）。"""
    result, seen = _figure_validation({0: ("supported", "t"), 1: ("figure_only", "f"), 2: ("figure_only", "f")})
    # 図の根拠にだけ種類を渡す（本文の根拠には付けない）。
    assert [item.get("evidence_type") for item in seen["evidence"]] == [None, "ocr"]
    assert "figure_only" in seen["system"] and "evidence_type" in seen["system"]
    claims = result["claim_checks"]
    assert [claim["status"] for claim in claims] == ["supported", "figure_only", "figure_only"]
    assert [claim["source_id"] for claim in claims] == ["t1", "f1", "f1"]
    assert claims[1]["reference_check"] == "resolved" and claims[1]["audit_status"] == "figure_only"
    assert result["counts"] == {"supported": 1, "figure_only": 2}


def test_figure_only_claims_that_do_not_point_to_a_figure_are_unsupported() -> None:
    """figure_only の根拠が図ではない・決まらず、図に結び付けられないときは unsupported にする (#2678)。"""
    second_figure = {**FLOW_FIGURE, "id": "f2", "text": "別の図の文字"}
    result, _seen = _figure_validation(
        {0: ("supported", "t"), 1: ("figure_only", "t"), 2: ("figure_only", "")}, evidence=[FLOW_TEXT, FLOW_FIGURE, second_figure]
    )
    claims = result["claim_checks"]
    assert [(claim["status"], claim["audit_status"]) for claim in claims[1:]] == [
        ("unsupported", "figure_only"),
        ("unsupported", "figure_only"),
    ]
    assert claims[1]["reference_check"] == "not_figure" and claims[1]["source_id"] == ""
    assert claims[2]["reference_check"] == "audit_id_missing"


def test_figure_only_claims_with_unresolved_ids_take_the_only_figure() -> None:
    """渡した根拠に図が 1 つだけなら、ID を写し損ねた figure_only もその図に結び付ける (#2678)。"""
    result, _seen = _figure_validation({0: ("supported", "t"), 1: ("figure_only", "t"), 2: ("figure_only", "")})
    claims = result["claim_checks"]
    assert [(claim["status"], claim["source_id"], claim["reference_check"]) for claim in claims[1:]] == [
        ("figure_only", "f1", "single_figure"),
        ("figure_only", "f1", "single_figure"),
    ]


def test_claims_judged_from_the_figure_text_stay_as_judged() -> None:
    """正しい場合は変えない: 図の本文が明示する内容は、確かめ直しでも supported なら図の根拠のまま残す (#2678)。"""
    result, seen = _figure_validation({0: ("supported", "t"), 1: ("supported", "f"), 2: ("contradicted", "f")}, recheck={1: "supported"})
    assert [(claim["status"], claim["source_id"]) for claim in result["claim_checks"]] == [
        ("supported", "t1"),
        ("supported", "f1"),
        ("contradicted", "f1"),
    ]
    assert result["figure_rechecked_passage_ids"] == []
    assert len(seen["calls"]) == 2


def test_supported_claims_backed_only_by_a_figure_are_rechecked() -> None:
    """図の項目だけを根拠に supported とした段落は、その段落と図の項目だけで確かめ直し、figure_only なら替える (#2678)。"""
    from rag_engine.evaluation.answer_validation import FIGURE_RECHECK_FEEDBACK

    result, seen = _figure_validation(
        {0: ("supported", "t"), 1: ("supported", "f"), 2: ("supported", "f")}, recheck={1: "figure_only", 2: "unsupported"}
    )
    first, second = seen["calls"]
    assert "validation_feedback" not in first
    # 確かめ直しは本文の根拠で裏付けた段落を渡さず、根拠は図の項目（と質問の文）だけ。
    assert second["validation_feedback"] == FIGURE_RECHECK_FEEDBACK
    assert [passage["id"] for passage in second["answer_passages"]] == seen["passages"][1:]
    assert [item["id"] for item in _documents(second)] == ["f1"]
    claims = result["claim_checks"]
    # figure_only は採り、unsupported は採らない（図の項目に絞った確かめ直しで外す理由を増やさない）。
    assert [(claim["status"], claim["support_check"]) for claim in claims] == [
        ("supported", ""),
        ("figure_only", "figure_recheck"),
        ("supported", ""),
    ]
    assert claims[1]["source_id"] == "f1"
    assert result["figure_rechecked_passage_ids"] == [seen["passages"][1]]


def test_claims_backed_by_text_evidence_are_not_rechecked() -> None:
    """本文の根拠で裏付けた supported・figure_only の判定だけなら、確かめ直しのモデルは呼ばない (#2678)。"""
    result, seen = _figure_validation({0: ("supported", "t"), 1: ("figure_only", "f"), 2: ("unsupported", "")})
    assert len(seen["calls"]) == 1
    assert result["figure_rechecked_passage_ids"] == []


def test_figure_only_is_only_in_the_validation_schema() -> None:
    """figure_only は最終の検証の判定だけにあり、品質評価の判定の schema には出さない。図の種類は ID を変えない (#2678)。"""
    from rag_engine.evaluation.answer_eval import AnswerEvaluationOutput, _evidence_fragments

    assert "figure_only" in json.dumps(ClaimAuditOutput.model_json_schema())
    assert "figure_only" not in json.dumps(AnswerEvaluationOutput.model_json_schema())
    [fragment] = list(_evidence_fragments([FLOW_FIGURE]))
    bare = {key: value for key, value in FLOW_FIGURE.items() if key != "evidence_type"}
    [plain] = list(_evidence_fragments([bare]))
    assert fragment["evidence_type"] == "ocr" and "evidence_type" not in plain
    assert plain["evidence_id"] == fragment["evidence_id"]


def test_nested_item_label_and_extra_citation_do_not_pollute_counts() -> None:
    """父ラベルと、監査が余分に返した出典の行は判定の集計に入れない。"""
    label = "- 照合すべき項目"
    fact = EVIDENCE[0]["text"]
    citation = "出典: manual.pdf p.1"
    answer = f"{label}\n  - {fact}\n{citation}"
    calls = []

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        calls.append(payload)
        output = _claim_output(payload, {f"- {fact}": "supported", label: "not_a_claim"})
        output.claim_checks.append(
            output.claim_checks[0].model_copy(
                update={
                    "answer_passage_id": "",
                    "answer_quote": citation,
                    "status": "unsupported",
                }
            )
        )
        return output

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("有効期限は？", answer, EVIDENCE, settings=None)
    assert len(calls) == 1
    assert [p["text"] for p in calls[0]["answer_passages"]] == [f"- {fact}"]
    assert result["counts"] == {"supported": 1}
    assert result["reaudited_passage_ids"] == []
    assert [c["answer_quote"] for c in result["claim_checks"]] == [f"- {fact}"]


@pytest.mark.parametrize("quote", ["対象の状 態を確認します。", "**対象の状態**を確認します。", "対象の`状態`を確認します。"])
def test_support_quote_accepts_pdf_spaces_and_balanced_markdown(quote: str) -> None:
    fact = "対象の状態を確認します。"

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        output = _claim_output(payload, {fact: "supported"})
        output.claim_checks[0].evidence_quote = quote
        return output

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims(
            "対象は？",
            fact + "\n根拠：manual.pdf p.1",
            [{"id": "c1", "source": "manual.pdf", "page_start": 1, "text": "対象の状 態を確認します。"}],
            settings=None,
        )
    assert result["counts"] == {"supported": 1}
    assert result["claim_checks"][0]["support_check"] == ""


@pytest.mark.parametrize(
    ("quote", "text"),
    [
        ("値は **43** です。", "値は 42 です。"),
        ("対象の状態を確認します。", "別の記録を保存します。"),
        ("式は 23 です。", "式は 2*3 です。"),
        ("値は 23 です。", "値は 2**3 です。"),
    ],
)
def test_quote_format_normalization_does_not_accept_changed_content(quote: str, text: str) -> None:
    from rag_engine.generation.grounded import quote_in_text

    assert not quote_in_text(quote, text)


def test_other_chapter_table_is_not_supported_when_scope_does_not_apply() -> None:
    fact = "第1章の値は 42 です。"

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        document = _documents(payload)[0]
        assert document["section_path"] == ["第2章 範囲 B", "集計"]
        output = _claim_output(payload, {fact: "supported"})
        output.claim_checks[0].applicability = "not_applicable"
        return output

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims(
            "第1章の値は？", fact, [{"id": "c1", "text": "値は42です。", "section_path": ["第2章 範囲 B", "集計"]}], settings=None
        )
    assert result["counts"] == {"unsupported": 1}
    assert result["claim_checks"][0]["support_check"] == "target_mismatch"


@pytest.mark.parametrize("case", HEADING_CASES["table_header_cases"], ids=lambda case: case["id"])
def test_table_header_contract_with_empty_cells(case: dict) -> None:
    from rag_engine.generation.operation_audit import is_non_claim_passage, table_header_lines

    headers = table_header_lines(case["answer"])
    assert headers == set(case["headers"])
    for header in headers:
        assert is_non_claim_passage(header, table_headers=headers)
    assert not is_non_claim_passage("| 値 | 10 | 20 |", table_headers=headers)
