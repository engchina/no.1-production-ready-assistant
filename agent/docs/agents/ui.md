# Agent の UI/UX

> agent/AGENTS.md から移した詳細（#2276）。agent/AGENTS.md の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、AGENTS.md には規則の要約と案内だけを書く。

## UI/UX

- ナビは上に一般の利用者の画面、下に管理者の画面を置く（RAG / NL2SQL と同じ）。**AI 活用**: チャット / 実行履歴 / 承認 → **Agent 構築**: 業務 Agent / スキル → **改善・運用**: 品質評価 / フィードバック / 利用状況 / 監査ログ → セキュリティ設定 → ユーザーとロール → 運用設定 → システム設定。既定の入口はチャット。
  - Tools を独立ナビに戻さない。ツール権限はナビに出さない。Planner・Memory・Command Policy・Runtime Safety の画面とコマンド実行ツール（`sandbox_command_run`）は作り直さない（#756 で削除）。
  - **運用設定**: システムテーブル（先頭）/ 実行環境（組み込み Runtime の状態）/ MCP 接続。**システム設定**: OCI 認証 / アップロード保存先 / モデル / データベース（3 製品で共通。platform の共有パッケージ）。
- 画面・ナビの文言に「Control Plane」「Runtime」のような基盤の用語を使わない（識別子・URL・権限コードは変えない）。用語は Run → 「実行」、Skill → 「スキル」、組み込み Runtime → 「実行環境」（表は docs/frontend-page-archetypes-spec.md §1）。
- チャットは業務利用者の入口で、1 往復が 1 Run。同じ会話（`thread_id`）の前の質問と回答と使ったツールの名前を、組み込み Runtime が 3 製品共通の予算（`PLATFORM_CHAT_HISTORY_*`。backend-standard.md「チャットの会話の履歴を LLM に渡す量」）で未信頼のデータとしてモデルへ渡す。会話は作った利用者（持ち主）だけが読み、続けられる（同じ Agent に限る）。system_admin は全員の会話を見られるが、他の利用者の会話は閲覧だけ（#1889・#3244）。
- 「AI 活用」（チャット・実行履歴・承認）は、system_admin は全員の Run・会話、それ以外は自分の Run・会話だけ（3 製品共通。ほかの利用者の Run は 404。承認の権限を持つ人も自分が実行した Run の承認だけ。#1889。docs/security-rbac.md §4.1）。
  - 実行履歴の行は実行した人と会話の持ち主の両方に出る（自分の会話で system_admin が実行した Run も出す）。行・詳細・会話の Run に `executed_by_me`・`executor_display_name`・`can_decide`（詳細と会話は `owned_by_me`・`conversation_owner_display_name` も）を返し、`GET /runs?executor=all|mine|others` で絞る。画面は「実行者」の列・「〈名前〉 が実行」・実行者の絞り込み・詳細の「実行者」「会話の持ち主」（#1954）。
  - 承認・再開・Agent 自身の確認への回答は実行した人と system_admin だけ（持ち主でも system_admin が実行した Run は 404）。持ち主の画面は待っている相手（「システム管理者（〈名前〉）の承認・回答を待っています」）を出して操作を出さず、チャットの「自分で質問し直す」で止めて同じ質問を持ち主の権限で送り直せる（#1954。#3244 より前に system_admin が持ち主の会話で実行した Run）。
  - system_admin が他の利用者のデータにできるのは閲覧と運用の操作（停止・取消・会話の削除・承認の判断・再開・確認をやめる。再実行は system_admin 自身の新しい会話）だけ（#3244。#1928 を置き換え）。続きの質問・回答前の確認への答え・評価・会話の名前の変更は持ち主だけで、system_admin でも 403（外部の MCP も `OTHERS_CONTENT_FORBIDDEN`）。画面は他の利用者の会話を開くと入力欄の代わりに閲覧だけの案内（3 製品共通の `ChatReadOnlyNotice`。「停止」を含む）を出し、確認のカード・評価と、会話の履歴の行の名前の変更（共通の `useChatHistoryActions` の `canRename`）を出さない。会話の添付の一覧は閲覧なので返し、会話への添付は 403（platform の添付の API）。#3244 より前に system_admin が送った往復は、送った人の表示名（`sender_display_names`）を添えて出す。詳細は docs/security-rbac.md §4.1。
- ログインと権限: 共通認証（`PLATFORM_AUTH_MODE=authenticated`）。製品固有の権限管理は「セキュリティ設定」。メニュー権限は `menu.*`、実データの閲覧・操作は capability（`agent.runs.view` / `agent.runs.operate` / `agent.approvals.decide` / `agent.audit.view` / `agent.admin`）。権限カタログと API の manifest の正本は `backend/app/security/permissions.py`、説明は [docs/security-rbac.md](../../docs/security-rbac.md)。
- 業務 Agent の編集画面は指示・スキル・モデル・データの範囲を選ぶ（範囲の強制は組み込み Runtime の tool handler。モデルの指示に頼らない。docs/agent-control-plane-design.md §2）。実行（Run）は業務 Agent と目標だけで作る（実行先の選択は無い）。
- 空、読込、エラー、モデル未設定（組み込み Runtime が実行できない）、承認待ちを明示する。
- 各ページの型（A〜D）の割り当て、離脱ガードの対象画面、作業状態として残す field は [docs/frontend-page-archetypes-spec.md](../../docs/frontend-page-archetypes-spec.md) に書く。
- サイドバーのアカウント領域（ユーザー・ロール・ログアウト・テーマ切替）は `SidebarAccountFooter` を `Sidebar` の `footer` に渡す。ダークテーマ（`data-theme="dark"`）に追従するため、色の直書きを残さない。
- lint は `frontend/eslint.config.js` が `../../platform/docs/design-system/adherence.oxlintrc.json` を import する。
- Playwright は `desktop` と `mobile-375`（Pixel 5、375×812、タッチ）の 2 project。spec の中で viewport を回しているテストと、画面幅に関係しない spec は `mobile-375` では実行しない（`playwright.config.ts`）。PR の CI の `mobile-375` は、さらに `@responsive` の tag の test だけ（全件は nightly。root の [docs/agents/ui.md](../../../docs/agents/ui.md)「UI 変更の検証」）。
