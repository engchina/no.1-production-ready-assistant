# ページの型の割り当て（RAG の差分）

> ページの型（A〜D）と共有プリミティブの共通規約は platform の [UX 契約 page-archetypes.md](../../platform/docs/ux-contracts/page-archetypes.md)、対象の操作の置き場所は [buttons.md §5.1](../../platform/docs/ux-contracts/buttons.md#51-オブジェクト操作一覧行--詳細)が正本。RAG の離脱ガードと作業状態の保持の割り当ては [frontend-workspace-state-spec.md](./frontend-workspace-state-spec.md) に書く。
> 本書には RAG の各ページがどの型に属するかと、RAG 固有の補足・例外だけを書く（Issue #131）。システム設定（アップロード保存先 / モデル / データベース / 外観と証明書）は共有パッケージの画面なので対象外。

## 1. 割り当て

ルートの正本は `frontend/src/lib/routes.ts`（`APP_ROUTES`）と `frontend/src/App.tsx`。

| 型 | ページ（ルート） | 補足 |
|---|---|---|
| A. 一覧 → 全画面エディタ | 文書インデックス（`/file-list`）→ 文書詳細（`/documents/:id`）/ ナレッジベース（`/knowledge-bases`・作成 `?id=new`）→ ナレッジベース詳細（`/knowledge-bases/:id`）/ 検索・回答プロファイル（`/search-answer-profiles`） | 文書は一覧の名前のリンクから全画面の詳細へ移る（対象はパスの `:id`）。検索・回答プロファイルは同じルートの `?id=` を唯一の情報源にする全画面エディタ（§1.1）。ナレッジベースは検索・回答プロファイルと同じ構成のエディタで、詳細の URL だけ既存のパスの `:id` を保つ（§1.2。#555）。 |
| B. マスタ詳細の閲覧 | フィードバック（`/feedback`）/ 実行履歴（`/history` → `/history/:runId`。#1890） | フィードバックは共有の `ListDetailLayout layout="single"` で全幅の一覧から全幅の詳細へ切り替える（§4。#3635）。絞り込み・ページ・選んだ行は URL の検索パラメータ（選んだ行は `feedback`。共有の `useUrlSelection`）に持つ。実行履歴は Agent の実行履歴と同じ例外（UX 契約 page-archetypes.md B 型の「一覧 → 全幅の詳細」）で、長い回答・根拠・処理の経過を読むため一覧から全幅の詳細へ移る。詳細はパスの `:runId` を唯一の情報源にし、`一覧へ戻る` は一覧の検索・絞り込み・ページを保つ。一覧と詳細の形は 3 製品で共通（UX 契約 run-history.md。#1955）: 一覧は `RunHistoryToolbar` → `RunHistoryTable` → `OffsetPagination`、詳細はタブを使わず `RunDetailLayout`（概要 → 処理の経過〔タイムラインと処理の詳細。#2325〕→ 回答 → 根拠 → フィードバック）を縦に 1 枚。 |
| C. ツール / ワークフロー | 文書アップロード（`/upload`）/ RAG 検索（`/search`）/ チャット（`/chat`）/ 品質評価（`/evaluation`） | 入力 → 実行 → 結果。チャットの会話一覧（会話の履歴）は作業の切替のためのサイドバーで、マスタ詳細の一覧としては扱わない。既定で閉じ、チャットの上端の開閉ボタンで開く（lg 以上はチャットの左のパネル、lg 未満は `SideSheet`。#664）。 |
| D. ダッシュボード / 状態 | 設定の概要（`/settings/pipeline`）/ 取込・検索・回答設定の各ページ（`/settings/preprocess` `/settings/parser-adapters` `/settings/chunking` `/settings/vector-index` `/settings/retrieval` `/settings/prompts` `/settings/guardrail` `/settings/evaluation` `/settings/graph`）/ 運用設定（`/settings/huggingface` `/settings/services`）/ 文書の分類（`/document-categories`。階層の一覧。`page-archetypes.md` の例外。#2131） | 設定の単一フォームは「状態 + 最小の編集」として D 型に置く。 |

- 削除した設定のページ（根拠確認 `/settings/grounding`・回答スタイル `/settings/generation`・高度な検索 `/settings/agentic`。#595）の URL は、回答の設定が移った検索方法（`/settings/retrieval`）へ置き換えで移す（ブックマーク対策。`App.tsx` の `REMOVED_ANSWER_SETTINGS_ROUTES`）。移った先の権限は通常どおり判定し、検索方法の権限が無ければ権限なしの画面になる。

### 1.1 検索・回答プロファイルの全画面エディタ（`?id=`）

- `src/lib/editor-route.ts` の `useEditorRoute` が `?id=` を読む。なし = 一覧 / `new` = 新規 / `<id>` = その検索・回答プロファイルの編集。ほかの検索パラメータは残す。
- 一覧は `DataTable`。行の操作以外の領域のクリックと、先頭セルの名前のボタン（`{name} を編集`）でエディタを開く。アーカイブ済みの行は開かない。「新規作成」は `PageHeader` の primary。
- 開く・一覧へ戻るは履歴に積む（再読込・ブラウザの戻る / 進むで同じ対象が開く）。作成に成功したら作成した検索・回答プロファイルの `?id=` へ、エディタからアーカイブしたら一覧へ、どちらも `replace` で移る（戻るで空の新規フォームや消えた対象へ戻さない）。
- エディタの上部は パンくず（`検索・回答プロファイル › 名前`、共有 `Breadcrumbs`）+ `一覧へ戻る`（secondary）+ `保存する` / `作成する`（primary）。375px では共有 `PageHeader` の規則で `一覧へ戻る` が「その他の操作」に入る。対象の操作（アーカイブ）は「基本情報と検索・回答設定」の見出しの `ObjectActionBar`、知識パネル（Approved FAQ / 用語・同義語 / ドメインキーワード / 回答ルール / 業務ガイドのタブ。回答フローで使う順。#682 / #1237）はその下に積む。
- `?id=` の検索・回答プロファイルが無い（404）ときは「対象が見つかりません」と `一覧へ戻る` を出し、別の対象へ置き換えない。取得の失敗（404 以外）は再試行を出す。直接 URL で開いたアーカイブ済みの検索・回答プロファイルは警告を出し、保存を無効にする。
- 共通の部品（`EditorBreadcrumbs` / `RowTitleButton` / `MissingEditorTarget` / `EditorTargetState` / `EditorDraftNotice`）と、一覧 + 詳細の保存 key の前置き（`RAG_LIST_DETAIL_STORAGE_PREFIX`）は `src/components/layout/EntityLayout.tsx`、下書きと離脱の確認は `src/components/layout/use-entity-editor-draft.ts` の `useEntityEditorDraft`（ナレッジベースと共有。#555）。
- エディタの `PageHeader` は、編集のときに状態（`StatusBadge`）と件数・更新日時（`meta`）を出す。アーカイブ済みは警告を出し、保存を無効にしたうえで入力欄も読み取り専用・無効にする（入力しても保存できない欄を出さない。#555）。
- 一覧が空のときは、空の状態に「最初の検索・回答プロファイルを作成」を置く（ヘッダーの「新規作成」と同じ作成エディタへ）。

### 1.2 ナレッジベースの一覧 → 作成 / 詳細（#555）

検索・回答プロファイル（§1.1）と同じ構成・部品にそろえる。違いは、詳細の URL が既存のパスの `:id`（`/knowledge-bases/:id`）であることだけ。

- 一覧（`/knowledge-bases`）は `DataTable`。「新規作成」は `PageHeader` の primary（ナレッジベース管理の権限があるときだけ）。一覧の上に作成のフォームを常に出さない。行の操作以外の領域のクリックと、先頭セルの名前のリンクで詳細を開く（詳細は別の URL なので名前はリンクにし、新しいタブでも開ける。アーカイブ済みも閲覧のため開ける）。空のときは「最初のナレッジベースを作成」を置く。作成中の下書きがあれば「下書きを開く」を出す。
- 作成（`/knowledge-bases?id=new`）は `KnowledgeBaseEditor`。パンくず（`ナレッジベース › ナレッジベースを作成`）+ `一覧へ戻る`（secondary）+ `作成する`（primary）。作成に成功したら作成した詳細（`/knowledge-bases/:id`）へ `replace` で移る。管理の権限が無い利用者が `?id=new` を開いたときは一覧を出す。`?id=<id>` で開かれたら `/knowledge-bases/:id` へ置き換える。
- 詳細（`/knowledge-bases/:id`）も同じ `KnowledgeBaseEditor`。`PageHeader` に パンくず（`ナレッジベース › 名前`）・状態・件数（文書・索引済み・エラー）と更新日時・`一覧へ戻る`・`保存する`。本文は 基本情報（名前・説明。見出しに `ObjectActionBar`）→ 所属文書 → 関係情報 → 抽出する項目（#548）→ 構築フロー → 検索テスト の順（文書の追加 → 構築設定 → 検索で確認。#616）。名前・説明はその場の欄で編集し、ヘッダーの `保存する` で保存する（「編集」ボタンで表示と編集を切り替えない）。所属文書の追加・外すと抽出する項目の保存は、それぞれのカードの操作ですぐ反映する（ヘッダーの `保存する` の対象ではない）。
- 詳細からアーカイブしたら一覧へ `replace` で戻る。アーカイブ済みは警告を出し、名前・説明を読み取り専用にして保存を無効にする。
- 読み込み中・見つからない（404。再試行しない）・取得の失敗は、検索・回答プロファイルと同じ `EditorTargetState`（パンくず + `一覧へ戻る` の `PageHeader` の下に Skeleton / 「対象が見つかりません」/ 再試行）。

### 1.3 文書詳細（`/documents/:id`。#581）

見出しはナレッジベース・検索・回答プロファイルの詳細と同じ構成にする（`src/components/documents/DocumentDetailPage.tsx`）。URL と本文（`DocumentWorkspace`）は変えない。

- `PageHeader`: パンくず（`文書インデックス › ファイル名`、`EditorBreadcrumbs`）・タイトル（ファイル名）・状態（`StatusBadge`。本文と同じく `?recipe=` で選んだレシピの状態、無ければ文書の状態）・`一覧へ戻る`（secondary。375px では「その他の操作」）。本文のカードにはファイル名と状態を重ねない（`DocumentWorkspace` の `showTitle={false}`。アップロード直後の画面では今までどおりカードに出す）。
- 主な操作（処理の開始・承認・再試行・再処理、レシピの操作）は本文の工程の段のまま（§3。対象の操作ではなく工程を進める操作のため、ヘッダーへ上げない）。
- 読み込み中・見つからない（404。再試行しない）・取得の失敗は `EditorTargetState`（§1.2 と同じ）。取得済みの文書があれば、ポーリング中の一時的な失敗で本文を置き換えない。
- `一覧へ戻る` は `navigate()` で移るため、先に `confirmPendingLeave()` を通す（抽出確認の未保存の編集の確認）。パンくずのリンクは共有の離脱ガードが確認する。

## 2. 対象の操作（`EntityAction` / `RowActionMenu` / `ObjectActionBar`）

対象オブジェクトの操作は画面ごとに `EntityAction[]` を 1 回だけ定義し、一覧の行は `RowActionMenu`（1 行に 1 個）、詳細は `ObjectActionBar` に渡す。危険な操作は `tone: "danger"` にして、確定は `useConfirm` の確認ダイアログで行う。メニューのトリガーと詳細の操作のバーの名前は `common.objectActions.aria`（`{name} の操作`）、「その他の操作」は `common.objectActions.more`。

| 画面 | 対象 | 行（`RowActionMenu`） | 詳細（`ObjectActionBar`） | 定義の場所 |
|---|---|---|---|---|
| 文書インデックス | 文書 | ファイル準備を実行 / 再実行（`UPLOADED` / `ERROR` のみ）、削除（danger） | —（文書詳細は工程の操作が中心。§3） | `FileListClient.tsx` の `documentActions` |
| ナレッジベース / 詳細 | ナレッジベース | アーカイブ（danger。DEFAULT は理由付きで無効） | 詳細の「基本情報」の見出しに同じ定義（危険な操作だけなので「その他の操作」に入る）。名前・説明は欄でそのまま編集するため「編集」の操作は持たない（#555。#302 の「編集」を置き換えた） | `knowledge-bases/knowledge-base-actions.ts` の `useKnowledgeBaseActions` |
| ナレッジベース詳細 | 所属文書 | 外す（確認は warning。所属の行だけを消し、chunk へ波及しない） | — | `KnowledgeBaseDetailClient.tsx` |
| 検索・回答プロファイル | 検索・回答プロファイル | アーカイブ（danger。DEFAULT は理由付きで無効） | エディタの「基本情報と検索・回答設定」の見出しに同じ定義 | `SearchAnswerProfileManagementClient.tsx` の `useSearchAnswerProfileActions` |
| 検索・回答プロファイル（知識パネル） | 承認済み FAQ | 削除（danger。確認ダイアログを通す） | — | `ApprovedFaqManager.tsx` |
| 検索・回答プロファイル（用語・同義語 / 回答ルール。タブごと。#682） | 用語・同義語 / 回答ルール | —（行は選択専用。名前のボタンと行のクリックで編集フォームへ読み込む） | フォームの 保存 / 削除（danger。確認ダイアログを通す） | `RuntimeKnowledgeManager.tsx` |
| 検索・回答プロファイル（業務ガイド。#1237） | 業務ガイド / 公開の履歴 | 業務ガイドは —（行は選択専用。名前のボタンと行のクリックで一覧の下の編集フォームへ読み込む）。公開の履歴は行のメニューに「この版を見る」「この版に戻す」（確認ダイアログを通す） | 編集フォームの見出しの `ObjectActionBar` に アーカイブ（確認ダイアログを通す）/ アーカイブから戻す。フォーム末尾の操作行は 保存（primary）→ 検証 → 公開（確認ダイアログを通す）、反対の端に 閉じる | `SupportGuideManager.tsx` / `SupportGuideEditor.tsx` |
| フィードバック | 回答のフィードバック | —（行は選択専用） | Approved FAQ に登録（確認ダイアログを通す。同じ質問の FAQ は置き換える）、品質評価のケースを書き出す（評価リクエストの Excel の 1 行をダウンロードする。#2065）。詳細の見出しの下の行に置く。引用のフィードバックには出さない | `FeedbackClient.tsx` の `FeedbackPromotionActions` |
| サービス管理 | サービス（`deployable` の行） | ログを表示 / 閉じる、起動（一部異常のときだけ）、ビルド、削除（danger。確認ダイアログを通す）。行には別に状態に応じた起動 / 停止を 1 つだけ出す（§3.1） | — | `ServicesManagementClient.tsx` の `ServiceRow`（`servicePrimaryAction`） |
| 文書詳細（処理レシピ） | 選択中のレシピ | —（レシピのカードは選択専用） | 処理を開始 / 再開 / 再試行 / 再処理、レシピを削除（danger。最後の 1 件と処理中は無効） | `DocumentRecipeManager.tsx` の `recipeActions` |

- **選択の導線**：`編集` / `詳細` のような選ぶだけの操作はメニューに入れない。検索・回答プロファイル・用語 / ルール・フィードバックは、行の操作以外の領域のクリックと先頭セルの名前のボタンで開く / 選ぶ。同じ画面で一覧と選んだ対象を並べる場合（用語 / ルール・フィードバック）は、選んだ行を `aria-current` と背景で示す。
- **一括操作**：文書インデックスで行を 1 件以上選んでいる間と一括処理の間は、行の `RowActionMenu` を disabled にし、一括操作のバーへ集める。
- **3 層モデル**：操作の置き場所を変えただけで、文書レシピ / KB スコープ / Search Answer Profile の責務と API は変えない。

## 3. 例外（行に `RowActionMenu` 以外のボタンを残す画面）

理由のある例外だけを残す（#147 で検索・回答プロファイル・フィードバック・ランタイム知識・回答プロンプト（#595 で版の一覧ごと削除）・保存された回答は規約に揃えた。#158 でサービス管理を §3.1 の規則に揃え、例外から外した）。

| 画面 | 残しているボタン | 理由 |
|---|---|---|
| 文書詳細の工程操作 | 承認・ファイル準備の実行・工程の再試行・再処理 | 対象の操作ではなく工程を進める操作（buttons.md §3 の主操作）。C 型の工程の段として扱う。 |
| チャットの会話一覧 | 名前を変更（icon-only 1 個） | 会話一覧は作業の切替のサイドバーで、行に 1 個だけのその場の名前の編集。`RowActionMenu` へ移すと 1 手増えるため据え置く。 |

### 3.1 運用のコントロールパネル（サービス管理）：状態に応じた主操作 1 つ + `RowActionMenu`

サービス管理は、サービスごとの起動 / 停止をすぐ押せることが要件の運用のパネルなので、行の操作を次の形にそろえる（#158 で決定）。規約（buttons.md §5.1）の例外ではなく、この画面の型として扱う。

- **主操作は 1 つだけ**：行に常に表示するのは、状態から決まる起動 / 停止のどちらか 1 つのボタン（`servicePrimaryAction`）。稼働中・一部異常は「停止」、停止中・未設定・状態の取得中 / 失敗は「起動」。起動と停止を同時に並べない。見た目は `secondary` の `sm`（停止は `tone="danger"`）で、ページの `primary` は使わない。
- **副操作はメニュー 1 個**：ログ（表示 / 閉じる、journalctl）と再起動は、`EntityAction[]` にして行に 1 個の `RowActionMenu` へ入れる。Docker のイメージのビルド・コンテナの削除は #286 で無くした（systemd の unit を操作する）。
- **確認と無効**：停止は `useConfirm` の確認ダイアログ（danger）を通し、起動と再起動は確認なしで実行する。制御が無効（prod の可視化のみ）・状態の取得中 / 失敗のときは主操作を無効にし、理由をツールチップで示す。同じサービスの操作中は、そのサービスの他の操作を無効にする。操作中は主操作のボタン、またはメニューのトリガーにスピナーを出す。
- **推論サーバー（vLLM / SGLang）**：画面の行として出さない（GPU の解析エンジンは外部運用済みの API を文書解析設定で指定し、このリポジトリでは構築・起動しない。#270 / #286）。独立した行として出さず、この変更でも扱いを変えない。
- **レスポンシブ**：375px では状態の badge・主操作・メニューを名前の下に折り返し、ページの横スクロールを出さない。

## 4. 一覧 + 詳細と分割ペイン

共通のユーザー管理・ロール管理・権限管理（`@production-ready/system-settings` の `UserManagementPage` / `RoleManagementPage` / `RolePermissionsPage`）は共有の `ListDetailLayout` の一覧 + 詳細（#2282）。選択は URL の `?user=` / `?role=`（ロール管理の詳細の「権限管理で設定」は `?role=` を付けて権限管理を開く）、先頭の行を自動で選ばない。作成・編集は今までどおり一覧に代わる 1 枚のフォーム。製品のラッパー（`src/components/security/Security*Page.tsx`）は `listDetailStoragePrefix={RAG_LIST_DETAIL_STORAGE_PREFIX}`（`production-ready-rag.listDetail`。`EntityLayout.tsx`）を渡す。

フィードバックは共有の `ListDetailLayout layout="single"` を使い、すべての画面幅で一覧と全幅の詳細を切り替える（#3635）。保存 key の前置きは `RAG_LIST_DETAIL_STORAGE_PREFIX`（`production-ready-rag.listDetail`）、文言は `listDetail.*`。RAG の画面で `FixedSplitPane` を使うものは今は無い（入力と結果・比較の画面を足すときに使う）。

| 画面 | `id` | 補足 |
|---|---|---|
| フィードバック | `feedback` | 範囲の案内・条件のツールバー（`ListToolbar`。集計にも明細にも効く）・集計のパネルは一覧で全幅に出し、詳細の表示中は隠す。一覧の列は見出し「フィードバック明細」と並び順（`SelectField` の inline のラベル）→ 問題の概要（2 行まで）と補足 1 行（評価のバッジ・評価の日時・コメントの有無）の 1 列の表 → 列の下の `OffsetPagination`。理由・対象 / 送信元・検索・回答プロファイル・モデルは詳細の「内容」「実行情報」タブで見る。詳細の列は見出し・`ObjectActionBar`（Approved FAQ・品質評価のケース）・内容 / 根拠 / 実行情報のタブで、上端の行に `ListDetailPager`（今のページの前後）。先頭の行を自動で選ばない（未選択は「フィードバックを選んでください」）。すべての幅で 1 ペインを使い、選ぶと詳細だけ・左上の「一覧へ戻る」（`PageHeader` の `back`）で一覧へ戻り選んだ行へフォーカスを戻す。根拠タブは共通リストの最大高さ（base 17.5rem / md 28rem）で内部スクロールする。 |

チャットの会話一覧は作業の切替のサイドバーで、分割ペインとしては扱わない。既定で閉じ（多くの利用者は使わないので、チャットに面積を渡す。ChatGPT・Claude・Gemini・Microsoft Copilot と同じ型）、チャットの上端の行の開閉ボタン（「会話の履歴」）で開く。lg 以上は開くとチャットの左に 280px のパネルを置き（開閉は作業状態に残す）、lg 未満は左からの `SideSheet`（会話を選ぶ・Esc・外側・閉じるボタンで閉じ、開閉ボタンへフォーカスを戻す）。「新しい会話」と今の会話の名前は、履歴を閉じていてもチャットの上端の行に出す。会話を選ばずに送信すると、最初の送信で会話を作る（#664）。

## 5. 結果の表（検索・チャット。#1159）

NL2SQL のチャットの SQL の実行結果と Agent のツールの結果は、共有の結果の表の部品（#1154。要約の 1 行・表頭固定と表の中のスクロールのプレビュー・打ち切りの明示・「すべての行を見る」・CSV・NULL の区別）で出す。RAG の検索（`/search`）・チャット（`/chat`）には、いまこの部品で出す結果が無いので使わない（#1159 で調べた）。

- **回答の本文**：回答エンジン（`rag_engine` の `generation/grounded.py`）が LLM の構造化出力（`summary` と items）から本文をシステムで組み立てる。LLM には「見出し・番号・出典表記・Markdown は書かない」と指示し、本文は要約の段落・節の見出し・「・」/「N. 」の説明・「根拠：」の行だけになる（画面は `components/search/AnswerText.tsx` が節・説明・根拠に分けて出す）。画面説明の「表の行:」「N 行目:」の行は回答の前に除く。Markdown の表は本文に出ないので、Markdown の表の描画も足さない。
- **API**：検索・チャットの応答（`SearchResponse` / `ChatMessage`）は回答の文字列・根拠のチャンク・診断だけで、列と行を持つ結果を返さない。
- **根拠**：表のチャンク（`content_kind = table`）も根拠のカード（`CitationCard.tsx`）では本文の 3 行の抜粋にし、表の全体は「引用箇所を表示」（右のサイドパネル `CitationSourceSheet`。#2360）で元の文書の表をその位置（`table_id`・セル）で見せる。根拠を行と列の表に組み直さない（元の文書の体裁と位置が根拠の確認に要るため）。
- **対象外**：文書の抽出の表を編集するグリッド（`ReviewTextEditor.tsx`）は、元の文書の表を再現して編集する `<table>` の例外（AGENTS.md）で、検索・回答の結果ではない。
- **足すときの規則**：検索・チャットに列と行の結果を返す経路（構造化データの抽出の結果を一覧で返す回答など）を足すときは、表を手書きせず共有の結果の表の部品で出す（NL2SQL・Agent と同じ見た目と振る舞い）。行数が多い・列が広いときの表の中のスクロール・「すべての行を見る」・CSV は部品が持つ。

## 6. 検証

- Playwright：`e2e/document-delete.spec.ts`（行のメニューの Enter で開く / Esc で閉じてトリガーへフォーカスを戻す、矢印キー、削除の確認のキャンセルと確定、一括選択中の disabled）、`e2e/knowledge-bases.spec.ts`（「新規作成」→ `?id=new` の作成 → 詳細への `replace`、行のクリック、詳細の `PageHeader`（パンくず・状態・件数・一覧へ戻る・保存する）と 1920 / 1280 / 375px の左端、`?id=<id>` の置き換え・存在しない詳細、作成と詳細の未保存の確認と下書き、空の状態の作成の入口、アーカイブ済みの読み取り専用、行のメニューからのアーカイブ・所属から外す、詳細の `ObjectActionBar` の「その他の操作」とフォーカスの戻り・アーカイブ後の一覧への `replace`、DEFAULT の無効理由）、`e2e/knowledge-base-ops.spec.ts`（名前・説明の編集と同名の理由、DEFAULT・説明が空の KB の保存）、`e2e/search-answer-profiles.spec.ts`（行のクリック / 名前のボタンで `?id=` のエディタを開く、再読込・戻る / 進む、パンくず、未保存の変更の確認と対象ごとの下書き、存在しない `?id=`、作成と アーカイブ の `replace`、DEFAULT の無効理由）、`e2e/feedback.spec.ts`（行のクリック / 名前のボタンでの選択と `aria-current`、全幅の一覧と詳細、desktop / 375px の切替・詳細へのフォーカス・根拠の最大高さ、URL からの復元）、`e2e/answer-knowledge.spec.ts`（用語・同義語の行の選択と削除の確認）、`e2e/search-answers.spec.ts`（チャットの「処理の詳細」の保存された回答に「その他の操作」を出さない。#2325）、`e2e/services-management.spec.ts`（各行が状態に応じた主操作 1 つ + メニューのトリガー 1 つだけ、起動 / 停止で主操作が切り替わる、メニューからのログ・ビルド・削除の確認、制御無効時の無効、Esc でトリガーへフォーカスが戻る、375px で横スクロールなし）。いずれも desktop / mobile-375 の両 project で動く。


### 文書解析と文書詳細の表示（#3608〜#3614）

- 抽出項目の定義は表形式の編集行として全幅を使い、名前と値の型は共通の幅の段、説明は残りの幅に置く。375px は縦に並べる。通常の単一入力フォームの最大幅の対象外とする。
- 章節ナビゲーションの表示と編集の一覧は共通の高さ上限で内部スクロールする。説明と操作は一覧の外に置く。
- ページ画像のプレビューでは、現在ページを本文・構造化要素・Chunk / Citation の表示へ伝える。跨頁 chunk はページ範囲で一致させ、ページ不明の項目を他のページへ割り当てない。ページを持たない原本や iframe にフォールバックした場合は文書全体の表示を保つ。章節の目次と全文の書き出しは文書全体のまま。
- 章節規則の専用の候補確認は廃止し、規則の方式と保存、文書詳細の章節表示と編集は維持する。
- 方式の欄は lg、展開した選択肢は長いときに折り返す。章節の要約は実効の対象数上限を読み取り専用で示し、モデル呼出回数の上限とは区別する。

## プロファイルの知識のページング（#3719）

登録済みQ&A・用語と同義語・確認と回答ルール・業務ガイドの4一覧は、空・単一ページでもページングできる一覧であることを示すため、`PagedDataTable` の `showSinglePagePagination` を指定する。範囲とページ番号を常に表示し、移動先のない前後の操作は無効にする。表示件数の選択と複数ページの操作は共有部品の規則に従う。
