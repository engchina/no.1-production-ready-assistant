"""言語資源のOracle永続化を対象としない route 単体テストの明示スタブ。"""

from datetime import UTC, datetime
from typing import Any

import pytest
from rag_parser_core.language import builtin_pack

from app.api.routes import documents
from app.clients import language_pack_release_store
from app.rag import published_language_runtime
from app.schemas.language_pack_release import LanguagePackRelease


def install_language_release_stub(monkeypatch: pytest.MonkeyPatch) -> LanguagePackRelease:
    pack = builtin_pack()
    release = LanguagePackRelease(
        id="synthetic-release",
        version=1,
        effective=pack.to_dict(),
        hash=pack.hash,
        ingest_hash=pack.ingest_hash,
        published_at=datetime.now(UTC),
    )

    class Store:
        def __init__(self, _oracle: Any) -> None:
            pass

        async def current(self) -> LanguagePackRelease:
            return release

        async def get(self, identity: str) -> LanguagePackRelease | None:
            return release if identity == release.id else None

    async def initialized(_oracle: Any = None) -> Store:
        return Store(_oracle)

    monkeypatch.setattr(published_language_runtime, "initialized_release_store", initialized)
    monkeypatch.setattr(documents, "initialized_release_store", initialized)
    monkeypatch.setattr(language_pack_release_store, "LanguagePackReleaseStore", Store)
    return release
