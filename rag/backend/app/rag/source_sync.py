"""知識源の同期の契約(差分・再開・削除・権限の撤回。#77)。

外部の知識源(source)の 1 つの instance を、文書の取込へ差分で反映する。取得(一覧の列挙)・
cursor の checkpoint・取込の成否は別の状態として持ち、1 つの「同期の成功」にまとめない。

契約:

- 1 つの item は ``source_id + external_id`` で 1 つの文書の系列に対応する。同じ版の再通知・
  再試行・再開で文書を増やさない(取込の key は item と内容の hash から決める)。
- 内容の更新は新しい文書(次の版)へ取り込み、取込が成功(``indexed``)してから公開を切り替える
  (前の版は ``SyncTarget.publish`` が置き換え済みにする)。取込の失敗では前の版が公開のまま残る。
- 削除と権限の撤回は、source が明示した tombstone(``deleted``)・非公開(``visible=False``)と、
  **最後まで列挙できた**一覧の差分だけで決める。認証の失敗・rate limit・途中のページの失敗
  (``SourceListingError``)・件数の上限での打ち切り(``complete=False`` で次のページが無い)は
  削除の根拠にしない。撤回では公開中の版・前の版・取込中の版をすべて消す(古い ``as_of`` でも
  戻らない)。
- 一覧の位置(cursor)は、ページの item をすべて状態に記録してから進める。失敗した item は状態に
  残し(``failed``)、次の同期の最初に再試行する(cursor の先へ送って取りこぼさない)。
- 同期の設定(利用者が編集する)と実行の状態(worker が書く)は別に持つ。設定が変わったら、
  途中の一覧は捨てて最初から列挙し直す(item の対応は残す)。
- 同じ source の同期は同時に 1 つだけ(``SyncStateStore.lock``)。
- 結果は件数と状態だけを出す(本文・資格情報・ファイルの中身は出さない)。
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

ItemStatus = Literal["published", "pending", "failed", "removing", "deleted", "revoked"]
DocumentState = Literal["indexed", "pending", "failed", "missing"]
RunStatus = Literal["succeeded", "partial", "listing_failed"]
_REMOVED: frozenset[str] = frozenset({"deleted", "revoked"})


# ----------------------------------------------------------------- source / target の契約


@dataclass(frozen=True)
class SourceItem:
    """一覧の 1 件。``version`` は source の版(etag・更新日時・内容の hash など)。"""

    external_id: str
    version: str
    name: str
    content_type: str = "text/plain"
    visible: bool = True
    deleted: bool = False


@dataclass(frozen=True)
class SourcePage:
    """一覧の 1 ページ。``complete`` は一覧がこのページで最後まで終わったこと。"""

    items: Sequence[SourceItem]
    next_cursor: str | None
    complete: bool


class SourceListingError(RuntimeError):
    """一覧を読めなかった(認証・rate limit・途中のページの失敗)。削除の根拠にしない。"""


class SyncItemRejected(RuntimeError):
    """取込先が item を受け付けなかった(形式・解析エンジンの未対応など)。``kind`` は理由の符号。"""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class SyncSource(Protocol):
    async def page(self, cursor: str | None) -> SourcePage:
        """``cursor`` の位置から 1 ページを返す(``None`` は最初から)。"""

    async def read(self, external_id: str) -> bytes:
        """item の内容を返す。"""


class SyncTarget(Protocol):
    async def stage(self, *, key: str, item: SourceItem, data: bytes) -> str:
        """内容を新しい文書として取込に出し、文書の ID を返す。同じ ``key`` は同じ文書。"""

    async def state(self, document_id: str) -> DocumentState:
        """取込の状態。"""

    async def publish(self, *, previous: str | None, document_id: str) -> None:
        """取込の済んだ文書を公開の版にする(``previous`` は置き換え済みにする)。"""

    async def remove(self, document_id: str) -> bool:
        """文書を消す。取込の実行中で今は消せなければ False(止めて、次の同期で消し直す)。"""


# ----------------------------------------------------------------- 状態


@dataclass
class ItemState:
    external_id: str
    name: str
    content_type: str
    status: ItemStatus
    version: str | None = None
    content_sha256: str | None = None
    document_id: str | None = None
    # 置き換え済みの前の版(撤回・削除のときに一緒に消す)。
    previous_document_ids: list[str] = field(default_factory=list)
    pending_document_id: str | None = None
    pending_version: str | None = None
    pending_sha256: str | None = None
    # 取込に出す前に記録する key(出した直後に止まっても、再開で同じ文書を使う)。
    staging_key: str | None = None
    # 消し直す文書(取込の実行中で消せなかったもの)と、消し終えたときの状態。
    removal_document_ids: list[str] = field(default_factory=list)
    removal_reason: Literal["deleted", "revoked"] | None = None
    failed_version: str | None = None
    attempts: int = 0
    last_error: str | None = None
    detected_at: str | None = None
    published_at: str | None = None


@dataclass
class RunState:
    run_id: str
    config_sha256: str
    started_at: str
    cursor: str | None = None
    seen: list[str] = field(default_factory=list)


@dataclass
class SyncReport:
    run_id: str
    status: RunStatus
    resumed: bool
    listing_complete: bool
    pages: int = 0
    listed: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    revoked: int = 0
    published: int = 0
    failed: int = 0
    retried: int = 0
    # 公開まで済んでいない変更(取込中・失敗・消し直し待ち)。
    unreflected: int = 0
    # 公開まで済んだ変更の、検知から公開までの最大(秒)と、未反映の最古の検知からの経過(秒)。
    max_publish_lag_seconds: float | None = None
    oldest_unreflected_seconds: float | None = None
    errors: dict[str, int] = field(default_factory=dict)


@dataclass
class SyncState:
    source_id: str
    items: dict[str, ItemState] = field(default_factory=dict)
    run: RunState | None = None
    last_report: SyncReport | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> SyncState:
        raw = json.loads(text)
        run = raw.get("run")
        report = raw.get("last_report")
        return cls(
            source_id=raw["source_id"],
            items={key: ItemState(**value) for key, value in raw.get("items", {}).items()},
            run=RunState(**run) if run else None,
            last_report=SyncReport(**report) if report else None,
        )


class SyncAlreadyRunningError(RuntimeError):
    """同じ source の同期が実行中。"""


class SyncStateStore:
    """実行の状態を 1 つの JSON ファイルに置く(同じ host の CLI・worker 用)。

    書き込みは一時ファイルから置き換える(途中で止まっても前の状態か次の状態のどちらか)。
    ``lock`` は同じ source の同期を同時に 1 つにする。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path.with_suffix(self._path.suffix + ".lock"), "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SyncAlreadyRunningError("同じ知識源の同期が実行中です。") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self, source_id: str) -> SyncState:
        if not self._path.exists():
            return SyncState(source_id=source_id)
        state = SyncState.from_json(self._path.read_text(encoding="utf-8"))
        if state.source_id != source_id:
            raise ValueError("状態のファイルが別の知識源のものです。")
        return state

    def save(self, state: SyncState) -> None:
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(state.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)


