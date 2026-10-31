# RAG の UI/UX

> rag/AGENTS.md から移した詳細（#2276）。rag/AGENTS.md の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、AGENTS.md には規則の要約と案内だけを書く。

## UI/UX

- 静かで読み取りやすい情報密度、安定したナビゲーション、明確なフォームの状態を優先する（SaaS / 業務ツール）。
- サイドナビ（折りたたみ可）の主なセクション:
  - **ナレッジ構築**: 文書アップロード、文書インデックス、ナレッジベース、検索・回答プロファイル。
  - **AI 活用**: チャット、RAG 検索、実行履歴（チャット・RAG 検索の実行。#1890）。
  - **取込・検索・回答設定**: ファイル準備、文書解析、文書分割、検索インデックス、実体・別名索引、文書構造索引、検索方法、回答プロンプト、安全チェック、評価の基準。
  - **改善・運用**: 品質評価、フィードバック。
  - **セキュリティ設定**: 権限管理（ロールごとのメニュー権限・検索・回答プロファイル・ナレッジベース）。
  - **ユーザーとロール** / **システム設定**（OCI 認証、アップロード保存先、モデル、データベース、外観と証明書）: 3 製品で共通（platform の共有パッケージ）。
  - **運用設定**: システムテーブル管理（先頭）、HuggingFace 設定、サービス管理。サービス管理の工程の並び・名前・説明は、取込・検索・回答設定のナビの項目と各設定画面の説明から作る（`frontend/src/components/settings/service-stages.ts`）。
- 「AI 活用」の実行履歴（`/history`。`menu.history`）は、system_admin は全員の実行（利用者の列つき）、それ以外は自分の実行だけを、一覧・件数・詳細とも backend の SQL の条件で絞る（他人の実行の詳細は 404。#1889 の規則・`pr_system_settings.auth.visibility`。#1890）。
- ナビ・ルート・ページ内の操作は権限（`backend/app/security/permissions.py` のコード）で出し分ける。API は backend の manifest が既定で拒否するため、画面を足したら使う API を manifest にも登録する（完全性テストがある）。
- **「AI 活用」の下の機能のデータの見え方（3 製品共通。#1889）**: system_admin は全員のデータ、それ以外の利用者は自分のデータだけ（判定は `pr_system_settings.auth.visibility`。規則は [platform/docs/backend-standard.md](../../../platform/docs/backend-standard.md)「「AI 活用」の下の機能のデータの見え方」）。`rag.feedback.manage` などの管理権限は全員を見る理由にしない。
  - チャットの会話（`/api/chat/conversations*`）: 一覧・詳細・削除・停止・購読し直しは、system_admin なら全員の会話（context の `all_users_visible`。他の利用者の会話には `actor_display_name`、`owned=false`）、それ以外は自分の会話だけ（ほかの利用者の会話は 404）。会話は利用者を `user_id_hash` でしか持たないので、表示名は利用者の UUID を同じ hash にして引く（`app/security/user_names.py`）。
  - **新しい内容を足す操作は会話の持ち主だけ（#3244。#1928 を置き換え）**: 続きの質問（`POST …/messages/stream`。確認への回答・再実行・添付を含む）・名前の変更（`PATCH /api/chat/conversations/{id}`）・回答の評価（`POST /api/feedback` のチャットの評価）・会話への添付（`POST /api/chat-attachments` の `conversation_id`）は、system_admin でも他の利用者の会話には 403（`OTHERS_CONTENT_FORBIDDEN_MESSAGE`）。判定は応答の `owned` と同じ（`request_context.caller_owns`。持ち主の分からない古い会話（`user_id_hash` が NULL）には誰も足せない。platform の `can_add_user_content` と同じ）。会話の添付の一覧（`GET /api/chat-attachments?conversation_id=…`）は閲覧なので見える会話なら返す。system_admin の検索・回答プロファイルと KB の範囲で作った回答・引用が持ち主の会話に入るのを防ぐ。画面は `owned=false` の会話で入力欄・名前の変更・評価・再実行・確認への回答を出さず、閲覧だけの案内（3 製品共通の `ChatReadOnlyNotice`）を出す（停止・削除は出す。名前の変更は共通の `useChatHistoryActions` の `canRename` で行ごとに隠す）。
  - #3244 より前に system_admin が送った質問は消さず、`sent_by_owner=false` と `sender_display_name` を付け、画面は質問の上に「〈名前〉 が送信」を出す。
  - RAG 検索: 検索そのものは保存しない。生成した回答の記録（`/api/search/answers*`）は、本人と、system_admin と `rag.feedback.manage`（運用設定の回答の記録の管理と共用の API。#304）。
  - 実行履歴（#1890）は同じ関数で、system_admin は全員・それ以外は自分の実行だけにする。

- RAG 固有の画面の差分: [docs/frontend-messaging-spec.md](../../docs/frontend-messaging-spec.md)（文書詳細の失敗表示）、[docs/frontend-workspace-state-spec.md](../../docs/frontend-workspace-state-spec.md)（離脱ガードと作業状態の対象・保存 key）、[docs/frontend-page-archetypes-spec.md](../../docs/frontend-page-archetypes-spec.md)（ページの型 A〜D と、対象の操作の `RowActionMenu` / `ObjectActionBar` への割り当て・例外）。
- lint は `frontend/eslint.config.mjs` が `../../platform/docs/design-system/adherence.oxlintrc.json` を import する。
- `ToggleChip` をタブとして使っている箇所（ビューの切替）は `Tabs` に置き換え、絞り込みの箇所だけ `ToggleChip` に残す。
- `--font-mono`（ID・ログ・SQL の等幅書体）は共有の tokens が定義し、書体ファイルは `frontend/src/fonts.css` で `@fontsource/google-sans-code` を自前ホストする。
