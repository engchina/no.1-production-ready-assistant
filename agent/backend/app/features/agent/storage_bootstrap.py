"""Run の repository・画面で変えた定義・履歴の読み込み（#1212）。

DB に接続できないあいだも backend は起動する（RAG / NL2SQL と同じ。画面は DB ゲートで案内する）。
読み込みは起動時にバックグラウンドで始めて一定の間隔で再試行し、業務の API は読み込みが済むまで
最初の要求で読み込みを試みる（`require_agent_storage`）。DB が起動すれば、再起動せずに読み込む。

- Run の repository（`runtime.get_runtime_repository`）→ 中断した Run の失敗（in_process だけ。
  #1894）→ 定義の復元（`restore_control_plane`）→ 履歴の準備（評価の整理と Run の事実の
  backfill）の順に 1 回だけ行い、済んだら `is_ready()`。
- DB に接続できない（接続のエラー）ときは `AgentRuntimeStorageUnavailableError`（API は 503）。
  済んだ段階は持ち越し、次の試行は残りから行う。
- 定義の読み込みの接続以外のエラー（権限など）は、今までどおり記録して先へ進む（画面を止めない）。
- 失敗の直後（`FAILURE_COOLDOWN_SECONDS`）は接続を試さずに同じ理由を返す。DB の停止中に要求ごとに
  接続を待たない（wallet の接続記述子の再試行で、1 回の接続に 1 分以上かかることがある）。
- 別のスレッドが読み込んでいるあいだの要求は `WAIT_SECONDS` まで待ち、超えたら 503 にする（要求の
  スレッドを塞ぎ続けない）。
- DB の状態・システム設定・認証・ユーザーとロールの API は読み込みを待たない（`requires_storage`）。

データを書き換える migration（システムテーブルの「作成・更新」・全再作成）は `rewrite_saved_data` で
包む（#1844）。書き換えの間は読み込み・Run の repository の変更・定義の保存を止め、書き換えの後に
同じプロセスのメモリを保存先から読み直す。別のプロセス（dispatcher）のメモリは読み直せないので、
再起動が要ることを結果で返す。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Collection
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pr_backend_core.oracle_errors import is_oracle_connection_error, oracle_error_codes

from app.features.agent import control_plane_store, runtime, storage_backend
from app.features.agent.runtime import AgentRuntimeStorageUnavailableError
from app.settings import get_settings

logger = logging.getLogger(__name__)

# 起動時のバックグラウンドの読み込みの再試行の間隔（秒）。
RETRY_INTERVAL_SECONDS = 15.0
# 失敗の直後に、接続を試さずに同じ理由を返す時間（秒）。
FAILURE_COOLDOWN_SECONDS = 5.0
# 別のスレッドが読み込んでいるあいだ、要求が待つ上限（秒）。
WAIT_SECONDS = 10.0

# 読み込みを待たない API（DB ゲート・システム設定・認証・ユーザーとロール）。`/` 区切りの前方一致。
STORAGE_INDEPENDENT_PATHS = (
    "/api/health",
    "/api/ready",
    "/api/auth",
    "/api/settings/database",
    "/api/settings/model",
    "/api/settings/oci",
    "/api/settings/upload-storage",
    "/api/security/users",
    "/api/security/roles",
    "/api/security/permissions",
)

_LOADING_MESSAGE = (
    "Agent の保存データを読み込んでいます。しばらくしてから、もう一度実行してください。"
)
_LOAD_FAILED_MESSAGE = (
    "Agent の保存データを読み込めません。バックエンドのログ（agent_storage_load_failed）を"
    "確認してください。"
)

_lock = threading.Lock()
_ready = False
_control_plane_restored = False
_interrupted_runs_checked = False
# このプロセスの起動の時刻（import の時刻）。これより前に作った・変えた `running` / `queued` の
# Run は前のプロセスが実行していたもので、このプロセスでは続かない（#1894）。
_process_started_at: datetime = datetime.now(UTC)
# 直前の失敗（時刻・利用者に返す理由）。
_failure: tuple[float, str] | None = None
_monotonic: Callable[[], float] = time.monotonic
_stop = threading.Event()
_thread: threading.Thread | None = None


def requires_storage(path: str) -> bool:
    """この API が Run の repository と定義の読み込みを待つか。"""
    return not any(
        path == prefix or path.startswith(f"{prefix}/") for prefix in STORAGE_INDEPENDENT_PATHS
    )


def is_ready() -> bool:
    return _ready


def ensure_ready() -> None:
    """読み込む（済んでいれば何もしない）。

    読み込めなければ `AgentRuntimeStorageUnavailableError`。
    """
    global _ready, _failure
    if _ready:
        return
    if not _lock.acquire(timeout=WAIT_SECONDS):
        raise AgentRuntimeStorageUnavailableError(_LOADING_MESSAGE)
    try:
        if _ready:
            return
        if _failure is not None and _monotonic() - _failure[0] < FAILURE_COOLDOWN_SECONDS:
            raise AgentRuntimeStorageUnavailableError(_failure[1])
        try:
            _load_locked()
        except AgentRuntimeStorageUnavailableError as exc:
            _failure = (_monotonic(), str(exc))
            raise
        except Exception as exc:
            # 接続以外のエラー（明示した Oracle の checkpoint の破損・SQL・権限）。直し方を書いた
            # 例外（checkpoint の破損）はその文を返し、それ以外はログへ案内する。
            logger.exception("agent_storage_load_failed")
            message = (
                str(exc)
                if isinstance(exc, runtime.AgentRuntimeSnapshotCorruptError)
                else _LOAD_FAILED_MESSAGE
            )
            _failure = (_monotonic(), message)
            raise AgentRuntimeStorageUnavailableError(message) from exc
        _failure = None
        _ready = True
    finally:
        _lock.release()
    logger.info("agent_storage_ready", extra={"backend": storage_backend.resolved_backend()})


def _load_locked() -> None:
    global _control_plane_restored, _interrupted_runs_checked
    repository = runtime.get_runtime_repository()
    if storage_backend.fell_back_to_memory():
        # checkpoint が読めず memory にした。定義も同じ保存先にそろえる（作った store を捨てる）。
        control_plane_store.set_control_plane_store(None)
    if not _interrupted_runs_checked:
        _fail_interrupted_runs(repository)
        _interrupted_runs_checked = True
    if not _control_plane_restored:
        _restore_control_plane()
        _control_plane_restored = True
    _prepare_history()


def _fail_interrupted_runs(repository: runtime.AgentRuntimeRepositoryContract) -> None:
    """前のプロセスが実行していて、再起動で止まった Run を失敗にする（#1894）。

    in_process では Run の実行は backend のプロセスの asyncio の task だけで、再起動で task が
    消えると Run は `running` / `queued` のまま終わらない（チャットは待ち続け、自動実行は前回の
    Run の終わりを待って次を作らない）。このプロセスの起動より前に作った・変えた Run だけを
    対象にする（起動の後の Run は、このプロセスが始めたものか、別のプロセスが動かしている途中）。
    承認待ち・確認待ちは保存した SDK の状態から再開できるので触らない。

    external（runtime-dispatcher）のときは触らない（dispatcher が lease で claim する）。
    失敗の保存に失敗しても読み込みは止めない（メモリの Run は失敗にしたので、次の保存で
    書かれる）。
    """
    if get_settings().agent_runtime_dispatch_mode.strip().lower() != "in_process":
        return
    try:
        run_ids = repository.fail_interrupted_runs(updated_before=_process_started_at)
    except Exception:  # noqa: BLE001 - メモリの Run は失敗にした。保存は次の変更で書かれる
        logger.exception("agent_interrupted_runs_not_saved")
        return
    if run_ids:
        logger.warning(
            "agent_interrupted_runs_failed",
            extra={"count": len(run_ids), "run_ids": run_ids[:20]},
        )


def _restore_control_plane() -> None:
    """画面・API で変えた定義（Skill・MCP 接続・ツール権限など）を読み込む（#764）。"""
    try:
        restored = control_plane_store.restore_control_plane()
    except Exception as exc:
        if is_oracle_connection_error(exc):
            codes = ", ".join(oracle_error_codes(exc)) or type(exc).__name__
            raise AgentRuntimeStorageUnavailableError(
                f"Agent の定義の保存先のデータベースに接続できません（{codes}）。"
                "接続できるようになると、再起動せずに読み込みます。"
            ) from exc
        # 接続以外の障害で画面を止めない（今までどおり記録して先へ進む）。
        logger.exception("agent_control_plane_restore_failed")
        return
    logger.info("agent_control_plane_restored", extra={"restored": restored})


