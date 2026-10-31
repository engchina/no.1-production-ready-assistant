import { expect, test, type Locator, type Page } from "./fixtures/test";
import { expectMainScrollEndsAtContent, expectNoPageOverflow, mockLocalAuth } from "./_helpers";

const SECONDARY_CONNECTION = {
  connection_id: "secondary",
  endpoint: "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1",
  project_ocid: "ocid1.generativeaiproject.oc1.us-chicago-1.secondary",
  api_key: "",
  has_api_key: true,
  clear_api_key: false,
};

type ModelSettingsOptions = { secondary?: boolean; textModel?: string };

function createModelSettings({
  secondary = false,
  textModel = "enterprise-llm",
}: ModelSettingsOptions = {}) {
  return {
    settings: {
      enterprise_ai: {
        connections: [
          {
            connection_id: "primary",
            endpoint: "",
            project_ocid: "ocid1.generativeaiproject.oc1.us-chicago-1.example",
            api_key: "",
            has_api_key: true,
            clear_api_key: false,
          },
          ...(secondary ? [SECONDARY_CONNECTION] : []),
        ],
        models: [
          {
            model_id: "enterprise-llm",
            display_name: "標準 LLM",
            vision_enabled: false,
            connection_id: "primary",
            max_output_tokens: 32768,
          },
          {
            model_id: "enterprise-vision",
            display_name: "Vision LLM",
            vision_enabled: true,
            connection_id: secondary ? "secondary" : "primary",
            max_output_tokens: 32768,
          },
        ],
        default_text_model_id: textModel,
        default_vision_model_id: "enterprise-vision",
        // 既定の判定モデル（#3574）。空 = 既定のテキストモデル。
        default_judge_model_id: "",
        api_path: "/responses",
        vlm_input_mode: "files_api",
        text_payload_template: '{"input":{"messages":"${messages}","params":"${parameters}"}}',
        vision_payload_template: '{"input":{"document":"${data_base64}"}}',
        text_response_path: "",
        vision_response_path: "",
        timeout_seconds: 60,
        max_retries: 3,
      },
      generative_ai: {
        embedding_model: "cohere.embed-v4.0",
        embedding_dim: 1536,
        rerank_model: "cohere.rerank-v4.0-fast",
      },
    },
    model_settings_file: "model-settings.json",
    source: "runtime",
    secret_source: "environment",
    legacy_secret_detected: false,
  };
}

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 720, collapseSidebar: false },
  { name: "mobile", width: 375, height: 812, collapseSidebar: true },
]) {
  test(`モデル設定は Enterprise AI の複数 LLM と既定のモデル（テキスト・画像対応・判定）を表示する (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapseSidebar) {
      await page.addInitScript(() => {
        window.localStorage.setItem(
          "production-ready-rag.ui",
          JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
        );
      });
    }

    await mockModelSettings(page);
    await page.goto("/settings/model");

    await expect(page.getByPlaceholder("https://…oraclecloud.com/openai/v1")).toBeVisible();
    await expect(page.getByLabel("Project OCID")).toHaveValue(
      "ocid1.generativeaiproject.oc1.us-chicago-1.example"
    );
    await expect(page.getByRole("textbox", { name: "API key" })).toBeVisible();
    await expect(page.getByText("保存済み", { exact: true }).first()).toBeVisible();
    await expect(page.getByLabel("モデル ID 1")).toHaveValue("enterprise-llm");
    await expect(page.getByLabel("表示名 1")).toHaveValue("標準 LLM");
    await expect(page.getByLabel("モデル ID 2")).toHaveValue("enterprise-vision");
    // 登録モデルの一覧は登録と画像入力（Vision）対応の指定だけ。既定の選択は一覧の下（#499）。
    await expect(page.getByRole("radio", { name: /既定/ })).toHaveCount(0);
    await expect(page.getByRole("switch", { name: "画像入力に対応 1" })).toHaveAttribute(
      "aria-checked",
      "false"
    );
    await expect(page.getByRole("switch", { name: "画像入力に対応 2" })).toHaveAttribute(
      "aria-checked",
      "true"
    );
    await expect(page.getByRole("heading", { name: "既定のモデル" })).toBeVisible();
    const textDefault = page.getByRole("combobox", { name: "既定のテキストモデル" });
    const visionDefault = page.getByRole("combobox", { name: "既定の画像対応モデル" });
    await expect(visionDefault).toContainText("Vision LLM");
    await expect(textDefault).toContainText("標準 LLM");
    // 並びはテキスト → Vision（desktop の 2 列でも 375px の縦積みでも同じ順）で、2 つとも必須（#566）。
    await expectTextBeforeVision(textDefault, visionDefault);
    for (const [field, id] of [
      [textDefault, "enterprise-default-text-model"],
      [visionDefault, "enterprise-default-vision-model"],
    ] as const) {
      await expect(field).toHaveAttribute("aria-required", "true");
      await expect(page.locator(`label[for="${id}"]`)).toContainText("必須");
    }
    // 既定の判定モデル（#3574）は Vision の後ろに置く任意の欄で、既定は「既定のテキストモデル」。
    const judgeDefault = page.getByRole("combobox", { name: "既定の判定モデル" });
    await expect(judgeDefault).toContainText("既定のテキストモデル");
    await expect(judgeDefault).not.toHaveAttribute("aria-required", "true");
    await expectTextBeforeVision(visionDefault, judgeDefault);
    await judgeDefault.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: test.info().outputPath(`model-settings-default-models-${viewport.name}.png`),
    });
    await expect(
      page.getByText("自分の回答は高く判定されやすいため、モデルの比較では別のモデルに固定してください。", {
        exact: false,
      })
    ).toBeVisible();
    // 共有画面（NL2SQL と同じ。#103）は詳細項目・構成状態・プレビューを表示しない。
    await expect(page.getByLabel("API パス")).toHaveCount(0);
    await expect(page.getByRole("combobox", { name: "VLM 入力方式" })).toHaveCount(0);
    await expect(page.getByLabel("最大リトライ回数")).toHaveCount(0);
    await expect(page.getByLabel("回答生成 payload template")).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "構成状態" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: ".env プレビュー" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "JSON プレビュー" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "運用メモ" })).toHaveCount(0);
    await expect(
      page.getByText("OpenAI-compatible gateway の Bearer 認証で使います。")
    ).toBeVisible();
    await expect(page.getByPlaceholder("業務 RAG 標準").first()).toBeVisible();

    await expectControlContentToBeVerticallyCentered(
      page.getByRole("switch", { name: "画像入力に対応 2" }),
      "span[aria-hidden='true']"
    );
    const enterpriseSave = page.getByRole("button", { name: "OCI Enterprise AI: 保存" });
    const modelsSave = page.getByRole("button", { name: "登録モデル: 保存" });
    const genaiSave = page.getByRole("button", { name: "OCI Generative AI: 保存" });
    const enterpriseTestButton = page.getByRole("button", { name: "enterprise-llm をテスト" });
    const embeddingTestButton = page.getByRole("button", {
      name: "cohere.embed-v4.0 をテスト",
    });
    const rerankTestButton = page.getByRole("button", {
      name: "cohere.rerank-v4.0-fast をテスト",
    });
    await expectActionInsideCard(page, "OCI Enterprise AI", enterpriseSave);
    await expectActionInsideCard(page, "登録モデル", modelsSave);
    await expectActionInsideCard(page, "登録モデル", enterpriseTestButton);
    await expectActionInsideCard(page, "OCI Generative AI", genaiSave);
    await expectActionInsideCard(page, "OCI Generative AI", embeddingTestButton);
    await expectActionInsideCard(page, "OCI Generative AI", rerankTestButton);

    for (const button of [
      enterpriseSave,
      modelsSave,
      genaiSave,
      enterpriseTestButton,
      embeddingTestButton,
      rerankTestButton,
      page.getByRole("button", { name: "追加", exact: true }),
      page.getByRole("button", { name: "モデルを削除 1" }),
    ]) {
      await expectControlContentToBeVerticallyCentered(button, "svg");
    }
    await expectNoPageOverflow(page);
    await expectMainScrollEndsAtContent(page);
  });
}

test("モデル設定は節ごとに保存し、画面にない項目は保存済みの値を送る", async ({ page }) => {
  let savedPayload: unknown;
  await mockModelSettings(page, (payload) => {
    savedPayload = payload;
  });
  await page.goto("/settings/model");

  await page.getByLabel("モデル ID 2").fill("enterprise-vision-v2");
  await page.getByPlaceholder("https://…oraclecloud.com/openai/v1").fill("https://unsaved.example");
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();

  await expect(page.getByText("登録モデルを保存しました。").first()).toBeVisible();
  expect(savedPayload).toMatchObject({
    enterprise_ai: {
      // 別の節（Enterprise AI 接続）の未保存の入力は送らない。
      connections: [{ connection_id: "primary", endpoint: "" }],
      api_path: "/responses",
      vlm_input_mode: "files_api",
      text_payload_template: '{"input":{"messages":"${messages}","params":"${parameters}"}}',
      models: [{ model_id: "enterprise-llm" }, { model_id: "enterprise-vision-v2" }],
      // 既定に選んだモデルの ID を書き換えると、既定も追従する（#499）。
      default_text_model_id: "enterprise-llm",
      default_vision_model_id: "enterprise-vision-v2",
    },
  });
  // 保存していない節の入力は画面に残る。
  await expect(
    page.getByPlaceholder("https://…oraclecloud.com/openai/v1")
  ).toHaveValue("https://unsaved.example");
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 720 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`画面を開いた後にほかの製品が保存していたら、保存は競合を案内し、最新の設定を読み直せる (#1037, ${viewport.name})`, { tag: "@responsive" }, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    // サーバーの状態と版（revision）。PATCH は読み込んだ時点の版（base_revision）が違えば 409。
    let current = { ...createModelSettings(), revision: "rev-1" };
    const patches: Array<{ base_revision?: string }> = [];
    await page.route("**/api/settings/model", async (route) => {
      const request = route.request();
      if (request.method() === "PATCH") {
        const { base_revision, ...settings } = request.postDataJSON();
        patches.push({ base_revision });
        if (base_revision !== current.revision) {
          await route.fulfill({
            status: 409,
            json: {
              detail:
                "モデル設定は、この画面を開いた後にほかの画面（別の製品を含む）で更新されました。最新の設定を読み込んでから、保存し直してください。",
            },
          });
          return;
        }
        current = { ...current, settings, revision: `${current.revision}+` };
      }
      await route.fulfill({ json: { data: current, error_messages: [], warning_messages: [] } });
    });
    await page.goto("/settings/model");
    const rerank = page.getByRole("textbox", { name: "リランクモデル ID" });
    await expect(rerank).toHaveValue("cohere.rerank-v4.0-fast");

    // ほかの製品がセカンダリ接続とリランクモデルを保存した。
    const other = createModelSettings({ secondary: true });
    other.settings.generative_ai.rerank_model = "rerank-from-nl2sql";
    current = { ...other, revision: "rev-2" };

    await rerank.fill("rerank-mine");
    await page.getByRole("button", { name: "OCI Generative AI: 保存" }).click();

    await expect(page.getByText("この画面を開いた後にほかの画面（別の製品を含む）で更新されました。")).toBeVisible();
    expect(patches).toEqual([{ base_revision: "rev-1" }]);
    await expect(rerank).toHaveValue("rerank-mine");
    const reload = page.getByRole("button", { name: "最新の設定を読み込む" });
    await expect(reload).toHaveCount(1);
    await expectActionInsideCard(page, "OCI Generative AI", reload);
    await expectNoPageOverflow(page);
    await page.screenshot({
      path: test.info().outputPath(`model-settings-conflict-${viewport.name}.png`),
      fullPage: true,
    });

    await reload.click();
    const dialog = page.getByRole("alertdialog", { name: "最新の設定を読み込みますか？" });
    await dialog.getByRole("button", { name: "読み込む" }).click();

    await expect(rerank).toHaveValue("rerank-from-nl2sql");
    await expect(page.getByRole("tab", { name: /セカンダリ接続/ })).toBeVisible();
    await expect(reload).toHaveCount(0);

    await rerank.fill("rerank-mine");
    await page.getByRole("button", { name: "OCI Generative AI: 保存" }).click();
    await expect(page.getByText("OCI Generative AI 設定を保存しました。").first()).toBeVisible();
    expect(patches.at(-1)).toEqual({ base_revision: "rev-2" });
  });
}

