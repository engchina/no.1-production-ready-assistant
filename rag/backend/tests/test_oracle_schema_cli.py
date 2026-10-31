"""Oracle schema artifact CLI のテスト。"""

import hashlib
import json
from pathlib import Path

from pytest import CaptureFixture

from app.rag import oracle_schema
from app.schemas.knowledge_base import DEFAULT_KNOWLEDGE_BASE_DESCRIPTION


def test_oracle_schema_sql_contains_required_rag_tables() -> None:
    """RAG 本番運用に必要な Oracle table / vector 契約を artifact に含める。"""
    sql = oracle_schema.oracle_schema_sql()

    assert "-- section: system_schema_control" in sql
    assert "CREATE TABLE rag_schema_operations" in sql
    assert "CREATE TABLE rag_schema_migrations" in sql
    assert "-- section: documents" in sql
    assert "CREATE TABLE rag_documents" in sql
    assert "-- section: knowledge_bases" in sql
    assert "CREATE TABLE rag_knowledge_bases" in sql
    assert "CREATE TABLE rag_document_knowledge_bases" in sql
    assert "extraction_fields     JSON," in sql
    assert "-- section: search_answer_profiles" in sql
    assert "CREATE TABLE rag_search_answer_profiles" in sql
    # 旧 standard の回答エンジンだけが使っていた表は base schema から外した（#596）。
    assert "CREATE TABLE rag_prompt_versions" not in sql
    assert "CREATE TABLE rag_generation_settings" not in sql
    assert "CREATE TABLE rag_agent_memories" not in sql
    assert "rag_agent_memories_embedding_hnsw_idx" not in sql
    # 回答の記録の answer_engine の列は履歴で使うため残す（#596）。
    assert "answer_engine       VARCHAR2(32) NOT NULL" in sql
    assert "-- section: ingestion_jobs" in sql
    assert "CREATE TABLE rag_ingestion_jobs" in sql
    assert "lease_owner      VARCHAR2(128)," in sql
    assert "heartbeat_at     TIMESTAMP WITH TIME ZONE," in sql
    assert "CREATE INDEX rag_ingestion_jobs_lease_idx" in sql
    assert "-- section: ingestion_segments" in sql
    assert "CREATE TABLE rag_ingestion_segments" in sql
    assert "CREATE TABLE rag_chunks" in sql
    assert "embedding       VECTOR(1536, FLOAT32)" in sql
    assert "chunk_set_id    VARCHAR2(64)" in sql
    assert "CREATE VECTOR INDEX rag_chunks_embedding_hnsw_idx" in sql
    assert "CTX_DDL.CREATE_PREFERENCE('RAG_TEXT_WORLD_LEXER', 'WORLD_LEXER')" in sql
    assert "CTX_DDL.CREATE_STOPLIST('RAG_TEXT_STOPLIST', 'BASIC_STOPLIST')" in sql
    assert (
        "PARAMETERS ('LEXER RAG_TEXT_WORLD_LEXER STOPLIST RAG_TEXT_STOPLIST SYNC (ON COMMIT)')"
        in sql
    )
    assert "CREATE INDEX rag_chunks_chunk_set_idx" in sql
    assert "TYPE HNSW" in sql
    # 新しい環境の索引は既定の検索精度「高精度」の推奨ビルドで作る(#1990)。
    assert "WITH TARGET ACCURACY 98" in sql
    assert "NEIGHBORS 48" in sql
    assert "EFCONSTRUCTION 800" in sql
    assert "-- section: chunk_sets" in sql
    assert "CREATE TABLE rag_chunk_sets" in sql
    assert "extraction_recipe_id VARCHAR2(64)" in sql
    assert "CREATE TABLE rag_document_extractions" in sql
    assert sql.count("CREATE TABLE rag_document_extractions") == 1
    assert "extraction_id   VARCHAR2(64) PRIMARY KEY" not in sql
    assert "CREATE TABLE rag_artifact_layers" in sql
    assert "input_fingerprint   JSON" in sql
    # 3 層モデル: per-KB binding 表は base schema から退役済み(membership + is_serving に一本化)。
    assert "CREATE TABLE rag_kb_chunk_set_bindings" not in sql
    assert "rag_chunk_sets_document_fk" in sql
    assert "CREATE TABLE rag_search_audit" in sql
    assert "memory_plan_id        VARCHAR2(32)" in sql
    assert "resolver_rejected_count NUMBER(10) DEFAULT 0 NOT NULL" in sql
    assert "CREATE TABLE rag_ingestion_audit" in sql
    assert "parser_backend         VARCHAR2(80)" in sql
    assert "segment_count          NUMBER(10) DEFAULT 0 NOT NULL" in sql
    assert "failed_segment_count   NUMBER(10) DEFAULT 0 NOT NULL" in sql
    assert "-- section: knowledge_graph" in sql
    assert "CREATE TABLE rag_graph_entities" in sql
    assert "-- section: citation_feedback" in sql
    assert "CREATE TABLE rag_citation_feedback" in sql
    assert "-- section: feedback_details" in sql
    assert "CREATE TABLE rag_feedback_details" in sql
    assert "CREATE INDEX rag_feedback_details_text_idx" in sql
    assert "-- section: evaluation_artifacts" in sql
    assert "CREATE TABLE rag_evaluation_runs" in sql
    assert "result_sha256     CHAR(64) NOT NULL" in sql
    assert "SELECT AI" not in sql.upper()


