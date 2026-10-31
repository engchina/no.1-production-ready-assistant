import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "../src/lib/api.ts";
import {
  MAX_CONSECUTIVE_POLL_FAILURES,
  classifyPollFailure,
  isJobGoneError,
} from "../src/features/nl2sql/jobPollingPolicy.ts";

function apiError(status: number): ApiError {
  return new ApiError(status, ["dummy"]);
}

test("isJobGoneError detects only ApiError 404", () => {
  assert.equal(isJobGoneError(apiError(404)), true);
  assert.equal(isJobGoneError(apiError(500)), false);
  assert.equal(isJobGoneError(new Error("network")), false);
  assert.equal(isJobGoneError(undefined), false);
});

test("404 gives up immediately as job-gone regardless of failure count", () => {
  assert.equal(classifyPollFailure(apiError(404), 1), "job-gone");
});

test("transient failures retry until the consecutive threshold", () => {
  for (let count = 1; count < MAX_CONSECUTIVE_POLL_FAILURES; count += 1) {
    assert.equal(classifyPollFailure(new Error("network"), count), "retry");
  }
  assert.equal(
    classifyPollFailure(new Error("network"), MAX_CONSECUTIVE_POLL_FAILURES),
    "give-up"
  );
  assert.equal(classifyPollFailure(apiError(503), MAX_CONSECUTIVE_POLL_FAILURES + 1), "give-up");
});

test("custom threshold is honored", () => {
  assert.equal(classifyPollFailure(new Error("network"), 1, 1), "give-up");
  assert.equal(classifyPollFailure(new Error("network"), 1, 5), "retry");
});
