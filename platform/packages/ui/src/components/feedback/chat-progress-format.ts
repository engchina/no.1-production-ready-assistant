/**
 * 所要時間の既定の表記（1 秒未満も 0.1 秒単位で出す。0.1 秒に満たない時間は「0.1 秒未満」（「0.0 秒」と出すと
 * 情報が無い。#2096）。1 分以上は「1 分 5 秒」）。
 *
 * チャットの処理の経過（`ChatProgress`）と実行履歴（`formatRunDuration`・`RunTimeline`）が同じ表記を使う。
 * `ChatProgress` が `RunTimeline` を描くため、循環の import を作らないよう部品から分けて置く（#2265）。
 */
export function formatChatProgressDuration(ms: number): string {
  const value = Math.max(0, ms);
  if (value > 0 && value < 100) return "0.1 秒未満";
  if (value < 10_000) return `${(Math.floor(value / 100) / 10).toFixed(1)} 秒`;
  const totalSeconds = Math.floor(value / 1000);
  if (totalSeconds < 60) return `${totalSeconds} 秒`;
  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes < 60) return `${totalMinutes} 分 ${totalSeconds % 60} 秒`;
  return `${Math.floor(totalMinutes / 60)} 時間 ${totalMinutes % 60} 分`;
}
