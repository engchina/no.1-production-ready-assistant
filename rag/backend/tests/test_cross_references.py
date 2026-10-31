"""文書の交差参照の抽出・解決と、回答の検索での参照先の追加(#1280)。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_parser_core.language import FIELD_SPECS, builtin_pack, resolve, set_default_pack

from app.config import Settings
from app.rag.answer_engine import AnswerEngine, _outcome_from_result, _SearchState
from app.rag.chunking import Chunk
from app.rag.cross_references import (
    MAX_REFERENCES_PER_CHUNK,
    REFERENCE_FROM_KEY,
    REFERENCE_LABEL_KEY,
    REFERENCE_RANK_KEY,
    REFERENCE_TARGETS_KEY,
    ReferenceSpec,
    ReferenceTarget,
    SectionIndex,
    annotate_cross_references,
    apply_reference_search_context,
    document_name_aliases,
    document_name_match,
    document_name_references,
    extract_references,
    heading_keys,
    reference_targets,
    same_document_title,
    section_path_within,
    title_key,
)
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest

# ---- 抽出 ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("承認の条件は第3章を参照してください。", [("第3章", "chapter", "3", None)]),
        ("詳細は３．２節参照。", [("3.2節", "section", "3.2", None)]),
        ("様式は別紙1のとおり。", [("別紙1", "appendix", "別紙:1", None)]),
        ("第5条に定める手続きを行う。", [("第5条", "article", "5", None)]),
        ("第三章第二節を参照", [("第三章第二節", "section", "3.2", None)]),
        ("付録Aをご覧ください", [("付録A", "appendix", "付録:A", None)]),
        (
            "第3章及び第5章を参照",
            [("第3章", "chapter", "3", None), ("第5章", "chapter", "5", None)],
        ),
        ("本マニュアルの第2章を参照", [("第2章", "chapter", "2", None)]),
        (
            "経費精算マニュアルの「権限」を参照すること。",
            [("「権限」", "title", "権限", "経費精算マニュアル")],
        ),
        (
            "『経費精算マニュアル』の第4章を参照",
            [("第4章", "chapter", "4", "経費精算マニュアル")],
        ),
        ("詳細は出張旅費規程第6条による。", [("第6条", "article", "6", "出張旅費規程")]),
        (
            "See Section 3.2 of the Expense Manual for details.",
            [("See Section 3.2 of the Expense Manual", "section", "3.2", "Expense Manual")],
        ),
        ("Please refer to Appendix A.", [("refer to Appendix A", "appendix", "付録:A", None)]),
        # 手がかりの語の無い章の参照(「の」+ 名詞。#1382)。
        (
            "この章に無いシステムは、第 2 章の共通の保守枠で保守します。",
            [("第 2 章", "chapter", "2", None)],
        ),
        (
            "組織規程の第 4 章の代理の規定で決める。",
            [("第 4 章", "chapter", "4", "組織規程")],
        ),
        (
            "第3章と第5章の手順で行う。",
            [("第3章", "chapter", "3", None), ("第5章", "chapter", "5", None)],
        ),
        ("第5条の手続きで申請する。", [("第5条", "article", "5", None)]),
        ("別紙2の様式で提出する。", [("別紙2", "appendix", "別紙:2", None)]),
        ("3.2節の手順で行う。", [("3.2節", "section", "3.2", None)]),
        # 条の枝番と附則(#2147)。
        ("第2条の2の手続きで行う。", [("第2条の2", "article", "2の2", None)]),
        ("附則第五条第一項の規定による", [("附則第五条第一項", "article", "5", None)]),
        (
            "テスト保険法附則第四条の三第一項に規定する給付",
            [("附則第四条の三第一項", "article", "4の3", "テスト保険法")],
        ),
        (
            "テスト法(昭和三十年法律第百号)第四十二条に規定する",
            [("第四十二条", "article", "42", "テスト法")],
        ),
        ("法第十四条に規定する事務", [("第十四条", "article", "14", "法")]),
        ("同法第五条の規定による", [("第五条", "article", "5", "同法")]),
        ("本法第三条の規定による", [("第三条", "article", "3", None)]),
        ("処理方法第3条による", [("第3条", "article", "3", None)]),
        ("第三条の一部を改正する", [("第三条", "article", "3", None)]),
    ],
    ids=[
        "chapter",
        "full-width-section",
        "appendix",
        "article",
        "kanji-chapter-section",
        "appendix-letter",
        "enumeration",
        "this-manual",
        "other-document-title",
        "quoted-document-chapter",
        "regulation-article",
        "english-section-of-document",
        "english-appendix",
        "possessive-chapter",
        "possessive-other-document",
        "possessive-enumeration",
        "possessive-article",
        "possessive-appendix",
        "possessive-numbered-section",
        "branch-article",
        "supplementary-article",
        "other-law-supplementary-branch",
        "other-law-with-number",
        "parent-law-abbreviation",
        "same-law-as-before",
        "this-law",
        "general-word-ending-with-ho",
        "not-branch-number",
    ],
)
def test_extract_references(text: str, expected: list[tuple[str, str, str, str | None]]) -> None:
    specs = extract_references(text)

    assert [(s.label, s.kind, s.key, s.document_title) for s in specs] == expected


@pytest.mark.parametrize(
    "text",
    [
        "第3章では概要を説明する。",
        "1.5倍を参照",
        "「承認」ボタンを押す。",
        "バージョン 2 では変わった。",
        "",
        "本書は全12章の構成です。",
        "別紙の様式で提出する。",
        "3.2の機能で動く。",
        "第 1 章 個別の保守枠",
    ],
    ids=[
        "no-cue",
        "decimal",
        "ui-label",
        "plain",
        "empty",
        "chapter-count",
        "unnumbered-appendix",
        "version-number",
        "heading",
    ],
)
def test_extract_references_ignores_text_without_reference(text: str) -> None:
    """参照の手がかりの語の無い表記・手がかりの語と離れた語は拾わない。"""
    assert extract_references(text) == []


def test_extract_references_marks_whole_document_reference() -> None:
    """『文書名』だけの参照は文書全体への参照(節を決めないので辿らない)。"""
    specs = extract_references("詳しくは『経費精算マニュアル』を参照。")

    assert [(s.kind, s.document_title) for s in specs] == [("document", "経費精算マニュアル")]


def test_extract_references_caps_count_and_dedupes() -> None:
    text = "。".join(f"第{n}章を参照" for n in range(1, 12)) + "。第1章を参照"

    specs = extract_references(text)

    assert len(specs) == MAX_REFERENCES_PER_CHUNK
    assert len({spec.key for spec in specs}) == len(specs)


# ---- 解決 ----

PATHS = [
    "第1章 総則",
    "第2章 手順 > 第1節 準備",
    "第2章 手順 > 第2節 入力",
    "第3章 承認 > 3.1 申請",
    "第3章 承認 > 3.2 上長承認",
    "第4章 権限 > 4.1 権限の設定",
    "第5章 監査 > 第2節 記録",
    "別紙1 申請書様式",
    "Appendix A Forms",
]


def _resolve(text: str, citing: tuple[str, ...] = ()) -> str | None:
    (spec,) = extract_references(text)
    return SectionIndex(PATHS).resolve(spec, citing_path=citing)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("第3章を参照", "第3章 承認"),
        ("3.2節を参照", "第3章 承認 > 3.2 上長承認"),
        ("別紙1のとおり", "別紙1 申請書様式"),
        ("「権限の設定」を参照", "第4章 権限 > 4.1 権限の設定"),
        ("「権限」を参照", "第4章 権限"),
        ("see Appendix A", "Appendix A Forms"),
        ("第2章第2節を参照", "第2章 手順 > 第2節 入力"),
        ("第9章を参照", None),
        ("「存在しない節」を参照", None),
    ],
    ids=[
        "chapter",
        "numbered",
        "appendix",
        "title-exact",
        "title-partial",
        "english-appendix",
        "chapter-section-as-jsection",
        "missing-chapter",
        "missing-title",
    ],
)
def test_section_index_resolves_reference(text: str, expected: str | None) -> None:
    assert _resolve(text) == expected


def test_section_index_prefers_section_near_citing_chunk() -> None:
    """「第2節」が複数の章にあるときは、参照元と同じ章の節を選ぶ。"""
    assert _resolve("第2節を参照", ("第5章 監査", "第1節 概要")) == "第5章 監査 > 第2節 記録"
    assert _resolve("第2節を参照", ("第2章 手順", "第1節 準備")) == "第2章 手順 > 第2節 入力"


LAW_PATHS = [
    "テスト法 > 第一章 総則 > 第五条(定義)",
    "テスト法 > 第一章 総則 > 第七条(資格)",
    "テスト法 > 附則(制定時)(抜粋) > 第三条(資格の特例)",
    "テスト法 > 附則(制定時)(抜粋) > 第五条(任意加入)",
    "テスト法 > 附則(制定時)(抜粋) > 第七条の二(期間の特例)",
    "テスト法 > 附則(令和二年法律第八号) > 第一条(施行期日)",
    "テスト法 > 附則(令和二年法律第八号) > 第五条(経過措置)",
]
_MAIN = ("テスト法", "第一章 総則", "第五条(定義)")
_ORIGINAL = ("テスト法", "附則(制定時)(抜粋)", "第三条(資格の特例)")
_AMENDMENT = ("テスト法", "附則(令和二年法律第八号)", "第一条(施行期日)")


@pytest.mark.parametrize(
    ("text", "citing", "expected"),
    [
        (
            "第七条第一項第二号の規定の適用については",
            _ORIGINAL,
            "テスト法 > 第一章 総則 > 第七条(資格)",
        ),
        ("附則第五条の規定による", _ORIGINAL, "テスト法 > 附則(制定時)(抜粋) > 第五条(任意加入)"),
        ("附則第五条の規定による", _MAIN, "テスト法 > 附則(制定時)(抜粋) > 第五条(任意加入)"),
        (
            "附則第五条の規定による",
            _AMENDMENT,
            "テスト法 > 附則(令和二年法律第八号) > 第五条(経過措置)",
        ),
        ("第五条の規定による", _AMENDMENT, None),
        (
            "附則第七条の二の規定による",
            _MAIN,
            "テスト法 > 附則(制定時)(抜粋) > 第七条の二(期間の特例)",
        ),
        ("第七条の二の規定による", _ORIGINAL, None),
    ],
    ids=[
        "bare-article-in-supplement-is-main",
        "supplement-article-in-original-supplement",
        "supplement-article-from-main",
        "supplement-article-in-amendment",
        "bare-article-in-amendment-is-amending-act",
        "branch-number",
        "branch-number-not-in-main",
    ],
)
def test_section_index_resolves_law_articles_by_part(
    text: str, citing: tuple[str, ...], expected: str | None
) -> None:
    """附則の付かない条は本則、「附則第N条」は附則、枝番は枝番の付いた条に解決する(#2147)。"""
    (spec,) = extract_references(text)

    assert SectionIndex(LAW_PATHS).resolve(spec, citing_path=citing) == expected


def test_annotate_cross_references_resolves_law_supplement_and_other_law() -> None:
    """附則の読替え規定の参照: 本則の条に解決し、他の法令の附則の条は他の文書として残す(#2147)。"""
    chunks = [
        _chunk(0, "目的を定める。", " > ".join(_MAIN)),
        _chunk(1, "資格を定める。", "テスト法 > 第一章 総則 > 第七条(資格)"),
        _chunk(
            2,
            "第七条第一項第二号の規定の適用については、同号中「者」とあるのは、"
            "「者(テスト保険法附則第四条の三第一項に規定する給付の受給権を有しない者に限る。)」"
            "とする。",
            " > ".join(_ORIGINAL),
        ),
        _chunk(3, "期間の特例。", "テスト法 > 附則(制定時)(抜粋) > 第七条の二(期間の特例)"),
    ]

    annotate_cross_references(chunks, document_title="テスト法.json")

    targets = reference_targets(chunks[2].metadata)
    assert sorted((t.label, t.key, t.document_title, t.section_path) for t in targets) == [
        ("第七条第一項第二号", "7", None, "テスト法 > 第一章 総則 > 第七条(資格)"),
        ("附則第四条の三第一項", "4の3", "テスト保険法", None),
    ]


RELATIVE_PATHS = [
    "テスト法 > 第三章 給付 > 第三十条(障害基礎年金)",
    "テスト法 > 第三章 給付 > 第三十条の二",
    "テスト法 > 第三章 給付 > 第三十条の三",
    "テスト法 > 附則(制定時)(抜粋) > 第一条(施行期日)",
    "テスト法 > 附則(制定時)(抜粋) > 第二条(経過措置)",
]


@pytest.mark.parametrize(
    ("text", "citing", "expected"),
    [
        (
            "前条第一項各号のいずれかに該当した者",
            ("テスト法", "第三章 給付", "第三十条の二"),
            "テスト法 > 第三章 給付 > 第三十条(障害基礎年金)",
        ),
        (
            "次条の規定により",
            ("テスト法", "第三章 給付", "第三十条の二"),
            "テスト法 > 第三章 給付 > 第三十条の三",
        ),
        (
            "前条の規定により",
            ("テスト法", "附則(制定時)(抜粋)", "第二条(経過措置)"),
            "テスト法 > 附則(制定時)(抜粋) > 第一条(施行期日)",
        ),
        ("前条の規定により", ("テスト法", "第三章 給付", "第三十条(障害基礎年金)"), None),
        ("次条の規定により", ("テスト法", "附則(制定時)(抜粋)", "第二条(経過措置)"), None),
    ],
    ids=["previous", "next", "previous-in-supplement", "no-previous", "no-next-in-supplement"],
)
def test_section_index_resolves_relative_articles(
    text: str, citing: tuple[str, ...], expected: str | None
) -> None:
    """「前条」「次条」は参照元の条の前・次の条(本則・同じ附則の中)に解決する(#2153)。"""
    (spec,) = extract_references(text)

    assert SectionIndex(RELATIVE_PATHS).resolve(spec, citing_path=citing) == expected


def test_extract_references_same_article_follows_previous_label() -> None:
    """「同条」は同じ本文の中で直前に出た条の参照と同じ条にする。無ければ拾わない(#2153)。"""
    specs = extract_references("第九十条第一項の規定による申請。同条第二項に規定する期間")
    assert [(s.kind, s.key) for s in specs] == [("article", "90")]
    other = extract_references("テスト保険法第四十二条に規定する者。同条第二項に規定する")
    assert [(s.key, s.document_title) for s in other] == [("42", "テスト保険法")]
    assert extract_references("同条第二項に規定する期間") == []


@pytest.fixture
def reference_words() -> Iterator[Callable[..., None]]:
    """言語資源の項目(#2157・#2181)を差し替える。テストの後に組み込みのパックへ戻す。"""
    sections = {spec.key: spec.section for spec in FIELD_SPECS}
    overrides: dict[str, dict[str, object]] = {}

    def change(**words: object) -> None:
        for name, value in words.items():
            overrides.setdefault(sections[name], {})[name] = value
        set_default_pack(resolve(builtin_pack(), overrides))

    yield change
    set_default_pack(None)


def test_reference_words_come_from_settings(reference_words: Callable[..., None]) -> None:
    """部の前置き・枝番・文書名の語尾・相対の参照の語は設定で変わる(#2157)。"""
    reference_words(part_prefixes=[], branch_connectors=[], document_suffixes=["要項"])
    assert [(s.label, s.key) for s in extract_references("附則第五条の規定による")] == [
        ("第五条", "5")
    ]
    # 枝番のつなぎが無ければ「第2条の2」を枝番の条として拾わない。
    assert extract_references("第2条の2の手続きで行う。") == []
    assert [
        (s.key, s.document_title) for s in extract_references("詳細は経費精算要項第6条による。")
    ] == [("6", "経費精算要項")]
    # 部の前置きが無ければ「附則」を部として区別せず、参照元に近い条(本則の第五条)にする。
    (spec,) = extract_references("附則第五条の規定による")
    assert (
        SectionIndex(LAW_PATHS).resolve(spec, citing_path=_MAIN)
        == "テスト法 > 第一章 総則 > 第五条(定義)"
    )

    reference_words(previous_words=["直前の"], same_words=[])
    assert [s.key for s in extract_references("直前の条の規定により")] == ["前"]
    assert extract_references("同条第二項に規定する期間") == []


def test_apply_reference_search_context_adds_referenced_item() -> None:
    """XML ツリー JSON の読替え規定の検索の文に、参照先の項・号の行を足す(#2148)。

    回答に使う本文は変えない。
    """
    law = {"source_parser": "xml_tree_json"}
    main_path = "テスト法 > 第一章 総則 > 第七条(資格)"
    chunks = [
        _chunk(
            0,
            "テスト法 第七条第一項(資格)\n次の者を被保険者とする。\n"
            "一 国内に住所を有する者\n二 甲の被保険者",
            main_path,
            xml_tree_paragraph_num="1",
            engine_search_text="検索 0",
            **law,
        ),
        _chunk(
            1,
            "テスト法 第七条第二項(資格)\n前項の特例。",
            main_path,
            xml_tree_paragraph_num="2",
            **law,
        ),
        _chunk(
            2,
            "テスト法 附則(制定時)第三条(特例)\n"
            "第七条第一項第二号の規定の適用については、当分の間、別の読み方とする。",
            " > ".join(_ORIGINAL),
            engine_search_text="検索 2",
            **law,
        ),
        _chunk(3, "第3章を参照。", "第1章 総則", engine_search_text="検索 3"),
    ]
    annotate_cross_references(chunks, document_title="テスト法.json")

    assert apply_reference_search_context(chunks) == 1
    assert chunks[2].metadata["engine_search_text"] == (
        "検索 2\n参照先:\n- テスト法 第七条第一項(資格): 二 甲の被保険者"
    )
    assert chunks[2].text.startswith("テスト法 附則(制定時)第三条")
    # XML ツリー JSON 以外の chunk は変えない。
    assert chunks[3].metadata["engine_search_text"] == "検索 3"


def test_section_index_uses_numbered_heading_for_chapter() -> None:
    """章の見出しが「3 承認」のように番号だけの文書でも「第3章」を解決する。"""
    index = SectionIndex(["1 総則", "3 承認 > 3.1 申請"])

    assert index.resolve(ReferenceSpec("第3章", "chapter", "3")) == "3 承認"


def test_section_index_leaves_ambiguous_partial_title_unresolved() -> None:
    index = SectionIndex(["承認A", "承認B", "承認C"])

    assert index.resolve(ReferenceSpec("「承認」", "title", "承認")) is None


def test_section_path_within_and_document_title() -> None:
    assert section_path_within("第3章 承認 > 3.2 上長承認", "第3章 承認")
    assert section_path_within("第3章 承認", "第3章 承認")
    assert not section_path_within("第3章 承認2", "第3章 承認")
    assert not section_path_within(None, "第3章 承認")
    assert same_document_title("経費精算マニュアル", "経費精算マニュアル_v3.pdf")
    assert same_document_title("Expense Manual", "expense_manual.docx")
    assert not same_document_title("経費精算マニュアル", "出張旅費規程.pdf")
    # 同じ名前で始まる別の法令(#2147)。
    assert not same_document_title("テスト法", "テスト法施行令.json")
    assert not same_document_title("テスト法施行規則", "テスト法.json")
    assert same_document_title("テスト法", "テスト法.json")


# ---- 取込 ----


def _chunk(index: int, text: str, section_path: str | None, **metadata: Any) -> Chunk:
    return Chunk(
        text=text,
        index=index,
        start_offset=0,
        end_offset=len(text),
        metadata={"section_path": section_path, **metadata},
    )


def test_annotate_cross_references_resolves_same_document_sections() -> None:
    chunks = [
        _chunk(0, "承認の条件は第3章を参照すること。様式は別紙1のとおり。", "第1章 総則"),
        _chunk(1, "上長の承認が必要である。", "第3章 承認 > 3.1 申請"),
        _chunk(2, "第3章を参照。3.1節を参照。", "第3章 承認 > 3.1 申請"),
        _chunk(3, "申請日を書く。", "別紙1 申請書様式"),
        _chunk(4, "第9章を参照。", "第1章 総則"),
        _chunk(5, "要約。第3章を参照。", "第1章 総則", raptor_summary=True),
        # 章節の要約・項目の値(派生の chunk。印の無い #2590 より前の chunk も。#2641)。
        _chunk(6, "第1章 総則: 第3章を参照。", "第1章 総則", content_kind="section_summary"),
        _chunk(7, "項目: 第3章を参照。", None, content_kind="field", extracted_field=True),
    ]

    annotated = annotate_cross_references(chunks, document_title="経費精算規程.pdf")

    assert REFERENCE_TARGETS_KEY not in chunks[6].metadata
    assert REFERENCE_TARGETS_KEY not in chunks[7].metadata
    assert annotated == 2
    first = reference_targets(chunks[0].metadata)
    assert [(t.label, t.section_path, t.resolved) for t in first] == [
        ("第3章", "第3章 承認", True),
        ("別紙1", "別紙1 申請書様式", True),
    ]
    # 参照の無い chunk・要約の chunk には付けない。自分の節(祖先の節)だけを参照する chunk は
    # 空の列(取込で解決済みの印。回答のときに本文から抜き出し直さない。#1382)。
    assert REFERENCE_TARGETS_KEY not in chunks[1].metadata
    assert chunks[2].metadata[REFERENCE_TARGETS_KEY] == "[]"
    assert REFERENCE_TARGETS_KEY not in chunks[5].metadata
    # 見つからない節は未解決として残す(診断に使う)。
    assert [(t.label, t.resolved) for t in reference_targets(chunks[4].metadata)] == [
        ("第9章", False)
    ]


def test_annotate_cross_references_keeps_other_documents_unresolved() -> None:
    chunks = [
        _chunk(0, "経費精算マニュアルの「権限」を参照。", "第1章 総則"),
        _chunk(1, "経費精算規程の第2章を参照。", "第1章 総則"),
        _chunk(2, "権限を設定する。", "第2章 権限"),
    ]

    annotate_cross_references(chunks, document_title="経費精算規程.pdf")

    (other,) = reference_targets(chunks[0].metadata)
    assert other.document_title == "経費精算マニュアル"
    assert other.section_path is None
    # 自分の文書名の参照は、同じ文書の節として解決する。
    (own,) = reference_targets(chunks[1].metadata)
    assert own.document_title is None
    assert own.section_path == "第2章 権限"


def test_reference_targets_round_trip_and_ignore_broken_values() -> None:
    target = ReferenceTarget("第3章", "chapter", "3", None, "第3章 承認")

    assert ReferenceTarget.from_dict(target.to_dict()) == target
    assert reference_targets({REFERENCE_TARGETS_KEY: "{broken"}) == []
    assert reference_targets({REFERENCE_TARGETS_KEY: json.dumps([{"kind": "x"}, 1])}) == []
    assert reference_targets({}) == []


async def test_ingestion_records_cross_references_on_chunks() -> None:
    """取込の分割の後に、chunk の metadata へ参照先を残す。"""
    from app.rag.ingestion import IngestionPipeline
    from app.rag.ingestion_quality import build_ingestion_quality_report
    from app.schemas.extraction import StructuredExtraction

    extraction = StructuredExtraction(
        raw_text=(
            "経費精算規程\n\n# 第1章 総則\n\n承認の条件は第3章を参照すること。\n\n"
            "# 第3章 承認\n\n上長の承認が必要である。\n"
        )
    )
    pipeline = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, object()),
        object_storage=cast(Any, object()),
        settings=Settings(rag_chunking_strategy="structure_aware"),
    )

    chunks = await pipeline._build_chunks_for_extraction(
        trace_id="trace-1280",
        extraction=extraction,
        quality_report=build_ingestion_quality_report(extraction),
        parser_profile="unstructured",
        source_name="経費精算規程.pdf",
    )

    referencing = [chunk for chunk in chunks if REFERENCE_TARGETS_KEY in chunk.metadata]
    assert len(referencing) == 1
    assert [t.section_path for t in reference_targets(referencing[0].metadata)] == ["第3章 承認"]


