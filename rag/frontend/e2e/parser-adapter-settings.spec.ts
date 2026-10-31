import { mockFeaturePrompts, verifyFeaturePrompt } from "./_feature-prompt-helpers";
import { expect, test, type Page } from "./fixtures/test";
import { expectNoPageOverflow, mockAuthUser, mockLocalAuth, openSidebarNav } from "./_helpers";

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
  await mockParserServiceStatuses(page);
  await mockExternalParserStatuses(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`準備中（ロードマップ）の OCI Document Understanding は灰色で選べない (${viewport.name})`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const serviceRequests: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/services/parser-oci-document-understanding/")) {
        serviceRequests.push(request.url());
      }
    });
    await mockParserAdapters(page);

    await page.goto("/settings/parser-adapters");

    const radio = page.getByRole("radio", { name: /OCI Document Understanding/ });
    await expect(radio).toBeDisabled();
    await expect(radio).toHaveAttribute("data-availability", "roadmap");
    // 理由はカードの文で読み上げる（aria-describedby）。
    await expect(radio).toHaveAccessibleDescription("準備中（ロードマップ）のため、まだ選べません。");
    const card = page.locator('label[for="settings-parser-backend-oci_document_understanding"]');
    await expect(card.locator("[data-roadmap-badge]")).toHaveText("準備中（ロードマップ）");
    await expect(card).toContainText("準備中（ロードマップ）のため、まだ選べません。");
    // 押しても選ばれず、保存できる変更にならない。
    await card.click({ force: true });
    await expect(radio).not.toBeChecked();
    await expect(page.getByRole("radio", { name: /Docling.*既定の解析エンジン/ })).toBeChecked();
    expect(serviceRequests).toEqual([]);
    await expectNoHorizontalOverflow(page);
    await card.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath(`parser-roadmap-${viewport.name}.png`),
      fullPage: true,
    });
  });
}

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
    // MinerU は OCI Document Understanding と同じ準備中（ロードマップ。#3359）。灰色で選べず、
    // 接続の状態（未設定）も出さない。カードの下にライセンスの短い案内と公式のリンクを出す。
    const mineru = page.getByRole("radio", { name: /MinerU.*GPU.*準備中（ロードマップ）/ });
    await expect(mineru).toBeDisabled();
    await expect(mineru).toHaveAttribute("data-availability", "roadmap");
    await expect(page.locator('label[for="settings-parser-backend-mineru"]')).not.toContainText("未設定");
    const mineruNote = page.getByTestId("mineru-license-note");
    await expect(mineruNote).toContainText("MinerU Open Source License");
    await expect(mineruNote.getByRole("link", { name: /MinerU の公式リポジトリ/ })).toHaveAttribute(
      "href",
      "https://github.com/opendatalab/MinerU"
    );
    await expect(mineruNote.getByRole("link", { name: /ライセンスの全文/ })).toHaveAttribute(
      "href",
      "https://github.com/opendatalab/MinerU/blob/master/LICENSE.md"
    );
    // 準備中の間は確認の checkbox と外部接続の欄を出さない。
    await expect(page.getByRole("checkbox", { name: "MinerU のライセンスの条件を確認した" })).toHaveCount(0);
    await expect(page.locator("#external-parser-mineru-endpoint")).toHaveCount(0);
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
    await expect(page.getByRole("radio", { name: /Dots\.OCR.*GPU.*設定済み/ })).toBeVisible();
    // Marker / Unlimited-OCR / GLM-OCR は #270 で削除した。選択肢に出さない。
    await expect(page.getByRole("radio", { name: /Marker|Unlimited-OCR|GLM-OCR/ })).toHaveCount(0);
    // OCI Generative AI (Vision) は削除した（#2042）。
    await expect(page.getByRole("radio", { name: /OCI Generative AI/ })).toHaveCount(0);
    // XML ツリー JSON は Docling・Unstructured と同じ形のカード（CPU・説明・対応形式・稼働の状態。#2042）。
    const xmlTreeJson = page.getByRole("radio", { name: /XML ツリー JSON.*CPU.*稼働中/ });
    await expect(xmlTreeJson).toBeVisible();
    const xmlTreeJsonCard = page.locator('label[for="settings-parser-backend-xml_tree_json"]');
    await expect(xmlTreeJsonCard).toContainText("要素の階層のまま解析");
    await expect(xmlTreeJsonCard).toContainText("対応形式: JSON");
    await expect(xmlTreeJsonCard).toContainText(".json");
    // 構造化レコード JSONL も同じ形のカード（Unstructured の後・XML ツリー JSON の前。#2457）。
    await expect(
      page.getByRole("radio", { name: /構造化レコード JSONL.*CPU.*稼働中/ })
    ).toBeVisible();
    const structuredRecordsCard = page.locator(
      'label[for="settings-parser-backend-structured_records_jsonl"]'
    );
    await expect(structuredRecordsCard).toContainText("記録の単位で解析");
    await expect(structuredRecordsCard).toContainText("一般の JSONL ではなく");
    await expect(structuredRecordsCard).toContainText("分割は「文書分割」の設定");
    // 構造化レコードの JSONL を作るファイル準備は 3 つとも挙げる（#2480）。
    await expect(structuredRecordsCard).toContainText("CSV→JSONL・Excel→JSONL・Word→JSONL");
    await expect(structuredRecordsCard).toContainText("対応形式: JSONL");
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
    // 並びは PARSER_BACKEND_ORDER（#3359。利用者の判断）。CPU は Docling → Unstructured →
    // 構造化レコード JSONL → XML ツリー JSON（#2457）、続いて Dots.OCR → MinerU →
    // OCI Document Understanding。処理レシピの選択肢も同じ順。
    expect(engineNames).toHaveLength(7);
    expect(engineNames[0]).toContain("Docling");
    expect(engineNames[1]).toContain("Unstructured");
    expect(engineNames[2]).toContain("構造化レコード JSONL");
    expect(engineNames[3]).toContain("XML ツリー JSON");
    expect(engineNames[4]).toContain("Dots.OCR");
    expect(engineNames[5]).toContain("MinerU");
    expect(engineNames[6]).toContain("OCI Document Understanding");
    // desktop は 2 列（1 行目 Docling・Unstructured / 2 行目 構造化レコード JSONL・XML ツリー JSON /
    // 3 行目 Dots.OCR・MinerU / 4 行目 OCI Document Understanding）、375px は 1 列で同じ順。
    const cardBoxes = await page
      .locator('label[for^="settings-parser-backend-"]')
      .evaluateAll((elements) =>
        elements.map((element) => {
          const box = element.getBoundingClientRect();
          return { x: Math.round(box.left), y: Math.round(box.top) };
        })
      );
    expect(cardBoxes).toHaveLength(7);
    if (viewport.name === "desktop") {
      for (const row of [0, 2, 4]) {
        expect(cardBoxes[row].y).toBe(cardBoxes[row + 1].y);
        expect(cardBoxes[row].x).toBeLessThan(cardBoxes[row + 1].x);
      }
      expect(cardBoxes[0].y).toBeLessThan(cardBoxes[2].y);
      expect(cardBoxes[2].y).toBeLessThan(cardBoxes[4].y);
      expect(cardBoxes[4].y).toBeLessThan(cardBoxes[6].y);
      expect(cardBoxes[6].x).toBe(cardBoxes[0].x);
    } else {
      for (let index = 1; index < cardBoxes.length; index += 1) {
        expect(cardBoxes[index].x).toBe(cardBoxes[0].x);
        expect(cardBoxes[index].y).toBeGreaterThan(cardBoxes[index - 1].y);
      }
    }
    await expect(page.getByText("外部 GPU 解析エンジンの接続")).toBeVisible();
    // 準備中（ロードマップ）の MinerU の接続の欄は出さない（Dots.OCR の 1 組だけ。#3359）。
    await expect(page.getByLabel("Endpoint")).toHaveCount(1);
    await expect(page.getByLabel("Model")).toHaveCount(1);
    await expect(page.getByLabel("API key", { exact: true })).toHaveCount(1);
    await expect(page.getByText("運用診断", { exact: true })).toHaveCount(0);
    await expect(page.getByText("解析方式の稼働状況")).toHaveCount(0);
    await expect(page.getByText("原本種別ごとの実行順")).toHaveCount(0);
    await expect(page.getByText("未導入", { exact: true })).toHaveCount(0);
    await expect(page.getByText("パッケージ未導入", { exact: true })).toHaveCount(0);
    // OCI Document Understanding は準備中（ロードマップ。#3374）。灰色のカードで選べず、未設定の印も出さない。
    const roadmapRadio = page.getByRole("radio", { name: /OCI Document Understanding/ });
    await expect(roadmapRadio).toBeVisible();
    await expect(roadmapRadio).toBeDisabled();
    await expect(page.getByText("未設定", { exact: true })).toHaveCount(0);

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
        disabledAdapter("mineru"),
        disabledAdapter("dots_ocr"),
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
          adapter_backend: "mineru",
          effective_order: ["mineru"],
          config_source: "runtime",
          adapters: [
            disabledAdapter("docling"),
            disabledAdapter("unstructured"),
            {
              ...disabledAdapter("mineru"),
              availability: "available",
              enabled: true,
              selected: true,
              status: "active",
            },
            disabledAdapter("dots_ocr"),
          ],
          mineru_license: {
            acknowledged: true,
            acknowledged_by: "管理者 A",
            acknowledged_at: "2026-10-26T00:00:00+00:00",
          },
        }),
      });
      return;
    }
    await route.fulfill({
      json: parserAdapterEnvelope({
        adapter_backend: "local",
        effective_order: [],
        config_source: "runtime",
        adapters: [
          disabledAdapter("docling"),
          disabledAdapter("unstructured"),
          // ライセンスの確認から MinerU を選ぶ経路は、準備中の判定を外して確かめる（#3359）。
          { ...disabledAdapter("mineru"), availability: "available" },
          disabledAdapter("dots_ocr"),
        ],
      }),
    });
  });

  await page.goto("/settings/parser-adapters");

  // local は廃止。既定の Docling で解析する旨を出し、microservice エンジン(MinerU)を選択する。
  await expect(page.getByRole("radio", { name: /^Local/ })).toHaveCount(0);
  await expect(page.getByText(/未選択時は既定の Docling で解析します/)).toBeVisible();
  const mineruBackend = page.getByRole("radio", { name: /MinerU/ });
  // MinerU はライセンスの条件を確認するまで選べない（#3359）。
  await expect(mineruBackend).toBeDisabled();
  await page.getByRole("checkbox", { name: "MinerU のライセンスの条件を確認した" }).check();
  await expect(mineruBackend).toBeEnabled();
  await mineruBackend.focus();
  await expect(mineruBackend).toBeFocused();
  // 本物のラジオは Space（または矢印キー）で選ぶ（#469）。
  await page.keyboard.press("Space");
  await expect(mineruBackend).toBeChecked();

  await expect(page.getByText("未保存の変更があります。")).toBeVisible();

  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect(page.getByText("文書解析の設定を保存しました。")).toBeVisible();
  expect(savedPayload).toEqual({
    adapter_backend: "mineru",
    docling_enabled: false,
    unstructured_enabled: false,
    mineru_enabled: true,
    mineru_license_acknowledged: true,
    dots_ocr_enabled: false,
    structured_records_jsonl_enabled: false,
    xml_tree_json_enabled: false,
    // 接続の並びも使用エンジンのカードと同じ（Dots.OCR → MinerU。#3359）。
    connections: ["dots_ocr", "mineru"].map((backend) => {
      const connection = defaultConnections().find((item) => item.backend === backend)!;
      return {
        backend: connection.backend,
        endpoint: connection.endpoint,
        ...(connection.backend === "mineru" ? {} : { model: connection.model }),
      };
    }),
  });
  await expectNoHorizontalOverflow(page);
});

