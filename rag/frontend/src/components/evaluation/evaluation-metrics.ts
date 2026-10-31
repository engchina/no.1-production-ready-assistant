import type {
  EvaluationMetricName,
  EvaluationMetrics,
  EvaluationOutcome,
  EvaluationSuiteName,
} from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";

/** 六指標の分類・順序は評価の基準と品質評価で共有する。 */
export type EvaluationPerspective = "retrieval" | "answer" | "decision";
export const EVALUATION_PERSPECTIVES: ReadonlyArray<{id: EvaluationPerspective; metrics: readonly EvaluationMetricName[]}> = [
  {id:"retrieval", metrics:["required_evidence_recall", "mrr"]},
  {id:"answer", metrics:["answer_correctness", "requirement_coverage", "claim_support_rate"]},
  {id:"decision", metrics:["response_decision_accuracy"]},
];
export const EVALUATION_METRIC_NAMES: readonly EvaluationMetricName[] = EVALUATION_PERSPECTIVES.flatMap(item => item.metrics);
export const LOWER_IS_BETTER_METRICS: ReadonlySet<string> = new Set();
export const STANDARD_ANSWER_METRICS: ReadonlySet<EvaluationMetricName> = new Set(["answer_correctness", "requirement_coverage"]);
export const EVALUATION_SUITE_NAMES: readonly EvaluationSuiteName[] = ["standard", "strict"];
const METRIC_KEYS = Object.fromEntries(EVALUATION_METRIC_NAMES.map(name => [name, {
 label: `evaluation.metric.${name}` as I18nKey, description: `evaluation.metric.${name}.description` as I18nKey,
}])) as Record<EvaluationMetricName,{label:I18nKey;description:I18nKey}>;
const PERSPECTIVE_KEYS = Object.fromEntries(EVALUATION_PERSPECTIVES.map(({id}) => [id, {
 label: `evaluation.perspective.${id}` as I18nKey, description: `evaluation.perspective.${id}.description` as I18nKey,
}])) as Record<EvaluationPerspective,{label:I18nKey;description:I18nKey}>;

const FAILURE_REASON_KEYS: Record<string, I18nKey> = {
  retrieval_miss: "evaluation.failureReason.retrieval_miss",
  partial_recall: "evaluation.failureReason.partial_recall",
  unexpected_answer: "evaluation.failureReason.unexpected_answer",
  unexpected_refusal: "evaluation.failureReason.unexpected_refusal",
  answer_keyword_miss: "evaluation.failureReason.answer_keyword_miss",
  low_groundedness: "evaluation.failureReason.low_groundedness",
  claim_unassessed: "evaluation.failureReason.claim_unassessed",
  unsupported_claim: "evaluation.failureReason.unsupported_claim",
  missing_content: "evaluation.failureReason.missing_content",
  answer_failed: "evaluation.failureReason.answer_failed",
  answer_evaluation_error: "evaluation.failureReason.answer_evaluation_error",
  guardrail_warning: "evaluation.failureReason.guardrail_warning",
  unexpected_handling: "evaluation.failureReason.unexpected_handling",
  step_missing: "evaluation.failureReason.step_missing",
  forbidden_action: "evaluation.failureReason.forbidden_action",
  condition_missing: "evaluation.failureReason.condition_missing",
  evidence_miss: "evaluation.failureReason.evidence_miss",
  known_condition_reasked: "evaluation.failureReason.known_condition_reasked",
  screen_missing: "evaluation.failureReason.screen_missing",
  wrong_screen: "evaluation.failureReason.wrong_screen",
  off_target: "evaluation.failureReason.off_target",
  case_error: "evaluation.failureReason.case_error",
};

const SUITE_KEYS: Record<EvaluationSuiteName, { label: I18nKey; description: I18nKey }> = {
  standard: {
    label: "settings.evaluation.suite.standard",
    description: "settings.evaluation.suite.standard.description",
  },
  strict: {
    label: "settings.evaluation.suite.strict",
    description: "settings.evaluation.suite.strict.description",
  },
};

export function isEvaluationMetric(name: string): name is EvaluationMetricName {
  return Object.hasOwn(METRIC_KEYS, name);
}

export function isEvaluationSuite(name: string): name is EvaluationSuiteName {
  return Object.hasOwn(SUITE_KEYS, name);
}

export function metricLabel(name: string): string {
  return isEvaluationMetric(name) ? t(METRIC_KEYS[name].label) : name;
}
export function metricDescription(name: EvaluationMetricName): string {
  return t(METRIC_KEYS[name].description);
}

export function perspectiveLabel(perspective: EvaluationPerspective): string {
  return t(PERSPECTIVE_KEYS[perspective].label);
}

export function perspectiveDescription(perspective: EvaluationPerspective): string {
  return t(PERSPECTIVE_KEYS[perspective].description);
}

