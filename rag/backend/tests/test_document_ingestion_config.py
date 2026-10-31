"""文書の処理設定（レシピ）の解決と、派生情報のレイヤーの状態のテスト。

3 層モデル: effective レシピは global 既定(「取込・検索・回答設定」)から解決する。
`PUT /api/documents/{id}/ingestion-config` とその応答のドリフトは #488 で削除した
（画面はレシピ単位の API を使う）。
"""

from collections.abc import Mapping
from pathlib import Path

import pytest

from app.api.routes import documents as documents_route
from app.config import get_settings
from app.main import app
from app.schemas.document import (
    DocumentProcessingConfig,
)
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


@pytest.mark.parametrize(
    "removed", ["marker", "unlimited_ocr", "glm_ocr", "oci_genai_vision", "enterprise_ai_vlm"]
)
def test_saved_recipe_with_removed_engine_inherits_global_default(removed: str) -> None:
    """削除したエンジン(#270)が文書レシピに保存されていても読み込めて、global 既定を継承する。

    文書レシピは extra="forbid" だが、旧フラグは検証前に取り除く。分割などの他の上書きは保つ。
    """
    config = DocumentProcessingConfig.model_validate(
        {
            "parser_adapter_backend": removed,
            f"parser_{removed}_enabled": True,
            "chunk_size": 900,
        }
    )

    assert config.parser_adapter_backend is None
    assert config.chunk_size == 900
    assert removed not in config.model_dump_json()
    effective, _ = documents_route._merge_document_processing_config(config)
    assert effective.rag_parser_adapter_backend == get_settings().rag_parser_adapter_backend


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ({"parser_docling_vision_enabled": True}, True),
        ({"parser_docling_vision_enabled": False, "asset_summary_enabled": True}, False),
        ({"parser_docling_vision_enabled": None, "asset_summary_enabled": True}, True),
        ({"asset_summary_enabled": False}, False),
        ({"vision_enabled": False, "parser_docling_vision_enabled": True}, False),
        ({"parser_docling_vision_enabled": None, "asset_summary_enabled": None}, None),
    ],
    ids=["docling", "docling_first", "asset_when_unset", "asset_off", "new_key_wins", "all_null"],
)
def test_saved_recipe_legacy_vision_keys_move_to_vision_enabled(
    saved: dict[str, object], expected: bool | None
) -> None:
    """保存済みレシピの旧 key(Docling の Vision・図表 VLM 要約)は vision_enabled へ移す(#497)。

    文書レシピは extra="forbid" だが、旧 key は検証前に移して捨てる。次に保存すると消える。
    """
    config = DocumentProcessingConfig.model_validate({**saved, "chunk_size": 900})

    assert config.vision_enabled is expected
    assert config.chunk_size == 900
    dumped = config.model_dump_json()
    assert "parser_docling_vision_enabled" not in dumped
    assert "asset_summary_enabled" not in dumped
    effective, _ = documents_route._merge_document_processing_config(config)
    assert effective.rag_vision_enabled is (
        expected if expected is not None else get_settings().rag_vision_enabled
    )


def test_recipe_vision_change_is_parser_output_drift() -> None:
    """Vision の変更は解析結果(図の要素の本文)を変えるので、解析の出力の差分に入る。"""
    groups = documents_route.DOCUMENT_PROCESSING_OUTPUT_GROUPS
    assert "vision_enabled" in groups["parser_adapter_backend"]
    assert "asset_summary_enabled" not in groups
    assert all("asset_summary_enabled" not in fields for fields in groups.values())


@pytest.fixture()
def _isolated_field_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.rag import extraction_field_adapter as fields_mod

    monkeypatch.setenv(fields_mod.FIELD_SCHEMA_FILE_ENV, str(tmp_path / "extraction-fields.json"))


