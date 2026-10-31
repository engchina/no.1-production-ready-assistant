import { expect, test, type Page } from "./fixtures/test";
import { expectNoPageOverflow, mockLocalAuth, openSidebarNav } from "./_helpers";

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760, collapseSidebar: false },
  { name: "mobile", width: 375, height: 812, collapseSidebar: true },
]) {
  test(`文書分割設定は方式とパラメータを表示する (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapseSidebar) {
      await page.addInitScript(() => {
        window.localStorage.setItem(
          "production-ready-rag.ui",
          JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
        );
      });
    }
    await mockChunkingSettings(page);

    await page.goto("/settings/chunking");

    await expect(page.getByRole("heading", { name: "文書分割方式" })).toBeVisible();
    await expect(
      page.getByText("取込済みの文書の chunk は、文書の詳細で再処理するまで変わりません。", { exact: false })
    ).toBeVisible();
    // 実行の場所の説明（backend 内で実行）は出さない（#3007）。
    await expect(page.locator("main")).not.toContainText("backend 内で実行");
    await expect(page.locator("main")).not.toContainText("pipeline-chunking");
    // 画面の文言に移植元の呼び名（DocRAG）を出さない（#598）。
    await expect(page.locator("main")).not.toContainText("DocRAG");
    // 親子階層（small-to-big）は、削除した「親子階層」があった位置(3 番目)に並ぶ(#271)。
    const radios = page.getByRole("radio");
    await expect(radios).toHaveCount(7);
    for (const [index, name] of [
      "構造認識",
      "再帰文字分割",
      "親子階層（small-to-big）",
      "見出し単位",
      "ページ単位",
      "固定長",
      "固定分割符",
    ].entries()) {
      await expect(radios.nth(index)).toHaveAccessibleName(new RegExp(name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    }
    await expect(page.getByRole("radio", { name: /親子分割|AutoMerging/ })).toHaveCount(0);
    // 各方式カードに概念図(装飾 SVG)が 1 つずつ描画される。
    await expect(page.locator('svg[viewBox="0 0 48 36"]')).toHaveCount(7);
    await expect(page.getByRole("heading", { name: "戦略別パラメータ" })).toBeVisible();
    await expect(page.getByRole("spinbutton", { name: "chunk サイズ(文字)", exact: true })).toHaveValue("800");
    await expect(page.getByLabel("overlap(文字)")).toHaveValue("120");
    await expect(
      page.getByRole("switch", { name: "文脈ヘッダを検索対象へ追加" })
    ).toHaveCount(0);

    await expectStrategyParams(page, /^構造認識/, [
      "chunk サイズ(文字)",
      "overlap(文字)",
      "最小 chunk 文字数",
    ]);
    await expectStrategyParams(page, /再帰文字分割/, [
      "chunk サイズ(文字)",
      "overlap(文字)",
      "最小 chunk 文字数",
    ]);
    await expectStrategyParams(page, /親子階層（small-to-big）/, SMALL_TO_BIG_LABELS);
    await expect(page.getByRole("switch", { name: "文脈ヘッダを検索対象へ追加" })).toHaveCount(0);
    await expect(page.getByRole("spinbutton", { name: "子チャンク目標文字数", exact: true })).toHaveValue("1000");
    await expect(page.getByRole("spinbutton", { name: "表の子チャンク目標文字数", exact: true })).toHaveValue("3000");
    await expect(page.getByRole("spinbutton", { name: "親チャンク目標文字数", exact: true })).toHaveValue("6000");
    await expect(page.getByRole("spinbutton", { name: "親チャンク最大ページ数", exact: true })).toHaveValue("3");
    await expect(page.getByRole("spinbutton", { name: "親チャンク最大 child 数", exact: true })).toHaveValue("12");
    // 「有効パラメータ」に親子階層の 5 項目が出る(空にならない)。
    const activeParams = page.locator("dl > div").filter({ hasText: "有効パラメータ" });
    await expect(activeParams).toContainText("子チャンク目標文字数: 1,000");
    await expect(activeParams).toContainText("表の子チャンク目標文字数: 3,000");
    await expect(activeParams).toContainText("親チャンク目標文字数: 6,000");
    await expect(activeParams).toContainText("親チャンク最大ページ数: 3");
    await expect(activeParams).toContainText("親チャンク最大 child 数: 12");
    await expectNoHorizontalOverflow(page);
    await expectSemanticStrategyParams(
      page,
      /見出し単位/,
      "見出し内の再分割上限(文字)"
    );
    await expectSemanticStrategyParams(
      page,
      /ページ単位/,
      "ページ内の再分割上限(文字)"
    );
    await expectStrategyParams(page, /^固定長 /, ["chunk サイズ(文字)", "overlap(文字)"]);

    await page.getByRole("radio", { name: /^構造認識/ }).click();
    await expect(page.getByRole("spinbutton", { name: "chunk サイズ(文字)", exact: true })).toHaveValue("800");
    await expect(page.getByLabel("overlap(文字)")).toHaveValue("120");

    await page.getByRole("radio", { name: /固定分割符/ }).click();
    await expect(page.getByRole("heading", { name: "戦略別パラメータ" })).toBeVisible();
    await expect(page.getByLabel("固定分割符文字列")).toHaveValue("\\n\\n");
    const delimiter = page.getByLabel("固定分割符文字列");
    await delimiter.fill("<<<JSONL_RECORD_BOUNDARY>>>");
    await expect.poll(() => delimiter.evaluate((input: HTMLInputElement) => {
      const style = getComputedStyle(input);
      const context = document.createElement("canvas").getContext("2d")!;
      context.font = style.font;
      const textWidth = context.measureText(input.value).width;
      const available = input.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      return textWidth <= available;
    })).toBe(true);
    await delimiter.screenshot({ path: test.info().outputPath("chunking-delimiter.png") });
    await expect(page.getByRole("spinbutton", { name: "chunk サイズ(文字)", exact: true })).toHaveCount(0);
    await expect(page.getByLabel("overlap(文字)")).toHaveCount(0);
    await expect(page.getByRole("spinbutton", { name: "子チャンク目標文字数", exact: true })).toHaveCount(0);
    await expect(page.getByLabel("最小 chunk 文字数")).toHaveCount(0);

    // 375px ではナビがドロワー（#367）。開いて現在地を確かめる。
    const navLink = (await openSidebarNav(page)).getByRole("link", { name: "文書分割" });
    await expect(navLink).toHaveAttribute("aria-current", "page");
    await expectNoHorizontalOverflow(page);
  });
}

test("文書分割設定取得に失敗したら再試行できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await page.route("**/api/settings/chunking", async (route) => {
    await route.fulfill({
      status: 503,
      json: {
        data: null,
        error_messages: ["文書分割設定を取得できませんでした。"],
        warning_messages: [],
      },
    });
  });

  await page.goto("/settings/chunking");

  await expect(page.getByRole("alert")).toContainText("文書分割設定を取得できませんでした。");
  await expect(page.getByRole("button", { name: "再試行" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("文書分割設定は親子階層（small-to-big）のパラメータを保存できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  let savedPayload: unknown = null;
  await page.route("**/api/settings/chunking", async (route) => {
    if (route.request().method() === "PATCH") {
      savedPayload = route.request().postDataJSON();
      await route.fulfill({
        json: chunkingEnvelope({
          strategy: "small_to_big",
          chunk_child_target_chars: 600,
          chunk_parent_max_pages: 2,
        }),
      });
      return;
    }
    await route.fulfill({ json: chunkingEnvelope() });
  });

  await page.goto("/settings/chunking");

  const smallToBig = page.getByRole("radio", { name: /親子階層（small-to-big）/ });
  await smallToBig.click();
  await expect(smallToBig).toBeChecked();

  const childTarget = page.getByRole("spinbutton", { name: "子チャンク目標文字数", exact: true });
  await childTarget.fill("2000");
  // 範囲外は保存を押したときに欄の直下へ理由を出し、その欄へフォーカスする（押せないボタンだけにしない。#541）。
  await page.getByRole("button", { name: "保存" }).click();
  await expect(childTarget).toHaveAccessibleDescription(/子チャンク目標文字数は 300 以上 1,600 以下の整数を入力してください。/);
  await expect(childTarget).toHaveAttribute("aria-invalid", "true");
  await expect(childTarget).toBeFocused();
  expect(savedPayload).toBeNull();

  await childTarget.fill("600");
  await page.getByRole("spinbutton", { name: "親チャンク最大ページ数", exact: true }).fill("2");
  await expect(page.getByText("未保存の変更があります。")).toBeVisible();
  await expect(page.locator("dl > div").filter({ hasText: "有効パラメータ" })).toContainText(
    "子チャンク目標文字数: 600"
  );

  await page.getByRole("button", { name: "保存" }).click();

  await expect(page.getByText("文書分割の設定を保存しました。")).toBeVisible();
  expect(savedPayload).toEqual({
    strategy: "small_to_big",
    chunk_size: 800,
    overlap: 120,
    min_chars: 120,
    delimiter: "\\n\\n",
    chunk_child_target_chars: 600,
    chunk_table_child_target_chars: 3000,
    chunk_parent_target_chars: 6000,
    chunk_parent_max_pages: 2,
    chunk_parent_max_children: 12,
  });
  await expect(page.getByRole("spinbutton", { name: "子チャンク目標文字数", exact: true })).toHaveValue("600");
  await expectNoHorizontalOverflow(page);
});

test("文書分割設定は方式とパラメータを保存できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  let savedPayload: unknown = null;
  await page.route("**/api/settings/chunking", async (route) => {
    if (route.request().method() === "PATCH") {
      savedPayload = route.request().postDataJSON();
      await route.fulfill({
        json: chunkingEnvelope({
          strategy: "recursive_character",
          chunk_size: 1000,
          overlap: 120,
          min_chars: 40,
          delimiter: "\\n\\n",
              }),
      });
      return;
    }
    await route.fulfill({ json: chunkingEnvelope() });
  });

  await page.goto("/settings/chunking");

  const recursive = page.getByRole("radio", { name: /再帰文字分割/ });
  await recursive.click();
  await expect(recursive).toBeChecked();

  const minChars = page.getByLabel("最小 chunk 文字数");
  await minChars.fill("40");
  const chunkSize = page.getByRole("spinbutton", { name: "chunk サイズ(文字)", exact: true });
  await chunkSize.fill("1000");
  await expect(page.getByText("未保存の変更があります。")).toBeVisible();

  await page.getByRole("button", { name: "保存" }).click();

  await expect(page.getByText("文書分割の設定を保存しました。")).toBeVisible();
  expect(savedPayload).toEqual({
    strategy: "recursive_character",
    chunk_size: 1000,
    overlap: 120,
    min_chars: 40,
    delimiter: "\\n\\n",
    ...SMALL_TO_BIG_DEFAULTS,
  });
  await expectNoHorizontalOverflow(page);
});

test("文書分割設定は固定分割符を保存できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  let savedPayload: unknown = null;
  await page.route("**/api/settings/chunking", async (route) => {
    if (route.request().method() === "PATCH") {
      savedPayload = route.request().postDataJSON();
      await route.fulfill({
        json: chunkingEnvelope({
          strategy: "fixed_delimiter",
          delimiter: "---SECTION---",
        }),
      });
      return;
    }
    await route.fulfill({ json: chunkingEnvelope() });
  });

  await page.goto("/settings/chunking");
  await page.getByRole("radio", { name: /固定分割符/ }).click();
  await page.getByLabel("固定分割符文字列").fill("---SECTION---");
  await page.getByRole("button", { name: "保存" }).click();

  await expect(page.getByText("文書分割の設定を保存しました。")).toBeVisible();
  expect(savedPayload).toEqual({
    strategy: "fixed_delimiter",
    chunk_size: 800,
    overlap: 120,
    min_chars: 120,
    delimiter: "---SECTION---",
    ...SMALL_TO_BIG_DEFAULTS,
  });
  await expectNoHorizontalOverflow(page);
});

const SMALL_TO_BIG_LABELS = [
  "子チャンク目標文字数",
  "表の子チャンク目標文字数",
  "親チャンク目標文字数",
  "親チャンク最大ページ数",
  "親チャンク最大 child 数",
];

const SMALL_TO_BIG_DEFAULTS = {
  chunk_child_target_chars: 1000,
  chunk_table_child_target_chars: 3000,
  chunk_parent_target_chars: 6000,
  chunk_parent_max_pages: 3,
  chunk_parent_max_children: 12,
};

type ChunkingOverrides = {
  strategy?: string;
  chunk_size?: number;
  overlap?: number;
  min_chars?: number;
  delimiter?: string;
  context_header_enabled?: boolean;
} & Partial<typeof SMALL_TO_BIG_DEFAULTS>;


function chunkingEnvelope(overrides: ChunkingOverrides = {}) {
  const strategy = overrides.strategy ?? "structure_aware";
  const specs: { name: string; origin: string; recommended_for: string[] }[] = [
    { name: "structure_aware", origin: "ragflow_docling_marker", recommended_for: ["pdf", "office"] },
    { name: "recursive_character", origin: "langchain_recursive_character", recommended_for: ["text"] },
    {
      name: "small_to_big",
      origin: "small_to_big",
      recommended_for: ["pdf", "manual", "table", "screenshot"],
    },
    { name: "markdown_heading", origin: "markdown_header_splitter", recommended_for: ["markdown"] },
    { name: "page_level", origin: "pageindex_coarse", recommended_for: ["pdf"] },
    { name: "fixed_size", origin: "ragflow_general_fixed", recommended_for: ["text"] },
    { name: "fixed_delimiter", origin: "fixed_delimiter_split", recommended_for: ["text"] },
  ];
  return {
    data: {
      strategy,
      chunk_size: overrides.chunk_size ?? 800,
      overlap: overrides.overlap ?? 120,
      min_chars: overrides.min_chars ?? 120,
      delimiter: overrides.delimiter ?? "\\n\\n",
      context_header_enabled: overrides.context_header_enabled ?? true,
      chunk_child_target_chars:
        overrides.chunk_child_target_chars ?? SMALL_TO_BIG_DEFAULTS.chunk_child_target_chars,
      chunk_table_child_target_chars:
        overrides.chunk_table_child_target_chars ??
        SMALL_TO_BIG_DEFAULTS.chunk_table_child_target_chars,
      chunk_parent_target_chars:
        overrides.chunk_parent_target_chars ?? SMALL_TO_BIG_DEFAULTS.chunk_parent_target_chars,
      chunk_parent_max_pages:
        overrides.chunk_parent_max_pages ?? SMALL_TO_BIG_DEFAULTS.chunk_parent_max_pages,
      chunk_parent_max_children:
        overrides.chunk_parent_max_children ?? SMALL_TO_BIG_DEFAULTS.chunk_parent_max_children,
      strategies: specs.map((spec) => ({
        ...spec,
        selected: spec.name === strategy,
      })),
      config_source: "runtime",
    },
    error_messages: [],
    warning_messages: [],
  };
}

async function mockChunkingSettings(page: Page) {
  await page.route("**/api/settings/chunking", async (route) => {
    await route.fulfill({ json: chunkingEnvelope() });
  });
}

async function expectStrategyParams(page: Page, radioName: RegExp, visibleLabels: string[]) {
  const allLabels = [
    "chunk サイズ(文字)",
    "overlap(文字)",
    "最小 chunk 文字数",
    "固定分割符文字列",
    ...SMALL_TO_BIG_LABELS,
    "見出し内の再分割上限(文字)",
    "ページ内の再分割上限(文字)",
    "再分割時の重複文字数",
  ];
  await page.getByRole("radio", { name: radioName }).click();
  for (const label of allLabels) {
    // 必須の欄はラベルに「必須」のタグ（aria-hidden）を持ち、getByLabel の exact はタグの文字も含めて
    // 比べるため、タグを除いた accessible name で探す（#531）。
    const locator = page
      .getByRole("spinbutton", { name: label, exact: true })
      .or(page.getByRole("textbox", { name: label, exact: true }));
    await expect(locator).toHaveCount(visibleLabels.includes(label) ? 1 : 0);
  }
}

async function expectSemanticStrategyParams(
  page: Page,
  radioName: RegExp,
  splitLimitLabel: string
) {
  await page.getByRole("radio", { name: radioName }).click();
  await expect(page.getByText("32,000文字超のみ再分割 / 重複なし")).toBeVisible();

  const details = page.locator("details").filter({
    hasText: "長大な単位の再分割(詳細設定)",
  });
  await expect(details).not.toHaveAttribute("open", "");
  await details.getByText("長大な単位の再分割(詳細設定)").click();
  await expect(details.getByRole("spinbutton", { name: splitLimitLabel, exact: true })).toHaveValue("32000");
  await expect(details.getByLabel("再分割時の重複文字数")).toHaveValue("0");
  await expect(details.getByLabel("最小 chunk 文字数")).toHaveValue("120");
}

async function expectNoHorizontalOverflow(page: Page) {
  // documentElement と main の双方を検査する共通ヘルパーへ委譲(_helpers.ts)。
  await expectNoPageOverflow(page);
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`文書分割の設定は保存に失敗しても入力を残し、操作の行に失敗を出す（#966, ${viewport.name}）`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    let patchCount = 0;
    await page.route("**/api/settings/chunking", async (route) => {
      if (route.request().method() === "PATCH") {
        patchCount += 1;
        if (patchCount === 1) {
          await route.fulfill({
            status: 500,
            json: {
              data: null,
              error_messages: ["文書分割設定を backend/.env へ保存できませんでした。"],
              warning_messages: [],
            },
          });
          return;
        }
        await route.fulfill({
          json: chunkingEnvelope({ strategy: "recursive_character", chunk_size: 1000 }),
        });
        return;
      }
      await route.fulfill({ json: chunkingEnvelope() });
    });
    await page.goto("/settings/chunking");

    const recursive = page.getByRole("radio", { name: /再帰文字分割/ });
    await recursive.click();
    const chunkSize = page.getByRole("spinbutton", { name: "chunk サイズ(文字)", exact: true });
    await chunkSize.fill("1000");
    const actions = page.getByRole("group", { name: "文書分割の設定の操作" });
    const saveButton = actions.getByRole("button", { name: "保存" });
    await saveButton.click();

    // 失敗は操作の行に出し、選んだ方式・入力・保存のボタンを残す。
    await expect(actions).toContainText("文書分割設定を backend/.env へ保存できませんでした。");
    await expect(recursive).toBeChecked();
    await expect(chunkSize).toHaveValue("1000");
    await expect(saveButton).toBeEnabled();
    await expectNoHorizontalOverflow(page);

    // そのまま再保存できる。
    await saveButton.click();
    await expect(page.getByText("文書分割の設定を保存しました。")).toBeVisible();
    await expect(recursive).toBeChecked();
    await expect(saveButton).toBeDisabled();
    expect(patchCount).toBe(2);
  });
}

