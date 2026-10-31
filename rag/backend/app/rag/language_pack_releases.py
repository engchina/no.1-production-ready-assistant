"""完全な公開スナップショットの復元と、処理単位の言語資源の固定。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from rag_parser_core.language import (
    FIELD_SPECS,
    LanguagePack,
    bind_pack,
    build_pack,
    stored_pack_hash,
)

from app.schemas.language_pack_release import (
    LanguagePackFieldDifference,
    LanguagePackIndexRelation,
    LanguagePackRelease,
    LanguagePackReleaseComparison,
)

_ACTIVE_RELEASE: ContextVar[LanguagePackRelease | None] = ContextVar(
    "rag_language_pack_release", default=None
)

# 読み取り元: parser_core の text/markers、抽出項目の型変換、
# chunk の問い合わせ情報、索引保存後の entity_index。前工程からの再実行は後工程も更新する。
_STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(
        (
            "numbers",
            "symbols",
            "references",
            "titles",
            "english",
            "markers",
            "boilerplate",
            "labels",
            "content_labels",
            "search_labels",
            "field_value_cues",
        ),
        ("EXTRACT", "CHUNK", "INDEX"),
    ),
    "inquiry_cues": ("CHUNK", "INDEX"),
    "entity_cues": ("INDEX",),
}
_STAGE_ORDER = ("PREPROCESS", "EXTRACT", "CHUNK", "INDEX")


def pack_from_release(release: LanguagePackRelease) -> LanguagePack:
    """保存時の hash を検証し、追加表示文言だけを実行用に補う。"""
    pack = build_pack(release.effective)
    if (
        stored_pack_hash(release.effective, pack) != release.hash
        or pack.ingest_hash != release.ingest_hash
    ):
        raise ValueError("言語資源の公開版と保存した内容の照合に失敗しました。")
    return pack


@contextmanager
def bind_release(release: LanguagePackRelease) -> Iterator[LanguagePack]:
    """同じ処理から起動する非同期タスクへ公開版と設定を一緒に引き継ぐ。"""
    pack = pack_from_release(release)
    token = _ACTIVE_RELEASE.set(release)
    try:
        with bind_pack(pack):
            yield pack
    finally:
        _ACTIVE_RELEASE.reset(token)


def active_release() -> LanguagePackRelease | None:
    return _ACTIVE_RELEASE.get()


def index_relation(
    *,
    used_release_id: str | None,
    used_ingest_hash: str | None,
    current: LanguagePackRelease,
) -> LanguagePackIndexRelation:
    """版の欠落・内容の同値・現在版を区別し、欠落から旧版を推定しない。"""
    if not used_release_id or not used_ingest_hash:
        return LanguagePackIndexRelation.UNRECORDED
    if used_ingest_hash != current.ingest_hash:
        return LanguagePackIndexRelation.DIFFERENT
    if used_release_id == current.id:
        return LanguagePackIndexRelation.CURRENT
    return LanguagePackIndexRelation.EQUIVALENT


def compare_releases(
    before: LanguagePackRelease, after: LanguagePackRelease
) -> LanguagePackReleaseComparison:
    """保存した内容の差を比較する。版番号や後日の内蔵既定値には依存しない。"""
    specs = {(spec.section, spec.key): spec for spec in FIELD_SPECS}
    differences: list[LanguagePackFieldDifference] = []
    sections = (set(before.effective) | set(after.effective)) - {"locale", "version"}
    unknown = before.effective.get("locale") != after.effective.get("locale")
    stages: set[str] = set()
    for section in sorted(sections):
        old = before.effective.get(section, {})
        new = after.effective.get(section, {})
        for key in sorted(set(old) | set(new)):
            if old.get(key) == new.get(key):
                continue
            spec = specs.get((section, key))
            unknown = unknown or spec is None
            if spec is not None and spec.ingest:
                dependency = _STAGE_DEPENDENCIES.get(section)
                unknown = unknown or dependency is None
                stages.update(dependency or ())
            differences.append(
                LanguagePackFieldDifference(
                    section=section,
                    key=key,
                    before=old.get(key),
                    after=new.get(key),
                    ingest=spec.ingest if spec else True,
                )
            )
    return LanguagePackReleaseComparison(
        before_id=before.id,
        after_id=after.id,
        differences=differences,
        ingest_changed=before.ingest_hash != after.ingest_hash,
        impact_requires_review=unknown,
        affected_stages=[stage for stage in _STAGE_ORDER if stage in stages],
    )


def artifact_reusable_for_phase(
    before: LanguagePackRelease, after: LanguagePackRelease, phase: str
) -> bool:
    """保存済み成果物より前の工程へ影響する変更があれば、途中から再開しない。"""
    if phase not in _STAGE_ORDER:
        return False
    comparison = compare_releases(before, after)
    return not comparison.impact_requires_review and not any(
        _STAGE_ORDER.index(stage) < _STAGE_ORDER.index(phase)
        for stage in comparison.affected_stages
    )
