"""回答の最終の検証（#1246）。根拠は今の権限と版で読み直し、主張の監査（モデル）はスタブ。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
from opentelemetry import trace as otel_trace
from pytest import MonkeyPatch

from app.clients import support_guide_store
from app.config import get_settings
from app.mcp import tools as mcp_tools
from app.rag.answer_checks import (
    AnswerFinding,
    check_guide_steps,
    check_impact,
    check_requests,
    excluded_steps,
    impact_applies,
    instruction_text,
)
from app.rag.answer_validation import (
    AnswerValidation,
    EvidenceRef,
    GuideCheckRef,
    GuideProfileNotFoundError,
    is_valid,
    validate_answer,
    validation_reuse_context,
)
from app.schemas.search import RetrievedChunk
from app.schemas.support_guide import SupportGuideContent
from tests.test_mcp_api import _call, _token, auth  # noqa: F401 - fixture を使う

SUPPORT_GUIDES = (
    Path(__file__).resolve().parents[2] / "evaluation/business-support/support-guides.json"
)


def test_validity_rules() -> None:
    assert is_valid("completed", {"supported": 2, "data_confirmation": 1}, unreadable=0)
    assert not is_valid("completed", {"supported": 2, "unsupported": 1}, unreadable=0)
    assert not is_valid("completed", {"supported": 1, "contradicted": 1}, unreadable=0)
    assert not is_valid("completed", {"supported": 1, "unassessed": 1}, unreadable=0)
    assert not is_valid("completed", {"supported": 1}, unreadable=1)
    assert not is_valid("completed", {"data_confirmation": 1}, unreadable=0)
    assert not is_valid("input_too_large", {}, unreadable=0)
    # 図の見た目だけで決まる主張は根拠の本文では確かめられない（#2678）。
    assert not is_valid("completed", {"supported": 1, "figure_only": 1}, unreadable=0)


async def test_validate_answer_marks_figure_evidence(monkeypatch: MonkeyPatch) -> None:
    """図の根拠にだけ種類を付けて監査に渡し、figure_only は検証に通さない（#2678）。"""
    captured: dict[str, Any] = {}
    oracle = _Oracle()
    oracle.chunks[("d1", "f1")] = RetrievedChunk(
        document_id="d1",
        chunk_id="f1",
        text="開始 判定 処理A 処理B 終了",
        score=0.5,
        file_name="manual.pdf",
        metadata={"page_start": 2, "page_end": 2, "content_kind": "figure"},
    )
    oracle.chunks[("d1", "f2")] = RetrievedChunk(
        document_id="d1",
        chunk_id="f2",
        text="図の中の文字",
        score=0.5,
        file_name="manual.pdf",
        metadata={"page_start": 3, "content_kind": "figure", "figure_text_source": "ocr"},
    )

    def fake_claims(
        question: str, answer: str, items: list[dict[str, Any]], settings: Any, **_options: Any
    ) -> dict[str, Any]:
        captured["items"] = items
        return {
            "status": "completed",
            "counts": {"supported": 1, "figure_only": 1},
            "claim_checks": [
                {"answer_quote": "30 日。", "status": "supported", "source_id": "c1"},
                {"answer_quote": "右へ進む。", "status": "figure_only", "source_id": "f1"},
            ],
        }

    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)
    refs = [EvidenceRef("d1", "c1"), EvidenceRef("d1", "f1"), EvidenceRef("d1", "f2")]
    result = await validate_answer(
        "q", "30 日。右へ進む。", refs, get_settings(), oracle=cast(Any, oracle)
    )
    assert [item.get("evidence_type") for item in captured["items"]] == [
        None,
        "figure_description",
        "ocr",
    ]
    assert result.valid is False


def test_mcp_validate_answer_returns_figure_only_claims(
    auth: Any,  # noqa: F811
    monkeypatch: MonkeyPatch,
) -> None:
    """figure_only は図の chunk_id を、図でない根拠を指した判定は照合の結果を返す（#2678）。"""

    async def fake_validate(
        question: str, answer: str, refs: list[EvidenceRef], settings: Any, **kwargs: Any
    ) -> AnswerValidation:
        return AnswerValidation(
            status="completed",
            valid=False,
            counts={"figure_only": 1, "unsupported": 1},
            claim_checks=[
                {
                    "answer_quote": "右へ進む。",
                    "status": "figure_only",
                    "source_id": "f1",
                    "reason": "図の配置",
                    "audit_status": "figure_only",
                    "reference_check": "resolved",
                },
                {
                    "answer_quote": "左は終了。",
                    "status": "unsupported",
                    "source_id": "",
                    "reason": "図の根拠に結び付けられない",
                    "audit_status": "figure_only",
                    "reference_check": "not_figure",
                },
            ],
        )

    monkeypatch.setattr(mcp_tools, "validate_answer", fake_validate)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    body = _call(
        "rag_validate_answer",
        {
            "query": "q",
            "answer": "右へ進む。左は終了。",
            "evidence": [{"document_id": "d1", "chunk_id": "f1"}],
        },
        _token(user.user_uuid),
    )["structuredContent"]
    figure, unbound = body["claims"]
    assert (figure["status"], figure["chunk_id"], figure["audit_status"]) == (
        "figure_only",
        "f1",
        "figure_only",
    )
    assert (unbound["status"], unbound["reference_check"]) == ("unsupported", "not_figure")


class _Oracle:
    def __init__(self) -> None:
        self.chunks = {
            ("d1", "c1"): RetrievedChunk(
                document_id="d1",
                chunk_id="c1",
                text="有効期限は最長 30 日です。",
                score=0.5,
                file_name="manual.pdf",
                metadata={"page_start": 1, "page_end": 1},
            )
        }
        self.stale = {("d1", "c-old")}

    async def retrievable_chunk(self, document_id: str, chunk_id: str) -> Any:
        return self.chunks.get((document_id, chunk_id))

    async def accessible_chunk_exists(self, document_id: str, chunk_id: str) -> bool:
        return (document_id, chunk_id) in self.stale


async def test_validate_answer_rereads_evidence_and_reports_unreadable(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_claims(
        question: str, answer: str, items: list[dict[str, Any]], settings: Any, **_options: Any
    ) -> dict[str, Any]:
        captured["items"] = items
        return {
            "status": "completed",
            "counts": {"supported": 1},
            "claim_checks": [
                {
                    "answer_quote": answer,
                    "status": "supported",
                    "source_id": "c1",
                    "reason": "根拠に記載",
                }
            ],
            "evidence_truncated": False,
        }

    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)
    refs = [
        EvidenceRef("d1", "c1"),
        EvidenceRef("d1", "c-old"),
        EvidenceRef("d9", "x"),
        EvidenceRef("d1", "c1"),
    ]
    result = await validate_answer(
        "q", "有効期限は 30 日です。", refs, get_settings(), oracle=cast(Any, _Oracle())
    )
    # サーバーが読み直した本文だけを監査に渡す（重複は 1 回）。
    assert [item["text"] for item in captured["items"]] == ["有効期限は最長 30 日です。"]
    assert result.stale_evidence == [EvidenceRef("d1", "c-old")]
    assert result.missing_evidence == [EvidenceRef("d9", "x")]
    assert result.valid is False  # 読めない根拠がある

    none = await validate_answer(
        "q", "a", [EvidenceRef("d9", "x")], get_settings(), oracle=cast(Any, _Oracle())
    )
    assert (none.status, none.valid) == ("no_evidence", False)


@pytest.mark.usefixtures("auth")
def test_mcp_validate_answer_returns_reference_failure_diagnostics(
    auth: Any,  # noqa: F811
    monkeypatch: MonkeyPatch,
) -> None:
    """監査の応答の参照の誤りの判定だけ、監査が写した原文と返した ID を上限の字数で返し、
    監査し直しの結果を返す（#112）。"""
    long_quote = "一般社員の宿泊費の上限は\n1泊12,000円" + "あ" * 300

    async def fake_validate(
        question: str, answer: str, refs: list[EvidenceRef], settings: Any, **kwargs: Any
    ) -> AnswerValidation:
        return AnswerValidation(
            status="completed",
            valid=False,
            counts={"citation_error": 1, "supported": 2},
            claim_checks=[
                {
                    "answer_quote": "上限は 12,000円。",
                    "status": "citation_error",
                    "source_id": "c1",
                    "reason": "裏付けとして写した原文が、結び付けた根拠に見当たりません。",
                    "audit_status": "supported",
                    "reference_check": "quote_unmatched",
                    "support_check": "quote_unmatched",
                    "support_quote": long_quote,
                    "audit_evidence_id": "E02284da54382ec31f90d",
                    "reference_reaudit": "rejected",
                },
                # 監査し直しで照合を通った判定は、誤りではないので診断を返さない。
                {
                    "answer_quote": "課長以上は 15,000円。",
                    "status": "supported",
                    "source_id": "c1",
                    "reason": "表に記載",
                    "audit_status": "supported",
                    "reference_check": "resolved",
                    "support_quote": "課長以上 15,000円",
                    "audit_evidence_id": "E02284da54382ec31f90d",
                    "reference_reaudit": "adopted",
                },
                {
                    "answer_quote": "日当は 2,000円。",
                    "status": "supported",
                    "source_id": "c1",
                    "reason": "表に記載",
                    "audit_status": "supported",
                    "reference_check": "resolved",
                    "support_quote": "一般社員 2,000円",
                },
            ],
        )

    monkeypatch.setattr(mcp_tools, "validate_answer", fake_validate)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    body = _call(
        "rag_validate_answer",
        {
            "query": "q",
            "answer": "上限は 12,000円。課長以上は 15,000円。日当は 2,000円。",
            "evidence": [{"document_id": "d1", "chunk_id": "c1"}],
        },
        _token(user.user_uuid),
    )["structuredContent"]
    failed, adopted, plain = body["claims"]
    assert (
        failed["support_quote"]
        == ("一般社員の宿泊費の上限は 1泊12,000円" + "あ" * 300)[: mcp_tools.CLAIM_DIAGNOSTIC_CHARS]
    )
    assert len(failed["support_quote"]) == mcp_tools.CLAIM_DIAGNOSTIC_CHARS == 200
    assert (failed["audit_evidence_id"], failed["reference_reaudit"]) == (
        "E02284da54382ec31f90d",
        "rejected",
    )
    assert (adopted["support_quote"], adopted["audit_evidence_id"]) == (None, None)
    assert adopted["reference_reaudit"] == "adopted"
    assert (plain["support_quote"], plain["audit_evidence_id"], plain["reference_reaudit"]) == (
        None,
        None,
        None,
    )


