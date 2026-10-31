"""アップロード保存先（OCI Object Storage）の bucket の存在と権限の確認（#2101）。

保存の前と「接続を確認」で、文書のアップロード・読み取り・削除と同じ操作を小さく試す。

1. OCI 認証の設定を読む（OCI config・秘密鍵）
2. `HeadBucket`: bucket があり、参照できるか（`BUCKET_READ`。IAM の `read buckets`）
3. `PutObject`: 確認用の小さな object を書けるか（`OBJECT_CREATE`。`manage objects`）
4. `GetObject`: 書いた object を読めるか（`OBJECT_READ`。`read objects`）
5. `DeleteObject`: 書いた object を消せるか（`OBJECT_DELETE`。`manage objects`）

前の段階が失敗したら、後の段階は試さない（「未実施」）。確認用の object は
`CHECK_OBJECT_PREFIX` の下に作り、最後に消す。

OCI が 401 / 403 / 404 などで拒否したとき（設定の誤り）は `retryable=False`、接続できない・
時間切れ・429・5xx（一時的な失敗）は `retryable=True` にする。保存は設定の誤りのときだけ止め、
一時的な失敗のときは保存して警告を返す（`upload_storage.build_upload_storage_router`）。

応答とログには secret・namespace・bucket の値を出さない（HTTP status・OCI のサービスコード・
`opc-request-id` と、確認用の object の名前だけ）。
"""

from __future__ import annotations

import importlib
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .oci_auth import load_oci_config_without_prompt

logger = logging.getLogger(__name__)

UploadStorageCheckStageKey = Literal["config", "bucket", "write", "read", "delete"]
UploadStorageCheckStageStatus = Literal["success", "failed", "skipped"]

CHECK_STAGE_KEYS: tuple[UploadStorageCheckStageKey, ...] = (
    "config",
    "bucket",
    "write",
    "read",
    "delete",
)
# 確認用の object の名前の接頭辞。製品の保存先（`<製品>/...`）と重ねない。
CHECK_OBJECT_PREFIX = ".production-ready/storage-check/"
CHECK_OBJECT_BODY = b"production-ready upload storage check\n"
OCI_CONNECT_TIMEOUT_SECONDS = 5.0
OCI_READ_TIMEOUT_SECONDS = 10.0

_OCI_AUTH_ACTION = (
    "システム設定 › OCI 認証 で設定を保存し、「接続テスト」が成功することを確かめてください。"
)
_RETRY_ACTION = (
    "時間をおいて再試行してください。続く場合は、バックエンドから OCI への HTTPS 通信"
    "（DNS・プロキシ・ファイアウォール）と OCI のステータスページを確かめてください。"
)

ObjectStorageClientFactory = Callable[[Any], Any]


class UploadStorageCheckStage(BaseModel):
    """確認の 1 段階の結果。"""

    key: UploadStorageCheckStageKey
    status: UploadStorageCheckStageStatus
    message: str
    action: str | None = None


class UploadStorageCheckResult(BaseModel):
    """アップロード保存先の確認の結果（秘密・namespace・bucket の値を含めない）。"""

    status: Literal["success", "failed"]
    # 一時的な失敗（接続できない・時間切れ・429・5xx）なら True。設定の誤りなら False。
    retryable: bool = False
    message: str
    action: str | None = None
    stages: list[UploadStorageCheckStage] = Field(default_factory=list)
    elapsed_ms: int = Field(ge=0)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    region: str | None = None
    check_object: str | None = None
    http_status: int | None = None
    service_code: str | None = None
    request_id: str | None = None
    error_type: str | None = None


