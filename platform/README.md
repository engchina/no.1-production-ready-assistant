# platform — No.1 Production Ready 共通基盤

monorepo `no.1-production-ready-assistant` の `platform/`。No.1 Production Ready 製品群（**RAG / NL2SQL / Agent**）が共有する **前後端の single source of truth**。
1 回の変更（トークン・コンポーネント / ログ・metrics・エラー envelope・app factory）で 3 プロジェクトを同時に底上げする。

```
packages/
  ui/             @production-ready/ui    — 共有フロント UI/UX（Vite library / React 19 / Tailwind v4）
  system-settings/ @production-ready/system-settings — 3製品共通のシステム設定画面（OCI 認証・アップロード保存先・モデル・データベース。#70。外観と証明書は #19 で削除）とユーザー管理・ロール管理画面（#206）
  backend_core/   production-ready-backend-core    — 共有 FastAPI インフラ（Python 3.12 / pydantic v2 / uv）
  system_settings_backend/ production-ready-system-settings-backend — 3製品共通のシステム設定 API と、共通認証基盤（ユーザー・ロール・セッション・ログイン。PLATFORM_* テーブル。pr_system_settings。#70 / #206 / #212）
templates/
  backend-service/  FastAPI サービス雛形（backend_core 利用）
docs/
  design-system/    デザインシステムの正本（ARCHITECTURE / README / components-reference / adherence lint）
  ux-contracts/     3 製品共通の画面の振る舞いの正本（ボタン・通知・ページの型・作業状態・横断契約。#118）
  backend-standard.md  共有 backend の標準
```

- フロント標準・使い方: 本ファイル以下 + [`packages/ui/README.md`](packages/ui/README.md)
- バックエンド標準・使い方: [`docs/backend-standard.md`](docs/backend-standard.md) + [`packages/backend_core/README.md`](packages/backend_core/README.md)
- 変更運用ルール（前後端共通）: [`CONTRIBUTING.md`](CONTRIBUTING.md)

各製品（`../rag/` `../nl2sql/` `../agent/`）は自分の `features/*`・ページ・API hooks・業務文言だけを持ち、共通基盤はこの `platform/` に集約する。

---

# @production-ready/ui（フロント共有 UI）

3 プロジェクトが共有する UI/UX の single source of truth。
デザイントークン・基本コンポーネント・アプリシェル(Sidebar / AppShell / PageHeader / Breadcrumbs)・
状態ビュー・通知機構をまとめ、同一の見た目・操作・アクセシビリティを共有する。

技術スタック: **Vite (library mode) + React 19 + TypeScript + Tailwind v4 + shadcn/ui 流コンポーネント**。
日本語第一(`Noto Sans JP` / `Noto Sans SC` / `Noto Sans TC` / `Noto Sans HK`（4 書体とも可変書体。書体名は `Noto Sans JP Variable` など）/ `Roboto` / system-ui、本文 14px。JP に無い字だけを SC → TC → HK で補う)。

## 構成

```
packages/ui/
  src/
    styles/tokens.css        デザイントークン(CSS 変数 + @theme + base + keyframes)
    styles/structure/        utility で表せない共有部品の構造 CSS（FixedSplitPane）
    lib/                     cn() / 経過時間の計算 / 分割比率の計算 / メニューのフォーカス復帰
    components/
      ui/                    Button / Card / Switch / Skeleton / Select / Banner /
                             Toast / MessageText / ConfirmDialog / ToggleChip / Tabs / FieldError / FormStatus /
                             ContentActionBar / BulkSelectionActions / ClearActionButton /
                             FloatingActionMenu / DisclosureChevron
      feedback/              LoadingState / ErrorState / EmptyState /
                             ActionResultRegion / ProcessingIndicator / TimedLoadingState
      data/                  StatusBadge(汎用 variant) / DataTable / Pagination /
                             RowActionMenu / ObjectActionBar（EntityAction）
      app-shell/             AppShell / Sidebar / PageHeader / PageBody / Section / Breadcrumbs / ListDetailLayout / FixedSplitPane
    navigation/types.ts      NavItem / NavSection / NavLinkComponent / SidebarLabels
    store/                   createUiStore(factory) / toast store
    index.ts                 公開 API バレル
```

## ビルド

```bash
npm install
npm run build         # dist/index.js + dist/index.d.ts + dist/tokens.css
```

## 消費側(各アプリ)の使い方

