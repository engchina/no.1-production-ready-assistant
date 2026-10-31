import assert from "node:assert/strict";
import test from "node:test";
import { createSyntheticRunPollingInterval } from "../src/features/nl2sql/syntheticRunPolling.ts";

function query(statuses: string[] = []) {
  return { state: {
    data: statuses.map((status, index) => ({ run_id: String(index), status })),
    dataUpdateCount: 1,
    errorUpdateCount: 0,
    error: null as { status: number } | null,
  } };
}

test("idle and terminal runs stop; each active state and a newly submitted run resume polling", () => {
  const interval = createSyntheticRunPollingInterval();
  for (const statuses of [[], ["completed"], ["partial", "failed", "no_data"]]) {
    const q = query(statuses);
    assert.equal(interval(q), false);
    q.state.data.push({ run_id: "new", status: "pending" });
    q.state.dataUpdateCount++;
    assert.equal(interval(q), 2_000);
  }
  for (const status of ["pending", "running", "verifying"]) {
    assert.equal(interval(query([status, "unknown", "completed"])), 2_000);
  }
});

test("unknown results have three slower rechecks, preserve state, and resume when recovered", () => {
  const interval = createSyntheticRunPollingInterval();
  const q = query(["unknown"]);
  for (const expected of [5_000, 15_000, 30_000, false, false]) {
    assert.equal(interval(q), expected);
    assert.equal(interval(q), expected, "observer updates must not spend a recheck");
    assert.equal(q.state.data[0].status, "unknown");
    q.state.dataUpdateCount++;
  }
  q.state.data[0].status = "running";
  assert.equal(interval(q), 2_000);
  q.state.dataUpdateCount++;
  q.state.data[0].status = "completed";
  assert.equal(interval(q), false);
});

test("new unknown IDs and independent user/DB/detail queries have their own budget", () => {
  const interval = createSyntheticRunPollingInterval();
  const first = query(["unknown"]);
  for (let i = 0; i < 4; i++) { interval(first); first.state.dataUpdateCount++; }
  assert.equal(interval(first), false);
  assert.equal(interval(query(["unknown"])), 5_000);
  first.state.data = [{ run_id: "another", status: "unknown" }];
  first.state.dataUpdateCount++;
  assert.equal(interval(first), 5_000);
});

test("transient failures back off and stop even with cached active data; success restores monitoring", () => {
  const interval = createSyntheticRunPollingInterval();
  const q = query(["running"]);
  assert.equal(interval(q), 2_000);
  for (const expected of [5_000, 15_000, 30_000, false]) {
    q.state.error = { status: 503 };
    q.state.errorUpdateCount++;
    assert.equal(interval(q), expected);
    assert.equal(interval(q), expected);
  }
  q.state.error = null;
  q.state.dataUpdateCount++;
  assert.equal(interval(q), 2_000);
  q.state.error = { status: 429 };
  q.state.errorUpdateCount++;
  assert.equal(interval(q), 5_000);
});

test("permanent HTTP failures do not retry, including missing historical details", () => {
  const interval = createSyntheticRunPollingInterval();
  for (const status of [400, 401, 403, 404, 409, 422]) {
    const q = query(["running"]);
    q.state.error = { status };
    q.state.errorUpdateCount++;
    assert.equal(interval(q), false);
  }
  const detail = { state: { data: { run_id: "old", status: "unknown" }, dataUpdateCount: 1, errorUpdateCount: 0, error: null } };
  assert.equal(interval(detail), 5_000);
  detail.state.data.status = "completed";
  detail.state.dataUpdateCount++;
  assert.equal(interval(detail), false);
});
