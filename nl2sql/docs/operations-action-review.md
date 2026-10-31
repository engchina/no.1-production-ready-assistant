# 改善・運用：ボタン別レビュー（2026-09-11）

対象は業務プロファイル、オントロジー構築、フィードバック管理、質問分類モデル管理、SQL生成評価。調査記録は #409。各画面の action handler、送信値、状態遷移を読み、既存および追加 Playwright と backend 回帰テストに照合した。

## 登録・修正した問題

| Issue | 確認した問題 | 修正 | PR |
|---|---|---|---|
| #403 | Profile 保存・一括選択中の編集が応答で上書きされる | 処理中の競合操作を固定し、応答を対象 Profile に限定 | #410 |
| #404 | Profile 詳細取得失敗が一覧への遷移で隠れる | URL を保持し、取得失敗と再試行を表示 | #411 |
| #405 | Ontology 公開前の内部保存中に編集・構築できる | 内部保存・公開中の草稿編集と構築を固定 | #412 |
| #406 | feedback 再取得で未保存レビューと設定が消える | 草稿・ページ保持、離脱確認、処理中操作の固定 | #413 |
| #407 | 分類候補のページ取得失敗で cursor 履歴が壊れる | 成功時だけ履歴更新し、失敗した条件で再試行 | #415 |
| #408 | 評価開始 POST の応答前に送信条件を変更できる | 提出中も条件を固定し、失敗後に保持したまま復帰 | #416 |
| #414 | 訓練データの先行保存が後から開いた編集を閉じる | 競合操作固定、未保存編集の取消・離脱保護 | #418 |
| #417 | 分類候補の表示更新でデータとページ番号が不一致 | 適用済み検索条件・cursor を再取得と前後移動で使用 | #419 |

8 件をそれぞれ Issue → 専用 branch → PR に分離。最新 commit の Backend / Frontend / Terraform checks 成功を確認し、8 件とも main へ merge 済み。

## 操作別の確認

以下の「確認」は API fixture の実ブラウザ検証とコード・backend テストの範囲を示す。実 OCI / Oracle の運用データを更新したという意味ではない。

### 業務プロファイル `/profiles`

| 操作 | 確認内容・結果 |
|---|---|
| 最新情報を取得 | 一覧・参照情報を再取得。エラー通知は再描画で消えず再試行可能 |
| DB 構造を再取得 | schema 更新 job の進行、完了、エラーを表示 |
| DB profile 情報を取得 | DBMS_CLOUD_AI profile の参照更新と処理表示 |
| 一覧検索 | API 検索結果と total を採用。打鍵ごとの不要な再送なし |
| 列見出しで並べ替え | サーバー順と方向を保持、キーボード操作を確認 |
| 追加読込・追加取得の再試行 | cursor と取得済み一覧を保持 |
| 新規作成 | 空フォームと credential region の初期値。未編集では破棄確認を出さない |
| 行の編集・深い URL | 指定 Profile を取得。#404 で失敗時も URL を保持し再試行可能 |
| 一覧に戻る | dirty 時に確認、取消で草稿保持。保存・一括選択中は固定 |
| 名称・カテゴリ・Select AI 設定 | 必須、重複、識別子、大文字化、狭い幅の表示を確認 |
| オブジェクト検索 | owner/object 名の範囲で表・ビューを絞り込む |
| schema 展開・追加取得 | catalog の対象を表示。別の管理用 schema 一覧を混在させない |
| 表・ビュー個別選択 | 同名の別 owner を分離し、引用符付き保存値も解除可能 |
| schema のすべて選択 | 表示対象の snapshot を取得。#403 で取得中の保存・対象切替を固定 |
| schema の選択解除 | 絞り込み時は該当 object のみに作用する |
| Oracle 実行確認語の入力・クリア | 保存成功後に確認語を解除し、次回実行を再ゲート |
| 保存 | 入力検証、許可 object と設定 payload、失敗時の草稿保持。#403 で競合編集を停止 |
| Oracle 反映の再試行 | 失敗を明示し、必要な確認を保持して再試行。Ontology を更新しない |
| Credential 不足の設定リンク | DB 設定へ案内。戻っただけでは以前の job を自動再送しない |
| 一覧・編集画面から削除 | 標準 Profile を含め確認ダイアログ。cleanup 警告と対象消失を表示 |
| 削除・離脱確認の取消 | mutation を送らず、選択と草稿を保持 |

