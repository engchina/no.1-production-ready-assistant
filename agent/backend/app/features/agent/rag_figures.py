"""Run の RAG の根拠を画面で開く、短命の URL（図 #1311・原文のプレビュー #2389）。

Agent の画面（ブラウザ）は RAG の Cookie を持たないため、図の根拠・根拠の原文を開くときは Agent の
backend が RAG の MCP の ``rag_read_source``（``include_image_url`` / ``include_preview``）を呼び、
RAG が作る短命・対象（利用者・chunk・版）に縛った署名つきの URL を受け取る。画面はその URL で図・
原文のページを開く（RAG が読むたびに今の権限・版を確かめ直す）。

- 呼ぶのは組み込みの接続 ``rag``（サービストークン）だけ。``sub`` は **画面を見ている利用者**
  （Run の利用者ではない。Run を見られる管理者が、Run の利用者の権限を借りて図を開けないように
  する）。サービストークンに ``purpose=figure_url`` を入れ、RAG は Run の中でモデルが呼んだときには
  URL を作らない（URL を Run の記録・モデルの文脈に残さない）。
- 画面の操作で、Run のツール呼び出しではないので ``tool_registry.invoke`` を通さない（承認・
  ガードレール・成果物の対象ではない。URL は保存しない）。
- 開けるのは、画面の根拠の一覧と同じ集合（``run_evidence_items``）にある根拠だけ（図は ``image_ref``
  がある根拠だけ。#2427）。
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, Field

from app.features.agent.answer_evidence import ANSWER_EVIDENCE_KIND, passed_evidence
from app.features.agent.config import runtime_config_store
from app.features.agent.runtime import Artifact, RunState
from app.features.agent.tools import (
    MCP_TOOL_SEPARATOR,
    ExternalToolError,
    McpConnectionClient,
    ToolInvocationContext,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)

RAG_CONNECTION_ID = "rag"
RAG_EVIDENCE_KIND = "rag_evidence"
READ_SOURCE_TOOL = "rag_read_source"
# RAG が URL を作ってよい呼び出し（画面の操作）の印（サービストークンの claim `purpose`。値は
# platform/docs/backend-standard.md の共通の規則）。
FIGURE_URL_PURPOSE = "figure_url"
SOURCE_PREVIEW_PURPOSE = "source_preview"
# RAG の図・原文のプレビューの読み取りの path の始まり（契約 rag-tools.json の `rag_read_source` の
# `image_url.path`・`preview.content_path` の pattern。#3240）。公開の起点に付け替えるときに
# 確かめる。
FIGURE_PATH_PREFIX = "/api/figures/"
SOURCE_PREVIEW_PATH_PREFIX = "/api/source-previews/"

NOT_IN_RUN_MESSAGE = "この実行の根拠に、その図はありません。"
NOT_SUPPORTED_MESSAGE = "この接続の根拠の図は開けません。"
STALE_MESSAGE = "資料が更新されたため、この図は開けません。もう一度質問してください。"
NOT_FOUND_MESSAGE = "図が見つからないか、この図を見る権限がありません。"
NOT_AVAILABLE_MESSAGE = "この根拠には開ける図がありません。"
UNAVAILABLE_MESSAGE = "RAG の設定が足りないため、図を開けません。管理者に連絡してください。"
FAILED_MESSAGE = "RAG から図を開く URL を取得できませんでした。時間をおいて再度お試しください。"
PREVIEW_NOT_IN_RUN_MESSAGE = "この実行の根拠に、その資料はありません。"
PREVIEW_NOT_SUPPORTED_MESSAGE = "この接続の根拠の原文は表示できません。"
PREVIEW_NOT_FOUND_MESSAGE = "資料が見つからないか、この資料を見る権限がありません。"
PREVIEW_STALE_MESSAGE = (
    "資料が更新されたため、この根拠の原文は表示できません。もう一度質問してください。"
)
PREVIEW_FILE_MISSING_MESSAGE = (
    "原文のファイルが保存先にありません。RAG の管理者に連絡してください。"
)
PREVIEW_UNAVAILABLE_MESSAGE = (
    "RAG の設定が足りないため、原文を表示できません。管理者に連絡してください。"
)
PREVIEW_FAILED_MESSAGE = (
    "RAG から原文のプレビューを取得できませんでした。時間をおいて再度お試しください。"
)

# RAG の MCP のエラー（`rag_read_source`。code は契約 rag-tools.json の `errors`）→ 画面へ返す
# status と文言。
_TOOL_ERRORS: dict[str, tuple[int, str]] = {
    "source_stale": (409, STALE_MESSAGE),
    "source_not_found": (404, NOT_FOUND_MESSAGE),
    "image_not_available": (404, NOT_AVAILABLE_MESSAGE),
    "image_url_unavailable": (503, UNAVAILABLE_MESSAGE),
    "MCP_TOOL_FORBIDDEN": (403, NOT_FOUND_MESSAGE),
}
_PREVIEW_TOOL_ERRORS: dict[str, tuple[int, str]] = {
    "source_stale": (409, PREVIEW_STALE_MESSAGE),
    "source_not_found": (404, PREVIEW_NOT_FOUND_MESSAGE),
    "preview_file_missing": (404, PREVIEW_FILE_MISSING_MESSAGE),
    "preview_unavailable": (503, PREVIEW_UNAVAILABLE_MESSAGE),
    "MCP_TOOL_FORBIDDEN": (403, PREVIEW_NOT_FOUND_MESSAGE),
}


class RunFigureUrlData(BaseModel):
    """図を開く短命の URL（画面は保存しない。期限が切れたら取り直す）。"""

    url: str = Field(description="図の画像の URL（RAG。期限つき）。")
    expires_at: str = Field(description="URL の期限（ISO 8601、UTC）。")


class RunSourcePreviewPage(BaseModel):
    """プレビューの 1 ページの表示寸法（pt）。"""

    page_number: int
    width: float
    height: float


class RunSourcePreviewData(BaseModel):
    """根拠の原文のプレビュー（画面は保存しない。期限が切れる前に取り直す）。"""

    kind: Literal["pdf", "image", "text", "unsupported"] = Field(
        description="pdf=ページの画像 / image=画像 / text=本文 / unsupported=表示できない形式。"
    )
    file_name: str
    pages: list[RunSourcePreviewPage] = Field(default_factory=list)
    page_url_template: str | None = Field(
        default=None,
        description=(
            "ページの画像の URL（RAG。{page} をページ番号に置き換え、query の dpi で解像度）。"
        ),
    )
    content_url: str = Field(description="原文のファイルの URL（RAG。期限つき）。")
    highlight_metadata: dict[str, Any] = Field(
        default_factory=dict, description="強調する位置を決める根拠の metadata（頁・bbox など）。"
    )
    expires_at: str = Field(description="URL の期限（ISO 8601、UTC）。")
    expires_in_seconds: int = Field(description="発行からの有効な秒数。")


class RunFigureUrlError(Exception):
    """画面へ返す失敗（status と日本語の文言）。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# 画面から根拠を開くときに読む成果物の種類（router が中身を読み込んでから渡す）。
