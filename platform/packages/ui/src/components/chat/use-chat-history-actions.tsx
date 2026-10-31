import { Check, Pencil, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { apiErrorMessage } from "../../lib/api-error";
import { isSubmitEnter } from "../../lib/keyboard";
import { toast } from "../../store/toast-store";
import { Button } from "../ui/button";
import { useConfirm } from "../ui/confirm-dialog";
import { TextField } from "../ui/text-field";
import type { ChatHistoryItem } from "./chat-history-list";

/** 会話名の上限（3 製品の API と同じ。#1817）。 */
export const CHAT_HISTORY_TITLE_MAX_LENGTH = 80;

export interface ChatHistoryActionLabels {
  /** 行の「名前の変更」のボタンの名前（例:「「{title}」の名前を変更」）。 */
  rename: (title: string) => string;
  /** 名前の欄のラベル（画面には出さない）。 */
  renameLabel: string;
  renameSave: string;
  renameCancel: string;
  /** 空の名前を保存しようとしたときの文（欄の下に出す）。 */
  renameEmpty: string;
  /** 保存できなかったときの既定の文（API の文が無いとき）。 */
  renameError: string;
  /** 行の「削除」のボタンの名前。 */
  delete: (title: string) => string;
  deleteConfirmTitle: string;
  deleteConfirmDescription: (title: string) => string;
  deleteConfirm: string;
  /** 削除の結果（Toast）。 */
  deleted: string;
  /** 削除できなかったときの既定の文（Toast）。 */
  deleteError: string;
}

/**
 * 会話の名前の変更・削除の文言（3 製品で同じ。RAG の文を正本にした。#1817）。製品は i18n で上書きできるが、
 * 3 製品で同じ機能の文は同じにする（AGENTS.md「言語」）。
 */
export const DEFAULT_CHAT_HISTORY_ACTION_LABELS: ChatHistoryActionLabels = {
  rename: (title) => `「${title}」の名前を変更`,
  renameLabel: "会話名",
  renameSave: "会話名を保存",
  renameCancel: "会話名の変更を取り消す",
  renameEmpty: "会話名を入力してください。",
  renameError: "会話名を変更できませんでした。入力内容を確認して再試行してください。",
  delete: (title) => `「${title}」を削除`,
  deleteConfirmTitle: "会話を削除しますか？",
  deleteConfirmDescription: (title) =>
    `「${title}」を会話の履歴から削除します。削除した会話は開くことも続けることもできません。この操作は取り消せません。`,
  deleteConfirm: "削除",
  deleted: "会話を削除しました。",
  deleteError: "会話を削除できませんでした。時間をおいて再試行してください。",
};

export interface UseChatHistoryActionsOptions {
  /** 名前を保存する（前後の空白を除いた 1〜80 文字）。失敗は throw する（欄の下に文を出す）。 */
  onRename: (id: string, title: string) => Promise<unknown>;
  /** 会話を削除する。失敗は throw する（Toast で出す）。 */
  onDelete: (id: string) => Promise<unknown>;
  /**
   * 確認の後、削除の要求の前に呼ぶ（開いている会話なら送信を止めて選択を外す。削除した会話を開いたままにしない）。
   */
  onBeforeDelete?: (item: ChatHistoryItem) => void;
  /** 削除できた後（一覧の取り直しなど）。 */
  onDeleted?: (item: ChatHistoryItem) => void;
  /** 名前の欄の初期値（既定は行の名前）。 */
  initialTitle?: (item: ChatHistoryItem) => string;
  /** 操作を受け付けない間（送信の要求中など）。 */
  disabled?: boolean;
  /**
   * 行ごとに名前を変えられるか（既定はすべての行で可）。false の行は「名前の変更」を出さず、欄も開かない。
   * 他の利用者の会話（system_admin が見る）の名前は持ち主だけが変えられるので、その行で false を返す（削除は
   * 運用の操作として出す。#3244）。
   */
  canRename?: (item: ChatHistoryItem) => boolean;
  labels?: Partial<ChatHistoryActionLabels>;
  /** 失敗の文（既定は共通の `apiErrorMessage`）。 */
  errorMessage?: (error: unknown, fallback: string) => string;
  /** 名前の欄の id の接頭辞（同じ画面に 2 つ置くとき）。 */
  idPrefix?: string;
}

export interface ChatHistoryActions {
  /** `ChatHistoryList` の `renderActions` に渡す（行の右の「名前の変更」「削除」）。 */
  renderActions: (item: ChatHistoryItem) => ReactNode;
  /** `ChatHistoryList` の `renderEditor` に渡す（名前の変更の欄）。 */
  renderEditor: (item: ChatHistoryItem) => ReactNode | null;
  /** 名前を変えている会話（無ければ `null`）。 */
  editingId: string | null;
  /** 名前の変更をやめる（対象の画面を切り替えたときなど）。 */
  cancelRename: () => void;
}

/**
 * 会話の履歴の行の「名前の変更」「削除」（3 製品共通。UX 契約 page-archetypes.md §6、#1817）。
 *
 * - 名前の変更は行をその場の欄にする（Enter で保存・Esc で取り消し。空の名前は保存せず欄の下に理由を出し、
 *   保存の失敗も欄の下に出して欄にフォーカスを戻す。messaging.md §10）。
 * - 削除は取り消せないので確認のダイアログ（danger。scrim では閉じない。buttons.md §5.1）を通し、結果は Toast。
 * - 行の操作は `ChatHistoryList` が sm 以上でホバー・フォーカスの間と開いている会話で出す（モバイルは常に出す）。
 * - `canRename` が false の行（他の利用者の会話。#3244）は「名前の変更」を出さず、「削除」だけを出す。
 */
export function useChatHistoryActions({
  onRename,
  onDelete,
  onBeforeDelete,
  onDeleted,
  initialTitle,
  disabled = false,
  canRename,
  labels: labelOverrides,
  errorMessage = apiErrorMessage,
  idPrefix = "chat-history-title",
}: UseChatHistoryActionsOptions): ChatHistoryActions {
  const labels = { ...DEFAULT_CHAT_HISTORY_ACTION_LABELS, ...labelOverrides };
  const confirm = useConfirm();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editingId) inputRef.current?.focus();
  }, [editingId]);

  // 空の名前・保存の失敗の理由を出したら欄へ戻す（保存中は欄が disabled なので、保存が終わって描いた後）。
  useEffect(() => {
    if (error && !saving) inputRef.current?.focus();
  }, [error, saving]);

  const renameAllowed = (item: ChatHistoryItem) => (canRename ? canRename(item) : true);

  function startRename(item: ChatHistoryItem) {
    if (!renameAllowed(item)) return;
    setEditingId(item.id);
    setDraft(initialTitle ? initialTitle(item) : item.title);
    setError("");
  }

  function cancelRename() {
    setEditingId(null);
    setError("");
  }

  async function saveRename() {
    if (!editingId || saving) return;
    const title = draft.trim();
    if (!title) {
      setError(labels.renameEmpty);
      inputRef.current?.focus();
      return;
    }
    setError("");
    setSaving(true);
    try {
      await onRename(editingId, title);
      setEditingId(null);
    } catch (caught) {
      setError(errorMessage(caught, labels.renameError));
    } finally {
      setSaving(false);
    }
  }

  async function removeConversation(item: ChatHistoryItem) {
    const ok = await confirm({
      title: labels.deleteConfirmTitle,
      description: labels.deleteConfirmDescription(item.title),
      confirmLabel: labels.deleteConfirm,
      tone: "danger",
      dismissOnOverlay: false,
    });
    if (!ok) return;
    setDeletingId(item.id);
    try {
      onBeforeDelete?.(item);
      await onDelete(item.id);
      if (editingId === item.id) cancelRename();
      toast.success(labels.deleted);
      onDeleted?.(item);
    } catch (caught) {
      toast.error(errorMessage(caught, labels.deleteError));
    } finally {
      setDeletingId(null);
    }
  }

  return {
    editingId,
    cancelRename,
    renderEditor: (item) =>
      editingId === item.id && renameAllowed(item) ? (
        <div className="flex items-start gap-1">
          <TextField
            ref={inputRef}
            id={`${idPrefix}-${item.id}`}
            label={labels.renameLabel}
            labelHidden
            className="min-w-0 flex-1"
            value={draft}
            maxLength={CHAT_HISTORY_TITLE_MAX_LENGTH}
            required
            disabled={saving}
            error={error || undefined}
            onValueChange={setDraft}
            onKeyDown={(event) => {
              if (isSubmitEnter(event)) {
                event.preventDefault();
                void saveRename();
              } else if (event.key === "Escape") {
                event.preventDefault();
                cancelRename();
              }
            }}
          />
          <Button
            type="button"
            variant="ghost"
            size="md"
            iconOnly
            disabled={saving}
            aria-label={labels.renameSave}
            onClick={() => void saveRename()}
            icon={Check}
          />
          <Button
            type="button"
            variant="ghost"
            size="md"
            iconOnly
            disabled={saving}
            aria-label={labels.renameCancel}
            onClick={cancelRename}
            icon={X}
          />
        </div>
      ) : null,
    renderActions: (item) => (
      <>
        {renameAllowed(item) ? (
          <Button
            type="button"
            variant="ghost"
            size="md"
            iconOnly
            disabled={disabled}
            aria-label={labels.rename(item.title)}
            onClick={() => startRename(item)}
            icon={Pencil}
          />
        ) : null}
        <Button
          type="button"
          variant="ghost"
          size="md"
          tone="danger"
          iconOnly
          disabled={disabled || deletingId !== null}
          aria-label={labels.delete(item.title)}
          onClick={() => void removeConversation(item)}
          icon={Trash2}
        />
      </>
    ),
  };
}
