"""言語資源(#2181): 組み込みのパック・重ね合わせ・形の検証・束ね。"""

from __future__ import annotations

import json
from importlib import resources

import pytest

from rag_parser_core.language import (
    FIELD_SPECS,
    REMOVED_FIELDS,
    LanguagePackError,
    bind_pack,
    build_pack,
    builtin_pack,
    current_pack,
    overrides_from,
    pack_from_payload,
    resolve,
    set_default_pack,
    stored_pack_hash,
)


def test_builtin_pack_has_every_field_and_nothing_else() -> None:
    raw = json.loads(
        resources.files("rag_parser_core.language").joinpath("packs", "ja.json").read_text("utf-8")
    )
    keys = {
        (section, key)
        for section, fields in raw.items()
        if isinstance(fields, dict)
        for key in fields
    }
    assert keys == {(spec.section, spec.key) for spec in FIELD_SPECS}
    assert builtin_pack().value("symbols", "normalization") == "NFKC"


def test_resolve_replaces_fields_and_keeps_others() -> None:
    base = builtin_pack()
    pack = resolve(base, {"numbers": {"article_units": ["条", " 款 ", "款", ""]}})
    assert pack.words("numbers", "article_units") == ("条", "款")
    assert pack.words("numbers", "chapter_units") == base.words("numbers", "chapter_units")
    assert pack.hash != base.hash
    assert overrides_from(pack, base) == {"numbers": {"article_units": ["条", "款"]}}
    assert resolve(base, {}).hash == base.hash


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("symbols", "bracket_pairs", ["("]),
        ("numbers", "kanji_digits", ["一=いち"]),
        ("english", "en_section_words", ["part=volume"]),
        ("symbols", "normalization", "NFKD"),
        ("symbols", "name_chars", ["あいう"]),
        ("references", "separate_part_patterns", ["改正"]),
        ("references", "appendix_words", ["別添"]),
        ("numbers", "no_such_field", ["x"]),
    ],
    ids=[
        "pair",
        "char-value",
        "enum-map",
        "choice",
        "char-range",
        "number-pattern",
        "word-map",
        "unknown",
    ],
)
def test_resolve_rejects_malformed_values(section: str, key: str, value: object) -> None:
    with pytest.raises(LanguagePackError) as caught:
        resolve(builtin_pack(), {section: {key: value}})
    assert f"{section}.{key}" in caught.value.errors


def test_current_pack_prefers_bound_then_default_then_builtin() -> None:
    custom = resolve(builtin_pack(), {"numbers": {"article_units": ["款"]}})
    other = resolve(builtin_pack(), {"numbers": {"article_units": ["目"]}})
    assert current_pack() is builtin_pack()
    set_default_pack(custom)
    try:
        assert current_pack() is custom
        with bind_pack(other):
            assert current_pack() is other
        assert current_pack() is custom
    finally:
        set_default_pack(None)
    assert current_pack() is builtin_pack()


def test_parse_and_format_numbers_follow_the_pack() -> None:
    """数の読み方と漢数字の書き方は言語資源の字と位取り(#2181 段 1b)。"""
    from rag_parser_core.language.text import format_kanji_number, parse_number

    cases = {
        "二十八": 28,
        "百四": 104,
        "三千二百万": 32_000_000,
        "一〇四": 104,
        "元": 1,
        "２８": 28,
    }
    assert {text: parse_number(text) for text in cases} == cases
    assert parse_number("拾") is None
    assert [format_kanji_number(n) for n in (28, 104, 1200, 35000, 0)] == [
        "二十八",
        "百四",
        "千二百",
        "三万五千",
        "〇",
    ]
    multipliers = ["十=10", "拾=10", "百=100"]
    custom = resolve(builtin_pack(), {"numbers": {"kanji_multipliers": multipliers}})
    with bind_pack(custom):
        assert parse_number("三拾") == 30
        assert format_kanji_number(30) == "三十"


def test_roman_numerals_follow_the_pack() -> None:
    """ローマ数字の字と値は言語資源(#2289)。標準の書き方だけを読む。"""
    from rag_parser_core.language.text import format_roman, parse_roman

    cases = {"i": 1, "iv": 4, "ix": 9, "xiv": 14, "XL": 40, "mcmxciv": 1994, "Ⅻ": None}
    assert {text: parse_roman(text) for text in cases} == cases
    for invalid in ("iiii", "vx", "ic", "", "page"):
        assert parse_roman(invalid) is None
    assert [format_roman(n) for n in (3, 4, 49, 1994, 0)] == ["iii", "iv", "xlix", "mcmxciv", ""]
    custom = resolve(builtin_pack(), {"numbers": {"roman_digits": ["i=1", "v=5"]}})
    with bind_pack(custom):
        assert parse_roman("iv") == 4
        assert parse_roman("x") is None
        assert format_roman(6) == "vi"


