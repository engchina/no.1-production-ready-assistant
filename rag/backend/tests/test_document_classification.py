"""文書の分類・有効期間と、検索の分類フィルタ(rag_poc の ClassificationFilter)のテスト。"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.api.routes import documents as documents_route
from app.clients.oracle import OracleClient, _classification_where, _oracle_retrieval_where
from app.config import settings_today
from app.main import app
from app.rag.classification_normalization import normalize_document_classifications
from app.schemas.classification import category_label, normalize_category_value
from app.schemas.document import (
    DocumentClassification,
    DocumentDetail,
    FileStatus,
)
from app.schemas.search import normalize_search_filters
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


def test_classification_blank_values_become_none() -> None:
    classification = DocumentClassification(large_category=" 経理 ", middle_category="")

    assert classification.large_category == "経理"
    assert classification.middle_category is None
    assert not classification.is_empty()
    assert DocumentClassification(small_category=" ").is_empty()


def test_normalize_category_value_unifies_width_and_whitespace() -> None:
    assert normalize_category_value("　ＡＢＣ　業務　 A ") == "ABC 業務 A"
    assert normalize_category_value("ｾｲｻﾝ") == "セイサン"
    assert normalize_category_value("１０_業務A") == "10_業務A"
    assert normalize_category_value(" 　 ") is None
    assert normalize_category_value(None) is None


def test_category_label_strips_code_prefix_like_rag_engine() -> None:
    assert category_label("10_業務A") == "業務A"
    assert category_label("１０＿業務A") == "業務A"  # 全角の数字・下線も NFKC で接頭辞になる
    assert category_label("業務A") == "業務A"
    assert category_label("10_") == "10_"  # 名前が無いときは外さない
    assert category_label("A10_業務") == "A10_業務"
    assert category_label(None) == ""


def test_classification_normalizes_category_values_on_save() -> None:
    classification = DocumentClassification(
        large_category="１０_ 業務Ａ ", middle_category="ｿｳｻ  説明書", small_category="　"
    )

    # 番号の接頭辞は保存値に残す(並び順を持つため)。表記だけをそろえる。
    assert classification.large_category == "10_ 業務A"
    assert classification.middle_category == "ソウサ 説明書"
    assert classification.small_category is None


def test_classification_rejects_inverted_effective_period() -> None:
    with pytest.raises(ValidationError, match="終了日"):
        DocumentClassification(effective_from=date(2026, 4, 1), effective_to=date(2026, 4, 1))


def test_normalize_accepts_classification_filters_and_as_of() -> None:
    normalized = normalize_search_filters(
        {"large_category": " 経理 ", "small_category": "", "as_of": "2026-04-01"}
    )

    assert normalized == {"large_category": "経理", "as_of": "2026-04-01"}


def test_normalize_search_filters_normalizes_classification_like_save() -> None:
    normalized = normalize_search_filters(
        {"large_category": "１０_業務Ａ", "middle_category": "　"}
    )

    assert normalized == {"large_category": "10_業務A"}


def _sql_label(value: str | None) -> str | None:
    """`_category_label_sql` の REGEXP_REPLACE('^[0-9]+_(.)', '\\1') を Python で再現する。"""
    return None if value is None else re.sub(r"^[0-9]+_(.)", r"\1", value)


@pytest.mark.parametrize(
    ("stored", "filter_value", "expected"),
    [
        ("10_業務A", "業務A", True),
        ("業務A", "10_業務A", True),
        ("１０_業務Ａ", "業務A", True),
        ("20_業務A", "10_業務A", True),  # 名前で比べる(rag_engine の業務の判定と同じ)
        (" 業務 A ", "業務　A", True),
        ("10_", "10_", True),
        ("業務A", "業務B", False),
        ("10_業務A", "業務", False),
    ],
)
def test_classification_filter_matches_before_and_after_normalization(
    stored: str, filter_value: str, expected: bool
) -> None:
    """保存の正規化と検索の入力の正規化を通した値が、SQL の比較で同じ結果になる。"""
    saved = DocumentClassification(large_category=stored).large_category
    filters = normalize_search_filters({"large_category": filter_value})
    _, binds = _classification_where(filters)

    assert (_sql_label(saved) == binds["filter_large_category"]) is expected
    # 一括の正規化の前(表記が揃っていない保存値)でも、接頭辞の違いだけなら一致する。
    if normalize_category_value(stored) == stored:
        assert (_sql_label(stored) == binds["filter_large_category"]) is expected


class FakeNormalizationOracle:
    def __init__(self) -> None:
        self.rows: list[tuple[str, object]] = [
            ("doc-1", {"large_category": "１０_経理", "effective_from": "2026-04-01"}),
            ("doc-2", {"large_category": "経理"}),
            ("doc-3", {"small_category": "　", "source": "path_default"}),
            ("doc-4", {"effective_from": "2026-05-01", "effective_to": "2026-04-01"}),
            ("doc-5", {}),
        ]
        self.updates: dict[str, dict[str, object] | None] = {}

    async def list_document_classifications_for_normalization(
        self, *, limit: int, after_document_id: str | None
    ) -> list[tuple[str, object]]:
        rows = [row for row in self.rows if after_document_id is None or row[0] > after_document_id]
        return rows[:limit]

    async def update_document_classification_for_normalization(
        self, document_id: str, classification: dict[str, object] | None
    ) -> None:
        self.updates[document_id] = classification


@pytest.mark.parametrize("apply", [False, True])
async def test_normalize_document_classifications_dry_run_and_apply(apply: bool) -> None:
    fake = FakeNormalizationOracle()

    counts = await normalize_document_classifications(
        apply=apply,
        batch_size=2,
        oracle=fake,  # type: ignore[arg-type]
    )

    assert (counts.scanned, counts.updated, counts.failed) == (5, 2, 2)
    if not apply:
        assert fake.updates == {}
        return
    assert fake.updates == {
        "doc-1": {"large_category": "10_経理", "effective_from": "2026-04-01"},
        # schema に無い key は残し、空になった分類の項目だけを消す。
        "doc-3": {"source": "path_default"},
    }


def test_normalize_rejects_bad_as_of() -> None:
    with pytest.raises(ValueError, match="基準日"):
        normalize_search_filters({"as_of": "2026/04/01"})


def test_retrieval_where_filters_classification_by_label_and_effective_period() -> None:
    sql, binds = _oracle_retrieval_where(
        {"large_category": "10_経理", "middle_category": "精算", "as_of": "2026-04-01"}
    )

    assert (
        "REGEXP_REPLACE(JSON_VALUE(d.classification, '$.large_category'), '^[0-9]+_(.)', '\\1')"
        " = :filter_large_category"
    ) in sql
    assert "JSON_VALUE(d.classification, '$.middle_category'), '^[0-9]+_(.)'" in sql
    assert "small_category" not in sql
    assert "'$.effective_from'), :filter_as_of) <= :filter_as_of" in sql
    assert "'$.effective_to'), '9999-12-31') > :filter_as_of" in sql
    # 番号の接頭辞を外した名前で比べる(rag_engine の _category_label と同じ)。
    assert binds["filter_large_category"] == "経理"
    assert binds["filter_middle_category"] == "精算"
    assert binds["filter_as_of"] == "2026-04-01"


def test_retrieval_where_always_applies_effective_period_with_today() -> None:
    """基準日を指定しなくても、期間外の文書は今日を基準に除外する(rag_poc と同じ)。

    今日は共通の設定の時間帯の今日(OS の時間帯の ``date.today()`` ではない。#3249)。
    """
    sql, binds = _oracle_retrieval_where({})

    assert "'$.effective_to'), '9999-12-31') > :filter_as_of" in sql
    assert binds["filter_as_of"] == settings_today().isoformat()


class FakeDocumentOracle:
    def __init__(self) -> None:
        self.saved: DocumentClassification | None = None

    async def save_document_classification(
        self, document_id: str, classification: DocumentClassification
    ) -> DocumentDetail:
        if document_id != "doc-1":
            raise KeyError(document_id)
        self.saved = classification
        return DocumentDetail(
            id="doc-1",
            file_name="manual.pdf",
            status=FileStatus.INDEXED,
            uploaded_at=datetime(2026, 1, 1, tzinfo=UTC),
            classification=None if classification.is_empty() else classification,
        )


def test_put_classification_saves_and_returns_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocumentOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)

    response = client.put(
        "/api/documents/doc-1/classification",
        json={"large_category": "経理", "effective_from": "2026-04-01"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["classification"] == {
        "large_category": "経理",
        "middle_category": None,
        "small_category": None,
        "effective_from": "2026-04-01",
        "effective_to": None,
    }
    assert fake.saved is not None and fake.saved.effective_from == date(2026, 4, 1)


def test_put_classification_validates_and_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents_route, "OracleClient", FakeDocumentOracle)

    invalid = client.put(
        "/api/documents/doc-1/classification",
        json={"effective_from": "2026-05-01", "effective_to": "2026-04-01"},
    )
    missing = client.put("/api/documents/doc-x/classification", json={"large_category": "経理"})

    assert invalid.status_code == 422
    assert missing.status_code == 404


@pytest.mark.usefixtures("oracle_db")
async def test_classification_is_saved_and_filters_documents_on_real_oracle() -> None:
    """実 Oracle AI Database で、保存した分類と有効期間が述語どおりに絞り込まれることを確かめる。"""
    oracle = OracleClient()
    document = await oracle.create_document(
        file_name="classification.pdf",
        object_storage_path="local://classification.pdf",
        content_type="application/pdf",
    )
    saved = await oracle.save_document_classification(
        document.id,
        DocumentClassification(
            large_category="経理",
            effective_from=date(2026, 4, 1),
            effective_to=date(2027, 4, 1),
        ),
    )
    assert saved.classification is not None
    assert saved.classification.large_category == "経理"

    async def matches(filters: dict[str, str], *, effective_period: bool = True) -> bool:
        clauses, binds = _classification_where(filters, effective_period=effective_period)
        row = await oracle._fetch_one(  # noqa: SLF001 - 述語を実 DB で評価する
            "SELECT COUNT(*) AS count_value FROM rag_documents d "
            "WHERE d.document_id = :document_id AND " + " AND ".join(clauses),
            {**binds, "document_id": document.id},
        )
        return bool(row and int(str(row["count_value"])))

    assert await matches({"large_category": "経理", "as_of": "2026-04-01"})
    # 番号の接頭辞の有無が違っても、同じ名前なら一致する(#547)。
    assert await matches({"large_category": "10_経理", "as_of": "2026-04-01"})
    assert not await matches({"large_category": "人事", "as_of": "2026-04-01"})
    assert not await matches({"as_of": "2026-03-31"})
    # 終了日は排他的。
    assert not await matches({"as_of": "2027-04-01"})
    # ID で読む入口は有効期間で絞らない(#3249)。期間の外の基準日でも分類の条件だけで一致する。
    assert await matches({"large_category": "経理", "as_of": "2027-04-01"}, effective_period=False)

    cleared = await oracle.save_document_classification(document.id, DocumentClassification())
    assert cleared.classification is None
    assert await matches({"as_of": "2000-01-01"})