# ---- 回答の検索 ----


def _retrieved(
    chunk_id: str,
    text: str,
    section_path: str,
    *,
    document_id: str = "doc-1",
    file_name: str = "経費精算規程.pdf",
    chunk_set_id: str = "cs-1",
    targets: list[ReferenceTarget] | None = None,
) -> RetrievedChunk:
    metadata: dict[str, Any] = {
        "section_path": section_path,
        "chunk_set_id": chunk_set_id,
        "chunk_group_id": chunk_id,
        "page_start": 1,
        "page_end": 1,
    }
    if targets:
        metadata[REFERENCE_TARGETS_KEY] = json.dumps(
            [target.to_dict() for target in targets], ensure_ascii=False
        )
    return RetrievedChunk(
        document_id=document_id,
        chunk_id=chunk_id,
        text=text,
        score=0.9,
        file_name=file_name,
        metadata=metadata,
    )


CHAPTER3 = ReferenceTarget("第3章", "chapter", "3", None, "第3章 承認")


class ReferenceOracle:
    """回答の検索が読む OracleClient の部分集合(交差参照の参照先を返す)。"""

    def __init__(
        self,
        hits: list[RetrievedChunk],
        sections: list[RetrievedChunk],
        *,
        knowledge_bases: dict[str, set[str]] | None = None,
    ) -> None:
        self.hits = hits
        self.sections = sections
        # ナレッジベース → 所属の文書(検索の範囲。None は範囲を見ない)。
        self.knowledge_bases = knowledge_bases
        self.reference_calls: list[dict[str, Any]] = []
        self.document_calls: list[dict[str, Any]] = []
        self.document_batch_calls: list[dict[str, Any]] = []
        self.fail_batch = False
        self.section_calls: list[dict[str, str]] = []
        self.search_calls: list[dict[str, Any]] = []
        # 文書名・タイトルの照合の結果を固定するとき(None は sections から作る)。
        self.documents: list[tuple[str, str, tuple[str, ...]]] | None = None
        self.fail = False

    async def retrieval_large_categories(self, filters: dict[str, str]) -> list[str]:
        return []

    async def document_classifications(self, ids: list[str]) -> dict[str, dict[str, object]]:
        return {}

    async def chunk_set_first_page_contexts(self, ids: list[str]) -> dict[str, dict[str, object]]:
        return {}

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        document_id = (filters or {}).get("document_id")
        if document_id is None:
            return list(self.hits)
        # 文書の中の検索(文書名だけの参照の参照先。#1400)。sections の順を関連の順とみなす。
        self.search_calls.append({"query": query, "filters": dict(filters or {})})
        return [
            chunk
            for chunk in self.sections
            if chunk.document_id == document_id and self._in_scope(filters or {}, chunk)
        ][:top_k]

    async def context_group_siblings(
        self,
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        return []

    async def retrieval_reference_chunks(
        self,
        filters: dict[str, str],
        *,
        document_id: str,
        section_path: str,
        chunk_set_id: str | None = None,
        limit: int,
    ) -> list[RetrievedChunk]:
        self.reference_calls.append(
            {
                "filters": dict(filters),
                "document_id": document_id,
                "section_path": section_path,
                "chunk_set_id": chunk_set_id,
            }
        )
        if self.fail:
            raise RuntimeError("db down")
        return [
            chunk
            for chunk in self.sections
            if chunk.document_id == document_id
            and self._in_scope(filters, chunk)
            and section_path_within(chunk.metadata.get("section_path"), section_path)
            and (chunk_set_id is None or chunk.metadata.get("chunk_set_id") == chunk_set_id)
        ][:limit]

    def _in_scope(self, filters: dict[str, str], chunk: RetrievedChunk) -> bool:
        knowledge_base_id = filters.get("knowledge_base_id")
        if self.knowledge_bases is None or not knowledge_base_id:
            return True
        return chunk.document_id in self.knowledge_bases.get(knowledge_base_id, set())

    async def retrieval_reference_documents(
        self, filters: dict[str, str], *, title: str, limit: int
    ) -> list[tuple[str, str, tuple[str, ...]]]:
        self.document_calls.append({"filters": dict(filters), "title": title})
        if self.documents is not None:
            return list(self.documents)
        # SQL と同じく、範囲の中で文書名か見出しの列が title を含む chunk の、見出しの列の先頭。
        found: dict[str, tuple[str, list[str]]] = {}
        for chunk in self.sections:
            path = str(chunk.metadata.get("section_path") or "")
            if not self._in_scope(filters, chunk) or (
                title not in title_key(chunk.file_name or "") and title not in title_key(path)
            ):
                continue
            _name, roots = found.setdefault(chunk.document_id, (chunk.file_name or "", []))
            root = path.split(" > ")[0]
            if root and root not in roots:
                roots.append(root)
        return [(key, name, tuple(roots)) for key, (name, roots) in found.items()][:limit]

    async def retrieval_reference_documents_many(
        self, filters: dict[str, str], *, titles: list[str], limit: int
    ) -> dict[str, list[tuple[str, str, tuple[str, ...]]]]:
        # 文書名ごとの照合と同じ結果(document_calls にも文書名ごとに残す)。
        self.document_batch_calls.append({"filters": dict(filters), "titles": list(titles)})
        if self.fail_batch:
            raise RuntimeError("db down")
        found = {
            title: await self.retrieval_reference_documents(filters, title=title, limit=limit)
            for title in titles
        }
        return {title: rows for title, rows in found.items() if rows}

    async def retrieval_screen_sections(
        self, filters: dict[str, str]
    ) -> list[tuple[str, str, int]]:
        self.section_calls.append(dict(filters))
        document_id = filters.get("document_id")
        chunk_set_id = filters.get("chunk_set_id")
        paths = dict.fromkeys(
            str(chunk.metadata["section_path"])
            for chunk in self.sections
            if chunk.document_id == document_id
            and self._in_scope(filters, chunk)
            and (chunk_set_id is None or chunk.metadata.get("chunk_set_id") == chunk_set_id)
        )
        return [("x.pdf", path, 1) for path in paths]


class FakeGenAi:
    """埋め込みと rerank のスタブ。rerank は本文が含む語の点(``scores``)の高い順。"""

    def __init__(self, scores: dict[str, float] | None = None, *, fail: bool = False) -> None:
        self.scores = scores or {}
        self.fail = fail
        self.rerank_calls: list[tuple[str, list[str]]] = []

    async def embed(self, texts: list[str], *, input_type: str = "") -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        self.rerank_calls.append((query, list(documents)))
        if self.fail:
            raise RuntimeError("rerank down")

        def score(text: str) -> float:
            return max((value for word, value in self.scores.items() if word in text), default=0.0)

        ranked = sorted(range(len(documents)), key=lambda index: -score(documents[index]))
        return [(index, score(documents[index])) for index in ranked[:top_n]]


def _engine(
    oracle: ReferenceOracle, genai: FakeGenAi | None = None, **settings: Any
) -> AnswerEngine:
    return AnswerEngine(
        Settings(**settings), oracle=cast(Any, oracle), genai=cast(Any, genai or FakeGenAi())
    )


def _section_chunks(count: int, *, chunk_set_id: str = "cs-1") -> list[RetrievedChunk]:
    return [
        _retrieved(
            f"doc-1:{chunk_set_id}:s{n}",
            f"承認の条件 {n}",
            "第3章 承認 > 3.1 申請",
            chunk_set_id=chunk_set_id,
        )
        for n in range(count)
    ]


async def test_search_adds_referenced_section_after_citing_anchor() -> None:
    anchor = _retrieved("doc-1:a", "承認の条件は第3章を参照。", "第1章 総則", targets=[CHAPTER3])
    other = _retrieved("doc-1:b", "精算の概要。", "第1章 総則")
    oracle = ReferenceOracle(
        [anchor, other],
        [*_section_chunks(3), *_section_chunks(1, chunk_set_id="cs-2")],
    )
    state = _SearchState()

    result = await _engine(oracle)._search(
        SearchRequest(
            query="承認の条件",
            filters={"knowledge_base_id": "kb-1", "file_name": "経費", "large_category": "業務A"},
        ),
        state,
        retrieval_queries=["承認の条件"],
    )

    order = [child.chunk_uid for child in result.child_chunks]
    # 参照先の節の先頭から 2 件を、参照元の直後に置く(rerank が無効でも根拠に入る)。
    assert order == ["doc-1:a", "doc-1:cs-1:s0", "doc-1:cs-1:s1", "doc-1:b"]
    child = result.child_chunks[1]
    assert child.metadata["rrf_score"] == pytest.approx(
        result.child_chunks[0].metadata["rrf_score"] * 0.5
    )
    # 参照先は参照元と同じ版(chunk_set)から読み、検索範囲は KB だけに絞る(文書名・分類は外す)。
    (call,) = oracle.reference_calls
    assert call["chunk_set_id"] == "cs-1"
    assert call["filters"] == {"knowledge_base_id": "kb-1"}
    added = state.chunks["doc-1:cs-1:s0"]
    assert added.metadata[REFERENCE_FROM_KEY] == "doc-1:a"
    assert added.metadata[REFERENCE_LABEL_KEY] == "第3章"
    assert set(state.reference_expansions) == {"doc-1:cs-1:s0", "doc-1:cs-1:s1"}
    # 非親子の参照先は子の原文を保持し、連結した親を合成しない。
    assert all(chunk.chunk_level == "child" for chunk in result.all_chunks)


async def test_search_reference_expansion_respects_budget_and_caches_targets() -> None:
    targets = [
        CHAPTER3,
        ReferenceTarget("別紙1", "appendix", "別紙:1", None, "別紙1 申請書様式"),
    ]
    anchors = [
        _retrieved(f"doc-1:a{n}", "第3章と別紙1を参照。", "第1章 総則", targets=targets)
        for n in range(3)
    ]
    appendix = [_retrieved(f"doc-1:cs-1:x{n}", "様式", "別紙1 申請書様式") for n in range(3)]
    oracle = ReferenceOracle(anchors, [*_section_chunks(3), *appendix])
    state = _SearchState()
    engine = _engine(oracle, rag_reference_expansion_max_chunks=3)

    result = await engine._search(SearchRequest(query="q"), state, retrieval_queries=["q"])
    await engine._search(SearchRequest(query="q"), state, retrieval_queries=["q2"])

    referenced = [
        child.chunk_uid
        for child in result.child_chunks
        if child.chunk_uid not in {a.chunk_id for a in anchors}
    ]
    assert referenced == ["doc-1:cs-1:s0", "doc-1:cs-1:s1", "doc-1:cs-1:x0"]
    # 同じ参照先は 1 回の回答で 1 回だけ読む(CRAG の各回でも読み直さない)。
    assert [call["section_path"] for call in oracle.reference_calls] == [
        "第3章 承認",
        "別紙1 申請書様式",
    ]


async def test_search_skips_reference_expansion_when_disabled_or_search_only() -> None:
    anchor = _retrieved("doc-1:a", "第3章を参照。", "第1章 総則", targets=[CHAPTER3])
    oracle = ReferenceOracle([anchor], _section_chunks(2))

    disabled = await _engine(oracle, rag_reference_expansion_enabled=False)._search(
        SearchRequest(query="q"), _SearchState(), retrieval_queries=["q"]
    )
    retrieved = await _engine(oracle).retrieve(SearchRequest(query="q", top_k=1))

    assert [child.chunk_uid for child in disabled.child_chunks] == ["doc-1:a"]
    assert [chunk.chunk_id for chunk in retrieved] == ["doc-1:a"]
    assert oracle.reference_calls == []


async def test_search_continues_when_reference_target_cannot_be_read() -> None:
    anchor = _retrieved("doc-1:a", "第3章を参照。", "第1章 総則", targets=[CHAPTER3])
    oracle = ReferenceOracle([anchor], _section_chunks(2))
    oracle.fail = True

    result = await _engine(oracle)._search(
        SearchRequest(query="q"), _SearchState(), retrieval_queries=["q"]
    )

    assert [child.chunk_uid for child in result.child_chunks] == ["doc-1:a"]


async def test_search_resolves_other_document_reference_within_scope() -> None:
    """他の文書への参照は、検索範囲の文書から文書名で探し、その文書の見出しで節を決める。"""
    target = ReferenceTarget("「権限」", "title", "権限", "経費精算マニュアル")
    anchor = _retrieved(
        "doc-1:a", "経費精算マニュアルの「権限」を参照。", "第1章", targets=[target]
    )
    manual = [
        _retrieved(
            f"doc-2:cs-9:{n}",
            "権限の設定",
            "第4章 権限 > 4.1 設定",
            document_id="doc-2",
            file_name="経費精算マニュアル.pdf",
            chunk_set_id="cs-9",
        )
        for n in range(2)
    ] + [
        _retrieved(
            "doc-2:cs-8:0",
            "権限の設定(別レシピ)",
            "第4章 権限 > 4.1 設定",
            document_id="doc-2",
            file_name="経費精算マニュアル.pdf",
            chunk_set_id="cs-8",
        )
    ]
    oracle = ReferenceOracle([anchor], manual)
    oracle.documents = [
        ("doc-2", "経費精算マニュアル.pdf", ("第4章 権限",)),
        ("doc-3", "旧経費精算マニュアル集.pdf", ()),
    ]

    result = await _engine(oracle)._search(
        SearchRequest(query="q", filters={"knowledge_base_id": "kb-1", "category_name": "経費"}),
        _SearchState(),
        retrieval_queries=["q"],
    )

    assert [child.chunk_uid for child in result.child_chunks] == [
        "doc-1:a",
        "doc-2:cs-9:0",
        "doc-2:cs-9:1",
    ]
    assert oracle.document_calls == [
        {"filters": {"knowledge_base_id": "kb-1"}, "title": "経費精算マニュアル"}
    ]
    assert oracle.section_calls == [{"knowledge_base_id": "kb-1", "document_id": "doc-2"}]


async def test_search_matches_other_document_titles_in_one_read() -> None:
    """起点の候補が参照する他の文書の文書名は、辿る前にまとめて 1 回で照合する(#2137)。

    1 つずつ照合したときと同じ chunk を足し、文書名ごとの照合(SQL)は繰り返さない。
    読めなければ辿るときに 1 つずつ照合する。
    """
    first = _retrieved(
        "doc-1:a",
        "経費精算マニュアルの「権限」を参照。",
        "第1章",
        targets=[ReferenceTarget("「権限」", "title", "権限", "経費精算マニュアル")],
    )
    second = _retrieved(
        "doc-1:b",
        "出張旅費規程の「日当」を参照。",
        "第2章",
        targets=[ReferenceTarget("「日当」", "title", "日当", "出張旅費規程")],
    )
    manual = [
        _retrieved(
            "doc-2:cs-9:0",
            "権限の設定",
            "第4章 権限",
            document_id="doc-2",
            file_name="経費精算マニュアル.pdf",
            chunk_set_id="cs-9",
        ),
        _retrieved(
            "doc-3:cs-7:0",
            "日当の額",
            "第3章 日当",
            document_id="doc-3",
            file_name="出張旅費規程.pdf",
            chunk_set_id="cs-7",
        ),
    ]
    request = SearchRequest(query="q", filters={"knowledge_base_id": "kb-1"})

    async def search(fail_batch: bool) -> tuple[list[str], ReferenceOracle]:
        oracle = ReferenceOracle([first, second], manual)
        oracle.fail_batch = fail_batch
        result = await _engine(oracle)._search(request, _SearchState(), retrieval_queries=["q"])
        return [child.chunk_uid for child in result.child_chunks], oracle

    batched, oracle = await search(fail_batch=False)
    one_by_one, fallback = await search(fail_batch=True)

    assert batched == one_by_one == ["doc-1:a", "doc-2:cs-9:0", "doc-1:b", "doc-3:cs-7:0"]
    assert oracle.document_batch_calls == [
        {"filters": {"knowledge_base_id": "kb-1"}, "titles": ["経費精算マニュアル", "出張旅費規程"]}
    ]
    # まとめて照合した文書名は、辿るときに照合し直さない(document_calls はまとめた中の分だけ)。
    assert [call["title"] for call in oracle.document_calls] == [
        "経費精算マニュアル",
        "出張旅費規程",
    ]
    assert [call["title"] for call in fallback.document_calls] == [
        "経費精算マニュアル",
        "出張旅費規程",
    ]


async def test_search_does_not_follow_other_document_outside_scope() -> None:
    target = ReferenceTarget("第2章", "chapter", "2", "出張旅費規程")
    anchor = _retrieved("doc-1:a", "出張旅費規程の第2章を参照。", "第1章", targets=[target])
    oracle = ReferenceOracle([anchor], [])

    result = await _engine(oracle)._search(
        SearchRequest(query="q"), _SearchState(), retrieval_queries=["q"]
    )

    assert [child.chunk_uid for child in result.child_chunks] == ["doc-1:a"]
    assert oracle.section_calls == []


async def test_search_resolves_references_of_chunks_without_ingest_targets() -> None:
    """取込で参照先を解決していない chunk(#1382 より前の取込)は、回答のときに本文から解決する。"""
    first = _retrieved("doc-1:a", "申請は第3章の手順で行う。", "第1章 総則")
    second = _retrieved("doc-1:b", "様式は第3章の規定で決める。", "第1章 総則")
    marked = _retrieved("doc-1:c", "第3章の手順で行う。", "第1章 総則")
    marked.metadata[REFERENCE_TARGETS_KEY] = "[]"
    plain = _retrieved("doc-1:d", "精算の概要。", "第1章 総則")
    oracle = ReferenceOracle([first, second, marked, plain], _section_chunks(3))
    state = _SearchState()

    result = await _engine(oracle)._search(
        SearchRequest(query="q", filters={"knowledge_base_id": "kb-1", "file_name": "経費"}),
        state,
        retrieval_queries=["q"],
    )

    order = [child.chunk_uid for child in result.child_chunks]
    # 2 つ目の候補は同じ参照先の残り(足した chunk は重ねない)。
    assert order == [
        "doc-1:a",
        "doc-1:cs-1:s0",
        "doc-1:cs-1:s1",
        "doc-1:b",
        "doc-1:cs-1:s2",
        "doc-1:c",
        "doc-1:d",
    ]
    # 見出しの列は文書と版(chunk_set)ごとに 1 回だけ、検索と同じ範囲(KB)で読む。取込で解決済みの
    # 印(空の列)のある chunk と、参照の無い chunk では読まない。
    assert oracle.section_calls == [
        {"knowledge_base_id": "kb-1", "document_id": "doc-1", "chunk_set_id": "cs-1"}
    ]
    assert [call["section_path"] for call in oracle.reference_calls] == ["第3章 承認"]
    assert {item["resolved_at"] for item in state.reference_expansions.values()} == {"query"}
    assert [t.section_path for t in state.query_reference_targets["doc-1:a"]] == ["第3章 承認"]


async def test_search_skips_section_read_for_other_document_references_only() -> None:
    anchor = _retrieved("doc-1:a", "出張旅費規程の第2章の規定で決める。", "第1章 総則")
    oracle = ReferenceOracle([anchor], [])

    result = await _engine(oracle)._search(
        SearchRequest(query="q"), _SearchState(), retrieval_queries=["q"]
    )

    assert [child.chunk_uid for child in result.child_chunks] == ["doc-1:a"]
    # 他の文書への参照だけなら、この文書の見出しは読まず、参照先の文書を範囲の中で探す。
    assert oracle.document_calls == [{"filters": {}, "title": "出張旅費規程"}]
    assert oracle.section_calls == []


# ---- 多段の評価セットの形(#1382) ----

MULTI_HOP_DIR = Path(__file__).resolve().parents[2] / "evaluation" / "multi-hop"
_PLAN_KB = "kb-eval"


def _html_inner(pattern: str, html: str) -> str:
    match = re.search(pattern, html, re.S)
    assert match is not None
    return re.sub(r"<[^>]+>", "", match.group(1)).strip()


def _plan_chunks(file_name: str, document_id: str, chunk_set_id: str) -> list[RetrievedChunk]:
    """評価の資料(定期保守計画の原稿)を章ごとの chunk にする(Docling の章ごとの分割と同じ形)。"""
    html = (MULTI_HOP_DIR / "sources" / f"{Path(file_name).stem}.html").read_text(encoding="utf-8")
    title = _html_inner(r"<h1>(.*?)</h1>", html)
    chunks: list[RetrievedChunk] = []
    for index, part in enumerate(re.split(r"(?=<h2>)", html)[1:]):
        heading = _html_inner(r"<h2>(.*?)</h2>", part)
        body = "\n".join(
            re.sub(r"<[^>]+>", "", item).strip()
            for item in re.findall(r"<(?:p|li)>.*?</(?:p|li)>", part, re.S)
        )
        chunks.append(
            _retrieved(
                f"{document_id}:{chunk_set_id}:{index}",
                body,
                f"{title} > {heading}",
                document_id=document_id,
                file_name=file_name,
                chunk_set_id=chunk_set_id,
            )
        )
    return chunks


def _annotated(chunks: list[RetrievedChunk], file_name: str) -> list[RetrievedChunk]:
    """取込と同じく、chunk の本文の参照を同じ文書の見出しの列で解決して metadata に残す。"""
    staged = [
        _chunk(chunk_index, chunk.text, str(chunk.metadata["section_path"]))
        for chunk_index, chunk in enumerate(chunks)
    ]
    annotate_cross_references(staged, document_title=file_name)
    return [
        chunk.model_copy(update={"metadata": {**chunk.metadata, **staged_chunk.metadata}})
        for chunk, staged_chunk in zip(chunks, staged, strict=True)
    ]


def _common_window_cases() -> list[dict[str, Any]]:
    payload = json.loads((MULTI_HOP_DIR / "multi-hop.json").read_text(encoding="utf-8"))
    return [
        case
        for case in payload["cases"]
        if any(item["id"] == "mt-common" for item in case.get("required_evidence", []))
    ]


@pytest.mark.parametrize("resolved_at", ["ingest", "query"], ids=["ingest", "query"])
async def test_multi_hop_common_window_reference_reaches_chapter_two(resolved_at: str) -> None:
    """定期保守計画の第 1 章の「第 2 章の共通の保守枠で保守します」から第 2 章が文脈に入る。

    #1362 の評価で A(実体のレシピあり)が欠けた根拠 4 件(`mt-common`)の型。第 1 章だけが
    検索に当たっても、同じ文書の同じ版の第 2 章を足し、旧版(2025 年度)の第 2 章は足さない。
    """
    from app.rag.evaluation_handling import contains_normalized

    current = _plan_chunks("maintenance-plan.pdf", "doc-plan", "cs-plan")
    old = _plan_chunks("maintenance-plan-2025.pdf", "doc-plan-2025", "cs-old")
    if resolved_at == "ingest":
        current = _annotated(current, "maintenance-plan.pdf")
        old = _annotated(old, "maintenance-plan-2025.pdf")
    chapter_one = current[0]
    assert "第 2 章の共通の保守枠" in chapter_one.text
    # 旧版は新しい版に置き換えた文書なので、検索の範囲(既定)に入らない。
    oracle = ReferenceOracle(
        [chapter_one], [*current, *old], knowledge_bases={_PLAN_KB: {"doc-plan"}}
    )
    state = _SearchState()

    result = await _engine(oracle)._search(
        SearchRequest(query="保守枠", filters={"knowledge_base_id": _PLAN_KB}),
        state,
        retrieval_queries=["保守枠"],
    )

    context = "\n".join(child.text for child in result.child_chunks)
    cases = _common_window_cases()
    assert {case["id"] for case in cases} >= {"br-budget-change-window", "br-sales-change-window"}
    for case in cases:
        evidence = [
            item
            for item in case["required_evidence"]
            if item["document_id"] == "file:maintenance-plan.pdf"
        ]
        missing = [
            item["id"] for item in evidence if not contains_normalized(context, item["text"])
        ]
        assert missing == [], case["id"]
    assert "毎月最終土曜日" not in context
    added = state.chunks["doc-plan:cs-plan:1"]
    assert added.metadata[REFERENCE_FROM_KEY] == chapter_one.chunk_id
    assert added.metadata[REFERENCE_LABEL_KEY] == "第 2 章"
    # 章の参照の参照先には、関連の順(文書名だけの参照の印)を付けない。
    assert REFERENCE_RANK_KEY not in added.metadata
    assert {item["resolved_at"] for item in state.reference_expansions.values()} == {resolved_at}
    # 参照先は KB の範囲と参照元の版(chunk_set)だけで読む(文書名などの絞り込みは外す)。
    assert all(
        call["filters"] == {"knowledge_base_id": _PLAN_KB} for call in oracle.reference_calls
    )
    assert {call["chunk_set_id"] for call in oracle.reference_calls} == {"cs-plan"}


async def test_multi_hop_common_window_reference_stays_within_knowledge_base() -> None:
    """参照元の文書が検索の範囲(ナレッジベース)の外なら、見出しも参照先も読めず足さない。"""
    current = _plan_chunks("maintenance-plan.pdf", "doc-plan", "cs-plan")
    oracle = ReferenceOracle([current[0]], current, knowledge_bases={_PLAN_KB: set()})
    state = _SearchState()

    result = await _engine(oracle)._search(
        SearchRequest(query="保守枠", filters={"knowledge_base_id": _PLAN_KB}),
        state,
        retrieval_queries=["保守枠"],
    )

    assert [child.chunk_uid for child in result.child_chunks] == [current[0].chunk_id]
    assert state.reference_expansions == {}
    assert oracle.reference_calls == []


# ---- 文書名の参照(#1400) ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("時間帯は定期保守計画で確かめます。", [("定期保守計画", "定期保守計画")]),
        # PDF の抽出で文字の間に空白が入る(dev の評価の KB の本文)。
        ("時間帯は定期保守計画で確かめま す。", [("定期保守計画", "定期保守計画")]),
        ("時間帯は定期保守計 画で確かめます。", [("定期保守計画", "定期保守計画")]),
        (
            "担当部署はシステム台帳で、承認者は組織規程で確かめます。",
            [("システム台帳", "システム台帳"), ("組織規程", "組織規程")],
        ),
        ("サンプル社の就業規則に従う。", [("就業規則", "就業規則")]),
        (
            "『経費精算マニュアル』に従って精算する。",
            [("『経費精算マニュアル』", "経費精算マニュアル")],
        ),
        ("ＨＲＭ運用手順書を確認する。", [("HRM運用手順書", "HRM運用手順書")]),
        ("保管の期間はデータ保管規程による。", [("データ保管規程", "データ保管規程")]),
    ],
    ids=[
        "check-with",
        "pdf-space-in-predicate",
        "pdf-space-in-name",
        "coordinated",
        "company-prefix",
        "quoted",
        "full-width",
        "according-to",
    ],
)
def test_extract_document_name_references(text: str, expected: list[tuple[str, str]]) -> None:
    specs = [spec for spec in extract_references(text) if spec.kind == "document"]

    assert [(spec.label, spec.document_title) for spec in specs] == expected
    assert all(spec.key == "" for spec in specs)


