"""外部への書き込みの結果不明と重複の契約（#72）。

通信の request ID（`ToolCall.trace_id` = SDK の call ID）、承認の対象、業務上 1 回の副作用は別の
単位である。Agent の台帳（Run の step）は呼び先と原子的に commit できないので exactly-once は
保証しない。ここでは「重複を起こさない側に倒す」規則だけを決める。

ツールの分類（`ToolDefinition` から決める。未信頼の注釈だけでは緩めない）:

- 再送してよい（`resend_safe`）: 信頼できる読み取り専用（`readOnlyHint`）か、信頼できる冪等の申告
  （`idempotentHint`）。届いた後の失敗も通信の層で再送し、結果不明の後の呼び直しも妨げない。
- 標準の自動実行（`standard_auto_approve`。`nl2sql_query`）: 副作用は読み取りの job の作成だけ。
  重複しても業務の副作用は無いので、結果不明の後の呼び直しも妨げない（job ID で照合できる）。
- それ以外（`guarded`）: 書き込み・副作用があり得る。次の規則で守る。

`guarded` と冪等のツールの呼び出しには論理操作の ID（`operation_key`）を付け、MCP の
`params._meta` で渡す（引数には足さない。対応しない呼び先は無視する）。ID は Run・利用者・ツール・
引数・call ID から作るので、別の引数・別の利用者・別の Run に同じ ID は出ない。

規則（`guarded` のツール）:

1. 送る前に、実行を始めた記録（step の running と ID）を保存先に書き切る（書けなければ送らない）。
   成功・失敗の記録は従来どおり background で書く（その前に落ちたら 3. で結果不明になる）。
2. 届いた後の失敗（読み取りの timeout・通信の途中の切断・500 / 502 / 504・読めない応答）は
   「結果不明」（`error_details.outcome = "unknown"`）。通信の層で再送しない（#2595）。
3. 起動時に中断した Run の、送ったかもしれない step（running で ID がある）は結果不明にする
   （未実行と断定しない）。
4. 同じ call ID の呼び出しが再び来たら（同じ承認の再開など）、送り直さず記録した結果を返す。
5. 結果不明の操作と同じツール・同じ引数の呼び出しは、ツール権限が自動実行でも人の承認を要する
   （承認 = 人が相手のシステムで照合した、という確認）。承認したら最初と同じ ID で送る（ID で
   重複を判定できる呼び先は二重に反映しない）。無人の Run（MCP・品質評価）は承認できないので
   送らない。

6. 利用者の再実行（`POST /runs/{id}/replay`）は新しい論理操作（新しい ID）。元の Run に結果不明が
   あれば、明示の確認なしには始めない（重複を防げないので）。

権限の判定（ツール権限・データの範囲・承認と引数の照合）は従来どおりで、重複の判定はその代わりに
しない。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from app.features.agent.tools import OUTCOME_UNKNOWN, ToolDefinition, ToolResult

# 送る前の記録を保存先に書けなかった（送っていない。#72）。
INTENT_NOT_PERSISTED_CODE = "tool.intent_not_persisted"
INTENT_NOT_PERSISTED_MESSAGE = (
    "実行の記録を保存できなかったため、外部への書き込みを送りませんでした。"
    "時間をおいてから、もう一度試してください。"
)
# 起動時に中断した Run の、送ったかもしれない書き込み（#72）。
OUTCOME_UNKNOWN_CODE = "tool.outcome_unknown"
OUTCOME_UNKNOWN_MESSAGE = (
    "処理の中断で、外部への書き込みの結果を受け取れませんでした。相手のシステムで反映されたかを"
    "確かめてください（未実行とは限りません）。"
)
# 結果不明の書き込みで Run を止めたときの文（呼び先の文の「再実行してください」の代わり）。
OUTCOME_UNKNOWN_RUN_MESSAGE = (
    "外部への書き込みの結果が不明です（相手で反映済みかもしれません）。相手のシステムで反映されたかを"
    "確かめてから、必要なときだけ再実行してください。"
)
# モデルへの案内（結果不明の失敗の結果に足す）。

OUTCOME_UNKNOWN_NEXT_STEP = (
    "この操作は相手に届いた可能性があり、反映されたかは分かりません。同じ操作を繰り返さないで"
    "ください（同じ引数で呼ぶと人の承認が要ります）。状態を読み取るツールがあれば確かめ、無ければ"
    "結果が不明であることを利用者に伝えてください。"
)

_SENT_STATUSES = frozenset({"running", "completed"})


def has_side_effects(definition: ToolDefinition) -> bool:
    """論理操作の ID を付けるツールか（信頼できる読み取り専用・標準の自動実行以外）。"""
    return not definition.trusted_read_only and not definition.standard_auto_approve


def guarded(definition: ToolDefinition) -> bool:
    """結果不明の後の呼び直し・同じ call ID の送り直しを止めるツールか（規則 1〜5）。"""
    return has_side_effects(definition) and not definition.resend_safe


def operation_key(
    *,
    run_id: str,
    user_uuid: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str,
) -> str:
    """論理操作の ID（同じ Run・利用者・ツール・引数・call ID だけが同じ値になる）。"""
    material = json.dumps(
        [run_id, user_uuid or "", tool_name, arguments, call_id],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"op_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def is_outcome_unknown(result: ToolResult | None) -> bool:
    return (
        result is not None
        and not result.success
        and isinstance(result.error_details, dict)
        and result.error_details.get("outcome") == OUTCOME_UNKNOWN
    )


def _status(step: Any) -> str:
    return str(getattr(step.status, "value", step.status))


def step_outcome_unknown(step: Any) -> bool:
    """結果不明の書き込みの step か（ID のある呼び出しの、届いた後の失敗・中断）。"""
    call = step.tool_call
    return bool(call is not None and call.operation_key and is_outcome_unknown(step.tool_result))


def sent_step(steps: Iterable[Any], call_id: str | None) -> Any | None:
    """この call ID で既に送った（送ったかもしれない）書き込みの step（規則 4）。"""
    if not call_id:
        return None
    for step in steps:
        call = step.tool_call
        if call is None or call.trace_id != call_id or not call.operation_key:
            continue
        if _status(step) in _SENT_STATUSES or step_outcome_unknown(step):
            return step
    return None


def unresolved_unknown(steps: Iterable[Any], name: str, arguments: dict[str, Any]) -> Any | None:
    """同じツール・同じ引数の最後の書き込みが結果不明なら、その step（規則 5）。

    解消するのは、後で同じ操作が成功したときだけ（承認待ち・送る前の拒否・実行中・失敗は
    見ない。送り直しが失敗しても、最初の操作が相手で反映されたかは分からないまま）。
    """
    latest = None
    for step in steps:
        call = step.tool_call
        if (
            call is None
            or not call.operation_key
            or call.name != name
            or call.arguments != arguments
        ):
            continue
        if step_outcome_unknown(step) or (
            step.tool_result is not None and step.tool_result.success
        ):
            latest = step

    return latest if latest is not None and step_outcome_unknown(latest) else None


def interrupted_result(step: Any) -> ToolResult:
    """起動時に中断した、送ったかもしれない書き込みの結果（規則 3）。"""
    call = step.tool_call
    return ToolResult(
        name=call.name,
        success=False,
        error=OUTCOME_UNKNOWN_MESSAGE,
        error_code=OUTCOME_UNKNOWN_CODE,
        error_details={"outcome": OUTCOME_UNKNOWN, "operation_key": call.operation_key},
    )


def approval_reason(name: str) -> str:
    """結果不明の操作を送り直す承認の理由（承認の画面に出す。規則 5）。"""
    return (
        f"前回の {name} は結果が不明です（相手で反映済みかもしれません）。相手のシステムで"
        "反映されていないことを確かめてから承認してください。"
    )


def unknown_outcome_payload(operation: str | None) -> dict[str, Any]:
    """モデルへ返す結果に足す、結果不明の項目。"""
    payload: dict[str, Any] = {"outcome": OUTCOME_UNKNOWN, "next_step": OUTCOME_UNKNOWN_NEXT_STEP}
    if operation:
        payload["operation_key"] = operation
    return payload


def unknown_steps(steps: Iterable[Any], *, run_ended: bool) -> list[Any]:
    """Run の結果不明の書き込みの step（画面・再実行の確認に使う）。

    終わった Run（取消の後に結果が届かないまま止まったなど）の実行中の書き込みも含める。
    """
    return [
        step
        for step in steps
        if step_outcome_unknown(step)
        or (
            run_ended
            and _status(step) == "running"
            and step.tool_call is not None
            and step.tool_call.operation_key
        )
    ]
