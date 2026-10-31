// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { AnswerFooter } from "../src";

// #2707: チャットの回答のカードの末尾（3 製品共通）は 注意書き → 回答の根拠 → 評価 の順。

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

describe("AnswerFooter", () => {
  it("注意書き → 回答の根拠 → 評価 の順に並べる", () => {
    act(() =>
      root.render(
        <AnswerFooter
          notice="合成の注意書き"
          noticeLabel="回答の注意書き"
          evidence={<div data-testid="evidence">根拠</div>}
          feedback={<div data-testid="feedback">評価</div>}
        />,
      ),
    );
    const footer = host.querySelector('[data-testid="answer-footer"]');
    const order = Array.from(footer?.children ?? []).map(
      (element) => element.getAttribute("data-testid") ?? element.tagName.toLowerCase(),
    );
    expect(order).toEqual(["answer-notice", "evidence", "feedback"]);
    expect(footer?.querySelector("aside")?.getAttribute("aria-label")).toBe("回答の注意書き");
  });

  it("注意書きが空なら根拠と評価だけを出し、どれも無ければ何も出さない", () => {
    act(() => root.render(<AnswerFooter notice="  " evidence={<div data-testid="evidence" />} />));
    expect(host.querySelector("aside")).toBeNull();
    expect(host.querySelector('[data-testid="evidence"]')).not.toBeNull();
    act(() => root.render(<AnswerFooter notice={null} />));
    expect(host.innerHTML).toBe("");
  });
});
