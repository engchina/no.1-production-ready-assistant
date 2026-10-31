"""parser adapter scorecard のテスト。"""

from collections.abc import Sequence

import pytest
from pytest import MonkeyPatch

from app.config import Settings
from app.rag import parser_adapter_readiness
from app.rag.parser_adapter_readiness import parser_adapter_runtime_settings
from app.rag.parser_adapter_scorecard import (
    ParserAdapterScoreBackend,
    ParserAdapterScorecard,
    ParserAdapterScorecardEntry,
    build_parser_adapter_scorecard,
    build_parser_adapter_source_routes,
)


def test_scorecard_recommends_active_adapter_with_strong_downstream_metrics(
    monkeypatch: MonkeyPatch,
) -> None:
    """導入済み adapter は staging 指標が良ければ local より上位になる。"""
    settings = Settings(
        rag_parser_adapter_backend="docling",
        rag_parser_docling_enabled=True,
    )
    monkeypatch.setattr(parser_adapter_readiness, "_package_info", _docling_installed)

    runtime = parser_adapter_runtime_settings(settings)
    scorecard = build_parser_adapter_scorecard(
        runtime,
        metrics={
            "retrieval_recall": 1.0,
            "table_qa_accuracy": 1.0,
            "page_hit_accuracy": 1.0,
            "element_lineage_coverage": 1.0,
            "source_kind_coverage": 1.0,
            "backend_source_kind_coverage": 1.0,
            "adapter_contract_coverage": 1.0,
            "parser_fallback_rate": 0.0,
            "failed_segment_rate": 0.0,
            "ingestion_p95_ms": 4_000.0,
        },
        metrics_source="file_processing_staging",
    )

    assert scorecard.recommended_backend == "docling"
    docling = _entry(scorecard, "docling")
    assert docling.recommended is True
    assert docling.status == "recommended"
    assert docling.metric_count == 10
    assert docling.metric_source == "file_processing_staging"
    assert "downstream_metrics_applied" in docling.reason_codes
    assert _entry(scorecard, "local").metric_source == "none"


def test_scorecard_falls_back_to_local_when_selected_adapter_is_missing(
    monkeypatch: MonkeyPatch,
) -> None:
    """明示 adapter が未導入なら scorecard は executable な local を推奨する。"""
    settings = Settings(
        rag_parser_adapter_backend="docling",
        rag_parser_docling_enabled=True,
    )
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )

    runtime = parser_adapter_runtime_settings(settings)
    scorecard = build_parser_adapter_scorecard(runtime)

    assert scorecard.recommended_backend == "local"
    assert _entry(scorecard, "local").recommended is True
    docling = _entry(scorecard, "docling")
    assert docling.executable is False
    assert docling.warning_codes == ("adapter_package_missing",)


def test_scorecard_penalizes_poor_downstream_metrics(
    monkeypatch: MonkeyPatch,
) -> None:
    """active adapter でも staging 指標が悪ければ local fallback を推奨する。"""
    settings = Settings(
        rag_parser_adapter_backend="docling",
        rag_parser_docling_enabled=True,
    )
    monkeypatch.setattr(parser_adapter_readiness, "_package_info", _docling_installed)

    runtime = parser_adapter_runtime_settings(settings)
    scorecard = build_parser_adapter_scorecard(
        runtime,
        metrics={
            "retrieval_recall": 0.0,
            "table_qa_accuracy": 0.0,
            "page_hit_accuracy": 0.0,
            "parser_fallback_rate": 1.0,
            "failed_segment_rate": 1.0,
            "ingestion_p95_ms": 60_000.0,
        },
        metrics_source="file_processing_staging",
    )

    assert scorecard.recommended_backend == "local"
    docling = _entry(scorecard, "docling")
    assert docling.metric_count == 6
    assert docling.score < _entry(scorecard, "local").score
    assert "downstream_metrics_applied" in docling.reason_codes


