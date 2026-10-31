# Production Ready RAG

A production-ready RAG reference implementation covering data ingestion, chunking, indexing, hybrid retrieval, reranking, evaluation, observability, guardrails, and deployment best practices.

特定業務ドメインに固定せず、RAG システムを本番品質で構築するための参照実装として、ドキュメント取込から検索・回答生成・運用品質までを一貫して扱う。

> プロジェクトの開発ルール（技術スタック・規約）は [AGENTS.md](./AGENTS.md) が正本です（Claude Code / Codex 共通）。
>
> 第三者の部品のライセンスの通知（pypdfium2 が同梱する PDFium などの native ライブラリ・書体など）は root の [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md) にあります。

## 技術スタック

| レイヤー | 採用 |
|---|---|
| LLM / VLM | **OCI Enterprise AI**（OCI Generative AI の chat API は使わない） |
| 埋め込み / リランク | **OCI Generative AI**（Cohere Embed v4 = 1536次元 / Rerank v4 fast） |
| ベクトル検索 / DB | **Oracle AI Database** / Oracle AI Vector Search（`VECTOR(1536, FLOAT32)`）+ Oracle Text |
| バックエンド | Python 3.12 + **FastAPI** + Pydantic v2 + uv |
| フロントエンド | **Vite + React Router** + TypeScript + Tailwind v4 + shadcn/ui + TanStack Query + Zustand |
| ストレージ | OCI Object Storage |

UI/UX は日本語第一の業務アプリとして、情報設計・画面構成・状態モデル・タイポグラフィを本リポジトリ内で管理する。実装技術は AGENTS.md の確定スタックを正とする。

## クイックスタート

```bash
# バックエンド(重い parser 依存は持たない。外部 parser はマイクロサービスへ HTTP 委譲)
cd backend
uv sync
cp ../../platform/.env.example ../../platform/.env   # 3製品共通の設定（OCI / Oracle 接続など。PLATFORM_*）
cp .env.example .env        # RAG 固有の設定（RAG_*）
uv run uvicorn app.main:app --reload    # http://localhost:8000/api/health

# フロントエンド（別ターミナル）
cd frontend
npm ci
BACKEND_URL=http://localhost:8000 npm run dev   # http://localhost:3000（BACKEND_URL 未指定なら /api は 404）

# 前処理 / CPU parser(サービスごとの uv の venv + systemd の unit。rag/ で実行。#286)
scripts/rag-services.sh install            # 前処理と既定の parser(docling・structured-records-jsonl・xml-tree-json。xml-tree-json は登録だけで起動しない)。--unstructured で足す
```

