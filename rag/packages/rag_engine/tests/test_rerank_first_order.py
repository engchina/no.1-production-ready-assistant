"""rerank の点数を候補の選択・並べ替えの主キーにする（#3504）。件数・合成の点数・語の一致は同点の順だけ。"""

from dataclasses import replace
from types import SimpleNamespace

from rag_engine.generation import answering as a
from rag_engine.generation.answer_records import _records_by_rerank_ranks
from rag_engine.generation.crag_support import _merge_crag_evidence
from rag_engine.models.llm import RerankTextRank
from rag_engine.retrieval.context_builder import ContextBuildRequest, build_chunk_context_bundle
from rag_engine.retrieval.rerank_scores import best_rerank_score, question_rerank_score, rerank_first_key, rerank_score
from tests.test_document_selection import child, parent


def _bundle(children, parents, *, top_k=20, max_records=12):
    return build_chunk_context_bundle(
        ContextBuildRequest(
            question="質問",
            ranked_children=children,
            active_records=[*children, *parents],
            top_k=top_k,
            neighbor_child_count=0,
            support_record_limit=0,
            max_anchors_per_parent=2,
            max_records=max_records,
            max_chars=100000,
        )
    )


def test_rerank_score_helpers_read_the_ranking_and_the_original_question_scores():
    scored = child("c", 1, "本文", "a.pdf", "p", 0.8, question_score=0.6)
    plain = child("d", 2, "本文", "a.pdf", "p")
    assert (rerank_score(scored), question_rerank_score(scored)) == (0.8, 0.6)
    assert question_rerank_score(child("e", 3, "本文", "a.pdf", "p", 0.7)) == 0.7  # 検索文が無ければ同じ値
    assert rerank_score(plain) is None and best_rerank_score([plain]) is None
    assert best_rerank_score([plain, scored]) == 0.8
    assert sorted([None, 0.2, 0.9], key=rerank_first_key) == [0.9, 0.2, None]


def test_rerank_metadata_keeps_the_original_question_score():
    records = [child("a", 1, "A", "a.pdf", "pa"), child("b", 2, "B", "b.pdf", "pb")]
    ranks = [RerankTextRank(1, 0.9, question_relevance_score=0.3), RerankTextRank(0, 0.5)]
    ranked = _records_by_rerank_ranks(records, ranks, SimpleNamespace(rerank_min_relevance_score=0.0))
    assert [(r.id, r.metadata["rerank"]["relevance_score"], r.metadata["rerank"]["question_relevance_score"]) for r in ranked] == [
        ("b", 0.9, 0.3),
        ("a", 0.5, 0.5),
    ]


def test_parent_with_one_high_reranked_anchor_is_not_pushed_out_by_a_parent_with_more_anchors():
    """起点 20 件が 12 を超える親に分かれても、親の枠（max_records）は起点の rerank の点数の順で切る。

    以前は起点の数の加点（+0.35/件）で、rerank 18・19 位の起点 2 件の親が 7 番目に上がり、rerank 12 位の
    起点 1 件の親が枠の外に落ちていた。
    """
    children, parents = [], []
    for i in range(20):
        score = round(0.95 - i * 0.02, 3)
        if i in (17, 18):
            children.append(child(f"c{i}", i + 1, f"本文{i}", "d.pdf", "pz", score))
        else:
            children.append(child(f"c{i}", i + 1, f"本文{i}", f"d{i}.pdf", f"p{i}", score))
            parents.append(parent(f"p{i}", i + 1, f"本文{i}", f"d{i}.pdf"))
    parents.append(parent("pz", 18, "本文17 本文18", "d.pdf"))

    bundle = _bundle(children, parents)

    assert [r.id for r in bundle.records] == [f"p{i}" for i in range(12)]


def test_parents_follow_the_best_anchor_in_the_rerank_order():
    """親の並びは起点の最上位の順位（rerank の後の並び）。起点の数・前後の chunk の数は同点の順だけ。

    rerank の工程の明示の配置（位置を保つ候補など）は rerank の後の並びのまま。rerank が動かなかったときは
    取得の順位。
    """
    children = [
        child("u1", 1, "位置を保つ候補", "u.pdf", "pu"),
        child("s1", 2, "本文", "s.pdf", "ps", 0.6),
        child("s2", 3, "本文", "t.pdf", "pt", 0.5),
        child("s3", 4, "本文", "t.pdf", "pt", 0.4),
        child("u2", 5, "位置を保つ候補", "v.pdf", "pv"),
    ]
    parents = [
        parent(name, i, "本文", source)
        for i, (name, source) in enumerate(zip(["pu", "ps", "pt", "pv"], ["u.pdf", "s.pdf", "t.pdf", "v.pdf"], strict=True), start=1)
    ]
    assert [r.id for r in _bundle(children, parents, max_records=3).records] == ["pu", "ps", "pt"]


def _context(records, children):
    return a.AnswerContext(records=list(records), text="", ranked_candidates=tuple(children))


def test_crag_merge_cuts_by_rerank_score_not_by_lexical_overlap():
    """CRAG の併合の満杯時の切りは rerank の点数の順。語の一致の点数は同点の順だけ。"""
    question = "登録の手順"
    previous = [replace(parent(f"p{i}", i, "登録の手順 登録の手順" if i else "別の説明", "a.pdf"), metadata={}) for i in range(2)]
    current = [replace(parent(f"q{i}", 10 + i, "登録の手順", "b.pdf"), metadata={}) for i in range(2)]
    scores = {"p0": 0.95, "p1": 0.2, "q0": 0.5, "q1": None}
    for records in (previous, current):
        for index, record in enumerate(records):
            if scores[record.id] is not None:
                records[index] = replace(record, metadata={"rerank": {"relevance_score": scores[record.id]}})

    merged = _merge_crag_evidence(_context(previous, ()), _context(current, ()), question, max_records=1, max_chars=10000)

    # 語の一致の少ない p0（rerank 0.95）を残し、語の一致の多い p1（0.2）・点数の無い q1 を切る。
    assert [r.id for r in merged.records] == ["p0", "q0"]


