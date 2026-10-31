"""会話の添付のアップロードと送信した後の解析の分け方（#2652）。

parser・画像の読み取り（Vision）の呼び出しを数えるスタブと合成のファイルで、アップロードで解析
しないこと・送信した添付だけを読むこと・解析の状態と再利用・同時のアクセス・失敗と空の区別・
削除や期限の後の完了を確かめる。業務の内容は書かない。
"""

from __future__ import annotations

import asyncio
import io
import threading
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from chat_attachment_fixtures import PNG_BYTES, docx_bytes, pdf_bytes, pptx_bytes, xlsx_bytes

from pr_system_settings.chat_attachments import (
    ChatAttachmentError,
    ChatAttachmentService,
    ChatAttachmentToolError,
    InMemoryChatAttachmentStore,
    parsing,
)
from pr_system_settings.chat_attachments.models import ParsedAttachment
from pr_system_settings.chat_attachments.parsing import WARNING_NO_CONTENT
from pr_system_settings.chat_attachments.service import (
    MAX_PARSE_ATTEMPTS,
    PARSE_FAILED_CODE,
    PARSE_LEASE_SECONDS,
    PARSE_RETRY_SECONDS,
)
from pr_system_settings.chat_attachments.tools import IMAGE_TRANSCRIPTION_SOURCE


@dataclass(frozen=True)
class _Principal:
    user_uuid: str
    is_system_admin: bool = False


ALICE = _Principal("user-a")

_FORMAT_PARSERS = ("_parse_pdf", "_parse_docx", "_parse_pptx", "_parse_xlsx", "_parse_csv")
_TEXT_PARSER = "_parse_text"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@dataclass
class _Calls:
    parsers: Counter[str]
    vision: list[str]

    @property
    def parser_total(self) -> int:
        return sum(self.parsers.values())


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """形式ごとの parser の呼び出しを数える（中身は元の parser）。"""
    counter: Counter[str] = Counter()
    for name in (*_FORMAT_PARSERS, _TEXT_PARSER):
        original = getattr(parsing, name)

        def counted(*args: Any, _name: str = name, _original: Any = original) -> Any:
            counter[_name] += 1
            return _original(*args)

        monkeypatch.setattr(parsing, name, counted)
    return _Calls(parsers=counter, vision=[])


def _service(
    calls: _Calls,
    *,
    store: InMemoryChatAttachmentStore | None = None,
    clock: _Clock | None = None,
    vision_model: str | None = "vision-model-a",
    vision_text: str = "図の中の文字",
    **kwargs: Any,
) -> ChatAttachmentService:
    model = [vision_model]

    async def reader(data: bytes, media_type: str) -> str:
        calls.vision.append(model[0] or "")
        return vision_text

    service = ChatAttachmentService(
        store or InMemoryChatAttachmentStore(),
        product="agent",
        settings=lambda: SimpleNamespace(),
        image_reader=lambda: reader if model[0] else None,
        image_model_id=lambda: model[0],
        clock=clock or _Clock(),
        **kwargs,
    )
    service._test_model = model  # type: ignore[attr-defined]
    return service


def _upload(service: ChatAttachmentService, name: str, data: bytes, **kwargs: Any) -> str:
    item = asyncio.run(service.upload(ALICE, file_name=name, data=data, **kwargs))
    return item.attachment_id


def _send(service: ChatAttachmentService, *attachment_ids: str, conversation: str = "c1") -> None:
    service.attach_to_conversation(
        ALICE, list(attachment_ids), conversation_id=conversation, owner_user_uuid="user-a"
    )


