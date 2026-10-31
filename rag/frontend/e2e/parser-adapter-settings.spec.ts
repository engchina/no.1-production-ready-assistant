import { mockFeaturePrompts, verifyFeaturePrompt } from "./_feature-prompt-helpers";
import { expect, test, type Page } from "./fixtures/test";
import { expectNoPageOverflow, mockAuthUser, mockLocalAuth, openSidebarNav } from "./_helpers";

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
  await mockParserServiceStatuses(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760, collapseSidebar: false },
  { name: "mobile", width: 375, height: 812, collapseSidebar: true },
]) {
  test(`文書解析設定は稼働状況を表示する (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapseSidebar) {
      await page.addInitScript(() => {
        window.localStorage.setItem(
          "production-ready-rag.ui",
          JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
        );
      });
    }
    await mockParserAdapters(page);

    await page.goto("/settings/parser-adapters");

    await expect(page.getByRole("heading", { name: "文書解析", exact: true, level: 1 })).toBeVisible();
    await expect(page.getByRole("radio", { name: /^Local/ })).toHaveCount(0);
    await expect(page.getByRole("radio", { name: /Docling.*CPU.*稼働中/ })).toBeVisible();
    await expect(page.getByRole("radio", { name: /Unstructured.*CPU.*縮退/ })).toBeVisible();
    // 既定の解析エンジンは Docling(#286)。Unstructured は明示選択したときだけ使う。
    await expect(page.getByRole("radio", { name: /Docling.*既定の解析エンジン/ })).toBeVisible();
    await expect(page.getByRole("radio", { name: /既定の解析エンジン/ })).toHaveCount(1);
    await expect(
      page.getByRole("radio", { name: /Unstructured.*選んだ場合だけ使用/ })
    ).toBeVisible();
    // 対応形式の拡張子に .rst / .org を出し、読まない形式（.heic / .epub など）は出さない（#3351）。
    const unstructuredCard = page.locator('label[for="settings-parser-backend-unstructured"]');
    await expect(unstructuredCard).toContainText(".org");
    await expect(unstructuredCard).toContainText(".rst");
    await expect(unstructuredCard).not.toContainText(".heic");
    await expect(unstructuredCard).not.toContainText(".epub");
    await page.screenshot({
      path: test.info().outputPath(`parser-settings-${viewport.name}.png`),
      fullPage: true,
    });
    // Marker / Unlimited-OCR / GLM-OCR は #270 で削除した。選択肢に出さない。
    await expect(page.getByRole("radio", { name: /Marker|Unlimited-OCR|GLM-OCR/ })).toHaveCount(0);
    // OCI Generative AI (Vision) は削除した（#2042）。
    await expect(page.getByRole("radio", { name: /OCI Generative AI/ })).toHaveCount(0);
    // 構造化レコード JSONL・XML ツリー JSON の専用のエンジンは削除した（#21）。
    await expect(page.getByRole("radio", { name: /構造化レコード JSONL|XML ツリー JSON/ })).toHaveCount(0);
    // 「形式で自動に回す解析エンジン」の節とスイッチは無い。
    await expect(page.getByText("形式で自動に回す解析エンジン")).toHaveCount(0);
    await expect(page.getByRole("switch", { name: "XML ツリー JSON" })).toHaveCount(0);
    // エンジン名は本物のラジオのラベル（カード）の文字（#469）。
    const engineNames = (
      await page
        .getByRole("radio")
        .evaluateAll((elements) =>
          elements.map((element) => (element as HTMLInputElement).labels?.[0]?.textContent ?? "")
        )
    ).map((text) => text.replace(/\s+/g, " ").trim());
    // 並びは PARSER_BACKEND_ORDER。Docling → Unstructured（#21）。処理レシピの選択肢も同じ順。
    expect(engineNames).toHaveLength(2);
    expect(engineNames[0]).toContain("Docling");
    expect(engineNames[1]).toContain("Unstructured");
    // desktop は 2 列（1 行に Docling・Unstructured）、375px は 1 列で同じ順。
    const cardBoxes = await page
      .locator('label[for^="settings-parser-backend-"]')
      .evaluateAll((elements) =>
        elements.map((element) => {
          const box = element.getBoundingClientRect();
          return { x: Math.round(box.left), y: Math.round(box.top) };
        })
      );
    expect(cardBoxes).toHaveLength(2);
    if (viewport.name === "desktop") {
      expect(cardBoxes[0].y).toBe(cardBoxes[1].y);
      expect(cardBoxes[0].x).toBeLessThan(cardBoxes[1].x);
    } else {
      for (let index = 1; index < cardBoxes.length; index += 1) {
        expect(cardBoxes[index].x).toBe(cardBoxes[0].x);
        expect(cardBoxes[index].y).toBeGreaterThan(cardBoxes[index - 1].y);
      }
    }
    // 外部 GPU 解析エンジンの接続は削除した（#22）。
    await expect(page.getByText("外部 GPU 解析エンジンの接続")).toHaveCount(0);
    await expect(page.getByLabel("Endpoint")).toHaveCount(0);
    await expect(page.getByText("運用診断", { exact: true })).toHaveCount(0);
    await expect(page.getByText("解析方式の稼働状況")).toHaveCount(0);
    await expect(page.getByText("原本種別ごとの実行順")).toHaveCount(0);
    await expect(page.getByText("未導入", { exact: true })).toHaveCount(0);
    await expect(page.getByText("パッケージ未導入", { exact: true })).toHaveCount(0);
    // MinerU / Dots.OCR / OCI Document Understanding は削除した（#22）。選択肢に出さない。
    await expect(
      page.getByRole("radio", { name: /MinerU|Dots\.OCR|OCI Document Understanding/ })
    ).toHaveCount(0);

    // 375px ではナビがドロワー（#367）。開いて現在地を確かめる。
    const navLink = (await openSidebarNav(page)).getByRole("link", { name: "文書解析" });
    await expect(navLink).toHaveAttribute("aria-current", "page");
    await navLink.focus();
    await expect(navLink).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/settings\/parser-adapters$/);
    await expectNoHorizontalOverflow(page);
  });
}

const SCREENSHOT_DIR = process.env.RAG_E2E_SCREENSHOT_DIR;

async function useTheme(page: Page, scheme: "light" | "dark") {
  await page.addInitScript((theme) => {
    window.localStorage.setItem(
      "production-ready-rag.ui",
      JSON.stringify({ state: { theme }, version: 0 })
    );
  }, scheme);
}

async function expectTheme(page: Page, scheme: "light" | "dark") {
  await expect
    .poll(() => page.evaluate(() => document.documentElement.classList.contains("dark")))
    .toBe(scheme === "dark");
}

/** 文書解析の GET / PATCH。PATCH の payload を記録し、保存後の値を返す。 */
async function mockParserAdaptersWithPostParse(
  page: Page,
  initial: { vision_enabled: boolean; field_extraction_enabled: boolean; navigation_summary_enabled: boolean }
) {
  const patches: Record<string, unknown>[] = [];
  let current = { heading_level_inference_enabled: false, navigation_summary_max_nodes: 37, ...initial };
  const envelope = () =>
    parserAdapterEnvelope({
      ...current,
      adapter_backend: "docling",
      effective_order: ["docling"],
      config_source: "runtime",
      adapters: [
        { ...disabledAdapter("docling"), enabled: true, selected: true, installed: true, status: "active" },
        disabledAdapter("unstructured"),
      ],
    });
  await page.route("**/api/settings/parser-adapters", async (route) => {
    if (route.request().method() === "PATCH") {
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      patches.push(payload);
      current = { ...current, ...payload } as typeof current;
    }
    await route.fulfill({ json: envelope() });
  });
  return patches;
}

async function mockExtractionFields(
  page: Page,
  initial: { name: string; description: string; value_type: string }[] = []
) {
  const patches: unknown[] = [];
  let fields = initial;
  await page.route("**/api/settings/extraction-fields", async (route) => {
    if (route.request().method() === "PATCH") {
      const payload = route.request().postDataJSON() as { fields: typeof fields };
      patches.push(payload);
      fields = payload.fields;
    }
    await route.fulfill({
      json: { data: { fields, uses_standard: false }, error_messages: [], warning_messages: [] },
    });
  });
  return patches;
}

/** 標準の項目（backend の STANDARD_FIELD_DEFINITIONS。説明は画面の確認に要らないので短くする）。 */
const STANDARD_FIELDS = [
  { name: "文書の種類", description: "文書の種類を短い名詞で答える。", value_type: "string" },
  { name: "文書タイトル", description: "文書の正式な題名。", value_type: "string" },
  { name: "発行日・作成日", description: "文書が発行・作成された日付。", value_type: "date" },
  { name: "発行元・作成部署", description: "文書を発行・作成した組織名。", value_type: "string" },
];

/** 全体の既定を保存していない環境（標準の項目。#556）。PATCH で保存、DELETE で標準に戻す。 */
async function mockStandardExtractionFields(page: Page) {
  const calls: string[] = [];
  let fields = STANDARD_FIELDS;
  let usesStandard = true;
  await page.route("**/api/settings/extraction-fields", async (route) => {
    const method = route.request().method();
    if (method === "PATCH") {
      calls.push("PATCH");
      fields = (route.request().postDataJSON() as { fields: typeof fields }).fields;
      usesStandard = false;
    } else if (method === "DELETE") {
      calls.push("DELETE");
      fields = STANDARD_FIELDS;
      usesStandard = true;
    }
    await route.fulfill({
      json: { data: { fields, uses_standard: usesStandard }, error_messages: [], warning_messages: [] },
    });
  });
  return calls;
}

for (const scheme of ["light", "dark"] as const) {
  test(`解析後の処理は Vision・見出しの階層・項目抽出・章節木を処理順に並べ、全体の既定を保存する (${scheme})`, async ({
    page,
  }, testInfo) => {
    // #528: Vision は解析エンジンに関係なく使えるので、全体の既定の入口をこの画面に置く。
    await useTheme(page, scheme);
    const patches = await mockParserAdaptersWithPostParse(page, {
      vision_enabled: false,
      field_extraction_enabled: false,
      navigation_summary_enabled: false,
    });
    await mockExtractionFields(page);

    await page.goto("/settings/parser-adapters");
    await expectTheme(page, scheme);

    const card = page.getByRole("heading", { name: "解析後の処理", exact: true }).locator("xpath=ancestor::*[.//ol][1]");
    await expect(card.getByRole("heading", { level: 3 })).toHaveText([
      "1. 図・画像を AI で読み取る",
      "2. 見出しの階層の推定（AI）",
      "3. メタデータ/項目抽出",
      "4. 章節の抽出規則",
      "5. 章節の要約",
    "6. RAPTOR の階層の要約",
    ]);
    await expect(card).toContainText("Docling の解析結果のうち、PDF のしおりの階層を使えず");
    await expect(card).toContainText("番号から階層を決められない見出しの割合と見出し数が設定の条件を満たす文書が対象です。");
    await expect(card).toContainText("見出しの文字列だけ（長さの上限あり）");
    await expect(card).toContainText("通常は対象の文書ごとに 1 回呼び出しますが");
    await expect(card).toContainText("一時的な通信障害や流量制限などで再試行する場合があります。");
    await expect(card).toContainText("今の階層のまま取り込みます。");
    await expect(card).not.toContainText("PDF のしおりも見出しの番号も無い");
    // 見出しの階層の推定は既定で無効（#2277）。
    await expect(page.getByRole("switch", { name: "見出しの階層の推定（AI）" })).toHaveAttribute(
      "aria-checked",
      "false"
    );
    await expect(card).toContainText("解析エンジンに関係なく");
    await expect(card).toContainText("既定の画像対応モデル");
    await expect(card.getByRole("link", { name: "モデル設定を開く" })).toHaveAttribute("href", "/settings/model");

    const vision = page.getByRole("switch", { name: "図・画像を AI で読み取る" });
    const fieldExtraction = page.getByRole("switch", { name: "メタデータ/項目抽出" });
    await expect(card).toContainText("解析した本文全体から");
    await expect(card).toContainText("長い本文は分けて送り、部分ごとにモデルを呼び出します。");
    await expect(card).toContainText("本文が長いほど入力トークンの総量と処理時間が増えます。");
    await expect(card).not.toContainText("有効にすると、文書ごとにモデルの呼び出しが 1 回増えます。");
    const navigation = page.getByRole("switch", { name: "章節の要約" });
    await expect(vision).toHaveAttribute("aria-checked", "false");
    await vision.click();
    await fieldExtraction.click();
    await expect(vision).toHaveAttribute("aria-checked", "true");
    // 項目の定義を 0 件で保存した環境で有効にすると、何も抽出しないことを知らせる（#556）。
    await expect(card.getByText(/項目の定義が 0 件で保存されているため/)).toBeVisible();
    await expect(card.getByText("未保存の変更があります。")).toBeVisible();

    if (SCREENSHOT_DIR) {
      await card.screenshot({ path: `${SCREENSHOT_DIR}/parser-post-parse-${testInfo.project.name}-${scheme}.png` });
    }

    await card.getByRole("button", { name: "解析後の処理を保存" }).click();
    await expect(page.getByText("解析後の処理を保存しました。")).toBeVisible();
    // 解析エンジンの設定は送らない（「解析後の処理」だけを保存する）。
    expect(patches).toEqual([
      {
        vision_enabled: true,
        heading_level_inference_enabled: false,
        field_extraction_enabled: true,
        navigation_summary_enabled: false,
        raptor_enabled: false,
        vision_concurrency: 4,
      },
    ]);
    await expect(navigation).toHaveAttribute("aria-checked", "false");
    // 解析エンジンの側は未保存にならない。
    await expect(page.getByText("未保存の変更があります。")).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
  });
}

test("図・表の説明を同時に呼ぶ数を検証して保存する（#2002）", async ({ page }) => {
  const patches = await mockParserAdaptersWithPostParse(page, {
    vision_enabled: true,
    field_extraction_enabled: false,
    navigation_summary_enabled: false,
  });
  await mockExtractionFields(page);

  await page.goto("/settings/parser-adapters");

  const card = page.getByRole("heading", { name: "解析後の処理", exact: true }).locator("xpath=ancestor::*[.//ol][1]");
  const concurrency = card.getByLabel("同時に説明する図・表の数");
  await expect(concurrency).toHaveValue("4");
  await expect(card).toContainText("API の制限（429）を受けると自動で減らし");

  // 範囲の外は保存せず、欄の直下に理由を出してその欄へ移る。
  await concurrency.fill("0");
  await card.getByRole("button", { name: "解析後の処理を保存" }).click();
  await expect(card.getByText("1 から 16 までの整数を入力してください。")).toBeVisible();
  await expect(concurrency).toBeFocused();
  expect(patches).toEqual([]);

  await concurrency.fill("6");
  await card.getByRole("button", { name: "解析後の処理を保存" }).click();
  await expect(page.getByText("解析後の処理を保存しました。")).toBeVisible();
  expect(patches).toEqual([
    {
      vision_enabled: true,
      heading_level_inference_enabled: false,
      field_extraction_enabled: false,
      navigation_summary_enabled: false,
      raptor_enabled: false,
      vision_concurrency: 6,
    },
  ]);
  await expect(concurrency).toHaveValue("6");
  await expect(card.getByText("未保存の変更があります。")).toHaveCount(0);
});

test("項目抽出の項目の定義を追加・検証・保存できる", { tag: "@responsive" }, async ({ page }) => {
  await mockParserAdaptersWithPostParse(page, {
    vision_enabled: false,
    field_extraction_enabled: true,
    navigation_summary_enabled: false,
  });
  const patches = await mockExtractionFields(page, [
    { name: "請求書番号", description: "請求書の番号", value_type: "string" },
  ]);

  await page.goto("/settings/parser-adapters");

  const definitions = page.getByText("抽出する項目の定義", { exact: true });
  await expect(page.getByText("1 件", { exact: true })).toBeVisible();
  await definitions.click();
  const editor = page.getByTestId("extraction-fields-editor");
  await expect(editor.getByLabel("項目名")).toHaveValue("請求書番号");
  const layout = await editor.evaluate(node => {
    const row = node.querySelector("ol > li")!;
    const description = node.querySelector<HTMLInputElement>('input[id$="-description"]')!;
    return { container: node.clientWidth, row: row.getBoundingClientRect().width, description: description.getBoundingClientRect().width,
      name: node.querySelector('input[id$="-name"]')!.getBoundingClientRect().width };
  });
  expect(Math.abs(layout.row - layout.container)).toBeLessThanOrEqual(1);
  if (page.viewportSize()!.width >= 1440) expect(layout.description).toBeGreaterThan(layout.name);
  await expectNoPageOverflow(page);
  await editor.screenshot({ path: test.info().outputPath("extraction-fields-full-width.png") });


  await editor.getByRole("button", { name: "項目を追加" }).click();
  await expect(editor.getByLabel("項目名").nth(1)).toBeFocused();
  // 項目名が空のまま保存すると、その行で止める。
  await editor.getByRole("button", { name: "項目の定義を保存" }).click();
  await expect(editor.getByText("項目名を入力してください。")).toBeVisible();
  await expect(editor.getByLabel("項目名").nth(1)).toBeFocused();
  // 大文字小文字の違いだけの重複も止める。
  await editor.getByLabel("項目名").nth(1).fill("請求書番号");
  await editor.getByRole("button", { name: "項目の定義を保存" }).click();
  await expect(editor.getByText("同じ項目名がほかの行にあります。")).toBeVisible();
  expect(patches).toEqual([]);

  await editor.getByLabel("項目名").nth(1).fill(" 合計金額 ");
  await editor.getByLabel("説明", { exact: true }).nth(1).fill("税込の合計");
  await editor.getByRole("combobox", { name: "値の型" }).nth(1).click();
  await page.getByRole("option", { name: "数値" }).click();
  await editor.getByRole("button", { name: "項目の定義を保存" }).click();
  await expect(editor.getByText("項目の定義を保存しました。")).toBeVisible();
  expect(patches).toEqual([
    {
      fields: [
        { name: "請求書番号", description: "請求書の番号", value_type: "string" },
        { name: "合計金額", description: "税込の合計", value_type: "number" },
      ],
    },
  ]);
  await expect(page.getByText("2 件", { exact: true })).toBeVisible();
  if (SCREENSHOT_DIR) {
    await editor.screenshot({ path: `${SCREENSHOT_DIR}/extraction-fields-${test.info().project.name}.png` });
  }

  // 行を削除すると未保存になり、破棄で保存値に戻る。
  await editor.getByRole("button", { name: "項目 2 を削除" }).click();
  await expect(editor.getByLabel("項目名")).toHaveCount(1);
  await editor.getByRole("button", { name: "変更を破棄" }).click();
  await expect(editor.getByLabel("項目名")).toHaveCount(2);
  await expectNoHorizontalOverflow(page);
});

for (const scheme of ["light", "dark"] as const) {
  test(`未保存の環境は標準の項目を使い、保存した定義を「標準の項目に戻す」で戻せる (${scheme})`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    // #556: 一度も保存していない環境は標準の 4 項目。375px ではナビを畳んで本文の幅で確かめる。
    const mobile = testInfo.project.name === "mobile";
    await page.addInitScript(
      ({ theme, collapsed }) => {
        window.localStorage.setItem(
          "production-ready-rag.ui",
          JSON.stringify({ state: { theme, sidebarCollapsed: collapsed }, version: 0 })
        );
      },
      { theme: scheme, collapsed: mobile }
    );
    await mockParserAdaptersWithPostParse(page, {
      vision_enabled: false,
      field_extraction_enabled: true,
      navigation_summary_enabled: false,
    });
    const calls = await mockStandardExtractionFields(page);

    await page.goto("/settings/parser-adapters");
    await expectTheme(page, scheme);

    const item = page.locator("#post-parse-field-extraction");
    // 長い本文は部分ごとにモデルを呼び出すことを明記する。
    await expect(item).toContainText("長い本文は分けて送り、部分ごとにモデルを呼び出します。");
    await expect(item.getByText("標準の項目", { exact: true })).toBeVisible();
    await expect(item.getByText("4 件", { exact: true })).toBeVisible();
    // 標準の項目は 0 件ではないので、何も抽出しない警告は出さない。
    await expect(item.getByText(/0 件で保存されているため/)).toHaveCount(0);

    await item.getByText("抽出する項目の定義", { exact: true }).click();
    const editor = page.getByTestId("extraction-fields-editor");
    await expect(editor.getByText(/標準の項目を使っています/)).toBeVisible();
    await expect(editor.getByLabel("項目名")).toHaveCount(4);
    await expect(editor.getByLabel("項目名").nth(2)).toHaveValue("発行日・作成日");
    const resetButton = editor.getByRole("button", { name: "標準の項目に戻す" });
    await expect(resetButton).toBeDisabled();
    if (SCREENSHOT_DIR) {
      await item.screenshot({
        path: `${SCREENSHOT_DIR}/extraction-fields-standard-${testInfo.project.name}-${scheme}.png`,
      });
    }
    await expectNoHorizontalOverflow(page);

    // 標準の項目を編集して保存すると、保存した定義を使う（標準の印が消え、戻せるようになる）。
    await editor.getByRole("button", { name: "項目 4 を削除" }).click();
    await editor.getByRole("button", { name: "項目の定義を保存" }).click();
    await expect(editor.getByText("項目の定義を保存しました。")).toBeVisible();
    await expect(item.getByText("3 件", { exact: true })).toBeVisible();
    await expect(item.getByText("標準の項目", { exact: true })).toHaveCount(0);
    await expect(editor.getByText(/標準の項目を使っています/)).toHaveCount(0);
    await expect(resetButton).toBeEnabled();

    // 編集中でも戻せる。確認ダイアログを取り消すと何もしない。
    await editor.getByLabel("項目名").first().fill("文書種別");
    await resetButton.click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("標準の項目に戻しますか？");
    await expect(dialog).toContainText("編集中の変更も破棄します");
    await dialog.getByRole("button", { name: "キャンセル" }).click();
    await expect(dialog).toHaveCount(0);
    expect(calls).toEqual(["PATCH"]);
    await expect(editor.getByLabel("項目名").first()).toHaveValue("文書種別");

    await resetButton.click();
    await page.getByRole("alertdialog").getByRole("button", { name: "標準の項目に戻す" }).click();
    await expect(page.getByText("標準の項目に戻しました。")).toBeVisible();
    expect(calls).toEqual(["PATCH", "DELETE"]);
    // 編集中の変更も捨て、標準の 4 項目に戻る。
    await expect(editor.getByLabel("項目名")).toHaveCount(4);
    await expect(editor.getByLabel("項目名").first()).toHaveValue("文書の種類");
    await expect(editor.getByText(/標準の項目を使っています/)).toBeVisible();
    await expect(item.getByText("標準の項目", { exact: true })).toBeVisible();
    await expect(resetButton).toBeDisabled();
    await expect(page.getByText("未保存の変更があります。")).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
  });
}

// #715: 章節の抽出規則（方式・独自の規則）。見本の文書でのプレビューは削除した（#2042）。
async function mockSectionRules(page: Page) {
  const state: { mode: string; rules: unknown[]; patches: unknown[] } = {
    mode: "parser",
    rules: [],
    patches: [],
  };
  const presets = {
    legal: [{ name: "章", pattern: "^第[0-9]+章", level: 2, enabled: true }],
    official: [{ name: "第1", pattern: "^第[0-9]+(\\s|$)", level: 1, enabled: true }],
    numbered: [{ name: "1.", pattern: "^[0-9]+\\.\\s", level: 1, enabled: true }],
  };
  const envelope = () => ({
    data: { mode: state.mode, rules: state.rules, presets },
    error_messages: [],
    warning_messages: [],
  });
  await page.route("**/api/settings/section-rules", async (route) => {
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON() as { mode: string; rules: unknown[] };
      state.patches.push(body);
      state.mode = body.mode;
      state.rules = body.rules;
    }
    await route.fulfill({ json: envelope() });
  });
  return state;
}

test("章節の抽出規則を独自の規則にして保存できる", { tag: "@responsive" }, async ({ page }) => {
  await mockParserAdaptersWithPostParse(page, {
    vision_enabled: false,
    field_extraction_enabled: false,
    navigation_summary_enabled: false,
  });
  await mockExtractionFields(page);
  const state = await mockSectionRules(page);

  await page.goto("/settings/parser-adapters#post-parse-section-rules");
  await expect(page.locator("#post-parse-section-rules")).toBeInViewport();
  const rulesCard = page.locator("#post-parse-section-rules");
  const postParse = page.getByRole("heading", { name: "解析後の処理", exact: true })
    .locator("xpath=ancestor::*[.//ol][1]");
  await expect(postParse.locator("#post-parse-section-rules")).toHaveCount(1);
  await expect(rulesCard.getByRole("heading", { name: "4. 章節の抽出規則", exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("section-rules-step.png") });
  await expectNoPageOverflow(page);
  await expect(page.locator("#post-parse-navigation-summary").getByTestId("section-rules-editor")).toHaveCount(0);
  await expect(page.getByRole("switch", { name: "章節の要約", exact: true })).not.toBeChecked();
  await expect(postParse.getByRole("heading", { level: 3 })).toHaveText([
    "1. 図・画像を AI で読み取る",
    "2. 見出しの階層の推定（AI）",
    "3. メタデータ/項目抽出",
    "4. 章節の抽出規則",
    "5. 章節の要約",
    "6. RAPTOR の階層の要約",
  ]);
  const editor = page.getByTestId("section-rules-editor");
  await expect(editor).toContainText("保存済みの解析結果にある見出しと階層を使います");

  // プリセット（法令）を独自の規則として編集する。
  await editor.getByRole("combobox", { name: "方式" }).click();
  await page.getByRole("option", { name: "番号の規則：編・章・節・款・目・条" }).click();
  await expect(editor).toContainText("^第[0-9]+章");
  await editor.getByRole("button", { name: "このプリセットを独自の規則として編集" }).click();
  await expect(editor.getByRole("textbox", { name: "正規表現" })).toHaveValue("^第[0-9]+章");

  // 規則を追加し、読めない正規表現は保存の前に欄の下で知らせる。
  await editor.getByRole("button", { name: "規則を追加" }).click();
  await editor.getByRole("textbox", { name: "名前" }).nth(1).fill("条");
  await editor.getByRole("textbox", { name: "正規表現" }).nth(1).fill("^第[0-9]+条(");
  await editor.getByRole("button", { name: "保存" }).click();
  await expect(editor.getByText("正規表現として読めません。括弧の対応などを確認してください。")).toBeVisible();
  expect(state.patches).toHaveLength(0);
  await editor.getByRole("textbox", { name: "正規表現" }).nth(1).fill("^第[0-9]+条");

  // 見本の文書でのプレビューは削除した（#2042）。
  await expect(editor.getByText("見本の文書でプレビュー")).toHaveCount(0);
  await expect(editor.getByRole("button", { name: "プレビュー" })).toHaveCount(0);

  await editor.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("章節の抽出規則を保存しました。").first()).toBeVisible();
  expect(state.patches).toEqual([
    {
      mode: "custom",
      rules: [
        { name: "章", pattern: "^第[0-9]+章", level: 2, enabled: true },
        { name: "条", pattern: "^第[0-9]+条", level: 1, enabled: true },
      ],
    },
  ]);
  await expectNoHorizontalOverflow(page);
});

test("図・画像の読み取りプロンプトは Vision の項目の中で編集できる", async ({ page }) => {
  // 読み取りの指示は全体で 1 つ。Vision の全体の既定を無効にしていても編集できる(#497 / #528)。
  await mockParserAdaptersWithPostParse(page, {
    vision_enabled: false,
    field_extraction_enabled: false,
    navigation_summary_enabled: false,
  });
  await mockExtractionFields(page);
  let saved: unknown = null;
  await page.route("**/api/settings/answer-prompts**", async (route) => {
    if (route.request().method() === "PUT") saved = route.request().postDataJSON();
    const content = saved ? (saved as { content: string }).content : "既定の指示 {{image_metadata}}";
    await route.fulfill({
      json: {
        data: {
          prompts: [
            {
              key: "image_retrieval",
              content,
              default_content: "既定の指示 {{image_metadata}}",
              customized: Boolean(saved),
              required_placeholders: ["image_metadata"],
              updated_at: saved ? "2026-09-26T01:00:00Z" : null,
            },
          ],
          stages: [],
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  await page.goto("/settings/parser-adapters");

  const visionItem = page.locator("#post-parse-vision");
  await visionItem.locator("summary", { hasText: "図・画像の読み取りプロンプト" }).click();
  await expect(visionItem).toContainText("解析エンジンに関係なく");
  await expect(visionItem.getByText("既定値", { exact: true })).toHaveCount(0);
  await expect(visionItem.getByRole("group", { name: "編集内容の操作" }).getByRole("button"))
    .toHaveText(["プロンプトを保存", "変更を破棄", "既定に戻す"]);
  await visionItem.getByLabel("プロンプト").fill("図の要点を短く {{image_metadata}}");
  await visionItem.getByRole("button", { name: "プロンプトを保存" }).click();
  await expect.poll(() => saved).toEqual({ content: "図の要点を短く {{image_metadata}}" });
  // 保存の成功は Toast（messaging.md §10.2。#1010）。
  await expect(page.getByText("プロンプトを保存しました。")).toBeVisible();
  await expect(visionItem.getByText(/^編集済み/)).toBeVisible();
});

test("hash 付きの URL で解析後の処理の項目へ移動する", async ({ page }) => {
  // レシピの「グローバル設定を開く」と設定の概要のリンクの移動先(#528)。読み込み後に描画される節にも届く。
  await mockParserAdaptersWithPostParse(page, {
    vision_enabled: false,
    field_extraction_enabled: false,
    navigation_summary_enabled: true,
  });
  await mockExtractionFields(page);

  await page.goto("/settings/parser-adapters#post-parse-navigation-summary");

  const target = page.locator("#post-parse-navigation-summary");
  await expect(target).toBeInViewport();
  await expect(target.getByRole("switch", { name: "章節の要約" })).toHaveAttribute(
    "aria-checked",
    "true"
  );
});

test("文書解析設定取得に失敗したら再試行できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await page.route("**/api/settings/parser-adapters", async (route) => {
    await route.fulfill({
      status: 503,
      json: {
        data: null,
        error_messages: ["文書解析設定を取得できませんでした。"],
        warning_messages: [],
      },
    });
  });

  await page.goto("/settings/parser-adapters");

  await expect(page.getByRole("alert")).toContainText(
    "文書解析設定を取得できませんでした。"
  );
  await expect(page.getByRole("button", { name: "再試行" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("文書解析設定は使用エンジンを保存できる", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "production-ready-rag.ui",
      JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
    );
  });
  let savedPayload: unknown = null;
  await page.route("**/api/settings/parser-adapters", async (route) => {
    if (route.request().method() === "PATCH") {
      savedPayload = route.request().postDataJSON();
      await route.fulfill({
        json: parserAdapterEnvelope({
          adapter_backend: "unstructured",
          effective_order: ["unstructured"],
          config_source: "runtime",
          adapters: [
            disabledAdapter("docling"),
            {
              ...disabledAdapter("unstructured"),
              enabled: true,
              selected: true,
              status: "active",
            },
          ],
        }),
      });
      return;
    }
    await route.fulfill({
      json: parserAdapterEnvelope({
        adapter_backend: "local",
        effective_order: [],
        config_source: "runtime",
        adapters: [disabledAdapter("docling"), disabledAdapter("unstructured")],
      }),
    });
  });

  await page.goto("/settings/parser-adapters");

  // local は廃止。既定の Docling で解析する旨を出し、microservice エンジン(Unstructured)を選択する。
  await expect(page.getByRole("radio", { name: /^Local/ })).toHaveCount(0);
  await expect(page.getByText(/未選択時は既定の Docling で解析します/)).toBeVisible();
  const unstructuredBackend = page.getByRole("radio", { name: /Unstructured/ });
  await unstructuredBackend.focus();
  await expect(unstructuredBackend).toBeFocused();
  // 本物のラジオは Space（または矢印キー）で選ぶ（#469）。
  await page.keyboard.press("Space");
  await expect(unstructuredBackend).toBeChecked();

  await expect(page.getByText("未保存の変更があります。")).toBeVisible();

  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect(page.getByText("文書解析の設定を保存しました。")).toBeVisible();
  expect(savedPayload).toEqual({
    adapter_backend: "unstructured",
    docling_enabled: false,
    unstructured_enabled: true,
  });
  await expectNoHorizontalOverflow(page);
});

async function mockParserAdapters(page: Page, extra: object = {}) {
  await page.route("**/api/settings/parser-adapters", async (route) => {
    await route.fulfill({
      json: parserAdapterEnvelope({
          ...extra,
          adapter_backend: "docling",
          effective_order: ["docling"],
          config_source: "runtime",
          adapters: [
            {
              backend: "docling",
              package_name: "docling",
              import_name: "docling",
              distribution_name: "docling",
              install_package: "docling==2.103.0",
              enabled: true,
              selected: true,
              installed: true,
              status: "active",
              version: "1.2.3",
              warning_code: null,
            },
            {
              backend: "unstructured",
              package_name: "unstructured",
              import_name: "unstructured",
              distribution_name: null,
              install_package: "unstructured[all-docs]==0.18.32",
              enabled: true,
              selected: false,
              installed: false,
              status: "ignored",
              version: null,
              warning_code: "adapter_flag_ignored_by_backend",
            },
          ],
      }),
    });
  });
}

function parserAdapterEnvelope(data: object) {
  const sourceRoutes = defaultSourceRoutes();
  return {
    data: {
      source_routes: sourceRoutes,
      capabilities: [
        { backend: "docling", modalities: ["pdf", "image"], extensions: [".pdf", ".png"] },
        // Unstructured の代表の拡張子（rag_parser_core.capabilities の一部。#3351 で .rst / .org を足し、
        // pandoc・pi-heif を入れないため HEIC / EPUB / ODT / RTF は出さない）。
        {
          backend: "unstructured",
          modalities: ["pdf", "image", "text", "html", "email", "office"],
          extensions: [".pdf", ".txt", ".md", ".rst", ".org", ".csv", ".html", ".eml", ".png", ".docx"],
        },
      ],
      backend_source_kind_matrix: {
        evidence_source: "runtime_routes",
        required_source_kinds: ["pdf", "image", "office", "html", "email", "text", "unknown"],
        covered_source_kinds: ["pdf", "image", "office", "html", "email", "text", "unknown"],
        missing_source_kinds: [],
        backend_source_kinds: {
          docling: ["pdf", "image", "office", "html"],
          local: ["text", "unknown"],
        },
        route_evidence: sourceRoutes,
      },
      ...data,
    },
    error_messages: [],
    warning_messages: [],
  };
}

function defaultSourceRoutes() {
  return [
    {
      source_kind: "pdf",
      candidate_order: ["docling", "unstructured"],
      attempted_order: ["docling", "unstructured"],
      active_order: ["docling"],
      selected_backend: "docling",
      reason_codes: ["selected_adapter_supported_for_source", "active_adapter_available_for_source"],
      warning_codes: ["unstructured_adapter_package_missing"],
    },
    {
      source_kind: "image",
      candidate_order: ["unstructured", "docling"],
      attempted_order: ["docling"],
      active_order: ["docling"],
      selected_backend: "docling",
      reason_codes: ["selected_adapter_supported_for_source", "active_adapter_available_for_source"],
      warning_codes: [],
    },
    {
      source_kind: "email",
      candidate_order: ["unstructured"],
      attempted_order: [],
      active_order: [],
      selected_backend: "local",
      reason_codes: ["selected_adapter_unsupported_for_source"],
      warning_codes: ["docling_adapter_source_unsupported"],
    },
    {
      source_kind: "text",
      candidate_order: [],
      attempted_order: [],
      active_order: [],
      selected_backend: "local",
      reason_codes: ["local_parser_preferred_for_source", "selected_adapter_unsupported_for_source"],
      warning_codes: [],
    },
  ];
}

async function mockParserServiceStatuses(page: Page) {
  const statuses: Record<string, string> = {
    "parser-docling": "running",
    "parser-unstructured": "degraded",
  };
  await page.route("**/api/services/*/status", async (route) => {
    const serviceId = decodeURIComponent(
      route.request().url().match(/services\/([^/]+)\/status/)?.[1] ?? ""
    );
    const status = statuses[serviceId];
    await route.fulfill({
      status: status ? 200 : 404,
      json: {
        data: status
          ? {
              service_id: serviceId,
              category: "parser",
              profile: serviceProfileForId(serviceId),
              label_key: "settings.services.item.parserDocling",
              execution_policy: "selected_adapter",
              configured: status !== "unconfigured",
              status,
            }
          : null,
        error_messages: status ? [] : ["指定したサービスが見つかりません。"],
        warning_messages: [],
      },
    });
  });
}

function serviceProfileForId(_serviceId: string) {
  return "cpu";
}

function disabledAdapter(backend: "docling" | "unstructured") {
  return {
    backend,
    package_name: backend,
    import_name: backend,
    distribution_name: null,
    install_package:
      backend === "unstructured" ? "unstructured[all-docs]==0.18.32" : "docling==2.103.0",
    enabled: false,
    selected: false,
    installed: false,
    status: "disabled",
    version: null,
    warning_code: null,
  };
}

async function expectNoHorizontalOverflow(page: Page) {
  // documentElement と main の双方を検査する共通ヘルパーへ委譲(_helpers.ts)。
  await expectNoPageOverflow(page);
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`文書解析の設定は保存に失敗しても選んだエンジンと入力を残し、操作の行に失敗を出す（#976, ${viewport.name}）`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockParserServiceStatuses(page);
    const saved = {
      adapter_backend: "docling",
      effective_order: ["docling"],
      config_source: "runtime",
      adapters: [
        { ...disabledAdapter("docling"), enabled: true, selected: true, installed: true, status: "active" },
        disabledAdapter("unstructured"),
      ],
    };
    let patchCount = 0;
    await page.route("**/api/settings/parser-adapters", async (route) => {
      if (route.request().method() === "PATCH") {
        patchCount += 1;
        if (patchCount === 1) {
          await route.fulfill({
            status: 500,
            json: {
              data: null,
              error_messages: ["設定を共有永続化ファイルへ保存できませんでした。"],
              warning_messages: [],
            },
          });
          return;
        }
        await route.fulfill({
          json: parserAdapterEnvelope({
            ...saved,
            adapter_backend: "unstructured",
            effective_order: ["unstructured"],
            adapters: [
              { ...disabledAdapter("docling"), enabled: true, installed: true, status: "active" },
              { ...disabledAdapter("unstructured"), enabled: true, selected: true, status: "active" },
            ],
          }),
        });
        return;
      }
      await route.fulfill({ json: parserAdapterEnvelope(saved) });
    });
    await page.goto("/settings/parser-adapters");

    const unstructured = page.getByRole("radio", { name: /Unstructured/ });
    await unstructured.check();
    const actions = page.getByRole("group", { name: "文書解析の設定の操作" });
    const saveButton = actions.getByRole("button", { name: "保存", exact: true });
    await saveButton.click();

    // 失敗は操作の行に出し、選んだエンジンと保存のボタンを残す。
    await expect(actions).toContainText("設定を共有永続化ファイルへ保存できませんでした。");
    await expect(unstructured).toBeChecked();
    await expect(saveButton).toBeEnabled();
    await expect(actions.getByRole("button", { name: "変更を破棄" })).toBeEnabled();
    await expectNoHorizontalOverflow(page);

    // そのまま再保存できる。
    await saveButton.click();
    await expect(page.getByText("文書解析の設定を保存しました。")).toBeVisible();
    await expect(unstructured).toBeChecked();
    await expect(saveButton).toBeDisabled();
    expect(patchCount).toBe(2);
  });
}


