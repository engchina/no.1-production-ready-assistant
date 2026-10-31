import { expect, test, type Page } from "./fixtures/test";
import { expectNoPageOverflow, mockLocalAuth } from "./_helpers";

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760, collapseSidebar: false },
  { name: "mobile", width: 375, height: 812, collapseSidebar: true },
]) {
  test(`前処理設定はファイル準備方式を表示する (${viewport.name})`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapseSidebar) {
      await page.addInitScript(() => {
        window.localStorage.setItem(
          "production-ready-rag.ui",
          JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
        );
      });
    }
    await mockPreprocessSettings(page);

    await page.goto("/settings/preprocess");

    await expect(page.getByRole("heading", { name: "ファイル準備方式" })).toBeVisible();
    await expect(page.getByRole("radio", { name: /原本をそのまま解析/ })).toBeVisible();
    // text_normalize は廃止(in-process 正規化撤去)。radio として出ないことを確認する。
    await expect(page.getByRole("radio", { name: /文字コード→UTF-8/ })).toHaveCount(0);
    await expect(page.getByRole("radio", { name: /Word・PowerPoint の文書.*PDF にしてから解析/ })).toBeVisible();
    // CSV→JSONL・Excel→JSONL の順で、使う解析エンジンも説明に出す（#2457）。
    await expect(page.getByRole("radio", { name: /CSV→JSONL.*表頭を除くデータの 1 記録/ })).toBeVisible();
    await expect(
      page.getByRole("radio", { name: /Excel→JSONL.*Excel ファイル\(\.xls・\.xlsx・マクロ付きの \.xlsm\)/ })
    ).toBeVisible();
    await expect(page.getByRole("radio", { name: /CSV→JSONL.*Docling・Unstructured/ })).toBeVisible();
    await expect(page.getByRole("radio", { name: /^Excel→JSONL/ })).toHaveAccessibleName(/保存済みの計算結果/);
    await expect(page.getByRole("radio", { name: /^Word→JSONL/ })).toHaveAccessibleName(/ページ番号は付けません/);
    // Word→JSONL は Excel→JSONL の直後（#2457）。
    await expect(
      page.getByRole("radio", { name: /Word→JSONL.*見出しの下の段落・リスト・表のまとまり/ })
    ).toBeVisible();
    const values = await page
      .getByRole("radio")
      .evaluateAll((items) => items.map((item) => (item as HTMLInputElement).value));
    expect(values.indexOf("word_to_jsonl")).toBe(values.indexOf("excel_to_jsonl") + 1);
    await page.getByRole("radio", { name: /^Word→JSONL/ }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("preprocess-word-to-jsonl.png") });
    // 名前と説明で、どの文書に使うかが分かる(#563)。実装の言葉(ラスタライズ・canonical な中間物 等)は出さない(保存先の値 artifacts/canonical は除く)。
    // スキャン画像の補正と URL→Markdown（#2042）、PDF を画像として読み直す（#3347）、PIIマスク（#3358）は削除した。
    await expect(
      page.getByRole("radio", { name: /スキャン画像の補正|URL→Markdown|PDF を画像として読み直す|PIIマスク/ })
    ).toHaveCount(0);
    await expect(page.getByRole("radio")).toHaveCount(5);
    const main = page.getByRole("main");
    for (const jargon of ["ラスタライズ", "VLM/OCR 経路", "canonical な", "派生系譜", "溯源", "in-process"]) {
      await expect(main).not.toContainText(jargon);
    }
    await expect(page.getByRole("radio", { name: /自動/ })).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
  });
}

