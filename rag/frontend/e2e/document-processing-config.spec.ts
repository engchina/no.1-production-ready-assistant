import { expect, type Page, test } from "./fixtures/test";

import type { DocumentProcessingConfig, DocumentRecipeStep } from "../src/lib/api";
import { expectNoPageOverflow, mockAuthUser, mockDatabaseReady, mockLocalAuth } from "./_helpers";

const documentDetail = {
  id: "doc-1",
  file_name: "policy.pdf",
  status: "INDEXED",
  category_name: null,
  content_type: "application/pdf",
  file_size_bytes: 1024,
  content_sha256: "abc",
  duplicate_of_document_id: null,
  uploaded_at: "2026-06-15T00:00:00Z",
  indexed_at: "2026-06-15T00:05:00Z",
  knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
  object_storage_path: "indexed/doc-1.pdf",
  extraction: {
    raw_text: "経費申請の規程",
    elements: [
      {
        kind: "text",
        text: "経費申請の規程",
        order: 0,
        element_id: "e0",
        metadata: {},
      },
    ],
  },
  error_message: null,
};

function ok(data: unknown) {
  return { data, error_messages: [], warning_messages: [] };
}

function config(): DocumentProcessingConfig {
  return {
    preprocess_profile: null,
    parser_adapter_backend: null,
    parser_docling_enabled: null,
    parser_unstructured_enabled: null,
    chunking_strategy: null,
    chunk_size: 800,
    chunk_overlap: null,
    chunk_min_chars: null,
    graph_profile: null,
    field_extraction_enabled: null,
    vision_enabled: null,
    heading_level_inference_enabled: null,
    navigation_summary_enabled: null,
    auto_parse_after_preprocess_enabled: null,
    auto_chunk_after_extract_enabled: null,
    auto_index_after_chunk_enabled: null,
  };
}

const effectiveBase: DocumentProcessingConfig = {
  ...config(),
  preprocess_profile: "office_to_pdf",
  parser_adapter_backend: "docling",
  chunking_strategy: "page_level",
  graph_profile: "off",
  // 全体で明示的に有効にした設定の継承・上書きを確かめる。
  entity_index_enabled: true,
  entity_name_columns: [],
  entity_attribute_columns: [],
  field_extraction_enabled: false,
  vision_enabled: false,
  heading_level_inference_enabled: false,
  navigation_summary_enabled: false,
  section_rules_mode: "parser",
  auto_parse_after_preprocess_enabled: true,
  auto_chunk_after_extract_enabled: true,
  auto_index_after_chunk_enabled: true,
};

function recipeSteps(status: string): DocumentRecipeStep[] {
  const phases = ["PREPROCESS", "EXTRACT", "CHUNK", "INDEX"] as const;
  const completedCount: Record<string, number> = {
    UPLOADED: 0,
    REVIEW: 2,
    INDEXED: 4,
  };
  return phases.map((phase, index) => ({
    phase,
    status: index < (completedCount[status] ?? 0) ? "SUCCEEDED" : "PENDING",
    started_at: null,
    finished_at: null,
    error_message: null,
  }));
}

async function mockWorkspace(
  page: Page,
  options: {
    documentStatus?: string;
    putFails?: boolean;
    previewFails?: boolean;
    recipeCount?: number;
    /** プレビュー結果の chunk に足す metadata(例: 親子階層の親の ID と本文)。 */
    previewChunkMetadata?: Record<string, string>;
    /** 保存済みの chunk を 1 件返し、その metadata に足す値(例: 以前の版の縮退の印)。 */
    storedChunkMetadata?: Record<string, string>;
    /** 設定を保存するまで、レシピの処理の投入を 409 にする文(#2639)。 */
    enqueueConflictUntilSaved?: string;
  } = {}
) {
  const status = options.documentStatus ?? "INDEXED";
  let processing = config();
  let configRevision = 1;
  let saved: DocumentProcessingConfig | null = null;
  let ingestionPosts = 0;
  let previewPosts = 0;
  let previewPayload: unknown = null;
  const recipeResponse = (slotNo = 1) => {
    const effective = { ...effectiveBase };
    for (const [key, value] of Object.entries(processing)) {
      if (value !== null) Object.assign(effective, { [key]: value });
    }
    return {
      recipe_id: `recipe-${slotNo}`,
      document_id: "doc-1",
      slot_no: slotNo,
      status,
      failed_phase: null,
      processing_config: processing,
      effective_processing_config: effective,
      preprocess_artifact: null,
      active_extraction_recipe_id:
        status === "INDEXED" || status === "REVIEW" ? `er-recipe-${slotNo}-r1` : null,
      active_chunk_set_id: status === "INDEXED" ? `chunk-set-recipe-${slotNo}` : null,
      chunk_count: status === "INDEXED" ? 2 : 0,
      vector_count: status === "INDEXED" ? 2 : 0,
      config_revision: configRevision,
      materialized_revision: status === "INDEXED" ? 1 : null,
      searchable: status === "INDEXED",
      needs_reprocessing: false,
      error_message: null,
      steps: recipeSteps(status),
      created_at: "2026-06-15T00:00:00Z",
      updated_at: "2026-06-15T00:00:20Z",
      started_at: null,
      finished_at: status === "INDEXED" ? "2026-06-15T00:00:20Z" : null,
    };
  };

  // 処理設定パネルは文書解析の設定から対応形式を読む。
  await page.route("**/api/settings/parser-adapters", (route) =>
    route.fulfill({ json: ok({ capabilities: [] }) })
  );
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: ok({
        items: [{ id: "kb-1", name: "社内規程" }],
        total: 1,
        limit: 100,
        offset: 0,
        has_next: false,
      }),
    })
  );
  await page.route("**/api/documents/doc-1/recipes", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        json: ok(
          Array.from({ length: options.recipeCount ?? 1 }, (_, index) =>
            recipeResponse(index + 1)
          )
        ),
      });
    }
    return route.fulfill({ status: 404, json: { data: null, error_messages: [], warning_messages: [] } });
  });
  await page.route("**/api/documents/doc-1/recipes/recipe-1", (route) => {
    if (route.request().method() === "PUT") {
      if (options.putFails) {
        return route.fulfill({
          status: 409,
          json: {
            data: null,
            error_messages: ["取込ジョブの実行中は設定を変更できません。"],
            warning_messages: [],
          },
        });
      }
      saved = route.request().postDataJSON() as DocumentProcessingConfig;
      processing = saved;
      configRevision += 1;
    }
    return route.fulfill({ json: ok(recipeResponse()) });
  });
  await page.route("**/api/documents/doc-1/recipes/recipe-1/ingestion-jobs**", (route) => {
    if (route.request().method() === "POST") {
      ingestionPosts += 1;
      if (options.enqueueConflictUntilSaved && configRevision === 1) {
        return route.fulfill({
          status: 409,
          json: {
            data: null,
            error_messages: [options.enqueueConflictUntilSaved],
            warning_messages: [],
          },
        });
      }
    }
    return route.fulfill({ json: ok([]) });
  });
  await page.route("**/api/documents/doc-1/recipes/recipe-1/chunks", (route) =>
    route.fulfill({
      json: ok(
        options.storedChunkMetadata
          ? [
              {
                document_id: "doc-1",
                chunk_id: "doc-1:recipe-1:0",
                chunk_index: 0,
                text: "保存済みの合成の本文です。",
                page_start: 1,
                page_end: 1,
                bbox: null,
                section_path: "節 A",
                content_kind: "text",
                chunk_group_id: null,
                source_parser: "markdown",
                element_ids: ["e0"],
                metadata: { ...options.storedChunkMetadata },
              },
            ]
          : []
      ),
    })
  );
  await page.route("**/api/documents/doc-1/recipes/recipe-1/chunk-preview", (route) => {
    previewPosts += 1;
    previewPayload = route.request().postDataJSON();
    if (options.previewFails) {
      return route.fulfill({
        status: 503,
        json: {
          data: null,
          error_messages: ["分割サービスに接続できませんでした。"],
          warning_messages: [],
        },
      });
    }
    return route.fulfill({
      json: ok({
        chunks: [
          {
            document_id: "doc-1",
            chunk_id: "preview:recipe-1:0",
            chunk_index: 0,
            text: "経費申請は部門長の承認後、経理部が確認します。",
            page_start: 1,
            page_end: 1,
            bbox: null,
            section_path: "第1章 > 経費申請",
            content_kind: "text",
            chunk_group_id: null,
            source_parser: "docling",
            element_ids: ["e0"],
            metadata: {
              context_header: "policy.pdf > 第1章 > 経費申請",
              ...options.previewChunkMetadata,
            },
          },
        ],
        stats: {
          chunk_count: 1,
          min_chars: 24,
          average_chars: 24,
          max_chars: 24,
          overflow_count: 0,
          embedding_overflow_count: 0,
        },
        warnings: [],
      }),
    });
  });
  await page.route("**/api/documents/doc-1/recipes/recipe-1/extraction-export**", (route) =>
    route.fulfill({
      json: ok({
        document_id: "doc-1",
        file_name: "policy.pdf",
        // 文書の詳細は抽出結果を JSON で 1 回だけ取得する（Markdown / HTML はコピー・ダウンロードのときだけ。#561）。
        format: "json",
        content_type: "application/json; charset=utf-8",
        content: "",
        payload: documentDetail.extraction,
        chunks: [],
        parser_backend: "docling",
        parser_profile: "docling",
        page_count: 0,
        element_count: 0,
        table_count: 0,
        asset_count: 0,
      }),
    })
  );
  await page.route("**/api/documents/doc-1/ingestion-jobs**", (route) =>
    route.fulfill({ json: ok([]) })
  );
  await page.route("**/api/documents/doc-1/chunk-sets", (route) => route.fulfill({ json: ok([]) }));
  await page.route("**/api/documents/doc-1/ingestion-segments", (route) =>
    route.fulfill({ json: ok([]) })
  );
  await page.route("**/api/documents/doc-1/knowledge-bases", (route) =>
    route.fulfill({ json: ok([{ id: "kb-1", name: "社内規程" }]) })
  );
  await page.route("**/api/documents/doc-1/content**", (route) =>
    route.fulfill({ status: 204, body: "" })
  );
  await page.route("**/api/documents/doc-1/recipes/recipe-1/content**", (route) =>
    route.fulfill({ status: 204, body: "" })
  );
  await page.route("**/api/documents/doc-1", (route) =>
    route.fulfill({
      json: ok({ ...documentDetail, status }),
    })
  );

  return {
    saved: () => saved,
    ingestionPosts: () => ingestionPosts,
    previewPosts: () => previewPosts,
    previewPayload: () => previewPayload,
  };
}

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

