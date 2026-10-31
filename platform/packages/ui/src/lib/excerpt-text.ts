/**
 * 根拠の抜粋・chunk の本文に入る HTML（文書の表を解析した `<table>`・`<br>` など）を、読める形に解析する（#2387）。
 *
 * HTML の無害化ではない: 結果は React の text node にだけ入れ、HTML として解釈しない（`innerHTML` を使わない）。
 * タグを除いてから実体参照を 1 回だけ文字に戻すので、`&lt;script&gt;` はセルの文字「<script>」として出るだけで実行されない。
 * 壊れた HTML（閉じていない・途中で切れた・`<table>` の無い `<tr>`）も例外を出さず、読める範囲で返す。
 */

/** 表のセル。`header` は `<th>` か `<thead>` の中のセル。 */
export interface ExcerptTableCell {
  text: string;
  header: boolean;
}

export type ExcerptBlock =
  | { kind: "text"; text: string }
  | { kind: "table"; caption: string; rows: ExcerptTableCell[][] };

const ENTITY: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
};

/** 実体参照を文字に戻す（名前付きは主なものだけ。知らない名前は文字のまま残す）。 */
function decodeEntities(text: string): string {
  return text.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi, (match, body: string) => {
    if (body[0] === "#") {
      const code = body[1] === "x" || body[1] === "X" ? Number.parseInt(body.slice(2), 16) : Number.parseInt(body.slice(1), 10);
      // 制御文字・範囲外は戻さない（文字のまま）。
      if (!Number.isFinite(code) || code < 0x20 || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return match;
      return String.fromCodePoint(code);
    }
    return ENTITY[body.toLowerCase()] ?? match;
  });
}

/** 中身ごと捨てる要素（読める本文を持たない）。 */
const DROP_CONTENT = new Set(["script", "style", "template", "noscript", "iframe", "object", "svg", "math", "head", "title"]);

/** 改行に置き換える要素（開き・閉じの両方）。 */
const LINE_BREAK = new Set([
  "br",
  "p",
  "div",
  "li",
  "ul",
  "ol",
  "dl",
  "dt",
  "dd",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "hr",
  "pre",
  "blockquote",
  "section",
  "article",
  "header",
  "footer",
  "figure",
  "figcaption",
]);

/**
 * 除く既知の HTML の要素（中身の文字は残す）。ここに無いタグの形の文字（`<値>` など）は本文の文字として残す。
 */
const KNOWN_INLINE = new Set([
  "a",
  "abbr",
  "b",
  "big",
  "body",
  "button",
  "center",
  "cite",
  "code",
  "col",
  "colgroup",
  "del",
  "em",
  "embed",
  "font",
  "form",
  "html",
  "i",
  "img",
  "input",
  "ins",
  "kbd",
  "label",
  "link",
  "mark",
  "meta",
  "s",
  "small",
  "source",
  "span",
  "strike",
  "strong",
  "sub",
  "sup",
  "u",
  "video",
  "audio",
  "wbr",
]);

const TABLE_PARTS = new Set(["table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"]);

/** タグの形（`<tag …>` / `</tag>` / `<tag/>`）。属性の中の `>` は考えない（抜粋の HTML は parser の出力で単純）。 */
const TAG = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)\b([^<>]*)>/g;
/** 末尾で途中から切れたタグ（`<td cla`）。 */
const CUT_TAG_AT_END = /<\/?[a-zA-Z][a-zA-Z0-9]*\b[^<>]*$/;

function colspanOf(attrs: string): number {
  const match = /colspan\s*=\s*["']?(\d+)/i.exec(attrs);
  const value = match ? Number.parseInt(match[1], 10) : 1;
  return Number.isFinite(value) && value > 1 ? Math.min(value, 50) : 1;
}

/** 文字の前後の空白を整える（行ごとに右の空白を除き、3 行以上の空行をつめる）。 */
function tidyText(text: string): string {
  return text
    .split("\n")
    .map((line) => line.replace(/[ \t]+$/g, ""))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/^\n+|\n+$/g, "");
}

function tidyCell(text: string): string {
  return text
    .split("\n")
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .join("\n");
}

interface TableState {
  caption: string;
  inCaption: boolean;
  rows: ExcerptTableCell[][];
  row: ExcerptTableCell[] | null;
  cell: { text: string; header: boolean; span: number } | null;
  inHead: boolean;
  /** 入れ子の表の深さ（1 = この表の直下）。 */
  depth: number;
}

function newTable(): TableState {
  return { caption: "", inCaption: false, rows: [], row: null, cell: null, inHead: false, depth: 1 };
}

function closeCell(table: TableState) {
  if (!table.cell) return;
  if (!table.row) table.row = [];
  const text = decodeEntities(tidyCell(table.cell.text));
  table.row.push({ text, header: table.cell.header });
  for (let index = 1; index < table.cell.span; index += 1) table.row.push({ text: "", header: table.cell.header });
  table.cell = null;
}

function closeRow(table: TableState) {
  closeCell(table);
  if (table.row && table.row.some((cell) => cell.text)) table.rows.push(table.row);
  table.row = null;
}

function finishTable(table: TableState): ExcerptBlock | null {
  closeRow(table);
  const width = Math.max(0, ...table.rows.map((row) => row.length));
  if (width === 0) {
    const caption = decodeEntities(tidyCell(table.caption));
    return caption ? { kind: "text", text: caption } : null;
  }
  const rows = table.rows.map((row) =>
    row.length < width ? [...row, ...Array.from({ length: width - row.length }, () => ({ text: "", header: false }))] : row
  );
  return { kind: "table", caption: decodeEntities(tidyCell(table.caption)), rows };
}