@pytest.mark.usefixtures("auth")
def test_mcp_validate_answer(auth: Any, monkeypatch: MonkeyPatch) -> None:  # noqa: F811
    async def fake_validate(
        question: str, answer: str, refs: list[EvidenceRef], settings: Any, **kwargs: Any
    ) -> AnswerValidation:
        assert [ref.chunk_id for ref in refs] == ["c1"]
        # MCP の照合は回答フローと同じ「資料との照合」の工程の span の中で動く（#3470）。
        current = otel_trace.get_current_span()
        assert getattr(current, "attributes", {}).get("pr.stage") == "資料との照合"
        # 任意の入力が無ければ、決定的な検査はしない（#1276）。
        assert (kwargs["requests"], kwargs["gaps"], kwargs["guide"]) == (None, [], None)
        return AnswerValidation(
            status="completed",
            valid=False,
            counts={"supported": 1, "unsupported": 1},
            claim_checks=[
                {
                    "answer_quote": "30 日です。",
                    "answer_passage_id": "A1",
                    "status": "supported",
                    "source_id": "c1",
                    "reason": "記載",
                },
                {
                    "answer_quote": "延長は 90 日。",
                    "status": "unsupported",
                    "source_id": "",
                    "reason": "記載なし",
                },
                # 根拠の ID を照合できなかった矛盾（#2622）。監査の判定と照合の結果を返す。
                {
                    "answer_quote": "設定Aは有効。",
                    "status": "contradicted",
                    "source_id": "",
                    "reason": "記載と食い違う",
                    "audit_status": "contradicted",
                    "reference_check": "audit_id_invalid",
                },
                # 監査が見出しとした本文の段落（#2625）。監査し直しても決まらなければ unassessed。
                {
                    "answer_quote": "上限は 90 日。",
                    "status": "unassessed",
                    "source_id": "",
                    "reason": "本文を見出しとして監査から除外できません。",
                    "audit_status": "not_a_claim",
                    "classification_check": "non_claim_mismatch",
                },
                # 監査は supported としたが、根拠が別の対象の記載なので採らなかった段落（#2653）。
                {
                    "answer_quote": "画面 B で値を直す。",
                    "status": "unsupported",
                    "source_id": "c1",
                    "reason": "根拠は段落・質問と別の対象についての記載です。",
                    "audit_status": "supported",
                    "reference_check": "resolved",
                    "support_check": "target_mismatch",
                },
            ],
        )

    monkeypatch.setattr(mcp_tools, "validate_answer", fake_validate)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    body = _call(
        "rag_validate_answer",
        {
            "query": "q",
            "answer": "30 日です。延長は 90 日。",
            "evidence": [{"document_id": "d1", "chunk_id": "c1"}],
        },
        _token(user.user_uuid),
    )["structuredContent"]
    assert body["valid"] is False
    assert body["schema_version"] == mcp_tools.MCP_OUTPUT_SCHEMA_VERSION
    assert (body["checks"], body["findings"], body["guide_revision"]) == ([], [], None)
    assert [claim["status"] for claim in body["claims"]] == [
        "supported",
        "unsupported",
        "contradicted",
        "unassessed",
        "unsupported",
    ]
    # 裏付けを採らなかった理由（#2659）。採った・理由の無い判定は null。
    assert [claim["support_check"] for claim in body["claims"]] == [None] * 4 + ["target_mismatch"]
    assert body["claims"][4]["audit_status"] == "supported"
    assert body["claims"][0]["chunk_id"] == "c1" and body["claims"][1]["chunk_id"] is None
    assert body["claims"][0]["classification_check"] is None
    assert body["claims"][3]["classification_check"] == "non_claim_mismatch"
    # 診断の無い判定は null、照合できなかった矛盾は監査の判定と照合の結果を返す（#2622）。
    assert (body["claims"][0]["audit_status"], body["claims"][0]["reference_check"]) == (
        None,
        None,
    )
    assert body["claims"][2]["chunk_id"] is None
    assert (body["claims"][2]["audit_status"], body["claims"][2]["reference_check"]) == (
        "contradicted",
        "audit_id_invalid",
    )
    # 回答の中の段落の位置（#3240）: 段落の ID の位置、ID が無ければ回答に 1 回だけ現れる位置。
    # 回答に無い原文の段落は null。
    assert [claim["answer_range"] for claim in body["claims"]] == [
        {"start": 0, "end": 7},
        {"start": 7, "end": 16},
        None,
        None,
        None,
    ]
    chatter = auth.user_with_permissions("chatter", ["menu.chat"])
    denied = _call(
        "rag_validate_answer",
        {"query": "q", "answer": "a", "evidence": [{"document_id": "d1", "chunk_id": "c1"}]},
        _token(chatter.user_uuid),
    )
    assert denied["isError"] is True


# ---- 決定的な検査（#1276）。業務ガイドは架空の「サンプル業務ポータル」の例だけを使う。----


def _guide(**overrides: Any) -> SupportGuideContent:
    payload: dict[str, Any] = {
        "title": "アクセス権限を付与する",
        "goal": {"expected_result": "対象の利用者が機能を使える", "match_terms": ["権限を付与"]},
        "conditions": [
            {
                "id": "target",
                "label": "付与先",
                "allowed_values": ["個別", "グループ"],
                "question": "権限は個別の利用者とグループのどちらに付けますか？",
            }
        ],
        "steps": [
            {"id": "open", "title": "権限タブを開く"},
            {"id": "user", "title": "利用者を選ぶ", "depends_on": ["open"]},
            {"id": "group", "title": "グループを選ぶ", "depends_on": ["open"]},
            {"id": "group_save", "title": "グループの設定を保存する", "depends_on": ["group"]},
            {"id": "verify", "title": "付与結果を確認する", "depends_on": ["user", "group_save"]},
        ],
        "branches": [
            {
                "id": "b1",
                "when": {"condition_id": "target", "values": ["個別"]},
                "goto_step": "user",
            },
            {
                "id": "b2",
                "when": {"condition_id": "target", "values": ["グループ"]},
                "goto_step": "group",
            },
        ],
        "impact": {"scope": "individual"},
    }
    payload.update(overrides)
    return SupportGuideContent.model_validate(payload)


def _codes(findings: list[AnswerFinding]) -> list[tuple[str, str]]:
    return [(item.code, item.severity) for item in findings]


def test_check_requests_reports_silent_and_disclosed_gaps() -> None:
    requests = [
        {"id": "Q1", "text": "期限", "status": "addressed"},
        {"id": "Q2", "text": "例外の扱い", "status": "missing"},
        {"id": "Q3", "text": "申請先", "status": "partial"},
        {"id": "Q4", "text": "様式", "status": "unknown"},
    ]
    silent = check_requests("期限は 30 日です。", requests)
    assert _codes(silent) == [
        ("request_missing", "error"),
        ("request_partial", "error"),
        ("request_unverified", "warning"),
    ]
    assert [item.request_id for item in silent] == ["Q2", "Q3", "Q4"]
    # 本文の不足の節・要求の文・渡した gaps の文のどれかで示していれば warning。
    section = "期限は 30 日です。\n\n資料から確認できなかった事項\n\n・例外"
    assert {item.severity for item in check_requests(section, requests[1:3])} == {"warning"}
    named = check_requests("例外の扱いは資料にありません。", requests[1:2])
    assert _codes(named) == [("request_missing", "warning")]
    gap = check_requests("申請先の窓口は不明です。", requests[2:3], ["申請先の窓口は不明"])
    assert _codes(gap) == [("request_partial", "warning")]
    assert check_requests("答え", []) == []


def test_excluded_steps_follow_known_branch_conditions() -> None:
    guide = _guide()
    # 条件が分からなければ、どの分岐も除かない。
    assert excluded_steps(guide, {}) == set()
    # 個別なら、グループの分岐の手順とそれだけに依存する手順を除く（確認は利用者の分岐にも依存）。
    assert excluded_steps(guide, {"target": "個別"}) == {"group", "group_save"}
    assert excluded_steps(guide, {"target": " ｸﾞﾙｰﾌﾟ "}) == {"user"}
    # どの分岐にも当たらない値では分岐を決められないので、除かない（#1320）。
    assert excluded_steps(guide, {"target": "全員"}) == set()


