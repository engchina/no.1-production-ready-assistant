"""Run の claim（dispatcher の lease）と実行の世代（fencing token）の契約（#2606）。

runtime-dispatcher は queued の Run を期限付きの lease で claim してから実行を始める。lease が
切れた後に別の worker が同じ Run を claim し直すことがあるので、実行の開始と書き戻しを次の
契約で守る。表・列は足さず、Run の予約の metadata（`_` で始まる key）に持つ。

- claim は、所有者（`worker_id`）・一意な token・単調に増える世代（`generation`）を lease に
  書く。世代は lease と実行の記録の大きい方に 1 を足す（承認の後に queued に戻った Run を
  claim し直すと増える）。
- 実行の開始は、queued の Run だけを、今の claim（token と所有者が一致し、期限 `expires_at` が今より
  後）に対して 1 回だけ受け付ける。開始で lease を外し、同じ token と世代を実行の記録に移す。期限の
  切れた・claim し直された旧所有者の開始と、同じ claim の 2 回目の開始は受け付けない。in_process の
  開始（claim なし）は、期限内の lease を別の worker が持つ Run を開始しない。
- 実行の区切りの書き戻し（完了・失敗・承認待ちの保存〔SDK の状態〕）は、実行中（running）で、実行の
  記録の token が書き戻す実行と同じときだけ受け付ける（`execution_fence` で実行の文脈に token を
  持たせる）。別の実行が始まった・自分の実行が区切りを終えた後の遅れた書き戻しは無視する。

この fencing は、この保存先の中で同じ Run の実行が重ならないようにするだけで、外部のサービスへの
副作用の重複（呼び先に届いた後の失敗・結果の不明）を防ぐものではない（#2601）。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

# dispatcher の claim の lease（queued の Run だけが持つ。開始で外す）。
LEASE_METADATA_KEY = "_runtime_dispatch_lease"
# 実行の記録（開始した実行の token・世代。次の開始で置き換える）。
EXECUTION_METADATA_KEY = "_runtime_execution"

# 開始を受け付けない理由（ログの `reason`）。
REJECT_NOT_QUEUED = "not_queued"
REJECT_LEASE_MISSING = "lease_missing"
REJECT_LEASE_MISMATCH = "lease_mismatch"
REJECT_LEASE_EXPIRED = "lease_expired"
REJECT_LEASE_HELD = "lease_held_by_worker"


@dataclass(frozen=True)
class RunClaim:
    """dispatcher が claim した Run の所有の証明（開始の条件に渡す）。"""

    run_id: str
    worker_id: str
    token: str
    generation: int


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def lease_of(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return _mapping(metadata.get(LEASE_METADATA_KEY))


def execution_of(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return _mapping(metadata.get(EXECUTION_METADATA_KEY))


def generation_of(metadata: Mapping[str, Any]) -> int:
    """Run の今の世代（lease と実行の記録の大きい方。どちらも無ければ 0）。"""
    lease = lease_of(metadata) or {}
    execution = execution_of(metadata) or {}
    return max(_int(lease.get("generation")), _int(execution.get("generation")))


def _expires_at(lease: Mapping[str, Any]) -> datetime | None:
    value = lease.get("expires_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def lease_active(metadata: Mapping[str, Any], now: datetime) -> bool:
    """期限内の lease があるか（期限 `expires_at` が今より後。期限ちょうどは切れている）。"""
    lease = lease_of(metadata)
    if lease is None:
        return False
    expires = _expires_at(lease)
    return expires is not None and expires > now


def new_lease(
    metadata: Mapping[str, Any], worker_id: str, *, now: datetime, lease_seconds: int
) -> dict[str, Any]:
    """claim の lease（所有者・一意な token・次の世代・期限）。"""
    expires = now + timedelta(seconds=max(1, lease_seconds))
    return {
        "worker_id": worker_id,
        "token": uuid4().hex,
        "generation": generation_of(metadata) + 1,
        "claimed_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }


def claim_of(run_id: str, metadata: Mapping[str, Any]) -> RunClaim | None:
    """claim した Run の lease から、開始に渡す claim を作る（lease が無い・不完全なら None）。"""
    lease = lease_of(metadata)
    if lease is None:
        return None
    worker_id, token = lease.get("worker_id"), lease.get("token")
    if not isinstance(worker_id, str) or not isinstance(token, str) or not token:
        return None
    return RunClaim(
        run_id=run_id, worker_id=worker_id, token=token, generation=_int(lease.get("generation"))
    )


def begin_rejection(
    metadata: Mapping[str, Any], claim: RunClaim | None, *, now: datetime
) -> str | None:
    """queued の Run の開始を受け付けない理由（受け付けるなら None）。状態の判定は呼び出し側。"""
    if claim is None:
        # in_process の開始。期限内の lease を持つ worker がいれば、その worker に任せる。
        return REJECT_LEASE_HELD if lease_active(metadata, now) else None
    lease = lease_of(metadata)
    if lease is None:
        return REJECT_LEASE_MISSING
    if (
        lease.get("token") != claim.token
        or lease.get("worker_id") != claim.worker_id
        or _int(lease.get("generation")) != claim.generation
    ):
        return REJECT_LEASE_MISMATCH
    if not lease_active(metadata, now):
        return REJECT_LEASE_EXPIRED
    return None


def start_execution(
    metadata: dict[str, Any], claim: RunClaim | None, *, now: datetime
) -> dict[str, Any]:
    """開始で lease を外し、実行の記録（token・世代）を置く。置いた記録を返す。

    claim があればその token と世代を引き継ぐ。in_process の開始は新しい token と次の世代にする。
    """
    if claim is not None:
        token, generation, worker_id = claim.token, claim.generation, claim.worker_id
    else:
        token, generation, worker_id = uuid4().hex, generation_of(metadata) + 1, None
    metadata.pop(LEASE_METADATA_KEY, None)
    execution: dict[str, Any] = {
        "token": token,
        "generation": generation,
        "worker_id": worker_id,
        "started_at": now.isoformat(),
    }
    metadata[EXECUTION_METADATA_KEY] = execution
    return execution


def execution_token(metadata: Mapping[str, Any]) -> str | None:
    execution = execution_of(metadata)
    token = execution.get("token") if execution is not None else None
    return token if isinstance(token, str) and token else None


# ---- 実行の文脈（書き戻しの fencing） --------------------------------------------------------

# この文脈で動いている実行（Run の ID と実行の token）。asyncio の task・`asyncio.to_thread` に
# 引き継がれる。実行の外（API の操作など）は None で、書き戻しの検査をしない。
_current_execution: ContextVar[tuple[str, str] | None] = ContextVar(
    "agent_runtime_current_execution", default=None
)


def bind_execution(run_id: str, metadata: Mapping[str, Any]) -> Token[tuple[str, str] | None]:
    """開始した実行を、この文脈に結び付ける（戻り値を `release_execution` に渡す）。"""
    token = execution_token(metadata)
    return _current_execution.set((run_id, token) if token is not None else None)


def release_execution(binding: Token[tuple[str, str] | None]) -> None:
    _current_execution.reset(binding)


@contextmanager
def execution_fence(run_id: str, metadata: Mapping[str, Any]) -> Iterator[None]:
    binding = bind_execution(run_id, metadata)
    try:
        yield
    finally:
        release_execution(binding)


def current_execution_token(run_id: str) -> str | None:
    """この文脈が `run_id` の実行なら、その token（実行の外・別の Run なら None）。"""
    current = _current_execution.get()
    if current is None or current[0] != run_id:
        return None
    return current[1]