### オントロジー構築 `/ontology-build`

| 操作 | 確認内容・結果 |
|---|---|
| Profile 選択・情報を取得 | 明示した Profile のみ取得。無効 URL は別 Profile に自動置換しない |
| Profile / View 読込再試行 | 失敗領域とキーボード再試行、workspace の loading を確認 |
| 初期読込の取消 | 遅い旧応答で取り消した画面を上書きしない |
| 構築資料・Q/A ファイルの選択／解除 | 入力から抽出対象を決定。送信ファイルと役割を確認 |
| 保存済みファイル表示 | Profile ごとの資料・Q/A、状態、抽出件数を表示 |
| 保存済みファイル削除・取消 | 確認後のみ対象 ID を削除。取消では保持 |
| 業務説明・構築条件 | 現在入力を新規 job の payload に使用 |
| AI 構築を実行 | job 作成、進捗、成果物取得。#405 で公開との競合を停止 |
| 処理状況の開閉 | 折畳・再実行時の展開、処理時間と step 状態を確認 |
| 実行中 job の中止・取消 | 確認して cancel API、取消なら継続 |
| 失敗 job の再実行 | retry API を使用。主ボタンからの再構築は現在入力で新規実行 |
| job 監視 | reload 後の復元、一時通信断、404、長時間更新停止を検証 |
| DB 構造を再取得 | 未解決 schema からの復旧。進行表示と再取得後の graph を確認 |
| Markdown 下書き／公開版 tab | version、公開日時、キーボード切替を確認 |
| Markdown コピー | 表示中の内容を clipboard へ出力、成功通知 |
| Markdown 草稿保存 | ETag と草稿を送信。古い再取得応答で編集を戻さない |
| Markdown 読込の再試行 | 失敗領域から再取得。公開直後の一時失敗でも公開内容を保持 |
| 公開 | dirty 草稿の内部保存→公開→監視。#405 で内部保存中も編集を固定 |
| SHACL 違反後の再公開 | 検証違反を表示し、修正・保存後に再公開可能 |
| 質問の接地 | 入力質問の分類と graph 強調を表示、入力変更で旧検索結果を無効化 |
| サーバー検索 | Profile/revision/question を送信し、古い応答を sequence で除外 |
| 質問入力クリア | 質問、検索結果、強調・選択を解除 |
| graph 検索・カード選択 | 概念と物理表、ノード・エッジ詳細を区別して表示 |
| 詳細折畳・物理 ER 展開 | 選択した object の詳細のみ段階表示 |
| graph 表示切替・mobile 展開 | 凡例と主要ノードの可視性、内部スクロールを確認 |
| graph ノード移動・配置リセット | ドラッグ後に初期配置へ復帰。hover 時にエッジが消えない |

### フィードバック管理 `/feedback-management`

| 操作 | 確認内容・結果 |
|---|---|
| 最新情報を取得 | 参照データ再取得。#406 で草稿・設定・現在ページを保持 |
| 4 種の tab 切替 | アプリ内、Select AI feedback、Select AI ベクトル、類似検索設定を切替 |
| DBMS_CLOUD_AI profile 選択 | 対象 Profile の Select AI feedback を取得 |
| エントリの最新情報を取得 | entries の再取得、空・読込・エラーからの復旧 |
| Select AI feedback 行選択 | CONTENT と SQL_TEXT の選択・詳細表示 |
| 選択 SQL のコピー | 選択中の SQL のみ clipboard に出力 |
| 選択 feedback の削除・取消 | 確認後に Profile/SQL を送信。executed=false は成功扱いにしない |
| Select AI ベクトルインデックス更新 | Profile、閾値、件数を送信し warnings を表示 |
| 履歴検索・評価フィルター・Profile フィルター | API 条件と一覧・件数の更新。#406 で未保存変更を確認、取消で条件維持 |
| アプリ内履歴の前後移動 | cursor と現在ページ。保存後も現在ページを再取得 |
| 履歴行・対象履歴の選択 | 管理者レビューと利用者評価を分離。未保存変更は確認 |
| 利用者コメントを反映 | 利用者コメントを管理者レビュー草稿へ明示コピー |
| 管理者レビュー結果 | 「違う」はコメント必須。必要な field にフォーカス |
| Select AI feedback に登録する | 明示チェックと SQL の入力を登録 payload に反映 |
| フィードバック保存 | 管理者レビュー保存・類似検索公開・任意 Select AI 登録を区別。#406 で待機中編集を固定 |
| フィードバックを解除・取消 | 確認後に履歴 ID を削除。解除成功後の baseline と確認状態を更新 |
| 学習候補で確認 | history_id のリンク、#406 の未保存離脱確認と取消 |
| 最低スコア・最大候補数の変更 | slider / number と設定値の連動。未保存の既定は最低スコア 0.9・最大候補数 3（#1721） |
| 設定保存 | 閾値・候補数 PATCH、成功時 baseline 更新。刷新で未保存設定を消さない |

