"""質問が業務を名指ししたとき、rerank 後の候補をその業務（フォルダ分類）の文書に絞る (#954)。"""

from types import SimpleNamespace

import rag_engine.generation.answering as answering
from rag_engine.generation.answering import AnswerRecord, rerank_records


def _child(index, large_category, source, business_domains=()):
    metadata = {"section_path": ["架空権限設定"]}
    if large_category is not None:
        metadata["document"] = {"classification": {"large_category": large_category}}
    # 本文由来の業務推定は別業務の文書でも他業務名を含み得るので、絞り込みの根拠にしないことを確かめる
    if business_domains:
        metadata["retrieval_profile"] = {"business_domains": list(business_domains)}
    return AnswerRecord(
        id=f"c{index}",
        engine="docling",
        engine_label="Docling",
        page=index,
        seq_no=index,
        category="Chunk",
        text=f"使用可能な処理を選び実行ボタンを押します。{index}",
        source=source,
        chunk_level="child",
        chunk_id=f"c{index}",
        chunk_uid=f"uid:c{index}",
        source_run_id="run",
        metadata=metadata,
    )


QUESTION = "倉庫連携メニューの権限を付与する操作を教えてほしい。"


def test_only_the_named_business_documents_remain_in_rank_order():
    other = _child(1, "20_業務B", "業務B操作説明書.pdf", business_domains=("業務A", "業務B"))
    first = _child(2, "10_業務A", "架空権限設定.pdf")
    second = _child(3, "10_業務A", "システム管理.pdf")

    kept = rerank_records(QUESTION, [other, first, second], None, business_domains=("業務A",))

    assert [record.id for record in kept] == ["c2", "c3"]


def test_numbered_category_and_plain_business_name_compare_equal():
    kept = rerank_records(QUESTION, [_child(1, "20_業務B", "b.pdf"), _child(2, "10_業務A", "a.pdf")], None, business_domains=("10_業務A",))

    assert [record.id for record in kept] == ["c2"]


def test_questions_without_a_business_keep_every_candidate():
    records = [_child(1, "20_業務B", "b.pdf"), _child(2, "10_業務A", "a.pdf")]

    assert [r.id for r in rerank_records(QUESTION, records, None, business_domains=())] == ["c1", "c2"]


def test_no_matching_candidate_keeps_every_candidate_instead_of_refusing():
    records = [_child(1, "20_業務B", "b.pdf"), _child(2, None, "unclassified.pdf")]

    assert [r.id for r in rerank_records(QUESTION, records, None, business_domains=("業務A",))] == ["c1", "c2"]


def test_multiple_named_businesses_keep_either_business():
    records = [_child(1, "20_業務B", "b.pdf"), _child(2, "30_業務C", "c.pdf"), _child(3, "10_業務A", "a.pdf")]

    kept = rerank_records(QUESTION, records, None, business_domains=("業務A", "業務B"))

    assert [record.id for record in kept] == ["c1", "c3"]


def test_business_is_parsed_from_the_question_when_not_given(monkeypatch):
    monkeypatch.setattr(answering, "parse_inquiry_conditions", lambda question: SimpleNamespace(business_domains=("業務A",)))
    records = [_child(1, "20_業務B", "b.pdf"), _child(2, "10_業務A", "a.pdf")]

    assert [r.id for r in rerank_records(QUESTION, records, None)] == ["c2"]