def _metadata_state(extraction: Mapping[str, object], *, field: bool) -> tuple[str, str]:
    from app.config import Settings
    from app.rag import extraction_field_adapter as fields_mod
    from app.schemas.document import DocumentLayerStatusName

    status, reason = documents_route._metadata_layer_state(
        "項目抽出",
        extraction,
        Settings.model_construct(rag_field_extraction_enabled=field),
        bool(fields_mod.load_field_schema().fields),
    )
    assert isinstance(status, DocumentLayerStatusName)
    return status.value, reason


@pytest.mark.usefixtures("_isolated_field_schema")
def test_metadata_layer_planned_only_with_empty_field_schema() -> None:
    """項目抽出 有効 + 項目の定義が 0 件は planned_only になり理由で案内する。"""
    from app.rag import extraction_field_adapter as fields_mod

    # 未保存の環境は標準の項目を使う(#556)。0 件になるのは空の定義を保存したときだけ。
    fields_mod.save_field_schema([])
    status, reason = _metadata_state({}, field=True)
    assert status == "planned_only"
    assert "項目の定義が 0 件" in reason
    assert "標準の項目に戻す" in reason


@pytest.mark.usefixtures("_isolated_field_schema")
def test_metadata_layer_uses_standard_fields_until_saved() -> None:
    """未保存の環境は標準の項目があるため、成果物が無ければ「まだありません」で案内する。"""
    status, reason = _metadata_state({}, field=True)
    assert status == "planned_only"
    assert reason == "項目抽出の成果物がまだありません。"


@pytest.mark.usefixtures("_isolated_field_schema")
def test_metadata_layer_distinguishes_field_extraction_outcomes() -> None:
    """取込が刻んだ実行結果で、正常な 0 件・定義なし・本文なしを区別する(#2481)。"""
    from app.rag import extraction_field_adapter as fields_mod

    fields_mod.save_field_schema([fields_mod.FieldDefinition(name="請求書番号")])

    def outcome(status: str, count: int = 0) -> dict[str, object]:
        return {
            "parser_artifacts": {
                fields_mod.FIELD_EXTRACTION_OUTCOME_ARTIFACT_KEY: {
                    "status": status,
                    "field_count": count,
                }
            }
        }

    status, reason = _metadata_state(outcome("completed"), field=True)
    assert status == "materialized"
    assert "0 件" in reason
    status, reason = _metadata_state(outcome("no_definitions"), field=True)
    assert status == "planned_only"
    assert "取込の時点では" in reason
    status, reason = _metadata_state(outcome("no_text"), field=True)
    assert status == "planned_only"
    assert "本文が無い" in reason
    # 刻みの無い抽出結果(未実行・旧い取込)は従来どおり。
    status, reason = _metadata_state({"raw_text": "本文"}, field=True)
    assert status == "planned_only"
    assert reason == "項目抽出の成果物がまだありません。"


@pytest.mark.usefixtures("_isolated_field_schema")
def test_metadata_layer_ignores_asset_summaries() -> None:
    """図の要約(Vision の解析結果。#497)は派生情報のレイヤーの成果物として数えない。"""
    from app.rag import extraction_field_adapter as fields_mod

    fields_mod.save_field_schema([fields_mod.FieldDefinition(name="請求書番号")])
    extraction = {"assets": [{"asset_id": "a1", "summary": "図の説明"}]}
    status, reason = _metadata_state(extraction, field=True)
    assert status == "planned_only"
    assert "項目抽出" in reason
    assert "has_asset_summary" not in documents_route._layer_metrics("metadata", extraction)


@pytest.mark.usefixtures("_isolated_field_schema")
def test_metadata_layer_materialized_with_fields_only() -> None:
    """項目抽出のみ有効で fields があれば materialized(図表要約の有無に依らない)。"""
    from app.rag import extraction_field_adapter as fields_mod

    fields_mod.save_field_schema([fields_mod.FieldDefinition(name="請求書番号")])
    extraction = {"fields": [{"name": "請求書番号", "value": "INV-1"}]}
    status, _reason = _metadata_state(extraction, field=True)
    assert status == "materialized"
