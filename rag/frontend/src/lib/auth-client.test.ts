import { CSRF_COOKIE_NAME } from "@production-ready/system-settings";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./api";
import { describeSecurityApiError, securityApi } from "./security-api";
import { streamSearch } from "./search-stream";

/** API client の Cookie セッション対応（CSRF・401 / 403 の通知・エラー形式。#214）。 */

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

const OK = { data: {}, error_messages: [], warning_messages: [] };

/** node 環境に document.cookie と window のイベントを用意する。 */
function stubBrowser(cookie = "") {
  const events: string[] = [];
  const target = new EventTarget();
  vi.stubGlobal("document", { cookie });
  vi.stubGlobal("window", {
    dispatchEvent: (event: Event) => {
      events.push(event.type);
      return target.dispatchEvent(event);
    },
  });
  return events;
}

function sentHeaders(fetchMock: ReturnType<typeof vi.fn>, call = 0): Headers {
  return new Headers((fetchMock.mock.calls[call][1] as RequestInit).headers);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CSRF header", () => {
  it("状態を変える method では共有の CSRF の Cookie（pr_csrf）の値を X-CSRF-Token で送る", async () => {
    stubBrowser(`other=1; ${CSRF_COOKIE_NAME}=token-123`);
    const fetchMock = vi.fn().mockImplementation(async () =>jsonResponse(OK));
    vi.stubGlobal("fetch", fetchMock);

    await api.archiveKnowledgeBase("kb-1");
    await securityApi.logout();

    expect(sentHeaders(fetchMock, 0).get("X-CSRF-Token")).toBe("token-123");
    expect(sentHeaders(fetchMock, 1).get("X-CSRF-Token")).toBe("token-123");
    expect((fetchMock.mock.calls[0][1] as RequestInit).credentials).toBe("same-origin");
  });

  it("GET では送らない", async () => {
    stubBrowser(`${CSRF_COOKIE_NAME}=token-123`);
    const fetchMock = vi.fn().mockImplementation(async () =>jsonResponse({ ...OK, data: { status: "ok", check: "ok", detail: null } }));
    vi.stubGlobal("fetch", fetchMock);

    await api.getDatabaseStatus();

    expect(sentHeaders(fetchMock).has("X-CSRF-Token")).toBe(false);
    expect(sentHeaders(fetchMock).get("Accept")).toBe("application/json");
  });

  // multipart のアップロード（XHR）は api.test.ts の「文書アップロードの送信（XHR）」で確認する（#306）。
  it("stream の直接 fetch にも付ける", async () => {
    stubBrowser(`${CSRF_COOKIE_NAME}=token-xyz`);
    const fetchMock = vi.fn().mockResolvedValueOnce(
      new Response("event: done\ndata: {\"trace_id\":\"t\"}\n\n", {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await streamSearch({ query: "q" } as never, {});

    expect(sentHeaders(fetchMock, 0).get("X-CSRF-Token")).toBe("token-xyz");
  });
});

describe("401 / 403 の通知", () => {
  it("401 はログインへ戻すイベント、403 は権限なしの画面へ移すイベントを出す", async () => {
    const events = stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ data: null, error_messages: ["ログインが必要です。"] }, 401))
        .mockResolvedValueOnce(
          jsonResponse({ data: null, error_messages: ["権限がありません。"] }, 403, {
            "X-Request-ID": "req-1",
          })
        )
    );

    await expect(api.getDatabaseStatus()).rejects.toMatchObject({ status: 401 });
    await expect(api.getDatabaseStatus()).rejects.toMatchObject({ status: 403, requestId: "req-1" });

    expect(events).toEqual(["app-auth-unauthorized", "app-auth-forbidden"]);
  });

  it("検索・回答プロファイル / KB の範囲外の 403（検索・stream・セキュリティの更新）は画面を移さず理由を返す", async () => {
    const events = stubBrowser();
    // 範囲外は RAG_SCOPE_FORBIDDEN、権限の付与の制限は SECURITY_PERMISSION_DENIED（#224）。
    const forbidden = (url: string) =>
      jsonResponse(
        {
          data: null,
          error_messages: ["この検索・回答プロファイルのナレッジベースを利用する権限がありません。"],
          error_code: url.includes("/security/") ? "SECURITY_PERMISSION_DENIED" : "RAG_SCOPE_FORBIDDEN",
        },
        403
      );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => Promise.resolve(forbidden(String(url))))
    );

    await expect(api.search({ query: "q" } as never)).rejects.toMatchObject({
      status: 403,
      message: "この検索・回答プロファイルのナレッジベースを利用する権限がありません。",
    });
    await expect(streamSearch({ query: "q" } as never, {})).rejects.toBeInstanceOf(ApiError);
    await expect(
      securityApi.updateRoleAccess({
        role_id: "r1",
        version: 1,
        permissions: [],
        search_answer_profile_ids: ["bv-x"],
        knowledge_base_ids: [],
      })
    ).rejects.toMatchObject({ status: 403 });

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(events).toEqual([]);
  });

  it("経路の権限拒否（SECURITY_ROUTE_FORBIDDEN）の 403 だけ権限なしの画面へ移す", async () => {
    const events = stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() =>
        Promise.resolve(
          jsonResponse(
            { data: null, error_messages: ["この機能を利用する権限がありません。"], error_code: "SECURITY_ROUTE_FORBIDDEN" },
            403
          )
        )
      )
    );

    await expect(api.search({ query: "q" } as never)).rejects.toMatchObject({ status: 403 });
    await expect(streamSearch({ query: "q" } as never, {})).rejects.toBeInstanceOf(ApiError);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(events).toEqual(["app-auth-forbidden", "app-auth-forbidden"]);
  });

  it("stream の 401 はログインへ戻すイベントを出す", async () => {
    const events = stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () =>jsonResponse({ data: null, error_messages: ["ログインが必要です。"] }, 401))
    );

    await expect(streamSearch({ query: "q" } as never, {})).rejects.toMatchObject({ status: 401 });
    expect(events).toEqual(["app-auth-unauthorized"]);
  });
});

