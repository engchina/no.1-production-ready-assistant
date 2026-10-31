"""Parser adapter の runtime compatibility matrix。

Docling / Unstructured などの parser service が利用できる場合だけ、実際に
`parse_with_registry` を通して本プロジェクト schema へ remap できるかを確認する。
外部 adapter 実行は parser マイクロサービス境界へ委譲し、出力 artifact には本文を含めない。
"""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rag_parser_core.result import ExternalAdapterRunner

from app.clients.parser_service import ParserServiceClient as ParserServiceClient
from app.config import Settings
from app.rag.parser_adapter_readiness import (
    ADAPTER_ORDER,
    ParserAdapterName,
    ParserAdapterRuntimeStatus,
    parser_adapter_runtime_settings,
)
from app.rag.parser_adapter_routing import (
    SOURCE_ROUTE_KINDS,
    ParserAdapterSourceKind,
    adapter_order_for_source_kind,
    normalize_source_kind,
)
from app.rag.source_profile import build_source_profile
from app.schemas.extraction import StructuredExtraction

ParserAdapterCompatibilityStatus = Literal[
    "passed",
    "failed",
    "fallback",
    "available",
    "ignored",
    "disabled",
    "missing",
    "unsupported",
    "fixture_missing",
]


@dataclass(frozen=True)
class ParserAdapterFixtureSpec:
    """compatibility smoke に使う非機密 fixture。"""

    source_kind: ParserAdapterSourceKind
    file_name: str
    content_type: str
    case_id: str | None = None
    scenario: str | None = None


@dataclass(frozen=True)
class ParserAdapterCompatibilityCase:
    """1 adapter/source の compatibility 結果。"""

    backend: ParserAdapterName
    source_kind: ParserAdapterSourceKind
    fixture_name: str
    content_type: str
    status: ParserAdapterCompatibilityStatus
    blocking: bool
    case_id: str | None = None
    scenario: str | None = None
    parser_backend: str | None = None
    parser_version: str | None = None
    adapter_import_name: str | None = None
    adapter_distribution_name: str | None = None
    adapter_package_version: str | None = None
    template: str | None = None
    element_count: int = 0
    page_count: int = 0
    table_count: int = 0
    table_cell_count: int = 0
    asset_count: int = 0
    bbox_count: int = 0
    warning_codes: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParserAdapterCompatibilityMatrix:
    """parser adapter compatibility の非機密 summary。"""

    passed: bool
    fixture_root: str
    source_kinds: tuple[ParserAdapterSourceKind, ...]
    backends: tuple[ParserAdapterName, ...]
    case_count: int
    blocking_failure_count: int
    cases: tuple[ParserAdapterCompatibilityCase, ...]


DEFAULT_FIXTURES: Mapping[ParserAdapterSourceKind, ParserAdapterFixtureSpec] = {
    "pdf": ParserAdapterFixtureSpec(
        source_kind="pdf",
        file_name="policy-ja.pdf",
        content_type="application/pdf",
    ),
    "image": ParserAdapterFixtureSpec(
        source_kind="image",
        file_name="receipt-ja.png",
        content_type="image/png",
    ),
    "office": ParserAdapterFixtureSpec(
        source_kind="office",
        file_name="budget-ja.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "html": ParserAdapterFixtureSpec(
        source_kind="html",
        file_name="manual.html",
        content_type="text/html",
    ),
    "email": ParserAdapterFixtureSpec(
        source_kind="email",
        file_name="approval-thread.eml",
        content_type="message/rfc822",
    ),
    "text": ParserAdapterFixtureSpec(
        source_kind="text",
        file_name="runbook-code-formula.md",
        content_type="text/markdown",
    ),
}
DEFAULT_SOURCE_KINDS: tuple[ParserAdapterSourceKind, ...] = (
    "pdf",
    "image",
    "office",
    "html",
    "email",
    "text",
)
BLOCKING_FAILURE_STATUSES = frozenset(
    {"failed", "fallback", "missing", "disabled", "fixture_missing", "unsupported"}
)


