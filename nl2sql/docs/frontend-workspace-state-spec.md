# ページ遷移と作業状態の仕様

正本の原則は platform の [UX 契約 workspace-state.md](../../platform/docs/ux-contracts/workspace-state.md)。Issue #298 の実装範囲と保存境界をここに記載する。

## 保存境界

| 分類 | ページ往復 | ブラウザ再読込 |
|---|---|---|
| 明示登録した入力・表示条件 | 保持 | 同一タブ・同一ユーザー・同一 DB context の一時草稿を復元 |
| 生成結果、取得済み構造、結果行、ファイル内容 | SQL 作業ページではメモリ内に保持 | 一括保存しない。参照情報は再取得、結果は実行履歴を使用 |
| 実行確認語・破壊的操作のダイアログ | 解除 | 復元しない |
| 読み取り query | stale なものを再検証 | 再取得 |
| mutation | 復帰による再送なし | 再送なし |

一時草稿は `production-ready-nl2sql.draft.v1:` namespace の `sessionStorage` を使用する。レコードは最終保存から 8 時間で失効し、1 field の JSON は 200,000 文字まで。これは恒久保存機能ではない。ブラウザ自身のセッション復元機能では同一タブの storage が復元される場合があるため、期限・認証確認も併用する。

パスワード、確認語、credential、サーバー応答全体、結果行を保存対象に登録しない。所有ユーザーが変わる場合や logout / 401 では草稿を削除し、query cache も消去する。遅延した旧ユーザーの保存も拒否する。保存失敗は warning と `beforeunload` による離脱保護で知らせる。未実行のアップロードファイルも、保存対象外の入力として `beforeunload` で保護する。

## 対象画面

| 画面 | 再読込で復元する主な内容 |
|---|---|
| チャット | 業務プロファイル、生成方法、会話 ID、プロファイル・会話ごとの未送信草稿、デスクトップの履歴開閉（既定で閉じる）、会話の履歴のページ（offset `historyOffset`。#1265 / #1891）、送った質問の要件の確認（`requirementCheck`: 送った質問と送信の内容・回答前の確認の session の ID。SQL の生成のジョブを投入するまで。#1442）。本文・生成 SQL はサーバーの永続ジョブから、確認の質問・回答は回答前の確認の session から再取得 |
| SQL 生成 | engine、Profile、Profile ごとの質問／追加指示、実行オプション、Schema 検索 |
| SELECT SQL / 管理 SQL 実行 | SQL 入力、取得行数 |
| DB 管理チャット（#2264・#2319） | 会話 ID、会話ごとの未送信草稿、デスクトップの履歴開閉（既定で閉じる）、会話の履歴のページ（offset `historyOffset`）。チャットと同じく常時マウントする。会話・生成した SQL・文ごとの最後の実行の要約と「すべて実行」の要約（#2333）はサーバー（migration 024 の表）から再取得し、確認語と結果の行は保存しない（結果の行は画面のメモリにだけ持ち、開き直したら要約と「もう一度実行」） |
| SQL から質問を生成 | Tab、Profile、Profile ごとの SQL、用語利用設定 |
| コメント / アノテーション / ドメイン管理 | Tab、生成対象、追加指示、サンプル件数、検索・所有者・種類・ソート、実行用 SQL 草稿 |
| テーブル / ビュー管理 | Tab、検索・所有者・ソート、詳細の選択対象と表示 Tab、共通 SQL runner の入力 |
| データ管理 | 表示 Tab、プレビュー対象と検索・種類・ソート・件数、生成用の指示・件数等 |
| 実行履歴 | 検索と絞り込み（状態・種類・期間・実行者。`filters`）・一覧のページ番号と表示件数（`listPaging`。#1764 / #1891）・最後に開いた実行（`selectedId`。一覧へ戻ったときの `aria-current`）。詳細は URL の `?id=<run_id>`（#1955） |
| フィードバック管理 | アプリ内フィードバック: 履歴の検索・利用者評価・業務プロファイルの絞り込み、一覧のページ番号と表示件数（`appFeedbackPaging`）。Select AI feedback の一覧のページ番号と表示件数（`selectAiFeedbackEntriesPaging`。#1764 / #1891） |
| 質問分類モデル管理 | 学習データの検索・ページ番号と表示件数（`trainingSearch` / `trainingPaging`）、学習候補の検索・状態・業務プロファイルの絞り込みとページ番号・表示件数（`candidateSearch` / `candidateStatus` / `candidateProfileId` / `candidatePaging`。#1891） |
| SQL生成評価 | 最近の job と結果明細のページ番号・表示件数（`recentJobsPaging` / `resultsPaging`。結果明細は開いている job（`resultsJobId`）が変わったら 1 ページ目。#1891） |
| 用語・同義語 / 共通ルール | 一覧のページ番号と表示件数（#1891） |
| 業務プロファイル | 一覧の検索（`profileSearch`。#1933） |
| Deep Data Security | 対象ロールの一覧の検索（`entitlementSearch`。#1933） |

その他の編集画面は既存の保存・未保存変更 guard を継続する。新たに保存する field は `useWorkspaceState` 呼び出しとして明示登録し、機密性・所有 context・再検証方法を確認する。画面 component 全体の state serialization は行わない。

## context と再検証

- `/api/ready/database` の `context_id` は runtime / persistence mode、接続先、接続ユーザー、Wallet 位置から算出する一方向の識別子。生の接続先・ユーザー名・パス・パスワードは返さない。パスワード更新だけでは草稿 context を変更しない。
- context が変われば業務 query cache を破棄し、画面を新 context で構築する。以前の DB の草稿はその DB の namespace に残り、新 DB に流用しない。
- SQL 管理・表・ビュー・データ管理は、訪問済みページをメモリに保持する。非表示の一覧 query は無効化し、再表示時に stale な読み取りを再取得する。ページごとの Suspense により、未ロードページへ移動中でも以前の確認状態を解除できる。
- コメント / アノテーションの選択対象は復帰時と再読込時に詳細 API で検証する。失敗しても SQL 草稿と選択を残し、再検証成功まで実行を無効にする。部分的な一覧検索結果だけで、未表示の対象を「削除済み」と判定しない。
- SQL の変更・実行条件の変更・ページ切替で実行確認を解除する。前回の完了結果には日時を表示し、入力変更や復帰時は前回の結果であることを示す。
- 戻る／進む／サイドナビで同じ URL の作業位置へ戻る場合は主スクロール位置を復元する。hash による明示的な移動先を優先する。

## 操作

- `最新情報を取得`：参照情報の更新。SQL の手編集は保持する。
- `入力をクリア`：該当 runner の入力・実行表示・確認をクリアする。
- `新しい作業を開始`：SQL 生成の作業状態を初期化する。入力がある場合は共通確認ダイアログで破棄を確認する。

## 検証

Playwright の `nl2sql-workflows.spec.ts` の `workspace:` ケースで、desktop / mobile-375 の往復・再読込・確認解除・前回結果・対象失効・DB / アカウント分離・一時保存不可を確認する。草稿の期限、型不整合、ユーザー切替、保存失敗、遅延書込み拒否は `workspace-drafts.test.ts`、接続 context の識別境界は backend の `test_database_persistence.py` で検証する。
