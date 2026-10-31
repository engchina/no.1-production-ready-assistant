import { expect, type Locator, type Page } from "./fixtures/test";

export const itemCitationText = "資料により確認できた事項\n\n・合成の説明 A。\n根拠：document-a.pdf p.1\n・合成の説明 B。\n根拠：document-a.pdf p.1\n・合成の説明 C。\n根拠：document-a.pdf p.1\n・合成の説明 D。\n根拠：document-a.pdf p.1";
export const itemCitationChunks = ["A", "B"].map((letter, index) => ({
  document_id: "document-a",
  chunk_id: `chunk-${letter.toLowerCase()}`,
  text: `合成の原文 ${letter}。`,
  score: 0.8,
  rerank_score: 0.9,
  file_name: "document-a.pdf",
  category_name: null,
  metadata: {
    page_start: 1,
    answer_citation_lines: index === 0 ? [1, 3, 4] : [2, 3],
    answer_citation_candidate_lines: index === 1 ? [4] : [],
    answer_citation_provenance: "child",
    evidence_model_used: true,
    evidence_answer_use: "confirmed",
  },
}));

export async function expectItemCitationSources(page: Page, answer: Locator) {
  const rows = answer.getByRole("listitem");
  await expect(rows).toHaveCount(4);
  await expect(rows.nth(0).getByRole("button")).toHaveCount(1);
  await expect(rows.nth(1).getByRole("button")).toHaveCount(1);
  await expect(rows.nth(2).getByRole("button")).toHaveCount(2);
  for (const [item, target, letter] of [[0, 0, "A"], [1, 0, "B"], [2, 0, "A"], [2, 1, "B"]] as const) {
    const button = rows.nth(item).getByRole("button").nth(target);
    await button.click();
    const sheet = page.getByRole("dialog", { name: "回答の根拠", exact: true });
    await expect(sheet).toBeVisible();
    const summary = sheet.getByTestId("evidence-source-summary");
    await expect(summary.getByTestId("evidence-file-name")).toHaveText("document-a.pdf");
    const toggle = summary.getByTestId("evidence-excerpt-toggle");
    if (await toggle.isVisible()) await toggle.click();
    await expect(summary.getByText(`合成の原文 ${letter}。`, { exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
    await expect(button).toBeFocused();
  }
  const mixed = rows.nth(3);
  await expect(mixed.getByRole("button")).toHaveCount(2);
  const candidate = mixed.getByRole("button", { name: /位置未確定の候補/ });
  await candidate.click();
  const sheet = page.getByRole("dialog", { name: "回答の根拠", exact: true });
  await expect(sheet.getByText("この根拠の行の位置は確定していません", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(candidate).toBeFocused();
}