def run_parser_adapter_compatibility_matrix(
    settings: Settings,
    *,
    fixture_root: Path | None = None,
    source_kinds: Sequence[object] | None = None,
    fixture_specs: Sequence[ParserAdapterFixtureSpec] | None = None,
    backends: Sequence[object] | None = None,
    require_routed: bool = False,
    require_backend_evidence: bool = False,
    require_selected_adapter_runtime: bool = True,
) -> ParserAdapterCompatibilityMatrix:
    """runtime 設定で adapter/source compatibility smoke を実行する。

    ``require_selected_adapter_runtime=False`` のときは、選択済み adapter の実行環境
    (parser サービスまたは外部 API)が見つからない ``missing`` を記録だけにして
    blocking にしない。parser サービスを起動しない CI の通常実行(#343)用で、
    昇格判定(strict / staging)は既定の True のまま fail-closed にする。実行環境が
    見つかった adapter の schema remap 失敗・fallback は従来どおり blocking にする。
    """
    resolved_fixture_root = fixture_root or _default_fixture_root()
    resolved_fixture_specs = _resolved_fixture_specs(fixture_specs, source_kinds)
    resolved_source_kinds = _resolved_matrix_source_kinds(
        resolved_fixture_specs,
        source_kinds,
    )
    resolved_backends = _normalized_backends(backends)
    runtime = parser_adapter_runtime_settings(settings)
    external_adapter_runner = ParserServiceClient(settings).runner
    adapter_by_backend = {adapter.backend: adapter for adapter in runtime.adapters}
    cases = tuple(
        _compatibility_case(
            settings,
            fixture_root=resolved_fixture_root,
            adapter=adapter_by_backend[backend],
            fixture=fixture,
            require_routed=require_routed,
            require_selected_adapter_runtime=require_selected_adapter_runtime,
            external_adapter_runner=external_adapter_runner,
        )
        for backend in resolved_backends
        for fixture in resolved_fixture_specs
    )
    if require_backend_evidence:
        cases = cases + _backend_evidence_failure_cases(
            adapters=tuple(adapter_by_backend[backend] for backend in resolved_backends),
            cases=cases,
            source_kinds=resolved_source_kinds,
        )
    blocking_failure_count = sum(
        1 for case in cases if case.blocking and case.status in BLOCKING_FAILURE_STATUSES
    )
    return ParserAdapterCompatibilityMatrix(
        passed=blocking_failure_count == 0,
        fixture_root=str(resolved_fixture_root),
        source_kinds=resolved_source_kinds,
        backends=resolved_backends,
        case_count=len(cases),
        blocking_failure_count=blocking_failure_count,
        cases=cases,
    )


def parser_service_probe_settings(settings: Settings) -> Settings:
    """adapter の可用性を parser サービスの /health で判定する設定にする(#343)。

    contract の parse は `ParserServiceClient` で parser サービス(または外部 API)へ
    HTTP 委譲するため、可用性も同じサービスで判定する。backend の venv に Docling /
    Unstructured の package があるかどうかは、今の構成(#286)では可用性と関係しない。
    """
    return settings.model_copy(update={"rag_parser_readiness_probe_enabled": True})


def strict_parser_adapter_settings(settings: Settings) -> Settings:
    """staging smoke 用に代表 parser adapter を明示選択し、adapter flag を有効化する。"""
    return settings.model_copy(
        update={
            "rag_parser_adapter_backend": "docling",
            "rag_parser_docling_enabled": True,
            "rag_parser_unstructured_enabled": True,
        }
    )


def parser_adapter_fixture_root_from_manifest(
    manifest: Mapping[str, object],
    *,
    manifest_path: Path | None = None,
) -> Path:
    """manifest の fixture_root を staging manifest 基準で解決する。"""
    raw_fixture_root = manifest.get("fixture_root")
    if not isinstance(raw_fixture_root, str) or not raw_fixture_root.strip():
        return _default_fixture_root()
    configured_path = Path(raw_fixture_root)
    if configured_path.is_absolute():
        return configured_path.resolve()
    candidates: list[Path] = []
    if manifest_path is not None:
        candidates.append(manifest_path.parent / configured_path)
    candidates.append(Path.cwd() / configured_path)
    candidates.append(_default_fixture_root())
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def parser_adapter_fixture_specs_from_manifest(
    manifest: Mapping[str, object],
    *,
    require_declared_schema_remap: bool = False,
) -> tuple[ParserAdapterFixtureSpec, ...]:
    """staging manifest cases を adapter smoke 用 fixture spec に変換する。"""
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list):
        return ()
    specs: list[ParserAdapterFixtureSpec] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            continue
        if require_declared_schema_remap and raw_case.get("adapter_schema_remap") is not True:
            continue
        if not _manifest_case_requires_schema_remap(raw_case):
            continue
        fixture_name = _string_value(raw_case.get("fixture"))
        if not fixture_name:
            continue
        source_kind = normalize_source_kind(raw_case.get("modality"))
        if source_kind not in SOURCE_ROUTE_KINDS:
            continue
        specs.append(
            ParserAdapterFixtureSpec(
                source_kind=source_kind,
                file_name=fixture_name,
                content_type=_fixture_content_type(fixture_name, source_kind),
                case_id=_string_value(raw_case.get("id")) or f"case-{index + 1}",
                scenario=_string_value(raw_case.get("scenario")) or None,
            )
        )
    return tuple(specs)


