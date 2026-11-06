/** 対象選択と新しい会話の隣接を、製品の assertion で確認する。 */
import type { EvaluatePage } from "./layout-checks";

type Assert = (actual: unknown) => {
  toBe(expected: unknown): unknown;
  toBeLessThan(expected: number): unknown;
};

export async function expectChatTargetNextToNewConversation(page: EvaluatePage, expect: Assert) {
  const measured = await page.evaluate(() => {
    const toolbar = document.querySelector<HTMLElement>("[data-chat-toolbar]");
    const target = toolbar?.querySelector<HTMLElement>("[data-chat-target-selector]");
    const button = toolbar?.lastElementChild;
    if (!toolbar || !target || !(button instanceof HTMLButtonElement)) {
      return { found: false, gapDifference: Infinity, fitsPage: false };
    }
    const targetBox = target.getBoundingClientRect();
    const buttonBox = button.getBoundingClientRect();
    const gap = parseFloat(getComputedStyle(toolbar).columnGap);
    return {
      found: true,
      gapDifference: Math.abs(buttonBox.left - targetBox.right - gap),
      fitsPage: document.documentElement.scrollWidth <= innerWidth,
    };
  }, undefined);
  expect(measured.found).toBe(true);
  expect(measured.gapDifference).toBeLessThan(2);
  expect(measured.fitsPage).toBe(true);
}
