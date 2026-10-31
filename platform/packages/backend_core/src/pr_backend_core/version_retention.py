"""公開した版の履歴の保存の上限（#2356）。

公開した版を持つもの（Agent の業務 Agent・RAG の業務ガイド）は、新しい版を公開したときに、
公開日時の新しい順に上限を超えた版を消す。公開中の版は古くても消さない（前の版に戻して古い版を
公開しているときは、公開中の版と直近の版で計 ``limit`` 版を残し、公開中以外の古い版から消す）。
版の番号は消しても振り直さない。

版の番号は公開の順に増える（番号の大きい版ほど新しい）前提で、番号の順を公開日時の順として使う。
"""

from __future__ import annotations

from collections.abc import Iterable

DEFAULT_MAX_PUBLISHED_VERSIONS = 5


def versions_to_prune(
    versions: Iterable[int], *, keep: Iterable[int | None] = (), limit: int
) -> list[int]:
    """上限を超えて消す版の番号（古い順）。

    ``keep`` は古くても残す版（公開中の版。None・一覧に無い番号は無視する）で、上限に数える。
    残りの枠を新しい版から埋める。``limit`` が 1 未満なら 1 として扱う。``keep`` が上限より
    多ければ ``keep`` だけを残す。
    """
    keep_count = max(1, int(limit))
    ordered = sorted(set(versions), reverse=True)
    kept = {version for version in keep if version is not None and version in ordered}
    for version in ordered:
        if len(kept) >= keep_count:
            break
        kept.add(version)
    return sorted(version for version in ordered if version not in kept)


__all__ = ["DEFAULT_MAX_PUBLISHED_VERSIONS", "versions_to_prune"]
