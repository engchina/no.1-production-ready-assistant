"""取込ジョブと成果物へ、処理開始時に固定した公開版を引き継ぐ。"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from app.rag.language_pack_releases import (
    active_release,
    artifact_reusable_for_phase,
    bind_release,
    pack_from_release,
)
from app.schemas.language_pack_release import LanguagePackRelease

if TYPE_CHECKING:
    from app.clients.oracle import OracleClient

_JOB: ContextVar[str | None] = ContextVar("rag_language_pack_job", default=None)


@contextmanager
def ingestion_release_scope(release: LanguagePackRelease, job_id: str) -> Iterator[None]:
    token = _JOB.set(job_id)
    try:
        with bind_release(release):
            yield
    finally:
        _JOB.reset(token)


def ingestion_release_job_id() -> str | None:
    return _JOB.get()


async def artifact_language_compatible(
    oracle: "OracleClient", provenance: object, phase: str
) -> bool:
    """版が不明な成果物を推定せず、再利用する前工程への実際の影響を照合する。"""
    from app.clients.language_pack_release_store import LanguagePackReleaseStore

    current = active_release()
    if current is None:
        return True
    if not isinstance(provenance, Mapping):
        return False
    identity = provenance.get("language_pack_release_id")
    ingest_hash = provenance.get("language_pack_ingest_hash")
    if not isinstance(identity, str) or not isinstance(ingest_hash, str):
        return False
    before = await LanguagePackReleaseStore(oracle).get(identity)
    if before is None or before.ingest_hash != ingest_hash:
        return False
    pack_from_release(before)
    return artifact_reusable_for_phase(before, current, phase)


def language_pack_artifact_statements() -> str:
    columns = {
        "RAG_INGESTION_JOBS": {"LANGUAGE_PACK_RELEASE_ID": "VARCHAR2(64)"},
        "RAG_CHUNK_SETS": {
            "LANGUAGE_PACK_RELEASE_ID": "VARCHAR2(64)",
            "LANGUAGE_PACK_INGEST_HASH": "VARCHAR2(64)",
            "LANGUAGE_PACK_JOB_ID": "VARCHAR2(64)",
            "LANGUAGE_PACK_BUILT_AT": "TIMESTAMP WITH TIME ZONE",
        },
    }
    blocks = []
    for table, fields in columns.items():
        for column, kind in fields.items():
            blocks.append(f"""DECLARE v_count NUMBER; BEGIN
                SELECT COUNT(*) INTO v_count FROM user_tab_columns
                WHERE table_name = '{table}' AND column_name = '{column}';
                IF v_count = 0 THEN EXECUTE IMMEDIATE
                    'ALTER TABLE {table} ADD ({column} {kind})'; END IF;
            END;
            /""")
    for table, constraint in (
        ("RAG_INGESTION_JOBS", "RAG_INGEST_JOB_LANG_RELEASE_FK"),
        ("RAG_CHUNK_SETS", "RAG_CHUNK_SET_LANG_RELEASE_FK"),
    ):
        blocks.append(f"""DECLARE v_count NUMBER; BEGIN
            SELECT COUNT(*) INTO v_count FROM user_constraints
            WHERE constraint_name = '{constraint}';
            IF v_count = 0 THEN EXECUTE IMMEDIATE
                'ALTER TABLE {table} ADD CONSTRAINT {constraint} '
                || 'FOREIGN KEY (LANGUAGE_PACK_RELEASE_ID) '
                || 'REFERENCES RAG_LANGUAGE_PACK_RELEASES (RELEASE_ID)'; END IF;
        END;
        /""")
    return "\n".join(blocks)
