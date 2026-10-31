"""MinerU のライセンスの条件の確認（#3359）のテスト。

system_admin が「文書解析」で条件を確認するまで、MinerU を有効にも選択にもできず（設定・レシピの
保存は 422）、取込でも MinerU を呼ばない。確認・取り消しは system_admin だけ（ほかは 403）。確認した
人・日時は parser-settings.json に保存し、環境変数・`.env` では書けない。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from pytest import MonkeyPatch

from app.api.routes import settings as settings_routes
from app.clients.external_parser import ExternalParserCallError, ExternalParserClient
from app.clients.parser_service import ParserServiceClient, ParserServiceUnavailableError
from app.config import PARSER_SETTINGS_STORE, Settings, get_settings
from app.main import app
from app.rag.mineru_license import (
    MINERU_LICENSE_ADMIN_ONLY_MESSAGE,
    MINERU_LICENSE_REQUIRED_MESSAGE,
    mineru_license_acknowledged,
    mineru_license_acknowledgement,
    set_mineru_license_acknowledgement,
)
from app.schemas.document import SourceModality, SourceProfile
from tests.support import AsgiTestClient

# MinerU は準備中(ロードマップ。#3359)。ライセンスの確認の関門の経路は判定を外して確かめる。
pytestmark = pytest.mark.usefixtures("mineru_selectable")

client = AsgiTestClient(app)
ACKNOWLEDGED_AT = "2026-10-26T00:00:00+00:00"


def _as_actor(monkeypatch: MonkeyPatch, name: str, *, system_admin: bool) -> None:
    monkeypatch.setattr(
        settings_routes,
        "_mineru_license_actor",
        lambda _request: settings_routes._MineruLicenseActor(
            display_name=name, is_system_admin=system_admin
        ),
    )


def _persisted_parser(settings: Settings) -> dict[str, Any]:
    persisted: dict[str, Any] = json.loads(
        PARSER_SETTINGS_STORE.path(settings).read_text(encoding="utf-8")
    )
    return persisted


def _profile() -> SourceProfile:
    return SourceProfile(
        original_file_name="a.pdf",
        sanitized_file_name="a.pdf",
        content_type="application/pdf",
        file_size_bytes=4,
        content_sha256="0" * 64,
        modality=SourceModality.PDF,
        parser_profile="pdf",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"adapter_backend": "mineru", "mineru_enabled": True},
        {"adapter_backend": "docling", "mineru_enabled": True},
        {"adapter_backend": "mineru"},
    ],
    ids=["select-and-enable", "enable-only", "select-only"],
)
def test_enabling_mineru_without_license_acknowledgement_is_rejected(
    payload: dict[str, object],
) -> None:
    settings = get_settings()
    before_backend = settings.rag_parser_adapter_backend
    before_vision = settings.rag_vision_enabled

    response = client.patch(
        "/api/settings/parser-adapters", json={**payload, "vision_enabled": not before_vision}
    )

    assert response.status_code == 422
    assert MINERU_LICENSE_REQUIRED_MESSAGE in response.text, response.text
    assert settings.rag_parser_adapter_backend == before_backend
    assert settings.rag_parser_mineru_enabled is False
    # 拒否した保存は、同じ要求の「解析後の処理」も書かない。
    assert settings.rag_vision_enabled is before_vision
    assert not PARSER_SETTINGS_STORE.path(settings).exists()


def test_enabling_mineru_with_license_acknowledgement_records_who_and_when(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    _as_actor(monkeypatch, "管理者 A", system_admin=True)

    response = client.patch(
        "/api/settings/parser-adapters",
        json={
            "adapter_backend": "mineru",
            "mineru_enabled": True,
            "mineru_license_acknowledged": True,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["adapter_backend"] == "mineru"
    license_data = data["mineru_license"]
    assert license_data["acknowledged"] is True
    assert license_data["acknowledged_by"] == "管理者 A"
    assert datetime.fromisoformat(license_data["acknowledged_at"]).tzinfo is not None
    persisted = _persisted_parser(settings)
    assert persisted["mineru_license_acknowledged_by"] == "管理者 A"
    assert persisted["mineru_license_acknowledged_at"] == license_data["acknowledged_at"]

    # 確認済みのまま保存し直しても、最初の確認の記録を保つ。
    _as_actor(monkeypatch, "管理者 B", system_admin=True)
    again = client.patch(
        "/api/settings/parser-adapters",
        json={"adapter_backend": "mineru", "mineru_license_acknowledged": True},
    )
    assert again.status_code == 200
    assert again.json()["data"]["mineru_license"] == license_data


def test_get_reports_unacknowledged_mineru_license_by_default() -> None:
    response = client.get("/api/settings/parser-adapters")

    assert response.status_code == 200
    assert response.json()["data"]["mineru_license"] == {
        "acknowledged": False,
        "acknowledged_by": None,
        "acknowledged_at": None,
    }


def test_withdrawing_acknowledgement_while_mineru_is_used_is_rejected(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_parser_adapter_backend", "mineru")
    monkeypatch.setattr(settings, "rag_parser_mineru_enabled", True)
    set_mineru_license_acknowledgement(settings, by="管理者 A", at=ACKNOWLEDGED_AT)

    response = client.patch(
        "/api/settings/parser-adapters",
        json={"adapter_backend": "mineru", "mineru_license_acknowledged": False},
    )

    assert response.status_code == 422
    assert mineru_license_acknowledgement(settings) == ("管理者 A", ACKNOWLEDGED_AT)


def test_withdrawing_acknowledgement_after_switching_away_from_mineru(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_parser_adapter_backend", "mineru")
    monkeypatch.setattr(settings, "rag_parser_mineru_enabled", True)
    set_mineru_license_acknowledgement(settings, by="管理者 A", at=ACKNOWLEDGED_AT)

    response = client.patch(
        "/api/settings/parser-adapters",
        json={
            "adapter_backend": "docling",
            "docling_enabled": True,
            "mineru_enabled": False,
            "mineru_license_acknowledged": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["mineru_license"]["acknowledged"] is False
    persisted = _persisted_parser(settings)
    assert persisted["mineru_license_acknowledged_at"] == ""
    assert persisted["mineru_license_acknowledged_by"] == ""


def test_unrelated_save_is_not_blocked_when_mineru_was_enabled_before(
    monkeypatch: MonkeyPatch,
) -> None:
    """`.env` で前から MinerU が有効でも、ほかの項目の保存は止めない（取込で止める）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_parser_mineru_enabled", True)

    response = client.patch(
        "/api/settings/parser-adapters",
        json={"adapter_backend": "docling", "docling_enabled": True, "mineru_enabled": True},
    )

    assert response.status_code == 200