def _corrupt_docx() -> bytes:
    """zip の構造と本体の部分はあるが、本文の XML が壊れた Word（入力の検査は通る）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document><w:body>")
    return buffer.getvalue()


_UPLOADS: tuple[tuple[str, Callable[[], bytes]], ...] = (
    ("a.png", lambda: PNG_BYTES),
    ("a.pdf", lambda: pdf_bytes(["page one", ""])),
    ("a.docx", docx_bytes),
    ("a.pptx", pptx_bytes),
    ("a.xlsx", xlsx_bytes),
    ("a.csv", lambda: b"code,value\nA,1\n"),
    ("a.txt", lambda: "テキストの本文".encode()),
)


# ---- アップロード: 保存だけ ----


def test_upload_calls_no_parser_or_vision_and_deleted_before_send_stays_zero(
    calls: _Calls,
) -> None:
    service = _service(calls)
    ids = [_upload(service, name, make()) for name, make in _UPLOADS]

    assert calls.parser_total == 0 and calls.vision == []
    listed = service.list_items(ALICE).items
    assert {item.parse_status for item in listed} == {"pending"}
    assert all(item.section_count == 0 and item.sheets == [] for item in listed)
    # 原本・種類・大きさ・hash は保存している。
    record = service.store.get(ids[0])
    assert record is not None and record.kind == "image" and len(record.content_sha256) == 64
    assert service.store.content(ids[0]) == PNG_BYTES

    for attachment_id in ids:
        service.delete(ALICE, attachment_id)
    assert service.toolset("c1").records == []
    assert calls.parser_total == 0 and calls.vision == []


def test_image_upload_succeeds_without_vision_model(calls: _Calls) -> None:
    service = _service(calls, vision_model=None)
    attachment_id = _upload(service, "a.png", PNG_BYTES)
    assert service.limits_data().image_enabled is False
    assert ".png" in service.limits_data().accept
    _send(service, attachment_id)
    toolset = service.toolset("c1")
    listed = toolset.list_attachments()["attachments"][0]
    assert (listed["parse_status"], listed["media_type"]) == ("pending", "image/png")
    assert "chat_attachment_view_image" in listed["message"]
    # 文字の読み取りは、読むときにモデルが無いことを明示する（アップロードは失敗させない）。
    with pytest.raises(ChatAttachmentToolError, match="画像を読み取れるモデル"):
        toolset.parsed(attachment_id)
    # 原画像はモデルが無くても渡せる（モデルへの入力は製品が決める）。
    assert toolset.view_image(attachment_id, "表の見出し").data == PNG_BYTES
    assert calls.vision == []


def test_upload_keeps_input_inspection(calls: _Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(calls)
    with pytest.raises(ChatAttachmentError) as mismatch:
        _upload(service, "a.png", b"\xff\xd8\xff")
    assert mismatch.value.code == "CHAT_ATTACHMENT_TYPE_MISMATCH"
    with pytest.raises(ChatAttachmentError) as not_office:
        _upload(service, "a.docx", b"PK\x03\x04broken")
    assert not_office.value.status_code == 415
    # 展開の上限（zip bomb）はアップロードで確かめる（本文は読まない）。
    monkeypatch.setattr(parsing, "MAX_ZIP_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(ChatAttachmentError) as bomb:
        _upload(service, "a.docx", docx_bytes())
    assert bomb.value.status_code == 422
    assert calls.parser_total == 0


def test_upload_reports_macros_from_the_container(calls: _Calls) -> None:
    service = _service(calls)
    item = asyncio.run(service.upload(ALICE, file_name="a.docm", data=docx_bytes(macro=True)))
    assert item.warnings == ["macros_not_executed"]
    assert calls.parser_total == 0


# ---- 送信: 送信した添付だけを読む ----


def test_only_sent_attachments_are_visible_and_continue_in_the_conversation(
    calls: _Calls,
) -> None:
    service = _service(calls)
    sent = _upload(service, "a.csv", b"code,value\nA,1\n")
    unsent = _upload(service, "b.csv", b"code,value\nB,2\n")
    # 会話を指定したアップロードも、送信するまで会話に結び付けない。
    into_conversation = _upload(
        service,
        "c.csv",
        b"code,value\nC,3\n",
        conversation_id="c1",
        conversation_owner_user_uuid="user-a",
    )
    assert not service.toolset("c1")
    _send(service, sent)

    toolset = service.toolset("c1")
    assert [record.attachment_id for record in toolset.records] == [sent]
    assert unsent not in str(toolset.list_attachments())
    assert into_conversation not in toolset.index_lines()[0]
    assert calls.parsers["_parse_csv"] == 1

    # 同じ会話の後続の質問（新しい toolset）では、保存した結果を再利用して読む。
    again = service.toolset("c1")
    rows = again.call(
        "chat_attachment_read_rows",
        {"attachment_id": sent, "sheet": "a", "start_row": 1, "limit": 5},
    )
    assert rows["rows"][0]["values"] == {"code": "A", "value": "1"}
    assert calls.parsers["_parse_csv"] == 1
    assert service.ensure_parsed(sent).reused is True


def test_reference_fixes_the_original_version(calls: _Calls) -> None:
    service = _service(calls)
    attachment_id = _upload(service, "a.txt", b"body")
    [reference] = service.prepare_attachments(ALICE, [attachment_id], conversation_id=None)
    record = service.store.get(attachment_id)
    assert record is not None and reference.content_sha256 == record.content_sha256


def test_index_shows_kind_and_parse_state_before_reading(calls: _Calls) -> None:
    service = _service(calls)
    attachment_id = _upload(service, "a.csv", b"code,value\nA,1\n")
    _send(service, attachment_id)
    toolset = service.toolset("c1")
    assert toolset.index_lines() == [f"- a.csv（attachment_id={attachment_id}、種類=csv、未解析）"]
    assert calls.parser_total == 0
    listed = toolset.list_attachments()["attachments"][0]
    assert listed["parse_status"] == "ready" and listed["sheets"][0]["row_count"] == 1
    assert toolset.index_lines() == [
        f"- a.csv（attachment_id={attachment_id}、種類=csv、シート「a」1 行）"
    ]


# ---- 解析の状態と再利用 ----


def test_concurrent_reads_in_one_process_parse_once(
    calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(calls)
    attachment_id = _upload(service, "a.txt", b"shared body")
    _send(service, attachment_id)
    started = threading.Event()
    release = threading.Event()
    original = parsing._parse_text

    def slow(*args: Any) -> ParsedAttachment:
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(parsing, "_parse_text", slow)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(service.ensure_parsed(attachment_id)))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    assert started.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)
    assert [result.status for result in results] == ["ready"] * 4
    assert sum(1 for result in results if not result.reused) == 1


def test_other_process_waits_for_the_lease_instead_of_parsing(
    calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InMemoryChatAttachmentStore()
    clock = _Clock()
    first = _service(calls, store=store, clock=clock)
    sleeps: list[float] = []
    second = _service(calls, store=store, clock=clock, parse_wait_seconds=0.0, sleep=sleeps.append)
    attachment_id = _upload(first, "a.txt", b"body")
    _send(first, attachment_id)

    in_progress: list[Any] = []
    original = parsing._parse_text

    def parse_while_other_reads(*args: Any) -> ParsedAttachment:
        in_progress.append(second.ensure_parsed(attachment_id))
        return original(*args)

    monkeypatch.setattr(parsing, "_parse_text", parse_while_other_reads)
    done = first.ensure_parsed(attachment_id)

    assert [result.status for result in in_progress] == ["parsing"]
    assert done.status == "ready"
    assert second.ensure_parsed(attachment_id).reused is True
    assert calls.parsers["_parse_text"] == 1


def test_expired_lease_is_taken_over_and_late_completion_is_discarded(
    calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """終わった process の試行は lease の後に引き継ぎ、古い試行の遅い完了は結果を上書きしない。"""
    store = InMemoryChatAttachmentStore()
    clock = _Clock()
    stale = _service(calls, store=store, clock=clock)
    fresh = _service(calls, store=store, clock=clock)
    attachment_id = _upload(stale, "a.txt", b"body")
    _send(stale, attachment_id)
    outputs = iter(["late result", "fresh result"])

    def parse(file_name: str, data: bytes) -> ParsedAttachment:
        text = next(outputs)
        if text == "late result":
            # 古い試行が止まっている間に lease が切れ、ほかの process が引き継いで完了する。
            clock.now += timedelta(seconds=PARSE_LEASE_SECONDS + 1)
            assert fresh.ensure_parsed(attachment_id).status == "ready"
        return ParsedAttachment.model_validate(
            {"sections": [{"section_id": "s1", "title": file_name, "text": text}]}
        )

    monkeypatch.setattr(parsing, "_parse_text", parse)
    result = stale.ensure_parsed(attachment_id)

    assert result.status == "ready"
    assert result.parsed is not None and result.parsed.sections[0].text == "fresh result"
    saved = store.parsed(attachment_id)
    assert saved is not None and saved.sections[0].text == "fresh result"


def test_parser_version_change_reparses_and_same_version_reuses(
    calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(calls)
    attachment_id = _upload(service, "a.txt", b"body")
    _send(service, attachment_id)
    assert service.ensure_parsed(attachment_id).reused is False
    assert service.ensure_parsed(attachment_id).reused is True
    assert calls.parsers["_parse_text"] == 1
    monkeypatch.setitem(parsing.DOCUMENT_PARSER_VERSIONS, "text", 99)
    assert service.ensure_parsed(attachment_id).reused is False
    assert calls.parsers["_parse_text"] == 2
    # 出力に影響する解析の設定（上限）が変わっても解析し直す。
    monkeypatch.setattr(parsing, "TEXT_SECTION_CHARACTERS", 10)
    assert service.ensure_parsed(attachment_id).reused is False
    assert calls.parsers["_parse_text"] == 3


def test_empty_content_is_ready_and_distinct_from_failure(calls: _Calls) -> None:
    service = _service(calls)
    empty = _upload(service, "empty.txt", b"   \n\n")
    header_only = _upload(service, "header.csv", b"\n\n")
    broken = _upload(service, "broken.docx", _corrupt_docx())
    _send(service, empty, header_only, broken)
    toolset = service.toolset("c1")

    listed = {item["attachment_id"]: item for item in toolset.list_attachments()["attachments"]}
    assert listed[empty]["parse_status"] == "ready"
    assert WARNING_NO_CONTENT in listed[empty]["warnings"]
    assert listed[header_only]["parse_status"] == "ready"
    assert listed[broken]["parse_status"] == "failed"
    assert "読み取れません" in listed[broken]["message"]
    record = service.store.get(broken)
    assert record is not None and record.outline.parse_state is not None
    assert record.outline.parse_state.error_code == "CHAT_ATTACHMENT_UNREADABLE"
    # 入力のせいの失敗は、同じ原本と版では試し直さない。
    service.ensure_parsed(broken)
    assert calls.parsers["_parse_docx"] == 1
    found = toolset.call("chat_attachment_find", {"text": "x", "attachment_id": None, "limit": 5})
    assert [item["attachment_id"] for item in found["unreadable"]] == [broken]


def test_transient_failures_retry_with_limits(
    calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """モデルの応答の時間切れなど一時的な失敗は、回数と間隔を決めて試し直す。"""
    clock = _Clock()
    service = _service(calls, clock=clock)
    attachment_id = _upload(service, "a.png", PNG_BYTES)
    _send(service, attachment_id)
    attempts: list[int] = []

    async def flaky(data: bytes, media_type: str) -> str:
        attempts.append(1)
        raise TimeoutError("temporary")

    monkeypatch.setattr(service, "_image_reader", lambda: flaky)
    first = service.ensure_parsed(attachment_id)
    assert (first.status, first.error_code) == ("failed", PARSE_FAILED_CODE)
    # 再試行の時刻までは試さない。
    assert service.ensure_parsed(attachment_id).status == "failed"
    assert len(attempts) == 1
    for attempt in range(1, MAX_PARSE_ATTEMPTS):
        clock.now += timedelta(seconds=PARSE_RETRY_SECONDS * attempt + 1)
        service.ensure_parsed(attachment_id)
    assert len(attempts) == MAX_PARSE_ATTEMPTS
    clock.now += timedelta(hours=1)
    assert service.ensure_parsed(attachment_id).status == "failed"
    assert len(attempts) == MAX_PARSE_ATTEMPTS


@pytest.mark.parametrize("ending", ["deleted", "expired"], ids=["deleted", "expired"])
def test_completion_after_delete_or_expiry_does_not_revive(
    calls: _Calls, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    clock = _Clock()
    store = InMemoryChatAttachmentStore()
    service = _service(calls, store=store, clock=clock)
    attachment_id = _upload(service, "a.txt", b"body")
    _send(service, attachment_id)
    original = parsing._parse_text

    def end_while_parsing(*args: Any) -> ParsedAttachment:
        if ending == "deleted":
            service.delete(ALICE, attachment_id)
        else:
            clock.now += timedelta(days=60)
        return original(*args)

    monkeypatch.setattr(parsing, "_parse_text", end_while_parsing)
    result = service.ensure_parsed(attachment_id)

    assert result.status == "missing" and result.parsed is None
    record = store.get(attachment_id)
    assert record is None or record.outline.parse_status != "ready"
    assert service.toolset("c1").records == []


def test_legacy_rows_parsed_at_upload_are_read_without_parsing(calls: _Calls) -> None:
    """#2652 より前の行（`parse_state` が無い）は保存した結果をそのまま読む。"""
    store = InMemoryChatAttachmentStore()
    service = _service(calls, store=store)
    attachment_id = _upload(service, "a.txt", b"body")
    _send(service, attachment_id)
    legacy = ParsedAttachment.model_validate(
        {"sections": [{"section_id": "s1", "title": "a.txt", "text": "保存済みの本文"}]}
    )
    record = store.get(attachment_id)
    assert record is not None
    store._records[attachment_id] = record.model_copy(update={"outline": legacy.outline()})  # noqa: SLF001
    store._parsed[attachment_id] = legacy  # noqa: SLF001

    section = service.toolset("c1").call(
        "chat_attachment_read_section",
        {"attachment_id": attachment_id, "section_id": "s1", "offset": 0},
    )
    assert section["content"] == "保存済みの本文"
    assert calls.parser_total == 0


