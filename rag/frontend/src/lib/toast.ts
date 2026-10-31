// Toast ストア/API は共有 UI パッケージ @production-ready/ui へ移管済み。
// 互換のため re-export。message/description/action.label には i18n 済み文字列を渡す方針は不変。
export {
  toast,
  useToastStore,
  type ToastItem,
  type ToastOptions,
  type ToastAction,
} from "@production-ready/ui";