class _StageFailed(Exception):
    """段階の失敗（結果を組み立てるための内部の例外）。"""

    def __init__(
        self,
        stage: UploadStorageCheckStage,
        *,
        retryable: bool,
        http_status: int | None = None,
        service_code: str | None = None,
        request_id: str | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(stage.message)
        self.stage = stage
        self.retryable = retryable
        self.http_status = http_status
        self.service_code = service_code
        self.request_id = request_id
        self.error_type = error_type


def default_object_storage_client(settings: Any) -> Any:
    """OCI 認証の設定から、短いタイムアウト・再試行なしの Object Storage client を作る。"""
    oci_config = importlib.import_module("oci.config")
    object_storage = importlib.import_module("oci.object_storage")
    retry = importlib.import_module("oci.retry")
    circuit_breaker = importlib.import_module("oci.circuit_breaker")
    config_file = str(getattr(settings, "oci_config_file", "") or "").strip() or "~/.oci/config"
    profile = str(getattr(settings, "oci_config_profile", "") or "").strip() or "DEFAULT"
    region = _region(settings) or None
    config = load_oci_config_without_prompt(oci_config, config_file, profile, region=region)
    return object_storage.ObjectStorageClient(
        config,
        timeout=(OCI_CONNECT_TIMEOUT_SECONDS, OCI_READ_TIMEOUT_SECONDS),
        retry_strategy=retry.NoneRetryStrategy(),
        circuit_breaker_strategy=circuit_breaker.NoCircuitBreakerStrategy(),
    )


def check_object_storage(
    settings: Any,
    *,
    client_factory: ObjectStorageClientFactory | None = None,
) -> UploadStorageCheckResult:
    """bucket の存在と、object の書き込み・読み取り・削除の権限を確かめる。"""
    started = time.monotonic()
    namespace = str(getattr(settings, "object_storage_namespace", "") or "").strip()
    bucket = str(getattr(settings, "object_storage_bucket", "") or "").strip()
    object_name = f"{CHECK_OBJECT_PREFIX}{uuid.uuid4().hex}.txt"
    stages: list[UploadStorageCheckStage] = []
    written = False
    try:
        try:
            client = (client_factory or default_object_storage_client)(settings)
        except Exception as exc:
            raise _config_failure(exc) from exc
        stages.append(_success("config", "OCI 認証の設定を読み込みました。"))

        _call(
            "bucket",
            lambda: client.head_bucket(namespace, bucket),
            not_found=(
                "bucket が見つからないか、bucket を参照する権限がありません。",
                "bucket 名・リージョン・namespace が正しいかと、IAM ポリシーで bucket の "
                "compartment に `read buckets` が許可されているかを確かめてください。",
            ),
        )
        stages.append(_success("bucket", "bucket を確認しました。"))

        _call(
            "write",
            lambda: client.put_object(
                namespace,
                bucket,
                object_name,
                CHECK_OBJECT_BODY,
                content_type="text/plain",
            ),
            not_found=(
                "bucket に object を書き込む権限がありません。",
                "IAM ポリシーで bucket の compartment に `manage objects` が許可されているかを"
                "確かめてください（文書のアップロードに要ります）。",
            ),
        )
        written = True
        stages.append(_success("write", "確認用の小さな object を書き込みました。"))

        response = _call(
            "read",
            lambda: client.get_object(namespace, bucket, object_name),
            not_found=(
                "書き込んだ object を読み取れません。",
                "IAM ポリシーで bucket の compartment に `read objects` が許可されているかを"
                "確かめてください（文書の表示・取り込みに要ります）。",
            ),
        )
        if _response_body(response) not in (None, CHECK_OBJECT_BODY):
            raise _StageFailed(
                _failed(
                    "read",
                    "読み取った object の内容が書き込んだ内容と一致しません。",
                    "bucket の設定（保持ルール・暗号化の鍵など）を確かめてください。",
                ),
                retryable=False,
            )
        stages.append(_success("read", "書き込んだ object を読み取りました。"))

        _call(
            "delete",
            lambda: client.delete_object(namespace, bucket, object_name),
            not_found=(
                "書き込んだ object を削除できません。",
                "IAM ポリシーで bucket の compartment に `manage objects` が許可されているかを"
                "確かめてください（文書の削除に要ります）。確認用の object "
                f"（{object_name}）が残っているので、OCI コンソールで削除してください。",
            ),
        )
        written = False
        stages.append(_success("delete", "確認用の object を削除しました。"))
    except _StageFailed as failure:
        stages.append(failure.stage)
        if written and failure.stage.key != "delete":
            _cleanup(client, namespace, bucket, object_name)
        result = UploadStorageCheckResult(
            status="failed",
            retryable=failure.retryable,
            message=failure.stage.message,
            action=failure.stage.action,
            stages=_finish(stages),
            elapsed_ms=_elapsed_ms(started),
            region=_region(settings) or None,
            check_object=object_name if failure.stage.key == "delete" else None,
            http_status=failure.http_status,
            service_code=failure.service_code,
            request_id=failure.request_id,
            error_type=failure.error_type,
        )
        logger.warning(
            "upload_storage_check_failed",
            extra={
                "stage": failure.stage.key,
                "retryable": failure.retryable,
                "http_status": failure.http_status,
                "service_code": failure.service_code,
                "error_type": failure.error_type,
            },
        )
        return result
    return UploadStorageCheckResult(
        status="success",
        message="bucket を確認し、object の書き込み・読み取り・削除ができました。",
        stages=_finish(stages),
        elapsed_ms=_elapsed_ms(started),
        region=_region(settings) or None,
    )


def check_failure_detail(result: UploadStorageCheckResult) -> str:
    """保存を止めるときの 422 の文（何が問題か・どうすればよいか）。"""
    parts = [f"OCI Object Storage を確認できなかったため、保存していません: {result.message}"]
    if result.action:
        parts.append(result.action)
    code = " ".join(
        part for part in (str(result.http_status or ""), result.service_code or "") if part
    )
    if code:
        parts.append(f"（OCI の応答: {code}）")
    return " ".join(parts)


def _call(
    key: UploadStorageCheckStageKey,
    operation: Callable[[], Any],
    *,
    not_found: tuple[str, str],
) -> Any:
    try:
        return operation()
    except Exception as exc:
        raise _service_failure(key, exc, not_found=not_found) from exc


def _service_failure(
    key: UploadStorageCheckStageKey,
    exc: BaseException,
    *,
    not_found: tuple[str, str],
) -> _StageFailed:
    status = _int_or_none(getattr(exc, "status", None))
    code = _safe_token(getattr(exc, "code", None))
    request_id = _safe_token(getattr(exc, "request_id", None))
    if status is None or status <= 0:
        # SDK の ServiceError でない（接続できない・時間切れなど）。
        timeout = "timeout" in type(exc).__name__.lower()
        message = (
            f"OCI の応答が {OCI_READ_TIMEOUT_SECONDS:g} 秒以内に返りませんでした。"
            if timeout
            else "OCI Object Storage へ接続できませんでした。"
        )
        return _StageFailed(
            _failed(key, message, _RETRY_ACTION),
            retryable=True,
            error_type=type(exc).__name__,
        )
    label = " ".join(part for part in (str(status), code or "") if part)
    retryable = False
    if status == 401:
        message, action = f"OCI が認証を拒否しました（{label}）。", _OCI_AUTH_ACTION
    elif status in {403, 404}:
        message, action = f"{not_found[0]}（{label}）", not_found[1]
    elif status == 429 or status >= 500:
        message, action, retryable = (
            f"OCI が一時的に応答できませんでした（{label}）。",
            _RETRY_ACTION,
            True,
        )
    else:
        message = f"OCI が要求を受け付けませんでした（{label}）。"
        action = "bucket の設定と IAM ポリシーを確かめてください。"
    return _StageFailed(
        _failed(key, message, action),
        retryable=retryable,
        http_status=status,
        service_code=code,
        request_id=request_id,
        error_type="ServiceError",
    )


def _config_failure(exc: BaseException) -> _StageFailed:
    if getattr(exc, "safe_for_user", False):
        message = str(exc)
    else:
        message = f"OCI 認証の設定を読み込めませんでした（{type(exc).__name__}）。"
    return _StageFailed(
        _failed("config", message, _OCI_AUTH_ACTION),
        retryable=False,
        error_type=type(exc).__name__,
    )


def _cleanup(client: Any, namespace: str, bucket: str, object_name: str) -> None:
    """途中で失敗したとき、書き込んだ確認用の object を消す（失敗しても結果は変えない）。"""
    try:
        client.delete_object(namespace, bucket, object_name)
    except Exception as exc:  # noqa: BLE001 - 確認の結果を優先する
        logger.warning(
            "upload_storage_check_cleanup_failed",
            extra={"error_type": type(exc).__name__, "check_object": object_name},
        )


def _response_body(response: Any) -> bytes | None:
    """`GetObject` の本文（読めなければ None。確認の対象は権限なので比較しない）。"""
    data = getattr(response, "data", None)
    content = getattr(data, "content", None)
    if isinstance(content, bytes):
        return content
    return None


def _success(key: UploadStorageCheckStageKey, message: str) -> UploadStorageCheckStage:
    return UploadStorageCheckStage(key=key, status="success", message=message)


def _failed(
    key: UploadStorageCheckStageKey, message: str, action: str | None
) -> UploadStorageCheckStage:
    return UploadStorageCheckStage(key=key, status="failed", message=message, action=action)


def _finish(stages: list[UploadStorageCheckStage]) -> list[UploadStorageCheckStage]:
    done = {stage.key for stage in stages}
    for key in CHECK_STAGE_KEYS:
        if key not in done:
            stages.append(
                UploadStorageCheckStage(
                    key=key,
                    status="skipped",
                    message="前の段階が失敗したため実施していません。",
                )
            )
    order = {key: index for index, key in enumerate(CHECK_STAGE_KEYS)}
    return sorted(stages, key=lambda stage: order[stage.key])


def _region(settings: Any) -> str:
    return str(
        getattr(settings, "object_storage_region", "") or getattr(settings, "oci_region", "") or ""
    ).strip()


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _safe_token(value: object) -> str | None:
    """OCI のサービスコード・request id だけを通す（任意の文を応答に出さない）。"""
    text = str(value or "").strip()
    if not text or len(text) > 128:
        return None
    if not all(char.isalnum() or char in "-_./:" for char in text):
        return None
    return text
