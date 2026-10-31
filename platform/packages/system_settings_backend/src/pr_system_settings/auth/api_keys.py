"""外部の MCP クライアント向けの API キー（3 製品共通。#1805・#1874）。

- 形式は `prak_<id>_<秘密>`。秘密は作成の応答で 1 回だけ返し、保存するのは全体の SHA-256 だけ。
- 各利用者が自分で発行する（セルフサービス。1 人 `API_KEY_MAX_PER_USER` 件まで）。キーは
  発行した利用者として動く。権限と対象範囲は、呼び先の製品が利用者の
  現在のロールから毎回計算する（ロールを変えればキーの権限も変わる）。
- 使える製品（`rag` / `nl2sql` / `agent`）を選ぶ。キーは選んだ製品の MCP の入口
  （`POST /api/mcp`）だけで使える（画面用の Cookie / CSRF の API には使わせない）。Agent の MCP は
  業務 Agent へのチャットのツールを外部の MCP クライアントに出す（#1874）。
- 製品ごとの対象の範囲（`resource_scopes`。#1874）: キーで使える対象を絞る（最小権限）。今は
  Agent の業務 Agent（`{"agent": [<業務 Agent の ID>…]}`）だけ。製品の key が無ければ、その製品では
  利用者が使えるすべての対象（キーは利用者の権限を広げない。呼び先が「利用者の権限 ∩ キーの範囲」
  で判定する）。
- 呼び出しの回数の上限（#1874）: キーごとの 1 分あたりの呼び出しの回数
  （`PLATFORM_API_KEY_CALLS_PER_MINUTE`。`ApiKeyRateLimiter`）。プロセスの中で数えるので、
  worker が複数なら worker ごとの上限になる（乱用・誤用のループを止めるための目安）。
- 保存先は共通の `PLATFORM_API_KEYS`。最後に使った日時は、保存の負荷を抑えるため
  10 分に 1 回だけ更新する（条件付きの UPDATE なので、worker・製品をまたいでも 1 回）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import threading
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

from .errors import SecurityMigrationRequired, missing_security_migration_object

API_KEYS_TABLE = "PLATFORM_API_KEYS"
API_KEY_PREFIX = "prak_"
API_KEY_PRODUCTS: tuple[str, ...] = ("rag", "nl2sql", "agent")
ApiKeyProduct = Literal["rag", "nl2sql", "agent"]
ApiKeyExpiryDays = Literal[30, 90, 365]
API_KEY_NAME_MAX_CHARS = 100
# 1 人が持てるキーの数（端末・用途ごとに分けられる程度）。
API_KEY_MAX_PER_USER = 10
LAST_USED_PERSIST_INTERVAL = timedelta(minutes=10)
# 対象の範囲を選べる製品（#1874。Agent の業務 Agent）。RAG / NL2SQL の範囲はまだ持たない
# （受け付けると、絞ったつもりで絞れていないキーになるため、検証で断る）。
API_KEY_SCOPED_PRODUCTS: tuple[str, ...] = ("agent",)
API_KEY_SCOPE_MAX_ITEMS = 50
# 対象の ID（Agent の業務 Agent の ID と同じ形。URL の path に置ける文字）。
_SCOPE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
# 1 分あたりの呼び出しの回数の既定（`PLATFORM_API_KEY_CALLS_PER_MINUTE`。0 は上限なし）。
# MCP のクライアントは接続のたびに initialize・tools/list を送り、ツールを続けて呼ぶ。人が使う
# 範囲では届かず、繰り返しの誤用（ループ）を 1 分で止められる値にする。
DEFAULT_API_KEY_CALLS_PER_MINUTE = 60
_TOKEN_PATTERN = re.compile(r"^prak_([0-9a-f]{16})_([A-Za-z0-9_-]{43})$")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_api_key(token: str) -> bool:
    return token.startswith(API_KEY_PREFIX)


def parse_api_key_id(token: str) -> str | None:
    """キーの ID（形式が違えば None）。"""
    match = _TOKEN_PATTERN.match(token)
    return match.group(1) if match else None


def bearer_api_key(authorization: str) -> str | None:
    """`Authorization: Bearer prak_…` のキー。それ以外（サービストークン・空）は None。"""
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not looks_like_api_key(token):
        return None
    return token


@dataclass(slots=True)
class ApiKeyRecord:
    """保存する API キー（秘密は持たない）。"""

    key_id: str
    name: str
    token_prefix: str
    token_hash: str
    # 実行する利用者（キーはこの利用者として動く）。
    owner_user_uuid: str
    products: list[str]
    created_at: datetime = field(default_factory=_now)
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    # 製品 → 使える対象の ID（#1874）。製品の key が無ければその製品のすべての対象。
    resource_scopes: dict[str, list[str]] = field(default_factory=dict)

    def expired(self, now: datetime | None = None) -> bool:
        expires_at = _aware(self.expires_at)
        return expires_at is not None and expires_at <= (now or _now())

    def scope(self, product: str) -> frozenset[str] | None:
        """この製品で使える対象の ID（None は範囲なし = 利用者が使えるすべて）。"""
        items = self.resource_scopes.get(product)
        return None if items is None else frozenset(items)


def new_api_key(
    *,
    name: str,
    owner_user_uuid: str,
    products: list[str],
    expires_in_days: int | None = None,
    resource_scopes: Mapping[str, list[str]] | None = None,
    now: datetime | None = None,
) -> tuple[ApiKeyRecord, str]:
    """新しいキー（保存する record と、1 回だけ返す秘密の token）。"""
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    token = f"{API_KEY_PREFIX}{key_id}_{secret}"
    created_at = now or _now()
    record = ApiKeyRecord(
        key_id=key_id,
        name=name,
        token_prefix=f"{API_KEY_PREFIX}{key_id}_{secret[:4]}",
        token_hash=hash_api_key(token),
        owner_user_uuid=owner_user_uuid,
        products=sorted(set(products), key=API_KEY_PRODUCTS.index),
        created_at=created_at,
        expires_at=created_at + timedelta(days=expires_in_days) if expires_in_days else None,
        resource_scopes={key: list(value) for key, value in (resource_scopes or {}).items()},
    )
    return record, token


def token_matches(record: ApiKeyRecord, token: str) -> bool:
    return hmac.compare_digest(record.token_hash, hash_api_key(token))


def dump_resource_scopes(scopes: Mapping[str, list[str]]) -> str | None:
    """保存する範囲（JSON。範囲が無ければ NULL）。"""
    return json.dumps(dict(scopes), ensure_ascii=False, sort_keys=True) if scopes else None


def load_resource_scopes(value: object) -> dict[str, list[str]]:
    """保存した範囲（JSON の CLOB）。

    壊れた値を「範囲なし（すべて）」として通さず、どの製品の対象も使えない範囲にする。
    """
    if value is None:
        return {}
    read = getattr(value, "read", None)
    text = read() if callable(read) else value
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {product: [] for product in API_KEY_PRODUCTS}
    return {
        str(product): (
            [str(item) for item in items if isinstance(item, str)]
            if isinstance(items, list)
            else []
        )
        for product, items in data.items()
    }


# ---- 呼び出しの回数の上限（#1874） ---------------------------------------------------


class ApiKeyRateLimiter:
    """キーごとの 1 分あたりの呼び出しの回数（直近 60 秒の窓。プロセスの中で数える）。"""

    window = timedelta(minutes=1)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, deque[datetime]] = {}

    def acquire(self, key_id: str, *, limit: int, now: datetime | None = None) -> int | None:
        """1 回数える。上限に達していたら数えずに、再試行までの秒数を返す（通れば None）。"""
        if limit <= 0:
            return None
        current = now or _now()
        with self._lock:
            calls = self._calls.setdefault(key_id, deque())
            while calls and current - calls[0] >= self.window:
                calls.popleft()
            if len(calls) >= limit:
                return max(1, math.ceil((calls[0] + self.window - current).total_seconds()))
            calls.append(current)
            return None

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()


api_key_rate_limiter = ApiKeyRateLimiter()


# ---- 保存 -------------------------------------------------------------------------


class ApiKeyStore(Protocol):
    def create(self, record: ApiKeyRecord) -> None: ...
    def get(self, key_id: str) -> ApiKeyRecord | None: ...
    def list(self) -> list[ApiKeyRecord]: ...
    def delete(self, key_id: str) -> bool: ...
    def delete_owned_by(self, user_uuid: str) -> int: ...
    def touch_last_used(self, key_id: str, *, now: datetime) -> None: ...


class InMemoryApiKeyStore:
    """プロセスの中だけの保存（テスト・DB を使わない構成）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ApiKeyRecord] = {}

    def create(self, record: ApiKeyRecord) -> None:
        with self._lock:
            self._records[record.key_id] = replace(record)

    def get(self, key_id: str) -> ApiKeyRecord | None:
        with self._lock:
            record = self._records.get(key_id)
            return replace(record) if record is not None else None

    def list(self) -> list[ApiKeyRecord]:
        with self._lock:
            records = [replace(record) for record in self._records.values()]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def delete(self, key_id: str) -> bool:
        with self._lock:
            return self._records.pop(key_id, None) is not None

    def delete_owned_by(self, user_uuid: str) -> int:
        with self._lock:
            owned = [
                key for key, item in self._records.items() if item.owner_user_uuid == user_uuid
            ]
            for key in owned:
                del self._records[key]
            return len(owned)

    def touch_last_used(self, key_id: str, *, now: datetime) -> None:
        with self._lock:
            record = self._records.get(key_id)
            if record is None:
                return
            last_used = _aware(record.last_used_at)
            if last_used is None or now - last_used >= LAST_USED_PERSIST_INTERVAL:
                record.last_used_at = now


