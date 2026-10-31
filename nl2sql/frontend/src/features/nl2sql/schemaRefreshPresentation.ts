import { t } from "../../lib/i18n";

import type { SchemaRefreshJob } from "./types";

export function schemaRefreshJobIsActive(job: SchemaRefreshJob | null | undefined) {
  return job?.status === "pending" || job?.status === "running";
}

function schemaRefreshPhase(job: SchemaRefreshJob | null, starting: boolean, failed: boolean) {
  if (failed || job?.status === "error") return "error" as const;
  if (starting && !job) return "starting" as const;
  if (!job) return "queued" as const;
  if (job.status === "pending") return job.phase ?? "queued";
  if (job.status === "done") return "done" as const;
  return job.phase ?? "scanning";
}

export interface SchemaRefreshHeaderPresentation {
  label: string;
  announcementLabel: string;
  variant: "info" | "danger";
}

export function schemaRefreshHeaderPresentation(
  job: SchemaRefreshJob | null,
  options: { starting?: boolean; error?: boolean } = {},
): SchemaRefreshHeaderPresentation | null {
  const starting = options.starting ?? false;
  const failed = options.error ?? false;
  if (!starting && !failed && (!job || job.status === "done")) return null;

  const phase = schemaRefreshPhase(job, starting, failed);
  const phaseLabel = t(`common.schemaRefresh.phase.${phase}`);
  const progress =
    !failed && job && (job.total_objects ?? 0) > 0
      ? ` ${job.processed_objects ?? 0}/${job.total_objects}`
      : "";
  const key =
    job?.mode === "targeted"
      ? "common.schemaRefresh.status.targeted"
      : "common.schemaRefresh.status.full";

  return {
    label: t(key, { phase: phaseLabel, progress }),
    announcementLabel: t(key, { phase: phaseLabel, progress: "" }),
    variant: failed || job?.status === "error" ? "danger" : "info",
  };
}

export function schemaRefreshProcessingLabel(job: SchemaRefreshJob | null) {
  return job?.mode === "targeted"
    ? t("common.processing.schemaDeltaSyncing")
    : t("common.processing.schemaRefreshing");
}

export function schemaRefreshJobErrorMessage(job: SchemaRefreshJob | null) {
  if (!job?.error_code) return t("dataMgmt.schemaJob.error");
  return `${t("dataMgmt.schemaJob.error")} (${job.error_code})`;
}

/** job の失敗が「全体の再取得が必要」を意味するか（差分同期の不整合・対象を特定できない）。 */
export function schemaRefreshJobRequiresFull(job: SchemaRefreshJob | null | undefined) {
  if (!job) return false;
  return (
    Boolean(job.requires_full_refresh) ||
    job.error_code === "schema_refresh_full_required" ||
    job.error_code === "schema_refresh_target_unresolved"
  );
}

/** 全体の再取得を求める案内の文（理由のコードごと）。 */
export function schemaRefreshRequiredMessage(reasonCode = "") {
  if (reasonCode === "schema_refresh_target_unresolved") {
    return t("dataMgmt.schemaJob.targetUnresolved");
  }
  return t("dataMgmt.schemaJob.fullRequired");
}

/** 案内を出す場所。`inline` は起点の操作の結果の直下、`page` はページの先頭（結果の面を持たない操作）。 */
export type SchemaSyncScope = "inline" | "page";

/** 案内（Banner）の中身。warning は再取得を求める案内、danger は再取得・同期の失敗。 */
export interface SchemaSyncNoticeContent {
  severity: "warning" | "danger";
  /** 案内の見出し（何が起きたか）。無ければ本文だけ。API の失敗は要約を見出しにする。 */
  title?: string;
  message: string;
  /** 技術的な詳細（「詳細」に出すエラーコード）。 */
  errorCode: string;
  /** API の失敗（応答が届かなかった失敗を含む）。要約と「詳細」は `presentApiError` で作る。 */
  error?: unknown;
}

