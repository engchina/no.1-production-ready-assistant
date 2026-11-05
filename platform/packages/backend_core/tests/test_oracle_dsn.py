"""接続記述子の DSN から再試行の設定を外す（#1212）。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pr_backend_core.oracle_dsn import (
    CLI_TCP_CONNECT_TIMEOUT_SECONDS,
    apply_cli_connect_mode,
    cli_connect_mode_enabled,
    connect_kwargs_without_retry,
    disable_cli_connect_mode,
    dsn_without_retry,
    enable_cli_connect_mode,
    strip_tns_retry_settings,
)

_DESCRIPTOR = (
    "(description= (retry_count=20)(retry_delay=3)(address=(protocol=tcps)(port=1522)"
    "(host=adb.example.com))(connect_data=(service_name=mydb_svc.example.com))"
    "(security=(ssl_server_dn_match=yes)))"
)
_EASY_CONNECT = "db.example.com:1521/mydb_svc"


def test_strip_tns_retry_settings() -> None:
    assert strip_tns_retry_settings("(a=(RETRY_COUNT = 2)( retry_delay=1 )(b=1))") == "(a=(b=1))"


@pytest.fixture
def _cli_mode_reset() -> Iterator[None]:
    disable_cli_connect_mode()
    yield
    disable_cli_connect_mode()


def test_dsn_without_retry_strips_descriptor_only() -> None:
    """記述子の DSN の再試行を外す（#1779）。簡易接続の文字列はそのまま。"""
    stripped = dsn_without_retry(_DESCRIPTOR)
    assert "retry_count" not in stripped and "retry_delay" not in stripped
    assert "service_name=mydb_svc.example.com" in stripped
    assert "(security=(ssl_server_dn_match=yes))" in stripped
    assert stripped.count("(") == stripped.count(")")
    assert dsn_without_retry(_EASY_CONNECT) == _EASY_CONNECT


def test_connect_kwargs_without_retry_caps_tcp_timeout() -> None:
    base = {
        "user": "APP",
        "password": "secret-value",
        "dsn": _DESCRIPTOR,
        "tcp_connect_timeout": 10.0,
        "min": 1,
    }
    kwargs = connect_kwargs_without_retry(base)
    assert "retry_count" not in str(kwargs["dsn"])
    assert kwargs["retry_count"] == 0 and kwargs["retry_delay"] == 0
    assert kwargs["tcp_connect_timeout"] == CLI_TCP_CONNECT_TIMEOUT_SECONDS
    # 資格情報・pool の引数はそのまま。元の dict は変えない。
    assert kwargs["password"] == "secret-value" and kwargs["min"] == 1
    assert base["dsn"] == _DESCRIPTOR and "retry_count" not in base
    # 設定がより短ければその値、無い・不正なら上限。
    shorter = connect_kwargs_without_retry({"dsn": "x", "tcp_connect_timeout": 2})
    assert shorter["tcp_connect_timeout"] == 2.0
    for invalid in (None, 0, -1, True, "5"):
        kwargs = connect_kwargs_without_retry({"dsn": "x", "tcp_connect_timeout": invalid})
        assert kwargs["tcp_connect_timeout"] == CLI_TCP_CONNECT_TIMEOUT_SECONDS, invalid
    assert connect_kwargs_without_retry({"dsn": "x"})["tcp_connect_timeout"] == (
        CLI_TCP_CONNECT_TIMEOUT_SECONDS
    )


@pytest.mark.usefixtures("_cli_mode_reset")
def test_apply_cli_connect_mode_changes_kwargs_only_in_cli() -> None:
    """常駐の server（CLI の接続を有効にしない）は接続引数が変わらない（#1779）。"""
    base = {"user": "APP", "dsn": _DESCRIPTOR, "tcp_connect_timeout": 10.0}
    assert not cli_connect_mode_enabled()
    server = apply_cli_connect_mode(base)
    assert server == base and server is not base

    enable_cli_connect_mode()
    assert cli_connect_mode_enabled()
    cli = apply_cli_connect_mode(base)
    assert "retry_count" not in str(cli["dsn"]) and "mydb_svc" in str(cli["dsn"])
    assert cli["retry_count"] == 0
    assert cli["tcp_connect_timeout"] == CLI_TCP_CONNECT_TIMEOUT_SECONDS

    disable_cli_connect_mode()
    assert apply_cli_connect_mode(base) == base
