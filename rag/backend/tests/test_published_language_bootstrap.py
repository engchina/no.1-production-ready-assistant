"""既存の公開版があるときは、旧ファイルから実行設定を変えない。"""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from rag_parser_core.language import builtin_pack

from app.clients.oracle import OracleClient
from app.rag import published_language
from app.rag.language_pack_store import LANGUAGE_PACK_STORE


async def test_existing_publication_does_not_read_changed_or_unreadable_legacy_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SimpleNamespace(
        current=AsyncMock(return_value=object()), ensure_initialized=AsyncMock()
    )
    monkeypatch.setattr(published_language, "LanguagePackReleaseStore", lambda _oracle: store)
    legacy = Mock(side_effect=AssertionError("公開後に旧ファイルは読み込まない"))
    monkeypatch.setattr(LANGUAGE_PACK_STORE, "overrides", legacy)
    assert cast(object, await published_language.initialized_release_store(OracleClient())) is store
    legacy.assert_not_called()
    store.ensure_initialized.assert_not_awaited()


@pytest.mark.parametrize("error", [ValueError("broken"), OSError("unreadable")])
async def test_first_bootstrap_uses_builtin_when_legacy_is_not_readable(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    store = SimpleNamespace(
        current=AsyncMock(side_effect=LookupError()), ensure_initialized=AsyncMock()
    )
    monkeypatch.setattr(published_language, "LanguagePackReleaseStore", lambda _oracle: store)
    monkeypatch.setattr(LANGUAGE_PACK_STORE, "overrides", Mock(side_effect=error))
    assert cast(object, await published_language.initialized_release_store(OracleClient())) is store
    store.ensure_initialized.assert_awaited_once_with(builtin_pack().to_dict())
