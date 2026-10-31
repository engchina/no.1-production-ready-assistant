"""ParserServiceClient(parser マイクロサービス HTTP クライアント)のテスト。

成功時は ParseResponse を ParserRegistryResult へ戻し、接続失敗/未設定/不正応答時は
warning 付き fallback(extraction=None)へ縮退することを検証する。
"""

from __future__ import annotations

import httpx
import pytest
from rag_parser_core.extraction import DocumentElement, StructuredExtraction
from rag_parser_core.registry import ParserRegistryResult
from rag_parser_core.result import ParseResponse

from app.clients.parser_service import (
    ParserServiceClient,
    ParserServiceUnavailableError,
    split_parser_warning,
    supported_formats_label,
)
from app.config import Settings
from app.schemas.document import SourceModality, SourceProfile


def _profile() -> SourceProfile:
    return SourceProfile(
        original_file_name="a.pdf",
        sanitized_file_name="a.pdf",
        content_type="application/pdf",
        file_size_bytes=3,
        content_sha256="0" * 64,
        modality=SourceModality.PDF,
        parser_profile="pdf",
    )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: httpx.MockTransport,
) -> None:
    """ParserServiceClient が内部で作る httpx.Client に MockTransport を注入する。"""
    original = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("timeout", None)
        return original(transport=handler)

    monkeypatch.setattr(httpx, "Client", factory)


def test_runner_returns_extraction_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction = StructuredExtraction(
        raw_text="本文",
        document_type="PDF",
        confidence=1.0,
        elements=[
            DocumentElement(
                kind="text",
                text="本文",
                order=0,
                element_id="el-0",
                content_kind="text",
                source_parser="docling_adapter",
                page_number=1,
            )
        ],
    )
    response = ParseResponse(
        extraction=extraction,
        parser_backend="docling",
        parser_version="docling:2.103.0",
        template="structure_aware",
    )

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/parse"
        return httpx.Response(200, json=response.model_dump(mode="json"))

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(Settings(rag_parser_docling_service_url="http://127.0.0.1:18020"))
    result = client.runner("docling", b"abc", _profile(), "application/pdf")

    assert isinstance(result, ParserRegistryResult)
    assert result.fallback_used is False
    assert result.extraction is not None
    assert result.extraction.elements[0].text == "本文"


def test_runner_falls_back_when_service_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(
        Settings(
            rag_parser_docling_service_url="http://127.0.0.1:18020",
            rag_http_service_retry_attempts=1,
        )
    )
    result = client.runner("docling", b"abc", _profile(), "application/pdf")

    assert result.extraction is None
    assert result.fallback_used is True
    assert "docling_adapter_service_unreachable" in result.warnings


def test_runner_fail_fast_raises_when_service_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(
        Settings(
            rag_parser_unstructured_service_url="http://127.0.0.1:18022",
            rag_http_service_retry_attempts=1,
        )
    )

    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        client.runner(
            "unstructured",
            b"abc",
            _profile(),
            "application/pdf",
            fail_fast=True,
        )

    assert exc_info.value.backend == "unstructured"
    assert exc_info.value.reason == "unreachable"
    assert "選択した文書解析サービス（Unstructured）に接続できません" in str(exc_info.value)


def test_runner_fail_fast_raises_when_service_url_is_unconfigured() -> None:
    settings = Settings(rag_parser_unstructured_service_url="")
    client = ParserServiceClient(settings)

    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        client.runner(
            "unstructured",
            b"%PDF",
            _profile(),
            "application/pdf",
            fail_fast=True,
        )

    assert exc_info.value.backend == "unstructured"
    assert exc_info.value.reason == "unconfigured"
    assert "Unstructured）の接続先 URL が未設定です" in str(exc_info.value)


