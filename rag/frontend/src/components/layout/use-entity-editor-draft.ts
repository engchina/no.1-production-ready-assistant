import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

import { useConfirm } from "@/components/ui/confirm-dialog";
import { t } from "@/lib/i18n";
import { useCustomLeaveGuard } from "@/lib/leave-guard";
import {
  readWorkspace,
  removeWorkspace,
  writeWorkspace,
  type WorkspaceField,
} from "@/lib/workspace-state";

/**
 * A 型の全画面エディタ（一覧 → 作成 / 編集）の未保存の下書き（platform UX 契約 workspace-state.md、
 * RAG の docs/frontend-workspace-state-spec.md）。検索・回答プロファイルとナレッジベースのエディタで共有する（#555）。
 *
 * - 下書きは同じタブの sessionStorage に、対象ごとの scope（`new` または対象の ID）で残す。
 *   開き直すと復元し、`discard` か保存（`markSaved`）で消す。
 * - dirty は `signature` で比べる（前後の空白や、順序に意味のない集合を正規化するのは呼び出し側）。
 * - dirty の間は、サイドナビ・内部リンク・再読込・ブラウザの戻る / 進むで確認する。下書きはこのタブに
 *   残るため、確認は「破棄」ではなく「移動する」。画面内の「一覧へ戻る」は `confirmLeave` を先に通す。
 */
export interface EntityEditorDraftOptions<T> {
  field: WorkspaceField;
  /** `?id=` の値（新規は `new`、編集は対象の ID）。 */
  scope: string;
  /** サーバーの値（新規は空の値）。保存に成功するまでの dirty の基準。 */
  initial: T;
  isDraft: (value: unknown) => value is T;
  signature: (value: T) => string;
  /** 離脱の確認の説明（対象ごとの文言。タイトルと確定のラベルは共通）。 */
  leaveDescription: string;
}

export interface EntityEditorDraft<T> {
  draft: T;
  setDraft: Dispatch<SetStateAction<T>>;
  dirty: boolean;
  /** 保存していない下書きを復元して開いたか（dirty の間だけ true）。 */
  restored: boolean;
  /** 変更を元に戻す（基準の値へ戻し、下書きを消す）。 */
  discard: () => void;
  /** 保存に成功した値を新しい基準にする（下書きも消える）。 */
  markSaved: (saved: T) => void;
  /** 画面内の移動（一覧へ戻る など）の前に呼ぶ。dirty でなければ確認せずに true。 */
  confirmLeave: () => Promise<boolean>;
}

/** 保存している下書きを読む（一覧の「下書きを開く」の判定にも使う）。 */
export function readEditorDraft<T>(
  field: WorkspaceField,
  scope: string,
  isDraft: (value: unknown) => value is T
): T | null {
  return readWorkspace<T | null>(
    field,
    null,
    (value): value is T | null => value === null || isDraft(value),
    scope
  );
}

export function useEntityEditorDraft<T>({
  field,
  scope,
  initial,
  isDraft,
  signature,
  leaveDescription,
}: EntityEditorDraftOptions<T>): EntityEditorDraft<T> {
  const confirm = useConfirm();
  const [baseline, setBaseline] = useState<T>(initial);
  const [restored] = useState(() => readEditorDraft(field, scope, isDraft));
  const [draft, setDraft] = useState<T>(restored ?? initial);
  const dirty = signature(draft) !== signature(baseline);

  useEffect(() => {
    if (dirty) writeWorkspace(field, draft, scope);
    else removeWorkspace(field, scope);
  }, [dirty, draft, field, scope]);

  const confirmNavigation = () =>
    confirm({
      title: t("editor.leaveGuard.title"),
      description: leaveDescription,
      confirmLabel: t("editor.leaveGuard.confirm"),
      tone: "warning",
      dismissOnOverlay: false,
    });
  useCustomLeaveGuard(dirty, confirmNavigation);

  return {
    draft,
    setDraft,
    dirty,
    restored: restored !== null && dirty,
    discard: () => setDraft(baseline),
    markSaved: (saved) => {
      setBaseline(saved);
      removeWorkspace(field, scope);
    },
    confirmLeave: async () => !dirty || (await confirmNavigation()),
  };
}
