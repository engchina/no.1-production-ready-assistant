import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  dbProfileListJobErrorNotice,
  dbProfileListRequiredNotice,
  dbProfileListSyncView,
  type DbProfileListSyncState,
} from "../src/features/nl2sql/dbProfileListSyncPresentation";
import type { SelectAiDbProfileRefreshJobData } from "../src/features/nl2sql/types";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

function job(overrides: Partial<SelectAiDbProfileRefreshJobData> = {}): SelectAiDbProfileRefreshJobData {
  return {
    job_id: "db-profile-refresh-1",
    status: "running",
    mode: "full",
    source: "manual",
    target_profiles: [],
    requires_full_refresh: false,
    phase: "fetching",
    created_at: "2026-10-15T00:00:00Z",
    started_at: "2026-10-15T00:00:01Z",
    total_profiles: 0,
    processed_profiles: 0,
    scanned_profiles: 0,
    changed_profiles: 0,
    deleted_profiles: 0,
    error_code: "",
    error_message: "",
    ...overrides,
  } as SelectAiDbProfileRefreshJobData;
}

const requiredState: DbProfileListSyncState = {
  kind: "notice",
  scope: "inline",
  notice: dbProfileListRequiredNotice("profile_list_refresh_target_unresolved"),
};

test("DB Profile 一覧の再取得を求める案内は warning で、押した案内の中で refreshing になり完了で消える（#2034）", () => {
  const idle = dbProfileListSyncView(requiredState, { job: null, noticeStarting: false });
  assert.equal(idle?.kind, "notice");
  assert.equal(idle?.kind === "notice" && idle.severity, "warning");
  assert.equal(idle?.kind === "notice" && idle.refreshing, false);
  assert.match(idle?.kind === "notice" ? idle.message : "", /DB Profile 一覧を再取得してください/u);

  const starting = dbProfileListSyncView(requiredState, { job: null, noticeStarting: true });
  assert.equal(starting?.kind === "notice" && starting.refreshing, true);

  const running: DbProfileListSyncState = {
    kind: "job",
    scope: "inline",
    jobId: "db-profile-refresh-1",
    notice: requiredState.notice,
  };
  const inProgress = dbProfileListSyncView(running, { job: job(), noticeStarting: false });
  assert.equal(inProgress?.kind === "notice" && inProgress.refreshing, true);

  // 完了したら、再取得を求める案内を残さない。
  assert.equal(dbProfileListSyncView(running, { job: job({ status: "done" }), noticeStarting: false }), null);
});

test("差分同期の job は案内を出さず進行だけ、失敗は同じ場所に出す（#2034）", () => {
  const syncing: DbProfileListSyncState = {
    kind: "job",
    scope: "inline",
    jobId: "db-profile-refresh-1",
    notice: null,
  };
  assert.deepEqual(dbProfileListSyncView(syncing, { job: job({ mode: "targeted" }), noticeStarting: false }), {
    kind: "syncing",
    scope: "inline",
  });

  // 差分同期が全体の再取得を求めて失敗したら、warning の案内（再取得のボタン）にする。
  const fullRequired = dbProfileListSyncView(syncing, {
    job: job({ status: "error", requires_full_refresh: true, error_code: "profile_list_refresh_full_required" }),
    noticeStarting: false,
  });
  assert.equal(fullRequired?.kind === "notice" && fullRequired.severity, "warning");

  // それ以外の失敗は danger で、エラーコードは「詳細」に出す。
  const failed = dbProfileListJobErrorNotice(job({ status: "error", error_code: "profile_list_refresh_failed" }));
  assert.equal(failed.severity, "danger");
  assert.equal(failed.errorCode, "profile_list_refresh_failed");

  const queryFailed = dbProfileListSyncView(syncing, {
    job: null,
    jobQueryError: new Error("network"),
    noticeStarting: false,
  });
  assert.equal(queryFailed?.kind === "notice" && queryFailed.severity, "danger");
});

