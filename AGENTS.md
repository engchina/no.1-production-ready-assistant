# AGENTS.md — No.1 Production Ready Assistant（monorepo 共通ルール）

> monorepo 全体の共通ルールの正本（Claude Code と Codex が直接読む。`CLAUDE.md` は置かない）。作業対象のディレクトリの `AGENTS.md` もあわせて適用し、矛盾する場合は製品固有のルールを優先する（GitHub 運用と CI は本ファイルが優先）。
> 本ファイルには守る規則と、作業ごとに読む文書の案内だけを書く。詳細は下の表の文書にあり、**その作業を始める前に読む**（本ファイルと同じ強さで守る）。詳細を足すときは表の文書に書き、本ファイルには規則の要約と案内だけを足す。Codex は repo の直下から起動したディレクトリまでの `AGENTS.md` を合計 32 KiB で切るため、本ファイルは 20 KB 以下、製品の `AGENTS.md` との合計は 32 KB 以下に保つ（`scripts/check-agents-md-size.sh`）。`@path` の import は使わない。

## 作業ごとに読む文書

| 作業 | 読む文書 |
|---|---|
| `platform/` `rag/` `nl2sql/` `agent/` の下の作業 | その下の `AGENTS.md`（repo の直下で起動したときも自分で読む） |
| Issue・ブランチ・PR・CI の待ちと merge・worktree・開発の ADB | [docs/agents/github-workflow.md](./docs/agents/github-workflow.md) |
| CI の job・workflow・テストの書き方・ローカルの検証の範囲 | [docs/agents/ci.md](./docs/agents/ci.md) |
| UI（画面・部品・lint・e2e） | [docs/agents/ui.md](./docs/agents/ui.md) と [ARCHITECTURE.md](./platform/docs/design-system/ARCHITECTURE.md) |
| LLM への指示・`skills/`・MCP の instructions とツールの説明・Runtime の指示 | [docs/agents/prompts.md](./docs/agents/prompts.md) |
| 配備（Compute・DB ユーザー） | [docs/agents/deploy.md](./docs/agents/deploy.md) |
| 3 製品で同じ機能（システム設定・ナビ・API キー・アカウント・データの見え方・権限管理） | [docs/agents/shared-features.md](./docs/agents/shared-features.md) |
| `.env` の変数・DB の object の名前 | [docs/agents/naming.md](./docs/agents/naming.md) |
| backend の実装の標準 | [platform/docs/backend-standard.md](./platform/docs/backend-standard.md) |

## 構成

| ディレクトリ | 内容 |
|---|---|
| `platform/` | 3 製品の共通基盤: `packages/ui`（`@production-ready/ui`）、`packages/system-settings`（`@production-ready/system-settings`。共通のシステム設定・ユーザー / ロール管理の画面）、`packages/backend_core`（`pr_backend_core`）、`packages/system_settings_backend`（`pr_system_settings`。共通の設定 API と認証基盤）、`packages/evaluation_scoring`（`pr_evaluation_scoring`。製品と suite の評価が共有する採点の部品）、`packages/enterprise_ai`（`pr_enterprise_ai`。OCI Enterprise AI の URL の規則と失敗の分類。標準 library だけ）、`docs/design-system/`、`deploy/`（1 台の Compute の配備） |
| `rag/` | Production Ready RAG |
| `nl2sql/` | Production Ready NL2SQL（SQL 専用の自然言語問い合わせ） |
| `agent/` | Production Control Plane for AI Agents |
| `suite/` | 製品をまたぐスイートのアプリケーション（独立の uv project。常駐のサービスは持たない）。`evaluation/`（`pr_suite_evaluation`）は製品横断の評価の CLI で、製品を公開の MCP と API キーだけで呼ぶ（[README](./suite/evaluation/README.md)。#3240） |
| `skills/` | 組み込みのスキル（Agent Skills の形式 `skills/<name>/SKILL.md`）。Agent が読み、Claude Code・Codex などの外部の Agent にも配る（`.claude-plugin/` の plugin marketplace。#1816。[設計](./agent/docs/agent-control-plane-design.md)） |

