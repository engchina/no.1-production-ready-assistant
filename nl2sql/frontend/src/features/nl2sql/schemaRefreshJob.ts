import { apiGet } from "../../lib/api";
import { API_TIMEOUT_MS } from "../../lib/requestPolicy";
import type { SchemaRefreshJob } from "./types";

export interface WaitForSchemaRefreshJobOptions {
  pollIntervalMs?: number;
  maxWaitMs?: number;
}

export async function waitForSchemaRefreshJob(
  jobId: string,
  signal?: AbortSignal,
  options: WaitForSchemaRefreshJobOptions = {}
) {
  const pollIntervalMs = Math.max(100, options.pollIntervalMs ?? 1_000);
  const maxWaitMs = Math.max(0, options.maxWaitMs ?? 0);
  const startedAt = Date.now();
  let job = await apiGet<SchemaRefreshJob>(`/api/schema/refresh-jobs/${jobId}`, {
    signal,
    timeoutMs: API_TIMEOUT_MS.jobControl,
  });
  while (job.status === "pending" || job.status === "running") {
    if (maxWaitMs > 0 && Date.now() - startedAt >= maxWaitMs) {
      throw new DOMException("schema_refresh_timeout", "TimeoutError");
    }
    if (signal?.aborted) {
      throw signal.reason ?? new DOMException("schema_refresh_aborted", "AbortError");
    }
    await new Promise<void>((resolve, reject) => {
      const remainingMs = maxWaitMs > 0 ? maxWaitMs - (Date.now() - startedAt) : pollIntervalMs;
      const timeoutId = globalThis.setTimeout(
        resolve,
        Math.max(0, Math.min(pollIntervalMs, remainingMs))
      );
      signal?.addEventListener(
        "abort",
        () => {
          globalThis.clearTimeout(timeoutId);
          reject(signal.reason ?? new DOMException("schema_refresh_aborted", "AbortError"));
        },
        { once: true }
      );
    });
    job = await apiGet<SchemaRefreshJob>(`/api/schema/refresh-jobs/${jobId}`, {
      signal,
      timeoutMs: API_TIMEOUT_MS.jobControl,
    });
  }
  if (job.status === "error") throw new Error(job.error_code || "schema_refresh_failed");
  return job;
}