test("文書処理設定を保存し、手動再処理を案内する", async ({ page }) => {
  const state = await mockWorkspace(page);
  await page.goto("/documents/doc-1");

  // 読み込み中は中に「処理レシピを読み込んでいます」の region があり、部分一致だと 2 つに当たる（strict mode violation）。
  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  await expect(panel).toContainText("Office→PDF");
  await expect(panel).toContainText("Docling");
  for (const label of [
    "ファイル準備後に抽出へ進む",
    "抽出後に Chunk 作成へ進む",
    "Chunk 後に Embedding / 索引へ進む",
  ]) {
    await expect(panel.getByText(label, { exact: true }).locator("../..")).toContainText("有効");
  }
  await panel.getByRole("button", { name: "処理設定を編集" }).click();

  const headingLevelsRow = panel.getByTestId("document-processing-config-editor-items")
    .locator('[data-config-field="heading_level_inference_enabled"]');
  await expect(headingLevelsRow).toContainText("Docling の解析結果で、しおりの階層を使えず");
  await expect(headingLevelsRow).toContainText("番号から階層を決められない見出しの割合と見出し数が設定の条件を満たす場合");
  await expect(headingLevelsRow).toContainText("通常は文書ごとに 1 回で、再試行する場合があります。");
  await headingLevelsRow.screenshot({ path: test.info().outputPath("heading-level-description.png") });

  await panel.getByRole("group", { name: "文書解析" }).getByText("上書き").click();
  await panel.getByRole("combobox", { name: "文書解析" }).click();
  // 解析エンジンの選択肢は文書解析の画面のカードと同じ並び（PARSER_BACKEND_ORDER）。
  // 構造化レコード JSONL・XML ツリー JSON の専用のエンジンは削除した（#21）。
  await expect(page.getByRole("option")).toHaveText(["Docling", "Unstructured"]);
  await page.getByRole("option", { name: "Unstructured" }).click();
  // Vision は解析エンジンに関係なくレシピで選べる。「図表 VLM 要約」は Vision に統合した(#497)。
  await expect(panel.getByText("図表 VLM 要約")).toHaveCount(0);
  // 概要(読み取り専用)にも同じ見出しがあるので、編集欄(説明付き)に絞る。
  const visionRow = panel
    .getByText("図・画像を AI で読み取る", { exact: true })
    .locator("../..")
    .filter({ hasText: "どの解析エンジンでも使えます" });
  await expect(visionRow).toBeVisible();
  await visionRow.getByText("上書き", { exact: true }).click();
  await visionRow.getByText("有効", { exact: true }).click();
  await panel.getByRole("button", { name: "構築設定を保存" }).click();

  await expect(page.getByText(/この文書の処理設定を保存しました/)).toBeVisible();
  expect(state.saved()).toMatchObject({
    parser_adapter_backend: "unstructured",
    vision_enabled: true,
    chunk_size: 800,
  });
  expect(state.ingestionPosts()).toBe(0);
  await expectNoPageOverflow(page);
});

