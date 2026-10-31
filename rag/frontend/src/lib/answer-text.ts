import type { RetrievedChunk } from "./api";
import { integerMetadataValue } from "./table-cell-focus";

/**
 * 回答本文の構成（#651）。回答エンジン（rag_engine の `grounded.render()`）は本文を決まった書式で組み立てる:
 * 要約の段落、節の見出し（「資料により確認できた事項」「操作手順（機能名）」など）、「・」か「N. 」で始まる説明、
 * 説明の直後の「根拠：ファイル名 p.N」の行。画面はこの書式で節・説明・根拠に分けて出す。
 * 書式の見出しが 1 つも無い本文（根拠不足の案内など）は null（そのままの文字列で出す）。
 */

export type AnswerTextEntry =
  /** `citationLines` は各根拠の行が本文の何番目（1 始まり）の「根拠：」の行か（#1330）。 */
  | { kind: "item"; text: string; citations: string[]; citationLines: number[] }
  | { kind: "text"; text: string };

export type AnswerTextBlock =
  | { kind: "paragraph"; text: string }
  | { kind: "section"; title: string; ordered: boolean; entries: AnswerTextEntry[] };

// rag_engine の grounded.py の RULES_SECTION_TITLE / QUOTE_ONLY_LABEL / GAPS_SECTION_TITLE /
// UNVERIFIED_SECTION_TITLE / CONFIRMATIONS_SECTION_TITLE と手順の見出し。「参考情報」の直下の
// 固定の説明（QUOTE_ONLY_NOTE）は箇条でないので、節の中の文として出す。
// 後ろの 3 つは #2040 より前に保存した回答の見出し（見出しが読めないと、参考の原文が手順の節に混ざって見えるため）。
const SECTION_TITLE = new RegExp(
  "^(?:" +
    [
      "資料により確認できた事項",
      "根拠で確かめきれていない事項",
      "参考情報",
      "資料から確認できなかった事項",
      "回答に未反映の関連章節",
      "回答時間の上限により省略した処理",
      "回答を確定するために確認すること",
      "(?:操作|確認)手順(?:（.+）)?",
      "確認できる内容",
      "資料の記載（今回への適用は未確認）",
      "資料からは確認できない点",
    ].join("|") +
    ")$",
);
const CITATION = /^根拠：/;
const BULLET = /^・/;
const NUMBERED = /^\d+\.\s/;

export type AnswerFormat = Readonly<Record<string, string>>;

/** 生成時に保存した表示形式。現在の言語設定は参照しない。 */
export function answerFormat(diagnostics: unknown): AnswerFormat | undefined {
  if (!diagnostics || typeof diagnostics !== "object") return undefined;
  const value = (diagnostics as Record<string, unknown>).answer_format;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const entries = Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === "string");
  return entries.length ? Object.fromEntries(entries) : undefined;
}

function escapePattern(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function templatePattern(template: string, values: Record<string, string>): string {
  return template.split(/(\{\w+\})/).map((part) =>
    part.startsWith("{") && part.endsWith("}")
      ? (values[part.slice(1, -1)] ?? ".+?")
      : escapePattern(part)
  ).join("");
}

function sectionTitle(line: string, format?: AnswerFormat): boolean {
  if (!format) return SECTION_TITLE.test(line);
  if (Object.entries(format).some(([key, value]) => key.endsWith("_section") && value === line)) return true;
  return [format.operation_steps_heading, format.confirmation_steps_heading].some((heading) =>
    heading && (line === heading || new RegExp("^" + templatePattern(
      format.steps_heading_template ?? "{heading}（{title}）", { heading: escapePattern(heading), title: ".+" }
    ) + "$").test(line))
  ) || Boolean(format.subquestion_heading_template && new RegExp("^" + templatePattern(
    format.subquestion_heading_template, { label: ".+?", text: ".+" }
  ) + "$").test(line));
}

export function parseAnswerText(text: string, format?: AnswerFormat): AnswerTextBlock[] | null {
  const lines = text.split("\n");
  const citation = format?.citation_prefix ? new RegExp("^" + escapePattern(format.citation_prefix)) : CITATION;
  const bullet = format?.item_bullet ? new RegExp("^" + escapePattern(format.item_bullet)) : BULLET;
  if (!lines.some((line) => sectionTitle(line.trim(), format))) return null;
  const blocks: AnswerTextBlock[] = [];
  let section: Extract<AnswerTextBlock, { kind: "section" }> | null = null;
  let paragraph: string[] = [];
  // 本文の「根拠：」の行の順番（1 始まり）。回答エンジンが出典行ごとの根拠に付ける番号と同じ数え方（#1330）。
  let citationOrdinal = 0;
  const flushParagraph = () => {
    const joined = paragraph.join("\n").trim();
    paragraph = [];
    if (!joined) return;
    if (section) section.entries.push({ kind: "text", text: joined });
    else blocks.push({ kind: "paragraph", text: joined });
  };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      flushParagraph();
      continue;
    }
    if (citation.test(line)) citationOrdinal += 1;
    if (sectionTitle(line, format)) {
      flushParagraph();
      section = { kind: "section", title: line, ordered: true, entries: [] };
      blocks.push(section);
      continue;
    }
    if (section && (bullet.test(line) || NUMBERED.test(line))) {
      flushParagraph();
      if (!NUMBERED.test(line)) section.ordered = false;
      section.entries.push({
        kind: "item",
        text: line.replace(bullet, "").replace(NUMBERED, ""),
        citations: [],
        citationLines: [],
      });
      continue;
    }
    const last = section?.entries[section.entries.length - 1];
    if (citation.test(line) && last?.kind === "item" && paragraph.length === 0) {
      last.citations.push(line);
      last.citationLines.push(citationOrdinal);
      continue;
    }
    paragraph.push(line);
  }
  flushParagraph();
  for (const block of blocks) {
    if (block.kind === "section" && !block.entries.some((entry) => entry.kind === "item")) {
      block.ordered = false;
    }
  }
  return blocks;
}

