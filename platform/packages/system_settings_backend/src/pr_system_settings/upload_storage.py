"""アップロード保存先の設定 API（3製品共通。NL2SQL の実装を基準に移設。#97）。

アップロードの原本はローカルのファイルシステム（`PLATFORM_LOCAL_STORAGE_DIR`）に保存する。
OCI Object Storage は扱わない（#11）。`.env` に残っている `PLATFORM_UPLOAD_STORAGE_BACKEND` /
`PLATFORM_OBJECT_STORAGE_*` は読まない。

保存の順序は「候補を作る → `.env` へ保存する → 保存が成功したら runtime の Settings へ反映する」。
保存に失敗した場合は runtime の Settings を変えない。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, params
from pr_backend_core.schemas import ApiResponse
from pydantic import BaseModel, Field, field_validator

from .env_file import write_env_values

ENV_SECTION_COMMENT = "# アップロード保存先"
# アップロード保存先の Settings の項目。保存の後の反映と、別のプロセスの読み直し
# （`pr_backend_core.config.EnvFieldReloader`。#2092）に使う。
UPLOAD_STORAGE_SETTING_FIELDS = ("local_storage_dir",)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class UploadStorageSettingsData(BaseModel):
    """アップロード原本保存先の表示用データ。"""

    local_storage_dir: str
    readiness: str
    max_upload_bytes: int
    config_source: Literal["runtime"]


class UploadStorageSettingsUpdate(BaseModel):
    """アップロード原本保存先の更新 payload。"""

    local_storage_dir: str = Field(default="", max_length=1024)

    @field_validator("local_storage_dir")
    @classmethod
    def validate_local_storage_dir(cls, value: str) -> str:
        """前後空白を除き、改行・NUL などの制御文字を含む path を拒否する。

        `.env` と path の解決を壊さないため。
        """
        value = value.strip()
        if _CONTROL_CHAR_RE.search(value):
            raise ValueError("ローカル保存ディレクトリに改行などの制御文字は使用できません。")
        return value


def upload_storage_settings_data(settings: Any) -> UploadStorageSettingsData:
    """runtime の Settings から表示用データを作る。"""
    local_dir = getattr(settings, "local_storage_dir", "") or ""
    return UploadStorageSettingsData(
        local_storage_dir=local_dir,
        readiness="ok" if local_dir else "missing",
        max_upload_bytes=int(getattr(settings, "max_upload_bytes", 104857600)),
        config_source="runtime",
    )


def upload_storage_candidate(base: Any, payload: UploadStorageSettingsUpdate) -> Any:
    """runtime の Settings を変えずに、更新後の Settings の候補を作る。"""
    updates = {"local_storage_dir": payload.local_storage_dir}
    if hasattr(base, "model_copy"):
        return base.model_copy(update=updates)
    # pydantic 以外の Settings（テスト用の SimpleNamespace など）も扱えるようにする。
    candidate = copy.copy(base)
    for key, value in updates.items():
        setattr(candidate, key, value)
    return candidate


def persist_upload_storage(settings: Any, env_file: Path) -> None:
    """アップロード保存先を `.env` へ保存する。失敗は 500 に変換する。"""
    values: dict[str, str | None] = {"PLATFORM_LOCAL_STORAGE_DIR": settings.local_storage_dir}
    try:
        write_env_values(env_file, values, section_comment=ENV_SECTION_COMMENT)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="アップロード保存先設定を platform/.env へ保存できませんでした。",
        ) from exc


def apply_upload_storage(target: Any, source: Any) -> None:
    """保存が成功した後に、runtime の Settings へ反映する。"""
    for field in UPLOAD_STORAGE_SETTING_FIELDS:
        setattr(target, field, getattr(source, field))


def build_upload_storage_router(
    *,
    get_settings: Callable[[], Any],
    env_file: Callable[[], Path],
    write_dependencies: Sequence[params.Depends] = (),
) -> APIRouter:
    """`GET/PATCH /upload-storage` の router を作る。製品側で `/settings` 配下に include する。

    - `get_settings`：製品の runtime Settings（キャッシュされた同じインスタンス）を返す関数
    - `env_file`：保存先の共通 `.env`（`platform/.env`）を返す関数
      （テストで差し替えられるよう呼出時に解決する）
    - `write_dependencies`：PATCH に付ける依存関係（例: 管理者権限の確認）
    """
    router = APIRouter()

    @router.get("/upload-storage", response_model=ApiResponse[UploadStorageSettingsData])
    def get_upload_storage_settings() -> ApiResponse[UploadStorageSettingsData]:
        return ApiResponse(data=upload_storage_settings_data(get_settings()))

    @router.patch(
        "/upload-storage",
        response_model=ApiResponse[UploadStorageSettingsData],
        dependencies=list(write_dependencies),
    )
    def update_upload_storage_settings(
        payload: UploadStorageSettingsUpdate,
    ) -> ApiResponse[UploadStorageSettingsData]:
        settings = get_settings()
        candidate = upload_storage_candidate(settings, payload)
        persist_upload_storage(candidate, env_file())
        apply_upload_storage(settings, candidate)
        return ApiResponse(data=upload_storage_settings_data(settings))

    return router
