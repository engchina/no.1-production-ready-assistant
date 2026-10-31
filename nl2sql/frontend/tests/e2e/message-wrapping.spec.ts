import { expect, test, type Locator, type Page } from "./_helpers/test";

import { expectToastPlacement } from "./_helpers/toast";
import { FIXTURE_BASE_URL } from "./_helpers/web-server";

// `tests/fixtures/*.tsx` を `/@react-refresh` と一緒に読み込むので、dev server の変換が要る。preview（PR の CI）でも
// fixture 用の dev server を開く（tests/e2e/_helpers/web-server.ts。#1583）。
test.use({ baseURL: FIXTURE_BASE_URL });

// #899: 通知（Toast）・Banner・FormStatus の本文が語の途中で折り返さない。
// 以前は通知の幅が 22rem 固定で、業務プロファイルの保存の後の「Oracle Profile の反映が完了しました。」が
// desktop でも「…完了しま / した。」と 2 行になった。通知の幅は内容に合わせて 22〜32rem の間で広がり、
// メッセージの本文は `.pr-message-text`（word-break: auto-phrase / text-wrap: pretty）で文節で折り返す。
async function openFixture(page: Page, theme: "light" | "dark") {
  await page.route("**/__message-wrapping", (route) => route.fulfill({ contentType: "text/html", body:
    `<html class="${theme === "dark" ? "dark" : ""}" lang="ja"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/tests/fixtures/message-wrapping.tsx"></script></body></html>` }));
  // HMR timestamp 付きの自己 import で fixture が二重に実行されないようにする（toast-placement.spec.ts と同じ）。
  await page.route(/\/tests\/fixtures\/message-wrapping\.tsx\?t=\d+/, (route) => route.fulfill({
    contentType: "text/javascript", body: 'export * from "/tests/fixtures/message-wrapping.tsx";' }));
  // 通知が確かめる前に消えないよう時刻を止める。
  await page.clock.install({ time: new Date("2026-10-04T09:00:00+09:00") });
  await page.clock.pauseAt(new Date("2026-10-04T09:00:01+09:00"));
  await page.goto("/__message-wrapping");
}

interface WrappedLines {
  /** 描画された行（折り返しの位置で分けた文字列）。 */
  lines: string[];
  /** 語の途中の折り返し（`前の行の末尾 / 次の行の先頭`）。 */
  midWord: string[];
}

/**
 * 要素の本文の折り返しの位置を、文字ごとの描画位置から求める。
 * 折り返しの位置が日本語の語の境界（`Intl.Segmenter` の word）か空白でなければ「語の途中」とする。
 * 文節は語の並びなので、文節で折り返していれば語の途中にはならない。
 * ただし片仮名の複合語の中の境界（「バック｜グラウンド」）は許す。Chrome の auto-phrase は複合語を構成する語の間でも
 * 折り返すが、`Intl.Segmenter` は片仮名の連なりを 1 語にまとめるため。
 */
async function wrappedLines(target: Locator): Promise<WrappedLines> {
  return target.evaluate((element) => {
    const lines: string[] = [];
    const midWord: string[] = [];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let current = "";
    let lastTop: number | null = null;
    const segmenter = new Intl.Segmenter("ja", { granularity: "word" });
    const boundariesOf = (text: string) => {
      const set = new Set<number>([0, text.length]);
      for (const segment of segmenter.segment(text)) set.add(segment.index);
      return set;
    };
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.textContent ?? "";
      const boundaries = boundariesOf(text);
      const range = document.createRange();
      for (let index = 0; index < text.length; index += 1) {
        range.setStart(node, index);
        range.setEnd(node, index + 1);
        const rect = range.getClientRects()[0];
        if (!rect || rect.width === 0) {
          current += text[index];
          continue;
        }
        const lineHeight = parseFloat(getComputedStyle(node.parentElement!).lineHeight) || rect.height;
        if (lastTop !== null && rect.top - lastTop > lineHeight / 2) {
          lines.push(current);
          const atSpace = /\s/u.test(text[index - 1] ?? " ") || /\s/u.test(text[index]);
          const inKatakana = /\p{Script=Katakana}/u.test(text[index - 1] ?? "") && /\p{Script=Katakana}/u.test(text[index]);
          if (index > 0 && !atSpace && !inKatakana && !boundaries.has(index)) {
            midWord.push(`${current.slice(-4)} / ${text.slice(index, index + 4)}`);
          }
          current = "";
        }
        lastTop = rect.top;
        current += text[index];
      }
    }
    if (current) lines.push(current.trim());
    return { lines: lines.map((line) => line.trim()), midWord };
  });
}

for (const theme of ["light", "dark"] as const) {
  test(`${theme}: 通知と画面の中のメッセージは語の途中で折り返さない`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    const mobile = testInfo.project.name === "mobile-375";
    await openFixture(page, theme);
    const region = page.getByRole("region", { name: "通知", exact: true });

    // 業務プロファイルの保存の後の通知。desktop は 1 行に収まり、375px でも語の途中で切らない。
    await page.getByRole("group", { name: "ページ操作" }).getByRole("button", { name: "保存", exact: true }).click();
    const synced = region.getByRole("status").filter({ hasText: "Oracle Profile の反映が完了しました。" });
    await expect(synced).toBeVisible();
    await expectToastPlacement(page);
    const syncedLines = await wrappedLines(synced.locator("[data-message-text]"));
    expect(syncedLines.midWord).toEqual([]);
    // 以前の「…完了しま / した。」（`Intl.Segmenter` は「しま｜した」を語の境界とみなすため、行の先頭で確かめる）。
    expect(syncedLines.lines.some((line) => /^(?:した|ました)/u.test(line))).toBe(false);
    if (!mobile) expect(syncedLines.lines).toEqual(["Oracle Profile の反映が完了しました。"]);
    // 短い通知は従来の幅（22rem）のまま、長くても 32rem まで。
    const rem = await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize));
    const width = (await region.boundingBox())!.width;
    if (!mobile) {
      expect(width).toBeGreaterThanOrEqual(22 * rem - 1);
      expect(width).toBeLessThanOrEqual(32 * rem + 1);
    }
    await page.screenshot({ path: testInfo.outputPath(`message-wrapping-toast-${theme}.png`) });

    // 2 文の通知: 文末を優先して折り返し、文の中でも語の途中で切らない。
    await page.getByRole("group", { name: "ページ操作" }).getByRole("button", { name: "長い通知" }).click();
    const long = region.getByRole("status").filter({ hasText: "バックグラウンドで続きます" });
    await expect(long).toBeVisible();
    expect((await wrappedLines(long.locator("[data-message-text]"))).midWord).toEqual([]);
    if (!mobile) {
      // 積んだ通知は最も広い通知の幅にそろい、上限（32rem）を超えない。
      expect((await region.boundingBox())!.width).toBeLessThanOrEqual(32 * rem + 1);
    }
    await expectToastPlacement(page);

    // Banner（見出し・本文）と FormStatus。
    const banner = page.getByRole("alert").filter({ hasText: "システムテーブルの作成に失敗しました。" });
    for (const message of await banner.locator("[data-message-text]").all()) {
      expect((await wrappedLines(message)).midWord).toEqual([]);
    }
    const formStatus = page.getByRole("alert").filter({ hasText: "別の名前を入力してください。" });
    expect((await wrappedLines(formStatus.locator("[data-message-text]"))).midWord).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`message-wrapping-${theme}.png`), fullPage: true });

    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });
}