test("DB Profile 一覧の再取得の案内と進行は共有の hook と部品にまとめ、警告の文で判定しない（#2034）", () => {
  const dataManagement = source("../src/features/nl2sql/pages/DataManagementPage.tsx");
  const profileManagement = source("../src/features/nl2sql/pages/ProfileManagementPage.tsx");
  for (const page of [dataManagement, profileManagement]) {
    assert.match(page, /useDbProfileListSync\(/u);
    assert.match(page, /<DbProfileListSyncNotice/u);
    assert.doesNotMatch(page, /useStartSelectAiDbProfileRefresh/u);
    // 失敗の Toast と案内を重ねない。
    assert.doesNotMatch(page, /toast\.error\(dbProfileRefresh/u);
  }
  // 一覧の再取得の要否は構造化した項目だけで決める（文言の一致で warnings を除かない）。
  assert.doesNotMatch(dataManagement, /isSelectAiDbProfileRefreshWarning|read model が未初期化/u);
  assert.match(dataManagement, /profile_list_refresh_required/u);
  // 反映の結果の直下に同期の案内を出す。
  assert.match(profileManagement, /testId="profile-db-profile-sync-notice"/u);
});

test("案内の中のボタンの進行は同じ案内の中に出し、ページの先頭に重ねない（#2034）", () => {
  const ontologyPage = source("../src/features/nl2sql/pages/OntologyBuildPage.tsx");
  const buildSection = source("../src/features/nl2sql/ontology/OntologyBuildSection.tsx");
  const playground = source("../src/features/nl2sql/ontology/OntologyQueryPlayground.tsx");
  assert.match(ontologyPage, /ONTOLOGY_NOTICE_SCHEMA_REFRESH_ORIGINS/u);
  assert.match(ontologyPage, /sharedSchemaRefresh\.jobOrigin/u);
  assert.doesNotMatch(ontologyPage, /onRefreshSchema=/u);
  for (const component of [buildSection, playground]) {
    assert.match(component, /useMutationSchemaSync\(\{[\s\S]*required:/u);
    assert.match(component, /<SchemaSyncNotice/u);
    assert.match(component, /requiredResolved/u);
  }
  // 構築の失敗の警告は案内の中に 1 か所だけ出す。
  assert.match(buildSection, /!schemaScopeFailure \? buildWarningsDisclosure/u);

  const coordinator = source("../src/features/nl2sql/SchemaRefreshCoordinator.tsx");
  assert.match(coordinator, /setJobOrigin\(origin\)/u);

  // 取得の失敗の案内の「再試行」は、押したボタンだけを回し、進行を案内の中に出す。
  const pageNotice = source("../src/components/page-notice.tsx");
  assert.match(pageNotice, /export function RetryPageNotice/u);
  assert.match(pageNotice, /progress=\{progress\}/u);
  for (const path of [
    "../src/features/nl2sql/pages/TableManagementPage.tsx",
    "../src/features/nl2sql/pages/ViewManagementPage.tsx",
    "../src/features/nl2sql/pages/MetadataSqlManagementPage.tsx",
  ]) {
    const page = source(path);
    assert.match(page, /<RetryPageNotice[\s\S]*retrying=\{noticeRetry\.pending\}/u, path);
  }
  const feedback = source("../src/features/nl2sql/pages/FeedbackManagementPage.tsx");
  assert.match(feedback, /if \(origin === "header"\) setMessage\(""\)/u);
  const learning = source("../src/features/nl2sql/pages/QuestionLearningPage.tsx");
  assert.match(learning, /if \(origin === "header"\) setMessage\(""\)/u);
  const deepSec = source("../src/features/security/SecurityDeepSecPage.tsx");
  assert.match(deepSec, /loadStatus\(\{ keepError: true \}\)/u);

  // サンプルデータの取込の失敗は押した案内の中だけに出す（Toast とページの先頭に重ねない）。
  const workbench = source("../src/features/nl2sql/Nl2SqlWorkbench.tsx");
  const importSample = workbench.slice(
    workbench.indexOf("const importSampleData = useCallback("),
    workbench.indexOf("const sampleImportProgress"),
  );
  assert.doesNotMatch(importSample, /toast\.error|setPageError\(\{ source: "sample-import"/u);
});
