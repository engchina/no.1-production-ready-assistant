# Backend Concurrency Contract

FastAPI は ASGI で動作するため、`async def` route の中で同期 I/O を直接実行すると event loop 全体を塞ぐ。
Production Ready NL2SQL の API は、単一 worker のローカル開発でも 1 つの重い処理が他画面の API を止めないことを契約とする。

## 原則

- **sync route by default**: domain service、Oracle repository、file/CLOB/Excel、同期の HTTP client など同期処理を呼ぶ route は普通の `def` で実装する。FastAPI/Starlette が route 全体を threadpool で実行する。
- **async route は本当に async の時だけ**: `UploadFile.read()`、SSE/WebSocket、async の HTTP client など、`await` が必要な処理を持つ route だけ `async def` にする。
- **async route から同期処理を呼ぶ時は `run_sync_io`**: `backend/app/api/concurrency.py` の `run_sync_io(...)` で同期 I/O を threadpool に逃がす。
- **CLI/worker は対象外**: ASGI event loop 上ではない CLI、batch、worker は同期 service を直接呼んでよい。

## 禁止例

```python
@router.get("/example")
async def example() -> ApiResponse[ExampleData]:
    data = nl2sql_service.load_heavy_data()
    return ApiResponse(data=data)
```

## 推奨例

同期 service だけを呼ぶ route は `def` にする。

```python
@router.get("/example")
def example() -> ApiResponse[ExampleData]:
    return ApiResponse(data=nl2sql_service.load_heavy_data())
```

UploadFile など async I/O と同期 service が混在する場合だけ `run_sync_io` を使う。

```python
@router.post("/example/import")
async def import_example(file: UploadFile) -> ApiResponse[ImportData]:
    content = await file.read()
    data = await run_sync_io(nl2sql_service.import_data, content)
    return ApiResponse(data=data)
```

## Guardrail

`backend/tests/test_async_route_concurrency_contract.py` が `backend/app/**/router.py` を AST で走査し、route-decorated `async def` が同期 service/Oracle/file/SDK 呼び出しを直接行わないことを検査する。
新しい async route を追加してこのテストが落ちた場合は、route を `def` に変えるか、同期呼び出しを `await run_sync_io(...)` に移す。

## Markdown 公開準備の実行期限と中断

- Markdown 公開前の解析ジョブは成果物を状態の正本とし、`jobs` の状態を開始時・終了時・取得時に同期する。
- `NL2SQL_ONTOLOGY_PREPARATION_TIMEOUT_SECONDS`（既定 600 秒）は待機時間を含む期限。Enterprise AI の解析は自動再送せず、残り時間を HTTP timeout に渡す。
- in-process で開始した解析は shutdown 時に off-loop で `PREPARATION_INTERRUPTED` として終了させてから DB pool を閉じる。起動時の DB 接続は行わない。
- 強制終了や旧版が残した `queued` / `running` は、状態取得時に期限を確認して `PREPARATION_TIMEOUT` に収束する。旧レコードは作成日時と設定値から期限を求める。
- 実行開始と結果保存は同じ成果物の ETag を使う。重複配送は解析を再送せず、中断・期限切れ後の遅着結果は破棄する。期限は同期 HTTP スレッドを強制 kill するものではない。
- エラー後は画面から公開前の確認を再実行する。解析成功後も、差分・検証結果を確認してから公開するゲートを維持する。

## オントロジー AI 構築

- `NL2SQL_ONTOLOGY_BUILD_TIMEOUT_SECONDS`（既定 21600 秒）の絶対期限と `NL2SQL_ONTOLOGY_BUILD_LEASE_SECONDS`（既定 120 秒）の heartbeat を永続化する。長い LLM 呼び出し中も heartbeat を更新する。
- 取得・履歴・次の開始時に失われた実行を `cancelled` へ収束させ、`ONTOLOGY_BUILD_TIMEOUT` / `ONTOLOGY_BUILD_WORKER_LOST` を表示する。旧 job は作成日時・開始日時・イベント日時を使う。external worker の queued は heartbeat 不在だけでは中断しない。
- queued の ETag で実行を claim し、実行 ID を確認して結果を保存する。期限切れ・中断後の遅着結果は下書きを上書きしない。profile lock の削除には所有 job ID も指定し、新しい job の lock を削除しない。
- 状態確認は認可後に行い、startup で全件を走査しない。同期 SDK の実行スレッド自体を強制停止する機構ではない。
- 再実行は利用者の明示操作で新しい job を作る。保存済み入力を引き継ぎ、profile fingerprint と task/context が一致する既存の抽出 checkpoint を再利用する。保存済み下書きは保持し、自動公開しない。

## 旧 Ontology 公開・推論