def test_top_rerank_score_is_the_highest_score_not_the_first_candidate():
    """画面名の先頭寄せ・位置を保つ候補で先頭が最高点とは限らない。"""
    candidates = [
        child("pinned", 1, "画面", "a.pdf", "pa", 0.2),
        child("best", 2, "本文", "b.pdf", "pb", 0.9),
        child("u", 3, "x", "c", "pc"),
    ]
    assert a._top_rerank_score(_context((), candidates)) == 0.9
    assert a._top_rerank_score(_context((), [child("u", 1, "x", "c", "pc")])) is None


def test_evidence_items_without_a_tree_are_ordered_by_rerank_score_before_rrf():
    """根拠の木の無い根拠の一覧は、回答に使った順の次に rerank の点数の順。RRF は同点の順だけ。"""
    from rag_engine.generation.answer_payload import answer_evidence_items

    def record(name, rerank, rrf):
        metadata = {"adb_hybrid": {"rrf_score": rrf}}
        if rerank is not None:
            metadata["rerank"] = {"relevance_score": rerank}
        return replace(parent(name, 1, f"{name} の本文", f"{name}.pdf"), metadata=metadata)

    items = answer_evidence_items([record("high_rrf", 0.2, 0.9), record("high_rerank", 0.9, 0.1), record("unscored", None, 0.95)])
    assert [item["id"] for item in items] == ["high_rerank", "high_rrf", "unscored"]


# ---- rerank 最優先の例外: 条の目録で選んだ条（#2254。利用者の判断 2026-10-26） ----


def _scored(record, score):
    return replace(record, metadata={**record.metadata, "rerank": {"relevance_score": score}})


def test_catalog_articles_with_low_rerank_scores_still_reach_the_context():
    """rerank の点数の高い起点で枠が埋まっても、目録で選んだ条（点数 0.01）は枠の外に足される。"""
    from tests.test_article_catalog import _bundle as catalog_bundle
    from tests.test_article_catalog import _context_record

    children = [_scored(_context_record(f"m{i}", "child"), 0.9 - i * 0.01) for i in range(6)]
    article = _scored(_context_record("art1", "child", article=True), 0.01)

    bundle = catalog_bundle([*children, article], top_k=3, max_records=3, catalog_records=2)

    assert [r.id for r in bundle.records] == ["m0-p", "m1-p", "m2-p", "art1-p"]
    assert bundle.evidence_tree[-1].reason == "catalog_article"


def test_crag_merge_keeps_catalog_article_parents_outside_the_rerank_cut():
    """CRAG の併合の満杯時の切り（rerank の点数の順）でも、目録で選んだ条の親は切らない。"""
    from tests.test_article_catalog import _context_record

    def context(names, scores, catalog=()):
        children = [
            _scored(_context_record(name, "child", article=name in catalog), score) for name, score in zip(names, scores, strict=True)
        ]
        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="質問",
                ranked_children=children,
                active_records=[*children, *(_context_record(name, "parent") for name in names)],
                top_k=len(names),
                neighbor_child_count=0,
                max_records=len(names),
                max_chars=5000,
                catalog_records=len(catalog),
            )
        )
        return a.AnswerContext(records=list(bundle.records), text=bundle.text, evidence_tree=bundle.evidence_tree)

    previous = context(["m0", "art9"], [0.9, 0.01], catalog=("art9",))
    current = context(["m1", "m2"], [0.8, 0.7])

    merged = _merge_crag_evidence(previous, current, "質問", max_records=1, max_chars=10000)

    assert [r.id for r in merged.records] == ["m0-p", "m1-p", "art9-p"]


def test_document_selection_does_not_touch_catalog_articles():
    from rag_engine.config import get_settings
    from tests.test_article_catalog import _marked

    selection = replace(get_settings(environ={}, dotenv_path=None), document_selection_enabled=True)
    top = child("m1", 1, "手引き", "a.pdf", "p1", 0.9)
    article = _marked(child("art1", 30, "第一条", "架空規程.json", "q1", 0.01))
    kept, deferred, trace = a.select_documents([top, article], selection, top_k=1)
    assert article in kept and deferred == []
    assert all(entry["source"] != "架空規程.json" for entry in [*trace["selected"], *trace["deferred"]])


def test_evidence_items_with_catalog_articles_keep_the_original_order():
    """条の目録で選んだ条を含む根拠の一覧（根拠の木の無い経路）は、rerank の点数を使わない元の順。"""
    from rag_engine.generation.answer_payload import answer_evidence_items
    from tests.test_article_catalog import _marked

    def record(name, rerank, rrf, article=False):
        metadata = {"adb_hybrid": {"rrf_score": rrf}, "rerank": {"relevance_score": rerank}}
        built = replace(parent(name, 1, f"{name} の本文", f"{name}.pdf"), metadata=metadata)
        return _marked(built) if article else built

    items = answer_evidence_items([record("art", 0.01, 0.9, article=True), record("high_rerank", 0.9, 0.1)])
    assert [item["id"] for item in items] == ["art", "high_rerank"]
