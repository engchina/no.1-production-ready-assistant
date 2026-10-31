import { elapsedMsSince, formatElapsedClock } from "@production-ready/ui";

// 汎用の計算は共有パッケージが持つ。NL2SQL 固有の表示だけをここに置く。
export {
  elapsedMsBetween,
  elapsedMsSince,
  formatElapsedClock,
  operationTimestampMs,
  type OperationTimestamp,
} from "@production-ready/ui";

/** 開始時刻からの経過 seconds。既存 NL2SQL timer との互換用。 */
export function elapsedSecondsSince(startedAtMs: number, nowMs = Date.now()): number {
  return Math.floor(elapsedMsSince(startedAtMs, nowMs) / 1000);
}

/** 技術結果・履歴用。1秒未満の milliseconds 精度は従来どおり維持する。 */
export function formatElapsedDuration(ms?: number | null): string {
  if (ms === null || ms === undefined) return "-";
  const normalized = Math.max(0, ms);
  if (normalized < 1000) return `${Math.round(normalized)}ms`;
  if (normalized < 60_000) return `${(normalized / 1000).toFixed(1)}秒`;
  return formatElapsedClock(normalized);
}