### 質問分類モデル管理 `/question-classifier-models`

| 操作 | 確認内容・結果 |
|---|---|
| 最新情報を取得 | 全参照情報を取得。#417 で候補の適用済み条件と現在ページを保持 |
| 訓練データ／モデル学習／モデルテスト／学習候補 tab | キーボード・深い URL・mobile 表示を確認 |
| 訓練データの最新情報を取得 | 一覧を再取得。#414 で mutation 待機中は実行不可 |
| CATEGORY / TEXT / SOURCE 検索 | 一覧内検索とページ先頭への調整 |
| 訓練データの前後ページ | 既定 25 件単位の範囲、総件数、前後ボタン |
| 訓練ファイル選択・取込 | CATEGORY/TEXT 契約、追加／置換、既存データ置換の確認 |
| ファイル選択解除・置換確認の取消 | 取込を送らず選択状態を解除 |
| Training XLSX 出力 | export endpoint と CATEGORY/TEXT 形式（backend テスト） |
| 行メニュー：編集 | Profile と質問を入力。#414 で別行の未保存編集を確認 |
| 行編集：保存 | ID/text/profile_id PATCH。#414 で処理中の入力・取消・別操作を停止 |
| 行編集：キャンセル | dirty の場合に確認、取消で入力を保持 |
| 行メニュー：削除・取消 | 確認して対象 ID を削除し、モデルの再学習待ちを表示 |
| Classifier 学習 | ready / version / warnings を評価し、拒否時はエラーを表示 |
| 分類を試す | 入力・モデル version と予測を対応付け。未学習・空入力・処理中は停止 |
| 候補検索・絞り込み | search/status/profile/history_id を送信。適用前の入力を一覧条件と分離 |
| 条件解除 | 空検索・全状態・全 Profile で先頭から取得 |
| 候補の前後ページ・失敗再読込 | #407 で成功時だけ cursor 更新。再試行は失敗した方向・条件を使用 |
| 候補個別選択・表示中をすべて選択・解除 | 対象件数と可選択状態を反映 |
| 追加する Profile 選択 | 名前・カテゴリ、keyboard、対象別 override を確認 |
| 選択候補を追加・確認取消 | 対象 history_id と Profile を送信。エラーを action 領域で表示 |
| フィードバック管理で確認 | history_id 付きリンク。#414 で未保存訓練編集の離脱を確認 |

### SQL生成評価 `/evaluation`

| 操作 | 確認内容・結果 |
|---|---|
| 業務 Profile 選択 | profile_id と利用可能な Profile を確認 |
| XLSX テンプレートダウンロード | download 応答とファイル名、backend のテンプレート契約 |
| 評価 XLSX 選択・解除・drop | 拡張子・容量・必須検証、選択ファイルの表示 |
| エンジン個別選択・すべて選択・解除 | unavailable を有効化せず、選択可能なエンジンだけを変更 |
| 繰り返し回数 | limits と推定試行数、数値検証 |
| 評価を開始 | Profile/file/engines/repeat_count を POST。#408 で応答前から条件を固定 |
| 開始失敗後の再試行 | action footer に検証・通信エラー、入力とファイルを保持 |
| 最近の job 表示・前後移動 | job ID の URL、一覧 cursor、日時・状態を確認 |
| job 詳細を開く・reload | 永続 job を復元して進捗取得。ページ表示だけで新規 job を作らない |
| 実行中の中止・確認取消 | active job に cancel、取消では継続。backend は所有者・状態を検証 |
| 結果の前後ページ | job 切替時の cursor リセットと結果範囲 |
| 分析詳細の開閉 | 判定理由・生成 SQL の詳細、desktop table / mobile card |
| 結果 Excel ダウンロード | ファイル名と結果形式、空・実行中状態の操作条件 |
| 完了 job 削除・確認取消 | terminal job だけ削除、現在 job 削除後は URL と結果を解除 |

