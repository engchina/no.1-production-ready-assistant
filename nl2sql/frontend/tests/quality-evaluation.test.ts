import assert from "node:assert/strict";
import test from "node:test";

import {
  qualityEvaluationAttemptCount,
  qualityEvaluationAttemptTimedOut,
  qualityEvaluationLeaseExpired,
  qualityEvaluationPollingInterval,
  toggleQualityEvaluationEngine,
  validateQualityEvaluationInput,
} from "../src/features/nl2sql/qualityEvaluationLogic.ts";
import type { QualityEvaluationEngineCapability } from "../src/features/nl2sql/types.ts";

const capabilities: QualityEvaluationEngineCapability[] = [
  { engine: "select_ai", label: "Select AI", available: true, reason: "" },
  {
    engine: "select_ai_agent",
    label: "Select AI Agent",
    available: false,
    reason: "not configured",
  },
  {
    engine: "enterprise_ai_direct",
    label: "Enterprise AI Direct",
    available: true,
    reason: "",
  },
];

test("quality evaluation form requires profile, xlsx, ready engine and repeat 1-10", () => {
  const errors = validateQualityEvaluationInput({
    profileId: "",
    file: { name: "cases.csv", size: 12 },
    engines: ["select_ai_agent"],
    repeatCount: 11,
    maxFileBytes: 10,
    capabilities,
  });
  assert.deepEqual(errors, {
    profile: "profile_required",
    file: "file_extension",
    engines: "engine_required",
    repeat: "repeat_range",
  });
});

test("quality evaluation form accepts repeat boundary 1 and 10", () => {
  for (const repeatCount of [1, 10]) {
    assert.deepEqual(
      validateQualityEvaluationInput({
        profileId: "default",
        file: { name: "cases.XLSX", size: 10 },
        engines: ["select_ai", "enterprise_ai_direct"],
        repeatCount,
        maxFileBytes: 10,
        capabilities,
      }),
      {}
    );
  }
});

test("engine selection toggles independently and attempt estimate multiplies all dimensions", () => {
  const selected = toggleQualityEvaluationEngine([], "select_ai");
  assert.deepEqual(toggleQualityEvaluationEngine(selected, "enterprise_ai_direct"), [
    "select_ai",
    "enterprise_ai_direct",
  ]);
  assert.deepEqual(toggleQualityEvaluationEngine(selected, "select_ai"), []);
  assert.equal(qualityEvaluationAttemptCount(12, 2, 3), 72);
});

test("polling continues only for pending and running jobs", () => {
  assert.equal(qualityEvaluationPollingInterval("pending"), 1_500);
  assert.equal(qualityEvaluationPollingInterval("running"), 1_500);
  assert.equal(qualityEvaluationPollingInterval("completed_with_errors"), false);
  assert.equal(qualityEvaluationPollingInterval("failed"), false);
  assert.equal(qualityEvaluationPollingInterval("cancelled"), false);
});

test("stale helpers detect expired quality evaluation attempt and lease", () => {
  const now = Date.parse("2026-07-22T08:06:00Z");
  assert.equal(
    qualityEvaluationAttemptTimedOut(
      {
        status: "running",
        current_attempt_started_at: "2026-07-22T08:00:00Z",
        attempt_timeout_seconds: 300,
      },
      now
    ),
    true
  );
  assert.equal(
    qualityEvaluationAttemptTimedOut(
      {
        status: "running",
        current_attempt_started_at: "2026-07-22T08:02:00Z",
        attempt_timeout_seconds: 300,
      },
      now
    ),
    false
  );
  assert.equal(
    qualityEvaluationLeaseExpired(
      { status: "running", lease_expires_at: "2026-07-22T08:05:30Z" },
      now
    ),
    true
  );
  assert.equal(
    qualityEvaluationLeaseExpired(
      { status: "cancelled", lease_expires_at: "2026-07-22T08:05:30Z" },
      now
    ),
    false
  );
});

test("attempt timeout waits for the server-provided generation plus judge deadline", () => {
  const judgingNow = Date.parse("2026-07-22T08:06:00Z");
  const deadlineNow = Date.parse("2026-07-22T08:10:00Z");
  const job = {
    status: "running" as const,
    current_attempt_started_at: "2026-07-22T08:00:00Z",
    current_attempt_deadline_at: "2026-07-22T08:10:00Z",
    attempt_timeout_seconds: 300,
  };

  assert.equal(qualityEvaluationAttemptTimedOut(job, judgingNow), false);
  assert.equal(qualityEvaluationAttemptTimedOut(job, deadlineNow), true);
});
