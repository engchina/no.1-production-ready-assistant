"""文書構造索引の構築アダプター(構築する / しない)。

決定論ロジックは共有パッケージ ``rag_pipeline_core.graph`` を単一ソースとして使い、backend の中で
解決する(サービスへは委譲しない。#3218)。

- ``off``(既定): 構築しない。
- ``entities``: 文書と章・節の見出しのつながり(entity + relationship)を構築する。LLM は使わない
  (``app/rag/graph_index.py``)。構築した関係情報はナレッジベースの「文書構造グラフ」で見られ、
  ``rag_graph_expansion_enabled``(既定 OFF)を ON にすると回答の検索の候補を広げるのにも使う
  (``app/rag/graph_expansion.py``。#2055)。

claims / community summary まで作る ``full`` と legacy の ``RAG_GRAPH_ENABLED`` は、読む経路が
無かったため #621 で削除した。外部グラフ DB は導入しない。
"""

from __future__ import annotations

from dataclasses import dataclass

from rag_pipeline_core.graph import resolve_graph_profile

from app.config import GraphProfile, Settings

GraphProfileName = GraphProfile
DEFAULT_GRAPH_PROFILE: GraphProfileName = "off"
GRAPH_PROFILE_ORDER: tuple[GraphProfileName, ...] = ("off", "entities")


@dataclass(frozen=True)
class GraphAdapterParams:
    """文書構造索引の構築へ渡す解決済みパラメータ。"""

    profile: GraphProfileName
    enabled: bool


@dataclass(frozen=True)
class GraphProfileStatus:
    """1 プロファイルの選択状態。"""

    name: GraphProfileName
    selected: bool


@dataclass(frozen=True)
class GraphAdapterRuntimeSettings:
    """文書構造索引の構築の非機密 runtime snapshot。"""

    profile: GraphProfileName
    enabled: bool
    profiles: tuple[GraphProfileStatus, ...]


def normalize_graph_profile(value: object) -> GraphProfileName:
    """未知のプロファイル名は既定 off へ寄せる。"""
    normalized = str(value).casefold()
    for name in GRAPH_PROFILE_ORDER:
        if normalized == name:
            return name
    return DEFAULT_GRAPH_PROFILE


def resolve_graph_adapter(settings: Settings) -> GraphAdapterParams:
    """Settings から文書構造索引の構築の解決済みパラメータを作る。"""
    profile = normalize_graph_profile(getattr(settings, "rag_graph_profile", DEFAULT_GRAPH_PROFILE))
    resolved = resolve_graph_profile(profile)
    return GraphAdapterParams(
        profile=normalize_graph_profile(resolved.profile),
        enabled=resolved.build_entities,
    )


def graph_adapter_runtime_settings(settings: Settings) -> GraphAdapterRuntimeSettings:
    """Settings から文書構造索引の構築の snapshot を作る。"""
    params = resolve_graph_adapter(settings)
    return GraphAdapterRuntimeSettings(
        profile=params.profile,
        enabled=params.enabled,
        profiles=tuple(
            GraphProfileStatus(name=name, selected=name == params.profile)
            for name in GRAPH_PROFILE_ORDER
        ),
    )