test("文書分割の設定は、方式を切り替えて隠れた欄の不正な値を送らない（#966）", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  let savedPayload: Record<string, unknown> | null = null;
  await page.route("**/api/settings/chunking", async (route) => {
    if (route.request().method() === "PATCH") {
      savedPayload = route.request().postDataJSON();
      await route.fulfill({ json: chunkingEnvelope({ strategy: "small_to_big" }) });
      return;
    }
    await route.fulfill({ json: chunkingEnvelope() });
  });
  await page.goto("/settings/chunking");

  // 構造認識で最小 chunk 文字数を空にしてから、その欄の無い親子階層へ切り替えて保存する。
  await page.getByLabel("最小 chunk 文字数").fill("");
  await page.getByRole("radio", { name: /親子階層/ }).click();
  await page.getByRole("button", { name: "保存" }).click();

  await expect(page.getByText("文書分割の設定を保存しました。")).toBeVisible();
  // 隠れた欄は保存済みの値で送る（空を null として送ると backend が英語の 422 を返す）。
  expect(savedPayload).toMatchObject({ strategy: "small_to_big", min_chars: 120 });
});

test("文脈ヘッダの切替と RAPTOR の設定は文書分割に出さない", { tag: "@responsive" }, async ({ page }) => {
  await mockChunkingSettings(page);
  await page.goto("/settings/chunking");
  await expect(page.getByRole("switch", {name: "文脈ヘッダを検索対象へ追加"})).toHaveCount(0);
  await expect(page.getByText("RAPTOR の階層の要約", {exact:true})).toHaveCount(0);
  await expectNoPageOverflow(page);
});
