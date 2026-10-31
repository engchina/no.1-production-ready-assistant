import { expect, type Page, type Route, test } from "./fixtures/test";

import {
  FORM_GRID_PROBE_WIDTHS_REM,
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  findNarrowFieldTexts,
  measureHorizontalOverflow,
  setFormGridMaxWidth,
} from "../../../platform/packages/ui/e2e/layout-checks";
import { apiEnvelope, mockLocalAuth, selectSearchAnswerProfile } from "./_helpers";

/**
 * RAG のフォームの欄の幅と placeholder（#1891 段 2）。
 *
 * 設定・文書・検索・ナレッジベース・検索・回答プロファイル・アップロードのフォームを開き、次を確かめる。
 * - ページが横にあふれない（`measureHorizontalOverflow`）
 * - placeholder が欄に収まる（`findClippedPlaceholders`）
 * - desktop（1280px・1920px）では、欄の幅が段（xs / sm / md / lg）か置き場所の幅いっぱい（full）で、`FormGrid` の
 *   セルからはみ出さない（`findFieldsOffWidthScale`。段の幅は sm 以上だけで効くので 375px では見ない）
 * - 欄の補足・エラーの文が min(20rem, 置き場所の幅) より狭く折り返さない（`findNarrowFieldTexts`。#1891 段 5）
 * - 1280px で `FormGrid` の置き場所を 42〜58rem に狭めても、上のすべてが成り立つ（2 列の境 57.5rem の前後。#1891 段 5）
 * ライト・ダークの両方で開く。desktop の project は 1280px と 1920px、mobile の project は 375px。
 * `RAG_E2E_SCREENSHOT_DIR` を渡すと、代表の画面の画像を残す（PR の説明用）。
 */

const SCREENSHOT_DIR = process.env.RAG_E2E_SCREENSHOT_DIR;
const DESKTOP_WIDTHS = [
  { width: 1280, height: 900 },
  { width: 1920, height: 1080 },
] as const;

type Theme = "light" | "dark";

interface FormScreen {
  /** 画面の名前（テスト名・画像の名前）。 */
  name: string;
  path: string;
  /** API の mock（goto の前に 1 回だけ呼ぶ）。 */
  mock: (page: Page) => Promise<void>;
  /** フォームを開くまでの操作（goto の後）。 */
  open: (page: Page) => Promise<void>;
  /**
   * 欄の幅の段を調べる範囲（CSS セレクタ。既定は main）。ほかの段（一覧・別の担当の画面）の欄を含む画面は、
   * この段で変えたフォームだけに絞る。
   */
  widthRoots?: string[];
  /** 画像を残す範囲（既定はページ全体）。 */
  screenshotTarget?: string;
}

// ---- 検索方法（回答の検索と生成・検索プロンプト） ----

async function mockRetrieval(page: Page) {
  const cards: [string, unknown][] = [
    [
      "**/api/settings/answering",
      {
        query_strategy: "rag_fusion",
        answer_flow: "standard_rag",
        neighbor_child_count: 3,
        rerank_enabled: true,
        screen_linking_enabled: false,
        request_coverage_retrieval_enabled: false,
        auto_field_filter_enabled: false,
        graph_expansion_enabled: false,
        entity_expansion_enabled: true, entity_expansion_max_chunks: 6,
        section_expansion_enabled: true, section_expansion_max_levels: 4,
        section_expansion_input_token_percent: 50,
        config_source: "runtime",
      },
    ],
  ];
  for (const [url, data] of cards) {
    await page.route(url, (route) => route.fulfill({ json: apiEnvelope(data) }));
  }
}

// ---- 文書分割 ----

function chunkingSettings(strategy: string) {
  const names = [
    "structure_aware",
    "recursive_character",
    "small_to_big",
    "markdown_heading",
    "page_level",
    "fixed_size",
    "fixed_delimiter",
  ];
  return {
    strategy,
    chunk_size: 800,
    overlap: 120,
    min_chars: 120,
    delimiter: "\\n\\n",
    context_header_enabled: true,
    chunk_child_target_chars: 1000,
    chunk_table_child_target_chars: 3000,
    chunk_parent_target_chars: 6000,
    chunk_parent_max_pages: 3,
    chunk_parent_max_children: 12,
    strategies: names.map((name) => ({
      name,
      origin: name,
      recommended_for: ["pdf"],
      selected: name === strategy,
    })),
    config_source: "runtime",
  };
}

