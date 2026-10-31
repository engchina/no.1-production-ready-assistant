import {
  matchAnswerPassageMarks,
  type AnswerPassageLabels,
  type AnswerPassageRecords,
} from "@production-ready/ui";

import type { AnswerEvidenceData } from "@/lib/answer-evidence";
import { t } from "@/lib/i18n";

/**
 * 回答の本文の節（#2328）。RAG のチャットの回答と同じく、要約の段落 →「資料により確認できた事項」の箇条書き
 * （各項目の下に「根拠：文書 p.N」）→「資料から確認できなかった事項」の形で出すための、表示だけの組み立て。
 *
 * - 本文の文は書き換えない（AI で直さない）。並べ方と、根拠の ID の印（「【証拠 ID: …】」）を根拠の行にする
 *   ことだけをする。根拠の行は backend の成果物「回答の根拠」の段落ごとの根拠（`passages`。最終の検証が段落を
 *   裏付けた根拠と、段落が書いた ID の印を最終の検証と同じ照合で引き当てた根拠）から決める。
 * - 「資料から確認できなかった事項」は、資料との照合が回答の末尾に足した節（見出しは成果物の `notice.heading`）。
 *   除いた内容の一覧はこの節の 1 か所だけに全件を同じ書式で出し（項目が多ければ共通の「すべて表示」で畳む）、
 *   回答の下の「資料との照合」には件数のバッジだけを出す（#2361）。
 * - 最終の検証が判定を出さなかった回答（時間切れ・失敗。成果物の `binding: "text"`。#2707）は、決定的な照合で
 *   結び付けた根拠を RAG と同じ「参考情報」の節（根拠の原文と「根拠：文書 p.N」）にし、「資料により確認できた
 *   事項」は作らない（照合していない内容を「確認できた」と見せない）。検証が一部の段落だけを確かめた回答
 *   （`partial`。#2700）は、確かめ終わった段落に根拠の行を付け、確かめ終わらなかった段落の根拠を「参考情報」にする。
 * - 段落に根拠を 1 つも結び付けられないときは null（呼び出し側は今までの Markdown の描き方にする）。
 * - 段落の印の記録（`passages`。#2738）のある回答は、根拠を結び付けられなくても節に組み立てる（印は段落ごとに
 *   付けるため）。「資料から確認できなかった事項」の節は作らず、本文の後ろの節（「根拠と矛盾し除いた内容」
 *   「確認が必要な点」）は共有の `AnswerSections` が記録から作る。印の付いた段落は「資料により確認できた事項」に
 *   入れない（確かめていない段落を「確認できた」と見せない）。段落の照合のため、段落・項目は原文（`raw`）を持つ。
 */

export interface StructuredEntry {
  key: string;
  text: string;
  /** 本文の原文（段落の印の照合に使う。#2738）。 */
  raw?: string;
  chunkIds: string[];
  /** 箇条書きの印を付けない補足の行。 */
  muted?: boolean;
  /** Markdown として読まない文（根拠の原文・決まった説明の文）。 */
  plain?: boolean;
  /** 画面の決まった説明の文（回答の言語ではなく画面の言語で描く。#3349）。 */
  uiText?: boolean;
}

export type StructuredBlock =
  | { kind: "paragraph"; key: string; text: string; raw?: string; chunkIds: string[] }
  /** 表・コード・見出しの続きなど、段落にできない Markdown の塊。 */
  | { kind: "markdown"; key: string; text: string; raw?: string; chunkIds: string[] }
  | {
      kind: "section";
      key: string;
      title: string | null;
      ordered: boolean;
      entries: StructuredEntry[];
      /** 項目が多いとき畳む（「資料から確認できなかった事項」の節）。 */
      collapsible?: boolean;
    };

