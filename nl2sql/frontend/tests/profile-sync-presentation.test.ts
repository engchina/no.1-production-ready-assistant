import assert from "node:assert/strict";
import test from "node:test";

import {
  profileSaveProgressPresentation,
  type ProfileSaveProgressStepId,
  type ProfileSaveProgressStepStatus,
} from "../src/features/nl2sql/profileSyncPresentation";
import type {
  AssetRefreshData,
  ProfileSyncJobData,
  SelectAiDbProfileMutationData,
} from "../src/features/nl2sql/types";

const oracleResult: SelectAiDbProfileMutationData = {
  runtime: "oracle",
  executed: true,
  status: "saved",
  profile_name: "NL2SQL_SALES_PROFILE",
  original_name: "",
  ddl: [],
  warnings: [],
  engine_meta: {},
};

const agentResult: AssetRefreshData = {
  engine: "select_ai_agent",
  refreshed: true,
  status: "ready",
  refreshed_at: "2026-08-29T00:00:03Z",
  profile_name: "NL2SQL_SALES_AGENT_PROFILE",
  team_name: "NL2SQL_SALES_TEAM",
  warning: "",
  asset_names: { team: "NL2SQL_SALES_TEAM" },
  engine_meta: {},
};

function job(overrides: Partial<ProfileSyncJobData> = {}): ProfileSyncJobData {
  return {
    job_id: "profile-sync-1",
    profile_id: "sales",
    profile_etag: "etag-sales",
    status: "running",
    phase: "syncing_oracle_profile",
    rebuild_agent_assets: true,
    error_code: "",
    error_message_ja: "",
    created_at: "2026-08-29T00:00:00Z",
    ...overrides,
  };
}

function statuses(
  presentation: ReturnType<typeof profileSaveProgressPresentation>,
): Record<ProfileSaveProgressStepId, ProfileSaveProgressStepStatus> {
  assert.ok(presentation);
  return Object.fromEntries(
    presentation.steps.map((step) => [step.id, step.status]),
  ) as Record<ProfileSaveProgressStepId, ProfileSaveProgressStepStatus>;
}

test("profile save progress stays hidden before save and exposes submission failure", () => {
  assert.equal(
    profileSaveProgressPresentation(null, { rebuildAgentAssets: false }),
    null,
  );

  const failed = profileSaveProgressPresentation(null, {
    rebuildAgentAssets: true,
    submissionError: "Oracle job submit failed",
  });
  assert.equal(failed?.status, "submission_failed");
  assert.equal(failed?.active, false);
  assert.deepEqual(statuses(failed), {
    save_profile: "done",
    sync_oracle_profile: "error",
    rebuild_agent_assets: "pending",
    verify: "pending",
  });
});

test("profile save progress maps queued and every running phase", () => {
  const queued = profileSaveProgressPresentation(
    job({ status: "queued", phase: "queued", rebuild_agent_assets: false }),
    { rebuildAgentAssets: true },
  );
  assert.equal(queued?.active, true);
  assert.deepEqual(statuses(queued), {
    save_profile: "done",
    sync_oracle_profile: "pending",
    rebuild_agent_assets: "skipped",
    verify: "pending",
  });

  assert.deepEqual(
    statuses(profileSaveProgressPresentation(job(), { rebuildAgentAssets: false })),
    {
      save_profile: "done",
      sync_oracle_profile: "running",
      rebuild_agent_assets: "pending",
      verify: "pending",
    },
  );
  assert.deepEqual(
    statuses(
      profileSaveProgressPresentation(
        job({ phase: "rebuilding_agent_assets", oracle_result: oracleResult }),
        { rebuildAgentAssets: false },
      ),
    ),
    {
      save_profile: "done",
      sync_oracle_profile: "done",
      rebuild_agent_assets: "running",
      verify: "pending",
    },
  );
  assert.deepEqual(
    statuses(
      profileSaveProgressPresentation(
        job({ phase: "verifying", oracle_result: oracleResult, agent_result: agentResult }),
        { rebuildAgentAssets: false },
      ),
    ),
    {
      save_profile: "done",
      sync_oracle_profile: "done",
      rebuild_agent_assets: "done",
      verify: "running",
    },
  );
});