- 旧 global revision API の OWL2RL / SHACL worker は `NL2SQL_ONTOLOGY_PUBLISH_TIMEOUT_SECONDS`（既定600秒）の絶対期限を保存する。現在の Profile Markdown snapshot 公開とは別の経路であり、Profile を指定した旧公開要求は引き続き拒否する。
- GET は認可用 peek の後に store の最新状態を確認する。旧 job も作成日時から失効させ、inprocess の正常 shutdown は所有 job だけを off-loop で終了させる。startup は DB-free。
- queued の ETag を claim し、実行 ID を持つ worker だけが進捗・成果物を書ける。推論・検証後と公開 head 切替直前にも失効を確認し、二重配送では推論を繰り返さない。
- revision の `publish_job_id` / reasoning graph を照合する。既に公開 commit 済みなら immutable draft の Markdown コピーだけを補い、既存のコピーと head / revision 履歴を保持して job を完了させる。保存確認に失敗した場合は「版は公開済み」と明記し、再公開を案内しない。
- 期限は同期スレッドを kill しない。遅着処理の後続 commit を制限し、commit 境界での停止は取得時の照合で収束させる。

## 旧実データ検証 job

- `NL2SQL_ONTOLOGY_VALIDATION_TIMEOUT_SECONDS`（既定600秒）の絶対期限を保存し、旧 job は作成日時で判定する。認可済みの GET / 同じキーの再送で失効状態を回復する。再実行は新しいキーで明示的に開始する。
- queued（external worker の queued 由来 claim を含む）だけを CAS で開始し、実行 ID と取得済み ETag を保持する。running の再配送・期限切れ後の SQL 結果を破棄し、SQL の次の呼び出し・検証レポート commit 前にも失効を確認する。
- レポートを含む bundle と hash 付き完了証跡を artifacts の同一 transaction で保存する。job の完了保存だけが失われた場合、証跡から結果を復元し、Oracle 検証 SQL を再送しない。
- SQL_EXECUTE / Profile scope の認可、読み取り専用 SQL、sample_limit、検証後の Profile / schema fingerprint / bundle ETag 確認を維持する。sampled data を全 DB の検証済みとは扱わない。
- inprocess の shutdown は所有 job のみを off-loop で回復し、pool close 前に実施する。同期 SQL スレッドの強制 kill は行わない。現在の Markdown snapshot のデータ検証とは別の旧 async API が対象。

### DB 構造再取得の実行所有権 (#645)

- schema refresh の heartbeat / phase / 完了 / 失敗保存は、保存済みの `running`、`worker_id`、`attempt`、未失効 lease が一致する場合だけ受理する。例外処理で最新 job を読み直して新 worker の権限を借りない。
- catalog の差分適用は同じ transaction で job 行を lock して所有権を検証し、その後 catalog head / object / column / constraint / dependency を更新する。接管後の旧 worker は catalog や job を変更しない。
- lease 切れの再 claim は metadata の再取得として維持する。catalog commit 後に job 完了保存前で終了しても、次の worker は保存済み manifest と照合して完了できる。DDL・ユーザ SQL の再実行はこの worker の責務に含めない。

### SQL 生成の lease 接管と結果保存 (#646)

- claim の `worker_id` / `attempt` は実行 thread に固定し、terminal payload の worker field をクリアしても保存条件として保持する。別 thread の接管により process 内 cache が置換された場合も旧実行は新しい権限を借りない。
- heartbeat / phase / error / result の保存は永続 job 行の `running` と所有権・未失効 lease を確認する。結果と history は同じ transaction で保存し、遅着結果の history 追加も拒否する。
- 各 stage と最終保存前で所有権・キャンセルを確認する。外部呼び出し中の thread は強制停止せず、戻った時点で継続と保存を拒否する。既に実行を開始した read-only SQL は取り消せない場合がある。
- SQL job の同期実行中は専用 heartbeat thread が `min(30秒, lease / 3)`（lease は最低30秒）ごとに元の worker / attempt で時刻 field だけを条件付き更新する。長い生成・実行・結果整形も対象とし、進捗や結果の snapshot を heartbeat から書き戻さない。実行終了時に停止を通知し、DB I/O 中の thread の join は1秒までとする。一時的な保存障害はログに残して次の tick で再試行するが、失効・接管後の lease は復活させない。
- GET は実行元 process を含めて永続 job を正本とする。結果保存障害時の手元の結果＋警告は、元の lease と所有権がなお有効な場合だけ表示し、新 worker の状態を隠さない。
- GET の DB 読取り後、cache 更新の lock 内で現在の entry を再確認する。読取り途中で claim された実行 object と所有権を、取得した古い snapshot で置換しない（#650 / #651）。

### バックグラウンド起動経路の横断確認 (#639)

2026-09-15 時点の `threading.Thread` / `ThreadPoolExecutor` / `create_task` の起動経路を再検索した。SQL 生成・schema refresh 以外は次の契約で処理する。