test("文書解析の各プロンプトは機能の位置で共通の編集操作を使う", { tag: "@responsive" }, async ({ page }) => {
  await mockParserAdaptersWithPostParse(page, { vision_enabled: false, field_extraction_enabled: false, navigation_summary_enabled: false });
  await mockExtractionFields(page);
  const requests = await mockFeaturePrompts(page);
  await page.goto("/settings/parser-adapters");
  await expect(page.getByText("GPU 解析エンジンは外部 API を直接呼びます。", {exact: false})).toHaveCount(0);

  for (const [key, title] of [
    ["heading_levels", "見出しの階層の推定プロンプト"],
    ["field_extraction", "メタデータ/項目抽出プロンプト"],
    ["navigation_summary", "章節の要約プロンプト"],
    ["raptor_summary_system", "RAPTOR の階層の要約のシステムプロンプト"],
  ]) {
    await verifyFeaturePrompt(page, key, title);
  }
  expect(requests.map((item) => item.method)).toEqual(["PUT", "DELETE", "PUT", "DELETE", "PUT", "DELETE", "PUT", "DELETE"]);
  await page.getByRole("region", { name: "通知", exact: true }).getByRole("button", { name: "閉じる", exact: true }).evaluateAll((buttons) => buttons.forEach((button) => (button as HTMLButtonElement).click()));
  await page.screenshot({ path: test.info().outputPath("feature-prompts.png"), fullPage: true });
});

