import { describe, expect, it } from "vitest";

import type { DocumentDeleteImpact } from "@/lib/api";
import {
  deleteConfirmDescription,
  deleteImpactDescription,
  summarizeDeleteImpact,
} from "./document-delete-impact";

const kb = (id: string) => ({ id, name: `KB ${id}` });

const impact = (
  document_id: string,
  duplicate_count: number,
  knowledgeBaseIds: string[] = []
): DocumentDeleteImpact => ({
  document_id,
  duplicate_count,
  knowledge_bases: knowledgeBaseIds.map(kb),
});

describe("summarizeDeleteImpact", () => {
  it("重複文書のある正本だけを数え、ナレッジベースは重複を除いて順に並べる", () => {
    expect(
      summarizeDeleteImpact([
        impact("doc-1", 2, ["hr", "legal"]),
        impact("doc-2", 0),
        impact("doc-3", 1, ["legal", "sales"]),
      ])
    ).toEqual({
      sourceCount: 2,
      duplicateCount: 3,
      knowledgeBaseNames: ["KB hr", "KB legal", "KB sales"],
    });
  });
});

describe("deleteImpactDescription", () => {
  it("影響が無ければ null（既存の確認文だけ）", () => {
    expect(deleteImpactDescription(summarizeDeleteImpact([impact("doc-1", 0)]), { bulk: false })).toBeNull();
  });

  it("1 件の削除では、重複文書の件数・消える KB・戻し方を示す", () => {
    const text = deleteImpactDescription(summarizeDeleteImpact([impact("doc-1", 2, ["hr"])]), {
      bulk: false,
    });
    expect(text).toContain("重複文書が 2 件あります");
    expect(text).toContain("ナレッジベース（KB hr）の検索対象からこの内容が消えます");
    expect(text).toContain("ファイル準備を実行してください");
  });

  it("一括削除では、正本の件数と重複文書の合計を示し、KB が多いと残りを件数にする", () => {
    const text = deleteImpactDescription(
      summarizeDeleteImpact([impact("doc-1", 1, ["a", "b"]), impact("doc-2", 2, ["c", "d", "e"])]),
      { bulk: true }
    );
    expect(text).toContain("選択した文書のうち 2 件は、重複文書 3 件の正本です");
    expect(text).toContain("（KB a、KB b、KB c ほか 2 件）");
  });

  it("見られる KB が無いとき（範囲外の重複文書だけ）は KB 名を省く", () => {
    const text = deleteImpactDescription(summarizeDeleteImpact([impact("doc-1", 1)]), {
      bulk: false,
    });
    expect(text).toContain("重複文書が所属するナレッジベースの検索対象から");
    expect(text).not.toContain("（");
  });
});

describe("deleteConfirmDescription", () => {
  it("影響があるときだけ既存の確認文の後ろに続ける", () => {
    expect(deleteConfirmDescription("元に戻せません。", [impact("doc-1", 0)], { bulk: false })).toBe(
      "元に戻せません。"
    );
    expect(
      deleteConfirmDescription("元に戻せません。", [impact("doc-1", 1, ["hr"])], { bulk: false })
    ).toMatch(/^元に戻せません。この文書を正本として参照する重複文書が 1 件あります。/);
  });
});
