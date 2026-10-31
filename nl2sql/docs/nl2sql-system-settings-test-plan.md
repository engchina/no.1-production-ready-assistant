# NL2SQL システム設定 全機能テスト仕様・テストデータ

## 1. 目的

本書は `Production Ready NL2SQL` のサイドナビ **システム設定** 配下にある全機能を対象に、Markdown 版のテスト仕様、確認観点、テストデータ、アップロード用テストファイルをまとめたものです。

対象メニュー:

| メニュー | 画面 URL | 主な対象 |
| --- | --- | --- |
| OCI 認証 | `/settings/oci` | OCI config 読込、秘密鍵アップロード、認証保存、接続確認、Object Storage namespace 取得 |
| アップロード保存先 | `/settings/upload-storage` | local / OCI Object Storage 保存先切替、保存、入力検証 |
| モデル | `/settings/model` | OCI Enterprise AI / OCI Generative AI 設定、モデル一覧、接続テスト、テンプレート検証 |
| データベース | `/settings/database` | Oracle 接続設定、wallet upload / 自動取得、ADB start / stop、接続テスト |
| システムテーブル | `/settings/system-tables` | NL2SQL system table 状態確認、初期化、再作成、実行権限制御 |
| 外観と証明書 | `/settings/appearance` | light / dark / system theme 切替、localStorage 永続化 |

テストデータ配置先:

`docs/test-data/nl2sql-system-settings/`

## 2. 共通前提

- UI 表示言語は日本語を前提にする。
- 管理者ロール `SYSTEM_ADMIN` または同等の権限で検証する。
- 実 OCI OCID、秘密鍵、DB パスワード、wallet は使用しない。本文書の fixture はすべてダミー値である。
- 実接続テストは、検証環境の `.env` / OCI / Oracle ADB が設定済みの場合のみ成功を期待する。未設定環境では失敗メッセージと復旧導線を確認する。
- 設定保存系 API は環境設定ファイルや runtime 設定へ反映されるため、ローカルまたは検証用環境で実施する。
- Playwright では `docs/test-data/nl2sql-system-settings/mock-responses/` の JSON を API mock として使用できる。
- ファイルアップロード試験では `oci/` と `database/` 配下の fixture を使用する。

## 3. 共通 UI / 非機能テスト

| ID | 観点 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| SYS-COM-001 | サイドナビ導線 | システム設定を展開し、6 メニューを順にクリックする | 各ページタイトル、パンくず、選択状態が正しく表示される | - |
| SYS-COM-002 | ローディング | API 応答を遅延させる | スケルトンまたは読込状態が表示され、ボタンが多重実行されない | `mock-responses/*.json` |
| SYS-COM-003 | API エラー | 各 GET API を 500 応答にする | エラー文言、再読込/再試行導線が表示される | `mock-responses/system-tables-failed.json` |
| SYS-COM-004 | 権限制御 | 閲覧権限のみでアクセスする | 保存/実行系ボタンが非表示または disabled になり、状態は閲覧できる | - |
| SYS-COM-005 | 秘密情報マスク | 設定取得後に画面と DevTools log を確認する | API key、DB password、wallet password、private key 本文が表示されない | `mock-responses/*.json` |
| SYS-COM-006 | 入力保持 | 入力途中にバリデーションエラーを発生させる | 入力値が消えず、エラー箇所が特定できる | `api/*.json` |
| SYS-COM-007 | キーボード操作 | Tab / Shift+Tab / Enter / Space で操作する | フォーカス順が自然で、主要ボタン・toggle がキーボードで操作できる | - |
| SYS-COM-008 | モバイル幅 | 375px 幅で各ページを開く | 横スクロールが必要な表以外で文字やボタンが重ならない | - |

## 4. OCI 認証

対象 API:

- `GET /api/settings/oci`
- `PATCH /api/settings/oci`
- `POST /api/settings/oci/config/read`
- `POST /api/settings/oci/config/test`
- `POST /api/settings/oci/key-file`
- `POST /api/settings/oci/object-storage/namespace`
- `PATCH /api/settings/oci/object-storage`

