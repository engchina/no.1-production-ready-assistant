import { expect, test, type Page } from "./fixtures/test";
import { expectNoPageOverflow, mockLocalAuth } from "./_helpers";
import { t } from "../src/lib/i18n";

// 言語資源（#2181）。項目の定義と組み込みの値は backend の一部（画面の確認に足りる分）。
const FIELDS = [
  { section: "numbers", key: "article_units", kind: "words", choices: [], ingest: true, max_chars: 40 },
  { section: "numbers", key: "kanji_multipliers", min_value: 1, kind: "char_values", choices: [], ingest: true, max_chars: 40 },
  { section: "symbols", key: "normalization", kind: "choice", choices: ["NFKC", "NFC", "none"], ingest: true, max_chars: 40 },
  { section: "symbols", key: "bracket_pairs", kind: "pairs", choices: [], ingest: true, max_chars: 40 },
  { section: "references", key: "cue_words", kind: "words", choices: [], ingest: true, max_chars: 40 },
  { section: "titles", key: "edition_words", kind: "words", choices: [], ingest: true, max_chars: 40 },
  {
    section: "english",
    key: "en_section_words",
    kind: "enum_map",
    choices: ["chapter", "section", "article", "appendix"],
    ingest: true,
    max_chars: 40,
  },
];
const BUILTIN = {
  numbers: { article_units: ["条"], kanji_multipliers: ["十=10", "百=100"] },
  symbols: { normalization: "NFKC", bracket_pairs: ["()", "【】"] },
  references: { cue_words: ["を参照", "による"] },
  titles: { edition_words: ["改訂版"] },
  english: { en_section_words: ["section=section"] },
};

function envelope(data: unknown) {
  return { data, error_messages: [], warning_messages: [] };
}

