import { t } from "../../lib/i18n";

import type { SelectAiDbProfileRefreshJobData } from "./types";

/** 案内を出す場所。`inline` は起点の操作の結果の直下、`page` はページの先頭（結果の面を持たない操作）。 */
export type DbProfileListSyncScope = "inline" | "page";

/** 案内（Banner）の中身。warning は再取得を求める案内、danger は再取得・同期の失敗。 */
export interface DbProfileListSyncNoticeContent {
  severity: "warning" | "danger";
  /** 案内の見出し（何が起きたか）。API の失敗は要約を見出しにする。 */
  title?: string;
  message: string;
  /** 技術的な詳細（「詳細」に出すエラーコード）。 */
  errorCode: string;
  /** API の失敗（応答が届かなかった失敗を含む）。要約と「詳細」は `presentApiError` で作る。 */
  error?: unknown;
}

/**
 * 変更（業務プロファイルの反映・DB Profile の作成や削除）の後の DB Profile 一覧の同期の状態
 * （`useDbProfileListSync` が持つ）。
 * - `notice`: 案内を出している（再取得を求める・失敗）。
 * - `job`: 再取得・差分同期の job を待つ。`notice` は案内の「DB Profile 一覧を再取得」から始めたときの案内
 *   （job の間も同じ案内を出し、その中のボタンの直下に進行を出す）。
 */
export type DbProfileListSyncState =
  | { kind: "notice"; scope: DbProfileListSyncScope; notice: DbProfileListSyncNoticeContent }
  | {
      kind: "job";
      scope: DbProfileListSyncScope;
      jobId: string;
      notice: DbProfileListSyncNoticeContent | null;
    };

/** 案内の部品（`DbProfileListSyncNotice`）が描く形。 */
export type DbProfileListSyncView =
  | ({
      kind: "notice";
      scope: DbProfileListSyncScope;
      /** 案内のボタンから再取得を始めた（送信中または job の実行中）。進行を同じ Banner の中に出す。 */
      refreshing: boolean;
    } & DbProfileListSyncNoticeContent)
  /** 変更の後の差分同期（job）を待つ。案内は出さず、進行だけを出す。 */
  | { kind: "syncing"; scope: DbProfileListSyncScope };

export function dbProfileListSyncJobIsActive(job: SelectAiDbProfileRefreshJobData | null | undefined) {
  return job?.status === "pending" || job?.status === "running";
}

/** 全体の再取得を求める理由のコード（backend の `profile_list_refresh_reason_code`）。 */
const REFRESH_REQUIRED_REASON_CODES = new Set([
  "profile_list_refresh_target_unresolved",
  "profile_list_refresh_submit_failed",
  "profile_list_refresh_full_required",
  "profile_list_read_model_uninitialized",
]);

/** 全体の再取得を求める案内の文（理由のコードごと）。 */
export function dbProfileListRefreshRequiredMessage(reasonCode = "") {
  if (reasonCode === "profile_list_refresh_target_unresolved") {
    return t("profiles.dbProfileRefresh.targetUnresolved");
  }
  if (reasonCode === "profile_list_refresh_submit_failed") {
    return t("profiles.dbProfileRefresh.submitFailed");
  }
  if (reasonCode === "profile_list_read_model_uninitialized") {
    return t("profiles.dbProfileRefresh.uninitialized");
  }
  return t("profiles.dbProfileRefresh.fullRequired");
}

/** 全体の再取得を求める案内（warning。変更は済んでおり、DB Profile 一覧の読み取りが古いだけ）。 */
export function dbProfileListRequiredNotice(reasonCode = ""): DbProfileListSyncNoticeContent {
  return {
    severity: "warning",
    message: dbProfileListRefreshRequiredMessage(reasonCode),
    errorCode: "",
  };
}

/** job の失敗の案内。全体の再取得が要る失敗は再取得を求める案内、それ以外は danger（エラーコードは「詳細」）。 */
export function dbProfileListJobErrorNotice(
  job: SelectAiDbProfileRefreshJobData,
): DbProfileListSyncNoticeContent {
  if (job.requires_full_refresh || REFRESH_REQUIRED_REASON_CODES.has(job.error_code)) {
    return dbProfileListRequiredNotice(job.error_code);
  }
  return {
    severity: "danger",
    message: t("profiles.dbProfileRefresh.error"),
    errorCode: job.error_code,
  };
}

/**
 * 同期の状態と job から、案内の形を決める（描くものが無ければ null）。job が完了したら案内を消す
 * （再取得を求め続けない。messaging §10.2、#2034）。
 */
export function dbProfileListSyncView(
  state: DbProfileListSyncState | null,
  options: {
    job: SelectAiDbProfileRefreshJobData | null | undefined;
    jobQueryError?: unknown;
    /** 案内のボタンの再取得の送信中。 */
    noticeStarting: boolean;
  },
): DbProfileListSyncView | null {
  if (!state) return null;
  if (state.kind === "notice") {
    return { kind: "notice", scope: state.scope, refreshing: options.noticeStarting, ...state.notice };
  }
  const job = options.job && options.job.job_id === state.jobId ? options.job : null;
  if (job?.status === "done") return null;
  if (job?.status === "error") {
    return { kind: "notice", scope: state.scope, refreshing: false, ...dbProfileListJobErrorNotice(job) };
  }
  if (options.jobQueryError) {
    return {
      kind: "notice",
      scope: state.scope,
      refreshing: false,
      severity: "danger",
      message: t("profiles.dbProfileRefresh.error"),
      errorCode: "",
      error: options.jobQueryError,
    };
  }
  if (state.notice) return { kind: "notice", scope: state.scope, refreshing: true, ...state.notice };
  return { kind: "syncing", scope: state.scope };
}

export function dbProfileListSyncProcessingLabel(job: SelectAiDbProfileRefreshJobData | null) {
  return job?.mode === "targeted"
    ? t("common.processing.dbProfileListDeltaSyncing")
    : t("common.processing.dbProfileListRefreshing");
}
