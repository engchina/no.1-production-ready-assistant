"""条の選択と範囲の文書の選択の、process をまたいで時間で切れる記録（#3507）。

process をまたぐことは、process の中の記録（L1）を空にして同じ記録のディレクトリから読むことで確かめる。
"""

import json
import multiprocessing
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from rag_engine.config import get_settings
from rag_engine.generation import answering
from rag_engine.resources.shared_cache import SharedResultCache, cache_key_digest, shared_result_cache
from rag_engine.retrieval.article_catalog import ArticleCatalog, ArticleIssue, ArticleIssueSelection, ArticleSelection
from tests.test_article_catalog import CATALOG, LAW, NESTED, _law_leads, _scope_dependencies, _select_with


def _articles(*groups):
    return ArticleIssueSelection(issues=[ArticleIssue(issue=f"論点{i}", lines=list(group)) for i, group in enumerate(groups, 1)])


@pytest.fixture(autouse=True)
def _empty_process_records():
    answering._ARTICLE_SELECTION_CACHE.clear()
    answering._ARTICLE_DOCUMENT_CACHE.clear()
    yield
    answering._ARTICLE_SELECTION_CACHE.clear()
    answering._ARTICLE_DOCUMENT_CACHE.clear()


def _settings(directory: Path | None, **changes):
    base = get_settings(environ={}, dotenv_path=None)
    return replace(base, article_catalog_cache_dir=directory, **changes)


def _new_process():
    """別の process の始まり: process の中の記録は空で、ディレクトリの記録だけが残る。"""
    answering._ARTICLE_SELECTION_CACHE.clear()
    answering._ARTICLE_DOCUMENT_CACHE.clear()


def _counting(answer):
    calls = []

    def parse(system, prompt, settings, schema, **kwargs):
        payload = json.loads(prompt)
        calls.append(("refine" if "candidates" in payload else "select", payload.get("document")))
        return answer(payload)

    parse.calls = calls
    return parse


def _select(parse, settings, catalogs, *, leads=None, sources=None):
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies

    def unused(*args, **kwargs):
        raise AssertionError("呼ばない")

    dependencies = AnswerDependencies(
        search=unused, check_ready=unused, rerank=unused, parse_images=unused, parse_text=parse, article_leads=leads
    )
    with bind_dependencies(dependencies):
        selected, failed, _added, refine_failed, _refs = answering._select_catalog_articles(
            "届出の期限は", catalogs, settings, sources=sources
        )
    return selected, failed, refine_failed


# --- 記録のファイル -----------------------------------------------------------------------------------------


def test_cache_returns_the_value_until_the_ttl_passes(tmp_path):
    now = [1000.0]
    cache = SharedResultCache(tmp_path, ttl_seconds=60, max_entries=10, clock=lambda: now[0])

    assert cache.get(("k", 1)) is None
    assert cache.put(("k", 1), [["s1", "第一条"]])
    assert cache.get(("k", 1)) == [["s1", "第一条"]]
    # 別の key は別の記録（tuple と list は同じ key に揃える）。
    assert cache.get(("k", 2)) is None
    assert cache.get(["k", 1]) == [["s1", "第一条"]]

    now[0] += 60
    assert cache.get(("k", 1)) == [["s1", "第一条"]]
    now[0] += 1
    assert cache.get(("k", 1)) is None


def test_cache_file_does_not_hold_the_key_text(tmp_path):
    cache = SharedResultCache(tmp_path, ttl_seconds=60, max_entries=10)
    cache.put(("合成の質問の文", "doc"), ["doc-g"])

    [path] = list(tmp_path.iterdir())
    assert path.name == f"{cache_key_digest(('合成の質問の文', 'doc'))}.json"
    assert "合成の質問の文" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"\xff\xfe", b"[]", b'{"v": 99, "created": 0, "value": []}', b'{"v": 1, "created": "x", "value": []}', b""],
    ids=["broken-json", "not-utf8", "not-object", "other-version", "bad-time", "empty"],
)
def test_corrupt_entry_is_a_miss(tmp_path, content):
    cache = SharedResultCache(tmp_path, ttl_seconds=60, max_entries=10)
    cache.put("k", ["doc-g"])
    (tmp_path / f"{cache_key_digest('k')}.json").write_bytes(content)

    assert cache.get("k") is None
    # 書き直せば読める。
    assert cache.put("k", ["doc-g"])
    assert cache.get("k") == ["doc-g"]