通知・バナー・フォーム状態・確認ダイアログ・状態ビューの文字列は `MessageText` を内部利用し、
改行や連続空白を正規化したうえで、日本語・英語の文末を優先して折り返す。単独の長文や URL は
コンテナ幅内で安全に折り返す。独自の通知本文を組む場合も公開 `MessageText` を使用する。

### 1. 依存追加(monorepo 内の file: リンク)

```jsonc
// frontend/package.json
"dependencies": {
  "@production-ready/ui": "file:../../platform/packages/ui"
}
```


### 2. Vite 設定(必須: React 重複回避)

file: リンクは自分の `node_modules` の React を解決しうるため、必ず dedupe する:

```ts
// vite.config.ts
resolve: { dedupe: ["react", "react-dom"] }
```

### 3. スタイル取り込み

```css
/* src/globals.css */
@import "tailwindcss";
@import "@production-ready/ui/tokens.css";
/* 共有コンポーネントの utility クラスを Tailwind v4 のスキャン対象に含める */
@source "../node_modules/@production-ready/ui/dist";
```

### 4. アプリシェル

```tsx
import { AppShell, Sidebar } from "@production-ready/ui";

<AppShell sidebar={<AppSidebar />}>
  <Routes>…</Routes>
</AppShell>
```

`Sidebar` はルーター・i18n・auth・状態ストアに依存せず、すべて props で注入する
(`linkComponent` に react-router の `Link`、`labels` に翻訳済み文字列、`footer` に
ユーザー/ログアウト slot、`sections` に解決済みラベルの NavSection)。各アプリは
`AppSidebar` ラッパで自分の nav-config / i18n / ストアを束ねる(RAG / NL2SQL / Agent 同一パターン)。

## 役割分担

| 層 | 置き場所 |
|---|---|
| Design tokens / 基本コンポーネント / レイアウト / 状態・通知 | **このパッケージ** |
| アプリ固有の nav 構成・ページ・API hooks・業務文言 | 各アプリ repo |

共通 UI の変更は **必ずこのパッケージで行い** タグを切る。各アプリは `components/ui/*` を
私的にコピーしない。

---

## 既存環境の更新手順（#46 埋め込みの次元を 1536 から 1024 に）

#46 で埋め込みの次元を、Cohere Embed v4 に合わせた **1536** から、Aliyun 百煉の `text-embedding-v4` の既定の **1024** に変えた。
次元の正本は `pr_system_settings.embedding` の `EMBEDDING_DIMENSIONS`（とベクトル列の型 `EMBEDDING_VECTOR_COLUMN_TYPE` =
`VECTOR(1024, FLOAT32)`）の 1 か所で、3 製品の Oracle の DDL・埋め込みの呼び出しの `dimensions`・返った次元の検証・画面の表示がこれを使う。
platform を import できない層（RAG の `rag_engine` の `DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS` / `ADB_EMBEDDING_DIMENSIONS`、NL2SQL の
SQL の migration のファイル）は同じ値を書き、一致をテストで確かめる。

- **次元は設定では変えない（1024 に固定）。** Oracle のベクトル列は固定の次元で、値を変えるには列と索引の作り直しと全件の再埋め込みが
  要るため、`.env` の値は 1024 以外を受け付けない（「システム設定 › モデル」に次元の入力欄は無い。#45）。
- **共通 `.env`（`platform/.env`）の `PLATFORM_OCI_GENAI_EMBEDDING_DIM=1536` は `1024` に書き換える（行を消してもよい）。** 1024 以外は
  3 製品とも起動の時に設定の検証で止まる（列と合わない次元で埋め込みを保存しないため）。
- #45 より前に保存した `model-settings.json` の `generative_ai`（`embedding_dim` の 1536 を含む）は、読み込みで登録モデルへ移し、次元は
  読み捨てる（製品の設定の値を使う。書き換えは要らない）。

### 既存の DB の更新（ベクトルを削除する migration）

ベクトルの列は次元を変えられないため、列と HNSW 索引を作り直す migration（RAG は `20261031_002_embedding_dimensions_1024`、NL2SQL は
`025_embedding_dimensions_1024`）を足した。**保存済みのベクトルはすべて消える**ので、データを削除する migration として明示の承認が要る
（[rag/docs/deployment.md](../rag/docs/deployment.md)「既存環境の更新手順の共通の注意」）。次元が既に 1024 の列（新しい環境）は何もしない。
Agent はベクトルの列を持たないので、`.env` の書き換えだけでよい。

