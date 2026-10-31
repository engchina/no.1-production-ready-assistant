import { afterEach, describe, expect, it, vi } from "vitest";
import { diagnosticTimestamp, logBrowserDiagnostic } from "../src/lib/diagnostic-log";

afterEach(() => vi.restoreAllMocks());

describe("ブラウザの安全な診断", () => {
  it("JST の timezone を明示し、UTC の epoch を変えない", () => {
    const instant = new Date("2026-10-03T00:01:02.345Z");
    const timestamp = diagnosticTimestamp(instant);
    expect(timestamp).toBe("2026-10-03T09:01:02.345+09:00");
    expect(new Date(timestamp).getTime()).toBe(instant.getTime());
  });

  it("API の相関を残し、Error 原文・stack・余分な属性を出さない", () => {
    const output = vi.spyOn(console, "error").mockImplementation(() => {});
    const error = Object.assign(new Error("PRIVATE_ANSWER_SENTINEL"), { requestId: "request-1", token: "PRIVATE_TOKEN_SENTINEL" });
    logBrowserDiagnostic("ERROR", { event: "card_render_failed", serviceName: "production-ready-rag", error });
    const wire = output.mock.calls[0][0] as string;
    expect(wire).not.toContain("SENTINEL");
    expect(JSON.parse(wire)).toMatchObject({ level: "ERROR", request_id: "request-1", exception_type: "Error" });
    expect(wire).not.toContain("stack");
  });

  it("改行のある request ID を捨て、危険な getter と console failure でも throw しない", () => {
    const output = vi.spyOn(console, "warn").mockImplementation(() => {});
    logBrowserDiagnostic("WARNING", { event: "unsaved_changes_blocker_missing", serviceName: "production-ready-platform", requestId: "injected\nline" });
    expect(JSON.parse(output.mock.calls[0][0] as string)).not.toHaveProperty("request_id");
    const error = Object.defineProperty({}, "requestId", { get: () => { throw new Error("PRIVATE"); } });
    expect(() => logBrowserDiagnostic("WARNING", { event: "unsaved_changes_blocker_missing", serviceName: "production-ready-platform", error })).not.toThrow();
    output.mockImplementation(() => { throw new Error("console failure"); });
    expect(() => logBrowserDiagnostic("WARNING", { event: "unsaved_changes_blocker_missing", serviceName: "production-ready-platform" })).not.toThrow();
  });
});
