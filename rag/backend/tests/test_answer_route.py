"""回答の経路と理由の記録（#1283）。"""

from __future__ import annotations

from typing import Any, cast

from app.config import Settings
from app.rag.answer_route import answer_route


def _diagnostics(outcome: str, handoff: list[str] | None = None) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "envelope": {"schema_version": 1, "outcome": outcome, "handoff_reasons": handoff or []},
    }


def test_route_records_outcome_as_reason() -> None:
    """答えた・条件付き・確認・人への引き継ぎ・資料の不足は、現場のデータを求めない。"""
    for outcome in (
        "answered",
        "conditional",
        "needs_clarification",
        "needs_human",
        "insufficient_evidence",
    ):
        route = answer_route(_diagnostics(outcome, ["gaps"]))
        assert route == {
            "schema_version": 1,
            "path": "rag",
            "reason": outcome,
            "signals": ["gaps"],
            "requires_environment_data": False,
        }


def test_route_marks_environment_data_when_documents_cannot_finish() -> None:
    """資料だけでは確定できず、現場の値・記録の確認が要る回答だけ印を立てる。"""
    route = answer_route(_diagnostics("needs_environment_data", ["environment_data"]))
    assert route["reason"] == "needs_environment_data"
    assert route["requires_environment_data"] is True
    assert route["signals"] == ["environment_data"]


def test_route_conditional_with_environment_signal_is_not_marked() -> None:
    """手順を答えたうえで一部の値の確認が要る（conditional）回答は、回答で完了できる。"""
    route = answer_route(_diagnostics("conditional", ["environment_data"]))
    assert route["requires_environment_data"] is False


def test_route_without_envelope_is_unknown() -> None:
    """回答の構造が無い・知らない対応は unknown にする。"""
    assert answer_route({})["reason"] == "unknown"
    assert answer_route({"outcome": "something"})["reason"] == "unknown"
    # 回答の構造が無くても、診断の outcome があれば使う。
    assert answer_route({"outcome": "needs_environment_data"})["requires_environment_data"] is True


def test_route_has_no_reference_to_other_products() -> None:
    """経路の記録は製品に中立（呼び出し元・ほかのサービスの名前・URL を持たない）。"""
    route = answer_route(_diagnostics("needs_environment_data"))
    assert set(route) == {
        "schema_version",
        "path",
        "reason",
        "signals",
        "requires_environment_data",
    }
    assert "agent" not in str(route).lower()


async def test_pipeline_records_route_for_guide_clarification() -> None:
    """業務ガイドの確認の質問（モデルを呼ばない回答）にも経路を記録する。"""
    from app.rag.pipeline import RagPipeline
    from app.schemas.search import SearchRequest

    settings = Settings(
        _env_file=None,
        rag_support_guide_enabled=True,
        rag_support_guide={
            "guide_id": "g-1",
            "revision": 1,
            "title": "権限の付与",
            "decision": "clarify",
            "short_answer": "対象は個別ですか、グループですか。",
        },
    )
    pipeline = RagPipeline(
        settings=settings,
        oracle=object(),  # type: ignore[arg-type]
        genai=object(),  # type: ignore[arg-type]
    )
    response = await pipeline.run(SearchRequest(query="権限を付与したい"))
    assert response.diagnostics.answer is not None
    route = cast(dict[str, Any], response.diagnostics.answer["route"])
    assert (route["reason"], route["requires_environment_data"]) == ("needs_clarification", False)