# ----------------------------------------------------------------- 同期の実行


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def staging_key(source_id: str, external_id: str, content_sha256: str) -> str:
    """取込の key(item と内容で決まる。同じ内容の再通知・再開で同じ文書を使う)。"""
    item = hashlib.sha256(f"{source_id}\0{external_id}".encode()).hexdigest()[:24]
    return f"{source_id}/{item}/{content_sha256[:24]}"


def _seconds_between(start: str | None, end: datetime) -> float | None:
    if start is None:
        return None
    return max(0.0, (end - datetime.fromisoformat(start)).total_seconds())


class _Run:
    def __init__(
        self,
        *,
        source_id: str,
        source: SyncSource,
        target: SyncTarget,
        store: SyncStateStore,
        state: SyncState,
        report: SyncReport,
        clock: Callable[[], datetime],
        max_attempts: int,
    ) -> None:
        self.source_id = source_id
        self.source = source
        self.target = target
        self.store = store
        self.state = state
        self.report = report
        self.clock = clock
        self.max_attempts = max_attempts

    def now(self) -> str:
        return self.clock().isoformat()

    def save(self) -> None:
        self.store.save(self.state)

    def error(self, item: ItemState, exc: BaseException) -> None:
        kind = exc.kind if isinstance(exc, SyncItemRejected) else type(exc).__name__
        item.attempts += 1
        item.last_error = kind
        self.report.errors[kind] = self.report.errors.get(kind, 0) + 1

    async def stage(self, item: ItemState, source_item: SourceItem, data: bytes) -> None:
        """内容を取込に出す(key を先に記録してから出す)。"""
        content_sha256 = _sha256(data)
        key = staging_key(self.source_id, item.external_id, content_sha256)
        if item.pending_document_id and item.staging_key != key:
            # 取込中の古い版は公開しない(消し直しの対象にする)。
            item.removal_document_ids.append(item.pending_document_id)
            item.pending_document_id = None
        item.staging_key = key
        item.pending_version = source_item.version
        item.pending_sha256 = content_sha256
        item.detected_at = item.detected_at or self.now()
        self.save()
        document_id = await self.target.stage(key=key, item=source_item, data=data)
        item.pending_document_id = document_id
        item.status = "pending"
        item.failed_version = None
        self.save()

    async def remove_all(self, item: ItemState, status: Literal["deleted", "revoked"]) -> None:
        ids = [
            *([item.document_id] if item.document_id else []),
            *item.previous_document_ids,
            *([item.pending_document_id] if item.pending_document_id else []),
            *item.removal_document_ids,
        ]
        item.document_id = None
        item.previous_document_ids = []
        item.pending_document_id = None
        item.staging_key = None
        item.removal_document_ids = list(dict.fromkeys(ids))
        item.removal_reason = status
        item.status = "removing"
        item.detected_at = item.detected_at or self.now()
        self.save()
        await self.retry_removal(item)

    async def retry_removal(self, item: ItemState) -> None:
        remaining: list[str] = []
        for document_id in item.removal_document_ids:
            try:
                if not await self.target.remove(document_id):
                    remaining.append(document_id)
            except Exception as exc:  # noqa: BLE001 - 1 件の失敗で同期を止めない
                self.error(item, exc)
                remaining.append(document_id)
        item.removal_document_ids = remaining
        if not remaining and item.status == "removing":
            item.status = item.removal_reason or "deleted"
            item.removal_reason = None
            item.version = None
            item.content_sha256 = None
            item.detected_at = None
        self.save()

    async def apply(self, source_item: SourceItem) -> None:
        external_id = source_item.external_id
        item = self.state.items.get(external_id)
        if source_item.deleted or not source_item.visible:
            if item is None or item.status in _REMOVED:
                return
            status: Literal["deleted", "revoked"] = "deleted" if source_item.deleted else "revoked"
            if status == "revoked":
                self.report.revoked += 1
            else:
                self.report.deleted += 1
            await self.remove_all(item, status)
            return
        if item is None:
            item = ItemState(
                external_id=external_id,
                name=source_item.name,
                content_type=source_item.content_type,
                status="pending",
            )
            self.state.items[external_id] = item
        item.name = source_item.name
        item.content_type = source_item.content_type
        if (
            item.status == "pending"
            and item.pending_document_id is not None
            and source_item.version == item.pending_version
        ):
            self.report.unchanged += 1
            return
        if item.status == "published" and source_item.version == item.version:
            self.report.unchanged += 1
            return
        if (
            item.status == "failed"
            and source_item.version == item.failed_version
            and item.attempts >= self.max_attempts
        ):
            return
        is_new = item.document_id is None and item.status != "published"
        try:
            data = await self.source.read(external_id)
            if item.status == "published" and _sha256(data) == item.content_sha256:
                # 版だけが変わった(内容は同じ)。取り込み直さない。
                item.version = source_item.version
                self.report.unchanged += 1
                self.save()
                return
            await self.stage(item, source_item, data)
        except Exception as exc:  # noqa: BLE001 - 1 件の失敗で同期を止めない
            # 公開中の版はそのまま残し、失敗した版だけを記録する(次の同期で再試行する)。
            item.status = "published" if item.document_id else "failed"
            item.failed_version = source_item.version
            self.error(item, exc)
            self.save()
            return
        if is_new:
            self.report.added += 1
        else:
            self.report.updated += 1

    async def retry_failed(self) -> None:
        """前の同期で失敗した item を、一覧より先に再試行する。"""
        for item in list(self.state.items.values()):
            if item.status == "removing":
                await self.retry_removal(item)
                continue
            if item.failed_version is None or item.attempts >= self.max_attempts:
                continue
            self.report.retried += 1
            try:
                data = await self.source.read(item.external_id)
                await self.stage(
                    item,
                    SourceItem(
                        external_id=item.external_id,
                        version=item.failed_version,
                        name=item.name,
                        content_type=item.content_type,
                    ),
                    data,
                )
            except Exception as exc:  # noqa: BLE001
                self.error(item, exc)
                self.save()

    async def reconcile_pending(self) -> None:
        """取込の済んだ版を公開し、失敗した版を記録する(前の版は残す)。"""
        for item in self.state.items.values():
            if item.pending_document_id is None:
                continue
            try:
                document_state = await self.target.state(item.pending_document_id)
                if document_state == "indexed":
                    await self.target.publish(
                        previous=item.document_id, document_id=item.pending_document_id
                    )
                    if item.document_id:
                        item.previous_document_ids.append(item.document_id)
                    item.document_id = item.pending_document_id
                    item.version = item.pending_version
                    item.content_sha256 = item.pending_sha256
                    item.published_at = self.now()
                    lag = _seconds_between(item.detected_at, self.clock())
                    if lag is not None:
                        self.report.max_publish_lag_seconds = max(
                            lag, self.report.max_publish_lag_seconds or 0.0
                        )
                    item.status = "published"
                    item.detected_at = None
                    item.failed_version = None
                    item.attempts = 0
                    item.last_error = None
                    self.report.published += 1
                elif document_state in {"failed", "missing"}:
                    item.removal_document_ids.append(item.pending_document_id)
                    item.failed_version = item.pending_version
                    item.attempts += 1
                    item.last_error = f"ingestion_{document_state}"
                    self.report.errors[item.last_error] = (
                        self.report.errors.get(item.last_error, 0) + 1
                    )
                    item.status = "published" if item.document_id else "failed"
                else:
                    continue
                item.pending_document_id = None
                item.pending_version = None
                item.pending_sha256 = None
                item.staging_key = None
            except Exception as exc:  # noqa: BLE001
                self.error(item, exc)
            self.save()
        for item in self.state.items.values():
            if item.removal_document_ids and item.status != "removing":
                await self.retry_removal(item)


