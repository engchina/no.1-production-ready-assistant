import { useCallback, useEffect, useRef, useState } from "react";

import { useWorkspaceIdentity } from "@/components/WorkspaceState";
import { apiGet, isAbortError } from "@/lib/api";
import { t } from "@/lib/i18n";
import { useValuesChanged } from "@/lib/render-sync";
import { API_TIMEOUT_MS } from "@/lib/requestPolicy";
import {
  clearActiveJobSnapshot,
  hasLegacyActiveJobSnapshot,
  scopedActiveJobStorage,
  isJobInFlight,
  isJobTerminal,
  persistActiveJobSnapshot,
  readActiveJobSnapshot,
  type ActiveJobStorage,
} from "./jobPersistence";
import { classifyPollFailure } from "./jobPollingPolicy";
import type { JobCreateData, JobData, Nl2SqlResult } from "./types";

interface UseNl2SqlJobPollingOptions {
  /** 終わったジョブの結果。`job.status` で SQL を実行して結果を整形したか（`done`）を判定できる（#917）。 */
  onResult(result: Nl2SqlResult, job: JobData): void;
  /** job 自体の失敗(終端遷移の error_message)。表示は OperationStatusStrip 側が正本。 */
  onJobFailed(message: string): void;
  /** ポーリング通信の断念(連続失敗)/ job 消失(404)。追跡は解除済みで UI ロックは解ける。 */
  onPollingLost(message: string): void;
  onHistoryRefresh(): Promise<void> | void;
  /** 履歴更新の失敗。結果自体は成功しているため、失敗表示に変えてはならない。 */
  onHistoryRefreshFailed?(cause: unknown): void;
  pollIntervalMs?: number;
}

// ブラウザ保存は復元用の補助。getter / 読込 / 書込 / 削除の失敗で実行追跡を止めない。
function withBrowserStorage(owner: string, context: string, action: (storage: ActiveJobStorage) => void): boolean {
  try {
    if (typeof window === "undefined") return false;
    action(scopedActiveJobStorage(window.sessionStorage, owner, context));
    return true;
  } catch {
    return false;
  }
}

/** 復元直後に表示する in-flight プレースホルダ。実データは次のポーリングで上書きされる。 */
function syntheticInFlightJob(jobId: string, startedAtMs: number): JobData {
  return {
    job_id: jobId,
    status: "running",
    created_at: new Date(startedAtMs).toISOString(),
    result: null,
    error_message: null,
    warning_message: null,
    timing: null,
    steps: [],
  };
}

/** 旧形式(localStorage)の実行中 job の保存が残っているか。読めなければ false。 */
function readLegacyJobSnapshotIgnored(): boolean {
  try {
    return hasLegacyActiveJobSnapshot(window.localStorage, Date.now());
  } catch {
    return false;
  }
}

