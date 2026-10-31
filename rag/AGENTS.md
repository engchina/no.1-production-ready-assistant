# AGENTS.md — Production Ready RAG

> RAG（`rag/`）固有のルール。GitHub 運用・Issue / PR 規約・CI・デザインシステム・共通の技術方針（言語・AI / DB の分担・シークレット・LLM 出力の検証・ローカルの検証の範囲）は、root の [../AGENTS.md](../AGENTS.md) と、そこから案内する root の `docs/agents/` を先に適用する。ルールを変えるときはこのファイル（共通ルールは root）を編集し、詳細は下の表の文書に書く。
> Issue の label は `product:rag`、PR title の scope は `rag`。

## 作業ごとに読む文書

| 作業 | 読む文書 |
|---|---|
| 文書レシピ・KB・検索・回答プロファイルの設定、取込・検索の工程 | [docs/agents/settings-responsibility.md](./docs/agents/settings-responsibility.md) |
| 画面・ナビ・権限・「AI 活用」のデータの見え方 | [docs/agents/ui.md](./docs/agents/ui.md) |
| コードの置き場所 | [docs/agents/structure.md](./docs/agents/structure.md) |

## プロジェクト概要

文書とナレッジベースを構築し、業務ごとの **Search Answer Profile** から検索・回答する RAG を本番品質で提供する（ingestion・grounding・回答生成・評価・観測・Oracle / OCI への配備）。SQL 専用の自然言語問い合わせは `../nl2sql/` の責務で、`rag/` へ機能・UI・API・設定を混在させない。

## 用語

- ユーザー向けの主概念は **ナレッジ構築** / **検索・回答プロファイル** / **取込・検索・回答設定**。「検索・回答プロファイル」は正式な製品語で、parser / source profile 等の工程の技術語とは区別する（KB membership・文書レシピの責務を移さない）。
- `producer` / `consumer` / `Pipeline` / `Adapter` / `Profile` / `Runtime` / `Backend` などの技術語は、code identifier・コード内部・開発者向けの診断パネルに限る。
- `SearchAnswerProfile` は正式な code / API 名として維持し、ユーザー表示は「検索・回答プロファイル」。
- **「DocRAG」は使わない。** rag_poc から移したときの名前で、画面・API のメッセージ・docs の地の文にも、コードの識別子・設定（env）・API・DB の名前にも使わない。RAG の標準の用語・名前で呼ぶ（回答フロー・親子階層（small-to-big）・回答の記録・回答生成のプロンプト・質問の拡張・回答の生成方式（CRAG / 標準 RAG）。package は `rag_engine`、回答は `app/rag/answer_engine.py`、分割方式は `small_to_big`、回答の設定は `RAG_QUERY_STRATEGY` など）。旧名は migration（保存値・表の改名）と docs/deployment.md の更新手順にだけ残す。
- 3 層モデル（文書レシピ / KB スコープ / Search Answer Profile）に関わる Issue では、どの層の責務かを明記し、責務越境になっていないかを `修正方針` に書く。

## 技術スタック（RAG 固有の分）

| 用途 | 採用 | 制約 |
|---|---|---|
| 回答生成・構造化抽出・クエリ計画 | OCI Enterprise AI | モデル設定は環境変数 / 設定 API 経由 |
| 埋め込み / リランク | OCI Generative AI（Cohere Embed v4 / Rerank v4 fast） | 埋め込みの次元数を Oracle AI Vector Search とそろえる |
| 画像・文書理解 | Enterprise AI Vision（OCI Document Understanding は準備中〔ロードマップ〕。#3374） | OCR・ページ解析・VLM 入力 |

- データ: Oracle Autonomous AI Database / Oracle AI Vector Search（チャンク・引用・文書構造・評価結果・監査ログ・ベクトル検索の正本）と OCI Object Storage（原本・変換済み artifact・レビュー済み文書・評価 artifact）。
- SDK は `oci` / `python-oracledb`。Frontend は共有 UI に加えて shadcn/ui、通信は REST + SSE / WebSocket。
- 観測は Langfuse + Prometheus + OpenTelemetry。品質は pytest / pytest-cov / ruff / mypy / bandit / pip-audit / Vitest / Playwright。
- **配備**: backend・取込 worker・前処理・parser はサービスごとの uv の venv（`uv sync --locked --no-dev --python 3.12`）と systemd の unit で動かす。unit の定義は `scripts/rag-systemd.sh`（本番の `init_script.sh` と開発の `scripts/rag-services.sh` が共通で使う）。Compute には root の [docs/agents/deploy.md](../docs/agents/deploy.md) のとおり他の製品と同じ 1 台に入る（CPU parser のみ）。Dockerfile・compose は作り直さない。開発の起動は backend が `uv run uvicorn`、前処理 / parser が `scripts/rag-services.sh`、frontend が `npm run dev`（docs/deployment.md「ローカル開発」）。
- **サービス管理画面**は systemd の unit を操作する（起動 = `enable --now`、停止 = `disable --now` で最後に操作した状態を保つ）。backend が実行してよいのは、sudoers で許可した allowlist の unit の `systemctl` / `journalctl` だけ（argv 固定・shell を通さない。`app/services/systemd.py`）。前処理 / parser を足すときは、catalog・`scripts/rag-systemd.sh` の `RAG_MICROSERVICES`・URL 設定の既定値（`127.0.0.1:<port>`）をそろえる（テストで照合する）。

