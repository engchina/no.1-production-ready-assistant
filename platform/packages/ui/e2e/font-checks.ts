/**
 * 3 製品の e2e が共通に使う、自前ホストの書体の検査（#3292）。
 *
 * - `FONT_SANS_PREFIX` / `FONT_MONO_FAMILY`: 共有 tokens の `--font-sans` / `--font-mono` の計算値（先頭・全体）
 * - `JAPANESE_FONT`: Noto Sans JP（主の CSS）。可変書体（`@fontsource-variable`）で、CSS の書体名は `Noto Sans JP Variable`、
 *   Chromium が描画に使った書体の名前は `Noto Sans JP` で始まる（#3333）。`loadJapaneseFonts` が各太さを読み込み、読み込んだ
 *   face の太さの範囲がその太さを含む（合成の太字にならない）かを `faceCoversWeight` で確かめる。`renderedJapaneseFonts` は
 *   合成の日本語の字を各太さの本文（`--font-sans`）と等幅（`code`）で描き、Chromium が実際に使った書体を CDP で返す
 *   （可変書体の書体の名前は太さによらず既定の instance の名前。例: `Noto Sans JP Thin`）。`japaneseInkByWeight` は各太さで
 *   描いた字の墨の量を返す（太さごとに別の字形なら、太いほど多い。近い太さの face で代わりに描くと同じ量になる）
 * - `CJK_FALLBACK_SAMPLES`: Noto Sans JP に無く、Noto Sans SC・TC・HK のそれぞれだけにある合成の字。SC・TC・HK は
 *   可変書体（`@fontsource-variable`）で、CSS の書体名（`family`）は `Noto Sans SC Variable` など、Chromium が描画に
 *   使った書体の名前（`renderedFamily`）は `Noto Sans SC` で始まる（#3328）
 * - `loadCjkFallbackFonts`: Noto Sans SC / TC / HK の各太さを、合成の字で `document.fonts.load` する
 * - `renderedCjkFallbackFonts`: 合成の字を本文の書体（`--font-sans`）と等幅（`code`）で描き、Chromium が実際に使った
 *   書体の名前を CDP（`CSS.getPlatformFontsForNode`）で返す。JP が先頭のまま、JP に無い字だけが SC → TC → HK で補われるか
 * - SC / TC / HK の `@font-face` は主の CSS に入れず、最初の描画の後に別の CSS の chunk で読む（#3305。
 *   `loadStylesAfterFirstRender`）。`recordFirstRenderFontFaces`（`page.goto` の前）が最初の描画の時点の `@font-face` の
 *   書体を記録し、`firstRenderFontFaces` で読む。`initialStylesheetFontFaces` は index.html が読む（preload を含む）CSS の
 *   `@font-face` の書体、`waitForCjkFallbackFaces` は後から読む face が登録されるまで待つ
 *
 * - `renderedLangFonts`: 内容の言語（`lang`）ごとに、共通の漢字が JP / SC / TC / HK のどれで描かれるか（#3335）。
 *   `renderedFontsForSelector` は画面の部品（回答など）の文字を描いた書体
 *
 * 製品の spec は相対パスで import し（RAG・Agent `../../../platform/packages/ui/e2e/font-checks`、NL2SQL
 * `../../../../platform/packages/ui/e2e/font-checks`）、結果を製品の `expect` で確かめる。
 * `@playwright/test` の型は import しない（`layout-checks.ts` と同じ理由）。
 */

/** 共有 tokens の太さ（`--font-weight-regular` / `medium` / `semibold` / `bold`）。 */
export const UI_FONT_WEIGHTS = [400, 500, 600, 700] as const;

/**
 * Noto Sans JP（主の CSS）。`family` は CSS の書体名、`renderedFamily` は Chromium が描画に使った書体の名前の先頭。
 * `text` は Noto Sans JP にある合成の字（漢字・ひらがな・カタカナ。業務の内容ではない）。空白を入れない（等幅では空白だけが
 * Google Sans Code で描かれる）。
 */
