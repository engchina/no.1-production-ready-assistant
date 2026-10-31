// 設定の下書き保護は platform の共有パッケージに移した（#97）。既存の import を保つため re-export する。
export { useSettingsDraftGuard } from "@production-ready/system-settings";
