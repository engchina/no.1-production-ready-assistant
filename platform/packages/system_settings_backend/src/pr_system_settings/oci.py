"""OCI 認証の設定 API（3製品共通。NL2SQL の実装を基準に移設。#100）。

- `~/.oci/config` の読込 / 書込（既存の profile と権限を保つ）、秘密鍵 PEM の配置
- 共通 `.env` への保存（`PLATFORM_OCI_CONFIG_FILE` / `PLATFORM_OCI_CONFIG_PROFILE` /
  `PLATFORM_OCI_REGION`、
  Object Storage の region / namespace）
- 段階的な接続テスト（形式 → 鍵 → リージョン到達 → 認証）と Object Storage namespace の取得

入力検証は RAG / Agent の厳しい検証を採用する（値は `~/.oci/config` にそのまま書かれるため）。
"""

from __future__ import annotations

import configparser
import importlib
import io
import logging
import re
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile, params
from fastapi.concurrency import run_in_threadpool
from pr_backend_core.schemas import ApiResponse
from pydantic import BaseModel, Field, field_validator

from .env_file import write_env_values
from .oci_auth import (
    OCI_PRIVATE_KEY_PASSPHRASE_REQUIRED_ERROR,
    load_oci_config_without_prompt,
    pem_file_is_encrypted,
    resolve_oci_key_file,
)
from .oci_connectivity import (
    OCI_AUTH_CHECK_OPERATION,
    OciConnectivityReport,
    OciConnectivityStage,
    check_authenticated_api,
    check_config_format,
    check_private_key,
)
from .upload_storage import UploadStorageSettingsData, upload_storage_settings_data

logger = logging.getLogger(__name__)

OCI_DIRECTORY_MODE = 0o700
OCI_CONFIG_MAX_BYTES = 64 * 1024
OCI_CONFIG_FILE_MODE = 0o600
OCI_PRIVATE_KEY_FILE = "~/.oci/oci_api_key.pem"
OCI_PRIVATE_KEY_FILE_MODE = 0o600
OCI_PRIVATE_KEY_MAX_BYTES = 64 * 1024

OciConfigTestStatus = Literal["success", "failed"]
OciConfigField = Literal["user", "fingerprint", "tenancy", "region", "key_file"]
OCI_CONFIG_KEYS: tuple[OciConfigField, ...] = (
    "user",
    "fingerprint",
    "tenancy",
    "region",
    "key_file",
)

_REGION_RE = re.compile(r"[a-z0-9-]+")
_FINGERPRINT_RE = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2})+")
_OBJECT_STORAGE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def _validate_profile_name(value: str) -> str:
    profile = value or "DEFAULT"
    if any(char in profile for char in "[]\r\n"):
        raise ValueError("プロファイル名に [ ] や改行は使用できません。")
    return profile


def _validate_region(value: str) -> str:
    if value and not _REGION_RE.fullmatch(value):
        raise ValueError("リージョンは英小文字、数字、ハイフンで入力してください。")
    return value


# --------------------------------------------------------------------------- schemas


class OciConfigReadRequest(BaseModel):
    """OCI config file の profile 読み取り request。

    `config_file` / `profile` は互換のために受け取るが使わない。読むのは実行中の設定
    （`PLATFORM_OCI_CONFIG_FILE` / `PLATFORM_OCI_CONFIG_PROFILE`）の config で、
    保存・接続テストと同じファイルにそろえる
    （要求の path でサーバーの任意のファイルを読ませない。#1067）。
    """

    config_file: str = Field(default="~/.oci/config", max_length=1024)
    profile: str = Field(default="DEFAULT", max_length=128)

    @field_validator("config_file", "profile")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        """profile 名は INI section として安全な文字列に限定する。"""
        return _validate_profile_name(value)


class OciConfigReadData(BaseModel):
    """OCI config profile から読み取った UI 反映値。"""

    profile: str
    user: str = ""
    fingerprint: str = ""
    tenancy: str = ""
    region: str = ""
    key_file: str = ""
    applied_fields: list[OciConfigField] = Field(default_factory=list)


