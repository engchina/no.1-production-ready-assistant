"""多段の質問の回答: 根拠の段をつないだ結論を本文に書き、根拠に無い主張は書かない (#1383)。

#1362 の評価で、台帳の行（システム → 担当部署の略号）・組織規程（略号 → 部署 → 承認者）・承認規程（役職 →
期限）が文脈にそろっているのに、回答が台帳の行を「参考情報」に置き、冒頭に結論を
書かなかった。LLM を差し替えて、その回答の形（初回の草稿が段を 1 つの item に混ぜて不支持、是正で段を分けて
支持）を決定的に再現する。
"""

from test_grounded_answer import FakeModel, audit, context, draft, ids, item, observed_audit, observed_supports, record, run

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedCitation, GroundedItem

QUESTION = "人事給与システムの変更の承認者と、承認の期限を教えてください。"
LEDGER = "システムID: SYS-111 / 正式名: 人事給与システム / 略称・別表記: HRP / 担当部署: 労 / 重要度: A / 機密区分: 社外秘"
CODES = "略号「経」: 経理部。管理本部に属します。\n略号「労」: 労務部。管理本部に属します。\n略号「製」: 製造部。生産本部に属します。"
APPROVERS = "経理部の承認者は管理本部長です。\n労務部の承認者は労務部長です。"
DEADLINES = "部長が承認する申請は、受付から 3 営業日以内に承認します。\n本部長が承認する申請は、受付から 5 営業日以内に承認します。"
CONCLUSION = "人事給与システムの変更は労務部長が承認し、受付から 3 営業日以内に承認します。"
MERGED = "人事給与システムは労務部が担当で、労務部長が承認者です。"


def _context():
    return context(
        record(0, LEDGER, "システム台帳", page=1, source="system-ledger.xlsx"),
        record(1, CODES, "第 2 章 部署と略号", page=1, source="organization-rules.pdf"),
        record(2, APPROVERS, "第 3 章 部署の承認者", page=2, source="organization-rules.pdf"),
        record(3, DEADLINES, "承認の期限", page=1, source="approval-rules.pdf"),
    )


def _hops(by_text):
    """段ごとに 1 item（台帳の行 → 略号 → 承認者 → 期限）。"""
    return (
        item(by_text[LEDGER], "人事給与システムの担当部署は略号「労」です。", LEDGER, kind="rule", request_id="Q1"),
        item(by_text[CODES], "略号「労」は労務部です。", "略号「労」: 労務部。", kind="rule", request_id="Q1"),
        item(by_text[APPROVERS], "労務部の承認者は労務部長です。", "労務部の承認者は労務部長です。", kind="rule", request_id="Q1"),
        item(
            by_text[DEADLINES],
            "部長が承認する申請は、受付から 3 営業日以内に承認します。",
            "部長が承認する申請は、受付から 3 営業日以内に承認します。",
            kind="rule",
            request_id="Q1",
        ),
    )


def _supported(count, **extra):
    return audit(*[(index, "supported", "matched", "") for index in range(count)], requests=[("Q1", "addressed")], **extra)


def test_conclusion_chained_from_split_hops_is_written_and_the_ledger_row_is_not_left_unverified():
    """段を分けた item がすべて支持されれば、つないだ結論を冒頭に書き、台帳の行も資料により確認できた事項に出す。"""
    ctx = _context()
    by_text = ids(ctx, QUESTION)
    model = FakeModel([draft(*_hops(by_text), summary=CONCLUSION)], [_supported(4)])

    text = run(model, ctx, QUESTION).response.answer_text

    assert text.startswith(CONCLUSION)
    assert grounded.answer_labels().quote_only_section not in text
    assert grounded.answer_labels().quote_only_note not in text
    assert "人事給与システムの担当部署は略号「労」です。" in text


