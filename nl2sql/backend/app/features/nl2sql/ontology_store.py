"""Versioned Ontology persistence boundary for NL2SQL.

The production implementation persists the shared business Ontology, profile views,
and query traces in Oracle AI Database.  Local development and CI use the in-memory
implementation with the same optimistic-concurrency contract.

DDL is deliberately migration-only: constructing or checking
:class:`OracleOntologyStore` never changes the database. ``ensure_schema()`` performs
only a bounded existence query.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from array import array
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from importlib import import_module
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from app.settings import get_settings

from .oracle_lob import configure_clob_fetch_as_text

OntologyCollection = Literal[
    "revisions",
    "nodes",
    "edges",
    "profile_views",
    "query_sessions",
    "artifacts",
    "proposals",
    "idempotency",
    "source_documents",
    "jobs",
    "recommendations",
]

ONTOLOGY_COLLECTIONS: tuple[OntologyCollection, ...] = (
    "revisions",
    "nodes",
    "edges",
    "profile_views",
    "query_sessions",
    "artifacts",
    "proposals",
    "idempotency",
    "source_documents",
    "jobs",
    "recommendations",
)
IDEMPOTENCY_KEY_STORAGE_MAX_BYTES = 160
_ORACLE_ROW_LOCK_WAIT_MAX_SECONDS = 30
_ORACLE_INSERT_BATCH_SIZE = 256
_ORACLE_STRING_BIND_MAX_BYTES = 32767

_ID_KIND = re.compile(r"[^a-z0-9_]+")
_UNORDERED_SCHEMA_COLLECTIONS = frozenset(
    {
        "schemas",
        "tables",
        "views",
        "columns",
        "constraints",
        "foreign_keys",
        "unique_constraints",
        "indexes",
        "nodes",
        "edges",
    }
)


class OntologyVersionConflict(RuntimeError):
    """Raised when a mutation does not match the currently persisted ETag."""

    def __init__(
        self,
        message: str,
        *,
        current_etag: str | None = None,
        current_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.current_etag = current_etag
        self.current_version = current_version


def _json_compatible(value: Any) -> Any:
    """Convert supported domain values into a deterministic JSON-compatible tree."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_compatible(model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _json_compatible(asdict(value))
    if isinstance(value, Enum):
        return _json_compatible(value.value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        normalized = [_json_compatible(item) for item in value]
        return sorted(normalized, key=canonical_json)
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_json_compatible(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"Ontology JSON serialization does not support {type(value).__name__}.")


def canonical_json(value: Any) -> str:
    """Serialize a value with stable key order and without ASCII escaping."""

    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def deserialize_json(value: str | bytes | Mapping[str, Any] | Any) -> dict[str, Any]:
    """Decode Oracle JSON/CLOB values into a detached dictionary."""

    if isinstance(value, Mapping):
        decoded = json.loads(canonical_json(value))
    else:
        read = getattr(value, "read", None)
        raw = read() if callable(read) else value
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        decoded = json.loads(str(raw))
    if not isinstance(decoded, dict):
        raise ValueError("永続化されたオントロジー payload が JSON オブジェクトではありません。")
    return cast(dict[str, Any], decoded)


def stable_ontology_id(kind: str, *identity_parts: Any, length: int = 24) -> str:
    """Build a deterministic identifier from canonical identity components."""

    normalized_kind = _ID_KIND.sub("_", kind.strip().lower()).strip("_") or "ontology"
    if length < 16 or length > 64:
        raise ValueError("Stable ID digest length must be between 16 and 64.")
    digest = hashlib.sha256(canonical_json(identity_parts).encode("utf-8")).hexdigest()
    return f"{normalized_kind}_{digest[:length]}"


def _normalize_oracle_identifier(identifier: str) -> str:
    value = unicodedata.normalize("NFC", identifier.strip())
    if len(value) >= 2 and value[0] == value[-1] == '"':
        # `physical_identity_part` の引用表現（#563）。大文字化しても同じ名前は、引用符を外して
        # 既存 ID と同じ値にする。小文字を含む名前だけ引用符付きのまま hash に入れる。
        inner = value[1:-1]
        return inner if inner == inner.upper() else value
    return value.upper()


def physical_identity_part(name: str) -> str:
    """カタログ上の owner / object / column 名を `stable_physical_id` に渡す値にする（#563）。

    `stable_physical_id` は引用なしの値を大文字化して ID を作るため、`Mixed_Case` をそのまま渡すと
    大文字の同名表 `MIXED_CASE` と同じ ID になる。大文字化しても変わらない名前（`ORDERS`、`売上`、
    `MY TABLE`）は既存 ID を変えないためそのまま返し、小文字を含む名前だけ `"Mixed_Case"` と
    二重引用符で囲んで別の ID にする。Oracle の識別子は `"` を含まないため表現は衝突しない。
    """

    value = unicodedata.normalize("NFC", str(name or "").strip())
    return value if value == value.upper() else f'"{value}"'


def stable_physical_id(
    object_kind: str,
    owner: str,
    object_name: str,
    *subordinate_names: str,
) -> str:
    """Build a stable ID for an Oracle schema object or one of its members."""

    parts = (
        object_kind.strip().lower(),
        _normalize_oracle_identifier(owner),
        _normalize_oracle_identifier(object_name),
        *(_normalize_oracle_identifier(name) for name in subordinate_names),
    )
    return stable_ontology_id("physical", *parts)


def _normalize_schema_tree(value: Any, *, parent_key: str = "") -> Any:
    normalized = _json_compatible(value)
    if isinstance(normalized, dict):
        return {
            key: _normalize_schema_tree(item, parent_key=key)
            for key, item in sorted(normalized.items())
        }
    if isinstance(normalized, list):
        items = [_normalize_schema_tree(item, parent_key=parent_key) for item in normalized]
        if parent_key in _UNORDERED_SCHEMA_COLLECTIONS:
            return sorted(items, key=canonical_json)
        return items
    if isinstance(normalized, str):
        return unicodedata.normalize("NFC", normalized)
    return normalized


def schema_fingerprint(schema: Any) -> str:
    """Hash schema metadata while ignoring catalog row ordering.

    Collections such as tables, columns, constraints, nodes, and edges are sorted.
    Ordered members of composite keys (for example ``source_columns`` and
    ``target_columns``) retain their sequence, so a changed join mapping changes the
    fingerprint.
    """

    normalized = _normalize_schema_tree(schema)
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def compute_etag(document: Mapping[str, Any], version: int) -> str:
    """Compute the strong ETag for a versioned document."""

    payload = dict(document)
    payload.pop("etag", None)
    payload["version"] = version
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _oracle_row_lock_wait_seconds() -> int:
    """Bound Oracle row-lock waits independently from driver-level call_timeout."""

    return max(
        1,
        min(
            _ORACLE_ROW_LOCK_WAIT_MAX_SECONDS,
            int(float(get_settings().nl2sql_oracle_call_timeout_seconds)),
        ),
    )


def _is_oracle_row_lock_timeout(exc: Exception) -> bool:
    return "ORA-30006" in str(exc).upper()


def next_versioned_document(
    document: Mapping[str, Any] | Any,
    *,
    current: Mapping[str, Any] | None,
    expected_etag: str | None,
) -> dict[str, Any]:
    """Prepare an immutable copy for create/update with optimistic concurrency."""

    prepared = deserialize_json(canonical_json(document))
    prepared.pop("etag", None)
    prepared.pop("version", None)
    if current is None:
        if expected_etag is not None:
            raise OntologyVersionConflict("The Ontology document does not exist.")
        version = 1
    else:
        current_etag = str(current.get("etag") or "")
        current_version = int(current.get("version") or 0)
        if expected_etag is None:
            raise OntologyVersionConflict(
                "If-Match ETag is required when updating an Ontology document.",
                current_etag=current_etag,
                current_version=current_version,
            )
        if expected_etag != current_etag:
            raise OntologyVersionConflict(
                "The Ontology document was changed by another request.",
                current_etag=current_etag,
                current_version=current_version,
            )
        version = current_version + 1
    prepared["version"] = version
    prepared["etag"] = compute_etag(prepared, version)
    return prepared


@dataclass(frozen=True)
class _CollectionSpec:
    table_name: str
    key_fields: tuple[str, ...]
    scalar_columns: Mapping[str, str]
    has_embedding: bool = False

    @property
    def allowed_filter_fields(self) -> frozenset[str]:
        return frozenset((*self.key_fields, *self.scalar_columns.keys()))


_SPECS: dict[OntologyCollection, _CollectionSpec] = {
    "revisions": _CollectionSpec(
        "NL2SQL_ONTOLOGY_REVISIONS",
        ("revision_id",),
        {
            "status": "STATUS",
            "schema_fingerprint": "SCHEMA_FINGERPRINT",
            "profile_id": "PROFILE_ID",
        },
    ),
    "nodes": _CollectionSpec(
        "NL2SQL_ONTOLOGY_NODES",
        ("revision_id", "node_id"),
        {
            "node_type": "NODE_TYPE",
            "review_status": "REVIEW_STATUS",
            "physical_id": "PHYSICAL_ID",
        },
        has_embedding=True,
    ),
    "edges": _CollectionSpec(
        "NL2SQL_ONTOLOGY_EDGES",
        ("revision_id", "edge_id"),
        {
            "source_node_id": "SOURCE_NODE_ID",
            "target_node_id": "TARGET_NODE_ID",
            "review_status": "REVIEW_STATUS",
        },
    ),
    "profile_views": _CollectionSpec(
        "NL2SQL_ONTOLOGY_PROFILE_VIEW_REVISIONS",
        ("profile_id", "revision_id"),
        {},
    ),
    "query_sessions": _CollectionSpec(
        "NL2SQL_ONTOLOGY_QUERY_SESSIONS",
        ("session_id",),
        {
            "ontology_revision_id": "ONTOLOGY_REVISION_ID",
            "profile_id": "PROFILE_ID",
            "status": "STATUS",
            "intent_version": "INTENT_VERSION",
            "sql_version": "SQL_VERSION",
        },
    ),
    "artifacts": _CollectionSpec(
        "NL2SQL_ONTOLOGY_ARTIFACTS",
        ("artifact_id",),
        {
            "session_id": "SESSION_ID",
            "artifact_type": "ARTIFACT_TYPE",
            "content_hash": "CONTENT_HASH",
        },
    ),
    "proposals": _CollectionSpec(
        "NL2SQL_ONTOLOGY_PROPOSALS",
        ("proposal_id",),
        {
            "session_id": "SESSION_ID",
            "ontology_revision_id": "ONTOLOGY_REVISION_ID",
            "profile_id": "PROFILE_ID",
            "status": "STATUS",
        },
    ),
    "idempotency": _CollectionSpec(
        "NL2SQL_ONTOLOGY_IDEMPOTENCY",
        ("operation", "idempotency_key"),
        {
            "request_hash": "REQUEST_HASH",
            "resource_id": "RESOURCE_ID",
            "status": "STATUS",
        },
    ),
    "source_documents": _CollectionSpec(
        "NL2SQL_ONTOLOGY_SOURCE_DOCS",
        ("source_document_id",),
        {
            "profile_id": "PROFILE_ID",
            "status": "STATUS",
            "sha256": "SHA256",
        },
    ),
    "jobs": _CollectionSpec(
        "NL2SQL_ONTOLOGY_JOBS",
        ("job_id",),
        {
            "job_type": "JOB_TYPE",
            "profile_id": "PROFILE_ID",
            "status": "STATUS",
        },
    ),
    "recommendations": _CollectionSpec(
        "NL2SQL_ONTOLOGY_RECOMMENDATIONS",
        ("recommendation_id",),
        {
            "question_hash": "QUESTION_HASH",
            "status": "STATUS",
        },
    ),
}


ONTOLOGY_TABLE_DDL: dict[OntologyCollection, str] = {
    "revisions": """
        CREATE TABLE NL2SQL_ONTOLOGY_REVISIONS (
            REVISION_ID VARCHAR2(128) PRIMARY KEY,
            PROFILE_ID VARCHAR2(128),
            STATUS VARCHAR2(32) NOT NULL,
            SCHEMA_FINGERPRINT VARCHAR2(64) NOT NULL,
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "nodes": """
        CREATE TABLE NL2SQL_ONTOLOGY_NODES (
            REVISION_ID VARCHAR2(128) NOT NULL,
            NODE_ID VARCHAR2(128) NOT NULL,
            NODE_TYPE VARCHAR2(48) NOT NULL,
            REVIEW_STATUS VARCHAR2(32) NOT NULL,
            PHYSICAL_ID VARCHAR2(128),
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            EMBEDDING VECTOR(1536, FLOAT32),
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            CONSTRAINT PK_NL2SQL_ONT_NODES PRIMARY KEY (REVISION_ID, NODE_ID)
        )
    """,
    "edges": """
        CREATE TABLE NL2SQL_ONTOLOGY_EDGES (
            REVISION_ID VARCHAR2(128) NOT NULL,
            EDGE_ID VARCHAR2(128) NOT NULL,
            SOURCE_NODE_ID VARCHAR2(128) NOT NULL,
            TARGET_NODE_ID VARCHAR2(128) NOT NULL,
            REVIEW_STATUS VARCHAR2(32) NOT NULL,
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            CONSTRAINT PK_NL2SQL_ONT_EDGES PRIMARY KEY (REVISION_ID, EDGE_ID)
        )
    """,
    "profile_views": """
        CREATE TABLE NL2SQL_ONTOLOGY_PROFILE_VIEWS (
            PROFILE_ID VARCHAR2(128) PRIMARY KEY,
            REVISION_ID VARCHAR2(128) NOT NULL,
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "query_sessions": """
        CREATE TABLE NL2SQL_ONTOLOGY_QUERY_SESSIONS (
            SESSION_ID VARCHAR2(128) PRIMARY KEY,
            ONTOLOGY_REVISION_ID VARCHAR2(128) NOT NULL,
            PROFILE_ID VARCHAR2(128) NOT NULL,
            STATUS VARCHAR2(48) NOT NULL,
            INTENT_VERSION NUMBER(19) DEFAULT 0 NOT NULL,
            SQL_VERSION NUMBER(19) DEFAULT 0 NOT NULL,
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "artifacts": """
        CREATE TABLE NL2SQL_ONTOLOGY_ARTIFACTS (
            ARTIFACT_ID VARCHAR2(128) PRIMARY KEY,
            SESSION_ID VARCHAR2(128) NOT NULL,
            ARTIFACT_TYPE VARCHAR2(48) NOT NULL,
            CONTENT_HASH VARCHAR2(64) NOT NULL,
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "proposals": """
        CREATE TABLE NL2SQL_ONTOLOGY_PROPOSALS (
            PROPOSAL_ID VARCHAR2(128) PRIMARY KEY,
            SESSION_ID VARCHAR2(128) NOT NULL,
            ONTOLOGY_REVISION_ID VARCHAR2(128) NOT NULL,
            PROFILE_ID VARCHAR2(128) DEFAULT '' NOT NULL,
            STATUS VARCHAR2(32) NOT NULL,
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "idempotency": f"""
        CREATE TABLE NL2SQL_ONTOLOGY_IDEMPOTENCY (
            OPERATION VARCHAR2(96) NOT NULL,
            IDEMPOTENCY_KEY VARCHAR2({IDEMPOTENCY_KEY_STORAGE_MAX_BYTES}) NOT NULL,
            REQUEST_HASH VARCHAR2(64) NOT NULL,
            RESOURCE_ID VARCHAR2(160) NOT NULL,
            STATUS VARCHAR2(32) NOT NULL,
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            CONSTRAINT PK_NL2SQL_ONT_IDEMPOTENCY PRIMARY KEY (OPERATION, IDEMPOTENCY_KEY)
        )
    """,
    "source_documents": """
        CREATE TABLE NL2SQL_ONTOLOGY_SOURCE_DOCS (
            SOURCE_DOCUMENT_ID VARCHAR2(128) PRIMARY KEY,
            PROFILE_ID VARCHAR2(128) NOT NULL,
            STATUS VARCHAR2(32) NOT NULL,
            SHA256 VARCHAR2(64) NOT NULL,
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "jobs": """
        CREATE TABLE NL2SQL_ONTOLOGY_JOBS (
            JOB_ID VARCHAR2(128) PRIMARY KEY,
            JOB_TYPE VARCHAR2(32) NOT NULL,
            PROFILE_ID VARCHAR2(128) NOT NULL,
            STATUS VARCHAR2(32) NOT NULL,
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
    "recommendations": """
        CREATE TABLE NL2SQL_ONTOLOGY_RECOMMENDATIONS (
            RECOMMENDATION_ID VARCHAR2(128) PRIMARY KEY,
            QUESTION_HASH VARCHAR2(64) NOT NULL,
            STATUS VARCHAR2(32) NOT NULL,
            VERSION_NO NUMBER(19) NOT NULL,
            ETAG VARCHAR2(64) NOT NULL,
            PAYLOAD_JSON CLOB CHECK (PAYLOAD_JSON IS JSON) NOT NULL,
            CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
            UPDATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
        )
    """,
}

ONTOLOGY_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IX_NL2SQL_ONT_NODE_PHYSICAL ON NL2SQL_ONTOLOGY_NODES (PHYSICAL_ID)",
    "CREATE VECTOR INDEX IX_NL2SQL_ONT_NODE_EMBED ON NL2SQL_ONTOLOGY_NODES (EMBEDDING) "
    "ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE",
    "CREATE INDEX IX_NL2SQL_ONT_EDGE_SOURCE ON NL2SQL_ONTOLOGY_EDGES (REVISION_ID, SOURCE_NODE_ID)",
    "CREATE INDEX IX_NL2SQL_ONT_EDGE_TARGET ON NL2SQL_ONTOLOGY_EDGES (REVISION_ID, TARGET_NODE_ID)",
    "CREATE INDEX IX_NL2SQL_ONT_SESSION_PROFILE ON NL2SQL_ONTOLOGY_QUERY_SESSIONS "
    "(PROFILE_ID, UPDATED_AT)",
    "CREATE INDEX IX_NL2SQL_ONT_ART_SESSION ON NL2SQL_ONTOLOGY_ARTIFACTS "
    "(SESSION_ID, ARTIFACT_TYPE)",
    "CREATE INDEX IX_NL2SQL_ONT_PROP_SESSION ON NL2SQL_ONTOLOGY_PROPOSALS (SESSION_ID, STATUS)",
    "CREATE INDEX IX_NL2SQL_ONT_IDEMPOTENCY_RESOURCE ON NL2SQL_ONTOLOGY_IDEMPOTENCY "
    "(RESOURCE_ID, STATUS)",
    "CREATE INDEX IX_NL2SQL_ONT_SOURCE_PROFILE ON NL2SQL_ONTOLOGY_SOURCE_DOCS (PROFILE_ID, STATUS)",
    "CREATE INDEX IX_NL2SQL_ONT_JOB_STATE ON NL2SQL_ONTOLOGY_JOBS (JOB_TYPE, STATUS, UPDATED_AT)",
    "CREATE INDEX IX_NL2SQL_ONT_REC_QUESTION ON NL2SQL_ONTOLOGY_RECOMMENDATIONS "
    "(QUESTION_HASH, STATUS)",
    "CREATE UNIQUE INDEX UX_NL2SQL_ONT_ONE_PUBLISHED ON NL2SQL_ONTOLOGY_REVISIONS "
    "(CASE WHEN STATUS = 'published' THEN NVL(PROFILE_ID, '__legacy__') END)",
)

ONTOLOGY_DDL_STATEMENTS: tuple[str, ...] = (
    *(ONTOLOGY_TABLE_DDL[collection] for collection in ONTOLOGY_COLLECTIONS),
    *ONTOLOGY_INDEX_DDL,
)


class OntologyStore(Protocol):
    """Storage contract shared by Oracle production and in-memory CI stores."""

    mode: str

    def ensure_schema(self) -> None:
        """Create missing storage objects; never called implicitly."""

    def get_document(
        self,
        collection: OntologyCollection,
        identity: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Read one detached document by its complete identity."""

    def list_documents(
        self,
        collection: OntologyCollection,
        filters: Mapping[str, Any] | None = None,
        *,
        include_embedding: bool = True,
    ) -> list[dict[str, Any]]:
        """List detached documents using indexed equality filters."""

    def save_document(
        self,
        collection: OntologyCollection,
        document: Mapping[str, Any] | Any,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        """Create or update one document and return its new version/ETag."""

    def save_documents_atomic(
        self,
        collection: OntologyCollection,
        documents: Sequence[tuple[Mapping[str, Any], str | None]],
    ) -> list[dict[str, Any]]:
        """Save multiple documents in one storage transaction."""

    def delete_documents(
        self,
        collection: OntologyCollection,
        filters: Mapping[str, Any],
    ) -> int:
        """Delete documents matching indexed equality filters and return the row count."""

    def search_node_embeddings(
        self,
        *,
        revision_id: str,
        query_embedding: Sequence[float],
        candidate_node_ids: Sequence[str],
        limit: int,
    ) -> list[tuple[str, float]]:
        """Search ontology node vectors inside the backing store when supported."""

    def get_idempotency(self, operation: str, idempotency_key: str) -> dict[str, Any] | None: ...

    def save_idempotency(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]: ...

    def get_job(self, job_id: str) -> dict[str, Any] | None: ...

    def save_job(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]: ...

    def save_artifact(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]: ...

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None: ...

    def list_artifacts(self, session_id: str) -> list[dict[str, Any]]: ...


class _ConvenienceMethods:
    """Domain-specific wrappers over the small generic storage contract."""

    def get_document(
        self,
        collection: OntologyCollection,
        identity: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def list_documents(
        self,
        collection: OntologyCollection,
        filters: Mapping[str, Any] | None = None,
        *,
        include_embedding: bool = True,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def save_document(
        self,
        collection: OntologyCollection,
        document: Mapping[str, Any] | Any,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def save_documents_atomic(
        self,
        collection: OntologyCollection,
        documents: Sequence[tuple[Mapping[str, Any], str | None]],
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def delete_documents(
        self,
        collection: OntologyCollection,
        filters: Mapping[str, Any],
    ) -> int:
        raise NotImplementedError

    def search_node_embeddings(
        self,
        *,
        revision_id: str,
        query_embedding: Sequence[float],
        candidate_node_ids: Sequence[str],
        limit: int,
    ) -> list[tuple[str, float]]:
        return []

    def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        return self.get_document("revisions", {"revision_id": revision_id})

    def save_revision(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("revisions", document, expected_etag=expected_etag)

    def list_revisions(self) -> list[dict[str, Any]]:
        return self.list_documents("revisions")

    def get_node(self, revision_id: str, node_id: str) -> dict[str, Any] | None:
        return self.get_document("nodes", {"revision_id": revision_id, "node_id": node_id})

    def save_node(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("nodes", document, expected_etag=expected_etag)

    def list_nodes(self, revision_id: str) -> list[dict[str, Any]]:
        return self.list_documents("nodes", {"revision_id": revision_id})

    def get_edge(self, revision_id: str, edge_id: str) -> dict[str, Any] | None:
        return self.get_document("edges", {"revision_id": revision_id, "edge_id": edge_id})

    def save_edge(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("edges", document, expected_etag=expected_etag)

    def list_edges(self, revision_id: str) -> list[dict[str, Any]]:
        return self.list_documents("edges", {"revision_id": revision_id})

    def get_profile_view(
        self, profile_id: str, revision_id: str | None = None
    ) -> dict[str, Any] | None:
        if revision_id is None:
            # 1 compatibility cycle: old callers requested only the current profile view.
            # Runtime paths always pass revision_id and therefore remain deterministic.
            documents = self.list_documents("profile_views", {"profile_id": profile_id})
            return (
                max(
                    documents,
                    key=lambda item: (
                        str(item.get("updated_at") or ""),
                        str(item.get("revision_id") or ""),
                    ),
                )
                if documents
                else None
            )
        return self.get_document(
            "profile_views",
            {"profile_id": profile_id, "revision_id": revision_id},
        )

    def save_profile_view(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("profile_views", document, expected_etag=expected_etag)

    def get_query_session(self, session_id: str) -> dict[str, Any] | None:
        return self.get_document("query_sessions", {"session_id": session_id})

    def save_query_session(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("query_sessions", document, expected_etag=expected_etag)

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self.get_document("artifacts", {"artifact_id": artifact_id})

    def save_artifact(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("artifacts", document, expected_etag=expected_etag)

    def list_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        return self.list_documents("artifacts", {"session_id": session_id})

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return self.get_document("proposals", {"proposal_id": proposal_id})

    def save_proposal(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("proposals", document, expected_etag=expected_etag)

    def list_proposals(self, session_id: str) -> list[dict[str, Any]]:
        return self.list_documents("proposals", {"session_id": session_id})

    def get_idempotency(self, operation: str, idempotency_key: str) -> dict[str, Any] | None:
        return self.get_document(
            "idempotency",
            {"operation": operation, "idempotency_key": idempotency_key},
        )

    def save_idempotency(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("idempotency", document, expected_etag=expected_etag)

    def get_source_document(self, source_document_id: str) -> dict[str, Any] | None:
        return self.get_document("source_documents", {"source_document_id": source_document_id})

    def save_source_document(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("source_documents", document, expected_etag=expected_etag)

    def list_source_documents(self, profile_id: str) -> list[dict[str, Any]]:
        return self.list_documents("source_documents", {"profile_id": profile_id})

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.get_document("jobs", {"job_id": job_id})

    def save_job(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("jobs", document, expected_etag=expected_etag)

    def list_jobs(
        self, *, job_type: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        filters = {
            key: value
            for key, value in {"job_type": job_type, "status": status}.items()
            if value is not None
        }
        return self.list_documents("jobs", filters)

    def get_recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        return self.get_document("recommendations", {"recommendation_id": recommendation_id})

    def save_recommendation(
        self, document: Mapping[str, Any] | Any, *, expected_etag: str | None = None
    ) -> dict[str, Any]:
        return self.save_document("recommendations", document, expected_etag=expected_etag)


def _document_identity(
    collection: OntologyCollection,
    document: Mapping[str, Any],
) -> tuple[str, ...]:
    spec = _SPECS[collection]
    values: list[str] = []
    for field in spec.key_fields:
        value = str(document.get(field) or "").strip()
        if not value:
            raise ValueError(f"{collection}.{field} is required.")
        values.append(value)
    return tuple(values)


def _validate_identity(
    collection: OntologyCollection,
    identity: Mapping[str, Any],
) -> tuple[str, ...]:
    spec = _SPECS[collection]
    if frozenset(identity) != frozenset(spec.key_fields):
        expected = ", ".join(spec.key_fields)
        raise ValueError(f"{collection} identity must contain exactly: {expected}.")
    return _document_identity(collection, identity)


def _validate_filters(
    collection: OntologyCollection,
    filters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    normalized = dict(filters or {})
    unknown = frozenset(normalized) - _SPECS[collection].allowed_filter_fields
    if unknown:
        raise ValueError(f"Unsupported {collection} filters: {', '.join(sorted(unknown))}.")
    return normalized


class InMemoryOntologyStore(_ConvenienceMethods):
    """Thread-safe deterministic store for local runtime and CI."""

    mode = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._documents: dict[OntologyCollection, dict[tuple[str, ...], dict[str, Any]]] = {
            collection: {} for collection in ONTOLOGY_COLLECTIONS
        }

    def ensure_schema(self) -> None:
        """Memory storage requires no schema initialization."""

    def get_document(
        self,
        collection: OntologyCollection,
        identity: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        key = _validate_identity(collection, identity)
        with self._lock:
            document = self._documents[collection].get(key)
            return deserialize_json(canonical_json(document)) if document is not None else None

    def list_documents(
        self,
        collection: OntologyCollection,
        filters: Mapping[str, Any] | None = None,
        *,
        include_embedding: bool = True,
    ) -> list[dict[str, Any]]:
        accepted_filters = _validate_filters(collection, filters)
        with self._lock:
            documents = [
                document
                for document in self._documents[collection].values()
                if all(document.get(field) == value for field, value in accepted_filters.items())
            ]
            documents.sort(key=lambda item: _document_identity(collection, item))
            detached = [deserialize_json(canonical_json(document)) for document in documents]
            if not include_embedding:
                for document in detached:
                    document.pop("embedding", None)
            return detached

    def save_document(
        self,
        collection: OntologyCollection,
        document: Mapping[str, Any] | Any,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        normalized = deserialize_json(canonical_json(document))
        key = _document_identity(collection, normalized)
        with self._lock:
            current = self._documents[collection].get(key)
            prepared = next_versioned_document(
                normalized,
                current=current,
                expected_etag=expected_etag,
            )
            self._documents[collection][key] = prepared
            return deserialize_json(canonical_json(prepared))

    def save_documents_atomic(
        self,
        collection: OntologyCollection,
        documents: Sequence[tuple[Mapping[str, Any], str | None]],
    ) -> list[dict[str, Any]]:
        with self._lock:
            prepared_documents: list[tuple[tuple[str, ...], dict[str, Any]]] = []
            for document, expected_etag in documents:
                normalized = deserialize_json(canonical_json(document))
                key = _document_identity(collection, normalized)
                current = self._documents[collection].get(key)
                prepared = next_versioned_document(
                    normalized,
                    current=current,
                    expected_etag=expected_etag,
                )
                prepared_documents.append((key, prepared))
            for key, prepared in prepared_documents:
                self._documents[collection][key] = prepared
            return [
                deserialize_json(canonical_json(prepared)) for _key, prepared in prepared_documents
            ]

    def delete_documents(
        self,
        collection: OntologyCollection,
        filters: Mapping[str, Any],
    ) -> int:
        accepted_filters = _validate_filters(collection, filters)
        if not accepted_filters:
            raise ValueError("Ontology document delete requires at least one filter.")
        with self._lock:
            keys = [
                key
                for key, document in self._documents[collection].items()
                if all(document.get(field) == value for field, value in accepted_filters.items())
            ]
            for key in keys:
                self._documents[collection].pop(key, None)
            return len(keys)

    def search_node_embeddings(
        self,
        *,
        revision_id: str,
        query_embedding: Sequence[float],
        candidate_node_ids: Sequence[str],
        limit: int,
    ) -> list[tuple[str, float]]:
        candidate_set = set(candidate_node_ids)

        def cosine(left: Sequence[float], right: Sequence[float]) -> float:
            left_norm = sum(value * value for value in left) ** 0.5
            right_norm = sum(value * value for value in right) ** 0.5
            if left_norm == 0 or right_norm == 0:
                return 0.0
            return float(
                sum(a * b for a, b in zip(left, right, strict=False)) / (left_norm * right_norm)
            )

        rows = self.list_documents("nodes", {"revision_id": revision_id})
        scored: list[tuple[str, float]] = []
        for row in rows:
            if row.get("embedding") is None or str(row["node_id"]) not in candidate_set:
                continue
            embedding = [float(value) for value in row["embedding"]]
            scored.append((str(row["node_id"]), cosine(query_embedding, embedding)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]


class OracleOntologyStore(_ConvenienceMethods):
    """Oracle AI Database implementation using a caller-supplied connection factory."""

    mode = "oracle"

    def __init__(
        self,
        *,
        connection_factory: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._connection_factory = connection_factory
        self._schema_ready = False
        self._schema_lock = threading.RLock()

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        with self._connection_factory() as connection:
            call_timeout_ms = int(
                max(1.0, float(get_settings().nl2sql_oracle_call_timeout_seconds)) * 1000
            )
            if hasattr(connection, "call_timeout"):
                connection.call_timeout = call_timeout_ms
            yield connection

    def ensure_schema(self) -> None:
        """Migration 済み schema を bounded query で確認する（DDL は実行しない）。"""

        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM NL2SQL_ONTOLOGY_REVISIONS WHERE 1 = 0")
            self._schema_ready = True

    def get_document(
        self,
        collection: OntologyCollection,
        identity: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        _validate_identity(collection, identity)
        spec = _SPECS[collection]
        where_sql, binds = _where_clause(spec, identity)
        select_columns = "PAYLOAD_JSON, VERSION_NO, ETAG"
        if spec.has_embedding:
            select_columns += ", EMBEDDING"
        sql = f"SELECT {select_columns} FROM {spec.table_name} WHERE {where_sql}"  # nosec B608
        with self._connection() as connection, connection.cursor() as cursor:
            configure_clob_fetch_as_text(cursor)
            cursor.execute(sql, binds)
            row = cursor.fetchone()
        return _decode_row(row, has_embedding=spec.has_embedding) if row else None

    def list_documents(
        self,
        collection: OntologyCollection,
        filters: Mapping[str, Any] | None = None,
        *,
        include_embedding: bool = True,
    ) -> list[dict[str, Any]]:
        accepted_filters = _validate_filters(collection, filters)
        spec = _SPECS[collection]
        select_columns = "PAYLOAD_JSON, VERSION_NO, ETAG"
        if spec.has_embedding and include_embedding:
            select_columns += ", EMBEDDING"
        sql = f"SELECT {select_columns} FROM {spec.table_name}"  # nosec B608
        binds: dict[str, Any] = {}
        if accepted_filters:
            where_sql, binds = _where_clause(spec, accepted_filters)
            sql += f" WHERE {where_sql}"
        sql += " ORDER BY UPDATED_AT DESC"
        with self._connection() as connection, connection.cursor() as cursor:
            configure_clob_fetch_as_text(cursor)
            cursor.execute(sql, binds)
            rows = cursor.fetchall()
        return [
            _decode_row(row, has_embedding=spec.has_embedding and include_embedding) for row in rows
        ]

    def save_document(
        self,
        collection: OntologyCollection,
        document: Mapping[str, Any] | Any,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        normalized = deserialize_json(canonical_json(document))
        _document_identity(collection, normalized)
        spec = _SPECS[collection]
        with self._connection() as connection, connection.cursor() as cursor:
            current = self._select_for_update(cursor, collection, normalized)
            prepared = next_versioned_document(
                normalized,
                current=current,
                expected_etag=expected_etag,
            )
            try:
                _set_payload_clob_input_size(cursor)
                if current is None:
                    self._insert_document(cursor, spec, prepared)
                else:
                    self._update_document(cursor, spec, prepared, str(current["etag"]))
                connection.commit()
            except Exception as exc:
                rollback = getattr(connection, "rollback", None)
                if callable(rollback):
                    rollback()
                if "ORA-00001" in str(exc).upper():
                    raise OntologyVersionConflict(
                        "The Ontology document was concurrently created."
                    ) from exc
                raise
        return prepared

    def save_documents_atomic(
        self,
        collection: OntologyCollection,
        documents: Sequence[tuple[Mapping[str, Any], str | None]],
    ) -> list[dict[str, Any]]:
        spec = _SPECS[collection]
        prepared_documents: list[dict[str, Any]] = []
        with self._connection() as connection, connection.cursor() as cursor:
            try:
                if documents and all(
                    expected_etag is None for document, expected_etag in documents
                ):
                    for document, _expected_etag in documents:
                        normalized = deserialize_json(canonical_json(document))
                        _document_identity(collection, normalized)
                        prepared_documents.append(
                            next_versioned_document(
                                normalized,
                                current=None,
                                expected_etag=None,
                            )
                        )
                    self._insert_documents(cursor, spec, prepared_documents)
                    connection.commit()
                    return prepared_documents
                pending_operations: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
                for document, expected_etag in documents:
                    normalized = deserialize_json(canonical_json(document))
                    _document_identity(collection, normalized)
                    current = self._select_for_update(cursor, collection, normalized)
                    prepared = next_versioned_document(
                        normalized,
                        current=current,
                        expected_etag=expected_etag,
                    )
                    pending_operations.append((prepared, current))
                    prepared_documents.append(prepared)
                _set_payload_clob_input_size(cursor)
                for prepared, current in pending_operations:
                    if current is None:
                        self._insert_document(cursor, spec, prepared)
                    else:
                        self._update_document(cursor, spec, prepared, str(current["etag"]))
                connection.commit()
            except Exception as exc:
                rollback = getattr(connection, "rollback", None)
                if callable(rollback):
                    rollback()
                if "ORA-00001" in str(exc).upper():
                    raise OntologyVersionConflict(
                        "An Ontology document was concurrently created."
                    ) from exc
                raise
        return prepared_documents

    def delete_documents(
        self,
        collection: OntologyCollection,
        filters: Mapping[str, Any],
    ) -> int:
        accepted_filters = _validate_filters(collection, filters)
        if not accepted_filters:
            raise ValueError("Ontology document delete requires at least one filter.")
        spec = _SPECS[collection]
        where_sql, binds = _where_clause(spec, accepted_filters)
        sql = f"DELETE FROM {spec.table_name} WHERE {where_sql}"  # nosec B608
        with self._connection() as connection, connection.cursor() as cursor:
            try:
                cursor.execute(sql, binds)
                deleted = int(getattr(cursor, "rowcount", 0) or 0)
                connection.commit()
            except Exception:
                rollback = getattr(connection, "rollback", None)
                if callable(rollback):
                    rollback()
                raise
        return deleted

    def search_node_embeddings(
        self,
        *,
        revision_id: str,
        query_embedding: Sequence[float],
        candidate_node_ids: Sequence[str],
        limit: int,
    ) -> list[tuple[str, float]]:
        ids = [str(value) for value in candidate_node_ids if str(value).strip()]
        if not ids or not query_embedding or limit <= 0:
            return []
        id_binds = {f"node_id_{index}": value for index, value in enumerate(ids)}
        id_sql = ", ".join(f":{name}" for name in id_binds)
        embedding_json = json.dumps([float(value) for value in query_embedding])
        sql = (
            "SELECT NODE_ID, VECTOR_DISTANCE(EMBEDDING, TO_VECTOR(:query_embedding), COSINE) "  # nosec B608
            "AS DISTANCE FROM NL2SQL_ONTOLOGY_NODES "
            "WHERE REVISION_ID = :revision_id "
            "AND NODE_ID IN (" + id_sql + ") "
            "AND EMBEDDING IS NOT NULL "
            "ORDER BY DISTANCE FETCH FIRST :limit ROWS ONLY"
        )
        binds: dict[str, Any] = {
            "revision_id": revision_id,
            "query_embedding": embedding_json,
            "limit": int(limit),
            **id_binds,
        }
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(sql, binds)
            rows = cursor.fetchall()
        return [(str(row[0]), 1.0 - float(row[1])) for row in rows]

    def _select_for_update(
        self,
        cursor: Any,
        collection: OntologyCollection,
        document: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        spec = _SPECS[collection]
        identity = {field: document[field] for field in spec.key_fields}
        where_sql, binds = _where_clause(spec, identity)
        select_columns = "PAYLOAD_JSON, VERSION_NO, ETAG"
        if spec.has_embedding:
            select_columns += ", EMBEDDING"
        wait_seconds = _oracle_row_lock_wait_seconds()
        sql = (
            f"SELECT {select_columns} FROM {spec.table_name} "  # nosec B608
            f"WHERE {where_sql} FOR UPDATE WAIT {wait_seconds}"
        )
        try:
            cursor.execute(sql, binds)
        except Exception as exc:
            if _is_oracle_row_lock_timeout(exc):
                raise TimeoutError(
                    f"Ontology store の行ロック待ちが {wait_seconds} 秒を超えました。"
                    "実行中の job または別操作を確認して再実行してください。"
                ) from exc
            raise
        row = cursor.fetchone()
        return _decode_row(row, has_embedding=spec.has_embedding) if row else None

    def _insert_document(
        self,
        cursor: Any,
        spec: _CollectionSpec,
        document: Mapping[str, Any],
    ) -> None:
        field_columns = _persisted_field_columns(spec)
        columns = [column for _, column in field_columns]
        binds = {field: _scalar_bind_value(document.get(field)) for field, _ in field_columns}
        columns.extend(("VERSION_NO", "ETAG", "PAYLOAD_JSON"))
        binds.update(
            {
                "version_no": document["version"],
                "etag": document["etag"],
                "payload_json": _payload_json(document),
            }
        )
        bind_names = [f":{field}" for field, _ in field_columns]
        bind_names.extend((":version_no", ":etag", ":payload_json"))
        if spec.has_embedding:
            columns.append("EMBEDDING")
            bind_names.append(":embedding")
            binds["embedding"] = _embedding_bind_value(document.get("embedding"))
        sql = (
            f"INSERT INTO {spec.table_name} ({', '.join(columns)}) "  # nosec B608
            f"VALUES ({', '.join(bind_names)})"
        )
        cursor.execute(sql, binds)

    def _insert_documents(
        self,
        cursor: Any,
        spec: _CollectionSpec,
        documents: Sequence[Mapping[str, Any]],
    ) -> None:
        """小さい JSON は文字列で batch bind し、行ごとの一時 LOB 往復を避ける。"""

        if not documents:
            return
        field_columns = _persisted_field_columns(spec)
        columns = [column for _, column in field_columns]
        columns.extend(("VERSION_NO", "ETAG", "PAYLOAD_JSON"))
        bind_names = [f":{field}" for field, _ in field_columns]
        bind_names.extend((":version_no", ":etag", ":payload_json"))
        if spec.has_embedding:
            columns.append("EMBEDDING")
            bind_names.append(":embedding")
        sql = (
            f"INSERT INTO {spec.table_name} ({', '.join(columns)}) "  # nosec B608
            f"VALUES ({', '.join(bind_names)})"
        )
        rows: list[dict[str, Any]] = []
        for document in documents:
            binds = {
                field: _scalar_bind_value(document.get(field)) for field, _column in field_columns
            }
            binds.update(
                {
                    "version_no": document["version"],
                    "etag": document["etag"],
                    "payload_json": _payload_json(document),
                }
            )
            if spec.has_embedding:
                binds["embedding"] = _embedding_bind_value(document.get("embedding"))
            rows.append(binds)
        for offset in range(0, len(rows), _ORACLE_INSERT_BATCH_SIZE):
            batch = rows[offset : offset + _ORACLE_INSERT_BATCH_SIZE]
            payloads = [row["payload_json"] for row in batch]
            set_input_sizes = getattr(cursor, "setinputsizes", None)
            if callable(set_input_sizes):
                if max(len(payload.encode("utf-8")) for payload in payloads) <= (
                    _ORACLE_STRING_BIND_MAX_BYTES
                ):
                    set_input_sizes(payload_json=max(len(payload) for payload in payloads))
                else:
                    # 大きい本文は従来の CLOB bind を維持する。分割しても commit は
                    # 呼出元で一度だけ行い、後続 batch の失敗時は全件 rollback する。
                    _set_payload_clob_input_size(cursor)
            cursor.executemany(sql, batch)

    def _update_document(
        self,
        cursor: Any,
        spec: _CollectionSpec,
        document: Mapping[str, Any],
        current_etag: str,
    ) -> None:
        field_columns = _persisted_field_columns(spec, include_keys=False)
        assignments = [f"{column} = :{field}" for field, column in field_columns]
        assignments.extend(
            (
                "VERSION_NO = :version_no",
                "ETAG = :etag",
                "PAYLOAD_JSON = :payload_json",
                "UPDATED_AT = SYSTIMESTAMP",
            )
        )
        binds = {field: _scalar_bind_value(document.get(field)) for field, _ in field_columns}
        binds.update(
            {
                "version_no": document["version"],
                "etag": document["etag"],
                "payload_json": _payload_json(document),
                "current_etag": current_etag,
            }
        )
        if spec.has_embedding:
            assignments.append("EMBEDDING = :embedding")
            binds["embedding"] = _embedding_bind_value(document.get("embedding"))
        identity = {field: document[field] for field in spec.key_fields}
        where_sql, identity_binds = _where_clause(spec, identity)
        binds.update(identity_binds)
        sql = (
            f"UPDATE {spec.table_name} SET {', '.join(assignments)} "  # nosec B608
            f"WHERE {where_sql} AND ETAG = :current_etag"
        )
        cursor.execute(sql, binds)
        if getattr(cursor, "rowcount", 1) == 0:
            raise OntologyVersionConflict(
                "The Ontology document was changed during the update.",
                current_etag=current_etag,
                current_version=int(document["version"]) - 1,
            )


def _persisted_field_columns(
    spec: _CollectionSpec,
    *,
    include_keys: bool = True,
) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    if include_keys:
        fields.extend((field, field.upper()) for field in spec.key_fields)
    fields.extend(spec.scalar_columns.items())
    return fields


def _column_for_field(spec: _CollectionSpec, field: str) -> str:
    if field in spec.key_fields:
        return field.upper()
    return spec.scalar_columns[field]


def _where_clause(
    spec: _CollectionSpec,
    values: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    binds: dict[str, Any] = {}
    for field, value in values.items():
        parts.append(f"{_column_for_field(spec, field)} = :filter_{field}")
        binds[f"filter_{field}"] = _scalar_bind_value(value)
    return " AND ".join(parts), binds


def _scalar_bind_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def _embedding_bind_value(value: Any) -> array[float] | None:
    if value is None:
        return None
    if isinstance(value, array) and value.typecode == "f":
        return value
    return array("f", (float(item) for item in value))


def _payload_json(document: Mapping[str, Any]) -> str:
    payload = dict(document)
    payload.pop("version", None)
    payload.pop("etag", None)
    payload.pop("embedding", None)
    return canonical_json(payload)


def _decode_row(row: Sequence[Any], *, has_embedding: bool) -> dict[str, Any]:
    document = deserialize_json(row[0])
    document["version"] = int(row[1])
    document["etag"] = str(row[2])
    if has_embedding and len(row) > 3 and row[3] is not None:
        embedding = row[3]
        document["embedding"] = list(embedding) if not isinstance(embedding, list) else embedding
    return document


def _set_payload_clob_input_size(cursor: Any) -> None:
    set_input_sizes = getattr(cursor, "setinputsizes", None)
    if not callable(set_input_sizes):
        return
    try:
        oracledb = import_module("oracledb")
        db_type_clob = oracledb.DB_TYPE_CLOB
    except Exception:  # pragma: no cover - defensive when driver is unavailable
        return
    set_input_sizes(payload_json=db_type_clob)
