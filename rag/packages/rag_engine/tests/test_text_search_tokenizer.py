"""text search tokenizer の挙動を保護するテスト。

分割は 1 つ（Sudachi の C / A を主に、文字種の区切りと漢字の部分語・送り仮名を除いた形を補う。#588）。
Sudachi が無い環境は ``_sudachi_available`` を False にして再現する。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rag_engine.retrieval.text_search_tokenizer import (
    MAX_ORACLE_TEXT_QUERY_CHARS,
    TEXT_SEARCH_TOKENIZER_HYBRID,
    TEXT_SEARCH_TOKENIZER_SCRIPT_ONLY,
    TextSearchTokenizerConfig,
    _sudachi_available,
    build_oracle_text_query,
    build_oracle_text_query_with_trace,
    escape_oracle_text_term,
    normalize_text_search_index_text,
    normalize_text_search_text,
    oracle_text_query_for_question,
    tokenize_text_search_query,
    tokenize_text_search_query_with_trace,
    tokenizer_fingerprint,
)

_AVAILABLE = "rag_engine.retrieval.text_search_tokenizer._sudachi_available"


def _weighted(query, *, domain_keywords=None, latin_stemmer="none"):
    result = tokenize_text_search_query_with_trace(
        query,
        domain_keywords=domain_keywords,
        config=TextSearchTokenizerConfig(latin_stemmer=latin_stemmer),
    )
    return dict(zip(result.tokens, result.weights, strict=False)), result


class TextSearchTokenizerTests(unittest.TestCase):
    def test_normalize_text_unifies_width_space_and_dash_variants(self):
        self.assertEqual(normalize_text_search_text("ＯＲＡ−０１５５５　再起動"), "ora-01555 再起動")

    def test_index_text_uses_same_characters_as_question_terms(self):
        # 索引の側も質問の側と同じ文字の形にする（#1336）。WORLD_LEXER が同一視しない互換文字を含む。
        for raw in ("手順①", "第Ⅴ章", "㈱サンプル", "５㌔", "２５℃", "１～３", "Ａ－１２３"):
            index_text = normalize_text_search_index_text(raw)
            self.assertEqual(index_text.casefold(), normalize_text_search_text(raw), raw)
        self.assertEqual(normalize_text_search_index_text("手順①"), "手順1")
        self.assertEqual(normalize_text_search_index_text("㈱サンプル"), "(株)サンプル")

    def test_index_text_keeps_line_breaks_and_case(self):
        # 語の区切りと大文字小文字の無視は Oracle Text の lexer が行うため、索引の文字列では変えない。
        self.assertEqual(
            normalize_text_search_index_text("規程.pdf > 申請\nＶＰＮ\t接続\x00"),
            "規程.pdf > 申請\nVPN\t接続 ",
        )
        self.assertEqual(normalize_text_search_index_text(None), "")

    def test_oracle_text_query_escapes_reserved_characters_with_braces(self):
        self.assertEqual(escape_oracle_text_term("ORA-01555"), "{ora-01555}")
        self.assertEqual(escape_oracle_text_term("50%"), "{50%}")
        self.assertEqual(build_oracle_text_query(["ora-01555", "契約区分"]), "{ora-01555} ACCUM {契約区分}")

    def test_oracle_text_query_joins_terms_with_accum_and_weights(self):
        query = build_oracle_text_query(["契約区分", "契約", "取消", "年"], weights=[2.0, 1.0, 0.5, 0.5])

        self.assertEqual(query, "{契約区分}*2 ACCUM {契約} ACCUM {取消}*0.5 ACCUM {年}*0.5")

    def test_oracle_text_query_clamps_weights_to_oracle_text_range(self):
        self.assertEqual(build_oracle_text_query(["a1", "b2"], weights=[50.0, 0.01]), "{a1}*10 ACCUM {b2}*0.1")

    def test_oracle_text_query_respects_length_limit_including_weights(self):
        terms = [f"語{index:04d}" for index in range(2000)]
        query = build_oracle_text_query(terms, weights=[0.5] * len(terms))

        self.assertLessEqual(len(query), MAX_ORACLE_TEXT_QUERY_CHARS)
        self.assertTrue(query.endswith("*0.5"))
        self.assertLessEqual(len(build_oracle_text_query(terms, max_chars=40)), 40)

    def test_character_limit_skips_long_term_and_retains_following_short_terms(self):
        plan = build_oracle_text_query_with_trace(["x" * 80, "id-12", "5"], max_chars=24)
        self.assertEqual(plan.tokens, ("id-12", "5"))
        self.assertEqual(plan.omitted_tokens, ("x" * 80,))
        self.assertEqual(plan.query, "{id-12} ACCUM {5}")
        self.assertLessEqual(len(plan.query), 24)

    def test_fingerprint_is_stable_for_same_config(self):
        config = TextSearchTokenizerConfig(latin_stemmer="none")

        self.assertEqual(tokenizer_fingerprint(config), tokenizer_fingerprint(config))

    def test_fingerprint_changes_when_sudachi_becomes_unavailable(self):
        config = TextSearchTokenizerConfig(latin_stemmer="none")
        with patch(_AVAILABLE, return_value=True):
            with_sudachi = tokenizer_fingerprint(config)
        with patch(_AVAILABLE, return_value=False):
            without_sudachi = tokenizer_fingerprint(config)

        self.assertNotEqual(with_sudachi, without_sudachi)


class ScriptOnlyFallbackTests(unittest.TestCase):
    """Sudachi（辞書）が使えない環境では、自動で文字種の区切りだけにする。"""

    def setUp(self):
        patcher = patch(_AVAILABLE, return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_script_runs_become_primary_terms_and_compound_parts_are_light(self):
        weights, result = _weighted(
            "AND ORA-01555が発生。契約区分とindexesを確認してください",
            domain_keywords=["契約区分"],
            latin_stemmer="light",
        )

        self.assertEqual(result.tokenizer, TEXT_SEARCH_TOKENIZER_SCRIPT_ONLY)
        self.assertEqual(result.tokens[:2], ("契約区分", "契約"))
        self.assertEqual(weights["契約区分"], 2.0)  # ドメインキーワードは 1 語として優先する
        self.assertEqual(weights["ora-01555"], 1.0)
        self.assertEqual(weights["ora"], 0.5)  # 型番の部分は補う語
        self.assertEqual(weights["発生"], 1.0)
        self.assertIn("index", weights)  # 英語の語幹
        self.assertNotIn("and", weights)
        self.assertNotIn("確認してください", weights)

    def test_hiragana_is_a_separator_without_sudachi(self):
        weights, _ = _weighted("注文を取り消したい")

        self.assertEqual(weights, {"注文": 1.0})

    def test_unknown_proper_noun_keeps_the_whole_kanji_run(self):
        weights, _ = _weighted("楽々精算の申請期限")

        self.assertEqual(weights["楽々精算"], 1.0)
        self.assertEqual(weights["楽々"], 0.5)
        self.assertEqual(weights["精算"], 0.5)

    def test_truncation_ranks_specific_terms_before_generic_early_terms(self):
        terms = tokenize_text_search_query(
            "表示 確認 画面 情報 倉庫マスタ登録 伝票様式設定 拠点倉庫名",
            max_tokens=4,
        )

        self.assertEqual(terms, ["伝票様式設定", "拠点倉庫名", "マスタ", "倉庫"])
        self.assertNotIn("確認", terms)

    def test_tokenization_trace_exposes_truncated_candidates(self):
        result = tokenize_text_search_query_with_trace(
            "表示 確認 画面 情報 倉庫マスタ登録 伝票様式設定 拠点倉庫名",
            max_tokens=3,
        )

        self.assertEqual(result.tokens, ("伝票様式設定", "拠点倉庫名", "マスタ"))
        self.assertEqual(len(result.weights), 3)
        self.assertTrue(result.truncated)
        self.assertEqual(result.candidate_count, 13)
        self.assertEqual(result.truncated_count, 10)
        self.assertIn("表示", result.truncated_tokens)

    def test_explicit_codes_versions_and_numeric_conditions_survive_keyword_overflow(self):
        keywords = [f"候補語{letter}" for letter in "abcdefghijkl"]
        result = tokenize_text_search_query_with_trace(
            " ".join((*keywords, "ID-789 v2.3 2026/10/27 50以上")),
            domain_keywords=keywords,
            max_tokens=4,
        )
        self.assertEqual(set(result.tokens), {"id-789", "v2.3", "2026/10/27", "50"})
        self.assertEqual(set(result.protected_tokens), set(result.tokens))
        self.assertEqual(result.omitted_protected_tokens, ())
        self.assertNotIn("789", result.protected_tokens)
        self.assertNotIn("2", result.protected_tokens)
        self.assertEqual(result.oracle_text_query, build_oracle_text_query(result.tokens, weights=result.weights))

    def test_overflow_reports_unsearched_explicit_terms_without_claiming_coverage(self):
        result = tokenize_text_search_query_with_trace("ID-123 ID-456 ID-789", max_tokens=2)
        self.assertEqual(len(result.tokens), 2)
        self.assertEqual(len(result.omitted_protected_tokens), 1)
        self.assertTrue(result.truncated)
        self.assertEqual(set(result.protected_tokens), set(result.tokens) | set(result.omitted_protected_tokens))

    def test_trace_matches_actual_query_after_character_limit(self):
        result = tokenize_text_search_query_with_trace("ID-123 v2.3 50", max_query_chars=14)
        self.assertLessEqual(len(result.oracle_text_query), 14)
        self.assertTrue(result.character_limit_tokens)
        self.assertTrue(result.omitted_protected_tokens)
        self.assertTrue(all(escape_oracle_text_term(term) in result.oracle_text_query for term in result.tokens))
        self.assertEqual(result.oracle_text_query, oracle_text_query_for_question("ID-123 v2.3 50", max_chars=14))

    def test_sudachi_failure_during_tokenization_falls_back_to_script_runs(self):
        with (
            patch(_AVAILABLE, return_value=True),
            patch("rag_engine.retrieval.text_search_tokenizer._sudachi_tokens", side_effect=RuntimeError("broken")),
            self.assertLogs("rag_engine.retrieval.text_search_tokenizer", level="WARNING"),
        ):
            terms = tokenize_text_search_query("契約区分 ORA-01555", config=TextSearchTokenizerConfig(latin_stemmer="none"))

        self.assertEqual(terms, ["ora-01555", "契約区分", "契約", "区分", "ora", "01555"])


class FakeSudachiTests(unittest.TestCase):
    """辞書に依存しない形で、Sudachi の C / A の扱いを確かめる。"""

    def _tokenize(self, text, mapping, stopwords=frozenset()):
        tokenizer = _FakeSudachiTokenizer(mapping)
        with (
            patch(_AVAILABLE, return_value=True),
            patch("rag_engine.retrieval.text_search_tokenizer._get_sudachi_tokenizer", return_value=tokenizer),
            patch("rag_engine.retrieval.text_search_tokenizer._sudachi_split_mode", side_effect=lambda mode: mode),
            patch("rag_engine.retrieval.text_search_tokenizer._sudachi_stopwords", return_value=stopwords),
        ):
            return _weighted(text)

    def test_sudachi_path_filters_pos_and_adds_multi_granularity_tokens(self):
        weights, result = self._tokenize(
            "東京都知事選挙についてのこと",
            {
                "東京都知事選挙": [
                    _morpheme(
                        "東京都知事選挙",
                        "東京都知事選挙",
                        ("名詞", "普通名詞"),
                        subs=[
                            _morpheme("東京", "東京", ("名詞", "普通名詞")),
                            _morpheme("知事", "知事", ("名詞", "普通名詞")),
                            _morpheme("選挙", "選挙", ("名詞", "普通名詞")),
                        ],
                    )
                ],
                "について": [
                    _morpheme("に", "に", ("助詞", "*")),
                    _morpheme("つい", "つく", ("動詞", "非自立可能")),
                    _morpheme("て", "て", ("助詞", "*")),
                ],
                "こと": [_morpheme("こと", "事", ("名詞", "普通名詞"))],
            },
            stopwords=frozenset({"こと", "事"}),
        )

        self.assertEqual(result.tokenizer, TEXT_SEARCH_TOKENIZER_HYBRID)
        # C と A は主の語（重み 1）。文字種の区切りの語（漢字の連続・先頭 2 字・末尾 2 字）は
        # Sudachi の語と重なるので、主の重みのまま増えない。
        self.assertEqual(weights, {"東京都知事選挙": 1.0, "東京": 1.0, "知事": 1.0, "選挙": 1.0})

    def test_sudachi_path_keeps_surface_form_for_oracle_text_raw_index(self):
        weights, _ = self._tokenize(
            "サーバの再起動",
            {
                "サーバ": [_morpheme("サーバ", "サーバー", ("名詞", "普通名詞"))],
                "再起動": [_morpheme("再起動", "再起動", ("名詞", "普通名詞"))],
            },
        )

        self.assertEqual(list(weights)[:3], ["サーバー", "サーバ", "再起動"])
        self.assertEqual(weights["再起"], 0.5)

    def test_okurigana_free_form_is_added_as_light_term(self):
        weights, _ = self._tokenize(
            "取り消し",
            {"取り消し": [_morpheme("取り消し", "取り消し", ("名詞", "普通名詞"))]},
        )

        self.assertEqual(weights, {"取り消し": 1.0, "取消": 0.5})


class RealSudachiTests(unittest.TestCase):
    """Sudachi の辞書（CI は sudachidict_full）で、日本語の書き分けに届くかを確かめる。"""

    def setUp(self):
        if not _sudachi_available(TextSearchTokenizerConfig()):
            self.skipTest("Sudachi の辞書が入っていない")

    def test_okurigana_variants_reach_both_written_forms(self):
        weights, result = _weighted("見積りの有効期限はいつまでですか")

        self.assertEqual(result.tokenizer, TEXT_SEARCH_TOKENIZER_HYBRID)
        self.assertEqual(weights["見積もり"], 1.0)  # 正規化形
        self.assertEqual(weights["見積り"], 1.0)  # 表層形
        self.assertEqual(weights["見積"], 0.5)  # 送り仮名を除いた形
        self.assertEqual(weights["有効期限"], 1.0)

    def test_inflected_verb_reaches_noun_without_okurigana(self):
        weights, _ = _weighted("注文を取り消したい")

        self.assertEqual(weights["注文"], 1.0)
        self.assertEqual(weights["取り消す"], 1.0)
        self.assertEqual(weights["取消"], 0.5)
        self.assertNotIn("たい", weights)

    def test_unknown_proper_noun_is_supplemented_by_kanji_run(self):
        weights, _ = _weighted("楽々精算にログインできない")

        self.assertEqual(weights["楽々"], 1.0)
        self.assertEqual(weights["精算"], 1.0)
        self.assertEqual(weights["楽々精算"], 0.5)
        self.assertEqual(weights["ログイン"], 1.0)

    def test_model_numbers_and_alphanumerics_are_kept(self):
        weights, _ = _weighted("ABC-1234とX200のVPN設定")

        for term in ("abc-1234", "abc", "1234", "x200", "vpn", "設定"):
            self.assertIn(term, weights)
        self.assertEqual(weights["x"], 0.5)  # 1 字の語は軽くする

    def test_domain_keyword_is_one_heavy_term(self):
        query = oracle_text_query_for_question("出荷基準年月を修正したい", domain_keywords=["出荷基準年月"])

        self.assertTrue(query.startswith("{出荷基準年月}*2 ACCUM {出荷} ACCUM {基準}"))
        self.assertIn("{年月}*0.5", query)
        self.assertIn("{修正}", query)
        self.assertNotIn(" OR ", query)


def _morpheme(surface, normalized, pos, subs=None):
    return SimpleNamespace(
        surface=lambda: surface,
        normalized_form=lambda: normalized,
        part_of_speech=lambda: pos,
        split=lambda mode: list(subs or []),
    )


class _FakeSudachiTokenizer:
    def __init__(self, mapping):
        self._mapping = mapping

    def tokenize(self, text, mode):
        result = []
        remaining = text
        while remaining:
            matched = False
            for key, morphemes in self._mapping.items():
                if remaining.startswith(key):
                    result.extend(morphemes)
                    remaining = remaining[len(key) :]
                    matched = True
                    break
            if matched:
                continue
            char = remaining[0]
            remaining = remaining[1:]
            if char.isspace() or char in "の":
                continue
            result.append(_morpheme(char, char, ("名詞", "普通名詞")))
        return result


if __name__ == "__main__":
    unittest.main()
