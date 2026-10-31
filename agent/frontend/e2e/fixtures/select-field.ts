import { expect, type Locator } from "@playwright/test";

/**
 * 共有の SelectField（select-only combobox）で選択肢を選ぶ（#631。ネイティブの `<select>` の `selectOption` の代わり）。
 * - 文字列は選択肢の値（SelectField が選択肢とボタンに出す `data-value`）で選ぶ
 * - `{ label }` は選択肢の表示名（完全一致）で選ぶ
 * 一覧は body（モーダルの中ならそのモーダル）へ Portal で描かれるので、ボタンの `aria-controls` から一覧を引く。
 */
export async function chooseSelectFieldOption(combobox: Locator, option: string | { label: string }) {
  await expect(combobox).toBeEnabled();
  await combobox.click();
  const listboxId = await combobox.getAttribute("aria-controls");
  const listbox = combobox.page().locator(`[id="${listboxId}"]`);
  const target =
    typeof option === "string"
      ? listbox.locator(`[role="option"][data-value="${option}"]`)
      : listbox.getByRole("option", { name: option.label, exact: true });
  await target.click();
  await expect(listbox).toBeHidden();
  if (typeof option === "string") await expect(combobox).toHaveAttribute("data-value", option);
}

/** SelectField の選択中の値（ネイティブの `toHaveValue` の代わり）。 */
export async function expectSelectFieldValue(combobox: Locator, value: string) {
  await expect(combobox).toHaveAttribute("data-value", value);
}

/**
 * 一覧のツールバーの絞り込みを使う（#1933）。狭い置き場所（`filtersLayout="sheet"`）では「絞り込み」のシートを開いて
 * `action` に渡し、終わったら「N 件を表示」で閉じる。広い置き場所ではツールバーをそのまま渡す。
 */
export async function withListFilters(toolbar: Locator, action: (scope: Locator) => Promise<void>) {
  await expect(toolbar).toBeVisible();
  if ((await toolbar.getAttribute("data-list-toolbar-filters-layout")) !== "sheet") {
    await action(toolbar);
    return;
  }
  const sheet = toolbar.page().getByTestId(`${await toolbar.getAttribute("data-testid")}-filters-sheet`);
  await toolbar.getByRole("button", { name: /^絞り込み/ }).click();
  await expect(sheet).toBeVisible();
  await action(sheet);
  await sheet.getByRole("button", { name: /件を表示$|^結果を表示$/ }).click();
  await expect(sheet).toBeHidden();
}