async function mockLanguagePack(page: Page, builtin: Record<string, Record<string, unknown>> = BUILTIN, fields = FIELDS) {
  const saved: { overrides: Record<string, Record<string, unknown>> | null } = { overrides: null };
  const previews: Record<string, unknown>[] = [];
  const restored: string[] = [];
  const publications: Record<string, unknown>[] = [];
  const release = (version: number, effective = structuredClone(builtin)) => ({ id: `v${version}`, version, effective, hash: `hash${version}`, ingest_hash: `ingest${version}`, published_at: "2026-10-17T09:00:00Z", published_by: "管理者", previous_id: null, restored_from: null, change_note: "合成の設定変更" });
  let current = release(2);
  let draftEffective = structuredClone(builtin);
  let revision = 1;
  const state = () => ({ current, draft_revision: revision, draft_effective: draftEffective });
  await page.route("**/api/settings/language-pack/draft", async route => {
    if (route.request().method() === "PUT") {
      expect(route.request().headers()["content-type"]).toBe("application/json");
      const payload = route.request().postDataJSON();
      expect(payload.expected_revision).toBe(revision);
      saved.overrides = payload.overrides;
      draftEffective = structuredClone(builtin);
      for (const [section, values] of Object.entries(saved.overrides!)) Object.assign(draftEffective[section], values);
      revision += 1;
    }
    await route.fulfill({ json: envelope(state()) });
  });
  await page.route("**/api/settings/language-pack/draft/comparison?*", async route => {
    const url = new URL(route.request().url()); expect(url.searchParams.get("expected_revision")).toBe(String(revision)); expect(url.searchParams.get("expected_current_id")).toBe(current.id);
    const differences = Object.entries(draftEffective).flatMap(([section, fields]) => Object.entries(fields).flatMap(([key, after]) => {
      const before = current.effective[section]?.[key];
      return JSON.stringify(before) === JSON.stringify(after) ? [] : [{ section, key, before, after, ingest: true }];
    }));
    await route.fulfill({ json: envelope({ before_id: current.id, after_id: `draft:${revision}`, differences, ingest_changed: differences.length > 0, impact_requires_review: false, affected_stages: differences.length ? ["EXTRACT", "CHUNK", "INDEX"] : [] }) });
  });
  await page.route("**/api/settings/language-pack/releases?*", async route => {
    await route.fulfill({ json: envelope({ releases: [current, release(1, { ...builtin, numbers: { ...builtin.numbers, article_units: ["条", "款"] } })], total: 2 }) });
  });
  await page.route("**/api/settings/language-pack/releases/v*", async route => {
    const id = route.request().url().split("/releases/")[1];
    const version = Number(id.slice(1));
    await route.fulfill({ json: envelope(version === current.version ? current : release(version, { ...builtin, numbers: { ...builtin.numbers, article_units: ["条", "款"] } })) });
  });
  await page.route("**/api/settings/language-pack/releases/compare?*", async route => route.fulfill({ json: envelope({ before_id: "v1", after_id: current.id, differences: [{ section: "numbers", key: "article_units", before: ["条", "款"], after: ["条"], ingest: true }], ingest_changed: true, impact_requires_review: true, affected_stages: [] }) }));
  await page.route("**/api/settings/language-pack/releases/*/restore", async route => {
    restored.push(route.request().url().split("/releases/")[1].split("/")[0]);
    const body = route.request().postDataJSON();
    expect(body.expected_current_id).toBe(current.id);
    expect(body.expected_revision).toBe(revision);
    draftEffective = { ...builtin, numbers: { ...builtin.numbers, article_units: ["条", "款"] } };
    current = release(3, draftEffective); revision += 1;
    await route.fulfill({ json: envelope(state()) });
  });
  await page.route("**/api/settings/language-pack/publish", async route => {
    expect(route.request().headers()["content-type"]).toBe("application/json");
    const body = route.request().postDataJSON(); publications.push(body);
    expect(body.expected_current_id).toBe(current.id); expect(body.expected_revision).toBe(revision);
    current = release(3, draftEffective); revision += 1;
    await route.fulfill({ json: envelope(state()) });
  });
  await page.route("**/api/settings/language-pack/preview", async route => {
    previews.push(route.request().postDataJSON());
    const result = (headingKeys: string[]) => ({ references: [{ label: "第二款", kind: "article", key: "2", document_title: null }], title_keys: ["第二款届出"], heading_keys: headingKeys });
    await route.fulfill({ json: envelope({ saved: result([]), draft: result(["article:2"]) }) });
  });
  await page.route("**/api/settings/language-pack", async route => {
    expect(route.request().method()).toBe("GET");
    await route.fulfill({ json: envelope({ fields, builtin, overrides: {}, effective: current.effective, hash: current.hash, current_release: current, outdated_documents: 3, unrecorded_documents: 2 }) });
  });
  return { saved, previews, restored, publications, readState: state };
}

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`言語資源は節のタブで項目を編集し、組み込みと違う項目だけを保存する（#2181, ${viewport.name}）`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const { saved } = await mockLanguagePack(page);

    await page.goto("/settings/language-pack");

    await expect(page.getByRole("heading", { name: "言語資源", level: 1 })).toBeVisible();
    // 今の言語資源で分割していない文書の数を案内する（#2181）。
    await expect(page.getByText("取込設定に差分のある文書 3 件")).toBeVisible();
    const tabs = page.getByRole("tablist", { name: "言語資源", exact: true });
    await expect(tabs.getByRole("tab")).toHaveText([
      "番号の書き方",
      "記号と文字",
      "参照の書き方",
      "文書名の照合",
      "英語の参照の書き方",
    ]);
    const units = page.getByLabel("条の単位", { exact: true });
    await expect(units).toHaveValue("条");
    await units.fill("条\n款");
    await expect(page.getByText("既定から変更")).toBeVisible();
    await page.getByRole("button", { name: "既定に戻す" }).click();
    await expect(units).toHaveValue("条");
    await units.fill("条\n款");

    // 別の節の形の誤り: 保存を押すとその節を開き、欄の直下に出してその欄へ移す。
    await tabs.getByRole("tab", { name: "記号と文字" }).click();
    const pairs = page.getByLabel("括弧書きの括弧", { exact: true });
    await pairs.fill("()\n(");
    await tabs.getByRole("tab", { name: "番号の書き方" }).click();
    await page.getByRole("button", { name: "草稿を保存" }).click();
    await expect(page.getByText("開きと閉じの 2 文字で書いてください（例: 「」）。")).toBeVisible();
    await expect(pairs).toBeFocused();
    expect(saved.overrides).toBeNull();

    await pairs.fill("()\n【】");
    await page.getByRole("combobox", { name: "文字の正規化" }).click();
    await page.getByRole("option", { name: "NFC（全角と半角をそろえない）" }).click();
    await page.getByRole("button", { name: "草稿を保存" }).click();
    await expect(page.getByText("草稿を保存しました。")).toBeVisible();
    expect(saved.overrides).toEqual({
      numbers: { article_units: ["条", "款"] },
      symbols: { normalization: "NFC" },
    });
    await expect(page.getByRole("button", { name: "草稿を保存" })).toBeDisabled();
    await expectNoPageOverflow(page);
  });
}

