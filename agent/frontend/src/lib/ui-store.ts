import { createUiStore } from "@production-ready/ui";

// UI ストア（サイドバー開閉等）。永続化キーは Agent 専用 namespace。
export const UI_STORAGE_KEY = "production-ready-agent.ui";

export const useUiStore = createUiStore({
  storageKey: UI_STORAGE_KEY,
  mobileBreakpoint: 640,
});
