"""DB 停止/応答不良時の閲覧系 API 縮退テスト。

ドキュメント一覧・統計・取込ジョブ・ナレッジベース一覧が、DB 不通でも
500 ではなく空データ + warning の 200 応答へ縮退することを検証する。
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from app.api.routes import documents as documents_route
from app.api.routes import knowledge_bases as knowledge_bases_route
from app.config import get_settings
from app.main import app
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


class _RaisingOracle:
    """全 DB 呼び出しが即時に例外を送出する fake(DB 接続不可を再現)。"""

    def __getattr__(self, _name: str) -> Callable[..., Awaitable[object]]:
        async def _raise(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("database is down")

        return _raise


class _HangingOracle:
    """全 DB 呼び出しが返らない fake(DB 応答待ちハングを再現)。"""

    def __getattr__(self, _name: str) -> Callable[..., Awaitable[object]]:
        async def _hang(*_args: object, **_kwargs: object) -> object:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

        return _hang


def _set_timeout(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "db_read_timeout_seconds", seconds)


@pytest.mark.parametrize(
    ("path", "fake"),
    [
        ("/api/documents", _RaisingOracle),
        ("/api/documents/ingestion-jobs", _RaisingOracle),
    ],
)
def test_document_reads_degrade_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    fake: type,
) -> None:
    """DB 接続不可でも documents 系 GET は 200 + warning で縮退する。"""
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake())

    response = client.get(path)

    assert response.status_code == 200
    body = response.json()
    assert len(body["warning_messages"]) == 1
    assert "データベース" in body["warning_messages"][0]


def test_document_list_degrades_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 応答待ちでハングしても timeout 縮退して空一覧を返す。"""
    _set_timeout(monkeypatch, 0.01)
    monkeypatch.setattr(documents_route, "OracleClient", lambda: _HangingOracle())

    response = client.get("/api/documents")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["items"] == []
    assert body["data"]["total"] == 0
    assert "0.01 秒以内に応答しませんでした" in body["warning_messages"][0]


def test_knowledge_base_list_degrades_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 接続不可でもナレッジベース一覧は 200 + warning で縮退する。"""
    monkeypatch.setattr(knowledge_bases_route, "OracleClient", lambda: _RaisingOracle())

    response = client.get("/api/knowledge-bases")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["items"] == []
    assert len(body["warning_messages"]) == 1


@pytest.mark.parametrize("error", [TypeError("bug"), AttributeError("bug")])
async def test_load_or_degrade_does_not_hide_programming_errors(error: Exception) -> None:
    """プログラムの誤りは「DB に接続できない」に変えず、そのまま投げる（#476）。"""
    from app.db_degradation import load_or_degrade

    async def loader() -> object:
        raise error

    with pytest.raises(type(error)):
        await load_or_degrade(loader, timeout_seconds=1, fallback=None, log_label="test")


async def test_load_or_degrade_keeps_http_exceptions() -> None:
    """404 などの意図した応答は、縮退せずにそのまま返す（#476）。"""
    from fastapi import HTTPException

    from app.db_degradation import load_or_degrade

    async def loader() -> object:
        raise HTTPException(status_code=404, detail="not found")

    with pytest.raises(HTTPException):
        await load_or_degrade(loader, timeout_seconds=1, fallback=None, log_label="test")


async def test_load_or_degrade_degrades_db_side_errors() -> None:
    """DB 側の問題（RuntimeError なども含む）は、従来どおり fallback と warning にする。"""
    from app.db_degradation import load_or_degrade

    async def loader() -> str:
        raise RuntimeError("database is down")

    value, degraded = await load_or_degrade(
        loader, timeout_seconds=1, fallback="fallback", log_label="test"
    )
    assert value == "fallback"
    assert degraded is not None and degraded.status == "error"
