"""複数レシピ融合で、元の文書の範囲が重なる根拠を 1 つにまとめる (#1331)。"""

from rag_engine.generation.answering import AnswerRecord
from rag_engine.retrieval.evidence_selection import (
    collapse_source_spans,
    evidence_spans,
    new_evidence_count,
    record_element_ids,
    source_span_relation,
)

PARAGRAPHS = [
    "経費精算の申請は、支出した日から30日以内に経費精算画面で行います。",
    "領収書は原本を経理部へ郵送し、申請番号を封筒に記入します。",
    "交通費は経路と運賃を入力し、定期区間の運賃は差し引きます。",
    "立替金が5万円を超える場合は、部長の事前承認を添付します。",
    "差し戻された申請は、修正して再申請ボタンを押します。",
]


def _record(uid, element_ids, text, *, document_id="doc-1", source="経費精算マニュアル.pdf", refs=None):
    metadata = {"section_path": ["経費精算"], "document_id": document_id}
    if element_ids is not None:
        metadata["element_ids"] = element_ids if isinstance(element_ids, str) else list(element_ids)
    return AnswerRecord(
        id=uid,
        engine="docling",
        engine_label="Docling",
        page=3,
        seq_no=1,
        category="ParentChunk",
        text=text,
        source=source,
        source_run_id="run",
        chunk_id=uid,
        chunk_uid=uid,
        chunk_level="parent",
        source_record_refs=tuple(refs or ()),
        metadata=metadata,
    )


def _ids(*numbers):
    return [f"docling-p3-{n}" for n in numbers]


def _text(*numbers):
    return "\n".join(PARAGRAPHS[n] for n in numbers)


def test_chunks_of_two_recipes_sharing_elements_collapse_and_keep_the_other_id_as_alias():
    """親子分割の親と、構造で分けた chunk が同じ要素を含むと、根拠は 1 つになり、もう一方は別名に残る。"""
    parent = _record("doc-1:parent-a", _ids(0, 1, 2, 3, 4), _text(0, 1, 2, 3, 4))
    structure = _record("doc-1:structure-b", _ids(1, 2, 3), _text(1, 2, 3))

    spans = evidence_spans("経費精算の申請方法", [parent, structure])

    assert {span["source_id"] for span in spans} == {"doc-1:parent-a"}
    assert spans[0]["source_aliases"] == ["doc-1:parent-a", "doc-1:structure-b"]
    assert "".join(span["text"] for span in spans) == parent.text


def test_wider_lower_ranked_chunk_is_kept_at_the_higher_rank_position():
    """順位の高い狭い chunk を残すと広いほうにだけある本文を落とすので、広いほうを高い順位の位置に残す。"""
    narrow = _record("doc-1:structure-b", _ids(1, 2), _text(1, 2))
    other = _record("doc-2:other", _ids(0), "別の文書の本文。" * 5, document_id="doc-2", source="出張規程.pdf")
    wide = _record("doc-1:parent-a", _ids(0, 1, 2, 3, 4), _text(0, 1, 2, 3, 4))

    collapsed = collapse_source_spans([narrow, other, wide])

    assert [(kept.id, [m.id for m in members]) for kept, members in collapsed] == [
        ("doc-1:parent-a", ["doc-1:structure-b", "doc-1:parent-a"]),
        ("doc-2:other", ["doc-2:other"]),
    ]
    spans = evidence_spans("経費精算の申請方法", [narrow, other, wide])
    assert spans[0]["source_id"] == "doc-1:parent-a"
    assert spans[0]["source_aliases"] == ["doc-1:structure-b", "doc-1:parent-a"]
    assert any(PARAGRAPHS[4] in span["text"] for span in spans)


def test_near_identical_ranges_keep_the_higher_ranked_record():
    """互いの本文をほぼ含む（同じ範囲）ときは、rerank の順位が高いほうを残す。"""
    first = _record("doc-1:recipe-a", _ids(0, 1, 2), _text(0, 1, 2))
    second = _record("doc-1:recipe-b", _ids(0, 1, 2), _text(0, 1, 2).replace("\n", "\n\n"))

    assert source_span_relation(first, second) == "same"
    assert [kept.id for kept, _ in collapse_source_spans([second, first])] == ["doc-1:recipe-b"]


