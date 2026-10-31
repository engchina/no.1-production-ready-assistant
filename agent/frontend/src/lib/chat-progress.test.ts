import { describe, expect, it } from "vitest";
import { AGENT_CHAT_PROGRESS_STEPS } from "./chat-progress";

describe("処理の名前", () => {
  it("内部名があっても表示名の無い処理は一般の案内にする", () => {
    const label = AGENT_CHAT_PROGRESS_STEPS.tool.label;
    expect(typeof label === "function" ? label("done", {tool:"rag__rag_validate_answer"}) : label).toBe("選ばれたツールを呼び出しました");
  });
  it("表示名があれば表示名を使う", () => {
    const label = AGENT_CHAT_PROGRESS_STEPS.tool.label;
    expect(typeof label === "function" ? label("done", {tool:"record_output",title:"出力の項目の記録"}) : label).toBe("「出力の項目の記録」を実行しました");
  });
});