# ---- 形式ごとの読み取り ----


def test_pdf_reports_pages_without_text(calls: _Calls) -> None:
    service = _service(calls)
    text_pdf = _upload(service, "text.pdf", pdf_bytes(["first page", ""]))
    image_only = _upload(service, "image.pdf", pdf_bytes(["", ""]))
    _send(service, text_pdf, image_only)
    listed = {
        item["attachment_id"]: item
        for item in service.toolset("c1").list_attachments()["attachments"]
    }
    assert listed[text_pdf]["pages_without_text"] == [2]
    assert [section["page"] for section in listed[text_pdf]["sections"]] == [1]
    assert listed[image_only]["parse_status"] == "ready"
    assert listed[image_only]["pages_without_text"] == [1, 2]
    assert listed[image_only]["sections"] == []
    assert "読める文字" in listed[image_only]["message"]


def test_table_and_office_files_are_read_after_send(calls: _Calls) -> None:
    service = _service(calls)
    ids = {
        name: _upload(service, name, make())
        for name, make in _UPLOADS
        if name.endswith((".docx", ".pptx", ".xlsx", ".csv"))
    }
    _send(service, *ids.values())
    listed = {
        item["file_name"]: item for item in service.toolset("c1").list_attachments()["attachments"]
    }
    assert {item["parse_status"] for item in listed.values()} == {"ready"}
    assert listed["a.xlsx"]["sheets"][0]["columns"] == ["コード", "名前", "金額"]
    assert listed["a.docx"]["sections"][1]["title"] == "第一の見出し"
    assert calls.parsers == Counter(
        {"_parse_docx": 1, "_parse_pptx": 1, "_parse_xlsx": 1, "_parse_csv": 1}
    )


