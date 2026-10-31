"""文書の分類の一覧（大分類 → 中分類 → 小分類）の管理のテスト（#2131）。"""

from __future__ import annotations

from contextlib import suppress
from typing import Any
from uuid import uuid4

import pytest

from app.api.routes import document_categories as categories_route
from app.clients.document_category_store import (
    CategoryConflictError,
    CategoryDepthError,
    CategoryInUseError,
    CategoryNode,
    CategoryNotFoundError,
    DocumentCategoryStore,
    build_category_tree,
)
from app.clients.oracle import OracleClient
from app.main import app
from app.schemas.document import DocumentClassification
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


def _row(category_id: str, name: str, level: int, parent: str | None, order: int) -> dict[str, Any]:
    return {
        "category_id": category_id,
        "name": name,
        "category_level": level,
        "parent_id": parent,
        "sort_order": order,
    }


def test_build_category_tree_orders_and_counts_documents_by_label() -> None:
    rows = [
        _row("b", "20_業務B", 1, None, 2),
        _row("a", "10_業務A", 1, None, 1),
        _row("a2", "種別Y", 2, "a", 2),
        _row("a1", "種別X", 2, "a", 1),
        _row("a1s", "項目1", 3, "a1", 1),
    ]
    # 文書の値は番号の接頭辞の有無が違っても同じ分類として数える（検索の絞り込みと同じ）。
    usage = [
        ("業務A", "種別X", "項目1", 2),
        ("10_業務A", "種別X", None, 1),
        ("10_業務A", None, None, 4),
        ("業務C", None, None, 9),
    ]

    tree = build_category_tree(rows, usage)

    assert [node.name for node in tree] == ["10_業務A", "20_業務B"]
    large = tree[0]
    assert [node.name for node in large.children] == ["種別X", "種別Y"]
    assert large.document_count == 7
    assert large.children[0].document_count == 3
    assert large.children[0].children[0].document_count == 2
    assert large.children[1].document_count == 0
    assert tree[1].document_count == 0


class FakeStore:
    """route の振る舞いを確かめる store（呼び出しを記録し、指定の例外を投げる）。"""

    error: Exception | None = None
    calls: list[tuple[str, Any]] = []

    def __init__(self, *_: object) -> None:
        pass

    async def list_tree(self) -> list[CategoryNode]:
        return [CategoryNode(id="a", name="業務A", level=1, parent_id=None, sort_order=1)]

    async def _call(self, name: str, *args: Any) -> int:
        FakeStore.calls.append((name, args))
        if FakeStore.error is not None:
            raise FakeStore.error
        return 3

    async def create(self, *, parent_id: str | None, name: object) -> str:
        await self._call("create", parent_id, name)
        return "new"

    async def rename(self, category_id: str, name: object) -> int:
        return await self._call("rename", category_id, name)

    async def move(self, category_id: str, direction: int) -> None:
        await self._call("move", category_id, direction)

    async def delete(self, category_id: str) -> None:
        await self._call("delete", category_id)


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> type[FakeStore]:
    FakeStore.error = None
    FakeStore.calls = []
    monkeypatch.setattr(categories_route, "DocumentCategoryStore", FakeStore)
    return FakeStore


def test_category_routes_call_the_store(fake_store: type[FakeStore]) -> None:
    listed = client.get("/api/document-categories")
    created = client.post("/api/document-categories", json={"parent_id": "a", "name": "種別X"})
    renamed = client.patch("/api/document-categories/a", json={"name": "業務A2"})
    moved = client.post("/api/document-categories/a/move", json={"direction": "down"})
    deleted = client.delete("/api/document-categories/a")

    assert listed.status_code == 200
    assert listed.json()["data"][0]["name"] == "業務A"
    assert created.status_code == 201
    assert renamed.json()["data"] == {"updated_document_count": 3}
    assert moved.status_code == 200
    assert deleted.status_code == 200
    assert fake_store.calls == [
        ("create", ("a", "種別X")),
        ("rename", ("a", "業務A2")),
        ("move", ("a", 1)),
        ("delete", ("a",)),
    ]
    # 動かす向きは up / down だけ。
    assert (
        client.post("/api/document-categories/a/move", json={"direction": "left"}).status_code
        == 422
    )