test("既定の判定モデルは「既定のテキストモデル」→「回答と同じモデル」→ 登録モデルの順に選べ、選んだ値を保存する (#3574)", async ({
  page,
}) => {
  const patches: Array<{ enterprise_ai: { default_judge_model_id?: string } }> = [];
  await mockModelSettings(page, (payload) =>
    patches.push(payload as { enterprise_ai: { default_judge_model_id?: string } })
  );
  await page.goto("/settings/model");

  const judge = page.getByRole("combobox", { name: "既定の判定モデル" });
  await expect(judge).toContainText("既定のテキストモデル");
  await judge.click();
  const options = page.getByRole("listbox", { name: "既定の判定モデル" }).getByRole("option");
  await expect(options).toHaveCount(4);
  await expect(options.nth(0)).toContainText("既定のテキストモデル");
  // 今の既定のテキストモデルを説明に出す。
  await expect(options.nth(0)).toContainText("標準 LLM");
  await expect(options.nth(1)).toContainText("回答と同じモデル");
  await expect(options.nth(2)).toContainText("標準 LLM");
  await expect(options.nth(3)).toContainText("Vision LLM");

  // 「回答と同じモデル」は明示の値（same_as_answer）で保存する。
  await options.nth(1).click();
  await expect(judge).toContainText("回答と同じモデル");
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  await expect(page.getByText("登録モデルを保存しました。").first()).toBeVisible();
  expect(patches.at(-1)?.enterprise_ai.default_judge_model_id).toBe("same_as_answer");

  // 登録モデルを選ぶと、そのモデルの ID を保存する。
  await judge.click();
  await page
    .getByRole("listbox", { name: "既定の判定モデル" })
    .getByRole("option", { name: /enterprise-llm/ })
    .click();
  await expect(judge).toContainText("標準 LLM");
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  await expect.poll(() => patches.at(-1)?.enterprise_ai.default_judge_model_id).toBe("enterprise-llm");
  await expectNoPageOverflow(page);
});

