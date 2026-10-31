# 合成データ生成の受理・実行・結果確認

Issue #305。UI の `生成開始` は永続記録の受理であり、書込み成功の通知ではない。
要求件数と `DBA_LOAD_OPERATIONS` / operation の状態表が報告する書込み件数を分離する。
既存表の総行数から今回の生成件数を推定しない。

## UI

「生成状況」に受付済み、生成中、結果確認中、完了、部分完了、生成失敗、追加 0 件、結果未確認を表示する。
表ごとの要求・追加件数、処理終了日時、失敗理由を保持する。生成番号は常時表示する。
画面内の詳細領域と結果欄への移動専用ボタンは設けず、データ参照は下部の「データを表示」を使う。
ページ遷移・再読込では既存 run を再取得し、生成を再送しない。確認語は解除する。
別ページでは完了通知と結果へのリンクを表示する。状態取得失敗時は以前の状態を残して更新失敗を明示する。

完了・部分完了後、結果の表示条件を編集していなければ、一度だけ上限付きの参照を行う。
参照結果は「現在のテーブル内容」であり、今回生成された行だけの抽出ではない。SQL/結果本体は永続化しない。
生成記録の追加件数が正数でも現在の参照が 0 件なら、接続・権限・生成後の変更を確認する説明を表示する。
参照エラーは生成失敗に書き換えない。削除・再作成された対象表の結果参照は 409 で拒否する。

## API

- `POST /api/nl2sql/synthetic-data/runs` → 202。既存生成条件に `idempotency_key`（16〜128 文字）を追加する。
- `GET /api/nl2sql/synthetic-data/runs` → 同一 actor / DB context の直近 100 件（実行中・結果未確認の run はすべて）。
- `GET /api/nl2sql/synthetic-data/runs/{run_id}` → 保存済み状態。一覧に無い古い run も ID で参照できる。各 run は確認用データの期間を過ぎたか（`review_expired`）を返す。
- `GET /api/nl2sql/synthetic-data/runs/{run_id}/results?table_name=...&limit=100` → 今回の対象だけを現在の actor の参照権限で取得。上限 1〜10000。

同一 actor・context・受付キーで同じ条件なら既存 run を返す。条件が変われば 409。
生成記録（`NL2SQL_SYNTHETIC_RUNS`）は期間で削除しない（実行の記録は無期限に残す。#3235。以前は処理終了から
24 時間で削除していた）。処理終了から 24 時間（旧終端記録に終了日時が無い場合は受付日時から）を過ぎた終端記録
（completed / partial / failed / no_data）は確認用データ（一時表）を worker が片付け、閲覧だけにする（適用・破棄は 409）。
生成した業務テーブルのデータや Oracle の operation 記録は削除しない。受付キーの幂等保証は記録が残る限り続く。
同じ表・別の表に未終了 run があっても、新しい受付キーの生成を独立した run として受理する。
受理リクエストの処理中だけ二重クリックを防ぎ、受理後は確認語を解除して次の入力・生成・参照を許可する。
旧 `POST /synthetic-data/generate` も同じ永続受理を使う（202、`executed=false`、`status=accepted`、
`engine_meta.run_id/status_url`）。旧レスポンスの成功判定に依存する外部クライアントは新 API へ移行する。
旧 endpoint の再送は受付キーを持たないため、応答不明時は一覧から確認する。

## Oracle と worker

migration `019_synthetic_runs.sql` が `NL2SQL_SYNTHETIC_RUNS` と `NL2SQL_SYNTHETIC_LOCKS` を作成する。
受理は run のみを commit し、actor/context/受付キーの unique 制約で同一要求の再送をまとめる。
表単位の排他は使用しない。`NL2SQL_SYNTHETIC_LOCKS` は既存環境との互換性のため残し、旧版の
lock 行は対応 run の終端時に片付ける。追加 migration は不要。CAS version で `pending → running` を一度だけ claim する。
実行直前に actor/権限、DB context、対象 OBJECT_ID、Profile、非対応列を再検証する。
生成前に監視権限と専用接続の SID / SERIAL# / USERNAME / 開始時刻を確認・保存する。
`DBA_LOAD_OPERATIONS` はこの組合せで検索し、全体の `MAX(ID)` を使わない。
Oracle 終端状態と全対象の chunk 件数を確認して初めて完了とする。未確認件数は null のまま保持する。

worker 再起動時は既に claim した生成を再実行せず、Oracle 記録との照合だけを再開する。
書込み開始後の応答切断は `unknown` とし、同じ run は再実行しない。照合が復旧すれば最終状態へ移行する。
Oracle が結果を一意に返せない場合は自動再実行しない。新しい明示的な生成要求は別 run として受理できる。
管理者が実行記録を調査する必要がある。現時点では中止・部分実行の自動再試行 UI は提供しない。

監視には接続ユーザーによる `DBA_LOAD_OPERATIONS`、現在セッションの `V$SESSION`、生成状態表の参照が必要。
監視できない構成では生成前に失敗させる。Oracle runtime / Oracle persistence が必須。

## 配置

1. 既存のシステムテーブル初期化フローで migration 019 を適用する。
   `cd backend && uv run python -m app.cli.nl2sql_system_schema --initialize`