| ID | 機能 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| OCI-001 | 初期表示 | OCI 認証ページを開く | config path、profile、user、tenancy、fingerprint、region、key 状態が表示される | `mock-responses/oci-settings.success.json` |
| OCI-002 | config 読込成功 | `OCI 設定ファイルを読み込む` を実行 | `~/.oci/config` / `DEFAULT` から user, tenancy, fingerprint, region が反映される | `oci/config.valid`、`api/oci-config-read.request.json` |
| OCI-003 | config 欠落 | 欠落 config を読み込ませる | 欠落フィールドがエラー表示され、既存値は不用意に消えない | `oci/config.missing-fields` |
| OCI-004 | 秘密鍵アップロード成功 | `.pem` ファイルを選択する | アップロード成功 toast、key 存在状態、key path が更新される | `oci/oci_api_key.pem` |
| OCI-005 | 秘密鍵拡張子エラー | `.txt` を選択する | クライアント側で拒否され、API が呼ばれない | `oci/oci_api_key.invalid.txt` |
| OCI-006 | 認証設定保存 | user / fingerprint / tenancy / region を変更して保存 | PATCH payload が送信され、保存成功後に再取得値が表示される | `api/oci-settings.patch.json` |
| OCI-007 | 接続確認成功 | config/key ありの状態で接続確認 | success 状態、region / tenancy 確認結果が表示される | `mock-responses/oci-config-test.success.json` |
| OCI-008 | 接続確認失敗 | key 不正または OCID 不正で接続確認 | failed 状態、原因メッセージ、修正対象が表示される | `mock-responses/oci-config-test.failed.json` |
| OCI-009 | Namespace 取得 | Object Storage namespace 取得を実行 | namespace が入力欄へ反映される | `api/oci-namespace.request.json` |
| OCI-010 | Object Storage 共通設定保存 | region / namespace を変更して保存 | `PATCH /api/settings/oci/object-storage` が送信され、アップロード保存先画面でも同値が参照できる | `api/oci-object-storage.patch.json` |

## 5. アップロード保存先

対象 API:

- `GET /api/settings/upload-storage`
- `PATCH /api/settings/upload-storage`

| ID | 機能 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| UPL-001 | local 初期表示 | ページを開く | backend が `local`、local path、最大アップロードサイズが表示される | `mock-responses/upload-storage-local.success.json` |
| UPL-002 | local 保存 | local backend を選択し保存 | local path が保存され、bucket 未設定でも成功する | `api/upload-storage-local.patch.json` |
| UPL-003 | OCI 保存 | OCI backend を選択し namespace / bucket を入力して保存 | namespace / bucket が保存され、保存先説明が OCI 表示になる | `api/upload-storage-oci.patch.json` |
| UPL-004 | bucket 名検証 | 不正文字を含む bucket 名で保存 | 保存が止まり、bucket 名エラーが表示される | `upload-storage/invalid-bucket.json` |
| UPL-005 | namespace 名検証 | 空または不正 namespace で OCI 保存 | 保存が止まり、namespace エラーが表示される | `api/upload-storage-oci-invalid-namespace.patch.json` |
| UPL-006 | backend 切替保持 | local → OCI → local に切り替える | 各入力値が不必要に消えず、保存 payload は選択 backend に対応する | `api/*.json` |
| UPL-007 | DB/OCI 未設定時 | OCI 認証が未設定の mock で表示する | 保存先設定画面自体は開け、必要情報の不足が分かる | `mock-responses/upload-storage-local.success.json` |

## 6. モデル

対象 API:

- `GET /api/settings/model`
- `PATCH /api/settings/model`
- `POST /api/settings/model/test`

