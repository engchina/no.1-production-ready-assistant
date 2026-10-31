#!/usr/bin/env python3
"""共通 .env（platform/.env）の値だけで ADB Thin + mTLS 接続を検証する単体スクリプト。

Oracle AI Database の接続設定は3製品共通（PLATFORM_ORACLE_*）なので共通 .env から読む（#211）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from dotenv import dotenv_values
from pr_backend_core.config import platform_env_file

MASKED = "<present>"
EMPTY = "<empty>"
WALLET_REQUIRED_FILES = ("tnsnames.ora", "ewallet.pem")
PUBLIC_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://checkip.amazonaws.com",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "共通 .env（platform/.env）から Oracle ADB Thin + mTLS の接続条件を読み、"
            "socket 到達性と python-oracledb 接続を単体検証します。"
        )
    )
    parser.add_argument(
        "--env-file",
        default=str(platform_env_file(Path(__file__).resolve().parents[1])),
        help="読み込む .env ファイル。既定は共通 .env（PLATFORM_ENV_FILE または platform/.env）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="socket / DB 接続 timeout 秒。既定は 10 秒",
    )
    parser.add_argument(
        "--skip-public-ip",
        action="store_true",
        help="外部サービスによる public egress IP 確認を省略します。",
    )
    parser.add_argument(
        "--refresh-wallet-from-oci",
        action="store_true",
        help=(
            "OCI Database API で ADB Wallet を再生成し、PLATFORM_ORACLE_WALLET_DIR に設置して "
            "PLATFORM_ORACLE_WALLET_PASSWORD を共通 .env へ保存してから検証します。"
        ),
    )
    args = parser.parse_args()

    env_file = Path(args.env_file).expanduser().resolve()
    if args.refresh_wallet_from_oci:
        print_header("Refresh Wallet From OCI")
        if not refresh_wallet_from_oci():
            return 2

    env = load_env(env_file)

    print_header("Environment")
    print(f"env_file={env_file}")
    print_setting(env, "PLATFORM_ORACLE_USER")
    print_setting(env, "PLATFORM_ORACLE_PASSWORD", secret=True)
    print_setting(env, "PLATFORM_ORACLE_DSN")
    print_setting(env, "PLATFORM_ORACLE_DRIVER_MODE")
    print_setting(env, "PLATFORM_ORACLE_CONNECTION_SECURITY")
    print_setting(env, "PLATFORM_ORACLE_CLIENT_LIB_DIR")
    print_setting(env, "PLATFORM_ORACLE_WALLET_DIR")
    print_setting(env, "PLATFORM_ORACLE_WALLET_PASSWORD", secret=True)
    print_setting(env, "PLATFORM_ORACLE_ADB_OCID", secret=True)
    print_setting(env, "PLATFORM_ORACLE_ADB_REGION")

    driver_mode = normalized(env, "PLATFORM_ORACLE_DRIVER_MODE", "thin")
    connection_security = normalized(env, "PLATFORM_ORACLE_CONNECTION_SECURITY", "wallet_mtls")
    if driver_mode != "thin":
        print(f"ERROR: この検証は Thin 専用です。PLATFORM_ORACLE_DRIVER_MODE={driver_mode}")
        return 2
    if connection_security != "wallet_mtls":
        print(
            "ERROR: この検証は wallet_mtls 専用です。"
            f"PLATFORM_ORACLE_CONNECTION_SECURITY={connection_security}"
        )
        return 2

    wallet_dir = Path(value(env, "PLATFORM_ORACLE_WALLET_DIR")).expanduser()
    dsn = value(env, "PLATFORM_ORACLE_DSN")
    user = value(env, "PLATFORM_ORACLE_USER")
    password = value(env, "PLATFORM_ORACLE_PASSWORD")
    wallet_password = value(env, "PLATFORM_ORACLE_WALLET_PASSWORD") or password
    timeout = max(args.timeout, 1.0)

    print_header("Wallet")
    wallet_ok = validate_wallet(wallet_dir)
    print_wallet_password_status(wallet_dir, wallet_password)
    alias_descriptor = tns_alias_descriptor(wallet_dir, dsn)
    if alias_descriptor:
        print(f"tns_alias={dsn}")
        print("tns_alias_in_tnsnames=present")
    else:
        print(f"tns_alias={dsn or EMPTY}")
        print("tns_alias_in_tnsnames=missing")

    descriptor = alias_descriptor or dsn
    endpoints = extract_tns_endpoints(descriptor)
    if endpoints:
        for endpoint in endpoints:
            print(f"tns_endpoint={endpoint['protocol']}://{endpoint['host']}:{endpoint['port']}")
    else:
        print("tns_endpoint=<not-detected>")

    if not wallet_ok or not user or not password or not dsn or not alias_descriptor:
        print("ERROR: Wallet / user / password / DSN alias の前提が不足しています。")
        return 2

    print_header("Network")
    print_local_route_ip(endpoints)
    if not args.skip_public_ip:
        print_public_ip(timeout)
    for endpoint in endpoints:
        test_socket_endpoint(endpoint["host"], int(endpoint["port"]), timeout)

    print_header("python-oracledb")
    try:
        import oracledb
    except ModuleNotFoundError:
        print("ERROR: python-oracledb がインストールされていません。")
        return 2

    print(f"oracledb_version={getattr(oracledb, '__version__', '<unknown>')}")
    if getattr(oracledb, "is_thin_mode", lambda: True)():
        print("oracledb_mode=thin")
    else:
        print("oracledb_mode=thick_already_initialized")
        print("ERROR: この Python process はすでに Thick mode で初期化されています。")
        return 2

    alias_kwargs = {
        "user": user,
        "password": password,
        "dsn": dsn,
        "config_dir": str(wallet_dir),
        "wallet_location": str(wallet_dir),
        "tcp_connect_timeout": timeout,
    }
    if wallet_password:
        alias_kwargs["wallet_password"] = wallet_password

    alias_ok = test_oracledb_connect("alias", oracledb, alias_kwargs)
    if alias_ok:
        return 0

    descriptor_kwargs = dict(alias_kwargs)
    descriptor_kwargs["dsn"] = strip_tns_retry_settings(alias_descriptor)
    descriptor_ok = test_oracledb_connect("descriptor_without_retry", oracledb, descriptor_kwargs)
    return 0 if descriptor_ok else 1


def load_env(env_file: Path) -> dict[str, str]:
    values = {key: str(val) for key, val in dotenv_values(env_file).items() if val is not None}
    for key, val in os.environ.items():
        values.setdefault(key, val)
    return values


def refresh_wallet_from_oci() -> bool:
    try:
        from pr_system_settings import database as shared_database
        from pr_system_settings.oci_database import OciDatabaseClient

        from app.clients.oracle import close_oracle_pool
        from app.settings import PLATFORM_ENV_FILE, get_settings
    except Exception as exc:
        print(f"refresh_wallet_error=import_failed ({type(exc).__name__}: {exc})")
        return False

    async def run() -> bool:
        settings = get_settings()
        adb_ocid = settings.oracle_adb_ocid.strip()
        if not adb_ocid:
            print("refresh_wallet_error=missing_oracle_adb_ocid")
            return False
        try:
            password = shared_database.wallet_download_password(settings)
        except Exception as exc:
            print(f"refresh_wallet_error={type(exc).__name__}: {exc}")
            return False
        client = OciDatabaseClient(settings=settings)
        try:
            info = await client.get_autonomous_database(adb_ocid)
            generate_type = None if info.is_dedicated is True else "SINGLE"
            wallet_zip = await client.download_autonomous_database_wallet(
                adb_ocid,
                password,
                generate_type,
                shared_database.ORACLE_WALLET_MAX_BYTES,
            )
            settings_data = shared_database.install_downloaded_database_wallet(
                settings,
                wallet_zip,
                password,
                env_file=PLATFORM_ENV_FILE,
                on_saved=lambda _settings: close_oracle_pool(),
            )
        except Exception as exc:
            print(f"refresh_wallet_error={type(exc).__name__}: {exc}")
            return False
        print("refresh_wallet_status=downloaded")
        print(f"refresh_wallet_dir={settings_data.wallet_dir}")
        print("refresh_wallet_password_saved=yes")
        return True

    return asyncio.run(run())


def print_header(title: str) -> None:
    print()
    print(f"== {title} ==")


def value(env: dict[str, str], key: str, default: str = "") -> str:
    return env.get(key, default).strip()


def normalized(env: dict[str, str], key: str, default: str) -> str:
    return value(env, key, default).lower()


def print_setting(env: dict[str, str], key: str, *, secret: bool = False) -> None:
    raw = value(env, key)
    shown = (MASKED if raw else EMPTY) if secret else (raw if raw else EMPTY)
    print(f"{key}={shown}")


def validate_wallet(wallet_dir: Path) -> bool:
    print(f"wallet_dir={wallet_dir}")
    if not wallet_dir.is_dir():
        print("wallet_dir_exists=no")
        return False
    print("wallet_dir_exists=yes")
    ok = True
    for name in WALLET_REQUIRED_FILES:
        exists = (wallet_dir / name).is_file()
        ok = ok and exists
        print(f"{name}={'present' if exists else 'missing'}")
    for optional in ("sqlnet.ora", "cwallet.sso", "ewallet.p12"):
        exists = (wallet_dir / optional).is_file()
        print(f"{optional}={'present' if exists else 'missing'}")
    return ok


def print_wallet_password_status(wallet_dir: Path, wallet_password: str) -> None:
    pem_path = wallet_dir / "ewallet.pem"
    if not pem_path.is_file():
        print("ewallet_pem_password_status=missing_pem")
        return
    try:
        pem = pem_path.read_bytes()
    except OSError as exc:
        print(f"ewallet_pem_password_status=read_error ({exc})")
        return
    encrypted = (
        b"BEGIN ENCRYPTED PRIVATE KEY" in pem[:4096] or b"PROC-TYPE: 4,ENCRYPTED" in pem[:4096]
    )
    print(f"ewallet_pem_encrypted={'yes' if encrypted else 'no'}")
    if not encrypted:
        print("ewallet_pem_password_status=not_required")
        return
    if not wallet_password:
        print("ewallet_pem_password_status=missing_password")
        return
    try:
        load_pem_private_key(pem, password=wallet_password.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        print(f"ewallet_pem_password_status=invalid ({type(exc).__name__}: {exc})")
        return
    print("ewallet_pem_password_status=ok")


def tns_alias_descriptor(wallet_dir: Path, alias: str) -> str | None:
    tnsnames = wallet_dir / "tnsnames.ora"
    if not alias or not tnsnames.is_file():
        return None
    content = tnsnames.read_text(encoding="utf-8", errors="replace")
    for match in re.finditer(r"(?im)^\s*([A-Za-z0-9_.-]+)\s*=\s*", content):
        if match.group(1).lower() != alias.lower():
            continue
        descriptor_start = content.find("(", match.end())
        if descriptor_start < 0:
            return None
        return balanced_parenthesized_text(content, descriptor_start)
    return None


def balanced_parenthesized_text(content: str, start: int) -> str | None:
    depth = 0
    for index in range(start, len(content)):
        char = content[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return content[start : index + 1]
        if depth < 0:
            return None
    return None


def strip_tns_retry_settings(descriptor: str) -> str:
    without_retry_count = re.sub(r"\(\s*retry_count\s*=\s*\d+\s*\)", "", descriptor, flags=re.I)
    return re.sub(r"\(\s*retry_delay\s*=\s*\d+\s*\)", "", without_retry_count, flags=re.I)


def extract_tns_endpoints(descriptor: str) -> list[dict[str, str]]:
    endpoints: list[dict[str, str]] = []
    for address in re.finditer(r"\(\s*address\s*=\s*(\(.+?\))\s*\)", descriptor, re.I | re.S):
        text = address.group(1)
        host = search_tns_value(text, "host")
        port = search_tns_value(text, "port")
        protocol = search_tns_value(text, "protocol") or "tcp"
        if host and port:
            endpoints.append({"protocol": protocol.lower(), "host": host, "port": port})
    if endpoints:
        return endpoints
    host = search_tns_value(descriptor, "host")
    port = search_tns_value(descriptor, "port")
    protocol = search_tns_value(descriptor, "protocol") or "tcp"
    return [{"protocol": protocol.lower(), "host": host, "port": port}] if host and port else []


def search_tns_value(text: str, key: str) -> str | None:
    match = re.search(rf"\(\s*{re.escape(key)}\s*=\s*([^)]+?)\s*\)", text, re.I)
    return match.group(1).strip() if match else None


def print_local_route_ip(endpoints: Iterable[dict[str, str]]) -> None:
    first = next(iter(endpoints), None)
    if not first:
        print("local_route_ip=<not-detected>")
        return
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as exc:
        print(f"local_route_ip_error={exc}")
        return
    try:
        sock.connect((first["host"], int(first["port"])))
        print(f"local_route_ip={sock.getsockname()[0]}")
    except OSError as exc:
        print(f"local_route_ip_error={exc}")
    finally:
        sock.close()


def print_public_ip(timeout: float) -> None:
    for endpoint in PUBLIC_IP_ENDPOINTS:
        try:
            request = urllib.request.Request(endpoint, headers={"User-Agent": "nl2sql-adb-check/1"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(128).decode("utf-8", errors="replace").strip()
            if body:
                print(f"public_egress_ip={body}")
                print(f"public_egress_ip_source={endpoint}")
                return
        except (OSError, urllib.error.URLError) as exc:
            print(f"public_egress_ip_source_failed={endpoint} ({exc})")
    print("public_egress_ip=<not-detected>")


def test_socket_endpoint(host: str, port: int, timeout: float) -> None:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = round((time.monotonic() - started) * 1000)
            print(f"socket_connect={host}:{port} ok elapsed_ms={elapsed_ms}")
    except OSError as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        print(f"socket_connect={host}:{port} failed elapsed_ms={elapsed_ms} error={exc}")


def test_oracledb_connect(label: str, oracledb: Any, kwargs: dict[str, Any]) -> bool:
    print(f"connect_case={label}")
    print_connect_kwargs(kwargs)
    started = time.monotonic()
    try:
        with oracledb.connect(**kwargs) as connection, connection.cursor() as cursor:
            cursor.execute("select 1 from dual")
            row = cursor.fetchone()
        elapsed_ms = round((time.monotonic() - started) * 1000)
        print(f"connect_result={label} ok elapsed_ms={elapsed_ms} row={row}")
        return True
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        print(f"connect_result={label} failed elapsed_ms={elapsed_ms}")
        print(f"connect_error_type={type(exc).__name__}")
        print(f"connect_error={exc}")
        print_diagnosis(str(exc))
        return False


def print_connect_kwargs(kwargs: dict[str, Any]) -> None:
    safe = {}
    for key, val in kwargs.items():
        if key in {"password", "wallet_password"}:
            safe[key] = MASKED if val else EMPTY
        elif key == "dsn" and isinstance(val, str) and len(val) > 160:
            safe[key] = f"{val[:157]}..."
        else:
            safe[key] = val
    print(f"connect_kwargs={safe}")


def print_diagnosis(error_text: str) -> None:
    upper = error_text.upper()
    if "ORA-12506" in upper:
        print(
            "diagnosis=listener_rejected_by_acl_or_service_acl; "
            "ADB ACL に backend 実行元の public egress IP、または Service Gateway 経由の VCN/CIDR "
            "が許可されているか確認してください。"
        )
    elif "DPY-6005" in upper or "DPY-6000" in upper:
        print(
            "diagnosis=network_connect_failed; host/port 到達性、ACL、private endpoint 経路、"
            "DNS を確認してください。"
        )
    elif "DPY-4011" in upper or "DPI-1032" in upper:
        print("diagnosis=wallet_password_or_wallet_file_problem")
    elif "ORA-01017" in upper:
        print("diagnosis=db_username_or_password_invalid")
    else:
        print("diagnosis=unclassified")


if __name__ == "__main__":
    sys.exit(main())
