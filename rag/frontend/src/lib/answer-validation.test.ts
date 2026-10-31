import { describe, expect, it } from "vitest";
import { parseAnswerValidation, validationExecutionBadge } from "./answer-validation";

describe("資料照合の実行状態", () => {
  it.each([
    [{ status: "disabled", valid: false }, "disabled"],
    [{ status: "not_run", reason: "audit_disabled" }, "disabled"],
    [{ status: "not_run", valid: true }, "not_run"],
    [{ status: "error", valid: true }, "error"],
    [{ status: "timed_out", valid: false }, "timed_out"],
    [{ status: "completed", valid: true }, "passed"],
    [{ status: "completed", valid: false }, "failed"],
    [{ status: "completed" }, "unknown"],
    [{ status: "ok", valid: true }, "passed"],
    [{ status: "validated", valid: false }, "failed"],
    [{ status: "no_claims" }, "not_run"],
    [{ status: "timeout" }, "timed_out"],
    [{ status: "partial", valid: false }, "partial"],
    [{ valid: true }, "unknown"],
    [{ status: "future_status", valid: true }, "unknown"],
  ])("実際の状態を写し、未実行を成功にしない: %j", (raw, execution) => {
    expect(parseAnswerValidation(raw)?.execution).toBe(execution);
    expect(parseAnswerValidation(raw)?.status).toBe("status" in raw ? raw.status : "");
  });
  it("元の理由を保持し、記録が無い場合は表示を作らない", () => {
    expect(parseAnswerValidation({ status: "error", reason: "synthetic_failure" })?.reason).toBe("synthetic_failure");
    expect(parseAnswerValidation(null)).toBeNull();
    expect(validationExecutionBadge(null)).toBeNull();
    expect(validationExecutionBadge("future_status")).toBeNull();
    expect(validationExecutionBadge("failed")?.variant).toBe("danger");
  });
});
