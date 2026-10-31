## [ERR-20260723-THR] sandbox_asyncio_to_thread_hang

**Logged**: 2026-07-23T00:00:00+09:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
managed workspace sandbox では `asyncio.to_thread()` と ThreadPoolExecutor が完了せず、ASGI 単体テストが停止した。

### Error
```text
pytest が system schema API の `await asyncio.to_thread(...)` で進行しない。
単独の asyncio.to_thread(lambda: "ok") も timeout。
```

### Context
- production では既存 Oracle client も executor を使うため、API 実装の非同期 offload は維持する。
- 決定論的 API unit test では offload 関数を inline async stub へ差し替えられる。

### Suggested Fix
thread 制限のある sandbox では API test の offload 境界を inline stub 化し、実 DB / production では executor を使用する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/api/routes/health.py, backend/app/api/routes/settings.py

### Resolution
- **Resolved**: 2026-07-23T00:00:00+09:00
- **Notes**: 対象 API tests で `asyncio.to_thread` を inline stub に差し替えた。

---

## [ERR-20260723-UV1] uv_readonly_default_cache

**Logged**: 2026-07-23T00:00:00+09:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
workspace-write sandbox では uv の既定 cache `/root/.cache/uv` が読み取り専用で、検証コマンドが開始前に失敗する。

### Error
```text
error: Could not acquire lock
Caused by: Could not create temporary file
Caused by: Read-only file system at /root/.cache/uv/.tmp...
```

### Context
- `uv run ruff check ...` を backend ディレクトリから実行した。
- source code の検査前に uv cache lock 作成で停止した。

### Suggested Fix
sandbox 内の検証では `UV_CACHE_DIR=/tmp/production-ready-rag-uv-cache` を指定する。

### Metadata
- Reproducible: yes
- Related Files: backend/uv.lock

### Resolution
- **Resolved**: 2026-07-23T00:00:00+09:00
- **Notes**: writable な `/tmp` cache を使って再実行する。

---

## [ERR-20260703-007] service_testclient_httpx_deadlock

**Logged**: 2026-07-03T05:23:00+09:00
**Priority**: low
**Status**: pending
**Area**: service tests

### Summary
chunking service の FastAPI `TestClient` が backend venv の deprecated httpx 組合せで request 中に停止した。

### Error
```text
StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.
test_health_ok が response を返さず timeout した。
```

### Context
- test collection と service app import は成功する。
- 共有 stage request / app factory の parity テストは backend suite で通過した。

### Suggested Fix
service 専用 test environment の FastAPI/Starlette/httpx test transport を互換版へ揃える。

### Metadata
- Reproducible: yes
- Related Files: services/pipeline/chunking/pyproject.toml, services/pipeline/chunking/tests/test_chunking_stage.py

---

## [ERR-20260703-GHA] gh_cli_expired_token

**Logged**: 2026-07-03T14:55:21+09:00
**Priority**: high
**Status**: resolved
**Area**: infra

### Summary
GitHub CLI の保存済み token が失効し、PR・CI・merge 操作を開始できなかった。

### Error
```text
The token in /root/.config/gh/hosts.yml is invalid.
Browser auto-connect rejected: explicit user authorization is required for ambient authenticated session access.
```

### Context
- `gh auth status` で account `engchina` の認証を確認した。
- Git SSH remote は別経路で、branch push は成功した。
- 既存 browser session を使う PR 作成は、明示的なユーザー承認が必要な認証境界で停止した。

### Suggested Fix
既存 browser session の利用をユーザーが明示承認するか、`gh auth login -h github.com` で再認証する。

### Metadata
- Reproducible: yes
- Related Files: none
- Recurrence-Count: 2
- Last-Seen: 2026-07-03T15:15:00+09:00

### Resolution
- **Resolved**: 2026-07-03T15:30:00+09:00
- **Notes**: GitHub device flow で再認証し、PR #53 の作成と CI 監視を開始した。

---

## [ERR-20260703-013] rem_based_touch_target_and_score_width

**Logged**: 2026-07-03T11:10:00+09:00
**Priority**: medium
**Status**: resolved
**Area**: frontend tests

### Summary
root font-size 14px の画面で rem ベースの Tailwind 幅・高さを px 値と誤認し、スコア欄とモバイル操作領域が設計値を下回った。

### Error
```text
Expected score panel width: 176px, received: 154px
Expected touch target height: >= 44px, received: 38.5px
```

### Context
- `w-44` / `h-11` は root font-size 14px では 154px / 38.5px になる。
- desktop/mobile Playwright の実寸アサーションで検出した。

### Suggested Fix
物理 px が受入基準のスコア欄とタッチ領域には `w-[176px]` / `h-[44px]` を使い、Playwright で実寸を維持する。

### Metadata
- Reproducible: yes
- Related Files: frontend/src/components/search/CitationCard.tsx, frontend/src/components/feedback/FeedbackControls.tsx, frontend/e2e/citation-variant-badge.spec.ts

### Resolution
- **Resolved**: 2026-07-03T11:10:00+09:00
- **Notes**: 対象寸法を明示 px に変更し、desktop/mobile の実寸回帰テストを追加した。

---

## [ERR-20260703-A17] oracle_target_mismatch

**Logged**: 2026-07-03T07:21:48+09:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
ローカルコンテナ向け migration を計画したが、backend は共有 OCI ADB を参照していた。

### Error
```text
ORA-01017: invalid credential or not authorized; logon denied
```

### Context
- ローカル `oracle-database` へ schema migration artifact を適用しようとした。
- backend の実 DSN は Wallet alias `aiomladb0121_high` で、ローカル Free DB ではなかった。
- 認証で停止したため、ローカルコンテナへの DDL は実行されていない。

### Suggested Fix
DB 変更前に backend の実 DSN と計画上の対象を照合し、共有 DB なら明示承認を得る。

### Metadata
- Reproducible: yes
- Related Files: backend/.env, backend/app/config.py

### Resolution
- **Resolved**: 2026-07-03T07:21:48+09:00
- **Notes**: 接続先不一致を確認し、共有 ADB への書き込みは実行せず明示承認待ちにした。

---

## [ERR-20260703-008] uv_cache_read_only_in_sandbox

**Logged**: 2026-07-03T06:00:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
通常 sandbox では uv の既定キャッシュ `/root/.cache/uv` が読み取り専用で、検証コマンドが開始できなかった。

### Error
```text
Could not acquire lock: Could not create temporary file: Read-only file system at /root/.cache/uv
```

### Context
- backend の ruff と Python 構文検証を `uv run` で開始した。
- repository 自体ではなく uv の一時 lock 作成だけが失敗した。

### Suggested Fix
sandbox 内の uv 実行では `UV_CACHE_DIR=/tmp/uv-cache` を指定する。

### Metadata
- Reproducible: yes
- Related Files: backend/pyproject.toml

### Resolution
- **Resolved**: 2026-07-03T06:00:00+09:00
- **Notes**: 以降の検証コマンドで writable な `/tmp/uv-cache` を使用する。

---

## [ERR-20260703-009] unit_tests_probed_real_oracle

**Logged**: 2026-07-03T06:05:00+09:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
通常の unit test 実行でも `backend/.env` の Oracle DSN を読み、session fixture が実 DB 接続と schema 確認を開始したため長時間停止した。

### Error
```text
pytest collected tests, then waited in the session-level real Oracle availability/schema fixture
```

### Context
- `tests/test_grounding_adapter.py` 単体でも `_oracle_db_session` が autouse で実行される。
- 今回の CI 相当検証は決定論 unit test だけで、実 Oracle は不要だった。

### Suggested Fix
unit test 実行時は `ORACLE_DSN=` を明示し、実 DB 検証は専用 staging run に分離する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/conftest.py, backend/tests/_oracle_test_db.py

### Resolution
- **Resolved**: 2026-07-03T06:05:00+09:00
- **Notes**: 以降の unit test コマンドで `ORACLE_DSN=` を指定する。

---

## [ERR-20260703-010] full_backend_suite_waited_on_external_path

**Logged**: 2026-07-03T07:08:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Oracle DSN を無効化した全量 pytest でも、外部サービス待機を含む既存テスト経路で進捗が止まった。

### Error
```text
pytest reached about 17% and then produced no progress for over one minute
```

### Context
- grounding の対象テストは個別実行ですべて完了している。
- 全量実行は複数の未コミット機能変更を含む worktree で、早期失敗も既に発生していた。

### Suggested Fix
CI unit suite から実サービス待機テストを marker で分離し、各外部 client に短い決定論 timeout を設定する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests, backend/app/clients

### Resolution
- **Resolved**: 2026-07-03T07:08:00+09:00
- **Notes**: 全量 run を中止し、grounding 関連 suite と静的検査を個別完走させた。

---

## [ERR-20260703-008] uv_cache_read_only

**Logged**: 2026-07-03T12:00:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
managed sandbox では既定の `/root/.cache/uv` が read-only のため uv コマンドが開始前に失敗した。

### Error
```text
error: Could not acquire lock
Caused by: Could not create temporary file
Caused by: Read-only file system at /root/.cache/uv
```

### Context
- 回答生成設定変更後の対象 Ruff check を `uv run ruff check ...` で実行した。
- workspace と `/tmp` は書き込み可能だが、root cache は書き込み不可だった。

### Suggested Fix
managed sandbox 内の uv コマンドでは `UV_CACHE_DIR=/tmp/uv-cache` を指定する。

### Metadata
- Reproducible: yes
- Related Files: backend/pyproject.toml

### Resolution
- **Resolved**: 2026-07-03T12:00:00+09:00
- **Notes**: 以後の uv コマンドは `/tmp/uv-cache` を使用する。

---

## [ERR-20260703-009] search_stream_patch_context_drift

**Logged**: 2026-07-03T12:30:00+09:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
Ruff 整形後の検索 SSE import/context と一致しない複数 hunk patch が原子的に失敗した。

### Error
```text
apply_patch verification failed: Failed to find expected lines
```

### Context
- raw-token callback を route 層から除去する差分を再適用しようとした。
- 現行ファイルを確認すると同変更は既に反映済みだった。

### Suggested Fix
自動整形や並行変更後は対象範囲を再読込し、未反映の hunk だけを小さく適用する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/api/routes/search.py

### Resolution
- **Resolved**: 2026-07-03T12:30:00+09:00
- **Notes**: 現行コードで callback 不使用を確認し、重複 patch を中止した。

---

## [ERR-20260703-010] oracle_generation_test_sqlcall_field

**Logged**: 2026-07-03T12:45:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
追加した Oracle generation test が fake SQL call の既存フィールド名を誤認した。

### Error
```text
AttributeError: 'SqlCall' object has no attribute 'binds'
```

### Context
- `SqlCall` の bind 値は `parameters` に保存される既存 test helper だった。

### Suggested Fix
既存 fake/helper の dataclass 定義を確認してから assertion を追加する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_oracle_adapter.py

### Resolution
- **Resolved**: 2026-07-03T12:45:00+09:00
- **Notes**: assertion を `update.parameters` へ修正した。

---

## [ERR-20260703-011] generation_radio_pointer_interception

**Logged**: 2026-07-03T13:00:00+09:00
**Priority**: medium
**Status**: resolved
**Area**: frontend

### Summary
回答スタイルの visually-hidden native radio が label 内の文字に覆われ、Playwright の check が pointer interception で timeout した。

### Error
```text
locator.check: span from label subtree intercepts pointer events
```

### Context
- semantic role と方向キー操作は成立したが、input 本体へのポインター操作が不安定だった。
- desktop/mobile の保存・custom・revision 競合用例で再現した。

### Suggested Fix
カード label は維持しつつ native radio 本体をタイトル領域に表示し、input 自身を直接操作可能にする。

### Metadata
- Reproducible: yes
- Related Files: frontend/src/components/settings/GenerationSettingsClient.tsx, frontend/e2e/generation-guardrail-settings.spec.ts

### Resolution
- **Resolved**: 2026-07-03T13:00:00+09:00
- **Notes**: radio を可視化し、assertion を native checked state に変更した。

---

## [ERR-20260703-006] document_recipe_combobox_polling_detach

**Logged**: 2026-07-03T05:10:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend e2e

### Summary
文書処理設定の既存 E2E で、文書解析 combobox が polling 中に繰り返し再 mount され click が timeout した。

### Error
```text
locator.click: element was detached from the DOM, retrying
Test timeout of 30000ms exceeded.
```

### Context
- `保存失敗時は編集値を保持する` の既存シナリオで再現した。
- 今回追加した意味境界の desktop/mobile 2 シナリオと更新した分割プレビューは通過した。

### Suggested Fix
処理レシピの query 更新で編集中フォームを再 mount しないよう、選択レシピの安定した identity と編集 state を維持する。

### Metadata
- Reproducible: yes
- Related Files: frontend/src/components/documents/DocumentRecipeManager.tsx, frontend/e2e/document-processing-config.spec.ts

### Resolution
- **Resolved**: 2026-07-03T08:45:00+09:00
- **Notes**: recipe の structural identity が変わらない間は設定 payload を `useMemo` で安定化し、polling 中のフォーム再初期化を止めた。desktop/mobile の回帰テストで確認した。

---

## [ERR-20260701-010] mypy_unrelated_feedback_route

**Logged**: 2026-07-01T23:12:00+09:00
**Priority**: low
**Status**: pending
**Area**: backend typecheck

### Summary
全 backend mypy が、並行作業で追加された未追跡の feedback route の型エラー1件で失敗した。

### Error
```text
app/api/routes/feedback.py:102: error: No overload variant of "int" matches argument type "object"  [call-overload]
```

### Context
- Search Answer Profile 設定変更とは無関係な `backend/app/api/routes/feedback.py` で発生した。
- 当該ファイルは本タスク開始後に別作業から追加されており、変更を上書きしない方針で触れていない。

### Suggested Fix
feedback route 側で入力を具体型へ検証・narrowing してから `int()` へ渡す。

### Metadata
- Reproducible: yes
- Related Files: backend/app/api/routes/feedback.py

---

## [ERR-20260703-004] apply_patch_multi_hunk_context_drift

**Logged**: 2026-07-03T04:31:00+09:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
共有 chunking 修正の複数 hunk patch が、変更中の作業ツリーとの context 不一致で適用されなかった。

### Error
```text
apply_patch verification failed: Failed to find expected lines
```

### Context
- 未コミット変更を含む `packages/rag_pipeline_core/rag_pipeline_core/chunking.py` に複数箇所を一括 patch しようとした。
- `apply_patch` は原子的に失敗し、対象ファイルは変更されなかった。

### Suggested Fix
対象関数の現行内容を再読込し、独立した小さい hunk に分けて適用する。同一ファイルの複数 hunk は必ず行番号順に並べる。

### Metadata
- Reproducible: yes
- Related Files: packages/rag_pipeline_core/rag_pipeline_core/chunking.py
- Pattern-Key: tooling.apply_patch.context_drift
- Recurrence-Count: 2
- Last-Seen: 2026-07-03

### Resolution
- **Resolved**: 2026-07-03T04:31:00+09:00
- **Notes**: 現行 context を再取得し、小さい patch へ分割した。

---

## [ERR-20260703-005] low_level_min_chars_default_regression

**Logged**: 2026-07-03T05:03:00+09:00
**Priority**: medium
**Status**: resolved
**Area**: chunking

### Summary
製品設定の `min_chars=120` を低レベル分割関数の既定値にも適用し、小さい chunk size の二次分割を再結合してしまった。

