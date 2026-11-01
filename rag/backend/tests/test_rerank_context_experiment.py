"""再ランクの入力に生成した文脈を足す効果の、回答の品質と大きな KB での実験（#101）。

埋め込み・再ランクは文字の 2-gram の決定的な台本、生成・判定は台本で、モデルの API を呼ばない。
資料と質問は同梱の合成の評価セット（rag/evaluation/retrieval-representations）と妨害の文書。
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from rag_engine.resources.external_calls import external_call, use_external_calls

from app.rag.representation_experiment import DEFAULT_DATASET, Evidence, _bigrams
from app.rag.rerank_context_experiment import (
    AVOID_TERMS,
    BASELINE,
    KBS,
    TREATMENT,
    WHOLE_DOCUMENT,
    AnswerOutput,
    AnswerParagraph,
    BudgetedSession,
    ContextBatchOutput,
    DistractorBatch,
    DistractorDraft,
    FragmentContext,
    TokenBudgetExceeded,
    adoption,
    answer_evidence,
    answer_units,
    context_batch_prompt,
    context_batches,
    context_cache_key,
    distractor_seeds,
    generate_distractors,
    load_distractors,
    load_experiment_corpus,
    main,
    render_answer,
    run_retrieval,
    summarize_answers,
)


def _vector(text: str) -> list[float]:
    vector = [0.0] * 32
    for gram in _bigrams(text):
        vector[int(hashlib.sha256(gram.encode()).hexdigest(), 16) % 32] += 1.0
    return vector


class FakeClient:
    """埋め込みを記録の session に通す台本（製品の client と同じく external_call を使う）。"""

    def __init__(self) -> None:
        self.reranked: list[list[str]] = []

    async def embed(
        self, texts: list[str], *, input_type: Any = "SEARCH_DOCUMENT"
    ) -> list[list[float]]:
        result = external_call(
            "embeddings", {"texts": texts}, lambda: {"data": [_vector(t) for t in texts]}
        )
        return list(result["data"])

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        self.reranked.append(documents)
        terms = set(_bigrams(query))
        scores = [len(terms & set(_bigrams(doc))) / (len(terms) or 1) for doc in documents]
        order = sorted(range(len(documents)), key=lambda i: (-scores[i], i))
        return [(i, scores[i]) for i in order[:top_n]]


def fake_generate(system: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
    data = json.loads(prompt)
    if schema is ContextBatchOutput:
        return ContextBatchOutput(
            contexts=[
                FragmentContext(
                    id=fragment["id"],
                    context=f"{document['document'].splitlines()[0].lstrip('# ')}の断片。",
                )
                for document in data["documents"]
                for fragment in document["fragments"]
            ]
        )
    if schema is AnswerOutput:
        first = data["evidence"][0]
        return AnswerOutput(
            insufficient=False,
            paragraphs=[AnswerParagraph(text=first["text"][:40], evidence_ids=[first["id"]])],
        )
    raise AssertionError(schema)


def fake_evaluate(
    case: Mapping[str, Any],
    answer: str,
    evidence: Sequence[Mapping[str, str]],
    cited: Sequence[str],
) -> Mapping[str, Any]:
    # 標準回答の先頭の 4 文字が回答にあれば正しいとする台本。
    correct = case["standard_answer"][:4] in answer
    return {
        "status": "completed",
        "answer_correctness": 1 if correct else 0,
        "requirement_coverage": 1.0 if correct else 0.0,
        "claim_support_rate": 1.0,
    }


def test_bundled_distractors_are_large_same_domain_and_avoid_question_subjects() -> None:
    distractors = load_distractors(DEFAULT_DATASET)
    assert distractors is not None
    assert len(distractors.documents) >= 2200
    corpus = load_experiment_corpus(DEFAULT_DATASET)
    assert len(corpus.small) == 215
    assert len(corpus.large) >= 2400
    seeds = {seed["name"] for seed in distractor_seeds(DEFAULT_DATASET)}
    assert {item.seed for item in distractors.documents} <= seeds
    for item in distractors.documents:
        assert not any(term in item.text for term in AVOID_TERMS), item.name
    # 根拠は小さい KB の child（妨害の文書を根拠にしない）。
    small = {item.key for item in corpus.small}
    assert all(keys <= small for keys in corpus.expected.values())
    standard = json.loads((DEFAULT_DATASET / "standard-answers.json").read_text("utf-8"))
    assert {case.id for case in corpus.cases} == set(standard["answers"])


def test_distractor_generation_drops_avoided_terms_duplicates_and_bad_lengths(
    tmp_path: Path,
) -> None:
    body = (
        "本社の別館の会議室は、利用の3日前までに総務課へ申し込む。利用は平日の9時から18時までで、"
        "利用の後は机と椅子を元の配置に戻し、鍵を総務課へ返す。"
    )

    def generate(system: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        data = json.loads(prompt)
        assert data["avoid_terms"] == list(AVOID_TERMS)
        assert "seed" in data and data["count"] == 25
        return DistractorBatch(
            documents=[
                DistractorDraft(title="別館の会議室の案内", body=body),
                DistractorDraft(title="重複", body=body),
                DistractorDraft(title="短い", body="短い。"),
                DistractorDraft(title="契約社員の手当", body=body + "契約社員は対象外。"),
            ]
        )

    documents, stats = generate_distractors(DEFAULT_DATASET, generate, per_seed=25, max_seeds=2)
    assert stats["calls"] == 2
    assert stats["dropped"] == {"duplicate": 3, "length": 2, "avoided_term": 2}
    assert [item.name for item in documents] == ["別館の会議室の案内.md"]
    assert documents[0].text.startswith("# 別館の会議室の案内\n")


def test_budgeted_session_stops_llm_calls_after_the_limit() -> None:
    session = BudgetedSession("record", limit=100)
    request = {"method": "POST", "path": "/v1/responses", "body": {"model": "m"}}
    body = json.dumps({"usage": {"input_tokens": 80, "output_tokens": 30}})
    with use_external_calls(session):
        external_call("http", request, lambda: {"status": 200, "body": body})
        assert session.used == 110
        # 埋め込みは LLM の token に数えず、止めない。
        external_call("embeddings", {"texts": ["a"]}, lambda: {"data": [[1.0]]})
        with pytest.raises(TokenBudgetExceeded):
            external_call(
                "http", {**request, "body": {"x": 1}}, lambda: {"status": 200, "body": body}
            )


def test_context_cache_key_follows_document_chunk_body_model_and_prompt() -> None:
    item = Evidence("a.md", "c1", "本文", ("節",), "検索文")
    key = context_cache_key(item, "# A\n本文", "model-a")
    assert key == context_cache_key(item, "# A\n本文", "model-a")
    assert key != context_cache_key(item, "# A\n本文 改", "model-a")  # 文書（周辺の文脈）の変更
    assert key != context_cache_key(item, "# A\n本文", "model-b")
    assert key != context_cache_key(
        Evidence("b.md", "c1", "本文", ("節",), "検索文"), "# A\n本文", "model-a"
    )
    assert key != context_cache_key(
        Evidence("a.md", "c1", "本文2", ("節",), "検索文"), "# A\n本文", "model-a"
    )


def test_answer_evidence_is_the_child_text_without_generated_context() -> None:
    item = Evidence("a.md", "c1", "終業は18時とする。", ("勤務",), "検索文")
    evidence = answer_evidence(["a.md#c1"], {"a.md#c1": item})
    assert evidence == [
        {"id": "E1", "source": "a.md", "heading": "勤務", "text": "終業は18時とする。"}
    ]
    output = AnswerOutput(
        insufficient=False,
        paragraphs=[
            AnswerParagraph(text="終業は18時です。", evidence_ids=["E1", "E1", "E9"]),
            AnswerParagraph(text="補足です。", evidence_ids=[]),
        ],
    )
    text, cited = render_answer(output, evidence)
    assert text == "終業は18時です。\n根拠：a.md\n\n補足です。"
    assert cited == ["E1"]  # 知らない id・重複は引用にしない


def _retrieval() -> tuple[Any, dict[str, Any]]:
    corpus = load_experiment_corpus(DEFAULT_DATASET)
    client = FakeClient()
    result = asyncio.run(
        run_retrieval(corpus, client, fake_generate, model="fake", cache={}, measure_latency=False)
    )
    return corpus, {
        "summary": result["summary"],
        "cases": result["cases"],
        "client": client,
        **result,
    }


@pytest.fixture(scope="module")
def retrieval() -> tuple[Any, dict[str, Any]]:
    return _retrieval()


def test_retrieval_compares_both_kbs_and_adds_context_only_to_the_treatment_rerank(
    retrieval: tuple[Any, dict[str, Any]],
) -> None:
    corpus, result = retrieval
    assert set(result["summary"]) == set(KBS)
    for kb in KBS:
        final = result["summary"][kb][TREATMENT]["hybrid_rerank"]["all"]
        assert final["cases"] == 70
        assert set(final["vs_current"]) == {"hit_at_5", "mrr"}
        assert "vs_current" not in result["summary"][kb][BASELINE]["hybrid_rerank"]["all"]
        # 索引が同じなので候補の集合（ハイブリッド）は 2 つの変種で同じ。
        for case in corpus.cases:
            assert (
                result["cases"][kb][TREATMENT][case.id]["hybrid"]
                == result["cases"][kb][BASELINE][case.id]["hybrid"]
            )
    # 文脈は候補に入った child にだけ作り、再ランクの入力の先頭に足すのは treatment だけ。
    assert result["candidates_with_context"] == len(result["contexts"])
    assert len(result["contexts"]) < len(corpus.large)
    with_context = [docs for docs in result["client"].reranked if docs[0].startswith("文脈: ")]
    assert with_context and all(
        all(doc.startswith("文脈: ") for doc in docs) for docs in with_context
    )
    assert len(with_context) + len(
        [docs for docs in result["client"].reranked if not docs[0].startswith("文脈: ")]
    ) <= 4 * len(corpus.cases)
    # 最終の上位 5 件は child の key（原文）。
    assert all("#" in key for key in result["cases"]["large"][TREATMENT]["deep-01"]["final"])


def test_answer_units_skip_unchanged_finals_and_count_them_as_zero(
    retrieval: tuple[Any, dict[str, Any]],
) -> None:
    corpus, result = retrieval
    units, compared = answer_units(result, corpus.cases, scope="changed")
    for kb in KBS:
        changed = compared[kb]["changed"]
        assert changed == compared[kb]["answered"]
        assert len(changed) == result["summary"][kb][TREATMENT]["final_changed_cases"]
    assert len({unit.key for unit in units}) == len(units)
    outputs = {
        unit.key: {"metrics": {"answer_correctness": 1.0 if unit.final == units[0].final else 0.0}}
        for unit in units
    }
    summary = summarize_answers(corpus.cases, result, outputs, compared)
    entry = summary["large"]["answer_correctness"]
    assert entry["pairs"] + entry["changed_pairs_unjudged"] == len(corpus.cases)
    # 判定の値の無い指標の変わった対は除く（改善にも悪化にも数えない）。
    assert summary["large"]["citation_match"]["changed_pairs_judged"] == 0


def test_adoption_needs_answer_quality_and_large_kb_improvement() -> None:
    def metric(interval: list[float]) -> dict[str, Any]:
        return {"vs_current": {"interval": interval}}

    flat = {
        name: metric([-0.01, 0.01])
        for name in (
            "answer_correctness",
            "citation_match",
            "requirement_coverage",
            "claim_support_rate",
            "abstained",
        )
    }
    better = {**flat, "answer_correctness": metric([0.02, 0.1])}
    worse = {**better, "claim_support_rate": metric([-0.2, -0.01])}
    retrieval = {
        "large": {
            TREATMENT: {
                "hybrid_rerank": {
                    "all": {
                        "vs_current": {
                            "mrr": {"interval": [0.01, 0.05]},
                            "hit_at_5": {"interval": None},
                        }
                    }
                }
            }
        }
    }
    assert adoption(retrieval, {"small": flat, "large": better})["adopt"] is True
    assert adoption(retrieval, {"small": flat, "large": worse})["adopt"] is False
    assert adoption(retrieval, {"small": flat, "large": flat})["adopt"] is False
    no_gain = {
        "large": {
            TREATMENT: {
                "hybrid_rerank": {
                    "all": {
                        "vs_current": {
                            "mrr": {"interval": [-0.01, 0.05]},
                            "hit_at_5": {"interval": None},
                        }
                    }
                }
            }
        }
    }
    assert adoption(no_gain, {"small": flat, "large": better})["adopt"] is False


def test_cli_retrieve_and_answer_record_and_replay(tmp_path: Path) -> None:
    def dependencies() -> tuple[Any, Any, Any, dict[str, Any]]:
        def generate(system: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
            result = external_call(
                "generate",
                {"system": system, "prompt": prompt},
                lambda: fake_generate(system, prompt, schema).model_dump(),
            )
            return schema.model_validate(result)

        return FakeClient(), generate, fake_evaluate, {"generation": "fake"}

    recording = tmp_path / "retrieve.json.gz"
    output = tmp_path / "retrieval.json"
    assert (
        main(
            ["retrieve", "--record", str(recording), "--output", str(output)],
            dependencies=dependencies,
        )
        == 0
    )
    with gzip.open(recording, "rt", encoding="utf-8") as handle:
        assert json.load(handle)["format"]
    live = json.loads(output.read_text("utf-8"))
    assert live["rerank"]["latency"][BASELINE] is not None
    replayed = tmp_path / "retrieval-replay.json"
    assert (
        main(
            ["retrieve", "--replay", str(recording), "--output", str(replayed)],
            dependencies=dependencies,
        )
        == 0
    )
    again = json.loads(replayed.read_text("utf-8"))
    assert again["summary"] == live["summary"]
    assert again["rerank"]["latency"] is None  # 再生の時間は測らない

    answer_recording = tmp_path / "answer.json.gz"
    answers = tmp_path / "answers.json"
    args = ["answer", "--retrieval", str(output), "--output", str(answers)]
    assert main([*args, "--record", str(answer_recording)], dependencies=dependencies) == 0
    first = json.loads(answers.read_text("utf-8"))
    assert set(first["adoption"]) == {"a_answer_quality", "b_large_kb", "adopt"}
    assert main([*args, "--replay", str(answer_recording)], dependencies=dependencies) == 0
    assert json.loads(answers.read_text("utf-8"))["summary"] == first["summary"]

    def other() -> tuple[Any, Any, Any, dict[str, Any]]:
        client, generate, evaluate, models = dependencies()

        def changed(system: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
            output: BaseModel = generate(system + "!", prompt, schema)
            return output

        return client, changed, evaluate, models

    # 記録と違う入力は、実の API に接続せずに失敗する。
    assert main([*args, "--replay", str(answer_recording)], dependencies=other) == 2


def test_context_batches_group_windows_and_mark_whole_short_documents() -> None:
    long_document = "# 長い文書\n" + "前の段落。" * 10 + "\n主語の無い断片。"
    short = "# 短い文書\n終業は18時とする。"
    items = {
        "long.md#c1": Evidence("long.md", "c1", "前の段落。" * 10, (), "検索文"),
        "long.md#c2": Evidence("long.md", "c2", "主語の無い断片。", (), "検索文"),
        **{
            f"s{i}.md#c1": Evidence(f"s{i}.md", "c1", "終業は18時とする。", (), "検索文")
            for i in range(12)
        },
    }
    documents = {"long.md": long_document, **{f"s{i}.md": short for i in range(12)}}
    batches = context_batches(list(items), items, documents)
    # 長い文書の 2 断片は同じ窓の 1 回、短い 1 断片の文書 12 件は 10 件と 2 件の 2 回。
    assert [[len(keys) for _window, keys in batch] for batch in batches] == [[2], [1] * 10, [1, 1]]
    prompt, ids = context_batch_prompt(batches[1][:1], items)
    assert json.loads(prompt)["documents"][0]["fragments"] == [{"id": "F1", "text": WHOLE_DOCUMENT}]
    assert ids == {"F1": "s0.md#c1"}


def test_answer_subset_pairs_only_the_answered_cases(
    retrieval: tuple[Any, dict[str, Any]],
) -> None:
    corpus, result = retrieval
    changed = [
        case.id
        for case in corpus.cases
        if result["cases"]["large"][TREATMENT][case.id]["final"]
        != result["cases"]["large"][BASELINE][case.id]["final"]
    ]
    only = changed[:2]
    units, compared = answer_units(result, corpus.cases, scope="changed", kbs=["large"], only=only)
    assert set(compared) == {"large"} and compared["large"]["subset"] is True
    assert {unit.case_id for unit in units} == set(only)
    outputs = {unit.key: {"metrics": {"answer_correctness": 1.0}} for unit in units}
    entry = summarize_answers(corpus.cases, result, outputs, compared)["large"][
        "answer_correctness"
    ]
    assert entry["pairs"] == 2
    assert entry["changed_pairs_not_answered"] == len(changed) - 2