def parser_adapter_contract_summary(
    matrix: ParserAdapterCompatibilityMatrix,
) -> dict[str, object]:
    """matrix を CI/UI が使いやすい低機密 summary へ畳み込む。"""
    backend_status_counts: dict[str, dict[str, int]] = {}
    backend_source_status_counts: dict[str, dict[str, dict[str, int]]] = {}
    source_kind_status_counts: dict[str, dict[str, int]] = {}
    backend_passed_source_kinds: dict[str, set[str]] = {}
    backend_passed_scenarios: dict[str, set[str]] = {}
    blocking_failure_source_kinds: set[str] = set()
    blocking_failure_backends: set[str] = set()
    reason_code_counts: dict[str, int] = {}
    warning_code_counts: dict[str, int] = {}
    blocking_failure_reason_counts: dict[str, int] = {}
    passed_source_kinds: set[str] = set()
    scenarios: set[str] = set()
    passed_scenarios: set[str] = set()
    blocking_failure_scenarios: set[str] = set()
    passed_case_refs: set[str] = set()
    backend_passed_case_refs: dict[str, set[str]] = {}
    blocking_failure_case_refs: set[str] = set()
    blocking_failures: list[dict[str, object]] = []
    for case in matrix.cases:
        case_ref = _case_ref_label(case)
        if case.scenario:
            scenarios.add(case.scenario)
        backend_status_counts.setdefault(case.backend, {})
        backend_status_counts[case.backend][case.status] = (
            backend_status_counts[case.backend].get(case.status, 0) + 1
        )
        backend_source_status_counts.setdefault(case.backend, {})
        backend_source_status_counts[case.backend].setdefault(case.source_kind, {})
        backend_source_status_counts[case.backend][case.source_kind][case.status] = (
            backend_source_status_counts[case.backend][case.source_kind].get(
                case.status,
                0,
            )
            + 1
        )
        source_kind_status_counts.setdefault(case.source_kind, {})
        source_kind_status_counts[case.source_kind][case.status] = (
            source_kind_status_counts[case.source_kind].get(case.status, 0) + 1
        )
        for reason_code in case.reason_codes:
            reason_code_counts[reason_code] = reason_code_counts.get(reason_code, 0) + 1
        for warning_code in case.warning_codes:
            warning_code_counts[warning_code] = warning_code_counts.get(warning_code, 0) + 1
        if case.status == "passed":
            passed_case_refs.add(case_ref)
            backend_passed_case_refs.setdefault(case.backend, set()).add(case_ref)
            passed_source_kinds.add(case.source_kind)
            backend_passed_source_kinds.setdefault(case.backend, set()).add(case.source_kind)
            if case.scenario:
                passed_scenarios.add(case.scenario)
                backend_passed_scenarios.setdefault(case.backend, set()).add(case.scenario)
        if case.blocking and case.status in BLOCKING_FAILURE_STATUSES:
            blocking_failure_case_refs.add(case_ref)
            blocking_failure_source_kinds.add(case.source_kind)
            blocking_failure_backends.add(case.backend)
            if case.scenario:
                blocking_failure_scenarios.add(case.scenario)
            for reason_code in case.reason_codes:
                blocking_failure_reason_counts[reason_code] = (
                    blocking_failure_reason_counts.get(reason_code, 0) + 1
                )
            failure: dict[str, object] = {
                "backend": case.backend,
                "source_kind": case.source_kind,
                "status": case.status,
                "warning_codes": list(case.warning_codes),
                "reason_codes": list(case.reason_codes),
            }
            if case.case_id:
                failure["case_id"] = case.case_id
            if case.scenario:
                failure["scenario"] = case.scenario
            blocking_failures.append(failure)
    return {
        "passed": matrix.passed,
        "case_count": matrix.case_count,
        "blocking_failure_count": matrix.blocking_failure_count,
        "source_kinds": list(matrix.source_kinds),
        "backends": list(matrix.backends),
        "passed_source_kinds": sorted(passed_source_kinds),
        "missing_source_kinds": sorted(set(matrix.source_kinds) - passed_source_kinds),
        "passed_case_refs": sorted(passed_case_refs),
        "backend_passed_case_refs": {
            backend: sorted(case_refs)
            for backend, case_refs in sorted(backend_passed_case_refs.items())
        },
        "scenarios": sorted(scenarios),
        "passed_scenarios": sorted(passed_scenarios),
        "missing_scenarios": sorted(scenarios - passed_scenarios),
        "blocking_failure_scenarios": sorted(blocking_failure_scenarios),
        "blocking_failure_source_kinds": sorted(blocking_failure_source_kinds),
        "blocking_failure_backends": sorted(blocking_failure_backends),
        "blocking_failure_case_refs": sorted(blocking_failure_case_refs),
        "backend_status_counts": backend_status_counts,
        "backend_source_status": _aggregate_backend_source_status(backend_source_status_counts),
        "backend_source_status_counts": backend_source_status_counts,
        "source_kind_status_counts": source_kind_status_counts,
        "backend_passed_source_kinds": {
            backend: sorted(source_kinds)
            for backend, source_kinds in sorted(backend_passed_source_kinds.items())
        },
        "backend_passed_scenarios": {
            backend: sorted(scenarios)
            for backend, scenarios in sorted(backend_passed_scenarios.items())
        },
        "reason_code_counts": reason_code_counts,
        "warning_code_counts": warning_code_counts,
        "blocking_failure_reason_counts": blocking_failure_reason_counts,
        "blocking_failures": blocking_failures,
    }