@pytest.mark.parametrize(
    "text",
    [
        "承認者を確認します。",
        "本手順書に従って作業する。",
        "同規程に従う。",
        "画面で確認してください。",
        "利用者の担当部署を確認する。",
        "上の表を見て決める。",
        "保守計画を見直す。",
        "システムで確認する。",
        "マニュアルに従う。",
        "台帳で確かめます。",
        "第3章で確認する。",
        "別紙1で確認する。",
    ],
    ids=[
        "role",
        "this-procedure",
        "same-regulation",
        "screen",
        "generic-department",
        "table",
        "review-verb",
        "generic-system",
        "suffix-only",
        "too-short",
        "chapter-label",
        "appendix-label",
    ],
)
def test_extract_document_name_references_ignores_generic_words(text: str) -> None:
    """一般語・短い語・同じ文書を指す語・章や別紙の表記は、文書名の参照として拾わない。"""
    assert [spec for spec in extract_references(text) if spec.kind == "document"] == []


@pytest.mark.parametrize(
    ("name", "file_name", "headings", "expected"),
    [
        ("定期保守計画", "maintenance-plan.pdf", ["定期保守計画 2026年度"], 0),
        ("組織規程", "organization-rules.pdf", ["組織規程（システムの担当と承認者）"], 0),
        ("定期保守計画", "定期保守計画_2026.pdf", [], 0),
        ("定期保守計画", "plan.pdf", ["サンプル社 定期保守計画 第2版"], 0),
        ("HRM手順書", "hrm.pdf", ["ＨＲＭ手順書"], 0),
        ("経費精算マニュアル", "旧経費精算マニュアル集.pdf", [], 1),
        ("保守計画", "maintenance-plan.pdf", ["定期保守計画 2026年度"], None),
        ("定期保守計画", "guide.pdf", ["第 1 章 定期保守計画"], None),
        ("定期保守計画", "system-ledger.xlsx", ["システム台帳"], None),
    ],
    ids=[
        "title-with-year",
        "title-with-parenthetical",
        "file-name",
        "company-and-edition",
        "nfkc",
        "file-name-contains",
        "partial-title",
        "numbered-heading",
        "other-document",
    ],
)
def test_document_name_match(
    name: str, file_name: str, headings: list[str], expected: int | None
) -> None:
    """文書名は検索範囲の文書のタイトル(番号の無い先頭の見出し)・ファイル名と別名で照合する。"""
    assert document_name_match(name, file_name, headings) == expected


