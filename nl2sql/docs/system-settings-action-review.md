# システム設定のボタン別レビュー（2026-09-11）

対象は OCI 認証、アップロード保存先、モデル、データベース、システムテーブル、外観と証明書の6画面。UI の event handler → API payload → backend の認可・状態更新・永続化を確認し、下表の action を desktop 1280×900 / mobile-375 で検証した。

## 発見した問題と修正

|Issue|問題|修正 PR|
|---|---|---|
|[#375](https://github.com/engchina/no.1-production-ready-nl2sql/issues/375)|OCI 初期取得失敗の黙認、処理中編集との競合、未保存離脱|[#377](https://github.com/engchina/no.1-production-ready-nl2sql/pull/377)|
|[#376](https://github.com/engchina/no.1-production-ready-nl2sql/issues/376)|OCI Object Storage / ADB 永続化失敗時にも runtime が変わる|[#379](https://github.com/engchina/no.1-production-ready-nl2sql/pull/379)|
|[#378](https://github.com/engchina/no.1-production-ready-nl2sql/issues/378)|アップロード保存先の背景再取得・保存応答が草稿を消す|[#381](https://github.com/engchina/no.1-production-ready-nl2sql/pull/381)|
|[#380](https://github.com/engchina/no.1-production-ready-nl2sql/issues/380)|モデルの旧テスト結果と変更後入力の混在、保存中編集の消失|[#384](https://github.com/engchina/no.1-production-ready-nl2sql/pull/384)|
|[#382](https://github.com/engchina/no.1-production-ready-nl2sql/issues/382)|Wallet と再取得が未保存 DB ユーザー名・パスワードを消す|[#385](https://github.com/engchina/no.1-production-ready-nl2sql/pull/385)|
|[#383](https://github.com/engchina/no.1-production-ready-nl2sql/issues/383)|状態変更・失敗後の古い実行確認語の再利用|[#387](https://github.com/engchina/no.1-production-ready-nl2sql/pull/387)|
|[#388](https://github.com/engchina/no.1-production-ready-nl2sql/issues/388)|OCI-only 権限では画面に必要な共有設定 GET が403|[#389](https://github.com/engchina/no.1-production-ready-nl2sql/pull/389)|

レビュー証跡・追加操作テストは [#386](https://github.com/engchina/no.1-production-ready-nl2sql/issues/386)。基線で検出したモデル保存ボタンの色比較の非決定的失敗は #380 で CSS transition 完了待ちを追加して解消した。

## OCI 認証

|操作|確認した挙動|
|---|---|
|初期読込・再試行|両設定の取得成功後に編集可能。失敗は明示して再試行。OCI-only 権限でも必要な GET は許可する。|
|config から反映|固定 config/profile を API に送り、返された認証 field を反映。読込中の別操作を禁止。|
|秘密鍵選択・ドラッグ＆ドロップ|即時 upload、形式不正を拒否、進行表示・成功通知。backend の PEM/サイズ検証も確認。|
|OCI 設定を保存|必須値・形式検証、PATCH payload、処理中の重複操作・編集禁止、成功後 baseline 更新。|
|接続テスト|保存済みサーバー設定が対象であることを説明。成功・診断失敗・API 失敗の共通表示を確認。|
|namespace 取得|指定 region で取得し read-only 欄へ反映。取得中は入力を固定。|
|Object Storage 保存|region/namespace のみを当該 API へ送信。失敗時 runtime 全体が不変。|

## アップロード保存先

|操作|確認した挙動|
|---|---|
|ローカル / OCI 切替|該当入力を表示し、保存前に必要項目を検証する。|
|path・region・bucket 入力|編集中 field は背景再取得で上書きしない。namespace は read-only。|
|保存・失敗後の再試行|遅延保存中は入力と再送を禁止。失敗時は入力とエラーを表示、再試行成功を反映。|
|OCI 認証設定を開く|未保存変更を確認。キャンセルで同画面に留まり、破棄承認後に遷移。|
|取得エラー時の再試行|標準 ErrorState を表示し取得を再実行。|

## モデル

|操作|確認した挙動|
|---|---|
|接続設定保存|接続カードの変更を保存し、他カードの草稿・非表示設定を保持。|
|API key 表示 / 非表示 / 削除|明示操作、保存済み状態の表示、削除指定を API payload と .env 永続化テストで確認。|
|モデル追加|編集行が追加され、入力した model_id / display_name を保存 payload に含める。|
|既定選択・Vision 切替|選択と boolean が保存に反映される。|
|モデル削除・確認キャンセル|キャンセルで保持、承認で草稿から除去。既定モデル削除時は残存モデルへ変更し、明示保存するまで API 更新しない。|
|モデルごとのテスト|text / vision の対象を送信。テスト中に編集・削除・保存・他テストを行わせず、編集後は旧結果を解除。|
|登録モデル保存|接続・Generative AI の未保存入力を保持する独立保存。|
|embedding / rerank のテスト・保存|対象 model_id のテストと診断表示、1536 次元の設定契約、Generative AI カード独立保存。|
|公式説明リンク|キーボードで別タブを開き未保存入力を維持。|
|読込失敗の再試行|未取得値を保存せず再試行で復帰。|

## データベース

|操作|確認した挙動|
|---|---|
|ADB 情報を再取得・保存|情報更新に続く Wallet 確認、各段階の進行・失敗表示。永続化失敗時は runtime を変更しない。|
|ADB 起動・停止|lifecycle に応じた実行可否と進行状態、成功・API 失敗。対象不明・情報エラー時は起動停止不可。|
|Wallet upload / drag&drop|形式検証・即時 upload。成功後も未保存 DB ユーザー名・password を保持。|
|Wallet 自動取得・再試行|必要条件を満たす未設定 Wallet をページ表示ごとに一度確認。失敗の保持とキーボードによる再試行、有効 Wallet 時は取得しない。|
|TLS / mTLS・サービス DSN|接続方式に応じた入力、サービス選択、必要項目の検証。|
|DB password 表示 / 非表示・削除|明示 reveal、重複取得防止、取得中表示、平文の不要な表示なし、削除 payload。|
|Wallet password 表示 / 非表示・削除|入力状態・保存済み状態の区別、削除指定の永続化。|
|DB 保存・接続テスト|送信中入力固定、草稿保持、保存成功後 baseline 更新、編集後の旧テスト結果解除、診断失敗表示。|
|Credential 状態再取得|失敗時に旧情報を明示し再試行を提供。未確認状態で mutation 不可。|
|Credential region・作成・再作成|region 変更時の再確認、実行開始時に確認を消費、処理中 region 固定、失敗後も再確認必須。backend の認可・CSRF・確認語検証を維持。|

## システムテーブル

|操作|確認した挙動|
|---|---|
|状態取得・再試行|read-only 取得。取得失敗は通知し、旧状態で DDL を実行させない。|
|作成・更新|権限確認、処理中の再送禁止、ready/no-op を含む結果通知。|
|詳細開閉・一覧スクロール|object/table/index 等の表示、desktop/mobile の行数と内部スクロール、空・欠損状態。|
|すべて再作成|一致する確認語を必須とし、開始・再取得・失敗で確認を解除する。backend の確認語・lease/lock・version 管理も回帰確認。|
|通知を閉じる|操作結果通知の閉じるボタン、折返し、キーボード・focus 表示。|

画面（`SystemTablesCard`）と backend の schema manager の骨格（lease・台帳・状態の分類・確認語の検証）は 3 製品共通になった（#325。画面は `@production-ready/system-settings`、backend は `pr_system_settings.system_schema`）。NL2SQL が持つのは migration ファイル・manifest・外部キーの検証・実行中の job の確認と、確認語 `RECREATE_NL2SQL_SYSTEM_TABLES`・製品固有の文言・所有者付きの object 名の表示だけ。

## 外観と証明書

|操作|確認した挙動|
|---|---|
|ライト・ダーク|配色 token と aria-pressed が変わり、再読込で選択を復元。|
|自動（OS 設定）|表示中の OS color scheme 変更にも追従する。|

## 検証と制約

- Playwright: `frontend/tests/e2e/nl2sql-system-settings.spec.ts`、`system-tables.spec.ts`、`database-unavailable-settings.spec.ts`。
- backend: `tests/test_security.py`、`test_settings_api.py`、`test_database_persistence.py`、`test_system_schema_manager.py` — 266 passed。
- `npm run build` — pass。
- `npm run test:logic` — 379 passed。
- `npm run test:e2e -- tests/e2e/nl2sql-system-settings.spec.ts tests/e2e/system-tables.spec.ts tests/e2e/database-unavailable-settings.spec.ts` — 146 passed（desktop / mobile-375、1.3分）。
- 6画面×2 viewport のスクリーンショットを取得。OCI / モデル desktop とモデル / DB mobile を目視し、幅・入力ラベル・focus 表示を確認。全画面の横幅・focus は Playwright でも確認。
- `git diff --check` — pass。
- UI 検証は隔離 API fixture、backend は一時ファイル・InMemory/モックを使用。実 OCI lifecycle、鍵、Wallet、DB credential、実 schema DDL の変更は行っていない。
- 共通未保存ガードは内部 link・画面内遷移・reload/tab close が対象。BrowserRouter の既存制約により browser back/forward の完全ブロックは含まない。秘密情報を新たに browser storage に保存しない。
