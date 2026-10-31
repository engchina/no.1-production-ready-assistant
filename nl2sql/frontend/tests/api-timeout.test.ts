import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiTransportError,
  apiGet,
  apiPost,
  isAbortError,
  isTimeoutError,
  isTransportError,
} from "../src/lib/api.ts";
import { API_TIMEOUT_MS } from "../src/lib/requestPolicy.ts";

function abortableFetch(
  calls: { count: number },
): typeof fetch {
  return async (_input, init) => {
    calls.count += 1;
    return await new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      if (signal?.aborted) {
        reject(signal.reason);
        return;
      }
      signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
    });
  };
}

test("apiGet reports a timeout without treating it as a database readiness failure", async () => {
  const originalFetch = globalThis.fetch;
  const calls = { count: 0 };
  // AbortSignal.timeout の timer は Node では unref されるため、検証中だけ event loop を保持する。
  const keepAlive = setTimeout(() => undefined, 1_000);
  globalThis.fetch = abortableFetch(calls);
  try {
    await assert.rejects(
      apiGet("/api/nl2sql/db-admin/tables/TABLE_01", { timeoutMs: 10 }),
      (cause) => isTimeoutError(cause) && !isAbortError(cause),
    );
    assert.equal(calls.count, 1, "timeout must not trigger a database readiness probe");
  } finally {
    clearTimeout(keepAlive);
    globalThis.fetch = originalFetch;
  }
});

test("apiGet preserves an explicit user cancellation as AbortError", async () => {
  const originalFetch = globalThis.fetch;
  const calls = { count: 0 };
  globalThis.fetch = abortableFetch(calls);
  const controller = new AbortController();
  try {
    const request = apiGet("/api/nl2sql/db-admin/views/VIEW_01", {
      signal: controller.signal,
      timeoutMs: API_TIMEOUT_MS.interactiveDetail,
    });
    controller.abort();
    await assert.rejects(
      request,
      (cause) => isAbortError(cause) && !isTimeoutError(cause),
    );
    assert.equal(calls.count, 1, "cancellation must not trigger a database readiness probe");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("timeout は英語の signal timed out ではなく、上限の秒数と次の操作を日本語で伝える (#900)", async () => {
  const originalFetch = globalThis.fetch;
  const calls = { count: 0 };
  const keepAlive = setTimeout(() => undefined, 1_000);
  globalThis.fetch = abortableFetch(calls);
  try {
    await assert.rejects(
      apiPost("/api/nl2sql/jobs?trace=1", { question: "select * from employee" }, {
        timeoutMs: 10,
      }),
      (cause) => {
        assert.ok(isTransportError(cause));
        assert.equal(cause.kind, "timeout");
        assert.equal(cause.summary, "サーバーの応答が 1 秒以内に返りませんでした。");
        assert.match(cause.message, /^サーバーの応答が 1 秒以内に返りませんでした。/u);
        assert.match(cause.nextAction, /もう一度実行してください/u);
        assert.doesNotMatch(cause.message, /signal|timed out|aborted/iu);
        // 技術的な詳細は「詳細」用に分けて持つ（query string は出さない）。
        assert.equal(cause.method, "POST");
        assert.equal(cause.path, "/api/nl2sql/jobs");
        assert.equal(cause.timeoutMs, 10);
        assert.equal(cause.causeName, "TimeoutError");
        return true;
      },
    );
  } finally {
    clearTimeout(keepAlive);
    globalThis.fetch = originalFetch;
  }
});

test("サーバーに接続できない (TypeError: Failed to fetch) ときも日本語の失敗にする (#900)", async () => {
  const originalFetch = globalThis.fetch;
  const requested: string[] = [];
  globalThis.fetch = async (input) => {
    requested.push(String(input));
    throw new TypeError("Failed to fetch");
  };
  try {
    await assert.rejects(
      apiGet("/api/nl2sql/chats"),
      (cause) => {
        assert.ok(cause instanceof ApiTransportError);
        assert.equal(cause.kind, "network");
        assert.equal(cause.summary, "サーバーに接続できませんでした。");
        assert.doesNotMatch(cause.message, /Failed to fetch/u);
        assert.equal(cause.causeMessage, "Failed to fetch");
        assert.equal(cause.method, "GET");
        assert.ok(!isTimeoutError(cause) && !isAbortError(cause));
        return true;
      },
    );
    // 通信の失敗では、今までどおり DB の起動状態も確かめる。
    assert.ok(requested.length > 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("本文の読み取り中の timeout も利用者向けの失敗にする (#900)", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    ({
      ok: true,
      status: 200,
      url: "http://localhost/api/nl2sql/chats/chat-1",
      headers: new Headers(),
      json: async () => {
        throw new DOMException("signal timed out", "TimeoutError");
      },
    }) as unknown as Response;
  try {
    await assert.rejects(
      apiGet("/api/nl2sql/chats/chat-1", { timeoutMs: 30_000 }),
      (cause) => {
        assert.ok(isTransportError(cause));
        assert.equal(cause.summary, "サーバーの応答が 30 秒以内に返りませんでした。");
        assert.equal(cause.path, "/api/nl2sql/chats/chat-1");
        assert.ok(isTimeoutError(cause));
        return true;
      },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