def test_image_is_read_after_send_and_reused_per_model(calls: _Calls) -> None:
    service = _service(calls)
    attachment_id = _upload(service, "a.png", PNG_BYTES)
    assert calls.vision == []
    _send(service, attachment_id)
    toolset = service.toolset("c1")
    listed = toolset.list_attachments()["attachments"][0]
    # 一覧では読み取らない（汎用の文字の読み取りは、節を読むときだけ）。
    assert listed["parse_status"] == "pending" and calls.vision == []
    section = toolset.call(
        "chat_attachment_read_section",
        {"attachment_id": attachment_id, "section_id": "s1", "offset": 0},
    )
    assert section["content"] == "図の中の文字"
    assert section["content_source"] == IMAGE_TRANSCRIPTION_SOURCE
    assert calls.vision == ["vision-model-a"]
    # 同じモデル・同じ指示の版なら再利用し、モデルが変われば読み直す。
    assert service.ensure_parsed(attachment_id).reused is True
    service._test_model[0] = "vision-model-b"  # type: ignore[attr-defined]
    result = service.ensure_parsed(attachment_id)
    assert result.parsed is not None and result.parsed.extractor is not None
    assert result.parsed.extractor.startswith("vision:vision-model-b@")
    assert calls.vision == ["vision-model-a", "vision-model-b"]


