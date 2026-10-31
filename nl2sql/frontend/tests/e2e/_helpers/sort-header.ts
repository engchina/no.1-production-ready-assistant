import { expect, type Locator } from "@playwright/test";

/** 共有 DataTable の並べ替え見出し（`<th aria-sort>` 直下の button）。 */
export function sortHeaderButtons(container: Locator) {
  return container.locator("th[aria-sort] > button");
}

/**
 * 共有 DataTable の並べ替え見出しの契約（#530 / platform README §7 #13）。
 * - 当たり判定は見出しセル全体（button の幅 = th の幅）
 * - 表・メタの文字（12px / 600）、折り返しなし・切れなし
 * - 高さは --button-height-sm（マウス 32px、タッチ端末 44px）
 */
export async function expectCompactSortHeaders(container: Locator) {
  const buttons = sortHeaderButtons(container);
  expect(await buttons.count()).toBeGreaterThan(0);
  for (const button of await buttons.all()) {
    if (!(await button.isVisible())) continue;
    const metrics = await button.evaluate((node) => {
      const cell = node.closest("th")!;
      const label = node.querySelector("span")!;
      const labelRange = document.createRange();
      labelRange.selectNodeContents(label);
      return {
        touch: matchMedia("(pointer: coarse)").matches,
        widthGap: Math.abs(cell.getBoundingClientRect().width - node.getBoundingClientRect().width),
        clipped: node.scrollWidth > node.clientWidth + 1,
        labelLines: new Set(Array.from(labelRange.getClientRects()).filter((rect) => rect.width > 0).map((rect) => Math.round(rect.top))).size,
        whiteSpace: getComputedStyle(node).whiteSpace,
      };
    });
    await expect(button).toHaveCSS("font-size", "12px");
    await expect(button).toHaveCSS("font-weight", "600");
    await expect(button).toHaveCSS("min-height", metrics.touch ? "44px" : "32px");
    expect(metrics.widthGap).toBeLessThanOrEqual(1);
    expect(metrics.clipped).toBe(false);
    expect(metrics.labelLines).toBe(1);
    expect(metrics.whiteSpace).toBe("nowrap");
  }
}