class OciSettingsUpdate(BaseModel):
    """OCI config / profile の更新 payload。"""

    user: str = Field(default="", max_length=512)
    fingerprint: str = Field(default="", max_length=128)
    tenancy: str = Field(default="", max_length=512)
    region: str = Field(default="", max_length=128)

    @field_validator("user", "fingerprint", "tenancy", "region")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()

    @field_validator("user")
    @classmethod
    def validate_user_ocid(cls, value: str) -> str:
        """OCI user OCID は入力時だけ形式を確認する。"""
        if value and not value.startswith("ocid1.user."):
            raise ValueError("ユーザー OCID は ocid1.user. で始めてください。")
        return value

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        """API key fingerprint は入力時だけ OCI 形式を確認する。"""
        if value and not _FINGERPRINT_RE.fullmatch(value):
            raise ValueError("fingerprint は 16 進数をコロン区切りで入力してください。")
        return value

    @field_validator("tenancy")
    @classmethod
    def validate_tenancy_ocid(cls, value: str) -> str:
        """OCI tenancy OCID は入力時だけ形式を確認する。"""
        if value and not value.startswith("ocid1.tenancy."):
            raise ValueError("テナンシ OCID は ocid1.tenancy. で始めてください。")
        return value

    @field_validator("region")
    @classmethod
    def validate_region(cls, value: str) -> str:
        """リージョン名は入力時だけ OCI region identifier として確認する。"""
        return _validate_region(value)


class OciSettingsData(BaseModel):
    """OCI 認証設定画面の表示用データ。"""

    config_file: str
    profile: str
    user: str
    fingerprint: str
    tenancy: str
    region: str
    key_file: str
    key_file_exists: bool
    config_file_exists: bool
    config_source: Literal["runtime"]
    # config ファイルはあるが読み取れない（形式・文字コード・サイズ・権限）ときの理由。
    # 画面は欄を空のまま「未設定」と見せず、この理由を警告に出す（#1067）。
    config_error: str | None = None


class OciObjectStorageSettingsUpdate(BaseModel):
    """OCI Object Storage 共通設定の更新 payload。"""

    object_storage_region: str = Field(default="", max_length=128)
    object_storage_namespace: str = Field(default="", max_length=256)

    @field_validator("object_storage_region", "object_storage_namespace")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()

    @field_validator("object_storage_region")
    @classmethod
    def validate_region(cls, value: str) -> str:
        """Object Storage region は入力時だけ OCI region identifier として確認する。"""
        return _validate_region(value)

    @field_validator("object_storage_namespace")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        """OCI Object Storage namespace で危険な文字を拒否する。"""
        if value and not _OBJECT_STORAGE_NAME_RE.fullmatch(value):
            raise ValueError(
                "Object Storage namespace は英数字、ハイフン、アンダースコア、"
                "ドットで入力してください。"
            )
        return value


OciConfigTestStageKey = Literal["config_format", "key_file", "region", "authentication"]
OciConfigTestStageStatus = Literal["success", "failed", "skipped"]


class OciConfigTestStage(BaseModel):
    """OCI 接続テストの 1 段階の結果。秘密の値は含めない。"""

    key: OciConfigTestStageKey
    status: OciConfigTestStageStatus
    message: str
    action: str | None = None


class OciConfigTestResult(BaseModel):
    """OCI config / 秘密鍵 / 認証付き API 呼び出しの段階的な検証結果。"""

    status: OciConfigTestStatus
    profile: str
    config_file: str
    key_file: str
    config_file_exists: bool
    key_file_exists: bool
    missing_fields: list[OciConfigField] = Field(default_factory=list)
    permission_issues: list[str] = Field(default_factory=list)
    oci_directory_mode: str | None = None
    config_file_mode: str | None = None
    key_file_mode: str | None = None
    message: str
    elapsed_ms: int = Field(ge=0)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    error_type: str | None = None
    stages: list[OciConfigTestStage] = Field(default_factory=list)
    region: str | None = None
    auth_check_operation: str | None = None
    http_status: int | None = None
    service_code: str | None = None
    request_id: str | None = None


class OciObjectStorageNamespaceRequest(BaseModel):
    """Object Storage namespace 取得 request。

    `config_file` / `profile` は互換のために受け取るが使わない
    （実行中の設定の config で取得する。#1067）。
    """

    config_file: str = Field(default="~/.oci/config", max_length=1024)
    profile: str = Field(default="DEFAULT", max_length=128)
    region: str = Field(default="", max_length=128)

    @field_validator("config_file", "profile", "region")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        """profile 名は INI section として安全な文字列に限定する。"""
        return _validate_profile_name(value)

    @field_validator("region")
    @classmethod
    def validate_region(cls, value: str) -> str:
        """region は入力時だけ OCI region identifier として確認する。"""
        return _validate_region(value)


class OciObjectStorageNamespaceData(BaseModel):
    """Object Storage namespace 取得結果。"""

    namespace: str


class OciPrivateKeyUploadData(BaseModel):
    """OCI API 秘密鍵アップロード結果。"""

    key_file: str
    saved: bool


