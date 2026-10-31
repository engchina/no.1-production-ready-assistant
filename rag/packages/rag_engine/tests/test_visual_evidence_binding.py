"""画像の画面名と操作を同じ原文単位で保持し、別版の本文を混ぜない。"""

from dataclasses import replace

import pytest
from fictional_examples import STEP_3, STEP_4, STEP_5
from test_rag_evidence_recovery import rec

from rag_engine.generation.answering import AnswerContext, _visual_evidence_anchors
from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence
from rag_engine.retrieval.evidence_selection import _child_ranges, evidence_spans


def fixture_context():
    """画面名と操作の間に長い表示例がある合成画像説明を作る。"""
    body = "配送予定画面\n" + "表示例の補足\n" * 90 + "対象日を入力して追加し、保存で確定します。"
    child = replace(
        rec("c", "child", 1, body, parent="p"),
        chunk_uid="v1:c",
        parent_chunk_uid="v1:p",
        source_record_refs=({"record_id": "img-1", "page": 1, "category": "Picture"},),
    )
    parent = replace(rec("p", "parent", 1, body + "\n\n別節の補足。" * 30), chunk_uid="v1:p")
    tree = ContextParentEvidence(
        parent, "synthesis_parent", "parent_of_retrieved_child", (ContextChildEvidence(child, "retrieved_anchor", "top_retrieved_child"),)
    )
    other = rec("other", "parent", 2, "無関係な説明" * 400, source="other.pdf")
    context = AnswerContext(records=[parent, other], text="", evidence_tree=(tree,))
    image = dict(source_run_id="manual.pdf", source="manual.pdf", chunk_id="p", image_id="img-1")
    return context, child, image


def test_attached_picture_keeps_screen_and_action_in_one_bounded_span():
    context, child, image = fixture_context()
    anchors = _visual_evidence_anchors(context, [image])
    assert anchors == [child]
    spans = evidence_spans("対象日を追加する方法", context.records, anchors=anchors, budget=1200)
    assert any("配送予定画面" in s["text"] and "保存で確定" in s["text"] for s in spans)
    assert sum(len(s["text"]) for s in spans) <= 1200
    parent = context.records[0]
    assert not _child_ranges(parent, [replace(child, chunk_uid="v2:c")])
    assert not _child_ranges(parent, [replace(child, text="原文にない操作")])


@pytest.mark.parametrize("key,value", [("source_run_id", "other"), ("source", "other.pdf"), ("chunk_id", "other"), ("image_id", "img-2")])
def test_picture_id_alone_does_not_select_another_document_or_parent(key, value):
    context, _, image = fixture_context()
    assert not _visual_evidence_anchors(context, [dict(image, **{key: value})])


def test_visual_anchors_respect_count_and_body_size_limits():
    context, child, image = fixture_context()
    parent = context.evidence_tree[0]
    large = replace(child, text="表示例" * 1400)
    oversized = replace(parent, children=(ContextChildEvidence(large, "retrieved_anchor", "hit"),))
    assert not _visual_evidence_anchors(replace(context, evidence_tree=(oversized,)), [image])
    trees, images = [], []
    for n in range(5):
        p = replace(parent.record, id=f"p{n}", chunk_id=f"p{n}", chunk_uid=f"v1:p{n}")
        c = replace(child, id=f"c{n}", chunk_id=f"c{n}", parent_chunk_id=p.chunk_id, chunk_uid=f"v1:c{n}", parent_chunk_uid=p.chunk_uid)
        trees.append(replace(parent, record=p, children=(ContextChildEvidence(c, "retrieved_anchor", "hit"),)))
        images.append(dict(image, chunk_id=p.chunk_id))
    assert len(_visual_evidence_anchors(replace(context, evidence_tree=tuple(trees)), images)) == 4


def test_procedure_uses_existing_context_crop_but_other_questions_keep_target(tmp_path):
    from rag_engine.generation.answering import answer_image_evidence

    root = tmp_path / "runs"
    run = root / "run-a"
    run.mkdir(parents=True)
    target = run / "target.png"
    target.write_bytes(b"target")
    context = run / "context.png"
    context.write_bytes(b"context")
    ref = dict(
        record_id="img",
        page=1,
        category="Picture",
        vision_context_crop="context.png",
        vision_context_bbox=[0, 0, 100, 100],
        vision_context_record_refs=[dict(record_id="text", category="List-item", partially_visible=False)],
    )
    record = replace(
        rec("p", "parent", 1, "入力欄の右のアイコンを押す。"),
        source_run_id="run-a",
        source_record_refs=(ref,),
        metadata={"image_evidence": [dict(image_id="img", crop_path="target.png")]},
    )
    procedural = answer_image_evidence([record], root, question="追加する方法は？")[0]
    assert procedural["prompt_path"] == str(context)
    assert procedural["asset_kind"] == "context_crop"
    assert procedural["target_highlight"] == "magenta_rectangle"
    assert answer_image_evidence([record], root, question="色は？")[0]["prompt_path"] == str(target)
    context.unlink()
    assert answer_image_evidence([record], root, question="追加する方法は？")[0]["prompt_path"] == str(target)


