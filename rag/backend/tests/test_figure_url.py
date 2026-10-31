"""図の根拠を開く短命・対象に縛った署名つきの URL（#1311）のテスト。

- トークン（`app.rag.figure_url`）: 署名・期限・用途を区別した鍵。
- MCP（`rag_read_source` の `include_image_url`）: Agent の画面の操作（`purpose=figure_url` の
  サービストークン）でだけ URL を作る。
- 読み取り（`GET /api/figures/{token}`）: 読むたびに署名・期限・利用者の今の権限・見え方・版を
  確かめ直し、cache・Referer・MIME の推測を止めるヘッダーで返す。

Oracle・Object Storage は fake（`tests.test_mcp_api` と同じ図の PDF）。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import pytest
from pr_system_settings.auth.service_token import issue_service_token
from pytest import MonkeyPatch

from app.api.routes import figures as figures_route
from app.config import get_settings
from app.logging_config import _FigureTokenAccessFilter
from app.main import app
from app.mcp import tools as mcp_tools
from app.rag import document_crop
from app.rag.figure_url import (
    FIGURE_URL_MAX_TTL_SECONDS,
    FigureTokenError,
    FigureUrlUnavailableError,
    figure_signing_key,
    issue_figure_token,
    verify_figure_token,
)
from app.rag.request_context import current_audit_request_context
from app.schemas.search import RetrievedChunk
from app.security.domain import RoleRecord
from app.security.permissions import PUBLIC_API_PATHS, permission_for_route
from tests.security_support import ProductionAuth, enable_production_auth, login
from tests.support import AsgiTestClient
from tests.test_mcp_api import (
    _FIGURE_PAGE,
    SECRET,
    _call,
    _chunk,
    _figure_pdf,
    _FigureSourceOracle,
    _FigureStorage,
    _png_size,
)

client = AsgiTestClient(app)
UI_CLAIMS = {"purpose": "figure_url", "run_id": "run-1", "agent_id": "agent-1"}


def _headers(user_uuid: str, claims: dict[str, Any] | None = None) -> dict[str, str]:
    token = issue_service_token(
        SECRET,
        subject=user_uuid,
        audience="rag",
        issuer="agent",
        claims=UI_CLAIMS if claims is None else claims,
    )
    return {"Authorization": f"Bearer {token}"}


class _ScopedFigureOracle(_FigureSourceOracle):
    """ナレッジベース kb-1 を使える利用者にだけ図の chunk が見える fake。"""

    async def retrievable_chunk(self, document_id: str, chunk_id: str) -> RetrievedChunk | None:
        context = current_audit_request_context()
        self.contexts.append(context)
        allowed = context.allowed_knowledge_base_ids
        if allowed is not None and "kb-1" not in allowed:
            return None
        return self.visible.get((document_id, chunk_id))

    async def accessible_chunk_exists(self, document_id: str, chunk_id: str) -> bool:
        allowed = current_audit_request_context().allowed_knowledge_base_ids
        if allowed is not None and "kb-1" not in allowed:
            return False
        return (document_id, chunk_id) in self.stale


def _figure_chunk(chunk_set_id: str = "cs-1") -> RetrievedChunk:
    chunk: RetrievedChunk = _chunk(
        "c-fig",
        text="申請画面。右上の「承認」ボタンを押す。",
        content_kind="figure",
        figure_text_source="vision",
        chunk_set_id=chunk_set_id,
        recipe_id="r-1",
        bbox="[60, 80, 300, 200]",
        bbox_unit="absolute",
        **_FIGURE_PAGE,
    )
    return chunk.model_copy(update={"file_name": "申請 手順/画面.pdf"})


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> ProductionAuth:
    production = enable_production_auth(monkeypatch)
    monkeypatch.setattr(get_settings(), "app_service_token_secret", SECRET)
    return production


@pytest.fixture
def figures(monkeypatch: MonkeyPatch) -> type[_ScopedFigureOracle]:
    _ScopedFigureOracle.visible = {
        ("d1", "c-fig"): _figure_chunk(),
        ("d1", "c-text"): _chunk("c-text", content_kind="text", page_start=1),
    }
    _ScopedFigureOracle.stale = set()
    _ScopedFigureOracle.contexts = []
    _FigureStorage.files = {"docs/d1/r-1/prepared.pdf": _figure_pdf()}
    _FigureStorage.reads = []
    monkeypatch.setattr(mcp_tools, "OracleClient", _ScopedFigureOracle)
    monkeypatch.setattr(figures_route, "OracleClient", _ScopedFigureOracle)
    monkeypatch.setattr(document_crop, "ObjectStorageClient", _FigureStorage)
    return _ScopedFigureOracle


def _issue_url(user_uuid: str, chunk_id: str = "c-fig") -> dict[str, Any]:
    result = _call(
        "rag_read_source",
        {"document_id": "d1", "chunk_id": chunk_id, "include_image_url": True, "max_chars": 1},
        _headers(user_uuid),
    )
    assert result["isError"] is False, result
    image_url: dict[str, Any] = result["structuredContent"]["image_url"]
    return image_url


def _segments(token: str) -> tuple[str, dict[str, Any], str]:
    version, body, signature = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return version, payload, signature


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# ---------------------------------------------------------------------------
# トークン
# ---------------------------------------------------------------------------


def test_token_round_trip_and_tampering_are_rejected() -> None:
    token, expires_at = issue_figure_token(
        SECRET, subject="u1", document_id="d1", chunk_id="c1", chunk_set_id="cs-1", now=1000
    )
    claims = verify_figure_token(SECRET, token, now=1001)
    assert (claims.subject, claims.document_id, claims.chunk_id, claims.chunk_set_id) == (
        "u1",
        "d1",
        "c1",
        "cs-1",
    )
    assert expires_at == claims.expires_at == 1000 + FIGURE_URL_MAX_TTL_SECONDS
    # URL の path の 1 つの区切りに収まる（"/" を含まない）。
    assert "/" not in token and "?" not in token

    version, payload, signature = _segments(token)
    for change in ({"chunk": "c2"}, {"doc": "d2"}, {"sub": "u2"}, {"cs": "cs-2"}, {"exp": 1100}):
        forged = f"{version}.{_encode({**payload, **change})}.{signature}"
        with pytest.raises(FigureTokenError) as error:
            verify_figure_token(SECRET, forged, now=1001)
        assert error.value.expired is False
    with pytest.raises(FigureTokenError):
        verify_figure_token(SECRET, f"{version}.{_encode(payload)}.{signature[:-2]}AA", now=1001)
    with pytest.raises(FigureTokenError):
        verify_figure_token("other-secret-0123456789abcdef-0123456789", token, now=1001)
    for malformed in ("", "f1", "f1.a.b.c", f"f0.{_encode(payload)}.{signature}"):
        with pytest.raises(FigureTokenError):
            verify_figure_token(SECRET, malformed, now=1001)


def test_token_expires_and_ttl_is_bounded() -> None:
    token, expires_at = issue_figure_token(
        SECRET, subject="u1", document_id="d1", chunk_id="c1", chunk_set_id=None, now=1000
    )
    with pytest.raises(FigureTokenError) as error:
        verify_figure_token(SECRET, token, now=expires_at)
    assert error.value.expired is True
    # 発行時刻が未来の token（時計のずれの許容を超える）は無効。
    future, _ = issue_figure_token(
        SECRET, subject="u1", document_id="d1", chunk_id="c1", chunk_set_id=None, now=2000
    )
    with pytest.raises(FigureTokenError):
        verify_figure_token(SECRET, future, now=1000)
    with pytest.raises(ValueError):
        issue_figure_token(
            SECRET,
            subject="u1",
            document_id="d1",
            chunk_id="c1",
            chunk_set_id=None,
            ttl_seconds=FIGURE_URL_MAX_TTL_SECONDS + 1,
        )


def test_signing_key_is_derived_for_this_purpose_and_fails_closed() -> None:
    # 鍵はサービストークンの鍵そのものではない（用途を区別して導く）。
    assert figure_signing_key(SECRET) != SECRET.encode()
    assert len(figure_signing_key(SECRET)) == 32
    # サービストークンを図のトークンとしては使えない。
    service_token = issue_service_token(SECRET, subject="u1", audience="rag", issuer="agent")
    with pytest.raises(FigureTokenError):
        verify_figure_token(SECRET, service_token)
    for secret in ("", "short-secret"):
        with pytest.raises(FigureUrlUnavailableError):
            issue_figure_token(
                secret, subject="u1", document_id="d1", chunk_id="c1", chunk_set_id=None
            )
        with pytest.raises(FigureUrlUnavailableError):
            verify_figure_token(secret, "f1.e30.x")


# ---------------------------------------------------------------------------
# MCP（rag_read_source の include_image_url）
# ---------------------------------------------------------------------------


def test_mcp_issues_url_only_for_agent_screen_requests(
    auth: ProductionAuth, figures: type[_ScopedFigureOracle]
) -> None:
    user = auth.user_with_permissions("searcher", ["menu.search"], knowledge_base_ids=["kb-1"])

    image_url = _issue_url(user.user_uuid)
    assert image_url["path"].startswith(f"{mcp_tools.FIGURE_URL_PATH_PREFIX}/")
    assert image_url["url"] == f"http://testserver{image_url['path']}"
    assert image_url["expires_in_seconds"] == FIGURE_URL_MAX_TTL_SECONDS
    claims = verify_figure_token(SECRET, image_url["path"].rsplit("/", 1)[1])
    assert (claims.subject, claims.document_id, claims.chunk_id, claims.chunk_set_id) == (
        user.user_uuid,
        "d1",
        "c-fig",
        "cs-1",
    )
    # URL を作るだけでは元のファイルを読まない。
    assert _FigureStorage.reads == []

    # Run の中でモデルが呼んだとき（purpose の無いサービストークン）は作らない。
    model_call = _call(
        "rag_read_source",
        {"document_id": "d1", "chunk_id": "c-fig", "include_image_url": True},
        _headers(user.user_uuid, claims={"run_id": "run-1", "agent_id": "agent-1"}),
    )["structuredContent"]
    assert (model_call["error_code"], model_call["status"]) == (
        mcp_tools.IMAGE_URL_NOT_ALLOWED_CODE,
        403,
    )
    # 図でない根拠・見えない根拠には作らない。
    not_figure = _call(
        "rag_read_source",
        {"document_id": "d1", "chunk_id": "c-text", "include_image_url": True},
        _headers(user.user_uuid),
    )["structuredContent"]
    assert not_figure["error_code"] == mcp_tools.IMAGE_NOT_AVAILABLE_CODE
    outsider = auth.user_with_permissions("outsider", ["menu.search"], knowledge_base_ids=["kb-2"])
    hidden = _call(
        "rag_read_source",
        {"document_id": "d1", "chunk_id": "c-fig", "include_image_url": True},
        _headers(outsider.user_uuid),
    )["structuredContent"]
    assert hidden["error_code"] == mcp_tools.SOURCE_NOT_FOUND_CODE
    # 求めなければ URL は無い。
    plain = _call(
        "rag_read_source", {"document_id": "d1", "chunk_id": "c-fig"}, _headers(user.user_uuid)
    )["structuredContent"]
    assert plain["image_url"] is None


def test_local_mode_issues_url_and_fails_closed_without_key(
    figures: type[_ScopedFigureOracle], monkeypatch: MonkeyPatch
) -> None:
    del figures
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "app_service_token_secret", "")
    unavailable = _call(
        "rag_read_source", {"document_id": "d1", "chunk_id": "c-fig", "include_image_url": True}, {}
    )["structuredContent"]
    assert (unavailable["error_code"], unavailable["status"]) == (
        mcp_tools.IMAGE_URL_UNAVAILABLE_CODE,
        503,
    )
    # 鍵が無ければ、読み取りも拒否する（fail closed）。
    token, _ = issue_figure_token(
        SECRET, subject="u1", document_id="d1", chunk_id="c-fig", chunk_set_id="cs-1"
    )
    assert client.get(f"/api/figures/{token}").status_code == 503

    monkeypatch.setattr(settings, "app_service_token_secret", SECRET)
    result = _call(
        "rag_read_source", {"document_id": "d1", "chunk_id": "c-fig", "include_image_url": True}, {}
    )
    path = result["structuredContent"]["image_url"]["path"]
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


# ---------------------------------------------------------------------------
# 読み取り（GET /api/figures/{token}）
# ---------------------------------------------------------------------------


def test_figure_route_is_public_path_authenticated_by_token() -> None:
    assert "/figures/{token}" in PUBLIC_API_PATHS
    assert permission_for_route("GET", "/figures/{token}") is None
    # 同じ path の別の method は黙って公開されない。
    assert permission_for_route("POST", "/figures/{token}") is not None


def test_figure_url_returns_bounded_png_with_private_headers(
    auth: ProductionAuth, figures: type[_ScopedFigureOracle], monkeypatch: MonkeyPatch
) -> None:
    user = auth.user_with_permissions("searcher", ["menu.search"], knowledge_base_ids=["kb-1"])
    scopes: list[str] = []
    monkeypatch.setattr(
        figures_route, "enforce_rate_limit", lambda scope, _request: scopes.append(scope)
    )
    path = _issue_url(user.user_uuid)["path"]
    contexts_before = len(figures.contexts)

    # Cookie もサービストークンも使わない（path のトークンだけ）。
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('inline; filename="')
    # 日本語のファイル名は RFC 5987 で渡し、path の区切り・空白は使わない。
    assert "filename*=UTF-8''%E7%94%B3%E8%AB%8B" in disposition
    assert "_p2_figure.png" in disposition
    assert "/" not in disposition.split("filename*=", 1)[1]
    png = response.content
    assert max(_png_size(png)) <= mcp_tools.IMAGE_MAX_EDGE_PX
    assert len(png) <= mcp_tools.IMAGE_MAX_BYTES
    assert _FigureStorage.reads == ["docs/d1/r-1/prepared.pdf"]
    # 読むたびにトークンの利用者の範囲で chunk を読み直し、検索と同じ上限で守る。
    assert len(figures.contexts) == contexts_before + 1
    assert figures.contexts[-1].allowed_knowledge_base_ids == frozenset({"kb-1"})
    assert client.get(path).status_code == 200
    assert len(figures.contexts) == contexts_before + 2
    assert scopes == ["search", "search"]


def test_figure_url_rejects_tampered_expired_and_other_targets(
    auth: ProductionAuth, figures: type[_ScopedFigureOracle], monkeypatch: MonkeyPatch
) -> None:
    del figures
    user = auth.user_with_permissions("searcher", ["menu.search"], knowledge_base_ids=["kb-1"])
    token = _issue_url(user.user_uuid)["path"].rsplit("/", 1)[1]
    version, payload, signature = _segments(token)

    def status(value: str) -> int:
        response = client.get(f"/api/figures/{value}")
        # エラーの応答も cache・Referer に残さない。
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        return response.status_code

    # 改ざん（別の chunk・別の文書・別の利用者・版・期限）は署名が合わない。
    for change in (
        {"chunk": "c-text"},
        {"doc": "d2"},
        {"sub": "someone-else"},
        {"cs": "cs-2"},
        {"exp": payload["exp"] + 60},
    ):
        assert status(f"{version}.{_encode({**payload, **change})}.{signature}") == 403
    assert status("not-a-token") == 403
    # 期限切れ（5 分後）。
    later = time.time() + FIGURE_URL_MAX_TTL_SECONDS + 1
    monkeypatch.setattr(figures_route, "verify_figure_token", _verify_at(later))
    assert status(token) == 410
    assert _FigureStorage.reads == []


def _verify_at(now: float) -> Any:
    def verify(secret: str, token: str) -> Any:
        return verify_figure_token(secret, token, now=now)

    return verify


def test_figure_url_is_bound_to_user_and_rechecks_permission_and_version(
    auth: ProductionAuth, figures: type[_ScopedFigureOracle]
) -> None:
    user = auth.user_with_permissions("searcher", ["menu.search"], knowledge_base_ids=["kb-1"])
    path = _issue_url(user.user_uuid)["path"]
    assert client.get(path).status_code == 200

    # 別の利用者（kb-1 を使えない）として署名した URL では、同じ chunk でも見えない。
    outsider = auth.user_with_permissions("outsider", ["menu.search"], knowledge_base_ids=["kb-2"])
    token, _ = issue_figure_token(
        SECRET,
        subject=outsider.user_uuid,
        document_id="d1",
        chunk_id="c-fig",
        chunk_set_id="cs-1",
    )
    assert client.get(f"/api/figures/{token}").status_code == 404

    # 版が変わった（同じ chunk_id で新しい chunk_set）→ 409。
    figures.visible = {("d1", "c-fig"): _figure_chunk("cs-2")}
    assert client.get(path).status_code == 409
    # 古い版になった（有効でない chunk_set）→ 409。見えなくなった → 404。
    figures.visible = {}
    figures.stale = {("d1", "c-fig")}
    assert client.get(path).status_code == 409
    figures.stale = set()
    assert client.get(path).status_code == 404
    figures.visible = {("d1", "c-fig"): _figure_chunk()}
    assert client.get(path).status_code == 200

    # 権限の剥奪（検索の権限を外す）・ナレッジベースの対象範囲を外す・利用者の無効化。
    stored = auth.store.roles[auth.store.users[user.user_uuid].role_ids[0]]
    assert isinstance(stored, RoleRecord)
    role = stored
    role.permissions = {"menu.upload"}
    assert client.get(path).status_code == 403
    role.permissions = {"menu.search"}
    role.knowledge_base_ids = {"kb-2"}
    assert client.get(path).status_code == 404
    role.knowledge_base_ids = {"kb-1"}
    assert client.get(path).status_code == 200
    auth.store.users[user.user_uuid].status = "INACTIVE"
    assert client.get(path).status_code == 403


def test_figure_url_size_bounds_and_missing_source(
    auth: ProductionAuth, figures: type[_ScopedFigureOracle], monkeypatch: MonkeyPatch
) -> None:
    del figures
    user = auth.user_with_permissions("searcher", ["menu.search"], knowledge_base_ids=["kb-1"])
    path = _issue_url(user.user_uuid)["path"]
    max_bytes = mcp_tools.IMAGE_MAX_BYTES
    monkeypatch.setattr(mcp_tools, "IMAGE_MAX_BYTES", 10)
    assert client.get(path).status_code == 413
    monkeypatch.setattr(mcp_tools, "IMAGE_MAX_BYTES", max_bytes)
    assert client.get(path).status_code == 200
    _FigureStorage.files = {}
    assert client.get(path).status_code == 404


def test_access_log_masks_figure_token() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/api/figures/f1.abc.def", "1.1", 200),
        None,
    )
    assert _FigureTokenAccessFilter().filter(record) is True
    assert record.getMessage() == '127.0.0.1:1 - "GET /api/figures/{token} HTTP/1.1" 200'
    assert "f1.abc.def" not in record.getMessage()


# ---------------------------------------------------------------------------
# 画面の図（GET /api/figures?document_id=&chunk_id=。#2428）
# ---------------------------------------------------------------------------


def _screen_url(chunk_id: str = "c-fig", document_id: str = "d1") -> str:
    return f"/api/figures?document_id={document_id}&chunk_id={chunk_id}"


def test_screen_figure_route_needs_document_view_permission() -> None:
    assert "/figures" not in PUBLIC_API_PATHS
    allowed = permission_for_route("GET", "/figures")
    assert allowed == permission_for_route("GET", "/documents/{document_id}/preview-pages")


def test_screen_figure_returns_png_in_the_session_users_scope(
    auth: ProductionAuth, figures: type[_ScopedFigureOracle], monkeypatch: MonkeyPatch
) -> None:
    scopes: list[str] = []
    monkeypatch.setattr(
        figures_route, "enforce_rate_limit", lambda scope, _request: scopes.append(scope)
    )
    auth.user_with_permissions("chat-user", ["menu.chat"], knowledge_base_ids=["kb-1"])
    headers = login(client, "chat-user")
    response = client.get(_screen_url(), headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "_p2_figure.png" in response.headers["content-disposition"]
    assert max(_png_size(response.content)) <= mcp_tools.IMAGE_MAX_EDGE_PX
    # 保存した metadata の領域で、セッションの利用者の範囲で chunk を読み直す。
    assert figures.contexts[-1].allowed_knowledge_base_ids == frozenset({"kb-1"})
    assert scopes == ["search"]

    # 図でない根拠・見えない根拠は 404、古い版は 409。
    assert client.get(_screen_url("c-text"), headers=headers).status_code == 404
    assert client.get(_screen_url("other"), headers=headers).status_code == 404
    figures.visible = {}
    figures.stale = {("d1", "c-fig")}
    assert client.get(_screen_url(), headers=headers).status_code == 409
    figures.stale = set()
    figures.visible = {("d1", "c-fig"): _figure_chunk()}
    # ナレッジベースの範囲の外の利用者には見えない（404）。
    auth.user_with_permissions("outsider", ["menu.chat"], knowledge_base_ids=["kb-2"])
    assert client.get(_screen_url(), headers=login(client, "outsider")).status_code == 404
    # 文書を見る画面の権限が無い利用者は経路で 403、未ログインは 401。
    auth.user_with_permissions("evaluator", ["menu.evaluation"], knowledge_base_ids=["kb-1"])
    assert client.get(_screen_url(), headers=login(client, "evaluator")).status_code == 403
    assert client.get(_screen_url()).status_code == 401
