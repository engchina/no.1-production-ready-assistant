"""文書解析の設定の保存先 `parser-settings.json` と、共有の model-settings.json の分離（#1701）。

model-settings.json は3製品で共有する（#211）。NL2SQL / Agent が保存したファイルには RAG の
文書解析の設定が無い。RAG は文書解析の設定を自分の parser-settings.json だけで読み書きし、
共有のファイルの中身に左右されずに、設定の読み込み・system schema の CLI・backend を動かす。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pr_system_settings.model import ModelSettingsStore

from app.clients.oracle import OracleClient
from app.config import (
    MODEL_SETTINGS_STORE,
    PARSER_SETTINGS_STORE,
    Settings,
    get_settings,
)
from app.main import app
from app.rag import parser_adapter_readiness, system_schema_cli
from app.rag.system_schema import system_schema_manager
from app.schemas.settings import ModelSettingsPayload
from tests.support import AsgiTestClient

client = AsgiTestClient(app)

# 本番（OCI）で NL2SQL / Agent が保存した version 4 のファイルの形（RAG の設定が無い）。
V4_SHARED_DOCUMENT: dict[str, Any] = {
    "version": 4,
    "enterprise_ai": {
        "connections": [
            {
                "connection_id": "primary",
                "endpoint": "https://shared.example.com",
                "project_ocid": "ocid1.generativeaiproject.oc1..shared",
            }
        ],
        "models": [{"model_id": "shared-llm", "display_name": "共有 LLM"}],
        "default_text_model_id": "shared-llm",
    },
    "generative_ai": {
        "embedding_model": "cohere.embed-v4.0",
        "embedding_dim": 1536,
        "rerank_model": "cohere.rerank-v4.0-fast",
    },
}


@pytest.fixture(autouse=True)
def _no_package_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        parser_adapter_readiness, "_package_info", lambda *_args: (False, None, None)
    )


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _nl2sql_payload() -> ModelSettingsPayload:
    return ModelSettingsPayload.model_validate(
        {
            "enterprise_ai": {
                "endpoint": "https://nl2sql-saved.example.com",
                "project_ocid": "ocid1.generativeaiproject.oc1..nl2sql",
                "models": [{"model_id": "nl2sql-llm", "display_name": "NL2SQL LLM"}],
                "default_text_model_id": "nl2sql-llm",
            },
            "generative_ai": {
                "embedding_model": "cohere.embed-v4.0",
                "embedding_dim": 1536,
                "rerank_model": "cohere.rerank-v4.0-fast",
            },
        }
    )


def _nl2sql_save(model_file: Path) -> None:
    """NL2SQL / Agent の保存（同じ platform の store。RAG の設定を知らない）。"""
    store = ModelSettingsStore(
        resolve_path=lambda current: Path(current.model_settings_file),
        env_file=lambda current: MODEL_SETTINGS_STORE.env_file(current),
    )
    nl2sql_settings = Settings(_env_file=None, model_settings_file=str(model_file))
    with store.lock(nl2sql_settings):
        store.write_document(nl2sql_settings, _nl2sql_payload())


def test_v4_shared_file_from_other_product_loads_and_backend_serves() -> None:
    """NL2SQL / Agent が保存した version 4 のファイルで、設定の読み込みと backend が動く。"""
    settings = get_settings()
    before = settings.rag_parser_adapter_backend
    _write(Path(settings.model_settings_file), V4_SHARED_DOCUMENT)

    reloaded = get_settings()  # mtime の変化で読み直す

    assert reloaded.oci_enterprise_ai_endpoint == "https://shared.example.com"
    assert reloaded.rag_parser_adapter_backend == before
    response = client.get("/api/settings/parser-adapters")
    assert response.status_code == 200
    assert response.json()["data"]["adapter_backend"] == before


def test_rag_parser_save_then_nl2sql_save_then_rag_reads() -> None:
    """RAG が文書解析を保存 → NL2SQL がモデル設定を保存 → RAG は両方を読む。"""
    settings = get_settings()
    model_file = Path(settings.model_settings_file)
    saved = client.patch(
        "/api/settings/parser-adapters",
        json={
            "adapter_backend": "xml_tree_json",
            "xml_tree_json_enabled": True,
        },
    )
    assert saved.status_code == 200
    assert not model_file.exists()

    _nl2sql_save(model_file)
    settings.rag_parser_adapter_backend = "unstructured"  # 別 worker の古い値を模す
    PARSER_SETTINGS_STORE.reset()

    reloaded = get_settings()
    shared = json.loads(model_file.read_text(encoding="utf-8"))
    assert set(shared) == {"version", "enterprise_ai", "generative_ai"}
    assert reloaded.oci_enterprise_ai_endpoint == "https://nl2sql-saved.example.com"
    assert reloaded.rag_parser_adapter_backend == "xml_tree_json"
    assert reloaded.rag_parser_xml_tree_json_enabled is True


def test_parser_section_in_shared_file_is_not_read() -> None:
    """共有の model-settings.json の `parser_adapters` は読まない（読み替え・移行はしない）。"""
    settings = get_settings()
    before = settings.rag_parser_adapter_backend
    _write(
        Path(settings.model_settings_file),
        {**V4_SHARED_DOCUMENT, "parser_adapters": {"adapter_backend": "xml_tree_json"}},
    )

    assert get_settings().rag_parser_adapter_backend == before


class _FakeConnection:
    def close(self) -> None:
        return None


def _ready_status(_connection: Any) -> dict[str, Any]:
    return {"status": "ready", "operation_state": {"status": "idle"}}


def test_schema_cli_status_initialize_check_with_v4_shared_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI の status / initialize / check は、設定の読み込み（`OracleClient()`）で落ちない。"""
    settings = get_settings()
    _write(Path(settings.model_settings_file), V4_SHARED_DOCUMENT)
    monkeypatch.setattr(OracleClient, "acquire_connection", lambda self: _FakeConnection())
    monkeypatch.setattr(system_schema_manager, "_status_on", _ready_status)

    def initialize(**_kwargs: Any) -> dict[str, Any]:
        # 実際の初期化と同じく、接続（設定の読み込みを含む）を開いてから操作する。
        with system_schema_manager._connection_factory():
            return {"operation": "no_op"}

    monkeypatch.setattr(system_schema_manager, "initialize", initialize)

    assert system_schema_cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert system_schema_cli.main(["initialize"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "no_op"
    assert system_schema_cli.main(["check"]) == 0
    assert capsys.readouterr().out.strip() == "status=ready"


def test_schema_cli_reports_settings_failure_hint_without_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """設定の読み込みの失敗は、原因の手がかり（message の 1 行目と path）を出し、値は出さない。"""
    secret_value = "parser-top-secret-value"
    settings = get_settings()
    parser_file = PARSER_SETTINGS_STORE.path(settings)

    @contextmanager
    def broken_connection() -> Any:
        _write(parser_file, {"adapter_backend": secret_value})
        OracleClient()  # get_settings() が壊れたファイルを読み直して失敗する
        yield _FakeConnection()

    monkeypatch.setattr(system_schema_manager, "_connection_factory", broken_connection)

    try:
        assert system_schema_cli.main(["status"]) == 1
        error = json.loads(capsys.readouterr().err)
        assert error["error_code"] == "SCHEMA_OPERATION_FAILED"
        assert "文書解析の設定ファイルを読み込めません" in error["hint"]
        assert str(parser_file) in error["hint"]
        assert "adapter_backend" in error["hint"]
        assert secret_value not in json.dumps(error, ensure_ascii=False)

        assert system_schema_cli.main(["check"]) == 1
        summary = capsys.readouterr().out
        assert "文書解析の設定ファイルを読み込めません" in summary
        assert secret_value not in summary
    finally:
        # 壊れたファイルを消し、共有の Settings を次のテストのために読める状態へ戻す。
        parser_file.unlink(missing_ok=True)
        get_settings()
