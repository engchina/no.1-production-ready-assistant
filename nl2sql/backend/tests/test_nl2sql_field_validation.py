"""画面の必須・数値の検証と backend の検証をそろえる（#540 / #541）。

画面の検証は先回りで、正本は backend。
API を直接呼んでも、画面の欄と同じ規則・文言で、欄を指して拒否する。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.problems import validation_field_problems
from app.features.nl2sql.models import (
    AdminFeedbackReviewRequest,
    DbAdminExecuteRequest,
    ExecuteRequest,
    FeedbackRequest,
    JobCreateRequest,
)
from app.features.nl2sql.service import Nl2SqlService, StoredJob
from app.features.nl2sql.store import MemoryNl2SqlStore


def _problems(exc: pytest.ExceptionInfo[ValidationError]) -> list[tuple[str, str]]:
    return [(item.pointer, item.message) for item in validation_field_problems(exc.value.errors())]


@pytest.mark.parametrize(
    "payload",
    [
        {"history_id": "h", "rating": "bad"},
        {"history_id": "h", "rating": "bad", "feedback_content": "  ", "comment": ""},
    ],
    ids=["missing", "blank"],
)
def test_negative_feedback_requires_user_comment(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError) as exc:
        FeedbackRequest.model_validate(payload)
    assert _problems(exc) == [
        ("/feedback_content", "「違う」のときは利用者コメントを入力してください。")
    ]


def test_feedback_comment_is_optional_for_good_and_legacy_comment_is_accepted() -> None:
    assert FeedbackRequest(history_id="h", rating="good").feedback_content == ""
    legacy = FeedbackRequest.model_validate({"history_id": "h", "rating": "bad", "comment": "違う"})
    assert legacy.feedback_content == "違う"


def test_negative_admin_review_requires_comment() -> None:
    with pytest.raises(ValidationError) as exc:
        AdminFeedbackReviewRequest.model_validate({"history_id": "h", "rating": "bad"})
    assert _problems(exc) == [
        ("/feedback_content", "「違う」のときは管理者レビューコメントを入力してください。")
    ]
    assert AdminFeedbackReviewRequest(history_id="h", rating="good").feedback_content == ""


@pytest.mark.parametrize("row_limit", [0, -1, 100001], ids=["zero", "negative", "over"])
def test_sql_row_limit_message_matches_screen(row_limit: int) -> None:
    expected = [("/row_limit", "取得件数上限は 1 以上 100000 以下の整数を入力してください。")]
    with pytest.raises(ValidationError) as exc:
        ExecuteRequest(sql="SELECT 1 FROM DUAL", row_limit=row_limit)
    assert _problems(exc) == expected
    with pytest.raises(ValidationError) as admin_exc:
        DbAdminExecuteRequest(sql="SELECT 1 FROM DUAL", row_limit=row_limit)
    assert ("/row_limit", expected[0][1]) in _problems(admin_exc)


def test_sql_row_limit_omitted_keeps_api_contract() -> None:
    """API で省略した取得件数上限は「上限なし」のまま（画面は常に明示する）。"""
    assert ExecuteRequest(sql="SELECT 1 FROM DUAL").row_limit is None
    assert ExecuteRequest(sql="SELECT 1 FROM DUAL", row_limit=100000).row_limit == 100000


@pytest.mark.parametrize("generation_only", [False, True], ids=["job", "chat"])
@pytest.mark.parametrize(
    ("question", "message"),
    [
        ("", "質問を入力してください。"),
        ("   \n\t", "質問を入力してください。"),
        ("a" * 10001, "質問は 10000 文字以内で入力してください。"),
    ],
    ids=["empty", "blank", "too-long"],
)
def test_job_question_is_validated_for_every_job(
    generation_only: bool, question: str, message: str
) -> None:
    """SQL 生成画面・API・MCP のジョブも、チャットと同じ規則・文言で欄を指して拒否する（#1054）。"""
    with pytest.raises(ValidationError) as exc:
        JobCreateRequest(question=question, generation_only=generation_only)
    assert _problems(exc) == [("/question", message)]


def test_job_question_accepts_the_upper_limit() -> None:
    assert len(JobCreateRequest(question="a" * 10000).question) == 10000


@pytest.mark.parametrize("question", ["   ", "a" * 20000], ids=["blank", "too-long"])
def test_stored_job_snapshot_keeps_questions_accepted_before(question: str) -> None:
    """以前の規則で受け付けて保存したジョブは、読み戻しで新しい検証に掛けない（#1054）。"""
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    request = JobCreateRequest.model_construct(**JobCreateRequest(question="件数").__dict__)
    request.question = question
    snapshot = service._job_to_snapshot(StoredJob(job_id="legacy", request=request))  # noqa: SLF001

    restored = service._job_from_snapshot(snapshot)  # noqa: SLF001

    assert restored.request.question == question
