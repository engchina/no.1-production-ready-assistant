"""RAG / NL2SQL の MCP の契約どおりの fake サーバー（#233）。

`pr_backend_core.mcp.McpServer` で契約（#230〜#233）のツールを持つサーバーを作り、
`httpx.MockTransport` で Agent の `httpx.Client` の送信先にする。サービストークンは
`verify_service_token` で検証し、呼び出しごとの claims（`sub` / `aud` / `run_id` /
`agent_id` / データの範囲の `profile_ids`。#1379）を記録する。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pr_backend_core.mcp import McpServer, McpTool, McpToolError
from pr_system_settings.auth.errors import SecurityApiError
from pr_system_settings.auth.service_token import verify_service_token
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pytest import MonkeyPatch

from app.features.agent.config import McpConnectionConfig, runtime_config_store
from app.settings import get_settings

SERVICE_TOKEN_SECRET = "agent-mcp-test-secret-0123456789abcdef"  # nosec B105 - テスト用
RAG_MCP_URL = "http://rag.test/api/mcp"
NL2SQL_MCP_URL = "http://nl2sql.test/api/mcp"
# monkeypatch で httpx.Client を差し替えても、fake の中では本物の client を使う。
_REAL_HTTPX_CLIENT = httpx.Client


class _ContractInput(BaseModel):
    # 契約にない項目を Agent が送ったら検出できるよう、fake は未知の項目を拒否する。
    model_config = ConfigDict(extra="forbid")


class RagSearchIn(_ContractInput):
    query: str
    search_answer_profile_id: str | None = None
    knowledge_base_ids: list[str] | None = None
    # 既定は契約の既定（#3240）。null は省略と同じ。
    top_k: int | None = Field(default=50, ge=1, le=100)
    filters: dict[str, str] | None = None
    # 旧版も検索するかは専用の真偽値（#1392。filters の値は文字列だけ）。
    include_superseded: bool = False
    evidence_limit: int = Field(default=12, ge=1, le=50)
    conditions: dict[str, str] | None = None
    # ルールの回答前の確認を出さない（#2117）。
    skip_clarification: bool = False
    # 利用者の元の質問（#2261）。
    original_question: str | None = None


class RagRetrieveEvidenceIn(RagSearchIn):
    # 根拠の収集は検索する件数（top_k の既定 50。#3512）と同じ件数を既定で返す（#1365）。
    evidence_limit: int = Field(default=50, ge=1, le=50)
    # 検索文を作る主体と、呼び出し元の検索文・追跡 ID（#2651）。
    planning: Literal["rag", "caller"] = "rag"
    queries: list[str] = Field(default_factory=list, max_length=5)
    request_id: str | None = Field(default=None, max_length=128)


class RagLookupGuidesIn(_ContractInput):
    query: str
    search_answer_profile_id: str
    conditions: dict[str, str] | None = None
    limit: int = Field(default=3, ge=1, le=10)


class RagReadSourceIn(_ContractInput):
    document_id: str
    # chunk_id か要素の定位子（locator。#1330）のどちらかで読む。
    chunk_id: str | None = None
    locator: str | None = Field(default=None, min_length=1, max_length=1024)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8000, ge=1, le=20000)
    # 図の根拠の元の画像（#1282）と、画面で開く短命の URL（#1311）。
    include_image: bool = False
    include_image_url: bool = False
    # 根拠の原文のプレビュー（#2389。画面の操作だけ）。
    include_preview: bool = False


class RagOutlineIn(_ContractInput):
    document_id: str = Field(min_length=1, max_length=128)


class RagReadDocumentIn(_ContractInput):
    # 文書を順に読む（#1332）。読み始めの位置はどれか 1 つ。
    document_id: str = Field(min_length=1, max_length=128)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    locator: str | None = Field(default=None, min_length=1, max_length=1024)
    page: int | None = Field(default=None, ge=1)
    # 章節（rag_outline の section_id）の始まりから読む（#1438）。
    section_id: str | None = Field(default=None, min_length=1, max_length=64)
    max_chars: int = Field(default=8000, ge=1, le=20000)


class RagReadSectionIn(_ContractInput):
    # 章節の本文を章節の終わりまで読む（#1438）。section_id・chunk_id・cursor のどれか 1 つ。
    document_id: str = Field(min_length=1, max_length=128)
    section_id: str | None = Field(default=None, min_length=1, max_length=64)
    chunk_id: str | None = Field(default=None, min_length=1, max_length=512)
    up: int = Field(default=0, ge=0, le=6)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    max_chars: int = Field(default=8000, ge=1, le=20000)


class RagEvidenceRefIn(_ContractInput):
    document_id: str = Field(min_length=1, max_length=128)
    chunk_id: str = Field(min_length=1, max_length=512)


class RagReadableDocumentsIn(_ContractInput):
    # 保存した根拠の文書を、見る人が今見られるかを確かめる内部のツール（#3252）。
    document_ids: list[str] = Field(min_length=1, max_length=200)


class RagValidateRequestIn(_ContractInput):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=2000)
    status: Literal["addressed", "partial", "missing", "unknown"]


class RagValidateGuideIn(_ContractInput):
    search_answer_profile_id: str = Field(min_length=1, max_length=128)
    guide_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    conditions: dict[str, str] | None = None


class RagValidatePremiseIn(_ContractInput):
    # 利用者が回答前の確認で答えた前提（#1898）。
    question: str = Field(default="", max_length=1000)
    answer: str = Field(min_length=1, max_length=2000)


class RagValidateDataResultIn(_ContractInput):
    # 業務データの照会の結果（#1950）。
    source: str = Field(default="", max_length=200)
    question: str = Field(default="", max_length=1000)
    sql: str = Field(default="", max_length=4000)
    result: str = Field(min_length=1, max_length=4000)


class RagValidateAnswerIn(_ContractInput):
    query: str = Field(min_length=1, max_length=8000)
    answer: str = Field(min_length=1, max_length=20000)
    evidence: list[RagEvidenceRefIn] = Field(min_length=1, max_length=30)
    # 決定的な検査の任意の入力（#1276）。
    requests: list[RagValidateRequestIn] | None = Field(default=None, max_length=30)
    gaps: list[str] | None = Field(default=None, max_length=30)
    guide: RagValidateGuideIn | None = None
    # 監査の判定を再利用する rag_search の回答の記録（#1527）。
    answer_trace_ids: list[str] = Field(default_factory=list, max_length=10)
    premises: list[RagValidatePremiseIn] = Field(default_factory=list, max_length=10)
    data_results: list[RagValidateDataResultIn] = Field(default_factory=list, max_length=10)


class RagListSearchAnswerProfilesIn(_ContractInput):
    query: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class RagRecommendSearchAnswerProfileIn(_ContractInput):
    # 質問から検索・回答プロファイルを推薦する（#1850。nl2sql_recommend_profile と同じ形）。
    question: str = Field(min_length=1, max_length=4000)
    profile_ids: list[str] | None = Field(default=None, min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=8)


class Nl2SqlRequirementAnswerIn(_ContractInput):
    # SQL の生成前の確認の項目への答え（#1441）。
    item_id: str = Field(min_length=1, max_length=128)
    selected_candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    free_text: str = Field(default="", max_length=2000)


class Nl2SqlQueryIn(_ContractInput):
    question: str = Field(min_length=1, max_length=4000)
    # 省略・使えない ID なら呼び先が選ぶ（"default" には切り替えない。#1379・#2355）。
    profile_id: str | None = Field(default=None, max_length=128)
    row_limit: int = Field(default=100, ge=1, le=1000)
    wait_seconds: int = Field(default=40, ge=0, le=45)
    # SQL の生成前の確認の続き（#1441）。
    requirement_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    answer: Nl2SqlRequirementAnswerIn | None = None
    # 利用者が確認せずに SQL を生成するよう選んだ（#1839）。answer と一緒には指定しない。
    skip_clarification: bool = False

    @model_validator(mode="after")
    def _skip_without_answer(self) -> Nl2SqlQueryIn:
        if self.skip_clarification and self.answer is not None:
            raise ValueError("skip_clarification と answer は一緒に指定できません。")
        return self


class Nl2SqlCancelRequirementIn(_ContractInput):
    requirement_session_id: str = Field(min_length=1, max_length=128)


class Nl2SqlCancelJobIn(_ContractInput):
    job_id: str = Field(min_length=1, max_length=128)


class Nl2SqlGetJobIn(_ContractInput):
    job_id: str
    wait_seconds: int = Field(default=0, ge=0, le=45)


class Nl2SqlListProfilesIn(_ContractInput):
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


class Nl2SqlRecommendProfileIn(_ContractInput):
    question: str = Field(min_length=1, max_length=4000)
    profile_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)


# NL2SQL の MCP の出力の形の版（nl2sql-tools.json の schema_version。#3240）。
NL2SQL_OUTPUT_SCHEMA_VERSION = 2


def nl2sql_requirement(status: str = "applied", **extra: Any) -> dict[str, Any]:
    """契約の `requirement`（SQL の生成前の確認の状態。#1441）。"""
    return {
        "session_id": "qs-1",
        "status": status,
        "question": None,
        "remaining_count": 0,
        "answered_count": 0,
        "select_from_clear": True,
        "minimum_missing": [],
        "summary": "- 使う表（FROM）: SALES",
        "clarified_question": "部門別の売上（SALES.AMOUNT の合計）",
        "sql_skeleton": "SELECT SALES.DEPARTMENT, SUM(SALES.AMOUNT) FROM SALES",
        "message": "",
        **extra,
    }