def test_scorecard_requires_core_downstream_metrics_before_recommending_adapter(
    monkeypatch: MonkeyPatch,
) -> None:
    """一部の良い指標だけでは外部 adapter を推奨しない。"""
    settings = Settings(
        rag_parser_adapter_backend="docling",
        rag_parser_docling_enabled=True,
    )
    monkeypatch.setattr(parser_adapter_readiness, "_package_info", _docling_installed)

    runtime = parser_adapter_runtime_settings(settings)
    scorecard = build_parser_adapter_scorecard(
        runtime,
        metrics={
            "parser_routing_accuracy": 1.0,
            "parser_fallback_rate": 0.0,
        },
        metrics_source="file_processing_staging",
    )

    assert scorecard.recommended_backend == "local"
    docling = _entry(scorecard, "docling")
    assert "downstream_metrics_applied" in docling.reason_codes
    assert "core_downstream_metrics_missing" in docling.reason_codes
    assert "adapter_metric_evidence_incomplete" in docling.warning_codes


def test_scorecard_requires_adapter_contract_coverage_for_external_adapter(
    monkeypatch: MonkeyPatch,
) -> None:
    """構造契約の総合証拠がない外部 adapter は推奨しない。"""
    settings = Settings(
        rag_parser_adapter_backend="docling",
        rag_parser_docling_enabled=True,
    )
    monkeypatch.setattr(parser_adapter_readiness, "_package_info", _docling_installed)

    runtime = parser_adapter_runtime_settings(settings)
    scorecard = build_parser_adapter_scorecard(
        runtime,
        metrics={
            "retrieval_recall": 1.0,
            "table_qa_accuracy": 1.0,
            "page_hit_accuracy": 1.0,
            "element_lineage_coverage": 1.0,
            "source_kind_coverage": 1.0,
            "backend_source_kind_coverage": 1.0,
            "parser_fallback_rate": 0.0,
        },
        metrics_source="file_processing_staging",
    )

    assert scorecard.recommended_backend == "local"
    docling = _entry(scorecard, "docling")
    assert "core_downstream_metrics_missing" in docling.reason_codes
    assert "adapter_metric_evidence_incomplete" in docling.warning_codes


def test_scorecard_applies_metrics_to_selected_active_adapter(
    monkeypatch: MonkeyPatch,
) -> None:
    """明示選択された active adapter へ staging 指標を帰属させる。"""
    settings = Settings(
        rag_parser_adapter_backend="unstructured",
        rag_parser_docling_enabled=False,
        rag_parser_unstructured_enabled=True,
    )

    def package_info(
        import_name: str,
        _distribution_names: Sequence[str],
    ) -> tuple[bool, str | None, str | None]:
        if import_name == "unstructured":
            return True, "0.27.8", "unstructured"
        return False, None, None

    monkeypatch.setattr(parser_adapter_readiness, "_package_info", package_info)

    runtime = parser_adapter_runtime_settings(settings)
    scorecard = build_parser_adapter_scorecard(
        runtime,
        metrics={
            "retrieval_recall": 1.0,
            "table_qa_accuracy": 1.0,
            "page_hit_accuracy": 1.0,
            "element_lineage_coverage": 1.0,
            "parser_routing_accuracy": 1.0,
            "source_kind_coverage": 1.0,
            "backend_source_kind_coverage": 1.0,
            "adapter_contract_coverage": 1.0,
            "parser_fallback_rate": 0.0,
        },
        metrics_source="file_processing_staging",
    )

    assert scorecard.metrics_applied_to == "unstructured"
    assert scorecard.recommended_backend == "unstructured"
    assert _entry(scorecard, "unstructured").signals == {
        "element_lineage_coverage": 1.0,
        "page_hit_accuracy": 1.0,
        "backend_source_kind_coverage": 1.0,
        "adapter_contract_coverage": 1.0,
        "parser_fallback_rate": 1.0,
        "parser_routing_accuracy": 1.0,
        "retrieval_recall": 1.0,
        "source_kind_coverage": 1.0,
        "table_qa_accuracy": 1.0,
    }
    assert _entry(scorecard, "docling").warning_codes == ()


