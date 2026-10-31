"""工程の観測に工程の表示用のデータを渡す（処理の経過は trace の投影。#3226）。"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

import pytest

from rag_engine.generation.execution_record import _execution_step
from rag_engine.resources.tracing import bind_trace_observer, observe_call


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.displays: list[tuple[str | None, dict[str, object], Mapping[str, object] | None]] = []

    @contextmanager
    def observe(self, operation: str, model: str) -> Iterator["_Recorder"]:
        self.calls.append((operation, model))
        yield self

    def usage(self, value: object) -> None:
        pass

    def display(
        self,
        kind: str | None = None,
        params: Mapping[str, object] | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.displays.append((kind, dict(params or {}), details))


def test_step_passes_kind_name_and_result_to_the_observer() -> None:
    """工程の開始で種類と工程名、終わりで LLM の回数・状態・工程の詳細を観測に渡す。"""
    recorder = _Recorder()
    with bind_trace_observer(recorder.observe), _execution_step("根拠確認", "目的の説明") as step:
        step.status = "該当なし"
        step.field("検索文", "合成の検索文")
    assert recorder.calls == [("stage", "根拠確認")]
    (start_kind, start_params, start_details), (end_kind, end_params, end_details) = recorder.displays
    assert (start_kind, start_params, start_details) == ("answer_step", {"name": "根拠確認"}, None)
    assert end_kind == "answer_step"
    assert end_params == {"llm_calls": 0, "status": "該当なし"}
    assert end_details == {
        "fields": [
            {"label": "目的", "value": "目的の説明"},
            {"label": "検索文", "value": "合成の検索文"},
        ]
    }


def test_failed_step_reports_the_interrupted_status_before_the_span_closes() -> None:
    """例外で止まった工程も、観測を閉じる前に中断の状態を渡す。"""
    recorder = _Recorder()
    with (
        bind_trace_observer(recorder.observe),
        pytest.raises(ValueError),
        _execution_step("文書検索"),
    ):
        raise ValueError("合成の失敗")
    assert recorder.displays[-1][1] == {"llm_calls": 0, "status": "エラーで中断"}


def test_observer_without_display_is_a_no_op() -> None:
    """観測が無いときの既定の観測は、表示用のデータを受けても何もしない。"""
    with observe_call("stage", "合成の工程") as observation:
        observation.display("answer_step", {"name": "合成の工程"})