def test_cache_keeps_at_most_max_entries_dropping_the_oldest(tmp_path):
    cache = SharedResultCache(tmp_path, ttl_seconds=600, max_entries=3)
    for index in range(5):
        cache.put(f"k{index}", [index])
        path = tmp_path / f"{cache_key_digest(f'k{index}')}.json"
        os.utime(path, (1000 + index, 1000 + index))

    cache.put("k5", [5])

    assert [cache.get(f"k{index}") for index in range(6)] == [None, None, None, [3], [4], [5]]


def test_unwritable_directory_is_not_an_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    cache = SharedResultCache(blocker / "cache", ttl_seconds=60, max_entries=10)

    assert cache.put("k", [1]) is False
    assert cache.get("k") is None


def test_cache_is_off_without_a_directory_ttl_or_entries(tmp_path):
    assert shared_result_cache(None, ttl_seconds=60, max_entries=10) is None
    assert shared_result_cache("", ttl_seconds=60, max_entries=10) is None
    assert shared_result_cache(tmp_path, ttl_seconds=0, max_entries=10) is None
    assert shared_result_cache(tmp_path, ttl_seconds=60, max_entries=0) is None
    assert isinstance(shared_result_cache(tmp_path, ttl_seconds=60, max_entries=10), SharedResultCache)


def _write_many(directory: str, worker: int) -> int:
    cache = SharedResultCache(Path(directory), ttl_seconds=600, max_entries=1000)
    for round_ in range(50):
        # 全 worker が同じ key と自分の key を書く。
        cache.put("shared", [[f"w{worker}", "x" * 2000]])
        cache.put(f"own-{worker}-{round_ % 5}", [worker, round_])
    return worker


def test_concurrent_writes_from_processes_and_threads_leave_whole_entries(tmp_path):
    reads: list[object] = []
    stop = threading.Event()
    cache = SharedResultCache(tmp_path, ttl_seconds=600, max_entries=1000)

    def reader():
        while not stop.is_set():
            reads.append(cache.get("shared"))

    # process を先に fork してから（thread のある process を fork しない）、読む thread を始める。
    context = multiprocessing.get_context("fork")
    processes = [context.Process(target=_write_many, args=(str(tmp_path), worker)) for worker in range(4)]
    for process in processes:
        process.start()
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for process in processes:
            process.join(timeout=60)
        assert [process.exitcode for process in processes] == [0, 0, 0, 0]
        threads = [threading.Thread(target=_write_many, args=(str(tmp_path), 10 + index)) for index in range(4)]
        for item in threads:
            item.start()
        for item in threads:
            item.join()
    finally:
        stop.set()
        thread.join()

    # 読めた値は、どれかの worker が書いた完全な値だけ（途中の内容・例外は無い）。
    for value in reads:
        assert value is None or (isinstance(value, list) and value[0][1] == "x" * 2000)
    assert cache.get("shared")[0][0] in {f"w{worker}" for worker in (0, 1, 2, 3, 10, 11, 12, 13)}
    assert {cache.get(f"own-{worker}-{round_}")[0] for worker in range(4) for round_ in range(5)} == {0, 1, 2, 3}
    # 一時ファイルは残らない。
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".tmp-")]


# --- 条の選択 ---------------------------------------------------------------------------------------------


