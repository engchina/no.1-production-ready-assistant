-- NL2SQL system tables の状態確認用 smoke SQL。
-- 検証 DB の read-only 確認で使用する。DDL は実行しない。

SELECT table_name
FROM user_tables
WHERE table_name LIKE 'NL2SQL_%'
ORDER BY table_name;

SELECT object_name, object_type, status
FROM user_objects
WHERE object_name LIKE 'NL2SQL_%'
ORDER BY object_type, object_name;

SELECT version
FROM nl2sql_schema_migrations
ORDER BY version;

