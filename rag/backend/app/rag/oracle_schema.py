"""Oracle AI Database schema artifact generator.

Oracle DDL は staging / production でレビュー済み artifact として適用する。
この CLI はアプリ内の DDL 契約から deterministic な SQL と manifest を生成する。
"""

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.document_category_store import oracle_document_category_schema_sql
from app.clients.document_cleanup_store import oracle_document_cleanup_schema_sql
from app.clients.entity_store import oracle_entity_schema_sql
from app.clients.evaluation_calibration_store import oracle_evaluation_calibration_schema_sql
from app.clients.language_pack_release_store import language_pack_release_statements
from app.clients.oracle import (
    oracle_answer_prompt_schema_sql,
    oracle_answer_record_schema_sql,
    oracle_chunk_set_schema_sql,
    oracle_conversation_schema_sql,
    oracle_document_recipe_schema_sql,
    oracle_document_schema_sql,
    oracle_document_sections_schema_sql,
    oracle_evaluation_artifact_schema_sql,
    oracle_evaluation_job_schema_sql,
    oracle_feedback_details_schema_sql,
    oracle_feedback_schema_sql,
    oracle_ingestion_audit_schema_sql,
    oracle_ingestion_job_schema_sql,
    oracle_ingestion_segment_schema_sql,
    oracle_knowledge_base_schema_sql,
    oracle_knowledge_graph_schema_sql,
    oracle_message_schema_sql,
    oracle_role_access_schema_sql,
    oracle_search_answer_profile_knowledge_schema_sql,
    oracle_search_answer_profile_schema_sql,
    oracle_search_audit_schema_sql,
    oracle_vector_index_create_sql,
    oracle_vector_schema_sql,
)
from app.clients.oracle_run_history import oracle_run_history_schema_sql
from app.clients.section_map_store import oracle_section_map_schema_sql
from app.clients.support_guide_store import (
    oracle_support_guide_embedding_schema_sql,
    oracle_support_guide_revision_schema_sql,
    oracle_support_guide_schema_sql,
)
from app.rag.ingestion_language import language_pack_artifact_statements
from app.rag.request_context import with_system_access
from app.rag.search_answer_profile_migration import rename_sql as search_answer_profile_rename_sql
from app.rag.vector_index_adapter import default_vector_index_build

SCHEMA_NAME = "production-ready-rag-oracle-26ai"
SCHEMA_VERSION = "3"
MIGRATION_ARTIFACT_VERSION = "20261009_001"
VECTOR_CONTRACT = "VECTOR(1536, FLOAT32)"
# 新しい環境の主検索索引の契約(作成の DDL と同じく、既定の検索精度の推奨ビルドから作る。#1990)。
_DEFAULT_VECTOR_INDEX_BUILD = default_vector_index_build()
VECTOR_INDEX_CONTRACT = {
    "type": "HNSW",
    "distance": _DEFAULT_VECTOR_INDEX_BUILD.distance,
    "target_accuracy": _DEFAULT_VECTOR_INDEX_BUILD.target_accuracy,
    "neighbors": _DEFAULT_VECTOR_INDEX_BUILD.neighbors,
    "efconstruction": _DEFAULT_VECTOR_INDEX_BUILD.efconstruction,
}


def vector_index_reindex_sql(
    *,
    target_accuracy: int,
    neighbors: int,
    efconstruction: int,
    distance: str = "COSINE",
    table: str = "rag_chunks",
    index: str = "rag_chunks_embedding_hnsw_idx",
) -> str:
    """選択 profile のビルド推奨値で HNSW 索引を再作成する DDL を返す。

    backend は実行時に DDL を実行しないため、ここでは適用用の SQL を生成するだけにする
    (DBA がレビュー済み artifact として適用する)。引数は ``resolve_vector_index_adapter``
    で解決済みの値を呼び出し側が渡す。
    対象は主検索索引 rag_chunks だけ。
    """
    create_sql = oracle_vector_index_create_sql(
        table=table,
        index=index,
        target_accuracy=target_accuracy,
        neighbors=neighbors,
        efconstruction=efconstruction,
        distance=distance,
    )
    return f"DROP INDEX {index};\n{create_sql}"


@dataclass(frozen=True)
class OracleSchemaSection:
    """Oracle schema artifact の論理セクション。

    `destructive_note` は、データを消す（テーブルの DROP・行の DELETE を含む）migration の印と、
    消えるデータ・適用の前にすることの説明（#619）。空でない migration が未適用なら、
    「作成・更新」（画面・`system_schema_cli initialize`）は明示の承認が無ければ止まり、
    実 Oracle のテストの fixture は当てない。checksum（SQL だけから作る）には含めない。
    """

    name: str
    table_name: str
    sql: str
    destructive_note: str = ""

    @property
    def destructive(self) -> bool:
        return bool(self.destructive_note)


def oracle_system_schema_control_sql() -> str:
    """明示的な schema 操作の lease / migration ledger 用 DDL を返す。"""
    return """
CREATE TABLE rag_schema_operations (
    operation_key    VARCHAR2(64) PRIMARY KEY,
    status           VARCHAR2(16) DEFAULT 'IDLE' NOT NULL,
    operation_kind   VARCHAR2(16),
    lease_owner      VARCHAR2(64),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    last_error_code  VARCHAR2(64),
    schema_epoch     NUMBER(19) DEFAULT 0 NOT NULL,
    updated_at       TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT rag_schema_operations_status_ck
        CHECK (status IN ('IDLE', 'RUNNING', 'FAILED')),
    CONSTRAINT rag_schema_operations_kind_ck
        CHECK (operation_kind IS NULL OR operation_kind IN ('INITIALIZE', 'RECREATE'))
);

CREATE TABLE rag_schema_migrations (
    migration_name VARCHAR2(64) PRIMARY KEY,
    description    VARCHAR2(512) NOT NULL,
    checksum       CHAR(64) NOT NULL,
    applied_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

MERGE INTO rag_schema_operations target
USING (
    SELECT 'system_schema' AS operation_key
    FROM dual
) source
ON (target.operation_key = source.operation_key)
WHEN NOT MATCHED THEN INSERT (
    operation_key,
    status,
    schema_epoch,
    updated_at
) VALUES (
    source.operation_key,
    'IDLE',
    0,
    SYSTIMESTAMP
);
""".strip()


