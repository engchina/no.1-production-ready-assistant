"""ASGI アプリを httpx transport 経由で同期的にテストする補助。"""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
from starlette.types import ASGIApp

from app.config import Settings, get_settings
from app.rag.request_context import (
    AuditRequestContext,
    audit_request_context_from_headers,
    reset_audit_request_context,
    set_audit_request_context,
)

TEST_TENANT_ID = "pytest-oracle-tenant"
TEST_USER_ID = "pytest-user"
TEST_REQUEST_HEADERS = {
    "X-Tenant-ID": TEST_TENANT_ID,
    "X-User-ID": TEST_USER_ID,
}
TEST_WALLET_TNSNAMES = "ragdb_high = (description=(address=(protocol=tcps)(port=1522)(host=adb)))\n"
TEST_WALLET_PEM = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
_SHARED_WALLET: tempfile.TemporaryDirectory[str] | None = None


def write_thin_wallet(wallet_dir: Path) -> Path:
    """Thin mode の mTLS 接続に必要な Wallet のファイル（tnsnames.ora / ewallet.pem）を置く。

    Oracle の readiness（platform の `database_readiness`。#325）を ok にするための偽物。
    """
    wallet_dir.mkdir(parents=True, exist_ok=True)
    (wallet_dir / "tnsnames.ora").write_text(TEST_WALLET_TNSNAMES, encoding="utf-8")
    (wallet_dir / "ewallet.pem").write_text(TEST_WALLET_PEM, encoding="utf-8")
    return wallet_dir


def shared_thin_wallet_dir() -> str:
    """読むだけのテストで共有する Wallet の配置先（プロセス終了時に削除する）。"""
    global _SHARED_WALLET
    if _SHARED_WALLET is None:
        _SHARED_WALLET = tempfile.TemporaryDirectory(prefix="rag-test-wallet-")
    return str(write_thin_wallet(Path(_SHARED_WALLET.name)))


@contextmanager
def test_audit_request_context(
    *,
    request_id: str = "pytest-request",
    settings: Settings | None = None,
) -> Iterator[AuditRequestContext]:
    """HTTP を経由しない OracleClient 呼び出しにもテスト tenant を適用する。"""
    context = audit_request_context_from_headers(
        TEST_REQUEST_HEADERS,
        request_id=request_id,
        settings=settings or get_settings(),
    )
    token = set_audit_request_context(context)
    try:
        yield context
    finally:
        reset_audit_request_context(token)


class AsgiTestClient:
    """Starlette TestClient に依存しない最小同期テストクライアント。"""

    def __init__(
        self,
        app: ASGIApp,
        base_url: str = "http://testserver",
        raise_app_exceptions: bool = True,
    ) -> None:
        self._app = app
        self._base_url = base_url
        self._raise_app_exceptions = raise_app_exceptions

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """ASGITransport で 1 リクエストを実行する。"""
        headers = {**TEST_REQUEST_HEADERS, **dict(kwargs.pop("headers", {}) or {})}

        async def send_request() -> httpx.Response:
            transport = httpx.ASGITransport(
                app=self._app,
                raise_app_exceptions=self._raise_app_exceptions,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url=self._base_url,
                follow_redirects=True,
            ) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
                await response.aread()
                return response

        return anyio.run(send_request)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET リクエストを実行する。"""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        """POST リクエストを実行する。"""
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        """PATCH リクエストを実行する。"""
        return self.request("PATCH", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        """PUT リクエストを実行する。"""
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        """DELETE リクエストを実行する。"""
        return self.request("DELETE", url, **kwargs)
