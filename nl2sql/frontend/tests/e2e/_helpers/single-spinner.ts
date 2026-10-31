import { expect, type Locator, type Page } from "@playwright/test";

/**
 * 画面に見えている共有 Spinner（`svg.animate-spin`）。reduced-motion で回転を止めていても数える。
 * `hidden` のタブパネルなど表示されていないものは数えない。
 */
export function visibleSpinners(scope: Page | Locator): Locator {
  return scope.locator("svg.animate-spin:visible");
}

/**
 * 同じ処理のスピナーが 1 つだけであること（UX 契約 messaging §3.7、#416）。
 * `owner` を渡すと、その 1 つが `owner` の中にあることも確かめる。
 */
export async function expectSingleSpinner(page: Page, owner?: Locator) {
  await expect(visibleSpinners(page)).toHaveCount(1);
  if (owner) await expect(visibleSpinners(owner)).toHaveCount(1);
}
