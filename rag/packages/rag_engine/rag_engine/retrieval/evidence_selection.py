"""質問に関連する原文を、出典ごとの予算と操作文脈を保って選ぶ。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from rag_parser_core.language import current_pack
from rag_parser_core.language.text import range_class

from rag_engine.parsing.content_labels import content_labels
from rag_engine.parsing.structural_text import structural_only
from rag_engine.retrieval.attachment_evidence import is_attachment_evidence
from rag_engine.retrieval.character_forms import find_width_folded
from rag_engine.retrieval.definition_evidence import definition_labels, definition_ranges, definition_windows
from rag_engine.retrieval.evidence_cues import evidence_cues
from rag_engine.retrieval.task_contract import (
    COUNT_REQUEST,
    LIST_REQUEST,
    STEPS_REQUEST,
    current_request_text,
    explicit_requests,
    goal_retrieval_queries,
    task_contract,
)


def _record_page_end(record: Any) -> int:
    """record の最終頁。持たない record では先頭頁を返す。"""
    try:
        value = getattr(record, "page_end", None)
        page = int(getattr(record, "page", 0) or 0)
        return max(page, int(value)) if value is not None else page
    except (TypeError, ValueError):
        return int(getattr(record, "page", 0) or 0)


def _terms(value: str) -> set[str]:
    text = unicodedata.normalize("NFKC", value).lower()
    cues = evidence_cues()
    terms = set(re.findall(r"[a-z][a-z0-9_-]{1,}", text))
    for word in re.findall(rf"{cues.kana_kanji}{{2,}}", text):
        terms.add(word)
        terms.update(word[i : i + 2] for i in range(len(word) - 1))
    return terms - set(cues.words["relevance_stop_terms"])


def evidence_relevance(question: str, text: str) -> float:
    """用語一致と操作ラベルから相対関連度を返す。事実の正しさは判定しない。"""
    normalized = unicodedata.normalize("NFKC", text).lower()
    cues = evidence_cues()
    alt = cues.alt
    labels = content_labels()
    # 画像の説明の「操作:」「条件と結果:」の欄（content_labels）と、操作の説明の語（evidence_cues）。
    operation_fields = labels.field_line("operation_steps")
    operation_text = f"{operation_fields}|{labels.field_line('condition_result_pairs')}|{alt['operation_text_cues']}"
    matches = [term for term in _terms(current_request_text(question)) if term in normalized]
    score = sum(min(len(term), 8) for term in matches)
    # 試行した経路の語数が多くても、希望する結果を扱う原文を残す。
    goal_terms = _terms(" ".join(goal_retrieval_queries(question)))
    score += 2 * sum(min(len(term), 8) for term in goal_terms if term in normalized)
    # 列名の出現数ではなく、質問の各項目に対する定義本文を優先する。
    score += 100 * len({item["label"] for item in definition_ranges(text, definition_labels(question))})
    # 一致する操作説明を画面のサンプル値より優先する。無関係な操作には加点しない。
    if matches:
        score *= 1 + 0.18 * min(3, len(re.findall(operation_text, text)))
    requests = explicit_requests(question)
    if STEPS_REQUEST in requests and matches:
        if re.search(rf"(?m)^\s*[{cues.chars('heading_bullet_chars')}]", text):
            score += 80
        if content_labels().index_line.match(text):
            score *= 0.2
    if re.search(alt["default_question_cues"], question, re.I):
        score += 10 * len(set(re.findall(alt["default_text_cues"], text)))
    if requests & {LIST_REQUEST, COUNT_REQUEST} == {LIST_REQUEST}:
        # 表名一致だけでは集計値の説明が名簿の操作を押し出す。原質問の出力対象で
        # 補正し、検索用ラベルの反復より一覧の選択・抽出・実行を残す。
        list_terms = set(re.findall(alt["list_text_cues"], normalized))
        score += 12 * len(list_terms)
        if list_terms and re.search(rf"{operation_fields}|{alt['list_action_words']}.*{alt['list_execute_words']}", text):
            score += 40
        if content_labels().index_line.match(text):
            score *= 0.2
    return score


def evidence_excerpt(
    question: str,
    text: str,
    budget: int,
    *,
    required_ranges: Sequence[tuple[int, int]] = (),
    priority_ranges: Sequence[tuple[int, int]] = (),
    required_only: bool = False,
) -> str:
    """関連窓と隣接文を原文順に返す。切断箇所は明示し、値や原文は改変しない。

    budgetは文字数。長いHTMLや改行なしのOCRも窓で探索するため、末尾の規則を
    先頭切詰めで失わない。短いテキストは全文を返す。
    """
    if budget <= 0:
        return ""
    if len(text) <= budget and not required_only:
        return text
    window = min(budget, 700, max(160, budget // 2))
    # 段落・操作行が収まる場合はその境界を守り、長いHTMLだけ窓へ分割する。
    spans = []
    cues = evidence_cues()
    bullets = cues.chars("heading_bullet_chars")
    notes = cues.chars("note_mark_chars")
    # 見出しにならない字（文末・ラベルの区切り・箇条書き・注記の印）。文末の字は question_cues。
    plain = cues.chars("label_colon_chars", "heading_bullet_chars", "item_bullet_chars", "note_mark_chars") + re.escape(
        "".join(current_pack().words("question_cues", "sentence_split_chars"))
    )
    # 項目見出しと続く操作・注意書きは一つの引用窓に保ち、操作だけが落ちるのを防ぐ。
    for match in re.finditer(rf"[{bullets}][^\n]*(?:\n(?!\n|[{bullets}])[^\n]+)*\n?|[^\n]+(?:\n|$)", text):
        left, right = match.span()
        if match.group().startswith(cues.words["heading_bullet_chars"]):
            # 項目直前の短い上位見出しも原文順で保持し、タブと項目の階層を失わない。
            headings = list(re.finditer(rf"(?m)^[^{plain}\n]{{2,24}}\n(?=[{notes}{bullets}])", text[max(0, left - 400) : left]))
            if headings:
                left = max(0, left - 400) + headings[-1].start()
        if right - left <= budget:
            spans.append((left, right))
        else:
            spans.extend((start, min(right, start + window)) for start in range(left, right, max(1, window // 2)))
    if not spans:
        return text[:budget]
    # 表の項目名だけに点数が集中しても、後続の番号付き完了操作を落とさない。
    # 長い表全体は必須にせず、実際の操作を含む原文行を保護する。
    steps = []
    step_number = rf"\s*(?:[0-9０-９]+\s*[{cues.chars('step_number_closers')}]|{range_class(cues.words['step_circled_numbers'])})"
    requests = explicit_requests(question)
    if STEPS_REQUEST in requests or requests & {LIST_REQUEST, COUNT_REQUEST} == {LIST_REQUEST}:
        for match in re.finditer(r"(?m)^[^\n]+", text):
            numbered = re.match(step_number, match.group())
            conditional = cues.has("step_condition_cues", match.group())
            if (numbered or conditional) and cues.has("step_action_words", match.group()):
                steps.append(match.span())
    # 短い節名の直後に箇条書きの条件が続く場合、節名と条件を同じ窓にする。
    # 片方の処理区分名だけが落ちると、同ページの別モードへ条件を誤配属する。
    if STEPS_REQUEST in requests:
        lines = list(re.finditer(r"(?m)^[^\n]*(?:\n|$)", text))
        for index, line in enumerate(lines[:-1]):
            heading = line.group().strip()
            if not re.fullmatch(rf"[^{plain}]{{2,32}}", heading) or not lines[index + 1].group().lstrip().startswith(
                cues.words["item_bullet_chars"]
            ):
                continue
            end = lines[index + 1].end()
            for following in lines[index + 2 :]:
                body = following.group().strip()
                if (
                    not body
                    or re.fullmatch(rf"[^{plain}]{{2,32}}", body)
                    or re.match(
                        rf"(?:[0-9０-９]+[{cues.chars('step_number_closers')}]|[{bullets}]"
                        rf"|[^{cues.chars('label_colon_chars')}]{{1,20}}[{cues.chars('label_colon_chars')}])",
                        body,
                    )
                ):
                    break
                end = following.end()
            steps.append((line.start(), end))
    priority = list(dict.fromkeys((a, b) for a, b in priority_ranges if 0 <= a < b <= len(text)))
    protected = sorted(set([*priority, *required_ranges, *definition_windows(text, definition_labels(question)), *steps]))
    if required_only and priority:
        protected = priority
    # 定義を一行ごとの点数で分断しない。収まらない完全定義の断片も候補から除く。
    spans = [(a, b) for a, b in spans if not any(a < y and b > x for x, y in protected)]
    if required_only:
        spans = []
    ranked = [*priority, *protected, *sorted(spans, key=lambda span: (-evidence_relevance(question, text[span[0] : span[1]]), span[0]))]
    selected: list[tuple[int, int]] = []
    for start, end in ranked:
        merged = sorted([*selected, (start, end)])
        union: list[tuple[int, int]] = []
        for left, right in merged:
            if union and left <= union[-1][1]:
                union[-1] = (union[-1][0], max(right, union[-1][1]))
            else:
                union.append((left, right))
        if sum(b - a for a, b in union) + 8 * (len(union) - 1) <= budget:
            selected = union
    return "\n[…]\n".join(text[a:b] for a, b in selected)[:budget]


def record_fingerprint(record: Any) -> str:
    """同じ文書・ページ・節・全文の重複だけを識別する。版の異なる本文・別の文書の同じ本文は統合しない。"""
    metadata = getattr(record, "metadata", {}) or {}
    record_id = getattr(record, "chunk_uid", "") or record.id
    # 出典不明の本文は、同文でも別資料の可能性があるためIDを隔離キーにする。
    source = getattr(record, "source", "") or record_id
    from rag_engine.retrieval.metadata_context import document_context_key, document_identity

    value = [
        source,
        document_context_key(record),
        # 文書の確かな識別子が無い根拠は、表示名が同じでも別の文書の可能性があるので record ごとに隔離する（#2603）。
        document_identity(record) or "record:" + record_id,
        getattr(record, "page", 0),
        metadata.get("section_path"),
        str(getattr(record, "text", "")),
    ]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# 複数レシピ融合の根拠の重複除去（#1331）。同じ文書の active なレシピ（最大 3 つ）の分割が違うと、同じ箇所の
# 本文が少しずつ違う根拠として並び、原文の文字予算を同じ箇所で使ってしまう。元の文書の範囲（解析の要素の ID）が
# 大きく重なり、本文もほぼ含まれる根拠を 1 つにまとめる。
# 要素の集合の重なりの下限（小さい側の要素の数に対する割合）。
SOURCE_SPAN_ELEMENT_OVERLAP_MIN = 0.8
# 本文の照合の下限（短い側の本文の 3 文字組のうち、もう一方の本文に含まれる割合）。要素の ID は解析の結果
# ごとの番号で、解析の設定が違うレシピでは同じ ID が別の箇所を指すことがあるため、本文でも確かめる。
SOURCE_SPAN_TEXT_CONTAINMENT_MIN = 0.9
_SOURCE_SPAN_SHINGLE_CHARS = 3


def record_element_ids(record: Any) -> frozenset[str]:
    """record が含む解析の要素の ID。metadata の ``element_ids``、無ければ ``source_record_refs`` の record_id。

    文字数・区切り文字・Markdown の見出しでの分割の chunk は要素の ID を持たず、空を返す。
    """
    value = (getattr(record, "metadata", {}) or {}).get("element_ids")
    items: list[Any] = value.split(",") if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else []
    ids = {text for item in items if (text := str(item).strip())}
    if not ids:
        ids = {
            text
            for ref in getattr(record, "source_record_refs", ()) or ()
            if isinstance(ref, dict) and (text := str(ref.get("record_id") or "").strip())
        }
    return frozenset(ids)


def _span_shingles(record: Any, cache: dict[int, frozenset[str]]) -> frozenset[str]:
    """本文（空白を除いた NFKC の小文字）の 3 文字組の集合。同じ呼び出しの中では record ごとに 1 回だけ作る。"""
    found = cache.get(id(record))
    if found is None:
        text = str(getattr(record, "body_text", "") or getattr(record, "text", "") or "")
        compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).lower()
        size = _SOURCE_SPAN_SHINGLE_CHARS
        found = frozenset(compact[i : i + size] for i in range(len(compact) - size + 1)) or frozenset({compact} - {""})
        cache[id(record)] = found
    return found


def _same_source_document(left: Any, right: Any) -> bool:
    """同じ文書の根拠か。文書の確かな識別子が両方で同じときだけ True（表示名だけでは決めない。#2603）。"""
    from rag_engine.retrieval.metadata_context import document_context_key, document_identity

    identity = document_identity(left)
    return bool(identity) and identity == document_identity(right) and document_context_key(left) == document_context_key(right)


