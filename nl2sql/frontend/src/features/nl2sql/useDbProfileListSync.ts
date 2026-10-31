import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { toast } from "@production-ready/ui";

import { t } from "@/lib/i18n";

import {
  dbProfileListRequiredNotice,
  dbProfileListSyncJobIsActive,
  dbProfileListSyncView,
  type DbProfileListSyncNoticeContent,
  type DbProfileListSyncScope,
  type DbProfileListSyncState,
  type DbProfileListSyncView,
} from "./dbProfileListSyncPresentation";
import {
  nl2sqlIncrementalKeys,
  useSelectAiDbProfileRefreshJob,
  useStartSelectAiDbProfileRefresh,
} from "./incrementalQueries";
import type { SelectAiDbProfileRefreshJobData } from "./types";

/** 変更の API の応答の、DB Profile 一覧の同期の項目（`profile_list_refresh_job_id` などを持つ応答はそのまま渡せる）。 */
export interface DbProfileListSyncFields {
  profile_list_refresh_job_id?: string | null;
  profile_list_refresh_required?: boolean | null;
  profile_list_refresh_reason_code?: string | null;
}

/**
 * 画面の状態（取得した一覧の `profile_list_refresh_required` など）から出す、再取得を求める案内。
 * `key` が変わると案内を出し直し、null になると消す。
 */
export interface DbProfileListSyncRequirement {
  key: string;
  notice: DbProfileListSyncNoticeContent;
}

export interface DbProfileListSync {
  /** 案内の形（無ければ null）。`DbProfileListSyncNotice` が描く。 */
  view: DbProfileListSyncView | null;
  /** 待っている job（進行の表示の文言・経過時間に使う）。 */
  job: SelectAiDbProfileRefreshJobData | null;
  /** 送信中または job の実行中（ほかの「DB Profile 一覧を再取得」を無効にする）。 */
  busy: boolean;
  /** 案内の「DB Profile 一覧を再取得」の送信中（押したボタンだけを回す）。 */
  noticeStarting: boolean;
  /** 案内の外のボタン（ヘッダー）の送信中。 */
  externalStarting: boolean;
  /** job の実行中（送信は含まない）。 */
  jobActive: boolean;
  /**
   * 案内（または差分同期の進行）が進行を出している。ページの別の場所に同じ進行を重ねない
   * （同じ処理の詳細表示は 1 つ。messaging §3.7）。
   */
  ownsProgress: boolean;
  /** 変更の応答から同期の状態を始める。案内・進行を出すなら true。 */
  track: (
    result: DbProfileListSyncFields | null | undefined,
    options?: { scope?: DbProfileListSyncScope },
  ) => boolean;
  /** 案内の「DB Profile 一覧を再取得」（同じ案内の中に進行を出す）。 */
  refresh: () => Promise<void>;
  /** 案内の外のボタン（ヘッダー）から再取得を始める。進行はページの処理中の表示、失敗はページの先頭に出す。 */
  refreshFrom: () => Promise<void>;
  /** 案内を消す。 */
  reset: () => void;
  /** `required` で出した案内が、再取得の完了で解消した（同じ `key` の間 true）。 */
  requiredResolved: boolean;
}

/**
 * DB Profile 一覧（Select AI の DB Profile の読み取りモデル）の再取得・差分同期の状態を 1 つにまとめる
 * （DB 構造の `useMutationSchemaSync` と同じ形。#2034）。
 *
 * - 差分同期の job: 案内を出さず、進行だけを起点の直下に出す。完了で消え、完了の Toast を出す。
 * - 差分同期できない（`profile_list_refresh_required`）・全体の再取得が要る失敗: warning の案内と
 *   「DB Profile 一覧を再取得」。押すと同じ案内の中のボタンの直下に進行を出し、完了で案内を消す。
 * - 失敗: 同じ場所の danger の案内（エラーコードは「詳細」）。失敗の Toast は出さない（面と重ねない）。
 */
