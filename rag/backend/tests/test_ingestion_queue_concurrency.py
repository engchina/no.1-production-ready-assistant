"""取込の worker の同時実行数の auto の決め方のテスト(#2027)。

既定の auto は Compute の vCPU の数から決め、3 製品の backend・Nginx・解析サービスに CPU を残す。
全体は max(2, vCPU − 2)(上限 16)、重い段(ファイル準備・抽出)は max(1, (vCPU − 2) // 3)。
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app import config
from app.config import Settings, detect_vcpu_count, resolve_ingestion_queue_concurrency


@pytest.mark.parametrize(
    ("vcpu", "total", "heavy"),
    [(1, 2, 1), (2, 2, 1), (4, 2, 1), (8, 6, 2), (16, 14, 4), (32, 16, 10)],
    ids=["1vcpu", "2vcpu", "4vcpu", "8vcpu", "16vcpu", "32vcpu"],
)
def test_auto_concurrency_from_vcpu(vcpu: int, total: int, heavy: int) -> None:
    plan = Settings().ingestion_queue_concurrency(vcpu=vcpu)

    assert (plan.total, plan.heavy_phase) == (total, heavy)
    assert plan.log_fields() == {
        "worker_concurrency": total,
        "heavy_phase_concurrency": heavy,
        "vcpu": vcpu,
        "worker_concurrency_auto": True,
        "heavy_phase_concurrency_auto": True,
    }


def test_auto_uses_detected_vcpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "detect_vcpu_count", lambda: 8)

    plan = Settings().ingestion_queue_concurrency()

    assert (plan.total, plan.heavy_phase, plan.vcpu) == (6, 2, 8)


def test_explicit_values_override_auto() -> None:
    settings = Settings(
        ingestion_queue_worker_concurrency="3", ingestion_queue_heavy_phase_concurrency=2
    )

    plan = settings.ingestion_queue_concurrency(vcpu=16)

    assert (plan.total, plan.heavy_phase) == (3, 2)
    assert (plan.total_auto, plan.heavy_phase_auto) == (False, False)


def test_auto_heavy_phase_is_capped_by_explicit_total() -> None:
    """全体の枠を数で下げたら、auto の重い段はその数で頭打ちにする。"""
    plan = resolve_ingestion_queue_concurrency(1, "auto", vcpu=16)

    assert (plan.total, plan.heavy_phase) == (1, 1)


@pytest.mark.parametrize(
    "value", ["", "AUTO", " auto ", None], ids=["empty", "upper", "space", "none"]
)
def test_auto_spellings(value: object) -> None:
    settings = Settings(ingestion_queue_worker_concurrency=value)

    assert settings.ingestion_queue_worker_concurrency == "auto"


@pytest.mark.parametrize("value", [0, 17, "many"], ids=["zero", "over-max", "text"])
def test_rejects_invalid_concurrency(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(ingestion_queue_heavy_phase_concurrency=value)


def test_rejects_explicit_heavy_phase_above_total() -> None:
    with pytest.raises(ValidationError, match="RAG_INGESTION_QUEUE_HEAVY_PHASE_CONCURRENCY"):
        Settings(ingestion_queue_worker_concurrency=2, ingestion_queue_heavy_phase_concurrency=3)


def test_detect_vcpu_count_prefers_affinity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2}, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 64)

    assert detect_vcpu_count() == 3


def test_detect_vcpu_count_falls_back_to_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)

    assert detect_vcpu_count() == 1
