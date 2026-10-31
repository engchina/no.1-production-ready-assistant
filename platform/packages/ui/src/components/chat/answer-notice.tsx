import { Info } from "lucide-react";
import { Fragment, type ReactNode } from "react";

import { cn } from "../../lib/utils";

/** 本文の中で自動でリンクにする URL（http / https だけ。末尾の句読点・閉じ括弧は含めない）。 */
const URL_PATTERN = /https?:\/\/[^\s<>"'「」『』（）()、。，．]+[^\s<>"'「」『』（）()、。，．.,;:!?]/g;

export interface AnswerNoticeSegment {
  kind: "text" | "link";
  value: string;
}

/**
 * 注意書きの 1 行を、文字と URL に分ける（URL だけをリンクにする。HTML・Markdown は解釈しない）。
 */
export function answerNoticeSegments(line: string): AnswerNoticeSegment[] {
  const segments: AnswerNoticeSegment[] = [];
  let last = 0;
  for (const match of line.matchAll(URL_PATTERN)) {
    const start = match.index ?? 0;
    if (start > last) segments.push({ kind: "text", value: line.slice(last, start) });
    segments.push({ kind: "link", value: match[0] });
    last = start + match[0].length;
  }
  if (last < line.length) segments.push({ kind: "text", value: line.slice(last) });
  return segments;
}

export interface AnswerNoticeProps {
  /** 注意書きの文（プレーンテキスト。改行はそのまま、URL はリンクにする）。空・null なら何も出さない。 */
  notice: string | null | undefined;
  /** 支援技術に読ませる名前（翻訳済み。例:「回答の注意書き」）。 */
  label?: string;
  className?: string;
  testId?: string;
}

/**
 * 回答の注意書き（3 製品共通。#2405）。回答の本文と根拠の後に、本文と区別できる控えめの見た目で出す。
 * 文は製品ごとの運用設定の「回答の注意書き」で決まり、backend が回答の確定の後に付ける（モデルは書かない）。
 */
export function AnswerNotice({ notice, label = "回答の注意書き", className, testId = "answer-notice" }: AnswerNoticeProps) {
  const text = notice?.trim();
  if (!text) return null;
  const lines = text.split("\n");
  return (
    <aside
      aria-label={label}
      className={cn(
        "flex min-w-0 items-start gap-2 border-t border-border pt-2 text-xs leading-relaxed text-fg-muted",
        className,
      )}
      data-testid={testId}
    >
      <Info size={14} className="mt-0.5 shrink-0" aria-hidden />
      <p className="min-w-0 break-words">
        {lines.map((line, index) => (
          <Fragment key={index}>
            {index > 0 ? <br /> : null}
            {renderLine(line)}
          </Fragment>
        ))}
      </p>
    </aside>
  );
}

function renderLine(line: string): ReactNode {
  return answerNoticeSegments(line).map((segment, index) =>
    segment.kind === "link" ? (
      <a
        key={index}
        href={segment.value}
        target="_blank"
        rel="noopener noreferrer"
        className="break-all text-accent-fg underline underline-offset-2"
      >
        {segment.value}
      </a>
    ) : (
      <Fragment key={index}>{segment.value}</Fragment>
    ),
  );
}
