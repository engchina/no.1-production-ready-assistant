import { apiGet, apiPost, isTransportError } from "@/lib/api";
import { randomUuid } from "@/lib/randomUuid";
import { API_TIMEOUT_MS } from "@/lib/requestPolicy";

import type { JobCreateData, JobData } from "./types";

/**
 * SQL の生成のジョブを投入する。job ID は送信の前に画面が決める（#900 / #916）。
 *
 * 投入は `API_TIMEOUT_MS.jobSubmit` で打ち切る。応答が届かない（timeout・通信断）ときも backend は
 * ジョブを作り終えていることがあるので、同じ ID でジョブを取り直して返す（二重に投入しない）。
 * 取り直せなければ（ジョブが無い・取得も失敗）、元の通信の失敗を投げる。
 */
export async function submitNl2SqlJob(
  body: Record<string, unknown>,
): Promise<JobCreateData | JobData> {
  const clientJobId = randomUuid();
  try {
    return await apiPost<JobCreateData>(
      "/api/nl2sql/jobs",
      { ...body, client_job_id: clientJobId },
      { timeoutMs: API_TIMEOUT_MS.jobSubmit },
    );
  } catch (cause) {
    if (!isTransportError(cause)) throw cause;
    try {
      return await apiGet<JobData>(`/api/nl2sql/jobs/${encodeURIComponent(clientJobId)}`, {
        timeoutMs: API_TIMEOUT_MS.interactiveDetail,
      });
    } catch {
      throw cause;
    }
  }
}