// 前処理が excel_to_jsonl のときだけ「Excel の読み方」を出し、選択肢を保存する(#1221)。
test("Excel の読み方を上書きして保存する", async ({ page }) => {
  const state = await mockWorkspace(page);
  await page.goto("/documents/doc-1");
  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  await panel.getByRole("button", { name: "処理設定を編集" }).click();
  // Office→PDF のあいだは出さない。
  await expect(panel.getByTestId("document-excel-options")).toHaveCount(0);

  await panel.getByRole("group", { name: "ファイル準備", exact: true }).getByText("上書き").click();
  await panel.getByRole("combobox", { name: "ファイル準備", exact: true }).click();
  await page.getByRole("option", { name: "Excel→JSONL" }).click();
  // JSONL のファイル準備は、分割（固定分割符・記録の境界）も見える上書きにする（#2457）。解析エンジンは
  // 変えない（成果物の JSONL は Docling・Unstructured のサービスが読む。#21）。
  await expect(panel.getByText(/分割（固定分割符・記録の境界）を合わせて上書きしました/)).toBeVisible();
  // 文書解析は全体の既定を継承したまま（上書きの選択欄を出さない）。
  await expect(panel.getByRole("combobox", { name: "文書解析", exact: true })).toHaveCount(0);
  await expect(panel.getByRole("combobox", { name: "文書分割", exact: true })).toContainText(
    "固定分割符"
  );
  await expect(panel.getByRole("textbox", { name: "固定分割符文字列" })).toHaveValue(
    "<<<JSONL_RECORD_BOUNDARY>>>"
  );
  const excel = panel.getByTestId("document-excel-options");
  await expect(excel).toBeVisible();
  await expect(excel.getByTestId("document-excel-options-inherited")).toContainText("自動");

  await excel.getByText("上書き", { exact: true }).click();
  // 帳票の判定の下限（#2516）: 自動の読み方のときだけ出し、0〜1 の外の値は保存しない。
  const threshold = excel.getByRole("spinbutton", { name: "帳票と判定する下限" });
  await expect(threshold).toHaveValue("0.5");
  await threshold.fill("1.5");
  await expect(excel.getByText("0 から 1 までの数を入力してください。")).toBeVisible();
  await threshold.fill("0.7");
  await expect(excel.getByText("0 から 1 までの数を入力してください。")).toHaveCount(0);
  // 表の分割（#2516）: 分けないときは空の行・列の数を出さない。
  await expect(excel.getByRole("combobox", { name: "表を分ける空の行" })).toContainText("1 行以上");
  await excel.getByRole("group", { name: "1 つのシートの複数の表" }).getByText("分けない").click();
  await expect(excel.getByRole("combobox", { name: "表を分ける空の行" })).toHaveCount(0);
  await excel.getByRole("group", { name: "1 つのシートの複数の表" }).getByText("分けて読む").click();
  await excel.getByRole("combobox", { name: "表を分ける空の行" }).click();
  await page.getByRole("option", { name: "2 行以上", exact: true }).click();
  await excel.getByRole("combobox", { name: "表を分ける空の列" }).click();
  await page.getByRole("option", { name: "3 列以上", exact: true }).click();
  // 非表示の行・列（#2526）: 既定は読まない。
  const hiddenCells = excel.getByRole("group", { name: "非表示の行・列" });
  await expect(hiddenCells.getByRole("button", { name: "読まない", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true"
  );
  await hiddenCells.getByText("読む", { exact: true }).click();

  await excel.getByRole("combobox", { name: "読み方" }).click();
  // 帳票（方眼紙）の読み方も選べる（#2516）。
  await expect(page.getByRole("option", { name: "帳票（項目ごと）" })).toBeVisible();
  if (process.env.RAG_E2E_SCREENSHOT_DIR) {
    await page.screenshot({
      path: `${process.env.RAG_E2E_SCREENSHOT_DIR}/excel-options-mode-${test.info().project.name}.png`,
    });
  }
  await page.getByRole("option", { name: "表（1 行ずつ）" }).click();
  await expect(threshold).toHaveCount(0);
  await excel.getByRole("spinbutton", { name: "表頭の行" }).fill("2");
  await excel.getByRole("textbox", { name: "読まない列" }).fill("備考、F");
  await excel.getByRole("textbox", { name: "読む範囲" }).fill("費目!A2:D200、A1:F50");
  await excel.getByRole("group", { name: "非表示のシート" }).getByText("読む", { exact: true }).click();
  // 列の役割（#1281）: 判定の有無と列ごとの指定。読めない指定は欄の下に示し、保存しない。
  await excel.getByRole("group", { name: "列の役割", exact: true }).getByText("判定しない").click();
  const roles = excel.getByRole("textbox", { name: "列の役割の指定" });
  await roles.fill("D=例示、設定値=現在値、E=実際");
  await expect(excel.getByText("読めない指定があります: E=実際")).toBeVisible();
  await roles.fill("D=例示、設定値=現在値");
  await expect(excel.getByText(/読めない指定があります/)).toHaveCount(0);
  await expectNoPageOverflow(page);
  await panel.getByRole("button", { name: "構築設定を保存" }).click();

  await expect(page.getByText(/この文書の処理設定を保存しました/)).toBeVisible();
  expect(state.saved()).toMatchObject({
    preprocess_profile: "excel_to_jsonl",
    chunking_strategy: "fixed_delimiter",
    chunk_delimiter: "<<<JSONL_RECORD_BOUNDARY>>>",
    excel_options: {
      mode: "table",
      header_row: 2,
      header_row_count: 1,
      exclude_columns: ["備考", "F"],
      include_hidden_sheets: true,
      include_hidden_cells: true,
      ranges: ["費目!A2:D200", "A1:F50"],
      split_tables: true,
      table_gap_rows: 2,
      table_gap_columns: 3,
      form_detection_threshold: 0.7,
      column_role_detection: "off",
      column_roles: { D: "example", 設定値: "current" },
    },
  });
});

// #1388: 実体・別名索引（文書レシピ。全体の既定は有効）と、その列を画面で選んで保存する。
test("実体・別名索引は全体が有効なら継承し、列の上書きと無効化を保存できる", async ({ page }) => {
  const state = await mockWorkspace(page);
  await page.goto("/documents/doc-1");
  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  const summary = panel.getByTestId("document-processing-config-summary");
  await expect(summary.locator('[data-config-field="entity_index_enabled"]')).toContainText("有効");
  await panel.getByRole("button", { name: "処理設定を編集" }).click();
  const editor = panel.getByTestId("document-processing-config-editor-items");
  const entityRow = editor.locator('[data-config-field="entity_index_enabled"]');
  await expect(entityRow).toContainText("グローバル設定に従う: 有効");
  await expect(entityRow).toContainText("全体の既定は無効です。");
  // 有効（継承）のあいだは列の行を出し、列は全体の既定（列名で決める）に従う。
  const columns = panel.getByTestId("document-entity-columns");
  await expect(columns.getByTestId("document-entity-columns-inherited")).toContainText(
    "名前の列: 列名で決める · 属性の列: 列名で決める"
  );

  // 無効にすると列の行を閉じる（使わない文書）。
  await entityRow.getByText("上書き", { exact: true }).click();
  await entityRow.getByText("無効", { exact: true }).click();
  await expect(columns).toHaveCount(0);
  await entityRow.getByText("有効", { exact: true }).click();
  await expect(columns).toBeVisible();

  await columns.getByText("上書き", { exact: true }).click();
  await columns.getByRole("textbox", { name: "名前の列" }).fill("システムID、正式名称、略称");
  await columns.getByRole("textbox", { name: "属性の列" }).fill("担当部署、");
  // 上限を超えた列名は欄の下に示す（backend の検証と同じ）。
  await columns.getByRole("textbox", { name: "属性の列" }).fill(`${"あ".repeat(81)}`);
  await expect(columns.getByText("列名は 1 つ 80 文字までです。")).toBeVisible();
  await columns.getByRole("textbox", { name: "属性の列" }).fill("担当部署、重要度");
  await expect(columns.getByText(/列名は/)).toHaveCount(0);
  await expectNoPageOverflow(page);
  if (process.env.RAG_E2E_SCREENSHOT_DIR) {
    await entityRow.locator("..").screenshot({
      path: `${process.env.RAG_E2E_SCREENSHOT_DIR}/recipe-entity-index-${test.info().project.name}.png`,
    });
  }
  await panel.getByRole("button", { name: "構築設定を保存" }).click();

  await expect(page.getByText(/この文書の処理設定を保存しました/)).toBeVisible();
  expect(state.saved()).toMatchObject({
    entity_index_enabled: true,
    entity_name_columns: ["システムID", "正式名称", "略称"],
    entity_attribute_columns: ["担当部署", "重要度"],
  });

  // 列を継承に戻し、実体・別名索引を無効にして保存する。
  await columns.getByText("グローバルを継承", { exact: true }).click();
  await entityRow.getByText("無効", { exact: true }).click();
  await panel.getByRole("button", { name: "構築設定を保存" }).click();
  await expect.poll(() => state.saved()).toMatchObject({
    entity_index_enabled: false,
    entity_name_columns: null,
    entity_attribute_columns: null,
  });
  await expect(summary.locator('[data-config-field="entity_index_enabled"]')).toContainText("無効");
});

// 要約と上書きの一覧は、同じ項目を取込の処理順に並べる(#523)。
const PROCESSING_ORDER = [
  "preprocess_profile",
  "auto_parse_after_preprocess_enabled",
  "parser_adapter_backend",
  "vision_enabled",
  "heading_level_inference_enabled",
  "field_extraction_enabled",
  "section_rules_mode",
  "navigation_summary_enabled",
  "auto_chunk_after_extract_enabled",
  "chunking_strategy",
  "auto_index_after_chunk_enabled",
  "entity_index_enabled",
  "graph_profile",
];
// 実体・別名索引も専用画面への導線を持つ。
const GLOBAL_LINK_COUNT = PROCESSING_ORDER.length;
// 上書きの一覧は、実体・別名索引が有効（全体の既定）のとき、その右に「実体・別名索引の列」の行を出す。
const EDITOR_ORDER = PROCESSING_ORDER.flatMap((field) =>
  field === "entity_index_enabled" ? [field, "entity_columns"] : [field]
);

/** 画面上の読み順（上から下、同じ行は左から右）に並べた data-config-field。 */
async function fieldsInReadingOrder(container: ReturnType<Page["locator"]>) {
  const boxes = await container.locator("[data-config-field]").evaluateAll((elements) =>
    elements.map((element) => {
      const rect = element.getBoundingClientRect();
      return {
        field: element.getAttribute("data-config-field") ?? "",
        top: Math.round(rect.top),
        left: Math.round(rect.left),
      };
    })
  );
  return boxes
    .sort((a, b) => (a.top === b.top ? a.left - b.left : a.top - b.top))
    .map((box) => box.field);
}

test("要約と上書きの一覧は、同じ項目を取込の処理順に並べる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockWorkspace(page);
  await page.goto("/documents/doc-1");

  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  const summary = panel.getByTestId("document-processing-config-summary");
  await expect(summary.locator("[data-config-field]")).toHaveCount(PROCESSING_ORDER.length);
  await expect(summary).not.toContainText("文脈ヘッダを検索対象へ追加");
  expect(await fieldsInReadingOrder(summary)).toEqual(PROCESSING_ORDER);
  await summary.locator('[data-config-field="section_rules_mode"]').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("recipe-summary-order.png") });
  await expect(summary.getByRole("heading")).toHaveText([
    "1. ファイル準備",
    "2. 抽出",
    "3. Chunk 作成",
    "4. Embedding / 索引",
  ]);

  await panel.getByRole("button", { name: "処理設定を編集" }).click();
  const editor = panel.getByTestId("document-processing-config-editor-items");
  await expect(editor.locator("[data-config-field]")).toHaveCount(EDITOR_ORDER.length);
  // 2 列でも、左から右・上から下に読むと処理順になる。
  expect(await fieldsInReadingOrder(editor)).toEqual(EDITOR_ORDER);
  await editor.locator('[data-config-field="section_rules_mode"]').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("recipe-editor-order.png") });
  await expect(panel).toContainText("すべてグローバル継承");
  // 文脈ヘッダは全体設定だけで決まり、レシピには出ない。
  await expect(editor.locator('[data-config-field="chunk_context_header_enabled"]')).toHaveCount(0);
  await expectNoPageOverflow(page);
});

