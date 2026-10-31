"""MCP サーバー（`POST /api/mcp`、#231）の回帰テスト。

LLM と Oracle は呼ばない。ツールの判定は fake の service で、ローカル実行モードの 1 件だけ
決定論（deterministic）の実 service で確認する。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from pr_system_settings.auth.service_token import issue_service_token
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_CODE,
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
)

from app.features.mcp import tools as mcp_tools
from app.features.nl2sql import ontology_router
from app.features.nl2sql.models import (
    JobCreateData,
    JobCreateRequest,
    JobData,
    JobOrigin,
    JobStatus,
    Nl2SqlEngine,
    Nl2SqlProfile,
    Nl2SqlResult,
    ProfileRecommendationCandidate,
    ProfileRecommendationData,
    ProfileRecommendationRequest,
    ProfileSummary,
    ProfileSummaryPage,
    QueryResults,
    SafetyReport,
    SqlRequirementSpec,
    TimingEnvelope,
)
from app.features.nl2sql.ontology_models import (
    ClarificationMode,
    QuerySession,
    QuerySessionStatus,
    RequirementPhase,
    RequirementState,
    SqlRequirementDraft,
)
from app.features.nl2sql.ontology_router import (
    QuerySessionApiCreate,
    QuerySessionData,
    RequirementApplyRequest,
)
from app.features.nl2sql.service import JobCancelOutcome, Nl2SqlService, StoredJob
from app.main import app
from app.security.domain import UserRecord
from app.security.passwords import hash_password
from app.security.permissions import permission_for_route
from app.security.service import SecurityService, reset_security_service
from app.security.store import InMemorySecurityStore
from app.settings import get_settings

SECRET = "mcp-test-secret-" + "x" * 32
PASSWORD = "McpUserPass!12345"
CREATED_AT = "2026-09-27T00:00:00+00:00"


# ---------------------------------------------------------------- fake service


class _FakeNl2SqlService:
    """MCP ツールが使う service の関数だけを持つ fake。ジョブの所有者判定は実装を使う。"""

    def __init__(self) -> None:
        self.profiles = [
            Nl2SqlProfile(id="default", name="既定", category="共通", description="既定の範囲"),
            Nl2SqlProfile(id="sales", name="売上", category="営業", description="売上の分析"),
            Nl2SqlProfile(id="hr", name="人事", category="人事", description="社員の情報"),
        ]
        self.started: list[tuple[JobCreateRequest, str, bool]] = []
        # ジョブのきっかけ（MCP の入口が渡す。#1986）。`started` と同じ順。
        self.origins: list[JobOrigin | None] = []
        self.jobs: dict[str, StoredJob] = {}
        # start_job で作るジョブを get_job 何回目で完了させるか（None は完了させない）。
        self.finish_after_polls: int | None = 0
        self.polls: dict[str, int] = {}
        # nl2sql_cancel_job が送った取消の要求（job_id・操作した人・入口。#3318）。
        self.cancels: list[tuple[str, str, dict[str, str]]] = []
        self.recommendation = ProfileRecommendationData(
            recommended_profile_id="sales",
            recommended_profile_name="売上",
            confidence=0.9,
            reason="売上の語に一致しました。",
            rewritten_question="営業部の今月の売上",
            recommended_allowed_objects={"table_names": [], "view_names": []},
            candidates=[
                ProfileRecommendationCandidate(
                    profile_id="sales", profile_name="売上", score=0.9, matched_terms=["売上"]
                ),
                ProfileRecommendationCandidate(
                    profile_id="hr", profile_name="人事", score=0.2, matched_terms=[]
                ),
            ],
        )

    def ensure_persistence_available(self) -> None:
        return None

    def list_profiles(self, *, include_archived: bool = False) -> list[Nl2SqlProfile]:
        return list(self.profiles)

    def get_profile(self, profile_id: str, **_kwargs: Any) -> Nl2SqlProfile:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        raise KeyError(profile_id)

    def search_profiles(
        self,
        *,
        cursor: str | None,
        limit: int,
        query: str,
        include_archived: bool,
        allowed_profile_ids: set[str] | None = None,
    ) -> ProfileSummaryPage:
        items = [
            ProfileSummary(
                id=profile.id,
                name=profile.name,
                category=profile.category,
                description=profile.description,
            )
            for profile in self.profiles
            if (allowed_profile_ids is None or profile.id in allowed_profile_ids)
            and (not query or query in profile.name or query in profile.description)
        ]
        return ProfileSummaryPage(items=items[:limit], total=len(items))

    def recommend_profile(
        self,
        request: ProfileRecommendationRequest,
        *,
        allowed_profile_ids: set[str] | None = None,
    ) -> ProfileRecommendationData:
        return self.recommendation

    def start_job(
        self,
        request: JobCreateRequest,
        *,
        actor_user_uuid: str = "",
        actor_is_system_admin: bool = False,
        origin: JobOrigin | None = None,
    ) -> JobCreateData:
        job_id = str(uuid.uuid4())
        # 実装と同じく、生成の方式を省略したジョブは業務プロファイルの既定にする（#1524）。
        profile = next(
            item for item in self.profiles if item.id == (request.profile_id or "default")
        )
        request = request.with_profile_default_engine(profile.default_engine)
        self.started.append((request, actor_user_uuid, actor_is_system_admin))
        self.origins.append(origin)
        self.jobs[job_id] = StoredJob(
            job_id=job_id, request=request, actor_user_uuid=actor_user_uuid
        )
        return JobCreateData(job_id=job_id, status=JobStatus.PENDING, created_at=CREATED_AT)

    def get_job(
        self,
        job_id: str,
        *,
        actor_user_uuid: str = "",
        actor_can_manage: bool = False,
    ) -> JobData | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        Nl2SqlService._assert_job_actor_access(  # noqa: SLF001 - 所有者判定は実装を使う
            job, actor_user_uuid=actor_user_uuid, actor_can_manage=actor_can_manage
        )
        polls = self.polls.get(job_id, 0) + 1
        self.polls[job_id] = polls
        if self.finish_after_polls is not None and polls > self.finish_after_polls:
            job.status = JobStatus.DONE
            job.result = _result(job.request)
        return JobData(
            job_id=job.job_id,
            status=job.status,
            profile_id=job.request.profile_id or "default",
            created_at=CREATED_AT,
            result=job.result,
            error_code=job.error_code,
            error_message=job.error_message,
            error_detail=job.error_detail,
        )

    def cancel_job(
        self,
        job_id: str,
        *,
        actor_user_uuid: str = "",
        actor_can_manage: bool = False,
        via: dict[str, str] | None = None,
    ) -> JobCancelOutcome | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        Nl2SqlService._assert_job_actor_access(  # noqa: SLF001 - 所有者判定は実装を使う
            job, actor_user_uuid=actor_user_uuid, actor_can_manage=actor_can_manage
        )
        in_flight = job.status in {JobStatus.PENDING, JobStatus.RUNNING}
        already = in_flight and job.cancel_requested
        if in_flight:
            job.cancel_requested = True
            self.cancels.append((job_id, actor_user_uuid, dict(via or {})))
        return JobCancelOutcome(
            job=JobData(
                job_id=job.job_id,
                status=job.status,
                profile_id=job.request.profile_id or "default",
                created_at=CREATED_AT,
                error_code=job.error_code,
            ),
            already_requested=already,
        )


def _result(request: JobCreateRequest) -> Nl2SqlResult:
    return Nl2SqlResult(
        history_id="history-1",
        engine=Nl2SqlEngine.SELECT_AI,
        original_question=request.question,
        rewritten_question=request.question,
        generated_sql="SELECT EMPLOYEE_ID FROM EMPLOYEE",
        executable_sql="SELECT EMPLOYEE_ID FROM EMPLOYEE FETCH FIRST 2 ROWS ONLY",
        explanation="社員 ID を取得します。",
        safety=SafetyReport(
            is_safe=True,
            is_select_only=True,
            row_limit_applied=request.row_limit or 0,
            warnings=["行数を制限しました。"],
        ),
        results=QueryResults(
            columns=["EMPLOYEE_ID"],
            rows=[{"EMPLOYEE_ID": 1}, {"EMPLOYEE_ID": 2}],
            total=2,
            has_more=True,
        ),
        timing=TimingEnvelope(created_at=CREATED_AT),
    )


class _ReadyRequirementRuntime:
    """回答前の確認（#1441）で確かめる項目が無く、すぐ SQL の生成に進める fake の runtime。

    確認の項目を返す流れは `test_nl2sql_guided_clarification.py` が実の runtime で確かめる。
    """

    def __init__(self) -> None:
        self.sessions: dict[str, QuerySession] = {}
        self.created: list[QuerySessionApiCreate] = []

    def _data(self, session: QuerySession) -> QuerySessionData:
        state = RequirementState(
            status=RequirementPhase.READY,
            select_from_clear=True,
            can_apply=True,
            summary_ja="- 使う表（FROM）: APP.EMPLOYEE",
        )
        # 画面のための profile view・graph は MCP が使わないので作らない。
        return QuerySessionData.model_construct(  # type: ignore[call-arg]
            session=session, requirement_state=state
        )

    def create_session(
        self,
        request: QuerySessionApiCreate,
        *,
        actor_user_uuid: str = "",
        actor_is_system_admin: bool = False,
        profile_confirmed: bool = False,
    ) -> QuerySessionData:
        assert profile_confirmed is True
        assert request.clarification_mode == ClarificationMode.GUIDED
        self.created.append(request)
        session = QuerySession(
            id=f"qs-{len(self.sessions) + 1}",
            profile_id=request.profile_id,
            profile_view_id="view-1",
            ontology_revision_id="revision-1",
            original_question=request.question,
            clarification_mode=ClarificationMode.GUIDED,
            requirement=SqlRequirementDraft(
                question_original=request.question,
                clarified_question_ja=f"{request.question}（APP.EMPLOYEE）",
            ),
            actor_user_uuid=actor_user_uuid,
        )
        self.sessions[session.id] = session
        return self._data(session)

    def get_session(self, session_id: str) -> QuerySessionData:
        return self._data(self.sessions[session_id])

    def apply_requirement(
        self, session_id: str, request: RequirementApplyRequest
    ) -> QuerySessionData:
        session = self.sessions[session_id].model_copy(
            update={"status": QuerySessionStatus.DONE, "requirement_applied_at": CREATED_AT}
        )
        self.sessions[session_id] = session
        return self._data(session)

    def applied_requirement_spec(
        self,
        session_id: str,
        *,
        actor_user_uuid: str,
        actor_is_system_admin: bool,
        profile_id: str,
        question: str,
        chat: bool = False,
        previous_job_id: str | None = None,
    ) -> SqlRequirementSpec:
        # MCP の nl2sql_query は SQL 生成の画面と同じ（チャットのターンではない）要件で
        # job を作る（#1442）。
        assert chat is False and previous_job_id is None
        session = self.sessions[session_id]
        assert session.requirement_applied_at is not None
        # job の質問の文は利用者の質問のまま（書き直した文は生成にだけ使う。#1727）。
        assert question == session.original_question
        return SqlRequirementSpec(
            session_id=session_id,
            profile_id=profile_id,
            clarified_question=session.requirement.clarified_question_ja,  # type: ignore[union-attr]
            tables=["APP.EMPLOYEE"],
        )


# ---------------------------------------------------------------- auth setup


async def _inline_threadpool(function: Callable[..., object], *args: object) -> object:
    return function(*args)


class _Users:
    def __init__(self, service: SecurityService) -> None:
        self.service = service
        self.admin, _, _ = service.login("ADMIN", "BootstrapPass!123")
        self._roles: dict[str, str] = {}

    def create(
        self,
        login_user_id: str,
        *,
        permissions: set[str],
        allowed_profile_ids: set[str] | None = None,
    ) -> UserRecord:
        role = self.service.create_role(
            role_code=login_user_id.upper().replace(".", "_"),
            display_name=login_user_id,
            description="",
            permissions=permissions,
            entitlements=[],
            allowed_profile_ids=allowed_profile_ids or set(),
            actor=self.admin,
        )
        user, _ = self.service.create_user(
            login_user_id=login_user_id,
            display_name=login_user_id,
            role_ids=[role.role_id],
            temporary_password=PASSWORD,
            actor=self.admin,
        )
        self.service.store.set_password(user.user_uuid, hash_password(PASSWORD), force_change=False)
        active = self.service.store.get_user(user.user_uuid)
        assert active is not None
        return active

    def system_admin(self) -> UserRecord:
        """構成管理者（SYSTEM_ADMIN のロール。初回のパスワード変更を済ませた状態にする）。"""
        self.service.store.set_password(
            self.admin.user_uuid, hash_password(PASSWORD), force_change=False
        )
        active = self.service.store.get_user(self.admin.user_uuid)
        assert active is not None
        return active


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> _FakeNl2SqlService:
    service = _FakeNl2SqlService()
    monkeypatch.setattr(mcp_tools, "nl2sql_service", service)
    monkeypatch.setattr(mcp_tools, "JOB_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(ontology_router, "ontology_runtime", _ReadyRequirementRuntime())
    return service


@pytest.fixture
def users(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Users]:
    monkeypatch.setattr("app.security.dependencies.run_in_threadpool", _inline_threadpool)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "authenticated")
    monkeypatch.setattr(settings, "app_service_token_secret", SECRET)
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "memory")
    reset_security_service()
    from app.security.service import get_security_service

    service = get_security_service()
    assert isinstance(service.store, InMemorySecurityStore)
    assert service.store.bootstrap(
        login_user_id="ADMIN",
        display_name="ADMIN（システム管理者）",
        password_hash=hash_password("BootstrapPass!123"),
    )
    try:
        yield _Users(service)
    finally:
        reset_security_service()


def _token(
    user_uuid: str,
    *,
    audience: str = "nl2sql",
    secret: str = SECRET,
    profile_ids: list[str] | None = None,
    claims: dict[str, str] | None = None,
) -> str:
    return issue_service_token(
        secret,
        subject=user_uuid,
        audience=audience,
        issuer="agent",
        claims={"run_id": "run-1", "agent_id": "agent-1", **(claims or {})},
        profile_ids=profile_ids,
    )


def _rpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> Any:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def _call(name: str, arguments: dict[str, Any]) -> Any:
    return _rpc("tools/call", {"name": name, "arguments": arguments})


async def _post(body: Any, *, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/mcp", json=body, headers=headers)


def _run(body: Any, *, token: str | None = None) -> httpx.Response:
    return asyncio.run(_post(body, token=token))


def _tool_names(response: httpx.Response) -> set[str]:
    assert response.status_code == 200, response.text
    return {tool["name"] for tool in response.json()["result"]["tools"]}


def _structured(response: httpx.Response) -> tuple[bool, dict[str, Any]]:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result["isError"], result["structuredContent"]


# ---------------------------------------------------------------- tests


def test_mcp_route_is_authenticated_without_route_permission() -> None:
    # 起動時の未分類 route 検査と manifest 完全性テストを通し、権限はツールごとに判定する。
    assert permission_for_route("POST", "/mcp") is None
    assert "/api/mcp" in app.openapi()["paths"]


def test_missing_or_invalid_token_is_401(users: _Users, fake_service: _FakeNl2SqlService) -> None:
    user = users.create("mcp.query", permissions={"menu.query"})

    assert _run(_rpc("tools/list")).status_code == 401
    assert _run(_rpc("tools/list"), token="not-a-token").status_code == 401
    wrong_secret = _token(user.user_uuid, secret="other-secret-" + "y" * 32)
    assert _run(_rpc("tools/list"), token=wrong_secret).status_code == 401
    rag_token = _token(user.user_uuid, audience="rag")
    assert _run(_rpc("tools/list"), token=rag_token).status_code == 401


def test_session_cookie_does_not_authenticate_mcp(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/api/auth/login",
                json={"login_user_id": "ADMIN", "password": "BootstrapPass!123"},
            )
            assert login.status_code == 200
            return await client.post("/api/mcp", json=_rpc("tools/list"))

    assert asyncio.run(exercise()).status_code == 401


def test_tools_list_follows_token_user_permissions(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    query_user = users.create("mcp.query", permissions={"menu.query"})
    evaluation_user = users.create("mcp.evaluation", permissions={"menu.evaluation"})
    direct_user = users.create("mcp.direct", permissions={"menu.direct_sql"})

    initialize = _run(
        _rpc("initialize", {"protocolVersion": "2025-06-18"}), token=_token(query_user.user_uuid)
    )
    assert initialize.json()["result"]["serverInfo"]["name"] == "production-ready-nl2sql"

    assert _tool_names(_run(_rpc("tools/list"), token=_token(query_user.user_uuid))) == {
        "nl2sql_list_profiles",
        "nl2sql_recommend_profile",
        "nl2sql_query",
        "nl2sql_get_job",
        "nl2sql_cancel_job",
        "nl2sql_cancel_requirement",
    }
    # SQL 生成はできるが実行できない利用者には nl2sql_query を出さない。
    assert _tool_names(_run(_rpc("tools/list"), token=_token(evaluation_user.user_uuid))) == {
        "nl2sql_list_profiles",
        "nl2sql_recommend_profile",
        "nl2sql_get_job",
        "nl2sql_cancel_job",
        "nl2sql_cancel_requirement",
    }
    assert _tool_names(_run(_rpc("tools/list"), token=_token(direct_user.user_uuid))) == set()


def test_query_requires_generate_and_execute_permissions(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    evaluation_user = users.create(
        "mcp.evaluation", permissions={"menu.evaluation"}, allowed_profile_ids={"default"}
    )
    direct_user = users.create(
        "mcp.direct", permissions={"menu.direct_sql"}, allowed_profile_ids={"default"}
    )

    for user in (evaluation_user, direct_user):
        is_error, content = _structured(
            _run(_call("nl2sql_query", {"question": "社員一覧"}), token=_token(user.user_uuid))
        )
        assert is_error is True
        assert content["error_code"] == "MCP_TOOL_FORBIDDEN"
    assert fake_service.started == []


def test_list_profiles_returns_only_allowed_profiles(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    user = users.create("mcp.query", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    token = _token(user.user_uuid)

    is_error, content = _structured(_run(_call("nl2sql_list_profiles", {}), token=token))
    assert is_error is False
    assert content == {
        "schema_version": mcp_tools.MCP_OUTPUT_SCHEMA_VERSION,
        "profiles": [
            {"id": "sales", "name": "売上", "category": "営業", "description": "売上の分析"}
        ],
    }
    is_error, content = _structured(
        _run(_call("nl2sql_list_profiles", {"limit": 101}), token=token)
    )
    assert is_error is True
    assert content["error_code"] == "MCP_TOOL_ARGUMENTS_INVALID"


def test_recommend_profile_respects_scope_and_threshold(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    sales_user = users.create(
        "mcp.sales", permissions={"menu.query"}, allowed_profile_ids={"sales"}
    )
    hr_user = users.create("mcp.hr", permissions={"menu.query"}, allowed_profile_ids={"hr"})
    call = _call("nl2sql_recommend_profile", {"question": "今月の売上"})

    is_error, content = _structured(_run(call, token=_token(sales_user.user_uuid)))
    assert is_error is False
    assert content == {
        "schema_version": mcp_tools.MCP_OUTPUT_SCHEMA_VERSION,
        "recommended_profile_id": "sales",
        "rewritten_question": "営業部の今月の売上",
        "candidates": [{"id": "sales", "name": "売上", "reason": "売上", "score": 0.9}],
    }

    # 範囲外の推薦は返さない。
    _, content = _structured(_run(call, token=_token(hr_user.user_uuid)))
    assert content["recommended_profile_id"] is None
    assert content["rewritten_question"] is None
    assert [candidate["id"] for candidate in content["candidates"]] == ["hr"]

    # しきい値未満は画面と同じく推薦しない。
    fake_service.recommendation.confidence = 0.1
    _, content = _structured(_run(call, token=_token(sales_user.user_uuid)))
    assert content["recommended_profile_id"] is None


@pytest.mark.parametrize("tool", ["nl2sql_query", "nl2sql_recommend_profile"])
def test_blank_question_is_invalid_arguments(
    users: _Users, fake_service: _FakeNl2SqlService, tool: str
) -> None:
    """空白だけの質問は引数の誤りにする（ジョブを作らず、内部エラーにもしない。#1054）。"""
    user = users.create("mcp.query", permissions={"menu.query"}, allowed_profile_ids={"sales"})

    is_error, content = _structured(
        _run(
            _call(
                tool,
                {"question": " \n\t", "profile_id": "sales"}
                if tool == "nl2sql_query"
                else {"question": " \n\t"},
            ),
            token=_token(user.user_uuid),
        )
    )

    assert is_error is True, content
    assert content["error_code"] == "MCP_TOOL_ARGUMENTS_INVALID"
    assert [error["loc"] for error in content["details"]["errors"]] == ["question"]
    assert "質問を入力してください。" in content["details"]["errors"][0]["message"]
    assert fake_service.started == []