for (const scheme of ["light", "dark"] as const) {
  test(`既定のモデルは Vision 対応のモデルだけを選べ、不正な選択は保存前にフィールドで止める (${scheme})`, async ({
    page,
  }) => {
    await page.addInitScript((theme) => {
      window.localStorage.setItem(
        "production-ready-rag.ui",
        JSON.stringify({ state: { theme }, version: 0 })
      );
    }, scheme);
    const patches: unknown[] = [];
    await mockModelSettings(page, (payload) => patches.push(payload));
    await page.goto("/settings/model");
    await expect
      .poll(() => page.evaluate(() => document.documentElement.classList.contains("dark")))
      .toBe(scheme === "dark");

    const vision = page.getByRole("combobox", { name: "既定の画像対応モデル" });
    const text = page.getByRole("combobox", { name: "既定のテキストモデル" });
    await expect(vision).toContainText("Vision LLM");

    // 選択肢: Vision は Vision 対応のモデルだけ、テキストは全モデル（未選択に戻す選択肢はない。#566）。
    await vision.click();
    const visionOptions = page.getByRole("listbox", { name: "既定の画像対応モデル" }).getByRole("option");
    await expect(visionOptions).toHaveCount(1);
    await expect(visionOptions.first()).toContainText("Vision LLM");
    await page.keyboard.press("Escape");
    await text.click();
    const textListbox = page.getByRole("listbox", { name: "既定のテキストモデル" });
    await expect(textListbox.getByRole("option")).toHaveCount(2);
    await expect(textListbox.getByRole("option", { name: "既定の画像対応モデルを使う" })).toHaveCount(0);
    // テキストに Vision 対応のモデルを選んでもよい。
    await textListbox.getByRole("option", { name: /Vision LLM/ }).click();
    await expect(text).toContainText("Vision LLM");

    // 選んでいたモデルの Vision 対応を外すと、保存前にフィールドのエラーを出す。
    await page.getByRole("switch", { name: "画像入力に対応 2" }).click();
    await expect(vision).toHaveAttribute("aria-invalid", "true");
    await expect(
      page.getByText("画像入力に対応したモデルがありません。", { exact: false })
    ).toBeVisible();

    // 保存は送信せず、最初の不正な欄へフォーカスする。
    await page.getByRole("button", { name: "登録モデル: 保存" }).click();
    await expect(vision).toBeFocused();
    expect(patches).toHaveLength(0);

    // Vision 対応を戻して保存すると、既定のモデル 2 つを送る。
    await page.getByRole("switch", { name: "画像入力に対応 2" }).click();
    await expect(vision).toHaveAttribute("aria-invalid", "false");
    await page.getByRole("button", { name: "登録モデル: 保存" }).click();
    await expect(page.getByText("登録モデルを保存しました。").first()).toBeVisible();
    expect(patches[0]).toMatchObject({
      enterprise_ai: {
        default_text_model_id: "enterprise-vision",
        default_vision_model_id: "enterprise-vision",
      },
    });
    await expectNoPageOverflow(page);
  });
}

