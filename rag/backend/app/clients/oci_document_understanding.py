"""OCI Document Understanding クライアント(共有 core の Settings adapter)。

正本は共有 package `rag_parser_core.oci_document_understanding`。backend は Settings から
`OciDocumentUnderstandingConfig` を組み立てて in-process 実行する薄い adapter として本
モジュールを使う(従来の import パス `app.clients.oci_document_understanding` を維持)。
parser マイクロサービス(`services/parsers/oci_document_understanding`)は共有 core を
env 由来 config で使う。
"""

from __future__ import annotations

from rag_parser_core.oci_document_understanding import (
    DocumentUnderstandingSdkProtocol,
    ObjectStorageSdkProtocol,
    OciDocumentUnderstandingConfig,
    OciDocumentUnderstandingService,
    document_understanding_result_to_payload,
)

from app.config import Settings

__all__ = [
    "DocumentUnderstandingSdkProtocol",
    "ObjectStorageSdkProtocol",
    "OciDocumentUnderstandingClient",
    "OciDocumentUnderstandingConfig",
    "config_from_settings",
    "document_understanding_result_to_payload",
]


def config_from_settings(settings: Settings) -> OciDocumentUnderstandingConfig:
    """backend Settings から共有 core 用の config を組み立てる。"""
    return OciDocumentUnderstandingConfig(
        compartment_id=settings.oci_document_understanding_compartment_id,
        fallback_compartment_id=settings.oci_compartment_id,
        namespace=settings.oci_document_understanding_namespace,
        fallback_namespace=settings.object_storage_namespace,
        input_bucket=settings.oci_document_understanding_input_bucket,
        fallback_input_bucket=settings.object_storage_bucket,
        output_bucket=settings.oci_document_understanding_output_bucket,
        input_prefix=settings.oci_document_understanding_input_prefix,
        output_prefix=settings.oci_document_understanding_output_prefix,
        language=settings.oci_document_understanding_language,
        features=list(settings.oci_document_understanding_features),
        poll_interval_seconds=float(settings.oci_document_understanding_poll_interval_seconds),
        timeout_seconds=float(settings.oci_document_understanding_timeout_seconds),
        oci_config_file=settings.oci_config_file,
        oci_config_profile=settings.oci_config_profile,
        oci_region=settings.oci_region,
        object_storage_region=(
            settings.oci_document_understanding_object_storage_region
            or settings.oci_region
            or settings.object_storage_region
        ),
    )


class OciDocumentUnderstandingClient(OciDocumentUnderstandingService):
    """Settings から config を組み立てて共有 service を駆動する backend adapter。"""

    def __init__(
        self,
        settings: Settings,
        *,
        document_client: DocumentUnderstandingSdkProtocol | None = None,
        object_storage_client: ObjectStorageSdkProtocol | None = None,
    ) -> None:
        super().__init__(
            config_from_settings(settings),
            document_client=document_client,
            object_storage_client=object_storage_client,
        )