def test_runner_retries_retryable_status_then_returns_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = StructuredExtraction(raw_text="retry success", document_type="PDF", confidence=1.0)
    response = ParseResponse(
        extraction=extraction,
        parser_backend="unstructured",
        parser_version="unstructured:0.23.1",
    )
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, text="starting")
        return httpx.Response(200, json=response.model_dump(mode="json"))

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(
        Settings(
            rag_parser_unstructured_service_url="http://127.0.0.1:18022",
            rag_http_service_retry_attempts=3,
            rag_http_service_retry_initial_delay_seconds=0,
        )
    )

    result = client.runner("unstructured", b"abc", _profile(), "application/pdf")

    assert calls == 3
    assert result.fallback_used is False
    assert result.extraction is not None
    assert result.extraction.raw_text == "retry success"


def test_runner_does_not_retry_non_retryable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(
        Settings(
            rag_parser_unstructured_service_url="http://127.0.0.1:18022",
            rag_http_service_retry_attempts=3,
            rag_http_service_retry_initial_delay_seconds=0,
        )
    )

    result = client.runner("unstructured", b"abc", _profile(), "application/pdf")

    assert calls == 1
    assert result.fallback_used is True
    assert result.extraction is None


def test_runner_fail_fast_reports_http_status_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="starting")

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(
        Settings(
            rag_parser_unstructured_service_url="http://127.0.0.1:18022",
            rag_http_service_retry_attempts=2,
            rag_http_service_retry_initial_delay_seconds=0,
        )
    )

    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        client.runner(
            "unstructured",
            b"abc",
            _profile(),
            "application/pdf",
            fail_fast=True,
        )

    assert calls == 2
    assert exc_info.value.reason == "http_error"
    assert exc_info.value.status_code == 503
    assert exc_info.value.attempts == 2
    assert "/parse が HTTP 503 を返しました" in str(exc_info.value)


def test_runner_falls_back_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(Settings(rag_http_service_retry_attempts=1))
    result = client.runner("unstructured", b"abc", _profile(), "application/pdf")

    assert result.extraction is None
    assert result.fallback_used is True
    assert "unstructured_adapter_service_unreachable" in result.warnings


def test_runner_fail_fast_raises_when_url_unconfigured() -> None:
    client = ParserServiceClient(Settings(rag_parser_docling_service_url=""))

    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        client.runner("docling", b"abc", _profile(), "application/pdf", fail_fast=True)

    assert exc_info.value.reason == "unconfigured"
    assert "接続先 URL が未設定です" in str(exc_info.value)


def test_runner_falls_back_when_url_unconfigured() -> None:
    client = ParserServiceClient(Settings(rag_parser_docling_service_url=""))
    result = client.runner("docling", b"abc", _profile(), "application/pdf")

    assert result.extraction is None
    assert result.fallback_used is True
    assert "docling_adapter_service_unconfigured" in result.warnings


def test_runner_fail_fast_classifies_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """破損ファイル由来の失敗は adapter_failed と区別し、破損向け文言を返す。"""
    response = ParseResponse(
        extraction=None,
        parser_backend="docling",
        parser_version="service_unavailable",
        fallback_used=True,
        warnings=["docling_adapter_invalid_input"],
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response.model_dump(mode="json"))

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(Settings(rag_parser_docling_service_url="http://127.0.0.1:18020"))

    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        client.runner("docling", b"not-a-pdf", _profile(), "application/pdf", fail_fast=True)

    assert exc_info.value.reason == "adapter_invalid_input"
    assert exc_info.value.warning_code == "docling_adapter_invalid_input"
    assert "ファイルが破損しているか" in str(exc_info.value)


