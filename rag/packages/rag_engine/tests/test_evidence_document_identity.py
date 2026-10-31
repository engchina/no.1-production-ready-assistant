"""根拠の重複除去と文書の背景を、表示名ではなく文書の識別子で分ける (#2603)。

同じファイル名・頁・章節・本文でも、文書 ID が違えば別の根拠にする。文書 ID が無く表示名からしか文書を
決められない根拠はまとめない。同じ文書の複数レシピの同じ範囲は、これまでどおり 1 つにまとめる。
"""

from rag_engine.generation.answering import AnswerRecord
from rag_engine.retrieval.context_builder import context_bundle_from_records
from rag_engine.retrieval.evidence_selection import (
    collapse_source_spans,
    evidence_spans,
    new_evidence_count,
    record_fingerprint,
)
from rag_engine.retrieval.metadata_context import document_context_key, document_identity

TEXT = "合成の段落です。申請は画面 A の保存ボタンで確定します。"
QUESTION = "合成の段落の保存の操作"


def _record(uid, *, document_id=None, document=None, section=("章 1",), text=TEXT, page=1, element_ids=None):
    metadata = {"section_path": list(section)}
    if document_id is not None:
        metadata["document_id"] = document_id
    if document is not None:
        metadata["document"] = dict(document)
    if element_ids is not None:
        metadata["element_ids"] = list(element_ids)
    return AnswerRecord(
        id=uid,
        engine="docling",
        engine_label="Docling",
        page=page,
        seq_no=1,
        category="ParentChunk",
        text=text,
        source="sample.pdf",
        source_run_id="run",
        chunk_id=uid,
        chunk_uid=uid,
        chunk_level="parent",
        metadata=metadata,
    )


def _first_page(text):
    return {
        "page": 1,
        "status": "available",
        "text": text,
        "engine": "docling",
        "record_ids": ["r1"],
        "truncated": False,
    }


def test_same_name_documents_with_different_ids_are_separate_evidence():
    """同じ表示名・頁・章節・本文でも、文書 ID が違えば fingerprint・出典・別名を分ける。"""
    left = _record("document-a:group-a", document_id="document-a")
    right = _record("document-b:group-a", document_id="document-b")

    assert record_fingerprint(left) != record_fingerprint(right)
    spans = evidence_spans(QUESTION, [left, right])

    assert [span["source_id"] for span in spans] == ["document-a:group-a", "document-b:group-a"]
    assert [span["source_aliases"] for span in spans] == [["document-a:group-a"], ["document-b:group-a"]]
    assert spans[0]["document_scope"] != spans[1]["document_scope"]
    assert new_evidence_count([right], [left]) == 1


def test_same_passage_of_the_same_document_from_two_recipes_is_merged():
    """同じ文書（同じ文書 ID）の同じ範囲の本文は、別のレシピの chunk でも 1 つにまとめ、別名に残す。"""
    recipe_a = _record("document-a:recipe-a", document_id="document-a")
    recipe_b = _record("document-a:recipe-b", document_id="document-a")

    assert record_fingerprint(recipe_a) == record_fingerprint(recipe_b)
    spans = evidence_spans(QUESTION, [recipe_a, recipe_b])

    assert [span["source_aliases"] for span in spans] == [["document-a:recipe-a", "document-a:recipe-b"]]
    assert new_evidence_count([recipe_b], [recipe_a]) == 0


def test_old_and_new_versions_are_not_merged():
    """旧版と新版（文書 ID と有効開始日が違う）は、同じ名前・本文でもまとめない。"""
    old = _record("document-old:g", document_id="document-old", document={"effective_from": "2020-04-01"})
    new = _record("document-new:g", document_id="document-new", document={"effective_from": "2024-04-01"})

    spans = evidence_spans(QUESTION, [old, new])

    assert {span["source_id"] for span in spans} == {"document-old:g", "document-new:g"}
    assert all(len(span["source_aliases"]) == 1 for span in spans)


