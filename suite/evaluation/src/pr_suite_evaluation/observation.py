"""Observation: Target が 1 ケースを流して観測したもの（#3240）。

外枠（実行の状態・所要時間・参照・読んだ出力の版・失敗の種類）だけを Target の間で共有し、
回答の本文・根拠・ツールの軌跡などの中身は Target ごとの `payload`（公開の契約の出力そのもの）に
置く。採点はこの保存した観測から行うので、モデルを呼び直さずに採点し直せる（`score` の命令）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

OBSERVATION_SCHEMA_VERSION = 1

# 往復の観測の状態。
# - completed: 回答まで終わった
# - confirmation: 回答の前の確認（Target が利用者への確認を返した）
# - failed / cancelled / waiting_approval: Target の実行がその状態で終わった
# - timeout: 時間内に終わらなかった（Target の取消の結果を refs に残す）
# - target_error: Target を呼べなかった・出力を読めなかった（HTTP・ツールのエラー・知らない版）
TurnStatus = Literal[
    "completed",
    "confirmation",
    "failed",
    "cancelled",
    "waiting_approval",
    "timeout",
    "target_error",
]


class TurnObservation(BaseModel):
    """1 往復の観測。"""

    turn: int = Field(..., ge=0)
    status: TurnStatus
    # Target の実行の状態の語（Agent の Run の status など。target_error は null）。
    target_status: str | None = None
    elapsed_ms: float = 0.0
    # 参照（Agent の run_id・thread_id など。本文は含めない）。
    refs: dict[str, str | None] = Field(default_factory=dict)
    # 読んだ公開の出力の版（ツールの名前 → schema_version）。
    output_versions: dict[str, int] = Field(default_factory=dict)
    # 失敗の種類と文（target_error・timeout など。質問の本文・資格情報は含めない）。
    error_type: str | None = None
    error_message: str | None = None
    # Target が送ったもの（Agent の agent_chat の引数・確認への答えなど）。
    request: dict[str, Any] = Field(default_factory=dict)
    # Target の公開の契約の出力（Agent は agent_get_run・agent_get_run_observation の出力）。
    payload: dict[str, Any] = Field(default_factory=dict)


class CaseObservation(BaseModel):
    """1 ケースの観測（往復の順）。"""

    schema_version: int = OBSERVATION_SCHEMA_VERSION
    case_id: str
    target_kind: str
    turns: list[TurnObservation] = Field(default_factory=list)

    @property
    def final(self) -> TurnObservation | None:
        return self.turns[-1] if self.turns else None