test("文書の章節候補の専用確認機能は表示しない", { tag: "@responsive" }, async ({ page }) => {
  await mockParserAdaptersWithPostParse(page, { vision_enabled: false, field_extraction_enabled: false, navigation_summary_enabled: false });
  await mockExtractionFields(page); await mockSectionRules(page);
  await page.goto("/settings/parser-adapters#post-parse-section-rules");
  await expect(page.getByTestId("section-rules-editor")).toBeVisible();
  await expect(page.getByText("文書で章節の候補を確認する", {exact: true})).toHaveCount(0);
  await expect(page.getByTestId("section-rules-preview")).toHaveCount(0);
});


test("章節の方式は広い欄と折り返す選択肢で全文を読める", { tag: "@responsive" }, async ({ page }) => {
  await mockParserAdaptersWithPostParse(page, { vision_enabled: false, field_extraction_enabled: false, navigation_summary_enabled: false });
  await mockExtractionFields(page); await mockSectionRules(page);
  await page.goto("/settings/parser-adapters#post-parse-section-rules");
  const select = page.getByRole("combobox", {name: "方式", exact: true});
  const size = await select.evaluate(node => ({width: node.getBoundingClientRect().width, root: parseFloat(getComputedStyle(document.documentElement).fontSize)}));
  if (page.viewportSize()!.width >= 640) expect(size.width).toBeCloseTo(size.root * 28, 0);
  await select.click();
  const option = page.getByRole("option", {name: "番号の規則：編・章・節・款・目・条", exact: true});
  await expect(option).toBeVisible();
  const clipped = await option.getByText("番号の規則：編・章・節・款・目・条", {exact: true}).evaluate(node => node.scrollWidth > node.clientWidth + 1);
  expect(clipped).toBe(false);
  await option.click(); await expect(select).toHaveText("番号の規則：編・章・節・款・目・条");
  await expectNoPageOverflow(page);
  await select.screenshot({path: test.info().outputPath("section-rules-mode.png")});
});


