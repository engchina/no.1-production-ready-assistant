import { describe, expect, it } from "vitest";

import type { CalibrationSummary, JudgeCalibrationAspectStatus } from "@/lib/api";

import {
  aspectLabel,
  aspectTargetReached,
  calibrationBadgeLabel,
  calibrationResultRows,
  formatKappa,
  formatRate,
  labelText,
  metricCalibration,
  shortItemId,
} from "./calibration-format";

const STATUS: JudgeCalibrationAspectStatus[] = [
  {
    aspect: "claim_support",
    metric: "claim_support_rate",
    calibrated: true,
    kappa: 0.724,
    calibration_id: "c1",
    calibrated_at: "2026-10-01T00:00:00Z",
    judge_rubric_version: "11",
  },
  {
    aspect: "answer_correctness",
    metric: "answer_correctness",
    calibrated: false,
    kappa: null,
    calibration_id: null,
    calibrated_at: null,
    judge_rubric_version: null,
  },
];

describe("calibration-format", () => {
  it("観点・ラベルを画面の言葉にする", () => {
    expect(aspectLabel("requirement_coverage")).toBe("網羅");
    expect(aspectLabel("claim_support")).toBe("根拠");
    expect(aspectLabel("unknown")).toBe("unknown");
    expect(labelText("cannot_judge")).toBe("判定できない");
    expect(labelText(null)).toBe("未記入");
  });

  it("κ と率を測れないときは「—」にする", () => {
    expect(formatKappa(0.7249)).toBe("0.72");
    expect(formatKappa(null)).toBe("—");
    expect(formatRate(0.0833)).toBe("8.3%");
    expect(formatRate(undefined)).toBe("—");
  });

  it("holdout の行・pass・fail の目標をすべて満たしたときだけ到達", () => {
    const base = {
      aspect: "claim_support" as const,
      rows: 80,
      labeled: 60,
      cannot_judge: 0,
      holdout_rows: 56,
    };
    expect(aspectTargetReached({ ...base, holdout_pass: 30, holdout_fail: 20 }, 50, 20)).toBe(true);
    expect(aspectTargetReached({ ...base, holdout_pass: 35, holdout_fail: 15 }, 50, 20)).toBe(false);
    expect(aspectTargetReached({ ...base, holdout_pass: 20, holdout_fail: 20 }, 50, 20)).toBe(false);
  });

  it("品質評価の指標に校正済みか未校正かを出す（対象でない指標は出さない）", () => {
    const calibrated = metricCalibration(STATUS, "claim_support_rate");
    expect(calibrated && calibrationBadgeLabel(calibrated)).toBe("校正済み κ=0.72");
    const uncalibrated = metricCalibration(STATUS, "answer_correctness");
    expect(uncalibrated && calibrationBadgeLabel(uncalibrated)).toBe("未校正");
    expect(metricCalibration(STATUS, "mrr")).toBeNull();
    expect(metricCalibration(undefined, "mrr")).toBeNull();
  });

  it("校正の結果を観点 × 区分の行にし、揺れは holdout の行だけに出す", () => {
    const summary = {
      aspects: [
        {
          aspect: "claim_support",
          passed: false,
          failures: ["holdout の件数 12 < 50"],
          splits: [
            {
              split: "holdout",
              total: 12,
              judged: 12,
              coverage: 1,
              accuracy: 0.9,
              kappa: 0.8,
              false_pass_rate: 0,
              false_fail_rate: 0.1,
            },
            {
              split: "dev",
              total: 5,
              judged: 5,
              coverage: 1,
              accuracy: 1,
              kappa: null,
              false_pass_rate: null,
              false_fail_rate: 0,
            },
          ],
          variance: { runs: 2, items: 17, unstable: 1, unstable_rate: 0.0588 },
        },
      ],
    } as unknown as CalibrationSummary;
    const rows = calibrationResultRows(summary);
    expect(rows.map((row) => [row.split, row.first, row.unstableRate])).toEqual([
      ["holdout", true, 0.0588],
      ["dev", false, null],
    ]);
  });

  it("長い項目の ID は末尾を残して省く", () => {
    expect(shortItemId("job-a:c01")).toBe("job-a:c01");
    expect(shortItemId("job-0123456789abcdef-cmp:case-0123456789")).toMatch(/^…/);
  });
});
