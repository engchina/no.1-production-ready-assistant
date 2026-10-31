# ページ遷移と作業状態の仕様（RAG）

正本の原則は platform の [UX 契約 workspace-state.md](../../platform/docs/ux-contracts/workspace-state.md)。ここには RAG での割り当て（どの画面・どの field を保存するか、namespace・期限、検証の spec）だけを書く（Issue #132）。

## 未保存変更の離脱ガード

共有パッケージ `@production-ready/system-settings` の `useSettingsDraftGuard` / `useUnsavedChangesGuard` を、`src/lib/leave-guard.ts` の `useLeaveGuard` / `useCustomLeaveGuard` 経由で使う。dirty のときだけ、内部リンク（サイドナビを含む）・再読込・タブを閉じる操作を確認する。`navigate()` で移動するログアウトは、先に `confirmPendingLeave()` で同じ確認を通す。保存に成功したら、サーバーの値（または保存後の入力）を基準に dirty を判定し直す。

| 画面 | dirty の判定 |
|---|---|
| 取込・検索・回答設定（ファイル準備 / 文書解析 / 文書分割 / 検索インデックス / 検索方法（回答の検索と生成・回答記録の保存期間・質問履歴） / 回答プロンプト / 安全チェック / 品質評価 / 文書構造索引の構築） | フォームとサーバーの設定値の差分 |
| HuggingFace 設定 | endpoint の差分、token の入力、token の削除指定（token は保存しない） |
| システム設定（OCI 認証 / アップロード保存先 / モデル / データベース） | 共有パッケージの画面が持つ判定。RAG は `draftGuardMessages` で文言を渡す |
| 検索・回答プロファイル（作成・編集のエディタ） | 名前・説明・設定の差分（KB は集合として比べる）。下書きはこのタブに残るため、確認は「移動する」。パンくず・サイドナビに加え、エディタの `一覧へ戻る` も同じ確認を通す |
| 検索・回答プロファイル（ドメインキーワード / 承認済み FAQ の追加入力 / ランタイム知識） | 保存済みの値、または読み込んだ行との差分 |
| 検索・回答プロファイル（業務ガイドの編集・取り込みの入力。#1237） | 読み込んだ下書きとの差分（内容に変えた形で比べる）。別のガイドへ移る・閉じる・知識のタブを離れる前も同じ確認を出す |
| ナレッジベース（作成の画面 `?id=new`・詳細の基本情報） | 名前・説明の差分（前後の空白は除いて比べる）。検索・回答プロファイルと同じ部品（`useEntityEditorDraft`）で、下書きはこのタブに残るため確認は「移動する」。パンくず・サイドナビに加え、`一覧へ戻る` も同じ確認を通す（#555） |
| 文書詳細（KB 所属 / 分類と有効期間 / 版（#1248） / レシピの処理設定 / 抽出確認の編集） | 集合としての所属の差分、分類と有効期間・選んだ新しい版と保存値の差分、レシピ設定の差分、抽出確認の未保存編集（抽出確認は画面固有の文言） |

ブラウザの戻る / 進む（`popstate`）も、data router の root（`main.tsx`）に 1 つだけ置いた共有の `UnsavedChangesBlocker` で確認する（#138 / #586）。1 画面に離脱の確認が複数あっても（検索・回答プロファイルのエディタと知識の節など）、どのフォームが未保存でも確認する。キャンセルすると URL と入力が残る。

## 作業状態の保持

`src/lib/workspace-state.ts` の `useWorkspaceState` が、同じタブの `sessionStorage` に allowlist（`WorkspaceField`）の field だけを保存する。

- namespace：`production-ready-rag.workspace.v1:`（field ごとに `<namespace><field>[:<scope>]`）
- 期限：最終保存から 8 時間。1 field の JSON は 200,000 文字まで
- ユーザーの結び付け：`AuthProvider` が今のユーザー ID を `…owner` に記録し、変わったら namespace 配下を消す。logout でも消す
- storage が使えない・上限を超えた場合は初期値で動き、初期値から変わった入力は `beforeunload` で保護する