test("言語資源の試すは、保存した値と編集中の値で例の文を読み比べる（保存しない。#2181）", async ({ page }) => {
  const { saved, previews } = await mockLanguagePack(page);
  await page.goto("/settings/language-pack");

  await page.getByLabel("条の単位", { exact: true }).fill("条\n款");
  await page.getByLabel("例の文", { exact: true }).fill("第二款 届出\n第二款を参照");
  await page.getByRole("button", { name: "試す" }).click();

  const result = page.getByTestId("language-preview-result");
  await expect(result.getByRole("region", { name: "編集中の言語資源" })).toContainText("article:2");
  await expect(result.getByRole("region", { name: "公開中の言語資源" })).toContainText("なし");
  expect(previews).toEqual([
    { text: "第二款 届出\n第二款を参照", overrides: { numbers: { article_units: ["条", "款"] } } },
  ]);
  expect(saved.overrides).toBeNull();
});

test("公開版は読み取り専用で差分を確認して再公開し、編集中は回退できない", { tag: "@responsive" }, async ({ page }) => {
  const { restored } = await mockLanguagePack(page);
  await page.goto("/settings/language-pack");
  const history = page.getByRole("table", { name: "公開版の履歴" });
  await expect(history.getByRole("row")).toHaveCount(3);
  await history.getByRole("button", { name: "v1", exact: true }).click();
  const details = page.getByRole("region", { name: "公開版の内容（読み取り専用）" });
  await expect(details.getByLabel("条の単位", { exact: true })).toBeDisabled();
  await expect(details.getByLabel("条の単位", { exact: true })).toHaveValue("条\n款");
  const restore = details.getByRole("button", { name: "この内容を再公開する" });
  const units = page.locator("#language-numbers-article-units");
  await units.fill("条\n款"); await expect(restore).toBeDisabled();
  await page.getByRole("button", { name: "変更を破棄", exact: true }).click();
  await expect(restore).toBeEnabled(); await restore.click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("自動では再処理しません");
  await dialog.getByRole("button", { name: "この内容を再公開する" }).click();
  await expect.poll(() => restored).toEqual(["v1"]);
  await expect(units).toHaveValue("条\n款");
  await expectNoPageOverflow(page);
});

test("草稿保存は公開版を変えず、差分確認後の公開だけが現在版を更新する", { tag: "@responsive" }, async ({ page }) => {
  const { publications } = await mockLanguagePack(page);
  await page.goto("/settings/language-pack");
  await page.locator("#language-numbers-article-units").fill("条\n款");
  await page.getByRole("button", { name: "草稿を保存", exact: true }).click();
  await expect(page.getByText("現在使用中 v2")).toBeVisible();
  expect(publications).toEqual([]);
  const publish = page.getByRole("button", { name: "公開する", exact: true });
  await expect(publish).toBeDisabled();
  await page.getByRole("button", { name: "差分と影響を確認する" }).click();
  await expect(page.getByRole("table", { name: "設定の差分" })).toBeVisible();
  await page.locator("#language-numbers-article-units").fill("条\n款\n項");
  await page.getByRole("button", { name: "草稿を保存", exact: true }).click();
  await expect(publish).toBeDisabled();
  await page.getByRole("button", { name: "差分と影響を確認する" }).click();
  await publish.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "公開する", exact: true }).click();
  await expect(page.getByText("現在使用中 v3")).toBeVisible();
  expect(publications).toEqual([{ expected_revision: 3, expected_current_id: "v2", change_note: "" }]);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("language-release-published.png"), fullPage: true });
});

