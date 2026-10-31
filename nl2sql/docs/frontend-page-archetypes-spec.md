# ページの型の割り当て（NL2SQL の差分）

> ページの型（A〜D）と共有プリミティブの共通規約は platform の [UX 契約 page-archetypes.md](../../platform/docs/ux-contracts/page-archetypes.md) が正本。
> 本書には NL2SQL の各ページがどの型に属するかと、NL2SQL 固有の補足だけを書く。

## 1. 割り当て

### A. 一覧 → 全画面エディタ

`ProfileManagementPage`（業務プロファイル）を参照実装とする。URL の検索パラメータは `?id=` / `?profile=` など。

- **割当**: 業務プロファイル / 用語・同義語 / 検証用サンプルデータ / コメント管理 / アノテーション管理 / ドメイン管理 / 質問分類モデル管理。テーブルの管理・ビューの管理の作成・取込（`?` を持たない画面内の切り替え）もこの型の置き方（`PageHeader` の `back`）にする。
- 旧タブ（list/create/import 等）は **一覧上のアクション** か **エディタ内の節** へ平坦化。破壊的操作は `useConfirm` + `ADMIN_EXECUTE` ゲートへ集約。

### B. マスタ詳細の閲覧

- **割当**: テーブルの管理 / ビューの管理（共有の `ListDetailLayout`。#2281）。一覧の列は見出し・検索・所有者の接頭辞・件数（`DbObjectSearchOwnerFields`）→ 名前とコメントの 2 段 + 行数の表（並べ替えは列見出し）→「さらに読み込む」、詳細の列は対象の見出し・`ObjectActionBar`・列情報 / DDL のタブ。選択は URL の `?table=` / `?view=`（`useWorkspaceUrlSelection`。keep-alive の間はほかの画面の URL を読まず、サイドナビから戻ると最後の選択を URL に戻す）。先頭の行を自動で選ばない。選んでいる行をもう一度押すと詳細を取り直す（取消・失敗の後の読み直し）。部品は `components/DbObjectManagementShared.tsx` の `DbObjectListDetail` / `DbObjectDetailPanel`。
- **割当（フィードバック管理。#2283）**: 「アプリ内フィードバック」のタブ（フィードバック履歴 + 管理者レビュー）も同じ `ListDetailLayout`（`id="feedback-management"`）。一覧の列は見出し・`ListToolbar`（履歴検索・利用者評価・業務プロファイル。列は 28rem 未満なので「絞り込み（N）」のシート）・件数 → 質問（2 行まで）と補足 1 行（評価の日時・利用者評価・管理者レビュー）の 1 列の表 → 列の下の `OffsetPagination`、詳細の列は管理者レビューのフォーム（業務プロファイル・日時・実行エンジンと処理時間・生成 SQL・評価・コメント・確定の行）。選択は URL の `?feedback=<履歴の ID>`（`useWorkspaceUrlSelection("feedback")`）で、先頭の行を自動で選ばない。編集欄の上の履歴の選択欄は持たない（一覧と前へ / 次へ で選ぶ）。未保存のレビューがあるときは、行・前へ / 次へ・1 ペインの「一覧へ戻る」の前に破棄を確認する（確認した編集は保存済みの扱いにしてから戻るので、戻る / 進むの確認を重ねない）。選んだ履歴が絞り込み・ページ送りで一覧から外れても別の行へ置き換えず、詳細に「選んだ履歴は今の一覧にありません」。
- **割当（データの管理のデータの表示。#2315）**: 「データの管理」の「テーブル・ビューデータの表示」のタブも同じ `DbObjectListDetail`（`ListDetailLayout` の `id="data-management-preview"`）。一覧の列は見出し・検索・所有者の接頭辞・種別（`FilterSelect`。列は 28rem 未満なので「絞り込み（N）」のシート）・件数 → 名前とコメントの 2 段 + 行数と種類（短い値）の表（並べ替えは名前・行数の列見出し）→「さらに読み込む」、詳細の列は対象の見出し（種類・行数・コメント）と `ObjectActionBar`（XLSX ダウンロード・データを空にする）→ 取得件数上限・「データを表示」・リセット → 表示結果（`ResultTable`。詳細の列の全幅）。選択は URL の `?object=<OWNER.NAME>`（`useWorkspaceUrlSelection("object", { enabled: <このタブを開いている> })`）で、先頭の行を自動で選ばない。ほかのタブへ移るときは URL から `?object=` を外し（選択は作業状態に残す）、タブへ戻ると URL に戻す。`?object=` 付きのリンクで開くとこのタブを開く。行を選ぶと（URL の選択が変わると）今の取得件数上限でデータを表示し、選んでいる行をもう一度押すと取り直す。以前の左右 1:1 の `FixedSplitPane` とステップの表示（対象選択 → 結果確認）は、狭い一覧 | 広い詳細の構成が段階をそのまま示すので持たない（結果の表に幅を渡す）。
- 「Select AI feedback」のタブ（Select AI の feedback のベクトル表の行 → 選んだ SQL の詳細と削除）は `FixedSplitPane`（`feedback-management-entries-split`）のまま。行に安定した ID が無く（`sql_id` は空のことがある）URL で選べないことと、一覧の内容（質問）と SQL を並べて見比べる技術の点検の画面であることから、一覧 + 詳細に移さない（#2283）。
- 実行履歴は 3 製品共通の型（一覧 → `?id=<run_id>` の全幅の詳細。UX 契約 `platform/docs/ux-contracts/run-history.md`。#1955）に移した。A 型の一覧内詳細は本規約を流用可。種類の絞り込みの既定は「品質評価以外」（Agent の品質評価の Run が MCP で呼んだ実行を隠す。選択肢は 品質評価以外 / すべて / チャット / SQL 生成 / MCP / 品質評価、「条件をクリア」は既定へ戻す）。品質評価の実行だけがあるときは、絞り込みを出したまま表の空の状態に隠している件数と出し方を出す（#1986。`security-rbac-deepsec.md`「実行履歴の種類」）。