- **依存の向きは `platform/` → 各製品・`suite/` の一方向。** platform は製品・suite を、製品は suite を import しない。suite は platform の公開の package だけに依存し、製品の module・DB・保存ファイルを読まない（`suite/evaluation/tests/test_boundaries.py`）。製品同士はコードで依存せず、HTTP API で連携する（rag は `rag/backend/tests/test_product_boundaries.py`）。Agent → RAG / NL2SQL は呼び先の `POST /api/mcp` を、Run の利用者を `sub` にした短命のサービストークン（署名鍵は `PLATFORM_SERVICE_TOKEN_SECRET`）で呼び、画面用の Cookie / CSRF の API を機械から呼ばない。呼び先は画面と同じ権限・対象範囲で判定する（[backend-standard.md](./platform/docs/backend-standard.md)「製品間の連携（MCP とサービストークン）」）。
- **外部の MCP クライアント（Claude Code など）は 3 製品の `POST /api/mcp` を、利用者が自分で発行した API キー（`Authorization: Bearer prak_…`）で呼ぶ**（RAG / NL2SQL は調べるツール、Agent は業務 Agent へのチャットのツール。[手順](./platform/docs/mcp-api-keys.md)。#1805・#1874）。RAG / NL2SQL は Agent を呼ばない（向きは外部のクライアント → Agent → RAG / NL2SQL）。
- 製品は `platform/` を相対パスで参照する（`file:../../platform/packages/ui`・`path = "../../platform/packages/backend_core"`・`../../platform/docs/design-system/…`）。publish や version pin はしない。
- 各製品の backend / frontend / uv の venv は独立している。配備は 1 台の Compute にまとめる（[docs/agents/deploy.md](./docs/agents/deploy.md)）。
- commit message の `engchina/no.1-production-ready-<製品>#N` は、統合（#71）前の旧 repo（archive 済み）の番号。

## 言語

- 応答・Issue / PR・commit message・コードコメントは日本語（identifier・API path・file path・command・固有名詞は英語のまま）。ユーザーが会話で別の言語を指定したら、応答だけ合わせる。
- システムの主要言語は日本語。UI 文言・エラー・通知・LLM への指示と出力は日本語を前提とし、ユーザー向け文言は i18n 経由で管理する。
- **利用者が入力する自然言語は、3 製品で「質問」と呼ぶ（#1183）。** 「クエリ」は SQL・技術の概念（サブクエリ・クエリ文字列など）にだけ使う（識別子・API・i18n のキーは変えなくてよい）。3 製品で同じ機能の説明の文（チャットの副題・空の状態・処理の経過・停止の案内など）は同じ書き方にし、製品の名詞だけを変える（例: 副題「〈対象〉に質問し、〈確かめるもの〉を確かめながら会話を続ける」、空の状態「質問を入力して会話を始めます」「同じ会話の中では、前の質問と〈回答 / SQL〉を踏まえて…」）。

## 開発ワークフロー / GitHub 運用

詳細（判定の条件・手順・コマンド）は [docs/agents/github-workflow.md](./docs/agents/github-workflow.md)。