def test_document_name_aliases_strip_edition_and_parenthetical() -> None:
    assert "定期保守計画" in document_name_aliases("定期保守計画 2026年度")
    assert "maintenanceplan" in document_name_aliases("maintenance-plan-2025.pdf")
    assert "組織規程" in document_name_aliases("組織規程(システムの担当と承認者)")
    assert "iso9001品質マニュアル" in document_name_aliases("ISO 9001 品質マニュアル 第2版")


def test_annotate_cross_references_keeps_document_name_references_unresolved() -> None:
    """取込は文書名だけの参照を未解決のまま残す(どの文書かは回答のときに検索範囲で決める)。"""
    chunks = [
        _chunk(0, "時間帯は定期保守計画で確かめます。", "第4章 作業の時間帯"),
        _chunk(1, "精算は経費精算規程に従う。", "第1章 総則"),
    ]

    annotate_cross_references(chunks, document_title="経費精算規程.pdf")

    (target,) = reference_targets(chunks[0].metadata)
    assert (target.kind, target.document_title, target.section_path) == (
        "document",
        "定期保守計画",
        None,
    )
    # 自分の文書名の参照は残さない(空の列は解決済みの印)。
    assert chunks[1].metadata[REFERENCE_TARGETS_KEY] == "[]"


_SALES_CASE = "br-sales-change-window"
_OTHER_KB = "kb-other"


