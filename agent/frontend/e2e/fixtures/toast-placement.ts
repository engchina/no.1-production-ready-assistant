import { expect, type Page } from "@playwright/test";

/**
 * 通知（Toast）の置き場所の e2e の確認（#1728）。3 製品の e2e に同じ内容の helper を置く
 * （RAG `e2e/_toast-placement.ts`・Agent `e2e/fixtures/toast-placement.ts`・NL2SQL `tests/e2e/_helpers/toast.ts`。内容をそろえる）。
 *
 * 通知は画面幅ごとに決まった 1 か所に出し、ページの内容（PageHeader の操作の有無・スクロール）で動かさない。
 * - desktop（md 以上）: 画面の右下。右端から 1rem、下端から `--toast-offset-bottom`（8rem）。下端に固定された入力欄
 *   （チャット）がそれより高い画面では、入力欄の上端から 1rem 上（`--toast-clear-bottom`。#2103）。
 * - 375px（md 未満）: 上端のバーに重ねる。上端から 0.5rem、右端から 1rem、左はメニューのボタンの後ろ（1rem + 44px）。
 */

type Box = { x: number; y: number; width: number; height: number };

export function overlaps(a: Box, b: Box) {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

/** md 以上の通知の幅の上限（`--toast-width-max`）。主操作と重ならないかは、最も広い通知で確かめる。 */
const TOAST_WIDTH_MAX_REM = 32;
/** 主操作と重ならないかを確かめる通知の高さ。本文と補足（description）の 1 件（約 72px）に余裕を見た高さ。 */
const TOAST_ZONE_HEIGHT_REM = 6;

interface ToastGeometry {
  mode: string | undefined;
  rect: Box;
  rem: number;
  offset: number;
  /** 下端に固定された入力欄の上端から画面の下端までの高さ（`--toast-clear-bottom`。無ければ 0）。 */
  clear: number;
  vw: number;
  vh: number;
}

async function readToastGeometry(page: Page): Promise<ToastGeometry> {
  const region = page.getByRole("region", { name: "通知", exact: true });
  await expect(region).toBeAttached();
  return region.evaluate((element) => {
    const { x, y, width, height } = element.getBoundingClientRect();
    const probe = document.createElement("div");
    probe.style.cssText = "position:absolute;visibility:hidden;height:var(--toast-offset-bottom)";
    document.body.append(probe);
    const offset = probe.getBoundingClientRect().height;
    probe.style.height = "var(--toast-clear-bottom, 0px)";
    const clear = probe.getBoundingClientRect().height;
    probe.remove();
    return {
      mode: (element as HTMLElement).dataset.toastPlacement,
      rect: { x, y, width, height },
      rem: parseFloat(getComputedStyle(document.documentElement).fontSize),
      offset,
      clear,
      vw: document.documentElement.clientWidth,
      vh: window.innerHeight,
    };
  });
}

/** 通知の領域が、画面幅ごとの決まった 1 か所にある（ページの内容によらない）。 */
export async function expectToastPlacement(page: Page) {
  const { mode, rect, rem, offset, clear, vw, vh } = await readToastGeometry(page);
  // 右端は画面の右端から 1rem（desktop・375px とも）。
  expect(Math.abs(vw - rem - (rect.x + rect.width))).toBeLessThanOrEqual(1);
  if (vw < 768) {
    expect(mode).toBe("top-bar");
    expect(Math.abs(rect.y - rem / 2)).toBeLessThanOrEqual(1);
    const menu = page.getByTestId("nav-drawer-trigger");
    if (await menu.count()) {
      const menuBox = (await menu.boundingBox())!;
      expect(rect.x).toBeGreaterThanOrEqual(menuBox.x + menuBox.width);
    }
    return;
  }
  expect(mode).toBe("bottom-right");
  // 下端は画面の下端から --toast-offset-bottom（8rem）。下端に固定された入力欄があれば、その上端から 1rem 上（#2103）。
  expect(Math.abs(offset - 8 * rem)).toBeLessThanOrEqual(1);
  expect(Math.abs(vh - Math.max(offset, clear + rem) - (rect.y + rect.height))).toBeLessThanOrEqual(1);
}

/**
 * 通知が出たときに覆う範囲（最も広い通知・本文と補足の 1 件）。通知の領域の位置（実際の CSS）から求める。
 * desktop は右下の角から左と上へ、375px は上端のバーから下へ広がる。
 */
export async function toastZone(page: Page): Promise<Box> {
  const { rect, rem, vw } = await readToastGeometry(page);
  const height = TOAST_ZONE_HEIGHT_REM * rem;
  if (vw < 768) return { x: rect.x, y: rect.y, width: rect.width, height };
  const right = rect.x + rect.width;
  const width = Math.min(TOAST_WIDTH_MAX_REM * rem, vw - 2 * rem);
  return { x: right - width, y: rect.y + rect.height - height, width, height };
}

/**
 * 主操作が通知の範囲と重ならない。2 つの状態で確かめる。
 * - 開いたまま（スクロールの前）: PageHeader のページの操作・FormActionBar・ObjectActionBar・チャットの入力欄と送信。
 * - すべてのスクロールの領域を末尾までスクロールした後: ページの末尾の操作（本文の最後のボタン。それ以上スクロール
 *   できないので画面の下端に来る）・チャットの入力欄と送信と、画面の下半分にある上の 3 種の操作。画面の上半分へ
 *   スクロールで動いた操作は、スクロールを戻せば通知から離れるので対象にしない（375px の通知は上端）。
 */
export async function expectMainActionsClearOfToast(page: Page) {
  const zone = await toastZone(page);
  const collect = (scrolledToEnd: boolean) =>
    page.evaluate((atEnd) => {
      const visible = (element: Element) => {
        const rect = element.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) return null;
        if (rect.bottom <= 0 || rect.top >= window.innerHeight || rect.right <= 0 || rect.left >= window.innerWidth) return null;
        const style = getComputedStyle(element);
        if (style.visibility === "hidden" || style.display === "none") return null;
        return rect;
      };
      const describe = (element: Element) =>
        (element.getAttribute("aria-label") || element.getAttribute("placeholder") || element.textContent || element.tagName)
          .trim()
          .slice(0, 40);
      const all = (selector: string) => Array.from(document.querySelectorAll(selector));
      const actions = all("[data-page-header-actions] :is(button, a[href]), [data-form-action-id], [data-entity-action-id]");
      const composer = all('[data-testid$="composer-region"] [data-field-action-row] :is(textarea, button)');
      const targets = new Set<Element>(composer);
      const inLowerHalf = (element: Element) => {
        const rect = visible(element);
        return rect !== null && rect.top + rect.height / 2 >= window.innerHeight / 2;
      };
      for (const element of actions) {
        if (visible(element) && (!atEnd || inLowerHalf(element))) targets.add(element);
      }
      if (atEnd) {
        // ページの末尾の操作: 本文（main）の中で DOM の最後の、見えているボタン（画面の下半分にあるとき。
        // 末尾にボタンの無いページでは、上の方の操作が最後のボタンになる）。
        const last = all("main button").filter((element) => visible(element)).at(-1);
        if (last && inLowerHalf(last)) targets.add(last);
      }
      return Array.from(targets).flatMap((element) => {
        const rect = visible(element);
        return rect ? [{ name: describe(element), box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } }] : [];
      });
    }, scrolledToEnd);
  const covered = (items: { name: string; box: Box }[]) => items.filter((item) => overlaps(item.box, zone)).map((item) => item.name);

  const before = covered(await collect(false));
  await page.evaluate(() => {
    for (const element of [document.scrollingElement, ...Array.from(document.querySelectorAll("*"))]) {
      if (!element) continue;
      if (element === document.scrollingElement || /(auto|scroll)/.test(getComputedStyle(element).overflowY)) {
        element.scrollTop = element.scrollHeight;
      }
    }
  });
  const after = covered(await collect(true));
  expect({ before, after }, "通知の範囲と重なる主操作").toEqual({ before: [], after: [] });
}