function mockChunking(strategy: string) {
  return async (page: Page) => {
    await page.route("**/api/settings/chunking", (route) =>
      route.fulfill({ json: apiEnvelope(chunkingSettings(strategy)) })
    );
  };
}

// ---- 文書解析（外部の解析の接続・章節の抽出規則・抽出する項目） ----

function parserAdapter(backend: string, selected: boolean) {
  const external = backend === "mineru" || backend === "dots_ocr";
  return {
    backend,
    package_name: external ? "external_api" : backend,
    import_name: external ? "external_api" : backend,
    distribution_name: null,
    install_package: external ? `外部 ${backend} API` : backend,
    enabled: selected,
    selected,
    installed: selected,
    status: selected ? "active" : "disabled",
    version: selected ? "1.2.3" : null,
    warning_code: null,
  };
}

async function mockParserAdapters(page: Page) {
  await page.route("**/api/settings/parser-adapters", (route) =>
    route.fulfill({
      json: apiEnvelope({
        adapter_backend: "docling",
        effective_order: ["docling"],
        config_source: "runtime",
        vision_enabled: false,
        field_extraction_enabled: true,
        navigation_summary_enabled: true,
        adapters: [
          parserAdapter("docling", true),
          parserAdapter("unstructured", false),
          parserAdapter("mineru", false),
          parserAdapter("dots_ocr", false),
        ],
        source_routes: [],
        connections: [
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
        ],
        service_backends: [],
        backend_source_kind_matrix: {
          evidence_source: "runtime_routes",
          required_source_kinds: [],
          covered_source_kinds: [],
          missing_source_kinds: [],
          backend_source_kinds: {},
          route_evidence: [],
        },
      }),
    })
  );
  await page.route("**/api/settings/extraction-fields", (route) =>
    route.fulfill({
      json: apiEnvelope({
        fields: [
          { name: "契約番号", description: "契約書の右上の番号", value_type: "string" },
          { name: "契約日", description: "", value_type: "date" },
        ],
        uses_standard: false,
      }),
    })
  );
  await page.route("**/api/settings/section-rules", (route) =>
    route.fulfill({
      json: apiEnvelope({
        mode: "custom",
        rules: [
          { name: "章", pattern: "^第[0-9]+章", level: 1, enabled: true },
          { name: "条", pattern: "^第[0-9]+条", level: 2, enabled: true },
        ],
        presets: { legal: [{ name: "章", pattern: "^第[0-9]+章", level: 2, enabled: true }] },
      }),
    })
  );
  await page.route("**/api/documents?**", (route) =>
    route.fulfill({
      json: apiEnvelope({
        items: [{ id: "doc-1", file_name: "経費規程.pdf" }],
        total: 1,
        limit: 50,
        offset: 0,
        has_next: false,
      }),
    })
  );
}

async function openParserAdapters(page: Page) {
  await expect(page.getByRole("textbox", { name: "Endpoint" }).first()).toBeVisible();
  await page.locator("summary", { hasText: "抽出する項目の定義" }).click();
  await expect(page.getByRole("textbox", { name: /項目名/ }).first()).toBeVisible();
  await page.locator("#post-parse-section-rules").scrollIntoViewIfNeeded();
  await expect(page.getByRole("heading", { name: "4. 章節の抽出規則", exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "正規表現" }).first()).toBeVisible();
}

// ---- HuggingFace ----

async function mockHuggingFace(page: Page) {
  await page.route("**/api/settings/huggingface", (route) =>
    route.fulfill({ json: apiEnvelope({ endpoint: "", token_configured: true, config_source: "runtime" }) })
  );
}

// ---- 回答プロンプト ----

