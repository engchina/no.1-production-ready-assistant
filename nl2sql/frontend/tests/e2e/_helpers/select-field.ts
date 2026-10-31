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
