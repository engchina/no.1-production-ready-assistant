import { describe, expect, it } from "vitest";

import { runtimeKnowledgeRequiredErrors } from "./RuntimeKnowledgeManager";

const rule = { kind: "rules" as const, selected: null, name: "", title: "合成ルール A", labels: "合成語 A", content: "合成の注意点。", enabled: true };

describe("確認・回答ルールの入力", () => {
  it("ID が空でも、必要な入力があれば保存できる", () => {
    expect(Object.values(runtimeKnowledgeRequiredErrors(rule)).filter(Boolean)).toEqual([]);
  });
  it.each(["", " \n ", "、,，\n"])("空の照合キーワードを受け付けない: %j", (labels) => {
    expect(runtimeKnowledgeRequiredErrors({ ...rule, labels }).labels).toBe("照合キーワードを入力してください。");
  });
  it("確認だけならダミーのルール内容を求めない", () => {
    expect(runtimeKnowledgeRequiredErrors({ ...rule, purpose: "confirmation", content: "" }).content).toBeNull();
    expect(runtimeKnowledgeRequiredErrors({ ...rule, purpose: "generation", content: "" }).content).toBe("ルール内容を入力してください。");
  });
  it("用語の名前は引き続き必須にする", () => {
    expect(runtimeKnowledgeRequiredErrors({ ...rule, kind: "terms" }).name).toBe("用語を入力してください。");
  });
});
