// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { MarkdownText } from "../src/components/ui/markdown-text";
import { parseMarkdownBlocks, parseMarkdownInline, safeMarkdownHref } from "../src/lib/markdown";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("parseMarkdownBlocks", () => {
  it("見出し・段落・箇条書き・番号付き・コード・引用・区切り線に分ける", () => {
    const blocks = parseMarkdownBlocks(
      [
        "### 主な構成要素",
        "本文の 1 行目",
        "本文の 2 行目",
        "",
        "- 項目 A",
        "  - 入れ子",
        "- 項目 B",
        "",
        "3. 三つ目",
        "4. 四つ目",
        "",
        "```python",
        "def run():",
        "    return 1",
        "```",
        "",
        "> 引用の文",
        "",
        "---",
      ].join("\n"),
    );
    expect(blocks.map((block) => block.kind)).toEqual([
      "heading",
      "paragraph",
      "list",
      "list",
      "code",
      "quote",
      "rule",
    ]);
    expect(blocks[0]).toEqual({ kind: "heading", level: 3, text: "主な構成要素" });
    expect(blocks[1]).toEqual({ kind: "paragraph", lines: ["本文の 1 行目", "本文の 2 行目"] });
    const list = blocks[2];
    expect(list?.kind === "list" && list.items.length).toBe(2);
    expect(list?.kind === "list" && list.items[0]?.map((child) => child.kind)).toEqual(["paragraph", "list"]);
    expect(blocks[3]).toMatchObject({ kind: "list", ordered: true, start: 3 });
    expect(blocks[4]).toEqual({ kind: "code", language: "python", text: "def run():\n    return 1" });
  });

  it("コードの囲みの中の記号は解釈しない", () => {
    const blocks = parseMarkdownBlocks("```\n# 見出しではない\n- 箇条書きではない\n```");
    expect(blocks).toEqual([{ kind: "code", language: "", text: "# 見出しではない\n- 箇条書きではない" }]);
  });
});

describe("parseMarkdownInline", () => {
  it("強調・斜体・コード・リンクを分ける", () => {
    expect(parseMarkdownInline("**太字** と *斜体* と `code` と [資料](https://example.com/a)")).toEqual([
      { kind: "strong", children: [{ kind: "text", text: "太字" }] },
      { kind: "text", text: " と " },
      { kind: "em", children: [{ kind: "text", text: "斜体" }] },
      { kind: "text", text: " と " },
      { kind: "code", text: "code" },
      { kind: "text", text: " と " },
      { kind: "link", href: "https://example.com/a", children: [{ kind: "text", text: "資料" }] },
    ]);
  });

  it("空白で囲んだ * と識別子の _ は文字のまま", () => {
    expect(parseMarkdownInline("2 * 3 * 4 と run_fad4758_x")).toEqual([
      { kind: "text", text: "2 * 3 * 4 と run_fad4758_x" },
    ]);
  });

  it("裸の URL は末尾の句読点を含めずにリンクにする", () => {
    expect(parseMarkdownInline("詳細は https://example.com/docs. を参照")).toEqual([
      { kind: "text", text: "詳細は " },
      { kind: "link", href: "https://example.com/docs", children: [{ kind: "text", text: "https://example.com/docs" }] },
      { kind: "text", text: ". を参照" },
    ]);
  });

  it("javascript: などのリンクは文字のまま", () => {
    expect(parseMarkdownInline("[押す](javascript:alert(1))")).toEqual([
      { kind: "text", text: "[押す](javascript:alert(1))" },
    ]);
    expect(safeMarkdownHref("javascript:alert(1)")).toBeNull();
    expect(safeMarkdownHref("/runs?id=1")).toBeNull();
    expect(safeMarkdownHref("mailto:a@example.com")).toBe("mailto:a@example.com");
  });

  it("\\* は文字の * にする", () => {
    expect(parseMarkdownInline("\\*強調ではない\\*")).toEqual([{ kind: "text", text: "*強調ではない*" }]);
  });
});

describe("MarkdownText", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  function render(text: string, headingLevel?: 2 | 3 | 4) {
    act(() => root.render(<MarkdownText text={text} headingLevel={headingLevel} testId="md" />));
    return container.querySelector<HTMLElement>("[data-testid='md']");
  }

  it("回答の見出し・太字・箇条書きを記号のまま出さずに要素で描く", () => {
    const element = render(
      "**関数シグネチャを解析して呼び出す**\n\n### 主な構成要素\n- `parse` を呼ぶ\n- *任意* の手順\n\n#### 詳細\n1. 一つ目\n2. 二つ目",
    );
    expect(element?.querySelector("strong")?.textContent).toBe("関数シグネチャを解析して呼び出す");
    // 最上位の見出し（###）を h3 にし、その下（####）を h4 にする。
    expect(element?.querySelector("h3")?.textContent).toBe("主な構成要素");
    expect(element?.querySelector("h4")?.textContent).toBe("詳細");
    expect(element?.querySelectorAll("ul > li")).toHaveLength(2);
    expect(element?.querySelector("ul code")?.textContent).toBe("parse");
    expect(element?.querySelector("em")?.textContent).toBe("任意");
    expect(element?.querySelectorAll("ol > li")).toHaveLength(2);
    expect(element?.textContent).not.toMatch(/\*\*|###|`/);
  });

  it("headingLevel で最上位の見出しの段を変える", () => {
    const element = render("# 大見出し\n## 中見出し", 2);
    expect(element?.querySelector("h2")?.textContent).toBe("大見出し");
    expect(element?.querySelector("h3")?.textContent).toBe("中見出し");
  });

  it("生の HTML は要素にせず文字のまま出す", () => {
    const element = render('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n**<b>太字</b>**');
    expect(element?.querySelector("script")).toBeNull();
    expect(element?.querySelector("img")).toBeNull();
    expect(element?.querySelector("b")).toBeNull();
    expect(element?.textContent).toContain("<script>alert(1)</script>");
    expect(element?.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(element?.querySelector("strong")?.textContent).toBe("<b>太字</b>");
  });

  it("外部のリンクは新しいタブで開き、noopener noreferrer を付ける", () => {
    const element = render("[資料](https://example.com/a) と [危険](javascript:alert(1))");
    const links = element?.querySelectorAll("a") ?? [];
    expect(links).toHaveLength(1);
    expect(links[0]?.getAttribute("href")).toBe("https://example.com/a");
    expect(links[0]?.getAttribute("target")).toBe("_blank");
    expect(links[0]?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(links[0]?.querySelector(".sr-only")?.textContent).toBe("（新しいタブで開きます）");
    expect(element?.textContent).toContain("[危険](javascript:alert(1))");
  });

  it("コードブロックは枠の中で横にスクロールし、キーボードでも動かせる", () => {
    const element = render("```sql\nSELECT * FROM very_long_table_name;\n```");
    const pre = element?.querySelector("pre");
    expect(pre?.className).toContain("overflow-x-auto");
    expect(pre?.getAttribute("tabindex")).toBe("0");
    expect(pre?.dataset.language).toBe("sql");
    expect(pre?.textContent).toBe("SELECT * FROM very_long_table_name;");
  });

  it("段落の中の改行は改行のまま、空の本文は何も出さない", () => {
    const element = render("1 行目\n2 行目");
    expect(element?.querySelectorAll("p br")).toHaveLength(1);
    act(() => root.render(<MarkdownText text={"  \n"} testId="empty" />));
    expect(container.querySelector("[data-testid='empty']")).toBeNull();
  });
});