def test_merged_hop_observation_does_not_trigger_correction():
    """段を混ぜた本文への不支持を診断に残し、本文・要約の自動変更や再生成には使わない。"""
    ctx = _context()
    by_text = ids(ctx, QUESTION)
    merged = item(by_text[APPROVERS], MERGED, "労務部の承認者は労務部長です。", kind="rule", request_id="Q1")
    deadline = _hops(by_text)[3]
    first = audit(
        (0, "unsupported", "matched", ""), (1, "supported", "matched", ""), requests=[("Q1", "addressed")], summary_supported=False
    )
    model = FakeModel([draft(merged, deadline, summary=CONCLUSION), draft(*_hops(by_text), summary=CONCLUSION)], [first, _supported(4)])

    result = run(model, ctx, QUESTION).response

    assert [r["accepted"] for r in result.generation_trace["rounds"]] == [True]
    assert result.answer_text.startswith(CONCLUSION)
    assert MERGED in result.answer_text
    assert observed_supports(result) == ["unsupported", "supported"]
    assert not observed_audit(result)["summary_supported"]
    assert grounded.answer_labels().quote_only_section not in result.answer_text


def test_summary_rejection_is_observed_without_rewriting_or_extra_hops():
    """要約の不支持と要求の部分回答は観測に残し、未校正の判定だけで本文や要約を変更しない。"""
    ctx = _context()
    by_text = ids(ctx, QUESTION)
    hops = _hops(by_text)
    guarantee = "人事給与システムの変更は労務部長が承認し、申請はすぐに承認されます。"
    first = audit(*[(index, "supported", "matched", "") for index in range(2)], requests=[("Q1", "partial")], summary_supported=False)
    model = FakeModel([draft(*hops[:2], summary=guarantee), draft(*hops, summary=guarantee)], [first, _supported(4)])

    result = run(model, ctx, QUESTION).response

    assert [r["accepted"] for r in result.generation_trace["rounds"]] == [True]
    assert result.answer_text.startswith(guarantee)
    assert result.generation_trace["finalization"]["summary"]["status"] == "kept"
    assert not observed_audit(result)["summary_supported"]
    assert observed_audit(result)["request_reviews"][0]["status"] == "partial"
    for hop in hops[:2]:
        assert hop["text"] in result.answer_text
    assert not result.envelope["removed_passages"]


def test_summary_rejection_does_not_select_an_unrequested_second_generation():
    """用意された二回目の結論へ未校正の判定だけで差し替えない。低層の反復検出は別途維持する。"""
    ctx = _context()
    by_text = ids(ctx, QUESTION)
    hops = _hops(by_text)
    wrong = "人事給与システムの変更は管理本部長が承認し、受付から 5 営業日以内に承認します。"
    first = audit(*[(index, "supported", "matched", "") for index in range(2)], requests=[("Q1", "partial")], summary_supported=False)
    model = FakeModel([draft(*hops[:2], summary=wrong), draft(*hops, summary=CONCLUSION)], [first, _supported(4)])

    result = run(model, ctx, QUESTION).response

    assert result.answer_text.startswith(wrong)
    assert len(result.generation_trace["rounds"]) == 1
    assert not observed_audit(result)["summary_supported"]
    assert grounded.repeats_summary("一覧を出力します。" + wrong, wrong)
    assert grounded.repeats_summary(wrong.replace("、", ""), wrong)  # 読点の揺れは同じ文
    assert not grounded.repeats_summary(CONCLUSION, wrong)


def test_missing_hop_observation_keeps_generated_gap_and_summary():
    """生成が明示した不足と本文は保持し、未校正の要約判定も独立して残す。"""
    ctx = context(
        record(2, APPROVERS, "第 3 章 部署の承認者", page=2, source="organization-rules.pdf"),
        record(3, DEADLINES, "承認の期限", page=1, source="approval-rules.pdf"),
    )
    by_text = ids(ctx, QUESTION)
    hops = _hops({**by_text, LEDGER: "E-none", CODES: "E-none"})[2:]
    gap = {"kind": "gap", "text": "人事給与システムの担当部署は取得した資料に記載がありません。", "request_id": "Q1"}
    rejected = audit(
        (0, "supported", "matched", ""), (1, "supported", "matched", ""), requests=[("Q1", "partial")], summary_supported=False
    )
    model = FakeModel([draft(*hops, gap, summary=CONCLUSION)], [rejected])

    result = run(model, ctx, QUESTION).response
    text = result.answer_text

    assert text.startswith(CONCLUSION)
    assert result.generation_trace["finalization"]["summary"]["status"] == "kept"
    assert not observed_audit(result)["summary_supported"]
    assert observed_audit(result)["request_reviews"][0]["status"] == "partial"
    for hop in hops:
        assert hop["text"] in text
    assert "人事給与システムの担当部署は取得した資料に記載がありません。" in text