# --------------------------------------------------------------------------- helpers
def _oci_settings_data(settings: Any) -> OciSettingsData:
    config_file = _oci_config_file(settings)
    profile = _oci_profile(settings)
    parsed, config_error = _read_runtime_oci_config_for_display(config_file, profile)
    key_file = OCI_PRIVATE_KEY_FILE
    return OciSettingsData(
        config_file=config_file,
        profile=profile,
        user=parsed.user if parsed is not None else "",
        fingerprint=parsed.fingerprint if parsed is not None else "",
        tenancy=parsed.tenancy if parsed is not None else "",
        region=parsed.region if parsed is not None else "",
        key_file=key_file,
        key_file_exists=_expand(key_file).exists(),
        config_file_exists=_expand(config_file).exists(),
        config_source="runtime",
        config_error=config_error,
    )


def _oci_config_file(settings: Any) -> str:
    return str(getattr(settings, "oci_config_file", "") or "").strip() or "~/.oci/config"


def _oci_profile(settings: Any) -> str:
    # NL2SQL は旧 OCI_PROFILE も考慮した resolved_oci_config_profile を持つ。
    resolved = getattr(settings, "resolved_oci_config_profile", None)
    if isinstance(resolved, str) and resolved.strip():
        return resolved.strip()
    return str(getattr(settings, "oci_config_profile", "") or "").strip() or "DEFAULT"


def _read_runtime_oci_config(config_file: str, profile: str) -> OciConfigReadData | None:
    """runtime の OCI config を表示用に読む。読めない場合は画面表示を継続する。"""
    try:
        content = _read_oci_config_text(config_file)
        return _parse_oci_config(content, profile)
    except HTTPException:
        return None


def _read_runtime_oci_config_for_display(
    config_file: str, profile: str
) -> tuple[OciConfigReadData | None, str | None]:
    """画面に出す OCI config を読む。読めない理由を、未設定と区別して返す。

    - ファイルが無い・profile が無い・profile に項目が無い: 未設定（理由は None）
    - ファイルはあるが読めない（形式・UTF-8・サイズ・権限）: 理由を返す
    """
    if not _expand(config_file).exists():
        return None, None
    try:
        content = _read_oci_config_text(config_file)
        return _parse_oci_config(content, profile), None
    except HTTPException as exc:
        if (
            exc.status_code in {404, 422}
            and _expand(config_file).is_file()
            and _is_readable(config_file)
        ):
            # profile が無い・項目が無い（まだ保存していない profile）は未設定として扱う。
            return None, None
        return None, str(exc.detail)


def _is_readable(config_file: str) -> bool:
    try:
        with _expand(config_file).open("rb"):
            return True
    except OSError:
        return False


def _expand(path: str) -> Path:
    return Path(path).expanduser()


async def _read_upload_file(
    file: UploadFile,
    max_bytes: int,
    too_large_detail: str = "ファイルのサイズが上限を超えています。",
) -> bytes:
    """アップロードファイルを上限付きで読み込む。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=too_large_detail)
        chunks.append(chunk)
    return b"".join(chunks)


def _install_oci_private_key(data: bytes, file_name: str | None) -> Path:
    """OCI API 秘密鍵 PEM を固定 path へ上書き保存する。"""
    safe_name = PurePosixPath((file_name or "oci_api_key.pem").replace("\\", "/")).name
    if Path(safe_name).suffix.lower() not in {".pem", ".key"}:
        raise HTTPException(
            status_code=415,
            detail="秘密鍵は .pem または .key ファイルを選択してください。",
        )
    if not data:
        raise HTTPException(status_code=400, detail="空の秘密鍵ファイルはアップロードできません。")
    _validate_private_key_pem(data)

    target = Path(OCI_PRIVATE_KEY_FILE).expanduser()
    tmp_path = target.with_name(f".{target.name}.tmp-{uuid4().hex}")
    try:
        _ensure_private_directory(target.parent)
        tmp_path.write_bytes(data)
        tmp_path.chmod(OCI_PRIVATE_KEY_FILE_MODE)
        tmp_path.replace(target)
        target.chmod(OCI_PRIVATE_KEY_FILE_MODE)
    except OSError as exc:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail="秘密鍵ファイルをバックエンドの固定 path へ保存できませんでした。",
        ) from exc
    return target


def _validate_private_key_pem(data: bytes) -> None:
    """秘密鍵らしい PEM テキストだけを受け付ける。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="秘密鍵ファイルは UTF-8 の PEM テキストとして読み取れる必要があります。",
        ) from exc
    if "\x00" in text or "-----BEGIN " not in text or "PRIVATE KEY-----" not in text:
        raise HTTPException(
            status_code=400,
            detail="秘密鍵 PEM ファイルの形式を確認してください。",
        )
    upper_text = text.upper()
    if "BEGIN ENCRYPTED PRIVATE KEY" in upper_text or "PROC-TYPE: 4,ENCRYPTED" in upper_text:
        raise HTTPException(
            status_code=400,
            detail=(
                "暗号化された OCI API 秘密鍵は pass phrase 入力が必要です。"
                "パスフレーズなしの秘密鍵 PEM を使用してください。"
            ),
        )


