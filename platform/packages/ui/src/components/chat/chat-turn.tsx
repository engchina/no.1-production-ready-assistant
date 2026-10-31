import type { ReactNode } from "react";

import type { OptimisticChatMessage } from "../../lib/chat-optimistic";
import { cn } from "../../lib/utils";
import { ChatSenderLabel, ChatUserMessage, type ChatUserMessageStatus } from "../ui/chat-message";
import type { ChatAttachmentItem } from "./chat-attachments";

export interface ChatTurnProps {
  /** 送った質問（改行はそのまま出す）。 */
  question?: ReactNode;
  /** 質問の言語（`ChatUserMessage` の `lang`。渡さなければ文字列の質問から判定する。#3335）。 */
  questionLang?: string | null;
  /**
   * 質問の吹き出しの代わりに出す利用者の側の表示（回答前の確認の答えの `ClarificationRecord`。#1839）。
   * 渡すと `question` の吹き出しは出さない。
   */
  userContent?: ReactNode;
  /** 質問の状態（送信中・失敗・停止。保存済みは `sent`）。 */
  questionStatus?: ChatUserMessageStatus;
  /** 送信できなかったときの質問の下の文（翻訳済み）。 */
  failedLabel?: string;
  /**
   * 会話の持ち主以外が送った質問の、送った人の表示（翻訳済み。`ChatUserMessage` の `senderLabel`。#1928）。
   * `userContent` を渡したときは、その上に出す。
   */
  questionSender?: string;
  /** 質問に添付したファイル（`ChatUserMessage` の `attachments`。#2517）。 */
  questionAttachments?: ChatAttachmentItem[];
  /** 添付の一覧の名前（翻訳済み。読み上げだけ）。 */
  attachmentsLabel?: string;
  /** 質問の下（回答の入れ物 `ChatAnswer`・失敗の Banner・回答の後の操作）。 */
  children?: ReactNode;
  className?: string;
  testId?: string;
}

/**
 * チャットの 1 往復（3 製品共通。UX 契約 page-archetypes.md §6、#1161）。
 * 質問の吹き出し（`ChatUserMessage`）と、その下の回答の場所を `<article>` にまとめる。
 */
export function ChatTurn({
  question,
  questionLang,
  userContent,
  questionStatus = "sent",
  failedLabel,
  questionSender,
  questionAttachments,
  attachmentsLabel,
  children,
  className,
  testId,
}: ChatTurnProps) {
  return (
    <article className={cn("space-y-2", className)} data-testid={testId}>
      {userContent ? (
        <>
          {questionSender ? <ChatSenderLabel>{questionSender}</ChatSenderLabel> : null}
          {userContent}
        </>
      ) : (
        <ChatUserMessage
          status={questionStatus}
          failedLabel={failedLabel}
          senderLabel={questionSender}
          attachments={questionAttachments}
          attachmentsLabel={attachmentsLabel}
          lang={questionLang}
        >
          {question}
        </ChatUserMessage>
      )}
      {children}
    </article>
  );
}

export interface ChatAnswerProps {
  /** 回答の本文（処理の段階 `ChatProgress` → 本文 → 補足・操作の順に並べる）。 */
  children: ReactNode;
  /** 回答の受信・状態の変化を polite で知らせる（回答が後から届く製品。RAG は本文側で知らせる）。 */
  live?: boolean;
  /** 回答への深いリンク（`#message-{id}`）の先。 */
  id?: string;
  className?: string;
  testId?: string;
}

/**
 * チャットの回答の入れ物（3 製品共通。#1161）。枠線の箱（`rounded-md`・`border`・`bg-surface`・`p-3`）で、
 * 中は縦に 0.75rem 間隔で並べる。複数モデルの比較（RAG）では、この箱を列にして並べる。
 */
export function ChatAnswer({ children, live = false, id, className, testId }: ChatAnswerProps) {
  return (
    <div
      id={id}
      className={cn("flex min-w-0 flex-col gap-3 rounded-md border border-border bg-surface p-3", className)}
      aria-live={live ? "polite" : undefined}
      data-testid={testId}
    >
      {children}
    </div>
  );
}

export interface ChatPendingTurnProps {
  /** 送った質問（仮のメッセージ。`createOptimisticChatMessage`）。 */
  message: OptimisticChatMessage;
  /** 送信できなかったときの質問の下の文（翻訳済み。例:「送信できませんでした」）。 */
  failedLabel: string;
  /** 送信の応答を待つ間の回答の場所（`ChatProgress` の「質問を送信しています」）。 */
  progress: ReactNode;
  /** 送信できなかったときの原因と「再送信」（danger の `Banner` / `ApiErrorBanner`）。 */
  failure?: ReactNode;
  /** 質問に添付したファイル（#2517。`ChatTurn` の `questionAttachments`）。 */
  attachments?: ChatAttachmentItem[];
  /** 添付の一覧の名前（翻訳済み。読み上げだけ）。 */
  attachmentsLabel?: string;
  testId?: string;
}

/**
 * 送った質問（サーバーの応答の前・送れなかったとき。messaging.md §11）。送信中は回答の入れ物に処理の段階を出し、
 * 送れなかったら質問を残したまま原因と「再送信」を出す（入力欄には戻さない）。確定したら製品が同じ位置の
 * `ChatTurn` に置き換える。
 */
export function ChatPendingTurn({
  message,
  failedLabel,
  progress,
  failure,
  attachments,
  attachmentsLabel,
  testId,
}: ChatPendingTurnProps) {
  return (
    <ChatTurn
      question={message.content}
      questionStatus={message.status}
      failedLabel={failedLabel}
      questionAttachments={attachments}
      attachmentsLabel={attachmentsLabel}
      testId={testId}
    >
      {message.status === "failed" ? failure : message.status === "sending" ? <ChatAnswer>{progress}</ChatAnswer> : null}
    </ChatTurn>
  );
}
