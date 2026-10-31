"""Run・業務 Agent と画面で変えた定義の保存先の状態（#839）。

保存先は `AGENT_RUNTIME_REPOSITORY_BACKEND`（Run の repository と定義の store が同じ値に従う。
#764）。`memory` はプロセスの中だけに持ち、バックエンドの再起動で消える。画面はこの状態を見て、
消えることと直す場所（保存先の設定か、データベースの設定か）を案内する。

- `persistent` は定義の store（`control_plane_store`）が保存するか。Run だけを file に保存する
  構成（`memory` + `AGENT_RUNTIME_SNAPSHOT_PATH`）でも、定義は消えるので `False` にする。
- `database_configured` は共通の `PLATFORM_ORACLE_*` の設定がそろっているか（接続は試さない。
  システム設定と同じ `database_readiness` の判定）。
- 既定の `auto` は起動時に DB の設定を見て決める（`storage_backend`）。起動の後に DB を設定した
  ときは、再起動するまで memory のまま（`reason=restart_required`）。DB の設定はあるが接続できない
  ときは memory にせず、接続できた時点で読み込む（#1212。それまで業務の API は 503）。
- 起動時の読み込みで直した Run（`repaired_runs`）と、直せずに読み込まず保存先に元の JSON のまま
  残している Run・業務 Agent（`skipped_runs` / `skipped_agents`）の数を返す（#853）。
- `auto` で checkpoint 全体が読めず memory で起動したときは `reason=checkpoint_invalid`（#853）。
- 接続先・資格情報は返さない。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.features.agent import storage_backend
from app.features.agent.control_plane_store import get_control_plane_store
from app.settings import get_settings

StorageBackend = Literal["memory", "file", "oracle_checkpoint", "oracle_normalized"]
# 保存されない理由。
# - `memory_backend`: DB は設定済みで、保存先に memory（または保存先のパスの無い file）を
#   明示している
# - `restart_required`: 保存先は `auto` で DB も設定済みだが、起動時は DB が未設定だった（再起動で
#   Oracle になる。起動時に接続できなかっただけなら memory にせず、接続できた時点で読み込む。#1212）
# - `database_not_configured`: DB の設定がそろっていない
# - `checkpoint_invalid`: 保存先は `auto` で DB に接続できたが、保存済みの checkpoint 全体が読めず
#   （JSON の破損・未対応の版）、上書きしないよう memory で起動した（#853）
NotPersistentReason = Literal[
    "memory_backend", "restart_required", "database_not_configured", "checkpoint_invalid"
]


class RuntimeStorageStatus(BaseModel):
    """保存先の状態（`GET /api/runtime/storage`）。"""

    # 実際に使っている保存先（`oracle` は `oracle_checkpoint` に、保存先のパスの無い file は
    # memory にそろえる）。
    backend: StorageBackend
    # 設定の値（`AGENT_RUNTIME_REPOSITORY_BACKEND`。既定は `auto`）。
    configured_backend: str
    # 業務 Agent・スキル・MCP 接続・実行・自動実行・品質評価などが再起動の後も残るか。
    persistent: bool
    # 共通の PLATFORM_ORACLE_* の設定がそろっているか（接続は試さない）。
    database_configured: bool
    # persistent が False の理由（True のときは None）。
    reason: NotPersistentReason | None = None
    # 起動時の読み込みで整合しない状態を直した Run の数（#853）。
    repaired_runs: int = 0
    # 直せずに読み込まなかった Run・業務 Agent の数（保存先に元の JSON のまま残す。#853）。
    skipped_runs: int = 0
    skipped_agents: int = 0


def effective_backend() -> StorageBackend:
    """実際に使う保存先の名前にそろえる。"""
    backend = storage_backend.resolved_backend()
    if backend in storage_backend.ORACLE_BACKENDS:
        return "oracle_normalized" if backend == "oracle_normalized" else "oracle_checkpoint"
    if backend in storage_backend.FILE_BACKENDS and get_settings().agent_runtime_snapshot_path:
        return "file"
    return "memory"


def database_configured() -> bool:
    return storage_backend.database_configured()


def runtime_storage_status() -> RuntimeStorageStatus:
    persistent = bool(get_control_plane_store().persistent)
    configured = database_configured()
    reason: NotPersistentReason | None = None
    if not persistent:
        if storage_backend.fallback_reason() == storage_backend.FALLBACK_CHECKPOINT_INVALID:
            reason = "checkpoint_invalid"
        elif not configured:
            reason = "database_not_configured"
        elif storage_backend.is_auto():
            reason = "restart_required"
        else:
            reason = "memory_backend"
    return RuntimeStorageStatus(
        backend=effective_backend(),
        configured_backend=storage_backend.configured_backend(),
        persistent=persistent,
        database_configured=configured,
        reason=reason,
        **_runtime_health(),
    )


def _runtime_health() -> dict[str, int]:
    """Run の repository の読み込みの結果（取得できなければ 0。状態の取得を止めない）。"""
    from app.features.agent.runtime import runtime_repository

    try:
        return runtime_repository.storage_health().model_dump()
    except Exception:  # noqa: BLE001 - 件数の取得の失敗で保存先の状態を返せなくしない
        return {}
