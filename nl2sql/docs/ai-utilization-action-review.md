# AI 活用：ボタン別の再レビュー（2026-09-11）

対象は SQL 生成、SELECT SQL を実行、SQL から質問を生成、実行履歴。既存の初回レビュー後の実装を再点検した。調査・検証記録は #399、実装上の不具合は以下の独立 Issue / PR で扱う。

## 発見した不具合

| Issue | 操作と問題 | 修正 | PR |
|---|---|---|---|
| #391 | 「良い／違う」保存後、履歴再取得だけが失敗すると保存失敗と表示。保存中もコメント編集可能 | 保存と再取得の結果を分離、入力固定、重複送信防止 | #392 |
| #393 | SELECT SQL でファイル読込中に手入力すると、遅いファイル内容が入力を上書き | 手入力で旧読込を無効化。次の明示選択は読込可能 | #397 |
| #394 | SQL→質問のスキーマ参照で別 owner の同名表を混同 | owner/object_name で詳細を対応付け、修飾名で表示 | #398 |
| #395 | 履歴の「表示を更新」が失敗すると「さらに読み込む」が消える | 同一条件の cursor を保持。更新中と条件変更時の旧 cursor 利用を防止 | #400 |
| #396 | 履歴の「この質問で再実行」後に編集しても reload で旧質問が復活 | URL 初期値を一度だけ消費し、その後は編集済み草稿を復元 | #401 |

5 件とも専用 branch / PR で分離し、最新 commit の Backend / Frontend / Terraform checks 成功後に main へ merge 済み。

## 操作別の確認

下表は action handler、送信 payload、状態遷移と対応するブラウザテストを突き合わせた結果。成功表示だけでなく、無効状態・失敗・再試行・再読込を含めて確認した。「確認済み」は API fixture と後述の backend テストの範囲であり、実 OCI / Oracle の運用データに対する実行を意味しない。

### SQL 生成 `/query`

| 操作 | 確認内容・結果 |
|---|---|
| 最新情報を取得 | 入力を保持して参照データを再取得。失敗は参照領域で案内、実行中は無効 |
| DB 構造の再取得／参照取得の再試行 | schema 更新と再取得を区別。空 catalog からの回復とエラー領域を確認 |
| 業務プロファイルの選択 | profile ID と許可表の切替。古い表選択を次の job へ流用しない |
| プロファイル追加読込／再試行 | cursor による追加取得と再試行 handler を確認 |
| プロファイル新規作成の案内 | profile がない場合に生成を止め、管理画面へ遷移 |
| プロファイル自動判定 | 成功・同じ profile・低信頼度・失敗を確認。判定中の競合操作を停止 |
| 推薦プロファイル適用 | 質問を移動先 profile の草稿へ渡し、許可表選択を再構築 |
| Select AI / Select AI Agent / Enterprise AI Direct | 選択値が job の engine と一致。使用できない個別 override は案内 |
| 自由入力 | テンプレートを解除して入力を空へ戻す |
| 項目抽出 | 対象・項目・条件のテンプレートで全置換し、入力へフォーカス |
| 集計・グループ化 | 集計用テンプレートで全置換 |
| 上位N件・並び替え | 並び替え・件数用テンプレートで全置換 |
| 複数テーブル結合 | 結合用テンプレートで全置換 |
| AI 要件確認を開く | 現在の質問・profile で確認を開始し、通常生成と競合しない |
| 推薦 profile 確定／回答送信 | 選択回答・自由回答・手動回答から次の確認状態へ遷移 |
| 確認内容を質問に反映 | 草稿へ反映し、通常の生成ボタンへ戻る。自動 SQL 実行はしない |
| AI 要件確認を閉じる | 確認パネルの終了と入力編集への復帰 |
| 今回だけの生成条件・役割の開閉 | 折りたたみ、override payload、リセットを確認 |
| スキーマ検索 | 名前・項目検索と profile の許可範囲を確認 |
| 表を開閉 | 必要な詳細を取得し、他の参照カードと状態を分離 |
| 表名／項目名を挿入 | それぞれの挿入値、連続挿入時の改行、入力位置を確認 |
| スキーマ追加読込／再試行／エラー閉じる | 読込 cursor と最小エラー領域の handler を確認 |
| 実行オプション開閉 | Enter 操作、aria-expanded、mobile での操作性 |
| 用語・同義語を使う | 既定 off、ON 時の rewrite と job flag。変更がなければ不要なカードを出さない |
| 公開版オントロジー／処理手順／Show Prompt | 各 flag と結果表示を確認。Show Prompt は既定 off |
| 書換えた質問を適用 | 現在の質問へ明示反映し、再編集で旧 rewrite を解除 |
| 参考履歴を開閉 | 件数・空状態・許可されるレビュー済み履歴・実行終了後の表示を確認 |
| SQL を生成して実行 | job payload、段階別進捗、安全性判定、結果表を確認。二重操作と旧成功結果の混同を防止 |
| 実行を中止 | cancel 要求・中止状態・入力操作の復帰。404／通信断／TTL 失効も確認 |
| 新しい質問を開始／取消 | 草稿破棄の確認とリセット、取消で保持。ナビ往復では草稿を維持 |
| サンプルデータ投入 | catalog 空の回復。executed=false は成功として扱わない |
| SQL をコピー／通知を閉じる | clipboard 正常・fallback・拒否を確認。失敗を成功通知にしない |
| 処理手順の技術詳細／Show Prompt 開閉 | 詳細と SQL の対応、遅延取得と空・失敗状態を確認 |
| Ontology 表示 | 読取専用のノード／エッジ選択と生成 SQL の接地を確認 |
| 結果の前へ／次へ | 既定 25 件単位の表示、境界の無効状態と結果件数 |
| 良い／違う／コメント | POST 対象の history_id、悪評価のコメント必須、保存失敗再試行。#391 を修正 |
| 履歴リンクからの初期値 | question / engine / profile の引継ぎ、編集後 reload、再度の再利用、自動実行なし。#396 を修正 |