test("章節の要約は実効の対象数上限を示し不要な比較文を出さない", { tag: "@responsive" }, async ({ page }) => {
  await mockParserAdaptersWithPostParse(page, {vision_enabled: false, field_extraction_enabled: false, navigation_summary_enabled: false});
  await mockExtractionFields(page); await mockFeaturePrompts(page);
  await page.goto("/settings/parser-adapters");
  await expect(page.getByText(/正常に処理した章節が最大 37 件/)).toBeVisible();
  await expect(page.getByText(/モデルの呼び出し回数の上限ではありません/)).toBeVisible();
  await expect(page.getByText("RAPTOR は使いません。", {exact: false})).toHaveCount(0);
});

 test("RAPTOR は文書解析の第6項で既定 OFF、保存と破棄を共通操作で扱う", {tag: "@responsive"}, async ({page}) => {
  const state = await mockParserAdaptersWithPostParse(page, {vision_enabled:false, field_extraction_enabled:false, navigation_summary_enabled:false});
  await mockExtractionFields(page);
  await mockFeaturePrompts(page);
  await page.goto("/settings/parser-adapters");
  const panel = page.locator("#post-parse-raptor-summary");
  await expect(panel.getByRole("heading", {name: "6. RAPTOR の階層の要約", exact:true})).toBeVisible();
  const toggle = panel.getByRole("switch", {name: "RAPTOR の階層の要約", exact:true});
  await expect(toggle).not.toBeChecked();
  await toggle.click();
  const container = panel.locator("xpath=ancestor::*[.//ol][1]");
  await container.getByRole("button", {name: "変更を破棄", exact:true}).click();
  await expect(toggle).not.toBeChecked();
  await toggle.click();
  await container.getByRole("button", {name: "解析後の処理を保存", exact:true}).click();
  await expect.poll(() => state.length).toBe(1);
  expect(state[0]).toMatchObject({raptor_enabled:true});
  await expect(page.locator("summary").filter({hasText: "RAPTOR の階層の要約プロンプト"})).toHaveCount(0);
  await expectNoPageOverflow(page);
  await panel.screenshot({path: test.info().outputPath("raptor-setting.png")});
 });


