import { expect, type Locator, type Page, test } from "./fixtures/test";
import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/knowledge-bases**", async (route) => {
    await route.fulfill({
      json: {
        data: {
          items: [{ id: "kb-1", name: "社内規程", document_count: 1 }],
          total: 1,
          limit: 100,
          offset: 0,
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
});

test("文書 workspace で chunk と構造化 block を相互に確認できる", async ({ page }) => {
  await mockDocumentWorkspace(page);

  await page.goto("/documents/doc-1");

  // 左ペインのプレビューは常時表示。右ペインは本文 / 構造化要素 / Chunk / エクスポートのタブ切替。
  await expect(page.getByRole("heading", { name: "原本プレビュー" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "本文テキスト" })).toHaveAttribute(
    "aria-selected",
    "true"
  );
  await expect(page.getByRole("tab", { name: "構造化要素" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /Chunk \/ Citation/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: "抽出エクスポート" })).toBeVisible();
  await expect(page.getByTestId("document-inspector-pane").getByRole("tabpanel").getByRole("button", { name: "本文をコピー" })).toBeVisible();
  // 処理の詳細(診断)パネルは折りたたみに集約。
  await expect(page.getByText("処理の詳細(診断)")).toBeVisible();

  // エクスポートタブ: 内容は表示せず、ダウンロード・コピーの操作だけを出す（詳細は下の #561 のテスト）。
  await page.getByRole("tab", { name: "抽出エクスポート" }).click();
  await expect(page.getByRole("link", { name: "Markdown をダウンロード" })).toBeVisible();

  // Chunk タブ: chunk を選ぶとプレビューに bbox がハイライトされる。
  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  const chunkButton = page.getByTestId("document-inspector-pane").getByRole("tabpanel").getByRole("button", { name: /交通費は1000円/ });
  await chunkButton.focus();
  await page.keyboard.press("Enter");
  await expect(chunkButton).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByText(/位置: p\.1 \/ bbox x=0\.0% y=0\.0% w=100\.0% h=40\.0%/)
  ).toBeVisible();
  await expect(page.getByTestId("bbox-page-map")).toBeVisible();
  const overlay = page.getByTestId("bbox-overlay");
  const previewOverlay = page.getByTestId("bbox-preview-overlay");
  await expect(overlay).toBeVisible();
  await expect(page.getByTestId("bbox-preview-page")).toBeVisible();
  await expect(previewOverlay).toBeVisible();
  await expect(page.getByTestId("bbox-content-overlay")).toHaveCount(0);
  await expect(overlay).toHaveAttribute("data-bbox-mode", "xyxy");
  await expect(overlay).toHaveAttribute("data-bbox-unit", "percent");
  await expect(overlay).toHaveAttribute("style", /left: 0%; top: 0%; width: 100%; height: 40%;/);
  await expect(previewOverlay).toHaveAttribute("data-bbox-mode", "xyxy");
  await expect(previewOverlay).toHaveAttribute("data-bbox-unit", "percent");
  await expect(previewOverlay).toHaveAttribute(
    "style",
    /left: 0%; top: 0%; width: 100%; height: 40%;/
  );

  // 構造化要素タブ: 直前の chunk 選択で紐づく element(tbl-1)が選択済み。
  await page.getByRole("tab", { name: "構造化要素" }).click();
  await expect(page.getByRole("button", { name: /tbl-1 \/ local_text_structure/ })).toHaveAttribute(
    "aria-pressed",
    "true"
  );
  // 構造化要素(title)を選ぶと選択が移り、プレビュー bbox が更新される。
  const titleButton = page
    .getByTestId("document-inspector-pane")
    .getByRole("tabpanel")
    .getByRole("button", { name: /経費申請[\s\S]*el-0000/ });
  await titleButton.click();
  await expect(titleButton).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByText(/位置: p\.1 \/ bbox x=25\.0% y=25\.0% w=25\.0% h=25\.0%/)
  ).toBeVisible();
  await expect(overlay).toHaveAttribute("data-bbox-mode", "xyxy");
  await expect(overlay).toHaveAttribute("data-bbox-unit", "absolute");
  await expect(overlay).toHaveAttribute("style", /left: 25%; top: 25%; width: 25%; height: 25%;/);
  await expect(previewOverlay).toHaveAttribute("data-bbox-mode", "xyxy");
  await expect(previewOverlay).toHaveAttribute("data-bbox-unit", "absolute");
  await expect(previewOverlay).toHaveAttribute(
    "style",
    /left: 25%; top: 25%; width: 25%; height: 25%;/
  );

  // 連動: Chunk タブへ戻ると紐づく chunk が選択され、元の chunk は外れている。
  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  const chunkPanelAfter = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  await expect(
    chunkPanelAfter.getByRole("button", { name: /経費申請の概要/ })
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    chunkPanelAfter.getByRole("button", { name: /交通費は1000円/ })
  ).toHaveAttribute("aria-pressed", "false");
  await expectNoHorizontalOverflow(page);
});

test("成果物の無い recipe は文書レベルの抽出・処理後ファイルへ fallback しない", async ({
  page,
}) => {
  const state = await mockDocumentWorkspace(page, {
    documentStatus: "UPLOADED",
    documentOnlyPreparedArtifact: true,
  });

  await page.goto("/documents/doc-1");

  const previewPanel = page
    .getByRole("heading", { name: "原本プレビュー" })
    .locator("xpath=ancestor::section[1]");
  await expect(previewPanel.getByRole("tab", { name: "処理後" })).toBeDisabled();
  await expect(page.getByTestId("document-inspector-pane").getByRole("tabpanel").getByText("交通費は1000円です。")).toHaveCount(0);
  await expect.poll(() => state.extractionExportRequests).toBe(0);
});

test("desktop の空の右ペイン上でも主ページをスクロールできる", async ({ page }) => {
  await mockDocumentWorkspace(page, { documentStatus: "UPLOADED", pdfPreview: true });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/documents/doc-1");

  const panel = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  const panelMetrics = await panel.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      overscrollBehaviorY: style.overscrollBehaviorY,
    };
  });
  expect(panelMetrics.scrollHeight).toBeLessThanOrEqual(panelMetrics.clientHeight);
  expect(panelMetrics.overscrollBehaviorY).toBe("auto");

  await panel.scrollIntoViewIfNeeded();
  const main = page.locator("main");
  const mainScrollTop = await main.evaluate((element) => element.scrollTop);
  await panel.hover();
  await page.mouse.wheel(0, -800);
  await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBeLessThan(mainScrollTop);
});

test("desktop の右ペインは高さを保ち、境界で主ページへスクロールを引き継ぐ", async ({
  page,
}, testInfo) => {
  // マウスホイールでのスクロール引き継ぎはデスクトップの操作。タッチ端末プロジェクトでは入力方式が前提と一致しない。
  test.skip(testInfo.project.name !== "desktop", "desktop (mouse wheel) contract");
  // chunk を増やし、Chunk の一覧が右ペインからあふれるようにする。
  await mockDocumentWorkspace(page, { pdfPreview: true, extraChunkCount: 23 });

  // 画面の低い xl の幅（ペインは 1 画面より高くなる）。
  await page.setViewportSize({ width: 1280, height: 640 });
  await page.goto("/documents/doc-1");

  // 右ペイン（タブ + 内容）は左のプレビューと同じ高さにそろえる（#349 / #559）。
  // ページ画像が無い（iframe の）PDF は A4 縦の縦横比で高さを取るので、1 画面分（下限）より高い。
  const previewPane = page.getByTestId("document-preview-pane");
  const inspectorPane = page.getByTestId("document-inspector-pane");
  await expect(page.locator('iframe[title="policy.pdf"]')).toBeVisible();
  const previewPaneBox = await previewPane.boundingBox();
  expect((await inspectorPane.boundingBox())!.height).toBeCloseTo(previewPaneBox!.height, 0);
  expect(previewPaneBox!.height).toBeGreaterThan(640 - 28);
  const textPanel = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  const previewHeight = await textPanel.evaluate((element) => element.clientHeight);
  expect(previewHeight).toBeGreaterThan(400);
  const textPanelMetrics = await textPanel.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      clientHeight: element.clientHeight,
      overflowY: style.overflowY,
      overscrollBehaviorY: style.overscrollBehaviorY,
      scrollbarGutter: style.scrollbarGutter,
    };
  });

  expect(textPanelMetrics.clientHeight).toBeCloseTo(previewHeight, 0);
  expect(textPanelMetrics.overflowY).toBe("auto");
  expect(textPanelMetrics.overscrollBehaviorY).toBe("auto");
  expect(textPanelMetrics.scrollbarGutter).toContain("stable");

  await page.getByRole("tab", { name: "Chunk / Citation", exact: false }).click();
  const panel = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  const panelMetrics = await panel.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      overflowY: style.overflowY,
      overscrollBehaviorY: style.overscrollBehaviorY,
    };
  });
  expect(panelMetrics.clientHeight).toBeCloseTo(previewHeight, 0);
  expect(panelMetrics.scrollHeight).toBeGreaterThan(panelMetrics.clientHeight + 1);
  expect(panelMetrics.overflowY).toBe("auto");
  expect(panelMetrics.overscrollBehaviorY).toBe("auto");

  await panel.scrollIntoViewIfNeeded();
  const main = page.locator("main");
  const mainScrollTopBeforePanelScroll = await main.evaluate((element) => element.scrollTop);
  await panel.hover();
  await page.mouse.wheel(0, 400);
  await expect.poll(() => panel.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBe(
    mainScrollTopBeforePanelScroll
  );

  await panel.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  const mainScrollTop = await main.evaluate((element) => element.scrollTop);
  // ペインの高さは A4 縦の縦横比（#559）で小数になる。scrollTop = scrollHeight で送った下端には 1px 未満の
  // 余りが残り、Chromium は最初のホイールでその余りをペインで送って、同じ操作の間はペインに留まる。
  // 利用者がホイールで下端まで送ったときと同じく、次のホイールから主ページへ引き継ぐことを確かめる（#656）。
  await expect
    .poll(async () => {
      await page.mouse.wheel(0, 800);
      return main.evaluate((element) => element.scrollTop);
    })
    .toBeGreaterThan(mainScrollTop);

  for (const tabName of ["構造化要素", "抽出エクスポート"]) {
    await page.getByRole("tab", { name: tabName, exact: false }).click();
    // 右ペインのタブの名前でパネルを引く。
    const tabPanelMetrics = await page
      .getByTestId("document-inspector-pane")
      .getByRole("tabpanel", { name: tabName, exact: false })
      .evaluate((element) => {
        const style = getComputedStyle(element);
        return {
          clientHeight: element.clientHeight,
          overflowY: style.overflowY,
          overscrollBehaviorY: style.overscrollBehaviorY,
        };
      });
    expect(tabPanelMetrics.clientHeight).toBeCloseTo(previewHeight, 0);
    expect(tabPanelMetrics.overflowY).toBe("auto");
    expect(tabPanelMetrics.overscrollBehaviorY).toBe("auto");
  }
});

test("取込解析エンジンは抽出工程行に segment parser だけを表示する", async ({
  page,
}) => {
  await mockDocumentWorkspace(page, { pdfPreview: true, unstructuredSegment: true });

  await page.goto("/documents/doc-1");

  // 診断情報は「処理の詳細(診断)」折りたたみ内にあるため展開する。
  await page.getByText("処理の詳細(診断)").click();
  // 原本情報は原本のファクトのみ(エンジンや工程情報を持たない)。
  const sourcePanel = page
    .getByRole("heading", { name: "原本情報" })
    .locator("xpath=ancestor::section[1]");
  await expect(sourcePanel.getByText("MIME type")).toBeVisible();
  await expect(sourcePanel.getByText("取込解析エンジン")).toHaveCount(0);
  // 実際に解析した engine は工程別ジョブの「抽出」行に表示する。
  const jobsPanel = page
    .getByRole("heading", { name: "工程ごとの実行状況" })
    .locator("xpath=ancestor::section[1]");
  await expect(jobsPanel.getByText("Unstructured v1")).toBeVisible();
  await expect(jobsPanel.getByText("unstructured_adapter")).toHaveCount(0);
  await expect(
    page.getByText("アップロード時の初期判定: OCI Enterprise AI / v1")
  ).toHaveCount(0);
});

test("原本情報に文書 ID を出し、コピーできる（#2086）", { tag: "@responsive" }, async ({ page, context }, testInfo) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await mockDocumentWorkspace(page, { pdfPreview: true });

  await page.goto("/documents/doc-1");
  await page.getByText("処理の詳細(診断)").click();

  const documentId = page.getByTestId("document-id");
  await expect(documentId).toContainText("文書 ID");
  await expect(documentId).toContainText("doc-1");
  const copy = documentId.getByRole("button", { name: "文書 ID をコピー" });
  await copy.click();
  // 成功はボタンのアイコンの Check と読み上げ（ラベルは変えない。共有の CopyButton。#2099）。
  await expect(copy).toHaveAttribute("data-copy-state", "copied");
  await expect(documentId.getByRole("status")).toHaveText("文書 ID をコピーしました");
  // 読み上げの文は画面に固定し、本文の横はみ出し・スクロール領域からの抜け出しを作らない。
  expect(await documentId.getByRole("status").evaluate((element) => getComputedStyle(element).position)).toBe(
    "fixed"
  );
  await documentId.screenshot({ path: testInfo.outputPath("document-id-copied.png") });
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("doc-1");
  await expectNoPageOverflow(page);
});