def test_oracle_schema_manifest_is_deterministic() -> None:
    """manifest は時刻を入れず、SQL の hash / statement 数を検証できる形にする。"""
    sql = oracle_schema.oracle_schema_sql()
    manifest = oracle_schema.oracle_schema_manifest()

    assert manifest == oracle_schema.oracle_schema_manifest()
    assert "generated_at" not in manifest
    assert manifest["schema_name"] == "production-ready-rag-oracle-26ai"
    assert manifest["schema_version"] == "3"
    assert manifest["vector_contract"] == "VECTOR(1536, FLOAT32)"
    assert manifest["vector_index"] == {
        "distance": "COSINE",
        "efconstruction": 800,
        "neighbors": 48,
        "target_accuracy": 98,
        "type": "HNSW",
    }
    assert manifest["sha256"] == hashlib.sha256(sql.encode("utf-8")).hexdigest()
    assert manifest["statement_count"] == len(oracle_schema.split_sql_statements(sql))
    assert [section["name"] for section in manifest["sections"]] == [
        "system_schema_control",
        "language_pack_releases",
        "documents",
        "document_recipes",
        "knowledge_bases",
        "search_answer_profiles",
        "answer_records",
        "answer_prompts",
        "run_history",
        "document_categories",
        "document_cleanups",
        "search_answer_profile_knowledge",
        "support_guides",
        "support_guide_revisions",
        "support_guide_embeddings",
        "document_sections",
        "conversations",
        "messages",
        "ingestion_jobs",
        "ingestion_segments",
        "chunks",
        "chunk_sets",
        "search_audit",
        "ingestion_audit",
        "knowledge_graph",
        "entities",
        "section_map",
        "citation_feedback",
        "feedback_details",
        "evaluation_artifacts",
        "evaluation_jobs",
        "evaluation_calibration",
        "role_access",
    ]
    assert all(section["statement_count"] > 0 for section in manifest["sections"])


