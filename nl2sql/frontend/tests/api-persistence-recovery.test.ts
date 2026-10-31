import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, apiGet, apiPost } from "../src/lib/api.ts";
import { supersedeDatabaseUnavailableProbe } from "../src/lib/database-load-error.ts";

function jsonResponse(data: unknown, status = 200, errorCode?: string): Response {
  return new Response(
    JSON.stringify({
      data: status < 400 ? data : null,
      error_messages: status < 400 ? [] : ["業務データを利用できません。"],
      warning_messages: [],
      error_code: errorCode,
    }),
    { status, headers: { "Content-Type": "application/json" } }
  );
}

test("GET は persistence 回復成功後に一度だけ自動再試行する", async () => {
  supersedeDatabaseUnavailableProbe();
  const originalFetch = globalThis.fetch;
  let objectRequests = 0;
  let recoveryRequests = 0;
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (path === "/api/ready/database") {
      return jsonResponse({ status: "ok", check: "ok", detail: null });
    }
    if (path === "/api/nl2sql/persistence" && (init?.method ?? "GET") === "GET") {
      return jsonResponse({
        ready: false,
        writable: false,
        reason_code: "oracle_connection_unavailable",
      });
    }
    if (path === "/api/nl2sql/persistence/recover") {
      recoveryRequests += 1;
      return jsonResponse({ ready: true, writable: true, reason_code: null });
    }
    if (path.startsWith("/api/nl2sql/db-admin/objects")) {
      objectRequests += 1;
      return objectRequests === 1
        ? jsonResponse(null, 503, "oracle_connection_unavailable")
        : jsonResponse({ items: ["ORDERS"] });
    }
    throw new Error(`unexpected request: ${path}`);
  };

  try {
    const data = await apiGet<{ items: string[] }>(
      "/api/nl2sql/db-admin/objects?limit=100"
    );
    assert.deepEqual(data.items, ["ORDERS"]);
    assert.equal(objectRequests, 2);
    assert.equal(recoveryRequests, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("非冪等 POST は persistence が回復可能でも自動再送しない", async () => {
  supersedeDatabaseUnavailableProbe();
  const originalFetch = globalThis.fetch;
  let mutationRequests = 0;
  let recoveryRequests = 0;
  globalThis.fetch = async (input, init) => {
    const path = String(input);
    if (path === "/api/ready/database") {
      return jsonResponse({ status: "ok", check: "ok", detail: null });
    }
    if (path === "/api/nl2sql/persistence") {
      return jsonResponse({
        ready: false,
        writable: false,
        reason_code: "oracle_connection_unavailable",
      });
    }
    if (path === "/api/nl2sql/persistence/recover") {
      recoveryRequests += 1;
      return jsonResponse({ ready: true, writable: true, reason_code: null });
    }
    if (path === "/api/nl2sql/profiles" && init?.method === "POST") {
      mutationRequests += 1;
      return jsonResponse(null, 503, "oracle_connection_unavailable");
    }
    throw new Error(`unexpected request: ${path}`);
  };

  try {
    await assert.rejects(
      apiPost("/api/nl2sql/profiles", { id: "sales" }),
      (cause) =>
        cause instanceof ApiError &&
        cause.status === 503 &&
        cause.errorCode === "oracle_connection_unavailable"
    );
    assert.equal(mutationRequests, 1);
    assert.equal(recoveryRequests, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
