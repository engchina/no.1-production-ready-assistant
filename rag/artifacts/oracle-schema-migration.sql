-- migration: 20260615_001_ingestion_jobs_attempt_counters
DECLARE
    v_column_count NUMBER;
    v_nullable VARCHAR2(1);
BEGIN
    SELECT COUNT(*)
    INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_INGESTION_JOBS'
      AND column_name = 'ATTEMPT_COUNT';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_ingestion_jobs ADD '
            || '(attempt_count NUMBER(5) DEFAULT 0 NOT NULL)';
    ELSE
        SELECT nullable
        INTO v_nullable
        FROM user_tab_columns
        WHERE table_name = 'RAG_INGESTION_JOBS'
          AND column_name = 'ATTEMPT_COUNT';

        EXECUTE IMMEDIATE
            'UPDATE rag_ingestion_jobs SET attempt_count = 0 '
            || 'WHERE attempt_count IS NULL';
        IF v_nullable = 'Y' THEN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_ingestion_jobs MODIFY '
                || '(attempt_count DEFAULT 0 NOT NULL)';
        ELSE
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_ingestion_jobs MODIFY '
                || '(attempt_count DEFAULT 0)';
        END IF;
    END IF;
END;
/

DECLARE
    v_column_count NUMBER;
    v_nullable VARCHAR2(1);
BEGIN
    SELECT COUNT(*)
    INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_INGESTION_JOBS'
      AND column_name = 'MAX_ATTEMPTS';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_ingestion_jobs ADD '
            || '(max_attempts NUMBER(5) DEFAULT 3 NOT NULL)';
    ELSE
        SELECT nullable
        INTO v_nullable
        FROM user_tab_columns
        WHERE table_name = 'RAG_INGESTION_JOBS'
          AND column_name = 'MAX_ATTEMPTS';

        EXECUTE IMMEDIATE
            'UPDATE rag_ingestion_jobs SET max_attempts = 3 '
            || 'WHERE max_attempts IS NULL';
        IF v_nullable = 'Y' THEN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_ingestion_jobs MODIFY '
                || '(max_attempts DEFAULT 3 NOT NULL)';
        ELSE
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_ingestion_jobs MODIFY '
                || '(max_attempts DEFAULT 3)';
        END IF;
    END IF;
END;
/

DECLARE
    v_constraint_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_INGESTION_JOBS'
      AND constraint_name = 'RAG_INGESTION_JOBS_ATTEMPTS_CK';

    IF v_constraint_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_ingestion_jobs DROP CONSTRAINT '
            || 'rag_ingestion_jobs_attempts_ck';
    END IF;

    EXECUTE IMMEDIATE
        'ALTER TABLE rag_ingestion_jobs ADD CONSTRAINT '
        || 'rag_ingestion_jobs_attempts_ck CHECK '
        || '(attempt_count >= 0 AND max_attempts >= 1)';
END;
/

-- migration: 20260616_001_search_audit_search_mode
DECLARE
    v_mode_count NUMBER;
    v_search_mode_count NUMBER;
    v_constraint_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_mode_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_SEARCH_AUDIT'
      AND column_name = 'MODE';

    SELECT COUNT(*)
    INTO v_search_mode_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_SEARCH_AUDIT'
      AND column_name = 'SEARCH_MODE';

    IF v_mode_count > 0 AND v_search_mode_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_search_audit RENAME COLUMN mode TO search_mode';
    ELSIF v_mode_count = 0 AND v_search_mode_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_search_audit ADD '
            || '(search_mode VARCHAR2(16) DEFAULT ''hybrid'' NOT NULL)';
    END IF;

    SELECT COUNT(*)
    INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_SEARCH_AUDIT'
      AND constraint_name IN ('RAG_SEARCH_AUDIT_MODE_CK', 'RAG_SEARCH_AUDIT_SEARCH_MODE_CK');

    IF v_constraint_count > 0 THEN
        BEGIN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_search_audit DROP CONSTRAINT rag_search_audit_mode_ck';
        EXCEPTION
            WHEN OTHERS THEN NULL;
        END;
        BEGIN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_search_audit DROP CONSTRAINT rag_search_audit_search_mode_ck';
        EXCEPTION
            WHEN OTHERS THEN NULL;
        END;
    END IF;

    EXECUTE IMMEDIATE
        'ALTER TABLE rag_search_audit ADD CONSTRAINT '
        || 'rag_search_audit_search_mode_ck CHECK '
        || '(search_mode IN (''hybrid'', ''vector'', ''keyword''))';
END;
/

-- migration: 20260616_002_evaluation_runs_result_sha256
DECLARE
    v_column_count NUMBER;
    v_index_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_EVALUATION_RUNS'
      AND column_name = 'RESULT_SHA256';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_evaluation_runs ADD '
            || '(result_sha256 CHAR(64) DEFAULT '''
            || RPAD('0', 64, '0')
            || ''' NOT NULL)';
    END IF;

    SELECT COUNT(*)
    INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_EVALUATION_RUNS_RESULT_HASH_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_evaluation_runs_result_hash_idx '
            || 'ON rag_evaluation_runs (result_sha256)';
    END IF;
END;
/

-- migration: 20260616_003_ingestion_jobs_cancelled_status
DECLARE
    v_constraint_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_INGESTION_JOBS'
      AND constraint_name = 'RAG_INGESTION_JOBS_STATUS_CK';

    IF v_constraint_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_ingestion_jobs DROP CONSTRAINT '
            || 'rag_ingestion_jobs_status_ck';
    END IF;

    EXECUTE IMMEDIATE
        'ALTER TABLE rag_ingestion_jobs ADD CONSTRAINT '
        || 'rag_ingestion_jobs_status_ck CHECK '
        || '(status IN (''QUEUED'', ''RUNNING'', ''SUCCEEDED'', ''FAILED'', '
        || '''SKIPPED'', ''CANCELLED''))';
END;
/

-- migration: 20260616_004_ingestion_segments
DECLARE
    v_table_count NUMBER;
    v_index_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_INGESTION_SEGMENTS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_ingestion_segments ('
            || 'segment_id VARCHAR2(128) PRIMARY KEY,'
            || 'document_id VARCHAR2(64) NOT NULL,'
            || 'tenant_id_hash CHAR(64),'
            || 'status VARCHAR2(32) DEFAULT ''QUEUED'' NOT NULL,'
            || 'parser_backend VARCHAR2(80) DEFAULT ''enterprise_ai'' NOT NULL,'
            || 'parser_profile VARCHAR2(80) DEFAULT ''enterprise_ai_generic'' NOT NULL,'
            || 'page_start NUMBER(10),'
            || 'page_end NUMBER(10),'
            || 'attempt_count NUMBER(5) DEFAULT 0 NOT NULL,'
            || 'artifact_path VARCHAR2(1024),'
            || 'error_code VARCHAR2(128),'
            || 'error_message VARCHAR2(2000),'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_ingestion_segments_status_ck CHECK '
            || '(status IN (''QUEUED'', ''RUNNING'', ''SUCCEEDED'', ''FAILED'', ''CANCELLED'')),'
            || 'CONSTRAINT rag_ingestion_segments_attempts_ck CHECK (attempt_count >= 0),'
            || 'CONSTRAINT rag_ingestion_segments_page_range_ck CHECK '
            || '(page_start IS NULL OR page_end IS NULL OR page_start <= page_end),'
            || 'CONSTRAINT rag_ingestion_segments_document_fk FOREIGN KEY (document_id) '
            || 'REFERENCES rag_documents (document_id) ON DELETE CASCADE'
            || ')';
    END IF;

    SELECT COUNT(*)
    INTO v_index_count
    FROM user_indexes
    WHERE index_name IN (
        'RAG_INGESTION_SEGMENTS_DOC_STATUS_IDX',
        'RAG_INGESTION_SEGMENTS_DOCUMENT_STATUS_IDX'
    );

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_ingestion_segments_document_status_idx '
            || 'ON rag_ingestion_segments (document_id, status, page_start, page_end)';
    END IF;

    SELECT COUNT(*)
    INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_INGESTION_SEGMENTS_TENANT_STATUS_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_ingestion_segments_tenant_status_idx '
            || 'ON rag_ingestion_segments (tenant_id_hash, status, updated_at DESC)';
    END IF;
END;
/

-- migration: 20260616_005_search_audit_memory_engineering
DECLARE
    PROCEDURE add_column_if_missing(p_column_name VARCHAR2, p_column_ddl VARCHAR2) IS
        v_column_count NUMBER;
    BEGIN
        SELECT COUNT(*)
        INTO v_column_count
        FROM user_tab_columns
        WHERE table_name = 'RAG_SEARCH_AUDIT'
          AND column_name = p_column_name;

        IF v_column_count = 0 THEN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_search_audit ADD (' || p_column_ddl || ')';
        END IF;
    END;
BEGIN
    add_column_if_missing('MEMORY_PLAN_ID', 'memory_plan_id VARCHAR2(32)');
    add_column_if_missing(
        'EVIDENCE_COUNT',
        'evidence_count NUMBER(10) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing(
        'SUPPORT_COUNT',
        'support_count NUMBER(10) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing(
        'STRUCTURE_COUNT',
        'structure_count NUMBER(10) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing(
        'HISTORY_COUNT',
        'history_count NUMBER(10) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing(
        'RESOLVER_REJECTED_COUNT',
        'resolver_rejected_count NUMBER(10) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing(
        'INSUFFICIENT_CONTEXT_COUNT',
        'insufficient_context_count NUMBER(10) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing(
        'AGENT_MEMORY_RETRIEVED_COUNT',
        'agent_memory_retrieved_count NUMBER(10) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing(
        'AGENT_MEMORY_WRITEBACK_COUNT',
        'agent_memory_writeback_count NUMBER(10) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing(
        'AGENT_MEMORY_WRITEBACK_STATUS',
        'agent_memory_writeback_status VARCHAR2(32) DEFAULT ''skipped'' NOT NULL'
    );
END;
/

-- migration: 20260616_006_agent_memories
DECLARE
    v_table_count NUMBER;
    v_index_count NUMBER;

    PROCEDURE add_column_if_missing(p_column_name VARCHAR2, p_column_ddl VARCHAR2) IS
        v_column_count NUMBER;
    BEGIN
        SELECT COUNT(*)
        INTO v_column_count
        FROM user_tab_columns
        WHERE table_name = 'RAG_AGENT_MEMORIES'
          AND column_name = p_column_name;

        IF v_column_count = 0 THEN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_agent_memories ADD (' || p_column_ddl || ')';
        END IF;
    END;

    PROCEDURE create_index_if_missing(p_index_name VARCHAR2, p_sql VARCHAR2) IS
    BEGIN
        SELECT COUNT(*)
        INTO v_index_count
        FROM user_indexes
        WHERE index_name = p_index_name;

        IF v_index_count = 0 THEN
            EXECUTE IMMEDIATE p_sql;
        END IF;
    END;
BEGIN
    SELECT COUNT(*)
    INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_AGENT_MEMORIES';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_agent_memories ('
            || 'memory_id VARCHAR2(64) PRIMARY KEY,'
            || 'tenant_id_hash CHAR(64),'
            || 'user_id_hash CHAR(64),'
            || 'role_id_hash CHAR(64),'
            || 'agent_id_hash CHAR(64),'
            || 'thread_id_hash CHAR(64),'
            || 'trace_id VARCHAR2(64) NOT NULL,'
            || 'memory_text CLOB NOT NULL,'
            || 'metadata_json JSON,'
            || 'embedding VECTOR(1536, FLOAT32) NOT NULL,'
            || 'usefulness_score NUMBER(8,6) DEFAULT 0.5 NOT NULL,'
            || 'eval_count NUMBER(10) DEFAULT 0 NOT NULL,'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_agent_memories_usefulness_ck CHECK '
            || '(usefulness_score >= 0 AND usefulness_score <= 1),'
            || 'CONSTRAINT rag_agent_memories_eval_count_ck CHECK (eval_count >= 0)'
            || ')';
    END IF;

    add_column_if_missing('ROLE_ID_HASH', 'role_id_hash CHAR(64)');

    create_index_if_missing(
        'RAG_AGENT_MEMORIES_EMBEDDING_HNSW_IDX',
        'CREATE VECTOR INDEX rag_agent_memories_embedding_hnsw_idx '
        || 'ON rag_agent_memories (embedding) '
        || 'ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE '
        || 'WITH TARGET ACCURACY 95 '
        || 'PARAMETERS (TYPE HNSW, NEIGHBORS 32, EFCONSTRUCTION 500)'
    );
    create_index_if_missing(
        'RAG_AGENT_MEMORIES_TEXT_IDX',
        'CREATE INDEX rag_agent_memories_text_idx '
        || 'ON rag_agent_memories (memory_text) INDEXTYPE IS CTXSYS.CONTEXT'
    );
    create_index_if_missing(
        'RAG_AGENT_MEMORIES_SCOPE_IDX',
        'CREATE INDEX rag_agent_memories_scope_idx '
        || 'ON rag_agent_memories (tenant_id_hash, user_id_hash, '
        || 'role_id_hash, agent_id_hash, thread_id_hash, updated_at DESC)'
    );
    create_index_if_missing(
        'RAG_AGENT_MEMORIES_TRACE_IDX',
        'CREATE INDEX rag_agent_memories_trace_idx ON rag_agent_memories (trace_id)'
    );
END;
/

-- migration: 20260617_001_ingestion_audit_file_processing_metrics
DECLARE
    v_index_count NUMBER;

    PROCEDURE add_column_if_missing(p_column_name VARCHAR2, p_column_ddl VARCHAR2) IS
        v_column_count NUMBER;
    BEGIN
        SELECT COUNT(*)
        INTO v_column_count
        FROM user_tab_columns
        WHERE table_name = 'RAG_INGESTION_AUDIT'
          AND column_name = p_column_name;

        IF v_column_count = 0 THEN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_ingestion_audit ADD (' || p_column_ddl || ')';
        END IF;
    END;
BEGIN
    add_column_if_missing('PARSER_BACKEND', 'parser_backend VARCHAR2(80)');
    add_column_if_missing('PARSER_PROFILE', 'parser_profile VARCHAR2(80)');
    add_column_if_missing(
        'SEGMENT_COUNT',
        'segment_count NUMBER(10) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing(
        'FALLBACK_COUNT',
        'fallback_count NUMBER(10) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing(
        'FAILED_SEGMENT_COUNT',
        'failed_segment_count NUMBER(10) DEFAULT 0 NOT NULL'
    );

    SELECT COUNT(*)
    INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_INGESTION_AUDIT_PARSER_CREATED_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_ingestion_audit_parser_created_idx '
            || 'ON rag_ingestion_audit (parser_backend, parser_profile, created_at DESC)';
    END IF;
END;
/

-- migration: 20260617_002_search_audit_adaptive_context
DECLARE
    v_column_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_SEARCH_AUDIT'
      AND column_name = 'CONTEXT_ADAPTIVE_EXPANDED_COUNT';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_search_audit ADD '
            || '(context_adaptive_expanded_count NUMBER(10) DEFAULT 0 NOT NULL)';
    END IF;
END;
/

-- migration: 20260617_003_search_audit_dependency_context
DECLARE
    v_column_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_SEARCH_AUDIT'
      AND column_name = 'CONTEXT_DEPENDENCY_PROMOTED_COUNT';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_search_audit ADD '
            || '(context_dependency_promoted_count NUMBER(10) DEFAULT 0 NOT NULL)';
    END IF;
END;
/

-- migration: 20260618_001_documents_review_status
DECLARE
    v_constraint_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_DOCUMENTS'
      AND constraint_name = 'RAG_DOCUMENTS_STATUS_CK';

    IF v_constraint_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_documents DROP CONSTRAINT '
            || 'rag_documents_status_ck';
    END IF;

    EXECUTE IMMEDIATE
        'ALTER TABLE rag_documents ADD CONSTRAINT '
        || 'rag_documents_status_ck CHECK '
        || '(status IN (''UPLOADED'', ''PREPROCESSING'', ''PREPROCESSED'', '
        || '''INGESTING'', ''REVIEW'', ''CHUNKING'', ''CHUNKED'', ''INDEXING'', '
        || '''INDEXED'', ''ERROR''))';
END;
/

-- migration: 20260618_002_ingestion_jobs_phase
DECLARE
    v_column_count NUMBER;
    v_constraint_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_INGESTION_JOBS'
      AND column_name = 'PHASE';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_ingestion_jobs ADD '
            || '(phase VARCHAR2(16) DEFAULT ''PREPROCESS'' NOT NULL)';
    END IF;

    SELECT COUNT(*)
    INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_INGESTION_JOBS'
      AND constraint_name = 'RAG_INGESTION_JOBS_PHASE_CK';

    IF v_constraint_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_ingestion_jobs DROP CONSTRAINT '
            || 'rag_ingestion_jobs_phase_ck';
    END IF;

    EXECUTE IMMEDIATE
        'ALTER TABLE rag_ingestion_jobs ADD CONSTRAINT '
        || 'rag_ingestion_jobs_phase_ck CHECK '
        || '(phase IN (''PREPROCESS'', ''EXTRACT'', ''CHUNK'', ''INDEX''))';
