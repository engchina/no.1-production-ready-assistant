"""公開版は完全な保存内容で復元し、処理と内容の同値を区別する。"""

import asyncio
from datetime import UTC, datetime

import pytest
from rag_parser_core.language import (
    FIELD_SPECS,
    FieldKind,
    LanguagePack,
    builtin_pack,
    current_pack,
    resolve,
)

from app.rag.language_pack_releases import (
    active_release,
    artifact_reusable_for_phase,
    bind_release,
    compare_releases,
    index_relation,
    pack_from_release,
)
from app.schemas.language_pack_release import LanguagePackIndexRelation, LanguagePackRelease


def release(pack: LanguagePack, version: int = 1) -> LanguagePackRelease:
    return LanguagePackRelease(
        id=f"synthetic-release-{version}",
        version=version,
        effective=pack.to_dict(),
        hash=pack.hash,
        ingest_hash=pack.ingest_hash,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def changed_pack(*, ingest: bool) -> LanguagePack:
    spec = next(
        item for item in FIELD_SPECS if item.ingest == ingest and item.kind == FieldKind.WORDS
    )
    return resolve(builtin_pack(), {spec.section: {spec.key: ["合成の語"]}})


def test_release_reconstructs_only_complete_content_and_rejects_tampering() -> None:
    original = release(changed_pack(ingest=True))
    assert pack_from_release(original).to_dict() == original.effective
    broken = original.model_copy(update={"effective": {"locale": "ja"}})
    with pytest.raises(ValueError):
        pack_from_release(broken)
    with pytest.raises(ValueError, match="照合"):
        pack_from_release(original.model_copy(update={"ingest_hash": "incorrect"}))


def test_version_number_does_not_determine_rebuild_requirement() -> None:
    before = release(builtin_pack())
    search_only = release(changed_pack(ingest=False), 2)
    assert before.hash != search_only.hash
    comparison = compare_releases(before, search_only)
    assert comparison.differences
    assert not comparison.ingest_changed
    assert not comparison.impact_requires_review
    assert (
        index_relation(
            used_release_id=before.id, used_ingest_hash=before.ingest_hash, current=search_only
        )
        == LanguagePackIndexRelation.EQUIVALENT
    )
    after = release(changed_pack(ingest=True), 3)
    assert compare_releases(before, after).ingest_changed
    assert (
        index_relation(
            used_release_id=before.id, used_ingest_hash=before.ingest_hash, current=after
        )
        == LanguagePackIndexRelation.DIFFERENT
    )
    # 同じ取込内容への回退も、版の大小ではなく内容で判断する。
    restored = release(builtin_pack(), 4)
    assert (
        index_relation(
            used_release_id=before.id, used_ingest_hash=before.ingest_hash, current=restored
        )
        == LanguagePackIndexRelation.EQUIVALENT
    )


def test_missing_version_is_not_retroactively_assigned_from_equal_hash() -> None:
    current = release(builtin_pack())
    assert (
        index_relation(used_release_id=None, used_ingest_hash=current.ingest_hash, current=current)
        == LanguagePackIndexRelation.UNRECORDED
    )

    assert (
        index_relation(used_release_id=current.id, used_ingest_hash=None, current=current)
        == LanguagePackIndexRelation.UNRECORDED
    )
    assert (
        index_relation(
            used_release_id=current.id, used_ingest_hash=current.ingest_hash, current=current
        )
        == LanguagePackIndexRelation.CURRENT
    )


@pytest.mark.parametrize(
    ("section", "key", "stages", "reuse_chunk", "reuse_index"),
    [
        ("markers", "bullet_chars", ["EXTRACT", "CHUNK", "INDEX"], False, False),
        ("inquiry_cues", "operation_trigger_terms", ["CHUNK", "INDEX"], True, False),
        ("entity_cues", "alias_column_words", ["INDEX"], True, True),
    ],
)
def test_impact_uses_reading_stage_and_checks_reused_earlier_artifacts(
    section: str, key: str, stages: list[str], reuse_chunk: bool, reuse_index: bool
) -> None:
    before = release(builtin_pack())
    after = release(resolve(builtin_pack(), {section: {key: ["合成の語"]}}), 2)
    comparison = compare_releases(before, after)
    assert comparison.affected_stages == stages
    assert not comparison.impact_requires_review
    assert artifact_reusable_for_phase(before, after, "CHUNK") is reuse_chunk
    assert artifact_reusable_for_phase(before, after, "INDEX") is reuse_index


def test_unknown_dependency_requires_review_and_cannot_reuse_artifacts() -> None:
    before = release(builtin_pack())
    after = before.model_copy(
        update={"effective": {**before.effective, "future": {"key": "value"}}}
    )
    assert compare_releases(before, after).impact_requires_review
    assert not artifact_reusable_for_phase(before, after, "INDEX")


@pytest.mark.asyncio
async def test_concurrent_jobs_pin_distinct_releases_and_restore_after_failure() -> None:
    first, second = release(builtin_pack()), release(changed_pack(ingest=True), 2)
    ready = asyncio.Event()
    started: list[str] = []

    async def job(version: LanguagePackRelease) -> None:
        with bind_release(version):
            started.append(version.id)
            if len(started) == 2:
                ready.set()
            await ready.wait()
            assert active_release() == version
            assert current_pack().hash == version.hash
            with pytest.raises(RuntimeError), bind_release(second):
                raise RuntimeError("合成の失敗")
            assert active_release() == version
            assert current_pack().hash == version.hash

    await asyncio.gather(job(first), job(second))
    assert active_release() is None
