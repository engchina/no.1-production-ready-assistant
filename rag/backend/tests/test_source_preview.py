"""根拠 1 件の原文のプレビューの署名つきの URL（#2389）。

- MCP の ``rag_read_source``（``include_preview``）は、Agent の画面の操作（``purpose`` のある
  サービストークン）でだけ、検索と同じ見え方の条件で見える根拠にプレビューの情報を作る。
- ``GET /api/source-previews/{token}/pages/{page}`` と ``…/content`` は、読むたびに利用者の今の
  権限・対象範囲と版を確かめ直す（改ざん 403・期限切れ 410・見えない 404・古い版 409）。
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest
from pr_system_settings.auth.service_token import issue_service_token
from pytest import MonkeyPatch

from app.api.routes import documents as documents_route
from app.config import get_settings
from app.logging_config import _FigureTokenAccessFilter
from app.main import app
from app.mcp import tools as mcp_tools
from app.rag.figure_url import issue_figure_token
from app.rag.request_context import current_audit_request_context
from app.rag.signed_url import SignedTokenError, SignedUrlUnavailableError
from app.rag.source_preview import (
    SOURCE_PREVIEW_TTL_SECONDS,
    issue_source_preview_token,
    preview_kind,
    source_preview_signing_key,
    verify_source_preview_token,
)
from app.schemas.document import DocumentPreprocessArtifact
from app.schemas.search import RetrievedChunk
from app.security.domain import RoleRecord
from app.security.permissions import PUBLIC_API_PATHS, permission_for_route
from tests.security_support import ProductionAuth, enable_production_auth
from tests.support import AsgiTestClient
from tests.test_mcp_api import SECRET, _call, _figure_pdf, _png_size

client = AsgiTestClient(app)
PREVIEW_CLAIMS = {"purpose": "source_preview", "run_id": "run-1", "agent_id": "agent-1"}
_PDF_PATH = "docs/d1/source.pdf"
_PREPARED_PATH = "docs/d2/r-1/prepared.pdf"
_TEXT_PATH = "docs/d3/source.json"


def _headers(user_uuid: str, claims: dict[str, Any] | None = None) -> dict[str, str]:
    token = issue_service_token(
        SECRET,
        subject=user_uuid,
        audience="rag",
        issuer="agent",
        claims=PREVIEW_CLAIMS if claims is None else claims,
    )
    return {"Authorization": f"Bearer {token}"}


def _chunk(document_id: str, chunk_id: str, file_name: str, **metadata: Any) -> RetrievedChunk:
    return RetrievedChunk(
        document_id=document_id,
        chunk_id=chunk_id,
        text="合成の本文。",
        score=0.5,
        file_name=file_name,
        metadata={"chunk_set_id": "cs-1", **metadata},
    )


def _documents() -> dict[str, Any]:
    return {
        "d1": SimpleNamespace(
            file_name="文書 A.pdf",
            content_type="application/pdf",
            object_storage_path=_PDF_PATH,
            preprocess_artifact=None,
        ),
        "d2": SimpleNamespace(
            file_name="文書 B.docx",
            content_type=None,
            object_storage_path="docs/d2/source.docx",
            preprocess_artifact=None,
        ),
        "d3": SimpleNamespace(
            file_name="文書 C.json",
            content_type="application/json",
            object_storage_path=_TEXT_PATH,
            preprocess_artifact=None,
        ),
    }


class _PreviewOracle:
    """ナレッジベース kb-1 を使える利用者にだけ根拠の chunk が見える fake。"""

    visible: dict[tuple[str, str], RetrievedChunk] = {}
    stale: set[tuple[str, str]] = set()
    documents: dict[str, Any] = {}

    async def retrievable_chunk(self, document_id: str, chunk_id: str) -> RetrievedChunk | None:
        allowed = current_audit_request_context().allowed_knowledge_base_ids
        if allowed is not None and "kb-1" not in allowed:
            return None
        return self.visible.get((document_id, chunk_id))

    async def accessible_chunk_exists(self, document_id: str, chunk_id: str) -> bool:
        allowed = current_audit_request_context().allowed_knowledge_base_ids
        if allowed is not None and "kb-1" not in allowed:
            return False
        return (document_id, chunk_id) in self.stale

    async def chunk_set_extraction_recipe_ids(self, chunk_set_ids: list[str]) -> dict[str, str]:
        return {}

    async def get_document(self, document_id: str) -> Any:
        return self.documents.get(document_id)

    async def get_document_recipe(self, document_id: str, recipe_id: str) -> Any:
        if (document_id, recipe_id) != ("d2", "r-1"):
            return None
        return {
            "preprocess_artifact": {
                "derivation_id": "dv-1",
                "profile": "office_to_pdf",
                "converted": True,
                "object_storage_path": _PREPARED_PATH,
                "content_type": "application/pdf",
                "file_name": "prepared.pdf",
            }
        }


class _Storage:
    files: dict[str, bytes] = {}
    reads: list[str] = []

    async def get(self, path: str) -> bytes:
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> ProductionAuth:
    production = enable_production_auth(monkeypatch)
    monkeypatch.setattr(get_settings(), "app_service_token_secret", SECRET)
    return production


@pytest.fixture
def previews(monkeypatch: MonkeyPatch) -> type[_PreviewOracle]:
    _PreviewOracle.visible = {
        ("d1", "c1"): _chunk(
            "d1",
            "c1",
            "文書 A.pdf",
            page_start=2,
            page_width=600,
            page_height=800,
            bbox="[60, 80, 300, 200]",
            bbox_unit="absolute",
            parent_text="親の本文",
            section_title="第 1 節",
        ),
        ("d2", "c2"): _chunk("d2", "c2", "文書 B.docx", recipe_id="r-1", page_start=1),
        ("d3", "c3"): _chunk("d3", "c3", "文書 C.json"),
    }
    _PreviewOracle.stale = set()
    _PreviewOracle.documents = _documents()
    _Storage.files = {
        _PDF_PATH: _figure_pdf(),
        _PREPARED_PATH: _figure_pdf(),
        _TEXT_PATH: '{"合成": "本文"}'.encode(),
    }
    _Storage.reads = []
    from app.api.routes import source_previews as source_previews_route

    monkeypatch.setattr(mcp_tools, "OracleClient", _PreviewOracle)
    monkeypatch.setattr(source_previews_route, "OracleClient", _PreviewOracle)
    monkeypatch.setattr(documents_route, "FileStorageClient", _Storage)
    return _PreviewOracle


def _preview(user_uuid: str, document_id: str = "d1", chunk_id: str = "c1") -> dict[str, Any]:
    result = _call(
        "rag_read_source",
        {"document_id": document_id, "chunk_id": chunk_id, "include_preview": True, "max_chars": 1},
        _headers(user_uuid),
    )
    assert result["isError"] is False, result
    preview: dict[str, Any] = result["structuredContent"]["preview"]
    return preview


def _page_path(preview: dict[str, Any], page: int = 1) -> str:
    return str(preview["page_path_template"]).replace("{page}", str(page))


# ---------------------------------------------------------------------------
# トークン
# ---------------------------------------------------------------------------


def _issue(**overrides: Any) -> tuple[str, int]:
    values: dict[str, Any] = {
        "subject": "u1",
        "document_id": "d1",
        "chunk_id": "c1",
        "chunk_set_id": "cs-1",
        "recipe_id": None,
        "variant": "original",
        **overrides,
    }
    return issue_source_preview_token(SECRET, **values)


def test_token_round_trip_tampering_expiry_and_purpose_separation() -> None:
    now = time.time()
    token, expires_at = _issue(now=now)
    claims = verify_source_preview_token(SECRET, token, now=now)
    assert (claims.subject, claims.document_id, claims.chunk_id, claims.chunk_set_id) == (
        "u1",
        "d1",
        "c1",
        "cs-1",
    )
    assert (claims.variant, claims.recipe_id) == ("original", None)
    assert expires_at - claims.issued_at == SOURCE_PREVIEW_TTL_SECONDS
    # 改ざん（中身の差し替え）・別の鍵は無効。
    version, body, signature = token.split(".")
    with pytest.raises(SignedTokenError):
        verify_source_preview_token(SECRET, f"{version}.{body}x.{signature}", now=now)
    with pytest.raises(SignedTokenError):
        verify_source_preview_token(SECRET.replace("a", "b"), token, now=now)
    # 期限切れは expired。
    with pytest.raises(SignedTokenError) as expired:
        verify_source_preview_token(SECRET, token, now=now + SOURCE_PREVIEW_TTL_SECONDS + 1)
    assert expired.value.expired is True
    # 図のトークン・サービストークンはプレビューのトークンとして使えない（用途の印が違う）。
    figure, _ = issue_figure_token(
        SECRET, subject="u1", document_id="d1", chunk_id="c1", chunk_set_id="cs-1"
    )
    with pytest.raises(SignedTokenError):
        verify_source_preview_token(SECRET, figure)
    service = issue_service_token(SECRET, subject="u1", audience="rag", issuer="agent")
    with pytest.raises(SignedTokenError):
        verify_source_preview_token(SECRET, service)
    # 鍵は用途を区別して導き、短い鍵では作らない・読ませない（fail closed）。
    assert source_preview_signing_key(SECRET) != SECRET.encode()
    for secret in ("", "short"):
        with pytest.raises(SignedUrlUnavailableError):
            issue_source_preview_token(
                secret,
                subject="u1",
                document_id="d1",
                chunk_id="c1",
                chunk_set_id=None,
                recipe_id=None,
                variant="original",
            )
        with pytest.raises(SignedUrlUnavailableError):
            verify_source_preview_token(secret, token)


@pytest.mark.parametrize(
    ("file_name", "prepared", "expected"),
    [
        ("a.pdf", False, ("pdf", "original")),
        ("a.PNG", False, ("image", "original")),
        ("a.json", False, ("text", "original")),
        ("a.docx", True, ("pdf", "prepared")),
        ("a.docx", False, ("unsupported", "original")),
        ("a.zip", True, ("unsupported", "original")),
    ],
    ids=["pdf", "image", "text", "office-prepared", "office-only", "unknown"],
)
def test_preview_kind_matches_the_rag_source_sheet(
    file_name: str, prepared: bool, expected: tuple[str, str]
) -> None:
    artifact = (
        DocumentPreprocessArtifact(
            derivation_id="dv",
            profile="office_to_pdf",
            object_storage_path="docs/p.pdf",
            content_type="application/pdf",
            file_name="p.pdf",
        )
        if prepared
        else None
    )
    assert preview_kind(file_name, artifact) == expected


# ---------------------------------------------------------------------------
# MCP（rag_read_source の include_preview）
# ---------------------------------------------------------------------------


def test_mcp_returns_preview_only_for_agent_screen_requests(
    auth: ProductionAuth, previews: type[_PreviewOracle]
) -> None:
    del previews
    user = auth.user_with_permissions("searcher", ["menu.search"], knowledge_base_ids=["kb-1"])

    preview = _preview(user.user_uuid)
    assert preview["kind"] == "pdf"
    assert preview["file_name"] == "文書 A.pdf"
    assert [page["page_number"] for page in preview["pages"]] == [1, 2]
    assert preview["pages"][0]["width"] == pytest.approx(600)
    assert preview["page_path_template"].startswith(f"{mcp_tools.SOURCE_PREVIEW_PATH_PREFIX}/")
    assert preview["page_path_template"].endswith("/pages/{page}")
    assert preview["page_url_template"] == f"http://testserver{preview['page_path_template']}"
    assert preview["content_path"].endswith("/content")
    assert preview["expires_in_seconds"] == SOURCE_PREVIEW_TTL_SECONDS
    # 強調に要る位置だけ（本文・親の本文・章節の名前などの業務の内容は返さない）。
    metadata = preview["highlight_metadata"]
    assert metadata["bbox"] == "[60, 80, 300, 200]"
    assert (metadata["page_start"], metadata["page_width"], metadata["page_height"]) == (
        2,
        600,
        800,
    )
    assert "parent_text" not in metadata
    token = preview["page_path_template"].split("/")[3]
    claims = verify_source_preview_token(SECRET, token)
    assert (claims.subject, claims.document_id, claims.chunk_id, claims.variant) == (
        user.user_uuid,
        "d1",
        "c1",
        "original",
    )

    # Run の中でモデルが呼んだとき（purpose の無いサービストークン）は作らない。
    model_call = _call(
        "rag_read_source",
        {"document_id": "d1", "chunk_id": "c1", "include_preview": True},
        _headers(user.user_uuid, claims={"run_id": "run-1", "agent_id": "agent-1"}),
    )["structuredContent"]
    assert (model_call["error_code"], model_call["status"]) == (
        mcp_tools.PREVIEW_NOT_ALLOWED_CODE,
        403,
    )
    # 見えない根拠（ナレッジベースの範囲の外の利用者）には作らない。
    outsider = auth.user_with_permissions("outsider", ["menu.search"], knowledge_base_ids=["kb-2"])
    hidden = _call(
        "rag_read_source",
        {"document_id": "d1", "chunk_id": "c1", "include_preview": True},
        _headers(outsider.user_uuid),
    )["structuredContent"]
    assert hidden["error_code"] == mcp_tools.SOURCE_NOT_FOUND_CODE
    # 求めなければプレビューは無い。
    plain = _call(
        "rag_read_source", {"document_id": "d1", "chunk_id": "c1"}, _headers(user.user_uuid)
    )["structuredContent"]
    assert plain["preview"] is None


def test_mcp_preview_kinds_office_text_and_missing_file(
    auth: ProductionAuth, previews: type[_PreviewOracle]
) -> None:
    user = auth.user_with_permissions("searcher", ["menu.search"], knowledge_base_ids=["kb-1"])
    # Office の原本は、処理レシピの処理後の PDF のページ。
    office = _preview(user.user_uuid, "d2", "c2")
    assert (office["kind"], len(office["pages"])) == ("pdf", 2)
    claims = verify_source_preview_token(SECRET, office["content_path"].split("/")[3])
    assert (claims.recipe_id, claims.variant) == ("r-1", "prepared")
    assert _PREPARED_PATH in _Storage.reads
    # 本文（JSON など）はページの一覧を持たず、原文の URL で読む。ファイルはまだ読まない。
    _Storage.reads = []
    text = _preview(user.user_uuid, "d3", "c3")
    assert (text["kind"], text["pages"], text["page_path_template"]) == ("text", [], None)
    assert _Storage.reads == []
    # ファイルが保存先に無い PDF は理由つきのエラー。
    previews.documents["d1"].object_storage_path = "docs/d1/missing.pdf"
    missing = _call(
        "rag_read_source",
        {"document_id": "d1", "chunk_id": "c1", "include_preview": True},
        _headers(user.user_uuid),
    )["structuredContent"]
    assert (missing["error_code"], missing["status"]) == (mcp_tools.PREVIEW_FILE_MISSING_CODE, 404)


# ---------------------------------------------------------------------------
# 読み取り（GET /api/source-previews/{token}/…）
# ---------------------------------------------------------------------------


def test_routes_are_public_paths_authenticated_by_token() -> None:
    for path in (
        "/source-previews/{token}/pages/{page_number}",
        "/source-previews/{token}/content",
    ):
        assert path in PUBLIC_API_PATHS
        assert permission_for_route("GET", path) is None
        # 同じ path の別の method は黙って公開されない。
        assert permission_for_route("POST", path) is not None


def test_page_and_content_return_private_responses(
    auth: ProductionAuth, previews: type[_PreviewOracle]
) -> None:
    del previews
    user = auth.user_with_permissions("searcher", ["menu.search"], knowledge_base_ids=["kb-1"])
    preview = _preview(user.user_uuid)

    page = client.get(f"{_page_path(preview, 2)}?dpi=72")
    assert page.status_code == 200
    assert page.headers["content-type"] == "image/png"
    assert page.headers["cache-control"] == "private, no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert _png_size(page.content) == (600, 800)
    assert client.get(_page_path(preview, 9)).status_code == 422

    content = client.get(preview["content_path"])
    assert content.status_code == 200
    assert content.headers["content-type"] == "application/pdf"
    assert content.headers["cache-control"] == "private, no-store"
    assert content.headers["access-control-allow-origin"] == "*"
    download = client.get(f"{preview['content_path']}?disposition=attachment")
    assert download.headers["content-disposition"].startswith("attachment;")

    text = client.get(_preview(user.user_uuid, "d3", "c3")["content_path"])
    assert text.status_code == 200
    assert text.headers["content-type"].startswith("application/json")
    assert text.content.decode() == '{"合成": "本文"}'


def test_urls_are_bound_to_user_and_recheck_permission_and_version(
    auth: ProductionAuth, previews: type[_PreviewOracle]
) -> None:
    user = auth.user_with_permissions("searcher", ["menu.search"], knowledge_base_ids=["kb-1"])
    preview = _preview(user.user_uuid)
    path = _page_path(preview)
    assert client.get(path).status_code == 200

    # 改ざん・期限切れ。
    assert client.get(path.replace("/pages/", "x/pages/")).status_code == 403
    expired, _ = _issue(subject=user.user_uuid, now=time.time() - SOURCE_PREVIEW_TTL_SECONDS - 5)
    assert client.get(f"/api/source-previews/{expired}/pages/1").status_code == 410
    # 図のトークンでは読めない。
    figure, _ = issue_figure_token(
        SECRET, subject=user.user_uuid, document_id="d1", chunk_id="c1", chunk_set_id="cs-1"
    )
    assert client.get(f"/api/source-previews/{figure}/content").status_code == 403

    # 別の利用者（kb-1 を使えない）として署名した URL では、同じ文書でも見えない。
    outsider = auth.user_with_permissions("outsider", ["menu.search"], knowledge_base_ids=["kb-2"])
    token, _ = _issue(subject=outsider.user_uuid)
    assert client.get(f"/api/source-previews/{token}/pages/1").status_code == 404
    assert client.get(f"/api/source-previews/{token}/content").status_code == 404

    # 版が変わった → 409。古い版になった → 409。見えなくなった → 404。
    current = previews.visible[("d1", "c1")]
    previews.visible = {
        ("d1", "c1"): current.model_copy(
            update={"metadata": {**current.metadata, "chunk_set_id": "cs-2"}}
        )
    }
    assert client.get(path).status_code == 409
    previews.visible = {}
    previews.stale = {("d1", "c1")}
    assert client.get(path).status_code == 409
    previews.stale = set()
    assert client.get(preview["content_path"]).status_code == 404
    previews.visible = {("d1", "c1"): current}
    assert client.get(path).status_code == 200

    # 権限の剥奪・ナレッジベースの対象範囲を外す・利用者の無効化。
    stored = auth.store.roles[auth.store.users[user.user_uuid].role_ids[0]]
    assert isinstance(stored, RoleRecord)
    stored.permissions = {"menu.upload"}
    assert client.get(path).status_code == 403
    stored.permissions = {"menu.search"}
    stored.knowledge_base_ids = {"kb-2"}
    assert client.get(path).status_code == 404
    stored.knowledge_base_ids = {"kb-1"}
    assert client.get(path).status_code == 200
    auth.store.users[user.user_uuid].status = "INACTIVE"
    assert client.get(path).status_code == 403


def test_missing_file_and_missing_key(
    auth: ProductionAuth, previews: type[_PreviewOracle], monkeypatch: MonkeyPatch
) -> None:
    del previews
    user = auth.user_with_permissions("searcher", ["menu.search"], knowledge_base_ids=["kb-1"])
    preview = _preview(user.user_uuid)
    _Storage.files = {}
    assert client.get(_page_path(preview)).status_code == 404
    assert client.get(preview["content_path"]).status_code == 404
    monkeypatch.setattr(get_settings(), "app_service_token_secret", "")
    assert client.get(preview["content_path"]).status_code == 503


def test_access_log_masks_preview_token() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/api/source-previews/p1.abc.def/pages/2?dpi=144", "1.1", 200),
        None,
    )
    assert _FigureTokenAccessFilter().filter(record) is True
    assert record.getMessage() == (
        '127.0.0.1:1 - "GET /api/source-previews/{token}/pages/2?dpi=144 HTTP/1.1" 200'
    )
