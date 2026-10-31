/** 画面応答性を揃えるための API request class。 */
export const API_TIMEOUT_MS = {
  interactiveList: 60_000,
  interactiveDetail: 30_000,
  jobControl: 5_000,
  /**
   * 永続ジョブの投入（`POST /api/nl2sql/jobs`）。投入は生成を待たずに返すが、業務プロファイル・
   * オントロジーの確認と保存で DB を何度も往復するため、DB への往復が遅い環境では 30 秒を超える（#900）。
   * 打ち切っても backend はジョブを作り終えるので、画面は送信前に決めた job ID で取り直す。
   */
  jobSubmit: 120_000,
  /**
   * チャットのターンの SQL の実行（`POST /api/nl2sql/jobs/{job_id}/execute`。#1154）。backend は SQL 生成の
   * ジョブと同じ Oracle の call timeout（既定 120 秒）で打ち切るので、それより少し長く待つ。
   */
  sqlExecute: 150_000,
  longRunningJob: 65 * 60_000,
} as const;

/** timeout 文言を policy とずれさせないための表示用変換。 */
export function requestTimeoutSeconds(timeoutMs: number): number {
  return Math.ceil(timeoutMs / 1000);
}
