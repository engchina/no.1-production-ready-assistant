"""回答生成の時間の上限と時間切れの文言のテスト（#375）。"""

import asyncio

import pytest
from pydantic import ValidationError

from app.config import ENTERPRISE_AI_TIMEOUT_MAX_SECONDS, Settings
from app.rag.answer_timeout import (
    AnswerTimeoutError,
    StageTracker,
    answer_timeout_message,
    run_answer_with_timeout,
)
from app.rag.pipeline import SearchStageProgress
from app.rag.span_display import ANSWER_STAGE_LABELS


def _progress(stage: str, outcome: str) -> SearchStageProgress:
    return SearchStageProgress(
        trace_id="trace", stage=stage, outcome=outcome, elapsed_ms=0.0, attributes={}
    )


def test_answer_timeout_default_and_limit() -> None:
    """回答生成は既定 600 秒（#1425）・上限は LLM 1 回の timeout の上限。

    検索だけの上限（旧 rag_search_timeout_seconds）は #383 で削除した（品質評価の 1 ケースも
    回答生成の上限で打ち切る）。
    """
    settings = Settings()
    assert settings.rag_answer_timeout_seconds == 600.0 == ENTERPRISE_AI_TIMEOUT_MAX_SECONDS
    assert not hasattr(settings, "rag_search_timeout_seconds")
    assert Settings(rag_answer_timeout_seconds=ENTERPRISE_AI_TIMEOUT_MAX_SECONDS)
    with pytest.raises(ValidationError):
        Settings(rag_answer_timeout_seconds=ENTERPRISE_AI_TIMEOUT_MAX_SECONDS + 1)
    with pytest.raises(ValidationError):
        Settings(rag_answer_timeout_seconds=0)


def test_answer_timeout_env_example_documents_setting() -> None:
    from pathlib import Path

    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    assert "RAG_ANSWER_TIMEOUT_SECONDS=600" in env_example.read_text(encoding="utf-8")


def test_answer_timeout_message_names_stage_and_retry() -> None:
    message = answer_timeout_message("history_rewrite", 300)
    assert "5 分" in message
    assert "前の会話を踏まえて質問を補う" in message
    assert "もう一度送信してください" in message
    assert "90 秒" in answer_timeout_message("answer", 90)
    assert "質問の条件を整理して回答を作る" in answer_timeout_message("answer", 90)
    # 開始前・未知の工程・工程の中の時間切れ（上限の秒数は出さない）。
    assert "検索の準備" in answer_timeout_message(None, 30)
    assert "（時間切れになった工程: 処理）" in answer_timeout_message("unknown_stage", 30)
    assert "時間内に終わりませんでした" in answer_timeout_message("answer", None)
    # 回答フローの各工程（#593）は利用者向けの名前にする（#2096。画面の処理の経過と同じ名前）。
    assert (
        "（時間切れになった工程: 検索文から根拠の候補を集める（1 回目））"
        in answer_timeout_message("answer_step:文書検索（1回目）", 30)
    )
    assert "（時間切れになった工程: 資料を探す検索表現を作る）" in answer_timeout_message(
        "answer_step:質問拡張戦略", 30
    )
    # 知らない工程名はそのまま出す。
    assert "（時間切れになった工程: 新しい工程（2回目））" in answer_timeout_message(
        "answer_step:新しい工程（2回目）", 30
    )
    assert "（時間切れになった工程: 処理）" in answer_timeout_message("answer_step:", 30)


def test_stage_labels_cover_answer_stages() -> None:
    """進捗の工程は回答の工程・保存(#2342)・会話の添付からの根拠の選択(#3475)と検索だけの工程。

    旧 standard の工程は #595 で削除した。
    """
    assert set(ANSWER_STAGE_LABELS) == {
        "retrieval",
        "history_rewrite",
        "attachment_evidence",
        "field_filter",
        "answer",
        # 回答を作らない RAG 検索の全体（#3506）。
        "evidence",
        "answer_record_save",
        "chat_message_save",
    }


async def test_stage_tracker_follows_nested_stages() -> None:
    forwarded: list[str] = []

    async def inner(progress: SearchStageProgress) -> None:
        forwarded.append(f"{progress.stage}:{progress.outcome}")

    tracker = StageTracker(inner)
    assert tracker.current_stage is None
    await tracker(_progress("answer", "started"))
    await tracker(_progress("answer_step:文書検索（1回目）", "started"))
    assert tracker.current_stage == "answer_step:文書検索（1回目）"
    await tracker(_progress("answer_step:文書検索（1回目）", "success"))
    # 入れ子の内側が終わったら外側の工程に戻る。
    assert tracker.current_stage == "answer"
    await tracker(_progress("answer", "success"))
    # 実行中の工程がなければ、最後に通知された工程。
    assert tracker.current_stage == "answer"
    assert forwarded[0] == "answer:started"
    assert len(forwarded) == 4


async def test_run_answer_with_timeout_reports_last_stage() -> None:
    settings = Settings(rag_answer_timeout_seconds=0.05)

    async def slow(tracker: StageTracker) -> str:
        await tracker(_progress("history_rewrite", "started"))
        await asyncio.sleep(1)
        return "done"

    with pytest.raises(AnswerTimeoutError) as caught:
        await run_answer_with_timeout(slow, settings)

    assert caught.value.stage == "history_rewrite"
    assert caught.value.timeout_seconds == 0.05
    assert "前の会話を踏まえて質問を補う" in caught.value.user_message
    assert isinstance(caught.value, TimeoutError)
    assert isinstance(caught.value.original_error, TimeoutError)


async def test_run_answer_with_timeout_wraps_inner_timeout_without_limit() -> None:
    """工程の中の TimeoutError（LLM 1 回の timeout など）も工程付きの時間切れにする。"""
    settings = Settings(rag_answer_timeout_seconds=60)

    async def inner_timeout(tracker: StageTracker) -> str:
        await tracker(_progress("answer", "started"))
        raise TimeoutError

    with pytest.raises(AnswerTimeoutError) as caught:
        await run_answer_with_timeout(inner_timeout, settings)

    assert caught.value.stage == "answer"
    assert caught.value.timeout_seconds is None
    assert "時間内に終わりませんでした" in caught.value.user_message


async def test_run_answer_with_timeout_returns_result_within_limit() -> None:
    settings = Settings(rag_answer_timeout_seconds=5)

    async def quick(tracker: StageTracker) -> str:
        await asyncio.sleep(0.05)
        return "ok"

    assert await run_answer_with_timeout(quick, settings) == "ok"


async def test_search_reports_started_stages_as_mcp_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rag_search の工程の始まりを MCP の進み具合の段階の名前にする（#1864）。"""
    from app.api.routes import search as search_route

    reported: list[str] = []
    monkeypatch.setattr(search_route, "report_tool_progress", reported.append)
    await search_route._report_tool_progress(_progress("answer", "started"))
    await search_route._report_tool_progress(_progress("answer_step:根拠確認（1回目）", "started"))
    await search_route._report_tool_progress(_progress("answer_step:根拠確認（1回目）", "success"))
    await search_route._report_tool_progress(_progress("answer_record_save", "started"))
    # 終わった工程は知らせない。名前は時間切れの文言と同じ（answer_stage_label）。
    assert reported == [
        "質問の条件を整理して回答を作る",
        "要求に答える根拠があるか判定する（1 回目）",
        "質問と回答と根拠の記録を保存する",
    ]