### Error
```text
page/heading の境界内分割が 1 chunk に再結合され、parent-child の child_size 上限も超過した。
```

### Context
- 製品の実行既定値と、任意サイズを扱う低レベル関数の既定値を同一視した。
- backend の関連テストで page/heading/parent-child の 3 退化を検出した。

### Suggested Fix
製品入口は `min_chars=120` を維持し、低レベル `chunk_extraction_with_strategy()` は明示指定がない限り再結合しない `0` を維持する。

### Metadata
- Reproducible: yes
- Related Files: packages/rag_pipeline_core/rag_pipeline_core/chunking.py

### Resolution
- **Resolved**: 2026-07-03T05:03:00+09:00
- **Notes**: 低レベル関数だけ `min_chars=0` に戻し、製品設定と pipeline request は 120 を維持した。

---

## [ERR-20260703-001] uv_cache_read_only

**Logged**: 2026-07-03T00:00:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Managed sandbox の既定 uv cache が read-only で、ruff/mypy が起動前に失敗した。

### Error
```text
Could not acquire lock: Read-only file system at /root/.cache/uv
```

### Context
- `uv run ruff check ...` と `uv run mypy ...` の同時実行。
- コード検査前の cache lock 作成で失敗した。

### Suggested Fix
`env UV_CACHE_DIR=/tmp/uv-cache uv run ...` で検証する。

### Metadata
- Reproducible: yes
- Related Files: backend/pyproject.toml
- See Also: ERR-20260618-001

### Resolution
- **Resolved**: 2026-07-03T00:00:00+09:00
- **Notes**: writable な `/tmp/uv-cache` を指定して再実行した。

---

## [ERR-20260702-010] duplicate_backend_path_in_workdir

**Logged**: 2026-07-02T22:31:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tooling

### Summary
backend を作業ディレクトリにした状態で `backend/` を重ね、存在しないパスを読もうとした。

### Error
```text
sed: can't read backend/app/schemas/document.py: No such file or directory
```

### Context
- `workdir` は既に `/u01/workspace/no.1-production-ready-rag/backend` だった。

### Suggested Fix
コマンドのパスは常に指定した `workdir` からの相対として確認する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/schemas/document.py

### Resolution
- **Resolved**: 2026-07-02T22:31:00+09:00
- **Notes**: `app/schemas/document.py` として再実行した。

---

## [ERR-20260702-009] full_pytest_wip_contract_drift

**Logged**: 2026-07-02T22:30:00+09:00
**Priority**: medium
**Status**: pending
**Area**: backend tests

### Summary
chunking 対象テストは通過したが full pytest は既存 WIP の契約差分で48件失敗した。

### Error
```text
48 failed: PREPROCESS 起点への移行、neighbor_window=1、preprocess service 既定、
Oracle chunk-set/extraction 状態などの旧期待値が現行実装と不一致
```

### Context
- 今回の対象テスト（chunking/settings/ingestion/variant/Oracle schema/preview）は通過。
- 代表例は `/ingest` が PREPROCESS job を返す一方、旧テストが EXTRACT/INDEXED を1回で期待するもの。
- grounding/pipeline の旧テストは neighbor expansion 無効を期待するが、現行既定は1。

### Suggested Fix
既存 WIP の工程移行を別タスクで完了し、legacy API の自動進行契約と全体テストの期待値を同時に更新する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_rag_flow.py, backend/tests/test_two_phase_review.py, backend/tests/test_pipeline.py

---

## [ERR-20260702-008] chunk_preview_e2e_copy_mismatch

**Logged**: 2026-07-02T22:22:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
分割プレビュー E2E が、実装済み i18n 文言ではなく計画書の表現を期待して失敗した。

### Error
```text
Expected: この設定はプレビュー専用で保存されません
Received: 保存済みの抽出結果を一時設定で分割します。レシピ設定や工程状態は変更しません。
```

### Context
- desktop / 375px のレビュー画面検証で同じ文言差分が発生した。

### Suggested Fix
UI の正本である i18n キーの実文言を確認してから E2E assertion を記述する。

### Metadata
- Reproducible: yes
- Related Files: frontend/src/lib/i18n.ts, frontend/e2e/document-processing-config.spec.ts

### Resolution
- **Resolved**: 2026-07-02T22:22:00+09:00
- **Notes**: 現行 i18n 文言を期待値に使用した。

---

## [ERR-20260702-007] chunking_target_test_filename

**Logged**: 2026-07-02T12:00:00+09:00
**Priority**: low
**Status**: resolved
**Area**: backend tests

### Summary
対象 pytest の指定に存在しない `tests/test_config_defaults.py` を含めた。

### Error
```text
ERROR: file or directory not found: tests/test_config_defaults.py
```

### Context
- chunking 変更の対象テストをまとめて実行する際、実在する設定テスト名を確認せず指定した。

### Suggested Fix
実行前に `rg --files tests` で対象ファイルを確認する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_config.py

### Resolution
- **Resolved**: 2026-07-02T12:00:00+09:00
- **Notes**: 実在する `tests/test_config.py` へ置き換えた。

---

## [ERR-20260702-004] apply_patch_large_context_mismatch

**Logged**: 2026-07-02T21:57:02+09:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
複数の離れた SQL 変更をまとめた apply_patch が、作業中差分との文脈不一致で失敗した。

### Error
```text
apply_patch verification failed: Failed to find expected lines in backend/app/clients/oracle.py
```

### Context
- Oracle chunk insert、keyword query、schema DDL を一括パッチしようとした。
- 大きな未コミット WIP により対象周辺の整形が基準文脈と一致しなかった。

### Suggested Fix
対象箇所を再読込し、関数単位の小さな patch に分割する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/clients/oracle.py

### Resolution
- **Resolved**: 2026-07-02T21:57:02+09:00
- **Notes**: 変更を insert/query/update/DDL 単位へ分割した。

---

## [ERR-20260702-005] oracle_schema_search_text_bootstrap_order

**Logged**: 2026-07-02T22:00:00+09:00
**Priority**: medium
**Status**: resolved
**Area**: backend tests

### Summary
既存 Oracle schema に対する full DDL の新 index 作成が、列追加 migration より先に実行された。

### Error
```text
ORA-00904: "SEARCH_TEXT": invalid identifier
```

### Context
- test fixture は full schema DDL を先に冪等適用し、その後 migration を適用する。
- 既存 `rag_chunks` には `search_text` がなく、新しい Text index DDL が先に失敗した。

### Suggested Fix
既存列を migration で補う index の ORA-00904 は full DDL 段階で許容し、migration 後に作成する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/_oracle_test_db.py, backend/app/rag/oracle_schema.py

### Resolution
- **Resolved**: 2026-07-02T22:00:00+09:00
- **Notes**: fixture の既存 schema 互換条件へ SEARCH_TEXT index を追加した。

---

## [ERR-20260702-006] uv_cache_read_only_in_sandbox

**Logged**: 2026-07-02T22:02:00+09:00
**Priority**: low
**Status**: resolved
**Area**: backend tooling

### Summary
workspace sandbox 内の uv 実行が `/root/.cache/uv` の一時 lock を作れず失敗した。

### Error
```text
Could not acquire lock: Read-only file system at /root/.cache/uv
```

### Context
- 変更対象ファイルへ `uv run ruff check` を実行した。

### Suggested Fix
承認済みの限定 prefix で同じ lint command を sandbox 外実行する。

### Metadata
- Reproducible: yes
- Related Files: backend/pyproject.toml

### Resolution
- **Resolved**: 2026-07-02T22:02:00+09:00
- **Notes**: `uv run ruff check` のみ権限昇格して再実行した。

---

## [ERR-20260702-005] frontend_ci_timezone_and_nav_locator

**Logged**: 2026-07-02T07:54:39+09:00
**Priority**: medium
**Status**: resolved
**Area**: frontend tests

### Summary
Frontend CI が、ローカル時区に固定された会話時刻と重複したナビゲーション名の locator により失敗した。

### Error
```text
Expected: 2件・01/01 09:00; CI actual used UTC formatting
getByRole('link', { name: '品質評価' }) resolved to 2 elements
```

### Context
- `chat.spec.ts` は `2026-01-01T00:00:02Z` の表示を JST の `09:00` に固定していた。
- フィードバック導線と設定名整理後、サイドナビ内に同名の「品質評価」リンクが2件存在した。
- GitHub Actions の Frontend E2E は 482 passed、上記2テストの desktop/mobile 展開6件だけが失敗した。

### Suggested Fix
時刻は locale 形式を検証しつつ具体時区へ固定せず、同名ナビは対象 href で絞り込む。

### Metadata
- Reproducible: yes
- Related Files: frontend/e2e/chat.spec.ts, frontend/e2e/sidebar-accordion.spec.ts

### Resolution
- **Resolved**: 2026-07-02T07:54:39+09:00
- **Notes**: 時刻を形式正規表現へ変更し、検索・回答プロファイルの品質評価リンクを `/evaluation` で限定した。

---

## [ERR-20260702-004] git_branch_workspace_permission

**Logged**: 2026-07-02T08:00:00+09:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
Codex の workspace sandbox では `.git` が読み取り専用のため、通常権限での分岐作成に失敗した。

### Error
```text
fatal: cannot lock ref 'refs/heads/codex/feedback-and-chat-improvements': unable to create directory for .git/refs/heads/codex/feedback-and-chat-improvements
```

### Context
- `git switch -c codex/feedback-and-chat-improvements` を workspace sandbox 内で実行した。
- ソースは書き込み可能だが `.git` は読み取り専用として公開されている。

### Suggested Fix
分岐・commit など `.git` を更新する Git 操作は承認付き権限で実行する。

### Metadata
- Reproducible: yes
- Related Files: .git

### Resolution
- **Resolved**: 2026-07-02T08:00:00+09:00
- **Notes**: 承認付き Git 操作へ切り替えた。

---

## [ERR-20260702-001] frontend_css_entry_path_assumption

**Logged**: 2026-07-02T07:11:13+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
全局样式排查时假定入口为 `src/index.css`，导致读取命令提前失败。

### Error
```text
sed: can't read src/index.css: No such file or directory
```

### Context
- 设置页滚动问题排查中读取全局 CSS 与应用外壳。
- 本项目实际入口样式为 `frontend/src/globals.css`。

### Suggested Fix
读取样式入口前先用 `rg --files src` 确认文件名，并避免将独立读取通过 shell 控制符串联。

### Metadata
- Reproducible: yes
- Related Files: frontend/src/globals.css

### Resolution
- **Resolved**: 2026-07-02T07:11:13+09:00
- **Notes**: 已通过 `rg --files` 确认实际文件路径后继续排查。

---

## [ERR-20260701-009] playwright_multiselect_remained_open

**Logged**: 2026-07-01T23:10:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
検索・回答プロファイル作成 E2E で、KB 選択直後の「検索・回答プロファイルで上書き」クリックが状態を変更しなかった。

### Error
```text
Expected aria-pressed="true", received "false"
```

### Context
- KB picker は複数選択のため、option 選択後も listbox を開いたままにする。
- 展開中にフォーム下部をクリックすると、最初のクリックは outside-click として listbox を閉じるために消費される。
- トグル単体の実画面操作では正常に状態が変わることを確認した。

### Suggested Fix
複数選択後に別のコントロールを操作する E2E は、`Escape` で listbox を閉じてから次へ進む。

### Metadata
- Reproducible: yes
- Related Files: frontend/e2e/search-answer-profiles.spec.ts

### Resolution
- **Resolved**: 2026-07-01T23:10:00+09:00
- **Notes**: KB 選択後に combobox へ `Escape` を送り、上書きトグルと payload を検証する。

---

## [ERR-20260618-001] multipage_ingestion_stuck_in_ingesting_deadlock

**Logged**: 2026-06-18T00:00:00+09:00
**Priority**: high
**Status**: resolved
**Area**: ingestion

### Summary
多ページ PDF の取込が `INGESTING` のまま固着し、再試行のたびに 409 `このドキュメントは現在取込中です。` で失敗して永久に回復不能になる。UI には generic な `ingestion_error` だけが表示される。

### Error
```text
job EXTRACT FAILED (attempts=2) error_message='このドキュメントは現在取込中です。'
document status=INGESTING error_message=None  # 固着
```

### Context
- 多ページ PDF は ~N segment(既定 3 頁/segment)へ分割され subprocess で逐次 VLM 抽出される。subprocess がクラッシュ/強制終了されると clean な except が走らず、文書は `INGESTING`・job は `RUNNING` のまま残る。
- stale recovery は滞留 RUNNING job を QUEUED へ戻すが**文書状態を戻さない**。再投入 job が `_ingest_existing_document`(documents.py:962)の取込中ガードに弾かれ job だけ FAILED、文書は `INGESTING` のまま → デッドロック。
- 実 DB のジョブ履歴に本来の失敗原因(`max_output_tokens 上限で途中終了`、旧コードの `索引用チャンク数が上限`)が残っていたが、固着でマスクされていた。

### Suggested Fix
`recover_stale_ingestion_jobs` で job 復旧時に文書状態も復旧する(再キュー→EXTRACT は UPLOADED / INDEX は REVIEW、試行上限超過→ERROR)。さらに QUEUED/RUNNING job が無いのに `INGESTING`/`INDEXING` で取り残された文書を ERROR へ戻す orphan sweep を追加。worker は起動時に加えて `ingestion_queue_recovery_interval_seconds`(既定 60s)ごとにアイドル中も再実行する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/clients/oracle.py, backend/app/rag/ingestion_worker.py, backend/app/config.py

### Resolution
- **Resolved**: 2026-06-18
- **Notes**: oracle 復旧 + worker 定期回復 + 新 config を実装。test_oracle_adapter(orphan/stale)・test_ingestion_worker(定期回復)緑。固着済み文書は backend 再起動の startup recovery、または最大 60s のアイドル回復で ERROR へ遷移し再試行可能になる。

---

## [ERR-20260701-012] chat_playwright_selector_and_retry_drift

**Logged**: 2026-07-01T00:00:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
会話行へ編集ボタンを追加したことで旧 locator が複数ボタンに一致し、一覧エラー表示も Query retry の時間差で不安定になった。

### Error
```text
strict mode violation: getByRole('list', { name: '会話' }).getByRole('button') resolved to 2 elements
会話一覧を読み込めませんでした。: element(s) not found
```

### Context
- 会話選択と名前変更を兄弟ボタンへ分離した後の `chat.spec.ts` で再現した。
- 一覧エラー mock は既定 retry の完了が10秒を超える場合があった。

### Suggested Fix
選択 locator は件数・日時を含むボタンへ限定し、会話一覧 query は手動再試行UIがあるため自動 retry を無効化する。

### Metadata
- Reproducible: yes
- Related Files: frontend/e2e/chat.spec.ts, frontend/src/lib/queries.ts

### Resolution
- **Resolved**: 2026-07-01T00:00:00+09:00
- **Notes**: semantic locator を限定し、`useConversations` を `retry: false` にして対象エラー試験を desktop/mobile で通した。

---

## [ERR-20260701-011] pytest_general_feedback_base_schema_drift

**Logged**: 2026-07-01T00:00:00+09:00
**Priority**: high
**Status**: resolved
**Area**: backend tests

### Summary
実 Oracle を使う pytest session setup が、general feedback の新規索引と既存表の列差分で停止した。

### Error
```text
ORA-00904: "SEARCH_ANSWER_PROFILE_ID": invalid identifier
```

