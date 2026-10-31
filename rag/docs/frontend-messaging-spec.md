# フロントエンド メッセージ機構（RAG の差分）

> 通知・成功 / エラー表示・フォーム検証・確認ダイアログ・空 / 読込 / エラー状態の共通規約は platform の [UX 契約 messaging.md](../../platform/docs/ux-contracts/messaging.md) が正本。ボタンは [UX 契約 buttons.md](../../platform/docs/ux-contracts/buttons.md)。
> 本書には RAG 固有の適用だけを書く。節番号は共通規約にそろえる。

## 3.3.1 全画面のエディタの保存の失敗（#585）

共通規約は [UX 契約 messaging.md §3.3.1](../../platform/docs/ux-contracts/messaging.md#331-全画面のエディタの保存の失敗585)。RAG の適用先:

| 画面 | 欄に結び付く失敗 | 欄に結び付かない失敗 |
|---|---|---|
| 検索・回答プロファイルのエディタ（`?id=`） | 名前・説明・参照 KB の未入力（欄の直下） | 確定の行（フォームの末尾の `FormActionBar`）の直上の `SaveErrorBanner`（`search-answer-profile-save-error`。#1786）。409（アーカイブ済みなど）もここ |
| ナレッジベースの作成・詳細 | 名前・説明の未入力、同じ名前の 409（名前の欄の直下 + フォーカス） | 確定の行の直上の `SaveErrorBanner`（`knowledge-base-save-error`。#1786） |

- 保存の失敗の `toast.error` と、フォームの下の `FormStatus` は出さない（成功だけ `toast.success`）。
- エディタの中の節の保存（検索・回答プロファイルの知識の節・ナレッジベースの抽出する項目・所属文書の追加など）は、節のボタンの近く（§3.3）のまま。

## 9. 失敗状態の情報設計（文書詳細への適用）

原則 P1–P5 は [UX 契約 messaging.md §9](../../platform/docs/ux-contracts/messaging.md#9-失敗状態の情報設計error-state-ia) を参照。

### 適用パターン（文書詳細）

```
header           : StatusBadge（文書状態の正本 = P1）
└ 重複/設定ドリフト等の警告 Banner（状況提示・失敗とは別概念）
└ レシピカード    : 工程ステップ表示（RecipeSteps）。工程列を維持し失敗ステップを danger 強調（P5）
└ 状態メッセージスロット（レシピ選択直下・**常に 1 本だけ**。優先順: 失敗原因 danger > 実行中 info > 承認待ちゲート案内 info）
   └ 失敗原因    : 「{工程}で失敗しました」+ 原因 + 対処（P2/P3 の要約）
   └ 工程の操作  : 承認・実行・再試行・再処理（Banner の action）。押した結果（投入・承認の失敗、開始）はボタンの直下（Banner の progress）に 1 つ
└ 取込・診断の詳細（折りたたみ・error 時自動展開 = P3）
   └ ジョブ/セグメント: 技術詳細。要約バナーと同一文字列は再掲しない（P2）
   └ 段の再試行  : 段の一覧の先頭に 1 つ（説明・ボタン・結果の順）
```

### 実装の指針

- 原因の一本化は `documents/ingestion-error-display.ts` の `resolveDocumentFailureView()`（最具体レイヤ採用 + 失敗ステップ導出）に集約する。要約バナーに昇格した文字列は `resolveIngestionErrorDisplayPlan({ suppressMessages })` と各詳細パネルの `suppressMessage` で**二重表示を抑止**する。
- **再処理の job が動いている間は前回の失敗を出さない（#2687）。** レシピは worker が job を claim するまで前回の `ERROR`・`error_message`・`failed_phase` を持つ。見出しが「〈工程〉の順番待ち」（レシピの step の `QUEUED`）を出す間と、画面が見ている job が `QUEUED` / `RUNNING` の間は、失敗原因のバナー・工程の `FAILED`・「再処理に失敗しました」の警告を出さない（判定は `recipeIngestionActive()`）。新しい job が失敗したら、新しい原因を出す。
- 「失敗した」状態の*存在*（バッジ）と「なぜ失敗したか」の*本文*（バナー1本）と「技術詳細」（折りたたみ）を**役割で分離**し、同じ文を場所を変えて繰り返さない。
- **抽出の warning は利用者の言葉で（#2034）。** 構造化要素のタブの warning は文にし、backend のコードは閉じた「詳細」に畳む（`extractionWarningView`）。状態メッセージスロットが Excel の表頭の確認の案内を出している間は、タブと変換の記録に同じ案内を出さない（同じ状態の案内は 1 か所）。
- **工程の操作と結果は状態メッセージスロットに置く（#2034）。** 承認・ファイル準備の実行・工程の再試行・再処理のボタンはスロットの `Banner` の `action`、押した結果（投入・承認の失敗は danger の `FormStatus`、開始の文）は同じ `Banner` の `progress` に出す（UX 契約 messaging.md §10.1・§10.2）。スロットの案内が無い状態（未処理・索引済み）は、同じ場所にボタンと結果だけを出す。カードの下端・別のカードに同じ操作・結果を出さない。
- **段の再試行は 1 つ（#2034）。** backend は 1 つの job で失敗・取消の段をまとめて解析し直す（段を選べない）ので、ボタンは段の一覧の先頭の「失敗した段を再試行」1 つにし、押したボタンだけを `loading`、結果はその直下に出す。結果を出している間は段の一覧を開いたままにする。
- **完了状態の常設 success バナーは出さない**（P1 の系）。完了という*状態*は `StatusBadge` / 工程ステップ表示 / 日時メタデータが担い、完了の*瞬間*の通知は遷移を観測した時のみ `toast.success` で 1 回出す（UX 契約 messaging.md §3.1）。