test("言語資源は使い道の組と節の 2 段のタブで開き、項目を名前で探せる（#2181）", { tag: "@responsive" }, async ({ page }) => {
  const fields = [
    ...FIELDS,
    { section: "search_terms", key: "stopwords", kind: "words", choices: [], ingest: false, max_chars: 40 },
    { section: "answer_labels", key: "citation_prefix", kind: "text", choices: [], ingest: false, max_chars: 200 },
    { section: "answer_labels", key: "missing_request_template", kind: "text", choices: [], ingest: false, max_chars: 200 },
    { section: "answer_labels", key: "guide_acquisition_incomplete_template", kind: "text", choices: [], ingest: false, max_chars: 200 },
  ];
  const missingRequest = "『{text}』については、根拠付きの説明を十分に示せていません。";
  const acquisitionNotice = "必須の参照を確認できていません（手順: {steps}）。";
  const builtin = { ...BUILTIN, search_terms: { stopwords: ["こと"] }, answer_labels: { citation_prefix: "根拠：", missing_request_template: missingRequest, guide_acquisition_incomplete_template: acquisitionNotice } };
  await mockLanguagePack(page, builtin);
  await page.route("**/api/settings/language-pack", (route) =>
    route.fulfill({ json: envelope({ fields, builtin, overrides: {}, effective: builtin, hash: "a" }) }),
  );
  await page.goto("/settings/language-pack");

  const groups = page.getByRole("tablist", { name: "言語資源の使い道" });
  await expect(groups.getByRole("tab")).toHaveText(["文書を読む", "質問を読む", "回答を確かめて表す"]);
  await groups.getByRole("tab", { name: "質問を読む" }).click();
  await expect(groups.getByRole("tab", { name: "質問を読む" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tablist", { name: "言語資源", exact: true }).getByRole("tab")).toHaveText(["全文検索の質問の語"]);
  await expect(page.getByLabel("止め語", { exact: true })).toHaveValue("こと");

  await page.getByRole("searchbox", { name: "項目を探す" }).fill("出典");
  const results = page.getByTestId("language-search-results");
  await expect(results.getByRole("heading", { name: "回答の表示" })).toBeVisible();
  await expect(page.getByLabel("出典の前置き", { exact: true })).toHaveValue("根拠：");
  await expect(page.getByRole("tablist", { name: "言語資源の使い道" })).toHaveCount(0);
  await page.getByRole("searchbox", { name: "項目を探す" }).fill("要求の説明が不足");
  await expect(page.getByLabel("要求の説明が不足したときの案内", { exact: true })).toHaveValue(missingRequest);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("missing-request-template.png"), fullPage: true });
  const acquisitionLabel = t("settings.language.field.guide_acquisition_incomplete_template");
  await page.getByRole("searchbox", { name: "項目を探す" }).fill(acquisitionLabel);
  const acquisition = page.getByLabel(acquisitionLabel, { exact: true });
  await expect(acquisition).toHaveValue(acquisitionNotice);
  await expect(acquisition).toHaveAccessibleDescription(t("settings.language.field.guide_acquisition_incomplete_template.help"));
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("required-acquisition-template.png"), fullPage: true });
  await page.getByRole("searchbox", { name: "項目を探す" }).fill("見つからない語");
  await expect(page.getByText("検索語に当たる項目はありません。")).toBeVisible();
  await expectNoPageOverflow(page);
});


test("言語資源のゼロ倍率は欄で拒否し、正しい倍率だけを保存する", { tag: "@responsive" }, async ({ page }) => {
  const { saved } = await mockLanguagePack(page);
  await page.goto("/settings/language-pack");
  const field = page.getByLabel("漢数字の位取りの字と倍率", { exact: true });
  await field.fill("十=0");
  await page.getByRole("button", { name: "草稿を保存", exact: true }).click();
  await expect(page.getByText("数は 1 以上で書いてください。", { exact: true })).toBeVisible();
  await expect(field).toBeFocused();
  expect(saved.overrides).toBeNull();
  await field.fill("十=10\n百=100\n千=1000");
  await page.getByRole("button", { name: "草稿を保存", exact: true }).click();
  await expect.poll(() => saved.overrides).toEqual({ numbers: { kanji_multipliers: ["十=10", "百=100", "千=1000"] } });
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("numeric-mapping.png"), fullPage: true });
});