def test_miscopied_sentence_of_a_repeated_table_is_pointed_out_in_the_correction_feedback():
    """略号の表の隣の行の語を写し誤った引用は従来どおり落とし、是正には原文の続きを示す (#1383)。"""
    ctx = _context()
    by_text = ids(ctx, QUESTION)
    # 説明が写し誤った文の語（管理本部）を使うので、逐語の先頭の文にも結び付けない。
    miscopied = item(
        by_text[CODES],
        "略号「製」は製造部で、管理本部に属します。",
        "略号「製」: 製造部。管理本部に属します。",
        kind="rule",
        request_id="Q1",
    )
    model = FakeModel([draft(miscopied, *_hops(by_text)[1:], summary=CONCLUSION)], [_supported(3)])

    result = run(model, ctx, QUESTION).response

    dropped = result.generation_trace["rounds"][0]["dropped"]
    assert dropped[0]["miscopied"] == {
        "quoted": "管理本部に属します。",
        "original": "生産本部に属します。",
        "verbatim_quote": "略号「製」: 製造部。生産本部に属します。",
    }
    feedback = next(prompt for name, prompt in model.prompts[2:] if name == "GroundedDraftWire")
    assert '"original": "生産本部に属します。"' in feedback


def test_uncalibrated_support_quote_does_not_recover_a_miscopied_citation():
    """未校正の支持原文で写し誤りを解消しない。低層の引用候補抽出の既存境界も維持する。"""
    ctx = _context()
    by_text = ids(ctx, QUESTION)
    hops = list(_hops(by_text))
    hops[1] = item(by_text[CODES], "略号「労」は労務部です。", "略号「労」: 労務部。研究本部に属します。", kind="rule", request_id="Q1")
    supported = _supported(4)
    # 元の引用には写し誤りがあるため、支持の判定と合わせて監査の支持原文を返す。
    supported["reviews"][1]["support_quote"] = "略号「労」: 労務部。"
    model = FakeModel([draft(*hops, summary=CONCLUSION)], [supported])

    result = run(model, ctx, QUESTION).response

    assert result.answer_text.startswith(CONCLUSION)
    assert "研究本部" not in result.answer_text
    (mark,) = result.envelope["passage_marks"]
    assert [reason["code"] for reason in mark["reasons"]] == ["quote_unmatched"]
    assert result.generation_trace["finalization"]["summary"]["status"] == "kept"
    assert result.generation_trace["rounds"][0]["items"][1]["quote_match"] != "audited_paraphrase"
    assert observed_audit(result)["reviews"][1]["support_quote"] == "略号「労」: 労務部。"
    assert result.generation_trace["rounds"][0]["items"][1]["review_assessment"]["effective_support"] == "unknown"
    spans = [{"evidence_id": "E1", "label": "E1", "text": CODES}]

    def bound(kind, text):
        return grounded.resolve_evidence(
            GroundedItem(
                kind=kind, text=text, citations=[GroundedCitation(evidence_id="E1", quote="略号「労」: 労務部。研究本部に属します。")]
            ),
            spans,
        )

    assert bound("rule", "略号「労」は労務部です。") == (spans[0], "略号「労」: 労務部。")
    assert bound("rule", "略号「労」は労務部で、研究本部に属します。")[0] is None  # 写し誤った語を説明に使う
    assert bound("operation", "略号「労」は労務部です。")[0] is None  # 手順は従来どおり


def test_miscopied_trailing_sentence_without_audit_support_quote_keeps_the_claim_unconfirmed():
    """原文候補があっても、支持原文なしの監査だけで引用不一致を解消しない。"""
    ctx = _context()
    by_text = ids(ctx, QUESTION)
    hops = list(_hops(by_text))
    hops[1] = item(by_text[CODES], "略号「労」は労務部です。", "略号「労」: 労務部。研究本部に属します。", kind="rule", request_id="Q1")
    model = FakeModel([draft(*hops, summary=CONCLUSION)], [_supported(4)])
    result = run(model, ctx, QUESTION).response
    assert result.answer_text.startswith(CONCLUSION)
    assert result.generation_trace["finalization"]["summary"]["status"] == "kept"
    assert observed_supports(result) == ["supported"] * 4
    assert not observed_audit(result)["reviews"][1]["support_quote"]
    for hop in hops:
        assert hop["text"] in result.answer_text
    (mark,) = result.envelope["passage_marks"]
    assert mark["state"] == "unverified"
    assert [reason["code"] for reason in mark["reasons"]] == ["quote_unmatched"]
    assert not result.envelope["removed_passages"]


