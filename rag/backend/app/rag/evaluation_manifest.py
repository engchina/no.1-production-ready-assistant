"""評価の実行条件の manifest（#59）。

評価の開始時に、結果に効く条件（基準日・評価セット・解決した設定とプロンプトの版・モデル・検索・
回答プロファイルの版・検索に出る chunk_set と内容の版・コードの版）を 1 つの JSON にして、評価の
結果（``EvaluationMetrics.run_manifest``）に残す。終わった後にもう一度作って比べ、実行中に変わった条件を
``changed_during_run`` に出す（変わったら ``comparable`` を false にする）。2 つの結果の条件の差は
``comparison_differences`` で求める（評価の CLI の ``diff``）。

- 既存の追跡を使う: 設定の fingerprint（``rag_config_fingerprint``）・プロンプトの版
  （``answer_prompt_version``）・プロファイルの版（``search_answer_profile_revision``）。
- 秘密（API キー・接続の情報）・質問や根拠の原文は入れない。条件の値（filters）は hash だけを残す。
- モデルの snapshot（提供側の重みの版）は API から取れないため ``unverified`` と書く。
- 読めなかった条件は ``unverified`` にし、評価は止めない（条件の差として現れる）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess  # nosec B404 - 固定の git の引数だけを渡す。
from collections.abc import Mapping, Sequence
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel
from rag_engine.resources.external_calls import external_call_mode

from app.config import (
    Settings,
    enterprise_ai_default_model_id,
    enterprise_ai_judge_model_id,
)
from app.rag.answer_provenance import answer_prompt_version, search_answer_profile_revision
from app.rag.diagnostics import rag_config_fingerprint
from app.rag.evaluation import evaluation_settings
from app.rag.search_answer_profile_knowledge import RUNTIME_KNOWLEDGE_KIND, load_domain_keywords
from app.schemas.evaluation import EvaluationRunRequest

MANIFEST_VERSION = 1
UNVERIFIED = "unverified"
# 2 つの結果の条件の比較に入れない項目（同じ条件で別の実行・別のコードの版を比べるため）。
NON_CONDITION_KEYS = frozenset(
    {"code_version", "external_calls", "changed_during_run", "comparable", "manifest_version"}
)
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


class ManifestOracle(Protocol):
    """manifest が読む Oracle の操作（テストでは偽物を渡す）。"""

    async def evaluation_corpus_rows(
        self, knowledge_base_ids: Sequence[str]
    ) -> list[dict[str, object]]: ...

    async def get_search_answer_profile(self, search_answer_profile_id: str) -> Any: ...

    async def get_search_answer_profile_knowledge(
        self, search_answer_profile_id: str, kind: str
    ) -> dict[str, object] | None: ...

    async def answer_prompt_overrides(self) -> dict[str, str]: ...


async def build_run_manifest(
    request: EvaluationRunRequest,
    *,
    settings: Settings,
    oracle: ManifestOracle,
    as_of: date,
    as_of_source: str,
) -> dict[str, Any]:
    """評価の実行条件の manifest を作る（``as_of_source`` は request / run_start / recording）。"""
    profile_ids = [
        profile_id
        for profile_id in (
            request.search_answer_profile_id,
            request.compare_search_answer_profile_id,
        )
        if profile_id
    ]
    profiles = {profile_id: await _profile(oracle, profile_id) for profile_id in profile_ids}
    knowledge_base_ids = set(request.knowledge_base_ids)
    for profile in profiles.values():
        knowledge_base_ids.update(profile.get("knowledge_base_ids") or [])
    answer_model = enterprise_ai_default_model_id(settings)
    return {
        "manifest_version": MANIFEST_VERSION,
        "as_of": as_of.isoformat(),
        "as_of_source": as_of_source,
        "evaluation_set": {
            "sha256": _sha256([case.model_dump(mode="json") for case in request.cases]),
            "case_count": len(request.cases),
        },
        "request": {
            "top_k": request.top_k,
            "filter_keys": sorted(request.filters),
            "filters_sha256": _sha256(request.filters),
            "knowledge_base_ids": sorted(request.knowledge_base_ids),
            "search_answer_profile_id": request.search_answer_profile_id,
            "compare_search_answer_profile_id": request.compare_search_answer_profile_id,
            "rag_overrides": (
                request.rag_overrides.model_dump(mode="json", exclude_none=True)
                if request.rag_overrides is not None
                else {}
            ),
        },
        "settings": {
            # 評価の上書き（rag_overrides）を当てた設定（ケースの回答と同じ）。
            "rag_config_fingerprint": rag_config_fingerprint(
                evaluation_settings(settings, request.rag_overrides)
            ),
            "prompt_version": await _prompt_version(oracle),
        },
        "models": {
            "answer": answer_model or UNVERIFIED,
            "judge": enterprise_ai_judge_model_id(settings, answer_model) or UNVERIFIED,
            "embedding": settings.genai_embedding_model or UNVERIFIED,
            "embedding_dim": settings.genai_embedding_dim,
            "rerank": settings.genai_rerank_model or UNVERIFIED,
            "snapshot": UNVERIFIED,
        },
        "profiles": profiles,
        "corpus": await _corpus(oracle, sorted(knowledge_base_ids)),
        "code_version": code_version(),
        # 外部の AI の応答を実の API で得たか、記録・再生したか（再生は live の評価と区別する）。
        "external_calls": {"mode": external_call_mode()},
    }


def comparison_differences(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    """2 つの manifest の条件の差（違う項目の dotted path。比較に入れない項目は除く）。"""
    flat_left = flatten_manifest(left)
    flat_right = flatten_manifest(right)
    return sorted(
        path
        for path in flat_left.keys() | flat_right.keys()
        if path.split(".", 1)[0] not in NON_CONDITION_KEYS
        and flat_left.get(path) != flat_right.get(path)
    )


def with_run_check(before: dict[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """開始時の manifest に、実行中に変わった条件と比較の可否を足す。"""
    changed = comparison_differences(before, after)
    return {**before, "changed_during_run": changed, "comparable": not changed}


@lru_cache(maxsize=1)
def code_version() -> str:
    """backend のコードの版（git の commit。手元の変更は ``+dirty``。取れなければ unverified）。"""
    try:
        commit = subprocess.run(  # noqa: S603 - 固定の引数  # nosec B603 B607
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=_BACKEND_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603 - 固定の引数  # nosec B603 B607
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            cwd=_BACKEND_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return UNVERIFIED
    return f"{commit}+dirty" if dirty else commit or UNVERIFIED


async def _profile(oracle: ManifestOracle, profile_id: str) -> dict[str, Any]:
    """検索・回答プロファイルの版（設定・用語と回答ルール・ドメインキーワードの内容の hash）。"""
    try:
        view = await oracle.get_search_answer_profile(profile_id)
        if view is None:
            return {"id": profile_id, "status": "not_found"}
        knowledge = await oracle.get_search_answer_profile_knowledge(
            str(view.id), RUNTIME_KNOWLEDGE_KIND
        )
        keywords = await load_domain_keywords(oracle, str(view.id))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - 読めない条件は unverified にして評価は続ける。
        _log_unverified("profile", exc)
        return {"id": profile_id, "status": UNVERIFIED}
    return {
        **search_answer_profile_revision(view),
        "knowledge_sha256": _sha256(knowledge or {}),
        "domain_keywords_sha256": _sha256(keywords or []),
        "knowledge_base_ids": sorted(view.config.normalized_knowledge_base_ids()),
    }


async def _prompt_version(oracle: ManifestOracle) -> str:
    try:
        return answer_prompt_version(await oracle.answer_prompt_overrides())
    except Exception as exc:  # noqa: BLE001
        _log_unverified("prompt_version", exc)
        return UNVERIFIED


async def _corpus(oracle: ManifestOracle, knowledge_base_ids: list[str]) -> dict[str, Any]:
    """検索に出る chunk_set の集合と、その内容の版の hash（件数と chunk_set の id も残す）。"""
    try:
        rows = await oracle.evaluation_corpus_rows(knowledge_base_ids)
    except Exception as exc:  # noqa: BLE001
        _log_unverified("corpus", exc)
        return {"status": UNVERIFIED, "knowledge_base_ids": knowledge_base_ids}
    return {
        "knowledge_base_ids": knowledge_base_ids,
        "sha256": _sha256(rows),
        "document_count": len({str(row.get("document_id")) for row in rows}),
        "chunk_set_ids": sorted(str(row.get("chunk_set_id")) for row in rows),
        "chunk_count": sum(int(str(row.get("chunk_count") or 0)) for row in rows),
        "vector_count": sum(int(str(row.get("vector_count") or 0)) for row in rows),
    }


def _sha256(payload: object) -> str:
    def default(value: object) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        return str(value)

    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=default
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def flatten_manifest(value: Mapping[str, Any], prefix: str = "") -> dict[str, object]:
    """入れ子の manifest を dotted path → 値にする（空の object は値として残す）。"""
    flat: dict[str, object] = {}
    for key, item in value.items():
        path = f"{prefix}{key}"
        if isinstance(item, Mapping) and item:
            flat.update(flatten_manifest(item, f"{path}."))
        else:
            flat[path] = item
    return flat


def _log_unverified(part: str, exc: BaseException) -> None:
    logger.info(
        "evaluation_manifest_unverified",
        extra={"part": part, "error_type": type(exc).__name__},
    )


__all__ = [
    "NON_CONDITION_KEYS",
    "UNVERIFIED",
    "build_run_manifest",
    "code_version",
    "comparison_differences",
    "flatten_manifest",
    "with_run_check",
]
