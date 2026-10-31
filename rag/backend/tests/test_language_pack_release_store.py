"""言語資源の公開契約と、失敗時に公開版を書き換えないことを確かめる。"""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from rag_parser_core.language import builtin_pack

from app.clients.language_pack_release_store import (
    LanguagePackReleaseConflict,
    LanguagePackReleaseStore,
    _release,
    _validated,
    language_pack_release_statements,
)
from app.clients.oracle import OracleClient
from app.schemas.language_pack_release import LanguagePackRelease, LanguagePackReleaseState
from tests.test_oracle_adapter import FakeOraclePool, _oci_settings, _run_inline


def release_row() -> dict[str, object]:
    pack = builtin_pack()
    return {
        "release_id": "release-a",
        "version": 1,
        "effective_json": pack.to_dict(),
        "content_hash": pack.hash,
        "ingest_hash": pack.ingest_hash,
        "published_at": datetime.now(UTC),
    }


def test_snapshot_requires_every_field_and_checks_hash() -> None:
    row = release_row()
    assert _release(row).effective == builtin_pack().to_dict()
    with pytest.raises(ValueError):
        _validated({"locale": "ja"})
    with pytest.raises(ValueError, match="照合"):
        _release({**row, "content_hash": "0" * 64})


def test_hash_columns_preserve_digest_without_fixed_width_padding() -> None:
    sql = language_pack_release_statements()
    assert "content_hash VARCHAR2(64) NOT NULL" in sql
    assert "ingest_hash VARCHAR2(64) NOT NULL" in sql


@pytest.mark.parametrize("revision,current", [(2, None), (1, "release-b"), (0, "release-a")])
def test_draft_and_head_compare_and_swap(revision: int, current: str | None) -> None:
    state = LanguagePackReleaseState(
        draft_revision=1,
        draft_effective=builtin_pack().to_dict(),
        current=LanguagePackRelease.model_validate(
            {
                "id": "release-a",
                "version": 1,
                "effective": builtin_pack().to_dict(),
                "hash": builtin_pack().hash,
                "ingest_hash": builtin_pack().ingest_hash,
                "published_at": datetime.now(UTC),
            }
        ),
    )
    with pytest.raises(LanguagePackReleaseConflict):
        LanguagePackReleaseStore._check(state, revision, current)


async def test_stale_save_rolls_back_without_insert_or_update() -> None:
    pool = FakeOraclePool(
        execute_results=[
            [
                {
                    "draft_revision": 3,
                    "draft_effective_json": builtin_pack().to_dict(),
                    "current_release_id": "release-a",
                }
            ],
            [release_row()],
        ]
    )
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)
    with pytest.raises(LanguagePackReleaseConflict):
        await LanguagePackReleaseStore(client).save_draft(
            builtin_pack().to_dict(), expected_revision=2
        )
    statements = [call.statement.upper() for call in pool.connection.calls]
    assert "FOR UPDATE" in statements[0]
    assert all("INSERT" not in sql and "UPDATE RAG_" not in sql for sql in statements)


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (25, -1)])
async def test_invalid_paging_does_not_connect(limit: int, offset: int) -> None:
    pool = FakeOraclePool()
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)
    with pytest.raises(ValueError):
        await LanguagePackReleaseStore(client).list(limit=limit, offset=offset)
    assert pool.acquire_calls == 0


_COMPAT_KEYS = (
    "crag_refusal_summary",
    "crag_missing_aspect_template",
    "crag_unknown_aspect_template",
    "crag_quote_only_note",
    "crag_definition_aspect_template",
    "guide_acquisition_incomplete_template",
)


def legacy_release_row(mask: int = 63) -> dict[str, object]:
    """旧形式の項目集合から独立に元の保存 hash を計算する。"""
    row = release_row()
    effective = builtin_pack().to_dict()
    effective["answer_labels"]["cause_gap"] = "合成の保存済み文言"
    effective["answer_labels"]["crag_quote_only_note"] = "合成の保存済み注記"
    for index, key in enumerate(_COMPAT_KEYS):
        if mask & (1 << index):
            del effective["answer_labels"][key]
    canonical = json.dumps(
        [
            [section, key, value]
            for section, fields in sorted(effective.items())
            if isinstance(fields, dict)
            for key, value in sorted(fields.items())
        ],
        ensure_ascii=False,
    )
    row["effective_json"] = effective
    row["content_hash"] = hashlib.sha256(
        f"{effective['locale']}\n{canonical}".encode()
    ).hexdigest()[:16]
    return row


