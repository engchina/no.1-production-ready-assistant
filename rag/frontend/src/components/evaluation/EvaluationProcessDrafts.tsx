import {
  Banner,
  Button,
  DataTable,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  ProcessingIndicator,
  StatusBadge,
  Switch,
} from "@production-ready/ui";
import { ListChecks } from "lucide-react";
import { useState } from "react";

import { ApiError, type EvaluationProcessDraft, type EvaluationProcessRow } from "@/lib/api";
import { t } from "@/lib/i18n";
import { useDraftEvaluationProcess } from "@/lib/queries";

import { evaluationCaseRowLabel } from "./evaluation-input";
import { outcomeLabel } from "./evaluation-metrics";

/** 1 回の下書きの API で送る行の数（backend の `EVALUATION_PROCESS_MAX_REQUEST_ROWS`）。 */
export const EVALUATION_PROCESS_BATCH_ROWS = 10;
const CONFIRM_SWITCH_ID = "evaluation-process-confirm";

/**
 * 回答の過程から作る評価の期待の下書き（#2402）。Excel の「回答の過程」のある行を、業務ガイドの取り込みと
 * 同じ構造化で手順・条件・根拠・対応の下書きにし、行ごとに確かめてから「評価に使う」をオンにした
 * ときだけ評価のケースに足す（下書きは保存しない）。行は 10 行ずつ順に送り、進み具合を出す。
 */
export function EvaluationProcessDrafts({
  rows,
  knowledgeBaseIds,
  drafts,
  onDrafts,
  confirmed,
  onConfirmedChange,
  disabled,
}: {
  rows: EvaluationProcessRow[];
  knowledgeBaseIds: string[];
  drafts: Record<string, EvaluationProcessDraft>;
  onDrafts: (drafts: Record<string, EvaluationProcessDraft>) => void;
  confirmed: boolean;
  onConfirmedChange: (confirmed: boolean) => void;
  disabled: boolean;
}) {
  const mutation = useDraftEvaluationProcess();
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const shown = rows.flatMap((row) => (drafts[row.case_id] ? [drafts[row.case_id]] : []));
  const usable = shown.filter((draft) => !draft.error).length;

  const build = async () => {
    setError(null);
    onConfirmedChange(false);
    const collected: Record<string, EvaluationProcessDraft> = {};
    setProgress({ done: 0, total: rows.length });
    try {
      for (let start = 0; start < rows.length; start += EVALUATION_PROCESS_BATCH_ROWS) {
        const batch = rows.slice(start, start + EVALUATION_PROCESS_BATCH_ROWS);
        const result = await mutation.mutateAsync({
          knowledge_base_ids: knowledgeBaseIds,
          rows: batch,
        });
        for (const draft of result.drafts) collected[draft.case_id] = draft;
        setProgress({ done: Math.min(rows.length, start + batch.length), total: rows.length });
      }
      onDrafts(collected);
    } catch (caught) {
      // 途中まで作った下書きは残す（作り直せる）。
      onDrafts(collected);
      setError(caught instanceof ApiError ? caught.message : t("evaluation.process.error"));
    } finally {
      setProgress(null);
    }
  };

  const running = progress !== null;
  return (
    <section
      className="grid min-w-0 gap-3 rounded-md border border-border p-4"
      aria-labelledby="evaluation-process-title"
      data-testid="evaluation-process"
    >
      <div className="grid gap-1">
        <h3 id="evaluation-process-title" className="text-sm font-semibold text-fg">
          {t("evaluation.process.title")}
        </h3>
        <p className="text-xs text-fg-muted">
          {t("evaluation.process.description", { count: rows.length })}
        </p>
      </div>
      <div>
        <Button
          type="button"
          variant="secondary"
          icon={ListChecks}
          loading={running}
          disabled={disabled || running || knowledgeBaseIds.length === 0}
          onClick={() => void build()}
        >
          {shown.length ? t("evaluation.process.rebuild") : t("evaluation.process.build")}
        </Button>
      </div>
      {progress ? (
        <ProcessingIndicator
          active
          label={t("evaluation.process.building", {
            done: progress.done,
            total: progress.total,
          })}
          operationKey="evaluation-process"
          placement="action"
          activityIcon="none"
          testId="evaluation-process-processing"
        />
      ) : null}
      {error ? <Banner severity="danger">{error}</Banner> : null}
      {shown.length ? (
        <>
          <ProcessDraftTable drafts={shown} />
          <div className="flex items-start gap-3">
            <Switch
              id={CONFIRM_SWITCH_ID}
              checked={confirmed}
              disabled={disabled || running || usable === 0}
              aria-describedby={`${CONFIRM_SWITCH_ID}-help`}
              onCheckedChange={onConfirmedChange}
            />
            <div className="min-w-0">
              <label
                htmlFor={CONFIRM_SWITCH_ID}
                className="cursor-pointer text-sm font-medium text-fg"
              >
                {t("evaluation.process.confirm", { count: usable })}
              </label>
              <p id={`${CONFIRM_SWITCH_ID}-help`} className="text-xs text-fg-muted">
                {t("evaluation.process.confirmHelp")}
              </p>
            </div>
          </div>
        </>
      ) : null}
    </section>
  );
}

