// 回答前の確認の LLM の失敗の「詳細」（#1715）。backend の `error_details`（失敗の種類・OCI Enterprise AI の
// HTTP ステータス・モデル ID・所要時間・試行回数・OCI の request id・応答の理由）を、共通の `ApiErrorBanner` の
// 「詳細」の項目にする。secret・prompt の全文は backend が入れない（入っていない値は出さない）。
//
// node:test(jiti)から直接 import されるため、"@/" alias を使わない。
import type { ApiErrorDetail } from "@production-ready/ui";

import { t } from "../../lib/i18n.ts";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "";
}

function detail(label: string, value: string): ApiErrorDetail[] {
  return value ? [{ label, value }] : [];
}

function seconds(ms: number): string {
  return (Math.round(ms / 100) / 10).toFixed(1);
}

/** backend の `error_details` を「詳細」の項目にする（形が違えば空）。 */
export function requirementFailureDetails(value: unknown): ApiErrorDetail[] {
  if (!isRecord(value)) return [];
  const elapsed = typeof value.elapsed_ms === "number" ? value.elapsed_ms : null;
  const attempts = typeof value.attempts === "number" ? value.attempts : null;
  const timeout = typeof value.timeout_seconds === "number" ? value.timeout_seconds : null;
  const maxTokens = typeof value.max_output_tokens === "number" ? value.max_output_tokens : null;
  return [
    ...detail(t("nl2sql.requirementFailure.kind"), text(value.kind_label_ja) || text(value.kind)),
    ...detail(t("nl2sql.requirementFailure.httpStatus"), text(value.http_status)),
    ...detail(t("nl2sql.requirementFailure.model"), text(value.model_id)),
    ...detail(
      t("nl2sql.requirementFailure.elapsed"),
      elapsed === null ? "" : t("nl2sql.requirementFailure.elapsedSeconds", { seconds: seconds(elapsed) })
    ),
    ...detail(
      t("nl2sql.requirementFailure.attempts"),
      attempts === null ? "" : t("nl2sql.requirementFailure.attemptsValue", { count: attempts })
    ),
    ...detail(
      t("nl2sql.requirementFailure.timeout"),
      timeout === null ? "" : t("nl2sql.requirementFailure.timeoutValue", { seconds: timeout })
    ),
    ...detail(
      t("nl2sql.requirementFailure.maxOutputTokens"),
      maxTokens === null ? "" : t("nl2sql.requirementFailure.maxOutputTokensValue", { count: maxTokens })
    ),
    ...detail(t("nl2sql.requirementFailure.providerMessage"), text(value.provider_message)),
    ...detail(t("nl2sql.requirementFailure.failureCode"), text(value.error_code)),
    ...detail(t("nl2sql.requirementFailure.endpointHost"), text(value.endpoint_host)),
    ...detail(t("nl2sql.requirementFailure.upstreamRequestId"), text(value.upstream_request_id)),
  ];
}