def test_oracle_schema_migration_sql_adds_ingestion_job_attempt_counters() -> None:
    """migration artifact は旧 ingestion queue table を現行 DDL 契約へ寄せる。"""
    sql = oracle_schema.oracle_schema_migration_sql()
    statements = oracle_schema.split_sql_statements(sql)

    assert "-- migration: 20260615_001_ingestion_jobs_attempt_counters" in sql
    assert "FROM user_tab_columns" in sql
    assert "column_name = 'ATTEMPT_COUNT'" in sql
    assert "column_name = 'MAX_ATTEMPTS'" in sql
    assert "ALTER TABLE rag_ingestion_jobs ADD" in sql
    assert "(attempt_count NUMBER(5) DEFAULT 0 NOT NULL)" in sql
    assert "UPDATE rag_ingestion_jobs SET attempt_count = 0" in sql
    assert "WHERE attempt_count IS NULL" in sql
    assert "ALTER TABLE rag_ingestion_jobs MODIFY" in sql
    assert "ALTER TABLE rag_ingestion_jobs ADD" in sql
    assert "(max_attempts NUMBER(5) DEFAULT 3 NOT NULL)" in sql
    assert "UPDATE rag_ingestion_jobs SET max_attempts = 3" in sql
    assert "WHERE max_attempts IS NULL" in sql
    assert "ALTER TABLE rag_ingestion_jobs MODIFY" in sql
    assert "DROP CONSTRAINT" in sql
    assert "rag_ingestion_jobs_attempts_ck" in sql
    assert "CHECK" in sql
    assert "(attempt_count >= 0 AND max_attempts >= 1)" in sql
    assert "-- migration: 20260616_001_search_audit_search_mode" in sql
    assert "table_name = 'RAG_SEARCH_AUDIT'" in sql
    assert "column_name = 'MODE'" in sql
    assert "column_name = 'SEARCH_MODE'" in sql
    assert "RENAME COLUMN mode TO search_mode" in sql
    assert "rag_search_audit_search_mode_ck" in sql
    assert "(search_mode IN (''hybrid'', ''vector'', ''keyword''))" in sql
    assert "-- migration: 20260616_002_evaluation_runs_result_sha256" in sql
    assert "table_name = 'RAG_EVALUATION_RUNS'" in sql
    assert "column_name = 'RESULT_SHA256'" in sql
    assert "rag_evaluation_runs_result_hash_idx" in sql
    assert "-- migration: 20260616_003_ingestion_jobs_cancelled_status" in sql
    assert "rag_ingestion_jobs_status_ck" in sql
    assert "''CANCELLED''" in sql
    assert "-- migration: 20260616_004_ingestion_segments" in sql
    assert "CREATE TABLE rag_ingestion_segments" in sql
    assert "RAG_INGESTION_SEGMENTS_DOCUMENT_STATUS_IDX" in sql
    assert "-- migration: 20260616_005_search_audit_memory_engineering" in sql
    assert "column_name = p_column_name" in sql
    assert "MEMORY_PLAN_ID" in sql
    assert "RESOLVER_REJECTED_COUNT" in sql
    assert "AGENT_MEMORY_RETRIEVED_COUNT" in sql
    assert "-- migration: 20260616_006_agent_memories" in sql
    assert "CREATE TABLE rag_agent_memories" in sql
    assert "ROLE_ID_HASH" in sql
    assert "CREATE VECTOR INDEX rag_agent_memories_embedding_hnsw_idx" in sql
    assert "-- migration: 20260617_001_ingestion_audit_file_processing_metrics" in sql
    assert "table_name = 'RAG_INGESTION_AUDIT'" in sql
    assert "PARSER_BACKEND" in sql
    assert "PARSER_PROFILE" in sql
    assert "SEGMENT_COUNT" in sql
    assert "FAILED_SEGMENT_COUNT" in sql
    assert "rag_ingestion_audit_parser_created_idx" in sql
    assert "-- migration: 20260617_002_search_audit_adaptive_context" in sql
    assert "table_name = 'RAG_SEARCH_AUDIT'" in sql
    assert "CONTEXT_ADAPTIVE_EXPANDED_COUNT" in sql
    assert "-- migration: 20260617_003_search_audit_dependency_context" in sql
    assert "CONTEXT_DEPENDENCY_PROMOTED_COUNT" in sql
    assert "-- migration: 20260618_001_documents_review_status" in sql
    assert "rag_documents_status_ck" in sql
    assert "''REVIEW''" in sql
    assert "''CHUNKING''" in sql
    assert "''CHUNKED''" in sql
    assert "''INDEXING''" in sql
    assert "-- migration: 20260618_002_ingestion_jobs_phase" in sql
    assert "rag_ingestion_jobs_phase_ck" in sql
    assert "ALTER TABLE rag_ingestion_jobs DROP CONSTRAINT" in sql
    assert "(phase IN (''PREPROCESS'', ''EXTRACT'', ''CHUNK'', ''INDEX''))" in sql
    assert "-- migration: 20260619_001_business_views" in sql
    assert "table_name = 'RAG_BUSINESS_VIEWS'" in sql
    assert "rag_business_views_status_ck" in sql
    assert "-- migration: 20260621_001_chunk_sets" in sql
    assert "CREATE TABLE rag_chunk_sets" in sql
    assert "RAG_DOCUMENT_EXTRACTIONS_DOCUMENT_IDX" in sql
    assert "(status IN (''INGESTING'', ''CHUNKED'', ''INDEXED'', ''ERROR''))" in sql
    assert "RAG_DOCUMENT_EXTRACTIONS" in sql
    assert "RAG_ARTIFACT_LAYERS" in sql
    assert "CREATE TABLE rag_kb_chunk_set_bindings" in sql
    assert "column_name = 'EXTRACTION_RECIPE_ID'" in sql
    assert "column_name = 'CHUNK_SET_ID'" in sql
    assert "ALTER TABLE rag_chunks ADD (chunk_set_id VARCHAR2(64))" in sql
    assert "-- migration: 20260621_002_document_extractions" in sql
    assert "CREATE TABLE rag_document_extractions" in sql
    assert "ALTER TABLE rag_chunk_sets ADD (extraction_id VARCHAR2(64))" in sql
    assert "-- migration: 20260623_001_nullable_chunk_embeddings" in sql
    assert "ALTER TABLE rag_chunks MODIFY (embedding NULL)" in sql
    assert "-- migration: 20260625_001_chunks_text_world_lexer" in sql
    assert "CTX_DDL.CREATE_PREFERENCE('RAG_TEXT_WORLD_LEXER', 'WORLD_LEXER')" in sql
    assert "CTX_DDL.CREATE_STOPLIST('RAG_TEXT_STOPLIST', 'BASIC_STOPLIST')" in sql
    assert "DROP INDEX rag_chunks_text_idx" in sql
    assert "FROM ctx_user_index_objects" in sql
    assert "ixo_object = 'WORLD_LEXER'" in sql
    assert "'CREATE INDEX rag_chunks_text_idx '" in sql
    assert "|| 'ON rag_chunks (chunk_text) '" in sql
    assert "|| 'INDEXTYPE IS CTXSYS.CONTEXT '" in sql
    assert (
        "|| 'PARAMETERS (''LEXER RAG_TEXT_WORLD_LEXER STOPLIST RAG_TEXT_STOPLIST "
        "SYNC (ON COMMIT)'')'" in sql
    )
    assert "-- migration: 20260625_002_preprocess_artifact" in sql
    assert "column_name = 'PREPROCESS_ARTIFACT'" in sql
    assert "ALTER TABLE rag_documents ADD (preprocess_artifact JSON)" in sql
    assert "''PREPROCESSING''" in sql
    assert "-- migration: 20260627_001_documents_preprocessed_status" in sql
    assert "''PREPROCESSED''" in sql
    assert "-- migration: 20260629_001_chunk_sets_serving" in sql
    assert "-- migration: 20260629_002_drop_kb_chunk_set_bindings" in sql
    assert "DROP TABLE rag_kb_chunk_set_bindings" in sql
    assert "-- migration: 20260629_003_ingestion_jobs_settings_overrides" in sql
    assert "ALTER TABLE rag_ingestion_jobs ADD (settings_overrides JSON)" in sql
    assert "-- migration: 20260629_004_documents_processing_config" in sql
    assert "ALTER TABLE rag_documents ADD (processing_config JSON)" in sql
    assert "-- migration: 20260630_001_default_knowledge_base_name" in sql
    assert "WHERE name = '既定ナレッジベース'" in sql
    assert "name = 'DEFAULT'" in sql
    assert "-- migration: 20260630_002_default_business_view" in sql
    assert "JSON_MERGEPATCH" in sql
    assert "JSON_ARRAY(kb.knowledge_base_id RETURNING JSON)" in sql
    assert "INSERT INTO rag_business_views" in sql
    assert "-- migration: 20260630_003_document_recipes" in sql
    assert "CREATE TABLE rag_document_recipes" in sql
    assert "RAG_CHUNK_SETS_RECIPE_ACTIVE_UIDX" in sql
    assert "-- migration: 20260701_001_general_feedback" in sql
    assert "business_view_id VARCHAR2(64)" in sql
    assert "RAG_FEEDBACK_USER_TRACE_IDX" in sql
    assert "column_name IN ('DOCUMENT_ID', 'CHUNK_ID')" in sql
    assert "AND nullable = 'N'" in sql
    assert "column_row.column_name || ' NULL)'" in sql
    assert "SET reason = 'answer_untrusted'" in sql
    assert "target_type = ''answer'' AND reason IN" in sql
    assert "target_type = ''citation'' AND reason IN" in sql
    assert "-- migration: 20260701_002_conversation_titles" in sql
    assert "WHEN LENGTH(normalized_title) > 80" in sql
    assert "-- migration: 20260702_001_chunk_search_text" in sql
    assert "ALTER TABLE rag_chunks ADD (search_text CLOB)" in sql
    assert "SET search_text = chunk_text WHERE search_text IS NULL" in sql
    assert "ON rag_chunks (search_text)" in sql
    assert "-- migration: 20260703_001_generation_settings" in sql
    assert "CREATE TABLE rag_prompt_versions" in sql
    assert "CREATE TABLE rag_generation_settings" in sql
    assert "-- migration: 20260703_002_feedback_details" in sql
    assert "CREATE TABLE rag_feedback_details" in sql
    assert "RAG_FEEDBACK_DETAILS_TEXT_IDX" in sql
    assert "SYNC (ON COMMIT)" in sql
    assert "-- migration: 20260927_001_role_access" in sql
    assert "REFERENCES platform_roles (role_id) ON DELETE CASCADE" in sql
    assert "-- migration: 20260928_001_retire_dashboard_permission" in sql
    assert "DELETE FROM rag_role_permissions WHERE permission_code = ''menu.dashboard''" in sql
    # 保存済みの回答の持ち主（#304）。既存の行はチャットの回答・検索の監査から補う。
    assert "-- migration: 20260928_002_answer_record_owner" in sql
    assert "ALTER TABLE rag_answer_records ADD (user_id_hash CHAR(64))" in sql
    assert "rag_answer_records_owner_idx" in sql
    assert "HAVING COUNT(DISTINCT m.user_id_hash) = 1" in sql
    assert "HAVING COUNT(DISTINCT a.user_id_hash) = 1" in sql
    # レシピ行の無い文書にだけレシピ1を補う（#341）。削除したレシピ1は戻さない。
    assert "-- migration: 20260928_003_default_document_recipes" in sql
    assert "SELECT 1 FROM rag_document_recipes existing" in sql
    # 取込 worker の lease 列（#357）。既存の行は NULL のまま（heartbeat の無い行は従来の判定）。
    lease_migration = sql.split("-- migration: 20260928_004_ingestion_jobs_lease", 1)[1].split(
        "-- migration: ", 1
    )[0]
    assert "column_name = 'LEASE_OWNER'" in lease_migration
    assert "ALTER TABLE rag_ingestion_jobs ADD (lease_owner VARCHAR2(128))" in lease_migration
    assert "column_name = 'HEARTBEAT_AT'" in lease_migration
    assert (
        "ALTER TABLE rag_ingestion_jobs ADD (heartbeat_at TIMESTAMP WITH TIME ZONE)"
        in lease_migration
    )
    assert "index_name = 'RAG_INGESTION_JOBS_LEASE_IDX'" in lease_migration
    assert "ON rag_ingestion_jobs (lease_owner, status)" in lease_migration
    assert "UPDATE rag_ingestion_jobs" not in lease_migration
    # 品質評価の job（#390）。表と index を無ければ作る（冪等）。query の本文の列は持たない。
    jobs_migration = sql.split("-- migration: 20260928_005_evaluation_jobs", 1)[1]
    assert "table_name = 'RAG_EVALUATION_JOBS'" in jobs_migration
    assert "'CREATE TABLE rag_evaluation_jobs ('" in jobs_migration
    assert "result_json JSON" in jobs_migration
    assert "CHECK (status IN (''RUNNING'', ''SUCCEEDED'', ''FAILED'', ''CANCELLED''))" in (
        jobs_migration
    )
    assert "index_name = 'RAG_EVALUATION_JOBS_STATUS_IDX'" in jobs_migration
    assert "ON rag_evaluation_jobs (status, heartbeat_at)" in jobs_migration
    assert "index_name = 'RAG_EVALUATION_JOBS_OWNER_CREATED_IDX'" in jobs_migration
    assert "query" not in jobs_migration.split("-- migration: ", 1)[0].lower()
    # 説明が空の DEFAULT の KB・業務ビューに既定の説明を補う（#521）。利用者の説明は上書きしない。
    descriptions_migration = sql.split("-- migration: 20260930_001_default_descriptions", 1)[
        1
    ].split("-- migration: ", 1)[0]
    assert "UPDATE rag_knowledge_bases" in descriptions_migration
    assert "UPDATE rag_business_views" in descriptions_migration
    assert f"'{DEFAULT_KNOWLEDGE_BASE_DESCRIPTION}'" in descriptions_migration
    assert (
        "'DEFAULT ナレッジベースを検索・回答に使う、既定の業務ビューです。'"
        in descriptions_migration
    )
    assert descriptions_migration.count("AND TRIM(description) IS NULL") == 2
    assert "name =" not in descriptions_migration
    # 派生情報レイヤーに、作ったときの入力の指紋の列を足す（#550）。無ければ足す（冪等）。
    fingerprint_migration = sql.split(
        "-- migration: 20260930_002_artifact_layers_input_fingerprint", 1
    )[1].split("-- migration: ", 1)[0]
    assert "table_name = 'RAG_ARTIFACT_LAYERS'" in fingerprint_migration
    assert "column_name = 'INPUT_FINGERPRINT'" in fingerprint_migration
    assert "IF v_column_count = 0 THEN" in fingerprint_migration
    assert "ALTER TABLE rag_artifact_layers ADD (input_fingerprint JSON)" in fingerprint_migration
    # 既存の行は NULL のまま（不明）。値を埋める UPDATE はしない。
    assert "UPDATE" not in fingerprint_migration
    # 文書の 1 ページ目の本文を chunk set ごとに 1 つ持つ列（#557）。列が無ければ足す（冪等）。
    first_page_marker = "-- migration: 20260930_003_chunk_sets_first_page_context"
    first_page_migration = sql.split(first_page_marker, 1)[1]
    assert "column_name = 'FIRST_PAGE_CONTEXT'" in first_page_migration
    assert "'ALTER TABLE rag_chunk_sets ADD (first_page_context JSON)'" in first_page_migration
    # KB ごとの項目抽出の定義の列（#548）。既存の KB は NULL（全体の既定に従う）のまま。
    fields_migration = sql.split("-- migration: 20260930_004_knowledge_base_extraction_fields", 1)[
        1
    ].split("-- migration: ", 1)[0]
    assert "column_name = 'EXTRACTION_FIELDS'" in fields_migration
    assert "ALTER TABLE rag_knowledge_bases ADD (extraction_fields JSON)" in fields_migration
    assert "UPDATE" not in fields_migration
    # 旧 standard の回答エンジンの表とメニュー権限を片付ける（#596）。あるときだけ消す（冪等）。
    retire_migration = sql.split("-- migration: 20260930_005_retire_standard_engine_objects", 1)[
        1
    ].split("-- migration: ", 1)[0]
    assert "table_name = 'RAG_ROLE_PERMISSIONS'" in retire_migration
    assert "'DELETE FROM rag_role_permissions WHERE permission_code IN ('" in retire_migration
    for code in ("menu.settings_grounding", "menu.settings_generation", "menu.settings_agentic"):
        assert f"''{code}''" in retire_migration
    for table in ("RAG_GENERATION_SETTINGS", "RAG_PROMPT_VERSIONS", "RAG_AGENT_MEMORIES"):
        assert f"table_name = '{table}'" in retire_migration
        assert f"'DROP TABLE {table.lower()} CASCADE CONSTRAINTS PURGE'" in retire_migration
    assert retire_migration.count("IF v_count > 0 THEN") == 4
    # rag_prompt_versions を参照する rag_generation_settings を先に消す。
    assert retire_migration.index("DROP TABLE rag_generation_settings") < retire_migration.index(
        "DROP TABLE rag_prompt_versions"
    )
    # 残すもの: 回答の記録（answer_engine の列）・知識グラフ・監査の列。
    assert "rag_answer_records" not in retire_migration
    assert "rag_graph_" not in retire_migration
    assert "rag_search_audit" not in retire_migration
    # 文書構造索引の構築の保存値 full を entities へ書き換える（#621）。値があるときだけ置き換える。
    profile_migration = sql.split("-- migration: 20260930_006_graph_profile_entities", 1)[1].split(
        "-- migration: ", 1
    )[0]
    for table, column, path in (
        ("rag_documents", "processing_config", "$.graph_profile"),
        ("rag_document_recipes", "processing_config", "$.graph_profile"),
        ("rag_knowledge_bases", "retrieval_config", "$.ingestion.graph_profile"),
        ("rag_ingestion_jobs", "settings_overrides", "$.processing_config.graph_profile"),
        ("rag_chunk_sets", "recipe_subset", "$.processing_config.graph_profile"),
        ("rag_chunk_sets", "recipe_subset", "$.effective_processing_config.graph_profile"),
    ):
        assert (
            f"UPDATE {table}\nSET {column} = JSON_TRANSFORM({column}, "
            f"REPLACE '{path}' = 'entities')\nWHERE JSON_VALUE({column}, '{path}') = 'full'"
        ) in profile_migration
    # SET は欄が無いと足してしまう（継承の欄を上書きに変える）ため使わない。
    assert "SET '$" not in profile_migration
    assert "DELETE" not in profile_migration
    # 読む経路の無かった claims / community summary の表を消す（#621）。あるときだけ消す。
    graph_migration = sql.split("-- migration: 20260930_007_retire_graph_claims_community", 1)[
        1
    ].split("-- migration: ", 1)[0]
    for table in ("RAG_GRAPH_CLAIMS", "RAG_GRAPH_COMMUNITY_SUMMARIES"):
        assert f"table_name = '{table}'" in graph_migration
        assert f"'DROP TABLE {table.lower()} CASCADE CONSTRAINTS PURGE'" in graph_migration
    # 文書構造グラフが読む表は残す。
    for table in ("rag_graph_entities", "rag_graph_relationships", "rag_graph_entity_chunks"):
        assert table not in graph_migration
    # 取込 job の抽出の段の進捗の列を足すだけ（#1999）。JSON 型ではなく CLOB で持つ。
    progress_migration = sql.split("-- migration: 20261015_004_ingestion_job_progress", 1)[1].split(
        "-- migration: ", 1
    )[0]
    assert "column_name = 'PROGRESS_JSON'" in progress_migration
    assert "'ALTER TABLE rag_ingestion_jobs ADD (progress_json CLOB)'" in progress_migration
    assert "DROP" not in progress_migration
    # 品質評価の設定比較（#2088 で削除）の名残を消す（#2099）。表・列・索引・制約があるときだけ。
    compare_migration = sql.split("-- migration: 20261016_001_retire_evaluation_compare", 1)[1]
    assert "'DELETE FROM rag_evaluation_jobs WHERE kind <> ''run'''" in compare_migration
    assert "constraint_name = 'RAG_EVALUATION_JOBS_KIND_CK'" in compare_migration
    assert "'CHECK (kind IN (''run''))'" in compare_migration
    assert "''compare''" not in compare_migration
    assert "column_name = 'CURRENT_EXPERIMENT_ID'" in compare_migration
    # 文書の分類の一覧の表と索引を無いときだけ作る（#2131）。一意の索引も作る。
    categories_migration = sql.split("-- migration: 20261016_003_document_categories", 1)[1].split(
        "-- migration: ", 1
    )[0]
    assert "CREATE TABLE rag_document_categories" in categories_migration
    assert "CREATE UNIQUE INDEX rag_document_categories_name_ux" in categories_migration
    assert "index_name = 'RAG_DOCUMENT_CATEGORIES_NAME_UX'" in categories_migration
    assert "DROP" not in categories_migration
    assert (
        "'ALTER TABLE rag_evaluation_jobs DROP COLUMN current_experiment_id'" in compare_migration
    )
    assert "index_name = 'RAG_EVALUATION_RUNS_BEST_EXPERIMENT_IDX'" in compare_migration
    assert "'DROP INDEX rag_evaluation_runs_best_experiment_idx'" in compare_migration
    assert "column_name = 'BEST_EXPERIMENT_ID'" in compare_migration
    assert "'ALTER TABLE rag_evaluation_runs DROP COLUMN best_experiment_id'" in compare_migration
    # 評価の結果の行は消さない。
    assert "DELETE FROM rag_evaluation_runs" not in compare_migration
    # 業務ガイドの質問の例の埋め込みの表を無いときだけ作る（#2398）。
    embeddings_migration = sql.split("-- migration: 20261018_001_support_guide_embeddings", 1)[
        1
    ].split("-- migration: ", 1)[0]
    assert "CREATE TABLE rag_support_guide_embeddings" in embeddings_migration
    assert "table_name = 'RAG_SUPPORT_GUIDE_EMBEDDINGS'" in embeddings_migration
    assert "DROP" not in embeddings_migration
    # 回答に付けた注意書きの列を無いときだけ足す（#2405。会話の回答と回答の記録）。
    for name, table in (
        ("20261018_002_message_answer_notice", "rag_messages"),
        ("20261018_003_answer_record_notice", "rag_answer_records"),
    ):
        notice_migration = sql.split(f"-- migration: {name}", 1)[1].split("-- migration:", 1)[0]
        assert f"'ALTER TABLE {table} ADD (answer_notice CLOB)'" in notice_migration
        assert "column_name = 'ANSWER_NOTICE'" in notice_migration
        assert "DROP" not in notice_migration
    # 質問に添付したファイルの列を無いときだけ足す（#2517）。
    attachments_migration = sql.split("-- migration: 20261019_010_message_attachments", 1)[1]
    assert "'ALTER TABLE rag_messages ADD (attachments_json CLOB)'" in attachments_migration
    assert "column_name = 'ATTACHMENTS_JSON'" in attachments_migration
    assert "DROP" not in attachments_migration.split("-- migration:", 1)[0]
    # 業務ガイドの内容の形の版を 2 にする（#2395）。schema_version だけを書き換える。
    guide_migration = sql.split("-- migration: 20261018_004_support_guide_schema_v2", 1)[1].split(
        "-- migration: ", 1
    )[0]
    assert "JSON_TRANSFORM(draft_json, SET '$.schema_version' = 2)" in guide_migration
    assert "JSON_TRANSFORM(content_json, SET '$.schema_version' = 2)" in guide_migration
    assert "DELETE" not in guide_migration and "DROP" not in guide_migration
    # 取り消した migration（20261016_002）を当てた DB の分類の列を戻す（#2450）。列を足し、
    # effective_period の有効期間を classification が NULL の行にだけ写す。改名・削除はしない。
    restore_migration = sql.split("-- migration: 20261019_001_restore_document_classification", 1)[
        1
    ].split("-- migration: ", 1)[0]
    assert "'ALTER TABLE rag_documents ADD (classification JSON)'" in restore_migration
    assert "'ALTER TABLE rag_query_history ADD (classification_filter JSON)'" in restore_migration
    assert "column_name = 'EFFECTIVE_PERIOD'" in restore_migration
    assert "WHERE target.classification IS NULL" in restore_migration
    assert "RENAME" not in restore_migration
    assert "DROP" not in restore_migration and "DELETE" not in restore_migration
    # 文書の削除の後始末の記録の表と索引を無いときだけ作る（#2452）。
    cleanups_migration = sql.split("-- migration: 20261019_002_document_cleanups", 1)[1].split(
        "-- migration: 20261019_003_structured_records_jsonl_profiles", 1
    )[0]
    assert "CREATE TABLE rag_document_cleanups" in cleanups_migration
    assert "index_name = 'RAG_DOCUMENT_CLEANUPS_UPDATED_IDX'" in cleanups_migration
    assert "DROP" not in cleanups_migration
    # ファイル準備の改名と、文書レシピの解析エンジン・分割符の移行(#2457)。値の書き換えだけ。
    jsonl_migration = sql.split("-- migration: 20261019_003_structured_records_jsonl_profiles", 1)[
        1
    ].split("-- migration: ", 1)[0]
    assert "''\"csv_to_json\"'', ''\"csv_to_jsonl\"''" in jsonl_migration
    assert "''\"excel_to_json\"'', ''\"excel_to_jsonl\"''" in jsonl_migration
    assert "'RAG_DOCUMENT_RECIPES.PROCESSING_CONFIG'" in jsonl_migration
    assert "'RAG_INGESTION_JOBS.SETTINGS_OVERRIDES'" in jsonl_migration
    assert "SET ''$.parser_adapter_backend'' = ''structured_records_jsonl''" in jsonl_migration
    assert "IN (''docling'', ''unstructured'')" in jsonl_migration
    assert "SET ''$.chunk_delimiter'' = ''\\n\\n''" in jsonl_migration
    assert "= ''fixed_delimiter''" in jsonl_migration
    assert "DROP" not in jsonl_migration and "DELETE" not in jsonl_migration
    assert len(statements) == 124
    assert all(
        statement.startswith(
            ("-- migration:", "DECLARE", "INSERT", "MERGE", "UPDATE", "COMMIT", "CREATE")
        )
        for statement in statements
    )