| ID | 機能 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| MOD-001 | 初期表示 | モデル設定ページを開く | Enterprise AI endpoint / project / model list / default model / Generative AI embedding & rerank が表示される | `mock-responses/model-settings.success.json` |
| MOD-002 | Enterprise AI 保存 | endpoint、project、モデル一覧、既定の Vision モデル・既定のテキストモデルを変更して保存 | PATCH payload が送信され、保存成功 toast が出る | `api/model-settings.patch.json` |
| MOD-003 | API key 更新 | 新しい API key を入力して保存 | API key 本文は応答や画面に出ず、has_api_key が true になる | `model/model-settings.valid.json` |
| MOD-004 | API key clear | clear_api_key を有効にして保存 | 保存後 has_api_key が false になる | `api/model-settings-clear-api-key.patch.json` |
| MOD-005 | モデル追加/削除 | model row を追加し、削除確認する | 追加 row が保存 payload に含まれ、削除時は確認後に消える | `model/model-settings.valid.json` |
| MOD-006 | 既定のモデルの整合性（#499） | モデル ID の変更・削除、画像入力（Vision）対応の切替と、既定の Vision モデル / 既定のテキストモデルの関係を見る | ID の変更には既定が追従する。既定に選んだモデルの削除・Vision 対応のオフ・Vision 対応のモデルがない状態は、保存前にフィールドのエラーで止まり、API も 422 で拒否する。既定のテキストモデルも必須で、未選択の保存は「既定のテキストモデルを選択してください。」で止まる（#566） | `model/model-settings.invalid-default-model.json` |
| MOD-007 | VLM 入力モード | `auto` / `files_api` / `inline_image` を切替 | 選択値が保存 payload に反映される | `api/model-settings.patch.json` |
| MOD-008 | JSON テンプレート検証 | 不正 JSON template で保存 | 保存が失敗し、JSON object が必要と表示される | `model/model-settings.invalid-payload-template.json` |
| MOD-009 | response path 検証 | `/` で始まらない path で保存 | 保存が失敗し、JSON pointer 形式の修正が求められる | `model/model-settings.invalid-response-path.json` |
| MOD-010 | timeout / retry 境界 | timeout 0 / 601、max_retries 6 で保存 | バリデーションエラーになる | `model/model-settings.invalid-timeout-retries.json` |
| MOD-011 | embedding 次元固定 | embedding_dim を 1536 以外にする | 保存が失敗し、`1536` 固定であることが分かる | `model/model-settings.invalid-embedding-dim.json` |
| MOD-012 | Enterprise text test | text model のテストを実行 | success / failed が結果領域に表示される | `api/model-test-enterprise-text.request.json` |
| MOD-013 | Enterprise vision test | vision model のテストを実行 | vision_enabled に応じた target test が実行される | `api/model-test-enterprise-vision.request.json` |
| MOD-014 | Embedding test | embedding model のテストを実行 | 次元 `1536` が確認できる | `api/model-test-embedding.request.json` |
| MOD-015 | Rerank test | rerank model のテストを実行 | rerank success / failed の結果が表示される | `api/model-test-rerank.request.json` |

## 7. データベース

対象 API:

- `GET /api/settings/database`
- `PATCH /api/settings/database`
- `POST /api/settings/database/test`
- `POST /api/settings/database/wallet`
- `POST /api/settings/database/wallet/download`
- `GET /api/settings/database/adb`
- `POST /api/settings/database/adb/settings`
- `POST /api/settings/database/adb/start`
- `POST /api/settings/database/adb/stop`
- `GET /api/schema/owners`

| ID | 機能 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| DB-001 | 初期表示 | データベース設定ページを開く | user、dsn、wallet_dir、wallet 状態、接続状態、ADB 情報が表示される | `mock-responses/database-settings.success.json`、`mock-responses/adb-available.json` |
| DB-002 | 必須入力 | user / dsn を空にして保存 | 保存されず必須エラーが表示される | `api/database-settings.patch.json` |
| DB-003 | password 保存 | password を入力して保存 | password 本文は応答に出ず、has_password が true になる | `api/database-settings.patch.json` |
| DB-004 | password clear | clear_password を有効にして保存 | 保存後 has_password が false になる | `api/database-settings-clear-password.patch.json` |
| DB-005 | wallet password 保存/clear | wallet_password を保存し、次に clear_wallet_password | has_wallet_password の true/false が切り替わる | `api/database-settings.patch.json` |
| DB-006 | wallet upload 成功 | ZIP wallet を選択する | wallet_dir / wallet_uploaded / service list が更新される | `database/wallet-valid.zip` |
| DB-007 | wallet 拡張子エラー | `.txt` を選択する | クライアント側で拒否され、API が呼ばれない | `database/not-wallet.txt` |
| DB-008 | wallet 内容不足 | 必須ファイル不足 ZIP をアップロード | サーバー側エラーが表示される | `database/wallet-missing-required.zip` |
| DB-009 | wallet 自動取得 | ADB OCID と region がある状態で自動取得 | 成功時 wallet が更新され、失敗時は OCI/ADB 設定不足が表示される | `api/adb-settings.request.json` |
| DB-010 | DB 接続成功 | 接続テストを実行 | success、サービス名、vector column `VECTOR(1536, FLOAT32)` 等が表示される | `mock-responses/database-test.success.json` |
| DB-011 | DB 接続失敗 | 不正 dsn / password で接続テスト | failed、Oracle エラー要約、修正導線が表示される | `mock-responses/database-test.failed.json` |
| DB-012 | ADB 情報更新 | ADB 情報を再取得する | lifecycle_state、display_name、region、ocid のマスク表示が更新される | `mock-responses/adb-available.json` |
| DB-013 | ADB start | stopped 状態で start を実行 | start 中はボタン disabled、完了後 available 表示へ変わる | `mock-responses/adb-stopped.json` |
| DB-014 | ADB stop | available 状態で stop を実行 | 確認後に stop API が呼ばれ、状態更新が行われる | `mock-responses/adb-available.json` |