async def run_sync(
    *,
    source_id: str,
    source: SyncSource,
    target: SyncTarget,
    store: SyncStateStore,
    config_sha256: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_attempts: int = 3,
) -> SyncReport:
    """1 回の同期(失敗の再試行 → 一覧の差分 → 取込の済んだ版の公開)。

    一覧が途中で止まったら(``SourceListingError`` / 打ち切り)、cursor と列挙済みの item を
    残して ``partial`` / ``listing_failed`` で終わる。次の同期はその位置から続ける。
    """
    with store.lock():
        state = store.load(source_id)
        resumed = state.run is not None and state.run.config_sha256 == config_sha256
        if not resumed:
            state.run = RunState(
                run_id=uuid4().hex, config_sha256=config_sha256, started_at=clock().isoformat()
            )
        run_state = state.run
        assert run_state is not None
        report = SyncReport(
            run_id=run_state.run_id, status="succeeded", resumed=resumed, listing_complete=False
        )
        run = _Run(
            source_id=source_id,
            source=source,
            target=target,
            store=store,
            state=state,
            report=report,
            clock=clock,
            max_attempts=max_attempts,
        )
        store.save(state)
        await run.retry_failed()
        seen = set(run_state.seen)
        listing_error = False
        while True:
            try:
                page = await source.page(run_state.cursor)
            except SourceListingError as exc:
                report.errors[type(exc).__name__] = report.errors.get(type(exc).__name__, 0) + 1
                listing_error = True
                break
            report.pages += 1
            for source_item in page.items:
                report.listed += 1
                await run.apply(source_item)
                if source_item.external_id not in seen:
                    seen.add(source_item.external_id)
                    run_state.seen.append(source_item.external_id)
            if page.complete:
                report.listing_complete = True
                break
            if page.next_cursor is None:
                break  # 件数の上限などで打ち切られた一覧(最後まで列挙していない)
            run_state.cursor = page.next_cursor
            store.save(state)
        if report.listing_complete:
            # 最後まで列挙した一覧に無い item だけを削除にする。
            for item in list(state.items.values()):
                if item.external_id not in seen and item.status not in {*_REMOVED, "removing"}:
                    report.deleted += 1
                    await run.remove_all(item, "deleted")
            state.run = None
        await run.reconcile_pending()
        now = clock()
        unreflected = [
            item
            for item in state.items.values()
            if item.pending_document_id
            or item.removal_document_ids
            or item.status == "removing"
            or item.failed_version is not None
        ]
        report.unreflected = len(unreflected)
        ages = [
            age
            for age in (_seconds_between(item.detected_at, now) for item in unreflected)
            if age is not None
        ]
        report.oldest_unreflected_seconds = max(ages) if ages else None
        report.failed = sum(1 for item in state.items.values() if item.failed_version is not None)
        if listing_error:
            report.status = "listing_failed"
        elif not report.listing_complete or report.unreflected:
            report.status = "partial"
        state.last_report = report
        store.save(state)
        return report