for (const scheme of ["light", "dark"] as const) {
  // desktop（1440px）と mobile（375px）は playwright.config の project で回す。
  test(`既定のテキストモデルが未設定なら欄の直下にエラーを出し、選ぶまで保存しない (#566, ${scheme})`, { tag: "@responsive" }, async ({
    page,
  }) => {
    await page.addInitScript((theme) => {
      window.localStorage.setItem(
        "production-ready-rag.ui",
        JSON.stringify({ state: { theme }, version: 0 })
      );
    }, scheme);
    const patches: unknown[] = [];
    // 以前の画面で「既定の画像対応モデルを使う」のまま保存した既存環境。
    await mockModelSettings(page, (payload) => patches.push(payload), { textModel: "" });
    await page.goto("/settings/model");
    await expect
      .poll(() => page.evaluate(() => document.documentElement.classList.contains("dark")))
      .toBe(scheme === "dark");

    const text = page.getByRole("combobox", { name: "既定のテキストモデル" });
    await expect(text).toContainText("モデルを選んでください");
    await expect(text).toHaveAttribute("aria-invalid", "true");
    await expectFieldErrorBelow(text, "既定のテキストモデルを選択してください。");

    // 保存は送信せず、テキストの欄へフォーカスする。
    await page.getByRole("button", { name: "登録モデル: 保存" }).click();
    await expect(text).toBeFocused();
    expect(patches).toHaveLength(0);

    await text.click();
    await page
      .getByRole("listbox", { name: "既定のテキストモデル" })
      .getByRole("option", { name: /標準 LLM/ })
      .click();
    await expect(text).toHaveAttribute("aria-invalid", "false");
    await expect(page.getByText("既定のテキストモデルを選択してください。")).toHaveCount(0);
    await page.getByRole("button", { name: "登録モデル: 保存" }).click();
    await expect(page.getByText("登録モデルを保存しました。").first()).toBeVisible();
    expect(patches[0]).toMatchObject({
      enterprise_ai: {
        default_text_model_id: "enterprise-llm",
        default_vision_model_id: "enterprise-vision",
      },
    });
    await expectNoPageOverflow(page);
  });
}

test("既定の画像対応モデルに選んだモデルを一覧から削除すると、選び直しを案内する", async ({ page }) => {
  const patches: unknown[] = [];
  await mockModelSettings(page, (payload) => patches.push(payload));
  await page.goto("/settings/model");

  await page.getByRole("button", { name: "モデルを削除 2" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "削除" }).click();

  const vision = page.getByRole("combobox", { name: "既定の画像対応モデル" });
  await expect(vision).toContainText("enterprise-vision");
  await expect(vision).toHaveAttribute("aria-invalid", "true");
  await expect(
    page.getByText("画像入力に対応したモデルがありません。", { exact: false })
  ).toBeVisible();
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  await expect(vision).toBeFocused();
  expect(patches).toHaveLength(0);
});

for (const scheme of ["light", "dark"] as const) {
  // desktop（1440px）と mobile（375px）は playwright.config の project で回す。
  test(`接続はプライマリ接続とセカンダリ接続のタブで切り替え、登録モデルごとに接続を選んで保存する (${scheme})`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    const viewportWidth = page.viewportSize()?.width ?? 1440;
    await page.addInitScript(
      ({ theme, collapsed }) => {
        window.localStorage.setItem(
          "production-ready-rag.ui",
          JSON.stringify({ state: { theme, sidebarCollapsed: collapsed }, version: 0 })
        );
      },
      { theme: scheme, collapsed: viewportWidth < 768 }
    );
    const patches: Array<{ enterprise_ai: Record<string, unknown> }> = [];
    await mockModelSettings(page, (payload) =>
      patches.push(payload as { enterprise_ai: Record<string, unknown> })
    );
    await page.goto("/settings/model");

    // 接続は OCI Enterprise AI のカードの中の共有の Tabs（#542）。表示名の欄はない。
    const tablist = page.getByRole("tablist", { name: "OCI Enterprise AI の接続" });
    const primaryTab = tablist.getByRole("tab", { name: "プライマリ接続" });
    const secondaryTab = tablist.getByRole("tab", { name: "セカンダリ接続" });
    const tertiaryTab = tablist.getByRole("tab", { name: "ターシャリ接続" });
    // タブの並びはプライマリ接続 → セカンダリ接続 → ターシャリ接続（#786）。
    await expect(tablist.getByRole("tab")).toHaveText(["プライマリ接続", "セカンダリ接続", "ターシャリ接続"]);
    await expect(primaryTab).toHaveAttribute("aria-selected", "true");
    await expect(page.getByLabel("表示名", { exact: true })).toHaveCount(0);
    await expect(page.locator("#enterprise-connection-name")).toHaveCount(0);
    const modelConnection1 = page.getByRole("combobox", { name: "モデル 1 の接続" });
    await expect(modelConnection1).toContainText("プライマリ接続");
    // 未設定のセカンダリ接続は、登録モデルの「接続」の選択肢に出さない。
    await modelConnection1.click();
    await expect(
      page.getByRole("listbox", { name: "モデル 1 の接続" }).getByRole("option")
    ).toHaveText(["プライマリ接続"]);
    await page.keyboard.press("Escape");

    // ← → / Home / End でタブを移る。未設定のセカンダリ接続は空の状態と「セカンダリ接続を設定」。
    await primaryTab.focus();
    await page.keyboard.press("ArrowRight");
    await expect(secondaryTab).toBeFocused();
    await expect(secondaryTab).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("enterprise-connection-secondary-empty")).toContainText(
      "セカンダリ接続は設定されていません。"
    );
    await page.keyboard.press("Home");
    await expect(primaryTab).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("End");
    await expect(tertiaryTab).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("enterprise-connection-tertiary-empty")).toContainText(
      "ターシャリ接続は設定されていません。"
    );
    await page.keyboard.press("ArrowLeft");
    await expect(secondaryTab).toHaveAttribute("aria-selected", "true");
    await page.screenshot({
      path: testInfo.outputPath(`connection-secondary-empty-${scheme}-${testInfo.project.name}.png`),
    });

    // 設定すると入力欄を出し、Endpoint URL・Project OCID・API key はすべて必須。
    await page.getByRole("button", { name: "セカンダリ接続を設定" }).click();
    const secondary = page.getByTestId("enterprise-connection-secondary");
    const secondaryEndpoint = page.locator("#enterprise-secondary-endpoint");
    const secondaryProject = page.locator("#enterprise-secondary-project-ocid");
    const secondaryApiKey = page.locator("#enterprise-secondary-api-key");
    await expect(secondaryEndpoint).toBeFocused();
    for (const field of [secondaryEndpoint, secondaryProject, secondaryApiKey]) {
      await expect(field).toHaveAttribute("aria-required", "true");
    }
    await expect(page.getByTestId("enterprise-connection-tab-secondary-unsaved")).toHaveText("未保存");

    // プライマリ接続のタブから保存しても、エラーのあるセカンダリ接続のタブへ移り、最初の欄へフォーカスする。
    await primaryTab.click();
    await page.getByRole("button", { name: "OCI Enterprise AI: 保存" }).click();
    await expect(secondaryTab).toHaveAttribute("aria-selected", "true");
    await expect(secondaryTab).toHaveAttribute("data-invalid", "true");
    await expect(secondaryEndpoint).toBeFocused();
    await expect(secondaryEndpoint).toHaveAttribute("aria-invalid", "true");
    await expect(secondary.getByText("Endpoint URL を入力してください。")).toBeVisible();
    await expect(secondary.getByText("Project OCID を入力してください。")).toBeVisible();
    await expect(secondary.getByText("API key を入力してください。")).toBeVisible();
    expect(patches).toHaveLength(0);
    await page.screenshot({
      path: testInfo.outputPath(`connection-secondary-errors-${scheme}-${testInfo.project.name}.png`),
    });

    await secondaryEndpoint.fill("https://secondary.example/openai/v1");
    await secondaryProject.fill("ocid1.generativeaiproject.oc1.us-chicago-1.secondary");
    await secondaryApiKey.fill("sk-secondary-input");
    await expect(secondaryTab).not.toHaveAttribute("data-invalid", "true");

    // 保存前の接続を選んだモデルは、登録モデルの保存を止めて先に接続の保存を案内する。
    await modelConnection1.click();
    await page
      .getByRole("listbox", { name: "モデル 1 の接続" })
      .getByRole("option", { name: "セカンダリ接続" })
      .click();
    await expect(modelConnection1).toContainText("セカンダリ接続");
    await expect(modelConnection1).toHaveAttribute("aria-invalid", "true");
    await expect(
      page.getByText("セカンダリ接続 はまだ保存されていません。", { exact: false })
    ).toBeVisible();
    await page.getByRole("button", { name: "登録モデル: 保存" }).click();
    await expect(modelConnection1).toBeFocused();
    expect(patches).toHaveLength(0);

    await page.getByRole("button", { name: "OCI Enterprise AI: 保存" }).click();
    await expect(page.getByText("OCI Enterprise AI 接続設定を保存しました。").first()).toBeVisible();
    expect(patches[0]?.enterprise_ai.connections).toEqual([
      expect.objectContaining({ connection_id: "primary" }),
      expect.objectContaining({
        connection_id: "secondary",
        endpoint: "https://secondary.example/openai/v1",
        project_ocid: "ocid1.generativeaiproject.oc1.us-chicago-1.secondary",
        api_key: "sk-secondary-input",
      }),
    ]);
    for (const connection of patches[0]?.enterprise_ai.connections as object[]) {
      expect(connection).not.toHaveProperty("display_name");
    }
    await expect(modelConnection1).toHaveAttribute("aria-invalid", "false");
    await expect(page.getByTestId("enterprise-connection-tab-secondary-unsaved")).toHaveCount(0);

    await page.getByRole("button", { name: "登録モデル: 保存" }).click();
    await expect(page.getByText("登録モデルを保存しました。").first()).toBeVisible();
    expect(patches[1]?.enterprise_ai.models).toMatchObject([
      { model_id: "enterprise-llm", connection_id: "secondary" },
      { model_id: "enterprise-vision", connection_id: "primary" },
    ]);

    await expectNoPageOverflow(page);
    await tablist.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath(`connections-${scheme}-${testInfo.project.name}.png`),
    });
    await page.locator("#enterprise-model-catalog").screenshot({
      path: testInfo.outputPath(`model-catalog-${scheme}-${testInfo.project.name}.png`),
    });
  });
}

