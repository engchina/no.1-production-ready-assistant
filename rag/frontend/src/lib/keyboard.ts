/**
 * キーボード入力の判定。IME 対応の判定は 3 製品で共有する（@production-ready/ui、#535）。
 * 既存の import（`@/lib/keyboard`）を保つための再 export。
 */
export { isImeComposing, isSubmitEnter } from "@production-ready/ui";