def oracle_schema_sections() -> list[OracleSchemaSection]:
    """production RAG に必要な Oracle schema section を順序付きで返す。"""
    return [
        OracleSchemaSection(
            name="system_schema_control",
            table_name="rag_schema_operations",
            sql=oracle_system_schema_control_sql(),
        ),
        OracleSchemaSection(
            name="language_pack_releases",
            table_name="rag_language_pack_releases",
            sql=language_pack_release_statements(),
        ),
        OracleSchemaSection(
            name="documents",
            table_name="rag_documents",
            sql=oracle_document_schema_sql(),
        ),
        OracleSchemaSection(
            name="document_recipes",
            table_name="rag_document_recipes",
            sql=oracle_document_recipe_schema_sql(),
        ),
        OracleSchemaSection(
            name="knowledge_bases",
            table_name="rag_knowledge_bases",
            sql=oracle_knowledge_base_schema_sql(),
        ),
        OracleSchemaSection(
            name="search_answer_profiles",
            table_name="rag_search_answer_profiles",
            sql=oracle_search_answer_profile_schema_sql(),
        ),
        OracleSchemaSection(
            name="answer_records",
            table_name="rag_answer_records",
            sql=oracle_answer_record_schema_sql(),
        ),
        OracleSchemaSection(
            name="answer_prompts",
            table_name="rag_answer_prompts",
            sql=oracle_answer_prompt_schema_sql(),
        ),
        # 実行履歴（チャット・RAG 検索の実行。#1890）。
        OracleSchemaSection(
            name="run_history",
            table_name="rag_run_history",
            sql=oracle_run_history_schema_sql(),
        ),
        # 文書の分類の一覧（大分類 → 中分類 → 小分類。#2131）。
        OracleSchemaSection(
            name="document_categories",
            table_name="rag_document_categories",
            sql=oracle_document_category_schema_sql(),
        ),
        # 文書の削除の後始末（保存先から消せなかった原本・成果物）の記録（#2452）。
        OracleSchemaSection(
            name="document_cleanups",
            table_name="rag_document_cleanups",
            sql=oracle_document_cleanup_schema_sql(),
        ),
        OracleSchemaSection(
            name="search_answer_profile_knowledge",
            table_name="rag_search_answer_profile_knowledge",
            sql=oracle_search_answer_profile_knowledge_schema_sql(),
        ),
        # 業務ガイド（#1237）。頭（下書き）と公開の版。
        OracleSchemaSection(
            name="support_guides",
            table_name="rag_support_guides",
            sql=oracle_support_guide_schema_sql(),
        ),
        OracleSchemaSection(
            name="support_guide_revisions",
            table_name="rag_support_guide_revisions",
            sql=oracle_support_guide_revision_schema_sql(),
        ),
        # 業務ガイドの公開の版の質問の例の埋め込み（意味の照合。#2398）。
        OracleSchemaSection(
            name="support_guide_embeddings",
            table_name="rag_support_guide_embeddings",
            sql=oracle_support_guide_embedding_schema_sql(),
        ),
        OracleSchemaSection(
            name="document_sections",
            table_name="rag_document_sections",
            sql=oracle_document_sections_schema_sql(),
        ),
        OracleSchemaSection(
            name="conversations",
            table_name="rag_conversations",
            sql=oracle_conversation_schema_sql(),
        ),
        OracleSchemaSection(
            name="messages",
            table_name="rag_messages",
            sql=oracle_message_schema_sql(),
        ),
        OracleSchemaSection(
            name="ingestion_jobs",
            table_name="rag_ingestion_jobs",
            sql=oracle_ingestion_job_schema_sql(),
        ),
        OracleSchemaSection(
            name="ingestion_segments",
            table_name="rag_ingestion_segments",
            sql=oracle_ingestion_segment_schema_sql(),
        ),
        OracleSchemaSection(
            name="chunks",
            table_name="rag_chunks",
            sql=oracle_vector_schema_sql(),
        ),
        OracleSchemaSection(
            name="chunk_sets",
            table_name="rag_chunk_sets",
            sql=oracle_chunk_set_schema_sql(),
        ),
        OracleSchemaSection(
            name="search_audit",
            table_name="rag_search_audit",
            sql=oracle_search_audit_schema_sql(),
        ),
        OracleSchemaSection(
            name="ingestion_audit",
            table_name="rag_ingestion_audit",
            sql=oracle_ingestion_audit_schema_sql(),
        ),
        OracleSchemaSection(
            name="knowledge_graph",
            table_name="rag_graph_entities",
            sql=oracle_knowledge_graph_schema_sql(),
        ),
        # 実体の層（#1362）。実体・別名・実体と chunk の関連。rag_chunk_sets / rag_chunks
        # を参照する。
        OracleSchemaSection(
            name="entities",
            table_name="rag_entities",
            sql=oracle_entity_schema_sql(),
        ),
        # 章節と chunk の対応（#1438）。rag_chunks を参照する。
        OracleSchemaSection(
            name="section_map",
            table_name="rag_chunk_set_sections",
            sql=oracle_section_map_schema_sql(),
        ),
        OracleSchemaSection(
            name="citation_feedback",
            table_name="rag_citation_feedback",
            sql=oracle_feedback_schema_sql(),
        ),
        OracleSchemaSection(
            name="feedback_details",
            table_name="rag_feedback_details",
            sql=oracle_feedback_details_schema_sql(),
        ),
        OracleSchemaSection(
            name="evaluation_artifacts",
            table_name="rag_evaluation_runs",
            sql=oracle_evaluation_artifact_schema_sql(),
        ),
        # 品質評価の job（非同期の実行・進捗・取り消し。#390）。
        OracleSchemaSection(
            name="evaluation_jobs",
            table_name="rag_evaluation_jobs",
            sql=oracle_evaluation_job_schema_sql(),
        ),
        # 品質評価の判定の校正（記入の集合・項目・判定・校正の結果。#3578）。
        OracleSchemaSection(
            name="evaluation_calibration",
            table_name="rag_evaluation_label_sets",
            sql=oracle_evaluation_calibration_schema_sql(),
        ),
        # ロールの RAG 権限と対象範囲（#214）。
        # PLATFORM_ROLES と検索・回答プロファイル・KB を参照する。
        OracleSchemaSection(
            name="role_access",
            table_name="rag_role_permissions",
            sql=oracle_role_access_schema_sql(),
        ),
    ]


def oracle_schema_sql(sections: Sequence[OracleSchemaSection] | None = None) -> str:
    """SQLcl 等で適用できる Oracle schema SQL artifact を返す。"""
    resolved_sections = list(sections or oracle_schema_sections())
    return (
        "\n\n".join(
            f"-- section: {section.name}\n{section.sql.rstrip()}" for section in resolved_sections
        )
        + "\n"
    )