| 機能 | 回復と再実行の契約 | 検証 |
| --- | --- | --- |
| Ontology AI 構築 | deadline / heartbeat lease / 実行 ID。中断を表示し、入力と checkpoint を保持した明示的 retry | #636 / #641 |
| 旧公開・推論 | deadline / 実行 ID。既に公開された revision の receipt を照合し、公開操作を繰り返さない | #637 / #642 |
| 旧実データ検証 | deadline / 実行 ID / ETag。保存済み report receipt で回復し、SQL を再送しない | #638 / #644 |
| Markdown 公開準備 | deadline / shutdown / ETag。解析の自動再送なし | #633 / #634、`test_nl2sql_markdown_preparation_lifecycle.py` |
| SQL 生成評価 | lease / worker / attempt による条件付き保存。中断 attempt を timeout と記録し、遅着結果を破棄 | `test_nl2sql_quality_evaluation.py` |

実 DB は job テーブルの SELECT 集計で確認し、回復のために生成・公開・反映操作を呼ばない。2026-09-15 12:18 JST の対象集計では、ontology / SQL job / schema refresh / 評価に pending・running 等の残留はなかった。これは同時点の観測であり、将来の全外部障害を保証するものではない。

## 一括の LLM・Embedding の呼び出し（#2004）

多数の LLM・Embedding を呼ぶ一括の処理は、共通の上限つきの並行の実行器（`pr_backend_core.bounded_calls`）を
`app/features/nl2sql/ai_calls.py` から使う。同期のコード（thread）からは `SyncBoundedCaller`（`sync_ai_caller`）を通し、
1 つの処理の呼び出しは 1 つの実行器を共有する。エラーの種類ごとの扱いは #2002 の表と同じ（429 は全体で待って並行の数を
半分にし、成功が続けば上限まで戻す。5xx・接続・時間切れは種類ごとに再試行し、`Retry-After` を守る。権限・認証・モデルの設定の
誤り・割り当ての使い切りは再試行せず、処理を止める）。実行器を通す呼び出しは client の再試行を切る（Enterprise AI の direct
client は `max_retries=0`。埋め込みの client は自前の再試行を持たない）。

| 処理 | 並行の上限（`.env`） | 結果の順 | 権限・設定の誤り |
|---|---|---|---|
| 業務プロファイルの AI 構築の抽出の組（Q/A・資料）と、資料の PDF の文字の無いページの OCR | `NL2SQL_ONTOLOGY_BUILD_CONCURRENCY`（既定 4） | 組・ページの順に取り込む（統合の順は変わらない）。組の再試行・二分割は今までどおり | 残りの組を呼ばずに job を失敗にする（`LLM_CALL_ABORTED`） |
| コメント・アノテーションの SQL の生成の組（ドメインは前の組の結果を渡すため順に呼ぶ） | `NL2SQL_METADATA_SQL_CONCURRENCY`（既定 4） | 組の順 | 残りの組を呼ばず、組ごとに規則ベースへ戻す（今までと同じ案内） |
| Embedding（96 件ずつ。Feedback の索引・質問分類の学習・オントロジーの埋め込み） | `NL2SQL_EMBEDDING_CONCURRENCY`（既定 2） | 入力の順 | `EmbeddingClientError`（呼び出し側の縮退のまま） |
| SQL生成評価の試行（ケース × engine × 繰り返し。#2059） | `NL2SQL_QUALITY_EVALUATION_CONCURRENCY`（既定 2、1〜8） | 試行はケース・engine・繰り返しの順に始め、結果はその位置に書く（終わる順は問わない） | 残りの試行を始めずに job を失敗にする（`error_message` に直す所） |

SQL生成評価（#2059）の扱い:

- 試行の SQL の生成と判定（LLM-as-judge）は 1 つの実行器を共有する。規則は RAG・Agent の品質評価と同じで、429 だけを
  全体で待って送り直し（3 回まで）、時間切れ・5xx などはその試行の失敗のまま記録する（送り直さない）。権限・認証・モデルの
  設定の誤り・割り当ての使い切りは評価を止める。判定の再現性は、Enterprise AI の `temperature=0` と、結果を
  ケース・engine・繰り返しの位置に書くことで保つ。
- job の記録は実行中の試行を複数持つ（`running_attempts`。試行ごとのケース・engine・繰り返しと開始時刻。`PAYLOAD_JSON`
  の中なので表の変更は無い）。lease の再取得のときは、試行の timeout を過ぎた試行をそれぞれ時間切れの結果として記録し、
  過ぎていない試行は次の worker がもう一度実行する。job の記録の読み書きは worker の中の lock で直列にし、別の worker との
  競合は今までどおり fence（`worker_id`・`attempt_no`）が防ぐ。
- 画面の「現在のケース」などは、実行中の試行のうち最も前に始めた試行（時間切れに最も近い試行）を出し、診断の行に
  「同時に実行中 N / M 試行」を出す。
