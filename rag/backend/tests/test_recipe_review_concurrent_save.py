"""抽出確認の修正の並行保存で、先の修正を黙って失わないテスト(#2616)。

`_apply_recipe_review_text_edits`(PATCH review-edits と、修正を伴う承認の共通の保存)を、
メモリの Oracle fake で確かめる。fake はレシピごとの lock で実 Oracle の行ロック
(``SELECT ... FOR UPDATE``)に当たる直列化をし、lock の中の読みと書きの間で制御を渡す。
barrier で 2 つの保存の事前の読み込みをそろえてから同時に保存する。実 Oracle の並行は
tests/test_recipe_review_save_oracle.py で確かめる。
"""

from __future__ import annotations

import asyncio
import copy
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.api.routes import documents as documents_route
from app.api.routes.documents import _apply_recipe_review_text_edits
from app.clients.oracle import DocumentReviewEditConflictError, ReviewedExtractionWrite
from app.schemas.document import (
    DocumentApproveRequest,
    DocumentDetail,
    DocumentElementTextEdit,
    DocumentReviewEditsRequest,
    DocumentTableCellTextEdit,
    FileStatus,
)
from app.schemas.extraction import (
    DocumentElement,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)

_DOCUMENT_ID = "doc-1"
_RECIPE_ID = "recipe-1"
_OTHER_RECIPE_ID = "recipe-2"
_SHARED_ID = "er_shared"
_OTHER_ID = "er_other"
_CONFLICT = "読み込んだ後に、ほかの操作で同じ箇所が保存されました。"


def _extraction() -> dict[str, object]:
    """合成の要素 A / B と 2 セルの表を持つ抽出成果。"""
    return StructuredExtraction(
        raw_text="A0\nB0",
        elements=[
            DocumentElement(kind="text", text="A0", order=0, element_id="el-a", page_number=1),
            DocumentElement(kind="text", text="B0", order=1, element_id="el-b", page_number=1),
            DocumentElement(
                kind="table",
                text="| X0 | Y0 |",
                order=2,
                element_id="el-t",
                page_number=1,
                metadata={"table_id": "tbl-1"},
            ),
        ],
        tables=[
            ExtractionTable(
                table_id="tbl-1",
                element_id="el-t",
                page_number=1,
                cells=[
                    ExtractionTableCell(row=0, col=0, text="X0"),
                    ExtractionTableCell(row=0, col=1, text="Y0"),
                ],
            )
        ],
    ).to_document_payload()


class _ReviewStore:
    """レシピ・抽出成果をメモリに持つ Oracle fake。"""

    def __init__(self) -> None:
        self.recipes: dict[str, dict[str, object]] = {
            recipe_id: {
                "document_id": _DOCUMENT_ID,
                "recipe_id": recipe_id,
                "status": FileStatus.REVIEW.value,
                "config_revision": 1,
                "processing_config": {},
                "active_extraction_recipe_id": artifact_id,
            }
            for recipe_id, artifact_id in ((_RECIPE_ID, _SHARED_ID), (_OTHER_RECIPE_ID, _OTHER_ID))
        }
        self.artifacts: dict[str, dict[str, object]] = {
            artifact_id: {
                "extraction_json": _extraction(),
                "recipe_subset": {},
                "status": "materialized",
            }
            for artifact_id in (_SHARED_ID, _OTHER_ID)
        }
        self.active_job = False
        self.read_barrier: asyncio.Barrier | None = None
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get_document(self, document_id: str) -> DocumentDetail:
        # 2 つの保存が、どちらも書く前に事前の読み込みを終えるまで待ち合わせる。
        if self.read_barrier is not None:
            await self.read_barrier.wait()
        return DocumentDetail(
            id=document_id,
            file_name="synthetic.pdf",
            status=FileStatus.REVIEW,
            content_sha256="a" * 64,
            uploaded_at=datetime.now(UTC),
        )

    async def save_document_recipe_review_extraction(
        self,
        *,
        document_id: str,
        recipe_id: str,
        build: Callable[[Mapping[str, object], Mapping[str, object]], ReviewedExtractionWrite],
    ) -> str:
        # 実 Oracle のレシピの行ロック(SELECT ... FOR UPDATE)に当たる。
        async with self._locks[recipe_id]:
            row = self.recipes.get(recipe_id)
            if row is None or row["document_id"] != document_id:
                raise KeyError("レシピが見つかりません。")
            if row["status"] != FileStatus.REVIEW.value:
                raise DocumentReviewEditConflictError("確認待ちのレシピのみ修正できます。")
            if self.active_job:
                raise DocumentReviewEditConflictError("このレシピは処理中または待機中です。")
            artifact = copy.deepcopy(self.artifacts[str(row["active_extraction_recipe_id"])])
            # 読みと書きの間で、もう一方の保存へ制御を渡す。
            await asyncio.sleep(0)
            write = build(dict(row), artifact)
            self.artifacts[write.extraction_recipe_id] = {
                "extraction_json": copy.deepcopy(dict(write.extraction)),
                "recipe_subset": write.recipe_subset,
                "status": write.status,
            }
            row["active_extraction_recipe_id"] = write.extraction_recipe_id
            return write.extraction_recipe_id

    def texts(self, recipe_id: str = _RECIPE_ID) -> dict[str, str]:
        """レシピの今の抽出成果の、要素と表セルの文。"""
        artifact_id = str(self.recipes[recipe_id]["active_extraction_recipe_id"])
        extraction = StructuredExtraction.model_validate(
            self.artifacts[artifact_id]["extraction_json"]
        )
        texts = {str(element.element_id): element.text for element in extraction.elements}
        for table in extraction.tables:
            for cell in table.cells:
                texts[f"{table.table_id}:{cell.row},{cell.col}"] = cell.text
        return texts


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _ReviewStore:
    fake = _ReviewStore()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    return fake