/**
 * 根拠の行（「根拠：ファイル名 p.N」、表計算は「根拠：ファイル名 シート「名前」A3:D6」。rag_engine の
 * grounded.py の `_citation`）のファイル名と頁・シート・行（#657 / #1224）。
 */
export interface AnswerCitationRef {
  fileName: string;
  page: number | null;
  /** 表計算のシート名（#1224）。頁の根拠では null。 */
  sheet?: string | null;
  /** シートの行の範囲（セル範囲から読む）。 */
  rowStart?: number | null;
  rowEnd?: number | null;
}

const CITATION_LINE = /^根拠：(.+?)(?:\s+p\.(\d*))?$/;
const SHEET_CITATION_LINE = /^根拠：(.+?)\s+シート「(.+)」(?:[A-Z]+(\d+):[A-Z]+(\d+))?$/;

/**
 * 根拠の行のファイル名の後ろの位置（「p.3」「シート「名前」A3:D6」）。押せる根拠の行で長いファイル名を省略しても、
 * 位置は省略せずに出すのに使う（#3353）。ファイル名が読めない・位置が無いときは undefined。
 */
export function citationLocation(line: string, fileName: string | null | undefined): string | undefined {
  if (!fileName) return undefined;
  const value = line.trim();
  const at = value.lastIndexOf(fileName);
  if (at < 0) return undefined;
  return value.slice(at + fileName.length).trim() || undefined;
}

export function parseCitationLine(line: string, format?: AnswerFormat): AnswerCitationRef | null {
  if (format?.citation_prefix) {
    const value = line.trim();
    if (!value.startsWith(format.citation_prefix)) return null;
    const body = value.slice(format.citation_prefix.length);
    const sheetPattern = templatePattern(format.sheet_location ?? 'シート「{sheet}」{cell_range}', {
      sheet: "(?<sheet>.+)", cell_range: "(?<cells>[A-Z]+[0-9]+:[A-Z]+[0-9]+)?",
    });
    const sheet = new RegExp("^(?<file>.+?)\\s+" + sheetPattern + "$").exec(body)?.groups;
    if (sheet) {
      const rows = sheet.cells?.match(/[0-9]+/g);
      return { fileName: sheet.file.trim(), page: null, sheet: sheet.sheet,
        rowStart: rows?.[0] ? Number(rows[0]) : null, rowEnd: rows?.[1] ? Number(rows[1]) : null };
    }
    const pagePattern = templatePattern(format.page_location ?? "p.{page}", { page: "(?<page>[0-9]*)" });
    const page = new RegExp("^(?<file>.+?)\\s+" + pagePattern + "$").exec(body)?.groups;
    if (page) return { fileName: page.file.trim(), page: page.page ? Number(page.page) : null };
    return body.trim() ? { fileName: body.trim(), page: null } : null;
  }
  const trimmed = line.trim();
  const sheet = SHEET_CITATION_LINE.exec(trimmed);
  if (sheet) {
    const fileName = sheet[1].trim();
    if (!fileName) return null;
    return {
      fileName,
      page: null,
      sheet: sheet[2],
      rowStart: sheet[3] ? Number(sheet[3]) : null,
      rowEnd: sheet[4] ? Number(sheet[4]) : null,
    };
  }
  const match = CITATION_LINE.exec(trimmed);
  if (!match) return null;
  const fileName = match[1].trim();
  if (!fileName) return null;
  return { fileName, page: match[2] ? Number(match[2]) : null };
}

