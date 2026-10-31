import { describe, expect, it } from "vitest";

import { splitErrorCode } from "./ingestion-error-display";

describe("splitErrorCode", () => {
  it("末尾のエラーコードを本文から分け、無ければ本文のまま返す", () => {
    expect(
      splitErrorCode("処理レシピで Unstructured に変えてください。 エラーコード: docling_adapter_source_unsupported")
    ).toEqual({
      message: "処理レシピで Unstructured に変えてください。",
      code: "docling_adapter_source_unsupported",
    });
    expect(splitErrorCode("取込に失敗しました。")).toEqual({
      message: "取込に失敗しました。",
      code: null,
    });
  });
});
