# backend — production-ready RAG API

FastAPI + OCI Enterprise AI（LLM/VLM）+ OCI Generative AI（埋め込み/リランク）+ Oracle AI Database。

## セットアップ

```bash
uv sync                       # 依存解決（共有 package rag-parser-core を path 依存で取り込む）
cp ../../platform/.env.example ../../platform/.env   # 3製品共通の設定（PLATFORM_*。初回だけ）
cp .env.example .env          # RAG 固有の設定（RAG_*）
uv run uvicorn app.main:app --reload
# -> http://localhost:8000/api/health（API ドキュメント /docs は公開しない。#748）
```

本番は `rag/init_script.sh` が作る systemd の unit（`production-ready-rag-backend.service`）で、
Gunicorn + `uvicorn.workers.UvicornWorker`（`127.0.0.1:8000`、workers 2、timeout 60 秒）として動きます。
取込キューは別の unit（`production-ready-rag-ingestion-worker.service`）が消費します。Docker は使いません（#286 / #356）。
local 開発だけ `uvicorn --reload` を使います。前処理 / parser の開発環境は `../scripts/rag-services.sh`（[docs/deployment.md](../docs/deployment.md)）。

外部 parser(Docling / Unstructured / MinerU / Dots.OCR)は **backend には載せず**、
独立した FastAPI マイクロサービス(`services/parsers/<name>`)で動かします。backend は取込時に
`app.clients.parser_service` で HTTP 委譲します。未達・空振り時は別経路へ縮退せず取込を止めます。
既定の解析エンジンは Docling(`RAG_PARSER_ADAPTER_BACKEND=docling`、#286)です。Unstructured などは
「取込・検索・回答設定 › 文書解析」で明示的に選びます。
重い parser 依存は runtime / 既定 `uv sync` に入りません。必要時のみ単一 adapter を per-adapter extra
(`uv sync --extra docling` 等、ローカルデバッグ用)で導入できます。
Marker / Unlimited-OCR / GLM-OCR への対応は削除しました(#270)。保存済みの設定・文書レシピ・KB 構築設定に
これらのエンジンが残っていても、読み込み時に既定の解析エンジン(レシピと KB は global 既定の継承)として扱います。
> 依存(`rag-parser-core` path 依存)を追加・変更したら **`uv lock` の再生成**が必要です
> (配備は `uv sync --locked` で lock どおりに venv を作ります)。

## 開発コマンド

```bash
uv run pytest                 # テスト
uv run ruff check .           # lint
uv run ruff format .          # フォーマット
uv run mypy .                 # 型チェック
uv run bandit -r app          # セキュリティ
```

## 主要 API

| API | 用途 |
|---|---|
| `GET /api/health` | 稼働確認。OCI 前提の稼働 message を返す |
| `GET /api/ready` | 依存設定を含む readiness。未設定時は 503 |
| `GET /api/ready/database` | 画面の DB ゲートが使う DB の状態（3製品共通の判定と契約。常に 200。ログイン不要） |
| `POST /api/documents/upload` | ドキュメントファイルを Object Storage 境界へ保存 |
| `GET /api/documents?status=UPLOADED&q=manual&limit=50&offset=0` | 文書一覧をページング・状態・ファイル名で絞り込み |
| `POST /api/documents/{id}/ingestion-jobs?force=false&phase=PREPROCESS` | 保存済みドキュメント（既定レシピ）を永続取込 job としてキュー投入 |
| `POST /api/documents/{id}/recipes/{recipe_id}/ingestion-jobs` | 処理レシピ単位で取込 job をキュー投入 |
| `POST /api/documents/{id}/recipes/{recipe_id}/approve` | 確認待ちのレシピ（ファイル準備・抽出・Chunk）を承認して次の工程の job を投入。抽出の修正を同時に保存できる |
| `PATCH /api/documents/{id}/recipes/{recipe_id}/review-edits` | 確認待ち（REVIEW）のレシピの抽出の修正を保存（job は投入しない）。修正は要素・表セルごとに `base_text`（編集を始めたときの文）を持ち、レシピの行ロックの下で最新の抽出結果へ当てる。別の箇所の並行の修正は両方残し、同じ箇所がほかの保存で変わっていれば 409 |
| `GET /api/documents/ingestion-jobs?status=QUEUED` | 取込 job 履歴・状態をページング取得 |
| `POST /api/documents/ingestion-jobs/drain` | 永続化済み QUEUED job を再実行 |
| `POST /api/documents/ingestion-jobs/{job_id}/retry` | 失敗・完了・キャンセル済み job の対象文書を新規 job として再投入 |
| `POST /api/documents/ingestion-jobs/{job_id}/cancel` | QUEUED/RUNNING job を CANCELLED にし、worker 終了時の上書きを防ぐ |
| `POST /api/search` | 回答フロー（質問の理解・質問拡張・hybrid 検索（RRF）・rerank・CRAG・small-to-big）による citation-grounded 回答生成 |
| `POST /api/search/stream` | SSE 形式で回答・引用をストリーミング |
| `POST /api/evaluation/jobs/run` | golden set 評価を job として投入（#390） |
| `GET /api/evaluation/jobs/{job_id}` | 評価の job の進捗と結果（`POST .../cancel` で取り消し） |
| `POST /api/evaluation/run` | golden set 評価（同期。互換のため残す。評価全体を 600 秒で打ち切る） |
| `POST /api/mcp` | Agent 向けの MCP サーバー（Streamable HTTP の JSON 応答）。サービストークンの利用者として検索・回答を呼ぶ（下記「MCP」。#232。チャットは提供しない。#787） |
| `GET /metrics` | Prometheus metrics |

## OCI / Oracle 実装

Backend は常に以下の OCI / Oracle 実装を使います。local / oci の実行モード切り替えはありません。

- `OciEnterpriseAiClient`: OCI Enterprise AI の VLM / LLM
- `OciGenAiClient`: OCI Generative AI の Cohere Embed v4 / Rerank v4 fast
- `OracleClient`: python-oracledb pool + Oracle AI Vector Search / Oracle Text
- `ObjectStorageClient`: OCI Object Storage SDK による原本ファイル保存 / 取得

設定は、3製品共通の設定（OCI 認証・アップロード保存先・モデル・データベース。`PLATFORM_*`）を共通 `.env`（リポジトリの `platform/.env`、場所は `PLATFORM_ENV_FILE` で上書き可）、RAG 固有の設定（`RAG_*`）を `backend/.env` に置きます。環境変数 → 共通 `.env` → `backend/.env` の順に読み、旧名（接頭辞のない名前や `HF_TOKEN` / `HF_ENDPOINT`）は読みません（#211。既存環境の移行は [docs/deployment.md](../docs/deployment.md) の「既存環境の更新手順（#211）」）。システム設定画面（OCI 認証・アップロード保存先・データベース）は共通 `.env` に、RAG 固有の設定画面は `backend/.env` に保存します。

モデル設定画面で保存した Enterprise AI / Generative AI 設定は `PLATFORM_MODEL_SETTINGS_FILE` の JSON を正本として永続化します。既定は `model-settings.json` で、相対パスは共通 `.env` と同じディレクトリ（`platform/`）を基準に解決され、3製品で共有します（知らない key は保存時に残します）。`.env` は初期値・bootstrap 用で、保存済み JSON が存在する場合は JSON が優先されます。Enterprise AI API key は JSON には書かず、共通 `.env` の `PLATFORM_OCI_ENTERPRISE_AI_API_KEY` に保存します（3製品共通の `pr_system_settings.model`。#103）。文書解析の画面で保存した解析エンジンと外部接続は、共有の JSON ではなく RAG 専用の `RAG_PARSER_SETTINGS_FILE`（既定 `parser-settings.json`。相対パスは `backend/` 基準）に保存します（#1701）。文書解析の外部 parser の API key（`RAG_PARSER_MINERU_API_KEY` / `RAG_PARSER_DOTS_OCR_API_KEY`）は RAG 固有のため `backend/.env` に保存します（#106 / #211）。旧形式のモデル設定の JSON に残っている key は、次にモデル設定を保存したときに `.env` へ移します。画面で保存した key は、プロセスの環境変数より優先されます（画面で削除すると環境変数の値に戻ります）。環境変数から来ただけの key は `.env` に書きません。backend は親ディレクトリを `0700`、ファイルを `0600` に補正して保存します。

`OciEnterpriseAiClient` は Enterprise AI の実 endpoint / model deployment / gateway が返す JSON envelope の揺れを吸収します。VLM は `structured_extraction`、`extraction`、`prediction(s)`、`output(s)`、JSON 文字列などから `StructuredExtraction` を取り出して Pydantic で検証します。`StructuredExtraction` は `raw_text` と `elements` を持ち、ページ、読み順、見出し、本文、リスト、表、図、header/footer などを同じ JSON で表せます。LLM は `answer`、`text`、`output_text`、`generated_text`、`choices[].message.content`、`inference_response` などから回答 text を取り出します。独自 gateway がさらに深い envelope を返す場合は `PLATFORM_OCI_ENTERPRISE_AI_LLM_RESPONSE_PATH` / `PLATFORM_OCI_ENTERPRISE_AI_VLM_RESPONSE_PATH` に JSON Pointer 形式(`/payload/results/0/text` など)を設定して候補 node を明示できます。いずれも OCI Generative AI chat API には接続しません。

Enterprise AI endpoint の request shape が標準 payload と異なる場合は、`PLATFORM_OCI_ENTERPRISE_AI_LLM_PAYLOAD_TEMPLATE` / `PLATFORM_OCI_ENTERPRISE_AI_VLM_PAYLOAD_TEMPLATE` に JSON object template を設定できます。`${prompt}`、`${context}`、`${mime_type}`、`${data_base64}`、`${structure_instructions}` などの文字列 placeholder と、`"${messages}"`、`"${parameters}"`、`"${structured_extraction_schema}"` などの object placeholder を使えます。テンプレート未設定時は標準 payload を使い、アップロード時の MIME type と構造化抽出 instructions を VLM input に渡します。

VLM input の搬送方式は `PLATFORM_OCI_ENTERPRISE_AI_VLM_INPUT_MODE` で選べます。既定の `auto` は画像を inline data URL、PDF など非画像を OCI Enterprise AI `/files` API にアップロードして `file_id` を `/responses` へ渡します。`files_api` は画像も含めて明示的に `/files` 経由にし、`inline_image` は画像だけ inline で送ります。設定画面では「VLM 入力方式」で選択し、API パスは通常 `/responses` のままにします。

Enterprise AI endpoint の request / response 契約だけを Oracle や Object Storage から切り離して確認する場合は、`app.rag.enterprise_ai_probe` を使います。`--dry-run` は endpoint へ送信せず、URL、template 使用有無、payload key、payload shape、JSON byte 数だけを出します。本実行は LLM / VLM を直接呼び、回答本文や OCR 本文は出さず、text 文字数・element 件数などの非機密 summary だけを返します。

```bash
uv run python -m app.rag.enterprise_ai_probe --surface both --dry-run
uv run python -m app.rag.enterprise_ai_probe --surface llm
uv run python -m app.rag.enterprise_ai_probe --surface vlm --mime-type text/plain
```

Embedding は Cohere Embed v4 / Oracle `VECTOR(1536, FLOAT32)` に合わせて 1536 次元を固定契約にしています。`OciGenAiClient` は OCI Generative AI Inference SDK の `embed_text` / `rerank_text` を使い、検索 query は `SEARCH_QUERY`、文書 chunk は `SEARCH_DOCUMENT` として embedding します。`OciGenAiClient.embed()` は返却件数と次元数を検証し、`OracleClient` も chunk 保存・vector search の入口で再検証します。`OciGenAiClient.rerank()` は Cohere Rerank v4 fast の返却 index が候補範囲内で重複せず、返却件数が `top_n` 以内、score が finite number であることを検証してから pipeline に渡します。

Oracle は共有 connection pool を遅延初期化し、アプリ終了時に閉じます。document/chunk の永続化、HNSW vector index + `FETCH APPROX ... WITH TARGET ACCURACY` による vector search、Oracle Text `CONTAINS` による keyword search を同じ tenant filter 付きで実行します。query 側の approximate search 精度は `RAG_ORACLE_VECTOR_TARGET_ACCURACY` で調整できます。

Object Storage は `PLATFORM_OBJECT_STORAGE_REGION` / `PLATFORM_OBJECT_STORAGE_NAMESPACE` / `PLATFORM_OBJECT_STORAGE_BUCKET` を使って OCI SDK の `put_object` / `get_object` を呼び出し、保存後は `oci://namespace/bucket/key` を document table に保存します。取得時は URI の namespace / bucket が設定と一致することを検証し、別 bucket の object を誤って取込しないようにします。

## 認証

`PLATFORM_AUTH_MODE=local` ではログインを要求しません。全権限・対象範囲の制限なしのローカル利用者として動き、UI もログイン画面とログアウト導線を表示しません。開発・CI の既定値です。

`PLATFORM_AUTH_MODE=authenticated` では、3製品共通の認証（`pr_system_settings.auth`。#212 / #214）でログインします。

- 利用者: 構成管理者 `system_admin`（共通 `.env` の `PLATFORM_ADMIN_LOGIN_USER_ID` / `PLATFORM_ADMIN_LOGIN_USER_PASSWORD`）と、「ユーザーとロール」で作る DB ユーザー（`PLATFORM_USERS` などを NL2SQL / Agent と共有）。
- セッション: Cookie `pr_session`（HttpOnly）と `pr_csrf`（名前は共通 `.env` の `PLATFORM_AUTH_SESSION_COOKIE_NAME` / `PLATFORM_AUTH_CSRF_COOKIE_NAME`）。3 製品で共有し、1 回のログインで NL2SQL / Agent も使えます。ログアウトは全製品で共通です（#1565）。更新系の API は `X-CSRF-Token` header に `pr_csrf` の値が必要です。期限・ロック・パスワード方針・`Secure` 属性は共通 `.env` の `PLATFORM_AUTH_*`。
- 認可: 全 API は router の dependency で確認し、`app/security/permissions.py` の manifest に登録されていない API は拒否します（公開は `/health`・`/ready`・`/ready/database`・`/auth/login` だけ）。
- 権限: ロールごとにメニュー権限（`menu.*`）と、`rag.search_answer_profiles.manage`（全検索・回答プロファイル・検索・回答プロファイルの作成とアーカイブ）、`rag.knowledge_bases.manage`（全ナレッジベース・KB の作成とアーカイブ）、`rag.feedback.manage`（承認 FAQ への反映と、他の利用者の保存済みの回答の表示・評価・削除。#304）、`rag.system_tables.manage`（システムテーブルの初期化・再作成）を付けます。「RAG セキュリティ > 権限管理」で編集し、`RAG_ROLE_PERMISSIONS` / `RAG_ROLE_SEARCH_ANSWER_PROFILES` / `RAG_ROLE_KNOWLEDGE_BASES` に保存します。
- 対象範囲: 検索・回答プロファイル・ナレッジベースを割り当てたロールの利用者は、その検索・回答プロファイル・ナレッジベース（と、そこに属する文書・回答履歴・フィードバック・会話）だけを使えます。範囲外の検索・回答プロファイルは 404、検索・回答プロファイルの KB を 1 つも許可されていない検索・チャットは 403（`error_code: RAG_SCOPE_FORBIDDEN`。画面はその場で理由を表示する。#224）です。検索・回答プロファイルで検索させるには、その検索・回答プロファイルのナレッジベースも許可してください。
- 保存済みの回答: 回答履歴（`GET /api/search/answers`。総件数つきのページング）・詳細・評価は、回答を生成した利用者（持ち主）の回答だけが対象です。SYSTEM_ADMIN と `rag.feedback.manage` を持つ利用者は全員の回答を扱えます（検索・回答プロファイルの範囲の制限は別にかかる）。参照するナレッジベースが 0 件の検索・回答プロファイルでの検索・チャットは 409 です（#304）。

## MCP（Agent からの呼び出し）

`POST /api/mcp` は Agent（Production Control Plane）が RAG を利用者として呼ぶための MCP サーバーです（#232。共通部品は `pr_backend_core.mcp`、契約は `platform/docs/backend-standard.md`「製品間の連携」）。`initialize` / `ping` / `tools/list` / `tools/call` だけを扱い、GET は 405 です。

- 認証: `Authorization: Bearer <サービストークン>`（`aud=rag`、`sub`=利用者の `user_uuid`、既定 60 秒）。署名鍵は共通 `.env` の `PLATFORM_SERVICE_TOKEN_SECRET`（32 文字以上。空なら 503）。Cookie と CSRF は使いません。token なし・不正・期限切れは 401、無効な利用者・初回パスワード変更待ちは 403。`PLATFORM_AUTH_MODE=local` では token を見ず、全権限のローカル利用者です。
- 利用者: token の `sub` の利用者の現在のロール・権限・対象範囲をそのまま使います（回答履歴・rate limit も同じ利用者）。token の `agent_id` / `run_id` は hash して監査 context の agent / thread に入れます（`X-RAG-Agent-ID` / `X-RAG-Thread-ID` header は使いません）。
- manifest: `/mcp` は「認証済みなら通す」で登録し、権限はツールごとに判定します（`tools/list` は使えるツールだけを返し、権限のないツールの呼び出しは `MCP_TOOL_FORBIDDEN`）。

| ツール | 権限 | 内容 |
|---|---|---|
| `rag_list_search_answer_profiles` | `GET /api/search-answer-profiles` と同じ | 利用者の範囲の ACTIVE な検索・回答プロファイル（`query` / `limit`） |
| `rag_recommend_search_answer_profile` | `menu.search` | 質問から検索・回答プロファイルを推薦する（#1850。`question`・候補の `profile_ids`・`limit`）。利用者が使える（業務 Agent のデータの範囲がある時はその中の）プロファイルの参照先を 1 回 hybrid 検索し（質問の埋め込みは 1 回、LLM は呼ばない）、当たった資料が属するプロファイルを順位で点を付け（上位 10 件の本文だけ。#1865）、名前・説明の重なりを少し足す。`recommended_profile_id`（1 位と近い候補が 1 位と同じ資料に 1 つも当たっていないとき（同じ資料の写しは割れにしない。#1865）・当たりが無いときは `null`）と `candidates`（`id`・`name`・`description`・`reason`・相対の `score`・当たった資料名 `documents`。最大 8 件）を返す。`null` で候補が複数なら、呼び出し元が候補を選択肢にして利用者に確かめる（回答前の確認） |
| `rag_search` | `menu.search` | `POST /api/search` と同じ検索・回答（検索・回答プロファイル / KB の範囲、rate limit、`RAG_ANSWER_TIMEOUT_SECONDS`）。根拠 `citations` の本文は先頭 1000 文字。検索・回答プロファイルのルールの回答前の確認はチャットと同じ判定で、聞くときは検索・回答をせずに `outcome=needs_clarification` と `clarifications`（`condition_id` は `rule:<ルールの id>`）を返す。答えは `conditions` の `rule:<id>`（選択肢の表示名・自由入力を「、」で区切る）、確認しないときは `skip_clarification: true`（#2117） |

RAG のチャット（会話の作成・送信・取得）は画面の機能で、MCP では提供しません（#787）。MCP で提供するのは検索（`rag_search`）と、その対象を選ぶための検索・回答プロファイルの一覧だけです。

新しい版に置き換えた文書（旧版。#1248）は、既定では検索しません。旧版・変更点を尋ねるときは、`rag_search` / `rag_retrieve_evidence` の真偽値の引数 `include_superseded: true` を渡します（既定 `false`。`filters` に `include_superseded` を書くと入力の誤り `MCP_TOOL_ARGUMENTS_INVALID`。#1392）。旧版を含めても検索の範囲（検索・回答プロファイル・ナレッジベース・業務 Agent のデータの範囲）は変わらず、範囲の中の旧版だけが加わります。旧版の根拠は `superseded: true` で返します（`rag_read_source` は旧版の根拠も読めます）。旧版を含めずに検索したとき、当たった今の版の文書に旧版があれば、結果の `older_versions` に旧版（`document_id`・`file_name`・`title`（先頭の見出し。「定期保守計画 2025年度」のように版・年度を含むことが多い）・`superseded_by_document_id`。旧版を含めた検索と同じ範囲の中だけ、最大 10 件）を返します。旧版の年度を名指しする質問（「2025 年度と 2026 年度では」）で `include_superseded` を渡す手がかりです（#1405。Agent はこれと質問の年度・版を照らして、旧版も含めた検索し直しを案内します）。

ツールの業務エラーは `isError: true` の `structuredContent` に `error_code` / `message` / `status` で返します（例: 範囲外の検索・回答プロファイルは `status: 404`、KB の範囲外は `error_code: RAG_SCOPE_FORBIDDEN`、タイムアウトは `status: 504`、rate limit は `status: 429`）。

## Readiness

`GET /api/ready` は外部 API へ ping せず、デプロイ時に注入される設定を依存グループ単位で検証します。

checks は `oci_common`、`enterprise_ai`、`genai`、`oracle`、`object_storage` です。`PLATFORM_ENVIRONMENT=production` では追加で `audit_context_salt` を返し、`RAG_AUDIT_CONTEXT_HASH_SALT` の注入を必須にします。すべて `ok` のときだけ HTTP 200 になり、`ok` 以外の値が含まれる場合は HTTP 503 / `status=degraded` を返します。Oracle の判定はシステム設定 > データベースと同じ platform の `database_readiness` です（#325）。`PLATFORM_ORACLE_USER` / `PLATFORM_ORACLE_DSN` に加え、`PLATFORM_ORACLE_WALLET_DIR`（Thick mode では `PLATFORM_ORACLE_CLIENT_LIB_DIR/network/admin`）に接続用の Wallet ファイル（Thin: `tnsnames.ora` / `ewallet.pem`、Thick: `tnsnames.ora` / `sqlnet.ora` / `cwallet.sso`）がそろい、暗号化された `ewallet.pem` を `PLATFORM_ORACLE_WALLET_PASSWORD`（無ければ `PLATFORM_ORACLE_PASSWORD`）で復号でき、DSN が Wallet の別名なら `tnsnames.ora` にあることを要求します。値は `ok` / `missing` / `wallet_not_found` / `wallet_password_invalid` / `invalid` です。
以前の RAG は DB パスワードがあれば Wallet を見ずに `ok` としていましたが、#325 からは **DB パスワードがあっても Wallet が不備なら `wallet_not_found`（DB の状態 API では `not_configured`）** になります。既存環境で `/api/ready` が 503 になった場合は、システム設定 > データベースで Wallet を登録し直してください。
レスポンスには設定値や secret は含めません。

## DB の状態（`GET /api/ready/database`）

画面の DB ゲート（3製品共通の `DatabaseGate`）が、システム設定の 5 画面（OCI 認証・アップロード保存先・モデル・データベース・外観と証明書）以外を開く前に参照します。3製品共通の部品（`pr_system_settings.database_status`。#325）で、常に HTTP 200 を返し、`status` で `ok` / `not_configured` / `unreachable` / `setup_required` を区別します。応答は `{status, check, detail, context_id, schema_status, adb_lifecycle_state}` です。

1. 設定の判定（上の Readiness の `oracle` と同じ）が `ok` でなければ `not_configured`（接続は試さない）
2. `test_oracle_connection` の bounded な接続確認に失敗すれば `unreachable`
3. RAG の system schema が `ready` でない（または作成・更新の実行中）なら `setup_required`（`schema_status` に `missing` / `partial` / `outdated`）。状態を読めなければ `setup_required`（`check=schema_check_failed`）
4. それ以外は `ok`

ログイン不要の path なので、`detail` には接続先・資格情報・Wallet の path を含めず、ORA / DPY / DPI のコードだけを返します（#320）。

## Oracle AI Database schema

`app.rag.oracle_schema` は production 初期化用の DDL artifact と監査 manifest を生成します。`oracle_document_schema_sql()` は文書メタデータ、`oracle_vector_schema_sql()` は `VECTOR(1536, FLOAT32)` の chunk/vector table、`oracle_search_audit_schema_sql()` / `oracle_ingestion_audit_schema_sql()` は検索・取込の監査 table を生成します。

```bash
uv run python -m app.rag.oracle_schema \
  --output ../artifacts/oracle-schema.sql \
  --manifest-output ../artifacts/oracle-schema.manifest.json
```

manifest は artifact 全体と section ごとの SHA-256、statement 数、`VECTOR(1536, FLOAT32)`、HNSW index (`COSINE`, target accuracy `95`, neighbors `32`, efconstruction `500`) の契約を含みます。staging / production では生成物をレビューし、SQLcl や OCI Resource Manager の安全な適用手順で実行してから smoke test へ進みます。

既存 Oracle schema を現行 DDL 契約へ寄せる場合は migration artifact を生成します。現在の migration は `rag_ingestion_jobs.attempt_count NUMBER(5) DEFAULT 0 NOT NULL`、`rag_ingestion_jobs.max_attempts NUMBER(5) DEFAULT 3 NOT NULL`、`rag_ingestion_jobs_attempts_ck` を追加/補正します。

```bash
uv run python -m app.rag.oracle_schema --migration \
  --output ../artifacts/oracle-schema-migration.sql \
  --manifest-output ../artifacts/oracle-schema-migration.manifest.json
```

アプリからの適用（状態の確認・作成 / 更新・全再作成）は `app.rag.system_schema`（CLI は `app.rag.system_schema_cli`、画面は「システム設定 > データベース > RAG システムテーブル」）が行います。lease・migration の台帳・状態の分類・確認語の検証は 3 製品共通の `pr_system_settings.system_schema` の骨格を使い（#325）、RAG は manifest（`MANAGED_TABLES` / `MANAGED_INDEXES` / Oracle Text の object / 廃止した object）、`oracle_schema` の DDL の正本、共通認証の `PLATFORM_*` の先行作成、取込ジョブの実行中の確認、確認語 `RECREATE_RAG_SYSTEM_TABLES` を持ちます。

外部キー（#505）: 表の作成は「無ければ作る」なので、古い版で作った表には後から正本に足した FK が無いまま残ります。状態の確認は、DDL 正本の `CREATE TABLE` の FK（`MANAGED_FOREIGN_KEYS`）と `USER_CONSTRAINTS` を、名前ではなく定義（表・列・参照先）で比べ、足りない FK を `missing_foreign_keys` に出して状態を `outdated` にします。「作成・更新」（CLI は `initialize`）が FK を追加します。参照先のない既存の行（孤立した行）がある FK は `ENABLE NOVALIDATE`（新しい行と更新から強制し、既存の行は検査しない）で追加し、残った件数を `orphaned_foreign_keys` に警告として出します。既存の行は自動では削除しません。

同じ定義の FK があっても、削除規則（`ON DELETE CASCADE` など）が正本と違うものは `mismatched_foreign_keys`、無効化（DISABLED）されているものは `disabled_foreign_keys` に出して `outdated` にします（#511）。「作成・更新」は、削除規則の違いを既存の FK の DROP と正本の定義での ADD で、無効化を ENABLE で直します（孤立した行があれば NOVALIDATE。既存の行は削除しません）。

孤立した行の削除（#511）は、利用者が件数を確認してから明示的に実行する操作です。画面では「参照先のない行があります」の警告の各 FK の「参照先のない行を削除」から、確認ダイアログ（表・FK・件数）で承認して実行します。CLI は `uv run python -m app.rag.system_schema_cli delete-orphans --constraint <FK> --expected-rows <status で確認した件数>` です。権限は作成・更新と同じ `rag.system_tables.manage` です。実行時の件数が確認した件数より多いときは削除しません（409）。削除の後に FK を `VALIDATE`（検査済み）にします。削除規則が `ON DELETE CASCADE` の FK の先に行があれば、それらも一緒に削除されます。削除した行は復元できません。

監査 table は query 本文や OCR 原文を保存せず、`query_hash`、`source_sha256`、件数、guardrail code、trace id、error type などの運用メタデータだけを永続化する設計です。

## アップロード制限

`RAG_MAX_UPLOAD_BYTES` で最大ファイルサイズを制御します。既定は 200 MiB です。
`RAG_ALLOWED_UPLOAD_CONTENT_TYPES` で MIME type を制限します（既定は `app/config.py` の `allowed_upload_content_types`。画面のファイル選択の `rag/frontend/src/lib/upload-selection.ts` と同じ一覧で、テストで照合します）。HEIC / HEIF の画像と EPUB は、MIME type に関係なく変換の方法を添えた 415 で止めます（解析の依存の pi-heif・pandoc を入れないため。#3351）。
Object Storage client の key は保存時に安全な文字へ正規化します。local 取得時は `local://`、OCI 取得時は `oci://namespace/bucket/key` または plain key だけを受け付けます。相対パス要素、16 階層超、1 要素 255 文字超、全体 1024 文字超の key は拒否し、異常な object path を取込処理へ渡しません。

アップロード時は原本 bytes の `content_sha256` と `file_size_bytes` を保存します。同じ content hash の既存ドキュメントがある場合、レスポンスと詳細 API に `duplicate_of_document_id` を返します。重複アップロードでも原本は保存しますが、後続処理や UI で確認・スキップ判断できるよう参照元を明示します。

## 取込ステートマシン

`POST /api/documents/{id}/ingestion-jobs` と `POST /api/documents/{id}/recipes/{recipe_id}/ingestion-jobs`（確認待ちの工程を進める `POST /api/documents/{id}/recipes/{recipe_id}/approve` も同じ）は、HTTP リクエスト内では取込を実行せず、永続化済み `IngestionJob` を返します。`UPLOADED` / `ERROR` は `EXTRACT` job として `QUEUED`、`REVIEW` 承認後は `INDEX` job として `QUEUED`、`INDEXED` は既定で `SKIPPED(already_indexed)`、`force=true` では再取込用の `QUEUED` になります。`INGESTING` / `INDEXING` は二重実行を避けるため 409 を返します。実際の OCR/本文抽出、chunking、embedding、Oracle の索引は `IngestionQueueWorker` が消費します。

ローカル開発の既定では `RAG_INGESTION_QUEUE_DEDICATED_WORKER_ENABLED=true`、`RAG_INGESTION_QUEUE_INPROCESS_WORKER_ENABLED=true`、`RAG_INGESTION_QUEUE_PROCESS_ISOLATION_ENABLED=true` です。API process 内の worker は軽量 dispatcher として動き、job 本体は `python -m app.rag.ingestion_job_runner <job_id>` の subprocess で実行されます。Docling / OCR / CUDA 初期化が API event loop や他画面の設定 API を塞がないようにするためです。本番（#286 以降の systemd の配備）では `production-ready-rag-backend.service`（`RAG_INGESTION_QUEUE_INPROCESS_WORKER_ENABLED=false`）と `production-ready-rag-ingestion-worker.service` を分け、取込ジョブの consumer は worker の unit だけにします（process isolation は既定の true のまま）。

取込前に Object Storage から取得した原本 bytes を `file_size_bytes` / `content_sha256` と照合します。不一致の場合は OCR/索引へ進まず `ERROR` 状態にし、409 を返します。

chunking は `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP` で制御し、通常方式の既定値は 800 / 120 です。chunk size は 200-32,000 文字、overlap は 0-8,000 文字で、`RAG_CHUNK_OVERLAP >= RAG_CHUNK_SIZE` は起動時に拒否します。見出し単位・ページ単位は 32,000 / 0 を推奨し、見出し/ページを第一境界として、上限を超えた単位だけ同じ境界内で再分割します。文字数はモデルの token 数とは一致しません。取込では `chunk_extraction()` が `StructuredExtraction.elements` を優先し、Docling / Marker / Unstructured / RAGFlow DeepDoc の要素単位・章節単位 chunking を OCI-native に再実装した `structure_v1` chunk を作ります。外部 parser 依存は追加せず、旧形式の `raw_text` だけの抽出結果は軽量 element 推定へ fallback します。大きな文書でも chunk 総数では拒否せず、生成された全 chunk を embedding / indexing 対象にします。

`structure_v1` では章節境界を跨がず、表は他要素から孤立させ、図・画像説明と図注は `content_kind=figure` として同一 chunk にまとめ、リストは連続性を保ち、header/footer は繰り返しノイズとして主索引から除外します。citation metadata には `chunk_profile`、`content_kind`、`section_title/path/level`、`page_start/page_end`、`element_kinds`、`element_ids`、`text_sha256`、`text_chars` を入れ、Oracle DDL を変えずに `rag_chunks.metadata_json` でトレースできるようにします。

`INGESTING` / `ERROR` へ移ると、そのドキュメントの既存 chunk/index 行と古い抽出結果は検索・表示対象から外します。検索対象の chunk は `INDEXED` の文書の chunk だけです。ユーザーが修正できる取込エラーは日本語の原因を残し、未知の内部/SDK エラーは汎用メッセージだけを document error に保存します。

`INDEXED` は chunk、embedding、Oracle 保存、chunk_set、KB binding、extraction artifact が揃った場合だけ付与します。KB への公開 binding に失敗した場合は `ERROR` に戻し、部分的に保存された chunk を検索対象にしません。

## 検索フィルター

RAG は単一 tenant で動かします。`PLATFORM_AUTH_MODE=authenticated` では client の `X-Tenant-ID` を使わず、tenant の条件なしで動きます（client が tenant を切り替えて別の tenant のデータを指定できないようにするため。#225）。`PLATFORM_AUTH_MODE=local` では、HTTP header `X-Tenant-ID` がある場合、アップロード時に tenant id を hash 化して document に保存し、文書一覧、詳細、重複判定、検索 retrieval は同じ `tenant_id_hash` のデータだけを対象にします（開発・テスト用）。raw tenant id は DB / レスポンス / 監査ログへ保存しません。tenant header がない場合は全体を参照できます。

`PLATFORM_AUTH_MODE=local` で、認証ゲートウェイやアプリケーション権限層が `X-RAG-Allowed-Document-Ids` / `X-RAG-Allowed-Category-Names` を付与した場合、文書一覧、詳細、chunk count、検索 retrieval はその document/category scope にも閉じます。header が存在するが有効値が 0 件の場合は deny-all、未指定の場合だけ制限なしです。これらの raw scope 値は監査ログへ出しません。`production` では client の範囲 header（`X-User-ID` / `X-RAG-Allowed-*`）を使わず、ログインした利用者の権限から範囲を決めます。

`POST /api/search` の `filters` は Oracle retrieval に適用されます。対応 key は `document_id`、`file_name`、`category_name`、`status`、`content_kind`、`section_title`、`section_path` です。`content_kind=table` で表 chunk だけ、`content_kind=figure` で図・画像説明 chunk だけ、`section_path` で特定章節だけを候補にできます。未対応 key、未対応 status、未対応 `content_kind` は 422 を返します。
`mode`・`strategy`・`rerank_top_n`・`generation_profile`（旧 standard の回答エンジンの指定）は #595 で削除しました。送っても 422 にせず読み捨てます。
keyword retrieval の local score は重複を除いた query token の coverage として 0.0-1.0 に正規化します。vector / keyword / hybrid の同点は document id、chunk index、chunk id で安定順にし、golden set 評価の再現性を保ちます。
chunk metadata には章節・ページ・要素 ID に加えて `chunk_group_id`、`chunk_group_kind`、`chunk_part_index`、`chunk_part_count` を保存します。長い表・リスト・本文が複数 chunk に分かれても、同じ親要素/章節から来た引用を後で集約できます。
Hybrid retrieval は Reciprocal Rank Fusion を使います。`RAG_RRF_K` で RRF 定数を調整できます。検索結果の citation metadata には `retrieval_mode`、`vector_rank`、`keyword_rank`、`vector_score`、`keyword_score`、`rrf_k`、`rrf_score` を可能な範囲で含めます。query 本文は入れず、hybrid 検索で vector/keyword のどちらが候補を拾ったかを trace id と合わせて調査できます。

回答は回答フロー（`app/rag/answer_engine.py`・`packages/rag_engine`。#594）だけが行います。質問の理解と質問拡張戦略（`RAG_QUERY_STRATEGY`）で作った検索文ごとに hybrid 検索し、原質問を主軸にした重み付き RRF で融合して、Cohere Rerank（`RAG_RERANK_ENABLED`）で並べ替えます。根拠の child と同じ `chunk_group_id` の兄弟 chunk（上限 `RAG_CONTEXT_GROUP_MAX_CHUNKS`）と親本文で親子を復元し（small-to-big）、根拠の前後の child（`RAG_NEIGHBOR_CHILD_COUNT`）を文脈に足します。CRAG（`RAG_ANSWER_FLOW=crag`）は根拠を評価し、足りなければ質問を補正して再検索します。回答文の生成後に根拠との整合を確かめ（監査）、根拠が足りないときは理由と人手確認の要否を付けます。設定と仕組みは `docs/rag-engine.md` を参照してください。

旧 standard の回答エンジンの検索前後の処理（業務同義語の query expansion・本文 hash の重複除去・MMR の多様化・同じ group / 前後の chunk の追加・圧縮・context window と、`RAG_QUERY_EXPANSION_*`・`RAG_CONTEXT_WINDOW_CHARS`・`RAG_CONTEXT_DIVERSITY_LAMBDA`・`RAG_CONTEXT_GROUP_EXPANSION_ENABLED`・`RAG_CONTEXT_NEIGHBOR_WINDOW`・`RAG_CONTEXT_COMPRESSION_*` などの設定）は #595 で削除しました。`backend/.env` に残っていても読みません（`docs/deployment.md` の「既存環境の更新手順（#595）」）。

検索レスポンスの `diagnostics` は、`retrieval_strategy`（常に `hybrid`）、`retrieval_strategy_adapter`（`grounded` / `retrieval_only` / `blocked`）、`answer`（回答フローの診断。工程ごとの実行記録 `execution_steps` など）、安全チェックの `guardrails_input_enabled` / `guardrail_degraded`、filter key、ナレッジベースの件数、適用した KB / 検索・回答プロファイルの設定、非機密の RAG 設定 fingerprint を返します。query 本文や secret は含めず、評価回帰の原因調査に使います（検索の内訳・候補・context の件数などの旧 standard の診断は #595 で削除しました）。

`RAG_ANSWER_TIMEOUT_SECONDS`（既定 600 秒で上限と同じ。#1425）で、LLM を呼ぶ回答生成（`/api/search`・`/api/search/stream`・チャットの送信・MCP の `rag_search`・品質評価の 1 ケース）の pipeline 実行時間を制限します（#375 / #383）。回答フローが質問の理解・根拠の評価（CRAG）・回答の生成・監査で LLM を複数回呼ぶためです（以前の検索だけの上限 `RAG_SEARCH_TIMEOUT_SECONDS` は #383 で削除しました）。品質評価はケースの時間切れを工程とともにケースの結果に残して評価を続け、評価全体を job の上限（回答の回数 × `RAG_ANSWER_TIMEOUT_SECONDS` と `RAG_EVALUATION_JOB_TIMEOUT_SECONDS`〔最小値。既定 3600 秒〕の大きいほうで、86400 秒まで。#1430。同期の API は 600 秒）で打ち切ります（`docs/evaluation-observability-guardrails.md`）。timeout 時は `ApiResponse` 形式の 504（SSE は `error` event、チャットは ERROR の回答として保存）を返し、文言には時間切れになった工程（例: 回答フローの「文書検索（1回目）」）と再試行の案内を含めます。`rag_search_audit` には `outcome=error` / `error_stage=timeout` を残します。

安全チェックは `RAG_GUARDRAILS_INPUT_ENABLED`（質問）と `RAG_GUARDRAILS_OUTPUT_ENABLED`（回答）を独立に選びます。既定は両方 false で、有効な側だけ OCI Guardrails を呼びます。PII の検出位置はアプリでマスクし、攻撃・不適切な内容・検査失敗は処理を止めます。Local 検査と根拠の一致率による方針は使いません。詳しくは `docs/evaluation-observability-guardrails.md` を参照してください。

## 評価

`POST /api/evaluation/run` は aggregate metrics に加えて `passed`、`error_count`、`threshold_failures`、`case_results` を返します。`thresholds` を指定すると precision / recall / MRR / 回答キーワード命中率 / groundedness pass rate の最低値を CI gate として評価できます。検索の方式（`mode`）と rerank の件数（`rerank_top_n`）は受け付けません（#591）。各 case の `trace_id`、`status`、取得 document id、関連 document id、hit document id、case 単位の precision / recall / reciprocal rank、回答キーワード命中、groundedness pass / score / overlap count、guardrail warning、diagnostics、error type を含むため、CI や nightly 評価で失敗した golden case を追跡できます。1 case の検索失敗や timeout は batch 全体を中断せず `status=error` として返し、`passed=false` にします。評価 runner が捕捉した case 失敗は `rag_search_audit` にも残し、timeout は `error_stage=timeout`、その他の case 例外は `error_stage=evaluation` として trace できます。

`evaluation/golden-set.example.json` は評価 API の request schema に合うテンプレートです。実データ投入後に document id と期待キーワードを差し替え、`evaluation/golden-set.json` として CI / staging gate に使います。

CI / nightly では `python -m app.rag.evaluation_cli` を使うと、golden set を評価 API に POST し、レスポンス JSON を artifact として保存しつつ終了コードで gate できます。exit `0` は成功、exit `1` は品質 gate 失敗、exit `2` は golden set / 引数不備、exit `3` は評価 API 接続・HTTP・レスポンス形式の問題です。

## Staging smoke test

OCI / Oracle staging では各種接続設定を注入し、`PLATFORM_UPLOAD_STORAGE_BACKEND=oci` で原本保存先も OCI Object Storage にした上で、backend container 内から `uv run python -m app.rag.enterprise_ai_probe --surface both --dry-run` と `uv run python -m app.rag.staging_smoke --preflight-only` を先に実行します。Enterprise AI probe は LLM/VLM の request 契約を、staging smoke preflight は `/api/ready` と同じ依存グループに加えて実 smoke が local storage へ逃げていないことを `smoke_object_storage_backend` で確認します。すべて `ok` になったら `uv run python -m app.rag.enterprise_ai_probe --surface both` と `uv run python -m app.rag.staging_smoke` を実行します。

本実行では Object Storage put/get、Oracle document 作成、Enterprise AI VLM、chunking、embedding、Oracle indexing、hybrid search、Enterprise AI LLM 生成を 1 回通し、作成した smoke document が citation に含まれることを JSON で確認できます。既定 query は今回作成した一意な `SMOKE-...` marker の引用を要求し、検索は `document_id` filter で新規 document に限定します。既定 query では LLM 回答にも marker が含まれることを gate し、出力には `marker`、実行 `query`、`answer_contains_marker`、`trace_id`、chunk/citation 件数、非機密 `diagnostics`、`cleanup` status が含まれます。既定では evidence として smoke artifact を残すため `cleanup` は `skipped` です。staging DB/Object Storage を汚したくない確認では `--cleanup` を付け、成功・失敗どちらでも作成済み Oracle document/chunk と Object Storage object の削除を best-effort で試みます。回答フローの工程ごとの記録は `diagnostics.answer.execution_steps` で確認します。失敗時は raw 例外 message を出さず、preflight 失敗では `checks`、実行中の失敗では `stage` と `cause_type`、cleanup 指定時は `cleanup` status だけを JSON に含めます。

```bash
uv run python -m app.rag.staging_smoke --preflight-only
uv run python -m app.rag.staging_smoke
uv run python -m app.rag.staging_smoke --cleanup
uv run python -m app.rag.staging_smoke --query "確認用キーワード {marker} を要約してください"
```

## 検索ストリーミング

`POST /api/search/stream` は `text/event-stream` を返します。進捗の `stage`（`history_rewrite`・`answer` と入れ子の `answer_step:<工程名>`、検索だけの経路の `retrieval`）を流し、回答を完全生成し、機微情報マスク・回答検査が完了した後だけ `metadata`、`delta`、`citations`、`done` を返します。`RAG_STREAM_REALTIME_ENABLED` は #595 で削除しました（検査前の token は配信しません）。フロントエンドは旧サービスの `replace` event を引き続き解析できますが、新 backend は使用しません。

既存チャット履歴は、まず `uv run python -m app.rag.chat_history_sanitization --dry-run` で件数だけを確認し、Oracle バックアップ後に `--apply` を実行します。出力は会話・走査・更新・マスク・阻止・失敗件数だけで、原文や検出値を記録しません。

## 監査ログ

Prometheus metrics は `/metrics` で公開します。RAG 全体の latency は `rag_search_duration_seconds`、stage 別 latency（`history_rewrite`・`answer`・検索だけの経路の `retrieval`）は `rag_search_stage_duration_seconds{mode,stage,outcome}` で確認できます。回答フローの中の工程ごとの時間は `diagnostics.answer.execution_steps` に残ります。stage outcome は `success` / `error` / `cancelled` です。評価 case は `rag_evaluation_cases_total{mode,status}` と `rag_evaluation_case_duration_seconds{mode,status}` で記録します。guardrail finding は `rag_guardrail_findings_total{surface,code,severity,action}` で記録し、label に query 本文や回答本文は含めません。

RAG 検索は `app.audit` logger に `rag_search_audit` を構造化ログとして出します。`trace_id`、`request_id`、`outcome`、guardrail code、filter key、検索件数・citation 件数、設定 fingerprint、引用 document id を含みます（旧 standard の検索・context の内訳の列は #595 以降は既定値。既存の監査の行を変えないため列は残す。#596）。`X-Tenant-ID` / `X-User-ID` がある場合は raw 値ではなく `tenant_id_hash` / `user_id_hash` として記録します。`RAG_AUDIT_CONTEXT_HASH_SALT` を `.env` から注入すると hash に salt を加えられます。`outcome=error` では `error_stage` と `error_type` だけを記録します。query/回答本文/例外 message は保存せず、query は SHA-256 hash と文字数だけを記録します。

取込は `rag_ingestion_audit` を出します。`trace_id`、`request_id`、`tenant_id_hash`、`user_id_hash`、`document_id`、outcome、原本 SHA-256、byte 数、document type、抽出 confidence、chunk/vector 件数、エラー種別を含みます。OCR 原文は保存しません。未知の内部/SDK エラーでは例外 message を保存せず、安全な固定メッセージだけを残します。

`RAG_TRACE_EXPORT_HTTP_ENDPOINT` を設定すると、検索・取込 pipeline の `rag.trace_span` event を OpenTelemetry / Langfuse gateway へ非同期 HTTP JSON で送信します。送信 payload は `trace_id`、stage、outcome、duration、低 cardinality attributes、`error_type` に限定し、query 本文、context 本文、OCR 原文、prompt、例外 message は含めません。queue full や送信失敗は `rag_trace_export_dropped` / `rag_trace_export_failed` として `app.trace` logger に残し、RAG request は失敗させません。

## エラーレスポンス

HTTP エラー、リクエスト検証エラー、未処理の 500 エラーは `ApiResponse` 形式へ統一しています。
すべての HTTP レスポンスに `X-Request-ID` を付与します。クライアントが `X-Request-ID` を送った場合はその値を引き継ぎます。
ただし、反射する request id は `A-Z a-z 0-9 . _ : -` の 1-128 文字だけに制限し、空白や制御文字を含む値は新しく採番します。
未処理例外のレスポンスには内部詳細を出さず、`app.main` logger の `unhandled_api_error` に `request_id`、HTTP method、path、例外型を記録します。

```json
{
  "data": null,
  "error_messages": ["対応していないファイル形式です。"],
  "warning_messages": []
}
```

## 構成

```
app/
  main.py            FastAPI エントリ
  config.py          設定（pydantic-settings）
  api/routes/        health / documents / search / evaluation
  clients/           oci_enterprise_ai(LLM/VLM) / oci_genai(embed,rerank) / oracle(Oracle AI Database) / object_storage
  rag/               chunking / ingestion / pipeline
  schemas/           common / document / search
tests/
```

> ⚠️ LLM/VLM は **OCI Enterprise AI**（OCI Generative AI の chat API は使わない）。埋め込み/リランクは **OCI Generative AI**（Cohere Embed v4 / Rerank v4 fast）。ベクトル検索は **Oracle AI Vector Search**。
