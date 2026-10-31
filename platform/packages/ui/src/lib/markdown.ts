/**
 * 本文の Markdown の解析（#1808）。LLM の回答・スキルの指示などの Markdown を、`MarkdownText` が React の要素で
 * 描くためのブロックと行の中の要素に分ける。製品に依存しない。
 *
 * 扱うのは、見出し（`#`〜`######`）・段落・記号 / 番号付きの箇条書き（字下げで入れ子）・コードの囲み（``` / ~~~）・
 * 引用（`>`）・区切り線と、行の中の `code`・**強調**・*斜体*・[リンク](https://…)・`<https://…>`・裸の URL。
 * 生の HTML は解釈せず文字のまま残す（描画は React の要素だけで行い、innerHTML を使わない）。
 * 表は扱わない（段落の文字のまま）。表は呼び出し側が `splitMarkdownTables` で先に取り出し、共通の結果の表
 * （`ResultTable`）で出す。
 */

export type MarkdownBlock =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; lines: string[] }
  | { kind: "list"; ordered: boolean; start: number; items: MarkdownBlock[][] }
  | { kind: "code"; language: string; text: string }
  | { kind: "quote"; blocks: MarkdownBlock[] }
  | { kind: "rule" };

export type MarkdownInline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "strong"; children: MarkdownInline[] }
  | { kind: "em"; children: MarkdownInline[] }
  | { kind: "link"; href: string; children: MarkdownInline[] };

const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
const FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*([^`\s]*)[^`]*$/;
const LIST_ITEM = /^( {0,3})([-*+]|\d{1,9}[.)])( {1,4}|\t)(.*)$/;
// 中身の無い項目（`-` だけの行）も項目にする。
const EMPTY_LIST_ITEM = /^( {0,3})([-*+]|\d{1,9}[.)])[ \t]*$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const QUOTE = /^ {0,3}>[ \t]?(.*)$/;

function isBlank(line: string): boolean {
  return line.trim() === "";
}

function indentOf(line: string): number {
  return line.length - line.replace(/^ +/, "").length;
}

function listItem(line: string): { indent: string; marker: string; gap: string; text: string } | null {
  const match = LIST_ITEM.exec(line);
  if (match) return { indent: match[1] ?? "", marker: match[2] ?? "", gap: match[3] ?? "", text: match[4] ?? "" };
  const empty = EMPTY_LIST_ITEM.exec(line);
  if (empty) return { indent: empty[1] ?? "", marker: empty[2] ?? "", gap: " ", text: "" };
  return null;
}

function startsBlock(line: string): boolean {
  return (
    HEADING.test(line) || FENCE.test(line) || RULE.test(line) || QUOTE.test(line) || listItem(line) !== null
  );
}

function isOrderedMarker(marker: string): boolean {
  return /^\d/.test(marker);
}

/** Markdown の本文をブロックの並びにする。 */
export function parseMarkdownBlocks(source: string): MarkdownBlock[] {
  return parseLines(source.replace(/\r\n?/g, "\n").split("\n"));
}

