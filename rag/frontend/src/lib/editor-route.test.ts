import { describe, expect, it } from "vitest";

import { parseEditorTarget } from "./editor-route";

describe("parseEditorTarget", () => {
  it("?id= が無い・空白だけなら一覧", () => {
    expect(parseEditorTarget(null)).toEqual({ kind: "list" });
    expect(parseEditorTarget("")).toEqual({ kind: "list" });
    expect(parseEditorTarget("  ")).toEqual({ kind: "list" });
  });

  it("new は新規、それ以外はその ID の編集", () => {
    expect(parseEditorTarget("new")).toEqual({ kind: "new" });
    expect(parseEditorTarget("bv-1")).toEqual({ kind: "edit", id: "bv-1" });
  });
});