CPU の parser(docling / unstructured / structured_records_jsonl / xml_tree_json)は **独立した FastAPI マイクロサービス**(`services/parsers/<name>`)で
動き、backend は取込時に HTTP 委譲する。各 parser は独自依存で個別に upgrade できる。
構造化レコードの JSONL(前処理 CSV→JSONL・Excel→JSONL・Word→JSONL の成果物)は、使用エンジンで選んだときだけ `structured_records_jsonl` で
記録の単位に解析し、固定分割で 1 記録 1 chunk にする(#2457)。
XML ツリー JSON(`.json`)は、文書解析の既定か処理レシピの使用エンジンで選んだときだけ `xml_tree_json` で解析する(#2042)。
音声・動画は取り込まない(アップロードの画面と API の 415 で止める。#3369)。
既定の解析エンジンは **Docling**(#286)。Docling は **PDF と画像だけ**を解析し、それ以外(テキスト・HTML・Office・メール など)は
取込を始める前に止めて、処理レシピで Unstructured(または Office→PDF)を選ぶよう案内する(自動では振り分けない)。
Unstructured の解析サービスは既定では配備しない。形式ごとの取り込み方は [docs/deployment.md](./docs/deployment.md) の
「既定の解析エンジン（Docling）で扱える形式と、それ以外の形式の取り込み方」を参照。
Marker / Unlimited-OCR / GLM-OCR への対応は削除した(#270)。
MinerU・Dots.OCR・OCI Document Understanding への対応も削除した(#22。OCI に依存しない構成へ移すため)。
詳細は [services/parsers/README.md](./services/parsers/README.md) と
[AGENTS.md](./AGENTS.md) の「Parser マイクロサービス」節を参照。

### サービス管理画面(マイクロサービスの稼働可視化・起動/停止)

前処理 / parser は、サービスごとの uv の venv で動くネイティブのプロセスで、systemd の unit
(`production-ready-rag-<service>.service`、`127.0.0.1:<port>`)として動かす(#286。Docker は使わない)。
システム設定の **サービス管理**(`/settings/services`)は、各 unit の状態(`systemctl show`)と `GET /health` から
稼働状態(稼働中 / 縮退 / 起動中 / 起動失敗 / 停止 / 未登録 / 未設定)を 5 秒ごとに表示し、起動 / 停止 / 再起動とログ
(`journalctl -u <unit>`)を画面から操作する。

- 起動 = `systemctl enable --now`、停止 = `systemctl disable --now`。利用者が最後に操作した状態が再起動・再配備後も保たれる。
- backend は、sudoers で許可した unit の `systemctl` / `journalctl` だけを `sudo -n` で実行する。unit 名は
  `app/services/catalog.py` の allowlist で検証し、argv は固定(任意のコマンドは実行できない)。
- 本番は `RAG_SERVICE_CONTROL_ENABLED=true`(Terraform の stack の既定)のときだけ操作でき、開発(`PLATFORM_ENVIRONMENT=local`)は自動で有効。
- 開発環境の unit と sudoers は `scripts/rag-services.sh install` で登録する(systemd の無い環境は
  `scripts/rag-services.sh run <service>` で前面に起動)。手順は [docs/deployment.md](./docs/deployment.md)。

詳細は [backend/README.md](./backend/README.md) / [frontend/README.md](./frontend/README.md) を参照。

## 実装済みの参照フロー

- `POST /api/documents/upload`: 原本を Object Storage 境界へ保存し、SHA-256 / サイズ / 重複元を記録してドキュメント行を作成。
- `POST /api/documents/{id}/ingestion-jobs`（文書の既定レシピ）/ `POST /api/documents/{id}/recipes/{recipe_id}/ingestion-jobs`（レシピ単位）: 取込 job を投入し、worker がファイル準備 → OCI Enterprise AI 境界での OCR/構造化要素抽出 → ページ・章節・表・リスト感知 chunking → embedding → Oracle AI Database 境界への索引を工程ごとに実行する。確認待ちの工程は `POST /api/documents/{id}/recipes/{recipe_id}/approve` で次へ進め、抽出の修正は `PATCH /api/documents/{id}/recipes/{recipe_id}/review-edits` で保存する。
- `POST /api/search`: 質問の安全チェックと検索範囲（Business Context Pack）の確定の後、回答フロー（質問の理解・質問の拡張・Oracle AI Vector Search と Oracle Text の hybrid 検索（RRF）・Cohere Rerank・根拠の評価と補正検索（CRAG）・small-to-big・回答の生成と監査）で citation-grounded 回答を作り、回答側の安全チェック・trace ID・guardrail warning・回答フローの実行記録を返却。
- `POST /api/evaluation/run`: golden set による precision@k、recall@k、MRR、回答キーワード命中率、groundedness pass rate、case 単位の失敗理由分布を算出。
- `/metrics`: Prometheus metrics を公開。

`evaluation/golden-set.example.json` は評価 API のテンプレートです。実データ投入後に `evaluation/golden-set.json` へコピーして document id と期待キーワードを調整し、CI / staging gate で使います。

Backend は常に OCI Enterprise AI、OCI Generative AI、Oracle AI Database を前提に動作します。開発・staging・本番のいずれも OCI / Oracle 接続情報を共通 `.env`（リポジトリの `platform/.env`、`PLATFORM_*`）または設定画面から注入してください。RAG 固有の設定は `backend/.env`（`RAG_*`）に置きます（#211。詳細と既存環境の移行は [docs/deployment.md](docs/deployment.md)）。

## OCI への配備（Resource Manager）

3製品共通の Terraform stack（monorepo root の `terraform/stack/`）で配備します。Oracle Autonomous AI Database は NL2SQL / Agent と共有し、
RAG は専用の Compute 1 台に NL2SQL / Agent と同じネイティブ配備（uv の venv + systemd + Nginx。Docker は使わない。#286）を作ります
（CPU の parser だけを配備する）。以前の Docker Compose で配備した環境の移行は [docs/deployment.md](./docs/deployment.md) を参照してください。
「配備する製品」で RAG を選び、入力・配備方式・instance 上の構成・制約は [terraform/README.md](../terraform/README.md) を参照してください。

## 外部の MCP クライアントから使う（Claude Code など）

RAG の検索・根拠の読み出しは MCP（`POST /rag/api/mcp`。開発は `http://localhost:8000/api/mcp`）で公開しています。
画面の左下のアカウントのメニュー →「API キー」で自分のキーを発行し、`Authorization: Bearer <API キー>` を付けて接続します。
キーは発行した利用者として動き、その利用者のロールの権限と検索・回答プロファイル / ナレッジベースの範囲で答えます。
検索・回答プロファイルが決まっていないときは、`rag_recommend_search_answer_profile` に質問を渡すと、使えるプロファイルの資料を 1 回検索して合うものを推薦します（LLM は呼びません。候補が違う資料に割れたら `recommended_profile_id` が `null` で、候補の一覧を返します）。

```bash
claude mcp add --scope user --transport http rag https://<host>/rag/api/mcp \
  --header 'Authorization: Bearer ${PR_API_KEY}'
```

MCP サーバーの名前は `rag` にします（組み込みのスキル（[skills/README.md](../skills/README.md)）がこの名前でツールを使います）。`--header` は単引用符で渡すと、キーの値ではなく `${PR_API_KEY}` が設定に保存され、起動のたびに環境変数から展開されます。

`rag_search` は回答を作るので 20 秒〜2 分かかることがあります（サーバーの上限 600 秒）。Claude Code は MCP のツールを既定で 60 秒で打ち切るので、`MCP_TOOL_TIMEOUT=660000` を付けて起動してください（進み具合の通知は [skills/README.md](../skills/README.md)「長くかかるツール」）。

手順・応答・制約は [platform/docs/mcp-api-keys.md](../platform/docs/mcp-api-keys.md) を参照してください。

## ドキュメント

- [RAG アーキテクチャ](./docs/rag-architecture.md)
- [AIDB Memory Engineering](./docs/aidb-memory-engineering.md)
- [評価・観測性・ガードレール](./docs/evaluation-observability-guardrails.md)
- [検索・回答の能力の一覧（必須 / 任意と縮退）](./docs/answer-capability-inventory.md)
- [デプロイメント](./docs/deployment.md)
- [参考 RAG プロジェクト](./docs/reference-rag-projects.md)

## シークレット混入防止(gitleaks)

`.env` 等の機微値を扱うため、コミット前に **gitleaks** でシークレット混入を検出する pre-commit hook を用意している。設定は `.gitleaks.toml`(誤検知の test/E2E fixture のみ allowlist)。各開発者は一度だけ以下を実行する。

```bash
# 1. gitleaks バイナリを導入(例)
brew install gitleaks            # macOS
#  Linux は GitHub Releases の tar.gz を展開し PATH へ配置

# 2. pre-commit を導入して hook を有効化
uv tool install pre-commit       # または pipx install pre-commit
pre-commit install

# 任意: 全ファイルを手動走査 / 履歴全体を走査
pre-commit run --all-files
gitleaks git -c .gitleaks.toml .
```

CI(`secret-scan` ジョブ)でも同じ `.gitleaks.toml` で full history を走査するため、hook 未導入の環境からの混入も PR で検出される。

## CI

monorepo root の `.github/workflows/ci.yml`(統合 CI)で secret-scan(gitleaks)と RAG の job(`RAG / Backend`・`RAG / Engine`・`RAG / Frontend`・`RAG / E2E smoke`)と、3 製品の配備の検証(`Suite / Deploy`)を実行する。Pull Request と `main` への push で、gitleaks による full history シークレット走査、backend の format・lint・type check・test・security audit(依存が変わったときは dependency audit)、frontend の unit test・build、Playwright の smoke、配備スクリプト(`init_script.sh`・`scripts/rag-systemd.sh`・`scripts/rag-services.sh`)のテストを実行する。Docker Compose の検証は自前の compose を削除したため無い(#356)。

frontend の lint(`npm run lint`)は CI の `RAG / Frontend` で実行する(#1099)。dependency audit(`npm audit`)は CI では実行せず、ローカル検証と PR review で確認する。Playwright は PR では smoke と差分に影響を受ける spec(`e2e-impact`)を実行し、全件は `e2e-nightly.yml` が毎晩実行する(#184・#885)。RAG のサービス(`rag/services`)と `rag_parser_core` の単体テスト・lock の検査は `Lint / services & scripts` が実行する(#1525)。型検査は `npm run build` の `tsc --noEmit` が CI 上で兼ねる。

`main` の ruleset の必須 check は `CI OK` の 1 つだけ(変更のあった製品の job の結果を集約する。root の [AGENTS.md](../AGENTS.md) の「CI」)。

`.github/workflows/dependabot-auto-merge.yml` は Dependabot PR の patch / minor 更新だけを required checks 成功後に自動 merge する。major 更新と、major を含む grouped PR は自動 merge せず手動レビューで判断する。人間が作成した PR には作用しない。
