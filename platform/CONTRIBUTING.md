# 共通基盤の運用ルール（governance）

`platform/` は **RAG / NL2SQL / Agent の前後端 single source of truth**。
共通コードはここに集約し、各製品（`rag/` `nl2sql/` `agent/`）は `features/*`・ページ・業務文言だけを持つ。

- フロント共有: `@production-ready/ui`（`packages/ui`）、`@production-ready/system-settings`（`packages/system-settings`、共通のシステム設定画面。`packages/ui` と違い業務の既定文言（日本語）を持ち、製品の i18n で上書きできる）
- バックエンド共有: `production-ready-backend-core` / `pr_backend_core`（`packages/backend_core`）、`production-ready-system-settings-backend` / `pr_system_settings`（`packages/system_settings_backend`、共通のシステム設定 API。backend_core と違い業務の設定項目を持つ。OCI / Oracle に依存する設定は今後ここに入れる）

原則は前後端で共通: **共通機能の変更は必ず `platform/` で行い、各製品にコピーしない。**
GitHub 運用・PR 規約・CI は monorepo 共通の [../AGENTS.md](../AGENTS.md) に従う。

## 1. 配布方法：monorepo 内の相対パス参照

- 各製品は相対パスで参照する。publish や version pin は行わない（2026-09-25 の monorepo 統合で GitHub Packages 配布は廃止した）。
  - frontend: `"@production-ready/ui": "file:../../platform/packages/ui"`、`"@production-ready/system-settings": "file:../../platform/packages/system-settings"`（`globals.css` に両方の `@source` を書く。build は platform で `npm run build`）
  - backend: `production-ready-backend-core = { path = "../../platform/packages/backend_core", editable = true }`（path を変えたら `uv lock` を再生成）
- `package.json` / `pyproject.toml` の `version` は変更履歴の目安として semver で上げる（`patch`: バグ修正・内部実装、`minor`: 後方互換の追加、`major`: 破壊的変更）。破壊的変更は、同じ PR で3製品の利用箇所も直す。
- frontend では Vite の `resolve.dedupe: ["react","react-dom"]` と `@source ".../@production-ready/ui/dist"`（globals.css）が必須（詳細は README）。

## 2. 変更時の確認範囲

- `platform/` を変更した PR では、統合 CI（suite root の `.github/workflows/ci.yml`）が `Platform / UI`・`Platform / backend_core` に加えて **3製品すべての job** を実行する。必須 check `CI OK` が成功するまで merge しない。
- 見た目に関わる `packages/ui` の変更は、CI に加えて3製品の画面をローカルで起動して確認する（light / dark、1280px / 1920px）。結果は PR の `検証結果` に書く。

## 3. 共有 UI（`@production-ready/ui`）

- **共通 UI（トークン・基本コンポーネント・レイアウト・状態/通知）の変更は必ずこのパッケージで行う。** 各製品の `components/ui/*` に私的にコピー・改変しない（ドリフトの元）。
- 製品固有のもの（nav 構成・ページ・API hooks・業務文言）は各製品に置く。
- 判断基準・禁止事項は [AGENTS.md](./AGENTS.md)「デザインシステム / UI」と [docs/design-system/](./docs/design-system/) に従う。

## 4. 共有 backend（`production-ready-backend-core`）

`packages/backend_core`（import 名 `pr_backend_core`）は 3 サービスの **FastAPI インフラの正本**。
標準は [`docs/backend-standard.md`](docs/backend-standard.md)。

- **共通 backend 機能（app factory / logging / metrics / request-id / エラー envelope / health-ready / settings 基底 / pagination）の変更は必ず backend_core で行う。** 各製品にコピーしない。業務ロジック（RAG ingestion / NL2SQL の NL→SQL / Agent の tool 実行）は各製品側。
- **oci / oracledb は backend_core に入れない**（依存を軽く保つ）。各サービスが自分で持つ。
- envelope は `ApiResponse`（`data` / `error_messages` / `warning_messages`）で固定。破壊的変更は major として扱い、3 フロントの API 契約と同じ PR で更新する。
- CI gate は `ruff format --check` + `ruff check` + `mypy strict` + `pytest`(Oracle 不要) + `bandit` + `pip-audit`（`Platform / backend_core` job。`pip-audit` は `uv.lock` / `pyproject.toml` の変更時だけ実行し、全件は `dependency-audit-nightly.yml` が毎晩実行する。#339）。
- 新サービスは `templates/backend-service` から派生し、`service_name` / `features/<domain>` だけ実装する。
