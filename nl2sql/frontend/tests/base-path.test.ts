import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { apiGet } from "../src/lib/api.ts";
import {
  APP_BASE_PATH,
  ROUTER_BASENAME,
  appPath,
  overrideAppBasePathForTest,
  stripAppPath,
} from "../src/lib/base-path.ts";
import {
  confirmDatabaseUnavailable,
  isDatabaseReadinessRequest,
  supersedeDatabaseUnavailableProbe,
} from "../src/lib/database-load-error.ts";
import { resolveFrontendBasePath } from "../vite.config.ts";

// 1 台の Compute で `/nl2sql/` の前置きで配信するとき（#1316）、backend への要求と、ブラウザが直接開く URL
// （`<a href>`）に前置きが付き、前置きの無い配信（`/`）では何も変わらないことを確かめる。

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(
    JSON.stringify({ data: status < 400 ? data : null, error_messages: status < 400 ? [] : ["失敗"] }),
    { status, headers: { "Content-Type": "application/json" } }
  );
}

async function withBase<T>(base: string | null, run: () => Promise<T> | T): Promise<T> {
  overrideAppBasePathForTest(base);
  try {
    return await run();
  } finally {
    overrideAppBasePathForTest(null);
  }
}

async function captureFetches(run: () => Promise<unknown>, respond: (path: string) => Response) {
  const originalFetch = globalThis.fetch;
  const requested: string[] = [];
  globalThis.fetch = async (input) => {
    const path = String(input);
    requested.push(path);
    return respond(path);
  };
  try {
    await run();
  } catch {
    // 失敗の応答を返す場合も、要求した path だけを確かめる。
  } finally {
    globalThis.fetch = originalFetch;
  }
  return requested;
}

test("ロジックテスト（import.meta.env が無い）の既定の前置きは / で、router の basename は無い", () => {
  assert.equal(APP_BASE_PATH, "/");
  assert.equal(ROUTER_BASENAME, undefined);
});

test("vite の base は FRONTEND_BASE_PATH から決め、未設定・空なら / にする", () => {
  assert.equal(resolveFrontendBasePath(undefined), "/");
  assert.equal(resolveFrontendBasePath(""), "/");
  assert.equal(resolveFrontendBasePath("  "), "/");
  assert.equal(resolveFrontendBasePath("/"), "/");
  assert.equal(resolveFrontendBasePath("/nl2sql/"), "/nl2sql/");
  assert.equal(resolveFrontendBasePath("nl2sql"), "/nl2sql/");
  assert.equal(resolveFrontendBasePath("/nl2sql"), "/nl2sql/");
});

test("前置きが / なら appPath は path を変えない", () => {
  assert.equal(appPath("/api/nl2sql/jobs"), "/api/nl2sql/jobs");
  assert.equal(appPath("/feedback-management?tab=appFeedback"), "/feedback-management?tab=appFeedback");
  assert.equal(stripAppPath("/api/ready/database"), "/api/ready/database");
});

test("前置きが /nl2sql/ なら appPath は 1 回だけ付け、外部の URL・blob・相対パスは変えない", async () => {
  await withBase("/nl2sql/", () => {
    assert.equal(appPath("/api/nl2sql/jobs"), "/nl2sql/api/nl2sql/jobs");
    assert.equal(appPath(appPath("/api/nl2sql/jobs")), "/nl2sql/api/nl2sql/jobs");
    assert.equal(
      appPath("/question-classifier-models?tab=candidates"),
      "/nl2sql/question-classifier-models?tab=candidates"
    );
    assert.equal(appPath("https://docs.oracle.com/x"), "https://docs.oracle.com/x");
    assert.equal(appPath("blob:http://localhost/1"), "blob:http://localhost/1");
    assert.equal(appPath("api/x"), "api/x");
    assert.equal(stripAppPath("/nl2sql/api/ready/database"), "/api/ready/database");
  });
});

test("apiFetch（apiGet）は前置きが / なら /api/... のまま要求する", async () => {
  const requested = await captureFetches(
    () => apiGet("/api/nl2sql/profiles?limit=10"),
    () => jsonResponse({ items: [] })
  );
  assert.deepEqual(requested, ["/api/nl2sql/profiles?limit=10"]);
});