def _sales_case() -> dict[str, Any]:
    payload = json.loads((MULTI_HOP_DIR / "multi-hop.json").read_text(encoding="utf-8"))
    return next(case for case in payload["cases"] if case["id"] == _SALES_CASE)


def _change_window_corpus(resolved_at: str) -> tuple[RetrievedChunk, list[RetrievedChunk]]:
    """変更手順書の第 4 章(当たった chunk)と、定期保守計画の今の版・旧版・別の KB の写し。"""
    change = _plan_chunks("change-procedure.pdf", "doc-change", "cs-change")
    current = _plan_chunks("maintenance-plan.pdf", "doc-plan", "cs-plan")
    old = _plan_chunks("maintenance-plan-2025.pdf", "doc-plan-2025", "cs-old")
    if resolved_at == "ingest":
        change = _annotated(change, "change-procedure.pdf")
    elif resolved_at == "legacy":
        # #1400 より前の取込(印はあるが、文書名だけの参照を持たない)。
        change = [
            chunk.model_copy(update={"metadata": {**chunk.metadata, REFERENCE_TARGETS_KEY: "[]"}})
            for chunk in change
        ]
    (window,) = [chunk for chunk in change if "第 4 章" in str(chunk.metadata["section_path"])]
    assert "定期保守計画で確かめます" in window.text
    # 別のナレッジベースにある同じ名前の文書(旧版の写し)。
    copied = [
        chunk.model_copy(update={"document_id": "doc-plan-copy", "file_name": "定期保守計画.pdf"})
        for chunk in old
    ]
    return window, [*change, *current, *old, *copied]


@pytest.mark.parametrize(
    "resolved_at", ["ingest", "query", "legacy"], ids=["ingest", "query", "legacy"]
)
async def test_multi_hop_document_name_reference_reaches_maintenance_plan(
    resolved_at: str,
) -> None:
    """変更手順書の「時間帯は定期保守計画で確かめます」から、定期保守計画の保守枠が文脈に入る。

    #1390 の検証で `br-sales-change-window` の必要な根拠(共通の保守枠)が MCP の並びの 96 位の
    まま改善しなかった型(#1400)。参照先は同じナレッジベースの今の版の定期保守計画だけで、
    その中の質問に関連の高い chunk(rerank の順)を足す。旧版・別の KB の同じ名前の文書は足さない。
    """
    from app.rag.evaluation_handling import contains_normalized

    case = _sales_case()
    window, sections = _change_window_corpus(resolved_at)
    oracle = ReferenceOracle(
        [window],
        sections,
        knowledge_bases={_PLAN_KB: {"doc-change", "doc-plan"}, _OTHER_KB: {"doc-plan-copy"}},
    )
    genai = FakeGenAi({"共通の保守枠は": 0.9, "この章に無いシステム": 0.8, "保守枠を変えたい": 0.1})
    state = _SearchState()

    result = await _engine(oracle, genai)._search(
        SearchRequest(query=case["query"], filters={"knowledge_base_id": _PLAN_KB}),
        state,
        retrieval_queries=[case["query"]],
    )

    order = [child.chunk_uid for child in result.child_chunks]
    # 参照先の文書の質問に関連の高い 2 件を、参照元の直後に置く(第 3 章の保守枠の変更は足さない)。
    assert order == [window.chunk_id, "doc-plan:cs-plan:1", "doc-plan:cs-plan:0"]
    context = "\n".join(child.text for child in result.child_chunks)
    evidence = [
        item
        for item in case["required_evidence"]
        if item["document_id"] == "file:maintenance-plan.pdf"
    ]
    assert {item["id"] for item in evidence} == {"mt-common-ref", "mt-common"}
    assert [item["id"] for item in evidence if not contains_normalized(context, item["text"])] == []
    assert "毎月最終土曜日" not in context
    added = state.chunks["doc-plan:cs-plan:1"]
    assert added.metadata[REFERENCE_FROM_KEY] == window.chunk_id
    assert added.metadata[REFERENCE_LABEL_KEY] == "定期保守計画"
    # 参照先の中の関連の順(MCP の根拠の並びは 1 件目だけを確保する)。
    assert added.metadata[REFERENCE_RANK_KEY] == 1
    assert state.chunks["doc-plan:cs-plan:0"].metadata[REFERENCE_RANK_KEY] == 2
    assert {item["kind"] for item in state.reference_expansions.values()} == {"document"}
    expected_resolved_at = "ingest" if resolved_at == "ingest" else "query"
    assert {item["resolved_at"] for item in state.reference_expansions.values()} == {
        expected_resolved_at
    }
    # 文書は検索の範囲(KB)の中だけで探し、その文書の中を原質問で検索して rerank で選ぶ。
    assert oracle.document_calls == [
        {"filters": {"knowledge_base_id": _PLAN_KB}, "title": "定期保守計画"}
    ]
    assert oracle.search_calls == [
        {
            "query": case["query"],
            "filters": {"knowledge_base_id": _PLAN_KB, "document_id": "doc-plan"},
        }
    ]
    ((rerank_query, documents),) = genai.rerank_calls
    assert rerank_query == case["query"]
    # 見出しの列を先に置いた本文で並べる(rag_engine の rerank と同じ)。
    assert [document.split("\n", 1)[0] for document in documents] == [
        "見出し: 定期保守計画 2026年度 > 第 1 章 個別の保守枠",
        "見出し: 定期保守計画 2026年度 > 第 2 章 共通の保守枠",
        "見出し: 定期保守計画 2026年度 > 第 3 章 保守枠の変更",
    ]


async def test_document_name_reference_outside_scope_adds_nothing() -> None:
    """同じ名前の文書が検索の範囲(KB)の外・旧版だけなら、何も足さない。"""
    window, sections = _change_window_corpus("query")
    oracle = ReferenceOracle(
        [window],
        sections,
        knowledge_bases={_PLAN_KB: {"doc-change"}, _OTHER_KB: {"doc-plan-copy", "doc-plan"}},
    )
    state = _SearchState()

    result = await _engine(oracle)._search(
        SearchRequest(query="作業の時間帯", filters={"knowledge_base_id": _PLAN_KB}),
        state,
        retrieval_queries=["作業の時間帯"],
    )

    assert [child.chunk_uid for child in result.child_chunks] == [window.chunk_id]
    assert state.reference_expansions == {}
    assert oracle.search_calls == []


async def test_document_name_reference_needs_exact_title() -> None:
    """文書名がタイトルの一部(「保守計画」→「定期保守計画」)・一般語なら辿らない。"""
    plan = _plan_chunks("maintenance-plan.pdf", "doc-plan", "cs-plan")
    partial = _retrieved("doc-1:a", "時間帯は保守計画で確かめます。", "第1章 総則")
    generic = _retrieved("doc-1:b", "詳しくは画面で確認してください。", "第1章 総則")
    oracle = ReferenceOracle([partial, generic], plan)
    state = _SearchState()

    result = await _engine(oracle)._search(SearchRequest(query="q"), state, retrieval_queries=["q"])

    assert [child.chunk_uid for child in result.child_chunks] == ["doc-1:a", "doc-1:b"]
    # 一般語は照合の SQL も読まない。
    assert oracle.document_calls == [{"filters": {}, "title": "保守計画"}]
    assert oracle.search_calls == []


@pytest.mark.parametrize("mode", ["disabled", "failed"], ids=["rerank-disabled", "rerank-failed"])
async def test_document_name_reference_keeps_search_order_without_rerank(mode: str) -> None:
    """rerank が無効・失敗のときは、参照先の文書の中の検索の順で足す。"""
    window, sections = _change_window_corpus("query")
    oracle = ReferenceOracle(
        [window], sections, knowledge_bases={_PLAN_KB: {"doc-change", "doc-plan"}}
    )
    genai = FakeGenAi({"共通の保守枠は": 0.9}, fail=mode == "failed")
    settings = {"rag_rerank_enabled": False} if mode == "disabled" else {}

    result = await _engine(oracle, genai, **settings)._search(
        SearchRequest(query="q", filters={"knowledge_base_id": _PLAN_KB}),
        _SearchState(),
        retrieval_queries=["q"],
    )

    assert [child.chunk_uid for child in result.child_chunks] == [
        window.chunk_id,
        "doc-plan:cs-plan:0",
        "doc-plan:cs-plan:1",
    ]
    assert len(genai.rerank_calls) == (0 if mode == "disabled" else 1)


async def test_reference_target_in_lower_candidates_moves_after_citing_anchor() -> None:
    """参照先が起点より下位の候補にあるときは、起点の直後へ移す(元の位置には置かない。#1400)。

    回答の検索は候補が多い(既定 150)ため、小さなナレッジベースでは参照先がすでに下位の候補や
    前後の文脈にあり、足せる chunk が無かった(参照が辿られなかった)。上位の起点は動かさない。
    """
    anchor = _retrieved("doc-1:a", "承認の条件は第3章を参照。", "第1章 総則", targets=[CHAPTER3])
    others = [_retrieved(f"doc-1:o{n}", "精算の概要。", "第1章 総則") for n in range(4)]
    sections = _section_chunks(2)
    oracle = ReferenceOracle([anchor, *others, sections[1]], sections)
    state = _SearchState()

    result = await _engine(oracle)._search(SearchRequest(query="q"), state, retrieval_queries=["q"])

    order = [child.chunk_uid for child in result.child_chunks]
    assert order == [
        "doc-1:a",
        "doc-1:cs-1:s0",
        "doc-1:cs-1:s1",
        "doc-1:o0",
        "doc-1:o1",
        "doc-1:o2",
        "doc-1:o3",
    ]
    assert state.chunks["doc-1:cs-1:s1"].metadata[REFERENCE_FROM_KEY] == "doc-1:a"

    # 上位 5 件の起点の中にある参照先は、その位置のまま(重ねて足さない)。
    top = ReferenceOracle([anchor, sections[1], *others], sections)
    result = await _engine(top)._search(
        SearchRequest(query="q"), _SearchState(), retrieval_queries=["q"]
    )
    assert [child.chunk_uid for child in result.child_chunks] == [
        "doc-1:a",
        "doc-1:cs-1:s0",
        "doc-1:cs-1:s1",
        "doc-1:o0",
        "doc-1:o1",
        "doc-1:o2",
        "doc-1:o3",
    ]


def _sources(search: dict[str, object]) -> list[dict[str, Any]]:
    """診断の交差参照の起点の記録(検索 1 回分)の起点の列。"""
    return cast(list[dict[str, Any]], search["sources"])


def _filler(count: int) -> list[RetrievedChunk]:
    """参照を持たない上位の候補(統合の順位で参照の起点を下げる)。"""
    return [
        _retrieved(
            f"doc-guide:o{n}",
            "システムの運用の概要。",
            "第1章 総則",
            document_id="doc-guide",
            file_name="system-operation-guide.pdf",
        )
        for n in range(count)
    ]


