// 回答前の確認の LLM の失敗の「詳細」（#1715）。backend の `error_details` を共通の ApiErrorBanner の「詳細」に
// 並べ、要約は backend の 1 行の理由にする（チャットと SQL 生成の画面で同じ ApiError・同じ部品）。
import assert from "node:assert/strict";
import test from "node:test";

import { presentApiError } from "@production-ready/ui";

import { ApiError } from "../src/features/nl2sql/ontology/api.ts";
import { requirementFailureDetails } from "../src/features/nl2sql/requirementFailure.ts";

const details = {
  kind: "unavailable",
  kind_label_ja: "サービスの一時的な失敗（5xx）",
  error_code: "ENTERPRISE_AI_UNAVAILABLE",
  http_status: 500,
  model_id: "xai.grok-4.3",
  elapsed_ms: 28350,
  attempts: 2,
  timeout_seconds: 90,
  upstream_request_id: "opc-req-500",
  provider_message: "Internal server error (internal_error)",
  endpoint_host: "inference.generativeai.us-chicago-1.oci.oraclecloud.com",
};

test("失敗の種類・HTTP ステータス・モデル・所要時間・試行回数・request id を「詳細」に出す", () => {
  const rows = Object.fromEntries(requirementFailureDetails(details).map((item) => [item.label, item.value]));
  assert.equal(rows["失敗の種類"], "サービスの一時的な失敗（5xx）");
  assert.equal(rows["OCI Enterprise AI の HTTP ステータス"], "500");
  assert.equal(rows["モデル"], "xai.grok-4.3");
  assert.equal(rows["所要時間"], "28.4 秒");
  assert.equal(rows["試行回数"], "2 回");
  assert.equal(rows["LLM の待ち時間の上限"], "90 秒");
  assert.equal(rows["OCI のリクエスト ID"], "opc-req-500");
  assert.equal(rows["応答の理由"], "Internal server error (internal_error)");
  assert.deepEqual(requirementFailureDetails(null), []);
  assert.deepEqual(requirementFailureDetails("x"), []);
});

test("ApiError は backend の理由を要約に、code・LLM の失敗・request ID を「詳細」に分ける", () => {
  const error = new ApiError("回答前の確認で OCI Enterprise AI がサーバーのエラーを返しました（HTTP 500、2 回試行）。", 503, {
    errorCode: "REQUIREMENT_AI_FAILED",
    requestId: "req-1",
    errorDetails: details,
  });
  const presentation = presentApiError(error, "既定の文");
  assert.equal(presentation.summary, "回答前の確認で OCI Enterprise AI がサーバーのエラーを返しました（HTTP 500、2 回試行）。");
  const labels = presentation.details.map((item) => item.label);
  assert.deepEqual(labels.slice(0, 2), ["HTTP ステータス", "エラーコード"]);
  assert.equal(labels.at(-1), "リクエストID");
  assert.ok(labels.includes("モデル"));
  // 同じ値を要約と詳細で二重に出さない（要約に code を付けない）。
  assert.equal(presentation.summary.includes("REQUIREMENT_AI_FAILED"), false);
});
