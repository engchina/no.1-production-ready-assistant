-- Allow user-cancelled durable NL2SQL quality evaluation jobs.

BEGIN
    EXECUTE IMMEDIATE 'ALTER TABLE NL2SQL_EVALUATION_JOBS DROP CONSTRAINT CK_NL2SQL_EVAL_JOB_STATUS';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -2443 THEN
            RAISE;
        END IF;
END;

BEGIN
    EXECUTE IMMEDIATE q'[
        ALTER TABLE NL2SQL_EVALUATION_JOBS
        ADD CONSTRAINT CK_NL2SQL_EVAL_JOB_STATUS CHECK (
            STATUS IN (
                'pending',
                'running',
                'completed',
                'completed_with_errors',
                'failed',
                'cancelled'
            )
        )
    ]';
END;

INSERT INTO NL2SQL_SCHEMA_MIGRATIONS (VERSION_NO, DESCRIPTION, CHECKSUM)
VALUES (17, 'allow cancelled quality evaluation jobs', 'runtime-verified');