const BULLET = /^\s*(?:[-*+・•]|(\d{1,3})[.)．）])\s+/;
const HEADING = /^\s*(?:#{1,6}\s+(.+?)\s*#*|\*\*([^*\n]{1,60})\*\*\s*[:：]?|【([^】\n]{1,30})】\s*[:：]?)\s*$/;
const MARKDOWN_BLOCK = /^\s*(?:\||```|~~~|>|\s{2,}[-*+]\s)/;

/** 回答の節（`notice.heading`）。資料との照合の成果物の内容から読む。 */
export function noticeHeading(validation: Record<string, unknown> | null): string | null {
  const notice = validation?.notice;
  if (!notice || typeof notice !== "object" || Array.isArray(notice)) return null;
  const heading = (notice as Record<string, unknown>).heading;
  return typeof heading === "string" && heading ? heading : null;
}

function headingText(line: string): string | null {
  const match = HEADING.exec(line);
  if (!match) return null;
  return (match[1] ?? match[2] ?? match[3] ?? "").trim() || null;
}

/** 本文から根拠の ID の印を除いた表示の文（印の前後の空白をつめる）。 */
function displayText(raw: string, markers: readonly string[]): string {
  let value = raw;
  for (const marker of markers) value = value.split(marker).join("");
  return value
    .replace(/[ \t\u3000]+([。、．，,.）)」』])/g, "$1")
    .replace(/[ \t\u3000]{2,}/g, " ")
    .trim();
}

function chunkIdsOf(raw: string, evidence: AnswerEvidenceData): string[] {
  const ids: string[] = [];
  for (const passage of evidence.passages) {
    if (!raw.includes(passage.text)) continue;
    for (const id of passage.chunkIds) if (!ids.includes(id)) ids.push(id);
  }
  for (const marker of evidence.markers) {
    if (!raw.includes(marker.text)) continue;
    for (const id of marker.chunkIds) if (!ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** 空行で区切った本文の塊。 */
function chunks(text: string): string[][] {
  const result: string[][] = [];
  let current: string[] = [];
  for (const line of text.split("\n")) {
    if (line.trim()) {
      current.push(line);
    } else if (current.length) {
      result.push(current);
      current = [];
    }
  }
  if (current.length) result.push(current);
  return result;
}

/**
 * 本文と、本文の末尾に backend が足した節（「資料で確認できず除いた内容」など。見出しは太字の行）を分ける。
 * 見出しのうち本文の中で最も前にあるものから後ろを節にする。
 */
function splitNotice(answer: string, headings: readonly (string | null)[]): { body: string; notice: string[] } {
  let at = -1;
  let length = 0;
  for (const heading of headings) {
    if (!heading) continue;
    const marker = `**${heading}**`;
    const found = answer.indexOf(marker);
    if (found >= 0 && (at < 0 || found < at)) {
      at = found;
      length = marker.length;
    }
  }
  if (at < 0) return { body: answer, notice: [] };
  return {
    body: answer.slice(0, at).trim(),
    notice: answer
      .slice(at + length)
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean),
  };
}

interface Line {
  raw: string;
  text: string;
  chunkIds: string[];
}

/** 回答の本文を節に組み立てる。結び付けられないときは null。 */
export interface AnswerStructureTitles {
  /** 「資料により確認できた事項」。 */
  confirmed: string;
  /** 「資料から確認できなかった事項」。 */
  unconfirmed: string;
  /** 「参考情報」。 */
  reference: string;
  /** 「参考情報」の節の説明（「以下は資料の原文です。本質問の条件への該当は確認できていません。」）。 */
  referenceNote: string;
}

/** 節の見出しと説明の文（チャットと実行の詳細で同じ）。 */
export function answerStructureTitles(): AnswerStructureTitles {
  return {
    confirmed: t("chat.answer.confirmed"),
    unconfirmed: t("chat.answer.unconfirmed"),
    reference: t("chat.answer.reference"),
    referenceNote: t("chat.answer.referenceNote"),
  };
}

/** 段落の印の文言（共有の `AnswerSections`・`AnswerPassageBadges`。チャットと実行の詳細で同じ。#2738）。 */
export function answerPassageLabels(): AnswerPassageLabels {
  return {
    state: { unverified: t("chat.answer.passage.unverified"), warning: t("chat.answer.passage.warning") },
    stateDescription: {
      unverified: t("chat.answer.passage.unverifiedDescription"),
      warning: t("chat.answer.passage.warningDescription"),
    },
    toggleHint: t("chat.answer.passage.toggleHint"),
    removedTitle: t("chat.answer.passage.removedTitle"),
    removedNote: t("chat.answer.passage.removedNote"),
    marksTitle: t("chat.answer.passage.marksTitle"),
    marksNote: t("chat.answer.passage.marksNote"),
    jumpLabel: (quote) => t("chat.answer.passage.jump", { quote }),
    warningsTitle: t("chat.answer.passage.warningsTitle"),
    badge: {
      unverified: (count) => t("chat.answer.passage.badgeUnverified", { count }),
      warning: (count) => t("chat.answer.passage.badgeWarning", { count }),
      removed: (count) => t("chat.answer.passage.badgeRemoved", { count }),
    },
  };
}

/** 根拠を結び付けない回答の根拠（段落の印の記録のある回答を、根拠なしで節に組み立てるとき）。 */
const NO_EVIDENCE: AnswerEvidenceData = {
  entries: [],
  markers: [],
  passages: [],
  binding: "validation",
  references: [],
  citationLines: [],
};

export function structureAnswer(
  answer: string,
  evidence: AnswerEvidenceData | null,
  heading: string | null,
  titles: AnswerStructureTitles,
  passages: AnswerPassageRecords | null = null
): StructuredBlock[] | null {
  if (passages) return markedAnswer(answer, evidence, heading, titles, passages);
  if (!evidence) return null;
  if (evidence.binding === "text") return referenceAnswer(answer, evidence, titles);
  const { body, notice } = splitNotice(answer, [heading]);
  const { blocks, ownHeadings } = bodyBlocks(body, evidence, true);

  const cited = (block: StructuredBlock) =>
    block.kind === "section" ? block.entries.some((entry) => entry.chunkIds.length) : block.chunkIds.length > 0;
  const anyCited = blocks.some(cited);
  // 検証が一部の段落だけを確かめたとき（`partial`）、確かめ終わらなかった段落の根拠は「参考情報」（RAG と同じく
  // 「資料により確認できた事項」の後・「資料から確認できなかった事項」の前）。
  const references = evidence.references.length ? referenceSection(evidence, titles) : null;
  if (!anyCited && !references) return null;

  const shaped = !anyCited || ownHeadings ? blocks : withConfirmedSection(blocks, titles.confirmed);
  if (references) shaped.push(references);
  if (notice.length) {
    shaped.push({
      kind: "section",
      key: "notice",
      title: titles.unconfirmed,
      ordered: false,
      collapsible: true,
      entries: notice.map((item, position) =>
        BULLET.test(item)
          ? { key: `n${position}`, text: item.replace(BULLET, ""), chunkIds: [] }
          : { key: `n${position}`, text: item, chunkIds: [], muted: true }
      ),
    });
  }
  return shaped;
}

/**
 * 段落の印の記録のある回答（#2738）。根拠を結び付けられなくても節に組み立て（印は段落ごとに付ける）、
 * 「資料から確認できなかった事項」の節は作らない（外した段落・印の一覧は共有の `AnswerSections` が記録から作る）。
 * backend が本文の末尾に足した節（過去の見出し・「確認が必要な点」「根拠と矛盾し除いた内容」）は本文から除き、
 * 2 か所に出さない。印の付いた段落・項目は「資料により確認できた事項」に入れない。
 */
function markedAnswer(
  answer: string,
  evidence: AnswerEvidenceData | null,
  heading: string | null,
  titles: AnswerStructureTitles,
  passages: AnswerPassageRecords
): StructuredBlock[] {
  const { body } = splitNotice(answer, [heading, t("chat.answer.passage.marksTitle"), t("chat.answer.passage.removedTitle")]);
  const source = evidence ?? NO_EVIDENCE;
  if (source.binding === "text") {
    const { blocks } = bodyBlocks(withoutCitationLines(body, source.citationLines), source, false);
    if (source.references.length) blocks.push(referenceSection(source, titles));
    return blocks;
  }
  const { blocks, ownHeadings } = bodyBlocks(body, source, true);
  const marked = markedKeys(blocks, passages);
  const cited = (block: StructuredBlock) =>
    block.kind === "section" ? block.entries.some((entry) => entry.chunkIds.length) : block.chunkIds.length > 0;
  const shaped =
    !blocks.some(cited) || ownHeadings ? blocks : withConfirmedSection(blocks, titles.confirmed, marked);
  if (source.references.length) shaped.push(referenceSection(source, titles));
  return shaped;
}

/** 印の当たる段落・項目の key（共有の `AnswerSections` と同じ照合）。 */
function markedKeys(blocks: readonly StructuredBlock[], passages: AnswerPassageRecords): Set<string> {
  const keys: string[] = [];
  const texts: (string | undefined)[] = [];
  for (const block of blocks) {
    if (block.kind === "section") {
      for (const entry of block.entries) {
        if (entry.muted) continue;
        keys.push(entry.key);
        texts.push(entry.raw ?? entry.text);
      }
    } else {
      keys.push(block.key);
      texts.push(block.raw ?? block.text);
    }
  }
  const { groups } = matchAnswerPassageMarks(texts, passages.marks);
  return new Set(keys.filter((_key, index) => groups[index] !== null));
}

/**
 * 最終の検証が判定を出さなかった回答（`binding: "text"`。#2707）。照合で確かめていないので「資料により確認
 * できた事項」は作らず、本文はそのまま（根拠の行を付けない）出し、決定的な照合で結び付けた根拠を RAG と同じ
 * 「参考情報」の節（説明の文 → 根拠の原文「…」と「根拠：文書 p.N」）にする。結び付いた出典の行（平文の
 * 「文書 A.pdf p.3」）は「参考情報」の根拠の行で示すので本文から除き、中身が無くなった見出しも除く。
 * 根拠を 1 つも結び付けられないときは null。
 */
function referenceAnswer(
  answer: string,
  evidence: AnswerEvidenceData,
  titles: AnswerStructureTitles
): StructuredBlock[] | null {
  if (!evidence.references.length) return null;
  const { blocks } = bodyBlocks(withoutCitationLines(answer, evidence.citationLines), evidence, false);
  blocks.push(referenceSection(evidence, titles));
  return blocks;
}

/** 「参考情報」の節（説明の文 → 根拠の原文「…」と「根拠：文書 p.N」）。 */
function referenceSection(evidence: AnswerEvidenceData, titles: AnswerStructureTitles): StructuredBlock {
  return {
    kind: "section",
    key: "reference",
    title: titles.reference,
    ordered: false,
    entries: [
      { key: "reference-note", text: titles.referenceNote, chunkIds: [], muted: true, plain: true, uiText: true },
      ...evidence.references.map((reference, index) => ({
        key: `reference-${index}`,
        text: `「${reference.quote}」`,
        chunkIds: reference.chunkIds,
        plain: true,
      })),
    ],
  };
}

/** 根拠に結び付いた出典の行と、それで中身が無くなった見出しの行を除いた本文。 */
function withoutCitationLines(answer: string, citationLines: readonly string[]): string {
  if (!citationLines.length) return answer;
  const lines = answer.split("\n");
  const removed = lines.map((line) => citationLines.includes(line.trim()));
  const kept = (index: number) => !removed[index] && lines[index].trim() !== "";
  lines.forEach((line, index) => {
    if (removed[index] || headingText(line) === null) return;
    // 見出しの下（空行を除く）の最初の行が除いた出典の行で、残る次の行がその見出しの下に入らない（本文の終わり・
    // 次の見出し・空行の後の箇条書きでない段落）なら、見出しも除く。
    let next = index + 1;
    while (next < lines.length && !lines[next].trim()) next += 1;
    if (next >= lines.length || !removed[next]) return;
    let blank = false;
    while (next < lines.length && !kept(next)) {
      if (!lines[next].trim()) blank = true;
      next += 1;
    }
    if (next >= lines.length || headingText(lines[next]) !== null || (blank && !BULLET.test(lines[next]))) {
      removed[index] = true;
    }
  });
  return lines.filter((_line, index) => !removed[index]).join("\n");
}

/** 本文を段落・節に組み立てる。`linked` なら段落・項目に根拠を結び付ける。 */
function bodyBlocks(
  body: string,
  evidence: AnswerEvidenceData,
  linked: boolean
): { blocks: StructuredBlock[]; ownHeadings: boolean } {
  const markers = evidence.markers.map((item) => item.text);
  const ids = (raw: string) => (linked ? chunkIdsOf(raw, evidence) : []);
  const line = (raw: string): Line => ({
    raw,
    text: displayText(raw.replace(BULLET, ""), markers),
    chunkIds: ids(raw),
  });

  const blocks: StructuredBlock[] = [];
  let ownHeadings = false;
  chunks(body).forEach((lines, index) => {
    const key = `b${index}`;
    if (lines.some((item) => MARKDOWN_BLOCK.test(item))) {
      const raw = lines.join("\n");
      blocks.push({ kind: "markdown", key, text: displayText(raw, markers), raw, chunkIds: ids(raw) });
      return;
    }
    let rest = lines;
    const title = headingText(lines[0]);
    if (title !== null) {
      ownHeadings = true;
      rest = lines.slice(1);
      if (!rest.length) {
        blocks.push({ kind: "section", key, title, ordered: false, entries: [] });
        return;
      }
    }
    if (rest.every((item) => BULLET.test(item))) {
      const parsed = rest.map(line);
      blocks.push({
        kind: "section",
        key,
        title,
        ordered: rest.every((item) => /^\s*\d/.test(item)),
        entries: parsed.map((item, position) => ({
          key: `${key}-${position}`,
          text: item.text,
          raw: item.raw,
          chunkIds: item.chunkIds,
        })),
      });
      return;
    }
    if (title !== null) {
      // 見出しの下の段落は、見出しの節の項目にする（RAG の節と同じ形）。
      const parsed = rest.map(line);
      blocks.push({
        kind: "section",
        key,
        title,
        ordered: false,
        entries: parsed.map((item, position) => ({
          key: `${key}-${position}`,
          text: item.text,
          raw: item.raw,
          chunkIds: item.chunkIds,
        })),
      });
      return;
    }
    const raw = rest.join("\n");
    blocks.push({ kind: "paragraph", key, text: displayText(raw, markers), raw, chunkIds: ids(raw) });
  });
  merge(blocks);
  return { blocks, ownHeadings };
}

/** 空の見出しの節の直後の箇条書きは、その見出しの節にまとめる（見出しと箇条書きの間に空行がある書き方）。 */
function merge(blocks: StructuredBlock[]): void {
  for (let index = blocks.length - 2; index >= 0; index -= 1) {
    const current = blocks[index];
    const next = blocks[index + 1];
    if (current.kind === "section" && current.title && !current.entries.length && next.kind === "section" && !next.title) {
      blocks.splice(index, 2, { ...next, key: current.key, title: current.title });
    }
  }
}

/**
 * 見出しの無い回答: 根拠の付いた最初の塊より前を要約の段落にし、そこから後がすべて根拠の付いた段落・項目なら
 * 「資料により確認できた事項」の箇条書きにまとめる（文の順は変えない）。後ろに根拠の無い段落があれば、見出しは
 * 足さずに段落・箇条書きのまま出す。印の付いた段落・項目（`marked` の key。#2738）も根拠の無い段落と同じに扱う
 * （確かめていない段落を「確認できた」の見出しの下に入れない）。
 */
function withConfirmedSection(
  blocks: StructuredBlock[],
  title: string,
  marked: ReadonlySet<string> = new Set()
): StructuredBlock[] {
  const first = blocks.findIndex((block) =>
    block.kind === "section" ? block.entries.some((entry) => entry.chunkIds.length) : block.chunkIds.length > 0
  );
  const lead = blocks.slice(0, first);
  const rest = blocks.slice(first);
  const entries: StructuredEntry[] = [];
  for (const block of rest) {
    if (block.kind === "markdown") return blocks;
    if (block.kind === "paragraph") {
      if (!block.chunkIds.length || marked.has(block.key)) return blocks;
      entries.push({ key: block.key, text: block.text, raw: block.raw, chunkIds: block.chunkIds });
    } else {
      if (block.entries.some((entry) => !entry.chunkIds.length || marked.has(entry.key))) return blocks;
      entries.push(...block.entries);
    }
  }
  // 要約が無い（最初の塊から根拠が付く）ときは、最初の段落を要約として残す。
  if (!lead.length && rest[0]?.kind === "paragraph" && entries.length > 1) {
    const [summary, ...others] = entries;
    return [
      { kind: "paragraph", key: summary.key, text: summary.text, raw: summary.raw, chunkIds: summary.chunkIds },
      { kind: "section", key: "confirmed", title, ordered: false, entries: others },
    ];
  }
  return [...lead, { kind: "section", key: "confirmed", title, ordered: false, entries }];
}
