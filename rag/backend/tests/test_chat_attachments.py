"""チャットの添付（会話の期間だけ使う作業ファイル。#2517）。

- 添付の節・行を根拠の候補に分け、質問に関係する箇所を選ぶ（小さな添付はすべて、rerank、失敗したら
  文字の一致）。根拠は検索の chunk と同じ形で、保存した引用にも添付の印が残る。
- 回答の検索は、添付の根拠の候補を検索の結果の先頭に 1 回だけ足す（ナレッジベースに当たりが
  無くても）。
- チャットの送信: 添付を会話に結び付け、質問に添付の名前を残し、回答に会話の添付を渡す。
- 添付の API と運用設定の権限、会話の削除で添付も消える。

保存は platform のメモリの store、Oracle は fake（合成のデータ）。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pr_system_settings.chat_attachments import (
    ChatAttachmentService,
    InMemoryChatAttachmentStore,
    parsing,
)
from pytest import MonkeyPatch

from app.api.routes import chat as chat_route
from app.clients.oracle import StoredConversation, StoredMessage
from app.config import Settings
from app.main import app
from app.rag import chat_attachments
from app.rag.chat_attachments import (
    ATTACHMENT_METADATA_KEY,
    attachment_chunk,
    attachment_units,
    select_attachment_units,
)
from app.rag.stored_answer import stored_citation
from app.schemas.search import RetrievedChunk, SearchRequest
from app.security.permissions import ROUTE_PERMISSIONS
from tests.support import AsgiTestClient
from tests.test_chat_api import FakeChatOracle, _stub_stream

client = AsgiTestClient(app)
CSV = "コード,名前,金額\n" + "".join(f"A-{n},品目{n},{n * 100}\n" for n in range(1, 31))
TEXT = "# 第一章\n" + "前書きの本文。" * 20 + "\n\n# 第二章\n" + "保管の期限は三年とする。" * 10


@dataclass(frozen=True)
class _Principal:
    user_uuid: str
    is_system_admin: bool = False


ALICE = _Principal("user-a")


def _service() -> ChatAttachmentService:
    return ChatAttachmentService(
        InMemoryChatAttachmentStore(), product="rag", settings=lambda: SimpleNamespace()
    )


def _upload(service: ChatAttachmentService, name: str, data: str, conversation_id: str) -> str:
    """添付して会話に送信する（会話に結び付くのは送信だけ。解析は根拠を選ぶとき。#2652）。"""
    item = asyncio.run(service.upload(ALICE, file_name=name, data=data.encode()))
    service.attach_to_conversation(
        ALICE,
        [item.attachment_id],
        conversation_id=conversation_id,
        owner_user_uuid=ALICE.user_uuid,
    )
    return item.attachment_id


@pytest.fixture
def service(monkeypatch: MonkeyPatch) -> Iterator[ChatAttachmentService]:
    created = _service()
    chat_attachments.set_chat_attachment_service(created)
    yield created
    chat_attachments.set_chat_attachment_service(None)


# ---- 根拠の候補 ----


def test_units_split_sections_and_group_rows(service: ChatAttachmentService) -> None:
    _upload(service, "a.md", TEXT, "conv-1")
    _upload(service, "b.csv", CSV, "conv-1")
    toolset = service.toolset("conv-1")
    units = attachment_units(toolset.records, toolset.parsed)
    titles = [unit.section_title for unit in units if unit.sheet_name is None]
    assert titles == ["第一章", "第二章"]
    rows = [unit for unit in units if unit.sheet_name is not None]
    assert [(unit.row_start, unit.row_end) for unit in rows] == [(1, 20), (21, 30)]
    assert rows[0].text.splitlines()[1] == "行 1: コード: A-1 / 名前: 品目1 / 金額: 100"