/** 失敗理由の表示名。削除した理由（保存済みの結果）は原文のコードを返す。 */
export function failureReasonLabel(reason: string): string {
  const key = FAILURE_REASON_KEYS[reason];
  return key ? t(key) : reason;
}

/** 基準の表示名。削除した基準（保存済みの結果）は原文の名前を返す。 */
export function suiteLabel(name: string): string {
  return isEvaluationSuite(name) ? t(SUITE_KEYS[name].label) : name;
}

export function suiteDescription(name: EvaluationSuiteName): string {
  return t(SUITE_KEYS[name].description);
}

/** 0..1 の値を百分率で表示する。測れなかった値（null・欄なし）は「未評価」。 */
export function formatMetricValue(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return t("evaluation.metric.unassessed");
  return `${Math.round(value * 1000) / 10}%`;
}

/** 集計結果の指標の値（保存済みの古い結果で欄が無いときは null）。 */
export function metricValue(
  metrics: EvaluationMetrics,
  name: EvaluationMetricName
): number | null {
  const value = metrics[name];
  return typeof value === "number" ? value : null;
}

/** 指標の対象のケース数。古い結果で件数が無いときは null（件数を表示しない）。 */
export function metricCaseCount(
  metrics: EvaluationMetrics,
  name: EvaluationMetricName
): number | null {
  const count = metrics.metric_case_counts?.[name];
  return typeof count === "number" ? count : null;
}

/** 閾値の dict を、指標の並び順（観点の順）で返す。六指標以外は含めない。 */
export function orderedThresholdEntries(
  thresholds: Partial<Record<string, number | null>> | null | undefined
): Array<[string, number]> {
  const entries = Object.entries(thresholds ?? {}).filter(
    (entry): entry is [string, number] => typeof entry[1] === "number"
  );
  const order = new Map(EVALUATION_METRIC_NAMES.map((name, index) => [name as string, index]));
  return entries.filter(([name]) => isEvaluationMetric(name)).sort(
    ([left], [right]) =>
      (order.get(left) ?? Number.MAX_SAFE_INTEGER) - (order.get(right) ?? Number.MAX_SAFE_INTEGER)
  );
}

/**
 * 結果に出す観点か。業務支援の対応（handling）は、対象のケースが 1 件も無い評価では出さない
 * （期待する対応・手順を持たない評価セットに「対象のケースなし」の欄を並べない。#1231）。
 */
export function isPerspectiveShown(_metrics: EvaluationMetrics, _perspective: (typeof EVALUATION_PERSPECTIVES)[number]): boolean { return true; }

const OUTCOME_KEYS: Record<EvaluationOutcome, I18nKey> = {
  answered: "evaluation.outcome.answered",
  conditional: "evaluation.outcome.conditional",
  needs_clarification: "evaluation.outcome.needs_clarification",
  needs_environment_data: "evaluation.outcome.needs_environment_data",
  needs_human: "evaluation.outcome.needs_human",
  insufficient_evidence: "evaluation.outcome.insufficient_evidence",
};

/** 回答の対応の表示名（未知の値は原文のまま）。 */
export function outcomeLabel(outcome: string): string {
  return outcome in OUTCOME_KEYS ? t(OUTCOME_KEYS[outcome as EvaluationOutcome]) : outcome;
}


export type ComparisonDirection = "better" | "worse" | "same";

/** 比べる表の 1 行（#2402）。差はプロファイル − ナレッジベース（0..1）。 */
export interface EvaluationComparisonRow {
  metric: EvaluationMetricName;
  base: number | null;
  compared: number | null;
  delta: number | null;
  direction: ComparisonDirection | null;
}

/**
 * ナレッジベースの範囲と検索・回答プロファイルの範囲の結果を、指標ごとに並べる（#2402）。
 * 両側とも未評価でも六指標を表示し、差を未評価として扱う。
 */
export function comparisonRows(
  base: EvaluationMetrics,
  compared: EvaluationMetrics
): EvaluationComparisonRow[] {
  const names = EVALUATION_METRIC_NAMES;
  return names.flatMap((metric) => {
    const left = metricValue(base, metric);
    const right = metricValue(compared, metric);
    const delta = left !== null && right !== null ? Math.round((right - left) * 10000) / 10000 : null;
    let direction: ComparisonDirection | null = null;
    if (delta !== null) {
      const improved = LOWER_IS_BETTER_METRICS.has(metric) ? delta < 0 : delta > 0;
      direction = delta === 0 ? "same" : improved ? "better" : "worse";
    }
    return [{ metric, base: left, compared: right, delta, direction }];
  });
}

/** 差を百分率のポイントで表示する（+12.5pt / −3pt / ±0pt。測れなければ「—」）。 */
export function formatMetricDelta(delta: number | null): string {
  if (delta === null || Number.isNaN(delta)) return t("evaluation.metric.unassessed");
  const points = Math.round(delta * 1000) / 10;
  if (points === 0) return "±0pt";
  return `${points > 0 ? "+" : "−"}${Math.abs(points)}pt`;
}