function parseLines(lines: string[]): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index] ?? "";
    if (isBlank(line)) {
      index += 1;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1] ?? "```";
      const indent = indentOf(line);
      const body: string[] = [];
      index += 1;
      while (index < lines.length) {
        const current = lines[index] ?? "";
        // 閉じの囲みは、開きと同じ記号で同じ数以上（後ろに文字を持たない）。
        const close = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(current);
        if (close && close[1]?.[0] === marker[0] && (close[1]?.length ?? 0) >= marker.length) break;
        body.push(current.slice(Math.min(indent, indentOf(current))));
        index += 1;
      }
      index += 1; // 閉じの囲みを読み飛ばす（無ければ末尾まで）。
      blocks.push({ kind: "code", language: fence[2] ?? "", text: body.join("\n") });
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ kind: "heading", level: heading[1]?.length ?? 1, text: heading[2] ?? "" });
      index += 1;
      continue;
    }
    if (RULE.test(line)) {
      blocks.push({ kind: "rule" });
      index += 1;
      continue;
    }
    if (QUOTE.test(line)) {
      const body: string[] = [];
      while (index < lines.length) {
        const current = lines[index] ?? "";
        const quote = QUOTE.exec(current);
        if (quote) {
          body.push(quote[1] ?? "");
        } else if (!isBlank(current) && !startsBlock(current) && !isBlank(body[body.length - 1] ?? "")) {
          // `>` の無い続きの行（段落の続き）は引用に含める。
          body.push(current.trim());
        } else {
          break;
        }
        index += 1;
      }
      blocks.push({ kind: "quote", blocks: parseLines(body) });
      continue;
    }
    if (listItem(line)) {
      index = parseList(lines, index, blocks);
      continue;
    }
    const paragraph: string[] = [];
    while (index < lines.length) {
      const current = lines[index] ?? "";
      if (isBlank(current) || (paragraph.length > 0 && startsBlock(current))) break;
      paragraph.push(current.trim());
      index += 1;
    }
    blocks.push({ kind: "paragraph", lines: paragraph });
  }
  return blocks;
}

/** `start` の行から 1 つの箇条書きを読み、次に読む行の位置を返す。 */
function parseList(lines: string[], start: number, blocks: MarkdownBlock[]): number {
  const first = listItem(lines[start] ?? "");
  const ordered = isOrderedMarker(first?.marker ?? "");
  const items: MarkdownBlock[][] = [];
  let index = start;
  while (index < lines.length) {
    const item = listItem(lines[index] ?? "");
    if (!item || isOrderedMarker(item.marker) !== ordered) break;
    // 項目の中身の字下げ（記号の後の空白まで）。これ以上字下げした行は、この項目の続き・入れ子。
    const contentIndent = item.indent.length + item.marker.length + (item.gap === "\t" ? 1 : item.gap.length);
    const body = [item.text];
    index += 1;
    while (index < lines.length) {
      const current = lines[index] ?? "";
      if (isBlank(current)) {
        let next = index + 1;
        while (next < lines.length && isBlank(lines[next] ?? "")) next += 1;
        if (next < lines.length && indentOf(lines[next] ?? "") >= contentIndent) {
          body.push("");
          index += 1;
          continue;
        }
        break;
      }
      if (indentOf(current) >= contentIndent) {
        body.push(current.slice(contentIndent));
        index += 1;
        continue;
      }
      // 字下げが足りない入れ子（LLM は 2 文字の字下げで入れ子を書くことが多い）も、この項目の入れ子にする。
      const nested = listItem(current);
      if (nested && nested.indent.length > item.indent.length) {
        body.push(current.slice(Math.min(contentIndent, indentOf(current))));
        index += 1;
        continue;
      }
      // 字下げの無い続きの行（段落の続き）は、この項目の文に足す。
      if (!startsBlock(current) && !isBlank(body[body.length - 1] ?? "")) {
        body.push(current.trim());
        index += 1;
        continue;
      }
      break;
    }
    items.push(parseLines(body));
    // 項目の間の空行は、次が同じ種類の項目なら読み飛ばす。
    let next = index;
    while (next < lines.length && isBlank(lines[next] ?? "")) next += 1;
    const following = listItem(lines[next] ?? "");
    if (next !== index && following && isOrderedMarker(following.marker) === ordered) index = next;
  }
  const startNumber = ordered ? Number.parseInt(first?.marker ?? "1", 10) : 1;
  blocks.push({ kind: "list", ordered, start: Number.isFinite(startNumber) ? startNumber : 1, items });
  return index;
}

/** リンクにしてよい URL（`http(s)` と `mailto`）。ほかの scheme（`javascript:` など）・相対の URL は null。 */
export function safeMarkdownHref(url: string): string | null {
  const trimmed = url.trim();
  return /^(?:https?:\/\/[^\s]+|mailto:[^\s]+)$/i.test(trimmed) ? trimmed : null;
}

