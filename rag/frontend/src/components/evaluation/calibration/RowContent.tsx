import {
  AnswerPlainText,
  Disclosure,
  ExcerptContent,
  INFORMATION_LIST_SCROLL_CLASS,
  INFORMATION_TABLE_FOCUS_CLASS,
  StatusBadge,
} from "@production-ready/ui";
import { BookOpenCheck, FileText } from "lucide-react";
import type { ReactNode } from "react";

import type {
  CalibrationAspectCriterion,
  CalibrationEvidence,
} from "@/lib/api";
import { t } from "@/lib/i18n";

import { aspectLabel, shortItemId, splitLabel } from "./calibration-format";

/**
 * 記入・裁定の 1 行（項目 × 観点）の内容: 観点の基準・質問・回答・標準回答・根拠（評価器に渡したもの）。
 * 評価器の判定は出さない（記入の間は見せない）。
 */
export function RowContent({
  itemId,
  aspect,
  split,
  question,
  answer,
  standardAnswer,
  evidence,
  criterion,
}: {
  itemId: string;
  aspect: string;
  split: string;
  question: string;
  answer: string;
  standardAnswer: string | null;
  evidence: readonly CalibrationEvidence[];
  criterion: CalibrationAspectCriterion | undefined;
}) {
  return (
    <div className="grid min-w-0 gap-4" data-testid="calibration-row">
      <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs text-fg-muted">
        <StatusBadge variant="info" icon={false} label={aspectLabel(aspect)} />
        <StatusBadge
          variant={split === "holdout" ? "neutral" : "warning"}
          icon={false}
          label={splitLabel(split)}
        />
        <span className="min-w-0 truncate font-mono" title={itemId}>
          {shortItemId(itemId)}
        </span>
      </div>
      {criterion ? (
        <div
          data-surface-tint="accent"
          className="rounded-md border border-accent-border bg-accent-subtle p-3 text-sm"
          data-testid="calibration-criterion"
        >
          <p className="flex items-center gap-1.5 text-xs font-semibold text-accent-fg">
            <BookOpenCheck size={14} aria-hidden />
            {t("evaluation.calibration.row.criterion")}
          </p>
          <p className="mt-1 text-fg">{criterion.criteria}</p>
        </div>
      ) : null}
      <Field label={t("evaluation.calibration.row.question")}>
        <AnswerPlainText text={question} />
      </Field>
      <Field label={t("evaluation.calibration.row.answer")}>
        <AnswerPlainText text={answer} />
      </Field>
      <Field label={t("evaluation.calibration.row.standardAnswer")}>
        {standardAnswer ? (
          <AnswerPlainText text={standardAnswer} />
        ) : (
          <p className="text-sm text-fg-muted">
            {t("evaluation.calibration.row.noStandardAnswer")}
          </p>
        )}
      </Field>
      <Disclosure
        variant="card"
        icon={FileText}
        summary={t("evaluation.calibration.row.evidence", {
          count: evidence.length,
        })}
        defaultOpen={evidence.length <= 2}
      >
        {evidence.length ? (
          <div
            role="region"
            aria-label={t("evaluation.calibration.row.evidence", { count: evidence.length })}
            tabIndex={0}
            className={`min-w-0 ${INFORMATION_LIST_SCROLL_CLASS} ${INFORMATION_TABLE_FOCUS_CLASS}`}
            data-testid="calibration-evidence-scroll"
          >
            <ol className="grid min-w-0 gap-3">
              {evidence.map((item) => (
                <li
                  key={item.id}
                  className="min-w-0 rounded-md border border-border p-3"
                >
                  <p
                    className="mb-1 truncate font-mono text-xs text-fg-muted"
                    title={item.id}
                  >
                    {item.id}
                  </p>
                  <ExcerptContent text={item.text} className="text-sm text-fg" />
                </li>
              ))}
            </ol>
          </div>
        ) : (
          <p className="text-sm text-fg-muted">
            {t("evaluation.calibration.row.noEvidence")}
          </p>
        )}
      </Disclosure>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid min-w-0 gap-1">
      <p className="text-xs font-semibold text-fg-muted">{label}</p>
      <div className="min-w-0 break-words text-sm text-fg">{children}</div>
    </div>
  );
}