EVIDENCE_ARTIFACT_KINDS = frozenset({RAG_EVIDENCE_KIND, ANSWER_EVIDENCE_KIND})


def _is_rag_evidence(artifact: Artifact) -> bool:
    # 成果物の名前は `<接続>__rag_search:<step>`。組み込みの接続 `rag` の根拠だけを対象にする。
    return artifact.kind == RAG_EVIDENCE_KIND and artifact.name.startswith(
        f"{RAG_CONNECTION_ID}{MCP_TOOL_SEPARATOR}"
    )


def _artifact_evidence(artifact: Artifact) -> list[Any]:
    if artifact.kind == ANSWER_EVIDENCE_KIND:
        items = artifact.content.get("items")
    elif _is_rag_evidence(artifact):
        items = artifact.content.get("evidence")
    else:
        return []
    return items if isinstance(items, list) else []


def run_evidence_items(run: RunState, artifacts: list[Artifact]) -> list[dict[str, Any]]:
    """画面から開いてよい、その Run の RAG の根拠（画面の根拠の一覧と同じ集合。#2427）。

    - 画面の根拠の一覧と同じ関数（``answer_evidence.passed_evidence``）で、Run の根拠のツール
      （``rag_search`` / ``rag_retrieve_evidence``）の step の出力から作る根拠（回答の確定の前も）。
    - 成果物「回答の根拠」（``answer_evidence``）の ``items``（画面の一覧の正本）。
    - 成果物「検索の根拠」（``rag_evidence``。組み込みの接続 ``rag`` の ``rag_search``）の
      ``evidence``。

    ``artifacts`` は中身を読み込んだ成果物（種類は ``EVIDENCE_ARTIFACT_KINDS``）。
    """
    items: list[Any] = list(passed_evidence(run.steps))
    for artifact in artifacts:
        items.extend(_artifact_evidence(artifact))
    return [item for item in items if isinstance(item, dict)]


