import { SendHorizontal } from "lucide-react";
import type { ReactNode, Ref } from "react";

import { isSubmitEnter } from "../../lib/keyboard";
import { cn } from "../../lib/utils";
import { FieldActionRow } from "../ui/field-action-row";
import { InfoTip, type InfoTipProps } from "../ui/info-tip";
import { RunStopButton } from "../ui/run-stop-button";
import { TextareaField } from "../ui/textarea-field";
import { ChatComposerAttachmentArea, type ChatComposerAttachments } from "./chat-attachments";

export interface ChatComposerProps {
  /** 入力欄の id。 */
  id: string;
  /** 下書き（製品の作業状態に残す。workspace-state.md）。 */
  value: string;
  onValueChange: (value: string) => void;
  /** 送信（ボタン・Enter）。入力が空・送れない間は呼ばない。 */
  onSubmit: () => void;
  /** 停止（ボタンだけ。実行中の Enter では呼ばない。buttons.md §3.1）。 */
  onStop: () => void;
  /** 送信の要求中・回答の作成中か。true の間は同じボタンが「停止」になる。 */
  running: boolean;
  /** 入力が空のほかに送れない条件（対象が無い・選択待ち・承認待ちなど）。 */
  submitBlocked?: boolean;
  /**
   * 入力欄を書けない間（対象の一覧の読み込み中。messaging.md §11.7、#1153）。書いた文字は値に残す。送信もできない。
   * 会話の内容の読み込み中・回答の作成中は書けるので、ここには入れない（会話の内容の読み込み中は `submitBlocked`。
   * 書いている途中で無効にするとフォーカスが外れ、打った文字が入らない。#1188）。
   */
  disabled?: boolean;
  /** 入力欄の名前（翻訳済み。例:「質問」）。画面には出さず読み上げだけにする。 */
  label: string;
  /** 入力欄の placeholder（翻訳済み。例:「質問を入力（Enter で送信、Shift+Enter で改行）」）。 */
  placeholder?: string;
  /** 「送信」「停止」（翻訳済み）。 */
  sendLabel: string;
  stopLabel: string;
  /** 入力欄の要素（新しい会話のあとにフォーカスを移す）。 */
  textareaRef?: Ref<HTMLTextAreaElement>;
  /** 入力の上限（文字数）。 */
  maxLength?: number;
  /** 入力欄の行数（既定 2）。 */
  rows?: number;
  /** 入力欄の上の設定の行（`ChatComposerOption`。RAG「回答するモデル」、NL2SQL「生成方法」）。 */
  options?: ReactNode;
  /** 入力欄の下の通知（停止の失敗・会話の件数の上限など）。 */
  footer?: ReactNode;
  /**
   * 会話の添付（#2517）。渡すと「ファイルを添付」の行と添付の一覧を入力欄の上に出し、入力欄の領域への
   * ドロップと、入力欄への貼り付け（ファイル）で添付する。アップロード中の添付がある間は送れない。
   * 状態は `useChatAttachments` が持つ。
   */
  attachments?: ChatComposerAttachments;
  /** 送信 / 停止のボタンの testid。 */
  sendTestId?: string;
}

/**
 * チャットの入力欄の領域（3 製品共通。UX 契約 page-archetypes.md §6、#1161）。
 *
 * - 入力欄（`TextareaField`、3.5 行。#2008）と送信 / 停止（`RunStopButton`、lg）を `FieldActionRow` で並べる。
 *   送信は入力欄の下端にそろえ、sm 未満は下に全幅で置く（#613）。
 * - Enter で送信、Shift+Enter で改行。IME の変換を確定する Enter では送らない（`isSubmitEnter`。#459）。
 *   実行中の Enter では停止しない。停止はボタンだけ（buttons.md §3.1）。
 * - 入力欄は回答の作成中も書ける（次の質問を書ける。messaging.md §11.1）。
 * - 送れない間（入力が空・`submitBlocked`）のボタンは `aria-disabled`（フォーカスを受ける）。
 * - 閲覧だけの会話（他の利用者の会話。#3244）では、この部品の代わりに `ChatReadOnlyNotice` を置く。
 * - 添付（`attachments`）は「ファイルを添付」の行と一覧を入力欄の上に出す（#2517）。
 */
