"""全体の既定を画面で設定する API のテスト（#528）。

- 文書解析の「解析後の処理」: `GET/PATCH /api/settings/parser-adapters` の
  `vision_enabled` / `field_extraction_enabled` / `navigation_summary_enabled`
- 抽出項目の定義: `PATCH /api/settings/extraction-fields`
- 設定の概要: `GET/PATCH /api/settings/pipeline`（工程の自動進行と、レシピ 11 項目の全体の既定）

保存先の backend/.env と model-settings.json は conftest が tmp へ分離している。
"""

import json
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pytest import MonkeyPatch

from app import config as app_config
from app.api.routes import settings as settings_routes
from app.config import Settings, VectorIndexProfile, get_settings, reload_env_settings_if_changed
from app.main import app
from app.rag import extraction_field_adapter as fields_mod
from app.rag import parser_adapter_readiness
from tests.support import AsgiTestClient

client = AsgiTestClient(app)

POST_PARSE_ATTRS = (
    "rag_vision_enabled",
    "rag_field_extraction_enabled",
    "rag_navigation_summary_enabled",
    "rag_raptor_enabled",
)
AUTO_ADVANCE_ATTRS = (
    "rag_auto_parse_after_preprocess_enabled",
    "rag_auto_chunk_after_extract_enabled",
    "rag_auto_index_after_chunk_enabled",
)


@pytest.fixture
def settings(monkeypatch: MonkeyPatch) -> Settings:
    """テストで変える全体の既定を、テストの後に元へ戻す。"""
    current = get_settings()
    for name in (*POST_PARSE_ATTRS, *AUTO_ADVANCE_ATTRS):
        monkeypatch.setattr(current, name, getattr(current, name))
    monkeypatch.setattr(current, "rag_parser_adapter_backend", "docling")
    monkeypatch.setattr(current, "rag_parser_docling_enabled", True)
    # parser パッケージの有無は環境で変わるため、未導入に固定する。
    monkeypatch.setattr(
        parser_adapter_readiness, "_package_info", lambda *_args: (False, None, None)
    )
    return current


def _env_file() -> Path:
    return settings_routes.BACKEND_ENV_FILE


def test_parser_adapter_settings_report_post_parse_defaults(settings: Settings) -> None:
    settings.rag_vision_enabled = True
    settings.rag_field_extraction_enabled = False
    settings.rag_navigation_summary_enabled = True

    body = client.get("/api/settings/parser-adapters").json()["data"]

    assert body["vision_enabled"] is True
    assert body["field_extraction_enabled"] is False
    assert body["navigation_summary_enabled"] is True


