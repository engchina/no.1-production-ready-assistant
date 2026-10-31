"""backend の実体の索引・項目抽出の値・安全チェックの語は言語資源から作る(#2181 段 5)。"""

from __future__ import annotations

from rag_parser_core.language import bind_pack, builtin_pack, resolve

from app.rag.chunking import Chunk
from app.rag.entity_index import build_entity_index
from app.rag.extraction_field_adapter import normalize_field_value
from app.rag.grounding_metrics import grounding_stopwords


def _chunk(text: str) -> Chunk:
    return Chunk(text=text, index=0, start_offset=0, end_offset=len(text), metadata={})


def _names(text: str) -> set[str]:
    index = build_entity_index(document_id="d", chunks=[_chunk(text)], chunk_set_id="c")
    return {alias.alias_text for entity in index.entities for alias in entity.aliases}


def test_entity_cues_follow_the_pack() -> None:
    assert "受付窓口" not in _names("問い合わせは受付窓口が受けます。")
    with bind_pack(
        resolve(builtin_pack(), {"entity_cues": {"organization_suffixes": ["部", "窓口"]}})
    ):
        assert "受付窓口" in _names("問い合わせは受付窓口が受けます。")


def test_field_values_and_grounding_stopwords_follow_the_pack() -> None:
    assert normalize_field_value("bool", "済") == "済"
    assert "根拠" in grounding_stopwords()
    custom = resolve(
        builtin_pack(),
        {
            "field_value_cues": {"true_values": ["true", "済"]},
            "grounding_cues": {"grounding_stopwords": ["回答"]},
        },
    )
    with bind_pack(custom):
        assert normalize_field_value("bool", "済") == "true"
        assert grounding_stopwords() == frozenset({"回答"})
