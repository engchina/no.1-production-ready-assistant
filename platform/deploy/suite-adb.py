"""配備した Compute で、ADB（Autonomous AI Database）が止まっていれば起動し、AVAILABLE を待つ（#1745）。

platform/deploy/suite-restart.sh（suite-update.sh も最後にこれを実行する）が、DB を使う処理（system schema の
初期化・migration・サービスの起動）の前に、製品の backend の venv の Python で、製品のプロセスと同じユーザー（opc）
として実行する。

  python suite-adb.py ensure --env-file <repository の platform/.env> [--timeout-seconds 600]
  python suite-adb.py status --env-file <repository の platform/.env>   # 見るだけ（起動しない）

- 対象は platform/.env の PLATFORM_ORACLE_ADB_OCID、region は PLATFORM_ORACLE_ADB_REGION（空なら PLATFORM_OCI_REGION）。
- OCI の呼び出しはシステム設定 › データベース と同じ pr_system_settings.oci_database.OciDatabaseClient
  （get_autonomous_database / start_autonomous_database）。認証は PLATFORM_OCI_AUTH_MODE で選ぶ:
    - config_file（既定。空も同じ）: システム設定 › OCI 認証 で保存する PLATFORM_OCI_CONFIG_FILE（既定 ~/.oci/config）の
      PLATFORM_OCI_CONFIG_PROFILE（既定 DEFAULT）。読み方は画面と同じ（OciDatabaseClient の既定）。
    - instance_principal: Compute の instance principal（dynamic group の policy が要る）。
- ensure の振る舞い（状態は OCI の lifecycle_state）:
    - AVAILABLE（と、DB を開いたままの AVAILABLE_NEEDS_ATTENTION・BACKUP_IN_PROGRESS・SCALE_IN_PROGRESS）: そのまま進む。
    - STOPPED: 起動を要求し、AVAILABLE を待つ。STOPPING は STOPPED を待ってから起動する。
    - STARTING・PROVISIONING・RESTARTING・UPDATING など、OCI の作業が終われば使える状態: 待つ。
    - それ以外（UNAVAILABLE・TERMINATED・RESTORE_FAILED・INACCESSIBLE・STANDBY など）: 起動せず、理由を出して NG で終わる。
    - 設定が無い・認証の失敗（401）・ADB が見つからないか権限が無い（404）・時間切れ: 理由と手で起動する方法を出して NG で終わる。
    - NG は best effort の結果（#1756）: suite-restart.sh は warning を出し、build・再起動を続ける（DB に接続できなくても
      アプリケーションは起動する）。
- ADB を停止する処理は持たない。認証の値・secret は出さない（OCI の失敗は種類・HTTP status・code・request id だけを出す）。
- 標準出力の最後の行（[suite-adb] OK / NG）が結果。終了コード: 0 = 使える、1 = NG（使える状態にできない）、2 = 引数の誤り、
  3 = status で ADB が使える状態ではない。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

LOG_PREFIX = "[suite-adb]"
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_REPORT_SECONDS = 60.0
# 起動を要求した後も STOPPED のままなら、この秒数ごとに起動を要求し直す（要求が反映される前の STOPPED を除く）。
RESTART_REQUEST_SECONDS = 120.0
# 一時の失敗（network・429・5xx）を続けて許す回数。
MAX_TRANSIENT_ERRORS = 5

# DB に接続できる状態（そのまま進む）。
READY_STATES = frozenset({"AVAILABLE"})
# DB を開いたままの状態（そのまま進む。log に状態を残す）。
OPEN_BUSY_STATES = frozenset({"AVAILABLE_NEEDS_ATTENTION", "BACKUP_IN_PROGRESS", "SCALE_IN_PROGRESS"})
START_STATES = frozenset({"STOPPED"})
# OCI の作業が終われば使える（または STOPPED になって起動できる）状態。待つ。
WAIT_STATES = frozenset(
    {
        "STARTING",
        "STOPPING",
        "PROVISIONING",
        "RESTARTING",
        "UPDATING",
        "MAINTENANCE_IN_PROGRESS",
        "RESTORE_IN_PROGRESS",
        "ROLE_CHANGE_IN_PROGRESS",
        "UPGRADING",
        "RECREATING",
    }
)
# 待っても使えない状態の理由（起動しない）。
STOP_REASONS = {
    "UNAVAILABLE": "ADB が利用できない状態です（UNAVAILABLE）。OCI のコンソールで ADB の状態と作業要求を確かめてください",
    "TERMINATING": "ADB は削除中です（TERMINATING）。PLATFORM_ORACLE_ADB_OCID が正しいか確かめてください",
    "TERMINATED": "ADB は削除されています（TERMINATED）。PLATFORM_ORACLE_ADB_OCID が正しいか確かめてください",
    "RESTORE_FAILED": "ADB の復元が失敗しています（RESTORE_FAILED）。OCI のコンソールで復元の作業要求を確かめてください",
    "INACCESSIBLE": "ADB の暗号化の鍵（Vault）に届かず、利用できません（INACCESSIBLE）。鍵と Vault の状態を確かめてください",
    "STANDBY": "ADB は Data Guard の standby です（STANDBY）。primary の ADB の OCID を PLATFORM_ORACLE_ADB_OCID にしてください",
}
# 認証の設定を読めないときの例外（oci.exceptions・pr_system_settings.oci_auth・OS）。メッセージに値は出さない。
CONFIG_ERROR_TYPES = frozenset(
    {
        "ConfigFileNotFound",
        "ProfileNotFound",
        "InvalidConfig",
        "InvalidKeyFilePath",
        "InvalidPrivateKey",
        "MissingPrivateKeyPassphrase",
        "OciPrivateKeyPassPhraseRequiredError",
        "FileNotFoundError",
        "PermissionError",
    }
)
SUPPORTED_AUTH_MODES = ("config_file", "instance_principal")


class SuiteAdbError(Exception):
    """ADB を使える状態にできない理由（日本語）。suite-restart.sh は warning にして続ける（#1756）。"""


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