test("数量の混在表記の語は質問用の言語資源で確認できる", async ({ page }) => {
  const fields = [...FIELDS, { section: "question_cues", key: "mixed_number_notation_cues", kind: "words", choices: [], ingest: false, max_chars: 40 }];
  const builtin = { ...BUILTIN, question_cues: { mixed_number_notation_cues: ["混在", "組み合"] } };
  await mockLanguagePack(page, builtin);
  await page.route("**/api/settings/language-pack", route => route.fulfill({ json: envelope({ fields, builtin, overrides: {}, effective: builtin, hash: "a" }) }));
  await page.goto("/settings/language-pack");
  await page.getByRole("tablist", { name: "言語資源の使い道" }).getByRole("tab", { name: "質問を読む" }).click();
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByLabel("数字と位取りを混ぜる指定の語", { exact: true })).toHaveValue("混在\n組み合");
    await expectNoPageOverflow(page);
    await page.screenshot({ path: test.info().outputPath(`mixed-number-${width}.png`) });
  }
});


test("公開が競合したら現在版を成功表示せず再確認する", { tag: "@responsive" }, async ({ page }) => {
  await mockLanguagePack(page);
  await page.route("**/api/settings/language-pack/publish", route => route.fulfill({ status: 409, json: { data: null, error_messages: ["公開版が更新されました。再読み込みしてください。"], warning_messages: [] } }));
  await page.goto("/settings/language-pack");
  await page.locator("#language-numbers-article-units").fill("条\n款");
  await page.getByRole("button", { name: "草稿を保存", exact: true }).click();
  await page.getByRole("button", { name: "差分と影響を確認する" }).click();
  await page.getByRole("button", { name: "公開する", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "公開する", exact: true }).click();
  await expect(page.getByText("公開版が更新されました。再読み込みしてください。", { exact: true })).toBeVisible();
  await expect(page.getByText("現在使用中 v2")).toBeVisible();
  await expectNoPageOverflow(page);
});

test("公開版の履歴はサーバーのページ条件で読み、表示件数の変更時に先頭へ戻る", { tag: "@responsive" }, async ({ page }) => {
  await mockLanguagePack(page);
  const requests: string[] = [];
  await page.route("**/api/settings/language-pack/releases?*", async route => {
    const url = new URL(route.request().url()); requests.push(url.search);
    const offset = Number(url.searchParams.get("offset")); const limit = Number(url.searchParams.get("limit"));
    const releases = Array.from({ length: Math.min(limit, 35 - offset) }, (_, index) => ({ id: `v${35-offset-index}`, version: 35-offset-index, effective: BUILTIN, hash: "a", ingest_hash: "a", published_at: "2026-10-17T09:00:00Z", published_by: null, previous_id: null, restored_from: null, change_note: "合成の変更" }));
    await route.fulfill({ json: envelope({ releases, total: 35 }) });
  });
  await page.goto("/settings/language-pack");
  await page.getByRole("button", { name: "次へ", exact: true }).click();
  await expect.poll(() => requests.at(-1)).toBe("?offset=25&limit=25");
  await page.getByRole("combobox", { name: "表示件数", exact: true }).click();
  await page.getByRole("option", { name: "50 件", exact: true }).click();
  await expect.poll(() => requests.at(-1)).toBe("?offset=0&limit=50");
  await expectNoPageOverflow(page);
});


test("差分と版未記録の件数から現在版に固定した対象一覧へ移る", { tag: "@responsive" }, async ({ page }) => {
  await mockLanguagePack(page); await page.goto("/settings/language-pack");
  const links = page.getByRole("link", { name: "対象文書を確認する" });
  await expect(links.nth(0)).toHaveAttribute("href", "/file-list?language_pack_relation=different&current_release_id=v2");
  await expect(links.nth(1)).toHaveAttribute("href", "/file-list?language_pack_relation=unrecorded&current_release_id=v2");
  await expect(page.getByText("使用版が未記録の文書 2 件")).toBeVisible();
  await expectNoPageOverflow(page);
});


