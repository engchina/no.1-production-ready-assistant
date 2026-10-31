import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("質問の理解の JSON 通信", () => {
  it.each(["保存", "試す"] as const)("%s は JSON として送信する", async (operation) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: {}, error_messages: [], warning_messages: [] }), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const overrides = { question_cues: { cause_cues: ["なぜ", "原因"] } };
    const text = "対象 A の原因は何ですか。";
    if (operation === "保存") await api.saveQuestionUnderstanding("profile-a", overrides);
    else await api.previewQuestionUnderstanding("profile-a", text, overrides);

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const request = new Request(`http://localhost${path}`, init);
    expect(request.headers.get("Content-Type")).toBe("application/json");
    expect(request.method).toBe(operation === "保存" ? "PUT" : "POST");
    expect(new URL(request.url).pathname).toBe(`/api/search-answer-profiles/profile-a/question-understanding${operation === "保存" ? "" : "/preview"}`);
    expect(await request.json()).toEqual(operation === "保存" ? { overrides } : { text, overrides });
  });
});
