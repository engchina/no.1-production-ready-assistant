"""既定の文書解析エンジン(Docling)で扱えない形式を、取込を始める前に止める(#286)。

- Docling の parser サービスは PDF と画像だけを解析する。それ以外(テキスト・JSON・XML・HTML・
  Office・メール など)は、アップロードの結果に案内を出し、処理レシピの取込の投入を 409 で止める。
- 処理レシピで Unstructured を選んでいれば止めない。PDF / 画像は影響しない。
- Unstructured のサービスが未配備・停止中なら、その旨も案内する。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.api.routes import documents as documents_route
from app.config import Settings, get_settings
from app.main import app
from app.rag import parser_source_guard
from app.rag.parser_source_guard import (
    check_parser_source,
    parser_source_block,
    unstructured_service_hint,
)
from app.rag.source_profile import build_source_profile
from app.schemas.document import (
    DocumentDetail,
    FileStatus,
    IngestionJob,
    IngestionJobPhase,
    SourceProfile,
)
from tests.support import AsgiTestClient
from tests.test_document_workspace import FakeWorkspaceOracle

client = AsgiTestClient(app)

# Docling が扱えない形式(Unstructured は扱える)。
_UNSUPPORTED_BY_DOCLING = [
    ("memo.txt", "text/plain"),
    ("notes.md", "text/markdown"),
    ("table.csv", "text/csv"),
    ("table.tsv", "text/tab-separated-values"),
    ("data.json", "application/json"),
    ("data.xml", "application/xml"),
    ("page.html", "text/html"),
    ("mail.eml", "message/rfc822"),
    (
        "report.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ("sheet.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
]
_SUPPORTED_BY_DOCLING = [
    ("policy.pdf", "application/pdf"),
    ("scan.png", "image/png"),
    ("photo.jpg", "image/jpeg"),
    ("chart.webp", "image/webp"),
]


@pytest.fixture(autouse=True)
def _default_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    """本番の既定(Docling)にそろえる(conftest は既存テストのため Unstructured にしている)。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_parser_adapter_backend", "docling")
    monkeypatch.setattr(settings, "rag_parser_docling_enabled", True)
    monkeypatch.setattr(settings, "rag_parser_unstructured_enabled", False)
    monkeypatch.setattr(settings, "rag_preprocess_profile", "passthrough")


def _profile(file_name: str, content_type: str) -> SourceProfile:
    return build_source_profile(
        original_file_name=file_name,
        sanitized_file_name=file_name,
        content_type=content_type,
        file_size_bytes=16,
        content_sha256="a" * 64,
    )


# --- 判定(1 か所にまとめたもの) ---------------------------------------------------


def test_default_parser_backend_is_docling() -> None:
    assert Settings.model_fields["rag_parser_adapter_backend"].default == "docling"
    # 廃止済みの local は既定エンジン(Docling)のサービスへ渡る。
    local = get_settings().model_copy(update={"rag_parser_adapter_backend": "local"})
    assert parser_source_guard.effective_parser_backend(local) == "docling"


@pytest.mark.parametrize(("file_name", "content_type"), _UNSUPPORTED_BY_DOCLING)
def test_default_docling_blocks_unsupported_formats(file_name: str, content_type: str) -> None:
    block = parser_source_block(get_settings(), _profile(file_name, content_type))
    assert block is not None
    assert block.code == "parser_source_unsupported"
    assert block.backend == "docling"
    assert block.suggested_backend == "unstructured"
    assert block.file_format == f".{file_name.rsplit('.', 1)[1]}"
    # 理由と対処(処理レシピで Unstructured を選ぶ)を日本語で返す。
    assert "Docling（既定の解析エンジン）" in block.message
    assert "取込を開始しませんでした" in block.message
    assert "PDF・画像" in block.message
    assert "「処理レシピ」で「文書解析」を Unstructured に変えて" in block.message


@pytest.mark.parametrize(("file_name", "content_type"), _SUPPORTED_BY_DOCLING)
def test_default_docling_accepts_pdf_and_images(file_name: str, content_type: str) -> None:
    assert parser_source_block(get_settings(), _profile(file_name, content_type)) is None


