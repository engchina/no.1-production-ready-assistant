# NL2SQL AI 活用 全機能テスト仕様・テストデータ

## 1. 目的

本書は `Production Ready NL2SQL` のサイドナビ **AI 活用** 配下にある全機能を対象に、Markdown 版のテスト仕様、確認観点、テストデータ、入力ファイル fixture をまとめたものです。

対象メニュー:

| メニュー | 画面 URL | 主な対象 |
| --- | --- | --- |
| SQL 生成 | `/query` | 自然言語から SQL 生成、profile 推薦、schema 参照、rewrite、similar history、preview、job 実行、preview SQL 実行、feedback、業務確認 flow |
| SELECT SQL を実行 | `/direct-sql` | SELECT / WITH の直接実行、SQL ファイル読込、SELECT-only 安全境界 |
| SQL から質問を生成 | `/sql-to-question` | SQL 逆生成、deep 逆生成、論理構造解析、profile / schema 参照 |
| 実行履歴 | `/history` | 履歴一覧、検索、feedback / safety filter、sort、detail、再実行導線 |

テストデータ配置先:

`docs/test-data/nl2sql-ai-utilization/`

## 2. 共通前提

- UI 表示言語は日本語を前提にする。
- `AI 活用` メニューの閲覧には `search.view` 権限を使う。
- SQL 実行ボタンは `search.execute` 権限が必要。権限なしでは実行不可 banner を確認する。
- `SELECT SQL を実行` と SQL 生成 preview 実行は `/api/nl2sql/execute` を使う。管理 DDL/DML の `/api/nl2sql/db-admin/execute` とは別の SELECT-only 境界である。
- 実 OCI / Oracle 接続がない環境では、mock response を使って UI 状態を検証する。
- fixture の SQL は検証用であり、本番 DB に対して破壊的 SQL を実行しない。
- DML / DDL / 複文 SQL は「ブロックされること」を確認するための入力データである。

## 3. 共通 UI / 非機能テスト

| ID | 観点 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| AI-COM-001 | サイドナビ導線 | AI 活用の 4 メニューを順にクリック | ページタイトル、選択状態、URL が一致する | - |
| AI-COM-002 | 権限制御 | `search.view` のみで表示し、`search.execute` なしで実行系画面を開く | 閲覧は可能、実行ボタンは非表示/disabled または権限 banner | - |
| AI-COM-003 | persistence 未準備 | `/api/nl2sql/persistence` または対象 GET を失敗させる | 画面全体が落ちず、復旧/再試行導線が表示される | `mock-responses/error.persistence-unavailable.json` |
| AI-COM-004 | loading | API 応答を 1 秒以上遅延 | `TimedLoadingState` / skeleton / processing indicator が表示され、多重実行できない | `mock-responses/*.json` |
| AI-COM-005 | API エラー | 各 POST を 400/500 にする | `PageNotice` または `FormStatus` に再試行可能なエラーが表示される | `mock-responses/error.validation.json` |
| AI-COM-006 | 長い SQL | 長い SELECT を入力 | textarea / code block / table が横崩れせず、必要箇所だけ横スクロール | `sql/long-select.sql` |
| AI-COM-007 | モバイル幅 | 375px 幅で各ページ主要導線を操作 | ボタン・表・分割 panel が重ならず、detail へフォーカス移動できる | - |
| AI-COM-008 | キーボード | Tab / Shift+Tab / Enter / Space で操作 | 入力、toggle、button、filter、tab がキーボードで操作できる | - |

## 4. SQL 生成

対象 API:

- `GET /api/nl2sql/history`
- `GET /api/nl2sql/profiles/search`
- `GET /api/nl2sql/profiles/{profile_id}`
- `GET /api/schema/catalog/head`
- `GET /api/schema/objects`
- `GET /api/schema/objects/{owner}/{object_name}`
- `POST /api/nl2sql/recommend-profile`
- `POST /api/nl2sql/similar-history`
- `POST /api/nl2sql/rewrite`
- `POST /api/nl2sql/preview`
- `POST /api/nl2sql/execute`
- `POST /api/nl2sql/jobs`
- `GET /api/nl2sql/jobs/{job_id}`
- `POST /api/nl2sql/feedback`
- `POST /api/nl2sql/select-ai/feedback/add`
- `POST /api/nl2sql/ontology/profile-recommendations`
- `POST /api/nl2sql/ontology/profile-recommendations/{recommendation_id}/confirm`
- `POST /api/nl2sql/query-sessions`
- `PATCH /api/nl2sql/query-sessions/{session_id}/intent`
- `POST /api/nl2sql/query-sessions/{session_id}/generate-sql`
- `POST /api/nl2sql/query-sessions/{session_id}/confirm-sql`
- `POST /api/nl2sql/query-sessions/{session_id}/execute`

`query-sessions` の作成・intent/SQL 確認・SQL 生成は `nl2sql.query.generate` を要求する。
ただし `query-sessions/{session_id}/execute` は実 DB へ SELECT を発行するため、
通常の `POST /api/nl2sql/execute` と同じ `nl2sql.sql.execute` を要求する。

| ID | 機能 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| QRY-001 | 初期表示 | `/query` を開く | engine selector、profile selector、質問 textarea、schema 参照、preview/run ボタンが表示される | `mock-responses/profiles-search.success.json`、`mock-responses/schema-objects.success.json` |
| QRY-002 | profile 自動選択 | profile list 取得後、既定 profile が選択される | profile detail と allowed object が反映される | `mock-responses/profile-detail.default.json` |
| QRY-003 | profile 推薦 | 質問を 4 文字以上入力し 500ms 待つ | 推薦 profile、confidence、候補が表示され、適用できる | `api/recommend-profile.request.json`、`mock-responses/recommend-profile.success.json` |
| QRY-004 | schema 参照挿入 | schema panel の table / column をクリック | 質問 textarea のカーソル位置へ日本語論理名が挿入される | `mock-responses/schema-object-detail.orders.json` |
| QRY-005 | engine 切替 | `select_ai` / `select_ai_agent` / `enterprise_ai_direct` / `auto` を選択 | POST payload の `engine` が選択値になる | `api/preview.request.json`、`api/job-create.request.json` |
| QRY-006 | Select AI override | engine `select_ai` で詳細設定を開き role / additional instructions を入力 | `select_ai_overrides` が payload に含まれる | `api/preview-select-ai-overrides.request.json` |
| QRY-007 | override 不正組合せ | engine `enterprise_ai_direct` で override 付き payload を送る | backend validation で 400 になり、UI はエラーを表示 | `api/preview-invalid-overrides.request.json` |
| QRY-008 | 質問 rewrite(用語置換のみ) | `用語・同義語を使う` を ON にして「SQL を生成して実行」 | glossary 置換が起きたときだけ書き換えカード(変更前/生成に使用される質問・適用ボタン)が出る。無変換ならカードは出ず、件数表現などは追加されない | `api/rewrite.request.json`、`mock-responses/rewrite.success.json` |
| QRY-009 | similar history | 質問入力後 650ms 待つ | 類似履歴の score、reason、SQL snippet が表示される | `api/similar-history.request.json`、`mock-responses/similar-history.success.json` |
| QRY-010 | preview 成功 | `SQL プレビュー` を実行 | SQL、safety badge、推奨事項、`preview SQL を実行` 導線が表示される | `api/preview.request.json`、`mock-responses/preview.safe.json` |
| QRY-011 | preview block | DML を誘導する質問で preview | blocked badge、blocked reason、実行ボタン disabled | `mock-responses/preview.blocked.json` |
| QRY-012 | preview SQL 実行 | safe preview 後に preview SQL を実行 | `/api/nl2sql/execute` が呼ばれ、結果表が表示される | `api/execute-select.request.json`、`mock-responses/execute.success.json` |
| QRY-013 | 非同期 run | `実行` を押す | job 作成後、polling 中ステップが表示され、done で結果表と履歴が更新される | `mock-responses/jobs.created.json`、`mock-responses/job.running.json`、`mock-responses/job.done.json` |
| QRY-014 | job error | job が error で返る | OperationStatusStrip にエラーが出て、結果表は更新されない | `mock-responses/job.error.json` |
| QRY-015 | reset | 入力、selection、結果、override がある状態で reset | 全 UI 状態が初期化され、tracked job も消える | - |
| QRY-016 | sample data import | catalog empty の状態で sample import を押す | confirmation `ADMIN_EXECUTE` 付き import API が呼ばれる | `api/sample-data-import.request.json` |
| QRY-017 | feedback good | 実行結果ありで「良い」を押す | Select AI feedback add と app feedback が保存され、toast success | `api/select-ai-feedback-good.request.json`、`api/feedback-good.request.json` |
| QRY-018 | feedback bad 必須 | 「違う」で修正 SQL またはコメントを空にする | クライアント側で必須メッセージが出る | `api/select-ai-feedback-bad.request.json` |
| QRY-019 | 業務確認 profile 推薦 | `業務確認 flow` を開始 | ontology profile 候補が表示され、候補を確認できる | `api/ontology-profile-recommendation.request.json`、`mock-responses/ontology-profile-recommendation.success.json` |
| QRY-020 | query session 作成 | profile 推薦を確認後、再度 flow を開始 | `query-sessions` が作成され、intent 確認 UI が表示される | `api/query-session-create.request.json`、`mock-responses/query-session-created.json` |
| QRY-021 | query session 版競合 | intent patch が 409 を返す | current version と再読込導線が表示される | `mock-responses/query-session-conflict.error.json` |