async function mockAnswerPrompts(page: Page) {
  await page.route("**/api/settings/answer-prompts", (route) =>
    route.fulfill({
      json: apiEnvelope({
        prompts: [
          {
            key: "vlm_answer",
            content: "次の質問に答えてください。\n{{question}}\n{{answer_context}}",
            default_content: "既定のテンプレート {{question}} {{answer_context}}",
            customized: true,
            required_placeholders: ["question", "answer_context"],
            updated_at: "2026-09-26T01:00:00Z",
          },
        ],
        stages: [{ id: "routing", phase: "question", prompts: [{ id: "system", content: "ルーティングの system" }] }],
      }),
    })
  );
}

// ---- RAG 検索（詳細条件） ----

const PROFILES = [
  {
    id: "bv-1",
    name: "契約ビュー",
    description: null,
    status: "ACTIVE",
    knowledge_base_count: 1,
    created_at: "2026-06-19T00:00:00Z",
    updated_at: "2026-06-19T00:00:00Z",
    archived_at: null,
  },
];

async function mockSearch(page: Page) {
  await page.route("**/api/search-answer-profiles**", (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/approved-faq/suggest")) {
      return route.fulfill({ json: apiEnvelope({ suggestions: [] }) });
    }
    return route.fulfill({
      json: apiEnvelope({ items: PROFILES, total: 1, limit: 50, offset: 0, has_next: false }),
    });
  });
  await page.route("**/api/search/answers**", (route) =>
    route.fulfill({ json: apiEnvelope({ items: [], total: 0, limit: 10, offset: 0, has_next: false }) })
  );
  await page.route("**/api/settings/answer-records", (route) =>
    route.fulfill({ json: apiEnvelope({ retention_days: 30 }) })
  );
  await page.route("**/api/feedback/current**", (route) => route.fulfill({ json: apiEnvelope([]) }));
  await page.route("**/api/search/extraction-fields**", (route) =>
    route.fulfill({
      json: apiEnvelope({
        fields: [
          { name: "契約番号", description: "", value_type: "string" },
          { name: "金額", description: "契約金額", value_type: "number" },
          { name: "契約日", description: "", value_type: "date" },
        ],
      }),
    })
  );
}

/** 開閉のボタンを開く（作業状態から開いたまま戻っていれば押さない）。 */
async function expand(page: Page, name: string | RegExp) {
  const toggle = page.getByRole("button", { name });
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
}

async function openSearchConditions(page: Page) {
  await selectSearchAnswerProfile(page, /契約ビュー/);
  await expand(page, /詳細条件/);
  await expand(page, "文書の分類で絞り込む");
  await expand(page, "抽出項目の値で絞り込む");
  if ((await page.getByRole("combobox", { name: "項目" }).count()) > 0) return;
  for (const field of [/契約番号/, /金額/, /契約日/]) {
    await page.getByRole("button", { name: "条件を追加" }).click();
    await page.getByRole("combobox", { name: "項目" }).last().click();
    await page.getByRole("option", { name: field }).click();
  }
  await expect(page.getByRole("combobox", { name: "大分類" })).toBeVisible();
}

// ---- ナレッジベース（編集・抽出する項目） ----

const KNOWLEDGE_BASE = {
  id: "kb-1",
  name: "契約書",
  description: "契約書の文書",
  status: "ACTIVE",
  default_search_mode: "hybrid",
  document_count: 0,
  indexed_document_count: 0,
  error_document_count: 0,
  searchable_chunk_count: 0,
  created_at: "2026-06-15T00:00:00Z",
  updated_at: "2026-06-15T00:00:00Z",
  archived_at: null,
  retrieval_config: {},
  adapter_config: { version: 1, ingestion: {}, query: {} },
};

