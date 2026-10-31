import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

import adherence from "../../platform/docs/design-system/adherence.oxlintrc.json" with { type: "json" };

const { rules: adherenceRules } = adherence.overrides[0];

// Agent の全画面は画面幅いっぱい（共有 PageHeader / PageBody の `wide`）で統一する（#28、NL2SQL #566 と同じ基準）。
// PageHeader と PageBody の wide がずれると 1920px 以上でタイトルと本文の左端がずれるため、
// 補助の PageBody（読み込み中・エラー表示）も含めて `wide` を値なしで必ず渡す。単体テスト（vitest）は src/lib の関数だけで画面を描かないので lint で検査する。
const PAGE_LAYOUT_ELEMENT = "JSXOpeningElement[name.name=/^Page(Header|Body)$/]";
const pageWidthRules = [
  {
    selector: `${PAGE_LAYOUT_ELEMENT}:not(:has(> JSXAttribute[name.name='wide']))`,
    message: "PageHeader / PageBody には wide を渡してください（Agent は全画面 wide。付け忘れると 1440px に戻り左端がずれます）。",
  },
  {
    selector: `${PAGE_LAYOUT_ELEMENT} > JSXAttribute[name.name='wide'][value!=null]`,
    message: "wide は値なし（wide）で渡してください。画面ごとに wide={…} で切り替えないでください。",
  },
];

export default tseslint.config(
  { ignores: ["dist", "node_modules", "playwright-report", "test-results"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{js,mjs,cjs,ts,tsx}"],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
  // デザインシステムの adherence ルール。正本は platform の docs/design-system/adherence.oxlintrc.json で、
  // ルールをコピーせず同じセレクタを ESLint 標準の no-restricted-syntax / no-restricted-imports に渡す
  // （platform AGENTS.md「lint」節）。CI は platform を sibling に checkout するので同じ相対パスで解決できる。
  // アプリ固有のルールを足す場合は、同じルール名で上書きせず別のルール名にする（adherence のセレクタが消える）。
  // react-hooks 7 の recommended（React Compiler 系の set-state-in-effect / refs / purity 等を含む）をそのまま使う
  // （#179。RAG #154 と同じ方針）。
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: reactHooks.configs.recommended.rules,
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      // adherence のセレクタを残したまま、全画面 wide の検査を末尾に足す（同じルール名で上書きしない）。
      "no-restricted-syntax": [...adherenceRules["design-system/restricted-syntax"], ...pageWidthRules],
      "no-restricted-imports": adherenceRules["no-restricted-imports"],
    },
  },
);