def test_overlap_below_the_threshold_keeps_both():
    """要素の集合の重なりが小さい側の 8 割に届かない根拠は、両方残す。"""
    left = _record("doc-1:recipe-a", _ids(0, 1, 2, 3, 4), _text(0, 1, 2, 3, 4))
    right = _record("doc-1:recipe-b", _ids(3, 4, 5, 6), _text(3, 4) + "\n別の段落の本文です。\n最後の段落の本文です。")

    assert source_span_relation(left, right) is None
    spans = evidence_spans("経費精算の申請方法", [left, right])
    assert {span["source_id"] for span in spans} == {"doc-1:recipe-a", "doc-1:recipe-b"}


def test_look_alike_ids_from_different_parse_results_with_different_text_are_not_merged():
    """解析の設定が違うレシピでは同じ要素の ID が別の箇所を指すことがある。本文が違えばまとめない。"""
    docling_ocr = _record("doc-1:recipe-a", _ids(1, 2, 3), _text(1, 2, 3))
    docling_plain = _record(
        "doc-1:recipe-b",
        _ids(1, 2, 3),
        "第2章 旅費規程\n国内出張の日当は役職ごとに定めます。\n宿泊費は実費とし上限を超えた分は自己負担です。",
    )

    assert source_span_relation(docling_ocr, docling_plain) is None
    spans = evidence_spans("経費精算の申請方法", [docling_ocr, docling_plain])
    assert {span["source_id"] for span in spans} == {"doc-1:recipe-a", "doc-1:recipe-b"}


def test_same_elements_of_different_documents_are_not_merged():
    """文書が違えば、同じ要素の ID と本文でもまとめない。"""
    left = _record("doc-1:a", _ids(0, 1), _text(0, 1))
    right = _record("doc-9:a", _ids(0, 1), _text(0, 1), document_id="doc-9")

    assert source_span_relation(left, right) is None


def test_evidence_without_ranges_uses_the_identical_text_rule():
    """要素の ID の無い分割（文字数での分割など）の根拠は、本文が同じものだけをまとめる今の規則で判定する。"""
    fixed_a = _record("doc-1:fixed-1", None, _text(0, 1, 2))
    fixed_b = _record("doc-1:fixed-2", None, _text(1, 2))
    same_text = _record("doc-1:fixed-3", None, _text(0, 1, 2))

    assert record_element_ids(fixed_a) == frozenset()
    spans = evidence_spans("経費精算の申請方法", [fixed_a, fixed_b, same_text])
    assert {span["source_id"] for span in spans} == {"doc-1:fixed-1", "doc-1:fixed-2"}
    assert next(s for s in spans if s["source_id"] == "doc-1:fixed-1")["source_aliases"] == ["doc-1:fixed-1", "doc-1:fixed-3"]


def test_element_ids_fall_back_to_source_record_refs():
    """親子分割の親は、子の source_record_refs の record_id を要素の ID として使う。"""
    record = _record("doc-1:p", None, "本文", refs=[{"record_id": "docling-p3-1"}, {"record_id": "docling-p3-2"}])

    assert record_element_ids(record) == frozenset({"docling-p3-1", "docling-p3-2"})
    assert record_element_ids(_record("doc-1:q", "docling-p3-1, docling-p3-2", "本文")) == frozenset({"docling-p3-1", "docling-p3-2"})


def test_another_recipe_of_the_same_passage_is_not_new_evidence():
    """CRAG の再検索で、取得済みの箇所の別レシピの根拠が出ても新しい根拠と数えない。"""
    seen = [_record("doc-1:parent-a", _ids(0, 1, 2, 3, 4), _text(0, 1, 2, 3, 4))]
    same_passage = _record("doc-1:structure-b", _ids(1, 2), _text(1, 2))
    different = _record("doc-1:structure-c", _ids(7, 8), "別の章の本文です。" * 3)

    assert new_evidence_count([same_passage], seen) == 0
    assert new_evidence_count([same_passage, different, different], seen) == 1