async function mockKnowledgeBase(page: Page) {
  await page.route("**/api/documents**", (route) =>
    route.fulfill({ json: apiEnvelope({ items: [], total: 0, limit: 10, offset: 0, has_next: false }) })
  );
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: apiEnvelope({ items: [KNOWLEDGE_BASE], total: 1, limit: 20, offset: 0, has_next: false }),
    })
  );
  await page.route("**/api/knowledge-bases/kb-1", (route) => route.fulfill({ json: apiEnvelope(KNOWLEDGE_BASE) }));
  await page.route("**/api/knowledge-bases/kb-1/extraction-fields", (route) =>
    route.fulfill({
      json: apiEnvelope({
        inherits_default: false,
        fields: [{ name: "契約番号", description: "契約書の右上の番号", value_type: "string" }],
      }),
    })
  );
}

// ---- 文書の詳細（処理レシピ・分割プレビュー・分類と有効期間） ----

const PROCESSING_NULLS = {
  preprocess_profile: null,
  parser_adapter_backend: null,
  parser_docling_enabled: null,
  parser_unstructured_enabled: null,
  parser_mineru_enabled: null,
  parser_dots_ocr_enabled: null,
  chunking_strategy: null,
  chunk_size: null,
  chunk_overlap: null,
  chunk_min_chars: null,
  chunk_context_header_enabled: null,
  graph_profile: null,
  field_extraction_enabled: null,
  vision_enabled: null,
  navigation_summary_enabled: null,
  auto_parse_after_preprocess_enabled: null,
  auto_chunk_after_extract_enabled: null,
  auto_index_after_chunk_enabled: null,
};

function mockDocument(status: "REVIEW" | "INDEXED") {
  return async (page: Page) => {
    await mockDocumentWith(page, status);
  };
}

async function mockDocumentWith(page: Page, status: "REVIEW" | "INDEXED") {
  const processing = { ...PROCESSING_NULLS, preprocess_profile: "excel_to_jsonl" };
  const effective = {
    ...PROCESSING_NULLS,
    preprocess_profile: "excel_to_jsonl",
    parser_adapter_backend: "docling",
    chunking_strategy: "page_level",
    graph_profile: "off",
    entity_index_enabled: true,
    entity_name_columns: [],
    entity_attribute_columns: [],
    field_extraction_enabled: false,
    vision_enabled: false,
    navigation_summary_enabled: false,
    section_rules_mode: "parser",
    auto_parse_after_preprocess_enabled: true,
    auto_chunk_after_extract_enabled: true,
    auto_index_after_chunk_enabled: true,
  };
  const recipe = (slotNo: number) => ({
    recipe_id: `recipe-${slotNo}`,
    document_id: "doc-1",
    slot_no: slotNo,
    status,
    failed_phase: null,
    processing_config: processing,
    effective_processing_config: effective,
    preprocess_artifact: null,
    active_extraction_recipe_id: `er-recipe-${slotNo}-r1`,
    active_chunk_set_id: status === "INDEXED" ? `chunk-set-recipe-${slotNo}` : null,
    chunk_count: status === "INDEXED" ? 2 : 0,
    vector_count: status === "INDEXED" ? 2 : 0,
    config_revision: 1,
    materialized_revision: status === "INDEXED" ? 1 : null,
    searchable: status === "INDEXED",
    needs_reprocessing: false,
    error_message: null,
    steps: ["PREPROCESS", "EXTRACT", "CHUNK", "INDEX"].map((phase, index) => ({
      phase,
      status: index < (status === "INDEXED" ? 4 : 2) ? "SUCCEEDED" : "PENDING",
      started_at: null,
      finished_at: null,
      error_message: null,
    })),
    created_at: "2026-06-15T00:00:00Z",
    updated_at: "2026-06-15T00:00:20Z",
    started_at: null,
    finished_at: null,
  });
  const ok = (data: unknown) => ({ json: apiEnvelope(data) });
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill(ok({ items: [{ id: "kb-1", name: "社内規程" }], total: 1, limit: 100, offset: 0, has_next: false }))
  );
  await page.route("**/api/document-categories", (route) =>
    route.fulfill(
      ok([{ id: "c1", name: "業務A", level: 1, parent_id: null, document_count: 0, children: [] }])
    )
  );
  await page.route("**/api/documents/doc-1/recipes", (route) => route.fulfill(ok([recipe(1), recipe(2)])));
  await page.route("**/api/documents/doc-1/recipes/recipe-*", (route) => route.fulfill(ok(recipe(1))));
  await page.route("**/api/documents/doc-1/recipes/recipe-*/ingestion-jobs**", (route) => route.fulfill(ok([])));
  await page.route("**/api/documents/doc-1/recipes/recipe-*/chunks", (route) => route.fulfill(ok([])));
  await page.route("**/api/documents/doc-1/recipes/recipe-*/extraction-export**", (route) =>
    route.fulfill(
      ok({
        document_id: "doc-1",
        file_name: "policy.xlsx",
        format: "json",
        content_type: "application/json; charset=utf-8",
        content: "",
        payload: { raw_text: "経費申請の規程", elements: [] },
        chunks: [],
        parser_backend: "docling",
        parser_profile: "docling",
        page_count: 0,
        element_count: 0,
        table_count: 0,
        asset_count: 0,
      })
    )
  );
  await page.route("**/api/documents/doc-1/ingestion-jobs**", (route) => route.fulfill(ok([])));
  await page.route("**/api/documents/doc-1/chunk-sets", (route) => route.fulfill(ok([])));
  await page.route("**/api/documents/doc-1/ingestion-segments", (route) => route.fulfill(ok([])));
  await page.route("**/api/documents/doc-1/knowledge-bases", (route) =>
    route.fulfill(ok([{ id: "kb-1", name: "社内規程" }]))
  );
  await page.route("**/api/documents/doc-1/content**", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/documents/doc-1/recipes/recipe-*/content**", (route) =>
    route.fulfill({ status: 204, body: "" })
  );
  await page.route("**/api/documents/doc-1", (route) =>
    route.fulfill(
      ok({
        id: "doc-1",
        file_name: "policy.xlsx",
        status,
        category_name: null,
        content_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        file_size_bytes: 1024,
        content_sha256: "abc",
        duplicate_of_document_id: null,
        uploaded_at: "2026-06-15T00:00:00Z",
        indexed_at: null,
        knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
        object_storage_path: "indexed/doc-1.xlsx",
        extraction: { raw_text: "経費申請の規程", elements: [] },
        error_message: null,
        classification: null,
      })
    )
  );
}