def test_small_attachments_are_all_used_and_large_ones_are_ranked(
    service: ChatAttachmentService,
) -> None:
    _upload(service, "small.txt", "短い本文", "conv-small")
    small = service.toolset("conv-small")
    selected = asyncio.run(
        select_attachment_units("質問", attachment_units(small.records, small.parsed), rerank=None)
    )
    assert [unit.text for unit, _ in selected] == ["短い本文"]

    _upload(service, "b.csv", CSV * 5, "conv-large")
    large = service.toolset("conv-large")
    units = attachment_units(large.records, large.parsed)
    calls: list[tuple[str, int]] = []

    async def rerank(question: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        calls.append((question, top_n))
        return [(3, 0.9), (0, 0.5)]

    ranked = asyncio.run(select_attachment_units("品目の金額", units, rerank=rerank))
    assert calls == [("品目の金額", 6)]
    assert [unit for unit, _ in ranked] == [units[3], units[0]]

    async def broken(question: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        raise RuntimeError("rerank unavailable")

    fallback = asyncio.run(select_attachment_units("A-25", units, rerank=broken))
    assert fallback and "A-25" in fallback[0][0].text


def test_attachment_chunk_is_a_citable_evidence(service: ChatAttachmentService) -> None:
    attachment_id = _upload(service, "a.md", TEXT, "conv-1")
    toolset = service.toolset("conv-1")
    unit = attachment_units(toolset.records, toolset.parsed)[1]
    chunk = attachment_chunk(unit, 0.8)
    assert chunk.document_id == f"attachment:{attachment_id}"
    assert chunk.file_name == "a.md"
    assert chunk.metadata[ATTACHMENT_METADATA_KEY] == attachment_id
    assert chunk.metadata["section_title"] == "第二章"
    # 保存した引用（会話を開き直したとき）にも添付の印が残る。
    assert stored_citation(chunk).metadata[ATTACHMENT_METADATA_KEY] == attachment_id


# ---- 回答の検索 ----


async def test_search_adds_attachment_evidence_once_even_without_hits() -> None:
    from app.rag.answer_engine import AnswerEngine, _SearchState
    from tests.test_cross_references import FakeGenAi, ReferenceOracle

    evidence = RetrievedChunk(
        document_id="attachment:x",
        chunk_id="attachment:x:s1.1",
        text="保管の期限は三年とする。",
        score=1.0,
        file_name="a.md",
        metadata={ATTACHMENT_METADATA_KEY: "x", "section_title": "第二章"},
    )
    engine = AnswerEngine(
        Settings(rag_entity_expansion_enabled=False, rag_graph_expansion_enabled=False),
        oracle=cast(Any, ReferenceOracle([], [])),
        genai=cast(Any, FakeGenAi()),
        attachment_chunks=[evidence],
    )
    state = _SearchState()
    request = SearchRequest(query="保管の期限は？", filters={"knowledge_base_id": "kb-1"})
    first = await engine._search(request, state, retrieval_queries=["保管の期限は？"])
    second = await engine._search(request, state, retrieval_queries=["保管の期限は？", "期限"])
    assert [child.chunk_uid for child in first.child_chunks] == ["attachment:x:s1.1"]
    assert [child.chunk_uid for child in second.child_chunks] == ["attachment:x:s1.1"]
    # 引用に戻すときに使う chunk として登録する。
    assert state.chunks["attachment:x:s1.1"].file_name == "a.md"


def test_attachment_evidence_survives_document_selection_through_engine_records(
    service: ChatAttachmentService,
) -> None:
    """添付の chunk を engine の記録にしても印が残り、文書の選択で後回しにならない（#3126）。

    rerank の関連度が最上位の文書の半分未満で命中 1 件の添付は、ナレッジベースの文書と同じ基準なら
    後回しになる（#3100）。backend の印（`chat_attachment_id`）を engine の記録へ写さないと、
    engine の判定が効かない。
    """
    from dataclasses import replace

    from rag_engine.config import get_settings as engine_settings
    from rag_engine.generation.answer_records import _stored_chunk_answer_record
    from rag_engine.generation.answering import cap_anchors_per_document, select_documents
    from rag_engine.retrieval.attachment_evidence import is_attachment_evidence

    from app.rag.answer_engine import _stored_child

    _upload(service, "b.csv", CSV, "conv-1")
    toolset = service.toolset("conv-1")
    unit = attachment_units(toolset.records, toolset.parsed)[0]
    document = RetrievedChunk(
        document_id="doc-1",
        chunk_id="doc-1:c1",
        text="規則の本文。",
        score=0.9,
        file_name="rule.pdf",
        metadata={},
    )
    records = []
    for chunk, relevance in ((document, 0.9), (attachment_chunk(unit, 0.3), 0.3)):
        record = _stored_chunk_answer_record(_stored_child(chunk, rrf_score=1.0))
        record.metadata["rerank"] = {"relevance_score": relevance}
        records.append(record)
    rule, attached = records
    assert is_attachment_evidence(attached) and not is_attachment_evidence(rule)

    settings = replace(
        engine_settings(environ={}, dotenv_path=None),
        document_selection_enabled=True,
        document_selection_score_ratio=0.5,
        max_anchors_per_document=1,
    )
    kept, deferred, trace = select_documents(records, settings, top_k=1)
    assert attached in kept and attached not in deferred
    assert trace["deferred"] == []
    assert cap_anchors_per_document([rule, attached], settings, top_k=2) == [rule, attached]


def test_attachment_below_rerank_threshold_reaches_final_evidence(
    service: ChatAttachmentService,
) -> None:
    """実際の添付の変換から rerank・context・モデルへ渡す根拠までを通す（#3211）。"""
    from dataclasses import replace

    from rag_engine.config import get_settings as engine_settings
    from rag_engine.generation import answering as a
    from rag_engine.generation.answer_models import AnswerContext
    from rag_engine.generation.answer_records import (
        _records_by_rerank_ranks,
        _stored_chunk_answer_record,
    )
    from rag_engine.models.llm import RerankTextRank

    from app.rag.answer_engine import _stored_child

    _upload(service, "attachment-a.txt", "項目 A の値は17です。", "conv-3211")
    toolset = service.toolset("conv-3211")
    units = attachment_units(toolset.records, toolset.parsed)
    selected = asyncio.run(select_attachment_units("項目 A の値", units, rerank=None))
    assert len(selected) == 1
    source = RetrievedChunk(
        document_id="doc-a",
        chunk_id="chunk-a",
        text="項目の値は添付の入力を使います。",
        score=0.9,
        file_name="source-a.txt",
        metadata={},
    )
    chunks = [source, attachment_chunk(*selected[0])]
    records = [_stored_chunk_answer_record(_stored_child(chunk, rrf_score=1.0)) for chunk in chunks]
    settings = replace(
        engine_settings(environ={}, dotenv_path=None), rerank_min_relevance_score=0.1
    )
    ranked = _records_by_rerank_ranks(
        records, [RerankTextRank(0, 0.9), RerankTextRank(1, 0.01)], settings
    )
    kept, _, _ = a.select_documents(ranked, settings, top_k=1)
    anchors = a.cap_anchors_per_document(kept, settings, top_k=1)
    bundle = a._bundle_context(
        "項目 A の値は何ですか",
        anchors,
        records,
        settings,
        top_k=1,
        neighbor_child_count=0,
        max_records=1,
        max_chars=1000,
    )
    assert len(bundle.records) == 2
    context = AnswerContext(
        records=list(bundle.records),
        text=bundle.text,
        evidence=bundle.evidence,
        evidence_tree=bundle.evidence_tree,
    )
    spans = a._grounded_spans("項目 A の値は何ですか", context, (), None)
    assert any(
        span.get("conversation_attachment") is True and "17" in span["text"] for span in spans
    )
    assert any(
        not span.get("conversation_attachment") and "添付の入力" in span["text"] for span in spans
    )


def test_attachment_candidate_selection_keeps_its_existing_six_unit_limit(
    service: ChatAttachmentService,
) -> None:
    """rerank の下限を免除しても、前段で渡す添付候補を増やさない。"""
    from dataclasses import replace

    _upload(service, "attachment-b.txt", "項目 A の値", "conv-3211-limit")
    toolset = service.toolset("conv-3211-limit")
    unit = attachment_units(toolset.records, toolset.parsed)[0]
    units = [replace(unit, unit_id=str(index), text=f"項目 {index} の値") for index in range(10)]
    selected = asyncio.run(select_attachment_units("項目の値", units, rerank=None))
    assert len(selected) == chat_attachments.MAX_ATTACHMENT_EVIDENCE == 6


# ---- チャットの送信 ----


class _AttachmentOracle(FakeChatOracle):
    async def append_message(self, message: StoredMessage) -> StoredMessage:
        stored = await super().append_message(message)
        stored.attachments = message.attachments
        return stored


def _conversation(fake: FakeChatOracle) -> None:
    fake.conversations["conv-x"] = StoredConversation(
        id="conv-x",
        search_answer_profile_id="bv-1",
        status="ACTIVE",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fake.messages["conv-x"] = []


def test_send_binds_attachments_and_passes_conversation(
    monkeypatch: MonkeyPatch, service: ChatAttachmentService
) -> None:
    from tests.test_chat_api import _FakePipeline

    fake = _AttachmentOracle()
    _conversation(fake)
    _stub_stream(monkeypatch, fake, ["m1"])
    captured: list[object] = []
    original_init = _FakePipeline.__init__

    def init(self: Any, *args: object, **kwargs: object) -> None:
        captured.append(kwargs.get("attachment_conversation_id"))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(_FakePipeline, "__init__", init)
    monkeypatch.setattr(
        chat_route,
        "current_principal",
        lambda request: SimpleNamespace(user_uuid="local-user", is_system_admin=True),
    )
    item = asyncio.run(
        service.upload(_Principal("local-user", True), file_name="b.csv", data=CSV.encode())
    )

    response = client.post(
        "/api/chat/conversations/conv-x/messages/stream",
        json={"content": "品目の件数は?", "attachment_ids": [item.attachment_id]},
    )

    assert response.status_code == 200, response.text
    user = fake.messages["conv-x"][0]
    assert user.attachments == [
        {
            "attachment_id": item.attachment_id,
            "file_name": "b.csv",
            "kind": "csv",
            "size_bytes": len(CSV.encode()),
            "content_sha256": hashlib.sha256(CSV.encode()).hexdigest(),
        }
    ]
    assert chat_route._to_chat_message(user).attachments[0].file_name == "b.csv"
    assert captured == ["conv-x"]
    assert [record.attachment_id for record in service.conversation_records("conv-x")] == [
        item.attachment_id
    ]


def test_evidence_parses_sent_attachments_once_and_skips_unreadable(
    monkeypatch: MonkeyPatch, service: ChatAttachmentService
) -> None:
    """根拠を選ぶときに共通のサービスで解析し、保存した結果を再利用する（#2652）。"""
    parsed: list[str] = []
    original = parsing._parse_text

    def counted(*args: Any) -> Any:
        parsed.append(args[0])
        return original(*args)

    monkeypatch.setattr(parsing, "_parse_text", counted)
    _upload(service, "a.md", TEXT, "conv-e")
    # 壊れた Word は入力の検査を通るが解析で失敗する。根拠に入れずに続ける。
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document>")
    broken = asyncio.run(service.upload(ALICE, file_name="b.docx", data=buffer.getvalue()))
    service.attach_to_conversation(
        ALICE, [broken.attachment_id], conversation_id="conv-e", owner_user_uuid=ALICE.user_uuid
    )
    assert parsed == []

    first = asyncio.run(chat_attachments.attachment_evidence("保管の期限", "conv-e", rerank=None))
    second = asyncio.run(chat_attachments.attachment_evidence("前書き", "conv-e", rerank=None))

    assert first and {chunk.file_name for chunk in first} == {"a.md"}
    assert {chunk.file_name for chunk in second} == {"a.md"}
    assert parsed == ["a.md"]


def test_send_with_unknown_attachment_is_404(
    monkeypatch: MonkeyPatch, service: ChatAttachmentService
) -> None:
    fake = _AttachmentOracle()
    _conversation(fake)
    _stub_stream(monkeypatch, fake, ["m1"])
    monkeypatch.setattr(
        chat_route,
        "current_principal",
        lambda request: SimpleNamespace(user_uuid="local-user", is_system_admin=False),
    )
    response = client.post(
        "/api/chat/conversations/conv-x/messages/stream",
        json={"content": "質問", "attachment_ids": ["missing"]},
    )
    assert response.status_code == 404
    assert fake.messages["conv-x"] == []


def test_delete_conversation_deletes_attachments(
    monkeypatch: MonkeyPatch, service: ChatAttachmentService
) -> None:
    fake = _AttachmentOracle()
    _conversation(fake)
    monkeypatch.setattr(chat_route, "OracleClient", lambda *a, **k: fake)
    attachment_id = _upload(service, "a.md", TEXT, "conv-x")
    assert client.delete("/api/chat/conversations/conv-x").status_code == 200
    assert service.store.get(attachment_id) is None


def test_routes_are_in_the_permission_manifest() -> None:
    assert ROUTE_PERMISSIONS[("POST", "/chat-attachments")] == {"menu.chat"}
    assert ROUTE_PERMISSIONS[("DELETE", "/chat-attachments/{attachment_id}")] == {"menu.chat"}
    assert ROUTE_PERMISSIONS[("PUT", "/settings/chat-attachments")] == {
        "menu.settings_chat_attachments"
    }


def test_settings_default_disabled_and_upload_refused(monkeypatch: MonkeyPatch) -> None:
    from tests.security_support import enable_production_auth, login

    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("attachment_admin", ["menu.settings_chat_attachments", "menu.chat"])
    headers = login(client, "attachment_admin")
    chat_attachments.set_chat_attachment_service(None)
    try:
        settings = client.get("/api/settings/chat-attachments", headers=headers)
        assert settings.status_code == 200, settings.text
        assert settings.json()["data"]["enabled"] is False
        refused = client.post(
            "/api/chat-attachments",
            headers=headers,
            files={"file": ("a.txt", b"x", "text/plain")},
        )
        assert refused.status_code == 403
        assert refused.json()["error_code"] == "CHAT_ATTACHMENT_DISABLED"
        saved = client.put(
            "/api/settings/chat-attachments", headers=headers, json={"enabled": True}
        )
        assert saved.json()["data"]["enabled"] is True
        accepted = client.post(
            "/api/chat-attachments",
            headers=headers,
            files={"file": ("a.txt", b"x", "text/plain")},
        )
        assert accepted.status_code == 200, accepted.text
    finally:
        chat_attachments.set_chat_attachment_service(None)
