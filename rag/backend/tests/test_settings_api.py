"""設定 API のテスト。"""

import asyncio
import copy
import hashlib
import json
import stat
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zipfile import ZipFile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import dotenv_values
from pr_backend_core.observability.model_purpose import model_purpose_attributes
from pr_system_settings import oci_connectivity
from pr_system_settings.model import ModelSettingsTestRequest
from pr_system_settings.model_test_input import MODEL_TEST_IMAGE_BYTES
from pytest import MonkeyPatch

from app import config as app_config
from app.api.routes import settings as settings_routes
from app.clients.oracle import (
    OracleClient,
    OracleConnectionTimeoutError,
    OracleWalletPasswordRequiredError,
)
from app.config import (
    MODEL_SETTINGS_STORE,
    PARSER_SETTINGS_STORE,
    SMALL_TO_BIG_SETTING_FIELDS,
    Settings,
    get_settings,
    load_persisted_model_settings,
)
from app.main import app
from app.rag import parser_adapter_readiness
from app.schemas.settings import (
    EnterpriseAiModelSettings,
)
from app.services.control import service_runtime_env
from tests.support import AsgiTestClient

client = AsgiTestClient(app)
LLM_TEMPLATE = '{"input":"${user_message}"}'
VLM_TEMPLATE = '{"input":"${data_base64}"}'


def _saved_env_value(settings: Settings, name: str) -> str | None:
    """保存された secret。モデルの API key は共通 .env、parser の key は backend/.env。"""
    env_file = (
        PARSER_SETTINGS_STORE.env_file(settings)
        if name.startswith("RAG_")
        else MODEL_SETTINGS_STORE.env_file(settings)
    )
    return dotenv_values(env_file).get(name)


def _persisted_parser(settings: Settings) -> dict[str, Any]:
    """「文書解析」で保存した parser-settings.json（共有の model-settings ではない。#1701）。"""
    persisted: dict[str, Any] = json.loads(
        PARSER_SETTINGS_STORE.path(settings).read_text(encoding="utf-8")
    )
    return persisted


def _saved_enterprise_ai_api_key(settings: Settings) -> str | None:
    return _saved_env_value(settings, "PLATFORM_OCI_ENTERPRISE_AI_API_KEY")


