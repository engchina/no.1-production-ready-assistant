// 文書詳細の取得失敗の扱い（Issue 311）。
import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import { initialLoadError, retryUnlessNotFound } from "./queries";

describe("文書詳細の取得失敗の扱い", () => {
  const timeout = new ApiError(408, ["API の応答が 30 秒以内に返りませんでした。"]);

  it("初回の取得失敗だけエラーを返し、データがあるときの再取得失敗では前の内容を残す", () => {
    expect(initialLoadError({ data: undefined, error: timeout })).toBe(timeout);
    expect(initialLoadError({ data: [], error: timeout })).toBeNull();
    expect(initialLoadError({ data: [{ recipe_id: "r1" }], error: timeout })).toBeNull();
    expect(initialLoadError({ data: undefined, error: null })).toBeNull();
  });

  it("時間切れは再試行し、404 は再試行しない", () => {
    expect(retryUnlessNotFound(0, timeout)).toBe(true);
    expect(retryUnlessNotFound(3, timeout)).toBe(false);
    expect(retryUnlessNotFound(0, new ApiError(404, ["見つかりません"]))).toBe(false);
  });
});