def _aggregate_backend_source_status(
    status_counts: Mapping[str, Mapping[str, Mapping[str, int]]],
) -> dict[str, dict[str, str]]:
    """複数 case の backend/source status を UI 用の代表 status へ集約する。"""
    return {
        backend: {
            source_kind: _aggregate_status(counts)
            for source_kind, counts in sorted(source_counts.items())
        }
        for backend, source_counts in sorted(status_counts.items())
    }


def _aggregate_status(status_counts: Mapping[str, int]) -> str:
    for status in (
        "failed",
        "fallback",
        "fixture_missing",
        "missing",
        "disabled",
        "passed",
        "available",
        "ignored",
        "unsupported",
    ):
        if status_counts.get(status, 0) > 0:
            return status
    return "failed"


def parser_adapter_contract_artifact_payload(
    matrix: ParserAdapterCompatibilityMatrix,
) -> dict[str, object]:
    """matrix を CI/API 用の非機密 artifact payload へ変換する。

    内部 matrix は fixture root や fixture file name を保持するが、staging / nightly
    artifact は real-world manifest も扱うため、出力では安定 hash label だけを残す。
    """
    return {
        "passed": matrix.passed,
        "fixture_root": _redacted_label("fixture_root", matrix.fixture_root),
        "fixture_root_hash": _stable_hash(matrix.fixture_root),
        "source_kinds": list(matrix.source_kinds),
        "backends": list(matrix.backends),
        "case_count": matrix.case_count,
        "blocking_failure_count": matrix.blocking_failure_count,
        "cases": [_artifact_case_payload(case) for case in matrix.cases],
        "summary": _artifact_summary_payload(parser_adapter_contract_summary(matrix)),
    }


def _artifact_case_payload(case: ParserAdapterCompatibilityCase) -> dict[str, object]:
    payload: dict[str, object] = {
        "backend": case.backend,
        "source_kind": case.source_kind,
        "fixture_name": _redacted_label(
            f"{case.source_kind}_fixture",
            case.fixture_name,
        ),
        "fixture_name_hash": _stable_hash(case.fixture_name),
        "content_type": case.content_type,
        "status": case.status,
        "blocking": case.blocking,
        "element_count": case.element_count,
        "page_count": case.page_count,
        "table_count": case.table_count,
        "table_cell_count": case.table_cell_count,
        "asset_count": case.asset_count,
        "bbox_count": case.bbox_count,
        "warning_codes": list(case.warning_codes),
        "reason_codes": list(case.reason_codes),
    }
    if case.case_id:
        payload["case_ref_hash"] = _stable_hash(case.case_id)
    if case.scenario:
        payload["scenario"] = case.scenario
    if case.parser_backend is not None:
        payload["parser_backend"] = case.parser_backend
    if case.parser_version is not None:
        payload["parser_version"] = case.parser_version
    if case.adapter_import_name is not None:
        payload["adapter_import_name"] = case.adapter_import_name
    if case.adapter_distribution_name is not None:
        payload["adapter_distribution_name"] = case.adapter_distribution_name
    if case.adapter_package_version is not None:
        payload["adapter_package_version"] = case.adapter_package_version
    if case.template is not None:
        payload["template"] = case.template
    return payload


