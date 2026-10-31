# production-ready-backend-core (`pr_backend_core`)

No.1 Production Ready 製品群（**RAG / NL2SQL / Agent**）の backend が共有する FastAPI インフラ。
「全サービスが必ず使う」プラットフォーム機能だけを持ち、業務ロジックは持たない。

技術スタック（確定・RAG を正本）: **Python 3.12 + FastAPI + Pydantic v2 + uv**。
依存は軽量に保つため **oci / oracledb は含めない**（各サービスが自分で持つ）。

## 含むもの

| モジュール | 役割 |
|---|---|
| `create_app(...)` | CORS・request-id/メトリクス・例外 envelope・health/ready・/metrics を備えた app factory |
| `config.BaseServiceSettings` | `.env` ベースの共通設定基底（app_version / log_level / environment / cors_origins） |
| `config.product_settings_config` | 製品の Settings の `model_config`。共通 `platform/.env` → 製品の `backend/.env` の順に読み、共通の属性（`PLATFORM_SETTING_FIELDS`）は `PLATFORM_*`、それ以外は製品の接頭辞の環境変数名にする（#211。旧名は読まない） |
| `logging.configure_logging` | JSON 構造化ログ（サービス固有のノイズロガー抑制を注入可能） |
| `schemas` | `ApiResponse[T]` / `Page[T]` / `HealthData`（**RAG 実証済み envelope を標準採用**） |
| `api.errors` | HTTPException / 検証 / 未処理例外 → `ApiResponse` 統一ハンドラ |
| `api.validation` | 入力の検証エラー（422）の利用者向けの日本語の文・位置（`cases[0].query`）と problem 契約の `field_errors`（技術的な原文は `raw_location` / `raw_message`。#1065） |
| `api.pagination` | `paginate(...)` |
| `api.health` | `create_health_router(version_getter, readiness_checks_getter)` |
| `observability` | `MetricsMiddleware`（request-id 付与 + Prometheus）/ `record_http_request` / `metrics_asgi_app` |
| `security.cors` | `configure_cors` |
| `oracle_session.init_oracle_session` | Oracle の接続ごとの初期化（result cache を使わない。#333） |
| `oracle_pool.SharedOraclePool` | 接続引数ごとに 1 つの python-oracledb の pool を遅延作成して共有する（スレッド安全。引数が変わると作り直し、`close()` で閉じる。新しい接続に `init_oracle_session`。#793）。`oracledb` は使うときに import する |
| `bounded_calls.BoundedCaller` | 外部の AI の API（LLM・VLM・Embedding）を上限つきの並行で呼ぶ実行器（#2002）。エラーを種類（`ErrorKind`。429・quota・5xx・接続・TLS・timeout・401・403・404・400・413・安全の拒否・出力の形・打ち切り）に分け（`classify_error`。差し替え可）、種類ごとの再試行（指数の backoff と full jitter・`Retry-After` の優先）、全体で共有する待ち、AIMD の並行の数、再試行の予算、circuit breaker、件の最後の再試行と記録、heartbeat の event を持つ。この部品を通す呼び出しは SDK の再試行を切る。時計・乱数・sleep は差し替えられる。同期のコード（worker thread）からは `SyncBoundedCaller`（専用の thread の event loop へ投げ、thread をまたいで上限・待ちを共有する。#2004） |
| `internal_http.http_client_options` / `is_internal_url` | 宛先が内部（loopback・private・link-local・`localhost` / `*.local`・unix socket）なら環境のプロキシを使わない httpx の client の引数（`{"trust_env": False}`）を返す。外部の宛先は空（環境のプロキシを使う）。httpx は import しない（#852。backend-standard の「内部の HTTP と環境のプロキシ」） |
| `source_text` | 検索の結果と観測が返す本文（根拠の chunk など）の大きさの規則（#3321）。検索の結果は 1 件ごとに全文で、応答の合計の予算（`SOURCE_TEXT_RESPONSE_BUDGET_TOKENS`）に順位の順に入れ、入らない件は本文を省く（`fit_source_texts`。`text_omitted`）。1 件の上限（`SOURCE_TEXT_ITEM_MAX_TOKENS`）を超える件だけ、質問に当たる部分を中心に本文は文・段落、表は見出しの行を残して行の境界で切る（`excerpt_source_text`。`excerpt_range`・`header_range`・`next_offset`）。続きの読み取りは位置から文字数の上限に入る最後の文・段落（表は行）の境界までを返し、表の続きは見出しの行を付ける（`read_source_window`。#3344）。観測は全文を件数と 1 ページの予算でページに分ける（`page_source_texts`）。RAG の MCP の根拠・読み取りのツールと Agent の公開 MCP の `sources`・観測が使う |
| `llm_request_params` / `llm_request_params_httpx` | モデルが受け付けない LLM の要求の引数（`Unsupported parameter: 'temperature'` などの HTTP 400）を外して 1 回だけ送り直し、（接続の URL, モデル ID）ごとにプロセスの間だけ覚えて次から最初に外す（#3539。LiteLLM の `drop_params` と同じ型）。外すのは許可の一覧（サンプリングと生成の調整と、引数そのものを受け付けないときの推論の量）の top-level の引数だけ。判定・記憶・payload の絞り込みは標準 library だけ（`llm_request_params`）、httpx の client の `send` を包む部品（`ParameterDroppingClient` / `AsyncParameterDroppingClient` / `send_dropping_parameters` / `asend_dropping_parameters`）は `llm_request_params_httpx`（httpx は使う側が依存に入れる）。モデルのテストは `collect_dropped_parameters` で外した引数を集める。テストは `reset_dropped_parameters` で状態を捨てる |
| `token_estimate.estimate_input_tokens` | 本文の入力 token の見積もり（ASCII は 4 字で 1、それ以外は 1 字で 1。3 製品で共通の 1 つ。`pr_system_settings.model` も同じものを使う） |
| `spreadsheet.read_excel` | Excel（.xlsx / .xlsm / .xls）を、シートごとの記録（`SheetRecordsDocument`。表の行・手順書の手順・帳票の「ラベル: 値」・1 シートの複数の表・図形の文字・フォームコントロールのチェックの状態、元の行番号とセル範囲・表示の値・判定の根拠）にする（#2525）。選択肢は `ExcelOptions`。サービス・HTTP・DB に依存しない。openpyxl / xlrd は関数の中で import し、使う側が依存に入れる（RAG の前処理 `excel_to_jsonl`・チャットの添付が同じ読み方を使う） |