def test_check_guide_steps_order_branch_and_omissions() -> None:
    guide = _guide()
    ordered = "1. 権限タブを開く\n2. 利用者を選ぶ\n3. 付与結果を確認する"
    assert check_guide_steps(ordered, guide, {"target": "個別"}) == []

    reversed_order = "1. 利用者を選ぶ\n2. 権限タブを開く\n3. 付与結果を確認する"
    findings = check_guide_steps(reversed_order, guide, {"target": "個別"})
    assert _codes(findings) == [("step_order", "error")]
    assert (findings[0].step_id, findings[0].related_step_id) == ("user", "open")

    wrong = "1. 権限タブを開く\n2. グループを選ぶ\n3. 付与結果を確認する"
    findings = check_guide_steps(wrong, guide, {"target": "個別"})
    assert ("step_wrong_branch", "error") in _codes(findings)
    assert [item.step_id for item in findings if item.code == "step_wrong_branch"] == ["group"]

    # 前の手順・後の手順の欠落は warning。
    missing = check_guide_steps("利用者を選ぶ", guide, {"target": "個別"})
    assert _codes(missing) == [
        ("step_dependency_missing", "warning"),
        ("step_following_missing", "warning"),
    ]
    assert [item.step_id for item in missing] == ["user", "verify"]

    unmatched = check_guide_steps("設定画面で操作してください。", guide, {})
    assert _codes(unmatched) == [("guide_steps_unmatched", "warning")]
    assert check_guide_steps("何か", _guide(steps=[], branches=[]), {}) == []


def _delete_guide() -> SupportGuideContent:
    """評価セットの業務ガイド「アカウントの削除」（承認を得ていなければ承認の手順へ分岐）。"""
    data = json.loads(SUPPORT_GUIDES.read_text(encoding="utf-8"))
    payload = next(item for item in data["guides"] if item["title"] == "アカウントの削除")
    return SupportGuideContent.model_validate(payload)


# 評価 e1467 の D の mt-delete-account-approved（2 回目の発言「承認はもらっています」）の
# 回答の一部。
_DELETE_APPROVED_ANSWER = """部門長の承認は得ている、という前提で、資料に沿ってお示しします。

**削除の前提（満たしている条件）**
- 削除の前に、所属部署の部門長の承認を得る必要があり、承認の無い削除は行いません。\
→ 承認は得ているとのことなので、この前提は満たしています。
- 削除したアカウントは 30 日後に完全に消去され、復元できません。

**資料で確かめられなかった点**
- 実際にアカウントを削除する操作そのものの手順は、資料には記載が見当たらず、\
確認できません。"""


def test_check_guide_steps_ignores_a_premise_that_is_already_met() -> None:
    # 「承認を得る」を満たした前提として述べただけで、承認の手順を実行させていない。承認済みの
    # 条件に当たらない分岐の手順（step_wrong_branch の error）にしない（#1491）。
    guide = _delete_guide()
    findings = check_guide_steps(_DELETE_APPROVED_ANSWER, guide, {"approved": "はい"})
    assert _codes(findings) == [("guide_steps_unmatched", "warning")]
    # 見出しの無い行でも、前提を満たしたと述べた行は数えない。
    line = "所属部署の部門長の承認を得る（承認済み）。続いて削除を依頼してください。"
    codes = [item.code for item in check_guide_steps(line, guide, {"approved": "はい"})]
    assert "step_wrong_branch" not in codes


def test_check_guide_steps_still_flags_an_instruction_of_the_wrong_branch() -> None:
    # 承認済みなのに承認の手順を実行させる指示は、今までどおり error。
    guide = _delete_guide()
    answer = "1. まず所属部署の部門長の承認を得る\n2. 承認後に削除してください。"
    findings = check_guide_steps(answer, guide, {"approved": "はい"})
    assert ("step_wrong_branch", "error") in _codes(findings)
    # 満たした前提の節は、空行の後の箇条書きではない行で終わる（その後の指示は数える）。
    sectioned = (
        "**前提（満たしている条件）**\n- 部門長の承認\n\n"
        "所属部署の部門長の承認を得るよう依頼してください。"
    )
    assert ("step_wrong_branch", "error") in _codes(
        check_guide_steps(sectioned, guide, {"approved": "はい"})
    )
    assert instruction_text(sectioned) == "所属部署の部門長の承認を得るよう依頼してください。"


def test_check_impact_requires_scope_and_approval() -> None:
    assert check_impact("権限タブを開く", _guide()) == []
    group = _guide(impact={"scope": "group", "approval_required": True})
    assert _codes(check_impact("権限タブを開く", group)) == [
        ("impact_scope_missing", "error"),
        ("approval_missing", "error"),
    ]
    assert check_impact("グループ全員に効きます。部門長の承認を得てから行います。", group) == []
    everyone = _guide(impact={"scope": "all"})
    assert _codes(check_impact("権限タブを開く", everyone)) == [("impact_scope_missing", "error")]
    assert check_impact("すべての利用者に反映されます。", everyone) == []


def _access_guide() -> SupportGuideContent:
    """評価セットの業務ガイド「アクセス権限の付与」（承認はグループに付与する分岐だけ）。"""
    data = json.loads(SUPPORT_GUIDES.read_text(encoding="utf-8"))
    payload = next(item for item in data["guides"] if item["title"] == "アクセス権限の付与")
    return SupportGuideContent.model_validate(payload)


# #1317 の実環境の D（run3）の da-trial-account-setup の回答（個別の利用者の分岐で答えた）。
_TRIAL_SETUP_ANSWER = """**検証用アカウントの登録 → アクセス権限付与 → 通知設定の手順**

1. **検証用アカウントを登録**
   - 管理画面で「利用者」メニューを開き「追加」ボタンを押す。
   - 「利用者種別」から **「検証用」** を選択。
   - 有効期限（最長30日）を入力し、保存する。

2. **アクセス権限を付与**
   - 登録した検証用アカウントの詳細画面を開き、タブから **「権限」** を選択。
   - 必要な権限をチェックし、「付与」ボタンを押す。

3. **通知を設定**
   - 同じ詳細画面の **「通知」** タブを開く。
   - 通知先メールアドレスを入力して保存する。
   - 「テスト送信」ボタンを押し、テストメールが届くことを確認する。"""


def test_check_impact_skips_the_approval_of_a_branch_that_does_not_apply() -> None:
    guide = _access_guide()
    assert guide.impact.steps == ["group-approval", "group"]
    # 個別の利用者の分岐で答えた回答に、グループへの付与の影響範囲・承認を求めない（#1320）。
    assert impact_applies(guide, {"target": "個別"}) is False
    assert check_impact(_TRIAL_SETUP_ANSWER, guide, {"target": "個別"}) == []
    # グループの分岐なら、今までどおり影響範囲と承認を求める。
    assert impact_applies(guide, {"target": "グループ"}) is True
    assert _codes(check_impact(_TRIAL_SETUP_ANSWER, guide, {"target": "グループ"})) == [
        ("impact_scope_missing", "error"),
        ("approval_missing", "error"),
    ]


def test_check_impact_stays_strict_when_the_branch_is_unknown() -> None:
    guide = _access_guide()
    # 付与先が分からない（分岐を決められない）ときは、今までどおり一律に確かめる（安全側）。
    for conditions in ({}, None, {"target": "不明な値"}, {"other": "個別"}):
        assert _codes(check_impact(_TRIAL_SETUP_ANSWER, guide, conditions)) == [
            ("impact_scope_missing", "error"),
            ("approval_missing", "error"),
        ], conditions
    # 係る手順を決めていない業務ガイドは、条件が分かっていてもすべての場合に確かめる。
    whole = _guide(impact={"scope": "group", "approval_required": True})
    assert impact_applies(whole, {"target": "個別"}) is True
    assert _codes(check_impact("権限タブを開く", whole, {"target": "個別"})) == [
        ("impact_scope_missing", "error"),
        ("approval_missing", "error"),
    ]
    # 係る手順の一部でも当たる分岐に残れば確かめる（共通の手順に係るとき）。
    shared = _guide(
        impact={"scope": "group", "approval_required": True, "steps": ["group", "verify"]}
    )
    assert impact_applies(shared, {"target": "個別"}) is True


class _GuideOracle(_Oracle):
    def __init__(self, profiles: set[str]) -> None:
        super().__init__()
        self.profiles = profiles

    async def get_search_answer_profile(self, profile_id: str) -> Any:
        return type("View", (), {"id": profile_id})() if profile_id in self.profiles else None


def _supported(monkeypatch: MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_claims(
        question: str, answer: str, items: list[dict[str, Any]], settings: Any, **_options: Any
    ) -> dict[str, Any]:
        calls.append(answer)
        return {
            "status": "completed",
            "counts": {"supported": 1},
            "claim_checks": [
                {"answer_quote": answer, "status": "supported", "source_id": "c1", "reason": "記載"}
            ],
        }

    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)
    return calls


def _published(monkeypatch: MonkeyPatch, revision: int = 2) -> list[str]:
    seen: list[str] = []

    async def published_contents(self: Any, profile_id: str) -> list[Any]:
        seen.append(profile_id)
        return [("guide-1", revision, _guide(impact={"scope": "group"}))]

    monkeypatch.setattr(
        support_guide_store.SupportGuideStore, "published_contents", published_contents
    )
    return seen


