-- NL2SQL profile 名を大文字小文字非依存で一意にする。
-- 既存データに重複がある場合は、運用者が rename してから再実行する。

DECLARE
    duplicate_count NUMBER;
BEGIN
    SELECT COUNT(*)
      INTO duplicate_count
      FROM (
          SELECT UPPER(NAME) AS NAME_KEY
            FROM NL2SQL_PROFILES
           GROUP BY UPPER(NAME)
          HAVING COUNT(*) > 1
      );

    IF duplicate_count > 0 THEN
        RAISE_APPLICATION_ERROR(
            -20062,
            'Duplicate NL2SQL profile names exist. Rename duplicates before applying migration 018.'
        );
    END IF;
END;
/

CREATE UNIQUE INDEX UX_NL2SQL_PROFILES_NAME
    ON NL2SQL_PROFILES (UPPER(NAME));
