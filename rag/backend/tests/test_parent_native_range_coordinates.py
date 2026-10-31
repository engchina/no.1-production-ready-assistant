"""親復元で子のローカル座標を親座標へ一意に移す。外部I/Oなし。"""

import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from app.rag.answer_engine import _SearchState, _stored_child, _stored_parents
from app.schemas.search import RetrievedChunk


def chunk(index: int, text: str, parent_text: str, ranges: Any) -> RetrievedChunk:
    return RetrievedChunk(
        document_id="document-a",
        chunk_id=f"child-{index}",
        text=text,
        score=1.0,
        file_name="synthetic.pdf",
        metadata={
            "chunk_strategy": "small_to_big",
            "chunk_set_id": "version-a",
            "recipe_id": "recipe-a",
            "chunk_group_id": "parent-a",
            "parent_text": parent_text,
            "engine_chunk_seq": index,
            "engine_metadata_json": json.dumps(
                {
                    "layout": {
                        "native_text_ranges": ranges,
                        "caption_context": [{"text": "合成注釈"}],
                    },
                    "image_evidence": [{"image_id": "image-a"}],
                }
            ),
        },
    )


def restore(chunks: list[RetrievedChunk], *, mutate: str = "") -> tuple[Any, _SearchState]:
    state = _SearchState(chunks={value.chunk_id: value for value in chunks})
    stored = [_stored_child(value, rrf_score=1.0) for value in chunks]
    if mutate:
        stored[-1] = replace(stored[-1], metadata={**stored[-1].metadata, "chunk_set_id": mutate})
    (parent,) = _stored_parents(stored, state)
    return parent, state


def test_single_nonzero_child_translates_each_valid_range_without_mutating_source() -> None:
    body, prefix = "対象を選択し、保存します。", "原文の前置きです。\n"
    ranges: list[dict[str, Any]] = [
        {"start": 0, "end": len(body), "origin": "native"},
        {"start": 2, "end": 4, "element_id": "element-a"},
    ]
    value = chunk(1, body, prefix + body, ranges)
    original = deepcopy(value.model_dump())
    parent, state = restore([value])
    assert parent.text == prefix + body
    assert parent.metadata["layout"]["native_text_ranges"] == [
        {**entry, "start": entry["start"] + len(prefix), "end": entry["end"] + len(prefix)}
        for entry in ranges
    ]
    assert parent.metadata["layout"]["caption_context"] == [{"text": "合成注釈"}]
    assert parent.metadata["image_evidence"] == [{"image_id": "image-a"}]
    assert value.model_dump() == original
    assert state.retrieval_stages[-1]["status"] == "verified"


def test_multiple_children_use_their_own_offsets() -> None:
    first, second = "対象Aを選択します。", "対象Bを保存します。"
    text = "前置き\n" + first + "\n区切り\n" + second
    parent, _state = restore(
        [
            chunk(i, body, text, [{"start": 0, "end": len(body)}])
            for i, body in enumerate((first, second))
        ]
    )
    assert parent.metadata["layout"]["native_text_ranges"] == [
        {"start": text.index(body), "end": text.index(body) + len(body)} for body in (first, second)
    ]


@pytest.mark.parametrize(
    "parent_text,reason",
    [("同じ本文\n同じ本文", "child_text_ambiguous"), ("異なる本文", "child_text_missing")],
)
def test_unlocated_child_keeps_native_coordinates_unknown(parent_text: str, reason: str) -> None:
    parent, state = restore([chunk(1, "同じ本文", parent_text, [{"start": 0, "end": 4}])])
    assert "native_text_ranges" not in parent.metadata["layout"]
    assert state.retrieval_stages[-1]["reason"] == reason
    assert state.retrieval_stages[-1]["status"] == "unknown"
    assert parent.text == parent_text


def test_conflicting_parent_text_does_not_merge_coordinates_from_two_versions() -> None:
    first, second = "対象A", "対象B"
    parent, state = restore(
        [
            chunk(0, first, first + second, [{"start": 0, "end": 3}]),
            chunk(1, second, first + "変更" + second, [{"start": 0, "end": 3}]),
        ]
    )
    assert parent.text == first + second
    assert "native_text_ranges" not in parent.metadata["layout"]
    assert [row["reason"] for row in state.retrieval_stages] == ["parent_text_conflict"] * 2


@pytest.mark.parametrize(
    "bad",
    [
        {"start": -1, "end": 2},
        {"start": 2, "end": 1},
        {"start": 0, "end": 999},
        {"start": True, "end": 2},
        {"start": "0", "end": 2},
        {"start": 0},
        "0-2",
    ],
)
def test_invalid_range_is_dropped_but_valid_sibling_range_survives(bad: Any) -> None:
    parent, state = restore(
        [
            chunk(
                0,
                "対象を選択",
                "前置き対象を選択",
                [bad, {"start": 0, "end": 2, "element_id": "valid"}],
            )
        ]
    )
    assert parent.metadata["layout"]["native_text_ranges"] == [
        {"start": 3, "end": 5, "element_id": "valid"}
    ]
    assert state.retrieval_stages[-1]["status"] == "unknown"
    assert state.retrieval_stages[-1]["reason"] == "invalid_child_range"
    assert state.retrieval_stages[-1]["translated_range_count"] == 1


def test_parent_coordinates_in_child_metadata_are_not_accepted_as_local_coordinates() -> None:
    parent, state = restore(
        [chunk(0, "保存", "前置きが長い本文です。保存", [{"start": 11, "end": 13}])]
    )
    assert "native_text_ranges" not in parent.metadata["layout"]
    assert state.retrieval_stages[-1]["reason"] == "invalid_child_range"


def test_stored_child_version_mismatch_does_not_translate_coordinates() -> None:
    parent, state = restore(
        [chunk(0, "保存", "前置き保存", [{"start": 0, "end": 2}])], mutate="version-b"
    )
    assert "native_text_ranges" not in parent.metadata["layout"]
    assert state.retrieval_stages[-1]["reason"] == "source_version_mismatch"
