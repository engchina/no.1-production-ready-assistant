"""合成データ生成の永続状態。要求件数と確認済み書込み件数を分離する。"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import SyntheticDataGenerateRequest

RunStatus = Literal[
    "pending", "running", "verifying", "completed", "partial", "failed", "no_data", "unknown"
]
TERMINAL = {"completed", "partial", "failed", "no_data"}
# 確認用データ（一時表）を残し、適用・破棄を受け付ける期間（処理の終了から）。生成の記録そのものは
# 期間の後も消さない（実行の記録は無期限に残す。#3235）。
REVIEW_WINDOW = timedelta(hours=24)


def now() -> str:
    return datetime.now(UTC).isoformat()


class SyntheticRunRequest(SyntheticDataGenerateRequest):
    idempotency_key: str = Field(min_length=16, max_length=128)


class SyntheticApplyRequest(BaseModel):
    confirmation: str
    previews: dict[str, str]


class SyntheticTarget(BaseModel):
    table_name: str
    requested_rows: int
    loaded_rows: int | None = None
    status: str = "pending"
    error: str = ""


class SyntheticRun(BaseModel):
    run_id: str
    actor_id: str
    context_id: str
    idempotency_key: str
    request_hash: str
    request: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = "pending"
    targets: list[SyntheticTarget]
    created_at: str = Field(default_factory=now)
    started_at: str | None = None
    finished_at: str | None = None
    checked_at: str | None = None
    message: str = ""
    session: dict[str, Any] = Field(default_factory=dict)
    operation_ids: list[int] = Field(default_factory=list)
    failure_phase: Literal["validation"] | None = None
    execution_returned: bool = False
    worker_id: str = ""
    version: int = 0
    # 旧履歴は直接生成。新規受付は必ず preview=True にする（request から選択不可）。
    preview: bool = False
    review_status: Literal["pending", "ready", "applied", "discarded"] = "pending"
    applied_at: str | None = None
    staging: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def review_expired(self, at: datetime | None = None) -> bool:
        """確認用データの期間（`REVIEW_WINDOW`）を過ぎたか。過ぎた記録は閲覧だけにする。"""
        if self.status not in TERMINAL:
            return False
        ended = datetime.fromisoformat(self.finished_at or self.created_at)
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=UTC)
        return ended < (at or datetime.now(UTC)) - REVIEW_WINDOW

    def public(self) -> dict[str, Any]:
        return {
            **self.model_dump(
                exclude={
                    "actor_id",
                    "worker_id",
                    "context_id",
                    "idempotency_key",
                    "request_hash",
                    "request",
                    "session",
                    "staging",
                }
            ),
            "review_expired": self.review_expired(),
        }