1. 共通 `.env` の `PLATFORM_OCI_GENAI_EMBEDDING_DIM` を `1024` にする。
2. RAG の取込・NL2SQL の job を止める（`systemctl stop` で worker を止めるか、実行中の job の完了を待つ）。
3. 承認して更新する（画面は各製品の「システム設定 > データベース」のシステムテーブルの「作成・更新」の確認ダイアログで承認する）。

   ```bash
   # RAG（rag/backend）
   uv run python -m app.rag.system_schema_cli status       # pending_destructive_migrations を確かめる
   uv run python -m app.rag.system_schema_cli initialize --allow-destructive
   # NL2SQL（nl2sql/backend）
   uv run python -m app.cli.nl2sql_system_schema --status
   uv run python -m app.cli.nl2sql_system_schema --initialize --allow-destructive
   ```

4. 3 製品の backend と worker を再起動する（プロセスの中の埋め込みの cache を捨てる）。`check`（RAG は `system_schema_cli check`、NL2SQL は
   `nl2sql_system_schema --check`、Agent は `app.cli.agent_system_schema --check`）が ready になることを確かめる。
5. 再埋め込みする。
   - **RAG**: 文書の chunk は残るが、ベクトルが空なのでベクトル検索に出ない（キーワード検索には出る）。すべての文書を再取込する
     （`POST /api/documents/{id}/ingestion-jobs?force=true`。文書ごとに取込の job を入れ直す）。索引を検索・回答プロファイルの推奨ビルドで
     作り直していた場合は、migration が既定のビルドで作るので、画面の SQL で作り直す。業務ガイドの質問の例・承認済み FAQ の埋め込みは、
     保存の次元が違うと次の照合で作り直す（手順は要らない）。
   - **NL2SQL**: オントロジーのノードの埋め込みが消える。業務プロファイルごとにオントロジーを構築し直して公開する。feedback の学習を
     使う環境は、feedback の索引を再構築する（`POST /api/nl2sql/feedback-index/rebuild` を `{"execute":true}` で。旧い表は migration が削除した）。
     質問の分類器は 1536 次元で学習した model を使えない（予測は決定論の推薦に切り替わる）ので、学習し直す。

## 既存環境の更新手順（#566 既定のテキストモデルの必須化）

#566 で「システム設定 › モデル」の既定のモデルを **既定のテキストモデル** → **既定の Vision モデル** の順に並べ、2 つとも必須にした。
テキストの選択肢の「既定の Vision モデルを使う」は無くなり、登録モデルがあるのにテキストが空のまま保存すると、画面は欄の直下に、
API は 422 で「既定のテキストモデルを選択してください。」を返す。

- **既定のテキストモデルが未設定の環境は、「システム設定 › モデル」で既定のテキストモデルを選んで保存する。** 画面を開くと欄にエラーが出る。
  以前と同じ動きにするには、既定の Vision モデルと同じモデルを選ぶ。
- 保存し直すまでの実行時の動きは変わらない。`enterprise_ai_default_model_id` は、テキスト → Vision → 登録モデルの先頭 の順の代替を
  既存環境のための安全策として残している（`.env` だけで構成した環境も、テキストが空なら既定の Vision モデルを使う）。
- 接続情報・Generative AI の節の保存は止めない（既定のモデルの検証は、登録モデルか既定のモデルを変えたときだけ行う）。

## 既存環境の更新手順（#499 既定のモデルの変数名）

#499 で「システム設定 › モデル」の既定のモデルを **既定の Vision モデル**（画像を読む処理）と **既定のテキストモデル**
（画像を扱わない処理。#566 で必須にした）の 2 つに分け、共通 `.env`（`platform/.env`）の変数名を変えた。
旧名は読まないため、`.env` に既定のモデルを書いている環境は次の手順で書き換える（画面だけで設定している環境は手順 2〜3 は不要）。

| 旧名 | 新名 |
|---|---|
| `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_MODEL` | `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL` |
| `PLATFORM_OCI_ENTERPRISE_AI_LLM_MODEL` | `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL`（`DEFAULT_MODEL` が無いか空のときだけ。旧版と同じ優先順） |
| `PLATFORM_OCI_ENTERPRISE_AI_VLM_MODEL` | `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL` |