@pytest.mark.parametrize(
    ("top_k", "followed"), [(20, True), (5, False)], ids=["top-k-20", "top-k-5"]
)
async def test_document_name_reference_outside_top_five_is_followed(
    top_k: int, followed: bool
) -> None:
    """参照を持つ chunk が統合の上位 5 件の外(8 位)でも、回答の文脈に入りうる上位なら辿る(#1549)。

    kb1517 の `br-budget-change-window`: LLM が作る検索文が前の記録と変わり、変更手順書の
    「時間帯は定期保守計画で確かめます」の chunk が統合の 8 位(前の記録は 2 位)になって、定期保守
    計画の共通の保守枠を辿らず、根拠の連鎖が欠けた。起点の範囲は回答の検索の top_k(20 件まで)。
    辿った・辿らなかった起点は、統合の順位と理由を診断に残す。
    """
    case = _sales_case()
    window, sections = _change_window_corpus("ingest")
    others = _filler(7)
    oracle = ReferenceOracle(
        [*others, window],
        sections,
        knowledge_bases={_PLAN_KB: {"doc-change", "doc-plan", "doc-guide"}},
    )
    genai = FakeGenAi({"共通の保守枠は": 0.9, "この章に無いシステム": 0.8})
    state = _SearchState()

    result = await _engine(oracle, genai)._search(
        SearchRequest(query=case["query"], top_k=top_k, filters={"knowledge_base_id": _PLAN_KB}),
        state,
        retrieval_queries=[case["query"]],
    )

    order = [child.chunk_uid for child in result.child_chunks]
    assert order[:7] == [chunk.chunk_id for chunk in others]
    (search,) = state.reference_sources
    assert search["top_sources"] == 5
    assert search["source_limit"] == top_k
    (source,) = _sources(search)
    assert source["chunk_id"] == window.chunk_id
    assert source["fused_rank"] == 8
    assert source["rrf_score"] > 0
    assert {"label": "定期保守計画", "kind": "document"} in source["targets"]
    if followed:
        # 参照先の文書の質問に関連の高い 2 件を、起点の直後に置く(上位 5 件の起点と同じ)。
        assert order[7:] == [window.chunk_id, "doc-plan:cs-plan:1", "doc-plan:cs-plan:0"]
        assert source["outcome"] == "added"
        assert "added" in source["target_outcomes"]
        assert search["added_count"] == 2
        assert {item["from_fused_rank"] for item in state.reference_expansions.values()} == {8}
        assert state.chunks["doc-plan:cs-plan:1"].metadata[REFERENCE_FROM_KEY] == window.chunk_id
        # rag_engine へは参照元の印を渡す(rerank の後に参照元の直後へ置く。#1549)。
        assert result.child_chunks[8].metadata["reference_expansion"] == {
            "from_chunk_uid": window.chunk_id,
            "label": "定期保守計画",
            "rank": 1,
        }
        assert "reference_expansion" not in result.child_chunks[7].metadata
    else:
        # top_k が 5 なら起点は上位 5 件だけ(辿らなかった理由を残す。DB も読まない)。
        assert order[7:] == [window.chunk_id]
        assert source["outcome"] == "outside_sources"
        assert state.reference_expansions == {}
        assert oracle.document_calls == []
        assert oracle.search_calls == []
    # 統合の順位と点は根拠の木にも出す(検索の候補ごと。複数回の検索では最も上の順位)。
    assert state.fused_ranks[window.chunk_id][0] == 8
    tree = _outcome_from_result(_TreeResult([window.chunk_id, "doc-plan:cs-plan:1"]), state)
    children = tree.diagnostics["evidence_tree"][0]["children"]
    assert children[0]["fused_rank"] == 8
    assert children[0]["rrf_score"] == pytest.approx(
        state.fused_ranks[window.chunk_id][1], abs=1e-6
    )
    if followed:
        # 参照先として読んだ chunk(検索の候補に無いもの)には統合の順位が無い。
        assert "fused_rank" not in children[1]
    else:
        # 辿らなかった参照先は読んでいないので、引用にも根拠の木にも出さない(#2097)。
        assert [child["chunk_id"] for child in children] == [window.chunk_id]


class _TreeResult:
    """rag_engine の回答の結果(``_outcome_from_result`` が読む属性だけ。根拠の木の確認用)。"""

    def __init__(self, chunk_ids: list[str]) -> None:
        self.evidence_items = [
            {
                "chunk_id": "p",
                "children": [
                    {"chunk_id": chunk_id, "retrieval_role": "retrieved_anchor"}
                    for chunk_id in chunk_ids
                ],
            }
        ]
        self.generation_trace: dict[str, Any] = {}
        self.execution_steps: list[Any] = []
        self.generated_queries: list[str] = []
        self.text_search_tokens: list[str] = []
        self.crag_attempts: list[Any] = []
        self.envelope: dict[str, Any] = {}
        self.external_data_items: list[Any] = []
        self.question_type: list[str] = []
        self.answer_text = "回答"

    def __getattr__(self, name: str) -> Any:
        return None


async def test_document_name_reference_adds_sentence_citing_selected_section() -> None:
    """文書名の参照で選んだ節を同じ文書の中で参照する文(適用の条件)も足す(#1549)。

    kb1517 では定期保守計画が章ごとの親に分かれ、第 2 章(共通の保守枠)と第 3 章を足しても、第 1 章の
    「この章に無いシステムは、第 2 章の共通の保守枠で保守します」(`mt-common-ref`)が文脈に
    入らなかった。
    参照先の文書の中の検索の候補のうち、取込で選んだ節(第 2 章)への参照を持つ chunk を足す。
    """
    case = _sales_case()
    window, _sections = _change_window_corpus("ingest")
    plan = _annotated(
        _plan_chunks("maintenance-plan.pdf", "doc-plan", "cs-plan"), "maintenance-plan.pdf"
    )
    oracle = ReferenceOracle([window], plan, knowledge_bases={_PLAN_KB: {"doc-change", "doc-plan"}})
    genai = FakeGenAi({"共通の保守枠は": 0.9, "保守枠を変えたい": 0.8, "この章に無い": 0.1})
    state = _SearchState()

    result = await _engine(oracle, genai)._search(
        SearchRequest(query=case["query"], filters={"knowledge_base_id": _PLAN_KB}),
        state,
        retrieval_queries=[case["query"]],
    )

    order = [child.chunk_uid for child in result.child_chunks]
    # 関連の高い 2 件(第 2 章・第 3 章)の後に、第 2 章を参照する第 1 章を置く。
    assert order == [
        window.chunk_id,
        "doc-plan:cs-plan:1",
        "doc-plan:cs-plan:2",
        "doc-plan:cs-plan:0",
    ]
    assert state.chunks["doc-plan:cs-plan:0"].metadata[REFERENCE_RANK_KEY] == 3
    assert "この章に無いシステムは" in state.chunks["doc-plan:cs-plan:0"].text

    # 上限(rag_reference_expansion_max_chunks)は超えない。
    limited = _SearchState()
    result = await _engine(oracle, genai, rag_reference_expansion_max_chunks=2)._search(
        SearchRequest(query=case["query"], filters={"knowledge_base_id": _PLAN_KB}),
        limited,
        retrieval_queries=[case["query"]],
    )
    assert [child.chunk_uid for child in result.child_chunks][1:] == [
        "doc-plan:cs-plan:1",
        "doc-plan:cs-plan:2",
    ]


async def test_extended_reference_sources_follow_one_hop_without_moving_sources() -> None:
    """上位 5 件の外の起点どうしで、参照先を足した起点は他の起点の参照先として移さない(#1549)。

    6 位の候補(第 3 章の chunk。別紙 1 を参照)と、7 位の候補(第 3 章を参照)。6 位の参照先(別紙)を
    足した後、7 位の参照先(第 3 章)からは 6 位の候補を除いて足す。参照先として足した候補は起点に
    しない(参照は 1 段)。
    """
    appendix_target = ReferenceTarget("別紙1", "appendix", "別紙:1", None, "別紙1 申請書様式")
    source = _retrieved(
        "doc-1:cs-1:s0",
        "承認の条件。様式は別紙1を参照。",
        "第3章 承認 > 3.1 申請",
        targets=[appendix_target],
    )
    citing = _retrieved("doc-1:f", "承認は第3章を参照。", "第1章 総則", targets=[CHAPTER3])
    chapter = [source, *_section_chunks(3)[1:]]
    appendix = [_retrieved(f"doc-1:cs-1:x{n}", "様式", "別紙1 申請書様式") for n in range(2)]
    oracle = ReferenceOracle([*_filler(5), source, citing], [*chapter, *appendix])
    state = _SearchState()

    result = await _engine(oracle)._search(SearchRequest(query="q"), state, retrieval_queries=["q"])

    order = [child.chunk_uid for child in result.child_chunks]
    assert order[5:] == [
        "doc-1:cs-1:s0",
        "doc-1:cs-1:x0",
        "doc-1:cs-1:x1",
        "doc-1:f",
        "doc-1:cs-1:s1",
        "doc-1:cs-1:s2",
    ]
    (search,) = state.reference_sources
    assert [(item["fused_rank"], item["outcome"]) for item in _sources(search)] == [
        (6, "added"),
        (7, "added"),
    ]

    # 7 位の候補が 6 位の候補の参照先なら(参照先を先に足した候補)、7 位は起点にしない。
    first = _retrieved("doc-1:f", "承認は第3章を参照。", "第1章 総則", targets=[CHAPTER3])
    later = source.model_copy(update={"chunk_id": "doc-1:cs-1:s0"})
    oracle = ReferenceOracle([*_filler(5), first, later], [*chapter, *appendix])
    state = _SearchState()

    result = await _engine(oracle)._search(SearchRequest(query="q"), state, retrieval_queries=["q"])

    order = [child.chunk_uid for child in result.child_chunks]
    assert order[5:] == ["doc-1:f", "doc-1:cs-1:s0", "doc-1:cs-1:s1"]
    (search,) = state.reference_sources
    assert [(item["fused_rank"], item["outcome"]) for item in _sources(search)] == [
        (6, "added"),
        (7, "referenced"),
    ]


# ---- Oracle の SQL ----


async def test_oracle_reference_queries_use_search_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.clients.oracle import OracleClient

    client = OracleClient(settings=Settings())
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append((statement, dict(binds or {})))
        if "first_chunk_index" in statement:
            # 範囲の chunk の見出しの列ごとの行（#2800。文書名との照合は Python で行う）。
            return [
                {
                    "document_id": "doc-2",
                    "file_name": "経費精算マニュアル.pdf",
                    "section_path": None,
                    "first_chunk_index": 0,
                },
                {
                    "document_id": "doc-3",
                    "file_name": "manual.pdf",
                    "section_path": "経費精算マニュアル 第2版 > 1 はじめに",
                    "first_chunk_index": 0,
                },
                {
                    "document_id": "doc-3",
                    "file_name": "manual.pdf",
                    "section_path": "第1章 経費精算マニュアルの使い方",
                    "first_chunk_index": 3,
                },
                {
                    "document_id": "doc-3",
                    "file_name": "manual.pdf",
                    "section_path": "第9章 別の話",
                    "first_chunk_index": 9,
                },
                {
                    "document_id": "doc-4",
                    "file_name": "ＨＲＭ手順書.pdf",
                    "section_path": None,
                    "first_chunk_index": 0,
                },
            ]
        return []

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)
    filters = {"knowledge_base_id": "kb-1"}

    assert (
        await client.retrieval_reference_chunks(
            filters, document_id="doc-1", section_path="第3章 承認", chunk_set_id="cs-1", limit=7
        )
        == []
    )
    # 文書名の短い順（文書名か見出しの列が文書名を含む。見出しは列の先頭の見出しを読み順に）。
    assert await client.retrieval_reference_documents(
        filters, title="経費精算マニュアル", limit=5
    ) == [
        ("doc-3", "manual.pdf", ("経費精算マニュアル 第2版", "第1章 経費精算マニュアルの使い方")),
        ("doc-2", "経費精算マニュアル.pdf", ()),
    ]

    chunk_sql, chunk_binds = calls[0]
    assert "kb-1" in chunk_binds.values()
    assert chunk_binds["filter_document_id"] == "doc-1"
    assert chunk_binds["filter_chunk_set_id"] == "cs-1"
    assert chunk_binds["reference_section_path"] == "第3章 承認"
    assert chunk_binds["reference_section_prefix"] == "第3章 承認 > "
    assert chunk_binds["reference_prefix_length"] == len("第3章 承認 > ")
    assert chunk_binds["reference_limit"] == 7
    assert "ORDER BY c.chunk_set_id, c.chunk_index" in chunk_sql
    document_sql, document_binds = calls[1]
    assert "kb-1" in document_binds.values()
    assert "superseded_by_document_id IS NULL" in document_sql
    # 文書名に加えて、見出しの列(文書のタイトル)でも探す。
    assert "$.section_path" in document_sql
    # 全角の英数字で保存した文書名・見出しとも比べる(#1400)。
    assert await client.retrieval_reference_documents(filters, title="hrm手順書", limit=5) == [
        ("doc-4", "ＨＲＭ手順書.pdf", ())
    ]
    # 件数の上限は文書名ごと（文書と見出しの組で数える）。
    assert await client.retrieval_reference_documents(
        filters, title="経費精算マニュアル", limit=1
    ) == [("doc-3", "manual.pdf", ("経費精算マニュアル 第2版",))]


async def test_oracle_reference_documents_many_reads_chunks_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """複数の文書名を、範囲の chunk を 1 回読んだ行で照合し、文書名ごとに返す(#2800)。"""
    from app.clients.oracle import OracleClient

    client = OracleClient(settings=Settings())
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append((statement, dict(binds or {})))
        return [
            {
                "document_id": "doc-1",
                "file_name": "a.pdf",
                "section_path": "A 手順書 > 1",
                "first_chunk_index": 0,
            },
            {
                "document_id": "doc-2",
                "file_name": "b規程.pdf",
                "section_path": None,
                "first_chunk_index": 0,
            },
            {
                "document_id": "doc-3",
                "file_name": "bb規程.pdf",
                "section_path": "B",
                "first_chunk_index": 2,
            },
        ]

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)

    found = await client.retrieval_reference_documents_many(
        {"knowledge_base_id": "kb-1"}, titles=["手順書", "規程", "手順書", ""], limit=3
    )

    assert found == {
        "手順書": [("doc-1", "a.pdf", ("A 手順書",))],
        "規程": [("doc-2", "b規程.pdf", ()), ("doc-3", "bb規程.pdf", ("B",))],
    }
    ((statement, _binds),) = calls
    # 範囲の chunk は 1 回だけ、見出しの列ごとの行で読む（文書名の数だけ読み直さない）。
    assert statement.count("FROM rag_chunks c") == 1
    assert "GROUP BY x.document_id, x.file_name, x.section_path" in statement
    assert await client.retrieval_reference_documents_many({}, titles=[], limit=3) == {}
    assert len(calls) == 1