def oracle_schema_migration_sections() -> list[OracleSchemaSection]:
    """過去の SQL/checksum は archive で固定し、新 migration だけを追加する。"""
    archive = json.loads(
        (Path(__file__).parent / "schema_migrations/pre_profile_rename.json").read_text()
    )
    sections = [OracleSchemaSection(**item) for item in archive["migrations"]]
    sections.append(
        OracleSchemaSection(
            name="20261003_001_search_answer_profiles",
            table_name="rag_search_answer_profiles",
            sql=search_answer_profile_rename_sql(),
            destructive_note=(
                "旧権限 code を新 code へコピーした後、旧 code の grant 行だけを削除します。"
                "実効権限と対象 ID・業務データは保持します。"
                "更新前に schema / 権限を export してください。"
            ),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261005_001_message_answer_runs",
            table_name="rag_messages",
            sql=message_answer_runs_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261007_001_document_superseded",
            table_name="rag_documents",
            sql=document_superseded_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261009_001_entity_layer",
            table_name="rag_entities",
            sql=entity_layer_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261011_001_section_chunk_map",
            table_name="rag_chunk_set_sections",
            sql=section_chunk_map_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261015_001_message_clarification",
            table_name="rag_messages",
            sql=message_clarification_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261015_002_run_history",
            table_name="rag_run_history",
            sql=run_history_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261015_003_run_history_conversation_idx",
            table_name="rag_run_history",
            sql=run_history_conversation_index_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261015_004_ingestion_job_progress",
            table_name="rag_ingestion_jobs",
            sql=ingestion_job_progress_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261016_001_retire_evaluation_compare",
            table_name="rag_evaluation_jobs",
            sql=retire_evaluation_compare_migration_sql(),
            destructive_note=(
                "品質評価の設定比較（#2088 で削除）の job の行（kind = 'compare'）を削除し、"
                "設定比較だけが使った列（rag_evaluation_runs.best_experiment_id・"
                "rag_evaluation_jobs.current_experiment_id）と索引を削除します。"
                "評価の結果（rag_evaluation_runs の行と result_json）は残ります。"
                "設定比較の job の記録が要るなら、"
                "更新前に rag_evaluation_jobs を書き出してください。"
            ),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261016_003_document_categories",
            table_name="rag_document_categories",
            sql=document_categories_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261018_001_support_guide_embeddings",
            table_name="rag_support_guide_embeddings",
            sql=support_guide_embeddings_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261018_002_message_answer_notice",
            table_name="rag_messages",
            sql=answer_notice_column_migration_sql("RAG_MESSAGES"),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261018_003_answer_record_notice",
            table_name="rag_answer_records",
            sql=answer_notice_column_migration_sql("RAG_ANSWER_RECORDS"),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261018_004_support_guide_schema_v2",
            table_name="rag_support_guides",
            sql=support_guide_schema_v2_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261019_001_restore_document_classification",
            table_name="rag_documents",
            sql=restore_document_classification_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261019_002_document_cleanups",
            table_name="rag_document_cleanups",
            sql=document_cleanups_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261019_003_structured_records_jsonl_profiles",
            table_name="rag_document_recipes",
            sql=structured_records_jsonl_profiles_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261019_010_message_attachments",
            table_name="rag_messages",
            sql=message_attachments_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261021_001_answer_prompt_placeholders",
            table_name="rag_answer_prompts",
            sql=answer_prompt_placeholders_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261021_002_document_delete_fk_indexes",
            table_name="rag_documents",
            sql="""
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_indexes
    WHERE index_name = 'RAG_ARTIFACT_LAYERS_DOCUMENT_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'CREATE INDEX rag_artifact_layers_document_idx '
            || 'ON rag_artifact_layers (document_id)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes
    WHERE index_name = 'RAG_DOCUMENTS_DUPLICATE_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'CREATE INDEX rag_documents_duplicate_idx '
            || 'ON rag_documents (duplicate_of_document_id)';
    END IF;
END;
/
""".strip(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261022_001_cited_evidence",
            table_name="rag_answer_records",
            sql=cited_evidence_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261026_001_evaluation_calibration",
            table_name="rag_evaluation_label_sets",
            sql=evaluation_calibration_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261026_002_evaluation_job_calibration_kind",
            table_name="rag_evaluation_jobs",
            sql=evaluation_job_calibration_kind_migration_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261027_001_raptor_prompt_consolidation",
            table_name="rag_answer_prompts",
            sql=raptor_prompt_consolidation_sql(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261028_001_language_pack_releases",
            table_name="rag_language_pack_releases",
            sql=language_pack_release_statements(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261028_002_language_pack_artifacts",
            table_name="rag_chunk_sets",
            sql=language_pack_artifact_statements(),
        )
    )
    sections.append(
        OracleSchemaSection(
            name="20261029_001_retire_query_history",
            table_name="rag_query_history",
            sql=retire_query_history_sql(),
            destructive_note=(
                "廃止する質問候補専用の RAG_QUERY_HISTORY と全行、同表の索引・制約を削除します。"
                "会話・メッセージ・回答の記録・実行履歴は削除しません。"
                "必要な履歴は適用前に同表を export してください。削除後は復元できません。"
            ),
        )
    )
    return sections


def retire_query_history_sql() -> str:
    """質問候補専用表だけを削除し、未作成・削除済みの環境でも適用する。"""
    return """
BEGIN
    EXECUTE IMMEDIATE 'DROP TABLE rag_query_history PURGE';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -942 THEN
            RAISE;
        END IF;
END;
/
""".strip()


def raptor_prompt_consolidation_sql() -> str:
    """保存した両方の指示を結合し、旧指示を監査用に残す。適用は migration ledger で一度だけ。"""
    return """
MERGE INTO rag_answer_prompts target
USING (
    SELECT 'raptor_summary_system' AS prompt_key,
        COALESCE(
            (SELECT content FROM rag_answer_prompts WHERE prompt_key = 'raptor_summary_system'),
            TO_CLOB('## 目的' || CHR(10) || CHR(10)
                || 'あなたは文書を階層的に要約するアシスタントです。')
        ) || CHR(10) || CHR(10) || COALESCE(
            (SELECT content FROM rag_answer_prompts WHERE prompt_key = 'raptor_summary'),
            TO_CLOB('## 目的' || CHR(10) || CHR(10)
                || '次のテキスト群の要点を簡潔な日本語で要約してください。')
        ) AS content
    FROM dual
    WHERE EXISTS (
        SELECT 1 FROM rag_answer_prompts
        WHERE prompt_key IN ('raptor_summary_system', 'raptor_summary')
    )
) source
ON (target.prompt_key = source.prompt_key)
WHEN MATCHED THEN UPDATE SET target.content = source.content, target.updated_at = SYSTIMESTAMP
WHEN NOT MATCHED THEN INSERT (prompt_key, content) VALUES (source.prompt_key, source.content);

UPDATE rag_answer_prompts
SET prompt_key = 'raptor_summary_archived'
WHERE prompt_key = 'raptor_summary';
""".strip()


def evaluation_calibration_migration_sql() -> str:
    """判定の校正の表（記入の集合・項目・判定・校正の結果）と索引を無ければ作る（#3578）。

    表と索引を足すだけでデータは変えない。DDL は正本
    （``oracle_evaluation_calibration_schema_sql``）から作る。
    """
    return _create_missing_objects_sql(oracle_evaluation_calibration_schema_sql(), "判定の校正")


def evaluation_job_calibration_kind_migration_sql() -> str:
    """品質評価の job の kind に判定の校正（``calibration``）を足す（#3578）。

    CHECK 制約を作り直すだけで行は変えない。表が無ければ何もしない（冪等）。
    """
    return """DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_EVALUATION_JOBS';
    IF v_count > 0 THEN
        SELECT COUNT(*) INTO v_count
        FROM user_constraints
        WHERE table_name = 'RAG_EVALUATION_JOBS'
          AND constraint_name = 'RAG_EVALUATION_JOBS_KIND_CK';
        IF v_count > 0 THEN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_evaluation_jobs DROP CONSTRAINT rag_evaluation_jobs_kind_ck';
        END IF;
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_evaluation_jobs ADD CONSTRAINT rag_evaluation_jobs_kind_ck '
            || 'CHECK (kind IN (''run'', ''calibration''))';
    END IF;
END;
/"""


# #2457 で改名したファイル準備の値(旧, 新)。JSON を文字列にして置き換える(値と、抽出の ID の入力の
# key の両方。引用符まで含めて書き、ほかの名前の一部を置き換えない)。
STORED_PREPROCESS_RENAMES_2457: tuple[tuple[str, str], ...] = (
    ('"csv_to_json"', '"csv_to_jsonl"'),
    ('"excel_to_json"', '"excel_to_jsonl"'),
    ('"csv_to_json_format"', '"csv_to_jsonl_format"'),
    ('"excel_to_json_format"', '"excel_to_jsonl_format"'),
)
# ファイル準備の値を持ちうる JSON の列(表, 列)。設定・レシピ・その記録だけで、抽出の本文は変えない。
STORED_PREPROCESS_COLUMNS_2457: tuple[tuple[str, str], ...] = (
    ("RAG_DOCUMENTS", "PROCESSING_CONFIG"),
    ("RAG_DOCUMENT_RECIPES", "PROCESSING_CONFIG"),
    ("RAG_KNOWLEDGE_BASES", "RETRIEVAL_CONFIG"),
    ("RAG_INGESTION_JOBS", "SETTINGS_OVERRIDES"),
    ("RAG_CHUNK_SETS", "RECIPE_SUBSET"),
    ("RAG_DOCUMENT_EXTRACTIONS", "RECIPE_SUBSET"),
)
# 文書レシピの処理設定の列(解析エンジンと分割符も書き換える)。
STORED_RECIPE_COLUMNS_2457: tuple[tuple[str, str], ...] = (
    ("RAG_DOCUMENTS", "PROCESSING_CONFIG"),
    ("RAG_DOCUMENT_RECIPES", "PROCESSING_CONFIG"),
)
# #2457 の前の固定分割符の既定(escape 表現)。
PREVIOUS_DEFAULT_CHUNK_DELIMITER_2457 = "\\n\\n"


def apply_stored_preprocess_renames_2457(text: str) -> str:
    """``STORED_PREPROCESS_RENAMES_2457`` を migration の SQL と同じ順に当てる(テストで照合)。"""
    for old, new in STORED_PREPROCESS_RENAMES_2457:
        text = text.replace(old, new)
    return text


def structured_records_jsonl_profiles_migration_sql() -> str:
    """ファイル準備 csv_to_json / excel_to_json の保存値を ``*_jsonl`` に移す(#2457)。

    - 設定・レシピ・取込ジョブ・その記録の JSON の ``"csv_to_json"`` / ``"excel_to_json"`` を
      ``*_jsonl`` に書き換える(抽出の ID の入力の key ``*_format`` も)。
    - 文書レシピでファイル準備が ``*_jsonl`` で、解析エンジンが Docling / Unstructured か未指定
      (global を継承)なら、解析エンジンを構造化レコード JSONL にする(Docling / Unstructured は
      構造化レコードの JSONL を受けなくなった)。分割の方式は書き換えない。
    - 文書レシピの分割方式が固定分割で分割符が未指定なら、旧い既定の ``\\n\\n`` を明示する
      (既定の分割符を記録の境界のマーカーに変えても、分割の結果を変えない)。
    表・列が無ければ何もしない。何度当てても同じ結果になる(冪等)。
    """
    targets = ", ".join(f"'{table}.{column}'" for table, column in STORED_PREPROCESS_COLUMNS_2457)
    recipe_targets = ", ".join(
        f"'{table}.{column}'" for table, column in STORED_RECIPE_COLUMNS_2457
    )
    replaces = "\n".join(
        f"            v_expr := 'REPLACE(' || v_expr || ', ''{old}'', ''{new}'')';"
        for old, new in STORED_PREPROCESS_RENAMES_2457
    )
    delimiter = PREVIOUS_DEFAULT_CHUNK_DELIMITER_2457
    return f"""DECLARE
    TYPE t_names IS TABLE OF VARCHAR2(261);
    v_targets t_names := t_names({targets});
    v_recipes t_names := t_names({recipe_targets});
    v_table   VARCHAR2(128);
    v_column  VARCHAR2(128);
    v_type    VARCHAR2(128);
    v_text    VARCHAR2(512);
    v_json    VARCHAR2(512);
    v_expr    VARCHAR2(32767);
    FUNCTION stored(p_expr VARCHAR2) RETURN VARCHAR2 IS
    BEGIN
        IF v_type = 'JSON' THEN
            RETURN p_expr;
        END IF;
        RETURN 'JSON_SERIALIZE(' || p_expr || ' RETURNING CLOB)';
    END;
BEGIN
    FOR i IN 1 .. v_targets.COUNT LOOP
        v_table := SUBSTR(v_targets(i), 1, INSTR(v_targets(i), '.') - 1);
        v_column := SUBSTR(v_targets(i), INSTR(v_targets(i), '.') + 1);
        SELECT MAX(data_type) INTO v_type
        FROM user_tab_columns
        WHERE table_name = v_table AND column_name = v_column;
        IF v_type IS NOT NULL THEN
            IF v_type = 'JSON' THEN
                v_text := 'JSON_SERIALIZE(' || v_column || ' RETURNING CLOB)';
            ELSE
                v_text := v_column;
            END IF;
            v_expr := v_text;
{replaces}
            IF v_type = 'JSON' THEN
                v_expr := 'JSON(' || v_expr || ')';
            END IF;
            EXECUTE IMMEDIATE
                'UPDATE ' || v_table || ' SET ' || v_column || ' = ' || v_expr
                || ' WHERE ' || v_text || ' LIKE ''%_to_json"%'''
                || ' OR ' || v_text || ' LIKE ''%_to_json_format"%''';
            COMMIT;
        END IF;
    END LOOP;
    FOR i IN 1 .. v_recipes.COUNT LOOP
        v_table := SUBSTR(v_recipes(i), 1, INSTR(v_recipes(i), '.') - 1);
        v_column := SUBSTR(v_recipes(i), INSTR(v_recipes(i), '.') + 1);
        SELECT MAX(data_type) INTO v_type
        FROM user_tab_columns
        WHERE table_name = v_table AND column_name = v_column;
        IF v_type IS NOT NULL THEN
            IF v_type = 'JSON' THEN
                v_json := v_column;
            ELSE
                v_json := 'JSON(' || v_column || ')';
            END IF;
            EXECUTE IMMEDIATE
                'UPDATE ' || v_table || ' SET ' || v_column || ' = '
                || stored('JSON_TRANSFORM(' || v_json
                || ', SET ''$.parser_adapter_backend'' = ''structured_records_jsonl'')')
                || ' WHERE ' || v_column || ' IS NOT NULL'
                || ' AND JSON_VALUE(' || v_json || ', ''$.preprocess_profile'')'
                || ' IN (''csv_to_jsonl'', ''excel_to_jsonl'')'
                || ' AND NVL(JSON_VALUE(' || v_json || ', ''$.parser_adapter_backend''),'
                || ' ''docling'') IN (''docling'', ''unstructured'')';
            EXECUTE IMMEDIATE
                'UPDATE ' || v_table || ' SET ' || v_column || ' = '
                || stored('JSON_TRANSFORM(' || v_json
                || ', SET ''$.chunk_delimiter'' = ''{delimiter}'')')
                || ' WHERE ' || v_column || ' IS NOT NULL'
                || ' AND JSON_VALUE(' || v_json || ', ''$.chunking_strategy'')'
                || ' = ''fixed_delimiter'''
                || ' AND JSON_VALUE(' || v_json || ', ''$.chunk_delimiter'') IS NULL';
            COMMIT;
        END IF;
    END LOOP;
END;
/"""


def document_cleanups_migration_sql() -> str:
    """文書の削除の後始末の記録の表（rag_document_cleanups）と索引を無ければ作る（#2452）。

    表と索引を足すだけでデータは変えない。DDL は正本
    （``oracle_document_cleanup_schema_sql``）から作る。
    """
    return _create_missing_objects_sql(
        oracle_document_cleanup_schema_sql(), "文書の削除の後始末の記録"
    )


def restore_document_classification_migration_sql() -> str:
    """取り消した migration ``20261016_002_document_effective_period`` を当てた DB の列を戻す。

    #2450。取り消した migration は ``rag_documents.classification`` を ``effective_period`` に
    改名して有効期間以外の key を消し、``rag_query_history.classification_filter`` を削除した。
    台帳には適用済みで残るので、ここで今の正本の形にそろえる（新しい DB・当てていない DB では
    何もしない）。

    - ``rag_documents.classification``・``rag_query_history.classification_filter`` が無ければ
      JSON の列を足す。
    - ``effective_period`` があれば、``classification`` が NULL の行にだけ、有効期間
      （``effective_from`` / ``effective_to``。YYYY-MM-DD の日付で、開始日 < 終了日）を写す。
      列の改名はしない。``effective_period`` の列と値は消さず、形の合わない値は写さない。
      取り消した migration が消した分類（大分類・中分類・小分類）と検索の履歴の分類の条件は DB に
      残っていないので戻らない（要るなら、そのときの書き出し・バックアップから戻す）。
    どの文も、表・列があるときだけ実行し、何度当てても同じ結果になる（冪等）。
    """
    period_date = (
        "TO_DATE(CASE WHEN REGEXP_LIKE(JSON_VALUE(effective_period, '$.{key}'), "
        "'^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$') "
        "THEN JSON_VALUE(effective_period, '$.{key}') END "
        "DEFAULT NULL ON CONVERSION ERROR, 'YYYY-MM-DD')"
    )
    effective_from = period_date.format(key="effective_from")
    effective_to = period_date.format(key="effective_to")
    return f"""DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_DOCUMENTS';
    IF v_count > 0 THEN
        SELECT COUNT(*) INTO v_count
        FROM user_tab_columns
        WHERE table_name = 'RAG_DOCUMENTS' AND column_name = 'CLASSIFICATION';
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE rag_documents ADD (classification JSON)';
        END IF;
        SELECT COUNT(*) INTO v_count
        FROM user_tab_columns
        WHERE table_name = 'RAG_DOCUMENTS' AND column_name = 'EFFECTIVE_PERIOD';
        IF v_count > 0 THEN
            EXECUTE IMMEDIATE q'[MERGE INTO rag_documents target
USING (
    SELECT document_id, period_from, period_to
    FROM (
        SELECT document_id,
            {effective_from} AS period_from,
            {effective_to} AS period_to,
            JSON_VALUE(effective_period, '$.effective_from') AS raw_from,
            JSON_VALUE(effective_period, '$.effective_to') AS raw_to
        FROM rag_documents
        WHERE effective_period IS NOT NULL AND classification IS NULL
    )
    WHERE (period_from IS NOT NULL OR period_to IS NOT NULL)
        AND (raw_from IS NULL OR period_from IS NOT NULL)
        AND (raw_to IS NULL OR period_to IS NOT NULL)
        AND (period_from IS NULL OR period_to IS NULL OR period_from < period_to)
) source
ON (target.document_id = source.document_id)
WHEN MATCHED THEN UPDATE SET target.classification = JSON_OBJECT(
    'effective_from' VALUE TO_CHAR(source.period_from, 'YYYY-MM-DD'),
    'effective_to' VALUE TO_CHAR(source.period_to, 'YYYY-MM-DD')
    ABSENT ON NULL RETURNING JSON)
WHERE target.classification IS NULL]';
        END IF;
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_QUERY_HISTORY';
    IF v_count > 0 THEN
        SELECT COUNT(*) INTO v_count
        FROM user_tab_columns
        WHERE table_name = 'RAG_QUERY_HISTORY' AND column_name = 'CLASSIFICATION_FILTER';
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE rag_query_history ADD (classification_filter JSON)';
        END IF;
    END IF;
END;
/"""


def support_guide_embeddings_migration_sql() -> str:
    """業務ガイドの質問の例の埋め込みの表（rag_support_guide_embeddings）を無ければ作る（#2398）。

    表を足すだけでデータは変えない。DDL は正本（``oracle_support_guide_embedding_schema_sql``）から
    作る。埋め込みは意味の照合を使うときに作るので、既存の公開の版の行は足さない。
    """
    return _create_missing_objects_sql(
        oracle_support_guide_embedding_schema_sql(), "業務ガイドの埋め込み"
    )


def message_attachments_migration_sql() -> str:
    """質問に添付したファイルの列（USER のメッセージの attachments_json）を無ければ足す（#2517）。

    列を足すだけでデータは変えない。この機能より前の質問は NULL（添付なし）。JSON 型の列は既存の
    index の隠し列と ORA-54015 になる（#1193）ため、JSON の文字列を CLOB で持つ。
    """
    return """DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_MESSAGES' AND column_name = 'ATTACHMENTS_JSON';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_messages ADD (attachments_json CLOB)';
    END IF;
END;
/"""


def answer_notice_column_migration_sql(table_name: str) -> str:
    """回答に付けた注意書きの列（answer_notice）を無ければ足す（#2405）。

    列を足すだけでデータは変えない。この機能より前の回答は NULL（注意書きを出していない）。
    """
    return f"""DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = '{table_name}' AND column_name = 'ANSWER_NOTICE';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE {table_name.lower()} ADD (answer_notice CLOB)';
    END IF;
END;
/"""


def support_guide_schema_v2_migration_sql() -> str:
    """業務ガイドの内容の形の版を 1 から 2 にする（#2395）。

    版 2 は資料の頁の範囲（``references[].page_ranges``）と手順の参照（``steps[].references``）
    を足しただけで、版 1 の内容はそのまま版 2 として読める。下書きと公開の版の
    ``schema_version`` だけを書き換え、ほかの値・内容の指紋の列は変えない（冪等。表が無ければ
    何もしない）。
    """
    updates = (
        ("RAG_SUPPORT_GUIDES", "rag_support_guides", "draft_json"),
        ("RAG_SUPPORT_GUIDE_REVISIONS", "rag_support_guide_revisions", "content_json"),
    )
    blocks = "\n".join(
        f"""    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = '{table}';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE q'[UPDATE {name} SET {column} = JSON_TRANSFORM({column}, """
        f"""SET '$.schema_version' = 2) WHERE JSON_VALUE({column}, '$.schema_version' """
        f"""RETURNING NUMBER) = 1]';
    END IF;"""
        for table, name, column in updates
    )
    return f"""DECLARE
    v_count NUMBER;
BEGIN
{blocks}
    COMMIT;
END;
/"""


def document_categories_migration_sql() -> str:
    """文書の分類の一覧の表（rag_document_categories）と索引を無ければ作る（#2131）。

    表と索引を足すだけでデータは変えない。DDL は正本
    （``oracle_document_category_schema_sql``）から作る。
    """
    return _create_missing_objects_sql(oracle_document_category_schema_sql(), "文書の分類の一覧")


def retire_evaluation_compare_migration_sql() -> str:
    """品質評価の設定比較（#2088 で削除）の名残を消す（#2099）。

    - 設定比較の job の行（kind <> 'run'）を消し、job の kind の CHECK を 'run' だけにする。
    - 設定比較だけが書いた列（rag_evaluation_jobs.current_experiment_id・
      rag_evaluation_runs.best_experiment_id）と、その索引（rag_evaluation_runs_best_experiment_idx）
      を消す。
    どの文も、表・列・索引・制約があるときだけ実行し、何度当てても同じ結果になる（冪等）。
    """
    return """DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_EVALUATION_JOBS';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE 'DELETE FROM rag_evaluation_jobs WHERE kind <> ''run''';
        SELECT COUNT(*) INTO v_count
        FROM user_constraints
        WHERE table_name = 'RAG_EVALUATION_JOBS'
          AND constraint_name = 'RAG_EVALUATION_JOBS_KIND_CK';
        IF v_count > 0 THEN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_evaluation_jobs DROP CONSTRAINT rag_evaluation_jobs_kind_ck';
        END IF;
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_evaluation_jobs ADD CONSTRAINT rag_evaluation_jobs_kind_ck '
            || 'CHECK (kind IN (''run''))';
        SELECT COUNT(*) INTO v_count
        FROM user_tab_columns
        WHERE table_name = 'RAG_EVALUATION_JOBS' AND column_name = 'CURRENT_EXPERIMENT_ID';
        IF v_count > 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE rag_evaluation_jobs DROP COLUMN current_experiment_id';
        END IF;
    END IF;
    SELECT COUNT(*) INTO v_count
    FROM user_indexes
    WHERE index_name = 'RAG_EVALUATION_RUNS_BEST_EXPERIMENT_IDX';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE 'DROP INDEX rag_evaluation_runs_best_experiment_idx';
    END IF;
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_EVALUATION_RUNS' AND column_name = 'BEST_EXPERIMENT_ID';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_evaluation_runs DROP COLUMN best_experiment_id';
    END IF;
END;
/"""


def run_history_migration_sql() -> str:
    """実行履歴の表（rag_run_history）と索引を無ければ作る（#1890）。

    表と索引を足すだけでデータは変えない。DDL は正本（``oracle_run_history_schema_sql``）から作る。
    """
    return _create_missing_objects_sql(oracle_run_history_schema_sql(), "実行履歴")


def run_history_conversation_index_migration_sql() -> str:
    """実行履歴の会話の索引（rag_run_history_conversation_idx）を無ければ作る（#1954）。

    会話の持ち主にも、持ち主の会話で system_admin が実行した行を見せる条件に使う。索引を足すだけで
    データは変えない。DDL は正本（``oracle_run_history_schema_sql``）の該当の文から作る。
    """
    statements = [
        statement
        for statement in split_sql_statements(oracle_run_history_schema_sql())
        if "_conversation_idx" in statement
    ]
    return _create_missing_objects_sql("\n".join(f"{item};" for item in statements), "実行履歴")


def message_clarification_migration_sql() -> str:
    """回答前に確認した内容の列（USER のメッセージ）を無ければ足す（#1839）。

    列を足すだけでデータは変えない。JSON 型の列を足すと既存の index の隠し列と ORA-54015 になる
    （#1193）ため、JSON の文字列を CLOB で持つ。
    """
    return """DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_MESSAGES' AND column_name = 'CLARIFICATION_JSON';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_messages ADD (clarification_json CLOB)';
    END IF;
END;
/"""


def ingestion_job_progress_migration_sql() -> str:
    """取込 job の抽出の段の進捗の列（rag_ingestion_jobs.progress_json）を無ければ足す（#1999）。

    列を足すだけでデータは変えない。JSON 型の列は既存の index の隠し列と ORA-54015 になりうる
    （#1193）ため、JSON の文字列を CLOB で持つ。
    """
    return """DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_INGESTION_JOBS' AND column_name = 'PROGRESS_JSON';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_ingestion_jobs ADD (progress_json CLOB)';
    END IF;
END;
/"""


def entity_layer_migration_sql() -> str:
    """実体の層の表(rag_entities / rag_entity_aliases /
    rag_entity_chunks)と索引を無ければ作る(#1362)。

    表と索引を足すだけでデータは変えない。DDL は正本(``oracle_entity_schema_sql``)から作る。
    """
    return _create_missing_objects_sql(oracle_entity_schema_sql(), "実体の層")


def section_chunk_map_migration_sql() -> str:
    """章節と chunk の対応の表(rag_chunk_set_sections / rag_section_chunks)と索引を無ければ
    作る(#1438)。

    表と索引を足すだけでデータは変えない。DDL は正本(``oracle_section_map_schema_sql``)から作る。
    対応は取込の後・章節の保存の後と、MCP で章節を読むときに無ければ作る。
    """
    return _create_missing_objects_sql(oracle_section_map_schema_sql(), "章節と chunk の対応")


def _create_missing_objects_sql(ddl: str, label: str) -> str:
    """表・索引の DDL を、無いものだけを作る PL/SQL にする。"""
    blocks: list[str] = []
    for statement in split_sql_statements(ddl):
        match = _CREATE_OBJECT_PATTERN.match(statement)
        if match is None:
            raise ValueError(f"{label}の DDL に表・索引以外の文があります: {statement[:60]}")
        kind, name = match.group(1).upper(), match.group(2).upper()
        dictionary = (
            "user_tables WHERE table_name" if kind == "TABLE" else "user_indexes WHERE index_name"
        )
        literal_lines = _ddl_literal_lines(statement.replace("'", "''"))
        lines = "\n".join(
            f"            {'|| ' if index else ''}'{line}'"
            for index, line in enumerate(literal_lines)
        )
        blocks.append(
            f"""    SELECT COUNT(*) INTO v_count FROM {dictionary} = '{name}';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
{lines};
    END IF;"""
        )
    body = "\n".join(blocks)
    return f"""DECLARE
    v_count NUMBER;
BEGIN
{body}
END;
/"""


_CREATE_OBJECT_PATTERN = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX)\s+([A-Za-z0-9_]+)", re.IGNORECASE
)


def _ddl_literal_lines(statement: str) -> list[str]:
    """DDL を EXECUTE IMMEDIATE の文字列の行にする(行の間は空白 1 つでつなぐ)。"""
    lines = [line.strip() for line in statement.splitlines() if line.strip()]
    return [line + " " if index < len(lines) - 1 else line for index, line in enumerate(lines)]


def document_superseded_migration_sql() -> str:
    """文書の版(置き換えた新しい版の文書と、置き換えた日時)の列を無ければ足す(#1248)。

    列を足すだけでデータは変えない。自己参照の FK は付けない(存在・循環はアプリで検証する)。
    """
    columns = (
        ("SUPERSEDED_BY_DOCUMENT_ID", "superseded_by_document_id VARCHAR2(64)"),
        ("SUPERSEDED_AT", "superseded_at TIMESTAMP WITH TIME ZONE"),
    )
    column_blocks = "\n".join(
        f"""    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_DOCUMENTS' AND column_name = '{name}';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_documents ADD ({definition})';
    END IF;
"""
        for name, definition in columns
    )
    return f"""DECLARE
    v_count NUMBER;
BEGIN
{column_blocks}END;
/"""


def message_answer_runs_migration_sql() -> str:
    """作成中の回答の段階・プロセス・heartbeat の列と、停止（CANCELLED）の状態を足す（#1175）。

    既存の rag_messages は created_at を含む index を 2 つ持ち、どちらも同じ式
    SYS_EXTRACT_UTC("CREATED_AT") の隠し列を作る。その表に JSON の列を足すと ORA-54015 になるため、
    JSON の列が無いときだけ conversation_created_idx を一時的に消し、列を足してから
    同じ定義で作り直す（#1193）。
    """
    columns = (
        ("LEASE_OWNER", "lease_owner VARCHAR2(128)"),
        ("HEARTBEAT_AT", "heartbeat_at TIMESTAMP WITH TIME ZONE"),
    )
    column_blocks = "\n".join(
        f"""    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_MESSAGES' AND column_name = '{name}';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_messages ADD ({definition})';
    END IF;
"""
        for name, definition in columns
    )
    return f"""DECLARE
    v_count NUMBER;
    v_index_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_MESSAGES' AND column_name = 'PROGRESS_JSON';
    IF v_count = 0 THEN
        SELECT COUNT(*) INTO v_index_count
        FROM user_indexes
        WHERE index_name = 'RAG_MESSAGES_CONVERSATION_CREATED_IDX';
        IF v_index_count > 0 THEN
            EXECUTE IMMEDIATE 'DROP INDEX rag_messages_conversation_created_idx';
        END IF;
        EXECUTE IMMEDIATE 'ALTER TABLE rag_messages ADD (progress_json JSON)';
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_messages_conversation_created_idx '
            || 'ON rag_messages (conversation_id, created_at)';
    END IF;
{column_blocks}
    SELECT COUNT(*) INTO v_count
    FROM user_constraints
    WHERE table_name = 'RAG_MESSAGES'
      AND constraint_name = 'RAG_MESSAGES_STATUS_CK';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_messages DROP CONSTRAINT rag_messages_status_ck';
    END IF;
    EXECUTE IMMEDIATE
        'ALTER TABLE rag_messages ADD CONSTRAINT rag_messages_status_ck CHECK '
        || '(status IN (''STREAMING'', ''COMPLETE'', ''ERROR'', ''CANCELLED''))';
END;
/"""


def oracle_schema_migration_sql(
    sections: Sequence[OracleSchemaSection] | None = None,
) -> str:
    """SQLcl 等で適用できる Oracle schema migration artifact を返す。"""
    resolved_sections = list(sections or oracle_schema_migration_sections())
    return (
        "\n\n".join(
            f"-- migration: {section.name}\n{section.sql.rstrip()}"
            + ("" if section.sql.rstrip().endswith((";", "/")) else ";")
            for section in resolved_sections
        )
        + "\n"
    )


def oracle_schema_manifest(sections: Sequence[OracleSchemaSection] | None = None) -> dict[str, Any]:
    """schema artifact の監査用 manifest を返す。"""
    resolved_sections = list(sections or oracle_schema_sections())
    sql = oracle_schema_sql(resolved_sections)
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "vector_contract": VECTOR_CONTRACT,
        "vector_index": VECTOR_INDEX_CONTRACT,
        "sha256": _sha256(sql),
        "statement_count": len(split_sql_statements(sql)),
        "sections": [
            {
                "name": section.name,
                "table_name": section.table_name,
                "sha256": _sha256(section.sql),
                "statement_count": len(split_sql_statements(section.sql)),
            }
            for section in resolved_sections
        ],
    }


