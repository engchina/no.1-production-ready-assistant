import { DataTable, StatusBadge } from "@production-ready/ui";

import type { CalibrationErrorType, CalibrationProgress } from "@/lib/api";
import { t } from "@/lib/i18n";

import { aspectLabel, aspectTargetReached } from "./calibration-format";

/**
 * 観点ごとの記入の進み具合（holdout の pass / fail の数と目標）と、誤りの型・対照の holdout の件数。
 * 目標は suite の judge-calibration.md と同じ（holdout の行 50・pass 20・fail 20、誤りの型ごとの件数）。
 */
export function ProgressTable({
  progress,
  errorTypes,
  testId,
  title,
}: {
  progress: CalibrationProgress;
  errorTypes: readonly CalibrationErrorType[];
  testId: string;
  title: string;
}) {
  const counts = new Map(
    progress.error_types.map((item) => [item.id, item.holdout_count]),
  );
  return (
    <div className="grid min-w-0 gap-3" data-testid={testId}>
      <DataTable
        ariaLabel={title}
        rows={progress.aspects}
        getRowKey={(row) => row.aspect}
        dense
        columns={[
          {
            key: "aspect",
            header: t("evaluation.calibration.progress.aspect"),
            rowHeader: true,
            className: "whitespace-nowrap",
            render: (row) => aspectLabel(row.aspect),
          },
          {
            key: "labeled",
            header: t("evaluation.calibration.progress.labeled"),
            className: "tnum whitespace-nowrap",
            render: (row) => `${row.labeled} / ${row.rows}`,
          },
          {
            key: "holdout",
            header: t("evaluation.calibration.progress.holdout", {
              target: progress.holdout_target,
            }),
            className: "tnum whitespace-nowrap",
            render: (row) =>
              `${row.holdout_pass + row.holdout_fail} / ${progress.holdout_target}`,
          },
          {
            key: "pass",
            header: t("evaluation.calibration.progress.pass", {
              target: progress.label_target,
            }),
            className: "tnum whitespace-nowrap",
            render: (row) => `${row.holdout_pass} / ${progress.label_target}`,
          },
          {
            key: "fail",
            header: t("evaluation.calibration.progress.fail", {
              target: progress.label_target,
            }),
            className: "tnum whitespace-nowrap",
            render: (row) => `${row.holdout_fail} / ${progress.label_target}`,
          },
          {
            key: "status",
            header: t("evaluation.calibration.progress.status"),
            className: "whitespace-nowrap",
            render: (row) =>
              aspectTargetReached(
                row,
                progress.holdout_target,
                progress.label_target,
              ) ? (
                <StatusBadge
                  variant="success"
                  label={t("evaluation.calibration.progress.reached")}
                />
              ) : (
                <StatusBadge
                  variant="neutral"
                  label={t("evaluation.calibration.progress.notReached")}
                />
              ),
          },
        ]}
      />
      <div className="grid min-w-0 gap-1.5">
        <p className="text-xs font-medium text-fg-muted">
          {t("evaluation.calibration.progress.errorTypes")}
        </p>
        <ul
          className="flex min-w-0 flex-wrap gap-1.5"
          data-testid={`${testId}-error-types`}
        >
          {errorTypes.map((item) => {
            const count = counts.get(item.id) ?? 0;
            const reached = count >= item.holdout_target;
            return (
              <li
                key={item.id}
                title={item.title}
                className="inline-flex min-h-6 items-center gap-1 rounded-md bg-surface px-2 text-xs text-fg ring-1 ring-border"
              >
                <span>
                  {t(
                    `evaluation.calibration.errorType.${item.id}` as Parameters<
                      typeof t
                    >[0],
                  )}
                </span>
                <span
                  className={
                    reached
                      ? "tnum font-semibold text-success-fg"
                      : "tnum text-fg-muted"
                  }
                >
                  {count}/{item.holdout_target}
                </span>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