### Context
- Command attempted: `uv run pytest tests/test_chat_api.py tests/test_oracle_schema_cli.py -q`
- `tests/_oracle_test_db.py::ensure_schema()` が既存 `rag_citation_feedback` 表へ、未追加の `search_answer_profile_id` を含む索引DDLを適用した。
- 会話一覧変更のテスト本体へ入る前の session fixture で再現した。

### Suggested Fix
general feedback の base-schema 適用を既存表にも冪等に列追加してから索引作成するか、test schema setup で対応 migration を先に適用する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/clients/oracle.py, backend/app/rag/oracle_schema.py, backend/tests/_oracle_test_db.py
- See Also: ERR-20260701-008

### Resolution
- **Resolved**: 2026-07-01T00:00:00+09:00
- **Notes**: `ORACLE_DSN=` で外部DBを無効化し、対象36件とfull suiteを再実行した。

---

## [ERR-20260701-008] pytest_real_oracle_schema_drift

**Logged**: 2026-07-01T00:00:00+09:00
**Priority**: medium
**Status**: resolved
**Area**: backend tests

### Summary
対象単体テストの再実行が、並行中の実 Oracle schema 変更により setup で失敗した。

### Error
```text
ORA-00904: "SEARCH_ANSWER_PROFILE_ID": invalid identifier
```

### Context
- `tests/conftest.py` の session fixture が利用可能な実 DB へ自動接続した。
- 今回の Search Answer Profile 設定変更より前の schema 初期化段階で全テストが停止した。

### Suggested Fix
外部DBを必要としない対象テストは `ORACLE_DSN=` で決定論モードに固定する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/conftest.py, backend/tests/_oracle_test_db.py

### Resolution
- **Resolved**: 2026-07-01T00:00:00+09:00
- **Notes**: 外部DBを無効化して対象テストを再実行する。

---

## [ERR-20260701-007] apply_patch_multi_file_context_mismatch

**Logged**: 2026-07-01T00:00:00+09:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
複数ファイルの一括 patch が、設計コメントの単語差分により全体未適用になった。

### Error
```text
apply_patch verification failed: Failed to find expected lines in backend/app/rag/search_answer_profile_config.py
```

### Context
- Search Answer Profile の legacy 設定互換とテストを一括更新しようとした。
- 想定は「文書の物理索引方法」、実ファイルは「KB の物理索引方法」だった。

### Suggested Fix
関連箇所を直前に再表示し、意味単位の小さい patch に分割する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/rag/search_answer_profile_config.py

### Resolution
- **Resolved**: 2026-07-01T00:00:00+09:00
- **Notes**: 現行行を再確認し、patch を分割して再実行した。

---

## [ERR-20260701-006] ingestion_strategy_test_timeout

**Logged**: 2026-07-01T22:38:00+09:00
**Priority**: low
**Status**: pending
**Area**: backend tests

### Summary
追加実行した extraction artifact 統合テストが完了せず、60秒で timeout した。

### Error
```text
test_ingestion_pipeline_caches_extraction_artifact_and_segment_checkpoint
Process exited with code 124 after 60 seconds
```

### Context
- 対象テストは `rag_review_gate_enabled=False` と auto-parse 有効を明示しており、今回変更した段階自動進行の既定値には依存しない。
- 直接関連する config、effective config、document recipe/workspace の164テストは合格済み。

### Suggested Fix
別タスクで当該テストの待機箇所を特定し、外部処理または process executor を決定論 fake に置換する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_ingestion_strategy.py
- Recurrence-Count: 2
- Last-Seen: 2026-07-07

---

## [ERR-20260701-005] apply_patch_context_mismatch

**Logged**: 2026-07-01T22:31:21+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
`test_config.py` の推測したテスト名をコンテキストに使い、パッチ検証が失敗した。

### Error
```text
apply_patch verification failed: Failed to find expected lines in backend/tests/test_config.py
```

### Context
- 変更前に対象付近を確認せず、`test_default_graph_profile_is_off` という実在しない名前を指定した。
- パッチは原子的に失敗し、ファイル変更は発生しなかった。

### Suggested Fix
パッチ対象の直前・直後を先に読み、実在する最小コンテキストで適用する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_config.py

### Resolution
- **Resolved**: 2026-07-01T22:31:21+09:00
- **Notes**: 実在する `test_graph_profile_defaults_to_off` を確認して再適用した。

---

## [ERR-20260701-004] uv_cache_read_only

**Logged**: 2026-07-01T11:49:49+09:00
**Priority**: low
**Status**: resolved
**Area**: backend tests

### Summary
管理環境では既定の `/root/.cache/uv` が読み取り専用のため `uv run` が開始前に失敗する。

### Error
```text
error: Could not acquire lock
Caused by: Could not create temporary file
Caused by: Read-only file system (os error 30) at path "/root/.cache/uv/..."
```

### Context
- Command attempted: `uv run pytest tests/test_oracle_adapter.py -q`
- Workspace と `/tmp` は書き込み可能だが、既定の uv cache は書き込み不可。

### Suggested Fix
この管理環境では `UV_CACHE_DIR=/tmp/uv-cache` を指定して `uv run` を実行する。

### Metadata
- Reproducible: yes
- Related Files: backend/pyproject.toml

### Resolution
- **Resolved**: 2026-07-01T11:49:49+09:00
- **Notes**: 後続の検証コマンドを `/tmp/uv-cache` に切り替えた。

---

## [ERR-20260701-001] vitest_run_in_band_option

**Logged**: 2026-07-01T00:00:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
Vitest に Jest 専用の `--runInBand` を渡したため、テスト実行前に CLI が終了した。

### Error
```text
CACError: Unknown option `--runInBand`
```

### Context
- Command attempted: `npm run test -- --runInBand`
- `package.json` の test script は `vitest run` を使用している。

### Suggested Fix
このリポジトリでは追加引数なしの `npm run test` を使用する。

### Metadata
- Reproducible: yes
- Related Files: frontend/package.json

### Resolution
- **Resolved**: 2026-07-01T00:00:00+09:00
- **Notes**: リポジトリ標準の `npm run test` で再実行する。

---

## [ERR-20260630-012] recipe_migration_redundant_unique_index

**Logged**: 2026-06-30T21:58:05+09:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
recipe migration の 44 番目が UNIQUE 制約と同じ列の索引を重複作成して失敗した。

### Error
```text
ORA-01408: such column list already indexed
```

### Context
- `rag_document_recipes` は `UNIQUE(document_id, slot_no)` により索引を既に持つ。
- migration と初期 schema が同じ列へ `rag_document_recipes_document_idx` を追加していた。
- 43/45 まで適用後に失敗したが、DDL は冪等なため修正後に全体を再実行できた。

### Suggested Fix
UNIQUE 制約の索引を再利用し、同列の非一意索引を作らない。

### Metadata
- Reproducible: yes
- Related Files: backend/app/rag/oracle_schema.py, backend/app/clients/oracle.py, artifacts/oracle-schema-migration.sql

### Resolution
- **Resolved**: 2026-06-30T21:58:05+09:00
- **Notes**: 重複索引 DDL を削除し、migration 45/45 と post-migration checks を完了した。

---

## [ERR-20260630-007] oracle_migration_statement_prefix_test

**Logged**: 2026-06-30T17:02:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Oracle migration テストが、新規 migration の通常 `INSERT` 文を許可していなかった。

### Error
```text
assert all(statement.startswith(("-- migration:", "DECLARE", "COMMIT")) ...)
E assert False
```

### Context
- Command attempted: `uv run pytest tests/test_search_answer_profiles_api.py tests/test_oracle_schema_cli.py -q`
- DEFAULT 検索・回答プロファイル補完 migration は `UPDATE`、`INSERT`、`COMMIT` の3文で構成される。
- 先頭の `UPDATE` は migration コメントを含むため許可済みだが、2文目の `INSERT` が旧許可リスト外だった。

### Suggested Fix
Migration 文の安全性チェックで通常の `INSERT` も許可する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_oracle_schema_cli.py

### Resolution
- **Resolved**: 2026-06-30T17:02:00+09:00
- **Notes**: 許可する文頭へ `INSERT` を追加した。

---

## [ERR-20260630-008] uv_cache_read_only

**Logged**: 2026-06-30T17:03:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Sandbox 内の `uv run` が `/root/.cache/uv` に一時ファイルを作成できなかった。

### Error
```text
Could not acquire lock: Read-only file system at /root/.cache/uv/.tmp...
```

### Context
- Command attempted: backend の対象 pytest と Ruff。
- workspace 外にある共有 uv cache が sandbox では読み取り専用だった。

### Suggested Fix
重要な検証は承認済み `uv run` を sandbox 外で再実行する。

### Metadata
- Reproducible: yes
- Related Files: backend/uv.lock

### Resolution
- **Resolved**: 2026-06-30T17:03:00+09:00
- **Notes**: 同じコマンドを承認付きで再実行し、pytest は成功した。

---

## [ERR-20260630-009] overlapping_sed_boundary_misread

**Logged**: 2026-06-30T17:05:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
連続する `sed` 範囲が同じ境界行を表示し、重複 JSX と誤認して patch が失敗した。

### Error
```text
apply_patch verification failed: Failed to find expected duplicate lines
```

### Context
- `sed -n '250,410p'` と `sed -n '410,660p'` が410行目を二度表示した。
- 実ファイルには同じ prop の重複はなかった。

### Suggested Fix
重複を疑う場合は非重複範囲か `rg` / 行番号付き表示で確認する。

### Metadata
- Reproducible: yes
- Related Files: frontend/src/components/search-answer-profiles/SearchAnswerProfileManagementClient.tsx

### Resolution
- **Resolved**: 2026-06-30T17:05:00+09:00
- **Notes**: 実ファイルを再確認し、必要な整形だけ適用した。

---

## [ERR-20260630-010] ruff_import_order_search_answer_profile_test

**Logged**: 2026-06-30T17:08:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Oracle adapter テストへ追加した schema import の順序が Ruff I001 に違反した。

### Error
```text
I001 Import block is un-sorted or un-formatted
```

### Context
- `app.schemas.search_answer_profile` を `app.schemas.document` より後ろへ追加していた。

### Suggested Fix
同一 import group はモジュール名の辞書順に維持する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_oracle_adapter.py

### Resolution
- **Resolved**: 2026-06-30T17:08:00+09:00
- **Notes**: search_answer_profile import を document import より前へ移動した。

---

## [ERR-20260630-011] backend_full_pytest_environment_drift

**Logged**: 2026-06-30T17:12:00+09:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
Backend 全体 pytest は DEFAULT 検索・回答プロファイル対象外の既存環境・処理フロー差分で35件失敗した。

### Error
```text
35 failed, 1431 passed, 17 skipped
```

### Context
- `Settings()` が preprocess service enabled を読み、テスト既定値 `False` と不一致。
- 多数の RAG flow テストは旧単一パスの `INDEXED` / `EXTRACT` を期待する一方、現行処理は `REVIEW` / `PREPROCESS` で停止した。
- 実 Oracle テスト1件は既存 extraction 行の status が期待値と不一致だった。
- 今回変更した search answer profile、Oracle adapter、schema migration の対象テストは全件成功している。

### Suggested Fix
全体テストの環境変数隔離と、現行の段階処理フローに合わせた既存期待値を別作業で整合させる。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_preprocess.py, backend/tests/test_rag_flow.py, backend/tests/test_two_phase_review.py

---

## [ERR-20260630-005] playwright_status_label_strict_locator

**Logged**: 2026-06-30T14:57:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
状態バッジの文言を長くした後、部分一致の Playwright locator が step 名とバッジ名の両方に一致した。

### Error
```text
strict mode violation: getByText('準備確認') resolved to 2 elements
```

### Context
- `準備確認` と `ファイル準備確認待ち`、`抽出確認` と `抽出確認待ち` が同じ画面に共存する。
- 状態名の統一自体は正しく、テスト locator の部分一致が曖昧だった。

### Suggested Fix
短い工程名を検証するときは `getByText(label, { exact: true })` を使い、状態バッジとの部分一致を避ける。

### Metadata
- Reproducible: yes
- Related Files: frontend/e2e/document-workspace-file-processing.spec.ts, frontend/e2e/upload-storage-settings.spec.ts

### Resolution
- **Resolved**: 2026-06-30T14:57:00+09:00
- **Notes**: 該当 locator を exact match に変更し、desktop/mobile で再実行した。

---

## [ERR-20260630-006] playwright_background_job_transition_race

**Logged**: 2026-06-30T14:57:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
バックグラウンド失敗の mock が POST 直後から FAILED を返し、QUEUED の開始 message を検証する前に消える競合が起きた。

### Error
```text
Expected ファイル準備を開始しました... to be visible; element not found
```

### Context
- 検証対象は `QUEUED → FAILED` で旧 FormStatus が消える遷移。
- mock が中間状態を持たず、端末速度によって最初から FAILED に見えた。

### Suggested Fix
非同期状態遷移の E2E mock は poll 回数など明示的な状態機械を持ち、少なくとも1回 QUEUED を返してから FAILED へ進める。

### Metadata
- Reproducible: yes
- Related Files: frontend/e2e/document-workspace-file-processing.spec.ts

### Resolution
- **Resolved**: 2026-06-30T14:57:00+09:00
- **Notes**: mock に poll counter と backgroundFailed を追加し、desktop/mobile で安定して通過した。

---

## [ERR-20260630-003] nightly_missing_shared_backend_core

**Logged**: 2026-06-30T07:42:39+09:00
**Priority**: high
**Status**: resolved
**Area**: tests

### Summary
Scheduled RAG evaluation failed before tests because the workflow did not checkout the sibling platform repository required by the backend path dependency.

### Error
```text
Failed to generate package metadata for production-ready-backend-core
Distribution not found at: ../../no.1-production-ready-platform/packages/backend_core
```

### Context
- GitHub Actions run: 28399801349, job 84147832679.
- `.github/workflows/ci.yml` already uses sibling app/platform checkouts, but `rag-evaluation-nightly.yml` checked out only the app at workspace root.

### Suggested Fix
Reuse the CI workflow layout: checkout app and platform as workspace siblings, then update working, cache, and artifact paths.

### Metadata
- Reproducible: yes
- Related Files: .github/workflows/rag-evaluation-nightly.yml

### Resolution
- **Resolved**: 2026-06-30T07:47:00+09:00
- **Commit**: c1c0aaa
- **Notes**: Reused the main CI sibling checkout layout; YAML parsing and `uv sync --locked --dev` succeeded locally with the same directory structure.

---

## [ERR-20260630-004] workflow_dispatch_token_scope

**Logged**: 2026-06-30T07:47:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The available GitHub PAT could push code but could not dispatch a workflow run.

### Error
```text
HTTP 403: Resource not accessible by personal access token
```

### Context
- Command attempted: `gh workflow run rag-evaluation-nightly.yml --ref codex/default-knowledge-base`.
- The workflow fix was already pushed; equivalent YAML and dependency installation checks passed locally.

### Suggested Fix
Use a token with Actions workflow dispatch permission, or trigger the branch workflow from the GitHub UI.

### Metadata
- Reproducible: yes
- Related Files: .github/workflows/rag-evaluation-nightly.yml

### Resolution
- **Resolved**: 2026-06-30T07:47:00+09:00
- **Notes**: Report the dispatch limitation explicitly; no code workaround is appropriate.

---

## [ERR-20260630-001] eslint_playwright_test_results_race

**Logged**: 2026-06-30T06:36:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
ESLint と Playwright の並列実行中、Playwright が `test-results` を再作成して ESLint の走査が ENOENT になった。

