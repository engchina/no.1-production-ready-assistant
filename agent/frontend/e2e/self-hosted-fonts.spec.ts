import { expect, test } from "./fixtures/mock-api";
import type { Page } from "@playwright/test";

import {
  CJK_FALLBACK_SAMPLES,
  DEFERRED_CJK_FAMILIES,
  FONT_MONO_FAMILY,
  FONT_SANS_PREFIX,
  JAPANESE_FONT,
  LANG_FONT_SAMPLES,
  UI_FONT_WEIGHTS,
  faceCoversWeight,
  firstRenderFontFaces,
  japaneseInkByWeight,
  initialStylesheetFontFaces,
  loadCjkFallbackFonts,
  loadJapaneseFonts,
  recordFirstRenderFontFaces,
  renderedCjkFallbackFonts,
  renderedJapaneseFonts,
  renderedLangFonts,
} from "../../../platform/packages/ui/e2e/font-checks";

/** 共有 tokens の太さ（400 / 500 / 600 / 700）。JP・SC・TC・HK は可変書体で、どの太さも本物の字形で出す。 */
const WEIGHTS = UI_FONT_WEIGHTS;

/** 等幅書体（--font-mono）が Google Sans Code で、code / .font-mono に効くことを確かめる。 */
async function checkMonoFont(page: Page) {
  const result = await page.evaluate(async () => {
    const code = document.createElement("code");
    code.textContent = "SELECT 1200000";
    const utility = document.createElement("span");
    utility.className = "font-mono";
    utility.textContent = "doc-0001";
    document.body.append(code, utility);
    const counts: number[] = [];
    for (const weight of [400, 500, 600, 700]) {
      const faces = await document.fonts.load(`${weight} 14px "Google Sans Code"`, "SELECT 1200000");
      counts.push(faces.filter((face) => face.status === "loaded").length);
    }
    const families = [getComputedStyle(code).fontFamily, getComputedStyle(utility).fontFamily];
    code.remove();
    utility.remove();
    return { counts, families };
  });
  expect(result.counts.every((count) => count > 0), "Google Sans Code 400/500/600/700").toBe(true);
  for (const family of result.families) {
    expect(family).toBe(FONT_MONO_FAMILY);
  }
}

/**
 * Noto Sans JP（可変書体）を各太さで読み込み、読み込んだ face の太さの範囲がその太さを含む（合成の太字にならない）こと、
 * 合成の日本語の字を本文と等幅で描いたときに Chromium が自前ホストの Noto Sans JP を使うことを確かめる（#3333）。
 */