export function useDbProfileListSync({
  onDone,
  required,
}: {
  /** job が完了したとき（一覧を取り直す）。 */
  onDone?: () => void;
  required?: DbProfileListSyncRequirement | null;
} = {}): DbProfileListSync {
  const queryClient = useQueryClient();
  const startMutation = useStartSelectAiDbProfileRefresh();
  const [state, setState] = useState<DbProfileListSyncState | null>(null);
  const [startOrigin, setStartOrigin] = useState<"notice" | "external" | "">("");
  const [requiredKey, setRequiredKey] = useState("");
  const nextRequiredKey = required?.key ?? "";
  if (nextRequiredKey !== requiredKey) {
    setRequiredKey(nextRequiredKey);
    setState(required ? { kind: "notice", scope: "inline", notice: required.notice } : null);
  }

  const jobId = state?.kind === "job" ? state.jobId : "";
  const jobQuery = useSelectAiDbProfileRefreshJob(jobId);
  const job = jobQuery.data && jobQuery.data.job_id === jobId ? jobQuery.data : null;
  const starting = startMutation.isPending;
  const noticeStarting = starting && startOrigin === "notice";
  const externalStarting = starting && startOrigin === "external";
  const view = dbProfileListSyncView(state, {
    job,
    jobQueryError: jobQuery.error,
    noticeStarting,
  });
  const jobActive = Boolean(jobId) && !jobQuery.error && (!job || dbProfileListSyncJobIsActive(job));

  const onDoneRef = useRef(onDone);
  const jobRef = useRef(job);
  useLayoutEffect(() => {
    onDoneRef.current = onDone;
    jobRef.current = job;
  });

  // 待っている job が完了したら、一度だけ一覧を取り直して完了の Toast を出す。
  const doneJobId = job?.status === "done" ? job.job_id : "";
  useEffect(() => {
    if (!doneJobId) return;
    const doneJob = jobRef.current;
    void queryClient.invalidateQueries({ queryKey: ["nl2sql", "select-ai"] });
    onDoneRef.current?.();
    // 1 文目は何が起きたかだけにし、件数は description に分ける（#899）。
    toast.success(t("profiles.dbProfileRefresh.done"), {
      description: t("profiles.dbProfileRefresh.doneDetail", {
        changed: doneJob?.changed_profiles ?? 0,
        deleted: doneJob?.deleted_profiles ?? 0,
      }),
    });
  }, [doneJobId, queryClient]);

  const track = useCallback(
    (
      result: DbProfileListSyncFields | null | undefined,
      options: { scope?: DbProfileListSyncScope } = {},
    ) => {
      const scope = options.scope ?? "inline";
      const nextJobId = result?.profile_list_refresh_job_id ?? "";
      if (nextJobId) {
        setState({ kind: "job", scope, jobId: nextJobId, notice: null });
        return true;
      }
      if (result?.profile_list_refresh_required) {
        setState({
          kind: "notice",
          scope,
          notice: dbProfileListRequiredNotice(result.profile_list_refresh_reason_code ?? ""),
        });
        return true;
      }
      return false;
    },
    [],
  );

  const startRefresh = useCallback(
    async (
      origin: "notice" | "external",
      scope: DbProfileListSyncScope,
      notice: DbProfileListSyncNoticeContent | null,
    ) => {
      setStartOrigin(origin);
      let nextJob: SelectAiDbProfileRefreshJobData;
      try {
        nextJob = await startMutation.mutateAsync();
      } catch (error) {
        setState({
          kind: "notice",
          scope,
          notice: {
            severity: "danger",
            message: t("profiles.dbProfileRefresh.submitError"),
            errorCode: "",
            error,
          },
        });
        return;
      }
      queryClient.setQueryData(nl2sqlIncrementalKeys.selectAiDbProfileRefreshJob(nextJob.job_id), nextJob);
      setState({ kind: "job", scope, jobId: nextJob.job_id, notice });
    },
    [queryClient, startMutation],
  );

  const refresh = useCallback(async () => {
    if (!view || view.kind !== "notice") return;
    const { scope, severity, title, message, errorCode, error } = view;
    // 再取得の間は、押したときの案内の文をそのまま出す（失敗の詳細は再取得の間は出さない）。
    await startRefresh("notice", scope, { severity, title, message, errorCode, error });
  }, [startRefresh, view]);

  const refreshFrom = useCallback(async () => {
    await startRefresh("external", "page", null);
  }, [startRefresh]);

  const reset = useCallback(() => setState(null), []);

  const ownsProgress =
    (view?.kind === "notice" && view.refreshing) ||
    (view?.kind === "syncing" && view.scope === "inline");
  const requiredResolved = Boolean(required) && requiredKey === nextRequiredKey && view === null;

  return {
    view,
    job,
    busy: starting || jobActive,
    noticeStarting,
    externalStarting,
    jobActive,
    ownsProgress,
    track,
    refresh,
    refreshFrom,
    reset,
    requiredResolved,
  };
}
