"""親の混在機能を引用へ転写せず、省略可能な監査値で操作を失わない。"""

from rag_engine.generation.answering import AnswerRecord
from rag_engine.retrieval.evidence_selection import evidence_spans


def record(uid, text, level="parent", **kwargs):
    return AnswerRecord(
        id=uid,
        chunk_uid=uid,
        chunk_id=uid,
        source="manual.pdf",
        source_run_id="run",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        chunk_level=level,
        text=text,
        **kwargs,
    )


def test_native_steps_take_verified_child_scope_without_changing_offsets():
    a = "管理－（１）拠点設定\n名称を修正し実行します。"
    b = "管理－（２）利用者設定\n利用者を削除します。"
    p = record("r:p", a + "\n" + b, metadata={"section_path": ["管理－（１）拠点設定", "管理－（２）利用者設定"]})
    c = record("r:c", a, "child", parent_chunk_uid="r:p", metadata={"section_path": ["管理－（１）拠点設定"]})
    other = record("r:d", b, "child", parent_chunk_uid="r:p", metadata={"section_path": ["管理－（２）利用者設定"]})
    spans = evidence_spans("拠点の名称を修正する方法", [p], scope_records=[c, other])
    assert all(s["text"] == p.text[s["start"] : s["end"]] for s in spans)
    edit = next(s for s in spans if "名称を修正" in s["text"])
    deletion = next(s for s in spans if "利用者を削除" in s["text"])
    assert edit["section_path"] == c.metadata["section_path"]
    assert deletion["section_path"] == other.metadata["section_path"]
    assert p.metadata["section_path"] == ["管理－（１）拠点設定", "管理－（２）利用者設定"]
    wrong = c.__class__(**{**c.__dict__, "source_run_id": "other"})
    assert "scope_source_ids" not in evidence_spans("拠点変更", [p], scope_records=[wrong])[0]


def test_explicit_heading_clears_previous_function_but_adjacency_does_not():
    from rag_engine.retrieval.evidence_selection import _local_child_path

    c = record("r:c", "", metadata={"section_path": ["管理－（１）拠点設定", "管理－（２）利用者設定", "B【操作説明】"]})
    assert _local_child_path(c, "管理－（２）利用者設定\n") == ("管理－（２）利用者設定", "B【操作説明】")
    assert _local_child_path(c, "名称が似ています。\n") == tuple(c.metadata["section_path"])


def test_alternative_button_needs_its_own_literal_label():
    from rag_engine.generation.interaction_grounding import interaction_binding_error

    spans = [dict(text="実行ボタンで登録します。", origin="document_text")]
    assert interaction_binding_error("「実行」または「保存」ボタンを押します。", spans)
    assert not interaction_binding_error("「実行」ボタンを押します。", spans)
    assert not interaction_binding_error(
        "「実行」または「保存」ボタンを押します。", [dict(text="実行ボタンまたは保存ボタンで登録します。", origin="document_text")]
    )


def test_button_presence_in_cited_image_is_left_to_visual_applicability_audit():
    from rag_engine.generation.interaction_grounding import interaction_binding_error

    assert not interaction_binding_error("「更新」ボタンを押します。", [dict(text="画像のボタン：更新", origin="image_extraction")])