# ---- 参照先の章節の拡張(#3325) ----

_SECTION_KB = "kb-1"
_OUTSIDE_KB = "kb-2"


def _section_corpus() -> list[RetrievedChunk]:
    """文書 A は文書 B の第3章を、文書 C は文書 B の全体を参照する(合成。B は章ごとに 1 chunk)。"""
    return [
        _retrieved(
            "doc-a:cs-1:0",
            "合成の操作は合成文書Bの第3章を参照。",
            "第1章 概要",
            document_id="doc-a",
            file_name="合成文書A.md",
            targets=[ReferenceTarget("第3章", "chapter", "3", "合成文書B")],
        ),
        _retrieved(
            "doc-c:cs-3:0",
            "合成の操作は『合成文書B』を参照。",
            "第1章 概要",
            document_id="doc-c",
            file_name="合成文書C.md",
            chunk_set_id="cs-3",
            targets=[ReferenceTarget("合成文書B", "document", "合成文書B", "合成文書B")],
        ),
        _retrieved(
            "doc-b:cs-9:0",
            "合成文書Bの概要の本文。",
            "第1章 概要",
            document_id="doc-b",
            file_name="合成文書B.md",
            chunk_set_id="cs-9",
        ),
        _retrieved(
            "doc-b:cs-9:1",
            "合成の入力画面で確認ボタンを押す。",
            "第3章 手順",
            document_id="doc-b",
            file_name="合成文書B.md",
            chunk_set_id="cs-9",
        ),
    ]


class SectionScopeMap:
    """章節と chunk の対応の代役(#1438)。

    章節の本文は ``SectionMapStore.section_chunks`` と同じく、渡された検索条件(文書・文書名・
    ナレッジベース)と読む文書の条件を重ねて読む(条件に合わなければ空)。読んだときの監査 context
    (認可の範囲)も残す。
    """

    def __init__(self, chunks: list[RetrievedChunk], knowledge_bases: dict[str, set[str]]) -> None:
        self.chunks = chunks
        self.knowledge_bases = knowledge_bases
        self.reads: list[tuple[str, str, dict[str, str]]] = []
        self.contexts: list[Any] = []

    @staticmethod
    def _section_id(chunk: RetrievedChunk) -> str:
        return f"{chunk.document_id}:{chunk.metadata['section_path']}"

    def _document_chunks(self, document_id: str) -> list[RetrievedChunk]:
        return [chunk for chunk in self.chunks if chunk.document_id == document_id]

    async def chunk_section(
        self, document_id: str, chunk_id: str, *, oracle: Any = None
    ) -> tuple[str, str] | None:
        for chunk in self._document_chunks(document_id):
            if chunk.chunk_id == chunk_id:
                return str(chunk.metadata["chunk_set_id"]), self._section_id(chunk)
        return None

    async def section_outline(
        self, document_id: str, *, chunk_set_id: str | None = None, oracle: Any = None
    ) -> Any:
        from app.clients.section_map_store import ReadableChunkSet
        from app.rag.section_chunk_map import SectionNode
        from app.rag.section_map_service import SectionOutline

        chunks = self._document_chunks(document_id)
        root = f"{document_id}:root"
        nodes = [SectionNode(root, chunks[0].file_name or "", 0, 0, None, 1, 1, len(chunks), 40)]
        nodes.extend(
            SectionNode(
                self._section_id(chunk),
                str(chunk.metadata["section_path"]),
                1,
                n,
                root,
                1,
                1,
                1,
                20,
            )
            for n, chunk in enumerate(chunks, start=1)
        )
        chunk_set = ReadableChunkSet(
            document_id, str(chunks[0].metadata["chunk_set_id"]), chunks[0].file_name or "", False
        )
        return SectionOutline(chunk_set=chunk_set, sections=nodes)

    async def read_section(
        self,
        document_id: str,
        section_id: str,
        *,
        chunk_set_id: str | None = None,
        limit: int | None = None,
        filters: dict[str, str] | None = None,
        oracle: Any = None,
        **_: Any,
    ) -> Any:
        from app.clients.section_map_store import SectionChunkRow
        from app.rag.request_context import current_audit_request_context
        from app.rag.section_map_service import SectionText

        scoped = dict(filters or {})
        self.reads.append((document_id, section_id, scoped))
        self.contexts.append(current_audit_request_context())
        outline = await self.section_outline(document_id)
        node = outline.get(section_id)
        knowledge_bases = [kb for kb in scoped.get("knowledge_base_id", "").split(",") if kb]
        visible = [
            chunk
            for chunk in self._document_chunks(document_id)
            if (section_id.endswith(":root") or self._section_id(chunk) == section_id)
            and scoped.get("document_id", document_id) == document_id
            and scoped.get("file_name", "") in (chunk.file_name or "")
            and int(str(chunk.metadata["page_start"])) <= int(scoped.get("page_number_max") or 9999)
            and (
                not knowledge_bases
                or any(document_id in self.knowledge_bases.get(kb, set()) for kb in knowledge_bases)
            )
        ]
        rows = [SectionChunkRow(order, chunk) for order, chunk in enumerate(visible)]
        return SectionText(outline=outline, section=node, chunks=rows, complete=True)


_SECTION_ANSWER = "合成の入力画面で確認ボタンを押す。"


def _section_answer_llm(
    system: str, prompt: str, settings: Any, schema: type, **options: Any
) -> Any:
    """CRAG は不足と判定して章節を広げ、回答は参照先の文書 B の手順を引用する LLM の代役。"""
    from rag_engine.models.llm import CragRetrievalGradeOutput, GroundedDraft

    from tests.test_answer_engine import _fake_llm

    if schema is CragRetrievalGradeOutput:
        candidates = json.loads(
            prompt.split("BEGIN_UNTRUSTED_CRAG_CANDIDATES\n", 1)[1].split(
                "END_UNTRUSTED_CRAG_CANDIDATES", 1
            )[0]
        )
        candidate = next((row for row in candidates if _SECTION_ANSWER in row["text"]), None)
        evidence = (
            [dict(source_id=candidate["chunk_uid"], quote=_SECTION_ANSWER)] if candidate else []
        )
        return CragRetrievalGradeOutput(
            reason="参照先の操作原文を確認" if candidate else "参照先の操作原文が必要",
            sufficient=bool(candidate),
            confidence=0.9 if candidate else 0.4,
            rewritten_query="",
            candidate_verdicts=[
                dict(chunk_uid=row["chunk_uid"], relevant=True, reason="参照先の資料")
                for row in candidates
            ],
            aspect_checks=[
                dict(
                    aspect=aspect,
                    status="supported" if candidate else "missing",
                    source_ids=[candidate["chunk_uid"]] if candidate else [],
                    evidence=evidence,
                    reason="操作原文の有無",
                )
                for aspect in ["business_object", "requested_result", "applicability", "procedure"]
            ],
            answerable_parts=[
                dict(aspect="procedure", kind="operation", claim=_SECTION_ANSWER, evidence=evidence)
            ]
            if candidate
            else [],
        )
    if schema in (GroundedDraft, generation_draft_wire_model()):
        # CRAGの判定に含まれる引用ではなく、番号付き根拠ブロックから出典を選ぶ。
        parts = re.split(r"\[(E[0-9]+)\]", prompt)
        ids = [
            parts[index] for index in range(1, len(parts), 2) if _SECTION_ANSWER in parts[index + 1]
        ]
        items = (
            [
                {
                    "kind": "operation",
                    "request_id": "Q1",
                    "text": _SECTION_ANSWER,
                    "citations": [{"evidence_id": ids[0], "quote": _SECTION_ANSWER}],
                }
            ]
            if _SECTION_ANSWER in prompt and ids
            else []
        )
        return GroundedDraft.model_validate(
            {"summary": "手順", "items": items, "confidence": "high"}
        )
    return _fake_llm(system, prompt, settings, schema, **options)


@pytest.fixture
def section_scope(monkeypatch: pytest.MonkeyPatch) -> SectionScopeMap:
    import rag_engine.adapters.oci as engine_oci

    from app.rag import section_map_service

    stub = SectionScopeMap(
        _section_corpus(), {_SECTION_KB: {"doc-a", "doc-b", "doc-c"}, _OUTSIDE_KB: {"doc-b"}}
    )
    monkeypatch.setattr(section_map_service, "chunk_section", stub.chunk_section)
    monkeypatch.setattr(section_map_service, "section_outline", stub.section_outline)
    monkeypatch.setattr(section_map_service, "read_section", stub.read_section)
    monkeypatch.setattr(engine_oci, "parse_text_response", _section_answer_llm)
    return stub


class SectionReferenceOracle(ReferenceOracle):
    """回答の実行(``AnswerEngine.run``)で読む部分も持つ交差参照の代役。"""

    async def has_retrieval_chunks(self, filters: dict[str, str]) -> bool:
        return True


class PageScopedReferenceOracle(SectionReferenceOracle):
    """主の検索は、呼び出し元のページの範囲の中の ``hits`` だけを返す代役。"""

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        return list(self.hits)


def _section_engine(oracle: ReferenceOracle) -> AnswerEngine:
    return _engine(
        oracle,
        FakeGenAi({"手順": 0.9}),
        rag_answer_flow="crag",
        rag_query_strategy="auto_routing",
        rag_section_expansion_enabled=True,
    )


@pytest.mark.parametrize(
    ("anchor_document", "added"),
    [("doc-a", ["doc-b:cs-9:1"]), ("doc-c", ["doc-b:cs-9:1", "doc-b:cs-9:0"])],
    ids=["chapter-of-other-document", "whole-other-document"],
)
async def test_section_expansion_reads_reference_target_outside_caller_document_filter(
    section_scope: SectionScopeMap, anchor_document: str, added: list[str]
) -> None:
    """文書を絞った検索の交差参照で足した文書 B の章節は、参照先と同じ範囲で読める(#3325)。

    呼び出し元の文書の絞り込み(document_id)は、参照で足した B の章節の読み取りに重ねない。
    ナレッジベース・基準日は残す。絞り込んだ文書 A / C の章節は呼び出し元の条件のまま読む。
    """
    oracle = SectionReferenceOracle(
        [], _section_corpus(), knowledge_bases={_SECTION_KB: {"doc-a", "doc-b", "doc-c"}}
    )
    filters = {"knowledge_base_id": _SECTION_KB, "document_id": anchor_document}

    outcome = await _section_engine(oracle).run(
        SearchRequest(query="合成の操作の手順は？", filters=filters)
    )

    references = outcome.diagnostics["reference_expansion"]
    assert sorted(item["chunk_id"] for item in references["chunks"]) == sorted(added)
    reads = section_scope.reads
    reference_reads = [scoped for document, _section, scoped in reads if document == "doc-b"]
    assert reference_reads
    # 参照先の章節は参照先を探した範囲(KB・基準日)で読み、呼び出し元の文書の条件を重ねない。
    assert all(set(scoped) == {"knowledge_base_id", "as_of"} for scoped in reference_reads)
    assert all(scoped["knowledge_base_id"] == _SECTION_KB for scoped in reference_reads)
    # 絞り込んだ文書の章節は呼び出し元の条件(文書の絞り込みを含む)のまま読む。
    own_reads = [scoped for document, _section, scoped in reads if document != "doc-b"]
    assert own_reads
    assert all(scoped["document_id"] == anchor_document for scoped in own_reads)
    expansion = outcome.diagnostics["section_expansion"]
    assert expansion["stop_reason"] != "partial_read"
    assert all(item.get("document_id") != "doc-b" for item in expansion.get("unread", []))
    assert "回答に未反映の関連章節" not in outcome.answer
    assert "原文の一部だけを取得" not in outcome.answer
    assert outcome.diagnostics["outcome"] == "answered"
    assert _SECTION_ANSWER in outcome.answer


async def test_section_expansion_does_not_read_reference_target_outside_knowledge_base(
    section_scope: SectionScopeMap,
) -> None:
    """検索範囲(KB)の外の文書は参照先にせず、その章節も読まない(#3325)。"""
    oracle = SectionReferenceOracle(
        [],
        _section_corpus(),
        knowledge_bases={_SECTION_KB: {"doc-a", "doc-c"}, _OUTSIDE_KB: {"doc-b"}},
    )

    outcome = await _section_engine(oracle).run(
        SearchRequest(
            query="合成の操作の手順は？",
            filters={"knowledge_base_id": _SECTION_KB, "document_id": "doc-a"},
        )
    )

    assert "reference_expansion" not in outcome.diagnostics or not outcome.diagnostics[
        "reference_expansion"
    ].get("chunks")
    assert section_scope.reads
    assert all(document == "doc-a" for document, _section, _scoped in section_scope.reads)
    assert all(
        scoped["document_id"] == "doc-a" for _document, _section, scoped in section_scope.reads
    )
    assert all(citation.document_id != "doc-b" for citation in outcome.citations)


