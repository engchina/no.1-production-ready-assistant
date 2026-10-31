"""OCI 認証設定の段階的な接続テスト。OCI SDK の通信部分は mock し、実 OCI には接続しない。"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import BaseModel
from pytest import MonkeyPatch

from pr_system_settings import oci as settings_router
from pr_system_settings import oci_connectivity
from pr_system_settings.oci import OciConfigTestResult


class FakeSettings(BaseModel):
    """製品の Settings のうち OCI 接続テストが読む属性だけを持つ（NL2SQL から移設。#100）。"""

    oci_config_file: str = "~/.oci/config"
    oci_config_profile: str = "DEFAULT"
    oci_region: str = ""


_SETTINGS = FakeSettings()


def get_settings() -> FakeSettings:
    return _SETTINGS


oci: Any = importlib.import_module("oci")

USER_OCID = "ocid1.user.oc1..aaaaaaaauserexamplesecretsuffix"
TENANCY_OCID = "ocid1.tenancy.oc1..aaaaaaaatenancyexamplesecretsuffix"
REGION = "ap-osaka-1"


def _rsa_pem_and_fingerprint() -> tuple[bytes, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.md5(der, usedforsecurity=False).hexdigest()
    return pem, ":".join(digest[index : index + 2] for index in range(0, 32, 2))


def _write_oci_files(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    *,
    fingerprint: str | None = None,
    key_bytes: bytes | None = None,
) -> str:
    """tmp 配下に 0700 / 0600 の OCI config と鍵を作り、Settings をそこへ向ける。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    oci_dir = tmp_path / "oci"
    oci_dir.mkdir()
    pem, actual_fingerprint = _rsa_pem_and_fingerprint()
    key_file = oci_dir / "oci_api_key.pem"
    key_file.write_bytes(key_bytes if key_bytes is not None else pem)
    config_file = oci_dir / "config"
    selected_fingerprint = fingerprint if fingerprint is not None else actual_fingerprint
    config_file.write_text(
        "[DEFAULT]\n"
        f"user={USER_OCID}\n"
        f"fingerprint={selected_fingerprint}\n"
        f"tenancy={TENANCY_OCID}\n"
        f"region={REGION}\n"
        f"key_file={key_file}\n",
        encoding="utf-8",
    )
    oci_dir.chmod(0o700)
    config_file.chmod(0o600)
    key_file.chmod(0o600)
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(config_file))
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    return selected_fingerprint


def _mock_namespace_call(
    monkeypatch: MonkeyPatch,
    behavior: Callable[[dict[str, Any]], object],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(config: dict[str, Any]) -> object:
        calls.append(dict(config))
        return behavior(config)

    monkeypatch.setattr(oci_connectivity, "_get_object_storage_namespace", fake)
    return calls


def _stages(result: OciConfigTestResult) -> dict[str, str]:
    return {stage.key: stage.status for stage in result.stages}


def _assert_no_secrets(result: OciConfigTestResult, fingerprint: str) -> None:
    text = result.model_dump_json()
    assert fingerprint not in text
    assert USER_OCID not in text
    assert TENANCY_OCID not in text
    assert "PRIVATE KEY" not in text


def test_missing_config_fails_first_stage_and_skips_the_rest(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(tmp_path / "missing-config"))
    calls = _mock_namespace_call(monkeypatch, lambda _config: "ns")

    result = settings_router._test_oci_config(settings)

    assert result.status == "failed"
    assert result.error_type == "HTTPException"
    assert _stages(result) == {
        "config_format": "failed",
        "key_file": "skipped",
        "region": "skipped",
        "authentication": "skipped",
    }
    assert [stage.key for stage in result.stages] == [
        "config_format",
        "key_file",
        "region",
        "authentication",
    ]
    assert calls == []


def test_malformed_fingerprint_fails_format_without_calling_oci(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    fingerprint = _write_oci_files(tmp_path, monkeypatch, fingerprint="aa:bb:cc:dd:ee:ff:00:11")
    calls = _mock_namespace_call(monkeypatch, lambda _config: "ns")

    result = settings_router._test_oci_config(get_settings())

    assert result.status == "failed"
    assert _stages(result)["config_format"] == "failed"
    assert _stages(result)["key_file"] == "skipped"
    assert "fingerprint" in result.message
    format_stage = result.stages[0]
    assert format_stage.action and "16 バイト" in format_stage.action
    assert calls == []
    _assert_no_secrets(result, fingerprint)


def test_fingerprint_not_matching_key_fails_key_stage(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    fingerprint = _write_oci_files(tmp_path, monkeypatch, fingerprint=":".join(["ab"] * 16))
    calls = _mock_namespace_call(monkeypatch, lambda _config: "ns")

    result = settings_router._test_oci_config(get_settings())

    assert _stages(result) == {
        "config_format": "success",
        "key_file": "failed",
        "region": "skipped",
        "authentication": "skipped",
    }
    assert "一致しません" in result.message
    assert calls == []
    _assert_no_secrets(result, fingerprint)


@pytest.mark.parametrize(
    "key_bytes",
    [
        b"-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    ],
    ids=["broken-pem", "non-rsa"],
)
def test_unusable_private_key_fails_key_stage(
    monkeypatch: MonkeyPatch, tmp_path: Path, key_bytes: bytes
) -> None:
    _write_oci_files(tmp_path, monkeypatch, fingerprint=":".join(["ab"] * 16), key_bytes=key_bytes)
    calls = _mock_namespace_call(monkeypatch, lambda _config: "ns")

    result = settings_router._test_oci_config(get_settings())

    assert _stages(result)["key_file"] == "failed"
    assert _stages(result)["authentication"] == "skipped"
    assert calls == []


def test_group_readable_key_fails_key_stage(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _write_oci_files(tmp_path, monkeypatch)
    (tmp_path / "oci" / "oci_api_key.pem").chmod(0o644)
    calls = _mock_namespace_call(monkeypatch, lambda _config: "ns")

    result = settings_router._test_oci_config(get_settings())

    assert _stages(result)["key_file"] == "failed"
    assert result.permission_issues == ["秘密鍵ファイルは 0600 にしてください。"]
    assert calls == []


def test_successful_authenticated_call_marks_all_stages_success(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    fingerprint = _write_oci_files(tmp_path, monkeypatch)
    calls = _mock_namespace_call(monkeypatch, lambda _config: "exampletenancy")

    result = settings_router._test_oci_config(get_settings())

    assert result.status == "success"
    assert set(_stages(result).values()) == {"success"}
    assert result.region == REGION
    assert result.auth_check_operation == "Object Storage GetNamespace"
    assert result.http_status is None
    assert len(calls) == 1
    assert calls[0]["region"] == REGION
    _assert_no_secrets(result, fingerprint)


@pytest.mark.parametrize(
    ("status", "code", "expected_action"),
    [
        (401, "NotAuthenticated", "fingerprint"),
        (404, "NotAuthorizedOrNotFound", "IAM ポリシー"),
        (429, "TooManyRequests", "しばらく待って"),
        (503, "ServiceUnavailable", "時間をおいて"),
    ],
)
def test_service_error_reports_status_code_and_next_action(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    status: int,
    code: str,
    expected_action: str,
) -> None:
    fingerprint = _write_oci_files(tmp_path, monkeypatch)

    def raise_service_error(_config: dict[str, Any]) -> object:
        raise oci.exceptions.ServiceError(
            status,
            code,
            {"opc-request-id": "REQ123/ABC"},
            f"service message with {fingerprint}",
        )

    _mock_namespace_call(monkeypatch, raise_service_error)

    result = settings_router._test_oci_config(get_settings())

    assert result.status == "failed"
    assert _stages(result) == {
        "config_format": "success",
        "key_file": "success",
        "region": "success",
        "authentication": "failed",
    }
    assert result.http_status == status
    assert result.service_code == code
    assert result.request_id == "REQ123/ABC"
    assert f"{status} {code}" in result.message
    auth_stage = result.stages[3]
    assert auth_stage.action and expected_action in auth_stage.action
    _assert_no_secrets(result, fingerprint)


def test_connect_timeout_fails_region_and_skips_authentication(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _write_oci_files(tmp_path, monkeypatch)

    def raise_timeout(_config: dict[str, Any]) -> object:
        raise oci.exceptions.ConnectTimeout("connect timeout")

    _mock_namespace_call(monkeypatch, raise_timeout)

    result = settings_router._test_oci_config(get_settings())

    assert _stages(result)["region"] == "failed"
    assert _stages(result)["authentication"] == "skipped"
    assert result.error_type == "ConnectTimeout"
    assert "5 秒" in result.message


def test_read_timeout_reaches_region_but_fails_authentication(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _write_oci_files(tmp_path, monkeypatch)

    def raise_read_timeout(_config: dict[str, Any]) -> object:
        raise oci.exceptions.RequestException(requests.exceptions.ReadTimeout("read timeout"))

    _mock_namespace_call(monkeypatch, raise_read_timeout)

    result = settings_router._test_oci_config(get_settings())

    assert _stages(result)["region"] == "success"
    assert _stages(result)["authentication"] == "failed"
    assert result.error_type == "ReadTimeout"
    assert "10 秒" in result.message


def test_connection_error_fails_region(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _write_oci_files(tmp_path, monkeypatch)

    def raise_connection_error(_config: dict[str, Any]) -> object:
        raise oci.exceptions.RequestException(requests.exceptions.ConnectionError("dns"))

    _mock_namespace_call(monkeypatch, raise_connection_error)

    result = settings_router._test_oci_config(get_settings())

    assert _stages(result)["region"] == "failed"
    assert result.error_type == "ConnectionError"
    assert result.stages[2].action and REGION in result.stages[2].action


def test_namespace_call_uses_short_timeout_without_retry(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
            captured["config"] = config
            captured["kwargs"] = kwargs

        def get_namespace(self, **kwargs: Any) -> object:
            captured["call_kwargs"] = kwargs
            return object()

    monkeypatch.setattr(oci.object_storage, "ObjectStorageClient", FakeClient)

    oci_connectivity._get_object_storage_namespace({"region": REGION})

    assert captured["kwargs"]["timeout"] == (5.0, 10.0)
    assert isinstance(captured["kwargs"]["retry_strategy"], oci.retry.NoneRetryStrategy)
    assert isinstance(
        captured["kwargs"]["circuit_breaker_strategy"],
        oci.circuit_breaker.NoCircuitBreakerStrategy,
    )
    assert isinstance(captured["call_kwargs"]["retry_strategy"], oci.retry.NoneRetryStrategy)