def test_view_image_returns_the_original_for_each_focus_without_reusing_a_reading(
    calls: _Calls,
) -> None:
    """原画像は質問の観点ごとにそのまま返す（前の解釈を原画像の代わりに使い回さない）。"""
    service = _service(calls)
    attachment_id = _upload(service, "a.png", PNG_BYTES)
    unsent = _upload(service, "b.png", PNG_BYTES)
    _send(service, attachment_id)
    toolset = service.toolset("c1")

    first = toolset.view_image(attachment_id, "左上の数値")
    second = toolset.call(
        "chat_attachment_view_image", {"attachment_id": attachment_id, "focus": "凡例の色"}
    )

    assert first.data == PNG_BYTES and first.media_type == "image/png"
    assert first.output["focus"] == "左上の数値" and second["focus"] == "凡例の色"
    record = service.store.get(attachment_id)
    assert record is not None and second["content_sha256"] == record.content_sha256
    assert calls.vision == []
    with pytest.raises(ChatAttachmentToolError, match="この会話に無い"):
        toolset.view_image(unsent, "x")
    service.delete(ALICE, attachment_id)
    with pytest.raises(ChatAttachmentToolError, match="期限が切れたか、削除"):
        toolset.view_image(attachment_id, "x")


def test_image_read_from_event_loop_does_not_block_on_nested_loop(calls: _Calls) -> None:
    service = _service(calls)
    attachment_id = _upload(service, "a.png", PNG_BYTES)
    _send(service, attachment_id)

    async def read() -> Any:
        return await service.ensure_parsed_async(attachment_id)

    assert asyncio.run(read()).status == "ready"
    assert calls.vision == ["vision-model-a"]


