"""知識源の同期の契約(#77)を、合成の source と取込先の fake で確かめる(DB・AI は使わない)。

追加 / 同じ内容の再通知 / 内容の更新 / 削除 / 権限の撤回 / 一覧の途中の失敗 / 再開 /
同期の重複起動 / 設定の変更を、決定論の時計で確かめる。製品の取込先(Oracle)は
``test_source_sync_oracle.py``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.rag.source_sync import (
    DocumentState,
    ManifestSource,
    SourceItem,
    SourceListingError,
    SourcePage,
    SyncAlreadyRunningError,
    SyncItemRejected,
    SyncReport,
    SyncStateStore,
    run_sync,
    staging_key,
)

SOURCE_ID = "manuals"


class FakeSource:
    """ページに分けた一覧と内容。``fail_at`` のページで一覧の失敗を起こす。"""

    def __init__(self, page_size: int = 2) -> None:
        self.items: dict[str, tuple[str, bytes, bool]] = {}
        self.tombstones: list[str] = []
        self.page_size = page_size
        self.fail_at: int | None = None
        self.truncate_at: int | None = None
        self.read_errors: set[str] = set()
        self.requested_cursors: list[str | None] = []

    def put(self, external_id: str, content: str, *, version: str | None = None) -> None:
        visible = self.items.get(external_id, ("", b"", True))[2]
        self.items[external_id] = (version or content, content.encode(), visible)

    def hide(self, external_id: str, *, visible: bool = False) -> None:
        version, data, _ = self.items[external_id]
        self.items[external_id] = (version, data, visible)

    def _listing(self) -> list[SourceItem]:
        listed = [
            SourceItem(external_id, version, f"{external_id}.md", visible=visible)
            for external_id, (version, _data, visible) in sorted(self.items.items())
        ]
        listed.extend(SourceItem(eid, "", eid, deleted=True) for eid in self.tombstones)
        return sorted(listed, key=lambda item: item.external_id)

    async def page(self, cursor: str | None) -> SourcePage:
        """external_id の順に、``cursor``(最後に返した external_id)の次から返す。"""
        self.requested_cursors.append(cursor)
        page_number = len(self.requested_cursors) - 1
        if self.fail_at is not None and page_number == self.fail_at:
            raise SourceListingError("rate limit")
        listing = [item for item in self._listing() if cursor is None or item.external_id > cursor]
        chunk = listing[: self.page_size]
        if self.truncate_at is not None and page_number == self.truncate_at:
            return SourcePage(chunk, None, False)
        complete = len(chunk) == len(listing)
        return SourcePage(chunk, None if complete else chunk[-1].external_id, complete)

    async def read(self, external_id: str) -> bytes:
        if external_id in self.read_errors:
            raise OSError("read failed")
        return self.items[external_id][1]


@dataclass
class FakeTarget:
    """取込先。``next_state`` を次に取込の済んだ状態にする(既定は成功)。"""

    next_state: DocumentState = "indexed"
    documents: dict[str, str] = field(default_factory=dict)  # key -> document_id
    contents: dict[str, bytes] = field(default_factory=dict)
    states: dict[str, DocumentState] = field(default_factory=dict)
    published: list[tuple[str | None, str]] = field(default_factory=list)
    superseded: set[str] = field(default_factory=set)
    removed: list[str] = field(default_factory=list)
    blocked: set[str] = field(default_factory=set)
    stage_calls: int = 0
    created: int = 0
    reject: str | None = None

    async def stage(self, *, key: str, item: SourceItem, data: bytes) -> str:
        self.stage_calls += 1
        if self.reject:
            raise SyncItemRejected(self.reject)
        if key not in self.documents:
            self.created += 1
            self.documents[key] = f"doc-{self.created}"
        document_id = self.documents[key]
        self.contents[document_id] = data
        self.states.setdefault(document_id, self.next_state)
        return document_id

    async def state(self, document_id: str) -> DocumentState:
        return self.states.get(document_id, "missing")

    async def publish(self, *, previous: str | None, document_id: str) -> None:
        self.published.append((previous, document_id))
        if previous:
            self.superseded.add(previous)

    async def remove(self, document_id: str) -> bool:
        if document_id in self.blocked:
            return False
        self.removed.append(document_id)
        self.states.pop(document_id, None)
        # 消した文書は同じ key でも使わない(製品の取込先は保存先の path で文書を探す)。
        self.documents = {k: v for k, v in self.documents.items() if v != document_id}
        return True

    def searchable(self) -> set[str]:
        """検索に出る文書(取込済み・公開済み・置き換え済みでない・消されていない)。"""
        published = {document_id for _previous, document_id in self.published}
        return {
            document_id
            for document_id in published
            if document_id not in self.superseded and document_id not in self.removed
        }


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def store(tmp_path: Path) -> SyncStateStore:
    return SyncStateStore(tmp_path / "state.json")


async def _sync(
    source: FakeSource,
    target: FakeTarget,
    store: SyncStateStore,
    *,
    config: str = "config-1",
    clock: Clock | None = None,
) -> SyncReport:
    return await run_sync(
        source_id=SOURCE_ID,
        source=source,
        target=target,
        store=store,
        config_sha256=config,
        clock=clock or Clock(),
    )


async def test_new_items_are_staged_once_and_published(store: SyncStateStore) -> None:
    source, target = FakeSource(), FakeTarget()
    source.put("a", "勤務規程 第1条")
    source.put("b", "リモートワーク細則")
    source.put("c", "経費精算の手順")

    report = await _sync(source, target, store)

    assert (report.status, report.added, report.published, report.pages) == ("succeeded", 3, 3, 2)
    assert report.listing_complete and report.unreflected == 0
    assert target.searchable() == {"doc-1", "doc-2", "doc-3"}

    # 同じ内容の再通知・再実行では取込に出さない(文書を増やさない)。
    again = await _sync(source, target, store)
    assert (again.added, again.unchanged, target.stage_calls) == (0, 3, 3)


async def test_version_change_with_same_content_is_not_reingested(store: SyncStateStore) -> None:
    source, target = FakeSource(), FakeTarget()
    source.put("a", "勤務規程", version="v1")
    await _sync(source, target, store)
    source.put("a", "勤務規程", version="v2")

    report = await _sync(source, target, store)

    assert (report.updated, report.unchanged, target.stage_calls) == (0, 1, 1)
    assert store.load(SOURCE_ID).items["a"].version == "v2"


async def test_update_keeps_the_published_version_until_the_new_one_is_indexed(
    store: SyncStateStore,
) -> None:
    source, target = FakeSource(), FakeTarget()
    source.put("a", "2023 年度版", version="2023")
    await _sync(source, target, store)
    target.next_state = "pending"
    source.put("a", "2025 年度版", version="2025")

    report = await _sync(source, target, store)

    # 取込中は前の版が公開のまま(未反映として数える)。
    assert (report.updated, report.published, report.unreflected) == (1, 0, 1)
    assert target.searchable() == {"doc-1"}
    item = store.load(SOURCE_ID).items["a"]
    assert (item.document_id, item.pending_document_id) == ("doc-1", "doc-2")

    target.states["doc-2"] = "indexed"
    report = await _sync(source, target, store)

    assert report.published == 1
    assert target.published[-1] == ("doc-1", "doc-2")
    assert target.searchable() == {"doc-2"}
    item = store.load(SOURCE_ID).items["a"]
    assert (item.document_id, item.previous_document_ids, item.version) == (
        "doc-2",
        ["doc-1"],
        "2025",
    )


async def test_failed_update_keeps_the_old_version_and_is_retried(store: SyncStateStore) -> None:
    source, target = FakeSource(), FakeTarget()
    source.put("a", "旧版", version="1")
    await _sync(source, target, store)
    target.next_state = "failed"
    source.put("a", "新版", version="2")

    report = await _sync(source, target, store)

    assert report.status == "partial"
    assert report.errors == {"ingestion_failed": 1}
    assert target.searchable() == {"doc-1"}
    assert "doc-2" in target.removed  # 失敗した版は残さない
    item = store.load(SOURCE_ID).items["a"]
    assert (item.status, item.document_id, item.failed_version) == ("published", "doc-1", "2")

    # 次の同期の最初に再試行する(同じ内容なので同じ key → 取込先が成功させる)。
    target.next_state = "indexed"
    target.states.pop("doc-2", None)
    report = await _sync(source, target, store)
    assert (report.retried, report.published, report.status) == (1, 1, "succeeded")
    assert store.load(SOURCE_ID).items["a"].failed_version is None


async def test_repeated_failures_stop_after_max_attempts(store: SyncStateStore) -> None:
    source, target = FakeSource(), FakeTarget()
    source.put("a", "本文")
    source.read_errors.add("a")
    for _ in range(4):
        await _sync(source, target, store)
    item = store.load(SOURCE_ID).items["a"]
    assert (item.status, item.attempts, item.last_error) == ("failed", 3, "OSError")
    assert target.stage_calls == 0


async def test_tombstone_removes_every_version(store: SyncStateStore) -> None:
    source, target = FakeSource(), FakeTarget()
    source.put("a", "v1", version="1")
    await _sync(source, target, store)
    source.put("a", "v2", version="2")
    await _sync(source, target, store)
    del source.items["a"]
    source.tombstones.append("a")

    report = await _sync(source, target, store)

    assert report.deleted == 1
    assert set(target.removed) == {"doc-1", "doc-2"}
    assert target.searchable() == set()
    assert store.load(SOURCE_ID).items["a"].status == "deleted"


async def test_item_missing_from_a_complete_listing_is_deleted(store: SyncStateStore) -> None:
    source, target = FakeSource(), FakeTarget()
    source.put("a", "残る")
    source.put("b", "消える")
    await _sync(source, target, store)
    del source.items["b"]

    report = await _sync(source, target, store)

    assert (report.deleted, report.listing_complete) == (1, True)
    assert target.searchable() == {"doc-1"}


@pytest.mark.parametrize("failure", ["listing_error", "truncated"])
async def test_incomplete_listing_never_deletes(store: SyncStateStore, failure: str) -> None:
    source, target = FakeSource(page_size=1), FakeTarget()
    for name in ("a", "b", "c"):
        source.put(name, name)
    await _sync(source, target, store)
    # 2 ページ目で失敗・1 ページ目で打ち切り。一覧に出ない item があっても削除にしない。
    source.requested_cursors.clear()
    if failure == "listing_error":
        source.fail_at = 1
    else:
        source.truncate_at = 0

    report = await _sync(source, target, store)

    assert report.deleted == 0
    assert not report.listing_complete
    assert report.status == ("listing_failed" if failure == "listing_error" else "partial")
    assert target.searchable() == {"doc-1", "doc-2", "doc-3"}


async def test_resume_continues_from_the_checkpoint_without_loss_or_duplicates(
    store: SyncStateStore,
) -> None:
    source, target = FakeSource(page_size=1), FakeTarget()
    for name in ("a", "b", "c", "d"):
        source.put(name, name)
    source.fail_at = 2  # a・b を取り込んだところで一覧が止まる

    first = await _sync(source, target, store)

    assert (first.status, first.added) == ("listing_failed", 2)
    run = store.load(SOURCE_ID).run
    assert run is not None and run.cursor == "b" and run.seen == ["a", "b"]

    source.fail_at = None
    del source.items["a"]  # 再開の後で、最後まで列挙した一覧の差分として消える
    source.requested_cursors.clear()
    second = await _sync(source, target, store)

    assert second.resumed
    assert source.requested_cursors[0] == "b"  # 止まった位置から続ける(最初からではない)
    assert (second.added, second.listing_complete) == (2, True)
    assert target.stage_calls == 4  # 欠落(c・d)も二重の取込も無い
    assert store.load(SOURCE_ID).run is None
    # a は再開の前の部分で列挙済みなので、この同期では消さない。次の一覧の差分で消える。
    third = await _sync(source, target, store)
    assert (third.deleted, third.added) == (1, 0)
    assert target.searchable() == {"doc-2", "doc-3", "doc-4"}


async def test_crash_after_staging_reuses_the_same_document(store: SyncStateStore) -> None:
    """取込に出した直後(文書の ID を記録する前)に止まっても、再開で文書を増やさない。"""
    source, target = FakeSource(), FakeTarget()
    source.put("a", "本文")

    class Crash(BaseException):
        pass

    original_stage = target.stage

    async def crashing_stage(*, key: str, item: SourceItem, data: bytes) -> str:
        await original_stage(key=key, item=item, data=data)
        raise Crash

    target.stage = crashing_stage  # type: ignore[method-assign]
    with pytest.raises(Crash):
        await _sync(source, target, store)
    item = store.load(SOURCE_ID).items["a"]
    assert item.pending_document_id is None
    assert item.staging_key == staging_key(SOURCE_ID, "a", item.pending_sha256 or "")

    target.stage = original_stage  # type: ignore[method-assign]
    report = await _sync(source, target, store)

    assert report.resumed
    assert target.created == 1
    assert target.searchable() == {"doc-1"}


async def test_revocation_removes_published_and_pending_versions(store: SyncStateStore) -> None:
    source, target = FakeSource(), FakeTarget()
    source.put("a", "社外秘 v1", version="1")
    await _sync(source, target, store)
    target.next_state = "pending"
    source.put("a", "社外秘 v2", version="2")
    await _sync(source, target, store)
    source.hide("a")

    report = await _sync(source, target, store)

    assert report.revoked == 1
    assert set(target.removed) == {"doc-1", "doc-2"}
    item = store.load(SOURCE_ID).items["a"]
    assert (item.status, item.document_id, item.pending_document_id) == ("revoked", None, None)
    # 取込中だった版が後から終わっても公開しない。
    target.states["doc-2"] = "indexed"
    await _sync(source, target, store)
    assert target.searchable() == set()

    # 権限が戻ったら、新しい文書として取り込む。
    source.hide("a", visible=True)
    target.next_state = "indexed"
    report = await _sync(source, target, store)
    assert report.added == 1
    assert len(target.searchable()) == 1


async def test_blocked_removal_is_retried(store: SyncStateStore) -> None:
    source, target = FakeSource(), FakeTarget()
    source.put("a", "本文")
    await _sync(source, target, store)
    target.blocked.add("doc-1")
    source.hide("a")

    report = await _sync(source, target, store)

    assert (report.status, report.unreflected) == ("partial", 1)
    assert store.load(SOURCE_ID).items["a"].status == "removing"
    target.blocked.clear()
    report = await _sync(source, target, store)
    assert store.load(SOURCE_ID).items["a"].status == "revoked"
    assert report.unreflected == 0


async def test_concurrent_sync_is_rejected(store: SyncStateStore) -> None:
    source, target = FakeSource(), FakeTarget()
    with store.lock(), pytest.raises(SyncAlreadyRunningError):
        await _sync(source, target, store)


async def test_config_change_restarts_the_listing_but_keeps_items(store: SyncStateStore) -> None:
    source, target = FakeSource(page_size=1), FakeTarget()
    source.put("a", "a")
    source.put("b", "b")
    source.fail_at = 1
    await _sync(source, target, store, config="config-1")
    source.requested_cursors.clear()
    source.fail_at = None
    source.requested_cursors.clear()

    report = await _sync(source, target, store, config="config-2")

    assert not report.resumed
    assert source.requested_cursors[0] is None
    assert (report.added, report.unchanged) == (1, 1)


async def test_lag_is_reported_and_state_holds_no_content(store: SyncStateStore) -> None:
    source, target = FakeSource(), FakeTarget(next_state="pending")
    source.put("a", "給与テーブルの本文", version="v1")
    clock = Clock()
    report = await _sync(source, target, store, clock=clock)
    assert report.oldest_unreflected_seconds is not None

    target.states["doc-1"] = "indexed"
    report = await _sync(source, target, store, clock=clock)
    assert report.max_publish_lag_seconds is not None and report.max_publish_lag_seconds > 0
    text = (store._path).read_text(encoding="utf-8")
    assert "給与テーブル" not in text


async def test_manifest_source_pages_tombstones_and_visibility(tmp_path: Path) -> None:
    (tmp_path / "kinmu.md").write_text("勤務規程", encoding="utf-8")
    (tmp_path / "secret.md").write_text("社外秘", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "items": [
                    {"external_id": "rules/kinmu", "path": "kinmu.md", "version": "2026-04"},
                    {"external_id": "rules/secret", "path": "secret.md", "visible": False},
                ],
                "deleted": ["rules/old"],
            }
        ),
        encoding="utf-8",
    )
    source = ManifestSource(manifest, page_size=2)

    first = await source.page(None)
    second = await source.page(first.next_cursor)

    # external_id の順(tombstone も同じ並び)。cursor は最後に返した external_id。
    assert [item.external_id for item in first.items] == ["rules/kinmu", "rules/old"]
    assert first.items[0].version == "2026-04"
    assert first.items[1].deleted
    assert (first.complete, first.next_cursor) == (False, "rules/old")
    assert second.complete and [item.external_id for item in second.items] == ["rules/secret"]
    assert not second.items[0].visible and len(second.items[0].version) == 64
    assert await source.read("rules/kinmu") == "勤務規程".encode()


async def test_manifest_source_failures_are_listing_errors(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    with pytest.raises(SourceListingError):
        await ManifestSource(manifest).page(None)
    manifest.write_text(json.dumps({"items": [{"external_id": "x", "path": "../x"}]}))
    with pytest.raises(SourceListingError):
        await ManifestSource(manifest).page(None)


async def test_rejected_item_is_failed_with_the_reason(store: SyncStateStore) -> None:
    """取込先が受け付けない(解析エンジンが扱えない形式など)item は、理由つきで失敗にする。"""
    source, target = FakeSource(), FakeTarget(reject="parser_unsupported")
    source.put("a", "本文")

    report = await _sync(source, target, store)

    assert (report.status, report.failed, report.errors) == (
        "partial",
        1,
        {"parser_unsupported": 1},
    )
    item = store.load(SOURCE_ID).items["a"]
    assert (item.status, item.last_error, item.pending_document_id) == (
        "failed",
        "parser_unsupported",
        None,
    )