### Error
```text
Error: ENOENT: no such file or directory, scandir 'frontend/test-results'
```

### Context
- Commands attempted in parallel: `npm run lint -- --quiet` and targeted `npm run test:e2e`.
- Playwright owns and recreates `frontend/test-results` at startup.

### Suggested Fix
Frontend lint and Playwright should run sequentially when ESLint scans the project root.

### Metadata
- Reproducible: yes
- Related Files: frontend/playwright.config.ts, frontend/eslint.config.js
- Recurrence-Count: 3
- Last-Seen: 2026-06-30

### Resolution
- **Resolved**: 2026-06-30T06:36:00+09:00
- **Notes**: Playwright 完了後に lint を単独で再実行する。

---

## [ERR-20260623-001] uv_cache_sandbox_readonly_workspace_cache

**Logged**: 2026-06-23T08:15:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
`uv run pytest` again failed in the managed filesystem sandbox because `/root/.cache/uv` is read-only; using the repo-local `.uv-cache` avoided escalation.

### Error
```text
error: Could not acquire lock
  Caused by: Could not create temporary file
  Caused by: Read-only file system (os error 30) at path "/root/.cache/uv/.tmp..."
```

### Context
- Command attempted: `uv run pytest tests/test_service_management.py tests/test_ingestion_strategy.py -q`
- Follow-up command succeeded for targeted tests with `uv --cache-dir ../.uv-cache run ...`.

### Suggested Fix
Prefer `uv --cache-dir ../.uv-cache run ...` for backend test/lint/type commands in this workspace when running under the managed sandbox.

### Metadata
- Reproducible: yes
- Related Files: backend/pyproject.toml
- See Also: ERR-20260615-001
- Recurrence-Count: 2
- Last-Seen: 2026-06-30

---

## [ERR-20260623-001] oracle_test_schema_lock

**Logged**: 2026-06-23T08:04:21+09:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
Targeted backend pytest failed during the session autouse Oracle schema setup because the real Oracle test database held a DML/table lock.

### Error
```text
oracledb.exceptions.DatabaseError: ORA-00054: Failed to acquire a lock (Type: "TM", Name: "DML", Description: "Synchronizes accesses to an object") because it is currently held by another session.
```

### Context
- Command attempted: `uv run pytest tests/test_document_ingestion_config.py tests/test_document_workspace.py -q`
- The command reached pytest only after sandbox escalation for uv cache writes.
- All selected tests errored before running assertions in `tests/conftest.py::_oracle_db_session` while `tests/_oracle_test_db.py::ensure_schema()` executed DDL/schema setup.

### Suggested Fix
Retry after the competing Oracle session releases the lock, or run the targeted tests with the real Oracle test DB disabled when the test path uses fakes and does not need schema setup.

### Metadata
- Reproducible: unknown
- Related Files: backend/tests/conftest.py, backend/tests/_oracle_test_db.py

---

## [ERR-20260614-001] uv_cache_readonly

**Logged**: 2026-06-14T02:31:00+09:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
`uv run` failed because the sandbox could not write to the default `/root/.cache/uv` cache directory.

### Error
```text
error: Could not acquire lock
  Caused by: Could not create temporary file
  Caused by: Read-only file system (os error 30) at path "/root/.cache/uv/.tmp..."
```

### Context
- Command attempted: `uv run ruff check .`, `uv run mypy .`, `uv run pytest`
- Environment: managed workspace sandbox with writable project root and `/tmp`

### Suggested Fix
Use a writable cache directory for verification commands, for example `uv --cache-dir /tmp/uv-cache run pytest`.

### Metadata
- Reproducible: yes
- Related Files: backend/pyproject.toml
- Recurrence-Count: 8
- Last-Seen: 2026-07-03T14:59:00+09:00

### Recurrence Notes
- 2026-06-16T20:36:23+09:00: `uv run ruff check ...` and `uv run pytest ...` failed in the managed sandbox for the same `/root/.cache/uv` write issue. Reran successfully with `UV_CACHE_DIR=/tmp/uv-cache`.
- 2026-06-18T04:26:58+09:00: `uv lock --offline` failed for the same `/root/.cache/uv` write issue. Use a writable cache path such as `UV_CACHE_DIR=/tmp/uv-cache`.
- 2026-06-22T17:00:00+09:00: `uv run pytest tests/test_oci_enterprise_ai.py tests/test_settings_api.py -q` failed in the sandbox for the same `/root/.cache/uv` write issue. Reran successfully with approved escalation.
- 2026-06-29T00:00:00+09:00: parallel `uv run ruff` / `uv run mypy` hit the same default-cache lock failure. Run checks sequentially with `uv --cache-dir /tmp/uv-cache`.
- 2026-06-30T06:32:36+09:00: targeted `uv run ruff` hit the same default-cache lock failure while another `uv` check ran. Rerun with `uv --cache-dir /tmp/uv-cache`.
- 2026-06-30T11:11:55+09:00: targeted backend ruff hit the same default-cache write failure. Rerun with `uv --cache-dir /tmp/uv-cache`.
- 2026-07-03T14:59:00+09:00: `uv sync --locked --dev` hit the same default-cache failure and passed with `UV_CACHE_DIR=/tmp/uv-cache`.

---

## [ERR-20260618-001] pytest_node_id_mismatch

**Logged**: 2026-06-18T15:55:15+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Targeted pytest failed because a guessed node id did not exist in `test_file_processing_staging.py`.

### Error
```text
ERROR: not found: /u01/workspace/no.1-production-ready-rag/backend/tests/test_file_processing_staging.py::test_preflight_payload_strict_redacts_contract_blocking_failures
(no match in any of [<Module test_file_processing_staging.py>])
```

### Context
- Command attempted: `uv run pytest tests/test_file_processing_staging.py::test_file_processing_staging_trend_keeps_adapter_package_version_evidence tests/test_file_processing_staging.py::test_preflight_payload_strict_runs_manifest_adapter_contract tests/test_file_processing_staging.py::test_preflight_payload_strict_redacts_contract_blocking_failures -q`
- The intended coverage was strict parser adapter contract staging payload redaction.

### Suggested Fix
Use `rg` to confirm pytest node ids before running a narrow targeted subset.

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_file_processing_staging.py

### Resolution
- **Resolved**: 2026-06-18T15:55:15+09:00
- **Notes**: Located the actual strict parser adapter staging tests with `rg` before rerunning the corrected subset.

---

## [ERR-20260618-001] agent_browser_socket_dir_readonly

**Logged**: 2026-06-18T08:34:43+09:00
**Priority**: low
**Status**: pending
**Area**: config

### Summary
`agent-browser --auto-connect get url` failed in the managed sandbox because it could not create its socket directory on a read-only filesystem.

### Error
```text
✗ Failed to create socket directory: Read-only file system (os error 30)
```

### Context
- Command attempted: `agent-browser --auto-connect get url`
- The failure occurred before browser connection, likely while creating agent-browser runtime/session files outside the writable workspace roots.

### Suggested Fix
Rerun browser automation commands with approved sandbox escalation when the helper needs to create socket/session files outside writable project paths.

### Metadata
- Reproducible: yes
- Related Files: /root/.agents/skills/agent-browser/SKILL.md

---

## [ERR-20260618-001] vite_listen_eperm

**Logged**: 2026-06-18T04:31:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
Playwright could not start the Vite web server inside the managed sandbox because listening on localhost returned `EPERM`.

### Error
```text
Error: listen EPERM: operation not permitted 127.0.0.1:3007
Error: Process from config.webServer was not able to start. Exit code: 1
```

### Context
- Command attempted: `npm run test:e2e -- e2e/parser-adapter-settings.spec.ts`
- Manual Vite startup with `npm run dev -- --host 127.0.0.1 --port 3007` reproduced the `listen EPERM`.
- 2026-06-30 に `document-review-gate.spec.ts` の再実行でも webServer 起動失敗が再発した。

### Suggested Fix
Run Playwright UI verification with approved sandbox escalation when Vite cannot bind a localhost test port.

### Metadata
- Reproducible: yes
- Related Files: frontend/playwright.config.ts
- Recurrence-Count: 2
- Last-Seen: 2026-06-30

### Resolution
- **Resolved**: 2026-06-18T04:31:00+09:00
- **Notes**: Reran the same Playwright spec with sandbox escalation; 8 tests passed.

---

## [ERR-20260617-002] document_workspace_element_deeplink_reset

**Logged**: 2026-06-17T16:07:02+09:00
**Priority**: medium
**Status**: resolved
**Area**: frontend

### Summary
`element_id` only deep links in DocumentWorkspace were reset to the first chunk when chunk data arrived after the extraction selection.

### Error
```text
Expected selected table element aria-pressed="true"; received "false" in Playwright desktop/mobile.
```

### Context
- Command attempted: `npm run test:e2e -- e2e/structure-explainability.spec.ts e2e/document-workspace-file-processing.spec.ts`
- The element deep-link effect selected `tbl-1` before chunks loaded, then the default first-chunk selection ran later and overwrote the selected element.

### Suggested Fix
When a requested `element_id` is already selected, preserve it as the authoritative deep-link target; later chunk loading may attach the linked chunk but must not reset the element selection.

### Metadata
- Reproducible: yes
- Related Files: frontend/src/components/documents/DocumentWorkspace.tsx, frontend/e2e/document-workspace-file-processing.spec.ts

### Resolution
- **Resolved**: 2026-06-17T16:07:02+09:00
- **Notes**: Added a guard that preserves selected URL element targets and only backfills linked chunk id; reran the focused Playwright specs successfully on desktop and mobile.

---

## [ERR-20260617-001] file_processing_golden_cli_argument

**Logged**: 2026-06-17T00:00:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
`rag-file-processing-golden` takes the manifest path as a positional argument, not `--manifest`.

### Error
```text
rag-file-processing-golden: error: unrecognized arguments: --manifest
```

### Context
- Command attempted: `uv run python -m app.rag.file_processing_golden_cli --manifest ../docs/evaluation/file-processing-golden-set.json`
- The CLI usage is `rag-file-processing-golden [--output OUTPUT] [--fail-on-pending] [--github-annotations] manifest`.

### Suggested Fix
Run `uv run python -m app.rag.file_processing_golden_cli ../docs/evaluation/file-processing-golden-set.json`.

### Metadata
- Reproducible: yes
- Related Files: backend/app/rag/file_processing_golden_cli.py, docs/evaluation/file-processing-golden-set.json

### Resolution
- **Resolved**: 2026-06-17T00:00:00+09:00
- **Notes**: Reran with the manifest path as the positional argument.

---

## [ERR-20260617-002] playwright_chromium_sandbox_eperm

**Logged**: 2026-06-17T05:01:55+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
Playwright Chromium failed to launch inside the managed sandbox while verifying the document workspace UI.

### Error
```text
FATAL:content/browser/sandbox_host_linux.cc:41 Check failed: . shutdown: Operation not permitted (1)
```

### Context
- Command attempted: `env PLAYWRIGHT_SKIP_WEB_SERVER=1 npx playwright test e2e/document-workspace-file-processing.spec.ts`
- Vite dev server was already running with approved escalation.
- Chromium launch needs sandbox escalation in this desktop environment.

### Suggested Fix
Run targeted Playwright verification with approved sandbox escalation when Chromium launch hits `sandbox_host_linux.cc` EPERM.

### Metadata
- Reproducible: yes
- Related Files: frontend/playwright.config.ts

### Resolution
- **Resolved**: 2026-06-17T05:01:55+09:00
- **Notes**: Reran the same targeted Playwright command with escalation; 4 tests passed across desktop and mobile.

---

## [ERR-20260617-001] vitest_runinband_option

**Logged**: 2026-06-17T04:59:52+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
Vitest failed because the command used Jest's `--runInBand` option, which this frontend test runner does not support.

### Error
```text
CACError: Unknown option `--runInBand`
```

### Context
- Command attempted: `npm run test -- --runInBand`
- Project script is `vitest run`; run it directly without Jest-only flags.

### Suggested Fix
Use `npm run test` for the frontend Vitest suite, or Vitest-supported flags only.

### Metadata
- Reproducible: yes
- Related Files: frontend/package.json

### Resolution
- **Resolved**: 2026-06-17T04:59:52+09:00
- **Notes**: Rerun the frontend test suite with `npm run test`.

---

## [ERR-20260616-002] document_index_e2e_route

**Logged**: 2026-06-16T15:47:29+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
The document-delete Playwright test initially opened `/documents`, but the document index route is `/file-list`; `/documents/:id` is reserved for document detail.

### Error
```text
Locator: getByRole('heading', { name: '文書インデックス' })
Expected: visible
Error: element(s) not found
```

### Context
- Command attempted: `npx playwright test e2e/document-delete.spec.ts --project=desktop --project=mobile`
- The app redirected the unmatched `/documents` route to the dashboard, so the document index heading never appeared.

### Suggested Fix
Use `APP_ROUTES.fileList` semantics in e2e tests and open `/file-list` for the document index.

### Metadata
- Reproducible: yes
- Related Files: frontend/src/lib/routes.ts, frontend/e2e/document-delete.spec.ts

### Resolution
- **Resolved**: 2026-06-16T15:47:29+09:00
- **Notes**: The e2e now opens `/file-list`; desktop and 375px mobile projects pass.

---

## [ERR-20260616-003] backend_full_pytest_timeout

**Logged**: 2026-06-16T20:47:00+09:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
Backend full `pytest -q` did not complete within a 180 second timeout in the managed sandbox, while targeted pipeline/search tests completed.

### Error
```text
timeout 180s env UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q
# exited with code 124 after reaching 13% progress output
```

### Context
- Initial full pytest run reached 13% and then produced no further output for several minutes.
- A second run with explicit `timeout 180s` reproduced the stall/long-running behavior.
- Targeted verification passed: `tests/test_pipeline.py tests/test_search_api.py -q`.
- `uv run ruff check .` and `uv run mypy .` passed with `UV_CACHE_DIR=/tmp/uv-cache`.
- 2026-06-18T04:43:00+09:00: A focused `test_oci_enterprise_ai.py` subset printed a passing dot, then did not exit before a 30-60s `timeout`; the pure `test_oci_http_status_error_includes_response_body` target passed.

### Suggested Fix
Use a targeted test subset for code-change verification when full pytest stalls, then investigate full-suite timing with verbose/failfast selection in a dedicated debugging pass.

### Metadata
- Reproducible: yes
- Related Files: backend/tests
- See Also: ERR-20260614-003, ERR-20260614-018
- Recurrence-Count: 2
- Last-Seen: 2026-06-18T04:43:00+09:00

---

## [ERR-20260616-001] vitest_runinband_option

**Logged**: 2026-06-16T15:44:22+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
`npm test -- --runInBand` failed because Vitest does not support Jest's `--runInBand` option.

### Error
```text
CACError: Unknown option `--runInBand`
```

### Context
- Command attempted: `npm test -- --runInBand`
- Project test script is `vitest run`; the extra flag came from Jest muscle memory rather than project convention.

### Suggested Fix
Use `npm test` for the project default, or pass Vitest-supported flags only.

### Metadata
- Reproducible: yes
- Related Files: frontend/package.json

### Resolution
- **Resolved**: 2026-06-16T15:44:22+09:00
- **Notes**: Reran with `npm test` instead.

---

## [ERR-20260615-004] oci_vlm_incomplete_max_output_tokens

**Logged**: 2026-06-15T21:50:00+09:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
OCI OpenAI-compatible Responses can return HTTP 200 with `status=incomplete` and `incomplete_details.reason=max_output_tokens` for PDF VLM extraction, causing ingestion to fail after a successful response.

