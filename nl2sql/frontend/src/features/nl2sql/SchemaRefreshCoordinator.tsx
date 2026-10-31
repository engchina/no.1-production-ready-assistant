import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";

import { toast } from "@production-ready/ui";

import { t } from "@/lib/i18n";
import { useValuesChanged } from "@/lib/render-sync";

import {
  nl2sqlIncrementalKeys,
  useActiveSchemaRefreshJob,
  useSchemaRefreshJob,
  useStartSchemaRefresh,
} from "./incrementalQueries";
import { schemaRefreshJobErrorMessage, schemaRefreshJobIsActive } from "./schemaRefreshPresentation";
import type { SchemaRefreshActiveJobData, SchemaRefreshJob } from "./types";

export interface SchemaRefreshCoordinatorValue {
  job: SchemaRefreshJob | null;
  /** job の投入（POST）を送信している間。 */
  isStarting: boolean;
  /**
   * 送信中の更新を始めた起点（`start(origin)` に渡した値）。送信中でなければ ""。
   * 押した「スキーマを更新」だけを送信の間 `loading` にするために使う。job の間はどのボタンも回さず
   * `disabled` にし、スピナーは `SchemaRefreshProcessing` が 1 つだけ出す（durable job の規則。
   * UX 契約 messaging §3.7「進捗の表示に残す」・buttons §8、#821）。
   */
  startingOrigin: string;
  /** 送信中または job の実行中。 */
  isRefreshing: boolean;
  completedJob: SchemaRefreshJob | null;
  /**
   * 今の（または最後の）job を `start(origin)` で始めた起点。変更の応答から追跡した job・別の画面が始めた
   * job は ""。案内の中のボタンで始めた再取得の進行・失敗を、その案内だけに出す（ページの別の場所に重ねない。
   * messaging §10.2、#2034）。
   */
  jobOrigin: string;
  error: string;
  /**
   * `origin` は押したボタンの識別子（同じ画面の複数の「スキーマを更新」を区別する）。
   * `announce: false` は開始の Toast を出さない（案内の中のボタンのように、押したボタンの直下に進行を出す
   * とき。進行が開始を示すので Toast と重ねない。messaging §4.2・§10.1、#2031）。
   */
  start: (origin?: string, options?: { announce?: boolean }) => Promise<SchemaRefreshJob>;
  track: (job: SchemaRefreshJob | string) => void;
  clearError: () => void;
}

const SchemaRefreshContext = createContext<SchemaRefreshCoordinatorValue | null>(null);

export function useSchemaRefreshCoordinator() {
  const value = useContext(SchemaRefreshContext);
  if (!value) {
    throw new Error("useSchemaRefreshCoordinator は SchemaRefreshCoordinator の配下で使用してください。");
  }
  return value;
}