def _prepare_history() -> None:
    """評価の履歴の整理（保持期間）と、Run の事実の backfill（#794）。

    Run の事実は Oracle の構成だけ保存する。Runtime repository にある Run の事実をすべて
    キューに入れ、バックグラウンドで MERGE する（今見えている Run の集計を消さない）。
    """
    from app.features.agent import run_facts_store
    from app.features.agent.evaluation import evaluation_store

    try:
        evaluation_store.prune()
    except Exception:  # noqa: BLE001 - 整理の失敗で読み込みを止めない
        logger.exception("agent_evaluation_prune_failed")
    try:
        run_facts_store.backfill(runtime.get_runtime_repository())
    except Exception:  # noqa: BLE001 - 集計用の事実の失敗で読み込みを止めない
        logger.exception("agent_run_facts_backfill_failed")


def start_background_load() -> None:
    """起動時の読み込みをバックグラウンドで始める（済むまで一定の間隔で再試行する）。

    daemon thread にする（DB の停止中の接続の待ちで、終了・`--reload` の再起動を待たせない）。
    """
    global _thread
    if _ready or (_thread is not None and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_load_until_ready, name="agent-storage-load", daemon=True)
    _thread.start()


def stop_background_load() -> None:
    _stop.set()


def _load_until_ready() -> None:
    attempt = 0
    while not _stop.is_set() and not _ready:
        try:
            ensure_ready()
            return
        except AgentRuntimeStorageUnavailableError as exc:
            attempt += 1
            logger.warning(
                "agent_storage_not_ready",
                extra={
                    "attempt": attempt,
                    "retry_seconds": RETRY_INTERVAL_SECONDS,
                    "reason": str(exc),
                },
            )
        except Exception:  # noqa: BLE001 - 再試行を止めない
            attempt += 1
            logger.exception("agent_storage_load_failed")
        _stop.wait(RETRY_INTERVAL_SECONDS)


