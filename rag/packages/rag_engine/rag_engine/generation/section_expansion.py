"""当たった chunk の章節から、親の章節へ段階的に広げて回答の文脈に入れる（#1436）。

章節の正本は文書の「章節ナビゲーション」で、章節と chunk の対応（#1438）は呼び出し元（backend）が持つ。この module は
対応を ``AnswerDependencies.section_outline`` / ``section_chunks`` から読み、広げる順・予算・止める条件だけを決める
（I/O と LLM は注入された関数と ``grade`` を通す。決定論のテストはスタブで行う）。

広げる順は、当たった chunk → 所有の章節（leaf）→ 親の章節 → … → 最上位の章節 → 文書全体（根の章節）。

- 最初の判定が十分でも、最低 1 段（所有の章節）を読んでから、既存の CRAG の十分性の判定（``grade``）で
  足りるかを確かめ、足りれば止める。所有の章節がすでに全量文脈にあれば、再判定せず最後の判定を使う。
  足りるは「十分と判定され、内容の観点（``CONTENT_ASPECTS``）に
  確かめられていないものが無い」。適用の観点（applicability）は利用者の状況の確認で、章節を読んでも埋まらないため
  広げる理由にしない。
- 列挙・網羅の問（#1503。``retrieval.enumeration``）は、件数・章の範囲の欠けを評価器の項目ごとの根拠から数え、
  欠ければ内容の観点 ``enumeration_coverage`` が確かめられていないとして広げる。件数も範囲も無い網羅の問
  （「〜要領で、…をすべて挙げて」）は根拠の数から足りるかを決められないため、評価器が足りると判定しても、
  読む範囲（``minimum_scope``。当たった章節 = leaf、文書全体 = 根の章節）まで読んでから判定する。読む範囲に
  届くまでの段は評価器を呼ばない（段ごとの LLM の呼び出しを増やさない）。上限（段・token・時間）は変えない。
- 上の段の章節は、予算に入れば全体を読む。入らなければ、子の章節の見出し（目次）を質問との関連度（rerank。使えなければ
  語の重なり）で並べ、関連の高い子の章節から、予算に入る章節を全体で読む。入らない子の章節は読まない。
- 章節が予算に入るかは、章節の chunk のうち、まだ文脈に無いものの token で決める（最初の根拠・前の段で読んだ
  chunk は数えない。#1504）。章節の chunk がすべて文脈にあれば、読むものは無い（診断の ``read: "in_context"``）。
- 全体を読む必要があるのに予算に入らない章節は、第 3 段階（#1437。抽出してから統合）の差し込み口
  （``large_sections``。``section_extraction.SectionExtractor``）に渡し、質問に関係する原文だけを抜き出して文脈に
  入れる。入らない子の章節が出たら、それより関連の低い子の章節もまとめて 1 つの要求で渡す。要求には、すでに文脈に
  ある章節の親（最初の根拠を含む）も入れ、その分を抜き出した原文の予算に戻す（抜き出した原文で置き換える。予算に
  入れば元のまま残す。#1504）。差し込み口が無ければ予算で打ち切る。
- 上限（入力 token・段の数・時間）で止めたときは、読めなかった章節を返し、呼び出し元は回答を条件つき
  （``conditional``）にして「回答に未反映の関連章節」に出す。ただし、当たった章節より上の段で子の章節を読むときに、
  関連度の順でより上の兄弟の章節を全体で読んだ後に予算で読めなかった章節のうち、どの起点の chunk も含まないものは、
  読む範囲が文書全体でなければ回答に出さず、診断（``unread_omitted``）にだけ残す（#2816。質問に合わせて広げた参考の
  範囲で、読めなくても当たった章節の読了は崩れない）。読取の失敗は、どの章節でも回答に出す。
- 1 回の回答の中で後に別の起点から広げる段（業務ガイドの手順ごとの検索の後など）があるときは、呼び出し元が
  ``reserve_tokens`` で残す token を渡し、先の段はその分を使わずに止める（#2816）。後の段は、前の段で起点に
  しなかった起点の章節を先に広げる。
- 呼び出し元が先に読む範囲（``priority``。業務ガイドの手順の参照の文書と頁）を渡したら、参照の頁に重なる起点 →
  参照の文書の起点 → その他の起点の順に広げる（#2828）。参照の文書の起点があれば、その他の文書の起点は段の外に
  置き（親へ上がらない）、所有の章節だけを止めた後に残りの予算で読む。読めなくても回答に出さず、診断
  （``unread_omitted``）にだけ残す（質問と関係の弱い別の文書の章節が予算と「回答に未反映の関連章節」を占めない）。
- 「回答に未反映の関連章節」に出す行は、回答の生成の後に ``answer_unread_view`` で、回答が根拠にした文書と先に読む
  範囲の文書の章節に絞り、``MAX_UNREAD_LINES`` 行を超えた分は件数の 1 行にする（#2828）。

入力 token の上限は、回答のモデルの入力の上限（共通のモデル設定の登録モデルの「入力の上限（tokens）」。#1437。
backend が platform の ``enterprise_ai_max_input_tokens`` で決めて ``answer_max_input_tokens`` で渡す）に対する割合
（既定 50%）。根拠の本文の token は ``estimate_tokens``（platform の ``estimate_input_tokens`` と同じ見積もり）で
数える。文脈の並びは、もとの根拠（関連度の順）を先頭に、広げた章節をその後ろに章節の中の読み順で置く。引用の ID は
chunk のまま（章節の chunk は検索の chunk と同じ親子の record にする）。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any

from rag_engine.dependencies import report_degraded
from rag_engine.generation.answer_models import AnswerContext, CragRetrievalAttempt
from rag_engine.generation.answer_records import AnswerRecord, _stored_chunk_answer_record
from rag_engine.resources.deadline import ANSWER_FINALIZE_RESERVE_SECONDS, DEFAULT_GENERATION_ROUND_SECONDS
from rag_engine.retrieval.context_builder import (
    ContextChildEvidence,
    ContextParentEvidence,
    context_bundle_from_parent_evidence,
)
from rag_engine.retrieval.enumeration import ENUMERATION_ASPECT, minimum_scope

# 根の章節（文書全体）の id。backend の ``section_chunk_map.ROOT_SECTION_ID`` と同じ。
ROOT_SECTION_ID = "document"
# 広げた章節の根拠の役割（context の parent の理由と child の役割）。
SECTION_EXPANSION_REASON = "section_expansion"
SECTION_CONTEXT_ROLE = "section_context"
# 「足りる」の判定で、確かめられていないと広げる内容の観点（``task_contract`` の required_aspects のうち）。
CONTENT_ASPECTS = frozenset({"requested_result", "procedure", "member_details", "population_membership", ENUMERATION_ASPECT})
# 1 つの章節で読む chunk の上限（文書全体でも、これより多い章節は予算に関係なく読まない）。
MAX_SECTION_CHUNKS = 400
# モデルの入力の上限の既定（tokens。platform の DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS と同じ。#1437）。
DEFAULT_MODEL_INPUT_TOKENS = 128_000
# 回答の生成に残す秒数。これより残りが少なければ上がらない。生成と監査の 1 組の見積もりの既定と回答の確定の分の和
# （#1516。150 秒は、長い根拠で 1 組 125〜344 秒かかるモデルの 1 組目に足りなかった）。是正の round は、残りの時間と
# 直前の組の実測から grounded の `RoundBudget` が決める。
GENERATION_RESERVE_SECONDS = DEFAULT_GENERATION_ROUND_SECONDS + ANSWER_FINALIZE_RESERVE_SECONDS
# 次の段の見積もりの秒数（評価器の 1 回の実測が無いとき）。
DEFAULT_LEVEL_SECONDS = 20.0
_ASCII = re.compile(r"[\x00-\x7f]")

# 止めた理由。LIMIT_STOPS は上限で止めた（読めなかった章節があれば条件つきの回答にする）。
STOP_SUFFICIENT = "sufficient"
STOP_DOCUMENT = "document_read"
STOP_MAX_LEVELS = "max_levels"
STOP_TOKEN_BUDGET = "token_budget"
STOP_TIME_LIMIT = "time_limit"
STOP_NO_SECTIONS = "no_sections"
STOP_GRADE_ERROR = "grade_error"
STOP_READ_ERROR = "read_error"
STOP_PARTIAL = "partial_read"
STOP_DISABLED = "disabled"
# 列挙の問の読む範囲（文書全体）まで読み、評価し直しを省いて止めた（#1528。``section_skip_regrade_at_scope``）。
STOP_SCOPE_READ = "scope_read"
# 足りると判定したが列挙の観点が未確認（写しの照合の失敗など）のため、評価し直さずに上の章節を 1 段読んで止めた（#3755）。
STOP_UNVERIFIED_EXPANDED = "unverified_expanded"
# 抽出してから統合（#1437）で読めなかった範囲の理由（章節の拡張の止めた理由には使わない）。
STOP_EXTRACTION_CALLS = "extraction_calls"
STOP_EXTRACTION_ERROR = "extraction_error"
STOP_SECTION_CHUNKS = "section_chunks"
LIMIT_STOPS = frozenset({STOP_MAX_LEVELS, STOP_TOKEN_BUDGET, STOP_TIME_LIMIT})
# 「回答に未反映の関連章節」に出す行の上限（超えた分は件数の 1 行にする。#2828）。
MAX_UNREAD_LINES = 10
# 「回答に未反映の関連章節」の各行の理由（利用者の言葉。#2040）。段・入力・抽出の呼び出し・chunk の上限は
# 利用者から見て同じ意味なので「読み込み量の上限」にまとめる。上限の区別は工程の記録（``_SECTION_STOP_LABELS``）と
# 回答の診断の ``section_expansion.unread[].reason`` に残す。
_READ_LIMIT_LABEL = "読み込み量の上限"
_LIMIT_LABELS = {
    STOP_MAX_LEVELS: _READ_LIMIT_LABEL,
    STOP_TOKEN_BUDGET: _READ_LIMIT_LABEL,
    STOP_TIME_LIMIT: "回答時間の上限",
    STOP_EXTRACTION_CALLS: _READ_LIMIT_LABEL,
    STOP_SECTION_CHUNKS: _READ_LIMIT_LABEL,
}


@dataclass(frozen=True)
class SectionInfo:
    """章節の一覧の 1 つ（根の章節を含む）。``chars`` は章節に入る chunk の本文の文字数（子の章節の分を含む）。"""

    section_id: str
    title: str
    level: int
    parent_section_id: str | None
    page_start: int | None = None
    page_end: int | None = None
    chunk_count: int = 0
    chars: int = 0


@dataclass(frozen=True)
class SectionOutline:
    """chunk の文書（chunk_set）の章節の一覧と、その chunk の所有の章節。"""

    document_id: str
    chunk_set_id: str
    owner_section_id: str
    sections: tuple[SectionInfo, ...]
    file_name: str = ""

    def get(self, section_id: str | None) -> SectionInfo | None:
        return next((item for item in self.sections if item.section_id == section_id), None) if section_id else None

    def parent(self, section: SectionInfo) -> SectionInfo | None:
        return self.get(section.parent_section_id)

    def children(self, section_id: str) -> list[SectionInfo]:
        return [item for item in self.sections if item.parent_section_id == section_id]

    def chain(self) -> list[SectionInfo]:
        """所有の章節から根の章節までの列（所有の章節が無ければ空）。"""
        chain: list[SectionInfo] = []
        current = self.get(self.owner_section_id)
        while current is not None and current not in chain:
            chain.append(current)
            current = self.parent(current)
        return chain

    def path(self, section: SectionInfo) -> list[str]:
        """根の下から章節までの名前の列（根の章節は空）。"""
        titles: list[str] = []
        current: SectionInfo | None = section
        while current is not None and current.section_id != ROOT_SECTION_ID and len(titles) < 32:
            titles.append(current.title)
            current = self.parent(current)
        return list(reversed(titles))


@dataclass(frozen=True)
class UnreadSection:
    """上限で読めなかった章節（「回答に未反映の関連章節」に出す）。"""

    document_id: str
    section_id: str
    label: str
    reason: str
    # 章節（範囲）の始まりの頁。画面が各行から原文を開くのに使う（#2040。頁の無い文書は None）。
    page: int | None = None

    def gap_line(self) -> str:
        """本文の行。影響（回答に未反映）と理由を書き言葉で述べる（#2040）。"""
        if self.reason in {STOP_EXTRACTION_ERROR, STOP_READ_ERROR}:
            return f"{self.label}は、原文を取得できなかったため未反映です。"
        if self.reason == STOP_PARTIAL:
            return f"{self.label}は、原文の一部だけを取得したため未確認の範囲が残っています。"
        return f"{self.label}は、{_LIMIT_LABELS.get(self.reason, _READ_LIMIT_LABEL)}に達したため未反映です。"

    def to_payload(self) -> dict[str, Any]:
        # line は本文の行と同じ文。画面は本文の行をこれで引き当て、その行に原文を開く操作を付ける（#2040）。
        return {
            "document_id": self.document_id,
            "section_id": self.section_id,
            "label": self.label,
            "reason": self.reason,
            "page": self.page,
            "line": self.gap_line(),
        }