def test_query_selects_the_only_usable_profile_for_an_unusable_id(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    """使えない profile_id は推測のまま失敗させず、使える 1 つで続ける（#2355）。"""
    user = users.create("mcp.query", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    token = _token(user.user_uuid)

    for requested in ("hr", "推測した id"):
        is_error, content = _structured(
            _run(
                _call("nl2sql_query", {"question": "社員一覧", "profile_id": requested}),
                token=token,
            )
        )
        assert is_error is False, content
        assert content["profile_id"] == "sales"
        assert content["auto_selected_profile"] == {
            "id": "sales",
            "name": "売上",
            "requested_id": requested,
            "method": "only_usable",
        }
    assert {started.profile_id for started, _, _ in fake_service.started} == {"sales"}


@pytest.mark.parametrize("profile_id", [None, "", "  "], ids=["omitted", "empty", "blank"])
def test_query_without_profile_id_uses_the_recommended_profile(
    users: _Users, fake_service: _FakeNl2SqlService, profile_id: str | None
) -> None:
    """profile_id が無ければ "default" に黙って切り替えず（#1379）、質問の推薦で選ぶ（#2355）。"""
    user = users.create(
        "mcp.query", permissions={"menu.query"}, allowed_profile_ids={"default", "sales"}
    )
    arguments: dict[str, Any] = {"question": "社員一覧"}
    if profile_id is not None:
        arguments["profile_id"] = profile_id

    is_error, content = _structured(
        _run(_call("nl2sql_query", arguments), token=_token(user.user_uuid))
    )

    assert is_error is False, content
    assert content["profile_id"] == "sales"
    assert content["auto_selected_profile"] == {
        "id": "sales",
        "name": "売上",
        "requested_id": None,
        "method": "recommended",
    }
    assert [started.profile_id for started, _, _ in fake_service.started] == ["sales"]


def test_query_without_a_decided_profile_returns_candidates(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    """推薦が決まらなければ、候補と次の手の付いたエラーにする（"default" にしない。#2355）。"""
    user = users.create(
        "mcp.query", permissions={"menu.query"}, allowed_profile_ids={"default", "sales", "hr"}
    )
    fake_service.recommendation.confidence = 0.1

    is_error, content = _structured(
        _run(
            _call("nl2sql_query", {"question": "社員一覧", "profile_id": "default-guess"}),
            token=_token(user.user_uuid),
        )
    )

    assert is_error is True
    assert content["error_code"] == "NL2SQL_PROFILE_NOT_SELECTED"
    assert content["status"] == 404
    assert "「default-guess」" in content["message"]
    assert "（id: sales）" in content["message"]
    assert content["details"]["candidates"] == [
        {"id": "sales", "name": "売上"},
        {"id": "hr", "name": "人事"},
    ]
    assert fake_service.started == []


def test_profile_scope_claim_limits_query_list_and_recommend(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    """claim（業務 Agent のデータの範囲。#1379）があれば「利用者の権限 ∩ claim」で判定する。

    Agent の Runtime を通さず POST /api/mcp を直接呼んでも、範囲外は 403 になる。
    """
    user = users.create(
        "mcp.query", permissions={"menu.query"}, allowed_profile_ids={"default", "sales", "hr"}
    )
    token = _token(user.user_uuid, profile_ids=["sales", "finance"])

    is_error, content = _structured(
        _run(_call("nl2sql_query", {"question": "社員一覧", "profile_id": "hr"}), token=token)
    )
    assert is_error is True
    assert (content["status"], content["error_code"]) == (403, "PROFILE_SCOPE_FORBIDDEN")
    assert "hr" in content["message"]
    assert fake_service.started == []
    # claim にあっても利用者が使えない業務プロファイルは使わず、権限 ∩ claim の中（sales だけ）
    # から選び直す（#2355）。
    is_error, content = _structured(
        _run(_call("nl2sql_query", {"question": "売上", "profile_id": "finance"}), token=token)
    )
    assert is_error is False, content
    assert content["auto_selected_profile"]["id"] == "sales"
    assert [started.profile_id for started, _, _ in fake_service.started] == ["sales"]

    is_error, content = _structured(
        _run(_call("nl2sql_query", {"question": "売上", "profile_id": "sales"}), token=token)
    )
    assert (is_error, content["profile_id"]) == (False, "sales")

    _, listed = _structured(_run(_call("nl2sql_list_profiles", {}), token=token))
    assert [item["id"] for item in listed["profiles"]] == ["sales"]

    # 推薦は範囲の中からだけ（fake の推薦は sales・候補は sales / hr）。
    _, recommended = _structured(
        _run(_call("nl2sql_recommend_profile", {"question": "人事の売上"}), token=token)
    )
    assert recommended["recommended_profile_id"] == "sales"
    assert [item["id"] for item in recommended["candidates"]] == ["sales"]
    # 候補の一覧が範囲と重ならなければ推薦しない（範囲の外を推薦しない）。
    _, recommended = _structured(
        _run(
            _call("nl2sql_recommend_profile", {"question": "人事", "profile_ids": ["hr"]}),
            token=token,
        )
    )
    assert recommended == {
        "schema_version": mcp_tools.MCP_OUTPUT_SCHEMA_VERSION,
        "recommended_profile_id": None,
        "rewritten_question": None,
        "candidates": [],
    }


def test_recommend_profile_limits_to_requested_candidates(
    users: _Users, fake_service: _FakeNl2SqlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """profile_ids（候補の一覧。#1379）を渡すと、その中からだけ推薦する。"""
    user = users.create(
        "mcp.query", permissions={"menu.query"}, allowed_profile_ids={"default", "sales", "hr"}
    )
    received: list[set[str] | None] = []
    original = fake_service.recommend_profile

    def recommend(
        request: ProfileRecommendationRequest, *, allowed_profile_ids: set[str] | None = None
    ) -> ProfileRecommendationData:
        received.append(allowed_profile_ids)
        return original(request, allowed_profile_ids=allowed_profile_ids)

    monkeypatch.setattr(fake_service, "recommend_profile", recommend)
    token = _token(user.user_uuid)

    _, content = _structured(
        _run(
            _call(
                "nl2sql_recommend_profile",
                {"question": "人事の売上", "profile_ids": ["hr", "unknown"]},
            ),
            token=token,
        )
    )
    # 利用者が使えない ID（unknown）は候補にしない。推薦（sales）は候補の外なので null。
    assert received == [{"hr"}]
    assert content["recommended_profile_id"] is None
    assert [item["id"] for item in content["candidates"]] == ["hr"]

    empty = _call("nl2sql_recommend_profile", {"question": "売上", "profile_ids": []})
    is_error, content = _structured(_run(empty, token=token))
    assert (is_error, content["error_code"]) == (True, "MCP_TOOL_ARGUMENTS_INVALID")


def test_query_with_sql_statement_runs_requirement_check_like_any_question(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    """質問が SQL の文でも、ほかの質問と同じに回答前の確認を行う（#1727）。

    判断は SQL を 1 つに決める最低限の情報があるかだけで、SQL の文かどうかでは分けない。
    """
    user = users.create("mcp.sql", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    runtime = ontology_router.ontology_runtime
    assert isinstance(runtime, _ReadyRequirementRuntime)

    is_error, content = _structured(
        _run(
            _call("nl2sql_query", {"question": "select * from employee;", "profile_id": "sales"}),
            token=_token(user.user_uuid),
        )
    )
    assert is_error is False
    assert [request.question for request in runtime.created] == ["select * from employee;"]
    request, _, _ = fake_service.started[0]
    assert request.requirement_session_id == "qs-1"
    assert content["requirement"]["status"] == "applied"


def test_query_passes_row_limit_and_actor_and_returns_result(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    user = users.create("mcp.query", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    token = _token(user.user_uuid)

    is_error, content = _structured(
        _run(_call("nl2sql_query", {"question": "売上一覧", "profile_id": "sales"}), token=token)
    )
    assert is_error is False
    request, actor_user_uuid, actor_is_system_admin = fake_service.started[0]
    # 既定の row_limit（100）を必ず渡す（None だと全件取得になる）。
    assert request.row_limit == 100
    assert request.profile_id == "sales"
    assert request.use_ontology_context is True
    # 確かめた要件（書き直した質問の文と構造化した要件・表の範囲）で SQL を作る（#1441）。質問の文は
    # 利用者の質問のままにし、書き直した文は生成にだけ使う（#1727）。
    assert request.question == "売上一覧"
    assert request.generation_question == "売上一覧（APP.EMPLOYEE）"
    assert request.requirement_session_id == "qs-1"
    assert request.sql_requirement is not None
    assert request.allowed_objects.table_names == ["APP.EMPLOYEE"]
    assert actor_user_uuid == user.user_uuid
    assert actor_is_system_admin is False
    assert content == {
        "schema_version": mcp_tools.MCP_OUTPUT_SCHEMA_VERSION,
        "job_id": content["job_id"],
        "status": "done",
        "profile_id": "sales",
        "generated_sql": "SELECT EMPLOYEE_ID FROM EMPLOYEE",
        "executable_sql": "SELECT EMPLOYEE_ID FROM EMPLOYEE FETCH FIRST 2 ROWS ONLY",
        "explanation": "社員 ID を取得します。",
        "is_safe": True,
        "safety_issues": ["行数を制限しました。"],
        "safety_check": "checked",
        "columns": ["EMPLOYEE_ID"],
        "rows": [{"EMPLOYEE_ID": 1}, {"EMPLOYEE_ID": 2}],
        "returned_count": 2,
        "total": 2,
        "has_more": True,
        "truncated": False,
        "history_id": "history-1",
        "error_code": None,
        "error_message": None,
        "requirement": {
            "session_id": "qs-1",
            "status": "applied",
            "question": None,
            "remaining_count": 0,
            "answered_count": 0,
            "select_from_clear": True,
            "minimum_missing": [],
            "summary": "- 使う表（FROM）: APP.EMPLOYEE",
            "clarified_question": "売上一覧（APP.EMPLOYEE）",
            "sql_skeleton": "",
            "message": "",
        },
        "auto_selected_profile": None,
        # API キーではない呼び出し（local）には回答の注意書きを返さない（#2405）。
        "notice": None,
    }

    _run(
        _call("nl2sql_query", {"question": "売上", "profile_id": "sales", "row_limit": 1000}),
        token=token,
    )
    assert fake_service.started[1][0].row_limit == 1000
    for invalid in ({"row_limit": 1001}, {"row_limit": 0}, {"wait_seconds": 46}):
        is_error, content = _structured(
            _run(
                _call("nl2sql_query", {"question": "売上", "profile_id": "sales", **invalid}),
                token=token,
            )
        )
        assert is_error is True
        assert content["error_code"] == "MCP_TOOL_ARGUMENTS_INVALID"
    assert len(fake_service.started) == 2


def test_query_uses_profile_default_engine(users: _Users, fake_service: _FakeNl2SqlService) -> None:
    """nl2sql_query は生成の方式を指定せず、業務プロファイルの既定の生成方式で作る（#1524）。"""

    user = users.create("mcp.engine", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    token = _token(user.user_uuid)
    for engine in Nl2SqlEngine:
        fake_service.profiles[1] = fake_service.profiles[1].model_copy(
            update={"default_engine": engine}
        )
        is_error, _content = _structured(
            _run(
                _call("nl2sql_query", {"question": "売上一覧", "profile_id": "sales"}), token=token
            )
        )
        assert is_error is False
        assert fake_service.started[-1][0].engine == engine
    assert len(fake_service.started) == len(Nl2SqlEngine)


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({}, JobOrigin(run_source="mcp", agent_run_id="run-1")),
        ({"run_source": "user"}, JobOrigin(run_source="mcp", agent_run_id="run-1")),
        ({"run_source": "evaluation"}, JobOrigin(run_source="evaluation", agent_run_id="run-1")),
    ],
    ids=["agent-without-source", "agent-user-run", "agent-evaluation-run"],
)
def test_query_records_the_origin_from_the_service_token(
    users: _Users,
    fake_service: _FakeNl2SqlService,
    claims: dict[str, str],
    expected: JobOrigin,
) -> None:
    """Agent の Run の呼び出しは MCP、品質評価の Run（claim `run_source`）は品質評価（#1986）。"""
    user = users.create("mcp.origin", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    token = _token(user.user_uuid, claims=claims)

    is_error, _content = _structured(
        _run(_call("nl2sql_query", {"question": "売上一覧", "profile_id": "sales"}), token=token)
    )

    assert is_error is False
    assert fake_service.origins == [expected]


def test_query_records_the_api_key_as_mcp(users: _Users, fake_service: _FakeNl2SqlService) -> None:
    """外部の MCP クライアント（API キー）は MCP とキーの名前（#1986）。

    API キーの呼び出しは claim を持たないので、品質評価と偽れない。
    """
    user = users.create("mcp.key", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    token = _api_key(users, user)
    # 引数に Agent の claim と同じ名前の値を入れても、種類は変わらない（引数は MCP の契約の外）。
    is_error, content = _structured(
        _run(
            _call(
                "nl2sql_query",
                {"question": "売上一覧", "profile_id": "sales", "run_source": "evaluation"},
            ),
            token=token,
        )
    )
    assert is_error is True
    assert content["error_code"] == "MCP_TOOL_ARGUMENTS_INVALID"

    is_error, _content = _structured(
        _run(_call("nl2sql_query", {"question": "売上一覧", "profile_id": "sales"}), token=token)
    )
    assert is_error is False
    assert fake_service.origins == [JobOrigin(run_source="mcp", api_key_name="Claude Code")]


def test_query_returns_pending_job_after_wait_and_get_job_continues(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    user = users.create("mcp.query", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    token = _token(user.user_uuid)
    fake_service.finish_after_polls = None

    is_error, content = _structured(
        _run(
            _call("nl2sql_query", {"question": "売上", "profile_id": "sales", "wait_seconds": 0}),
            token=token,
        )
    )
    assert is_error is False
    assert content["status"] == "pending"
    assert content["rows"] == []
    job_id = content["job_id"]

    # 1 秒待っても終わらなければ pending のまま返す。
    is_error, content = _structured(
        _run(_call("nl2sql_get_job", {"job_id": job_id, "wait_seconds": 1}), token=token)
    )
    assert (is_error, content["status"], content["job_id"]) == (False, "pending", job_id)
    assert fake_service.polls[job_id] > 2  # 待ちの間は polling する

    fake_service.finish_after_polls = 0
    _, content = _structured(_run(_call("nl2sql_get_job", {"job_id": job_id}), token=token))
    assert content["status"] == "done"
    assert content["returned_count"] == 2


@pytest.mark.parametrize(
    ("stored_code", "expected"),
    [("ORA-04027", "ORA-04027"), (None, "NL2SQL_JOB_FAILED")],
    ids=["oracle-code", "legacy-null"],
)
def test_failed_job_returns_error_code(
    users: _Users,
    fake_service: _FakeNl2SqlService,
    stored_code: str | None,
    expected: str,
) -> None:
    """失敗したジョブの error_code は null にしない（#847）。"""
    user = users.create("mcp.failed", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    token = _token(user.user_uuid)
    fake_service.finish_after_polls = None
    _, content = _structured(
        _run(
            _call("nl2sql_query", {"question": "売上", "profile_id": "sales", "wait_seconds": 0}),
            token=token,
        )
    )
    job = fake_service.jobs[content["job_id"]]
    job.status = JobStatus.ERROR
    job.error_code = stored_code
    job.error_message = "SQL の生成に失敗しました。"
    job.error_detail = "ORA-04027: self-deadlock"

    is_error, content = _structured(
        _run(_call("nl2sql_get_job", {"job_id": job.job_id}), token=token)
    )
    assert is_error is False
    assert content["status"] == "error"
    assert content["error_code"] == expected
    # 画面では「詳細」に分ける元の文も、MCP では文の後ろに付けて返す（#1072）。
    assert content["error_message"] == "SQL の生成に失敗しました。\n詳細: ORA-04027: self-deadlock"


def test_get_job_hides_other_users_jobs(users: _Users, fake_service: _FakeNl2SqlService) -> None:
    owner = users.create("mcp.owner", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    # フィードバック管理者は画面では他人のジョブを見られるが、MCP では本人のジョブだけ。
    other = users.create(
        "mcp.other",
        permissions={"menu.query", "menu.feedback_management"},
        allowed_profile_ids={"sales"},
    )
    _, content = _structured(
        _run(
            _call("nl2sql_query", {"question": "売上", "profile_id": "sales"}),
            token=_token(owner.user_uuid),
        )
    )
    job_id = content["job_id"]

    for job in (job_id, "missing-job"):
        is_error, content = _structured(
            _run(_call("nl2sql_get_job", {"job_id": job}), token=_token(other.user_uuid))
        )
        assert is_error is True
        assert content["status"] == 404
        assert content["message"] == "指定されたジョブが見つかりません。"

    is_error, content = _structured(
        _run(_call("nl2sql_get_job", {"job_id": job_id}), token=_token(owner.user_uuid))
    )
    assert (is_error, content["job_id"]) == (False, job_id)

    # 利用者が system_admin なら、画面の「AI 活用」と同じく全員のジョブを見られる（#1889）。
    is_error, content = _structured(
        _run(
            _call("nl2sql_get_job", {"job_id": job_id}),
            token=_token(users.system_admin().user_uuid),
        )
    )
    assert (is_error, content["job_id"]) == (False, job_id)


def _owned_job(fake_service: _FakeNl2SqlService, owner: UserRecord, profile_id: str) -> str:
    """利用者のジョブを直接作る（MCP の作成の判定を通さない。権限が後で変わった状態を作る）。"""
    created = fake_service.start_job(
        JobCreateRequest(question="売上", profile_id=profile_id),
        actor_user_uuid=owner.user_uuid,
    )
    return created.job_id


def test_job_reads_and_cancel_are_authorized_against_the_current_scope(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    """nl2sql_get_job / nl2sql_cancel_job は、読む・止めるたびにその時点の範囲で判定する（#3318）。

    OWASP API1（物を扱うたびにその時点の権限で判定する）: 本人のジョブでも、トークンの claim
    （業務 Agent のデータの範囲）の外と、利用者が今は使えない業務プロファイルのジョブは返さない
    （確認の session と同じ 403）。他の利用者は存在を明かさない 404、system_admin は全員の
    ジョブを読めて止められる（#3244 の運用の操作）が、claim の外は同じく 403。
    """
    fake_service.finish_after_polls = None
    owner = users.create(
        "mcp.scope.owner", permissions={"menu.query"}, allowed_profile_ids={"sales", "hr"}
    )
    other = users.create(
        "mcp.scope.other", permissions={"menu.query"}, allowed_profile_ids={"sales", "hr"}
    )
    admin = users.system_admin()
    sales_job = _owned_job(fake_service, owner, "sales")
    # 作った後に利用者のロールから外れた業務プロファイルのジョブ。
    lost_job = _owned_job(fake_service, owner, "default")

    def outcome(tool: str, job_id: str, token: str) -> tuple[bool, Any]:
        is_error, content = _structured(_run(_call(tool, {"job_id": job_id}), token=token))
        if is_error:
            return True, (content["status"], content.get("error_code"))
        return False, content["job_id"]

    for tool in ("nl2sql_get_job", "nl2sql_cancel_job"):
        # 本人・claim の中（claim なしも同じ）。
        assert outcome(tool, sales_job, _token(owner.user_uuid, profile_ids=["sales"])) == (
            False,
            sales_job,
        )
        assert outcome(tool, sales_job, _token(owner.user_uuid)) == (False, sales_job)
        # 本人・claim の外: 業務 Agent の範囲を変えた後に、前の範囲で作ったジョブを読めない。
        assert outcome(tool, sales_job, _token(owner.user_uuid, profile_ids=["hr"])) == (
            True,
            (403, "PROFILE_SCOPE_FORBIDDEN"),
        )
        # 本人・利用者が今は使えない業務プロファイル。
        assert outcome(tool, lost_job, _token(owner.user_uuid))[1][0] == 403
        # 他の利用者: 存在も明かさない。
        for job_id in (sales_job, "missing-job"):
            assert outcome(tool, job_id, _token(other.user_uuid)) == (True, (404, "HTTP_404"))
        # system_admin: 全員のジョブ。claim の外は同じく 403。
        assert outcome(tool, sales_job, _token(admin.user_uuid)) == (False, sales_job)
        assert outcome(tool, sales_job, _token(admin.user_uuid, profile_ids=["hr"])) == (
            True,
            (403, "PROFILE_SCOPE_FORBIDDEN"),
        )
    # 取消を送ったのは判定を通った呼び出しだけ（本人 2 回・system_admin 1 回）。
    assert [(job_id, actor) for job_id, actor, _ in fake_service.cancels] == [
        (sales_job, owner.user_uuid),
        (sales_job, owner.user_uuid),
        (sales_job, admin.user_uuid),
    ]


def test_cancel_job_is_idempotent_and_records_the_entry(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    """nl2sql_cancel_job は冪等で、終わったジョブもエラーにせず result で返す（#3318）。"""
    fake_service.finish_after_polls = None
    user = users.create("mcp.cancel", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    token = _token(user.user_uuid)
    job_id = _owned_job(fake_service, user, "sales")
    cancel = _call("nl2sql_cancel_job", {"job_id": job_id})

    is_error, content = _structured(_run(cancel, token=token))
    assert is_error is False
    assert content == {
        "schema_version": mcp_tools.MCP_OUTPUT_SCHEMA_VERSION,
        "job_id": job_id,
        "status": "pending",
        "result": "cancelled",
        "message": "ジョブの取消を受け付けました。実行中の段階の区切りで止まります。",
    }
    # 入口は MCP と、サービストークンの Run の ID（署名した claim から）。
    assert fake_service.cancels[-1] == (
        job_id,
        user.user_uuid,
        {"via": "mcp", "agent_run_id": "run-1"},
    )
    # 2 回目（まだ止まっていない）は要求済み。
    _, content = _structured(_run(cancel, token=token))
    assert (content["status"], content["result"]) == ("pending", "already_cancelled")

    job = fake_service.jobs[job_id]
    job.status = JobStatus.ERROR
    job.error_code = "JOB_CANCELLED"
    is_error, content = _structured(_run(cancel, token=token))
    assert (is_error, content["status"], content["result"]) == (False, "error", "already_cancelled")

    done_job = _owned_job(fake_service, user, "sales")
    fake_service.jobs[done_job].status = JobStatus.DONE
    sent = len(fake_service.cancels)
    is_error, content = _structured(
        _run(_call("nl2sql_cancel_job", {"job_id": done_job}), token=token)
    )
    assert (is_error, content["status"], content["result"]) == (False, "done", "already_finished")
    assert "nl2sql_get_job" in content["message"]
    assert len(fake_service.cancels) == sent  # 終わったジョブには取消を送らない

    # API キー（外部の MCP クライアント）の入口はキーの ID。
    key_job = _owned_job(fake_service, user, "sales")
    _structured(_run(_call("nl2sql_cancel_job", {"job_id": key_job}), token=_api_key(users, user)))
    key_cancel = fake_service.cancels[-1]
    assert key_cancel[0] == key_job
    assert key_cancel[2]["via"] == "mcp"
    assert key_cancel[2]["api_key_id"]
    assert "agent_run_id" not in key_cancel[2]


def test_cancel_job_annotations(users: _Users, fake_service: _FakeNl2SqlService) -> None:
    user = users.create("mcp.cancel.list", permissions={"menu.query"})
    response = _run(_rpc("tools/list"), token=_token(user.user_uuid))
    assert response.status_code == 200
    tool = next(
        item for item in response.json()["result"]["tools"] if item["name"] == "nl2sql_cancel_job"
    )
    assert tool["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def test_requirement_session_is_continued_by_owner_and_closed_by_system_admin(
    users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    """SQL の生成前の確認の session を続けられるのは本人だけ（#3244）。

    他の利用者は存在を漏らさない 404、system_admin は見えるので 403。閉じる（取り消す）のは
    運用の操作なので system_admin もできる。
    """
    owner = users.create("mcp.req.owner", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    other = users.create("mcp.req.other", permissions={"menu.query"}, allowed_profile_ids={"sales"})
    _structured(
        _run(
            _call("nl2sql_query", {"question": "売上", "profile_id": "sales"}),
            token=_token(owner.user_uuid),
        )
    )
    runtime = ontology_router.ontology_runtime
    assert isinstance(runtime, _ReadyRequirementRuntime)
    (session_id,) = runtime.sessions
    cancel = _call("nl2sql_cancel_requirement", {"requirement_session_id": session_id})
    admin_token = _token(users.system_admin().user_uuid)

    for arguments in (
        {"question": "売上", "profile_id": "sales", "requirement_session_id": session_id},
        # profile_id を省くと確認の業務プロファイルを読む（同じ判定）。
        {"question": "売上", "requirement_session_id": session_id},
    ):
        continued = _call("nl2sql_query", arguments)
        is_error, content = _structured(_run(continued, token=_token(other.user_uuid)))
        assert (is_error, content["status"]) == (True, 404)
        is_error, content = _structured(_run(continued, token=admin_token))
        assert (is_error, content["status"]) == (True, 403)
        # 3 製品の MCP で同じ code（Agent の agent_chat の thread 指定と同じ）。
        assert content["error_code"] == OTHERS_CONTENT_FORBIDDEN_CODE
        assert content["message"] == OTHERS_CONTENT_FORBIDDEN_MESSAGE
    is_error, content = _structured(_run(cancel, token=_token(other.user_uuid)))
    assert (is_error, content["status"]) == (True, 404)
    is_error, content = _structured(_run(cancel, token=admin_token))
    assert (is_error, content["status"]) == (False, "closed")


def test_local_mode_uses_local_debug_user_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ローカル実行モードは token なしで全権限。実 service（deterministic）でジョブを実行する。"""

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(mcp_tools, "JOB_POLL_INTERVAL_SECONDS", 0.01)

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            imported = await client.post(
                "/api/nl2sql/sample-data/import",
                json={"step": "all", "confirmation": "ADMIN_EXECUTE"},
            )
            assert imported.status_code == 200, imported.text
            try:
                listed = await client.post("/api/mcp", json=_rpc("tools/list"))
                assert _tool_names(listed) == {
                    "nl2sql_list_profiles",
                    "nl2sql_recommend_profile",
                    "nl2sql_query",
                    "nl2sql_get_job",
                    "nl2sql_cancel_job",
                    "nl2sql_cancel_requirement",
                }
                response = await client.post(
                    "/api/mcp",
                    json=_call(
                        "nl2sql_query",
                        {
                            "question": "社員一覧を確認したい",
                            "profile_id": "default",
                            "row_limit": 2,
                            "wait_seconds": 10,
                        },
                    ),
                )
                is_error, content = _structured(response)
                assert is_error is False, content
                assert content["status"] == "done", content
                assert content["profile_id"] == "default"
                assert content["columns"]
                assert 0 < content["returned_count"] <= 2
                assert len(content["rows"]) == content["returned_count"]
            finally:
                deleted = await client.post(
                    "/api/nl2sql/sample-data/delete",
                    json={"confirmation": "ADMIN_EXECUTE"},
                )
                assert deleted.status_code == 200, deleted.text

    asyncio.run(exercise())


# ---------------------------------------------------------------- API キー（#1805）


def _api_key(users: _Users, user: UserRecord, products: list[str] | None = None) -> str:
    """利用者が自分で発行した API キー（外部の MCP クライアント向け）。"""
    from pr_system_settings.auth.api_keys import ApiKeyCreateRequest

    principal, _, _ = users.service.login(user.login_user_id, PASSWORD)
    request = ApiKeyCreateRequest.model_validate(
        {"name": "Claude Code", "products": products or ["nl2sql"]}
    )
    _, token = users.service.create_api_key(request, actor=principal)
    return token


def test_api_key_calls_mcp_as_the_user(users: _Users, fake_service: _FakeNl2SqlService) -> None:
    user = users.create("mcp.query", permissions={"menu.query"})
    token = _api_key(users, user)
    initialize = _rpc("initialize", {"protocolVersion": "2025-06-18"})
    assert _run(initialize, token=token).status_code == 200
    with_key = _tool_names(_run(_rpc("tools/list"), token=token))
    with_service_token = _tool_names(_run(_rpc("tools/list"), token=_token(user.user_uuid)))
    # ツールは利用者の権限で決まる（サービストークンで同じ利用者として呼んだときと同じ）。
    assert with_key == with_service_token
    assert "nl2sql_list_profiles" in with_key
    is_error, content = _structured(_run(_call("nl2sql_list_profiles", {}), token=token))
    assert is_error is False
    assert "profiles" in content


def test_api_key_rejections(users: _Users, fake_service: _FakeNl2SqlService) -> None:
    user = users.create("mcp.query", permissions={"menu.query"})
    token = _api_key(users, user)
    assert _run(_rpc("tools/list"), token=token[:-2] + "zz").status_code == 401
    rag_only = _api_key(users, user, ["rag"])
    denied = _run(_rpc("tools/list"), token=rag_only)
    assert (denied.status_code, denied.json()["error_code"]) == (
        403,
        "SECURITY_API_KEY_PRODUCT_FORBIDDEN",
    )

    async def outside_mcp() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                "/api/account/api-keys", headers={"Authorization": f"Bearer {token}"}
            )

    response = asyncio.run(outside_mcp())
    assert (response.status_code, response.json()["error_code"]) == (
        401,
        "SECURITY_API_KEY_NOT_ALLOWED",
    )