def test_recipe_selecting_mineru_without_acknowledgement_is_rejected() -> None:
    response = client.put(
        "/api/documents/doc-1/recipes/recipe-1",
        json={"parser_adapter_backend": "mineru"},
    )

    assert response.status_code == 422
    assert MINERU_LICENSE_REQUIRED_MESSAGE in response.text, response.text


def test_recipe_gate_allows_mineru_after_acknowledgement(monkeypatch: MonkeyPatch) -> None:
    from app.api.routes.documents import _raise_if_recipe_selects_mineru_without_license
    from app.schemas.document import DocumentProcessingConfig

    settings = get_settings()
    config = DocumentProcessingConfig(parser_adapter_backend="mineru")
    set_mineru_license_acknowledgement(settings, by="管理者 A", at=ACKNOWLEDGED_AT)

    _raise_if_recipe_selects_mineru_without_license(config)
    # MinerU を選ばないレシピは確認に関係なく保存できる。
    set_mineru_license_acknowledgement(settings, by="", at="")
    _raise_if_recipe_selects_mineru_without_license(
        DocumentProcessingConfig(parser_adapter_backend="docling")
    )


def test_external_parser_does_not_call_mineru_without_acknowledgement() -> None:
    settings = Settings(rag_parser_mineru_api_host="https://mineru.example.com")

    with pytest.raises(ExternalParserCallError) as exc_info:
        ExternalParserClient(settings).parse("mineru", b"%PDF", _profile(), "application/pdf")

    assert exc_info.value.reason == "license_unacknowledged"
    assert exc_info.value.warning_code == "mineru_license_unacknowledged"


