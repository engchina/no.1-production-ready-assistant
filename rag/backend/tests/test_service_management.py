"""サービス管理(カタログ / 稼働プローブ / systemd の制御 / API)のテスト(#286)。

systemctl / journalctl / sudo は実行しない。conftest の autouse fixture が
``app.services.control.run_command`` を「systemd を使えない」に差し替えており、
systemd の振る舞いが要るテストは ``_FakeSystemd`` を差し込む。
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any, Literal, get_args

import pytest
from pytest import MonkeyPatch

from app.config import Settings, get_settings
from app.main import app
from app.services import control as service_control
from app.services.catalog import (
    ALLOWED_SYSTEMD_UNITS,
    SERVICE_CATALOG,
    SYSTEMD_UNIT_PREFIX,
    ServiceCatalogEntry,
    ServiceCategory,
    get_catalog_entry,
    is_allowed_systemd_unit,
    is_dev_mode,
    resolve_service_base_url,
    service_health_url,
)
from app.services.control import (
    ControlResult,
    ServiceControlClient,
    ServiceControlError,
    ServiceLogsError,
    ServiceLogsResult,
    SystemdDriver,
    read_service_logs,
    render_service_runtime_env,
    service_runtime_env,
    write_service_runtime_env,
)
from app.services.status import probe_service_status
from app.services.systemd import (
    JOURNAL_FETCH_LINES,
    CommandOutput,
    CommandUnavailableError,
    is_permission_error,
    is_systemd_unavailable,
    journalctl_argv,
    parse_journal,
    parse_systemctl_show,
    run_command,
    systemctl_action_argv,
    systemctl_show_argv,
    validate_unit,
)
from tests.security_support import enable_production_auth, login_configured_admin
from tests.support import AsgiTestClient

client = AsgiTestClient(app)
RAG_ROOT = Path(__file__).resolve().parents[2]
DOCLING_UNIT = "production-ready-rag-parser-docling.service"


# --- カタログ ---------------------------------------------------------------


def test_catalog_ids_unique_and_url_fields_resolve() -> None:
    settings = get_settings()
    ids = [entry.service_id for entry in SERVICE_CATALOG]
    assert len(ids) == len(set(ids)), "service_id は一意であること"
    for entry in SERVICE_CATALOG:
        # URL フィールドが Settings に実在し、文字列で取得できること。
        assert hasattr(settings, entry.url_field)
        assert isinstance(service_health_url(settings, entry), str)


def test_catalog_covers_preprocess_and_parser_without_gpu() -> None:
    categories = {entry.category for entry in SERVICE_CATALOG}
    # 文書分割・検索インデックス・文書構造索引の構築・安全チェック・評価の基準は backend 内だけで
    # 実行し、独立サービスのカテゴリには含めない(#3218)。
    assert categories == {"preprocess", "parser"}
    # GPU のサービスは持たない(ASR は削除した。#2042)。
    gpu_ids = {entry.service_id for entry in SERVICE_CATALOG if entry.profile == "gpu"}
    assert gpu_ids == set()
    # GPU OCR は外部接続へ移行し、ローカル管理対象には残さない。Marker は削除した(#270)。
    # OCI Generative AI (Vision) と ASR は削除した(#2042)。
    for service_id in (
        "parser-asr",
        "parser-oci-genai-vision",
        "parser-marker",
        "parser-unlimited-ocr",
        "parser-mineru",
        "parser-dots-ocr",
        "parser-glm-ocr",
    ):
        assert get_catalog_entry(service_id) is None
        directory = service_id.removeprefix("parser-").replace("-", "_")
        assert not (RAG_ROOT / "services" / "parsers" / directory).exists()


def test_catalog_execution_policies_mark_fallback_boundaries() -> None:
    by_id = {entry.service_id: entry for entry in SERVICE_CATALOG}
    assert by_id["parser-docling"].execution_policy == "selected_adapter"
    assert by_id["preprocess-office-to-pdf"].execution_policy == "selected_adapter"


def test_catalog_has_no_backend_internal_stages() -> None:
    """backend の中で動く段はサービスを持たない(サービスの実装も無い)。

    旧 standard の回答エンジンだけのステージは #595、検索インデックス・文書構造索引の構築・
    安全チェック・評価の基準の段は #3218 で削除した。
    """
    ids = {entry.service_id for entry in SERVICE_CATALOG}
    for stage in (
        "retrieval",
        "grounding",
        "agentic",
        "generation",
        "vector-index",
        "graphrag",
        "guardrail",
        "evaluation",
    ):
        assert f"pipeline-{stage}" not in ids
        assert not (RAG_ROOT / "services" / "pipeline" / stage.replace("-", "_")).exists()


def test_catalog_is_ordered_by_service_category() -> None:
    """カタログは工程(ServiceCategory。サイドナビと同じ並び)の順に、工程ごとにまとめて並べる(#638)。"""
    order = list(get_args(ServiceCategory))
    categories = [entry.category for entry in SERVICE_CATALOG]
    assert set(categories) == set(order)
    positions = [order.index(category) for category in categories]
    assert positions == sorted(positions)


def test_model_cache_path_set_only_for_model_downloading_parsers() -> None:
    """ローカルでモデル DL を行う parser だけ、実行ユーザーの ~/.cache を持つ。"""
    with_cache = {e.service_id: e.model_cache_path for e in SERVICE_CATALOG if e.model_cache_path}
    assert with_cache == {"parser-docling": "~/.cache"}


def test_parser_catalog_order_lists_cpu_then_oci() -> None:
    """サービス管理の文書解析の並び(#2042・#2457): CPU(Docling → Unstructured → 構造化レコード JSONL
    → XML ツリー JSON)→ OCI。"""
    parsers = [entry.service_id for entry in SERVICE_CATALOG if entry.category == "parser"]
    assert parsers == [
        "parser-docling",
        "parser-unstructured",
        "parser-structured-records-jsonl",
        "parser-xml-tree-json",
        "parser-oci-document-understanding",
    ]


def test_catalog_ports_are_unique_and_high() -> None:
    """ポートは一意で、sibling app の backend port と衝突しない高番台に寄せる。"""
    ports = [entry.port for entry in SERVICE_CATALOG]
    assert len(ports) == len(set(ports)), "port は一意であること"
    assert all(port >= 18000 for port in ports)


def test_service_url_defaults_point_to_native_localhost_ports() -> None:
    """URL 設定の既定値は、ネイティブ配備で listen する 127.0.0.1:<port>(#286)。

    Docker Compose の service 名(http://parser-docling:8000 など)を既定にしない。
    """
    defaults = Settings.model_fields
    for entry in SERVICE_CATALOG:
        assert defaults[entry.url_field].default == f"http://127.0.0.1:{entry.port}", entry
        assert entry.default_url == f"http://127.0.0.1:{entry.port}"


def test_catalog_systemd_units_cover_catalog_services() -> None:
    for entry in SERVICE_CATALOG:
        assert entry.systemd_unit == f"{SYSTEMD_UNIT_PREFIX}{entry.service_id}.service"
    assert {entry.systemd_unit for entry in SERVICE_CATALOG} == ALLOWED_SYSTEMD_UNITS
    assert DOCLING_UNIT in ALLOWED_SYSTEMD_UNITS
    # backend / ingestion-worker の unit は画面から操作させない。
    assert "production-ready-rag-backend.service" not in ALLOWED_SYSTEMD_UNITS
    assert "production-ready-rag-ingestion-worker.service" not in ALLOWED_SYSTEMD_UNITS


def test_get_catalog_entry_allowlist() -> None:
    assert get_catalog_entry("parser-docling") is not None
    assert get_catalog_entry("unknown-service") is None
    assert get_catalog_entry("../etc/passwd") is None


# --- サービス実行用の env ファイル ---------------------------------------------


def test_service_runtime_env_injects_huggingface_settings() -> None:
    settings = get_settings().model_copy(
        update={
            "huggingface_token": "hf_secret",
            "huggingface_endpoint": "https://hf-mirror.com",
        }
    )
    env = service_runtime_env(settings)
    assert env["HF_TOKEN"] == "hf_secret"
    assert env["HF_ENDPOINT"] == "https://hf-mirror.com"
    # os.environ を丸ごと渡さない(unit に余計な環境変数を持ち込まない)。
    assert "PATH" not in env


def test_service_runtime_env_defaults_hf_endpoint_to_official_hub() -> None:
    """空の HF_ENDPOINT は huggingface_hub の DL を壊すため、公式 hub を渡す。"""
    settings = get_settings().model_copy(update={"huggingface_endpoint": ""})
    assert service_runtime_env(settings)["HF_ENDPOINT"] == "https://huggingface.co"


def test_service_runtime_env_injects_oci_enterprise_ai_settings() -> None:
    """OCI parser と docling の Vision 向けに実効 OCI Enterprise AI 設定を渡す。"""
    settings = get_settings().model_copy(
        update={
            "oci_enterprise_ai_endpoint": "https://inference.example/openai/v1",
            "oci_enterprise_ai_api_key": "sk-secret",
            "oci_enterprise_ai_project_ocid": "ocid1.generativeaiproject.oc1..x",
            # catalog が空でも resolver は legacy VLM/LLM へフォールバックする。
            "oci_enterprise_ai_models": [],
            "oci_enterprise_ai_default_vision_model": "xai.grok-4.3",
            "oci_enterprise_ai_default_text_model": "xai.grok-4.3",
            "oci_enterprise_ai_vlm_input_mode": "files_api",
        }
    )
    env = service_runtime_env(settings)
    assert env["PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT"] == "https://inference.example/openai/v1"
    assert env["PLATFORM_OCI_ENTERPRISE_AI_API_KEY"] == "sk-secret"
    assert env["PLATFORM_OCI_ENTERPRISE_AI_PROJECT_OCID"] == "ocid1.generativeaiproject.oc1..x"
    assert env["PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL"] == "xai.grok-4.3"
    assert env["PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL"] == "xai.grok-4.3"
    assert env["PLATFORM_OCI_ENTERPRISE_AI_VLM_INPUT_MODE"] == "files_api"
    # 出力の上限はモデルごとの値(#1719。登録の無いモデルは既定の 32768)。
    assert env["PLATFORM_OCI_ENTERPRISE_AI_TEXT_MAX_OUTPUT_TOKENS"] == "32768"
    assert env["PLATFORM_OCI_ENTERPRISE_AI_VISION_MAX_OUTPUT_TOKENS"] == "32768"
    # docling サービスは Vision を呼ばない(#497)。rag_engine の接頭辞なしの名前は渡さない。
    assert "OCI_ENTERPRISE_AI_ENDPOINT" not in env
    assert "OCI_ENTERPRISE_AI_API_KEY" not in env


def test_service_runtime_env_oci_vlm_model_empty_when_unconfigured() -> None:
    """Vision モデル未設定なら空文字で渡し、parser は degraded を維持する。"""
    settings = get_settings().model_copy(
        update={
            "oci_enterprise_ai_models": [],
            "oci_enterprise_ai_default_vision_model": "",
            "oci_enterprise_ai_default_text_model": "",
        }
    )
    assert service_runtime_env(settings)["PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL"] == ""


def test_render_service_runtime_env_escapes_for_systemd() -> None:
    """EnvironmentFile の値は二重引用符で囲み、引用符・バックスラッシュ・改行を無害化する。"""
    text = render_service_runtime_env(
        {"HF_TOKEN": 'a"b\\c\nINJECTED=1', "HF_ENDPOINT": "https://huggingface.co"}
    )
    lines = text.splitlines()
    assert 'HF_TOKEN="a\\"b\\\\cINJECTED=1"' in lines
    assert 'HF_ENDPOINT="https://huggingface.co"' in lines
    # 改行で別の変数を注入できない。
    assert not any(line.startswith("INJECTED=") for line in lines)


def test_write_service_runtime_env_is_private(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    target = tmp_path / "nested" / "service-runtime.env"
    monkeypatch.setattr(settings, "rag_service_runtime_env_file", str(target))
    monkeypatch.setattr(settings, "huggingface_token", "hf_secret")
    written = write_service_runtime_env(settings)
    assert written == target
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert 'HF_TOKEN="hf_secret"' in target.read_text(encoding="utf-8")
    assert list(target.parent.glob(".service-runtime.env.tmp-*")) == []


# --- systemd の argv(allowlist・固定の argv・shell なし) ----------------------


def test_systemctl_action_argv_is_fixed_and_uses_enable_disable() -> None:
    """起動/停止は enable --now / disable --now(最後に操作した状態を systemd に残す)。"""
    sudo_systemctl = ["/usr/bin/sudo", "-n", "/usr/bin/systemctl"]
    assert systemctl_action_argv("start", DOCLING_UNIT) == [
        *sudo_systemctl,
        "enable",
        "--now",
        DOCLING_UNIT,
    ]
    assert systemctl_action_argv("stop", DOCLING_UNIT) == [
        *sudo_systemctl,
        "disable",
        "--now",
        DOCLING_UNIT,
    ]
    assert systemctl_action_argv("restart", DOCLING_UNIT) == [
        *sudo_systemctl,
        "restart",
        DOCLING_UNIT,
    ]


def test_systemctl_show_and_journalctl_argv_are_fixed() -> None:
    # 状態の読み取りは root が要らないため sudo を使わない。
    assert systemctl_show_argv(DOCLING_UNIT) == [
        "/usr/bin/systemctl",
        "show",
        DOCLING_UNIT,
        "--property=LoadState,ActiveState,SubState,UnitFileState",
        "--no-pager",
    ]
    # sudoers と同じ argv にするため、行数は固定で取る(画面の行数は Python で切り詰める)。
    assert journalctl_argv(DOCLING_UNIT) == [
        "/usr/bin/sudo",
        "-n",
        "/usr/bin/journalctl",
        "-u",
        DOCLING_UNIT,
        "-n",
        str(JOURNAL_FETCH_LINES),
        "--no-pager",
        "-o",
        "short-iso",
    ]
    assert JOURNAL_FETCH_LINES == 1000


@pytest.mark.parametrize(
    "unit",
    [
        "",
        "sshd.service",
        "docker.service",
        "production-ready-rag-backend.service",
        "production-ready-rag-ingestion-worker.service",
        # backend 内処理のステージは unit を持たない。
        "production-ready-rag-pipeline-chunking.service",
        "production-ready-rag-parser-docling",
        "production-ready-rag-parser-docling.service; rm -rf /",
        "production-ready-rag-parser-docling.service --now",
        "production-ready-rag-parser-docling.service\n",
        "../../etc/systemd/system/production-ready-rag-parser-docling.service",
        "--now",
        "*",
        "production-ready-rag-*.service",
    ],
)
def test_systemd_argv_rejects_units_outside_allowlist(unit: str) -> None:
    assert is_allowed_systemd_unit(unit) is False
    with pytest.raises(ValueError):
        validate_unit(unit)
    with pytest.raises(ValueError):
        systemctl_action_argv("start", unit)
    with pytest.raises(ValueError):
        systemctl_action_argv("stop", unit)
    with pytest.raises(ValueError):
        systemctl_action_argv("restart", unit)
    with pytest.raises(ValueError):
        systemctl_show_argv(unit)
    with pytest.raises(ValueError):
        journalctl_argv(unit)


def test_systemctl_action_argv_rejects_unknown_action() -> None:
    with pytest.raises(ValueError):
        systemctl_action_argv("mask", DOCLING_UNIT)  # type: ignore[arg-type]


def test_run_command_uses_exec_without_shell_and_c_locale(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"LoadState=loaded\n", b""

    async def fake_exec(*args: Any, **kwargs: Any) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    output = asyncio.run(run_command(systemctl_show_argv(DOCLING_UNIT), 5.0))
    assert output == CommandOutput(returncode=0, stdout="LoadState=loaded", stderr="")
    assert captured["args"] == tuple(systemctl_show_argv(DOCLING_UNIT))
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"]["env"]["LC_ALL"] == "C"
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL


def test_run_command_missing_binary_is_unavailable(monkeypatch: MonkeyPatch) -> None:
    async def fake_exec(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("/usr/bin/systemctl")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(CommandUnavailableError):
        asyncio.run(run_command(systemctl_show_argv(DOCLING_UNIT), 5.0))


# --- systemctl show / journalctl の出力の解釈 -----------------------------------


def test_parse_systemctl_show_normal_and_not_found() -> None:
    state = parse_systemctl_show(
        "LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\n"
    )
    assert state.installed is True
    assert state.enabled is True
    assert state.active_state == "active"

    missing = parse_systemctl_show(
        "LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\n"
    )
    assert missing.installed is False
    assert missing.enabled is False

    disabled = parse_systemctl_show(
        "LoadState=loaded\nActiveState=inactive\nSubState=dead\nUnitFileState=disabled\n"
    )
    assert disabled.installed is True
    assert disabled.enabled is False


def test_parse_journal_no_entries_is_empty() -> None:
    assert parse_journal("-- No entries --", 200) == ""
    assert parse_journal("", 200) == ""


def test_parse_journal_strips_meta_lines_and_keeps_tail() -> None:
    stdout = "\n".join(
        [
            "-- Logs begin at Mon 2026-09-28 00:00:00 UTC, end at Mon 2026-09-28 01:00:00 UTC. --",
            "2026-09-28T00:00:01+0000 host gunicorn[1]: line 1",
            "-- Boot 0123456789abcdef --",
            "2026-09-28T00:00:02+0000 host gunicorn[1]: line 2",
            "2026-09-28T00:00:03+0000 host gunicorn[1]: line 3",
        ]
    )
    assert parse_journal(stdout, 2) == (
        "2026-09-28T00:00:02+0000 host gunicorn[1]: line 2\n"
        "2026-09-28T00:00:03+0000 host gunicorn[1]: line 3"
    )
    assert "line 1" in parse_journal(stdout, 200)
    assert "-- Boot" not in parse_journal(stdout, 200)


@pytest.mark.parametrize(
    "message",
    [
        "sudo: a password is required",
        "Sorry, user ragsvc is not allowed to execute '/usr/bin/systemctl enable --now x' as root",
        "ragsvc is not in the sudoers file.  This incident will be reported.",
        "Failed to enable unit: Access denied",
        "Failed to start x.service: Interactive authentication required.",
        "Hint: You are currently not seeing messages from other users and the system.",
        "No journal files were opened due to insufficient permissions.",
    ],
)
def test_permission_errors_are_detected(message: str) -> None:
    assert is_permission_error(message) is True
    assert is_systemd_unavailable(message) is False


def test_systemd_unavailable_and_generic_failures_are_distinguished() -> None:
    unavailable = (
        "System has not been booted with systemd as init system (PID 1). Can't operate.\n"
        "Failed to connect to bus: Host is down"
    )
    assert is_systemd_unavailable(unavailable) is True
    generic = "Job for x.service failed because the control process exited with error code."
    assert is_permission_error(generic) is False
    assert is_systemd_unavailable(generic) is False


# --- systemd の fake ------------------------------------------------------------


class _FakeSystemd:
    """``app.services.control.run_command`` の代わり。argv を記録し、unit の状態を返す。"""

    def __init__(
        self,
        *,
        load_state: str = "loaded",
        active_state: str = "active",
        unit_file_state: str = "enabled",
        action_result: CommandOutput | None = None,
        journal_result: CommandOutput | None = None,
        show_error: Exception | None = None,
    ) -> None:
        self.load_state = load_state
        self.active_state = active_state
        self.unit_file_state = unit_file_state
        self.action_result = action_result or CommandOutput(0, "", "")
        self.journal_result = journal_result or CommandOutput(0, "-- No entries --", "")
        self.show_error = show_error
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], timeout: float) -> CommandOutput:
        self.calls.append(list(argv))
        if argv[:2] == ["/usr/bin/systemctl", "show"]:
            if self.show_error is not None:
                raise self.show_error
            return CommandOutput(
                0,
                f"LoadState={self.load_state}\nActiveState={self.active_state}\n"
                f"SubState=running\nUnitFileState={self.unit_file_state}",
                "",
            )
        if argv[2] == "/usr/bin/journalctl":
            return self.journal_result
        return self.action_result

    def install(self, monkeypatch: MonkeyPatch) -> _FakeSystemd:
        monkeypatch.setattr(service_control, "run_command", self)
        return self


def _docling() -> ServiceCatalogEntry:
    entry = get_catalog_entry("parser-docling")
    assert entry is not None
    return entry


# --- 制御(起動/停止/再起動) ------------------------------------------------------


def test_driver_start_enables_unit_and_writes_runtime_env(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "huggingface_token", "hf_secret")
    fake = _FakeSystemd(active_state="inactive", unit_file_state="disabled").install(monkeypatch)
    result = asyncio.run(SystemdDriver().run(settings, _docling(), "start"))
    assert result.ok is True
    # unit の登録を確かめてから、enable --now で起動する(最後に操作した状態を残す)。
    assert fake.calls == [
        systemctl_show_argv(DOCLING_UNIT),
        systemctl_action_argv("start", DOCLING_UNIT),
    ]
    env_file = Path(settings.rag_service_runtime_env_file)
    assert 'HF_TOKEN="hf_secret"' in env_file.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600


def test_driver_stop_disables_unit(monkeypatch: MonkeyPatch) -> None:
    fake = _FakeSystemd().install(monkeypatch)
    result = asyncio.run(SystemdDriver().run(get_settings(), _docling(), "stop"))
    assert result.ok is True
    assert fake.calls[-1] == [
        "/usr/bin/sudo",
        "-n",
        "/usr/bin/systemctl",
        "disable",
        "--now",
        DOCLING_UNIT,
    ]


def test_driver_restart_keeps_enable_state(monkeypatch: MonkeyPatch) -> None:
    fake = _FakeSystemd().install(monkeypatch)
    result = asyncio.run(SystemdDriver().run(get_settings(), _docling(), "restart"))
    assert result.ok is True
    assert fake.calls[-1] == ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", DOCLING_UNIT]
    assert not any("enable" in call or "disable" in call for call in fake.calls)


def test_driver_missing_unit_is_not_executed(monkeypatch: MonkeyPatch) -> None:
    fake = _FakeSystemd(load_state="not-found", active_state="inactive").install(monkeypatch)
    result = asyncio.run(SystemdDriver().run(get_settings(), _docling(), "start"))
    assert result.ok is False
    assert result.reason == "unit_not_found"
    assert DOCLING_UNIT in (result.detail or "")
    # unit が無いときは sudo systemctl を呼ばない。
    assert fake.calls == [systemctl_show_argv(DOCLING_UNIT)]


def test_driver_permission_denied_is_reported(monkeypatch: MonkeyPatch) -> None:
    _FakeSystemd(action_result=CommandOutput(1, "", "sudo: a password is required")).install(
        monkeypatch
    )
    result = asyncio.run(SystemdDriver().run(get_settings(), _docling(), "stop"))
    assert result.ok is False
    assert result.reason == "permission_denied"
    assert "sudoers" in (result.detail or "")


def test_driver_generic_failure_points_to_journal(monkeypatch: MonkeyPatch) -> None:
    _FakeSystemd(
        action_result=CommandOutput(
            1, "", "Job for x.service failed because the control process exited with error code."
        )
    ).install(monkeypatch)
    result = asyncio.run(SystemdDriver().run(get_settings(), _docling(), "start"))
    assert result.ok is False
    assert result.reason == "failed"
    assert result.exit_code == 1
    assert f"journalctl -u {DOCLING_UNIT}" in (result.detail or "")


def test_driver_without_systemd_is_unavailable() -> None:
    # conftest の既定(systemd を使えない)。
    result = asyncio.run(SystemdDriver().run(get_settings(), _docling(), "start"))
    assert result.ok is False
    assert result.reason == "unavailable"


def test_control_client_raises_on_failure(monkeypatch: MonkeyPatch) -> None:
    _FakeSystemd(load_state="not-found").install(monkeypatch)
    with pytest.raises(ServiceControlError) as exc_info:
        asyncio.run(ServiceControlClient().control(get_settings(), _docling(), "stop"))
    assert exc_info.value.result.reason == "unit_not_found"


class _RecordingDriver(SystemdDriver):
    """run() の呼び出し action を記録する driver スタブ。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(
        self,
        settings: Settings,
        entry: ServiceCatalogEntry,
        action: Literal["start", "stop", "restart"],
    ) -> ControlResult:
        self.calls.append(action)
        return ControlResult(ok=True, action=action, service_id=entry.service_id, exit_code=0)


def test_control_client_delegates_to_driver() -> None:
    driver = _RecordingDriver()
    asyncio.run(ServiceControlClient(driver=driver).control(get_settings(), _docling(), "start"))
    assert driver.calls == ["start"]


def test_control_client_serializes_same_service() -> None:
    """同一サービスへの並行 control はロックで直列化される。"""
    active = 0
    max_active = 0

    class _SlowDriver(SystemdDriver):
        async def run(
            self,
            settings: Settings,
            entry: ServiceCatalogEntry,
            action: Literal["start", "stop", "restart"],
        ) -> ControlResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return ControlResult(ok=True, action=action, service_id=entry.service_id, exit_code=0)

    c = ServiceControlClient(driver=_SlowDriver())
    settings = get_settings()
    entry = _docling()

    async def _drive() -> None:
        await asyncio.gather(*(c.control(settings, entry, "start") for _ in range(3)))

    asyncio.run(_drive())
    assert max_active == 1, "同一サービスの control は同時に 1 つだけ実行されること"


# --- ログ(journalctl) ------------------------------------------------------------


def test_read_service_logs_journal_success(monkeypatch: MonkeyPatch) -> None:
    fake = _FakeSystemd(
        journal_result=CommandOutput(0, "-- Logs begin at x --\nline 1\nline 2\nline 3", "")
    ).install(monkeypatch)
    result = asyncio.run(read_service_logs(get_settings(), _docling(), 2))
    assert result == ServiceLogsResult(
        service_id="parser-docling", source="journald", lines=2, content="line 2\nline 3"
    )
    assert fake.calls[-1] == journalctl_argv(DOCLING_UNIT)


def test_read_service_logs_zero_lines_is_empty(monkeypatch: MonkeyPatch) -> None:
    _FakeSystemd().install(monkeypatch)
    result = asyncio.run(read_service_logs(get_settings(), _docling(), 200))
    assert result.content == ""


def test_read_service_logs_missing_unit_raises(monkeypatch: MonkeyPatch) -> None:
    """unit が無いと journalctl は exit 0 の空を返すため、先に unit を確かめて失敗にする。"""
    fake = _FakeSystemd(load_state="not-found").install(monkeypatch)
    with pytest.raises(ServiceLogsError) as exc_info:
        asyncio.run(read_service_logs(get_settings(), _docling(), 200))
    assert exc_info.value.reason == "unit_not_found"
    assert not any(call[2:3] == ["/usr/bin/journalctl"] for call in fake.calls)


def test_read_service_logs_permission_denied_raises(monkeypatch: MonkeyPatch) -> None:
    _FakeSystemd(journal_result=CommandOutput(1, "", "sudo: a password is required")).install(
        monkeypatch
    )
    with pytest.raises(ServiceLogsError) as exc_info:
        asyncio.run(read_service_logs(get_settings(), _docling(), 200))
    assert exc_info.value.reason == "permission_denied"


def test_read_service_logs_permission_hint_with_exit_zero_raises(monkeypatch: MonkeyPatch) -> None:
    """journal を読めないときの exit 0 + 案内だけの出力を、空のログとして返さない。"""
    _FakeSystemd(
        journal_result=CommandOutput(
            0,
            "-- No entries --",
            "Hint: You are currently not seeing messages from other users and the system.",
        )
    ).install(monkeypatch)
    with pytest.raises(ServiceLogsError) as exc_info:
        asyncio.run(read_service_logs(get_settings(), _docling(), 200))
    assert exc_info.value.reason == "permission_denied"


# --- 稼働プローブ(systemctl show + /health) ------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    """status probe 用の httpx.AsyncClient 代替。url→応答 を引く。"""

    routes: dict[str, _FakeResponse] = {}
    raise_on_connect: set[str] = set()
    calls: list[str] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        self.calls.append(url)
        base = url.removesuffix("/health")
        if base in self.raise_on_connect:
            raise ConnectionError("connection refused")
        return self.routes.get(base, _FakeResponse({"status": "ok"}))


@pytest.fixture
def fake_http(monkeypatch: MonkeyPatch) -> Iterator[type[_FakeAsyncClient]]:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.routes = {}
    _FakeAsyncClient.raise_on_connect = set()
    _FakeAsyncClient.calls = []
    yield _FakeAsyncClient
    _FakeAsyncClient.routes = {}
    _FakeAsyncClient.raise_on_connect = set()
    _FakeAsyncClient.calls = []


def _url(service_id: str) -> str:
    entry = get_catalog_entry(service_id)
    assert entry is not None
    return service_health_url(get_settings(), entry)


def _probe_statuses(settings: Any, service_ids: list[str]) -> dict[str, str]:
    """指定サービスの稼働状態を 1 件ずつ問い合わせる(画面は 1 件ずつ status を取得する)。"""

    async def probe_all() -> dict[str, str]:
        statuses: dict[str, str] = {}
        for service_id in service_ids:
            entry = get_catalog_entry(service_id)
            assert entry is not None
            statuses[service_id] = await probe_service_status(settings, entry)
        return statuses

    return asyncio.run(probe_all())


def test_probe_without_systemd_uses_health_only(fake_http: type[_FakeAsyncClient]) -> None:
    fake_http.routes = {
        _url("parser-docling"): _FakeResponse({"status": "ok"}),
        _url("parser-xml-tree-json"): _FakeResponse({"status": "degraded"}),
    }
    fake_http.raise_on_connect = {_url("parser-unstructured")}
    statuses = _probe_statuses(
        get_settings(), ["parser-docling", "parser-xml-tree-json", "parser-unstructured"]
    )
    assert statuses["parser-docling"] == "running"
    assert statuses["parser-xml-tree-json"] == "degraded"
    assert statuses["parser-unstructured"] == "stopped"


@pytest.mark.parametrize(
    ("load_state", "active_state", "reachable", "expected"),
    [
        ("loaded", "active", True, "running"),
        # unit は動いているが /health にまだ届かない(モデル読込中など)。
        ("loaded", "active", False, "starting"),
        ("loaded", "activating", False, "starting"),
        ("loaded", "failed", False, "failed"),
        ("loaded", "inactive", False, "stopped"),
        ("not-found", "inactive", False, "not_installed"),
        # unit の外のプロセスが同じポートで応答しているときは、その状態を出す。
        ("not-found", "inactive", True, "running"),
    ],
)
def test_probe_combines_unit_state_and_health(
    monkeypatch: MonkeyPatch,
    fake_http: type[_FakeAsyncClient],
    load_state: str,
    active_state: str,
    reachable: bool,
    expected: str,
) -> None:
    _FakeSystemd(load_state=load_state, active_state=active_state).install(monkeypatch)
    if not reachable:
        fake_http.raise_on_connect = {_url("parser-docling")}
    assert asyncio.run(probe_service_status(get_settings(), _docling())) == expected


def test_probe_stopped_service_is_not_retried(fake_http: type[_FakeAsyncClient]) -> None:
    url = _url("parser-docling")
    fake_http.raise_on_connect = {url}
    status = asyncio.run(probe_service_status(get_settings(), _docling()))
    assert status == "stopped"
    assert fake_http.calls == [f"{url}/health"]


def test_probe_unconfigured_when_url_blank(
    monkeypatch: MonkeyPatch, fake_http: type[_FakeAsyncClient]
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "rag_parser_docling_service_url", "")
    statuses = _probe_statuses(settings, ["parser-docling"])
    assert statuses["parser-docling"] == "unconfigured"


# --- API ----------------------------------------------------------------------


def test_list_service_catalog_returns_catalog_prod(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    enable_production_auth(monkeypatch)
    headers = login_configured_admin(client)
    monkeypatch.setattr(client, "request", partial(client.request, headers=headers))
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "rag_service_control_enabled", False)
    resp = client.get("/api/services/catalog")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # prod + flag OFF は可視化のみ。
    assert data["control_enabled"] is False
    assert data["deployment_mode"] == "production"
    assert {s["service_id"] for s in data["services"]} == {e.service_id for e in SERVICE_CATALOG}
    docling = next(s for s in data["services"] if s["service_id"] == "parser-docling")
    assert docling["systemd_unit"] == DOCLING_UNIT
    assert all(s["systemd_unit"] for s in data["services"])
    assert all("deployable" not in s for s in data["services"])


def test_list_service_catalog_exposes_model_cache_path() -> None:
    data = client.get("/api/services/catalog").json()["data"]
    docling = next(s for s in data["services"] if s["service_id"] == "parser-docling")
    assert docling["model_cache"] == {
        "path": str(Path("~/.cache").expanduser()),
        "editable": False,
    }
    unstructured = next(s for s in data["services"] if s["service_id"] == "parser-unstructured")
    assert unstructured["model_cache"] is None


def test_list_service_catalog_dev_auto_enables_control(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "rag_service_control_enabled", False)
    data = client.get("/api/services/catalog").json()["data"]
    # dev は flag OFF でも制御を自動有効化。
    assert data["control_enabled"] is True
    assert data["deployment_mode"] == "local"


def test_list_service_catalog_does_not_probe_status(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    enable_production_auth(monkeypatch)
    headers = login_configured_admin(client)
    monkeypatch.setattr(client, "request", partial(client.request, headers=headers))
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "rag_service_control_enabled", False)

    async def fail_probe(_settings: Any, _entry: ServiceCatalogEntry) -> str:
        raise AssertionError("catalog endpoint must not probe service health")

    monkeypatch.setattr("app.api.routes.services.probe_service_status", fail_probe)
    resp = client.get("/api/services/catalog")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["control_enabled"] is False
    assert len(data["services"]) == len(SERVICE_CATALOG)
    assert "status" not in data["services"][0]


def test_get_service_status_probes_only_target(monkeypatch: MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_probe(_settings: Any, entry: ServiceCatalogEntry) -> str:
        seen.append(entry.service_id)
        return "starting"

    monkeypatch.setattr("app.api.routes.services.probe_service_status", fake_probe)
    resp = client.get("/api/services/parser-xml-tree-json/status")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "starting"
    assert seen == ["parser-xml-tree-json"]


def test_get_service_logs_returns_journal_tail(monkeypatch: MonkeyPatch) -> None:
    _FakeSystemd(journal_result=CommandOutput(0, "a\nb\nc", "")).install(monkeypatch)
    resp = client.get("/api/services/parser-docling/logs?lines=2")
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "service_id": "parser-docling",
        "source": "journald",
        "lines": 2,
        "content": "b\nc",
    }


def test_get_service_logs_zero_lines_is_200_empty(monkeypatch: MonkeyPatch) -> None:
    _FakeSystemd().install(monkeypatch)
    resp = client.get("/api/services/parser-docling/logs?lines=200")
    assert resp.status_code == 200
    assert resp.json()["data"]["content"] == ""


def test_get_service_logs_missing_unit_is_404(monkeypatch: MonkeyPatch) -> None:
    _FakeSystemd(load_state="not-found").install(monkeypatch)
    resp = client.get("/api/services/parser-docling/logs?lines=200")
    assert resp.status_code == 404
    assert DOCLING_UNIT in resp.text


def test_get_service_logs_permission_denied_is_503(monkeypatch: MonkeyPatch) -> None:
    _FakeSystemd(journal_result=CommandOutput(1, "", "sudo: a password is required")).install(
        monkeypatch
    )
    resp = client.get("/api/services/parser-docling/logs?lines=200")
    assert resp.status_code == 503
    assert "sudoers" in resp.text


def test_get_service_logs_without_systemd_is_503() -> None:
    resp = client.get("/api/services/parser-docling/logs?lines=200")
    assert resp.status_code == 503
    assert "systemd" in resp.text


def test_get_service_logs_unknown_service_is_404() -> None:
    assert client.get("/api/services/unknown-service/logs").status_code == 404


def test_control_rejected_when_disabled_in_prod(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    enable_production_auth(monkeypatch)
    headers = login_configured_admin(client)
    monkeypatch.setattr(client, "request", partial(client.request, headers=headers))
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "rag_service_control_enabled", False)
    fake = _FakeSystemd().install(monkeypatch)
    assert client.post("/api/services/parser-docling/start").status_code == 409
    assert fake.calls == []


def test_control_unknown_service_is_404(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_service_control_enabled", True)
    fake = _FakeSystemd().install(monkeypatch)
    assert client.post("/api/services/pipeline-chunking/start").status_code == 404
    # backend の中で動く段はサービスを持たない(#3218)。
    assert client.post("/api/services/pipeline-vector-index/start").status_code == 404
    assert client.post("/api/services/unknown-service/stop").status_code == 404
    assert client.post("/api/services/sshd/stop").status_code == 404
    assert fake.calls == []


ROADMAP_SERVICE_ID = "parser-oci-document-understanding"


def test_catalog_marks_roadmap_service_as_unavailable() -> None:
    """OCI Document Understanding は準備中(ロードマップ)。カタログが roadmap を返す(#3374)。"""
    resp = client.get("/api/services/catalog")
    assert resp.status_code == 200
    by_id = {item["service_id"]: item for item in resp.json()["data"]["services"]}
    assert by_id[ROADMAP_SERVICE_ID]["availability"] == "roadmap"
    assert {item["availability"] for sid, item in by_id.items() if sid != ROADMAP_SERVICE_ID} == {
        "available"
    }


def test_roadmap_service_status_is_roadmap_without_probe(monkeypatch: MonkeyPatch) -> None:
    """準備中のサービスは systemd・/health を問い合わせず roadmap を返す(エラーにしない。#3374)。"""
    fake = _FakeSystemd().install(monkeypatch)
    resp = client.get(f"/api/services/{ROADMAP_SERVICE_ID}/status")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "roadmap"
    assert resp.json()["data"]["availability"] == "roadmap"
    assert fake.calls == []


@pytest.mark.parametrize("action", ["start", "restart"], ids=["start", "restart"])
def test_control_rejects_starting_roadmap_service(monkeypatch: MonkeyPatch, action: str) -> None:
    """準備中のサービスは起動・再起動を 409 で拒否し、systemctl を実行しない(#3374)。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_service_control_enabled", True)
    fake = _FakeSystemd().install(monkeypatch)
    resp = client.post(f"/api/services/{ROADMAP_SERVICE_ID}/{action}")
    assert resp.status_code == 409
    assert "準備中（ロードマップ）" in resp.text
    assert fake.calls == []


def test_control_can_stop_roadmap_service_left_by_old_deployment(
    monkeypatch: MonkeyPatch,
) -> None:
    """以前の配備に残った準備中のサービスの unit は止められる(disable --now。#3374)。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_service_control_enabled", True)
    fake = _FakeSystemd().install(monkeypatch)
    resp = client.post(f"/api/services/{ROADMAP_SERVICE_ID}/stop")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "roadmap"
    unit = f"{SYSTEMD_UNIT_PREFIX}{ROADMAP_SERVICE_ID}.service"
    assert systemctl_action_argv("stop", unit) in fake.calls


def test_docker_build_and_remove_endpoints_are_gone(monkeypatch: MonkeyPatch) -> None:
    """Docker のイメージ build / コンテナ削除の操作は無くした(#286)。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_service_control_enabled", True)
    fake = _FakeSystemd().install(monkeypatch)
    assert client.post("/api/services/parser-docling/build").status_code in {404, 405}
    assert client.post("/api/services/parser-docling/remove").status_code in {404, 405}
    assert fake.calls == []


def test_control_start_returns_updated_status(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_service_control_enabled", True)
    fake = _FakeSystemd().install(monkeypatch)

    async def fake_probe(_settings: Any, entry: ServiceCatalogEntry) -> str:
        return "starting"

    monkeypatch.setattr("app.api.routes.services.probe_service_status", fake_probe)
    resp = client.post("/api/services/parser-docling/start")
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "service_id": "parser-docling",
        "action": "start",
        "status": "starting",
    }
    assert systemctl_action_argv("start", DOCLING_UNIT) in fake.calls


@pytest.mark.parametrize(
    ("fake_kwargs", "expected_status", "expected_text"),
    [
        ({"load_state": "not-found"}, 404, DOCLING_UNIT),
        ({"action_result": CommandOutput(1, "", "sudo: a password is required")}, 503, "sudoers"),
        ({"action_result": CommandOutput(1, "", "Job failed.")}, 502, "journalctl -u"),
        ({"show_error": CommandUnavailableError("no systemctl")}, 503, "systemd"),
    ],
)
def test_control_failures_map_to_http_status(
    monkeypatch: MonkeyPatch,
    fake_kwargs: dict[str, Any],
    expected_status: int,
    expected_text: str,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_service_control_enabled", True)
    _FakeSystemd(**fake_kwargs).install(monkeypatch)
    resp = client.post("/api/services/parser-docling/stop")
    assert resp.status_code == expected_status
    assert expected_text in resp.text


# --- URL の解決 -----------------------------------------------------------------


def test_is_dev_mode_maps_environment() -> None:
    settings = get_settings()
    for value, expected in (
        ("staging", False),
        ("local", True),
        ("", False),
        ("production", False),
        ("prod", False),
    ):
        object.__setattr__(settings, "environment", value)
        try:
            assert is_dev_mode(settings) is expected
        finally:
            object.__setattr__(settings, "environment", "local")


def test_resolve_service_base_url_does_not_rewrite_legacy_docker_name(
    monkeypatch: MonkeyPatch,
) -> None:
    """以前の Docker Compose の service 名は読み替えない(#356)。設定値をそのまま使う。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "rag_parser_docling_service_url", "http://parser-docling:8000")
    assert (
        resolve_service_base_url(settings, "rag_parser_docling_service_url")
        == "http://parser-docling:8000"
    )


def test_resolve_service_base_url_dev_respects_overrides(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "local")
    # 明示上書き(host != service 名)は尊重する。
    monkeypatch.setattr(settings, "rag_parser_docling_service_url", "http://127.0.0.1:9999")
    assert (
        resolve_service_base_url(settings, "rag_parser_docling_service_url")
        == "http://127.0.0.1:9999"
    )
    # 空欄(未設定)はそのまま空文字(unconfigured)。
    monkeypatch.setattr(settings, "rag_parser_docling_service_url", "")
    assert resolve_service_base_url(settings, "rag_parser_docling_service_url") == ""


def test_resolve_service_base_url_prod_uses_setting(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "rag_parser_docling_service_url", "http://10.0.0.5:18020/")
    assert (
        resolve_service_base_url(settings, "rag_parser_docling_service_url")
        == "http://10.0.0.5:18020"
    )


def test_parser_client_service_url_uses_resolved_setting(monkeypatch: MonkeyPatch) -> None:
    """取込の委譲先は稼働プローブと同じ解決(設定値、末尾スラッシュ除去)を使う。"""
    from app.clients.parser_service import ParserServiceClient

    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "rag_parser_docling_service_url", "http://127.0.0.1:18020/")
    assert ParserServiceClient(settings).service_url("docling") == "http://127.0.0.1:18020"


def test_preprocess_service_url_uses_resolved_setting(monkeypatch: MonkeyPatch) -> None:
    from app.rag.preprocess_strategy import preprocess_service_url

    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(
        settings, "rag_preprocess_csv_to_jsonl_service_url", "http://127.0.0.1:18012/"
    )
    assert preprocess_service_url(settings, "csv_to_jsonl") == "http://127.0.0.1:18012"


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("docling", "parser-docling"),
        ("unstructured", "parser-unstructured"),
        ("xml_tree_json", "parser-xml-tree-json"),
    ],
)
def test_catalog_marks_the_current_global_parser_as_default(
    monkeypatch: MonkeyPatch, backend: str, expected: str
) -> None:
    """固定の製品名でなく、未指定のレシピが継承するエンジンを既定として表示する。"""
    monkeypatch.setattr(get_settings(), "rag_parser_adapter_backend", backend)
    response = client.get("/api/services/catalog")
    assert response.status_code == 200
    services = response.json()["data"]["services"]
    assert [
        item["service_id"] for item in services if item["execution_policy"] == "default_adapter"
    ] == [expected]
    assert all(
        item["execution_policy"] == "selected_adapter"
        for item in services
        if item["category"] == "parser" and item["service_id"] != expected
    )
