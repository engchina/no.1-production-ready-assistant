"""画面目録で質問から画面を選び、その画面の根拠を検索候補に加える (#1108)。"""

from types import SimpleNamespace

from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.retrieval.screen_catalog import (
    LinkedScreen,
    ScreenLinks,
    build_screen_catalog,
    link_screens,
    screen_candidates,
    validated_links,
)


def _chunk(uid, source, path, *, level="child", page=1, seq=1):
    return SimpleNamespace(
        chunk_uid=uid, source_file_name=source, chunk_level=level, page_start=page, chunk_seq=seq, metadata={"section_path": list(path)}
    )


POOL = [
    _chunk("a1", "架空管理.pdf", ["架空管理", "（２）伝票印刷設定", "B 【操作説明】"], page=5, seq=2),
    _chunk("a0", "架空管理.pdf", ["架空管理", "（２）伝票印刷設定", "A 【画面説明】"], page=5, seq=1),
    _chunk("b1", "架空管理.pdf", ["架空管理", "（３）出力様式設定"], page=8),
    _chunk("c1", "架空仕入.pdf", ["架空仕入", "（２）伝票印刷設定"], page=3),
    _chunk("p1", "架空管理.pdf", ["架空管理", "（２）伝票印刷設定"], level="parent"),
]


def test_catalog_lists_numbered_screen_headings_per_document():
    catalog = build_screen_catalog(POOL)

    assert catalog["架空管理.pdf"][0] == "（２）伝票印刷設定"  # 出現の多い順
    assert "（３）出力様式設定" in catalog["架空管理.pdf"]
    assert "B 【操作説明】" not in catalog["架空管理.pdf"]  # 番号付きの見出しだけ
    assert "架空管理" not in catalog["架空管理.pdf"]


def test_only_links_that_exist_in_the_catalog_are_kept():
    catalog = build_screen_catalog(POOL)
    links = ScreenLinks(
        candidates=[
            LinkedScreen(document="架空管理.pdf", screen="(2)伝票印刷設定"),  # 全角半角の違いは許す
            LinkedScreen(document="架空管理.pdf", screen="（９）存在しない画面"),
            LinkedScreen(document="無い文書.pdf", screen="（２）伝票印刷設定"),
        ]
    )

    assert validated_links(links, catalog) == [("架空管理.pdf", "（２）伝票印刷設定")]

    # 目録の表記「[文書名]」を括弧ごと写した文書名も受け入れる。
    bracketed = ScreenLinks(candidates=[LinkedScreen(document="[架空管理.pdf]", screen="（３）出力様式設定")])
    assert validated_links(bracketed, catalog) == [("架空管理.pdf", "（３）出力様式設定")]


def test_links_without_the_heading_number_are_kept_with_the_catalog_heading():
    """モデルが見出しの先頭の番号を省いて写しても、その文書で 1 つに決まれば目録の表記で採る (#2056)。"""
    catalog = {
        "架空管理.pdf": ["（２）伝票印刷設定", "（３）出力様式設定"],
        "架空仕入.pdf": ["（１）発注入力", "（４）発注入力"],
    }
    links = ScreenLinks(
        candidates=[
            LinkedScreen(document="架空管理.pdf", screen="伝票印刷設定"),
            LinkedScreen(document="架空管理.pdf", screen=" 出力様式設定 "),
            # 番号違いの同名の見出しが 2 つあり、どちらか決まらない。
            LinkedScreen(document="架空仕入.pdf", screen="発注入力"),
        ]
    )

    assert validated_links(links, catalog) == [
        ("架空管理.pdf", "（２）伝票印刷設定"),
        ("架空管理.pdf", "（３）出力様式設定"),
    ]

    # 番号つきで写せば、同名の見出しでもその見出しを採る。名前の部分一致は採らない。
    numbered = ScreenLinks(
        candidates=[
            LinkedScreen(document="架空仕入.pdf", screen="（４）発注入力"),
            LinkedScreen(document="架空管理.pdf", screen="伝票印刷"),
            LinkedScreen(document="架空仕入.pdf", screen="（９）発注入力"),
        ]
    )
    assert validated_links(numbered, catalog) == [("架空仕入.pdf", "（４）発注入力")]


def test_children_of_the_linked_screen_are_added_in_reading_order_without_duplicates():
    added = screen_candidates(POOL, [("架空管理.pdf", "（２）伝票印刷設定")], existing={"a1"})

    assert [c.chunk_uid for c in added] == ["a0"]  # 既存の候補・別文書・parent は加えない


