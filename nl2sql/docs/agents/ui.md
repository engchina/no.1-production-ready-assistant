# NL2SQL の UI/UX と用語の詳細

> nl2sql/AGENTS.md から移した詳細（#2276）。nl2sql/AGENTS.md の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、AGENTS.md には規則の要約と案内だけを書く。

## 用語

- 利用者が入力する自然言語（SQL 生成・チャット・AI 要件確認の入力など）は **`質問`**（root「言語」）。`クエリ` は SQL・技術の概念（サブクエリなど）にだけ使い、旧称の「検索クエリ」も使わない。`frontend/tests/i18n-key-contract.test.ts` が辞書の文言を検査する。
- 利用者の自然言語の入力とその実行を **「検索」と呼ばない**（#1189）。入力は「質問」、それによる SQL の生成と実行は「SQL を生成して実行」（ボタン）・「SQL の生成と実行」（説明の文）、その結果は「実行結果」と呼ぶ（旧称「NL2SQL 検索」「検索実行」「検索結果」「検索画面」は使わない）。
  - 「検索」のままでよいもの: 一覧の絞り込みの検索欄（`SearchField`・「検索語をクリア」）、選択欄の候補の検索、類似検索・ベクトル検索・埋め込み検索・オントロジーのサーバ検索などの技術の概念、利用者の入力の解析で受け付ける語（`検索条件` のラベルなど）。
  - オントロジーの Markdown の関係の行の `検索利用: 利用可 / 利用不可`（`ontology_build.py`）は変えない。生成した Markdown の hash（`markdown_hash`）で変更を判定するため、文言を変えると既存のすべての業務プロファイルが「変更あり」になり再公開が要る（#1197）。
  - 同じ i18n-key-contract のテストが、SQL 生成の画面の主な文言・旧称の言い回し・AI 要件確認（`sql_requirement.py`）の文言を検査する。

## UI/UX

- 日本語 UI のレイアウト（行高・禁則・フォント）を検証する。
- UI/UX の機能追加・修正では、Playwright のテスト（e2e / interaction / 必要に応じた visual check）を追加・更新し、完了前に実行する。主要導線・レスポンシブ・キーボード操作・アクセシビリティの破綻が無いことを確かめる。
- 情報設計・画面構成・ナビ導線・状態遷移・文言設計は、`frontend/src` と `src/lib/i18n` / `src/lib/routes` を正本として整備する（見た目と共有部品は platform の `docs/design-system/`）。
- サイドナビ（折りたたみ可）の構成の正本は `frontend/src/components/layout/nav-config.ts`:
  - **AI 活用** / **データ準備** / **改善・運用**: NL2SQL の業務機能。
  - **セキュリティ設定**: 権限管理（ロールごとの機能権限・業務プロファイル利用権限）/ Deep Data Security。
  - **ユーザーとロール** / **システム設定**（OCI 認証 / アップロード保存先 / モデル / データベース / 外観と証明書）: 3 製品で共通（platform の共有パッケージ）。
  - **運用設定**: システムテーブル管理 / Select AI Credential（データベース設定から分けた）。
- サイドナビのラベルは日本語第一。正式名が長い項目だけ短縮形（`sidebarLabelKey`）で表示し（例: `システムテーブル管理` → `システムテーブル`）、**ページタイトル / `aria-label` は正式名を維持**する（`nav.*` と `nav.*.sidebar` の二段管理）。
- ページネーション・確認ダイアログ・トースト・一括選択（全選択 / 選択件数）は共通の部品を使い、データ取得・通知・ページングは hooks に集約する。
- NL2SQL 固有の画面の差分は [docs/frontend-messaging-spec.md](../../docs/frontend-messaging-spec.md) / [docs/frontend-button-spec.md](../../docs/frontend-button-spec.md) / [docs/frontend-page-archetypes-spec.md](../../docs/frontend-page-archetypes-spec.md) に書く。メッセージは UX 契約の 6 チャネル / 4 トーン / i18n 規約に従う。アクションは共通の `Button` で、類似機能は同じ size・variant にする。

### lint

- `frontend/.oxlintrc.json` は platform の adherence 設定を `extends` で相対パス参照する（`../../platform/…`。ルールとプラグインはコピーしない）。CI の `NL2SQL / Frontend` は `npm run lint`（`oxlint --deny-warnings`）を実行する。
- アプリ固有のセレクタは、platform のプラグインを別名で再公開した `frontend/lint/app-design-system-plugin.mjs` の `nl2sql-design-system/restricted-syntax` に書く（今は無い）。`design-system/restricted-syntax` / `no-restricted-imports` を同じ名前で上書きしない。
- グラフ座標など px が正しい幾何値は `// oxlint-disable-next-line <rule>` と理由で局所的に除外する。

### NL2SQL 固有の部品

- ドメイン固有として残すもの: `WorkflowProgressStrip`、オントロジー / 関係グラフ（配色は `--color-graph-*`。種別 = 塗り、状態 = 線）。
- `--font-mono`（Google Sans Code）は共有の tokens が定義し、書体ファイルは `@fontsource/google-sans-code` で自前ホストする。
