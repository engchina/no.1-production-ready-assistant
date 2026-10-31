import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Banner } from "../src";

describe("Banner の progress（#2031）", () => {
  it("本文 → 操作 → 進行の順に、同じ Banner の中に出す", () => {
    const html = renderToStaticMarkup(
      <Banner
        severity="warning"
        action={<button type="button">再取得</button>}
        progress={<div data-testid="progress">再取得しています</div>}
      >
        再取得してください。
      </Banner>
    );
    const order = ["再取得してください。", ">再取得<", 'data-testid="progress"'].map((text) => html.indexOf(text));
    expect(order.every((index) => index >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    // 進行は Banner（role="status"）の中にある。
    expect(html.indexOf('role="status"')).toBeLessThan(order[2]!);
  });

  it("progress が無ければ進行の枠を描かない", () => {
    const html = renderToStaticMarkup(<Banner severity="info">本文</Banner>);
    expect(html).not.toContain("mt-2 min-w-0");
  });
});
