"""回答の注意書き（設定は製品ごと。#2405・#2449）。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_auth import _service

from pr_system_settings.answer_notice import (
    ANSWER_NOTICE_CONTACT_MAX_CHARS,
    ANSWER_NOTICE_CONTACT_TEMPLATE,
    ANSWER_NOTICE_MAX_CHARS,
    ANSWER_NOTICE_TABLE,
    DEFAULT_ANSWER_NOTICE_BODY,
    DEFAULT_ANSWER_NOTICE_CONTACT,
    DEFAULT_ANSWER_NOTICE_TEXT,
    AnswerNoticeParts,
    AnswerNoticeProvider,
    AnswerNoticeRecord,
    AnswerNoticeSettingsUpdate,
    InMemoryAnswerNoticeStore,
    OracleAnswerNoticeStore,
    answer_notice_setting_key,
    build_answer_notice_router,
)
from pr_system_settings.auth.errors import SecurityApiError, SecurityMigrationRequired
from pr_system_settings.auth.migrations import PLATFORM_AUTH_DDL
from pr_system_settings.auth.store import PLATFORM_AUTH_TABLES


def _client() -> tuple[TestClient, Any]:
    service, _ = _service()
    app = FastAPI()
    app.include_router(
        build_answer_notice_router(
            load=service.load_answer_notice, save=service.save_answer_notice
        ),
        prefix="/api/settings",
    )

    @app.exception_handler(SecurityApiError)
    async def _handler(_: Request, exc: SecurityApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    return TestClient(app), service


def test_get_returns_default_text_when_unset() -> None:
    client, _ = _client()
    response = client.get("/api/settings/answer-notice")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["enabled"] is True
    assert data["text"] is None
    assert data["contact"] is None
    assert data["default_text"] == DEFAULT_ANSWER_NOTICE_BODY
    assert data["default_contact"] == DEFAULT_ANSWER_NOTICE_CONTACT
    assert data["contact_template"] == ANSWER_NOTICE_CONTACT_TEMPLATE
    # 回答に付ける文は本文の後に窓口の案内の 1 行を続ける（#2400）。
    assert data["effective_text"] == DEFAULT_ANSWER_NOTICE_TEXT
    assert DEFAULT_ANSWER_NOTICE_TEXT == (
        DEFAULT_ANSWER_NOTICE_BODY + "\n解決しない場合は、サポート窓口へお問い合わせください。"
    )
    assert data["max_chars"] == ANSWER_NOTICE_MAX_CHARS
    assert data["contact_max_chars"] == ANSWER_NOTICE_CONTACT_MAX_CHARS


def test_put_saves_custom_text_and_next_answer_uses_it() -> None:
    client, service = _client()
    # 保存の前に読んだ cache が残っていても、保存したら次の回答から効く。
    assert service.answer_notice.current_text() == DEFAULT_ANSWER_NOTICE_TEXT
    response = client.put(
        "/api/settings/answer-notice",
        json={"enabled": True, "text": "  1 行目  \r\n2 行目 https://example.com/help \n\n"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["text"] == "1 行目\n2 行目 https://example.com/help"
    expected = data["text"] + "\n解決しない場合は、サポート窓口へお問い合わせください。"
    assert data["effective_text"] == expected
    assert data["updated_at"]
    assert service.answer_notice.current_text() == expected
    assert client.get("/api/settings/answer-notice").json()["data"]["text"] == data["text"]


@pytest.mark.parametrize(
    "text",
    [None, "", "  \n ", DEFAULT_ANSWER_NOTICE_BODY],
    ids=["null", "empty", "blank", "same-as-default"],
)
def test_put_resets_to_default_text(text: str | None) -> None:
    client, service = _client()
    client.put("/api/settings/answer-notice", json={"enabled": True, "text": "独自の文"})
    response = client.put("/api/settings/answer-notice", json={"enabled": True, "text": text})
    assert response.status_code == 200
    data = response.json()["data"]
    # 既定の文は DB に書き込まない（既定を後から直せるようにする）。
    assert data["text"] is None
    assert data["effective_text"] == DEFAULT_ANSWER_NOTICE_TEXT
    assert service.answer_notice.store.load().text is None
    assert service.answer_notice.current_text() == DEFAULT_ANSWER_NOTICE_TEXT


def test_disabled_notice_is_not_attached() -> None:
    client, service = _client()
    response = client.put(
        "/api/settings/answer-notice", json={"enabled": False, "text": "独自の文"}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["enabled"] is False
    # 無効にしても文は残し、画面は有効にしたときの文を出せる。
    assert data["effective_text"].startswith("独自の文\n")
    assert service.answer_notice.current_text() is None
    assert service.answer_notice.current_parts() is None


@pytest.mark.parametrize(
    "text",
    ["あ" * (ANSWER_NOTICE_MAX_CHARS + 1), "制御文字\x07を含む"],
    ids=["too-long", "control-char"],
)
def test_put_rejects_invalid_text(text: str) -> None:
    client, _ = _client()
    response = client.put("/api/settings/answer-notice", json={"enabled": True, "text": text})
    assert response.status_code == 422


def test_update_allows_max_chars_and_tabs() -> None:
    assert AnswerNoticeSettingsUpdate(enabled=True, text="あ" * ANSWER_NOTICE_MAX_CHARS).text
    assert AnswerNoticeSettingsUpdate(enabled=True, text="a\tb").text == "a\tb"


def test_provider_caches_and_expires() -> None:
    store = InMemoryAnswerNoticeStore()
    now = [100.0]
    provider = AnswerNoticeProvider(store, cache_seconds=10, clock=lambda: now[0])
    assert provider.current_text() == DEFAULT_ANSWER_NOTICE_TEXT
    # ほかのプロセス（製品）が保存した。cache の期限までは前の文。
    store.save(AnswerNoticeRecord(enabled=True, text="別の製品で保存した文"))
    now[0] += 5
    assert provider.current_text() == DEFAULT_ANSWER_NOTICE_TEXT
    now[0] += 6
    assert provider.current_text() == (
        "別の製品で保存した文\n解決しない場合は、サポート窓口へお問い合わせください。"
    )


class _FailingStore:
    def load(self) -> AnswerNoticeRecord | None:
        raise SecurityMigrationRequired(ANSWER_NOTICE_TABLE)

    def save(self, record: AnswerNoticeRecord) -> AnswerNoticeRecord:
        raise SecurityMigrationRequired(ANSWER_NOTICE_TABLE)


def test_provider_falls_back_to_default_when_store_fails() -> None:
    provider = AnswerNoticeProvider(_FailingStore())
    # 表が未作成・DB の停止中でも、回答は既定の文を付けて返す。
    assert provider.current_text() == DEFAULT_ANSWER_NOTICE_TEXT


def test_settings_api_reports_missing_table_as_migration_required() -> None:
    client, service = _client()
    service.answer_notice.store = _FailingStore()
    response = client.get("/api/settings/answer-notice")
    assert response.status_code == 409
    assert response.json()["code"] == "SECURITY_SCHEMA_MIGRATION_REQUIRED"
    response = client.put("/api/settings/answer-notice", json={"enabled": True, "text": None})
    assert response.status_code == 409


class _Cursor:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], row: Any, fail: Exception | None):
        self._log = log
        self._row = row
        self._fail = fail
        self.rowcount = 1

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any] | None = None) -> None:
        if self._fail is not None:
            raise self._fail
        self._log.append((" ".join(sql.split()), dict(binds or {})))

    def fetchone(self) -> Any:
        return self._row


def _oracle_store(
    row: Any = None, fail: Exception | None = None
) -> tuple[OracleAnswerNoticeStore, list[tuple[str, dict[str, Any]]]]:
    log: list[tuple[str, dict[str, Any]]] = []

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor(log, row, fail)

        def commit(self) -> None:
            log.append(("COMMIT", {}))

    @contextmanager
    def factory() -> Any:
        yield _Connection()

    return OracleAnswerNoticeStore(factory, answer_notice_setting_key("rag")), log


def test_oracle_store_loads_row() -> None:
    updated = datetime(2026, 10, 18, 9, 0)
    store, log = _oracle_store(row=(0, "本文\r\n2 行目", updated, " 窓口 A\n（内線 1） "))
    record = store.load()
    assert record == AnswerNoticeRecord(
        enabled=False,
        text="本文\n2 行目",
        contact="窓口 A （内線 1）",
        updated_at=updated.replace(tzinfo=UTC),
    )
    assert log[0][0].startswith(
        "SELECT ENABLED, NOTICE_TEXT, UPDATED_AT, CONTACT_TEXT FROM PLATFORM_ANSWER_NOTICE"
    )
    assert log[0][1] == {"setting_key": "answer_notice:rag"}


def test_oracle_store_returns_none_without_row() -> None:
    store, _ = _oracle_store(row=None)
    assert store.load() is None


def test_oracle_store_upserts_the_product_row() -> None:
    store, log = _oracle_store()
    saved = store.save(AnswerNoticeRecord(enabled=True, text=None))
    sql, binds = log[0]
    assert sql.startswith("MERGE INTO PLATFORM_ANSWER_NOTICE")
    assert binds["setting_key"] == "answer_notice:rag"
    assert binds["enabled"] == 1
    assert binds["notice_text"] is None
    assert binds["contact_text"] is None
    assert log[-1][0] == "COMMIT"
    assert saved.updated_at is not None


def test_setting_key_is_per_product() -> None:
    """注意書きは製品ごとの行に保存する（#2449）。製品の行のキーは互いに重ならない。"""
    keys = {answer_notice_setting_key(product) for product in ("rag", "nl2sql", "agent")}
    assert keys == {"answer_notice:rag", "answer_notice:nl2sql", "answer_notice:agent"}
    assert all(len(key) <= 32 for key in keys)  # SETTING_KEY VARCHAR2(32)


