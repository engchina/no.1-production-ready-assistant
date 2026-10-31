import { describe, expect, it } from "vitest";

import {
  citationAnswerUse,
  citationCountLabel,
  citationMetadataChips,
  citationModelUsed,
} from "./chunk-metadata";

const sourceChips = (metadata: Parameters<typeof citationMetadataChips>[0]) => citationMetadataChips(metadata).filter(chip => chip.id !== "context_header_enabled");

describe("citationMetadataChips", () => {
  it("取込済みの文書に残った XML ツリー JSON の引用ラベルは chip にしない（issue 42）", () => {
    expect(
      sourceChips({
        xml_tree_citation_label_arabic: "サンプル規程 第7条第1項",
        section_path: "サンプル規程 > 第七条（対象の範囲）",
      })
    ).toEqual([{ id: "section_path", value: "サンプル規程 > 第七条（対象の範囲）" }]);
  });

  it("ページ範囲と構造 metadata を chip 化する", () => {
    expect(
      sourceChips({
        page_start: 2,
        page_end: 4,
        content_kind: "table",
        section_title: "料金表",
        section_path: "契約 > 料金表",
        chunk_profile: "structure_v1",
      })
    ).toEqual([
      { id: "page", value: "2-4" },
      { id: "content_kind", value: "table" },
      { id: "section_title", value: "料金表" },
      { id: "section_path", value: "契約 > 料金表" },
      { id: "chunk_profile", value: "structure_v1" },
    ]);
  });

  it("資料に印刷されたページ番号があれば物理頁と並べる（issue 1287）", () => {
    expect(sourceChips({ page_start: 12, page_label_start: "3-4" })).toEqual([
      { id: "page", value: "12", printed: "3-4" },
    ]);
    expect(
      sourceChips({
        page_start: 12,
        page_end: 13,
        page_label_start: "3-4",
        page_label_end: "3-5",
      })
    ).toEqual([{ id: "page", value: "12-13", printed: "3-4〜3-5" }]);
    // 印刷の番号が無い・空・物理頁と同じ表記なら物理頁だけ。
    expect(sourceChips({ page_start: 12 })).toEqual([{ id: "page", value: "12" }]);
    expect(sourceChips({ page_start: 12, page_label_start: " " })).toEqual([
      { id: "page", value: "12" },
    ]);
    expect(sourceChips({ page_start: 12, page_label_start: "12" })).toEqual([
      { id: "page", value: "12" },
    ]);
    // 物理頁が無ければ印刷の番号だけでは出さない（MCP の locator と同じ）。
    expect(sourceChips({ page_label_start: "ii" })).toEqual([]);
  });

  it("欠損値や非文字列 metadata は表示対象にしない", () => {
    expect(
      sourceChips({
        page_start: null,
        page_end: 3,
        content_kind: "",
        chunk_profile: true,
      })
    ).toEqual([]);
  });
});

// AI に渡した件数と内訳の表示（#1208・#2040）。
describe("citationCountLabel", () => {
  const chunk = (metadata: Record<string, boolean | string>) => ({ metadata });

  it("まとまり（親）だけを参照した箇所は「回答で引用」に数えず、「まとまりで参照」に分ける（issue 2710）", () => {
    expect(
      citationCountLabel([
        chunk({ evidence_model_used: true }),
        chunk({ evidence_model_used: false, evidence_parent_used: true }),
        chunk({ evidence_model_used: false }),
      ])
    ).toBe("AI に渡した 3 件 ／ 回答で引用 1 件 ／ まとまりで参照 1 件");
  });

  it("回答で引用したかが分かるときは、使用した件数を添える", () => {
    expect(
      citationCountLabel([
        chunk({ evidence_model_used: true }),
        chunk({ evidence_model_used: false }),
        chunk({ evidence_model_used: false }),
      ])
    ).toBe("AI に渡した 3 件 ／ 回答で引用 1 件");
    // 0 件の項目は出さない。
    expect(citationCountLabel([chunk({ evidence_model_used: false })])).toBe("AI に渡した 1 件");
  });

  it("回答での使われ方が分かるときは、該当を確認した件数と参考の件数を添える", () => {
    const chunks = [
      chunk({ evidence_model_used: true, evidence_answer_use: "confirmed" }),
      chunk({ evidence_model_used: true, evidence_answer_use: "confirmed" }),
      chunk({ evidence_model_used: true, evidence_answer_use: "reference" }),
      // 回答で引用していない箇所の使われ方は数えない。
      chunk({ evidence_model_used: false, evidence_answer_use: "confirmed" }),
    ];
    expect(citationCountLabel(chunks)).toBe(
      "AI に渡した 4 件 ／ 回答で引用 3 件 ／ うち該当確認済み 2 件 ／ 参考 1 件"
    );
    expect(chunks.map(citationAnswerUse)).toEqual(["confirmed", "confirmed", "reference", null]);
    expect(
      citationCountLabel([chunk({ evidence_model_used: true, evidence_answer_use: "reference" })])
    ).toBe("AI に渡した 1 件 ／ 回答で引用 1 件 ／ 参考 1 件");
  });

  it("回答を作らない検索の結果（evidence_model_used が無い）は件数だけ", () => {
    expect(citationCountLabel([chunk({}), chunk({ evidence_role: "anchor" })])).toBe("AI に渡した 2 件");
    expect(citationModelUsed(chunk({ evidence_model_used: "true" }))).toBeNull();
  });
});


 it("文脈ヘッダの実値と有効・無効・未記録を区別する", () => {
    expect(citationMetadataChips({context_header: "文書 A > 上位 > 下位", context_header_enabled: true})).toEqual([
      {id: "context_header", value: "文書 A > 上位 > 下位"}, {id: "context_header_enabled", value: "true"}
    ]);
    expect(citationMetadataChips({context_header_enabled: false})).toEqual([{id: "context_header_enabled", value: "false"}]);
    expect(citationMetadataChips({})).toEqual([{id: "context_header_enabled", value: "unknown"}]);
 });


describe("citation support display", () => {
  it.each(["not_run", "partial", "error", "completed"])("%s の unknown を確認済みと数えず、回答使用数は保つ", (execution) => {
    const chunk = { metadata: { evidence_model_used: true, evidence_answer_use: "confirmed", evidence_claim_support: { execution, status: "unknown" } } };
    expect(citationAnswerUse(chunk)).toBeNull();
    expect(citationCountLabel([chunk])).toContain("回答で引用 1 件");
    expect(citationCountLabel([chunk])).not.toContain("該当確認済み");
    expect(citationCountLabel([chunk])).not.toContain("参考");
  });
  it("記録された支持と旧記録を区別する", () => {
    const metadata = { evidence_model_used: true, evidence_answer_use: "confirmed" };
    expect(citationAnswerUse({ metadata })).toBe("confirmed");
    expect(citationAnswerUse({ metadata: { ...metadata, evidence_claim_support: { execution: "completed", status: "supported" } } })).toBe("confirmed");
  });
});