test("apiFetch（apiGet）は前置きが /nl2sql/ なら /nl2sql/api/... を要求する", async () => {
  const requested = await withBase("/nl2sql/", () =>
    captureFetches(
      () => apiGet("/api/nl2sql/profiles?limit=10"),
      () => jsonResponse({ items: [] })
    )
  );
  assert.deepEqual(requested, ["/nl2sql/api/nl2sql/profiles?limit=10"]);
});

test("5xx の後の DB の確認・persistence の回復・再試行も前置きを付けて要求する", async () => {
  supersedeDatabaseUnavailableProbe();
  let objectRequests = 0;
  const requested = await withBase("/nl2sql/", () =>
    captureFetches(
      () => apiGet("/api/nl2sql/db-admin/objects"),
      (path) => {
        if (path === "/nl2sql/api/ready/database") {
          return jsonResponse({ status: "ok", check: "ok", detail: null });
        }
        if (path === "/nl2sql/api/nl2sql/persistence") {
          return jsonResponse({ ready: false, writable: false, reason_code: "oracle_connection_unavailable" });
        }
        if (path === "/nl2sql/api/nl2sql/persistence/recover") {
          return jsonResponse({ ready: true, writable: true, reason_code: null });
        }
        if (path === "/nl2sql/api/nl2sql/db-admin/objects") {
          objectRequests += 1;
          return objectRequests === 1 ? jsonResponse(null, 503) : jsonResponse({ items: [] });
        }
        throw new Error(`前置きの無い要求: ${path}`);
      }
    )
  );
  assert.deepEqual(requested, [
    "/nl2sql/api/nl2sql/db-admin/objects",
    "/nl2sql/api/ready/database",
    "/nl2sql/api/nl2sql/persistence",
    "/nl2sql/api/nl2sql/persistence/recover",
    "/nl2sql/api/nl2sql/db-admin/objects",
  ]);
});

test("DB の確認（confirmDatabaseUnavailable）は前置きが / なら /api/ready/database を要求する", async () => {
  supersedeDatabaseUnavailableProbe();
  const requested: string[] = [];
  await confirmDatabaseUnavailable(
    async (input) => {
      requested.push(String(input));
      return jsonResponse({ status: "ok", check: "ok", detail: null }, 500);
    },
    () => undefined
  );
  assert.deepEqual(requested, ["/api/ready/database"]);
});

test("readiness 自体の要求の判定は前置きが付いた path でも同じ", async () => {
  assert.equal(isDatabaseReadinessRequest("/api/ready/database"), true);
  await withBase("/nl2sql/", () => {
    assert.equal(isDatabaseReadinessRequest("/api/ready/database"), true);
    assert.equal(isDatabaseReadinessRequest("/nl2sql/api/ready/database?x=1"), true);
    assert.equal(isDatabaseReadinessRequest("/nl2sql/api/nl2sql/jobs"), false);
  });
});

test("ブラウザが直接開く URL（<a href>）と router の basename は前置きを通す", () => {
  const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  const main = read("../src/main.tsx");
  assert.match(main, /createBrowserRouter\([\s\S]*basename: ROUTER_BASENAME/);

  const questionLearning = read("../src/features/nl2sql/pages/QuestionLearningPage.tsx");
  assert.match(questionLearning, /href=\{appPath\("\/api\/nl2sql\/classifier\/training-data\/export\.xlsx"\)\}/);
  assert.match(questionLearning, /href=\{appPath\(`\$\{APP_ROUTES\.feedbackManagement\}/);

  const feedback = read("../src/features/nl2sql/pages/FeedbackManagementPage.tsx");
  assert.match(feedback, /href: appPath\(`\$\{APP_ROUTES\.questionClassifierModels\}/);

  const profiles = read("../src/features/nl2sql/pages/ProfileManagementPage.tsx");
  assert.match(profiles, /profileHref=\{\(profile\) => appPath\(/);

  // 画面のコードから、前置きを通さない生の href="/api/..." を書かない。
  for (const path of [
    "../src/features/nl2sql/pages/QuestionLearningPage.tsx",
    "../src/features/nl2sql/pages/EvaluationPage.tsx",
    "../src/features/nl2sql/pages/FeedbackManagementPage.tsx",
  ]) {
    assert.doesNotMatch(read(path), /href=["'`]\/api\//, path);
  }
});