def nl2sql_needs_clarification(
    *,
    session_id: str = "qs-1",
    item_id: str = "item-period",
    question: str = "SALES のどの日付の列で期間を絞りますか？",
    candidates: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """`nl2sql_query` が SQL を作らずに確認の項目を 1 つ返した結果（#1441）。"""
    return {
        "schema_version": NL2SQL_OUTPUT_SCHEMA_VERSION,
        "job_id": None,
        "status": "needs_clarification",
        "profile_id": "profile-sales",
        "generated_sql": None,
        "executable_sql": None,
        "explanation": None,
        "is_safe": None,
        "safety_issues": [],
        "columns": [],
        "rows": [],
        "returned_count": 0,
        "total": None,
        "has_more": False,
        "truncated": False,
        "history_id": None,
        "error_code": None,
        "error_message": None,
        "requirement": nl2sql_requirement(
            "needs_answer",
            session_id=session_id,
            remaining_count=1,
            select_from_clear=True,
            question={
                "item_id": item_id,
                "section": "period",
                "label": "期間の日付の列",
                "question": question,
                "reason": "「今月」の基準の列が決まっていません。",
                "answer_kind": "single_select",
                "candidates": candidates
                if candidates is not None
                else [
                    {
                        "id": "c-order",
                        "label": "SALES.ORDER_DATE（受注日）",
                        "description": "",
                        "refs": "SALES.ORDER_DATE（受注日）",
                    },
                    {
                        "id": "c-ship",
                        "label": "SALES.SHIP_DATE（出荷日）",
                        "description": "",
                        "refs": "SALES.SHIP_DATE（出荷日）",
                    },
                ],
                "value": "",
                "refs": "",
            },
        ),
    }


def _job(status: str = "done") -> dict[str, Any]:
    return {
        "schema_version": NL2SQL_OUTPUT_SCHEMA_VERSION,
        "job_id": "job-1",
        "status": status,
        "profile_id": "profile-sales",
        "generated_sql": "select department, sum(amount) amount from sales group by department",
        "executable_sql": "select department, sum(amount) amount from sales group by department",
        "explanation": "部門別の売上を集計しました。",
        "is_safe": True,
        "safety_issues": [],
        "columns": ["DEPARTMENT", "AMOUNT"],
        "rows": [{"DEPARTMENT": "営業", "AMOUNT": 1200}],
        "returned_count": 1,
        "total": 1,
        "has_more": False,
        "truncated": False,
        "history_id": "history-1",
        "error_code": None,
        "error_message": None,
        "requirement": nl2sql_requirement(),
    }


# RAG の MCP のツールの既定の出力（#3240）。契約（platform/contracts/mcp/rag-tools.json）の
# outputSchema だけで作った共有の合成の例（platform/contracts/mcp/fixtures/rag-tool-outputs.json）を
# 使い、Agent のテストを RAG の内部の形ではなく公開の契約で動かす（契約との一致は
# test_rag_contract_usage.py、RAG の出力の型で読めることは rag の
# test_mcp_contract.py が確かめる）。
RAG_OUTPUTS_FIXTURE = (
    Path(__file__).resolve().parents[3] / "platform/contracts/mcp/fixtures/rag-tool-outputs.json"
)
_RAG_FIXTURE: dict[str, Any] = json.loads(RAG_OUTPUTS_FIXTURE.read_text(encoding="utf-8"))
# RAG の MCP の出力の形の版（契約の schema_version。版の経緯は rag の app/mcp/tools.py）。
RAG_OUTPUT_SCHEMA_VERSION: int = _RAG_FIXTURE["schema_version"]
RAG_OUTPUTS: dict[str, Any] = _RAG_FIXTURE["outputs"]
# RAG の `rag_search` の根拠（契約の evidence の 1 件）と場所。
_EVIDENCE: dict[str, Any] = RAG_OUTPUTS["rag_search"]["evidence"][0]
_LOCATOR: dict[str, Any] = _EVIDENCE["locator"]

DEFAULT_OUTPUTS: dict[str, Any] = {
    # rag_readable_documents は fake が引数と `unreadable_documents` から作る。
    **{name: output for name, output in RAG_OUTPUTS.items() if name != "rag_readable_documents"},
    "nl2sql_query": _job(),
    "nl2sql_get_job": _job(),
    "nl2sql_list_profiles": {
        "schema_version": NL2SQL_OUTPUT_SCHEMA_VERSION,
        "profiles": [
            {
                "id": "profile-sales",
                "name": "売上",
                "category": "営業",
                "description": "売上の集計",
            }
        ],
    },
    "nl2sql_recommend_profile": {
        "schema_version": NL2SQL_OUTPUT_SCHEMA_VERSION,
        "recommended_profile_id": "profile-sales",
        "rewritten_question": None,
        "candidates": [{"id": "profile-sales", "name": "売上", "reason": None, "score": 0.9}],
    },
    # ジョブの取消（#3318。Run の取消で Agent の Control Plane が呼ぶ）。
    "nl2sql_cancel_job": lambda argument: {
        "schema_version": NL2SQL_OUTPUT_SCHEMA_VERSION,
        "job_id": argument.job_id,
        "status": "running",
        "result": "cancelled",
        "message": "ジョブの取消を受け付けました。実行中の段階の区切りで止まります。",
    },
    # SQL の生成前の確認の取り消し（#1441。Agent の Control Plane が呼ぶ）。
    "nl2sql_cancel_requirement": lambda argument: {
        "schema_version": NL2SQL_OUTPUT_SCHEMA_VERSION,
        "requirement_session_id": argument.requirement_session_id,
        "status": "cancelled",
    },
}

Product = Literal["rag", "nl2sql"]
ToolOutput = dict[str, Any] | Exception | Callable[[BaseModel], dict[str, Any]]


# 失敗の指定（HTTP status・読み取りの timeout・接続できない・status と header）。
# "invalid_json" は 200 で JSON でない本文（保守画面の HTML など。障害注入の #64）。
Failure = (
    int
    | Literal["timeout", "connect", "connect_timeout", "invalid_json"]
    | tuple[int, dict[str, str]]
)


@dataclass
class FakeProductMcp:
    """契約どおりの RAG / NL2SQL の MCP サーバー（HTTP の記録つき）。"""

    outputs: dict[str, ToolOutput] = field(default_factory=lambda: deepcopy(DEFAULT_OUTPUTS))
    # 全 HTTP request（product / method / headers / body / claims）。
    requests: list[dict[str, Any]] = field(default_factory=list)
    # tools/call だけ（product / name / arguments / claims / timeout）。
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # 次の tools/call に返す HTTP status（再試行の確認用。先頭から使う。呼び先に届いた後の失敗）。
    # "timeout" は読み取りの timeout、(status, headers) は応答の header 付き（Retry-After など）。
    tool_call_statuses: list[Failure] = field(default_factory=list)
    # メソッドごとの失敗（先頭から使う。#854）。呼び先に届く前に判定し、request を記録しない。
    # "connect" / "connect_timeout" は接続できない（送信前の失敗）。
    method_failures: dict[str, list[Failure]] = field(default_factory=dict)
    # MCP 以外の URL（外部 MCP など）への応答と、その request（url / json / timeout）。
    other_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    other_calls: list[dict[str, Any]] = field(default_factory=list)
    # `rag_readable_documents` が見られないと答える文書（保存した根拠の確かめ直し。#3252）。
    unreadable_documents: set[str] = field(default_factory=set)
    _session_counter: int = 0

    def __post_init__(self) -> None:
        self.servers: dict[str, McpServer] = {
            "rag": McpServer(
                name="production-ready-rag",
                version="test",
                tools=[
                    self._tool("rag_search", RagSearchIn),
                    self._tool("rag_list_search_answer_profiles", RagListSearchAnswerProfilesIn),
                    self._tool(
                        "rag_recommend_search_answer_profile", RagRecommendSearchAnswerProfileIn
                    ),
                    self._tool("rag_read_source", RagReadSourceIn),
                    self._tool("rag_lookup_guides", RagLookupGuidesIn),
                    self._tool("rag_retrieve_evidence", RagRetrieveEvidenceIn),
                    self._tool("rag_outline", RagOutlineIn),
                    self._tool("rag_read_document", RagReadDocumentIn),
                    self._tool("rag_read_section", RagReadSectionIn),
                    self._tool("rag_validate_answer", RagValidateAnswerIn),
                    McpTool(
                        name="rag_readable_documents",
                        description="rag_readable_documents",
                        input_model=RagReadableDocumentsIn,
                        handler=self._readable_documents,
                        listed=False,
                    ),
                ],
            ),
            "nl2sql": McpServer(
                name="production-ready-nl2sql",
                version="test",
                tools=[
                    self._tool("nl2sql_query", Nl2SqlQueryIn, read_only=False),
                    self._tool("nl2sql_get_job", Nl2SqlGetJobIn),
                    self._tool("nl2sql_list_profiles", Nl2SqlListProfilesIn),
                    self._tool("nl2sql_recommend_profile", Nl2SqlRecommendProfileIn),
                    self._tool(
                        "nl2sql_cancel_requirement", Nl2SqlCancelRequirementIn, read_only=False
                    ),
                    self._tool("nl2sql_cancel_job", Nl2SqlCancelJobIn, read_only=False),
                ],
            ),
        }

    def _tool(self, name: str, model: type[BaseModel], *, read_only: bool = True) -> McpTool:
        async def handler(argument: BaseModel) -> dict[str, Any]:
            output = self.outputs[name]
            if isinstance(output, Exception):
                raise output
            if callable(output):
                return output(argument)
            return deepcopy(output)

        return McpTool(
            name=name, description=name, input_model=model, handler=handler, read_only=read_only
        )

    async def _readable_documents(self, argument: RagReadableDocumentsIn) -> dict[str, Any]:
        return {
            "schema_version": RAG_OUTPUT_SCHEMA_VERSION,
            "readable_document_ids": [
                item for item in argument.document_ids if item not in self.unreadable_documents
            ],
        }

    def calls_of(self, name: str) -> list[dict[str, Any]]:
        return [call for call in self.tool_calls if call["name"] == name]

    def client_factory(self, timeout: float) -> httpx.Client:
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(self._handle), timeout=timeout)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content or b"null")
        product: Product | None = {"rag.test": "rag", "nl2sql.test": "nl2sql"}.get(request.url.host)  # type: ignore[assignment]
        if product is None:
            self.other_calls.append(
                {
                    "url": url,
                    "json": body,
                    "headers": dict(request.headers),
                    "timeout": request.extensions.get("timeout", {}).get("read"),
                }
            )
            return httpx.Response(200, json=self.other_responses[url])
        claims: dict[str, Any] | None = None
        authorization = request.headers.get("authorization", "")
        try:
            claims = verify_service_token(
                SERVICE_TOKEN_SECRET, authorization.removeprefix("Bearer "), audience=product
            )
        except SecurityApiError:
            claims = None
        method = body.get("method") if isinstance(body, dict) else None
        self.requests.append(
            {
                "product": product,
                "method": method,
                "headers": dict(request.headers),
                "body": body,
                "claims": claims,
            }
        )
        if claims is None:
            return httpx.Response(401, json={"detail": "invalid token"})
        failures = self.method_failures.get(str(method), [])
        if failures:
            return _failure_response(failures.pop(0), request)
        if method == "tools/call":
            params = body.get("params") or {}
            self.tool_calls.append(
                {
                    "product": product,
                    "name": params.get("name"),
                    "arguments": params.get("arguments"),
                    "claims": claims,
                    "timeout": request.extensions.get("timeout", {}).get("read"),
                }
            )
            if self.tool_call_statuses:
                return _failure_response(self.tool_call_statuses.pop(0), request)
        response = _run_async(
            self.servers[product].handle(body, has_any_permission=lambda _codes: True)
        )
        if response is None:
            return httpx.Response(202)
        headers: dict[str, str] = {}
        if method == "initialize":
            self._session_counter += 1
            headers["Mcp-Session-Id"] = f"session-{product}-{self._session_counter}"
        return httpx.Response(200, json=response, headers=headers)