def oracle_schema_migration_manifest(
    sections: Sequence[OracleSchemaSection] | None = None,
) -> dict[str, Any]:
    """schema migration artifact の監査用 manifest を返す。"""
    resolved_sections = list(sections or oracle_schema_migration_sections())
    sql = oracle_schema_migration_sql(resolved_sections)
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "migration",
        "migration_artifact_version": MIGRATION_ARTIFACT_VERSION,
        "sha256": _sha256(sql),
        "statement_count": len(split_sql_statements(sql)),
        "migrations": [
            {
                "name": section.name,
                "table_name": section.table_name,
                "sha256": _sha256(section.sql),
                "statement_count": len(split_sql_statements(section.sql)),
                # データを消す migration（#619）。SQLcl などで直接当てる前に書き出しを確かめる。
                "destructive": section.destructive,
            }
            for section in resolved_sections
        ],
    }


def split_sql_statements(sql: str) -> list[str]:
    """SQL artifact を statement ごとに分割する。

    通常 DDL はセミコロン終端で分割する。SQLcl 向け PL/SQL block は
    行単独の `/` までを 1 statement として扱う。
    """
    statements: list[str] = []
    current: list[str] = []
    in_plsql_block = False
    for line in sql.splitlines():
        if not line.strip():
            continue
        stripped = line.strip()
        if (
            not in_plsql_block
            and not _current_statement_has_sql(current)
            and _starts_plsql_block(stripped)
        ):
            in_plsql_block = True
        if in_plsql_block and stripped == "/":
            statement = "\n".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            in_plsql_block = False
            continue
        current.append(line.rstrip())
        if not in_plsql_block and line.rstrip().endswith(";"):
            statement = "\n".join(current).strip()
            statements.append(statement.removesuffix(";").rstrip())
            current = []
    if current:
        statements.append("\n".join(current).strip())
    return statements


