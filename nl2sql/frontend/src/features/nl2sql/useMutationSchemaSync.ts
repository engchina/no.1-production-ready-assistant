import { useCallback, useEffect, useRef, useState } from "react";

import { t } from "@/lib/i18n";

import { useSchemaRefreshCoordinator } from "./SchemaRefreshCoordinator";
import { useSchemaRefreshJob } from "./incrementalQueries";
import {
  schemaSyncRequiredNotice,
  schemaSyncView,
  type SchemaSyncNoticeContent,
  type SchemaSyncScope,
  type SchemaSyncState,
  type SchemaSyncView,
} from "./schemaRefreshPresentation";
import type { SchemaRefreshJob } from "./types";

/** 変更の API の応答の、DB 構造の同期の項目（`schema_refresh_job_id` などを持つ応答はそのまま渡せる）。 */
export interface MutationSchemaSyncFields {
  schema_refresh_job_id?: string | null;
  schema_refresh_required?: boolean | null;
  schema_refresh_reason_code?: string | null;
}

export interface MutationSchemaSync {
  /** 案内の形（無ければ null）。`SchemaSyncNotice` が描く。 */
  view: SchemaSyncView | null;
  /** 案内の中の「DB 構造を再取得」の起点（`SchemaRefreshCoordinator` の `start(origin)`）。 */
  noticeOrigin: string;
  /**
   * 案内（または差分同期の進行）が DB 構造の再取得の進行を出している。ページの別の場所に同じ進行を
   * 重ねない（同じ処理の詳細表示は 1 つ。messaging §3.7）。
   */
  ownsProgress: boolean;
  /** 変更の応答から同期の状態を始める。案内・進行を出すなら true。 */
  track: (result: MutationSchemaSyncFields, options?: { scope?: SchemaSyncScope }) => boolean;
  /** 案内の「DB 構造を再取得」（同じ案内の中に進行を出し、開始の Toast は出さない）。 */
  refresh: () => Promise<void>;
  /**
   * 案内の外のボタン（ヘッダーの「DB 構造を再取得」など）から全体の再取得を始める。進行はページの
   * 処理中の表示が出し、失敗だけをページの先頭（`page`）の案内に出す。
   */
  refreshFrom: (origin: string) => Promise<void>;
  /** 案内を消す（次の実行・入力の消去）。 */
  reset: () => void;
  /** 待っている再取得・同期の job（無ければ ""）。ページの別の場所に同じ job の失敗を重ねない。 */
  jobId: string;
  /**
   * `required` で出した案内が、再取得の完了（この案内か別の起点）で解消した（同じ `key` の間 true）。
   * 再取得を求める案内を消し、次の行動（「再実行」など）に導く（messaging §10.2、#2034）。
   */
  requiredResolved: boolean;
}

/**
 * 画面の状態（失敗した job・取得した警告など）から出す、再取得を求める案内。`key` が変わると案内を出し直し、
 * null になると消す。変更の応答から始める `track` とは同時に使わない。
 */
export interface SchemaSyncRequirement {
  /** 案内の元の状態の識別子（失敗した job の ID・警告の並びなど）。 */
  key: string;
  notice: SchemaSyncNoticeContent;
}

/**
 * 変更（DDL・取込・メタデータの適用など）の後の DB 構造の同期の状態を 1 つにまとめる（#2031）。
 *
 * - 差分同期の job は、案内を出さず進行だけを起点の直下に出す。完了で消え、`onDone` を呼ぶ。
 * - 差分同期できない（`schema_refresh_required`）・全体の再取得が要る失敗は、warning の案内と
 *   「DB 構造を再取得」。押すと同じ案内の中のボタンの直下に進行を出し、完了で案内を消す。
 * - 別の起点の全体の再取得が完了したときも、再取得を求める案内を消す（再取得を求め続けない）。
 * - 再取得の失敗は同じ場所の danger の案内（エラーコードは「詳細」）。
 */
