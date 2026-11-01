"""評価の実行条件の manifest（#59）。合成の fixture だけを使い、Oracle・モデルの API に接続しない。

- 条件（評価セット・基準日・設定・プロファイル・chunk_set とその内容の版）を残し、差を検出する。
- 実行中の更新（active の切替・抽出の更新・公開の版の更新・権限の剥奪・削除）は比較不能にする。
- 評価の間は「今日」を開始時の日に固定する（日付を跨いでも同じ日。通常の質問は変えない）。
- 秘密・質問の原文を含まない。#59 より前の結果も読める。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pytest import MonkeyPatch

from app import config as config_module
from app.config import fixed_today, get_settings, settings_today
from app.rag.evaluation_manifest import (
    build_run_manifest,
    comparison_differences,
    with_run_check,
)
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.schemas.evaluation import EvaluationMetrics, EvaluationRunRequest

_SECRET_QUERY = "社外秘キーワード ABC-123 の承認条件"


def _rows() -> list[dict[str, object]]:
    return [
        {
            "document_id": "doc-1",
            "chunk_set_id": "cs-1",
            "chunk_count": 10,
            "vector_count": 10,
            "chunk_set_updated_at": "2026-10-01T00:00:00+00:00",
            "source_sha256": "a" * 64,
            "extraction_updated_at": "2026-10-01T00:00:00+00:00",
        },
        {
            "document_id": "doc-2",
            "chunk_set_id": "cs-2",
            "chunk_count": 5,
            "vector_count": 5,
            "chunk_set_updated_at": "2026-10-01T00:00:00+00:00",
            "source_sha256": "b" * 64,
            "extraction_updated_at": "2026-10-01T00:00:00+00:00",
        },
    ]


class FakeOracle:
    """manifest が読む Oracle の操作の偽物（行・プロファイルを書き換えて実行中の更新を作る）。"""

    def __init__(self) -> None:
        self.rows = _rows()
        self.profile = SimpleNamespace(
            id="profile-1",
            updated_at=datetime(2026, 10, 1, tzinfo=UTC),
            config=SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"]),
        )
        self.knowledge: dict[str, object] = {"rules": ["r1"]}
        self.prompts = {"generation.system": "回答のプロンプト"}
        self.corpus_scopes: list[list[str]] = []

    async def evaluation_corpus_rows(
        self, knowledge_base_ids: Sequence[str]
    ) -> list[dict[str, object]]:
        self.corpus_scopes.append(list(knowledge_base_ids))
        return copy.deepcopy(self.rows)

    async def get_search_answer_profile(self, search_answer_profile_id: str) -> object | None:
        return self.profile if search_answer_profile_id == self.profile.id else None

    async def get_search_answer_profile_knowledge(
        self, search_answer_profile_id: str, kind: str
    ) -> dict[str, object] | None:
        del search_answer_profile_id
        return self.knowledge if kind != "domain_keywords" else None

    async def answer_prompt_overrides(self) -> dict[str, str]:
        return dict(self.prompts)

    async def save_evaluation_artifact(self, artifact: dict[str, Any]) -> str:
        del artifact
        return "eval-1"


def _request(**update: object) -> EvaluationRunRequest:
    payload: dict[str, object] = {
        "cases": [{"id": "c1", "query": _SECRET_QUERY, "relevant_document_ids": ["doc-1"]}],
        "top_k": 3,
        "search_answer_profile_id": "profile-1",
        **update,
    }
    return EvaluationRunRequest.model_validate(payload)


async def _manifest(
    oracle: FakeOracle, request: EvaluationRunRequest | None = None
) -> dict[str, Any]:
    return await build_run_manifest(
        request or _request(),
        settings=get_settings(),
        oracle=oracle,
        as_of=date(2026, 10, 31),
        as_of_source="run_start",
    )


async def test_manifest_identifies_conditions_without_secrets(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "enterprise_ai_api_key", "sk-test-secret-value")
    oracle = FakeOracle()

    manifest = await _manifest(oracle)

    assert manifest["as_of"] == "2026-10-31"
    assert manifest["evaluation_set"]["case_count"] == 1
    assert manifest["corpus"]["chunk_set_ids"] == ["cs-1", "cs-2"]
    assert manifest["corpus"]["chunk_count"] == 15
    # プロファイルの参照 KB で、検索に出る chunk_set を読む。
    assert oracle.corpus_scopes == [["kb-1"]]
    assert manifest["profiles"]["profile-1"]["updated_at"] == "2026-10-01T00:00:00+00:00"
    assert manifest["models"]["snapshot"] == "unverified"
    assert manifest["settings"]["prompt_version"].startswith("prompt-")
    assert manifest["external_calls"] == {"mode": "live"}
    text = json.dumps(manifest, ensure_ascii=False)
    assert "sk-test-secret-value" not in text
    assert "社外秘キーワード" not in text
    assert "回答のプロンプト" not in text
    # 同じ条件なら差は無い。
    assert comparison_differences(manifest, await _manifest(oracle)) == []


async def test_manifest_detects_condition_differences() -> None:
    oracle = FakeOracle()
    base = await _manifest(oracle)

    changed_set = await _manifest(
        oracle,
        _request(cases=[{"id": "c1", "query": "別の質問", "relevant_document_ids": ["doc-1"]}]),
    )
    assert comparison_differences(base, changed_set) == ["evaluation_set.sha256"]

    other_day = await build_run_manifest(
        _request(),
        settings=get_settings(),
        oracle=oracle,
        as_of=date(2026, 11, 1),
        as_of_source="run_start",
    )
    assert comparison_differences(base, other_day) == ["as_of"]

    oracle.prompts = {"generation.system": "直したプロンプト"}
    assert comparison_differences(base, await _manifest(oracle)) == ["settings.prompt_version"]

    # コードの版と外部の呼び出しの扱いは条件の差に入れない（同じ条件でコード・再生を比べる）。
    other_code = {**base, "code_version": "other", "external_calls": {"mode": "replay"}}
    assert comparison_differences(base, other_code) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        # active の切替（配信中の chunk_set が替わる）。
        (lambda o: o.rows[0].update(chunk_set_id="cs-1b"), "corpus.chunk_set_ids"),
        # 抽出の内容の更新（同じ chunk_set の id でも内容の版が変わる）。
        (
            lambda o: o.rows[1].update(extraction_updated_at="2026-10-31T09:00:00+00:00"),
            "corpus.sha256",
        ),
        # 文書の削除・権限の剥奪（利用者の範囲から行が消える）。
        (lambda o: o.rows.pop(), "corpus.document_count"),
        # 公開の版の更新（プロファイルの設定・更新時刻）。
        (
            lambda o: setattr(o.profile, "updated_at", datetime(2026, 10, 31, tzinfo=UTC)),
            "profiles.profile-1.updated_at",
        ),
        # 用語・回答ルールの更新。
        (
            lambda o: setattr(o, "knowledge", {"rules": ["r2"]}),
            "profiles.profile-1.knowledge_sha256",
        ),
    ],
)
async def test_run_check_marks_updates_during_run_as_not_comparable(
    change: Any, expected: str
) -> None:
    oracle = FakeOracle()
    before = await _manifest(oracle)
    change(oracle)

    checked = with_run_check(before, await _manifest(oracle))

    assert checked["comparable"] is False
    assert expected in checked["changed_during_run"]


async def test_manifest_marks_unreadable_conditions_unverified() -> None:
    class BrokenOracle(FakeOracle):
        async def evaluation_corpus_rows(
            self, knowledge_base_ids: Sequence[str]
        ) -> list[dict[str, object]]:
            raise RuntimeError("DB に接続できません")

    manifest = await _manifest(BrokenOracle())

    assert manifest["corpus"]["status"] == "unverified"


def test_results_before_manifest_remain_readable() -> None:
    metrics = EvaluationMetrics.model_validate({"case_count": 1, "passed": True})

    assert metrics.run_manifest is None


def test_fixed_today_overrides_only_inside_context(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "local_today", lambda settings: date(2026, 11, 2))
    with fixed_today(date(2026, 11, 1)):
        assert settings_today() == date(2026, 11, 1)
    assert settings_today() == date(2026, 11, 2)


async def test_execute_run_fixes_today_and_records_run_check(monkeypatch: MonkeyPatch) -> None:
    """評価の途中で日付を跨いでも同じ日で流し、途中の active の切替を比較不能として残す。"""
    from app.api.routes import evaluation as evaluation_route

    oracle = FakeOracle()
    clock = iter([date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 1)])
    monkeypatch.setattr(config_module, "local_today", lambda settings: next(clock))
    observed: list[date] = []

    class SwitchingRunner:
        async def run(self, **kwargs: object) -> EvaluationMetrics:
            del kwargs
            observed.append(settings_today())
            oracle.rows[0]["chunk_set_id"] = "cs-1b"
            observed.append(settings_today())
            return EvaluationMetrics(case_count=1, passed=True)

    monkeypatch.setattr(evaluation_route, "EvaluationRunner", SwitchingRunner)
    monkeypatch.setattr(evaluation_route, "OracleClient", lambda: oracle)

    metrics, _ = await evaluation_route._execute_run(_request(), time_budget_seconds=60)

    assert observed == [date(2026, 10, 31), date(2026, 10, 31)]
    manifest = metrics.run_manifest
    assert manifest is not None
    assert manifest["as_of"] == "2026-10-31"
    assert manifest["as_of_source"] == "run_start"
    assert manifest["comparable"] is False
    assert "corpus.chunk_set_ids" in manifest["changed_during_run"]


async def test_execute_run_keeps_explicit_as_of(monkeypatch: MonkeyPatch) -> None:
    from app.api.routes import evaluation as evaluation_route

    oracle = FakeOracle()

    class Runner:
        async def run(self, **kwargs: object) -> EvaluationMetrics:
            del kwargs
            return EvaluationMetrics(case_count=1, passed=True)

    monkeypatch.setattr(evaluation_route, "EvaluationRunner", Runner)
    monkeypatch.setattr(evaluation_route, "OracleClient", lambda: oracle)

    metrics, _ = await evaluation_route._execute_run(
        _request(filters={"as_of": "2026-04-01"}), time_budget_seconds=60
    )

    assert metrics.run_manifest is not None
    assert metrics.run_manifest["as_of"] == "2026-04-01"
    assert metrics.run_manifest["as_of_source"] == "request"
    assert metrics.run_manifest["comparable"] is True