test("profile save progress completes or skips the Agent step from the job snapshot", () => {
  const withAgent = profileSaveProgressPresentation(
    job({
      status: "succeeded",
      phase: "succeeded",
      oracle_result: oracleResult,
      agent_result: agentResult,
    }),
    { rebuildAgentAssets: false },
  );
  assert.deepEqual(statuses(withAgent), {
    save_profile: "done",
    sync_oracle_profile: "done",
    rebuild_agent_assets: "done",
    verify: "done",
  });

  const withoutAgent = profileSaveProgressPresentation(
    job({
      status: "succeeded",
      phase: "succeeded",
      rebuild_agent_assets: false,
      oracle_result: oracleResult,
    }),
    { rebuildAgentAssets: true },
  );
  assert.deepEqual(statuses(withoutAgent), {
    save_profile: "done",
    sync_oracle_profile: "done",
    rebuild_agent_assets: "skipped",
    verify: "done",
  });
});

test("profile save progress restores Oracle, Agent, and verification failures", () => {
  const oracleFailure = profileSaveProgressPresentation(
    job({ status: "failed", phase: "failed" }),
    { rebuildAgentAssets: true },
  );
  assert.deepEqual(statuses(oracleFailure), {
    save_profile: "done",
    sync_oracle_profile: "error",
    rebuild_agent_assets: "pending",
    verify: "pending",
  });

  const agentFailure = profileSaveProgressPresentation(
    job({ status: "failed", phase: "failed", oracle_result: oracleResult }),
    { rebuildAgentAssets: true },
  );
  assert.deepEqual(statuses(agentFailure), {
    save_profile: "done",
    sync_oracle_profile: "done",
    rebuild_agent_assets: "error",
    verify: "pending",
  });

  const verifyFailure = profileSaveProgressPresentation(
    job({
      status: "failed",
      phase: "failed",
      oracle_result: oracleResult,
      agent_result: agentResult,
    }),
    { rebuildAgentAssets: true },
  );
  assert.deepEqual(statuses(verifyFailure), {
    save_profile: "done",
    sync_oracle_profile: "done",
    rebuild_agent_assets: "done",
    verify: "error",
  });
});

test("profile save progress marks unfinished cancellation steps skipped and resets on retry", () => {
  const cancelled = profileSaveProgressPresentation(
    job({
      status: "cancelled",
      phase: "cancelled",
      oracle_result: oracleResult,
    }),
    { rebuildAgentAssets: true },
  );
  assert.deepEqual(statuses(cancelled), {
    save_profile: "done",
    sync_oracle_profile: "done",
    rebuild_agent_assets: "skipped",
    verify: "skipped",
  });

  const retry = profileSaveProgressPresentation(
    job({
      job_id: "profile-sync-retry",
      status: "queued",
      phase: "queued",
      retry_of_job_id: "profile-sync-1",
    }),
    { rebuildAgentAssets: false },
  );
  assert.equal(retry?.status, "queued");
  assert.equal(retry?.active, true);
  assert.deepEqual(statuses(retry), {
    save_profile: "done",
    sync_oracle_profile: "pending",
    rebuild_agent_assets: "pending",
    verify: "pending",
  });
});

test("failed_phase identifies the failed step even when results alone are ambiguous", () => {
  // 名称の変更の後始末（旧名の印の消去）で失敗した: Oracle は反映済みだが Agent は未着手。
  assert.deepEqual(
    statuses(
      profileSaveProgressPresentation(
        job({
          status: "failed",
          phase: "failed",
          failed_phase: "syncing_oracle_profile",
          oracle_result: oracleResult,
        }),
        { rebuildAgentAssets: true },
      ),
    ),
    {
      save_profile: "done",
      sync_oracle_profile: "error",
      rebuild_agent_assets: "pending",
      verify: "pending",
    },
  );
  assert.deepEqual(
    statuses(
      profileSaveProgressPresentation(
        job({
          status: "failed",
          phase: "failed",
          failed_phase: "rebuilding_agent_assets",
          oracle_result: oracleResult,
        }),
        { rebuildAgentAssets: true },
      ),
    ),
    {
      save_profile: "done",
      sync_oracle_profile: "done",
      rebuild_agent_assets: "error",
      verify: "pending",
    },
  );
  assert.deepEqual(
    statuses(
      profileSaveProgressPresentation(
        job({
          status: "failed",
          phase: "failed",
          failed_phase: "verifying",
          rebuild_agent_assets: false,
          oracle_result: oracleResult,
        }),
        { rebuildAgentAssets: false },
      ),
    ),
    {
      save_profile: "done",
      sync_oracle_profile: "done",
      rebuild_agent_assets: "skipped",
      verify: "error",
    },
  );
});
