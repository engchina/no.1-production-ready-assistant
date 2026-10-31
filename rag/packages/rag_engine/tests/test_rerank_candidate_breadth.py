"""rerank は取得できた候補すべてを並べ替える (#1084)。

定数で幅を固定すると、取得幅が top_k に応じて広がっても後ろの候補は一段目の順位のまま残り、
context の起点が先頭 top_k 件で埋まるため、質問に合致していても context に入らない。
"""

from types import SimpleNamespace

import rag_engine.generation.answering as answering
from rag_engine.generation.answering import AnswerRecord, rerank_records


def _child(index):
    return AnswerRecord(
        id=f"c{index}",
        engine="docling",
        engine_label="Docling",
        page=index,
        seq_no=index,
        category="Chunk",
        text=f"抽出条件を選んで実行ボタンを押します。{index}",
        source="架空仕入業務.pdf",
        chunk_level="child",
        chunk_id=f"c{index}",
        chunk_uid=f"uid:c{index}",
        source_run_id="run",
        metadata={},
    )


def _settings():
    return SimpleNamespace(rerank_model="rerank-test", rerank_enabled=True, document_selection_enabled=False)


def _stub_rerank(monkeypatch, seen):
    """rerank model の呼び出しを記録し、最後の候補を 1 位に上げた結果を返す。"""
    monkeypatch.setattr(answering, "_rerank_configured", lambda settings: True)

    def fake(question, documents, settings, top_n=None):
        seen.append(len(documents))
        order = [len(documents) - 1, *range(len(documents) - 1)]
        return [SimpleNamespace(index=index, relevance_score=1.0 - position / 1000) for position, index in enumerate(order)]

    monkeypatch.setattr(answering, "rerank_text_with_scores", fake)


def test_all_retrieved_candidates_are_reranked_by_default(monkeypatch):
    seen: list[int] = []
    _stub_rerank(monkeypatch, seen)
    records = [_child(i) for i in range(150)]

    ranked = rerank_records("抽出条件の指定方法", records, _settings(), enabled=True, business_domains=(), screen_terms=())

    assert seen == [150]
    # 150 件目が rerank で 1 位に上がる。幅を 50 件に固定していると末尾のままになる。
    assert ranked[0].id == "c149"


def test_an_explicit_candidate_limit_still_caps_the_reranked_range(monkeypatch):
    seen: list[int] = []
    _stub_rerank(monkeypatch, seen)
    records = [_child(i) for i in range(150)]

    ranked = rerank_records(
        "抽出条件の指定方法", records, _settings(), enabled=True, candidate_limit=50, business_domains=(), screen_terms=()
    )

    assert seen == [50]
    assert ranked[0].id == "c49"
    assert len(ranked) == 150  # 対象外の候補は落とさず取得順で後ろに残す
