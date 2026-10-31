import { createUiStore } from "@production-ready/ui";

// UI ストア（サイドバー開閉等）。永続化キーは NL2SQL 専用 namespace。
export const UI_STORAGE_KEY = "production-ready-nl2sql.ui";

export const useUiStore = createUiStore({
  storageKey: UI_STORAGE_KEY,
  mobileBreakpoint: 640,
});

/** 分割ペインの比率を保存する localStorage key の前置き（共有 FixedSplitPane の既定から変えず、保存済みの比率を保つ）。 */
export const FIXED_SPLIT_STORAGE_PREFIX = "production-ready-nl2sql.fixedSplitPane";

/** 一覧 + 詳細（共有 ListDetailLayout）の一覧の幅と開閉を保存する localStorage key の前置き（#2281）。 */
export const LIST_DETAIL_STORAGE_PREFIX = "production-ready-nl2sql.listDetail";