END;
/

-- migration: 20260619_001_business_views
DECLARE
    v_table_count NUMBER;
    v_index_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_BUSINESS_VIEWS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_business_views ('
            || 'business_view_id VARCHAR2(64) PRIMARY KEY,'
            || 'tenant_id_hash CHAR(64),'
            || 'name VARCHAR2(256) NOT NULL,'
            || 'description VARCHAR2(2000),'
            || 'status VARCHAR2(32) DEFAULT ''ACTIVE'' NOT NULL,'
            || 'view_config JSON,'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'archived_at TIMESTAMP WITH TIME ZONE,'
            || 'CONSTRAINT rag_business_views_status_ck CHECK '
            || '(status IN (''ACTIVE'', ''ARCHIVED''))'
            || ')';
    END IF;

    SELECT COUNT(*)
    INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_BUSINESS_VIEWS_TENANT_NAME_UIDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE UNIQUE INDEX rag_business_views_tenant_name_uidx '
            || 'ON rag_business_views (NVL(tenant_id_hash, ''__GLOBAL__''), LOWER(name))';
    END IF;

    SELECT COUNT(*)
    INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_BUSINESS_VIEWS_TENANT_STATUS_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_business_views_tenant_status_idx '
            || 'ON rag_business_views (tenant_id_hash, status, updated_at DESC)';
    END IF;
END;
/

-- migration: 20260621_001_chunk_sets
DECLARE
    v_table_count NUMBER;
    v_index_count NUMBER;
    v_col_count   NUMBER;
    v_constraint_count NUMBER;
    PROCEDURE add_column_if_missing(
        p_table_name IN VARCHAR2,
        p_column_name IN VARCHAR2,
        p_definition IN VARCHAR2
    ) IS
    BEGIN
        SELECT COUNT(*) INTO v_col_count
        FROM user_tab_columns
        WHERE table_name = p_table_name AND column_name = p_column_name;

        IF v_col_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE ' || p_table_name || ' ADD (' || p_definition || ')';
        END IF;
    END;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables WHERE table_name = 'RAG_CHUNK_SETS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_chunk_sets ('
            || 'chunk_set_id VARCHAR2(64) PRIMARY KEY,'
            || 'document_id VARCHAR2(64) NOT NULL,'
            || 'extraction_recipe_id VARCHAR2(64),'
            || 'tenant_id_hash CHAR(64),'
            || 'recipe_subset JSON,'
            || 'status VARCHAR2(32) DEFAULT ''INGESTING'' NOT NULL,'
            || 'chunk_count NUMBER(10) DEFAULT 0 NOT NULL,'
            || 'vector_count NUMBER(10) DEFAULT 0 NOT NULL,'
            || 'metrics_json JSON,'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_chunk_sets_document_fk FOREIGN KEY (document_id) '
            || 'REFERENCES rag_documents (document_id) ON DELETE CASCADE,'
            || 'CONSTRAINT rag_chunk_sets_status_ck CHECK '
            || '(status IN (''INGESTING'', ''CHUNKED'', ''INDEXED'', ''ERROR''))'
            || ')';
    END IF;

    SELECT COUNT(*) INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_CHUNK_SETS'
      AND constraint_name = 'RAG_CHUNK_SETS_STATUS_CK';

    IF v_constraint_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_chunk_sets DROP CONSTRAINT rag_chunk_sets_status_ck';
    END IF;

    EXECUTE IMMEDIATE
        'ALTER TABLE rag_chunk_sets ADD CONSTRAINT rag_chunk_sets_status_ck CHECK '
        || '(status IN (''INGESTING'', ''CHUNKED'', ''INDEXED'', ''ERROR''))';

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes WHERE index_name = 'RAG_CHUNK_SETS_DOCUMENT_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_chunk_sets_document_idx ON rag_chunk_sets (document_id, status)';
    END IF;

    SELECT COUNT(*) INTO v_col_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_CHUNK_SETS' AND column_name = 'EXTRACTION_RECIPE_ID';

    IF v_col_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_chunk_sets ADD (extraction_recipe_id VARCHAR2(64))';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes WHERE index_name = 'RAG_CHUNK_SETS_EXTRACTION_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_chunk_sets_extraction_idx '
            || 'ON rag_chunk_sets (document_id, extraction_recipe_id)';
    END IF;

    SELECT COUNT(*) INTO v_table_count
    FROM user_tables WHERE table_name = 'RAG_DOCUMENT_EXTRACTIONS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_document_extractions ('
            || 'document_id VARCHAR2(64) NOT NULL,'
            || 'extraction_recipe_id VARCHAR2(64) NOT NULL,'
            || 'source_sha256 CHAR(64),'
            || 'tenant_id_hash CHAR(64),'
            || 'recipe_subset JSON,'
            || 'extraction_json JSON,'
            || 'status VARCHAR2(32) DEFAULT ''planned_only'' NOT NULL,'
            || 'reason VARCHAR2(2000),'
            || 'metrics_json JSON,'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_document_extractions_pk '
            || 'PRIMARY KEY (document_id, extraction_recipe_id),'
            || 'CONSTRAINT rag_doc_ext_document_fk FOREIGN KEY (document_id) '
            || 'REFERENCES rag_documents (document_id) ON DELETE CASCADE,'
            || 'CONSTRAINT rag_doc_ext_status_ck CHECK '
            || '(status IN (''not_requested'', ''planned_only'', ''materialized'', '
            || '''needs_reingest'', ''error''))'
            || ')';
    END IF;

    add_column_if_missing('RAG_DOCUMENT_EXTRACTIONS', 'DOCUMENT_ID', 'document_id VARCHAR2(64)');
    add_column_if_missing(
        'RAG_DOCUMENT_EXTRACTIONS',
        'EXTRACTION_RECIPE_ID',
        'extraction_recipe_id VARCHAR2(64)'
    );
    add_column_if_missing('RAG_DOCUMENT_EXTRACTIONS', 'SOURCE_SHA256', 'source_sha256 CHAR(64)');
    add_column_if_missing('RAG_DOCUMENT_EXTRACTIONS', 'TENANT_ID_HASH', 'tenant_id_hash CHAR(64)');
    add_column_if_missing('RAG_DOCUMENT_EXTRACTIONS', 'RECIPE_SUBSET', 'recipe_subset JSON');
    add_column_if_missing('RAG_DOCUMENT_EXTRACTIONS', 'EXTRACTION_JSON', 'extraction_json JSON');
    add_column_if_missing(
        'RAG_DOCUMENT_EXTRACTIONS',
        'STATUS',
        'status VARCHAR2(32) DEFAULT ''planned_only'''
    );
    add_column_if_missing('RAG_DOCUMENT_EXTRACTIONS', 'REASON', 'reason VARCHAR2(2000)');
    add_column_if_missing('RAG_DOCUMENT_EXTRACTIONS', 'METRICS_JSON', 'metrics_json JSON');
    add_column_if_missing(
        'RAG_DOCUMENT_EXTRACTIONS',
        'CREATED_AT',
        'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP'
    );
    add_column_if_missing(
        'RAG_DOCUMENT_EXTRACTIONS',
        'UPDATED_AT',
        'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP'
    );

    SELECT COUNT(*) INTO v_col_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_DOCUMENT_EXTRACTIONS' AND column_name = 'EXTRACTION_ID';

    IF v_col_count > 0 THEN
        BEGIN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_document_extractions '
                || 'MODIFY (extraction_id DEFAULT RAWTOHEX(SYS_GUID()))';
        EXCEPTION
            WHEN OTHERS THEN
                NULL;
        END;
    END IF;

    SELECT COUNT(*) INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_DOCUMENT_EXTRACTIONS'
      AND constraint_name = 'RAG_DOCUMENT_EXTRACTIONS_STATUS_CK';

    IF v_constraint_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_document_extractions '
            || 'DROP CONSTRAINT rag_document_extractions_status_ck';
    END IF;

    SELECT COUNT(*) INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_DOCUMENT_EXTRACTIONS'
      AND constraint_name = 'RAG_DOC_EXT_STATUS_CK';

    IF v_constraint_count = 0 THEN
        BEGIN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_document_extractions ADD CONSTRAINT rag_doc_ext_status_ck '
                || 'CHECK (status IN (''not_requested'', ''planned_only'', ''materialized'', '
                || '''needs_reingest'', ''error''))';
        EXCEPTION
            WHEN OTHERS THEN
                NULL;
        END;
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes
    WHERE index_name IN (
        'RAG_DOC_EXT_STATUS_IDX',
        'RAG_DOCUMENT_EXTRACTIONS_DOCUMENT_IDX'
    );

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_doc_ext_status_idx ON rag_document_extractions (document_id, status)';
    END IF;

    SELECT COUNT(*) INTO v_table_count
    FROM user_tables WHERE table_name = 'RAG_ARTIFACT_LAYERS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_artifact_layers ('
            || 'layer_id VARCHAR2(64) PRIMARY KEY,'
            || 'layer_kind VARCHAR2(32) NOT NULL,'
            || 'parent_chunk_set_id VARCHAR2(64) NOT NULL,'
            || 'document_id VARCHAR2(64) NOT NULL,'
            || 'tenant_id_hash CHAR(64),'
            || 'requested NUMBER(1) DEFAULT 1 NOT NULL,'
            || 'status VARCHAR2(32) DEFAULT ''planned_only'' NOT NULL,'
            || 'reason VARCHAR2(2000),'
            || 'metrics_json JSON,'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_artifact_layers_chunk_set_fk FOREIGN KEY (parent_chunk_set_id) '
            || 'REFERENCES rag_chunk_sets (chunk_set_id) ON DELETE CASCADE,'
            || 'CONSTRAINT rag_artifact_layers_document_fk FOREIGN KEY (document_id) '
            || 'REFERENCES rag_documents (document_id) ON DELETE CASCADE,'
            || 'CONSTRAINT rag_artifact_layers_requested_ck CHECK (requested IN (0, 1)),'
            || 'CONSTRAINT rag_artifact_layers_kind_ck CHECK '
            || '(layer_kind IN (''metadata'', ''graph'', ''navigation'')),'
            || 'CONSTRAINT rag_artifact_layers_status_ck CHECK '
            || '(status IN (''not_requested'', ''planned_only'', ''materialized'', '
            || '''needs_reingest'', ''error''))'
            || ')';
    END IF;

    add_column_if_missing('RAG_ARTIFACT_LAYERS', 'LAYER_ID', 'layer_id VARCHAR2(64)');
    add_column_if_missing('RAG_ARTIFACT_LAYERS', 'LAYER_KIND', 'layer_kind VARCHAR2(32)');
    add_column_if_missing(
        'RAG_ARTIFACT_LAYERS',
        'PARENT_CHUNK_SET_ID',
        'parent_chunk_set_id VARCHAR2(64)'
    );
    add_column_if_missing('RAG_ARTIFACT_LAYERS', 'DOCUMENT_ID', 'document_id VARCHAR2(64)');
    add_column_if_missing('RAG_ARTIFACT_LAYERS', 'TENANT_ID_HASH', 'tenant_id_hash CHAR(64)');
    add_column_if_missing('RAG_ARTIFACT_LAYERS', 'REQUESTED', 'requested NUMBER(1) DEFAULT 1');
    add_column_if_missing(
        'RAG_ARTIFACT_LAYERS',
        'STATUS',
        'status VARCHAR2(32) DEFAULT ''planned_only'''
    );
    add_column_if_missing('RAG_ARTIFACT_LAYERS', 'REASON', 'reason VARCHAR2(2000)');
    add_column_if_missing('RAG_ARTIFACT_LAYERS', 'METRICS_JSON', 'metrics_json JSON');
    add_column_if_missing(
        'RAG_ARTIFACT_LAYERS',
        'CREATED_AT',
        'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP'
    );
    add_column_if_missing(
        'RAG_ARTIFACT_LAYERS',
        'UPDATED_AT',
        'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP'
    );

    SELECT COUNT(*) INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_ARTIFACT_LAYERS'
      AND constraint_name = 'RAG_ARTIFACT_LAYERS_STATUS_CK';

    IF v_constraint_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_artifact_layers '
            || 'DROP CONSTRAINT rag_artifact_layers_status_ck';
    END IF;

    SELECT COUNT(*) INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_ARTIFACT_LAYERS'
      AND constraint_name = 'RAG_ARTIFACT_LAYERS_KIND_CK';

    IF v_constraint_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_artifact_layers '
            || 'DROP CONSTRAINT rag_artifact_layers_kind_ck';
    END IF;

    SELECT COUNT(*) INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_ARTIFACT_LAYERS'
      AND constraint_name = 'RAG_ARTIFACT_LAYERS_STATUS_CK';

    IF v_constraint_count = 0 THEN
        BEGIN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_artifact_layers ADD CONSTRAINT rag_artifact_layers_status_ck '
                || 'CHECK (status IN (''not_requested'', ''planned_only'', ''materialized'', '
                || '''needs_reingest'', ''error''))';
        EXCEPTION
            WHEN OTHERS THEN
                NULL;
        END;
    END IF;

    SELECT COUNT(*) INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_ARTIFACT_LAYERS'
      AND constraint_name = 'RAG_ARTIFACT_LAYERS_KIND_CK';

    IF v_constraint_count = 0 THEN
        BEGIN
            EXECUTE IMMEDIATE
                'ALTER TABLE rag_artifact_layers ADD CONSTRAINT rag_artifact_layers_kind_ck '
                || 'CHECK (layer_kind IN (''metadata'', ''graph'', ''navigation''))';
        EXCEPTION
            WHEN OTHERS THEN
                NULL;
        END;
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes WHERE index_name = 'RAG_ARTIFACT_LAYERS_PARENT_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_artifact_layers_parent_idx '
            || 'ON rag_artifact_layers (parent_chunk_set_id, layer_kind, status)';
    END IF;

    SELECT COUNT(*) INTO v_table_count
    FROM user_tables WHERE table_name = 'RAG_KB_CHUNK_SET_BINDINGS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_kb_chunk_set_bindings ('
            || 'knowledge_base_id VARCHAR2(64) NOT NULL,'
            || 'document_id VARCHAR2(64) NOT NULL,'
            || 'chunk_set_id VARCHAR2(64) NOT NULL,'
            || 'tenant_id_hash CHAR(64),'
            || 'is_serving NUMBER(1) DEFAULT 1 NOT NULL,'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_kb_chunk_set_bindings_pk '
            || 'PRIMARY KEY (knowledge_base_id, document_id, chunk_set_id),'
            || 'CONSTRAINT rag_kb_cs_bind_cs_fk FOREIGN KEY (chunk_set_id) '
            || 'REFERENCES rag_chunk_sets (chunk_set_id) ON DELETE CASCADE,'
            || 'CONSTRAINT rag_kb_cs_bind_serving_ck CHECK (is_serving IN (0, 1))'
            || ')';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes WHERE index_name = 'RAG_KB_CS_BIND_CS_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_kb_cs_bind_cs_idx ON rag_kb_chunk_set_bindings (chunk_set_id)';
    END IF;

    SELECT COUNT(*) INTO v_col_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_CHUNKS' AND column_name = 'CHUNK_SET_ID';

    IF v_col_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_chunks ADD (chunk_set_id VARCHAR2(64))';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes WHERE index_name = 'RAG_CHUNKS_CHUNK_SET_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_chunks_chunk_set_idx ON rag_chunks (chunk_set_id, chunk_index)';
    END IF;
END;
/

-- migration: 20260621_002_document_extractions
DECLARE
    v_table_count NUMBER;
    v_index_count NUMBER;
    v_col_count   NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables WHERE table_name = 'RAG_DOCUMENT_EXTRACTIONS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_document_extractions ('
            || 'extraction_id VARCHAR2(64) PRIMARY KEY,'
            || 'document_id VARCHAR2(64) NOT NULL,'
            || 'tenant_id_hash CHAR(64),'
            || 'recipe_subset JSON,'
            || 'extraction_json JSON,'
            || 'status VARCHAR2(32) DEFAULT ''EXTRACTING'' NOT NULL,'
            || 'quality_json JSON,'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_document_extractions_document_fk FOREIGN KEY (document_id) '
            || 'REFERENCES rag_documents (document_id) ON DELETE CASCADE,'
            || 'CONSTRAINT rag_document_extractions_status_ck CHECK '
            || '(status IN (''EXTRACTING'', ''EXTRACTED'', ''ERROR''))'
            || ')';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes WHERE index_name = 'RAG_DOCUMENT_EXTRACTIONS_DOCUMENT_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_document_extractions_document_idx '
            || 'ON rag_document_extractions (document_id, status)';
    END IF;

    SELECT COUNT(*) INTO v_col_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_CHUNK_SETS' AND column_name = 'EXTRACTION_ID';

    IF v_col_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_chunk_sets ADD (extraction_id VARCHAR2(64))';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes WHERE index_name = 'RAG_CHUNK_SETS_EXTRACTION_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_chunk_sets_extraction_idx ON rag_chunk_sets (extraction_id)';
    END IF;
END;
/

-- migration: 20260623_001_nullable_chunk_embeddings
DECLARE
    v_nullable VARCHAR2(1);
BEGIN
    SELECT nullable INTO v_nullable
    FROM user_tab_columns
    WHERE table_name = 'RAG_CHUNKS'
      AND column_name = 'EMBEDDING';

    IF v_nullable = 'N' THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_chunks MODIFY (embedding NULL)';
    END IF;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        NULL;
END;
/

-- migration: 20260625_001_chunks_text_world_lexer
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM ctx_user_preferences
    WHERE pre_name = 'RAG_TEXT_WORLD_LEXER'
      AND pre_class = 'LEXER';

    IF v_count = 0 THEN
        CTX_DDL.CREATE_PREFERENCE('RAG_TEXT_WORLD_LEXER', 'WORLD_LEXER');
    END IF;
END;
/

DECLARE
    v_count NUMBER;
    PROCEDURE add_stopword(p_word VARCHAR2) IS
    BEGIN
        CTX_DDL.ADD_STOPWORD('RAG_TEXT_STOPLIST', p_word);
    EXCEPTION
        WHEN OTHERS THEN
            NULL;
    END;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM ctx_user_stoplists
    WHERE spl_name = 'RAG_TEXT_STOPLIST';

    IF v_count = 0 THEN
        CTX_DDL.CREATE_STOPLIST('RAG_TEXT_STOPLIST', 'BASIC_STOPLIST');
    END IF;

    add_stopword('の');
    add_stopword('は');
    add_stopword('が');
    add_stopword('を');
    add_stopword('に');
    add_stopword('へ');
    add_stopword('で');
    add_stopword('と');
    add_stopword('も');
    add_stopword('か');
    add_stopword('です');
    add_stopword('ます');
    add_stopword('なん');
    add_stopword('んで');
END;
/

DECLARE
    v_table_count NUMBER;
    v_index_count NUMBER;
    v_target_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_CHUNKS';

    IF v_table_count > 0 THEN
        SELECT COUNT(*) INTO v_index_count
        FROM user_indexes
        WHERE index_name = 'RAG_CHUNKS_TEXT_IDX';

        IF v_index_count > 0 THEN
            SELECT COUNT(*) INTO v_target_count
            FROM ctx_user_index_objects o
            JOIN ctx_user_indexes i
              ON i.idx_name = o.ixo_index_name
            WHERE o.ixo_index_name = 'RAG_CHUNKS_TEXT_IDX'
              AND o.ixo_class = 'LEXER'
              AND o.ixo_object = 'WORLD_LEXER'
              AND i.idx_sync_type = 'ON COMMIT';

            IF v_target_count = 0 THEN
                EXECUTE IMMEDIATE 'DROP INDEX rag_chunks_text_idx';
                v_index_count := 0;
            END IF;
        END IF;

        IF v_index_count = 0 THEN
            EXECUTE IMMEDIATE
                'CREATE INDEX rag_chunks_text_idx '
                || 'ON rag_chunks (chunk_text) '
                || 'INDEXTYPE IS CTXSYS.CONTEXT '
                || 'PARAMETERS (''LEXER RAG_TEXT_WORLD_LEXER STOPLIST RAG_TEXT_STOPLIST SYNC (ON COMMIT)'')';
        END IF;
    END IF;
END;
/

-- migration: 20260625_002_preprocess_artifact
DECLARE
    v_column_count NUMBER;
    v_constraint_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_DOCUMENTS'
      AND column_name = 'PREPROCESS_ARTIFACT';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_documents ADD (preprocess_artifact JSON)';
    END IF;

    SELECT COUNT(*)
    INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_DOCUMENTS'
      AND constraint_name = 'RAG_DOCUMENTS_STATUS_CK';

    IF v_constraint_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_documents DROP CONSTRAINT rag_documents_status_ck';
    END IF;

    EXECUTE IMMEDIATE
        'ALTER TABLE rag_documents ADD CONSTRAINT '
        || 'rag_documents_status_ck CHECK '
        || '(status IN (''UPLOADED'', ''PREPROCESSING'', ''PREPROCESSED'', '
        || '''INGESTING'', ''REVIEW'', ''CHUNKING'', ''CHUNKED'', ''INDEXING'', '
        || '''INDEXED'', ''ERROR''))';
END;
/

-- migration: 20260627_001_documents_preprocessed_status
DECLARE
    v_constraint_count NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_DOCUMENTS'
      AND constraint_name = 'RAG_DOCUMENTS_STATUS_CK';

    IF v_constraint_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_documents DROP CONSTRAINT rag_documents_status_ck';
    END IF;

    EXECUTE IMMEDIATE
        'ALTER TABLE rag_documents ADD CONSTRAINT '
        || 'rag_documents_status_ck CHECK '
        || '(status IN (''UPLOADED'', ''PREPROCESSING'', ''PREPROCESSED'', '
        || '''INGESTING'', ''REVIEW'', ''CHUNKING'', ''CHUNKED'', ''INDEXING'', '
        || '''INDEXED'', ''ERROR''))';
END;
/

-- migration: 20260629_001_chunk_sets_serving
DECLARE
    v_col_count   NUMBER;
    v_constraint_count NUMBER;
    v_index_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_col_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_CHUNK_SETS' AND column_name = 'IS_SERVING';

    IF v_col_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_chunk_sets ADD (is_serving NUMBER(1) DEFAULT 1 NOT NULL)';
        -- backfill は列追加直後の一度だけ。動的 SQL(EXECUTE IMMEDIATE)にしないと新列を
        -- 静的参照できず PL/SQL コンパイルに失敗する。同一文書で別 chunk_set が serving
        -- binding を持ち自分は持たない chunk_set だけ 0、それ以外は既定 1(配信を残す安全側)。
        EXECUTE IMMEDIATE
            'UPDATE rag_chunk_sets cs SET is_serving = 0 '
            || 'WHERE EXISTS (SELECT 1 FROM rag_kb_chunk_set_bindings b '
            || 'WHERE b.document_id = cs.document_id AND b.is_serving = 1 '
            || 'AND b.chunk_set_id <> cs.chunk_set_id) '
            || 'AND NOT EXISTS (SELECT 1 FROM rag_kb_chunk_set_bindings b2 '
            || 'WHERE b2.chunk_set_id = cs.chunk_set_id AND b2.is_serving = 1)';
    END IF;

    SELECT COUNT(*) INTO v_constraint_count
    FROM user_constraints
    WHERE table_name = 'RAG_CHUNK_SETS'
      AND constraint_name = 'RAG_CHUNK_SETS_SERVING_CK';

    IF v_constraint_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_chunk_sets ADD CONSTRAINT '
            || 'rag_chunk_sets_serving_ck CHECK (is_serving IN (0, 1))';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes WHERE index_name = 'RAG_CHUNK_SETS_SERVING_IDX';

    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_chunk_sets_serving_idx '
            || 'ON rag_chunk_sets (document_id, is_serving)';
    END IF;
END;
/

-- migration: 20260629_002_drop_kb_chunk_set_bindings
DECLARE
    v_table_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables WHERE table_name = 'RAG_KB_CHUNK_SET_BINDINGS';

    IF v_table_count > 0 THEN
        EXECUTE IMMEDIATE 'DROP TABLE rag_kb_chunk_set_bindings';
    END IF;
END;
/

-- migration: 20260629_003_ingestion_jobs_settings_overrides
DECLARE
    v_column_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_INGESTION_JOBS'
      AND column_name = 'SETTINGS_OVERRIDES';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_ingestion_jobs ADD (settings_overrides JSON)';
    END IF;
END;
/

-- migration: 20260629_004_documents_processing_config
DECLARE
    v_column_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_DOCUMENTS'
      AND column_name = 'PROCESSING_CONFIG';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_documents ADD (processing_config JSON)';
    END IF;
END;
/

-- migration: 20260630_001_default_knowledge_base_name
BEGIN
    UPDATE rag_knowledge_bases current_default
    SET
        name = 'DEFAULT-' || current_default.knowledge_base_id,
        updated_at = SYSTIMESTAMP
    WHERE LOWER(current_default.name) = 'default'
      AND EXISTS (
          SELECT 1
          FROM rag_knowledge_bases legacy_default
          WHERE legacy_default.name = '既定ナレッジベース'
            AND NVL(legacy_default.tenant_id_hash, '__GLOBAL__') =
                NVL(current_default.tenant_id_hash, '__GLOBAL__')
      );

    UPDATE rag_knowledge_bases
    SET
        name = 'DEFAULT',
        updated_at = SYSTIMESTAMP
    WHERE name = '既定ナレッジベース';
END;
/
COMMIT;

-- migration: 20260630_002_default_business_view
UPDATE rag_business_views bv
SET
    status = 'ACTIVE',
    view_config = (
        SELECT JSON_MERGEPATCH(
            COALESCE(bv.view_config, JSON_OBJECT('version' VALUE 1 RETURNING JSON)),
            JSON_OBJECT(
                'knowledge_base_ids' VALUE
                    JSON_ARRAY(kb.knowledge_base_id RETURNING JSON)
                RETURNING JSON
            )
            RETURNING JSON
        )
        FROM rag_knowledge_bases kb
        WHERE LOWER(kb.name) = 'default'
          AND NVL(kb.tenant_id_hash, '__GLOBAL__') =
              NVL(bv.tenant_id_hash, '__GLOBAL__')
    ),
    updated_at = SYSTIMESTAMP,
    archived_at = NULL
WHERE LOWER(bv.name) = 'default'
  AND EXISTS (
      SELECT 1
      FROM rag_knowledge_bases kb
      WHERE LOWER(kb.name) = 'default'
        AND NVL(kb.tenant_id_hash, '__GLOBAL__') =
            NVL(bv.tenant_id_hash, '__GLOBAL__')
  );

INSERT INTO rag_business_views (
    business_view_id,
    tenant_id_hash,
    name,
    description,
    status,
    view_config,
    created_at,
    updated_at,
    archived_at
)
SELECT
    LOWER(RAWTOHEX(SYS_GUID())),
    kb.tenant_id_hash,
    'DEFAULT',
    NULL,
    'ACTIVE',
    JSON_OBJECT(
        'version' VALUE 1,
        'knowledge_base_ids' VALUE JSON_ARRAY(kb.knowledge_base_id RETURNING JSON),
        'query' VALUE JSON_OBJECT(RETURNING JSON),
        'system_prompt' VALUE NULL,
        'default_language' VALUE NULL,
        'serving_mode' VALUE 'single'
        RETURNING JSON
    ),
    SYSTIMESTAMP,
    SYSTIMESTAMP,
    NULL
FROM rag_knowledge_bases kb
WHERE LOWER(kb.name) = 'default'
  AND kb.status = 'ACTIVE'
  AND NOT EXISTS (
      SELECT 1
      FROM rag_business_views bv
      WHERE LOWER(bv.name) = 'default'
        AND NVL(bv.tenant_id_hash, '__GLOBAL__') =
            NVL(kb.tenant_id_hash, '__GLOBAL__')
  );

COMMIT;

-- migration: 20260630_003_document_recipes
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables
    WHERE table_name = 'RAG_DOCUMENT_RECIPES';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE q'[
            CREATE TABLE rag_document_recipes (
                recipe_id VARCHAR2(64) PRIMARY KEY,
                document_id VARCHAR2(64) NOT NULL,
                slot_no NUMBER(1) NOT NULL,
                tenant_id_hash CHAR(64),
                processing_config JSON,
                status VARCHAR2(32) DEFAULT 'UPLOADED' NOT NULL,
                failed_phase VARCHAR2(16),
                preprocess_artifact JSON,
                active_extraction_recipe_id VARCHAR2(64),
                config_revision NUMBER(10) DEFAULT 1 NOT NULL,
                materialized_revision NUMBER(10),
                error_message VARCHAR2(2000),
                started_at TIMESTAMP WITH TIME ZONE,
                finished_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
                CONSTRAINT rag_document_recipes_document_fk
                    FOREIGN KEY (document_id) REFERENCES rag_documents (document_id)
                    ON DELETE CASCADE,
                CONSTRAINT rag_document_recipes_slot_ck CHECK (slot_no BETWEEN 1 AND 3),
                CONSTRAINT rag_document_recipes_status_ck CHECK (
                    status IN ('UPLOADED','PREPROCESSING','PREPROCESSED','INGESTING','REVIEW',
                               'CHUNKING','CHUNKED','INDEXING','INDEXED','ERROR')
                ),
                CONSTRAINT rag_document_recipes_phase_ck CHECK (
                    failed_phase IS NULL OR failed_phase IN ('PREPROCESS','EXTRACT','CHUNK','INDEX')
                ),
                CONSTRAINT rag_document_recipes_revision_ck CHECK (
                    config_revision >= 1
                    AND (materialized_revision IS NULL OR materialized_revision >= 1)
                ),
                CONSTRAINT rag_document_recipes_slot_uq UNIQUE (document_id, slot_no)
            )
        ]';
    END IF;
END;
/

MERGE INTO rag_document_recipes r
USING (
    SELECT
        LOWER(RAWTOHEX(STANDARD_HASH(document_id || ':recipe:1', 'SHA256'))) AS recipe_id,
        document_id,
        tenant_id_hash,
        processing_config,
        status,
        preprocess_artifact,
        error_message,
        uploaded_at,
        indexed_at
    FROM rag_documents
) d
ON (r.document_id = d.document_id AND r.slot_no = 1)
WHEN NOT MATCHED THEN INSERT (
    recipe_id, document_id, slot_no, tenant_id_hash, processing_config, status,
    preprocess_artifact, config_revision, materialized_revision, error_message,
    created_at, updated_at, finished_at
) VALUES (
    d.recipe_id, d.document_id, 1, d.tenant_id_hash, d.processing_config, d.status,
    d.preprocess_artifact, 1, CASE WHEN d.status = 'INDEXED' THEN 1 END, d.error_message,
    d.uploaded_at, SYSTIMESTAMP, d.indexed_at
);

DECLARE
    PROCEDURE add_column_if_missing(
        p_table_name IN VARCHAR2,
        p_column_name IN VARCHAR2,
        p_definition IN VARCHAR2
    ) IS
        v_count NUMBER;
    BEGIN
        SELECT COUNT(*) INTO v_count FROM user_tab_columns
        WHERE table_name = p_table_name AND column_name = p_column_name;
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE ' || p_table_name || ' ADD (' || p_definition || ')';
        END IF;
    END;
BEGIN
    add_column_if_missing('RAG_CHUNK_SETS', 'RECIPE_ID', 'recipe_id VARCHAR2(64)');
    add_column_if_missing(
        'RAG_CHUNK_SETS', 'IS_ACTIVE', 'is_active NUMBER(1) DEFAULT 0 NOT NULL'
    );
    add_column_if_missing('RAG_INGESTION_JOBS', 'RECIPE_ID', 'recipe_id VARCHAR2(64)');
    add_column_if_missing(
        'RAG_INGESTION_JOBS', 'RECIPE_REVISION', 'recipe_revision NUMBER(10)'
    );
    add_column_if_missing('RAG_INGESTION_SEGMENTS', 'RECIPE_ID', 'recipe_id VARCHAR2(64)');
    add_column_if_missing('RAG_GRAPH_ENTITIES', 'CHUNK_SET_ID', 'chunk_set_id VARCHAR2(64)');
    add_column_if_missing(
        'RAG_GRAPH_RELATIONSHIPS', 'CHUNK_SET_ID', 'chunk_set_id VARCHAR2(64)'
    );
    add_column_if_missing('RAG_GRAPH_CLAIMS', 'CHUNK_SET_ID', 'chunk_set_id VARCHAR2(64)');
    add_column_if_missing(
        'RAG_GRAPH_COMMUNITY_SUMMARIES', 'CHUNK_SET_ID', 'chunk_set_id VARCHAR2(64)'
    );
    add_column_if_missing(
        'RAG_GRAPH_ENTITY_CHUNKS', 'CHUNK_SET_ID', 'chunk_set_id VARCHAR2(64)'
    );
END;
/

MERGE INTO rag_document_recipes r
USING (
    SELECT
        LOWER(RAWTOHEX(STANDARD_HASH(
            ranked.document_id || ':recipe:' || TO_CHAR(ranked.slot_no), 'SHA256'
        ))) AS recipe_id,
        ranked.document_id,
        ranked.slot_no,
        ranked.tenant_id_hash,
        JSON_QUERY(ranked.recipe_subset, '$.processing_config' RETURNING CLOB) AS processing_config,
        ranked.extraction_recipe_id,
        ranked.updated_at
    FROM (
        SELECT
            cs.document_id,
            cs.tenant_id_hash,
            cs.recipe_subset,
            cs.extraction_recipe_id,
            cs.updated_at,
            ROW_NUMBER() OVER (
                PARTITION BY cs.document_id
                ORDER BY cs.updated_at DESC, cs.chunk_set_id DESC
            ) + 1 AS slot_no
        FROM rag_chunk_sets cs
        WHERE cs.status = 'INDEXED' AND cs.is_serving = 0
    ) ranked
    WHERE ranked.slot_no <= 3
) candidate
ON (r.document_id = candidate.document_id AND r.slot_no = candidate.slot_no)
WHEN NOT MATCHED THEN INSERT (
    recipe_id, document_id, slot_no, tenant_id_hash, processing_config, status,
    active_extraction_recipe_id, config_revision, materialized_revision,
    created_at, updated_at, finished_at
) VALUES (
    candidate.recipe_id, candidate.document_id, candidate.slot_no, candidate.tenant_id_hash,
    candidate.processing_config, 'INDEXED', candidate.extraction_recipe_id, 1, 1,
    candidate.updated_at, SYSTIMESTAMP, candidate.updated_at
);

MERGE INTO rag_chunk_sets cs
USING (
    SELECT ranked.chunk_set_id, r.recipe_id
    FROM (
        SELECT
            candidate.chunk_set_id,
            candidate.document_id,
            ROW_NUMBER() OVER (
                PARTITION BY candidate.document_id
                ORDER BY candidate.updated_at DESC, candidate.chunk_set_id DESC
            ) AS candidate_rank
        FROM rag_chunk_sets candidate
        WHERE candidate.status = 'INDEXED' AND candidate.is_serving = 0
    ) ranked
    JOIN rag_document_recipes r
      ON r.document_id = ranked.document_id
     AND r.slot_no = ranked.candidate_rank + 1
    WHERE ranked.candidate_rank <= 2
) mapped
ON (cs.chunk_set_id = mapped.chunk_set_id)
WHEN MATCHED THEN UPDATE SET cs.recipe_id = mapped.recipe_id, cs.is_active = 1;

UPDATE rag_chunk_sets cs
SET
    recipe_id = (
        SELECT r.recipe_id FROM rag_document_recipes r
        WHERE r.document_id = cs.document_id AND r.slot_no = 1
    ),
    is_active = CASE WHEN cs.status = 'INDEXED' THEN 1 ELSE 0 END
WHERE cs.recipe_id IS NULL AND cs.is_serving = 1;

UPDATE rag_graph_entity_chunks ec
SET chunk_set_id = (
    SELECT c.chunk_set_id FROM rag_chunks c WHERE c.chunk_id = ec.chunk_id
)
WHERE ec.chunk_set_id IS NULL;

UPDATE rag_graph_claims claim
SET chunk_set_id = (
    SELECT c.chunk_set_id FROM rag_chunks c WHERE c.chunk_id = claim.source_chunk_id
)
WHERE claim.chunk_set_id IS NULL;

UPDATE rag_graph_entities entity
SET chunk_set_id = (
    SELECT MIN(ec.chunk_set_id)
    FROM rag_graph_entity_chunks ec
    WHERE ec.entity_id = entity.entity_id
)
WHERE entity.chunk_set_id IS NULL;

UPDATE rag_graph_relationships relationship
SET chunk_set_id = (
    SELECT entity.chunk_set_id
    FROM rag_graph_entities entity
    WHERE entity.entity_id = relationship.source_entity_id
)
WHERE relationship.chunk_set_id IS NULL;

UPDATE rag_graph_community_summaries community
SET chunk_set_id = (
    SELECT MIN(cs.chunk_set_id)
    FROM rag_chunk_sets cs
    WHERE cs.is_active = 1
      AND cs.status = 'INDEXED'
      AND JSON_EXISTS(
          community.source_document_ids,
          '$[*]?(@ == $document_id)'
          PASSING cs.document_id AS "document_id"
      )
)
WHERE community.chunk_set_id IS NULL;

DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_indexes
    WHERE index_name = 'RAG_GRAPH_ENTITIES_CHUNK_SET_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_graph_entities_chunk_set_idx '
            || 'ON rag_graph_entities (chunk_set_id)';
    END IF;

    SELECT COUNT(*) INTO v_count FROM user_indexes
    WHERE index_name = 'RAG_GRAPH_COMMUNITY_CHUNK_SET_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_graph_community_chunk_set_idx '
            || 'ON rag_graph_community_summaries (chunk_set_id)';
    END IF;

    SELECT COUNT(*) INTO v_count FROM user_indexes
    WHERE index_name = 'RAG_INGESTION_JOBS_RECIPE_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_ingestion_jobs_recipe_idx '
            || 'ON rag_ingestion_jobs (recipe_id, status, queued_at DESC)';
    END IF;

    SELECT COUNT(*) INTO v_count FROM user_indexes
    WHERE index_name = 'RAG_INGESTION_SEGMENTS_RECIPE_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_ingestion_segments_recipe_idx '
            || 'ON rag_ingestion_segments (recipe_id, status, updated_at DESC)';
    END IF;

    SELECT COUNT(*) INTO v_count FROM user_indexes
    WHERE index_name = 'RAG_CHUNK_SETS_RECIPE_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_chunk_sets_recipe_idx '
            || 'ON rag_chunk_sets (recipe_id, status, updated_at DESC)';
    END IF;

    SELECT COUNT(*) INTO v_count FROM user_indexes
    WHERE index_name = 'RAG_CHUNK_SETS_RECIPE_ACTIVE_UIDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE UNIQUE INDEX rag_chunk_sets_recipe_active_uidx '
            || 'ON rag_chunk_sets (CASE WHEN is_active = 1 THEN recipe_id END)';
    END IF;

    SELECT COUNT(*) INTO v_count FROM user_constraints
    WHERE constraint_name = 'RAG_CHUNK_SETS_RECIPE_FK';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_chunk_sets ADD CONSTRAINT rag_chunk_sets_recipe_fk '
            || 'FOREIGN KEY (recipe_id) REFERENCES rag_document_recipes (recipe_id) '
            || 'ON DELETE CASCADE';
    END IF;

    SELECT COUNT(*) INTO v_count FROM user_constraints
    WHERE constraint_name = 'RAG_CHUNK_SETS_ACTIVE_CK';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_chunk_sets ADD CONSTRAINT rag_chunk_sets_active_ck '
            || 'CHECK (is_active IN (0, 1))';
    END IF;

    SELECT COUNT(*) INTO v_count FROM user_constraints
    WHERE constraint_name = 'RAG_INGESTION_JOBS_RECIPE_FK';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_ingestion_jobs ADD CONSTRAINT rag_ingestion_jobs_recipe_fk '
            || 'FOREIGN KEY (recipe_id) REFERENCES rag_document_recipes (recipe_id) '
            || 'ON DELETE CASCADE';
    END IF;

    SELECT COUNT(*) INTO v_count FROM user_constraints
    WHERE constraint_name = 'RAG_INGESTION_SEGMENTS_RECIPE_FK';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_ingestion_segments '
            || 'ADD CONSTRAINT rag_ingestion_segments_recipe_fk '
            || 'FOREIGN KEY (recipe_id) REFERENCES rag_document_recipes (recipe_id) '
            || 'ON DELETE CASCADE';
    END IF;
END;
/

COMMIT;

-- migration: 20260701_001_general_feedback
DECLARE
    v_count NUMBER;

    PROCEDURE add_column_if_missing(p_name VARCHAR2, p_definition VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO v_count
        FROM user_tab_columns
        WHERE table_name = 'RAG_CITATION_FEEDBACK' AND column_name = UPPER(p_name);
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE rag_citation_feedback ADD (' || p_definition || ')';
        END IF;
    END;

    PROCEDURE drop_constraint_if_exists(p_name VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO v_count
        FROM user_constraints
        WHERE table_name = 'RAG_CITATION_FEEDBACK' AND constraint_name = UPPER(p_name);
        IF v_count > 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE rag_citation_feedback DROP CONSTRAINT ' || p_name;
        END IF;
    END;

    PROCEDURE add_constraint_if_missing(p_name VARCHAR2, p_definition VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO v_count
        FROM user_constraints
        WHERE table_name = 'RAG_CITATION_FEEDBACK' AND constraint_name = UPPER(p_name);
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE rag_citation_feedback ADD CONSTRAINT '
                || p_name || ' ' || p_definition;
        END IF;
    END;

    PROCEDURE add_index_if_missing(p_name VARCHAR2, p_columns VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = UPPER(p_name);
        IF v_count = 0 THEN
            EXECUTE IMMEDIATE 'CREATE INDEX ' || p_name
                || ' ON rag_citation_feedback (' || p_columns || ')';
        END IF;
    END;
BEGIN
    add_column_if_missing('BUSINESS_VIEW_ID', 'business_view_id VARCHAR2(64)');
    add_column_if_missing(
        'TARGET_TYPE',
        'target_type VARCHAR2(16) DEFAULT ''citation'' NOT NULL'
    );
    add_column_if_missing('SOURCE_SURFACE', 'source_surface VARCHAR2(16)');

    FOR column_row IN (
        SELECT column_name
        FROM user_tab_columns
        WHERE table_name = 'RAG_CITATION_FEEDBACK'
          AND column_name IN ('DOCUMENT_ID', 'CHUNK_ID')
          AND nullable = 'N'
    ) LOOP
        EXECUTE IMMEDIATE 'ALTER TABLE rag_citation_feedback MODIFY ('
            || column_row.column_name || ' NULL)';
    END LOOP;

    UPDATE rag_citation_feedback
    SET reason = NULL
    WHERE rating = 'helpful' AND reason IS NOT NULL;

    UPDATE rag_citation_feedback
    SET reason = 'answer_untrusted'
    WHERE rating = 'not_helpful' AND reason IS NULL;

    drop_constraint_if_exists('RAG_CITATION_FEEDBACK_REASON_CK');
    add_constraint_if_missing(
        'RAG_CITATION_FEEDBACK_REASON_CK',
        'CHECK ((rating = ''helpful'' AND reason IS NULL) OR '
        || '(rating = ''not_helpful'' AND ('
        || '(target_type = ''answer'' AND reason IN ('
        || '''incorrect'', ''incomplete'', ''not_relevant'', ''answer_untrusted'')) OR '
        || '(target_type = ''citation'' AND reason IN ('
        || '''missing_evidence'', ''not_relevant'', ''answer_untrusted'')))))'
    );
    add_constraint_if_missing(
        'RAG_CITATION_FEEDBACK_TARGET_CK',
        'CHECK (target_type IN (''answer'', ''citation''))'
    );
    add_constraint_if_missing(
        'RAG_CITATION_FEEDBACK_SURFACE_CK',
        'CHECK (source_surface IS NULL OR source_surface IN (''search'', ''chat''))'
    );
    add_constraint_if_missing(
        'RAG_CITATION_FEEDBACK_SHAPE_CK',
        'CHECK ((target_type = ''answer'' AND document_id IS NULL AND chunk_id IS NULL) '
        || 'OR (target_type = ''citation'' AND document_id IS NOT NULL '
        || 'AND chunk_id IS NOT NULL))'
    );

    add_index_if_missing(
        'RAG_FEEDBACK_BUSINESS_CREATED_IDX',
        'tenant_id_hash, business_view_id, created_at DESC'
    );
    add_index_if_missing(
        'RAG_FEEDBACK_USER_TRACE_IDX',
        'tenant_id_hash, user_id_hash, trace_id, created_at DESC'
    );
END;
/

-- migration: 20260701_002_conversation_titles
MERGE INTO rag_conversations c
USING (
    SELECT
        conversation_id,
        CASE
            WHEN LENGTH(normalized_title) > 80
            THEN SUBSTR(normalized_title, 1, 79) || '…'
            ELSE normalized_title
        END AS title
    FROM (
        SELECT
            conversation_id,
            NULLIF(
                REGEXP_REPLACE(
                    TRIM(DBMS_LOB.SUBSTR(content, 400, 1)),
                    '[[:space:]]+',
                    ' '
                ),
                ''
            ) AS normalized_title
        FROM (
            SELECT
                m.conversation_id,
                m.content,
                ROW_NUMBER() OVER (
                    PARTITION BY m.conversation_id
                    ORDER BY m.created_at, m.message_id
                ) AS message_order
            FROM rag_messages m
            WHERE m.role = 'USER'
        ) ranked_messages
        WHERE message_order = 1
    ) first_messages
    WHERE normalized_title IS NOT NULL
) first_message
ON (c.conversation_id = first_message.conversation_id)
WHEN MATCHED THEN UPDATE SET c.title = first_message.title
WHERE c.title IS NULL;

COMMIT;

-- migration: 20260702_001_chunk_search_text
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM ctx_user_preferences
    WHERE pre_name = 'RAG_TEXT_WORLD_LEXER'
      AND pre_class = 'LEXER';

    IF v_count = 0 THEN
        CTX_DDL.CREATE_PREFERENCE('RAG_TEXT_WORLD_LEXER', 'WORLD_LEXER');
    END IF;
END;
/

DECLARE
    v_count NUMBER;
    PROCEDURE add_stopword(p_word VARCHAR2) IS
    BEGIN
        CTX_DDL.ADD_STOPWORD('RAG_TEXT_STOPLIST', p_word);
    EXCEPTION
        WHEN OTHERS THEN
            NULL;
    END;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM ctx_user_stoplists
    WHERE spl_name = 'RAG_TEXT_STOPLIST';

    IF v_count = 0 THEN
        CTX_DDL.CREATE_STOPLIST('RAG_TEXT_STOPLIST', 'BASIC_STOPLIST');
    END IF;

    add_stopword('の');
    add_stopword('は');
    add_stopword('が');
    add_stopword('を');
    add_stopword('に');
    add_stopword('へ');
    add_stopword('で');
    add_stopword('と');
    add_stopword('も');
    add_stopword('か');
    add_stopword('です');
    add_stopword('ます');
    add_stopword('なん');
    add_stopword('んで');
END;
/

DECLARE
    v_table_count NUMBER;
    v_col_count NUMBER;
    v_nullable VARCHAR2(1);
    v_index_count NUMBER;
    v_target_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_CHUNKS';

    IF v_table_count > 0 THEN
        SELECT COUNT(*) INTO v_col_count
        FROM user_tab_columns
        WHERE table_name = 'RAG_CHUNKS'
          AND column_name = 'SEARCH_TEXT';

        IF v_col_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE rag_chunks ADD (search_text CLOB)';
        END IF;

        EXECUTE IMMEDIATE
            'UPDATE rag_chunks SET search_text = chunk_text WHERE search_text IS NULL';

        SELECT nullable INTO v_nullable
        FROM user_tab_columns
        WHERE table_name = 'RAG_CHUNKS'
          AND column_name = 'SEARCH_TEXT';

        IF v_nullable = 'Y' THEN
            EXECUTE IMMEDIATE 'ALTER TABLE rag_chunks MODIFY (search_text NOT NULL)';
        END IF;

        SELECT COUNT(*) INTO v_index_count
        FROM user_indexes
        WHERE index_name = 'RAG_CHUNKS_TEXT_IDX';

        IF v_index_count > 0 THEN
            SELECT COUNT(*) INTO v_target_count
            FROM user_ind_columns
            WHERE index_name = 'RAG_CHUNKS_TEXT_IDX'
              AND column_name = 'SEARCH_TEXT';

            IF v_target_count = 0 THEN
                EXECUTE IMMEDIATE 'DROP INDEX rag_chunks_text_idx';
                v_index_count := 0;
            END IF;
        END IF;

        IF v_index_count = 0 THEN
            EXECUTE IMMEDIATE
                'CREATE INDEX rag_chunks_text_idx '
                || 'ON rag_chunks (search_text) '
                || 'INDEXTYPE IS CTXSYS.CONTEXT '
                || 'PARAMETERS (''LEXER RAG_TEXT_WORLD_LEXER STOPLIST RAG_TEXT_STOPLIST SYNC (ON COMMIT)'')';
        END IF;
    END IF;
END;
/

-- migration: 20260703_001_generation_settings
DECLARE
    v_table_count NUMBER;
    v_index_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_PROMPT_VERSIONS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_prompt_versions ('
            || 'version_id VARCHAR2(64) PRIMARY KEY,'
            || 'name VARCHAR2(120) NOT NULL,'
            || 'system_prompt CLOB NOT NULL,'
            || 'note VARCHAR2(2000),'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'created_by_hash CHAR(64)'
            || ')';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_PROMPT_VERSIONS_CREATED_IDX';
    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_prompt_versions_created_idx '
            || 'ON rag_prompt_versions (created_at DESC, version_id DESC)';
    END IF;

    SELECT COUNT(*) INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_GENERATION_SETTINGS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_generation_settings ('
            || 'settings_key VARCHAR2(32) PRIMARY KEY,'
            || 'generation_profile VARCHAR2(64) NOT NULL,'
            || 'active_prompt_version_id VARCHAR2(64),'
            || 'revision NUMBER(19) DEFAULT 1 NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'updated_by_hash CHAR(64),'
            || 'CONSTRAINT rag_generation_settings_singleton_ck '
            || 'CHECK (settings_key = ''GLOBAL''),'
            || 'CONSTRAINT rag_generation_settings_profile_ck CHECK ('
            || 'generation_profile IN ('
            || '''grounded_concise'',''detailed_cited'',''strict_extractive'','
            || '''structured_json'',''bilingual_ja_en'',''inline_cited'',''custom'')),'
            || 'CONSTRAINT rag_generation_settings_revision_ck CHECK (revision >= 1),'
            || 'CONSTRAINT rag_generation_settings_active_prompt_fk '
            || 'FOREIGN KEY (active_prompt_version_id) '
            || 'REFERENCES rag_prompt_versions (version_id)'
            || ')';
    END IF;
END;
/

-- migration: 20260703_002_feedback_details
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM ctx_user_preferences
    WHERE pre_name = 'RAG_TEXT_WORLD_LEXER'
      AND pre_class = 'LEXER';

    IF v_count = 0 THEN
        CTX_DDL.CREATE_PREFERENCE('RAG_TEXT_WORLD_LEXER', 'WORLD_LEXER');
    END IF;
END;
/

DECLARE
    v_count NUMBER;
    PROCEDURE add_stopword(p_word VARCHAR2) IS
    BEGIN
        CTX_DDL.ADD_STOPWORD('RAG_TEXT_STOPLIST', p_word);
    EXCEPTION
        WHEN OTHERS THEN
            NULL;
    END;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM ctx_user_stoplists
    WHERE spl_name = 'RAG_TEXT_STOPLIST';

    IF v_count = 0 THEN
        CTX_DDL.CREATE_STOPLIST('RAG_TEXT_STOPLIST', 'BASIC_STOPLIST');
    END IF;

    add_stopword('の');
    add_stopword('は');
    add_stopword('が');
    add_stopword('を');
    add_stopword('に');
    add_stopword('へ');
    add_stopword('で');
    add_stopword('と');
    add_stopword('も');
    add_stopword('か');
    add_stopword('です');
    add_stopword('ます');
    add_stopword('なん');
    add_stopword('んで');
END;
/

DECLARE
    v_table_count NUMBER;
    v_index_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_FEEDBACK_DETAILS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_feedback_details ('
            || 'feedback_id VARCHAR2(64) PRIMARY KEY,'
            || 'tenant_id_hash CHAR(64),'
            || 'message_id VARCHAR2(64),'
            || 'content_source VARCHAR2(32) NOT NULL,'
            || 'question_text CLOB,'
            || 'answer_text CLOB,'
            || 'comment_text VARCHAR2(1000 CHAR),'
            || 'citations_json JSON,'
            || 'search_text CLOB,'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_feedback_details_feedback_fk FOREIGN KEY (feedback_id) '
            || 'REFERENCES rag_citation_feedback (feedback_id) ON DELETE CASCADE,'
            || 'CONSTRAINT rag_feedback_details_source_ck CHECK '
            || '(content_source IN (''chat_message'', ''search_snapshot''))'
            || ')';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_FEEDBACK_DETAILS_TENANT_CREATED_IDX';
    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_feedback_details_tenant_created_idx '
            || 'ON rag_feedback_details (tenant_id_hash, created_at DESC)';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_FEEDBACK_DETAILS_MESSAGE_IDX';
    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_feedback_details_message_idx '
            || 'ON rag_feedback_details (message_id)';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_FEEDBACK_DETAILS_TEXT_IDX';
    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_feedback_details_text_idx '
            || 'ON rag_feedback_details (search_text) '
            || 'INDEXTYPE IS CTXSYS.CONTEXT '
            || 'PARAMETERS (''LEXER RAG_TEXT_WORLD_LEXER STOPLIST RAG_TEXT_STOPLIST SYNC (ON COMMIT)'')';
    END IF;
END;
/

-- migration: 20260925_001_business_view_knowledge
DECLARE
    v_table_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_BUSINESS_VIEW_KNOWLEDGE';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_business_view_knowledge ('
            || 'business_view_id VARCHAR2(64) NOT NULL,'
            || 'kind VARCHAR2(32) NOT NULL,'
            || 'payload_json JSON NOT NULL,'
            || 'revision NUMBER(19) DEFAULT 1 NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_business_view_knowledge_pk PRIMARY KEY (business_view_id, kind),'
            || 'CONSTRAINT rag_business_view_knowledge_kind_ck CHECK ('
            || 'kind IN (''domain_keywords'', ''approved_faq'', ''runtime_knowledge'')))';
    END IF;
END;
/

-- migration: 20260925_002_answer_records
DECLARE
    v_table_count NUMBER;
    v_index_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_ANSWER_RECORDS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_answer_records ('
            || 'trace_id VARCHAR2(64) PRIMARY KEY,'
            || 'business_view_id VARCHAR2(64),'
            || 'surface VARCHAR2(16) NOT NULL,'
            || 'answer_engine VARCHAR2(32) NOT NULL,'
            || 'question CLOB NOT NULL,'
            || 'rewritten_question CLOB,'
            || 'answer CLOB NOT NULL,'
            || 'citations_json JSON NOT NULL,'
            || 'diagnostics_json JSON NOT NULL,'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_answer_records_surface_ck CHECK '
            || '(surface IN (''search'', ''chat'')))';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_ANSWER_RECORDS_VIEW_IDX';
    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_answer_records_view_idx '
            || 'ON rag_answer_records (business_view_id, created_at DESC)';
    END IF;
END;
/

-- migration: 20260926_001_documents_classification
DECLARE
    v_column_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_DOCUMENTS'
      AND column_name = 'CLASSIFICATION';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_documents ADD (classification JSON)';
    END IF;
END;
/

-- migration: 20260926_002_answer_record_evaluation
DECLARE
    PROCEDURE add_json_column(p_column VARCHAR2) IS
        v_column_count NUMBER;
    BEGIN
        SELECT COUNT(*) INTO v_column_count
        FROM user_tab_columns
        WHERE table_name = 'RAG_ANSWER_RECORDS'
          AND column_name = UPPER(p_column);
        IF v_column_count = 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE rag_answer_records ADD (' || p_column || ' JSON)';
        END IF;
    END;
BEGIN
    add_json_column('evaluation_input_json');
    add_json_column('evaluation_json');
END;
/

-- migration: 20260926_003_feedback_reasons_corrected_answer
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_FEEDBACK_DETAILS'
      AND column_name = 'CORRECTED_ANSWER_TEXT';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_feedback_details ADD (corrected_answer_text CLOB)';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM user_constraints
    WHERE table_name = 'RAG_CITATION_FEEDBACK'
      AND constraint_name = 'RAG_CITATION_FEEDBACK_REASON_CK';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_citation_feedback DROP CONSTRAINT rag_citation_feedback_reason_ck';
    END IF;
    EXECUTE IMMEDIATE
        'ALTER TABLE rag_citation_feedback ADD CONSTRAINT rag_citation_feedback_reason_ck '
        || 'CHECK ((rating = ''helpful'' AND reason IS NULL) OR '
        || '(rating = ''not_helpful'' AND ('
        || '(target_type = ''answer'' AND reason IN ('
        || '''incorrect'', ''incomplete'', ''not_relevant'', ''answer_untrusted'', '
        || '''missing_knowledge'', ''outdated_source'', ''ambiguous_question'')) OR '
        || '(target_type = ''citation'' AND reason IN ('
        || '''missing_evidence'', ''not_relevant'', ''answer_untrusted'')))))';