## 5. SELECT SQL を実行

対象 API:

- `POST /api/nl2sql/execute`

| ID | 機能 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| DIR-001 | 初期表示 | `/direct-sql` を開く | SQL textarea、SQL ファイル読込、実行、クリアが表示される | - |
| DIR-002 | 権限なし | `search.execute` なしで開く | 実行フォームではなく権限 banner が表示される | - |
| DIR-003 | SELECT 実行 | SELECT SQL を入力し実行 | `/api/nl2sql/execute` payload に SQL が入り、結果表表示 | `sql/direct-select.sql`、`mock-responses/execute.success.json` |
| DIR-004 | WITH 実行 | WITH SQL を入力し実行 | SELECT-only として許可される | `sql/direct-with.sql` |
| DIR-005 | DML ブロック | UPDATE / DELETE を入力し実行 | 400 エラー、結果表は表示されない | `sql/blocked-dml.sql`、`mock-responses/error.select-only.json` |
| DIR-006 | 複文ブロック | SELECT; DELETE; を入力 | 複文が拒否される | `sql/blocked-multiple-statements.sql` |
| DIR-007 | SQL ファイル読込 | `.sql` / `.txt` を drop または選択 | textarea に内容が反映される | `sql/direct-select.sql`、`sql/direct-select.txt` |
| DIR-008 | クリア | 実行後にクリア | SQL、結果、エラー、file input が初期化される | - |

## 6. SQL から質問を生成

対象 API:

- `GET /api/nl2sql/profiles/search`
- `GET /api/nl2sql/profiles/{profile_id}`
- `GET /api/schema/objects`
- `GET /api/schema/objects/{owner}/{object_name}`
- `POST /api/nl2sql/analyze`
- `POST /api/nl2sql/reverse`
- `POST /api/nl2sql/reverse/deep`