### C. ツール / ワークフロー

- **割当**: SQL 生成 / SQL から質問を生成。入力と結果を左右に並べる画面（SQL から質問を生成・サンプルデータ管理）は `FixedSplitPane`（§3）。
- **一覧 + 詳細に移さないもの（#2315 で点検）**:
  - データの管理の「Excel/CSV アップロード（既存テーブル）」: アップロード先の表の選択は、ファイル・取込方法・実行確認語と並ぶフォームの 1 つの必須の欄（`fieldset` の複合入力）で、選んだ表の詳細を読む画面ではない。1 列の C 型（入力 → 実行 → 結果）のまま。
  - データの管理の「合成データ生成」: 業務プロファイル → 対象の表（複数選択の `ListPicker`）→ 条件 → 生成 → 確認 → 結果の段階の作業（C 型）。行を開いて点検する一覧が無い。
  - サンプルデータ管理（`sample-data-import` / `sample-data-delete`）と SQL から質問を生成（`sql-to-question-input`）: 入力（文・SQL）と実行の結果を同じ重さで見比べる C 型の `FixedSplitPane`。
  - フィードバック管理の「Select AI feedback」: 上の B 型の節のとおり、行に安定した ID が無く、質問と SQL を並べて見比べる点検のため `FixedSplitPane` のまま。

### D. ダッシュボード / 状態

- **割当**: SQL生成評価。

## 3. 一覧 + 詳細と分割ペイン

- `FixedSplitPane`（C 型の入力 | 結果と比較）は共有の `@production-ready/ui` の部品で、`DbObjectManagementPanelShell` の `splitId` に渡すと左右に並べる。保存 key の前置きは `FIXED_SPLIT_STORAGE_PREFIX`（`src/lib/ui-store.ts`）。
- `ListDetailLayout`（B 型の一覧 + 詳細）は `DbObjectListDetail` が包む。一覧の幅と開閉の保存 key の前置きは `LIST_DETAIL_STORAGE_PREFIX`（`production-ready-nl2sql.listDetail`）。一覧 + 詳細の画面は `DbObjectManagementPanelShell` に `framed={false}` を渡し、外側の枠を付けない（一覧と詳細の列がそれぞれ枠を持つ）。
- 共通のユーザー管理・ロール管理・権限管理（`@production-ready/system-settings` の `UserManagementPage` / `RoleManagementPage` / `RolePermissionsPage`）は共有の `ListDetailLayout` の一覧 + 詳細（#2282）。選択は URL の `?user=` / `?role=`（ロール管理の詳細の「権限管理で設定」は `?role=` を付けて権限管理を開く）、先頭の行を自動で選ばない。作成・編集は今までどおり一覧に代わる 1 枚のフォーム。製品のラッパー（`features/security/Security*Page.tsx`）は `listDetailStoragePrefix={LIST_DETAIL_STORAGE_PREFIX}` を渡す。

## 4. 色の移行

生のパレットの class をトークンへ置き換えるときは、重いファイルから着手する：共有 `DbObjectManagementShared`（テーブル / ビュー / SQL 分析の見た目の核）/ `DbAdminShared` / `DataManagementPage` / `EvaluationPage` / `QuestionLearningPage`。
