import assert from "node:assert/strict";
import test from "node:test";

import {
  confirmDatabaseUnavailable,
  isDatabaseReadinessRequest,
  shouldConfirmDatabaseUnavailable,
  supersedeDatabaseUnavailableProbe,
  type DatabaseOperationalFailure,
  type DatabaseReadinessSnapshot,
} from "../src/lib/database-load-error.ts";

function readinessResponse(status: DatabaseReadinessSnapshot["status"]): Response {
  return new Response(
    JSON.stringify({
      data: {
        status,
        check: status === "ok" ? "ok" : "oracle_unreachable",
        detail: status === "ok" ? null : "ORA-12514",
      },
      error_messages: [],
      warning_messages: [],
    }),
    { status: 200, headers: { "Content-Type": "application/json" } }
  );
}

function persistenceResponse(ready: boolean): Response {
  return new Response(
    JSON.stringify({
      data: {
        ready,
        writable: ready,
        reason_code: ready ? null : "incremental_schema_search_failed",
      },
      error_messages: [],
      warning_messages: [],
    }),
    { status: 200, headers: { "Content-Type": "application/json" } }
  );
}

function operationalFetch(
  databaseStatus: DatabaseReadinessSnapshot["status"],
  persistenceReady = true
) {
  return async (input: RequestInfo | URL) =>
    String(input).includes("/api/nl2sql/persistence")
      ? persistenceResponse(persistenceReady)
      : readinessResponse(databaseStatus);
}

test("5xx と通信失敗だけが readiness 再確認の対象になる", () => {
  assert.equal(shouldConfirmDatabaseUnavailable("/api/security/roles", 500), true);
  assert.equal(shouldConfirmDatabaseUnavailable("/api/security/roles", 503), true);
  assert.equal(shouldConfirmDatabaseUnavailable("/api/security/roles", 403), false);
  assert.equal(shouldConfirmDatabaseUnavailable("/api/ready/database", 503), false);
  assert.equal(isDatabaseReadinessRequest("/api/ready/database?fresh=true"), true);
});

test("readiness が非正常の場合だけ DB 未到達を通知する", async () => {
  supersedeDatabaseUnavailableProbe();
  const reports: DatabaseOperationalFailure[] = [];

  const confirmed = await confirmDatabaseUnavailable(
    operationalFetch("unreachable"),
    (status) => reports.push(status)
  );

  assert.equal(confirmed?.kind, "database");
  assert.equal(reports.length, 1);
  assert.equal(reports[0]?.kind, "database");
  assert.equal(reports[0]?.kind === "database" && reports[0].database.status, "unreachable");
});

test("DB が正常なら画面固有エラーを維持する", async () => {
  supersedeDatabaseUnavailableProbe();
  const reports: DatabaseOperationalFailure[] = [];

  const confirmed = await confirmDatabaseUnavailable(
    operationalFetch("ok"),
    (status) => reports.push(status)
  );

  assert.equal(confirmed, null);
  assert.deepEqual(reports, []);
});

test("DB 接続済みでも persistence が閉じていれば別状態として通知する", async () => {
  supersedeDatabaseUnavailableProbe();
  const reports: DatabaseOperationalFailure[] = [];

  const confirmed = await confirmDatabaseUnavailable(
    operationalFetch("ok", false),
    (failure) => reports.push(failure)
  );

  assert.equal(confirmed?.kind, "persistence");
  assert.equal(reports[0]?.kind, "persistence");
  assert.equal(
    reports[0]?.kind === "persistence" && reports[0].persistence.reason_code,
    "incremental_schema_search_failed"
  );
});

test("readiness の確認自体に失敗した場合は通知しない", async () => {
  supersedeDatabaseUnavailableProbe();
  let reported = false;

  const confirmed = await confirmDatabaseUnavailable(
    async () => {
      throw new TypeError("Failed to fetch");
    },
    () => {
      reported = true;
    }
  );

  assert.equal(confirmed, null);
  assert.equal(reported, false);
});

test("同時発生した失敗は1回の readiness probe へ集約する", async () => {
  supersedeDatabaseUnavailableProbe();
  let probes = 0;
  let reports = 0;
  const fetchReadiness = async () => {
    probes += 1;
    await Promise.resolve();
    return readinessResponse("unreachable");
  };

  const [first, second] = await Promise.all([
    confirmDatabaseUnavailable(fetchReadiness, () => {
      reports += 1;
    }),
    confirmDatabaseUnavailable(fetchReadiness, () => {
      reports += 1;
    }),
  ]);

  assert.deepEqual([first?.kind, second?.kind], ["database", "database"]);
  assert.equal(probes, 1);
  assert.equal(reports, 1);
});

test("再試行開始前の古い probe 結果は通知しない", async () => {
  supersedeDatabaseUnavailableProbe();
  let resolveResponse: ((response: Response) => void) | undefined;
  let reported = false;
  const pending = confirmDatabaseUnavailable(
    () =>
      new Promise<Response>((resolve) => {
        resolveResponse = resolve;
      }),
    () => {
      reported = true;
    }
  );

  supersedeDatabaseUnavailableProbe();
  resolveResponse?.(readinessResponse("unreachable"));

  assert.equal(await pending, null);
  assert.equal(reported, false);
});

test("応答しない DB の状態の確認は上限で打ち切り、元の失敗のまま返す（#1160）", async () => {
  supersedeDatabaseUnavailableProbe();
  let reported = false;
  const signals: (AbortSignal | null | undefined)[] = [];
  const started = Date.now();
  const confirmed = await confirmDatabaseUnavailable(
    (_input, init) => {
      signals.push(init?.signal);
      // signal を見ない（応答しない）fetch でも、上限で打ち切る。
      return new Promise<Response>(() => undefined);
    },
    () => {
      reported = true;
    },
    50
  );

  assert.equal(confirmed, null);
  assert.equal(reported, false);
  assert.ok(Date.now() - started < 5_000);
  assert.ok(signals[0] instanceof AbortSignal);
  assert.equal(signals[0]?.aborted, true);
  // 打ち切った確認は残さない（次の失敗で確認し直す）。
  let probes = 0;
  await confirmDatabaseUnavailable(async () => {
    probes += 1;
    return readinessResponse("ok");
  });
  // readiness と persistence の 2 回（打ち切った確認を返さず、新しく確認した）。
  assert.equal(probes, 2);
});