async def test_section_expansion_keeps_caller_filters_for_sections_not_reached_by_reference(
    section_scope: SectionScopeMap,
) -> None:
    """同じ文書の中の参照で足した章節だけを参照先の範囲で読み、当たった章節は呼び出し元の条件で読む。

    参照で足した文書であることを理由に、その文書の他の章節の読み取りから呼び出し元の絞り込み
    (ページなど)を外さない(#3325)。
    """
    anchor = _retrieved(
        "doc-a:cs-1:0",
        "合成の操作は第3章を参照。",
        "第1章 概要",
        document_id="doc-a",
        file_name="合成文書A.md",
        targets=[ReferenceTarget("第3章", "chapter", "3", None, "第3章 手順")],
    )
    referenced = _retrieved(
        "doc-a:cs-1:1",
        _SECTION_ANSWER,
        "第3章 手順",
        document_id="doc-a",
        file_name="合成文書A.md",
    )
    # 参照先の章は呼び出し元のページの範囲の外(主の検索では当たらない)。
    referenced.metadata.update(page_start=5, page_end=5)
    section_scope.chunks = [anchor, referenced]
    oracle = PageScopedReferenceOracle(
        [anchor], [anchor, referenced], knowledge_bases={_SECTION_KB: {"doc-a"}}
    )
    caller = {"knowledge_base_id": _SECTION_KB, "document_id": "doc-a", "page_number_max": "1"}

    outcome = await _section_engine(oracle).run(
        SearchRequest(query="合成の操作の手順は？", filters=caller)
    )

    assert [item["chunk_id"] for item in outcome.diagnostics["reference_expansion"]["chunks"]] == [
        "doc-a:cs-1:1"
    ]
    reads = {section: scoped for _document, section, scoped in section_scope.reads}
    # 主の検索で当たった章節は、呼び出し元の条件(文書・ページ)のまま読む。
    assert reads["doc-a:第1章 概要"] == {**caller, "as_of": reads["doc-a:第1章 概要"]["as_of"]}
    # 参照で足した章節は、参照先と同じ範囲(KB・基準日)で読む。
    assert set(reads["doc-a:第3章 手順"]) == {"knowledge_base_id", "as_of"}
    assert all(
        item.get("section_id") != "doc-a:第3章 手順"
        for item in outcome.diagnostics["section_expansion"].get("unread", [])
    )


# ---- MCP の根拠 ----


def test_mcp_evidence_carries_references_and_origin() -> None:
    from app.mcp.tools import _evidence

    chunk = _retrieved("doc-1:a", "第3章を参照。", "第1章 総則", targets=[CHAPTER3])
    chunk.metadata[REFERENCE_FROM_KEY] = "doc-1:z"
    chunk.metadata[REFERENCE_LABEL_KEY] = "別紙1"

    evidence = _evidence(chunk)

    assert [ref.model_dump() for ref in evidence.references] == [
        {
            "label": "第3章",
            "document_title": None,
            "section_path": ["第3章 承認"],
            "resolved": True,
        }
    ]
    assert evidence.reference_from == "doc-1:z"
    assert evidence.reference_label == "別紙1"


# ---- 設定 ----


def test_reference_expansion_settings_and_evaluation_override() -> None:
    from app.rag.evaluation import evaluation_settings
    from app.schemas.evaluation import EvaluationRagOverrides

    settings = Settings()
    assert settings.rag_reference_expansion_enabled is True
    assert settings.rag_reference_expansion_max_chunks == 4
    with pytest.raises(ValueError):
        Settings(rag_reference_expansion_max_chunks=0)

    overridden = evaluation_settings(
        settings,
        EvaluationRagOverrides(reference_expansion_enabled=False, reference_expansion_max_chunks=2),
    )
    assert overridden.rag_reference_expansion_enabled is False
    assert overridden.rag_reference_expansion_max_chunks == 2


def test_abbreviation_references_point_to_the_base_document(
    reference_words: Callable[..., None],
) -> None:
    """派生の文書の「法第N条」「令第N条」は、文書の名前から決めた本体の文書名で残す(#2169)。"""
    path = "テスト法施行規則 > 第二条(届出)"
    chunks = [_chunk(0, "法第七条第一項の規定による届出。令第三条に定める書類を添える。", path)]

    annotate_cross_references(chunks, document_title="12345_テスト法施行規則.json")

    targets = json.loads(str(chunks[0].metadata[REFERENCE_TARGETS_KEY]))
    assert [(t["key"], t["document_title"]) for t in targets] == [
        ("7", "テスト法"),
        ("3", "テスト法施行令"),
    ]

    # 本体の名前が決まらない(派生の語尾が無い)文書・語の一覧が空なら置き換えない。
    plain = [_chunk(0, "法第七条の規定による", "テスト法 > 第二条(届出)")]
    annotate_cross_references(plain, document_title="テスト法.json")
    assert json.loads(str(plain[0].metadata[REFERENCE_TARGETS_KEY]))[0]["document_title"] == "法"
    reference_words(abbreviation_targets=[])
    chunks = [_chunk(0, "法第七条の規定による", path)]
    annotate_cross_references(chunks, document_title="テスト法施行規則.json")
    assert json.loads(str(chunks[0].metadata[REFERENCE_TARGETS_KEY]))[0]["document_title"] == "法"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("法第七条第一項及び令第三条の規定による", [("7", "法"), ("3", "令")]),
        ("法第七条第一項又は令第三条の規定による", [("7", "法"), ("3", "令")]),
        ("法第七条第一項及び第二項並びに令第三条に規定する", [("7", "法"), ("3", "令")]),
        ("テスト法第五条及び第六条の規定による", [("5", "テスト法"), ("6", "テスト法")]),
        ("第七条及び第八条の規定による", [("7", None), ("8", None)]),
    ],
    ids=["oyobi", "matawa", "item-and-narabini", "same-document", "no-document"],
)
def test_enumerated_references_keep_each_document(
    text: str, expected: list[tuple[str, str | None]]
) -> None:
    """「及び」「又は」でつないだ列の途中で文書名が変わっても、各表記をその文書名で拾う(#2171)。"""
    assert [(s.key, s.document_title) for s in extract_references(text)] == expected


def test_cue_and_connector_words_come_from_settings(reference_words: Callable[..., None]) -> None:
    """参照の手がかりの語と列のつなぎの語は設定で変わる(#2171)。"""
    reference_words(cue_words=["に依る"])
    assert extract_references("第五条による") == []
    assert [s.key for s in extract_references("第五条に依る")] == ["5"]
    reference_words(cue_words=["による"], connector_words=["、"])
    # 「及び」をつなぎにしなければ、手がかりに近い表記だけを拾う。
    assert [s.key for s in extract_references("第五条及び第六条による")] == ["6"]
    assert [s.key for s in extract_references("第五条、第六条による")] == ["5", "6"]


def test_item_patterns_come_from_settings(reference_words: Callable[..., None]) -> None:
    """条に続く項・号の書き方は設定で変わる(#2173)。"""
    text = "法第七条第一款及び第二款並びに令第三条の規定による"
    assert [(s.label, s.document_title) for s in extract_references(text)] == [("第三条", "令")]
    reference_words(item_patterns=["第N項", "第N号", "第N款"])
    assert [(s.label, s.document_title) for s in extract_references(text)] == [
        ("第七条第一款", "法"),
        ("第三条", "令"),
    ]
    # 空なら条に続く項を表記に含めない(条と手がかりの間の項で列が切れる)。
    reference_words(item_patterns=[])
    assert extract_references("第七条第一項による") == []
    assert [s.label for s in extract_references("第七条による")] == ["第七条"]


def test_document_name_appendix_and_edition_words_come_from_settings(
    reference_words: Callable[..., None],
) -> None:
    """文書名だけの参照の述語・別紙の語と種類・タイトルの別名で外す語は設定で変わる(#2175)。"""
    assert [s.document_title for s in document_name_references("定期保守計画で確かめます。")] == [
        "定期保守計画"
    ]
    assert [s.key for s in extract_references("別添1を参照")] == ["別紙:1"]
    assert "定期保守計画" in document_name_aliases("定期保守計画 改訂版.pdf")
    assert "定期保守計画" in document_name_aliases("令和6年度 定期保守計画.pdf")
    assert "定期保守計画" in document_name_aliases("サンプル社 定期保守計画.pdf")

    reference_words(
        document_name_predicates=["で照合"],
        appendix_words=["別添=別表", "附属書=別紙"],
        edition_words=["第二稿"],
        era_names=[],
        company_suffixes=["株式会社"],
    )
    assert document_name_references("定期保守計画で確かめます。") == []
    assert [s.document_title for s in document_name_references("定期保守計画で照合します。")] == [
        "定期保守計画"
    ]
    assert [s.key for s in extract_references("別添1と附属書2を参照")] == ["別表:1", "別紙:2"]
    assert "定期保守計画" not in document_name_aliases("定期保守計画 改訂版.pdf")
    assert "定期保守計画" in document_name_aliases("定期保守計画 第二稿.pdf")
    assert "定期保守計画" not in document_name_aliases("令和6年度 定期保守計画.pdf")
    assert "定期保守計画" in document_name_aliases("サンプル株式会社 定期保守計画.pdf")
    # 見出しの別紙も同じ種類で鍵にする。
    assert ("appendix", "別紙:2") in heading_keys("附属書2 申請書様式")


def test_number_symbol_and_english_settings_change_extraction(
    reference_words: Callable[..., None],
) -> None:
    """数の書き方・記号・助詞・英語の書き方・正規化は設定で変わる(#2177)。"""
    # 年度・版・日付の型。
    assert "定期保守計画" in document_name_aliases("定期保守計画 2026年度.pdf")
    reference_words(edition_patterns=["第N稿"], edition_words=[])
    assert "定期保守計画" in document_name_aliases("定期保守計画 第3稿.pdf")
    assert "定期保守計画" not in document_name_aliases("定期保守計画 2026年度.pdf")

    # 題名の括弧・必ず文書名とみなす括弧。
    assert [s.kind for s in extract_references("【定期保守計画】を参照")] == []
    reference_words(quote_brackets=["【】"], document_title_brackets=["〈〉"])
    assert [s.kind for s in extract_references("【定期保守計画】を参照")] == ["title"]
    assert [s.label for s in document_name_references("〈定期保守計画〉に従う。")] == [
        "〈定期保守計画〉"
    ]

    # 文書名と表記の間の語。
    text = "経費精算マニュアルにおける第3章を参照"
    assert [s.document_title for s in extract_references(text)] == ["経費精算マニュアル"]
    reference_words(name_label_connectors=["の"])
    assert [s.document_title for s in extract_references(text)] == [None]

    # 英語の手がかりの語・章や節の語と種類。
    assert [s.key for s in extract_references("see Section 3.2")] == ["3.2"]
    reference_words(en_cue_words=["consult"], en_section_words=["part=chapter"])
    assert extract_references("see Section 3.2") == []
    assert [(s.kind, s.key) for s in extract_references("consult Part 2")] == [("chapter", "2")]

    # 正規化: しないなら全角の「３．２節」を節の番号として読まない。
    assert [s.key for s in extract_references("詳細は３．２節参照。")] == ["3.2"]
    reference_words(normalization="none")
    assert [s.key for s in extract_references("詳細は３．２節参照。")] == []


def test_number_units_kanji_name_chars_and_coordination_come_from_settings(
    reference_words: Callable[..., None],
) -> None:
    """番号の前置き・単位・漢数字・枝番にしない字・文書名の文字・並列の助詞は設定で変わる(#2179)。"""
    assert extract_references("第二款を参照") == []
    reference_words(article_units=["条", "款"])
    assert [(s.kind, s.key) for s in extract_references("第二款を参照")] == [("article", "2")]
    assert ("article", "3") in heading_keys("第三款 届出")

    # 漢数字の位取りを足すと千の位も読む。
    def articles(text: str) -> list[str]:
        return [s.key for s in extract_references(text) if s.kind == "article"]

    # 既定で千・万の位も読む(#2181 段 1b)。大字の「拾」は位取りに足すと読む。
    assert articles("第千二百条を参照") == ["1200"]
    assert articles("第拾条を参照") == []
    reference_words(kanji_multipliers=["十=10", "拾=10", "百=100", "千=1000"])
    assert articles("第拾条を参照") == ["10"]

    # 番号の前置き。
    reference_words(number_prefixes=["No."], chapter_units=["章"])
    assert [(s.kind, s.key) for s in extract_references("No.3章を参照")] == [("chapter", "3")]

    # 枝番にしない後ろの字。
    reference_words(number_prefixes=["第"])
    assert ("article", "3の2") in heading_keys("第3条の2点 届出")
    reference_words(branch_stop_chars=["点"])
    assert ("article", "3") in heading_keys("第3条の2点 届出")

    # 文書名の文字と並列の助詞。
    text = "担当部署はシステム台帳で、承認者は組織規程で確かめます。"
    assert [s.document_title for s in document_name_references(text)] == [
        "システム台帳",
        "組織規程",
    ]
    reference_words(coordination_particles=[])
    assert [s.document_title for s in document_name_references(text)] == ["組織規程"]
    text = "手順は保守マニュアルで確かめます。"
    assert [s.document_title for s in document_name_references(text)] == ["保守マニュアル"]
    reference_words(name_chars=["一-鿿"])
    assert document_name_references(text) == []
