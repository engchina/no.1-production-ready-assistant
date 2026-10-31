import { createUiStore } from "@production-ready/ui";

// UI ストア（サイドバー開閉等）は共有 UI パッケージの factory で生成する。
// 永続化キーは RAG 専用 namespace を維持し、旧バージョンの単独キーから移行する。
export const UI_STORAGE_KEY = "production-ready-rag.ui";
const LEGACY_SIDEBAR_COLLAPSED_STORAGE_KEY = "production-ready-rag.sidebarCollapsed";

export const useUiStore = createUiStore({
  storageKey: UI_STORAGE_KEY,
  legacyCollapsedKey: LEGACY_SIDEBAR_COLLAPSED_STORAGE_KEY,
  mobileBreakpoint: 640,
  sectionKeyMigrations: { "nav.section.rag": "nav.section.use" },
});