- **`main` へ直接 commit / push しない。** Issue を先に作り（label `product:rag` / `product:nl2sql` / `product:agent` / `platform`。複数ならすべて）、ブランチ `codex/<issue-number>-<short-topic>` で変更して PR を出す。docs-only・緊急の修正も同じ（例外は理由を添えてユーザーに確認）。
- **CI はエージェント自身が待ち、merge の可否は `scripts/pr-merge-check.sh <PR>` で判定する**（`gh pr checks --watch --required` は使わない）。検証と PR 本文の更新が済み、最新 commit の `CI OK` が成功したら、ユーザーの確認を求めず `gh pr merge <PR> --merge` で merge する（報告だけで終わらない）。main の取り込みは、main の変更が PR と重なるか全体に効くときだけ。
- CI の失敗・conflict は直して再検証してから merge する。ruleset を迂回した強制 merge はしない。**main が赤（label `ci-failure` の Issue）なら、ほかの作業より先に直す。**
- **merge 後は `main` へ fast-forward し、その作業の worktree とローカルのブランチを消してから完了を報告する**（`reset --hard` 等で未保存の変更を破棄しない）。他のセッションの未コミット変更・stash・worktree を確認なしに破棄・上書きしない。
- **開発の ADB（`platform/.env` の `PLATFORM_ORACLE_DSN` の接続先）が停止していたら、利用者に聞かずに起動する**（停止は指示が無い限りしない。起動したことは報告に書く）。
- **一時ファイルは `/tmp` ではなく repo の直下の `./tmp/<作業の名前>/` に置き、commit しない**（作業用の worktree は `.claude/worktrees/`）。

## Issue / PR の書き方

見出しと各項目はテンプレート（[Issue](./.github/ISSUE_TEMPLATE/issue.md)・[PR](./.github/PULL_REQUEST_TEMPLATE.md)）、規約の全文は [docs/agents/github-workflow.md](./docs/agents/github-workflow.md)「GitHub Issue / Pull Request の記述規約」。

- 日本語で、対象と事象が分かる具体的なタイトルにする。事実と推測を分け、secret・token・個人情報・実 credential は書かない。
- **Issue** は `問題` / `症状` / `原因` / `修正方針` を必ず含める。**PR title** は `<type>(<scope>): <日本語の要約> (#<issue-number>)`。`検証結果` は実行した command と結果を正確に書き、失敗・skip・未実行を隠さない。PR 作成後の変化は本文を最終状態へ更新してから merge する。
- **業務の内容は書かない（repo は public。#2019）。** 利用者が扱う文書・データの業務の内容（顧客・案件・制度・文書・法令などの名前、ナレッジベース・プロファイル・業務 Agent などの利用者が付けた名前、質問や文書の実データの抜粋）を、Issue・PR・commit message・ブランチ名・コード・コメント・識別子・ログ・画面の文言・i18n・docs・テストの fixture のどこにも書かない。再現情報は技術の言葉と匿名の名前（「ナレッジベース A」）で書き、テストは合成の fixture を使い、機能・設定の名前も技術の言葉で付ける。利用者が会話で示した業務の文書・質問は、ローカルの確認（repo の外か `./tmp/`）だけに使う。
- **製品の不具合を確かめたら、必ず Issue を作る（#2488）。** 作業中（実装・検証・評価・配備・調査）に再現か根拠で確かめた不具合は、依頼の範囲の内外やその場で直すかに関わらず、ユーザーの指示を待たずに Issue にする（推測だけでは作らない）。匿名の技術の言葉で書き、業務の内容・secret・個人情報・実環境の機密（IP・ホスト名・OCID・実データのログなど）を書かない。

## CI

詳細は [docs/agents/ci.md](./docs/agents/ci.md)（正本は `.github/workflows/ci.yml`）。

- 必須 check は **`CI OK`** だけ。`changes` job が変更のあった製品の job だけを動かす。**job を追加したら `ci-ok` の `needs` に足し、すべての job に `timeout-minutes` を付ける。** schedule の workflow を足したら `ci-failure-issue.yml` の `workflows` にも足す。runner は `ubuntu-24.04`。
- **テストは並列でも直列でも通るように書く**（CI の pytest は `-n auto`）。**実 Oracle のテストは skip のまま成功にしない。** DB の層・テストが読むファイルを足したら `changes` の filter にも足す。
- Python の整形は `ruff format`（black は使わない）。

## デザインシステム / UI（platform が正本）

**UI に触る前に [docs/agents/ui.md](./docs/agents/ui.md)（禁止事項・画面の構成・操作部品の高さと幅・読み込み中・一覧・ページング・メッセージ・lint・UI 変更の検証）と [ARCHITECTURE.md](./platform/docs/design-system/ARCHITECTURE.md) を読む。** 特に次を守る。

