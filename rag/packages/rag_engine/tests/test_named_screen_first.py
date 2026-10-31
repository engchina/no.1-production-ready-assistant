"""質問が画面名を名指ししたとき、その画面名を節見出しに持つ候補を rerank 後に先頭へ寄せる (#1044)。"""

from types import SimpleNamespace

import rag_engine.generation.answering as answering
from rag_engine.generation.answering import AnswerRecord, rerank_records


def _child(index, heading, source="架空仕入業務.pdf"):
    return AnswerRecord(
        id=f"c{index}",
        engine="docling",
        engine_label="Docling",
        page=index,
        seq_no=index,
        category="Chunk",
        text=f"抽出対象を選ぶと処理内容の選択肢が変わります。{index}",
        source=source,
        chunk_level="child",
        chunk_id=f"c{index}",
        chunk_uid=f"uid:c{index}",
        source_run_id="run",
        metadata={"section_path": ["９架空仕入業務", heading, "B 【操作説明】"]},
    )


QUESTION = "月次締め処理を実行した後で、仕入単価を訂正したい。"


def RECORDS():
    return [
        _child(1, "（Ｓ）仕入明細訂正"),
        _child(2, "（２）－（Ｄ）月次締め処理"),
        _child(3, "（Ｘ）仕入内容確定処理"),
        _child(4, "（１）－（Ｄ） 月次締め処理", source="架空経理業務.pdf"),
    ]


def test_candidates_under_the_named_screen_heading_come_first_in_rank_order():
    kept = rerank_records(QUESTION, RECORDS(), None, business_domains=(), screen_terms=("月次締め処理",))

    assert [record.id for record in kept] == ["c2", "c4", "c1", "c3"]  # 全角・空白の違いは無視、候補は落とさない


def test_no_matching_heading_keeps_the_order():
    kept = rerank_records(QUESTION, RECORDS(), None, business_domains=(), screen_terms=("在庫照会画面",))

    assert [record.id for record in kept] == ["c1", "c2", "c3", "c4"]


def test_all_candidates_matching_keeps_the_order():
    records = [_child(1, "（２）－（Ｄ）月次締め処理"), _child(2, "（１）－（Ｄ）月次締め処理")]

    assert [r.id for r in rerank_records(QUESTION, records, None, business_domains=(), screen_terms=("月次締め処理",))] == ["c1", "c2"]


def test_screen_terms_are_parsed_from_the_question_when_not_given():
    kept = rerank_records(QUESTION, RECORDS(), None, business_domains=())  # 「月次締め処理」は語尾の一般規則で取れる

    assert [record.id for record in kept] == ["c2", "c4", "c1", "c3"]


def test_parsed_conditions_without_screen_terms_do_not_reorder(monkeypatch):
    monkeypatch.setattr(answering, "parse_inquiry_conditions", lambda question: SimpleNamespace(business_domains=()))

    assert [r.id for r in rerank_records(QUESTION, RECORDS(), None)] == ["c1", "c2", "c3", "c4"]


def test_interrogative_phrases_are_not_taken_as_screen_names():
    """「どこから登録」「どのように設定」は画面名ではない。検索語と並び替えを疑問部分に引きずらせない (#1066)。"""
    from rag_engine.retrieval.inquiry_conditions import parse_inquiry_conditions

    cases = {
        "架空在庫照会画面の備考欄は、どこから登録できるか。": ("架空在庫照会画面",),
        "架空納品書に社印を印字しないようにしたい。どのように設定するのか。": (),
        "月次締め処理を実行した後で、仕入単価を訂正したい。": ("月次締め処理",),
    }
    for question, expected in cases.items():
        assert parse_inquiry_conditions(question).screen_terms == expected, question