def test_context_crop_cannot_escape_the_source_run(tmp_path):
    from rag_engine.generation.answering import answer_image_evidence

    run = tmp_path / "run-a"
    run.mkdir()
    target = run / "target.png"
    target.write_bytes(b"target")
    outside = tmp_path / "other.png"
    outside.write_bytes(b"outside")
    ref = dict(
        record_id="img",
        vision_context_crop="../other.png",
        vision_context_record_refs=[dict(record_id="text", category="Text", partially_visible=False)],
    )
    record = replace(
        rec("p", "parent", 1, "操作説明"),
        source_run_id="run-a",
        source_record_refs=(ref,),
        metadata={"image_evidence": [dict(image_id="img", crop_path="target.png")]},
    )
    assert answer_image_evidence([record], tmp_path, question="操作方法は？")[0]["prompt_path"] == str(target)


def test_document_text_origin_uses_verified_ranges_and_never_the_same_words_elsewhere():
    body = "画像の説明: 日付を入力します。\n\n日付を入力します。\n保存ボタンを押します。"
    start = body.index("\n\n") + 2
    record = replace(
        rec("p", "parent", 1, body), metadata={"layout": {"native_text_ranges": [dict(record_id="native", start=start, end=len(body))]}}
    )
    from rag_engine.retrieval.evidence_selection import _span_origin

    assert _span_origin(record, start, len(body), []) == "document_text"
    assert _span_origin(record, 0, start - 2, [(0, start - 2, 1)]) == "image_extraction"
    assert _span_origin(record, 0, len(body), []) == "unclassified"
    assert _span_origin(replace(record, metadata={}), start, len(body), []) == "unclassified"
    bad = replace(record, metadata={"layout": {"native_text_ranges": [dict(start=-1, end=99999)]}})
    assert _span_origin(bad, 0, len(body), []) == "unclassified"


def test_visual_anchor_keeps_adjacent_native_completion_inside_total_budget():
    context, child, image = fixture_context()
    body = child.text
    completion = "保存ボタンを押して登録を確定します。"
    parent = replace(
        context.records[0],
        text=body + "\n" + completion,
        metadata={"layout": {"native_text_ranges": [dict(start=len(body) + 1, end=len(body) + 1 + len(completion))]}},
    )
    spans = evidence_spans("予定日を追加する方法", [parent, context.records[1]], anchors=[child], budget=1200)
    assert any(s["text"] == completion and s["origin"] == "document_text" for s in spans)
    assert sum(len(s["text"]) for s in spans) <= 1200


def test_short_native_steps_are_kept_before_long_generated_descriptions_for_any_goal():
    """親 6,000 字で生成説明が予算を食っても、原文の手順行は質問の種類・画像の有無に関係なく残す (#687)。"""
    steps = f"{STEP_3}\n{STEP_4}\n{STEP_5}"
    generated = "回答用本文: 画面の説明。" + "例示値と画面の並びの説明が長く続く。" * 120
    body = generated + "\n" + steps
    parent = replace(
        rec("p", "parent", 1, body), metadata={"layout": {"native_text_ranges": [dict(start=len(generated) + 1, end=len(body))]}}
    )
    noise = [replace(rec(f"n{i}", "parent", i + 2, "別の機能の説明。" * 200), chunk_uid=f"run:n{i}") for i in range(6)]
    spans = evidence_spans("エラーが出ている。何か操作が必要か。", [parent, *noise], budget=6000)
    native = [s for s in spans if s["origin"] == "document_text"]
    assert any("③在庫振替ボタン" in s["text"] for s in native) and any("⑤確認ボタン" in s["text"] for s in native)
    assert sum(len(s["text"]) for s in spans) <= 6000


@pytest.mark.parametrize("partial", [False, True])
def test_image_selection_prefers_visible_native_operation_over_repeated_caption(tmp_path, partial):
    from rag_engine.generation.answering import answer_image_evidence

    run = tmp_path / "run-a"
    run.mkdir()
    for name in ["correct", "caption"]:
        (run / f"{name}.png").write_bytes(b"image")
    reference = dict(
        record_id="correct",
        vision_context_record_refs=[
            dict(category="Text", partially_visible=partial, text_preview="配送予定の対象日を入力して追加します。")
        ],
    )
    correct = replace(
        rec("p1", "parent", 1, "原図"),
        source_run_id="run-a",
        source_record_refs=(reference,),
        metadata={"image_evidence": [dict(image_id="correct", crop_path="correct.png")]},
    )
    caption = replace(
        rec("p2", "parent", 1, "配送予定の追加方法と対象日の入力説明。" * 30),
        source_run_id="run-a",
        metadata={
            "image_evidence": [dict(image_id="caption", crop_path="caption.png", text_preview="配送予定の対象日を入力して追加します。")]
        },
    )
    selected = answer_image_evidence([caption, correct], tmp_path, max_images=1, question="配送予定の対象日を追加する方法は？")
    assert selected[0]["image_id"] == ("caption" if partial else "correct")
