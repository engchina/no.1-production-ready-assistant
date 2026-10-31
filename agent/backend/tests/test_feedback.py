"""チャットの回答へのフィードバックと、その集計（#774）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent.feedback import build_feedback_report
from app.features.agent.run_facts import fact_from_run
from app.features.agent.runtime import (
    Artifact,
    FeedbackRating,
    FeedbackReason,
    RunFeedback,
    RunState,
    RunStatus,
    runtime_repository,
)
from app.security.domain import LOCAL_DEBUG_USER_UUID
from app.security.permissions import MENU_FEEDBACK, MENU_RUNS, RUNS_OPERATE
from app.security.service import set_security_service

NOW = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)
ALICE = "aaaaaaaa-0000-0000-0000-000000000001"


def _run(
    run_id: str,
    *,
    agent_id: str = "default",
    status: RunStatus = RunStatus.COMPLETED,
    user: str | None = LOCAL_DEBUG_USER_UUID,
    rated_at: datetime | None = None,
    rating: FeedbackRating = FeedbackRating.HELPFUL,
    reason: FeedbackReason | None = None,
) -> RunState:
    return RunState(
        id=run_id,
        goal=f"{run_id} の質問",
        agent_id=agent_id,
        runtime_id="builtin",
        status=status,
        created_by_user_uuid=user,
        artifacts=(
            [Artifact(name="回答", kind="answer", content={"text": f"{run_id} の回答"})]
            if status == RunStatus.COMPLETED
            else []
        ),
        feedback=(
            None
            if rated_at is None
            else RunFeedback(rating=rating, reason=reason, user_uuid=user, updated_at=rated_at)
        ),
    )


def _report(runs: list[RunState], **filters: Any) -> Any:
    return build_feedback_report(
        [fact_from_run(run) for run in runs],
        days=filters.get("days", 7),
        now=NOW,
        agent_id=filters.get("agent_id"),
        rating=filters.get("rating"),
        reason=filters.get("reason"),
        agent_names={"default": "資料・データ横断アシスタント", "sales": "営業の Agent"},
        user_names=lambda uuids: {uuid: "Alice" for uuid in uuids if uuid == ALICE},
        offset=filters.get("offset", 0),
        limit=filters.get("limit", 10),
    )


def test_report_summarizes_the_period_and_lists_newest_first() -> None:
    bad = FeedbackRating.NOT_HELPFUL
    runs = [
        _run("r1", user=ALICE, rated_at=NOW - timedelta(hours=1)),
        _run("r2", rated_at=NOW - timedelta(days=1), rating=bad, reason=FeedbackReason.INCOMPLETE),
        _run("r3", rated_at=NOW - timedelta(days=2), rating=bad, reason=FeedbackReason.INCORRECT),
        _run(
            "r4",
            agent_id="sales",
            rated_at=NOW - timedelta(days=3),
            rating=bad,
            reason=FeedbackReason.INCORRECT,
        ),
        # 前の 7 日（比較用）と、評価していない Run・2 期間より前の評価。
        _run("r5", rated_at=NOW - timedelta(days=10)),
        _run("r6"),
        _run("r7", rated_at=NOW - timedelta(days=30)),
    ]

    report = _report(runs)

    summary = report.summary
    assert (summary.total, summary.helpful, summary.not_helpful) == (4, 1, 3)
    assert summary.helpful_rate == pytest.approx(0.25)
    # 理由は件数の多い順。
    assert [(item.reason, item.count) for item in summary.reason_counts] == [
        (FeedbackReason.INCORRECT, 2),
        (FeedbackReason.INCOMPLETE, 1),
    ]
    assert (report.previous.total, report.previous.helpful_rate) == (1, 1.0)
    assert [item.run_id for item in report.items] == ["r1", "r2", "r3", "r4"]
    first = report.items[0]
    assert (first.display_name, first.question, first.answer) == ("Alice", "r1 の質問", "r1 の回答")
    assert report.items[3].agent_name == "営業の Agent"


def test_report_filters_the_list_by_agent_rating_and_reason() -> None:
    bad = FeedbackRating.NOT_HELPFUL
    runs = [
        _run("r1", rated_at=NOW - timedelta(hours=1)),
        _run("r2", rated_at=NOW - timedelta(hours=2), rating=bad, reason=FeedbackReason.INCORRECT),
        _run(
            "r3",
            agent_id="sales",
            rated_at=NOW - timedelta(hours=3),
            rating=bad,
            reason=FeedbackReason.WRONG_ACTION,
        ),
    ]

    by_agent = _report(runs, agent_id="sales")
    # 業務 Agent の絞り込みは集計にも効く。評価・理由の絞り込みは一覧だけ。
    assert (by_agent.summary.total, [item.run_id for item in by_agent.items]) == (1, ["r3"])
    by_rating = _report(runs, rating=bad)
    assert by_rating.summary.total == 3
    assert ([item.run_id for item in by_rating.items], by_rating.total) == (["r2", "r3"], 2)
    by_reason = _report(runs, reason=FeedbackReason.WRONG_ACTION)
    assert [item.run_id for item in by_reason.items] == ["r3"]
    empty = _report([])
    assert (empty.summary.total, empty.summary.helpful_rate, empty.items) == (0, None, [])


def test_report_pages_the_list_newest_first_and_counts_every_match() -> None:
    """一覧はサーバー側のページング（#794）。集計と件数はすべての評価で数える。"""
    runs = [_run(f"r{index:02d}", rated_at=NOW - timedelta(hours=index)) for index in range(25)]

    first = _report(runs, limit=10)
    third = _report(runs, offset=20, limit=10)

    assert (first.total, first.summary.total, first.offset, first.limit, first.has_next) == (
        25,
        25,
        0,
        10,
        True,
    )
    assert [item.run_id for item in first.items] == [f"r{index:02d}" for index in range(10)]
    assert [item.run_id for item in third.items] == [f"r{index:02d}" for index in range(20, 25)]
    beyond = _report(runs, offset=40, limit=10)
    assert (beyond.items, beyond.total, beyond.has_next) == ([], 25, False)