def _write_oci_config(settings: Any, payload: OciSettingsUpdate) -> Path:
    """OCI SDK config を安全な権限で作成または更新する。"""
    target = Path(_oci_config_file(settings)).expanduser()
    profile = _safe_oci_profile_name(_oci_profile(settings))
    parser = _load_oci_config_for_write(target)
    values = {
        "user": payload.user.strip(),
        "fingerprint": payload.fingerprint.strip(),
        "tenancy": payload.tenancy.strip(),
        "region": payload.region.strip(),
    }
    if any(value.strip() for value in values.values()):
        values["key_file"] = OCI_PRIVATE_KEY_FILE
    _set_oci_config_profile(
        parser,
        profile,
        {key: value for key, value in values.items() if value.strip()},
    )
    _atomic_write_oci_config(target, parser)
    return target


def _safe_oci_profile_name(profile: str) -> str:
    """OCI profile 名を INI section として安全な文字列へ制限する。"""
    selected = profile.strip() or "DEFAULT"
    if any(char in selected for char in "[]\r\n"):
        raise HTTPException(status_code=422, detail="プロファイル名に [ ] や改行は使用できません。")
    return selected


def _load_oci_config_for_write(path: Path) -> configparser.ConfigParser:
    """既存 config があれば読み、なければ空の parser を返す。"""
    parser = configparser.ConfigParser(interpolation=None)
    if not path.exists():
        return parser
    if path.is_dir():
        raise HTTPException(
            status_code=400,
            detail="OCI config ファイル path がディレクトリを指しています。",
        )
    try:
        if path.stat().st_size > OCI_CONFIG_MAX_BYTES:
            raise HTTPException(status_code=413, detail="OCI config ファイルが大きすぎます。")
        content = path.read_text(encoding="utf-8")
    except HTTPException:
        raise
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="OCI config ファイルは UTF-8 テキストとして読み取れる必要があります。",
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="OCI config ファイルを更新前に読み取れませんでした。",
        ) from exc
    if not content.strip():
        return parser
    try:
        parser.read_string(content)
    except configparser.Error as exc:
        raise HTTPException(
            status_code=400,
            detail="OCI config ファイルの形式を確認してください。",
        ) from exc
    return parser


def _set_oci_config_profile(
    parser: configparser.ConfigParser,
    profile: str,
    values: dict[str, str],
) -> None:
    """DEFAULT または指定 profile に OCI SDK 必須値を設定する。"""
    if profile.upper() == "DEFAULT":
        for key, value in values.items():
            parser["DEFAULT"][key] = value
        return
    if not parser.has_section(profile):
        parser.add_section(profile)
    for key, value in values.items():
        parser[profile][key] = value


def _atomic_write_oci_config(path: Path, parser: configparser.ConfigParser) -> None:
    """config を一時ファイル経由で保存し、ディレクトリ/ファイル権限を補正する。"""
    tmp_path = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        _ensure_private_directory(path.parent)
        buffer = io.StringIO()
        parser.write(buffer, space_around_delimiters=False)
        tmp_path.write_text(buffer.getvalue(), encoding="utf-8")
        tmp_path.chmod(OCI_CONFIG_FILE_MODE)
        tmp_path.replace(path)
        path.chmod(OCI_CONFIG_FILE_MODE)
    except OSError as exc:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail="OCI config ファイルをバックエンドの固定 path へ保存できませんでした。",
        ) from exc


def _ensure_private_directory(path: Path) -> None:
    """OCI credential directory を作成し、所有者だけが入れる権限に補正する。"""
    path.mkdir(mode=OCI_DIRECTORY_MODE, parents=True, exist_ok=True)
    path.chmod(OCI_DIRECTORY_MODE)


