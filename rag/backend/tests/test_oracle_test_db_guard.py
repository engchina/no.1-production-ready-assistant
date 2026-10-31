"""実 Oracle のテストの fixture が、データを消す migration を当てないことのテスト（#619）。

実 DB には接続しない（`SystemSchemaManager` を fake に差し替える）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

import pytest

from tests import _oracle_test_db

_PENDING = [
    {
        "name": "20260930_005_retire_standard_engine_objects",
        "description": "rag_agent_memories などを削除します。",
    }
]


class _FakeManager:
    """状態だけを返し、initialize の呼び出しを記録する。"""

    status_payload: ClassVar[dict[str, Any]] = {}
    initialize_calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, _connection_factory: Any) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return dict(self.status_payload)

    def initialize(self, **kwargs: Any) -> dict[str, Any]:
        type(self).initialize_calls.append(kwargs)
        return {"status": "ready"}


@pytest.fixture
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakeManager]]:
    monkeypatch.setattr(_oracle_test_db, "SystemSchemaManager", _FakeManager)
    _FakeManager.initialize_calls = []
    previous = _oracle_test_db._schema_skip_reason
    _oracle_test_db.ensure_schema.cache_clear()
    yield _FakeManager
    # 実行中のセッションの判定（lru_cache と skip の理由）を元に戻す。
    _oracle_test_db.ensure_schema.cache_clear()
    monkeypatch.setattr(_oracle_test_db, "_schema_skip_reason", previous)


def test_ensure_schema_does_not_apply_pending_destructive_migrations(
    fake_manager: type[_FakeManager],
) -> None:
    fake_manager.status_payload = {
        "status": "outdated",
        "pending_destructive_migrations": _PENDING,
    }

    reason = _oracle_test_db.ensure_schema()

    assert fake_manager.initialize_calls == []
    assert reason is not None
    assert "20260930_005_retire_standard_engine_objects" in reason
    assert "--allow-destructive" in reason
    assert _oracle_test_db.schema_skip_reason() == reason


def test_ensure_schema_applies_non_destructive_migrations(
    fake_manager: type[_FakeManager],
) -> None:
    fake_manager.status_payload = {"status": "outdated", "pending_destructive_migrations": []}

    assert _oracle_test_db.ensure_schema() is None
    # 承認（allow_destructive）は渡さない。
    assert fake_manager.initialize_calls == [{}]
    assert _oracle_test_db.schema_skip_reason() is None


def test_oracle_db_fixture_skips_with_reason_when_schema_was_not_updated(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    # 実 Oracle の結合テストの CI（RAG_TEST_REQUIRE_ORACLE=1）でも、既定の skip を確かめる。
    monkeypatch.delenv("RAG_TEST_REQUIRE_ORACLE", raising=False)
    monkeypatch.setattr(_oracle_test_db, "db_available", lambda: True)
    monkeypatch.setattr(_oracle_test_db, "_schema_skip_reason", "理由のテスト")

    with pytest.raises(pytest.skip.Exception, match="理由のテスト"):
        request.getfixturevalue("oracle_db")


@pytest.mark.parametrize(
    ("available", "schema_reason", "message"),
    [
        (False, None, "未到達"),
        (True, "理由のテスト", "理由のテスト"),
    ],
    ids=["unreachable", "schema_not_updated"],
)
def test_oracle_db_fixture_fails_instead_of_skip_when_oracle_is_required(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    available: bool,
    schema_reason: str | None,
    message: str,
) -> None:
    """RAG_TEST_REQUIRE_ORACLE=1 では、実 Oracle を使えないとき skip せず失敗にする（#1595）。"""
    monkeypatch.setenv("RAG_TEST_REQUIRE_ORACLE", "1")
    monkeypatch.setattr(_oracle_test_db, "db_available", lambda: available)
    monkeypatch.setattr(_oracle_test_db, "_schema_skip_reason", schema_reason)

    with pytest.raises(pytest.fail.Exception, match=message):
        request.getfixturevalue("oracle_db")