def test_long_periods_include_older_ratings() -> None:
    """90 日を超える期間（180・365 日）を選べる（#794）。"""
    runs = [
        _run("recent", rated_at=NOW - timedelta(days=10)),
        _run("half-year", rated_at=NOW - timedelta(days=150)),
        _run("last-year", rated_at=NOW - timedelta(days=500)),
    ]

    assert _report(runs, days=90).summary.total == 1
    year = _report(runs, days=365)
    assert year.summary.total == 2
    # 直前の 365 日（比較用）。
    assert year.previous.total == 1


@pytest.fixture
def seeded_runs() -> Iterator[None]:
    repository: Any = runtime_repository
    runs = [
        _run("feedback-774-done"),
        _run("feedback-774-running", status=RunStatus.RUNNING),
        _run("feedback-774-other", user=ALICE),
    ]
    with repository._lock:  # noqa: SLF001 - テスト用に Run を直接置く
        for run in runs:
            repository._runs[run.id] = run
    try:
        yield
    finally:
        with repository._lock:  # noqa: SLF001
            for run in runs:
                repository._runs.pop(run.id, None)


def test_user_rates_own_answer_and_can_change_it(seeded_runs: None) -> None:
    del seeded_runs
    url = "/api/runs/feedback-774-done/feedback"

    helpful = client.put(url, json={"rating": "helpful", "reason": "incorrect", "comment": "x"})
    assert helpful.status_code == 200, helpful.text
    feedback = helpful.json()["data"]["feedback"]
    # 役に立った評価には理由・コメントを残さない。
    assert (feedback["rating"], feedback["reason"], feedback["comment"]) == ("helpful", None, "")
    assert feedback["user_uuid"] == LOCAL_DEBUG_USER_UUID

    missing_reason = client.put(url, json={"rating": "not_helpful"})
    assert missing_reason.status_code == 422
    assert "理由を選んでください" in missing_reason.text
    too_long = client.put(
        url, json={"rating": "not_helpful", "reason": "incomplete", "comment": "あ" * 1001}
    )
    assert too_long.status_code == 422

    changed = client.put(
        url, json={"rating": "not_helpful", "reason": "wrong_action", "comment": " 別の表を見た "}
    )
    assert changed.status_code == 200, changed.text
    stored = runtime_repository.get_run("feedback-774-done").feedback
    assert stored is not None
    assert (stored.rating, stored.reason, stored.comment) == (
        FeedbackRating.NOT_HELPFUL,
        FeedbackReason.WRONG_ACTION,
        "別の表を見た",
    )


def test_only_answered_runs_can_be_rated_and_the_rater_is_recorded(seeded_runs: None) -> None:
    del seeded_runs
    running = client.put("/api/runs/feedback-774-running/feedback", json={"rating": "helpful"})
    assert running.status_code == 409
    # 評価は Run の持ち主だけ。system_admin（local のローカル利用者）はほかの利用者の回答を
    # 読めるが、評価は付けられない（403。#3244。ほかの利用者の Run の 404 は
    # test_security_scope.py）。
    other = client.put("/api/runs/feedback-774-other/feedback", json={"rating": "helpful"})
    assert other.status_code == 403, other.text
    assert runtime_repository.get_run("feedback-774-other").feedback is None
    # 自分の回答の評価には、評価した利用者が残る。
    own = client.put("/api/runs/feedback-774-done/feedback", json={"rating": "helpful"})
    assert own.status_code == 200, own.text
    feedback = own.json()["data"]["feedback"]
    assert (feedback["rating"], feedback["user_uuid"]) == ("helpful", LOCAL_DEBUG_USER_UUID)
    missing = client.put("/api/runs/run-missing-774/feedback", json={"rating": "helpful"})
    assert missing.status_code == 404