test("セカンダリ接続を使うモデルがあるとき、削除は確認してプライマリ接続に移す", async ({ page }) => {
  const patches: Array<{ enterprise_ai: Record<string, unknown> }> = [];
  await mockModelSettings(
    page,
    (payload) => patches.push(payload as { enterprise_ai: Record<string, unknown> }),
    { secondary: true }
  );
  await page.goto("/settings/model");

  const modelConnection2 = page.getByRole("combobox", { name: "モデル 2 の接続" });
  await expect(modelConnection2).toContainText("セカンダリ接続");
  await page.getByRole("tab", { name: "セカンダリ接続" }).click();
  const remove = page.getByRole("button", { name: "セカンダリ接続を削除" });

  // やめると何も変わらない。
  await remove.click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("セカンダリ接続を使っている登録モデルがあります（Vision LLM）");
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page.getByTestId("enterprise-connection-secondary")).toBeVisible();

  await remove.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "プライマリ接続に移して削除" }).click();
  await expect(page.getByTestId("enterprise-connection-secondary")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "セカンダリ接続を設定" })).toBeFocused();
  await expect(modelConnection2).toContainText("プライマリ接続");

  await page.getByRole("button", { name: "OCI Enterprise AI: 保存" }).click();
  await expect(page.getByText("OCI Enterprise AI 接続設定を保存しました。").first()).toBeVisible();
  expect(patches[0]?.enterprise_ai.connections).toHaveLength(1);
  expect(patches[0]?.enterprise_ai.models).toMatchObject([
    { connection_id: "primary" },
    { connection_id: "primary" },
  ]);
});

