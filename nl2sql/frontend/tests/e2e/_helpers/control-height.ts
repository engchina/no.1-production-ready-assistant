import type { Page } from "@playwright/test";

/** 操作部品の高さの段（README §4「操作部品の高さと幅」、#613）。 */
export const CONTROL_HEIGHT = { sm: 32, md: 36, lg: 40 } as const;

/**
 * 入力欄・選択欄・ボタンの期待する高さ（px）。タッチ端末（pointer: coarse）では段によらず 44px。
 * 画面幅ではなく入力方式で決まるので、desktop の project で 375px に縮めた画面は 32 / 36 / 40px のまま。
 */
export async function expectedControlHeight(page: Page, size: keyof typeof CONTROL_HEIGHT = "md") {
  const coarse = await page.evaluate(() => window.matchMedia("(pointer: coarse)").matches);
  return coarse ? 44 : CONTROL_HEIGHT[size];
}
