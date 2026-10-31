import { Button, DisclosureChevron } from "@production-ready/ui";
import { useMemo, useState } from "react";

import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export type QuestionTextVariant = "list" | "detail" | "compact" | "select";

interface QuestionTextProps {
  /** API 応答由来の値は欠落しうるため tolerant に受ける(未定義でも画面を落とさない)。 */
  value: string | null | undefined;
  variant?: QuestionTextVariant;
  maxLines?: number;
  className?: string;
  testId?: string;
  expandable?: boolean;
}

const DEFAULT_LINES: Record<QuestionTextVariant, number> = {
  list: 2,
  detail: 3,
  compact: 1,
  select: 1,
};

const VARIANT_CLASS: Record<QuestionTextVariant, string> = {
  list: "text-sm font-semibold leading-5 text-fg",
  detail: "text-sm leading-6 text-fg",
  compact: "text-xs leading-5 text-fg-muted",
  select: "text-sm leading-5 text-fg",
};

const CLAMP_CLASS: Record<number, string> = {
  1: "line-clamp-1",
  2: "line-clamp-2",
  3: "line-clamp-3",
  4: "line-clamp-4",
  5: "line-clamp-5",
  6: "line-clamp-6",
};

function compactQuestion(value: string) {
  return value.replace(/\s+/gu, " ").trim();
}

function lineClampClass(maxLines: number) {
  return CLAMP_CLASS[Math.max(1, Math.min(6, Math.round(maxLines)))] ?? CLAMP_CLASS[2];
}

function shouldOfferExpansion(value: string, maxLines: number) {
  return value.length > maxLines * 42 || value.includes("\n");
}

export function QuestionText({
  value,
  variant = "list",
  maxLines,
  className,
  testId,
  expandable = false,
}: QuestionTextProps) {
  const [expanded, setExpanded] = useState(false);
  const fullValue = (value ?? "").trim() || "-";
  const displayValue = useMemo(
    () => (variant === "detail" ? fullValue : compactQuestion(fullValue) || "-"),
    [fullValue, variant]
  );
  const lines = maxLines ?? DEFAULT_LINES[variant];
  const canExpand = expandable && shouldOfferExpansion(fullValue, lines);
  const clamped = !expanded && lines > 0;

  return (
    <span className="grid w-full min-w-0 max-w-full gap-1 overflow-hidden">
      <span
        className={cn(
          "block w-full min-w-0 max-w-full whitespace-pre-wrap break-words [overflow-wrap:anywhere]",
          VARIANT_CLASS[variant],
          clamped && lineClampClass(lines),
          className
        )}
        data-testid={testId}
        aria-label={fullValue}
        title={fullValue}
      >
        {displayValue}
      </span>
      {canExpand ? (
        <Button
          variant="ghost"
          size="sm"
          type="button"
          className="w-fit"
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          <span>{expanded ? t("nl2sql.questionText.collapse") : t("nl2sql.questionText.expand")}</span>
          <DisclosureChevron expanded={expanded} size={14} />
        </Button>
      ) : null}
    </span>
  );
}
