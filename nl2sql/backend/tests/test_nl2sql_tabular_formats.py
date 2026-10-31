from __future__ import annotations

import base64
import hashlib
import importlib
import io
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request

from app.features.nl2sql.models import DbAdminCsvUploadRequest, DbAdminImportTabularRequest
from app.features.nl2sql.ontology_models import OntologySourceDocument
from app.features.nl2sql.ontology_sources import (
    OntologySourceError,
    extract_ontology_source,
    validate_source_media_type,
    validate_source_signature,
)
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.features.nl2sql.tabular_files import (
    TabularFileReadError,
    TabularSheetSelectionError,
    read_workbook_sheet,
    read_workbook_sheets,
    select_workbook_sheet,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tabular-import.xls.b64"
nl2sql_router = importlib.import_module("app.features.nl2sql.router")


def _xls_fixture() -> bytes:
    return base64.b64decode(_FIXTURE_PATH.read_text(encoding="ascii"))


def _xlsx_fixture() -> bytes:
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "ImportData"
    sheet.append(["ID", "NAME"])
    sheet.append([1, "架空商事"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_real_xls_reader_preserves_sheets_and_scalar_types_case_insensitively() -> None:
    sheets = read_workbook_sheets("TABULAR-IMPORT.XLS", _xls_fixture())

    assert [sheet.title for sheet in sheets] == ["ImportData", "Secondary"]
    assert sheets[0].active is True
    assert sheets[0].rows[1][:4] == [
        1,
        "架空商事",
        True,
        datetime(2026, 7, 30, 9, 15),
    ]
    assert sheets[0].rows[2][0] == 2.5
    selected, warnings = select_workbook_sheet(sheets, "Secondary")
    assert selected.rows[1] == ["商品一覧を取得", "SELECT * FROM PRODUCTS"]
    assert warnings == []
    fallback, warnings = select_workbook_sheet(sheets, "Missing")
    assert fallback.title == "ImportData"
    assert warnings == [
        "Missing: Sheet が見つからないため active または先頭 Sheet を使用しました。"
    ]
    with pytest.raises(TabularSheetSelectionError, match="Missing: Sheet が見つかりません"):
        select_workbook_sheet(sheets, "Missing", require_requested_name=True)
    with pytest.raises(TabularSheetSelectionError, match="Sheet 名は必須"):
        select_workbook_sheet(sheets, require_requested_name=True)


def test_read_workbook_sheet_loads_only_requested_openxml_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Sheet:
        def __init__(self, title: str, rows: list[list[object]], *, fail: bool = False) -> None:
            self.title = title
            self._rows = rows
            self._fail = fail
            self.iterated = False

        def iter_rows(self, *, values_only: bool) -> list[list[object]]:
            assert values_only is True
            self.iterated = True
            if self._fail:
                raise AssertionError("unrequested sheet should not be read")
            return self._rows

    class _Workbook:
        def __init__(self) -> None:
            self.first = _Sheet("First", [["SHOULD_NOT_READ"]], fail=True)
            self.second = _Sheet("Second", [["ID"], [1]])
            self.active = self.first
            self.sheetnames = ["First", "Second"]
            self.closed = False

        def __getitem__(self, name: str) -> _Sheet:
            return {"First": self.first, "Second": self.second}[name]

        def close(self) -> None:
            self.closed = True

    workbook = _Workbook()
    openpyxl = importlib.import_module("openpyxl")
    monkeypatch.setattr(openpyxl, "load_workbook", lambda *_args, **_kwargs: workbook)

    sheet, warnings = read_workbook_sheet(
        "import.xlsx",
        b"placeholder",
        "Second",
        require_requested_name=True,
    )

    assert sheet.title == "Second"
    assert sheet.rows == [["ID"], [1]]
    assert warnings == []
    assert workbook.first.iterated is False
    assert workbook.second.iterated is True
    assert workbook.closed is True


@pytest.mark.parametrize(
    ("filename", "content", "sheet_name"),
    [
        ("IMPORT.CSV", b"ID,NAME\n1,Aoyama\n", ""),
        ("IMPORT.XLSX", _xlsx_fixture(), "ImportData"),
    ],
    # 生成した xlsx の bytes は作成時刻を含む。xdist の worker ごとにテスト ID が
    # 変わらないよう ID を固定する（#344）。
    ids=["IMPORT.CSV", "IMPORT.XLSX"],
)
def test_core_csv_and_xlsx_formats_are_case_insensitive(
    filename: str, content: bytes, sheet_name: str
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    imported = service.import_db_admin_tabular(
        DbAdminImportTabularRequest(
            table_name="CORE_IMPORT",
            content_base64=base64.b64encode(content).decode(),
            filename=filename,
            sheet_name=sheet_name,
        )
    )

    assert imported.row_count == 1
    assert imported.sample_rows[0]["ID"] == "1"


def test_real_xls_flows_cover_table_upload_learning_material_and_ontology() -> None:
    content = _xls_fixture()
    encoded = base64.b64encode(content).decode()
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    imported = service.import_db_admin_tabular(
        DbAdminImportTabularRequest(
            table_name="LEGACY_IMPORT",
            content_base64=encoded,
            filename="legacy.XLS",
            sheet_name="ImportData",
        )
    )
    assert imported.sheet_name == "ImportData"
    assert imported.row_count == 2
    assert imported.sample_rows[0]["NAME"] == "架空商事"
    assert imported.sample_rows[0]["CREATED_AT"] == "2026-07-30 09:15:00"

    uploaded = service.upload_db_admin_csv(
        DbAdminCsvUploadRequest(
            table_name="LEGACY_IMPORT",
            content_base64=encoded,
            filename="legacy.xls",
        )
    )
    assert uploaded.row_count == 2
    assert uploaded.sample_rows[1]["NAME"] == "見本物産"

    classifier_warnings: list[str] = []
    classifier_rows, classifier_skipped = service._parse_classifier_training_file(
        "training.xls", content, classifier_warnings
    )
    assert classifier_skipped == 0
    assert classifier_warnings == []
    assert classifier_rows[0] == ("監査", "監査ログを確認したい", "audit")

    material_warnings: list[str] = []
    material, skipped = service._parse_profile_learning_material_file(
        "learning.xls", content, material_warnings
    )
    assert skipped == 0
    assert material_warnings == []
    assert material["terms"]["粗利"] == "売上から原価を引いた金額"
    assert material["examples"][0]["question"] == "商品一覧を取得"

    terms_warnings: list[str] = []
    assert service._parse_legacy_terms_file("terms.xls", content, terms_warnings)["売上"] == (
        "確定済み受注の合計"
    )
    rules_warnings: list[str] = []
    assert service._parse_legacy_rules_file("rules.xls", content, rules_warnings)[0] == (
        "監査ログを確認したい"
    )

    source = OntologySourceDocument(
        id="source-xls",
        profile_id="sales",
        filename="evidence.xls",
        media_type="application/vnd.ms-excel",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_uri="/tmp/evidence.xls",
    )
    extracted = extract_ontology_source(source, content)
    assert any(chunk.locator == "sheet:Secondary;row:2" for chunk in extracted.chunks)
    assert extracted.qa_pairs[0].question == "監査ログを確認したい"


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("missing-sheet.xlsx", _xlsx_fixture()),
        ("missing-sheet.xls", _xls_fixture()),
    ],
    # 生成した xlsx の bytes は作成時刻を含む。xdist の worker ごとにテスト ID が
    # 変わらないよう ID を固定する（#344）。
    ids=["missing-sheet.xlsx", "missing-sheet.xls"],
)
def test_db_admin_tabular_import_requires_existing_workbook_sheet(
    filename: str, content: bytes
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    encoded = base64.b64encode(content).decode()

    with pytest.raises(ValueError, match="Sheet 名は必須"):
        service.import_db_admin_tabular(
            DbAdminImportTabularRequest(
                table_name="STRICT_IMPORT",
                content_base64=encoded,
                filename=filename,
            )
        )

    with pytest.raises(ValueError, match="Missing: Sheet が見つかりません"):
        service.import_db_admin_tabular(
            DbAdminImportTabularRequest(
                table_name="STRICT_IMPORT",
                content_base64=encoded,
                filename=filename,
                sheet_name="Missing",
            )
        )


def test_db_admin_tabular_import_rejects_invalid_mode_without_create_fallback() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    with pytest.raises(ValueError, match="未対応 mode"):
        service.import_db_admin_tabular(
            DbAdminImportTabularRequest(
                table_name="STRICT_IMPORT",
                content_base64=base64.b64encode(b"ID\n1\n").decode(),
                filename="strict.csv",
                mode="upsert",
            )
        )


def test_xls_signature_media_type_and_corruption_errors_are_actionable() -> None:
    content = _xls_fixture()
    validate_source_signature("evidence.xls", content)
    validate_source_media_type("evidence.xls", "application/vnd.ms-excel")

    with pytest.raises(TabularFileReadError, match="破損、暗号化"):
        read_workbook_sheets("broken.xls", b"not-an-ole-workbook")
    with pytest.raises(OntologySourceError, match="再選択"):
        validate_source_signature("spoofed.xls", b"not-an-ole-workbook")
    with pytest.raises(OntologySourceError, match="正しい形式"):
        validate_source_signature("spoofed.csv", _xlsx_fixture())
    with pytest.raises(OntologySourceError, match="Content-Type"):
        validate_source_media_type("evidence.xls", "application/zip")


def test_binary_disguised_as_csv_and_corrupt_learning_workbook_are_rejected() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    with pytest.raises(TabularFileReadError, match="実際の形式"):
        service.import_db_admin_tabular(
            DbAdminImportTabularRequest(
                table_name="SPOOFED_IMPORT",
                content_base64=base64.b64encode(_xlsx_fixture()).decode(),
                filename="spoofed.csv",
            )
        )
    with pytest.raises(ValueError, match="xlsx テンプレート"):
        service.import_classifier_training_data(
            filename="broken.xls",
            content=b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1broken",
        )
    with pytest.raises(TabularFileReadError, match="破損、暗号化"):
        service.import_classifier_training_data(
            filename="broken.xlsx",
            content=b"not-a-workbook",
        )


@pytest.mark.asyncio
async def test_corrupt_classifier_workbook_returns_japanese_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_import(*_: object, **__: object) -> None:
        raise TabularFileReadError(
            "XLSX の読込に失敗しました。"
            "ファイルが破損、暗号化、または拡張子と内容が不一致でないか確認してください。"
        )

    monkeypatch.setattr(nl2sql_router, "run_sync_io", reject_import)

    class Upload:
        filename = "broken.xlsx"

        async def read(self) -> bytes:
            return b"not-a-workbook"

    with pytest.raises(HTTPException) as exc_info:
        await nl2sql_router.import_classifier_training_data(
            cast(Request, SimpleNamespace(state=SimpleNamespace(principal=None))),
            Upload(),
        )

    assert exc_info.value.status_code == 400
    assert "破損、暗号化" in str(exc_info.value.detail)
