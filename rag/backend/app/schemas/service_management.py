"""サービス管理(前処理 / Parser マイクロサービスの稼働可視化・起動/停止。systemd。#286)スキーマ。"""

from __future__ import annotations

from typing import Literal

from pr_backend_core.config import Environment
from pydantic import BaseModel, Field

from app.services.catalog import (
    ServiceCategory,
    ServiceExecutionPolicy,
    ServiceProfile,
)
from app.services.status import ServiceRuntimeStatus

DeploymentMode = Environment
ServiceLogsSource = Literal["journald"]


class ServiceModelCacheData(BaseModel):
    """モデル DL を行うサービスのキャッシュの場所(読み取り専用表示)。"""

    path: str = Field(
        description="サービスの実行ユーザーのキャッシュ親ディレクトリ(HF・docling などのモデル)。",
    )
    editable: Literal[False] = Field(
        default=False,
        description="キャッシュの場所は固定。UI からは編集不可。",
    )


class ServiceCatalogItemData(BaseModel):
    """1 マイクロサービスの非機密カタログ情報(稼働プローブなし)。"""

    service_id: str
    category: ServiceCategory
    profile: ServiceProfile
    label_key: str
    execution_policy: ServiceExecutionPolicy = Field(
        description="停止時・未使用時の runtime 契約。fallback 境界の UI 表示に使う。",
    )
    configured: bool = Field(
        description="base URL が設定済みか(未設定なら status=unconfigured)。",
    )
    systemd_unit: str = Field(
        description="起動/停止・ログに使う systemd の unit 名。",
    )
    model_cache: ServiceModelCacheData | None = Field(
        default=None,
        description="モデル DL を行うサービスのキャッシュマウント情報。なければ None。",
    )


class ServiceStatusData(ServiceCatalogItemData):
    """1 マイクロサービスの非機密ステータス。"""

    status: ServiceRuntimeStatus


class ServiceCatalogData(BaseModel):
    """サービス一覧の静的メタデータ + 制御可否 + 配備モード。"""

    control_enabled: bool = Field(
        description="起動/停止制御が有効か。False なら可視化のみ。local は自動的に有効。",
    )
    deployment_mode: DeploymentMode = Field(
        description=(
            "PLATFORM_ENVIRONMENT 由来。全環境で systemd の unit を操作する。"
            "local は起動/停止を自動で有効にする。"
        ),
    )
    services: list[ServiceCatalogItemData] = Field(default_factory=list)


class ServiceControlResultData(BaseModel):
    """起動/停止実行後の結果(更新後ステータス込み)。"""

    service_id: str
    action: str
    status: ServiceRuntimeStatus


class ServiceLogsData(BaseModel):
    """サービスログの末尾。"""

    service_id: str
    source: ServiceLogsSource
    lines: int = Field(description="取得したログ末尾の最大行数。")
    content: str = Field(description="ログ本文。ログが無い場合は空文字。")