// 旧形式の Office は未対応理由を 1 回だけ、短い文で出す（同じ文の警告の行を重ねない。#2834）。
test("原本情報の未対応理由は警告の行と重ねず 1 回だけ出す", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockDocumentWorkspace(page, {});
  const detail = documentDetail();
  await page.route("**/api/documents/doc-1", async (route) => {
    await route.fulfill({
      json: {
        data: {
          ...detail,
          file_name: "legacy.doc",
          source_profile: {
            ...detail.source_profile,
            original_file_name: "legacy.doc",
            sanitized_file_name: "legacy.doc",
            extension: ".doc",
            content_type: "application/msword",
            parser_profile: "unsupported_legacy_office_binary",
            unsupported_reason: "legacy_office_binary_not_supported",
            quality_status: "warning",
            quality_warnings: ["unsupported_legacy_office_binary"],
          },
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  await page.goto("/documents/doc-1");
  await page.getByText("処理の詳細(診断)").click();
  const sourcePanel = page.getByRole("heading", { name: "原本情報" }).locator("xpath=ancestor::section[1]");
  await expect(sourcePanel.getByText("旧 Office(要変換)")).toBeVisible();
  const reason = "旧形式の Office は変換が必要です（文書レシピのファイル準備で変換を選択）。";
  await expect(sourcePanel.getByText(reason, { exact: false })).toHaveCount(1);
  await expect(sourcePanel.getByText("未対応理由", { exact: false })).toHaveCount(1);
  await sourcePanel.screenshot({ path: testInfo.outputPath("legacy-office-reason.png") });
  await expectNoPageOverflow(page);
});

test("処理の詳細(診断) は Chevron で開閉の状態を示し、クリック・Enter・Space で開閉できる（#397）", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockDocumentWorkspace(page, { pdfPreview: true });

  await page.goto("/documents/doc-1");

  const diagnostics = page.getByTestId("document-diagnostics");
  const summary = diagnostics.locator("summary");
  const chevron = summary.locator("svg[data-state]");
  const sourceHeading = diagnostics.getByRole("heading", { name: "原本情報" });
  const rotate = () => chevron.evaluate((icon) => getComputedStyle(icon).rotate);

  // 折りたたみ: Chevron は右向き（-90deg）。ブラウザ標準の三角は出さない。
  await expect(summary).toContainText("処理の詳細(診断)");
  await expect(chevron).toHaveAttribute("data-state", "collapsed");
  await expect.poll(rotate).toBe("-90deg");
  await expect(sourceHeading).toBeHidden();
  expect(await summary.evaluate((node) => getComputedStyle(node).listStyleType)).toBe("none");
  // 見出しの行全体が押せる（高さはトークン。タッチ端末では 44px）。
  const box = await summary.boundingBox();
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(40);
  expect(box?.width ?? 0).toBeGreaterThan(((await diagnostics.boundingBox())?.width ?? 0) - 4);

  // クリックで開く: 下向き（0deg）、内容が見える。
  await summary.click({ position: { x: (box?.width ?? 200) - 12, y: (box?.height ?? 40) / 2 } });
  await expect(diagnostics).toHaveAttribute("open", "");
  await expect(chevron).toHaveAttribute("data-state", "expanded");
  await expect.poll(rotate).toBe("0deg");
  await expect(sourceHeading).toBeVisible();
  await summary.click();
  await expect(diagnostics).not.toHaveAttribute("open", "");
  await expect(chevron).toHaveAttribute("data-state", "collapsed");

  // キーボード: Enter で開き、Space で閉じる。フォーカスの表示は outline。
  await summary.focus();
  await page.keyboard.press("Enter");
  await expect(chevron).toHaveAttribute("data-state", "expanded");
  await expect(sourceHeading).toBeVisible();
  await page.keyboard.press("Space");
  await expect(chevron).toHaveAttribute("data-state", "collapsed");
  await expect(sourceHeading).toBeHidden();
  expect(await summary.evaluate((node) => node.matches(":focus-visible"))).toBe(true);
  expect(await summary.evaluate((node) => getComputedStyle(node).outlineStyle)).not.toBe("none");

  // reduced-motion では回転のアニメーションを止める（向きは変わる。base.css は 0.01ms に縮める）。
  await page.emulateMedia({ reducedMotion: "reduce" });
  const duration = await chevron.evaluate((icon) => parseFloat(getComputedStyle(icon).transitionDuration));
  expect(duration).toBeLessThan(0.001);
  await expectNoPageOverflow(page);
});

test("Chunk 作成と Embedding/索引の工程行に chunk 数・ベクトル数・embedding モデルを表示する", async ({
  page,
}) => {
  await mockDocumentWorkspace(page, { pdfPreview: true, jobsForAllPhases: true });

  await page.goto("/documents/doc-1");

  await page.getByText("処理の詳細(診断)").click();
  const jobsPanel = page
    .getByRole("heading", { name: "工程ごとの実行状況" })
    .locator("xpath=ancestor::section[1]");
  // recipes mock は INDEXED で chunk_count/vector_count = 2。
  await expect(jobsPanel.getByText("chunk 2 件")).toBeVisible();
  await expect(jobsPanel.getByText("ベクトル 2 件")).toBeVisible();
  await expect(jobsPanel.getByText("text-embedding-v4 · 1536 次元")).toBeVisible();
  // active chunk_set の作成日時(TZ 非依存で書式のみ検証)。
  await expect(jobsPanel.getByText(/chunk_set 作成: \d{2}\/\d{2} \d{2}:\d{2}/)).toBeVisible();
  // 未実行行は無い(4工程すべて成功ジョブあり)。
  await expect(jobsPanel.getByText("未実行")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("狭い画面幅(375px)でも文書 workspace がページを横スクロール(崩れ)させない", async ({ page }) => {
  await mockDocumentWorkspace(page);

  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/documents/doc-1");

  await expect(page.getByRole("heading", { name: "原本プレビュー" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "本文テキスト" })).toHaveAttribute(
    "aria-selected",
    "true"
  );
  await expect(page.getByRole("tab", { name: /Chunk \/ Citation/ })).toBeVisible();
  const panelScrollStyles = await page.getByTestId("document-inspector-pane").getByRole("tabpanel").evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      overflowY: style.overflowY,
      overscrollBehaviorY: style.overscrollBehaviorY,
    };
  });
  expect(panelScrollStyles).toEqual({ overflowY: "visible", overscrollBehaviorY: "auto" });
  await expectNoPageOverflow(page);
});

test("PDF 原本プレビューは左サイドバーを初期表示しない", async ({ page }) => {
  await mockDocumentWorkspace(page, { pdfPreview: true });

  await page.goto("/documents/doc-1");

  const pdfFrame = page.locator('iframe[title="policy.pdf"]');
  await expect(pdfFrame).toHaveAttribute(
    "src",
    /\/api\/documents\/doc-1\/recipes\/recipe-1\/content#page=1&pagemode=none&navpanes=0$/
  );
  await expectNoHorizontalOverflow(page);
});

test("原本プレビューで処理前/処理後を切り替え、ファイル準備から再処理できる", async ({
  page,
}) => {
  const state = await mockDocumentWorkspace(page, { preparedArtifact: true });

  await page.goto("/documents/doc-1");

  const previewPanel = page
    .getByRole("heading", { name: "原本プレビュー" })
    .locator("xpath=ancestor::section[1]");
  await expect(previewPanel.getByRole("tab", { name: "処理前" })).toBeVisible();
  await expect(previewPanel.getByRole("tab", { name: "処理後" })).toBeEnabled();
  await expect(previewPanel.getByText("経費申請")).toBeVisible();
  await expect(previewPanel.getByRole("link", { name: "ダウンロード" })).toHaveAttribute(
    "href",
    /\/api\/documents\/doc-1\/recipes\/recipe-1\/content\?disposition=attachment$/
  );

  await previewPanel.getByRole("tab", { name: "処理後" }).click();

  await expect(previewPanel.getByText("準備後ファイル")).toBeVisible();
  await expect(previewPanel.getByRole("link", { name: "ダウンロード" })).toHaveAttribute(
    "href",
    /\/api\/documents\/doc-1\/recipes\/recipe-1\/content\?variant=prepared&disposition=attachment$/
  );

  await page.getByRole("button", { name: "ファイル準備から再処理" }).click();
  await page.getByRole("button", { name: "再処理する" }).click();

  expect(state.enqueueRequest).toEqual({
    method: "POST",
    path: "/api/documents/doc-1/recipes/recipe-1/ingestion-jobs",
    force: null,
    phase: "PREPROCESS",
  });
  await expectNoPageOverflow(page);
});

test("処理前/処理後は共有の Tabs で、選択が読み上げられ矢印キーで切り替わる", async ({
  page,
}) => {
  // #396: 枠の中に Button を並べた手書きのセグメント（枠線が二重・隙間 0・選択状態を読み上げない）をやめた。
  await mockDocumentWorkspace(page, { preparedArtifact: true });

  await page.goto("/documents/doc-1");

  const previewPane = page.getByTestId("document-preview-pane");
  const previewTabs = previewPane.getByRole("tablist", { name: "原本プレビュー" });
  const before = previewTabs.getByRole("tab", { name: "処理前" });
  const after = previewTabs.getByRole("tab", { name: "処理後" });
  const download = previewPane.getByRole("link", { name: "ダウンロード" });
  await expect(before).toHaveAttribute("aria-selected", "true");
  await expect(after).toHaveAttribute("aria-selected", "false");
  await expect(previewPane.getByRole("tabpanel", { name: "処理前" })).toBeVisible();
  await expect(previewPane.getByRole("group")).toHaveCount(0);

  // 枠で囲まず（外枠なし）、タブは左右の枠線を持たず、間を空ける。
  const geometry = await previewTabs.evaluate((list) => {
    const tabs = Array.from(list.querySelectorAll<HTMLElement>('[role="tab"]'));
    const [first, second] = tabs.map((tab) => tab.getBoundingClientRect());
    const listStyle = getComputedStyle(list);
    return {
      listBorder: listStyle.borderTopWidth,
      tabSideBorders: tabs.map((tab) => getComputedStyle(tab).borderLeftWidth),
      gap: second.left - first.right,
    };
  });
  expect(geometry.listBorder).toBe("0px");
  expect(geometry.tabSideBorders).toEqual(["0px", "0px"]);
  expect(geometry.gap).toBeGreaterThan(0);

  // WAI-ARIA Tabs: ← → / Home / End で選択とフォーカスが移り、ダウンロードも選んだファイルに追従する。
  await before.focus();
  await page.keyboard.press("ArrowRight");
  await expect(after).toBeFocused();
  await expect(after).toHaveAttribute("aria-selected", "true");
  await expect(previewPane.getByRole("tabpanel", { name: "処理後" })).toBeVisible();
  await expect(previewPane.getByText("準備後ファイル")).toBeVisible();
  await expect(download).toHaveAttribute("href", /variant=prepared&disposition=attachment$/);
  await page.keyboard.press("Home");
  await expect(before).toBeFocused();
  await expect(before).toHaveAttribute("aria-selected", "true");
  await expect(download).toHaveAttribute("href", /content\?disposition=attachment$/);
  await page.keyboard.press("End");
  await expect(after).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowLeft");
  await expect(before).toHaveAttribute("aria-selected", "true");
  await expectNoPageOverflow(page);
});

test("抽出エクスポートは Markdown / HTML / JSON をダウンロード・コピーでき、Chunk は Chunk / Citation からダウンロードする", async ({
  page,
}) => {
  // #561: 画面の中で 4 形式を切り替えて見せるだけで持ち出せず、件数の Chunks が常に 0 だった。
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await mockDocumentWorkspace(page);
  await page.goto("/documents/doc-1");

  await page.getByRole("tab", { name: "抽出エクスポート" }).click();
  const exportPanel = page.getByTestId("document-extraction-export");
  // 形式の切り替え・本文の表示・件数（0 の誤り）を出さない。
  await expect(exportPanel.getByRole("tab")).toHaveCount(0);
  await expect(exportPanel.locator("pre")).toHaveCount(0);
  await expect(exportPanel.getByText("Chunks")).toHaveCount(0);

  for (const [format, label, extension] of [
    ["markdown", "Markdown", ".md"],
    ["html", "HTML", ".html"],
    ["json", "JSON", ".json"],
  ] as const) {
    const group = exportPanel.getByRole("group", { name: `${label} の操作` });
    await expect(exportPanel.getByTestId(`document-extraction-export-${format}`)).toContainText(
      `ファイル: ${extension}`
    );
    await expect(group.getByRole("button", { name: `${label} をコピー` })).toBeVisible();
    await expect(group.getByRole("link", { name: `${label} をダウンロード` })).toHaveAttribute(
      "href",
      `/api/documents/doc-1/recipes/recipe-1/extraction-export?format=${format}&download=true`
    );
  }

  // ダウンロードは原本プレビューの「ダウンロード」と同じく、backend の添付の応答へのリンク。
  // ファイル名（文書名 + レシピ）と content type は backend が決める（pytest で検証）。
  // `<a download>` の遷移は page.route で横取りできないため、ここでは属性だけを見る。
  await expect(exportPanel.getByRole("link", { name: "Markdown をダウンロード" })).toHaveAttribute(
    "download",
    ""
  );

  // キーボード: コピーを Enter で実行し、Tab で同じ行のダウンロードへ進む。
  const copyMarkdown = exportPanel.getByRole("button", { name: "Markdown をコピー" });
  await copyMarkdown.focus();
  await page.keyboard.press("Enter");
  await expect(copyMarkdown).toHaveAttribute("data-copy-state", "copied");
  await expect(exportPanel.getByRole("status").filter({ hasText: "Markdown をコピーしました" })).toHaveCount(1);
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    "<!-- page: 1 -->\n# 経費申請\n\n交通費は1000円です。"
  );
  await page.keyboard.press("Tab");
  await expect(exportPanel.getByRole("link", { name: "Markdown をダウンロード" })).toBeFocused();

  await exportPanel.getByRole("button", { name: "JSON をコピー" }).click();
  await expect(exportPanel.getByRole("button", { name: "JSON をコピー" })).toHaveAttribute("data-copy-state", "copied");
  await expect(exportPanel.getByRole("status").filter({ hasText: "JSON をコピーしました" })).toHaveCount(1);
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain(
    '"document_type": "規程"'
  );

  // 取得に失敗したら、操作した行の直下に残す。
  await page.route(/\/extraction-export\?format=html$/, (route) =>
    route.fulfill({
      status: 404,
      json: { data: null, error_messages: ["抽出結果が見つかりません。"], warning_messages: [] },
    })
  );
  await exportPanel.getByRole("button", { name: "HTML をコピー" }).click();
  await expect(exportPanel.getByTestId("document-extraction-export-html")).toContainText(
    "HTML をコピーできませんでした。"
  );

  // Chunk は「Chunk / Citation」タブから JSON でダウンロードする。
  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  const chunksLink = page.getByRole("link", { name: "保存済みの Chunk の JSON をダウンロード" });
  await expect(chunksLink).toHaveAttribute(
    "href",
    "/api/documents/doc-1/recipes/recipe-1/extraction-export?format=chunks&download=true"
  );
  await expect(chunksLink).toHaveAttribute("download", "");
  await expectNoPageOverflow(page);
});

test("抽出エクスポートは抽出結果の読み込み中・取得失敗を示す", async ({ page }) => {
  await mockDocumentWorkspace(page);
  let releaseExport: () => void = () => undefined;
  const exportReleased = new Promise<void>((resolve) => {
    releaseExport = resolve;
  });
  let failExport = false;
  await page.route(/\/extraction-export\?format=json$/, async (route) => {
    if (failExport) {
      await route.fulfill({
        status: 404,
        json: { data: null, error_messages: ["抽出結果が見つかりません。"], warning_messages: [] },
      });
      return;
    }
    await exportReleased;
    await route.fallback();
  });

  await page.goto("/documents/doc-1");
  await page.getByRole("tab", { name: "抽出エクスポート" }).click();
  await expect(page.getByTestId("document-extraction-export-loading")).toContainText(
    "抽出結果を読み込んでいます"
  );
  releaseExport();
  await expect(page.getByRole("link", { name: "JSON をダウンロード" })).toBeVisible();

  failExport = true;
  await page.reload();
  await page.getByRole("tab", { name: "抽出エクスポート" }).click();
  await expect(page.getByText("抽出結果を取得できません")).toBeVisible();
  await expect(page.getByRole("link", { name: "JSON をダウンロード" })).toHaveCount(0);
});

test("変換なしでも REVIEW では抽出確認を促す", async ({
  page,
}) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "REVIEW",
    preprocessProfile: "passthrough",
  });

  await page.goto("/documents/doc-1");

  await expect(page.getByText("抽出確認待ち", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "抽出から再処理" })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("PREPROCESSED ではファイル準備を確認し、解析へ進む承認を促す", async ({ page }) => {
  const state = await mockDocumentWorkspace(page, {
    documentStatus: "PREPROCESSED",
    preparedArtifact: true,
  });

  await page.goto("/documents/doc-1");

  await expect(page.getByText("ファイル準備確認待ち", { exact: true })).toBeVisible();
  await expect(
    page.getByText("ファイル準備が完了しました。処理後ファイルを確認し、問題なければ解析(抽出)へ進めてください。")
  ).toBeVisible();
  const approve = page.getByRole("button", { name: "承認して解析へ" });
  const actionButtons = approve.locator("xpath=parent::div").getByRole("button");
  await expect(actionButtons.nth(0)).toHaveText("承認して解析へ");
  await expect(actionButtons.nth(1)).toHaveText("ファイル準備から再処理");
  await expect(page.getByRole("button", { name: "却下" })).toHaveCount(0);
  await actionButtons.nth(1).click();
  await page.getByRole("button", { name: "再処理する" }).click();
  await expect.poll(() => state.enqueueRequest).toEqual({
    method: "POST",
    path: "/api/documents/doc-1/recipes/recipe-1/ingestion-jobs",
    force: null,
    phase: "PREPROCESS",
  });
  await expectNoPageOverflow(page);
});

test("CHUNKED では承認と段階別再処理だけを表示する", async ({ page }) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "CHUNKED",
    preparedArtifact: true,
  });

  await page.goto("/documents/doc-1");

  const approve = page.getByRole("button", { name: "承認して Embedding / 索引" });
  const actionButtons = approve.locator("xpath=parent::div").getByRole("button");
  await expect(actionButtons).toHaveText([
    "承認して Embedding / 索引",
    "ファイル準備から再処理",
    "抽出から再処理",
    "Chunk から再処理",
  ]);
  await expect(page.getByRole("button", { name: "却下" })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("PREPROCESSED で処理後ファイルが未保存なら危険バナーと再処理導線を出し承認させない", async ({
  page,
}) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "PREPROCESSED",
    brokenPreparedArtifact: true,
  });

  await page.goto("/documents/doc-1");

  await expect(page.getByText("ファイル準備確認待ち", { exact: true })).toBeVisible();
  // 変換成功なのに保存パス欠落 → 危険バナーで明示。
  await expect(
    page.getByText(
      "ファイル準備で変換した処理後ファイルを保存できませんでした。このままでは解析(抽出)へ進めません。ストレージ設定を確認し、ファイル準備を再実行してください。"
    )
  ).toBeVisible();
  // 409 になる承認は出さず、復旧導線(再処理)を出す。
  await expect(page.getByRole("button", { name: "承認して解析へ" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "ファイル準備を再実行" })).toBeVisible();
  await expect(page.getByRole("button", { name: /から再処理/ })).toHaveCount(0);
  // 「処理後」プレビューは保存物が無いので無効。
  await expect(page.getByRole("tab", { name: "処理後" })).toBeDisabled();
  await expectNoPageOverflow(page);
});