test("XML ツリー JSON は使用エンジンとして選んで保存する（issue 2042）", async ({ page }) => {
  let savedPayload: Record<string, unknown> | null = null;
  await page.route("**/api/settings/parser-adapters", async (route) => {
    if (route.request().method() === "PATCH") {
      savedPayload = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        json: parserAdapterEnvelope({
          adapter_backend: "xml_tree_json",
          effective_order: ["xml_tree_json"],
          config_source: "runtime",
          adapters: [
            { ...disabledAdapter("docling"), enabled: true, status: "ignored" },
            disabledAdapter("unstructured"),
            { ...disabledAdapter("xml_tree_json"), enabled: true, selected: true, status: "active" },
            disabledAdapter("mineru"),
            disabledAdapter("dots_ocr"),
          ],
        }),
      });
      return;
    }
    await route.fulfill({
      json: parserAdapterEnvelope({
        adapter_backend: "docling",
        effective_order: ["docling"],
        config_source: "runtime",
        adapters: [
          { ...disabledAdapter("docling"), enabled: true, selected: true, status: "active" },
          disabledAdapter("unstructured"),
          disabledAdapter("xml_tree_json"),
          disabledAdapter("mineru"),
          disabledAdapter("dots_ocr"),
        ],
      }),
    });
  });

  await page.goto("/settings/parser-adapters");

  const xmlTreeJson = page.getByRole("radio", { name: /XML ツリー JSON/ });
  await expect(xmlTreeJson).not.toBeChecked();
  await xmlTreeJson.check();
  await expect(xmlTreeJson).toBeChecked();
  await expect(page.getByText("未保存の変更があります。")).toBeVisible();

  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect(page.getByText("文書解析の設定を保存しました。")).toBeVisible();
  expect(savedPayload).toMatchObject({ adapter_backend: "xml_tree_json", xml_tree_json_enabled: true });
  await expect(xmlTreeJson).toBeChecked();
  await expectNoHorizontalOverflow(page);
});

