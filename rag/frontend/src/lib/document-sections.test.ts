import { describe, expect, it } from "vitest";

import type { DocumentSection } from "@/lib/api";
import {
  canIndent,
  canMoveDown,
  canMoveUp,
  childCount,
  indent,
  insertAfter,
  insertChild,
  moveDown,
  moveUp,
  newSection,
  outdent,
  removeSubtree,
  sectionErrors,
  sectionPageLabel,
  updateSection,
  unmatchedSummaryCount,
} from "./document-sections";

function section(id: string, level: number, pages: [number, number] | null = null): DocumentSection {
  return {
    ...newSection(id, level),
    title: id,
    page_start: pages?.[0] ?? null,
    page_end: pages?.[1] ?? null,
  };
}

// A(1) ─ A1(2) ─ A-1-a(3) / A2(2) / B(1)
const outline = [section("A", 1), section("A-1", 2), section("A-1-a", 3), section("A-2", 2), section("B", 1)];
const shape = (sections: DocumentSection[]) => sections.map((s) => `${s.id}:${s.level}`);

describe("章節の一覧の操作", () => {
  it("上へ・下へは兄弟の間だけで、子も一緒に動かす", () => {
    expect(canMoveUp(outline, 1)).toBe(false);
    expect(canMoveDown(outline, 1)).toBe(true);
    expect(shape(moveDown(outline, 1))).toEqual(["A:1", "A-2:2", "A-1:2", "A-1-a:3", "B:1"]);
    expect(shape(moveUp(outline, 4))).toEqual(["B:1", "A:1", "A-1:2", "A-1-a:3", "A-2:2"]);
    expect(canMoveDown(outline, 4)).toBe(false);
  });

  it("階層の上げ下げは子も一緒に動かし、前の兄弟が無ければ下げない", () => {
    expect(canIndent(outline, 0)).toBe(false);
    expect(canIndent(outline, 3)).toBe(true);
    expect(shape(indent(outline, 3))).toEqual(["A:1", "A-1:2", "A-1-a:3", "A-2:3", "B:1"]);
    expect(shape(outdent(outline, 1))).toEqual(["A:1", "A-1:1", "A-1-a:2", "A-2:2", "B:1"]);
  });

  it("後ろに追加は子の後ろに同じ階層で、子として追加は直後に 1 段深く入れる", () => {
    expect(shape(insertAfter(outline, 1, newSection("N", 9)))).toEqual([
      "A:1", "A-1:2", "A-1-a:3", "N:2", "A-2:2", "B:1",
    ]);
    expect(shape(insertChild(outline, 4, newSection("N", 1)))).toEqual([
      "A:1", "A-1:2", "A-1-a:3", "A-2:2", "B:1", "N:2",
    ]);
  });

  it("削除は子も含めて消す", () => {
    expect(childCount(outline, 0)).toBe(3);
    expect(shape(removeSubtree(outline, 0))).toEqual(["B:1"]);
  });

  it("抽出の章節の名前・ページを変えたら印を付ける", () => {
    const extracted: DocumentSection = { ...section("E", 1), origin: "extraction" };
    expect(updateSection(extracted, { title: "改名" }).edited).toBe(true);
    expect(updateSection(section("M", 1), { title: "改名" }).edited).toBe(false);
  });

  it("名前の空欄・ページの範囲外・開始と終了の逆転をエラーにする", () => {
    const errors = sectionErrors(
      [
        { ...section("a", 1, [3, 2]) },
        { ...section("b", 1, [1, 9]) },
        { ...section("c", 1), title: " " },
      ],
      6
    );
    expect(errors.get("a")).toEqual({ page_end: "終了ページは開始ページ以降にしてください。" });
    expect(errors.get("b")).toEqual({ page_end: "1〜6 のページを入力してください。" });
    expect(errors.get("c")).toEqual({ title: "章節の名前を入力してください。" });
  });

  it("ページ範囲は 1 ページなら p.N、範囲なら p.N–M", () => {
    expect(sectionPageLabel({ page_start: 3, page_end: 5 })).toBe("p.3–5");
    expect(sectionPageLabel({ page_start: 3, page_end: 3 })).toBe("p.3");
    expect(sectionPageLabel({ page_start: null, page_end: null })).toBeNull();
  });
});


describe("章節と保存済み要約の対応", () => {
  const summaries = new Map([["nav-a", "要約 A"], ["nav-b", "要約 B"]]);
  it("対応しない要約を名前やページの類似で割り当てず件数で知らせる", () => {
    const sections = [{ ...section("rule-a", 1), source_section_id: "rule-a" }];
    expect(unmatchedSummaryCount(sections, summaries)).toBe(2);
  });
  it("人が修正した章節も元の章節 ID で対応させる", () => {
    const sections = [{ ...section("manual-a", 1), source_section_id: "nav-a" }];
    expect(unmatchedSummaryCount(sections, summaries)).toBe(1);
    expect(unmatchedSummaryCount([...sections, { ...section("b", 1), source_section_id: "nav-b" }], summaries)).toBe(0);
  });
  it("要約が無効で保存されていないときは未対応の要約も無い", () => {
    expect(unmatchedSummaryCount(outline, undefined)).toBe(0);
    expect(unmatchedSummaryCount(outline, new Map())).toBe(0);
  });
});