async def test_validate_answer_runs_deterministic_checks(monkeypatch: MonkeyPatch) -> None:
    calls = _supported(monkeypatch)
    seen = _published(monkeypatch)
    oracle = cast(Any, _GuideOracle({"sap-1"}))
    refs = [EvidenceRef("d1", "c1")]
    good = (
        "1. 権限タブを開く\n2. グループを選ぶ\n3. グループの設定を保存する\n4. 付与結果を確認する"
    )
    result = await validate_answer(
        "q",
        good,
        refs,
        get_settings(),
        oracle=oracle,
        requests=[{"id": "Q1", "text": "手順", "status": "addressed"}],
        guide=GuideCheckRef("sap-1", "guide-1", 2, {"target": "グループ"}),
    )
    # 手順の題名が回答にあっても、手順の根拠（参照の頁・検索の手がかり）が渡した根拠に無い手順は、
    # 本文の充足は未実行として診断に保ち、不足の発見にはしない。条件で外れた分岐は確かめない。
    assert result.valid is True
    assert result.findings == []
    assert all(
        step["validation"]["body_coverage"]["execution"] == "not_run" for step in result.guide_steps
    )
    assert [(step["step_id"], step["status"]) for step in result.guide_steps] == [
        ("open", "no_evidence"),
        ("group", "no_evidence"),
        ("group_save", "no_evidence"),
        ("verify", "no_evidence"),
    ]
    assert result.checks == ["requests", "guide", "guide_steps", "impact"]
    assert result.guide_revision == 2
    assert seen == ["sap-1"]

    # 主張が裏付けられていても、error の finding があれば valid にしない。
    bad = "1. 権限タブを開く\n2. 利用者を選ぶ"
    result = await validate_answer(
        "q",
        bad,
        refs,
        get_settings(),
        oracle=oracle,
        requests=[{"id": "Q2", "text": "確認方法", "status": "missing"}],
        guide=GuideCheckRef("sap-1", "guide-1", 1, {"target": "グループ"}),
    )
    assert result.status == "completed" and result.counts == {"supported": 1}
    assert result.valid is False
    codes = {item.code for item in result.findings if item.severity == "error"}
    assert codes == {
        "request_missing",
        "guide_revision_stale",
        "step_wrong_branch",
        "impact_scope_missing",
    }
    assert len(calls) == 2

    # 根拠が読めなくても、決定的な検査の結果は返す。
    unreadable = await validate_answer(
        "q",
        bad,
        [EvidenceRef("d9", "x")],
        get_settings(),
        oracle=oracle,
        requests=[{"id": "Q2", "text": "確認方法", "status": "missing"}],
    )
    assert unreadable.status == "no_evidence"
    assert [item.code for item in unreadable.findings] == ["request_missing"]


async def test_validate_answer_guide_unavailable_and_profile_scope(
    monkeypatch: MonkeyPatch,
) -> None:
    calls = _supported(monkeypatch)
    _published(monkeypatch)
    oracle = cast(Any, _GuideOracle({"sap-1"}))
    refs = [EvidenceRef("d1", "c1")]
    archived = await validate_answer(
        "q",
        "権限タブを開く",
        refs,
        get_settings(),
        oracle=oracle,
        guide=GuideCheckRef("sap-1", "guide-gone", 1),
    )
    assert archived.valid is False
    assert [item.code for item in archived.findings] == ["guide_unavailable"]
    assert (archived.checks, archived.guide_revision) == (["guide"], None)
    # 利用できない検索・回答プロファイルは、モデルを呼ぶ前に止める。
    before = len(calls)
    with pytest.raises(GuideProfileNotFoundError):
        await validate_answer(
            "q",
            "a",
            refs,
            get_settings(),
            oracle=oracle,
            guide=GuideCheckRef("sap-other", "guide-1", 1),
        )
    assert len(calls) == before


# 業務ガイドの手順の充足（#2708）。2 つの手順が同じ頁の 1 つの chunk（手順 1・2 の主の操作と
# 共通の末尾の操作）を参照し、手順の題名は根拠の原文と言い回しが違う。
_MAIN_1 = "設定画面で設定 A の値を入力します。"
_MAIN_2 = "反映画面で反映ボタンを押して変更を反映します。"
_COMMON = "最後に保存ボタンを押して画面を閉じます。"
_STEP_VECTORS = (
    ("値の登録", (1.0, 0.0, 0.0)),
    ("内容の確定", (0.0, 1.0, 0.0)),
    ("設定 A の値", (1.0, 0.0, 0.3)),
    ("反映ボタンを押", (0.0, 1.0, 0.3)),
    ("保存ボタンを押", (0.5, 0.5, 1.0)),
)


def _shared_guide() -> SupportGuideContent:
    reference = [{"document_id": "d2", "page_ranges": [{"page_start": 3, "page_end": 3}]}]
    return _guide(
        conditions=[],
        branches=[],
        steps=[
            {"id": "s1", "title": "値の登録", "references": reference},
            {"id": "s2", "title": "内容の確定", "depends_on": ["s1"], "references": reference},
        ],
    )


class _SharedOracle(_GuideOracle):
    def __init__(self) -> None:
        super().__init__({"sap-1"})
        self.chunks[("d2", "c2")] = RetrievedChunk(
            document_id="d2",
            chunk_id="c2",
            text=_MAIN_1 + _MAIN_2 + _COMMON,
            score=0.5,
            file_name="manual.pdf",
            metadata={"page_start": 3, "page_end": 3},
        )


def _paragraph_claims(monkeypatch: MonkeyPatch, quotes: dict[str, str]) -> None:
    """段落（行）ごとに、裏付けとして写した原文を返す主張の監査のスタブ（すべて supported）。"""

    def fake_claims(
        question: str, answer: str, items: list[dict[str, Any]], settings: Any, **_options: Any
    ) -> dict[str, Any]:
        lines = [line for line in answer.split("\n") if line.strip()]
        return {
            "status": "completed",
            "counts": {"supported": len(lines)},
            "claim_checks": [
                {
                    "answer_quote": line,
                    "status": "supported",
                    "source_id": "c2",
                    "support_quote": next(q for key, q in quotes.items() if key in line),
                    "reason": "記載",
                }
                for line in lines
            ],
        }

    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)