def source_span_relation(kept: Any, other: Any, cache: dict[int, frozenset[str]] | None = None) -> str | None:
    """2 つの根拠の元の文書の範囲の関係（#1331）。まとめられなければ None。

    - ``"same"``: 互いの本文をほぼ含む（同じ範囲）。
    - ``"kept_in_other"``: kept の本文が other にほぼ含まれ、other のほうが広い。
    - ``"other_in_kept"``: other の本文が kept にほぼ含まれ、kept のほうが広い。

    同じ文書で、要素の ID の集合が小さい側の ``SOURCE_SPAN_ELEMENT_OVERLAP_MIN`` 以上重なり、短い側の本文の
    ``SOURCE_SPAN_TEXT_CONTAINMENT_MIN`` 以上がもう一方に含まれるときだけまとめる。要素の ID が無い根拠は
    判定しない（本文が同じ根拠を統合する ``record_fingerprint`` の規則だけが働く）。
    """
    left_ids, right_ids = record_element_ids(kept), record_element_ids(other)
    if not left_ids or not right_ids or not _same_source_document(kept, other):
        return None
    if len(left_ids & right_ids) < SOURCE_SPAN_ELEMENT_OVERLAP_MIN * min(len(left_ids), len(right_ids)):
        return None
    cache = {} if cache is None else cache
    left, right = _span_shingles(kept, cache), _span_shingles(other, cache)
    if not left or not right:
        return None
    left_inside = len(left & right) / len(left) >= SOURCE_SPAN_TEXT_CONTAINMENT_MIN
    right_inside = len(left & right) / len(right) >= SOURCE_SPAN_TEXT_CONTAINMENT_MIN
    if left_inside and right_inside:
        return "same"
    if left_inside:
        return "kept_in_other"
    if right_inside:
        return "other_in_kept"
    return None


