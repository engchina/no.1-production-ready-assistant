# AGENTS.md — Production Ready Platform

> 共通基盤（`platform/`）固有のルール。GitHub 運用・Issue / PR 規約・CI・デザインシステムの共通ルール・共通の技術方針は、root の [../AGENTS.md](../AGENTS.md) と、そこから案内する root の `docs/agents/` を先に適用する。ルールを変えるときはこのファイル（共通ルールは root）を編集する。
> 共有パッケージの運用（版管理・変更時の確認範囲）は [CONTRIBUTING.md](./CONTRIBUTING.md)。Issue の label と PR title の scope は `platform`。

## デザインシステム / UI

- **`packages/ui` と各製品の `frontend/` の UI は `docs/design-system/` が正本。** 依存の向き・製品が持てるもの・禁止事項・画面の構成・UI 変更の検証は root の [docs/agents/ui.md](../docs/agents/ui.md) に従う。
- 共通の権限管理（`packages/system-settings` の `RolePermissionsPage`）は、渡された権限の一覧の順にグループを並べる。一覧は製品が `permissionNavSections` / `arrangePermissionsByNav` で左のナビにそろえてから渡す（root の [docs/agents/shared-features.md](../docs/agents/shared-features.md)）。
- 1 製品しか使わないもの（`WorkflowProgressStrip`、オントロジーグラフ等）はその製品に置いてよい。

### `packages/ui` の実装の規則

- **回すのは `Spinner` だけ。** `packages/ui` の部品の中でも、`Button` の `loading`・`ProcessingIndicator` / `TimedLoadingState`・`ChatProgress`・`LoadMoreFooter` などは `Spinner` を通す。`Spinner` の形（全周のトラック + 180 度対称の 2 本のアーク）と箱（回転しない固定の正方形の `span.pr-spinner` の中で svg だけが回る）を崩さない（理由は README §4「`Spinner`」。#1180）。
- アイコンは `lucide-react` のコンポーネントを `LucideIcon` として受け取る（`icon={Upload}`。Lucide 名の文字列では受け取らない）。
- `wide` の画面では、カード・危険な操作の区画・一覧のコンテナは 100% の幅を使う（max-width を付けない）。例外はフォームの欄の並び（`FormGrid` が `--form-max-width` 60rem で止める）と、一覧の検索欄（`ListToolbar` が `lg` 28rem で固定する）（README §4「wide 画面の 100% 充填」。#1891）。

### `packages/ui` の変更の検証

- ライト / ダーク、1280px / 1920px、キーボード操作（Tab 順・フォーカスリング・`Tabs` の ← → / Home / End）を確かめる。
- 色・コントラストに関わる変更は、`docs/design-system/reference/*.html` をブラウザで開いて実測値と突き合わせる。
- 状態を表す UI はアイコンでも符号化し、グレースケールでも判別できるか確かめる。
- `docs/design-system/README.md` §9 の検収基準を PR の `検証結果` に転記する。

### lint

`docs/design-system/adherence.oxlintrc.json` がデザインシステムの遵守ルールの正本。新しいコードにこの lint を通すことで、デザインシステムからのずれを止める。

- 対象は各製品の `frontend/src/**/*.{ts,tsx}` と `packages/system-settings/src`（`packages/system-settings/.oxlintrc.json`。`npm run lint`。CI は `Platform / UI`。#1891）。検出する規則の一覧と理由は、adherence の各ルールの `message` と README の表（§0）にある。
- 規則のテストは RAG の `frontend/src/design-system-adherence.test.ts`（ESLint の `lintText` で検出と許容の例を確かめる）。規則を足すときは同じテストに例を足す。
- prop の妥当性は TypeScript の型チェックに任せ、lint では検査しない。
- oxlint にはネイティブの `no-restricted-syntax` が無いため、同じ `{selector, message}` を受け取る JS プラグイン `docs/design-system/design-system-plugin.mjs` を置き、adherence 設定から相対パスで読み込む。
- **各製品はルールもプラグインもコピーせず、相対パス（`../../platform/docs/design-system/…`）で参照する**（コピーすると変更が届かない）。

```jsonc
// oxlint（frontend/.oxlintrc.json）
{
  "extends": ["../../platform/docs/design-system/adherence.oxlintrc.json"]
}
```

```js
// ESLint（frontend/eslint.config.mjs）— 同じセレクタを ESLint 標準の no-restricted-syntax に渡す
import adherence from "../../platform/docs/design-system/adherence.oxlintrc.json" with { type: "json" };
const { rules } = adherence.overrides[0];
export default [
  // …
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": rules["design-system/restricted-syntax"],
      "no-restricted-imports": rules["no-restricted-imports"],
    },
  },
];
```

- adherence 設定は oxlint と ESLint の両方が読むため、コメントや独自キーの無い純粋な JSON に保つ（未知のキーがあると oxlint が読み込みに失敗する）。
- ルールを変えるときは、NL2SQL（oxlint）と RAG / Agent（ESLint）の `src` で違反件数を確かめて PR の `検証結果` に書く。違反が残るなら、同じ PR で製品を直すか、製品ごとの追従 Issue を作ってから merge する。
- 製品固有のルールは各製品の設定に足してよいが、同じルール名（`design-system/restricted-syntax` / `no-restricted-syntax`）は上書きしない（adherence のセレクタが消える）。

## CI / 検証コマンド

CI の job は `Platform / UI`・`Platform / backend_core`・`Platform / system_settings_backend`・`Platform / evaluation_scoring`・`Platform / enterprise_ai` と、影響を受ける全製品の job（root の [docs/agents/ci.md](../docs/agents/ci.md)）。ローカルでは変更した package の command を実行し、製品の全件の検査は CI の job の結果を引用してよい。

```bash
# frontend（共有 UI）— platform/ で実行
npm ci && npm run typecheck && npm run lint && npm test && npm run build

# backend（production-ready-backend-core）
cd packages/backend_core
uv sync --locked --dev
uv run ruff format --check . && uv run ruff check . && uv run mypy src
uv run pytest --cov=pr_backend_core && uv run bandit -r src && uv run pip-audit
```

- 1 台の Compute の配備（`deploy/`）のテストは `for t in deploy/tests/*.test.sh; do bash "$t"; done`（nginx と openssl が要る。CI は `Suite / Deploy`）。