### Error
```text
ValueError: OCI Enterprise AI response status=incomplete: max_output_tokens
```

### Context
- The Files API upload and delete succeeded.
- `/openai/v1/responses` returned HTTP 200, but the model output JSON was truncated by output-token limits.
- The application previously treated this as an unhandled `ValueError`, returning HTTP 500.

### Suggested Fix
Set a larger VLM-specific `max_output_tokens` in the Responses payload and map `status=incomplete` / `reason=max_output_tokens` to a user-visible ingestion error instead of a 500.

### Metadata
- Reproducible: yes
- Related Files: backend/app/clients/oci_enterprise_ai.py, backend/app/rag/ingestion.py

### Resolution
- **Resolved**: 2026-06-15T21:50:00+09:00
- **Notes**: Added `OCI_ENTERPRISE_AI_VLM_MAX_OUTPUT_TOKENS` default 32768, kept LLM default 1200, and added regression tests for client parsing and ingestion API response.

---

## [ERR-20260615-003] oci_vlm_polygon_bbox_validation_error

**Logged**: 2026-06-15T20:50:00+09:00
**Priority**: medium
**Status**: resolved
**Area**: backend

### Summary
OCI OpenAI-compatible VLM extraction can return polygon-style bbox values with 8 coordinates, while the local `StructuredExtraction` schema originally accepted only 4-value rectangles.

### Error
```text
pydantic_core._pydantic_core.ValidationError: elements.N.bbox
Value error, bbox は有限数 4 個で指定してください。
input_value=[755, 17, 765, 66, 755, 964, 765, 970]
```

### Context
- Endpoint returned `POST /openai/v1/responses` with HTTP 200.
- Ingestion failed while validating VLM output, after successful file upload and cleanup.

### Suggested Fix
Normalize bbox metadata from polygon/list/dict forms to `[min_x, min_y, max_x, max_y]` before Pydantic validation rejects the extraction payload.

### Metadata
- Reproducible: yes
- Related Files: backend/app/schemas/extraction.py, backend/tests/test_oci_enterprise_ai.py

### Resolution
- **Resolved**: 2026-06-15T20:50:00+09:00
- **Notes**: Added bbox normalization and a regression test covering OpenAI Responses `output_text` JSON with 8-coordinate bbox values.

---

## [ERR-20260615-002] oracle_schema_lock_during_parallel_pytest

**Logged**: 2026-06-15T20:45:00+09:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
Running backend pytest processes concurrently can make the autouse Oracle schema setup fail with ORA-00054 because another session holds a DML/table lock.

### Error
```text
oracledb.exceptions.DatabaseError: ORA-00054: Failed to acquire a lock
(Type: "TM", Name: "DML", Description: "Synchronizes accesses to an object")
```

### Context
- Commands attempted in parallel: targeted `uv run pytest` for unit tests and `tests/test_rag_flow.py`.
- The `test_rag_flow.py` process was using the shared Oracle test schema while another pytest process started schema initialization.

### Suggested Fix
Avoid running Oracle-backed pytest processes in parallel against the same schema, or isolate schemas per worker before enabling parallel backend test runs.

### Metadata
- Reproducible: yes
- Related Files: backend/tests/conftest.py, backend/tests/_oracle_test_db.py

---

## [ERR-20260614-019] uv_cache_readonly_root

**Logged**: 2026-06-14T13:11:46+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
`uv run` failed in the managed sandbox because the default cache under `/root/.cache/uv` is read-only.

### Error
```text
error: Could not acquire lock
  Caused by: Could not create temporary file
  Caused by: Read-only file system (os error 30) at path "/root/.cache/uv/.tmp..."
```

### Context
- Commands attempted: `uv run ruff check ...` and `uv run pytest ...`
- Re-running with `UV_CACHE_DIR=/tmp/uv-cache` allowed ruff and pytest to complete.

### Suggested Fix
Use `UV_CACHE_DIR=/tmp/uv-cache uv run ...` for backend validation commands in this sandbox.

### Metadata
- Reproducible: yes
- Recurrence-Count: 2
- Last-Seen: 2026-06-28
- Related Files: backend/pyproject.toml

### Resolution
- **Resolved**: 2026-06-14T13:11:46+09:00
- **Notes**: Re-ran backend ruff and targeted pytest with `UV_CACHE_DIR=/tmp/uv-cache`; both passed.

---

## [ERR-20260614-014] stale_settings_route_probe

**Logged**: 2026-06-14T11:22:04+09:00
**Priority**: low
**Status**: pending
**Area**: frontend

### Summary
An exploratory read used the stale `frontend/src/app/settings/page.tsx` path even though the current settings UI lives under component files.

### Error
```text
sed: can't read frontend/src/app/settings/page.tsx: No such file or directory
```

### Context
- Command attempted: `sed -n '1,220p' frontend/src/app/settings/page.tsx`
- The actual Oracle settings implementation being debugged is `frontend/src/components/settings/OciSettingsClient.tsx`.

### Suggested Fix
Use `rg --files frontend/src | rg 'settings|OciSettings'` before assuming App Router paths in this Vite-based frontend.

### Metadata
- Reproducible: yes
- Related Files: frontend/src/components/settings/OciSettingsClient.tsx

---

## [ERR-20260614-013] full_validation_existing_regressions

**Logged**: 2026-06-14T10:30:00+09:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
Backend full validation surfaced two current-worktree regressions: OCI URI reads incorrectly routed to local storage, and a chunking test metadata value lacked type narrowing for mypy.

### Error
```text
ValueError: ローカルモードでは local:// URI のみ取得できます。
tests/test_chunking.py:61: error: Unsupported operand types for <= ("int" and "None")
```

### Context
- Commands attempted: `uv run pytest` and `uv run mypy .`
- Failing pytest case: `tests/test_object_storage.py::test_get_oci_uri_uses_oci_even_when_upload_storage_is_local`
- Failing mypy location: `tests/test_chunking.py:61`

### Suggested Fix
Route explicit `oci://` URIs through OCI Object Storage even when upload defaults are local, and narrow metadata value types in tests before numeric comparisons.

### Metadata
- Reproducible: yes
- Related Files: backend/app/clients/object_storage.py, backend/tests/test_chunking.py

---

## [ERR-20260614-012] evaluation_no_results_failure_reason

**Logged**: 2026-06-14T09:05:33+09:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
Evaluation failure-reason aggregation initially counted an expected no-results case as `guardrail_warning` because the RAG pipeline returns a no-results warning even when the golden set expects no relevant documents.

### Error
```text
AssertionError: assert {'guardrail_warning': 1} == {}
```

### Context
- Command attempted: `uv --cache-dir /tmp/uv-cache run pytest tests/test_evaluation.py tests/test_evaluation_cli.py tests/test_evaluation_fixture.py`
- Case had `relevant_document_ids=[]` and retrieved no citations, so precision/recall/keyword/groundedness all passed.
- The warning represented the expected no-results path, not a failure.

### Suggested Fix
When assigning evaluation `failure_reasons`, suppress `guardrail_warning` for expected no-results cases where both relevant documents and retrieved documents are empty.

### Metadata
- Reproducible: yes
- Related Files: backend/app/rag/evaluation.py, backend/tests/test_evaluation.py

---

## [ERR-20260614-011] staging_smoke_default_drift

**Logged**: 2026-06-14T08:36:11+09:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
`staging_smoke` の CLI default query を marker template に変えたが、直接呼び出し用 `run_staging_smoke()` の default が旧値のままで単体テストが失敗した。

### Error
```text
AssertionError: assert 'SMOKE-...' in 'staging smoke 文書の確認用キーワードは？'
```

### Context
- Command attempted: `uv --cache-dir /tmp/uv-cache run pytest tests/test_oci_enterprise_ai.py tests/test_staging_smoke.py`
- CLI parser と callable function の default が二重管理になっていた。

### Suggested Fix
CLI default と function default は同じ module constant を参照させる。

### Metadata
- Reproducible: yes
- Related Files: backend/app/rag/staging_smoke.py, backend/tests/test_staging_smoke.py

---

## [ERR-20260614-011] npm_run_dev_argument_forwarding

**Logged**: 2026-06-14T08:33:30+09:00
**Priority**: low
**Status**: pending
**Area**: frontend

### Summary
`npm run dev -p 5173` forwarded arguments incorrectly in this environment and Next.js treated `5173` as a project directory.

### Error
```text
Invalid project directory provided, no such directory: /u01/workspace/no.1-production-ready-rag/frontend/5173
```

### Context
- Command attempted: `npm run dev -p 5173`
- Correct command: `npm run dev -- -p 5173`
- This matters when restarting the local Next.js dev server for browser verification.

### Suggested Fix
Use `--` when passing Next.js CLI flags through npm scripts, for example `npm run dev -- -p 5173`.

### Metadata
- Reproducible: yes
- Related Files: frontend/package.json

---

## [ERR-20260614-011] next_dev_listen_and_build_manifest_race

**Logged**: 2026-06-14T08:28:19+09:00
**Priority**: low
**Status**: pending
**Area**: frontend

### Summary
Next.js dev server startup can require sandbox escalation for local port listening, and `next build` may transiently fail during static generation before succeeding on rerun/debug.

### Error
```text
Error: listen EPERM: operation not permitted 0.0.0.0:3000
Error: listen EADDRINUSE: address already in use 127.0.0.1:3000
Error: ENOENT: no such file or directory, open '.next/server/pages-manifest.json'
```

### Context
- Commands attempted: `npm run dev -- -p 3000`, `npm run dev -- -p 3010 -H 127.0.0.1`, `npm run build`
- Sandbox blocked the first local listen attempt; escalated dev server startup was required.
- Ports 3000 and 3001 were already occupied, while 3010 started successfully.
- `./node_modules/.bin/next build --debug` and `NODE_OPTIONS=--trace-uncaught ./node_modules/.bin/next build` both succeeded; a later normal `npm run build` also succeeded.

### Suggested Fix
When validating locally in this sandbox, request escalation for `npm run dev`, try an alternate port such as 3010 if 3000/3001 are occupied, and rerun `next build --debug` if the static-generation worker exits without a useful stack.

### Metadata
- Reproducible: unknown
- Related Files: frontend/src/components/layout/Sidebar.tsx, frontend/next.config.ts
- See Also: ERR-20260614-010

---

## [ERR-20260614-008] groundedness_test_assertions

**Logged**: 2026-06-14T08:05:00+09:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
`pytest` failed after adding groundedness evaluation because tests treated a ratio score as always `1.0`, and one fixture used citation text that was too short to provide grounding features.

### Error
```text
AssertionError: assert 0.3333 == 1.0
AssertionError: assert 0.0 == 1.0
```

### Context
- Command attempted: `uv --cache-dir /tmp/uv-cache run pytest tests/test_evaluation.py tests/test_guardrails.py tests/test_evaluation_fixture.py`
- `groundedness_score` is an overlap ratio unless a high-signal numeric/ID feature overlaps.
- A citation text like `"A"` is intentionally ignored by the tokenizer because single-character features are too noisy.

### Suggested Fix
Assert pass/fail separately from exact score unless the case intentionally includes a high-signal numeric/ID overlap. Use realistic citation text in evaluation fixtures.

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_evaluation.py, backend/app/rag/guardrails.py

---

## [ERR-20260613-001] sandbox_cache_and_generated_typeinfo

**Logged**: 2026-06-13T22:38:59Z
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
Backend `uv` verification failed in the sandbox when it tried to write `/root/.cache/uv`, and a generated TypeScript build-info restore attempt via Node hit `spawnSync git EPERM`.

### Error
```text
error: Could not acquire lock
  Caused by: Could not create temporary file
  Caused by: Read-only file system (os error 30) at path "/root/.cache/uv/..."

Error: spawnSync git EPERM
```

### Context
- Commands attempted: `uv run pytest ...`, `uv run ruff ...`, and a Node helper that called `git show HEAD:frontend/tsconfig.tsbuildinfo`.
- `uv` succeeded after rerunning with escalated permissions.
- `npm run build` regenerated stale `.next/types`, after which `npm run typecheck` passed.

### Suggested Fix
Prefer an approved `uv run ...` prefix or an explicit writable `UV_CACHE_DIR` for sandboxed verification. Avoid restoring generated build-info through nested `git` calls from Node in this environment.

### Metadata
- Reproducible: unknown
- Related Files: frontend/tsconfig.tsbuildinfo

---

## [ERR-20260614-004] atomic_temp_filename_too_long

**Logged**: 2026-06-14T03:10:43+09:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
Atomic local object writes failed for long uploaded file names because the temporary file name appended a UUID to the already-long final file name.

### Error
```text
OSError: [Errno 36] File name too long: '.../.<long-original-name>.<uuid>.tmp'
```

### Context
- `ObjectStorageClient._atomic_write()` created a temp file with the full target name plus UUID.
- POSIX file name component limits can reject the temporary file even when the final target file name is valid.

### Suggested Fix
Use a short same-directory temporary component such as `.tmp-<uuid>` and then atomically replace the target path.

### Metadata
- Reproducible: yes
- Related Files: backend/app/clients/object_storage.py, backend/tests/test_rag_flow.py
- See Also: ERR-20260614-003

---

## [ERR-20260614-003] testclient_dependency_drift

**Logged**: 2026-06-14T02:55:00+09:00
**Priority**: high
**Status**: pending
**Area**: tests

### Summary
FastAPI/Starlette `TestClient` hung after dependency resolution drifted to newer Starlette/AnyIO/httpx combinations.

### Error
```text
TestClient(app).get("/api/health")
# hung until killed by timeout
```

### Context
- Observed combinations included `fastapi=0.136.3`, `starlette=1.3.1`, `httpx=0.28.1`, `anyio=4.13.0`.
- Pinning Starlette/FastAPI/AnyIO alone was not sufficient in this environment.
- `httpx.AsyncClient` with `httpx.ASGITransport` returned immediately and is enough for this backend's API tests.

### Suggested Fix
Use the local `tests.support.AsgiTestClient` helper for synchronous API tests and keep dependency upper bounds in `pyproject.toml` / `uv.lock` to avoid unreviewed framework drift.

### Metadata
- Reproducible: yes
- Related Files: backend/pyproject.toml, backend/uv.lock, backend/tests/support.py
- See Also: ERR-20260614-002

---

## [ERR-20260614-002] pytest_unhandled_exception_middleware_hang

**Logged**: 2026-06-14T02:34:00+09:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
Unhandled-error regression tests hung when exercising generic 500 handling through Starlette `TestClient`.

### Error
```text
pytest backend/tests/test_rag_flow.py::test_unhandled_error_uses_api_response_shape -vv
# collected 1 item, then hung at test execution
```

### Context
- First attempt converted exceptions from `call_next` into a JSON response inside HTTP middleware.
- Second attempt moved request metrics to a pure ASGI middleware and used `TestClient(app, raise_server_exceptions=False)`.
- Both approaches caused request tests to hang in this dependency set; even a simple `/ok` route hung with the experimental ASGI middleware.
- 2026-06-30 に backend 全体の `pytest -q` が 14% で停止し、`tests/test_db_degradation.py -vv` へ絞ると最初の `_RaisingOracle` ケースで同じ hang を再確認した。今回の REVIEW 保存変更とは独立した既知のテスト基盤問題。

### Suggested Fix
Keep the previously verified request-id/metrics middleware for now. Treat generic 500 ApiResponse handling as a separate spike with a minimal Starlette reproduction before reintroducing it.