@dataclass(frozen=True)
class PriorityRange:
    """先に読む範囲（業務ガイドの手順の参照の文書と頁。#2828）。頁が無ければ文書の全体。"""

    document_id: str
    page_start: int | None = None
    page_end: int | None = None


# 起点の順位（小さいほど先に読む）。参照の頁に重なる・参照の文書・その他。
_RANK_PAGES, _RANK_DOCUMENT, _RANK_OTHER = 0, 1, 2


def _priority_rank(outline: SectionOutline, priority: Sequence[PriorityRange]) -> int:
    """起点の所有の章節が、先に読む範囲のどれに当たるか（``_RANK_*``）。"""
    owner = outline.get(outline.owner_section_id)
    ranges = [item for item in priority if item.document_id == outline.document_id]
    if not ranges:
        return _RANK_OTHER
    start = owner.page_start if owner is not None else None
    end = (owner.page_end or start) if owner is not None else None
    for item in ranges:
        if item.page_start is None:
            return _RANK_PAGES
        if start is not None and end is not None and start <= (item.page_end or item.page_start) and item.page_start <= end:
            return _RANK_PAGES
    return _RANK_DOCUMENT


@dataclass(frozen=True)
class UnreadView:
    """回答の本文に出す「回答に未反映の関連章節」の行と、出さなかった章節（#2828）。"""

    lines: tuple[str, ...] = ()
    # 回答が根拠にした文書でも先に読む範囲の文書でもないため出さなかった章節。
    hidden: tuple[UnreadSection, ...] = ()
    # 行の上限を超え、件数の 1 行にまとめた章節の数。
    overflow: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {"shown": len(self.lines), "hidden": [item.to_payload() for item in self.hidden], "overflow": self.overflow}


def answer_unread_view(
    unread: Sequence[UnreadSection | str],
    cited_documents: Collection[str],
    kept_documents: Collection[str] = (),
    *,
    max_lines: int = MAX_UNREAD_LINES,
) -> UnreadView:
    """読めなかった章節から、回答の本文に出す行を決める（#2828）。

    回答が根拠にした文書（``cited_documents``）か先に読む範囲の文書（``kept_documents``）の章節だけを出す。どちらも
    無ければ（根拠を示せなかった回答）絞らない。文の行（文書の分からない行）はそのまま出す。``max_lines`` を超えた
    分は「ほか N 件」の 1 行にする。
    """
    focus = {str(item) for item in (*cited_documents, *kept_documents) if item}
    lines: list[str] = []
    hidden: list[UnreadSection] = []
    for item in unread:
        if isinstance(item, UnreadSection):
            if focus and item.document_id not in focus:
                hidden.append(item)
                continue
            line = item.gap_line()
        else:
            line = str(item or "")
        if line and line not in lines:
            lines.append(line)
    limit = max(1, int(max_lines))
    overflow = max(0, len(lines) - limit)
    if overflow:
        lines = [*lines[:limit], f"ほか {overflow} 件の関連章節も未反映です。"]
    return UnreadView(lines=tuple(lines), hidden=tuple(hidden), overflow=overflow)


