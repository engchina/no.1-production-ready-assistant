import { StatusBadge } from "@production-ready/ui";

import { useJudgeCalibrationStatus } from "@/lib/calibration-queries";
import { formatDateTime } from "@/lib/format";
import { t } from "@/lib/i18n";

import { calibrationBadgeLabel, metricCalibration } from "./calibration-format";

/**
 * 品質評価の結果の指標に、今の評価器（判定のモデル・指示）でその観点の判定が校正済みか（#3578）。
 * 校正の対象でない指標・状態を取れないときは何も出さない（指標の値と計算は変えない）。
 */
export function JudgeCalibrationBadge({ metric }: { metric: string }) {
  const query = useJudgeCalibrationStatus();
  const status = metricCalibration(query.data?.aspects, metric);
  if (!status) return null;
  const title = status.calibrated
    ? t("evaluation.calibration.badge.calibratedTitle", {
        time: status.calibrated_at ? formatDateTime(status.calibrated_at) : "—",
        model: query.data?.judge_model_id ?? "—",
      })
    : t("evaluation.calibration.badge.uncalibratedTitle");
  return (
    <span title={title} data-testid={`evaluation-metric-calibration-${metric}`}>
      <StatusBadge
        variant={status.calibrated ? "success" : "neutral"}
        label={calibrationBadgeLabel(status)}
      />
    </span>
  );
}
