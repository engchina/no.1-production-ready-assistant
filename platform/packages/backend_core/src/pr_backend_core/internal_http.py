"""内部のサービス・MCP への HTTP を環境のプロキシに通さないための helper（#852）。

OCI に届くために ``HTTP_PROXY`` / ``HTTPS_PROXY``（社内プロキシ）を設定し、``NO_PROXY`` を
設定しない環境では、httpx の既定（``trust_env=True``）が 127.0.0.1 などの内部の宛先への要求まで
プロキシへ送る。プロキシは内部の宛先へ届けられず ``502 cannotconnect`` などを返すため、
未起動のサービスへの「未到達」が「サービスが応答した失敗」に見えてしまう。

宛先が内部（loopback・link-local・private network・``localhost`` / ``*.local``・unix socket）の
ときだけ ``trust_env=False`` にして環境のプロキシを使わない。外部の宛先（OCI など）は今までどおり
環境のプロキシを使う。DNS は引かない（名前の形と IP の値だけで判定する）。

使い方::

    with httpx.Client(timeout=10.0, **http_client_options(url)) as client:
        client.post(url, ...)

httpx を import しない（``pr_backend_core`` は httpx に依存しない）。
"""

from __future__ import annotations

import ipaddress
from typing import TypedDict
from urllib.parse import urlsplit

__all__ = [
    "HttpClientOptions",
    "LOOPBACK_NO_PROXY_HOSTS",
    "http_client_options",
    "is_internal_host",
    "is_internal_url",
]

# 起動スクリプトが NO_PROXY に足す loopback（scripts/start-backend.sh と同じ値）。
LOOPBACK_NO_PROXY_HOSTS = ("localhost", "127.0.0.1", "::1")

_INTERNAL_NAME_SUFFIXES = (".localhost", ".local", ".internal")
_UNIX_SOCKET_SCHEMES = frozenset({"unix", "http+unix", "https+unix"})
# RFC 1918 と IPv6 の ULA（fc00::/7）。ipaddress の is_private は文書用・予約済みの範囲も含むため、
# 内部の network として使う範囲だけを明示する。
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class HttpClientOptions(TypedDict, total=False):
    """``httpx.Client`` / ``httpx.AsyncClient`` に ``**`` で渡す引数。"""

    trust_env: bool


def is_internal_host(host: str | None) -> bool:
    """host が同じマシン・private network の宛先か（プロキシを通しても届かない宛先か）。"""
    if not host:
        return False
    name = host.strip().strip("[]").rstrip(".").lower()
    if not name:
        return False
    if name == "localhost" or name.endswith(_INTERNAL_NAME_SUFFIXES):
        return True
    # IPv6 の zone（fe80::1%eth0）を除いてから IP として読む。
    try:
        address = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback or address.is_link_local or address.is_unspecified:
        return True
    return any(address in network for network in _PRIVATE_NETWORKS)


def is_internal_url(url: str | None) -> bool:
    """URL の宛先が内部か。unix socket は内部、host の無い・読めない URL は内部ではない。"""
    if not url:
        return False
    text = url.strip()
    try:
        parts = urlsplit(text if "://" in text else f"http://{text}")
        hostname = parts.hostname
    except ValueError:
        return False
    if parts.scheme.lower() in _UNIX_SOCKET_SCHEMES:
        return True
    return is_internal_host(hostname)


def http_client_options(url: str | None) -> HttpClientOptions:
    """宛先が内部なら環境のプロキシを使わない httpx の client の引数を返す（外部なら空）。

    ``trust_env=False`` は環境のプロキシに加えて ``SSL_CERT_FILE`` / ``.netrc`` も読まない。
    内部の宛先は平文の HTTP か、証明書を明示する前提のため影響しない。
    """
    if is_internal_url(url):
        return {"trust_env": False}
    return {}