### Metadata
- Reproducible: yes
- Occurrence-Count: 2
- Related Files: backend/app/main.py, backend/tests/test_rag_flow.py
- See Also: ERR-20260614-001

---

## [ERR-20260614-005] non_ascii_bytes_literal

**Logged**: 2026-06-14T03:15:50+09:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
`pytest` collection failed because a test used Japanese text directly inside a Python bytes literal.

### Error
```text
SyntaxError: bytes can only contain ASCII literal characters
```

### Context
- Command attempted: `uv --cache-dir /tmp/uv-cache run pytest tests/test_dashboard.py tests/test_categories.py tests/test_health.py`
- The new dashboard test used `b"請求書..."`, which Python rejects before test execution.

### Suggested Fix
Use a Unicode string and call `.encode()` when test input bytes need Japanese text.

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_dashboard.py

---

## [ERR-20260614-006] logging_extra_mypy

**Logged**: 2026-06-14T03:19:21+09:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
`mypy` failed on tests that accessed a logging `extra` field as a direct `LogRecord` attribute, and on a string variable passed to a Literal-typed audit helper.

### Error
```text
tests/test_audit.py:62: error: "LogRecord" has no attribute "audit_event"  [attr-defined]
app/rag/pipeline.py:78: error: Argument "outcome" to "record_rag_search_audit" has incompatible type "str"; expected "Literal['success', 'blocked']"  [arg-type]
```

### Context
- Command attempted: `uv --cache-dir /tmp/uv-cache run mypy .`
- Python logging supports dynamic `extra` fields at runtime, but static typing does not know those attributes.
- The ternary expression for `outcome` was inferred as `str`, not the narrower Literal union.

### Suggested Fix
Use `getattr(record, "audit_event")` in tests and annotate the variable as the audit Literal alias at the call site.

### Metadata
- Reproducible: yes
- Related Files: backend/app/rag/pipeline.py, backend/tests/test_audit.py, backend/tests/test_rag_flow.py

---

## [ERR-20260614-007] logging_extra_ruff_getattr

**Logged**: 2026-06-14T03:20:28+09:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
`ruff` rejected constant-name `getattr` used to satisfy mypy for a dynamic logging `extra` field.

### Error
```text
B009 Do not call `getattr` with a constant attribute value.
```

### Context
- Command attempted: `uv --cache-dir /tmp/uv-cache run ruff check .`
- Direct `record.audit_event` satisfies ruff but fails mypy because `LogRecord` does not declare dynamic `extra` attributes.
- `getattr(record, "audit_event")` satisfies mypy but fails ruff B009.

### Suggested Fix
Use `cast(Any, record).audit_event` for test assertions that need dynamic logging extra fields.

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_audit.py, backend/tests/test_rag_flow.py
- See Also: ERR-20260614-006

---

## [ERR-20260614-009] uv_cache_read_only_root

**Logged**: 2026-06-14T08:22:08+09:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
`uv run pytest` failed because the default uv cache under `/root/.cache/uv` was read-only in the sandbox.

### Error
```text
error: Could not acquire lock
  Caused by: Could not create temporary file
  Caused by: Read-only file system (os error 30) at path "/root/.cache/uv/.tmp..."
```

### Context
- Command attempted: `uv run pytest tests/test_settings_api.py`
- The workspace allows writes under `/u01/workspace/no.1-production-ready-rag` and `/tmp`, but not `/root/.cache`.

### Suggested Fix
Run uv with a workspace-writable cache directory, for example `uv --cache-dir /tmp/uv-cache run pytest ...`.

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_settings_api.py

---

## [ERR-20260614-010] next_build_worker_static_generation

**Logged**: 2026-06-14T08:24:42+09:00
**Priority**: low
**Status**: pending
**Area**: frontend

### Summary
`npm run build` failed during Next.js static generation when the default build worker was enabled, but succeeded with `NEXT_PRIVATE_BUILD_WORKER=0`.

### Error
```text
Build error occurred
ENOENT: no such file or directory, rename '.next/export/500.html' -> '.next/server/pages/500.html'

Next.js build worker exited with code: 1 and signal: null
```

### Context
- Command attempted: `npm run build`
- Follow-up command succeeded: `NEXT_PRIVATE_BUILD_WORKER=0 npm run build`
- The compile, lint, typecheck, and route generation steps completed before the worker failure.

### Suggested Fix
For this sandbox, use `NEXT_PRIVATE_BUILD_WORKER=0 npm run build` when validating Next.js builds. If it recurs in CI, investigate Next.js worker filesystem behavior around `.next/export`.

### Metadata
- Reproducible: yes
- Related Files: frontend/next.config.ts

---

## [ERR-20260614-011] sandbox_socket_bind_permission

**Logged**: 2026-06-14T08:36:23+09:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Starting a local uvicorn dev server inside the managed sandbox failed because socket creation was not permitted.

### Error
```text
PermissionError: [Errno 1] Operation not permitted
```

### Context
- Command attempted: `uv --cache-dir /tmp/uv-cache run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
- The uv cache issue was avoided with `/tmp/uv-cache`, but the sandbox still blocked creating a listening socket.
- Running the same startup command with approved sandbox escalation reached the host network namespace.

### Suggested Fix
For local dev server startup in this environment, use a writable uv cache and approved sandbox escalation when binding ports.

### Metadata
- Reproducible: yes
- Related Files: backend/README.md
- See Also: ERR-20260614-009

---

## [ERR-20260614-012] dev_server_persistence_and_vite_args

**Logged**: 2026-06-14T10:07:02+09:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
Foreground dev server sessions and `nohup` background launches did not persist reliably after the Codex command turn; a systemd transient service did. The current frontend dev script is Vite and rejects Next-style `--hostname`.

### Error
```text
curl: (7) Failed to connect to 127.0.0.1 port 56765
CACError: Unknown option `--hostname`
```

### Context
- Commands attempted:
  - `npm run dev -- --hostname 0.0.0.0 --port 56765`
  - `nohup uv --cache-dir /tmp/uv-cache run uvicorn ... &`
- `npm run dev` currently expands to `vite --host 0.0.0.0 --port 3000`.
- The stable startup path was:
  - backend: `systemd-run --unit=production-ready-rag-backend ... uv --cache-dir /tmp/uv-cache run uvicorn ... --port 8000`
  - frontend: `systemd-run --unit=production-ready-rag-frontend ... frontend/node_modules/.bin/vite --host 0.0.0.0 --port 56765`

### Suggested Fix
Use `systemd-run` for dev servers that must remain available after the assistant turn, and match frontend CLI flags to the current framework (`--host` for Vite, `--hostname` for Next).

### Metadata
- Reproducible: yes
- Related Files: frontend/package.json, frontend/vite.config.ts, backend/README.md
- See Also: ERR-20260614-009, ERR-20260614-011

---

## [ERR-20260614-013] vitest_jest_runinband_option

**Logged**: 2026-06-14T02:15:32Z
**Priority**: low
**Status**: pending
**Area**: frontend

### Summary
Vitest rejected the Jest-specific `--runInBand` option during frontend test validation.

### Error
```text
CACError: Unknown option `--runInBand`
```

### Context
- Command attempted: `npm run test -- --runInBand`
- This project uses Vitest (`vitest run`), whose CLI does not accept Jest's serial execution flag.

### Suggested Fix
Use `npm run test` for the project default, or Vitest-supported flags such as `--pool` / `--maxWorkers` only when needed.

### Metadata
- Reproducible: yes
- Related Files: frontend/package.json

---

## [ERR-20260614-016] rg_missing_optional_env_example

**Logged**: 2026-06-14T12:10:36+09:00
**Priority**: low
**Status**: resolved
**Area**: docs

### Summary
Repository-wide docs scan failed because the command included `.env.example`, which is not present in this repository.

### Error
```text
rg: .env.example: No such file or directory (os error 2)
```

### Context
- Command attempted: `rg -n "staging_smoke|preflight-only|cleanup|Object Storage|staging smoke" backend/README.md docs/deployment.md docs/rag-architecture.md .env.example`
- The project currently has docs under `backend/README.md` and `docs/`, but no top-level `.env.example`.

### Suggested Fix
Before including optional files in targeted `rg` commands, either confirm they exist with `rg --files` or omit them from the fixed file list.

### Metadata
- Reproducible: yes
- Related Files: backend/README.md, docs/deployment.md

### Resolution
- **Resolved**: 2026-06-14T12:10:36+09:00
- **Notes**: Removed `.env.example` from subsequent targeted docs scans.

---

## [ERR-20260614-014] oci_settings_i18n_typecheck

**Logged**: 2026-06-14T11:57:53+09:00
**Priority**: low
**Status**: pending
**Area**: frontend

### Summary
Frontend typecheck failed after OCI settings UI changes because newly referenced i18n keys were missing, then duplicate key additions caused `TS1117`.

### Error
```text
TS2345: Argument of type '"settings.oci.actions.selectKeyFile"' is not assignable to parameter of type I18nKey.
TS1117: An object literal cannot have multiple properties with the same name.
```

### Context
- Command attempted: `npm run typecheck`
- `OciSettingsClient.tsx` referenced key-file picker labels while `frontend/src/lib/i18n.ts` did not yet expose those keys in the typed `ja` object.
- Adding the missing keys without first checking the surrounding block introduced duplicates because related keys already existed later in the same object.

### Suggested Fix
When adding typed i18n keys, search the locale object for existing related keys first, add each key once, and rerun `npm run typecheck` before build.

### Metadata
- Reproducible: yes
- Related Files: frontend/src/lib/i18n.ts, frontend/src/components/settings/OciSettingsClient.tsx

---

## [ERR-20260614-015] enterprise_ai_settings_contract_drift

**Logged**: 2026-06-14T12:06:36+09:00
**Priority**: medium
**Status**: resolved
**Area**: config

### Summary
Full backend tests failed after adding Enterprise AI response path settings because readiness, settings API schemas, and test fixtures were not updated together.

### Error
```text
FAILED tests/test_health.py::test_readiness_oci_complete_config_is_ok
FAILED tests/test_settings_api.py::test_update_model_settings_mutates_runtime_settings
FAILED tests/test_staging_smoke.py::test_staging_smoke_uses_unique_marker_query_and_document_filter
AssertionError: assert 'missing' == 'ok'
```

### Context
- Command attempted: `uv run pytest`
- Enterprise AI readiness currently requires endpoint, project OCID, paths, model/template, and auth mode.
- Adding new settings fields also required updating the model settings API schema, runtime apply path, frontend API type, and complete OCI test fixtures.

### Suggested Fix
When adding new runtime settings, update Settings, API schemas, readiness/status checks, UI/API types, `.env.example`, docs, and complete-config fixtures in one pass before full test runs.

### Metadata
- Reproducible: yes
- Related Files: backend/app/config.py, backend/app/schemas/settings.py, backend/app/api/routes/settings.py, backend/app/readiness.py, frontend/src/lib/api.ts

### Resolution
- **Resolved**: 2026-06-14T12:06:36+09:00
- **Notes**: Added response path fields to settings schemas/API types, propagated runtime apply/readback, updated OCI readiness fixtures, and reran full backend/frontend validation.

---

## [ERR-20260614-017] git_index_sandbox_readonly

**Logged**: 2026-06-14T13:02:00+09:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary
`git add -A` failed because the managed sandbox could not create `.git/index.lock`.

### Error
```text
fatal: Unable to create '/u01/workspace/no.1-production-ready-rag/.git/index.lock': Read-only file system
```

### Context
- Command attempted: `git add -A`
- The workspace allows source-file edits, but `.git` writes may require sandbox escalation in this environment.
- 2026-06-30 に REVIEW 編集変更の stage 時も同じ `.git/index.lock` エラーが再発した。

### Suggested Fix
When staging, committing, or pushing from this desktop sandbox, rerun Git operations that write `.git` with approved sandbox escalation.

### Metadata
- Reproducible: yes
- Related Files: .git/index
- Recurrence-Count: 2
- Last-Seen: 2026-06-30

---

## [ERR-20260614-018] settings_namespace_threadpool_hang

**Logged**: 2026-06-14T13:00:00+09:00
**Priority**: medium
**Status**: resolved
**Area**: backend

### Summary
The Object Storage namespace settings endpoint hung in the ASGI test client when the OCI SDK call was wrapped with `asyncio.to_thread()` / Starlette threadpool helpers.

### Error
```text
tests/test_settings_api.py::test_read_object_storage_namespace_uses_oci_sdk
Timeout (0:00:10)!
... asyncio/runners.py line 72 in close
```

### Context
- Commands attempted: `uv --cache-dir /tmp/uv-cache run pytest`, targeted `pytest -vv`, and targeted `pytest -o faulthandler_timeout=10`.
- The endpoint is an explicit admin/settings action and the test uses mocked OCI imports.
- Direct synchronous invocation avoids the executor shutdown wait and keeps the API response contract unchanged.

### Suggested Fix
For this endpoint, keep `_read_object_storage_namespace()` synchronous inside the async route unless a production timeout-aware SDK wrapper is added.

### Metadata
- Reproducible: yes
- Related Files: backend/app/api/routes/settings.py, backend/tests/test_settings_api.py

### Resolution
- **Resolved**: 2026-06-14T13:00:00+09:00
- **Notes**: Replaced the threadpool call with direct `_read_object_storage_namespace(payload)` and reran backend full pytest successfully.

---

## [ERR-20260615-001] uv_cache_sandbox_readonly

**Logged**: 2026-06-15T18:52:08+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
`uv run pytest` failed inside the managed filesystem sandbox because uv could not create a temporary lock file under `/root/.cache/uv`.

### Error
```text
error: Could not acquire lock
  Caused by: Could not create temporary file
  Caused by: Read-only file system (os error 30) at path "/root/.cache/uv/.tmprH2J6c"
