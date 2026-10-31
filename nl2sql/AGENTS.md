# AGENTS.md — Production Ready NL2SQL

> NL2SQL（`nl2sql/`）固有のルール。GitHub 運用・Issue / PR 規約・CI・デザインシステム・共通の技術方針（言語・AI / DB の分担・シークレット・LLM 出力の検証・ローカルの検証の範囲・UI 変更の検証）は、root の [../AGENTS.md](../AGENTS.md) と、そこから案内する root の `docs/agents/` を先に適用する。ルールを変えるときはこのファイル（共通ルールは root）を編集し、詳細は下の表の文書に書く。
> Issue の label は `product:nl2sql`、PR title の scope は `nl2sql`。NL2SQL の docs・コメントの `#N` のうち、2026-09-25 の monorepo 統合より前のものは旧 repo `engchina/no.1-production-ready-nl2sql` の番号。

## 作業ごとに読む文書

| 作業 | 読む文書 |
|---|---|
| 機能の全体（チャット・SQL 生成・データ準備・改善・運用）とコードの置き場所 | [docs/agents/structure.md](./docs/agents/structure.md) |
| 画面・ナビ・lint・NL2SQL 固有の部品、「質問」「検索」の用語の細則 | [docs/agents/ui.md](./docs/agents/ui.md) |
| 「AI 活用」のデータの見え方・role の権限昇格の防止・状態保持 | [docs/agents/security-contracts.md](./docs/agents/security-contracts.md) |
| オントロジー（生成・検証・Markdown・画面） | [docs/agents/ontology-standard.md](./docs/agents/ontology-standard.md) |
| 新機能の設計・比較検討 | [docs/reference-rag-projects.md](./docs/reference-rag-projects.md) の該当カテゴリ |

## プロジェクト概要

> **製品名と固有 namespace は `Production Ready NL2SQL` / `NL2SQL` を正とする。**
> 新規 DB object、cookie、storage key、context、UI の製品ラベルへ汎用 `RAG` prefix を使用しない。
> `GraphRAG`、`RAGAS`、`RAGFlow` など技術・製品の固有名詞はこの置換対象外とする。

Oracle AI Database の業務データに、自然言語で SQL を生成・検証・実行する SQL 専用の NL2SQL の参照実装。特定の業務ドメインに固定しない。機能（AI 活用 / データ準備 / 改善・運用）は [docs/agents/structure.md](./docs/agents/structure.md)。

- チャットは SQL を作る前に回答前の確認を自動で行い、確認のカード（3 製品共通の `ClarificationCard`。SQL 生成の画面と同じ部品・文言）で「この内容で SQL を生成して実行」を押すまで SQL を作らない。
- 外部 OSS の優れた点は取り込むが、確定スタック（下記）へ必ず再マッピングし、外部ベクトル DB・別 LLM プロバイダをそのまま導入しない（逸脱するなら理由を添えてユーザーに確認する）。

## 用語

細則（「検索」のままでよいもの・変えない文言）は [docs/agents/ui.md](./docs/agents/ui.md)「用語」。

- 利用者が入力する自然言語は **`質問`**（root「言語」）。`クエリ` は SQL・技術の概念にだけ使い、旧称の「検索クエリ」も使わない。
- 利用者の自然言語の入力とその実行を **「検索」と呼ばない**（#1189）。SQL の生成と実行は「SQL を生成して実行」（ボタン）・「SQL の生成と実行」（説明の文）、その結果は「実行結果」。
- `frontend/tests/i18n-key-contract.test.ts` が辞書の文言を検査する。

## 技術スタック（確定。NL2SQL 固有の分）

| 用途 | 採用 | 重要な制約 |
|---|---|---|
| LLM（SQL 生成・構造化出力） | **OCI Enterprise AI** | ⚠️ **OCI Generative AI の chat 推論 API は LLM / VLM に使わない** |
| VLM（オントロジー構築の資料の PDF ページの読み取り） | **OCI Enterprise AI** | 同上 |
| 埋め込み | **OCI Generative AI**（Cohere Embed v4） | オントロジーのノードの埋め込み。多言語・**1536 次元** |
| リランク | **OCI Generative AI**（Cohere Rerank v4 fast） | 3 製品共通のモデル設定。NL2SQL ではモデル設定の接続テストだけが呼ぶ |

- LLM / VLM と embedding / rerank は別クライアント（`app/clients/oci_enterprise_ai.py` / `app/clients/oci_genai.py`）に抽象化する。
- **Oracle AI Database** が業務データと NL2SQL の状態（業務プロファイル・オントロジー・履歴・job 等）の正本。ベクトル列は **`VECTOR(1536, FLOAT32)`**。アップロード保存先はローカルのディレクトリ（共通のシステム設定）。
- Backend: DB の接続は `python-oracledb`、SQL の解析は **sqlglot**、オントロジーは **rdflib** / **pyshacl**、サーバは Uvicorn（本番は + Gunicorn）。
- Frontend: Tailwind CSS v4 と共有 UI、関係グラフは `@xyflow/react`、通信は REST（時間のかかる処理は永続 job のポーリング）。
- 観測は構造化ログ + Prometheus（`prometheus-client`）。品質は pytest / pytest-cov / ruff / mypy / bandit / pip-audit / `node --test`（ロジックテスト）/ Playwright。
- 配備は root の [docs/agents/deploy.md](../docs/agents/deploy.md)（1 台の Compute。NL2SQL の手順は `init_script.sh`）。

