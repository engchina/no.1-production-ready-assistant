"""文書の章節(章節ナビゲーション)の組み立てと検証(#713)。

章節は「並び順 + 階層(1〜6)」の平らな一覧で持つ(Markdown の見出しと同じ。親は並びから決まる)。
人の修正が無ければ、抽出結果の章節(``DocumentNavigationNode``)をこの形に写して返す。人の修正が
あればそれを返し、人が名前・ページを変えていない抽出の章節は、ページ範囲を今の抽出結果に合わせる。
"""

from collections.abc import Mapping, Sequence

from rag_parser_core.extraction import DocumentNavigationNode

from app.schemas.document import DocumentSection


def sections_from_navigation(nodes: Sequence[DocumentNavigationNode]) -> list[DocumentSection]:
    """抽出結果の章節を、並び順 + 階層の一覧に写す(階層は 1〜6 に収める)。"""
    sections: list[DocumentSection] = []
    for node in nodes:
        title = node.title.strip()
        if not title:
            continue
        previous = sections[-1].level if sections else 0
        sections.append(
            DocumentSection(
                id=node.section_id,
                title=title[:200],
                level=max(1, min(node.depth or 1, previous + 1, 6)),
                page_start=node.page_start,
                page_end=node.page_end,
                origin="extraction",
                source_section_id=node.section_id,
            )
        )
    return sections


def refresh_from_extraction(
    sections: Sequence[DocumentSection], extracted: Sequence[DocumentSection]
) -> list[DocumentSection]:
    """人が変えていない抽出の章節のページ範囲を、今の抽出結果に合わせる。"""
    by_source = {section.source_section_id: section for section in extracted}
    refreshed: list[DocumentSection] = []
    for section in sections:
        source = by_source.get(section.source_section_id) if not section.edited else None
        refreshed.append(
            section.model_copy(
                update={"page_start": source.page_start, "page_end": source.page_end}
            )
            if source is not None
            else section
        )
    return refreshed


def section_errors(sections: Sequence[DocumentSection], page_count: int | None) -> list[str]:
    """保存できない理由(id の重複・階層の飛び・ページ範囲)。問題が無ければ空。"""
    errors: list[str] = []
    seen: set[str] = set()
    previous_level = 0
    for index, section in enumerate(sections, start=1):
        label = f"{index} 行目「{section.title}」"
        if section.id in seen:
            errors.append(f"{label}: id が重複しています。")
        seen.add(section.id)
        if section.level > previous_level + 1:
            errors.append(f"{label}: 階層が直前の章節より 2 段以上深くなっています。")
        previous_level = section.level
        start, end = section.page_start, section.page_end
        if start is not None and end is not None and start > end:
            errors.append(f"{label}: 開始ページが終了ページより後ろです。")
        if page_count is not None and any(
            page is not None and page > page_count for page in (start, end)
        ):
            errors.append(f"{label}: ページが文書のページ数({page_count})を超えています。")
    return errors


def stored_sections(raw: object) -> list[DocumentSection]:
    """保存済みの章節の JSON を読む(壊れた行は飛ばす)。"""
    if not isinstance(raw, list):
        return []
    sections: list[DocumentSection] = []
    for item in raw:
        if isinstance(item, Mapping):
            try:
                sections.append(DocumentSection.model_validate(dict(item)))
            except ValueError:
                continue
    return sections


def stored_sections_payload(raw: object) -> tuple[list[DocumentSection], set[str]]:
    """保存済みの章節と、人が消した抽出の章節の id(#721)。古い形(一覧だけ)も読む。"""
    if isinstance(raw, Mapping):
        removed = raw.get("removed_source_ids")
        return stored_sections(raw.get("sections")), (
            {str(item) for item in removed if item} if isinstance(removed, list) else set()
        )
    return stored_sections(raw), set()


def add_new_extraction(
    sections: Sequence[DocumentSection],
    extracted: Sequence[DocumentSection],
    removed: set[str],
) -> list[DocumentSection]:
    """修正済みの一覧に無く、人が消してもいない抽出の章節を足す(#721)。

    抽出の並びで直前にある章節(とその子)の後ろに入れ、「新しく抽出」の印を付ける。
    """
    result = list(sections)
    present = {section.source_section_id for section in result if section.source_section_id}
    insert_after = -1
    for item in extracted:
        source_id = item.source_section_id
        if source_id in present:
            insert_after = next(
                index
                for index, section in enumerate(result)
                if section.source_section_id == source_id
            )
            continue
        if not source_id or source_id in removed:
            continue
        position = insert_after + 1
        if insert_after >= 0:
            anchor_level = result[insert_after].level
            while position < len(result) and result[position].level > anchor_level:
                position += 1
        previous_level = result[position - 1].level if position > 0 else 0
        result.insert(
            position,
            item.model_copy(
                update={"level": min(item.level, previous_level + 1), "added_from_extraction": True}
            ),
        )
        present.add(source_id)
        insert_after = position
    return result


def removed_source_ids(
    previous: set[str],
    extracted: Sequence[DocumentSection],
    submitted: Sequence[DocumentSection],
) -> list[str]:
    """保存する一覧に無い抽出の章節の id(人が消したもの)。前に消したものも残す(#721)。"""
    kept = {section.source_section_id for section in submitted if section.source_section_id}
    current = {section.source_section_id for section in extracted if section.source_section_id}
    return sorted((previous | current) - kept)
