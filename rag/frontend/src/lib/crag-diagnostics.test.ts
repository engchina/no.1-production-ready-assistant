import { describe, expect, it } from "vitest";

import { parseCragDiagnostics } from "./crag-diagnostics";

describe("CRAG の生成可否と充足の分離", () => {
  it("旧 sufficient=true だけでは必須観点を充足したことにしない", () => {
    expect(parseCragDiagnostics([{ sufficient: true }])).toMatchObject({ generationUsable: true, coverage: "unknown", missingAspects: [], unknownAspects: [] });
    expect(parseCragDiagnostics([])).toBeNull();
    expect(parseCragDiagnostics([{ sufficient: true, missing_aspects: ["procedure"] }])).toMatchObject({ missingAspects: [], unknownAspects: ["procedure"] });
  });
  it("最終判定の不足と未確認を分け、原文を保持する", () => {
    const parsed = parseCragDiagnostics([{ sufficient: false }, {
      generation_usable: true, required_aspect_coverage: "partial", grade_execution: "completed",
      missing_aspects: ["procedure"], unknown_aspects: ["applicability"],
      aspect_checks: [{ aspect: "business_object", status: "supported", source_ids: ["e1"], evidence: [{ source_id: "e1", quote: "対象 A の説明" }], evidence_validation: "matched" }, { aspect: "applicability", status: "unknown" }],
    }]);
    expect(parsed).toMatchObject({ generationUsable: true, coverage: "partial", missingAspects: ["procedure"], unknownAspects: ["applicability"] });
    expect(parsed?.aspects[0]).toMatchObject({ status: "supported", evidence: [{ sourceId: "e1", quote: "対象 A の説明" }], evidenceValidation: "matched" });
    expect(parsed?.aspects[1].status).toBe("unknown");
  });
  it.each([
    { grade_execution: "not_run" }, { recovery_trace: { grade_skipped: {} } }, { grade_error: "timeout" }, { grade_execution: "error" },
  ])("未実行・失敗を全観点確認済みにしない (%j)", (state) => {
    expect(parseCragDiagnostics([{ ...state, sufficient: true, required_aspect_coverage: "complete", aspect_checks: [{ aspect: "procedure", status: "supported" }] }])).toMatchObject({ coverage: "unknown", aspects: [{ status: "unknown" }] });
  });
  it("部分的な判定も全体の充足は未確認のままにする", () => {
    expect(parseCragDiagnostics([{ generation_usable: true, grade_execution: "partial", required_aspect_coverage: "complete" }])).toMatchObject({ execution: "partial", coverage: "unknown" });
  });
});
