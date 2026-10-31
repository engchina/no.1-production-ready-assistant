import { AlertCircle, UserRound } from "lucide-react";
import type { ReactNode } from "react";

import { resolveContentLang } from "../../lib/content-lang";
import { cn } from "../../lib/utils";
import { ChatAttachmentList, type ChatAttachmentItem } from "../chat/chat-attachments";

/** 利用者のメッセージの状態（#907）。`sending` は送信の応答を待つ仮のメッセージ。 */
export type ChatUserMessageStatus = "sending" | "sent" | "failed" | "stopped";

export interface ChatUserMessageProps {
  /** 送った本文。改行はそのまま出す。 */
  children: ReactNode;
  /** 既定 `sent`。`failed` のときは吹き出しの下に `failedLabel` を出す。 */
  status?: ChatUserMessageStatus;
  /** 送信できなかったときの状態の文（翻訳済み。例:「送信できませんでした」）。 */
  failedLabel?: string;
  /**
   * 会話の持ち主以外が送った質問の、送った人の表示（翻訳済み。例:「システム管理者 が送信」。#1928）。
   * 吹き出しの上に `UserRound` 付きの小さな文で出す。持ち主が送った質問には渡さない。
   */
  senderLabel?: string;
  /** 質問に添付したファイル（#2517）。吹き出しの下に右寄せで出す。 */
  attachments?: ChatAttachmentItem[];
  /** 添付の一覧の名前（翻訳済み。例:「添付ファイル」）。読み上げだけ。 */
  attachmentsLabel?: string;
  /**
   * 質問の言語（#3335。`resolveContentLang`）。渡さなければ、本文が文字列のときに本文から判定して吹き出しに
   * `lang` を付ける。`null` なら付けない。
   */
  lang?: string | null;
  className?: string;
  testId?: string;
}

/**
 * チャットの利用者のメッセージ（右寄せの吹き出し）。3 製品のチャットで同じ形にする（#907）。
 *
 * - 送信した瞬間に出す（楽観的な表示）。送信中（`sending`）も見た目は送信後と同じにし、
 *   応答を待つ表示は回答の場所（`ProcessingIndicator`）の 1 つだけにする（スピナーを増やさない）。
 * - 送信できなかったとき（`failed`）は、吹き出しを残したまま下に状態の文をアイコン付きで出す
 *   （色だけに頼らない）。原因と「再送信」は製品が回答の場所の Banner に出す（messaging.md §9 P1 / P2）。
 * - 状態は `data-status` にも出す（e2e で状態を確かめる）。
 * - 会話の持ち主以外（system_admin）が送った質問は、吹き出しの上に送った人を文で出す
 *   （`senderLabel`。色だけに頼らない。#1928）。
 * - 質問の本文の言語を吹き出しの `lang` に付ける（中国語の質問は中国語の字形。#3335）。
 */
export function ChatUserMessage({
  children,
  status = "sent",
  failedLabel,
  senderLabel,
  attachments,
  attachmentsLabel,
  lang,
  className,
  testId,
}: ChatUserMessageProps) {
  // 質問は利用者の内容なので、言語の字形で描く（#3335）。本文が要素のときは呼び出し側が `lang` を渡す。
  const contentLang = resolveContentLang(lang, typeof children === "string" ? children : null);
  return (
    <div
      className={cn("flex flex-col items-end gap-1", className)}
      data-status={status}
      data-testid={testId}
    >
      {senderLabel ? <ChatSenderLabel>{senderLabel}</ChatSenderLabel> : null}
      <div
        data-surface-tint="accent"
        className="max-w-[85%] whitespace-pre-wrap break-words rounded-md bg-accent-subtle px-3 py-2 text-sm text-fg"
        lang={contentLang}
      >
        {children}
      </div>
      {attachments?.length ? (
        <ChatAttachmentList
          items={attachments}
          labels={{ list: attachmentsLabel ?? "" }}
          align="end"
          className="max-w-[85%]"
        />
      ) : null}
      {status === "failed" && failedLabel ? (
        <p className="flex items-center gap-1 text-xs text-danger-fg">
          <AlertCircle size={14} aria-hidden className="shrink-0" />
          {failedLabel}
        </p>
      ) : null}
    </div>
  );
}

/**
 * 会話の持ち主以外が送った利用者の側の発言の、送った人の表示（右寄せの小さな文。#1928）。
 * `ChatUserMessage` の `senderLabel` と、`ChatTurn` の `userContent`（回答前の確認の答え）の上で使う。
 */
export function ChatSenderLabel({ children }: { children: ReactNode }) {
  return (
    <p className="flex items-center justify-end gap-1 text-xs text-fg-muted" data-chat-sender="">
      <UserRound size={14} aria-hidden className="shrink-0" />
      {children}
    </p>
  );
}