def test_ingestion_stops_with_license_message_without_acknowledgement() -> None:
    client_ = ParserServiceClient(Settings(rag_parser_mineru_api_host="https://mineru.example.com"))

    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        client_.runner("mineru", b"%PDF", _profile(), "application/pdf", fail_fast=True)

    assert exc_info.value.reason == "license_unacknowledged"
    assert str(exc_info.value).startswith(MINERU_LICENSE_REQUIRED_MESSAGE)


def test_acknowledgement_cannot_be_set_from_environment(
    monkeypatch: MonkeyPatch, tmp_path: Any
) -> None:
    """確認は画面の保存だけで書く。同じ名前の環境変数・`.env` は効かない（利用者の判断）。"""
    for name in (
        "RAG_PARSER_MINERU_LICENSE_ACKNOWLEDGED_AT",
        "RAG_PARSER_MINERU_LICENSE_ACKNOWLEDGED_BY",
        "RAG_PARSER_MINERU_LICENSE_ACKNOWLEDGED",
    ):
        monkeypatch.setenv(name, ACKNOWLEDGED_AT)
    settings = Settings(rag_parser_mineru_api_host="https://mineru.example.com")

    assert mineru_license_acknowledged(settings) is False
    assert "rag_parser_mineru_license_acknowledged_at" not in Settings.model_fields
    assert "rag_parser_mineru_license_acknowledged_by" not in Settings.model_fields
    with pytest.raises(ExternalParserCallError):
        ExternalParserClient(settings).parse("mineru", b"%PDF", _profile(), "application/pdf")
    response = client.get("/api/settings/parser-adapters")
    assert response.json()["data"]["mineru_license"]["acknowledged"] is False


def test_acknowledgement_is_read_back_from_parser_settings_file(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    _as_actor(monkeypatch, "管理者 A", system_admin=True)
    saved = client.patch(
        "/api/settings/parser-adapters",
        json={"adapter_backend": "docling", "mineru_license_acknowledged": True},
    )
    assert saved.status_code == 200

    set_mineru_license_acknowledgement(settings, by="", at="")
    PARSER_SETTINGS_STORE.load(settings)

    by, at = mineru_license_acknowledgement(settings)
    assert by == "管理者 A"
    assert at == saved.json()["data"]["mineru_license"]["acknowledged_at"]


@pytest.mark.parametrize("acknowledged_before", [False, True], ids=["set", "withdraw"])
def test_non_system_admin_cannot_change_acknowledgement(
    monkeypatch: MonkeyPatch, acknowledged_before: bool
) -> None:
    settings = get_settings()
    if acknowledged_before:
        set_mineru_license_acknowledgement(settings, by="管理者 A", at=ACKNOWLEDGED_AT)
    _as_actor(monkeypatch, "利用者 B", system_admin=False)

    response = client.patch(
        "/api/settings/parser-adapters",
        json={
            "adapter_backend": "docling",
            "mineru_enabled": False,
            "mineru_license_acknowledged": not acknowledged_before,
        },
    )

    assert response.status_code == 403
    assert MINERU_LICENSE_ADMIN_ONLY_MESSAGE in response.text
    assert mineru_license_acknowledged(settings) is acknowledged_before
    assert not PARSER_SETTINGS_STORE.path(settings).exists()


def test_non_system_admin_can_use_mineru_after_acknowledgement(
    monkeypatch: MonkeyPatch,
) -> None:
    """確認の後は、文書解析の設定の権限を持つほかの利用者も MinerU を選べる。"""
    settings = get_settings()
    set_mineru_license_acknowledgement(settings, by="管理者 A", at=ACKNOWLEDGED_AT)
    _as_actor(monkeypatch, "利用者 B", system_admin=False)

    response = client.patch(
        "/api/settings/parser-adapters",
        json={
            "adapter_backend": "mineru",
            "mineru_enabled": True,
            "mineru_license_acknowledged": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["mineru_license"]["acknowledged_by"] == "管理者 A"


def test_non_system_admin_without_acknowledgement_still_gets_422_for_mineru(
    monkeypatch: MonkeyPatch,
) -> None:
    _as_actor(monkeypatch, "利用者 B", system_admin=False)

    response = client.patch(
        "/api/settings/parser-adapters",
        json={"adapter_backend": "mineru", "mineru_license_acknowledged": False},
    )

    assert response.status_code == 422