### SELECT SQL を実行 `/direct-sql`

| 操作 | 確認内容・結果 |
|---|---|
| ファイルを選択 | SQL テキスト読込、ファイルエラー、次ファイル選択。遅延読込と手入力の競合は #393 を修正 |
| ファイルをドロップ | 選択と同じ読込経路、キーボード導線、操作領域の高さ |
| SQL を編集 | 作業草稿として保持し、以前の実行結果と現在入力を識別 |
| 取得件数上限 | 空欄、0、負値、最大超過を拒否。有効値を通常実行 API に送る |
| SQL 実行 | SELECT 用 API のみを利用、DML を拒否。実行中は SQL・ファイル・上限と主操作を固定 |
| SQL をクリア | SQL・ファイル・結果・上限を明示リセット。ナビ復帰で勝手にクリアしない |
| 結果の前へ／次へ | 共通ページング。実行時の上限スナップショットを表示 |
| 再実行 | main スクロール位置と現在の実行結果を確認 |

### SQL から質問を生成 `/sql-to-question`

| 操作 | 確認内容・結果 |
|---|---|
| 最新情報を取得／参照エラーの再試行 | profile と schema 再取得。空・読込・失敗を区別 |
| 業務プロファイル選択 | 入力草稿の profile 分離、結果の無効化、許可表参照 |
| SQL入力・生成タブ | 入力へ戻る。キーボードによるタブ操作と状態保持 |
| SQL分析・質問候補タブ | 論理構造の後に候補を表示。旧 result タブ状態を移行 |
| 用語・同義語を使う | reverse 要求へ現在値を渡す。実行中の変更を防止 |
| 業務質問を生成 | SQL を解析して候補を生成し、成功時に分析タブへ移動。失敗時は入力保持 |
| SQL 論理構造を編集 | 編集内容を再生成の正本として保持。結果の鮮度を表示 |
| 論理構造から SQL を生成 | 編集済み構造を POST。空出力・生成不能・通信失敗の説明と再試行を確認 |
| 手順の技術詳細開閉 | 候補の業務説明・処理手順・SQL 断片を参照 |
| スキーマ参照 | 読取専用。同名表の owner と列の混同は #394 を修正 |

### 実行履歴 `/history`

