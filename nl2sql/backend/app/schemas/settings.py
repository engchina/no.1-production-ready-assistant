"""設定 API のスキーマ。secret はレスポンスに含めない。"""

from typing import Literal

# データベース設定の schema は3製品共通（pr_system_settings。#108）。互換のため re-export する。
from pr_system_settings.database import DatabaseConnectionSecurity as DatabaseConnectionSecurity
from pr_system_settings.database import DatabaseConnectionTestResult as DatabaseConnectionTestResult
from pr_system_settings.database import DatabaseConnectionTestStatus as DatabaseConnectionTestStatus
from pr_system_settings.database import DatabasePasswordRevealData as DatabasePasswordRevealData
from pr_system_settings.database import DatabaseSettingsData as DatabaseSettingsData
from pr_system_settings.database import DatabaseSettingsUpdate as DatabaseSettingsUpdate

# モデル設定の schema は3製品共通（pr_system_settings。#103）。互換のため re-export する。
from pr_system_settings.model import (
    EnterpriseAiModelEntrySettings as EnterpriseAiModelEntrySettings,
)
from pr_system_settings.model import EnterpriseAiModelKind as EnterpriseAiModelKind
from pr_system_settings.model import EnterpriseAiModelSettings as EnterpriseAiModelSettings
from pr_system_settings.model import EnterpriseAiVlmInputMode as EnterpriseAiVlmInputMode
from pr_system_settings.model import ModelSettingsData as ModelSettingsData
from pr_system_settings.model import ModelSettingsPayload as ModelSettingsPayload
from pr_system_settings.model import ModelSettingsSecretSource as ModelSettingsSecretSource
from pr_system_settings.model import ModelSettingsTestRequest as ModelSettingsTestRequest
from pr_system_settings.model import ModelSettingsTestResult as ModelSettingsTestResult
from pr_system_settings.model import ModelSettingsTestStatus as ModelSettingsTestStatus
from pr_system_settings.model import ModelSettingsTestTargetType as ModelSettingsTestTargetType
from pr_system_settings.system_schema import SystemSchemaOperation, SystemSchemaStatus
from pr_system_settings.system_schema import SystemTableColumnData as SystemTableColumnData
from pr_system_settings.system_schema import SystemTableOperationState as SystemTableOperationState
from pr_system_settings.system_schema import (
    SystemTablesInitializeRequest as SystemTablesInitializeRequest,
)

# アップロード保存先の schema は3製品共通（platform の pr_system_settings。#97）。
# 互換のため re-export する。
from pr_system_settings.upload_storage import (
    UploadStorageSettingsData as UploadStorageSettingsData,
)
from pr_system_settings.upload_storage import (
    UploadStorageSettingsUpdate as UploadStorageSettingsUpdate,
)
from pydantic import BaseModel, Field

# 状態・操作の型と、操作の状態・初期化の request は 3 製品共通（#325）。
SystemTableSchemaStatus = SystemSchemaStatus
SystemTableOperationResult = SystemSchemaOperation


class SystemTableMissingObject(BaseModel):
    """不足している manifest object。"""

    name: str
    object_type: Literal["TABLE", "INDEX", "SEQUENCE", "PACKAGE", "PACKAGE BODY"]


class SystemTableMetadata(BaseModel):
    """USER_TABLES / USER_OBJECTS から取得する概算 metadata。"""

    name: str
    # 接続ユーザーの schema と所有者付きの名前（取得できない場合は空文字）。
    owner: str = ""
    qualified_name: str = ""
    exists: bool
    estimated_rows: int | None = None
    created_at: str | None = None
    last_analyzed_at: str | None = None


class SystemObjectMetadata(BaseModel):
    """必須の table / index / sequence を統一表示する metadata。"""

    name: str
    owner: str = ""
    qualified_name: str = ""
    object_type: Literal["TABLE", "INDEX", "SEQUENCE", "PACKAGE", "PACKAGE BODY"]
    exists: bool
    estimated_rows: int | None = None
    created_at: str | None = None
    last_analyzed_at: str | None = None


class SystemTablesStatusData(BaseModel):
    """NL2SQL system table の read-only status。"""

    status: SystemTableSchemaStatus
    schema_head: int
    applied_versions: list[int]
    pending_versions: list[int]
    expected_object_count: int
    existing_object_count: int
    expected_table_count: int
    existing_table_count: int
    missing_objects: list[SystemTableMissingObject]
    # 既存の表に無い正本の列（台帳が適用済みでも欠けた列。状態は outdated。#2458）。
    missing_columns: list[SystemTableColumnData] = Field(default_factory=list)
    tables: list[SystemTableMetadata]
    objects: list[SystemObjectMetadata]
    operation_state: SystemTableOperationState


class SystemTablesOperationData(SystemTablesStatusData):
    """DDL operation 後の状態と件数。"""

    operation: SystemTableOperationResult
    dropped_object_count: int
    created_object_count: int
