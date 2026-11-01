"""Navigation tree 構築と node 要約（Knowhere 由来の memory 層）。

Knowhere は「文書階層を tree 状に再構築し、chunk + navigation tree + 各 node の summary を
agent-ready context として保存」する。本モジュールは確定スタックに合わせて再実装し、
- `build_navigation_tree`: `StructuredExtraction.elements` の section_path / parent_id /
  reading order から **決定論的**（LLM 不要）に章節 tree を組む。
- `summarize_navigation_nodes`: 任意で LLM を使い node 単位の summary を付ける
  （feature flag。既定 OFF）。要約器は注入可能でテストは決定論。
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from rag_parser_core.extraction import (
    NAV_SUMMARY_KEY,
    SECTION_SUMMARY_CONTENT_KIND,
    is_derived_content,
)

from app.schemas.extraction import (
    DocumentElement,
    DocumentNavigationNode,
    StructuredExtraction,
)

# node 要約の既定 bound。長文書でも LLM 呼び出し回数と入力長を抑える。
DEFAULT_SUMMARY_MAX_NODES = 24
DEFAULT_SUMMARY_MAX_CHARS = 4000
DEFAULT_SUMMARY_MIN_CHARS = 200


def navigation_section_id(section_path: tuple[str, ...]) -> str:
    """section_path から安定した section_id を作る（同一 path は常に同一 id）。"""
    # section_id の安定生成のみに使う非暗号用途(セキュリティ用途ではない)。
    digest = hashlib.sha1(
        "\x1f".join(section_path).encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return f"nav-{digest[:16]}"


@dataclass
class _NodeAccumulator:
    """tree 構築中の node 集計。"""

    section_path: tuple[str, ...]
    element_ids: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    child_ids: list[str] = field(default_factory=list)


def build_navigation_tree(extraction: StructuredExtraction) -> list[DocumentNavigationNode]:
    """extraction から決定論的に navigation tree（flat list + 親子 id link）を組む。

    各 element の section_path の全 prefix を node 化し、祖先 node が leaf だけの文書でも
    欠落しないようにする。element_ids は完全一致 path の node にのみ付与し、page 範囲は
    subtree 全体を覆うよう全 prefix へ伝播する。

    node は木の前順（親の直後にその子孫。根・兄弟は最初に現れた順）に並べる。平らな一覧
    （``sections_from_navigation``）は並び順と階層で親を決めるので、目次の要素が章の見出しを
    本文より先に持つ文書でも、本文の節が目次の最後の章の子にならない（#3511）。
    """
    accumulators: dict[tuple[str, ...], _NodeAccumulator] = {}
    order: list[tuple[str, ...]] = []
    for element in extraction.elements:
        if is_derived_content(element.content_kind, element.metadata):
            continue
        path = tuple(segment for segment in element.section_path if segment)
        if not path:
            continue
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            if prefix not in accumulators:
                accumulators[prefix] = _NodeAccumulator(section_path=prefix)
                order.append(prefix)
            if element.page_number:
                accumulators[prefix].pages.append(element.page_number)
        if element.element_id:
            accumulators[path].element_ids.append(element.element_id)

    # 親子 link を構築（first-appearance 順を保持）。
    for path in order:
        if len(path) < 2:
            continue
        parent = accumulators[path[:-1]]
        child_id = navigation_section_id(path)
        if child_id not in parent.child_ids:
            parent.child_ids.append(child_id)

    nodes: list[DocumentNavigationNode] = []
    for path in _preorder(order):
        acc = accumulators[path]
        nodes.append(
            DocumentNavigationNode(
                section_id=navigation_section_id(path),
                title=path[-1],
                section_path=list(path),
                depth=len(path),
                parent_section_id=(navigation_section_id(path[:-1]) if len(path) > 1 else None),
                child_section_ids=list(acc.child_ids),
                element_ids=list(acc.element_ids),
                page_start=min(acc.pages) if acc.pages else None,
                page_end=max(acc.pages) if acc.pages else None,
            )
        )
    return nodes


def _preorder(order: Sequence[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """最初に現れた順の path の列を、木の前順（親の直後に子孫）に並べ直す。"""
    children: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    roots: list[tuple[str, ...]] = []
    for path in order:
        if len(path) == 1:
            roots.append(path)
        else:
            children.setdefault(path[:-1], []).append(path)
    ordered: list[tuple[str, ...]] = []
    stack = list(reversed(roots))
    while stack:
        path = stack.pop()
        ordered.append(path)
        stack.extend(reversed(children.get(path, [])))
    return ordered


def _node_source_text(
    node: DocumentNavigationNode,
    element_text_by_id: dict[str, str],
    *,
    max_chars: int,
) -> str:
    """node 直属の element text を連結し、要約入力として返す（上限 max_chars）。"""
    parts: list[str] = []
    total = 0
    for element_id in node.element_ids:
        text = element_text_by_id.get(element_id, "").strip()
        if not text:
            continue
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n".join(parts)[:max_chars]


async def summarize_navigation_nodes(
    nodes: list[DocumentNavigationNode],
    extraction: StructuredExtraction,
    summarize: Callable[[str], Awaitable[str]] | None,
    *,
    summarize_many: Callable[[Sequence[str]], Awaitable[Sequence[str | None]]] | None = None,
    max_nodes: int = DEFAULT_SUMMARY_MAX_NODES,
    max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    min_chars: int = DEFAULT_SUMMARY_MIN_CHARS,
) -> list[DocumentNavigationNode]:
    """注入された要約器で node summary を埋める。

    入力 text が `min_chars` 未満の node は要約せず（断片要約を避ける）、上限 `max_nodes`
    件までで止める。要約器が失敗した node は summary 無しのまま据え置く（best-effort）。

    ``summarize_many``（入力の順の結果。失敗は None）を渡すと、まとめて呼ぶ（上限つきの並行。
    #2002）。結果は 1 件ずつ順に呼んだときと同じ: 前から順に、成功した node が ``max_nodes`` 件に
    なるまで要約する（失敗した node は数えず、その分を後ろの node で埋める）。
    """
    element_text_by_id = {
        element.element_id: element.text for element in extraction.elements if element.element_id
    }
    if summarize_many is not None:
        return await _summarize_nodes_in_waves(
            nodes,
            element_text_by_id,
            summarize_many,
            max_nodes=max_nodes,
            max_chars=max_chars,
            min_chars=min_chars,
        )
    if summarize is None:
        raise ValueError("summarize か summarize_many のどちらかを渡してください。")
    summarized: list[DocumentNavigationNode] = []
    remaining = max_nodes
    for node in nodes:
        source = _node_source_text(node, element_text_by_id, max_chars=max_chars)
        if remaining <= 0 or len(source) < min_chars:
            summarized.append(node)
            continue
        try:
            summary = (await summarize(source)).strip()
        except Exception:
            summarized.append(node)
            continue
        remaining -= 1
        summarized.append(node.model_copy(update={"summary": summary or None}))
    return summarized


async def _summarize_nodes_in_waves(
    nodes: list[DocumentNavigationNode],
    element_text_by_id: dict[str, str],
    summarize_many: Callable[[Sequence[str]], Awaitable[Sequence[str | None]]],
    *,
    max_nodes: int,
    max_chars: int,
    min_chars: int,
) -> list[DocumentNavigationNode]:
    """足りない件数だけ前から順にまとめて呼び、失敗の分を次の node で埋める。"""
    candidates: list[tuple[int, str]] = []
    for index, node in enumerate(nodes):
        source = _node_source_text(node, element_text_by_id, max_chars=max_chars)
        if len(source) >= min_chars:
            candidates.append((index, source))
    summaries: dict[int, str] = {}
    remaining = max_nodes
    position = 0
    while remaining > 0 and position < len(candidates):
        wave = candidates[position : position + remaining]
        position += len(wave)
        results = await summarize_many([source for _, source in wave])
        for (index, _), result in zip(wave, results, strict=True):
            if result is None:
                continue
            summaries[index] = result.strip()
            remaining -= 1
    return [
        node.model_copy(update={"summary": summaries[index] or None})
        if index in summaries
        else node
        for index, node in enumerate(nodes)
    ]


def navigation_summary_elements(
    nodes: list[DocumentNavigationNode],
    *,
    start_order: int,
) -> list[DocumentElement]:
    """summary を持つ navigation node を検索可能な合成 element 化する。

    Knowhere の Navigate / progressive disclosure を、確定スタックの hybrid retrieval へ
    つなぐ実現として、章節 summary を `content_kind=section_summary` の検索可能 element
    にして既存 chunking 経路へ流す（pipeline / oracle を変更しない）。citation 側からは
    `section_id` / `section_path` で anchor 章節へ辿れる。
    """
    elements: list[DocumentElement] = []
    order = start_order
    for node in nodes:
        summary = (node.summary or "").strip()
        if not summary:
            continue
        title = node.title.strip()
        text = f"{title}: {summary}" if title else summary
        metadata: dict[str, object] = {
            "section_id": node.section_id,
            "section_path": " > ".join(node.section_path),
            NAV_SUMMARY_KEY: True,
            "nav_depth": node.depth,
            "nav_source_element_ids": ",".join(node.element_ids),
        }
        if node.parent_section_id:
            metadata["parent_section_id"] = node.parent_section_id
        elements.append(
            DocumentElement(
                kind="text",
                text=text,
                order=order,
                element_id=f"nav-summary-{node.section_id}"[:128],
                content_kind=SECTION_SUMMARY_CONTENT_KIND,
                page_number=node.page_start,
                section_path=list(node.section_path),
                metadata=metadata,
            )
        )
        order += 1
    return elements
