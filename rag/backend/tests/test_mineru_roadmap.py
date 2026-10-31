"""MinerU は準備中(ロードマップ。#3359)。OCI Document Understanding(#3374)と同じ扱い。

client・ライセンスの確認の関門の実装は残すが、選べない(全体の設定・処理レシピ・`.env`)。
保存済みの値は既定へ寄せ、取込では呼ばずに止める。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.config import PARSER_SETTINGS_STORE, Settings, get_settings
from app.main import app
from app.rag.kb_adapter_config import KnowledgeBaseAdapterConfig
from app.schemas.document import DocumentProcessingConfig, DocumentProcessingConfigInput
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


def test_parser_adapter_settings_reports_mineru_as_roadmap() -> None:
    resp = client.get("/api/settings/parser-adapters")

    assert resp.status_code == 200
    adapters = {item["backend"]: item for item in resp.json()["data"]["adapters"]}
    assert adapters["mineru"]["availability"] == "roadmap"
    assert adapters["mineru"]["selected"] is False
    assert adapters["dots_ocr"]["availability"] == "available"
    assert adapters["docling"]["availability"] == "available"


@pytest.mark.parametrize(
    "payload",
    [
        {"adapter_backend": "mineru"},
        {"adapter_backend": "docling", "mineru_enabled": True},
    ],
    ids=["select", "enable-flag"],
)
def test_update_parser_adapter_settings_rejects_roadmap_mineru(
    payload: dict[str, object],
) -> None:
    settings = get_settings()
    before = settings.rag_parser_adapter_backend

    resp = client.patch(
        "/api/settings/parser-adapters", json={**payload, "mineru_license_acknowledged": True}
    )

    assert resp.status_code == 422
    assert "準備中（ロードマップ）" in json.dumps(resp.json(), ensure_ascii=False)
    assert settings.rag_parser_adapter_backend == before
    assert settings.rag_parser_mineru_enabled is False
    assert not PARSER_SETTINGS_STORE.path(settings).exists()


def test_env_mineru_backend_is_rejected_with_reason() -> None:
    with pytest.raises(ValueError, match="準備中（ロードマップ）"):
        Settings(_env_file=None, rag_parser_adapter_backend="mineru")


def test_persisted_mineru_backend_falls_back_to_default(tmp_path: Path) -> None:
    parser_file = tmp_path / "parser-settings.json"
    parser_file.write_text(
        json.dumps({"adapter_backend": "mineru", "mineru_enabled": True}), encoding="utf-8"
    )
    worker_settings = Settings(_env_file=None, rag_parser_settings_file=str(parser_file))
    store = copy.copy(PARSER_SETTINGS_STORE)
    store.reset()

    store.load(worker_settings)

    assert worker_settings.rag_parser_adapter_backend == "docling"


@pytest.mark.parametrize(
    "config",
    [{"parser_adapter_backend": "mineru"}, {"parser_mineru_enabled": True}],
    ids=["backend", "flag"],
)
def test_recipe_input_rejects_roadmap_mineru(config: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="準備中（ロードマップ）"):
        DocumentProcessingConfigInput.model_validate(config)


def test_recipe_put_rejects_roadmap_mineru_before_saving() -> None:
    resp = client.put(
        "/api/documents/doc-1/recipes/recipe-1", json={"parser_adapter_backend": "mineru"}
    )

    assert resp.status_code == 422
    assert "MinerU の解析は準備中（ロードマップ）" in json.dumps(resp.json(), ensure_ascii=False)


def test_saved_recipe_mineru_inherits_global_default() -> None:
    saved = DocumentProcessingConfig.model_validate(
        {"parser_adapter_backend": "mineru", "chunking_strategy": "page_level"}
    )

    assert saved.parser_adapter_backend is None
    assert saved.chunking_strategy == "page_level"
    config = KnowledgeBaseAdapterConfig(ingestion=saved)
    assert "rag_parser_adapter_backend" not in config.settings_overrides("ingestion")