export function useMutationSchemaSync({
  noticeOrigin,
  onDone,
  required,
}: {
  noticeOrigin: string;
  /** 同期・再取得の job が完了したとき（一覧などの読み取りモデルを取り直す）。 */
  onDone?: () => void;
  /** 画面の状態から出す、再取得を求める案内（`SchemaSyncRequirement`）。 */
  required?: SchemaSyncRequirement | null;
}): MutationSchemaSync {
  const coordinator = useSchemaRefreshCoordinator();
  const { start, track: trackCoordinatorJob, clearError, completedJob, startingOrigin, isRefreshing } =
    coordinator;
  const [state, setState] = useState<SchemaSyncState | null>(null);
  // `required` の案内を出した key。key が変わったレンダーで案内を出し直す（effect で setState しない）。
  const [requiredKey, setRequiredKey] = useState("");
  const nextRequiredKey = required?.key ?? "";
  if (nextRequiredKey !== requiredKey) {
    setRequiredKey(nextRequiredKey);
    setState(
      required
        ? {
            kind: "notice",
            scope: "inline",
            notice: required.notice,
            baselineCompletedJobId: completedJob?.job_id ?? "",
          }
        : null,
    );
  }
  const jobId = state?.kind === "job" ? state.jobId : "";
  const jobQuery = useSchemaRefreshJob(jobId);
  const noticeStarting = startingOrigin === noticeOrigin;
  const view = schemaSyncView(state, {
    job: jobQuery.data,
    jobQueryError: jobQuery.error,
    completedJob,
    noticeStarting,
  });

  // track は await の後に呼ばれるので、案内を出した時点の完了済みの job は最新の値を ref で読む。
  const completedJobIdRef = useRef("");
  const onDoneRef = useRef(onDone);
  useEffect(() => {
    completedJobIdRef.current = completedJob?.job_id ?? "";
  }, [completedJob]);
  useEffect(() => {
    onDoneRef.current = onDone;
  }, [onDone]);

  // 待っている job が完了したら、一度だけ読み取りモデルを取り直す。
  const doneJobId =
    state?.kind === "job" && jobQuery.data?.job_id === state.jobId && jobQuery.data.status === "done"
      ? state.jobId
      : "";
  useEffect(() => {
    if (doneJobId) onDoneRef.current?.();
  }, [doneJobId]);

  const showNotice = useCallback((scope: SchemaSyncScope, notice: SchemaSyncNoticeContent) => {
    setState({ kind: "notice", scope, notice, baselineCompletedJobId: completedJobIdRef.current });
  }, []);

  const track = useCallback(
    (result: MutationSchemaSyncFields, options: { scope?: SchemaSyncScope } = {}) => {
      const scope = options.scope ?? "inline";
      clearError();
      if (result.schema_refresh_job_id) {
        trackCoordinatorJob(result.schema_refresh_job_id);
        setState({ kind: "job", scope, jobId: result.schema_refresh_job_id, notice: null });
        return true;
      }
      if (result.schema_refresh_required) {
        showNotice(scope, schemaSyncRequiredNotice(result.schema_refresh_reason_code ?? ""));
        return true;
      }
      setState(null);
      return false;
    },
    [clearError, showNotice, trackCoordinatorJob],
  );

  const startRefresh = useCallback(
    async (origin: string, scope: SchemaSyncScope, notice: SchemaSyncNoticeContent | null) => {
      let job: SchemaRefreshJob;
      try {
        job = await start(origin, { announce: notice === null });
      } catch (error) {
        showNotice(scope, {
          severity: "danger",
          message: t("dataMgmt.schemaJob.submitError"),
          errorCode: "",
          error,
        });
        return;
      }
      if (!job.job_id || job.status === "done") {
        setState(null);
        onDoneRef.current?.();
        return;
      }
      setState({ kind: "job", scope, jobId: job.job_id, notice });
    },
    [showNotice, start],
  );

  const refresh = useCallback(async () => {
    if (!view || view.kind !== "notice") return;
    const { scope, severity, title, message, errorCode, error } = view;
    // 再取得の間は、押したときの案内の文をそのまま出す（失敗の詳細は再取得の間は出さない）。
    await startRefresh(noticeOrigin, scope, { severity, title, message, errorCode, error });
  }, [noticeOrigin, startRefresh, view]);

  const refreshFrom = useCallback(
    async (origin: string) => {
      clearError();
      setState(null);
      await startRefresh(origin, "page", null);
    },
    [clearError, startRefresh],
  );

  const reset = useCallback(() => {
    setState(null);
    clearError();
  }, [clearError]);

  const ownsProgress =
    isRefreshing &&
    ((view?.kind === "notice" && view.refreshing) ||
      (view?.kind === "syncing" && view.scope === "inline"));

  const requiredResolved = Boolean(required) && requiredKey === nextRequiredKey && view === null;

  return {
    view,
    noticeOrigin,
    ownsProgress,
    track,
    refresh,
    refreshFrom,
    reset,
    jobId,
    requiredResolved,
  };
}