// #528: 「グローバル設定に従う」の各行から、その全体の既定を変える画面へ移動できる。
test("グローバル設定に従う行に、全体の既定を変える画面へのリンクを出す", async ({ page }) => {
  await mockWorkspace(page);
  await page.route("**/api/settings/pipeline", (route) =>
    route.fulfill({
      json: ok({
        auto_parse_after_preprocess_enabled: true,
        auto_chunk_after_extract_enabled: true,
        auto_index_after_chunk_enabled: true,
        recipe_defaults: effectiveBase,
        vector_index_profile: "accurate",
        config_source: "runtime",
      }),
    })
  );
  await page.goto("/documents/doc-1");

  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  await panel.getByRole("button", { name: "処理設定を編集" }).click();
  const editor = panel.getByTestId("document-processing-config-editor-items");
  const links = editor.getByRole("link", { name: / のグローバル設定を開く$/ });
  // すべて継承のときは、全体の既定の画面がある 12 行すべてにリンクがある。
  await expect(links).toHaveCount(GLOBAL_LINK_COUNT);
  await expect(editor.locator('[data-config-field="entity_index_enabled"]')).toContainText(
    "グローバル設定に従う: 有効"
  );
  await expect(
    editor.getByRole("link", { name: "図・画像を AI で読み取る のグローバル設定を開く" })
  ).toHaveAttribute("href", "/settings/parser-adapters#post-parse-vision");
  await expect(
    editor.getByRole("link", { name: "文書構造索引の構築 のグローバル設定を開く" })
  ).toHaveAttribute("href", "/settings/graph");
  const visionRow = editor.locator('[data-config-field="vision_enabled"]');
  await expect(visionRow).toContainText("グローバル設定に従う: 無効");
  await expect(visionRow.getByRole("link", { name: /グローバル設定を開く/ })).toHaveText(
    "グローバル設定を開く"
  );

  if (process.env.RAG_E2E_SCREENSHOT_DIR) {
    await editor.screenshot({
      path: `${process.env.RAG_E2E_SCREENSHOT_DIR}/recipe-global-links-${test.info().project.name}.png`,
    });
  }

  // 上書きした行には出さない（全体の既定を使っていないため）。
  await visionRow.getByText("上書き", { exact: true }).click();
  await expect(links).toHaveCount(GLOBAL_LINK_COUNT - 1);
  await visionRow.getByText("グローバルを継承", { exact: true }).click();
  await expect(links).toHaveCount(GLOBAL_LINK_COUNT);

  // 自動進行のゲートは設定の概要の工程の流れへ移動する。
  await editor
    .getByRole("link", { name: "抽出後に Chunk 作成へ進む のグローバル設定を開く" })
    .click();
  await expect(page).toHaveURL(/\/settings\/pipeline#pipeline-gate-auto-chunk$/);
  await expect(page.getByRole("switch", { name: "抽出後に Chunk 作成へ進む" })).toBeInViewport();
  await expectNoPageOverflow(page);
});

test("権限のない設定画面へのグローバル設定のリンクは出さない", async ({ page }) => {
  await mockAuthUser(page, {
    permissions: ["menu.file_list", "menu.settings_chunking"],
    allowed_knowledge_base_ids: null,
  });
  await mockWorkspace(page);
  await page.goto("/documents/doc-1");

  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  await panel.getByRole("button", { name: "処理設定を編集" }).click();
  const editor = panel.getByTestId("document-processing-config-editor-items");
  await expect(editor.locator("[data-config-field]")).toHaveCount(EDITOR_ORDER.length);
  // 文書分割の画面だけ。
  await expect(editor.getByRole("link", { name: / のグローバル設定を開く$/ })).toHaveCount(1);
});

test("レシピ比較で空の引用を理由付きの状態として表示する", async ({ page }) => {
  await mockWorkspace(page, { recipeCount: 2 });
  const searchRequests: Array<Record<string, unknown>> = [];
  await page.route("**/api/search", (route) => {
    searchRequests.push(route.request().postDataJSON() as Record<string, unknown>);
    return route.fulfill({
      json: ok({
        answer: "取得候補の関連度が十分でないため、回答に使える根拠がありませんでした。",
        citations: [],
        trace_id: "trace-no-results",
        elapsed_ms: 12,
        diagnostics: {},
      }),
    });
  });
  await page.goto("/documents/doc-1");

  await page.getByRole("button", { name: "検索結果を横並びで比較" }).click();
  await page.getByLabel("比較する質問").fill("承認条件を教えて");
  await page.getByRole("button", { name: "比較", exact: true }).click();

  await expect(page.getByText("一致する根拠が見つかりませんでした。")).toHaveCount(2);
  await expect(
    page.getByText("取得候補の関連度が十分でないため、回答に使える根拠がありませんでした。")
  ).toHaveCount(2);
  expect(
    searchRequests
      .map((request) => (request.filters as Record<string, string>).chunk_set_id)
      .sort()
  ).toEqual(["chunk-set-recipe-1", "chunk-set-recipe-2"]);
  // 引用の候補を比べるので、回答は作らずに検索だけを頼む（#593）。
  expect(searchRequests.map((request) => request.retrieval_only)).toEqual([true, true]);
  await expectNoPageOverflow(page);
});

test("レシピ比較の引用も共有 score layout で表示する", async ({ page }) => {
  await mockWorkspace(page, { recipeCount: 2 });
  await page.route("**/api/search", (route) => {
    const request = route.request().postDataJSON() as {
      filters?: { chunk_set_id?: string };
    };
    const chunkSetId = request.filters?.chunk_set_id ?? "chunk-set";
    return route.fulfill({
      json: ok({
        answer: "承認条件の比較結果です。",
        citations: [
          {
            document_id: "doc-1",
            chunk_id: `doc-1:${chunkSetId}:0`,
            text: "申請は上長の承認後に精算します。",
            score: 0.048,
            rerank_score: 0.869,
            file_name: "policy.pdf",
            category_name: null,
            metadata: {},
          },
        ],
        trace_id: "trace-comparison",
        elapsed_ms: 12,
        diagnostics: {},
      }),
    });
  });
  await page.goto("/documents/doc-1");

  await page.getByRole("button", { name: "検索結果を横並びで比較" }).click();
  await page.getByLabel("比較する質問").fill("承認条件を教えて");
  await page.getByRole("button", { name: "比較", exact: true }).click();

  await expect(page.getByTestId("citation-score-panel")).toHaveCount(2);
  await expect(page.getByRole("meter", { name: /取得スコア/ })).toHaveCount(0);
  await expect(page.getByRole("meter", { name: "Rerank スコア: 0.869" })).toHaveCount(2);
  await expectNoPageOverflow(page);
});

test("保存失敗時は編集値を保持する", async ({ page }) => {
  await mockWorkspace(page, { putFails: true });
  await page.goto("/documents/doc-1");

  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  await panel.getByRole("button", { name: "処理設定を編集" }).click();
  await panel.getByRole("group", { name: "文書解析" }).getByText("上書き").click();
  await panel.getByRole("combobox", { name: "文書解析" }).click();
  await page.getByRole("option", { name: "Unstructured" }).click();
  await panel.getByRole("button", { name: "構築設定を保存" }).click();

  await expect(panel).toContainText("取込ジョブの実行中は設定を変更できません");
  await expect(panel.getByRole("combobox", { name: "文書解析" })).toContainText("Unstructured");
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(
    `処理の開始の失敗は、設定を直して保存したら消える (${viewport.name})`,
    { tag: "@responsive" },
    async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const conflict = "この形式は原本のままでは解析できません。ファイル準備で変換を選んでください。";
      const state = await mockWorkspace(page, { enqueueConflictUntilSaved: conflict });
      await page.goto("/documents/doc-1");

      const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
      const actions = panel.getByTestId("document-recipe-actions");
      await actions.getByRole("button", { name: "再処理", exact: true }).click();
      await expect(panel.getByText(conflict)).toBeVisible();
      expect(state.ingestionPosts()).toBe(1);

      // 原因の設定を直して保存したら、前の失敗の文は出し続けない(#2639)。
      await panel.getByRole("button", { name: "処理設定を編集" }).click();
      await panel.getByRole("group", { name: "文書解析" }).getByText("上書き").click();
      await panel.getByRole("combobox", { name: "文書解析" }).click();
      await page.getByRole("option", { name: "Unstructured" }).click();
      await panel.getByRole("button", { name: "構築設定を保存" }).click();
      await expect(page.getByText(/この文書の処理設定を保存しました/)).toBeVisible();
      await expect(panel.getByText(conflict)).toHaveCount(0);
      await expectNoPageOverflow(page);
    }
  );
}

test("派生レイヤー状態チップ・項目の定義が 0 件の警告・抽出セクションを表示する", async ({ page }) => {
  await mockWorkspace(page);
  // 後着 route が優先される。派生 layer 状態と抽出 payload を上書きする。
  await page.route("**/api/documents/doc-1/chunk-sets", (route) =>
    route.fulfill({
      json: ok([
        {
          chunk_set_id: "chunk-set-recipe-1",
          extraction_recipe_id: "er-recipe-1-r1",
          extraction_status: "materialized",
          extraction_reason: null,
          status: "INDEXED",
          chunk_count: 2,
          vector_count: 2,
          is_serving: true,
          created_at: "2026-06-15T00:00:10Z",
          extraction_id: "ex-1",
          parser: "docling",
          preprocess: "office_to_pdf",
          knowledge_base_ids: ["kb-1"],
          serving_knowledge_base_ids: ["kb-1"],
          layer_statuses: {
            metadata: { layer_id: null, requested: false, status: "not_requested", reason: null },
            graph: {
              layer_id: "gl-1",
              requested: true,
              status: "planned_only",
              reason: "文書構造索引は構築計画に含まれていますが、まだ実体化していません。",
            },
            navigation: {
              layer_id: "nv-1",
              requested: true,
              status: "materialized",
              reason: "ナビゲーションは 2 件の章節として実体化済みです。",
            },
          },
        },
      ]),
    })
  );
  await page.route("**/api/documents/doc-1/recipes/recipe-1/extraction-export**", (route) =>
    route.fulfill({
      json: ok({
        document_id: "doc-1",
        file_name: "policy.pdf",
        format: "json",
        content_type: "application/json",
        content: "",
        payload: {
          raw_text: "経費申請の規程",
          elements: [{ kind: "text", text: "経費申請の規程", order: 0, element_id: "e0" }],
          navigation: [
            { section_id: "sec-1", title: "第1章 総則", depth: 0, page_start: 1, summary: "章の要約テキスト" },
            { section_id: "sec-2", title: "1.1 目的", depth: 1, parent_section_id: "sec-1" },
          ],
          fields: [
            { name: "請求書番号", value: "INV-1", value_type: "string", confidence: 0.9 },
          ],
          assets: [
            { asset_id: "fig-1", kind: "figure", alt_text: "承認フロー図", summary: "承認の流れを示す図", page_number: 2 },
          ],
        },
        chunks: [],
        parser_backend: "docling",
        parser_profile: "docling",
        page_count: 1,
        element_count: 1,
        table_count: 0,
        asset_count: 1,
      }),
    })
  );
  // 全体の既定を 0 件で保存した環境（未保存なら標準の項目を使う。#556）。
  await page.route("**/api/settings/extraction-fields", (route) =>
    route.fulfill({ json: ok({ fields: [], uses_standard: false }) })
  );

  await page.goto("/documents/doc-1");

  // レシピヘッダに派生 layer 状態チップ(not_requested は出さない)。
  const layerChips = page.getByTestId("recipe-layer-statuses");
  await expect(layerChips.getByTestId("recipe-layer-graph")).toContainText("計画のみ");
  await expect(layerChips.getByTestId("recipe-layer-navigation")).toContainText("構築済み");
  await expect(layerChips.getByTestId("recipe-layer-metadata")).toHaveCount(0);

  // 構造化要素タブに章節ナビ・図表要約・抽出項目の折りたたみセクション。章節は章節の API から
  // 読み、抽出の章節の要約を添える（#713）。
  await page.route("**/api/documents/doc-1/sections**", (route) =>
    route.fulfill({
      json: ok({
        document_id: "doc-1",
        source: "extraction",
        rules_mode: "parser",
        sections: [
          {
            id: "sec-1",
            title: "第1章 総則",
            level: 1,
            page_start: 1,
            page_end: null,
            origin: "extraction",
            source_section_id: "sec-1",
            edited: false,
          },
        ],
        extraction_section_count: 1,
        page_count: 1,
        revision: null,
        updated_at: null,
      }),
    })
  );
  await page.getByRole("tab", { name: "構造化要素" }).click();
  const navigationSection = page.getByTestId("extraction-navigation");
  await expect(navigationSection).toContainText("第1章 総則");
  await expect(navigationSection).toContainText("章の要約テキスト");
  const assetSection = page.getByTestId("extraction-asset-summaries");
  await assetSection.getByText("図表の要約").click();
  await expect(assetSection).toContainText("承認の流れを示す図");
  const fieldsSection = page.getByTestId("extraction-fields");
  await fieldsSection.getByText("抽出項目").click();
  await expect(fieldsSection).toContainText("請求書番号");
  await expect(fieldsSection).toContainText("INV-1");

  // 項目抽出を上書きで有効にすると、項目の定義が 0 件の警告を出す。
  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  await panel.getByRole("button", { name: "処理設定を編集" }).click();
  const fieldGroup = panel
    .getByText("メタデータ/項目抽出", { exact: true })
    .locator("../..");
  await fieldGroup.getByText("上書き", { exact: true }).click();
  await fieldGroup.getByText("有効", { exact: true }).click();
  await expect(panel).toContainText(
    "全体の既定の項目の定義が 0 件で保存されているため、ナレッジベースで項目を定義していない文書では項目抽出は実行されません"
  );
  await expect(panel).not.toContainText("標準の項目（");
  await expectNoPageOverflow(page);
});

test(`項目抽出を有効にすると、標準の項目で抽出することを案内する`, async ({
  page,
}, testInfo) => {
  // #556: 全体の既定を保存していない環境は標準の項目で動くため、警告ではなく案内を出す。
  await mockWorkspace(page);
  await page.route("**/api/settings/extraction-fields", (route) =>
    route.fulfill({
      json: ok({
        fields: [
          { name: "文書の種類", description: "", value_type: "string" },
          { name: "文書タイトル", description: "", value_type: "string" },
          { name: "発行日・作成日", description: "", value_type: "date" },
          { name: "発行元・作成部署", description: "", value_type: "string" },
        ],
        uses_standard: true,
      }),
    })
  );

  await page.goto("/documents/doc-1");

  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  await panel.getByRole("button", { name: "処理設定を編集" }).click();
  const fieldGroup = panel.getByText("メタデータ/項目抽出", { exact: true }).locator("../..");
  await fieldGroup.getByText("上書き", { exact: true }).click();
  await fieldGroup.getByText("有効", { exact: true }).click();
  // 編集欄の行（要約の一覧にも同じ data-config-field の行があるため、最後の 1 つ）。
  const fieldRow = panel.locator('[data-config-field="field_extraction_enabled"]').last();
  await expect(fieldRow).toContainText(
    "標準の項目（文書の種類・文書タイトル・発行日・作成日・発行元・作成部署）を抽出します。"
  );
  await expect(panel).not.toContainText("0 件で保存されているため");
  await expect(panel).not.toContainText("未設定のため");
  if (process.env.RAG_E2E_SCREENSHOT_DIR) {
    await fieldRow.screenshot({
      path: `${process.env.RAG_E2E_SCREENSHOT_DIR}/recipe-field-standard-${testInfo.project.name}.png`,
    });
  }
  await expectNoPageOverflow(page);
});

test(`作成後に項目の定義が変わったレイヤーに作り直しの印と再処理の案内を出す`, async ({
  page,
}, testInfo) => {
  await mockWorkspace(page);
  // 後着 route が優先される。項目抽出だけが作り直しが必要（#550）。
  await page.route("**/api/documents/doc-1/chunk-sets", (route) =>
    route.fulfill({
      json: ok([
        {
          chunk_set_id: "chunk-set-recipe-1",
          extraction_recipe_id: "er-recipe-1-r1",
          extraction_status: "materialized",
          extraction_reason: null,
          status: "INDEXED",
          chunk_count: 2,
          vector_count: 2,
          is_serving: true,
          created_at: "2026-06-15T00:00:10Z",
          extraction_id: "ex-1",
          parser: "docling",
          preprocess: "office_to_pdf",
          knowledge_base_ids: ["kb-1"],
          serving_knowledge_base_ids: ["kb-1"],
          layer_statuses: {
            metadata: {
              layer_id: "md-1",
              requested: true,
              status: "materialized",
              reason: "項目抽出は保存済み抽出 artifact から実体化済みです。",
              rebuild_required: true,
              rebuild_inputs: ["field_schema_hash"],
            },
            graph: { layer_id: null, requested: false, status: "not_requested", reason: null },
            navigation: {
              layer_id: "nv-1",
              requested: true,
              status: "materialized",
              reason: null,
              rebuild_required: false,
              rebuild_inputs: [],
            },
          },
        },
      ]),
    })
  );

  await page.goto("/documents/doc-1");

  const layerChips = page.getByTestId("recipe-layer-statuses");
  const metadata = layerChips.getByTestId("recipe-layer-metadata");
  // 状態（構築済み）は変えず、別の印として「作り直しが必要」をアイコンと文言で出す。
  await expect(metadata).toContainText("構築済み");
  await expect(metadata).toContainText("作り直しが必要");
  await expect(metadata).toHaveAttribute("data-rebuild-required", "true");
  await expect(metadata.locator("svg")).toHaveCount(1);
  await expect(metadata).toHaveAttribute("title", /項目の定義が変わりました/);
  const navigation = layerChips.getByTestId("recipe-layer-navigation");
  await expect(navigation).not.toContainText("作り直しが必要");
  await expect(navigation).not.toHaveAttribute("data-rebuild-required", "true");

  // 自動では作り直さず、既存の「再処理」の導線を案内する（画面幅に関係なく読める文言）。
  await expect(page.getByTestId("recipe-layer-rebuild")).toContainText(
    "項目抽出は、作成後に項目の定義が変わりました。レシピの再処理で作り直すまで"
  );
  await expect(page.getByRole("button", { name: "再処理" }).first()).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({
    path: testInfo.outputPath("layer-rebuild.png"),
    fullPage: true,
  });
});

test("処理途中の文書は設定を編集できない", async ({ page }) => {
  await mockWorkspace(page, { documentStatus: "REVIEW" });
  await page.goto("/documents/doc-1");

  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  await panel.getByRole("button", { name: "処理設定を編集" }).click();

  await expect(panel).toContainText("処理途中または文書処理の実行中は設定を変更できません");
  await expect(
    panel.getByRole("group", { name: "文書解析" }).getByText("上書き")
  ).toBeDisabled();
  await expectNoPageOverflow(page);
});

test("確認待ちレシピの分割を一時設定でプレビューする", async ({ page }) => {
  const state = await mockWorkspace(page, { documentStatus: "REVIEW" });
  await page.goto("/documents/doc-1");

  await page.getByRole("tab", { name: "Chunk" }).click();
  const preview = page.getByRole("region", { name: "分割プレビュー" });
  await expect(preview).toContainText(
    "保存済みの抽出結果を一時設定で分割します。レシピ設定や工程状態は変更しません。"
  );
  await expect(page.getByText("chunk はまだ作成されていません。")).toBeVisible();
  await preview.getByRole("combobox", { name: "分割方式" }).click();
  await page.getByRole("option", { name: "構造認識" }).click();
  await preview.getByRole("spinbutton", { name: "chunk サイズ(文字)", exact: true }).fill("640");
  await preview.getByRole("button", { name: "プレビュー実行" }).click();

  await expect(preview).toContainText("件数");
  await expect(preview).toContainText("24 文字");
  await expect(page.getByText("経費申請は部門長の承認後、経理部が確認します。"))
    .toBeVisible();
  expect(state.previewPosts()).toBe(1);
  expect(state.previewPayload()).toMatchObject({
    chunk_size: 640,
  });
  await expectNoPageOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760, collapseSidebar: false },
  { name: "mobile", width: 375, height: 812, collapseSidebar: true },
]) {
  test(`分割プレビューの実行中はスピナーをボタンの 1 つだけにする (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapseSidebar) {
      await page.addInitScript(() => {
        window.localStorage.setItem(
          "production-ready-rag.ui",
          JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
        );
      });
    }
    await mockWorkspace(page, { documentStatus: "REVIEW" });
    let releasePreview = () => {};
    const previewGate = new Promise<void>((resolve) => {
      releasePreview = resolve;
    });
    await page.route("**/api/documents/doc-1/recipes/recipe-1/chunk-preview", async (route) => {
      await previewGate;
      await route.fallback();
    });
    await page.goto("/documents/doc-1");
    await page.getByRole("tab", { name: "Chunk" }).click();

    const preview = page.getByRole("region", { name: "分割プレビュー" });
    const run = preview.getByRole("button", { name: "プレビュー実行" });
    await run.click();
    const processing = page.getByTestId("chunk-preview-processing");
    const chunksLoading = page.getByTestId("document-chunks-loading");
    try {
      await expect(run).toHaveAttribute("aria-busy", "true");
      await expect(processing.getByRole("timer")).toBeVisible();
      await expect(chunksLoading).toBeVisible();
      // 同じ処理のスピナーと経過時間は 1 つずつ（messaging §3.7、#416）。chunk 一覧は Skeleton だけ。
      await expect(page.locator("svg.animate-spin:visible")).toHaveCount(1);
      await expect(run.locator("svg.animate-spin")).toHaveCount(1);
      await expect(page.getByRole("timer")).toHaveCount(1);
      await expectNoPageOverflow(page);
    } finally {
      releasePreview();
    }
    await expect(page.getByText("経費申請は部門長の承認後、経理部が確認します。")).toBeVisible();
    await expect(page.locator("svg.animate-spin:visible")).toHaveCount(0);
  });

  test(`分割プレビューの意味境界方式は推奨値を適用する (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapseSidebar) {
      await page.addInitScript(() => {
        window.localStorage.setItem(
          "production-ready-rag.ui",
          JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
        );
      });
    }
    const state = await mockWorkspace(page, { documentStatus: "REVIEW" });
    await page.goto("/documents/doc-1");
    await page.getByRole("tab", { name: "Chunk" }).click();

    const preview = page.getByRole("region", { name: "分割プレビュー" });
    const strategy = preview.getByRole("combobox", { name: "分割方式" });
    await strategy.click();
    await page.getByRole("option", { name: "見出し単位" }).click();

    let details = preview.locator("details").filter({
      hasText: "長大な単位の再分割(詳細設定)",
    });
    await expect(details).not.toHaveAttribute("open", "");
    await details.getByText("長大な単位の再分割(詳細設定)").click();
    await expect(details.getByLabel("見出し内の再分割上限(文字)")).toHaveValue("32000");
    await expect(details.getByLabel("再分割時の重複文字数")).toHaveValue("0");

    await strategy.click();
    await page.getByRole("option", { name: "ページ単位" }).click();
    details = preview.locator("details").filter({
      hasText: "長大な単位の再分割(詳細設定)",
    });
    await expect(details).not.toHaveAttribute("open", "");
    await details.getByText("長大な単位の再分割(詳細設定)").click();
    await expect(details.getByLabel("ページ内の再分割上限(文字)")).toHaveValue("32000");
    await expect(details.getByLabel("再分割時の重複文字数")).toHaveValue("0");

    await preview.getByRole("button", { name: "プレビュー実行" }).click();
    expect(state.previewPayload()).toMatchObject({
      chunking_strategy: "page_level",
      chunk_size: 32_000,
      chunk_overlap: 0,
    });
    await expectNoPageOverflow(page);
  });
}

