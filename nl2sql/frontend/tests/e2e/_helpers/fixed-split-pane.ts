import { expect, type Locator, type Page } from "@playwright/test";

const LAYOUT_SELECTOR = '.fixed-split-pane[data-split-layout="split"]';

export async function expectSplitPaneReservedTrack(pane: Locator) {
  const left = pane.locator(':scope > [data-split-pane-side="left"]');
  const divider = pane.locator(':scope > [role="separator"]');
  const right = pane.locator(':scope > [data-split-pane-side="right"]');
  const [leftBox, dividerBox, rightBox] = await Promise.all([
    left.boundingBox(),
    divider.boundingBox(),
    right.boundingBox(),
  ]);

  expect(leftBox).not.toBeNull();
  expect(dividerBox).not.toBeNull();
  expect(rightBox).not.toBeNull();
  expect(dividerBox!.width).toBeCloseTo(14, 0);
  expect(leftBox!.x + leftBox!.width).toBeLessThanOrEqual(dividerBox!.x + 1);
  expect(dividerBox!.x + dividerBox!.width).toBeLessThanOrEqual(rightBox!.x + 1);
}

export async function expectVisibleSplitPanesReserved(page: Page) {
  const panes = page.locator(LAYOUT_SELECTOR);
  const count = await panes.count();
  for (let index = 0; index < count; index += 1) {
    await expectSplitPaneReservedTrack(panes.nth(index));
  }
}

export async function expectSplitPaneStacked(pane: Locator) {
  const left = pane.locator(':scope > [data-split-pane-side="left"]');
  const divider = pane.locator(':scope > [role="separator"]');
  const right = pane.locator(':scope > [data-split-pane-side="right"]');
  const [leftBox, rightBox] = await Promise.all([left.boundingBox(), right.boundingBox()]);

  await expect(pane).toHaveAttribute("data-split-layout", "stacked");
  await expect(divider).toBeHidden();
  expect(leftBox).not.toBeNull();
  expect(rightBox).not.toBeNull();
  expect(rightBox!.y).toBeGreaterThanOrEqual(leftBox!.y + leftBox!.height);
}
