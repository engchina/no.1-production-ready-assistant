"""OCI Autonomous Database 管理クライアント（NL2SQL から移設。#108）。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .oci import oci_config_file, oci_profile
from .oci_auth import load_oci_config_without_prompt

type SdkCallRunner = Callable[[Callable[[], Any]], Awaitable[Any]]
type WalletDetailsFactory = Callable[..., Any]


class WalletDownloadTooLargeError(RuntimeError):
    """OCI Wallet のレスポンスが許可サイズを超えた。"""


class WalletDownloadResponseError(RuntimeError):
    """OCI Wallet のレスポンス本文を安全に読み取れなかった。"""


@dataclass(frozen=True)
class AutonomousDatabaseInfo:
    """ADB 情報の表示用スナップショット。"""

    id: str | None
    display_name: str | None
    lifecycle_state: str | None
    db_name: str | None
    cpu_core_count: int | None
    data_storage_size_in_tbs: float | None
    is_dedicated: bool | None = None


class DatabaseSdkClientProtocol(Protocol):
    """OCI Database control plane client の最小インターフェース。"""

    def get_autonomous_database(self, autonomous_database_id: str) -> Any:
        """ADB 情報を取得する。"""

    def start_autonomous_database(self, autonomous_database_id: str) -> Any:
        """ADB を起動する。"""

    def stop_autonomous_database(self, autonomous_database_id: str) -> Any:
        """ADB を停止する。"""

    def generate_autonomous_database_wallet(
        self,
        autonomous_database_id: str,
        generate_autonomous_database_wallet_details: Any,
    ) -> Any:
        """ADB Wallet ZIP を生成する。"""


class OciDatabaseClient:
    """OCI Autonomous Database の制御プレーン操作クライアント。"""

    def __init__(
        self,
        settings: Any,
        database_client: DatabaseSdkClientProtocol | None = None,
        sdk_call_runner: SdkCallRunner | None = None,
        wallet_details_factory: WalletDetailsFactory | None = None,
    ) -> None:
        self._settings = settings
        self._database_client = database_client
        self._sdk_call_runner = sdk_call_runner or _run_sdk_call_in_thread
        self._wallet_details_factory = wallet_details_factory

    async def get_autonomous_database(self, adb_ocid: str) -> AutonomousDatabaseInfo:
        """ADB の現在情報を取得する。"""
        response = await self._sdk_call_runner(
            lambda: self._client().get_autonomous_database(adb_ocid)
        )
        return _to_info(getattr(response, "data", response))

    async def start_autonomous_database(self, adb_ocid: str) -> None:
        """ADB の起動をリクエストする。"""
        await self._sdk_call_runner(lambda: self._client().start_autonomous_database(adb_ocid))

    async def stop_autonomous_database(self, adb_ocid: str) -> None:
        """ADB の停止をリクエストする。"""
        await self._sdk_call_runner(lambda: self._client().stop_autonomous_database(adb_ocid))

    async def download_autonomous_database_wallet(
        self,
        adb_ocid: str,
        password: str,
        generate_type: str | None,
        max_bytes: int,
    ) -> bytes:
        """ADB Wallet ZIP を上限付きストリーミングで取得する。"""
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        details_factory = self._wallet_details_factory or _wallet_details_model
        details_kwargs: dict[str, str] = {"password": password}
        if generate_type is not None:
            details_kwargs["generate_type"] = generate_type
        details = details_factory(**details_kwargs)
        result = await self._sdk_call_runner(
            lambda: _download_wallet_bytes(
                self._client(),
                adb_ocid,
                details,
                max_bytes=max_bytes,
            )
        )
        if not isinstance(result, bytes):
            raise WalletDownloadResponseError("wallet response was not bytes")
        return result

    def _client(self) -> DatabaseSdkClientProtocol:
        """OCI Database client を遅延初期化する。"""
        if self._database_client is not None:
            return self._database_client

        oci_config = importlib.import_module("oci.config")
        database = importlib.import_module("oci.database")
        region = str(getattr(self._settings, "resolved_oracle_adb_region", "") or "")
        config = load_oci_config_without_prompt(
            oci_config,
            oci_config_file(self._settings),
            oci_profile(self._settings),
            region=region or None,
        )
        client: DatabaseSdkClientProtocol = database.DatabaseClient(config)
        self._database_client = client
        return client


def _to_info(data: Any) -> AutonomousDatabaseInfo:
    """OCI SDK の AutonomousDatabase model を表示用スナップショットへ変換する。"""
    return AutonomousDatabaseInfo(
        id=getattr(data, "id", None),
        display_name=getattr(data, "display_name", None),
        lifecycle_state=getattr(data, "lifecycle_state", None),
        db_name=getattr(data, "db_name", None),
        cpu_core_count=getattr(data, "cpu_core_count", None),
        data_storage_size_in_tbs=getattr(data, "data_storage_size_in_tbs", None),
        is_dedicated=getattr(data, "is_dedicated", None),
    )


def _wallet_details_model(**kwargs: str) -> Any:
    """OCI SDK model を遅延 import して Wallet 生成 details を作る。"""
    models = importlib.import_module("oci.database.models")
    return models.GenerateAutonomousDatabaseWalletDetails(**kwargs)


def _download_wallet_bytes(
    client: DatabaseSdkClientProtocol,
    adb_ocid: str,
    details: Any,
    *,
    max_bytes: int,
) -> bytes:
    """同期 OCI SDK レスポンスを上限内で読み切る。"""
    response = client.generate_autonomous_database_wallet(adb_ocid, details)
    data = getattr(response, "data", response)
    raw = getattr(data, "raw", None)
    chunks: list[bytes] = []
    total = 0
    try:
        if raw is not None and callable(getattr(raw, "stream", None)):
            stream = raw.stream(1024 * 1024, decode_content=True)
        else:
            content = (
                data if isinstance(data, (bytes, bytearray)) else getattr(data, "content", None)
            )
            if not isinstance(content, (bytes, bytearray)):
                raise WalletDownloadResponseError("wallet response body is unavailable")
            stream = (bytes(content),)

        for chunk in stream:
            if not isinstance(chunk, (bytes, bytearray)):
                raise WalletDownloadResponseError("wallet response contained a non-bytes chunk")
            total += len(chunk)
            if total > max_bytes:
                raise WalletDownloadTooLargeError("wallet response exceeded max_bytes")
            chunks.append(bytes(chunk))
    finally:
        close = getattr(raw, "close", None)
        if callable(close):
            close()
    return b"".join(chunks)


async def _run_sdk_call_in_thread(operation: Callable[[], Any]) -> Any:
    """同期 OCI SDK 呼び出しを event loop 外で実行する。"""
    return await asyncio.to_thread(operation)
