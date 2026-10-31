import { expect, type Locator, type Page } from "./test";
import { expectedControlHeight } from "./control-height";

/** 欄の幅の段（rem。共通の `FIELD_WIDTH_REM`）。ルートは 14px。 */
const FIELD_WIDTH_PX = { sm: 12 * 14, md: 20 * 14, lg: 28 * 14 } as const;
/**
 * ツールバーの幅が 28rem（`LIST_TOOLBAR_NARROW_REM`）未満では、検索欄も絞り込みの欄も全幅になる。
 * 欄の幅の段は画面ではなくツールバーの幅（container）で切り替わる（#1933）。
 */
const NARROW_TOOLBAR_PX = 28 * 14;
const FILTERS_SHEET_NAME = "絞り込み";

function listToolbarRoot(toolbar: Locator) {
  return toolbar.locator("xpath=descendant-or-self::*[@data-list-toolbar]").first();
}

/**
 * 一覧のツールバーの絞り込みを操作できる場所を返す（#1933）。狭い置き場所（28rem 未満）で `filtersLayout="sheet"` の
 * ツールバーは「絞り込み（N）」を押してシートを開き、その dialog を返す。それ以外はツールバーをそのまま返す。
 * 開いたシートは `closeListToolbarFilters` で閉じる。
 */
export async function openListToolbarFilters(toolbar: Locator): Promise<Locator> {
  const root = listToolbarRoot(toolbar);
  await expect(root).toBeVisible();
  if ((await root.getAttribute("data-list-toolbar-filters-layout")) !== "sheet") return toolbar;
  await root.locator("[data-list-toolbar-filters-trigger] button").first().click();
  const sheet = toolbar.page().getByRole("dialog", { name: FILTERS_SHEET_NAME, exact: true });
  await expect(sheet).toBeVisible();
  return sheet;
}

/** `openListToolbarFilters` で開いたシートを末尾の「N 件を表示」で閉じる（開いていなければ何もしない）。 */
export async function closeListToolbarFilters(page: Page) {
  const sheet = page.getByRole("dialog", { name: FILTERS_SHEET_NAME, exact: true });
  if (!(await sheet.isVisible())) return;
  await sheet.getByRole("button", { name: /件を表示$|^結果を表示$/ }).click();
  await expect(sheet).toBeHidden();
}

/** ツールバーが狭い置き場所でシートに絞り込みをまとめているか。 */
export async function listToolbarUsesSheet(toolbar: Locator) {
  const root = listToolbarRoot(toolbar);
  await expect(root).toBeVisible();
  return (await root.getAttribute("data-list-toolbar-filters-layout")) === "sheet";
}

/**
 * DB オブジェクトの一覧のツールバー（共通の `ListToolbar`。#1891）の検索・所有者・種類の並びを確かめる。
 *
 * - 検索欄は lg（28rem）、所有者は md（20rem）、種類は sm（12rem）。置き場所（ツールバー）より広い段は置き場所の幅。
 *   ツールバーが 28rem 未満なら 3 つとも全幅（狭い分割のペインでも検索欄と所有者がそろう。#1933）。
 * - 絞り込みのシートの中の欄（狭い置き場所の `filtersLayout="sheet"`）は、シートの幅いっぱい。
 * - 並びは DOM の順（検索 → 所有者 → 種類）。次の欄は同じ行の右か、次の行の左端（検索欄の左端にそろう）。
 * - 3 つとも同じ高さ（md 36px。タッチ端末は 44px）。
 */
export async function expectDbObjectToolbarLayout(search: Locator, owner: Locator, kind?: Locator) {
  const fields = kind ? [search, owner, kind] : [search, owner];
  const steps = kind ? [FIELD_WIDTH_PX.lg, FIELD_WIDTH_PX.md, FIELD_WIDTH_PX.sm] : [FIELD_WIDTH_PX.lg, FIELD_WIDTH_PX.md];
  const page = search.page();
  const minHeight = await expectedControlHeight(page);
  await expect
    .poll(async () => {
      const measured = await Promise.all(
        fields.map((field) =>
          field.evaluate((node) => {
            // 入力欄は先頭のアイコンと消去のボタンを重ねる relative の箱が欄の外形（共通の layout-checks と同じ）。
            const box =
              node instanceof HTMLInputElement && node.parentElement?.classList.contains("relative")
                ? node.parentElement
                : node;
            const toolbar = node.closest("[data-list-toolbar-sheet-filters]") ?? node.closest("[data-list-toolbar]");
            const rect = box.getBoundingClientRect();
            return {
              x: rect.left,
              y: rect.top,
              width: rect.width,
              height: rect.height,
              toolbarWidth: toolbar?.getBoundingClientRect().width ?? 0,
            };
          })
        )
      );
      const problems: string[] = [];
      measured.forEach((box, index) => {
        const toolbarWidth = box.toolbarWidth;
        if (toolbarWidth <= 0) problems.push(`${index}: ListToolbar の中にない`);
        if (box.height < minHeight - 1) problems.push(`${index}: 高さ ${box.height} < ${minHeight}`);
        if (Math.abs(box.height - measured[0].height) > 2) problems.push(`${index}: 高さが揃わない`);
        const fullWidth = toolbarWidth < NARROW_TOOLBAR_PX;
        const expected = fullWidth ? toolbarWidth : Math.min(steps[index], toolbarWidth);
        if (Math.abs(box.width - expected) > 2) problems.push(`${index}: 幅 ${box.width} != ${expected}`);
        if (index === 0) return;
        const previous = measured[index - 1];
        // シートの中の欄は検索欄と別の場所（並びはシートの中だけで比べる）。
        if (previous.toolbarWidth !== box.toolbarWidth) return;
        const sameRow = Math.abs(box.y - previous.y) <= 2;
        if (sameRow) {
          if (box.x < previous.x + previous.width - 1) problems.push(`${index}: 前の欄に重なる`);
        } else {
          if (box.y < previous.y + previous.height - 1) problems.push(`${index}: 前の欄より上`);
          const first = measured.find((other) => other.toolbarWidth === box.toolbarWidth)!;
          if (Math.abs(box.x - first.x) > 2) problems.push(`${index}: 折り返した行の左端が揃わない`);
        }
      });
      return problems;
    })
    .toEqual([]);
}

/**
 * 一覧のツールバーの絞り込みを操作して戻る（#1933）。狭い置き場所のシートなら開いて `action` を実行し、閉じてから返る。
 */
export async function withListToolbarFilters<T>(toolbar: Locator, action: (filters: Locator) => Promise<T>): Promise<T> {
  const filters = await openListToolbarFilters(toolbar);
  const result = await action(filters);
  await closeListToolbarFilters(toolbar.page());
  return result;
}