test("分割プレビューで親子階層（small-to-big）の 5 項目を指定できる", async ({ page }) => {
  const state = await mockWorkspace(page, { documentStatus: "REVIEW" });
  await page.goto("/documents/doc-1");
  await page.getByRole("tab", { name: "Chunk" }).click();

  const preview = page.getByRole("region", { name: "分割プレビュー" });
  await preview.getByRole("combobox", { name: "分割方式" }).click();
  await page.getByRole("option", { name: "親子階層（small-to-big）" }).click();

  // 親子階層は chunk サイズ等を使わず、子と親の大きさの 5 項目だけを出す。
  await expect(preview.getByRole("spinbutton", { name: "chunk サイズ(文字)", exact: true })).toHaveCount(0);
  await expect(preview.getByRole("spinbutton", { name: "子チャンク目標文字数", exact: true })).toHaveValue("1000");
  await expect(preview.getByRole("spinbutton", { name: "親チャンク最大 child 数", exact: true })).toHaveValue("12");
  await preview.getByRole("spinbutton", { name: "子チャンク目標文字数", exact: true }).fill("600");
  await preview.getByRole("button", { name: "プレビュー実行" }).click();

  await expect(preview).toContainText("件数");
  expect(state.previewPayload()).toMatchObject({
    chunking_strategy: "small_to_big",
    chunk_child_target_chars: 600,
    chunk_table_child_target_chars: 3000,
    chunk_parent_target_chars: 6000,
    chunk_parent_max_pages: 3,
    chunk_parent_max_children: 12,
  });
  await expectNoPageOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`Docling 以外の解析結果でも親子階層（small-to-big）のプレビューに縮退の表示を出さない (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockWorkspace(page, {
      documentStatus: "REVIEW",
      previewChunkMetadata: {
        chunk_strategy: "small_to_big",
        chunk_group_kind: "small_to_big_parent",
        parent_chunk_id: "parent-1",
        parent_text: "経費申請は部門長の承認後、経理部が確認します。",
        source_parser: "markdown",
      },
    });
    await page.goto("/documents/doc-1");
    await page.getByRole("tab", { name: "Chunk" }).click();

    const preview = page.getByRole("region", { name: "分割プレビュー" });
    await preview.getByRole("combobox", { name: "分割方式" }).click();
    await page.getByRole("option", { name: "親子階層（small-to-big）" }).click();
    await preview.getByRole("button", { name: "プレビュー実行" }).click();

    // どの解析の結果でも親子で分割する(#3377)。構造認識へ切り替えた案内は出さない。
    await expect(page.getByText("経費申請は部門長の承認後、経理部が確認します。")).toBeVisible();
    await expect(page.getByText("構造認識で分割しました")).toHaveCount(0);
    await expect(page.getByText("親子階層で分割されていない Chunk です")).toHaveCount(0);
    await expect(page.getByRole("status").filter({ hasText: "Docling" })).toHaveCount(0);
    await expectNoPageOverflow(page);
  });

  test(`以前の版で構造認識に切り替えて作った chunk には、作り直しを案内する (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockWorkspace(page, {
      documentStatus: "INDEXED",
      storedChunkMetadata: {
        chunk_strategy: "structure_aware",
        chunk_strategy_requested: "small_to_big",
        chunk_strategy_fallback_reason: "layout_missing",
      },
    });
    await page.goto("/documents/doc-1");
    await page.getByRole("tab", { name: "Chunk" }).click();

    const notice = page.getByRole("status").filter({ hasText: "親子階層で分割されていない Chunk です" });
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("Chunk を作り直すと、解析の結果の構造から親子階層で分割します。");
    // 解析エンジンの名前を出さない(#3377)。
    await expect(notice).not.toContainText("Docling");
    await expect(page.getByText("保存済みの合成の本文です。")).toBeVisible();
    await expectNoPageOverflow(page);
  });
}