def _artifact_summary_payload(summary: Mapping[str, object]) -> dict[str, object]:
    payload = dict(summary)
    failures: list[dict[str, object]] = []
    raw_failures = payload.get("blocking_failures")
    failure_items = (
        raw_failures
        if isinstance(raw_failures, Sequence)
        and not isinstance(raw_failures, str | bytes | bytearray)
        else ()
    )
    for raw_failure in failure_items:
        if not isinstance(raw_failure, Mapping):
            continue
        failure = dict(raw_failure)
        case_id = failure.pop("case_id", None)
        if isinstance(case_id, str) and case_id:
            failure["case_ref_hash"] = _stable_hash(case_id)
        failures.append(failure)
    payload["blocking_failures"] = failures
    return payload


def _redacted_label(prefix: str, value: str) -> str:
    return f"{prefix}:{_stable_hash(value)}"


def _case_ref_label(case: ParserAdapterCompatibilityCase) -> str:
    raw_value = case.case_id or f"{case.source_kind}:{case.fixture_name}"
    return f"case:{_stable_hash(raw_value)}"


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _compatibility_case(
    settings: Settings,
    *,
    fixture_root: Path,
    adapter: ParserAdapterRuntimeStatus,
    fixture: ParserAdapterFixtureSpec,
    require_routed: bool,
    require_selected_adapter_runtime: bool,
    external_adapter_runner: ExternalAdapterRunner,
) -> ParserAdapterCompatibilityCase:
    source_kind = fixture.source_kind
    blocking = adapter.selected
    if adapter.status != "active":
        return _compatibility_case_result(
            adapter=adapter,
            fixture=fixture,
            status=_inactive_adapter_status(adapter),
            blocking=blocking and (require_selected_adapter_runtime or adapter.status != "missing"),
            warning_codes=(adapter.warning_code,) if adapter.warning_code else (),
            reason_codes=(f"adapter_{adapter.status}",),
        )
    if adapter.backend not in adapter_order_for_source_kind(source_kind):
        return _compatibility_case_result(
            adapter=adapter,
            fixture=fixture,
            status="unsupported",
            blocking=blocking and require_routed,
            reason_codes=("adapter_not_routed_for_source",),
        )
    fixture_path = fixture_root / fixture.file_name
    try:
        data = fixture_path.read_bytes()
    except FileNotFoundError:
        return _compatibility_case_result(
            adapter=adapter,
            fixture=fixture,
            status="fixture_missing",
            blocking=blocking,
            reason_codes=("fixture_missing",),
        )
    # registry(解析実体)は contract 検証時のみ遅延 import する。module import で
    # app.main グラフへ registry を持ち込まないため(backend は解析コードを load しない)。
    from rag_parser_core.registry import parse_with_registry

    result = parse_with_registry(
        data,
        source_profile=build_source_profile(
            original_file_name=fixture.file_name,
            sanitized_file_name=fixture.file_name,
            content_type=fixture.content_type,
            file_size_bytes=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        ),
        content_type=fixture.content_type,
        adapter_backend=adapter.backend,
        docling_enabled=adapter.backend == "docling",
        unstructured_enabled=adapter.backend == "unstructured",
        external_adapter_runner=external_adapter_runner,
    )
    if result.extraction is None or result.parser_backend != adapter.backend:
        return _compatibility_case_result(
            adapter=adapter,
            fixture=fixture,
            status="fallback",
            blocking=blocking,
            parser_backend=result.parser_backend,
            parser_version=result.parser_version,
            template=result.template,
            warning_codes=result.warnings,
            reason_codes=("adapter_fallback_used",),
        )
    contract = _schema_contract(result.extraction)
    schema_violations = _schema_contract_violations(
        source_kind,
        result.extraction,
        contract,
        adapter=adapter,
    )
    status: ParserAdapterCompatibilityStatus = "failed" if schema_violations else "passed"
    return _compatibility_case_result(
        adapter=adapter,
        fixture=fixture,
        status=status,
        blocking=blocking,
        parser_backend=result.parser_backend,
        parser_version=result.parser_version,
        template=result.template,
        element_count=contract["element_count"],
        page_count=contract["page_count"],
        table_count=contract["table_count"],
        table_cell_count=contract["table_cell_count"],
        asset_count=contract["asset_count"],
        bbox_count=contract["bbox_count"],
        warning_codes=result.warnings,
        reason_codes=(("schema_remap_contract_ok",) if status == "passed" else schema_violations),
    )


