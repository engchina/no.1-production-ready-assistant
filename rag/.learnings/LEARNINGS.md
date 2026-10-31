# Learnings

## [LRN-20260622-001] correction

**Logged**: 2026-06-22T12:45:00+09:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
Cross-cutting HTTP reliability requests must be implemented as shared infrastructure, not as a single caller-specific retry.

### Details
The initial fix focused on parser service retry after a parser-unstructured connection issue. The user clarified that all HTTP service calls in the system need a common exponential retry policy with 5 attempts.

### Suggested Action
When a user describes retry, timeout, or availability behavior for “the system” or “all services”, first identify every production HTTP service call site and route them through a shared helper with tests.

### Metadata
- Source: user_feedback
- Related Files: backend/app/clients/http_retry.py
- Tags: retry, http, reliability, shared-infrastructure

---

## [LRN-20260630-001] correction

**Logged**: 2026-06-30T21:58:05+09:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
Oracle 接続確認と migration は `backend/.env` を明示的に読み込み、ネットワーク sandbox 外で実行する。

### Details
通常 sandbox 内の接続失敗を DB 自体の到達不能と誤認した。`uv run --env-file .env` と許可済みの sandbox 外実行では同じ接続先へ正常接続できた。

### Suggested Action
Oracle の診断・migration では最初から `backend/.env` を指定し、sandbox 内で ORA-12545 が出た場合は秘密情報を出力せず同一コマンドを network sandbox 外で再検証する。

### Metadata
- Source: user_feedback
- Related Files: backend/.env, backend/tests/_oracle_test_db.py
- Tags: oracle, migration, env, sandbox

### Resolution
- **Resolved**: 2026-06-30T21:58:05+09:00
- **Notes**: `backend/.env` で接続し、recipe migration 45/45 を適用した。

---
