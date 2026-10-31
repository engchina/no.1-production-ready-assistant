"""合成の独立表で公開 CAS・復元・同時初期化を実 Oracle で検証する。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from rag_parser_core.language import builtin_pack

from app.clients import language_pack_release_store as module
from app.clients.oracle import OracleClient, OracleConnectionProtocol, _execute


@pytest.mark.usefixtures("oracle_db")
async def test_releases_are_atomic_immutable_and_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    suffix = uuid4().hex[:10]
    release_table = f"rag_test_lang_releases_{suffix}"
    state_table = f"rag_test_lang_state_{suffix}"
    monkeypatch.setattr(module, "RELEASE_TABLE", release_table)
    monkeypatch.setattr(module, "STATE_TABLE", state_table)
    sql = module.language_pack_release_statements().replace("rag_lang_", f"rag_test_{suffix}_")
    client = OracleClient()

    def create(connection: OracleConnectionProtocol) -> None:
        for statement in sql.split(";"):
            if statement.strip():
                _execute(connection, statement, {})

    await client._run_transaction(create)
    store = module.LanguagePackReleaseStore(client)
    initial = builtin_pack().to_dict()
    try:
        initialized = await asyncio.gather(
            store.ensure_initialized(initial),
            store.ensure_initialized(initial),
            return_exceptions=True,
        )
        # 片方が失敗しても他方の DB スレッドが終わるまで待ち、cleanup と競合させない。
        for outcome in initialized:
            if isinstance(outcome, BaseException):
                raise outcome
        first, simultaneous = initialized
        assert not isinstance(first, BaseException) and not isinstance(simultaneous, BaseException)
        assert first.current == simultaneous.current
        assert (await store.list()).total == 1
        changed = deepcopy(initial)
        changed["search_terms"]["stopwords"].append("合成語")
        draft = await store.save_draft(changed, expected_revision=first.draft_revision)
        assert draft.current == first.current
        assert draft.draft_revision == first.draft_revision + 1
        assert (await store.current()).effective == initial
        results = await asyncio.gather(
            store.publish(
                expected_revision=draft.draft_revision, expected_current_id=first.current.id
            ),
            store.publish(
                expected_revision=draft.draft_revision, expected_current_id=first.current.id
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, module.LanguagePackReleaseConflict) for item in results) == 1
        current = await store.state()
        assert current.current.version == 2
        assert current.current.effective == changed
        assert current.current.ingest_hash == first.current.ingest_hash
        assert current.current.hash != first.current.hash
        assert await store.get(first.current.id) == first.current
        with pytest.raises(module.LanguagePackReleaseConflict):
            await store.restore(
                first.current.id,
                expected_revision=first.draft_revision,
                expected_current_id=first.current.id,
            )
        restored = await store.restore(
            first.current.id,
            expected_revision=current.draft_revision,
            expected_current_id=current.current.id,
            published_by="synthetic-actor",
            change_note="復元テスト",
        )
        assert restored.current.version == 3
        assert restored.current.previous_id == current.current.id
        assert restored.current.restored_from == first.current.id
        assert restored.current.effective == initial
        assert restored.current.published_by == "synthetic-actor"
        assert restored.current.change_note == "復元テスト"
        assert restored.current.id != first.current.id
        assert restored.draft_effective == initial
        assert (await store.ensure_initialized(changed)).current == restored.current
        assert await store.get("missing-release") is None
        with pytest.raises(LookupError):
            await store.restore(
                "missing-release",
                expected_revision=restored.draft_revision,
                expected_current_id=restored.current.id,
            )
        assert (await store.state()) == restored
        for _ in range(6):
            restored = await store.publish(
                expected_revision=restored.draft_revision, expected_current_id=restored.current.id
            )
        versions = await store.list(limit=3, offset=3)
        assert versions.total == 9
        assert [item.version for item in versions.releases] == [6, 5, 4]
        assert await store.get(first.current.id) == first.current
    finally:

        def cleanup(connection: OracleConnectionProtocol) -> None:
            _execute(connection, f"DROP TABLE {state_table} PURGE", {})
            _execute(connection, f"DROP TABLE {release_table} PURGE", {})

        await client._run_transaction(cleanup)
