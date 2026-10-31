"""公開版を初めて使う入口で、旧保存の有効値を一度だけ移行する。"""

from rag_parser_core.language import builtin_pack, resolve

from app.clients.language_pack_release_store import LanguagePackReleaseStore
from app.clients.oracle import OracleClient
from app.config import get_settings, resolve_language_pack_file
from app.rag.language_pack_store import LANGUAGE_PACK_STORE


async def initialized_release_store(oracle: OracleClient | None = None) -> LanguagePackReleaseStore:
    store = LanguagePackReleaseStore(oracle or OracleClient())
    try:
        await store.current()
        return store
    except LookupError:
        pass
    try:
        overrides = LANGUAGE_PACK_STORE.overrides(resolve_language_pack_file(get_settings()))
        effective = resolve(builtin_pack(), overrides)
    except (ValueError, OSError):
        effective = builtin_pack()
    await store.ensure_initialized(effective.to_dict())
    return store
