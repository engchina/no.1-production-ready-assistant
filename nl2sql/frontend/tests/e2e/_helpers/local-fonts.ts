import { expect, type Page } from "@playwright/test";

import {
  CJK_FALLBACK_SAMPLES,
  DEFERRED_CJK_FAMILIES,
  FONT_MONO_FAMILY,
  FONT_SANS_PREFIX,
  JAPANESE_FONT,
  faceCoversWeight,
  LANG_FONT_SAMPLES,
  firstRenderFontFaces,
  initialStylesheetFontFaces,
  japaneseInkByWeight,
  loadCjkFallbackFonts,
  loadJapaneseFonts,
  renderedCjkFallbackFonts,
  renderedJapaneseFonts,
  renderedLangFonts,
} from "../../../../../platform/packages/ui/e2e/font-checks";

export { recordFirstRenderFontFaces } from "../../../../../platform/packages/ui/e2e/font-checks";

/** 自前ホストの書体の太さ（共有 tokens の太さ。JP・SC・TC・HK は可変書体で、どの太さも本物の字形で出す）。 */
export const LOCAL_FONT_WEIGHTS = [400, 500, 600, 700] as const;

/**
 * Noto Sans SC・TC・HK の @font-face が主の CSS に無く、最初の描画の後に読まれることを確かめる（#3305）。
 * `recordFirstRenderFontFaces(page)` を `page.goto` の前に呼んでおく。
 */
export async function expectDeferredCjkFontFaces(page: Page) {
  const firstRender = await firstRenderFontFaces(page);
  expect(firstRender.families).toContain(JAPANESE_FONT.family);
  const initial = await initialStylesheetFontFaces(page);
  // dev サーバの index.html は CSS の <link> を持たない（preview・本番だけ主の CSS を確かめられる）。
  if (initial.hrefs.length > 0) expect(initial.families).toContain(JAPANESE_FONT.family);
  for (const family of DEFERRED_CJK_FAMILIES) {
    expect(firstRender.families, `最初の描画の CSS に ${family}`).not.toContain(family);
    expect(initial.families, `index.html の CSS に ${family}`).not.toContain(family);
  }
}

/** 画面内の全テキストと入力欄を走査し、局所指定やコード要素の字体漏れを検出する。 */
export async function expectLocalUiFonts(page: Page) {
  await page.evaluate(() => document.fonts.ready);
  const result = await page.evaluate(() => {
    const canonical = (font: string) => font.split(",").map((part) => part.trim().replaceAll('"', "")).join(",");
    const expected = getComputedStyle(document.body).fontFamily;
    const mismatches: { tag: string; className: string; font: string }[] = [];
    const codeFont = getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim();
    let checked = 0;
    for (const node of document.querySelectorAll("body *")) {
      if (!(node instanceof HTMLElement || node instanceof SVGElement)) continue;
      if (["SCRIPT", "STYLE", "NOSCRIPT", "OPTION"].includes(node.tagName)) continue;
      const style = getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden" || !node.getClientRects().length) continue;
      const hasText = Array.from(node.childNodes).some(
        (child) => child.nodeType === Node.TEXT_NODE && child.textContent?.trim()
      );
      if (!hasText && !node.matches("input, textarea, select")) continue;
      checked += 1;
      if (canonical(style.fontFamily) !== canonical(expected) && canonical(style.fontFamily) !== canonical(codeFont)) {
        mismatches.push({ tag: node.tagName, className: node.getAttribute("class") ?? "", font: style.fontFamily });
      }
    }
    return { expected, codeFont, checked, mismatches };
  });
  expect(result.expected.slice(0, FONT_SANS_PREFIX.length)).toBe(FONT_SANS_PREFIX);
  // カスタムプロパティの値は宣言のまま（引用符つきの "Roboto"）で返るため、引用符を外して比べる。
  expect(result.codeFont.replaceAll('"Roboto"', "Roboto")).toBe(FONT_MONO_FAMILY);
  expect(result.checked).toBeGreaterThan(0);
  expect(result.mismatches).toEqual([]);
}