| ID | 機能 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| REV-001 | 初期表示 | `/sql-to-question` を開く | profile selector、SQL textarea、用語・同義語 checkbox、schema preview、3 step が表示される | `mock-responses/profiles-search.success.json` |
| REV-002 | 参照データ読込 | profile を選択 | profile detail と schema object detail が読み込まれ、参照 schema が表示される | `mock-responses/profile-detail.default.json`、`mock-responses/schema-object-detail.orders.json` |
| REV-003 | 標準逆生成 | SQL を入力し「質問を生成」 | question、explanation、referenced_tables、logical_steps が表示される | `api/reverse.request.json`、`mock-responses/reverse.success.json` |
| REV-004 | deep 逆生成 | SQL を入力し deep を実行 | source が Enterprise AI 系になり、logical_structure が更新される | `api/reverse-deep.request.json`、`mock-responses/reverse.deep.success.json` |
| REV-005 | 構造解析 | 「SQL 構造を解析」を押す | analysis 結果から structure text が生成される | `api/analyze-safe.request.json`、`mock-responses/analyze.safe.json` |
| REV-006 | 用語・同義語 OFF | `use_glossary=false` で逆生成 | request payload が false になり、用語置換をしない説明が返る | `api/reverse-no-glossary.request.json` |
| REV-007 | reference load error | profile/schema GET を 500 にする | PageNotice と再読込ボタンが表示される | `mock-responses/error.persistence-unavailable.json` |

## 7. 実行履歴

対象 API:

- `GET /api/nl2sql/history`

| ID | 機能 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| HIS-001 | 初期表示 | `/history` を開く | 一覧、検索、feedback filter、safety filter、sort、detail panel が表示される | `mock-responses/history.success.json` |
| HIS-002 | 空状態 | history が空 | EmptyState が表示される | `mock-responses/history.empty.json` |
| HIS-003 | 検索 filter | `未入金` で検索 | question / SQL / rewritten_question に一致する履歴だけ表示 | `mock-responses/history.success.json` |
| HIS-004 | feedback filter | `good` / `bad` / `unrated` を切替 | filter 条件に合う履歴だけ表示 | `mock-responses/history.success.json` |
| HIS-005 | safety filter | safe / blocked を切替 | blocked SQL 履歴が分離される | `mock-responses/history.success.json` |
| HIS-006 | sort | question / created_at sort を切替 | 昇順/降順が切り替わる | `mock-responses/history.success.json` |
| HIS-007 | detail tab | 履歴を選択し overview / SQL tab を切替 | 質問、profile、row/column count、generated/executable SQL が確認できる | `mock-responses/history.success.json` |
| HIS-008 | 再実行 | detail の再実行を押す | `/query` に遷移し、question / engine / profile が prefill される | `mock-responses/history.success.json` |
| HIS-009 | refresh | 表示更新を押す | loading indicator 後に toast success | `mock-responses/history.success.json` |
| HIS-010 | load error | history GET を 500 にする | retry hint と再試行ボタンが表示される | `mock-responses/error.persistence-unavailable.json` |

## 8. 推奨 Playwright fixture 利用例

```ts
import { test, expect } from "@playwright/test";
import preview from "../../docs/test-data/nl2sql-ai-utilization/mock-responses/preview.safe.json";

test("SQL 生成 preview の結果を表示できる", async ({ page }) => {
  await page.route("**/api/nl2sql/preview", async (route) => {
    await route.fulfill({ json: preview });
  });

  await page.goto("/query");
  await page.getByLabel("質問").fill("今月の受注金額上位5件");
  await page.getByRole("button", { name: "SQL プレビュー" }).click();
  await expect(page.getByText("SELECT")).toBeVisible();
});
```

## 9. 後片付け

- 実 Oracle で検証した場合は、履歴と feedback が検証用 profile に保存されていることを確認する。
- DML / DDL fixture は実行禁止。安全境界でブロックされることだけを確認する。
- Select AI feedback add は DBMS_CLOUD_AI profile へ学習データを追加する可能性があるため、専用検証 profile で実施する。
- query session / ontology flow は versioned state を作成するため、検証後に対象 session/proposal をアーカイブまたは検証 DB を破棄する。
