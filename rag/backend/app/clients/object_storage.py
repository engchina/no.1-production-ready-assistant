"""OCI Object Storage クライアント。原本ファイルの保管。"""

import asyncio
import contextlib
import importlib
import os
import re
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, Protocol
from uuid import uuid4

from app.clients.oci_auth import load_oci_config_without_prompt
from app.config import Settings, get_settings

MAX_OBJECT_KEY_LENGTH = 1024
MAX_OBJECT_KEY_DEPTH = 16
MAX_OBJECT_KEY_PART_LENGTH = 255
# prefix 単位の削除で許す最も浅い階層（`artifacts/` のような保存先全体の削除を防ぐ。#303）。
MIN_DELETE_PREFIX_DEPTH = 2
# OCI の namespace / bucket の名前（保存した `oci://` の参照から取り出す値の検証。#2093）。
_OBJECT_STORAGE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
# OCI SDK の既定（8 回・最大 600 秒の再試行）では、Object Storage に届かないときに
# アップロード・プレビューが 1〜2 分待ってから失敗する（#2093）。接続と再試行を短く区切る。
OCI_CONNECT_TIMEOUT_SECONDS = 10.0
OCI_READ_TIMEOUT_SECONDS = 120.0
OCI_MAX_ATTEMPTS = 3
OCI_RETRY_TOTAL_ELAPSED_SECONDS = 30
OCI_RETRY_MAX_WAIT_SECONDS = 5
type SdkCallRunner = Callable[[Callable[[], Any]], Awaitable[Any]]


class ObjectStorageSdkClientProtocol(Protocol):
    """OCI Object Storage SDK client の最小インターフェース。"""

    def put_object(
        self,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
        put_object_body: bytes,
        **kwargs: object,
    ) -> Any:
        """OCI Object Storage put_object を呼び出す。"""

    def get_object(
        self,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
    ) -> Any:
        """OCI Object Storage get_object を呼び出す。"""

    def delete_object(
        self,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
    ) -> Any:
        """OCI Object Storage delete_object を呼び出す。"""

    def list_objects(
        self,
        namespace_name: str,
        bucket_name: str,
        **kwargs: object,
    ) -> Any:
        """OCI Object Storage list_objects を呼び出す。"""


class PrefixDeleteResult(NamedTuple):
    """prefix 単位の削除の結果。"""

    deleted: int
    failed: int


