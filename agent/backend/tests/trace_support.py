"""テストの処理の経過（共通の span の表の snapshot。#3226 の案 A）の読み出し。

テストの保存先は memory なので、span は process の中の表（`trace_spans.memory`）にある。画面の
API（`GET /api/runs/{run_id}/trace`）と同じく、Run の trace id で読み、Run の状態と工程の詳細を
合わせてイベントにする。
"""

from __future__ import annotations

from pr_backend_core.chat_progress import ChatProgressEvent, parse_chat_progress_events

from app.features.agent.chat_progress import run_trace_events
from app.features.agent.runtime import RunState
from app.features.agent.trace_spans import trace_spans


def run_progress_events(run: RunState) -> list[ChatProgressEvent]:
    """Run の処理の経過のイベント（全員の span。system_admin の見え方）。"""
    spans = trace_spans.fetch(run.trace_id, viewer_user_uuid=None) if run.trace_id else []
    return parse_chat_progress_events(run_trace_events(run, spans))