def reset(*, ready: bool = False) -> None:
    """状態をやり直す（テスト。`ready=True` は読み込み済みにする）。"""
    global _ready, _control_plane_restored, _interrupted_runs_checked, _failure
    with _lock:
        _ready = ready
        _control_plane_restored = ready
        _interrupted_runs_checked = ready
        _failure = None


# ---- データを書き換える migration の後の読み直し（#1844） ----------------------------

SavedDataArea = Literal["runtime", "control_plane"]
# runtime: Run・業務 Agent の checkpoint（AGENT_RUNTIME_CHECKPOINTS）。control_plane: 画面・API で
# 変えた定義（AGENT_CONTROL_PLANE_ITEMS。スキル・MCP 接続・ツール権限・自動実行・品質評価）。
SAVED_DATA_AREAS: frozenset[SavedDataArea] = frozenset({"runtime", "control_plane"})
BACKEND_PROCESS = "Agent の backend（systemd の production-ready-agent-backend.service）"
DISPATCHER_PROCESS = "実行の dispatcher（python -m app.features.agent.runtime_dispatcher）"


@dataclass(frozen=True, slots=True)
class SavedDataReload:
    """書き換えの後に、このプロセスで読み直した保存先と、再起動が要るプロセス。"""

    reloaded: tuple[SavedDataArea, ...] = ()
    restart_processes: tuple[str, ...] = ()

    def restart_required(self) -> dict[str, object] | None:
        """画面・CLI の結果に出す再起動の案内（要らなければ None）。"""
        if not self.restart_processes:
            return None
        return {
            "message": (
                "保存済みのデータを書き換える migration を適用しました。次のプロセスは"
                "書き換える前の定義をメモリに持っているため、再起動してください。再起動するまで、"
                "そのプロセスは古い定義で動き、保存すると migration の結果を上書きすることが"
                "あります。"
            ),
            "processes": list(self.restart_processes),
        }