@pytest.mark.parametrize(("file_name", "content_type"), _UNSUPPORTED_BY_DOCLING)
def test_unstructured_selected_is_not_blocked(file_name: str, content_type: str) -> None:
    settings = get_settings().model_copy(update={"rag_parser_adapter_backend": "unstructured"})
    assert parser_source_block(settings, _profile(file_name, content_type)) is None


def test_office_to_pdf_preprocess_is_left_to_ingestion() -> None:
    """ファイル準備で形式が変わる(Office→PDF)ときは、変換後の形式で取込時に判定する。"""
    settings = get_settings().model_copy(
        update={"rag_preprocess_enabled": True, "rag_preprocess_profile": "office_to_pdf"}
    )
    profile = _profile(
        "report.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert parser_source_block(settings, profile) is None


def test_office_message_mentions_office_to_pdf() -> None:
    block = parser_source_block(
        get_settings(),
        _profile(
            "report.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    )
    assert block is not None
    assert "「ファイル準備」で Office→PDF" in block.message


_WORD = "application/msword"
_PPT = "application/vnd.ms-powerpoint"
_XLS = "application/vnd.ms-excel"
_DOCM = "application/vnd.ms-word.document.macroEnabled.12"
_PPTM = "application/vnd.ms-powerpoint.presentation.macroEnabled.12"
_XLSM = "application/vnd.ms-excel.sheet.macroEnabled.12"
_ODT = "application/vnd.oasis.opendocument.text"


def _preprocess(profile: str, **update: Any) -> Settings:
    return get_settings().model_copy(
        update={"rag_preprocess_enabled": True, "rag_preprocess_profile": profile, **update}
    )


@pytest.mark.parametrize(
    ("file_name", "content_type", "reason"),
    [
        ("old.doc", _WORD, "legacy_office_binary_not_supported"),
        ("old.ppt", _PPT, "legacy_office_binary_not_supported"),
        ("old.xls", _XLS, "legacy_office_binary_not_supported"),
        ("macro.docm", _DOCM, "macro_enabled_office_not_supported"),
        ("macro.pptm", _PPTM, "macro_enabled_office_not_supported"),
        ("macro.xlsm", _XLSM, "macro_enabled_office_not_supported"),
        ("memo.odt", _ODT, "odt_rtf_not_supported"),
        ("memo.rtf", "text/rtf", "odt_rtf_not_supported"),
        ("memo.rtf", "application/rtf", "odt_rtf_not_supported"),
    ],
)
def test_office_needing_conversion_is_blocked_without_converting_preprocess(
    file_name: str, content_type: str, reason: str
) -> None:
    """旧形式・マクロ付きの Office は、変換を選ばなければ案内で止める(#2470)。"""
    profile = _profile(file_name, content_type)
    assert profile.modality == "office"
    assert profile.unsupported_reason == reason
    block = parser_source_block(get_settings(), profile)
    assert block is not None
    assert block.code == "office_conversion_required"
    assert "原本のままでは解析できません" in block.message
    assert "マクロは実行しません" in block.message
    expected = "Excel→JSONL" if file_name.endswith(("xls", "xlsm")) else "Office→PDF"
    assert f"「{expected}」" in block.message


@pytest.mark.parametrize(
    ("file_name", "content_type"),
    [
        ("old.doc", _WORD),
        ("old.ppt", _PPT),
        ("macro.docm", _DOCM),
        ("macro.pptm", _PPTM),
        ("memo.odt", _ODT),
        ("memo.rtf", "text/rtf"),
        ("memo", _ODT),
        ("memo", "application/rtf"),
    ],
)
def test_office_to_pdf_converts_word_and_powerpoint(file_name: str, content_type: str) -> None:
    profile = _profile(file_name, content_type)
    assert parser_source_guard.settings_convert_source(_preprocess("office_to_pdf"), profile)
    assert parser_source_block(_preprocess("office_to_pdf"), profile) is None


@pytest.mark.parametrize(
    ("file_name", "content_type"),
    [
        ("old.xls", _XLS),
        ("macro.xlsm", _XLSM),
        ("sheet.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ],
)
def test_office_to_pdf_does_not_convert_excel(file_name: str, content_type: str) -> None:
    """Office→PDF は Excel を変換しない。Excel→JSONL を選ぶよう案内する(#2470)。"""
    profile = _profile(file_name, content_type)
    block = parser_source_block(_preprocess("office_to_pdf"), profile)
    assert block is not None
    assert block.code == "office_to_pdf_spreadsheet_not_supported"
    assert "Word・PowerPoint だけ" in block.message
    assert "「Excel→JSONL」" in block.message
    jsonl = _preprocess("excel_to_jsonl", rag_parser_adapter_backend="structured_records_jsonl")
    assert parser_source_block(jsonl, profile) is None


@pytest.mark.parametrize(("file_name", "content_type"), [("old.doc", _WORD), ("macro.docm", _DOCM)])
def test_word_to_jsonl_converts_legacy_and_macro_word(file_name: str, content_type: str) -> None:
    """Word→JSONL は .doc / .docm を変換する（#2457 の ④。解析エンジンは構造化レコード JSONL）。"""
    profile = _profile(file_name, content_type)
    jsonl = _preprocess("word_to_jsonl", rag_parser_adapter_backend="structured_records_jsonl")
    assert parser_source_guard.settings_convert_source(jsonl, profile)
    assert parser_source_block(jsonl, profile) is None
    # 解析エンジンが構造化レコード JSONL でなければ、選ぶよう案内する（形式で自動に切り替えない）。
    docling = parser_source_block(_preprocess("word_to_jsonl"), profile)
    assert docling is not None
    assert docling.suggested_backend == "structured_records_jsonl"
    # 変換を選ばないときの案内に、Word→JSONL も出す。
    block = parser_source_block(get_settings(), profile)
    assert block is not None and "「Office→PDF」か「Word→JSONL」" in block.message


def test_word_to_jsonl_does_not_convert_excel_or_powerpoint() -> None:
    jsonl = _preprocess("word_to_jsonl", rag_parser_adapter_backend="structured_records_jsonl")
    for file_name, content_type in (("old.xls", _XLS), ("old.ppt", _PPT), ("macro.xlsm", _XLSM)):
        block = parser_source_block(jsonl, _profile(file_name, content_type))
        assert block is not None and block.code == "office_conversion_required"


def test_excel_to_jsonl_does_not_convert_word() -> None:
    profile = _profile("old.doc", _WORD)
    jsonl = _preprocess("excel_to_jsonl", rag_parser_adapter_backend="structured_records_jsonl")
    block = parser_source_block(jsonl, profile)
    assert block is not None and block.code == "office_conversion_required"


@pytest.mark.parametrize("preprocess", ["word_to_jsonl", "excel_to_jsonl"])
def test_only_office_to_pdf_converts_odt_rtf(preprocess: str) -> None:
    """ODT・RTF は Office→PDF だけが変換する(pandoc を入れない。#3351)。"""
    settings = _preprocess(preprocess, rag_parser_adapter_backend="structured_records_jsonl")
    for profile in (_profile("memo.odt", _ODT), _profile("memo.rtf", "text/rtf")):
        block = parser_source_block(settings, profile)
        assert block is not None and block.code == "office_conversion_required"
        assert "「Office→PDF」" in block.message
        assert "ODT・RTF" in block.message


def test_converted_source_profile_drops_original_format_reason() -> None:
    profile = _profile("old.doc", _WORD)
    converted = parser_source_guard.converted_source_profile(profile, converted=True)
    assert converted is not None
    assert converted.unsupported_reason is None
    assert "unsupported_legacy_office_binary" not in converted.quality_warnings
    # 変換しなかったときは原本の理由のまま止める。
    assert parser_source_guard.converted_source_profile(profile, converted=False) is profile
    tiff = _profile("scan.tiff", "image/tiff")
    assert parser_source_guard.converted_source_profile(tiff, converted=True) is tiff


def test_xlsx_docling_message_suggests_excel_to_jsonl() -> None:
    block = parser_source_block(
        get_settings(),
        _profile("sheet.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    )
    assert block is not None
    assert "Excel→JSONL" in block.message
    assert "Office→PDF" not in block.message


def test_formats_already_marked_unsupported_are_left_to_existing_check() -> None:
    """Outlook .msg・TIFF などは既存の「非対応形式」として別に止まる。"""
    msg = _profile("mail.msg", "application/vnd.ms-outlook")
    assert parser_source_block(get_settings(), msg) is None
    assert parser_source_block(get_settings(), _profile("scan.tiff", "image/tiff")) is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("not_installed", "配備されていません"),
        ("stopped", "停止しています"),
        ("failed", "停止しています"),
        ("running", ""),
        ("starting", ""),
        (None, ""),
    ],
)
def test_unstructured_service_hint(status: str | None, expected: str) -> None:
    hint = unstructured_service_hint(status)
    if expected:
        assert expected in hint
        assert "サービス管理" in hint
    else:
        assert hint == ""


def test_check_parser_source_adds_unstructured_service_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def not_installed(_settings: Any) -> str:
        return "not_installed"

    monkeypatch.setattr(parser_source_guard, "_unstructured_service_status", not_installed)
    block = asyncio.run(check_parser_source(get_settings(), _profile("mail.eml", "message/rfc822")))
    assert block is not None
    assert "parser-unstructured）は配備されていません" in block.message
    assert "rag_enable_parser_unstructured" in block.message


def test_check_parser_source_does_not_probe_for_supported_formats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(_settings: Any) -> str:
        raise AssertionError("PDF では Unstructured の状態を見ない")

    monkeypatch.setattr(parser_source_guard, "_unstructured_service_status", fail)
    pdf = _profile("a.pdf", "application/pdf")
    assert asyncio.run(check_parser_source(get_settings(), pdf)) is None


# --- アップロードの結果に案内を出す --------------------------------------------------


def _use_oracle(monkeypatch: pytest.MonkeyPatch, oracle: Any) -> None:
    monkeypatch.setattr(documents_route, "OracleClient", lambda: oracle)


def test_upload_returns_parser_notice_for_docling_unsupported_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_oracle(monkeypatch, FakeWorkspaceOracle())

    response = client.post(
        "/api/documents/upload",
        files={"file": ("policy.txt", "経費規程".encode(), "text/plain")},
    )

    assert response.status_code == 200
    notice = response.json()["data"]["parser_notice"]
    assert notice["code"] == "parser_source_unsupported"
    assert notice["backend"] == "docling"
    assert notice["suggested_backend"] == "unstructured"
    assert "Unstructured" in notice["message"]


def test_upload_has_no_parser_notice_for_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_oracle(monkeypatch, FakeWorkspaceOracle())

    response = client.post(
        "/api/documents/upload",
        files={"file": ("policy.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json()["data"]["parser_notice"] is None


# --- 処理レシピの取込の投入を止める ------------------------------------------------


class _RecipeOracle:
    def __init__(self, file_name: str, content_type: str, processing_config: dict[str, Any]):
        self.file_name = file_name
        self.content_type = content_type
        self.processing_config = processing_config
        self.created: IngestionJob | None = None

    async def get_document(self, document_id: str) -> DocumentDetail:
        return DocumentDetail(
            id=document_id,
            file_name=self.file_name,
            content_type=self.content_type,
            status=FileStatus.UPLOADED,
            object_storage_path=f"local://{self.file_name}",
            content_sha256="a" * 64,
            uploaded_at=datetime.now(UTC),
        )

    async def ensure_default_document_recipe(self, document_id: str) -> dict[str, Any]:
        return {"recipe_id": "recipe-1"}

    async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, Any]:
        return {
            "document_id": document_id,
            "recipe_id": recipe_id,
            "config_revision": 1,
            "processing_config": self.processing_config,
        }

    async def list_document_ingestion_jobs(self, document_id: str) -> list[IngestionJob]:
        return []

    async def create_ingestion_job(self, job: IngestionJob) -> IngestionJob:
        self.created = job
        return job


def _enqueue(monkeypatch: pytest.MonkeyPatch, oracle: _RecipeOracle) -> Any:
    _use_oracle(monkeypatch, oracle)
    monkeypatch.setattr(documents_route, "_dispatch_ingestion_job", lambda *_a, **_k: None)

    async def status(_settings: Any) -> str:
        return "stopped"

    monkeypatch.setattr(parser_source_guard, "_unstructured_service_status", status)
    return client.post("/api/documents/doc-1/recipes/recipe-1/ingestion-jobs?phase=PREPROCESS")


def test_recipe_job_for_docling_unsupported_format_is_rejected_before_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _RecipeOracle("mail.eml", "message/rfc822", {})

    response = _enqueue(monkeypatch, oracle)

    assert response.status_code == 409
    assert "取込を開始しませんでした" in response.text
    assert "Unstructured を起動してください" in response.text
    assert oracle.created is None


def test_recipe_job_with_unstructured_is_not_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    oracle = _RecipeOracle(
        "mail.eml",
        "message/rfc822",
        {"parser_adapter_backend": "unstructured", "parser_unstructured_enabled": True},
    )

    response = _enqueue(monkeypatch, oracle)

    assert response.status_code == 200
    assert oracle.created is not None
    overrides = oracle.created.settings_overrides or {}
    assert overrides["rag_parser_adapter_backend"] == "unstructured"


def test_recipe_job_for_pdf_is_not_affected(monkeypatch: pytest.MonkeyPatch) -> None:
    oracle = _RecipeOracle("policy.pdf", "application/pdf", {})

    response = _enqueue(monkeypatch, oracle)

    assert response.status_code == 200
    assert oracle.created is not None


def test_recipe_chunk_phase_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """抽出済みの結果を使う工程(分割・索引)は止めない。"""
    oracle = _RecipeOracle("mail.eml", "message/rfc822", {})
    _use_oracle(monkeypatch, oracle)
    monkeypatch.setattr(documents_route, "_dispatch_ingestion_job", lambda *_a, **_k: None)

    response = client.post("/api/documents/doc-1/recipes/recipe-1/ingestion-jobs?phase=CHUNK")

    assert response.status_code == 200
    assert oracle.created is not None
    assert oracle.created.phase == IngestionJobPhase.CHUNK


def test_document_ingestion_job_for_docling_unsupported_format_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文書単位の取込(既定レシピ。ファイル一覧の一括取込など)も、出力を初期化する前に止める。"""
    oracle = _RecipeOracle("memo.txt", "text/plain", {})
    _use_oracle(monkeypatch, oracle)

    async def fail_reset(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("止める前に出力を初期化してはいけない")

    monkeypatch.setattr(documents_route, "_reset_document_outputs_for_extract", fail_reset)
    monkeypatch.setattr(
        parser_source_guard, "_unstructured_service_status", lambda _s: _async_value("running")
    )

    response = client.post("/api/documents/doc-1/ingestion-jobs")

    assert response.status_code == 409
    assert "Docling（既定の解析エンジン）" in response.text
    assert oracle.created is None


async def _async_value(value: str) -> str:
    return value


class _RecipeOnlyOracle(_RecipeOracle):
    """処理設定はレシピだけにある(文書行の設定は読まない)。"""

    def __init__(self, processing_config: dict[str, Any]) -> None:
        super().__init__("memo.md", "text/markdown", processing_config)
        self.recipe_ids: list[str] = []

    async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, Any]:
        self.recipe_ids.append(recipe_id)
        return await super().get_document_recipe(document_id, recipe_id)

    async def get_document_processing_config(self, document_id: str) -> Any:
        raise AssertionError("レシピを読めるときは文書行の設定を読まない")


def test_ingestion_settings_come_from_recipe() -> None:
    """取込の実行・表示の設定は、取込の開始前の検査と同じくレシピから読む(#697)。"""
    config = {"parser_adapter_backend": "unstructured", "parser_unstructured_enabled": True}

    oracle = _RecipeOnlyOracle(config)
    settings, _config = asyncio.run(
        documents_route._resolve_ingestion_settings(cast(Any, oracle), "doc-1")
    )
    assert settings.rag_parser_adapter_backend == "unstructured"
    assert oracle.recipe_ids == ["recipe-1"]

    oracle = _RecipeOnlyOracle(config)
    asyncio.run(documents_route._resolve_ingestion_settings(cast(Any, oracle), "doc-1", "recipe-2"))
    assert oracle.recipe_ids == ["recipe-2"]