def test_evidence_without_a_certain_document_identity_is_not_merged():
    """文書 ID が無く、ファイル名から導いた識別子・表示名しか無い根拠は、同じ本文でもまとめない。"""
    by_name = {"source_document_id": "source-from-name", "identity_source": "local_filename"}
    left = _record("run-a:1", document=by_name)
    right = _record("run-b:1", document=by_name)
    bare_left, bare_right = _record("run-c:1"), _record("run-d:1")

    assert document_identity(left) == ""
    assert record_fingerprint(left) != record_fingerprint(right)
    assert len(evidence_spans(QUESTION, [left, right])) == 2
    assert len(evidence_spans(QUESTION, [bare_left, bare_right])) == 2
    # 同じ record を 2 回渡したときは 1 つ（record の ID で隔離するだけで、同じ根拠は重ねない）。
    assert len(evidence_spans(QUESTION, [bare_left, bare_left])) == 1


def test_explicit_source_document_id_is_a_certain_identity():
    """管理側で明示した source_document_id は文書の識別子として使い、同じ文書の同じ本文はまとめる。"""
    explicit = {"source_document_id": "managed-1", "identity_source": "explicit"}
    left = _record("run-a:1", document=explicit)
    right = _record("run-b:1", document=explicit)

    assert document_identity(left) == "source:managed-1"
    assert len(evidence_spans(QUESTION, [left, right])) == 1


def test_document_id_takes_precedence_over_a_shared_source_document_id():
    """ファイル名から導いた source_document_id が同じでも、文書 ID が違えば別の文書。"""
    by_name = {"source_document_id": "source-from-name", "identity_source": "local_filename"}
    left = _record("document-a:g", document_id="document-a", document=by_name)
    right = _record("document-b:g", document_id="document-b", document=by_name)

    assert document_identity(left) == "document:document-a"
    assert document_context_key(left) != document_context_key(right)
    assert len(evidence_spans(QUESTION, [left, right])) == 2


def test_different_sections_of_the_same_document_are_not_merged():
    """同じ文書の同じ本文でも、章節が違えば別の根拠にする。"""
    first = _record("document-a:g1", document_id="document-a", section=("章 1",))
    second = _record("document-a:g2", document_id="document-a", section=("章 2",))

    assert len(evidence_spans(QUESTION, [first, second])) == 2


def test_source_span_collapse_requires_the_same_document_identity():
    """複数レシピの範囲の統合（要素の ID の重なり）も、文書の識別子が同じときだけ行う。"""
    ids = ["el-1", "el-2"]
    same_a = _record("document-a:recipe-a", document_id="document-a", element_ids=ids)
    same_b = _record("document-a:recipe-b", document_id="document-a", element_ids=ids, text=TEXT + "\n")
    other = _record("document-b:recipe-a", document_id="document-b", element_ids=ids)
    unknown_a = _record("run-a:1", element_ids=ids)
    unknown_b = _record("run-b:1", element_ids=ids, text=TEXT + "\n")

    assert [len(members) for _, members in collapse_source_spans([same_a, same_b])] == [2]
    assert [len(members) for _, members in collapse_source_spans([same_a, other])] == [1, 1]
    assert [len(members) for _, members in collapse_source_spans([unknown_a, unknown_b])] == [1, 1]


def test_each_same_name_document_gets_its_own_first_page_background():
    """文書の背景（1 ページ目）は文書ごとに 1 回。同名の別文書の背景を省いたり混ぜたりしない。"""
    left = _record("document-a:g", document_id="document-a", page=3, document={"first_page_context": _first_page("背景 A の合成の本文。")})
    right = _record(
        "document-b:g",
        document_id="document-b",
        page=3,
        text="合成の別の段落です。",
        document={"first_page_context": _first_page("背景 B の合成の本文。")},
    )
    again = _record(
        "document-a:g2",
        document_id="document-a",
        page=4,
        text="合成の 3 つ目の段落です。",
        document={"first_page_context": _first_page("背景 A の合成の本文。")},
    )

    spans = evidence_spans(QUESTION, [left, right, again])
    contexts = {span["source_id"]: span.get("answer_context", "") for span in spans}

    assert "背景 A" in contexts["document-a:g"] and "背景 B" not in contexts["document-a:g"]
    assert "背景 B" in contexts["document-b:g"] and "背景 A" not in contexts["document-b:g"]
    # 同じ文書の 2 つ目の根拠には、背景を繰り返さない。
    assert "First-page background" not in contexts["document-a:g2"]

    bundle = context_bundle_from_records([left, right, again], max_chars=20000)
    assert bundle.text.count("背景 A") == 1
    assert bundle.text.count("背景 B") == 1
