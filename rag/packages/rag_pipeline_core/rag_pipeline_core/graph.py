"""文書構造索引の構築プロファイルの決定論解決(backend の中で使う)。

profile(off/entities)→ 構築フラグを決定論で解決する。entities は文書と章・節の見出しの
つながり(entity + relationship)を作る。claims / community summary まで作る full は、読む経路が
無かったため #621 で削除した。Settings 非依存(素の値で受け渡す)。
"""

from __future__ import annotations

from dataclasses import dataclass

GRAPH_PROFILES: tuple[str, ...] = ("off", "entities")
DEFAULT_GRAPH_PROFILE = "off"


@dataclass(frozen=True)
class GraphResolved:
    """解決済みの文書構造索引の構築フラグ。"""

    profile: str
    build_entities: bool
    build_relationships: bool


def normalize_graph_profile(value: object) -> str:
    normalized = str(value).casefold()
    return normalized if normalized in GRAPH_PROFILES else DEFAULT_GRAPH_PROFILE


def resolve_graph_profile(profile: object) -> GraphResolved:
    """profile から文書構造索引の構築フラグを解決する。"""
    name = normalize_graph_profile(profile)
    build_entities = name == "entities"
    return GraphResolved(
        profile=name,
        build_entities=build_entities,
        build_relationships=build_entities,
    )