- 依存は「デザインシステムの決定 → `platform/packages/ui` → 各製品」の一方向。製品で部品やトークンを新規実装せず、`packages/ui` に入れる（同じ PR でよい）。色・型・余白・角丸・影・モーション・フォーカス表示・テーマは `packages/ui` が持つ。
- 生の hex・色の関数・生の px、共有部品の再実装、`<table>` の手書き（`DataTable`）、余白コンテナの手書き（`PageBody`）、操作部品の高さ・幅の手書き（`size` / `width`）、`Spinner` 以外の回転するアイコン、絵文字と手描き SVG（アイコンは `lucide-react`）を使わない。
- 画面は `AppShell` → `PageHeader`（`actions` は配列）→ `PageBody` → `Section` の構成にする。単位の境界: 文字サイズとコントロール高さは px、余白とレイアウト寸法は rem。
- 読み込み中は `TimedLoadingState` と内容の形の `Skeleton`、一覧は `DataTable` と共通の `Pagination`（既定 25 件）と `ListToolbar` の件数。
- **UI/UX の作業は必ず `ui-ux-pro-max` skill を使い、変更ごとに Playwright で desktop と 375px の実画面を確かめる。** 画面のコードを変えたら、その画面の既存の spec も同じ PR で直す。画面幅で結果が変わる test には `{ tag: "@responsive" }` を付ける。

## 共通の技術方針

- **AI / DB は OCI / Oracle に集約する**: LLM / VLM = **OCI Enterprise AI**、embedding / rerank = **OCI Generative AI**（Cohere Embed v4 / Rerank v4 fast）、ベクトル検索 = **Oracle AI Vector Search**、データの正本 = **Oracle AI Database**。この分担を取り違えない。外部ベクトル DB・別 LLM provider・別 SaaS は入れず、逸脱するなら理由を添えてユーザーに確認する。
- **Oracle の製品名は公式名で、バージョン（`26ai` 等）を入れない**（「Oracle AI Database」「Oracle AI Vector Search」「Oracle Autonomous AI Database」）。対応バージョンは [platform/docs/deployment.md](./platform/docs/deployment.md)「Autonomous AI Database（全製品で共有）」だけに書く（`adb_db_version`・`oracle_26ai` などの値・識別子は変えない）。
- Backend は Python 3.12 + FastAPI + Pydantic v2 + uv と `pr_backend_core`。Frontend は Vite + React Router + TypeScript + Tailwind + 共有 UI + TanStack Query + Zustand。
- **Python は 3.12 に固定する**（全 `pyproject.toml` の `requires-python = ">=3.12,<3.13"`、直下の `.python-version` は `3.12`。uv は project の中で直下の `.python-version` を読まないため、上限は `requires-python` で掛ける）。版を変えるときは `pyproject.toml`・`uv.lock`・CI の `python-version`・各製品の `init_script.sh` の `uv python install` / `--python` を同じ PR でそろえる。
- シークレットは `.env` / secret store 経由（ハードコード・commit・API 応答への展開をしない）。LLM の出力は Pydantic で検証してから使う。
- **ハードコードしない。** 特定の文書・業務・質問・データに合わせた語・規則・閾値・手順をコード（分岐・正規表現・プロンプトの例）に書かない。必要なら skill（`skills/<name>/SKILL.md`）か設定（`.env`・設定画面・検索・回答プロファイル・言語資源）に置き、コードは汎用の仕組みにする。
- OCI / Oracle / LLM を呼ぶ層は CI では決定論スタブ / 録画応答でテストし、実サービスは手動 / ステージングで確かめる。
- 実装と同時にテストを追加・更新し、該当範囲の lint・型チェック・テストの結果を報告する。ローカルでは変更した範囲だけを検査し、全件は CI に任せる（[docs/agents/ci.md](./docs/agents/ci.md)「ローカルの検証の範囲」）。
- **不具合は局所検証で一つずつ直してからフルフローを確認する。** 毎回全工程を再実行せず、関連回帰の通過後に統合受入を行う。局所成功と全体成功を分け、必須 CI は省略しない（[手順](./docs/agents/ci.md)）。
- Excel（.xlsx / .xlsm / .csv / .tsv）の作成・編集・分析は Codex の `spreadsheet` skill を使う（[導入と検証](./docs/agents/spreadsheets.md)）。その他の文書（.docx / .pptx / .pdf）の生成は対応する skill（`document-skills:*` / `pdf`）を使う。
- **実際に渡す Prompt は Step 形式で責務を明確にし、複雑な処理だけを構造化した段階に分ける。** 全呼出を連鎖化せず、コードの保証・条件・原文・権限境界を保つ。分割の判断と検証は [docs/agents/prompts.md](./docs/agents/prompts.md)。
- **LLM に渡す文（プロンプト・skill・MCP の説明）を書く・変える前に [docs/agents/prompts.md](./docs/agents/prompts.md) を読む。** 1 つの規則は 1 つの層にだけ書き、コードで保証できることはコードに置き、経緯や内部の仕組みを書かない。変えたら同じ PR で関係するテストを直し、最初のターンに渡す字数の前後を PR に書く。