test("ターシャリ接続は Endpoint URL と API key だけで設定し、登録モデルで選べる（#786）", async ({
  page,
}, testInfo) => {
  const patches: Array<{ enterprise_ai: Record<string, unknown> }> = [];
  await mockModelSettings(page, (payload) =>
    patches.push(payload as { enterprise_ai: Record<string, unknown> })
  );
  await page.goto("/settings/model");

  const tablist = page.getByRole("tablist", { name: "OCI Enterprise AI の接続" });
  await expect(tablist.getByRole("tab")).toHaveText(["プライマリ接続", "セカンダリ接続", "ターシャリ接続"]);
  const tertiaryTab = tablist.getByRole("tab", { name: "ターシャリ接続" });
  await tertiaryTab.click();
  const empty = page.getByTestId("enterprise-connection-tertiary-empty");
  await expect(empty).toContainText("ターシャリ接続は設定されていません。");
  await expect(empty).toContainText("OpenAI や OpenAI 互換 API");
  await page.getByRole("button", { name: "ターシャリ接続を設定" }).click();

  // Endpoint URL と API key は必須、Project OCID は任意（OCI Enterprise AI を使うときだけ）。
  const tertiary = page.getByTestId("enterprise-connection-tertiary");
  const endpoint = page.locator("#enterprise-tertiary-endpoint");
  const project = page.locator("#enterprise-tertiary-project-ocid");
  const apiKey = page.locator("#enterprise-tertiary-api-key");
  await expect(endpoint).toBeFocused();
  await expect(endpoint).toHaveAttribute("aria-required", "true");
  await expect(apiKey).toHaveAttribute("aria-required", "true");
  await expect(project).not.toHaveAttribute("aria-required", "true");
  await expect(endpoint).toHaveAttribute("placeholder", "https://api.openai.com/v1");
  await expect(apiKey).toHaveAttribute("placeholder", /^sk-/);
  await expect(tertiary).toContainText("OCI Enterprise AI を使うときだけ");
  await expect(tertiary).not.toContainText("任意");

  await endpoint.fill("https://api.openai.com/v1");
  await apiKey.fill("sk-openai-input");
  await page.getByRole("button", { name: "OCI Enterprise AI: 保存" }).click();
  await expect(page.getByText("OCI Enterprise AI 接続設定を保存しました。").first()).toBeVisible();
  expect(patches[0]?.enterprise_ai.connections).toEqual([
    expect.objectContaining({ connection_id: "primary" }),
    expect.objectContaining({
      connection_id: "tertiary",
      endpoint: "https://api.openai.com/v1",
      project_ocid: "",
      api_key: "sk-openai-input",
    }),
  ]);

  // 登録モデルの「接続」で、設定したターシャリ接続を選べる（未設定のセカンダリ接続は出さない）。
  const modelConnection1 = page.getByRole("combobox", { name: "モデル 1 の接続" });
  await modelConnection1.click();
  const options = page.getByRole("listbox", { name: "モデル 1 の接続" }).getByRole("option");
  await expect(options).toHaveText(["プライマリ接続", "ターシャリ接続"]);
  await options.filter({ hasText: "ターシャリ接続" }).click();
  await expect(modelConnection1).toContainText("ターシャリ接続");
  await page.getByRole("button", { name: "登録モデル: 保存" }).click();
  await expect(page.getByText("登録モデルを保存しました。").first()).toBeVisible();
  expect(patches[1]?.enterprise_ai.models).toMatchObject([
    { model_id: "enterprise-llm", connection_id: "tertiary" },
    { model_id: "enterprise-vision", connection_id: "primary" },
  ]);

  await expectNoPageOverflow(page);
  await tablist.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath(`connection-tertiary-${testInfo.project.name}.png`),
  });
});