## UI/UX

詳細（サイドナビの各セクションの項目・会話と回答の記録の見え方・画面の差分 spec）は [docs/agents/ui.md](./docs/agents/ui.md)。

- 静かで読み取りやすい情報密度、安定したナビゲーション、明確なフォームの状態を優先する（SaaS / 業務ツール）。
- サイドナビは ナレッジ構築 / AI 活用 / 取込・検索・回答設定 / 改善・運用 / セキュリティ設定 / ユーザーとロール / 運用設定 / システム設定。
- ナビ・ルート・ページ内の操作は権限（`backend/app/security/permissions.py`）で出し分ける。画面を足したら使う API を backend の manifest にも登録する（既定で拒否。完全性テストがある）。
- 「AI 活用」（チャットの会話・回答の記録・実行履歴）は、system_admin は全員、それ以外は自分のデータだけを backend で絞る（他人のデータは 404）。`rag.feedback.manage` などの管理権限は全員を見る理由にしない。
- lint は `frontend/eslint.config.mjs` が `../../platform/docs/design-system/adherence.oxlintrc.json` を import する。

## RAG 設定責務

> **3 層モデル（確定）**: 文書 = 処理レシピを持つ / KB = 純スコープ（コレクション）/ Search Answer Profile → KB。
> KB は「レシピ」と「検索スコープ」を兼任しない。レシピは文書単位、検索・回答設定は Search Answer Profile。

設定・工程に触る前に [docs/agents/settings-responsibility.md](./docs/agents/settings-responsibility.md)（各層が持つもの・設定群ごとの上書きの可否・複数レシピの融合・ジョブの直列 / 並行）を読む。特に次を守る。

- 文書は 1〜3 件の独立した処理レシピ（preprocess / parser / chunking）を持つ。レシピの既定値は global（「取込・検索・回答設定」）から解決し、**KB からは解決しない**。解析エンジンは形式で自動に振り分けない。
- KB は文書の membership だけを持ち（例外は項目抽出の項目の定義）、出し入れは chunk へ波及しない。KB にレシピ・検索・回答設定を持たせず、KB の legacy の設定は runtime で使わず再保存しない。
- Search Answer Profile は検索・回答に使う設定（参照 KB・検索方法・安全チェック）だけを持つ。検索時の解決順は **request 明示 > Published Search Answer Profile > global defaults**。
- 各レシピの直近成功した active chunk_set はすべて検索対象（主レシピ・配信モードは持たない）。成功時だけ active を原子切替し、失敗時は旧 active を保つ。

## 開発コマンド

```bash
# backend
cd backend && uv sync
uv run pytest tests/test_<対象>.py   # 関係するテスト（全件・mypy . ・pip-audit は CI）
uv run ruff format --check . && uv run ruff check . && uv run mypy .
uv run uvicorn app.main:app --reload

# frontend
cd frontend && npm install
npm run lint && npm run build
npx vitest related <変更したファイル>   # または npm run test（全件）
npx playwright test e2e/<対象>.spec.ts  # 関係する spec だけ（smoke・全件は CI と nightly）
npm run dev   # /api は BACKEND_URL を明示したときだけ proxy する（未指定なら 404 の hermetic モード）

# 外部 parser のサービス（services/parsers/<name>。venv は parser ごと）
RAG_SERVICES_MODELS=skip scripts/rag-services.sh sync parser-docling   # torch は lock の CPU 版（#1650）
cd services/parsers/docling && uv run --no-sync --with pytest python -m pytest -q tests
```

CI の job は `RAG / Backend`・`RAG / Frontend`・`RAG / E2E smoke` 等。外部 parser のテストは `RAG / Parser docling` で、その parser と path 依存が変わったときだけ動く（#1512）。

## テスト/検証方針

- backend は pytest、frontend のロジックは Vitest、UI/UX は Playwright。
- **実 Oracle のテスト（`oracle_db` の fixture）は、共有の開発 DB ではなくテスト専用の schema（DB ユーザー）で流すことを推奨する（#619）。** fixture はテストの開始時に未適用の migration を当て、テストが作った行を消すため、共有の DB では並行作業のチェックアウトの migration が手元のデータに当たる。接続先は共通 `.env` の `PLATFORM_ORACLE_*` を読むので、テスト専用のユーザーを書いた別のファイルを `PLATFORM_ENV_FILE=<ファイル> uv run pytest` で渡す。データを削除する migration（テーブルの DROP・行の DELETE）が未適用なら、fixture は何も当てずに実 Oracle のテストを skip し、理由を出す（適用は書き出しの後に `system_schema_cli initialize --allow-destructive` で行う。docs/deployment.md「既存環境の更新手順の共通の注意」）。
- データを削除する migration を足すときは、`oracle_schema.py` の `OracleSchemaSection` に `destructive_note`（削除されるデータと、前にする書き出し）を書く。付け忘れは `tests/test_system_schema_manager.py` が SQL から検出する。

## コーディング規約

1. RAG の検索・回答は Oracle AI Vector Search、OCI Enterprise AI、OCI GenAI の embedding / rerank を中心に構成する（別 RAG SaaS も入れない）。
2. KB の query の legacy config は runtime で無視し、保存時に新規保存しない。
3. 機能開発では、既存のパターン・API・UI コンポーネントを優先する。