END;
/

-- migration: 20260926_004_docrag_prompts
DECLARE
    v_table_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_DOCRAG_PROMPTS';
    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_docrag_prompts ('
            || 'prompt_key VARCHAR2(64) PRIMARY KEY,'
            || 'content CLOB NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL)';
    END IF;
END;
/

-- migration: 20260926_005_query_history
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_QUERY_HISTORY';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_query_history ('
            || 'query_id VARCHAR2(64) PRIMARY KEY,'
            || 'business_view_id VARCHAR2(64) NOT NULL,'
            || 'surface VARCHAR2(16) NOT NULL,'
            || 'question VARCHAR2(2000 CHAR) NOT NULL,'
            || 'normalized_question VARCHAR2(2000 CHAR) NOT NULL,'
            || 'classification_filter JSON,'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_QUERY_HISTORY_VIEW_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_query_history_view_idx '
            || 'ON rag_query_history (business_view_id, created_at DESC)';
    END IF;
END;
/

-- migration: 20260927_001_role_access
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_ROLE_PERMISSIONS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_role_permissions ('
            || 'role_id VARCHAR2(36) NOT NULL,'
            || 'permission_code VARCHAR2(128) NOT NULL,'
            || 'CONSTRAINT rag_role_permissions_pk PRIMARY KEY (role_id, permission_code),'
            || 'CONSTRAINT rag_role_permissions_role_fk FOREIGN KEY (role_id) '
            || 'REFERENCES platform_roles (role_id) ON DELETE CASCADE)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_ROLE_BUSINESS_VIEWS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_role_business_views ('
            || 'role_id VARCHAR2(36) NOT NULL,'
            || 'business_view_id VARCHAR2(64) NOT NULL,'
            || 'CONSTRAINT rag_role_business_views_pk PRIMARY KEY (role_id, business_view_id),'
            || 'CONSTRAINT rag_role_business_views_role_fk FOREIGN KEY (role_id) '
            || 'REFERENCES platform_roles (role_id) ON DELETE CASCADE,'
            || 'CONSTRAINT rag_role_business_views_view_fk FOREIGN KEY (business_view_id) '
            || 'REFERENCES rag_business_views (business_view_id) ON DELETE CASCADE)';
    END IF;
    SELECT COUNT(*) INTO v_count
    FROM user_indexes WHERE index_name = 'RAG_ROLE_BUSINESS_VIEWS_VIEW_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_role_business_views_view_idx '
            || 'ON rag_role_business_views (business_view_id)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_ROLE_KNOWLEDGE_BASES';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_role_knowledge_bases ('
            || 'role_id VARCHAR2(36) NOT NULL,'
            || 'knowledge_base_id VARCHAR2(64) NOT NULL,'
            || 'CONSTRAINT rag_role_knowledge_bases_pk PRIMARY KEY (role_id, knowledge_base_id),'
            || 'CONSTRAINT rag_role_knowledge_bases_role_fk FOREIGN KEY (role_id) '
            || 'REFERENCES platform_roles (role_id) ON DELETE CASCADE,'
            || 'CONSTRAINT rag_role_knowledge_bases_kb_fk FOREIGN KEY (knowledge_base_id) '
            || 'REFERENCES rag_knowledge_bases (knowledge_base_id) ON DELETE CASCADE)';
    END IF;
    SELECT COUNT(*) INTO v_count
    FROM user_indexes WHERE index_name = 'RAG_ROLE_KNOWLEDGE_BASES_KB_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_role_knowledge_bases_kb_idx '
            || 'ON rag_role_knowledge_bases (knowledge_base_id)';
    END IF;
END;
/

-- migration: 20260928_001_retire_dashboard_permission
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_ROLE_PERMISSIONS';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE
            'DELETE FROM rag_role_permissions WHERE permission_code = ''menu.dashboard''';
    END IF;
END;
/

-- migration: 20260928_002_answer_record_owner
DECLARE
    v_column_count NUMBER;
    v_index_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_ANSWER_RECORDS'
      AND column_name = 'USER_ID_HASH';
    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_answer_records ADD (user_id_hash CHAR(64))';
    END IF;

    SELECT COUNT(*) INTO v_index_count
    FROM user_indexes
    WHERE index_name = 'RAG_ANSWER_RECORDS_OWNER_IDX';
    IF v_index_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_answer_records_owner_idx '
            || 'ON rag_answer_records (user_id_hash, business_view_id, created_at DESC)';
    END IF;

    -- 列を足した後の文なので動的 SQL で実行する。表が無い環境（ORA-00942）は補わない。
    BEGIN
        EXECUTE IMMEDIATE
            'UPDATE rag_answer_records r SET r.user_id_hash = ('
            || 'SELECT MIN(m.user_id_hash) FROM rag_messages m '
            || 'WHERE m.trace_id = r.trace_id AND m.role = ''ASSISTANT'' '
            || 'AND m.user_id_hash IS NOT NULL '
            || 'HAVING COUNT(DISTINCT m.user_id_hash) = 1) '
            || 'WHERE r.user_id_hash IS NULL';
    EXCEPTION
        WHEN OTHERS THEN
            IF SQLCODE != -942 THEN
                RAISE;
            END IF;
    END;
    BEGIN
        EXECUTE IMMEDIATE
            'UPDATE rag_answer_records r SET r.user_id_hash = ('
            || 'SELECT MIN(a.user_id_hash) FROM rag_search_audit a '
            || 'WHERE a.trace_id = r.trace_id AND a.user_id_hash IS NOT NULL '
            || 'HAVING COUNT(DISTINCT a.user_id_hash) = 1) '
            || 'WHERE r.user_id_hash IS NULL';
    EXCEPTION
        WHEN OTHERS THEN
            IF SQLCODE != -942 THEN
                RAISE;
            END IF;
    END;
END;
/

-- migration: 20260928_003_default_document_recipes
MERGE INTO rag_document_recipes r
USING (
    SELECT
        LOWER(RAWTOHEX(STANDARD_HASH(d.document_id || ':recipe:1', 'SHA256'))) AS recipe_id,
        d.document_id,
        d.tenant_id_hash,
        d.processing_config,
        d.status,
        d.preprocess_artifact,
        d.error_message,
        d.uploaded_at,
        d.indexed_at
    FROM rag_documents d
    WHERE NOT EXISTS (
        SELECT 1 FROM rag_document_recipes existing
        WHERE existing.document_id = d.document_id
    )
) d
ON (r.document_id = d.document_id AND r.slot_no = 1)
WHEN NOT MATCHED THEN INSERT (
    recipe_id, document_id, slot_no, tenant_id_hash, processing_config, status,
    preprocess_artifact, config_revision, materialized_revision, error_message,
    created_at, updated_at, finished_at
) VALUES (
    d.recipe_id, d.document_id, 1, d.tenant_id_hash, d.processing_config, d.status,
    d.preprocess_artifact, 1, CASE WHEN d.status = 'INDEXED' THEN 1 END, d.error_message,
    d.uploaded_at, SYSTIMESTAMP, d.indexed_at
);

-- migration: 20260928_004_ingestion_jobs_lease
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_INGESTION_JOBS'
      AND column_name = 'LEASE_OWNER';

    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_ingestion_jobs ADD (lease_owner VARCHAR2(128))';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_INGESTION_JOBS'
      AND column_name = 'HEARTBEAT_AT';

    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_ingestion_jobs ADD (heartbeat_at TIMESTAMP WITH TIME ZONE)';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM user_indexes
    WHERE index_name = 'RAG_INGESTION_JOBS_LEASE_IDX';

    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_ingestion_jobs_lease_idx '
            || 'ON rag_ingestion_jobs (lease_owner, status)';
    END IF;
END;
/

-- migration: 20260928_005_evaluation_jobs
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_EVALUATION_JOBS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_evaluation_jobs ('
            || 'job_id VARCHAR2(64) PRIMARY KEY,'
            || 'kind VARCHAR2(16) NOT NULL,'
            || 'status VARCHAR2(16) NOT NULL,'
            || 'tenant_id_hash VARCHAR2(64),'
            || 'user_id_hash VARCHAR2(64),'
            || 'total_cases NUMBER(10) DEFAULT 0 NOT NULL,'
            || 'completed_cases NUMBER(10) DEFAULT 0 NOT NULL,'
            || 'current_case_id VARCHAR2(200 CHAR),'
            || 'current_experiment_id VARCHAR2(80 CHAR),'
            || 'current_case_started_at TIMESTAMP WITH TIME ZONE,'
            || 'lease_owner VARCHAR2(128),'
            || 'heartbeat_at TIMESTAMP WITH TIME ZONE,'
            || 'time_limit_seconds NUMBER(10) NOT NULL,'
            || 'error_message VARCHAR2(2000 CHAR),'
            || 'result_json JSON,'
            || 'evaluation_run_id VARCHAR2(64),'
            || 'created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'started_at TIMESTAMP WITH TIME ZONE,'
            || 'finished_at TIMESTAMP WITH TIME ZONE,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_evaluation_jobs_kind_ck CHECK (kind IN (''run'', ''compare'')),'
            || 'CONSTRAINT rag_evaluation_jobs_status_ck '
            || 'CHECK (status IN (''RUNNING'', ''SUCCEEDED'', ''FAILED'', ''CANCELLED'')))';
    END IF;
    SELECT COUNT(*) INTO v_count
    FROM user_indexes
    WHERE index_name = 'RAG_EVALUATION_JOBS_STATUS_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_evaluation_jobs_status_idx '
            || 'ON rag_evaluation_jobs (status, heartbeat_at)';
    END IF;
    SELECT COUNT(*) INTO v_count
    FROM user_indexes
    WHERE index_name = 'RAG_EVALUATION_JOBS_OWNER_CREATED_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_evaluation_jobs_owner_created_idx '
            || 'ON rag_evaluation_jobs (tenant_id_hash, user_id_hash, created_at DESC)';
    END IF;
END;
/

-- migration: 20260930_001_default_descriptions
UPDATE rag_knowledge_bases
SET
    description = 'ナレッジベースを指定せずにアップロードした文書が入る、既定のナレッジベースです。',
    updated_at = SYSTIMESTAMP
WHERE LOWER(name) = 'default'
  AND TRIM(description) IS NULL;

UPDATE rag_business_views
SET
    description = 'DEFAULT ナレッジベースを検索・回答に使う、既定の業務ビューです。',
    updated_at = SYSTIMESTAMP
WHERE LOWER(name) = 'default'
  AND TRIM(description) IS NULL;

COMMIT;

-- migration: 20260930_002_artifact_layers_input_fingerprint
DECLARE
    v_column_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_ARTIFACT_LAYERS'
      AND column_name = 'INPUT_FINGERPRINT';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_artifact_layers ADD (input_fingerprint JSON)';
    END IF;
END;
/

-- migration: 20260930_003_chunk_sets_first_page_context
DECLARE
    v_column_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_CHUNK_SETS'
      AND column_name = 'FIRST_PAGE_CONTEXT';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_chunk_sets ADD (first_page_context JSON)';
    END IF;
END;
/

-- migration: 20260930_004_knowledge_base_extraction_fields
DECLARE
    v_column_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_column_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_KNOWLEDGE_BASES'
      AND column_name = 'EXTRACTION_FIELDS';

    IF v_column_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_knowledge_bases ADD (extraction_fields JSON)';
    END IF;
END;
/

-- migration: 20260930_005_retire_standard_engine_objects
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_ROLE_PERMISSIONS';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE
            'DELETE FROM rag_role_permissions WHERE permission_code IN ('
            || '''menu.settings_grounding'', ''menu.settings_generation'', '
            || '''menu.settings_agentic'')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_GENERATION_SETTINGS';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE 'DROP TABLE rag_generation_settings CASCADE CONSTRAINTS PURGE';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_PROMPT_VERSIONS';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE 'DROP TABLE rag_prompt_versions CASCADE CONSTRAINTS PURGE';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_AGENT_MEMORIES';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE 'DROP TABLE rag_agent_memories CASCADE CONSTRAINTS PURGE';
    END IF;
END;
/

-- migration: 20260930_006_graph_profile_entities
UPDATE rag_documents
SET processing_config = JSON_TRANSFORM(processing_config, REPLACE '$.graph_profile' = 'entities')
WHERE JSON_VALUE(processing_config, '$.graph_profile') = 'full';

UPDATE rag_document_recipes
SET processing_config = JSON_TRANSFORM(processing_config, REPLACE '$.graph_profile' = 'entities')
WHERE JSON_VALUE(processing_config, '$.graph_profile') = 'full';

UPDATE rag_knowledge_bases
SET retrieval_config = JSON_TRANSFORM(retrieval_config, REPLACE '$.ingestion.graph_profile' = 'entities')
WHERE JSON_VALUE(retrieval_config, '$.ingestion.graph_profile') = 'full';

UPDATE rag_ingestion_jobs
SET settings_overrides = JSON_TRANSFORM(settings_overrides, REPLACE '$.processing_config.graph_profile' = 'entities')
WHERE JSON_VALUE(settings_overrides, '$.processing_config.graph_profile') = 'full';

UPDATE rag_chunk_sets
SET recipe_subset = JSON_TRANSFORM(recipe_subset, REPLACE '$.processing_config.graph_profile' = 'entities')
WHERE JSON_VALUE(recipe_subset, '$.processing_config.graph_profile') = 'full';

UPDATE rag_chunk_sets
SET recipe_subset = JSON_TRANSFORM(recipe_subset, REPLACE '$.effective_processing_config.graph_profile' = 'entities')
WHERE JSON_VALUE(recipe_subset, '$.effective_processing_config.graph_profile') = 'full';

COMMIT;

-- migration: 20260930_007_retire_graph_claims_community
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_GRAPH_CLAIMS';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE 'DROP TABLE rag_graph_claims CASCADE CONSTRAINTS PURGE';
    END IF;
    SELECT COUNT(*) INTO v_count
    FROM user_tables
    WHERE table_name = 'RAG_GRAPH_COMMUNITY_SUMMARIES';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE 'DROP TABLE rag_graph_community_summaries CASCADE CONSTRAINTS PURGE';
    END IF;
END;
/

-- migration: 20260930_008_answer_prompts_table
DECLARE
    v_old_count NUMBER;
    v_new_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_old_count FROM user_tables WHERE table_name = 'RAG_DOCRAG_PROMPTS';
    SELECT COUNT(*) INTO v_new_count FROM user_tables WHERE table_name = 'RAG_ANSWER_PROMPTS';
    IF v_old_count > 0 AND v_new_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_docrag_prompts RENAME TO rag_answer_prompts';
    ELSIF v_old_count > 0 THEN
        EXECUTE IMMEDIATE
            'MERGE INTO rag_answer_prompts target '
            || 'USING (SELECT prompt_key, content, updated_at FROM rag_docrag_prompts) source '
            || 'ON (target.prompt_key = source.prompt_key) '
            || 'WHEN NOT MATCHED THEN INSERT (prompt_key, content, updated_at) '
            || 'VALUES (source.prompt_key, source.content, source.updated_at)';
        COMMIT;
    END IF;
END;
/

-- migration: 20260930_009_stored_engine_names
DECLARE
    TYPE t_names IS TABLE OF VARCHAR2(261);
    v_targets t_names := t_names('RAG_DOCUMENTS.PROCESSING_CONFIG', 'RAG_DOCUMENTS.EXTRACTION', 'RAG_DOCUMENT_RECIPES.PROCESSING_CONFIG', 'RAG_KNOWLEDGE_BASES.RETRIEVAL_CONFIG', 'RAG_BUSINESS_VIEWS.VIEW_CONFIG', 'RAG_INGESTION_JOBS.SETTINGS_OVERRIDES', 'RAG_INGESTION_JOBS.QUALITY_WARNINGS', 'RAG_CHUNK_SETS.RECIPE_SUBSET', 'RAG_CHUNK_SETS.METRICS_JSON', 'RAG_CHUNK_SETS.FIRST_PAGE_CONTEXT', 'RAG_DOCUMENT_EXTRACTIONS.RECIPE_SUBSET', 'RAG_DOCUMENT_EXTRACTIONS.EXTRACTION_JSON', 'RAG_DOCUMENT_EXTRACTIONS.METRICS_JSON', 'RAG_ARTIFACT_LAYERS.INPUT_FINGERPRINT', 'RAG_ARTIFACT_LAYERS.METRICS_JSON', 'RAG_CHUNKS.METADATA_JSON', 'RAG_ANSWER_RECORDS.CITATIONS_JSON', 'RAG_ANSWER_RECORDS.DIAGNOSTICS_JSON', 'RAG_ANSWER_RECORDS.EVALUATION_INPUT_JSON', 'RAG_ANSWER_RECORDS.EVALUATION_JSON', 'RAG_MESSAGES.CITATIONS_JSON', 'RAG_FEEDBACK_DETAILS.CITATIONS_JSON', 'RAG_EVALUATION_RUNS.REQUEST_JSON', 'RAG_EVALUATION_RUNS.RESULT_JSON', 'RAG_EVALUATION_JOBS.RESULT_JSON');
    v_table   VARCHAR2(128);
    v_column  VARCHAR2(128);
    v_type    VARCHAR2(128);
    v_text    VARCHAR2(512);
    v_expr    VARCHAR2(32767);
    v_count   NUMBER;
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
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_small_to_big'', ''small_to_big'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_table_child_target_chars'', ''rag_chunk_table_child_target_chars'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_child_target_chars'', ''rag_chunk_child_target_chars'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_parent_target_chars'', ''rag_chunk_parent_target_chars'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_parent_max_children'', ''rag_chunk_parent_max_children'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_parent_max_pages'', ''rag_chunk_parent_max_pages'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_answer_vision_enabled'', ''rag_answer_vision_enabled'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_history_rewrite_enabled'', ''rag_history_rewrite_enabled'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_screen_linking_enabled'', ''rag_screen_linking_enabled'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_neighbor_child_count'', ''rag_neighbor_child_count'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_query_strategy'', ''rag_query_strategy'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_rerank_enabled'', ''rag_rerank_enabled'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_answer_flow'', ''rag_answer_flow'')';
            v_expr := 'REPLACE(' || v_expr || ', ''rag_docrag_profile'', ''rag_answer_profile'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_table_child_target_chars'', ''chunk_table_child_target_chars'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_child_target_chars'', ''chunk_child_target_chars'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_parent_target_chars'', ''chunk_parent_target_chars'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_parent_max_children'', ''chunk_parent_max_children'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_parent_max_pages'', ''chunk_parent_max_pages'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_screen_linking_enabled'', ''screen_linking_enabled'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_neighbor_child_count'', ''neighbor_child_count'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_query_strategy'', ''query_strategy'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_rerank_enabled'', ''rerank_enabled'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_answer_flow'', ''answer_flow'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_first_page_context_json'', ''first_page_context_json'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_source_record_refs_json'', ''source_record_refs_json'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_source_seq_ranges_json'', ''source_seq_ranges_json'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_metadata_json'', ''engine_metadata_json'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_search_text'', ''engine_search_text'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_parent_text'', ''parent_text'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_chunk_seq'', ''engine_chunk_seq'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_chunk_id'', ''engine_chunk_id'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_model_used'', ''evidence_model_used'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_role'', ''evidence_role'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_parent'', ''small_to_big_parent'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docling_docrag'', ''docling_layout'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_instruction_callout'', ''instruction_callout'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_layout_records'', ''layout_records'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_layout_missing'', ''layout_missing'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_layout'', ''layout_records'')';
            v_expr := 'REPLACE(' || v_expr || ', ''docrag_chunk_contract'', ''chunk_metadata_contract'')';
            v_expr := 'REPLACE(' || v_expr || ', ''"docrag_retrieval_only"'', ''"retrieval_only"'')';
            v_expr := 'REPLACE(' || v_expr || ', ''"docrag_grounded"'', ''"grounded"'')';
            v_expr := 'REPLACE(' || v_expr || ', ''"docrag_history_rewrite"'', ''"history_rewrite"'')';
            v_expr := 'REPLACE(' || v_expr || ', ''"docrag_answer"'', ''"answer"'')';
            v_expr := 'REPLACE(' || v_expr || ', ''"retrieval_strategy":"docrag"'', ''"retrieval_strategy":"hybrid"'')';
            v_expr := 'REPLACE(' || v_expr || ', ''"answer_engine":"docrag"'', ''"answer_engine":"grounded"'')';
            v_expr := 'REPLACE(' || v_expr || ', ''"docrag":'', ''"answer":'')';
            IF v_type = 'JSON' THEN
                v_expr := 'JSON(' || v_expr || ')';
            END IF;
            EXECUTE IMMEDIATE
                'UPDATE ' || v_table || ' SET ' || v_column || ' = ' || v_expr
                || ' WHERE ' || v_text || ' LIKE ''%docrag%''';
            COMMIT;
        END IF;
    END LOOP;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_ANSWER_RECORDS';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE
            'UPDATE rag_answer_records SET answer_engine = ''grounded'' '
            || 'WHERE answer_engine = ''docrag''';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_SEARCH_AUDIT';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE
            'UPDATE rag_search_audit SET error_stage = SUBSTR(error_stage, 8) '
            || 'WHERE error_stage IN (''docrag_answer'', ''docrag_history_rewrite'')';
    END IF;
    COMMIT;
END;
/

-- migration: 20261001_001_document_sections
DECLARE
    v_table_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_table_count
    FROM user_tables
    WHERE table_name = 'RAG_DOCUMENT_SECTIONS';

    IF v_table_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_document_sections ('
            || 'document_id VARCHAR2(64) NOT NULL,'
            || 'sections_json JSON NOT NULL,'
            || 'revision NUMBER(19) DEFAULT 1 NOT NULL,'
            || 'updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,'
            || 'CONSTRAINT rag_document_sections_pk PRIMARY KEY (document_id),'
            || 'CONSTRAINT rag_document_sections_document_fk FOREIGN KEY (document_id) '
            || 'REFERENCES rag_documents (document_id) ON DELETE CASCADE)';
    END IF;
END;
/

-- migration: 20261003_001_search_answer_profiles
DECLARE
    old_count NUMBER;
    new_count NUMBER;
    table_value VARCHAR2(128);
    PROCEDURE rename_table(old_name VARCHAR2, new_name VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO old_count FROM user_tables WHERE table_name = old_name;
        SELECT COUNT(*) INTO new_count FROM user_tables WHERE table_name = new_name;
        IF old_count > 0 AND new_count > 0 THEN
            RAISE_APPLICATION_ERROR(-20060, 'PROFILE_RENAME_CONFLICT');
        ELSIF old_count > 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE ' || old_name || ' RENAME TO ' || new_name;
        END IF;
    END;
    PROCEDURE rename_column(tab VARCHAR2, old_name VARCHAR2, new_name VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO old_count
            FROM user_tab_columns WHERE table_name = tab
            AND column_name = old_name;
        SELECT COUNT(*) INTO new_count
            FROM user_tab_columns WHERE table_name = tab
            AND column_name = new_name;
        IF old_count > 0 AND new_count > 0 THEN
            RAISE_APPLICATION_ERROR(-20060, 'PROFILE_COLUMN_RENAME_CONFLICT');
        ELSIF old_count > 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE ' || tab || ' RENAME COLUMN '
                || old_name || ' TO ' || new_name;
        END IF;
    END;
    PROCEDURE rename_index(old_name VARCHAR2, new_name VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO old_count FROM user_indexes WHERE index_name = old_name;
        SELECT COUNT(*) INTO new_count FROM user_indexes WHERE index_name = new_name;
        IF old_count > 0 AND new_count > 0 THEN
            RAISE_APPLICATION_ERROR(-20060, 'PROFILE_INDEX_RENAME_CONFLICT');
        ELSIF old_count > 0 THEN
            EXECUTE IMMEDIATE 'ALTER INDEX ' || old_name || ' RENAME TO ' || new_name;
        END IF;
    END;
    PROCEDURE rename_constraint(old_name VARCHAR2, new_name VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO old_count
                FROM user_constraints WHERE constraint_name = old_name;
        SELECT COUNT(*) INTO new_count FROM user_constraints WHERE constraint_name = new_name;
        IF old_count > 0 AND new_count > 0 THEN
            RAISE_APPLICATION_ERROR(-20060, 'PROFILE_CONSTRAINT_RENAME_CONFLICT');
        ELSIF old_count > 0 THEN
            SELECT table_name INTO table_value
                FROM user_constraints WHERE constraint_name = old_name;
            EXECUTE IMMEDIATE 'ALTER TABLE ' || table_value || ' RENAME CONSTRAINT '
                || old_name || ' TO ' || new_name;
        END IF;
    END;
    PROCEDURE rename_permission(old_code VARCHAR2, new_code VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO old_count FROM user_tables WHERE table_name = 'RAG_ROLE_PERMISSIONS';
        IF old_count > 0 THEN
            EXECUTE IMMEDIATE 'MERGE INTO rag_role_permissions dst USING '
                || '(SELECT role_id FROM rag_role_permissions WHERE permission_code = :1) src '
                || 'ON (dst.role_id = src.role_id AND dst.permission_code = :2) '
                || 'WHEN NOT MATCHED THEN INSERT (role_id, permission_code) '
                || 'VALUES (src.role_id, :3)'
                USING old_code, new_code, new_code;
            EXECUTE IMMEDIATE 'DELETE FROM rag_role_permissions WHERE permission_code = :1'
                USING old_code;
        END IF;
    END;
    PROCEDURE rename_diagnostics IS
    BEGIN
        SELECT COUNT(*) INTO old_count FROM user_tab_columns
            WHERE table_name = 'RAG_ANSWER_RECORDS' AND column_name = 'DIAGNOSTICS_JSON';
        IF old_count > 0 THEN
            EXECUTE IMMEDIATE q'[SELECT COUNT(*) FROM rag_answer_records
                WHERE JSON_EXISTS(diagnostics_json, '$.business_view_applied')
                AND JSON_EXISTS(diagnostics_json, '$.search_answer_profile_applied')]'
                INTO new_count;
            IF new_count > 0 THEN
                RAISE_APPLICATION_ERROR(-20060, 'PROFILE_DIAGNOSTICS_RENAME_CONFLICT');
            END IF;
            EXECUTE IMMEDIATE q'[UPDATE rag_answer_records
                SET diagnostics_json = JSON_TRANSFORM(diagnostics_json,
                    RENAME '$.business_view_applied' = 'search_answer_profile_applied')
                WHERE JSON_EXISTS(diagnostics_json, '$.business_view_applied')]';
        END IF;
    END;
BEGIN
    rename_diagnostics;
    rename_table('RAG_BUSINESS_VIEWS', 'RAG_SEARCH_ANSWER_PROFILES');
    rename_table('RAG_BUSINESS_VIEW_KNOWLEDGE', 'RAG_SEARCH_ANSWER_PROFILE_KNOWLEDGE');
    rename_table('RAG_ROLE_BUSINESS_VIEWS', 'RAG_ROLE_SEARCH_ANSWER_PROFILES');
    rename_column('RAG_SEARCH_ANSWER_PROFILES', 'BUSINESS_VIEW_ID', 'SEARCH_ANSWER_PROFILE_ID');
    rename_column('RAG_SEARCH_ANSWER_PROFILES', 'VIEW_CONFIG', 'PROFILE_CONFIG');
    rename_column('RAG_ANSWER_RECORDS', 'BUSINESS_VIEW_ID', 'SEARCH_ANSWER_PROFILE_ID');
    rename_column('RAG_QUERY_HISTORY', 'BUSINESS_VIEW_ID', 'SEARCH_ANSWER_PROFILE_ID');
    rename_column('RAG_SEARCH_ANSWER_PROFILE_KNOWLEDGE', 'BUSINESS_VIEW_ID', 'SEARCH_ANSWER_PROFILE_ID');
    rename_column('RAG_CONVERSATIONS', 'BUSINESS_VIEW_ID', 'SEARCH_ANSWER_PROFILE_ID');
    rename_column('RAG_CITATION_FEEDBACK', 'BUSINESS_VIEW_ID', 'SEARCH_ANSWER_PROFILE_ID');
    rename_column('RAG_ROLE_SEARCH_ANSWER_PROFILES', 'BUSINESS_VIEW_ID', 'SEARCH_ANSWER_PROFILE_ID');
    rename_index('RAG_BUSINESS_VIEWS_TENANT_NAME_UIDX', 'RAG_SEARCH_ANSWER_PROFILES_TENANT_NAME_UIDX');
    rename_index('RAG_BUSINESS_VIEWS_TENANT_STATUS_IDX', 'RAG_SEARCH_ANSWER_PROFILES_TENANT_STATUS_IDX');
    rename_index('RAG_ANSWER_RECORDS_VIEW_IDX', 'RAG_ANSWER_RECORDS_PROFILE_IDX');
    rename_index('RAG_QUERY_HISTORY_VIEW_IDX', 'RAG_QUERY_HISTORY_PROFILE_IDX');
    rename_index('RAG_CONVERSATIONS_TENANT_VIEW_UPDATED_IDX', 'RAG_CONVERSATIONS_TENANT_PROFILE_UPDATED_IDX');
    rename_index('RAG_CONVERSATIONS_BUSINESS_VIEW_IDX', 'RAG_CONVERSATIONS_SEARCH_ANSWER_PROFILE_IDX');
    rename_index('RAG_ROLE_BUSINESS_VIEWS_VIEW_IDX', 'RAG_ROLE_SEARCH_ANSWER_PROFILES_VIEW_IDX');
    rename_constraint('RAG_BUSINESS_VIEWS_STATUS_CK', 'RAG_SEARCH_ANSWER_PROFILES_STATUS_CK');
    rename_constraint('RAG_BUSINESS_VIEW_KNOWLEDGE_PK', 'RAG_SEARCH_ANSWER_PROFILE_KNOWLEDGE_PK');
    rename_constraint('RAG_BUSINESS_VIEW_KNOWLEDGE_KIND_CK', 'RAG_SEARCH_ANSWER_PROFILE_KNOWLEDGE_KIND_CK');
    rename_constraint('RAG_CONVERSATIONS_BUSINESS_VIEW_FK', 'RAG_CONVERSATIONS_SEARCH_ANSWER_PROFILE_FK');
    rename_constraint('RAG_ROLE_BUSINESS_VIEWS_PK', 'RAG_ROLE_SEARCH_ANSWER_PROFILES_PK');
    rename_constraint('RAG_ROLE_BUSINESS_VIEWS_ROLE_FK', 'RAG_ROLE_SEARCH_ANSWER_PROFILES_ROLE_FK');
    rename_constraint('RAG_ROLE_BUSINESS_VIEWS_VIEW_FK', 'RAG_ROLE_SEARCH_ANSWER_PROFILES_VIEW_FK');
    rename_permission('menu.business_views', 'menu.search_answer_profiles');
    rename_permission('rag.business_views.manage', 'rag.search_answer_profiles.manage');
END;

-- migration: 20261005_001_message_answer_runs
DECLARE
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
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_MESSAGES' AND column_name = 'LEASE_OWNER';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_messages ADD (lease_owner VARCHAR2(128))';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_MESSAGES' AND column_name = 'HEARTBEAT_AT';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_messages ADD (heartbeat_at TIMESTAMP WITH TIME ZONE)';
    END IF;

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
/

-- migration: 20261007_001_document_superseded
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_DOCUMENTS' AND column_name = 'SUPERSEDED_BY_DOCUMENT_ID';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_documents ADD (superseded_by_document_id VARCHAR2(64))';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_DOCUMENTS' AND column_name = 'SUPERSEDED_AT';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_documents ADD (superseded_at TIMESTAMP WITH TIME ZONE)';
    END IF;
END;
/

-- migration: 20261009_001_entity_layer
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_ENTITIES';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_entities ( '
            || 'entity_id       VARCHAR2(64) PRIMARY KEY, '
            || 'chunk_set_id    VARCHAR2(64) NOT NULL, '
            || 'document_id     VARCHAR2(64) NOT NULL, '
            || 'tenant_id_hash  CHAR(64), '
            || 'entity_key      VARCHAR2(400 CHAR) NOT NULL, '
            || 'display_name    VARCHAR2(400 CHAR) NOT NULL, '
            || 'entity_type     VARCHAR2(16) NOT NULL, '
            || 'scope_label     VARCHAR2(200 CHAR), '
            || 'created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'CONSTRAINT rag_entities_document_fk '
            || 'FOREIGN KEY (document_id) REFERENCES rag_documents (document_id) ON DELETE CASCADE, '
            || 'CONSTRAINT rag_entities_type_ck '
            || 'CHECK (entity_type IN (''record'', ''defined'', ''value'', ''term'')) '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_ENTITIES_CHUNK_SET_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_entities_chunk_set_idx '
            || 'ON rag_entities (chunk_set_id)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_ENTITIES_DOCUMENT_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_entities_document_idx '
            || 'ON rag_entities (document_id)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_ENTITY_ALIASES';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_entity_aliases ( '
            || 'entity_id   VARCHAR2(64) NOT NULL, '
            || 'alias_key   VARCHAR2(400 CHAR) NOT NULL, '
            || 'alias_text  VARCHAR2(400 CHAR) NOT NULL, '
            || 'alias_kind  VARCHAR2(16) NOT NULL, '
            || 'CONSTRAINT rag_entity_aliases_pk PRIMARY KEY (entity_id, alias_key), '
            || 'CONSTRAINT rag_entity_aliases_entity_fk '
            || 'FOREIGN KEY (entity_id) REFERENCES rag_entities (entity_id) ON DELETE CASCADE '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_ENTITY_ALIASES_KEY_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_entity_aliases_key_idx '
            || 'ON rag_entity_aliases (alias_key, entity_id)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_ENTITY_CHUNKS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_entity_chunks ( '
            || 'entity_id       VARCHAR2(64) NOT NULL, '
            || 'chunk_id        VARCHAR2(128) NOT NULL, '
            || 'chunk_role      VARCHAR2(16) NOT NULL, '
            || 'attribute_name  VARCHAR2(200 CHAR), '
            || 'CONSTRAINT rag_entity_chunks_pk PRIMARY KEY (entity_id, chunk_id), '
            || 'CONSTRAINT rag_entity_chunks_entity_fk '
            || 'FOREIGN KEY (entity_id) REFERENCES rag_entities (entity_id) ON DELETE CASCADE, '
            || 'CONSTRAINT rag_entity_chunks_chunk_fk '
            || 'FOREIGN KEY (chunk_id) REFERENCES rag_chunks (chunk_id) ON DELETE CASCADE, '
            || 'CONSTRAINT rag_entity_chunks_role_ck '
            || 'CHECK (chunk_role IN (''definition'', ''attribute'', ''mention'')) '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_ENTITY_CHUNKS_CHUNK_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_entity_chunks_chunk_idx '
            || 'ON rag_entity_chunks (chunk_id, chunk_role)';
    END IF;
END;
/

-- migration: 20261011_001_section_chunk_map
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_CHUNK_SET_SECTIONS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_chunk_set_sections ( '
            || 'chunk_set_id       VARCHAR2(64) NOT NULL, '
            || 'section_id         VARCHAR2(64) NOT NULL, '
            || 'document_id        VARCHAR2(64) NOT NULL, '
            || 'section_ordinal    NUMBER(10) NOT NULL, '
            || 'section_level      NUMBER(2) NOT NULL, '
            || 'parent_section_id  VARCHAR2(64), '
            || 'title              VARCHAR2(400 CHAR) NOT NULL, '
            || 'page_start         NUMBER(10), '
            || 'page_end           NUMBER(10), '
            || 'chunk_count        NUMBER(10) DEFAULT 0 NOT NULL, '
            || 'char_count         NUMBER(19) DEFAULT 0 NOT NULL, '
            || 'first_chunk_index  NUMBER(10), '
            || 'last_chunk_index   NUMBER(10), '
            || 'created_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'CONSTRAINT rag_chunk_set_sections_pk PRIMARY KEY (chunk_set_id, section_id), '
            || 'CONSTRAINT rag_chunk_set_sections_document_fk '
            || 'FOREIGN KEY (document_id) REFERENCES rag_documents (document_id) ON DELETE CASCADE '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_CHUNK_SET_SECTIONS_DOCUMENT_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_chunk_set_sections_document_idx '
            || 'ON rag_chunk_set_sections (document_id)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_SECTION_CHUNKS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_section_chunks ( '
            || 'chunk_set_id  VARCHAR2(64) NOT NULL, '
            || 'section_id    VARCHAR2(64) NOT NULL, '
            || 'chunk_id      VARCHAR2(128) NOT NULL, '
            || 'document_id   VARCHAR2(64) NOT NULL, '
            || 'chunk_order   NUMBER(10) NOT NULL, '
            || 'chunk_index   NUMBER(10) NOT NULL, '
            || 'is_owner      NUMBER(1) DEFAULT 0 NOT NULL, '
            || 'CONSTRAINT rag_section_chunks_pk PRIMARY KEY (chunk_set_id, section_id, chunk_id), '
            || 'CONSTRAINT rag_section_chunks_section_fk '
            || 'FOREIGN KEY (chunk_set_id, section_id) '
            || 'REFERENCES rag_chunk_set_sections (chunk_set_id, section_id) ON DELETE CASCADE, '
            || 'CONSTRAINT rag_section_chunks_chunk_fk '
            || 'FOREIGN KEY (chunk_id) REFERENCES rag_chunks (chunk_id) ON DELETE CASCADE, '
            || 'CONSTRAINT rag_section_chunks_owner_ck CHECK (is_owner IN (0, 1)) '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_SECTION_CHUNKS_CHUNK_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_section_chunks_chunk_idx '
            || 'ON rag_section_chunks (chunk_id, is_owner)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_SECTION_CHUNKS_DOCUMENT_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_section_chunks_document_idx '
            || 'ON rag_section_chunks (document_id)';
    END IF;
END;
/

-- migration: 20261015_001_message_clarification
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_MESSAGES' AND column_name = 'CLARIFICATION_JSON';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_messages ADD (clarification_json CLOB)';
    END IF;
END;
/

-- migration: 20261015_002_run_history
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_RUN_HISTORY';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_run_history ( '
            || 'run_id                    VARCHAR2(64) PRIMARY KEY, '
            || 'surface                   VARCHAR2(16) NOT NULL, '
            || 'search_answer_profile_id  VARCHAR2(64), '
            || 'tenant_id_hash            CHAR(64), '
            || 'user_id_hash              CHAR(64), '
            || 'user_uuid                 VARCHAR2(256), '
            || 'question                  CLOB, '
            || 'answer_generated          NUMBER(1) DEFAULT 0 NOT NULL, '
            || 'status                    VARCHAR2(16) NOT NULL, '
            || 'answer_outcome            VARCHAR2(32), '
            || 'verification              VARCHAR2(16), '
            || 'confidence                VARCHAR2(16), '
            || 'needs_human_review        NUMBER(1), '
            || 'citation_count            NUMBER(10) DEFAULT 0 NOT NULL, '
            || 'elapsed_ms                NUMBER(12, 3), '
            || 'error_stage               VARCHAR2(128), '
            || 'error_type                VARCHAR2(128), '
            || 'model_id                  VARCHAR2(256), '
            || 'conversation_id           VARCHAR2(64), '
            || 'message_id                VARCHAR2(64), '
            || 'stages_json               JSON, '
            || 'citations_json            JSON, '
            || 'created_at                TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'CONSTRAINT rag_run_history_surface_ck CHECK (surface IN (''search'', ''chat'')), '
            || 'CONSTRAINT rag_run_history_status_ck CHECK (status IN (''success'', ''blocked'', ''error'')), '
            || 'CONSTRAINT rag_run_history_verification_ck '
            || 'CHECK (verification IS NULL OR verification IN (''verified'', ''partial'', ''unverified'')) '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_RUN_HISTORY_CREATED_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_run_history_created_idx '
            || 'ON rag_run_history (created_at DESC, run_id)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_RUN_HISTORY_OWNER_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_run_history_owner_idx '
            || 'ON rag_run_history (user_id_hash, created_at DESC)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_RUN_HISTORY_CONVERSATION_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_run_history_conversation_idx '
            || 'ON rag_run_history (conversation_id)';
    END IF;
END;
/

-- migration: 20261015_003_run_history_conversation_idx
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_RUN_HISTORY_CONVERSATION_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_run_history_conversation_idx '
            || 'ON rag_run_history (conversation_id)';
    END IF;
END;
/

-- migration: 20261015_004_ingestion_job_progress
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_INGESTION_JOBS' AND column_name = 'PROGRESS_JSON';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_ingestion_jobs ADD (progress_json CLOB)';
    END IF;
END;
/

-- migration: 20261016_001_retire_evaluation_compare
DECLARE
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
/

-- migration: 20261016_003_document_categories
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_DOCUMENT_CATEGORIES';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_document_categories ( '
            || 'category_id     VARCHAR2(64) PRIMARY KEY, '
            || 'parent_id       VARCHAR2(64), '
            || 'category_level  NUMBER(1) NOT NULL, '
            || 'name            VARCHAR2(200 CHAR) NOT NULL, '
            || 'sort_order      NUMBER(10) DEFAULT 0 NOT NULL, '
            || 'created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'CONSTRAINT rag_document_categories_level_ck CHECK (category_level BETWEEN 1 AND 3), '
            || 'CONSTRAINT rag_document_categories_parent_fk FOREIGN KEY (parent_id) REFERENCES rag_document_categories (category_id) '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_DOCUMENT_CATEGORIES_NAME_UX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE UNIQUE INDEX rag_document_categories_name_ux '
            || 'ON rag_document_categories (NVL(parent_id, ''-''), name)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_DOCUMENT_CATEGORIES_PARENT_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_document_categories_parent_idx '
            || 'ON rag_document_categories (parent_id, sort_order)';
    END IF;
END;
/

-- migration: 20261018_001_support_guide_embeddings
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_SUPPORT_GUIDE_EMBEDDINGS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_support_guide_embeddings ( '
            || 'guide_id         VARCHAR2(64) NOT NULL, '
            || 'revision         NUMBER(19) NOT NULL, '
            || 'model_id         VARCHAR2(256) NOT NULL, '
            || 'dimension        NUMBER(10) NOT NULL, '
            || 'examples_sha256  CHAR(64) NOT NULL, '
            || 'vectors_json     CLOB NOT NULL, '
            || 'created_at       TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'CONSTRAINT rag_support_guide_embeddings_pk PRIMARY KEY (guide_id, revision), '
            || 'CONSTRAINT rag_support_guide_embeddings_rev_fk FOREIGN KEY (guide_id, revision) '
            || 'REFERENCES rag_support_guide_revisions (guide_id, revision) ON DELETE CASCADE '
            || ')';
    END IF;
END;
/

-- migration: 20261018_002_message_answer_notice
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_MESSAGES' AND column_name = 'ANSWER_NOTICE';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_messages ADD (answer_notice CLOB)';
    END IF;
END;
/

-- migration: 20261018_003_answer_record_notice
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_ANSWER_RECORDS' AND column_name = 'ANSWER_NOTICE';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_answer_records ADD (answer_notice CLOB)';
    END IF;
END;
/

-- migration: 20261018_004_support_guide_schema_v2
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_SUPPORT_GUIDES';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE q'[UPDATE rag_support_guides SET draft_json = JSON_TRANSFORM(draft_json, SET '$.schema_version' = 2) WHERE JSON_VALUE(draft_json, '$.schema_version' RETURNING NUMBER) = 1]';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_SUPPORT_GUIDE_REVISIONS';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE q'[UPDATE rag_support_guide_revisions SET content_json = JSON_TRANSFORM(content_json, SET '$.schema_version' = 2) WHERE JSON_VALUE(content_json, '$.schema_version' RETURNING NUMBER) = 1]';
    END IF;
    COMMIT;
END;
/

-- migration: 20261019_001_restore_document_classification
DECLARE
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
            TO_DATE(CASE WHEN REGEXP_LIKE(JSON_VALUE(effective_period, '$.effective_from'), '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') THEN JSON_VALUE(effective_period, '$.effective_from') END DEFAULT NULL ON CONVERSION ERROR, 'YYYY-MM-DD') AS period_from,
            TO_DATE(CASE WHEN REGEXP_LIKE(JSON_VALUE(effective_period, '$.effective_to'), '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') THEN JSON_VALUE(effective_period, '$.effective_to') END DEFAULT NULL ON CONVERSION ERROR, 'YYYY-MM-DD') AS period_to,
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
/

-- migration: 20261019_002_document_cleanups
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_DOCUMENT_CLEANUPS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_document_cleanups ( '
            || 'cleanup_id      VARCHAR2(64) PRIMARY KEY, '
            || 'document_id     VARCHAR2(64) NOT NULL, '
            || 'file_name       VARCHAR2(512) NOT NULL, '
            || 'tenant_id_hash  CHAR(64), '
            || 'user_id_hash    CHAR(64), '
            || 'targets         JSON NOT NULL, '
            || 'attempt_count   NUMBER(10) DEFAULT 0 NOT NULL, '
            || 'last_error_type VARCHAR2(128), '
            || 'created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_DOCUMENT_CLEANUPS_DOCUMENT_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_document_cleanups_document_idx '
            || 'ON rag_document_cleanups (document_id)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_DOCUMENT_CLEANUPS_UPDATED_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_document_cleanups_updated_idx '
            || 'ON rag_document_cleanups (updated_at)';
    END IF;
END;
/

-- migration: 20261019_003_structured_records_jsonl_profiles
DECLARE
    TYPE t_names IS TABLE OF VARCHAR2(261);
    v_targets t_names := t_names('RAG_DOCUMENTS.PROCESSING_CONFIG', 'RAG_DOCUMENT_RECIPES.PROCESSING_CONFIG', 'RAG_KNOWLEDGE_BASES.RETRIEVAL_CONFIG', 'RAG_INGESTION_JOBS.SETTINGS_OVERRIDES', 'RAG_CHUNK_SETS.RECIPE_SUBSET', 'RAG_DOCUMENT_EXTRACTIONS.RECIPE_SUBSET');
    v_recipes t_names := t_names('RAG_DOCUMENTS.PROCESSING_CONFIG', 'RAG_DOCUMENT_RECIPES.PROCESSING_CONFIG');
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
            v_expr := 'REPLACE(' || v_expr || ', ''"csv_to_json"'', ''"csv_to_jsonl"'')';
            v_expr := 'REPLACE(' || v_expr || ', ''"excel_to_json"'', ''"excel_to_jsonl"'')';
            v_expr := 'REPLACE(' || v_expr || ', ''"csv_to_json_format"'', ''"csv_to_jsonl_format"'')';
            v_expr := 'REPLACE(' || v_expr || ', ''"excel_to_json_format"'', ''"excel_to_jsonl_format"'')';
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
                || ', SET ''$.chunk_delimiter'' = ''\n\n'')')
                || ' WHERE ' || v_column || ' IS NOT NULL'
                || ' AND JSON_VALUE(' || v_json || ', ''$.chunking_strategy'')'
                || ' = ''fixed_delimiter'''
                || ' AND JSON_VALUE(' || v_json || ', ''$.chunk_delimiter'') IS NULL';
            COMMIT;
        END IF;
    END LOOP;
END;
/

-- migration: 20261019_010_message_attachments
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_MESSAGES' AND column_name = 'ATTACHMENTS_JSON';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE 'ALTER TABLE rag_messages ADD (attachments_json CLOB)';
    END IF;
END;
/

-- migration: 20261021_001_answer_prompt_placeholders
UPDATE RAG_ANSWER_PROMPTS
SET CONTENT = REPLACE(REPLACE(CONTENT,
    '{{images}}', '{{answer_context}}'),
    '{{image_metadata}}', '{{evidence_metadata}}')
WHERE PROMPT_KEY = 'vlm_answer';

-- migration: 20261021_002_document_delete_fk_indexes
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

-- migration: 20261022_001_cited_evidence
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

-- migration: 20261026_001_evaluation_calibration
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_EVALUATION_LABEL_SETS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_evaluation_label_sets ( '
            || 'label_set_id    VARCHAR2(64) PRIMARY KEY, '
            || 'tenant_id_hash  VARCHAR2(64), '
            || 'name            VARCHAR2(200 CHAR) NOT NULL, '
            || 'origin          VARCHAR2(16) NOT NULL, '
            || 'rubric_id       VARCHAR2(80) NOT NULL, '
            || 'rubric_version  VARCHAR2(40), '
            || 'rubric_sha256   VARCHAR2(64), '
            || 'items_sha256    VARCHAR2(64) NOT NULL, '
            || 'item_count      NUMBER(10) DEFAULT 0 NOT NULL, '
            || 'split_policy    JSON, '
            || 'sources         JSON, '
            || 'created_by_hash VARCHAR2(64), '
            || 'created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'CONSTRAINT rag_evaluation_label_sets_origin_ck CHECK (origin IN (''jobs'', ''import'')) '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_EVALUATION_LABEL_SETS_TENANT_CREATED_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_evaluation_label_sets_tenant_created_idx '
            || 'ON rag_evaluation_label_sets (tenant_id_hash, created_at DESC)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_EVALUATION_LABEL_ITEMS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_evaluation_label_items ( '
            || 'label_set_id    VARCHAR2(64) NOT NULL, '
            || 'item_id         VARCHAR2(200 CHAR) NOT NULL, '
            || 'item_order      NUMBER(10) NOT NULL, '
            || 'split           VARCHAR2(16) NOT NULL, '
            || 'origin          VARCHAR2(16) NOT NULL, '
            || 'excluded        NUMBER(1) DEFAULT 0 NOT NULL, '
            || 'item_json       JSON NOT NULL, '
            || 'CONSTRAINT rag_evaluation_label_items_pk PRIMARY KEY (label_set_id, item_id), '
            || 'CONSTRAINT rag_evaluation_label_items_set_fk FOREIGN KEY (label_set_id) '
            || 'REFERENCES rag_evaluation_label_sets (label_set_id) ON DELETE CASCADE, '
            || 'CONSTRAINT rag_evaluation_label_items_split_ck CHECK (split IN (''dev'', ''holdout'')), '
            || 'CONSTRAINT rag_evaluation_label_items_origin_ck CHECK (origin IN (''human'', ''synthetic'')), '
            || 'CONSTRAINT rag_evaluation_label_items_excluded_ck CHECK (excluded IN (0, 1)) '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_EVALUATION_LABELS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_evaluation_labels ( '
            || 'label_set_id    VARCHAR2(64) NOT NULL, '
            || 'item_id         VARCHAR2(200 CHAR) NOT NULL, '
            || 'aspect          VARCHAR2(80) NOT NULL, '
            || 'role            VARCHAR2(16) NOT NULL, '
            || 'annotator_code  VARCHAR2(40) NOT NULL, '
            || 'annotator_hash  VARCHAR2(64), '
            || 'label           VARCHAR2(16) NOT NULL, '
            || 'error_type      VARCHAR2(40), '
            || 'note            VARCHAR2(2000 CHAR), '
            || 'created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'CONSTRAINT rag_evaluation_labels_pk '
            || 'PRIMARY KEY (label_set_id, item_id, aspect, role, annotator_code), '
            || 'CONSTRAINT rag_evaluation_labels_item_fk FOREIGN KEY (label_set_id, item_id) '
            || 'REFERENCES rag_evaluation_label_items (label_set_id, item_id) ON DELETE CASCADE, '
            || 'CONSTRAINT rag_evaluation_labels_role_ck CHECK (role IN (''annotation'', ''adjudication'')), '
            || 'CONSTRAINT rag_evaluation_labels_label_ck CHECK (label IN (''pass'', ''fail'', ''cannot_judge'')) '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_EVALUATION_LABELS_ANNOTATOR_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_evaluation_labels_annotator_idx '
            || 'ON rag_evaluation_labels (label_set_id, annotator_hash)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_tables WHERE table_name = 'RAG_EVALUATION_CALIBRATIONS';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE TABLE rag_evaluation_calibrations ( '
            || 'calibration_id       VARCHAR2(64) PRIMARY KEY, '
            || 'label_set_id         VARCHAR2(64) NOT NULL, '
            || 'tenant_id_hash       VARCHAR2(64), '
            || 'user_id_hash         VARCHAR2(64), '
            || 'status               VARCHAR2(16) NOT NULL, '
            || 'repeats              NUMBER(2) DEFAULT 1 NOT NULL, '
            || 'judge_model_id       VARCHAR2(200 CHAR), '
            || 'judge_rubric_version VARCHAR2(40), '
            || 'prompt_sha256        VARCHAR2(64), '
            || 'labels_sha256        VARCHAR2(64), '
            || 'passed               NUMBER(1), '
            || 'error_message        VARCHAR2(2000 CHAR), '
            || 'summary_json         JSON, '
            || 'report_json          JSON, '
            || 'created_at           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, '
            || 'finished_at          TIMESTAMP WITH TIME ZONE, '
            || 'CONSTRAINT rag_evaluation_calibrations_set_fk FOREIGN KEY (label_set_id) '
            || 'REFERENCES rag_evaluation_label_sets (label_set_id) ON DELETE CASCADE, '
            || 'CONSTRAINT rag_evaluation_calibrations_status_ck '
            || 'CHECK (status IN (''RUNNING'', ''SUCCEEDED'', ''FAILED'', ''CANCELLED'')), '
            || 'CONSTRAINT rag_evaluation_calibrations_passed_ck CHECK (passed IN (0, 1)) '
            || ')';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_EVALUATION_CALIBRATIONS_SET_CREATED_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_evaluation_calibrations_set_created_idx '
            || 'ON rag_evaluation_calibrations (label_set_id, created_at DESC)';
    END IF;
    SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'RAG_EVALUATION_CALIBRATIONS_JUDGE_IDX';
    IF v_count = 0 THEN
        EXECUTE IMMEDIATE
            'CREATE INDEX rag_evaluation_calibrations_judge_idx '
            || 'ON rag_evaluation_calibrations (tenant_id_hash, judge_model_id, prompt_sha256, status)';
    END IF;
END;
/

-- migration: 20261026_002_evaluation_job_calibration_kind
DECLARE
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
/

-- migration: 20261027_001_raptor_prompt_consolidation
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

-- migration: 20261028_001_language_pack_releases
CREATE TABLE rag_language_pack_releases (
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
        REFERENCES rag_language_pack_releases (release_id),
    CONSTRAINT rag_lang_release_restore_fk FOREIGN KEY (restored_from)
        REFERENCES rag_language_pack_releases (release_id)
);
CREATE TABLE rag_language_pack_state (
    singleton_id NUMBER(1) PRIMARY KEY,
    draft_revision NUMBER(19) NOT NULL,
    draft_effective_json JSON NOT NULL,
    current_release_id VARCHAR2(64),
    CONSTRAINT rag_lang_state_singleton_ck CHECK (singleton_id = 1),
    CONSTRAINT rag_lang_state_revision_ck CHECK (draft_revision >= 1),
    CONSTRAINT rag_lang_state_current_fk FOREIGN KEY (current_release_id)
        REFERENCES rag_language_pack_releases (release_id)
);
CREATE INDEX rag_lang_release_previous_idx ON rag_language_pack_releases (previous_id);
CREATE INDEX rag_lang_release_restore_idx ON rag_language_pack_releases (restored_from);
CREATE INDEX rag_lang_state_current_idx ON rag_language_pack_state (current_release_id);

-- migration: 20261028_002_language_pack_artifacts
DECLARE v_count NUMBER; BEGIN
                SELECT COUNT(*) INTO v_count FROM user_tab_columns
                WHERE table_name = 'RAG_INGESTION_JOBS' AND column_name = 'LANGUAGE_PACK_RELEASE_ID';
                IF v_count = 0 THEN EXECUTE IMMEDIATE
                    'ALTER TABLE RAG_INGESTION_JOBS ADD (LANGUAGE_PACK_RELEASE_ID VARCHAR2(64))'; END IF;
            END;
            /
DECLARE v_count NUMBER; BEGIN
                SELECT COUNT(*) INTO v_count FROM user_tab_columns
                WHERE table_name = 'RAG_CHUNK_SETS' AND column_name = 'LANGUAGE_PACK_RELEASE_ID';
                IF v_count = 0 THEN EXECUTE IMMEDIATE
                    'ALTER TABLE RAG_CHUNK_SETS ADD (LANGUAGE_PACK_RELEASE_ID VARCHAR2(64))'; END IF;
            END;
            /
DECLARE v_count NUMBER; BEGIN
                SELECT COUNT(*) INTO v_count FROM user_tab_columns
                WHERE table_name = 'RAG_CHUNK_SETS' AND column_name = 'LANGUAGE_PACK_INGEST_HASH';
                IF v_count = 0 THEN EXECUTE IMMEDIATE
                    'ALTER TABLE RAG_CHUNK_SETS ADD (LANGUAGE_PACK_INGEST_HASH VARCHAR2(64))'; END IF;
            END;
            /
DECLARE v_count NUMBER; BEGIN
                SELECT COUNT(*) INTO v_count FROM user_tab_columns
                WHERE table_name = 'RAG_CHUNK_SETS' AND column_name = 'LANGUAGE_PACK_JOB_ID';
                IF v_count = 0 THEN EXECUTE IMMEDIATE
                    'ALTER TABLE RAG_CHUNK_SETS ADD (LANGUAGE_PACK_JOB_ID VARCHAR2(64))'; END IF;
            END;
            /
DECLARE v_count NUMBER; BEGIN
                SELECT COUNT(*) INTO v_count FROM user_tab_columns
                WHERE table_name = 'RAG_CHUNK_SETS' AND column_name = 'LANGUAGE_PACK_BUILT_AT';
                IF v_count = 0 THEN EXECUTE IMMEDIATE
                    'ALTER TABLE RAG_CHUNK_SETS ADD (LANGUAGE_PACK_BUILT_AT TIMESTAMP WITH TIME ZONE)'; END IF;
            END;
            /
DECLARE v_count NUMBER; BEGIN
            SELECT COUNT(*) INTO v_count FROM user_constraints
            WHERE constraint_name = 'RAG_INGEST_JOB_LANG_RELEASE_FK';
            IF v_count = 0 THEN EXECUTE IMMEDIATE
                'ALTER TABLE RAG_INGESTION_JOBS ADD CONSTRAINT RAG_INGEST_JOB_LANG_RELEASE_FK '
                || 'FOREIGN KEY (LANGUAGE_PACK_RELEASE_ID) '
                || 'REFERENCES RAG_LANGUAGE_PACK_RELEASES (RELEASE_ID)'; END IF;
        END;
        /
DECLARE v_count NUMBER; BEGIN
            SELECT COUNT(*) INTO v_count FROM user_constraints
            WHERE constraint_name = 'RAG_CHUNK_SET_LANG_RELEASE_FK';
            IF v_count = 0 THEN EXECUTE IMMEDIATE
                'ALTER TABLE RAG_CHUNK_SETS ADD CONSTRAINT RAG_CHUNK_SET_LANG_RELEASE_FK '
                || 'FOREIGN KEY (LANGUAGE_PACK_RELEASE_ID) '
                || 'REFERENCES RAG_LANGUAGE_PACK_RELEASES (RELEASE_ID)'; END IF;
        END;
        /

-- migration: 20261029_001_retire_query_history
BEGIN
    EXECUTE IMMEDIATE 'DROP TABLE rag_query_history PURGE';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -942 THEN
            RAISE;
        END IF;
END;
/
