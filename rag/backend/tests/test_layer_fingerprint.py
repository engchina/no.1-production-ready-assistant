"""派生情報レイヤーの入力の指紋と「作り直しが必要」の判定(#550)。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.rag import extraction_field_adapter as fields_mod
from app.rag.chunking_small_to_big import SMALL_TO_BIG_STRATEGY
from app.rag.extraction_field_adapter import FieldDefinition, save_field_schema
from app.rag.ingestion import IngestionPipeline
from app.rag.layer_fingerprint import (
    CHUNK_METADATA_CONTRACT_INPUT,
    FIELD_SCHEMA_HASH_ARTIFACT_KEY,
    FIELD_SCHEMA_INPUT,
    NAVIGATION_SUMMARY_MAX_NODES_ARTIFACT_KEY,
    NAVIGATION_SUMMARY_MAX_NODES_INPUT,
    changed_layer_inputs,
    chunk_metadata_contract_hash,
    current_field_definitions,
    current_layer_inputs,
    field_schema_hash,
    recorded_layer_fingerprint,
)
from app.schemas.extraction import StructuredExtraction

_DEFS = [
    FieldDefinition(name="請求書番号", description="invoice no"),
    FieldDefinition(name="合計金額", value_type="number"),
]


def _current_fields() -> list[FieldDefinition]:
    """KB の定義を持たない文書の今の定義(全体の既定)。"""
    return current_field_definitions([])


@pytest.fixture(autouse=True)
def _isolated_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fields_mod.FIELD_SCHEMA_FILE_ENV, str(tmp_path / "extraction-fields.json"))


def test_field_schema_hash_changes_with_definitions_and_order() -> None:
    assert field_schema_hash(_DEFS) == field_schema_hash(list(_DEFS))
    assert field_schema_hash(_DEFS) != field_schema_hash(_DEFS[:1])
    # 並び順も抽出の指示に入るため、入れ替えは別の定義として扱う。
    assert field_schema_hash(_DEFS) != field_schema_hash(list(reversed(_DEFS)))
    edited = [_DEFS[0].model_copy(update={"description": "請求書の番号"}), _DEFS[1]]
    assert field_schema_hash(_DEFS) != field_schema_hash(edited)


def test_recorded_fingerprint_uses_values_stamped_at_extraction() -> None:
    """記録の時点の設定ではなく、抽出の工程で刻んだ値を使う(抽出を使い回す再実行に備える)。"""
    extraction = {
        "parser_artifacts": {
            FIELD_SCHEMA_HASH_ARTIFACT_KEY: "stamped-hash",
            NAVIGATION_SUMMARY_MAX_NODES_ARTIFACT_KEY: 7,
        }
    }
    settings = Settings(rag_chunking_strategy="structure_aware")

    assert recorded_layer_fingerprint("metadata", extraction, settings) == {
        FIELD_SCHEMA_INPUT: "stamped-hash"
    }
    assert recorded_layer_fingerprint("navigation", extraction, settings) == {
        NAVIGATION_SUMMARY_MAX_NODES_INPUT: 7
    }
    # 関係情報は実体化しないため、比べる入力を持たない。
    assert recorded_layer_fingerprint("graph", extraction, settings) is None


def test_recorded_fingerprint_adds_chunk_contract_only_for_small_to_big() -> None:
    small_to_big = Settings(rag_chunking_strategy=SMALL_TO_BIG_STRATEGY)
    other = Settings(rag_chunking_strategy="structure_aware")

    assert recorded_layer_fingerprint("metadata", None, small_to_big) == {
        CHUNK_METADATA_CONTRACT_INPUT: chunk_metadata_contract_hash()
    }
    # 刻みの無い抽出結果(この変更より前の抽出)からは指紋を作らない(不明)。
    assert recorded_layer_fingerprint("metadata", {"parser_artifacts": {}}, other) is None
    assert recorded_layer_fingerprint("navigation", None, other) is None


def test_standard_fields_are_the_current_definitions_until_saved() -> None:
    """未保存の環境の今の定義は標準の項目。保存すれば作り直しが必要、戻せば消える(#556)。"""
    settings = Settings()
    standard = fields_mod.standard_field_definitions()
    assert _current_fields() == standard
    recorded = {FIELD_SCHEMA_INPUT: field_schema_hash(standard)}
    assert changed_layer_inputs(recorded, current_layer_inputs(settings, _current_fields())) == []

    save_field_schema(_DEFS)
    changed = changed_layer_inputs(recorded, current_layer_inputs(settings, _current_fields()))
    assert changed == [FIELD_SCHEMA_INPUT]

    fields_mod.reset_field_schema()
    assert changed_layer_inputs(recorded, current_layer_inputs(settings, _current_fields())) == []


def test_changed_inputs_compares_recorded_inputs_with_current() -> None:
    save_field_schema(_DEFS)
    settings = Settings(rag_navigation_summary_max_nodes=24)
    current = current_layer_inputs(settings, _current_fields())
    recorded = {
        FIELD_SCHEMA_INPUT: field_schema_hash(_DEFS),
        NAVIGATION_SUMMARY_MAX_NODES_INPUT: 24,
    }

    assert changed_layer_inputs(recorded, current) == []

    save_field_schema(_DEFS[:1])
    changed = changed_layer_inputs(recorded, current_layer_inputs(settings, _current_fields()))
    assert changed == [FIELD_SCHEMA_INPUT]

    smaller = current_layer_inputs(
        settings.model_copy(update={"rag_navigation_summary_max_nodes": 8}), _current_fields()
    )
    assert changed_layer_inputs(recorded, smaller) == [
        FIELD_SCHEMA_INPUT,
        NAVIGATION_SUMMARY_MAX_NODES_INPUT,
    ]


@pytest.mark.parametrize("recorded", [None, {}, "not-a-mapping"], ids=["null", "empty", "text"])
def test_missing_fingerprint_is_unknown(recorded: object) -> None:
    """指紋の無い行は「不明」で、作り直しの警告を出さない(誤警告を避ける)。"""
    current = current_layer_inputs(Settings(), _current_fields())
    assert changed_layer_inputs(recorded, current) == []


def test_inputs_unknown_to_current_code_are_ignored() -> None:
    """記録にあっても今のコードが知らない(削除した)入力は、結果を変えないため比べない。"""
    current = current_layer_inputs(Settings(), _current_fields())
    recorded = {"retired_input": "old", FIELD_SCHEMA_INPUT: current[FIELD_SCHEMA_INPUT]}
    assert changed_layer_inputs(recorded, current) == []


class _FakeVlm:
    def __init__(self, response: str) -> None:
        self.response = response

    async def generate(self, prompt: str, text: str, **_options: object) -> str:
        _ = prompt, text
        return self.response


class _NoOracle:
    """文書は KB の定義を持たない(全体の既定で抽出する)。"""

    async def list_document_extraction_field_sets(self, document_id: str) -> list[object]:
        _ = document_id
        return []


def _pipeline(settings: Settings, response: str) -> Any:
    return IngestionPipeline(
        vlm=_FakeVlm(response),  # type: ignore[arg-type]
        oracle=_NoOracle(),  # type: ignore[arg-type]
        # 保存先の無い fake(項目抽出の単位の結果を既定の保存先へ書かない。#2720)。
        object_storage=object(),  # type: ignore[arg-type]
        settings=settings,
    )


async def test_field_extraction_stamps_the_definitions_it_used() -> None:
    save_field_schema(_DEFS)
    pipeline = _pipeline(
        Settings(rag_field_extraction_enabled=True),
        '[{"name": "請求書番号", "value": "INV-1", "value_type": "string", "confidence": 0.9}]',
    )

    result = await pipeline._attach_extraction_fields(
        "trace", StructuredExtraction(raw_text="請求書番号 INV-1"), document_id="doc-1"
    )

    assert [field.name for field in result.fields] == ["請求書番号"]
    assert result.parser_artifacts[FIELD_SCHEMA_HASH_ARTIFACT_KEY] == field_schema_hash(_DEFS)


async def test_field_extraction_without_definitions_does_not_stamp() -> None:
    # 空の全体の既定を保存した環境(未保存なら標準の項目で抽出する。#556)。
    save_field_schema([])
    pipeline = _pipeline(Settings(rag_field_extraction_enabled=True), "[]")

    result = await pipeline._attach_extraction_fields(
        "trace", StructuredExtraction(raw_text="請求書番号 INV-1"), document_id="doc-1"
    )

    assert FIELD_SCHEMA_HASH_ARTIFACT_KEY not in result.parser_artifacts


_SECTIONED_TEXT = (
    "# 第1章 概要\n\n社内規程の概要を説明します。\n\n"
    "## 1.1 経費申請\n\n部門長の承認後、経理部が確認します。\n"
)


async def test_navigation_summary_stamps_max_nodes_it_used() -> None:
    pipeline = _pipeline(
        Settings(rag_navigation_summary_enabled=True, rag_navigation_summary_max_nodes=3),
        "章節の要約です。",
    )

    result = await pipeline._attach_navigation_tree(
        "trace", StructuredExtraction(raw_text=_SECTIONED_TEXT)
    )

    assert result.navigation
    assert result.parser_artifacts[NAVIGATION_SUMMARY_MAX_NODES_ARTIFACT_KEY] == 3


async def test_navigation_without_summary_does_not_stamp() -> None:
    pipeline = _pipeline(Settings(rag_navigation_summary_enabled=False), "")

    result = await pipeline._attach_navigation_tree(
        "trace", StructuredExtraction(raw_text=_SECTIONED_TEXT)
    )

    assert result.navigation
    assert NAVIGATION_SUMMARY_MAX_NODES_ARTIFACT_KEY not in result.parser_artifacts


# ---- 文書が属する KB の定義で比べる（#548）----

_KB_DEFS = [FieldDefinition(name="契約日", value_type="date")]


def test_current_field_definitions_follow_knowledge_base_definitions() -> None:
    """今の定義は抽出と同じ解決(KB の定義・無ければ既定・複数 KB は和集合)で決める。"""
    save_field_schema(_DEFS)
    assert current_field_definitions([]) == _DEFS
    assert current_field_definitions([None]) == _DEFS
    assert current_field_definitions([_KB_DEFS]) == _KB_DEFS
    assert current_field_definitions([_KB_DEFS, None]) == [*_KB_DEFS, *_DEFS]
    # 全体の既定を渡せば file を読まない(一覧で文書ごとに読まない)。
    assert current_field_definitions([None], default_fields=_KB_DEFS) == _KB_DEFS


def test_knowledge_base_definition_change_marks_rebuild_only_for_its_documents() -> None:
    """KB の定義で抽出した層は、全体の既定が変わっても作り直しにならず、KB の定義が変わるとなる。"""
    save_field_schema(_DEFS)
    settings = Settings()
    recorded = {FIELD_SCHEMA_INPUT: field_schema_hash(_KB_DEFS)}

    def changed(fields: list[FieldDefinition]) -> list[str]:
        return changed_layer_inputs(
            recorded, current_layer_inputs(settings, current_field_definitions([fields]))
        )

    assert changed(_KB_DEFS) == []
    save_field_schema(_DEFS[:1])
    assert changed(_KB_DEFS) == []
    assert changed([_KB_DEFS[0].model_copy(update={"description": "締結日"})]) == [
        FIELD_SCHEMA_INPUT
    ]


async def test_field_extraction_stamps_knowledge_base_definitions_it_used() -> None:
    """抽出の刻みは、実際に使った KB の定義の hash にする。"""

    class _KbOracle:
        async def list_document_extraction_field_sets(self, document_id: str) -> list[object]:
            _ = document_id
            return [_KB_DEFS]

    save_field_schema(_DEFS)
    pipeline = IngestionPipeline(
        vlm=_FakeVlm('[{"name": "契約日", "value": "2025-04-01"}]'),  # type: ignore[arg-type]
        oracle=_KbOracle(),  # type: ignore[arg-type]
        settings=Settings(rag_field_extraction_enabled=True),
    )

    result = await pipeline._attach_extraction_fields(
        "trace", StructuredExtraction(raw_text="契約日 2025-04-01"), document_id="doc-1"
    )

    assert result.parser_artifacts[FIELD_SCHEMA_HASH_ARTIFACT_KEY] == field_schema_hash(_KB_DEFS)
