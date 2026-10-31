import type { Locator } from "@playwright/test";

/**
 * 共有 DataTable の visibleRows が決めるスクロール領域の高さ（border-box）を実測から求める（#530）。
 * 表頭 + 先頭 N 行の下端 + 枠（border・横スクロールバー）。2 行セルで行高が 3.5rem を超えても N 行ちょうどになる。
 */
export async function measuredVisibleRowsHeight(scrollRegion: Locator, rowSelector: string, rows: number) {
  return scrollRegion.evaluate(
    (node, { selector, limit }) => {
      const element = node as HTMLElement;
      const table = element.querySelector("table");
      const limitRow = element.querySelectorAll(selector)[limit - 1];
      if (!table || !limitRow) throw new Error(`DataTable の ${limit} 行目がありません`);
      return (
        limitRow.getBoundingClientRect().bottom -
        table.getBoundingClientRect().top +
        element.offsetHeight -
        element.clientHeight
      );
    },
    { selector: rowSelector, limit: rows }
  );
}
