"""共通認証の永続化（PLATFORM_* テーブル。NL2SQL の実装を基準に移設。#212）。

ユーザー・ロール・ユーザーとロールの割り当て・セッションを扱う。ロールに付ける製品固有の
権限・対象範囲は、製品が `OracleAuthStore` を継承して hook（`_role_details` /
`_replace_role_details` / `_before_delete_role`）で読み書きする。DDL は `migrations` の責務。
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from ..answer_notice import ANSWER_NOTICE_TABLE, OracleAnswerNoticeStore, answer_notice_setting_key
from ..chat_attachments.settings import (
    OracleChatAttachmentSettingsStore,
    chat_attachment_setting_key,
)
from ..chat_attachments.store import OracleChatAttachmentStore
from ..trace_recording import OracleTraceRecordingStore
from .api_keys import API_KEYS_TABLE, OracleApiKeyStore
from .domain import (
    SYSTEM_ADMIN_ROLE_CODE,
    SYSTEM_ADMIN_ROLE_DISPLAY_NAME,
    SYSTEM_ADMIN_ROLE_ID,
    RoleRecord,
    SessionRecord,
    UserIdentity,
    UserRecord,
)
from .errors import (
    SecurityConflict,
    SecurityMigrationRequired,
    SecurityNotFound,
    SecurityStoreError,
    missing_security_migration_object,
)
from .login_throttle import LOGIN_ATTEMPTS_TABLE, OracleLoginAttemptStore

USERS_TABLE = "PLATFORM_USERS"
ROLES_TABLE = "PLATFORM_ROLES"
USER_ROLES_TABLE = "PLATFORM_USER_ROLES"
SESSIONS_TABLE = "PLATFORM_AUTH_SESSIONS"
PLATFORM_AUTH_TABLES = (
    USERS_TABLE,
    ROLES_TABLE,
    USER_ROLES_TABLE,
    SESSIONS_TABLE,
    LOGIN_ATTEMPTS_TABLE,
    API_KEYS_TABLE,
    ANSWER_NOTICE_TABLE,
)

# 製品ごとの「ロールに付けた権限コード」テーブル（ROLE_ID, PERMISSION_CODE の形）。
# 製品をまたぐ権限昇格の判定で使う。未配備の製品のテーブルは空として扱う。
PRODUCT_ROLE_PERMISSION_TABLES = {
    "nl2sql": "NL2SQL_APP_ROLE_PERMISSIONS",
    "rag": "RAG_ROLE_PERMISSIONS",
    "agent": "AGENT_ROLE_PERMISSIONS",
}

SYSTEM_ADMIN_ROLE_DESCRIPTION = "すべてのアプリケーション機能を管理する組み込みロールです。"

_LOGIN_USER_ID_CONFLICT_MESSAGE = (
    "このログインユーザーIDは既に使用されています。別のIDを入力してください。"
)
_ROLE_CODE_CONFLICT_MESSAGE = (
    "このロールコードは既に使用されています。別のコードを入力してください。"
)
_ROLE_REFERENCED_MESSAGE = (
    "このロールには他の製品の設定が残っています。"
    "各製品の権限管理で設定を解除してから削除してください。"
)

# 役割 → 権限コードの集合（テーブル単位）。
RolePermissionCodes = dict[str, dict[str, set[str]]]

# 1 つの IN に並べる bind の上限（Oracle の IN リストは 1,000 件まで。ORA-01795）。
IN_LIST_CHUNK_SIZE = 1000


def in_list_binds(
    prefix: str, values: Iterable[str], *, chunk_size: int = IN_LIST_CHUNK_SIZE
) -> Iterator[tuple[str, dict[str, str]]]:
    """`IN (...)` の placeholder と bind を、1,000 件ごとに分けて返す（#793）。

    展開するのは生成した bind 名だけで、値はすべて bind する。重複は 1 つにまとめる。
    """
    unique = list(dict.fromkeys(values))
    size = max(1, min(int(chunk_size), IN_LIST_CHUNK_SIZE))
    for offset in range(0, len(unique), size):
        chunk = unique[offset : offset + size]
        binds = {f"{prefix}{index}": value for index, value in enumerate(chunk)}
        yield ", ".join(f":{key}" for key in binds), binds


def values_by_role_id(cursor: Any, select: str, role_ids: Sequence[str]) -> dict[str, set[str]]:
    """`<select> WHERE ROLE_ID IN (...)` の (ROLE_ID, 値) をロールごとに集める（#793）。

    製品の一括の hook（`_roles_details`）用。`select` は製品が書く固定の
    `SELECT ROLE_ID, <列> FROM <表>` で、ロールの数によらず 1,000 件ごとに 1 回だけ実行する。
    """
    values: dict[str, set[str]] = {}
    for placeholders, binds in in_list_binds("role_", role_ids):
        # 展開するのは固定の SELECT と生成した bind 名だけ。値はすべて bind する。
        cursor.execute(f"{select} WHERE ROLE_ID IN ({placeholders})", binds)  # nosec B608
        for role_id, value in cursor.fetchall():
            values.setdefault(str(role_id), set()).add(str(value))
    return values


class AuthStore(Protocol):
    def bootstrap(self, *, login_user_id: str, display_name: str, password_hash: str) -> bool: ...
    def get_user_by_login_user_id(self, normalized_login_user_id: str) -> UserRecord | None: ...
    def get_user(self, user_uuid: str) -> UserRecord | None: ...
    def get_user_identities(self, user_uuids: list[str]) -> dict[str, UserIdentity]: ...
    def list_users(self) -> list[UserRecord]: ...
    def create_user(self, user: UserRecord) -> UserRecord: ...
    def update_user(
        self,
        user_uuid: str,
        *,
        expected_version: int,
        display_name: str,
        status: str,
        role_ids: list[str],
    ) -> UserRecord: ...
    def delete_user(self, user_uuid: str, *, expected_version: int) -> None: ...
    def set_password(self, user_uuid: str, password_hash: str, *, force_change: bool) -> None: ...
    def record_login_failure(
        self, user_uuid: str, *, failed_count: int, locked_until: datetime | None
    ) -> None: ...
    def record_login_success(self, user_uuid: str, *, password_hash: str | None = None) -> None: ...
    def list_roles(self, *, include_archived: bool = False) -> Sequence[RoleRecord]: ...
    def get_role(self, role_id: str) -> RoleRecord | None: ...
    def get_roles(self, role_ids: Sequence[str]) -> Sequence[RoleRecord]: ...
    def create_role(self, role: RoleRecord) -> RoleRecord: ...
    def update_role(self, role: RoleRecord, *, expected_version: int) -> RoleRecord: ...
    def archive_role(self, role_id: str, *, expected_version: int) -> RoleRecord: ...
    def restore_role(self, role_id: str, *, expected_version: int) -> RoleRecord: ...
    def delete_role(self, role_id: str, *, expected_version: int) -> None: ...
    def count_active_system_admins(self) -> int: ...
    def create_session(self, session: SessionRecord) -> None: ...
    def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None: ...
    def touch_session(
        self, session_id: str, *, last_seen_at: datetime, idle_expires_at: datetime
    ) -> None: ...
    def revoke_session(self, session_id: str) -> None: ...
    def revoke_user_sessions(self, user_uuid: str) -> None: ...
    def role_permission_codes(
        self, role_ids: Sequence[str], *, tables: Sequence[str]
    ) -> RolePermissionCodes: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _copy_optional[T](value: T | None) -> T | None:
    return copy.deepcopy(value) if value is not None else None


@dataclass
class InMemoryAuthStore:
    """単体テスト・local 用。production は OracleAuthStore を使う。

    製品は `role_class` に自分の RoleRecord を渡し、`_role_delete_blocker` で削除条件を足せる。
    `product_role_permissions` はテーブル名 → ロール ID → 権限コードで、他製品の権限を再現する。
    """

    role_class: type[RoleRecord] = RoleRecord
    users: dict[str, UserRecord] = field(default_factory=dict)
    roles: dict[str, RoleRecord] = field(default_factory=dict)
    sessions: dict[str, SessionRecord] = field(default_factory=dict)
    product_role_permissions: RolePermissionCodes = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def bootstrap(self, *, login_user_id: str, display_name: str, password_hash: str) -> bool:
        with self._lock:
            self._ensure_system_admin_role()
            if self.users:
                return False
            user = UserRecord(
                user_uuid=str(uuid4()),
                login_user_id=login_user_id,
                display_name=display_name,
                password_hash=password_hash,
                status="ACTIVE",
                force_password_change=True,
                failed_login_count=0,
                locked_until=None,
                version=1,
                role_ids=[SYSTEM_ADMIN_ROLE_ID],
                is_bootstrap_admin=True,
            )
            self.users[user.user_uuid] = user
            return True

    def _ensure_system_admin_role(self) -> None:
        if SYSTEM_ADMIN_ROLE_ID in self.roles:
            return
        self.roles[SYSTEM_ADMIN_ROLE_ID] = self.role_class(
            role_id=SYSTEM_ADMIN_ROLE_ID,
            role_code=SYSTEM_ADMIN_ROLE_CODE,
            display_name=SYSTEM_ADMIN_ROLE_DISPLAY_NAME,
            description=SYSTEM_ADMIN_ROLE_DESCRIPTION,
            is_built_in=True,
            archived=False,
            version=1,
        )

    def get_user_by_login_user_id(self, normalized_login_user_id: str) -> UserRecord | None:
        with self._lock:
            return _copy_optional(
                next(
                    (
                        user
                        for user in self.users.values()
                        if user.login_user_id.casefold() == normalized_login_user_id.casefold()
                    ),
                    None,
                )
            )

    def get_user(self, user_uuid: str) -> UserRecord | None:
        with self._lock:
            return _copy_optional(self.users.get(user_uuid))

    def list_users(self) -> list[UserRecord]:
        with self._lock:
            return [
                copy.deepcopy(item)
                for item in sorted(self.users.values(), key=lambda u: u.login_user_id)
            ]

    def get_user_identities(self, user_uuids: list[str]) -> dict[str, UserIdentity]:
        with self._lock:
            return {
                user_uuid: UserIdentity(user_uuid, user.login_user_id, user.display_name)
                for user_uuid in set(user_uuids)
                if user_uuid and (user := self.users.get(user_uuid)) is not None
            }

    def create_user(self, user: UserRecord) -> UserRecord:
        with self._lock:
            if any(
                item.login_user_id.casefold() == user.login_user_id.casefold()
                for item in self.users.values()
            ):
                raise SecurityConflict(
                    _LOGIN_USER_ID_CONFLICT_MESSAGE,
                    code="SECURITY_USER_LOGIN_ID_CONFLICT",
                    pointer="/login_user_id",
                    field_code="already_exists",
                )
            self._validate_role_ids(user.role_ids)
            self.users[user.user_uuid] = copy.deepcopy(user)
            return copy.deepcopy(user)

    def update_user(
        self,
        user_uuid: str,
        *,
        expected_version: int,
        display_name: str,
        status: str,
        role_ids: list[str],
    ) -> UserRecord:
        with self._lock:
            user = self.users.get(user_uuid)
            if user is None:
                raise SecurityNotFound("ユーザーが見つかりません。")
            if user.version != expected_version:
                raise SecurityConflict("ユーザーが別の操作で更新されています。")
            self._validate_role_ids(role_ids, allow_inactive_role_ids=set(user.role_ids))
            removes_last_admin = (
                user.status == "ACTIVE"
                and SYSTEM_ADMIN_ROLE_ID in user.role_ids
                and (status != "ACTIVE" or SYSTEM_ADMIN_ROLE_ID not in role_ids)
                and self.count_active_system_admins() <= 1
            )
            if removes_last_admin:
                raise SecurityConflict("最後のシステム管理者は無効化または権限解除できません。")
            user.display_name = display_name
            user.status = status
            user.role_ids = list(dict.fromkeys(role_ids))
            user.version += 1
            return copy.deepcopy(user)

    def set_password(self, user_uuid: str, password_hash: str, *, force_change: bool) -> None:
        with self._lock:
            user = self._required_user(user_uuid)
            user.password_hash = password_hash
            user.force_password_change = force_change
            user.failed_login_count = 0
            user.locked_until = None
            user.version += 1

    def delete_user(self, user_uuid: str, *, expected_version: int) -> None:
        with self._lock:
            user = self.users.get(user_uuid)
            if user is None:
                raise SecurityNotFound("ユーザーが見つかりません。")
            if user.version != expected_version:
                raise SecurityConflict("ユーザーが別の操作で更新されています。")
            if user.status != "DISABLED":
                raise SecurityConflict(
                    "ユーザーを先に無効化してから削除してください。",
                    code="SECURITY_USER_DELETE_REQUIRES_DISABLED",
                )
            if user.is_bootstrap_admin:
                raise SecurityConflict(
                    "初期システム管理者は削除できません。",
                    code="SECURITY_USER_DELETE_PROTECTED",
                )
            self.sessions = {
                session_id: session
                for session_id, session in self.sessions.items()
                if session.user_uuid != user_uuid
            }
            del self.users[user_uuid]

    def record_login_failure(
        self, user_uuid: str, *, failed_count: int, locked_until: datetime | None
    ) -> None:
        with self._lock:
            user = self._required_user(user_uuid)
            user.failed_login_count = failed_count
            user.locked_until = locked_until

    def record_login_success(self, user_uuid: str, *, password_hash: str | None = None) -> None:
        with self._lock:
            user = self._required_user(user_uuid)
            user.failed_login_count = 0
            user.locked_until = None
            if password_hash:
                user.password_hash = password_hash

    def list_roles(self, *, include_archived: bool = False) -> Sequence[RoleRecord]:
        with self._lock:
            roles = [item for item in self.roles.values() if include_archived or not item.archived]
            return [copy.deepcopy(item) for item in sorted(roles, key=lambda role: role.role_code)]

    def get_role(self, role_id: str) -> RoleRecord | None:
        with self._lock:
            return _copy_optional(self.roles.get(role_id))

    def get_roles(self, role_ids: Sequence[str]) -> Sequence[RoleRecord]:
        """指定したロールを指定の順に返す（無いものは除く。重複は 1 つ）。"""
        with self._lock:
            return [
                copy.deepcopy(role)
                for role_id in dict.fromkeys(role_ids)
                if (role := self.roles.get(role_id)) is not None
            ]

    def create_role(self, role: RoleRecord) -> RoleRecord:
        with self._lock:
            if any(item.role_code == role.role_code for item in self.roles.values()):
                raise SecurityConflict(
                    _ROLE_CODE_CONFLICT_MESSAGE,
                    code="SECURITY_ROLE_CODE_CONFLICT",
                    pointer="/role_code",
                    field_code="already_exists",
                )
            self.roles[role.role_id] = copy.deepcopy(role)
            return copy.deepcopy(role)

    def update_role(self, role: RoleRecord, *, expected_version: int) -> RoleRecord:
        with self._lock:
            current = self.roles.get(role.role_id)
            if current is None:
                raise SecurityNotFound("ロールが見つかりません。")
            if current.version != expected_version:
                raise SecurityConflict("ロールが別の操作で更新されています。")
            role.version = expected_version + 1
            self.roles[role.role_id] = copy.deepcopy(role)
            return copy.deepcopy(role)

    def archive_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        role = self.get_role(role_id)
        if role is None:
            raise SecurityNotFound("ロールが見つかりません。")
        role.archived = True
        return self.update_role(role, expected_version=expected_version)

    def restore_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        role = self.get_role(role_id)
        if role is None:
            raise SecurityNotFound("ロールが見つかりません。")
        if not role.archived:
            raise SecurityConflict("ロールはアーカイブされていません。")
        role.archived = False
        return self.update_role(role, expected_version=expected_version)

    def delete_role(self, role_id: str, *, expected_version: int) -> None:
        with self._lock:
            role = self.roles.get(role_id)
            if role is None:
                raise SecurityNotFound("ロールが見つかりません。")
            if role.version != expected_version:
                raise SecurityConflict("ロールが別の操作で更新されています。")
            if role.is_built_in:
                raise SecurityConflict(
                    "組み込み SYSTEM_ADMIN ロールは削除できません。",
                    code="SECURITY_ROLE_DELETE_PROTECTED",
                )
            if not role.archived:
                raise SecurityConflict(
                    "ロールを先にアーカイブしてから削除してください。",
                    code="SECURITY_ROLE_DELETE_REQUIRES_ARCHIVED",
                )
            if any(role_id in user.role_ids for user in self.users.values()):
                raise SecurityConflict(
                    "このロールはユーザーに割り当てられています。割り当てを解除してから削除してください。",
                    code="SECURITY_ROLE_DELETE_ASSIGNED",
                )
            blocker = self._role_delete_blocker(role)
            if blocker is not None:
                raise blocker
            del self.roles[role_id]
            for codes_by_role in self.product_role_permissions.values():
                codes_by_role.pop(role_id, None)

    def _role_delete_blocker(self, role: RoleRecord) -> SecurityConflict | None:
        """製品固有の削除条件（例: NL2SQL の Data Grant）。削除できないときは例外を返す。"""
        return None

    def count_active_system_admins(self) -> int:
        with self._lock:
            return sum(
                1
                for user in self.users.values()
                if user.status == "ACTIVE" and SYSTEM_ADMIN_ROLE_ID in user.role_ids
            )

    def create_session(self, session: SessionRecord) -> None:
        with self._lock:
            self.sessions[session.session_id] = copy.deepcopy(session)

    def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        with self._lock:
            return _copy_optional(
                next(
                    (item for item in self.sessions.values() if item.token_hash == token_hash), None
                )
            )

    def touch_session(
        self, session_id: str, *, last_seen_at: datetime, idle_expires_at: datetime
    ) -> None:
        with self._lock:
            session = self.sessions.get(session_id)
            if session:
                session.last_seen_at = last_seen_at
                session.idle_expires_at = idle_expires_at

    def revoke_session(self, session_id: str) -> None:
        with self._lock:
            if session_id in self.sessions:
                self.sessions[session_id].revoked_at = _now()

    def revoke_user_sessions(self, user_uuid: str) -> None:
        with self._lock:
            for session in self.sessions.values():
                if session.user_uuid == user_uuid and session.revoked_at is None:
                    session.revoked_at = _now()

    def role_permission_codes(
        self, role_ids: Sequence[str], *, tables: Sequence[str]
    ) -> RolePermissionCodes:
        with self._lock:
            wanted = set(role_ids)
            return {
                table: {
                    role_id: set(codes)
                    for role_id, codes in self.product_role_permissions.get(table, {}).items()
                    if role_id in wanted
                }
                for table in tables
            }

    def _required_user(self, user_uuid: str) -> UserRecord:
        user = self.users.get(user_uuid)
        if user is None:
            raise SecurityNotFound("ユーザーが見つかりません。")
        return user

    def _validate_role_ids(
        self,
        role_ids: list[str],
        *,
        allow_inactive_role_ids: set[str] | None = None,
    ) -> None:
        allowed = allow_inactive_role_ids or set()
        for role_id in role_ids:
            role = self.roles.get(role_id)
            if role is not None and not role.archived:
                continue
            if role_id in allowed:
                continue
            raise SecurityNotFound("指定された有効なロールが見つかりません。")


ConnectionFactory = Callable[[], AbstractContextManager[Any]]


class OracleAuthStore:
    """Oracle AI Database の PLATFORM_* テーブルを使う store。

    `connection_factory` は、未コミットの変更を持たない接続の context manager を返すこと。
    製品は継承して `_role_details` / `_replace_role_details` / `_before_delete_role` を実装する。
    """

    role_class: type[RoleRecord] = RoleRecord
    # ORA-00942 のとき「migration が必要」と判定する object。製品は自分のテーブルを足す。
    schema_object_names: frozenset[str] = frozenset(PLATFORM_AUTH_TABLES)

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def login_attempt_store(self) -> OracleLoginAttemptStore:
        """ログインの試行の記録（`PLATFORM_LOGIN_ATTEMPTS`。3 製品・全 worker で共有。#1173）。"""
        return OracleLoginAttemptStore(self._connection_factory)

    def api_key_store(self) -> OracleApiKeyStore:
        """API キー（`PLATFORM_API_KEYS`。3 製品で共有。#1805）。"""
        return OracleApiKeyStore(self._connection_factory)

    def trace_recording_store(self) -> OracleTraceRecordingStore:
        return OracleTraceRecordingStore(self._connection_factory)

    def chat_attachment_store(self) -> OracleChatAttachmentStore:
        """会話の添付（`PLATFORM_CHAT_ATTACHMENTS`。3 製品で共有。#2517）。"""
        return OracleChatAttachmentStore(self._connection_factory)

    def chat_attachment_settings_store(self, product_key: str) -> OracleChatAttachmentSettingsStore:
        """製品のチャットの添付を使うか（#2517）。

        `PLATFORM_CHAT_ATTACHMENT_SETTINGS` の製品の行。
        """
        return OracleChatAttachmentSettingsStore(
            self._connection_factory, chat_attachment_setting_key(product_key)
        )

    def answer_notice_store(self, product_key: str) -> OracleAnswerNoticeStore:
        """製品の回答の注意書き（`PLATFORM_ANSWER_NOTICE` の製品の行。#2405・#2449）。"""
        return OracleAnswerNoticeStore(
            self._connection_factory, answer_notice_setting_key(product_key)
        )

    @contextmanager
    def connection(self, migration_object: str | None = None) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                yield connection
        except SecurityMigrationRequired:
            raise
        except Exception as exc:
            object_name = missing_security_migration_object(exc, self.schema_object_names)
            if object_name is None and migration_object and "ORA-00942" in str(exc).upper():
                object_name = migration_object.upper()
            if object_name is not None:
                raise SecurityMigrationRequired(object_name) from exc
            raise

    def bootstrap(self, *, login_user_id: str, display_name: str, password_hash: str) -> bool:
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            # 初回 user 判定から INSERT までを DB lock で直列化し、複数 worker の
            # 同時 startup でも管理者を一度だけ作成する。
            cursor.execute("LOCK TABLE PLATFORM_USERS IN EXCLUSIVE MODE")
            cursor.execute("SELECT COUNT(*) FROM PLATFORM_USERS")
            user_count = int(cursor.fetchone()[0])
            self._merge_system_admin_role(cursor)
            if user_count:
                conn.commit()
                return False
            user_uuid = str(uuid4())
            cursor.execute(
                """
                INSERT INTO PLATFORM_USERS
                  (USER_UUID, LOGIN_USER_ID, LOGIN_USER_ID_NORMALIZED, DISPLAY_NAME, PASSWORD_HASH,
                   STATUS, FORCE_PASSWORD_CHANGE, FAILED_LOGIN_COUNT, VERSION_NO)
                VALUES
                  (:user_uuid, :login_user_id, :normalized, :display_name, :password_hash,
                   'ACTIVE', 1, 0, 1)
                """,
                {
                    "user_uuid": user_uuid,
                    "login_user_id": login_user_id,
                    "normalized": login_user_id.casefold(),
                    "display_name": display_name,
                    "password_hash": password_hash,
                },
            )
            cursor.execute(
                "INSERT INTO PLATFORM_USER_ROLES (USER_UUID, ROLE_ID) "
                "VALUES (:user_uuid, :role_id)",
                {"user_uuid": user_uuid, "role_id": SYSTEM_ADMIN_ROLE_ID},
            )
            conn.commit()
            return True

    @staticmethod
    def _merge_system_admin_role(cursor: Any) -> None:
        cursor.execute(
            """
            MERGE INTO PLATFORM_ROLES r
            USING (SELECT :role_id role_id FROM dual) s
            ON (r.ROLE_ID = s.role_id)
            WHEN NOT MATCHED THEN INSERT
              (ROLE_ID, ROLE_CODE, DISPLAY_NAME, DESCRIPTION, IS_BUILT_IN, ARCHIVED, VERSION_NO)
            VALUES
              (:role_id, :role_code, :display_name, :description, 1, 0, 1)
            """,
            {
                "role_id": SYSTEM_ADMIN_ROLE_ID,
                "role_code": SYSTEM_ADMIN_ROLE_CODE,
                "display_name": SYSTEM_ADMIN_ROLE_DISPLAY_NAME,
                "description": SYSTEM_ADMIN_ROLE_DESCRIPTION,
            },
        )

    def get_user_by_login_user_id(self, normalized_login_user_id: str) -> UserRecord | None:
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                self._user_select() + " WHERE LOGIN_USER_ID_NORMALIZED = :login",
                {"login": normalized_login_user_id.casefold()},
            )
            row = cursor.fetchone()
            return self._user_from_row(cursor, row) if row else None

    def get_user(self, user_uuid: str) -> UserRecord | None:
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                self._user_select() + " WHERE USER_UUID = :user_uuid",
                {"user_uuid": user_uuid},
            )
            row = cursor.fetchone()
            return self._user_from_row(cursor, row) if row else None

    def list_users(self) -> list[UserRecord]:
        """全ユーザーとロールの割り当てを、ユーザー数によらず 2 回の SQL で読む（#793）。"""
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(self._user_select() + " ORDER BY LOGIN_USER_ID_NORMALIZED")
            rows = cursor.fetchall()
            if not rows:
                return []
            cursor.execute(
                "SELECT USER_UUID, ROLE_ID FROM PLATFORM_USER_ROLES ORDER BY USER_UUID, ROLE_ID"
            )
            role_ids: dict[str, list[str]] = {}
            for user_uuid, role_id in cursor.fetchall():
                role_ids.setdefault(str(user_uuid), []).append(str(role_id))
            return [self._user_record(row, role_ids.get(str(row[0]), [])) for row in rows]

    @staticmethod
    def _user_select() -> str:
        return (
            "SELECT USER_UUID, LOGIN_USER_ID, DISPLAY_NAME, PASSWORD_HASH, STATUS, "
            "FORCE_PASSWORD_CHANGE, FAILED_LOGIN_COUNT, LOCKED_UNTIL, VERSION_NO, "
            "CASE WHEN USER_UUID = ("
            "  SELECT MIN(USER_UUID) KEEP (DENSE_RANK FIRST ORDER BY CREATED_AT, USER_UUID) "
            "  FROM PLATFORM_USERS"
            ") THEN 1 ELSE 0 END AS IS_BOOTSTRAP_ADMIN "
            "FROM PLATFORM_USERS"
        )

    def get_user_identities(self, user_uuids: list[str]) -> dict[str, UserIdentity]:
        """履歴ページの実行者だけを一括解決し、認証情報・role は取得しない。"""
        ids = sorted(set(user_uuids) - {""})
        identities: dict[str, UserIdentity] = {}
        if not ids:
            return identities
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            for offset in range(0, len(ids), 500):
                binds = {
                    f"user_{index}": value for index, value in enumerate(ids[offset : offset + 500])
                }
                placeholders = ", ".join(f":{key}" for key in binds)
                # 展開するのは生成した bind 名だけ。UUID の値はすべて別引数で bind する。
                cursor.execute(
                    "SELECT USER_UUID, LOGIN_USER_ID, DISPLAY_NAME FROM PLATFORM_USERS "  # nosec B608
                    f"WHERE USER_UUID IN ({placeholders})",
                    binds,
                )
                for row in cursor.fetchall():
                    identity = UserIdentity(str(row[0]), str(row[1]), str(row[2] or ""))
                    identities[identity.user_uuid] = identity
        return identities

    def _user_from_row(self, cursor: Any, row: Any) -> UserRecord:
        user_uuid = str(row[0])
        cursor.execute(
            "SELECT ROLE_ID FROM PLATFORM_USER_ROLES WHERE USER_UUID = :user_uuid ORDER BY ROLE_ID",
            {"user_uuid": user_uuid},
        )
        return self._user_record(row, [str(item[0]) for item in cursor.fetchall()])

    @staticmethod
    def _user_record(row: Any, role_ids: list[str]) -> UserRecord:
        return UserRecord(
            user_uuid=str(row[0]),
            login_user_id=str(row[1]),
            display_name=str(row[2]),
            password_hash=str(row[3]),
            status=str(row[4]),
            force_password_change=bool(row[5]),
            failed_login_count=int(row[6] or 0),
            locked_until=row[7],
            version=int(row[8]),
            role_ids=role_ids,
            is_bootstrap_admin=bool(row[9]),
        )

    def create_user(self, user: UserRecord) -> UserRecord:
        try:
            with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
                self._assert_role_ids(cursor, user.role_ids)
                cursor.execute(
                    """
                    INSERT INTO PLATFORM_USERS
                      (USER_UUID, LOGIN_USER_ID, LOGIN_USER_ID_NORMALIZED,
                       DISPLAY_NAME, PASSWORD_HASH,
                       STATUS, FORCE_PASSWORD_CHANGE, FAILED_LOGIN_COUNT, LOCKED_UNTIL, VERSION_NO)
                    VALUES
                      (:user_uuid, :login_user_id, :normalized, :display_name, :password_hash,
                       :status, :force_change, 0, NULL, 1)
                    """,
                    {
                        "user_uuid": user.user_uuid,
                        "login_user_id": user.login_user_id,
                        "normalized": user.login_user_id.casefold(),
                        "display_name": user.display_name,
                        "password_hash": user.password_hash,
                        "status": user.status,
                        "force_change": int(user.force_password_change),
                    },
                )
                self._replace_user_roles(cursor, user.user_uuid, user.role_ids)
                conn.commit()
        except Exception as exc:
            if "ORA-00001" in str(exc):
                raise SecurityConflict(
                    _LOGIN_USER_ID_CONFLICT_MESSAGE,
                    code="SECURITY_USER_LOGIN_ID_CONFLICT",
                    pointer="/login_user_id",
                    field_code="already_exists",
                ) from exc
            raise
        return self.get_user(user.user_uuid) or user

    def update_user(
        self,
        user_uuid: str,
        *,
        expected_version: int,
        display_name: str,
        status: str,
        role_ids: list[str],
    ) -> UserRecord:
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            # 最後の管理者判定と更新を一つの DB critical section に置く。
            # 複数 API worker が同時に別の管理者を無効化しても 0 人にはならない。
            cursor.execute("LOCK TABLE PLATFORM_USERS IN SHARE ROW EXCLUSIVE MODE")
            cursor.execute("LOCK TABLE PLATFORM_USER_ROLES IN SHARE ROW EXCLUSIVE MODE")
            cursor.execute(
                "SELECT STATUS FROM PLATFORM_USERS WHERE USER_UUID = :user_uuid",
                {"user_uuid": user_uuid},
            )
            current_row = cursor.fetchone()
            if current_row is None:
                raise SecurityNotFound("ユーザーが見つかりません。")
            cursor.execute(
                "SELECT ROLE_ID FROM PLATFORM_USER_ROLES WHERE USER_UUID = :user_uuid",
                {"user_uuid": user_uuid},
            )
            current_role_ids = {str(item[0]) for item in cursor.fetchall()}
            self._assert_role_ids(cursor, role_ids, allow_inactive_role_ids=current_role_ids)
            is_admin = SYSTEM_ADMIN_ROLE_ID in current_role_ids
            removes_admin = is_admin and (
                status != "ACTIVE" or SYSTEM_ADMIN_ROLE_ID not in role_ids
            )
            if removes_admin:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                      FROM PLATFORM_USERS u
                      JOIN PLATFORM_USER_ROLES ur ON ur.USER_UUID = u.USER_UUID
                     WHERE u.STATUS = 'ACTIVE' AND ur.ROLE_ID = :role_id
                    """,
                    {"role_id": SYSTEM_ADMIN_ROLE_ID},
                )
                if int(cursor.fetchone()[0]) <= 1:
                    raise SecurityConflict("最後のシステム管理者は無効化または権限解除できません。")
            cursor.execute(
                """
                UPDATE PLATFORM_USERS
                   SET DISPLAY_NAME = :display_name, STATUS = :status,
                       VERSION_NO = VERSION_NO + 1, UPDATED_AT = SYSTIMESTAMP
                 WHERE USER_UUID = :user_uuid AND VERSION_NO = :expected_version
                """,
                {
                    "display_name": display_name,
                    "status": status,
                    "user_uuid": user_uuid,
                    "expected_version": expected_version,
                },
            )
            if cursor.rowcount == 0:
                self._raise_not_found_or_conflict(cursor, USERS_TABLE, user_uuid)
            self._replace_user_roles(cursor, user_uuid, role_ids)
            conn.commit()
        updated = self.get_user(user_uuid)
        if updated is None:
            raise SecurityNotFound("ユーザーが見つかりません。")
        return updated

    def delete_user(self, user_uuid: str, *, expected_version: int) -> None:
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT STATUS, VERSION_NO
                  FROM PLATFORM_USERS
                 WHERE USER_UUID = :user_uuid
                   FOR UPDATE
                """,
                {"user_uuid": user_uuid},
            )
            current = cursor.fetchone()
            if current is None:
                raise SecurityNotFound("ユーザーが見つかりません。")
            if int(current[1]) != expected_version:
                raise SecurityConflict("ユーザーが別の操作で更新されています。")
            if str(current[0]) != "DISABLED":
                raise SecurityConflict(
                    "ユーザーを先に無効化してから削除してください。",
                    code="SECURITY_USER_DELETE_REQUIRES_DISABLED",
                )
            cursor.execute("""
                SELECT MIN(USER_UUID) KEEP (DENSE_RANK FIRST ORDER BY CREATED_AT, USER_UUID)
                  FROM PLATFORM_USERS
                """)
            bootstrap_user_uuid = cursor.fetchone()[0]
            if bootstrap_user_uuid is not None and str(bootstrap_user_uuid) == user_uuid:
                raise SecurityConflict(
                    "初期システム管理者は削除できません。",
                    code="SECURITY_USER_DELETE_PROTECTED",
                )
            cursor.execute(
                "DELETE FROM PLATFORM_AUTH_SESSIONS WHERE USER_UUID = :user_uuid",
                {"user_uuid": user_uuid},
            )
            cursor.execute(
                "DELETE FROM PLATFORM_USER_ROLES WHERE USER_UUID = :user_uuid",
                {"user_uuid": user_uuid},
            )
            cursor.execute(
                """
                DELETE FROM PLATFORM_USERS
                 WHERE USER_UUID = :user_uuid
                   AND VERSION_NO = :expected_version
                   AND STATUS = 'DISABLED'
                """,
                {"user_uuid": user_uuid, "expected_version": expected_version},
            )
            if cursor.rowcount == 0:
                self._raise_not_found_or_conflict(cursor, USERS_TABLE, user_uuid)
            conn.commit()

    def set_password(self, user_uuid: str, password_hash: str, *, force_change: bool) -> None:
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE PLATFORM_USERS
                   SET PASSWORD_HASH = :password_hash, FORCE_PASSWORD_CHANGE = :force_change,
                       FAILED_LOGIN_COUNT = 0, LOCKED_UNTIL = NULL,
                       VERSION_NO = VERSION_NO + 1, UPDATED_AT = SYSTIMESTAMP
                 WHERE USER_UUID = :user_uuid
                """,
                {
                    "password_hash": password_hash,
                    "force_change": int(force_change),
                    "user_uuid": user_uuid,
                },
            )
            if cursor.rowcount == 0:
                raise SecurityNotFound("ユーザーが見つかりません。")
            conn.commit()

    def record_login_failure(
        self, user_uuid: str, *, failed_count: int, locked_until: datetime | None
    ) -> None:
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE PLATFORM_USERS SET FAILED_LOGIN_COUNT = :failed_count,
                    LOCKED_UNTIL = :locked_until, UPDATED_AT = SYSTIMESTAMP
                WHERE USER_UUID = :user_uuid
                """,
                {
                    "failed_count": failed_count,
                    "locked_until": locked_until,
                    "user_uuid": user_uuid,
                },
            )
            conn.commit()

    def record_login_success(self, user_uuid: str, *, password_hash: str | None = None) -> None:
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            if password_hash:
                cursor.execute(
                    """
                    UPDATE PLATFORM_USERS SET FAILED_LOGIN_COUNT = 0, LOCKED_UNTIL = NULL,
                        PASSWORD_HASH = :password_hash, UPDATED_AT = SYSTIMESTAMP
                    WHERE USER_UUID = :user_uuid
                    """,
                    {"password_hash": password_hash, "user_uuid": user_uuid},
                )
            else:
                cursor.execute(
                    """
                    UPDATE PLATFORM_USERS SET FAILED_LOGIN_COUNT = 0, LOCKED_UNTIL = NULL,
                        UPDATED_AT = SYSTIMESTAMP WHERE USER_UUID = :user_uuid
                    """,
                    {"user_uuid": user_uuid},
                )
            conn.commit()

    def list_roles(self, *, include_archived: bool = False) -> Sequence[RoleRecord]:
        with self.connection(ROLES_TABLE) as conn, conn.cursor() as cursor:
            sql = self._role_select()
            if not include_archived:
                sql += " WHERE ARCHIVED = 0"
            sql += " ORDER BY ROLE_CODE"
            cursor.execute(sql)
            roles = [self._role_base(row) for row in cursor.fetchall()]
            return self._roles_details(cursor, roles) if roles else []

    def get_roles(self, role_ids: Sequence[str]) -> Sequence[RoleRecord]:
        """指定したロールを指定の順に返す（無いものは除く）。ロール数によらず一括で読む（#793）。"""
        ids = list(dict.fromkeys(role_id for role_id in role_ids if role_id))
        if not ids:
            return []
        with self.connection(ROLES_TABLE) as conn, conn.cursor() as cursor:
            found: dict[str, RoleRecord] = {}
            for placeholders, binds in in_list_binds("role_", ids):
                cursor.execute(
                    self._role_select() + f" WHERE ROLE_ID IN ({placeholders})",  # nosec B608
                    binds,
                )
                for row in cursor.fetchall():
                    role = self._role_base(row)
                    found[role.role_id] = role
            ordered = [found[role_id] for role_id in ids if role_id in found]
            return self._roles_details(cursor, ordered) if ordered else []

    def get_role(self, role_id: str) -> RoleRecord | None:
        with self.connection(ROLES_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(self._role_select() + " WHERE ROLE_ID = :role_id", {"role_id": role_id})
            row = cursor.fetchone()
            return self._role_from_row(cursor, row) if row else None

    @staticmethod
    def _role_select() -> str:
        return (
            "SELECT ROLE_ID, ROLE_CODE, DISPLAY_NAME, DESCRIPTION, IS_BUILT_IN, "
            "ARCHIVED, VERSION_NO FROM PLATFORM_ROLES"
        )

    def _role_from_row(self, cursor: Any, row: Any) -> RoleRecord:
        return self._role_details(cursor, self._role_base(row))

    def _role_base(self, row: Any) -> RoleRecord:
        return self.role_class(
            role_id=str(row[0]),
            role_code=str(row[1]),
            display_name=str(row[2]),
            description="" if row[3] in (None, "-") else str(row[3]),
            is_built_in=bool(row[4]),
            archived=bool(row[5]),
            version=int(row[6]),
        )

    def _role_details(self, cursor: Any, role: RoleRecord) -> RoleRecord:
        """製品固有のロールのデータ（権限・対象範囲）を読み込む hook（1 件）。"""
        return role

    def _roles_details(self, cursor: Any, roles: Sequence[RoleRecord]) -> list[RoleRecord]:
        """複数のロールの製品固有のデータを読み込む hook（一覧・principal 用。#793）。

        既定は `_role_details` を 1 件ずつ呼ぶ（後方互換）。ロールの数だけ SQL が増えるため、製品は
        `ROLE_ID IN (...)`（`in_list_binds`）で一括に読むよう上書きする。順序は `roles` のまま返す。
        """
        return [self._role_details(cursor, role) for role in roles]

    def _replace_role_details(self, cursor: Any, role: RoleRecord) -> None:
        """製品固有のロールのデータを、ロール本体と同じトランザクションで書き込む hook。"""

    def _before_delete_role(self, cursor: Any, role_id: str) -> None:
        """製品固有の削除条件の確認と後始末（同じトランザクション）。"""

    def create_role(self, role: RoleRecord) -> RoleRecord:
        try:
            with self.connection(ROLES_TABLE) as conn, conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO PLATFORM_ROLES
                      (ROLE_ID, ROLE_CODE, DISPLAY_NAME, DESCRIPTION,
                       IS_BUILT_IN, ARCHIVED, VERSION_NO)
                    VALUES
                      (:role_id, :role_code, :display_name, :description,
                       :is_built_in, :archived, 1)
                    """,
                    {
                        "role_id": role.role_id,
                        "role_code": role.role_code,
                        "display_name": role.display_name,
                        "description": role.description or "-",
                        "is_built_in": int(role.is_built_in),
                        "archived": int(role.archived),
                    },
                )
                self._replace_role_details(cursor, role)
                conn.commit()
        except Exception as exc:
            if "ORA-00001" in str(exc):
                raise SecurityConflict(
                    _ROLE_CODE_CONFLICT_MESSAGE,
                    code="SECURITY_ROLE_CODE_CONFLICT",
                    pointer="/role_code",
                    field_code="already_exists",
                ) from exc
            raise
        return self.get_role(role.role_id) or role

    def update_role(self, role: RoleRecord, *, expected_version: int) -> RoleRecord:
        with self.connection(ROLES_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE PLATFORM_ROLES
                   SET DISPLAY_NAME = :display_name, DESCRIPTION = :description,
                       VERSION_NO = VERSION_NO + 1, UPDATED_AT = SYSTIMESTAMP
                 WHERE ROLE_ID = :role_id AND VERSION_NO = :expected_version
                """,
                {
                    "display_name": role.display_name,
                    "description": role.description or "-",
                    "role_id": role.role_id,
                    "expected_version": expected_version,
                },
            )
            if cursor.rowcount == 0:
                self._raise_not_found_or_conflict(cursor, ROLES_TABLE, role.role_id)
            self._replace_role_details(cursor, role)
            conn.commit()
        updated = self.get_role(role.role_id)
        if updated is None:
            raise SecurityNotFound("ロールが見つかりません。")
        return updated

    def archive_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        with self.connection(ROLES_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE PLATFORM_ROLES SET ARCHIVED = 1, VERSION_NO = VERSION_NO + 1,
                    UPDATED_AT = SYSTIMESTAMP
                WHERE ROLE_ID = :role_id AND VERSION_NO = :expected_version AND IS_BUILT_IN = 0
                """,
                {"role_id": role_id, "expected_version": expected_version},
            )
            if cursor.rowcount == 0:
                self._raise_not_found_or_conflict(cursor, ROLES_TABLE, role_id)
            conn.commit()
        role = self.get_role(role_id)
        if role is None:
            raise SecurityNotFound("ロールが見つかりません。")
        return role

    def restore_role(self, role_id: str, *, expected_version: int) -> RoleRecord:
        current = self.get_role(role_id)
        if current is None:
            raise SecurityNotFound("ロールが見つかりません。")
        if not current.archived:
            raise SecurityConflict("ロールはアーカイブされていません。")
        with self.connection(ROLES_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE PLATFORM_ROLES SET ARCHIVED = 0, VERSION_NO = VERSION_NO + 1,
                    UPDATED_AT = SYSTIMESTAMP
                WHERE ROLE_ID = :role_id AND VERSION_NO = :expected_version AND IS_BUILT_IN = 0
                """,
                {"role_id": role_id, "expected_version": expected_version},
            )
            if cursor.rowcount == 0:
                self._raise_not_found_or_conflict(cursor, ROLES_TABLE, role_id)
            conn.commit()
        role = self.get_role(role_id)
        if role is None:
            raise SecurityNotFound("ロールが見つかりません。")
        return role

    def delete_role(self, role_id: str, *, expected_version: int) -> None:
        with self.connection(ROLES_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT IS_BUILT_IN, ARCHIVED, VERSION_NO
                  FROM PLATFORM_ROLES
                 WHERE ROLE_ID = :role_id
                   FOR UPDATE
                """,
                {"role_id": role_id},
            )
            current = cursor.fetchone()
            if current is None:
                raise SecurityNotFound("ロールが見つかりません。")
            if int(current[2]) != expected_version:
                raise SecurityConflict("ロールが別の操作で更新されています。")
            if bool(current[0]):
                raise SecurityConflict(
                    "組み込み SYSTEM_ADMIN ロールは削除できません。",
                    code="SECURITY_ROLE_DELETE_PROTECTED",
                )
            if not bool(current[1]):
                raise SecurityConflict(
                    "ロールを先にアーカイブしてから削除してください。",
                    code="SECURITY_ROLE_DELETE_REQUIRES_ARCHIVED",
                )
            cursor.execute(
                "SELECT COUNT(*) FROM PLATFORM_USER_ROLES WHERE ROLE_ID = :role_id",
                {"role_id": role_id},
            )
            if int(cursor.fetchone()[0]) > 0:
                raise SecurityConflict(
                    "このロールはユーザーに割り当てられています。割り当てを解除してから削除してください。",
                    code="SECURITY_ROLE_DELETE_ASSIGNED",
                )
            self._before_delete_role(cursor, role_id)
            try:
                cursor.execute(
                    """
                    DELETE FROM PLATFORM_ROLES
                     WHERE ROLE_ID = :role_id
                       AND VERSION_NO = :expected_version
                       AND IS_BUILT_IN = 0
                       AND ARCHIVED = 1
                    """,
                    {"role_id": role_id, "expected_version": expected_version},
                )
            except Exception as exc:
                # 他製品の子テーブルが削除制限の FK で参照している（例: NL2SQL の Data Grant）。
                if "ORA-02292" in str(exc):
                    raise SecurityConflict(
                        _ROLE_REFERENCED_MESSAGE, code="SECURITY_ROLE_DELETE_REFERENCED"
                    ) from exc
                raise
            if cursor.rowcount == 0:
                self._raise_not_found_or_conflict(cursor, ROLES_TABLE, role_id)
            conn.commit()

    def count_active_system_admins(self) -> int:
        with self.connection(USERS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*)
                  FROM PLATFORM_USERS u
                  JOIN PLATFORM_USER_ROLES ur ON ur.USER_UUID = u.USER_UUID
                  JOIN PLATFORM_ROLES r ON r.ROLE_ID = ur.ROLE_ID
                 WHERE u.STATUS = 'ACTIVE' AND r.ROLE_CODE = 'SYSTEM_ADMIN' AND r.ARCHIVED = 0
                """)
            return int(cursor.fetchone()[0])

    def create_session(self, session: SessionRecord) -> None:
        with self.connection(SESSIONS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO PLATFORM_AUTH_SESSIONS
                  (SESSION_ID, USER_UUID, TOKEN_HASH, CSRF_TOKEN_HASH, IDLE_EXPIRES_AT,
                   ABSOLUTE_EXPIRES_AT, LAST_SEEN_AT)
                VALUES
                  (:session_id, :user_uuid, :token_hash, :csrf_hash, :idle_expires,
                   :absolute_expires, :last_seen)
                """,
                {
                    "session_id": session.session_id,
                    "user_uuid": session.user_uuid,
                    "token_hash": session.token_hash,
                    "csrf_hash": session.csrf_token_hash,
                    "idle_expires": session.idle_expires_at,
                    "absolute_expires": session.absolute_expires_at,
                    "last_seen": session.last_seen_at,
                },
            )
            conn.commit()

    def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        with self.connection(SESSIONS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT SESSION_ID, USER_UUID, TOKEN_HASH, CSRF_TOKEN_HASH, IDLE_EXPIRES_AT,
                       ABSOLUTE_EXPIRES_AT, LAST_SEEN_AT, REVOKED_AT
                  FROM PLATFORM_AUTH_SESSIONS WHERE TOKEN_HASH = :token_hash
                """,
                {"token_hash": token_hash},
            )
            row = cursor.fetchone()
            if not row:
                return None
            return SessionRecord(
                session_id=str(row[0]),
                user_uuid=str(row[1]),
                token_hash=str(row[2]),
                csrf_token_hash=str(row[3]),
                idle_expires_at=row[4],
                absolute_expires_at=row[5],
                last_seen_at=row[6],
                revoked_at=row[7],
            )

    def touch_session(
        self, session_id: str, *, last_seen_at: datetime, idle_expires_at: datetime
    ) -> None:
        with self.connection(SESSIONS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE PLATFORM_AUTH_SESSIONS SET LAST_SEEN_AT = :last_seen,
                    IDLE_EXPIRES_AT = :idle_expires
                WHERE SESSION_ID = :session_id AND REVOKED_AT IS NULL
                """,
                {
                    "last_seen": last_seen_at,
                    "idle_expires": idle_expires_at,
                    "session_id": session_id,
                },
            )
            conn.commit()

    def revoke_session(self, session_id: str) -> None:
        with self.connection(SESSIONS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE PLATFORM_AUTH_SESSIONS SET REVOKED_AT = SYSTIMESTAMP "
                "WHERE SESSION_ID = :session_id AND REVOKED_AT IS NULL",
                {"session_id": session_id},
            )
            conn.commit()

    def revoke_user_sessions(self, user_uuid: str) -> None:
        with self.connection(SESSIONS_TABLE) as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE PLATFORM_AUTH_SESSIONS SET REVOKED_AT = SYSTIMESTAMP "
                "WHERE USER_UUID = :user_uuid AND REVOKED_AT IS NULL",
                {"user_uuid": user_uuid},
            )
            conn.commit()

    def role_permission_codes(
        self, role_ids: Sequence[str], *, tables: Sequence[str]
    ) -> RolePermissionCodes:
        """製品ごとの権限テーブルから、指定ロールの権限コードを読む。未配備のテーブルは空。"""
        result: RolePermissionCodes = {table: {} for table in tables}
        ids = sorted(set(role_ids) - {""})
        if not ids:
            return result
        allowed_tables = set(PRODUCT_ROLE_PERMISSION_TABLES.values())
        with self.connection(ROLES_TABLE) as conn, conn.cursor() as cursor:
            for table in tables:
                if table not in allowed_tables:
                    raise SecurityStoreError("未登録の権限テーブルです。")
                for placeholders, binds in in_list_binds("role_", ids):
                    try:
                        # テーブル名は登録済みの定数だけ。値はすべて bind する。
                        cursor.execute(
                            f"SELECT ROLE_ID, PERMISSION_CODE FROM {table} "  # nosec B608
                            f"WHERE ROLE_ID IN ({placeholders})",
                            binds,
                        )
                    except Exception as exc:
                        if "ORA-00942" in str(exc).upper():
                            break
                        raise
                    for role_id, code in cursor.fetchall():
                        result[table].setdefault(str(role_id), set()).add(str(code))
        return result

    @staticmethod
    def _replace_user_roles(cursor: Any, user_uuid: str, role_ids: Iterable[str]) -> None:
        cursor.execute(
            "DELETE FROM PLATFORM_USER_ROLES WHERE USER_UUID = :user_uuid",
            {"user_uuid": user_uuid},
        )
        for role_id in dict.fromkeys(role_ids):
            cursor.execute(
                "INSERT INTO PLATFORM_USER_ROLES (USER_UUID, ROLE_ID) "
                "VALUES (:user_uuid, :role_id)",
                {"user_uuid": user_uuid, "role_id": role_id},
            )

    @staticmethod
    def _assert_role_ids(
        cursor: Any,
        role_ids: list[str],
        *,
        allow_inactive_role_ids: set[str] | None = None,
    ) -> None:
        allowed = allow_inactive_role_ids or set()
        for role_id in dict.fromkeys(role_ids):
            cursor.execute(
                "SELECT COUNT(*) FROM PLATFORM_ROLES WHERE ROLE_ID = :role_id AND ARCHIVED = 0",
                {"role_id": role_id},
            )
            if int(cursor.fetchone()[0]) == 1:
                continue
            if role_id in allowed:
                continue
            raise SecurityNotFound("指定された有効なロールが見つかりません。")

    @staticmethod
    def _raise_not_found_or_conflict(cursor: Any, table: str, value: str) -> None:
        queries = {
            USERS_TABLE: "SELECT COUNT(*) FROM PLATFORM_USERS WHERE USER_UUID = :value",
            ROLES_TABLE: "SELECT COUNT(*) FROM PLATFORM_ROLES WHERE ROLE_ID = :value",
        }
        sql = queries.get(table)
        if sql is None:
            raise SecurityStoreError("安全でない競合確認です。")
        cursor.execute(sql, {"value": value})
        if int(cursor.fetchone()[0]) == 0:
            raise SecurityNotFound("対象が見つかりません。")
        raise SecurityConflict("別の操作で更新されています。最新情報を再読込してください。")