async function openDocumentRecipe(page: Page) {
  const panel = page.getByRole("region", { name: "処理レシピ", exact: true });
  await panel.getByRole("button", { name: "処理設定を編集" }).click();
  const excel = panel.getByTestId("document-excel-options");
  await excel.getByText("上書き", { exact: true }).click();
  await expect(excel.getByRole("combobox", { name: "読み方" })).toBeVisible();
  const columns = panel.getByTestId("document-entity-columns");
  await columns.getByText("上書き", { exact: true }).click();
  await expect(columns.getByRole("textbox", { name: "名前の列" })).toBeVisible();
  await panel.getByRole("button", { name: "検索結果を横並びで比較" }).click();
  await expect(panel.getByRole("textbox", { name: /比較する質問/ })).toBeVisible();
}

async function openDocumentChunkPreview(page: Page) {
  await page.getByRole("tab", { name: "Chunk" }).click();
  const preview = page.getByRole("region", { name: "分割プレビュー" });
  await expect(preview.getByRole("combobox", { name: "分割方式" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "文書の分類と有効期間" })).toBeVisible();
}

// ---- 業務ガイドの編集・下書きで試す ----

function supportGuideContent() {
  return {
    schema_version: 1,
    title: "パスワードの再設定",
    description: "",
    goal: { expected_result: "手順を終えられる", intent_examples: ["パスワードを忘れた"], match_terms: [] },
    applicability: { business_domains: [], object_types: [], versions: [], effective_from: null, effective_to: null },
    conditions: [
      {
        id: "account",
        label: "アカウントの種類",
        type: "enum",
        allowed_values: ["社員", "派遣"],
        required: true,
        source: "user",
        unknown_handling: "ask",
        question: "社員ですか、派遣ですか？",
        value_aliases: {},
      },
    ],
    steps: [
      {
        id: "step1",
        title: "本人を確かめる",
        purpose: "",
        depends_on: [],
        retrieval_hints: [],
        evidence_requirements: [],
        allowed_tools: ["rag_search"],
        done_when: "",
      },
    ],
    branches: [
      { id: "branch1", when: { condition_id: "account", operator: "equals", values: ["社員"] }, goto_step: "step1", note: "" },
    ],
    references: [{ document_id: "doc-1", title: "運用手順書", section_path: [], version: "" }],
    completion: [{ id: "done1", description: "再設定できた", check_method: "" }],
    impact: { scope: "individual", approval_required: false, approval_note: "", steps: [] },
    handoff: { conditions: [], contact: "" },
  };
}