async function checkJapaneseFont(page: Page) {
  for (const font of await loadJapaneseFonts(page, WEIGHTS)) {
    expect(font.count, `${JAPANESE_FONT.family} ${font.weight}`).toBeGreaterThan(0);
    expect(font.weights.every((range) => faceCoversWeight(range, font.weight)), `${font.weight}: ${font.weights.join(", ")}`).toBe(true);
  }
  // 太さごとに別の字形（太いほど墨が多い）。近い太さの face で代わりに描くと同じ量になる。
  const ink = await japaneseInkByWeight(page, WEIGHTS);
  for (let index = 1; index < ink.length; index += 1) {
    expect(ink[index]!.ink, JSON.stringify(ink)).toBeGreaterThan(ink[index - 1]!.ink);
  }
  const cdp = await page.context().newCDPSession(page);
  try {
    const rendered = await renderedJapaneseFonts(page, cdp, WEIGHTS);
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

/** Noto Sans JP（各太さを本物の字形で）と等幅書体 Google Sans Code を自前ホストで適用する。 */
async function checkSelfHostedFonts(page: Page) {
  const external: string[] = [];
  page.on("request", (request) => {
    if (/fonts\.(googleapis|gstatic)\.com|fonts\.bunny\.net/u.test(request.url())) external.push(request.url());
  });
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);
  await checkJapaneseFont(page);
  const bodyFont = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
  expect(bodyFont.slice(0, FONT_SANS_PREFIX.length)).toBe(FONT_SANS_PREFIX);
  await checkMonoFont(page);
  expect(external, "外部フォント CDN への通信").toEqual([]);
}

// 書体は @fontsource の woff2 を同一 origin で配信し、外部フォント CDN に依存しない（#75）。
// Noto Sans JP は可変書体（1 つの face が太さ 100〜900 を持つ。#3333）。
test("Noto Sans JP と等幅書体 Google Sans Code を自前ホストで適用する", async ({ page }) => {
  await checkSelfHostedFonts(page);
});

/**
 * Noto Sans JP に無い字だけを、自前ホストの Noto Sans SC → TC → HK で補う（#3292）。字は合成の例。
 * SC・TC・HK の @font-face は主の CSS に入れず、最初の描画の後に別の CSS の chunk で読む（#3305）。
 */
async function checkDeferredCjkFallbackFonts(page: Page) {
  const external: string[] = [];
  page.on("request", (request) => {
    if (/fonts\.(googleapis|gstatic)\.com|fonts\.bunny\.net/u.test(request.url())) external.push(request.url());
  });
  await recordFirstRenderFontFaces(page);
  await page.goto("/");
  // 最初の描画の時点の CSS（dev の <style> を含む）と、index.html が読む CSS（build の成果物）に SC・TC・HK は無い。
  const firstRender = await firstRenderFontFaces(page);
  expect(firstRender.families).toContain(JAPANESE_FONT.family);
  const initial = await initialStylesheetFontFaces(page);
  // dev サーバの index.html は CSS の <link> を持たない（preview・本番だけ主の CSS を確かめられる）。
  if (initial.hrefs.length > 0) expect(initial.families).toContain(JAPANESE_FONT.family);
  for (const family of DEFERRED_CJK_FAMILIES) {
    expect(firstRender.families, `最初の描画の CSS に ${family}`).not.toContain(family);
    expect(initial.families, `index.html の CSS に ${family}`).not.toContain(family);
  }
  await page.evaluate(() => document.fonts.ready);
  const bodyFont = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
  expect(bodyFont.slice(0, FONT_SANS_PREFIX.length)).toBe(FONT_SANS_PREFIX);
  for (const font of await loadCjkFallbackFonts(page, WEIGHTS)) {
    expect(font.count, `${font.family} ${font.weight}`).toBeGreaterThan(0);
  }
  const cdp = await page.context().newCDPSession(page);
  try {
    const rendered = await renderedCjkFallbackFonts(page, cdp);
    for (const sample of CJK_FALLBACK_SAMPLES) {
      for (const kind of ["sans", "mono"] as const) {
        const families = rendered[`${sample.id}-${kind}`];
        expect(families.length, `${sample.id}-${kind}`).toBeGreaterThan(0);
        expect(families.every((family) => family.startsWith(sample.renderedFamily)), `${sample.id}-${kind}: ${families.join(", ")}`).toBe(true);
      }
    }
  } finally {
    await cdp.detach();
  }
  expect(external, "外部フォント CDN への通信").toEqual([]);
}

test("JP に無い簡体字・繁体字・香港の字を、最初の描画の後に読む Noto Sans SC・TC・HK で補う", async ({ page }) => {
  await checkDeferredCjkFallbackFonts(page);
});

/** 内容の言語（lang）ごとに、共通の漢字（合成の例）がその言語の書体で描かれることを確かめる（#3335）。 */
async function checkLangFonts(page: Page) {
  await page.goto("/");
  const cdp = await page.context().newCDPSession(page);
  try {
    const rendered = await renderedLangFonts(page, cdp);
    for (const sample of LANG_FONT_SAMPLES) {
      for (const kind of ["sans", "mono"] as const) {
        const families = rendered[`${sample.lang}-${kind}`];
        expect(families.length, `${sample.lang}-${kind}`).toBeGreaterThan(0);
        expect(families.every((family) => family.startsWith(sample.renderedFamily)), `${sample.lang}-${kind}: ${families.join(", ")}`).toBe(true);
      }
    }
  } finally {
    await cdp.detach();
  }
}

// 利用者の内容は lang を付けた要素で、言語の字形の書体を先頭にする（#3335）。画面の文言（html の ja）は JP のまま。
test("内容の言語（lang）で共通の漢字を JP・SC・TC・HK の字形で描き分ける", async ({ page }) => {
  await checkLangFonts(page);
});

// この spec は mobile-375 の project で実行しない（playwright.config.ts）。端末の幅によらず同じ読み方であることを、
// 375px の viewport でも確かめる。
test.describe("375px", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("JP に無い字を最初の描画の後に読む Noto Sans SC・TC・HK で補う（375px）", async ({ page }) => {
    await checkDeferredCjkFallbackFonts(page);
  });

  test("Noto Sans JP と等幅書体 Google Sans Code を自前ホストで適用する（375px）", async ({ page }) => {
    await checkSelfHostedFonts(page);
  });

  test("内容の言語（lang）で共通の漢字を JP・SC・TC・HK の字形で描き分ける（375px）", async ({ page }) => {
    await checkLangFonts(page);
  });
});
