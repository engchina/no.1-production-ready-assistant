"""Oracle Text 検索に渡す日本語・英数字 token を生成する（#588）。

分割の方式は 1 つだけで、利用者が選ぶ設定は持たない。

- 主の語: Sudachi の長い単位（C）と短い単位（A）。正規化形と表層形の両方を出す。
- 補う語: 文字種の区切り（漢字・カタカナ・英数字の連続）、漢字の複合語の部分語（先頭 2 字・末尾 2 字）、
  Sudachi の語から送り仮名を除いた形（「取り消し」→「取消」）。辞書に無い語・型番・送り仮名の
  書き分けに届かせる。
- ドメインキーワード・質問で明示された列名は 1 語として優先する。
- Sudachi（辞書）が使えない環境では、自動で文字種の区切りだけにする（文字種の連続を主の語にする）。

Oracle Text の query は語を ``ACCUM`` で結び、語ごとに重み（キーワード 2・主 1・補う語と 1 字の語 0.5）を
付ける。多くの語に当たる文書ほど上になり、補った語だけの一致は軽くなる。索引の側（WORLD_LEXER）は変えない。
"""

from __future__ import annotations

import functools
import hashlib
import importlib.metadata as importlib_metadata
import importlib.util
import json
import logging
import re
import threading
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from rag_parser_core.language import current_pack
from rag_parser_core.language.text import range_class, text_patterns

from rag_engine.dependencies import report_degraded
from rag_engine.knowledge.domain_keywords import extract_domain_keywords
from rag_engine.retrieval.character_forms import SYMBOL_FOLD, fold_text
from rag_engine.retrieval.definition_evidence import definition_labels

logger = logging.getLogger(__name__)

DEFAULT_TEXT_SEARCH_TOKENIZER_SUDACHI_DICT = "full"
DEFAULT_TEXT_SEARCH_TOKENIZER_LATIN_STEMMER = "auto"
MAX_TEXT_SEARCH_TOKENS = 24
MAX_ORACLE_TEXT_QUERY_CHARS = 3800
MAX_TEXT_SEARCH_TOKEN_TRACE = 64
# 実行記録に出す分割の名前。Sudachi が使えたかどうかで変わる。
TEXT_SEARCH_TOKENIZER_HYBRID = "sudachi+script"
TEXT_SEARCH_TOKENIZER_SCRIPT_ONLY = "script"

# 語の重み（Oracle Text の ``term*weight``。0.1〜10）。
KEYWORD_TERM_WEIGHT = 2.0
PRIMARY_TERM_WEIGHT = 1.0
SUPPLEMENT_TERM_WEIGHT = 0.5

# 語の出どころ。値は上限で切り詰めるときの優先度（大きいほど残す）。
_TIER_EXPLICIT = 4
_TIER_KEYWORD = 3
_TIER_KEYWORD_PART = 2
_TIER_PRIMARY = 1
_TIER_SUPPLEMENT = 0

_VALID_LATIN_STEMMERS = {"auto", "snowball", "light", "none"}
_SUDACHI_MAX_BYTES = 20000
_LATIN_ALPHA = re.compile(r"^[a-z]+$")
# 文字種の区切り（漢字・カタカナの範囲は言語資源。ひらがなは区切りとして捨てる）。
_LATIN_RUN = re.compile(r"[a-z0-9][a-z0-9_]*(?:[-.][a-z0-9_]+)*")
_EXTRA_PATTERNS = (
    re.compile(r"[a-z][a-z0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*"),
    re.compile(r"[a-z]+\d+[a-z0-9]*"),
    re.compile(r"\d+[a-z][a-z0-9]*"),
    re.compile(r"v?\d+(?:\.\d+)+"),
)
# 文字の同一視（質問の側と索引の側、回答のモデルに渡す根拠で共通。#1336・#1350）。波線・ダッシュは
# ``character_forms.SYMBOL_FOLD``、質問の側だけが空白の連続を 1 つにまとめる。
_WHITESPACE_RUN = re.compile(r"[　\s]+")


ORACLE_TEXT_RESERVED_TERMS = {
    "about",
    "accum",
    "and",
    "bt",
    "btg",
    "bti",
    "btp",
    "equiv",
    "fuzzy",
    "haspath",
    "inpath",
    "mdata",
    "minus",
    "near",
    "not",
    "nt",
    "ntg",
    "nti",
    "ntp",
    "or",
    "pattern",
    "pt",
    "rt",
    "sqe",
    "syn",
    "tr",
    "trsyn",
    "tt",
    "within",
}


@dataclass(frozen=True)
class _Vocabulary:
    """全文検索の質問の語の書き方（言語資源の search_terms。#2181 段 3）。"""

    key: str
    stopwords: tuple[str, ...]
    generic: frozenset[str]
    allow: frozenset[str]
    deny: frozenset[str]
    particle_edges: frozenset[str]
    polite_suffixes: tuple[str, ...]
    keep_pos: frozenset[str]
    drop_pos_details: frozenset[str]
    split_bounds: frozenset[str]
    en_irregular: dict[str, str]
    kana_only: re.Pattern[str]
    katakana: re.Pattern[str]
    kanji_or_katakana: re.Pattern[str]
    hiragana: re.Pattern[str]
    kanji_run: re.Pattern[str]
    katakana_run: re.Pattern[str]
    sentence_split: re.Pattern[str]


