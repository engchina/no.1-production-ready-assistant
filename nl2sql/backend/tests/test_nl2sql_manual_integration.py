"""Manual integration script の lightweight 回帰テスト。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.features.nl2sql.models import (
    Nl2SqlEngine,
    SchemaRefreshJobStatus,
)
from app.settings import get_settings
from scripts import nl2sql_manual_integration as script


class _UnconfiguredEnterpriseAiClient:
    def is_configured(self) -> bool:
        return False

    def model_id(self) -> str:
        return ""

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        response_format: object | None = None,
    ) -> str:
        raise AssertionError("Enterprise AI must not be called in smoke tests")


def test_parse_engines_rejects_unknown_engine() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        script._parse_engines("auto")


def test_parse_engines_accepts_enterprise_ai_direct() -> None:
    assert script._parse_engines("enterprise_ai_direct") == [Nl2SqlEngine.ENTERPRISE_AI_DIRECT]


def test_parse_engines_rejects_removed_engine() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        script._parse_engines("enterprise_ai_direct,removed_engine")


def test_default_engines_are_enterprise_ai_direct() -> None:
    args = script._build_parser().parse_args([])
    assert args.engines == [Nl2SqlEngine.ENTERPRISE_AI_DIRECT]


def test_manual_integration_preview_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cast(Any, script).nl2sql_service,
        "_enterprise_ai_client",
        _UnconfiguredEnterpriseAiClient(),
    )
    exit_code = script.main(
        [
            "--engines",
            "enterprise_ai_direct",
            "--question",
            "社員一覧を確認したい",
            "--import-sample-data",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "[ok] diagnostics:" in output
    assert "readiness=" in output
    assert "[ok] preview_enterprise_ai_direct:" in output
    assert "provider=enterprise_ai,mode=direct" in output
    assert "PLATFORM_ORACLE_PASSWORD" not in output


def test_manual_integration_execute_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cast(Any, script).nl2sql_service,
        "_enterprise_ai_client",
        _UnconfiguredEnterpriseAiClient(),
    )
    exit_code = script.main(
        [
            "--engines",
            "enterprise_ai_direct",
            "--question",
            "社員一覧を確認したい",
            "--import-sample-data",
            "--execute",
            "--timeout",
            "5",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "[ok] preview_enterprise_ai_direct:" in output
    assert "[ok] job_enterprise_ai_direct:" in output
    assert "status=done" in output
    assert "provider=enterprise_ai,mode=direct" in output
    assert "PLATFORM_ORACLE_PASSWORD" not in output


def test_manual_integration_diagnostics_only_writes_single_step_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "diagnostics-only.json"

    monkeypatch.setattr(
        script,
        "_diagnostics_with_timeout",
        lambda *args, **kwargs: script.StepResult(
            name="diagnostics", ok=True, message="runtime=oracle; readiness=ok"
        ),
    )
    monkeypatch.setattr(
        script,
        "_refresh_catalog",
        lambda refresh: pytest.fail("diagnostics-only must not refresh catalog"),
    )

    exit_code = script.main(
        [
            "--diagnostics-only",
            "--engines",
            "enterprise_ai_direct",
            "--json-report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "[ok] diagnostics:" in output
    assert "[ok] json_report:" in output
    assert "preview_enterprise_ai_direct" not in output
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["started_at"].endswith("Z")
    assert report["finished_at"].endswith("Z")
    assert report["generated_at"] == report["finished_at"]
    assert isinstance(report["elapsed_ms"], int)
    assert report["elapsed_ms"] >= 0
    assert report["summary"] == {"failed": 0, "passed": 1, "total": 1}
    assert [step["name"] for step in report["steps"]] == ["diagnostics"]


def test_manual_integration_require_enterprise_ai_stops_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "enterprise_ai_endpoint", "")
    monkeypatch.setattr(settings, "enterprise_ai_api_key", "")
    monkeypatch.setattr(settings, "enterprise_ai_default_vision_model", "")
    monkeypatch.setattr(settings, "enterprise_ai_models", [])
    monkeypatch.setattr(settings, "enterprise_ai_default_text_model", "")

    exit_code = script.main(
        [
            "--engines",
            "enterprise_ai_direct",
            "--question",
            "請求金額を確認したい",
            "--require-enterprise-ai",
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "[ng] diagnostics:" in output
    assert "Enterprise AI Direct is not ready" in output
    assert "PLATFORM_ENTERPRISE_AI_ENDPOINT" in output
    assert "PLATFORM_ORACLE_PASSWORD" not in output


def test_manual_integration_require_feedback_embedding_stops_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_feedback_embedding_enabled", False)

    exit_code = script.main(
        [
            "--engines",
            "enterprise_ai_direct",
            "--question",
            "請求金額を確認したい",
            "--require-feedback-embedding",
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "[ng] diagnostics:" in output
    assert "Feedback embedding is not ready" in output
    assert "PLATFORM_ORACLE_PASSWORD" not in output


def test_manual_integration_require_oracle_persistence_stops_when_memory_store(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _MemoryStore:
        mode = "memory"

        def check(self) -> tuple[bool, str]:
            return True, "memory store を使用しています。"

    monkeypatch.setattr(cast(Any, script).nl2sql_service, "_store", _MemoryStore())

    exit_code = script.main(
        [
            "--engines",
            "enterprise_ai_direct",
            "--question",
            "請求金額を確認したい",
            "--require-oracle-persistence",
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "[ng] diagnostics:" in output
    assert "Oracle persistence is not ready" in output
    assert "PLATFORM_ORACLE_PASSWORD" not in output


def test_manual_integration_refresh_catalog_uses_durable_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    job = SimpleNamespace(
        job_id="manual-refresh-1",
        status=SchemaRefreshJobStatus.PENDING,
        catalog_version=None,
        error_code="",
    )
    completed = SimpleNamespace(
        job_id="manual-refresh-1",
        status=SchemaRefreshJobStatus.DONE,
        catalog_version=7,
        error_code="",
    )

    class _Service:
        def start_schema_refresh_job(self, *, dispatch: bool, source: str) -> Any:
            calls.append(f"start:{dispatch}:{source}")
            return job

        def run_next_schema_refresh_job(self) -> bool:
            calls.append("run_next")
            return True

        def get_schema_refresh_job(self, job_id: str) -> Any:
            calls.append(f"get:{job_id}")
            return completed

        def get_catalog(self) -> Any:
            calls.append("catalog")
            return SimpleNamespace(
                tables=[object(), object()],
                refreshed_at="2026-08-31T00:00:00Z",
            )

    service = _Service()
    monkeypatch.setattr(script, "nl2sql_service", service)

    result = script._refresh_catalog(True)

    assert result.ok
    assert result.name == "schema_catalog"
    assert "refreshed via job=manual-refresh-1" in result.message
    assert "tables=2" in result.message
    assert calls == [
        "start:False:manual_integration",
        "run_next",
        "get:manual-refresh-1",
        "catalog",
    ]


def test_manual_integration_execute_feedback_index_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        script,
        "_feedback_index_smoke",
        lambda *, include_bad: script.StepResult(
            name="feedback_index_rebuild",
            ok=include_bad,
            message=(
                "execute=True; executed=True; runtime=oracle; status=ready; "
                "indexable=3; indexed=3; backend=oracle_26ai; "
                "embedding_configured=True; warnings=0"
            ),
        ),
    )
    exit_code = script.main(
        [
            "--engines",
            "enterprise_ai_direct",
            "--question",
            "社員一覧を確認したい",
            "--import-sample-data",
            "--seed-demo-learning",
            "--execute-feedback-index",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "[ok] seed_demo_learning:" in output
    assert "[ok] feedback_index_rebuild:" in output
    assert "executed=True" in output
    assert "backend=oracle_26ai" in output
    assert "PLATFORM_ORACLE_PASSWORD" not in output


def test_manual_integration_full_smoke_runs_supporting_checks_and_jobs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cast(Any, script).nl2sql_service,
        "_enterprise_ai_client",
        _UnconfiguredEnterpriseAiClient(),
    )
    exit_code = script.main(
        [
            "--engines",
            "enterprise_ai_direct",
            "--question",
            "社員一覧を確認したい",
            "--import-sample-data",
            "--full-smoke",
            "--timeout",
            "5",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "[ok] support_comments:" in output
    assert "[ok] support_feedback_index:" in output
    assert "[ok] preview_enterprise_ai_direct:" in output
    assert "[ok] job_enterprise_ai_direct:" in output
    assert "PLATFORM_ORACLE_PASSWORD" not in output


def test_manual_integration_release_gate_expands_production_gate_steps(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    diagnostics_calls: list[dict[str, object]] = []
    report_path = tmp_path / "release-gate-report.json"

    def diagnostics_stub(
        require_oracle: bool,
        timeout_seconds: float,
        require_enterprise_ai: bool = False,
        require_feedback_embedding: bool = False,
        require_oracle_persistence: bool = False,
    ) -> script.StepResult:
        diagnostics_calls.append(
            {
                "require_oracle": require_oracle,
                "require_enterprise_ai": require_enterprise_ai,
                "require_oracle_persistence": require_oracle_persistence,
                "timeout_seconds": timeout_seconds,
            }
        )
        return script.StepResult(
            name="diagnostics",
            ok=True,
            message="runtime=oracle; readiness=release_gate",
        )

    monkeypatch.setattr(script, "_diagnostics_with_timeout", diagnostics_stub)
    monkeypatch.setattr(
        script,
        "_refresh_catalog",
        lambda refresh: script.StepResult(
            name="refresh_catalog", ok=True, message=f"refresh={refresh}"
        ),
    )
    monkeypatch.setattr(
        script,
        "_prepare_profile",
        lambda *, profile_id, allowed_tables, row_limit: (
            script.StepResult(name="prepare_profile", ok=True, message="profile=manual"),
            "manual_integration",
        ),
    )
    monkeypatch.setattr(
        script,
        "_supporting_features",
        lambda: [
            script.StepResult(name="support_comments", ok=True, message="suggestions=1"),
            script.StepResult(name="support_feedback_index", ok=True, message="status=ready"),
        ],
    )
    monkeypatch.setattr(
        script,
        "_preview",
        lambda **kwargs: (
            script.StepResult(
                name=f"preview_{kwargs['engine'].value}", ok=True, message="safe=True"
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        script,
        "_job",
        lambda **kwargs: script.StepResult(
            name=f"job_{kwargs['engine'].value}", ok=True, message="status=done"
        ),
    )
    exit_code = script.main(
        [
            "--release-gate",
            "--engines",
            "enterprise_ai_direct",
            "--allowed-table",
            "DENPYO_REGISTRATIONS",
            "--json-report",
            str(report_path),
            "--timeout",
            "1",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "[ok] support_feedback_index:" in output
    assert "[ok] preview_enterprise_ai_direct:" in output
    assert "[ok] job_enterprise_ai_direct:" in output
    assert "[ok] json_report:" in output
    assert diagnostics_calls[0]["require_oracle"] is True
    assert diagnostics_calls[0]["require_oracle_persistence"] is True
    assert diagnostics_calls[0]["require_enterprise_ai"] is True
    assert len(diagnostics_calls) == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "nl2sql_manual_integration_report_v1"
    assert report["release_gate"] is True
    assert report["ok"] is True
    assert report["exit_code"] == 0
    assert report["started_at"].endswith("Z")
    assert report["finished_at"].endswith("Z")
    assert report["generated_at"] == report["finished_at"]
    assert isinstance(report["elapsed_ms"], int)
    assert report["elapsed_ms"] >= 0
    assert report["engines"] == ["enterprise_ai_direct"]
    assert report["allowed_tables"] == ["DENPYO_REGISTRATIONS"]
    assert report["summary"]["failed"] == 0
    assert {step["name"] for step in report["steps"]} >= {
        "diagnostics",
        "preview_enterprise_ai_direct",
        "job_enterprise_ai_direct",
    }
    assert "PLATFORM_ORACLE_PASSWORD" not in output


def test_manual_integration_supporting_features_smoke(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = script.main(
        [
            "--engines",
            "enterprise_ai_direct",
            "--question",
            "社員一覧を確認したい",
            "--import-sample-data",
            "--check-supporting-features",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "[ok] support_comments:" in output
    assert "[ok] support_comment_sql_generation:" in output
    assert "[ok] support_feedback_index:" in output
    assert "PLATFORM_ORACLE_PASSWORD" not in output


def test_manual_integration_legacy_absorption_flags_and_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    report_path = tmp_path / "legacy-absorption.json"

    def legacy_stub(**kwargs: object) -> list[script.StepResult]:
        captured.update(kwargs)
        return [
            script.StepResult(name="legacy_classifier", ok=True, message="ready=True"),
        ]

    monkeypatch.setattr(script, "_legacy_absorption_checks", legacy_stub)
    monkeypatch.setattr(
        script,
        "_feedback_index_smoke",
        lambda *, include_bad: script.StepResult(
            name="feedback_index_rebuild",
            ok=include_bad,
            message="execute=True; executed=True",
        ),
    )
    monkeypatch.setattr(
        script,
        "_preview",
        lambda **kwargs: (
            script.StepResult(
                name=f"preview_{kwargs['engine'].value}", ok=True, message="safe=True"
            ),
            None,
        ),
    )

    exit_code = script.main(
        [
            "--check-legacy-absorption",
            "--engines",
            "enterprise_ai_direct",
            "--question",
            "請求金額を確認したい",
            "--allowed-table",
            "NL2SQL_DISPOSABLE_TABLE",
            "--execute-comments",
            "--execute-annotations",
            "--execute-feedback-index",
            "--require-classifier-oracle-state",
            "--json-report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "[ok] legacy_classifier:" in output
    assert captured["allowed_tables"] == ["NL2SQL_DISPOSABLE_TABLE"]
    assert captured["execute_comments"] is True
    assert captured["execute_annotations"] is True
    assert captured["execute_feedback_index"] is True
    assert captured["require_classifier_oracle_state"] is True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert {step["name"] for step in report["steps"]} >= {
        "legacy_classifier",
        "feedback_index_rebuild",
    }
    assert "PLATFORM_ORACLE_PASSWORD" not in output


def test_manual_integration_legacy_classifier_requires_oracle_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MemoryStore:
        mode = "memory"

    monkeypatch.setattr(cast(Any, script).nl2sql_service, "_store", _MemoryStore())

    result = script._legacy_classifier_smoke(
        question="請求金額を確認したい",
        require_oracle_state=True,
    )

    assert not result.ok
    assert result.name == "legacy_classifier"
    assert "persistence_mode=memory" in result.message


def test_manual_integration_table_mutation_requires_disposable_allowed_table() -> None:
    with pytest.raises(SystemExit):
        script.main(
            [
                "--check-legacy-absorption",
                "--execute-comments",
                "--allowed-table",
                "DENPYO_REGISTRATIONS",
            ]
        )


def test_manual_integration_stops_when_required_oracle_diagnostics_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        script,
        "_diagnostics_with_timeout",
        lambda *_args, **_kwargs: script.StepResult(
            name="diagnostics", ok=False, message="diagnostics timeout after 1.0s"
        ),
    )
    monkeypatch.setattr(
        script,
        "_refresh_catalog",
        lambda _enabled: pytest.fail("schema refresh should not run after diagnostics failure"),
    )

    exit_code = script.main(["--require-oracle", "--diagnostics-timeout", "1"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "[ng] diagnostics: diagnostics timeout after 1.0s" in output


@pytest.mark.parametrize(
    "removed_option",
    ["--refresh-assets", "--cleanup-assets", "--debug-raw-preview", "--require-refreshed-assets"],
)
def test_manual_integration_rejects_removed_options(removed_option: str) -> None:
    with pytest.raises(SystemExit):
        script.main([removed_option])