class ObjectStorageClient:
    """OCI Object Storage への保存・取得。"""

    def __init__(
        self,
        settings: Settings | None = None,
        storage_client: ObjectStorageSdkClientProtocol | None = None,
        sdk_call_runner: SdkCallRunner | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._storage_client = storage_client
        self._sdk_call_runner = sdk_call_runner or _run_sdk_call_in_thread

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """オブジェクトを保存し、参照パスを返す。"""
        safe_key = _local_storage_key(key)
        if self._settings.upload_storage_backend == "oci":
            return await self._put_to_oci(safe_key, data, content_type)
        return self._put_to_local(safe_key, data)

    async def get(self, key: str) -> bytes:
        """オブジェクトを取得する。無ければ FileNotFoundError。

        保存した参照（`oci://` / `local://`）は、今の保存先の設定に関係なく置いた場所から読む。
        """
        if key.strip().startswith("oci://"):
            return await self._get_from_oci(key)
        if key.strip().startswith("local://"):
            return self._get_from_local(key)
        if self._settings.upload_storage_backend == "oci":
            return await self._get_from_oci(key)
        return self._get_from_local(key)

    async def delete(self, key: str) -> bool:
        """オブジェクトを削除する。存在した場合は True を返す。"""
        if key.strip().startswith("oci://"):
            return await self._delete_from_oci(key)
        if key.strip().startswith("local://"):
            return self._delete_from_local(key)
        if self._settings.upload_storage_backend == "oci":
            return await self._delete_from_oci(key)
        return self._delete_from_local(key)

    async def delete_prefix(self, prefix: str, *, include_oci: bool = False) -> PrefixDeleteResult:
        """prefix（末尾 `/` で終わるディレクトリ単位）配下のオブジェクトをすべて削除する。

        文書の削除で、参照が残っていない過去の取込の成果物を後始末するために使う（#303）。
        対象は local（保存先を OCI に切り替える前の成果物も消す。#2093）と、今の保存先が OCI か
        `include_oci`（文書が OCI の参照を持つ）なら OCI。一覧の取得に失敗したら例外を送出し、
        個々のオブジェクトの削除の失敗は数えて残りを続ける。
        """
        safe_prefix = _delete_prefix_key(prefix)
        local = PrefixDeleteResult(deleted=0, failed=0)
        if str(getattr(self._settings, "local_storage_dir", "") or "").strip():
            local = self._delete_prefix_from_local(safe_prefix)
        if not include_oci and self._settings.upload_storage_backend != "oci":
            return local
        remote = await self._delete_prefix_from_oci(safe_prefix)
        return PrefixDeleteResult(
            deleted=local.deleted + remote.deleted, failed=local.failed + remote.failed
        )

    def _put_to_local(self, safe_key: str, data: bytes) -> str:
        """ローカル保存先へ保存する。"""
        root = Path(self._settings.local_storage_dir).expanduser().resolve()
        path = (root / "objects" / safe_key).resolve()
        if not path.is_relative_to(root):
            raise ValueError("保存先キーが不正です。")
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, data)
        return f"local://{safe_key}"

    def _get_from_local(self, key: str) -> bytes:
        """ローカル保存先から取得する。"""
        safe_key = _local_object_key(key)
        root = Path(self._settings.local_storage_dir).expanduser().resolve()
        path = (root / "objects" / safe_key).resolve()
        if not path.is_relative_to(root):
            raise ValueError("取得キーが不正です。")
        return path.read_bytes()

    def _delete_from_local(self, key: str) -> bool:
        """ローカル保存先から削除する。"""
        safe_key = _local_object_key(key)
        root = Path(self._settings.local_storage_dir).expanduser().resolve()
        path = (root / "objects" / safe_key).resolve()
        if not path.is_relative_to(root):
            raise ValueError("削除キーが不正です。")
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    def _delete_prefix_from_local(self, safe_prefix: str) -> PrefixDeleteResult:
        """ローカル保存先の prefix ディレクトリ配下を削除する。symlink は辿らずリンクだけ消す。"""
        objects_root = (Path(self._settings.local_storage_dir).expanduser() / "objects").resolve()
        base = (objects_root / safe_prefix.rstrip("/")).resolve()
        if not base.is_relative_to(objects_root) or base == objects_root:
            raise ValueError("削除 prefix が不正です。")
        if not base.is_dir():
            return PrefixDeleteResult(deleted=0, failed=0)
        deleted = 0
        failed = 0
        for dirpath, dirnames, filenames in os.walk(base, topdown=False):
            current = Path(dirpath)
            for name in filenames:
                try:
                    (current / name).unlink()
                    deleted += 1
                except OSError:
                    failed += 1
            for name in dirnames:
                child = current / name
                try:
                    if child.is_symlink():
                        child.unlink()
                        deleted += 1
                    else:
                        child.rmdir()
                except OSError:
                    # 消せなかったファイルが残るディレクトリは残す（失敗はファイル側で数えている）。
                    continue
        with contextlib.suppress(OSError):
            base.rmdir()
        return PrefixDeleteResult(deleted=deleted, failed=failed)

    async def _delete_prefix_from_oci(self, safe_prefix: str) -> PrefixDeleteResult:
        """OCI Object Storage の prefix 配下を一覧してから 1 件ずつ削除する。"""
        namespace, bucket = self._require_oci_location()
        object_names: list[str] = []
        start: str | None = None
        while True:
            response = await self._sdk_call_runner(
                partial(self._list_objects_page, namespace, bucket, safe_prefix, start)
            )
            data = getattr(response, "data", None)
            for summary in getattr(data, "objects", None) or []:
                name = getattr(summary, "name", None)
                # prefix 外の名前は返らない前提だが、念のため削除対象から外す。
                if isinstance(name, str) and name.startswith(safe_prefix):
                    object_names.append(name)
            next_start = getattr(data, "next_start_with", None)
            if not isinstance(next_start, str) or not next_start or next_start == start:
                break
            start = next_start
        deleted = 0
        failed = 0
        for object_name in object_names:
            try:
                await self._sdk_call_runner(
                    partial(self._delete_oci_object, namespace, bucket, object_name)
                )
                deleted += 1
            except Exception as exc:  # noqa: BLE001 - 個々の失敗は数えて残りを続ける
                # 一覧の後に別の処理が消した object は、失敗に数えない。
                if not is_oci_object_not_found(exc):
                    failed += 1
        return PrefixDeleteResult(deleted=deleted, failed=failed)

    def _list_objects_page(
        self, namespace: str, bucket: str, prefix: str, start: str | None
    ) -> Any:
        """OCI Object Storage の object 名を 1 ページ分一覧する（SDK 呼び出しは thread で行う）。"""
        return self._client().list_objects(
            namespace, bucket, prefix=prefix, start=start, fields="name"
        )

    def _delete_oci_object(self, namespace: str, bucket: str, object_name: str) -> Any:
        """OCI Object Storage の object を 1 件削除する（SDK 呼び出しは thread で行う）。"""
        return self._client().delete_object(namespace, bucket, object_name)

    async def _put_to_oci(self, key: str, data: bytes, content_type: str) -> str:
        """OCI Object Storage へ保存する。"""
        namespace, bucket = self._require_oci_location()
        await self._sdk_call_runner(
            lambda: self._client().put_object(
                namespace,
                bucket,
                key,
                data,
                content_type=content_type,
            )
        )
        return _oci_storage_uri(namespace, bucket, key)

    async def _get_from_oci(self, key: str) -> bytes:
        """OCI Object Storage から取得する。object が無ければ FileNotFoundError。"""
        namespace, bucket, object_key = self._oci_location(key)
        try:
            response = await self._sdk_call_runner(
                lambda: self._client().get_object(namespace, bucket, object_key)
            )
        except Exception as exc:
            if is_oci_object_not_found(exc):
                raise FileNotFoundError("OCI Object Storage の object が見つかりません。") from exc
            raise
        return _response_body_to_bytes(response)

    async def _delete_from_oci(self, key: str) -> bool:
        """OCI Object Storage から削除する。object が無ければ False（local と同じ）。"""
        namespace, bucket, object_key = self._oci_location(key)
        try:
            await self._sdk_call_runner(
                lambda: self._client().delete_object(namespace, bucket, object_key)
            )
        except Exception as exc:
            if is_oci_object_not_found(exc):
                return False
            raise
        return True

    def _oci_location(self, reference: str) -> tuple[str, str, str]:
        """参照から (namespace, bucket, object key) を決める。

        `oci://` の参照は保存したときの bucket から読む（bucket の設定を変えた後も、前の bucket の
        文書を読み・消せるように。#2093）。namespace（テナンシ）は今の設定と同じものに限る。
        key だけの参照は今の設定の bucket。
        """
        normalized = reference.strip()
        if not normalized.startswith("oci://"):
            namespace, bucket = self._require_oci_location()
            return namespace, bucket, _oci_object_key(normalized)
        namespace = self._settings.object_storage_namespace.strip()
        if not namespace:
            raise ValueError("OCI Object Storage の namespace が未設定です。")
        parts = normalized.removeprefix("oci://").split("/", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("OCI Object Storage URI が不正です。")
        actual_namespace, bucket, object_key = parts
        if actual_namespace != namespace:
            raise ValueError("OCI Object Storage URI の namespace が設定と一致しません。")
        if not _OBJECT_STORAGE_NAME_RE.fullmatch(bucket):
            raise ValueError("OCI Object Storage URI の bucket 名が不正です。")
        return namespace, bucket, _oci_object_key(object_key)

    def _client(self) -> ObjectStorageSdkClientProtocol:
        """OCI Object Storage client を遅延初期化する。"""
        if self._storage_client is not None:
            return self._storage_client

        oci_config = importlib.import_module("oci.config")
        object_storage = importlib.import_module("oci.object_storage")
        oci_retry = importlib.import_module("oci.retry")
        object_storage_region = self._settings.object_storage_region.strip()
        region = object_storage_region or None
        config = load_oci_config_without_prompt(
            oci_config,
            self._settings.oci_config_file,
            self._settings.oci_config_profile,
            region=region,
        )
        self._storage_client = object_storage.ObjectStorageClient(
            config,
            timeout=(OCI_CONNECT_TIMEOUT_SECONDS, OCI_READ_TIMEOUT_SECONDS),
            retry_strategy=oci_retry.RetryStrategyBuilder(
                max_attempts=OCI_MAX_ATTEMPTS,
                total_elapsed_time_seconds=OCI_RETRY_TOTAL_ELAPSED_SECONDS,
                retry_max_wait_between_calls_seconds=OCI_RETRY_MAX_WAIT_SECONDS,
            ).get_retry_strategy(),
        )
        return self._storage_client

    def _require_oci_location(self) -> tuple[str, str]:
        """OCI Object Storage の namespace/bucket 設定を検証する。"""
        namespace = self._settings.object_storage_namespace.strip()
        bucket = self._settings.object_storage_bucket.strip()
        if not namespace or not bucket:
            raise ValueError("OCI Object Storage の namespace / bucket が未設定です。")
        return namespace, bucket


def _safe_key(key: str, *, reject_relative_segments: bool = False) -> str:
    """Object Storage キーとして扱える文字だけに正規化する。"""
    normalized = key.strip().replace("\\", "/")
    if reject_relative_segments and any(part in (".", "..") for part in normalized.split("/")):
        raise ValueError("Object Storage キーに相対パス要素は指定できません。")
    normalized = re.sub(r"[^A-Za-z0-9._/\-]", "_", normalized)
    parts = [part for part in normalized.split("/") if part not in ("", ".", "..")]
    if not parts:
        raise ValueError("Object Storage キーが空です。")
    if len(parts) > MAX_OBJECT_KEY_DEPTH:
        raise ValueError("Object Storage キーの階層が深すぎます。")
    if any(len(part) > MAX_OBJECT_KEY_PART_LENGTH for part in parts):
        raise ValueError("Object Storage キーの各要素は 255 文字以下にしてください。")
    safe_key = "/".join(parts)
    if len(safe_key) > MAX_OBJECT_KEY_LENGTH:
        raise ValueError("Object Storage キーが長すぎます。")
    return safe_key


def _local_object_key(key: str) -> str:
    """local upload storage backend で扱える参照だけをキー化する。"""
    if "://" in key and not key.startswith("local://"):
        raise ValueError("ローカルモードでは local:// URI のみ取得できます。")
    return _safe_key(key.removeprefix("local://"), reject_relative_segments=True)


def _local_storage_key(key: str) -> str:
    """local upload storage backend の保存キーとして扱える値だけをキー化する。"""
    if "://" in key:
        raise ValueError("保存先キーに URI は指定できません。")
    return _safe_key(key)


def _delete_prefix_key(prefix: str) -> str:
    """prefix 単位の削除の対象を `a/b/` の形へ正規化する。

    浅すぎる prefix・URI・相対パス要素と、`/` で終わらない（ファイル名の前方一致になる）
    ものは拒否する。
    """
    normalized = prefix.strip().replace("\\", "/")
    if "://" in normalized:
        raise ValueError("削除 prefix に URI は指定できません。")
    if not normalized.endswith("/"):
        raise ValueError("削除 prefix は `/` で終わるディレクトリ単位で指定してください。")
    safe_prefix = _safe_key(normalized, reject_relative_segments=True)
    if len(safe_prefix.split("/")) < MIN_DELETE_PREFIX_DEPTH:
        raise ValueError("削除 prefix の階層が浅すぎます。")
    return f"{safe_prefix}/"


def _oci_object_key(object_key: str) -> str:
    """OCI Object Storage で取得対象にできる object key に正規化する（URI は受けない）。"""
    if "://" in object_key:
        raise ValueError("OCI Object Storage では oci:// URI のみ取得できます。")
    return _safe_key(object_key, reject_relative_segments=True)


def is_oci_object_not_found(exc: BaseException) -> bool:
    """OCI SDK の「object が無い」（404 ObjectNotFound）か。bucket が無い 404 は含めない。"""
    return getattr(exc, "status", None) == 404 and getattr(exc, "code", None) == "ObjectNotFound"


# OCI に届かない（DNS・接続・時間切れ）ときの例外の型の名前（OCI SDK・requests・urllib3）。
# oci を import せずに判定する（local の保存先だけの環境・テストでも使う）。
_OCI_UNREACHABLE_ERROR_NAMES = frozenset(
    {"RequestException", "ConnectTimeout", "ConnectionError", "Timeout", "ReadTimeout"}
)


def describe_upload_storage_error(exc: BaseException) -> str | None:
    """保存先への保存の失敗を、利用者が直す場所の分かる文言にする（#2093）。

    分からない失敗は None（呼び出し側の既定の文言を使う）。namespace・bucket の値は入れない。
    """
    status = getattr(exc, "status", None)
    if isinstance(status, int) and getattr(exc, "code", None) is not None:
        if status == 404:
            return (
                "Object Storage の bucket が見つからないか、bucket に書き込む権限がありません。"
                "システム設定 > アップロード保存先 の bucket 名と、OCI の IAM ポリシーを"
                "確認してください。"
            )
        if status == 401:
            return (
                "OCI の認証に失敗したため、Object Storage に保存できませんでした。"
                "システム設定 > OCI 認証 の設定を確認してください。"
            )
        return None
    if any(cls.__name__ in _OCI_UNREACHABLE_ERROR_NAMES for cls in type(exc).__mro__):
        return (
            "Object Storage に接続できませんでした。ネットワークと、システム設定 > "
            "アップロード保存先 のリージョンを確認してから、もう一度アップロードしてください。"
        )
    return None


def _oci_storage_uri(namespace: str, bucket: str, key: str) -> str:
    """保存済み object の参照 URI を生成する。"""
    return f"oci://{namespace}/{bucket}/{key}"


def _atomic_write(path: Path, data: bytes) -> None:
    """同一ディレクトリ内の一時ファイル経由で原子的に書き込む。"""
    temporary_path = path.with_name(f".tmp-{uuid4().hex}")
    try:
        temporary_path.write_bytes(data)
        temporary_path.replace(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


async def _run_sdk_call_in_thread(operation: Callable[[], Any]) -> Any:
    """同期 OCI SDK 呼び出しを event loop 外で実行する。"""
    return await asyncio.to_thread(operation)


def _response_body_to_bytes(response: Any) -> bytes:
    """OCI SDK response から object body bytes を取り出す。"""
    data = getattr(response, "data", None)
    if isinstance(data, bytes):
        return data
    content = getattr(data, "content", None)
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    read = getattr(data, "read", None)
    if callable(read):
        body = read()
        if isinstance(body, bytes):
            return body
        if isinstance(body, str):
            return body.encode("utf-8")
    raise ValueError("OCI Object Storage response に bytes body がありません。")
