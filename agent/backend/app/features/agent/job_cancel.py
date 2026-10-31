"""Run の取消で、その Run が始めた NL2SQL のジョブを止める（#3318）。

Run の取消（#2691）は実行中の読み取りの MCP の呼び出しを切るが、NL2SQL のジョブ（`nl2sql_query` が
作る pending / running のジョブ）は呼び先の worker で動き続ける。取消が確定した Run について、Run の
step に残ったジョブの最後の状態を見て、まだ終わっていないジョブに Control Plane が
`nl2sql_cancel_job` を Run の利用者のサービストークンで送る
（`builtin_runtime.cancel_run_nl2sql_jobs`）。

- 対象は組み込みの NL2SQL の接続（`nl2sql`）の `nl2sql_query` / `nl2sql_get_job` の成功した結果の
  `job_id` で、同じ job の最後の結果の `status` が pending / running のものだけ。取消を送った job
  （`nl2sql_cancel_job` の step）は除く（取消は冪等なので、重なって送っても結果は変わらない）。
- 送るのは、取消の後でも呼ぶ後始末（`cleanup`）の step。ポリシーの「拒否」は守り、承認は待たない。
  trace_id は `RUN_CANCEL_TRACE_PREFIX` で始め、モデルの予算・ツールで確かめた回数に数えない。
- RAG などの読み取りのツールの扱い（接続を切る。#2691）は変えない。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.features.agent.data_scope import NL2SQL_CONNECTION_ID
from app.features.agent.tools import MCP_TOOL_SEPARATOR, mcp_base_tool_name

NL2SQL_CANCEL_JOB_TOOL = "nl2sql_cancel_job"
# Run の取消の後に Control Plane が送る後始末の step の trace_id の接頭辞。
RUN_CANCEL_TRACE_PREFIX = "run_cancel_"
# 結果に job_id と状態を返す NL2SQL のツール（`tools._PRODUCT_JOB_FOLLOW_UPS` と同じ）。
_JOB_TOOLS = frozenset({"nl2sql_query", "nl2sql_get_job"})
_IN_FLIGHT_STATUSES = frozenset({"pending", "running"})
# 1 つの Run で止めるジョブの数の上限（step の数の上限より十分小さい）。
MAX_CANCELLED_JOBS = 20


def in_flight_nl2sql_jobs(steps: list[Any]) -> list[str]:
    """Run の step から、最後に見た状態が pending / running で取消を送っていないジョブの ID。"""
    prefix = f"{NL2SQL_CONNECTION_ID}{MCP_TOOL_SEPARATOR}"
    latest: dict[str, str] = {}
    cancelled: set[str] = set()
    for step in steps:
        call = getattr(step, "tool_call", None)
        if call is None or not call.name.startswith(prefix):
            continue
        tool = mcp_base_tool_name(call.name)
        if tool == NL2SQL_CANCEL_JOB_TOOL:
            job_id = call.arguments.get("job_id")
            if isinstance(job_id, str) and job_id:
                cancelled.add(job_id)
            continue
        if tool not in _JOB_TOOLS:
            continue
        result = getattr(step, "tool_result", None)
        output = result.output if result is not None and result.success else None
        if not isinstance(output, Mapping):
            continue
        job_id, status = output.get("job_id"), output.get("status")
        if isinstance(job_id, str) and job_id and isinstance(status, str):
            # 同じ job の続きの取得は後の step が新しい。
            latest.pop(job_id, None)
            latest[job_id] = status
    return [
        job_id
        for job_id, status in latest.items()
        if status in _IN_FLIGHT_STATUSES and job_id not in cancelled
    ][:MAX_CANCELLED_JOBS]


__all__ = [
    "MAX_CANCELLED_JOBS",
    "NL2SQL_CANCEL_JOB_TOOL",
    "RUN_CANCEL_TRACE_PREFIX",
    "in_flight_nl2sql_jobs",
]