test("PREPROCESSED で処理後ファイルが欠落(converted以外)でも再処理導線を出し承認させない", async ({
  page,
}) => {
  // preparedArtifact 未指定 = preprocess_artifact が null(passthrough/欠落想定)。
  await mockDocumentWorkspace(page, { documentStatus: "PREPROCESSED" });

  await page.goto("/documents/doc-1");

  await expect(page.getByText("ファイル準備確認待ち", { exact: true })).toBeVisible();
  // converted フラグが無くても「見つからない」旨を明示。
  await expect(
    page.getByText(
      "処理後ファイルが見つかりません。このままでは解析(抽出)へ進めません。ファイル準備を再実行してください。"
    )
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "承認して解析へ" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "ファイル準備を再実行" })).toBeVisible();
  await expect(page.getByRole("button", { name: /から再処理/ })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("chunk 取得失敗時は workspace 内にエラー状態を表示する", async ({ page }) => {
  await mockDocumentWorkspace(page, { chunksError: true });

  await page.goto("/documents/doc-1");

  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  // 404 以外の失敗は 3 回まで再試行してからエラーにする（#311。約 7 秒）。
  await expect(page.getByText("chunk を取得できません")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("索引状態を確認して再読み込みしてください。")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("画像 preview は同一 surface 上で bbox overlay を位置決めする", { tag: "@responsive" }, async ({ page }) => {
  await mockDocumentWorkspace(page, { imagePreview: true });

  await page.goto("/documents/doc-1");

  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  const tableChunkButton = page
    .getByTestId("document-inspector-pane")
    .getByRole("tabpanel")
    .getByRole("button", { name: /交通費は1000円/ });
  await tableChunkButton.click();

  const surface = page.getByTestId("preview-image-surface");
  const overlay = page.getByTestId("bbox-content-overlay");
  await expect(surface).toBeVisible();
  await expect(overlay).toBeVisible();
  await expect(page.getByTestId("bbox-preview-page")).toHaveCount(0);
  await expect(overlay).toHaveAttribute("data-bbox-mode", "xyxy");
  await expect(overlay).toHaveAttribute("data-bbox-unit", "percent");
  await expect(overlay).toHaveAttribute(
    "style",
    /left: 0%; top: 0%; width: 100%; height: 40%;/
  );

  const surfaceBox = await surface.boundingBox();
  const overlayBox = await overlay.boundingBox();
  expect(surfaceBox).not.toBeNull();
  expect(overlayBox).not.toBeNull();
  expect(overlayBox!.width).toBeCloseTo(surfaceBox!.width, 1);
  expect(overlayBox!.height).toBeCloseTo(surfaceBox!.height * 0.4, 1);
  await expectNoHorizontalOverflow(page);
});

// #349: PDF はページ画像で表示し、要素の表示領域（要素ごとの bbox）を強調する。強調は回転・拡大に追従する。
async function relativeBox(child: Locator, parent: Locator) {
  const childBox = await child.boundingBox();
  const parentBox = await parent.boundingBox();
  expect(childBox).not.toBeNull();
  expect(parentBox).not.toBeNull();
  return {
    left: (childBox!.x - parentBox!.x) / parentBox!.width,
    top: (childBox!.y - parentBox!.y) / parentBox!.height,
    width: childBox!.width / parentBox!.width,
    height: childBox!.height / parentBox!.height,
  };
}

function expectBoxClose(
  actual: { left: number; top: number; width: number; height: number },
  expected: { left: number; top: number; width: number; height: number }
) {
  expect(actual.left).toBeCloseTo(expected.left, 2);
  expect(actual.top).toBeCloseTo(expected.top, 2);
  expect(actual.width).toBeCloseTo(expected.width, 2);
  expect(actual.height).toBeCloseTo(expected.height, 2);
}

test("章節ナビゲーションの章節を押すと、プレビューをその章節の開始ページへ移す", async ({ page }) => {
  await mockDocumentWorkspace(page, { pdfPreview: true, pdfPages: true });
  await page.route("**/api/documents/doc-1/sections**", async (route) => {
    const section = (id: string, title: string, start: number, end: number) => ({
      id, title, level: 1, page_start: start, page_end: end, origin: "extraction", source_section_id: id, edited: false,
    });
    await route.fulfill({
      json: {
        data: {
          document_id: "doc-1",
          source: "extraction",
          sections: [section("nav-1", "総則", 1, 1), section("nav-2", "料金表", 2, 2)],
          extraction_section_count: 2,
          page_count: 2,
          revision: null,
          updated_at: null,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  await page.goto("/documents/doc-1");
  await page.getByRole("tab", { name: "構造化要素" }).click();
  await expect(page.getByTestId("preview-page-status")).toHaveText("1 / 2");
  await page
    .getByTestId("extraction-navigation")
    .getByRole("button", { name: "料金表（p.2）をプレビューで開く" })
    .click();
  await expect(page.getByTestId("preview-page-status")).toHaveText("2 / 2");
});

test("PDF はページ画像で表示し、要素の表示領域を要素ごとに強調する", { tag: "@responsive" }, async ({ page }) => {
  await mockDocumentWorkspace(page, { pdfPreview: true, pdfPages: true, layoutRegions: true });

  await page.goto("/documents/doc-1");
  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  await page.getByTestId("document-inspector-pane").getByRole("tabpanel").getByRole("button", { name: /交通費は1000円/ }).click();

  const viewer = page.getByTestId("preview-viewer");
  await expect(viewer).toBeVisible();
  await expect(page.locator('iframe[title="policy.pdf"]')).toHaveCount(0);
  await expect(page.getByTestId("preview-page-status")).toHaveText("1 / 2");
  await expect(viewer.getByRole("img", { name: "policy.pdf p.1" })).toHaveAttribute(
    "src",
    /\/api\/documents\/doc-1\/recipes\/recipe-1\/preview-pages\/1\?variant=original&dpi=\d+$/
  );
  const surface = page.getByTestId("preview-image-surface");
  const overlays = page.getByTestId("bbox-content-overlay");
  await expect(overlays).toHaveCount(2);
  await expect(overlays.first()).toHaveAttribute("data-highlight-tone", "primary");
  await expect(overlays.first()).toHaveAttribute("data-bbox-unit", "absolute");
  await expect(page.getByText("p.1 の 2 か所を強調しています")).toBeVisible();
  expectBoxClose(await relativeBox(overlays.nth(0), surface), {
    left: 0.1,
    top: 0.1,
    width: 0.4,
    height: 0.1,
  });
  expectBoxClose(await relativeBox(overlays.nth(1), surface), {
    left: 0.5,
    top: 0.5,
    width: 0.4,
    height: 0.1,
  });

  // 強調のある別のページ（横向き）へ移ると、そのページの寸法に合わせて重ねる。
  await page.getByRole("button", { name: "強調のある 2 ページ目を表示" }).click();
  await expect(page.getByTestId("preview-page-status")).toHaveText("2 / 2");
  await expect(overlays).toHaveCount(1);
  expectBoxClose(await relativeBox(overlays.first(), surface), {
    left: 0,
    top: 0,
    width: 0.5,
    height: 0.5,
  });
  await expectNoHorizontalOverflow(page);
});

test("プレビューの回転・拡大の後も強調が同じ位置に重なり、キーボードでも操作できる", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockDocumentWorkspace(page, { pdfPreview: true, pdfPages: true, layoutRegions: true });

  await page.goto("/documents/doc-1");
  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  await page.getByTestId("document-inspector-pane").getByRole("tabpanel").getByRole("button", { name: /交通費は1000円/ }).click();

  const viewport = page.getByTestId("preview-viewport");
  const frame = page.getByTestId("preview-page-frame");
  const firstOverlay = page.getByTestId("bbox-content-overlay").first();
  await expect(firstOverlay).toBeVisible();

  // 右に 90 度回す: 回転前の (x, y, w, h) = (0.1, 0.1, 0.4, 0.1) は、外枠の中で
  // (1 - y - h, x, h, w) = (0.8, 0.1, 0.1, 0.4) になる（画像と強調を同じ層で回すため）。
  await page.getByRole("button", { name: "右に回転" }).click();
  await expect(viewport).toHaveAttribute("data-rotation", "90");
  expectBoxClose(await relativeBox(firstOverlay, frame), {
    left: 0.8,
    top: 0.1,
    width: 0.1,
    height: 0.4,
  });

  // 拡大しても、外枠に対する位置は変わらない。
  await page.getByRole("button", { name: "拡大", exact: true }).click();
  await page.getByRole("button", { name: "拡大", exact: true }).click();
  await expect(page.getByTestId("preview-zoom-status")).toHaveText("150%");
  await expect(viewport).toHaveAttribute("data-fit-mode", "zoom");
  expectBoxClose(await relativeBox(firstOverlay, frame), {
    left: 0.8,
    top: 0.1,
    width: 0.1,
    height: 0.4,
  });
  // 拡大するとビューポートの中でスクロール（パン）できる。
  await expect
    .poll(() => viewport.evaluate((element) => element.scrollWidth > element.clientWidth))
    .toBe(true);

  // キーボード: ビューポートにフォーカスして操作する。
  await viewport.focus();
  await page.keyboard.press("Shift+R");
  await expect(viewport).toHaveAttribute("data-rotation", "0");
  expectBoxClose(await relativeBox(firstOverlay, frame), {
    left: 0.1,
    top: 0.1,
    width: 0.4,
    height: 0.1,
  });
  await page.keyboard.press("-");
  await expect(page.getByTestId("preview-zoom-status")).toHaveText("125%");
  await page.keyboard.press("0");
  await expect(viewport).toHaveAttribute("data-fit-mode", "fit-page");
  await expect(page.getByRole("button", { name: "全体を表示" })).toHaveAttribute(
    "aria-pressed",
    "true"
  );
  // 全体表示ではページ全体がビューポートに収まる。
  const viewportBox = await viewport.boundingBox();
  const frameBox = await frame.boundingBox();
  expect(frameBox!.height).toBeLessThanOrEqual(viewportBox!.height);
  expect(frameBox!.width).toBeLessThanOrEqual(viewportBox!.width);
  await page.keyboard.press("w");
  await expect(viewport).toHaveAttribute("data-fit-mode", "fit-width");
  await page.keyboard.press("r");
  await expect(viewport).toHaveAttribute("data-rotation", "90");
  await page.keyboard.press("PageDown");
  await expect(page.getByTestId("preview-page-status")).toHaveText("2 / 2");
  // 回転はページごと。2 ページ目は回していない。
  await expect(viewport).toHaveAttribute("data-rotation", "0");
  await page.keyboard.press("Home");
  await expect(page.getByTestId("preview-page-status")).toHaveText("1 / 2");
  await expect(viewport).toHaveAttribute("data-rotation", "90");
  await expectNoHorizontalOverflow(page);
});

// 文書詳細のプレビューは、幅に合わせたときに 1 ページ全体が縦スクロールなしで入る高さにする（#559）。
// desktop（1280 / 1920）と 375px で、開いた直後の状態・拡大・ページ送りを確かめる。
for (const viewportCase of [
  { name: "1280", width: 1280, height: 800 },
  { name: "1920", width: 1920, height: 1080 },
  { name: "375", width: 375, height: 812 },
] as const) {
  test(`プレビューは開いた直後に 1 ページ全体を縦スクロールなしで表示する (${viewportCase.name})`, async ({
    page,
  }, testInfo) => {
    // 幅は setViewportSize で決めるので、desktop のプロジェクトだけで実行する（mobile と同じ内容を 2 回走らせない）。
    test.skip(testInfo.project.name !== "desktop", "viewport は各ケースで指定する");
    await page.setViewportSize({ width: viewportCase.width, height: viewportCase.height });
    await mockDocumentWorkspace(page, { pdfPreview: true, pdfPages: true, layoutRegions: true });

    await page.goto("/documents/doc-1");

    const pane = page.getByTestId("document-preview-pane");
    const inspectorPane = page.getByTestId("document-inspector-pane");
    const viewport = page.getByTestId("preview-viewport");
    const frame = page.getByTestId("preview-page-frame");
    await expect(page.getByTestId("preview-viewer")).toHaveAttribute("data-sizing", "page");
    await expect(viewport.getByRole("img", { name: "policy.pdf p.1" })).toBeVisible();
    await expect(viewport).toHaveAttribute("data-fit-mode", "fit-width");

    // 開いた直後: ビューポートの中に縦・横のスクロールが無く、ページの枠全体がビューポートの中にある。
    const metrics = await viewport.evaluate((element) => ({
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
    }));
    expect(metrics.scrollHeight).toBeLessThanOrEqual(metrics.clientHeight);
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth);
    const viewportBox = (await viewport.boundingBox())!;
    const frameBox = (await frame.boundingBox())!;
    expect(frameBox.y).toBeGreaterThanOrEqual(viewportBox.y);
    expect(frameBox.y + frameBox.height).toBeLessThanOrEqual(viewportBox.y + viewportBox.height);
    // 幅に合わせた表示（ページの幅 ≒ ビューポートの幅）。
    expect(frameBox.width).toBeGreaterThan(viewportBox.width - 32);

    const screenHeight = viewportCase.height;
    const paneBox = (await pane.boundingBox())!;
    if (viewportCase.width >= 1280) {
      // xl 以上: 右ペインは左と同じ高さ。下限は今までの 1 画面分（上下 1rem ずつの余白を除く）。
      expect((await inspectorPane.boundingBox())!.height).toBeCloseTo(paneBox.height, 0);
      expect(paneBox.height).toBeGreaterThanOrEqual(screenHeight - 28 - 1);
      const inspectorWidth = (await inspectorPane.boundingBox())!.width;
      if (viewportCase.width >= 1536) {
        // 2xl 以上はプレビュー 2 : 右 1 で、右は 35rem（14px ルートで 490px）を下限にする（#579）。
        expect(inspectorWidth).toBeGreaterThanOrEqual(490 - 1);
        expect(paneBox.width / inspectorWidth).toBeCloseTo(2, 1);
      } else {
        // xl（1280px〜1535px）は今までどおりほぼ半分ずつ。
        expect(paneBox.width / inspectorWidth).toBeCloseTo(1.05, 1);
      }
    } else {
      // xl 未満の縦積み: 1 画面分の枠に小さなページが浮かないよう、ページの形の高さに詰める。
      expect(paneBox.height).toBeLessThan(screenHeight - 28);
    }
    if (viewportCase.width !== 1280) {
      // 下限にかからないときは、ビューポートはページの高さ + ステージの余白（と数 px の遊び）だけ。
      expect(viewportBox.height - frameBox.height).toBeLessThan(32);
    }
    // 目視の記録: ペインの下端（ページの下端とビューアの枠）が見える位置までスクロールして撮る。
    await pane.evaluate((element) => element.scrollIntoView({ block: "end" }));
    await page.screenshot({
      path: testInfo.outputPath(`preview-pane-${viewportCase.width}.png`),
    });

    // 拡大してもペインの高さは変わらず、ページはビューアの中でスクロールする。
    await viewport.focus();
    for (let index = 0; index < 5; index += 1) await page.keyboard.press("+");
    await expect(page.getByTestId("preview-zoom-status")).toHaveText("300%");
    await expect
      .poll(() => viewport.evaluate((element) => element.scrollHeight > element.clientHeight))
      .toBe(true);
    expect((await pane.boundingBox())!.height).toBeCloseTo(paneBox.height, 0);

    // 横長の 2 ページ目へ送っても、ペインの高さは変わらない（文書で最も縦長のページで高さを決める）。
    await page.keyboard.press("w");
    await page.keyboard.press("PageDown");
    await expect(page.getByTestId("preview-page-status")).toHaveText("2 / 2");
    await expect(viewport.getByRole("img", { name: "policy.pdf p.2" })).toBeVisible();
    expect((await pane.boundingBox())!.height).toBeCloseTo(paneBox.height, 0);
    await expect
      .poll(() => viewport.evaluate((element) => element.scrollHeight <= element.clientHeight))
      .toBe(true);
    await expectNoHorizontalOverflow(page);
  });
}

test("ページ画像の寸法が届く前は A4 縦で領域を取り、届いた後も高さがほとんど変わらない", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop の幅で確かめる");
  await page.setViewportSize({ width: 1920, height: 1080 });
  await mockDocumentWorkspace(page, { pdfPreview: true, pdfPages: true, layoutRegions: true });
  // ページ一覧の応答を遅らせ、読み込み中の表示の高さを測る。
  let releasePages: () => void = () => {};
  const pagesGate = new Promise<void>((resolve) => {
    releasePages = resolve;
  });
  await page.route(
    /\/api\/documents\/doc-1(?:\/recipes\/recipe-1)?\/preview-pages(?:\?|$)/,
    async (route) => {
      await pagesGate;
      await route.fallback();
    }
  );

  await page.goto("/documents/doc-1");
  const pane = page.getByTestId("document-preview-pane");
  await expect(page.getByTestId("preview-loading")).toBeVisible();
  const loadingHeight = (await pane.boundingBox())!.height;
  releasePages();
  await expect(page.getByTestId("preview-viewport").getByRole("img")).toBeVisible();
  const loadedHeight = (await pane.boundingBox())!.height;
  // A4 縦（1 : 1.414）と、このページ（US Letter 1 : 1.294）の違いの分だけ変わる（1 割未満）。
  expect(Math.abs(loadedHeight - loadingHeight) / loadedHeight).toBeLessThan(0.1);
});

test("「強調した位置へ移動」は画面の外にある強調をページごと見せる", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop の幅で確かめる");
  // 画面が低いと、プレビューのページの上の方の強調でも開いた直後は画面の外にある。
  await page.setViewportSize({ width: 1440, height: 600 });
  await mockDocumentWorkspace(page, { pdfPreview: true, pdfPages: true, layoutRegions: true });
  await page.goto("/documents/doc-1");
  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  await page
    .getByTestId("document-inspector-pane")
    .getByRole("tabpanel")
    .getByRole("button", { name: /交通費は1000円/ })
    .click();
  // ビューアが中央へ寄せるのは、最初の主の強調（querySelector と同じ）。
  const highlight = page.locator("[data-highlight-tone='primary']").first();
  await expect(highlight).toBeVisible();
  // chunk を選んだだけではページは動かさない。主ページを先頭へ戻して強調を画面の外に置く。
  await page.locator("main").evaluate((element) => element.scrollTo(0, 0));
  await expect.poll(() => highlight.evaluate((element) => element.getBoundingClientRect().top)).toBeGreaterThan(600);

  await page.getByRole("button", { name: "強調した位置へ移動" }).click();
  await expect
    .poll(() =>
      highlight.evaluate((element) => {
        const box = element.getBoundingClientRect();
        return box.top >= 0 && box.bottom <= window.innerHeight;
      })
    )
    .toBe(true);
});

test("明示された xywh bbox mode で citation overlay を位置決めする", async ({ page }) => {
  await mockDocumentWorkspace(page, { chunkBboxMode: "xywh" });

  await page.goto("/documents/doc-1");

  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  await page.getByTestId("document-inspector-pane").getByRole("tabpanel").getByRole("button", { name: /交通費は1000円/ }).click();

  await expect(
    page.getByText(/位置: p\.1 \/ bbox x=25\.0% y=10\.0% w=50\.0% h=40\.0%/)
  ).toBeVisible();
  const overlay = page.getByTestId("bbox-overlay");
  await expect(overlay).toHaveAttribute("data-bbox-mode", "xywh");
  await expect(overlay).toHaveAttribute("data-bbox-unit", "percent");
  await expect(overlay).toHaveAttribute(
    "style",
    /left: 25%; top: 10%; width: 50%; height: 40%;/
  );
  await expectNoHorizontalOverflow(page);
});

test("metadata の bbox unit を優先して citation overlay を位置決めする", async ({ page }) => {
  await mockDocumentWorkspace(page, { chunkBboxUnit: "absolute" });

  await page.goto("/documents/doc-1");

  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  await page.getByTestId("document-inspector-pane").getByRole("tabpanel").getByRole("button", { name: /交通費は1000円/ }).click();

  await expect(
    page.getByText(/位置: p\.1 \/ bbox x=4\.1% y=1\.3% w=4\.1% h=3\.8%/)
  ).toBeVisible();
  const overlay = page.getByTestId("bbox-overlay");
  await expect(overlay).toHaveAttribute("data-bbox-mode", "xyxy");
  await expect(overlay).toHaveAttribute("data-bbox-unit", "absolute");
  await expect(overlay).toHaveAttribute(
    "style",
    /left: 4\.08497.*%; top: 1\.26263.*%; width: 4\.08497.*%; height: 3\.78788.*%;/
  );
  await expectNoHorizontalOverflow(page);
});

test("element_id 深リンクは構造化 block をフォーカスして preview bbox に定位する", async ({ page }) => {
  await mockDocumentWorkspace(page);

  await page.goto("/documents/doc-1?element_id=tbl-1");

  const extractionPanel = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  const tableElementButton = extractionPanel.getByRole("button", {
    name: /交通費は1000円[\s\S]*tbl-1/,
  });
  await expect(tableElementButton).toHaveAttribute("aria-pressed", "true");
  await expect(tableElementButton).toBeFocused();
  await expect(
    page.getByText(/位置: p\.1 \/ bbox x=0\.0% y=0\.0% w=100\.0% h=40\.0%/)
  ).toBeVisible();
  await expect(page.getByTestId("bbox-preview-overlay")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("formula cell 深リンクは表セルをフォーカスして cell bbox に定位する", async ({ page }) => {
  await mockDocumentWorkspace(page);

  await page.goto("/documents/doc-1?chunk_id=doc-1:1&table_id=tbl-1&formula_cell_ref=B2&page=1");

  const extractionPanel = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  const targetCell = extractionPanel
    .getByTestId("extraction-table-cell")
    .filter({ hasText: "B2" });
  await expect(targetCell).toHaveAttribute("aria-pressed", "true");
  await expect(targetCell).toBeFocused();
  await expect(targetCell).toContainText("1000円");
  await expect(
    page.getByText(/位置: p\.1 \/ bbox x=50\.0% y=10\.0% w=25\.0% h=20\.0%/)
  ).toBeVisible();
  const overlay = page.getByTestId("bbox-overlay");
  await expect(overlay).toBeVisible();
  await expect(overlay).toHaveAttribute("data-bbox-mode", "xyxy");
  await expect(overlay).toHaveAttribute("data-bbox-unit", "percent");
  await expect(overlay).toHaveAttribute(
    "style",
    /left: 50%; top: 10%; width: 25%; height: 20%;/
  );
  await expectNoHorizontalOverflow(page);
});

test("citation 深リンクの bbox fallback で chunk 欠損時も preview に定位する", async ({ page }) => {
  await mockDocumentWorkspace(page, { chunksEmpty: true });

  await page.goto(
    "/documents/doc-1?chunk_id=missing:chunk&page=1&bbox=10,15,40,30&bbox_mode=xywh&bbox_unit=percent&page_width=612&page_height=792"
  );

  await expect(
    page.getByText(/位置: p\.1 \/ bbox x=10\.0% y=15\.0% w=40\.0% h=30\.0%/)
  ).toBeVisible();
  await expect(page.getByText("chunk はまだ作成されていません。")).toBeVisible();
  const overlay = page.getByTestId("bbox-overlay");
  await expect(overlay).toBeVisible();
  await expect(overlay).toHaveAttribute("data-bbox-mode", "xywh");
  await expect(overlay).toHaveAttribute("data-bbox-unit", "percent");
  await expect(overlay).toHaveAttribute(
    "style",
    /left: 10%; top: 15%; width: 40%; height: 30%;/
  );
  await expectNoHorizontalOverflow(page);
});

test("citation 深リンクは page rotation を反映して bbox overlay を定位する", async ({ page }) => {
  await mockDocumentWorkspace(page, { chunksEmpty: true });

  await page.goto(
    "/documents/doc-1?chunk_id=missing:chunk&page=1&bbox=10,15,40,30&bbox_mode=xywh&bbox_unit=percent&page_width=612&page_height=792&page_rotation=90"
  );

  await expect(
    page.getByText(/位置: p\.1 \/ bbox x=15\.0% y=50\.0% w=30\.0% h=40\.0%/)
  ).toBeVisible();
  const overlay = page.getByTestId("bbox-overlay");
  await expect(overlay).toBeVisible();
  await expect(overlay).toHaveAttribute("data-bbox-mode", "xywh");
  await expect(overlay).toHaveAttribute("data-bbox-unit", "percent");
  await expect(overlay).toHaveAttribute(
    "style",
    /left: 15%; top: 50%; width: 30%; height: 40%;/
  );
  await expectNoHorizontalOverflow(page);
});

test("抽出セグメント失敗時は原因と復旧導線を表示する", async ({ page }) => {
  const state = await mockDocumentWorkspace(page, { segmentError: true });

  await page.goto("/documents/doc-1");

  // 段の一覧は既定で畳むが、失敗した段があるときは開く（#2001）。
  const segments = page.getByTestId("document-segments");
  await expect(segments).toHaveAttribute("open", "");
  // 失敗 code は segment パネルに残す。
  await expect(page.getByText("enterprise_ai_response_validation_error")).toBeVisible();
  // 原因(confidence...)は上部の原因バナーに 1 本化し、diagnostics に埋もれさせない（§9 P2/P3）。
  await expect(page.getByText(/confidence/)).toHaveCount(1);
  await expect(page.getByRole("alert").filter({ hasText: /confidence/ })).toBeVisible();
  // 復旧導線(recovery hint + retry)は段の一覧の先頭に 1 つだけ出す（失敗・取消の段をまとめて再試行する。#2034）。
  // 始めていない段は取消で理由を添える。
  await expect(
    segments.getByText(
      "再試行すると、失敗・取消の段だけを解析し直します（完了した段はそのまま使います）。"
    )
  ).toBeVisible();
  await expect(
    segments.getByText("前の段の解析が失敗・取消になったため、この段は解析していません。", {
      exact: false,
    })
  ).toBeVisible();
  await expect(segments.getByRole("button", { name: /再試行/ })).toHaveCount(1);
  await segments.getByRole("button", { name: "失敗した段を再試行" }).click();
  await expect.poll(() => state.retryRequest).toEqual({
    method: "POST",
    path: "/api/documents/doc-1/ingestion-segments/retry",
    recipeId: "recipe-1",
  });
  await expectNoHorizontalOverflow(page);
});

// 段の再試行は 1 つの job で失敗・取消の段をまとめて解析し直すので、ボタンは段の一覧の先頭の 1 つだけ。
// 押したボタンだけが送信の間回り、結果（受付・失敗）はそのボタンの直下の 1 か所に出る（#2034）。
test(
  "段の再試行はボタン 1 つで、押したボタンだけが回り、結果はその直下の 1 か所に出る（#2034）",
  { tag: "@responsive" },
  async ({ page }) => {
    await mockDocumentWorkspace(page, { segmentError: true, segmentCount: 3 });
    let releaseRetry: () => void = () => {};
    const retryHeld = new Promise<void>((resolve) => {
      releaseRetry = resolve;
    });
    let retryCalls = 0;
    // 1 回目は失敗、2 回目は受付（送信の間はボタンを回したまま止めておく）。
    await page.route("**/api/documents/doc-1/ingestion-segments/retry**", async (route) => {
      retryCalls += 1;
      if (retryCalls === 1) {
        await route.fulfill({
          status: 400,
          json: { data: null, error_messages: ["再試行できる段がありません。"], warning_messages: [] },
        });
        return;
      }
      await retryHeld;
      await route.fallback();
    });

    await page.goto("/documents/doc-1");
    const segments = page.getByTestId("document-segments");
    await expect(segments).toHaveAttribute("open", "");
    const retryButtons = segments.getByRole("button", { name: /再試行/ });
    await expect(retryButtons).toHaveCount(1);
    const retry = segments.getByRole("button", { name: "失敗した段を再試行" });
    const retryArea = page.getByTestId("document-segments-retry");

    // 失敗: 同じ場所（ボタンの直下）に danger の結果を 1 つだけ出す。
    await retry.click();
    const failure = retryArea.getByRole("alert").filter({ hasText: "再試行できる段がありません。" });
    await expect(failure).toBeVisible();
    await expect(page.getByText("再試行できる段がありません。")).toHaveCount(1);
    await expectBelow(failure, retry);

    // 受付: 送信の間は押したボタンだけが回り、受付の結果はボタンの直下に 1 つだけ出る。
    await retry.click();
    await expect(retry).toHaveAttribute("aria-busy", "true");
    await expect(segments.locator("[aria-busy=true]")).toHaveCount(1);
    releaseRetry();
    const queued = retryArea.getByText("失敗した段の再試行をキューに追加しました。");
    await expect(queued).toBeVisible();
    await expect(page.getByText("失敗した段の再試行をキューに追加しました。")).toHaveCount(1);
    await expect(failure).toHaveCount(0);
    await expectBelow(queued, retry);
    // 段の一覧は開いたまま（結果が畳まれて見えなくならない）。
    await expect(segments).toHaveAttribute("open", "");
    await expectNoHorizontalOverflow(page);
  }
);

// 工程の再試行・再処理は、文書の先頭の状態の案内（失敗の Banner）の中に置き、押した結果（失敗・開始）は
// その案内の中のボタンの直下に 1 つだけ出す。再試行で解決したら失敗の案内は消える（#2034）。
test(
  "工程の再試行は失敗の案内の中のボタンで行い、結果はその直下の 1 か所に出て、解決すると案内が消える（#2034）",
  { tag: "@responsive" },
  async ({ page }) => {
    await mockDocumentWorkspace(page, {
      documentStatus: "ERROR",
      latestJobStatus: "FAILED",
      latestJobPhase: "EXTRACT",
      preparedArtifact: true,
      autoRefreshAfterEnqueue: true,
    });
    // 1 回目の投入は失敗させる。
    await page.route(
      "**/api/documents/doc-1/recipes/recipe-1/ingestion-jobs**",
      async (route) => {
        await route.fulfill({
          status: 400,
          json: { data: null, error_messages: ["この工程は開始できません。"], warning_messages: [] },
        });
      },
      { times: 1 }
    );

    await page.goto("/documents/doc-1");
    const block = page.getByTestId("document-status-actions");
    const failureBanner = block.getByRole("alert").filter({ hasText: "抽出で失敗しました" });
    await expect(failureBanner).toBeVisible();
    // 操作は失敗の案内の中にあり、カードの別の場所に同じ操作を重ねない。
    const retry = failureBanner.getByRole("button", { name: "抽出を再実行" });
    await expect(retry).toBeVisible();
    await expect(page.getByRole("button", { name: "抽出を再実行" })).toHaveCount(1);
    await expect(failureBanner.getByRole("button", { name: "ファイル準備から再処理" })).toBeVisible();
    // 案内（と操作）は処理レシピの直下にある。押した後の結果も同じ場所に出ることを、処理レシピとの位置で確かめる。
    const recipes = page.locator('section[aria-label="処理レシピ"]');
    await expectBelow(block, recipes);
    const actionRow = retry.locator("xpath=..");

    // 失敗: 同じ案内の中のボタンの直下に danger の結果を 1 つだけ出す。
    await retry.click();
    await page.getByRole("alertdialog").getByRole("button", { name: "再実行する" }).click();
    const startFailed = block.getByText("抽出を開始できませんでした。", { exact: false });
    await expect(startFailed).toBeVisible();
    await expect(page.getByText("抽出を開始できませんでした。", { exact: false })).toHaveCount(1);
    await expectBelow(startFailed, actionRow);

    // 再試行の受付: 開始の文が同じ場所（押したボタンがあった位置の直下）に 1 つだけ出て、失敗の案内は消える。
    await retry.click();
    await page.getByRole("alertdialog").getByRole("button", { name: "再実行する" }).click();
    const started = block.getByText("抽出を開始しました。完了まで状態を更新します。");
    await expect(started).toBeVisible();
    await expect(page.getByText("抽出を開始しました。完了まで状態を更新します。")).toHaveCount(1);
    await expect(page.getByText("抽出を開始できませんでした。", { exact: false })).toHaveCount(0);
    await expect(page.getByText("抽出で失敗しました")).toHaveCount(0, { timeout: 9_000 });
    // 案内が消えた後も、結果はカードの同じ場所（処理レシピの直下）に出る。
    await expectBelow(started, recipes);
    await expectNoHorizontalOverflow(page);
  }
);

test("文書 workspace はこの文書の取込 job と時間線を表示する", async ({ page }) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "INGESTING",
    latestJobStatus: "RUNNING",
    latestJobStartedAt: new Date(Date.now() - 2_000).toISOString(),
    pdfPreview: true,
  });

  await page.goto("/documents/doc-1");

  const panel = page
    .getByRole("heading", { name: "工程ごとの実行状況" })
    .locator("xpath=ancestor::section[1]");
  await expect(page.getByText("解析（抽出）中")).toBeVisible();
  await expect(page.getByText("取込中", { exact: true })).toHaveCount(0);
  await expect(panel.getByText("実行中")).toBeVisible();
  await expect(panel.getByText("抽出", { exact: true })).toBeVisible();
  await expect(panel.getByText(/job: job-runn/)).toBeVisible();
  await expect(panel.getByText("開始")).toBeVisible();
  // ファイル準備は通しジョブ内で完了済み、後続2工程は未実行として工程順に見える。
  await expect(panel.getByText("未実行")).toHaveCount(2);
  // 初回試行(1/1 相当)は試行メトリクスを出さない。
  await expect(panel.getByText("1/3 回")).toHaveCount(0);
  const elapsed = panel.getByTestId("ingestion-job-elapsed-extract");
  const firstElapsed = await elapsed.textContent();
  await expect.poll(() => elapsed.textContent(), { timeout: 4_000 }).not.toBe(firstElapsed);
  await expect(
    panel.getByText("この job が完了するまで文書状態・segment・抽出結果を自動更新します。")
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("失敗した取込 job は取込中 banner を残さず原因を表示する", async ({ page }) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "ERROR",
    latestJobStatus: "FAILED",
    latestJobPhase: "EXTRACT",
    pdfPreview: true,
  });

  await page.goto("/documents/doc-1");

  await expect(
    page.getByText("抽出を実行しています。完了まで状態を更新します。")
  ).toHaveCount(0);
  const panel = page
    .getByRole("heading", { name: "工程ごとの実行状況" })
    .locator("xpath=ancestor::section[1]");
  await expect(panel.getByText("失敗", { exact: true })).toBeVisible();
  // 原因本文は上部の状態メッセージへ1本化し、診断パネルでは重複表示しない。
  await expect(panel.getByText("原因")).toHaveCount(0);
  await expect(panel.getByText("取込処理に失敗しました。")).toHaveCount(0);
  await expect(
    page.getByRole("alert").filter({ hasText: "取込処理に失敗しました。" })
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("同じ取込エラー原因は上部の原因バナーに 1 本化する", async ({ page }) => {
  const message =
    "選択した文書解析サービス（Unstructured）で解析処理が失敗しました。エラーコード: unstructured_adapter_failed";
  await mockDocumentWorkspace(page, {
    documentStatus: "ERROR",
    latestJobStatus: "FAILED",
    segmentError: true,
    sharedErrorMessage: message,
  });

  await page.goto("/documents/doc-1");

  const summary = "選択した文書解析サービス（Unstructured）で解析処理が失敗しました。";
  // 原因は画面全体で 1 回だけ表示する（§9 P2）。
  await expect(page.getByText(summary, { exact: true })).toHaveCount(1);
  // 上部の原因バナー(role=alert)に昇格する。エラーコードは本文から分け、「詳細」に出す（失敗なので開く。§10。#705）。
  const banner = page.getByRole("alert").filter({ hasText: summary });
  await expect(banner).toBeVisible();
  await expect(banner.getByText("エラーコード: unstructured_adapter_failed")).toBeVisible();
  await expect(banner.getByText(message)).toHaveCount(0);
  // job パネルでは再掲しない。
  const jobPanel = page
    .getByRole("heading", { name: "工程ごとの実行状況" })
    .locator("xpath=ancestor::section[1]");
  await expect(jobPanel.getByText(message)).toHaveCount(0);
  await expect(jobPanel.getByText(summary)).toHaveCount(0);
  // segment の code と復旧導線は残す。
  const segmentPanel = page.getByTestId("document-segments");
  // segment のエラーコードは手書きのチップではなく「詳細」に出す（失敗なので開く。§10。#723）。
  await expect(
    segmentPanel.getByText("エラーコード: enterprise_ai_response_validation_error")
  ).toBeVisible();
  await expect(segmentPanel.getByRole("button", { name: "失敗した段を再試行" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("PDF 抽出中はページ単位の進捗を表示する", async ({ page }) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "INGESTING",
    latestJobStatus: "RUNNING",
    pdfPreview: true,
    progressScenario: "pdf17",
  });

  await page.goto("/documents/doc-1");

  await expect(page.getByText("抽出: 9 / 17 ページ完了")).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "抽出: 9 / 17 ページ完了" })).toBeVisible();
  // 主の bar の下の段の帯に、段ごとの状態とページの範囲を出す（#2001）。
  const strip = page.getByTestId("ingestion-segment-strip");
  await expect(strip.getByRole("listitem", { name: "p.10-13: 処理中" })).toBeVisible();
  await expect(strip.getByRole("listitem", { name: "p.14-17: 待機中" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test(
  "通しジョブ内の抽出は Docling のページの進捗と図・表の説明の件数を表示する（#1999）",
  { tag: "@responsive" },
  async ({ page }) => {
    const updatedAt = new Date(Date.now() - 2_000).toISOString();
    await mockDocumentWorkspace(page, {
      documentStatus: "INGESTING",
      latestJobStatus: "RUNNING",
      latestJobPhase: "PREPROCESS",
      latestJobStartedAt: new Date(Date.now() - 14 * 60_000).toISOString(),
      pdfPreview: true,
      progressScenario: "doclingInline",
      jobProgress: {
        stage: "vision",
        parse: { engine: "docling", unit: "page", completed: 82, failed: 0, total: 82, finished: true },
        vision: {
          phase: "pictures",
          succeeded: 12,
          failed: 0,
          skipped: 2,
          running: 1,
          pending: 25,
          total_known: 40,
          total_final: false,
          current_page: 37,
          current_kind: "picture",
          finished: false,
        },
        updated_at: updatedAt,
      },
    });

    await page.goto("/documents/doc-1");

    const panel = page
      .getByRole("heading", { name: "工程ごとの実行状況" })
      .locator("xpath=ancestor::section[1]");
    const stages = panel.getByTestId("extract-stage-progress");
    await expect(stages).toHaveCount(1);
    // 抽出の工程の行（起点の工程の job の中で実行。#2001 で「通しジョブ内で実行」の文言はやめた）に出す。
    await expect(panel.getByText("通しジョブ内で実行")).toHaveCount(0);
    await expect(
      stages.getByRole("progressbar", { name: "ページの解析（Docling）: 完了（82 ページ）" })
    ).toBeVisible();
    await expect(
      stages.getByRole("progressbar", {
        name: "図・表の説明（Vision）: 14 / 40 件（表の対象を探しています）",
      })
    ).toHaveAttribute("value", "14");
    await expect(
      stages.getByText("Vision モデルの応答を待っています（p.37 の図）。1 件に数十秒かかることがあります。")
    ).toBeVisible();
    // 進んでいる間は「最後に進んでから」を出さない。
    await expect(stages.getByText(/最後に進んでから/)).toHaveCount(0);
    // セグメントからの「抽出: 0 / 82 ページ完了」は重ねない。segment は待機中ではなく処理中。
    await expect(page.getByText(/抽出: 0 \/ 82/)).toHaveCount(0);
    const strip = panel.getByTestId("ingestion-segment-strip");
    await expect(strip.locator('[data-status="RUNNING"]')).toHaveCount(9);
    await expect(strip.locator('[data-status="QUEUED"]')).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
  }
);

function segmentedProgress(statuses: string[], runningCompleted = 4) {
  return statuses.map((status, index) => {
    const start = index * 10 + 1;
    const end = Math.min(start + 9, 82);
    return {
      segment_id: `doc-1:p${start}-${end}`,
      page_start: start,
      page_end: end,
      status,
      completed: status === "RUNNING" ? runningCompleted : status === "SUCCEEDED" ? end - start + 1 : 0,
      reused: false,
    };
  });
}

test(
  "PDF を段ごとに解析するときは「抽出: N / M ページ」・段の帯・段の一覧を同じ記録から出す（#2001）",
  { tag: "@responsive" },
  async ({ page }) => {
    await mockDocumentWorkspace(page, {
      documentStatus: "INGESTING",
      latestJobStatus: "RUNNING",
      latestJobPhase: "PREPROCESS",
      pdfPreview: true,
      // 抽出セグメントの行は前の試行の待機中のまま。今の状態は job の進捗（段の状態）が正。
      progressScenario: "segmentedStale",
      jobProgress: {
        stage: "parse",
        parse: {
          engine: "docling",
          unit: "page",
          completed: 4,
          failed: 0,
          total: 10,
          finished: false,
          segments: segmentedProgress([
            "SUCCEEDED",
            "SUCCEEDED",
            "RUNNING",
            "QUEUED",
            "QUEUED",
            "QUEUED",
            "QUEUED",
            "QUEUED",
            "QUEUED",
          ]),
        },
        vision: null,
        updated_at: new Date(Date.now() - 2_000).toISOString(),
      },
    });

    await page.goto("/documents/doc-1");

    const panel = page
      .getByRole("heading", { name: "工程ごとの実行状況" })
      .locator("xpath=ancestor::section[1]");
    const stages = panel.getByTestId("extract-stage-progress");
    // 主の表示は 1 か所: 段ごとに進む「抽出: N / M ページ完了」。
    await expect(
      stages.getByRole("progressbar", { name: "抽出: 20 / 82 ページ完了" })
    ).toHaveAttribute("value", "20");
    await expect(
      stages.getByText("Docling が p.21-30（段 3 / 9）を解析しています（10 ページ中 4 ページ）。")
    ).toBeVisible();
    await expect(panel.getByText("ページの解析（Docling）", { exact: false })).toHaveCount(0);

    // 主の bar の下の段の帯。色だけでなくアイコン・読み上げ・凡例で状態を区別する。
    const strip = stages.getByTestId("ingestion-segment-strip");
    await expect(strip.getByRole("listitem")).toHaveCount(9);
    await expect(strip.getByRole("list")).toHaveAccessibleName(
      "段ごとの解析の状態: 完了 2・処理中 1・待機中 6"
    );
    await expect(strip.locator("svg").first()).toBeVisible();
    await expect(strip.getByText("待機中 6")).toBeVisible();

    // 段の一覧は既定で畳み、開くと段のカードも同じ記録（job の進捗）の状態を出す。
    const segments = page.getByTestId("document-segments");
    await expect(segments).toBeVisible();
    await expect(segments).not.toHaveAttribute("open", "");
    await segments.locator("summary").click();
    const cards = segments.getByTestId("document-segment");
    await expect(cards).toHaveCount(9);
    await expect(cards.nth(0)).toHaveAttribute("data-status", "SUCCEEDED");
    await expect(cards.nth(2)).toHaveAttribute("data-status", "RUNNING");
    await expect(cards.nth(2).getByText("4 / 10 ページ")).toBeVisible();
    await expect(cards.nth(3)).toHaveAttribute("data-status", "QUEUED");
    await expectNoHorizontalOverflow(page);
  }
);

test(
  "段の解析と並べて図・表を説明し、API の制限で待っている理由と残りの秒数を出す（#2002）",
  { tag: "@responsive" },
  async ({ page }) => {
    await mockDocumentWorkspace(page, {
      documentStatus: "INGESTING",
      latestJobStatus: "RUNNING",
      latestJobPhase: "PREPROCESS",
      pdfPreview: true,
      progressScenario: "segmentedStale",
      jobProgress: {
        stage: "parse",
        parse: {
          engine: "docling",
          unit: "page",
          completed: 4,
          failed: 0,
          total: 10,
          finished: false,
          segments: segmentedProgress([
            "SUCCEEDED",
            "SUCCEEDED",
            "RUNNING",
            "QUEUED",
            "QUEUED",
            "QUEUED",
            "QUEUED",
            "QUEUED",
            "QUEUED",
          ]),
        },
        vision: {
          phase: "segments",
          succeeded: 12,
          failed: 0,
          skipped: 0,
          running: 2,
          pending: 6,
          total_known: 20,
          total_final: false,
          current_page: 14,
          current_kind: "picture",
          finished: false,
          segment_page_start: 11,
          segment_page_end: 20,
          segments_completed: 1,
          segments_total: 9,
        },
        calls: {
          stage: "vision",
          succeeded: 12,
          failed: 0,
          retries: 3,
          total: null,
          concurrency: 2,
          max_concurrency: 4,
          in_flight: 0,
          waiting_reason: "rate_limited",
          waiting_seconds: 26,
          finished: false,
        },
        updated_at: new Date(Date.now() - 2_000).toISOString(),
      },
    });

    await page.goto("/documents/doc-1");

    const panel = page
      .getByRole("heading", { name: "工程ごとの実行状況" })
      .locator("xpath=ancestor::section[1]");
    const stages = panel.getByTestId("extract-stage-progress");
    await expect(
      stages.getByRole("progressbar", { name: "抽出: 20 / 82 ページ完了" })
    ).toHaveAttribute("value", "20");
    // 解析の途中でも、解析の済んだ段の図・表の説明の件数を出す。
    await expect(
      stages.getByRole("progressbar", {
        name: "図・表の説明（Vision）: 12 / 20 件（説明の済んだ段 1 / 9）",
      })
    ).toHaveAttribute("value", "12");
    await expect(
      stages.getByText("Docling が p.21-30（段 3 / 9）を解析しています（10 ページ中 4 ページ）。")
    ).toBeVisible();
    await expect(
      stages.getByText("解析の済んだ p.11-20 の図・表を、次の段の解析と並べて説明しています。")
    ).toBeVisible();
    // 待ちの理由は文で出す（色だけで示さない）。進んでいる間は「最後に進んでから」を出さない。
    await expect(stages.getByTestId("extract-stage-api-waiting")).toHaveText(
      "図・表の説明（Vision）: API の制限のため 26 秒待っています（同時の呼び出しを 2 / 4 件に減らしています）。"
    );
    await expect(stages.getByText(/最後に進んでから/)).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
  }
);

test(
  "段の帯はキーボードで区画を移り、ページの範囲と状態を吹き出しに出す（#2001）",
  { tag: "@responsive" },
  async ({ page, isMobile }) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "INGESTING",
    latestJobStatus: "RUNNING",
    latestJobPhase: "PREPROCESS",
    pdfPreview: true,
    progressScenario: "segmentedStale",
    jobProgress: {
      stage: "parse",
      parse: {
        engine: "docling",
        unit: "page",
        completed: 4,
        failed: 0,
        total: 10,
        finished: false,
        segments: segmentedProgress(["SUCCEEDED", "RUNNING", "QUEUED", "QUEUED", "QUEUED", "QUEUED", "QUEUED", "QUEUED", "QUEUED"]),
      },
      vision: null,
      updated_at: new Date(Date.now() - 2_000).toISOString(),
    },
  });

  await page.goto("/documents/doc-1");

  const strip = page.getByTestId("ingestion-segment-strip");
  const items = strip.getByRole("listitem");
  // Tab の止まり先は 1 つ（処理中の段）。← → で区画を移る。
  await expect(items.nth(1)).toHaveAttribute("tabindex", "0");
  await expect(items.nth(0)).toHaveAttribute("tabindex", "-1");
  // 吹き出しは読み上げ名（aria-label）と同じ文なので aria-hidden（二重に読み上げない）。
  // タッチ端末では吹き出しを出さない（共有の Tooltip の規則）。読み上げ名と凡例で示す。
  const tooltip = page.locator('[role="tooltip"]:visible');
  const expectTooltip = async (text: string) => {
    if (isMobile) await expect(tooltip).toHaveCount(0);
    else await expect(tooltip).toHaveText(text);
  };
  await items.nth(1).focus();
  await page.keyboard.press("ArrowRight");
  await expect(items.nth(2)).toBeFocused();
  await expectTooltip("p.21-30: 待機中");
  await page.keyboard.press("ArrowLeft");
  await expect(items.nth(1)).toBeFocused();
  await expectTooltip("p.11-20: 処理中");
  await page.keyboard.press("Home");
  await expect(items.nth(0)).toBeFocused();
  await page.keyboard.press("End");
  await expect(items.nth(8)).toBeFocused();
  await expectTooltip("p.81-82: 待機中");
  await expectNoHorizontalOverflow(page);
  }
);

test("段が 1 つの PDF は段の帯と段の一覧を出さない（#2001）", async ({ page }) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "INGESTING",
    latestJobStatus: "RUNNING",
    latestJobPhase: "PREPROCESS",
    pdfPreview: true,
    jobProgress: {
      stage: "parse",
      parse: { engine: "docling", unit: "page", completed: 3, failed: 0, total: 6, finished: false },
      vision: null,
      updated_at: new Date(Date.now() - 2_000).toISOString(),
    },
  });

  await page.goto("/documents/doc-1");

  await expect(
    page.getByRole("progressbar", { name: "ページの解析（Docling）: 3 / 6 ページ" })
  ).toBeVisible();
  await expect(page.getByTestId("ingestion-segment-strip")).toHaveCount(0);
  await expect(page.getByTestId("document-segments")).toHaveCount(0);
});

test("通しジョブ内の解析が長く進まないときは、最後に進んでからの時間と待っている相手を表示する", async ({
  page,
}) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "INGESTING",
    latestJobStatus: "RUNNING",
    latestJobPhase: "PREPROCESS",
    pdfPreview: true,
    progressScenario: "doclingInline",
    jobProgress: {
      stage: "parse",
      parse: { engine: "docling", unit: "page", completed: 40, failed: 0, total: 82, finished: false },
      vision: null,
      updated_at: new Date(Date.now() - 3 * 60_000).toISOString(),
    },
  });

  await page.goto("/documents/doc-1");

  const stages = page.getByTestId("extract-stage-progress");
  await expect(
    stages.getByRole("progressbar", { name: "ページの解析（Docling）: 40 / 82 ページ" })
  ).toHaveAttribute("value", "40");
  await expect(
    stages.getByText("Docling が原本全体のページを解析しています。ページの多い文書は数分かかります。")
  ).toBeVisible();
  await expect(stages.getByText(/最後に進んでから 3 分/)).toBeVisible();
});

test("ページ数が取れない原本は原本全体の解析中として表示する", async ({ page }) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "INGESTING",
    latestJobStatus: "RUNNING",
    progressScenario: "source",
  });

  await page.goto("/documents/doc-1");

  await expect(page.getByText("抽出: 原本全体を解析中")).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "抽出: 原本全体を解析中" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("ファイル準備の開始 message を操作欄に表示し、本文 export と chunk を自動更新する", async ({
  page,
}) => {
  await mockDocumentWorkspace(page, {
    autoRefreshAfterEnqueue: true,
    documentStatus: "UPLOADED",
  });

  await page.goto("/documents/doc-1");

  // Chunk / エクスポートは初期状態では空。
  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  await expect(page.getByText("chunk はまだ作成されていません。")).toBeVisible();
  await page.getByRole("tab", { name: "抽出エクスポート" }).click();
  await expect(page.getByText("抽出結果がありません")).toBeVisible();

  await page.getByRole("button", { name: "ファイル準備を実行" }).click();
  const actionStatus = page.getByText(
    "ファイル準備を開始しました。完了まで状態を更新します。"
  );
  await expect(actionStatus).toBeVisible();
  await expect(page.getByText(/取込ジョブをキューに投入/)).toHaveCount(0);
  // 開始の文は押したボタンと同じ場所（状態の案内と工程の操作の枠）に 1 つだけ出す（#2034）。
  await expect(page.getByTestId("document-status-actions").getByText(
    "ファイル準備を開始しました。完了まで状態を更新します。"
  )).toBeVisible();
  await expect(actionStatus).toHaveCount(1);

  // 取込後、エクスポート(現在のタブ)が自動更新され、ダウンロードできるようになる。
  await expect(page.getByRole("link", { name: "Markdown をダウンロード" })).toBeVisible({
    timeout: 9_000,
  });
  // Chunk タブにも反映される。
  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  await expect(page.getByRole("button", { name: /経費申請の概要です。/ })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("バックグラウンド失敗後は開始 message を消し、失敗原因と単一の再実行だけを残す", async ({
  page,
}) => {
  await mockDocumentWorkspace(page, { backgroundFailureAfterEnqueue: true });

  await page.goto("/documents/doc-1");
  await page.getByRole("button", { name: "ファイル準備を実行" }).click();
  const startedMessage = page.getByText(
    "ファイル準備を開始しました。完了まで状態を更新します。"
  );
  await expect(startedMessage).toBeVisible();

  await expect(page.getByRole("alert").filter({ hasText: "取込処理に失敗しました。" })).toBeVisible({
    timeout: 9_000,
  });
  await expect(startedMessage).toHaveCount(0);
  await expect(page.getByText("取込処理に失敗しました。")).toHaveCount(1);
  await expect(page.getByRole("button", { name: "ファイル準備を再実行" })).toBeVisible();
  await expect(page.getByRole("button", { name: /から再処理/ })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

// 既定の文書解析エンジン（Docling）で扱えない形式は、backend が取込を始める前に 409 で止める（#286）。
// 処理レシピの失敗表示に、理由と対処（処理レシピで Unstructured を選ぶ・サービスを起動する）を出す。
test("Docling で解析できない形式は、取込を始めずに理由と対処を表示する", async ({ page }) => {
  await mockDocumentWorkspace(page, { documentStatus: "UPLOADED" });
  const detail =
    "文書解析エンジン Docling（既定の解析エンジン）はこのファイル形式（.eml）を解析できないため、取込を開始しませんでした。" +
    "この文書の「処理レシピ」で「文書解析」を Unstructured に変えてから「処理を開始」してください。" +
    "Unstructured の解析サービス（parser-unstructured）は停止しています。「運用設定 › サービス管理」で Unstructured を起動してください。";
  await page.route("**/api/documents/doc-1/recipes/recipe-1/ingestion-jobs**", (route) =>
    route.fulfill({
      status: 409,
      json: { data: null, error_messages: [detail], warning_messages: [] },
    })
  );

  await page.goto("/documents/doc-1");
  await page.getByRole("button", { name: "ファイル準備を実行" }).click();

  await expect(page.getByText(detail, { exact: false }).first()).toBeVisible();
  await expect(
    page.getByText("ファイル準備を開始しました。完了まで状態を更新します。")
  ).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("ERROR は失敗 phase と前段の再処理ボタンを表示する", async ({ page }) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "ERROR",
    latestJobStatus: "FAILED",
    latestJobPhase: "CHUNK",
    preparedArtifact: true,
  });

  await page.goto("/documents/doc-1");

  await expect(page.getByRole("button", { name: "Chunk 作成を再実行" })).toBeVisible();
  await expect(page.getByRole("button", { name: "ファイル準備から再処理" })).toBeVisible();
  await expect(page.getByRole("button", { name: "抽出から再処理" })).toBeVisible();
  await expect(page.getByRole("button", { name: "ファイル準備を再実行" })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

// Chunk 作成から索引まで通す job が Embedding で失敗したとき（job の phase は CHUNK）、保存済みの
// Chunk（CHUNKED の chunk_set）から Embedding / 索引を再実行する導線を主にし、原因を表示する（#2467）。
test("Embedding / 索引の失敗は保存済みの Chunk から索引を再実行する導線と原因を表示する", { tag: "@responsive" }, async ({
  page,
}) => {
  const cause =
    "Embedding の要求が失敗しました（HTTP 400・サービスの高負荷）。バッチ 2/4（83 件・98,131 文字）。" +
    "再試行しても回復しませんでした。時間をおいて、保存済みの Chunk から Embedding / 索引を再実行してください。";
  const state = await mockDocumentWorkspace(page, {
    documentStatus: "ERROR",
    latestJobStatus: "FAILED",
    latestJobPhase: "CHUNK",
    recipeFailedPhase: "INDEX",
    pendingChunkSet: true,
    preparedArtifact: true,
    sharedErrorMessage: cause,
  });

  await page.goto("/documents/doc-1");

  const alert = page.getByRole("alert").filter({ hasText: "Embedding / 索引で失敗しました" });
  await expect(alert).toContainText(cause);
  await expect(page.getByRole("button", { name: "Embedding / 索引を再実行" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Chunk 作成を再実行" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Chunk から再処理" })).toBeVisible();
  await expect(page.getByRole("button", { name: "抽出から再処理" })).toBeVisible();
  await expect(page.getByRole("button", { name: "ファイル準備から再処理" })).toBeVisible();
  await expectNoPageOverflow(page);

  await page.getByRole("button", { name: "Embedding / 索引を再実行" }).click();
  const confirm = page.getByRole("alertdialog");
  await expect(confirm).toContainText("保存済みの Chunk から Embedding / 索引を再実行します。");
  await confirm.getByRole("button", { name: "再実行する" }).click();
  await expect.poll(() => state.enqueueRequest).toMatchObject({
    path: "/api/documents/doc-1/recipes/recipe-1/ingestion-jobs",
    phase: "INDEX",
  });
});

// 失敗したレシピを再処理すると、レシピは worker が claim するまで前回の ERROR と本文を持つ。見出しが
// 「〈工程〉の順番待ち」の間は本文にも前回の失敗を出さず、新しい job が失敗したら新しい本文を出す（#2687）。
test("再処理の job が順番待ちの間は前回の失敗を出さず、また失敗したら新しい原因を出す", { tag: "@responsive" }, async ({
  page,
}) => {
  const previousCause = "前回の索引の失敗（合成の本文）。";
  const newCause = "新しい索引の失敗（合成の本文）。";
  await mockDocumentWorkspace(page, {
    documentStatus: "ERROR",
    latestJobStatus: "FAILED",
    latestJobPhase: "CHUNK",
    recipeFailedPhase: "INDEX",
    pendingChunkSet: true,
    preparedArtifact: true,
    sharedErrorMessage: previousCause,
  });
  // backend と同じ遷移: 失敗 → 再処理の job が順番待ち（レシピは ERROR と前回の本文のまま）→ 新しい失敗。
  let scenario: "failed" | "queued" | "refailed" = "failed";
  const reprocessJob = (status: "QUEUED" | "FAILED") => ({
    ...ingestionJob(status, {
      phase: "INDEX",
      queuedAt: "2026-06-15T00:01:00Z",
      errorMessage: newCause,
    }),
    id: "job-reprocess",
  });
  const previousJob = ingestionJob("FAILED", { phase: "CHUNK", errorMessage: previousCause });
  await page.route("**/api/documents/doc-1/recipes", async (route) => {
    const failedSteps = recipeSteps("ERROR", "INDEX").map((step) =>
      step.status === "FAILED"
        ? { ...step, error_message: scenario === "refailed" ? newCause : previousCause }
        : step
    );
    const steps =
      scenario === "queued"
        ? failedSteps.map((step) =>
            step.phase === "INDEX"
              ? {
                  ...step,
                  status: "QUEUED",
                  error_message: null,
                  queue_wait: {
                    phase: "INDEX",
                    job_id: "job-reprocess",
                    queued_at: "2026-06-15T00:01:00Z",
                    jobs_ahead: 2,
                    concurrency: 1,
                  },
                }
              : step
          )
        : failedSteps;
    await route.fulfill({
      json: {
        data: [
          {
            recipe_id: "recipe-1",
            document_id: "doc-1",
            slot_no: 1,
            status: "ERROR",
            failed_phase: "INDEX",
            processing_config: {},
            effective_processing_config: {},
            preprocess_artifact: documentDetail({ status: "ERROR", preparedArtifact: true })
              .preprocess_artifact,
            active_extraction_recipe_id: "er-recipe-1-r1",
            active_chunk_set_id: null,
            pending_chunk_set_id: "chunk-set-pending",
            chunk_count: 0,
            vector_count: 0,
            config_revision: 1,
            materialized_revision: null,
            searchable: false,
            needs_reprocessing: false,
            error_message: scenario === "refailed" ? newCause : previousCause,
            steps,
            created_at: "2026-06-15T00:00:00Z",
            updated_at: "2026-06-15T00:00:20Z",
            started_at: null,
            finished_at: "2026-06-15T00:00:20Z",
          },
        ],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/ingestion-jobs**", async (route) => {
    await route.fulfill({
      json: {
        data:
          scenario === "failed"
            ? [previousJob]
            : [reprocessJob(scenario === "queued" ? "QUEUED" : "FAILED"), previousJob],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/recipes/recipe-1/ingestion-jobs**", async (route) => {
    scenario = "queued";
    await route.fulfill({
      json: { data: reprocessJob("QUEUED"), error_messages: [], warning_messages: [] },
    });
  });
  await page.route("**/api/documents/ingestion-jobs/job-reprocess", async (route) => {
    await route.fulfill({
      json: {
        data: reprocessJob(scenario === "refailed" ? "FAILED" : "QUEUED"),
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  await page.goto("/documents/doc-1");
  const failureAlert = page.getByRole("alert").filter({ hasText: "Embedding / 索引で失敗しました" });
  await expect(failureAlert).toContainText(previousCause);

  await page.getByRole("button", { name: "Embedding / 索引を再実行" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "再実行する" }).click();

  // 順番待ちの間: 見出しは「Embedding / 索引の順番待ち」、前回の失敗は出さず、開始の結果を出す。
  await expect(page.getByText("Embedding / 索引の順番待ち").first()).toBeVisible();
  await expect(
    page.getByText("Embedding / 索引を開始しました。完了まで状態を更新します。")
  ).toBeVisible();
  await expect(failureAlert).toHaveCount(0);
  await expect(page.getByText(previousCause)).toHaveCount(0);
  await expectNoPageOverflow(page);

  // 再処理の job も失敗した: 新しい原因を出す（前回の本文は出さない）。
  scenario = "refailed";
  await expect(failureAlert).toContainText(newCause, { timeout: 9_000 });
  await expect(page.getByText(previousCause)).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("ERROR は前提 artifact が無ければファイル準備の再実行だけに戻す", async ({ page }) => {
  await mockDocumentWorkspace(page, {
    documentStatus: "ERROR",
    latestJobStatus: "FAILED",
    latestJobPhase: "EXTRACT",
  });

  await page.goto("/documents/doc-1");

  await expect(page.getByRole("button", { name: "ファイル準備を再実行" })).toBeVisible();
  await expect(page.getByRole("button", { name: "抽出を再実行" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /から再処理/ })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("重複文書は重複元を表示し、明示操作では recipe のファイル準備を投入する", async ({ page }) => {
  const state = await mockDocumentWorkspace(page, {
    chunksEmpty: true,
    duplicate: true,
    documentStatus: "UPLOADED",
  });

  await page.goto("/documents/doc-1");

  await expect(
    page.getByText(/同一内容の文書が既に登録されています。重複元: original\.pdf \/ 索引済み/)
  ).toBeVisible();
  await expect(page.getByText("内容が同じでも別文書として処理したい場合")).toBeVisible();
  await page.getByRole("button", { name: "重複を無視してファイル準備" }).click();
  await expect.poll(() => state.enqueueRequest).toMatchObject({
    path: "/api/documents/doc-1/recipes/recipe-1/ingestion-jobs",
    phase: "PREPROCESS",
  });
  await expectNoHorizontalOverflow(page);
});

test("抽出セグメントが多い場合は高さ固定で内部スクロールする", async ({ page }) => {
  await mockDocumentWorkspace(page, { segmentCount: 30 });

  await page.goto("/documents/doc-1");

  // 抽出セグメントは「処理の詳細(診断)」折りたたみ内にあり、段の一覧も既定で畳む（#2001）。
  await page.getByText("処理の詳細(診断)").click();
  const panel = page.getByTestId("document-segments");
  await panel.locator("summary").click();
  const list = panel.getByTestId("document-segment-list");

  await expect(page.getByText("30 件")).toBeVisible();
  await expect(list.getByText("p.1-10")).toBeVisible();
  await expect(await isScrollable(list)).toBe(true);
  await expectNoHorizontalOverflow(page);
});

test("chunk が多い場合は 5 / 8 行の高さで中をスクロールし、選んだ chunk を一覧の中で見せる", { tag: "@responsive" }, async ({ page }) => {
  await mockDocumentWorkspace(page, { extraChunkCount: 23 });
  await page.goto("/documents/doc-1");
  await page.getByRole("tab", { name: "Chunk / Citation", exact: false }).click();

  const list = page.getByTestId("document-chunk-list");
  await expect(list.getByRole("button")).toHaveCount(25);
  const width = page.viewportSize()?.width ?? 1440;
  const metrics = await list.evaluate((element) => ({
    maxHeight: getComputedStyle(element).maxHeight,
    rem: Number.parseFloat(getComputedStyle(document.documentElement).fontSize),
    scrollable: element.scrollHeight > element.clientHeight + 1,
    panelScrollable: (() => {
      const panel = element.closest('[role="tabpanel"]') as HTMLElement;
      return getComputedStyle(panel).overflowY === "auto" && panel.scrollHeight > panel.clientHeight + 1;
    })(),
  }));
  if (width >= 1280) {
    // xl 以上はプレビューの横に並び、プレビューと同じ高さのタブのパネルがスクロールする（二重のスクロールにしない）。
    expect(metrics.maxHeight).toBe("none");
    expect(metrics.panelScrollable).toBe(true);
  } else {
    const expectedRem = width >= 768 ? 28 : 17.5;
    expect(Math.abs(Number.parseFloat(metrics.maxHeight) - expectedRem * metrics.rem)).toBeLessThanOrEqual(1);
    expect(metrics.scrollable).toBe(true);
  }

  // 末尾の chunk を選び、別のタブへ移って戻っても、選んだ chunk がスクロール領域の中で見える位置にある。
  const lastChunk = list.getByRole("button").last();
  await lastChunk.scrollIntoViewIfNeeded();
  await lastChunk.click();
  await expect(lastChunk).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("tab", { name: "抽出エクスポート", exact: false }).click();
  await page.getByRole("tab", { name: "Chunk / Citation", exact: false }).click();
  const selected = page.getByTestId("document-chunk-list").locator('button[aria-pressed="true"]');
  await expect(selected).toContainText("#25");
  await expect.poll(() => selectedChunkVisibleInScrollArea(page)).toBe(true);
  await expectNoHorizontalOverflow(page);
});

test("引用の deep-link は一覧の中の chunk までスクロールしてフォーカスする", async ({ page }) => {
  await mockDocumentWorkspace(page, { extraChunkCount: 23 });
  await page.goto("/documents/doc-1?chunk_id=doc-1:20");

  const target = page.getByTestId("document-chunk-list").locator('button[aria-pressed="true"]');
  await expect(target).toContainText("#21");
  await expect(target).toBeFocused();
  await expect.poll(() => selectedChunkVisibleInScrollArea(page)).toBe(true);
});

/** 選んだ chunk が、それを囲む縦スクロールの領域（一覧自身か、xl 以上はタブのパネル）の見える範囲にある。 */
function selectedChunkVisibleInScrollArea(page: Page) {
  return page.getByTestId("document-chunk-list").evaluate((list) => {
    const selected = list.querySelector('button[aria-pressed="true"]');
    if (!selected) return false;
    let container: HTMLElement | null = list as HTMLElement;
    while (container && !(getComputedStyle(container).overflowY === "auto" && container.scrollHeight > container.clientHeight + 1)) {
      container = container.parentElement;
    }
    if (!container) return false;
    const box = container.getBoundingClientRect();
    const item = selected.getBoundingClientRect();
    return container.scrollTop > 0 && item.top >= box.top - 1 && item.bottom <= box.bottom + 1;
  });
}

async function mockDocumentWorkspace(
  page: Page,
  options: {
    chunkBboxMode?: "xywh";
    chunkBboxUnit?: "absolute";
    chunksEmpty?: boolean;
    chunksError?: boolean;
    imagePreview?: boolean;
    pdfPreview?: boolean;
    /** PDF をページ画像で表示する API（preview-pages）を返す（#349）。無ければ iframe に戻る。 */
    pdfPages?: boolean;
    /** Chunk「交通費は1000円」に要素の表示領域（要素ごとの bbox・2 ページ）を持たせる（#349）。 */
    layoutRegions?: boolean;
    segmentError?: boolean;
    segmentCount?: number;
    /** 既定の 2 件の後ろに足す chunk の数（一覧の内部スクロールの検証用。#403）。 */
    extraChunkCount?: number;
    unstructuredSegment?: boolean;
    latestJobStatus?: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
    latestJobPhase?: "PREPROCESS" | "EXTRACT" | "CHUNK" | "INDEX";
    latestJobStartedAt?: string;
    /** 4工程すべての成功ジョブを返す(工程別サマリ表示の検証用)。 */
    jobsForAllPhases?: boolean;
    autoRefreshAfterEnqueue?: boolean;
    backgroundFailureAfterEnqueue?: boolean;
    documentStatus?: string;
    duplicate?: boolean;
    progressScenario?: "pdf17" | "source" | "doclingInline" | "segmentedStale";
    /** 実行中の job の抽出の段の進捗（`IngestionJob.progress`。#1999）。 */
    jobProgress?: Record<string, unknown>;
    preparedArtifact?: boolean;
    documentOnlyPreparedArtifact?: boolean;
    brokenPreparedArtifact?: boolean;
    preprocessProfile?: "passthrough" | "text_normalize";
    sharedErrorMessage?: string;
    /** レシピの失敗工程（通しジョブでは job の phase と違う。#2467）。無ければ latestJobPhase。 */
    recipeFailedPhase?: "PREPROCESS" | "EXTRACT" | "CHUNK" | "INDEX";
    /** 索引の前の保存済み chunk_set（CHUNKED）を持つ（#2467）。 */
    pendingChunkSet?: boolean;
  } = {}
) {
  const state: {
    retryRequest: { method: string; path: string; recipeId: string | null } | null;
    enqueueRequest: {
      method: string;
      path: string;
      force: string | null;
      phase: string | null;
    } | null;
    enqueued: boolean;
    backgroundJobPolls: number;
    backgroundFailed: boolean;
    extractionExportRequests: number;
  } = {
    retryRequest: null,
    enqueueRequest: null,
    enqueued: false,
    backgroundJobPolls: 0,
    backgroundFailed: false,
    extractionExportRequests: 0,
  };
  const segmentFailureMessage =
    "OCI Enterprise AI VLM response が StructuredExtraction schema と一致しません。失敗項目: confidence: less_than_equal。";
  await page.route("**/api/documents/doc-1", async (route) => {
    await route.fulfill({
      json: {
        data: documentDetail({
          imagePreview: options.imagePreview,
          pdfPreview: options.pdfPreview,
          status: currentDocumentStatus(options, state),
          duplicate: options.duplicate,
          preparedArtifact: options.preparedArtifact || options.documentOnlyPreparedArtifact,
          brokenPreparedArtifact: options.brokenPreparedArtifact,
          errorMessage:
            options.sharedErrorMessage ?? (options.segmentError ? segmentFailureMessage : undefined),
        }),
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/recipes", async (route) => {
    const status = currentDocumentStatus(options, state);
    const detail = documentDetail({
      imagePreview: options.imagePreview,
      pdfPreview: options.pdfPreview,
      status,
      preparedArtifact: options.preparedArtifact || options.segmentError,
      brokenPreparedArtifact: options.brokenPreparedArtifact,
      errorMessage:
        options.sharedErrorMessage ?? (options.segmentError ? segmentFailureMessage : undefined),
    });
    const hasExtraction = ["REVIEW", "CHUNKED", "INDEXED"].includes(status) ||
      (status === "ERROR" && ["CHUNK", "INDEX"].includes(options.latestJobPhase ?? ""));
    await route.fulfill({
      json: {
        data: [
          {
            recipe_id: "recipe-1",
            document_id: "doc-1",
            slot_no: 1,
            status,
            failed_phase:
              status === "ERROR"
                ? (options.recipeFailedPhase ?? options.latestJobPhase ?? "PREPROCESS")
                : null,
            processing_config: {},
            effective_processing_config: {},
            preprocess_artifact: detail.preprocess_artifact,
            active_extraction_recipe_id: hasExtraction ? "er-recipe-1-r1" : null,
            active_chunk_set_id: status === "INDEXED" ? "chunk-set-recipe-1" : null,
            pending_chunk_set_id: options.pendingChunkSet ? "chunk-set-pending" : null,
            chunk_count: status === "INDEXED" ? 2 : 0,
            vector_count: status === "INDEXED" ? 2 : 0,
            config_revision: 1,
            materialized_revision: status === "INDEXED" ? 1 : null,
            searchable: status === "INDEXED",
            needs_reprocessing: false,
            error_message:
              status === "ERROR"
                ? (options.sharedErrorMessage ??
                  (options.segmentError ? segmentFailureMessage : "取込処理に失敗しました。"))
                : null,
            steps: recipeSteps(status, options.recipeFailedPhase ?? options.latestJobPhase),
            created_at: "2026-06-15T00:00:00Z",
            updated_at: "2026-06-15T00:00:20Z",
            started_at: null,
            finished_at: status === "INDEXED" || status === "ERROR" ? "2026-06-15T00:00:20Z" : null,
          },
        ],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/recipes/recipe-1/ingestion-jobs**", async (route) => {
    const url = new URL(route.request().url());
    state.enqueueRequest = {
      method: route.request().method(),
      path: url.pathname,
      force: url.searchParams.get("force"),
      phase: url.searchParams.get("phase"),
    };
    state.enqueued = true;
    await route.fulfill({
      json: {
        data: ingestionJob("QUEUED", {
          phase: (url.searchParams.get("phase") as "PREPROCESS" | "EXTRACT" | "CHUNK" | "INDEX" | null) ?? "PREPROCESS",
        }),
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/recipes/recipe-1/approve", async (route) => {
    const status = currentDocumentStatus(options, state);
    const phase = status === "PREPROCESSED" ? "EXTRACT" : status === "CHUNKED" ? "INDEX" : "CHUNK";
    await route.fulfill({
      json: {
        data: ingestionJob("QUEUED", { phase }),
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/ingestion-segments/retry**", async (route) => {
    const url = new URL(route.request().url());
    state.retryRequest = {
      method: route.request().method(),
      path: url.pathname,
      recipeId: url.searchParams.get("recipe_id"),
    };
    await route.fulfill({
      json: {
        data: retrySegmentsJob(),
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/ingestion-jobs**", async (route) => {
    if (route.request().method() === "POST") {
      const url = new URL(route.request().url());
      state.enqueueRequest = {
        method: route.request().method(),
        path: url.pathname,
        force: url.searchParams.get("force"),
        phase: url.searchParams.get("phase"),
      };
      state.enqueued = true;
      await route.fulfill({
        json: {
          data: ingestionJob("QUEUED", {
            phase:
              (url.searchParams.get("phase") as
                | "PREPROCESS"
                | "EXTRACT"
                | "CHUNK"
                | "INDEX"
                | null) ?? "PREPROCESS",
          }),
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fulfill({
      json: {
        data: options.jobsForAllPhases
          ? (["INDEX", "CHUNK", "EXTRACT", "PREPROCESS"] as const).map((phase) =>
              ingestionJob("SUCCEEDED", { phase })
            )
          : [
              ingestionJob(
                options.backgroundFailureAfterEnqueue && state.backgroundFailed
                  ? "FAILED"
                  : options.latestJobStatus ?? "SUCCEEDED",
                {
                  startedAt: options.latestJobStartedAt,
                  errorMessage: options.sharedErrorMessage,
                  progress: options.jobProgress,
                  phase: options.backgroundFailureAfterEnqueue
                    ? "PREPROCESS"
                    : options.latestJobPhase,
                }
              ),
            ],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/chunk-sets", async (route) => {
    const status = currentDocumentStatus(options, state);
    await route.fulfill({
      json: {
        data:
          status === "INDEXED"
            ? [
                {
                  chunk_set_id: "chunk-set-recipe-1",
                  extraction_recipe_id: "er-recipe-1-r1",
                  extraction_status: "materialized",
                  extraction_reason: null,
                  status: "ACTIVE",
                  chunk_count: 2,
                  vector_count: 2,
                  is_serving: true,
                  created_at: "2026-06-15T00:00:20Z",
                  extraction_id: null,
                  parser: null,
                  preprocess: null,
                  knowledge_base_ids: ["kb-1"],
                  serving_knowledge_base_ids: ["kb-1"],
                  layer_statuses: {},
                },
              ]
            : [],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/settings/model", async (route) => {
    await route.fulfill({
      json: {
        data: {
          settings: {
            enterprise_ai: {},
            generative_ai: {
              embedding_model: "text-embedding-v4",
              embedding_dim: 1536,
              embedding_base_url: "",
              rerank_model: "qwen3-rerank",
              rerank_base_url: "",
            },
          },
          checks: {},
          model_settings_file: "models.json",
          source: "runtime",
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/ingestion-jobs/job-retry-segments", async (route) => {
    if (options.backgroundFailureAfterEnqueue) {
      state.backgroundFailed = state.backgroundJobPolls > 0;
      state.backgroundJobPolls += 1;
    }
    await route.fulfill({
      json: {
        data: ingestionJob(
          options.backgroundFailureAfterEnqueue && state.backgroundFailed ? "FAILED" : "QUEUED",
          {
          phase:
            (state.enqueueRequest?.phase as
              | "PREPROCESS"
              | "EXTRACT"
              | "CHUNK"
              | "INDEX"
              | null) ?? "EXTRACT",
          }
        ),
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/knowledge-bases", async (route) => {
    await route.fulfill({
      json: {
        data: [{ id: "kb-1", name: "社内規程" }],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route(
    /\/api\/documents\/doc-1(?:\/recipes\/recipe-1)?\/content/,
    async (route) => {
    const variant = new URL(route.request().url()).searchParams.get("variant");
    if (variant === "prepared") {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
        body: "準備後ファイル\n交通費は1000円です。",
      });
      return;
    }
    if (options.imagePreview) {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "image/svg+xml" },
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="612" height="792"><rect width="612" height="792" fill="white"/><text x="24" y="80">TOTAL 1000 JPY</text></svg>',
      });
      return;
    }
    if (options.pdfPreview) {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "application/pdf" },
        body: [
          "%PDF-1.1",
          "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj",
          "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj",
          "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj",
          "trailer << /Root 1 0 R >>",
          "%%EOF",
        ].join("\n"),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/plain; charset=utf-8" },
      body: "経費申請\n交通費は1000円です。",
    });
    }
  );
  if (options.pdfPages) {
    await page.route(
      /\/api\/documents\/doc-1(?:\/recipes\/recipe-1)?\/preview-pages(?:\?|$)/,
      async (route) => {
        await route.fulfill({
          json: {
            data: {
              page_count: 2,
              pages: [
                { page_number: 1, width: 612, height: 792 },
                { page_number: 2, width: 792, height: 612 },
              ],
            },
            error_messages: [],
            warning_messages: [],
          },
        });
      }
    );
    await page.route(
      /\/api\/documents\/doc-1(?:\/recipes\/recipe-1)?\/preview-pages\/\d+/,
      async (route) => {
        const pageNumber = Number(new URL(route.request().url()).pathname.split("/").pop());
        const [width, height] = pageNumber === 2 ? [792, 612] : [612, 792];
        await route.fulfill({
          status: 200,
          headers: { "content-type": "image/svg+xml" },
          body: `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><rect width="${width}" height="${height}" fill="white"/><rect x="61.2" y="79.2" width="244.8" height="79.2" fill="none" stroke="black"/><text x="24" y="40">PAGE ${pageNumber}</text></svg>`,
        });
      }
    );
  }
  await page.route(
    /\/api\/documents\/doc-1(?:\/recipes\/recipe-1)?\/chunks(?:\?|$)/,
    async (route) => {
    if (options.chunksError) {
      await route.fulfill({
        status: 500,
        json: {
          data: null,
          error_messages: ["chunk error"],
          warning_messages: [],
        },
      });
      return;
    }
    if (options.chunksEmpty || (options.autoRefreshAfterEnqueue && !state.enqueued)) {
      await route.fulfill({
        json: {
          data: [],
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fulfill({
      json: {
        data: [
          {
            document_id: "doc-1",
            chunk_id: "doc-1:0",
            chunk_index: 0,
            text: "経費申請の概要です。",
            page_start: 1,
            page_end: 1,
            bbox: null,
            section_path: "経費申請",
            content_kind: "text",
            chunk_group_id: "grp-1",
            source_parser: "local_text_structure",
            element_ids: ["el-0000"],
            metadata: { chunk_profile: "structure_v1" },
          },
          {
            document_id: "doc-1",
            chunk_id: "doc-1:1",
            chunk_index: 1,
            text: "交通費は1000円です。",
            page_start: 1,
            page_end: 1,
            bbox:
              options.chunkBboxMode === "xywh" || options.chunkBboxUnit
                ? [25, 10, 50, 40]
                : [0, 0, 100, 40],
            section_path: "経費申請 > 料金表",
            content_kind: "table",
            chunk_group_id: "grp-2",
            source_parser: "local_text_structure",
            element_ids: ["tbl-1"],
            metadata:
              options.chunkBboxMode === "xywh" || options.chunkBboxUnit
                ? {
                    ...(options.chunkBboxMode === "xywh"
                      ? { bbox_coordinate_mode: "xywh" }
                      : {}),
                    ...(options.chunkBboxUnit ? { bbox_unit: options.chunkBboxUnit } : {}),
                    chunk_profile: "structure_v1",
                  }
                : options.layoutRegions
                  ? {
                      chunk_profile: "structure_v1",
                      page_width: 612,
                      page_height: 792,
                      bbox_unit: "absolute",
                      engine_metadata_json: JSON.stringify({
                        schema_version: 4,
                        layout: {
                          display_regions: [
                            {
                              page: 1,
                              boxes: [
                                // 10% / 10% / 40% / 10%
                                { record_id: "docling-p1-1", seq_no: 1, category: "Text", bbox: [61.2, 79.2, 306, 158.4] },
                                // 50% / 50% / 40% / 10%
                                { record_id: "docling-p1-2", seq_no: 2, category: "Table", bbox: [306, 396, 550.8, 475.2] },
                              ],
                            },
                            // 2 ページ目は横向き（792x612 pt）。1 ページ目と同じ dpi の px 座標で、左上 1/4。
                            { page: 2, boxes: [{ record_id: "docling-p2-1", seq_no: 1, category: "Text", bbox: [0, 0, 396, 306] }] },
                          ],
                        },
                      }),
                    }
                  : { chunk_profile: "structure_v1" },
          },
          ...Array.from({ length: options.extraChunkCount ?? 0 }, (_, index) => ({
            document_id: "doc-1",
            chunk_id: `doc-1:${index + 2}`,
            chunk_index: index + 2,
            text: `追加の chunk ${index + 3} の本文です。`,
            page_start: 1,
            page_end: 1,
            bbox: null,
            section_path: "経費申請",
            content_kind: "text",
            chunk_group_id: `grp-extra-${index}`,
            source_parser: "local_text_structure",
            element_ids: [],
            metadata: { chunk_profile: "structure_v1" },
          })),
        ],
        error_messages: [],
        warning_messages: [],
      },
    });
    }
  );
  await page.route(
    /\/api\/documents\/doc-1(?:\/recipes\/recipe-1)?\/extraction-export/,
    async (route) => {
    state.extractionExportRequests += 1;
    const url = new URL(route.request().url());
    const format = url.searchParams.get("format") ?? "markdown";
    const exportData =
      options.autoRefreshAfterEnqueue && !state.enqueued
        ? {
            ...extractionExport(format),
            content: "",
            payload: {},
            chunks: [],
            page_count: 0,
            element_count: 0,
            table_count: 0,
            asset_count: 0,
          }
        : extractionExport(format);
    await route.fulfill({
      json: {
        data: exportData,
        error_messages: [],
        warning_messages: [],
      },
    });
    }
  );
  await page.route("**/api/documents/doc-1/ingestion-segments", async (route) => {
    const failedSegment = {
      segment_id: "doc-1:p1-10",
      document_id: "doc-1",
      recipe_id: "recipe-1",
      status: "FAILED",
      parser_backend: "enterprise_ai",
      parser_profile: "enterprise_ai_pdf_layout",
      page_start: 1,
      page_end: 10,
      attempt_count: 2,
      artifact_path: null,
      error_code: "enterprise_ai_response_validation_error",
      error_message:
        options.sharedErrorMessage ??
        segmentFailureMessage,
    };
    // 段ごとの解析で p.1-10 が失敗し、始めていない p.11-12 は取消になった（#2001）。
    const notStartedSegment = {
      ...failedSegment,
      segment_id: "doc-1:p11-12",
      status: "CANCELLED",
      page_start: 11,
      page_end: 12,
      attempt_count: 0,
      error_code: "segment_not_started",
      error_message:
        "前の段の解析が失敗・取消になったため、この段は解析していません。再試行すると、この段から解析します。",
    };
    await route.fulfill({
      json: {
        data: options.segmentError
          ? [failedSegment, notStartedSegment]
          : ingestionSegments(options.segmentCount ?? 1, {
              unstructured: options.unstructuredSegment,
              progressScenario: options.progressScenario,
            }),
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  return state;
}

function currentDocumentStatus(
  options: {
    backgroundFailureAfterEnqueue?: boolean;
    documentStatus?: string;
    segmentError?: boolean;
    autoRefreshAfterEnqueue?: boolean;
  },
  state: { backgroundFailed: boolean; enqueued: boolean }
) {
  if (options.backgroundFailureAfterEnqueue) {
    return state.backgroundFailed ? "ERROR" : "UPLOADED";
  }
  if (options.autoRefreshAfterEnqueue) {
    return state.enqueued ? "INDEXED" : (options.documentStatus ?? "ERROR");
  }
  return options.documentStatus ??
    (options.segmentError || (options.autoRefreshAfterEnqueue && !state.enqueued)
      ? "ERROR"
      : "INDEXED");
}

function recipeSteps(
  status: string,
  failedPhase: "PREPROCESS" | "EXTRACT" | "CHUNK" | "INDEX" | undefined
) {
  const phases = ["PREPROCESS", "EXTRACT", "CHUNK", "INDEX"] as const;
  const completedCount: Record<string, number> = {
    UPLOADED: 0,
    PREPROCESSING: 0,
    PREPROCESSED: 1,
    INGESTING: 1,
    REVIEW: 2,
    CHUNKING: 2,
    CHUNKED: 3,
    INDEXING: 3,
    INDEXED: 4,
  };
  const runningPhase: Record<string, (typeof phases)[number]> = {
    PREPROCESSING: "PREPROCESS",
    INGESTING: "EXTRACT",
    CHUNKING: "CHUNK",
    INDEXING: "INDEX",
  };
  const failedIndex = status === "ERROR" ? phases.indexOf(failedPhase ?? "PREPROCESS") : -1;
  return phases.map((phase, index) => ({
    phase,
    status:
      index < (completedCount[status] ?? failedIndex)
        ? "SUCCEEDED"
        : runningPhase[status] === phase
          ? "RUNNING"
          : failedIndex === index
            ? "FAILED"
            : "PENDING",
    started_at: null,
    finished_at: null,
    error_message: failedIndex === index ? "取込処理に失敗しました。" : null,
  }));
}

function ingestionSegments(
  count: number,
  options: {
    unstructured?: boolean;
    progressScenario?: "pdf17" | "source" | "doclingInline" | "segmentedStale";
  } = {}
) {
  if (options.progressScenario === "segmentedStale") {
    // 前の試行（中断）の段の記録が待機中のまま残っている（#2001）。今の状態は job の進捗が正。
    return Array.from({ length: 9 }, (_, index) => {
      const start = index * 10 + 1;
      const end = Math.min(start + 9, 82);
      return {
        segment_id: `doc-1:p${start}-${end}`,
        document_id: "doc-1",
        recipe_id: "recipe-1",
        status: "QUEUED",
        parser_backend: "docling",
        parser_profile: "docling",
        page_start: start,
        page_end: end,
        progress_unit: "page",
        progress_start: start,
        progress_end: end,
        attempt_count: 0,
        artifact_path: null,
        error_code: null,
        error_message: null,
      };
    });
  }
  if (options.progressScenario === "doclingInline") {
    // 82 ページを 10 ページずつの 9 件。原本全体を Docling が 1 回で解析した後に作るため処理中（#1999）。
    return Array.from({ length: 9 }, (_, index) => {
      const start = index * 10 + 1;
      const end = Math.min(start + 9, 82);
      return {
        segment_id: `doc-1:p${start}-${end}`,
        document_id: "doc-1",
        recipe_id: "recipe-1",
        status: "RUNNING",
        parser_backend: "docling",
        parser_profile: "docling",
        page_start: start,
        page_end: end,
        progress_unit: "page",
        progress_start: start,
        progress_end: end,
        attempt_count: 1,
        artifact_path: null,
        error_code: null,
        error_message: null,
      };
    });
  }
  if (options.progressScenario === "source") {
    return [
      {
        segment_id: "doc-1:source",
        document_id: "doc-1",
        recipe_id: "recipe-1",
        status: "RUNNING",
        parser_backend: "local_partition",
        parser_profile: "local_text_structure",
        page_start: null,
        page_end: null,
        progress_unit: "source",
        progress_start: null,
        progress_end: null,
        attempt_count: 1,
        artifact_path: null,
        error_code: null,
        error_message: null,
      },
    ];
  }
  if (options.progressScenario === "pdf17") {
    const ranges: Array<[string, "SUCCEEDED" | "RUNNING" | "QUEUED", number, number]> = [
      ["1-5", "SUCCEEDED", 1, 5],
      ["6-9", "SUCCEEDED", 6, 9],
      ["10-13", "RUNNING", 10, 13],
      ["14-17", "QUEUED", 14, 17],
    ];
    return ranges.map(([range, status, start, end]) => ({
      segment_id: `doc-1:p${range}`,
      document_id: "doc-1",
      recipe_id: "recipe-1",
      status,
      parser_backend: "enterprise_ai",
      parser_profile: "enterprise_ai_pdf_layout",
      page_start: start,
      page_end: end,
      progress_unit: "page",
      progress_start: start,
      progress_end: end,
      attempt_count: status === "QUEUED" ? 0 : 1,
      artifact_path: status === "SUCCEEDED" ? `local://doc-1/${range}` : null,
      error_code: null,
      error_message: null,
    }));
  }
  return Array.from({ length: count }, (_, index) => {
    const start = index * 10 + 1;
    const end = start + 9;
    const status = index < 4 ? "SUCCEEDED" : index === 4 ? "RUNNING" : "QUEUED";
    const unstructured = Boolean(options.unstructured);
    return {
      segment_id: `doc-1:p${start}-${end}`,
      document_id: "doc-1",
      recipe_id: "recipe-1",
      status,
      parser_backend: unstructured ? "unstructured" : index === 0 ? "local_partition" : "enterprise_ai",
      parser_profile: unstructured
        ? "unstructured_adapter"
        : index === 0
          ? "local_text_structure"
          : "enterprise_ai_pdf_layout",
      page_start: start,
      page_end: end,
      progress_unit: "page",
      progress_start: start,
      progress_end: end,
      attempt_count: status === "QUEUED" ? 0 : 1,
      artifact_path: status === "SUCCEEDED" ? `local://doc-1/${start}` : null,
      error_code: null,
      error_message: null,
    };
  });
}

function extractionExport(format: string) {
  const chunks = [
    {
      document_id: "doc-1",
      chunk_id: "doc-1:0",
      chunk_index: 0,
      text: "経費申請の概要です。",
      page_start: 1,
      page_end: 1,
      bbox: null,
      section_path: "経費申請",
      content_kind: "text",
      chunk_group_id: "grp-1",
      source_parser: "local_text_structure",
      element_ids: ["el-0000"],
      metadata: { chunk_profile: "structure_v1" },
    },
  ];
  const payload = documentDetail().extraction;
  const htmlContent = [
    "<article>",
    "  <h1>経費申請</h1>",
    "  <p>交通費は1000円です。</p>",
    '  <table data-element-id="tbl-1" class="table-block" data-table-id="tbl-1">',
    "    <tbody>",
    '      <tr data-row="0"><th data-table-id="tbl-1" data-row="0" data-col="0">項目</th><th data-table-id="tbl-1" data-row="0" data-col="1">値</th></tr>',
    '      <tr data-row="1"><td data-table-id="tbl-1" data-row="1" data-col="0">交通費</td><td data-table-id="tbl-1" data-row="1" data-col="1">1000円</td></tr>',
    "    </tbody>",
    "  </table>",
    "</article>",
  ].join("\n");
  const content =
    format === "markdown"
      ? "<!-- page: 1 -->\n# 経費申請\n\n交通費は1000円です。"
      : format === "html"
        ? htmlContent
        : JSON.stringify(format === "chunks" ? { chunks } : payload, null, 2);
  return {
    document_id: "doc-1",
    file_name: "policy.txt",
    format,
    content_type:
      format === "markdown"
        ? "text/markdown; charset=utf-8"
        : format === "html"
          ? "text/html; charset=utf-8"
          : "application/json",
    content,
    payload:
      format === "markdown" || format === "html"
        ? {}
        : format === "chunks"
          ? { chunks }
          : payload,
    chunks: format === "chunks" ? chunks : [],
    parser_backend: "local_partition",
    parser_profile: "local_text_structure",
    page_count: 1,
    element_count: 2,
    table_count: 0,
    asset_count: 0,
  };
}

function retrySegmentsJob() {
  return ingestionJob("QUEUED");
}

function ingestionJob(
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED",
  options: {
    queuedAt?: string;
    startedAt?: string;
    finishedAt?: string;
    errorMessage?: string;
    phase?: "PREPROCESS" | "EXTRACT" | "CHUNK" | "INDEX";
    progress?: Record<string, unknown>;
  } = {}
) {
  return {
    id: status === "RUNNING" ? "job-running-0001" : "job-retry-segments",
    document_id: "doc-1",
    recipe_id: "recipe-1",
    recipe_revision: 1,
    status,
    phase: options.phase ?? "EXTRACT",
    parser_profile: "enterprise_ai_pdf_layout",
    quality_warnings: [],
    skip_reason: null,
    error_message: status === "FAILED" ? options.errorMessage ?? "取込処理に失敗しました。" : null,
    attempt_count: status === "QUEUED" ? 0 : 1,
    max_attempts: 3,
    queued_at: options.queuedAt ?? "2026-06-15T00:00:05Z",
    started_at: status === "QUEUED" ? null : options.startedAt ?? "2026-06-15T00:00:10Z",
    finished_at:
      status === "SUCCEEDED" || status === "FAILED"
        ? options.finishedAt ?? "2026-06-15T00:00:20Z"
        : null,
    progress: options.progress ?? null,
  };
}

function documentDetail(
  options: {
    duplicate?: boolean;
    imagePreview?: boolean;
    pdfPreview?: boolean;
    status?: string;
    preparedArtifact?: boolean;
    brokenPreparedArtifact?: boolean;
    errorMessage?: string;
  } = {}
) {
  const fileName = options.pdfPreview
    ? "policy.pdf"
    : options.imagePreview
      ? "receipt.png"
      : "policy.txt";
  const contentType = options.pdfPreview
    ? "application/pdf"
    : options.imagePreview
      ? "image/png"
      : "text/plain";
  const extension = options.pdfPreview ? ".pdf" : options.imagePreview ? ".png" : ".txt";
  const previewKind = options.pdfPreview ? "pdf" : options.imagePreview ? "image" : "text";
  const modality = options.pdfPreview ? "pdf" : options.imagePreview ? "image" : "text";
  const parserProfile = options.pdfPreview
    ? "enterprise_ai_pdf_layout"
    : options.imagePreview
      ? "enterprise_ai_image_ocr"
      : "local_text_structure";
  const parserBackend =
    options.imagePreview || options.pdfPreview ? "enterprise_ai" : "local_partition";

  const duplicateOfDocumentId = options.duplicate ? "doc-original" : null;
  return {
    id: "doc-1",
    file_name: fileName,
    status: options.status ?? "INDEXED",
    category_name: null,
    content_type: contentType,
    file_size_bytes: 64,
    content_sha256: "a".repeat(64),
    duplicate_of_document_id: duplicateOfDocumentId,
    uploaded_at: "2026-06-15T00:00:00Z",
    indexed_at: "2026-06-15T00:00:03Z",
    object_storage_path: `local://${fileName}`,
    preprocess_artifact:
      options.preparedArtifact || options.brokenPreparedArtifact
        ? {
            derivation_id: "prepared-1",
            profile: options.brokenPreparedArtifact ? "office_to_pdf" : "text_normalize",
            converted: true,
            converter_name: options.brokenPreparedArtifact ? "office_to_pdf" : "text_normalize",
            converter_version: "v1",
            source_content_type: contentType,
            source_sha256: "a".repeat(64),
            // 壊れ状態: 変換は成功(converted=true)したが保存パスが欠落している。
            object_storage_path: options.brokenPreparedArtifact
              ? null
              : "local://policy__prepared.txt",
            content_type: "text/plain",
            sha256: "b".repeat(64),
            file_name: "policy__prepared.txt",
            page_map: {},
            warnings: [],
          }
        : null,
    extraction: {
      raw_text: "経費申請\n交通費は1000円です。",
      document_type: "規程",
      confidence: 0.98,
      warnings: [],
      pages: [{ page_number: 1, width: 612, height: 792, element_ids: ["el-0000", "tbl-1"] }],
      elements: [
        {
          kind: "title",
          text: "経費申請",
          order: 0,
          element_id: "el-0000",
          content_kind: "text",
          source_parser: "local_text_structure",
          page_number: 1,
          bbox: [153, 198, 306, 396],
          section_path: ["経費申請"],
          confidence: 0.98,
          metadata: {},
        },
        {
          kind: "table",
          text: "交通費は1000円です。",
          order: 1,
          element_id: "tbl-1",
          content_kind: "table",
          source_parser: "local_text_structure",
          page_number: 1,
          bbox: [0, 0, 100, 40],
          section_path: ["経費申請", "料金表"],
          confidence: 0.88,
          metadata: {},
        },
      ],
      tables: [
        {
          table_id: "tbl-1",
          element_id: "tbl-1",
          page_number: 1,
          caption: "料金表",
          metadata: { bbox_unit: "percent" },
          cells: [
            {
              row: 0,
              col: 0,
              text: "項目",
              row_span: 1,
              col_span: 1,
              page_number: 1,
              bbox: [0, 0, 50, 10],
              confidence: 0.95,
              metadata: { cell_ref: "A1", bbox_unit: "percent" },
            },
            {
              row: 0,
              col: 1,
              text: "値",
              row_span: 1,
              col_span: 1,
              page_number: 1,
              bbox: [50, 0, 75, 10],
              confidence: 0.95,
              metadata: { cell_ref: "B1", bbox_unit: "percent" },
            },
            {
              row: 1,
              col: 0,
              text: "交通費",
              row_span: 1,
              col_span: 1,
              page_number: 1,
              bbox: [0, 10, 50, 30],
              confidence: 0.92,
              metadata: { cell_ref: "A2", bbox_unit: "percent" },
            },
            {
              row: 1,
              col: 1,
              text: "1000円",
              row_span: 1,
              col_span: 1,
              page_number: 1,
              bbox: [50, 10, 75, 30],
              confidence: 0.92,
              metadata: {
                cell_ref: "B2",
                formula_cell_ref: "B2",
                formula: "=SUM(B2)",
                bbox_unit: "percent",
              },
            },
          ],
        },
      ],
      assets: [],
      parser_artifacts: { parser_backend: "local_partition" },
    },
    error_message: options.errorMessage ?? null,
    duplicate_source: options.duplicate
      ? {
          id: "doc-original",
          file_name: "original.pdf",
          status: "INDEXED",
          uploaded_at: "2026-06-14T00:00:00Z",
          indexed_at: "2026-06-14T00:02:00Z",
        }
      : null,
    knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
    source_profile: {
      original_file_name: fileName,
      sanitized_file_name: fileName,
      extension,
      content_type: contentType,
      inferred_content_type: contentType,
      file_size_bytes: 64,
      content_sha256: "a".repeat(64),
      modality,
      parser_profile: parserProfile,
      parser_backend: parserBackend,
      parser_version: "v1",
      preview_kind: previewKind,
      text_charset: options.imagePreview || options.pdfPreview ? null : "utf-8",
      duplicate_of_document_id: duplicateOfDocumentId,
      unsupported_reason: null,
      quality_status: options.duplicate ? "warning" : "ready",
      quality_warnings: options.duplicate ? ["duplicate_content"] : [],
    },
  };
}

// 既存呼び出しを保ちつつ、documentElement だけでなく main の内部はみ出しも検査する
// 共通ヘルパー(_helpers.ts)へ委譲する。
async function expectNoHorizontalOverflow(page: Page) {
  await expectNoPageOverflow(page);
}

async function isScrollable(locator: Locator): Promise<boolean> {
  return locator.evaluate((el) => el.scrollHeight > el.clientHeight + 1);
}

/** `target` が `origin` の直下（origin の下端より下、かつ近い）にあることを確かめる（#2034）。 */
async function expectBelow(target: Locator, origin: Locator) {
  const [targetBox, originBox] = await Promise.all([target.boundingBox(), origin.boundingBox()]);
  expect(targetBox).not.toBeNull();
  expect(originBox).not.toBeNull();
  if (!targetBox || !originBox) return;
  expect(targetBox.y).toBeGreaterThanOrEqual(originBox.y + originBox.height - 1);
  expect(targetBox.y - (originBox.y + originBox.height)).toBeLessThan(48);
}


for (const width of [1280, 375]) {
  test(`JSONL の本文とコピーは表示用テキストを使う (${width}px)`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await mockDocumentWorkspace(page);
    const display = "id: row-a\nvalue: 10\nid: row-b\nvalue: 20";
    await page.route("**/api/documents/doc-1/recipes/*/extraction-export?format=json", route => {
      const view = extractionExport("json");
      return route.fulfill({ json: { data: { ...view, payload: { ...view.payload, raw_text: "id: row-a\n<<<JSONL_RECORD_BOUNDARY>>>\nid: row-b", display_text: display } }, error_messages: [], warning_messages: [] } });
    });
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", { value: { writeText: async (text: string) => { (window as unknown as { copiedText: string }).copiedText = text; } } });
    });
    await page.goto("/documents/doc-1");
    const panel = page.getByTestId("document-inspector-pane");
    await expect(panel).toContainText("row-a"); await expect(panel).toContainText("row-b");
    await expect(panel).not.toContainText("<<<JSONL_RECORD_BOUNDARY>>>");
    await panel.getByRole("button", { name: "本文をコピー" }).click();
    await expect.poll(() => page.evaluate(() => (window as unknown as { copiedText: string }).copiedText)).toBe(display);
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath("jsonl-display-text.png"), fullPage: true });
  });
}
