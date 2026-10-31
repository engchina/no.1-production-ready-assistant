import { ListChecks } from "lucide-react";
import { useId } from "react";

import { cn } from "../../lib/utils";
import { Disclosure } from "../ui/disclosure";
import { DEFAULT_CLARIFICATION_LABELS, type ClarificationLabels } from "./clarification-labels";

/** 会話に残す 1 件の確認事項と利用者の答え。 */
export interface ClarificationRecordEntry {
  id: string;
  /** 確認事項の文。 */
  prompt: string;
  /** 選んだ選択肢の名前（翻訳済み）。 */
  selected: string[];
  /** 自由入力の答え。 */
  otherText?: string;
  /** 選択肢の無い（自由入力だけで答える）確認事項。答えを「その他: 」を付けずに出す（#3366）。 */
  freeTextOnly?: boolean;
}

export interface ClarificationRecordProps {
  entries: ClarificationRecordEntry[];
  /** `answered`（既定）は答えた内容、`skipped` は「確認せずに〈回答〉しました」。 */
  outcome?: "answered" | "skipped";
  /**
   * `bubble`（既定）は会話の利用者の側（右寄せの吹き出し。利用者の発言として残す）、`disclosure` は回答の詳細の
   * 折りたたみ（回答の枠の中で「回答前に確認した内容」を開いて見る）。
   */
  variant?: "bubble" | "disclosure";
  /** `disclosure` の初期の開閉（既定は閉じる）。 */
  defaultOpen?: boolean;
  labels?: Partial<ClarificationLabels>;
  className?: string;
  testId?: string;
}

/** 1 件の答えの文（選んだ選択肢・自由入力。どちらも無ければ「（答えなし）」）。 */
export function clarificationRecordAnswerText(
  entry: ClarificationRecordEntry,
  labels: ClarificationLabels = DEFAULT_CLARIFICATION_LABELS
): string {
  const parts = [...entry.selected];
  const other = entry.otherText?.trim();
  if (other) parts.push(entry.freeTextOnly ? other : labels.recordOther(other));
  return parts.length > 0 ? parts.join(labels.recordSeparator) : labels.recordNoAnswer;
}

/**
 * 回答前に確認した内容（#1839。3 製品共通）。利用者が確認のカードで答えた内容（選んだ選択肢・自由入力）か、
 * スキップしたことを会話に残す。カードは確定・スキップの後に閉じ、この表示に置き換える（会話の履歴を開き直しても
 * 同じ表示）。入力欄の「停止」で取り消した質問には残さない（カードに「確認をやめる」は無い。#2311）。
 */
export function ClarificationRecord({
  entries,
  outcome = "answered",
  variant = "bubble",
  defaultOpen = false,
  labels: labelOverrides,
  className,
  testId = "clarification-record",
}: ClarificationRecordProps) {
  const labels = { ...DEFAULT_CLARIFICATION_LABELS, ...labelOverrides };
  const titleId = `clarification-record-${useId().replace(/:/g, "")}`;
  const skipped = outcome === "skipped";

  const body = (
    <div className="grid min-w-0 gap-1.5">
      {skipped ? (
        <p className="break-words text-sm text-fg" data-testid={`${testId}-skipped`}>
          {labels.recordSkipped}
        </p>
      ) : null}
      {entries.length > 0 ? (
        <dl className="grid min-w-0 gap-1.5">
          {entries.map((entry) => (
            <div key={entry.id} className="min-w-0" data-testid={`${testId}-entry`} data-entry-id={entry.id}>
              <dt className="break-words text-xs leading-5 text-fg-muted">{entry.prompt}</dt>
              {skipped ? null : (
                <dd className="whitespace-pre-wrap break-words text-sm leading-6 text-fg">
                  {clarificationRecordAnswerText(entry, labels)}
                </dd>
              )}
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );

  if (variant === "disclosure") {
    return (
      <Disclosure
        variant="plain"
        size="sm"
        icon={ListChecks}
        summary={labels.recordTitle}
        defaultOpen={defaultOpen}
        className={className}
        data-testid={testId}
        data-outcome={outcome}
      >
        {body}
      </Disclosure>
    );
  }

  return (
    <div className={cn("flex flex-col items-end", className)} data-testid={testId} data-outcome={outcome}>
      <div
        role="group"
        aria-labelledby={titleId}
        data-surface-tint="accent"
        className="grid max-w-[85%] min-w-0 gap-1.5 rounded-md bg-accent-subtle px-3 py-2"
      >
        <p id={titleId} className="flex items-center gap-1.5 text-xs font-medium text-fg-muted">
          <ListChecks size={14} aria-hidden className="shrink-0" />
          {labels.recordTitle}
        </p>
        {body}
      </div>
    </div>
  );
}