# #599 で改名した、保存値の中の名前(旧, 新)。JSON を文字列にしてこの順に置き換える
# (長い名前を先にし、短い名前が長い名前の一部を置き換えないようにする)。
# JSON_SERIALIZE は空白を入れないので、`"key":"value"` の形で key と値の組を書ける。
STORED_NAME_RENAMES_599: tuple[tuple[str, str], ...] = (
    # 分割方式の値
    ("docrag_small_to_big", "small_to_big"),
    # Settings の属性名(取込ジョブの上書き・chunk set の recipe の記録)
    ("rag_docrag_table_child_target_chars", "rag_chunk_table_child_target_chars"),
    ("rag_docrag_child_target_chars", "rag_chunk_child_target_chars"),
    ("rag_docrag_parent_target_chars", "rag_chunk_parent_target_chars"),
    ("rag_docrag_parent_max_children", "rag_chunk_parent_max_children"),
    ("rag_docrag_parent_max_pages", "rag_chunk_parent_max_pages"),
    ("rag_docrag_answer_vision_enabled", "rag_answer_vision_enabled"),
    ("rag_docrag_history_rewrite_enabled", "rag_history_rewrite_enabled"),
    ("rag_docrag_screen_linking_enabled", "rag_screen_linking_enabled"),
    ("rag_docrag_neighbor_child_count", "rag_neighbor_child_count"),
    ("rag_docrag_query_strategy", "rag_query_strategy"),
    ("rag_docrag_rerank_enabled", "rag_rerank_enabled"),
    ("rag_docrag_answer_flow", "rag_answer_flow"),
    ("rag_docrag_profile", "rag_answer_profile"),
    # 文書・レシピの処理設定、KB の構築設定、検索・回答プロファイルの検索・回答設定の key
    ("docrag_table_child_target_chars", "chunk_table_child_target_chars"),
    ("docrag_child_target_chars", "chunk_child_target_chars"),
    ("docrag_parent_target_chars", "chunk_parent_target_chars"),
    ("docrag_parent_max_children", "chunk_parent_max_children"),
    ("docrag_parent_max_pages", "chunk_parent_max_pages"),
    ("docrag_screen_linking_enabled", "screen_linking_enabled"),
    ("docrag_neighbor_child_count", "neighbor_child_count"),
    ("docrag_query_strategy", "query_strategy"),
    ("docrag_rerank_enabled", "rerank_enabled"),
    ("docrag_answer_flow", "answer_flow"),
    # 親子階層の chunk の metadata
    ("docrag_first_page_context_json", "first_page_context_json"),
    ("docrag_source_record_refs_json", "source_record_refs_json"),
    ("docrag_source_seq_ranges_json", "source_seq_ranges_json"),
    ("docrag_metadata_json", "engine_metadata_json"),
    ("docrag_search_text", "engine_search_text"),
    ("docrag_parent_text", "parent_text"),
    ("docrag_chunk_seq", "engine_chunk_seq"),
    ("docrag_chunk_id", "engine_chunk_id"),
    ("docrag_model_used", "evidence_model_used"),
    ("docrag_role", "evidence_role"),
    ("docrag_parent", "small_to_big_parent"),
    ("docling_docrag", "docling_layout"),
    ("docrag_instruction_callout", "instruction_callout"),
    # Docling の解析結果(parser_artifacts)と、縮退の理由
    ("docrag_layout_records", "layout_records"),
    ("docrag_layout_missing", "layout_missing"),
    ("docrag_layout", "layout_records"),
    # 派生情報レイヤーの指紋の入力
    ("docrag_chunk_contract", "chunk_metadata_contract"),
    # 回答の経路・段の名前・診断(品質評価の結果・回答の記録)
    ('"docrag_retrieval_only"', '"retrieval_only"'),
    ('"docrag_grounded"', '"grounded"'),
    ('"docrag_history_rewrite"', '"history_rewrite"'),
    ('"docrag_answer"', '"answer"'),
    ('"retrieval_strategy":"docrag"', '"retrieval_strategy":"hybrid"'),
    ('"answer_engine":"docrag"', '"answer_engine":"grounded"'),
    ('"docrag":', '"answer":'),
)

