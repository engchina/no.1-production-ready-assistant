import {describe, expect, it} from "vitest";
import type {EvaluationMetrics} from "@/lib/api";
import {EVALUATION_METRIC_NAMES, EVALUATION_PERSPECTIVES, STANDARD_ANSWER_METRICS, comparisonRows, metricCaseCount, metricValue, metricLabel, formatMetricValue, orderedThresholdEntries} from "./evaluation-metrics";
const result = (values:Partial<EvaluationMetrics>={}):EvaluationMetrics => ({case_count:1,error_count:0,evaluation_suite:"standard",passed:null,threshold_failures:[],failure_reason_counts:{},case_results:[],...values});
describe("六指標の共通契約",()=>{
 it("両ページで検索二・回答三・応答判断一の順を使う",()=>{
  expect(EVALUATION_PERSPECTIVES.map(({id,metrics})=>[id,metrics.length])).toEqual([["retrieval",2],["answer",3],["decision",1]]);
  expect(EVALUATION_METRIC_NAMES.map(metricLabel)).toEqual(["必要な根拠の取得率","正解文書の順位（MRR）","回答の正確性","回答の網羅性","根拠への忠実性","回答・確認・保留の判断の正しさ"]);
  expect(STANDARD_ANSWER_METRICS.has("claim_support_rate")).toBe(false);
 });
 it("未評価を0点や合格と混同しない",()=>{
  expect(formatMetricValue(null)).toBe("未評価");expect(formatMetricValue(0)).toBe("0%");
  expect(metricValue(result(),"answer_correctness")).toBeNull();
  expect(metricCaseCount(result({metric_case_counts:{mrr:0}}),"mrr")).toBe(0);
 });
 it("削除した指標を閾値や比較に戻さない",()=>{
  expect(orderedThresholdEntries({mrr:0.8,faithfulness:1,answer_pass_rate:1})).toEqual([["mrr",0.8]]);
  const rows=comparisonRows(result({mrr:0.5}),result({mrr:1}));
  expect(rows.filter(row=>row.metric==="mrr")).toEqual([{metric:"mrr",base:0.5,compared:1,delta:0.5,direction:"better"}]);
 });
});

it("比較は六指標の順で差と向きを計算する",()=>{
 const a=result({mrr:0.8,answer_correctness:1,requirement_coverage:0.5});
 const b=result({mrr:0.4,answer_correctness:1,requirement_coverage:0.75});
 expect(comparisonRows(a,b).filter(row=>row.delta!==null).map(row=>[row.metric,row.delta,row.direction])).toEqual([["mrr",-0.4,"worse"],["answer_correctness",0,"same"],["requirement_coverage",0.25,"better"]]);
});
it("片側だけ未評価なら差も改善判定も付けない",()=>{
 expect(comparisonRows(result(),result({claim_support_rate:0})).filter(row=>row.metric==="claim_support_rate")).toEqual([{metric:"claim_support_rate",base:null,compared:0,delta:null,direction:null}]);
});
it("両側とも未評価でも六指標を表示する",()=>{
 const rows=comparisonRows(result(),result()); expect(rows).toHaveLength(6);expect(rows.every(row=>row.base===null && row.compared===null && row.direction===null)).toBe(true);
});
it("全閾値を共通の表示順に並べる",()=>{
 expect(orderedThresholdEntries(Object.fromEntries([...EVALUATION_METRIC_NAMES].reverse().map(name=>[name,0.9]))).map(([name])=>name)).toEqual(EVALUATION_METRIC_NAMES);
});
it("対象件数は未指定と零件を区別する",()=>{
 expect(metricCaseCount(result(),"mrr")).toBeNull();
 expect(metricCaseCount(result({metric_case_counts:{mrr:2}}),"mrr")).toBe(2);
});
it("零と小数を正しく百分率表示し、欠損値は未評価にする",()=>{
 expect(formatMetricValue(0.125)).toBe("12.5%");expect(formatMetricValue(1)).toBe("100%");expect(formatMetricValue(undefined)).toBe("未評価");expect(formatMetricValue(NaN)).toBe("未評価");
});

it("支持の未評価を意味の不支持と別の理由で表示する", async () => {
 const {failureReasonLabel}=await import("./evaluation-metrics");
 expect(failureReasonLabel("claim_unassessed")).toBe("主張の支持を確認できていない");
 expect(failureReasonLabel("unsupported_claim")).not.toBe(failureReasonLabel("claim_unassessed"));
});
