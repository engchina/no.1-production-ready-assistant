import { expect, type Page } from "./test";

// 一覧 + 詳細（共有 ListDetailLayout）の 2 ペインの一覧の列の高さ（#2330）。
// 列の高さは「本文（AppShell の main）の見える高さ − 列の上端 − 下の余白」で、詳細が短ければ本文に縦のスクロールを作らず、
// 詳細が長く本文をスクロールしても高さを変えず、上端に貼り付く PageHeader の下に貼り付く。

export interface ListDetailFit {
  /** 本文（#pr-main）の縦にスクロールできる量（scrollHeight − clientHeight）。 */
  mainOverflow: number;
  /** 文書の縦にスクロールできる量（document.scrollingElement.scrollHeight − innerHeight）。 */
  documentOverflow: number;
  /** 本文の見える下端（画面の座標）。 */
  viewportBottom: number;
  /** 上端に貼り付く PageHeader の下端（画面の座標）。 */
  headerBottom: number;
  columnTop: number;
  columnBottom: number;
  columnHeight: number;
  /** 一覧の列の下の帯（件数・「さらに読み込む」・ページ送り）。無ければ null。 */
  footerTop: number | null;
  footerBottom: number | null;
  /** 一覧の行が列の中でスクロールするか。 */
  bodyScrolls: boolean;
  scrollTop: number;
}

export async function measureListDetailFit(page: Page, id: string): Promise<ListDetailFit> {
  return page.evaluate((layoutId) => {
    const main = document.getElementById("pr-main")!;
    const column = document.querySelector<HTMLElement>(`[data-testid="list-detail-${layoutId}-list"]`)!;
    const body = column.querySelector<HTMLElement>("[data-list-detail-body]")!;
    const footer = column.querySelector<HTMLElement>("[data-list-detail-footer]");
    const header = document.querySelector<HTMLElement>("[data-page-header]");
    const columnRect = column.getBoundingClientRect();
    const footerRect = footer && footer.getClientRects().length > 0 ? footer.getBoundingClientRect() : null;
    const scroller = document.scrollingElement ?? document.documentElement;
    // 一覧の行の表（DataTable）は自分のスクロール領域を持つので、列の本体の中のいちばん深いスクロールを見る。
    const scrollables = [body, ...Array.from(body.querySelectorAll<HTMLElement>("*"))].filter((node) => {
      const overflowY = getComputedStyle(node).overflowY;
      return (overflowY === "auto" || overflowY === "scroll") && node.scrollHeight > node.clientHeight + 1;
    });
    return {
      mainOverflow: main.scrollHeight - main.clientHeight,
      documentOverflow: scroller.scrollHeight - window.innerHeight,
      viewportBottom: main.getBoundingClientRect().top + main.clientTop + main.clientHeight,
      headerBottom: header ? header.getBoundingClientRect().bottom : 0,
      columnTop: columnRect.top,
      columnBottom: columnRect.bottom,
      columnHeight: columnRect.height,
      footerTop: footerRect ? footerRect.top : null,
      footerBottom: footerRect ? footerRect.bottom : null,
      bodyScrolls: scrollables.length > 0,
      scrollTop: main.scrollTop,
    };
  }, id);
}

/**
 * 詳細が短いとき: 本文と文書に縦のスクロールが無く、一覧の列の下端と下の帯が画面の中に見える。
 * 行が列に収まらない（列の中をスクロールする）ときは、列の下端が本文の下の余白（PageBody の 1.5rem）の分だけ上にある
 * （画面の高さいっぱいに使う）。
 */
export async function expectListColumnFitsViewport(page: Page, id: string, options: { requireFooter?: boolean } = {}) {
  await expect.poll(async () => (await measureListDetailFit(page, id)).mainOverflow).toBeLessThanOrEqual(0);
  const fit = await measureListDetailFit(page, id);
  expect(fit.documentOverflow).toBeLessThanOrEqual(0);
  expect(fit.columnBottom).toBeLessThanOrEqual(fit.viewportBottom + 0.5);
  // 下の帯は中身が無ければ出さない（1 ページの Pagination。#2283）。出すなら画面の中に見える。
  if (options.requireFooter !== false) expect(fit.footerBottom).not.toBeNull();
  if (fit.footerBottom !== null) {
    expect(fit.footerTop!).toBeGreaterThan(fit.columnTop);
    expect(fit.footerBottom).toBeLessThanOrEqual(fit.viewportBottom + 0.5);
  }
  if (fit.bodyScrolls) {
    const bottomGap = fit.viewportBottom - fit.columnBottom;
    const pageBodyPadding = await page.evaluate(() => Number.parseFloat(getComputedStyle(document.documentElement).fontSize) * 1.5);
    expect(bottomGap).toBeGreaterThanOrEqual(pageBodyPadding - 1.5);
    expect(bottomGap).toBeLessThanOrEqual(pageBodyPadding + 1.5);
  }
  return fit;
}

/**
 * 詳細が長いとき: 本文はスクロールし、一覧の列は高さを変えず、上端に貼り付く PageHeader の下（1rem 空けて）に貼り付き、
 * 下端と下の帯は画面の中に残る。
 */
export async function expectListColumnStaysFitWhileScrolling(page: Page, id: string) {
  const before = await expectListColumnFitsViewportTop(page, id);
  expect(before.mainOverflow).toBeGreaterThan(0);
  await page.evaluate(() => {
    const main = document.getElementById("pr-main")!;
    main.scrollTop = main.scrollHeight;
  });
  await expect.poll(async () => (await measureListDetailFit(page, id)).scrollTop).toBeGreaterThan(0);
  const after = await measureListDetailFit(page, id);
  const fontSize = await page.evaluate(() => Number.parseFloat(getComputedStyle(document.documentElement).fontSize));
  expect(Math.abs(after.columnHeight - before.columnHeight)).toBeLessThanOrEqual(0.5);
  expect(Math.abs(after.columnTop - (after.headerBottom + fontSize))).toBeLessThanOrEqual(1);
  expect(after.columnBottom).toBeLessThanOrEqual(after.viewportBottom + 0.5);
  expect(after.footerBottom!).toBeLessThanOrEqual(after.viewportBottom + 0.5);
  return { before, after };
}

/** 本文の先頭での一覧の列（下端と下の帯が画面の中）。 */
async function expectListColumnFitsViewportTop(page: Page, id: string) {
  const fit = await measureListDetailFit(page, id);
  expect(fit.scrollTop).toBe(0);
  expect(fit.columnBottom).toBeLessThanOrEqual(fit.viewportBottom + 0.5);
  expect(fit.footerBottom).not.toBeNull();
  expect(fit.footerBottom!).toBeLessThanOrEqual(fit.viewportBottom + 0.5);
  return fit;
}