test("親子階層（small-to-big）で分割した chunk には縮退の表示を出さない", async ({ page }) => {
  await mockWorkspace(page, { documentStatus: "REVIEW" });
  await page.goto("/documents/doc-1");
  await page.getByRole("tab", { name: "Chunk" }).click();
  const preview = page.getByRole("region", { name: "分割プレビュー" });
  await preview.getByRole("button", { name: "プレビュー実行" }).click();

  await expect(page.getByText("経費申請は部門長の承認後、経理部が確認します。")).toBeVisible();
  await expect(page.getByText("親子階層で分割されていない Chunk です")).toHaveCount(0);
});

test("分割プレビュー失敗を画面内に表示する", async ({ page }) => {
  await mockWorkspace(page, { documentStatus: "REVIEW", previewFails: true });
  await page.goto("/documents/doc-1");

  await page.getByRole("tab", { name: "Chunk" }).click();
  const preview = page.getByRole("region", { name: "分割プレビュー" });
  await preview.getByRole("button", { name: "プレビュー実行" }).click();

  await expect(preview).toContainText("分割サービスに接続できませんでした。");
  await expectNoPageOverflow(page);
});

test("未保存の抽出修正がある間は分割プレビューを無効にする", async ({ page }) => {
  const state = await mockWorkspace(page, { documentStatus: "REVIEW" });
  await page.goto("/documents/doc-1");

  await page.getByRole("tab", { name: "構造化要素" }).click();
  await page.getByRole("button", { name: "構造化要素を修正" }).click();
  await page.locator('textarea[id^="review-edit-"]').first().fill("修正中の経費申請規程");
  await page.getByRole("tab", { name: "Chunk" }).click();

  const preview = page.getByRole("region", { name: "分割プレビュー" });
  await expect(preview).toContainText(
    "未保存の抽出修正があります。修正を保存してからプレビューしてください。"
  );
  await expect(preview.getByRole("button", { name: "プレビュー実行" })).toBeDisabled();
  expect(state.previewPosts()).toBe(0);
  await expectNoPageOverflow(page);
});
