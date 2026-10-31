import js from "@eslint/js";
import tsParser from "@typescript-eslint/parser";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import reactHooks from "eslint-plugin-react-hooks";
// CI / ローカルとも platform repo を sibling に置く（package.json の file: リンクと同じ前提）。
import adherence from "../../platform/docs/design-system/adherence.oxlintrc.json" with { type: "json" };

const { rules: adherenceRules } = adherence.overrides[0];

const browserGlobals = {
  AbortController: "readonly",
  Blob: "readonly",
  File: "readonly",
  FormData: "readonly",
  Headers: "readonly",
  URLSearchParams: "readonly",
  console: "readonly",
  document: "readonly",
  fetch: "readonly",
  localStorage: "readonly",
  navigator: "readonly",
  process: "readonly",
  setTimeout: "readonly",
  window: "readonly",
};

export default [
  {
    ignores: ["dist/**", "node_modules/**", "coverage/**", "*.tsbuildinfo"],
  },
  js.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}", "vite.config.ts"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaFeatures: { jsx: true },
        ecmaVersion: "latest",
        sourceType: "module",
      },
      globals: browserGlobals,
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
      "react-hooks": reactHooks,
    },
    rules: {
      ...tsPlugin.configs.recommended.rules,
      // react-hooks 7 の recommended（React Compiler 系の set-state-in-effect / refs / purity 等を含む）をそのまま使う（#154）。
      ...reactHooks.configs.recommended.rules,
      "no-undef": "off",
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
    },
  },
  // デザインシステムの adherence ルール。platform の正本をコピーせず参照する（platform AGENTS.md「lint」節）。
  // oxlint の JS プラグイン用ルール名 design-system/restricted-syntax と同じセレクタを、ESLint 標準の
  // no-restricted-syntax に渡す。アプリ固有のルールを足す場合は同じルール名で上書きしない（セレクタが消える）。
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": adherenceRules["design-system/restricted-syntax"],
      "no-restricted-imports": adherenceRules["no-restricted-imports"],
    },
  },
];