test("モデル設定はモデルごとのテスト成功と失敗を行内に表示する", async ({ page }, testInfo) => {
  await mockModelSettings(page);
  await page.goto("/settings/model");

  await page.getByRole("button", { name: "enterprise-llm をテスト" }).click();
  await expect(
    page.getByText("Enterprise AI の回答生成モデル「enterprise-llm」から応答を取得しました。")
  ).toBeVisible();
  // 成功の技術的な詳細（API の key/value）は「詳細」に畳む（messaging.md §10。#705）。
  await expect(page.getByText("surface")).toBeHidden();
  await page.getByText("詳細", { exact: true }).first().click();
  await expect(page.getByText("surface")).toBeVisible();
  await expect(page.getByText("llm", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "cohere.embed-v4.0 をテスト" }).click();
  await expect(
    page.getByText("Embedding モデル「cohere.embed-v4.0」のテストに失敗しました。")
  ).toBeVisible();
  await expect(page.getByText("確認ポイント")).toBeVisible();
  await expect(page.getByText("OCI config、設定名、region")).toBeVisible();

  await expect(page.getByText("ServiceError")).toBeVisible();
  // 機密情報を伏せた API エラー本文も、開いた「詳細」に表示する。
  await expect(page.getByText("401 Unauthorized: invalid model")).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({path: testInfo.outputPath("model-test-error-detail.png"), fullPage: true});
  // 結果は操作した欄の行のすぐ下に出す: Embedding の結果は Rerank の欄より上（messaging.md §10）。
  const embeddingResult = page.getByText("Embedding モデル「cohere.embed-v4.0」のテストに失敗しました。");
  const embeddingBox = await embeddingResult.boundingBox();
  const embeddingFieldBox = await page.locator("#genai-embedding-model").boundingBox();
  const rerankFieldBox = await page.locator("#genai-rerank-model").boundingBox();
  expect(embeddingBox && embeddingFieldBox && rerankFieldBox).toBeTruthy();
  expect(embeddingBox!.y).toBeGreaterThan(embeddingFieldBox!.y);
  expect(embeddingBox!.y).toBeLessThan(rerankFieldBox!.y);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "wide", width: 1920, height: 1000 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`登録モデルごとに推論の量を選び、テストと保存に選んだ値を送る（既定は low。#1425・#3004, ${viewport.name}）`, { tag: "@responsive" }, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    let savedPayload: { enterprise_ai: { models: Array<Record<string, unknown>> } } | undefined;
    const testedEfforts: unknown[] = [];
    await mockModelSettings(page, (payload) => {
      savedPayload = payload as typeof savedPayload;
    });
    await page.route("**/api/settings/model/test", async (route) => {
      const payload = route.request().postDataJSON();
      testedEfforts.push(payload.settings.enterprise_ai.models[0].reasoning_effort);
      await route.fulfill({
        json: {
          data: {
            status: "success",
            target_type: payload.target_type,
            model_id: payload.model_id,
            message: `Enterprise AI の回答生成モデル「${payload.model_id}」から応答を取得しました。`,
            troubleshooting: [],
            raw_error: null,
            error_type: null,
            elapsed_ms: 42,
            checked_at: "2026-10-11T00:00:00Z",
            details: { surface: "llm", response_chars: 8 },
          },
          error_messages: [],
          warning_messages: [],
        },
      });
    });
    await page.goto("/settings/model");

    const effort1 = page.getByRole("combobox", { name: "モデル 1 の推論の量" });
    // 値の無い保存済みのモデルは既定の low。
    await expect(effort1).toHaveText("low");
    if (viewport.width >= 1536) {
      // 広い画面（2xl。#1437 で 8 列になり xl から変えた）は表頭に「推論の量」を出し、接続の右に並べる。
      const header = page.locator("#enterprise-model-catalog").getByText("推論の量", { exact: true });
      await expect(header).toBeVisible();
      const connectionBox = await page.getByRole("combobox", { name: "モデル 1 の接続" }).boundingBox();
      const effortBox = await effort1.boundingBox();
      expect(effortBox!.x).toBeGreaterThan(connectionBox!.x);
      expect(Math.abs(effortBox!.y - connectionBox!.y)).toBeLessThan(1);
    }
    // 説明（none は送らない・保存の前にテスト）を欄から読める。
    await expect(effort1).toHaveAccessibleDescription(/none は指定を送らず.*「テスト」で確認/);
    await effort1.click();
    await expect(
      page.getByRole("listbox", { name: "モデル 1 の推論の量" }).getByRole("option")
    ).toHaveText(["none（送らない）", "low", "medium", "high"]);
    await page.getByRole("option", { name: "none（送らない）" }).click();
    await expect(effort1).toHaveText("none（送らない）");

    // テストは保存前に選んだ値で呼ぶ。
    await page.getByRole("button", { name: "enterprise-llm をテスト" }).click();
    await expect(
      page.getByText("Enterprise AI の回答生成モデル「enterprise-llm」から応答を取得しました。")
    ).toBeVisible();
    expect(testedEfforts).toEqual(["none"]);

    // 追加したモデルの既定も low。
    await page.getByRole("button", { name: "追加" }).click();
    await expect(page.getByRole("combobox", { name: "モデル 3 の推論の量" })).toHaveText("low");
    await page.getByRole("combobox", { name: "モデル 3 の推論の量" }).click();
    await page.getByRole("option", { name: "high", exact: true }).click();
    await page.getByRole("textbox", { name: "モデル ID 3" }).fill("claude-opus-5");
    await page.getByRole("button", { name: "登録モデル: 保存" }).click();
    await expect(page.getByText("登録モデルを保存しました。").first()).toBeVisible();
    expect(savedPayload?.enterprise_ai.models.map((model) => model.reasoning_effort)).toEqual([
      "none",
      "low",
      "high",
    ]);
    await expectNoPageOverflow(page);
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "wide", width: 1920, height: 1000 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`登録モデルごとに入力の上限（tokens）を入力し、範囲外は保存の前に止める（既定は 128000。#1437, ${viewport.name}）`, { tag: "@responsive" }, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    let savedPayload: { enterprise_ai: { models: Array<Record<string, unknown>> } } | undefined;
    await mockModelSettings(page, (payload) => {
      savedPayload = payload as typeof savedPayload;
    });
    await page.goto("/settings/model");

    const limit1 = page.getByRole("spinbutton", { name: "モデル 1 の入力の上限（tokens）" });
    // 値の無い保存済みのモデルは既定の 128000。
    await expect(limit1).toHaveValue("128000");
    await expect(limit1).toHaveAccessibleDescription(/入力と出力を合わせたコンテキスト長.*token 予算/);
    if (viewport.name === "desktop") {
      // xl〜2xl は 1 つのモデルの欄を 2 列に並べる（表の形は 2xl から）。入力の上限は接続の下の左の列。
      const connectionBox = await page.getByRole("combobox", { name: "モデル 1 の接続" }).boundingBox();
      const limitBox = await limit1.boundingBox();
      expect(Math.abs(limitBox!.x - connectionBox!.x)).toBeLessThan(1);
      expect(limitBox!.y).toBeGreaterThan(connectionBox!.y);
      await expect(
        page.locator("#enterprise-model-catalog").getByText("入力の上限（tokens）", { exact: true })
      ).toBeHidden();
    }
    if (viewport.name === "wide") {
      // 広い画面（2xl）は表頭に「入力の上限（tokens）」を出し、推論の量の右に同じ行で並べる。
      const header = page
        .locator("#enterprise-model-catalog")
        .getByText("入力の上限（tokens）", { exact: true });
      await expect(header).toBeVisible();
      const effortBox = await page.getByRole("combobox", { name: "モデル 1 の推論の量" }).boundingBox();
      const limitBox = await limit1.boundingBox();
      expect(limitBox!.x).toBeGreaterThan(effortBox!.x);
      expect(Math.abs(limitBox!.y - effortBox!.y)).toBeLessThan(1);
      // 同じ行の入力欄と選択欄は同じ高さ。
      expect(Math.abs(limitBox!.height - effortBox!.height)).toBeLessThan(1);
    }
    if (process.env.RAG_E2E_SCREENSHOT_DIR) {
      await page.locator("#enterprise-model-catalog").screenshot({
        path: `${process.env.RAG_E2E_SCREENSHOT_DIR}/model-max-input-tokens-${viewport.name}.png`,
      });
    }

    // 範囲外は保存の前に欄のエラーで止め、その欄にフォーカスする。
    await limit1.fill("1000");
    await page.getByRole("button", { name: "登録モデル: 保存" }).click();
    await expect(page.getByText("入力の上限は 4096〜2000000 の整数で入力してください。")).toBeVisible();
    await expect(limit1).toBeFocused();
    expect(savedPayload).toBeUndefined();

    await limit1.fill("200000");
    await page.getByRole("button", { name: "登録モデル: 保存" }).click();
    await expect(page.getByText("登録モデルを保存しました。").first()).toBeVisible();
    expect(savedPayload?.enterprise_ai.models.map((model) => model.max_input_tokens)).toEqual([
      200000,
      128000,
    ]);
    await expectNoPageOverflow(page);
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`seed の欄を持たず、登録モデルの保存に seed を含めない（#3529, ${viewport.name}）`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const savedEnterprise: Record<string, unknown>[] = [];
    await mockModelSettings(page, (payload) => {
      savedEnterprise.push((payload as { enterprise_ai: Record<string, unknown> }).enterprise_ai);
    });
    await page.goto("/settings/model");

    await expect(page.getByRole("button", { name: "登録モデル: 保存" })).toBeVisible();
    await expect(page.getByRole("spinbutton", { name: "seed" })).toHaveCount(0);
    // 推論の量が Select AI に効かない注記は読める。
    await expect(
      page.getByRole("combobox", { name: "モデル 1 の推論の量" })
    ).toHaveAccessibleDescription(/Select AI・Select AI Agent には、この画面の推論の量は送信しません/);

    await page.getByRole("button", { name: "登録モデル: 保存" }).click();
    await expect(page.getByText("登録モデルを保存しました。").first()).toBeVisible();
    expect(savedEnterprise).toHaveLength(1);
    expect(savedEnterprise[0]).not.toHaveProperty("seed");
    await expectNoPageOverflow(page);
  });
}