def run_has_figure_evidence(
    run: RunState, artifacts: list[Artifact], *, document_id: str, chunk_id: str
) -> bool:
    """Run の RAG の根拠に、その図（``image_ref`` がある根拠）があるか。"""
    return any(
        item.get("document_id") == document_id
        and item.get("chunk_id") == chunk_id
        and isinstance(item.get("image_ref"), dict)
        for item in run_evidence_items(run, artifacts)
    )


def run_has_rag_evidence(
    run: RunState, artifacts: list[Artifact], *, document_id: str, chunk_id: str
) -> bool:
    """Run の RAG の根拠に、その根拠があるか。"""
    return any(
        item.get("document_id") == document_id and item.get("chunk_id") == chunk_id
        for item in run_evidence_items(run, artifacts)
    )


def _browser_url(
    url_info: dict[str, Any],
    *,
    prefix: str = FIGURE_PATH_PREFIX,
    url_key: str = "url",
    path_key: str = "path",
    failed_message: str = FAILED_MESSAGE,
) -> str:
    """画面が開く URL。公開の起点（AGENT_EXTERNAL_RAG_PUBLIC_URL）があれば path をそこに付ける。

    RAG の図・原文のプレビューの path（``prefix``）だけを許し、ほかの URL は返さない（502）。
    """
    path = url_info.get(path_key)
    url = url_info.get(url_key)
    public_base = (get_settings().agent_external_rag_public_url or "").strip()
    if public_base and isinstance(path, str) and path.startswith(prefix):
        candidate = f"{public_base.rstrip('/')}{path}"
    elif isinstance(url, str):
        candidate = url
    else:
        raise RunFigureUrlError(502, failed_message)
    parts = urlsplit(candidate)
    if _is_same_origin_prefix(public_base) and candidate.startswith(public_base.rstrip("/") + "/"):
        # 1 台の Compute（#1316）: 起点は同じ origin の path（例 `/rag`）。ブラウザが Agent の
        # 画面と同じ origin で解決するので host を持たない。起点の後ろは RAG の path だけを許す。
        rag_path = candidate[len(public_base.rstrip("/")) :]
        if (
            not rag_path.startswith(prefix)
            or parts.query
            or parts.fragment
            or parts.scheme
            or parts.netloc
            or "/../" in rag_path
        ):
            raise RunFigureUrlError(502, failed_message)
        return candidate
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise RunFigureUrlError(502, failed_message)
    if not parts.path.startswith(prefix) or parts.query or parts.fragment or "/../" in parts.path:
        raise RunFigureUrlError(502, failed_message)
    return candidate


def _is_same_origin_prefix(public_base: str) -> bool:
    """公開の起点が同じ origin の path（`/rag` など。`//host` ではない）か。"""
    return public_base.startswith("/") and not public_base.startswith("//")


def _rag_service_client(
    run: RunState, viewer_user_uuid: str, purpose: str
) -> tuple[McpConnectionClient, str]:
    """RAG の接続（画面を見ている利用者を ``sub``、画面の操作の印 ``purpose`` を入れる）。"""
    try:
        config = runtime_config_store.get_mcp(RAG_CONNECTION_ID)
    except KeyError:
        raise RunFigureUrlError(409, NOT_SUPPORTED_MESSAGE) from None
    if config.effective_auth_mode() != "service_token":
        raise RunFigureUrlError(409, NOT_SUPPORTED_MESSAGE)
    trace_id = f"{purpose}_{uuid4().hex}"
    client = McpConnectionClient(
        config,
        context=ToolInvocationContext(
            trace_id=trace_id,
            agent_id=run.agent_id,
            run_id=run.id,
            user_uuid=viewer_user_uuid,
        ),
        extra_claims={"purpose": purpose},
    )
    return client, trace_id