def _compatibility_case_result(
    *,
    adapter: ParserAdapterRuntimeStatus,
    fixture: ParserAdapterFixtureSpec,
    status: ParserAdapterCompatibilityStatus,
    blocking: bool,
    parser_backend: str | None = None,
    parser_version: str | None = None,
    template: str | None = None,
    element_count: int = 0,
    page_count: int = 0,
    table_count: int = 0,
    table_cell_count: int = 0,
    asset_count: int = 0,
    bbox_count: int = 0,
    warning_codes: tuple[str, ...] = (),
    reason_codes: tuple[str, ...] = (),
) -> ParserAdapterCompatibilityCase:
    return ParserAdapterCompatibilityCase(
        backend=adapter.backend,
        source_kind=fixture.source_kind,
        fixture_name=fixture.file_name,
        content_type=fixture.content_type,
        case_id=fixture.case_id,
        scenario=fixture.scenario,
        status=status,
        blocking=blocking,
        parser_backend=parser_backend,
        parser_version=parser_version,
        adapter_import_name=adapter.import_name,
        adapter_distribution_name=adapter.distribution_name,
        adapter_package_version=adapter.version,
        template=template,
        element_count=element_count,
        page_count=page_count,
        table_count=table_count,
        table_cell_count=table_cell_count,
        asset_count=asset_count,
        bbox_count=bbox_count,
        warning_codes=warning_codes,
        reason_codes=reason_codes,
    )


def _backend_evidence_failure_cases(
    *,
    adapters: Sequence[ParserAdapterRuntimeStatus],
    cases: Sequence[ParserAdapterCompatibilityCase],
    source_kinds: Sequence[ParserAdapterSourceKind],
) -> tuple[ParserAdapterCompatibilityCase, ...]:
    """strict smoke で selected adapter/source ごとの schema remap 証跡を必須にする。"""
    failures: list[ParserAdapterCompatibilityCase] = []
    cases_by_backend: dict[ParserAdapterName, list[ParserAdapterCompatibilityCase]] = {}
    for case in cases:
        cases_by_backend.setdefault(case.backend, []).append(case)
    evidence_fixture = ParserAdapterFixtureSpec(
        source_kind="unknown",
        file_name="manifest-fixture-set",
        content_type="application/octet-stream",
        scenario="adapter_schema_remap_evidence",
    )
    for adapter in adapters:
        if not adapter.selected:
            continue
        backend_cases = cases_by_backend.get(adapter.backend, [])
        source_failures = _backend_source_evidence_failure_cases(
            adapter,
            backend_cases=backend_cases,
            source_kinds=source_kinds,
        )
        failures.extend(source_failures)
        if source_failures:
            continue
        if any(case.status == "passed" for case in backend_cases):
            continue
        if any(
            case.blocking and case.status in BLOCKING_FAILURE_STATUSES for case in backend_cases
        ):
            continue
        failures.append(
            _compatibility_case_result(
                adapter=adapter,
                fixture=evidence_fixture,
                status=(
                    _inactive_adapter_status(adapter) if adapter.status != "active" else "failed"
                ),
                blocking=True,
                warning_codes=(adapter.warning_code,) if adapter.warning_code else (),
                reason_codes=(
                    (
                        "adapter_schema_remap_evidence_missing"
                        if adapter.status == "active"
                        else f"adapter_{adapter.status}"
                    ),
                ),
            )
        )
    return tuple(failures)