@pytest.mark.parametrize(
    ("error", "status", "message"),
    [
        (CategoryNotFoundError("x"), 404, "分類が見つかりません"),
        (CategoryConflictError("x"), 409, "同じ名前の分類があります"),
        (CategoryDepthError("x"), 422, "小分類の下には分類を作れません"),
        (CategoryInUseError(2, 5), 409, "下に 2 件の分類があります。5 件の文書が使っています"),
    ],
    ids=["not-found", "conflict", "depth", "in-use"],
)
def test_category_errors_are_explained(
    fake_store: type[FakeStore], error: Exception, status: int, message: str
) -> None:
    fake_store.error = error
    method = (
        client.delete("/api/document-categories/a")
        if isinstance(error, CategoryInUseError)
        else client.post("/api/document-categories", json={"parent_id": "a", "name": "名前"})
    )

    assert method.status_code == status
    assert message in method.json()["error_messages"][0]


@pytest.mark.usefixtures("oracle_db")
async def test_category_master_crud_on_real_oracle() -> None:
    """実 Oracle AI Database で、作成・重複・段の上限・名前の変更（文書の値も）・並べ替え・
    削除を確かめる。"""
    store = DocumentCategoryStore()
    suffix = uuid4().hex[:8]
    large = await store.create(parent_id=None, name=f"10_業務A{suffix}")
    other = await store.create(parent_id=None, name=f"20_業務B{suffix}")
    middle = await store.create(parent_id=large, name="種別X")
    small = await store.create(parent_id=middle, name="項目1")
    sibling: str | None = None
    try:
        # 同じ親の下で、番号の接頭辞を除いた名前が同じなら作れない。別の親の下なら作れる。
        with pytest.raises(CategoryConflictError):
            await store.create(parent_id=None, name=f"業務A{suffix}")
        sibling = await store.create(parent_id=other, name="種別X")
        assert sibling is not None
        with pytest.raises(CategoryDepthError):
            await store.create(parent_id=small, name="さらに下")

        oracle = OracleClient()
        document = await oracle.create_document(
            file_name="category.pdf",
            object_storage_path="local://category.pdf",
            content_type="application/pdf",
        )
        await oracle.save_document_classification(
            document.id,
            DocumentClassification(
                large_category=f"業務A{suffix}", middle_category="種別X", small_category="項目1"
            ),
        )

        # 使われている分類・子を持つ分類は消せない。
        with pytest.raises(CategoryInUseError) as in_use:
            await store.delete(small)
        assert (in_use.value.child_count, in_use.value.document_count) == (0, 1)
        with pytest.raises(CategoryInUseError) as has_child:
            await store.delete(large)
        assert has_child.value.child_count == 1

        # 名前を変えると、その分類を使っている文書の値も変わる
        # （別の大分類の同名の中分類は数えない）。
        assert await store.rename(middle, "種別Z") == 1
        assert await store.rename(sibling, "種別W") == 0
        saved = await oracle.get_document(document.id)
        assert saved is not None and saved.classification is not None
        assert saved.classification.middle_category == "種別Z"
        assert saved.classification.small_category == "項目1"

        tree = {node.id: node for node in await store.list_tree()}
        assert tree[large].document_count == 1
        assert tree[large].children[0].children[0].document_count == 1

        # 並べ替え: 大分類の 2 つを入れ替える。
        ours = [node.id for node in await store.list_tree() if node.id in {large, other}]
        assert ours == [large, other]
        await store.move(other, -1)
        ours = [node.id for node in await store.list_tree() if node.id in {large, other}]
        assert ours == [other, large]

        await oracle.save_document_classification(document.id, DocumentClassification())
        await store.delete(small)
        await store.delete(sibling)
    finally:
        for category_id in (small, middle, sibling, large, other):
            if category_id is None:
                continue
            with suppress(CategoryNotFoundError, CategoryInUseError):
                await store.delete(category_id)