def test_selection_is_shared_across_processes_without_calling_the_llm(tmp_path):
    other = replace(CATALOG, document_id="doc-b", file_name="架空細則.json", chunk_set_id="cs-b")
    parse = _counting(lambda payload: _articles([3], [2]))
    settings = _settings(tmp_path)

    first_sources: dict[str, int] = {}
    first, failed, _ = _select(parse, settings, [CATALOG, other], sources=first_sources)
    assert failed == [] and len(parse.calls) == 2
    assert first_sources == {"llm": 2}

    _new_process()
    second_sources: dict[str, int] = {}
    second, failed, _ = _select(parse, settings, [CATALOG, other], sources=second_sources)

    assert len(parse.calls) == 2
    assert second == first and failed == []
    assert second_sources == {"shared": 2}
    # 同じ process の 3 回目は process の中の記録から。
    third_sources: dict[str, int] = {}
    _select(parse, settings, [CATALOG, other], sources=third_sources)
    assert third_sources == {"process": 2} and len(parse.calls) == 2


def test_selection_from_the_shared_record_matches_a_fresh_llm_selection(tmp_path):
    """2 段目（同じ章と名指しの条）を含む選択でも、記録から使った結果は LLM で選び直した結果と同じ。"""

    def answer(payload):
        if "candidates" in payload:
            return _articles([9], [4])
        return _articles([3], [1])

    parse = _counting(answer)
    leads = lambda *, document_id, chunk_set_id, sections, settings: _law_leads(sections)  # noqa: E731
    cached_settings = _settings(tmp_path)

    _select(parse, cached_settings, [LAW, NESTED], leads=leads)
    _new_process()
    from_record, _, _ = _select(parse, cached_settings, [LAW, NESTED], leads=leads)
    calls_before_fresh = len(parse.calls)

    _new_process()
    fresh, _, _ = _select(parse, _settings(None), [LAW, NESTED], leads=leads)

    assert len(parse.calls) > calls_before_fresh
    assert from_record == fresh
    assert [article.section_id for article in fresh]


@pytest.mark.parametrize(
    "change",
    ["chunk_set", "catalog", "model", "reasoning", "limit", "question"],
    ids=["chunk_set", "catalog", "model", "reasoning", "limit", "question"],
)
def test_selection_is_chosen_again_when_an_input_changes(tmp_path, change):
    parse = _counting(lambda payload: _articles([3]))
    settings = _settings(tmp_path)
    _select(parse, settings, [CATALOG])
    assert len(parse.calls) == 1

    catalog = CATALOG
    question = "届出の期限は"
    if change == "chunk_set":
        catalog = replace(CATALOG, chunk_set_id="cs-a2")
    elif change == "catalog":
        # 行の数は同じで内容だけが違う目録。
        catalog = replace(CATALOG, lines=(*CATALOG.lines[:-1], "    第三条（期限）"))
    elif change == "model":
        provider = settings.llm_providers[settings.default_answer_llm]
        settings = replace(
            settings, llm_providers={**settings.llm_providers, settings.default_answer_llm: replace(provider, model="other")}
        )
    elif change == "reasoning":
        provider = settings.llm_providers[settings.default_answer_llm]
        settings = replace(
            settings, llm_providers={**settings.llm_providers, settings.default_answer_llm: replace(provider, reasoning_effort="high")}
        )
    elif change == "limit":
        settings = replace(settings, article_catalog_max_articles=3)
    elif change == "question":
        question = "届出の様式は"

    _new_process()
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies

    def unused(*args, **kwargs):
        raise AssertionError("呼ばない")

    with bind_dependencies(AnswerDependencies(search=unused, check_ready=unused, rerank=unused, parse_images=unused, parse_text=parse)):
        answering._select_catalog_articles(question, [catalog], settings)
    assert len(parse.calls) == 2


