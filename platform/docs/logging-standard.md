# 共通の診断ログ契約（#858）

## 出力と時刻

Python の診断は `pr_backend_core.configure_logging` が所有する stderr handler から UTF-8 JSON Lines に出す。stdout は既存 CLI の SQL / JSON report に残す。共通初期化を繰り返しても所有 handler は1つで、外部 collector は消さない。Uvicorn / Gunicorn の native handler は共通 handler に接続する。

`schema_version=1`、`timestamp`、`level`、`name`、`event`、`message`、`service_name`、`service_version`、`environment`、`component`、`process_id` を基本とする。`timestamp` は `Asia/Tokyo` の RFC3339 millisecond、必ず `+09:00` を付ける。OS / process timezone や epoch は変更しない。

Nginx access は `escape=json`、同じ identity / HTTP 語彙と `timestamp_epoch`（秒）を記録する。Nginx の local-time 文字列から推測せず、共通 viewer / JSON export が同じ瞬間の JST へ変換する。native error log は原形式を維持する。旧 naive アプリ時刻は journald epoch があればそれを正本にし、なければ UTC として変換する。元の timezone が不明な native text を JST と断定しない。raw は原時刻を保持する。

OpenTelemetry への対応は `timestamp`→Timestamp、`level`→SeverityText、`message`→Body、service identity→Resource、event / domain fields→Attributes。有効な W3C `traceparent` の trace は `trace_id`、受信 span は `parent_span_id` に残す。ローカル span を開始していないため `span_id` は捏造しない。既存の業務 `trace_id` は `domain_trace_id` に分ける。独自 JSON は OTLP wire protocol ではない。

## Entry point と責務

| 対象 | bootstrap / component |
|---|---|
| RAG / NL2SQL / Agent API | 各 `app/main.py` / `api` |
| RAG ingestion worker と subprocess | `ingestion_worker.py` / `ingestion_job_runner.py` / `worker`・`ingestion-child` |
| NL2SQL schema-refresh・ontology・synthetic・quality worker | `app/cli/nl2sql_*_worker.py` / worker の種類 |
| Agent dedicated dispatcher | `runtime_dispatcher.main` / `worker` |
| RAG parser・preprocess・5 pipeline factory | `configure_http_logging` / `microservice` |
| ASR | 独立 app の `configure_http_logging` / `microservice` |
| offline CLI | `configure_cli_logging` / `cli`（DB settings は初期化しない） |
| backend template | `configure_logging` / `api` |
| browser の ErrorBoundary / 離脱ガード診断 | 共有 `logBrowserDiagnostic` / `frontend` |

製品は既存 `*_LOG_LEVEL`・version・environment・service identity を渡す。微サービスは RAG の runtime environment を使い、製品 backend / DB / 認証を import しない。重い OCR 依存なしの factory テストを RAG Backend CI で実行する。

HTTP の相関は ASGI 境界で bind し、body / SSE 終了・例外・取消後に reset。sync threadpool と WebSocket も同じ context を使う。応答の `X-Request-ID` は検証済みの値。不正な ID は新規発行する。MCP / parser / preprocess / pipeline の outbound に request / W3C trace と安全な Run / job の診断 ID を伝播する。`X-Correlation-Run-ID` / `X-Correlation-Job-ID` は診断専用で、認証 claim や scope として信用しない。process 境界は永続 job の ID を claim / 子プロセスで bind する。

HTTP summary は hop ごとに `http_access` を1件。route template、status、body 終了までの `duration_ms`、header 到達までの `headers_duration_ms`、outcome を残す。MCP の HTTP 200 内の tool error は既存の tool execution event が正本。成功 GET `/api/services/{service_id}/status` のみ抑制し、401 / 403 / 5xx / stream 中断は保持する。未解決 route は `/unmatched`、Nginx は `/api/*` 等に粗粒度化し、入力値を route に入れない。

## Event / level

既存の安定した英語 event 名は維持する。英語 identifier だけの既存 message は event にも対応する。自由形式の既存日本語 message は `application_log`、第三者は `dependency_log` とし、SDK の自由本文は通常診断に持ち込まない。