def collapse_source_spans(records: Sequence[Any]) -> list[tuple[Any, list[Any]]]:
    """順位順の根拠を、元の文書の範囲が同じものごとに 1 つにまとめる（#1331）。

    返り値は (残す根拠, まとめた根拠の順位順の list) を、まとめた中で最も順位の高い根拠の位置に並べたもの。
    残すのは順位の高いほう。ただし順位の低いほうが高いほうの本文を含んでさらに広いときは、範囲の広いほうを
    残す（狭いほうを残すと、広いほうにだけある本文を落とす）。
    """
    cache: dict[int, frozenset[str]] = {}
    groups: list[tuple[list[Any], list[Any]]] = []
    for record in records:
        for kept, members in groups:
            relation = source_span_relation(kept[0], record, cache)
            if relation is None:
                continue
            members.append(record)
            if relation == "kept_in_other":
                kept[0] = record
            break
        else:
            groups.append(([record], [record]))
    return [(kept[0], members) for kept, members in groups]


def new_evidence_count(records: Sequence[Any], seen: Sequence[Any]) -> int:
    """seen に無い根拠の数。本文が同じ根拠と、元の文書の範囲が同じ根拠（#1331）は新しいと数えない。"""
    cache: dict[int, frozenset[str]] = {}
    keys = {record_fingerprint(record) for record in seen}
    known = list(seen)
    count = 0
    for record in records:
        key = record_fingerprint(record)
        if key in keys or any(source_span_relation(item, record, cache) for item in known):
            continue
        keys.add(key)
        known.append(record)
        count += 1
    return count


# 回答生成に渡す原文の総予算（文字）。親 6,000 字の候補を複数の機能・文書から入れられる大きさ (#665)。
EVIDENCE_BUDGET_CHARS = 80000
# 後続候補のために取り置く短い原文範囲の上限（親ごと）。
NATIVE_RESERVE_CAP = 1200