export const JAPANESE_FONT = { family: "Noto Sans JP Variable", renderedFamily: "Noto Sans JP", text: "表示の確認ひらがなカタカナ" } as const;

/**
 * 本文の書体（`--font-sans`）の計算値の先頭。日本語の字形を優先し、JP に無い字だけを SC → TC → HK で補う。
 * Web の書体の直後に、寸法を合わせた代わりの書体（`Noto Sans JP Fallback`。端末の書体の local()。#3335）を置く。
 */
export const FONT_SANS_PREFIX =
  '"Noto Sans JP Variable", "Noto Sans JP Fallback", "Noto Sans SC Variable", "Noto Sans TC Variable", "Noto Sans HK Variable", Roboto,';

/** 等幅書体（`--font-mono`）の計算値。Google Sans Code に無い字は本文と同じ順で補う。 */
export const FONT_MONO_FAMILY =
  '"Google Sans Code", "Google Sans Code Fallback", "Noto Sans JP Variable", "Noto Sans JP Fallback", "Noto Sans SC Variable", "Noto Sans TC Variable", "Noto Sans HK Variable", Roboto, monospace';

/**
 * Noto Sans JP に無い合成の字（業務の内容ではなく、字の収録の違いだけで選ぶ）。
 * 「们」は SC だけ、「亷」は TC と HK（TC が先）、「乪」は HK だけに収録される（JP には無い）。
 */
export const CJK_FALLBACK_SAMPLES = [
  { id: "simplified", family: "Noto Sans SC Variable", renderedFamily: "Noto Sans SC", text: "们" },
  { id: "traditional", family: "Noto Sans TC Variable", renderedFamily: "Noto Sans TC", text: "亷" },
  { id: "hongkong", family: "Noto Sans HK Variable", renderedFamily: "Noto Sans HK", text: "乪" },
] as const;

export type CjkFallbackSampleId = (typeof CJK_FALLBACK_SAMPLES)[number]["id"];

/** `page.evaluate` を持つもの（Playwright の `Page`）。 */
export interface EvaluatePage {
  evaluate<R, A>(pageFunction: (arg: A) => R | Promise<R>, arg: A): Promise<R>;
}

/** CDP の session（Playwright の `CDPSession`。`page.context().newCDPSession(page)`）。 */
export interface CdpSessionLike {
  send(method: never, params?: never): Promise<unknown>;
}

/** 書体の 1 つの太さを読み込んだ結果。`count` は読み込めた face の数（0 なら自前ホストの face が無い）。 */
export interface LoadedFont {
  family: string;
  weight: number;
  count: number;
}

/** 最初の描画の後に読む書体（主の CSS に入れない。#3305）。 */
export const DEFERRED_CJK_FAMILIES: readonly string[] = CJK_FALLBACK_SAMPLES.map((sample) => sample.family);

/** `page.addInitScript` を持つもの（Playwright の `Page`）。 */
export interface InitScriptPage {
  addInitScript(script: (key: string) => void, key: string): Promise<unknown>;
}

/** 最初の描画（`#root` に最初の子が入った時点）の CSS の `@font-face` の書体（重複なし）。 */
export interface FirstRenderFontFaces {
  families: string[];
}

const FIRST_RENDER_KEY = "__prFirstRenderFontFaces";

/**
 * 最初の描画の時点の `@font-face` の書体を記録する init script を足す（`page.goto` の前に呼ぶ）。
 * `#root` に最初の子が入った commit の通知（microtask）で、その時点の全 stylesheet（dev の `<style>` を含む）を読む。
 */
