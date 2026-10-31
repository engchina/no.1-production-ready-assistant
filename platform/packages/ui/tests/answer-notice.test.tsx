// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { AnswerNotice, answerNoticeSegments } from "../src";

// #2405: 回答の注意書き（4 つのチャットと RAG 検索で共通）。

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

describe("AnswerNotice", () => {
  it("改行を保ち、URL だけをリンクにして、HTML は文字のまま出す", () => {
    act(() =>
      root.render(
        <AnswerNotice notice={"1 行目 <b>太字</b>\n連絡先は https://example.com/help です。"} label="回答の注意書き" />,
      ),
    );
    const aside = host.querySelector("aside");
    expect(aside?.getAttribute("aria-label")).toBe("回答の注意書き");
    expect(aside?.querySelector("b")).toBeNull();
    expect(aside?.textContent).toContain("<b>太字</b>");
    expect(aside?.querySelectorAll("br")).toHaveLength(1);
    const link = aside?.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://example.com/help");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(aside?.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
  });

  it("空・null なら何も出さない", () => {
    act(() => root.render(<AnswerNotice notice={null} />));
    expect(host.innerHTML).toBe("");
    act(() => root.render(<AnswerNotice notice="  " />));
    expect(host.innerHTML).toBe("");
  });
});

describe("answerNoticeSegments", () => {
  it("末尾の句読点・閉じ括弧を URL に含めず、http / https 以外はリンクにしない", () => {
    expect(answerNoticeSegments("窓口（https://example.com/a?b=1）へ。")).toEqual([
      { kind: "text", value: "窓口（" },
      { kind: "link", value: "https://example.com/a?b=1" },
      { kind: "text", value: "）へ。" },
    ]);
    expect(answerNoticeSegments("see http://example.com.")).toEqual([
      { kind: "text", value: "see " },
      { kind: "link", value: "http://example.com" },
      { kind: "text", value: "." },
    ]);
    expect(answerNoticeSegments("javascript:alert(1)")).toEqual([{ kind: "text", value: "javascript:alert(1)" }]);
  });
});