# ---- Oracle の SQL ----


class _Cursor:
    def __init__(self, log: list[tuple[str, Any]], rowcount: int) -> None:
        self._log = log
        self.rowcount = rowcount

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, binds: Any = None) -> None:
        self._log.append((" ".join(sql.split()), binds))


@pytest.fixture
def oracle_log() -> Iterator[tuple[list[tuple[str, Any]], list[int]]]:
    yield [], [1]


def test_oracle_replace_state_compares_revision_and_expiry(
    oracle_log: tuple[list[tuple[str, Any]], list[int]],
) -> None:
    from contextlib import contextmanager

    from pr_system_settings.chat_attachments import OracleChatAttachmentStore
    from pr_system_settings.chat_attachments.models import AttachmentOutline, AttachmentParseState

    log, rowcount = oracle_log

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor(log, rowcount[0])

        def commit(self) -> None:
            log.append(("COMMIT", None))

    @contextmanager
    def factory() -> Any:
        yield _Connection()

    store = OracleChatAttachmentStore(factory)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    outline = AttachmentOutline(parse_state=AttachmentParseState(status="parsing", revision=2))
    assert store.replace_state("id-1", outline, expected_revision=1, now=now) is True
    sql, binds = log[0]
    assert sql.startswith("UPDATE PLATFORM_CHAT_ATTACHMENTS SET OUTLINE_JSON = :outline_json WHERE")
    assert "EXPIRES_AT > :now" in sql
    assert "NVL(JSON_VALUE(OUTLINE_JSON, '$.parse_state.revision' RETURNING NUMBER), 0)" in sql
    assert binds["revision"] == 1 and "parsed_json" not in binds

    rowcount[0] = 0
    parsed = ParsedAttachment()
    assert (
        store.replace_state("id-1", outline, expected_revision=2, now=now, parsed=parsed) is False
    )
    sql, binds = log[2]
    assert "PARSED_JSON = :parsed_json" in sql and binds["parsed_json"] == parsed.model_dump_json()