| 画面 | 保存する内容 | 保存方法 |
|---|---|---|
| RAG 検索 | 質問、対象の検索・回答プロファイル、検索方式、詳細条件（文書の分類と基準日・抽出項目の値の条件（#549）・候補取得数・Rerank 採用数・開閉）、「LLM で回答を生成する」のオン / オフ（#649） | sessionStorage |
| チャット | 選択中の検索・回答プロファイル、選択中の会話、入力中のメッセージ、会話の一覧のページ、lg 以上の会話の履歴のパネルの開閉（`chat.historyOpen`、既定は閉じる。#664） | sessionStorage（URL の `search_answer_profile_id` / `conversation_id` を優先）。lg 未満の会話の履歴のシート（モーダル）の開閉は保存しない（戻ったとき・再読込で画面を塞がない） |
| 検索・回答プロファイル | 絞り込み・検索・ページ・表示件数（#1891） | sessionStorage |
| 検索・回答プロファイル | 編集対象 | URL の `?id=`（なし / `new` / `<id>`。[frontend-page-archetypes-spec.md §1.1](./frontend-page-archetypes-spec.md)）。#132 の `searchAnswerProfiles.view.editingId` は使わず、保存値に残っていても読み捨てる |
| 検索・回答プロファイル | 作成 / 編集の未保存の下書き | sessionStorage の `searchAnswerProfiles.draft:<?id= の値>`（`new` または検索・回答プロファイルの ID）。同じ対象のエディタを開き直すと復元する。新規の下書きは一覧の「下書きを開く」から再開する。`変更を元に戻す`・保存の成功で消す |
| 文書インデックス | 状態の絞り込み・KB の絞り込み・検索・ページ・表示件数（#1891） | sessionStorage |
| ナレッジベース | 状態の絞り込み・検索・ページ・表示件数（#1891） | sessionStorage |
| ナレッジベースの所属文書 | 検索・ページ（ナレッジベースごと）・表示件数（別のナレッジベースでも保つ。#1891）（`knowledgeBases.documentsPage`） | sessionStorage |
| 検索・回答プロファイルの業務ガイド・用語 / ルール・類似問、業務ガイドの公開の履歴、品質評価のケースごとの結果（クライアント側で全件を持つ一覧） | ページと表示件数（`supportGuides.listPaging` / `runtimeKnowledge.listPaging` / `approvedFaqs.listPaging` / `supportGuides.revisionsPaging` / `evaluation.casesPaging` / `evaluation.previewPaging`。対象（検索・回答プロファイル・絞り込み・評価の結果）が変わったら 1 ページ目から、表示件数は保つ。`lib/list-paging.ts` の `useClientListPaging`。#1891） | sessionStorage |
| 文書の分類（#2141） | 選んだ大分類・中分類（URL の `?large=` / `?middle=`。共有・再読込で同じ位置を開く。一覧に無い ID は選んでいないものとして扱う）と、検索の結果のページと表示件数（`documentCategories.searchPaging`。検索語が変わったら 1 ページ目から） | URL / sessionStorage |
| 検索・回答プロファイルの業務ガイドの一覧の表示（アーカイブを除く / 含む） | `supportGuides.includeArchived`（#1933） | sessionStorage |
| ナレッジベース | 作成 / 詳細の名前・説明の未保存の下書き | sessionStorage の `knowledgeBases.draft:<new または KB の ID>`。同じ対象を開き直すと復元する。新規の下書きは一覧の「下書きを開く」から再開する。`変更を元に戻す`・保存の成功で消す（#555） |
| 品質評価 | 検索・回答プロファイル（`evaluation.profileId`）、実行時の主条件の名前（`evaluation.runProfileName`）、結果の出力に使う実行時の入力（`evaluation.runInput`。保存上限は取込と同じ）、検証した評価リクエストの Excel の結果（ケース・プレビュー・ファイル名。`evaluation.import`。200,000 文字を超えると保存しない。選んだファイルそのものはメモリだけ）、読み込んだケースのページ（`evaluation.previewPaging`）、評価の job id（`evaluation.runJobId`。戻ったらこの id でサーバーの状態を確かめる。#390）と、その評価のケースの見出し（質問・期待する根拠。`evaluation.runCaseLabels`。#2065） | sessionStorage |
| 品質評価 › 判定の校正（#3578） | 集合一覧のページ番号と表示件数（`evaluation.calibration.setsPaging`。既定 25 件）、選んだ記入の集合（`evaluation.calibration.labelSetId`）、手順のタブ（`evaluation.calibration.step`）、記入の観点と状態の絞り込み（`evaluation.calibration.aspect`・`evaluation.calibration.rowFilter`）、校正の job id（`evaluation.calibration.jobId`。戻ったらこの id でサーバーの状態を確かめる）。タブ（評価の実行 / 判定の校正）は URL の `?tab=calibration` | sessionStorage |
| 実行履歴（#1890・#1955） | 検索・状態・種類・期間・実行者の絞り込み・ページ・表示件数・最後に開いた行（`history.view`。#1891）。詳細はパスの `:runId` | sessionStorage |
| フィードバック | 期間・絞り込み・検索・並べ替え・ページ・表示件数（`size`。10 / 25 / 50 / 100、既定 25。#1891・#1952）・選んだ行（`feedback`） | URL の検索パラメータ。サイドナビでパラメータなしの URL に戻ったときは `RememberedSearchParams` が直前の URL を復元 |

保存しないもの：回答・引用・評価結果などのサーバー応答、結果行、秘密情報（token・API key）、文書インデックスの行の選択（一括削除につながるため、ページを離れたら解除）、確認ダイアログと確認語。戻っただけで検索・チャット・評価を送り直さない。

復元した対象（検索・回答プロファイル・KB の絞り込み）が一覧に無くなった場合は、その選択だけを外し、別の対象へ置き換えない。URL の `?id=` の検索・回答プロファイルが無い場合は「対象が見つかりません」と一覧へ戻る導線を出し、別の検索・回答プロファイルを開かない。チャットの会話が取得できない場合は、選択を残してエラーと再試行を示す。

## 検証

- Playwright：`e2e/workspace-state.spec.ts`（desktop / mobile-375）。離脱の確認とキャンセル・破棄、dirty でないときの自由な移動、`beforeunload`、保存後の基準更新、検索・回答プロファイルの新規の下書きの一覧からの再開（対象ごとの下書きと `?id=` の戻る / 進むは `e2e/search-answer-profiles.spec.ts`、ナレッジベースの作成・詳細の下書きは `e2e/knowledge-bases.spec.ts`）、検索・チャット・文書インデックスのページ往復と再読込、フィードバックの URL の復元
- Vitest：`src/lib/workspace-state.test.ts`（期限・型不一致・上限・storage 無効・ユーザー切替・namespace 外を消さないこと）
