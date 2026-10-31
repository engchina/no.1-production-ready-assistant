import { useId, type ReactNode } from "react";

import { cn } from "../../lib/utils";

/**
 * 回答の見出し（#3433）。回答の本文の中の節の見出し（「資料により確認できた事項」など。`AnswerSections`）と、チャットの
 * 回答の枠の見出し「回答」（`ChatAnswerBody`）を同じ書式（本文の大きさの太字）で描く。書式はここだけに置く。
 */
export interface AnswerHeadingProps {
  /** HTML の見出しの段（置く場所の見出しの下の段。既定 4）。見た目は段で変えない。 */
  level?: 2 | 3 | 4 | 5 | 6;
  id?: string;
  /** 見出しの文字の言語（回答の見出しのとき。部品・製品の文言なら渡さない）。 */
  lang?: string;
  className?: string;
  testId?: string;
  children: ReactNode;
}

export function AnswerHeading({ level = 4, id, lang, className, testId, children }: AnswerHeadingProps) {
  const Tag = `h${level}` as "h2" | "h3" | "h4" | "h5" | "h6";
  return (
    <Tag id={id} className={cn("text-sm font-semibold text-fg", className)} lang={lang} data-testid={testId}>
      {children}
    </Tag>
  );
}

export interface ChatAnswerBodyLabels {
  /** 回答の本文の見出し。 */
  heading: string;
}

export const DEFAULT_CHAT_ANSWER_BODY_LABELS: ChatAnswerBodyLabels = {
  heading: "回答",
};

export interface ChatAnswerBodyProps {
  /** 回答の本文（`AnswerSections`・`AnswerPlainText`・Markdown の本文など）。 */
  children: ReactNode;
  /**
   * 見出しの段（既定 3。チャットの会話の名前 h2 の下で、本文の中の節の見出し h4 の上）。
   */
  headingLevel?: 2 | 3 | 4;
  labels?: Partial<ChatAnswerBodyLabels>;
  className?: string;
  testId?: string;
}

/**
 * チャットの回答の本文と、その見出し「回答」（3 製品共通。#3433。UX 契約 page-archetypes.md §6.1）。
 * 回答の枠（`ChatAnswer`）の中で、処理の経過（`ChatProgress`）の後に置き、回答の本文を描くときだけ使う
 * （処理の経過だけの間・失敗・停止だけの枠には置かない）。実行の詳細の画面は `Section` の見出し「回答」を持つので
 * 使わない。見出しは本文の中の節の見出しと同じ書式（`AnswerHeading`）で、本文の領域の名前にもする。
 */
export function ChatAnswerBody({
  children,
  headingLevel = 3,
  labels: labelOverrides,
  className,
  testId,
}: ChatAnswerBodyProps) {
  const labels = { ...DEFAULT_CHAT_ANSWER_BODY_LABELS, ...labelOverrides };
  const headingId = useId();
  return (
    // 見出しと本文の間は、節の見出しと箇条書きの間と同じ（`space-y-1.5`）。
    <section aria-labelledby={headingId} className={cn("min-w-0 space-y-1.5", className)} data-testid={testId}>
      <AnswerHeading level={headingLevel} id={headingId} testId="chat-answer-heading">
        {labels.heading}
      </AnswerHeading>
      {children}
    </section>
  );
}
