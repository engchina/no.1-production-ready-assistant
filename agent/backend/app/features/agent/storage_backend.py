"""保存先（`AGENT_RUNTIME_REPOSITORY_BACKEND`）の決定（#839）。

Run の repository（`runtime`）・画面で変えた定義の store（`control_plane_store`）・Run の事実
（`run_facts_store`）は、ここで決めた 1 つの保存先に従う。

- `auto`（既定）: 共通の `PLATFORM_ORACLE_*` の設定がそろっていれば `oracle_checkpoint`、
  そろっていなければ `memory`。判定は起動の後の最初の 1 回だけ行い、プロセスの間は変えない
  （Run と定義の保存先がずれないように）。起動の後に DB を設定したときは、再起動で Oracle になる。
- `auto` で Oracle を選んだが DB に接続できない（ADB の停止中など）ときは、`memory` に縮退しない。
  backend は起動し、Run の repository と定義は DB に接続できるようになった時点で読み込む（#1212）。
- `auto` で Oracle を選んだが、checkpoint 全体が読めない（JSON の破損・未対応の版）ときは、
  checkpoint を上書きしないよう `memory` にする（`fall_back_to_memory`。`fallback_reason()` が
  `checkpoint_invalid`。#853）。1 件の Run の不整合では縮退しない（Run 単位で直す・退避する）。
- `memory` / `file` / `oracle_checkpoint` / `oracle_normalized` を明示したときは、その値に従う
  （`file` は `AGENT_RUNTIME_SNAPSHOT_PATH` が要る）。
"""

from __future__ import annotations

import logging
import threading

from pr_system_settings.database import database_readiness

from app.settings import get_settings

logger = logging.getLogger(__name__)

AUTO = "auto"
ORACLE_BACKENDS = frozenset({"oracle", "oracle_checkpoint", "oracle_normalized"})
FILE_BACKENDS = frozenset({"file", "file_snapshot"})
MEMORY_BACKENDS = frozenset({"memory", "in_memory"})

_lock = threading.Lock()
# `auto` の判定の結果（None はまだ判定していない）。
_auto_decision: str | None = None
# `auto` で Oracle を選んだが checkpoint が読めず memory にしたか。
_fell_back = False
# memory にした理由（checkpoint が読めない。#853）。
FALLBACK_CHECKPOINT_INVALID = "checkpoint_invalid"
_fallback_reason: str | None = None


def configured_backend() -> str:
    """設定の値（小文字・前後の空白なし）。空は `auto`。"""
    return get_settings().agent_runtime_repository_backend.strip().lower() or AUTO


def database_configured() -> bool:
    """共通の DB の設定がそろっているか（システム設定・DB ゲートと同じ判定。接続は試さない）。"""
    try:
        return database_readiness(get_settings()) == "ok"
    except Exception:  # 設定の読み取りの失敗は「未設定」として扱う（起動・画面を止めない）。
        logger.warning("agent_storage_database_readiness_failed", exc_info=True)
        return False


def resolved_backend() -> str:
    """実際に使う保存先（`auto` は判定した結果。明示した値はそのまま）。"""
    global _auto_decision
    configured = configured_backend()
    if configured != AUTO:
        return configured
    with _lock:
        if _auto_decision is None:
            _auto_decision = "oracle_checkpoint" if database_configured() else "memory"
            logger.info("agent_storage_backend_auto_resolved", extra={"backend": _auto_decision})
        return _auto_decision


def is_auto() -> bool:
    return configured_backend() == AUTO


def fell_back_to_memory() -> bool:
    return _fell_back


def fallback_reason() -> str | None:
    """memory にした理由（`checkpoint_invalid`。縮退していなければ None）。"""
    return _fallback_reason if _fell_back else None


def fall_back_to_memory(reason: str = FALLBACK_CHECKPOINT_INVALID) -> None:
    """`auto` で選んだ Oracle の checkpoint が読めない。以降はこのプロセスでは memory にする。"""
    global _auto_decision, _fell_back, _fallback_reason
    with _lock:
        _auto_decision = "memory"
        _fell_back = True
        _fallback_reason = reason


def reset() -> None:
    """判定をやり直す（テスト）。"""
    global _auto_decision, _fell_back, _fallback_reason
    with _lock:
        _auto_decision = None
        _fell_back = False
        _fallback_reason = None