@dataclass(frozen=True)
class AdbTarget:
    adb_ocid: str
    region: str
    auth_mode: str
    config_file: str
    profile: str


def read_target(env_file: Path) -> AdbTarget:
    """platform/.env から対象の ADB と OCI の認証の方式を読む（製品と同じ python-dotenv で読む）。"""
    if not env_file.is_file():
        raise SuiteAdbError(f"{env_file} がありません（配備（suite-init.sh）が終わっているか確かめてください）")
    dotenv = importlib.import_module("dotenv")
    values = {key: (value or "").strip() for key, value in dotenv.dotenv_values(env_file).items()}
    adb_ocid = values.get("PLATFORM_ORACLE_ADB_OCID", "")
    if not adb_ocid:
        raise SuiteAdbError(
            f"{env_file} に PLATFORM_ORACLE_ADB_OCID がありません（システム設定 › データベース の ADB の OCID を保存してください）"
        )
    auth_mode = values.get("PLATFORM_OCI_AUTH_MODE", "").lower() or "config_file"
    if auth_mode not in SUPPORTED_AUTH_MODES:
        raise SuiteAdbError(
            f"PLATFORM_OCI_AUTH_MODE={auth_mode} では ADB を操作できません（Compute では config_file か instance_principal）"
        )
    return AdbTarget(
        adb_ocid=adb_ocid,
        region=values.get("PLATFORM_ORACLE_ADB_REGION", "") or values.get("PLATFORM_OCI_REGION", ""),
        auth_mode=auth_mode,
        config_file=values.get("PLATFORM_OCI_CONFIG_FILE", "") or "~/.oci/config",
        profile=values.get("PLATFORM_OCI_CONFIG_PROFILE", "") or "DEFAULT",
    )


