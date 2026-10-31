import { useConfirm } from "@production-ready/ui";

import { useUnsavedChangesGuard } from "./useUnsavedChangesGuard";

/** 下書き破棄の確認ダイアログの既定の文言。製品側は `messages` で上書きできる。 */
export const DRAFT_GUARD_MESSAGES = {
  discardTitle: "変更を破棄しますか",
  discardDescription: "保存されていない変更があります。移動すると編集内容は破棄されます。",
  discardConfirm: "破棄して移動",
};

export type DraftGuardMessages = typeof DRAFT_GUARD_MESSAGES;

/**
 * 設定の編集と進行中の操作を離脱時に保護する（NL2SQL から移設。#97）。
 * 戻り値の `confirmLeave()` は、画面内のボタンで別画面へ移る前に呼ぶ。秘密情報は保存しない。
 */
export function useSettingsDraftGuard(
  isDirty: boolean,
  busy: boolean,
  messages?: Partial<DraftGuardMessages>,
) {
  const confirm = useConfirm();
  const m = { ...DRAFT_GUARD_MESSAGES, ...messages };
  const confirmLeave = async () =>
    !busy &&
    (!isDirty ||
      (await confirm({
        title: m.discardTitle,
        description: m.discardDescription,
        confirmLabel: m.discardConfirm,
        tone: "danger",
        dismissOnOverlay: false,
      })));
  useUnsavedChangesGuard(isDirty || busy, confirmLeave);
  return confirmLeave;
}
