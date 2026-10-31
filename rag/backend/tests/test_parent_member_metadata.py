"""親子階層の親は、先頭の子以外の子の画像・表・注釈の文脈も持つ (#3478)。

保存するのは子だけで、回答の親は子の metadata から復元する(``_stored_parents``)。図の子が親の
先頭でないと、親が先頭の子の metadata だけを写して画像を失い、回答に図の画像が添付されなかった。
合成の手引き(架空の画面の説明と図)で、backend の実の経路(子 → 親 → 回答の記録)を確かめる。
"""

from __future__ import annotations

import json
from typing import Any

from rag_engine.generation.answer_images import answer_image_evidence
from rag_engine.generation.answer_records import _stored_chunk_answer_record
from rag_engine.retrieval.context_builder import record_has_image_evidence

from app.rag.answer_engine import _SearchState, _stored_child, _stored_parents
from app.schemas.search import RetrievedChunk

_GROUP = "chunk-docling-p000004"
_PARENT_TEXT = (
    "1.3 画面 A\n画面 A では、番号・状態を確かめられます。画面の下に操作のボタンが並びます。\n\n"
    "図1 画面 A の画面例\n\n図2 処理の流れ"
)


def _picture(image_id: str, *, page: int, text: str) -> dict[str, Any]:
    return {
        "image_id": image_id,
        "record_id": image_id,
        "raw_type": "picture",
        "page": page,
        "bbox": [10.0, 10.0, 200.0, 120.0],
        "crop_path": f"images/{image_id}.png",
        "text_preview": text,
        "visual_kind": "screenshot",
        "visual_role": "content",
    }


def _child(
    index: int,
    text: str,
    *,
    images: list[dict[str, Any]] | None = None,
    categories: tuple[str, ...] = ("Text",),
    captions: list[dict[str, Any]] | None = None,
) -> RetrievedChunk:
    engine: dict[str, Any] = {
        "schema_version": 4,
        "active": True,
        "atomic": False,
        "content_hash": f"hash-{index}",
        "classification": {},
        "source_categories": list(categories),
        "section_path": ["合成の手引き", "1.3 画面 A"],
        "section_path_sources": [],
    }
    if images:
        engine["image_evidence"] = images
    if captions:
        engine["layout"] = {"caption_context": captions}
    return RetrievedChunk(
        document_id="doc-1",
        chunk_id=f"doc-1:cs:{index}",
        text=text,
        score=1.0,
        file_name="synthetic-manual.pdf",
        metadata={
            "page_start": 1,
            "page_end": 1,
            "chunk_index": index,
            "engine_chunk_seq": index,
            "chunk_group_id": _GROUP,
            "parent_text": _PARENT_TEXT,
            "chunk_strategy": "small_to_big",
            "engine_metadata_json": json.dumps(engine, ensure_ascii=False),
        },
    )


def _parent(children: list[RetrievedChunk], scores: list[float]) -> Any:
    state = _SearchState(chunks={chunk.chunk_id: chunk for chunk in children})
    stored = [
        _stored_child(chunk, rrf_score=score) for chunk, score in zip(children, scores, strict=True)
    ]
    (parent,) = _stored_parents(stored, state)
    return parent


def test_parent_keeps_the_image_of_a_later_figure_child() -> None:
    """本文の子が先頭、図の子が 2 つ目の親でも、親の記録に図の画像がある(添付の判定が真)。"""
    text = _child(4, "1.3 画面 A\n画面 A では、番号・状態を確かめられます。")
    figure = _child(
        5,
        "画面名: 画面 A\nボタン: 保存 / 戻す",
        images=[_picture("docling-p1-11", page=1, text="画面 A 保存 戻す")],
        categories=("Picture",),
        captions=[{"record_id": "docling-p1-12", "text": "図1 画面 A の画面例"}],
    )

    parent = _parent([text, figure], [0.0, 0.9])
    record = _stored_chunk_answer_record(parent)

    assert [image["image_id"] for image in parent.metadata["image_evidence"]] == ["docling-p1-11"]
    assert parent.metadata["source_categories"] == ["Picture", "Text"]
    assert parent.metadata["layout"]["caption_context"][0]["record_id"] == "docling-p1-12"
    assert record_has_image_evidence(record)
    selected = answer_image_evidence([record], "/nonexistent-output", question="画面 A のボタン")
    assert [item["image_id"] for item in selected] == ["docling-p1-11"]
    # 親の範囲・本文は今までどおり(先頭の子の位置、親の本文)。
    assert parent.chunk_seq == 4
    assert parent.text == _PARENT_TEXT


def test_parent_keeps_every_figure_and_puts_the_most_relevant_child_first() -> None:
    """1 つの親の 2 つの図は両方残り、関連度の高い子の図が先に並ぶ。同じ画像は 1 回だけ。"""
    first = _child(
        9,
        "図1 画面 A の画面例",
        images=[_picture("docling-p2-8", page=2, text="一覧")],
        categories=("Picture",),
    )
    middle = _child(10, "処理の流れは図2のとおりです。")
    second = _child(
        11,
        "図2 処理の流れ",
        images=[
            _picture("docling-p2-11", page=2, text="流れ"),
            # 同じ図を別の子も参照する(重複は 1 回)。
            _picture("docling-p2-8", page=2, text="一覧"),
        ],
        categories=("Picture",),
    )

    parent = _parent([first, middle, second], [0.2, 0.0, 0.8])

    assert [image["image_id"] for image in parent.metadata["image_evidence"]] == [
        "docling-p2-11",
        "docling-p2-8",
    ]
    assert "rrf_score" not in parent.metadata


def test_single_child_parent_metadata_is_unchanged() -> None:
    figure = _child(5, "図1", images=[_picture("docling-p1-11", page=1, text="画面 A")])

    parent = _parent([figure], [1.0])

    assert [image["image_id"] for image in parent.metadata["image_evidence"]] == ["docling-p1-11"]
    assert parent.metadata["source_categories"] == ["Text"]
    assert "layout" not in parent.metadata
