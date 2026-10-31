import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  schemaRefreshHeaderPresentation,
  schemaRefreshJobIsActive,
  schemaRefreshProcessingLabel,
  schemaSyncRequiredNotice,
  schemaSyncView,
  type SchemaSyncState,
} from "../src/features/nl2sql/schemaRefreshPresentation";
import type { SchemaRefreshJob } from "../src/features/nl2sql/types";

function job(overrides: Partial<SchemaRefreshJob> = {}): SchemaRefreshJob {
  return {
    job_id: "schema-refresh-1",
    status: "running",
    mode: "full",
    created_at: "2026-08-29T00:00:00Z",
    started_at: "2026-08-29T00:00:01Z",
    phase: "persisting",
    processed_objects: 218,
    total_objects: 218,
    scanned_objects: 218,
    changed_objects: 12,
    deleted_objects: 0,
    catalog_version: 4,
    error_code: "",
    ...overrides,
  };
}

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("schema refresh formatter centralizes phase, mode, and known progress", () => {
  assert.deepEqual(schemaRefreshHeaderPresentation(job()), {
    label: "DB 構造再取得: 保存中 218/218",
    announcementLabel: "DB 構造再取得: 保存中",
    variant: "info",
  });
  assert.deepEqual(
    schemaRefreshHeaderPresentation(
      job({ mode: "targeted", phase: "fetching", processed_objects: 3, total_objects: 10 }),
    ),
    {
      label: "DB 構造差分同期: 構造取得中 3/10",
      announcementLabel: "DB 構造差分同期: 構造取得中",
      variant: "info",
    },
  );
  assert.equal(schemaRefreshProcessingLabel(job()), "DB 構造を再取得しています");
  assert.equal(
    schemaRefreshProcessingLabel(job({ mode: "targeted" })),
    "DB 構造の差分を同期しています",
  );
});

test("schema refresh formatter never invents unknown totals and clears success history", () => {
  const unknown = schemaRefreshHeaderPresentation(
    job({ phase: "scanning", processed_objects: 7, total_objects: 0 }),
  );
  assert.equal(unknown?.label, "DB 構造再取得: 変更確認中");
  assert.doesNotMatch(unknown?.label ?? "", /7\/0/u);
  assert.equal(schemaRefreshHeaderPresentation(job({ status: "done", phase: "done" })), null);
  assert.deepEqual(schemaRefreshHeaderPresentation(null, { starting: true }), {
    label: "DB 構造再取得: 開始中",
    announcementLabel: "DB 構造再取得: 開始中",
    variant: "info",
  });
  assert.equal(schemaRefreshJobIsActive(job({ status: "pending" })), true);
  assert.equal(schemaRefreshJobIsActive(job({ status: "error" })), false);
});

