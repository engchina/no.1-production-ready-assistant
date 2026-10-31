import { expect, type Locator } from "@playwright/test";

/**
 * 入力欄・選択欄（`id` を持つ input / combobox）のラベルと必須の印が 1 行に出ていることを確かめる（#1746）。
 *
 * 段の幅（`width="xs"` など）は入力欄そのものに付き、ラベルは入力欄の幅で折り返さない（README §4「操作部品の高さと幅」）。
 * - ラベル（`label[for=id]`）の高さが 1 行分（文字の line-height。必須の印を含む行の高さ）
 * - 必須の印（「必須」）がラベルの文字と同じ行（縦の範囲がラベルの中）
 * - ラベルが画面の幅をはみ出さない（横あふれを作らない）
 */
export async function expectFieldLabelOnOneLine(control: Locator) {
  const id = await control.getAttribute("id");
  expect(id, "欄に id がありません").toBeTruthy();
  const label = control.page().locator(`label[for="${id}"]`);
  await expect(label).toBeVisible();
  const layout = await label.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const lineHeight = Number.parseFloat(getComputedStyle(element).lineHeight);
    // ラベルの文字（必須の印を除く）の行の数。折り返すと行ごとに別の矩形になる。
    const range = document.createRange();
    const textNode = Array.from(element.childNodes).find((node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim());
    let lines = 0;
    if (textNode) {
      range.selectNodeContents(textNode);
      lines = new Set(Array.from(range.getClientRects()).map((line) => Math.round(line.top))).size;
    }
    const badge = Array.from(element.querySelectorAll("span")).find((span) => span.textContent?.trim() === "必須");
    const badgeRect = badge?.getBoundingClientRect();
    return {
      height: rect.height,
      lineHeight,
      lines,
      right: rect.right,
      viewportWidth: document.documentElement.clientWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      badgeInside: badgeRect ? badgeRect.top >= rect.top - 0.5 && badgeRect.bottom <= rect.bottom + 0.5 : true,
    };
  });
  expect(layout.lines, "ラベルの文字が折り返しています").toBe(1);
  expect(layout.height, "ラベルの高さが 1 行分を超えています").toBeLessThanOrEqual(layout.lineHeight * 1.4);
  expect(layout.badgeInside, "必須の印がラベルと別の行にあります").toBe(true);
  expect(layout.right).toBeLessThanOrEqual(layout.viewportWidth + 0.5);
  expect(layout.pageScrollWidth).toBeLessThanOrEqual(layout.viewportWidth);
}