## 新サービスの最小構成

```python
# app/main.py
from pr_backend_core import create_app
from app.api.router import api_router
from app.settings import get_settings
from app.readiness import readiness_checks

settings = get_settings()
app = create_app(
    service_name="production-ready-nl2sql",
    version=settings.app_version,
    cors_origins=settings.cors_origins,
    api_router=api_router,
    readiness_checks_getter=lambda: readiness_checks(get_settings()),
)
```

## 既成サービス（RAG）の部分採用

RAG のように独自の lifespan / 認証ミドルウェア / DB 依存 readiness を持つサービスは、
`create_app` を使わず各部品を個別採用できる（envelope / 例外ハンドラ / health router /
MetricsMiddleware / configure_logging を import）。

## 開発

```bash
uv sync --dev
uv run pytest          # 純インフラなので Oracle 不要で全テスト実行可
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```

## 配布 / 依存方法

開発時は各サービスの `pyproject.toml` で uv の path source を使う:

```toml
dependencies = ["production-ready-backend-core"]

[tool.uv.sources]
production-ready-backend-core = { path = "../../platform/packages/backend_core", editable = true }
```

> path source を変更したら **`uv lock` の再生成**が必要。安定後は GitHub Release / index 配布へ切替可能。

### 呼び出し本文の streaming の記録（#3899）

`CallStreamCapture` は Responses API の本文・返却された思考・tool arguments の delta を上限付きでメモリに合成する。途中の checkpoint は最大 5 秒ごとに最新の snapshot を渡し、完了の応答を優先する。取消・失敗・完了イベントの欠落では受信済み prefix を保持する。delta ごとの DB 行・履歴は作らず、製品の既存の最新状態の保存と非同期の書き手を使う。記録できる本文の上限は 64,000 文字、配列は 1,000 件で、省略を明示する。JSON の値を縮めてから serialize し、途中で切れた JSON を新規に作らない。

共通の span 表には本文を複製しない。画面の本文・embedding の要約は製品の snapshot から投影し、別の要約を DB に保存しない。`PLATFORM_TRACE_RETENTION_DAYS` は span 表の期限であり、製品の本文を削除する設定ではない。