test("前処理設定取得に失敗したら再試行できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await page.route("**/api/settings/preprocess", async (route) => {
    await route.fulfill({
      status: 503,
      json: {
        data: null,
        error_messages: ["前処理設定を取得できませんでした。"],
        warning_messages: [],
      },
    });
  });

  await page.goto("/settings/preprocess");

  await expect(page.getByRole("alert")).toContainText("前処理設定を取得できませんでした。");
  await expect(page.getByRole("button", { name: "再試行" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("前処理設定はファイル準備方式を保存できる", async ({ page }) => {
  let savedContentType: string | undefined;
  let savedPayload: unknown = null;
  await page.route("**/api/settings/preprocess", async (route) => {
    if (route.request().method() === "PATCH") {
      savedPayload = route.request().postDataJSON();
      savedContentType = route.request().headers()["content-type"];
      await route.fulfill({ json: preprocessEnvelope({ profile: "office_to_pdf" }) });
      return;
    }
    await route.fulfill({ json: preprocessEnvelope() });
  });

  await page.goto("/settings/preprocess");

  const office = page.getByRole("radio", { name: /Word・PowerPoint の文書.*PDF にしてから解析/ });
  await office.click();
  await expect(office).toBeChecked();
  await expect(page.getByText("未保存の変更があります。")).toBeVisible();

  await page.getByRole("button", { name: "保存" }).click();

  await expect(page.getByText("ファイル準備の設定を保存しました。")).toBeVisible();
  await expect(page.getByText("未保存の変更があります。")).toHaveCount(0);
  expect(savedPayload).toEqual({ profile: "office_to_pdf" });
  expect(savedContentType).toBe("application/json");
  await expectNoHorizontalOverflow(page);
});

test("ファイル準備方式のカードは矢印キーで選択が移り、Tab ではグループで 1 回だけ止まる（#469）", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await page.route("**/api/settings/preprocess", (route) =>
    route.fulfill({ json: preprocessEnvelope() })
  );
  await page.goto("/settings/preprocess");

  const passthrough = page.getByRole("radio", { name: /原本をそのまま解析/ });
  const office = page.getByRole("radio", { name: /Word・PowerPoint の文書.*PDF にしてから解析/ });
  await expect(passthrough).toBeChecked();
  await passthrough.focus();
  await page.keyboard.press("ArrowRight");
  await expect(office).toBeChecked();
  await expect(office).toBeFocused();
  await expect(page.getByText("未保存の変更があります。")).toBeVisible();

  // Tab はグループの次の操作へ進む（カードを 1 枚ずつ通らない）。
  await page.keyboard.press("Tab");
  await expect
    .poll(() => page.evaluate(() => (document.activeElement as HTMLInputElement | null)?.type))
    .not.toBe("radio");
});

type PreprocessOverrides = { profile?: string; service_enabled?: boolean };

function preprocessEnvelope(overrides: PreprocessOverrides = {}) {
  const profile = overrides.profile ?? "passthrough";
  const serviceEnabled = overrides.service_enabled ?? false;
  const specs: {
    name: string;
    origin: string;
    recommended_for: string[];
    in_process: boolean;
    requires_service: boolean;
  }[] = [
    { name: "passthrough", origin: "baseline_no_conversion", recommended_for: ["any"], in_process: true, requires_service: false },
    { name: "office_to_pdf", origin: "libreoffice_headless", recommended_for: ["office"], in_process: false, requires_service: true },
    { name: "csv_to_jsonl", origin: "no1_csv2json_records", recommended_for: ["csv"], in_process: false, requires_service: true },
    { name: "excel_to_jsonl", origin: "no1_excel2json_records", recommended_for: ["excel"], in_process: false, requires_service: true },
    { name: "word_to_jsonl", origin: "libreoffice_python_docx_records", recommended_for: ["word"], in_process: false, requires_service: true },
  ];
  return {
    data: {
      profile,
      service_enabled: serviceEnabled,
      service_url: "http://preprocess-office-to-pdf:8000",
      canonical_artifact_prefix: "artifacts/canonical",
      profiles: specs.map((spec) => ({
        ...spec,
        selected: spec.name === profile,
        available: spec.in_process || serviceEnabled,
      })),
      config_source: "runtime",
    },
    error_messages: [],
    warning_messages: [],
  };
}

async function mockPreprocessSettings(page: Page) {
  await page.route("**/api/settings/preprocess", async (route) => {
    await route.fulfill({ json: preprocessEnvelope() });
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  await expectNoPageOverflow(page);
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`ファイル準備の設定は保存に失敗しても選んだ方式を残し、操作の行に失敗を出す（#956, ${viewport.name}）`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    let patchCount = 0;
    await page.route("**/api/settings/preprocess", async (route) => {
      if (route.request().method() === "PATCH") {
        patchCount += 1;
        if (patchCount === 1) {
          await route.fulfill({
            status: 500,
            json: {
              data: null,
              error_messages: ["ファイル準備設定を backend/.env へ保存できませんでした。"],
              warning_messages: [],
            },
          });
          return;
        }
        await route.fulfill({ json: preprocessEnvelope({ profile: "office_to_pdf" }) });
        return;
      }
      await route.fulfill({ json: preprocessEnvelope() });
    });
    await page.goto("/settings/preprocess");

    const office = page.getByRole("radio", { name: /Word・PowerPoint の文書.*PDF にしてから解析/ });
    await office.click();
    const actions = page.getByRole("group", { name: "ファイル準備の設定の操作" });
    const saveButton = actions.getByRole("button", { name: "保存" });
    await saveButton.click();

    // 失敗は操作の行に出し、選んだ方式・未保存の状態・保存のボタンを残す。
    await expect(actions).toContainText("ファイル準備設定を backend/.env へ保存できませんでした。");
    await expect(office).toBeChecked();
    await expect(saveButton).toBeEnabled();
    await expect(actions.getByRole("button", { name: "変更を破棄" })).toBeEnabled();
    await expectNoHorizontalOverflow(page);

    // そのまま再保存できる。
    await saveButton.click();
    await expect(page.getByText("ファイル準備の設定を保存しました。")).toBeVisible();
    await expect(office).toBeChecked();
    await expect(saveButton).toBeDisabled();
    expect(patchCount).toBe(2);
  });
}