/**
 * 変更（DDL・取込・メタデータの適用など）の後の DB 構造の同期の状態（`useMutationSchemaSync` が持つ）。
 * - `notice`: 案内を出している（再取得を求める・失敗）。`baselineCompletedJobId` は案内を出した時点の
 *   完了済みの job（それより後に別の起点の全体の再取得が完了したら、案内を消す）。
 * - `job`: 再取得・差分同期の job を待つ。`notice` は案内の「DB 構造を再取得」から始めたときの案内
 *   （job の間も同じ案内を出し、その中のボタンの直下に進行を出す）。
 */
export type SchemaSyncState =
  | {
      kind: "notice";
      scope: SchemaSyncScope;
      notice: SchemaSyncNoticeContent;
      baselineCompletedJobId: string;
    }
  | {
      kind: "job";
      scope: SchemaSyncScope;
      jobId: string;
      notice: SchemaSyncNoticeContent | null;
    };

/** 案内の部品（`SchemaSyncNotice`）が描く形。 */
export type SchemaSyncView =
  | ({
      kind: "notice";
      scope: SchemaSyncScope;
      /** 案内のボタンから再取得を始めた（送信中または job の実行中）。進行を同じ Banner の中に出す。 */
      refreshing: boolean;
    } & SchemaSyncNoticeContent)
  /** 変更の後の差分同期（job）を待つ。案内は出さず、進行だけを出す。 */
  | { kind: "syncing"; scope: SchemaSyncScope };

/** 全体の再取得を求める案内（warning。SQL などの変更は成功しており、DB 構造の読み取りが古いだけ）。 */
export function schemaSyncRequiredNotice(reasonCode = ""): SchemaSyncNoticeContent {
  return { severity: "warning", message: schemaRefreshRequiredMessage(reasonCode), errorCode: "" };
}

/** job の失敗の案内。全体の再取得が要る失敗は再取得を求める案内、それ以外は danger。 */
export function schemaSyncJobErrorNotice(job: SchemaRefreshJob): SchemaSyncNoticeContent {
  if (schemaRefreshJobRequiresFull(job)) return schemaSyncRequiredNotice(job.error_code);
  return { severity: "danger", message: t("dataMgmt.schemaJob.error"), errorCode: job.error_code };
}

/**
 * 同期の状態と job から、案内の形を決める（描くものが無ければ null）。
 * job の完了・別の起点の全体の再取得の完了（`completedJob`）では案内を消す（再取得を求め続けない。#2031）。
 */
export function schemaSyncView(
  state: SchemaSyncState | null,
  options: {
    job: SchemaRefreshJob | null | undefined;
    jobQueryError?: unknown;
    completedJob: SchemaRefreshJob | null;
    /** 案内のボタンの再取得の送信中（`startingOrigin` が案内の起点）。 */
    noticeStarting: boolean;
  },
): SchemaSyncView | null {
  if (!state) return null;
  if (state.kind === "notice") {
    const completed = options.completedJob;
    const resolvedElsewhere =
      completed?.status === "done" &&
      completed.mode !== "targeted" &&
      completed.job_id !== state.baselineCompletedJobId;
    if (resolvedElsewhere && !options.noticeStarting) return null;
    return { kind: "notice", scope: state.scope, refreshing: options.noticeStarting, ...state.notice };
  }
  const job = options.job && options.job.job_id === state.jobId ? options.job : null;
  if (job?.status === "done") return null;
  if (job?.status === "error") {
    return { kind: "notice", scope: state.scope, refreshing: false, ...schemaSyncJobErrorNotice(job) };
  }
  if (options.jobQueryError) {
    return {
      kind: "notice",
      scope: state.scope,
      refreshing: false,
      severity: "danger",
      message: t("dataMgmt.schemaJob.error"),
      errorCode: "",
      error: options.jobQueryError,
    };
  }
  if (state.notice) return { kind: "notice", scope: state.scope, refreshing: true, ...state.notice };
  return { kind: "syncing", scope: state.scope };
}
