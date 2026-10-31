import type { ProfileSyncJobData, ProfileSyncJobStatus } from "./types";

export const PROFILE_SAVE_PROGRESS_STEP_IDS = [
  "save_profile",
  "sync_oracle_profile",
  "rebuild_agent_assets",
  "verify",
] as const;

export type ProfileSaveProgressStepId = (typeof PROFILE_SAVE_PROGRESS_STEP_IDS)[number];
export type ProfileSaveProgressStepStatus =
  | "pending"
  | "running"
  | "done"
  | "error"
  | "skipped";
export type ProfileSaveProgressStatus = ProfileSyncJobStatus | "submission_failed";

export interface ProfileSaveProgressStep {
  id: ProfileSaveProgressStepId;
  status: ProfileSaveProgressStepStatus;
}

export interface ProfileSaveProgressPresentation {
  active: boolean;
  status: ProfileSaveProgressStatus;
  steps: ProfileSaveProgressStep[];
}

function initialSteps(rebuildAgentAssets: boolean): ProfileSaveProgressStep[] {
  return [
    { id: "save_profile", status: "done" },
    { id: "sync_oracle_profile", status: "pending" },
    {
      id: "rebuild_agent_assets",
      status: rebuildAgentAssets ? "pending" : "skipped",
    },
    { id: "verify", status: "pending" },
  ];
}

function updateStep(
  steps: ProfileSaveProgressStep[],
  id: ProfileSaveProgressStepId,
  status: ProfileSaveProgressStepStatus,
) {
  return steps.map((step) => (step.id === id ? { ...step, status } : step));
}

function cancelledSteps(
  steps: ProfileSaveProgressStep[],
  job: ProfileSyncJobData,
) {
  let next = steps;
  next = updateStep(
    next,
    "sync_oracle_profile",
    job.oracle_result ? "done" : "skipped",
  );
  if (job.rebuild_agent_assets) {
    next = updateStep(next, "rebuild_agent_assets", job.agent_result ? "done" : "skipped");
  }
  return updateStep(next, "verify", "skipped");
}

/**
 * 保存APIと永続Oracle同期jobを、SQL生成と同じ4段階の表示モデルへ正規化する。
 * failed phase はterminal保存時に失われるため、永続化済みresultから失敗工程を復元する。
 */
export function profileSaveProgressPresentation(
  job: ProfileSyncJobData | null,
  options: {
    rebuildAgentAssets: boolean;
    submissionError?: string;
  },
): ProfileSaveProgressPresentation | null {
  const rebuildAgentAssets = job?.rebuild_agent_assets ?? options.rebuildAgentAssets;
  let steps = initialSteps(rebuildAgentAssets);

  if (!job) {
    if (!options.submissionError) return null;
    steps = updateStep(steps, "sync_oracle_profile", "error");
    return { active: false, status: "submission_failed", steps };
  }

  if (job.status === "queued") {
    return { active: true, status: job.status, steps };
  }

  if (job.status === "succeeded") {
    steps = updateStep(steps, "sync_oracle_profile", "done");
    if (rebuildAgentAssets) steps = updateStep(steps, "rebuild_agent_assets", "done");
    steps = updateStep(steps, "verify", "done");
    return { active: false, status: job.status, steps };
  }

  if (job.status === "failed") {
    // backend が失敗した工程を残していれば、それを正とする（結果の有無からの推定より確か）。
    if (job.failed_phase === "rebuilding_agent_assets" && rebuildAgentAssets) {
      steps = updateStep(steps, "sync_oracle_profile", "done");
      steps = updateStep(steps, "rebuild_agent_assets", "error");
      return { active: false, status: job.status, steps };
    }
    if (job.failed_phase === "verifying") {
      steps = updateStep(steps, "sync_oracle_profile", "done");
      if (rebuildAgentAssets) steps = updateStep(steps, "rebuild_agent_assets", "done");
      steps = updateStep(steps, "verify", "error");
      return { active: false, status: job.status, steps };
    }
    if (job.failed_phase === "queued" || job.failed_phase === "syncing_oracle_profile") {
      steps = updateStep(steps, "sync_oracle_profile", "error");
      return { active: false, status: job.status, steps };
    }
    if (!job.oracle_result) {
      steps = updateStep(steps, "sync_oracle_profile", "error");
    } else if (rebuildAgentAssets && !job.agent_result) {
      steps = updateStep(steps, "sync_oracle_profile", "done");
      steps = updateStep(steps, "rebuild_agent_assets", "error");
    } else {
      steps = updateStep(steps, "sync_oracle_profile", "done");
      if (rebuildAgentAssets) steps = updateStep(steps, "rebuild_agent_assets", "done");
      steps = updateStep(steps, "verify", "error");
    }
    return { active: false, status: job.status, steps };
  }

  if (job.status === "cancelled") {
    return {
      active: false,
      status: job.status,
      steps: cancelledSteps(steps, job),
    };
  }

  if (job.phase === "rebuilding_agent_assets") {
    steps = updateStep(steps, "sync_oracle_profile", "done");
    steps = updateStep(steps, "rebuild_agent_assets", "running");
  } else if (job.phase === "verifying") {
    steps = updateStep(steps, "sync_oracle_profile", "done");
    if (rebuildAgentAssets) steps = updateStep(steps, "rebuild_agent_assets", "done");
    steps = updateStep(steps, "verify", "running");
  } else {
    steps = updateStep(steps, "sync_oracle_profile", "running");
  }

  return { active: true, status: job.status, steps };
}