def test_source_unsupported_message_lists_supported_formats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非対応形式の文言には backend の対応形式一覧を含める。"""
    response = ParseResponse(
        extraction=None,
        parser_backend="docling",
        parser_version="service_unavailable",
        fallback_used=True,
        warnings=["docling_adapter_source_unsupported"],
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response.model_dump(mode="json"))

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(Settings(rag_parser_docling_service_url="http://127.0.0.1:18020"))

    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        client.runner("docling", b"abc", _profile(), "text/markdown", fail_fast=True)

    assert exc_info.value.reason == "adapter_source_unsupported"
    assert f"対応形式: {supported_formats_label('docling')}" in str(exc_info.value)


@pytest.mark.parametrize(
    ("warning", "expected"),
    [
        ("docling_adapter_failed", ("docling_adapter_failed", None)),
        ("docling_adapter_failed: ImportError", ("docling_adapter_failed", "ImportError")),
        ("docling_unavailable:  ImportError ", ("docling_unavailable", "ImportError")),
        (
            "docling_unavailable: 応答が\n途中で 終わりました",
            ("docling_unavailable", "応答が 途中で 終わりました"),
        ),
        ("docling_adapter_failed:", ("docling_adapter_failed", None)),
    ],
)
def test_split_parser_warning(warning: str, expected: tuple[str, str | None]) -> None:
    assert split_parser_warning(warning) == expected


def _raise_for_parse_warning(
    monkeypatch: pytest.MonkeyPatch, warning: str
) -> ParserServiceUnavailableError:
    response = ParseResponse(
        extraction=None,
        parser_backend="docling",
        parser_version="docling",
        fallback_used=True,
        template="docling_fallback",
        warnings=[warning],
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response.model_dump(mode="json"))

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(Settings(rag_parser_docling_service_url="http://127.0.0.1:18020"))
    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        client.runner("docling", b"%PDF", _profile(), "application/pdf", fail_fast=True)
    return exc_info.value


@pytest.mark.parametrize(
    ("warning", "expected_code"),
    [
        # rag_parser_core の adapter 実行(unstructured など)が返す形式。
        ("docling_adapter_failed: ImportError", "docling_adapter_failed"),
        # parser-docling の service_failure_warning が返す形式。
        ("docling_unavailable: ImportError", "docling_unavailable"),
    ],
)
def test_runner_fail_fast_surfaces_exception_type(
    monkeypatch: pytest.MonkeyPatch, warning: str, expected_code: str
) -> None:
    """解析失敗の warning に付いた例外の型名を、利用者向けの文言に添える(#310)。"""
    error = _raise_for_parse_warning(monkeypatch, warning)

    assert error.reason == "adapter_failed"
    assert error.warning_code == expected_code
    assert error.error_detail == "ImportError"
    message = str(error)
    assert "解析処理が失敗しました（原因: ImportError）。" in message
    assert f"エラーコード: {expected_code}" in message
    assert "parser-docling のログを確認" in message


def test_runner_fail_fast_legacy_adapter_failed_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """型名の無い従来の warning も adapter_failed として扱う(原因は添えない)。"""
    error = _raise_for_parse_warning(monkeypatch, "docling_adapter_failed")

    assert error.reason == "adapter_failed"
    assert error.warning_code == "docling_adapter_failed"
    assert error.error_detail is None
    assert "解析処理が失敗しました。" in str(error)
    assert "原因:" not in str(error)


def test_runner_sends_language_pack_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """画面で上書きした言語資源(#2181)を、組み込みとの差だけ解析のサービスへ送る。"""
    from rag_parser_core.language import builtin_pack, resolve, set_default_pack

    bodies: list[bytes] = []
    response = ParseResponse(
        extraction=StructuredExtraction(raw_text="本文", document_type="PDF", confidence=1.0),
        parser_backend="docling",
        parser_version="docling:2.103.0",
        template="structure_aware",
    )

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json=response.model_dump(mode="json"))

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    client = ParserServiceClient(Settings(rag_parser_docling_service_url="http://127.0.0.1:18020"))
    client.runner("docling", b"abc", _profile(), "application/pdf")
    assert b'name="language_pack"' not in bodies[-1]

    set_default_pack(resolve(builtin_pack(), {"numbers": {"article_units": ["条", "款"]}}))
    try:
        client.runner("docling", b"abc", _profile(), "application/pdf")
    finally:
        set_default_pack(None)
    assert b'name="language_pack"' in bodies[-1]
    assert '"article_units": ["条", "款"]'.encode() in bodies[-1]
