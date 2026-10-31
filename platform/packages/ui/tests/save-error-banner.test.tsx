// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { SaveErrorBanner } from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
let scrolls: unknown[];
const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  scrolls = [];
  HTMLElement.prototype.scrollIntoView = function scrollIntoView(options?: boolean | ScrollIntoViewOptions) {
    scrolls.push(options);
  };
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
});

describe("SaveErrorBanner（#585）", () => {
  it("文言が無ければ何も描かない（空の面を出さない）", () => {
    expect(renderToStaticMarkup(<SaveErrorBanner message={null} />)).toBe("");
    expect(renderToStaticMarkup(<SaveErrorBanner message="" />)).toBe("");
  });

  it("danger の Banner（role=alert）で文言を出す", () => {
    const html = renderToStaticMarkup(
      <SaveErrorBanner message="保存できませんでした。時間をおいて再試行してください。" testId="save-error" />
    );
    expect(html).toContain('data-testid="save-error"');
    expect(html).toContain('role="alert"');
    expect(html).toContain("保存できませんでした。");
  });

  it("失敗が出たとき・同じ文言でも保存をやり直したときに画面へ入れ、文言が消えたら入れない", () => {
    act(() => root.render(<SaveErrorBanner message="失敗しました。" attemptKey={1} />));
    expect(scrolls).toEqual([{ block: "center", inline: "nearest" }]);
    act(() => root.render(<SaveErrorBanner message="失敗しました。" attemptKey={1} />));
    expect(scrolls).toHaveLength(1);
    act(() => root.render(<SaveErrorBanner message="失敗しました。" attemptKey={2} />));
    expect(scrolls).toHaveLength(2);
    act(() => root.render(<SaveErrorBanner message={null} attemptKey={3} />));
    expect(scrolls).toHaveLength(2);
    expect(host.textContent).toBe("");
  });
});