def test_source_routes_follow_selected_adapter_support(
    monkeypatch: MonkeyPatch,
) -> None:
    """明示選択 backend は対応 source kind だけで adapter を試行する。"""
    settings = Settings(
        rag_parser_adapter_backend="unstructured",
        rag_parser_docling_enabled=True,
        rag_parser_unstructured_enabled=True,
    )

    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (True, "1.0.0", import_name),
    )

    runtime = parser_adapter_runtime_settings(settings)
    routes = build_parser_adapter_source_routes(
        runtime,
        source_kinds=["pdf", "image", "office", "html", "email", "markdown"],
    )
    by_kind = {route.source_kind: route for route in routes}

    assert by_kind["pdf"].candidate_order == ("docling", "unstructured", "mineru")
    assert by_kind["pdf"].attempted_order == ("unstructured",)
    assert by_kind["pdf"].selected_backend == "unstructured"
    assert by_kind["image"].candidate_order == ("unstructured", "docling", "dots_ocr", "mineru")
    assert by_kind["image"].attempted_order == ("unstructured",)
    assert by_kind["image"].selected_backend == "unstructured"
    # Docling は PDF と画像だけを解析する(capabilities.ADAPTER_CAPABILITIES)ため、office / html の
    # 候補に出さない(#366)。以前は候補に出し、取込では source_unsupported で必ず fallback していた。
    assert by_kind["office"].candidate_order == ("unstructured", "mineru")
    assert by_kind["office"].attempted_order == ("unstructured",)
    assert by_kind["office"].selected_backend == "unstructured"
    assert by_kind["html"].candidate_order == ("unstructured",)
    assert by_kind["html"].attempted_order == ("unstructured",)
    assert by_kind["html"].selected_backend == "unstructured"
    assert by_kind["email"].candidate_order == ("unstructured",)
    assert by_kind["email"].attempted_order == ("unstructured",)
    assert by_kind["email"].selected_backend == "unstructured"
    assert by_kind["text"].candidate_order == ()
    assert by_kind["text"].attempted_order == ()
    assert by_kind["text"].selected_backend == "local"
    assert "local_parser_preferred_for_source" in by_kind["text"].reason_codes


@pytest.mark.usefixtures("mineru_selectable")
def test_source_routes_explain_explicit_adapter_source_mismatch(
    monkeypatch: MonkeyPatch,
) -> None:
    """明示 adapter が source を扱えない場合も routing evidence に警告を出す。"""
    settings = Settings(
        rag_parser_adapter_backend="mineru",
        rag_parser_mineru_enabled=True,
        rag_parser_mineru_api_host="http://mineru.example.com",
    )
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )

    runtime = parser_adapter_runtime_settings(settings)
    route = build_parser_adapter_source_routes(runtime, source_kinds=["html"])[0]

    assert route.source_kind == "html"
    # html の候補は、html を宣言する Unstructured だけ(Docling は PDF と画像だけ。#366)。
    assert route.candidate_order == ("unstructured",)
    assert route.attempted_order == ()
    assert route.selected_backend == "local"
    assert "selected_adapter_unsupported_for_source" in route.reason_codes
    assert "mineru_adapter_source_unsupported" in route.warning_codes


def _docling_installed(
    import_name: str,
    _distribution_names: Sequence[str],
) -> tuple[bool, str | None, str | None]:
    if import_name == "docling":
        return True, "1.2.3", "docling"
    return False, None, None


def _entry(
    scorecard: ParserAdapterScorecard,
    backend: ParserAdapterScoreBackend,
) -> ParserAdapterScorecardEntry:
    entries = scorecard.entries
    return next(entry for entry in entries if entry.backend == backend)