test("画像の自然言語指示は保存・再読込でき、出力契約は読み取り専用", { tag: "@responsive" }, async ({ page }) => {
  await mockParserAdaptersWithPostParse(page, { vision_enabled: false, field_extraction_enabled: false, navigation_summary_enabled: false });
  await mockExtractionFields(page);
  const parts = [["system", "システムプロンプト"], ["picture", "図・画像を読むときの対象の指示"], ["table", "表を読むときの対象の指示"], ["rules", "共通の読み取り指示"]] as const;
  const saved = new Map<string, string>();
  const requests: string[] = [];
  const builtin = "## Step 1: 合成の既定指示";
  await page.route("**/api/settings/answer-prompts**", async (route) => {
    const method = route.request().method();
    const key = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1)!);
    if (method === "PUT") saved.set(key, route.request().postDataJSON().content);
    if (method === "DELETE") saved.delete(key);
    if (method !== "GET") requests.push(`${method}:${key}`);
    await route.fulfill({ json: { data: {
      prompts: ["image_retrieval", ...parts.map(([id]) => `picture_description.${id}`)].map(key => ({ key, content: saved.get(key) ?? builtin, default_content: builtin, customized: saved.has(key), required_placeholders: [], updated_at: null })),
      stages: [{ id: "picture_description", phase: "ingestion", prompts: [...parts.map(([id]) => ({ id, content: builtin, prompt_key: `picture_description.${id}` })), { id: "contract", content: "## Step 7: 固定の出力契約", prompt_key: null }] }],
    }, error_messages: [], warning_messages: [] } });
  });
  async function openInstructions() {
    await page.goto("/settings/parser-adapters");
    const vision = page.locator("#post-parse-vision");
    await vision.locator("summary", { hasText: "図・画像の読み取りプロンプト" }).click();
    await vision.locator("summary", { hasText: "図・画像の指示と出力形式" }).click();
    return vision.getByTestId("vision-prompt-parts");
  }
  let instructions = await openInstructions();
  for (const [part, label] of parts) {
    const key = `picture_description.${part}`;
    let field = instructions.getByRole("textbox", { name: label, exact: true });
    let actions = field.locator("xpath=ancestor::div[.//*[@role='group']][1]").getByRole("group");
    await field.fill("## Step 1: 保存する合成の指示");
    const custom = await field.inputValue();
    await actions.getByRole("button", { name: "プロンプトを保存", exact: true }).click();
    await expect.poll(() => saved.get(key)).toBe(custom);
    instructions = await openInstructions();
    field = instructions.getByRole("textbox", { name: label, exact: true });
    actions = field.locator("xpath=ancestor::div[.//*[@role='group']][1]").getByRole("group");
    await expect(field).toHaveValue(custom);
    await field.fill("破棄する内容");
    await actions.getByRole("button", { name: "変更を破棄", exact: true }).click();
    await expect(field).toHaveValue(custom);
    const count = requests.length;
    await actions.getByRole("button", { name: "既定に戻す", exact: true }).click();
    await expect(field).toHaveValue(builtin);
    expect(requests).toHaveLength(count);
    await actions.getByRole("button", { name: "変更を破棄", exact: true }).click();
    await expect(field).toHaveValue(custom);
    await actions.getByRole("button", { name: "既定に戻す", exact: true }).click();
    await actions.getByRole("button", { name: "プロンプトを保存", exact: true }).click();
    await expect.poll(() => saved.has(key)).toBe(false);
    await expect(field).toHaveValue(builtin);
  }
  await expect(instructions.getByRole("textbox", { name: "出力形式と上限（読み取り専用）", exact: true })).toHaveAttribute("readonly", "");
  await expect(instructions.getByText("既定値", { exact: true })).toHaveCount(0);
  await expectNoPageOverflow(page);
  await instructions.locator("summary").first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: test.info().outputPath("vision-editable-instructions.png") });
  await instructions.getByRole("textbox", { name: "出力形式と上限（読み取り専用）", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: test.info().outputPath("vision-contract-readonly.png") });
});