def test_whitespace_only_differences_in_the_question_share_the_record(tmp_path):
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies

    parse = _counting(lambda payload: _articles([3]))
    settings = _settings(tmp_path)

    def unused(*args, **kwargs):
        raise AssertionError("呼ばない")

    with bind_dependencies(AnswerDependencies(search=unused, check_ready=unused, rerank=unused, parse_images=unused, parse_text=parse)):
        answering._select_catalog_articles("届出の 期限は", [CATALOG], settings)
        _new_process()
        answering._select_catalog_articles("  届出の\n期限は ", [CATALOG], settings)
    assert len(parse.calls) == 1


def test_full_width_and_half_width_questions_share_the_record_and_the_llm_gets_the_original(tmp_path):
    """質問は NFKC で揃えてから key にする（全角・半角の英数字と記号、全角の空白）。LLM には元の文を渡す。"""
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies

    questions = []
    parse = _counting(lambda payload: questions.append(payload["question"]) or _articles([3]))
    settings = _settings(tmp_path)

    def unused(*args, **kwargs):
        raise AssertionError("呼ばない")

    with bind_dependencies(AnswerDependencies(search=unused, check_ready=unused, rerank=unused, parse_images=unused, parse_text=parse)):
        first = answering._select_catalog_articles("ＡＢＣ－１２３の届出（様式２）は", [CATALOG], settings)
        _new_process()
        # 先頭の全角の空白（NFKC で半角の空白になり、前後の空白として詰める）。
        second = answering._select_catalog_articles("　ABC-123の届出(様式2)は", [CATALOG], settings)
        # 揃えても違う文は別の記録。
        _new_process()
        answering._select_catalog_articles("ABC-124の届出(様式2)は", [CATALOG], settings)

    assert len(parse.calls) == 2
    assert second[0] == first[0]
    assert questions == ["ＡＢＣ－１２３の届出（様式２）は", "ABC-124の届出(様式2)は"]


def test_evaluation_neither_reads_nor_writes_the_shared_record_but_keeps_the_process_record(tmp_path):
    """品質評価（`bypass_shared_cache`）は共有の記録を読みも書きもしない。process の中の記録は今までどおり使う。"""
    from rag_engine.resources.shared_cache import bypass_shared_cache, shared_cache_bypassed

    parse = _counting(lambda payload: _articles([3]))
    settings = _settings(tmp_path)

    # 評価の外で書いた共有の記録があっても、評価では読まない。
    _select(parse, settings, [CATALOG])
    assert len(list(tmp_path.glob("*.json"))) == 1
    _new_process()
    other = replace(CATALOG, document_id="doc-b", file_name="架空細則.json", chunk_set_id="cs-b")
    with bypass_shared_cache():
        assert shared_cache_bypassed() is True
        sources: dict[str, int] = {}
        _select(parse, settings, [CATALOG, other], sources=sources)
        assert sources == {"llm": 2} and len(parse.calls) == 3
        # 評価で選んだ結果は共有の記録に書かない。
        assert len(list(tmp_path.glob("*.json"))) == 1
        # 同じ process の記録は使う。
        sources = {}
        _select(parse, settings, [CATALOG, other], sources=sources)
        assert sources == {"process": 2} and len(parse.calls) == 3
    assert shared_cache_bypassed() is False
    # 評価の後は共有の記録を使う（評価で選んだ文書は記録に無い）。
    _new_process()
    sources = {}
    _select(parse, settings, [CATALOG, other], sources=sources)
    assert sources == {"shared": 1, "llm": 1}