## 検証

最終ブラウザ回帰は **242 pass / 18 skip / 0 fail**。skip は既存の desktop 内で mobile も確認する重複抑制、viewport 専用フロー、mobile 非対象の mouse drag/hover テスト。backend は **192 pass**、frontend logic は **69 pass**。

| 検証 | 結果 |
|---|---|
| Profile / Ontology build / graph / SQL 評価（下記 core command） | 196 pass / 14 skip |
| feedback / 分類（下記 workflows command） | 46 pass / 4 skip |
| backend の 7 spec | 184 pass |
| XLSX 訓練データの import / export | 8 pass |
| `npm run test:logic` | 69 pass |
| `npm run build` / `git diff --check` | pass |

frontend ディレクトリで実行：

```bash
npm run test:e2e -- tests/e2e/profile-allowed-objects.spec.ts tests/e2e/profile-archive-reset.spec.ts tests/e2e/nl2sql-ontology-build.spec.ts tests/e2e/nl2sql-ontology-graph.spec.ts tests/e2e/quality-evaluation.spec.ts --workers=2
npm run test:e2e -- tests/e2e/nl2sql-workflows.spec.ts --workers=2 --grep 'feedback management|Select AI feedback|app feedback|admin good feedback|feedback missing-table|question classifier|learning candidate|feedback refresh|classifier training save|classifier refresh|分類結果|フィードバックの前ページ|フィードバック保存成功'
npm run test:logic
npm run build
```

backend ディレクトリで実行：

```bash
UV_CACHE_DIR=/tmp/nl2sql-uv-cache uv run --no-sync pytest tests/test_nl2sql_profile_lifecycle.py tests/test_nl2sql_profile_sync.py tests/test_nl2sql_ontology_build.py tests/test_nl2sql_ontology_access.py tests/test_nl2sql_feedback_classifier_learning.py tests/test_nl2sql_quality_evaluation.py tests/test_nl2sql_operation_profile_access.py -q
UV_CACHE_DIR=/tmp/nl2sql-uv-cache uv run --no-sync pytest tests/test_nl2sql_legacy_absorption.py -k 'classifier_training_data' -q
```

初回の core baseline は 154 pass / 6 fail / 12 skip。失敗した 3 シナリオ × 2 viewport は旧版の自動読込前提だったため、#409 で「情報を取得」を明示的に操作する現行フローへ更新し、6 pass を確認してから全体を再実行した。個別修正中の locator 誤記・初期ロード timeout・寸法計測の一時失敗は各 PR に記録し、最終回帰では解消した。

補足：`test_nl2sql_legacy_absorption.py -k 'classifier or feedback'` の追加実行は旧 model artifact import で OCI SDK の retry 待機に入ったため中断した。現在の UI にない legacy model import を合格件数に含めず、現行 XLSX 訓練データを対象に絞った 8 件を別途実行して pass を確認した。

ブラウザは desktop（既定 1280×900、一部 1440px）と mobile-375（375×812、一部 375×900）。fixture API を使用し、実ページの入力、クリック、キーボード、loading/empty/error、横幅を確認する。

## スクリーンショット

24 枚を `/home/ubuntu/.codex/visualizations/2026/09/10/01a08bc3-bb0c-7dd0-b06f-8312d66eda32/operations-review/` に保存した。Profile の保存失敗・詳細再試行、Ontology の公開回復・graph、feedback の草稿保持、分類の編集・ページ復旧、評価開始失敗を desktop / mobile-375 で記録。各画面の代表画像を目視し、日本語表示・操作領域を確認した。mobile の訓練データ一覧は従来どおり表内スクロールを使用する。

## 制約

- 実 OCI Enterprise AI、Oracle、Select AI の運用資格情報での新規生成・公開・評価は実行していない。backend は InMemory/store・client fixture を含む回帰テストで検証した。
- 未保存離脱の共通 hook は内部リンクと beforeunload を保護する。browser back/forward の完全なガードは既存 BrowserRouter の制約として対象外。
- このレビューは列挙した action と回帰ケースの範囲であり、全入力・全ネットワーク障害の無欠陥を保証するものではない。