def _test_oci_config(settings: Any, *, verify_with_oci: bool = True) -> OciConfigTestResult:
    """保存済み OCI 設定で、形式 → 鍵 → リージョン到達 → 認証の順に実疎通を確認する。

    `verify_with_oci=False` は画面表示用の静的 readiness 向けで、
    OCI へ通信せず鍵の検査までで終える。
    """
    started = time.perf_counter()
    config_file = _oci_config_file(settings)
    config_path = Path(config_file).expanduser()
    profile = _safe_oci_profile_name(_oci_profile(settings))
    key_path = Path(OCI_PRIVATE_KEY_FILE).expanduser()
    report = OciConnectivityReport()
    try:
        content = _read_oci_config_text(config_file)
        parsed = _parse_oci_config(content, profile)
    except HTTPException as exc:
        report.add(
            OciConnectivityStage(
                key="config_format",
                status="failed",
                message=str(exc.detail),
                action="OCI config を「config 読込」で確認し、認証設定を保存し直してください。",
            )
        )
        return _oci_config_test_result(
            report,
            started=started,
            profile=profile,
            config_file=config_file,
            key_file=OCI_PRIVATE_KEY_FILE,
            config_path=config_path,
            key_path=key_path,
            key_file_exists=key_path.is_file(),
            error_type="HTTPException",
        )

    parsed_values = {
        "user": parsed.user,
        "fingerprint": parsed.fingerprint,
        "tenancy": parsed.tenancy,
        "region": parsed.region,
        "key_file": parsed.key_file,
    }
    missing_fields: list[OciConfigField] = [
        field for field in OCI_CONFIG_KEYS if not parsed_values[field].strip()
    ]
    key_file = parsed.key_file or OCI_PRIVATE_KEY_FILE
    key_path = resolve_oci_key_file(key_file, config_path)
    key_file_exists = key_path.is_file()
    permission_issues = _oci_permission_issues(config_path, key_path)
    error_type: str | None = None

    def finish(
        *,
        auth_check_operation: str | None = None,
        http_status: int | None = None,
        service_code: str | None = None,
        request_id: str | None = None,
    ) -> OciConfigTestResult:
        return _oci_config_test_result(
            report,
            started=started,
            profile=parsed.profile,
            config_file=config_file,
            key_file=key_file,
            config_path=config_path,
            key_path=key_path,
            key_file_exists=key_file_exists,
            missing_fields=missing_fields,
            permission_issues=permission_issues,
            region=parsed.region or None,
            error_type=error_type,
            auth_check_operation=auth_check_operation,
            http_status=http_status,
            service_code=service_code,
            request_id=request_id,
        )

    config_permission_issues = [
        issue for issue in permission_issues if not issue.startswith("秘密鍵")
    ]
    if missing_fields:
        report.add(
            OciConnectivityStage(
                key="config_format",
                status="failed",
                message="OCI config の必須項目が不足しています。",
                action="不足している項目を入力して認証設定を保存してください。",
            )
        )
        return finish()
    if not report.add(check_config_format(parsed_values)):
        return finish()
    if config_permission_issues:
        report.stages[-1] = OciConnectivityStage(
            key="config_format",
            status="failed",
            message="OCI 認証ファイルの権限を確認してください。",
            action=" ".join(config_permission_issues),
        )
        return finish()

    pass_phrase = _oci_config_private_key_pass_phrase(content, profile)
    if not key_file_exists:
        key_stage = OciConnectivityStage(
            key="key_file",
            status="failed",
            message="OCI config の key_file が指す秘密鍵ファイルが見つかりません。",
            action="OCI コンソールで API キーを登録した秘密鍵 PEM をアップロードしてください。",
        )
    elif pem_file_is_encrypted(key_path) and not pass_phrase:
        key_stage = OciConnectivityStage(
            key="key_file",
            status="failed",
            message=OCI_PRIVATE_KEY_PASSPHRASE_REQUIRED_ERROR,
            action="パスフレーズなしの秘密鍵 PEM をアップロードしてください。",
        )
        error_type = "OciPrivateKeyPassPhraseRequiredError"
    elif len(permission_issues) != len(config_permission_issues):
        key_stage = OciConnectivityStage(
            key="key_file",
            status="failed",
            message="OCI 認証ファイルの権限を確認してください。",
            action="秘密鍵ファイルは 0600 にしてください。",
        )
    else:
        key_stage = check_private_key(key_path, parsed.fingerprint, pass_phrase)
    if not report.add(key_stage) or not verify_with_oci:
        return finish()

    try:
        oci_config = importlib.import_module("oci.config")
        sdk_config = load_oci_config_without_prompt(oci_config, config_file, profile)
    except Exception as exc:
        report.add(
            OciConnectivityStage(
                key="authentication",
                status="failed",
                message="OCI SDK で OCI config を読み込めませんでした。",
                action="OCI config の内容を確認し、認証設定を保存し直してください。",
            )
        )
        error_type = type(exc).__name__
        return finish()

    api_result = check_authenticated_api(sdk_config)
    report.add(api_result.region)
    report.add(api_result.authentication)
    logger.info(
        "oci_config_test_api_check_completed",
        extra={
            "oci_auth_check_operation": OCI_AUTH_CHECK_OPERATION,
            "oci_region_stage": api_result.region.status,
            "oci_authentication_stage": api_result.authentication.status,
            "oci_http_status": api_result.http_status,
            "oci_service_code": api_result.service_code,
            "oci_request_id": api_result.request_id,
            "oci_error_type": api_result.error_type,
        },
    )
    error_type = api_result.error_type
    return finish(
        auth_check_operation=OCI_AUTH_CHECK_OPERATION,
        http_status=api_result.http_status,
        service_code=api_result.service_code,
        request_id=api_result.request_id,
    )


