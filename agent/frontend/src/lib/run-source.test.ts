import { describe, expect, it } from "vitest";

import { runSourceLabel, runSourceLabelOf, runSourceOf } from "@/lib/run-source";

describe("Run のきっかけ", () => {
  it("backend の run_source と同じ順で決める", () => {
    expect(runSourceOf({})).toBe("user");
    expect(runSourceOf({ evaluation_job_id: "j", automation_id: "a" })).toBe("evaluation");
    expect(runSourceOf({ automation_id: "a", source: "mcp" })).toBe("automation");
    expect(runSourceOf({ source: "mcp" })).toBe("mcp");
    expect(runSourceOf({ replayed_from_run_id: "r" })).toBe("replay");
  });

  it("MCP の Run は API キーの名前を添える", () => {
    expect(runSourceLabelOf({ source: "mcp", api_key_name: "会社の PC" })).toBe("MCP（会社の PC）");
    expect(runSourceLabelOf({ source: "mcp" })).toBe("MCP");
    expect(runSourceLabel("automation")).toBe("自動実行");
    expect(runSourceLabel("unknown")).toBe("画面");
  });
});
