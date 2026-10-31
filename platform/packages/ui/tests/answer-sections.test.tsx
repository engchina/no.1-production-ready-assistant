// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ANSWER_SECTION_COLLAPSE_AFTER, AnswerSections, MarkdownInlineText } from "../src";

// #2328: 回答の本文の節と根拠の行（RAG のチャット・RAG 検索・Agent のチャットで共通）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("AnswerSections", () => {
  it("段落・見出しつきの箇条書き・補足の行・根拠の行（押せる行と文字だけの行）を出す", () => {
    const onOpen = vi.fn();
    act(() =>
      root.render(
        <AnswerSections
          live
          blocks={[
            { kind: "paragraph", key: "p", content: "要約の段落。" },
            {
              kind: "section",
              key: "s",
              title: "資料により確認できた事項",
              entries: [
                { key: "e0", content: "項目 1", citations: [{ key: "c1", label: "根拠：文書 A p.3", ariaLabel: "根拠：文書 A p.3 を表示", onOpen }] },
                { key: "e1", content: "項目 2", citations: [{ key: "c2", label: "根拠：文書 B" }] },
                { key: "e2", content: "補足", muted: true },
              ],
            },
            { kind: "content", key: "c", content: <div data-testid="table">表</div>, citations: [{ key: "c3", label: "根拠：文書 C" }] },
          ]}
        />
      )
    );
    const container = host.querySelector('[data-testid="answer-text"]')!;
    expect(container.getAttribute("aria-live")).toBe("polite");
    expect(container.querySelector("p")?.textContent).toBe("要約の段落。");
    expect(container.querySelector("h4")?.textContent).toBe("資料により確認できた事項");
    const items = container.querySelectorAll("li");
    expect(items).toHaveLength(3);
    expect(items[2].className).toContain("list-none");
    const link = container.querySelector('[data-testid="answer-citation-link"]') as HTMLButtonElement;
    expect(link.getAttribute("aria-label")).toBe("根拠：文書 A p.3 を表示");
    // #3353: 文言は span に入れ、Button の `[&>span]:truncate` で長い名前を 1 行に省略する（行は幅に収める）。
    expect(link.querySelector(":scope > span")?.textContent).toBe("根拠：文書 A p.3");
    expect(link.parentElement?.className).toContain("min-w-0");
    // 省略していないときは全文の Tooltip を出さない。
    expect(link.getAttribute("data-truncated")).toBeNull();
    act(() => link.click());
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(Array.from(container.querySelectorAll('[data-testid="answer-citation-text"]')).map((node) => node.textContent)).toEqual([
      "根拠：文書 B",
      "根拠：文書 C",
    ]);
    expect(container.querySelector('[data-testid="table"]')).not.toBeNull();
  });

  // #3353: 押せる根拠の行は、名前（省略してよい）と位置（省略しない）を別の span に分ける。
  it("押せる根拠の行の位置（location）は名前と別の span に分け、label の末尾と合わないときは分けない", () => {
    const onOpen = vi.fn();
    act(() =>
      root.render(
        <AnswerSections
          blocks={[
            { kind: "paragraph", key: "p1", content: "段落 1。", citations: [{ key: "c1", label: "根拠：文書 A.pdf p.3", location: "p.3", onOpen }] },
            { kind: "paragraph", key: "p2", content: "段落 2。", citations: [{ key: "c2", label: "根拠：文書 B.pdf p.4", location: "p.9", onOpen }] },
          ]}
        />,
      ),
    );
    const links = host.querySelectorAll('[data-testid="answer-citation-link"]');
    expect(Array.from(links[0].querySelectorAll(":scope > span")).map((node) => node.textContent)).toEqual([
      "根拠：文書 A.pdf",
      " p.3",
    ]);
    expect(links[0].textContent).toBe("根拠：文書 A.pdf p.3");
    expect(links[0].querySelectorAll(":scope > span")[1].className).toContain("shrink-0");
    expect(Array.from(links[1].querySelectorAll(":scope > span")).map((node) => node.textContent)).toEqual([
      "根拠：文書 B.pdf p.4",
    ]);
  });

  // #2361: 項目の多い節は「ほか N 件」で切らず、先頭だけを出して「すべて表示」で同じ書式のまま残りを開く。
  it("畳める節は先頭の項目だけを出し、すべて表示で全件を同じ書式で開く", () => {
    const entries = [
      { key: "intro", content: "次の内容は回答に載せていません。", muted: true },
      ...Array.from({ length: ANSWER_SECTION_COLLAPSE_AFTER + 2 }, (_, index) => ({
        key: `e${index}`,
        content: `項目 ${index + 1}`,
      })),
    ];
    act(() =>
      root.render(
        <AnswerSections
          blocks={[
            { kind: "section", key: "few", title: "資料により確認できた事項", entries: entries.slice(0, 3) },
            { kind: "section", key: "many", title: "資料から確認できなかった事項", entries, collapsible: true },
          ]}
        />
      )
    );
    const [few, many] = Array.from(host.querySelectorAll('[data-testid="answer-section"]'));
    // 項目の少ない節・畳めない節にはボタンを出さない。
    expect(few.querySelector('[data-testid="answer-section-toggle"]')).toBeNull();
    const items = () => Array.from(many.querySelectorAll("li:not(.list-none)")).map((node) => node.textContent);
    expect(many.querySelector("li.list-none")?.textContent).toBe("次の内容は回答に載せていません。");
    expect(items()).toHaveLength(ANSWER_SECTION_COLLAPSE_AFTER);
    const toggle = many.querySelector('[data-testid="answer-section-toggle"]') as HTMLButtonElement;
    expect(toggle.textContent).toBe(`すべて表示（全 ${ANSWER_SECTION_COLLAPSE_AFTER + 2} 件）`);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.getAttribute("aria-controls")).toBe(many.querySelector("ul")?.id);
    act(() => toggle.click());
    expect(items()).toHaveLength(ANSWER_SECTION_COLLAPSE_AFTER + 2);
    expect(items().at(-1)).toBe(`項目 ${ANSWER_SECTION_COLLAPSE_AFTER + 2}`);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(toggle.textContent).toBe("折りたたむ");
  });

  it("MarkdownInlineText は段落の中の太字とリンクだけを描き、改行を保つ", () => {
    act(() => root.render(<p><MarkdownInlineText text={"**太字** と [文書](https://example.com)\n2 行目"} /></p>));
    const paragraph = host.querySelector("p")!;
    expect(paragraph.querySelector("strong")?.textContent).toBe("太字");
    expect(paragraph.querySelector("a")?.getAttribute("href")).toBe("https://example.com");
    expect(paragraph.querySelectorAll("br")).toHaveLength(1);
  });
});
