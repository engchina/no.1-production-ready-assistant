"""複数引用を一操作へ接続する際の文書・版・機能境界を検証する。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from rag_parser_core.language import current_pack

from rag_engine.retrieval.evidence_cues import evidence_cues
from rag_engine.retrieval.operation_context import operation_labels


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _lines(text: str) -> list[str]:
    """原文を文に分ける（文を区切る字は言語資源の question_cues。改行はいつも区切る）。"""
    chars = re.escape("".join(current_pack().words("question_cues", "sentence_split_chars")))
    return re.split(rf"[{chars}\n]", text)


def _explicit_link(names: set[str], spans: Sequence[dict[str, Any]]) -> bool:
    """全機能名と関係動詞（言語資源の link_verbs）が同じ原文の文にある時だけ接続根拠とする。"""
    cues = evidence_cues()
    return any(
        all(_compact(name) in _compact(line) for name in names) and cues.has("link_verbs", line) and not cues.has("link_negations", line)
        for span in spans
        for line in _lines(span.get("text", ""))
    )


def evidence_scope_error(spans: Sequence[dict[str, Any]]) -> str:
    """明示metadataの衝突だけを返す。異なるページ・出典というだけでは拒否しない。

    文書の比較や別々の条件分岐でなく、一つの操作関係／操作段落へ引用を結ぶ時に使う。
    未記載の業務や版は推測しない。同機能の続き・原文明示の機能間連携は保持する。
    """
    versions: dict[str, set[str]] = {}
    functions = set()
    classifications = set()
    for span in spans:
        scope = span.get("document_scope") or []
        if len(scope) >= 2 and scope[0] and scope[1]:
            versions.setdefault(scope[0], set()).add(scope[1])
        business = span.get("business_scope", "")
        if business:
            classifications.add(business)
        labels = operation_labels(SimpleNamespace(metadata={"section_path": span.get("section_path", [])}))
        if len(labels) == 1:
            functions.add(next(iter(labels)).split(")", 1)[-1])
    if any(len(values) > 1 for values in versions.values()):
        return "同一文書の異なる版を一つの操作へ合成できない"
    if len(classifications) > 1 and not _explicit_link(classifications, spans):
        return "異なる業務を接続する明示根拠がない"
    if len(functions) > 1 and not _explicit_link(functions, spans):
        return "別機能の操作を接続する明示根拠がない"
    return ""


def unsupported_heading_navigation(answer: str, spans: Sequence[dict[str, Any]]) -> str:
    """節見出しを開く指示に変換した場合、同じ原文中の操作関係を要求する。"""
    cues = evidence_cues()
    alt = cues.alt
    labels = re.findall(rf"(?:[A-ZＡ-Ｚ]\s*)?[{cues.label_opens}][^{cues.label_closes}]{{1,40}}[{cues.label_closes}]", answer)
    place = f"{alt['screen_place_words']}{alt['place_particles']}"
    if not re.search(f"{alt['navigation_words']}|{place}", answer):
        return ""
    evidence = re.compile(f"{alt['navigation_evidence_words']}|{place}.*{alt['entry_action_words']}")
    for label in labels:
        if not any(_compact(label) in _compact(line) and evidence.search(line) for span in spans for line in _lines(span.get("text", ""))):
            return "文書の節見出しと操作入口の対応が未確認"
    return ""