def test_failed_selections_are_not_recorded(tmp_path):
    """1 段目・2 段目に失敗した文書は記録に残さず、別の process でも LLM で選び直す（#3285 と同じ）。"""
    from rag_engine.adapters import oci

    other = replace(CATALOG, document_id="doc-b", file_name="架空細則.json", chunk_set_id="cs-b")

    def answer(payload):
        if payload["document"] == other.file_name:
            raise oci.IncompleteResponse("未完了", "max_output_tokens")
        if "candidates" in payload:
            raise RuntimeError("provider unavailable")
        return _articles([4])

    parse = _counting(answer)
    leads = lambda *, document_id, chunk_set_id, sections, settings: {}  # noqa: E731
    settings = _settings(tmp_path)

    _, failed, refine_failed = _select(parse, settings, [NESTED, other], leads=leads)
    assert failed == [("架空細則.json", "出力の上限")]
    assert refine_failed == ["架空法.json"]
    assert list(tmp_path.glob("*.json")) == []

    _new_process()
    _select(parse, settings, [NESTED, other], leads=leads)
    # 2 回目も 2 文書の 1 段目と 2 段目を呼ぶ（1 回目と同じ 3 回）。
    assert sorted(parse.calls[3:]) == sorted(parse.calls[:3]) and len(parse.calls) == 6


def test_corrupt_record_is_chosen_again_and_rewritten(tmp_path):
    parse = _counting(lambda payload: _articles([3]))
    settings = _settings(tmp_path)
    first, _, _ = _select(parse, settings, [CATALOG])
    [path] = list(tmp_path.glob("*.json"))
    path.write_text('{"v": 1, "created": 1e18, "value": [["s3"]]}', encoding="utf-8")

    _new_process()
    second, _, _ = _select(parse, settings, [CATALOG])

    assert len(parse.calls) == 2 and second == first
    _new_process()
    _select(parse, settings, [CATALOG])
    assert len(parse.calls) == 2


def test_expired_record_is_chosen_again(tmp_path):
    parse = _counting(lambda payload: _articles([3]))
    settings = _settings(tmp_path, article_catalog_cache_ttl_seconds=60)
    _select(parse, settings, [CATALOG])
    [path] = list(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**payload, "created": payload["created"] - 61}), encoding="utf-8")

    _new_process()
    _select(parse, settings, [CATALOG])
    assert len(parse.calls) == 2


def test_cache_off_keeps_only_the_process_record(tmp_path):
    parse = _counting(lambda payload: _articles([3]))
    for settings in (_settings(None), _settings(tmp_path, article_catalog_cache_ttl_seconds=0.0)):
        _new_process()
        _select(parse, settings, [CATALOG])
        _select(parse, settings, [CATALOG])
    assert len(parse.calls) == 2
    assert list(tmp_path.iterdir()) == []


def test_existing_selection_helper_still_works_with_the_shared_record(tmp_path):
    """既存の補助（`_select_with`）からの呼び出しも、記録のディレクトリを設定すれば同じ結果を返す。"""
    parse = _counting(lambda payload: _articles([3]))
    settings = _settings(tmp_path)
    first, _ = _select_with(parse, settings, [CATALOG])
    _new_process()
    second, _ = _select_with(parse, settings, [CATALOG])
    assert first == second and len(parse.calls) == 1


# --- 範囲の文書の選択 ---------------------------------------------------------------------------------------