def _oci_config_test_result(
    report: OciConnectivityReport,
    *,
    started: float,
    profile: str,
    config_file: str,
    key_file: str,
    config_path: Path,
    key_path: Path,
    key_file_exists: bool,
    missing_fields: list[OciConfigField] | None = None,
    permission_issues: list[str] | None = None,
    region: str | None = None,
    auth_check_operation: str | None = None,
    http_status: int | None = None,
    service_code: str | None = None,
    request_id: str | None = None,
    error_type: str | None = None,
) -> OciConfigTestResult:
    """段階の結果から API 応答を組み立てる。すべての段階が成功したときだけ success にする。"""
    stages = report.finish()
    failure = report.first_failure
    status: OciConfigTestStatus = "failed" if failure else "success"
    message = (
        failure.message
        if failure
        else f"OCI へ認証付きで接続できました（{OCI_AUTH_CHECK_OPERATION}）。"
    )
    return OciConfigTestResult(
        status=status,
        profile=profile,
        config_file=config_file,
        key_file=key_file,
        config_file_exists=config_path.is_file(),
        key_file_exists=key_file_exists,
        missing_fields=missing_fields or [],
        permission_issues=permission_issues or [],
        oci_directory_mode=_mode_string(config_path.parent),
        config_file_mode=_mode_string(config_path),
        key_file_mode=_mode_string(key_path),
        message=message,
        elapsed_ms=_elapsed_ms(started),
        error_type=error_type,
        stages=[OciConfigTestStage(**asdict(stage)) for stage in stages],
        region=region,
        auth_check_operation=auth_check_operation,
        http_status=http_status,
        service_code=service_code,
        request_id=request_id,
    )


def _oci_permission_issues(config_path: Path, key_path: Path) -> list[str]:
    """OCI credential path の group/other 権限露出を検出する。"""
    issues: list[str] = []
    directory_mode = _path_mode(config_path.parent)
    config_mode = _path_mode(config_path)
    key_mode = _path_mode(key_path)
    if directory_mode is not None and directory_mode != OCI_DIRECTORY_MODE:
        issues.append("~/.oci ディレクトリは 0700 にしてください。")
    if config_mode is not None and config_mode & 0o077:
        issues.append("OCI config ファイルは 0600 にしてください。")
    if key_mode is not None and key_mode & 0o077:
        issues.append("秘密鍵ファイルは 0600 にしてください。")
    return issues


def _mode_string(path: Path) -> str | None:
    """path の permission mode を 4 桁 8 進数で返す。"""
    mode = _path_mode(path)
    return f"{mode:04o}" if mode is not None else None


def _oci_config_private_key_pass_phrase(content: str, profile: str) -> str | None:
    """OCI config profile の private key pass phrase を返す。値はログや応答に出さない。"""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content)
    except configparser.Error:
        return None

    selected_profile = profile.strip() or "DEFAULT"
    if selected_profile.upper() == "DEFAULT":
        entries = parser.defaults()
    elif parser.has_section(selected_profile):
        entries = parser[selected_profile]
    else:
        return None
    for key in ("pass_phrase", "passphrase", "key_password"):
        value = str(entries.get(key, "")).strip()
        if value:
            return value
    return None