function normalizedFileName(value: string): string {
  return value.normalize("NFKC").trim().toLowerCase();
}

function chunkPages(chunk: RetrievedChunk): [number, number] {
  // backend の _stored_child と同じ頁（page_start、無ければ page_number、どちらも無ければ 1）。
  const start =
    integerMetadataValue(chunk.metadata.page_start) ??
    integerMetadataValue(chunk.metadata.page_number) ??
    1;
  const end = integerMetadataValue(chunk.metadata.page_end) ?? start;
  return [start, Math.max(start, end)];
}

/**
 * 根拠の行に当たる引用の位置（#657）。同じファイルのうち、頁の範囲に入る引用を順位の順に選び、無ければ
 * 頁の近い引用にする。同じファイルの引用が無ければ -1（押せる見た目にしない）。
 */
export function matchCitation(ref: AnswerCitationRef, citations: readonly RetrievedChunk[]): number {
  const target = normalizedFileName(ref.fileName);
  const candidates = citations
    .map((chunk, index) => ({ chunk, index }))
    .filter(({ chunk }) => normalizedFileName(chunk.file_name ?? "") === target);
  if (!candidates.length) return -1;
  if (ref.sheet) return matchSheetCitation(ref, candidates);
  const page = ref.page;
  if (page == null) return candidates[0].index;
  let best = candidates[0];
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const candidate of candidates) {
    const [start, end] = chunkPages(candidate.chunk);
    const distance = page < start ? start - page : page > end ? page - end : 0;
    if (distance < bestDistance) {
      best = candidate;
      bestDistance = distance;
    }
  }
  return best.index;
}

/** 表計算の根拠の行に当たる引用（同じシートで、行の範囲が重なる引用を順位の順に。#1224）。 */
function matchSheetCitation(
  ref: AnswerCitationRef,
  candidates: { chunk: RetrievedChunk; index: number }[]
): number {
  const sameSheet = candidates.filter(({ chunk }) => chunk.metadata.sheet_name === ref.sheet);
  if (!sameSheet.length) return candidates[0].index;
  const start = ref.rowStart ?? null;
  const end = ref.rowEnd ?? start;
  if (start == null || end == null) return sameSheet[0].index;
  const overlapping = sameSheet.find(({ chunk }) => {
    const chunkStart = integerMetadataValue(chunk.metadata.row_start);
    const chunkEnd = integerMetadataValue(chunk.metadata.row_end) ?? chunkStart;
    return chunkStart != null && chunkEnd != null && chunkStart <= end && start <= chunkEnd;
  });
  return (overlapping ?? sameSheet[0]).index;
}

/**
 * 本文の根拠の行が開く引用の位置の決まり方（#2710）。`exact` は行の主張を支える箇所と確定した引用。
 * `unresolved` は確定できない引用で、`candidates` は候補の引用の位置（引用の並び順）。`reason` は
 * `candidates`（回答エンジンが候補として残した）か `legacy`（行と箇所の対応を持たない・以前の回答の記録で、
 * 同じ文書・頁の箇所が複数ある）。
 */
export type CitationPosition =
  | { kind: "exact" }
  | { kind: "unresolved"; reason: "candidates" | "legacy"; candidates: number[] };

export interface CitationTarget {
  /** 開く引用の位置（`citations` の index）。 */
  index: number;
  position: CitationPosition;
}

// 回答エンジンが出典行の主張を支える子と確定した番号の印（backend の ANSWER_CITATION_PROVENANCE_CHILD。#2710）。
const PROVENANCE_CHILD = "child";

function lineNumbers(value: unknown): number[] {
  return Array.isArray(value) ? value.filter((item): item is number => typeof item === "number") : [];
}

/**
 * 同じ文書・同じ頁に重なる引用の位置（自分を含む。表計算の行の引用は行で決まるので自分だけ）。以前の回答の
 * 番号・文書名と頁の対応で選んだ引用が、同じ頁の別の箇所と取り違えうるかを見る（#2710）。
 */