/**
 * Noto Sans JP（可変書体。#3333）を各太さで読み込み、読み込んだ face の太さの範囲がその太さを含む（合成の太字にならない）こと、
 * 合成の日本語の字を各太さの本文と等幅で描いたときに Chromium が自前ホストの Noto Sans JP を使うことを確かめる。
 */
export async function expectLocalJapaneseFonts(page: Page) {
  for (const font of await loadJapaneseFonts(page, LOCAL_FONT_WEIGHTS)) {
    expect(font.count, `${JAPANESE_FONT.family} ${font.weight}`).toBeGreaterThan(0);
    expect(
      font.weights.every((range) => faceCoversWeight(range, font.weight)),
      `${font.weight}: ${font.weights.join(", ")}`
    ).toBe(true);
  }
  // 太さごとに別の字形（太いほど墨が多い）。近い太さの face で代わりに描くと同じ量になる。
  const ink = await japaneseInkByWeight(page, LOCAL_FONT_WEIGHTS);
  for (let index = 1; index < ink.length; index += 1) {
    expect(ink[index]!.ink, JSON.stringify(ink)).toBeGreaterThan(ink[index - 1]!.ink);
  }
  const cdp = await page.context().newCDPSession(page);
  try {
    const rendered = await renderedJapaneseFonts(page, cdp, LOCAL_FONT_WEIGHTS);
    for (const [key, fonts] of Object.entries(rendered)) {
      expect(fonts.length, key).toBeGreaterThan(0);
      expect(
        fonts.every((font) => font.familyName.startsWith(JAPANESE_FONT.renderedFamily) && font.isCustomFont),
        `${key}: ${JSON.stringify(fonts)}`
      ).toBe(true);
    }
  } finally {
    await cdp.detach();
  }
}

/**
 * Noto Sans JP に無い字（合成の例）だけを、自前ホストの Noto Sans SC → TC → HK で補うことを確かめる（#3292）。
 * 各太さの読み込みと、本文・等幅で Chromium が実際に使った書体を見る。
 */
export async function expectLocalCjkFallbackFonts(page: Page) {
  for (const font of await loadCjkFallbackFonts(page, LOCAL_FONT_WEIGHTS)) {
    expect(font.count, `${font.family} ${font.weight}`).toBeGreaterThan(0);
  }
  const cdp = await page.context().newCDPSession(page);
  try {
    const rendered = await renderedCjkFallbackFonts(page, cdp);
    for (const sample of CJK_FALLBACK_SAMPLES) {
      for (const kind of ["sans", "mono"] as const) {
        const families = rendered[`${sample.id}-${kind}`];
        expect(families.length, `${sample.id}-${kind}`).toBeGreaterThan(0);
        expect(
          families.every((family) => family.startsWith(sample.renderedFamily)),
          `${sample.id}-${kind}: ${families.join(", ")}`
        ).toBe(true);
      }
    }
  } finally {
    await cdp.detach();
  }
}

/**
 * 内容の言語（`lang`）ごとに、JP・SC・TC・HK のすべてにある共通の漢字（合成の例）が、その言語の書体で描かれることを
 * 確かめる（#3335）。`lang="zh-Hans"` は SC、`zh-Hant` は TC、`zh-HK` は HK、`ja` は JP。本文と等幅の両方。
 */
export async function expectLangFonts(page: Page) {
  const cdp = await page.context().newCDPSession(page);
  try {
    const rendered = await renderedLangFonts(page, cdp);
    for (const sample of LANG_FONT_SAMPLES) {
      for (const kind of ["sans", "mono"] as const) {
        const families = rendered[`${sample.lang}-${kind}`];
        expect(families.length, `${sample.lang}-${kind}`).toBeGreaterThan(0);
        expect(
          families.every((family) => family.startsWith(sample.renderedFamily)),
          `${sample.lang}-${kind}: ${families.join(", ")}`
        ).toBe(true);
      }
    }
  } finally {
    await cdp.detach();
  }
}