export async function recordFirstRenderFontFaces(page: InitScriptPage): Promise<void> {
  await page.addInitScript((key: string) => {
    const families = () => {
      const found = new Set<string>();
      const walk = (rules: CSSRuleList) => {
        for (const rule of Array.from(rules)) {
          if (rule instanceof CSSFontFaceRule) {
            found.add(rule.style.getPropertyValue("font-family").replaceAll(/["']/gu, "").trim());
          } else if ("cssRules" in rule && rule.cssRules instanceof CSSRuleList) {
            walk(rule.cssRules);
          }
        }
      };
      for (const sheet of Array.from(document.styleSheets)) {
        try {
          walk(sheet.cssRules);
        } catch {
          // 別 origin の stylesheet は読めない（自前ホストの書体の CSS は同じ origin）。
        }
      }
      return [...found].sort();
    };
    const observer = new MutationObserver(() => {
      if (!document.getElementById("root")?.firstChild) return;
      observer.disconnect();
      (window as unknown as Record<string, unknown>)[key] = { families: families() };
    });
    observer.observe(document, { childList: true, subtree: true });
  }, FIRST_RENDER_KEY);
}

/** `recordFirstRenderFontFaces` が記録した最初の描画の時点の書体。描画されるまで待つ。 */
export async function firstRenderFontFaces(page: EvaluatePage, timeoutMs = 10_000): Promise<FirstRenderFontFaces> {
  return page.evaluate(
    async ({ key, timeoutMs }) => {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const recorded = (window as unknown as Record<string, unknown>)[key] as FirstRenderFontFaces | undefined;
        if (recorded) return recorded;
        if (Date.now() > deadline) throw new Error("最初の描画の記録がありません（recordFirstRenderFontFaces を goto の前に呼ぶ）");
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    },
    { key: FIRST_RENDER_KEY, timeoutMs }
  );
}

/**
 * index.html が読む CSS（`<link>` の href が `.css` のもの。stylesheet・preload を含む）の `@font-face` の書体。
 * build の成果物（preview・本番）で、後から読む chunk が主の CSS・preload に入っていないことを確かめる
 * （dev サーバの index.html は CSS の `<link>` を持たないので `hrefs` は空になる）。
 */
export async function initialStylesheetFontFaces(page: EvaluatePage): Promise<{ hrefs: string[]; families: string[] }> {
  return page.evaluate(async () => {
    const html = await (await fetch(location.href, { cache: "no-store" })).text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    const hrefs = Array.from(doc.querySelectorAll("link[href]"))
      .map((link) => new URL(link.getAttribute("href") ?? "", location.href))
      .filter((url) => url.pathname.endsWith(".css"))
      .map((url) => url.href);
    const families = new Set<string>();
    for (const href of hrefs) {
      const css = await (await fetch(href)).text();
      for (const match of css.matchAll(/@font-face\s*\{[^}]*?font-family:\s*([^;}]+)/gu)) {
        families.add((match[1] ?? "").replaceAll(/["']/gu, "").trim());
      }
    }
    return { hrefs, families: [...families].sort() };
  }, undefined);
}

/** 最初の描画の後に読む Noto Sans SC / TC / HK の face が `document.fonts` に登録されるまで待つ。 */
export async function waitForCjkFallbackFaces(page: EvaluatePage, timeoutMs = 10_000): Promise<void> {
  await page.evaluate(
    async ({ families, timeoutMs }) => {
      const deadline = Date.now() + timeoutMs;
      const registered = () => {
        const names = new Set(Array.from(document.fonts, (face) => face.family.replaceAll(/["']/gu, "")));
        return families.every((family) => names.has(family));
      };
      while (!registered()) {
        if (Date.now() > deadline) throw new Error(`後から読む書体が登録されません: ${families.join(", ")}`);
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    },
    { families: [...DEFERRED_CJK_FAMILIES], timeoutMs }
  );
}

/**
 * Noto Sans SC / TC / HK の各太さを、それぞれの合成の字で読み込む。unicode-range の分割のうち、その字の分だけを取る。
 * face は最初の描画の後に登録されるので、先に `waitForCjkFallbackFaces` で待つ。
 */
export async function loadCjkFallbackFonts(page: EvaluatePage, weights: readonly number[]): Promise<LoadedFont[]> {
  await waitForCjkFallbackFaces(page);
  return page.evaluate(
    async ({ samples, weights }) => {
      const loaded: { family: string; weight: number; count: number }[] = [];
      for (const sample of samples) {
        for (const weight of weights) {
          const faces = await document.fonts.load(`${weight} 14px "${sample.family}"`, sample.text);
          loaded.push({
            family: sample.family,
            weight,
            count: faces.filter((face) => face.status === "loaded").length,
          });
        }
      }
      return loaded;
    },
    { samples: CJK_FALLBACK_SAMPLES.map((sample) => ({ ...sample })), weights: [...weights] }
  );
}

/** 合成の字を描いたときに Chromium が使った書体の名前（`familyName`。太さの名前が付くことがある）。 */
export type RenderedCjkFallbackFonts = Record<`${CjkFallbackSampleId}-${"sans" | "mono"}`, string[]>;

/** Playwright の CDPSession.send は CDP の method 名で型が決まる。ここで使う 4 つだけを型にする。 */
type CdpSend = (
  method: string,
  params?: Record<string, unknown>
) => Promise<{ root?: { nodeId: number }; nodeId?: number; fonts?: { familyName: string }[] }>;

/** `selector` に当たる要素の子の文字を描いた書体の名前（CDP の `CSS.getPlatformFontsForNode`）。 */
export async function renderedFontsForSelector(cdp: CdpSessionLike, selector: string): Promise<string[]> {
  const send = cdp.send.bind(cdp) as unknown as CdpSend;
  await send("DOM.enable");
  await send("CSS.enable");
  const { root } = await send("DOM.getDocument");
  const { nodeId } = await send("DOM.querySelector", { nodeId: root?.nodeId, selector });
  if (!nodeId) throw new Error(`要素がありません: ${selector}`);
  const { fonts } = await send("CSS.getPlatformFontsForNode", { nodeId });
  return (fonts ?? []).map((font) => font.familyName);
}

/**
 * 内容の言語（`lang`）ごとの漢字の書体（#3335）。JP・SC・TC・HK のすべてにある共通の漢字（合成の例）を、
 * `lang` を付けた要素の本文（`span`）と等幅（`code`）で描き、`renderedFamily` で始まる書体だけで描かれるかを確かめる。
 * 画面の文言（`<html lang="ja">`）と `lang="ja"` の内容は JP、`zh-Hans` は SC、`zh-Hant` は TC、`zh-HK` は HK。
 */
export const LANG_FONT_SAMPLES = [
  { lang: "ja", family: "Noto Sans JP Variable", renderedFamily: "Noto Sans JP" },
  { lang: "zh-Hans", family: "Noto Sans SC Variable", renderedFamily: "Noto Sans SC" },
  { lang: "zh-Hant", family: "Noto Sans TC Variable", renderedFamily: "Noto Sans TC" },
  { lang: "zh-HK", family: "Noto Sans HK Variable", renderedFamily: "Noto Sans HK" },
] as const;

/** 4 書体のすべてにある共通の漢字（同じ符号で地域ごとに字形が違う例。業務の内容ではない）。 */
export const SHARED_HAN_SAMPLE = "骨直角";

export type RenderedLangFonts = Record<`${(typeof LANG_FONT_SAMPLES)[number]["lang"]}-${"sans" | "mono"}`, string[]>;

/** {@link LANG_FONT_SAMPLES} の各言語で共通の漢字を描き、実際に使った書体の名前を返す。描いた要素は返す前に消す。 */
export async function renderedLangFonts(page: EvaluatePage, cdp: CdpSessionLike): Promise<RenderedLangFonts> {
  await waitForCjkFallbackFaces(page);
  await page.evaluate(
    async ({ samples, text }) => {
      const host = document.createElement("div");
      host.setAttribute("data-lang-font-samples", "");
      for (const sample of samples) {
        const block = document.createElement("div");
        block.lang = sample.lang;
        const sans = document.createElement("span");
        sans.setAttribute("data-lang-font-sample", `${sample.lang}-sans`);
        sans.textContent = text;
        const mono = document.createElement("code");
        mono.setAttribute("data-lang-font-sample", `${sample.lang}-mono`);
        mono.textContent = text;
        block.append(sans, mono);
        host.append(block);
      }
      document.body.append(host);
      for (const sample of samples) {
        for (const weight of [400, 700]) await document.fonts.load(`${weight} 14px "${sample.family}"`, text);
      }
      await document.fonts.ready;
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    },
    { samples: LANG_FONT_SAMPLES.map((sample) => ({ ...sample })), text: SHARED_HAN_SAMPLE }
  );
  const result = {} as RenderedLangFonts;
  try {
    for (const sample of LANG_FONT_SAMPLES) {
      for (const kind of ["sans", "mono"] as const) {
        const key = `${sample.lang}-${kind}` as const;
        result[key] = await renderedFontsForSelector(cdp, `[data-lang-font-sample="${key}"]`);
      }
    }
  } finally {
    await page.evaluate(() => document.querySelector("[data-lang-font-samples]")?.remove(), undefined);
  }
  return result;
}

/**
 * 合成の字を本文の書体（`body` を継承する `span`）と等幅（`code`）で描き、実際に使った書体の名前を返す。
 * 描いた要素は返す前に消す。
 */
export async function renderedCjkFallbackFonts(
  page: EvaluatePage,
  cdp: CdpSessionLike
): Promise<RenderedCjkFallbackFonts> {
  await waitForCjkFallbackFaces(page);
  await page.evaluate(async (samples) => {
    const host = document.createElement("div");
    host.setAttribute("data-cjk-font-samples", "");
    for (const sample of samples) {
      const sans = document.createElement("span");
      sans.setAttribute("data-cjk-font-sample", `${sample.id}-sans`);
      sans.textContent = sample.text;
      const mono = document.createElement("code");
      mono.setAttribute("data-cjk-font-sample", `${sample.id}-mono`);
      mono.textContent = sample.text;
      host.append(sans, mono);
    }
    document.body.append(host);
    await document.fonts.ready;
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }, CJK_FALLBACK_SAMPLES.map((sample) => ({ ...sample })));

  // Playwright の CDPSession.send は CDP の method 名で型が決まる。ここで使う 4 つだけを型にする。
  const send = cdp.send.bind(cdp) as unknown as (
    method: string,
    params?: Record<string, unknown>
  ) => Promise<{ root?: { nodeId: number }; nodeId?: number; fonts?: { familyName: string }[] }>;
  const result = {} as RenderedCjkFallbackFonts;
  try {
    await send("DOM.enable");
    await send("CSS.enable");
    const { root } = await send("DOM.getDocument");
    for (const sample of CJK_FALLBACK_SAMPLES) {
      for (const kind of ["sans", "mono"] as const) {
        const key = `${sample.id}-${kind}` as const;
        const { nodeId } = await send("DOM.querySelector", {
          nodeId: root?.nodeId,
          selector: `[data-cjk-font-sample="${key}"]`,
        });
        const { fonts } = await send("CSS.getPlatformFontsForNode", { nodeId });
        result[key] = (fonts ?? []).map((font) => font.familyName);
      }
    }
  } finally {
    await page.evaluate(() => document.querySelector("[data-cjk-font-samples]")?.remove(), undefined);
  }
  return result;
}

/** 日本語の書体の 1 つの太さを読み込んだ結果。`weights` は読み込んだ face の太さの範囲（`FontFace.weight`。例: `100 900`）。 */
export interface LoadedJapaneseFont {
  weight: number;
  count: number;
  weights: string[];
}

/** face の太さの範囲（`400` / `100 900`）が `weight` を含むか。含まない太さは Chromium が合成の太字・細字で出す。 */
export function faceCoversWeight(range: string, weight: number): boolean {
  const [low, high = low] = range.trim().split(/\s+/u).map(Number);
  return low !== undefined && high !== undefined && low <= weight && weight <= high;
}

/** Noto Sans JP の各太さを合成の字で読み込む（unicode-range の slice のうち、その字の分だけを取る）。 */
export async function loadJapaneseFonts(page: EvaluatePage, weights: readonly number[]): Promise<LoadedJapaneseFont[]> {
  return page.evaluate(
    async ({ font, weights }) => {
      const loaded: { weight: number; count: number; weights: string[] }[] = [];
      for (const weight of weights) {
        const faces = (await document.fonts.load(`${weight} 14px "${font.family}"`, font.text)).filter(
          (face) => face.status === "loaded"
        );
        loaded.push({ weight, count: faces.length, weights: faces.map((face) => face.weight) });
      }
      return loaded;
    },
    { font: { ...JAPANESE_FONT }, weights: [...weights] }
  );
}

/** 合成の日本語の字を描いたときに Chromium が使った書体（`${太さ}-sans` / `${太さ}-mono`）。 */
export type RenderedJapaneseFonts = Record<string, { familyName: string; isCustomFont: boolean }[]>;

/**
 * 合成の日本語の字を各太さの本文の書体（`body` を継承する `span`）と等幅（`code`。Google Sans Code に無い字は JP で補う）で描き、
 * 実際に使った書体の名前を返す。描いた要素は返す前に消す。
 */
export async function renderedJapaneseFonts(
  page: EvaluatePage,
  cdp: CdpSessionLike,
  weights: readonly number[]
): Promise<RenderedJapaneseFonts> {
  await page.evaluate(
    async ({ text, weights }) => {
      const host = document.createElement("div");
      host.setAttribute("data-jp-font-samples", "");
      for (const weight of weights) {
        for (const kind of ["sans", "mono"] as const) {
          const node = document.createElement(kind === "sans" ? "span" : "code");
          node.setAttribute("data-jp-font-sample", `${weight}-${kind}`);
          node.style.fontWeight = String(weight);
          node.textContent = text;
          host.append(node);
        }
      }
      document.body.append(host);
      await document.fonts.ready;
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    },
    { text: JAPANESE_FONT.text, weights: [...weights] }
  );
  const send = cdp.send.bind(cdp) as unknown as (
    method: string,
    params?: Record<string, unknown>
  ) => Promise<{ root?: { nodeId: number }; nodeId?: number; fonts?: { familyName: string; isCustomFont: boolean }[] }>;
  const result: RenderedJapaneseFonts = {};
  try {
    await send("DOM.enable");
    await send("CSS.enable");
    const { root } = await send("DOM.getDocument");
    for (const weight of weights) {
      for (const kind of ["sans", "mono"] as const) {
        const key = `${weight}-${kind}`;
        const { nodeId } = await send("DOM.querySelector", { nodeId: root?.nodeId, selector: `[data-jp-font-sample="${key}"]` });
        const { fonts } = await send("CSS.getPlatformFontsForNode", { nodeId });
        result[key] = (fonts ?? []).map(({ familyName, isCustomFont }) => ({ familyName, isCustomFont }));
      }
    }
  } finally {
    await page.evaluate(() => document.querySelector("[data-jp-font-samples]")?.remove(), undefined);
  }
  return result;
}

/**
 * 合成の日本語の字を、各太さの Noto Sans JP で canvas に描いた墨の量（alpha の合計）。`loadJapaneseFonts` の後に呼ぶ。
 * 太さごとに本物の字形で描けば太いほど多くなり、face に無い太さを近い太さの face で代わりに描くと同じ量になる。
 */
export async function japaneseInkByWeight(page: EvaluatePage, weights: readonly number[]): Promise<{ weight: number; ink: number }[]> {
  return page.evaluate(
    ({ font, weights }) => {
      const canvas = document.createElement("canvas");
      canvas.width = 640;
      canvas.height = 64;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("canvas の 2d context がありません");
      return weights.map((weight) => {
        context.clearRect(0, 0, canvas.width, canvas.height);
        context.font = `${weight} 40px "${font.family}"`;
        context.fillText(font.text, 0, 48);
        const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
        let ink = 0;
        for (let index = 3; index < data.length; index += 4) ink += data[index] ?? 0;
        return { weight, ink };
      });
    },
    { font: { ...JAPANESE_FONT }, weights: [...weights] }
  );
}