def test_scope_document_selection_is_shared_and_failures_are_not_recorded(tmp_path):
    from rag_engine.dependencies import bind_dependencies

    calls = []

    def flaky(system, prompt, settings, schema, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("provider unavailable")
        return ArticleSelection(lines=[1])

    settings = _settings(tmp_path)
    reported: list[str] = []
    with bind_dependencies(_scope_dependencies(flaky, reported)):
        assert answering._scope_catalog_documents_with_source("届出の補正は", settings, {"doc-a"}) == (None, "llm")
        assert list(tmp_path.glob("*.json")) == []
        assert answering._scope_catalog_documents_with_source("届出の補正は", settings, {"doc-a"}) == (["doc-g"], "llm")
        assert answering._scope_catalog_documents_with_source("届出の補正は", settings, {"doc-a"}) == (["doc-g"], "process")
        _new_process()
        assert answering._scope_catalog_documents_with_source("届出の補正は", settings, {"doc-a"}) == (["doc-g"], "shared")
    assert len(calls) == 2


# --- 工程の記録 -----------------------------------------------------------------------------------------------


def _catalog_step(settings):
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies
    from rag_engine.generation.execution_record import _structured_steps
    from tests.test_article_catalog import _stored

    def unused(*args, **kwargs):
        raise AssertionError("呼ばない")

    def parse(system, prompt, settings, schema, **kwargs):
        parse.calls += 1
        return _articles([3]) if schema is ArticleIssueSelection else ArticleSelection(lines=[1])

    parse.calls = 0
    other = ArticleCatalog(
        document_id="doc-g", file_name="架空手続通則.json", chunk_set_id="cs-g", lines=CATALOG.lines, section_ids=CATALOG.section_ids
    )
    dependencies = AnswerDependencies(
        search=unused,
        check_ready=unused,
        rerank=unused,
        parse_images=unused,
        parse_text=parse,
        article_catalogs=lambda *, document_ids, limit, settings: [CATALOG if "doc-a" in document_ids else other],
        article_chunks=lambda *, articles, settings: [],
        article_documents=lambda *, settings: [("doc-a", "架空規程.json"), ("doc-g", "架空手続通則.json")],
    )
    ranked = [answering._stored_chunk_answer_record(_stored("hit", "架空規程.json"))]
    steps: list[dict] = []
    token = _structured_steps.set(steps)
    try:
        with bind_dependencies(dependencies):
            answering._catalog_article_children("届出の補正は", ranked, settings)
    finally:
        _structured_steps.reset(token)
    step = next(step for step in steps if step["name"] == "条の選択")
    return {field["label"]: field["value"] for field in step["details"]["fields"]}, parse.calls


def test_catalog_step_shows_whether_the_selection_came_from_the_llm_or_a_record(tmp_path):
    settings = _settings(tmp_path)

    fields, calls = _catalog_step(settings)
    assert calls == 3
    assert fields["条の選択の出どころ"] == "LLM 2 文書"
    assert fields["範囲の文書の選択の出どころ"] == "LLM"

    _new_process()
    fields, calls = _catalog_step(settings)
    assert calls == 0
    assert fields["条の選択の出どころ"] == "共有の記録 2 文書"
    assert fields["範囲の文書の選択の出どころ"] == "共有の記録"

    fields, calls = _catalog_step(settings)
    assert calls == 0
    assert fields["条の選択の出どころ"] == "同じ process の記録 2 文書"
    assert fields["範囲の文書の選択の出どころ"] == "同じ process の記録"


def test_catalog_step_shows_that_evaluation_does_not_use_the_shared_record(tmp_path):
    from rag_engine.resources.shared_cache import bypass_shared_cache

    settings = _settings(tmp_path)
    _catalog_step(settings)
    _new_process()
    with bypass_shared_cache():
        fields, calls = _catalog_step(settings)
    assert calls == 3
    assert fields["条の選択の出どころ"] == "LLM 2 文書・共有の記録を使わない（評価）"
    assert fields["範囲の文書の選択の出どころ"] == "LLM・共有の記録を使わない（評価）"


def test_cache_settings_come_from_the_environment(tmp_path):
    defaults = get_settings(environ={}, dotenv_path=None)
    custom = get_settings(
        environ={
            "RAG_ENGINE_ARTICLE_CATALOG_CACHE_DIR": str(tmp_path),
            "RAG_ENGINE_ARTICLE_CATALOG_CACHE_TTL_SECONDS": "120",
            "RAG_ENGINE_ARTICLE_CATALOG_CACHE_MAX_ENTRIES": "10",
        },
        dotenv_path=None,
    )
    assert (defaults.article_catalog_cache_dir, defaults.article_catalog_cache_ttl_seconds, defaults.article_catalog_cache_max_entries) == (
        None,
        86400.0,
        4096,
    )
    assert (custom.article_catalog_cache_dir, custom.article_catalog_cache_ttl_seconds, custom.article_catalog_cache_max_entries) == (
        tmp_path,
        120.0,
        10,
    )