async function mockSupportGuides(page: Page) {
  const profile = { ...PROFILES[0], id: "bv-1", name: "受注サポート" };
  const draft = supportGuideContent();
  const detail = {
    guide_id: "guide-1",
    search_answer_profile_id: "bv-1",
    status: "active",
    title: draft.title,
    draft_revision: 2,
    published_revision: 1,
    has_unpublished_changes: true,
    updated_at: "2026-10-07T01:00:00Z",
    updated_by: "管理者",
    draft,
    published: draft,
    revisions: [
      {
        revision: 1,
        title: draft.title,
        content_sha256: "sha-1",
        published_at: "2026-10-01T02:00:00Z",
        published_by: "管理者",
        rollback_from: null,
      },
    ],
    issues: [],
  };
  const { draft: _draft, published: _published, revisions: _revisions, issues: _issues, ...summary } = detail;
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({ json: apiEnvelope({ items: [], total: 0, limit: 50, offset: 0, has_next: false }) })
  );
  await page.route("**/api/search-answer-profiles**", (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const base = "/api/search-answer-profiles/bv-1/support-guides";
    if (path === base) return route.fulfill({ json: apiEnvelope([summary]) });
    if (path === `${base}/guide-1`) return route.fulfill({ json: apiEnvelope(detail) });
    if (path.endsWith("/approved-faq")) {
      return route.fulfill({ json: apiEnvelope({ search_answer_profile_id: "bv-1", enabled: true, records: [] }) });
    }
    if (path.endsWith("/bv-1")) {
      return route.fulfill({
        json: apiEnvelope({
          ...profile,
          config: { version: 1, knowledge_base_ids: ["kb-1"], query: { guardrail_policy: null } },
          knowledge_bases: [{ id: "kb-1", name: "受注マニュアル" }],
        }),
      });
    }
    return route.fulfill({
      json: apiEnvelope({ items: [profile], total: 1, limit: 50, offset: 0, has_next: false }),
    });
  });
}

async function openSupportGuideEditor(page: Page) {
  await page.getByRole("tab", { name: "業務ガイド" }).click();
  await page.getByRole("button", { name: "パスワードの再設定 を編集" }).click();
  const editor = page.getByTestId("support-guide-editor");
  await expect(editor.getByRole("textbox", { name: /^タイトル/ })).toBeVisible();
  await expect(editor.getByTestId("support-guide-try-conditions")).toBeVisible();
}

// ---- 文書アップロード ----

async function mockUpload(page: Page) {
  await page.route("**/api/settings/upload-storage", (route) =>
    route.fulfill({
      json: apiEnvelope({
        local_storage_dir: "/var/rag-uploads",
        readiness: "ready",
        source: "runtime",
        max_upload_bytes: 200 * 1024 * 1024,
      }),
    })
  );
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: apiEnvelope({
        items: [{ ...KNOWLEDGE_BASE, name: "社内規程" }],
        total: 1,
        limit: 50,
        offset: 0,
        has_next: false,
      }),
    })
  );
  await page.route("**/api/documents/ingestion-jobs**", (route) =>
    route.fulfill({ json: apiEnvelope({ items: [], total: 0, limit: 5, offset: 0, has_next: false }) })
  );
}