def _backend_source_evidence_failure_cases(
    adapter: ParserAdapterRuntimeStatus,
    *,
    backend_cases: Sequence[ParserAdapterCompatibilityCase],
    source_kinds: Sequence[ParserAdapterSourceKind],
) -> tuple[ParserAdapterCompatibilityCase, ...]:
    """manifest source ごとに実 fixture remap evidence があるか確認する。"""
    failures: list[ParserAdapterCompatibilityCase] = []
    cases_by_source: dict[ParserAdapterSourceKind, list[ParserAdapterCompatibilityCase]] = {}
    for case in backend_cases:
        cases_by_source.setdefault(case.source_kind, []).append(case)
    for source_kind in source_kinds:
        if adapter.backend not in adapter_order_for_source_kind(source_kind):
            continue
        source_cases = cases_by_source.get(source_kind, [])
        if any(case.status == "passed" for case in source_cases):
            continue
        if any(case.blocking and case.status in BLOCKING_FAILURE_STATUSES for case in source_cases):
            continue
        reason_code = (
            "adapter_schema_remap_fixture_missing_for_source"
            if not source_cases
            else "adapter_schema_remap_evidence_missing_for_source"
        )
        if adapter.status != "active":
            reason_code = f"adapter_{adapter.status}"
        failures.append(
            _compatibility_case_result(
                adapter=adapter,
                fixture=ParserAdapterFixtureSpec(
                    source_kind=source_kind,
                    file_name=f"manifest-{source_kind}-fixture-set",
                    content_type=_CONTENT_TYPE_BY_SOURCE_KIND.get(
                        source_kind,
                        "application/octet-stream",
                    ),
                    scenario="adapter_schema_remap_source_evidence",
                ),
                status=(
                    _inactive_adapter_status(adapter) if adapter.status != "active" else "failed"
                ),
                blocking=True,
                warning_codes=(adapter.warning_code,) if adapter.warning_code else (),
                reason_codes=(reason_code,),
            )
        )
    return tuple(failures)


def _inactive_adapter_status(
    adapter: ParserAdapterRuntimeStatus,
) -> ParserAdapterCompatibilityStatus:
    if adapter.status == "available":
        return "available"
    if adapter.status == "ignored":
        return "ignored"
    if adapter.status == "disabled":
        return "disabled"
    if adapter.status == "missing":
        return "missing"
    return "failed"


def _schema_contract(extraction: StructuredExtraction) -> dict[str, int]:
    return {
        "element_count": len(extraction.elements),
        "page_count": len(extraction.pages),
        "table_count": len(extraction.tables),
        "table_cell_count": sum(len(table.cells) for table in extraction.tables),
        "asset_count": len(extraction.assets),
        "bbox_count": sum(1 for element in extraction.elements if element.bbox)
        + sum(1 for table in extraction.tables for cell in table.cells if cell.bbox)
        + sum(1 for asset in extraction.assets if asset.bbox),
    }


def _schema_contract_violations(
    source_kind: ParserAdapterSourceKind,
    extraction: StructuredExtraction,
    contract: Mapping[str, int],
    *,
    adapter: ParserAdapterRuntimeStatus,
) -> tuple[str, ...]:
    """source kind ごとの最低限 schema remap 契約違反を返す。"""
    if contract["element_count"] <= 0:
        return ("schema_remap_empty",)
    violations: list[str] = []
    if any(not element.element_id for element in extraction.elements):
        violations.append("schema_remap_element_id_missing")
    if any(not element.source_parser for element in extraction.elements):
        violations.append("schema_remap_source_parser_missing")
    if not adapter.import_name:
        violations.append("adapter_import_name_missing")
    if not adapter.distribution_name:
        violations.append("adapter_distribution_name_missing")
    if not adapter.version:
        violations.append("adapter_package_version_missing")
    if source_kind in {"pdf", "image"} and not _has_page_lineage(extraction):
        violations.append("schema_remap_page_lineage_missing")
    if source_kind == "image" and contract["bbox_count"] <= 0 and contract["asset_count"] <= 0:
        violations.append("schema_remap_visual_lineage_missing")
    if source_kind == "office" and not _has_office_lineage(extraction):
        violations.append("schema_remap_office_lineage_missing")
    if source_kind == "html" and not _has_html_semantic_lineage(extraction):
        violations.append("schema_remap_html_semantic_lineage_missing")
    if source_kind == "email" and not _has_email_lineage(extraction):
        violations.append("schema_remap_email_lineage_missing")
    if source_kind == "text" and not extraction.raw_text.strip():
        violations.append("schema_remap_text_empty")
    return tuple(violations)


def _has_page_lineage(extraction: StructuredExtraction) -> bool:
    if extraction.pages:
        return True
    return any(element.page_number is not None for element in extraction.elements)


def _has_office_lineage(extraction: StructuredExtraction) -> bool:
    if extraction.tables or extraction.pages:
        return True
    return any(
        (element.content_kind or element.kind) in {"slide", "sheet", "table"}
        or element.page_number is not None
        or (
            element.kind == "title"
            and element.metadata.get("chunk_template") == "office_document"
            and (
                element.section_path
                or element.metadata.get("section_level") is not None
                or element.metadata.get("adapter_element_type") is not None
            )
        )
        for element in extraction.elements
    )