## 配備

詳細は [docs/agents/deploy.md](./docs/agents/deploy.md)。

- 自前のコードは Docker を使わずネイティブで動かす（開発は `uv run` / `npm run dev`、本番は systemd。Dockerfile・compose は持たない）。reverse proxy（Nginx など）と TLS の証明書は配備しない（置くかは利用者の選択。#19）。
- **再配備は repo の直下で `sudo -n ./platform/deploy/suite-update.sh` を実行する。**
- 選んだ製品を 1 台の Compute（Oracle Linux 9、ユーザー `opc`）に入れ、`platform/deploy/suite-init.sh` が各製品の `init_script.sh` を呼ぶ（製品は単独では配備しない）。OCI Resource Manager の Terraform stack は持たない（#12）。
- 3 製品は既定で ADB のアプリ用の DB ユーザー（ADMIN ではない）で接続する。DB の新しい機能を使うときは付ける権限で足りるかを確かめる。

## 共通の仕組みと設定・命名

詳細は [docs/agents/shared-features.md](./docs/agents/shared-features.md) と [docs/agents/naming.md](./docs/agents/naming.md)。

- **Agent の業務 Agent は、スキルが宣言したツールだけを使う**（直接の指定・明示の構成の暗黙の切り替え・未対応の成功扱い・検索の結果を根拠にした承認の省略をしない。五つの不変条件の正本は [agent/docs/agents/runtime.md](./agent/docs/agents/runtime.md)。#3240）。
- **3 製品で同じ機能は platform に 1 セットだけ置く**（システム設定・ユーザー / ロール管理・API キー・アカウントの画面）。サイドナビの下部の並びと名前、ナビのアイコン、権限管理のメニューの一覧（左のナビが正本）は 3 製品でそろえる。
- **「AI 活用」の下の機能は、system_admin は全員のデータ、それ以外は自分のデータだけ**を扱う（backend で絞り、他人のデータは 404）。system_admin が他の利用者のデータにできるのは閲覧と運用の操作（停止・削除・承認の判断）だけで、続きの質問・実行・回答・評価は持ち主だけ（#3244）。
- **`.env.example` は変数を用途別に分類し、工程の設定は使う工程の順にできる限り並べ、説明コメントも対応する変数と一緒に置く**（詳細: [docs/agents/naming.md](./docs/agents/naming.md)）。
- 共通の設定は `platform/.env`（`PLATFORM_`）、製品だけの設定は製品の `backend/.env`（`RAG_` / `NL2SQL_` / `AGENT_`）。表などの DB object の名前は、共通が `PLATFORM_`、製品固有が `RAG_` / `NL2SQL_` / `AGENT_` で始める。名前を変えるときは旧名との互換を持たない。
