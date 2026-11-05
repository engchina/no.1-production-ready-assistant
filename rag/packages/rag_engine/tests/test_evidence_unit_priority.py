"""原文予算の配分で、最上位候補と同じ機能ユニットの候補を先に扱う (#665)。"""

from dataclasses import replace

from fictional_examples import ERROR_QUESTION_15, ERROR_UNIT_7, ERROR_UNIT_15, STEP_3

from rag_engine.generation.answering import AnswerRecord
from rag_engine.retrieval.evidence_selection import _same_unit_first, evidence_spans


def _parent(index, path, source="月次締めエラー一覧.pdf", text=None):
    return AnswerRecord(
        id=f"p{index}",
        engine="docling",
        engine_label="Docling",
        page=index,
        seq_no=index,
        category="Chunk",
        text=text or f"{path[-1]} の本文 {index}。" * 40,
        source=source,
        chunk_level="parent",
        chunk_id=f"p{index}",
        chunk_uid=f"uid:p{index}",
        source_run_id="run",
        metadata={"section_path": list(path)},
    )


def test_same_unit_candidates_follow_the_top_candidate_and_keep_relative_order():
    unit15 = ["２．修正方法", ERROR_UNIT_15, "【修正方法２】"]
    top = _parent(1, unit15)
    other_doc = _parent(2, ["（１）ユーザ管理", "B 【操作説明】"], source="システム管理.pdf")
    unit7 = _parent(3, ["２．修正方法", ERROR_UNIT_7, "【修正方法】"])
    sibling_a = _parent(4, ["２．修正方法", ERROR_UNIT_15, "【修正方法１】"])
    sibling_b = _parent(5, unit15)
    ordered = [(r.id, r) for r in (top, other_doc, unit7, sibling_a, sibling_b)]

    assert [k for k, _ in _same_unit_first(ordered)] == ["p1", "p4", "p5", "p2", "p3"]


def test_candidates_without_a_numbered_heading_keep_retrieval_order():
    ordered = [(r.id, r) for r in (_parent(1, ["概要"]), _parent(2, ["概要", "【補足】"]), _parent(3, ["手順"]))]

    assert [k for k, _ in _same_unit_first(ordered)] == ["p1", "p2", "p3"]


def test_budget_is_spent_on_the_same_unit_before_other_documents():
    unit = [ERROR_UNIT_15, "【修正方法２】"]
    top = _parent(1, unit, text=STEP_3 * 30)
    sibling = _parent(2, unit, text="④出荷数量を０にします。⑤確認ボタンを押します。" * 30)
    noise = [_parent(10 + i, ["（１）ユーザ管理"], source="システム管理.pdf", text="ユーザ登録の説明。" * 60) for i in range(6)]
    spans = evidence_spans(f"{ERROR_QUESTION_15[:8]}のエラーの対処", [top, *noise, sibling], budget=2400)

    assert any("④出荷数量" in s["text"] for s in spans)


def _native(index, path, text, source="経理手順.pdf"):
    """解析本文の範囲（native_text_ranges）を持つ親。text 全体が原文。"""
    record = _parent(index, path, source=source, text=text)
    record.metadata.setdefault("layout", {})["native_text_ranges"] = [
        {"record_id": f"r{index}", "page": index, "start": 0, "end": len(text)}
    ]
    return record


def test_top_unit_is_the_first_candidate_whose_native_text_mentions_the_target():
    """生成説明の画面例の値だけが質問の語に一致する候補は起点にしない (#730)。"""
    heading = "Ａシステム管理-(6)パラメータ設定\n"
    described = _parent(
        1,
        ["Ａシステム管理-(6)パラメータ設定"],
        source="システム管理.pdf",
        text=heading + "回答用本文: 更新確認ダイアログ。画面例では締め対象年月=令和6年12月、出荷予定年月=令和6年9月。",
    )
    described.metadata.setdefault("layout", {})["native_text_ranges"] = [
        {"record_id": "r1", "page": 1, "start": 0, "end": len(heading)}
    ]  # 生成説明は原文ではない
    native = _native(2, ["Ⅴ．締め処理"], "出荷予定年月は予定月変更ボタンで変更します。")
    sibling = _native(3, ["Ⅴ．締め処理"], "①処理年月を確認します。")
    ordered = [(r.id, r) for r in (described, native, sibling)]

    assert [k for k, _ in _same_unit_first(ordered, "出荷予定年月を変更するには、どうしたらよいか。")] == ["p2", "p3", "p1"]
    # 対象が解析本文のどこにもなければ従来どおり先頭が起点
    assert [k for k, _ in _same_unit_first(ordered, "処理年月の確認方法")] == ["p1", "p2", "p3"]


