import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiError,
  apiGet,
  decodeApiProblem,
  type ApiProblem,
} from "../src/lib/api";
import {
  mapApiFieldErrors,
  unmappedApiErrorMessage,
  withoutFieldError,
} from "../src/lib/api-field-errors";

const conflictProblem: ApiProblem = {
  type: "urn:nl2sql:problem:security-user-login-id-conflict",
  title: "ユーザーを作成できません",
  status: 409,
  detail: "このログインユーザーIDは既に使用されています。別のIDを入力してください。",
  code: "SECURITY_USER_LOGIN_ID_CONFLICT",
  request_id: "request-123",
  retryable: false,
  field_errors: [
    {
      pointer: "/login_user_id",
      code: "already_exists",
      message: "別のログインユーザーIDを入力してください。",
    },
  ],
};

test("new problem envelope is decoded without parsing Japanese detail", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        data: null,
        error_messages: [conflictProblem.detail],
        warning_messages: [],
        error_code: conflictProblem.code,
        problem: conflictProblem,
      }),
      {
        status: 409,
        headers: { "Content-Type": "application/json", "X-Request-ID": "request-123" },
      }
    );
  try {
    await assert.rejects(apiGet("/api/security/users"), (cause: unknown) => {
      assert.ok(cause instanceof ApiError);
      assert.equal(cause.errorCode, "SECURITY_USER_LOGIN_ID_CONFLICT");
      assert.equal(cause.fieldErrors[0]?.pointer, "/login_user_id");
      assert.equal(cause.requestId, "request-123");
      assert.equal(cause.retryable, false);
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("legacy envelope remains supported", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        data: null,
        error_messages: ["従来形式の安全なエラーです。"],
        error_code: "LEGACY_ERROR",
      }),
      { status: 400, headers: { "Content-Type": "application/json" } }
    );
  try {
    await assert.rejects(apiGet("/api/legacy"), (cause: unknown) => {
      assert.ok(cause instanceof ApiError);
      assert.equal(cause.message, "従来形式の安全なエラーです。");
      assert.equal(cause.errorCode, "LEGACY_ERROR");
      assert.deepEqual(cause.fieldErrors, []);
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("401 keeps the request id out of baseMessages so the login banner can hide it", async () => {
  const loginFailedProblem: ApiProblem = {
    type: "urn:nl2sql:problem:security-login-failed",
    title: "ログインできません",
    status: 401,
    detail: "ログインユーザーIDまたはパスワードを確認してください。",
    code: "SECURITY_LOGIN_FAILED",
    request_id: "login-failed-request",
    retryable: false,
    field_errors: [],
  };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        data: null,
        error_messages: [loginFailedProblem.detail],
        warning_messages: [],
        error_code: loginFailedProblem.code,
        problem: loginFailedProblem,
      }),
      {
        status: 401,
        headers: { "Content-Type": "application/json", "X-Request-ID": "login-failed-request" },
      }
    );
  try {
    await assert.rejects(apiGet("/api/auth/login"), (cause: unknown) => {
      assert.ok(cause instanceof ApiError);
      assert.equal(cause.requestId, "login-failed-request");
      // 既定の表示文言(他画面向け)にはリクエスト ID が残る
      assert.ok(cause.message.includes("リクエストID: login-failed-request"));
      assert.ok(cause.messages[0]?.includes("リクエストID: login-failed-request"));
      // ログイン画面が使う元文言には含まれない
      assert.equal(cause.baseMessages[0], loginFailedProblem.detail);
      assert.ok(!cause.baseMessages[0]?.includes("リクエストID"));
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("security migration 409 includes request id in recovery UI message", () => {
  const migrationProblem: ApiProblem = {
    type: "urn:nl2sql:problem:security-schema-migration-required",
    title: "セキュリティ初期化が必要です",
    status: 409,
    detail: "アプリケーション認証/RBAC の schema migration が未適用です。",
    code: "SECURITY_SCHEMA_MIGRATION_REQUIRED",
    request_id: "security-migration-request",
    retryable: false,
    field_errors: [],
  };

  const cause = new ApiError(
    409,
    [migrationProblem.detail],
    undefined,
    undefined,
    migrationProblem
  );

  assert.ok(cause.message.includes("リクエストID: security-migration-request"));
  assert.equal(cause.baseMessages[0], migrationProblem.detail);
});

test("field mapping, de-duplication, and local clear are deterministic", () => {
  const cause = new ApiError(409, [conflictProblem.detail], undefined, undefined, conflictProblem);
  const pointerMap = { "/login_user_id": "loginUserId", "/display_name": "displayName" } as const;
  const mapped = mapApiFieldErrors(cause, pointerMap);

  assert.deepEqual(mapped, { loginUserId: "別のログインユーザーIDを入力してください。" });
  assert.equal(unmappedApiErrorMessage(cause, pointerMap, "fallback"), "");
  const multiple = { ...mapped, displayName: "表示名を入力してください。" };
  assert.deepEqual(withoutFieldError(multiple, "loginUserId"), {
    displayName: "表示名を入力してください。",
  });
});

test("unknown or malformed problem falls back safely", () => {
  assert.equal(decodeApiProblem({ detail: "incomplete" }), undefined);
  const cause = new ApiError(500, ["安全なサーバー文言"]);
  assert.equal(unmappedApiErrorMessage(cause, {}, "fallback"), "安全なサーバー文言");
});
