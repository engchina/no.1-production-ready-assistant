"""OCI 認証設定の段階的な接続テスト。

接続テストは「設定の形式 → 鍵の読み取り → リージョン到達 → 認証(API 応答)」の順に確認する。
認証の確認には Object Storage `GetNamespace` を 1 回だけ呼ぶ。この API は読み取り専用で
副作用が無く、呼び出し元の namespace を返すだけなので IAM ポリシーを必要としない
(OCI の Object Storage policy reference に
"API requires no permissions ... Use the API to validate your credentials" とある)。
Identity `GetUser` は `USER_INSPECT` が必要で、正しい認証でもポリシー不足で失敗し得るため使わない。

メッセージとログには秘密鍵・fingerprint・OCID の値を含めない。返すのは項目名、HTTP ステータス、
OCI のサービスコード、`opc-request-id` だけにする。
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

OciConnectivityStageKey = Literal["config_format", "key_file", "region", "authentication"]
OciConnectivityStageStatus = Literal["success", "failed", "skipped"]

OCI_CONNECTIVITY_STAGE_KEYS: tuple[OciConnectivityStageKey, ...] = (
    "config_format",
    "key_file",
    "region",
    "authentication",
)
OCI_CONNECT_TIMEOUT_SECONDS = 5.0
OCI_READ_TIMEOUT_SECONDS = 10.0
OCI_PRIVATE_KEY_READ_MAX_BYTES = 64 * 1024
OCI_AUTH_CHECK_OPERATION = "Object Storage GetNamespace"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OciConnectivityStage:
    """接続テストの 1 段階の結果。"""

    key: OciConnectivityStageKey
    status: OciConnectivityStageStatus
    message: str
    action: str | None = None


@dataclass(frozen=True)
class OciApiCheckResult:
    """認証付き API 呼び出しの結果。秘密を含まない値だけを持つ。"""

    region: OciConnectivityStage
    authentication: OciConnectivityStage
    http_status: int | None = None
    service_code: str | None = None
    request_id: str | None = None
    error_type: str | None = None


@dataclass
class OciConnectivityReport:
    """段階の結果を順序どおりに集める。前段が失敗した段階は未実施にする。"""

    stages: list[OciConnectivityStage] = field(default_factory=list)

    def add(self, stage: OciConnectivityStage) -> bool:
        """段階を追加し、成功したかどうかを返す。"""
        self.stages.append(stage)
        return stage.status == "success"

    def finish(self) -> list[OciConnectivityStage]:
        """未到達の段階を「未実施」で埋めて返す。"""
        done = {stage.key for stage in self.stages}
        for key in OCI_CONNECTIVITY_STAGE_KEYS:
            if key not in done:
                self.stages.append(skipped_stage(key))
        order = {key: index for index, key in enumerate(OCI_CONNECTIVITY_STAGE_KEYS)}
        return sorted(self.stages, key=lambda stage: order[stage.key])

    @property
    def first_failure(self) -> OciConnectivityStage | None:
        return next((stage for stage in self.stages if stage.status == "failed"), None)


def skipped_stage(key: OciConnectivityStageKey) -> OciConnectivityStage:
    """前段の失敗で実施しなかった段階。"""
    return OciConnectivityStage(
        key=key,
        status="skipped",
        message="前の段階が失敗したため実施していません。",
    )


def check_config_format(config: Mapping[str, str]) -> OciConnectivityStage:
    """`oci.config.validate_config` で fingerprint / OCID の形式と必須項目を確認する。"""
    oci_config = importlib.import_module("oci.config")
    oci_exceptions = importlib.import_module("oci.exceptions")
    try:
        oci_config.validate_config(dict(config))
    except oci_exceptions.InvalidConfig as exc:
        errors = exc.args[0] if exc.args and isinstance(exc.args[0], Mapping) else {}
        fields = sorted(str(name) for name in errors) or ["config"]
        return OciConnectivityStage(
            key="config_format",
            status="failed",
            message=f"OCI config の形式が正しくありません（{', '.join(fields)}）。",
            action=_config_format_action(fields),
        )
    return OciConnectivityStage(
        key="config_format",
        status="success",
        message="OCI config の必須項目と形式を確認しました。",
    )


def check_private_key(
    key_path: Path,
    expected_fingerprint: str,
    pass_phrase: str | None,
) -> OciConnectivityStage:
    """秘密鍵を読み込み、公開鍵から算出した fingerprint が config と一致するか確認する。"""
    replace_key = (
        "OCI コンソールで API キーを登録した秘密鍵 PEM を、この画面から再アップロードしてください。"
    )
    try:
        if key_path.stat().st_size > OCI_PRIVATE_KEY_READ_MAX_BYTES:
            return _key_failure("秘密鍵ファイルが大きすぎます。", replace_key)
        data = key_path.read_bytes()
    except OSError:
        return _key_failure("秘密鍵ファイルを読み取れません。", replace_key)

    password = pass_phrase.encode("utf-8") if pass_phrase else None
    try:
        private_key = serialization.load_pem_private_key(data, password=password)
    except TypeError:
        return _key_failure(
            "秘密鍵 PEM のパスフレーズ設定が鍵と一致しません。",
            "パスフレーズなしの秘密鍵 PEM を使うか、OCI config の pass_phrase を確認してください。",
        )
    except (ValueError, UnsupportedAlgorithm):
        return _key_failure(
            "秘密鍵を PEM として読み込めません（形式不正またはパスフレーズ誤り）。",
            replace_key,
        )

    if not isinstance(private_key, RSAPrivateKey):
        return _key_failure("OCI API 署名キーには RSA 秘密鍵が必要です。", replace_key)

    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.md5(public_der, usedforsecurity=False).hexdigest()
    actual_fingerprint = ":".join(digest[index : index + 2] for index in range(0, 32, 2))
    if actual_fingerprint != expected_fingerprint.strip().lower():
        return _key_failure(
            "秘密鍵から算出した fingerprint が OCI config の fingerprint と一致しません。",
            "OCI コンソールのユーザー > API キーに表示される fingerprint と、"
            "アップロードした秘密鍵の組み合わせを確認してください。",
        )
    return OciConnectivityStage(
        key="key_file",
        status="success",
        message="秘密鍵を読み込み、fingerprint との一致を確認しました。",
    )


def check_authenticated_api(config: Mapping[str, Any]) -> OciApiCheckResult:
    """保存済み設定で Object Storage GetNamespace を 1 回だけ呼ぶ(リトライなし)。"""
    oci_exceptions = importlib.import_module("oci.exceptions")
    region = str(config.get("region", "") or "")
    reached = OciConnectivityStage(
        key="region",
        status="success",
        message=f"{region} の OCI endpoint から応答がありました。",
    )
    try:
        _get_object_storage_namespace(config)
    except oci_exceptions.ServiceError as exc:
        return _service_error_result(exc, reached, region)
    except oci_exceptions.ConnectTimeout:
        return _unreachable_result(
            region,
            f"{region} の OCI endpoint への接続が {OCI_CONNECT_TIMEOUT_SECONDS:g} 秒以内に"
            "完了しませんでした。",
            "ConnectTimeout",
        )
    except oci_exceptions.RequestException as exc:
        if _is_read_timeout(exc):
            return OciApiCheckResult(
                region=OciConnectivityStage(
                    key="region",
                    status="success",
                    message=f"{region} の OCI endpoint へ接続しました。",
                ),
                authentication=OciConnectivityStage(
                    key="authentication",
                    status="failed",
                    message=(
                        f"OCI の応答が {OCI_READ_TIMEOUT_SECONDS:g} 秒以内に返りませんでした。"
                    ),
                    action=(
                        "時間をおいて再試行してください。続く場合はプロキシ設定と"
                        "OCI のステータスページを確認してください。"
                    ),
                ),
                error_type="ReadTimeout",
            )
        return _unreachable_result(
            region,
            f"{region} の OCI endpoint へ接続できませんでした。",
            "ConnectionError",
        )
    except Exception as exc:
        logger.warning(
            "oci_connectivity_api_check_failed",
            extra={"oci_error_type": type(exc).__name__},
        )
        return OciApiCheckResult(
            region=skipped_stage("region"),
            authentication=OciConnectivityStage(
                key="authentication",
                status="failed",
                message="OCI SDK で認証付きリクエストを作成できませんでした。",
                action="OCI config の内容と秘密鍵を確認し、保存し直してから再試行してください。",
            ),
            error_type=type(exc).__name__,
        )
    return OciApiCheckResult(
        region=reached,
        authentication=OciConnectivityStage(
            key="authentication",
            status="success",
            message=f"{OCI_AUTH_CHECK_OPERATION} が認証付きで成功しました。",
        ),
    )


def _get_object_storage_namespace(config: Mapping[str, Any]) -> object:
    """短いタイムアウト・リトライなし・circuit breaker なしで GetNamespace を呼ぶ。"""
    object_storage = importlib.import_module("oci.object_storage")
    retry = importlib.import_module("oci.retry")
    circuit_breaker = importlib.import_module("oci.circuit_breaker")
    client = object_storage.ObjectStorageClient(
        dict(config),
        timeout=(OCI_CONNECT_TIMEOUT_SECONDS, OCI_READ_TIMEOUT_SECONDS),
        retry_strategy=retry.NoneRetryStrategy(),
        circuit_breaker_strategy=circuit_breaker.NoCircuitBreakerStrategy(),
    )
    return client.get_namespace(retry_strategy=retry.NoneRetryStrategy())


def _service_error_result(
    exc: Any,
    reached: OciConnectivityStage,
    region: str,
) -> OciApiCheckResult:
    status = _int_or_none(getattr(exc, "status", None))
    code = _safe_token(getattr(exc, "code", None))
    request_id = _safe_token(getattr(exc, "request_id", None))
    label = " ".join(part for part in (str(status) if status else "", code or "") if part)
    if status is None or status <= 0:
        return _unreachable_result(
            region,
            f"{region} の OCI endpoint から HTTP 応答を受け取れませんでした。",
            "ServiceError",
        )
    if status == 401:
        message = f"OCI が認証を拒否しました（{label}）。"
        action = (
            "fingerprint が OCI コンソールの API キーと一致しているか、ユーザー OCID と"
            "テナンシ OCID の組み合わせ、リージョンがテナンシでサブスクライブ済みか、"
            "サーバーの時刻がずれていないかを確認してください。"
            "API キーを作り直した場合は秘密鍵を差し替えてください。"
        )
    elif status in {403, 404}:
        message = f"OCI がアクセスを許可しませんでした（{label}）。"
        action = (
            "IAM ポリシーでこのユーザーのグループに必要な権限が付与されているか、"
            "テナンシとリージョンが正しいかを確認してください。"
        )
    elif status == 429:
        message = f"OCI のリクエスト数制限に達しました（{label}）。"
        action = "しばらく待ってから再試行してください。"
    elif status >= 500:
        message = f"OCI 側でエラーが発生しました（{label}）。"
        action = (
            "時間をおいて再試行してください。続く場合は OCI のステータスページを確認してください。"
        )
    else:
        message = f"OCI がリクエストを受け付けませんでした（{label}）。"
        action = "OCI config の内容を確認し、保存し直してから再試行してください。"
    return OciApiCheckResult(
        region=reached,
        authentication=OciConnectivityStage(
            key="authentication",
            status="failed",
            message=message,
            action=action,
        ),
        http_status=status,
        service_code=code,
        request_id=request_id,
        error_type="ServiceError",
    )


def _unreachable_result(region: str, message: str, error_type: str) -> OciApiCheckResult:
    return OciApiCheckResult(
        region=OciConnectivityStage(
            key="region",
            status="failed",
            message=message,
            action=(
                f"リージョン名（{region}）が正しいか、バックエンドから OCI への HTTPS 通信"
                "（DNS・プロキシ・ファイアウォール）が許可されているかを確認してください。"
            ),
        ),
        authentication=skipped_stage("authentication"),
        error_type=error_type,
    )


def _is_read_timeout(exc: BaseException) -> bool:
    requests_exceptions = importlib.import_module("requests.exceptions")
    candidates: list[object] = [exc, *exc.args, exc.__cause__, exc.__context__]
    return any(isinstance(item, requests_exceptions.ReadTimeout) for item in candidates)


def _config_format_action(fields: list[str]) -> str:
    tips: list[str] = []
    if "fingerprint" in fields:
        tips.append(
            "fingerprint は OCI コンソールの API キーに表示される 16 バイトのコロン区切り"
            "（例: 12:34:…:ef、小文字）を設定してください。"
        )
    if "user" in fields or "tenancy" in fields:
        tips.append(
            "ユーザー OCID とテナンシ OCID は OCI コンソールからコピーした値を設定してください。"
        )
    if not tips:
        tips.append(
            "OCI config の必須項目（user / fingerprint / tenancy / region / key_file）を"
            "確認してください。"
        )
    return " ".join(tips)


def _key_failure(message: str, action: str) -> OciConnectivityStage:
    return OciConnectivityStage(key="key_file", status="failed", message=message, action=action)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_token(value: object) -> str | None:
    """サービスコード / request id として表示してよい短い ASCII 文字列だけを残す。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 256:
        return None
    if not all(char.isascii() and char.isprintable() for char in text):
        return None
    return text


__all__ = [
    "OCI_AUTH_CHECK_OPERATION",
    "OCI_CONNECTIVITY_STAGE_KEYS",
    "OCI_CONNECT_TIMEOUT_SECONDS",
    "OCI_READ_TIMEOUT_SECONDS",
    "OciApiCheckResult",
    "OciConnectivityReport",
    "OciConnectivityStage",
    "OciConnectivityStageKey",
    "OciConnectivityStageStatus",
    "check_authenticated_api",
    "check_config_format",
    "check_private_key",
    "skipped_stage",
]