def test_numbered_headings_follow_the_pack() -> None:
    """番号の見出しの単位は言語資源から作る(#2181 段 1b)。"""
    from rag_parser_core.extraction import heading_level_from_prefix, numbered_heading

    assert numbered_heading().match("第2部 概要") is not None
    assert heading_level_from_prefix("第2部") == 1
    assert numbered_heading().match("第2巻 概要") is None
    custom = resolve(builtin_pack(), {"numbers": {"part_units": ["編", "部", "巻"]}})
    with bind_pack(custom):
        assert numbered_heading().match("第2巻 概要") is not None
        assert heading_level_from_prefix("第2巻") == 1


def test_parse_service_binds_language_pack_from_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析のサービスは、要求に付いた言語資源の差をその要求の間だけ使う(#2181 段 1b-3)。"""
    from fastapi.testclient import TestClient

    import rag_parser_core.service as service
    from rag_parser_core.result import ParserRegistryResult

    seen: list[str] = []

    def fake_run(*args: object, **kwargs: object) -> ParserRegistryResult:
        seen.append(current_pack().hash)
        return ParserRegistryResult(extraction=None, parser_backend="unstructured")

    monkeypatch.setattr(service, "run_external_adapter", fake_run)
    app = service.create_parse_app(backend="unstructured", import_name="rag_parser_core")
    client = TestClient(app)
    overrides = json.dumps({"numbers": {"article_units": ["条", "款"]}}, ensure_ascii=False)
    files = {"file": ("a.json", b"{}", "application/json")}
    assert client.post("/parse", files=files, data={"language_pack": overrides}).status_code == 200
    assert client.post("/parse", files=files).status_code == 200
    assert seen[0] != builtin_pack().hash
    assert seen[1] == builtin_pack().hash
    bad = json.dumps({"numbers": {"no_such": ["x"]}})
    assert client.post("/parse", files=files, data={"language_pack": bad}).status_code == 422


def test_markers_and_boilerplate_follow_the_pack() -> None:
    """箇条書き・ページの印・要素の種類の語・社名・定型文は言語資源から作る(#2181 段 2b)。"""
    from rag_parser_core.extraction import DocumentElement, bullet_line, page_marker
    from rag_parser_core.language.markers import boilerplate_patterns

    assert bullet_line().match("● 項目") is not None
    assert bullet_line().match("◎ 項目") is None
    assert page_marker().match("ページ 3")["page"] == "3"
    assert DocumentElement(kind="本文", text="x", order=0).kind == "text"
    assert boilerplate_patterns().company.search("サンプル株式会社") is not None
    assert boilerplate_patterns().company.search("color") is None
    assert boilerplate_patterns().boilerplate.search("禁無断転載") is not None
    custom = resolve(
        builtin_pack(),
        {
            "markers": {
                "bullet_chars": ["◎"],
                "page_words": ["ページ", "枚目"],
                "element_kind_words": ["本文=text", "段落=text"],
            },
            "boilerplate": {"company_words": ["商会"], "boilerplate_words": ["取扱注意"]},
        },
    )
    with bind_pack(custom):
        assert bullet_line().match("◎ 項目") is not None
        assert bullet_line().match("● 項目") is None
        assert page_marker().match("枚目 3")["page"] == "3"
        assert DocumentElement(kind="段落", text="x", order=0).kind == "text"
        assert boilerplate_patterns().company.search("サンプル商会") is not None
        assert boilerplate_patterns().boilerplate.search("取扱注意") is not None
        assert boilerplate_patterns().boilerplate.search("https://example.com") is not None


def test_ingest_hash_ignores_search_only_fields() -> None:
    """検索だけの項目を変えても取込の hash は変わらない（文書は古くならない。#2181 段 3）。"""
    base = builtin_pack()
    search_only = resolve(base, {"search_terms": {"stopwords": ["こと", "もの", "例"]}})
    ingest = resolve(base, {"numbers": {"article_units": ["条", "箇条"]}})
    assert search_only.hash != base.hash
    assert search_only.ingest_hash == base.ingest_hash
    assert ingest.ingest_hash != base.ingest_hash


def test_field_keys_are_unique_across_sections() -> None:
    """項目の名前は節をまたいで重ねない（画面の名前と説明の鍵は項目の名前だけで引く）。"""
    keys = [spec.key for spec in FIELD_SPECS]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize(
    ("key", "word"),
    [
        ("roman_digits", "v=0"),
        ("kanji_multipliers", "十=0"),
        ("kanji_digits", "一=²"),
    ],
)
def test_numeric_maps_reject_values_unusable_by_number_functions(key: str, word: str) -> None:
    with pytest.raises(LanguagePackError):
        resolve(builtin_pack(), {"numbers": {key: [word]}})


def test_zero_digit_and_positive_multipliers_still_format_numbers() -> None:
    from rag_parser_core.language.text import format_kanji_number, format_roman, parse_roman

    pack = resolve(
        builtin_pack(),
        {
            "numbers": {
                "kanji_digits": ["〇=0", "一=1", "二=2"],
                "kanji_multipliers": ["十=10", "百=100"],
                "roman_digits": ["i=1", "v=5", "x=10"],
            }
        },
    )
    assert format_kanji_number(0, pack) == "〇"
    assert format_kanji_number(12, pack) == "十二"
    assert format_roman(14, pack) == "xiv"
    assert parse_roman("xiv", pack) == 14


def test_positive_unit_one_does_not_recurse_without_progress() -> None:
    from rag_parser_core.language.text import format_kanji_number

    pack = resolve(builtin_pack(), {"numbers": {"kanji_multipliers": ["十=1"]}})
    assert format_kanji_number(2, pack) == "二十"


def test_arabic_numbers_unifies_kanji_numbers_and_thousands_separators():
    from rag_parser_core.language.text import arabic_numbers

    assert arabic_numbers("上限は一回五千円です。") == "上限は1回5000円です。"
    assert arabic_numbers("上限は1回あたり5,000円です。") == "上限は1回あたり5000円です。"
    # 漢数字を読まないときは桁区切りだけを除く。小数・4 桁ごとの区切りでない数は変えない。
    assert arabic_numbers("一万四千円と14,000円", kanji=False) == "一万四千円と14000円"
    assert arabic_numbers("1,5 と 12,3456") == "1,5 と 12,3456"
    # 数を取り出すときは、直した値を隣の数字と混ぜない。
    assert arabic_numbers("一泊一万四千円", spaced=True) == " 1 泊 14000 円"


def test_numeric_layout_gaps_and_mixed_notation():
    from rag_parser_core.language.text import arabic_numbers

    for text in ["四万八 千件", "四万八　千件", "四万八\n千件"]:
        assert arabic_numbers(text) == "48000件"
    for text in ["1万4千円", "1万4000円", "14,000円"]:
        assert arabic_numbers(text, kanji=False) == "14000円"
    assert arabic_numbers("一般と一部", kanji=False) == "一般と一部"
    for text in ["四万\t八千", "四万  八千", "四万 八万", "一 二"]:
        assert "48000" not in arabic_numbers(text)


_CRAG_COMPAT_KEYS = (
    "crag_refusal_summary",
    "crag_missing_aspect_template",
    "crag_unknown_aspect_template",
    "crag_quote_only_note",
    "crag_definition_aspect_template",
)


@pytest.mark.parametrize("mask", range(32))
def test_build_pack_fills_only_absent_new_crag_labels(mask: int) -> None:
    raw = builtin_pack().to_dict()
    raw["answer_labels"]["cause_gap"] = "合成の独自文言"
    raw["answer_labels"]["crag_quote_only_note"] = "合成の独自注記"
    for index, key in enumerate(_CRAG_COMPAT_KEYS):
        if mask & (1 << index):
            del raw["answer_labels"][key]
    saved = json.dumps(raw, ensure_ascii=False)
    pack = build_pack(raw)
    assert pack.text("answer_labels", "cause_gap") == "合成の独自文言"
    for key in _CRAG_COMPAT_KEYS:
        expected = raw["answer_labels"].get(key, builtin_pack().text("answer_labels", key))
        assert pack.text("answer_labels", key) == expected
    assert json.dumps(raw, ensure_ascii=False) == saved


@pytest.mark.parametrize("value", [None, ""])
def test_new_crag_label_explicit_invalid_value_is_not_defaulted(value: object) -> None:
    raw = builtin_pack().to_dict()
    raw["answer_labels"]["crag_refusal_summary"] = value
    with pytest.raises(LanguagePackError):
        build_pack(raw)


def test_other_missing_language_fields_still_fail() -> None:
    raw = builtin_pack().to_dict()
    del raw["answer_labels"]["cause_gap"]
    with pytest.raises(LanguagePackError, match="cause_gap"):
        build_pack(raw)


def test_legacy_pack_fills_acquisition_label_without_changing_saved_values() -> None:
    raw = builtin_pack().to_dict()
    del raw["answer_labels"]["guide_acquisition_incomplete_template"]
    raw["answer_labels"]["cause_gap"] = "合成の保存済み表示"
    original = json.dumps(raw, ensure_ascii=False)
    pack = build_pack(raw)
    assert pack.text(
        "answer_labels", "guide_acquisition_incomplete_template"
    ) == builtin_pack().text("answer_labels", "guide_acquisition_incomplete_template")
    assert pack.text("answer_labels", "cause_gap") == "合成の保存済み表示"
    assert pack.ingest_hash == builtin_pack().ingest_hash
    assert json.dumps(raw, ensure_ascii=False) == original


def test_acquisition_label_preserves_custom_text_and_only_changes_display_hash() -> None:
    raw = builtin_pack().to_dict()
    raw["answer_labels"]["guide_acquisition_incomplete_template"] = "合成の取得未完了: {steps}"
    pack = build_pack(raw)
    assert (
        pack.text("answer_labels", "guide_acquisition_incomplete_template")
        == "合成の取得未完了: {steps}"
    )
    assert pack.hash != builtin_pack().hash
    assert pack.ingest_hash == builtin_pack().ingest_hash


@pytest.mark.parametrize("value", [None, ""])
def test_acquisition_label_explicit_invalid_value_is_not_defaulted(value: object) -> None:
    raw = builtin_pack().to_dict()
    raw["answer_labels"]["guide_acquisition_incomplete_template"] = value
    with pytest.raises(LanguagePackError):
        build_pack(raw)


@pytest.mark.parametrize("snapshot", [[], None, "invalid", 3])
def test_invalid_snapshot_returns_field_error_instead_of_internal_exception(
    snapshot: object,
) -> None:
    with pytest.raises(LanguagePackError, match="snapshot") as caught:
        pack_from_payload(json.dumps({"snapshot": snapshot}))
    assert set(caught.value.errors) == {"snapshot"}


@pytest.mark.parametrize(
    "key", ["insufficient_evidence_summary", "empty_search_guidance", "abstention_guidance"]
)
def test_legacy_pack_fills_abstention_labels_without_changing_ingest(key: str) -> None:
    raw = builtin_pack().to_dict()
    del raw["answer_labels"][key]
    raw["answer_labels"]["cause_gap"] = "合成の保存済み表示"
    original = json.dumps(raw, ensure_ascii=False)
    pack = build_pack(raw)
    assert pack.text("answer_labels", key) == builtin_pack().text("answer_labels", key)
    assert pack.text("answer_labels", "cause_gap") == "合成の保存済み表示"
    assert json.dumps(raw, ensure_ascii=False) == original
    assert pack.ingest_hash == builtin_pack().ingest_hash


@pytest.mark.parametrize(
    "key", ["insufficient_evidence_summary", "empty_search_guidance", "abstention_guidance"]
)
@pytest.mark.parametrize("value", [None, ""])
def test_invalid_abstention_labels_are_not_defaulted(key: str, value: object) -> None:
    raw = builtin_pack().to_dict()
    raw["answer_labels"][key] = value
    with pytest.raises(LanguagePackError):
        build_pack(raw)


def test_removed_xml_tree_label_is_dropped_without_changing_hashes() -> None:
    """削除した XML ツリーの文書の種類（#42）は、保存済みの版に残っても読み捨てる。"""
    pack = builtin_pack()
    legacy = pack.to_dict()
    legacy["labels"] = {**legacy["labels"], "xml_tree_document_type": "XML ツリー"}
    loaded = build_pack(legacy)
    assert ("labels", "xml_tree_document_type") in REMOVED_FIELDS
    assert "xml_tree_document_type" not in loaded.sections["labels"]
    assert (loaded.hash, loaded.ingest_hash) == (pack.hash, pack.ingest_hash)
    assert stored_pack_hash(legacy, loaded) == pack.hash
    assert resolve(pack, {"labels": {"xml_tree_document_type": "別の名前"}}).hash == pack.hash