@pytest.mark.parametrize("mask", range(64))
def test_legacy_release_keeps_stored_payload_hash_and_custom_values(mask: int) -> None:
    from rag_parser_core.language import current_pack

    from app.rag.language_pack_releases import bind_release, pack_from_release

    row = legacy_release_row(mask)
    untouched = deepcopy(row)
    release = _release(row)
    assert release.effective == row["effective_json"]
    assert release.hash == row["content_hash"]
    runtime = pack_from_release(release)
    assert runtime.text("answer_labels", "cause_gap") == "合成の保存済み文言"
    if mask:
        assert runtime.hash != release.hash
    else:
        assert runtime.hash == release.hash
    for key in _COMPAT_KEYS:
        expected = release.effective["answer_labels"].get(
            key, builtin_pack().text("answer_labels", key)
        )
        assert runtime.text("answer_labels", key) == expected
    with bind_release(release):
        assert current_pack().hash == runtime.hash
    assert row == untouched
    assert release.effective == row["effective_json"]


@pytest.mark.parametrize(
    "mutation", ["hash", "mandatory", "extra", "normalized", "ingest", "coerced"]
)
def test_legacy_release_does_not_weaken_integrity(mutation: str) -> None:
    from app.rag.language_pack_releases import pack_from_release

    row = legacy_release_row()
    original = _release(deepcopy(row))
    effective = row["effective_json"]
    assert isinstance(effective, dict)
    if mutation == "hash":
        row["content_hash"] = "incorrect"
    elif mutation == "ingest":
        row["ingest_hash"] = "incorrect"
    elif mutation == "mandatory":
        del effective["answer_labels"]["cause_gap"]
    elif mutation == "extra":
        effective["answer_labels"]["unknown_key"] = "合成の余剰項目"
    elif mutation == "coerced":
        effective["answer_labels"]["crag_refusal_summary"] = 3
    else:
        effective["answer_labels"]["cause_gap"] += " "
    with pytest.raises(ValueError):
        _release(row)
    broken = original.model_copy(
        update={
            "effective": effective,
            "hash": row["content_hash"],
            "ingest_hash": row["ingest_hash"],
        }
    )
    with pytest.raises(ValueError):
        pack_from_release(broken)


def test_legacy_draft_read_is_compatible_but_save_validation_is_strict() -> None:
    from app.clients.language_pack_release_store import _state

    row = legacy_release_row()
    pool = FakeOraclePool(
        execute_results=[
            [
                {
                    "draft_revision": 1,
                    "draft_effective_json": row["effective_json"],
                    "current_release_id": "release-a",
                }
            ],
            [row],
        ]
    )
    state = _state(pool.connection)
    assert state.draft_effective == row["effective_json"]
    assert state.current.hash == row["content_hash"]
    with pytest.raises(ValueError):
        _validated(state.draft_effective)


async def test_explicit_publish_enriches_new_release_without_rewriting_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.clients import language_pack_release_store as store_module
    from app.clients import oracle as oracle_module
    from app.rag.language_pack_releases import pack_from_release

    row = legacy_release_row()
    unchanged = deepcopy(row)
    old = _release(row)
    state = LanguagePackReleaseState(draft_revision=1, draft_effective=old.effective, current=old)
    inserted: list[LanguagePackRelease] = []
    monkeypatch.setattr(store_module, "_state", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(
        store_module, "_insert", lambda _connection, release: inserted.append(release)
    )
    monkeypatch.setattr(oracle_module, "_execute", lambda *_args, **_kwargs: None)
    pool = FakeOraclePool()
    oracle = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)
    await LanguagePackReleaseStore(oracle).publish(expected_revision=1, expected_current_id=old.id)
    assert len(inserted) == 1
    new = inserted[0]
    assert new.version == old.version + 1
    assert new.previous_id == old.id
    assert new.hash != old.hash
    assert new.ingest_hash == old.ingest_hash
    assert _validated(new.effective).hash == new.hash
    assert pack_from_release(new).hash == new.hash
    assert new.effective["answer_labels"]["cause_gap"] == "合成の保存済み文言"
    assert row == unchanged
    assert old.effective == row["effective_json"]
    assert old.hash == row["content_hash"]
