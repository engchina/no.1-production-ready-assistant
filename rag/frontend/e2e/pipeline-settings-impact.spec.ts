import { expect, test, type Page } from "./fixtures/test";
import { expectNoPageOverflow, mockLocalAuth } from "./_helpers";

/**
 * 全体の既定（ファイル準備・文書解析・文書分割）を変えても、取込済みの文書は再処理するまで変わらない。
 * 各設定画面の説明でそれを案内する（#1001）。
 */
test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

const PAGES = [
  {
    path: "/settings/preprocess",
    text: "取込済みの文書は、文書の詳細で再処理するまで今の結果のまま検索されます。",
    mock: mockPreprocess,
  },
  {
    path: "/settings/parser-adapters",
    text: "取込済みの文書は、文書の詳細で再処理するまで今の解析結果のまま検索されます。",
    mock: mockParserAdapters,
  },
  {
    path: "/settings/chunking",
    text: "取込済みの文書の chunk は、文書の詳細で再処理するまで変わりません。",
    mock: mockChunking,
  },
];

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  for (const target of PAGES) {
    test(`${target.path} は全体の既定の変更が取込済みの文書に及ぶ範囲を案内する (${viewport.name})`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await target.mock(page);
      await page.goto(target.path);
      await expect(page.getByText(target.text, { exact: false })).toBeVisible();
      await expect(page.getByText("文書のレシピで上書きしていない文書に、次の取込と再処理から使います。", { exact: false }).first()).toBeVisible();
      await expectNoPageOverflow(page);
    });
  }
}

function envelope(data: object) {
  return { data, error_messages: [], warning_messages: [] };
}

async function mockPreprocess(page: Page) {
  await page.route("**/api/settings/preprocess", (route) =>
    route.fulfill({
      json: envelope({
        profile: "passthrough",
        service_enabled: false,
        service_url: "",
        canonical_artifact_prefix: "artifacts/canonical",
        profiles: [
          {
            name: "passthrough",
            origin: "baseline_no_conversion",
            recommended_for: ["any"],
            selected: true,
            in_process: true,
            requires_service: false,
            available: true,
          },
        ],
        config_source: "runtime",
      }),
    })
  );
}

async function mockChunking(page: Page) {
  await page.route("**/api/settings/chunking", (route) =>
    route.fulfill({
      json: envelope({
        strategy: "structure_aware",
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
        strategies: [
          { name: "structure_aware", origin: "x", recommended_for: ["pdf"], selected: true },
        ],
        config_source: "runtime",
      }),
    })
  );
}

async function mockParserAdapters(page: Page) {
  await page.route("**/api/settings/parser-adapters", (route) =>
    route.fulfill({
      json: envelope({
        adapter_backend: "docling",
        effective_order: ["docling"],
        service_backends: [],
        adapters: [
          {
            backend: "docling",
            package_name: "docling",
            import_name: "docling",
            distribution_name: "docling",
            install_package: "docling",
            enabled: true,
            selected: true,
            installed: true,
            status: "active",
            version: null,
            warning_code: null,
          },
        ],
        connections: [],
        scorecard: {
          selected_backend: "docling",
          recommended_backend: "docling",
          metrics_source: "static",
          metrics_applied_to: [],
          entries: [],
        },
        source_routes: [],
        backend_source_kind_matrix: {
          evidence_source: "runtime_routes",
          required_source_kinds: [],
          covered_source_kinds: [],
          missing_source_kinds: [],
          backend_source_kinds: {},
          route_evidence: [],
        },
        capabilities: [],
        vision_enabled: false,
        field_extraction_enabled: false,
        navigation_summary_enabled: false,
        config_source: "runtime",
      }),
    })
  );
}
