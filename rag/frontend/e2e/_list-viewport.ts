import { expect, type Page, type TestInfo } from "@playwright/test";
import { expectNoPageOverflow } from "./_helpers";

/** 高い desktop では余白へ拡張し、mobile の表示密度とページ送りを維持する。 */
export async function expectGrowingList(page: Page, testInfo: TestInfo, id: string) {
  const mobile = testInfo.project.name === "mobile";
  const width = mobile ? 375 : 1280;
  await page.setViewportSize({ width, height: 900 });
  const area = page.getByTestId(`${id}-scroll-region`);
  await expect(area).toBeVisible();
  const before = await area.evaluate((node) => node.clientHeight);
  await page.setViewportSize({ width, height: 1400 });
  if (mobile) {
    await expect.poll(() => area.evaluate((node) => node.clientHeight)).toBe(before);
  } else {
    await expect.poll(() => area.evaluate((node) => node.clientHeight)).toBeGreaterThan(before + 200);
  }
  await expect.poll(() => area.evaluate((node) => node.scrollHeight > node.clientHeight)).toBe(true);
  await area.focus();
  await area.press("End");
  await expect.poll(() => area.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
  if (!mobile) {
    await page.setViewportSize({ width: 1920, height: 1400 });
    await expect(area).toBeVisible();
  }
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath(`${id}-tall.png`), fullPage: true });
  const pagination = page.getByTestId(`${id}-pagination`);
  await pagination.getByRole("button", { name: "次へ", exact: true }).click();
  await expect(pagination).toContainText("26–50 / 60 件");
}