def test_feedback_api_returns_the_report(seeded_runs: None) -> None:
    del seeded_runs
    client.put(
        "/api/runs/feedback-774-done/feedback",
        json={"rating": "not_helpful", "reason": "incomplete", "comment": "件数が足りない"},
    )

    response = client.get(
        "/api/feedback", params={"days": 7, "rating": "not_helpful", "limit": 100}
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    item = next(item for item in data["items"] if item["run_id"] == "feedback-774-done")
    assert (item["feedback"]["reason"], item["feedback"]["comment"], item["display_name"]) == (
        "incomplete",
        "件数が足りない",
        "ローカル利用者",
    )
    assert client.get("/api/feedback", params={"days": 14}).status_code == 422
    assert client.get("/api/feedback", params={"days": 365}).status_code == 200
    assert client.get("/api/feedback", params={"limit": 101}).status_code == 422
    assert client.get("/api/feedback", params={"offset": -1}).status_code == 422
    assert client.get("/api/feedback", params={"reason": "unknown"}).status_code == 422


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_feedback_permissions(auth: ProductionAuth, seeded_runs: None) -> None:
    del seeded_runs
    # 画面の閲覧だけでは評価を付けられない（チャットの利用者 = Run の実行権限）。
    auth.user_with_permissions("viewer-774", [MENU_RUNS], agent_ids=["default"])
    viewer = login("viewer-774")
    rated = client.put(
        "/api/runs/feedback-774-done/feedback", json={"rating": "helpful"}, headers=viewer
    )
    assert rated.status_code == 403
    assert client.get("/api/feedback", headers=viewer).status_code == 403

    operator = auth.user_with_permissions("operator-774", [RUNS_OPERATE], agent_ids=["default"])
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - 作成者を作った利用者に合わせる
        repository._runs["feedback-774-done"].created_by_user_uuid = operator.user_uuid  # noqa: SLF001
    own = client.put(
        "/api/runs/feedback-774-done/feedback",
        json={"rating": "helpful"},
        headers=login("operator-774"),
    )
    assert own.status_code == 200, own.text

    auth.user_with_permissions("manager-774", [MENU_FEEDBACK], agent_ids=["sales"])
    scoped = client.get("/api/feedback", headers=login("manager-774"))
    assert scoped.status_code == 200, scoped.text
    # 利用できる業務 Agent（sales）の評価だけ。
    assert scoped.json()["data"]["items"] == []
    # 管理者の評価は Agent 管理の権限だけ（Run の実行権限では付けられない）。
    denied = client.put(
        "/api/runs/feedback-774-other/admin-review",
        json={"rating": "helpful"},
        headers=login("operator-774"),
    )
    assert denied.status_code == 403


def test_admin_reviews_any_answer_separately_from_the_owner(seeded_runs: None) -> None:
    """管理者はだれの回答にも評価を付けられ、本人の評価とは別に残る（#774）。"""
    del seeded_runs
    # 本人ではない Run（作成者は ALICE）にも管理者の評価は付けられる（local は管理者）。
    reviewed = client.put(
        "/api/runs/feedback-774-other/admin-review",
        json={"rating": "not_helpful", "reason": "incorrect", "comment": "金額が違う"},
    )
    assert reviewed.status_code == 200, reviewed.text
    run = reviewed.json()["data"]
    assert run["feedback"] is None
    assert (run["admin_review"]["rating"], run["admin_review"]["comment"]) == (
        "not_helpful",
        "金額が違う",
    )
    assert run["admin_review"]["user_uuid"] == LOCAL_DEBUG_USER_UUID
    running = client.put("/api/runs/feedback-774-running/admin-review", json={"rating": "helpful"})
    assert running.status_code == 409

    data = client.get("/api/feedback", params={"days": 7}).json()["data"]
    item = next(item for item in data["items"] if item["run_id"] == "feedback-774-other")
    assert item["feedback"] is None
    assert item["admin_review"]["reason"] == "incorrect"
    assert item["reviewer_display_name"] == "ローカル利用者"
    assert data["summary"]["admin_reviewed"] >= 1
    assert data["summary"]["admin_not_helpful"] >= 1
    # 評価・理由の絞り込みは、管理者の評価にも当てる。
    by_reason = client.get("/api/feedback", params={"days": 7, "reason": "incorrect"}).json()
    assert "feedback-774-other" in [item["run_id"] for item in by_reason["data"]["items"]]


def test_summary_counts_distinct_rated_runs_before_filtering() -> None:
    owner = _run("owner-only", rated_at=NOW)
    admin = _run("admin-only")
    admin.admin_review = RunFeedback(rating=FeedbackRating.NOT_HELPFUL, updated_at=NOW)
    both = _run("both", rated_at=NOW)
    both.admin_review = RunFeedback(rating=FeedbackRating.NOT_HELPFUL, updated_at=NOW)
    report = _report([owner, admin, both], rating=FeedbackRating.NOT_HELPFUL)
    assert report.summary.rated_runs == 3
    assert report.summary.total == 2
    assert report.summary.admin_reviewed == 2
    assert report.total == 2
    assert report.previous.rated_runs == 0