class SectionRead(list[Any]):
    """章節の原文と読取結果。空・権限による欠落・途中までの取得を全量と区別する。"""

    def __init__(self, chunks: Sequence[Any] = (), *, complete: bool, reason: str = "") -> None:
        super().__init__(chunks)
        self.complete = complete
        self.reason = reason


@dataclass(frozen=True)
class SectionExpansionLimits:
    """拡張の上限（検索方法の全体設定で決める）。``input_tokens`` は根拠の本文の入力 token の上限。"""

    max_levels: int
    input_tokens: int
    # 後に別の起点から広げる段があるとき、先の段で使わずに残す token（``input_tokens`` に対する割合から。#2816）。
    followup_reserve_tokens: int = 0

    @classmethod
    def from_settings(cls, settings: Any) -> SectionExpansionLimits:
        percent = max(1, min(100, int(getattr(settings, "section_expansion_input_token_percent", 50) or 50)))
        input_tokens = model_input_tokens(settings) * percent // 100
        reserve = max(0, min(50, int(getattr(settings, "section_expansion_followup_reserve_percent", 20) or 0)))
        return cls(
            max_levels=max(0, int(getattr(settings, "section_expansion_max_levels", 4))),
            input_tokens=input_tokens,
            followup_reserve_tokens=input_tokens * reserve // 100,
        )


@dataclass
class SectionExpansionResult:
    """拡張の結果。``unread`` は上限で読めなかった章節（空なら条件つきにしない）。"""

    context: AnswerContext
    applied: bool = False
    stop_reason: str = ""
    levels: int = 0
    used_tokens: int = 0
    unread: list[UnreadSection] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    # 予算で読めなかった、起点を含まない兄弟の章節（回答に出さず診断にだけ残す。#2816）。
    omitted: list[UnreadSection] = field(default_factory=list)

    @property
    def gap_lines(self) -> list[str]:
        return [item.gap_line() for item in self.unread]


@dataclass(frozen=True)
class LargeSectionRequest:
    """全体を読む必要があるのに予算に入らない章節（第 3 段階 #1437 の抽出してから統合に渡す）。

    ``tree`` は章節の親（章節の中の読み順）。まだ文脈に無い親に加えて、すでに文脈にある章節の親（最初の根拠・前の段で
    読んだ親。``context_keys`` の chunk）も含む（#1504。抜き出した原文で置き換えられるので、同じ原文を二重に数えない）。
    ``remaining_tokens`` は抜き出した原文に使える入力 token（根拠の入力の上限の残りに、``context_keys`` の親の分を
    戻した値）。抽出の 1 回の呼び出しの入力は、これとは別にモデルの入力の上限から決める（``section_extraction``）。
    ``sections`` は読めなかったときに出す章節（既定は ``section``。子の章節をまとめて渡すときは、その子の章節）、
    ``seed_positions`` は同じ文書の当たった chunk の位置（``seq_no``。近い分から先に読む）、``seconds_left`` は抽出に使える残りの秒数
    （回答の時間の上限から、生成と次の判定の分を引いた値）、``truncated`` は章節の chunk が読む上限を超えて
    先頭だけを読んだこと。
    """

    question: str
    outline: SectionOutline
    section: SectionInfo
    tree: tuple[ContextParentEvidence, ...]
    remaining_tokens: int
    seed_positions: tuple[int, ...] = ()
    seconds_left: Callable[[], float] = lambda: math.inf
    truncated: bool = False
    context_keys: frozenset[str] = frozenset()
    sections: tuple[SectionInfo, ...] = ()

    @property
    def unread_sections(self) -> tuple[SectionInfo, ...]:
        return self.sections or (self.section,)


@dataclass
class LargeSectionOutcome:
    """抽出してから統合の結果。``tree`` は文脈に足す根拠、``unread`` は上限で読めなかった範囲。

    ``replacements`` は、すでに文脈にある親（``LargeSectionRequest.context_keys``）のうち、抜き出した原文の親に
    置き換えるもの（chunk の ID → 置き換える record。None は文脈から外す）。含まれない親は文脈のまま残す。
    """

    tree: tuple[ContextParentEvidence, ...] = ()
    unread: list[UnreadSection] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    replacements: dict[str, AnswerRecord | None] = field(default_factory=dict)


# (章節の要求) を受け取り、抜き出した根拠（読めなかった範囲つき）を返す。None は読まない（予算で打ち切る）。
LargeSectionHandler = Callable[[LargeSectionRequest], LargeSectionOutcome | None]


def no_large_section_handler(request: LargeSectionRequest) -> LargeSectionOutcome | None:
    """抽出してから統合を使わない差し込み口。読まずに None を返し、呼び出し元は予算で打ち切る。"""
    del request
    return None


def estimate_tokens(text: str) -> int:
    """本文の入力 token の見積もり（platform の ``estimate_input_tokens`` と同じ。#1437）。

    モデルごとの tokenizer を持たないため多めに見積もる: ASCII は 4 字で 1 token、それ以外（かな・漢字など）は
    1 字で 1 token。見積もりは文字数を超えない。
    """
    ascii_count = len(_ASCII.findall(text))
    return (len(text) - ascii_count) + math.ceil(ascii_count / 4)


