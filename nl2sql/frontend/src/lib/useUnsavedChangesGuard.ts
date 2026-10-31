// 未保存変更の離脱ガードは platform の共有パッケージに移した（#97）。既存の import を保つため re-export する。
export { useUnsavedChangesGuard } from "@production-ready/system-settings";