def test_oracle_schema_migration_manifest_is_deterministic() -> None:
    """migration manifest は artifact hash と migration 単位の hash を含む。"""
    sql = oracle_schema.oracle_schema_migration_sql()
    manifest = oracle_schema.oracle_schema_migration_manifest()

    assert manifest == oracle_schema.oracle_schema_migration_manifest()
    assert manifest["schema_name"] == "production-ready-rag-oracle-26ai"
    assert manifest["schema_version"] == "3"
    assert manifest["artifact_type"] == "migration"
    assert manifest["migration_artifact_version"] == "20261009_001"
    assert manifest["sha256"] == hashlib.sha256(sql.encode("utf-8")).hexdigest()
    assert manifest["statement_count"] == len(oracle_schema.split_sql_statements(sql))
    assert [migration["name"] for migration in manifest["migrations"]] == [
        "20260615_001_ingestion_jobs_attempt_counters",
        "20260616_001_search_audit_search_mode",
        "20260616_002_evaluation_runs_result_sha256",
        "20260616_003_ingestion_jobs_cancelled_status",
        "20260616_004_ingestion_segments",
        "20260616_005_search_audit_memory_engineering",
        "20260616_006_agent_memories",
        "20260617_001_ingestion_audit_file_processing_metrics",
        "20260617_002_search_audit_adaptive_context",
        "20260617_003_search_audit_dependency_context",
        "20260618_001_documents_review_status",
        "20260618_002_ingestion_jobs_phase",
        "20260619_001_business_views",
        "20260621_001_chunk_sets",
        "20260621_002_document_extractions",
        "20260623_001_nullable_chunk_embeddings",
        "20260625_001_chunks_text_world_lexer",
        "20260625_002_preprocess_artifact",
        "20260627_001_documents_preprocessed_status",
        "20260629_001_chunk_sets_serving",
        "20260629_002_drop_kb_chunk_set_bindings",
        "20260629_003_ingestion_jobs_settings_overrides",
        "20260629_004_documents_processing_config",
        "20260630_001_default_knowledge_base_name",
        "20260630_002_default_business_view",
        "20260630_003_document_recipes",
        "20260701_001_general_feedback",
        "20260701_002_conversation_titles",
        "20260702_001_chunk_search_text",
        "20260703_001_generation_settings",
        "20260703_002_feedback_details",
        "20260925_001_business_view_knowledge",
        "20260925_002_answer_records",
        "20260926_001_documents_classification",
        "20260926_002_answer_record_evaluation",
        "20260926_003_feedback_reasons_corrected_answer",
        "20260926_004_docrag_prompts",
        "20260926_005_query_history",
        "20260927_001_role_access",
        "20260928_001_retire_dashboard_permission",
        "20260928_002_answer_record_owner",
        "20260928_003_default_document_recipes",
        "20260928_004_ingestion_jobs_lease",
        "20260928_005_evaluation_jobs",
        "20260930_001_default_descriptions",
        "20260930_002_artifact_layers_input_fingerprint",
        "20260930_003_chunk_sets_first_page_context",
        "20260930_004_knowledge_base_extraction_fields",
        "20260930_005_retire_standard_engine_objects",
        "20260930_006_graph_profile_entities",
        "20260930_007_retire_graph_claims_community",
        "20260930_008_answer_prompts_table",
        "20260930_009_stored_engine_names",
        "20261001_001_document_sections",
        "20261003_001_search_answer_profiles",
        "20261005_001_message_answer_runs",
        "20261007_001_document_superseded",
        "20261009_001_entity_layer",
        "20261011_001_section_chunk_map",
        "20261015_001_message_clarification",
        "20261015_002_run_history",
        "20261015_003_run_history_conversation_idx",
        "20261015_004_ingestion_job_progress",
        "20261016_001_retire_evaluation_compare",
        "20261016_003_document_categories",
        "20261018_001_support_guide_embeddings",
        "20261018_002_message_answer_notice",
        "20261018_003_answer_record_notice",
        "20261018_004_support_guide_schema_v2",
        "20261019_001_restore_document_classification",
        "20261019_002_document_cleanups",
        "20261019_003_structured_records_jsonl_profiles",
        "20261019_010_message_attachments",
        "20261021_001_answer_prompt_placeholders",
        "20261021_002_document_delete_fk_indexes",
        "20261022_001_cited_evidence",
        "20261026_001_evaluation_calibration",
        "20261026_002_evaluation_job_calibration_kind",
        "20261027_001_raptor_prompt_consolidation",
        "20261028_001_language_pack_releases",
        "20261028_002_language_pack_artifacts",
        "20261029_001_retire_query_history",
    ]