_SELECT_COLUMNS = (
    "KEY_ID, NAME, TOKEN_PREFIX, TOKEN_HASH, OWNER_USER_UUID, PRODUCTS, "
    "CREATED_AT, EXPIRES_AT, LAST_USED_AT, RESOURCE_SCOPES"
)


def _record_from_row(row: Any) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=str(row[0]),
        name=str(row[1]),
        token_prefix=str(row[2]),
        token_hash=str(row[3]),
        owner_user_uuid=str(row[4]),
        products=[item for item in str(row[5] or "").split(",") if item],
        created_at=_aware(row[6]) or _now(),
        expires_at=_aware(row[7]),
        last_used_at=_aware(row[8]),
        resource_scopes=load_resource_scopes(row[9]),
    )


class OracleApiKeyStore:
    """`PLATFORM_API_KEYS`（3 製品で共有）の保存。接続は共通認証の store と同じものを使う。"""

    def __init__(self, connection_factory: Any) -> None:
        self._connection_factory = connection_factory

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                yield connection
        except SecurityMigrationRequired:
            raise
        except Exception as exc:
            # この store の SQL は PLATFORM_API_KEYS だけを読むので、名前の無い ORA-00942 も
            # この表が無い（共通認証の DDL が未適用）とみなす。ORA-00904 は列が無い（#1874 で
            # 足した RESOURCE_SCOPES の DDL が未適用）。
            message = str(exc).upper()
            if (
                missing_security_migration_object(exc, frozenset({API_KEYS_TABLE})) is not None
                or "ORA-00942" in message
                or "ORA-00904" in message
            ):
                raise SecurityMigrationRequired(API_KEYS_TABLE) from exc
            raise

    def create(self, record: ApiKeyRecord) -> None:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO PLATFORM_API_KEYS
                  (KEY_ID, NAME, TOKEN_PREFIX, TOKEN_HASH, OWNER_USER_UUID,
                   PRODUCTS, CREATED_AT, EXPIRES_AT, RESOURCE_SCOPES)
                VALUES
                  (:key_id, :name, :token_prefix, :token_hash, :owner,
                   :products, :created_at, :expires_at, :resource_scopes)
                """,
                {
                    "key_id": record.key_id,
                    "name": record.name,
                    "token_prefix": record.token_prefix,
                    "token_hash": record.token_hash,
                    "owner": record.owner_user_uuid,
                    "products": ",".join(record.products),
                    "created_at": record.created_at,
                    "expires_at": record.expires_at,
                    "resource_scopes": dump_resource_scopes(record.resource_scopes),
                },
            )
            conn.commit()

    def get(self, key_id: str) -> ApiKeyRecord | None:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM PLATFORM_API_KEYS "  # nosec B608 - 固定の列名
                "WHERE KEY_ID = :key_id",
                {"key_id": key_id},
            )
            row = cursor.fetchone()
            return _record_from_row(row) if row else None

    def list(self) -> list[ApiKeyRecord]:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM PLATFORM_API_KEYS "  # nosec B608 - 固定の列名
                "ORDER BY CREATED_AT DESC, KEY_ID"
            )
            return [_record_from_row(row) for row in cursor.fetchall()]

    def delete(self, key_id: str) -> bool:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM PLATFORM_API_KEYS WHERE KEY_ID = :key_id", {"key_id": key_id}
            )
            deleted = int(cursor.rowcount or 0)
            conn.commit()
            return deleted > 0

    def delete_owned_by(self, user_uuid: str) -> int:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM PLATFORM_API_KEYS WHERE OWNER_USER_UUID = :owner", {"owner": user_uuid}
            )
            deleted = int(cursor.rowcount or 0)
            conn.commit()
            return deleted

    def touch_last_used(self, key_id: str, *, now: datetime) -> None:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE PLATFORM_API_KEYS SET LAST_USED_AT = :now
                 WHERE KEY_ID = :key_id
                   AND (LAST_USED_AT IS NULL OR LAST_USED_AT <= :threshold)
                """,
                {"now": now, "key_id": key_id, "threshold": now - LAST_USED_PERSIST_INTERVAL},
            )
            conn.commit()


