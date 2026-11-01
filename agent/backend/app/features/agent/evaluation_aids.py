"""品質評価で一つずつ外して寄与を比べる実行補助（#69）。

品質評価の job（`EvaluationRequest.disabled_aids`）が、その job の評価の Run（dry-run）だけで補助を
外す。外す印は Run の metadata（`evaluation_disabled_aids`。予約の key）にあり、評価の Run で
なければ読まない（利用者の Run・本番の設定で補助を外さない）。ここに置くのは回答の品質のための
補助だけで、ツール権限・承認・データの範囲・機微情報の保護は外せない。

- `context_compaction`: モデルの入力の上限に収めるための古いツールの結果の縮小をしない
  （全体の既定は `AGENT_CONTEXT_COMPACTION_ENABLED`）。
- `final_validation`: 回答の保存の前の RAG の `rag_validate_answer` の検証をしない
  （`AGENT_FINAL_VALIDATION_ENABLED`）。
- `tool_grounding`: 根拠のツールを呼ばずに作った回答の差し戻し・拒答をしない
  （`AGENT_TOOL_GROUNDING_REQUIRED`）。
"""

from __future__ import annotations

from typing import Literal

from app.settings import get_settings

EvaluationAid = Literal["context_compaction", "final_validation", "tool_grounding"]
# 補助 → 全体の既定の設定の名前。
EVALUATION_AIDS: dict[str, str] = {
    "context_compaction": "agent_context_compaction_enabled",
    "final_validation": "agent_final_validation_enabled",
    "tool_grounding": "agent_tool_grounding_required",
}
DISABLED_AIDS_METADATA_KEY = "evaluation_disabled_aids"


def aid_enabled(run_id: str, aid: EvaluationAid) -> bool:
    """この Run で補助を使うか（全体の設定が on で、評価の Run がその補助を外していない）。"""
    from app.features.agent.runtime import EVALUATION_DRY_RUN_KEY, runtime_repository

    if not getattr(get_settings(), EVALUATION_AIDS[aid]):
        return False
    try:
        metadata = runtime_repository.get_run(run_id).metadata
    except KeyError:
        return True
    disabled = metadata.get(DISABLED_AIDS_METADATA_KEY)
    return not (
        metadata.get(EVALUATION_DRY_RUN_KEY) is True
        and isinstance(disabled, list)
        and aid in disabled
    )
