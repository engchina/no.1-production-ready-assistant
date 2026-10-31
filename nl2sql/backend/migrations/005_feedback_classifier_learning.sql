-- Feedback / classifier learning metadata backfill for incremental NL2SQL state.
-- Existing NL2SQL_STATE_DOCUMENTS columns and indexes are reused.

UPDATE NL2SQL_STATE_DOCUMENTS
SET PROFILE_ID = COALESCE(
        JSON_VALUE(PAYLOAD_JSON, '$.profile_id' RETURNING VARCHAR2(128) NULL ON ERROR),
        PROFILE_ID
    ),
    STATUS = COALESCE(
        JSON_VALUE(PAYLOAD_JSON, '$.feedback_rating' RETURNING VARCHAR2(32) NULL ON ERROR),
        'unrated'
    )
WHERE COLLECTION = 'history';

UPDATE NL2SQL_STATE_DOCUMENTS
SET PROFILE_ID = COALESCE(
        JSON_VALUE(PAYLOAD_JSON, '$.profile_id' RETURNING VARCHAR2(128) NULL ON ERROR),
        PROFILE_ID
    )
WHERE COLLECTION = 'classifier_examples';

INSERT INTO NL2SQL_SCHEMA_MIGRATIONS (VERSION_NO, DESCRIPTION, CHECKSUM)
VALUES (5, 'feedback classifier learning metadata', 'runtime-verified');