def test_link_failures_leave_retrieval_unchanged():
    def broken(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    # 失敗は None（「該当なし」の [] と分ける。#3278）。目録が空なら LLM を呼ばずに [] を返す。
    assert link_screens("帳票の印字を変えたい。", build_screen_catalog(POOL), broken, settings=None) is None
    assert link_screens("帳票の印字を変えたい。", {}, broken, settings=None) == []


def test_link_failure_is_reported_as_degraded_not_as_no_match():
    # 選択の失敗は「該当なし」と分けて、呼び出し元の記録に処理の種類と例外を渡す（#3259）。
    def broken(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    def no_match(*args, **kwargs):
        return ScreenLinks(candidates=[])

    reported: list[tuple[str, str]] = []
    dependencies = AnswerDependencies(
        search=lambda **_: None,
        check_ready=lambda **_: None,
        parse_text=lambda *a, **k: None,
        parse_images=lambda *a, **k: None,
        rerank=lambda *a, **k: None,
        report_degraded=lambda kind, exc: reported.append((kind, type(exc).__name__)),
    )
    with bind_dependencies(dependencies):
        assert link_screens("帳票の印字を変えたい。", build_screen_catalog(POOL), no_match, settings=None) == []
        assert reported == []
        assert link_screens("帳票の印字を変えたい。", build_screen_catalog(POOL), broken, settings=None) is None
    assert reported == [("screen_link", "RuntimeError")]


def test_reserve_moves_only_the_best_child_of_each_linked_screen_up_to_the_rank():
    from rag_engine.retrieval.screen_catalog import reserve_linked_screens

    records = [SimpleNamespace(id=f"r{i}", source="架空管理.pdf", metadata={"section_path": ["（９）別画面"]}) for i in range(6)]
    records += [
        SimpleNamespace(id="s1", source="架空管理.pdf", metadata={"section_path": ["（２）伝票印刷設定"]}),
        SimpleNamespace(id="s2", source="架空管理.pdf", metadata={"section_path": ["（２）伝票印刷設定"]}),
    ]

    ordered = reserve_linked_screens(records, [("架空管理.pdf", "（２）伝票印刷設定")], rank=3)

    assert [r.id for r in ordered][:4] == ["r0", "r1", "s1", "r2"]  # 1 件だけを 3 位へ、ほかの順は保つ
    assert [r.id for r in ordered].index("s2") == 7


def test_catalog_from_section_paths_matches_chunk_catalog():
    """DB で集計した（文書名、見出しの列、chunk 数）からも、chunk から作るのと同じ目録になる。"""
    from rag_engine.retrieval.screen_catalog import catalog_from_section_paths

    rows = [
        ("架空管理.pdf", ["架空管理", "（３）出力様式設定"], 1),
        ("架空管理.pdf", ["架空管理", "（２）伝票印刷設定", "A 【画面説明】"], 2),
        ("架空仕入.pdf", ["架空仕入", "（２）伝票印刷設定"], 1),
        ("", ["（１）文書名なし"], 1),
    ]

    assert catalog_from_section_paths(rows) == {
        "架空管理.pdf": ["（２）伝票印刷設定", "（３）出力様式設定"],
        "架空仕入.pdf": ["（２）伝票印刷設定"],
    }


def _stored(uid, source, path, *, level="child", seq=1):
    from rag_engine.models.storage import StoredChunk

    return StoredChunk(
        chunk_uid=uid,
        chunk_id=uid,
        chunk_level=level,
        chunk_seq=seq,
        parent_chunk_uid="" if level == "parent" else f"{source}:p",
        parent_chunk_id="",
        child_chunk_ids=(),
        text=uid,
        retrieval_text=uid,
        source_run_id="run",
        source_file_name=source,
        source_engine_id="docling",
        source_engine_label="Docling",
        page_start=1,
        page_end=1,
        source_seq_ranges=(),
        source_record_refs=(),
        metadata={"section_path": list(path)},
    )


def test_linked_screen_children_reads_catalog_and_chunks_from_injected_dependencies():
    """目録と画面の chunk を注入したら、検索結果（pool）に無い画面も選んで候補に加え、親を展開用に返す。"""
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies
    from rag_engine.generation import answering

    calls = []
    screen = ("架空管理.pdf", "（２）伝票印刷設定")

    def unused(*args, **kwargs):
        raise AssertionError("呼ばない")

    def screen_chunks(*, links, existing, settings):
        calls.append(("chunks", list(links), set(existing)))
        return [
            _stored("s1", screen[0], ["架空管理", screen[1]]),
            _stored(f"{screen[0]}:p", screen[0], ["架空管理", screen[1]], level="parent"),
        ]

    dependencies = AnswerDependencies(
        search=unused,
        check_ready=unused,
        rerank=unused,
        parse_images=unused,
        parse_text=lambda system, prompt, settings, schema, **kwargs: ScreenLinks(
            candidates=[LinkedScreen(document=screen[0], screen=screen[1])]
        ),
        screen_catalog=lambda *, settings: calls.append(("catalog",)) or {screen[0]: [screen[1]]},
        screen_chunks=screen_chunks,
    )
    pool = [_stored("h1", "別文書.pdf", ["（１）受注入力"])]
    ranked = [answering._stored_chunk_answer_record(pool[0])]
    answering._SCREEN_LINK_CACHE.clear()

    with bind_dependencies(dependencies):
        added, links, expansion = answering._linked_screen_children("印字されない", pool, ranked, settings=None)

    assert links == [screen]
    assert [record.chunk_uid for record in added] == ["s1"]
    assert [record.chunk_uid for record in expansion] == ["s1", f"{screen[0]}:p"]
    assert calls == [("catalog",), ("chunks", [screen], {"h1"})]


def _screen_link_dependencies(parse_text, reported, screen):
    """目録と画面の chunk を注入し、選択の LLM（parse_text）と失敗の記録（reported）を差し替えた依存。"""

    def unused(*args, **kwargs):
        raise AssertionError("呼ばない")

    return AnswerDependencies(
        search=unused,
        check_ready=unused,
        rerank=unused,
        parse_images=unused,
        parse_text=parse_text,
        screen_catalog=lambda *, settings: {screen[0]: [screen[1]]},
        screen_chunks=lambda *, links, existing, settings: [_stored("s1", screen[0], ["架空管理", screen[1]])],
        report_degraded=lambda kind, exc: reported.append(kind),
    )


def test_screen_link_failure_is_not_cached_and_the_next_answer_selects_again():
    """選択の LLM が一時的に失敗しても cache に残さず、同じ質問・同じ目録の次の回答で選び直す（#3278）。"""
    from rag_engine.generation import answering

    screen = ("架空管理.pdf", "（２）伝票印刷設定")
    calls = []

    def flaky(system, prompt, settings, schema, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("provider unavailable")
        return ScreenLinks(candidates=[LinkedScreen(document=screen[0], screen=screen[1])])

    reported = []
    pool = [_stored("h1", "別文書.pdf", ["（１）受注入力"])]
    ranked = [answering._stored_chunk_answer_record(pool[0])]
    answering._SCREEN_LINK_CACHE.clear()

    with bind_dependencies(_screen_link_dependencies(flaky, reported, screen)):
        first = answering._linked_screen_children("印字されない", pool, ranked, settings=None)
        assert first == ([], [], [])
        assert answering._SCREEN_LINK_CACHE == {}
        added, links, _ = answering._linked_screen_children("印字されない", pool, ranked, settings=None)

    assert len(calls) == 2
    assert links == [screen]
    assert [record.chunk_uid for record in added] == ["s1"]
    assert reported == ["screen_link"]


def test_screen_link_failure_is_reported_on_every_attempt():
    """失敗が続けば、回答ごとに失敗を記録する（最初の 1 回だけにしない。#3278）。"""
    from rag_engine.generation import answering

    def broken(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    reported = []
    pool = [_stored("h1", "別文書.pdf", ["（１）受注入力"])]
    answering._SCREEN_LINK_CACHE.clear()

    with bind_dependencies(_screen_link_dependencies(broken, reported, ("架空管理.pdf", "（２）伝票印刷設定"))):
        for _ in range(2):
            assert answering._linked_screen_children("印字されない", pool, [], settings=None) == ([], [], [])

    assert reported == ["screen_link", "screen_link"]


def test_screen_link_with_no_match_is_cached_and_not_asked_again():
    """選択に成功して画面が無かった（[]）ときは cache し、同じ質問・同じ目録では LLM を呼び直さない。"""
    from rag_engine.generation import answering

    calls = []

    def no_match(system, prompt, settings, schema, **kwargs):
        calls.append(prompt)
        return ScreenLinks(candidates=[])

    reported = []
    pool = [_stored("h1", "別文書.pdf", ["（１）受注入力"])]
    answering._SCREEN_LINK_CACHE.clear()

    with bind_dependencies(_screen_link_dependencies(no_match, reported, ("架空管理.pdf", "（２）伝票印刷設定"))):
        for _ in range(2):
            assert answering._linked_screen_children("印字されない", pool, [], settings=None) == ([], [], [])

    assert len(calls) == 1
    assert list(answering._SCREEN_LINK_CACHE.values()) == [[]]
    assert reported == []
    answering._SCREEN_LINK_CACHE.clear()