# 保存値に #599 の前の名前を持ちうる JSON の列(表, 列)。
STORED_NAME_COLUMNS_599: tuple[tuple[str, str], ...] = (
    ("RAG_DOCUMENTS", "PROCESSING_CONFIG"),
    ("RAG_DOCUMENTS", "EXTRACTION"),
    ("RAG_DOCUMENT_RECIPES", "PROCESSING_CONFIG"),
    ("RAG_KNOWLEDGE_BASES", "RETRIEVAL_CONFIG"),
    ("RAG_BUSINESS_VIEWS", "VIEW_CONFIG"),
    ("RAG_INGESTION_JOBS", "SETTINGS_OVERRIDES"),
    ("RAG_INGESTION_JOBS", "QUALITY_WARNINGS"),
    ("RAG_CHUNK_SETS", "RECIPE_SUBSET"),
    ("RAG_CHUNK_SETS", "METRICS_JSON"),
    ("RAG_CHUNK_SETS", "FIRST_PAGE_CONTEXT"),
    ("RAG_DOCUMENT_EXTRACTIONS", "RECIPE_SUBSET"),
    ("RAG_DOCUMENT_EXTRACTIONS", "EXTRACTION_JSON"),
    ("RAG_DOCUMENT_EXTRACTIONS", "METRICS_JSON"),
    ("RAG_ARTIFACT_LAYERS", "INPUT_FINGERPRINT"),
    ("RAG_ARTIFACT_LAYERS", "METRICS_JSON"),
    ("RAG_CHUNKS", "METADATA_JSON"),
    ("RAG_ANSWER_RECORDS", "CITATIONS_JSON"),
    ("RAG_ANSWER_RECORDS", "DIAGNOSTICS_JSON"),
    ("RAG_ANSWER_RECORDS", "EVALUATION_INPUT_JSON"),
    ("RAG_ANSWER_RECORDS", "EVALUATION_JSON"),
    ("RAG_MESSAGES", "CITATIONS_JSON"),
    ("RAG_FEEDBACK_DETAILS", "CITATIONS_JSON"),
    ("RAG_EVALUATION_RUNS", "REQUEST_JSON"),
    ("RAG_EVALUATION_RUNS", "RESULT_JSON"),
    ("RAG_EVALUATION_JOBS", "RESULT_JSON"),
)