test("構造化レコード JSONL は使用エンジンとして選んで保存する（issue 2457）", async ({ page }) => {
  let savedPayload: Record<string, unknown> | null = null;
  const adapters = (selected: "docling" | "structured_records_jsonl") => [
    {
      ...disabledAdapter("docling"),
      enabled: true,
      selected: selected === "docling",
      status: selected === "docling" ? "active" : "ignored",
    },
    disabledAdapter("unstructured"),
    selected === "structured_records_jsonl"
      ? {
          ...disabledAdapter("structured_records_jsonl"),
          enabled: true,
          selected: true,
          status: "active",
        }
      : disabledAdapter("structured_records_jsonl"),
    disabledAdapter("xml_tree_json"),
    disabledAdapter("mineru"),
    disabledAdapter("dots_ocr"),
  ];
  await page.route("**/api/settings/parser-adapters", async (route) => {
    if (route.request().method() === "PATCH") {
      savedPayload = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        json: parserAdapterEnvelope({
          adapter_backend: "structured_records_jsonl",
          effective_order: ["structured_records_jsonl"],
          config_source: "runtime",
          adapters: adapters("structured_records_jsonl"),
        }),
      });
      return;
    }
    await route.fulfill({
      json: parserAdapterEnvelope({
        adapter_backend: "docling",
        effective_order: ["docling"],
        config_source: "runtime",
        adapters: adapters("docling"),
      }),
    });
  });

  await page.goto("/settings/parser-adapters");

  const structuredRecords = page.getByRole("radio", { name: /構造化レコード JSONL/ });
  await expect(structuredRecords).not.toBeChecked();
  await structuredRecords.check();
  await expect(structuredRecords).toBeChecked();
  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect(page.getByText("文書解析の設定を保存しました。")).toBeVisible();
  expect(savedPayload).toMatchObject({
    adapter_backend: "structured_records_jsonl",
    structured_records_jsonl_enabled: true,
  });
  await expect(structuredRecords).toBeChecked();
  await expectNoHorizontalOverflow(page);
});

