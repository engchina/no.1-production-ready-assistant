"""Optional parser adapter runtime readiness.

Docling / Unstructured は任意依存として扱うため、
flag と実際の package installation 状態を分けて非機密に表示する。
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pr_backend_core.internal_http import http_client_options

from app.clients.http_retry import request_with_retry, retry_config_from_settings
from app.config import ParserAdapterBackend, Settings

logger = logging.getLogger(__name__)

# adapter ごとの parser サービス URL を持つ Settings フィールド名。
_SERVICE_URL_FIELDS: dict[str, str] = {
    "docling": "rag_parser_docling_service_url",
    "unstructured": "rag_parser_unstructured_service_url",
}

ParserAdapterName = Literal["docling", "unstructured"]
ParserAdapterStatus = Literal["active", "available", "disabled", "ignored", "missing"]


@dataclass(frozen=True)
class ParserAdapterPackageSpec:
    """optional adapter の import 名と配布 package 名を分けて管理する。"""

    import_name: str
    distribution_names: tuple[str, ...]
    install_package: str


ADAPTER_PACKAGES: dict[ParserAdapterName, ParserAdapterPackageSpec] = {
    "docling": ParserAdapterPackageSpec(
        import_name="docling",
        distribution_names=("docling",),
        install_package="docling==2.103.0",
    ),
    "unstructured": ParserAdapterPackageSpec(
        import_name="unstructured",
        distribution_names=("unstructured",),
        install_package="unstructured[all-docs]==0.23.1",
    ),
}
ADAPTER_ORDER: tuple[ParserAdapterName, ...] = ("docling", "unstructured")
ADAPTER_BACKENDS: tuple[ParserAdapterBackend, ...] = ("local", "docling", "unstructured")


@dataclass(frozen=True)
class ParserAdapterRuntimeStatus:
    """1 adapter の flag / package / selection 状態。"""

    backend: ParserAdapterName
    package_name: str
    import_name: str
    distribution_name: str | None
    install_package: str
    enabled: bool
    selected: bool
    installed: bool
    status: ParserAdapterStatus
    version: str | None = None
    warning_code: str | None = None


@dataclass(frozen=True)
class ParserAdapterRuntimeSettings:
    """parser adapter feature flags の非機密 runtime snapshot。"""

    adapter_backend: ParserAdapterBackend
    effective_order: tuple[ParserAdapterName, ...]
    adapters: tuple[ParserAdapterRuntimeStatus, ...]


def parser_adapter_runtime_settings(settings: Settings) -> ParserAdapterRuntimeSettings:
    """Settings から adapter readiness snapshot を作る。"""
    adapter_backend = _normalize_adapter_backend(settings.rag_parser_adapter_backend)
    effective_order = _effective_adapter_order(settings, adapter_backend)
    adapters = tuple(
        _adapter_status(settings, backend=backend, adapter_backend=adapter_backend)
        for backend in ADAPTER_ORDER
    )
    return ParserAdapterRuntimeSettings(
        adapter_backend=adapter_backend,
        effective_order=effective_order,
        adapters=adapters,
    )


def _effective_adapter_order(
    settings: Settings,
    adapter_backend: ParserAdapterBackend,
) -> tuple[ParserAdapterName, ...]:
    if adapter_backend in ADAPTER_PACKAGES:
        return (adapter_backend,) if _adapter_flag(settings, adapter_backend) else ()
    return ()


def _adapter_status(
    settings: Settings,
    *,
    backend: ParserAdapterName,
    adapter_backend: ParserAdapterBackend,
) -> ParserAdapterRuntimeStatus:
    package_spec = ADAPTER_PACKAGES[backend]
    enabled = _adapter_flag(settings, backend)
    explicitly_selected = adapter_backend == backend
    selected = explicitly_selected or backend in _effective_adapter_order(settings, adapter_backend)
    installed: bool
    version: str | None
    distribution_name: str | None
    if settings.rag_parser_readiness_probe_enabled:
        # 本番: parser サービスの /health で導入状況・version を解決する。
        installed, version, distribution_name = _probe_service_health(settings, backend)
    else:
        # 開発/テスト: backend プロセス内の import 検出にフォールバックする。
        installed, version, distribution_name = _package_info(
            package_spec.import_name,
            package_spec.distribution_names,
        )
    status: ParserAdapterStatus
    warning_code: str | None = None

    if selected and not enabled:
        status = "disabled"
        warning_code = "adapter_feature_flag_disabled"
    elif selected and installed:
        status = "active"
    elif selected:
        status = "missing"
        warning_code = "adapter_package_missing"
    elif enabled:
        status = "ignored"
        warning_code = "adapter_flag_ignored_by_backend"
    elif installed:
        status = "available"
    else:
        status = "disabled"

    return ParserAdapterRuntimeStatus(
        backend=backend,
        package_name=package_spec.import_name,
        import_name=package_spec.import_name,
        distribution_name=distribution_name,
        install_package=package_spec.install_package,
        enabled=enabled,
        selected=selected,
        installed=installed,
        status=status,
        version=version,
        warning_code=warning_code,
    )


def _adapter_flag(settings: Settings, backend: ParserAdapterName) -> bool:
    return bool(getattr(settings, f"rag_parser_{backend}_enabled", False))


def _normalize_adapter_backend(value: object) -> ParserAdapterBackend:
    normalized = str(value).casefold()
    if normalized in ADAPTER_BACKENDS:
        return normalized
    return "local"


def _package_info(
    import_name: str,
    distribution_names: Sequence[str],
) -> tuple[bool, str | None, str | None]:
    if importlib.util.find_spec(import_name) is None:
        return False, None, None
    for distribution_name in distribution_names:
        try:
            version = importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            continue
        return True, version, distribution_name
    return True, None, None


def _probe_service_health(
    settings: Settings,
    backend: ParserAdapterName,
) -> tuple[bool, str | None, str | None]:
    """parser サービスの /health を best-effort で問い合わせ、(installed, version, dist) を返す。

    サービス未設定・未達・degraded のときは installed=False を返し、readiness は missing 表示に
    縮退する(httpx は遅延 import、短 timeout、例外は握り潰す)。
    """
    field = _SERVICE_URL_FIELDS.get(backend)
    url = str(getattr(settings, field, "") or "").strip().rstrip("/") if field else ""
    if not url:
        return False, None, None
    try:
        import httpx

        with httpx.Client(
            timeout=float(settings.rag_parser_readiness_probe_timeout_seconds),
            **http_client_options(url),
        ) as c:
            response = request_with_retry(
                c,
                "GET",
                f"{url}/health",
                retry=retry_config_from_settings(settings),
                logger=logger,
                log_extra={"parser_backend": backend, "service_url": url},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - readiness は到達不可でも安全に missing 表示する
        logger.debug("parser readiness probe failed: backend=%s url=%s error=%s", backend, url, exc)
        return False, None, None
    installed = str(payload.get("status", "")).lower() == "ok"
    version = payload.get("package_version")
    distribution_name = payload.get("package_name")
    return (
        installed,
        version if isinstance(version, str) else None,
        (distribution_name if isinstance(distribution_name, str) else None),
    )