def apply_stored_name_renames_599(text: str) -> str:
    """``STORED_NAME_RENAMES_599`` を migration の SQL と同じ順に当てる(テストで照合する)。"""
    for old, new in STORED_NAME_RENAMES_599:
        text = text.replace(old, new)
    return text


def _current_statement_has_sql(lines: Sequence[str]) -> bool:
    return any(line.strip() and not line.strip().startswith("--") for line in lines)


def _starts_plsql_block(stripped_line: str) -> bool:
    upper = stripped_line.upper()
    return upper == "DECLARE" or upper == "BEGIN" or upper.startswith(("DECLARE ", "BEGIN "))


@with_system_access
def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.migration:
        sections = oracle_schema_migration_sections()
        sql = oracle_schema_migration_sql(sections)
        manifest = oracle_schema_migration_manifest(sections)
    else:
        sections = oracle_schema_sections()
        sql = oracle_schema_sql(sections)
        manifest = oracle_schema_manifest(sections)

    if args.manifest_only:
        _write_json(manifest, args.manifest_output)
        return 0

    _write_text(sql, args.output)
    if args.manifest_output is not None:
        _write_json(manifest, args.manifest_output)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag-oracle-schema",
        description="Oracle AI Database 用 RAG schema SQL と監査 manifest を生成します。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="SQL artifact の保存先。未指定なら stdout に出力します。",
    )
    parser.add_argument(
        "--manifest-output",
        type=Path,
        help="manifest JSON の保存先。--manifest-only では未指定なら stdout に出力します。",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="SQL ではなく manifest JSON だけを出力します。",
    )
    parser.add_argument(
        "--migration",
        action="store_true",
        help="新規 schema DDL ではなく既存 schema 用 migration SQL を出力します。",
    )
    return parser