- 画面で保存した `model-settings.json` は書き換えなくてよい。旧 key の `default_model_id` は、backend が読み込むときに
  既定のテキストモデルへ移し、既定の Vision モデルは従来と同じ規則（既定モデルが Vision 対応ならそれ、そうでなければ一覧で最初の
  Vision 対応のモデル）で補う。画面の「登録モデル」で保存し直すと新しい key（`default_text_model_id` / `default_vision_model_id`）で書かれる。
- 登録モデルに Vision 対応のモデルが 1 つもない環境は、画面を開くと「既定の Vision モデル」にエラーが出る。登録モデルの 1 つ以上で
  「画像入力（Vision）に対応」をオンにして保存する（接続情報・Generative AI の節の保存は止めない）。

1. 3 製品の backend と worker を停止する（Compute では各製品の systemd の unit。RAG は backend と ingestion-worker）。
2. 書き換えの内容を確認する（書き換えない）。リポジトリ root で実行する。

   ```bash
   uv run --project platform/packages/backend_core \
       python platform/scripts/migrate_model_env_names.py
   ```

3. 問題がなければ `--apply` で書き換える（`platform/.env.bak-499` を作ってから書き換える）。新名が既にあれば新名の値を残し、
   旧名の行を消す（競合として表示する）。`PLATFORM_ENV_FILE` で別の場所を使っている場合は `--env-file <path>` を渡す。

   ```bash
   uv run --project platform/packages/backend_core \
       python platform/scripts/migrate_model_env_names.py --apply
   ```

4. 3 製品の backend と worker を起動する。RAG は、解析サービス（parser）に渡す実行用の env（`RAG_SERVICE_RUNTIME_ENV_FILE`）を
   backend が書き直すので、RAG の「サービス管理」画面から OCI の parser を再起動する（`systemctl restart` だけでは古い env のまま）。

## OCI Enterprise AI のプライマリ接続・セカンダリ接続・ターシャリ接続（#533 / #542 / #786）

「システム設定 › モデル」の OCI Enterprise AI は、接続（Endpoint URL・Project OCID・API key）を「プライマリ接続」「セカンダリ接続」
「ターシャリ接続」の 3 つまで持ち、登録モデルごとに使う接続を選ぶ（未指定のモデルはプライマリ接続）。画面はカードの中の共有の `Tabs` で
プライマリ接続 → セカンダリ接続 → ターシャリ接続の順に切り替え、登録モデルの「接続」の選択肢もタブと同じ名前を出す（設定していない
接続は出さない）。内部の ID は `primary` / `secondary` / `tertiary`、上限は `pr_system_settings.model.MAX_ENTERPRISE_AI_CONNECTIONS`（3）。
接続はプライマリ接続を先頭にこの順で並べ、セカンダリ接続を置かずにターシャリ接続だけを置いてもよい（並びが違う・重複する payload は 422）。

- **既存環境の更新は要らない。** プライマリ接続は今までの属性・変数名（`PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT` / `_PROJECT_OCID` /
  `_API_KEY`）のままで、保存済みの `model-settings.json`（接続 1 組の形）は、読み込むときにプライマリ接続として扱う。画面で保存し直すと、
  `enterprise_ai.connections`（secret なし）と、登録モデルの `connection_id` を書く。
- **接続の表示名は廃止した（#542）。** #533 で入れた `PLATFORM_OCI_ENTERPRISE_AI_CONNECTION_NAME` /
  `PLATFORM_OCI_ENTERPRISE_AI_SECONDARY_CONNECTION_NAME` は読まない（旧名との互換は持たない。`.env` に残っていても無視されるので、
  消してよい）。API の接続の `display_name` もない。表示名が残った `model-settings.json`・payload は、その項目を無視して読み込む
  （次に保存すると消える）。
- 必須の欄: プライマリ接続の 3 つの欄は OCI で運用するときだけ必須（画面は「OCI 運用時必須」。保存は止めない）。セカンダリ接続は、
  設定したら Endpoint URL・Project OCID・API key がすべて必須（backend も 422 で止める。API key は保存後の値で確かめ、空欄は
  保存済みの key を保持する）。セカンダリ接続の API key だけを消す指定はなく、消すときはセカンダリ接続ごと削除する。