2. ローカルは `NL2SQL_SYNTHETIC_WORKER_MODE=inprocess`（既定）。生成の本体は API の要求処理外で動く。
3. 新規 systemd 配置は `production-ready-nl2sql-synthetic-worker.service`（ローカルで専用 worker を試す場合は下の `uv run`）。
   専用 worker 構成では backend を `NL2SQL_SYNTHETIC_WORKER_MODE=external` にする。
4. 旧 systemd 配置に専用 unit が無い場合、更新スクリプトは backend の inprocess 動作を維持する。
   手動で external に変更した場合は専用 worker も起動する。

worker: `uv run python -m app.cli.nl2sql_synthetic_worker`。
システム表の再作成は未終了・未確認 run があれば拒否する。

## 待機表示と時間

生成状況は「生成開始」の直下、結果データ領域の手前に配置する。送信中は共通
`ProcessingIndicator` の受付表示とボタン内の spinner、受理後は状態パネル内の spinner
1 個に切り替える。状態ラベルは `StatusBadge`、計時は `useOperationTiming` を再利用する。

受付待ち・生成中・結果確認はサーバー状態の polling に追従する。経過時間は受付時刻
`created_at` から毎秒更新し、待機時間を含む。開始への遷移や再読込でリセットしない。
終了後は `finished_at - created_at` を「処理時間」として保持する。表示は共通規約の
`mm:ss` / `h:mm:ss`、読み上げは `role="timer" aria-live="off"`、reduced motion に対応する。

状態取得失敗時は直前の情報と「現在の処理状況は未確認」を表示し、spinner を停止する。
この場合の時計は受付からの経過であり、処理継続の証拠ではない。結果不明 (`unknown`) は
終了を断定せず、計時表示を外して再確認を案内する。新規生成を受理したら過去結果の
URL 選択を解除し、新しい生成状態を表示する。

## 検証

pytest は受理、認可、再起動、CAS、部分/0/null 件、API の隔離を検証する。
Playwright は desktop / mobile-375 で再読込、状態、通知、空結果、参照上限、既存操作を検証する。
`NL2SQL_SYNTHETIC_LIVE_TEST=1 uv run pytest tests/test_nl2sql_synthetic_oracle_live.py`
は独立した一時メタデータ表を作成・削除し、実 Oracle の CLOB/JSON、CAS、同表の独立受理、
受付キー競合の rollback、旧 lock の互換 cleanup、後始末の対象の読み出し（`list_cleanup_candidates`）を検証する（CI の Oracle integration の Free で実行する）。ADB だけにある `DBA_LOAD_OPERATIONS` を読む `capture_session` は `NL2SQL_SYNTHETIC_ADB_LIVE_TEST=1` のときだけ確かめる。
このテストは業務表・生成プロシージャを変更/実行しない。

## 生成番号と引数拒否の追跡

受理時に生成番号 (`run_id`) を払い出し、202 応答の body と `Location` header の
`/api/nl2sql/synthetic-data/runs/{run_id}` から個別状況を取得できる。画面では生成番号と
最終 Oracle 確認時刻を折りたたまず表示する。HTTP 200 は取得成功だけを意味し、
生成が続いているかどうかは `status` / `checked_at` / `operation_ids` で判断する。
backend は受理・開始・結果変化時に `synthetic_run_state run_id=... status=...`
を記録し、prompt や credential は log に含めない。

[No.1-SQL-Assist の固定版](https://github.com/engchina/No.1-SQL-Assist/blob/cacd0959a5fa2a279cb1147de2d04e5a9b01815f/utils/selectai_util.py#L6542)
と [Oracle のパラメータ仕様](https://docs.oracle.com/en/database/oracle/oracle-database/19/arpls/dbms_cloud_ai1.html)
に合わせ、複数表の `object_list` で空の `user_prompt` はキーごと省略する。指定された
プロンプトは各対象に保持する。JSON null を含めると Oracle は
`Missing value for user_prompt ... in argument object_list` で拒否する。

過去の `unknown` も、この特定の引数拒否・呼出しからの戻り・対応 operation の不在・
元 session の終了を確認でき、過去に operation / 書込み件数を観測していない場合に限り
`failed` / 0 件 (`failure_phase=validation`) に確定する。旧版の表 lock が残る場合は通常の終端処理で片付ける。汎用 ORA-20000・timeout・監視失敗はこの処理の対象外。
新規生成はユーザーの再実行操作を必要とし、worker は失敗記録を自動再送しない。

## 複数の生成を継続する

進行中や結果未確認の記録は、新しい生成や現在のテーブル参照を妨げない。同じ表への新規要求も
別の生成番号で扱い、保存した生成条件・Oracle session・追加件数を run ごとに独立して追跡する。
履歴では時刻・状態・対象表・短縮番号を併記し、同時刻の要求を区別する。選択中以外の記録の
完了によって、新しく編集中の生成条件をリセットしない。

既存の worker は 1 process あたり 2 実行を並行処理し、枠を超えた要求は `pending` で待つ。
これは受理の拒否とは異なる。新しい生成ごとに確認語が必要で、HTTP 再試行だけの場合は同じ
受付キーを使う。完了件数は各 Oracle operation の記録から確認し、テーブル総行数を流用しない。