## 8. システムテーブル

対象 API:

- `GET /api/settings/database/system-tables`
- `POST /api/settings/database/system-tables/initialize`

| ID | 機能 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| ST-001 | ready 状態 | ページを開く | status `ready`、table count `27 / 27`、object count `51 / 51`、migration versions が表示され、詳細に TABLE / INDEX / SEQUENCE の全51件が表示される | `mock-responses/system-tables-ready.json` |
| ST-002 | missing 状態 | missing response を返す | 初期化 CTA が primary 表示される | `mock-responses/system-tables-missing.json` |
| ST-003 | partial / outdated 状態 | 一部 object 不足または version 遅れを返す | 管理オブジェクト詳細で不足した table / index / sequence、または不一致 version が分かる | `mock-responses/system-tables-partial.json`、`mock-responses/system-tables-outdated.json` |
| ST-004 | 初期化 | `recreate=false` で初期化を実行 | operation_state が running になり、poll 後 ready になる | `api/system-tables-initialize.request.json` |
| ST-005 | 再作成確認 | confirmation 未入力または誤入力で再作成 | API が呼ばれず、正確な確認文字列が求められる | - |
| ST-006 | 再作成成功 | `RECREATE_NL2SQL_SYSTEM_TABLES` を入力して実行 | `recreate=true` payload、完了後 ready | `api/system-tables-recreate.request.json` |
| ST-007 | 実行中 polling | running response を返す | 操作ボタン disabled、進行中メッセージ、再取得が継続する | `mock-responses/system-tables-running.json` |
| ST-008 | 失敗履歴 | failed response を返す | last_error と再試行導線が表示される | `mock-responses/system-tables-failed.json` |
| ST-009 | 実行権限なし | `settings.database.sql_execute` なしで開く | 状態は閲覧可、初期化/再作成は disabled または非表示 | `mock-responses/system-tables-ready.json` |
| ST-010 | DB 未接続 | GET が DB unavailable を返す | 画面全体が落ちず、DB 設定ページへの導線が表示される | `mock-responses/system-tables-failed.json` |

## 9. 外観と証明書

外観と証明書の画面の配色テーマは backend API を呼ばず、`localStorage` の `production-ready-nl2sql.ui` に保存される。

| ID | 機能 | 手順 | 期待結果 | テストデータ |
| --- | --- | --- | --- | --- |
| APP-001 | 初期表示 | 外観と証明書のページを開く | ライト / ダーク / システムの toggle が表示され、既定はライト | `appearance/theme-light.localstorage.json` |
| APP-002 | ダーク切替 | ダークを選択し reload | `html.dark` 相当のテーマが適用され、localStorage に `theme: "dark"` が残る | `appearance/theme-dark.localstorage.json` |
| APP-003 | システム切替 | system を選択し OS preference を mock | prefers-color-scheme に応じて light/dark が適用される | `appearance/theme-system.localstorage.json` |
| APP-004 | API 非依存 | Network tab を監視しながら切替 | theme 切替で settings API が呼ばれない | - |
| APP-005 | サイドバー状態との共存 | sidebarCollapsed / collapsedSections を含む保存値で開く | theme と sidebar 状態が同じ storage key 内で維持される | `appearance/theme-system.localstorage.json` |

## 10. 推奨 Playwright fixture 利用例

```ts
import { test, expect } from "@playwright/test";
import modelSettings from "../../docs/test-data/nl2sql-system-settings/mock-responses/model-settings.success.json";

test("モデル設定を表示できる", async ({ page }) => {
  await page.route("**/api/settings/model", async (route) => {
    await route.fulfill({ json: modelSettings });
  });

  await page.goto("/settings/model");
  await expect(page.getByRole("heading", { name: "モデル" })).toBeVisible();
  await expect(page.getByText("cohere.embed-v4.0")).toBeVisible();
});
```

## 11. 後片付け

- 実 DB / OCI 接続を使った場合は、検証後にダミー secret が runtime 設定へ残っていないことを確認する。
- `wallet-valid.zip` をアップロードした検証環境では、wallet 配置ディレクトリを検証用に限定する。
- ADB stop/start は共有環境では実行しない。mock または専用 ADB で検証する。
- システムテーブル再作成は既存データを削除または上書きする可能性があるため、必ず空の検証 DB で実行する。
