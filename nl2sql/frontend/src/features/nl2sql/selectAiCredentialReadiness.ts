/**
 * Select AI の Credential の事前条件（#1683 / #1688）。
 *
 * DBMS_CLOUD_AI Profile は Select AI の Credential を参照するため、Credential が無いと業務プロファイルの
 * 保存（Oracle 反映）・SQL 生成とチャットの Select AI / Select AI Agent・合成データ・SQL 生成評価が
 * Oracle の処理の途中で失敗する。backend の `GET /api/nl2sql/select-ai/credential-readiness` が
 * Credential の有無を返し、無いときはこれらの操作の開始が 409 `SELECT_AI_CREDENTIAL_MISSING` を返す。
 * 画面は操作の直前に `SelectAiCredentialPrecondition` で事前条件を示し、操作を無効にする。
 */
import { ApiError } from "../../lib/api";

import type { Nl2SqlEngine, SelectAiCredentialReadinessData } from "./types";

export const SELECT_AI_CREDENTIAL_READINESS_URL = "/api/nl2sql/select-ai/credential-readiness";
export const SELECT_AI_CREDENTIAL_READINESS_KEY = [
  "nl2sql",
  "select-ai",
  "credential-readiness",
] as const;
export const SELECT_AI_CREDENTIAL_MISSING_CODE = "SELECT_AI_CREDENTIAL_MISSING";

/** Select AI の Credential を使う生成の方式（OCI Enterprise AI の直接の生成は使わない）。 */
const SELECT_AI_CREDENTIAL_ENGINES: ReadonlySet<Nl2SqlEngine> = new Set([
  "select_ai",
  "select_ai_agent",
]);

export function engineUsesSelectAiCredential(engine: Nl2SqlEngine | string): boolean {
  return SELECT_AI_CREDENTIAL_ENGINES.has(engine as Nl2SqlEngine);
}

export function enginesUseSelectAiCredential(
  engines: Iterable<Nl2SqlEngine | string>,
): boolean {
  for (const engine of engines) {
    if (engineUsesSelectAiCredential(engine)) return true;
  }
  return false;
}

/** Credential が要り、無いと確かめられたときだけ true（確かめられない・不要なら止めない）。 */
export function isSelectAiCredentialMissing(
  readiness: SelectAiCredentialReadinessData | null | undefined,
): boolean {
  return Boolean(readiness?.credential_required && readiness.credential_exists === false);
}

export function isSelectAiCredentialMissingError(error: unknown): boolean {
  return error instanceof ApiError && error.errorCode === SELECT_AI_CREDENTIAL_MISSING_CODE;
}