## UI/UX

詳細（サイドナビの構成・ラベルの二段管理・lint・固有の部品・画面の差分 spec）は [docs/agents/ui.md](./docs/agents/ui.md)。

- 日本語 UI のレイアウト（行高・禁則・フォント）を検証する。UI/UX の機能追加・修正では Playwright のテストを追加・更新し、完了前に実行する。
- サイドナビの構成の正本は `frontend/src/components/layout/nav-config.ts`。ページタイトル / `aria-label` は正式名を維持し、短縮形はサイドナビだけ（`sidebarLabelKey`）。
- lint は `frontend/.oxlintrc.json` が platform の adherence 設定を相対パスで `extends` する。アプリ固有のセレクタは別名（`nl2sql-design-system/restricted-syntax`）に書く。

## 開発コマンド

```bash
# backend（http://localhost:8110/api/health）
cd backend && uv sync
uv run uvicorn app.main:app --reload --port 8110   # または ../scripts/start-backend.sh
uv run pytest tests/test_<対象>.py                  # 関係するテスト（全件は CI）
uv run ruff format --check . && uv run ruff check . && uv run mypy .   # 整形/lint/型

# frontend（http://localhost:3101）
cd frontend && npm install
BACKEND_URL=http://127.0.0.1:8110 npm run dev   # BACKEND_URL 未指定なら /api は proxy せず 404（hermetic。起動時に警告）
npm run lint && npm run build
node scripts/run-unit-tests.mjs tests/<対象>.test.ts   # ロジックテスト
npx playwright test tests/e2e/<対象>.spec.ts               # e2e（関係する spec だけ）
```

- CI の job は `NL2SQL / Backend`・`NL2SQL / Frontend`・`NL2SQL / E2E smoke`。backend は pytest、frontend のロジックは `node --test`（`frontend/tests/*.test.ts`）、UI とユーザー操作は Playwright。
- 時間のかかる処理（DB 構造の再取得・SQL 生成評価・オントロジー構築）は永続 job にし、API は job を投入して返す。本番では `init_script.sh` が作る systemd の worker（`production-ready-nl2sql-{schema-refresh,quality-evaluation,ontology}-worker`）が処理する（[docs/compute-operations.md](./docs/compute-operations.md)）。

## 横断的な保守・セキュリティ契約

3 製品共通の契約は platform の [UX 契約](../platform/docs/ux-contracts/README.md) が正本。NL2SQL 固有の規則は [docs/agents/security-contracts.md](./docs/agents/security-contracts.md) にあり、権限・会話・実行履歴・状態保持に触る前に読む。特に次を守る。

- 「AI 活用」は system_admin は全員、それ以外は自分のデータだけ（他人のデータは 404）。system_admin が他の利用者のデータにできるのは閲覧と運用の操作（停止・削除）だけで、続きの質問・SQL の実行・確認への回答・評価・名前の変更は持ち主だけ（403。#3244）。実行は操作した本人の権限と SQL の実行の接続（#904 の分け方を崩さない）。
- `SYSTEM_ADMIN` 以外の role の更新・割当では、追加される実効権限が actor 自身の実効権限の部分集合でなければ `403` にする（権限昇格を許可しない）。

## オントロジーの標準（#1729）

規則の全文は [docs/agents/ontology-standard.md](./docs/agents/ontology-standard.md)、対応表は [docs/profile-ontology-capabilities.md](./docs/profile-ontology-capabilities.md)「オントロジーの標準」。中身は Palantir Foundry の Ontology を基準に専門性を保ち、記述は日本語が主（訳は `ontology_terms.json` の 1 つの対応表）、画面の説明・診断は平易な日本語、生成は best effort（推測で埋めない）、LLM に渡す Markdown は業務の知識だけにする。

## コーディング規約

1. **LLM / VLM は OCI Enterprise AI 経由のみ**（OCI Generative AI の chat API を使わない）。embedding / rerank は OCI Generative AI（Cohere）、ベクトル検索は Oracle AI Vector Search。
2. **Backend の ASGI event loop 上で同期 I/O を直接実行しない。** 詳細は [docs/backend-concurrency-contract.md](./docs/backend-concurrency-contract.md) を正とする。同期 domain service / Oracle repository / file・CLOB・Excel 処理 / OCI SDK 呼び出しは sync FastAPI route（`def`）、CLI / worker、または `backend/app/api/concurrency.py` の `run_sync_io(...)` 内だけで実行する。`async def` route は `UploadFile`・SSE / WebSocket・async client など本当に `await` が必要な場合に限定する。