function ProcessDraftTable({ drafts }: { drafts: EvaluationProcessDraft[] }) {
  return (
    <DataTable<EvaluationProcessDraft>
      ariaLabel={t("evaluation.process.tableLabel")}
      rows={drafts}
      getRowKey={(draft) => draft.case_id}
      rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
      stickyHeader
      visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
      scrollAriaLabel={t("evaluation.process.tableLabel")}
      scrollTestId="evaluation-process-table"
      tableClassName="w-full min-w-[22.86rem] text-sm md:min-w-[44rem]"
      columns={[
        {
          key: "row",
          header: t("evaluation.import.column.row"),
          rowHeader: true,
          className: "tnum whitespace-nowrap font-medium text-fg",
          render: (draft) => evaluationCaseRowLabel(draft.case_id),
        },
        {
          key: "steps",
          header: t("evaluation.process.column.steps"),
          className: "min-w-[8rem] break-words text-xs text-fg",
          render: (draft) =>
            draft.error ? (
              <span className="grid gap-1" data-testid="evaluation-process-error">
                <StatusBadge variant="danger" label={t("evaluation.process.failed")} />
                <span className="text-fg-muted">{draft.error}</span>
              </span>
            ) : (
              <Lines items={draft.required_answer_points} ordered />
            ),
        },
        {
          key: "conditions",
          header: t("evaluation.process.column.conditions"),
          className: "break-words text-xs text-fg",
          render: (draft) => <Lines items={draft.required_conditions} />,
        },
        {
          key: "evidence",
          header: t("evaluation.process.column.evidence"),
          className: "break-words text-xs text-fg",
          render: (draft) => (
            <Lines
              items={draft.references.map((reference) =>
                reference.status === "resolved"
                  ? [reference.file_name ?? reference.name, reference.pages && `p.${reference.pages}`]
                      .filter(Boolean)
                      .join(" | ")
                  : t("evaluation.process.unresolved", { name: reference.name })
              )}
            />
          ),
        },
        {
          key: "outcomes",
          header: t("evaluation.process.column.outcomes"),
          headerClassName: "hidden md:table-cell",
          className: "hidden break-words text-xs text-fg md:table-cell",
          render: (draft) => <Lines items={draft.expected_outcomes.map(outcomeLabel)} />,
        },
        {
          key: "warnings",
          header: t("evaluation.process.column.warnings"),
          headerClassName: "hidden lg:table-cell",
          className: "hidden break-words text-xs text-fg-muted lg:table-cell",
          render: (draft) => <Lines items={draft.warnings} />,
        },
      ]}
    />
  );
}

function Lines({ items, ordered = false }: { items: readonly string[]; ordered?: boolean }) {
  if (!items.length) return <span className="text-fg-muted">—</span>;
  const List = ordered ? "ol" : "ul";
  return (
    <List className={ordered ? "grid list-decimal gap-0.5 pl-4" : "grid gap-0.5"}>
      {items.map((item, index) => (
        <li key={`${index}:${item}`}>{item}</li>
      ))}
    </List>
  );
}
