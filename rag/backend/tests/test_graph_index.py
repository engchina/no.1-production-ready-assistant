"""関係情報(文書と章・節の見出しのつながり)の index builder のテスト。"""

from app.rag.chunking import Chunk
from app.rag.graph_index import build_graph_index
from app.schemas.extraction import StructuredExtraction


def test_build_graph_index_creates_entities_and_relationships() -> None:
    """構造化 chunk metadata から deterministic KG artifact を生成する。"""
    extraction = StructuredExtraction.model_validate(
        {
            "raw_text": "第1章 承認条件\n12万円以上は部門長承認です。",
            "document_type": "社内規程",
            "confidence": 0.91,
            "elements": [
                {
                    "kind": "title",
                    "text": "第1章 承認条件",
                    "order": 1,
                    "page_number": 1,
                },
                {
                    "kind": "table",
                    "text": "| 項目 | 値 |\n| 承認 | 部門長 |",
                    "order": 2,
                    "page_number": 2,
                    "section_path": ["承認条件", "料金表"],
                },
            ],
        }
    )
    chunks = [
        Chunk(
            text="承認条件は12万円以上の場合に部門長承認です。",
            index=0,
            start_offset=0,
            end_offset=22,
            metadata={
                "section_path": "社内規程 > 承認条件",
                "section_title": "承認条件",
                "content_kind": "text",
                "confidence": 0.88,
                "page_number": 1,
            },
        ),
        Chunk(
            text="| 項目 | 値 |\n| 承認 | 部門長 |",
            index=1,
            start_offset=23,
            end_offset=43,
            metadata={
                "section_path": "社内規程 > 料金表",
                "section_title": "料金表",
                "content_kind": "table",
                "confidence": 0.74,
                "page_number": 2,
            },
        ),
    ]

    graph = build_graph_index(
        document_id="doc-1",
        knowledge_base_ids=["kb-1"],
        extraction=extraction,
        chunks=chunks,
    )

    entity_names = {entity.canonical_name for entity in graph.entities}
    assert "文書全体: 社内規程" in entity_names
    assert "社内規程 > 承認条件" in entity_names
    assert "料金表 (table)" in entity_names
    assert all(len(entity.entity_id) == 64 for entity in graph.entities)
    assert {entity.knowledge_base_id for entity in graph.entities} == {"kb-1"}
    assert any(entity.entity_type == "table_section" for entity in graph.entities)
    assert len(graph.relationships) >= 2
    assert {relationship.relationship_type for relationship in graph.relationships} == {"contains"}
    assert any(link.chunk_id == "doc-1:1" for link in graph.entity_chunk_links)
    # claims / community summary は読む経路が無かったため作らない(#621)。
    assert {field for field in vars(graph)} == {
        "entities",
        "relationships",
        "entity_chunk_links",
    }


def test_build_graph_index_uses_default_scope_without_knowledge_base() -> None:
    """KB 未所属 document でも fallback graph scope を生成できる。"""
    extraction = StructuredExtraction(raw_text="本文です。", document_type="メモ", confidence=0.8)
    chunks = [Chunk(text="本文です。", index=0, start_offset=0, end_offset=5)]

    graph = build_graph_index(
        document_id="doc-no-kb",
        knowledge_base_ids=[],
        extraction=extraction,
        chunks=chunks,
    )

    assert len(graph.entities) >= 1
    assert {entity.knowledge_base_id for entity in graph.entities} == {None}


def test_document_entity_uses_the_file_name() -> None:
    """文書の節点の名前はファイル名にし、同じ種類の文書を区別できる（#2051）。"""
    extraction = StructuredExtraction(raw_text="本文", document_type="社内規程", confidence=0.9)
    chunk = Chunk(
        text="本文", index=0, start_offset=0, end_offset=2, metadata={"section_path": "第 1 章"}
    )

    first = build_graph_index(
        document_id="doc-1",
        knowledge_base_ids=["kb-1"],
        extraction=extraction,
        chunks=[chunk],
        document_name="a.pdf",
    )
    second = build_graph_index(
        document_id="doc-2",
        knowledge_base_ids=["kb-1"],
        extraction=extraction,
        chunks=[chunk],
        document_name="b.pdf",
    )

    names = {
        e.canonical_name for e in [*first.entities, *second.entities] if e.entity_type == "document"
    }
    assert names == {"文書全体: a.pdf", "文書全体: b.pdf"}
