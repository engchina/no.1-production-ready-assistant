"""列挙・網羅の問の十分性（#1503）。

CRAG の評価器が「足りる」と判定しても、列挙の問は件数・章の範囲の欠けをプログラムで数え、件数・範囲の無い網羅の問は
読む範囲（当たった章節・文書全体）まで章節を広げてから判定する。評価器と章節の読み込みはスタブで、LLM と DB を呼ばない。
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest
from test_section_expansion import (
    Grader,
    SectionStub,
    _attempt,
    _child_ids,
    _expand,
    _members,
    _seed_context,
    _settings,
)

from rag_engine.generation import section_expansion as sections
from rag_engine.generation.answer_models import CragRetrievalAttempt
from rag_engine.generation.answering import AnswerContext, AnswerRecord, _grade_crag_retrieval
from rag_engine.models.llm import CragRetrievalGradeOutput
from rag_engine.retrieval.context_builder import ContextParentEvidence
from rag_engine.retrieval.enumeration import ENUMERATION_ASPECT, enumeration_check, enumeration_request
from rag_engine.retrieval.task_contract import task_contract

# s1437 の評価の問（サーバー室の定期点検要領）と、評価セットの多段・業務支援の問の例。
BUSINESS_DAY_FIVE = "サーバー室の定期点検要領の全体を通して、「営業日」で期限が決められている作業を 5 つすべて挙げてください。"
SHEET_COLUMNS = "サーバー室の定期点検要領の第 2 章から第 6 章までの各章で、点検表の欄に書くことを章ごとにすべて挙げてください。"
STOP_SIX = (
    "サーバー室の定期点検要領の第 4 章には、点検を中止する条件が 6 つ書かれています。"
    "6 つすべてと、それぞれ点検表に書くことを挙げてください。"
)
DO_NOT_TOUCH = "サーバー室の定期点検要領で、作業者が触れない・しないと決められていることをすべて挙げてください。"
PREPARATION = "サーバー室の定期点検の前に行う準備をすべて挙げてください。"


@pytest.mark.parametrize(
    "question, scope, count, chapters, minimum, max_items",
    [
        (BUSINESS_DAY_FIVE, "document", 5, [], None, 7),
        # 2 つ以上の章の範囲は 1 つの章節に入らないので、文書全体まで読んでから判定する。
        (SHEET_COLUMNS, "document", None, [2, 3, 4, 5, 6], "document", 10),
        (STOP_SIX, "section", 6, [], None, 8),
        (DO_NOT_TOUCH, "document", None, [], "document", 5),
        (PREPARATION, "section", None, [], "leaf", 5),
        ("重要度が A で、機密区分が極秘のシステムをすべて挙げてください。", "section", None, [], "leaf", 5),
        ("承認の条件を三つ、漏れなく教えてください。", "section", 3, [], None, 5),
    ],
    ids=["count-document", "chapter-range", "count-section", "open-document", "open-section", "open-table", "kanji-count"],
)
def test_detects_enumeration_requests(question, scope, count, chapters, minimum, max_items):
    request = enumeration_request(question)
    assert request is not None
    assert request.to_payload() == {
        "scope": scope,
        "expected_count": count,
        "chapters": chapters,
        "minimum_scope": minimum,
        "max_items": max_items,
    }
    contract = task_contract(question)
    assert ENUMERATION_ASPECT in contract["required_aspects"]
    assert contract["enumeration"] == request.to_payload()


@pytest.mark.parametrize(
    "question",
    [
        "経費精算ポータルの変更の申請は、誰が承認し、受付から何営業日以内に承認されますか？",
        "承認の記録は何年間保管しますか？",
        "人事評価システムと勤怠管理システムでは、どちらのデータを長く保管しますか？",
        "すべての部署に適用されますか？",
        "第3章の設備ごとの点検の項目を教えて",
        "障害の最初の連絡で、電話がつながらないときはどうしますか？",
    ],
    ids=["bridge", "single-hop", "comparison", "quantifier-only", "no-exhaustive-word", "procedure"],
)
def test_other_questions_are_not_enumerations(question):
    assert enumeration_request(question) is None
    contract = task_contract(question)
    assert contract["enumeration"] is None and ENUMERATION_ASPECT not in contract["required_aspects"]


# ---- 欠けを数える ---------------------------------------------------------------------------------------


def _record(uid: str, text: str, chapter: str) -> AnswerRecord:
    return AnswerRecord(
        id=uid,
        chunk_id=uid,
        chunk_uid=uid,
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=text,
        source="点検要領.pdf",
        chunk_level="parent",
        metadata={"section_path": ["サーバー室の定期点検要領", chapter]},
    )


def _context(*records: AnswerRecord) -> AnswerContext:
    tree = tuple(ContextParentEvidence(record=item, role="synthesis_parent", reason="retrieved") for item in records)
    return AnswerContext(records=list(records), text="\n".join(item.text for item in records), evidence_tree=tree)


RESERVE = _record(
    "p2",
    "点検の 5 営業日前までに、総務部にサーバー室の入室の予約をします。点検の 3 営業日前までに、"
    "点検の日時をサーバー室を使う部署に知らせます。",
    "第 2 章 点検の前の準備",
)
REPORT = _record("p12", "点検の記録は、点検の日から 3 営業日以内に情報システム部長へ提出します。", "第 6 章 点検の後の作業")


def _items(*pairs: tuple[str, str]) -> list[dict[str, object]]:
    return [{"item": quote[:8], "source_ids": [uid], "quote": quote} for uid, quote in pairs]


def _check(question: str, items, context: AnswerContext, checks=()) -> dict[str, object]:
    uids = {record.chunk_uid for record in context.records}
    result = enumeration_check(question, list(checks), items, context, context.records, lambda value: value if value in uids else "")
    [entry] = [item for item in result if item["aspect"] == ENUMERATION_ASPECT]
    return entry


def test_count_short_of_the_request_is_missing_even_if_the_grader_says_supported():
    context = _context(RESERVE, REPORT)
    items = _items(
        ("p2", "5 営業日前までに、総務部に"), ("p2", "3 営業日前までに、点検の日時"), ("p12", "3 営業日以内に情報システム部長へ提出")
    )
    llm = [{"aspect": ENUMERATION_ASPECT, "status": "supported", "source_ids": ["p2"], "reason": "そろっている"}]

    entry = _check(BUSINESS_DAY_FIVE, items, context, llm)

    assert entry["status"] == "missing" and entry["counted_items"] == 3 and entry["expected_count"] == 5
    assert "要求 5 件に対し、原文を確かめた項目 3 件" in entry["reason"]


def test_items_without_a_verbatim_quote_or_duplicated_are_not_counted():
    context = _context(RESERVE, REPORT)
    items = [
        *_items(
            ("p2", "5 営業日前までに、総務部に"),
            ("p2", "5 営業日前までに、総務部に"),  # 同じ原文
            ("p12", "10 営業日以内に補充します"),  # 根拠の本文に無い（推測）
            ("missing", "3 営業日前までに"),
        ),  # 候補に無い chunk_uid
        {"item": "知らせる", "source_ids": ["p2"], "quote": "3営業日前までに、点検の日時"},  # 空白の違いは同じ
    ]

    entry = _check(BUSINESS_DAY_FIVE, items, context)

    # 原文を確かめられない 2 件を足しても要求の 5 件に届かないので、不足は確かめた項目だけで確定する（#3755）。
    assert entry["counted_items"] == 2 and entry["unverified_items"] == 2
    assert entry["status"] == "missing" and entry["shortfall_cause"] == "verified_shortfall"


# ---- 写しの照合の失敗と、確かめた不足を分ける（#3755） ------------------------------------------------------------

# 合成の文書（業務の内容を持たない匿名の手順）。
STEPS = _record("s1", "手順 A は開始の 2 日前までに行います。手順 B は開始の 1 日前までに行います。", "第 2 章 準備")
CLOSING = _record("s2", "手順 C は終了の 3 日以内に行います。手順 D は終了の 5 日以内に行います。", "第 3 章 後処理")
FOUR_STEPS = "文書 A で、期限が決められている手順を 4 つすべて挙げてください。"
SUPPORTED = [{"aspect": ENUMERATION_ASPECT, "status": "supported", "source_ids": ["s1"], "reason": "そろっている"}]


def test_verified_shortfall_stays_missing():
    items = _items(("s1", "手順 A は開始の 2 日前まで"), ("s1", "手順 B は開始の 1 日前まで"), ("s2", "手順 C は終了の 3 日以内"))

    entry = _check(FOUR_STEPS, items, _context(STEPS, CLOSING), SUPPORTED)

    assert entry["status"] == "missing" and entry["shortfall_cause"] == "verified_shortfall"
    assert entry["counted_items"] == 3 and entry["unverified_items"] == 0


def test_unverified_quotes_that_could_fill_the_count_are_unknown_not_missing():
    # 評価器は 4 件を挙げて足りると判定したが、2 件の写しが本文と一致しない（言い換え）。照合の失敗は項目が無い根拠に
    # ならないので、不足を確定せず unknown にする。
    items = _items(
        ("s1", "手順 A は開始の 2 日前まで"),
        ("s1", "手順 B は開始の 1 日前まで"),
        ("s2", "手順 C を終了後 3 日で行う"),
        ("unknown-id", "手順 D は終了の 5 日以内"),
    )

    entry = _check(FOUR_STEPS, items, _context(STEPS, CLOSING), SUPPORTED)

    assert entry["status"] == "unknown" and entry["shortfall_cause"] == "quote_unverified"
    assert entry["counted_items"] == 2 and entry["unverified_items"] == 2 and entry["source_ids"] == []
    assert "原文を確かめられない項目 2 件" in entry["reason"]


def test_unverified_quotes_with_the_grader_reporting_missing_stay_missing():
    items = _items(("s1", "手順 A は開始の 2 日前まで"), ("s1", "手順 B は開始の 1 日前まで"), ("s2", "手順 C を終了後 3 日で行う"))
    items += _items(("s2", "手順 D を終了後 5 日で行う"))
    grader_missing = [{**SUPPORTED[0], "status": "missing"}]

    entry = _check(FOUR_STEPS, items, _context(STEPS, CLOSING), grader_missing)

    assert entry["status"] == "missing" and entry["shortfall_cause"] == "grader_status"


def test_unverified_items_passed_by_the_caller_are_not_counted_but_keep_the_shortfall_unknown():
    context = _context(STEPS, CLOSING)
    uids = {record.chunk_uid for record in context.records}
    verified = _items(("s1", "手順 A は開始の 2 日前まで"), ("s1", "手順 B は開始の 1 日前まで"), ("s2", "手順 C は終了の 3 日以内"))
    # 呼び出し側（送った本文）で確かめられなかった項目は、根拠の単位の本文にあっても数えない。
    sent_unverified = _items(("s2", "手順 D は終了の 5 日以内"))

    result = enumeration_check(
        FOUR_STEPS, SUPPORTED, verified, context, context.records, lambda v: v if v in uids else "", unverified_items=sent_unverified
    )

    [entry] = [item for item in result if item["aspect"] == ENUMERATION_ASPECT]
    assert entry["counted_items"] == 3 and entry["unverified_items"] == 1
    assert entry["status"] == "unknown" and entry["shortfall_cause"] == "quote_unverified"


def test_enough_verified_items_with_an_unverified_extra_are_supported():
    items = _items(
        ("s1", "手順 A は開始の 2 日前まで"),
        ("s1", "手順 B は開始の 1 日前まで"),
        ("s2", "手順 C は終了の 3 日以内"),
        ("s2", "手順 D は終了の 5 日以内"),
        ("s2", "手順 E を終了後 7 日で行う"),
    )

    entry = _check(FOUR_STEPS, items, _context(STEPS, CLOSING), SUPPORTED)

    assert entry["status"] == "supported" and entry["shortfall_cause"] is None
    assert entry["counted_items"] == 4 and entry["unverified_items"] == 1 and entry["source_ids"] == ["s1", "s2"]


def test_chapter_shortfall_is_unknown_only_when_unverified_items_could_cover_the_lacking_chapter():
    question = "文書 A の第 2 章から第 3 章までの各章で、決められている手順を章ごとにすべて挙げてください。"
    context = _context(STEPS, CLOSING)
    in_lacking_chapter = _items(("s1", "手順 A は開始の 2 日前まで"), ("s2", "手順 C を終了後 3 日で行う"))
    assert _check(question, in_lacking_chapter, context, SUPPORTED)["status"] == "unknown"
    # 確かめられない項目の根拠が確かめた章（第 2 章）にしか無ければ、第 3 章の欠けは確かめた不足。
    in_covered_chapter = _items(("s1", "手順 A は開始の 2 日前まで"), ("s1", "手順 B を開始前日に行う"))
    entry = _check(question, in_covered_chapter, context, SUPPORTED)
    assert entry["status"] == "missing" and entry["shortfall_cause"] == "verified_shortfall"


def test_open_enumeration_with_only_unverified_quotes_is_unknown():
    question = "文書 A で、作業の前に行う手順をすべて挙げてください。"
    entry = _check(question, _items(("s1", "手順 A を開始前に行う")), _context(STEPS), SUPPORTED)
    assert entry["status"] == "unknown" and entry["shortfall_cause"] == "quote_unverified"


def test_count_met_with_verified_items_is_supported():
    context = _context(RESERVE, REPORT)
    items = _items(("p2", "5 営業日前までに"), ("p2", "3 営業日前までに"), ("p12", "3 営業日以内に"))

    entry = _check("点検の要領で、営業日で期限が決められた作業を 3 つすべて挙げてください。", items, context)

    assert entry["status"] == "supported" and entry["source_ids"] == ["p2", "p12"]


def test_chapter_range_requires_evidence_from_every_chapter():
    context = _context(RESERVE, REPORT)
    items = _items(("p2", "入室の予約をします"), ("p12", "情報システム部長へ提出します"))

    entry = _check(SHEET_COLUMNS, items, context)

    assert entry["status"] == "missing"
    assert "根拠の無い章: 第 3 章、第 4 章、第 5 章" in entry["reason"]
    whole = _context(*(_record(f"c{n}", f"第 {n} 章の点検表の欄に書くこと{n}。", f"第 {n} 章 見出し") for n in range(2, 7)))
    covered = _check(SHEET_COLUMNS, _items(*((f"c{n}", f"点検表の欄に書くこと{n}") for n in range(2, 7))), whole)
    assert covered["status"] == "supported"


def test_chapter_comes_from_the_deepest_heading_and_string_paths():
    """見出しは「A > B」の文字列でもよく、深さが崩れて前の章の節の下に入った章は最も深い章で数える。"""
    nested = AnswerRecord(
        id="n5",
        chunk_id="n5",
        chunk_uid="n5",
        engine="docling",
        engine_label="Docling",
        page=10,
        seq_no=1,
        category="Chunk",
        text="点検の記録には、点検の日時と点検者の氏名を書きます。",
        chunk_level="parent",
        metadata={"section_path": "要領 > 第 3 章 設備ごとの点検 > 3.3 消火設備 > 第 5 章 点検の記録"},
    )
    context = _context(nested)
    entry = _check(
        "要領の第 3 章から第 5 章までの各章で、書くことを章ごとにすべて挙げてください。", _items(("n5", "点検者の氏名を書きます")), context
    )
    assert "根拠の無い章: 第 3 章、第 4 章" in entry["reason"]


def test_open_enumeration_uses_grader_status_only_with_verified_items():
    context = _context(RESERVE)
    llm = [{"aspect": ENUMERATION_ASPECT, "status": "supported", "source_ids": ["p2"], "reason": "そろう"}]
    assert _check(DO_NOT_TOUCH, [], context, llm)["status"] == "missing"  # 確かめた項目が無い
    assert _check(DO_NOT_TOUCH, _items(("p2", "入室の予約をします")), context, llm)["status"] == "supported"
    missing = [{**llm[0], "status": "missing"}]
    assert _check(DO_NOT_TOUCH, _items(("p2", "入室の予約をします")), context, missing)["status"] == "missing"


def test_non_enumeration_checks_are_unchanged():
    checks = [{"aspect": "business_object", "status": "supported", "source_ids": ["p2"], "reason": "r"}]
    assert enumeration_check("承認の記録は何年間保管しますか？", checks, [], _context(RESERVE), [RESERVE], lambda value: value) == checks


def _grade(question: str, items, *records: AnswerRecord):
    """評価器（スタブ）が全観点 supported・sufficient=true と答えたときの判定。"""
    checks = [
        {
            "aspect": aspect,
            "status": "supported",
            "source_ids": [records[0].chunk_uid],
            "evidence": [{"source_id": records[0].chunk_uid, "quote": records[0].text}],
            "reason": "関連",
        }
        for aspect in task_contract(question)["required_aspects"]
    ]
    output = CragRetrievalGradeOutput(
        candidate_verdicts=[{"chunk_uid": item.chunk_uid, "relevant": True, "reason": "関連"} for item in records],
        enumerated_items=items,
        aspect_checks=checks,
        answerable_parts=[
            dict(
                aspect="requested_result",
                kind="rule",
                claim=records[0].text,
                evidence=[dict(source_id=records[0].chunk_uid, quote=records[0].text)],
            )
        ],
        reason="足りる",
        sufficient=True,
        confidence=0.95,
        rewritten_query="",
    )
    with patch("rag_engine.generation.answering.parse_text_response", return_value=output):
        return _grade_crag_retrieval(1, question, question, [question], _context(*records), object())


def test_grader_saying_sufficient_still_reports_the_missing_enumeration():
    attempt = _grade(BUSINESS_DAY_FIVE, _items(("p2", "5 営業日前までに"), ("p12", "3 営業日以内に")), RESERVE, REPORT)

    # 一部の規則には直接根拠があっても、列挙の不足は章節の拡張へ渡す。
    assert attempt.sufficient is True
    assert ENUMERATION_ASPECT in attempt.missing_aspects
    assert not sections.is_enough(attempt)


def test_grade_keeps_unverified_enumeration_quotes_out_of_the_missing_aspects():
    # 評価器は全観点を supported・足りると判定したが、列挙の項目の写しが送った本文と一致しない（#3755）。
    paraphrased = _items(
        ("s1", "手順 A は開始の 2 日前まで"),
        ("s1", "手順 B を開始前日に行う"),
        ("s2", "手順 C を終了後 3 日で行う"),
        ("s2", "手順 D を終了後 5 日で行う"),
    )
    attempt = _grade(FOUR_STEPS, paraphrased, STEPS, CLOSING)

    entry = next(item for item in attempt.aspect_checks if item["aspect"] == ENUMERATION_ASPECT)
    assert entry["status"] == "unknown" and entry["shortfall_cause"] == "quote_unverified"
    assert ENUMERATION_ASPECT not in attempt.missing_aspects and ENUMERATION_ASPECT in attempt.unknown_aspects
    assert attempt.required_aspect_coverage == "partial"

    verified = _items(
        ("s1", "手順 A は開始の 2 日前まで"),
        ("s1", "手順 B は開始の 1 日前まで"),
        ("s2", "手順 C は終了の 3 日以内"),
        ("s2", "手順 D は終了の 5 日以内"),
    )
    complete = _grade(FOUR_STEPS, verified, STEPS, CLOSING)
    assert complete.required_aspect_coverage == "complete" and complete.missing_aspects == () and complete.unknown_aspects == ()

    short = _grade(FOUR_STEPS, verified[:3], STEPS, CLOSING)
    assert ENUMERATION_ASPECT in short.missing_aspects


def test_grader_judgement_of_other_questions_is_unchanged():
    question = "承認の記録は何年間保管しますか？"
    record = _record("r1", "承認の記録は 5 年間保管します。", "第 3 章 承認の記録")
    attempt = _grade(question, [], record)
    assert attempt.sufficient is True and attempt.missing_aspects == ()
    assert sections.is_enough(attempt)
    assert all(item["aspect"] != ENUMERATION_ASPECT for item in attempt.aspect_checks)


# ---- 読む範囲まで章節を広げる ---------------------------------------------------------------------------------


def test_open_section_enumeration_reads_the_owner_section_before_trusting_sufficient():
    stub, grader = SectionStub(), Grader(needs=set(_members("s3-1")))

    result = _expand(
        _seed_context("c31a"),
        _settings(),
        grader,
        stub,
        last=_attempt(sufficient=True),
        question="第3章の設備ごとの点検の項目をすべて教えて",
    )

    # CRAG の最初の判定は足りるでも、当たった章節（leaf の 3.1）は読む。読んだ後の判定で止める（1 段・判定 1 回）。
    assert stub.read_calls == ["s3-1"] and grader.calls == [1]
    assert result.stop_reason == sections.STOP_SUFFICIENT and result.applied
    assert result.trace["minimum_scope"] == "leaf"


def test_open_section_enumeration_with_the_section_already_in_context_does_not_grade_again():
    stub, grader = SectionStub(), Grader(needs=set())

    result = _expand(
        _seed_context("c31a", "c31b"),
        _settings(),
        grader,
        stub,
        last=_attempt(sufficient=True),
        question="第3章の設備ごとの点検の項目をすべて教えて",
    )

    # leaf の chunk はすでに文脈にある（足す根拠が無い）。最初の判定のまま止め、評価器を呼ばない。
    assert stub.read_calls == ["s3-1"] and grader.calls == []
    assert result.stop_reason == sections.STOP_SUFFICIENT and not result.applied


def test_open_document_enumeration_reads_up_to_the_document_and_grades_once():
    stub, grader = SectionStub(), Grader(needs=set())

    result = _expand(_seed_context("c31a"), _settings(), grader, stub, last=_attempt(sufficient=True), question=DO_NOT_TOUCH)

    # 文書の名前を範囲にした網羅の問: leaf → 第3章 → 文書全体まで読み、判定は文書全体を読んだ後の 1 回だけ。
    assert stub.read_calls == ["s3-1", "s3", "document"]
    assert grader.calls == [3]
    assert result.stop_reason == sections.STOP_SUFFICIENT
    assert [level.get("grade_skipped") for level in result.trace["levels"]] == ["minimum_scope", "minimum_scope", None]


def test_counted_enumeration_relies_on_the_counted_aspect():
    stub, grader = SectionStub(), Grader(needs=set(_members("s3")), missing=(ENUMERATION_ASPECT,))

    # 件数のある問は読む範囲の規則を使わない。最初の判定で件数が足りなければ（missing）広げ、足りた段で止める。
    short = _expand(
        _seed_context("c31a"),
        _settings(),
        grader,
        stub,
        last=_attempt(sufficient=True, missing=(ENUMERATION_ASPECT,)),
        question=BUSINESS_DAY_FIVE,
    )
    assert stub.read_calls == ["s3-1", "s3"] and short.stop_reason == sections.STOP_SUFFICIENT

    stub2, grader2 = SectionStub(), Grader(needs=set(_members("s3-1")))
    enough = _expand(_seed_context("c31a"), _settings(), grader2, stub2, last=_attempt(sufficient=True), question=BUSINESS_DAY_FIVE)
    # 最初の判定で件数が足りても、最低 1 段は読んでから止める。
    assert enough.stop_reason == sections.STOP_SUFFICIENT
    assert stub2.read_calls == ["s3-1"] and grader2.calls == [1]


class TitledSectionStub(SectionStub):
    """表題の章節（level 1）が文書の全部の chunk を持ち、章をその下に置く章節ナビゲーション（#1526）。

    PDF の表題が level 1 の見出しになる文書。文書全体（根）の段では、表題の章節を読んだ後に足す根拠が無い。
    """

    def outline(self, *, chunk_uid: str, **_: object) -> sections.SectionOutline | None:
        outline = super().outline(chunk_uid=chunk_uid)
        assert outline is not None
        root, *rest = outline.sections
        moved = [replace(item, parent_section_id="title") if item.parent_section_id == root.section_id else item for item in rest]
        title = replace(root, section_id="title", title="点検要領", level=1, parent_section_id=root.section_id)
        return replace(outline, sections=(root, title, *moved))

    def chunks(self, *, document_id: str, chunk_set_id: str, section_id: str, **options: object):
        chunks = super().chunks(
            document_id=document_id, chunk_set_id=chunk_set_id, section_id="document" if section_id == "title" else section_id, **options
        )
        self.read_calls[-1] = section_id
        return chunks


def test_document_scope_keeps_sections_read_before_the_document_level():
    stub, grader = TitledSectionStub(), Grader(needs=set())

    result = _expand(_seed_context("c4a"), _settings(), grader, stub, last=_attempt(sufficient=True), question=DO_NOT_TOUCH)

    # 第4章 → 表題の章節（文書の全部）を判定を飛ばして読み、文書全体の段は足す根拠が無い（in_context）。それでも
    # 読んだ根拠を生成に渡し（applied）、判定を 1 回だけ呼ぶ。
    assert stub.read_calls[:2] == ["s4", "title"]
    assert [level.get("grade_skipped") for level in result.trace["levels"]] == ["minimum_scope", "minimum_scope", None]
    assert grader.calls == [3]
    assert result.applied and result.stop_reason == sections.STOP_SUFFICIENT
    assert set(_child_ids(result.context)) == set(_members("document"))


def test_document_scope_returns_read_sections_even_when_still_not_enough():
    stub, grader = TitledSectionStub(), Grader(needs=None)

    result = _expand(_seed_context("c4a"), _settings(), grader, stub, last=_attempt(sufficient=True), question=DO_NOT_TOUCH)

    # 読んだ後の判定でも足りない: 文書全体まで読んで止め、読んだ根拠は捨てない。
    assert grader.calls == [3] and result.stop_reason == sections.STOP_DOCUMENT
    assert result.applied and set(_child_ids(result.context)) == set(_members("document"))


def test_max_levels_before_the_reading_scope_still_returns_read_sections():
    stub, grader = TitledSectionStub(), Grader(needs=None)

    result = _expand(
        _seed_context("c4a"), _settings(section_expansion_max_levels=2), grader, stub, last=_attempt(sufficient=True), question=DO_NOT_TOUCH
    )

    # 読む範囲（文書全体）に届く前に段の上限で止めても、判定を飛ばした段で読んだ根拠は返す。
    assert grader.calls == [] and result.stop_reason == sections.STOP_MAX_LEVELS
    assert result.applied and "c2a" in _child_ids(result.context)


# ---- 未確認の列挙の観点（#3755） -------------------------------------------------------------------------------


def _unverified() -> CragRetrievalAttempt:
    """足りると判定したが、列挙の観点が未確認（写しの照合の失敗など）の判定。"""
    return replace(_attempt(sufficient=True), unknown_aspects=(ENUMERATION_ASPECT,))


class _UnverifiedGrader:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, context: AnswerContext, level: int) -> CragRetrievalAttempt:
        self.calls.append(level)
        return _unverified()


def test_unverified_count_reads_one_more_level_without_grading_again():
    # 件数の問で、当たった章節を読んだ後の判定が「足りる・列挙は未確認」。足りるとして止めず、評価器を呼び直さずに
    # 上の章節（第3章）を 1 段だけ読んで生成へ渡す。
    stub, grader = SectionStub(), _UnverifiedGrader()

    result = _expand(_seed_context("c31a"), _settings(), grader, stub, last=_unverified(), question=BUSINESS_DAY_FIVE)

    assert stub.read_calls == ["s3-1", "s3"] and grader.calls == [1]
    assert result.stop_reason == sections.STOP_UNVERIFIED_EXPANDED and result.applied
    assert set(_child_ids(result.context)) == set(_members("s3"))
    assert result.trace["unverified_expansion"] == {"aspects": [ENUMERATION_ASPECT], "after_level": 1}
    assert result.trace["levels"][1]["grade_skipped"] == "unverified_aspects"


def test_unverified_count_with_the_section_in_context_climbs_without_any_grade():
    stub, grader = SectionStub(), _UnverifiedGrader()

    result = _expand(_seed_context("c31a", "c31b"), _settings(), grader, stub, last=_unverified(), question=BUSINESS_DAY_FIVE)

    assert stub.read_calls == ["s3-1", "s3"] and grader.calls == []
    assert result.stop_reason == sections.STOP_UNVERIFIED_EXPANDED


def test_unverified_count_does_not_extract_large_sections():
    # 未確認のために読む段では、入らない章節を抜き出さない（抜き出しはモデルを呼ぶ）。読めなかった章節は記録に残す。
    stub, grader = SectionStub(), _UnverifiedGrader()
    handed: list[object] = []

    result = _expand(
        _seed_context("c31a"),
        _settings(answer_max_input_tokens=2400),
        grader,
        stub,
        last=_unverified(),
        question=BUSINESS_DAY_FIVE,
        large_sections=lambda request: handed.append(request),
    )

    assert handed == [] and grader.calls == [1]
    assert result.trace["large_section_handoff"] == "not_used_for_unverified_aspects"
    assert result.stop_reason == sections.STOP_TOKEN_BUDGET
    assert result.unread or result.omitted


def test_unknown_non_enumeration_aspect_still_stops_as_sufficient():
    stub, grader = SectionStub(), Grader(needs=set(_members("s3-1")))
    last = replace(_attempt(sufficient=True), unknown_aspects=("procedure",))

    result = _expand(_seed_context("c31a"), _settings(), grader, stub, last=last, question=BUSINESS_DAY_FIVE)

    assert result.stop_reason == sections.STOP_SUFFICIENT
    assert stub.read_calls == ["s3-1"] and grader.calls == [1]


def test_standard_completion_ignores_unverified_enumeration():
    # 標準 RAG（評価器なし）は最低範囲の補完だけ。未確認の観点で上へ広げない。
    stub = SectionStub()

    result = _expand(_seed_context("c31a"), _settings(), None, stub, last=_unverified(), question=BUSINESS_DAY_FIVE)

    assert stub.read_calls == ["s3-1"]
    assert "unverified_expansion" not in result.trace