- **ターシャリ接続（#786）は OpenAI / OpenAI 互換 API 向け**（OCI Enterprise AI にも使える）。設定したら Endpoint URL と API key が
  必須で、Project OCID は任意（OCI Enterprise AI に使うときだけ入れる。入れたときだけ `OpenAI-Project` ヘッダーを送る）。
  呼び出しはほかの接続と同じ Responses API（`{Endpoint URL}/responses`）で、Chat Completions は使わない。画面の Endpoint URL の例は
  `https://api.openai.com/v1`。API key だけを消す指定はなく、消すときはターシャリ接続ごと削除する（使っていた登録モデルはプライマリ接続へ移す）。
- セカンダリ接続・ターシャリ接続の変数（共通 `.env`。`PLATFORM_SETTING_FIELDS` に登録済み）。Endpoint URL があるときだけその接続が
  有効になる。

  | 変数 | 内容 |
  |---|---|
  | `PLATFORM_OCI_ENTERPRISE_AI_SECONDARY_ENDPOINT` | セカンダリ接続の Endpoint URL |
  | `PLATFORM_OCI_ENTERPRISE_AI_SECONDARY_PROJECT_OCID` | セカンダリ接続の Project OCID |
  | `PLATFORM_OCI_ENTERPRISE_AI_SECONDARY_API_KEY` | セカンダリ接続の API key（`platform/.env` だけに保存。JSON・API の応答には含めない） |
  | `PLATFORM_OCI_ENTERPRISE_AI_TERTIARY_ENDPOINT` | ターシャリ接続の Endpoint URL（例: `https://api.openai.com/v1`） |
  | `PLATFORM_OCI_ENTERPRISE_AI_TERTIARY_PROJECT_OCID` | ターシャリ接続の Project OCID（任意。OCI Enterprise AI に使うときだけ） |
  | `PLATFORM_OCI_ENTERPRISE_AI_TERTIARY_API_KEY` | ターシャリ接続の API key（`platform/.env` だけに保存。JSON・API の応答には含めない） |

- `.env` だけで設定する場合は、登録モデル（`PLATFORM_OCI_ENTERPRISE_AI_MODELS`）に `"connection_id": "secondary"`（ターシャリ接続は `"tertiary"`）を書く。
- **Endpoint URL は入力どおり使い、path を補わない（#1440）。** 呼ぶ URL は `{Endpoint URL}{API path}`（既定 `/responses`）で、
  3 製品の呼び出し・接続テストはすべて `pr_system_settings.model` の `enterprise_ai_base_url` / `enterprise_ai_request_url`
  （接続の `base_url` / `request_url()`）で決める（前後の空白と末尾の `/` だけ除く）。OCI Enterprise AI も `/openai/v1` まで書き、
  OpenAI 互換 API は API の base URL まで書く。

  | 接続先 | Endpoint URL | 呼ぶ URL |
  |---|---|---|
  | OCI Enterprise AI | `https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1` | `…/openai/v1/responses` |
  | OpenAI | `https://api.openai.com/v1` | `https://api.openai.com/v1/responses` |
  | OpenAI 互換 API（例 DeepSeek） | `https://api.deepseek.com/v1` | `https://api.deepseek.com/v1/responses` |

  以前の RAG（`rag_engine`）は path の無い Endpoint URL に `/openai/v1` を足していたため、`https://api.deepseek.com` では
  `https://api.deepseek.com/openai/v1/responses`（404）を呼び、Agent（`https://api.deepseek.com/responses`）とずれていた。いまはどの製品も
  同じ URL を呼ぶ。OCI の root URL（`https://inference.generativeai.<region>.oci.oraclecloud.com`）だけを書いた接続は `/responses` が
  404 になるので、`/openai/v1` まで書き直す。`rag_engine` / `rag_parser_core` は platform に依存しないため、RAG の backend が
  `base_url` で決めた値を渡し、両者は受け取った値をそのまま使う。
- 実行時は、モデルを呼ぶたびに `enterprise_ai_connection_for_model(settings, model_id)` でそのモデルの接続を引く（3 製品の呼び出し・
  接続テスト・RAG の readiness / NL2SQL の診断）。接続を持たない ID（登録モデルにない ID、消えた接続を指すモデル）はプライマリ接続を使う。
