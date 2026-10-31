"""言語資源の下書きと不変の公開版を Oracle transaction で保存する。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pr_backend_core.oracle_errors import oracle_error_codes
from rag_parser_core.language import (
    LanguagePack,
    build_pack,
    complete_compatible_pack,
    stored_pack_hash,
)

from app.schemas.language_pack_release import (
    LanguagePackRelease,
    LanguagePackReleaseList,
    LanguagePackReleaseState,
)

if TYPE_CHECKING:
    from app.clients.oracle import OracleClient, OracleConnectionProtocol

STATE_TABLE = "rag_language_pack_state"
RELEASE_TABLE = "rag_language_pack_releases"


class LanguagePackReleaseConflict(ValueError):
    """読み込み後に下書きまたは公開版が変更された。"""


def language_pack_release_statements() -> str:
    """公開の監査履歴を残し、下書きと公開ポインタを別に持つ。"""
    return f"""
CREATE TABLE {RELEASE_TABLE} (
    release_id VARCHAR2(64) PRIMARY KEY,
    version NUMBER(19) NOT NULL UNIQUE,
    effective_json JSON NOT NULL,
    content_hash VARCHAR2(64) NOT NULL,
    ingest_hash VARCHAR2(64) NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE NOT NULL,
    published_by VARCHAR2(256),
    previous_id VARCHAR2(64),
    restored_from VARCHAR2(64),
    change_note VARCHAR2(2000),
    CONSTRAINT rag_lang_release_version_ck CHECK (version >= 1),
    CONSTRAINT rag_lang_release_previous_fk FOREIGN KEY (previous_id)
        REFERENCES {RELEASE_TABLE} (release_id),
    CONSTRAINT rag_lang_release_restore_fk FOREIGN KEY (restored_from)
        REFERENCES {RELEASE_TABLE} (release_id)
);
CREATE TABLE {STATE_TABLE} (
    singleton_id NUMBER(1) PRIMARY KEY,
    draft_revision NUMBER(19) NOT NULL,
    draft_effective_json JSON NOT NULL,
    current_release_id VARCHAR2(64),
    CONSTRAINT rag_lang_state_singleton_ck CHECK (singleton_id = 1),
    CONSTRAINT rag_lang_state_revision_ck CHECK (draft_revision >= 1),
    CONSTRAINT rag_lang_state_current_fk FOREIGN KEY (current_release_id)
        REFERENCES {RELEASE_TABLE} (release_id)
);
CREATE INDEX rag_lang_release_previous_idx ON {RELEASE_TABLE} (previous_id);
CREATE INDEX rag_lang_release_restore_idx ON {RELEASE_TABLE} (restored_from);
CREATE INDEX rag_lang_state_current_idx ON {STATE_TABLE} (current_release_id);
""".strip()


def _validated(effective: dict[str, Any], *, allow_legacy: bool = False) -> LanguagePack:
    pack = build_pack(effective)
    expected = complete_compatible_pack(effective) if allow_legacy else effective
    if pack.to_dict() != expected:
        raise ValueError("言語資源は全項目を含む完全な設定で保存してください。")
    return pack


def _release(row: dict[str, object]) -> LanguagePackRelease:
    from app.clients.oracle import _json_loads

    release = LanguagePackRelease.model_validate(
        {
            "id": row["release_id"],
            "version": row["version"],
            "effective": _json_loads(row["effective_json"]),
            "hash": row["content_hash"],
            "ingest_hash": row["ingest_hash"],
            "published_at": row["published_at"],
            "published_by": row.get("published_by"),
            "previous_id": row.get("previous_id"),
            "restored_from": row.get("restored_from"),
            "change_note": row.get("change_note") or "",
        }
    )
    pack = _validated(release.effective, allow_legacy=True)
    if (stored_pack_hash(release.effective, pack), pack.ingest_hash) != (
        release.hash,
        release.ingest_hash,
    ):
        raise ValueError("言語資源の保存内容と公開版の照合に失敗しました。")
    return release


def _state(connection: OracleConnectionProtocol, *, lock: bool = False) -> LanguagePackReleaseState:
    from app.clients.oracle import _fetch_one, _json_loads

    row = _fetch_one(
        connection,
        f"SELECT * FROM {STATE_TABLE} WHERE singleton_id=1" + (" FOR UPDATE" if lock else ""),
        {},
    )
    if row is None or not row.get("current_release_id"):
        raise LookupError("言語資源の公開版がまだありません。")
    current = _get(connection, str(row["current_release_id"]))
    if current is None:
        raise LookupError("言語資源の現在の公開版が見つかりません。")
    draft = _json_loads(row["draft_effective_json"])
    _validated(draft, allow_legacy=True)
    return LanguagePackReleaseState(
        draft_revision=int(str(row["draft_revision"])), draft_effective=draft, current=current
    )


def _get(connection: OracleConnectionProtocol, release_id: str) -> LanguagePackRelease | None:
    from app.clients.oracle import _fetch_one

    row = _fetch_one(
        connection, f"SELECT * FROM {RELEASE_TABLE} WHERE release_id=:id", {"id": release_id}
    )
    return _release(row) if row else None


def _insert(connection: OracleConnectionProtocol, release: LanguagePackRelease) -> None:
    from app.clients.oracle import _execute, _json_bind, _json_input_sizes

    _execute(
        connection,
        f"""INSERT INTO {RELEASE_TABLE}
        (release_id,version,effective_json,content_hash,ingest_hash,published_at,published_by,
         previous_id,restored_from,change_note)
        VALUES (:id,:version,:effective,:hash,:ingest_hash,:published_at,:published_by,
                :previous_id,:restored_from,:change_note)""",
        {**release.model_dump(exclude={"effective"}), "effective": _json_bind(release.effective)},
        input_sizes=_json_input_sizes("effective"),
    )


class LanguagePackReleaseStore:
    """公開版の変更は常に新しい版の追加と現在ポインタの変更で行う。"""

    def __init__(self, oracle: OracleClient) -> None:
        self._oracle = oracle

    async def ensure_initialized(
        self, initial_effective: dict[str, Any], *, published_by: str | None = None
    ) -> LanguagePackReleaseState:
        pack = _validated(initial_effective)

        def operation(connection: OracleConnectionProtocol) -> LanguagePackReleaseState:
            from app.clients.oracle import _execute, _fetch_one, _json_bind, _json_input_sizes

            # 一意な行の作成とロックを同一 transaction にし、同時 bootstrap も一度だけにする。
            _execute(
                connection,
                f"""MERGE INTO {STATE_TABLE} target USING (SELECT 1 id FROM dual) source
                ON (target.singleton_id=source.id)
                WHEN NOT MATCHED THEN INSERT (singleton_id,draft_revision,draft_effective_json)
                VALUES (1,1,:effective)""",
                {"effective": _json_bind(initial_effective)},
                input_sizes=_json_input_sizes("effective"),
            )
            row = _fetch_one(
                connection,
                f"SELECT current_release_id FROM {STATE_TABLE} WHERE singleton_id=1 FOR UPDATE",
                {},
            )
            if row and row.get("current_release_id"):
                return _state(connection)
            release = LanguagePackRelease(
                id=uuid4().hex,
                version=1,
                effective=pack.to_dict(),
                hash=pack.hash,
                ingest_hash=pack.ingest_hash,
                published_at=datetime.now(UTC),
                published_by=published_by,
            )
            _insert(connection, release)
            _execute(
                connection,
                f"UPDATE {STATE_TABLE} SET current_release_id=:id WHERE singleton_id=1",
                {"id": release.id},
            )
            return _state(connection)

        try:
            return await self._oracle._run_transaction(operation)
        except Exception as exc:
            # 同時 MERGE が未確定の行を見た場合、敗者の transaction 全体を rollback 後に読む。
            if "ORA-00001" not in oracle_error_codes(exc):
                raise
            return await self.state()

    async def state(self) -> LanguagePackReleaseState:
        return await self._oracle._run_transaction(_state)

    async def current(self) -> LanguagePackRelease:
        return (await self.state()).current

    async def get(self, release_id: str) -> LanguagePackRelease | None:
        return await self._oracle._run_transaction(lambda connection: _get(connection, release_id))

    async def list(self, *, limit: int = 25, offset: int = 0) -> LanguagePackReleaseList:
        if limit < 1 or limit > 100 or offset < 0:
            raise ValueError("公開版の表示件数または開始位置が範囲外です。")

        def operation(connection: OracleConnectionProtocol) -> LanguagePackReleaseList:
            from app.clients.oracle import _fetch_all, _fetch_one

            row = _fetch_one(connection, f"SELECT COUNT(*) total FROM {RELEASE_TABLE}", {})
            rows = _fetch_all(
                connection,
                f"SELECT * FROM {RELEASE_TABLE} ORDER BY version DESC "
                "OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY",
                {"offset": offset, "limit": limit},
            )
            return LanguagePackReleaseList(
                releases=[_release(item) for item in rows],
                total=int(str(row["total"])) if row else 0,
            )

        return await self._oracle._run_transaction(operation)

    async def save_draft(
        self, effective: dict[str, Any], *, expected_revision: int
    ) -> LanguagePackReleaseState:
        pack = _validated(effective)

        def operation(connection: OracleConnectionProtocol) -> LanguagePackReleaseState:
            from app.clients.oracle import _execute, _json_bind, _json_input_sizes

            state = _state(connection, lock=True)
            self._check(state, expected_revision)
            _execute(
                connection,
                f"UPDATE {STATE_TABLE} SET draft_effective_json=:effective, "
                "draft_revision=draft_revision+1 WHERE singleton_id=1",
                {"effective": _json_bind(pack.to_dict())},
                input_sizes=_json_input_sizes("effective"),
            )
            return _state(connection)

        return await self._oracle._run_transaction(operation)

    @staticmethod
    def _check(
        state: LanguagePackReleaseState,
        expected_revision: int,
        expected_current_id: str | None = None,
    ) -> None:
        if state.draft_revision != expected_revision or (
            expected_current_id is not None and state.current.id != expected_current_id
        ):
            raise LanguagePackReleaseConflict(
                "言語資源が変更されています。最新の内容を読み直してください。"
            )

    async def publish(
        self,
        *,
        expected_revision: int,
        expected_current_id: str,
        change_note: str = "",
        published_by: str | None = None,
    ) -> LanguagePackReleaseState:
        return await self._publish(
            expected_revision=expected_revision,
            expected_current_id=expected_current_id,
            change_note=change_note,
            published_by=published_by,
        )

    async def restore(
        self,
        release_id: str,
        *,
        expected_revision: int,
        expected_current_id: str,
        change_note: str = "",
        published_by: str | None = None,
    ) -> LanguagePackReleaseState:
        return await self._publish(
            expected_revision=expected_revision,
            expected_current_id=expected_current_id,
            change_note=change_note,
            published_by=published_by,
            restored_from=release_id,
        )

    async def _publish(
        self,
        *,
        expected_revision: int,
        expected_current_id: str,
        change_note: str,
        published_by: str | None,
        restored_from: str | None = None,
    ) -> LanguagePackReleaseState:
        def operation(connection: OracleConnectionProtocol) -> LanguagePackReleaseState:
            from app.clients.oracle import _execute, _json_bind, _json_input_sizes

            state = _state(connection, lock=True)
            self._check(state, expected_revision, expected_current_id)
            source = _get(connection, restored_from) if restored_from else None
            if restored_from and source is None:
                raise LookupError("復元する言語資源の公開版が見つかりません。")
            effective = complete_compatible_pack(
                source.effective if source else state.draft_effective
            )
            pack = _validated(effective)
            release = LanguagePackRelease(
                id=uuid4().hex,
                version=state.current.version + 1,
                effective=effective,
                hash=pack.hash,
                ingest_hash=pack.ingest_hash,
                published_at=datetime.now(UTC),
                published_by=published_by,
                previous_id=state.current.id,
                restored_from=restored_from,
                change_note=change_note,
            )
            _insert(connection, release)
            _execute(
                connection,
                f"UPDATE {STATE_TABLE} SET current_release_id=:id, "
                "draft_revision=draft_revision+1, draft_effective_json=:effective "
                "WHERE singleton_id=1",
                {"id": release.id, "effective": _json_bind(effective)},
                input_sizes=_json_input_sizes("effective"),
            )
            return _state(connection)

        return await self._oracle._run_transaction(operation)