def rewrite_saved_data[T](
    areas: Collection[str], rewrite: Callable[[], T]
) -> tuple[T, SavedDataReload]:
    """保存先を書き換える `rewrite` を実行し、同じプロセスのメモリを保存先から読み直す（#1844）。

    書き換えの間は、読み込み（`ensure_ready`）・Run の repository の変更と保存・定義の保存を止める
    （待っていた変更は読み直した状態の上で行う。定義の保存は待たずに 503）。書き換えが失敗しても、
    途中まで書いた分があるので読み直す。読み直せなければ、このプロセスの再起動を求める。
    まだ読み込んでいない保存先は、次の読み込みで新しい値を読むので読み直さない。
    """
    targets = frozenset(areas) & SAVED_DATA_AREAS
    if not targets:
        return rewrite(), SavedDataReload()
    with ExitStack() as stack:
        # 起動時の読み込みの途中なら、済むのを待つ（書き換える前の値を読み込ませない）。
        _lock.acquire()
        stack.callback(_lock.release)
        reload_runtime: Callable[[], None] | None = None
        repository = runtime.loaded_runtime_repository()
        if "runtime" in targets and isinstance(
            repository, runtime.AgentRuntimeOracleCheckpointRepository
        ):
            reload_runtime = stack.enter_context(repository.storage_rewrite())
        reload_control_plane = "control_plane" in targets and _control_plane_restored
        if reload_control_plane:
            stack.enter_context(control_plane_store.writes_paused())
        try:
            result = rewrite()
        except BaseException:
            _reload_saved_data(reload_runtime, reload_control_plane)
            raise
        reloaded, failed = _reload_saved_data(reload_runtime, reload_control_plane)
    processes: list[str] = []
    if failed:
        processes.append(BACKEND_PROCESS)
    if get_settings().agent_runtime_dispatch_mode.strip().lower() != "in_process":
        processes.append(DISPATCHER_PROCESS)
    outcome = SavedDataReload(reloaded=reloaded, restart_processes=tuple(processes))
    logger.info(
        "agent_saved_data_reloaded",
        extra={
            "areas": sorted(targets),
            "reloaded": list(reloaded),
            "failed": failed,
            "restart_processes": list(processes),
        },
    )
    return result, outcome


def _reload_saved_data(
    reload_runtime: Callable[[], None] | None, reload_control_plane: bool
) -> tuple[tuple[SavedDataArea, ...], bool]:
    """読み直す（読み直せた保存先と、失敗があったか）。失敗は記録して続ける。"""
    reloaded: list[SavedDataArea] = []
    failed = False
    if reload_runtime is not None:
        try:
            reload_runtime()
            reloaded.append("runtime")
        except Exception:  # noqa: BLE001 - 結果で再起動を求める
            logger.exception("agent_runtime_reload_failed")
            failed = True
    if reload_control_plane:
        try:
            if control_plane_store.reload_control_plane() is not None:
                reloaded.append("control_plane")
        except Exception:  # noqa: BLE001 - 結果で再起動を求める
            logger.exception("agent_control_plane_reload_failed")
            failed = True
    return tuple(reloaded), failed