def build_client(target: AdbTarget) -> Any:
    """システム設定 › データベース と同じ OciDatabaseClient を作る。"""
    oci_database = importlib.import_module("pr_system_settings.oci_database")
    settings = SimpleNamespace(
        oci_config_file=target.config_file,
        oci_config_profile=target.profile,
        resolved_oracle_adb_region=target.region,
        oracle_adb_ocid=target.adb_ocid,
    )
    if target.auth_mode == "config_file":
        config_path = Path(target.config_file).expanduser()
        if not config_path.is_file():
            raise SuiteAdbError(
                f"OCI の認証の設定ファイル（{target.config_file}）がありません"
                "（各製品の画面の システム設定 › OCI 認証 で保存してください）"
            )
        # 認証の読み方は画面と同じ（OciDatabaseClient が最初の呼び出しで config を読む）。
        return oci_database.OciDatabaseClient(settings=settings)
    # instance_principal: Compute の instance principal の signer で Database の client を作り、渡す。
    try:
        signers = importlib.import_module("oci.auth.signers")
        database = importlib.import_module("oci.database")
        signer = signers.InstancePrincipalsSecurityTokenSigner()
        config = {"region": target.region} if target.region else {}
        sdk_client = database.DatabaseClient(config, signer=signer)
    except Exception as exc:  # noqa: BLE001 - OCI SDK の多様な例外を理由に変える
        raise SuiteAdbError(
            f"instance principal の認証を準備できません（{type(exc).__name__}。dynamic group と Compute の network を確かめてください）"
        ) from exc
    return oci_database.OciDatabaseClient(settings=settings, database_client=sdk_client)


def failure_detail(exc: BaseException) -> dict[str, Any]:
    """OCI の失敗の種類・status・code・request id（画面の log と同じ。本文・認証の値は含めない）。"""
    database = importlib.import_module("pr_system_settings.database")
    detail: dict[str, Any] = database.oci_failure_log_extra(exc)
    return detail


def describe_failure(detail: dict[str, Any]) -> str:
    parts = [str(detail.get("exception_type", ""))]
    if "oci_status" in detail:
        parts.append(f"HTTP {detail['oci_status']}")
    if "oci_code" in detail:
        parts.append(str(detail["oci_code"]))
    if "opc_request_id" in detail:
        parts.append(f"opc-request-id {detail['opc_request_id']}")
    return " / ".join(part for part in parts if part)


def is_transient(detail: dict[str, Any]) -> bool:
    """待てば直りうる失敗（network・429・5xx）か。"""
    status = detail.get("oci_status")
    if isinstance(status, int):
        return status == 429 or status >= 500
    return detail.get("exception_type") not in CONFIG_ERROR_TYPES


def fatal_reason(exc: BaseException, detail: dict[str, Any], target: AdbTarget) -> str:
    """試し直さない失敗の理由（日本語）。"""
    kind = describe_failure(detail)
    status = detail.get("oci_status")
    if detail.get("exception_type") in CONFIG_ERROR_TYPES:
        safe = getattr(exc, "safe_for_user", False)
        hint = str(exc) if safe else "システム設定 › OCI 認証 で保存した設定を確かめてください"
        return f"OCI の認証の設定（{target.config_file} の [{target.profile}]）を読めません（{kind}）。{hint}"
    if status == 401:
        return (
            f"OCI の認証に失敗しました（{kind}）。システム設定 › OCI 認証 の user・fingerprint・tenancy・API キーと、"
            "Compute の時刻を確かめてください"
        )
    if status == 404:
        return (
            f"ADB が見つからないか、ADB を見る権限がありません（{kind}）。PLATFORM_ORACLE_ADB_OCID・region"
            f"（{target.region or 'OCI の config の region'}）と、IAM の policy（inspect autonomous-databases）を確かめてください"
        )
    if status == 403:
        return f"ADB を操作する権限がありません（{kind}）。IAM の policy（use autonomous-databases）を確かめてください"
    return f"OCI の呼び出しが失敗しました（{kind}）"