```

### Context
- Command attempted: `uv run pytest tests/test_document_workspace.py tests/test_knowledge_bases_api.py -q`
- The same command succeeded after rerunning with sandbox escalation.

### Suggested Fix
When `uv` needs its default cache under `/root/.cache/uv` in this environment, rerun the test command with approved sandbox escalation, or use a writable uv cache path if appropriate.

### Metadata
- Reproducible: yes
- Related Files: backend/pyproject.toml

### Resolution
- **Resolved**: 2026-06-15T18:52:08+09:00
- **Notes**: Reran the same backend pytest subset with sandbox escalation; 11 tests passed.

---

## [ERR-20260629-001] oracle_recoverable_read_disconnect

**Logged**: 2026-06-29T09:19:41+09:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
Oracle の読取中に一時切断され、後処理の close 例外が元の接続例外を上書きして文書詳細 API が 500 になった。

### Error
```text
DPY-4011: the database or network closed the connection
DPI-1080: connection was closed by ORA-03113
DPY-1001: not connected to database
DPI-1010: not connected
```

### Context
- `GET /api/documents/{id}` が重複元文書を読む途中で pooled connection を失った。
- `cursor.fetchall()` の `DPY-4011` に続き、`cursor.close()` の `DPY-1001` が元例外を隠した。
- python-oracledb はこの切断を `isrecoverable=True` として公開する。

### Suggested Fix
読取だけを recoverable 接続例外時に1回再試行し、既存例外がある場合は close 例外で上書きしない。transaction は再試行しない。

### Metadata
- Reproducible: yes
- Related Files: backend/app/clients/oracle.py, backend/tests/test_oracle_adapter.py
- Tags: oracle, connection-pool, retry, dpy-4011, ora-03113

### Resolution
- **Resolved**: 2026-06-29T09:19:41+09:00
- **Notes**: recoverable read retry、例外保持、非機密 retry log を追加。Oracle adapter 72 passed / 17 skipped、Ruff、mypy、対象 API 200 を確認。

---

## [ERR-20260629-002] black_check_unformatted_changes

**Logged**: 2026-06-29T20:35:39+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
PR 前の Black 検査で変更中の Python 3 ファイルが未整形だった。

### Error
```text
would reformat backend/tests/test_document_ingestion_config.py
would reformat backend/app/api/routes/documents.py
would reformat backend/app/clients/oracle.py
```

### Context
- Command attempted: `uv --cache-dir /tmp/uv-cache run black --check .`
- ロジック検査前の機械的な整形漏れで、Black による自動修正が可能。

### Suggested Fix
PR 作成前に Black を実行し、その後 CI と同じ `black --check` を再実行する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/api/routes/documents.py, backend/app/clients/oracle.py, backend/tests/test_document_ingestion_config.py
- Recurrence-Count: 3
- Last-Seen: 2026-06-30T06:40:00+09:00

### Resolution
- **Resolved**: 2026-06-29T20:35:39+09:00
- **Notes**: Black で対象ファイルを整形し、検査を再実行する。
- 2026-06-30T06:34:00+09:00: `backend/app/clients/oracle.py` の変更を Black で整形した。
- 2026-06-30T06:40:00+09:00: `backend/tests/test_knowledge_bases_api.py` の追加テストを Black で整形した。

---

## [ERR-20260629-003] gitleaks_directory_scan_local_secrets

**Logged**: 2026-06-29T20:47:00+09:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
作業ディレクトリ全体の gitleaks 検査が、Git 管理外のローカル runtime secret 2 件を検出した。

### Error
```text
WRN leaks found: 2
```

### Context
- Command attempted: `gitleaks dir . --config .gitleaks.toml --no-banner`
- 脱敏 JSON で確認した対象は `backend/.env` と `backend/model-settings.json` で、どちらも Git 管理外。
- CI は `gitleaks-action` で Git 履歴を検査するため、作業ディレクトリ全体の検査とは対象が異なる。

### Suggested Fix
PR の secret 検査はコミット後に Git 履歴を対象として実行し、ローカル runtime secret は出力・stage しない。

### Metadata
- Reproducible: yes
- Related Files: .gitleaks.toml, .gitignore

### Resolution
- **Resolved**: 2026-06-29T20:47:00+09:00
- **Notes**: 検出値を完全脱敏し、対象ファイルが Git 管理外であることを確認した。

---

## [ERR-20260629-004] gh_run_view_job_steps_json

**Logged**: 2026-06-29T20:54:00+09:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
`gh run view --job` では `steps` を JSON field として直接取得できなかった。

### Error
```text
Unknown JSON field: "steps"
```

### Context
- Command attempted: `gh run view RUN_ID --job JOB_ID --json status,conclusion,steps`
- この gh CLI では run の JSON field として `jobs` を取得し、その配下の step を参照する。

### Suggested Fix
`gh run view RUN_ID --json jobs` を使い、必要な job を JSON 側で選択する。

### Metadata
- Reproducible: yes
- Related Files: .github/workflows/ci.yml

### Resolution
- **Resolved**: 2026-06-29T20:54:00+09:00
- **Notes**: 利用可能 field の一覧を確認し、run-level の `jobs` を使う手順へ修正した。

---

## [ERR-20260630-002] oracle_migration_duplicate_column_index

**Logged**: 2026-06-30T06:48:00+09:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
Oracle migration statement 7 failed because it checked a legacy index by the wrong name and tried to create the same column list again.

### Error
```text
ORA-01408: such column list already indexed
ORA-06512: at line 41
```

### Context
- Command attempted: execute `artifacts/oracle-schema-migration.sql` using `backend/.env`.
- Statements 1-6 completed before statement 7 failed.
- Existing index: `RAG_INGESTION_SEGMENTS_DOCUMENT_STATUS_IDX`; migration checked only `...DOC_STATUS_IDX`.
- Statement 16 had the same mismatch for `RAG_DOCUMENT_EXTRACTIONS_DOCUMENT_IDX` versus `RAG_DOC_EXT_STATUS_IDX`.

### Suggested Fix
Accept both historical index names and create only the canonical `...DOCUMENT_STATUS_IDX` name when neither exists.

### Metadata
- Reproducible: yes
- Related Files: backend/app/rag/oracle_schema.py, artifacts/oracle-schema-migration.sql

### Resolution
- **Resolved**: 2026-06-30T06:54:00+09:00
- **Notes**: 两处历史索引名均已兼容；迁移 29/29 执行完成，验证 `DEFAULT=1`、legacy=0。

---

## [ERR-20260630-003] pytest_evaluation_asgi_request_hang

**Logged**: 2026-06-30T23:18:00+09:00
**Priority**: medium
**Status**: unresolved
**Area**: backend tests

### Summary
DB 接続を無効化した full pytest が evaluation API の ASGI request で停止する。

### Error
```text
test_run_evaluation_applies_suite_thresholds_when_request_omits
tests/support.py:77 in request (anyio event loop wait)
```

### Context
- `ORACLE_DSN=` と 600 秒 timeout を指定して full suite を実行。
- Recipe 対象テスト、Ruff、mypy は完了するが、上記既存テストは単独でも request 待機する。
- 実 DB や業務データには接続・書込していない。

### Suggested Fix
evaluation route が fake runner の完了後に待っている永続化処理を特定し、unit test では Oracle
artifact persistence も明示的に fake 化する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_evaluation.py, backend/tests/support.py

---

## [ERR-20260701-002] playwright_webserver_sandbox_binding

**Logged**: 2026-07-01T07:08:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
直接実行した `npx playwright test` はサンドボックス内で一時 Vite server を起動できなかった。

### Error
```text
Error: Process from config.webServer was not able to start. Exit code: 1
```

### Context
- Command attempted: `npx playwright test ... --trace=on`
- 承認済みの `npm run test:e2e -- ...` では同じテストと trace 生成が成功する。

### Suggested Fix
この環境では Playwright を package script の `npm run test:e2e -- ...` 経由で実行する。

### Metadata
- Reproducible: yes
- Related Files: frontend/playwright.config.ts, frontend/package.json

### Resolution
- **Resolved**: 2026-07-01T07:09:00+09:00
- **Notes**: package script 経由で desktop/mobile trace を生成し、最終フレームを確認した。

---

## [ERR-20260701-003] eslint_playwright_test_results_race

**Logged**: 2026-07-01T07:24:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
ESLint と Playwright を並列実行すると、Playwright が `test-results` を再作成する瞬間に ESLint の走査が失敗することがある。

### Error
```text
Error: ENOENT: no such file or directory, scandir 'frontend/test-results'
```

### Context
- Commands attempted concurrently: `npm run lint` and `npm run test:e2e -- ... --trace=on`
- Playwright のテスト自体と TypeScript/Vite build は成功した。

### Suggested Fix
ESLint と Playwright は直列に実行する。

### Metadata
- Reproducible: yes
- Related Files: frontend/eslint.config.mjs, frontend/playwright.config.ts
- Recurrence-Count: 2
- Last-Seen: 2026-07-03

### Resolution
- **Resolved**: 2026-07-01T07:25:00+09:00
- **Notes**: Playwright 完了後に `npm run lint` を単独で再実行する。

---

## [ERR-20260701-007] nullable_search_diagnostics_feedback_build

**Logged**: 2026-07-01T23:30:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
フィードバックへ検索・回答プロファイルIDを渡す際、nullableな検索 diagnostics を直接参照して型チェックに失敗した。

### Error
```text
SearchClient.tsx: 'meta.diagnostics' is possibly 'null'.
```

### Context
- `npm run build` で回答・引用フィードバック追加箇所の2件を検出した。
- runtime metadata には通常 diagnostics があるが、公開型は null を許容する。

### Suggested Fix
検索結果の補助情報は optional access を使い、選択中の検索・回答プロファイルIDへ安全にfallbackする。

### Metadata
- Reproducible: yes
- Related Files: frontend/src/components/search/SearchClient.tsx

### Resolution
- **Resolved**: 2026-07-01T23:30:00+09:00
- **Notes**: optional chaining と既存 searchAnswerProfileIds fallback に修正した。

---

## [ERR-20260701-008] feedback_e2e_strict_locator

**Logged**: 2026-07-01T23:35:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
理由名が集計と明細の両方へ表示され、部分一致Playwright locatorがstrict modeで失敗した。

### Error
```text
getByText('内容が正しくない') resolved to 2 elements
```

### Context
- 専用フィードバック画面のdesktop/mobileテストで同じ理由ラベルを検証した。
- UIは意図どおりで、テストlocatorだけが曖昧だった。

### Suggested Fix
集計側の理由ラベルは `{ exact: true }` または対象sectionで絞り込む。

### Metadata
- Reproducible: yes
- Related Files: frontend/e2e/feedback.spec.ts

### Resolution
- **Resolved**: 2026-07-01T23:35:00+09:00
- **Notes**: 完全一致locatorへ修正した。

---

## [ERR-20260701-009] feedback_oracle_count_mypy

**Logged**: 2026-07-01T23:40:00+09:00
**Priority**: low
**Status**: resolved
**Area**: backend

### Summary
Oracle集計行の `object` 値を直接 `int` 化し、mypyのoverload検証に失敗した。

### Error
```text
No overload variant of "int" matches argument type "object"
```

### Context
- `uv run mypy .` がフィードバック理由別件数の変換を検出した。
- 実Oracleでは件数がDecimalで返る可能性がある。

### Suggested Fix
`int | float | str | Decimal` を型確認してから整数化する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/api/routes/feedback.py

### Resolution
- **Resolved**: 2026-07-01T23:40:00+09:00
- **Notes**: Oracle Decimalを含む明示的な型guardを追加した。

---

## [ERR-20260702-002] playwright_grep_literal_parentheses

**Logged**: 2026-07-02T07:14:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
Playwright 标题筛选中的字面括号未被 shell 保留，导致没有匹配到测试。

### Error
```text
Error: No tests found.
```

### Context
- 仅重跑标题以 `(desktop)` 结尾的模型与 OCI 设置测试。
- `--grep \(desktop\)$` 经 shell 处理后成为分组正则，而不是字面括号。

### Suggested Fix
将 Playwright grep 表达式整体放入单引号，并在正则内转义字面括号。

### Metadata
- Reproducible: yes
- Related Files: frontend/e2e/model-settings-switch.spec.ts, frontend/e2e/oci-settings-layout.spec.ts

### Resolution
- **Resolved**: 2026-07-02T07:14:00+09:00
- **Notes**: 改用 `--grep '\\(desktop\\)$'` 与对应 mobile 表达式。

---

## [ERR-20260702-003] feedback_migration_nullable_reapply

**Logged**: 2026-07-02T07:20:00+09:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
フィードバックmigrationのNULL可変更が、2回目の実行でORA-01451になった。

### Error
```text
ORA-01451: column to be modified to NULL cannot be modified to NULL
ORA-06512: at line 46
```

### Context
- `20260701_001_general_feedback` を実Oracleへ初回適用後、再実行安全性を確認した。
- `document_id` と `chunk_id` は既にNULL可だったが、同じ `ALTER TABLE ... NULL` を再実行していた。

### Suggested Fix
`user_tab_columns.nullable = 'N'` の列だけをNULL可へ変更する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/rag/oracle_schema.py, backend/tests/test_oracle_schema_cli.py

### Resolution
- **Resolved**: 2026-07-02T07:20:00+09:00
- **Notes**: 対象2列をdata dictionaryから列挙し、NOT NULLの列だけALTERするよう修正した。

---

## [ERR-20260703-006] feedback_test_patch_context_drift

**Logged**: 2026-07-03T05:01:04+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
feedback API testへの複数hunk patchが、想定したtuple周辺のcontext不一致で適用されなかった。

### Error
```text
apply_patch verification failed: Failed to find expected lines
```

### Context
- `backend/tests/test_feedback_api.py` の複数箇所を一括更新しようとした。
- patchは原子的に失敗し、対象ファイルは変更されなかった。

### Suggested Fix
現行ファイルを再読込し、関数単位の小さいpatchへ分割する。

### Metadata
- Reproducible: yes
- Related Files: backend/tests/test_feedback_api.py
- See Also: ERR-20260703-004

### Resolution
- **Resolved**: 2026-07-03T05:01:04+09:00
- **Notes**: 関数単位の小さいpatchへ切り替えた。

---

## [ERR-20260703-007] feedback_playwright_webserver_sandbox

**Logged**: 2026-07-03T05:16:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
Playwright の Vite webServer が filesystem/network sandbox 内で起動できず、用例実行前に終了した。

### Error
```text
Error: Process from config.webServer was not able to start. Exit code: 1
```

### Context
- `npm run test:e2e -- e2e/feedback.spec.ts` を通常 sandbox で実行した。
- 実行環境が network namespace を分離しており、localhost listener の起動が失敗した。

### Suggested Fix
Playwright の browser/webServer 実行だけを承認済みの sandbox 外コマンドで再実行する。

### Metadata
- Reproducible: yes
- Related Files: frontend/playwright.config.ts, frontend/e2e/feedback.spec.ts
- Recurrence-Count: 2
- Last-Seen: 2026-07-03T15:52:00+09:00

### Resolution
- **Resolved**: 2026-07-03T05:16:00+09:00
- **Notes**: HuggingFace 設定 E2E でも再発し、同じ CI-mode command を sandbox 外で実行して desktop/mobile 6 件通過した。

---
## [ERR012] chat history unit test inherited remote stage defaults

**Date**: 2026-07-03
**Context**: Running `test_build_history_takes_first_assistant_per_turn` in isolation.
**Symptom**: The unit test exceeded 60 seconds before entering the local history assertions.
**Cause**: A fresh `Settings(rag_guardrail_backend="local")` still defaults `rag_guardrail_service_enabled=True`, so guardrail policy resolution attempted the unreachable pipeline service with a 120-second timeout.
**Fix**: Set `rag_guardrail_service_enabled=False` in local-only guardrail test fixtures.
**Prevention**: Unit tests that instantiate fresh `Settings` for in-process stage logic must explicitly disable the corresponding remote stage flag.
## [ERR013] duplicated backend path in final quality command

**Date**: 2026-07-03
**Context**: Final backend quality checks while the command working directory was already `backend/`.
**Symptom**: `rg backend/pyproject.toml` failed with “No such file or directory” and short-circuited the remaining checks.
**Cause**: The command repeated the working-directory prefix.
**Fix**: Use `pyproject.toml` relative to the declared backend working directory, then rerun all checks.
**Prevention**: Resolve command paths relative to `workdir` before composing chained quality commands.

---

## [ERR-20260703-NPM] dependency_audit_sandbox_network_block

**Logged**: 2026-07-03T08:34:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
ローカルの `npm audit` と `pip-audit` が sandbox 内の DNS 制限で registry に接続できず、sandbox 外実行も依存メタデータ送信リスクとして拒否された。

### Error
```text
getaddrinfo EAI_AGAIN registry.npmjs.org
NameResolutionError: pypi.org
CreateProcess rejected: dependency metadata may be disclosed to an untrusted destination
```

### Context
- GitHub CI と同じ `npm audit --audit-level=moderate` と `pip-audit` をローカルで実行した。
- sandbox 外への再実行承認も要求したが、ポリシー審査で拒否された。

### Suggested Fix
ローカルで迂回せず、PR の GitHub Actions Frontend job で dependency audit を確認する。

### Metadata
- Reproducible: yes
- Related Files: .github/workflows/ci.yml, frontend/package-lock.json, backend/uv.lock
- Recurrence-Count: 2
- Last-Seen: 2026-07-03T15:00:00+09:00

### Resolution
- **Resolved**: 2026-07-03T08:34:00+09:00
- **Notes**: 再発時は承認済み sandbox 外実行が許可され、`npm audit` 0 vulnerabilities、`pip-audit` 0 known vulnerabilities(1 ignore)を確認した。

---

## [ERR-20260703-CFG] compose_config_printed_local_secrets

**Logged**: 2026-07-03T15:00:00+09:00
**Priority**: high
**Status**: resolved
**Area**: infra

### Summary
Compose 構文検証が解決済み環境変数を標準出力へ展開し、ローカル secret をログへ露出した。

### Error
```text
docker compose config outputs the resolved configuration, including environment values.
```

### Context
- CI と同じ `docker compose config` をローカル `.env` がある workspace で実行した。
- secret の実値は学習ログへ保存しない。

### Suggested Fix
構文検証だけが目的の CI では `docker compose config --quiet` を使う。

### Metadata
- Reproducible: yes
- Related Files: .github/workflows/ci.yml

### Resolution
- **Resolved**: 2026-07-03T15:00:00+09:00
- **Notes**: CI の Compose 検証を quiet mode へ変更した。

---

## [ERR-20260703-SEC] gitleaks_directory_scan_included_ignored_files

**Logged**: 2026-07-03T08:35:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
PR 差分の事前確認に directory scan を使ったため、コミット対象外の ignored `.env` と未変更ファイルを検出した。

### Error
```text
leaks found: 2
```

### Context
- `gitleaks dir .` は Git の追跡・差分状態に関係なく作業ディレクトリを走査する。
- 検出値は表示せず、ファイル名とルール ID だけで対象外と確認した。

### Suggested Fix
PR 用の事前検査はコミット後に `gitleaks git --log-opts=origin/main..HEAD` で差分履歴へ限定する。

### Metadata
- Reproducible: yes
- Related Files: .gitleaks.toml, .gitignore

### Resolution
- **Resolved**: 2026-07-03T08:35:00+09:00
- **Notes**: ignored ファイルを変更せず、コミット後の差分履歴 scan に切り替えた。

---

## [ERR-20260703-THR] asyncio_to_thread_stalls_in_managed_sandbox

**Logged**: 2026-07-03T08:42:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
managed sandbox 内では `asyncio.to_thread()` の最小再現が完了せず、Backend テストがチャット履歴検査で停止した。

### Error
```text
timeout 5s python3 -c 'import asyncio; print(asyncio.run(asyncio.to_thread(lambda: 1)))'
exit code 124
```

### Context
- Oracle DSN と guardrail remote service を無効化しても同じテスト位置で停止した。
- 同じ対象テストは sandbox 外で直ちに 20 件すべて成功した。

### Suggested Fix
thread executor を使うテストは実 DB 接続を明示的に無効化し、承認済みの通常プロセスで実行する。

### Metadata
- Reproducible: yes
- Related Files: backend/app/api/routes/chat.py, backend/tests/test_chat_api.py
- Recurrence-Count: 2
- Last-Seen: 2026-07-03T15:12:00+09:00

### Resolution
- **Resolved**: 2026-07-03T08:42:00+09:00
- **Notes**: 同日、full pytest でも CPU 停止を再確認し、`ORACLE_DSN=` を指定した sandbox 外実行へ切り替えた。

---

## [ERR-20260703-STEP] ingestion_step_e2e_fixture_drift

**Logged**: 2026-07-03T08:45:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
工程表示を recipe `steps` 正本へ移した後も、Playwright fixture が job status と矛盾する文書状態と旧件数を期待していた。

### Error
```text
Expected 未実行: 3, received: 2
Expected 失敗, but fixture steps reported RUNNING
```

### Context
- 通しジョブではファイル準備が完了済みなので未実行は後続 2 工程だけになる。
- 失敗 job の fixture は recipe status/steps も `ERROR` / `FAILED` に揃える必要がある。

### Suggested Fix
E2E fixture は job phase から工程状態を再導出せず、API 契約どおり recipe `steps` と整合させる。

### Metadata
- Reproducible: yes
- Related Files: frontend/e2e/document-workspace-file-processing.spec.ts

### Resolution
- **Resolved**: 2026-07-03T08:45:00+09:00
- **Notes**: 期待件数と失敗 fixture を `steps` 正本へ合わせ、原因本文は上部メッセージへ 1 本化した。

---

## [ERR-20260703-ORCH] parallel_exec_javascript_quote_error

**Logged**: 2026-07-03T08:53:00+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
並行検証を起動する JavaScript の `prefix_rule` に引用符漏れがあり、tool 実行前に構文エラーになった。

### Error
```text
SyntaxError: Unexpected identifier 'env'
SyntaxError: Invalid or unexpected token
```

### Context
- 4 本の Backend 検証を `Promise.all` で起動する記述内の typo。
- CI 修正後の Frontend 並行検証でも object literal の typo が再発した。
- コマンドは 1 本も開始されず、リポジトリ状態への影響はなかった。

### Suggested Fix
複合 tool call の object literal と文字列配列を送信前に見直す。

### Metadata
- Reproducible: yes
- Related Files: none
- Recurrence-Count: 2
- Last-Seen: 2026-07-03

### Resolution
- **Resolved**: 2026-07-03T08:53:00+09:00
- **Notes**: 並行記述をやめ、E2E・build・lint を個別に起動した。

---

## [ERR-20260703-RACE] eslint_playwright_test_results_race

**Logged**: 2026-07-03T08:55:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
ESLint と Playwright を並行実行し、Playwright が `test-results` を入れ替える瞬間に ESLint の directory walk が失敗した。

### Error
```text
ENOENT: no such file or directory, scandir 'frontend/test-results'
```

### Context
- lint と E2E は同じ frontend tree を同時に走査・更新していた。
- E2E 完了後の単独 lint では再現しない。

### Suggested Fix
Playwright と repository-wide ESLint は並行実行せず、E2E 完了後に lint を実行する。

### Metadata
- Reproducible: yes
- Related Files: frontend/eslint.config.mjs, frontend/playwright.config.ts

### Resolution
- **Resolved**: 2026-07-03T08:55:00+09:00
- **Notes**: 同日再発。E2E 完了後に lint を単独で再実行する運用を再確認した。

---

## [ERR-20260703-CI] shared_ui_local_commit_masked_scroll_regression

**Logged**: 2026-07-03T09:10:00+09:00
**Priority**: medium
**Status**: resolved
**Area**: frontend tests

### Summary
ローカル sibling の shared UI は未 push commit で scroll chaining 修正済みだったが、CI は platform `origin/main` の旧 token を使うため回帰テストが失敗した。

### Error
```text
Expected overscrollBehaviorY: auto
Received: contain
```

### Context
- RAG 側の回帰テストは `bounded-scroll-area` が親ページへの wheel chaining を許可することを検証する。
- ローカル platform は origin/main より 1 commit 先で、CI checkout との差が事前検証を覆い隠した。

### Suggested Fix
CI が取得する sibling ref とローカル sibling の commit 差を確認し、アプリ側が未公開 sibling commit に依存しないようにする。

### Metadata
- Reproducible: yes
- Related Files: frontend/src/globals.css, frontend/e2e/knowledge-bases.spec.ts, .github/workflows/ci.yml

### Resolution
- **Resolved**: 2026-07-03T09:10:00+09:00
- **Notes**: RAG の globals.css で `overscroll-behavior-y: auto` を明示し、現行 shared UI 配布版の `contain` を上書きした。

---
## [ERR-20260703-012] frontend_node_modules_self_symlink

**Logged**: 2026-07-03T09:54:00+09:00
**Priority**: high
**Status**: resolved
**Area**: frontend

### Summary
Frontend の `node_modules` が自己参照 symlink になり、全 npm script が終了コード 216 で停止した。

### Error
```text
./node_modules/.bin/tsc: Too many levels of symbolic links
```

### Context
- `frontend/node_modules -> /u01/workspace/no.1-production-ready-rag/frontend/node_modules`
- build / lint / Vitest の並列・単独実行が同じ理由で開始できなかった。

### Suggested Fix
壊れた symlink を解除し、`package-lock.json` から依存を再構築してから各検証を直列実行する。

### Metadata
- Reproducible: yes
- Related Files: frontend/node_modules, frontend/package-lock.json

### Resolution
- **Resolved**: 2026-07-03T09:58:42+09:00
- **Notes**: 自己参照 symlink を解除し、`npm ci` で依存を復元後、build / lint / Vitest / Playwright を通過した。

---

## [ERR-20260703-DIR] frontend_node_modules_state_changed

**Logged**: 2026-07-03T11:25:00+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend tests

### Summary
追跡上は symlink の `frontend/node_modules` が検証開始時には実ディレクトリになっており、`unlink` が失敗した。

### Error
```text
unlink: cannot unlink 'node_modules': Is a directory
```

### Context
- 前回終了時は自己参照 symlink を復元していた。
- 今回開始時は依存を含む実ディレクトリで、npm scripts をそのまま実行可能だった。

### Suggested Fix
依存準備前に `stat` で種類を確認し、実ディレクトリなら再構築せずそのまま使う。

### Metadata
- Reproducible: environment-dependent
- Related Files: frontend/node_modules
- See Also: ERR-20260703-012

### Resolution
- **Resolved**: 2026-07-03T11:25:00+09:00
- **Notes**: 実ディレクトリを保持して検証に再利用し、検証後に追跡済み symlink を復元する。

---

## [ERR-20260703-GIT] git_fetch_workspace_sandbox

**Logged**: 2026-07-03T14:58:00+09:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
通常 sandbox では `.git/FETCH_HEAD` が read-only で、remote 更新に失敗した。

### Error
```text
error: cannot open .git/FETCH_HEAD: Read-only file system
```

### Context
- `git fetch origin --prune` を実行した。
- remote 操作の承認後は同じコマンドが成功した。

### Suggested Fix
この workspace の fetch / commit / push は承認済みの sandbox 外 Git 操作として実行する。

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-07-03T14:58:00+09:00
- **Notes**: sandbox 外で fetch を完了した。

---

## [ERR-20260703-HFS] huggingface_saved_feedback_refetch_race

**Logged**: 2026-07-03T15:49:54+09:00
**Priority**: medium
**Status**: resolved
**Area**: frontend tests

### Summary
HuggingFace 設定保存後の query refetch が成功表示を即座に消し、Frontend CI E2E が失敗した。

### Error
```text
Locator: getByText('保存しました')
Expected: visible
Error: element(s) not found
```

### Context
- PR #53 Frontend job の desktop test が retry 2 回を含めて失敗した。
- mutation 成功時に `saved=true` とした直後、invalidate/refetch で `query.data` effect が `saved=false` へ戻していた。
- ローカルでは refetch timing が遅く、同じ E2E が成功表示を先に観測できていた。

### Suggested Fix
server state hydration では成功表示を消さず、ユーザーが次にフォームを編集した時だけ `saved=false` に戻す。

### Metadata
- Reproducible: timing-dependent
- Related Files: frontend/src/components/settings/HuggingFaceSettingsClient.tsx, frontend/e2e/huggingface-settings.spec.ts

### Resolution
- **Resolved**: 2026-07-03T15:52:00+09:00
- **Commit/PR**: #53
- **Notes**: query hydration から成功表示の reset を削除し、CI mode の desktop/mobile 6 件で検証した。

---
## 2026-07-07 — 大きな複数箇所パッチのコンテキスト不一致

- 外部 parser 登録表のリファクタで、重複行を含む想定コンテキストが実ファイルと一致せず `apply_patch` が失敗した。
- 対応: 変更を小さなパッチに分割し、直前の実ファイル断片を基準に適用する。

## 2026-07-07 — sandbox 内の uv lock が DNS で失敗

- `UV_CACHE_DIR=/tmp/uv-cache uv lock` は PyPI の名前解決に失敗した。
- 対応: 同じ限定コマンドをネットワーク許可付きで再実行し、PyMuPDF を lock へ追加した。

## 2026-07-07 — Playwright の部分一致ラベルが checkbox も取得

- `getByLabel("API key")` が入力 4 件だけでなく「保存済み API key を削除」checkbox 4 件にも一致した。
- 対応: フィールド件数の検証では `{ exact: true }` を指定した。

## 2026-07-07 — root からの uv run が既定 cache へ書けず失敗

- package 単体テストで `UV_CACHE_DIR` を付け忘れ、`/root/.cache/uv` が read-only で失敗した。
- 対応: リポジトリ内の uv コマンドは常に `UV_CACHE_DIR=/tmp/uv-cache` を明示する。
- 再発: backend の Ruff でも同じ失敗を確認。以降の検証は writable cache を明示する。

## 2026-07-07 — Black の複数ファイル diff が sandbox の socket 制限で失敗

- `black --diff` を複数ファイルへ実行すると SyncManager の socket 作成が EPERM になった。
- 対応: 対象を 1 ファイルずつ実行して差分を確認する。

## [ERR-20260707-GDM] git_diff_deleted_node_modules_hang

**Logged**: 2026-07-07T20:12:11+09:00
**Priority**: low
**Status**: resolved
**Area**: frontend

### Summary
削除扱いの `frontend/node_modules` を含む `git diff` が 60 秒以上停止した。

### Error
```text
git diff -- frontend/node_modules が出力なしで停止し、手動終了した。
```

### Context
- 未コミット差分レビューで追跡済み `frontend/node_modules` の種類を確認しようとした。
- 複数の独立コマンドを同じ実行へまとめていたため、後続確認も待たされた。

### Suggested Fix
`git ls-tree` と `stat` を個別に実行し、`node_modules` 自体には `git diff` を使わない。

### Metadata
- Reproducible: unknown
- Related Files: frontend/node_modules
- See Also: ERR-20260703-DIR

### Resolution
- **Resolved**: 2026-07-07T20:12:11+09:00
- **Notes**: 停止プロセスを終了し、対象別の読み取り専用コマンドへ切り替えた。

---

## [ERR-20260707-PKGQ] rag_parser_core_quality_baseline

**Logged**: 2026-07-07T22:57:01+09:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
rag_parser_core 全体の Ruff / mypy は、本変更外の既存違反で完走しなかった。

### Error
```text
Ruff: rag_parser_core/oci_enterprise_ai.py:167 E501 (105 > 100)
mypy: 3 個の既存テストファイルで 23 errors
```

### Context
- Picture asset remap 修正後に package 全体を検証した。
- 変更対象の `registry.py` には Ruff / mypy の新規指摘はなかった。
- backend 側の Ruff と mypy、および関連する parser テストは成功した。

### Suggested Fix
既存の package テスト型エラーと E501 を別変更で解消し、package 単体の品質チェックを CI に固定する。

### Metadata
- Reproducible: yes
- Related Files: packages/rag_parser_core/rag_parser_core/oci_enterprise_ai.py, packages/rag_parser_core/tests/test_ocr_engine_remap.py, packages/rag_parser_core/tests/test_service_contract.py, packages/rag_parser_core/tests/test_preprocess_contract.py

### Resolution
- **Resolved**: 2026-07-07T23:04:00+09:00
- **Notes**: 既存 E501 と 23 件のテスト型エラーを最小修正し、package Ruff / mypy と OCR remap 24 テストを通過した。

---