_vocabularies: dict[str, _Vocabulary] = {}


def _vocabulary() -> _Vocabulary:
    """今の言語資源の全文検索の語（言語資源の hash ごとに 1 回だけ作る）。"""
    pack = current_pack()
    cached = _vocabularies.get(pack.hash)
    if cached is not None:
        return cached
    section = pack.sections["search_terms"]

    def words(key: str) -> tuple[str, ...]:
        return pack.words("search_terms", key)

    kanji = range_class(words("kanji_chars"))
    katakana = range_class(words("katakana_chars"))
    hiragana = range_class(words("hiragana_chars"))
    vocabulary = _Vocabulary(
        key=hashlib.sha256(json.dumps({k: list(v) for k, v in section.items()}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[
            :8
        ],
        stopwords=words("stopwords"),
        generic=frozenset(words("generic_terms")),
        allow=frozenset(words("single_char_terms")),
        deny=frozenset(normalize_text_search_text(word) for word in words("excluded_terms")),
        particle_edges=frozenset(words("particle_edge_chars")),
        polite_suffixes=words("polite_suffixes"),
        keep_pos=frozenset(words("kept_pos")),
        drop_pos_details=frozenset(words("dropped_pos_details")),
        split_bounds=frozenset(words("split_boundary_chars")),
        en_irregular=pack.mapping("search_terms", "en_irregular_forms"),
        kana_only=re.compile(f"^{hiragana}+$"),
        katakana=re.compile(f"^{katakana}+$"),
        kanji_or_katakana=re.compile(f"{katakana}|{kanji}"),
        hiragana=re.compile(hiragana),
        kanji_run=re.compile(f"{kanji}+"),
        katakana_run=re.compile(f"{katakana}+"),
        sentence_split=re.compile(f"(?<={text_patterns().sentence_end})|(?<=\n)"),
    )
    _vocabularies[pack.hash] = vocabulary
    return vocabulary


@dataclass(frozen=True)
class TextSearchTokenizerConfig:
    """Sudachi の辞書と英語の語幹の設定を保持します（分割の方式は 1 つで選べない。#588）。"""

    sudachi_dict_type: str = DEFAULT_TEXT_SEARCH_TOKENIZER_SUDACHI_DICT
    sudachi_config_path: str = ""
    latin_stemmer: str = DEFAULT_TEXT_SEARCH_TOKENIZER_LATIN_STEMMER


@dataclass(frozen=True)
class TextSearchTokenizationResult:
    """Oracle Text 検索用 token（と重み）と切り詰め有無を保持します。"""

    tokens: tuple[str, ...]
    weights: tuple[float, ...] = ()
    candidate_tokens: tuple[str, ...] = ()
    truncated_tokens: tuple[str, ...] = ()
    candidate_count: int = 0
    truncated_count: int = 0
    max_tokens: int = MAX_TEXT_SEARCH_TOKENS
    matched_domain_keywords: tuple[str, ...] = ()
    selected_domain_keywords: tuple[str, ...] = ()
    truncated_domain_keywords: tuple[str, ...] = ()
    tokenizer: str = TEXT_SEARCH_TOKENIZER_HYBRID
    protected_tokens: tuple[str, ...] = ()
    omitted_protected_tokens: tuple[str, ...] = ()
    character_limit_tokens: tuple[str, ...] = ()
    oracle_text_query: str = ""

    @property
    def truncated(self) -> bool:
        """token 上限により検索 token が切り詰められたかを返します。"""
        return self.truncated_count > 0


@dataclass(frozen=True)
class _TokenCandidate:
    term: str
    first_order: int
    priority: int = 0
    weight: float = PRIMARY_TERM_WEIGHT


_dict_lock = threading.Lock()
_dicts: dict[tuple[str, str], Any] = {}
_local = threading.local()


def normalize_text_search_text(text: Any) -> str:
    """Oracle Text token 化前の文字列を NFKC と空白規則で正規化します。"""
    normalized = unicodedata.normalize("NFKC", str(text or "")).translate(SYMBOL_FOLD)
    normalized = _WHITESPACE_RUN.sub(" ", normalized)
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", normalized)
    return normalized.casefold().strip()


def normalize_text_search_index_text(text: Any) -> str:
    """Oracle Text に索引する文字列を、質問の語と同じ文字の形にそろえます（#1336）。

    NFKC と波線・ダッシュの同一視は ``normalize_text_search_text`` と同じにし、索引の側だけが互換文字
    （``①`` ``Ⅴ`` ``㈱`` ``㌔`` ``℃`` など。WORLD_LEXER は同一視しない）を残して当たらないことを防ぎます。
    改行と大文字小文字は残します（Oracle Text の語の区切りと大文字小文字の無視は lexer が行う）。
    回答のモデルに渡す根拠も同じ規則でそろえる（``character_forms.fold_text``。#1350）。
    """
    return fold_text(text)


def tokenize_domain_keyword_candidates(
    text: str,
    *,
    config: TextSearchTokenizerConfig | None = None,
    max_tokens: int = MAX_TEXT_SEARCH_TOKENS,
) -> list[str]:
    """登録候補には本文の名詞の表層形とコードだけを使う。検索用の派生形は作らない。"""
    cfg = normalize_text_search_tokenizer_config(config)
    tokenizer = _get_sudachi_tokenizer(cfg.sudachi_dict_type, cfg.sudachi_config_path)
    # 片仮名の途中の改行は文書の折り返しとして読む。語の間の空白はつなげない。
    katakana = range_class(current_pack().words("search_terms", "katakana_chars"))
    body = re.sub(rf"(?<={katakana})[^\S\n]*\n[^\S\n]*(?={katakana})", "", text)
    body = normalize_text_search_text(body)
    vocabulary = _vocabulary()
    tokens: list[str] = []
    for pattern in _EXTRA_PATTERNS:
        for match in pattern.finditer(body):
            tokens.append(match.group())
    for match in _LATIN_RUN.finditer(body):
        code = match.group()
        if any(char.isalpha() for char in code) and any(char.isdecimal() for char in code):
            tokens.append(code)
    for chunk in _split_for_sudachi(body):
        compound: list[str] = []
        previous_end = -1

        def flush(parts: list[str] = compound) -> None:
            if parts:
                tokens.append("".join(parts))
                parts.clear()

        for morpheme in tokenizer.tokenize(chunk, _sudachi_split_mode("C")):
            pos = morpheme.part_of_speech()
            surface = morpheme.surface()
            if pos and pos[0] == "名詞" and not any(part in vocabulary.drop_pos_details for part in pos[1:]):
                if morpheme.begin() != previous_end:
                    flush()
                compound.append(surface)
                tokens.append(surface)
                previous_end = morpheme.end()
            else:
                flush()
                previous_end = -1
        flush()
    return list(
        dict.fromkeys(
            token for token in tokens if token and token in body and token not in vocabulary.stopwords and token not in vocabulary.deny
        )
    )[:max_tokens]


def tokenize_text_search_query(
    query: str,
    *,
    domain_keywords: Sequence[str] | None = None,
    config: TextSearchTokenizerConfig | None = None,
    max_tokens: int = MAX_TEXT_SEARCH_TOKENS,
) -> list[str]:
    """質問文を Oracle Text query に使う token 列へ変換します。"""
    return list(
        tokenize_text_search_query_with_trace(
            query,
            domain_keywords=domain_keywords,
            config=config,
            max_tokens=max_tokens,
        ).tokens
    )


def tokenize_text_search_query_with_trace(
    query: str,
    *,
    domain_keywords: Sequence[str] | None = None,
    config: TextSearchTokenizerConfig | None = None,
    max_tokens: int = MAX_TEXT_SEARCH_TOKENS,
    max_query_chars: int = MAX_ORACLE_TEXT_QUERY_CHARS,
) -> TextSearchTokenizationResult:
    """token 化結果に加えて、語の重みと採用・除外理由の trace を返します。"""
    cfg = normalize_text_search_tokenizer_config(config)
    limit = max(1, int(max_tokens or 1))
    candidates: dict[str, _TokenCandidate] = {}
    normalized_domain_keywords = tuple(domain_keywords or ())
    order = 0
    use_sudachi = _sudachi_available(cfg)

    def add(term: Any, tier: int) -> None:
        nonlocal order
        normalized = _normalize_term(term)
        if not normalized or normalized in ORACLE_TEXT_RESERVED_TERMS:
            return
        weight = _tier_weight(tier)
        if len(normalized) == 1 and tier < _TIER_KEYWORD and normalized not in _vocabulary().allow:
            # 1 字の語（「年」「月」）は多くの文書に当たるので軽くする。
            weight = min(weight, SUPPLEMENT_TERM_WEIGHT)
        existing = candidates.get(normalized)
        if existing is None:
            candidates[normalized] = _TokenCandidate(
                term=normalized,
                first_order=order,
                priority=tier,
                weight=weight,
            )
            order += 1
            return
        if tier > existing.priority or weight > existing.weight:
            candidates[normalized] = _TokenCandidate(
                term=normalized,
                first_order=existing.first_order,
                priority=max(tier, existing.priority),
                weight=max(weight, existing.weight),
            )

    matched_keywords = extract_domain_keywords(
        query,
        normalized_domain_keywords,
        limit=max(1, len(normalized_domain_keywords)),
    )
    for keyword in matched_keywords:
        add(keyword, _TIER_KEYWORD)
        for token, tier in _content_terms(keyword, cfg, use_sudachi):
            add(token, _TIER_KEYWORD_PART if tier == _TIER_PRIMARY else tier)

    # 品詞判定が接頭辞とする一文字でも、質問で明示された列名は失わない。
    labels = definition_labels(query)
    for label in labels:
        add(label, _TIER_KEYWORD)
    for token, tier in _content_terms(query, cfg, use_sudachi):
        add(token, tier)
    # 優先配分だけを変え、既存の重みと語順は維持する。意味の充足は判定しない。
    protected = tuple(dict.fromkeys((*_protected_query_terms(query), *(_normalize_term(label) for label in labels))))
    for token in protected:
        if token not in candidates:
            add(token, _TIER_PRIMARY)
        if candidate := candidates.get(token):
            candidates[token] = _TokenCandidate(token, candidate.first_order, _TIER_EXPLICIT, candidate.weight)
    ordered_candidates = list(candidates.values())
    if len(ordered_candidates) <= limit:
        selected = sorted(ordered_candidates, key=lambda candidate: candidate.first_order)
    else:
        selected = sorted(ordered_candidates, key=_ranked_token_sort_key)[:limit]
    character_plan = build_oracle_text_query_with_trace(
        tuple(candidate.term for candidate in selected),
        weights=tuple(candidate.weight for candidate in selected),
        max_chars=max_query_chars,
    )
    used = set(character_plan.tokens)
    selected = [candidate for candidate in selected if candidate.term in used]
    selected_keys = {candidate.term for candidate in selected}
    ranked_candidates = sorted(ordered_candidates, key=_ranked_token_sort_key)
    truncated_tokens = tuple(candidate.term for candidate in ranked_candidates if candidate.term not in selected_keys)
    return TextSearchTokenizationResult(
        tokens=tuple(candidate.term for candidate in selected),
        weights=tuple(candidate.weight for candidate in selected),
        matched_domain_keywords=tuple(matched_keywords),
        selected_domain_keywords=tuple(k for k in matched_keywords if _normalize_term(k) in selected_keys),
        truncated_domain_keywords=tuple(k for k in matched_keywords if _normalize_term(k) not in selected_keys),
        candidate_tokens=tuple(candidate.term for candidate in ranked_candidates[:MAX_TEXT_SEARCH_TOKEN_TRACE]),
        truncated_tokens=truncated_tokens[:MAX_TEXT_SEARCH_TOKEN_TRACE],
        candidate_count=len(ordered_candidates),
        truncated_count=max(0, len(ordered_candidates) - len(selected)),
        max_tokens=limit,
        tokenizer=TEXT_SEARCH_TOKENIZER_HYBRID if use_sudachi else TEXT_SEARCH_TOKENIZER_SCRIPT_ONLY,
        protected_tokens=tuple(protected),
        omitted_protected_tokens=tuple(token for token in protected if token not in selected_keys),
        character_limit_tokens=character_plan.omitted_tokens,
        oracle_text_query=character_plan.query,
    )


def _protected_query_terms(query: str) -> tuple[str, ...]:
    """識別子の内部の数字を別の条件として重複配分しない。"""
    normalized = normalize_text_search_text(query)
    matches = [
        match
        for pattern in (*_EXTRA_PATTERNS, re.compile(r"\d+(?:[-/.]\d+)+"), re.compile(r"\d+(?:[.,]\d+)*"))
        for match in pattern.finditer(normalized)
    ]
    return tuple(
        dict.fromkeys(
            match.group(0)
            for match in sorted(matches, key=lambda item: (item.start(), -item.end()))
            if not any(other.start() <= match.start() and match.end() <= other.end() and other.span() != match.span() for other in matches)
        )
    )


def _tier_weight(tier: int) -> float:
    if tier >= _TIER_KEYWORD:
        return KEYWORD_TERM_WEIGHT
    if tier >= _TIER_PRIMARY:
        return PRIMARY_TERM_WEIGHT
    return SUPPLEMENT_TERM_WEIGHT


def _ranked_token_sort_key(candidate: _TokenCandidate) -> tuple[float, int, str]:
    return (-_token_retrieval_score(candidate), candidate.first_order, candidate.term)


def _token_retrieval_score(candidate: _TokenCandidate) -> float:
    term = candidate.term
    score = float(candidate.priority * 1000)
    length = len(term)
    if _is_code_like_token(term):
        score += 420
    vocabulary = _vocabulary()
    if term in vocabulary.allow:
        score += 180
    if vocabulary.kanji_or_katakana.search(term):
        score += min(length, 24) * 18
    elif _LATIN_ALPHA.fullmatch(term):
        score += min(length, 24) * 12
    else:
        score += min(length, 24) * 8
    if term in vocabulary.generic:
        score -= 40
    if length == 1 and term not in vocabulary.allow:
        score -= 120
    return score


def _is_code_like_token(term: str) -> bool:
    if not term:
        return False
    if re.fullmatch(r"[0-9]{2,8}", term):
        return True
    return any(pattern.fullmatch(term) for pattern in _EXTRA_PATTERNS)


def oracle_text_query_for_question(
    query: str,
    *,
    domain_keywords: Sequence[str] | None = None,
    config: TextSearchTokenizerConfig | None = None,
    max_tokens: int = MAX_TEXT_SEARCH_TOKENS,
    max_chars: int = MAX_ORACLE_TEXT_QUERY_CHARS,
) -> str:
    """質問文から、重み付きの ``ACCUM`` の Oracle Text query を作ります。語が無ければ空文字。"""
    result = tokenize_text_search_query_with_trace(
        query,
        domain_keywords=domain_keywords,
        config=config,
        max_tokens=max_tokens,
        max_query_chars=max_chars,
    )
    return result.oracle_text_query


@dataclass(frozen=True)
class OracleTextQueryPlan:
    """実際の SQL bind に使う式と、文字数で採用できなかった語。"""

    query: str
    tokens: tuple[str, ...]
    omitted_tokens: tuple[str, ...]


def build_oracle_text_query_with_trace(
    terms: Sequence[str], *, weights: Sequence[float] | None = None, max_chars: int = MAX_ORACLE_TEXT_QUERY_CHARS
) -> OracleTextQueryPlan:
    """長さの枠を守って式を作り、長過ぎる語で後続の短い語まで捨てない。"""
    pieces: list[str] = []
    included: list[str] = []
    omitted: list[str] = []
    used_chars = 0
    limit = max(1, int(max_chars or 1))
    for index, term in enumerate(terms):
        escaped = escape_oracle_text_term(term)
        if not escaped:
            continue
        weight = weights[index] if weights is not None and index < len(weights) else PRIMARY_TERM_WEIGHT
        piece = escaped if weight == PRIMARY_TERM_WEIGHT else f"{escaped}*{_format_weight(weight)}"
        addition = len(piece) + (len(_ACCUM_JOINER) if pieces else 0)
        if used_chars + addition > limit:
            omitted.append(term)
            continue
        pieces.append(piece)
        included.append(term)
        used_chars += addition
    return OracleTextQueryPlan(_ACCUM_JOINER.join(pieces), tuple(included), tuple(omitted))


def build_oracle_text_query(
    terms: Sequence[str], *, weights: Sequence[float] | None = None, max_chars: int = MAX_ORACLE_TEXT_QUERY_CHARS
) -> str:
    """重み付き ACCUM の式を実行用と診断用で共通の計画から作る。"""
    return build_oracle_text_query_with_trace(terms, weights=weights, max_chars=max_chars).query


_ACCUM_JOINER = " ACCUM "


def _format_weight(weight: float) -> str:
    # Oracle Text の重みは 0.1〜10。
    return f"{min(10.0, max(0.1, float(weight))):g}"


def escape_oracle_text_term(term: Any) -> str:
    """Oracle Text query 内で安全に使える term 文字列へ escape します。"""
    normalized = _normalize_term(term)
    if not normalized:
        return ""
    return "{" + normalized.replace("}", "}}") + "}"


def text_search_tokenizer_name(config: TextSearchTokenizerConfig | None = None) -> str:
    """今の環境で使う分割の名前（Sudachi が使えれば ``sudachi+script``、無ければ ``script``）。"""
    cfg = normalize_text_search_tokenizer_config(config)
    return TEXT_SEARCH_TOKENIZER_HYBRID if _sudachi_available(cfg) else TEXT_SEARCH_TOKENIZER_SCRIPT_ONLY


def tokenizer_fingerprint(config: TextSearchTokenizerConfig | None = None) -> str:
    """tokenizer 設定の変更検知に使う fingerprint を返します。"""
    cfg = normalize_text_search_tokenizer_config(config)
    key = "|".join(
        [
            text_search_tokenizer_name(cfg),
            cfg.sudachi_dict_type,
            cfg.sudachi_config_path,
            cfg.latin_stemmer,
            f"w={KEYWORD_TERM_WEIGHT:g}/{PRIMARY_TERM_WEIGHT:g}/{SUPPLEMENT_TERM_WEIGHT:g}",
            _package_version("sudachipy"),
            _package_version(f"sudachidict_{cfg.sudachi_dict_type}"),
            _package_version(f"sudachidict-{cfg.sudachi_dict_type}"),
            _package_version("PyStemmer") if _resolve_latin_stemmer(cfg.latin_stemmer) == "snowball" else "-",
            _vocabulary().key,
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def normalize_text_search_tokenizer_config(
    config: TextSearchTokenizerConfig | None,
) -> TextSearchTokenizerConfig:
    """未指定 tokenizer 設定へ既定値を補完します。"""
    cfg = config or TextSearchTokenizerConfig()
    dict_type = str(cfg.sudachi_dict_type or DEFAULT_TEXT_SEARCH_TOKENIZER_SUDACHI_DICT).strip()
    if not dict_type:
        dict_type = DEFAULT_TEXT_SEARCH_TOKENIZER_SUDACHI_DICT
    config_path = str(cfg.sudachi_config_path or "").strip()
    latin_stemmer = str(cfg.latin_stemmer or DEFAULT_TEXT_SEARCH_TOKENIZER_LATIN_STEMMER).strip().casefold()
    if latin_stemmer not in _VALID_LATIN_STEMMERS:
        latin_stemmer = DEFAULT_TEXT_SEARCH_TOKENIZER_LATIN_STEMMER
    return TextSearchTokenizerConfig(
        sudachi_dict_type=dict_type,
        sudachi_config_path=config_path,
        latin_stemmer=latin_stemmer,
    )


def _sudachi_available(config: TextSearchTokenizerConfig) -> bool:
    return _sudachi_failure(config.sudachi_dict_type, config.sudachi_config_path) is None


def sudachi_unavailable(config: TextSearchTokenizerConfig | None = None) -> BaseException | None:
    """Sudachi を使えないときの例外（使えれば None）。回答の診断に縮退を残すために使う（#3286）。"""
    cfg = normalize_text_search_tokenizer_config(config)
    return _sudachi_failure(cfg.sudachi_dict_type, cfg.sudachi_config_path)


@functools.lru_cache(maxsize=16)
def _sudachi_failure(dict_type: str, config_path: str) -> BaseException | None:
    """Sudachi と辞書を使えないときの例外（process ごとに 1 回だけ確かめる。使えれば None）。"""
    try:
        _get_sudachi_tokenizer(dict_type, config_path)
        _sudachi_split_mode("C")
    except Exception as exc:  # noqa: BLE001 - 辞書の欠落・破損を含め、文字種の区切りへ切り替える
        logger.warning(
            "Sudachi を使えないため、全文検索の語は文字種の区切りだけで作ります (%s)",
            type(exc).__name__,
        )
        # cache に残すので、traceback（呼び出し元の frame）は持たない。
        return exc.with_traceback(None)
    return None


def _content_terms(
    text: str,
    config: TextSearchTokenizerConfig,
    use_sudachi: bool,
) -> list[tuple[str, int]]:
    """本文の語と出どころ（主 / 補う語）を返す。"""
    if use_sudachi:
        try:
            stopwords = _sudachi_stopwords(config.sudachi_dict_type, config.sudachi_config_path, _vocabulary().stopwords)
            primary = _sudachi_tokens(text, config)
        except Exception as exc:  # noqa: BLE001 - 解析できない入力は文字種の区切りで続ける
            logger.warning("Sudachi の分割に失敗したため、文字種の区切りで続けます", exc_info=True)
            # 回答の中で分割したときは、呼び出し元の診断に残す（#3286）。
            report_degraded("text_search_tokenizer", exc)
        else:
            terms = [(token, _TIER_PRIMARY) for token in primary]
            terms.extend((token, _TIER_SUPPLEMENT) for token in _okurigana_free_forms(primary))
            terms.extend(_script_terms(text, config, stopwords, primary=False))
            return terms
    return _script_terms(text, config, _basic_stopwords(), primary=True)


def _script_terms(
    text: str,
    config: TextSearchTokenizerConfig,
    stopwords: frozenset[str],
    *,
    primary: bool,
) -> list[tuple[str, int]]:
    """文字種の区切り（漢字・カタカナ・英数字の連続）と漢字の複合語の部分語。

    ``primary`` は Sudachi が無いときに連続そのものを主の語にする。部分語はいつも補う語。
    """
    normalized = normalize_text_search_text(text)
    whole = _TIER_PRIMARY if primary else _TIER_SUPPLEMENT
    terms: list[tuple[str, int]] = []

    def push(term: str, tier: int) -> None:
        tokens: list[str] = []
        _push_token(term, tokens, config, stopwords, bridge_latin=False)
        terms.extend((token, tier) for token in tokens)

    for pattern in _EXTRA_PATTERNS:
        for matched in pattern.findall(normalized):
            push(matched, whole)
    vocabulary = _vocabulary()
    for run in vocabulary.kanji_run.findall(normalized):
        if len(run) >= 2:
            push(run, whole)
        for part in _kanji_compound_parts(run):
            push(part, _TIER_SUPPLEMENT)
    for run in vocabulary.katakana_run.findall(normalized):
        if len(run) >= 2:
            push(run, whole)
    for run in _LATIN_RUN.findall(normalized):
        if len(run) >= 2:
            push(run, whole)
        if "-" in run or "." in run:
            for part in re.split(r"[-.]", run):
                if len(part) >= 2:
                    push(part, _TIER_SUPPLEMENT)
    return terms


def _kanji_compound_parts(run: str) -> list[str]:
    """漢字の複合語の先頭 2 字・末尾 2 字（「出荷基準年月」→「出荷」「年月」）。"""
    if len(run) == 3:
        return [run[:2]]
    if len(run) >= 4:
        return [run[:2], run[-2:]]
    return []


def _okurigana_free_forms(tokens: Iterable[str]) -> list[str]:
    """送り仮名を除いた漢字だけの形（「取り消し」→「取消」、「手続き」→「手続」）。"""
    vocabulary = _vocabulary()
    forms: list[str] = []
    for token in tokens:
        if not vocabulary.hiragana.search(token):
            continue
        stripped = vocabulary.hiragana.sub("", token)
        if len(stripped) >= 2 and vocabulary.kanji_run.fullmatch(stripped):
            forms.append(stripped)
    return forms


def _sudachi_tokens(text: str, config: TextSearchTokenizerConfig) -> list[str]:
    sudachi_tokenizer = _get_sudachi_tokenizer(config.sudachi_dict_type, config.sudachi_config_path)
    split_mode = _sudachi_split_mode("C")
    sub_split_mode = _sudachi_split_mode("A")
    normalized = normalize_text_search_text(text)
    if not normalized:
        return []
    tokens: list[str] = []
    katakana_run: list[str] = []
    vocabulary = _vocabulary()
    stopwords = _sudachi_stopwords(config.sudachi_dict_type, config.sudachi_config_path, vocabulary.stopwords)

    for pattern in _EXTRA_PATTERNS:
        for matched in pattern.findall(normalized):
            _push_token(matched, tokens, config, stopwords, derive_latin=False)

    def flush_katakana() -> None:
        if len(katakana_run) >= 2:
            _push_token("".join(katakana_run), tokens, config, stopwords)
        katakana_run.clear()

    for chunk in _split_for_sudachi(normalized):
        for morpheme in sudachi_tokenizer.tokenize(chunk, split_mode):
            norm = morpheme.normalized_form().casefold()
            if vocabulary.katakana.fullmatch(norm):
                katakana_run.append(norm)
            else:
                flush_katakana()
            _emit_morpheme(morpheme, tokens, config, stopwords)
            sub_morphemes = morpheme.split(sub_split_mode)
            if len(sub_morphemes) > 1:
                for sub_morpheme in sub_morphemes:
                    _emit_morpheme(sub_morpheme, tokens, config, stopwords)
        flush_katakana()
    return tokens


def _emit_morpheme(
    morpheme: Any,
    tokens: list[str],
    config: TextSearchTokenizerConfig,
    stopwords: frozenset[str],
) -> None:
    pos = morpheme.part_of_speech()
    vocabulary = _vocabulary()
    if not pos or pos[0] not in vocabulary.keep_pos or (len(pos) > 1 and pos[1] in vocabulary.drop_pos_details):
        return
    surface = morpheme.surface()
    if " " in surface.strip():
        for part in surface.split():
            for sub_morpheme in _get_sudachi_tokenizer(
                config.sudachi_dict_type,
                config.sudachi_config_path,
            ).tokenize(part, _sudachi_split_mode("C")):
                _emit_morpheme(sub_morpheme, tokens, config, stopwords)
        return
    normalized = morpheme.normalized_form().casefold()
    _push_token(normalized, tokens, config, stopwords)
    surface_term = normalize_text_search_text(surface)
    if surface_term != normalized:
        _push_token(surface_term, tokens, config, stopwords)


def _push_token(
    raw_token: Any,
    tokens: list[str],
    config: TextSearchTokenizerConfig,
    stopwords: frozenset[str],
    *,
    derive_latin: bool = True,
    bridge_latin: bool = True,
) -> None:
    token = _normalize_term(raw_token)
    if not _keep_token(token, stopwords):
        return
    tokens.append(token)
    if not derive_latin or not _LATIN_ALPHA.fullmatch(token):
        return
    for derived in _latin_variants(token, config):
        if _keep_token(derived, stopwords):
            tokens.append(derived)
            if bridge_latin:
                for bridged in _bridge_latin_to_sudachi(derived, config, stopwords):
                    tokens.append(bridged)


def _keep_token(token: str, stopwords: frozenset[str]) -> bool:
    vocabulary = _vocabulary()
    if not token or token in vocabulary.deny or token in ORACLE_TEXT_RESERVED_TERMS:
        return False
    if not any(char.isalnum() for char in token):
        # 記号だけの語（「?!」）は検索語にしない。
        return False
    if token in vocabulary.allow:
        return True
    if token in stopwords:
        return False
    return not (len(token) == 1 and vocabulary.kana_only.fullmatch(token))


def _latin_variants(token: str, config: TextSearchTokenizerConfig) -> list[str]:
    variants: list[str] = []
    irregular = _vocabulary().en_irregular.get(token)
    if irregular:
        variants.append(irregular)
    stemmer = _resolve_latin_stemmer(config.latin_stemmer)
    if stemmer == "snowball":
        stem = _get_en_stemmer().stemWord(token)
        if stem != token:
            variants.append(stem)
    elif stemmer == "light":
        variants.extend(_plural_variants(token))
    return _dedupe(variants)


def _plural_variants(token: str) -> list[str]:
    if token.endswith("sses") and len(token) > 5:
        return [token[:-2]]
    if token.endswith("ies") and len(token) > 4:
        return [token[:-3] + "y"]
    if token.endswith(("xes", "zes")) and len(token) > 4:
        return [token[:-2]]
    if token.endswith(("ches", "shes")) and len(token) > 5:
        return [token[:-2], token[:-1]]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")) and len(token) > 3:
        return [token[:-1]]
    return []


def _bridge_latin_to_sudachi(
    token: str,
    config: TextSearchTokenizerConfig,
    stopwords: frozenset[str],
) -> list[str]:
    bridged: list[str] = []
    tokenizer = _get_sudachi_tokenizer(config.sudachi_dict_type, config.sudachi_config_path)
    for morpheme in tokenizer.tokenize(token, _sudachi_split_mode("C")):
        normalized = _normalize_term(morpheme.normalized_form())
        if normalized != token and _vocabulary().katakana.fullmatch(normalized) and _keep_token(normalized, stopwords):
            bridged.append(normalized)
    return _dedupe(bridged)


def _normalize_term(term: Any) -> str:
    normalized = normalize_text_search_text(term)
    normalized = normalized.strip("{}[]()")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if " " in normalized:
        return ""
    return _trim_japanese_particle_edges(normalized)


def _trim_japanese_particle_edges(term: str) -> str:
    vocabulary = _vocabulary()
    script = vocabulary.kanji_or_katakana
    if len(term) <= 2 or not script.search(term):
        return term
    for suffix in vocabulary.polite_suffixes:
        if term.endswith(suffix) and script.search(term[: -len(suffix)]):
            term = term[: -len(suffix)]
            break
    while len(term) > 2 and term[0] in vocabulary.particle_edges and script.search(term[1:]):
        term = term[1:]
    while len(term) > 2 and term[-1] in vocabulary.particle_edges and script.search(term[:-1]):
        term = term[:-1]
    return term


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _resolve_latin_stemmer(value: str) -> str:
    stemmer = str(value or "auto").strip().casefold()
    if stemmer == "auto":
        return "snowball" if importlib.util.find_spec("Stemmer") else "light"
    if stemmer == "snowball":
        return "snowball"
    if stemmer in {"light", "none"}:
        return stemmer
    return "light"


def _get_en_stemmer() -> Any:
    stemmer = getattr(_local, "en_stemmer", None)
    if stemmer is None:
        import Stemmer

        stemmer = _local.en_stemmer = Stemmer.Stemmer("english")
    return stemmer


@functools.lru_cache(maxsize=16)
def _sudachi_stopwords(dict_type: str, config_path: str, surfaces: tuple[str, ...]) -> frozenset[str]:
    stopwords: set[str] = {normalize_text_search_text(word) for word in surfaces}
    tokenizer = _get_sudachi_tokenizer(dict_type, config_path)
    for word in surfaces:
        for morpheme in tokenizer.tokenize(word, _sudachi_split_mode("C")):
            stopwords.add(_normalize_term(morpheme.normalized_form()))
    return frozenset(stopwords)


def _basic_stopwords() -> frozenset[str]:
    return _basic_stopwords_of(_vocabulary().stopwords)


@functools.lru_cache(maxsize=16)
def _basic_stopwords_of(surfaces: tuple[str, ...]) -> frozenset[str]:
    return frozenset(normalize_text_search_text(word) for word in surfaces)


def _get_sudachi_tokenizer(dict_type: str, config_path: str) -> Any:
    key = (dict_type, config_path)
    cache = getattr(_local, "sudachi_tokenizers", None)
    if cache is None:
        cache = _local.sudachi_tokenizers = {}
    if key not in cache:
        cache[key] = _get_sudachi_dictionary(dict_type, config_path).create()
    return cache[key]


def _get_sudachi_dictionary(dict_type: str, config_path: str) -> Any:
    key = (dict_type, config_path)
    if key not in _dicts:
        with _dict_lock:
            if key not in _dicts:
                from sudachipy import dictionary

                kwargs: dict[str, str] = {"dict": dict_type}
                if config_path:
                    kwargs["config_path"] = config_path
                _dicts[key] = dictionary.Dictionary(**kwargs)
    return _dicts[key]


@functools.lru_cache(maxsize=3)
def _sudachi_split_mode(mode: str) -> Any:
    from sudachipy import tokenizer

    return {
        "A": tokenizer.Tokenizer.SplitMode.A,
        "B": tokenizer.Tokenizer.SplitMode.B,
        "C": tokenizer.Tokenizer.SplitMode.C,
    }[mode]


def _split_for_sudachi(text: str, max_bytes: int = _SUDACHI_MAX_BYTES) -> list[str]:
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    parts: list[str] = []
    buffer = ""
    for piece in _vocabulary().sentence_split.split(text):
        if len((buffer + piece).encode("utf-8")) > max_bytes:
            if buffer:
                parts.append(buffer)
                buffer = ""
            while len(piece.encode("utf-8")) > max_bytes:
                cut = _preferred_cut(piece, _fit_chars(piece, max_bytes))
                parts.append(piece[:cut])
                piece = piece[cut:]
        buffer += piece
    if buffer:
        parts.append(buffer)
    return parts


def _fit_chars(text: str, max_bytes: int) -> int:
    lo = 1
    hi = len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(text[:mid].encode("utf-8")) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _preferred_cut(text: str, limit: int) -> int:
    bounds = _vocabulary().split_bounds
    for index in range(limit, max(limit - 200, 1), -1):
        previous = text[index - 1]
        next_char = text[index]
        if previous.isspace() or next_char.isspace() or previous in bounds or next_char in bounds:
            return index
        if _script_class(previous) != _script_class(next_char):
            return index
    return limit


def _script_class(char: str) -> int:
    vocabulary = _vocabulary()
    if vocabulary.hiragana.fullmatch(char):
        return 1
    if vocabulary.katakana.fullmatch(char):
        return 2
    if vocabulary.kanji_run.fullmatch(char):
        return 3
    if char.isascii() and char.isalnum():
        return 4
    return 0


def _package_version(name: str) -> str:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return "not-installed"
