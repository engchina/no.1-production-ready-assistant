"""Wallet の別名から再試行の設定を外した接続記述子（#1212）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pr_backend_core.oracle_dsn import (
    CLI_TCP_CONNECT_TIMEOUT_SECONDS,
    apply_cli_connect_mode,
    cli_connect_mode_enabled,
    connect_kwargs_without_retry,
    disable_cli_connect_mode,
    dsn_without_retry,
    dsn_without_tns_retry,
    enable_cli_connect_mode,
    strip_tns_retry_settings,
    tns_alias_descriptor,
)

_HIGH = (
    "(description= (retry_count=20)(retry_delay=3)(address=(protocol=tcps)(port=1522)"
    "(host=adb.ap-osaka-1.oraclecloud.com))(connect_data=(service_name=x_mydb_high.adb.oraclecloud.com))"
    "(security=(ssl_server_dn_match=yes)))"
)


def _wallet(tmp_path: Path) -> Path:
    (tmp_path / "tnsnames.ora").write_text(
        f"mydb_high = {_HIGH}\n\nmydb_low = (description=(retry_count=5)(address=(host=h)))\n",
        encoding="utf-8",
    )
    return tmp_path


def test_alias_is_replaced_by_the_descriptor_without_retry(tmp_path: Path) -> None:
    wallet = _wallet(tmp_path)

    dsn = dsn_without_tns_retry("MYDB_HIGH", wallet)

    assert "retry_count" not in dsn
    assert "retry_delay" not in dsn
    assert "service_name=x_mydb_high.adb.oraclecloud.com" in dsn
    assert "(security=(ssl_server_dn_match=yes))" in dsn
    assert dsn.count("(") == dsn.count(")")


def test_other_dsns_are_kept(tmp_path: Path) -> None:
    wallet = _wallet(tmp_path)

    assert dsn_without_tns_retry("missing_alias", wallet) == "missing_alias"
    assert dsn_without_tns_retry("host:1521/service", None) == "host:1521/service"
    assert dsn_without_tns_retry("mydb_high", "") == "mydb_high"
    assert dsn_without_tns_retry("mydb_high", tmp_path / "no-wallet") == "mydb_high"


def test_descriptor_and_strip_helpers(tmp_path: Path) -> None:
    wallet = _wallet(tmp_path)

    assert tns_alias_descriptor(wallet, "mydb_high") == _HIGH
    assert (
        tns_alias_descriptor(wallet, "mydb_low")
        == "(description=(retry_count=5)(address=(host=h)))"
    )
    assert strip_tns_retry_settings("(a=(RETRY_COUNT = 2)( retry_delay=1 )(b=1))") == "(a=(b=1))"


@pytest.fixture
def _cli_mode_reset() -> Iterator[None]:
    disable_cli_connect_mode()
    yield
    disable_cli_connect_mode()


def test_dsn_without_retry_strips_alias_and_descriptor(tmp_path: Path) -> None:
    """CLI は別名に加えて、記述子の DSN（ADB の TLS の接続文字列）の再試行も外す（#1779）。"""
    wallet = _wallet(tmp_path)
    for dsn in ("mydb_high", _HIGH):
        stripped = dsn_without_retry(dsn, wallet)
        assert "retry_count" not in stripped and "retry_delay" not in stripped, dsn
        assert "x_mydb_high.adb.oraclecloud.com" in stripped
    assert dsn_without_retry(_HIGH, None) == strip_tns_retry_settings(_HIGH)
    # 簡易接続の文字列・知らない別名はそのまま。
    assert dsn_without_retry("db.example.com:1522/svc", wallet) == "db.example.com:1522/svc"
    assert dsn_without_retry("unknown_high", wallet) == "unknown_high"
    # 接続テスト・常駐の server の別名の置き換え（#1214）は、記述子の DSN を今までどおり変えない。
    assert dsn_without_tns_retry(_HIGH, wallet) == _HIGH


def test_connect_kwargs_without_retry_caps_tcp_timeout(tmp_path: Path) -> None:
    wallet = str(_wallet(tmp_path))
    base = {
        "user": "APP",
        "password": "secret-value",
        "dsn": "mydb_high",
        "config_dir": wallet,
        "wallet_location": wallet,
        "tcp_connect_timeout": 10.0,
        "min": 1,
    }
    kwargs = connect_kwargs_without_retry(base)
    assert "retry_count" not in str(kwargs["dsn"])
    assert kwargs["retry_count"] == 0 and kwargs["retry_delay"] == 0
    assert kwargs["tcp_connect_timeout"] == CLI_TCP_CONNECT_TIMEOUT_SECONDS
    # 資格情報・Wallet・pool の引数はそのまま。元の dict は変えない。
    assert kwargs["password"] == "secret-value" and kwargs["min"] == 1
    assert kwargs["config_dir"] == wallet
    assert base["dsn"] == "mydb_high" and "retry_count" not in base
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
def test_apply_cli_connect_mode_changes_kwargs_only_in_cli(tmp_path: Path) -> None:
    """常駐の server（CLI の接続を有効にしない）は接続引数が変わらない（#1779）。"""
    wallet = str(_wallet(tmp_path))
    base = {"user": "APP", "dsn": "mydb_high", "config_dir": wallet, "tcp_connect_timeout": 10.0}
    assert not cli_connect_mode_enabled()
    server = apply_cli_connect_mode(base)
    assert server == base and server is not base

    enable_cli_connect_mode()
    assert cli_connect_mode_enabled()
    cli = apply_cli_connect_mode(base)
    assert "retry_count" not in str(cli["dsn"]) and "x_mydb_high" in str(cli["dsn"])
    assert cli["retry_count"] == 0
    assert cli["tcp_connect_timeout"] == CLI_TCP_CONNECT_TIMEOUT_SECONDS

    disable_cli_connect_mode()
    assert apply_cli_connect_mode(base) == base