def test_miscopied_sentence_needs_a_verbatim_first_sentence_in_the_named_evidence():
    spans = [{"evidence_id": "E1", "label": "E1", "text": CODES}, {"evidence_id": "E2", "label": "E2", "text": APPROVERS}]

    def miscopied(evidence_id, quote):
        return grounded.miscopied_sentence(
            GroundedItem(kind="rule", text="t", citations=[GroundedCitation(evidence_id=evidence_id, quote=quote)]), spans
        )

    assert miscopied("E1", "略号「労」: 労務部。生産本部に属します。") == {
        "quoted": "生産本部に属します。",
        "original": "管理本部に属します。",
        "verbatim_quote": "略号「労」: 労務部。管理本部に属します。",
    }
    assert miscopied("E1", "略号「労」: 総務部。管理本部に属します。") == {}  # 先頭の文から違う
    assert miscopied("E2", "略号「労」: 労務部。生産本部に属します。") == {}  # 指した根拠に無い
    assert miscopied("", "略号「労」: 労務部。生産本部に属します。") == {}  # 根拠を指していない
    assert miscopied("E1", "略号「労」: 労務部。") == {}  # 1 文だけ


def test_prompts_tell_generation_and_audit_how_to_treat_intermediate_hops():
    generate, audit_prompt = grounded.GENERATE_SYSTEM_PROMPT, grounded.AUDIT_SYSTEM_PROMPT
    assert "### 複数の根拠をつなぐ答え（多段）" in generate
    assert "1 段 1 item" in generate
    assert "足りない段を推測で埋めない" in generate
    assert "その item だけで最終の答えにならないこと" in audit_prompt
    assert "「この item だけでは質問に直接答えていない」は not_applicable の理由にならない" in audit_prompt
    assert "尋ねている項目を quote が述べていないことは not_applicable の理由にならない" in audit_prompt
    assert "つなぐ段のどれかが supported な item に無ければ false" in audit_prompt
    # 監査の prompt は 1 行 1 規則（節見出しと空行を除く）。
    assert all(line.startswith(("- ", "## ", "### ")) or line[:1].isdigit() or not line for line in audit_prompt.splitlines())


def test_generation_asks_for_every_supporting_document_and_keeps_scope_checks():
    """同じ内容を述べた規則の原文と手順書の両方を出典にし、出典を 1 つに絞る言い方を残さない（#3526）。"""
    from rag_engine.models.llm import GroundedItem, RequestCoverage, RequestEvidenceScope

    generate = grounded.GENERATE_SYSTEM_PROMPT
    section = generate.split("### 出典の網羅", 1)[1].split("## Step 3:", 1)[0]
    assert "挙げた文書ごとに" in section
    assert "言い換えていても省かず" in section
    assert "説明の内容を支えない原文は、質問に関係していても出典にしない" in section
    # 1つの説明を実際の引用集合で共同支持し、未引用の資料で不足を埋めない。
    citations = GroundedItem.model_json_schema()["properties"]["citations"]["description"]
    assert "指定した出典の原文の集合で text 全体を支える" in citations
    assert "共同して支えてよく" in citations
    assert "各出典が text 全体を単独で支える必要はない" in citations
    assert "実際に指定していない原文で不足を補わない" in citations
    assert "重複する item をまとめ" not in generate
    assert "同じ根拠の同じ説明をまとめ" in generate
    relation = RequestEvidenceScope.model_json_schema()["properties"]["relation"]["description"]
    plan_ids = RequestCoverage.model_json_schema()["properties"]["evidence_ids"]["description"]
    assert "直接答える" not in relation + plan_ids
    # 別の対象の根拠を外す判断と、計画の根拠だけで items を作る契約は残す。
    assert "unrelated=別の対象・独立した操作" in relation
    assert "文書ごとに挙げる" in plan_ids
    assert "items はこの計画の根拠だけで作る" in plan_ids