def test_patch_post_parse_only_persists_env_and_keeps_parser_selection(
    settings: Settings,
) -> None:
    """「解析後の処理」だけの保存は .env に書き、解析エンジンの設定を変えない。

    解析エンジンの設定（parser-settings.json）には触れない。
    """
    settings.rag_vision_enabled = False
    settings.rag_field_extraction_enabled = False
    settings.rag_navigation_summary_enabled = False
    parser_settings_file = app_config.PARSER_SETTINGS_STORE.path(settings)

    resp = client.patch(
        "/api/settings/parser-adapters",
        json={"vision_enabled": True, "navigation_summary_enabled": True},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["vision_enabled"] is True
    assert body["field_extraction_enabled"] is False
    assert body["navigation_summary_enabled"] is True
    assert body["adapter_backend"] == "docling"
    assert settings.rag_vision_enabled is True
    assert settings.rag_field_extraction_enabled is False
    assert settings.rag_navigation_summary_enabled is True
    assert settings.rag_parser_adapter_backend == "docling"
    saved = dotenv_values(_env_file())
    assert saved["RAG_VISION_ENABLED"] == "true"
    assert saved["RAG_FIELD_EXTRACTION_ENABLED"] == "false"
    assert saved["RAG_NAVIGATION_SUMMARY_ENABLED"] == "true"
    # 解析エンジンの設定ファイルには触れない。
    assert not parser_settings_file.exists()


def test_patch_post_parse_keeps_unrelated_env_lines(settings: Settings) -> None:
    """既存の .env のコメントや無関係な値を保ったまま、該当の key だけを更新する。"""
    _env_file().write_text(
        "# 手で書いたコメント\nRAG_CHUNK_SIZE=900\nRAG_VISION_ENABLED=false\n",
        encoding="utf-8",
    )

    resp = client.patch("/api/settings/parser-adapters", json={"vision_enabled": True})

    assert resp.status_code == 200
    content = _env_file().read_text(encoding="utf-8")
    assert "# 手で書いたコメント" in content
    assert "RAG_CHUNK_SIZE=900" in content
    assert content.count("RAG_VISION_ENABLED=") == 1
    assert "RAG_VISION_ENABLED=true" in content


def test_patch_parser_selection_with_post_parse_saves_both(settings: Settings) -> None:
    resp = client.patch(
        "/api/settings/parser-adapters",
        json={"adapter_backend": "unstructured", "field_extraction_enabled": True},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["adapter_backend"] == "unstructured"
    assert body["field_extraction_enabled"] is True
    assert settings.rag_field_extraction_enabled is True
    parser_file = app_config.PARSER_SETTINGS_STORE.path(settings)
    persisted = json.loads(parser_file.read_text(encoding="utf-8"))
    assert persisted["adapter_backend"] == "unstructured"
    # 「解析後の処理」は parser-settings.json に書かない（保存先は backend/.env）。
    assert "field_extraction_enabled" not in persisted
    assert dotenv_values(_env_file())["RAG_FIELD_EXTRACTION_ENABLED"] == "true"


def test_patch_parser_selection_only_does_not_write_post_parse_env(settings: Settings) -> None:
    resp = client.patch("/api/settings/parser-adapters", json={"adapter_backend": "docling"})

    assert resp.status_code == 200
    assert not _env_file().exists() or "RAG_VISION_ENABLED" not in _env_file().read_text(
        encoding="utf-8"
    )


def test_patch_post_parse_write_failure_keeps_runtime(
    settings: Settings, monkeypatch: MonkeyPatch
) -> None:
    settings.rag_vision_enabled = False

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(settings_routes, "_replace_env_file", fail)

    resp = client.patch("/api/settings/parser-adapters", json={"vision_enabled": True})

    assert resp.status_code == 500
    assert settings.rag_vision_enabled is False


def test_saved_post_parse_defaults_reach_other_processes(settings: Settings) -> None:
    """ほかのプロセスは .env の更新時刻の変化で読み直し、変わった項目だけを取り込む（#466）。"""
    settings.rag_navigation_summary_enabled = False
    # 別プロセスの Settings（同じ .env を読む）。今の .env を基準として覚えさせる。
    other = Settings(_env_file=(app_config.PLATFORM_ENV_FILE, app_config.BACKEND_ENV_FILE))
    other.rag_navigation_summary_enabled = False
    reload_env_settings_if_changed(other)

    resp = client.patch("/api/settings/parser-adapters", json={"navigation_summary_enabled": True})
    assert resp.status_code == 200
    # conftest は app.config と設定 API の backend/.env を同じ tmp のファイルへ向ける。
    assert _env_file() == app_config.BACKEND_ENV_FILE

    reload_env_settings_if_changed(other)
    assert other.rag_navigation_summary_enabled is True


def test_patch_extraction_fields_saves_definitions(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    schema_file = tmp_path / "extraction-fields.json"
    monkeypatch.setenv(fields_mod.FIELD_SCHEMA_FILE_ENV, str(schema_file))

    resp = client.patch(
        "/api/settings/extraction-fields",
        json={
            "fields": [
                {"name": " 請求書番号 ", "description": "請求書の番号", "value_type": "string"},
                {"name": "合計金額", "description": "", "value_type": "number"},
            ]
        },
    )

    assert resp.status_code == 200
    fields = resp.json()["data"]["fields"]
    assert [field["name"] for field in fields] == ["請求書番号", "合計金額"]
    assert fields[1]["value_type"] == "number"
    stored = json.loads(schema_file.read_text(encoding="utf-8"))
    assert [field["name"] for field in stored["fields"]] == ["請求書番号", "合計金額"]
    assert client.get("/api/settings/extraction-fields").json()["data"]["fields"] == fields


def test_patch_extraction_fields_rejects_duplicates_and_blank_names(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    schema_file = tmp_path / "extraction-fields.json"
    monkeypatch.setenv(fields_mod.FIELD_SCHEMA_FILE_ENV, str(schema_file))

    duplicate = client.patch(
        "/api/settings/extraction-fields",
        json={"fields": [{"name": "Total"}, {"name": "total"}]},
    )
    blank = client.patch("/api/settings/extraction-fields", json={"fields": [{"name": "  "}]})
    unknown_type = client.patch(
        "/api/settings/extraction-fields",
        json={"fields": [{"name": "x", "value_type": "array"}]},
    )

    assert duplicate.status_code == 422
    assert blank.status_code == 422
    assert unknown_type.status_code == 422
    assert not schema_file.exists()


def test_pipeline_settings_report_auto_advance_and_recipe_defaults(settings: Settings) -> None:
    settings.rag_auto_parse_after_preprocess_enabled = True
    settings.rag_auto_chunk_after_extract_enabled = False
    settings.rag_auto_index_after_chunk_enabled = True
    settings.rag_vision_enabled = True

    body = client.get("/api/settings/pipeline").json()["data"]

    assert body["auto_parse_after_preprocess_enabled"] is True
    assert body["auto_chunk_after_extract_enabled"] is False
    assert body["auto_index_after_chunk_enabled"] is True
    defaults = body["recipe_defaults"]
    # レシピの「グローバル設定に従う」と同じ解決（11 項目すべてに値がある）。
    for field in (
        "preprocess_profile",
        "auto_parse_after_preprocess_enabled",
        "parser_adapter_backend",
        "vision_enabled",
        "field_extraction_enabled",
        "navigation_summary_enabled",
        "auto_chunk_after_extract_enabled",
        "chunking_strategy",
        "auto_index_after_chunk_enabled",
        "graph_profile",
    ):
        assert defaults[field] is not None, field
    assert defaults["vision_enabled"] is True
    assert defaults["auto_chunk_after_extract_enabled"] is False
    assert defaults["parser_adapter_backend"] == "docling"
    assert defaults["chunking_strategy"] == settings.rag_chunking_strategy
    assert "chunk_context_header_enabled" not in defaults


@pytest.mark.parametrize("profile", ["fast", "balanced", "accurate"])
def test_pipeline_reports_saved_vector_index_profile(
    settings: Settings, monkeypatch: MonkeyPatch, profile: VectorIndexProfile
) -> None:
    class IndexOracle:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def get_vector_index_build_params(self) -> tuple[int, int]:
            return (32, 500)

    monkeypatch.setattr(settings_routes, "OracleClient", IndexOracle)
    monkeypatch.setattr(settings, "rag_vector_index_profile", "balanced")
    saved = client.patch("/api/settings/vector-index", json={"profile": profile})
    assert saved.status_code == 200
    detail = client.get("/api/settings/vector-index").json()["data"]
    overview = client.get("/api/settings/pipeline").json()["data"]
    assert overview["vector_index_profile"] == detail["profile"] == profile
    assert "vector_index_profile" not in overview["recipe_defaults"]


def test_patch_pipeline_persists_env_and_keeps_omitted_gates(settings: Settings) -> None:
    settings.rag_auto_parse_after_preprocess_enabled = True
    settings.rag_auto_chunk_after_extract_enabled = True
    settings.rag_auto_index_after_chunk_enabled = True

    resp = client.patch(
        "/api/settings/pipeline",
        json={"auto_chunk_after_extract_enabled": False, "auto_index_after_chunk_enabled": False},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["auto_parse_after_preprocess_enabled"] is True
    assert body["auto_chunk_after_extract_enabled"] is False
    assert body["auto_index_after_chunk_enabled"] is False
    assert body["recipe_defaults"]["auto_chunk_after_extract_enabled"] is False
    assert settings.rag_auto_parse_after_preprocess_enabled is True
    assert settings.rag_auto_chunk_after_extract_enabled is False
    assert settings.rag_auto_index_after_chunk_enabled is False
    saved = dotenv_values(_env_file())
    assert saved["RAG_AUTO_PARSE_AFTER_PREPROCESS_ENABLED"] == "true"
    assert saved["RAG_AUTO_CHUNK_AFTER_EXTRACT_ENABLED"] == "false"
    assert saved["RAG_AUTO_INDEX_AFTER_CHUNK_ENABLED"] == "false"
    # 保存した .env から作った Settings も同じ値になる（環境変数名は変えていない）。
    reloaded = Settings(_env_file=(app_config.PLATFORM_ENV_FILE, _env_file()))
    assert reloaded.rag_auto_chunk_after_extract_enabled is False
    assert reloaded.rag_auto_index_after_chunk_enabled is False


def test_patch_pipeline_without_changes_does_not_write(settings: Settings) -> None:
    resp = client.patch("/api/settings/pipeline", json={})

    assert resp.status_code == 200
    assert not _env_file().exists()


def test_patch_pipeline_write_failure_keeps_runtime(
    settings: Settings, monkeypatch: MonkeyPatch
) -> None:
    settings.rag_auto_parse_after_preprocess_enabled = True

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(settings_routes, "_replace_env_file", fail)

    resp = client.patch(
        "/api/settings/pipeline", json={"auto_parse_after_preprocess_enabled": False}
    )

    assert resp.status_code == 500
    assert settings.rag_auto_parse_after_preprocess_enabled is True


def test_patch_pipeline_rejects_non_boolean() -> None:
    resp = client.patch(
        "/api/settings/pipeline", json={"auto_parse_after_preprocess_enabled": "maybe"}
    )

    assert resp.status_code == 422


def test_patch_post_parse_vision_concurrency_persists_env(settings: Settings) -> None:
    """図・表の説明（Vision）を同時に呼ぶ数の上限を画面から保存する（#2002）。"""
    resp = client.patch("/api/settings/parser-adapters", json={"vision_concurrency": 6})

    assert resp.status_code == 200
    assert resp.json()["data"]["vision_concurrency"] == 6
    assert settings.rag_vision_concurrency == 6
    assert dotenv_values(_env_file())["RAG_VISION_CONCURRENCY"] == "6"

    too_many = client.patch("/api/settings/parser-adapters", json={"vision_concurrency": 17})
    assert too_many.status_code == 422
    assert settings.rag_vision_concurrency == 6


@pytest.mark.parametrize("enabled", [True, False])
def test_raptor_setting_is_saved_by_post_parse_api(settings: Settings, enabled: bool) -> None:
    settings.rag_raptor_enabled = not enabled
    response = client.patch("/api/settings/parser-adapters", json={"raptor_enabled": enabled})
    assert response.status_code == 200
    assert response.json()["data"]["raptor_enabled"] is enabled
    assert settings.rag_raptor_enabled is enabled
    assert dotenv_values(_env_file())["RAG_RAPTOR_ENABLED"] == str(enabled).lower()
