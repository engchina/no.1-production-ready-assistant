"""native JSON、操作境界、分割回答の根拠引用を実際の障害条件で検証する。"""

import json
from dataclasses import replace

import pytest

from rag_engine.adapters.oracle.store import _json_dict, _stored_chunk_from_row
from rag_engine.generation.answering import AnswerRecord
from rag_engine.retrieval.context_builder import (
    ContextBuildRequest,
    build_chunk_context_bundle,
    record_has_image_evidence,
)


class Lob:
    """Oracle LOBと同じread契約を持つテスト値。"""

    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


@pytest.mark.parametrize("encode", [lambda v: v, json.dumps, lambda v: json.dumps(v).encode(), lambda v: Lob(json.dumps(v)), Lob])
def test_oracle_json_keeps_images_coordinates_and_children(encode):
    meta = {
        "image_evidence": [{"image_id": "img1", "raw_type": "picture", "vision_crop": "crop.png"}],
        "table_context": [{"table_id": "t1"}],
    }
    refs = [{"record_id": "img1", "page": 1, "bbox": [1, 2, 3, 4]}]
    row = [
        "run:p1",
        "p1",
        "parent",
        1,
        "",
        "",
        encode(["c1"]),
        "画面のボタンで入力",
        "検索",
        "run",
        "manual.pdf",
        "docling",
        "Docling",
        1,
        1,
        encode([{"page": 1, "seq_start": 1, "seq_end": 2}]),
        encode(refs),
        encode([{"page": 1, "bbox": [1, 2, 3, 4]}]),
        encode(meta),
    ]
    chunk = _stored_chunk_from_row(row)
    assert chunk.child_chunk_ids == ("c1",)
    assert chunk.source_record_refs[0]["bbox"] == [1, 2, 3, 4]
    assert chunk.metadata["layout"]["display_regions"]
    assert chunk.metadata["table_context"] == meta["table_context"]
    assert record_has_image_evidence(chunk)
    chunk.metadata["image_evidence"].clear()
    assert len(meta["image_evidence"]) == 1


@pytest.mark.parametrize("value", ["{'image_evidence': []}", "[1]", "secret-invalid-json", 12])
def test_invalid_nonempty_metadata_is_not_silently_erased(value):
    with pytest.raises(ValueError) as error:
        _json_dict(value)
    assert "secret" not in str(error.value)


def rec(key, level, seq, text, *, parent="", page=1, source="manual.pdf", headings=None):
    return AnswerRecord(
        id=key,
        engine="docling",
        engine_label="Docling",
        page=page,
        page_end=page,
        seq_no=seq,
        chunk_seq=seq,
        category="Chunk",
        text=text,
        chunk_id=key,
        chunk_level=level,
        parent_chunk_id=parent,
        source=source,
        source_run_id=source,
        metadata={"section_path": headings or []},
    )


def test_parent_context_does_not_add_another_parent_by_operation_name():
    anchor = rec("c1", "child", 1, "画面", parent="p1")
    parent = rec("p1", "parent", 1, "画面で編集", headings=["〔納品書〕"])
    support = rec("p2", "parent", 2, "ボタンの操作全文", page=2, headings=["〔納品書〕"])
    child = rec("c2", "child", 2, "ボタンの操作全文", parent="p2", page=2)
    others = [
        rec("p3", "parent", 3, "別画面", page=2, headings=["〔別帳票〕"]),
        rec("p4", "parent", 4, "別ファイル", source="other.pdf", headings=["〔納品書〕"]),
        rec("p5", "parent", 5, "遠いページ", page=9, headings=["〔納品書〕"]),
    ]
    request = ContextBuildRequest(
        question="入力方法",
        ranked_children=[anchor],
        active_records=[anchor, parent, support, child, *others],
        top_k=1,
        neighbor_child_count=2,
        max_records=3,
        max_chars=3000,
    )
    bundle = build_chunk_context_bundle(request)
    assert [r.id for r in bundle.records] == ["p1"]
    assert "ボタンの操作全文" not in bundle.text
    assert build_chunk_context_bundle(replace(request, neighbor_child_count=0)).records == [parent]
    assert len(build_chunk_context_bundle(replace(request, max_records=1)).records) == 1


def test_generic_section_does_not_link_unrelated_screens():
    anchor = rec("c1", "child", 1, "画面", parent="p1")
    records = [
        anchor,
        rec("p1", "parent", 1, "画面A", headings=["B 【操作説明】", "４在庫台帳"]),
        rec("p2", "parent", 2, "画面B", headings=["B 【操作説明】", "４在庫台帳"]),
    ]
    bundle = build_chunk_context_bundle(
        ContextBuildRequest(
            question="入力方法",
            ranked_children=[anchor],
            active_records=records,
            top_k=1,
            neighbor_child_count=2,
            max_records=3,
            max_chars=3000,
        )
    )
    assert [r.id for r in bundle.records] == ["p1"]


def test_same_image_id_in_different_documents_is_not_deduplicated(tmp_path):
    from rag_engine.generation.answering import answer_image_evidence

    records = [rec("p1", "parent", 1, "画面", source=source) for source in ("a.pdf", "b.pdf")]
    for record in records:
        record.metadata["image_evidence"] = [{"image_id": "docling-p1-1", "raw_type": "picture", "page": 1}]
    images = answer_image_evidence(records, tmp_path)
    assert len(images) == 2
    assert {i["source"] for i in images} == {"a.pdf", "b.pdf"}


def test_curated_operation_terms_keep_sources_and_unverified_gaps_inactive():
    from pathlib import Path

    from rag_engine.knowledge.runtime_knowledge import load_runtime_knowledge

    path = Path(__file__).resolve().parents[1] / "knowledge_assets/runtime_knowledge/operation_evidence_349.json"
    if not path.is_file():
        pytest.skip("顧客資料由来の用語データは repo 外。ローカルにある環境だけで照合する")
    payload = json.loads(path.read_text())
    knowledge = load_runtime_knowledge(path.parent, path)
    assert len(knowledge.terms) == 5 and not knowledge.error
    assert knowledge.rules == ()
    for term in payload["terms"]:
        evidence = term["evidence"]
        assert evidence["source_run_id"] in payload["documents"]
        assert evidence["page"] > 0 and evidence["verified_excerpt"]
    assert all(gap["status"] != "approved" for gap in payload["knowledge_gaps"])


def test_operation_expansion_never_evicts_retrieved_evidence_at_budget():
    records = []
    anchors = []
    for i in range(1, 4):
        child = rec(f"c{i}", "child", i, f"本文{i}", parent=f"p{i}")
        anchors.append(child)
        records.extend([child, rec(f"p{i}", "parent", i, f"本文{i}", headings=["〔納品書〕"])])
    records.append(rec("p4", "parent", 4, "補足", headings=["〔納品書〕"]))
    bundle = build_chunk_context_bundle(
        ContextBuildRequest(
            question="操作方法",
            ranked_children=anchors,
            active_records=records,
            top_k=3,
            neighbor_child_count=3,
            max_records=3,
            max_chars=3000,
        )
    )
    assert {r.id for r in bundle.records} == {"p1", "p2", "p3"}