def test_native_ranges_of_later_candidates_are_reserved_in_the_budget():
    """先頭の候補の生成説明に予算を使い切らせず、後続の親の短い原文範囲を残す (#730)。"""
    first = _parent(1, ["（１）パラメータ設定"], source="システム管理.pdf", text="回答用本文: 画面の説明。" * 300)
    later = _native(2, ["Ⅴ．締め処理"], "※締め処理の後は予定月変更ボタンを押して予定月を戻してから処理を行ってください。")
    spans = evidence_spans("出荷予定年月を変更するには", [first, later], budget=2000)

    assert any("予定月変更ボタン" in s["text"] for s in spans)


def test_reserve_follows_the_order_after_definition_sort():
    """定義質問で候補が並べ替わっても、取り置きは並べ替え後の後続候補の原文範囲で数える (#744)。"""
    step = "①伝票を選びます。②内容を確認します。③保存ボタンを押します。④一覧に戻ります。⑤終了します。"

    def native_pair(index, marker):
        text = (step * 10)[:500] + "\n" + ((step * 10)[:480] + marker)
        record = _parent(index, ["概要"], source=f"手順{index}.pdf", text=text)
        record.metadata.setdefault("layout", {})["native_text_ranges"] = [
            {"record_id": f"r{index}a", "page": index, "start": 0, "end": 500},
            {"record_id": f"r{index}b", "page": index, "start": 501, "end": len(text)},
        ]
        return record

    first = native_pair(1, "⑥締め処理を行います。")
    defined = _parent(
        2, ["用語"], source="用語集.pdf", text="「予定月」とは、出荷を予定している年月です。" + "回答用本文: 画面の説明。" * 200
    )
    last = native_pair(3, "⑥予定月変更ボタンを押します。")
    spans = evidence_spans("「予定月」とは何ですか", [first, defined, last], budget=2700)

    assert any("⑥予定月変更ボタン" in s["text"] for s in spans)


def test_spans_carry_business_scope_from_document_classification_and_heading_sources():
    """v4 では分類が document 配下、見出しの出所は section_path_sources。span へ転記する (#820)。"""
    record = _parent(1, ["（１）登録"], text="①保存ボタンを押します。" * 5)
    record.metadata["document"] = {"classification": {"large_category": "10_販売管理", "middle_category": "", "small_category": ""}}
    record.metadata["section_path_sources"] = ["section_header"]
    span = evidence_spans("保存の手順", [record], budget=2000)[0]
    assert span["business_scope"] == "10_販売管理"
    assert span["section_path_sources"] == ["section_header"]


def test_span_page_end_covers_the_parent_range_until_the_page_is_pinned():
    """頁を確定できない span は親の頁範囲を持ち、確定できた span は 1 頁に閉じる (#1080)。

    page だけを見ると、複数頁にまたがる親から採った span の出どころを先頭頁だけと
    見積もってしまい、context_recall を実際より低く測る。
    """
    body = "④利用者区分を選びます。⑤登録ボタンを押します。" * 8
    parent = replace(_parent(3, ["（１）ユーザ管理", "B 【操作説明】"], text=STEP_3 * 20 + body), page_end=7)
    spans = evidence_spans("ユーザ登録の操作手順", [parent], budget=2400)

    assert spans
    assert all(s["page"] == 3 and s["page_end"] == 7 for s in spans)

    # anchor の child で頁を確定できた span は、親が 3〜7 頁でもその 1 頁に閉じる。
    child = AnswerRecord(
        id="c3",
        engine="docling",
        engine_label="Docling",
        page=5,
        seq_no=1,
        category="Chunk",
        text=body,
        source=parent.source,
        chunk_level="child",
        chunk_id="c3",
        chunk_uid="uid:c3",
        parent_chunk_uid="uid:p3",
        source_run_id="run",
        metadata={},
    )
    pinned = evidence_spans("ユーザ登録の操作手順", [parent], budget=2400, anchors=[child])

    assert any(s["page"] == 5 and s["page_end"] == 5 for s in pinned)