export function ChatComposer({
  id,
  value,
  onValueChange,
  onSubmit,
  onStop,
  running,
  submitBlocked = false,
  disabled = false,
  label,
  placeholder,
  sendLabel,
  stopLabel,
  textareaRef,
  maxLength,
  rows = 3.5,
  options,
  footer,
  sendTestId,
  attachments,
}: ChatComposerProps) {
  const attachmentsUploading = attachments?.items.some((item) => item.status === "uploading") ?? false;
  const blocked = submitBlocked || attachmentsUploading;
  const input = (
    <>
      <FieldActionRow
        actions={
          // 主な問い合わせの入力の行なので lg（README §4「操作部品の高さと幅」）。
          <RunStopButton
            running={running}
            onRun={onSubmit}
            onStop={onStop}
            runLabel={sendLabel}
            stopLabel={stopLabel}
            runIcon={SendHorizontal}
            runDisabled={value.trim().length === 0 || blocked || disabled}
            size="lg"
            testId={sendTestId}
          />
        }
      >
        <TextareaField
          ref={textareaRef}
          id={id}
          label={label}
          labelHidden
          value={value}
          onChange={(event) => onValueChange(event.target.value)}
          onKeyDown={(event) => {
            if (isSubmitEnter(event) && !event.shiftKey) {
              event.preventDefault();
              // 実行中・送れない間の Enter は何もしない（停止もしない）。
              if (!running && !blocked && !disabled && value.trim().length > 0) onSubmit();
            }
          }}
          onPaste={
            attachments && !attachments.disabled
              ? (event) => {
                  // ファイル（画面のキャプチャなど）の貼り付けは添付にする。文字の貼り付けはそのまま。
                  const files = Array.from(event.clipboardData?.files ?? []);
                  if (files.length === 0) return;
                  event.preventDefault();
                  attachments.onAddFiles(files);
                }
              : undefined
          }
          rows={rows}
          maxLength={maxLength}
          placeholder={placeholder}
          disabled={disabled}
          // ラベルは読み上げだけ（sr-only）なので、欄の上に余白を空けない。
          className="space-y-0"
        />
      </FieldActionRow>
    </>
  );
  return (
    <>
      {options}
      {attachments ? <ChatComposerAttachmentArea attachments={attachments}>{input}</ChatComposerAttachmentArea> : input}
      {footer}
    </>
  );
}

export interface ChatComposerOptionProps {
  /** 設定の名前（翻訳済み。例:「回答するモデル」「生成方法」）。 */
  label: string;
  /**
   * 名前を読み上げから外す（中の選択欄が同じ名前を `labelHidden` で持つとき。二重に読ませない）。
   * チップの並びのように中に名前が無いときは false のまま。
   */
  labelDecorative?: boolean;
  /** 名前の横の info アイコン（補足の説明は常設しない。#901）。 */
  info?: Pick<InfoTipProps, "label" | "content" | "contentId" | "contentTestId"> & { testId?: string };
  /** 選択の部品（`SelectField size="sm"`・`ToggleChip` の並びなど）。 */
  children: ReactNode;
  className?: string;
  testId?: string;
}

/** 入力欄の直上の設定の行（「ラベル・説明のアイコン・選択」。#890 / #901）。 */
export function ChatComposerOption({
  label,
  labelDecorative = false,
  info,
  children,
  className,
  testId,
}: ChatComposerOptionProps) {
  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)} data-testid={testId}>
      <span className="inline-flex items-center gap-0.5">
        <span className="text-xs font-medium text-fg-muted" aria-hidden={labelDecorative || undefined}>
          {label}
        </span>
        {info ? (
          <InfoTip
            label={info.label}
            content={info.content}
            contentId={info.contentId}
            contentTestId={info.contentTestId}
            data-testid={info.testId}
          />
        ) : null}
      </span>
      {children}
    </div>
  );
}
