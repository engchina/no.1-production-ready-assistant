"""サービス起動/停止・ログの制御層(systemd。#286)。

各マイクロサービスは systemd の unit(``production-ready-rag-<service_id>.service``)として動く。
画面の起動/停止は unit を ``enable --now`` / ``disable --now`` し、利用者が最後に操作した状態を
systemd の enable / disable として残す(再起動・再配備でもその状態に戻る)。

セキュリティ要件:
- ``rag_service_control_enabled`` が False の間は呼び出し側が 409 で拒否する(本層は実行しない)。
- 対象は **カタログの allowlist の unit** に限定し、任意のコマンド・引数は受けない
  (argv は ``app.services.systemd`` が固定で組み立てる)。
- subprocess は timeout 付きで実行し、失敗は理由(unit が無い / 権限が無い / 失敗)付きで返す。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.config import (
    Settings,
    enterprise_ai_connection_for_model,
    enterprise_ai_default_model_id,
    enterprise_ai_output_token_budget,
    enterprise_ai_reasoning_effort_to_send,
    enterprise_ai_vision_model_id,
)
from app.services.catalog import ServiceCatalogEntry
from app.services.systemd import (
    CommandUnavailableError,
    SystemdAction,
    UnitState,
    is_permission_error,
    is_systemd_unavailable,
    journalctl_argv,
    parse_journal,
    parse_systemctl_show,
    run_command,
    systemctl_action_argv,
    systemctl_show_argv,
)

logger = logging.getLogger(__name__)

ServiceAction = SystemdAction
ServiceLogsSource = Literal["journald"]
# 失敗の理由。API は unit_not_found → 404、permission_denied / unavailable → 503、
# failed → 502 にする。
ServiceFailureReason = Literal["unit_not_found", "permission_denied", "unavailable", "failed"]

# サービス実行用の env ファイルに書く key(値は backend の実効設定)。
# HF_TOKEN / HF_ENDPOINT は huggingface_hub が読む標準名。PLATFORM_OCI_ENTERPRISE_AI_* は
# OCI parser が読む名前。docling の Vision が読んでいた接頭辞なしの OCI_ENTERPRISE_AI_* は、
# Vision を backend の解析後の共通の段へ移した(#497)ため渡さない(docling は LLM を呼ばない)。
SERVICE_RUNTIME_ENV_KEYS = (
    "HF_TOKEN",
    "HF_ENDPOINT",
    "PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT",
    "PLATFORM_OCI_ENTERPRISE_AI_API_KEY",
    "PLATFORM_OCI_ENTERPRISE_AI_PROJECT_OCID",
    "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL",
    "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL",
    "PLATFORM_OCI_ENTERPRISE_AI_VLM_PATH",
    "PLATFORM_OCI_ENTERPRISE_AI_LLM_PATH",
    "PLATFORM_OCI_ENTERPRISE_AI_VLM_INPUT_MODE",
    "PLATFORM_OCI_ENTERPRISE_AI_TEXT_REASONING_EFFORT",
    "PLATFORM_OCI_ENTERPRISE_AI_VISION_REASONING_EFFORT",
    "PLATFORM_OCI_ENTERPRISE_AI_TEXT_MAX_OUTPUT_TOKENS",
    "PLATFORM_OCI_ENTERPRISE_AI_VISION_MAX_OUTPUT_TOKENS",
)
_RUNTIME_ENV_FILE_MODE = 0o600
_ACTION_LABELS: dict[ServiceAction, str] = {
    "start": "起動",
    "stop": "停止",
    "restart": "再起動",
}


@dataclass(frozen=True)
class ControlResult:
    """制御コマンドの実行結果(非機密)。"""

    ok: bool
    action: ServiceAction
    service_id: str
    exit_code: int | None = None
    detail: str | None = None
    reason: ServiceFailureReason | None = None


@dataclass(frozen=True)
class ServiceLogsResult:
    """サービスログの末尾(非機密メタデータ + 本文)。"""

    service_id: str
    source: ServiceLogsSource
    lines: int
    content: str


class ServiceControlError(Exception):
    """制御コマンドの実行に失敗したことを表す(API は ``result.reason`` で status を決める)。"""

    def __init__(self, result: ControlResult) -> None:
        super().__init__(result.detail or f"{result.action} failed: {result.service_id}")
        self.result = result


class ServiceLogsError(Exception):
    """サービスログ取得に失敗したことを表す(API は ``reason`` で status を決める)。"""

    def __init__(self, message: str, reason: ServiceFailureReason = "failed") -> None:
        super().__init__(message)
        self.reason: ServiceFailureReason = reason


class _UnitError(Exception):
    """unit の状態確認・操作の失敗(理由と利用者向けメッセージ)。"""

    def __init__(self, message: str, reason: ServiceFailureReason) -> None:
        super().__init__(message)
        self.reason: ServiceFailureReason = reason


def unit_not_found_message(entry: ServiceCatalogEntry) -> str:
    """unit が登録されていないときの案内(操作・ログ共通)。"""
    return (
        f"{entry.service_id} の systemd の unit「{entry.systemd_unit}」が登録されていません。"
        "本番は rag/init_script.sh(配備の props の rag_services.txt で選んだサービスだけを"
        "登録します)、"
        "開発は rag/scripts/rag-services.sh install で登録してください(rag/docs/deployment.md)。"
    )


def permission_denied_message(entry: ServiceCatalogEntry, command: str) -> str:
    """sudoers の許可が無いときの案内。"""
    return (
        f"{entry.service_id} の {command} を実行する権限がありません。"
        "backend の実行ユーザーに、この unit の systemctl / journalctl だけを許可する sudoers "
        "(/etc/sudoers.d/production-ready-rag-services)を登録してください(rag/docs/deployment.md)。"
    )


def systemd_unavailable_message(detail: str) -> str:
    return (
        "systemd を使えないため、サービスを操作できません"
        f"(systemd が PID 1 の環境で実行してください)。詳細: {detail}"
    )


def service_runtime_env(settings: Settings) -> dict[str, str]:
    """マイクロサービスの unit に渡す実行用の環境変数(backend の実効設定)。

    HuggingFace 設定(``RAG_HUGGINGFACE_*``)を ``HF_TOKEN`` / ``HF_ENDPOINT`` として渡す。
    parser のサービスはモデル設定 JSON を読まず env からのみ OCI Enterprise AI の設定を読むため、
    backend が解決済みの実効値(model-settings.json 由来を含む)を渡す。これで「モデル画面で設定 →
    parser 再起動 → 稼働中」が成立する(parser は起動時に 1 回だけ env を読むため再起動が必要)。
    parser のサービスが呼ぶのは Vision のモデルなので、接続(endpoint / API key / project)は既定の
    Vision モデルの接続を渡す(#533。セカンダリ接続を選んだ Vision モデルでも同じ接続で呼ぶ)。
    推論(思考)の量(#1425)は、それぞれのモデルの送る値を渡す(none は空 = 送らない)。
    出力の上限(#1719)は、それぞれのモデルの値を渡す(platform の
    `enterprise_ai_output_token_budget`。共通の LLM / VLM の出力の上限は無い)。
    """
    vlm_model = enterprise_ai_vision_model_id(settings)
    default_model = enterprise_ai_default_model_id(settings)
    connection = enterprise_ai_connection_for_model(settings, vlm_model)
    # Endpoint URL は platform の関数で base URL にして渡す(#1440。parser はそのまま使う)。
    endpoint = connection.base_url
    api_key = connection.api_key
    project = connection.project_ocid
    text_reasoning = enterprise_ai_reasoning_effort_to_send(settings, default_model)
    vision_reasoning = enterprise_ai_reasoning_effort_to_send(settings, vlm_model)
    return {
        "HF_TOKEN": settings.huggingface_token,
        # 空の HF_ENDPOINT は huggingface_hub が scheme 無しの endpoint として扱い DL が
        # 失敗するため、未設定のときは公式 hub を渡す。
        "HF_ENDPOINT": settings.huggingface_endpoint or "https://huggingface.co",
        "PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT": endpoint,
        "PLATFORM_OCI_ENTERPRISE_AI_API_KEY": api_key,
        "PLATFORM_OCI_ENTERPRISE_AI_PROJECT_OCID": project,
        "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL": vlm_model,
        "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL": default_model,
        "PLATFORM_OCI_ENTERPRISE_AI_VLM_PATH": settings.oci_enterprise_ai_vlm_path,
        "PLATFORM_OCI_ENTERPRISE_AI_LLM_PATH": settings.oci_enterprise_ai_llm_path,
        "PLATFORM_OCI_ENTERPRISE_AI_VLM_INPUT_MODE": str(settings.oci_enterprise_ai_vlm_input_mode),
        "PLATFORM_OCI_ENTERPRISE_AI_TEXT_REASONING_EFFORT": text_reasoning,
        "PLATFORM_OCI_ENTERPRISE_AI_VISION_REASONING_EFFORT": vision_reasoning,
        "PLATFORM_OCI_ENTERPRISE_AI_TEXT_MAX_OUTPUT_TOKENS": str(
            enterprise_ai_output_token_budget(settings, default_model)
        ),
        "PLATFORM_OCI_ENTERPRISE_AI_VISION_MAX_OUTPUT_TOKENS": str(
            enterprise_ai_output_token_budget(settings, vlm_model)
        ),
    }


def _systemd_env_value(value: str) -> str:
    """systemd の EnvironmentFile の値として安全に書く(改行を除き、二重引用符でエスケープ)。"""
    single_line = value.replace("\r", "").replace("\n", "")
    escaped = single_line.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_service_runtime_env(values: dict[str, str]) -> str:
    """サービス実行用の env ファイルの本文(systemd の EnvironmentFile 形式)。"""
    lines = [
        "# backend がサービスの起動/再起動の前に書く(手で編集しない)。#286",
        "# マイクロサービスの systemd の unit が EnvironmentFile で読む。",
    ]
    lines += [
        f"{key}={_systemd_env_value(values.get(key, ''))}" for key in SERVICE_RUNTIME_ENV_KEYS
    ]
    return "\n".join(lines) + "\n"


def write_service_runtime_env(settings: Settings) -> Path:
    """サービス実行用の env ファイルを 0600 で atomic に書く(secret を含むため)。"""
    path = Path(settings.rag_service_runtime_env_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    content = render_service_runtime_env(service_runtime_env(settings))
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _RUNTIME_ENV_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        tmp_path.replace(path)
        path.chmod(_RUNTIME_ENV_FILE_MODE)
    finally:
        tmp_path.unlink(missing_ok=True)
    return path


async def read_unit_state(settings: Settings, entry: ServiceCatalogEntry) -> UnitState:
    """``systemctl show`` で unit の状態を読む(失敗は ``_UnitError``)。"""
    timeout = float(settings.rag_service_control_timeout_seconds)
    try:
        output = await run_command(systemctl_show_argv(entry.systemd_unit), timeout)
    except CommandUnavailableError as exc:
        raise _UnitError(systemd_unavailable_message(str(exc)), "unavailable") from exc
    if output.returncode != 0:
        if is_systemd_unavailable(output.message):
            raise _UnitError(systemd_unavailable_message(output.message), "unavailable")
        if is_permission_error(output.message):
            raise _UnitError(
                permission_denied_message(entry, "systemctl show"), "permission_denied"
            )
        raise _UnitError(output.message or "unit の状態を確認できませんでした。", "failed")
    return parse_systemctl_show(output.stdout)


async def _require_installed_unit(settings: Settings, entry: ServiceCatalogEntry) -> str:
    unit = entry.systemd_unit
    state = await read_unit_state(settings, entry)
    if not state.installed:
        raise _UnitError(unit_not_found_message(entry), "unit_not_found")
    return unit


class SystemdDriver:
    """allowlist の unit を ``sudo -n systemctl`` で操作する driver。"""

    async def run(
        self,
        settings: Settings,
        entry: ServiceCatalogEntry,
        action: ServiceAction,
    ) -> ControlResult:
        timeout = float(settings.rag_service_control_timeout_seconds)
        try:
            unit = await _require_installed_unit(settings, entry)
            if action in {"start", "restart"}:
                # unit は起動時に 1 回だけ env を読むため、起動/再起動の前に最新の設定を書く。
                try:
                    write_service_runtime_env(settings)
                except OSError as exc:
                    raise _UnitError(
                        f"サービス実行用の env ファイルを書けませんでした: {exc}", "failed"
                    ) from exc
            argv = systemctl_action_argv(action, unit)
            logger.info(
                "service_control_exec",
                extra={"service_id": entry.service_id, "action": action, "argv": argv},
            )
            try:
                output = await run_command(argv, timeout)
            except CommandUnavailableError as exc:
                raise _UnitError(str(exc), "unavailable") from exc
        except _UnitError as exc:
            return ControlResult(
                ok=False,
                action=action,
                service_id=entry.service_id,
                detail=str(exc),
                reason=exc.reason,
            )
        if output.returncode == 0:
            return ControlResult(ok=True, action=action, service_id=entry.service_id, exit_code=0)
        reason: ServiceFailureReason
        if is_permission_error(output.message):
            detail = permission_denied_message(entry, f"systemctl({_ACTION_LABELS[action]})")
            reason = "permission_denied"
        elif is_systemd_unavailable(output.message):
            detail = systemd_unavailable_message(output.message)
            reason = "unavailable"
        else:
            detail = (
                f"{entry.service_id} の{_ACTION_LABELS[action]}に失敗しました: "
                f"{output.message or f'exit {output.returncode}'}"
                f"(ログは journalctl -u {unit} で確認できます)"
            )
            reason = "failed"
        return ControlResult(
            ok=False,
            action=action,
            service_id=entry.service_id,
            exit_code=output.returncode,
            detail=detail,
            reason=reason,
        )


class ServiceControlClient:
    """カタログ allowlist と feature flag を front に、``SystemdDriver`` へ委譲する。"""

    def __init__(self, driver: SystemdDriver | None = None) -> None:
        self._driver = driver or SystemdDriver()
        # サービス単位の直列化ロック(同一サービスへの同時 start で二重操作を防ぐ)。
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, service_id: str) -> asyncio.Lock:
        lock = self._locks.get(service_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[service_id] = lock
        return lock

    async def control(
        self,
        settings: Settings,
        entry: ServiceCatalogEntry,
        action: ServiceAction,
    ) -> ControlResult:
        """allowlist 済みエントリに対し action を実行する。失敗は例外で送出する。"""
        # 同一サービスへの操作は直列化する(並行 start の race を回避)。
        async with self._lock_for(entry.service_id):
            result = await self._driver.run(settings, entry, action)
        if not result.ok:
            raise ServiceControlError(result)
        return result


async def read_service_logs(
    settings: Settings,
    entry: ServiceCatalogEntry,
    lines: int,
) -> ServiceLogsResult:
    """allowlist 済みサービスのログ末尾を ``journalctl -u <unit>`` から返す。

    unit が登録されていない・権限が無いときは ``ServiceLogsError``(理由付き)にする。
    unit があってログが 0 行(``-- No entries --``)なら、空の本文をそのまま返す。
    """
    timeout = float(settings.rag_service_control_timeout_seconds)
    try:
        unit = await _require_installed_unit(settings, entry)
        try:
            output = await run_command(journalctl_argv(unit), timeout)
        except CommandUnavailableError as exc:
            raise _UnitError(str(exc), "unavailable") from exc
    except _UnitError as exc:
        raise ServiceLogsError(str(exc), exc.reason) from exc
    if output.returncode != 0:
        if is_permission_error(output.message):
            raise ServiceLogsError(
                permission_denied_message(entry, "journalctl"), "permission_denied"
            )
        raise ServiceLogsError(output.message or "ログ取得に失敗しました。")
    # journal を読めないとき journalctl は exit 0 で本文なし・権限の案内だけを出すことがある。
    # 空のログとして返さず、権限不足として扱う。
    if not parse_journal(output.stdout, lines) and is_permission_error(output.stderr):
        raise ServiceLogsError(permission_denied_message(entry, "journalctl"), "permission_denied")
    return ServiceLogsResult(
        service_id=entry.service_id,
        source="journald",
        lines=lines,
        content=parse_journal(output.stdout, lines),
    )