def _stub_embed(monkeypatch: MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def embed(texts: Any) -> list[list[float]]:
        calls.append(list(texts))
        return [
            next(list(vector) for key, vector in _STEP_VECTORS if key in text) for text in texts
        ]

    import app.rag.answer_validation as validation

    monkeypatch.setattr(validation, "guide_step_embed", lambda settings, loop: embed)
    return calls


async def test_validate_answer_checks_each_guide_step_by_its_main_operation(
    monkeypatch: MonkeyPatch,
) -> None:
    """主張の支持と説明候補を保ち、手順全体の未検証を充足に昇格させない。"""

    async def published_contents(self: Any, profile_id: str) -> list[Any]:
        return [("guide-1", 2, _shared_guide())]

    monkeypatch.setattr(
        support_guide_store.SupportGuideStore, "published_contents", published_contents
    )
    calls = _stub_embed(monkeypatch)
    _paragraph_claims(
        monkeypatch, {"設定 A に": _MAIN_1, "反映ボタンで": _MAIN_2, "保存ボタン": _COMMON}
    )
    oracle = cast(Any, _SharedOracle())
    guide = GuideCheckRef("sap-1", "guide-1", 2)
    refs = [EvidenceRef("d2", "c2")]

    # 手順の題名（値の登録・内容の確定）は回答に出ない。
    reworded = (
        "1. 設定画面を開き、設定 A に値を入れてください。\n"
        "2. 続いて反映画面の反映ボタンで変更を反映させます。\n"
        "最後は保存ボタンで画面を閉じます。"
    )
    result = await validate_answer("q", reworded, refs, get_settings(), oracle=oracle, guide=guide)
    assert [(step["step_id"], step["status"]) for step in result.guide_steps] == [
        ("s1", "unknown"),
        ("s2", "unknown"),
    ]
    # 主張の支持・引用は確認済みでも、手順の全必須要件はこの検査では監査していない。
    assert result.counts["supported"] == 3
    assert all(
        check["status"] == "supported" and check["source_id"] == "c2"
        for check in result.claim_checks
    )
    assert result.missing_evidence == []
    assert result.findings == []
    assert all(
        step["validation"]["body_coverage"]["status"] == "unknown" for step in result.guide_steps
    )
    assert result.valid is True
    assert len(calls) == 1

    common = "保存ボタンを押して画面を閉じれば完了です。"
    result = await validate_answer("q", common, refs, get_settings(), oracle=oracle, guide=guide)
    assert [(step["step_id"], step["status"]) for step in result.guide_steps] == [
        ("s1", "unknown"),
        ("s2", "unknown"),
    ]
    assert result.counts["supported"] == 1
    assert result.missing_evidence == []
    assert result.findings == []
    assert all(
        step["validation"]["body_coverage"]["execution"] == "not_run" for step in result.guide_steps
    )

    # 説明の段落の順が手順の依存と逆なら、題名が回答に無くても順序の誤りにする。
    swapped = (
        "1. 反映画面の反映ボタンで変更を反映させます。\n"
        "2. 設定画面を開き、設定 A に値を入れてください。"
    )
    result = await validate_answer("q", swapped, refs, get_settings(), oracle=oracle, guide=guide)
    assert [(item.code, item.severity) for item in result.findings] == [
        ("step_order", "error"),
    ]
    assert result.valid is False


def test_check_guide_steps_uses_the_located_paragraphs() -> None:
    guide = _guide()
    answer = "1. タブを開きます。\n2. 対象のグループを指定します。"
    located = {"open": "1. タブを開きます。", "group": "2. 対象のグループを指定します。"}
    assert check_guide_steps(answer, guide, {"target": "グループ"}, located) == [
        AnswerFinding(
            "guide_steps",
            "step_following_missing",
            "warning",
            "手順「グループを選ぶ」の後の手順「グループの設定を保存する」が回答にありません。",
            step_id="group_save",
            related_step_id="group",
        )
    ]
    # 充足の判定がどの手順も説明にしなかったときは、題名の不一致を unmatched にしない。
    assert check_guide_steps("設定画面で操作します。", guide, {}, {}) == []


def test_conclusion_first_paragraph_order_is_a_warning_not_an_error() -> None:
    """結論を先に書く回答の段落の並びは実行の順を表さない（#2732）。

    番号つきの手順の列の中の逆は error のまま。
    """
    guide = _guide()
    conditions = {"target": "グループ"}
    conclusion_first = "対象のグループを指定すれば変更できます。\n1. タブを開きます。"
    located = {"open": "1. タブを開きます。", "group": "対象のグループを指定すれば変更できます。"}
    order = [
        item
        for item in check_guide_steps(conclusion_first, guide, conditions, located)
        if item.code == "step_order"
    ]
    assert [(item.severity, item.step_id) for item in order] == [("warning", "group")]

    numbered_reverse = "1. 対象のグループを指定します。\n2. タブを開きます。"
    located = {"open": "2. タブを開きます。", "group": "1. 対象のグループを指定します。"}
    order = [
        item
        for item in check_guide_steps(numbered_reverse, guide, conditions, located)
        if item.code == "step_order"
    ]
    assert [(item.severity, item.step_id) for item in order] == [("error", "group")]


@pytest.mark.usefixtures("auth")
def test_mcp_validate_answer_passes_requests_and_guide(
    auth: Any,  # noqa: F811
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_validate(
        question: str, answer: str, refs: list[EvidenceRef], settings: Any, **kwargs: Any
    ) -> AnswerValidation:
        captured.update(kwargs)
        if kwargs["guide"].search_answer_profile_id == "sap-other":
            raise GuideProfileNotFoundError("sap-other")
        return AnswerValidation(
            status="completed",
            valid=False,
            counts={"supported": 1},
            checks=["requests", "guide", "guide_steps", "impact"],
            findings=[
                AnswerFinding(
                    "guide_steps",
                    "step_order",
                    "error",
                    "順序が逆です。",
                    step_id="user",
                    related_step_id="open",
                )
            ],
            guide_revision=3,
            guide_steps=[
                {
                    "step_id": "open",
                    "title": "権限タブを開く",
                    "required": True,
                    "status": "explained",
                },
                {
                    "step_id": "user",
                    "title": "利用者を選ぶ",
                    "required": True,
                    "status": "unknown",
                    "validation": {
                        "source_association": {"execution": "completed", "linked": True},
                        "body_coverage": {
                            "execution": "not_run",
                            "status": "unknown",
                            "reason": "candidate_association_only",
                        },
                    },
                },
            ],
        )

    monkeypatch.setattr(mcp_tools, "validate_answer", fake_validate)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    arguments: dict[str, Any] = {
        "query": "q",
        "answer": "a",
        "evidence": [{"document_id": "d1", "chunk_id": "c1"}],
        "requests": [{"id": "Q1", "text": "手順", "status": "missing"}],
        "gaps": ["確認方法"],
        "guide": {
            "search_answer_profile_id": "sap-1",
            "guide_id": "guide-1",
            "revision": 3,
            "conditions": {"target": "個別"},
        },
    }
    body = _call("rag_validate_answer", arguments, _token(user.user_uuid))["structuredContent"]
    assert captured["requests"] == [{"id": "Q1", "text": "手順", "status": "missing"}]
    assert captured["gaps"] == ["確認方法"]
    assert captured["guide"] == GuideCheckRef("sap-1", "guide-1", 3, {"target": "個別"})
    assert body["valid"] is False
    assert body["checks"] == ["requests", "guide", "guide_steps", "impact"]
    assert body["guide_revision"] == 3
    assert body["guide_steps"] == [
        {
            "step_id": "open",
            "title": "権限タブを開く",
            "required": True,
            "status": "explained",
            "validation": None,
        },
        {
            "step_id": "user",
            "title": "利用者を選ぶ",
            "required": True,
            "status": "unknown",
            "validation": {
                "source_association": {"execution": "completed", "linked": True},
                "body_coverage": {
                    "execution": "not_run",
                    "status": "unknown",
                    "reason": "candidate_association_only",
                },
            },
        },
    ]
    assert body["findings"] == [
        {
            "check": "guide_steps",
            "code": "step_order",
            "severity": "error",
            "message": "順序が逆です。",
            "request_id": None,
            "step_id": "user",
            "related_step_id": "open",
        }
    ]
    # 範囲の外の検索・回答プロファイルは、業務ガイドが無いのと区別できるエラーにする。
    arguments["guide"]["search_answer_profile_id"] = "sap-other"
    denied = _call("rag_validate_answer", arguments, _token(user.user_uuid))
    assert denied["isError"] is True
    invalid = _call(
        "rag_validate_answer",
        {**arguments, "requests": [{"id": "Q1", "status": "done"}]},
        _token(user.user_uuid),
    )
    assert invalid["isError"] is True


# #1450: deepseek-flash の評価の dev `cmp-pms-severity`（run_aeda6990aad14955bb88d957deb2b6e2）が
# 最終の検証に渡した回答（原文のまま）。台帳の行の場所（行23・行24・行1〜3）を書いている。
_PMS_SEVERITY_ANSWER = (
    "略称が PMS のシステムは 2 つあり、重要度が高いのは **購買管理システム（SYS-119）** です。\n\n"
    "**根拠**\n\n"
    "- システム台帳（system-ledger.xlsx、シート「システム台帳」）\n"
    "  - 行23: システムID: SYS-119 / 正式名: 購買管理システム / 略称・別表記: PMS / 担当部署: 購 / "
    "**重要度: A** / 機密区分: 社外秘\n"
    "  - 行24: システムID: SYS-120 / 正式名: 案件管理システム / 略称・別表記: PMS / 担当部署: 業 / "
    "**重要度: B** / 機密区分: 社外秘\n"
    "  - 行1〜3（表頭の説明）: 「重要度は A・B・C の 3 段階です。"
    "A が最も高く、B、C の順に低くなります。」\n\n"
    "したがって、同じ略称 PMS でも 購買管理システムが重要度 A、案件管理システムが重要度 B で、"
    "**購買管理システムの方が重要度が高い**（A > B）と確かめられます。\n\n"
    "（補足として、重要度の区分ごとの扱いは他の規程にも表れています。"
    "例: 障害連絡規程 第 1 章では重要度 A は検知から 30 分以内、B は 2 時間以内に連絡、"
    "と区分ごとに規則が異なります。）\n\n"
    "なお、念のため確認した略称「購」「業」の正式名は組織規程 第 2 章にあり、"
    "購＝購買部、業＝営業推進部です。"
)
_LEDGER_HEADER = (
    "サンプル社のシステム台帳（架空の会社。評価用の合成資料）\n"
    "担当部署は部署の略号（漢字 1 文字）で書きます。"
    "略号と部署の正式名は組織規程の第 2 章を参照してください。\n"
    "重要度は A・B・C の 3 段階です。A が最も高く、B、C の順に低くなります。"
)
# 台帳の行（行番号と列の値）。その Run の根拠の 30 件のうち、表頭の説明と合わせて台帳の 14 件。
_LEDGER_ROWS = [
    (23, "SYS-119", "購買管理システム", "PMS", "購", "A", "社外秘"),
    (24, "SYS-120", "案件管理システム", "PMS", "業", "B", "社外秘"),
    (6, "SYS-102", "勤怠管理システム", "勤怠", "人", "B", "社外秘"),
    (57, "SYS-153", "販売管理システム", "SMS", "営", "A", "社外秘"),
    (12, "SYS-108", "予算管理システム", "BMS", "経", "A", "極秘"),
    (49, "SYS-145", "中期計画管理システム", "中計", "企", "C", "極秘"),
    (22, "SYS-118", "予算実績システム", "予実", "企", "B", "極秘"),
    (25, "SYS-121", "倉庫管理システム", "WMS", "物", "A", "社外秘"),
    (5, "SYS-101", "経費精算ポータル", "経費 Portal", "経", "B", "社外秘"),
    (55, "SYS-151", "名刺管理システム", "名刺", "業", "C", "社外秘"),
    (7, "SYS-103", "人事評価システム", "HRM", "人", "A", "極秘"),
    (28, "SYS-124", "製造実行システム", "MES", "製", "A", "社外秘"),
    (32, "SYS-128", "カスタマーポータル", "Customer Portal", "顧", "A", "社外秘"),
]
_ROW_REFERENCE = re.compile(r"行(\d+)(?:〜(\d+))?")


def _pms_severity_chunks() -> dict[tuple[str, str], RetrievedChunk]:
    """その Run の根拠の 30 件（台帳の 14 件と、規程の PDF の 16 件）。"""
    ledger = "ledger"
    chunks = {
        (ledger, f"{ledger}:0"): RetrievedChunk(
            document_id=ledger,
            chunk_id=f"{ledger}:0",
            text=_LEDGER_HEADER,
            score=0.5,
            file_name="system-ledger.xlsx",
            metadata={
                "sheet_name": "システム台帳",
                "row_start": 1,
                "row_end": 3,
                "cell_range": "A1:A3",
                "section_path": ["システム台帳"],
            },
        )
    }
    for row, system_id, name, alias, department, severity, secrecy in _LEDGER_ROWS:
        chunks[(ledger, f"{ledger}:{row}")] = RetrievedChunk(
            document_id=ledger,
            chunk_id=f"{ledger}:{row}",
            text=(
                f"システムID: {system_id} / 正式名: {name} / 略称・別表記: {alias} / "
                f"担当部署: {department} / 重要度: {severity} / 機密区分: {secrecy}"
            ),
            score=0.5,
            file_name="system-ledger.xlsx",
            metadata={
                "sheet_name": "システム台帳",
                "row_start": row,
                "row_end": row,
                "cell_range": f"A{row}:F{row}",
            },
        )
    for index in range(16):
        document_id = f"rule{index // 4}"
        chunks[(document_id, f"{document_id}:{index}")] = RetrievedChunk(
            document_id=document_id,
            chunk_id=f"{document_id}:{index}",
            text=f"規程の本文 {index}。重要度 A は検知から 30 分以内、B は 2 時間以内に連絡する。",
            score=0.4,
            file_name=f"rule-{index // 4}.pdf",
            metadata={"page_start": 1, "page_end": 1, "section_path": ["第 1 章"]},
        )
    return chunks


def _location_aware_model(
    system: str, inputs: str, settings: Any, schema: Any, provider_id: str | None = None
) -> Any:
    """場所を根拠の場所と照らすモデルのスタブ（#1450 の判定の指示どおり）。

    段落が「行N」「行N〜M」を書いていれば、内容が一致する根拠の行の範囲（row_start〜row_end）に
    入るときだけ supported。根拠に行の範囲が無ければ、実環境のモデルと同じく
    「位置情報が根拠に無い」で unsupported にする。
    """
    from rag_engine.evaluation.answer_validation import ClaimAuditOutput

    payload = json.loads(inputs)
    evidence = payload["evidence_items"]
    checks = []
    for passage in payload["answer_passages"]:
        text = passage["text"]
        reference = _ROW_REFERENCE.search(text)
        status, evidence_id, reason = "supported", evidence[-1]["evidence_id"], "規程に記載"
        if reference:
            first = int(reference.group(1))
            last = int(reference.group(2) or first)
            tokens = [token for token in ("SYS-119", "SYS-120", "3 段階") if token in text]
            located = [
                item
                for item in evidence
                if any(token in item["text"] for token in tokens)
                and isinstance(item.get("row_start"), int)
                and item["row_start"] <= first
                and last <= int(item.get("row_end") or item["row_start"])
            ]
            if located:
                evidence_id, reason = located[0]["evidence_id"], "内容と行の範囲が根拠と一致"
            else:
                status, evidence_id = "unsupported", ""
                reason = f"内容は一致するが、行{first} という位置情報は渡された根拠に含まれない。"
        checks.append(
            {
                "answer_quote": text,
                "answer_passage_id": passage["id"],
                "status": status,
                "evidence_id": evidence_id,
                "source_id": "",
                "evidence_quote": "",
                "reason": reason,
            }
        )
    return ClaimAuditOutput.model_validate({"claim_checks": checks})


@pytest.mark.parametrize("with_location", [True, False], ids=["location", "no-location"])
async def test_ledger_rows_with_written_locations_are_supported(
    monkeypatch: MonkeyPatch, with_location: bool
) -> None:
    """表計算の根拠の場所（シート・行・セル範囲）を検証に渡し、行番号を書いた段落を外さない（#1450）。

    以前は根拠に頁と本文しか渡さず、「行23」などを書いた台帳の行の段落が「位置情報が根拠に無い」で
    unsupported になり、回答の本文から外れていた（no-location はその再現）。
    """
    import rag_engine.evaluation.answer_eval as answer_eval

    from app.rag import answer_validation

    chunks = _pms_severity_chunks()
    oracle = _Oracle()
    oracle.chunks = chunks
    if not with_location:
        # 頁の無い根拠の場所を渡さない前の形（#1450 の前）。
        monkeypatch.setattr(answer_validation, "evidence_location", lambda metadata: {})
    monkeypatch.setattr(answer_eval, "parse_text_response", _location_aware_model)
    refs = [EvidenceRef(document_id, chunk_id) for document_id, chunk_id in chunks]
    assert len(refs) == 30
    # 場所の伝達だけを検査するため、対象の3行を取り出し、各説明に実際の出典を付ける。
    # 無出典の結論や、別の規程の説明をスタブの supported で通すことはしない。
    answer = "\n\n".join(
        line.strip() + "\n根拠：system-ledger.xlsx"
        for line in _PMS_SEVERITY_ANSWER.splitlines()
        if line.startswith("  - 行")
    )
    result = await validate_answer(
        "略称が PMS のシステムは 2 つあります。重要度が高いのはどちらですか？",
        answer,
        refs,
        get_settings(),
        oracle=cast(Any, oracle),
    )
    rows = {
        label: [check for check in result.claim_checks if label in check["answer_quote"]]
        for label in ("行23", "行24", "行1〜3")
    }
    assert all(rows.values())
    statuses = {label: {check["status"] for check in checks} for label, checks in rows.items()}
    expected = "supported" if with_location else "unsupported"
    assert statuses == {label: {expected} for label in rows}
    assert result.valid is with_location


def test_evidence_location_matches_the_mcp_locator() -> None:
    """検証に渡す場所は MCP の根拠の location と同じ値（分からない項目は入れない。#1450）。"""
    from app.rag.answer_validation import evidence_location

    metadata = {
        "sheet_name": " システム台帳 ",
        "row_start": 23,
        "row_end": 23,
        "cell_range": "A23:F23",
        "section_path": "システム台帳 > 一覧",
        "page_start": None,
    }
    location = evidence_location(metadata)
    assert location == {
        "section_path": ["システム台帳", "一覧"],
        "sheet_name": "システム台帳",
        "row_start": 23,
        "row_end": 23,
        "cell_range": "A23:F23",
    }
    locator = mcp_tools._locator(metadata)
    assert (locator.sheet_name, locator.row_start, locator.row_end, locator.cell_range) == (
        "システム台帳",
        23,
        23,
        "A23:F23",
    )
    assert locator.section_path == location["section_path"]
    assert evidence_location({"page_start": 3, "page_end": 3}) == {}


# #1527: rag_search の回答の監査を通った説明と本文・根拠が同じ段落は、その判定を使い
# モデルに渡さない。
_RAG_ANSWER = (
    "有効期限は最長 30 日です。\n根拠：manual.pdf p.1\n\n"
    "資料により確認できた事項\n\n・有効期限は最長 30 日です。\n根拠：manual.pdf p.1"
)


class _RecordOracle(_Oracle):
    def __init__(self, stored_text: str = "有効期限は最長 30 日です。") -> None:
        super().__init__()
        self.read_trace_ids: list[str] = []
        from app.rag.answer_provenance import answer_prompt_version

        context = validation_reuse_context(
            "有効期限は？",
            _RAG_ANSWER,
            get_settings(),
            prompt_version=answer_prompt_version({}),
            evidence=[
                {
                    "id": "c1",
                    "source": "manual.pdf",
                    "page_start": 1,
                    "page_end": 1,
                    "text": stored_text,
                    "artifact_version": {
                        key: None
                        for key in (
                            "recipe_id",
                            "chunk_set_id",
                            "artifact_id",
                            "source_run_id",
                            "chunk_strategy",
                        )
                    },
                }
            ],
            premises=[],
            data_results=[],
            guide=None,
            requests=None,
            gaps=[],
        )
        self.record: dict[str, Any] = {
            "answer": _RAG_ANSWER,
            "citations_json": [
                {
                    "chunk_id": "c1",
                    "document_id": "d1",
                    "text": stored_text,
                    "metadata": {"answer_citation_lines": [1, 2]},
                }
            ],
            "diagnostics_json": {
                "answer_validation": {
                    "status": "completed",
                    "valid": True,
                    "reuse_context": context,
                    "claim_checks": [
                        {
                            "status": "supported",
                            "answer_quote": "有効期限は最長 30 日です。",
                            "source_id": "c1",
                        }
                    ],
                },
                "envelope": {
                    "items": [
                        {"kind": "rule", "text": "有効期限は最長 30 日です。", "applies": "matched"}
                    ]
                },
            },
        }

    async def answer_prompt_overrides(self) -> dict[str, str]:
        return {}

    async def get_answer_record(self, trace_id: str) -> Any:
        # 記録は今の利用者の範囲で読む（範囲の外は None）。
        self.read_trace_ids.append(trace_id)
        return self.record if trace_id == "trace-1" else None


def _never_called(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("監査済みの段落だけならモデルを呼ばない")


async def test_validate_answer_reuses_the_audited_items_of_the_answer_record(
    monkeypatch: MonkeyPatch,
) -> None:
    import rag_engine.evaluation.answer_eval as answer_eval

    monkeypatch.setattr(answer_eval, "parse_text_response", _never_called)
    oracle = _RecordOracle()
    answer = _RAG_ANSWER
    result = await validate_answer(
        "有効期限は？",
        answer,
        [EvidenceRef("d1", "c1")],
        get_settings(),
        oracle=cast(Any, oracle),
        answer_trace_ids=["trace-1", "trace-other", "trace-1"],
    )
    assert oracle.read_trace_ids == ["trace-1", "trace-other"]
    assert (result.status, result.valid, result.reused_claims) == ("completed", True, 2)
    assert result.counts == {"supported": 2}


async def test_validate_answer_does_not_reuse_when_the_evidence_changed(
    monkeypatch: MonkeyPatch,
) -> None:
    """記録したときから根拠の本文が変わった（版が変わった）なら、モデルで確かめ直す。"""
    calls: list[str] = []

    def fake_claims(
        question: str, answer: str, items: list[dict[str, Any]], settings: Any, **options: Any
    ) -> dict[str, Any]:
        calls.append(answer)
        assert options["audited"] == {}
        return {"status": "completed", "counts": {"supported": 1}, "claim_checks": []}

    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)
    oracle = _RecordOracle(stored_text="有効期限は最長 14 日です。")
    result = await validate_answer(
        "有効期限は？",
        "有効期限は最長 30 日です。",
        [EvidenceRef("d1", "c1")],
        get_settings(),
        oracle=cast(Any, oracle),
        answer_trace_ids=["trace-1"],
    )
    assert calls == ["有効期限は最長 30 日です。"]
    assert result.reused_claims == 0


def test_mcp_validate_answer_passes_answer_trace_ids(
    auth: Any,  # noqa: F811
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_validate(
        question: str, answer: str, refs: list[EvidenceRef], settings: Any, **kwargs: Any
    ) -> AnswerValidation:
        captured.update(kwargs)
        return AnswerValidation(
            status="completed", valid=True, counts={"supported": 1}, reused_claims=1
        )

    monkeypatch.setattr(mcp_tools, "validate_answer", fake_validate)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    arguments: dict[str, Any] = {
        "query": "q",
        "answer": "a",
        "evidence": [{"document_id": "d1", "chunk_id": "c1"}],
        "answer_trace_ids": ["trace-1"],
    }
    body = _call("rag_validate_answer", arguments, _token(user.user_uuid))["structuredContent"]
    assert captured["answer_trace_ids"] == ["trace-1"]
    assert body["reused_claims"] == 1
    too_many = _call(
        "rag_validate_answer",
        {**arguments, "answer_trace_ids": [f"t{i}" for i in range(11)]},
        _token(user.user_uuid),
    )
    assert too_many["isError"] is True


def test_mcp_validate_answer_passes_premises_and_hides_premise_sources(
    auth: Any,  # noqa: F811
    monkeypatch: MonkeyPatch,
) -> None:
    """利用者が確認で答えた前提（premises）を検証に渡す（#1898）。

    前提だけで裏付けた段落の chunk_id は null。
    """
    captured: dict[str, Any] = {}

    async def fake_validate(
        question: str, answer: str, refs: list[EvidenceRef], settings: Any, **kwargs: Any
    ) -> AnswerValidation:
        captured.update(kwargs)
        return AnswerValidation(
            status="completed",
            valid=True,
            counts={"supported": 2},
            claim_checks=[
                {
                    "answer_quote": "承認は得ていません。",
                    "status": "supported",
                    "source_id": "premise:1",
                },
                {"answer_quote": "削除できません。", "status": "supported", "source_id": "c1"},
            ],
        )

    monkeypatch.setattr(mcp_tools, "validate_answer", fake_validate)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    premise = {"question": "部門長の承認は得ていますか？", "answer": "いいえ"}
    arguments: dict[str, Any] = {
        "query": "q",
        "answer": "承認は得ていません。削除できません。",
        "evidence": [{"document_id": "d1", "chunk_id": "c1"}],
        "premises": [premise],
    }
    body = _call("rag_validate_answer", arguments, _token(user.user_uuid))["structuredContent"]
    assert captured["premises"] == [premise]
    assert [claim["chunk_id"] for claim in body["claims"]] == [None, "c1"]
    for invalid in ([{**premise, "answer": ""}], [premise] * 11, [{**premise, "extra": "x"}]):
        denied = _call(
            "rag_validate_answer", {**arguments, "premises": invalid}, _token(user.user_uuid)
        )
        assert denied["isError"] is True


async def test_validate_answer_passes_premises_to_the_claim_audit(monkeypatch: MonkeyPatch) -> None:
    """前提は根拠を読めたときに主張の監査へ渡す（#1898）。"""
    seen: dict[str, Any] = {}

    def fake_claims(
        question: str, answer: str, items: list[dict[str, Any]], settings: Any, **options: Any
    ) -> dict[str, Any]:
        seen.update(options)
        return {"status": "completed", "counts": {"supported": 1}, "claim_checks": []}

    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)
    premises = [{"question": "承認は？", "answer": "いいえ"}]
    result = await validate_answer(
        "q",
        "承認は得ていません。",
        [EvidenceRef("d1", "c1")],
        get_settings(),
        oracle=cast(Any, _RecordOracle()),
        premises=premises,
    )
    assert seen["premises"] == premises
    assert result.status == "completed"


def test_mcp_validate_answer_passes_passage_citations(
    auth: Any,  # noqa: F811
    monkeypatch: MonkeyPatch,
) -> None:
    """本文に出典を書かない呼び出し側の段落ごとの出典（passage_citations）を検証に渡す（#104）。"""
    captured: dict[str, Any] = {}

    async def fake_validate(
        question: str, answer: str, refs: list[EvidenceRef], settings: Any, **kwargs: Any
    ) -> AnswerValidation:
        captured.update(kwargs)
        return AnswerValidation(status="completed", valid=True, counts={"supported": 1})

    monkeypatch.setattr(mcp_tools, "validate_answer", fake_validate)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    citation = {"answer_range": {"start": 0, "end": 9}, "chunk_ids": ["c1"]}
    arguments: dict[str, Any] = {
        "query": "q",
        "answer": "上限は1万円です。",
        "evidence": [{"document_id": "d1", "chunk_id": "c1"}],
        "passage_citations": [citation],
    }
    _call("rag_validate_answer", arguments, _token(user.user_uuid))
    assert captured["passage_citations"] == [{"start": 0, "end": 9, "chunk_ids": ["c1"]}]
    # 省略は空（今までどおり本文の出典だけ）。
    captured.clear()
    _call(
        "rag_validate_answer",
        {key: value for key, value in arguments.items() if key != "passage_citations"},
        _token(user.user_uuid),
    )
    assert captured["passage_citations"] == []
    for invalid in (
        [{**citation, "chunk_ids": []}],
        [{**citation, "answer_range": {"start": -1, "end": 9}}],
        [{**citation, "extra": "x"}],
    ):
        denied = _call(
            "rag_validate_answer",
            {**arguments, "passage_citations": invalid},
            _token(user.user_uuid),
        )
        assert denied["isError"] is True


async def test_validate_answer_passes_passage_citations_to_the_claim_audit(
    monkeypatch: MonkeyPatch,
) -> None:
    """段落ごとの出典は主張の監査へ渡す（#104）。"""
    seen: dict[str, Any] = {}

    def fake_claims(
        question: str, answer: str, items: list[dict[str, Any]], settings: Any, **options: Any
    ) -> dict[str, Any]:
        seen.update(options)
        return {"status": "completed", "counts": {"supported": 1}, "claim_checks": []}

    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)
    citations = [{"start": 0, "end": 9, "chunk_ids": ["c1"]}]
    await validate_answer(
        "q",
        "上限は1万円です。",
        [EvidenceRef("d1", "c1")],
        get_settings(),
        oracle=cast(Any, _RecordOracle()),
        passage_citations=citations,
    )
    assert seen["caller_citations"] == citations


def test_mcp_validate_answer_passes_data_results_and_hides_their_sources(
    auth: Any,  # noqa: F811
    monkeypatch: MonkeyPatch,
) -> None:
    """業務データの照会の結果（data_results）を検証に渡す（#1950）。

    結果だけで裏付けた段落の chunk_id は null。
    """
    captured: dict[str, Any] = {}

    async def fake_validate(
        question: str, answer: str, refs: list[EvidenceRef], settings: Any, **kwargs: Any
    ) -> AnswerValidation:
        captured.update(kwargs)
        return AnswerValidation(
            status="completed",
            valid=True,
            counts={"supported": 1},
            claim_checks=[
                {"answer_quote": "6 件です。", "status": "supported", "source_id": "data:1"}
            ],
        )

    monkeypatch.setattr(mcp_tools, "validate_answer", fake_validate)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    data = {"source": "NL2SQL", "question": "件数", "sql": "SELECT COUNT(*) FROM T", "result": "6"}
    arguments: dict[str, Any] = {
        "query": "q",
        "answer": "6 件です。",
        "evidence": [{"document_id": "d1", "chunk_id": "c1"}],
        "data_results": [data],
    }
    body = _call("rag_validate_answer", arguments, _token(user.user_uuid))["structuredContent"]
    assert captured["data_results"] == [data]
    assert [claim["chunk_id"] for claim in body["claims"]] == [None]
    for invalid in ([{**data, "result": ""}], [data] * 11, [{**data, "extra": "x"}]):
        denied = _call(
            "rag_validate_answer", {**arguments, "data_results": invalid}, _token(user.user_uuid)
        )
        assert denied["isError"] is True


def test_engine_settings_pass_the_validation_input_budget(tmp_path: Path) -> None:
    """回答の最終の検証の入力の上限は backend の設定から rag_engine に渡る。"""
    from app.config import Settings
    from app.rag.answer_engine import build_engine_settings

    settings = Settings(rag_answer_validation_max_input_bytes=120_000)
    engine_settings = build_engine_settings(settings, output_dir=tmp_path)

    assert engine_settings.answer_validation_max_input_bytes == 120_000
    assert Settings().rag_answer_validation_max_input_bytes == 96_000


@pytest.mark.parametrize(
    "changed",
    [
        "question",
        "answer",
        "prompt_version",
        "evidence",
        "premises",
        "data_results",
        "requests",
        "gaps",
        "guide",
        "model",
        "profile",
        "runtime",
    ],
)
def test_review_reuse_invalidates_changed_dependencies(changed: str) -> None:
    """前提・参照・指示などの変化を、本文の字句類似では省略しない。"""
    settings = get_settings()
    arguments: dict[str, Any] = dict(
        question="質問A",
        answer="回答A",
        settings=settings,
        prompt_version="prompt-a",
        evidence=[{"id": "c1", "text": "本文A", "artifact_version": "v1"}],
        premises=[],
        data_results=[],
        requests=None,
        gaps=[],
        guide=None,
    )
    original = validation_reuse_context(**arguments)
    replacements: dict[str, Any] = {
        "question": "質問B",
        "answer": "回答B",
        "prompt_version": "prompt-b",
        "evidence": [{"id": "c1", "text": "本文A", "artifact_version": "v2"}],
        "premises": [{"id": "p1", "text": "前提A"}],
        "data_results": [{"id": "r1", "text": "結果A"}],
        "requests": [{"id": "r1", "text": "要求A"}],
        "gaps": ["不足A"],
        "guide": GuideCheckRef("profile-a", "guide-a", 2),
    }
    if changed in {"profile", "runtime"}:
        name = (
            "rag_search_answer_profile_revision"
            if changed == "profile"
            else "rag_runtime_knowledge"
        )
        arguments["settings"] = settings.model_copy(update={name: {"revision": "b"}})
    elif changed == "model":
        arguments["settings"] = settings.model_copy(
            update={"enterprise_ai_default_text_model": "test-model-b"}
        )
    else:
        arguments[changed] = replacements[changed]
    assert validation_reuse_context(**arguments) != original


@pytest.mark.parametrize("status", ["partial", "timed_out", "error", "not_run", "missing_version"])
async def test_review_reuse_never_trusts_incomplete_or_legacy_records(status: str) -> None:
    from app.rag.answer_validation import audited_answer_lines

    oracle = _RecordOracle()
    validation = oracle.record["diagnostics_json"]["answer_validation"]
    context = validation["reuse_context"]
    if status == "missing_version":
        validation.pop("reuse_context")
    else:
        validation["status"] = status
    assert (
        await audited_answer_lines(
            cast(Any, oracle),
            ["trace-1"],
            {"c1": {"text": "有効期限は最長 30 日です。"}},
            reuse_context=context,
        )
        == {}
    )


@pytest.mark.parametrize("source_id,evidence_id", [("c2", "E2"), ("c1", "E1,E2")])
async def test_reuse_requires_the_final_audits_actual_claim_source_pair(
    source_id: str, evidence_id: str
) -> None:
    from app.rag.answer_validation import audited_answer_lines

    oracle = _RecordOracle()
    validation = oracle.record["diagnostics_json"]["answer_validation"]
    validation["claim_checks"][0].update(source_id=source_id, evidence_id=evidence_id)
    assert (
        await audited_answer_lines(
            cast(Any, oracle),
            ["trace-1"],
            {"c1": {"text": "有効期限は最長 30 日です。"}},
            reuse_context=validation["reuse_context"],
        )
        == {}
    )


# 原質問・確認の答えとの両立の比較の採否（#3800）。engine の診断のうち ID・状態・code だけを
# 記録と MCP の出力に渡し、モデルが書いた制約・推奨・原文は残さない。
_PREMISE_DIAGNOSTICS: list[Any] = [
    {
        "answer_passage_id": "A1",
        "constraints": "合成の制約",
        "recommendation": "合成の推奨",
        "fit": "conflicts",
        "constraint_scope": "execution_only",
        "passage_mode": "proposal",
        "constraint_quote": "合成の原文 A",
        "passage_quote": "合成の原文 B",
        "status": "completed",
        "adopted": False,
        "decision": "execution_prohibition_does_not_prohibit_proposal",
    },
    {
        "answer_passage_id": "A2",
        "adopted": False,
        "decision": "missing_passage_id",
        "fit": "unknown",
    },
    "壊れた記録",
]
_PREMISE_RECORDS = [
    {
        "answer_passage_id": "A1",
        "status": "completed",
        "decision": "execution_prohibition_does_not_prohibit_proposal",
        "adopted": False,
        "fit": "conflicts",
        "constraint_scope": "execution_only",
        "passage_mode": "proposal",
    },
    {
        "answer_passage_id": "A2",
        "decision": "missing_passage_id",
        "adopted": False,
        "fit": "unknown",
    },
]


async def test_validate_answer_keeps_the_premise_consistency_decisions(
    monkeypatch: MonkeyPatch,
) -> None:
    """engine の両立の比較の採否を、技術の項目だけにして結果に残す（#3800）。"""

    def fake_claims(
        question: str, answer: str, items: list[dict[str, Any]], settings: Any, **options: Any
    ) -> dict[str, Any]:
        return {
            "status": "completed",
            "counts": {"supported": 1},
            "claim_checks": [],
            "premise_consistency_checks": _PREMISE_DIAGNOSTICS,
        }

    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)
    result = await validate_answer(
        "q",
        "承認は得ていません。",
        [EvidenceRef("d1", "c1")],
        get_settings(),
        oracle=cast(Any, _RecordOracle()),
        premises=[{"question": "合成の確認", "answer": "合成の答え"}],
    )
    assert result.premise_consistency_checks == _PREMISE_RECORDS
    assert "合成" not in json.dumps(result.premise_consistency_checks, ensure_ascii=False)


