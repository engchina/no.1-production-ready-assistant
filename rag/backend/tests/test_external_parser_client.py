"""外部 GPU parser の native protocol と共通抽出変換のテスト。"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from collections.abc import Callable

import httpx
import pytest
from PIL import Image

from app.clients.external_parser import (
    ExternalParserCallError,
    ExternalParserClient,
    _dots_bboxes_to_page_px,
    _dots_model_input_size,
    _RenderedPage,
)
from app.config import Settings
from app.rag.mineru_license import set_mineru_license_acknowledgement
from app.schemas.document import SourceModality, SourceProfile
from tests.fixtures.synthetic_pdf import text_pdf


def _profile(name: str = "scan.pdf", *, content_type: str = "application/pdf") -> SourceProfile:
    modality = SourceModality.PDF if content_type == "application/pdf" else SourceModality.IMAGE
    return SourceProfile(
        original_file_name=name,
        sanitized_file_name=name,
        content_type=content_type,
        file_size_bytes=3,
        content_sha256="0" * 64,
        modality=modality,
        parser_profile="pdf" if modality == SourceModality.PDF else "image",
    )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    original = httpx.Client
    transport = httpx.MockTransport(handler)

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("timeout", None)
        return original(transport=transport)

    monkeypatch.setattr(httpx, "Client", factory)


def _pdf(page_count: int = 2) -> bytes:
    return text_pdf(
        [f"page {index + 1}" for index in range(page_count)], size=(120, 80), at=(12, 30)
    )


_MINERU_HOST = "https://mineru.example.com"


def _middle_json() -> dict[str, object]:
    """MinerU 4.x の Middle JSON 2.0（docvortex.middle）の最小の例。"""
    return {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "is_full_document": True,
        "metadata": {"file_suffix": "pdf", "producer": {"name": "mineru", "version": "4.0.11"}},
        "extensions": {"mineru": {"tier": "basic", "parse_mode": "ocr"}},
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "header",
                        "index": 0,
                        "bbox": [0.1, 0.01, 0.9, 0.03],
                        "content": [{"type": "text", "content": "社内資料"}],
                    },
                    {
                        "type": "paragraph_title",
                        "index": 1,
                        "level": 2,
                        "bbox": [0.1, 0.1, 0.5, 0.12],
                        "content": [{"type": "text", "content": "第1章 申請"}],
                    },
                    {
                        "type": "text",
                        "index": 2,
                        "bbox": [0.1, 0.2, 0.5, 0.4],
                        "content": [
                            {"type": "text", "content": "第一頁の"},
                            {
                                "type": "hyperlink",
                                "url": "https://example.com",
                                "content": [{"type": "text", "content": "本文"}],
                            },
                        ],
                    },
                ],
            },
            {
                "page_idx": 1,
                "blocks": [
                    {
                        "type": "table",
                        "index": 0,
                        "bbox": [0.005, 0.008, 0.11, 0.07],
                        "content": [
                            {
                                "type": "table_caption",
                                "content": [{"type": "text", "content": "表1 期限"}],
                            },
                            {
                                "type": "table_body",
                                "index": 0,
                                "bbox": [0.005, 0.008, 0.11, 0.07],
                                "content": "<table><tr><td>A</td></tr></table>",
                            },
                        ],
                    },
                    {
                        "type": "page_number",
                        "index": 1,
                        "content": [{"type": "text", "content": "2"}],
                    },
                ],
            },
        ],
    }


def _mineru_handler(
    seen: list[httpx.Request],
    *,
    upload: dict[str, object] | None = None,
    job_statuses: tuple[str, ...] = ("running", "completed"),
    file_status: str = "completed",
    download: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    polls = iter(job_statuses)

    def job(status: str) -> dict[str, object]:
        files: list[dict[str, object]] = [
            {
                "file_id": "file-src",
                "name": "scan.pdf",
                "page_range": "all",
                "status": file_status if status in {"completed", "partial"} else "running",
                "output_files": {"middle_json": {"file_id": "file-mj", "bytes": 10}},
            }
        ]
        return {
            "job_id": "job-1",
            "status": status,
            "tier": "basic",
            "files": files,
            "links": {"self": "/v1/parse/jobs/job-1", "cancel": "/v1/parse/jobs/job-1/cancel"},
        }

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/v1/uploads":
            return httpx.Response(
                200,
                json=upload
                or {
                    "id": "upload-1",
                    "status": "pending",
                    "upload_url": "/v1/uploads/upload-1/content",
                    "upload_method": "PUT",
                    "upload_headers": {"Content-Type": "application/octet-stream"},
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        if path == "/v1/uploads/upload-1/complete":
            return httpx.Response(
                200, json={"id": "upload-1", "status": "completed", "file": {"id": "file-src"}}
            )
        if request.method == "POST" and path == "/v1/parse/jobs":
            return httpx.Response(202, json=job("queued"))
        if request.method == "GET" and path == "/v1/parse/jobs/job-1":
            return httpx.Response(200, json=job(next(polls, job_statuses[-1])))
        if path == "/v1/parse/jobs/job-1/cancel":
            return httpx.Response(200, json={"job_id": "job-1", "status": "canceled"})
        if download is not None:
            return download(request)
        if path == "/v1/files/file-mj/content":
            return httpx.Response(200, json=_middle_json())
        return httpx.Response(404, json={"detail": path})

    return handle


def _mineru_settings(**overrides: object) -> Settings:
    settings = Settings(
        rag_parser_mineru_api_host=f"{_MINERU_HOST}/",
        rag_parser_mineru_api_key="mineru-secret",
        **overrides,  # type: ignore[arg-type]
    )
    # system_admin が MinerU のライセンスの条件を確認済み（#3359）。
    set_mineru_license_acknowledgement(settings, by="", at="2026-10-26T00:00:00+00:00")
    return settings


def test_mineru_v1_parses_middle_json_with_page_bbox_table_and_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    _install_transport(monkeypatch, _mineru_handler(seen))

    result = ExternalParserClient(_mineru_settings()).parse(
        "mineru", b"%PDF-1.7", _profile(), "application/pdf"
    )

    assert [(request.method, request.url.path) for request in seen] == [
        ("POST", "/v1/uploads"),
        ("PUT", "/v1/uploads/upload-1/content"),
        ("POST", "/v1/uploads/upload-1/complete"),
        ("POST", "/v1/parse/jobs"),
        ("GET", "/v1/parse/jobs/job-1"),
        ("GET", "/v1/parse/jobs/job-1"),
        ("GET", "/v1/files/file-mj/content"),
    ]
    assert all(request.headers["authorization"] == "Bearer mineru-secret" for request in seen)
    upload = json.loads(seen[0].content)
    assert upload["sha256sum"] == hashlib.sha256(b"%PDF-1.7").hexdigest()
    assert upload["bytes"] == len(b"%PDF-1.7")
    assert seen[1].content == b"%PDF-1.7"
    job = json.loads(seen[3].content)
    assert job["tier"] == "basic"
    assert job["output_formats"] == ["middle_json"]
    assert job["files"] == [{"source": {"type": "file_id", "file_id": "file-src"}}]

    extraction = result.extraction
    assert extraction is not None
    # 頁のヘッダーと頁番号は本文に入れない。
    assert [element.text for element in extraction.elements] == [
        "第1章 申請",
        "第一頁の本文",
        "表1 期限\n<table><tr><td>A</td></tr></table>",
    ]
    assert [element.page_number for element in extraction.elements] == [1, 1, 2]
    assert [element.element_id for element in extraction.elements] == [
        "mineru-p1-b1",
        "mineru-p1-b2",
        "mineru-p2-b0",
    ]
    # Middle JSON の 0〜1 の bbox を content list と同じ 0〜1000 にする。
    assert extraction.elements[1].bbox == [100.0, 200.0, 500.0, 400.0]
    assert extraction.elements[2].bbox == [5.0, 8.0, 110.0, 70.0]
    assert extraction.elements[2].content_kind == "table"
    # 見出しの block は見出しとして読み、後の本文の節の見出しの列になる。
    assert extraction.elements[0].kind == "title"
    assert extraction.elements[1].section_path == ["第1章 申請"]
    assert extraction.parser_artifacts["external_protocol"] == "mineru_v1"


def test_mineru_v1_known_file_skips_byte_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    _install_transport(
        monkeypatch,
        _mineru_handler(
            seen,
            upload={"id": "upload-1", "status": "completed", "file": {"id": "file-src"}},
        ),
    )

    ExternalParserClient(_mineru_settings()).parse(
        "mineru", b"%PDF-1.7", _profile(), "application/pdf"
    )

    assert [request.url.path for request in seen][:2] == ["/v1/uploads", "/v1/parse/jobs"]
    assert not any(request.method == "PUT" for request in seen)


def test_mineru_v1_cross_origin_upload_and_download_do_not_receive_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def download(request: httpx.Request) -> httpx.Response:
        if request.url.port is None:
            # 同じ host でも port が違えば別 origin。
            return httpx.Response(302, headers={"location": "https://mineru.example.com:8443/mj"})
        return httpx.Response(200, json=_middle_json())

    _install_transport(
        monkeypatch,
        _mineru_handler(
            seen,
            upload={
                "id": "upload-1",
                "status": "pending",
                "upload_url": "https://storage.example.net/put?signature=abc",
                "upload_method": "PUT",
                "upload_headers": {"x-signed": "1"},
            },
            download=download,
        ),
    )

    result = ExternalParserClient(_mineru_settings()).parse(
        "mineru", b"%PDF-1.7", _profile(), "application/pdf"
    )

    by_url = {(request.method, str(request.url)): request for request in seen}
    signed_put = by_url[("PUT", "https://storage.example.net/put?signature=abc")]
    assert "authorization" not in signed_put.headers
    assert signed_put.headers["x-signed"] == "1"
    redirected = by_url[("GET", "https://mineru.example.com:8443/mj")]
    assert "authorization" not in redirected.headers
    assert (
        by_url[("GET", f"{_MINERU_HOST}/v1/files/file-mj/content")].headers["authorization"]
        == "Bearer mineru-secret"
    )
    assert result.extraction is not None


@pytest.mark.parametrize(
    ("job_status", "file_status", "warning_code"),
    [
        ("failed", "failed", "mineru_external_job_failed"),
        ("canceled", "failed", "mineru_external_job_canceled"),
        ("partial", "failed", "mineru_external_job_failed"),
    ],
)
def test_mineru_v1_unsuccessful_job_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    job_status: str,
    file_status: str,
    warning_code: str,
) -> None:
    seen: list[httpx.Request] = []
    _install_transport(
        monkeypatch,
        _mineru_handler(seen, job_statuses=(job_status,), file_status=file_status),
    )

    with pytest.raises(ExternalParserCallError) as exc_info:
        ExternalParserClient(_mineru_settings()).parse(
            "mineru", b"%PDF-1.7", _profile(), "application/pdf"
        )

    assert exc_info.value.warning_code == warning_code
    assert not any(request.url.path.startswith("/v1/files/") for request in seen)


def test_mineru_v1_job_timeout_cancels_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.clients import external_parser

    seen: list[httpx.Request] = []
    clock = iter([0.0, 5.0, 11.0])
    monkeypatch.setattr(external_parser, "_poll_clock", lambda: next(clock))
    _install_transport(monkeypatch, _mineru_handler(seen, job_statuses=("running",)))

    with pytest.raises(ExternalParserCallError) as exc_info:
        ExternalParserClient(_mineru_settings(rag_parser_mineru_job_timeout_seconds=10)).parse(
            "mineru", b"%PDF-1.7", _profile(), "application/pdf"
        )

    assert exc_info.value.warning_code == "mineru_external_timeout"
    assert seen[-1].method == "POST"
    assert seen[-1].url.path == "/v1/parse/jobs/job-1/cancel"


def test_mineru_v1_uses_configured_tier_for_pdf_and_flash_for_office(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    _install_transport(monkeypatch, _mineru_handler(seen))
    client = ExternalParserClient(_mineru_settings(rag_parser_mineru_tier="standard"))
    office = SourceProfile(
        original_file_name="manual.docx",
        sanitized_file_name="manual.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size_bytes=3,
        content_sha256="0" * 64,
        modality=SourceModality.OFFICE,
        parser_profile="office",
    )

    client.parse("mineru", b"%PDF-1.7", _profile(), "application/pdf")
    client.parse("mineru", b"PK", office, office.content_type)

    tiers = [
        json.loads(request.content)["tier"]
        for request in seen
        if request.method == "POST" and request.url.path == "/v1/parse/jobs"
    ]
    assert tiers == ["standard", "flash"]


def test_mineru_v1_rejects_unknown_middle_json_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def download(_request: httpx.Request) -> httpx.Response:
        # 3.x の middle.json（pdf_info）は 4.0 の Middle JSON ではない。
        return httpx.Response(200, json={"pdf_info": [], "_backend": "pipeline"})

    _install_transport(monkeypatch, _mineru_handler(seen, download=download))

    with pytest.raises(ExternalParserCallError) as exc_info:
        ExternalParserClient(_mineru_settings()).parse(
            "mineru", b"%PDF-1.7", _profile(), "application/pdf"
        )

    assert exc_info.value.warning_code == "mineru_external_invalid_response"


@pytest.mark.parametrize("override", [None, "合成の読み取り指示"])
def test_dots_openai_layout_json_renders_pdf_pages_with_bounded_calls(
    monkeypatch: pytest.MonkeyPatch,
    override: str | None,
) -> None:
    calls: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                [
                                    {
                                        "bbox": [1, 2, 30, 20],
                                        "category": "Table",
                                        "text": "<table><tr><td>A</td></tr></table>",
                                    }
                                ]
                            )
                        },
                    }
                ]
            },
        )

    _install_transport(monkeypatch, handle)
    result = ExternalParserClient(
        Settings(
            rag_parser_dots_ocr_api_host="https://dots.example.com/v1/",
            rag_parser_dots_ocr_model="dots-model",
            rag_parser_dots_ocr_pdf_workers=2,
        ),
        dots_ocr_prompt=override,
    ).parse("dots_ocr", _pdf(), _profile(), "application/pdf")

    if override:
        assert all(override in str(call["messages"]) for call in calls)
    assert len(calls) == 2
    assert all(call["model"] == "dots-model" for call in calls)
    assert result.extraction is not None
    assert [element.page_number for element in result.extraction.elements] == [1, 2]
    assert all(element.content_kind == "table" for element in result.extraction.elements)
    assert len(result.extraction.pages) == 2
    assert all(page.width and page.height for page in result.extraction.pages)


def test_dots_picture_without_text_is_kept_only_as_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps([{"bbox": [1, 2, 30, 20], "category": "Picture"}])
                        },
                    }
                ]
            },
        )

    _install_transport(monkeypatch, handle)
    result = ExternalParserClient(
        Settings(
            rag_parser_dots_ocr_api_host="https://dots.example.com/v1/",
            rag_parser_dots_ocr_model="dots-model",
        )
    ).parse("dots_ocr", _pdf(), _profile(), "application/pdf")

    assert result.extraction is not None
    assert result.extraction.raw_text == ""
    assert result.extraction.elements == []
    assert len(result.extraction.assets) == 2
    assert all(asset.kind == "picture" for asset in result.extraction.assets)
    assert all(asset.alt_text is None for asset in result.extraction.assets)
    assert result.extraction.parser_artifacts["adapter_asset_count"] == 2


def test_dots_renders_only_one_worker_batch_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered: list[int] = []
    rendered_at_call: list[int] = []
    parser = ExternalParserClient(
        Settings(
            rag_parser_dots_ocr_api_host="https://dots.example.com",
            rag_parser_dots_ocr_model="dots-model",
            rag_parser_dots_ocr_pdf_workers=2,
        )
    )

    def source_images(*_args: object) -> object:
        for number in range(1, 6):
            rendered.append(number)
            yield _RenderedPage(number, b"png", 120, 80)

    def openai_chat(*_args: object, **_kwargs: object) -> str:
        rendered_at_call.append(len(rendered))
        return json.dumps([{"bbox": [1, 2, 30, 20], "category": "Text", "text": "本文"}])

    monkeypatch.setattr(parser, "_source_images", source_images)
    monkeypatch.setattr(parser, "_openai_chat", openai_chat)

    result = parser.parse("dots_ocr", b"%PDF", _profile(), "application/pdf")

    assert sorted(rendered_at_call) == [2, 2, 4, 4, 5]
    assert result.extraction is not None
    assert [page.page_number for page in result.extraction.pages] == [1, 2, 3, 4, 5]


def test_status_health_models_retry_and_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"status": "ok", "version": "4.0.11"})
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"detail": "loading"})
        return httpx.Response(200, json={"data": [{"id": "served-model"}]})

    _install_transport(monkeypatch, handle)
    settings = Settings(
        rag_parser_mineru_api_host="https://mineru.example.com",
        rag_parser_dots_ocr_api_host="https://dots.example.com/v1",
        rag_parser_dots_ocr_model="missing-model",
        rag_http_service_retry_attempts=2,
        rag_http_service_retry_initial_delay_seconds=0,
    )
    parser = ExternalParserClient(settings)

    mineru = parser.status("mineru")
    dots = parser.status("dots_ocr")

    assert mineru.status == "available"
    assert mineru.version == "4.0.11"
    assert dots.status == "model_missing"
    assert attempts == 2


@pytest.mark.parametrize(
    ("payload", "warning_code"),
    [
        ({"choices": []}, "dots_ocr_external_invalid_response"),
        (
            {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
            "dots_ocr_external_truncated",
        ),
        (
            {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
            "dots_ocr_external_invalid_response",
        ),
    ],
)
def test_openai_invalid_truncated_and_empty_results_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    warning_code: str,
) -> None:
    _install_transport(monkeypatch, lambda _request: httpx.Response(200, json=payload))
    parser = ExternalParserClient(
        Settings(
            rag_parser_dots_ocr_api_host="https://dots.example.com",
            rag_parser_dots_ocr_model="dots-model",
        )
    )

    with pytest.raises(ExternalParserCallError) as exc_info:
        parser.parse(
            "dots_ocr", b"png", _profile("scan.png", content_type="image/png"), "image/png"
        )

    assert exc_info.value.warning_code == warning_code


def test_api_key_is_not_exposed_in_error_or_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "never-log-this-key"
    _install_transport(
        monkeypatch,
        lambda _request: httpx.Response(401, json={"detail": f"invalid {secret}"}),
    )
    parser = ExternalParserClient(
        Settings(
            rag_parser_dots_ocr_api_host="https://dots.example.com",
            rag_parser_dots_ocr_model="dots-model",
            rag_parser_dots_ocr_api_key=secret,
            rag_http_service_retry_attempts=1,
        )
    )

    with caplog.at_level(logging.WARNING), pytest.raises(ExternalParserCallError) as exc_info:
        parser.parse(
            "dots_ocr", b"png", _profile("scan.png", content_type="image/png"), "image/png"
        )

    assert secret not in str(exc_info.value)
    assert secret not in caplog.text


# dots.ocr の smart_resize(公式の dots_ocr/utils/image_utils.py)で計算した、モデルへ入る画像の寸法。
# A4 を 200 dpi で描いた寸法は丸めで数 px、max_pixels(11289600)を超える寸法は縮小される(#502)。
@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((1653, 2339), (1652, 2352)),
        ((1200, 800), (1204, 812)),
        ((3306, 4678), (2800, 3976)),
        ((10, 10), (56, 56)),
    ],
    ids=["a4_200dpi", "image_1200x800", "a4_400dpi_over_max_pixels", "under_min_pixels"],
)
def test_dots_model_input_size_matches_official_smart_resize(
    size: tuple[int, int], expected: tuple[int, int]
) -> None:
    assert _dots_model_input_size(*size) == expected


def test_dots_bbox_is_mapped_from_model_input_to_rendered_page_pixels() -> None:
    """dots.ocr の bbox(smart_resize した入力画像の px)を、描いたページ画像の px へ戻す。"""
    page = _RenderedPage(1, b"", 3306, 4678)
    # 図の実際の位置 [556, 1111, 2556, 2445](ページ画像の px)をモデルの入力の寸法で表した値。
    elements: list[dict[str, object]] = [{"bbox": [471, 945, 2165, 2078], "category": "Picture"}]

    mapped = _dots_bboxes_to_page_px(elements, page)

    bbox = mapped[0]["bbox"]
    assert isinstance(bbox, list)
    assert [round(value) for value in bbox] == [556, 1112, 2556, 2445]


def test_dots_300dpi_a4_matches_ai_foundations_lab_observation() -> None:
    """ai-foundations-lab の dots.mocr の検証(300 dpi の A4)と、この換算が合うこと(#512)。

    lab(engchina/ai-foundations-lab、commit 572e9fa の 20260819/README.md)は、300 dpi の A4 の
    画像パッチを約 44,000 と記録している。smart_resize は縮小せず 28 の倍数へ丸めるだけなので、
    lab のように bbox をそのままページ画像の px として使っても 0.5% 未満の差で重なって見える。
    その丸めの差(右下の角で 12px / 9px)も、ここの換算で戻す。
    """
    width, height = 2480, 3509  # A4(595x842 pt)を 300 dpi で描いた寸法
    input_width, input_height = _dots_model_input_size(width, height)
    assert (input_width, input_height) == (2492, 3500)
    assert input_width * input_height // (14 * 14) == 44_500

    elements: list[dict[str, object]] = [
        {"bbox": [0, 0, input_width, input_height], "category": "Picture"}
    ]
    bbox = _dots_bboxes_to_page_px(elements, _RenderedPage(1, b"", width, height))[0]["bbox"]
    assert bbox == [0.0, 0.0, float(width), float(height)]
    # lab の扱い(そのまま使い、ページ内へ収める)との差は丸めの分だけ。
    assert abs(input_width - width) <= 14 and abs(input_height - height) <= 14


def test_dots_bbox_is_not_mapped_without_page_size() -> None:
    elements: list[dict[str, object]] = [{"bbox": [1, 2, 30, 20], "category": "Text"}]

    assert _dots_bboxes_to_page_px(elements, _RenderedPage(1, b"png", 0, 0)) == [
        {"bbox": [1, 2, 30, 20], "category": "Text"}
    ]


def test_dots_image_file_bbox_and_page_size_use_original_image_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """画像ファイルは元画像の寸法をページの寸法にし、bbox を元画像の px へ戻す(#502)。"""
    buffer = io.BytesIO()
    Image.new("RGB", (1200, 800), "white").save(buffer, format="PNG")
    layout = [{"bbox": [100, 101, 1104, 710], "category": "Picture"}]
    _install_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(layout)}}]
            },
        ),
    )

    result = ExternalParserClient(
        Settings(
            rag_parser_dots_ocr_api_host="https://dots.example.com/v1/",
            rag_parser_dots_ocr_model="dots-model",
        )
    ).parse(
        "dots_ocr",
        buffer.getvalue(),
        _profile("screen.png", content_type="image/png"),
        "image/png",
    )

    assert result.extraction is not None
    page = result.extraction.pages[0]
    assert (page.width, page.height) == (1200.0, 800.0)
    asset = result.extraction.assets[0]
    assert asset.bbox is not None
    assert [round(value) for value in asset.bbox] == [100, 100, 1100, 700]