def manual_start_hint(target: AdbTarget | None, script: str) -> str:
    ocid = target.adb_ocid if target else "<PLATFORM_ORACLE_ADB_OCID>"
    # DB が止まっていてもアプリケーションは起動し、構成管理者 system_admin はログインできる。
    # 画面から ADB を起動すれば、そのまま DB を使える（スクリプトの再実行は要らない。#1762）。
    return (
        "ADB は、system_admin でログインして「システム設定 › データベース」から起動できます"
        "（OCI のコンソールの Oracle Database › Autonomous AI Database › 対象の ADB › 起動、"
        f"または OCI CLI の `oci db autonomous-database start --autonomous-database-id {ocid}` でも起動できます）。"
        f"起動すれば、そのまま DB を使えます（sudo {script} の再実行は要りません）"
    )


class AdbWaiter:
    """ADB の状態を見て、止まっていれば起動し、AVAILABLE を待つ。"""

    def __init__(
        self,
        client: Any,
        target: AdbTarget,
        *,
        timeout_seconds: float,
        interval_seconds: float,
        report_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.target = target
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.report_seconds = report_seconds
        self.clock = clock
        self.sleep = sleep
        self.started_at = clock()
        self.transient_errors = 0

    def elapsed(self) -> int:
        return int(self.clock() - self.started_at)

    def progress(self) -> str:
        return f"経過 {self.elapsed()} 秒 / 上限 {self.timeout_seconds:g} 秒"

    async def get_state(self) -> tuple[str, str]:
        """(lifecycle_state, display_name)。一時の失敗は試し直す。止める失敗は SuiteAdbError。"""
        while True:
            try:
                info = await self.client.get_autonomous_database(self.target.adb_ocid)
            except Exception as exc:  # noqa: BLE001 - OCI SDK の多様な例外を理由に変える
                self.handle_failure(exc, "状態の取得")
                await self.sleep(self.interval_seconds)
                continue
            self.transient_errors = 0
            return str(info.lifecycle_state or "UNKNOWN").upper(), str(info.display_name or self.target.adb_ocid)

    def handle_failure(self, exc: BaseException, operation: str) -> None:
        detail = failure_detail(exc)
        if not is_transient(detail):
            raise SuiteAdbError(fatal_reason(exc, detail, self.target)) from exc
        self.transient_errors += 1
        if self.transient_errors >= MAX_TRANSIENT_ERRORS or self.clock() - self.started_at >= self.timeout_seconds:
            raise SuiteAdbError(
                f"ADB の{operation}が続けて失敗しました（{describe_failure(detail)}）。Compute から OCI の API "
                f"（region {self.target.region or 'OCI の config の region'}）に届くか確かめてください"
            ) from exc
        log(
            f"ADB の{operation}が失敗しました（{describe_failure(detail)}）。試し直します"
            f"（{self.transient_errors}/{MAX_TRANSIENT_ERRORS}。{self.progress()}）。"
        )

    async def request_start(self) -> None:
        try:
            await self.client.start_autonomous_database(self.target.adb_ocid)
        except Exception as exc:  # noqa: BLE001
            detail = failure_detail(exc)
            if detail.get("oci_status") == 409:
                # 状態が変わった（別の操作が起動・停止を始めた）。次の状態の取得で判断する。
                log(f"起動の要求が受け付けられませんでした（{describe_failure(detail)}）。状態を見直します。")
                return
            if detail.get("oci_status") == 404:
                raise SuiteAdbError(
                    f"ADB を起動する権限がありません（{describe_failure(detail)}）。IAM の policy"
                    "（use autonomous-databases。StartAutonomousDatabase に AUTONOMOUS_DATABASE_UPDATE が要る）を確かめてください"
                ) from exc
            self.handle_failure(exc, "起動の要求")
            return
        log(f"ADB の起動を要求しました。AVAILABLE になるまで待ちます（{self.progress()}）。")

    async def ensure(self) -> tuple[str, str]:
        """使える状態になったら (状態, 表示名) を返す。止めるときは SuiteAdbError。"""
        last_state = ""
        last_report = self.clock()
        last_start_request: float | None = None
        while True:
            state, name = await self.get_state()
            label = f"{name}（{self.target.region}）" if self.target.region else name
            if state in READY_STATES:
                if last_start_request is not None or last_state:
                    log(f"ADB {label} が AVAILABLE になりました（{self.progress()}）。")
                return state, label
            if state in OPEN_BUSY_STATES:
                log(f"ADB {label} は {state} です（DB は開いています）。そのまま進みます。")
                return state, label
            if state in STOP_REASONS or (state not in START_STATES and state not in WAIT_STATES):
                reason = STOP_REASONS.get(state, f"ADB の状態が {state} で、自動では起動できません")
                raise SuiteAdbError(reason)
            now = self.clock()
            if state in START_STATES and (
                last_start_request is None or now - last_start_request >= RESTART_REQUEST_SECONDS
            ):
                log(
                    f"ADB {label} は STOPPED です。platform/.env の OCI の認証（{self.target.auth_mode}）で起動します。"
                )
                await self.request_start()
                last_start_request = now
                last_report = now
            elif state != last_state or now - last_report >= self.report_seconds:
                log(f"ADB {label} は {state} です。待ちます（{self.progress()}）。")
                last_report = now
            last_state = state
            remaining = self.timeout_seconds - (self.clock() - self.started_at)
            if remaining <= 0:
                raise SuiteAdbError(
                    f"ADB が {self.timeout_seconds:g} 秒以内に AVAILABLE になりません（今の状態 {state}）。"
                    "OCI のコンソールで ADB の作業要求を確かめてください"
                )
            await self.sleep(min(self.interval_seconds, remaining))


async def run_ensure(args: argparse.Namespace, client_factory: Callable[[AdbTarget], Any]) -> int:
    target: AdbTarget | None = None
    try:
        target = read_target(Path(args.env_file))
        client = client_factory(target)
        log(f"ADB（{target.adb_ocid}）の状態を確かめます（OCI の認証 {target.auth_mode}）。")
        waiter = AdbWaiter(
            client,
            target,
            timeout_seconds=args.timeout_seconds,
            interval_seconds=args.interval_seconds,
            report_seconds=args.report_seconds,
        )
        state, label = await waiter.ensure()
    except SuiteAdbError as exc:
        raise SuiteAdbError(f"{exc}。{manual_start_hint(target, args.script_name)}") from exc
    log(f"OK: ADB {label} は {state} です。")
    return 0


async def run_status(args: argparse.Namespace, client_factory: Callable[[AdbTarget], Any]) -> int:
    target = read_target(Path(args.env_file))
    client = client_factory(target)
    try:
        info = await client.get_autonomous_database(target.adb_ocid)
    except Exception as exc:  # noqa: BLE001
        detail = failure_detail(exc)
        raise SuiteAdbError(fatal_reason(exc, detail, target)) from exc
    state = str(info.lifecycle_state or "UNKNOWN").upper()
    name = info.display_name or target.adb_ocid
    label = f"{name}（{target.region}）" if target.region else str(name)
    if state in READY_STATES or state in OPEN_BUSY_STATES:
        log(f"OK: ADB {label} は {state} です。")
        return 0
    log(
        f"ADB {label} は {state} です（--status では起動しません。suite-restart.sh・suite-update.sh は止まっていれば起動します）。"
    )
    return 3


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("ensure", "止まっていれば起動し、AVAILABLE を待つ"),
        ("status", "状態を 1 回だけ見る（起動しない）"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--env-file", required=True, help="repository の platform/.env")
        command.add_argument("--script-name", default="suite-restart.sh", help="手で起動した後に実行し直すスクリプト")
        if name == "ensure":
            command.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
            command.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
            command.add_argument("--report-seconds", type=float, default=DEFAULT_REPORT_SECONDS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, client_factory: Callable[[AdbTarget], Any] = build_client) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    runner = run_ensure if args.command == "ensure" else run_status
    try:
        return asyncio.run(runner(args, client_factory))
    except SuiteAdbError as exc:
        log(f"NG: {exc}。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
