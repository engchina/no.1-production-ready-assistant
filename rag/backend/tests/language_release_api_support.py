"""公開版 API の境界検証用インメモリストア。実 Oracle の検証には使わない。"""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from rag_parser_core.language import build_pack, builtin_pack

from app.clients.language_pack_release_store import LanguagePackReleaseConflict
from app.schemas.language_pack_release import (
    LanguagePackRelease,
    LanguagePackReleaseList,
    LanguagePackReleaseState,
)


class FakeReleaseStore:
    def __init__(self) -> None:
        pack = builtin_pack()
        first = LanguagePackRelease(
            id="synthetic-1",
            version=1,
            effective=pack.to_dict(),
            hash=pack.hash,
            ingest_hash=pack.ingest_hash,
            published_at=datetime.now(UTC),
        )
        self.releases = [first]
        self.snapshot = LanguagePackReleaseState(
            draft_revision=1, draft_effective=deepcopy(first.effective), current=first
        )

    async def current(self) -> LanguagePackRelease:
        return self.snapshot.current

    async def state(self) -> LanguagePackReleaseState:
        return self.snapshot.model_copy(deep=True)

    async def get(self, identity: str) -> LanguagePackRelease | None:
        return next((item for item in self.releases if item.id == identity), None)

    async def save_draft(
        self, effective: dict[str, Any], *, expected_revision: int
    ) -> LanguagePackReleaseState:
        if expected_revision != self.snapshot.draft_revision:
            raise LanguagePackReleaseConflict("下書きが更新されています。")
        pack = build_pack(effective)
        self.snapshot = self.snapshot.model_copy(
            update={"draft_revision": expected_revision + 1, "draft_effective": pack.to_dict()}
        )
        return await self.state()

    async def publish(
        self,
        *,
        expected_revision: int,
        expected_current_id: str,
        published_by: str | None = None,
        change_note: str | None = None,
    ) -> LanguagePackReleaseState:
        return await self._publish(
            expected_revision, expected_current_id, published_by, change_note
        )

    async def _publish(
        self,
        revision: int,
        identity: str,
        actor: str | None,
        note: str | None,
        restored_from: str | None = None,
    ) -> LanguagePackReleaseState:
        if revision != self.snapshot.draft_revision or identity != self.snapshot.current.id:
            raise LanguagePackReleaseConflict("公開版が更新されています。")
        pack = build_pack(self.snapshot.draft_effective)
        number = len(self.releases) + 1
        release = LanguagePackRelease(
            id=f"synthetic-{number}",
            version=number,
            effective=pack.to_dict(),
            hash=pack.hash,
            ingest_hash=pack.ingest_hash,
            published_at=datetime.now(UTC),
            published_by=actor,
            previous_id=identity,
            restored_from=restored_from,
            change_note=note,
        )
        self.releases.append(release)
        self.snapshot = LanguagePackReleaseState(
            draft_revision=revision + 1, draft_effective=pack.to_dict(), current=release
        )
        return await self.state()

    async def restore(
        self,
        identity: str,
        *,
        expected_revision: int,
        expected_current_id: str,
        published_by: str | None = None,
        change_note: str | None = None,
    ) -> LanguagePackReleaseState:
        release = await self.get(identity)
        if release is None:
            raise LookupError(identity)
        if (
            expected_revision != self.snapshot.draft_revision
            or expected_current_id != self.snapshot.current.id
        ):
            raise LanguagePackReleaseConflict("公開版が更新されています。")
        self.snapshot = self.snapshot.model_copy(
            update={"draft_effective": deepcopy(release.effective)}
        )
        return await self._publish(
            expected_revision, expected_current_id, published_by, change_note, identity
        )

    async def list(self, *, limit: int = 25, offset: int = 0) -> LanguagePackReleaseList:
        return LanguagePackReleaseList(
            total=len(self.releases),
            releases=list(reversed(self.releases))[offset : offset + limit],
        )