def _has_html_semantic_lineage(extraction: StructuredExtraction) -> bool:
    if extraction.tables or extraction.assets:
        return True
    return any(
        element.section_path or element.kind == "title" or "link_count" in element.metadata
        for element in extraction.elements
    )


def _has_email_lineage(extraction: StructuredExtraction) -> bool:
    if any(asset.kind == "email_attachment" for asset in extraction.assets):
        return True
    return any(
        (element.content_kind or element.kind) == "email"
        or element.metadata.get("email_part") in {"headers", "body"}
        for element in extraction.elements
    )


def _default_fixture_root() -> Path:
    return Path(__file__).resolve().parents[3] / "evaluation" / "file-processing-fixtures"


def _resolved_fixture_specs(
    fixture_specs: Sequence[ParserAdapterFixtureSpec] | None,
    source_kinds: Sequence[object] | None,
) -> tuple[ParserAdapterFixtureSpec, ...]:
    if fixture_specs is None:
        return tuple(
            DEFAULT_FIXTURES[source_kind] for source_kind in _normalized_source_kinds(source_kinds)
        )
    source_kind_filter = (
        set(_normalized_source_kinds(source_kinds)) if source_kinds is not None else None
    )
    resolved: list[ParserAdapterFixtureSpec] = []
    for fixture in fixture_specs:
        source_kind = normalize_source_kind(fixture.source_kind)
        if source_kind not in SOURCE_ROUTE_KINDS:
            continue
        if source_kind_filter is not None and source_kind not in source_kind_filter:
            continue
        resolved.append(
            ParserAdapterFixtureSpec(
                source_kind=source_kind,
                file_name=fixture.file_name,
                content_type=fixture.content_type,
                case_id=fixture.case_id,
                scenario=fixture.scenario,
            )
        )
    return tuple(resolved)


def _resolved_matrix_source_kinds(
    fixture_specs: Sequence[ParserAdapterFixtureSpec],
    source_kinds: Sequence[object] | None,
) -> tuple[ParserAdapterSourceKind, ...]:
    if source_kinds is not None:
        return _normalized_source_kinds(source_kinds)
    return tuple(dict.fromkeys(fixture.source_kind for fixture in fixture_specs))


def _normalized_source_kinds(
    source_kinds: Sequence[object] | None,
) -> tuple[ParserAdapterSourceKind, ...]:
    raw_values = source_kinds or DEFAULT_SOURCE_KINDS
    normalized = tuple(dict.fromkeys(normalize_source_kind(value) for value in raw_values))
    return tuple(
        source_kind
        for source_kind in normalized
        if source_kind in SOURCE_ROUTE_KINDS and source_kind in DEFAULT_FIXTURES
    )


def _normalized_backends(
    backends: Sequence[object] | None,
) -> tuple[ParserAdapterName, ...]:
    if not backends:
        return ADAPTER_ORDER
    normalized = tuple(dict.fromkeys(str(backend).strip().casefold() for backend in backends))
    return tuple(backend for backend in normalized if backend in ADAPTER_ORDER)


def _manifest_case_requires_schema_remap(case: Mapping[str, object]) -> bool:
    """negative/unsupported staging cases を adapter remap smoke から外す。"""
    if _string_value(case.get("expected_warning")):
        return False
    if _string_value(case.get("expected_unsupported_reason")):
        return False
    if _string_value(case.get("expected_parser_profile")).startswith("unsupported_"):
        return False
    return not _string_value(case.get("expected_chunk_template")).startswith("unsupported_")


def _fixture_content_type(
    file_name: str,
    source_kind: ParserAdapterSourceKind,
) -> str:
    suffix = Path(file_name).suffix.casefold()
    if suffix in _CONTENT_TYPE_BY_EXTENSION:
        return _CONTENT_TYPE_BY_EXTENSION[suffix]
    content_type, _ = mimetypes.guess_type(file_name)
    if content_type:
        return content_type
    return _CONTENT_TYPE_BY_SOURCE_KIND.get(source_kind, "application/octet-stream")


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


_CONTENT_TYPE_BY_EXTENSION: Mapping[str, str] = {
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".eml": "message/rfc822",
    ".htm": "text/html",
    ".html": "text/html",
    ".json": "application/json",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_CONTENT_TYPE_BY_SOURCE_KIND: Mapping[ParserAdapterSourceKind, str] = {
    "email": "message/rfc822",
    "html": "text/html",
    "image": "image/png",
    "office": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "text": "text/plain",
    "unknown": "application/octet-stream",
}
