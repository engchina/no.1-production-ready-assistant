"""要求の開始時に公開版を固定し、応答の終了まで同じ完全設定を使う。"""

from collections.abc import AsyncIterator

from app.rag.language_pack_releases import bind_release
from app.rag.published_language import initialized_release_store


async def published_language_dependency() -> AsyncIterator[None]:
    store = await initialized_release_store()
    release = await store.current()
    with bind_release(release):
        yield
