// node:test(jiti)から直接 import されるため、"@/" alias でなく相対 path を使う。
import { ApiError } from "../../lib/api";

/** 2.5s 間隔 × 3 回 ≒ 7.5s で追跡を断念する(永久ロック防止)。 */
export const MAX_CONSECUTIVE_POLL_FAILURES = 3;

export type PollFailureAction = "retry" | "give-up" | "job-gone";

/** job が既に存在しない(404)= リトライしても回復しない失敗。 */
export function isJobGoneError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/**
 * ポーリング失敗の扱いを分類する。
 * - 404 は即 "job-gone"(スナップショット破棄)。
 * - それ以外の失敗は連続 max 回まで "retry"、到達で "give-up"。
 */
export function classifyPollFailure(
  error: unknown,
  consecutiveFailures: number,
  max: number = MAX_CONSECUTIVE_POLL_FAILURES
): PollFailureAction {
  if (isJobGoneError(error)) return "job-gone";
  return consecutiveFailures >= max ? "give-up" : "retry";
}