- env だけを受け取る部品は、使うモデルの接続を渡す。
  - RAG の OCI の parser（`service_runtime_env`）: VLM 抽出だけなので **既定の Vision モデルの接続** を `PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT`
    / `_API_KEY` / `_PROJECT_OCID` に書く（接続を変えたら「サービス管理」から parser を再起動する）。
  - RAG の回答フローの回答（`build_engine_settings`）: 回答は **既定のテキストモデルの接続**（`OCI_ENTERPRISE_AI_*`）、根拠画像の読み取りは
    **既定の Vision モデルの接続**（`OCI_ENTERPRISE_AI_VLM_ENDPOINT` / `_VLM_API_KEY` / `_VLM_PROJECT_OCID`）。
  - RAG の共有 client（`rag_parser_core.OciEnterpriseAiConfig`）: テキストはテキストのモデルの接続、Vision の呼び出し（VLM 抽出・
    図の読み取り・Files API）は `vision_oci_enterprise_ai_*`（`for_vision()`）で既定の Vision モデルの接続を使う。

## 登録モデルごとの入力の上限（`max_input_tokens`。#1437）

「システム設定 › モデル」の登録モデルは、行ごとに「入力の上限（tokens）」（モデルが 1 回の呼び出しで受け付ける入力の token の数。コンテキストウィンドウ）を持つ。

- 既定は **128000**（値の無いモデル・#1437 より前の設定・env だけで設定したモデル）。範囲は 4096〜2000000 の整数で、範囲外は保存（422）・読み込みとも検証エラー。
- 値の解釈は `pr_system_settings.model` の 1 か所: `max_input_tokens_to_use`（値なし・範囲外は既定）、`enterprise_ai_max_input_tokens(settings, model_id)`（モデルの値）、`input_token_budget(上限, 割合%)`（入力の予算の token）、`estimate_input_tokens(text)`（本文の token の見積もり。ASCII は 4 字で 1 token、それ以外は 1 字で 1 token。モデルごとの tokenizer を持たないため多めに見積もる）。3 製品はこれを使う。
- 使う製品: RAG（回答の章節の拡張の根拠の入力の上限と、長い章節を抽出してから統合する 1 回分の大きさ。backend が回答のモデルの値を `OCI_ENTERPRISE_AI_MAX_INPUT_TOKENS` で `rag_engine` に渡す）。3 製品とも、出力の上限を入力の上限の残りで抑えるのに使う（#1719）。
- 保存の形式: `model-settings.json` は version 5 で、`enterprise_ai.models[].max_input_tokens` を書く。version 4 はそのまま読める（書き換えは要らない。画面で保存し直すと version 5 になる）。
- 画面: 登録モデルの表に「入力の上限（tokens）」の列を足し、8 列になったため表の形は 2xl（1536px）から（md〜2xl は 1 つのモデルの欄を 2 列に並べる）。#1719 で「出力の上限（tokens）」の列をその右に足した。範囲外は保存の操作で欄の直下にエラーを出してフォーカスする（登録モデルの form はブラウザの既定の検証を出さない）。

## 登録モデルごとの出力の上限（`max_output_tokens`。#1719）

「システム設定 › モデル」の登録モデルは、行ごとに「出力の上限（tokens）」（1 回の応答で出せる token の数。推論のあるモデルは推論の token も含む）を持つ。入力の上限は prompt と応答の合計（context）で、出力の上限はその中の応答の分。