const SCREENS: FormScreen[] = [
  {
    name: "settings-retrieval",
    path: "/settings/retrieval",
    mock: mockRetrieval,
    open: async (page) => {
      await expect(page.getByRole("combobox", { name: "質問の拡張", exact: true })).toBeVisible();
      await expect(page.getByRole("combobox", { name: "広げる段の上限", exact: true })).toBeVisible();
      await expect(page.getByRole("combobox", { name: "根拠の入力の上限（%）", exact: true })).toBeVisible();
      await expect(page.getByRole("combobox", { name: "1 回の検索で加える根拠の上限", exact: true })).toBeVisible();
      await expect(page.getByRole("textbox", { name: /保存・表示しない語/ })).toHaveCount(0);
    },
  },
  {
    name: "settings-chunking-small-to-big",
    path: "/settings/chunking",
    mock: mockChunking("small_to_big"),
    open: async (page) => {
      await expect(page.getByRole("spinbutton", { name: /子チャンク目標文字数/ }).first()).toBeVisible();
    },
  },
  {
    name: "settings-chunking-heading",
    path: "/settings/chunking",
    mock: mockChunking("markdown_heading"),
    open: async (page) => {
      await page.locator("summary", { hasText: "長大な単位の再分割" }).click();
      await expect(page.getByRole("spinbutton", { name: /再分割時の重複文字数/ })).toBeVisible();
    },
  },
  {
    name: "settings-chunking-delimiter",
    path: "/settings/chunking",
    mock: mockChunking("fixed_delimiter"),
    open: async (page) => {
      await expect(page.getByRole("textbox", { name: /固定分割符文字列/ })).toBeVisible();
    },
  },
  {
    name: "settings-parser-adapters",
    path: "/settings/parser-adapters",
    mock: mockParserAdapters,
    open: openParserAdapters,
    screenshotTarget: '[data-testid="section-rules-editor"]',
  },
  {
    name: "settings-huggingface",
    path: "/settings/huggingface",
    mock: mockHuggingFace,
    open: async (page) => {
      await expect(page.getByRole("textbox", { name: /ミラー/ }).or(page.locator("#hf-endpoint"))).toBeVisible();
    },
  },
  {
    name: "settings-prompts",
    path: "/settings/prompts",
    mock: mockAnswerPrompts,
    open: async (page) => {
      await expect(page.locator("#answer-prompt-vlm_answer")).toBeVisible();
    },
  },
  {
    name: "search-conditions",
    path: "/search",
    mock: mockSearch,
    open: openSearchConditions,
    screenshotTarget: "#search-advanced-conditions",
  },
  {
    name: "knowledge-base-form",
    path: "/knowledge-bases/kb-1",
    mock: mockKnowledgeBase,
    open: async (page) => {
      await expect(page.getByTestId("knowledge-base-form")).toBeVisible();
      await expect(page.getByTestId("knowledge-base-extraction-fields-editor")).toBeVisible();
    },
    widthRoots: ['[data-testid="knowledge-base-form"]', '[data-testid="knowledge-base-extraction-fields"]'],
  },
  {
    name: "document-recipe",
    path: "/documents/doc-1",
    mock: mockDocument("INDEXED"),
    open: openDocumentRecipe,
    widthRoots: ['section[aria-label="処理レシピ"]'],
    screenshotTarget: '[data-testid="document-excel-options"]',
  },
  {
    name: "document-chunk-preview",
    path: "/documents/doc-1",
    mock: mockDocument("REVIEW"),
    open: openDocumentChunkPreview,
  },
  {
    name: "support-guide-editor",
    path: "/search-answer-profiles?id=bv-1",
    mock: mockSupportGuides,
    open: openSupportGuideEditor,
    widthRoots: ['[data-testid="support-guide-editor"]'],
    screenshotTarget: '[data-testid="support-guide-editor"]',
  },
  {
    name: "upload",
    path: "/upload",
    mock: mockUpload,
    open: async (page) => {
      await expect(page.locator("#upload-knowledge-base-label")).toBeVisible();
      await expect(page.locator("#upload-knowledge-base-input")).toBeVisible();
    },
  },
];