def test_parser_adapter_settings_reports_flags_and_package_status(
    monkeypatch: MonkeyPatch,
) -> None:
    """adapter readiness は flag と Python package 有無を非機密に返す。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_navigation_summary_max_nodes", 37)
    # 旧 runtime 値 auto は利用者向けには返さず、明示値 local へ寄せる。
    monkeypatch.setattr(settings, "rag_parser_adapter_backend", "auto")
    monkeypatch.setattr(settings, "rag_parser_docling_enabled", True)
    monkeypatch.setattr(settings, "rag_parser_unstructured_enabled", False)

    def package_info(
        import_name: str,
        _distribution_names: Sequence[str],
    ) -> tuple[bool, str | None, str | None]:
        return (
            import_name == "docling",
            "1.2.3" if import_name == "docling" else None,
            "docling" if import_name == "docling" else None,
        )

    monkeypatch.setattr(parser_adapter_readiness, "_package_info", package_info)

    resp = client.get("/api/settings/parser-adapters")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["navigation_summary_max_nodes"] == 37
    assert body["adapter_backend"] == "local"
    assert body["effective_order"] == []
    by_backend = {adapter["backend"]: adapter for adapter in body["adapters"]}
    assert by_backend["docling"]["status"] == "ignored"
    assert by_backend["docling"]["import_name"] == "docling"
    assert by_backend["docling"]["distribution_name"] == "docling"
    assert by_backend["docling"]["install_package"] == "docling==2.103.0"
    assert by_backend["docling"]["version"] == "1.2.3"
    assert by_backend["docling"]["warning_code"] == "adapter_flag_ignored_by_backend"
    assert by_backend["unstructured"]["install_package"] == "unstructured[all-docs]==0.23.1"
    assert by_backend["unstructured"]["status"] == "disabled"
    # 外部の解析エンジンの接続（connections）は削除した（#22）。
    assert "connections" not in body
    assert "service_backends" not in body
    # 削除したエンジン(#270・#2042・#22・#21)は画面の選択肢・対応形式に出さない。
    assert list(by_backend) == ["docling", "unstructured"]
    assert "source_format_parsers" not in body
    assert body["scorecard"]["selected_backend"] == "local"
    assert body["scorecard"]["recommended_backend"] == "local"
    score_by_backend = {entry["backend"]: entry for entry in body["scorecard"]["entries"]}
    assert score_by_backend["local"]["recommended"] is True
    assert score_by_backend["docling"]["warning_codes"] == ["adapter_flag_ignored_by_backend"]
    assert set(score_by_backend) == {"local", "docling", "unstructured"}
    route_by_kind = {route["source_kind"]: route for route in body["source_routes"]}
    assert route_by_kind["pdf"]["candidate_order"] == ["docling", "unstructured"]
    assert route_by_kind["pdf"]["attempted_order"] == []
    assert route_by_kind["pdf"]["selected_backend"] == "local"
    assert route_by_kind["email"]["selected_backend"] == "local"
    assert "unstructured_adapter_feature_flag_disabled" in route_by_kind["email"]["warning_codes"]
    matrix = body["backend_source_kind_matrix"]
    assert matrix["evidence_source"] == "runtime_routes"
    assert "pdf" in matrix["backend_source_kinds"]["local"]
    assert "email" in matrix["backend_source_kinds"]["local"]
    assert "text" in matrix["backend_source_kinds"]["local"]
    assert matrix["missing_source_kinds"] == []
    assert "raw_text" not in str(matrix)
    capability_by_backend = {item["backend"]: item for item in body["capabilities"]}
    assert capability_by_backend["docling"]["modalities"] == ["pdf", "image"]
    assert ".pdf" in capability_by_backend["docling"]["extensions"]
    for removed in (
        "marker",
        "unlimited_ocr",
        "glm_ocr",
        "oci_genai_vision",
        "enterprise_ai_vlm",
        "mineru",
        "dots_ocr",
        "oci_document_understanding",
        "structured_records_jsonl",
        "xml_tree_json",
    ):
        assert removed not in capability_by_backend


@pytest.mark.parametrize(
    "removed", ["oci_document_understanding", "structured_records_jsonl", "xml_tree_json"]
)
def test_persisted_removed_parser_backend_falls_back_to_default(
    tmp_path: Path, removed: str
) -> None:
    """以前の版が parser-settings.json に保存した削除済みのエンジンは既定へ寄せる(#22・#21)。

    起動を止めない。削除したエンジンの有効化の項目も読み捨てる。
    """
    parser_file = tmp_path / "parser-settings.json"
    parser_file.write_text(
        json.dumps({"adapter_backend": removed, f"{removed}_enabled": True}), encoding="utf-8"
    )
    worker_settings = Settings(
        _env_file=None,
        rag_parser_settings_file=str(parser_file),
        rag_parser_adapter_backend="unstructured",
    )
    store = copy.copy(PARSER_SETTINGS_STORE)
    store.reset()

    store.load(worker_settings)

    assert worker_settings.rag_parser_adapter_backend == "docling"


def test_env_removed_parser_backend_falls_back_to_default() -> None:
    """RAG_PARSER_ADAPTER_BACKEND の削除済みのエンジンも既定へ寄せる(#22)。"""
    settings = Settings(_env_file=None, rag_parser_adapter_backend="oci_document_understanding")

    assert settings.rag_parser_adapter_backend == "docling"


def test_parser_adapter_contract_endpoint_is_removed() -> None:
    """画面の「運用診断」とともに互換性確認の API を削除した（#492）。

    検査は CLI と nightly の評価が行う。
    """
    resp = client.get("/api/settings/parser-adapters/contract")

    assert resp.status_code == 404


def test_parser_adapter_settings_explicit_backend_requires_feature_flag(
    monkeypatch: MonkeyPatch,
) -> None:
    """backend を明示しても flag が false なら実行順から外し警告する。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_parser_adapter_backend", "unstructured")
    monkeypatch.setattr(settings, "rag_parser_docling_enabled", True)
    monkeypatch.setattr(settings, "rag_parser_unstructured_enabled", False)
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )

    resp = client.get("/api/settings/parser-adapters")

    assert resp.status_code == 200
    body = resp.json()["data"]
    by_backend = {adapter["backend"]: adapter for adapter in body["adapters"]}
    assert body["adapter_backend"] == "unstructured"
    assert body["effective_order"] == []
    assert by_backend["unstructured"]["selected"] is True
    assert by_backend["unstructured"]["enabled"] is False
    assert by_backend["unstructured"]["status"] == "disabled"
    assert by_backend["unstructured"]["warning_code"] == "adapter_feature_flag_disabled"
    assert by_backend["docling"]["status"] == "ignored"


def test_update_parser_adapter_settings_rejects_legacy_auto_without_mutating_runtime(
    monkeypatch: MonkeyPatch,
) -> None:
    """廃止済み auto は拒否し、現在プロセスの ingestion 設定を変えない。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_parser_adapter_backend", "local")
    monkeypatch.setattr(settings, "rag_parser_docling_enabled", False)
    monkeypatch.setattr(settings, "rag_parser_unstructured_enabled", False)
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )
    resp = client.patch(
        "/api/settings/parser-adapters",
        json={
            "adapter_backend": "auto",
            "docling_enabled": True,
            "unstructured_enabled": True,
        },
    )

    assert resp.status_code == 422
    assert settings.rag_parser_adapter_backend == "local"
    assert settings.rag_parser_docling_enabled is False
    assert settings.rag_parser_unstructured_enabled is False


def test_parser_adapter_settings_no_longer_handle_docling_vision(
    monkeypatch: MonkeyPatch,
) -> None:
    """文書解析の画面から Vision のスイッチを削除した(#497)。API も受け取らず返さない。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_parser_adapter_backend", "docling")
    monkeypatch.setattr(settings, "rag_vision_enabled", False)
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )
    resp = client.patch(
        "/api/settings/parser-adapters",
        json={"adapter_backend": "docling", "docling_vision_enabled": True},
    )

    assert resp.status_code == 200
    assert "docling_vision_enabled" not in resp.json()["data"]
    assert settings.rag_vision_enabled is False
    assert "docling_vision_enabled" not in _persisted_parser(settings)
    assert (
        "docling_vision_enabled" not in client.get("/api/settings/parser-adapters").json()["data"]
    )


def test_update_parser_adapter_settings_persists_flags_and_preserves_omitted_flags(
    monkeypatch: MonkeyPatch,
) -> None:
    """parser flags を保存し、省略された既存 flag は維持する。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_parser_adapter_backend", "local")
    monkeypatch.setattr(settings, "rag_parser_docling_enabled", True)
    monkeypatch.setattr(settings, "rag_parser_unstructured_enabled", False)
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )
    resp = client.patch(
        "/api/settings/parser-adapters",
        json={
            "adapter_backend": "unstructured",
            "unstructured_enabled": True,
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["adapter_backend"] == "unstructured"
    assert body["effective_order"] == ["unstructured"]
    by_backend = {adapter["backend"]: adapter for adapter in body["adapters"]}
    assert by_backend["docling"]["enabled"] is True
    assert by_backend["unstructured"]["enabled"] is True
    assert settings.rag_parser_adapter_backend == "unstructured"
    assert settings.rag_parser_docling_enabled is True
    assert settings.rag_parser_unstructured_enabled is True
    parser = _persisted_parser(settings)
    assert parser["adapter_backend"] == "unstructured"
    assert parser["docling_enabled"] is True
    assert parser["unstructured_enabled"] is True
    # 削除したエンジン(#270・#22・#21)の項目は保存しない。
    assert not [
        key
        for key in parser
        if any(name in key for name in ("marker", "unlimited", "mineru", "xml_tree", "records"))
    ]


def test_parser_settings_file_reloads_in_worker_and_model_save_does_not_touch_it(
    monkeypatch: MonkeyPatch,
) -> None:
    """文書解析の設定は parser-settings.json に保存し、共有の model-settings に書かない(#1701)。"""
    settings = get_settings()
    model_file = Path(settings.model_settings_file)
    parser_file = PARSER_SETTINGS_STORE.path(settings)
    monkeypatch.setattr(settings, "oci_enterprise_ai_endpoint", "https://existing.example.com")
    settings.set_runtime_enterprise_ai_api_key("existing-model-secret")
    worker_settings = Settings(
        _env_file=None,
        rag_parser_settings_file=str(parser_file),
        rag_parser_adapter_backend="local",
    )
    # 別 worker は別プロセスなので、読込状態を持つ store も別になる。
    worker_store = copy.copy(PARSER_SETTINGS_STORE)
    worker_store.reset()
    worker_store.load(worker_settings)

    response = client.patch(
        "/api/settings/parser-adapters",
        json={"adapter_backend": "unstructured", "unstructured_enabled": True},
    )

    assert response.status_code == 200
    assert not model_file.exists()
    assert stat.S_IMODE(parser_file.stat().st_mode) == 0o600
    lock_file = parser_file.with_name(f"{parser_file.name}.lock")
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600
    worker_store.reload_if_changed(worker_settings)
    assert worker_settings.rag_parser_adapter_backend == "unstructured"
    assert worker_settings.rag_parser_unstructured_enabled is True

    parser_before = parser_file.read_text(encoding="utf-8")
    model_response = client.patch("/api/settings/model", json=_payload())

    assert model_response.status_code == 200
    persisted = json.loads(model_file.read_text(encoding="utf-8"))
    assert "parser_adapters" not in persisted
    assert _saved_enterprise_ai_api_key(settings) == "sk-update-secret"
    assert parser_file.read_text(encoding="utf-8") == parser_before
    assert get_settings().rag_parser_adapter_backend == "unstructured"


def test_update_parser_adapter_settings_does_not_mutate_runtime_when_write_fails(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """parser adapter 設定保存に失敗したら runtime を変更しない。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_parser_adapter_backend", "local")
    monkeypatch.setattr(settings, "rag_parser_docling_enabled", False)
    monkeypatch.setattr(settings, "rag_parser_unstructured_enabled", False)
    monkeypatch.setattr(settings, "rag_parser_settings_file", str(tmp_path))

    resp = client.patch(
        "/api/settings/parser-adapters",
        json={
            "adapter_backend": "docling",
            "docling_enabled": True,
            "unstructured_enabled": True,
        },
    )

    assert resp.status_code == 500
    assert settings.rag_parser_adapter_backend == "local"
    assert settings.rag_parser_docling_enabled is False
    assert settings.rag_parser_unstructured_enabled is False


def test_update_parser_adapter_settings_rejects_unknown_backend() -> None:
    resp = client.patch(
        "/api/settings/parser-adapters",
        json={
            "adapter_backend": "llama_parse",
            "docling_enabled": True,
            "unstructured_enabled": False,
        },
    )

    assert resp.status_code == 422


@pytest.mark.parametrize(
    "removed",
    [
        "marker",
        "unlimited_ocr",
        "glm_ocr",
        "mineru",
        "dots_ocr",
        "oci_document_understanding",
        "structured_records_jsonl",
        "xml_tree_json",
    ],
)
def test_update_parser_adapter_settings_rejects_removed_engine(removed: str) -> None:
    """削除したエンジン(#270・#22・#21)は新たに選択できない。"""
    settings = get_settings()
    before = settings.rag_parser_adapter_backend

    resp = client.patch("/api/settings/parser-adapters", json={"adapter_backend": removed})

    assert resp.status_code == 422
    assert settings.rag_parser_adapter_backend == before


def test_preprocess_settings_reports_runtime_profile(monkeypatch: MonkeyPatch) -> None:
    """前処理設定 API は選択プロファイル・サービス状態・プロファイル一覧を返す。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_preprocess_profile", "office_to_pdf")
    monkeypatch.setattr(settings, "rag_preprocess_enabled", False)

    resp = client.get("/api/settings/preprocess")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["profile"] == "office_to_pdf"
    assert body["service_enabled"] is False
    assert body["config_source"] == "runtime"
    names = [item["name"] for item in body["profiles"]]
    assert names == [
        "passthrough",
        "office_to_pdf",
        "csv_to_jsonl",
        "excel_to_jsonl",
        "word_to_jsonl",
    ]
    by_name = {item["name"]: item for item in body["profiles"]}
    assert by_name["office_to_pdf"]["available"] is False
    selected = [item["name"] for item in body["profiles"] if item["selected"]]
    assert selected == ["office_to_pdf"]


def test_update_preprocess_settings_persists_env_and_mutates_runtime(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_preprocess_profile", "passthrough")
    env_file = _settings_env_file(monkeypatch, tmp_path)

    resp = client.patch("/api/settings/preprocess", json={"profile": "office_to_pdf"})

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["profile"] == "office_to_pdf"
    assert settings.rag_preprocess_profile == "office_to_pdf"
    assert "RAG_PREPROCESS_PROFILE=office_to_pdf" in env_file.read_text(encoding="utf-8")


def test_update_preprocess_settings_rejects_unknown_profile() -> None:
    resp = client.patch("/api/settings/preprocess", json={"profile": "ocr_magic"})
    assert resp.status_code == 422


def test_chunking_settings_reports_runtime_strategy_and_params(
    monkeypatch: MonkeyPatch,
) -> None:
    """Chunking 設定 API は選択戦略・パラメータ・戦略一覧を返す。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_chunking_strategy", "page_level")
    monkeypatch.setattr(settings, "rag_chunk_size", 900)
    monkeypatch.setattr(settings, "rag_chunk_overlap", 150)
    monkeypatch.setattr(settings, "rag_chunk_min_chars", 50)
    monkeypatch.setattr(settings, "rag_chunk_child_target_chars", 800)
    monkeypatch.setattr(settings, "rag_chunk_delimiter", "\\n---\\n")
    monkeypatch.setattr(settings, "rag_chunk_context_header_enabled", False)

    resp = client.get("/api/settings/chunking")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["strategy"] == "page_level"
    assert body["chunk_size"] == 900
    assert body["overlap"] == 150
    assert "child_size" not in body
    assert body["min_chars"] == 50
    assert body["chunk_child_target_chars"] == 800
    assert body["chunk_table_child_target_chars"] == 3000
    assert body["chunk_parent_target_chars"] == 6000
    assert body["chunk_parent_max_pages"] == 3
    assert body["chunk_parent_max_children"] == 12
    assert body["delimiter"] == "\\n---\\n"
    assert body["context_header_enabled"] is False
    assert body["config_source"] == "runtime"
    names = [item["name"] for item in body["strategies"]]
    # 親子階層（small-to-big）は、削除した親子階層の位置(3 番目)に並ぶ(#271)。
    assert names == [
        "structure_aware",
        "recursive_character",
        "small_to_big",
        "markdown_heading",
        "page_level",
        "fixed_size",
        "fixed_delimiter",
    ]
    assert all("uses_child_size" not in item for item in body["strategies"])
    selected = [item["name"] for item in body["strategies"] if item["selected"]]
    assert selected == ["page_level"]


def _keep_chunking_settings(monkeypatch: MonkeyPatch) -> Any:
    """PATCH が現在プロセスへ反映する文書分割設定をテスト後に戻す。"""
    settings = get_settings()
    for field in (
        "rag_chunking_strategy",
        "rag_chunk_size",
        "rag_chunk_overlap",
        "rag_chunk_min_chars",
        "rag_chunk_delimiter",
        "rag_chunk_context_header_enabled",
        *SMALL_TO_BIG_SETTING_FIELDS,
    ):
        monkeypatch.setattr(settings, field, getattr(settings, field))
    return settings


def test_update_chunking_settings_persists_env_and_mutates_runtime(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Chunking 設定は .env と現在プロセスの取込設定へ反映する。"""
    settings = _keep_chunking_settings(monkeypatch)
    monkeypatch.setattr(settings, "rag_chunking_strategy", "structure_aware")
    monkeypatch.setattr(settings, "rag_chunk_min_chars", 0)
    env_file = _settings_env_file(monkeypatch, tmp_path)

    resp = client.patch(
        "/api/settings/chunking",
        json={
            "strategy": "recursive_character",
            "chunk_size": 1000,
            "overlap": 100,
            "min_chars": 40,
            "delimiter": "---",
            "context_header_enabled": False,
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["strategy"] == "recursive_character"
    assert body["chunk_size"] == 1000
    assert settings.rag_chunking_strategy == "recursive_character"
    assert settings.rag_chunk_size == 1000
    assert settings.rag_chunk_min_chars == 40
    assert settings.rag_chunk_delimiter == "---"
    assert settings.rag_chunk_context_header_enabled is True
    persisted = env_file.read_text(encoding="utf-8")
    assert "RAG_CHUNKING_STRATEGY=recursive_character" in persisted
    assert "RAG_CHUNK_SIZE=1000" in persisted
    assert "RAG_CHUNK_MIN_CHARS=40" in persisted
    assert "RAG_CHUNK_DELIMITER=---" in persisted
    assert "RAG_CHUNK_CONTEXT_HEADER_ENABLED" not in persisted


def test_update_chunking_settings_saves_small_to_big_params(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """親子階層（small-to-big）の 5 項目を保存し、削除した親子階層の旧変数は .env から消す。"""
    settings = _keep_chunking_settings(monkeypatch)
    env_file = _settings_env_file(
        monkeypatch,
        tmp_path,
        "RAG_CHUNKING_STRATEGY=hierarchical_parent_child\nRAG_CHUNK_CHILD_SIZE=300\n",
    )

    resp = client.patch(
        "/api/settings/chunking",
        json={
            "strategy": "small_to_big",
            "chunk_size": 800,
            "overlap": 120,
            "min_chars": 120,
            "delimiter": "\\n\\n",
            "context_header_enabled": True,
            "chunk_child_target_chars": 600,
            "chunk_table_child_target_chars": 1500,
            "chunk_parent_target_chars": 4000,
            "chunk_parent_max_pages": 2,
            "chunk_parent_max_children": 8,
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["strategy"] == "small_to_big"
    assert body["chunk_child_target_chars"] == 600
    assert body["chunk_parent_max_children"] == 8
    assert settings.rag_chunking_strategy == "small_to_big"
    assert settings.rag_chunk_child_target_chars == 600
    assert settings.rag_chunk_table_child_target_chars == 1500
    assert settings.rag_chunk_parent_target_chars == 4000
    assert settings.rag_chunk_parent_max_pages == 2
    assert settings.rag_chunk_parent_max_children == 8
    persisted = env_file.read_text(encoding="utf-8")
    assert "RAG_CHUNKING_STRATEGY=small_to_big" in persisted
    assert "RAG_CHUNK_CHILD_TARGET_CHARS=600" in persisted
    assert "RAG_CHUNK_TABLE_CHILD_TARGET_CHARS=1500" in persisted
    assert "RAG_CHUNK_PARENT_TARGET_CHARS=4000" in persisted
    assert "RAG_CHUNK_PARENT_MAX_PAGES=2" in persisted
    assert "RAG_CHUNK_PARENT_MAX_CHILDREN=8" in persisted
    assert "RAG_CHUNK_CHILD_SIZE" not in persisted
    assert "hierarchical_parent_child" not in persisted


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chunk_child_target_chars", 299),
        ("chunk_child_target_chars", 1601),
        ("chunk_table_child_target_chars", 8001),
        ("chunk_parent_target_chars", 1199),
        ("chunk_parent_max_pages", 6),
        ("chunk_parent_max_children", 2),
    ],
)
def test_update_chunking_settings_rejects_small_to_big_params_out_of_range(
    field: str,
    value: int,
) -> None:
    payload: dict[str, object] = {
        "strategy": "small_to_big",
        "chunk_size": 800,
        "overlap": 120,
        "min_chars": 120,
        field: value,
    }

    assert client.patch("/api/settings/chunking", json=payload).status_code == 422


def test_update_chunking_settings_rejects_removed_parent_child_strategy() -> None:
    """削除した親子階層は新しく保存できない(保存済みの値は読み込み時に回答フローへ読み替える)。"""
    resp = client.patch(
        "/api/settings/chunking",
        json={"strategy": "hierarchical_parent_child", "chunk_size": 800, "overlap": 120},
    )

    assert resp.status_code == 422


def test_update_chunking_settings_rejects_unknown_strategy() -> None:
    resp = client.patch(
        "/api/settings/chunking",
        json={
            "strategy": "semantic_double_pass",
            "chunk_size": 800,
            "overlap": 120,
            "min_chars": 0,
        },
    )

    assert resp.status_code == 422


def test_update_chunking_settings_rejects_overlap_not_smaller_than_size() -> None:
    resp = client.patch(
        "/api/settings/chunking",
        json={
            "strategy": "structure_aware",
            "chunk_size": 400,
            "overlap": 400,
            "min_chars": 0,
        },
    )

    assert resp.status_code == 422


def test_update_chunking_settings_accepts_new_maximums(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    for field in (
        "rag_chunking_strategy",
        "rag_chunk_size",
        "rag_chunk_overlap",
        "rag_chunk_min_chars",
        "rag_chunk_delimiter",
        "rag_chunk_context_header_enabled",
    ):
        monkeypatch.setattr(settings, field, getattr(settings, field))
    _settings_env_file(monkeypatch, tmp_path)

    resp = client.patch(
        "/api/settings/chunking",
        json={
            "strategy": "page_level",
            "chunk_size": 32_000,
            "overlap": 8_000,
            "min_chars": 120,
            "delimiter": "\\n\\n",
            "context_header_enabled": True,
        },
    )

    assert resp.status_code == 200
    assert settings.rag_chunk_size == 32_000
    assert settings.rag_chunk_overlap == 8_000


@pytest.mark.parametrize(
    ("field", "value"),
    [("chunk_size", 32_001), ("overlap", 8_001)],
)
def test_update_chunking_settings_rejects_values_above_new_maximums(
    field: str,
    value: int,
) -> None:
    payload = {
        "strategy": "structure_aware",
        "chunk_size": 32_000,
        "overlap": 120,
        "min_chars": 120,
        "delimiter": "\\n\\n",
        "context_header_enabled": True,
    }
    payload[field] = value

    assert client.patch("/api/settings/chunking", json=payload).status_code == 422


def test_update_chunking_settings_ignores_non_applicable_bounds_for_fixed_size(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """固定長は min_chars の cross-field bounds を使わない。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_chunking_strategy", "structure_aware")
    monkeypatch.setattr(settings, "rag_chunk_size", 800)
    monkeypatch.setattr(settings, "rag_chunk_overlap", 120)
    monkeypatch.setattr(settings, "rag_chunk_min_chars", 0)
    monkeypatch.setattr(settings, "rag_chunk_delimiter", "\\n\\n")
    _settings_env_file(monkeypatch, tmp_path)

    resp = client.patch(
        "/api/settings/chunking",
        json={
            "strategy": "fixed_size",
            "chunk_size": 400,
            "overlap": 120,
            "min_chars": 400,
            "delimiter": "\\n\\n",
        },
    )

    assert resp.status_code == 200
    assert settings.rag_chunking_strategy == "fixed_size"


def test_update_chunking_settings_allows_fixed_delimiter_without_chunk_bounds(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_chunking_strategy", "structure_aware")
    monkeypatch.setattr(settings, "rag_chunk_size", 800)
    monkeypatch.setattr(settings, "rag_chunk_overlap", 120)
    monkeypatch.setattr(settings, "rag_chunk_min_chars", 0)
    monkeypatch.setattr(settings, "rag_chunk_delimiter", "\\n\\n")
    env_file = _settings_env_file(monkeypatch, tmp_path)

    resp = client.patch(
        "/api/settings/chunking",
        json={
            "strategy": "fixed_delimiter",
            "chunk_size": 400,
            "overlap": 400,
            "min_chars": 400,
            "delimiter": "---",
        },
    )

    assert resp.status_code == 200
    assert settings.rag_chunking_strategy == "fixed_delimiter"
    assert "RAG_CHUNK_DELIMITER=---" in env_file.read_text(encoding="utf-8")


def test_guardrail_settings_exposes_independent_switches(monkeypatch: MonkeyPatch) -> None:
    for side in ("input", "output"):
        monkeypatch.setattr(get_settings(), f"rag_guardrails_{side}_enabled", False)
    body = client.get("/api/settings/guardrail").json()["data"]
    assert body["input_enabled"] is False and body["output_enabled"] is False
    assert set(body) == {
        "input_enabled",
        "output_enabled",
        "oci_configured",
        "oci_warning_code",
        "config_source",
    }


@pytest.mark.parametrize("input_enabled", [True, False])
@pytest.mark.parametrize("output_enabled", [True, False])
def test_guardrail_switches_persist_env(
    monkeypatch: MonkeyPatch, tmp_path: Path, input_enabled: bool, output_enabled: bool
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_guardrails_input_enabled", not input_enabled)
    monkeypatch.setattr(settings, "rag_guardrails_output_enabled", not output_enabled)
    monkeypatch.setattr(settings_routes, "_oci_guardrails_readiness_code", lambda _: None)
    env_file = _settings_env_file(monkeypatch, tmp_path)
    body = {"input_enabled": input_enabled, "output_enabled": output_enabled}
    response = client.patch("/api/settings/guardrail", json=body)
    assert response.status_code == 200
    for side, enabled in (("input", input_enabled), ("output", output_enabled)):
        assert response.json()["data"][f"{side}_enabled"] is enabled
        assert getattr(settings, f"rag_guardrails_{side}_enabled") is enabled
        assert (
            f"RAG_GUARDRAILS_{side.upper()}_ENABLED={str(enabled).lower()}" in env_file.read_text()
        )


@pytest.mark.parametrize("side", ["input", "output"])
def test_guardrail_on_rejects_missing_oci_configuration(
    monkeypatch: MonkeyPatch, side: str
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_guardrails_input_enabled", False)
    monkeypatch.setattr(settings, "rag_guardrails_output_enabled", False)
    monkeypatch.setattr(settings, "oci_guardrails_compartment_id", "")
    monkeypatch.setattr(settings, "oci_compartment_id", "")
    body = {"input_enabled": False, "output_enabled": False, f"{side}_enabled": True}
    assert client.patch("/api/settings/guardrail", json=body).status_code == 422
    assert not settings.rag_guardrails_input_enabled and not settings.rag_guardrails_output_enabled


def test_guardrail_off_does_not_require_oci_configuration(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(get_settings(), "rag_guardrails_input_enabled", True)
    monkeypatch.setattr(get_settings(), "rag_guardrails_output_enabled", True)
    monkeypatch.setattr(settings_routes, "_oci_guardrails_readiness_code", lambda _: "missing")
    _settings_env_file(monkeypatch, tmp_path)
    response = client.patch(
        "/api/settings/guardrail", json={"input_enabled": False, "output_enabled": False}
    )
    assert response.status_code == 200
    assert (
        not response.json()["data"]["input_enabled"]
        and not response.json()["data"]["output_enabled"]
    )


def test_guardrail_rejects_removed_policy_and_backend() -> None:
    response = client.patch(
        "/api/settings/guardrail", json={"enabled": False, "policy": "strict", "backend": "local"}
    )
    assert response.status_code == 422


def _stub_vector_index_build(monkeypatch: MonkeyPatch, actual: tuple[int, int] | None) -> None:
    """実際の索引の値(v$vector_graph_index)の読み取りを差し替える。"""

    async def fake_build_params(_self: object) -> tuple[int, int] | None:
        return actual

    monkeypatch.setattr(OracleClient, "get_vector_index_build_params", fake_build_params)


def test_vector_index_settings_reports_runtime_profile(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_vector_index_profile", "accurate")
    monkeypatch.setattr(settings, "oracle_vector_target_accuracy", 95)
    _stub_vector_index_build(monkeypatch, (32, 500))

    resp = client.get("/api/settings/vector-index")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["profile"] == "accurate"
    assert body["target_accuracy"] == 98
    assert body["requires_reprovision"] is True
    assert body["index_status"] == "reprovision"
    assert (body["actual_neighbors"], body["actual_efconstruction"]) == (32, 500)
    assert "NEIGHBORS 48" in body["reindex_sql"]
    names = [item["name"] for item in body["profiles"]]
    assert names[0] == "balanced"
    selected = [item["name"] for item in body["profiles"] if item["selected"]]
    assert selected == ["accurate"]


def test_vector_index_balanced_reports_existing_accuracy(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_vector_index_profile", "balanced")
    monkeypatch.setattr(settings, "oracle_vector_target_accuracy", 92)
    _stub_vector_index_build(monkeypatch, (32, 500))

    resp = client.get("/api/settings/vector-index")

    body = resp.json()["data"]
    assert body["profile"] == "balanced"
    assert body["target_accuracy"] == 92
    assert body["requires_reprovision"] is False


@pytest.mark.parametrize(
    ("actual", "status", "requires"),
    [((48, 800), "match", False), (None, "unknown", False)],
    ids=["match", "unknown"],
)
def test_vector_index_settings_judges_against_actual_index(
    monkeypatch: MonkeyPatch,
    actual: tuple[int, int] | None,
    status: str,
    requires: bool,
) -> None:
    """推奨値と実際の索引が同じなら再作成不要、読めないなら確認できない(#562)。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_vector_index_profile", "accurate")
    _stub_vector_index_build(monkeypatch, actual)

    body = client.get("/api/settings/vector-index").json()["data"]

    assert body["index_status"] == status
    assert body["requires_reprovision"] is requires
    assert body["actual_neighbors"] == (actual[0] if actual else None)
    assert body["actual_efconstruction"] == (actual[1] if actual else None)
    by_name = {item["name"]: item["index_status"] for item in body["profiles"]}
    assert by_name["accurate"] == status


def test_update_vector_index_settings_persists_env_and_mutates_runtime(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_vector_index_profile", "balanced")
    env_file = _settings_env_file(monkeypatch, tmp_path)
    _stub_vector_index_build(monkeypatch, None)

    resp = client.patch("/api/settings/vector-index", json={"profile": "fast"})

    assert resp.status_code == 200
    assert resp.json()["data"]["profile"] == "fast"
    assert resp.json()["data"]["target_accuracy"] == 85
    assert settings.rag_vector_index_profile == "fast"
    assert "RAG_VECTOR_INDEX_PROFILE=fast" in env_file.read_text(encoding="utf-8")


def test_update_vector_index_settings_rejects_unknown_profile() -> None:
    resp = client.patch("/api/settings/vector-index", json={"profile": "ivf_flat"})
    assert resp.status_code == 422


def test_evaluation_settings_reports_runtime_suite(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_evaluation_suite", "strict")

    resp = client.get("/api/settings/evaluation-suite")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["suite"] == "strict"
    assert body["thresholds"]["claim_support_rate"] == 1.0
    names = [item["name"] for item in body["suites"]]
    assert names == ["standard", "strict"]
    selected = [item["name"] for item in body["suites"] if item["selected"]]
    assert selected == ["strict"]


def test_evaluation_settings_every_suite_has_thresholds(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_evaluation_suite", "standard")

    resp = client.get("/api/settings/evaluation-suite")

    body = resp.json()["data"]
    assert body["suite"] == "standard"
    assert body["thresholds"]["required_evidence_recall"] == 0.9
    assert all(len(item["thresholds"]) == 6 for item in body["suites"])


def test_update_evaluation_settings_persists_env_and_mutates_runtime(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_evaluation_suite", "standard")
    env_file = _settings_env_file(monkeypatch, tmp_path)

    resp = client.patch("/api/settings/evaluation-suite", json={"suite": "strict"})

    assert resp.status_code == 200
    assert resp.json()["data"]["suite"] == "strict"
    assert settings.rag_evaluation_suite == "strict"
    assert "RAG_EVALUATION_SUITE=strict" in env_file.read_text(encoding="utf-8")


def test_update_evaluation_settings_rejects_unknown_suite() -> None:
    resp = client.patch("/api/settings/evaluation-suite", json={"suite": "autorag_tuner"})
    assert resp.status_code == 422
    # 削除した基準の名前は API では受け付けない(.env の旧値は起動時に後継へ寄せる)。
    resp = client.patch("/api/settings/evaluation-suite", json={"suite": "strict_ci"})
    assert resp.status_code == 422


def test_graph_settings_reports_runtime_profile(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_graph_profile", "entities")

    resp = client.get("/api/settings/graph")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body == {
        "profile": "entities",
        "enabled": True,
        # 選択肢は「構築しない」「構築する」の 2 つ(#621)。
        "profiles": [
            {"name": "off", "selected": False},
            {"name": "entities", "selected": True},
        ],
        "config_source": "runtime",
    }


def test_graph_settings_off_disables_build(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_graph_profile", "off")

    resp = client.get("/api/settings/graph")

    body = resp.json()["data"]
    assert body["profile"] == "off"
    assert body["enabled"] is False


def test_update_graph_settings_persists_env_and_mutates_runtime(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_graph_profile", "off")
    env_file = _settings_env_file(monkeypatch, tmp_path)

    resp = client.patch("/api/settings/graph", json={"profile": "entities"})

    assert resp.status_code == 200
    assert resp.json()["data"]["profile"] == "entities"
    assert resp.json()["data"]["enabled"] is True
    assert settings.rag_graph_profile == "entities"
    env_text = env_file.read_text(encoding="utf-8")
    assert "RAG_GRAPH_PROFILE=entities" in env_text
    # legacy の RAG_GRAPH_ENABLED は #621 で廃止した(書かない)。
    assert "RAG_GRAPH_ENABLED" not in env_text


def test_update_graph_settings_rejects_unknown_profile() -> None:
    resp = client.patch("/api/settings/graph", json={"profile": "neo4j"})
    assert resp.status_code == 422
    # claims / community summary まで作る full は #621 で削除した。
    resp = client.patch("/api/settings/graph", json={"profile": "full"})
    assert resp.status_code == 422


def test_get_model_settings_returns_runtime_values(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_enterprise_ai_endpoint", "https://enterprise-ai.example")
    monkeypatch.setattr(
        settings,
        "oci_enterprise_ai_project_ocid",
        "ocid1.generativeaiproject.oc1..example",
    )
    monkeypatch.setattr(settings, "oci_enterprise_ai_api_key", "sk-runtime-secret")
    monkeypatch.setattr(settings, "oci_enterprise_ai_default_text_model", "enterprise-llm")
    monkeypatch.setattr(settings, "oci_enterprise_ai_default_vision_model", "enterprise-vlm")
    monkeypatch.setattr(settings, "oci_enterprise_ai_models", [])
    monkeypatch.setattr(settings, "oci_enterprise_ai_llm_path", "/responses")
    monkeypatch.setattr(settings, "oci_enterprise_ai_vlm_path", "/responses")
    monkeypatch.setattr(settings, "oci_enterprise_ai_llm_payload_template", LLM_TEMPLATE)
    monkeypatch.setattr(settings, "oci_enterprise_ai_vlm_payload_template", VLM_TEMPLATE)
    monkeypatch.setattr(settings, "oci_enterprise_ai_llm_response_path", "/data/text")
    monkeypatch.setattr(settings, "oci_enterprise_ai_vlm_response_path", "/data/document")
    monkeypatch.setattr(settings, "oci_genai_embedding_model", "cohere.embed-v4.0")
    monkeypatch.setattr(settings, "oci_genai_embedding_dim", 1536)
    monkeypatch.setattr(settings, "oci_genai_rerank_model", "cohere.rerank-v4.0-fast")

    resp = client.get("/api/settings/model")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert (
        body["settings"]["enterprise_ai"]["connections"][0]["endpoint"]
        == "https://enterprise-ai.example"
    )
    assert (
        body["settings"]["enterprise_ai"]["connections"][0]["project_ocid"]
        == "ocid1.generativeaiproject.oc1..example"
    )
    assert body["settings"]["enterprise_ai"]["connections"][0]["api_key"] == ""
    assert body["settings"]["enterprise_ai"]["connections"][0]["has_api_key"] is True
    assert body["settings"]["enterprise_ai"]["models"] == [
        {
            "model_id": "enterprise-llm",
            "display_name": "enterprise-llm",
            "vision_enabled": False,
            "connection_id": "primary",
            "reasoning_effort": "low",
            "max_input_tokens": 128000,
            "max_output_tokens": 32768,
        },
        {
            "model_id": "enterprise-vlm",
            "display_name": "enterprise-vlm",
            "vision_enabled": True,
            "connection_id": "primary",
            "reasoning_effort": "low",
            "max_input_tokens": 128000,
            "max_output_tokens": 32768,
        },
    ]
    assert body["settings"]["enterprise_ai"]["default_text_model_id"] == "enterprise-llm"
    assert body["settings"]["enterprise_ai"]["default_vision_model_id"] == "enterprise-vlm"
    assert body["settings"]["enterprise_ai"]["api_path"] == "/responses"
    assert body["settings"]["enterprise_ai"]["vlm_input_mode"] == "files_api"
    assert body["settings"]["enterprise_ai"]["text_payload_template"] == LLM_TEMPLATE
    assert body["settings"]["enterprise_ai"]["vision_payload_template"] == VLM_TEMPLATE
    assert body["settings"]["enterprise_ai"]["text_response_path"] == "/data/text"
    assert body["settings"]["enterprise_ai"]["vision_response_path"] == "/data/document"
    assert body["settings"]["generative_ai"]["embedding_dim"] == 1536
    assert "sk-runtime-secret" not in resp.text


def test_update_model_settings_mutates_runtime_settings() -> None:
    payload = _payload()

    resp = client.patch("/api/settings/model", json=payload)

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["model_settings_file"] == get_settings().model_settings_file
    settings = get_settings()
    assert settings.oci_enterprise_ai_endpoint == "https://enterprise-ai.example"
    assert settings.oci_enterprise_ai_project_ocid == "ocid1.generativeaiproject.oc1..example"
    assert settings.oci_enterprise_ai_api_key == "sk-update-secret"
    assert settings.oci_enterprise_ai_default_text_model == "enterprise-llm"
    assert settings.oci_enterprise_ai_default_vision_model == "enterprise-vlm"
    assert [model.model_id for model in settings.oci_enterprise_ai_models] == [
        "enterprise-llm",
        "enterprise-vlm",
    ]
    assert settings.oci_enterprise_ai_llm_path == "/responses"
    assert settings.oci_enterprise_ai_vlm_path == "/responses"
    assert settings.oci_enterprise_ai_vlm_input_mode == "files_api"
    assert settings.oci_enterprise_ai_llm_payload_template == LLM_TEMPLATE
    assert settings.oci_enterprise_ai_vlm_payload_template == VLM_TEMPLATE
    assert settings.oci_enterprise_ai_llm_response_path == "/data/text"
    assert settings.oci_enterprise_ai_vlm_response_path == "/data/document"
    # 出力の上限は登録モデルごとの値（#1719）。
    assert [model.max_output_tokens for model in settings.oci_enterprise_ai_models] == [
        16000,
        64000,
    ]
    assert settings.oci_genai_embedding_model == "cohere.embed-v4.0"
    assert settings.oci_genai_embedding_dim == 1536
    assert settings.oci_genai_rerank_model == "cohere.rerank-v4.0-fast"
    assert "sk-update-secret" not in resp.text


def test_update_model_settings_persists_private_json(tmp_path: Path) -> None:
    settings = get_settings()
    settings.model_settings_file = str(tmp_path / "config" / "model-settings.json")
    payload = _payload()

    resp = client.patch("/api/settings/model", json=payload)

    assert resp.status_code == 200
    settings_file = Path(settings.model_settings_file)
    assert settings_file.is_file()
    assert stat.S_IMODE(settings_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert persisted["version"] == 6
    # API key は JSON ではなく共通 .env に保存する（#103 / #211）。
    assert "api_key" not in persisted["enterprise_ai"]
    assert _saved_enterprise_ai_api_key(settings) == "sk-update-secret"
    assert persisted["enterprise_ai"]["models"] == [
        {
            "model_id": "enterprise-llm",
            "display_name": "標準 LLM",
            "vision_enabled": False,
            "connection_id": "primary",
            "reasoning_effort": "low",
            "max_input_tokens": 128000,
            "max_output_tokens": 16000,
        },
        {
            "model_id": "enterprise-vlm",
            "display_name": "Vision LLM",
            "vision_enabled": True,
            "connection_id": "primary",
            "reasoning_effort": "low",
            "max_input_tokens": 128000,
            "max_output_tokens": 64000,
        },
    ]
    assert persisted["enterprise_ai"]["default_text_model_id"] == "enterprise-llm"
    assert persisted["enterprise_ai"]["default_vision_model_id"] == "enterprise-vlm"
    assert "default_model_id" not in persisted["enterprise_ai"]
    assert persisted["enterprise_ai"]["vlm_input_mode"] == "files_api"
    # 共通の LLM / VLM の出力の上限は書かない（#1719。モデルごとの max_output_tokens）。
    assert not [key for key in persisted["enterprise_ai"] if key.endswith("_max_output_tokens")]
    assert persisted["generative_ai"]["embedding_dim"] == 1536
    # 文書解析の設定は共有の model-settings.json に書かない（#1701）。
    assert "parser_adapters" not in persisted
    assert "sk-update-secret" not in resp.text


def test_load_persisted_model_settings_applies_saved_model_catalog(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "model-settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "version": 1,
                "enterprise_ai": {
                    "endpoint": "https://persisted-enterprise.example",
                    "project_ocid": "ocid1.generativeaiproject.oc1..persisted",
                    "api_key": "sk-persisted-secret",
                    "models": [
                        {
                            "model_id": "persisted-text",
                            "display_name": "永続 Text",
                            "vision_enabled": False,
                        },
                        {
                            "model_id": "persisted-vision",
                            "display_name": "永続 Vision",
                            "vision_enabled": True,
                        },
                    ],
                    "default_model_id": "persisted-text",
                    "api_path": "/responses",
                    "vlm_input_mode": "inline_image",
                    "text_payload_template": LLM_TEMPLATE,
                    "vision_payload_template": VLM_TEMPLATE,
                    "text_response_path": "/payload/text",
                    "vision_response_path": "/payload/document",
                    "timeout_seconds": 42,
                    "max_retries": 1,
                },
                "generative_ai": {
                    "embedding_model": "cohere.embed-v4.0",
                    "embedding_dim": 1536,
                    "rerank_model": "cohere.rerank-v4.0-fast",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        model_settings_file=str(settings_file),
        rag_parser_adapter_backend="docling",
        rag_parser_docling_enabled=True,
    )
    load_persisted_model_settings(settings)

    assert settings.oci_enterprise_ai_endpoint == "https://persisted-enterprise.example"
    assert settings.oci_enterprise_ai_project_ocid == "ocid1.generativeaiproject.oc1..persisted"
    assert settings.oci_enterprise_ai_api_key == "sk-persisted-secret"
    assert [model.model_id for model in settings.oci_enterprise_ai_models] == [
        "persisted-text",
        "persisted-vision",
    ]
    # #499 より前の JSON（default_model_id だけ）は、従来と同じモデルになるよう補う。
    assert settings.oci_enterprise_ai_default_text_model == "persisted-text"
    assert settings.oci_enterprise_ai_default_vision_model == "persisted-vision"
    assert settings.oci_enterprise_ai_vlm_input_mode == "inline_image"
    assert settings.oci_enterprise_ai_llm_response_path == "/payload/text"
    assert settings.oci_enterprise_ai_vlm_response_path == "/payload/document"
    assert settings.oci_enterprise_ai_timeout_seconds == 42
    assert settings.oci_enterprise_ai_max_retries == 1
    # 出力の上限の無い古い JSON のモデルは既定の 32768（#1719）。
    assert [model.max_output_tokens for model in settings.oci_enterprise_ai_models] == [
        32768,
        32768,
    ]
    assert settings.rag_parser_adapter_backend == "docling"
    assert settings.rag_parser_docling_enabled is True


def test_update_model_settings_does_not_mutate_runtime_when_persist_fails(
    tmp_path: Path,
) -> None:
    settings = get_settings()
    settings.model_settings_file = str(tmp_path)
    settings.oci_enterprise_ai_endpoint = "https://old-enterprise.example"
    settings.oci_enterprise_ai_api_key = "sk-old-secret"

    resp = client.patch("/api/settings/model", json=_payload())

    assert resp.status_code == 500
    assert settings.oci_enterprise_ai_endpoint == "https://old-enterprise.example"
    assert settings.oci_enterprise_ai_api_key == "sk-old-secret"


def test_model_settings_test_enterprise_text_uses_candidate_without_mutating_runtime(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_enterprise_ai_endpoint", "https://runtime.example")
    observed_settings: list[Settings] = []

    class FakeEnterpriseAiClient:
        def __init__(self, settings: Settings) -> None:
            observed_settings.append(settings)

        async def generate(self, prompt: str, context: str) -> str:
            assert prompt
            assert context
            return "接続テスト応答"

    monkeypatch.setattr(settings_routes, "OciEnterpriseAiClient", FakeEnterpriseAiClient)
    payload = _payload()

    resp = client.post(
        "/api/settings/model/test",
        json={
            "settings": payload,
            "target_type": "enterprise_text",
            "model_id": "enterprise-llm",
            "vision_enabled": False,
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "success"
    assert body["target_type"] == "enterprise_text"
    assert body["details"]["surface"] == "llm"
    assert observed_settings[0].oci_enterprise_ai_default_text_model == "enterprise-llm"
    assert observed_settings[0].oci_enterprise_ai_api_key == "sk-update-secret"
    assert settings.oci_enterprise_ai_endpoint == "https://runtime.example"
    assert "sk-update-secret" not in resp.text


def test_model_settings_test_enterprise_vision_uses_smoke_image_payload(
    monkeypatch: MonkeyPatch,
) -> None:
    observed: list[tuple[Settings, bytes, str, str]] = []

    class FakeEnterpriseAiClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def generate_from_image(
            self,
            image_bytes: bytes,
            prompt: str,
            *,
            mime_type: str,
        ) -> str:
            observed.append((self.settings, image_bytes, prompt, mime_type))
            return "画像を確認しました。"

    monkeypatch.setattr(settings_routes, "OciEnterpriseAiClient", FakeEnterpriseAiClient)
    payload = _payload()

    resp = client.post(
        "/api/settings/model/test",
        json={
            "settings": payload,
            "target_type": "enterprise_vision",
            "model_id": "google.gemini-2.5-flash",
            "vision_enabled": True,
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "success"
    assert body["details"]["surface"] == "vision"
    assert body["details"]["response_chars"] == len("画像を確認しました。")
    assert observed[0][0].oci_enterprise_ai_default_vision_model == "google.gemini-2.5-flash"
    assert observed[0][1] == MODEL_TEST_IMAGE_BYTES
    assert observed[0][2]
    assert observed[0][3] == "image/jpeg"


def test_model_settings_test_embedding_uses_candidate_model(
    monkeypatch: MonkeyPatch,
) -> None:
    observed_settings: list[Settings] = []

    class FakeGenAiClient:
        def __init__(self, settings: Settings) -> None:
            observed_settings.append(settings)

        async def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
            assert model_purpose_attributes() == {"pr.gen_ai.purpose": "connection.check"}
            assert texts == ["モデル接続テスト"]
            assert input_type == "SEARCH_QUERY"
            return [[0.0] * 1536]

    monkeypatch.setattr(settings_routes, "OciGenAiClient", FakeGenAiClient)
    payload = _payload()
    payload["generative_ai"]["embedding_model"] = "cohere.embed-custom"

    resp = client.post(
        "/api/settings/model/test",
        json={
            "settings": payload,
            "target_type": "embedding",
            "model_id": "cohere.embed-custom",
            "vision_enabled": False,
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "success"
    assert body["details"]["vector_dim"] == 1536
    assert observed_settings[0].oci_genai_embedding_model == "cohere.embed-custom"


def test_model_settings_test_returns_real_error_with_troubleshooting_and_masks_secret(
    monkeypatch: MonkeyPatch,
) -> None:
    class FailingEnterpriseAiClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def generate(self, prompt: str, context: str) -> str:
            raise RuntimeError(
                f"401 Unauthorized: bearer {self.settings.oci_enterprise_ai_api_key}"
            )

    monkeypatch.setattr(settings_routes, "OciEnterpriseAiClient", FailingEnterpriseAiClient)
    payload = _payload()

    resp = client.post(
        "/api/settings/model/test",
        json={
            "settings": payload,
            "target_type": "enterprise_text",
            "model_id": "enterprise-llm",
            "vision_enabled": False,
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "failed"
    assert body["error_type"] == "RuntimeError"
    assert "401 Unauthorized" in body["raw_error"]
    assert "<secret>" in body["raw_error"]
    assert body["troubleshooting"]
    assert "sk-update-secret" not in resp.text


def test_model_settings_rejects_null_endpoint() -> None:
    payload = _payload()
    payload["enterprise_ai"]["endpoint"] = None

    resp = client.patch("/api/settings/model", json=payload)

    assert resp.status_code == 422
    assert resp.json()["error_messages"]


def test_update_model_settings_allows_invalid_readiness_fields() -> None:
    payload = _payload()
    payload["enterprise_ai"]["endpoint"] = "enterprise-ai.example"
    payload["enterprise_ai"]["project_ocid"] = "not-an-ocid"
    payload["enterprise_ai"]["api_path"] = "responses"

    resp = client.patch("/api/settings/model", json=payload)

    assert resp.status_code == 200
    settings = get_settings()
    assert settings.oci_enterprise_ai_endpoint == "enterprise-ai.example"
    assert settings.oci_enterprise_ai_project_ocid == "not-an-ocid"
    assert settings.oci_enterprise_ai_llm_path == "responses"


def test_enterprise_ai_model_settings_defaults_max_retries_to_three() -> None:
    """設定 API スキーマの最大リトライ回数既定値は 3。"""
    assert EnterpriseAiModelSettings().max_retries == 3
    # 共有 schema（#103）の既定は auto。RAG の runtime は auto を files_api として扱う。
    assert EnterpriseAiModelSettings().vlm_input_mode == "auto"
    # 出力の上限は登録モデルごと（#1719。既定 32768）。共通の値は持たない。
    fields = EnterpriseAiModelSettings.model_fields
    assert not [name for name in fields if name.endswith("_max_output_tokens")]


def test_model_settings_rejects_invalid_payload_template() -> None:
    payload = _payload()
    payload["enterprise_ai"]["text_payload_template"] = "[1, 2, 3]"

    resp = client.patch("/api/settings/model", json=payload)

    assert resp.status_code == 422
    assert resp.json()["error_messages"]


def test_model_settings_keeps_existing_api_key_when_secret_input_is_blank(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_enterprise_ai_api_key", "sk-existing-secret")
    payload = _payload()
    payload["enterprise_ai"]["api_key"] = ""
    payload["enterprise_ai"]["has_api_key"] = True

    resp = client.patch("/api/settings/model", json=payload)

    assert resp.status_code == 200
    assert settings.oci_enterprise_ai_api_key == "sk-existing-secret"
    assert resp.json()["data"]["settings"]["enterprise_ai"]["connections"][0]["has_api_key"] is True
    assert "sk-existing-secret" not in resp.text


def test_model_settings_clears_existing_api_key(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_enterprise_ai_api_key", "sk-existing-secret")
    payload = _payload()
    payload["enterprise_ai"]["api_key"] = ""
    payload["enterprise_ai"]["has_api_key"] = True
    payload["enterprise_ai"]["clear_api_key"] = True

    resp = client.patch("/api/settings/model", json=payload)

    assert resp.status_code == 200
    assert settings.oci_enterprise_ai_api_key == ""
    assert (
        resp.json()["data"]["settings"]["enterprise_ai"]["connections"][0]["has_api_key"] is False
    )
    assert "sk-existing-secret" not in resp.text


def test_model_settings_rejects_non_1536_embedding_dim() -> None:
    payload = _payload()
    payload["generative_ai"]["embedding_dim"] = 1024

    resp = client.patch("/api/settings/model", json=payload)

    assert resp.status_code == 422
    body = resp.json()
    assert body["data"] is None
    assert body["error_messages"]


def test_read_oci_config_uses_runtime_config_and_profile(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """読むのは実行中の設定の config / profile（要求の path は使わない。#1067）。"""
    config_file = tmp_path / "config"
    config_file.write_text(
        "\n".join(
            [
                "[DEFAULT]",
                "tenancy=ocid1.tenancy.oc1..shared",
                "region=ap-tokyo-1",
                "key_file=/home/app/.oci/default.pem",
                "[RAG_PROD]",
                "user=ocid1.user.oc1..prod",
                "fingerprint=12:34:56:78",
                "region=ap-osaka-1",
                "compartment=ocid1.compartment.oc1..prod",
            ]
        ),
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(config_file))
    monkeypatch.setattr(settings, "oci_config_profile", "RAG_PROD")

    resp = client.post(
        "/api/settings/oci/config/read",
        json={"config_file": str(tmp_path / "other"), "profile": "DEFAULT"},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body == {
        "profile": "RAG_PROD",
        "user": "ocid1.user.oc1..prod",
        "fingerprint": "12:34:56:78",
        "tenancy": "ocid1.tenancy.oc1..shared",
        "region": "ap-osaka-1",
        "key_file": "/home/app/.oci/default.pem",
        "applied_fields": [
            "user",
            "fingerprint",
            "tenancy",
            "region",
            "key_file",
        ],
    }
    assert "compartment" not in body


def test_get_oci_settings_returns_runtime_and_config_values(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    key_file = tmp_path / ".oci" / "oci_api_key.pem"
    key_file.parent.mkdir()
    key_file.write_text(
        "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    config_file = tmp_path / ".oci" / "config"
    config_file.write_text(
        "\n".join(
            [
                "[DEFAULT]",
                "user=ocid1.user.oc1..runtime",
                "fingerprint=12:34:56:78",
                "tenancy=ocid1.tenancy.oc1..runtime",
                "region=ap-tokyo-1",
                "key_file=/tmp/ignored.pem",
            ]
        ),
        encoding="utf-8",
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(config_file))
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    monkeypatch.setattr(settings, "oci_region", "us-chicago-1")

    resp = client.get("/api/settings/oci")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body == {
        "config_file": str(config_file),
        "profile": "DEFAULT",
        "user": "ocid1.user.oc1..runtime",
        "fingerprint": "12:34:56:78",
        "tenancy": "ocid1.tenancy.oc1..runtime",
        "region": "ap-tokyo-1",
        "key_file": "~/.oci/oci_api_key.pem",
        "key_file_exists": True,
        "config_file_exists": True,
        "config_source": "runtime",
        "config_error": None,
    }


def test_get_oci_settings_reports_missing_private_key_without_failing(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(tmp_path / ".oci" / "missing-config"))
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    monkeypatch.setattr(settings, "oci_region", "ap-osaka-1")

    resp = client.get("/api/settings/oci")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["user"] == ""
    assert body["fingerprint"] == ""
    assert body["tenancy"] == ""
    assert body["region"] == ""
    assert body["key_file"] == "~/.oci/oci_api_key.pem"
    assert body["key_file_exists"] is False
    assert body["config_file_exists"] is False


def test_update_oci_settings_creates_config_dir_and_file_with_private_permissions(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", "~/.oci/config")
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    monkeypatch.setattr(settings, "oci_region", "us-chicago-1")
    env_file = _platform_env_file(
        monkeypatch,
        tmp_path,
        "\n".join(
            [
                "# 既存設定",
                "PLATFORM_OCI_REGION=us-chicago-1",
                "",
            ]
        ),
    )

    resp = client.patch(
        "/api/settings/oci",
        json={
            "user": "ocid1.user.oc1..new",
            "fingerprint": "12:34:56:78:90:ab:cd:ef",
            "tenancy": "ocid1.tenancy.oc1..new",
            "region": "ap-osaka-1",
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    config_dir = tmp_path / ".oci"
    config_file = config_dir / "config"
    assert config_dir.is_dir()
    assert config_file.is_file()
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    content = config_file.read_text(encoding="utf-8")
    assert "[DEFAULT]" in content
    assert "user=ocid1.user.oc1..new" in content
    assert "fingerprint=12:34:56:78:90:ab:cd:ef" in content
    assert "tenancy=ocid1.tenancy.oc1..new" in content
    assert "region=ap-osaka-1" in content
    assert "key_file=~/.oci/oci_api_key.pem" in content
    assert settings.oci_region == "ap-osaka-1"
    assert body["config_file_exists"] is True
    assert body["key_file_exists"] is False
    persisted = env_file.read_text(encoding="utf-8")
    assert "# 既存設定" in persisted
    assert "PLATFORM_OCI_CONFIG_FILE=~/.oci/config" in persisted
    assert "PLATFORM_OCI_CONFIG_PROFILE=DEFAULT" in persisted
    assert "PLATFORM_OCI_REGION=ap-osaka-1" in persisted


def test_update_oci_settings_does_not_write_empty_config_defaults(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", "~/.oci/config")
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    monkeypatch.setattr(settings, "oci_region", "us-chicago-1")
    env_file = _platform_env_file(monkeypatch, tmp_path)

    resp = client.patch(
        "/api/settings/oci",
        json={
            "user": "",
            "fingerprint": "",
            "tenancy": "",
            "region": "",
        },
    )

    assert resp.status_code == 200
    content = (tmp_path / ".oci" / "config").read_text(encoding="utf-8")
    assert "user=" not in content
    assert "fingerprint=" not in content
    assert "tenancy=" not in content
    assert "region=" not in content
    assert "key_file=" not in content
    assert settings.oci_region == ""
    assert "PLATFORM_OCI_REGION" not in env_file.read_text(encoding="utf-8")


def test_update_oci_settings_preserves_existing_non_default_profile(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / ".oci"
    config_dir.mkdir()
    config_file = config_dir / "config"
    config_file.write_text(
        "\n".join(
            [
                "[DEFAULT]",
                "user=ocid1.user.oc1..old",
                "fingerprint=aa:bb:cc:dd",
                "[ADMIN_USER]",
                "user=ocid1.user.oc1..admin",
                "fingerprint=11:22:33:44",
                "tenancy=ocid1.tenancy.oc1..admin",
                "region=us-chicago-1",
                "key_file=keys/admin.pem",
            ]
        ),
        encoding="utf-8",
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(config_file))
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    _settings_env_file(monkeypatch, tmp_path)

    resp = client.patch(
        "/api/settings/oci",
        json={
            "user": "ocid1.user.oc1..new",
            "fingerprint": "12:34:56:78:90:ab:cd:ef",
            "tenancy": "ocid1.tenancy.oc1..new",
            "region": "ap-osaka-1",
        },
    )

    assert resp.status_code == 200
    content = config_file.read_text(encoding="utf-8")
    assert "[ADMIN_USER]" in content
    assert "user=ocid1.user.oc1..admin" in content
    assert "key_file=keys/admin.pem" in content
    assert "user=ocid1.user.oc1..new" in content


def test_update_oci_settings_does_not_mutate_runtime_when_env_write_fails(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", "~/.oci/config")
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    monkeypatch.setattr(settings, "oci_region", "us-chicago-1")
    monkeypatch.setattr(app_config, "PLATFORM_ENV_FILE", tmp_path)

    resp = client.patch(
        "/api/settings/oci",
        json={
            "user": "ocid1.user.oc1..new",
            "fingerprint": "12:34:56:78:90:ab:cd:ef",
            "tenancy": "ocid1.tenancy.oc1..new",
            "region": "ap-osaka-1",
        },
    )

    assert resp.status_code == 500
    assert settings.oci_region == "us-chicago-1"


def test_update_oci_object_storage_settings_persists_env_and_mutates_runtime(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "object_storage_region", "ap-osaka-1")
    monkeypatch.setattr(settings, "object_storage_namespace", "old-namespace")
    env_file = _platform_env_file(
        monkeypatch,
        tmp_path,
        "\n".join(
            [
                "# OCI Object Storage",
                "PLATFORM_OBJECT_STORAGE_REGION=ap-osaka-1",
                "PLATFORM_OBJECT_STORAGE_NAMESPACE=old-namespace",
                "PLATFORM_OBJECT_STORAGE_BUCKET=rag-originals",
                "",
            ]
        ),
    )

    resp = client.patch(
        "/api/settings/oci/object-storage",
        json={
            "object_storage_region": "us-chicago-1",
            "object_storage_namespace": "mytenancynamespace",
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["object_storage_region"] == "us-chicago-1"
    assert body["object_storage_namespace"] == "mytenancynamespace"
    assert settings.object_storage_region == "us-chicago-1"
    assert settings.object_storage_namespace == "mytenancynamespace"
    persisted = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_OBJECT_STORAGE_REGION=us-chicago-1" in persisted
    assert "PLATFORM_OBJECT_STORAGE_NAMESPACE=mytenancynamespace" in persisted
    assert "PLATFORM_OBJECT_STORAGE_BUCKET=rag-originals" in persisted


def test_update_oci_object_storage_settings_does_not_mutate_runtime_when_env_write_fails(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "object_storage_region", "ap-osaka-1")
    monkeypatch.setattr(settings, "object_storage_namespace", "old-namespace")
    monkeypatch.setattr(app_config, "PLATFORM_ENV_FILE", tmp_path)

    resp = client.patch(
        "/api/settings/oci/object-storage",
        json={
            "object_storage_region": "us-chicago-1",
            "object_storage_namespace": "mytenancynamespace",
        },
    )

    assert resp.status_code == 500
    assert settings.object_storage_region == "ap-osaka-1"
    assert settings.object_storage_namespace == "old-namespace"


def test_update_oci_object_storage_settings_rejects_invalid_values() -> None:
    resp = client.patch(
        "/api/settings/oci/object-storage",
        json={
            "object_storage_region": "US_CHICAGO_1",
            "object_storage_namespace": "invalid namespace",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["error_messages"]


def test_test_oci_config_reports_missing_private_key_after_save(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", "~/.oci/config")
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    _settings_env_file(monkeypatch, tmp_path)
    client.patch(
        "/api/settings/oci",
        json={
            "user": "ocid1.user.oc1..new",
            "fingerprint": "12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef",
            "tenancy": "ocid1.tenancy.oc1..new",
            "region": "ap-osaka-1",
        },
    )

    resp = client.post("/api/settings/oci/config/test")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "failed"
    assert body["config_file_exists"] is True
    assert body["key_file_exists"] is False
    assert body["missing_fields"] == []
    assert body["oci_directory_mode"] == "0700"
    assert body["config_file_mode"] == "0600"
    assert body["key_file_mode"] is None
    assert "秘密鍵ファイルが見つかりません" in body["message"]


def test_test_oci_config_succeeds_with_private_key_and_permissions(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", "~/.oci/config")
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    _settings_env_file(monkeypatch, tmp_path)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.md5(public_der, usedforsecurity=False).hexdigest()
    fingerprint = ":".join(digest[index : index + 2] for index in range(0, 32, 2))
    client.patch(
        "/api/settings/oci",
        json={
            "user": "ocid1.user.oc1..new",
            "fingerprint": fingerprint,
            "tenancy": "ocid1.tenancy.oc1..new",
            "region": "ap-osaka-1",
        },
    )
    key_file = tmp_path / ".oci" / "oci_api_key.pem"
    key_file.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    namespace_calls: list[str] = []
    monkeypatch.setattr(
        oci_connectivity,
        "_get_object_storage_namespace",
        lambda config: namespace_calls.append(str(config["region"])),
    )

    resp = client.post("/api/settings/oci/config/test")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "success"
    assert body["key_file_exists"] is True
    assert body["permission_issues"] == []
    assert body["key_file_mode"] == "0600"
    assert [stage["status"] for stage in body["stages"]] == ["success"] * 4
    assert namespace_calls == ["ap-osaka-1"]
    assert fingerprint not in resp.text


def test_test_oci_config_reports_encrypted_private_key_without_pass_phrase(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", "~/.oci/config")
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")
    _settings_env_file(monkeypatch, tmp_path)
    client.patch(
        "/api/settings/oci",
        json={
            "user": "ocid1.user.oc1..new",
            "fingerprint": "12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef",
            "tenancy": "ocid1.tenancy.oc1..new",
            "region": "ap-osaka-1",
        },
    )
    key_file = tmp_path / ".oci" / "oci_api_key.pem"
    key_file.write_text(
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n-----END ENCRYPTED PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    key_file.chmod(0o600)

    resp = client.post("/api/settings/oci/config/test")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "failed"
    assert body["key_file_exists"] is True
    assert body["error_type"] == "OciPrivateKeyPassPhraseRequiredError"
    assert "暗号化されています" in body["message"]


def test_read_oci_config_rejects_missing_requested_profile(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config"
    config_file.write_text(
        "[DEFAULT]\nuser=ocid1.user.oc1..default\n",
        encoding="utf-8",
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(config_file))
    monkeypatch.setattr(settings, "oci_config_profile", "RAG_PROD")

    resp = client.post(
        "/api/settings/oci/config/read",
        json={"config_file": str(config_file), "profile": "RAG_PROD"},
    )

    assert resp.status_code == 404
    assert "指定した OCI config profile が見つかりません。" in resp.text


def test_read_object_storage_namespace_uses_oci_sdk(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class FakeObjectStorageClient:
        def __init__(self, config: dict[str, Any]) -> None:
            captured["config"] = config

        def get_namespace(self) -> object:
            captured["get_namespace_called"] = True
            return SimpleNamespace(data="mytenancynamespace")

    def fake_from_file(path: str, profile: str) -> dict[str, Any]:
        captured["config_path"] = path
        captured["profile"] = profile
        return {"region": "ap-tokyo-1"}

    def fake_import_module(name: str) -> object:
        if name == "oci.config":
            return SimpleNamespace(from_file=fake_from_file)
        if name == "oci.object_storage":
            return SimpleNamespace(ObjectStorageClient=FakeObjectStorageClient)
        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr("pr_system_settings.oci.importlib.import_module", fake_import_module)
    config_file = tmp_path / "config"
    # 取得に使う config は実行中の設定のもの（要求の config_file は使わない。#1067）。
    settings = get_settings()
    monkeypatch.setattr(settings, "oci_config_file", str(config_file))
    monkeypatch.setattr(settings, "oci_config_profile", "DEFAULT")

    resp = client.post(
        "/api/settings/oci/object-storage/namespace",
        json={
            "config_file": str(tmp_path / "other"),
            "profile": "OTHER",
            "region": "ap-osaka-1",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["data"] == {"namespace": "mytenancynamespace"}
    assert captured["config_path"] == str(config_file)
    assert captured["profile"] == "DEFAULT"
    assert captured["config"] == {"region": "ap-osaka-1"}
    assert captured["get_namespace_called"] is True


def test_read_object_storage_namespace_reports_oci_errors(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    def fake_import_module(name: str) -> object:
        if name == "oci.config":
            return SimpleNamespace(from_file=lambda path, profile: {"region": "ap-tokyo-1"})
        if name == "oci.object_storage":
            raise RuntimeError("sdk unavailable")
        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr("pr_system_settings.oci.importlib.import_module", fake_import_module)

    resp = client.post(
        "/api/settings/oci/object-storage/namespace",
        json={
            "config_file": str(tmp_path / "config"),
            "profile": "DEFAULT",
            "region": "ap-osaka-1",
        },
    )

    assert resp.status_code == 502
    assert "namespace を取得できませんでした" in resp.text


def test_read_object_storage_namespace_refuses_encrypted_private_key_without_prompt(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "encrypted.pem"
    key_file.write_text(
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n-----END ENCRYPTED PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    initialized = False

    class FakeObjectStorageClient:
        def __init__(self, config: dict[str, Any]) -> None:
            nonlocal initialized
            initialized = True

        def get_namespace(self) -> object:
            return SimpleNamespace(data="mytenancynamespace")

    def fake_import_module(name: str) -> object:
        if name == "oci.config":
            return SimpleNamespace(
                from_file=lambda path, profile: {
                    "key_file": str(key_file),
                    "region": "ap-tokyo-1",
                }
            )
        if name == "oci.object_storage":
            return SimpleNamespace(ObjectStorageClient=FakeObjectStorageClient)
        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr("pr_system_settings.oci.importlib.import_module", fake_import_module)

    resp = client.post(
        "/api/settings/oci/object-storage/namespace",
        json={
            "config_file": str(tmp_path / "config"),
            "profile": "DEFAULT",
            "region": "ap-osaka-1",
        },
    )

    assert resp.status_code == 502
    assert "暗号化されています" in resp.text
    assert initialized is False


def test_upload_oci_private_key_overwrites_fixed_path(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    pem = b"-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"

    resp = client.post(
        "/api/settings/oci/key-file",
        files={"file": ("new-key.pem", pem, "application/x-pem-file")},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body == {"key_file": "~/.oci/oci_api_key.pem", "saved": True}
    target = tmp_path / ".oci" / "oci_api_key.pem"
    assert target.read_bytes() == pem
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "PRIVATE KEY" not in resp.text


def test_upload_oci_private_key_rejects_invalid_content(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    resp = client.post(
        "/api/settings/oci/key-file",
        files={"file": ("new-key.pem", b"not a pem", "application/x-pem-file")},
    )

    assert resp.status_code == 400
    assert not (tmp_path / ".oci" / "oci_api_key.pem").exists()


def test_upload_oci_private_key_rejects_encrypted_content(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    pem = b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n-----END ENCRYPTED PRIVATE KEY-----\n"

    resp = client.post(
        "/api/settings/oci/key-file",
        files={"file": ("encrypted.pem", pem, "application/x-pem-file")},
    )

    assert resp.status_code == 400
    assert "pass phrase" in resp.text
    assert not (tmp_path / ".oci" / "oci_api_key.pem").exists()


def test_get_database_settings_masks_secrets(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "rag_app")
    monkeypatch.setattr(settings, "oracle_password", "super-secret-password")
    monkeypatch.setattr(settings, "oracle_dsn", "adb.example.com/rag")
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_password", "wallet-secret")

    resp = client.get("/api/settings/database")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["user"] == "rag_app"
    assert body["dsn"] == "adb.example.com/rag"
    assert body["wallet_dir"] == settings.resolved_oracle_wallet_dir
    assert body["has_password"] is True
    assert body["has_wallet_password"] is True
    assert body["wallet_uploaded"] is False
    assert body["available_services"] == []
    # #108 から readiness は NL2SQL と同じく Wallet のファイルも確認する。
    assert body["readiness"] == "wallet_not_found"
    assert body["vector_column"] == "VECTOR(1536, FLOAT32)"
    assert "super-secret-password" not in resp.text
    assert "wallet-secret" not in resp.text


def test_update_database_settings_mutates_runtime_without_echoing_secret(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "old_user")
    monkeypatch.setattr(settings, "oracle_password", "old-secret")
    monkeypatch.setattr(settings, "oracle_dsn", "old-dsn")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", "/opt/oracle/instantclient_23_26")
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_password", "")
    env_file = _database_env_file(
        monkeypatch,
        tmp_path,
        "\n".join(
            [
                "# 既存設定",
                "PLATFORM_ORACLE_USER=old_user",
                "PLATFORM_ORACLE_DSN=old-dsn",
                "PLATFORM_ORACLE_USER=duplicate",
                "",
            ]
        ),
    )

    # 別の Wallet の保存先は受け取らない（Wallet の設置がその場所を置き換えるため）。
    rejected = client.patch(
        "/api/settings/database",
        json={
            "user": "rag_app",
            "dsn": "adb.example.com/rag",
            "wallet_dir": "/opt/oracle/wallet",
        },
    )
    assert rejected.status_code == 422
    assert "PLATFORM_ORACLE_WALLET_DIR" in rejected.text

    # 画面は今の保存先（resolved_oracle_wallet_dir）を送る。
    resp = client.patch(
        "/api/settings/database",
        json={
            "user": "rag_app",
            "dsn": "adb.example.com/rag",
            "wallet_dir": "/opt/oracle/instantclient_23_26/network/admin",
        },
    )

    assert resp.status_code == 200
    assert settings.oracle_user == "rag_app"
    assert settings.oracle_dsn == "adb.example.com/rag"
    # RAG が実際に使う保存先は PLATFORM_ORACLE_CLIENT_LIB_DIR/network/admin のまま変わらない。
    assert settings.oracle_wallet_dir == "/opt/oracle/instantclient_23_26/network/admin"
    assert settings.resolved_oracle_wallet_dir == "/opt/oracle/instantclient_23_26/network/admin"
    assert settings.oracle_password == "old-secret"
    assert resp.json()["data"]["wallet_dir"] == settings.resolved_oracle_wallet_dir
    assert resp.json()["data"]["has_password"] is True
    assert "old-secret" not in resp.text
    persisted = env_file.read_text(encoding="utf-8")
    assert "# 既存設定" in persisted
    assert persisted.count("PLATFORM_ORACLE_USER=") == 1
    assert "PLATFORM_ORACLE_USER=rag_app" in persisted
    assert "PLATFORM_ORACLE_PASSWORD=old-secret" in persisted
    assert "PLATFORM_ORACLE_DSN=adb.example.com/rag" in persisted
    assert "PLATFORM_ORACLE_CLIENT_LIB_DIR=/opt/oracle/instantclient_23_26" in persisted
    assert "PLATFORM_ORACLE_WALLET_PASSWORD=" in persisted


def test_update_database_settings_does_not_mutate_runtime_when_env_write_fails(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "old_user")
    monkeypatch.setattr(settings, "oracle_password", "old-secret")
    monkeypatch.setattr(settings, "oracle_dsn", "old-dsn")
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_password", "old-wallet-secret")
    monkeypatch.setattr(app_config, "PLATFORM_ENV_FILE", tmp_path)

    resp = client.patch(
        "/api/settings/database",
        json={
            "user": "rag_app",
            "dsn": "adb.example.com/rag",
            "wallet_dir": "",
            "password": "new-secret",
            "wallet_password": "new-wallet-secret",
        },
    )

    assert resp.status_code == 500
    assert settings.oracle_user == "old_user"
    assert settings.oracle_password == "old-secret"
    assert settings.oracle_dsn == "old-dsn"
    assert settings.oracle_wallet_password == "old-wallet-secret"


def test_update_database_settings_clears_saved_secrets(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "rag_app")
    monkeypatch.setattr(settings, "oracle_password", "old-secret")
    monkeypatch.setattr(settings, "oracle_dsn", "ragdb_high")
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_password", "old-wallet-secret")
    env_file = _database_env_file(
        monkeypatch,
        tmp_path,
        "\n".join(
            [
                "PLATFORM_ORACLE_USER=rag_app",
                "PLATFORM_ORACLE_PASSWORD=old-secret",
                "PLATFORM_ORACLE_DSN=ragdb_high",
                "PLATFORM_ORACLE_WALLET_PASSWORD=old-wallet-secret",
                "",
            ]
        ),
    )

    resp = client.patch(
        "/api/settings/database",
        json={
            "user": "rag_app",
            "dsn": "ragdb_high",
            "wallet_dir": settings.resolved_oracle_wallet_dir,
            "clear_password": True,
            "clear_wallet_password": True,
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["has_password"] is False
    assert body["has_wallet_password"] is False
    assert settings.oracle_password == ""
    assert settings.oracle_wallet_password == ""
    persisted = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_ORACLE_PASSWORD=" in persisted
    assert "PLATFORM_ORACLE_PASSWORD=old-secret" not in persisted
    assert "PLATFORM_ORACLE_WALLET_PASSWORD=" in persisted
    assert "PLATFORM_ORACLE_WALLET_PASSWORD=old-wallet-secret" not in persisted


def test_database_connection_test_uses_candidate_without_mutating_runtime(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "")
    monkeypatch.setattr(settings, "oracle_password", "")
    monkeypatch.setattr(settings, "oracle_dsn", "")
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_password", "")

    async def fake_test_oracle_connection(candidate: Settings) -> None:
        assert candidate.oracle_user == "rag_app"
        assert candidate.oracle_password == "candidate-secret"
        assert candidate.oracle_dsn == "adb.example.com/rag"

    _write_thick_wallet(Path(settings.resolved_oracle_wallet_dir))
    monkeypatch.setattr(settings_routes, "test_oracle_connection", fake_test_oracle_connection)

    resp = client.post(
        "/api/settings/database/test",
        json={
            "user": "rag_app",
            "dsn": "adb.example.com/rag",
            "wallet_dir": "",
            "password": "candidate-secret",
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "success"
    assert body["readiness"] == "ok"
    assert body["elapsed_ms"] >= 0
    assert body["details"]["timeout_seconds"] == settings.oracle_db_test_timeout_seconds
    assert settings.oracle_user == ""
    assert settings.oracle_password == ""
    assert "candidate-secret" not in resp.text


def test_database_connection_test_returns_wallet_password_guidance(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "")
    monkeypatch.setattr(settings, "oracle_password", "")
    monkeypatch.setattr(settings, "oracle_dsn", "")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", str(tmp_path / "instantclient_23_26"))
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_password", "")
    _write_thick_wallet(Path(settings.resolved_oracle_wallet_dir))

    async def fake_test_oracle_connection(candidate: Settings) -> None:
        raise OracleWalletPasswordRequiredError("Wallet パスワードを入力してください。")

    monkeypatch.setattr(settings_routes, "test_oracle_connection", fake_test_oracle_connection)

    resp = client.post(
        "/api/settings/database/test",
        json={
            "user": "rag_app",
            "dsn": "ragdb_high",
            "wallet_dir": "",
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "failed"
    assert body["message"] == "Wallet パスワードを入力してください。"
    assert body["error_type"] == "OracleWalletPasswordRequiredError"
    assert body["elapsed_ms"] >= 0
    assert body["troubleshooting"]


def test_database_connection_test_returns_timeout_guidance(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "")
    monkeypatch.setattr(settings, "oracle_password", "")
    monkeypatch.setattr(settings, "oracle_dsn", "")
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_password", "")

    async def fake_test_oracle_connection(candidate: Settings) -> None:
        raise OracleConnectionTimeoutError(
            "Oracle AI Database の接続テストが 15 秒でタイムアウトしました。"
        )

    _write_thick_wallet(Path(settings.resolved_oracle_wallet_dir))
    monkeypatch.setattr(settings_routes, "test_oracle_connection", fake_test_oracle_connection)

    resp = client.post(
        "/api/settings/database/test",
        json={
            "user": "rag_app",
            "dsn": "ragdb_high",
            "wallet_dir": "",
            "password": "candidate-secret",
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "failed"
    assert body["error_type"] == "OracleConnectionTimeoutError"
    assert "タイムアウト" in body["message"]
    assert any("TCPS 1522" in tip for tip in body["troubleshooting"])
    assert (
        body["details"]["tcp_connect_timeout_seconds"]
        == settings.oracle_tcp_connect_timeout_seconds
    )


def test_database_connection_test_classifies_oracle_operational_error(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "")
    monkeypatch.setattr(settings, "oracle_password", "")
    monkeypatch.setattr(settings, "oracle_dsn", "")
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_password", "")

    class FakeOperationalError(Exception):
        """python-oracledb OperationalError 相当のテスト用例外。"""

    async def fake_test_oracle_connection(candidate: Settings) -> None:
        raise FakeOperationalError(
            "ORA-01017: invalid username/password; logon denied candidate-secret"
        )

    _write_thick_wallet(Path(settings.resolved_oracle_wallet_dir))
    monkeypatch.setattr(settings_routes, "test_oracle_connection", fake_test_oracle_connection)

    resp = client.post(
        "/api/settings/database/test",
        json={
            "user": "rag_app",
            "dsn": "ragdb_high",
            "wallet_dir": "",
            "password": "candidate-secret",
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "failed"
    assert body["error_type"] == "FakeOperationalError"
    assert "ORA-01017" in body["message"]
    assert "ユーザー名または DB パスワード" in body["message"]
    assert body["details"]["oracle_error_codes"] == "ORA-01017"
    assert any("DB パスワード" in tip for tip in body["troubleshooting"])
    assert "candidate-secret" not in resp.text


def test_database_connection_test_classifies_adb_acl_rejection(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "")
    monkeypatch.setattr(settings, "oracle_password", "")
    monkeypatch.setattr(settings, "oracle_dsn", "")
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    monkeypatch.setattr(settings, "oracle_wallet_password", "")

    class FakeOperationalError(Exception):
        """python-oracledb OperationalError 相当のテスト用例外。"""

    async def fake_test_oracle_connection(candidate: Settings) -> None:
        raise FakeOperationalError(
            "DPY-6005: cannot connect to database. DPY-6000: listener refused. "
            "ORA-12506: listener rejected connection based on service ACL filtering"
        )

    _write_thick_wallet(Path(settings.resolved_oracle_wallet_dir))
    monkeypatch.setattr(settings_routes, "test_oracle_connection", fake_test_oracle_connection)

    resp = client.post(
        "/api/settings/database/test",
        json={
            "user": "rag_app",
            "dsn": "ragdb_high",
            "wallet_dir": "",
            "password": "candidate-secret",
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert "ORA-12506" in body["message"]
    assert "アクセス制御リスト" in body["message"]
    assert body["details"]["oracle_error_codes"] == "DPY-6005, DPY-6000, ORA-12506"
    assert any("Network Access / ACL" in tip for tip in body["troubleshooting"])


def test_upload_database_wallet_zip_updates_runtime(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "oracle_user", "rag_app")
    monkeypatch.setattr(settings, "oracle_password", "")
    monkeypatch.setattr(settings, "oracle_dsn", "ragdb_high")
    monkeypatch.setattr(settings, "oracle_wallet_dir", "")
    wallet_dir = Path(settings.resolved_oracle_wallet_dir)
    wallet_dir.mkdir(parents=True)
    (wallet_dir / "old-wallet-file").write_text("old", encoding="utf-8")

    resp = client.post(
        "/api/settings/database/wallet",
        files={"file": ("Wallet_RAGDB.zip", _wallet_zip(), "application/zip")},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    wallet_dir = Path(settings.oracle_wallet_dir)
    assert wallet_dir.is_dir()
    assert (wallet_dir / "tnsnames.ora").read_text(encoding="utf-8") == "ragdb_high = ..."
    assert (wallet_dir / "ewallet.pem").is_file()
    assert not (wallet_dir / "ewallet.p12").exists()
    assert not (wallet_dir / "keystore.jks").exists()
    assert not (wallet_dir / "old-wallet-file").exists()
    assert body["wallet_dir"] == str(wallet_dir)
    assert body["wallet_uploaded"] is True
    assert body["available_services"] == ["ragdb_high"]
    assert body["readiness"] == "ok"
    assert "ewallet-secret" not in resp.text


def test_get_database_settings_extracts_available_services_from_wallet_dir(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    wallet_dir = Path(settings.resolved_oracle_wallet_dir)
    wallet_dir.mkdir(parents=True)
    (wallet_dir / "ewallet.p12").write_text("legacy-password-wallet", encoding="utf-8")
    (wallet_dir / "keystore.jks").write_text("legacy-java-keystore", encoding="utf-8")
    (wallet_dir / "tnsnames.ora").write_text(
        "\n".join(
            [
                "ragdb_high = (DESCRIPTION = ...)",
                "  (ADDRESS = (PROTOCOL = tcps)(HOST = example.oraclecloud.com)(PORT = 1522))",
                "ragdb_low = (DESCRIPTION = ...)",
                "ragdb_high = (DESCRIPTION = duplicate)",
            ]
        ),
        encoding="utf-8",
    )
    _write_thick_wallet(wallet_dir)

    resp = client.get("/api/settings/database")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["wallet_uploaded"] is True
    assert body["available_services"] == ["ragdb_high", "ragdb_low"]
    assert not (wallet_dir / "ewallet.p12").exists()
    assert not (wallet_dir / "keystore.jks").exists()


def test_upload_database_wallet_zip_rejects_unsafe_member_path(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    wallet_dir = Path(settings.resolved_oracle_wallet_dir)
    wallet_dir.mkdir(parents=True)
    sentinel = wallet_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    resp = client.post(
        "/api/settings/database/wallet",
        files={
            "file": (
                "Wallet_BAD.zip",
                _wallet_zip(
                    {
                        "../tnsnames.ora": "bad",
                        "sqlnet.ora": "...",
                        "cwallet.sso": "...",
                        "ewallet.pem": "...",
                    }
                ),
                "application/zip",
            )
        },
    )

    assert resp.status_code == 400
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (wallet_dir / "tnsnames.ora").exists()


def _write_thick_wallet(wallet_dir: Path, services: Sequence[str] = ("ragdb_high",)) -> None:
    """RAG の既定（Thick mode）で必要な Wallet ファイルを置く。

    #108 から接続テスト前の readiness が NL2SQL と同じく Wallet のファイルを確認する。
    """
    wallet_dir.mkdir(parents=True, exist_ok=True)
    tnsnames = wallet_dir / "tnsnames.ora"
    if not tnsnames.exists():
        tnsnames.write_text(
            "".join(f"{service} = (DESCRIPTION = ...)\n" for service in services),
            encoding="utf-8",
        )
    (wallet_dir / "sqlnet.ora").write_text("WALLET_LOCATION = ...\n", encoding="utf-8")
    (wallet_dir / "cwallet.sso").write_bytes(b"sso")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/settings/database/adb"),
        *(
            ("POST", f"/api/settings/database/adb/{action}")
            for action in ("settings", "start", "stop")
        ),
    ],
)
def test_adb_control_routes_are_removed(method: str, path: str) -> None:
    """ADB の情報取得・起動・停止の API は持たない（#10。起動・停止は docker 側の責務）。"""
    response = client.get(path) if method == "GET" else client.post(path)
    assert response.status_code in {404, 405}


def test_get_upload_storage_settings_returns_runtime_values(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_storage_backend", "local")
    monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "object_storage_region", "us-chicago-1")
    monkeypatch.setattr(settings, "object_storage_namespace", "example-namespace")
    monkeypatch.setattr(settings, "object_storage_bucket", "rag-originals")
    monkeypatch.setattr(settings, "max_upload_bytes", 12345)

    resp = client.get("/api/settings/upload-storage")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["backend"] == "local"
    assert body["local_storage_dir"] == str(tmp_path / "uploads")
    assert body["object_storage_region"] == "us-chicago-1"
    assert body["object_storage_namespace"] == "example-namespace"
    assert body["object_storage_bucket"] == "rag-originals"
    assert body["readiness"] == "ok"
    assert body["max_upload_bytes"] == 12345
    assert body["config_source"] == "runtime"


def test_update_upload_storage_settings_persists_env_and_mutates_runtime(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "object_storage_region", "us-chicago-1")
    monkeypatch.setattr(settings, "object_storage_namespace", "global-namespace")
    env_file = _upload_storage_env_file(
        monkeypatch,
        tmp_path,
        "\n".join(
            [
                "# 既存設定",
                "PLATFORM_UPLOAD_STORAGE_BACKEND=local",
                "PLATFORM_LOCAL_STORAGE_DIR=/old/uploads",
                "PLATFORM_UPLOAD_STORAGE_BACKEND=duplicate",
                "",
            ]
        ),
    )

    resp = client.patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/production-ready-rag",
            "object_storage_bucket": "rag-originals",
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["backend"] == "oci"
    assert body["readiness"] == "ok"
    assert settings.upload_storage_backend == "oci"
    assert settings.local_storage_dir == "/u01/data/production-ready-rag"
    assert settings.object_storage_namespace == "global-namespace"
    assert settings.object_storage_bucket == "rag-originals"
    persisted = env_file.read_text(encoding="utf-8")
    assert "# 既存設定" in persisted
    assert persisted.count("PLATFORM_UPLOAD_STORAGE_BACKEND=") == 1
    assert "PLATFORM_UPLOAD_STORAGE_BACKEND=oci" in persisted
    assert "PLATFORM_LOCAL_STORAGE_DIR=/u01/data/production-ready-rag" in persisted
    assert "PLATFORM_OBJECT_STORAGE_REGION=us-chicago-1" in persisted
    assert "PLATFORM_OBJECT_STORAGE_NAMESPACE=global-namespace" in persisted
    assert "PLATFORM_OBJECT_STORAGE_BUCKET=rag-originals" in persisted


def test_update_upload_storage_settings_can_apply_namespace_from_oci_settings_draft(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "object_storage_namespace", "")
    monkeypatch.setattr(settings, "object_storage_region", "us-chicago-1")
    env_file = _upload_storage_env_file(monkeypatch, tmp_path)

    resp = client.patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/production-ready-rag",
            "object_storage_namespace": "oci-page-namespace",
            "object_storage_bucket": "rag-originals",
        },
    )

    assert resp.status_code == 200
    assert settings.object_storage_namespace == "oci-page-namespace"
    assert settings.object_storage_bucket == "rag-originals"
    persisted = env_file.read_text(encoding="utf-8")
    assert "PLATFORM_OBJECT_STORAGE_NAMESPACE=oci-page-namespace" in persisted
    assert "PLATFORM_OBJECT_STORAGE_BUCKET=rag-originals" in persisted


def test_update_upload_storage_settings_does_not_mutate_runtime_when_env_write_fails(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_storage_backend", "local")
    monkeypatch.setattr(settings, "local_storage_dir", "/old/uploads")
    monkeypatch.setattr(settings, "object_storage_namespace", "global-namespace")
    monkeypatch.setattr(settings, "object_storage_bucket", "old-bucket")
    monkeypatch.setattr(settings, "object_storage_region", "us-chicago-1")
    monkeypatch.setattr(app_config, "PLATFORM_ENV_FILE", tmp_path)

    resp = client.patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/production-ready-rag",
            "object_storage_bucket": "rag-originals",
        },
    )

    assert resp.status_code == 500
    assert settings.upload_storage_backend == "local"
    assert settings.local_storage_dir == "/old/uploads"
    assert settings.object_storage_namespace == "global-namespace"
    assert settings.object_storage_bucket == "old-bucket"


def test_update_upload_storage_settings_rejects_missing_selected_backend_fields(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OCI の必須項目が欠けていれば保存しない（#97 で NL2SQL の仕様に統一）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_storage_backend", "local")
    monkeypatch.setattr(settings, "object_storage_namespace", "global-namespace")
    monkeypatch.setattr(settings, "object_storage_region", "us-chicago-1")
    env_file = _upload_storage_env_file(monkeypatch, tmp_path)
    before = env_file.read_text(encoding="utf-8") if env_file.exists() else None

    resp = client.patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/production-ready-rag",
            "object_storage_bucket": "",
        },
    )

    assert resp.status_code == 422
    assert settings.upload_storage_backend == "local"
    after = env_file.read_text(encoding="utf-8") if env_file.exists() else None
    assert after == before


def test_update_upload_storage_settings_rejects_missing_global_namespace(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """namespace が未設定のまま OCI を選ぶと保存しない（#97 で NL2SQL の仕様に統一）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_storage_backend", "local")
    monkeypatch.setattr(settings, "object_storage_namespace", "")
    monkeypatch.setattr(settings, "object_storage_region", "us-chicago-1")
    _upload_storage_env_file(monkeypatch, tmp_path)

    resp = client.patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/production-ready-rag",
            "object_storage_bucket": "rag-originals",
        },
    )

    assert resp.status_code == 422
    assert "Object Storage namespace" in str(resp.json())
    assert settings.upload_storage_backend == "local"


def _database_env_file(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    content: str = "",
) -> Path:
    return _platform_env_file(monkeypatch, tmp_path, content)


def _upload_storage_env_file(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    content: str = "",
) -> Path:
    return _platform_env_file(monkeypatch, tmp_path, content)


def _platform_env_file(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    content: str = "",
) -> Path:
    """共通 .env（OCI / アップロード保存先 / データベースの保存先。#211）を tmp に置く。"""
    env_file = tmp_path / "platform.env"
    if content:
        env_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(app_config, "PLATFORM_ENV_FILE", env_file)
    return env_file


def _settings_env_file(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    content: str = "",
) -> Path:
    env_file = tmp_path / ".env"
    if content:
        env_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(settings_routes, "BACKEND_ENV_FILE", env_file)
    return env_file


def _payload() -> dict[str, Any]:
    return {
        "enterprise_ai": {
            "endpoint": "https://enterprise-ai.example",
            "project_ocid": "ocid1.generativeaiproject.oc1..example",
            "api_key": "sk-update-secret",
            "has_api_key": False,
            "clear_api_key": False,
            "models": [
                {
                    "model_id": "enterprise-llm",
                    "display_name": "標準 LLM",
                    "vision_enabled": False,
                    "max_output_tokens": 16000,
                },
                {
                    "model_id": "enterprise-vlm",
                    "display_name": "Vision LLM",
                    "vision_enabled": True,
                    "max_output_tokens": 64000,
                },
            ],
            "default_text_model_id": "enterprise-llm",
            "default_vision_model_id": "enterprise-vlm",
            "api_path": "/responses",
            "vlm_input_mode": "files_api",
            "text_payload_template": LLM_TEMPLATE,
            "vision_payload_template": VLM_TEMPLATE,
            "text_response_path": "/data/text",
            "vision_response_path": "/data/document",
            "timeout_seconds": 60.0,
            "max_retries": 2,
        },
        "generative_ai": {
            "embedding_model": "cohere.embed-v4.0",
            "embedding_dim": 1536,
            "rerank_model": "cohere.rerank-v4.0-fast",
        },
    }


def test_get_huggingface_settings_masks_token(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "huggingface_endpoint", "https://hf-mirror.com")
    monkeypatch.setattr(settings, "huggingface_token", "hf_secret")

    body = client.get("/api/settings/huggingface").json()["data"]

    assert "download_dir" not in body
    assert body["endpoint"] == "https://hf-mirror.com"
    assert body["token_configured"] is True
    # token 実値はレスポンスに含めない。
    assert "token" not in body
    assert body["config_source"] == "runtime"


def test_update_huggingface_settings_persists_env_and_mutates_runtime(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "huggingface_token", "")
    env_file = _settings_env_file(monkeypatch, tmp_path)

    resp = client.patch(
        "/api/settings/huggingface",
        json={
            "endpoint": "https://hf-mirror.com",
            "token": "hf_new",
        },
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["endpoint"] == "https://hf-mirror.com"
    assert body["token_configured"] is True
    assert settings.huggingface_token == "hf_new"
    env_text = env_file.read_text(encoding="utf-8")
    assert "HUGGINGFACE_DOWNLOAD_DIR" not in env_text
    assert "RAG_HUGGINGFACE_TOKEN=hf_new" in env_text
    assert "RAG_HUGGINGFACE_ENDPOINT=https://hf-mirror.com" in env_text
    # huggingface_hub の標準名はコンテナへ渡すときだけ使い、.env には書かない（#211）。
    assert "\nHF_TOKEN=" not in f"\n{env_text}"


def test_update_huggingface_settings_keeps_token_when_blank(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "huggingface_token", "hf_existing")
    _settings_env_file(monkeypatch, tmp_path)

    # token 未指定なら既存値を保持し、clear_token で削除する。
    resp = client.patch(
        "/api/settings/huggingface",
        json={"endpoint": ""},
    )
    assert resp.status_code == 200
    assert settings.huggingface_token == "hf_existing"

    resp = client.patch(
        "/api/settings/huggingface",
        json={
            "endpoint": "",
            "clear_token": True,
        },
    )
    assert resp.status_code == 200
    assert settings.huggingface_token == ""
    assert resp.json()["data"]["token_configured"] is False


def test_update_huggingface_settings_normalizes_runtime_values(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """保存した値は再起動を待たず、起動時の読み込みと同じ正規化で実行中の設定に入る（#1018）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "huggingface_endpoint", "")
    monkeypatch.setattr(settings, "huggingface_token", "hf_existing")
    env_file = _settings_env_file(monkeypatch, tmp_path)

    resp = client.patch(
        "/api/settings/huggingface",
        json={"endpoint": "hf-mirror.com", "token": "  hf_new  "},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["endpoint"] == "https://hf-mirror.com"
    assert settings.huggingface_endpoint == "https://hf-mirror.com"
    assert settings.huggingface_token == "hf_new"
    # サービスの起動 / 再起動で parser へ渡す値も scheme 付き。
    assert service_runtime_env(settings)["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert service_runtime_env(settings)["HF_TOKEN"] == "hf_new"
    env_text = env_file.read_text(encoding="utf-8")
    assert "RAG_HUGGINGFACE_ENDPOINT=https://hf-mirror.com" in env_text
    assert "RAG_HUGGINGFACE_TOKEN=hf_new" in env_text

    # 空白だけの token は未入力として扱い、保存済みの token を保持する。
    resp = client.patch(
        "/api/settings/huggingface",
        json={"endpoint": "https://hf-mirror.com", "token": "   "},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["token_configured"] is True
    assert settings.huggingface_token == "hf_new"


def test_update_huggingface_settings_rejects_removed_download_dir() -> None:
    resp = client.patch(
        "/api/settings/huggingface",
        json={"download_dir": "/u01/models/huggingface", "endpoint": ""},
    )
    assert resp.status_code == 422


def _wallet_zip(entries: dict[str, str] | None = None) -> bytes:
    wallet_entries = entries or {
        "tnsnames.ora": "ragdb_high = ...",
        "sqlnet.ora": "WALLET_LOCATION = ...",
        "cwallet.sso": "ewallet-secret",
        "ewallet.pem": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
        "ewallet.p12": "password-wallet",
        "keystore.jks": "java-keystore",
    }
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, content in wallet_entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_update_answer_record_settings_persists_retention_and_purges(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回答記録の保持日数を .env へ保存し、期限切れの記録を削除する(0 は削除しない)。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_answer_record_retention_days", 90)
    env_file = _settings_env_file(monkeypatch, tmp_path)
    purged: list[int] = []

    class FakeOracleClient:
        async def purge_answer_records(self, retention_days: int) -> int:
            purged.append(retention_days)
            return 3

    monkeypatch.setattr(settings_routes, "OracleClient", FakeOracleClient)

    assert client.get("/api/settings/answer-records").json()["data"]["retention_days"] == 90

    resp = client.patch("/api/settings/answer-records", json={"retention_days": 30})
    assert resp.status_code == 200
    assert resp.json()["data"] == {"retention_days": 30, "config_source": "runtime"}
    assert settings.rag_answer_record_retention_days == 30
    assert "RAG_ANSWER_RECORD_RETENTION_DAYS=30" in env_file.read_text(encoding="utf-8")

    assert (
        client.patch("/api/settings/answer-records", json={"retention_days": 0}).status_code == 200
    )
    assert purged == [30]
    assert (
        client.patch("/api/settings/answer-records", json={"retention_days": -1}).status_code == 422
    )


def test_answering_settings_round_trip_to_env(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """回答の検索と生成の全体既定を .env と現在プロセスへ保存する(送った項目だけを変える。#593)。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_query_strategy", "auto_routing")
    monkeypatch.setattr(settings, "rag_answer_evidence_refinement_mode", "legacy")
    monkeypatch.setattr(settings, "rag_answer_flow", "crag")
    monkeypatch.setattr(settings, "rag_answer_audit_mode", "none")
    monkeypatch.setattr(settings, "rag_neighbor_child_count", 3)
    monkeypatch.setattr(settings, "rag_rerank_enabled", True)
    monkeypatch.setattr(settings, "rag_screen_linking_enabled", False)
    monkeypatch.setattr(settings, "rag_request_coverage_retrieval_enabled", True)
    monkeypatch.setattr(settings, "rag_auto_field_filter_enabled", False)
    monkeypatch.setattr(settings, "rag_graph_expansion_enabled", False)
    expansion_defaults = {
        "entity_expansion_enabled": True,
        "entity_expansion_max_chunks": 6,
        "section_expansion_enabled": True,
        "section_expansion_max_levels": 4,
        "section_expansion_input_token_percent": 50,
    }
    for name, value in expansion_defaults.items():
        monkeypatch.setattr(settings, f"rag_{name}", value)
        monkeypatch.delenv(f"RAG_{name.upper()}", raising=False)
    env_file = _settings_env_file(monkeypatch, tmp_path)

    assert client.get("/api/settings/answering").json()["data"] == {
        "query_strategy": "auto_routing",
        "answer_flow": "crag",
        "answer_audit_mode": "none",
        "answer_evidence_refinement_mode": "legacy",
        "neighbor_child_count": 3,
        "rerank_enabled": True,
        "screen_linking_enabled": False,
        "request_coverage_retrieval_enabled": True,
        "auto_field_filter_enabled": False,
        "graph_expansion_enabled": False,
        **expansion_defaults,
        "config_source": "runtime",
    }

    resp = client.patch(
        "/api/settings/answering",
        json={
            "query_strategy": "rag_fusion",
            "neighbor_child_count": 5,
            "rerank_enabled": False,
            "request_coverage_retrieval_enabled": False,
            "auto_field_filter_enabled": True,
            "graph_expansion_enabled": True,
            "entity_expansion_enabled": False,
            "entity_expansion_max_chunks": 9,
            "section_expansion_enabled": False,
            "section_expansion_max_levels": 3,
            "section_expansion_input_token_percent": 40,
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["query_strategy"] == "rag_fusion"
    assert data["answer_flow"] == "crag"
    assert data["neighbor_child_count"] == 5
    assert data["rerank_enabled"] is False
    assert settings.rag_query_strategy == "rag_fusion"
    assert settings.rag_neighbor_child_count == 5
    assert settings.rag_rerank_enabled is False
    env_text = env_file.read_text(encoding="utf-8")
    assert "RAG_QUERY_STRATEGY=rag_fusion" in env_text
    assert "RAG_ANSWER_FLOW=crag" in env_text
    assert "RAG_NEIGHBOR_CHILD_COUNT=5" in env_text
    assert "RAG_RERANK_ENABLED=false" in env_text
    assert "RAG_SCREEN_LINKING_ENABLED=false" in env_text
    # 質問から抽出項目の条件を読み取る(#652)。
    assert data["auto_field_filter_enabled"] is True
    assert settings.rag_auto_field_filter_enabled is True
    assert "RAG_AUTO_FIELD_FILTER_ENABLED=true" in env_text
    # 根拠の無い要求だけの再検索(#1279)。
    assert data["request_coverage_retrieval_enabled"] is False
    assert settings.rag_request_coverage_retrieval_enabled is False
    assert "RAG_REQUEST_COVERAGE_RETRIEVAL_ENABLED=false" in env_text
    # 関係情報で検索の候補を足す(#2055。既定 OFF)。
    assert data["graph_expansion_enabled"] is True
    assert settings.rag_graph_expansion_enabled is True
    assert "RAG_GRAPH_EXPANSION_ENABLED=true" in env_text

    reloaded = Settings(_env_file=env_file)
    for name, expected in {
        "entity_expansion_enabled": False,
        "entity_expansion_max_chunks": 9,
        "section_expansion_enabled": False,
        "section_expansion_max_levels": 3,
        "section_expansion_input_token_percent": 40,
    }.items():
        assert data[name] == expected
        assert getattr(settings, f"rag_{name}") == expected
        assert getattr(reloaded, f"rag_{name}") == expected


def test_answering_settings_rejects_invalid_values() -> None:
    """選択肢に無い値・範囲外の近傍数は 422(#593)。"""
    assert (
        client.patch("/api/settings/answering", json={"query_strategy": "unknown"}).status_code
        == 422
    )
    assert (
        client.patch("/api/settings/answering", json={"neighbor_child_count": 21}).status_code
        == 422
    )


@pytest.mark.parametrize("mode", ["none", "standard", "detailed"])
def test_answering_audit_mode_round_trip_and_independent_settings(
    monkeypatch: MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_answer_audit_mode", "none")
    monkeypatch.setattr(settings, "rag_answer_flow", "standard_rag")
    monkeypatch.setattr(settings, "rag_request_coverage_retrieval_enabled", False)
    monkeypatch.setattr(settings, "rag_answer_max_correction_rounds", 0)
    monkeypatch.delenv("RAG_ANSWER_AUDIT_MODE", raising=False)
    env_file = _settings_env_file(monkeypatch, tmp_path)
    response = client.patch("/api/settings/answering", json={"answer_audit_mode": mode})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["answer_audit_mode"] == mode
    assert data["answer_flow"] == "standard_rag"
    assert data["request_coverage_retrieval_enabled"] is False
    assert settings.rag_answer_max_correction_rounds == 0
    assert settings.rag_answer_audit_mode == mode
    assert f"RAG_ANSWER_AUDIT_MODE={mode}" in env_file.read_text()
    reloaded = Settings(_env_file=env_file)
    assert reloaded.rag_answer_audit_mode == mode
    from app.rag.answer_engine import build_engine_settings

    assert build_engine_settings(settings, output_dir=tmp_path).answer_audit_mode == mode
    assert build_engine_settings(reloaded, output_dir=tmp_path).answer_audit_mode == mode
    assert client.get("/api/settings/answering").json()["data"]["answer_audit_mode"] == mode
    # 別の項目だけの更新では、保存済みの確認範囲を変えない。
    unchanged = client.patch("/api/settings/answering", json={"neighbor_child_count": 2})
    assert unchanged.json()["data"]["answer_audit_mode"] == mode


def test_answering_audit_mode_rejects_unknown_value() -> None:
    assert (
        client.patch("/api/settings/answering", json={"answer_audit_mode": "automatic"}).status_code
        == 422
    )


class _MissingBucketError(Exception):
    """OCI SDK の ServiceError と同じ属性を持つ（HeadBucket の 404 BucketNotFound）。"""

    status = 404
    code = "BucketNotFound"
    request_id = "req-test"


class _MissingBucketStorage:
    def head_bucket(self, *_args: object, **_kwargs: object) -> None:
        raise _MissingBucketError("bucket not found")


def _stub_missing_bucket(monkeypatch: MonkeyPatch) -> None:
    from pr_system_settings import upload_storage_check

    monkeypatch.setattr(
        upload_storage_check,
        "default_object_storage_client",
        lambda _settings: _MissingBucketStorage(),
    )


def test_update_upload_storage_rejects_missing_bucket_without_saving(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """存在しない bucket は、保存せずに理由と直し方を返す（#2101）。"""
    settings = get_settings()
    env_file = _upload_storage_env_file(monkeypatch, tmp_path)
    _stub_missing_bucket(monkeypatch)
    before = settings.upload_storage_backend

    resp = client.patch(
        "/api/settings/upload-storage",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/check",
            "object_storage_region": "us-chicago-1",
            "object_storage_namespace": "examplenamespace",
            "object_storage_bucket": "missing-bucket",
        },
    )

    assert resp.status_code == 422
    body = resp.json()
    detail = json.dumps(body, ensure_ascii=False)  # 製品ごとのエラーの形に依らない
    assert "保存していません" in detail
    assert "404 BucketNotFound" in detail
    assert "missing-bucket" not in detail
    assert settings.upload_storage_backend == before
    assert not env_file.exists() or "missing-bucket" not in env_file.read_text(encoding="utf-8")


def test_upload_storage_check_reports_stages_without_saving(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """「接続を確認」は入力中の値で段階ごとの結果を返し、保存しない（#2101）。"""
    settings = get_settings()
    env_file = _upload_storage_env_file(monkeypatch, tmp_path)
    _stub_missing_bucket(monkeypatch)

    resp = client.post(
        "/api/settings/upload-storage/test",
        json={
            "backend": "oci",
            "local_storage_dir": "/u01/data/check",
            "object_storage_region": "us-chicago-1",
            "object_storage_namespace": "examplenamespace",
            "object_storage_bucket": "missing-bucket",
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert [stage["status"] for stage in data["stages"]] == [
        "success",
        "failed",
        "skipped",
        "skipped",
        "skipped",
    ]
    assert not env_file.exists() or "missing-bucket" not in env_file.read_text(encoding="utf-8")
    assert settings.object_storage_bucket != "missing-bucket"


@pytest.mark.parametrize("enabled", [True, False])
def test_entity_index_settings_persist_and_follow_pipeline_defaults(
    monkeypatch: MonkeyPatch, tmp_path: Path, enabled: bool
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_entity_index_enabled", not enabled)
    env_file = _settings_env_file(monkeypatch, tmp_path)
    response = client.patch("/api/settings/entity-index", json={"enabled": enabled})
    assert response.status_code == 200
    assert response.json()["data"] == {"enabled": enabled}
    assert settings.rag_entity_index_enabled is enabled
    assert f"RAG_ENTITY_INDEX_ENABLED={str(enabled).lower()}" in env_file.read_text()
    assert client.get("/api/settings/entity-index").json()["data"] == {"enabled": enabled}
    defaults = client.get("/api/settings/pipeline").json()["data"]["recipe_defaults"]
    assert defaults["entity_index_enabled"] is enabled


def test_entity_index_settings_require_an_explicit_value() -> None:
    assert client.patch("/api/settings/entity-index", json={}).status_code == 422


def test_entity_index_settings_keep_runtime_on_save_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_entity_index_enabled", False)

    def fail(*args: Any, **kwargs: Any) -> None:
        from fastapi import HTTPException

        raise HTTPException(status_code=500, detail="設定を保存できませんでした。")

    monkeypatch.setattr(settings_routes, "_write_env_values", fail)
    response = client.patch("/api/settings/entity-index", json={"enabled": True})
    assert response.status_code == 500
    assert settings.rag_entity_index_enabled is False


def test_model_settings_rerank_probe_has_explicit_purpose(monkeypatch: MonkeyPatch) -> None:
    class FakeGenAiClient:
        def __init__(self, settings: Settings) -> None:
            pass

        async def rerank(
            self, query: str, documents: list[str], *, top_n: int
        ) -> list[tuple[int, float]]:
            assert model_purpose_attributes() == {"pr.gen_ai.purpose": "connection.check"}
            assert query and documents and top_n == 1
            return [(0, 0.9)]

    monkeypatch.setattr(settings_routes, "OciGenAiClient", FakeGenAiClient)
    response = asyncio.run(
        settings_routes._run_model_settings_test(
            get_settings(),
            ModelSettingsTestRequest.model_validate(
                {"target_type": "rerank", "settings": _payload()}
            ),
        )
    )
    assert response == {"ranked_count": 1, "top_score": 0.9}
    assert model_purpose_attributes() == {}


@pytest.mark.parametrize("mode", ["legacy", "cited_only", "cited_required"])
def test_answering_evidence_refinement_mode_is_persisted_and_independent(
    monkeypatch: MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_answer_evidence_refinement_mode", "legacy")
    monkeypatch.setattr(settings, "rag_answer_audit_mode", "none")
    monkeypatch.setattr(settings, "rag_request_coverage_retrieval_enabled", False)
    monkeypatch.setattr(settings, "rag_answer_max_correction_rounds", 0)
    monkeypatch.delenv("RAG_ANSWER_EVIDENCE_REFINEMENT_MODE", raising=False)
    env_file = _settings_env_file(monkeypatch, tmp_path)
    response = client.patch(
        "/api/settings/answering", json={"answer_evidence_refinement_mode": mode}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["answer_evidence_refinement_mode"] == mode
    assert data["answer_audit_mode"] == "none"
    assert data["request_coverage_retrieval_enabled"] is False
    assert settings.rag_answer_max_correction_rounds == 0
    assert f"RAG_ANSWER_EVIDENCE_REFINEMENT_MODE={mode}" in env_file.read_text()
    reloaded = Settings(_env_file=env_file)
    assert reloaded.rag_answer_evidence_refinement_mode == mode
    from app.rag.answer_engine import build_engine_settings

    assert (
        build_engine_settings(settings, output_dir=tmp_path).answer_evidence_refinement_mode == mode
    )
    assert (
        build_engine_settings(reloaded, output_dir=tmp_path).answer_evidence_refinement_mode == mode
    )
    # 別の設定の保存で選別方式を暗黙に戻さない。
    changed = client.patch("/api/settings/answering", json={"neighbor_child_count": 2})
    assert changed.json()["data"]["answer_evidence_refinement_mode"] == mode
    assert (
        client.get("/api/settings/answering").json()["data"]["answer_evidence_refinement_mode"]
        == mode
    )


def test_answering_evidence_refinement_rejects_unknown_without_saving(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_answer_evidence_refinement_mode", "cited_required")
    env_file = _settings_env_file(monkeypatch, tmp_path)
    before = env_file.read_bytes() if env_file.exists() else None
    response = client.patch(
        "/api/settings/answering", json={"answer_evidence_refinement_mode": "automatic"}
    )
    assert response.status_code == 422
    assert settings.rag_answer_evidence_refinement_mode == "cited_required"
    assert (env_file.read_bytes() if env_file.exists() else None) == before


def test_answering_evidence_refinement_defaults_to_legacy(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("RAG_ANSWER_EVIDENCE_REFINEMENT_MODE", raising=False)
    assert Settings(_env_file=None).rag_answer_evidence_refinement_mode == "legacy"


def test_answering_evidence_refinement_changes_runtime_fingerprint() -> None:
    from app.rag.diagnostics import rag_config_fingerprint

    base = Settings(_env_file=None)
    fingerprints = {
        rag_config_fingerprint(
            base.model_copy(update={"rag_answer_evidence_refinement_mode": mode})
        )
        for mode in ("legacy", "cited_only", "cited_required")
    }
    assert len(fingerprints) == 3