外部の HTTP クライアント・SDK の logger（`httpx`・`httpx2`・`httpcore`・`httpcore2`・`urllib3`・`requests`・`oci`）は既定で WARNING にし、呼び出しごとの INFO（本文を伏せると情報が無い行）を出さない（`pr_backend_core.logging.DEPENDENCY_LOGGER_LEVELS`。#1935）。戻すときは `configure_logging` の `quiet_loggers` に同じ名前で level を渡すか、`*_LOG_LEVEL` を DEBUG にする。HTTP の呼び出しの情報が要るなら、自前の構造化ログ（依存先の呼び出しの所要時間など）に持つ。

| event | level / 用途 |
|---|---|
| `http_access` | INFO 正常、WARNING 4xx、ERROR 5xx / 中断 |
| 既存 worker / Run / retry event | 起動・完了 INFO、再試行・縮退 WARNING、最終失敗 ERROR |
| `dependency_log` | 元 severity を維持し、本文は安全な共通説明 |
| `card_render_failed` | ERROR、Error の型・request ID のみ |
| `unsaved_changes_blocker_missing` | WARNING、配置不備を1回通知 |
| `logging_format_failed` / `logging_sink_failed` | ERROR、安全な fallback。再帰 logger を呼ばない |

security audit は既存 Oracle / 権限 / 保存処理を維持する。診断の成功・poll 抑制・logger level を audit 永続化の条件にしない。既存 Langfuse / OTLP の保存値・epoch は変更しない。

## 脱機密化・上限

producer は business payload / prompt / SQL / 質問 / 回答 / 文書本文 / 認証情報を渡さない。共通境界は sensitive key を入れ子まで除去し、URL userinfo/query/fragment、Bearer 等も二次防御する。未知 object を `str()` に変換せず型だけ残す。例外は型・既知 ORA/DPY/DPI コード・最大16 stack frame の basename/function/line・最大5 cause 型。本文・locals・source line は残さない。

文字列1024文字、collection32件、深さ5、record全体で最大256 node を評価、record32 KiB。循環・非有限数・不正 format を安全に処理し、切り詰めは `truncated=true`。改行は JSON escape。format / sink fault で本来の例外を隠さない。browser は静的 event と型・検証済み ID の allowlist を console JSON に出し、Error の原文 / stack と新しい遠隔 telemetry は出さない。

合成10,000件（349 byte/event、総量3,490,000 byte）で formatter p95 0.0171ms、`asyncio.sleep(0)` を挟む event-loop gap p95 0.0213ms。共通 poll 抑制と上限を適用し、今回 process 内 queue は導入しない。この値は formatter / scheduler の測定で実ホスト sink latency の保証ではない。stderr→ローカル journald は同期出力を維持する。遠隔 sink の追加時は bounded queue・enqueue 前の安全化/context snapshot・drop count・ERROR fallback・停止時 flush timeout を別途検証する。

## 閲覧・export・運用

各製品の `scripts/tail-logs.sh` は `platform/scripts/tail-logs.sh` を使う。service を変更する操作はしない。

```bash
bash rag/scripts/tail-logs.sh --backend --level WARNING --no-follow
bash rag/scripts/tail-logs.sh --unit production-ready-rag-parser-docling --request-id request-1
bash nl2sql/scripts/tail-logs.sh --workers --job-id job-1 --json --no-follow
bash agent/scripts/tail-logs.sh --all --run-id run-1 --json --no-follow
```

JSON/native text の混在、UTF-8、journald byte array、破損行で後続を失わない。`--json` は安全な scalar field のみで JST を明示し、旧自由本文 / native text は export しない。`--raw` は原文を含む管理者の明示的な閲覧であり、そのまま Issue / public artifact に貼らない。ID filter は JSON の完全一致。raw と ID / JSON filter は併用できない。

journald / `/var/log/nginx` の既存権限・rotate を使う。デプロイ前に `journalctl --disk-usage`、`systemd-analyze cat-config systemd/journald.conf`、`logrotate --debug /etc/logrotate.conf` で容量 / 保持 / rotate を確認し、運用責任者が `SystemMaxUse` / `MaxRetentionSec` と Nginx の世代・日数を決める。本変更は既存データを vacuum しない。ログ閲覧は運用グループ、diagnostic export は `umask 077` で保存し、共有前に secret / 個人情報の有無を確認する。audit の保持は診断ファイルと別に管理する。初期化 scripts の共有 Nginx format は再配備で入る。既存 Compute は変更差分と `nginx -t` を確認してから反映する。実 OCI の保持値・Gunicorn fork / journald 配備は CI のスタブ確認と分けて記録する。