const INLINE = new RegExp(
  [
    // 1: 記号の取り消し（`\*` は文字の *）。
    String.raw`\\([\\` + "`" + String.raw`*_{}\[\]()#+\-.!|>~<])`,
    // 2, 3: `code`（同じ数の ` で閉じる）。
    "(`+)([\\s\\S]*?[^`])\\2(?!`)",
    // 4, 5: [文字](URL "題名")。URL の中の 1 段の括弧を許す。
    String.raw`\[([^\]\n]+)\]\(\s*<?([^\s()<>]+(?:\([^\s()]*\)[^\s()<>]*)*)>?(?:\s+"[^"\n]*")?\s*\)`,
    // 6: <https://…>
    String.raw`<((?:https?:\/\/|mailto:)[^\s<>]+)>`,
    // 7: **強調**
    String.raw`\*\*(?=\S)([\s\S]*?\S)\*\*(?!\*)`,
    // 8: *斜体*（`2 * 3 * 4` のように空白で囲んだ * は斜体にしない）。
    String.raw`\*(?=[^\s*])([^*\n]*?[^\s*\\])\*(?!\*)`,
    // 9: 裸の URL。日本語の句読点・括弧の手前で切る。
    String.raw`(https?:\/\/[^\s<>"'` + "`" + String.raw`、。，．「」『』（）【】]+)`,
  ].join("|"),
  "g",
);

/** 裸の URL の末尾の句読点・閉じ括弧は URL に含めない（URL の中の括弧の対は残す）。 */
function trimBareUrl(url: string): string {
  let end = url.length;
  while (end > 0) {
    const char = url[end - 1] ?? "";
    if (/[.,;:!?'"]/.test(char)) {
      end -= 1;
      continue;
    }
    if (char === ")") {
      const body = url.slice(0, end);
      const opens = (body.match(/\(/g) ?? []).length;
      const closes = (body.match(/\)/g) ?? []).length;
      if (closes > opens) {
        end -= 1;
        continue;
      }
    }
    break;
  }
  return url.slice(0, end);
}

/** 行の中の要素に分ける。リンクにできない URL・閉じていない記号は文字のまま。 */
export function parseMarkdownInline(text: string): MarkdownInline[] {
  const parts: MarkdownInline[] = [];
  const pushText = (value: string) => {
    if (!value) return;
    const last = parts[parts.length - 1];
    if (last?.kind === "text") last.text += value;
    else parts.push({ kind: "text", text: value });
  };
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    const at = match.index ?? 0;
    pushText(text.slice(last, at));
    let consumed = match[0].length;
    if (match[1] !== undefined) {
      pushText(match[1]);
    } else if (match[2] !== undefined) {
      // 前後の 1 つの空白は区切りなので外す（`` ` `x` ` `` → `` `x` ``）。
      parts.push({ kind: "code", text: (match[3] ?? "").replace(/^ (.*) $/, "$1") });
    } else if (match[4] !== undefined) {
      const href = safeMarkdownHref(match[5] ?? "");
      if (href) parts.push({ kind: "link", href, children: parseMarkdownInline(match[4]) });
      else pushText(match[0]);
    } else if (match[6] !== undefined) {
      const href = safeMarkdownHref(match[6]);
      if (href) parts.push({ kind: "link", href, children: [{ kind: "text", text: href }] });
      else pushText(match[0]);
    } else if (match[7] !== undefined) {
      parts.push({ kind: "strong", children: parseMarkdownInline(match[7]) });
    } else if (match[8] !== undefined) {
      parts.push({ kind: "em", children: parseMarkdownInline(match[8]) });
    } else if (match[9] !== undefined) {
      const url = trimBareUrl(match[9]);
      consumed = url.length;
      const href = safeMarkdownHref(url);
      if (href) parts.push({ kind: "link", href, children: [{ kind: "text", text: url }] });
      else pushText(url);
    }
    // 裸の URL から外した末尾（句読点・閉じ括弧）は、次の文字の部分として出す。
    last = at + consumed;
  }
  pushText(text.slice(last));
  return parts;
}