def model_input_tokens(settings: Any) -> int:
    """回答のモデルの入力の上限（tokens。#1437。共通のモデル設定の登録モデルの値。無ければ既定の 128000）。"""
    try:
        value = int(getattr(settings, "answer_max_input_tokens", DEFAULT_MODEL_INPUT_TOKENS) or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else DEFAULT_MODEL_INPUT_TOKENS


def context_tokens(context: AnswerContext) -> int:
    """文脈の根拠の本文（親の本文）の見積もりの token。"""
    return sum(estimate_tokens(str(getattr(record, "text", "") or "")) for record in context.records)


def is_enough(attempt: CragRetrievalAttempt | None) -> bool:
    """CRAG の判定で足りるか（十分で、内容の観点に確かめられていないものが無い）。"""
    if attempt is None or attempt.grade_error:
        return False
    return bool(attempt.sufficient) and not (set(attempt.missing_aspects) & CONTENT_ASPECTS)


def unverified_enumeration(attempt: CragRetrievalAttempt | None) -> bool:
    """足りると判定したが、列挙の観点が未確認（``unknown``）か（#3755）。

    未確認は不足ではない（足りないと確かめていない）が、足りるとも確かめていない。章節の拡張では、評価器を
    呼び直さずに上の章節を 1 段だけ読む理由にする（足りるとして止めない）。
    """
    return is_enough(attempt) and ENUMERATION_ASPECT in tuple(getattr(attempt, "unknown_aspects", ()) or ())


def section_label(outline: SectionOutline, section: SectionInfo) -> str:
    """利用者向けの章節の表記（例: 「点検要領.pdf」の「第3章 設備ごとの点検」（p.5〜7））。"""
    document = f"「{outline.file_name}」" if outline.file_name else "文書"
    path = outline.path(section)
    name = f"の「{' > '.join(path)}」" if path else "の全体"
    pages = ""
    if section.page_start:
        end = section.page_end or section.page_start
        pages = f"（p.{section.page_start}）" if end == section.page_start else f"（p.{section.page_start}〜{end}）"
    return f"{document}{name}{pages}"


def _record_key(record: Any) -> str:
    return str(getattr(record, "chunk_uid", "") or getattr(record, "id", "") or "")


def _context_chunk_ids(context: AnswerContext) -> set[str]:
    ids: set[str] = set()
    for parent in context.evidence_tree:
        ids.add(_record_key(parent.record))
        ids.update(str(cid) for cid in getattr(parent.record, "child_chunk_ids", ()) or ())
        ids.update(_record_key(child.record) for child in parent.children)
    for record in context.records:
        ids.add(_record_key(record))
        ids.update(str(cid) for cid in getattr(record, "child_chunk_ids", ()) or ())
    ids.discard("")
    return ids


def seed_chunk_ids(context: AnswerContext, relevant_ids: Sequence[str] = ()) -> list[str]:
    """起点にする当たった chunk（関連度の順。CRAG が関連とした child を先に）。"""
    relevant = [uid for uid in dict.fromkeys(relevant_ids) if uid]
    anchors: list[str] = []
    for parent in context.evidence_tree:
        for child in parent.children:
            uid = _record_key(child.record)
            if uid and child.role == "retrieved_anchor":
                anchors.append(uid)
    known = {_record_key(child.record) for parent in context.evidence_tree for child in parent.children}
    ordered = [uid for uid in relevant if uid in known] + anchors
    return list(dict.fromkeys(ordered))


@dataclass
class _Chain:
    """1 つの起点の章節から根までの列と、読んだ段。"""

    outline: SectionOutline
    sections: list[SectionInfo]
    position: int = -1  # 読み終えた段（-1 は当たった chunk だけ）
    seed_uid: str = ""  # 起点の当たった chunk

    @property
    def next_section(self) -> SectionInfo | None:
        return self.sections[self.position + 1] if self.position + 1 < len(self.sections) else None


@dataclass
class SectionReader:
    """章節を読み、文脈に足す record を作る（読んだ chunk・章節を覚える）。"""

    read_chunks: Callable[..., Sequence[Any]]
    known_ids: set[str]
    read_sections: set[tuple[str, str, str]] = field(default_factory=set)
    known_texts: dict[str, str] = field(default_factory=dict)
    # 章節ごとの親（読み順。文脈にあるかに関係なく全部）。同じ章節を 2 回読まない。
    _groups: dict[tuple[str, str, str], list[ContextParentEvidence]] = field(default_factory=dict)
    failures: dict[tuple[str, str, str], str] = field(default_factory=dict)
    # 前の呼び出しで起点にした章節（文書・chunk_set・所有の章節）。後の呼び出しは新しい起点を先に広げる（#2816）。
    seeded_owners: set[tuple[str, str, str]] = field(default_factory=set)

    def section_key(self, outline: SectionOutline, section: SectionInfo) -> tuple[str, str, str]:
        return outline.document_id, outline.chunk_set_id, section.section_id

    def unread_chars(self, outline: SectionOutline, section: SectionInfo) -> int:
        """読む前の見積もり（章節の文字数から、読み終えた子孫の章節の分を引く）。"""
        done = [
            item
            for item in outline.sections
            if self.section_key(outline, item) in self.read_sections and _is_descendant(outline, item, section)
        ]
        top = [item for item in done if not any(other is not item and _is_descendant(outline, item, other) for other in done)]
        return max(0, section.chars - sum(item.chars for item in top))

    def groups(self, outline: SectionOutline, section: SectionInfo) -> list[ContextParentEvidence]:
        """章節の chunk を読み、親ごとにまとめて章節の中の読み順で返す（文脈にある親も含む。child は章節の中の順）。"""
        key = self.section_key(outline, section)
        if key in self._groups:
            return self._groups[key]
        try:
            response = self.read_chunks(document_id=outline.document_id, chunk_set_id=outline.chunk_set_id, section_id=section.section_id)
            chunks = list(response)
            count = sum(getattr(chunk, "chunk_level", "") != "parent" for chunk in chunks)
            if not chunks or not getattr(response, "complete", count >= section.chunk_count):
                self.failures[key] = getattr(response, "reason", "") or STOP_PARTIAL
        except Exception as exc:  # noqa: BLE001 - 読取失敗を未確認として残して回答を続ける
            if type(exc).__name__ == "DeadlineExceeded":
                raise
            chunks = []
            self.failures[key] = STOP_READ_ERROR
        records = [_stored_chunk_answer_record(chunk) for chunk in chunks]
        parents = {_record_key(r): r for r in records if getattr(r, "chunk_level", "") == "parent"}
        grouped: dict[str, list[AnswerRecord]] = {}
        for record in records:
            if getattr(record, "chunk_level", "") == "parent":
                continue
            group_key = str(getattr(record, "parent_chunk_uid", "") or "") or _record_key(record)
            grouped.setdefault(group_key, []).append(record)
        tree = [
            ContextParentEvidence(
                record=parents.get(group_key) or children[0],
                role="synthesis_parent" if group_key in parents else "child_fallback",
                reason=SECTION_EXPANSION_REASON,
                children=tuple(
                    ContextChildEvidence(
                        record=child,
                        role=SECTION_CONTEXT_ROLE,  # type: ignore[arg-type]
                        reason=SECTION_EXPANSION_REASON,
                    )
                    for child in children
                ),
            )
            for group_key, children in grouped.items()
        ]
        self._groups[key] = tree
        return tree

    def read(self, outline: SectionOutline, section: SectionInfo) -> list[ContextParentEvidence]:
        """章節の親のうち、まだ文脈に無いものを章節の中の読み順で返す。"""
        return [
            item
            for item in self.groups(outline, section)
            if str(getattr(item.record, "text", "") or "") not in self.known_texts.get(_record_key(item.record), "")
        ]

    def mark(self, outline: SectionOutline, section: SectionInfo, tree: Sequence[ContextParentEvidence]) -> None:
        self.read_sections.add(self.section_key(outline, section))
        for item in tree:
            self.known_ids.add(_record_key(item.record))
            self.known_texts[_record_key(item.record)] = str(getattr(item.record, "text", "") or "")
            self.known_ids.update(_record_key(child.record) for child in item.children)


def _is_descendant(outline: SectionOutline, item: SectionInfo, ancestor: SectionInfo) -> bool:
    current = outline.parent(item)
    while current is not None:
        if current.section_id == ancestor.section_id:
            return True
        current = outline.parent(current)
    return False


def _tree_tokens(tree: Sequence[ContextParentEvidence]) -> int:
    return sum(estimate_tokens(str(getattr(item.record, "text", "") or "")) for item in tree)


def _fit_tree(tree: Sequence[ContextParentEvidence], remaining: int) -> tuple[list[ContextParentEvidence], bool]:
    """読み順の先頭から予算に入る親だけを残す（全部入れば True）。"""
    kept: list[ContextParentEvidence] = []
    used = 0
    for item in tree:
        cost = estimate_tokens(str(getattr(item.record, "text", "") or ""))
        if used + cost > remaining:
            return kept, False
        kept.append(item)
        used += cost
    return kept, True


def _rank_children(
    question: str, outline: SectionOutline, children: Sequence[SectionInfo], rerank: Callable[..., Any] | None, settings: Any
) -> tuple[list[SectionInfo], str]:
    """子の章節を見出しと質問の関連度で並べる（rerank。使えなければ語の重なり）。"""
    if len(children) <= 1:
        return list(children), "single"
    documents = [" > ".join(outline.path(child)) or child.title for child in children]
    if rerank is not None:
        try:
            ranked = rerank(question, documents, settings, top_n=len(documents))
            order = [int(getattr(item, "index", item)) for item in ranked or ()]
            order = [index for index in dict.fromkeys(order) if 0 <= index < len(children)]
            if order:
                rest = [index for index in range(len(children)) if index not in order]
                return [children[index] for index in [*order, *rest]], "rerank"
        except Exception as exc:  # noqa: BLE001 - 並びは補助。語の重なりで並べる
            # ``ranking: lexical`` は rerank が無いときと同じなので、失敗を呼び出し元の診断に残す（#3286）。
            report_degraded("section_child_rerank", exc)
    from rag_engine.retrieval.evidence_selection import evidence_relevance

    scored = sorted(range(len(children)), key=lambda index: (-evidence_relevance(question, documents[index]), index))
    return [children[index] for index in scored], "lexical"


def _merge(context: AnswerContext, tree: Sequence[ContextParentEvidence]) -> AnswerContext:
    """もとの根拠の後ろに、章節の根拠を読み順で足す。"""
    if not tree:
        return context
    replacements = {
        _record_key(item.record): item.record
        for item in tree
        if any(_record_key(record) == _record_key(item.record) for record in context.records)
    }
    if replacements:
        context = _replace_records(context, replacements)
        by_key = {_record_key(item.record): item for item in tree}
        merged_tree = []
        for parent in context.evidence_tree:
            extra = by_key.get(_record_key(parent.record))
            children = {_record_key(child.record): child for child in extra.children} if extra else {}
            children.update({_record_key(child.record): child for child in parent.children})
            merged_tree.append(replace(parent, children=tuple(children.values())))
        context = replace(context, evidence_tree=tuple(merged_tree))
        existing = {_record_key(record) for record in context.records}
        tree = tuple(item for item in tree if _record_key(item.record) not in existing)
    bundle = context_bundle_from_parent_evidence(
        tuple(tree), max_chars=max([len(str(getattr(item.record, "text", "") or "")) for item in tree] + [1])
    )
    if not bundle.records:
        return context
    return replace(
        context,
        records=[*context.records, *bundle.records],
        text="\n\n".join(part for part in (context.text, bundle.text) if part.strip()),
        evidence=(*context.evidence, *bundle.evidence),
        evidence_tree=(*context.evidence_tree, *bundle.evidence_tree),
    )


def _replace_records(context: AnswerContext, replacements: dict[str, AnswerRecord | None]) -> AnswerContext:
    """文脈の親を、抜き出した原文の親に置き換える（None は外す。#1504）。位置・役割・child はそのまま。"""
    if not replacements:
        return context

    def swap(record: Any) -> Any:
        key = _record_key(record)
        return replacements.get(key, record)

    tree = tuple(replace(item, record=new) for item in context.evidence_tree if (new := swap(item.record)) is not None)
    records = [new for record in context.records if (new := swap(record)) is not None]
    evidence = tuple(replace(item, record=new) for item in context.evidence if (new := swap(item.record)) is not None)
    text = (
        context_bundle_from_parent_evidence(
            tree, max_chars=max([len(str(getattr(item.record, "text", "") or "")) for item in tree] + [1])
        ).text
        if tree
        else ""
    )
    return replace(
        context,
        records=records,
        evidence=evidence,
        evidence_tree=tree,
        text=text or "\n\n".join(str(getattr(record, "text", "") or "") for record in records),
    )


def expand_sections(
    question: str,
    context: AnswerContext,
    settings: Any,
    *,
    last_attempt: CragRetrievalAttempt | None,
    grade: Callable[[AnswerContext, int], CragRetrievalAttempt] | None,
    section_outline: Callable[..., Any],
    section_chunks: Callable[..., Sequence[Any]],
    rerank: Callable[..., Any] | None = None,
    large_sections: LargeSectionHandler = no_large_section_handler,
    started_at: float | None = None,
    deadline_remaining: Callable[[], float | None] = lambda: None,
    clock: Callable[[], float] = monotonic,
    reader: SectionReader | None = None,
    reserve_tokens: int = 0,
    priority: Sequence[PriorityRange] = (),
) -> SectionExpansionResult:
    """当たった chunk の章節から段階的に広げる。grade が None なら最低範囲の補完だけを行う。

    ``grade(context, level)`` は既存の CRAG の十分性の判定、``started_at`` は回答の開始の時刻（``clock`` の値）、
    ``deadline_remaining`` は回答の期限までの残りの秒数（無ければ None）。``reserve_tokens`` は、後の段のために
    この呼び出しで使わずに残す入力 token（#2816）。``priority`` は先に読む範囲（業務ガイドの手順の参照。#2828）。
    """
    limits = SectionExpansionLimits.from_settings(settings)
    reserved = max(0, min(int(reserve_tokens or 0), limits.input_tokens))
    budget = limits.input_tokens - reserved
    start = clock() if started_at is None else started_at
    trace: dict[str, Any] = {
        "enabled": True,
        "max_levels": limits.max_levels,
        "input_token_limit": limits.input_tokens,
        "levels": [],
    }
    if reserved:
        trace["reserved_tokens"] = reserved
    chains: list[_Chain] = []
    # 先に読む範囲があるとき、範囲の外の文書の起点（段の外。所有の章節だけを最後に読む。#2828）。
    deferred: list[_Chain] = []
    result = SectionExpansionResult(context=context, trace=trace)
    current = context  # 読んだ章節を足した文脈（判定を飛ばした段の分を含む）

    def finish(reason: str) -> SectionExpansionResult:
        if deferred and reason not in {STOP_TIME_LIMIT, STOP_DISABLED, STOP_NO_SECTIONS}:
            read_deferred()
        if current is not result.context:
            # 判定を飛ばした段（読む範囲に届く前。#1503）で足した根拠も、止めた理由によらず返す（#1526）。
            result.context = current
            result.applied = True
        result.stop_reason = reason
        result.used_tokens = context_tokens(result.context)
        trace.update(
            stop_reason=reason,
            elapsed_seconds=max(0.0, clock() - start),
            applied=result.applied,
            levels_read=result.levels,
            used_tokens=result.used_tokens,
            unread=[item.to_payload() for item in result.unread],
            unread_omitted=[item.to_payload() for item in result.omitted],
            minimum_scope_complete=bool(chains) and scope_reached() if reader is not None and chains else False,
            judgement="llm" if grade is not None else "not_requested",
            completed_sections=[
                {"document_id": key[0], "chunk_set_id": key[1], "section_id": key[2]} for key in sorted(reader.read_sections)
            ]
            if reader is not None
            else [],
        )
        return result

    if not getattr(settings, "section_expansion_enabled", True) or limits.max_levels <= 0:
        trace["enabled"] = False
        return finish(STOP_DISABLED)
    # 最低でも所有の章節を読む。列挙・網羅の問がさらに広い範囲を求めればそちらを優先する。
    need = minimum_scope(question) or "leaf"
    trace["minimum_scope"] = need
    seeds = seed_chunk_ids(context, last_attempt.relevant_chunk_ids if last_attempt is not None else ())
    owners: set[tuple[str, str, str]] = set()
    for uid in seeds:
        try:
            outline = section_outline(chunk_uid=uid)
        except Exception:  # noqa: BLE001 - 対応を取得できなかったことを診断へ残す
            trace.setdefault("unmapped_seeds", []).append(uid)
            continue
        if not isinstance(outline, SectionOutline):
            trace.setdefault("unmapped_seeds", []).append(uid)
            continue
        key = (outline.document_id, outline.chunk_set_id, outline.owner_section_id)
        sections = outline.chain()
        if key in owners or not sections:
            continue
        owners.add(key)
        chains.append(_Chain(outline=outline, sections=sections, seed_uid=uid))
    trace["seeds"] = [
        {"document_id": c.outline.document_id, "chunk_set_id": c.outline.chunk_set_id, "section_id": c.outline.owner_section_id}
        for c in chains
    ]
    if not chains:
        return finish(STOP_NO_SECTIONS)

    reader = reader or SectionReader(read_chunks=section_chunks, known_ids=set())
    # 先に読む範囲（業務ガイドの手順の参照）に当たる起点を先に、前の呼び出しで起点にしなかった起点（後の段の新しい
    # 根拠）をその次に広げる（#2828・#2816。同じ段の中の並び）。
    ranks = {id(chain): _priority_rank(chain.outline, priority) for chain in chains}
    chains.sort(
        key=lambda c: (
            ranks[id(c)],
            (c.outline.document_id, c.outline.chunk_set_id, c.outline.owner_section_id) in reader.seeded_owners,
        )
    )
    if need != "document" and any(rank < _RANK_OTHER for rank in ranks.values()):
        # 範囲の外の文書の起点は段の外に置き、範囲の起点の章節を先に読む（#2828。列挙の問は文書全体が読む範囲）。
        deferred = [chain for chain in chains if ranks[id(chain)] == _RANK_OTHER]
        chains = [chain for chain in chains if ranks[id(chain)] < _RANK_OTHER]
        if deferred:
            trace["deferred_seeds"] = [
                {"document_id": c.outline.document_id, "chunk_set_id": c.outline.chunk_set_id, "section_id": c.outline.owner_section_id}
                for c in deferred
            ]
    reader.seeded_owners.update(owners)
    reader.known_ids = _context_chunk_ids(context)
    reader.known_texts = {_record_key(record): str(getattr(record, "text", "") or "") for record in context.records}
    # 再検索の組立で本文が短縮された場合、前の段の読了をそのまま再利用しない。
    reader.read_sections = {
        key
        for key in reader.read_sections
        if all(
            str(getattr(item.record, "text", "") or "") in reader.known_texts.get(_record_key(item.record), "")
            for item in reader._groups.get(key, ())
        )
    }
    level_seconds = DEFAULT_LEVEL_SECONDS
    latest = last_attempt  # 最後の判定（読む範囲に届くまでの段は判定しない）
    # 読む範囲が文書全体の列挙の問で、文書全体まで読んだら評価し直さない (#1528。既定 off)。文書全体を読んだ後は
    # 上に読む章節が無く、評価し直しは足りるかの記録にしか使われない。当たった章節（leaf）が範囲の問は、評価器が
    # 足りないと判定すれば親の章節へ広げるので、評価し直す。
    skip_regrade_at_scope = bool(getattr(settings, "section_skip_regrade_at_scope", False)) and need == "document"

    def scope_reached() -> bool:
        """読む範囲まで読んだか（leaf: 全起点の所有の章節、document: 全起点の文書全体）。"""
        return all(
            reader.section_key(chain.outline, chain.sections[0 if need == "leaf" else -1]) in reader.read_sections for chain in chains
        )

    replaced = 0  # この段で、抜き出した原文に置き換えた・外した文脈の親の数
    extracting = large_sections is not no_large_section_handler
    # 足りると判定したが列挙の観点が未確認のとき、上の章節を 1 段読むのを待つ（#3755）。待つ間の段は評価器も抜き出しも
    # 呼ばない（モデルの呼び出しを増やさない）。
    unverified_pending = False

    def wait_for_unverified(attempt: CragRetrievalAttempt | None, level: int) -> bool:
        """未確認の列挙の観点で、評価し直さずに上の章節を読みに行くか。行くなら診断に残して True。"""
        nonlocal unverified_pending
        if grade is None or unverified_pending or not unverified_enumeration(attempt):
            return False
        unverified_pending = True
        trace["unverified_expansion"] = {"aspects": [ENUMERATION_ASPECT], "after_level": level}
        return True

    def seconds_left() -> float:
        """抽出に使える残りの秒数（回答全体の期限から、生成と次の判定の分を引く）。"""
        remaining_deadline = deadline_remaining()
        left = math.inf if remaining_deadline is None else remaining_deadline
        return left - GENERATION_RESERVE_SECONDS - level_seconds

    def remaining_now(added: Sequence[ContextParentEvidence]) -> int:
        return budget - context_tokens(current) - _tree_tokens(added)

    def read_deferred() -> None:
        """範囲の外の文書の起点の所有の章節を、残りの予算に全体が入るものだけ読む（#2828）。

        読めなかった章節は回答に出さず、診断（``unread_omitted``）にだけ残す。親の章節へは上がらない。
        """
        nonlocal current
        assert reader is not None
        added: list[ContextParentEvidence] = []
        entries: list[dict[str, Any]] = []
        for chain in deferred:
            section = chain.sections[0]
            key = reader.section_key(chain.outline, section)
            if key in reader.read_sections or any(entry["key"] == key for entry in entries):
                continue
            entry: dict[str, Any] = {"key": key, "section_id": section.section_id, "title": section.title}
            entries.append(entry)
            tree = reader.read(chain.outline, section) if section.chunk_count <= MAX_SECTION_CHUNKS else None
            failure = reader.failures.get(key)
            if tree is not None and not failure and _tree_tokens(tree) <= remaining_now(added):
                reader.mark(chain.outline, section, tree)
                added.extend(tree)
                entry.update(read="whole" if tree else "in_context", added_parents=len(tree))
                continue
            entry["read"] = "failed" if failure else "skipped"
            result.omitted.append(
                UnreadSection(
                    chain.outline.document_id,
                    section.section_id,
                    section_label(chain.outline, section),
                    failure or STOP_TOKEN_BUDGET,
                    section.page_start,
                )
            )
        if added:
            current = _merge(current, added)
        trace["deferred"] = [{k: v for k, v in entry.items() if k != "key"} for entry in entries]

    def optional(chain: _Chain, section: SectionInfo, after_read: bool) -> bool:
        """予算で読めなくても回答に出さない章節か（#2816）。

        当たった章節（読む範囲が leaf のとき）より上の段で読む子の章節のうち、関連度の順でより上の兄弟の章節を
        全体で読んだ後に読めなかったもので（``after_read``）、どの起点の chunk も含まず、読取に失敗していないもの。
        読む範囲が文書全体（列挙の問）なら、どの章節も読む範囲に入るので出す。
        """
        if (
            not after_read
            or need == "document"
            or chain.position <= 0
            or section.section_id == chain.sections[chain.position].section_id
            or reader.section_key(chain.outline, section) in reader.failures
        ):
            return False
        same = (chain.outline.document_id, chain.outline.chunk_set_id)
        for other in chains:
            if (other.outline.document_id, other.outline.chunk_set_id) != same:
                continue
            owner = other.outline.get(other.outline.owner_section_id)
            if owner is not None and (owner.section_id == section.section_id or _is_descendant(other.outline, owner, section)):
                return False
        return True

    def report(chain: _Chain, item: UnreadSection, section: SectionInfo, after_read: bool) -> None:
        """予算で読めなかった章節を、回答に出すもの（``unread``）と診断だけのもの（``omitted``）に分ける。"""
        (result.omitted if optional(chain, section, after_read) else result.unread).append(item)

    def try_whole(chain: _Chain, section: SectionInfo, entry: dict[str, Any], added: list[ContextParentEvidence]) -> bool:
        """章節の、まだ文脈に無い親が予算に入れば全体を読む（読んだ・読む必要が無ければ True）。

        文脈にすでにある chunk（最初の根拠・前の段で読んだ章節）は数えない（#1504。二重に数えない）。章節の chunk が
        すべて文脈にあれば、読むものが無い（``in_context``）。
        """
        if section.chunk_count > MAX_SECTION_CHUNKS:
            return False
        tree = reader.read(chain.outline, section)
        failure = reader.failures.get(reader.section_key(chain.outline, section))
        if failure:
            result.unread.append(
                UnreadSection(
                    chain.outline.document_id, section.section_id, section_label(chain.outline, section), failure, section.page_start
                )
            )
            entry.update(read="partial" if tree else "failed", added_parents=0)
            return False
        if _tree_tokens(tree) > remaining_now(added):
            return False
        if not tree:
            reader.mark(chain.outline, section, ())
            entry.update(read="in_context", added_parents=0)
            return True
        reader.mark(chain.outline, section, tree)
        added.extend(tree)
        entry.update(read="whole", added_parents=len(tree))
        return True

    def without_extraction(
        chain: _Chain, section: SectionInfo, entry: dict[str, Any], added: list[ContextParentEvidence], after_read: bool = False
    ) -> None:
        """抽出してから統合が無いとき: 見積もりで入る章節は先頭から入る分だけ読み、入らなければ読まない。"""
        remaining = remaining_now(added)
        label = section_label(chain.outline, section)
        if section.chunk_count <= MAX_SECTION_CHUNKS and reader.unread_chars(chain.outline, section) <= remaining:
            kept, _whole = _fit_tree(reader.read(chain.outline, section), remaining)
            reader.known_ids.update(_record_key(item.record) for item in kept)
            added.extend(kept)
            entry.update(read="partial", added_parents=len(kept))
            label += "の残り"
        else:
            entry["read"] = "skipped"
        report(
            chain,
            UnreadSection(chain.outline.document_id, section.section_id, label, STOP_TOKEN_BUDGET, section.page_start),
            section,
            after_read,
        )

    def hand_off(
        chain: _Chain,
        scope: SectionInfo,
        pending: Sequence[tuple[SectionInfo, dict[str, Any]]],
        holder: dict[str, Any],
        added: list[ContextParentEvidence],
        after_read: bool = False,
    ) -> bool:
        """予算に入らない章節を、抽出してから統合（#1437）に渡す。読めなかった範囲があれば True。

        ``scope`` は広げている章節、``pending`` は入らない章節（``scope`` 自身か、入らない子の章節）。``scope`` の
        親（読めれば ``scope`` の全体、読めなければ ``pending`` の章節）のうち、まだ文脈に無い親と、すでに文脈にある親
        （最初の根拠・前の段で読んだ親）を読み順で渡す。文脈にある親の分は予算に戻し、抜き出した原文で置き換える
        （#1504。同じ原文を二重に数えない）。差し込み口が None を返したら、読めなかった章節にする。
        """
        nonlocal current, replaced
        outline = chain.outline
        sections_ = [section for section, _entry in pending]
        scoped = [scope] if scope.chunk_count <= MAX_SECTION_CHUNKS or sections_ == [scope] else sections_
        in_context = {_record_key(item.record): item for item in current.evidence_tree}
        tree: list[ContextParentEvidence] = []
        seen: set[str] = set()
        absorbed: set[str] = set()
        for section in scoped:
            for item in reader.groups(outline, section):
                key = _record_key(item.record)
                if key in seen:
                    continue
                seen.add(key)
                if key in in_context:
                    tree.append(in_context[key])
                    absorbed.add(key)
                elif key not in reader.known_ids:
                    tree.append(item)
        remaining = remaining_now(added) + sum(estimate_tokens(str(getattr(in_context[key].record, "text", "") or "")) for key in absorbed)
        outcome = large_sections(
            LargeSectionRequest(
                question=question,
                outline=outline,
                section=scope,
                tree=tuple(tree),
                remaining_tokens=max(0, remaining),
                seconds_left=seconds_left,
                seed_positions=_seed_positions(context, chains, chain),
                truncated=any(section.chunk_count > MAX_SECTION_CHUNKS for section in scoped),
                context_keys=frozenset(absorbed),
                sections=() if sections_ == [scope] else tuple(sections_),
            )
        )
        if outcome is None:
            for section, entry in pending:
                entry["read"] = "skipped"
                report(
                    chain,
                    UnreadSection(
                        outline.document_id, section.section_id, section_label(outline, section), STOP_TOKEN_BUDGET, section.page_start
                    ),
                    section,
                    after_read,
                )
            return True
        # 抜粋を取得しても、最低範囲を全量読了したことにはしない。
        for section, _entry in pending:
            if not any(item.document_id == outline.document_id and item.section_id == section.section_id for item in outcome.unread):
                report(
                    chain,
                    UnreadSection(
                        outline.document_id, section.section_id, section_label(outline, section), STOP_PARTIAL, section.page_start
                    ),
                    section,
                    after_read,
                )
        if outcome.replacements:
            current = _replace_records(current, outcome.replacements)
            replaced += len(outcome.replacements)
            reader.read_sections = {key for key in reader.read_sections if key[:2] != (outline.document_id, outline.chunk_set_id)}
        added.extend(outcome.tree)
        # 抜き出しで残った範囲も、起点を含まない兄弟の章節なら診断だけに残す。読取の失敗は回答に出す。
        omissible = {section.section_id for section in sections_ if optional(chain, section, after_read)}
        for item in outcome.unread:
            hidden = item.section_id in omissible and item.reason not in {STOP_EXTRACTION_ERROR, STOP_READ_ERROR}
            (result.omitted if hidden else result.unread).append(item)
        # 抜き出していない章節を「抽出した」にしない（#1504）。
        read = "skipped" if outcome.trace.get("skipped") else "extracted"
        holder.update(
            added_parents=len(outcome.tree),
            replaced_parents=sum(value is not None for value in outcome.replacements.values()),
            removed_parents=sum(value is None for value in outcome.replacements.values()),
            extraction=outcome.trace,
        )
        for _section, entry in pending:
            entry["read"] = read
            if entry is not holder:
                entry["extraction_group"] = scope.section_id
        return True

    for level in range(1, limits.max_levels + 1):
        targets = [chain for chain in chains if chain.next_section is not None]
        if not targets:
            return finish(STOP_DOCUMENT)
        # 時間: 回答全体の期限までに、この段と生成の分が残るか。
        remaining_deadline = deadline_remaining()
        left = math.inf if remaining_deadline is None else remaining_deadline
        if left < level_seconds + GENERATION_RESERVE_SECONDS:
            _mark_unread(result, reader, targets, STOP_TIME_LIMIT)
            return finish(STOP_TIME_LIMIT)
        level_trace: dict[str, Any] = {"level": level, "sections": []}
        trace["levels"].append(level_trace)
        # 未確認の観点のために読む段では、入らない章節を抜き出さない（抜き出しはモデルを呼ぶ）。
        extract_now = extracting and not unverified_pending
        added: list[ContextParentEvidence] = []
        replaced = 0
        budget_hit = False
        labels: list[str] = []
        seen_targets: set[tuple[str, str, str]] = set()
        for chain in targets:
            section = chain.next_section
            assert section is not None
            chain.position += 1
            key = reader.section_key(chain.outline, section)
            if key in seen_targets or key in reader.read_sections:
                continue
            seen_targets.add(key)
            labels.append(section_label(chain.outline, section))
            entry: dict[str, Any] = {
                "section_id": section.section_id,
                "title": section.title,
                "is_document": section.section_id == ROOT_SECTION_ID,
            }
            level_trace["sections"].append(entry)
            if try_whole(chain, section, entry, added):
                continue
            if reader.section_key(chain.outline, section) in reader.failures:
                budget_hit = True
                continue
            # 予算に入らない: 子の章節を関連度の順に、入るものから全体で読む。
            done = chain.sections[chain.position - 1] if chain.position > 0 else None
            children = [
                child for child in chain.outline.children(section.section_id) if done is None or child.section_id != done.section_id
            ]
            if not children:
                # 子の章節が無い章節（大きな leaf・子をすべて読んだ章節）は、抽出してから統合（#1437）に渡す。
                if extract_now:
                    budget_hit = hand_off(chain, section, [(section, entry)], entry, added) or budget_hit
                else:
                    without_extraction(chain, section, entry, added)
                    budget_hit = True
                continue
            ranked, method = _rank_children(question, chain.outline, children, rerank, settings)
            entry.update(read="children", ranking=method, children=[])
            pending: list[tuple[SectionInfo, dict[str, Any]]] = []
            read_above = False  # 関連度の順でより上の子の章節を全体で読んだ（#2816）
            for child in ranked:
                child_key = reader.section_key(chain.outline, child)
                if child_key in reader.read_sections:
                    continue
                child_entry: dict[str, Any] = {"section_id": child.section_id, "title": child.title}
                entry["children"].append(child_entry)
                if pending:
                    # 入らない子の章節が出たら、それより関連の低い子の章節もまとめて抽出してから統合に渡す
                    # （関連の低い章節が先に予算を使わない）。読むものが無い章節は渡さない。
                    if (
                        child.chunk_count <= MAX_SECTION_CHUNKS
                        and not reader.read(chain.outline, child)
                        and child_key not in reader.failures
                    ):
                        reader.mark(chain.outline, child, ())
                        child_entry.update(read="in_context", added_parents=0)
                    else:
                        pending.append((child, child_entry))
                    continue
                if try_whole(chain, child, child_entry, added):
                    read_above = True
                    continue
                if child_key in reader.failures:
                    # 読取に失敗した子の章節は、失敗として出す（予算で読めなかった章節にしない）。
                    budget_hit = True
                    continue
                if extract_now:
                    pending.append((child, child_entry))
                else:
                    without_extraction(chain, child, child_entry, added, read_above)
                    budget_hit = True
            if pending:
                # 入らない子の章節は、まとめて抽出してから統合（#1437）に渡す（1 回分に複数の章節を入れられる）。
                budget_hit = hand_off(chain, section, pending, entry, added, read_above) or budget_hit
        if budget_hit or any(item.get("read") == "extracted" for item in _entries(level_trace)):
            # 全体を読む必要のある章節は、抽出してから統合（#1437）に渡す（差し込み口が無ければ予算で打ち切る）。
            trace["large_section_handoff"] = (
                "extract_then_integrate"
                if extract_now
                else "not_used_for_unverified_aspects"
                if extracting
                else "extract_then_integrate_unavailable"
            )
        result.levels = level
        level_trace["labels"] = labels
        if added:
            current = _merge(current, added)
            level_trace["added_parents"] = len(added)
        if replaced:
            level_trace["replaced_parents"] = replaced
        if unverified_pending and (added or replaced):
            # 未確認の観点のために上の章節を読んだ。評価し直さずに生成へ渡す（#3755）。
            level_trace["grade_skipped"] = "unverified_aspects"
            return finish(STOP_TOKEN_BUDGET if budget_hit else STOP_UNVERIFIED_EXPANDED)
        if grade is None and (scope_reached() or budget_hit):
            level_trace["grade_skipped"] = "not_requested"
            return finish(STOP_PARTIAL if result.unread else STOP_SCOPE_READ)
        if not added and not replaced:
            if budget_hit:
                return finish(STOP_TOKEN_BUDGET)
            # 判定を飛ばした段で足した根拠がまだ判定・生成に渡っていない（#1526）。評価し直しの省略（#1528）は、
            # 下の判定の前で扱う（足した根拠が判定に渡っていないときだけ）。
            unjudged = current is not result.context or (latest is None and grade is not None)
            if unverified_pending:
                # 未確認の観点のために読む段で、足す根拠が無かった。さらに上の章節を読みに行く（段の上限まで）。
                continue
            if scope_reached() and is_enough(latest) and not unjudged:
                if wait_for_unverified(latest, level):
                    continue
                # 読む範囲の章節はすでに文脈にあった（足す根拠が無い）。最後の判定のまま止める。
                return finish(STOP_SUFFICIENT)
            if not (unjudged and scope_reached()):
                continue
            # 読む範囲に届いた段で足す根拠が無くても（表題の章節が文書の全部を持ち、文書全体の段が in_context）、
            # それまでの段で足した根拠で判定する（#1526）。
        if not scope_reached() and not budget_hit:
            # 読む範囲に届くまでは判定しない（網羅の問で、見えている数件で「足りる」としない。#1503）。
            level_trace["grade_skipped"] = "minimum_scope"
            continue
        if skip_regrade_at_scope and not budget_hit:
            # 読む範囲（文書全体）まで決定的に読んだ。上に読む章節は無いので、評価し直さずに生成へ進む (#1528)。
            level_trace["grade_skipped"] = "scope_reached"
            result.context = current
            result.applied = True
            return finish(STOP_SCOPE_READ)
        result.context = current
        result.applied = True
        before = clock()
        try:
            assert grade is not None
            attempt = grade(current, level)
        except Exception as exc:  # noqa: BLE001 - 判定の失敗は、読んだ章節のまま回答へ進む
            if type(exc).__name__ == "DeadlineExceeded":
                raise
            level_trace["grade_error"] = type(exc).__name__  # 例外の型の名前だけ（本文は残さない。#3259）
            return finish(STOP_GRADE_ERROR)
        level_seconds = max(1.0, clock() - before)
        latest = attempt
        level_trace.update(sufficient=attempt.sufficient, missing_aspects=list(attempt.missing_aspects))
        if is_enough(attempt) and scope_reached() and not result.unread:
            if budget_hit or not wait_for_unverified(attempt, level):
                return finish(STOP_TOKEN_BUDGET if budget_hit and unverified_enumeration(attempt) else STOP_SUFFICIENT)
            continue
        if budget_hit:
            return finish(STOP_TOKEN_BUDGET)
    remaining_targets = [chain for chain in chains if chain.next_section is not None]
    if not remaining_targets:
        return finish(STOP_DOCUMENT)
    _mark_unread(result, reader, remaining_targets, STOP_MAX_LEVELS)
    return finish(STOP_MAX_LEVELS)


def _seed_positions(context: AnswerContext, chains: Sequence[_Chain], chain: _Chain) -> tuple[int, ...]:
    """同じ文書（chunk_set）の起点の当たった chunk の、文書の中の位置（``seq_no``）。"""
    same = {
        item.seed_uid
        for item in chains
        if (item.outline.document_id, item.outline.chunk_set_id) == (chain.outline.document_id, chain.outline.chunk_set_id)
    }
    positions = [
        int(getattr(child.record, "seq_no", 0) or 0)
        for parent in context.evidence_tree
        for child in parent.children
        if _record_key(child.record) in same
    ]
    return tuple(dict.fromkeys(positions))


def _entries(level_trace: dict[str, Any]) -> list[dict[str, Any]]:
    """段の記録の章節と子の章節の記録（読み方を数えるため）。"""
    entries: list[dict[str, Any]] = []
    for entry in level_trace.get("sections", []):
        entries.append(entry)
        entries.extend(entry.get("children", []) or [])
    return entries


def _mark_unread(result: SectionExpansionResult, reader: SectionReader, chains: Sequence[_Chain], reason: str) -> None:
    """次に読むはずだった章節を、読めなかった章節にする（同じ章節は 1 回）。"""
    seen = {(item.document_id, item.section_id) for item in result.unread}
    for chain in chains:
        section = chain.next_section
        if section is None or reader.section_key(chain.outline, section) in reader.read_sections:
            continue
        key = (chain.outline.document_id, section.section_id)
        if key in seen:
            continue
        seen.add(key)
        result.unread.append(
            UnreadSection(chain.outline.document_id, section.section_id, section_label(chain.outline, section), reason, section.page_start)
        )


__all__ = [
    "CONTENT_ASPECTS",
    "GENERATION_RESERVE_SECONDS",
    "DEFAULT_MODEL_INPUT_TOKENS",
    "LIMIT_STOPS",
    "MAX_SECTION_CHUNKS",
    "STOP_EXTRACTION_CALLS",
    "STOP_EXTRACTION_ERROR",
    "STOP_SCOPE_READ",
    "STOP_SECTION_CHUNKS",
    "ROOT_SECTION_ID",
    "SECTION_CONTEXT_ROLE",
    "SECTION_EXPANSION_REASON",
    "LargeSectionHandler",
    "LargeSectionOutcome",
    "LargeSectionRequest",
    "SectionExpansionLimits",
    "SectionExpansionResult",
    "SectionInfo",
    "SectionRead",
    "SectionReader",
    "SectionOutline",
    "UnreadSection",
    "context_tokens",
    "estimate_tokens",
    "expand_sections",
    "is_enough",
    "unverified_enumeration",
    "no_large_section_handler",
    "model_input_tokens",
    "section_label",
    "seed_chunk_ids",
]