def test_oracle_schema_cli_writes_sql_and_manifest(tmp_path: Path) -> None:
    """CLI は SQL と manifest を staging artifact として保存できる。"""
    sql_output = tmp_path / "artifacts" / "oracle-schema.sql"
    manifest_output = tmp_path / "artifacts" / "oracle-schema.manifest.json"

    exit_code = oracle_schema.main(
        [
            "--output",
            str(sql_output),
            "--manifest-output",
            str(manifest_output),
        ]
    )

    assert exit_code == 0
    assert "CREATE TABLE rag_documents" in sql_output.read_text(encoding="utf-8")
    manifest = json.loads(manifest_output.read_text(encoding="utf-8"))
    assert (
        manifest["sha256"]
        == hashlib.sha256(sql_output.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    )


def test_oracle_schema_cli_writes_migration_sql_and_manifest(tmp_path: Path) -> None:
    """CLI は既存 schema 用 migration artifact も保存できる。"""
    sql_output = tmp_path / "artifacts" / "oracle-schema-migration.sql"
    manifest_output = tmp_path / "artifacts" / "oracle-schema-migration.manifest.json"

    exit_code = oracle_schema.main(
        [
            "--migration",
            "--output",
            str(sql_output),
            "--manifest-output",
            str(manifest_output),
        ]
    )

    assert exit_code == 0
    migration_sql = sql_output.read_text(encoding="utf-8")
    assert "MAX_ATTEMPTS" in migration_sql
    assert "SEARCH_MODE" in migration_sql
    assert "RESULT_SHA256" in migration_sql
    manifest = json.loads(manifest_output.read_text(encoding="utf-8"))
    assert manifest["artifact_type"] == "migration"
    assert (
        manifest["sha256"]
        == hashlib.sha256(sql_output.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    )


def test_oracle_schema_cli_manifest_only_prints_json(
    capsys: CaptureFixture[str],
) -> None:
    """--manifest-only は SQL を出さず manifest JSON だけを stdout に出す。"""
    exit_code = oracle_schema.main(["--manifest-only"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "CREATE TABLE" not in captured.out
    manifest = json.loads(captured.out)
    assert manifest["vector_contract"] == "VECTOR(1536, FLOAT32)"


def test_oracle_schema_cli_migration_manifest_only_prints_json(
    capsys: CaptureFixture[str],
) -> None:
    """--migration --manifest-only は migration manifest だけを stdout に出す。"""
    exit_code = oracle_schema.main(["--migration", "--manifest-only"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "ALTER TABLE" not in captured.out
    manifest = json.loads(captured.out)
    assert manifest["artifact_type"] == "migration"


def test_vector_index_reindex_sql_reflects_profile_build_params() -> None:
    """再作成 SQL は渡された profile のビルド推奨値を反映する(DROP + CREATE)。"""
    sql = oracle_schema.vector_index_reindex_sql(
        target_accuracy=98,
        neighbors=48,
        efconstruction=800,
    )

    assert "DROP INDEX rag_chunks_embedding_hnsw_idx;" in sql
    assert "CREATE VECTOR INDEX rag_chunks_embedding_hnsw_idx" in sql
    assert "ON rag_chunks (embedding)" in sql
    assert "WITH TARGET ACCURACY 98" in sql
    assert "NEIGHBORS 48" in sql
    assert "EFCONSTRUCTION 800" in sql
    assert "DISTANCE COSINE" in sql


def test_vector_index_reindex_sql_balanced_uses_current_build() -> None:
    """balanced 既定値では現行 HNSW ビルド(32/500)を出力する。"""
    sql = oracle_schema.vector_index_reindex_sql(
        target_accuracy=95,
        neighbors=32,
        efconstruction=500,
    )

    assert "WITH TARGET ACCURACY 95" in sql
    assert "NEIGHBORS 32" in sql
    assert "EFCONSTRUCTION 500" in sql


def test_migration_artifact_separates_unterminated_sql_from_plsql() -> None:
    sections = [
        oracle_schema.OracleSchemaSection(
            name="first", table_name="example", sql="UPDATE example SET value = 1"
        ),
        oracle_schema.OracleSchemaSection(
            name="second",
            table_name="example",
            sql="DECLARE\n    n NUMBER;\nBEGIN\n    n := 1;\nEND;\n/",
        ),
    ]
    statements = oracle_schema.split_sql_statements(
        oracle_schema.oracle_schema_migration_sql(sections)
    )
    assert len(statements) == 2
    assert statements[0].endswith("UPDATE example SET value = 1")
    assert statements[1].endswith("END;")


def test_oracle_schema_migration_sql_adds_evaluation_calibration() -> None:
    """判定の校正の表と索引は無いときだけ作り、job の kind に calibration を足す（#3578）。"""
    sql = oracle_schema.oracle_schema_migration_sql()
    tables = sql.split("-- migration: 20261026_001_evaluation_calibration", 1)[1].split(
        "-- migration: ", 1
    )[0]
    for table in (
        "RAG_EVALUATION_LABEL_SETS",
        "RAG_EVALUATION_LABEL_ITEMS",
        "RAG_EVALUATION_LABELS",
        "RAG_EVALUATION_CALIBRATIONS",
    ):
        assert f"table_name = '{table}'" in tables
    assert "index_name = 'RAG_EVALUATION_LABELS_ANNOTATOR_IDX'" in tables
    assert "DROP" not in tables
    assert "DELETE FROM" not in tables
    kind = sql.split("-- migration: 20261026_002_evaluation_job_calibration_kind", 1)[1].split(
        "-- migration: ", 1
    )[0]
    assert "constraint_name = 'RAG_EVALUATION_JOBS_KIND_CK'" in kind
    assert "'CHECK (kind IN (''run'', ''calibration''))'" in kind
    assert "DELETE" not in kind


def test_fresh_base_contains_release_tables_and_all_artifact_provenance_columns() -> None:
    """新規初期化では migration の再実行なしで公開版と成果物の列を作る。"""
    from app.rag.system_schema import MANAGED_TABLES

    sql = oracle_schema.oracle_schema_sql().upper()
    assert sql.index("CREATE TABLE RAG_LANGUAGE_PACK_RELEASES") < sql.index(
        "CREATE TABLE RAG_INGESTION_JOBS"
    )
    assert "CREATE TABLE RAG_LANGUAGE_PACK_STATE" in sql
    assert {"RAG_LANGUAGE_PACK_RELEASES", "RAG_LANGUAGE_PACK_STATE"} <= set(MANAGED_TABLES)
    for table in ("RAG_INGESTION_JOBS", "RAG_CHUNK_SETS"):
        ddl = sql.split(f"CREATE TABLE {table} (", 1)[1].split(";", 1)[0]
        assert "LANGUAGE_PACK_RELEASE_ID VARCHAR2(64)" in ddl
        assert "REFERENCES RAG_LANGUAGE_PACK_RELEASES (RELEASE_ID)" in ddl
        if table == "RAG_CHUNK_SETS":
            for column in (
                "LANGUAGE_PACK_INGEST_HASH",
                "LANGUAGE_PACK_JOB_ID",
                "LANGUAGE_PACK_BUILT_AT",
            ):
                assert column in ddl
    migrations = {
        section.name: section for section in oracle_schema.oracle_schema_migration_sections()
    }
    assert (
        "CREATE TABLE rag_language_pack_releases"
        in migrations["20261028_001_language_pack_releases"].sql
    )
    upgrade = migrations["20261028_002_language_pack_artifacts"].sql
    assert "user_tab_columns" in upgrade and "IF v_count = 0" in upgrade
    assert "DROP" not in upgrade and "DELETE" not in upgrade


def test_checked_in_migration_artifacts_match_the_generator() -> None:
    artifacts = Path(__file__).resolve().parents[2] / "artifacts"
    assert (artifacts / "oracle-schema-migration.sql").read_text() == (
        oracle_schema.oracle_schema_migration_sql()
    )
    assert json.loads((artifacts / "oracle-schema-migration.manifest.json").read_text()) == (
        oracle_schema.oracle_schema_migration_manifest()
    )