test("外部 GPU 接続は検証・秘密鍵保持・明示削除ができる", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "production-ready-rag.ui",
      JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
    );
  });
  const payloads: Array<Record<string, unknown>> = [];
  // 保存で endpoint・model などが変わるので、defaultConnections の literal の型（as const）を広げる。
  let connections: Array<
    Omit<ReturnType<typeof defaultConnections>[number], "endpoint" | "model" | "api_key_configured" | "configured"> & {
      endpoint: string;
      model: string | null;
      api_key_configured: boolean;
      configured: boolean;
    }
  > = defaultConnections().map((connection) => ({ ...connection }));
  await page.route("**/api/settings/parser-adapters", async (route) => {
    if (route.request().method() === "PATCH") {
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      payloads.push(payload);
      const updates = payload.connections as Array<Record<string, unknown>>;
      connections = connections.map((connection) => {
        const update = updates.find((item) => item.backend === connection.backend);
        if (!update) return connection;
        return {
          ...connection,
          endpoint: String(update.endpoint ?? connection.endpoint),
          model:
            connection.backend === "mineru"
              ? null
              : String(update.model ?? connection.model ?? ""),
          api_key_configured: update.clear_api_key ? false : connection.api_key_configured,
          configured: Boolean(update.endpoint),
        };
      });
    }
    await route.fulfill({
      json: parserAdapterEnvelope({
        adapter_backend: "dots_ocr",
        effective_order: ["dots_ocr"],
        config_source: "runtime",
        connections,
        adapters: [
          disabledAdapter("docling"),
          disabledAdapter("unstructured"),
          disabledAdapter("mineru"),
          { ...disabledAdapter("dots_ocr"), enabled: true, selected: true, status: "active" },
        ],
      }),
    });
  });
  let statusChecks = 0;
  await page.route("**/api/settings/parser-adapters/dots_ocr/status", async (route) => {
    statusChecks += 1;
    await route.fulfill({
      json: {
        data: {
          backend: "dots_ocr",
          status: statusChecks === 1 ? "available" : "model_missing",
          version: null,
          warning_code: statusChecks === 1 ? null : "external_parser_model_missing",
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  await page.goto("/settings/parser-adapters");
  const endpoint = page.locator("#external-parser-dots_ocr-endpoint");
  const model = page.locator("#external-parser-dots_ocr-model");
  const card = endpoint.locator(
    "xpath=ancestor::*[.//button[normalize-space()='接続を確認']][1]"
  );

  await card.getByRole("button", { name: "接続を確認" }).click();
  await expect(card.getByText("接続できました。")).toBeVisible();
  // 結果はシステム設定と同じ結果パネル（所要時間・「詳細」）。成功なら「詳細」は閉じる（messaging.md §10。#723）。
  const result = card.getByTestId("external-parser-test-result-dots_ocr");
  await expect(result).toHaveAttribute("data-tone", "success");
  await expect(result.getByText(/所要時間: \d+ ms/)).toBeVisible();
  await expect(result.locator("details")).not.toHaveAttribute("open", "");

  await endpoint.fill("ftp://invalid.example.com");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(
    page.getByText(
      "http または https の Endpoint を入力してください。認証情報、query、fragment は URL に含められません。"
    )
  ).toBeVisible();
  await expect(endpoint).toBeFocused();
  expect(payloads).toHaveLength(0);

  // `http:foo` は URL としては読めるが、backend は `http(s)://host` の形だけを受け付ける（#976）。
  await endpoint.fill("http:dots.example.com");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(
    page.getByText(
      "http または https の Endpoint を入力してください。認証情報、query、fragment は URL に含められません。"
    )
  ).toBeVisible();
  expect(payloads).toHaveLength(0);
  // 欄の上限は backend と同じ（超える入力で英語の 422 を出さない）。
  await expect(endpoint).toHaveAttribute("maxlength", "2048");
  await expect(model).toHaveAttribute("maxlength", "512");

  await endpoint.fill("https://dots-new.example.com/v1");
  await model.fill("served-dots");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("文書解析の設定を保存しました。")).toBeVisible();
  const firstConnections = payloads[0].connections as Array<Record<string, unknown>>;
  const firstDots = firstConnections.find((item) => item.backend === "dots_ocr");
  expect(firstDots).toEqual({
    backend: "dots_ocr",
    endpoint: "https://dots-new.example.com/v1",
    model: "served-dots",
  });
  await expect(card.getByText("接続できました。")).toHaveCount(0);

  await card.getByRole("button", { name: "接続を確認" }).click();
  await expect(
    page.getByText("設定した Model が接続先にありません。Model 名を確認してください。")
  ).toBeVisible();
  // 失敗では「詳細」を開き、警告コードなどの技術的な値をそこに出す。
  await expect(result).toHaveAttribute("data-tone", "danger");
  await expect(result.locator("details")).toHaveAttribute("open", "");
  await expect(result.getByText("external_parser_model_missing")).toBeVisible();

  await card.getByLabel("保存済み API key を削除").check();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  const secondConnections = payloads[1].connections as Array<Record<string, unknown>>;
  expect(secondConnections.find((item) => item.backend === "dots_ocr")).toEqual({
    backend: "dots_ocr",
    endpoint: "https://dots-new.example.com/v1",
    model: "served-dots",
    clear_api_key: true,
  });
  await expectNoHorizontalOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`MinerU のライセンスの主な条件と公式のリンクを出し、確認するまで選べない (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockParserAdapters(page, {}, { mineruAvailability: "available" });
    await page.goto("/settings/parser-adapters");

    const notice = page.getByTestId("mineru-license-notice");
    await expect(notice).toContainText("MinerU のライセンス（MinerU Open Source License）の主な条件");
    await expect(notice).toContainText("月間アクティブユーザーが 1 億を超えるか、月の売上が 2,000 万米ドルを超える");
    await expect(notice).toContainText("MinerU を使っていることを明示します");
    await expect(notice).toContainText("通知なしに自動で終わります");
    await expect(notice).toContainText("MinerU2.5-Pro-2605-1.2B（Apache-2.0）");
    await expect(notice).toContainText("利用の前に、ライセンスの全文を確認してください");
    await expect(notice).toContainText("法的な助言ではありません");
    await expect(notice.getByRole("link", { name: /MinerU の公式リポジトリ/ })).toHaveAttribute(
      "href",
      "https://github.com/opendatalab/MinerU"
    );
    const licenseLink = notice.getByRole("link", { name: /ライセンスの全文/ });
    await expect(licenseLink).toHaveAttribute(
      "href",
      "https://github.com/opendatalab/MinerU/blob/master/LICENSE.md"
    );
    await expect(licenseLink).toHaveAttribute("target", "_blank");
    // 確認するまで MinerU は選べない（#3359。backend も 422 で拒否する）。
    const mineruBackend = page.getByRole("radio", { name: /MinerU/ });
    await expect(mineruBackend).toBeDisabled();
    await expect(page.locator('label[for="settings-parser-backend-mineru"]')).toContainText(
      "先に下の「MinerU のライセンスの条件を確認した」を確かめてください"
    );
    const acknowledge = notice.getByRole("checkbox", { name: "MinerU のライセンスの条件を確認した" });
    await expect(acknowledge).not.toBeChecked();
    await acknowledge.check();
    await expect(mineruBackend).toBeEnabled();
    await expect(page.getByText("未保存の変更があります。")).toBeVisible();
    await expectNoPageOverflow(page);
  });

  test(`MinerU のライセンスの確認はシステム管理者だけができる (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    // 文書解析の設定の権限はあるが system_admin ではない利用者（#3359）。
    await mockAuthUser(page, { permissions: ["menu.settings_parser_adapters"] });
    await mockParserAdapters(page, {}, { mineruAvailability: "available" });
    await page.goto("/settings/parser-adapters");

    const notice = page.getByTestId("mineru-license-notice");
    await expect(notice).toContainText("MinerU Open Source License");
    const acknowledge = notice.getByRole("checkbox", { name: "MinerU のライセンスの条件を確認した" });
    await expect(acknowledge).not.toBeChecked();
    await expect(acknowledge).toBeDisabled();
    await expect(notice).toContainText(
      "MinerU のライセンスの条件の確認と取り消しは、システム管理者だけができます。"
    );
    await expect(page.getByRole("radio", { name: /MinerU/ })).toBeDisabled();
    await expectNoPageOverflow(page);
  });

  test(`MinerU を使用エンジンにしている間は確認を外せない (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.route("**/api/settings/parser-adapters", (route) =>
      route.fulfill({
        json: parserAdapterEnvelope({
          adapter_backend: "mineru",
          effective_order: ["mineru"],
          config_source: "runtime",
          adapters: [
            disabledAdapter("docling"),
            disabledAdapter("unstructured"),
            {
              ...disabledAdapter("mineru"),
              availability: "available",
              enabled: true,
              selected: true,
              status: "active",
            },
            disabledAdapter("dots_ocr"),
          ],
          mineru_license: {
            acknowledged: true,
            acknowledged_by: "管理者 A",
            acknowledged_at: "2026-10-26T00:00:00+00:00",
          },
        }),
      })
    );
    await page.goto("/settings/parser-adapters");

    const notice = page.getByTestId("mineru-license-notice");
    const acknowledge = notice.getByRole("checkbox", { name: "MinerU のライセンスの条件を確認した" });
    await expect(acknowledge).toBeChecked();
    await expect(acknowledge).toBeDisabled();
    await expect(notice).toContainText("MinerU を使用エンジンにしている間は、確認を外せません");
    await expect(notice).toContainText("確認済み: 管理者 A");
    await expectNoPageOverflow(page);
  });
}

async function mockParserAdapters(
  page: Page,
  extra: object = {},
  // MinerU のライセンスの確認の画面を確かめるときは、準備中の判定を外す（backend の
  // mineru_selectable と同じ。#3359）。
  options: { mineruAvailability?: "available" | "roadmap" } = {}
) {
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
            {
              backend: "structured_records_jsonl",
              package_name: "rag_parser_core",
              import_name: "rag_parser_core",
              distribution_name: "rag-parser-core",
              install_package: "rag-parser-core",
              enabled: false,
              selected: false,
              installed: true,
              status: "available",
              version: null,
              warning_code: null,
            },
            {
              backend: "xml_tree_json",
              package_name: "rag_parser_core",
              import_name: "rag_parser_core",
              distribution_name: "rag-parser-core",
              install_package: "rag-parser-core",
              enabled: false,
              selected: false,
              installed: true,
              status: "available",
              version: null,
              warning_code: null,
            },
            {
              backend: "mineru",
              availability: options.mineruAvailability ?? "roadmap",
              package_name: "external_api",
              import_name: "external_api",
              distribution_name: "mineru_v1",
              install_package: "外部 MinerU API",
              enabled: false,
              selected: false,
              installed: false,
              status: "disabled",
              version: null,
              warning_code: null,
            },
            {
              backend: "dots_ocr",
              package_name: "external_api",
              import_name: "external_api",
              distribution_name: "openai_chat_completions",
              install_package: "外部 Dots.OCR API",
              enabled: false,
              selected: false,
              installed: false,
              status: "disabled",
              version: null,
              warning_code: null,
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
      connections: defaultConnections(),
      service_backends: [
        {
          backend: "oci_document_understanding",
          // 準備中（ロードマップ。#3374）。backend は選択中・未設定の警告にしない。
          availability: "roadmap",
          selected: false,
          configured: false,
          warning_code: null,
        },
      ],
      capabilities: [
        { backend: "docling", modalities: ["pdf", "image"], extensions: [".pdf", ".png"] },
        // Unstructured の代表の拡張子（rag_parser_core.capabilities の一部。#3351 で .rst / .org を足し、
        // pandoc・pi-heif を入れないため HEIC / EPUB / ODT / RTF は出さない）。
        {
          backend: "unstructured",
          modalities: ["pdf", "image", "text", "html", "email", "office"],
          extensions: [".pdf", ".txt", ".md", ".rst", ".org", ".csv", ".html", ".eml", ".png", ".docx"],
        },
        { backend: "structured_records_jsonl", modalities: [], extensions: [".jsonl"] },
        { backend: "xml_tree_json", modalities: [], extensions: [".json"] },
      ],
      backend_source_kind_matrix: {
        evidence_source: "runtime_routes",
        required_source_kinds: ["pdf", "image", "office", "html", "email", "text", "unknown"],
        covered_source_kinds: ["pdf", "image", "office", "html", "email", "text", "unknown"],
        missing_source_kinds: [],
        backend_source_kinds: {
          docling: ["pdf", "image", "office", "html"],
          dots_ocr: ["pdf", "image"],
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
      candidate_order: ["docling", "unstructured", "mineru", "dots_ocr"],
      attempted_order: ["docling", "unstructured"],
      active_order: ["docling"],
      selected_backend: "docling",
      reason_codes: ["selected_adapter_supported_for_source", "active_adapter_available_for_source"],
      warning_codes: ["unstructured_adapter_package_missing"],
    },
    {
      source_kind: "image",
      candidate_order: ["unstructured", "docling", "dots_ocr", "mineru"],
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
    "parser-structured-records-jsonl": "running",
    "parser-xml-tree-json": "running",
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

async function mockExternalParserStatuses(page: Page) {
  await page.route("**/api/settings/parser-adapters/*/status", async (route) => {
    const backend = decodeURIComponent(
      route.request().url().match(/parser-adapters\/([^/]+)\/status/)?.[1] ?? ""
    );
    await route.fulfill({
      json: {
        data: {
          backend,
          status: "available",
          version: backend === "mineru" ? "3.4.0" : "served-model",
          warning_code: null,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

function defaultConnections() {
  return [
    {
      backend: "mineru",
      protocol: "mineru_v1",
      endpoint: "",
      model: null,
      api_key_configured: false,
      configured: false,
    },
    {
      backend: "dots_ocr",
      protocol: "openai_chat_completions",
      endpoint: "https://dots.example.com/v1",
      model: "rednote-hilab/dots.mocr",
      api_key_configured: true,
      configured: true,
    },
  ] as const;
}

function serviceProfileForId(serviceId: string) {
  if (serviceId.includes("oci")) return "oci";
  return "cpu";
}

function disabledAdapter(
  backend:
    | "docling"
    | "unstructured"
    | "structured_records_jsonl"
    | "xml_tree_json"
    | "mineru"
    | "dots_ocr"
) {
  const external = backend === "mineru" || backend === "dots_ocr";
  return {
    backend,
    // MinerU は準備中（ロードマップ。#3359）。backend の GET と同じく roadmap を返す。
    availability: backend === "mineru" ? "roadmap" : "available",
    package_name: external ? "external_api" : backend,
    import_name: external ? "external_api" : backend,
    distribution_name: null,
    install_package:
      backend === "unstructured"
        ? "unstructured[all-docs]==0.18.32"
        : backend === "xml_tree_json" || backend === "structured_records_jsonl"
          ? "rag-parser-core"
          : backend === "mineru"
          ? "外部 MinerU API"
          : backend === "dots_ocr"
            ? "外部 Dots.OCR API"
            : "docling==2.103.0",
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
    await mockExternalParserStatuses(page);
    const saved = {
      adapter_backend: "docling",
      effective_order: ["docling"],
      config_source: "runtime",
      adapters: [
        { ...disabledAdapter("docling"), enabled: true, selected: true, installed: true, status: "active" },
        disabledAdapter("unstructured"),
        disabledAdapter("mineru"),
        disabledAdapter("dots_ocr"),
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
            adapter_backend: "dots_ocr",
            effective_order: ["dots_ocr"],
            adapters: [
              { ...disabledAdapter("docling"), enabled: true, installed: true, status: "active" },
              disabledAdapter("unstructured"),
              disabledAdapter("mineru"),
              { ...disabledAdapter("dots_ocr"), enabled: true, selected: true, status: "active" },
            ],
          }),
        });
        return;
      }
      await route.fulfill({ json: parserAdapterEnvelope(saved) });
    });
    await page.goto("/settings/parser-adapters");

    const dots = page.getByRole("radio", { name: /Dots\.OCR/ });
    await dots.check();
    const apiKey = page.locator("#external-parser-dots_ocr-api-key");
    await apiKey.fill("new-secret");
    const actions = page.getByRole("group", { name: "文書解析の設定の操作" });
    const saveButton = actions.getByRole("button", { name: "保存", exact: true });
    await saveButton.click();

    // 失敗は操作の行に出し、選んだエンジン・入力した API key・保存のボタンを残す。
    await expect(actions).toContainText("設定を共有永続化ファイルへ保存できませんでした。");
    await expect(dots).toBeChecked();
    await expect(apiKey).toHaveValue("new-secret");
    await expect(saveButton).toBeEnabled();
    await expect(actions.getByRole("button", { name: "変更を破棄" })).toBeEnabled();
    await expectNoHorizontalOverflow(page);

    // そのまま再保存できる。
    await saveButton.click();
    await expect(page.getByText("文書解析の設定を保存しました。")).toBeVisible();
    await expect(dots).toBeChecked();
    await expect(apiKey).toHaveValue("");
    await expect(saveButton).toBeDisabled();
    expect(patchCount).toBe(2);
  });
}


test("文書解析の各プロンプトは機能の位置で共通の編集操作を使う", { tag: "@responsive" }, async ({ page }) => {
  await mockParserAdaptersWithPostParse(page, { vision_enabled: false, field_extraction_enabled: false, navigation_summary_enabled: false });
  await mockExtractionFields(page);
  const requests = await mockFeaturePrompts(page);
  await page.goto("/settings/parser-adapters");
  const dotsCard = page.getByTestId("external-parser-connection-dots_ocr");
  const prompt = dotsCard.locator("summary").filter({ hasText: "Dots.OCR の読み取りプロンプト" });
  await expect(prompt).toHaveCount(1);
  await expect(page.locator("summary").filter({hasText: "Dots.OCR の読み取りプロンプト"})).toHaveCount(1);
  const position = await prompt.boundingBox();
  const connection = await dotsCard.getByRole("button", { name: "接続を確認", exact: true }).boundingBox();
  const minimumGap = await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize));
  expect(position!.y - connection!.y - connection!.height).toBeGreaterThanOrEqual(minimumGap - 1);
  await expect(page.getByText("GPU 解析エンジンは外部 API を直接呼びます。", {exact: false})).toHaveCount(0);

  for (const [key, title] of [
    ["external_ocr", "Dots.OCR の読み取りプロンプト"],
    ["heading_levels", "見出しの階層の推定プロンプト"],
    ["field_extraction", "メタデータ/項目抽出プロンプト"],
    ["navigation_summary", "章節の要約プロンプト"],
    ["raptor_summary_system", "RAPTOR の階層の要約のシステムプロンプト"],
  ]) {
    await verifyFeaturePrompt(page, key, title);
  }
  expect(requests.map((item) => item.method)).toEqual(["PUT", "DELETE", "PUT", "DELETE", "PUT", "DELETE", "PUT", "DELETE", "PUT", "DELETE"]);
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