def _write_text(content: str, output_path: Path | None) -> None:
    if output_path is None:
        print(content, end="")
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")


def _write_json(payload: dict[str, Any], output_path: Path | None) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _write_text(serialized, output_path)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def answer_prompt_placeholders_migration_sql() -> str:
    """編集済みの指示を保ち、回答用の placeholder だけを改名する。"""
    return """
UPDATE RAG_ANSWER_PROMPTS
SET CONTENT = REPLACE(REPLACE(CONTENT,
    '{{images}}', '{{answer_context}}'),
    '{{image_metadata}}', '{{evidence_metadata}}')
WHERE PROMPT_KEY = 'vlm_answer'
""".strip()


def cited_evidence_migration_sql() -> str:
    """保存済み評価入力の引用キーを変更し、画像だけの ID は変更しない。"""
    return """
DECLARE
    v_conflicts NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_conflicts FROM rag_answer_records
    WHERE (JSON_EXISTS(evaluation_input_json, '$.used_images')
           AND JSON_EXISTS(evaluation_input_json, '$.cited_evidence'))
       OR (JSON_EXISTS(evaluation_input_json, '$.used_image_ids')
           AND JSON_EXISTS(evaluation_input_json, '$.cited_evidence_ids'))
       OR JSON_EXISTS(evaluation_input_json, '$.used_images[*].evidence_id');
    IF v_conflicts > 0 THEN
        RAISE_APPLICATION_ERROR(-20001,
            '引用証拠の新旧キーが重複しています。重複を解消してください。');
    END IF;
    UPDATE rag_answer_records
    SET evaluation_input_json = JSON_TRANSFORM(
        evaluation_input_json,
        RENAME '$.used_images' = 'cited_evidence',
        RENAME '$.used_image_ids' = 'cited_evidence_ids',
        RENAME '$.cited_evidence[*].image_id' = 'evidence_id'
    )
    WHERE JSON_EXISTS(evaluation_input_json, '$.used_images')
       OR JSON_EXISTS(evaluation_input_json, '$.used_image_ids');
END;
/
""".strip()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
