/**
 * 判定の校正（#3578）の表示のための判定と文言。画面の部品から切り離して単体テストする。
 */

import type { StatusVariant } from "@production-ready/ui";

import type {
  CalibrationAspect,
  CalibrationAspectProgress,
  CalibrationLabel,
  CalibrationRowStatus,
  CalibrationSplit,
  CalibrationSummary,
  JudgeCalibrationAspectStatus,
} from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";

export const CALIBRATION_ASPECTS: readonly CalibrationAspect[] = [
  "requirement_coverage",
  "answer_correctness",
  "claim_support",
];

export const CALIBRATION_LABELS: readonly CalibrationLabel[] = [
  "pass",
  "fail",
  "cannot_judge",
];

/** 観点 → 品質評価の指標の名前（結果のカードに「校正済み」を出す指標）。 */
export const CALIBRATION_ASPECT_METRIC: Record<CalibrationAspect, string> = {
  requirement_coverage: "requirement_coverage",
  answer_correctness: "answer_correctness",
  claim_support: "claim_support_rate",
};

export function aspectLabel(aspect: string): string {
  return (CALIBRATION_ASPECTS as readonly string[]).includes(aspect)
    ? t(`evaluation.calibration.aspect.${aspect}` as I18nKey)
    : aspect;
}

export function labelText(label: CalibrationLabel | null | undefined): string {
  return label
    ? t(`evaluation.calibration.label.${label}` as I18nKey)
    : t("evaluation.calibration.label.none");
}

export const LABEL_VARIANT: Record<CalibrationLabel, StatusVariant> = {
  pass: "success",
  fail: "danger",
  cannot_judge: "neutral",
};

export function splitLabel(split: CalibrationSplit | string): string {
  return t(`evaluation.calibration.split.${split}` as I18nKey);
}

export const ROW_STATUS_VARIANT: Record<CalibrationRowStatus, StatusVariant> = {
  pending: "neutral",
  agreed: "success",
  disagreed: "warning",
  adjudicated: "info",
};

export function rowStatusLabel(status: CalibrationRowStatus): string {
  return t(`evaluation.calibration.rowStatus.${status}` as I18nKey);
}

/** κ の表示（小数 2 桁。測れなければ「—」）。 */
export function formatKappa(value: number | null | undefined): string {
  return value === null || value === undefined || Number.isNaN(value)
    ? "—"
    : value.toFixed(2);
}

/** 率の表示（%。測れなければ「—」）。 */
export function formatRate(value: number | null | undefined): string {
  return value === null || value === undefined || Number.isNaN(value)
    ? "—"
    : `${Math.round(value * 1000) / 10}%`;
}

/** 観点の holdout の目標（行・pass・fail）に届いたか。 */
export function aspectTargetReached(
  progress: CalibrationAspectProgress,
  holdoutTarget: number,
  labelTarget: number,
): boolean {
  return (
    progress.holdout_pass + progress.holdout_fail >= holdoutTarget &&
    progress.holdout_pass >= labelTarget &&
    progress.holdout_fail >= labelTarget
  );
}

/** 品質評価の結果の指標に出す校正の状態（指標が校正の対象でなければ null）。 */
export function metricCalibration(
  aspects: readonly JudgeCalibrationAspectStatus[] | undefined,
  metric: string,
): JudgeCalibrationAspectStatus | null {
  return aspects?.find((item) => item.metric === metric) ?? null;
}

export function calibrationBadgeLabel(
  status: JudgeCalibrationAspectStatus,
): string {
  return status.calibrated
    ? t("evaluation.calibration.badge.calibrated", {
        kappa: formatKappa(status.kappa),
      })
    : t("evaluation.calibration.badge.uncalibrated");
}

/** 校正の結果の観点 × 区分の行（holdout・dev・synthetic の順）。 */
export interface CalibrationResultRow {
  key: string;
  aspect: CalibrationAspect;
  split: CalibrationSplit;
  passed: boolean;
  total: number;
  judged: number;
  coverage: number | null;
  kappa: number | null;
  accuracy: number | null;
  falsePass: number | null;
  falseFail: number | null;
  unstableRate: number | null;
  first: boolean;
}

export function calibrationResultRows(
  summary: CalibrationSummary,
): CalibrationResultRow[] {
  const rows: CalibrationResultRow[] = [];
  for (const aspect of summary.aspects) {
    aspect.splits.forEach((split, index) => {
      rows.push({
        key: `${aspect.aspect}:${split.split}`,
        aspect: aspect.aspect,
        split: split.split,
        passed: aspect.passed,
        total: split.total,
        judged: split.judged,
        coverage: split.coverage,
        kappa: split.kappa,
        accuracy: split.accuracy,
        falsePass: split.false_pass_rate,
        falseFail: split.false_fail_rate,
        unstableRate:
          split.split === "holdout"
            ? (aspect.variance?.unstable_rate ?? null)
            : null,
        first: index === 0,
      });
    });
  }
  return rows;
}

/** 指示の sha256 の表示（先頭 12 文字）。 */
export function shortSha(value: string | null | undefined): string {
  return value ? value.slice(0, 12) : "—";
}

/** 項目の ID の表示（長ければ先頭を省く）。 */
export function shortItemId(itemId: string): string {
  return itemId.length > 28 ? `…${itemId.slice(-27)}` : itemId;
}