def test_oracle_store_reports_missing_table_as_migration_required() -> None:
    store, _ = _oracle_store(fail=RuntimeError("ORA-00942: table or view does not exist"))
    with pytest.raises(SecurityMigrationRequired) as missing:
        store.load()
    assert missing.value.object_name == ANSWER_NOTICE_TABLE


def test_ddl_creates_answer_notice_table_and_it_is_preserved() -> None:
    joined = "\n".join(PLATFORM_AUTH_DDL)
    assert "CREATE TABLE PLATFORM_ANSWER_NOTICE" in joined
    assert "CONSTRAINT PK_PLATFORM_ANSWER_NOTICE PRIMARY KEY (SETTING_KEY)" in joined
    # 窓口の列は #2405 で作った表に足す（#2400）。
    assert "ALTER TABLE PLATFORM_ANSWER_NOTICE ADD (CONTACT_TEXT VARCHAR2(400 CHAR))" in joined
    # 製品のシステムテーブルの全再作成で消さない（共通の表）。
    assert ANSWER_NOTICE_TABLE in PLATFORM_AUTH_TABLES


def test_put_saves_contact_and_composes_the_contact_line() -> None:
    """窓口（連絡先の名前・連絡の方法。#2400）は本文と分けて保存し、案内の 1 行に入れる。"""
    client, service = _client()
    response = client.put(
        "/api/settings/answer-notice",
        json={"enabled": True, "text": None, "contact": "  窓口 A\n（https://example.com/help） "},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["contact"] == "窓口 A （https://example.com/help）"
    assert data["effective_text"] == (
        DEFAULT_ANSWER_NOTICE_BODY
        + "\n解決しない場合は、窓口 A （https://example.com/help）へお問い合わせください。"
    )
    parts = service.answer_notice.current_parts()
    assert parts == AnswerNoticeParts(
        body=DEFAULT_ANSWER_NOTICE_BODY, contact="窓口 A （https://example.com/help）"
    )
    # 製品は窓口の案内の行を差し替えて組み立てられる（RAG の検索・回答プロファイル）。
    assert parts.text("別の行") == DEFAULT_ANSWER_NOTICE_BODY + "\n別の行"
    assert parts.text("") == DEFAULT_ANSWER_NOTICE_BODY

    reset = client.put(
        "/api/settings/answer-notice",
        json={"enabled": True, "text": None, "contact": DEFAULT_ANSWER_NOTICE_CONTACT},
    )
    assert reset.json()["data"]["contact"] is None


def test_put_rejects_too_long_contact() -> None:
    client, _ = _client()
    response = client.put(
        "/api/settings/answer-notice",
        json={"enabled": True, "contact": "あ" * (ANSWER_NOTICE_CONTACT_MAX_CHARS + 1)},
    )
    assert response.status_code == 422