@pytest.mark.parametrize(
    "checks",
    [None, "not-a-list", [{"status": "not_run", "decision": "not_required", "adopted": False}]],
    ids=["missing", "invalid", "not-run"],
)
async def test_validate_answer_premise_checks_without_passages(
    monkeypatch: MonkeyPatch, checks: object
) -> None:
    """比較しなかった記録はそのまま残し、無い・壊れた値は空にする（#3800）。"""

    def fake_claims(
        question: str, answer: str, items: list[dict[str, Any]], settings: Any, **options: Any
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "completed", "counts": {}, "claim_checks": []}
        if checks is not None:
            result["premise_consistency_checks"] = checks
        return result

    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)
    result = await validate_answer(
        "q",
        "承認は得ていません。",
        [EvidenceRef("d1", "c1")],
        get_settings(),
        oracle=cast(Any, _RecordOracle()),
    )
    assert result.premise_consistency_checks == (checks if isinstance(checks, list) else [])


def test_mcp_validate_answer_returns_the_premise_checks(
    auth: Any,  # noqa: F811
    monkeypatch: MonkeyPatch,
) -> None:
    """両立の比較の採否を、段落の位置と code で返す（#3800。Agent は結果をそのまま記録する）。"""
    answer = "合成の案を示します。\n合成の件数は 2 件です。"

    async def fake_validate(
        question: str, answer: str, refs: list[EvidenceRef], settings: Any, **kwargs: Any
    ) -> AnswerValidation:
        return AnswerValidation(
            status="completed",
            valid=True,
            counts={"supported": 2},
            premise_consistency_checks=[
                *_PREMISE_RECORDS,
                {"answer_passage_id": "A9", "decision": "unknown_passage_id", "adopted": False},
                {"status": "failed", "decision": "check_failed", "adopted": False},
            ],
        )

    monkeypatch.setattr(mcp_tools, "validate_answer", fake_validate)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    body = _call(
        "rag_validate_answer",
        {
            "query": "q",
            "answer": answer,
            "evidence": [{"document_id": "d1", "chunk_id": "c1"}],
            "premises": [{"question": "合成の確認", "answer": "合成の答え"}],
        },
        _token(user.user_uuid),
    )["structuredContent"]
    checks = body["premise_checks"]
    first, second = (
        answer[item["answer_range"]["start"] : item["answer_range"]["end"]] for item in checks[:2]
    )
    assert (first, second) == ("合成の案を示します。", "合成の件数は 2 件です。")
    assert checks[0] == {
        "answer_range": checks[0]["answer_range"],
        "status": "completed",
        "decision": "execution_prohibition_does_not_prohibit_proposal",
        "adopted": False,
        "fit": "conflicts",
        "constraint_scope": "execution_only",
        "passage_mode": "proposal",
    }
    # 回答に無い段落の ID・段落に結び付かない記録は位置を null にして残す。
    assert [(item["answer_range"], item["decision"], item["status"]) for item in checks[2:]] == [
        (None, "unknown_passage_id", None),
        (None, "check_failed", "failed"),
    ]
