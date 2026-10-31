import type { Page } from "@playwright/test";

/**
 * 余白のトークン（rem）を px にする。余白とレイアウト寸法は rem（14px ルート）で決まるため、
 * e2e で間隔を確かめるときは 8px / 16px のような px を書かず、rem からルートの文字サイズで求める
 * （AGENTS.md「単位の境界」。#800 で gap-[8px] / gap-[16px] を gap-2 / gap-4 にしたとき、px の期待値が残った）。
 */
export async function remToPx(page: Page, rem: number) {
  const rootFontSize = await page.evaluate(() =>
    Number.parseFloat(getComputedStyle(document.documentElement).fontSize)
  );
  return rem * rootFontSize;
}

/** boundingBox の小数（サブピクセルの丸め）を吸収する許容差（px）。 */
export const SUBPIXEL_TOLERANCE = 0.5;
