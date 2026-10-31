"""ナレッジベース API のテスト。"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.api.routes import documents as documents_route
from app.api.routes import knowledge_bases as knowledge_bases_route
from app.clients.oracle import KnowledgeBaseNameConflictError
from app.main import app
from app.rag import extraction_field_adapter as fields_mod
from app.rag.extraction_field_adapter import FieldDefinition
from app.rag.kb_adapter_config import KnowledgeBaseQueryConfig, parse_adapter_config
from app.rag.search_answer_profile_config import MAX_SEARCH_ANSWER_PROFILE_KNOWLEDGE_BASES
from app.schemas.document import DocumentDetail, DocumentSummary, FileStatus, IngestionJobPhase
from app.schemas.knowledge_base import (
    KnowledgeBaseDetail,
    KnowledgeBaseRef,
    KnowledgeBaseStatus,
)
from app.schemas.search import SearchMode
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


class FakeKnowledgeBaseOracle:
    """knowledge base API テスト用のインメモリ fake。"""

    def __init__(self) -> None:
        self.knowledge_bases: dict[str, KnowledgeBaseDetail] = {}
        self.documents: dict[str, DocumentDetail] = {
            "doc-1": DocumentDetail(
                id="doc-1",
                file_name="policy.txt",
                status=FileStatus.INDEXED,
                uploaded_at=datetime(2026, 1, 1, tzinfo=UTC),
                indexed_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        }
        self.memberships: set[tuple[str, str]] = set()
        self.subgraphs: dict[str, tuple[list[dict[str, object]], list[dict[str, object]]]] = {}
        self.extraction_fields: dict[str, list[FieldDefinition]] = {}

    async def create_knowledge_base(
        self,
        *,
        name: str,
        description: str | None = None,
        default_search_mode: SearchMode = SearchMode.HYBRID,
        retrieval_config: dict[str, object] | None = None,
    ) -> KnowledgeBaseDetail:
        if any(item.name.casefold() == name.casefold() for item in self.knowledge_bases.values()):
            raise KnowledgeBaseNameConflictError()
        adapter_config = parse_adapter_config(retrieval_config)
        detail = KnowledgeBaseDetail(
            id=f"kb-{uuid4().hex[:8]}",
            name=name,
            description=description,
            status=KnowledgeBaseStatus.ACTIVE,
            default_search_mode=default_search_mode,
            retrieval_config=retrieval_config or {},
            adapter_config=adapter_config,
            legacy_query_config_ignored=adapter_config.query != KnowledgeBaseQueryConfig(),
            document_count=0,
            indexed_document_count=0,
            error_document_count=0,
            searchable_chunk_count=0,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.knowledge_bases[detail.id] = detail
        return detail

    async def list_knowledge_bases(
        self,
        *,
        status: KnowledgeBaseStatus | None = None,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        knowledge_base_ids: list[str] | None = None,
    ) -> list[KnowledgeBaseDetail]:
        items = list(self.knowledge_bases.values())
        if knowledge_base_ids is not None:
            items = [item for item in items if item.id in knowledge_base_ids]
        if status is not None:
            items = [item for item in items if item.status == status]
        if query:
            normalized = query.casefold()
            items = [
                item
                for item in items
                if normalized in item.name.casefold()
                or (item.description is not None and normalized in item.description.casefold())
            ]
        return items[offset : offset + limit if limit is not None else None]

    async def count_knowledge_bases(
        self,
        *,
        status: KnowledgeBaseStatus | None = None,
        query: str | None = None,
        knowledge_base_ids: list[str] | None = None,
    ) -> int:
        return len(
            await self.list_knowledge_bases(
                status=status,
                query=query,
                limit=None,
                offset=0,
                knowledge_base_ids=knowledge_base_ids,
            )
        )

    async def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseDetail | None:
        return self.knowledge_bases.get(knowledge_base_id)

    async def get_knowledge_base_extraction_fields(
        self, knowledge_base_id: str
    ) -> list[FieldDefinition] | None:
        return self.extraction_fields.get(knowledge_base_id)

    async def update_knowledge_base_extraction_fields(
        self, knowledge_base_id: str, fields: list[FieldDefinition] | None
    ) -> None:
        if fields is None:
            self.extraction_fields.pop(knowledge_base_id, None)
        else:
            self.extraction_fields[knowledge_base_id] = fields

    async def fetch_knowledge_base_subgraph(
        self, knowledge_base_id: str, *, limit: int
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        nodes, edges = self.subgraphs.get(knowledge_base_id, ([], []))
        return nodes[:limit], edges[:limit]

    async def update_knowledge_base(
        self,
        knowledge_base_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        default_search_mode: SearchMode | None = None,
        retrieval_config: dict[str, object] | None = None,
        update_fields: set[str] | None = None,
    ) -> KnowledgeBaseDetail:
        detail = self.knowledge_bases.get(knowledge_base_id)
        if detail is None:
            raise KeyError(knowledge_base_id)
        fields = update_fields or set()
        adapter_config = (
            parse_adapter_config(retrieval_config)
            if "retrieval_config" in fields
            else detail.adapter_config
        )
        updated = detail.model_copy(
            update={
                "name": name if "name" in fields and name is not None else detail.name,
                "description": description if "description" in fields else detail.description,
                "default_search_mode": (
                    default_search_mode
                    if "default_search_mode" in fields and default_search_mode is not None
                    else detail.default_search_mode
                ),
                "retrieval_config": (
                    retrieval_config
                    if "retrieval_config" in fields and retrieval_config is not None
                    else detail.retrieval_config
                ),
                "adapter_config": adapter_config,
                "legacy_query_config_ignored": (adapter_config.query != KnowledgeBaseQueryConfig()),
                "updated_at": datetime(2026, 1, 2, tzinfo=UTC),
            }
        )
        self.knowledge_bases[knowledge_base_id] = updated
        return updated

    async def archive_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseDetail:
        detail = self.knowledge_bases.get(knowledge_base_id)
        if detail is None:
            raise KeyError(knowledge_base_id)
        if detail.name.casefold() == "default":
            raise ValueError("DEFAULT ナレッジベースはアーカイブできません。")
        archived = detail.model_copy(
            update={
                "status": KnowledgeBaseStatus.ARCHIVED,
                "updated_at": datetime(2026, 1, 3, tzinfo=UTC),
                "archived_at": datetime(2026, 1, 3, tzinfo=UTC),
            }
        )
        self.knowledge_bases[knowledge_base_id] = archived
        return archived

    async def list_documents(
        self,
        status: FileStatus | None = None,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        knowledge_base_id: str | None = None,
        queued_phase: IngestionJobPhase | None = None,
    ) -> list[DocumentSummary]:
        if queued_phase is not None:
            # fake は取込の job の順番待ちを持たない。
            return []
        documents = [
            document
            for document in self.documents.values()
            # 本物と同じく、カンマ区切りの複数の KB のどれかに所属する文書（#2749）。
            if knowledge_base_id is None
            or any(
                (kb_id, document.id) in self.memberships for kb_id in knowledge_base_id.split(",")
            )
        ]
        if status is not None:
            documents = [document for document in documents if document.status == status]
        if query:
            documents = [
                document
                for document in documents
                if query.casefold() in document.file_name.casefold()
            ]
        return [
            DocumentSummary.model_validate(document.model_dump())
            for document in documents[offset : offset + limit if limit is not None else None]
        ]

    async def count_documents(
        self,
        status: FileStatus | None = None,
        query: str | None = None,
        knowledge_base_id: str | None = None,
        queued_phase: IngestionJobPhase | None = None,
    ) -> int:
        return len(
            await self.list_documents(
                status=status,
                queued_phase=queued_phase,
                query=query,
                limit=None,
                offset=0,
                knowledge_base_id=knowledge_base_id,
            )
        )

    async def assign_documents_to_knowledge_base(
        self,
        knowledge_base_id: str,
        document_ids: list[str],
    ) -> KnowledgeBaseDetail:
        detail = self.knowledge_bases.get(knowledge_base_id)
        if detail is None:
            raise KeyError(knowledge_base_id)
        if detail.status == KnowledgeBaseStatus.ARCHIVED:
            raise ValueError("アーカイブ済みナレッジベースは変更できません。")
        for document_id in document_ids:
            if document_id not in self.documents:
                raise KeyError(document_id)
            self.memberships.add((knowledge_base_id, document_id))
        assigned = len([doc_id for kb_id, doc_id in self.memberships if kb_id == knowledge_base_id])
        updated = detail.model_copy(
            update={
                "document_count": assigned,
                "indexed_document_count": assigned,
                "searchable_chunk_count": assigned,
            }
        )
        self.knowledge_bases[knowledge_base_id] = updated
        return updated

    async def remove_document_from_knowledge_base(
        self,
        knowledge_base_id: str,
        document_id: str,
    ) -> KnowledgeBaseDetail:
        detail = self.knowledge_bases.get(knowledge_base_id)
        if detail is None or document_id not in self.documents:
            raise KeyError(knowledge_base_id)
        self.memberships.discard((knowledge_base_id, document_id))
        return detail

    async def get_document(self, document_id: str) -> DocumentDetail | None:
        return self.documents.get(document_id)

    async def document_exists(self, document_id: str) -> bool:
        return document_id in self.documents

    async def get_document_summary(self, document_id: str) -> DocumentDetail | None:
        # 実装は JSON 列を読まない DocumentSummary を返す。fake は詳細(その上位型)で代用する。
        return self.documents.get(document_id)

    async def list_document_knowledge_bases(self, document_id: str) -> list[KnowledgeBaseRef]:
        if document_id not in self.documents:
            return []
        return [
            KnowledgeBaseRef(id=kb_id, name=self.knowledge_bases[kb_id].name)
            for kb_id, doc_id in sorted(self.memberships)
            if doc_id == document_id and kb_id in self.knowledge_bases
        ]

    async def replace_document_knowledge_bases(
        self,
        document_id: str,
        knowledge_base_ids: list[str],
    ) -> list[KnowledgeBaseRef]:
        if document_id not in self.documents:
            raise KeyError(document_id)
        for knowledge_base_id in knowledge_base_ids:
            detail = self.knowledge_bases.get(knowledge_base_id)
            if detail is None:
                raise KeyError(knowledge_base_id)
            if detail.status == KnowledgeBaseStatus.ARCHIVED:
                raise ValueError("アーカイブ済みナレッジベースは変更できません。")
        self.memberships = {
            (kb_id, doc_id) for kb_id, doc_id in self.memberships if doc_id != document_id
        }
        for knowledge_base_id in knowledge_base_ids:
            self.memberships.add((knowledge_base_id, document_id))
        return await self.list_document_knowledge_bases(document_id)


@pytest.fixture
def fake_oracle(monkeypatch: pytest.MonkeyPatch) -> FakeKnowledgeBaseOracle:
    """knowledge base / document router の OracleClient を fake へ差し替える。"""
    fake = FakeKnowledgeBaseOracle()
    monkeypatch.setattr(knowledge_bases_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    return fake


def test_create_and_list_knowledge_bases(fake_oracle: FakeKnowledgeBaseOracle) -> None:
    """ナレッジベースを作成し、一覧で確認できる。"""
    create_resp = client.post(
        "/api/knowledge-bases",
        json={
            "name": " 社内規程 ",
            "description": "就業規則",
            "default_search_mode": "hybrid",
            "retrieval_config": {"top_k": 20},
        },
    )

    assert create_resp.status_code == 200
    created = create_resp.json()["data"]
    assert created["name"] == "社内規程"
    assert created["retrieval_config"] == {"top_k": 20}
    assert created["id"] in fake_oracle.knowledge_bases

    list_resp = client.get("/api/knowledge-bases?q=規程")
    assert list_resp.status_code == 200
    page = list_resp.json()["data"]
    assert page["total"] == 1
    assert page["items"][0]["name"] == "社内規程"


@pytest.mark.parametrize("reserved_name", ["DEFAULT", "default", " DEFAULT "])
def test_default_is_a_reserved_knowledge_base_name(
    fake_oracle: FakeKnowledgeBaseOracle,
    reserved_name: str,
) -> None:
    """DEFAULT は大文字小文字・前後空白にかかわらずユーザー名に使えない。"""
    create_resp = client.post(
        "/api/knowledge-bases", json={"name": reserved_name, "description": "説明"}
    )

    assert create_resp.status_code == 422
    assert "DEFAULT は予約名のため使用できません。" in create_resp.json()["error_messages"][0]

    detail = client.post(
        "/api/knowledge-bases", json={"name": "社内規程", "description": "説明"}
    ).json()["data"]
    update_resp = client.patch(
        f"/api/knowledge-bases/{detail['id']}",
        json={"name": reserved_name},
    )

    assert update_resp.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "社内規程"},
        {"name": "社内規程", "description": ""},
        {"name": "社内規程", "description": " \u3000\t"},
        {"name": "社内規程", "description": None},
        {"name": " ", "description": "就業規則"},
    ],
    ids=["missing", "empty", "blank", "null", "blank-name"],
)
def test_create_knowledge_base_requires_name_and_description(
    fake_oracle: FakeKnowledgeBaseOracle,
    payload: dict[str, object],
) -> None:
    """ナレッジベースの名前と説明は必須。未指定・空・空白だけ・null は 422 で作成しない（#521）。"""
    response = client.post("/api/knowledge-bases", json=payload)

    assert response.status_code == 422
    assert fake_oracle.knowledge_bases == {}


