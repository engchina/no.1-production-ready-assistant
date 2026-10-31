// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { EvidenceCard, ExcerptContent, excerptHasTable, excerptPlainText, parseExcerpt, type EvidenceListItem } from "../src";

// #2387: 根拠の抜粋に入る HTML（文書の表・<br>）を、HTML として注入せずに読める形にする。内容はすべて合成。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TABLE =
  "<table><thead><tr><th>項目</th><th>値</th></tr></thead>" +
  "<tbody><tr><td>受付&amp;番号</td><td>A<br>B</td></tr><tr><td></td><td></td></tr><tr><td>期限</td><td>30 日</td></tr></tbody></table>";

describe("parseExcerpt / excerptPlainText", () => {
  it("表を行とセルにし、空の行を除き、実体参照を文字に戻す", () => {
    expect(parseExcerpt(TABLE)).toEqual([
      {
        kind: "table",
        caption: "",
        rows: [
          [
            { text: "項目", header: true },
            { text: "値", header: true },
          ],
          [
            { text: "受付&番号", header: false },
            { text: "A\nB", header: false },
          ],
          [
            { text: "期限", header: false },
            { text: "30 日", header: false },
          ],
        ],
      },
    ]);
    expect(excerptPlainText(TABLE)).toBe("項目 | 値\n受付&番号 | A B\n期限 | 30 日");
    expect(excerptHasTable(TABLE)).toBe(true);
  });

  it("表の前後の本文を別の block にし、<br> と段落は改行にする", () => {
    const text = "見出し A<br>本文 1\n<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table><p>後の本文</p>";
    expect(parseExcerpt(text).map((block) => block.kind)).toEqual(["text", "table", "text"]);
    expect(excerptPlainText(text)).toBe("見出し A\n本文 1\na | b\nc | d\n後の本文");
  });

  it("HTML を含まない本文はそのまま返す（< や > の文字も残す）", () => {
    expect(excerptPlainText("一行目\n\n  二行目 a < b > c")).toBe("一行目\n\n  二行目 a < b > c");
    expect(excerptHasTable("本文")).toBe(false);
    expect(parseExcerpt("")).toEqual([]);
  });

  it("既知のタグは除き、知らないタグの形の文字は残す", () => {
    expect(excerptPlainText("本文 <b>強調</b> と <値> と <span class=\"x\">印</span>")).toBe("本文 強調 と <値> と 印");
    expect(excerptPlainText("設定 <name> を入れる")).toBe("設定 <name> を入れる");
  });

  it("入れ子の表は外のセルの中の文字にする", () => {
    const text =
      "<table><tr><th>区分</th><th>内容</th></tr><tr><td>X</td><td>前<table><tr><td>p</td><td>q</td></tr><tr><td>r</td><td>s</td></tr></table>後</td></tr></table>";
    const [block] = parseExcerpt(text);
    expect(block.kind).toBe("table");
    if (block.kind !== "table") return;
    expect(block.rows).toHaveLength(2);
    expect(block.rows[1][0].text).toBe("X");
    expect(block.rows[1][1].text).toBe("前\np / q\nr / s\n後");
  });

  it("壊れた HTML（閉じていない・途中で切れた・<table> の無い <tr>）も読める範囲で返す", () => {
    expect(excerptPlainText("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d")).toBe("a | b\nc | d");
    expect(excerptPlainText("<table><tr><td>a</td><td>b</td></tr><tr><td cla")).toBe("a | b");
    expect(excerptPlainText("<td>c</td><td>d</td></tr></tbody></table>後")).toBe("c | d\n後");
    expect(excerptPlainText("</td></tr></table>本文")).toBe("本文");
    expect(excerptPlainText("<table><tr><td>1</td><td colspan=\"2\">2</td></tr><tr><td>a</td><td>b</td><td>c</td></tr></table>")).toBe(
      "1 | 2 | \na | b | c"
    );
  });

  it("script / style は中身ごと捨て、実体参照の <script> は文字のまま（実行しない）", () => {
    const text =
      "<table><tr><th>名</th></tr><tr><td><script>window.__xss = 1</script>値<img src=x onerror=\"window.__xss=2\"></td></tr></table>" +
      "<style>body{}</style>&lt;script&gt;alert(1)&lt;/script&gt;";
    expect(excerptPlainText(text)).toBe("名\n値\n<script>alert(1)</script>");
    expect(excerptPlainText("前<script>let a = '<table>';</script>後")).toBe("前後");
    expect(excerptPlainText("前<script>途中で切れた")).toBe("前");
  });

  it("数値の実体参照を戻し、制御文字は戻さない", () => {
    expect(excerptPlainText("A&#12354;&#x3044;&nbsp;B&#0;&unknown;")).toBe("Aあい B&#0;&unknown;");
  });
});

describe("ExcerptContent / 根拠のカード", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    delete (window as { __xss?: number }).__xss;
  });

  it("表は共通の表（DataTable）で出し、HTML として注入しない", () => {
    const text = `前の本文<br>次の行${TABLE}<script>window.__xss = 1</script><img src=x onerror="window.__xss=2">`;
    act(() => root.render(<ExcerptContent text={text} tableLabel="抜粋の表" />));
    const table = container.querySelector("table");
    expect(table).not.toBeNull();
    expect(table?.getAttribute("aria-label")).toBe("抜粋の表");
    expect(Array.from(container.querySelectorAll("th")).map((cell) => cell.textContent)).toEqual(["項目", "値"]);
    expect(Array.from(container.querySelectorAll("tbody td")).map((cell) => cell.textContent)).toEqual([
      "受付&番号",
      "A\nB",
      "期限",
      "30 日",
    ]);
    expect(container.querySelector("p")?.textContent).toBe("前の本文\n次の行");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).not.toContain("<");
    expect((window as { __xss?: number }).__xss).toBeUndefined();
  });

  it("見出しの行の無い表は先頭の行を見出しにし、1 行だけの表は行の文字にする", () => {
    act(() =>
      root.render(
        <ExcerptContent text={"<table><tr><td>項目</td><td>記録</td></tr><tr><td>A</td><td>B</td></tr></table><table><tr><td>x</td><td>y</td></tr></table>"} />
      )
    );
    expect(container.querySelectorAll("table")).toHaveLength(1);
    expect(Array.from(container.querySelectorAll("th")).map((cell) => cell.textContent)).toEqual(["項目", "記録"]);
    expect(container.querySelector("p")?.textContent).toBe("x | y");
  });

  it("カードの 3 行の抜粋は表を「セル | セル」の行にする", () => {
    const item: EvidenceListItem = {
      key: "doc-a:1",
      fileName: "文書 A.pdf",
      text: TABLE,
      usedInAnswer: null,
      answerUse: null,
      score: null,
      rerankScore: null,
    };
    act(() => root.render(<EvidenceCard item={item} index={0} />));
    const excerpt = container.querySelector('[data-testid="citation-text"]');
    expect(excerpt?.textContent).toBe("項目 | 値\n受付&番号 | A B\n期限 | 30 日");
    expect(excerpt?.className).toContain("line-clamp-3");
  });
});
