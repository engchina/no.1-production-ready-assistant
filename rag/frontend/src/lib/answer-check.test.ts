import { describe, expect, it } from "vitest";

import { ragAnswerCheck, runAnswerCheck } from "./answer-check";

// #141: RAG の照合の記録 → RAG・Agent 共通の 5 状態。
const MARK = { text: "段落。", state: "unverified", reasons: [] };

describe("ragAnswerCheck", () => {
  it.each([
    [{ status: "not_run", reason: "audit_disabled" }, "off"],
    [{ status: "not_run", reason: "empty_answer" }, "skipped"],
    [{ status: "error" }, "failed"],
    [{ status: "timed_out" }, "failed"],
    [{ status: "input_too_large" }, "failed"],
    [{ status: "completed", valid: null }, "verified"],
  ])("%o は %s", (answer_validation, state) => {
    expect(ragAnswerCheck({ answer_validation })?.state).toBe(state);
  });

  it("照合が完了しても段落の印があれば valid に関わらず flagged（印を優先する）", () => {
    const check = ragAnswerCheck({ answer_validation: { status: "completed", valid: true }, passage_marks: [MARK] });
    expect(check?.state).toBe("flagged");
    expect(check?.passages?.marks).toHaveLength(1);
  });

  it("失敗と対象外は理由を添え、照合の記録の無い回答は null", () => {
    expect(ragAnswerCheck({ answer_validation: { status: "timed_out" } })?.reason).toBe("時間内に終わりませんでした");
    expect(ragAnswerCheck({ answer_validation: { status: "not_run", reason: "empty_answer" } })?.reason).toBe("回答が空のため");
    expect(ragAnswerCheck({})).toBeNull();
    expect(ragAnswerCheck({ answer_validation: { status: "new_status" } })).toBeNull();
  });
});

describe("runAnswerCheck", () => {
  it("answer_check を優先し、無い過去の実行は照合の実行状態から読む", () => {
    expect(runAnswerCheck("flagged", "passed")?.state).toBe("flagged");
    expect(runAnswerCheck(null, "passed")?.state).toBe("verified");
    expect(runAnswerCheck(null, "disabled")?.state).toBe("off");
    expect(runAnswerCheck(null, "timed_out")?.state).toBe("failed");
    expect(runAnswerCheck(null, "unknown")).toBeNull();
    expect(runAnswerCheck(null, null)).toBeNull();
  });
});