- 既定は **32768**（値の無いモデル・#1719 より前の設定・env だけで設定したモデル。入力の上限がそれより小さいモデルは入力の上限）。範囲は 1〜2000000 の整数で、**入力の上限以下**。範囲外・入力の上限を超える値は保存（422）・読み込みとも検証エラー。
- OCI の公式文書（2026-10-13 に確認）: `xai.grok-4.3` は context 1M tokens（https://docs.oracle.com/en-us/iaas/Content/generative-ai/xai-grok-4-3.htm ）、`openai.gpt-oss-120b` は context 128k tokens（https://docs.oracle.com/en-us/iaas/Content/generative-ai/openai-gpt-oss-120b.htm ）。モデルで上限が大きく違うため、業界の一般的な作り（モデルごとに入力の上限と出力の上限を持つ）に合わせ、128k 以上も入れられる範囲にした。
- 呼び出しの `max_output_tokens` は `enterprise_ai_output_token_budget(settings, model_id, prompt_tokens=…, requested=…)`: min(機能の要る量（既定 32768 を下回らない。無ければモデルの出力の上限）, モデルの出力の上限, 入力の上限 − prompt の見積もり)。出力の上限の大きいモデルで prompt が入らずに失敗するのを防ぐ。
- 未完了（`status=incomplete`・`incomplete_details.reason=max_output_tokens`）は、`enterprise_ai_retry_output_token_budget` でモデルの出力の上限まで広げて 1 回、それでも未完了（広げられないときを含む）なら推論の量を 1 段下げて（`lower_reasoning_effort`。high → medium → low。low・none は下げない）1 回だけ送り直す（#2023。`content_filter` などほかの理由は送り直さない。`pr_backend_core.bounded_calls` を通す呼び出しは、送り直しを実行器の `adaptive` の再試行に任せる）。それでも未完了なら、`pr_system_settings.enterprise_ai_errors.enterprise_ai_incomplete_message` で理由・上限・使った token（出力・推論）を失敗の文に出す（応答の本文・secret は出さない）。
- 共通の LLM / VLM の出力の上限（#1719 より前の `llm_max_output_tokens`（既定 1200）/ `vlm_max_output_tokens`、`PLATFORM_OCI_ENTERPRISE_AI_LLM_MAX_OUTPUT_TOKENS` / `_VLM_MAX_OUTPUT_TOKENS`）は持たない。画像の処理も Vision のモデルの出力の上限を使う。
- 保存の形式: `model-settings.json` は version 6 で、`enterprise_ai.models[].max_output_tokens` を書く。version 5 はそのまま読め、共通の `llm_max_output_tokens` / `vlm_max_output_tokens` は読み捨てる（各モデルは既定の 32768）。画面で保存し直すと version 6 になる。
- 2026-10-13 の実測（NL2SQL のコメント管理の SQL 生成と同じ要求。5 表・32 列）: `openai.gpt-oss-120b`（推論 medium・上限 1200）は `incomplete`（output 1200 のうち reasoning 1198、本文 0 文字）、上限 32768 では completed（output 2756、うち reasoning 1506）。`xai.grok-4.3`（推論 medium）は上限 1200 でも completed のことがあり（output 3300〜3700）、上限 300 では incomplete。

## 登録モデルごとの推論の量（`reasoning_effort`。#1425）

「システム設定 › モデル」の登録モデルは、行ごとに「推論の量」（モデルが回答の前に考える量。Responses API の `reasoning.effort`）を持つ。

- **選択肢は `none` / `low` / `medium` / `high` の 4 つ、既定は `medium`。** 新しく追加したモデルも、値を書いていない保存済みのモデル
  （`model-settings.json` の version 3 まで・`.env` の `PLATFORM_OCI_ENTERPRISE_AI_MODELS`）も `medium` になり、登録モデルにない ID
  （`.env` だけで決めたモデルなど）も `medium` を送る。選択肢以外の値（`minimal` / `xhigh` など）は保存（422）でも読み込みでも検証エラー。
- **`none` は「送らない」**（`reasoning` を送らず、モデル（endpoint）の既定のまま。画面は「none（送らない）」）。それ以外はその値を送る。
  値の解釈は `pr_system_settings.model.reasoning_effort_to_send` の 1 か所だけで、3 製品は `enterprise_ai_reasoning(settings, model_id)`
  （送る `{"effort": …}`、送らないなら `None`）か `enterprise_ai_reasoning_effort_to_send`（送る値、送らないなら空）を使う。
- 4 つは 3 つの登録モデルとも使える（2026-10-11 の実測。`minimal` は `openai.gpt-oss-120b` が 400「Harmony は未対応」、
  `xhigh` / `max` は `claude-opus-5` だけが受け付けるので選択肢に入れない。`none` は送らないのでどのモデルでも使える）。
  モデルを足したときは、行の「テスト」で選んだ値でモデルが応答するかを確かめる（テストは保存前に選んだ値で呼ぶ）。
- 保存の形式: `model-settings.json` は version 4 で、`enterprise_ai.models[].reasoning_effort` を書く。version 3 はそのまま読める
  （書き換えは要らない。画面で保存し直すと version 4 になる）。
- 送る所（3 製品とも、Enterprise AI を呼ぶ所すべて。payload template の構成では template に任せる）:
  - RAG: 共有 client（`rag_parser_core.OciEnterpriseAiConfig` の `text_reasoning_effort` / `vision_reasoning_effort`。回答・要約・
    文書解析の VLM・図の読み取り・接続テスト）、回答フローの `responses.parse`（`build_engine_settings` の
    `OCI_ENTERPRISE_AI_REASONING_EFFORT` / `OCI_ENTERPRISE_AI_VLM_REASONING_EFFORT`。回答・評価・検証）、OCI の parser
    （`service_runtime_env` の `PLATFORM_OCI_ENTERPRISE_AI_TEXT_REASONING_EFFORT` / `_VISION_REASONING_EFFORT`。値を変えたら
    「サービス管理」から parser を再起動する）。env・config で渡す値は「送る値」（`none` は空）。
  - NL2SQL: `enterprise_ai_client`（SQL 生成・評価・オントロジーの VLM など、テキストと画像のすべての呼び出しと接続テスト）。
  - Agent: 組み込み Runtime の SDK の `ModelSettings(reasoning=…)`（Run と品質評価の判定）とモデルの接続テスト。