# ----------------------------------------------------------------- 合成の source(manifest)


class ManifestSource:
    """manifest(JSON)と、その隣のファイルを知識源にする(合成の検証・講義の演習用)。

    manifest の形::

        {"items": [{"external_id": "rules/kinmu.md", "path": "kinmu.md",
                    "version": "2026-04-01", "visible": true}],
         "deleted": ["rules/old.md"]}

    ``version`` を省くと内容の sha256 を版にする。``visible: false`` は閲覧権限の撤回、
    ``deleted`` は source の tombstone。``external_id`` の順に ``page_size`` 件ずつ返し、cursor は
    最後に返した ``external_id``(位置ではないので、途中で item が増減しても欠けない・重ならない)。
    """

    def __init__(self, manifest_path: Path, *, page_size: int = 50) -> None:
        self._path = manifest_path
        self._page_size = max(1, page_size)

    def _manifest(self) -> dict[str, object]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SourceListingError("manifest を読めません。") from exc
        if not isinstance(raw, dict):
            raise SourceListingError("manifest の形が違います。")
        return raw

    def _entries(self) -> list[dict[str, object]]:
        raw = self._manifest()
        listed = raw.get("items", [])
        deleted = raw.get("deleted", [])
        if not isinstance(listed, list) or not isinstance(deleted, list):
            raise SourceListingError("manifest の形が違います。")
        entries = [dict(entry) for entry in listed if isinstance(entry, dict)]
        entries.extend(
            {"external_id": str(external_id), "deleted": True} for external_id in deleted
        )
        return sorted(entries, key=lambda entry: str(entry["external_id"]))

    def _file(self, entry: dict[str, object]) -> Path:
        relative = Path(str(entry.get("path") or entry["external_id"]))
        resolved = (self._path.parent / relative).resolve()
        if not resolved.is_relative_to(self._path.parent.resolve()):
            raise SourceListingError("manifest の外のファイルは読めません。")
        return resolved

    async def page(self, cursor: str | None) -> SourcePage:
        entries = [
            entry
            for entry in self._entries()
            if cursor is None or str(entry["external_id"]) > cursor
        ]
        chunk = entries[: self._page_size]
        items: list[SourceItem] = []
        for entry in chunk:
            external_id = str(entry["external_id"])
            if entry.get("deleted"):
                items.append(SourceItem(external_id, "", Path(external_id).name, deleted=True))
                continue
            path = self._file(entry)
            version = entry.get("version")
            if version is None:
                try:
                    version = _sha256(path.read_bytes())
                except OSError as exc:
                    raise SourceListingError("一覧のファイルを読めません。") from exc
            items.append(
                SourceItem(
                    external_id=external_id,
                    version=str(version),
                    name=str(entry.get("name") or path.name),
                    content_type=str(entry.get("content_type") or "text/plain"),
                    visible=bool(entry.get("visible", True)),
                )
            )
        complete = len(chunk) == len(entries)
        return SourcePage(
            items=items,
            next_cursor=None if complete else str(chunk[-1]["external_id"]),
            complete=complete,
        )

    async def read(self, external_id: str) -> bytes:
        for entry in self._entries():
            if str(entry.get("external_id")) == external_id and not entry.get("deleted"):
                return self._file(entry).read_bytes()
        raise FileNotFoundError(external_id)