/**
 * 外観の設定（localStorage）でテーマを決める。`emulateMedia` では切り替わらない。画面が離れるときに保存する
 * 作業状態で上書きされないよう、読み込みの前（init script）に入れる（後から足した script が後に動く）。
 */
async function useTheme(page: Page, theme: Theme) {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-rag.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

async function expectLayout(page: Page, screen: FormScreen, label: string, checkWidthScale: boolean) {
  await expect
    .poll(() => measureHorizontalOverflow(page), { message: `${label}: ページの横あふれ`, timeout: 3000 })
    .toBeLessThanOrEqual(1);
  expect(await findClippedPlaceholders(page), `${label}: 切れた placeholder`).toEqual([]);
  for (const root of screen.widthRoots ?? ["main"]) {
    if ((await page.locator(root).count()) === 0) continue;
    expect(await findNarrowFieldTexts(page, { root }), `${label}: 欄の幅で狭く折り返す補足（${root}）`).toEqual([]);
  }
  if (!checkWidthScale) return;
  // 入力欄とその操作の行（共有の FieldActionRow）の欄は、共通の検査が段の対象から外す。
  for (const root of screen.widthRoots ?? ["main"]) {
    if ((await page.locator(root).count()) === 0) continue;
    expect(await findFieldsOffWidthScale(page, { root }), `${label}: 段にない欄の幅（${root}）`).toEqual([]);
  }
}

async function screenshot(page: Page, screen: FormScreen, suffix: string) {
  if (!SCREENSHOT_DIR) return;
  const path = `${SCREENSHOT_DIR}/${screen.name}-${suffix}.png`;
  // 画像は PR の説明用（検査ではない）ので、撮れなくてもテストは落とさない。
  try {
    // 対象の先頭を画面に入れて、画面（viewport）を撮る（要素だけを撮ると main のスクロールと固定の見出しが重なる）。
    if (screen.screenshotTarget) {
      await page.locator(screen.screenshotTarget).first().evaluate((element) => element.scrollIntoView({ block: "start" }));
    }
    await page.screenshot({ path, animations: "disabled", timeout: 5000 });
  } catch (error) {
    console.warn(`画像を撮れませんでした（${screen.name}-${suffix}）: ${String(error)}`);
  }
}

test.describe("フォームの欄の幅と placeholder（#1891）", { tag: "@responsive" }, () => {
  test.beforeEach(async ({ page }) => {
    await mockLocalAuth(page);
  });

  for (const screen of SCREENS) {
    test(`${screen.name} の欄が段の幅で、placeholder が収まり、横にあふれない`, async ({ page }, testInfo) => {
      const mobile = testInfo.project.name === "mobile";
      await screen.mock(page);
      for (const theme of ["light", "dark"] as const) {
        await useTheme(page, theme);
        await page.goto(screen.path);
        await expect
          .poll(() => page.evaluate(() => document.documentElement.classList.contains("dark")))
          .toBe(theme === "dark");
        if (mobile) {
          await screen.open(page);
          await expectLayout(page, screen, `375px ${theme}`, false);
          await screenshot(page, screen, `375-${theme}`);
          continue;
        }
        for (const size of DESKTOP_WIDTHS) {
          await page.setViewportSize(size);
          if (size === DESKTOP_WIDTHS[0]) await screen.open(page);
          await expectLayout(page, screen, `${size.width}px ${theme}`, true);
          if (size !== DESKTOP_WIDTHS[0] || theme !== "light") continue;
          // FormGrid の置き場所が 42〜58rem（2 列の境 57.5rem の前後）でも、欄が段の幅のままでセルからはみ出さない。
          for (const rem of FORM_GRID_PROBE_WIDTHS_REM) {
            await setFormGridMaxWidth(page, rem);
            await expectLayout(page, screen, `${size.width}px ${theme} FormGrid ${rem}rem`, true);
          }
          await setFormGridMaxWidth(page, null);
        }
        await screenshot(page, screen, `1920-${theme}`);
      }
    });
  }
});