def _element(element_id: str, base_text: str, text: str) -> DocumentReviewEditsRequest:
    return DocumentReviewEditsRequest(
        element_edits=[
            DocumentElementTextEdit(element_id=element_id, text=text, base_text=base_text)
        ]
    )


def _cell(col: int, base_text: str, text: str) -> DocumentReviewEditsRequest:
    return DocumentReviewEditsRequest(
        table_cell_edits=[
            DocumentTableCellTextEdit(
                table_id="tbl-1", row=0, col=col, text=text, base_text=base_text
            )
        ]
    )


async def _save_together(
    store: _ReviewStore, *edits: DocumentReviewEditsRequest
) -> list[BaseException | None]:
    """事前の読み込みを barrier でそろえてから同時に保存し、それぞれの例外(成功は None)を返す。"""
    store.read_barrier = asyncio.Barrier(len(edits))
    results = await asyncio.gather(
        *(_apply_recipe_review_text_edits(_DOCUMENT_ID, _RECIPE_ID, edit) for edit in edits),
        return_exceptions=True,
    )
    store.read_barrier = None
    return [result if isinstance(result, BaseException) else None for result in results]


def _assert_conflict(error: BaseException | None) -> None:
    assert isinstance(error, HTTPException)
    assert error.status_code == 409
    assert str(error.detail).startswith(_CONFLICT)


async def test_sequential_saves_keep_both_edits_and_leave_other_artifacts(
    store: _ReviewStore,
) -> None:
    """順に保存した別要素の修正は両方残り、共有の元の成果・他レシピの成果は変えない。"""
    await _apply_recipe_review_text_edits(_DOCUMENT_ID, _RECIPE_ID, _element("el-a", "A0", "A1"))
    await _apply_recipe_review_text_edits(_DOCUMENT_ID, _RECIPE_ID, _element("el-b", "B0", "B1"))

    texts = store.texts()
    assert (texts["el-a"], texts["el-b"]) == ("A1", "B1")
    reviewed_id = store.recipes[_RECIPE_ID]["active_extraction_recipe_id"]
    assert reviewed_id != _SHARED_ID
    # 2 回目も同じレシピ固有の成果へ書き、元の抽出成果は copy-on-write で残る。
    assert store.artifacts[_SHARED_ID]["extraction_json"] == _extraction()
    assert store.artifacts[_OTHER_ID]["extraction_json"] == _extraction()
    assert store.texts(_OTHER_RECIPE_ID)["el-a"] == "A0"


