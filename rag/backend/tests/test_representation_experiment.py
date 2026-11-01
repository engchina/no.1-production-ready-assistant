"""検索文の文脈補足と複数の検索表現の比較の実験（#75）。

埋め込み・再ランクは文字の 2-gram の決定的な台本、生成は台本で、モデルの API を呼ばない。
資料と質問は同梱の合成の評価セット（rag/evaluation/retrieval-representations）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.rag.representation_experiment import (
    DEFAULT_DATASET,
    VARIANTS,
    Aids,
    ContextOutput,
    Dataset,
    Evidence,
    Index,
    QuestionsOutput,
    _bigrams,
    expected_keys,
    html_records,
    load_cases,
    load_corpus,
    main,
    representations,
    rrf,
    run_experiment,
    search,
    unsupported_values,
)


def _vector(text: str) -> list[float]:
    vector = [0.0] * 64
    for gram in _bigrams(text):
        vector[int(hashlib.sha256(gram.encode()).hexdigest(), 16) % 64] += 1.0
    return vector


class FakeClient:
    def __init__(self) -> None:
        self.embedded: list[str] = []
        self.reranked: list[list[str]] = []

    async def embed(
        self, texts: list[str], *, input_type: Any = "SEARCH_DOCUMENT"
    ) -> list[list[float]]:
        self.embedded.extend(texts)
        return [_vector(text) for text in texts]

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        self.reranked.append(documents)
        terms = set(_bigrams(query))
        scores = [len(terms & set(_bigrams(doc))) / (len(terms) or 1) for doc in documents]
        order = sorted(range(len(documents)), key=lambda i: (-scores[i], i))
        return [(i, scores[i]) for i in order[:top_n]]


def fake_generate(system: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
    data = json.loads(prompt)
    title = data["document"].splitlines()[0].lstrip("# ")
    if schema is ContextOutput:
        return ContextOutput(context=f"{title}の断片。")
    return QuestionsOutput(questions=[f"{title}について {data['fragment'][:12]}"])


def _bundled() -> tuple[list[Evidence], list[Any]]:
    dataset = Dataset.model_validate_json((DEFAULT_DATASET / "cases.json").read_text("utf-8"))
    evidence, _documents = load_corpus(DEFAULT_DATASET, dataset)
    return evidence, load_cases(DEFAULT_DATASET, dataset, evidence)


def test_every_case_points_at_children_of_the_bundled_corpus() -> None:
    evidence, cases = _bundled()
    documents = {item.document for item in evidence}
    assert len(documents) == 28
    # 新しい版に置き換わった文書は索引に入れない（配信中の文書だけ）。
    assert "approval-rules-2023.html" not in documents
    assert len({item.key for item in evidence}) == len(evidence)
    counts = Counter(case.group for case in cases)
    assert counts["lost_subject_deep"] == counts["paraphrase"] == counts["lost_subject_title"] == 10
    assert counts["multi_hop_existing"] == 21
    for case in cases:
        keys = expected_keys(case, evidence)
        assert bool(keys) == bool(case.evidence), case.id
        if case.group != "multi_hop_existing":
            assert len(keys) == len(case.evidence), case.id
    # 主語を失った断片の群は、根拠の child の本文にも製品の検索文にも主語が無い
    # （文脈補足が要る群）。
    by_key = {item.key: item for item in evidence}
    subjects = {
        "システム運用手順書.md": "StockNavi",
        "製品保証規定.md": "HM-50",
        "拠点別防災手順.md": "大阪支店",
        "研修制度.md": "中途入社者研修",
    }
    for case in cases:
        if case.group == "lost_subject_deep":
            [key] = expected_keys(case, evidence)
            item = by_key[key]
            assert subjects[item.document] not in item.body + item.retrieval_text, case.id


def test_html_records_keep_headings_paragraphs_lists_and_table_rows() -> None:
    records = html_records(
        "<html><head><title>t</title><style>p{}</style></head><body><h1>規程</h1>"
        "<h2>第 1 章</h2><p>本文の\n段落</p><ul><li>項目</li></ul>"
        "<table><tr><th>列</th><th>値</th></tr><tr><td>A</td><td>1</td></tr></table></body></html>"
    )
    assert [(r["category"], r["text"]) for r in records] == [
        ("Title", "規程"),
        ("Section-header", "第 1 章"),
        ("Text", "本文の 段落"),
        ("List-item", "項目"),
        ("Text", "列 | 値"),
        ("Text", "A | 1"),
    ]


def test_same_body_in_two_documents_stays_two_evidence() -> None:
    first = Evidence("a.md", "chunk-docling-c000001", "同じ本文", ("見出し",), "検索文 a")
    second = Evidence("b.md", "chunk-docling-c000001", "同じ本文", ("見出し",), "検索文 b")
    assert first.key != second.key
    assert expected_keys(
        Dataset.model_validate(
            {
                "version": 1,
                "groups": {},
                "cases": [
                    {
                        "id": "x",
                        "group": "g",
                        "question": "q",
                        "evidence": [{"document": "b.md", "quote": "同じ本文"}],
                    }
                ],
            }
        ).cases[0],
        [first, second],
    ) == {second.key}


def test_representations_keep_the_original_text_as_the_evidence() -> None:
    item = Evidence("a.md", "c1", "本文", ("章", "節"), "Source file: a.md\nChild text:\n本文")
    aids = Aids(contexts={item.key: "文脈"}, questions={item.key: ["質問1", "質問2"]})
    assert representations(item, "body", aids) == ["本文"]
    assert representations(item, "structure", aids)[0].startswith(
        "Source file: a.md\nSection path: 章 > 節"
    )
    assert representations(item, "generated_context", aids)[0].startswith("Context: 文脈\n")
    assert representations(item, "multi_representation", aids) == [
        item.retrieval_text,
        "質問1",
        "質問2",
    ]
    # 生成に失敗した child は補助なしで製品の検索文のまま。
    assert representations(item, "generated_context", Aids()) == [item.retrieval_text]
    # 再ランク・返す根拠は原文（見出し + 本文）で、生成した補助を含まない。
    assert item.rerank_text == "見出し: 章 > 節\n\n本文"


def test_uncollapsed_representations_inflate_votes_but_collapsed_do_not() -> None:
    evidence = {
        "a#1": Evidence("a", "1", "経費の精算の期限", (), "経費の精算の期限"),
        "b#1": Evidence("b", "1", "経費の精算の方法", (), "経費の精算の方法"),
    }
    # b は同じ表現を 3 つ持つ。
    texts = ["経費の精算の期限", "経費の精算の方法", "経費の精算の方法", "経費の精算の方法"]
    owners = ["a#1", "b#1", "b#1", "b#1"]
    from app.rag.representation_experiment import Bm25

    client = FakeClient()
    vectors = [_vector(text) for text in texts]

    def run(variant: str) -> dict[str, Any]:
        index = Index(variant, owners, texts, vectors, Bm25(texts))
        documents = {key: item.rerank_text for key, item in evidence.items()}
        return asyncio.run(
            search(index, "経費の精算の期限", _vector("経費の精算の期限"), documents, client)
        )

    collapsed = run("multi_representation")
    uncollapsed = run("multi_representation_uncollapsed")
    assert collapsed["hybrid"][0] == "a#1"
    assert (
        collapsed["hybrid_duplicates"] == 2
    )  # 2 つの channel に同じ 2 つの child（重複は channel の間だけ）
    assert uncollapsed["hybrid_duplicates"] > collapsed["hybrid_duplicates"]
    # 返す並びは child ごとに 1 回（同じ根拠を何度も表示しない）。
    assert len(set(uncollapsed["hybrid"])) == len(uncollapsed["hybrid"])
    assert rrf(["b#1", "b#1", "a#1"], ["a#1"])[0] == "b#1"


def test_unsupported_values_flag_numbers_and_codes_not_in_the_document() -> None:
    document = "保存期間は14日とする。様式K-17 を使う。"
    assert unsupported_values("保存期間は14日。K-17 の様式。", document) == []
    assert unsupported_values("保存期間は30日。様式K-71。", document) == ["30", "K-71"]


def test_experiment_runs_every_variant_and_pairs_against_current(tmp_path: Path) -> None:
    client = FakeClient()
    report = asyncio.run(run_experiment(DEFAULT_DATASET, client, fake_generate))
    assert set(report["summary"]) == set(VARIANTS)
    final = report["summary"]["generated_context"]["hybrid_rerank"]
    assert final["all"]["cases"] == 70  # 答えの無い 6 問を除く
    assert set(final["lost_subject_deep"]["vs_current"]["hit_at_5"]) >= {
        "verdict",
        "p_value",
        "interval",
    }
    assert "vs_current" not in report["summary"]["current"]["hybrid_rerank"]["all"]
    assert report["summary"]["current"]["unanswerable_top_rerank_score"] is not None
    assert (
        report["indexes"]["multi_representation"]["entries"]
        > report["indexes"]["current"]["entries"]
    )
    children = report["manifest"]["children"]
    assert report["generation"]["children_with_context"] == children
    # 再ランクには原文を渡す。生成した文脈を足すのは generated_context_rerank の 76 問の
    # 呼び出しだけで、
    # 返す根拠（final）は child の key（原文）で、生成した文を含まない。
    contexts = set(report["generation"]["contexts"].values())
    with_context = [
        docs for docs in client.reranked if any(c in doc for doc in docs for c in contexts)
    ]
    assert len(with_context) == 76
    assert all(doc.startswith("文脈: ") for docs in with_context for doc in docs)
    final = report["cases"]["generated_context_rerank"]["deep-01"]["final"]
    assert all("#" in key for key in final)


def test_cli_records_and_replays_without_calling_the_model(tmp_path: Path) -> None:
    recording = tmp_path / "recording.json"
    output = tmp_path / "report.json"

    class RecordedClient(FakeClient):
        """埋め込みを記録の session に通す（製品の client と同じく external_call を使う）。"""

        async def embed(
            self, texts: list[str], *, input_type: Any = "SEARCH_DOCUMENT"
        ) -> list[list[float]]:
            from rag_engine.resources.external_calls import external_call

            result = external_call(
                "embeddings", {"texts": texts}, lambda: {"data": [_vector(t) for t in texts]}
            )
            return list(result["data"])

    def dependencies() -> tuple[Any, Any, dict[str, Any]]:
        return RecordedClient(), fake_generate, {"embedding": "fake"}

    assert (
        main(["--record", str(recording), "--output", str(output)], dependencies=dependencies) == 0
    )
    live = json.loads(output.read_text("utf-8"))
    replayed = tmp_path / "replay.json"
    assert (
        main(["--replay", str(recording), "--output", str(replayed)], dependencies=dependencies)
        == 0
    )
    again = json.loads(replayed.read_text("utf-8"))
    assert again["summary"] == live["summary"]
    assert again["manifest"]["external_calls"]["mode"] == "replay"

    def other() -> tuple[Any, Any, dict[str, Any]]:
        class Other(RecordedClient):
            async def embed(
                self, texts: list[str], *, input_type: Any = "SEARCH_DOCUMENT"
            ) -> list[list[float]]:
                return await super().embed([text + "!" for text in texts], input_type=input_type)

        return Other(), fake_generate, {}

    # 記録と違う入力は、実の API に接続せずに失敗する。
    assert main(["--replay", str(recording), "--output", str(replayed)], dependencies=other) == 2


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_variant_has_representations(variant: str) -> None:
    item = Evidence("a.md", "c1", "本文", (), "検索文")
    assert representations(item, variant, Aids())