async function mockModelSettings(
  page: Page,
  onPatch?: (payload: unknown) => void,
  options: ModelSettingsOptions = {}
) {
  // 保存した内容を次の取得で返す（接続の保存の後に登録モデルを保存する流れ。#533）。
  let current = createModelSettings(options);
  await page.route("**/api/settings/model", async (route) => {
    const request = route.request();
    if (request.method() === "PATCH") {
      const payload = request.postDataJSON();
      onPatch?.(payload);
      current = { ...current, settings: payload };
    }
    const data = current;
    await route.fulfill({
      json: {
        data,
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/settings/model/test", async (route) => {
    const payload = route.request().postDataJSON();
    const failed = payload.target_type === "embedding";
    await route.fulfill({
      json: {
        data: {
          status: failed ? "failed" : "success",
          target_type: payload.target_type,
          model_id: payload.model_id,
          message: failed
            ? `Embedding モデル「${payload.model_id}」のテストに失敗しました。`
            : `Enterprise AI の回答生成モデル「${payload.model_id}」から応答を取得しました。`,
          troubleshooting: failed
            ? [
                "OCI config、設定名、region、compartment OCID をサーバー側の実行環境から参照できるか確認してください。",
              ]
            : [],
          raw_error: failed ? "401 Unauthorized: invalid model" : null,
          error_type: failed ? "ServiceError" : null,
          elapsed_ms: 42,
          checked_at: "2026-06-14T00:00:00Z",
          details: failed ? {} : { surface: "llm", response_chars: 8 },
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

/** テキストの欄が Vision の欄より先（同じ行なら左、縦積みなら上）にある（#566）。 */
async function expectTextBeforeVision(text: Locator, vision: Locator) {
  const textBox = await text.boundingBox();
  const visionBox = await vision.boundingBox();
  expect(textBox).not.toBeNull();
  expect(visionBox).not.toBeNull();
  const sameRow = Math.abs(textBox!.y - visionBox!.y) < 1;
  expect(sameRow ? textBox!.x < visionBox!.x : textBox!.y < visionBox!.y).toBe(true);
}

/** 欄の直下（aria-describedby で結ばれた FieldError）にエラーが出ている。 */
async function expectFieldErrorBelow(field: Locator, message: string) {
  const describedBy = (await field.getAttribute("aria-describedby")) ?? "";
  const errorId = describedBy.split(" ").find((id) => id.endsWith("-error"));
  expect(errorId).toBeTruthy();
  const error = field.page().locator(`[id="${errorId}"]`);
  await expect(error).toHaveText(message);
  const fieldBox = await field.boundingBox();
  const errorBox = await error.boundingBox();
  expect(fieldBox).not.toBeNull();
  expect(errorBox).not.toBeNull();
  expect(errorBox!.y).toBeGreaterThanOrEqual(fieldBox!.y + fieldBox!.height - 1);
  expect(Math.abs(errorBox!.x - fieldBox!.x)).toBeLessThan(1);
}

async function expectActionInsideCard(
  page: Page,
  heading: string,
  button: ReturnType<Page["getByRole"]>
) {
  const card = page
    .getByRole("heading", { name: heading })
    .locator(
      "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' rounded-lg ')][1]"
    );
  const cardBox = await card.boundingBox();
  const buttonBox = await button.boundingBox();

  expect(cardBox).not.toBeNull();
  expect(buttonBox).not.toBeNull();
  expect(buttonBox!.x + buttonBox!.width).toBeLessThanOrEqual(
    cardBox!.x + cardBox!.width + 1
  );
  expect(buttonBox!.y + buttonBox!.height).toBeLessThanOrEqual(
    cardBox!.y + cardBox!.height + 1
  );
}

async function expectControlContentToBeVerticallyCentered(
  control: ReturnType<Page["getByRole"]>,
  childSelector: string
) {
  const controlBox = await control.boundingBox();
  const childBox = await control.locator(childSelector).first().boundingBox();

  expect(controlBox).not.toBeNull();
  expect(childBox).not.toBeNull();

  const controlCenter = controlBox!.y + controlBox!.height / 2;
  const childCenter = childBox!.y + childBox!.height / 2;
  expect(Math.abs(controlCenter - childCenter)).toBeLessThanOrEqual(0.75);
}


test("モデルの説明は初期状態で閉じ、クリックとキーボードでモデルに依存しない説明を開ける", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockModelSettings(page);
  await page.goto("/settings/model");
  const help = page.getByTestId("model-parameter-help");
  const toggle = help.locator("summary");
  const inputHelp = help.locator("#enterprise-model-max-input-tokens-help");
  await expect(help).toHaveAttribute("data-state", "closed");
  await expect(inputHelp).toBeHidden();
  await toggle.click();
  await expect(help).toHaveAttribute("data-state", "open");
  await expect(inputHelp).toBeVisible();
  await expect(inputHelp).toContainText("入力と出力を合わせたコンテキスト長");
  await expect(help).toContainText("対応する値や効果はモデル・API により異なり");
  await expect(help).toContainText("推論の token も含まれる場合があります");
  await expect(help).not.toContainText(/xai\.|openai\.|claude-|128000|32768/);
  await expect(help.getByRole("link", { name: "Oracle 公式: モデルの仕様" })).toHaveAttribute("href", "https://docs.oracle.com/en-us/iaas/Content/generative-ai/models.htm");
  await toggle.focus();
  await page.keyboard.press("Space");
  await expect(inputHelp).toBeHidden();
  await page.keyboard.press("Enter");
  await expect(inputHelp).toBeVisible();
  await toggle.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("model-parameter-help.png") });
  await expectNoPageOverflow(page);
});
