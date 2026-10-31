"""Vector Index アダプター(索引/検索精度の手動選択プリセット)。

決定論ロジック・spec は共有パッケージ ``rag_pipeline_core.vector_index`` を単一ソースとして使い、
backend の中で解決する(サービスへは委譲しない。#3218)。外部ベクトル DB は導入しない。

解決結果は (プロファイル, 設定の target accuracy) ごとにプロセス内でメモ化する(#828)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rag_pipeline_core.vector_index import (
    DEFAULT_VECTOR_INDEX_PROFILE as _CORE_DEFAULT_PROFILE,
)
from rag_pipeline_core.vector_index import (
    DISTANCE,
    VECTOR_INDEX_PROFILES,
    VECTOR_INDEX_SPECS,
    profile_target_accuracy,
    resolve_vector_index,
)
from rag_pipeline_core.vector_index import (
    normalize_vector_index_profile as _core_normalize,
)

from app.config import Settings, VectorIndexProfile

VectorIndexProfileName = VectorIndexProfile
DEFAULT_VECTOR_INDEX_PROFILE: VectorIndexProfileName = _CORE_DEFAULT_PROFILE  # type: ignore[assignment]
VECTOR_INDEX_PROFILE_ORDER: tuple[VectorIndexProfileName, ...] = VECTOR_INDEX_PROFILES  # type: ignore[assignment]

# 実際の索引と推奨ビルドの比較結果(#562)。unknown = 実際の値を確認できない。
IndexBuildStatus = Literal["match", "reprovision", "unknown"]


def index_build_status(
    neighbors: int, efconstruction: int, actual: tuple[int, int] | None
) -> IndexBuildStatus:
    """推奨ビルドを実際の索引の (NEIGHBORS, EFCONSTRUCTION) と比べる。"""
    if actual is None:
        return "unknown"
    return "match" if actual == (neighbors, efconstruction) else "reprovision"


@dataclass(frozen=True)
class VectorIndexParams:
    """ベクトル検索/索引へ渡す解決済みパラメータ。"""

    profile: VectorIndexProfileName
    target_accuracy: int
    neighbors: int
    efconstruction: int
    distance: str
    requires_reprovision: bool


@dataclass(frozen=True)
class VectorIndexProfileStatus:
    """1 索引プロファイルの選択状態と accuracy/build 推奨値。"""

    name: VectorIndexProfileName
    origin: str
    recommended_for: tuple[str, ...]
    selected: bool
    target_accuracy: int
    neighbors: int
    efconstruction: int
    distance: str
    index_status: IndexBuildStatus


@dataclass(frozen=True)
class VectorIndexAdapterRuntimeSettings:
    """Vector Index アダプターの非機密 runtime snapshot。"""

    profile: VectorIndexProfileName
    target_accuracy: int
    neighbors: int
    efconstruction: int
    distance: str
    requires_reprovision: bool
    index_status: IndexBuildStatus
    actual_neighbors: int | None
    actual_efconstruction: int | None
    profiles: tuple[VectorIndexProfileStatus, ...]


def normalize_vector_index_profile(value: object) -> VectorIndexProfileName:
    """未知のプロファイル名は既定(高精度。#272)へ寄せる。"""
    return _core_normalize(value)  # type: ignore[return-value]


# 設定 RAG_ORACLE_VECTOR_TARGET_ACCURACY の既定値(balanced の target accuracy)。
_DEFAULT_SETTINGS_TARGET_ACCURACY = int(
    Settings.model_fields["oracle_vector_target_accuracy"].default
)


def _settings_target_accuracy(settings: Settings) -> int:
    return int(
        getattr(settings, "oracle_vector_target_accuracy", _DEFAULT_SETTINGS_TARGET_ACCURACY)
    )


def default_vector_index_build() -> VectorIndexParams:
    """新しい環境の主検索索引のビルド(既定の検索精度の推奨ビルド。#1990)。

    schema の作成の DDL(``oracle_vector_schema_sql``)と契約(``VECTOR_INDEX_CONTRACT``)は
    この値から作る(同じ値を別の場所に書かない)。設定・サービスには依存しない決定論の値で、
    target accuracy は既定の設定値で解決する。既存の環境の索引は変えない(再作成の SQL を
    DBA が適用する)。
    """
    resolved = resolve_vector_index(DEFAULT_VECTOR_INDEX_PROFILE, _DEFAULT_SETTINGS_TARGET_ACCURACY)
    return VectorIndexParams(
        profile=resolved.profile,  # type: ignore[arg-type]
        target_accuracy=resolved.target_accuracy,
        neighbors=resolved.neighbors,
        efconstruction=resolved.efconstruction,
        distance=resolved.distance,
        requires_reprovision=resolved.requires_reprovision,
    )


# profile→パラメータの解決はプロセス内で不変(決定論)。解決結果を (プロファイル, 設定の target
# accuracy) ごとにメモ化する(guardrail の静的解決と同じ。#828)。
_STATIC_CACHE: dict[tuple[str, int], VectorIndexParams] = {}


def reset_vector_index_static_cache() -> None:
    """解決結果のメモ化をクリアする(テスト isolation 用)。"""
    _STATIC_CACHE.clear()


def resolve_vector_index_adapter(settings: Settings) -> VectorIndexParams:
    """Settings から Vector Index アダプターの解決済みパラメータを作る。

    結果は (プロファイル, 設定の target accuracy) ごとにメモ化する(#828)。
    """
    profile = normalize_vector_index_profile(
        getattr(settings, "rag_vector_index_profile", DEFAULT_VECTOR_INDEX_PROFILE)
    )
    settings_accuracy = _settings_target_accuracy(settings)
    key = (profile, settings_accuracy)
    cached = _STATIC_CACHE.get(key)
    if cached is not None:
        return cached
    resolved = resolve_vector_index(profile, settings_accuracy)
    params = VectorIndexParams(
        profile=resolved.profile,  # type: ignore[arg-type]
        target_accuracy=resolved.target_accuracy,
        neighbors=resolved.neighbors,
        efconstruction=resolved.efconstruction,
        distance=resolved.distance,
        requires_reprovision=resolved.requires_reprovision,
    )
    _STATIC_CACHE[key] = params
    return params


def vector_index_adapter_runtime_settings(
    settings: Settings,
    actual_build: tuple[int, int] | None = None,
) -> VectorIndexAdapterRuntimeSettings:
    """Settings から Vector Index アダプター readiness snapshot を作る。

    ``actual_build`` は実際の索引の (NEIGHBORS, EFCONSTRUCTION)。None は確認できないことを表し、
    再作成の要否は断定しない(#562)。``params.requires_reprovision`` は初期 DDL の値との差で、
    実際の索引を見ていないため使わない。
    """
    params = resolve_vector_index_adapter(settings)
    settings_accuracy = _settings_target_accuracy(settings)
    statuses = tuple(
        VectorIndexProfileStatus(
            name=spec.name,  # type: ignore[arg-type]
            origin=spec.origin,
            recommended_for=spec.recommended_for,
            selected=spec.name == params.profile,
            target_accuracy=profile_target_accuracy(spec, settings_accuracy),
            neighbors=spec.neighbors,
            efconstruction=spec.efconstruction,
            distance=DISTANCE,
            index_status=index_build_status(spec.neighbors, spec.efconstruction, actual_build),
        )
        for spec in (VECTOR_INDEX_SPECS[name] for name in VECTOR_INDEX_PROFILES)
    )
    index_status = index_build_status(params.neighbors, params.efconstruction, actual_build)
    return VectorIndexAdapterRuntimeSettings(
        profile=params.profile,
        target_accuracy=params.target_accuracy,
        neighbors=params.neighbors,
        efconstruction=params.efconstruction,
        distance=params.distance,
        requires_reprovision=index_status == "reprovision",
        index_status=index_status,
        actual_neighbors=actual_build[0] if actual_build else None,
        actual_efconstruction=actual_build[1] if actual_build else None,
        profiles=statuses,
    )