/** 文字に HTML の表・タグが入っているか（入っていなければ解析しない）。 */
function looksLikeHtml(text: string): boolean {
  return /<\/?[a-zA-Z][a-zA-Z0-9]*\b[^<>]*>|&(#x?[0-9a-f]+|[a-z]+);/i.test(text);
}

/**
 * 抜粋を、本文（text）と表（table）の並びにする。HTML を含まない文字は 1 つの text のまま返す。
 */
export function parseExcerpt(raw: string): ExcerptBlock[] {
  const source = raw ?? "";
  if (!source.trim()) return [];
  if (!looksLikeHtml(source)) return [{ kind: "text", text: source }];
  const input = source.replace(CUT_TAG_AT_END, "");
  const blocks: ExcerptBlock[] = [];
  let text = "";
  // クロージャの中で書き換えるので、宣言の null に型を絞らせない。
  let table = null as TableState | null;
  let dropUntil: string | null = null;

  const pushText = () => {
    const value = tidyText(decodeEntities(text));
    if (value) blocks.push({ kind: "text", text: value });
    text = "";
  };
  const appendChars = (chars: string) => {
    if (!chars) return;
    if (!table) {
      text += chars;
    } else if (table.cell) {
      table.cell.text += chars;
    } else if (table.inCaption) {
      table.caption += chars;
    }
    // 表の中でセルの外の文字（タグの間の空白など）は捨てる。
  };
  const endTable = () => {
    if (!table) return;
    const block = finishTable(table);
    if (block) blocks.push(block);
    table = null;
  };

  let last = 0;
  TAG.lastIndex = 0;
  for (let match = TAG.exec(input); match; match = TAG.exec(input)) {
    const chars = input.slice(last, match.index);
    last = TAG.lastIndex;
    const closing = match[1] === "/";
    const name = match[2].toLowerCase();
    const attrs = match[3] ?? "";
    if (dropUntil) {
      if (closing && name === dropUntil) dropUntil = null;
      continue;
    }
    appendChars(chars);
    if (DROP_CONTENT.has(name)) {
      if (!closing && !attrs.trim().endsWith("/")) dropUntil = name;
      continue;
    }
    if (TABLE_PARTS.has(name)) {
      if (!table) {
        if (closing) continue; // 対応する開きの無い閉じタグ（途中から始まる抜粋）は捨てる。
        // `<table>` の無い `<tr>` / `<td>` も表の始まりとして扱う（途中から始まる抜粋）。
        pushText();
        table = newTable();
        if (name === "table") continue;
      }
      const current: TableState = table;
      if (current.depth > 1) {
        // 入れ子の表は、外のセルの中の文字にする（行は改行、セルは「 / 」）。
        const cell = current.cell;
        if (name === "table") current.depth += closing ? -1 : 1;
        if (!cell || !closing) continue;
        if (name === "td" || name === "th") cell.text += " / ";
        else if (name === "tr" || name === "table") cell.text = `${cell.text.replace(/ \/ $/, "")}\n`;
        continue;
      }
      if (name === "table") {
        if (closing) endTable();
        else if (current.cell) {
          current.depth += 1;
          current.cell.text += "\n";
        } else {
          // セルの外で始まる 2 つ目の表（閉じ忘れ）。前の表を閉じて新しい表にする。
          endTable();
          table = newTable();
        }
        continue;
      }
      if (name === "caption") {
        current.inCaption = !closing;
        continue;
      }
      if (name === "thead") {
        current.inHead = !closing;
        continue;
      }
      if (name === "tbody" || name === "tfoot") {
        if (!closing) current.inHead = false;
        continue;
      }
      if (name === "tr") {
        closeRow(current);
        if (!closing) current.row = [];
        continue;
      }
      // td / th
      closeCell(current);
      if (!closing) current.cell = { text: "", header: name === "th" || current.inHead, span: colspanOf(attrs) };
      continue;
    }
    if (LINE_BREAK.has(name)) {
      appendChars("\n");
      continue;
    }
    if (KNOWN_INLINE.has(name)) continue;
    // 知らないタグの形の文字は本文の文字として残す。
    appendChars(match[0]);
  }
  if (!dropUntil) appendChars(input.slice(last));
  endTable();
  pushText();
  return blocks;
}

/** 表を「セル | セル」の行のテキストにする（空のセルだけの行は除く）。 */
function tableToText(block: Extract<ExcerptBlock, { kind: "table" }>): string {
  const lines = block.rows
    .map((row) => row.map((cell) => cell.text.replace(/\s*\n\s*/g, " ")).join(" | "))
    .filter((line) => line.replace(/[|\s]/g, ""));
  return [block.caption, ...lines].filter(Boolean).join("\n");
}

/**
 * 抜粋を読める文字にする（表は行ごとに「セル | セル」、`<br>` などは改行、タグは除く）。
 * 3 行に畳む表示・文字数で切る要約・コピーに使う。
 */
export function excerptPlainText(raw: string): string {
  return parseExcerpt(raw)
    .map((block) => (block.kind === "text" ? block.text : tableToText(block)))
    .join("\n");
}

/** 抜粋に表が入っているか。 */
export function excerptHasTable(raw: string): boolean {
  return parseExcerpt(raw).some((block) => block.kind === "table");
}