def _failure_response(failure: Failure, request: httpx.Request) -> httpx.Response:
    if failure == "timeout":
        raise httpx.ReadTimeout("read timeout", request=request)
    if failure == "connect":
        raise httpx.ConnectError("connection refused", request=request)
    if failure == "connect_timeout":
        raise httpx.ConnectTimeout("connect timeout", request=request)
    if failure == "invalid_json":
        return httpx.Response(
            200, text="<html>maintenance</html>", headers={"content-type": "text/html"}
        )
    if isinstance(failure, tuple):
        status, headers = failure
        return httpx.Response(status, text="upstream error", headers=headers)
    return httpx.Response(failure, text="upstream error")


def _run_async(coroutine: Any) -> Any:
    # tool の呼び出しは event loop の中（async の route）からも来るため、別 thread で動かす。
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def fake_product_mcp(
    monkeypatch: MonkeyPatch,
    *,
    outputs: dict[str, ToolOutput] | None = None,
    rag_timeout_seconds: float = 60.0,
    nl2sql_timeout_seconds: float = 60.0,
    secret: str = SERVICE_TOKEN_SECRET,
) -> FakeProductMcp:
    """RAG / NL2SQL の MCP を fake にし、MCP 接続（`rag` / `nl2sql`）の URL と署名鍵を合わせる。"""
    fake = FakeProductMcp()
    if outputs:
        fake.outputs.update(outputs)
    monkeypatch.setattr(get_settings(), "app_service_token_secret", secret)
    connections: dict[str, McpConnectionConfig] = {
        config.server_id: config for config in runtime_config_store.list_mcp_servers()
    }
    connections["rag"] = connections["rag"].model_copy(
        update={"base_url": RAG_MCP_URL, "timeout_seconds": rag_timeout_seconds}
    )
    connections["nl2sql"] = connections["nl2sql"].model_copy(
        update={"base_url": NL2SQL_MCP_URL, "timeout_seconds": nl2sql_timeout_seconds}
    )
    monkeypatch.setattr(runtime_config_store, "_mcp_servers", connections)
    monkeypatch.setattr("app.features.agent.tools.httpx.Client", fake.client_factory)
    return fake


__all__ = [
    "DEFAULT_OUTPUTS",
    "NL2SQL_MCP_URL",
    "NL2SQL_OUTPUT_SCHEMA_VERSION",
    "RAG_MCP_URL",
    "SERVICE_TOKEN_SECRET",
    "FakeProductMcp",
    "McpToolError",
    "fake_product_mcp",
    "nl2sql_needs_clarification",
    "nl2sql_requirement",
]