# ---- API の入出力 -------------------------------------------------------------------


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=API_KEY_NAME_MAX_CHARS)
    # 使える製品（1 つ以上）。
    products: list[ApiKeyProduct] = Field(min_length=1, max_length=len(API_KEY_PRODUCTS))
    # 省く・null は無期限。
    expires_in_days: ApiKeyExpiryDays | None = 90
    # 製品 → 使える対象の ID（#1874）。省いた製品はすべての対象。今は agent（業務 Agent）だけ。
    resource_scopes: dict[ApiKeyProduct, list[str]] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("名前を入力してください。")
        return value.strip()

    @field_validator("products")
    @classmethod
    def _products(cls, value: list[ApiKeyProduct]) -> list[ApiKeyProduct]:
        return sorted(set(value), key=API_KEY_PRODUCTS.index)

    @field_validator("resource_scopes")
    @classmethod
    def _resource_scopes(
        cls, value: dict[ApiKeyProduct, list[str]]
    ) -> dict[ApiKeyProduct, list[str]]:
        normalized: dict[ApiKeyProduct, list[str]] = {}
        for product, items in value.items():
            if product not in API_KEY_SCOPED_PRODUCTS:
                raise ValueError(f"{product} の対象の範囲はまだ選べません。")
            unique = list(dict.fromkeys(item.strip() for item in items))
            if not unique or len(unique) > API_KEY_SCOPE_MAX_ITEMS:
                raise ValueError(
                    f"対象は 1〜{API_KEY_SCOPE_MAX_ITEMS} 件選んでください"
                    "（すべての対象を使うときは範囲を指定しません）。"
                )
            if any(not _SCOPE_ID_PATTERN.fullmatch(item) for item in unique):
                raise ValueError("対象の ID の形が正しくありません。")
            normalized[product] = unique
        return normalized

    @model_validator(mode="after")
    def _scopes_within_products(self) -> ApiKeyCreateRequest:
        if set(self.resource_scopes) - set(self.products):
            raise ValueError("使える製品に選んでいない製品の範囲は指定できません。")
        return self