def _path_mode(path: Path) -> int | None:
    """存在しない path の mode 取得失敗を通常値として扱う。"""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _read_oci_config_text(config_file: str) -> str:
    """OCI config file を安全な上限付きで読み込む。"""
    path = Path(config_file).expanduser()
    try:
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=(
                    "OCI config ファイルを読み取れません。"
                    "バックエンドから参照できる path を指定してください。"
                ),
            )
        if path.stat().st_size > OCI_CONFIG_MAX_BYTES:
            raise HTTPException(status_code=413, detail="OCI config ファイルが大きすぎます。")
        return path.read_text(encoding="utf-8")
    except HTTPException:
        raise
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="OCI config ファイルは UTF-8 テキストとして読み取れる必要があります。",
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                "OCI config ファイルを読み取れません。"
                "バックエンドから参照できる path を指定してください。"
            ),
        ) from exc


def _parse_oci_config(content: str, profile: str) -> OciConfigReadData:
    """OCI config の profile から UI に反映する値だけを抽出する。"""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content)
    except configparser.Error as exc:
        raise HTTPException(
            status_code=400,
            detail="OCI config ファイルの形式を確認してください。",
        ) from exc

    selected_profile = profile.strip() or "DEFAULT"
    if selected_profile.upper() == "DEFAULT":
        entries = parser.defaults()
    elif parser.has_section(selected_profile):
        entries = parser[selected_profile]
    else:
        raise HTTPException(
            status_code=404,
            detail="指定した OCI config profile が見つかりません。",
        )

    values = {key: str(entries.get(key, "")).strip() for key in OCI_CONFIG_KEYS}
    applied_fields = [key for key in OCI_CONFIG_KEYS if values[key]]
    if not applied_fields:
        raise HTTPException(
            status_code=422,
            detail="指定した profile から OCI config 項目を読み取れませんでした。",
        )

    return OciConfigReadData(
        profile=selected_profile,
        user=values["user"],
        fingerprint=values["fingerprint"],
        tenancy=values["tenancy"],
        region=values["region"],
        key_file=values["key_file"],
        applied_fields=applied_fields,
    )


def _elapsed_ms(started: float) -> int:
    """perf_counter の開始時刻から経過 ms を返す。"""
    return max(0, round((time.perf_counter() - started) * 1000))


def _read_object_storage_namespace(
    payload: OciObjectStorageNamespaceRequest, *, config_file: str, profile: str
) -> str:
    """OCI SDK で Object Storage namespace を取得する。config は実行中の設定のもの（#1067）。"""
    try:
        oci_config = importlib.import_module("oci.config")
        object_storage = importlib.import_module("oci.object_storage")
        config = load_oci_config_without_prompt(
            oci_config,
            config_file,
            profile,
            region=payload.region,
        )
        response = object_storage.ObjectStorageClient(config).get_namespace()
    except Exception as exc:
        detail = (
            str(exc)
            if getattr(exc, "safe_for_user", False)
            else (
                "OCI Object Storage namespace を取得できませんでした。"
                "OCI config / profile / region を確認してください。"
            )
        )
        raise HTTPException(status_code=502, detail=detail) from exc

    namespace = getattr(response, "data", "")
    if not isinstance(namespace, str):
        namespace = str(namespace) if namespace is not None else ""
    namespace = namespace.strip()
    if not namespace:
        raise HTTPException(
            status_code=502,
            detail="OCI Object Storage namespace が空で返されました。",
        )
    return namespace


# --------------------------------------------------------------------------- persistence


def _persist_oci_settings(settings: Any, payload: OciSettingsUpdate, env_file: Path) -> None:
    """OCI 共通設定を platform/.env へ永続化する。"""
    try:
        write_env_values(
            env_file,
            {
                "PLATFORM_OCI_CONFIG_FILE": _oci_config_file(settings),
                "PLATFORM_OCI_CONFIG_PROFILE": _oci_profile(settings),
                "PLATFORM_OCI_REGION": payload.region.strip() or None,
            },
            section_comment="# OCI 共通",
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail="OCI 認証設定を platform/.env へ保存できませんでした。"
        ) from exc


def _persist_oci_object_storage_settings(settings: Any, env_file: Path) -> None:
    """OCI Object Storage 共通設定を platform/.env へ永続化する。"""
    try:
        write_env_values(
            env_file,
            {
                "PLATFORM_OBJECT_STORAGE_REGION": settings.object_storage_region,
                "PLATFORM_OBJECT_STORAGE_NAMESPACE": settings.object_storage_namespace,
            },
            section_comment="# OCI Object Storage",
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="OCI Object Storage 設定を platform/.env へ保存できませんでした。",
        ) from exc


def _object_storage_candidate(settings: Any, payload: OciObjectStorageSettingsUpdate) -> Any:
    updates = {
        "object_storage_region": payload.object_storage_region,
        "object_storage_namespace": payload.object_storage_namespace,
    }
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update=updates)
    import copy

    candidate = copy.copy(settings)
    for key, value in updates.items():
        setattr(candidate, key, value)
    return candidate


