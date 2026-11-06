import { describe, expect, it } from "vitest";

import {
  ApiError,
  type EvaluationCaseResult,
  type EvaluationImportResult,
  type EvaluationJob,
} from "@/lib/api";
import { evaluationJobRefetchInterval } from "@/lib/queries";

import {
  buildEvaluationCaseLabels,
  evaluationCaseRow,
  evaluationCaseRowLabel,
  evaluationEvidenceSummary,
  evaluationImportCounts,
  evaluationImportIssueLocation,
  sameIdSet,
} from "./evaluation-input";

const IMPORT: Pick<EvaluationImportResult, "cases" | "rows" | "document_names"> = {
  cases: [
    {
      id: "row-2",
      query: "経費精算の締め日はいつですか？",
      relevant_document_ids: ["doc-expense", "doc-travel"],
      required_evidence: [
        {
          id: "e2",
          document_id: "doc-travel",
          page_start: 4,
          label: "出張旅費規程.docx | p.4",
        },
      ],
      standard_answer: "毎月 25 日です。",
    },
    { id: "row-3", query: "社員食堂の来月の献立は？" },
  ],
  rows: [
    {
      row: 2,
      case_id: "row-2",
      question: "経費精算の締め日はいつですか？",
      evidence: ["経費精算規程.PDF", "出張旅費規程.docx | p.4"],
      standard_answer_preview: "毎月 25 日です。",
      has_standard_answer: true,
      expected_response_decision: "answer",
    },
    {
      row: 3,
      case_id: "row-3",
      question: "社員食堂の来月の献立は？",
      evidence: [],
      standard_answer_preview: null,
      has_standard_answer: false,
      expected_response_decision: "hold",
    },
  ],
  document_names: { "doc-expense": "経費精算規程.pdf", "doc-travel": "出張旅費規程.docx" },
};

describe("evaluationCaseRow", () => {
  it("Excel から作ったケースは行で示し、それ以外は id のまま", () => {
    expect(evaluationCaseRow("row-12")).toBe(12);
    expect(evaluationCaseRowLabel("row-12")).toBe("12 行目");
    expect(evaluationCaseRow("legacy-case")).toBeNull();
    expect(evaluationCaseRowLabel("legacy-case")).toBe("legacy-case");
  });
});

describe("Excel の検証の結果の表示", () => {
  it("誤りの位置は行と列で示す", () => {
    expect(evaluationImportIssueLocation({ row: 3, column: "期待する根拠", message: "x" })).toBe(
      "3 行目・期待する根拠"
    );
    expect(evaluationImportIssueLocation({ row: 3, column: null, message: "x" })).toBe("3 行目");
    expect(evaluationImportIssueLocation({ row: null, column: null, message: "x" })).toBe("ファイル全体");
  });

  it("件数の内訳を数える", () => {
    expect(evaluationImportCounts(IMPORT)).toEqual({
      total: 2,
      answerable: 1,
      unanswerable: 1,
      withStandardAnswer: 1,
    });
  });

  it("ナレッジベースの集合の違いは順序を問わない", () => {
    expect(sameIdSet(["a", "b"], ["b", "a"])).toBe(true);
    expect(sameIdSet(["a"], ["a", "b"])).toBe(false);
  });
});

describe("期待する根拠の照合の結果", () => {
  const labels = buildEvaluationCaseLabels(IMPORT);
  const result = (overrides: Partial<EvaluationCaseResult>) =>
    ({
      status: "success",
      mrr: 0.5,
      required_evidence_recall: 0,
      hit_document_ids: ["doc-expense"],
      missing_evidence: ["e2"],
      ...overrides,
    }) as EvaluationCaseResult;

  it("見出しは Excel の根拠の行ごとに文書と必要な根拠の id を持つ", () => {
    expect(labels["row-2"]).toEqual({
      question: "経費精算の締め日はいつですか？",
      evidence: [
        { label: "経費精算規程.PDF", documentId: "doc-expense", evidenceId: null },
        { label: "出張旅費規程.docx | p.4", documentId: "doc-travel", evidenceId: "e2" },
      ],
    });
    expect(labels["row-3"]?.evidence).toEqual([]);
  });

  it("ファイル名だけは引用の文書、条件つきは必要な根拠で判定する", () => {
    const summary = evaluationEvidenceSummary(result({}), labels["row-2"]);
    expect(summary).toMatchObject({ measured: true, found: 1, total: 2 });
    expect(summary?.items.map((item) => item.found)).toEqual([true, false]);

    const all = evaluationEvidenceSummary(
      result({ required_evidence_recall: 1, missing_evidence: [], hit_document_ids: ["doc-expense", "doc-travel"] }),
      labels["row-2"]
    );
    expect(all).toMatchObject({ found: 2, total: 2 });
  });

  it("測れないケースと根拠の無いケース", () => {
    expect(
      evaluationEvidenceSummary(result({ mrr: null, required_evidence_recall: null }), labels["row-2"])
    ).toMatchObject({ measured: false, found: 0 });
    expect(evaluationEvidenceSummary(result({ status: "error" }), labels["row-2"])?.measured).toBe(false);
    expect(evaluationEvidenceSummary(result({}), labels["row-3"])).toBeNull();
    expect(evaluationEvidenceSummary(result({}), undefined)).toBeNull();
  });
});

describe("evaluationJobRefetchInterval", () => {
  const running = { status: "RUNNING" } as EvaluationJob;

  it("実行中は取得の失敗の後も取得を続け、404 と終わった job では止める", () => {
    expect(evaluationJobRefetchInterval(running)).toBeGreaterThan(0);
    expect(evaluationJobRefetchInterval(running, new ApiError(503, ["x"]))).toBeGreaterThan(0);
    expect(evaluationJobRefetchInterval(running, new TypeError("Failed to fetch"))).toBeGreaterThan(0);
    expect(evaluationJobRefetchInterval(running, new ApiError(404, ["x"]))).toBe(false);
    expect(evaluationJobRefetchInterval({ status: "SUCCEEDED" } as EvaluationJob)).toBe(false);
    expect(evaluationJobRefetchInterval(undefined)).toBe(false);
  });
});
