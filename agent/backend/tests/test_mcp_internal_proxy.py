"""内部の MCP 接続は環境のプロキシを通さない（#852）。

OCI のために `HTTP_PROXY` を設定し `NO_PROXY` の無い環境で、127.0.0.1 の RAG / NL2SQL の MCP へ
送る要求がプロキシへ送られ、プロキシの `502 cannotconnect` になっていた。
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest
from pytest import MonkeyPatch

import app.features.agent.tools as tools_module
from app.features.agent.tools import ExternalToolError


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def proxy_received(monkeypatch: MonkeyPatch) -> Iterator[list[bytes]]:
    """NO_PROXY の無いプロキシの環境。プロキシはどの要求にも `502 cannotconnect` を返す。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(0.1)
    stop = threading.Event()
    received: list[bytes] = []

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn:
                received.append(conn.recv(65536))
                conn.sendall(
                    b"HTTP/1.1 502 cannotconnect\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    proxy = f"http://127.0.0.1:{server.getsockname()[1]}"
    for key in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    for key in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(key, proxy)
    try:
        yield received
    finally:
        stop.set()
        thread.join(timeout=2)
        server.close()


def test_local_mcp_is_not_sent_through_the_environment_proxy(
    proxy_received: list[bytes],
) -> None:
    """未起動のローカルの MCP は、プロキシの 502 ではなく接続の失敗として返る。"""
    with pytest.raises(ExternalToolError) as exc:
        tools_module._post_mcp_message(
            service_code="rag",
            service_label="RAG MCP",
            url=f"http://127.0.0.1:{_unused_loopback_port()}/api/mcp",
            payload={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={},
            timeout_seconds=2.0,
            max_retries=0,
        )
    # 接続できない（送信前の失敗）は `unreachable`（#854）。
    assert exc.value.code == "rag.unreachable"
    assert proxy_received == []