export function useNl2SqlJobPolling({
  onResult,
  onJobFailed,
  onPollingLost,
  onHistoryRefresh,
  onHistoryRefreshFailed,
  pollIntervalMs = 2500,
}: UseNl2SqlJobPollingOptions) {
  const { owner, context } = useWorkspaceIdentity();
  const withStorage = useCallback(
    (action: (storage: ActiveJobStorage) => void) => withBrowserStorage(owner, context, action),
    [owner, context]
  );
  const [job, setJob] = useState<JobData | null>(null);
  const [jobStartedAt, setJobStartedAt] = useState<number | null>(null);
  const [jobStorageUnavailable, setJobStorageUnavailable] = useState(false);
  const [legacyJobSnapshotIgnored, setLegacyJobSnapshotIgnored] = useState(false);
  const consecutiveFailuresRef = useRef(0);
  const trackedJobIdRef = useRef<string | null>(null);

  const stopTracking = useCallback(() => {
    withStorage((storage) => clearActiveJobSnapshot(storage, trackedJobIdRef.current));
    trackedJobIdRef.current = null;
    consecutiveFailuresRef.current = 0;
    setJob(null);
    setJobStartedAt(null);
  }, [withStorage]);

  const pollJob = useCallback(
    async (jobId: string, signal?: AbortSignal) => {
      const data = await apiGet<JobData>(`/api/nl2sql/jobs/${jobId}`, {
        signal,
        timeoutMs: API_TIMEOUT_MS.interactiveDetail,
      });
      if (signal?.aborted) return data;
      consecutiveFailuresRef.current = 0;
      setJob(data);
      if (isJobTerminal(data.status)) {
        withStorage((storage) => clearActiveJobSnapshot(storage, jobId));
        if (data.result) onResult(data.result, data);
        if (data.error_message) onJobFailed(data.error_message);
        if (signal?.aborted) return data;
        try {
          await onHistoryRefresh();
        } catch (cause) {
          if (!isAbortError(cause)) onHistoryRefreshFailed?.(cause);
        }
      }
      return data;
    },
    [onHistoryRefresh, onHistoryRefreshFailed, onJobFailed, onResult, withStorage]
  );

  // ポーリング失敗を分類し、断念時は追跡を解除して UI ロックを解く。
  // 旧実装は失敗を onError へ流すだけで追跡を解除せず、通信断で画面が永久ロックしていた。
  const handlePollFailure = useCallback(
    (cause: unknown) => {
      if (isAbortError(cause)) return;
      const action = classifyPollFailure(cause, ++consecutiveFailuresRef.current);
      if (action === "retry") return;
      stopTracking();
      onPollingLost(action === "job-gone" ? t("nl2sql.job.expired") : t("nl2sql.job.pollingLost"));
    },
    [onPollingLost, stopTracking]
  );

  const trackJob = useCallback((data: JobCreateData, startedAtMs: number) => {
    trackedJobIdRef.current = data.job_id;
    const saved = withStorage((storage) => persistActiveJobSnapshot(storage, data.job_id, startedAtMs));
    if (!saved) withStorage((storage) => clearActiveJobSnapshot(storage, data.job_id));
    setJobStorageUnavailable(!saved);
    consecutiveFailuresRef.current = 0;
    setJobStartedAt(startedAtMs);
    // 投入の応答が届かずに取り直したジョブ（#916）は、既に終わっていることがある。結果・履歴の反映は
    // ポーリングの経路で 1 回だけ行うため、終わったジョブも取得待ちとして追跡を始める。
    setJob({
      job_id: data.job_id,
      status: isJobInFlight(data.status) ? data.status : "pending",
      created_at: data.created_at,
      steps: data.steps,
      result: null,
      error_message: null,
      warning_message: null,
      timing: null,
    });
  }, [withStorage]);

  const clearTrackedJob = useCallback(() => {
    stopTracking();
  }, [stopTracking]);

  // 再訪時の復元は合成 in-flight job を置くだけにし、実際の取得・失敗処理は
  // 下の interval effect に一本化する(失効 job への失敗リクエスト連発を防ぐ)。
  // owner / context が変わったレンダーで追跡中の表示を消し（effect で setState しない）、
  // 保存からの復元は effect の中の保存読み出しの callback で行う。
  const storageScopeChanged = useValuesChanged([withStorage]);
  if (storageScopeChanged) {
    setJob(null);
    setJobStartedAt(null);
    setJobStorageUnavailable(false);
    setLegacyJobSnapshotIgnored(readLegacyJobSnapshotIgnored());
  }
  useEffect(() => {
    trackedJobIdRef.current = null;
    consecutiveFailuresRef.current = 0;
    withStorage((storage) => {
      const snapshot = readActiveJobSnapshot(storage, Date.now());
      if (!snapshot) return;
      trackedJobIdRef.current = snapshot.jobId;
      setJobStartedAt(snapshot.startedAtMs);
      setJob(syntheticInFlightJob(snapshot.jobId, snapshot.startedAtMs));
    });
  }, [withStorage]);

  // job オブジェクトではなく「in-flight な job_id」へ依存させる。
  // 未完了の取得がある tick はスキップし、遅い応答を次の tick で中断しない。
  const activeJobId = job && isJobInFlight(job.status) ? job.job_id : null;
  useEffect(() => {
    if (!activeJobId) return undefined;
    let controller: AbortController | null = null;
    const tick = () => {
      if (controller) return;
      controller = new AbortController();
      const signal = controller.signal;
      void pollJob(activeJobId, signal)
        .catch((cause: unknown) => {
          if (!signal.aborted) handlePollFailure(cause);
        })
        .finally(() => { controller = null; });
    };
    tick();
    const timer = window.setInterval(tick, pollIntervalMs);
    return () => {
      controller?.abort();
      window.clearInterval(timer);
    };
  }, [activeJobId, handlePollFailure, pollIntervalMs, pollJob]);

  return {
    job,
    jobStartedAt,
    jobStorageUnavailable,
    legacyJobSnapshotIgnored,
    pollJob,
    trackJob,
    clearTrackedJob,
  };
}