def issue_run_source_preview(
    run: RunState,
    artifacts: list[Artifact],
    *,
    document_id: str,
    chunk_id: str,
    viewer_user_uuid: str,
) -> RunSourcePreviewData:
    """画面を見ている利用者として RAG に根拠の原文のプレビューを作らせる（同期。thread で呼ぶ）。"""
    if not run_has_rag_evidence(run, artifacts, document_id=document_id, chunk_id=chunk_id):
        raise RunFigureUrlError(404, PREVIEW_NOT_IN_RUN_MESSAGE)
    try:
        client, trace_id = _rag_service_client(run, viewer_user_uuid, SOURCE_PREVIEW_PURPOSE)
    except RunFigureUrlError as exc:
        raise RunFigureUrlError(exc.status_code, PREVIEW_NOT_SUPPORTED_MESSAGE) from None
    try:
        output = client.call_tool(
            READ_SOURCE_TOOL,
            {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "include_preview": True,
                "max_chars": 1,
            },
            idempotent=True,
            trace_id=trace_id,
        )
    except ExternalToolError as exc:
        error_code = exc.details.get("error_code")
        status, message = _PREVIEW_TOOL_ERRORS.get(str(error_code), (502, PREVIEW_FAILED_MESSAGE))
        logger.warning(
            "agent_run_source_preview_failed",
            extra={"run_id": run.id, "error_code": error_code or exc.code},
        )
        raise RunFigureUrlError(status, message) from None
    preview = output.get("preview")
    if not isinstance(preview, dict):
        raise RunFigureUrlError(502, PREVIEW_FAILED_MESSAGE)
    url_options = {"prefix": SOURCE_PREVIEW_PATH_PREFIX, "failed_message": PREVIEW_FAILED_MESSAGE}
    content_url = _browser_url(
        preview, url_key="content_url", path_key="content_path", **url_options
    )
    page_url_template = (
        _browser_url(
            preview, url_key="page_url_template", path_key="page_path_template", **url_options
        )
        if preview.get("page_url_template") or preview.get("page_path_template")
        else None
    )
    try:
        data = RunSourcePreviewData.model_validate(
            {
                **preview,
                "page_url_template": page_url_template,
                "content_url": content_url,
            }
        )
    except ValueError:
        raise RunFigureUrlError(502, PREVIEW_FAILED_MESSAGE) from None
    # URL（トークン）はログに残さない。
    logger.info("agent_run_source_preview_issued", extra={"run_id": run.id})
    return data


def issue_run_figure_url(
    run: RunState,
    artifacts: list[Artifact],
    *,
    document_id: str,
    chunk_id: str,
    viewer_user_uuid: str,
) -> RunFigureUrlData:
    """画面を見ている利用者として RAG に図の URL を作らせる（同期。thread で呼ぶ）。"""
    if not run_has_figure_evidence(run, artifacts, document_id=document_id, chunk_id=chunk_id):
        raise RunFigureUrlError(404, NOT_IN_RUN_MESSAGE)
    client, trace_id = _rag_service_client(run, viewer_user_uuid, FIGURE_URL_PURPOSE)
    try:
        output = client.call_tool(
            READ_SOURCE_TOOL,
            {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "include_image_url": True,
                "max_chars": 1,
            },
            idempotent=True,
            trace_id=trace_id,
        )
    except ExternalToolError as exc:
        error_code = exc.details.get("error_code")
        status, message = _TOOL_ERRORS.get(str(error_code), (502, FAILED_MESSAGE))
        logger.warning(
            "agent_run_figure_url_failed",
            extra={"run_id": run.id, "error_code": error_code or exc.code},
        )
        raise RunFigureUrlError(status, message) from None
    image_url = output.get("image_url")
    if not isinstance(image_url, dict):
        raise RunFigureUrlError(502, FAILED_MESSAGE)
    expires_at = image_url.get("expires_at")
    # URL（トークン）はログに残さない。
    logger.info("agent_run_figure_url_issued", extra={"run_id": run.id})
    return RunFigureUrlData(
        url=_browser_url(image_url),
        expires_at=expires_at if isinstance(expires_at, str) else "",
    )
