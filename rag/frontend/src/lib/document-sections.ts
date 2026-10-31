import type { DocumentSection } from "@/lib/api";

/**
 * 文書の章節の編集（#713）。章節は「並び順 + 階層（1〜6）」の平らな一覧で、親は並びから決まる
 * （Markdown の見出しと同じ）。行の操作は、その行と子（続く、より深い行）をまとめて動かす。
 */

export const MAX_SECTION_LEVEL = 6;

/** `index` の行の子の範囲の終わり（その行を含む部分木の次の位置）。 */
export function subtreeEnd(sections: readonly DocumentSection[], index: number): number {
  const level = sections[index].level;
  let end = index + 1;
  while (end < sections.length && sections[end].level > level) end += 1;
  return end;
}

/** 同じ親の中で 1 つ前の兄弟の位置（無ければ null）。 */
function previousSibling(sections: readonly DocumentSection[], index: number): number | null {
  const level = sections[index].level;
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    if (sections[cursor].level === level) return cursor;
    if (sections[cursor].level < level) return null;
  }
  return null;
}

export function canMoveUp(sections: readonly DocumentSection[], index: number): boolean {
  return previousSibling(sections, index) !== null;
}

export function canMoveDown(sections: readonly DocumentSection[], index: number): boolean {
  const end = subtreeEnd(sections, index);
  return end < sections.length && sections[end].level === sections[index].level;
}

/** 前の兄弟（と子）と入れ替える。 */
export function moveUp(sections: readonly DocumentSection[], index: number): DocumentSection[] {
  const previous = previousSibling(sections, index);
  if (previous === null) return [...sections];
  const end = subtreeEnd(sections, index);
  return [
    ...sections.slice(0, previous),
    ...sections.slice(index, end),
    ...sections.slice(previous, index),
    ...sections.slice(end),
  ];
}

/** 次の兄弟（と子）と入れ替える。 */
export function moveDown(sections: readonly DocumentSection[], index: number): DocumentSection[] {
  if (!canMoveDown(sections, index)) return [...sections];
  const end = subtreeEnd(sections, index);
  return moveUp(sections, end);
}

export function canIndent(sections: readonly DocumentSection[], index: number): boolean {
  const end = subtreeEnd(sections, index);
  const deepest = Math.max(...sections.slice(index, end).map((section) => section.level));
  return previousSibling(sections, index) !== null && deepest < MAX_SECTION_LEVEL;
}

export function canOutdent(sections: readonly DocumentSection[], index: number): boolean {
  return sections[index].level > 1;
}

/** 階層を 1 段下げる（前の兄弟の子にする）。子も一緒に下げる。 */
export function indent(sections: readonly DocumentSection[], index: number): DocumentSection[] {
  if (!canIndent(sections, index)) return [...sections];
  return shiftLevels(sections, index, 1);
}

/** 階層を 1 段上げる。子も一緒に上げる。後ろの兄弟は、そのまま元の親の子に残す。 */
export function outdent(sections: readonly DocumentSection[], index: number): DocumentSection[] {
  if (!canOutdent(sections, index)) return [...sections];
  return shiftLevels(sections, index, -1);
}

function shiftLevels(
  sections: readonly DocumentSection[],
  index: number,
  delta: number
): DocumentSection[] {
  const end = subtreeEnd(sections, index);
  return sections.map((section, position) =>
    position >= index && position < end ? { ...section, level: section.level + delta } : section
  );
}

export function newSection(id: string, level: number): DocumentSection {
  return {
    id,
    title: "",
    level,
    page_start: null,
    page_end: null,
    origin: "manual",
    source_section_id: null,
    edited: false,
  };
}

/** 同じ階層で、その行（と子）の後ろに追加する。 */
export function insertAfter(
  sections: readonly DocumentSection[],
  index: number,
  section: DocumentSection
): DocumentSection[] {
  const end = subtreeEnd(sections, index);
  return [...sections.slice(0, end), { ...section, level: sections[index].level }, ...sections.slice(end)];
}

/** その行の最初の子として追加する。 */
export function insertChild(
  sections: readonly DocumentSection[],
  index: number,
  section: DocumentSection
): DocumentSection[] {
  const level = Math.min(sections[index].level + 1, MAX_SECTION_LEVEL);
  return [...sections.slice(0, index + 1), { ...section, level }, ...sections.slice(index + 1)];
}

/** その行と子を削除する。 */
export function removeSubtree(sections: readonly DocumentSection[], index: number): DocumentSection[] {
  return [...sections.slice(0, index), ...sections.slice(subtreeEnd(sections, index))];
}

export function childCount(sections: readonly DocumentSection[], index: number): number {
  return subtreeEnd(sections, index) - index - 1;
}

/** 名前・ページを変えた行に印を付ける（抽出の章節は、印の無いものだけ抽出のやり直しに合わせる）。 */
export function updateSection(
  section: DocumentSection,
  patch: Partial<Pick<DocumentSection, "title" | "page_start" | "page_end">>
): DocumentSection {
  return { ...section, ...patch, edited: section.origin === "extraction" ? true : section.edited };
}

export type SectionFieldErrors = Partial<Record<"title" | "page_start" | "page_end", string>>;

/** 保存の前に、欄の下に出すエラー（backend の検証と同じ規則）。 */
export function sectionErrors(
  sections: readonly DocumentSection[],
  pageCount: number | null
): Map<string, SectionFieldErrors> {
  const errors = new Map<string, SectionFieldErrors>();
  for (const section of sections) {
    const fieldErrors: SectionFieldErrors = {};
    if (!section.title.trim()) fieldErrors.title = "章節の名前を入力してください。";
    for (const key of ["page_start", "page_end"] as const) {
      const page = section[key];
      if (page !== null && (page < 1 || (pageCount !== null && page > pageCount))) {
        fieldErrors[key] =
          pageCount !== null
            ? `1〜${pageCount} のページを入力してください。`
            : "1 以上のページを入力してください。";
      }
    }
    if (
      !fieldErrors.page_end &&
      section.page_start !== null &&
      section.page_end !== null &&
      section.page_start > section.page_end
    ) {
      fieldErrors.page_end = "終了ページは開始ページ以降にしてください。";
    }
    if (Object.keys(fieldErrors).length > 0) errors.set(section.id, fieldErrors);
  }
  return errors;
}

/** 表示用のページ範囲（「p.3–5」「p.3」）。ページが無ければ null。 */
export function sectionPageLabel(section: Pick<DocumentSection, "page_start" | "page_end">): string | null {
  const { page_start: start, page_end: end } = section;
  if (start === null) return null;
  return end !== null && end !== start ? `p.${start}–${end}` : `p.${start}`;
}


/** 現在の章節へ対応付けられない保存済み要約の件数。推測で別の章節には割り当てない。 */
export function unmatchedSummaryCount(
  sections: readonly DocumentSection[],
  summaries: ReadonlyMap<string, string> | undefined,
): number {
  const sourceIds = new Set(sections.flatMap((section) => section.source_section_id ? [section.source_section_id] : []));
  return [...(summaries?.keys() ?? [])].filter((id) => !sourceIds.has(id)).length;
}