@pytest.mark.parametrize(
    "description",
    ["", "   ", None],
    ids=["empty", "blank", "null"],
)
def test_update_knowledge_base_rejects_empty_description(
    fake_oracle: FakeKnowledgeBaseOracle,
    description: str | None,
) -> None:
    """更新で説明を空・空白だけ・null にはできない（#521）。"""
    detail = client.post(
        "/api/knowledge-bases", json={"name": "社内規程", "description": "就業規則"}
    ).json()["data"]

    response = client.patch(
        f"/api/knowledge-bases/{detail['id']}", json={"description": description}
    )

    assert response.status_code == 422
    assert "説明を入力してください。" in response.json()["error_messages"][0]
    assert fake_oracle.knowledge_bases[detail["id"]].description == "就業規則"


def test_knowledge_base_without_description_still_loads(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    """説明が必須になる前の説明なしの KB も一覧・詳細で読め、説明を入れて保存できる（#521）。"""
    detail = client.post(
        "/api/knowledge-bases", json={"name": "旧規程", "description": "仮"}
    ).json()["data"]
    fake_oracle.knowledge_bases[detail["id"]] = fake_oracle.knowledge_bases[
        detail["id"]
    ].model_copy(update={"description": None})

    listed = client.get("/api/knowledge-bases").json()["data"]["items"]
    assert next(item for item in listed if item["id"] == detail["id"])["description"] is None
    assert client.get(f"/api/knowledge-bases/{detail['id']}").json()["data"]["description"] is None

    updated = client.patch(
        f"/api/knowledge-bases/{detail['id']}", json={"description": "  旧の就業規則  "}
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["description"] == "旧の就業規則"


def test_knowledge_base_graph_endpoint(fake_oracle: FakeKnowledgeBaseOracle) -> None:
    """文書構造グラフ endpoint が空/データ/404 を返す。"""
    detail = client.post(
        "/api/knowledge-bases", json={"name": "社内規程", "description": "説明"}
    ).json()["data"]
    kb_id = detail["id"]

    empty = client.get(f"/api/knowledge-bases/{kb_id}/graph")
    assert empty.status_code == 200
    assert empty.json()["data"]["status"] == "empty"
    assert empty.json()["data"]["nodes"] == []

    fake_oracle.subgraphs[kb_id] = (
        [
            {"id": "e1", "name": "就業規則", "type": "concept", "confidence": 0.9},
            {"id": "e2", "name": "有給休暇", "type": "concept", "confidence": 0.8},
        ],
        [{"id": "r1", "source": "e1", "target": "e2", "type": "relates_to", "confidence": 0.7}],
    )
    resp = client.get(f"/api/knowledge-bases/{kb_id}/graph")
    data = resp.json()["data"]
    assert data["status"] == "ok"
    assert [node["name"] for node in data["nodes"]] == ["就業規則", "有給休暇"]
    assert data["edges"][0]["source"] == "e1"
    assert data["edges"][0]["target"] == "e2"

    assert client.get("/api/knowledge-bases/does-not-exist/graph").status_code == 404


def test_update_and_archive_knowledge_base(fake_oracle: FakeKnowledgeBaseOracle) -> None:
    """ナレッジベースの更新とアーカイブができる。"""
    detail = client.post(
        "/api/knowledge-bases", json={"name": "FAQ", "description": "説明"}
    ).json()["data"]

    update_resp = client.patch(
        f"/api/knowledge-bases/{detail['id']}",
        json={"name": "製品 FAQ", "description": "製品の問い合わせ"},
    )

    assert update_resp.status_code == 200
    assert update_resp.json()["data"]["name"] == "製品 FAQ"

    archive_resp = client.post(f"/api/knowledge-bases/{detail['id']}/archive")
    assert archive_resp.status_code == 200
    assert archive_resp.json()["data"]["status"] == "ARCHIVED"


def test_default_knowledge_base_cannot_be_archived(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    """DEFAULT のアーカイブ要求は競合として拒否する。"""
    detail = client.post(
        "/api/knowledge-bases", json={"name": "一時名", "description": "説明"}
    ).json()["data"]
    kb_id = detail["id"]
    fake_oracle.knowledge_bases[kb_id] = fake_oracle.knowledge_bases[kb_id].model_copy(
        update={"name": "DEFAULT"}
    )

    response = client.post(f"/api/knowledge-bases/{kb_id}/archive")

    assert response.status_code == 409
    assert response.json()["error_messages"] == ["DEFAULT ナレッジベースはアーカイブできません。"]


def test_assign_and_list_knowledge_base_documents(fake_oracle: FakeKnowledgeBaseOracle) -> None:
    """既存文書をナレッジベースへ追加し、KB 文書一覧で確認できる。"""
    detail = client.post(
        "/api/knowledge-bases", json={"name": "社内規程", "description": "説明"}
    ).json()["data"]

    assign_resp = client.post(
        f"/api/knowledge-bases/{detail['id']}/documents",
        json={"document_ids": ["doc-1"]},
    )

    assert assign_resp.status_code == 200
    assert assign_resp.json()["data"]["document_count"] == 1

    # KB 詳細の文書一覧は、文書一覧 API を KB で絞り込んで取得する。
    docs_resp = client.get("/api/documents", params={"knowledge_base_id": detail["id"]})
    assert docs_resp.status_code == 200
    docs_page = docs_resp.json()["data"]
    assert docs_page["total"] == 1
    assert docs_page["items"][0]["id"] == "doc-1"


def test_list_documents_accepts_profile_knowledge_bases(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    """文書一覧は、プロファイルの KB の上限の数を、カンマ区切りでまとめて受ける（#2749）。"""
    detail = client.post(
        "/api/knowledge-bases", json={"name": "KB A", "description": "説明"}
    ).json()["data"]
    client.post(f"/api/knowledge-bases/{detail['id']}/documents", json={"document_ids": ["doc-1"]})
    others = [uuid4().hex for _ in range(MAX_SEARCH_ANSWER_PROFILE_KNOWLEDGE_BASES - 1)]

    resp = client.get(
        "/api/documents", params={"knowledge_base_id": ",".join([*others, detail["id"]])}
    )

    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()["data"]["items"]] == ["doc-1"]


def test_document_knowledge_base_replace_endpoint(fake_oracle: FakeKnowledgeBaseOracle) -> None:
    """文書側 endpoint から所属ナレッジベースを置換できる。"""
    detail = client.post(
        "/api/knowledge-bases", json={"name": "社内規程", "description": "説明"}
    ).json()["data"]

    replace_resp = client.put(
        "/api/documents/doc-1/knowledge-bases",
        json={"knowledge_base_ids": [detail["id"]]},
    )

    assert replace_resp.status_code == 200
    assert replace_resp.json()["data"] == [{"id": detail["id"], "name": "社内規程"}]

    list_resp = client.get("/api/documents/doc-1/knowledge-bases")
    assert list_resp.status_code == 200
    assert list_resp.json()["data"] == [{"id": detail["id"], "name": "社内規程"}]


def test_archived_knowledge_base_rejects_assignment(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    """アーカイブ済みナレッジベースには文書を追加できない。"""
    detail = client.post(
        "/api/knowledge-bases", json={"name": "旧規程", "description": "説明"}
    ).json()["data"]
    assert client.post(f"/api/knowledge-bases/{detail['id']}/archive").status_code == 200

    resp = client.post(
        f"/api/knowledge-bases/{detail['id']}/documents",
        json={"document_ids": ["doc-1"]},
    )

    assert resp.status_code == 409
    assert resp.json()["error_messages"] == ["アーカイブ済みナレッジベースは変更できません。"]


def test_create_knowledge_base_rejects_adapter_config(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    """KB は所属だけを持つ。作成で adapter_config を指定すると 422 で拒否し、保存しない(#302)。"""
    resp = client.post(
        "/api/knowledge-bases",
        json={
            "name": "Markdown FAQ",
            "description": "説明",
            "adapter_config": {
                "ingestion": {"chunking_strategy": "markdown_heading", "chunk_size": 1200},
            },
        },
    )

    assert resp.status_code == 422
    assert resp.json()["error_messages"] == [knowledge_bases_route.ADAPTER_CONFIG_REJECTED_MESSAGE]
    assert fake_oracle.knowledge_bases == {}


@pytest.mark.parametrize(
    "adapter_config",
    [
        {"ingestion": {"chunking_strategy": "page_level"}},
        {"ingestion": {"chunking_strategy": "does_not_exist"}},
        {},
        None,
    ],
)
def test_patch_knowledge_base_rejects_adapter_config(
    fake_oracle: FakeKnowledgeBaseOracle,
    adapter_config: object,
) -> None:
    """PATCH で adapter_config を指定すると(null・空でも)422 にし、保存値を変えない(#302)。"""
    created = client.post(
        "/api/knowledge-bases",
        json={
            "name": "Legacy KB",
            "description": "説明",
            "retrieval_config": {"ingestion": {"chunking_strategy": "structure_aware"}},
        },
    ).json()["data"]

    resp = client.patch(
        f"/api/knowledge-bases/{created['id']}",
        json={"description": "更新", "adapter_config": adapter_config},
    )

    assert resp.status_code == 422
    reloaded = client.get(f"/api/knowledge-bases/{created['id']}").json()["data"]
    assert reloaded["description"] == "説明"
    assert reloaded["adapter_config"]["ingestion"]["chunking_strategy"] == "structure_aware"


def test_knowledge_base_legacy_query_config_is_flagged(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    """既存 retrieval_config に残る query は読めるが legacy ignored として返る。"""
    created = client.post(
        "/api/knowledge-bases",
        json={
            "name": "Legacy KB",
            "description": "説明",
            "retrieval_config": {"query": {"rerank_enabled": False}},
        },
    ).json()["data"]

    assert created["adapter_config"]["query"]["rerank_enabled"] is False
    assert created["legacy_query_config_ignored"] is True

    # 名前・説明の更新では legacy の保存値を書き換えない(読み取りのみ)。
    patched = client.patch(
        f"/api/knowledge-bases/{created['id']}",
        json={"description": "更新後"},
    ).json()["data"]

    assert patched["description"] == "更新後"
    assert patched["adapter_config"]["query"]["rerank_enabled"] is False


def test_list_knowledge_bases_filters_by_ids_including_archived(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    """ids を指定すると、その ID の KB だけを(status 省略時はアーカイブ済みも)返す(#302)。"""
    first = client.post(
        "/api/knowledge-bases", json={"name": "社内規程", "description": "説明"}
    ).json()["data"]
    second = client.post(
        "/api/knowledge-bases", json={"name": "製品 FAQ", "description": "説明"}
    ).json()["data"]
    client.post("/api/knowledge-bases", json={"name": "設計資料", "description": "説明"})
    assert client.post(f"/api/knowledge-bases/{second['id']}/archive").status_code == 200

    resp = client.get(
        "/api/knowledge-bases",
        params=[("ids", first["id"]), ("ids", second["id"]), ("ids", "kb-missing")],
    )

    assert resp.status_code == 200
    page = resp.json()["data"]
    assert page["total"] == 2
    assert {(item["id"], item["status"]) for item in page["items"]} == {
        (first["id"], "ACTIVE"),
        (second["id"], "ARCHIVED"),
    }

    active_only = client.get(
        "/api/knowledge-bases",
        params=[("ids", first["id"]), ("ids", second["id"]), ("status", "ACTIVE")],
    ).json()["data"]
    assert [item["id"] for item in active_only["items"]] == [first["id"]]


def test_create_knowledge_base_with_duplicate_name_returns_409(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    """同じ名前の KB があると 500 ではなく 409 と日本語の理由を返す（#282）。"""
    assert (
        client.post(
            "/api/knowledge-bases", json={"name": "社内規程", "description": "説明"}
        ).status_code
        == 200
    )

    resp = client.post("/api/knowledge-bases", json={"name": "社内規程", "description": "説明"})

    assert resp.status_code == 409
    assert resp.json()["error_messages"] == [
        "同じ名前のナレッジベース（アーカイブ済みを含む）がすでにあります。別の名前を指定してください。"
    ]


def test_rename_to_duplicate_name_returns_409(
    fake_oracle: FakeKnowledgeBaseOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改名先の名前が使われているときも 409 にする。"""
    created = client.post(
        "/api/knowledge-bases", json={"name": "設計資料", "description": "説明"}
    ).json()["data"]

    async def conflicting_update(*_args: object, **_kwargs: object) -> KnowledgeBaseDetail:
        raise KnowledgeBaseNameConflictError()

    monkeypatch.setattr(fake_oracle, "update_knowledge_base", conflicting_update)

    resp = client.patch(f"/api/knowledge-bases/{created['id']}", json={"name": "社内規程"})

    assert resp.status_code == 409


def test_mutation_responses_include_refreshed_counts(
    fake_oracle: FakeKnowledgeBaseOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """変更系 API の応答は、集計列を 0 で返す Oracle 操作の結果ではなく取り直した詳細を返す。"""
    created = client.post(
        "/api/knowledge-bases", json={"name": "社内規程", "description": "説明"}
    ).json()["data"]
    stored = fake_oracle.knowledge_bases[created["id"]]
    fake_oracle.knowledge_bases[created["id"]] = stored.model_copy(
        update={"document_count": 5, "indexed_document_count": 4, "error_document_count": 1}
    )

    async def zero_count_update(knowledge_base_id: str, **_kwargs: object) -> KnowledgeBaseDetail:
        # Oracle の変更操作は KB の行だけを読むので集計列は 0 になる。
        return fake_oracle.knowledge_bases[knowledge_base_id].model_copy(
            update={
                "description": "更新後",
                "document_count": 0,
                "indexed_document_count": 0,
                "error_document_count": 0,
            }
        )

    monkeypatch.setattr(fake_oracle, "update_knowledge_base", zero_count_update)

    resp = client.patch(f"/api/knowledge-bases/{created['id']}", json={"description": "更新後"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["document_count"] == 5
    assert data["indexed_document_count"] == 4
    assert data["error_document_count"] == 1


def test_remove_default_only_membership_returns_409(
    fake_oracle: FakeKnowledgeBaseOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEFAULT にだけ所属する文書を外す操作は 409 と理由を返す。"""
    created = client.post(
        "/api/knowledge-bases", json={"name": "社内規程", "description": "説明"}
    ).json()["data"]

    async def reject_remove(*_args: object, **_kwargs: object) -> KnowledgeBaseDetail:
        raise ValueError("DEFAULT にだけ所属する文書は外せません。")

    monkeypatch.setattr(fake_oracle, "remove_document_from_knowledge_base", reject_remove)

    resp = client.delete(f"/api/knowledge-bases/{created['id']}/documents/doc-1")

    assert resp.status_code == 409
    assert resp.json()["error_messages"] == ["DEFAULT にだけ所属する文書は外せません。"]


# ---- ナレッジベースごとの項目抽出の定義（#548）----


@pytest.fixture
def _field_schema_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fields_mod.FIELD_SCHEMA_FILE_ENV, str(tmp_path / "extraction-fields.json"))
    fields_mod.save_field_schema([FieldDefinition(name="請求書番号")])


def _new_knowledge_base(name: str = "契約書") -> str:
    response = client.post("/api/knowledge-bases", json={"name": name, "description": "説明"})
    return str(response.json()["data"]["id"])


@pytest.mark.usefixtures("_field_schema_file")
def test_knowledge_base_extraction_fields_default_then_own_then_reset(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    """KB に定義が無ければ全体の既定を返し、保存後は KB の定義、null で既定に戻す。"""
    kb_id = _new_knowledge_base()
    url = f"/api/knowledge-bases/{kb_id}/extraction-fields"

    inherited = client.get(url).json()["data"]
    assert inherited["inherits_default"] is True
    assert [field["name"] for field in inherited["fields"]] == ["請求書番号"]

    fields = [
        {"name": " 契約日 ", "description": "締結日", "value_type": "date"},
        {"name": "金額", "description": "", "value_type": "number"},
    ]
    saved = client.put(url, json={"fields": fields})
    assert saved.status_code == 200
    assert saved.json()["data"]["inherits_default"] is False
    assert [field["name"] for field in saved.json()["data"]["fields"]] == ["契約日", "金額"]
    assert [field.name for field in fake_oracle.extraction_fields[kb_id]] == ["契約日", "金額"]
    assert client.get(url).json()["data"]["fields"][0]["value_type"] == "date"

    # 空の定義は「この KB では抽出しない」で、既定には戻さない。
    empty = client.put(url, json={"fields": []}).json()["data"]
    assert empty == {"inherits_default": False, "fields": []}

    reset = client.put(url, json={"fields": None}).json()["data"]
    assert reset["inherits_default"] is True
    assert kb_id not in fake_oracle.extraction_fields


@pytest.mark.usefixtures("_field_schema_file")
def test_knowledge_base_extraction_fields_reject_duplicates_and_over_limit(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    kb_id = _new_knowledge_base()
    url = f"/api/knowledge-bases/{kb_id}/extraction-fields"

    duplicate = client.put(url, json={"fields": [{"name": "契約日"}, {"name": "契約日 "}]})
    assert duplicate.status_code == 422
    assert "重複" in duplicate.json()["error_messages"][0]
    too_many = client.put(url, json={"fields": [{"name": f"項目{i}"} for i in range(51)]})
    assert too_many.status_code == 422
    assert kb_id not in fake_oracle.extraction_fields


@pytest.mark.usefixtures("_field_schema_file")
def test_knowledge_base_extraction_fields_missing_and_archived(
    fake_oracle: FakeKnowledgeBaseOracle,
) -> None:
    assert client.get("/api/knowledge-bases/kb-missing/extraction-fields").status_code == 404
    missing = client.put("/api/knowledge-bases/kb-missing/extraction-fields", json={"fields": []})
    assert missing.status_code == 404

    kb_id = _new_knowledge_base()
    client.post(f"/api/knowledge-bases/{kb_id}/archive")
    archived = client.put(f"/api/knowledge-bases/{kb_id}/extraction-fields", json={"fields": []})
    assert archived.status_code == 409