def _union_source_ranges(ranges: Sequence[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """同じ親の重なる物理範囲を、子の別名の数に依存せず一度だけ数える。"""
    merged: list[tuple[str, int, int]] = []
    for parent, start, end in sorted(set(ranges)):
        if merged and merged[-1][0] == parent and start <= merged[-1][2]:
            previous = merged[-1]
            merged[-1] = (parent, previous[1], max(previous[2], end))
        else:
            merged.append((parent, start, end))
    return merged


def _citation_partition_boundary(text: str, position: int) -> bool:
    """子・由来の境界だけでは文中を独立した引用候補へ切り分けない。"""
    if position <= 0 or position >= len(text):
        return True
    if text[position - 1] == "\n" or text[position] == "\n":
        return True
    preceding = text[:position].rstrip(" \t")
    return bool(preceding) and preceding[-1] in current_pack().words("question_cues", "unit_split_chars")


def evidence_spans(
    question: str,
    records: Sequence[Any],
    *,
    budget: int = EVIDENCE_BUDGET_CHARS,
    anchors: Sequence[Any] = (),
    definitions_only: bool = False,
    scope_records: Sequence[Any] = (),
    whole_records: bool = False,
    priority_source_groups: Sequence[Sequence[str]] = (),
    verified_quotes: Mapping[str, Sequence[str]] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """原文の文字予算内で安定IDを付け、照合済みanchorsの本文と頁を優先する。

    anchorsは同じ検索で取得したchildだけを渡す。文書・版・親子関係・位置を
    再検証し、照合できないchildは頁確定や原文保護に使わない。
    definitions_onlyは予算競合時の再切出し用で、定義以外の窓を追加しない。
    scope_recordsは取得済みchildの所属照合専用で、全文の予算優先には使わない。
    whole_recordsは、全部の根拠の本文が予算（前置きの分の 1 割を除く）に入るとき、根拠ごとの配分で切らずに
    全文を渡す（章節の拡張で読んだ章節を切らない。#1436）。入らなければ通常の配分にする。
    verified_quotes は出典の ID（根拠かまとめた別名の ID）ごとの、検索の評価が写しを確かめた原文（#3840）。
    根拠の本文の中の位置（``find_width_folded``。照合をあいまいにしない）を必須の範囲にして予算の中で保ち、
    span の分け目を写しの中に置かない。入らなかった写しは diagnostics に理由（``verified_quote``）を残す。
    """
    # 本文が同じ根拠（record_fingerprint）を先にまとめる。
    same_text: dict[str, list[Any]] = {}
    for record in records:
        if structural_only(record.text):
            if diagnostics is not None:
                diagnostics.append(
                    {
                        "stage": "excluded",
                        "reason_code": "structural_only",
                        "source_id": getattr(record, "chunk_uid", "") or record.id,
                        "start": 0,
                        "end": len(record.text),
                    }
                )
            continue
        same_text.setdefault(record_fingerprint(record), []).append(record)
    first_keys = {id(members[0]): key for key, members in same_text.items()}
    # 複数レシピ融合で元の文書の範囲が同じ根拠を 1 つにまとめ、まとめた根拠の ID は別名に残す (#1331)。
    unique: dict[str, Any] = {}
    aliases: dict[str, list[str]] = {}
    member_keys: dict[str, set[str]] = {}
    for kept, members in collapse_source_spans([members[0] for members in same_text.values()]):
        key = first_keys[id(kept)]
        grouped = [item for member in members for item in same_text[first_keys[id(member)]]]
        unique[key] = kept
        aliases[key] = [getattr(item, "chunk_uid", "") or item.id for item in grouped]
        member_keys[key] = {
            value for item in grouped for value in (getattr(item, "chunk_uid", ""), getattr(item, "chunk_id", ""), item.id) if value
        }
    # 評価が写しを確かめた原文（#3840）を、まとめた根拠ごとに本文の中の位置へ戻す。
    quote_order = list(
        dict.fromkeys((str(source), str(quote)) for source, quotes in (verified_quotes or {}).items() for quote in quotes if quote)
    )
    quote_requests: dict[str, list[tuple[str, str]]] = {}
    quote_ranges: dict[tuple[str, str], tuple[int, int]] = {}
    for item in quote_order:
        key = next((key for key, ids in member_keys.items() if item[0] in ids), None)
        if key is None:
            continue
        quote_requests.setdefault(key, []).append(item)
        located_quote = find_width_folded(unique[key].text, item[1])
        if located_quote is not None:
            quote_ranges[item] = located_quote
    labels = definition_labels(question)
    from rag_engine.retrieval.metadata_context import answer_metadata_context, document_context_key
    from rag_engine.retrieval.source_context import prompt_source_context_text, source_context

    def note_cost(context: dict[str, Any], record: Any) -> int:
        """生成の入力に描く出典情報の字数（#3831）。予算は描いた本文と補助情報を数える。"""
        return len(prompt_source_context_text(context, document_name=str(getattr(record, "source", "") or "")))

    remaining = max(0, budget)
    context_remaining = max(0, budget // 10)
    seen_documents = set()
    result = []
    ordered = _same_unit_first(list(unique.items()), question)
    if labels:
        ordered.sort(key=lambda item: -len(definition_ranges(item[1].text, labels)))
    # 必須手順の直接の根拠を各手順に一つずつ先に割り当てる。件数・文字予算は増やさない。
    # 同じ機能の説明や任意の章節が、後続の必須手順の原文を予算外へ押し出さないようにする。
    priority_keys: set[str] = set()
    # 子のIDだけで親の本文を採用しない。同じ検索のanchorを親子・版・本文位置で照合する。
    child_locations: dict[str, list[tuple[int, int, Any]]] = {
        key: [(a, b, child) for child in anchors for a, b, _page in _child_ranges(record, [child], parent_keys=member_keys[key])]
        for key, record in ordered
    }
    child_keys: dict[str, set[str]] = {
        key: {
            str(value)
            for _a, _b, child in locations
            for value in (getattr(child, "chunk_uid", ""), getattr(child, "chunk_id", ""), getattr(child, "id", ""))
            if value
        }
        for key, locations in child_locations.items()
    }
    if priority_source_groups:
        groups = [set(sources) for sources in priority_source_groups]
        selected: list[tuple[str, Any]] = []
        selected_keys: set[str] = set()
        for sources in groups:
            match = next(
                (item for item in ordered if item[0] not in selected_keys and (member_keys[item[0]] | child_keys[item[0]]) & sources), None
            )
            if match is not None:
                selected.append(match)
                selected_keys.add(match[0])
                priority_keys.add(match[0])
        wanted = set().union(*groups)
        remaining_guided = [
            item for item in ordered if item[0] not in selected_keys and (member_keys[item[0]] | child_keys[item[0]]) & wanted
        ]
        selected.extend(remaining_guided)
        selected_keys.update(item[0] for item in remaining_guided)
        ordered = [*selected, *(item for item in ordered if item[0] not in selected_keys)]

    # 後続の候補の短い原文範囲（各親 NATIVE_RESERVE_CAP まで）の分は先に取り置き、先頭の候補の生成説明に
    # 予算を使い切らせない (#730)。21 親のうち 17 番目の親の ※ 注記（標準回答の操作）が予算外に落ちていた。
    # 取り置きは最終的な処理順（定義質問の並べ替え後）で数える。並べ替え前に数えると index と後続候補が
    # 対応せず、定義質問で取り置きが過不足になる (#744)。
    def native_need(record: Any) -> int:
        ranges = [(a, b) for a, b in _native_ranges(record) if b - a <= 600]
        return (
            note_cost(source_context(record), record) * (1 + len(ranges))
            + len(answer_metadata_context(record, max_chars=1200, include_section=False))
            + min(NATIVE_RESERVE_CAP, sum(b - a for a, b in ranges) + 8 * max(0, len(ranges) - 1))
        )

    def whole_record_cost(key: str, record: Any) -> int:
        """本文の分割境界ごとに反復する完全な出典情報も全文の費用に含める。"""
        text = str(getattr(record, "text", "") or "")
        located = [(a, b) for a, b, _child in child_locations[key]]
        scoped = [(a, b) for child in scope_records for a, b, _page in _child_ranges(record, [child], parent_keys=member_keys[key])]
        provenance = [
            (a, b, child)
            for child in scope_records
            for a, b, _page in _child_ranges(record, [child], parent_keys=member_keys[key], body_text=True)
        ]
        cuts = sorted({0, len(text), *(edge for a, b in [*located, *scoped, *_native_ranges(record)] for edge in (a, b))})
        total = len(text)
        for start, end in zip(cuts, cuts[1:], strict=False):
            children = []
            for a, b, child in provenance:
                if a < end and start < b:
                    info = {"source_id": child.chunk_uid or child.id, **source_context(child)}
                    if info not in children:
                        children.append(info)
            info = {**source_context(record), **({"children": children} if children else {})}
            total += note_cost(info, record)
        return total

    whole = (
        whole_records
        and not definitions_only
        and sum(whole_record_cost(key, record) for key, record in ordered) <= max(0, budget - budget // 10)
    )
    reserve_after = [0] * (len(ordered) + 1)
    for position in range(len(ordered) - 1, -1, -1):
        reserve_after[position] = reserve_after[position + 1] + native_need(ordered[position][1])
    for index, (key, record) in enumerate(ordered):
        text = str(getattr(record, "text", "") or "")
        priority_ranges = [
            (a, b)
            for a, b, child in child_locations[key]
            if not definitions_only
            and any(
                (member_keys[key] | {str(getattr(child, attr, "")) for attr in ("chunk_uid", "chunk_id", "id")}) & set(group)
                for group in priority_source_groups
            )
        ]
        priority_ranges = list(dict.fromkeys(priority_ranges))
        source_info = source_context(record)
        source_cost = note_cost(source_info, record)
        if source_cost >= remaining:
            continue
        remaining -= source_cost
        document_key = document_context_key(record)
        required = definition_windows(text, labels)
        # まとめた根拠（#1331）の child も、本文が残す根拠の中に一意に見つかれば位置と頁に使う。
        parent_keys = member_keys[key]
        located = _child_ranges(record, anchors, parent_keys=parent_keys)
        scoped = [(a, b, page, child) for child in scope_records for a, b, page in _child_ranges(record, [child], parent_keys=parent_keys)]
        # 出典の行を結ぶ子の範囲（#2710）。子の検索文は画面の例の値などを除いているので、子の元の本文で親の中を探す。
        provenance = [
            (a, b, page, child)
            for child in scope_records
            for a, b, page in _child_ranges(record, [child], parent_keys=parent_keys, body_text=True)
        ]
        # 子の本文だけでなく、そこに実際に重なる完全な出典情報も先に取り置く。
        # 後付けの補助情報で選択済みの本文を取り消すことを避ける。
        priority_extra_cost = 0
        affordable_priority_ranges: list[tuple[int, int]] = []
        priority_cost = 0
        for start, end in priority_ranges:
            children = []
            for a, b, _page, child in provenance:
                if a < end and start < b:
                    info = {"source_id": child.chunk_uid or child.id, **source_context(child)}
                    if info not in children:
                        children.append(info)
            info = {**source_info, **({"children": children} if children else {})}
            identity_cost = note_cost(info, record)
            cost = end - start + identity_cost + (8 if affordable_priority_ranges else 0)
            if priority_cost + cost <= remaining + source_cost:
                affordable_priority_ranges.append((start, end))
                priority_cost += cost
                priority_extra_cost += identity_cost
        if priority_ranges:
            priority_extra_cost = max(0, priority_extra_cost - source_cost) if affordable_priority_ranges else remaining
        prefix = answer_metadata_context(
            record,
            max_chars=min(
                1200,
                context_remaining,
                max(0, remaining - sum(b - a for a, b in priority_ranges) - priority_extra_cost),
                max(0, remaining // 10),
            ),
            include_first_page=document_key not in seen_documents,
            include_section=False,
        )
        context_remaining -= len(prefix)
        remaining -= len(prefix)
        image_ranges = _child_ranges(
            record,
            [a for a in anchors if any(ref.get("category") == "Picture" for ref in getattr(a, "source_record_refs", ()))],
            parent_keys=parent_keys,
        )
        native_ranges = _native_ranges(record)
        verified = sorted({quote_ranges[item] for item in quote_requests.get(key, ()) if item in quote_ranges})
        # CRAGのchild本文を保護し、同じ出典の親本文中の位置と実頁を引き継ぐ。
        pinned = [] if definitions_only else [(a, b) for a, b, _ in located]
        # 原文（解析本文）の短い範囲は質問の種類・画像の有無に関係なく先に確保し、残りの予算で生成説明・OCR を
        # 入れる (#687)。親 6,000 字では画面の生成説明（1 枚 1,000〜3,000 字）が予算を食い、原文の手順行
        # （「③〇〇ボタンを押します。」）が抜粋から落ちて、引用照合やボタン名の検査の根拠を失っていた。
        # 全体の予算は既存の remaining で制限する。
        if not definitions_only:
            pinned.extend((a, b) for a, b in native_ranges if b - a <= 600)
        # 評価が写しを確かめた原文も必須の範囲にする（予算の総額は増やさない）。
        required = sorted(set([*required, *pinned, *verified]))
        needed = sum(b - a for a, b in required) + 8 * max(0, len(required) - 1)
        allowance = min(remaining, max(remaining // max(1, len(ordered) - index), needed))
        if not labels and not anchors:
            allowance = min(remaining, max(1, budget // max(1, len(unique))))
        if verified:
            allowance = min(remaining, max(allowance, sum(b - a for a, b in verified) + 8 * (len(verified) - 1)))
        # 取り置き分を超えない。ただし自分の必須範囲（原文・定義・照合済み child）は確保する。
        allowance = min(allowance, max(needed, remaining - reserve_after[index + 1]))
        if key in priority_keys:
            # 任意の資料の取り置きより、各必須手順の直接の根拠に残予算を公平に配る。
            allowance = min(remaining, max(allowance, remaining // max(1, len(priority_keys) - index)))
        if whole:
            allowance = min(remaining, max(allowance, len(text)))
        # 予算内に全文が入らない親では、指定された子の本文を先に確保する。
        # 任意の親見出しを別窓にすると出典情報の反復で後続の子本文が落ちるため、
        # この親の任意窓は追加しない。子そのものの本文・前提は改変しない。
        child_only = bool(priority_ranges) and not whole
        if child_only:
            allowance = min(allowance, max(0, remaining - priority_extra_cost))
            # 子の本文だけを切り出すときも、評価が写しを確かめた原文は子の本文の後に入れる。
            priority_ranges = [*affordable_priority_ranges, *(item for item in verified if item not in affordable_priority_ranges)]
        if verified and not whole:
            # 写しを含む抜粋ごとに繰り返す出典の分を残す（抜粋の本文だけで残りの予算を使い切らない）。
            allowance = min(
                allowance, max(sum(b - a for a, b in verified) + 8 * (len(verified) - 1), remaining - source_cost * len(verified))
            )
        excerpt = evidence_excerpt(
            question,
            text,
            allowance,
            required_ranges=priority_ranges if child_only else required,
            priority_ranges=priority_ranges,
            required_only=definitions_only or child_only,
        )
        remaining -= len(excerpt)
        cursor = 0
        parts = []
        for part in excerpt.split("\n[…]\n"):
            offset = text.find(part, cursor)
            if offset < 0:
                continue
            boundaries = [*((a, b) for a, b, _ in located), *native_ranges, *((a, b) for a, b, _, _ in scoped)]
            edges = sorted({edge for a, b in boundaries for edge in (a, b) if offset < edge < offset + len(part)})
            deferred = [edge for edge in edges if not _citation_partition_boundary(text, edge)]
            # 評価が写しを確かめた原文の中では切らない（写しが 2 つの span にまたがると、生成の根拠で照合できない）。
            in_quote = [edge for edge in edges if edge not in deferred and any(a < edge < b for a, b in verified)]
            for reason_code, positions in (("mid_sentence_boundary", deferred), ("verified_quote_range", in_quote)):
                if positions and diagnostics is not None:
                    diagnostics.append(
                        {
                            "stage": "boundary_preserved",
                            "reason_code": reason_code,
                            "source_id": getattr(record, "chunk_uid", "") or record.id,
                            "positions": positions,
                        }
                    )
            deferred += in_quote
            cuts = sorted({0, len(part), *(edge - offset for edge in edges if edge not in deferred)})
            parts.extend((offset + a, offset + b) for a, b in zip(cuts, cuts[1:], strict=False))
            cursor = offset + len(part)
        cursor = 0
        # 評価が写しを確かめた原文を含む抜粋。後に続くその抜粋の出典の分を、ほかの抜粋で使い切らない。
        quote_parts = [any(start <= a and b <= end for a, b in verified) for start, end in parts]
        for position, (start, end) in enumerate(parts):
            part = text[start:end]
            if not part:
                continue
            if structural_only(part):
                if diagnostics is not None:
                    diagnostics.append(
                        {
                            "stage": "excluded",
                            "reason_code": "structural_only",
                            "source_id": getattr(record, "chunk_uid", "") or record.id,
                            "start": start,
                            "end": end,
                        }
                    )
                continue
            source_id = getattr(record, "chunk_uid", "") or record.id
            span_id = "E" + hashlib.sha256(f"{source_id}:{start}:{end}:{part}".encode()).hexdigest()[:20]
            pages = {page for a, b, page in located if a <= start and end <= b}
            child_ranges = _overlapping_child_ranges(provenance, start, end)
            # 親の前置きと連結した窓も、含まれるchildが単一頁の場合だけ頁を確定する。
            if not pages:
                pages = {
                    page for a, b, page in located if start <= a and b <= end and text[start:a].strip() == "" and text[b:end].strip() == ""
                }
            result.append(
                {
                    "evidence_id": span_id,
                    "source_id": source_id,
                    "document_scope": list(document_context_key(record)),
                    # 分類は v4 で document 配下 (#814)。
                    "business_scope": str(
                        (((getattr(record, "metadata", {}) or {}).get("document") or {}).get("classification") or {}).get("large_category")
                        or ""
                    ),
                    "source": getattr(record, "source", ""),
                    "source_context": source_info,
                    # 関連度順で選んだ根拠も、生成時は見出しと本文を原文順に読めるようにする。
                    "source_order": (getattr(record, "chunk_seq", 0), getattr(record, "seq_no", 0), start),
                    "page": getattr(record, "page", 0),
                    # 新しい版に置き換えた文書（旧版）の根拠（backend の document_superseded。#1492）。
                    "superseded": (getattr(record, "metadata", {}) or {}).get("document_superseded") is True,
                    # 会話の添付から選んだ根拠（利用者が明示した入力。#3228）。要求の根拠計画の照合に掛けない。
                    "conversation_attachment": is_attachment_evidence(record),
                    # 頁の無い根拠（Excel の行の記録）の場所。出典の行・根拠の見出しは頁の代わりにこれを出す（#1224）。
                    "location": record_location_label(record),
                    # 本文位置から頁を確定できない span は親の先頭頁を page に持つ。親は複数頁にまたがるため、
                    # page だけを見ると span の出どころを実際より狭く見積もる。確定できた span は page_end == page。
                    "page_end": _record_page_end(record),
                    "section_path": (getattr(record, "metadata", {}) or {}).get("section_path", []),
                    # 見出しの出所（v4）。無い旧データでは空で、function_key は全要素を Docling 由来として扱う。
                    "section_path_sources": (getattr(record, "metadata", {}) or {}).get("section_path_sources", []),
                    "start": start,
                    "end": end,
                    "text": part,
                    "excerpt_only": len(excerpt) < len(text),
                    "source_aliases": list(dict.fromkeys(aliases[key])),
                    # 抽出方法とデータの用途は別軸。正確なOCRでも文書の表示例を
                    # 今回の業務レコードの実値へ昇格させない。
                    "value_context": "reference_document",
                    "origin": _span_origin(record, start, end, image_ranges),
                }
            )
            if prefix:
                result[-1]["answer_context"] = prefix
                prefix = ""
                seen_documents.add(document_key)
            if len(pages) == 1:
                result[-1]["page"] = result[-1]["page_end"] = next(iter(pages))
            # 抜粋の範囲に重なる子と、その子の親の本文の中の範囲（#2710）。出典の行を、引用が実際に入る子へ
            # 結ぶのに使う（親の ID だけでは同じ親・同じ頁の兄弟の子を区別できない）。
            if child_ranges:
                result[-1]["child_ranges"] = child_ranges
            # parentの章一覧には隣接する別機能が混在し得る。本文位置と文書・版・
            # 親子関係を照合した単一childだけで、引用そのものの所属を確定する。
            matching = [(a, child) for a, b, _, child in scoped if a <= start and end <= b]
            scopes = {(_local_child_path(child, text[a:start]), child.page) for a, child in matching}
            if len(scopes) == 1:
                path, page = next(iter(scopes))
                if path:
                    result[-1].update(
                        section_path=list(path),
                        page=page,
                        page_end=page,
                        scope_source_ids=sorted({c.chunk_uid or c.id for _, c in matching}),
                    )
            # 親の出典と、その抜粋に実際に重なる子の出典を別々に保持する。
            children = []
            for a, b, _, child in provenance:
                if a < end and start < b:
                    child_info = {"source_id": child.chunk_uid or child.id, **source_context(child)}
                    if child_info not in children:
                        children.append(child_info)
            info = {**source_info, **({"children": children} if children else {})}
            extra_cost = note_cost(info, record) - source_cost
            # 複数の抜粋では、出典を繰り返す分も予算に含める。階層を省略しない。
            if cursor:
                extra_cost += source_cost
            later_quotes = 0 if quote_parts[position] else sum(quote_parts[position + 1 :])
            if extra_cost > remaining - source_cost * later_quotes:
                result.pop()
                continue
            remaining -= extra_cost
            result[-1]["source_context"] = info
            cursor = end
    result = _merge_adjacent_spans(result)
    if diagnostics is not None:
        diagnostics.extend(_verified_quote_outcomes(quote_order, quote_requests, quote_ranges, unique, result))
        for group in priority_source_groups:
            wanted = set(group)
            ranges: list[tuple[str, int, int, str]] = []
            parents = []
            for key, record in ordered:
                if not (member_keys[key] | child_keys[key]) & wanted:
                    continue
                parent_id = str(getattr(record, "chunk_uid", "") or record.id)
                parents.append(parent_id)
                for a, b, child in child_locations[key]:
                    ids = {str(getattr(child, attr, "")) for attr in ("chunk_uid", "chunk_id", "id")}
                    if (member_keys[key] | ids) & wanted:
                        ranges.append((parent_id, a, b, str(getattr(child, "chunk_uid", "") or child.id)))
                if not child_locations[key] and member_keys[key] & wanted:
                    ranges.append((parent_id, 0, len(record.text), ""))
            ranges = list(dict.fromkeys(ranges))
            physical = _union_source_ranges([(parent, a, b) for parent, a, b, _child in ranges])
            required_chars = sum(b - a for _parent, a, b in physical)
            covered = _union_source_ranges(
                [
                    (parent, max(a, span["start"]), min(b, span["end"]))
                    for parent, a, b in physical
                    for span in result
                    if span["source_id"] == parent and max(a, span["start"]) < min(b, span["end"])
                ]
            )
            selected_chars = sum(b - a for _parent, a, b in covered)
            diagnostics.append(
                {
                    "stage": "selected",
                    "requested_source_ids": list(group),
                    "matched_parent_ids": list(dict.fromkeys(parents)),
                    "located_child_ids": list(dict.fromkeys(child for _parent, _a, _b, child in ranges if child)),
                    "body_status": "unlocated"
                    if not ranges
                    else "complete"
                    if selected_chars == required_chars
                    else "partial"
                    if selected_chars
                    else "missing",
                    "selected_chars": selected_chars,
                    "required_chars": required_chars,
                }
            )
    return result


def _verified_quote_outcomes(
    order: Sequence[tuple[str, str]],
    requests: Mapping[str, Sequence[tuple[str, str]]],
    ranges: Mapping[tuple[str, str], tuple[int, int]],
    records: Mapping[str, Any],
    spans: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """評価が写しを確かめた原文が、生成の根拠の span に入ったか（入らなければ理由）を写しごとに返す。本文は含めない。"""
    parent_of = {item: key for key, items in requests.items() for item in items}
    outcomes = []
    for item in order:
        key = parent_of.get(item)
        entry: dict[str, Any] = {"stage": "verified_quote", "source_id": item[0], "quote_chars": len(item[1])}
        if key is None:
            outcomes.append({**entry, "status": "source_not_in_generation_records"})
            continue
        parent_id = str(getattr(records[key], "chunk_uid", "") or records[key].id)
        entry["parent_id"] = parent_id
        if item not in ranges:
            outcomes.append({**entry, "status": "quote_not_in_source_text"})
            continue
        start, end = ranges[item]
        own = [span for span in spans if span["source_id"] == parent_id]
        if any(span["start"] <= start and end <= span["end"] for span in own):
            status = "included"
        elif any(span["start"] < end and start < span["end"] for span in own):
            status = "split_across_spans"
        else:
            status = "excluded_by_budget"
        outcomes.append({**entry, "start": start, "end": end, "status": status})
    return outcomes


def _merge_adjacent_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同じ出典で連続し、由来・頁・所属が同じ抜粋を1つに戻す。

    解析が1文を数字や句ごとの細かい原文範囲へ分けると、境界で切った抜粋が「10」「5」や
    改行だけの断片になり、文をまたぐ引用がどの根拠にも一致しなくなる。由来や頁が変わる
    境界は引用の分類に必要なので残す。空白だけの断片と、同じ所属の本文に挟まれた短い
    未分類の断片（原文範囲の隙間の助詞・単位など）は、前後をつなぐ部分として扱う。
    """
    # page_end も一致条件に入れる。頁を確定できた span（page_end == page）と、親の範囲のままの span を
    # つなぐと、確定していない側まで 1 頁に決まったように見えてしまう。
    same = (
        "source_context",
        "source_id",
        "origin",
        "page",
        "page_end",
        "section_path",
        "excerpt_only",
        "value_context",
        "scope_source_ids",
    )

    def joinable(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return left["end"] == right["start"] and "answer_context" not in right and all(left.get(key) == right.get(key) for key in same)

    def glue(index: int, previous: dict[str, Any]) -> bool:
        span = spans[index]
        if previous["end"] != span["start"] or previous["source_id"] != span["source_id"] or "answer_context" in span:
            return False
        if not span["text"].strip():
            return True
        following = spans[index + 1] if index + 1 < len(spans) else None
        return (
            span.get("origin") == "unclassified"
            and len(span["text"].strip()) <= 8
            and following is not None
            and following["start"] == span["end"]
            and all(previous.get(k) == following.get(k) for k in same)
        )

    merged: list[dict[str, Any]] = []
    for index, span in enumerate(spans):
        previous = merged[-1] if merged else None
        if previous is not None and (joinable(previous, span) or glue(index, previous)):
            text = previous["text"] + span["text"]
            if ranges := _merged_child_ranges(previous.get("child_ranges"), span.get("child_ranges")):
                previous["child_ranges"] = ranges
            previous.update(
                text=text,
                end=span["end"],
                evidence_id="E"
                + hashlib.sha256(f"{previous['source_id']}:{previous['start']}:{span['end']}:{text}".encode()).hexdigest()[:20],
            )
        else:
            merged.append(dict(span))
    return [span for span in merged if span["text"].strip()]


def _overlapping_child_ranges(scoped: Sequence[tuple[int, int, Any, Any]], start: int, end: int) -> list[dict[str, Any]]:
    """抜粋 [start, end) に重なる子の ID と、重なる範囲（親の本文の位置）。親の中の位置の順に並べる（#2710）。"""
    found: dict[str, dict[str, Any]] = {}
    for a, b, _page, child in scoped:
        child_id = getattr(child, "chunk_uid", "") or getattr(child, "id", "")
        if not child_id or a >= end or b <= start:
            continue
        key = str(child_id)
        current = found.get(key)
        if current is None:
            found[key] = {"id": key, "start": max(a, start), "end": min(b, end)}
        else:
            current.update(start=min(current["start"], max(a, start)), end=max(current["end"], min(b, end)))
    return sorted(found.values(), key=lambda item: (item["start"], item["end"], item["id"]))


def _merged_child_ranges(left: Any, right: Any) -> list[dict[str, Any]]:
    """つないだ抜粋の子の範囲（同じ子は範囲をまとめる。#2710）。"""
    merged: dict[str, dict[str, Any]] = {}
    for item in [*(left or ()), *(right or ())]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        key = str(item["id"])
        current = merged.get(key)
        if current is None:
            merged[key] = dict(item)
        else:
            current.update(start=min(current["start"], item["start"]), end=max(current["end"], item["end"]))
    return sorted(merged.values(), key=lambda item: (item["start"], item["end"], item["id"]))


def _local_child_path(child: Any, preceding: str) -> tuple[str, ...]:
    """child内の明示機能見出し以降だけ所属とする。隣接だけでは旧章を除かない。

    章切替直後のchildにも旧章metadataが残るため、本文中の独立した見出し行と
    metadataの完全一致を要求する。名称から操作入口の存在は推論しない。
    """
    from types import SimpleNamespace

    from rag_engine.retrieval.operation_context import operation_labels

    path = tuple((getattr(child, "metadata", {}) or {}).get("section_path", []))

    def compact(value):
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))

    selected = None
    for line in preceding.splitlines():
        for index, label in enumerate(path):
            if (
                compact(line) == compact(label)
                and re.search(rf"[{evidence_cues().chars('function_number_dashes')}]\([0-9A-Z]+\)", compact(label))
                and len(operation_labels(SimpleNamespace(metadata={"section_path": [label]}))) == 1
            ):
                selected = index
    return path[selected:] if selected is not None else path


def _native_ranges(record: Any) -> list[tuple[int, int]]:
    """本文内の有効な原文位置だけを返し、旧metadataでは空にする。"""
    ranges = ((getattr(record, "metadata", {}) or {}).get("layout") or {}).get("native_text_ranges") or []
    return sorted(
        (r["start"], r["end"])
        for r in ranges
        if isinstance(r, dict)
        and type(r.get("start")) is int
        and type(r.get("end")) is int
        and 0 <= r["start"] < r["end"] <= len(record.text)
    )


def _span_origin(record: Any, start: int, end: int, image_ranges: Sequence[tuple[int, int, Any]]) -> str:
    """解析原文の範囲に完全に収まる抜粋だけを文書本文と分類する。"""
    cursor = start
    for a, b in _native_ranges(record):
        if b <= cursor or a >= end:
            continue
        if record.text[cursor : max(cursor, a)].strip():
            break
        cursor = max(cursor, min(end, b))
    if cursor > start and not record.text[cursor:end].strip():
        return "document_text"
    if any(a <= start and end <= b for a, b, _ in image_ranges):
        return "image_extraction"
    return "unclassified"


def _child_ranges(
    parent: Any, anchors: Sequence[Any], *, parent_keys: Collection[str] = (), body_text: bool = False
) -> list[tuple[int, int, Any]]:
    """同文書・同版・親子関係と本文一致を検証してからchildの位置と頁を返す。

    parent_keysは parent にまとめた根拠（#1331）の ID。その child も本文一致を検証してから使う。
    body_text は child の元の本文（``body_text``）で探す（出典の行の子の範囲。#2710）。既定は検索文の本文。
    """
    found = []
    parent_key = getattr(parent, "chunk_uid", "") or parent.id
    for child in anchors:
        key = getattr(child, "parent_chunk_uid", "") or getattr(child, "parent_chunk_id", "")
        if key not in {parent_key, getattr(parent, "chunk_id", ""), *parent_keys}:
            continue
        if (
            not getattr(parent, "source", "")
            or getattr(child, "source", "") != getattr(parent, "source", "")
            or getattr(child, "source_run_id", "") != getattr(parent, "source_run_id", "")
            or (
                getattr(child, "chunk_uid", "")
                and getattr(parent, "chunk_uid", "")
                and child.chunk_uid.partition(":")[0] != parent.chunk_uid.partition(":")[0]
            )
        ):
            continue
        raw = (getattr(child, "body_text", "") or child.text) if body_text else child.text
        body = raw.split("Child text:\n", 1)[-1].strip()
        start = parent.text.find(body) if body else -1
        if start >= 0 and parent.text.find(body, start + 1) < 0:
            found.append((start, start + len(body), getattr(child, "page", 0)))
    return found


def _native_text(record: Any) -> str:
    """解析本文（native_text_ranges）だけをつないだ文字列。旧 metadata で範囲がなければ全文。"""
    ranges = _native_ranges(record)
    text = str(getattr(record, "text", "") or "")
    return "\n".join(text[a:b] for a, b in ranges) if ranges else text


def _mentions(term: str, corpus: str) -> bool:
    """corpus に term が完全一致か略称（文字が順に並ぶ term の 2 倍程度までの範囲）で現れるか (#700 と同じ)。"""
    needle = re.sub(r"\s+", "", unicodedata.normalize("NFKC", term))
    if not needle:
        return False
    if needle in corpus:
        return True
    pattern = (".{0," + str(len(needle) + 2) + "}?").join(re.escape(c) for c in needle)
    return any(len(m.group()) <= len(needle) * 2 + 2 for m in re.finditer(pattern, corpus))


def _question_targets(question: str) -> list[str]:
    """同一ユニット優先の起点を選ぶための質問の対象（操作対象・帳票・画面）。"""
    contract = task_contract(question)
    return [t for t in dict.fromkeys([*contract.get("action_targets", ()), *contract.get("business_objects", ())]) if len(t) >= 2]


def _same_unit_first(ordered: list[tuple[str, Any]], question: str = "") -> list[tuple[str, Any]]:
    """最上位候補と同じ機能ユニットの候補を直後に寄せ、原文予算をまず同じ機能に使う (#665)。

    機能ユニットは section_path の番号付き見出しまで（`chunking._section_unit`）。1 機能が複数の親に
    割れているとき、検索順のままだと別文書・別機能の候補に予算が先に配られ、同じ機能の残りの手順が
    予算外へ押し出される。安定ソートなので、それ以外の順序は変えない。

    起点は、質問の対象（`action_targets` / `business_objects`）を**解析本文**（生成説明を除く）に含む最初の
    候補にする (#730)。画面の生成説明の画面例の値（「出荷基準年月=令和6年9月」）だけが質問の語に一致して
    最上位になった候補を、機能ユニットの起点にしない。該当する候補がなければ従来どおり先頭。
    """
    from rag_engine.chunking import _section_unit

    if not ordered:
        return ordered

    def unit(record: Any) -> tuple[str, ...]:
        path = (getattr(record, "metadata", {}) or {}).get("section_path") or []
        return (str(getattr(record, "source", "")), *_section_unit(path))

    targets = _question_targets(question) if question else []
    anchor = ordered[0][1]
    if targets:
        for _, record in ordered:
            corpus = re.sub(r"\s+", "", unicodedata.normalize("NFKC", _native_text(record)))
            if any(_mentions(t, corpus) for t in targets):
                anchor = record
                break
    top = unit(anchor)
    if len(top) <= 1:
        return ordered
    return sorted(ordered, key=lambda item: unit(item[1]) != top)


def evidence_packet(question: str, records: Sequence[Any], *, budget: int = EVIDENCE_BUDGET_CHARS) -> str:
    """操作単位の原文と安定IDを整形する。全文と版参照は呼出元へ保持する。"""
    return format_evidence_packet(evidence_spans(question, records, budget=budget))


def format_evidence_packet(spans: Sequence[dict[str, Any]]) -> str:
    """選択済みの原文を再切出しせず、計画・生成・監査へ同じIDで渡す。"""
    return "\n\n".join(
        f"Source ID: {span['source_id']}\nEvidence ID: {span['evidence_id']}\n"
        f"Document: {span['source']} / {span.get('location') or 'page ' + str(span['page'])}\n"
        f"Section: {json.dumps(span['section_path'], ensure_ascii=False)}\n"
        f"Origin: {span.get('origin', 'unclassified')}\n"
        f"Value context: {span.get('value_context', 'reference_document')} (not verified as current case data)\n"
        f"Excerpt only: {str(span['excerpt_only']).lower()}\n"
        + (span.get("answer_context", "") + "\n" if span.get("answer_context") else "")
        + span["text"]
        for span in spans
    )


def record_location_label(record: Any) -> str | None:
    """頁の無い根拠の場所の表示（例: 「シート「費目コード」A3:D6」。#1224）。無ければ None。

    backend が Excel の行の記録（#1221）の chunk の場所を ``metadata["sheet_location"]`` に入れる。
    XML ツリー JSON の段落（#2024）は ``metadata["location_label"]``（引用ラベル）に入れる。
    """
    metadata = getattr(record, "metadata", {}) or {}
    label = metadata.get("location_label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    location = metadata.get("sheet_location")
    if not isinstance(location, dict):
        return None
    sheet = str(location.get("sheet_name") or "").strip()
    if not sheet:
        return None
    cell_range = str(location.get("cell_range") or "").strip()
    from rag_engine.generation.answer_labels import answer_labels

    return answer_labels().sheet(sheet, cell_range)
