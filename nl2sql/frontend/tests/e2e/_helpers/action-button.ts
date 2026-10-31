import { expect, type Locator } from "@playwright/test";

/** テーマの root font-size に依存せず、主操作の実寸と日本語ラベルの収まりを確認する。 */
export async function expectLargeActionButton(button: Locator) {
  await expect(button).toBeVisible();
  const metrics = await button.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      height: rect.height,
      expectedHeight: window.matchMedia("(pointer: coarse)").matches ? 44 : 40,
      clipped: element.scrollWidth > element.clientWidth,
    };
  });
  expect(metrics.height).toBe(metrics.expectedHeight);
  expect(metrics.clipped).toBe(false);
}