| 操作 | 確認内容・結果 |
|---|---|
| 最新情報を取得／再試行 | サーバー側の履歴を取得。既存データなしの失敗回復と、既存 cursor の保持（#395） |
| 質問検索 | q を送信してサーバー側で検索 |
| 利用者評価フィルター | all / good / bad / unrated と API rating の対応 |
| 安全状態フィルタ | 安全・拒否・全件と API safety の対応 |
| フィルターをクリア | 条件を戻して先頭を再取得。別条件の cursor は流用しない |
| 各ソート見出し | 同じ見出しで昇降順、別見出しで切替。取得済み一覧の並べ替え |
| 履歴行を選択 | 選択した ID の詳細表示、別行で概要タブへ戻る |
| 概要／SQL タブ | 概要内の評価・コメント、SQL、キーボード、長文折返し、選択タブの往復・reload 保持 |
| さらに読み込む | cursor による追加取得と ID 重複排除。更新中の競合と失敗後の回復（#395） |
| この質問で再実行 | query へ入力を引継ぐだけで自動実行しない。編集後の reload 保護（#396） |
| 失効した選択 | 勝手に別履歴へ切り替えず、追加読込／再選択を案内 |

## 検証

- 初回 baseline：Playwright 112 passed。
- 個別修正：#391 は 6、#393 は 8、#394 は 24、#395 は 24、#396 は 16 件の Playwright が成功。
- 最終横断回帰：Playwright **190 passed (2.7m)**。desktop / mobile-375、fail / skip 0。
- `cd frontend && npm run build`：各実装修正で成功（TypeScript + Vite）。
- `cd frontend && npm run test:logic`：69 test entries passed、fail / skip 0。
- Backend：下記 182 件成功。実行権限、SELECT 安全性、履歴 cursor、SQL 構造 roundtrip、reverse 権限、job、要件確認、profile 権限、解析再利用を検証。
- `git diff --check`：成功。

```bash
cd backend
UV_CACHE_DIR=/tmp/nl2sql-uv-cache uv run --no-sync pytest \
  tests/test_nl2sql_execute_scope.py tests/test_nl2sql_sql_safety.py \
  tests/test_nl2sql_history_paging.py tests/test_nl2sql_structure_roundtrip.py \
  tests/test_nl2sql_reverse_deep_steps.py tests/test_nl2sql_reverse_access.py \
  tests/test_nl2sql_job_runtime.py tests/test_nl2sql_guided_clarification.py \
  tests/test_nl2sql_operation_profile_access.py tests/test_nl2sql_analysis_reuse.py -q
```

### 最終ブラウザ回帰コマンド

```bash
cd frontend
npm run test:e2e -- tests/e2e/nl2sql-workflows.spec.ts tests/e2e/nl2sql-execution-options.spec.ts tests/e2e/history-management.spec.ts --grep 'nl2sql-execution-options|history-management|query workbench|クエリ|スキーマ参照|スキーマピッカー|実行エンジン|必須入力欄|プロファイル削除|自動判定|推薦適用|job|SQL の生成と実行の開始時|参考履歴|実行結果|SQL を生成|SQL の生成と実行のジョブ|Query Rewrite|抽出条件|Ontology グラフ|未修飾列|サンプルデータ投入|今回だけの生成条件|AI 活用|SQL 実行中|SQL 再実行|SQL ファイル入力|history rerun|dark theme|sql to question|論理構造|フィードバック保存成功|SELECT SQL の遅延|スキーマ参照は別 owner|AI要件'
```

### 画面記録

5 件の修正フローを desktop / mobile-375 で記録。Playwright の `testInfo.outputPath()` に加え、作業成果物の `ai-utilization-review/` に 10 枚を保存した。

## 検証の限界

Playwright は実ブラウザで frontend を表示し、REST API は fixture で正常・異常・遅延応答を再現した。desktop 1280×900 と mobile-375 を実行し、該当 spec ではキーボード、150% zoom、ダークテーマ、横スクロール、clipboard の正常・拒否も確認した。backend は mock / in-memory を使用し、実 OCI 推論、実 Oracle の SQL 実行・権限・ネットワーク障害については未検証。LLM 出力内容の品質を保証するレビューではない。