export function SchemaRefreshCoordinator({
  children,
  discoveryEnabled = true,
}: {
  children: ReactNode;
  discoveryEnabled?: boolean;
}) {
  const queryClient = useQueryClient();
  const activeJobQuery = useActiveSchemaRefreshJob(discoveryEnabled);
  const startMutation = useStartSchemaRefresh();
  const [trackedJobId, setTrackedJobId] = useState("");
  const [trackedSnapshot, setTrackedSnapshot] = useState<SchemaRefreshJob | null>(null);
  const [completedJob, setCompletedJob] = useState<SchemaRefreshJob | null>(null);
  const [error, setError] = useState("");
  // 送信中の更新を始めた起点。isStarting の間だけ意味を持つ（startingOrigin で公開する）。
  const [startOrigin, setStartOrigin] = useState("");
  const [jobOrigin, setJobOrigin] = useState("");
  // 終端を報告済みの job（`<job_id>:<status>`）。track で空に戻す。
  const [reportedTerminal, setReportedTerminal] = useState("");
  // 実行中の job を見つけて追跡し始めた job。job の cache の seed は effect で行う。
  const [adoptedActiveJob, setAdoptedActiveJob] = useState<SchemaRefreshJob | null>(null);
  const jobQuery = useSchemaRefreshJob(trackedJobId);

  const track = useCallback(
    (jobOrId: SchemaRefreshJob | string) => {
      const job = typeof jobOrId === "string" ? null : jobOrId;
      const jobId = typeof jobOrId === "string" ? jobOrId : jobOrId.job_id;
      setReportedTerminal("");
      setCompletedJob(null);
      setError("");
      setTrackedJobId(jobId);
      setTrackedSnapshot(job);
      setJobOrigin("");
      if (job) seedSchemaRefreshJobCache(queryClient, job);
    },
    [queryClient],
  );

  // 別の実行中の job が見つかったレンダーで、その job の追跡に切り替える（effect で setState しない）。
  const activeJob = activeJobQuery.data?.active_job;
  if (activeJob && activeJob.job_id !== trackedJobId) {
    setReportedTerminal("");
    setCompletedJob(null);
    setError("");
    setTrackedJobId(activeJob.job_id);
    setTrackedSnapshot(activeJob);
    setAdoptedActiveJob(activeJob);
    setJobOrigin("");
  }
  useEffect(() => {
    if (adoptedActiveJob) seedSchemaRefreshJobCache(queryClient, adoptedActiveJob);
  }, [adoptedActiveJob, queryClient]);

  const job = jobQuery.data ?? trackedSnapshot;
  const isStarting = startMutation.isPending;
  const startingOrigin = isStarting ? startOrigin : "";
  const isRefreshing =
    isStarting ||
    schemaRefreshJobIsActive(job) ||
    (Boolean(trackedJobId) && !job);

  const start = useCallback(async (origin = "", options: { announce?: boolean } = {}) => {
    setError("");
    setStartOrigin(origin);
    try {
      const nextJob = await startMutation.mutateAsync();
      track(nextJob);
      setJobOrigin(origin);
      if (nextJob.status !== "done" && options.announce !== false) {
        toast.info(t("dataMgmt.schemaJob.accepted"));
      }
      return nextJob;
    } catch (cause) {
      const message =
        cause instanceof Error ? cause.message : t("dataMgmt.schemaJob.submitError");
      // 表示は各ページの固定面(Banner / FormStatus)が正本。Toast と二重表示しない
      // (messaging spec §0.6)。context の error だけを更新する。
      setError(message);
      throw cause;
    }
  }, [startMutation, track]);

  // job が終端になったレンダーで、完了した job と error を直す（effect で setState しない）。
  const jobChanged = useValuesChanged([job]);
  const terminalKey =
    job && (job.status === "done" || job.status === "error") ? `${job.job_id}:${job.status}` : "";
  if (jobChanged && job && terminalKey && terminalKey !== reportedTerminal) {
    setReportedTerminal(terminalKey);
    setCompletedJob(job);
    if (job.status === "done") {
      setError("");
    } else {
      // 継続する失敗は固定面(各ページの Banner / FormStatus + header status badge)を
      // 正本とし、Toast は出さない(messaging spec §0.6 / §4.2)。
      setError(schemaRefreshJobErrorMessage(job));
    }
  }

  // 終端を報告したら（completedJob が変わったら）、cache の更新・再取得・Toast を行う。
  useEffect(() => {
    if (!completedJob) return;
    queryClient.setQueryData<SchemaRefreshActiveJobData>(
      nl2sqlIncrementalKeys.activeSchemaRefreshJob,
      { active_job: null },
    );
    if (completedJob.status !== "done") return;
    void queryClient.invalidateQueries({ queryKey: ["schema"] });
    void queryClient.invalidateQueries({ queryKey: ["nl2sql", "db-admin"] });
    toast.success(t("common.action.schemaRefreshed"));
  }, [completedJob, queryClient]);

  const value = useMemo<SchemaRefreshCoordinatorValue>(
    () => ({
      job,
      isStarting,
      startingOrigin,
      isRefreshing,
      completedJob,
      jobOrigin,
      error,
      start,
      track,
      clearError: () => setError(""),
    }),
    [completedJob, error, isRefreshing, isStarting, job, jobOrigin, start, startingOrigin, track],
  );

  return <SchemaRefreshContext.Provider value={value}>{children}</SchemaRefreshContext.Provider>;
}

/** 追跡を始めた job を job の cache と（実行中なら）実行中の job の cache に入れる。 */
function seedSchemaRefreshJobCache(
  queryClient: ReturnType<typeof useQueryClient>,
  job: SchemaRefreshJob,
) {
  if (job.job_id) {
    queryClient.setQueryData(nl2sqlIncrementalKeys.schemaRefreshJob(job.job_id), job);
  }
  if (schemaRefreshJobIsActive(job)) {
    queryClient.setQueryData<SchemaRefreshActiveJobData>(
      nl2sqlIncrementalKeys.activeSchemaRefreshJob,
      { active_job: job },
    );
  }
}
