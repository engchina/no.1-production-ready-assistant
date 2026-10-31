import { expect, type Locator, type Page } from "@playwright/test";

/** 両画面で、統一前の compact な外観と keyboard による選択を検証する。 */
export async function expectLegacyOntologyControls(page: Page, scope: Locator) {
  const mobile = (page.viewportSize()?.width ?? 0) < 640;
  const modes = scope.getByTestId("ontology-graph-view-mode");
  const all = scope.getByTestId("ontology-graph-mode-all");
  const originalMode = await modes.locator('[aria-pressed="true"]').getAttribute("data-testid");
  await expect(modes).toHaveCSS("min-height", mobile ? "44px" : "40px");
  // SQL 結果の入れ子領域は 375px 時に約 180px。ページ幅だけでは内部の裁切を検出できない。
  const layout = await modes.evaluate(element => {
    const bounds = element.getBoundingClientRect();
    const graph = element.parentElement!.parentElement!;
    return {
      width: graph.clientWidth,
      scrollWidth: graph.scrollWidth,
      buttonsFit: Array.from(element.querySelectorAll("button")).every(button => {
        const box = button.getBoundingClientRect();
        return box.left >= bounds.left && box.right <= bounds.right + 1
          && box.top >= bounds.top && box.bottom <= bounds.bottom + 1;
      }),
    };
  });
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.width + 1);
  expect(layout.buttonsFit).toBe(true);
  await expect(modes).toHaveCSS("border-top-width", "1px");
  await expect(modes).not.toHaveCSS("box-shadow", "none");
  await all.focus();
  await page.keyboard.press("Enter");
  await expect(all).toBeFocused();
  await expect(all).toHaveAttribute("aria-pressed", "true");
  await expect(all).toHaveCSS("height", mobile ? "36px" : "32px");
  // アイコンは 14 / 16 / 20 / 24px に統一（奇数 px はボケる。README §3）。
  await expect(all.locator("svg")).toHaveCSS("width", "14px");
  // フォーカスの表示はグローバルの :focus-visible の outline 1 つ（ring と二重にしない。#355）。
  const focus = await all.evaluate(element => {
    const style = getComputedStyle(element);
    return { visible: element.matches(":focus-visible"), outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth, shadow: style.boxShadow };
  });
  expect(focus.visible).toBe(true);
  expect(focus.outlineStyle).toBe("solid");
  expect(focus.outlineWidth).toBe("2px");
  expect(focus.shadow).toBe("none");
  // semantic primary と完全一致する塗り（統一後の薄い混色背景とは異なる）。
  const selectedColors = await all.evaluate(element => {
    const probe = document.createElement("span");
    probe.style.color = "var(--color-accent-emphasis)";
    element.append(probe);
    const primary = getComputedStyle(probe).color;
    probe.remove();
    return { primary };
  });
  await expect(all).toHaveCSS("background-color", selectedColors.primary);

  const physical = scope.getByTestId("ontology-graph-mode-physical_er");
  await physical.focus();
  await page.keyboard.press("Space");
  await expect(physical).toHaveAttribute("aria-pressed", "true");
  await expect(all).toHaveAttribute("aria-pressed", "false");
  await all.click();

  const legend = scope.getByTestId("ontology-graph-legend").getByRole("button").first();
  // 補助テキストの下限は 12px（README §3）。
  await expect(legend).toHaveCSS("font-size", "12px");
  await expect(legend).toHaveCSS("border-top-width", "0px");
  await expect(legend).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await legend.focus();
  await page.keyboard.press("Space");
  await expect(legend).toHaveAttribute("aria-pressed", "false");
  await expect(legend).toHaveCSS("text-decoration-line", "line-through");
  await page.mouse.move(0, 0);
  await expect(legend).toHaveCSS("opacity", "0.4");
  await page.keyboard.press("Enter");
  await expect(legend).toHaveAttribute("aria-pressed", "true");
  await expect(legend).toHaveCSS("opacity", "1");
  await expect(legend).toHaveCSS("text-decoration-line", "none");

  const zoom = scope.getByRole("button", { name: "グラフを拡大", exact: true });
  // 共有 Button sm（32px、タッチ端末は 44px）。
  await expect(zoom).toHaveCSS("height", mobile ? "44px" : "32px");
  await expect(zoom).toHaveCSS("padding-left", "12px");
  await expect(zoom.locator("svg")).toHaveCSS("width", "16px");
  if (originalMode) await scope.getByTestId(originalMode).click();
}

/**
 * グラフの線（`.react-flow__edge`）をクリックする。SVG の <g> は Playwright の visibility の判定ができないため、
 * 線を画面に入れてから、線の中点を画面の座標でクリックする（#1788）。
 */
export async function clickOntologyEdge(page: Page, edge: Locator) {
  const point = await edge.locator("path.react-flow__edge-path").evaluate((element) => {
    const path = element as SVGPathElement;
    path.scrollIntoView({ block: "center" });
    const middle = path.getPointAtLength(path.getTotalLength() / 2);
    const matrix = path.getScreenCTM()!;
    return {
      x: middle.x * matrix.a + middle.y * matrix.c + matrix.e,
      y: middle.x * matrix.b + middle.y * matrix.d + matrix.f,
    };
  });
  await page.mouse.click(point.x, point.y);
}
