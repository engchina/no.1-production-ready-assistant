"""OCI Document Understanding parser マイクロサービス。

共有 core(rag_parser_core.oci_document_understanding)を env 由来 config で駆動し、
OCI Document Understanding の非同期 processor job で OCR/表抽出した結果を
`StructuredExtraction`(ParseResponse wire 形式)で返す。OCI 認証はメインプロジェクトの
OCI env + ~/.oci マウントを継承する(個別設定なし)。未設定/失敗時は extraction=None の
fallback を返し、backend 側で既存 in-process / ローカルフローへ安全に縮退する。
"""

from rag_parser_core.extraction import StructuredExtraction
from rag_parser_core.oci_document_understanding import (
    OciDocumentUnderstandingConfig,
    OciDocumentUnderstandingService,
)
from rag_parser_core.result import ParseResponse
from rag_parser_core.service import create_service_parse_app
from rag_parser_core.source import SourceProfile

_BACKEND = "oci_document_understanding"
_CONFIG = OciDocumentUnderstandingConfig.from_env()


def _service() -> OciDocumentUnderstandingService:
    return OciDocumentUnderstandingService(_CONFIG)


async def _parse(
    source_bytes: bytes,
    content_type: str,
    source_profile: SourceProfile | None,
    document_id: str,
    prompt: str,
) -> ParseResponse:
    _ = (source_profile, prompt)  # 入力 object 名は document_id を使う。DU は prompt 不要。
    payload = await _service().analyze(
        source_bytes, content_type=content_type, document_id=document_id
    )
    if payload is None:
        return ParseResponse(
            extraction=None,
            parser_backend=_BACKEND,
            parser_version=_BACKEND,
            fallback_used=True,
            template=f"{_BACKEND}_fallback",
            warnings=[f"{_BACKEND}_unavailable"],
        )
    return ParseResponse(
        extraction=StructuredExtraction.model_validate(payload),
        parser_backend=_BACKEND,
        parser_version=_BACKEND,
        template=_BACKEND,
    )


app = create_service_parse_app(
    backend=_BACKEND,
    parse=_parse,
    is_configured=lambda: _service().is_configured(),
    title="parser-oci-document-understanding",
)