function samePlaceCitations(index: number, citations: readonly RetrievedChunk[]): number[] {
  const target = citations[index];
  if (typeof target.metadata.sheet_name === "string" && target.metadata.sheet_name) return [index];
  const name = normalizedFileName(target.file_name ?? "");
  const [start, end] = chunkPages(target);
  return citations.flatMap((chunk, position) => {
    if (normalizedFileName(chunk.file_name ?? "") !== name) return [];
    if (typeof chunk.metadata.sheet_name === "string" && chunk.metadata.sheet_name) return [];
    const [chunkStart, chunkEnd] = chunkPages(chunk);
    return chunkStart <= end && start <= chunkEnd ? [position] : [];
  });
}

function fromMatch(index: number, citations: readonly RetrievedChunk[]): CitationTarget {
  const candidates = samePlaceCitations(index, citations);
  return candidates.length > 1
    ? { index, position: { kind: "unresolved", reason: "legacy", candidates } }
    : { index, position: { kind: "exact" } };
}

/**
 * 本文の n 番目（1 始まり）の根拠の行が開く引用（#1330・#2710）。
 *
 * 回答エンジンが行の主張を支える箇所と確定した引用（`answer_citation_lines` と印 `answer_citation_provenance`）が
 * あれば、それを正確な位置として開く。確定できなかった行は、候補（`answer_citation_candidate_lines`）の先頭を開き、
 * 位置は未確定とする（別の箇所を正しい位置として強調しない）。印の無い番号（以前の回答の記録）と、番号の無い
 * 回答のファイル名と頁による対応は、同じ文書・頁に別の箇所があれば未確定とする（保存した対応は書き換えない）。
 * 当たる引用が無ければ null。
 */
export function resolveCitationLine(
  ordinal: number | undefined,
  ref: AnswerCitationRef | null,
  citations: readonly RetrievedChunk[],
): CitationTarget | null {
  if (ordinal != null) {
    const index = citations.findIndex((chunk) => lineNumbers(chunk.metadata.answer_citation_lines).includes(ordinal));
    if (index >= 0) {
      return citations[index].metadata.answer_citation_provenance === PROVENANCE_CHILD
        ? { index, position: { kind: "exact" } }
        : fromMatch(index, citations);
    }
    const candidates = citations.flatMap((chunk, position) =>
      lineNumbers(chunk.metadata.answer_citation_candidate_lines).includes(ordinal) ? [position] : [],
    );
    if (candidates.length) {
      return { index: candidates[0], position: { kind: "unresolved", reason: "candidates", candidates } };
    }
  }
  if (!ref) return null;
  const index = matchCitation(ref, citations);
  return index < 0 ? null : fromMatch(index, citations);
}

/** 一つの出典行が持つ全確定箇所と、別に残る未確定の候補を返す。旧本文の項目は再配置しない。 */
export function resolveCitationLineTargets(
  ordinal: number | undefined,
  ref: AnswerCitationRef | null,
  citations: readonly RetrievedChunk[],
): CitationTarget[] {
  const seen = new Set<string>();
  const exact = ordinal == null ? [] : citations.flatMap((chunk, index): CitationTarget[] => {
    if (chunk.metadata.answer_citation_provenance !== PROVENANCE_CHILD
      || !lineNumbers(chunk.metadata.answer_citation_lines).includes(ordinal)) return [];
    const key = chunk.chunk_id ? `${chunk.document_id}:${chunk.chunk_id}` : String(index);
    if (seen.has(key)) return [];
    seen.add(key);
    return [{ index, position: { kind: "exact" } }];
  });
  if (!exact.length) {
    const fallback = resolveCitationLine(ordinal, ref, citations);
    return fallback ? [fallback] : [];
  }
  const confirmed = new Set(exact.map((target) => target.index));
  const candidates = citations.flatMap((chunk, index) =>
    !confirmed.has(index) && ordinal != null
      && lineNumbers(chunk.metadata.answer_citation_candidate_lines).includes(ordinal) ? [index] : [],
  );
  return candidates.length
    ? [...exact, { index: candidates[0], position: { kind: "unresolved", reason: "candidates", candidates } }]
    : exact;
}

/** {@link resolveCitationLine} の開く引用の位置だけ（当たる引用が無ければ -1）。 */
export function matchCitationLine(
  ordinal: number | undefined,
  ref: AnswerCitationRef | null,
  citations: readonly RetrievedChunk[],
): number {
  return resolveCitationLine(ordinal, ref, citations)?.index ?? -1;
}