test("草稿の競合後は変更の破棄を確認して最新の草稿を読み、古い内容を新revisionで再送しない", { tag: "@responsive" }, async ({ page }) => {
  const { readState } = await mockLanguagePack(page);
  let conflicted = false;
  await page.route("**/api/settings/language-pack/draft", async route => {
    if (route.request().method() === "PUT") { conflicted = true; return route.fulfill({ status: 409, json: { data: null, error_messages: ["別の利用者が草稿を更新しました。"], warning_messages: [] } }); }
    if (!conflicted) return route.fallback();
    await route.fulfill({ json: envelope({ ...readState(), draft_revision: 2, draft_effective: { ...BUILTIN, numbers: { ...BUILTIN.numbers, article_units: ["条", "項"] } } }) });
  });
  await page.goto("/settings/language-pack");
  const units = page.locator("#language-numbers-article-units");
  await units.fill("条\n款"); await page.getByRole("button", { name: "草稿を保存", exact: true }).click();
  await expect(page.getByText("別の利用者が草稿を更新しました。", { exact: true })).toBeVisible();
  await expect(units).toHaveValue("条\n款");
  await page.getByRole("button", { name: "再試行", exact: true }).click();
  const dialog = page.getByRole("alertdialog"); await expect(dialog).toContainText("編集中の変更を破棄");
  await dialog.getByRole("button", { name: "最新の草稿を読み込む", exact: true }).click();
  await expect(units).toHaveValue("条\n項");
  await expect(page.getByRole("button", { name: "草稿を保存", exact: true })).toBeDisabled();
});


test("索引が参照する過去版は現在の履歴ページに無くても ID で読み取り専用表示する", { tag: "@responsive" }, async ({ page }) => {
  const { readState } = await mockLanguagePack(page);
  await page.route("**/api/settings/language-pack/releases?*", route => route.fulfill({ json: envelope({ releases: [readState().current], total: 1 }) }));
  await page.goto("/settings/language-pack?release=v1");
  const details = page.getByRole("region", { name: "公開版の内容（読み取り専用）" });
  await expect(details.getByRole("heading")).toContainText("v1");
  await expect(details.getByLabel("公開時の完全な設定", { exact: true })).toHaveAttribute("readonly");
  await expect(details.getByLabel("条の単位", { exact: true })).toHaveValue("条\n款");
  await expectNoPageOverflow(page);
});


test("入力上限で省略した根拠の案内を名前と説明で編集できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const key = "answer_evidence_budget_limited";
  const text = "入力の上限により {count} 件の根拠を省略しました。";
  await mockLanguagePack(page, { ...BUILTIN, answer_labels: { [key]: text } }, [
    ...FIELDS, { section: "answer_labels", key, kind: "text", choices: [], ingest: false, max_chars: 1000 },
  ]);
  await page.goto("/settings/language-pack");
  await page.getByRole("searchbox", { name: "項目を探す" }).fill("入力の上限で省略した根拠の案内");
  await expect(page.getByLabel("入力の上限で省略した根拠の案内", { exact: true })).toHaveValue(text);
  await expect(page.getByText("回答への入力の上限で根拠を省いたときの案内です。{count} を省略した件数に置き換えます。", { exact: true })).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath(`budget-language-${testInfo.project.name}.png`), fullPage: true });
});


test("根拠不足の定型文はラベルと説明で編集でき、草稿に保存される", { tag: "@responsive" }, async ({ page }) => {
  const values = {
    insufficient_evidence_summary: "合成の根拠不足の案内です。",
    empty_search_guidance: "合成の検索範囲の確認案内です。",
    abstention_guidance: "合成の質問条件の確認案内です。",
  };
  const fields = Object.keys(values).map(key => ({ section: "answer_labels", key, kind: "text", choices: [], ingest: false, max_chars: 1000 }));
  const { saved } = await mockLanguagePack(page, { ...BUILTIN, answer_labels: values }, [...FIELDS, ...fields]);
  await page.goto("/settings/language-pack");
  await page.getByRole("tablist", { name: "言語資源の使い道" }).getByRole("tab", { name: "回答を確かめて表す" }).click();
  for (const key of Object.keys(values) as (keyof typeof values)[]) {
    const value = values[key];
    const field = page.getByLabel(t(`settings.language.field.${key}`), { exact: true });
    await expect(field).toHaveValue(value);
    await expect(field).toHaveAccessibleDescription(t(`settings.language.field.${key}.help`));
  }
  const summary = page.getByLabel(t("settings.language.field.insufficient_evidence_summary"), { exact: true });
  await summary.fill("合成の変更した案内です。");
  await page.getByRole("button", { name: "草稿を保存", exact: true }).click();
  await expect.poll(() => saved.overrides).toEqual({ answer_labels: { insufficient_evidence_summary: "合成の変更した案内です。" } });
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("abstention-labels.png"), fullPage: true });
});
