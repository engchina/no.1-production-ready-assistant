# NL2SQL の機能とディレクトリ構成

> nl2sql/AGENTS.md から移した詳細（#2276）。nl2sql/AGENTS.md の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、AGENTS.md には規則の要約と案内だけを書く。

## プロジェクト概要

> **製品名と固有 namespace は `Production Ready NL2SQL` / `NL2SQL` を正とする。**
> 新規 DB object、cookie、storage key、context、UI の製品ラベルへ汎用 `RAG` prefix を使用しない。
> `GraphRAG`、`RAGAS`、`RAGFlow` など技術・製品の固有名詞はこの置換対象外とする。

Oracle AI Database の業務データに、自然言語で SQL を生成・検証・実行する SQL 専用の NL2SQL の参照実装。特定の業務ドメインに固定しない。

- **AI 活用**: チャット（自然言語による SQL の多輪の生成と実行。SQL 生成の画面と同じく送信のジョブが実行まで行う。送ると SQL を作る前に回答前の確認を自動で行い、SQL を 1 つに決められないときは会話の中で 1 問ずつ聞く。確認の質問が無くても、確認のカードで要件を確かめて「この内容で SQL を生成して実行」を押すまで SQL を作らない。回答前の確認は SQL 生成の画面と同じ部品・同じ文言で、確認事項とカードは 3 製品共通の回答前の確認のカード（`ClarificationCard`）。#1442 / #1715 / #1761 / #1839）、自然言語の質問から SQL を生成して実行する（SQL 生成）、SELECT SQL の直接実行、SQL から質問の生成、実行履歴。
- **データ準備**: DB 管理チャット（自然言語の質問から全種類の Oracle の SQL を生成し、文ごとに分類して、SELECT は押して実行、それ以外は実行確認語で実行する。複数の文は「すべて実行」で上から順に実行し、最初のエラーで止める（#2333）。会話はチャットと同じく履歴に残す（#2319）。[../db-admin-chat.md](../db-admin-chat.md)。#2264）、管理 SQL の実行、テーブル・ビュー・データ・コメント・アノテーション・ドメインの管理、用語・同義語、共通ルール、サンプルデータ。
- **改善・運用**: 業務プロファイル（SQL 生成の対象範囲）、オントロジー構築、フィードバック、質問分類モデル、SQL 生成評価。

新機能の設計・比較検討の前に、外部 OSS / 研究プロジェクトの一覧 [docs/reference-rag-projects.md](../../docs/reference-rag-projects.md) の該当カテゴリを参照する。優れた点は取り込むが、確定スタック（下記）へ必ず再マッピングし、外部ベクトル DB・別 LLM プロバイダをそのまま導入しない（逸脱するなら理由を添えてユーザーに確認する）。

## ディレクトリ構成

```
backend/                  FastAPI アプリ（uv の venv。pyproject.toml に ruff / mypy / pytest の設定）
  app/
    main.py               エントリ（pr_backend_core の create_app、ルーター、lifespan）
    settings.py           設定（PLATFORM_* は platform/.env、NL2SQL_* は backend/.env）
    api/                  ルーターの集約・health / readiness・problem 応答・run_sync_io（concurrency.py）
    features/nl2sql/      NL2SQL の業務機能（SQL 生成・DB 管理・業務プロファイル・オントロジー・
                          フィードバック・評価・job と worker）
    features/schema/      DB 構造の API
    features/settings/    システム設定の API（platform の pr_system_settings の共通 API ＋
                          システムテーブル管理）
    features/mcp/         他製品（Agent）から呼ぶ MCP（POST /api/mcp）
    security/             権限管理・認可の判定
    clients/              oci_enterprise_ai（LLM/VLM）/ oci_genai（embed・rerank）/ oracle
    cli/                  migration・system schema・各 worker のエントリ
    schemas/              設定 API のスキーマ
  migrations/             Oracle の DDL（番号順）
  scripts/                実 Oracle / OCI Enterprise AI への手動の接続確認
  tests/                  pytest
frontend/                 Vite + React Router + Tailwind v4 + 共有 UI
  src/App.tsx             ルート定義
  src/features/nl2sql/    NL2SQL の画面・状態・ロジック（ontology/ はオントロジーの画面）
  src/features/security/  ログイン・権限管理の画面
  src/components/         layout（AppSidebar・nav-config.ts）/ settings / system（DatabaseGate）
  src/lib/                api / routes / i18n / 作業状態・未保存変更のガード
  lint/                   NL2SQL 固有の oxlint プラグイン
  tests/                  ロジックテスト（*.test.ts、node --test）/ e2e（Playwright）
docs/                     NL2SQL 固有の設計・運用・テスト計画・UI の差分 spec
scripts/                  開発・Compute 上の起動 / 更新 / ログのスクリプトと、そのテスト
init_script.sh            NL2SQL の配備の手順（systemd の backend・worker。platform/deploy/suite-init.sh が呼ぶ）
```