class ApiKeyView(BaseModel):
    """画面に返す API キー（hash を含めない）。"""

    id: str
    name: str
    token_prefix: str
    products: list[str]
    owner_user_uuid: str
    owner_display_name: str = ""
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    expired: bool
    # 製品 → 使える対象の ID（#1874）。製品の key が無ければすべて。
    resource_scopes: dict[str, list[str]] = Field(default_factory=dict)


class ApiKeysListData(BaseModel):
    keys: list[ApiKeyView] = Field(default_factory=list)


class MyApiKeysListData(ApiKeysListData):
    """自分のキーの一覧。画面の接続先に出す公開の URL も返す（#1870）。"""

    # 配備（`platform/deploy/suite-init.sh`）が書く `PLATFORM_PUBLIC_BASE_URL`
    # （例 `https://<公開 IP>`）。
    # 未設定・不正な値は空（画面は今開いている URL だけを出す）。
    public_base_url: str = ""


def normalize_public_base_url(value: str) -> str:
    """公開の基底の URL を `scheme://host[:port]`（末尾の `/` なし）にそろえる。

    http(s) 以外・path を含む値は空にする。
    """
    text = value.strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s/?#]+", text):
        return ""
    return text


class ApiKeyCreated(BaseModel):
    """作成の応答。`token` はこの 1 回だけ返す。"""

    key: ApiKeyView
    token: str


def api_key_view(record: ApiKeyRecord, names: dict[str, str] | None = None) -> ApiKeyView:
    names = names or {}
    return ApiKeyView(
        id=record.key_id,
        name=record.name,
        token_prefix=record.token_prefix,
        products=list(record.products),
        owner_user_uuid=record.owner_user_uuid,
        owner_display_name=names.get(record.owner_user_uuid, ""),
        created_at=record.created_at,
        expires_at=record.expires_at,
        last_used_at=record.last_used_at,
        expired=record.expired(),
        resource_scopes={key: list(value) for key, value in record.resource_scopes.items()},
    )