async def test_concurrent_saves_to_different_elements_keep_both(store: _ReviewStore) -> None:
    """同じ版を読んだ 2 つの保存が別の要素を同時に直しても、両方の修正が残る(A2/B2)。"""
    await _apply_recipe_review_text_edits(_DOCUMENT_ID, _RECIPE_ID, _element("el-a", "A0", "A1"))
    await _apply_recipe_review_text_edits(_DOCUMENT_ID, _RECIPE_ID, _element("el-b", "B0", "B1"))

    errors = await _save_together(store, _element("el-a", "A1", "A2"), _element("el-b", "B1", "B2"))

    assert errors == [None, None]
    texts = store.texts()
    assert (texts["el-a"], texts["el-b"]) == ("A2", "B2")
    assert store.artifacts[_SHARED_ID]["extraction_json"] == _extraction()


async def test_concurrent_saves_to_same_element_conflict(store: _ReviewStore) -> None:
    """同じ要素を同じ版から別の文へ直す 2 つの保存は、片方だけ成功し他方は 409。"""
    errors = await _save_together(
        store, _element("el-a", "A0", "A-first"), _element("el-a", "A0", "A-second")
    )

    succeeded = [index for index, error in enumerate(errors) if error is None]
    assert len(succeeded) == 1
    _assert_conflict(errors[1 - succeeded[0]])
    assert store.texts()["el-a"] == ("A-first", "A-second")[succeeded[0]]
    assert store.texts()["el-b"] == "B0"


async def test_concurrent_saves_with_same_text_both_succeed(store: _ReviewStore) -> None:
    """同じ要素を同じ文へ直す保存どうしは競合にしない。"""
    errors = await _save_together(store, _element("el-a", "A0", "A1"), _element("el-a", "A0", "A1"))

    assert errors == [None, None]
    assert store.texts()["el-a"] == "A1"


async def test_concurrent_table_cell_saves(store: _ReviewStore) -> None:
    """別の表セルの同時の修正は両方残り、同じセルを別の文へ直す保存は 409。"""
    errors = await _save_together(store, _cell(0, "X0", "X1"), _cell(1, "Y0", "Y1"))

    assert errors == [None, None]
    texts = store.texts()
    assert (texts["tbl-1:0,0"], texts["tbl-1:0,1"]) == ("X1", "Y1")
    # 表の要素の文も、最新のセルから作り直す。
    assert "X1" in texts["el-t"] and "Y1" in texts["el-t"]

    errors = await _save_together(store, _cell(0, "X1", "X2"), _cell(0, "X1", "X3"))

    assert sum(error is None for error in errors) == 1
    _assert_conflict(next(error for error in errors if error is not None))


async def test_save_from_stale_page_is_rejected_without_writing(store: _ReviewStore) -> None:
    """読み込んだ後にほかの保存で変わった要素を、古い画面から上書きしない。"""
    await _apply_recipe_review_text_edits(_DOCUMENT_ID, _RECIPE_ID, _element("el-a", "A0", "A1"))
    before = copy.deepcopy(store.artifacts)

    with pytest.raises(HTTPException) as exc_info:
        await _apply_recipe_review_text_edits(
            _DOCUMENT_ID, _RECIPE_ID, _element("el-a", "A0", "A-stale")
        )

    _assert_conflict(exc_info.value)
    assert store.artifacts == before


async def test_save_after_approval_is_rejected(store: _ReviewStore) -> None:
    """承認で次の工程の job を投入した後の保存は 409(承認した内容を後から変えない)。"""
    store.active_job = True

    with pytest.raises(HTTPException) as exc_info:
        await _apply_recipe_review_text_edits(
            _DOCUMENT_ID, _RECIPE_ID, _element("el-a", "A0", "A1")
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "このレシピは処理中または待機中です。"
    assert store.texts()["el-a"] == "A0"


async def test_concurrent_save_and_approval_edits_keep_both(store: _ReviewStore) -> None:
    """下書きの保存と、修正を伴う承認の保存は同じ直列化で、別要素の修正を両方残す。"""
    approve = DocumentApproveRequest(
        element_edits=[DocumentElementTextEdit(element_id="el-b", text="B1", base_text="B0")]
    )

    errors = await _save_together(store, _element("el-a", "A0", "A1"), approve)

    assert errors == [None, None]
    texts = store.texts()
    assert (texts["el-a"], texts["el-b"]) == ("A1", "B1")