describe("ApiError と describeSecurityApiError", () => {
  it("error_code・field_errors・request ID を共通画面へ渡す", async () => {
    stubBrowser();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () =>
        jsonResponse(
          {
            data: null,
            error_messages: ["入力内容を確認してください。"],
            warning_messages: [],
            error_code: "SECURITY_REQUEST_INVALID",
            problem: {
              request_id: "req-9",
              field_errors: [{ pointer: "/login_user_id", message: "既に使われています。" }, { bad: 1 }],
            },
          },
          400
        )
      )
    );

    const error = await securityApi
      .createUser({ login_user_id: "u", display_name: "U", role_ids: [] })
      .catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 400,
      errorCode: "SECURITY_REQUEST_INVALID",
      requestId: "req-9",
      fieldErrors: [{ pointer: "/login_user_id", message: "既に使われています。" }],
    });
    expect(describeSecurityApiError(error)).toEqual({
      message: "入力内容を確認してください。",
      code: "SECURITY_REQUEST_INVALID",
      fieldErrors: [{ pointer: "/login_user_id", message: "既に使われています。" }],
    });
    expect(describeSecurityApiError(new Error("x"))).toEqual({ message: "x" });
    expect(describeSecurityApiError("x")).toBeUndefined();
  });

  it("ログイン API は login_user_id と password を送る", async () => {
    stubBrowser();
    const fetchMock = vi.fn().mockImplementation(async () =>jsonResponse({ ...OK, data: { user_uuid: "u1" } }));
    vi.stubGlobal("fetch", fetchMock);

    await securityApi.login("admin", "secret");

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/auth/login");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ login_user_id: "admin", password: "secret" });
  });
});
