import {
  confirmUnsavedChanges,
  useSettingsDraftGuard,
  useUnsavedChangesGuard,
  type DraftGuardMessages,
} from "@production-ready/system-settings";

import { t } from "@/lib/i18n";

/**
 * 未保存変更の離脱ガード（platform `docs/ux-contracts/workspace-state.md`）。
 *
 * 内部リンク（サイドナビ含む）の click と `beforeunload` は共有 hook が守る。
 * ログアウトのように `navigate()` で移動する経路は
 * `confirmPendingLeave()` を先に呼び、同じ確認ダイアログを通す。
 */
export function draftGuardMessages(): DraftGuardMessages {
  return {
    discardTitle: t("common.leaveGuard.title"),
    discardDescription: t("common.leaveGuard.description"),
    discardConfirm: t("common.leaveGuard.confirm"),
  };
}

/**
 * 編集画面の標準ガード。dirty のときは離脱を確認し、戻り値は画面内の移動前に呼ぶ確認関数。
 * `busy`（保存中など）の間は、共通のシステム設定の画面と同じく離脱そのものを止める。
 */
export function useLeaveGuard(isDirty: boolean, busy = false): () => Promise<boolean> {
  return useSettingsDraftGuard(isDirty, busy, draftGuardMessages());
}

/** 画面固有の確認文言を使うガード（例: 抽出確認の編集）。 */
export function useCustomLeaveGuard(enabled: boolean, confirmLeave: () => Promise<boolean>): void {
  useUnsavedChangesGuard(enabled, confirmLeave);
}

/**
 * `navigate()` で移動する前に呼ぶ。dirty な画面がなければ即 true。確認は 1 回だけ出す。
 * 未保存の画面の一覧は共有のガードが持つ（戻る / 進むの blocker と同じ。#586）。
 */
export const confirmPendingLeave = confirmUnsavedChanges;
