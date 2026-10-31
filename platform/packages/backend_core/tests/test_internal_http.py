"""内部の宛先の判定と、環境のプロキシを使わない httpx の client の引数（#852）。"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import httpx
import pytest

from pr_backend_core.internal_http import (
    LOOPBACK_NO_PROXY_HOSTS,
    http_client_options,
    is_internal_host,
    is_internal_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:18034/run",
        "http://127.10.0.5:8000",
        "http://localhost:8000/api/mcp",
        "http://LOCALHOST.:8000",
        "http://api.localhost:8000",
        "http://[::1]:8010/api/mcp",
        "http://[::ffff:127.0.0.1]:8010",
        "http://0.0.0.0:8000",
        "http://10.0.1.20:8000",
        "http://172.16.0.1",
        "http://172.31.255.254",
        "http://192.168.1.10:8080",
        "http://169.254.169.254/opc/v2/",
        "http://[fe80::1%25eth0]:8000",
        "http://[fd12:3456::1]:8000",
        "http://rag-backend.local:8000",
        "http://host.docker.internal:8000",
        "http+unix://%2Fvar%2Frun%2Frag.sock/run",
        "127.0.0.1:18034",
    ],
)
def test_internal_urls(url: str) -> None:
    assert is_internal_url(url) is True
    assert http_client_options(url) == {"trust_env": False}


@pytest.mark.parametrize(
    "url",
    [
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/20231130/actions/chat",
        "https://objectstorage.ap-tokyo-1.oraclecloud.com",
        "https://mcp.example.com/mcp",
        "http://8.8.8.8",
        "http://172.32.0.1",
        "http://192.0.2.10",
        "http://100.64.0.1",
        "http://[2001:db8::1]",
        "http://localhost.example.com",
        "http://local",
        "",
        None,
        "http://[::1",
    ],
)
def test_external_or_unknown_urls_keep_environment_proxies(url: str | None) -> None:
    assert is_internal_url(url) is False
    assert http_client_options(url) == {}


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", True), ("[::1]", True), (" localhost ", True), ("", False), (None, False)],
)
def test_is_internal_host(host: str | None, expected: bool) -> None:
    assert is_internal_host(host) is expected


def test_loopback_no_proxy_hosts_match_start_scripts() -> None:
    assert LOOPBACK_NO_PROXY_HOSTS == ("localhost", "127.0.0.1", "::1")
    assert all(is_internal_host(host) for host in LOOPBACK_NO_PROXY_HOSTS)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def fake_proxy() -> Iterator[str]:
    """どの要求にも ``502 cannotconnect`` を返す、社内プロキシの代わり。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn:
                conn.recv(65536)
                conn.sendall(
                    b"HTTP/1.1 502 cannotconnect\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        thread.join(timeout=2)
        server.close()


def test_internal_target_bypasses_proxy_from_environment(
    monkeypatch: pytest.MonkeyPatch, fake_proxy: str
) -> None:
    """NO_PROXY の無いプロキシの環境で、内部の未起動のサービスは 502 ではなく未到達になる。"""
    for key in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_PROXY", fake_proxy)
    monkeypatch.setenv("http_proxy", fake_proxy)
    url = f"http://127.0.0.1:{_unused_port()}/run"

    # 既定の httpx はプロキシへ送り、プロキシの 502 が返る（直す前の症状）。
    with httpx.Client(timeout=2.0) as client:
        assert client.post(url).status_code == 502

    with (
        httpx.Client(timeout=2.0, **http_client_options(url)) as client,
        pytest.raises(httpx.ConnectError),
    ):
        client.post(url)
