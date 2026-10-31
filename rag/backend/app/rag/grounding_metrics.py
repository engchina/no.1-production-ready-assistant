"""評価専用の回答と根拠の語の重なりの計測。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag_engine.retrieval.character_forms import fold_text
from rag_parser_core.language import current_pack
from rag_parser_core.language.text import range_class


def _japanese_chars() -> str:
    """日本語の文字の範囲（ひらがな・カタカナ・漢字。言語資源の search_terms。#2181）。"""
    pack = current_pack()
    return range_class(
        pack.words("search_terms", "hiragana_chars")
        + pack.words("search_terms", "katakana_chars")
        + pack.words("search_terms", "kanji_chars")
    )


def grounding_token_pattern() -> re.Pattern[str]:
    """根拠との重なりを数える語（英数字の並びか日本語の文字の並び）。"""
    return re.compile(rf"[a-z0-9_]+|{_japanese_chars()}+", re.IGNORECASE)


def grounding_stopwords() -> frozenset[str]:
    """根拠との重なりに数えない語（言語資源の grounding_cues.grounding_stopwords）。"""
    return frozenset(current_pack().words("grounding_cues", "grounding_stopwords"))


MIN_GROUNDING_OVERLAP = 3
MIN_GROUNDING_RATIO = 0.12


@dataclass(frozen=True)
class GroundednessEvaluation:
    """回答が citation context に支えられているかの軽量評価。"""

    grounded: bool
    score: float
    overlap_count: int
    answer_feature_count: int
    high_signal_overlap: bool


def evaluate_groundedness(
    answer: str,
    context: str,
    *,
    min_overlap: int = MIN_GROUNDING_OVERLAP,
    min_ratio: float = MIN_GROUNDING_RATIO,
) -> GroundednessEvaluation:
    """回答が引用 context と最低限重なっているかを軽量に評価する。

    評価の重なりを計測する。回答の安全チェックや表示の阻止には使わない。
    """
    if not answer.strip():
        return GroundednessEvaluation(
            grounded=True,
            score=1.0,
            overlap_count=0,
            answer_feature_count=0,
            high_signal_overlap=False,
        )
    if not context.strip():
        return GroundednessEvaluation(
            grounded=False,
            score=0.0,
            overlap_count=0,
            answer_feature_count=len(_grounding_features(answer)),
            high_signal_overlap=False,
        )

    answer_features = _grounding_features(answer)
    context_features = _grounding_features(context)
    if not answer_features or not context_features:
        return GroundednessEvaluation(
            grounded=False,
            score=0.0,
            overlap_count=0,
            answer_feature_count=len(answer_features),
            high_signal_overlap=False,
        )

    overlap = answer_features & context_features
    high_signal = {feature for feature in answer_features if _is_high_signal_feature(feature)}
    score = round(min(1.0, len(overlap) / len(answer_features)), 4)
    required_overlap = min(min_overlap, len(answer_features))
    return GroundednessEvaluation(
        grounded=len(overlap) >= required_overlap and score >= min_ratio,
        score=score,
        overlap_count=len(overlap),
        answer_feature_count=len(answer_features),
        high_signal_overlap=bool(high_signal & context_features),
    )


def _grounding_features(text: str) -> set[str]:
    """日本語・英数字の token と日本語 n-gram を groundedness 用特徴にする。

    文字の形は全文検索の索引と同じ ``fold_text``（NFKC・波線とダッシュ）でそろえる。
    根拠の全角の ``ＨＲＭ`` と回答の半角の ``HRM`` を同じ token にするため (#1350)。
    """
    features: set[str] = set()
    stopwords = grounding_stopwords()
    for match in grounding_token_pattern().finditer(fold_text(text)):
        token = match.group(0).lower()
        if len(token) < 2 or token in stopwords:
            continue
        features.add(token)
        if _contains_japanese(token) and len(token) >= 4:
            features.update(_char_ngrams(token, 3))
    return features


def _contains_japanese(text: str) -> bool:
    """日本語文字を含むか。"""
    return bool(re.search(_japanese_chars(), text))


def _char_ngrams(text: str, n: int) -> set[str]:
    """空白除去済み文字 n-gram。"""
    normalized = re.sub(r"\s+", "", text)
    if len(normalized) < n:
        return set()
    return {normalized[index : index + n] for index in range(len(normalized) - n + 1)}


def _is_high_signal_feature(feature: str) -> bool:
    """金額・日付・ID らしい特徴(数字を含む)は強い根拠として扱う。

    純英単語(data/system/oracle 等)まで high-signal にすると 1 語一致で
    min_overlap/min_ratio を素通りし strict/regulated の閾値が無効化されるため、
    数字を含む特徴に限定する。
    """
    return any(char.isdigit() for char in feature)