test("active discovery and durable job hooks use focus recovery and one-second active polling", () => {
  const hooks = source("../src/features/nl2sql/incrementalQueries.ts");
  assert.match(hooks, /apiPost<SchemaRefreshJob>\("\/api\/schema\/refresh-jobs"/u);
  assert.match(hooks, /\/api\/schema\/refresh-jobs\/active/u);
  assert.doesNotMatch(hooks, /\/api\/schema\/refresh["']/u);
  assert.doesNotMatch(hooks, /legacyCatalogOverride/u);
  assert.match(hooks, /refetchOnMount: "always"/u);
  assert.match(hooks, /refetchOnReconnect: "always"/u);
  assert.match(hooks, /refetchOnWindowFocus: "always"/u);
  assert.match(
    hooks,
    /return status === "done" \|\| status === "error" \? false : 1_000/u,
  );
});

test("coordinator owns start/track, discovery, terminal notification, and invalidation once", () => {
  const coordinator = source("../src/features/nl2sql/SchemaRefreshCoordinator.tsx");
  assert.match(coordinator, /useActiveSchemaRefreshJob\(discoveryEnabled\)/u);
  assert.match(coordinator, /const track = useCallback/u);
  assert.match(coordinator, /const start = useCallback/u);
  assert.match(coordinator, /terminalKey !== reportedTerminal/u);
  assert.match(coordinator, /invalidateQueries\(\{ queryKey: \["schema"\] \}\)/u);
  assert.match(coordinator, /invalidateQueries\(\{ queryKey: \["nl2sql", "db-admin"\] \}\)/u);
  assert.match(coordinator, /activeSchemaRefreshJob[\s\S]{0,120}active_job: null/u);
  assert.match(coordinator, /toast\.success\(t\("common\.action\.schemaRefreshed"\)\)/u);
  // 失敗は各ページの固定面(context の error)を正本とし、Toast と二重表示しない
  // (messaging spec §0.6)。coordinator は toast.error を呼ばない。
  assert.match(coordinator, /setError\(message\)/u);
  assert.match(coordinator, /setError\(schemaRefreshJobErrorMessage\(job\)\)/u);
  assert.doesNotMatch(coordinator, /toast\.error/u);
  assert.match(coordinator, /Boolean\(trackedJobId\) && !job/u);
  assert.doesNotMatch(coordinator, /:query-error/u);
});

test("all ten DB refresh routes use the shared header and workspace feedback", () => {
  const routeSources = [
    source("../src/features/nl2sql/Nl2SqlWorkbench.tsx"),
    source("../src/features/nl2sql/pages/TableManagementPage.tsx"),
    source("../src/features/nl2sql/pages/ViewManagementPage.tsx"),
    source("../src/features/nl2sql/pages/DataManagementPage.tsx"),
    source("../src/features/nl2sql/pages/SampleDataPage.tsx"),
    source("../src/features/nl2sql/pages/AdminSqlPage.tsx"),
    source("../src/features/nl2sql/pages/ProfileManagementPage.tsx"),
    source("../src/features/nl2sql/pages/OntologyBuildPage.tsx"),
  ];
  const metadata = source("../src/features/nl2sql/pages/MetadataSqlManagementPage.tsx");

  for (const routeSource of routeSources) {
    assert.match(routeSource, /SchemaRefreshHeaderStatus/u);
    assert.match(routeSource, /useSchemaRefreshCoordinator/u);
  }
  for (const routeSource of routeSources.slice(1)) {
    assert.match(routeSource, /SchemaRefreshProcessing/u);
  }
  assert.match(metadata, /export function CommentManagementPage/u);
  assert.match(metadata, /export function AnnotationManagementPage/u);
  assert.match(metadata, /SchemaRefreshHeaderStatus/u);
  assert.match(metadata, /SchemaRefreshProcessing/u);
});

test("shared schema feedback limits live announcements to the phase badge", () => {
  const feedback = source("../src/features/nl2sql/components/SchemaRefreshFeedback.tsx");
  const header = source("../src/components/PageHeaderStatusBadge.tsx");
  const processing = source("../../../platform/packages/ui/src/components/feedback/processing-state.tsx");

  assert.match(header, /role="status"[\s\S]{0,80}aria-live="polite"/u);
  assert.match(header, /<span aria-hidden="true">[\s\S]{0,120}className="sr-only"/u);
  assert.match(feedback, /announceActivity=\{false\}/u);
  assert.match(feedback, /announceSlow=\{false\}/u);
  // 既定は、押したボタンが送信中（loading）の間だけ "none"、job の間は "spinner"（durable job の規則。#821）。
  assert.match(feedback, /activityIcon \?\? \(startingOrigin \? "none" : "spinner"\)/u);
  assert.doesNotMatch(feedback, /activityIcon = "none"/u);
  assert.match(processing, /role="timer"/u);
  assert.match(processing, /aria-live="off"/u);
});

test("DDL mutation pages stay non-blocking and hand auto-refresh jobs to the coordinator", () => {
  const ddlSources = [
    source("../src/features/nl2sql/pages/TableManagementPage.tsx"),
    source("../src/features/nl2sql/pages/ViewManagementPage.tsx"),
    source("../src/features/nl2sql/pages/MetadataSqlManagementPage.tsx"),
  ];

  for (const page of ddlSources) {
    assert.doesNotMatch(page, /reloadAfterMutation\s*=\s*async/u);
    assert.doesNotMatch(page, /waitForSchemaRefreshJob/u);
    assert.doesNotMatch(page, /:query-error/u);
    // 同期の状態は共有の hook が持つ（画面ごとに状態と関数を複製しない。#2031）。
    assert.match(page, /useMutationSchemaSync\(/u);
    assert.match(page, /[sS]chemaSync\.track\(/u);
    assert.doesNotMatch(page, /setSchemaRefreshJobId\(/u);
    assert.doesNotMatch(page, /function schemaRefreshRequiresFull/u);
    assert.match(page, /void refreshObjects\(\)/u);
  }
});

test("view create renders the shared schema progress in its task panel", () => {
  const view = source("../src/features/nl2sql/pages/ViewManagementPage.tsx");
  assert.match(view, /footerProcessing=/u);
  assert.match(view, /processingTestId="view-create-schema-refresh-processing"/u);
});

test("「スキーマを更新」は押したボタンだけを送信の間 loading にし、job の間は回さない（#821）", () => {
  const pages = [
    "../src/features/nl2sql/Nl2SqlWorkbench.tsx",
    "../src/features/nl2sql/components/SchemaReferencePanel.tsx",
    "../src/features/nl2sql/pages/AdminSqlPage.tsx",
    "../src/features/nl2sql/pages/DataManagementPage.tsx",
    "../src/features/nl2sql/pages/MetadataSqlManagementPage.tsx",
    "../src/features/nl2sql/pages/OntologyBuildPage.tsx",
    "../src/features/nl2sql/pages/ProfileManagementPage.tsx",
    "../src/features/nl2sql/pages/SampleDataPage.tsx",
    "../src/features/nl2sql/pages/TableManagementPage.tsx",
    "../src/features/nl2sql/pages/ViewManagementPage.tsx",
  ];
  for (const path of pages) {
    const page = source(path);
    // job の間ずっと true の値・どのボタンが押したかを区別しない値を、ボタンの loading に渡さない。
    assert.doesNotMatch(page, /loading(?::|=\{)\s*(?:schemaRefreshing|refreshing)\b/u, path);
    assert.doesNotMatch(page, /loading(?::|=\{)\s*(?:sharedSchemaRefresh|schemaRefresh)\.(?:isRefreshing|isStarting)/u, path);
    assert.doesNotMatch(page, /refreshingSchema=\{refreshing\b/u, path);
  }
  const coordinator = source("../src/features/nl2sql/SchemaRefreshCoordinator.tsx");
  assert.match(coordinator, /const startingOrigin = isStarting \? startOrigin : ""/u);
});

const unresolvedState: SchemaSyncState = {
  kind: "notice",
  scope: "inline",
  notice: schemaSyncRequiredNotice("schema_refresh_target_unresolved"),
  baselineCompletedJobId: "done-before",
};

test("再取得を求める案内は warning で、案内のボタンの送信中と job の間は refreshing（#2031）", () => {
  const idle = schemaSyncView(unresolvedState, { job: null, completedJob: null, noticeStarting: false });
  assert.equal(idle?.kind, "notice");
  assert.equal(idle?.kind === "notice" && idle.severity, "warning");
  assert.equal(idle?.kind === "notice" && idle.refreshing, false);
  assert.match(idle?.kind === "notice" ? idle.message : "", /DB 構造を再取得してください/u);

  const starting = schemaSyncView(unresolvedState, { job: null, completedJob: null, noticeStarting: true });
  assert.equal(starting?.kind === "notice" && starting.refreshing, true);

  const running: SchemaSyncState = {
    kind: "job",
    scope: "inline",
    jobId: "schema-refresh-1",
    notice: schemaSyncRequiredNotice("schema_refresh_target_unresolved"),
  };
  const inProgress = schemaSyncView(running, { job: job(), completedJob: null, noticeStarting: false });
  assert.equal(inProgress?.kind === "notice" && inProgress.refreshing, true);
  assert.equal(inProgress?.kind === "notice" && inProgress.severity, "warning");
});

test("再取得が完了したら案内を消し、別の起点の全体の再取得の完了でも消す（#2031）", () => {
  const running: SchemaSyncState = {
    kind: "job",
    scope: "inline",
    jobId: "schema-refresh-1",
    notice: schemaSyncRequiredNotice(),
  };
  assert.equal(
    schemaSyncView(running, { job: job({ status: "done", phase: "done" }), completedJob: null, noticeStarting: false }),
    null,
  );
  // 案内を出した後に、ヘッダーなど別の起点の全体の再取得が完了した。
  assert.equal(
    schemaSyncView(unresolvedState, {
      job: null,
      completedJob: job({ job_id: "header-full", status: "done", mode: "full" }),
      noticeStarting: false,
    }),
    null,
  );
  // 案内を出す前に完了していた job・差分同期の完了では消さない。
  assert.notEqual(
    schemaSyncView(unresolvedState, {
      job: null,
      completedJob: job({ job_id: "done-before", status: "done", mode: "full" }),
      noticeStarting: false,
    }),
    null,
  );
  assert.notEqual(
    schemaSyncView(unresolvedState, {
      job: null,
      completedJob: job({ job_id: "targeted", status: "done", mode: "targeted" }),
      noticeStarting: false,
    }),
    null,
  );
});

test("再取得の失敗は同じ場所の danger、差分同期の不整合は再取得を求める warning（#2031）", () => {
  const running: SchemaSyncState = { kind: "job", scope: "inline", jobId: "schema-refresh-1", notice: null };
  assert.deepEqual(
    schemaSyncView(running, { job: job(), completedJob: null, noticeStarting: false }),
    { kind: "syncing", scope: "inline" },
  );
  const failed = schemaSyncView(running, {
    job: job({ status: "error", error_code: "schema_refresh_failed" }),
    completedJob: null,
    noticeStarting: false,
  });
  assert.equal(failed?.kind === "notice" && failed.severity, "danger");
  assert.equal(failed?.kind === "notice" && failed.errorCode, "schema_refresh_failed");
  assert.doesNotMatch(failed?.kind === "notice" ? failed.message : "", /schema_refresh_failed/u);
  const needsFull = schemaSyncView(running, {
    job: job({ status: "error", mode: "targeted", error_code: "schema_refresh_full_required" }),
    completedJob: null,
    noticeStarting: false,
  });
  assert.equal(needsFull?.kind === "notice" && needsFull.severity, "warning");
  assert.match(needsFull?.kind === "notice" ? needsFull.message : "", /不整合/u);
});

test("案内のボタンの再取得は開始の Toast を出さず、進行を同じ Banner の中に出す（#2031）", () => {
  const hook = source("../src/features/nl2sql/useMutationSchemaSync.ts");
  const feedback = source("../src/features/nl2sql/components/SchemaRefreshFeedback.tsx");
  const coordinator = source("../src/features/nl2sql/SchemaRefreshCoordinator.tsx");
  assert.match(hook, /start\(origin, \{ announce: notice === null \}\)/u);
  assert.match(coordinator, /options\.announce !== false/u);
  assert.match(feedback, /progress=\{/u);
  const admin = source("../src/features/nl2sql/pages/AdminSqlPage.tsx");
  assert.doesNotMatch(admin, /PageNotice/u);
  assert.match(admin, /<SchemaSyncNotice/u);
});
