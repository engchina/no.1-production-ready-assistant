import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

// RAG の全画面は画面幅いっぱい（共有 PageHeader / PageBody の `wide`）で統一する（#107）。
// PageHeader と PageBody の wide がずれると、1920px 以上でタイトルと本文の左端がずれる。
// 読み込み中・エラー表示・戻るリンクの補助 PageBody も含め、例外は設けない。
const SRC_DIR = join(__dirname, "..");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return entry.name.endsWith(".tsx") ? [relative(SRC_DIR, path)] : [];
  });
}

/**
 * `<PageHeader …>` / `<PageBody …>` の開始タグを切り出す。
 * props の `{…}`（`actions={<>…</>}` や `() =>` を含む）の中の `>` で途切れないよう、波括弧の深さを数える。
 * 検査用に、各 props の値（`{…}` と文字列）は取り除いて属性名だけを残す。
 */
function layoutTags(source: string): string[] {
  const tags: string[] = [];
  for (const match of source.matchAll(/<(PageHeader|PageBody)\b/gu)) {
    let depth = 0;
    let quote: string | null = null;
    let attrs = "";
    for (let i = match.index + match[0].length; i < source.length; i += 1) {
      const ch = source[i];
      if (depth === 0 && quote) {
        if (ch === quote) quote = null;
        continue;
      }
      if (depth === 0 && (ch === '"' || ch === "'")) {
        quote = ch;
        continue;
      }
      if (ch === "{") depth += 1;
      else if (ch === "}") depth -= 1;
      else if (depth === 0 && ch === ">") break;
      else if (depth === 0) attrs += ch;
    }
    tags.push(`<${match[1]}${attrs.replace(/\s+/gu, " ").replace(/\s*\/$/u, "")}>`);
  }
  return tags;
}

const hasWide = (tag: string) => /\swide(?=[\s>])/u.test(tag);

describe("全画面の wide（PageHeader / PageBody）", () => {
  const tags = sourceFiles(SRC_DIR).flatMap((file) =>
    layoutTags(readFileSync(join(SRC_DIR, file), "utf8")).map((tag) => ({ file, tag }))
  );

  it("検査対象の PageHeader / PageBody を src から見つけられる", () => {
    expect(tags.filter(({ tag }) => tag.startsWith("<PageHeader")).length).toBeGreaterThan(20);
    expect(tags.filter(({ tag }) => tag.startsWith("<PageBody")).length).toBeGreaterThan(40);
  });

  it("src のすべての PageHeader / PageBody が wide を渡す（付け忘れると 1440px に戻り左端がずれる）", () => {
    const missing = tags.filter(({ tag }) => !hasWide(tag)).map(({ file, tag }) => `${file}: ${tag}`);
    expect(missing).toEqual([]);
  });

  it("wide を false や式で切り替えない", () => {
    const conditional = tags.filter(({ tag }) => /\swide=/u.test(tag)).map(({ file, tag }) => `${file}: ${tag}`);
    expect(conditional).toEqual([]);
  });

  it("開始タグの切り出しは複数行の props・入れ子の JSX を読み飛ばし、wide の付け忘れを検出する", () => {
    const sample = `
      <PageHeader
        title={t("wide")}
        actions={
          <>
            {busy ? <span>{t("a")}</span> : null}
            <Button onClick={() => go()}>x</Button>
          </>
        }
      />
      <PageBody wide className="py-4 wide-x">
      <PageBody className="wide">
    `;
    expect(layoutTags(sample)).toEqual(["<PageHeader title= actions=>", '<PageBody wide className=>', "<PageBody className=>"]);
    expect(layoutTags(sample).map(hasWide)).toEqual([false, true, false]);
  });
});