# --------------------------------------------------------------------------- public API

# 製品側（NL2SQL の Select AI / ADB、RAG の guardrail readiness など）が使う関数。
oci_settings_data = _oci_settings_data
oci_config_file = _oci_config_file
oci_profile = _oci_profile
read_oci_config_text = _read_oci_config_text
parse_oci_config = _parse_oci_config
read_runtime_oci_config = _read_runtime_oci_config
test_oci_config = _test_oci_config


def build_oci_router(
    *,
    get_settings: Callable[[], Any],
    env_file: Callable[[], Path],
    write_dependencies: Sequence[params.Depends] = (),
    action_dependencies: Sequence[params.Depends] = (),
) -> APIRouter:
    """`/oci*` の router を作る。製品側で `/settings` 配下に include する。

    - `write_dependencies`：設定を書き換える PATCH と秘密鍵アップロードに付ける依存関係
    - `action_dependencies`：config 読込・接続テスト・namespace 取得に付ける依存関係
    """
    router = APIRouter()
    write = list(write_dependencies)
    action = list(action_dependencies)

    @router.get("/oci", response_model=ApiResponse[OciSettingsData])
    def get_oci_settings() -> ApiResponse[OciSettingsData]:
        return ApiResponse(data=_oci_settings_data(get_settings()))

    @router.patch("/oci", response_model=ApiResponse[OciSettingsData], dependencies=write)
    def update_oci_settings(payload: OciSettingsUpdate) -> ApiResponse[OciSettingsData]:
        settings = get_settings()
        _write_oci_config(settings, payload)
        _persist_oci_settings(settings, payload, env_file())
        settings.oci_region = payload.region
        return ApiResponse(data=_oci_settings_data(settings))

    @router.patch(
        "/oci/object-storage",
        response_model=ApiResponse[UploadStorageSettingsData],
        dependencies=write,
    )
    def update_oci_object_storage_settings(
        payload: OciObjectStorageSettingsUpdate,
    ) -> ApiResponse[UploadStorageSettingsData]:
        settings = get_settings()
        # 保存に成功してから runtime へ反映する（NL2SQL #376）。
        candidate = _object_storage_candidate(settings, payload)
        _persist_oci_object_storage_settings(candidate, env_file())
        settings.object_storage_region = candidate.object_storage_region
        settings.object_storage_namespace = candidate.object_storage_namespace
        return ApiResponse(data=upload_storage_settings_data(settings))

    @router.post(
        "/oci/config/read", response_model=ApiResponse[OciConfigReadData], dependencies=action
    )
    def read_oci_config(payload: OciConfigReadRequest) -> ApiResponse[OciConfigReadData]:
        # 要求の config_file / profile は使わない（#1067）。
        del payload
        settings = get_settings()
        content = _read_oci_config_text(_oci_config_file(settings))
        return ApiResponse(data=_parse_oci_config(content, _oci_profile(settings)))

    @router.post(
        "/oci/config/test", response_model=ApiResponse[OciConfigTestResult], dependencies=action
    )
    def run_oci_config_test() -> ApiResponse[OciConfigTestResult]:
        return ApiResponse(data=_test_oci_config(get_settings()))

    @router.post(
        "/oci/object-storage/namespace",
        response_model=ApiResponse[OciObjectStorageNamespaceData],
        dependencies=action,
    )
    def read_oci_object_storage_namespace(
        payload: OciObjectStorageNamespaceRequest,
    ) -> ApiResponse[OciObjectStorageNamespaceData]:
        settings = get_settings()
        return ApiResponse(
            data=OciObjectStorageNamespaceData(
                namespace=_read_object_storage_namespace(
                    payload,
                    config_file=_oci_config_file(settings),
                    profile=_safe_oci_profile_name(_oci_profile(settings)),
                )
            )
        )

    @router.post(
        "/oci/key-file", response_model=ApiResponse[OciPrivateKeyUploadData], dependencies=write
    )
    async def upload_oci_private_key(
        file: Annotated[UploadFile, File(...)],
    ) -> ApiResponse[OciPrivateKeyUploadData]:
        data = await _read_upload_file(
            file,
            OCI_PRIVATE_KEY_MAX_BYTES,
            "秘密鍵 PEM ファイルのサイズが上限を超えています。",
        )
        await run_in_threadpool(_install_oci_private_key, data, file.filename)
        return ApiResponse(data=OciPrivateKeyUploadData(key_file=OCI_PRIVATE_KEY_FILE, saved=True))

    return router