- NL2SQL の Select AI・Select AI Agent には効かない（AI profile（`DBMS_CLOUD_AI`）に推論の量の属性が無い。画面の推論の量の説明と、
  業務プロファイルの Select AI 設定に書いている）。
- `max_output_tokens` は思考の token も含むので、推論の量を減らすと出力上限での未完了（#987）も減る。
- 推論型のモデルの 1 回の呼び出しが数十秒になるため、RAG の回答の上限 `RAG_ANSWER_TIMEOUT_SECONDS` の既定を 600 秒（上限と同じ）にし、
  Agent の接続 `rag` の待ち時間 `AGENT_EXTERNAL_RAG_TIMEOUT_SECONDS` の既定を 630 秒（MCP のタイムアウト秒の上限は 900 秒）にした。
  画面（630 秒）は前から回答の上限より長い（前段に reverse proxy を置くときは、その待ち時間も回答の上限より長くする。#19）。

## LLM / VLM の呼び出しの seed は送らない（#3529）

3 製品の Enterprise AI の呼び出し（Responses API の top-level の `seed`）にも、NL2SQL の Select AI の AI profile（`DBMS_CLOUD_AI`）の
`seed` 属性にも、seed を入れない。設定・画面の欄も持たない。seed を受け付けないモデル（未知の引数を拒む endpoint）では、すべての
呼び出しが HTTP 400（`Unknown parameter: 'seed'`）になるため。受け付けるモデルでも出力の再現性はモデル側の対応次第だった。
保存済みの `model-settings.json` の `enterprise_ai.seed` は読み込みで読み捨てる。

## モデルが受け付けない引数は外して送り直す（#3539）

3 製品の Enterprise AI の呼び出しは、受け付けるモデルには今までどおり `temperature`（主に 0。同じ入力から同じ出力を
得やすくする）を送る。サンプリングの引数を受け付けない推論モデル（OpenAI の推論モデルは推論の量が `none` 以外のとき
`temperature`・`top_p`・`top_logprobs` などを受け付けない）が HTTP 400 で「受け付けない引数」
（`Unsupported parameter: 'temperature'` / `Unknown parameter: 'X'`）を返したら、その引数を外して 1 回だけ送り直す
（業界の型: LiteLLM の `drop_params`）。

- 判定と記憶は `pr_backend_core.llm_request_params`（標準 library だけ）。引数の名前は `error.param`、無ければ message の
  引用から読む。外すのは許可の一覧（サンプリングと生成の調整: `temperature`・`top_p`・`top_k`・`frequency_penalty`・
  `presence_penalty`・`logprobs`・`top_logprobs`・`logit_bias`・`seed`。引数そのものを受け付けないときの
  `reasoning` / `reasoning_effort`）の top-level の引数だけ。`model`・`input`・応答の形式・`tools`・出力の上限などの
  400 と、推論の量の値を拒む 400（`Unsupported value`。選んだ値の誤り）は今までどおり失敗にする。
- 外した引数は（接続の URL, モデル ID）ごとに backend のプロセスの間だけ覚え、次の呼び出しから最初に外す（再起動で
  忘れる）。初めて外したときに WARNING の log を 1 回だけ残す（payload は出さない）。
- 組み込みは `pr_backend_core.llm_request_params_httpx`（httpx の client の `send` を包む。環境のプロキシの扱いは
  変えない）: RAG は OpenAI SDK の `http_client`（`ParameterDroppingClient` / `AsyncParameterDroppingClient`）、
  NL2SQL とモデルの接続テスト（Agent）は `send_dropping_parameters` / `asend_dropping_parameters`、Agent の組み込み
  Runtime・チャットの添付の画像の読み取りは SDK の既定の client に同じ扱いを足した client。
- 「システム設定 › モデル」のモデルのテストは、外した引数を結果の詳細（`dropped_parameters`）と成功の文に出す。
