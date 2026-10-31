import { readFileSync } from "node:fs";
import { expect, test, type Locator, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady, systemAdminMe } from "./_helpers/database-gate";
import { dropFiles } from "./_helpers/file-dropzone";
import { expectLargeActionButton } from "./_helpers/action-button";
import { expectSingleSpinner, visibleSpinners } from "./_helpers/single-spinner";
import { chooseSelectFieldOption, expectSelectFieldValue } from "./_helpers/select-field";
import { pollWithClock } from "./_helpers/clock";
import {
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

test.beforeEach(async ({ page }) => mockDatabaseGateReady(page));

async function fulfillJson(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data }),
  });
}

function createRequestGate() {
  let release: () => void = () => undefined;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

type ScrollSettleElement = HTMLElement & { scrollSettled?: Promise<void> };

/**
 * スクロール領域でキー（Home / End）を押し、そのキーのスクロールが終わる（scrollend）まで待つ。
 * Chromium のキーボードのスクロールは滑らかなスクロール（アニメーション）で、scrollTop が端に届いても
 * scrollend の前に次のキーを押すと、そのキーのスクロールが失われることがある（375px で約 1 割。#342 / #391）。
 */
async function pressScrollKey(region: Locator, key: "Home" | "End") {
  await region.evaluate((element: ScrollSettleElement) => {
    element.scrollSettled = new Promise<void>((resolve) => {
      element.addEventListener("scrollend", () => resolve(), { once: true });
      // すでに端にあって動かないときは scrollend が来ないため、上限で打ち切る（位置は呼び出し側の poll が判定する）。
      setTimeout(resolve, 2_000);
    });
  });
  await region.press(key);
  await region.evaluate((element: ScrollSettleElement) => element.scrollSettled);
}

async function expectButtonLabelFits(button: Locator) {
  const spans = button.locator("span");
  const label = await spans.count() ? spans.last() : button;
  await expect(label).toBeVisible();
  const metrics = await label.evaluate((element) => {
    const buttonElement = element.closest("button");
    if (!(buttonElement instanceof HTMLButtonElement)) {
      throw new Error("Button label is not inside a button.");
    }
    const labelRect = element.getBoundingClientRect();
    const buttonRect = buttonElement.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return {
      buttonBottom: buttonRect.bottom,
      buttonTop: buttonRect.top,
      fontSize: Number.parseFloat(style.fontSize),
      labelBottom: labelRect.bottom,
      labelTop: labelRect.top,
      lineHeight: Number.parseFloat(style.lineHeight),
    };
  });
  expect(metrics.lineHeight).toBeGreaterThan(metrics.fontSize);
  expect(metrics.labelTop).toBeGreaterThanOrEqual(metrics.buttonTop - 0.5);
  expect(metrics.labelBottom).toBeLessThanOrEqual(metrics.buttonBottom + 0.5);
}

async function expectBuildPanelsStackedFullWidth(page: Page) {
  const section = page.getByTestId("profile-ontology-build");
  const setupPanel = page.getByTestId("ontology-build-setup-panel");
  const reviewPanel = page.getByTestId("ontology-build-review-panel");
  await expect(page.getByTestId("fixed-split-pane-ontology-build-workspace")).toHaveCount(0);
  await expect(setupPanel).toBeVisible();
  await expect(reviewPanel).toBeVisible();
  const [sectionBox, setupBox, reviewBox] = await Promise.all([
    section.boundingBox(),
    setupPanel.boundingBox(),
    reviewPanel.boundingBox(),
  ]);
  expect(sectionBox).not.toBeNull();
  expect(setupBox).not.toBeNull();
  expect(reviewBox).not.toBeNull();
  expect(reviewBox!.y).toBeGreaterThan(setupBox!.y + setupBox!.height - 1);
  expect(Math.abs(setupBox!.x - reviewBox!.x)).toBeLessThanOrEqual(1);
  expect(setupBox!.x).toBeGreaterThanOrEqual(sectionBox!.x);
  expect(reviewBox!.x).toBeGreaterThanOrEqual(sectionBox!.x);
  expect(setupBox!.x + setupBox!.width).toBeLessThanOrEqual(
    sectionBox!.x + sectionBox!.width + 1
  );
  expect(reviewBox!.x + reviewBox!.width).toBeLessThanOrEqual(
    sectionBox!.x + sectionBox!.width + 1
  );
  expect(setupBox!.width).toBeGreaterThanOrEqual(sectionBox!.width - 40);
  expect(reviewBox!.width).toBeGreaterThanOrEqual(sectionBox!.width - 40);
}

async function expectSourceDropzoneMatchesQaStyle(page: Page) {
  const sourcePanel = page.getByTestId("ontology-build-source-panel");
  await expect(sourcePanel).toHaveClass(/grid min-w-0 gap-2/);
  await expect(sourcePanel).not.toHaveClass(/rounded-md/);
  await expect(sourcePanel).not.toHaveClass(/border/);

  const [sourceRootClass, qaRootClass] = await Promise.all([
    page.getByTestId("ontology-build-source-files").getAttribute("class"),
    page.getByTestId("ontology-build-qa-file").getAttribute("class"),
  ]);
  expect(sourceRootClass).not.toBeNull();
  expect(sourceRootClass).toBe(qaRootClass);

  await expect(page.getByTestId("ontology-build-source-files-input")).toHaveAttribute(
    "aria-required",
    "false"
  );
  await expect(page.locator("label").filter({ hasText: /^構築資料$/ })).toHaveCount(1);
}

async function loadOntologyBuildWorkspace(page: Page) {
  const fetchButton = page.getByTestId("ontology-view-fetch");
  await expect(fetchButton).toBeVisible();
  await fetchButton.click();
  await expect(page.getByTestId("profile-ontology-build")).toBeVisible();
}

async function confirmPreparedPublish(page: Page) {
  const button = page.getByRole("button", { name: "確認した内容を公開", exact: true });
  // 公開は必ず準備 → 確認の順。前回の公開状態が表示されていても、確認ボタンが出るまで待つ（瞬間判定で抜けない。#175）。
  await button.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "オントロジーを公開", exact: true }).click();
}

type BuildRunOptions = {
  runSchemaNaming: boolean | null;
  runQaExtraction: boolean | null;
  runTextExtraction: boolean | null;
};

function multipartFieldValue(postData: string, fieldName: string): string | null {
  const marker = `name="${fieldName}"`;
  const markerIndex = postData.indexOf(marker);
  if (markerIndex < 0) return null;
  const valueStart = postData.indexOf("\r\n\r\n", markerIndex);
  if (valueStart < 0) return null;
  const rest = postData.slice(valueStart + 4);
  const valueEnd = rest.indexOf("\r\n");
  return (valueEnd < 0 ? rest : rest.slice(0, valueEnd)).trim();
}

function multipartBooleanValue(postData: string, fieldName: string): boolean | null {
  const value = multipartFieldValue(postData, fieldName);
  if (value === "true") return true;
  if (value === "false") return false;
  return null;
}

async function expectExtractionTargetsHidden(page: Page) {
  await expect(page.getByText("実行する抽出", { exact: true })).toHaveCount(0);
  await expect(page.getByText("業務エンティティ命名・説明", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Q/A からの関係・指標抽出", { exact: true })).toHaveCount(0);
  await expect(page.getByText("業務説明からの補強", { exact: true })).toHaveCount(0);
}

const profiles = [
  {
    id: "default",
    name: "既定プロファイル",
    category: "既定",
    description: "AI 構築の確認",
    allowed_tables: ["ORDERS", "CUSTOMERS"],
    allowed_views: [],
    glossary: {},
    sql_rules: [],
    default_row_limit: 100,
    safety_policy: "select_only",
    few_shot_examples: [],
    select_ai_config: null,
    archived: false,
  },
];

const ontologyView = {
  profile_ontology_view: {
    id: "profile-view:default",
    profile_id: "default",
    ontology_revision_id: "revision-1",
    etag: "view-etag",
    node_ids: ["table:APP:ORDERS", "table:APP:CUSTOMERS"],
    edge_ids: ["fk:orders-customers"],
    allowed_path_ids: [],
  },
  ontology_graph: {
    revision: {
      id: "revision-1",
      version: 3,
      status: "published",
      schema_fingerprint: "fp",
      etag: "rev-etag",
    },
    nodes: [
      {
        id: "table:APP:ORDERS",
        kind: "table",
        business_name_ja: "受注",
        review_status: "approved",
        physical_mappings: [
          { object_ref: { owner: "APP", object_name: "ORDERS", object_type: "table" } },
        ],
      },
      {
        id: "table:APP:CUSTOMERS",
        kind: "table",
        business_name_ja: "顧客",
        review_status: "approved",
        physical_mappings: [
          { object_ref: { owner: "APP", object_name: "CUSTOMERS", object_type: "table" } },
        ],
      },
    ],
    edges: [
      {
        id: "fk:orders-customers",
        kind: "foreign_key",
        source_node_id: "table:APP:ORDERS",
        target_node_id: "table:APP:CUSTOMERS",
        relationship_name_ja: "顧客を参照",
        cardinality: "many_to_one",
        review_status: "approved",
      },
    ],
  },
};

const generatedDraftMarkdown = [
  "# オントロジー下書き",
  "",
  "## 入力サマリー",
  "- プロファイル: `default`",
  "- DB スキーマオブジェクト: 2",
  "",
  "## 物理オブジェクト",
  "- `APP.ORDERS` (table)",
  "  - 業務名: 受注",
  "  - 用途: 確定済み受注の売上分析に使用",
  "- `APP.CUSTOMERS` (table)",
  "  - 業務名: 顧客",
  "",
  "## 業務エンティティ",
  "- 受注 (`APP.ORDERS`)",
  "",
  "## 関係 / Join",
  "- 受注 (`APP.ORDERS`) → 顧客 (`APP.CUSTOMERS`): 顧客を参照",
  "  - 検索利用: 利用可",
  "  - Join 条件:",
  "    - `APP.ORDERS.CUSTOMER_ID = APP.CUSTOMERS.ID`",
  "",
  "## 指標",
  "- 受注金額合計",
  "",
  "## 業務ルール / 列挙値",
  "- なし",
  "",
  "## 同義語",
  "- 対象: `APP.ORDERS`",
  "",
  "## 証拠 / 確認事項",
  "- なし",
  "",
  "## 採用外候補",
  "- 命名候補 APP.SECRET を profile 範囲内に解決できません。",
].join("\n");

function markdownDraftPayload(markdown: string, etag = "markdown-etag-1") {
  return {
    draft_markdown: markdown,
    published_markdown: "",
    draft_revision: {
      id: "revision-draft-4",
      version: 4,
      status: "draft",
      schema_fingerprint: "fp",
      etag: "draft-etag-4",
    },
    published_revision: null,
    draft_etag: etag,
    published_at: null,
  };
}

function emptyMarkdownPayload() {
  return {
    draft_markdown: "",
    published_markdown: "",
    draft_revision: null,
    published_revision: null,
    draft_etag: "",
    published_at: null,
  };
}

function buildJob(status: string, stepStatus: string, proposalIds: string[] = []) {
  const stepTimes =
    stepStatus === "pending"
      ? {}
      : {
          started_at: "2026-07-12T00:00:01Z",
          finished_at: stepStatus === "running" ? null : "2026-07-12T00:00:05Z",
        };
  const events =
    stepStatus === "pending"
      ? []
      : [
          { at: "2026-07-12T00:00:01Z", message_ja: "AI オントロジー構築を開始しました。" },
          {
            at: "2026-07-12T00:00:02Z",
            message_ja: "スキーマ情報を準備しました(表・ビュー 2 件、列 5 件)。",
          },
          ...(status === "succeeded"
            ? [
                {
                  at: "2026-07-12T00:00:08Z",
                  message_ja: "Markdown 下書き v4 を生成しました(候補 2 件、採用外 1 件)。",
                },
                {
                  at: "2026-07-12T00:00:09Z",
                  message_ja: "構築が完了しました(Markdown 下書き v4、採用外 1 件)。",
                },
              ]
            : []),
        ];
  return {
    job: {
      id: "job-1",
      definition_phases: ["freeze", "evidence", "objects", "shared", "capabilities", "validation"].map(name => ({ name, status: stepStatus, detail_ja: "" })),
      profile_id: "default",
      status,
      steps: [
        {
          name: "schema_context",
          status: stepStatus,
          detail_ja: "表・ビュー 2 件、列 5 件",
          ...stepTimes,
        },
        { name: "schema_naming", status: stepStatus, detail_ja: "", ...stepTimes },
        { name: "text_extraction", status: stepStatus, detail_ja: "", ...stepTimes },
        {
          name: "proposal_registration",
          status: stepStatus,
          detail_ja:
            stepStatus === "succeeded"
              ? "Markdown 下書き v4 を生成しました(候補 2 件、採用外 1 件)。"
              : "",
          ...stepTimes,
        },
      ],
      events,
      proposal_ids: proposalIds,
      result_bundle_id: status === "succeeded" ? "bundle-default" : "",
      draft_revision_id: status === "succeeded" ? "revision-draft-4" : "",
      draft_etag: status === "succeeded" ? "markdown-etag-1" : "",
      markdown_output: status === "succeeded" ? generatedDraftMarkdown : "",
      warnings_ja:
        status === "succeeded"
          ? [
              "命名候補 APP.SECRET を profile 範囲内に解決できません。",
              "schema_resolved_join_conditionsが空のため自己結合は明示せず。",
            ]
          : [],
      error_message_ja: "",
      created_at: "2026-07-12T00:00:00Z",
      started_at: stepStatus === "pending" ? null : "2026-07-12T00:00:01Z",
      finished_at: status === "succeeded" ? "2026-07-12T00:00:10Z" : null,
    },
  };
}

const proposalsPending = [
  {
    id: "proposal-1",
    session_id: "ontology_build:job-1",
    profile_id: "default",
    base_revision_id: "revision-1",
    title_ja: "業務エンティティ命名: 受注",
    description_ja: "APP.ORDERS の業務名候補",
    kind: "mapping",
    status: "submitted",
    proposal_payload: { kind: "mapping", values: {} },
    created_at: "2026-07-12T00:00:10Z",
  },
  {
    id: "proposal-2",
    session_id: "ontology_build:job-1",
    profile_id: "default",
    base_revision_id: "revision-1",
    title_ja: "業務関係の提案: 顧客を参照",
    description_ja: "Q/A の JOIN 句",
    kind: "relationship",
    status: "submitted",
    proposal_payload: { kind: "relationship", values: {} },
    created_at: "2026-07-12T00:00:10Z",
  },
];

async function mockApi(page: Page) {
  await page.route("**/api/nl2sql/profiles/*/ontology-capabilities", route => fulfillJson(route, {release_id:"",display_version:null,capabilities:[],implementations:{functions:[],actions:[]}}));
  await page.route("**/api/nl2sql/profiles/*/ontology-results/*/workspace", route => fulfillJson(route, { bundle: typedBundle("default"), artifacts: { markdown: "# 型付き定義", mermaid: "graph LR" }, head: { release_id: "", etag: "" } }));
  await page.route("**/api/nl2sql/profiles/*/ontology-results", route => fulfillJson(route, { results: [] }));
  const state = {
    jobPolls: 0,
    accepted: new Set<string>(),
    published: false,
    publishPolls: 0,
    startPayloadSeen: false,
    startCalls: 0,
    idempotencySeen: false,
    sourceFilesSeen: false,
    qaFileSeen: false,
    latestBusinessText: null as string | null,
    schemaRefreshCalls: 0,
    latestRunOptions: null as BuildRunOptions | null,
    ontologyDraftPayload: null as Record<string, unknown> | null,
    ontologyViewCalls: 0,
    draftMarkdown: generatedDraftMarkdown,
    draftMarkdownEtag: "markdown-etag-1",
    savedDraftMarkdown: null as string | null,
    publishedMarkdown: "",
    publishPayload: null as Record<string, unknown> | null,
    profileDetailCalls: [] as string[],
    buildJobProfileIds: [] as string[],
    sourceDocumentProfileIds: [] as string[],
    markdownProfileIds: [] as string[],
  };
  const currentOntologyViewPayload = () => {
    const revision = state.published
      ? {
          id: "revision-draft-4",
          version: 4,
          status: "published",
          schema_fingerprint: "fp",
          etag: "draft-etag-4",
        }
      : ontologyView.ontology_graph.revision;
    return {
      ...ontologyView,
      profile_ontology_view: {
        ...ontologyView.profile_ontology_view,
        ontology_revision_id: revision.id,
      },
      ontology_graph: {
        ...ontologyView.ontology_graph,
        revision,
      },
    };
  };
  const markdownStatePayload = () => {
    const hasDraft = state.jobPolls >= 2;
    return {
      draft_markdown: hasDraft ? state.draftMarkdown : "",
      published_markdown: state.published ? state.publishedMarkdown || state.draftMarkdown : "",
      draft_revision: hasDraft
        ? {
            id: "revision-draft-4",
            version: 4,
            status: state.published ? "published" : "draft",
            schema_fingerprint: "fp",
            etag: "draft-etag-4",
          }
        : null,
      published_revision: state.published
        ? {
            id: "revision-draft-4",
            version: 4,
            status: "published",
            schema_fingerprint: "fp",
            etag: "draft-etag-4",
            published_at: "2026-07-12T00:00:20Z",
          }
        : null,
      draft_etag: hasDraft ? state.draftMarkdownEtag : "",
      published_at: state.published ? "2026-07-12T00:00:20Z" : null,
    };
  };
  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(route, { refreshed_at: "2026-07-12T00:00:00Z", tables: [] })
  );
  await page.route("**/api/schema/refresh-jobs", (route) => {
    state.schemaRefreshCalls += 1;
    return fulfillJson(route, {
      job_id: "ontology-build-schema-refresh-done",
      status: "done",
      created_at: "2026-07-12T00:00:00Z",
      scanned_objects: 2,
      changed_objects: 1,
      deleted_objects: 0,
      catalog_version: 2,
      error_code: "",
    });
  });
  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfillJson(route, { runtime: "deterministic", items: [], warnings: [] })
  );
  await page.route("**/api/nl2sql/db-admin/views", (route) =>
    fulfillJson(route, { runtime: "deterministic", items: [], warnings: [] })
  );
  await page.route("**/api/nl2sql/select-ai/db-profiles**", (route) =>
    fulfillJson(route, { runtime: "deterministic", profiles: [], warnings: [] })
  );
  await page.route("**/api/nl2sql/profiles", (route) => fulfillJson(route, profiles));
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: profiles.map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: profile.archived,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: Object.keys(profile.glossary).length,
        few_shot_count: profile.few_shot_examples.length,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-07-12T00:00:00Z",
      })),
      next_cursor: null,
      total: profiles.length,
      change_token: 1,
    })
  );
  await page.route(/\/api\/nl2sql\/profiles\/[^/?]+$/, (route) => {
    state.profileDetailCalls.push(profileIdFromUrl(route.request().url()));
    return fulfillJson(route, profiles[0]);
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-view", (route) => {
    state.ontologyViewCalls += 1;
    if (route.request().method() === "PATCH") {
      state.ontologyDraftPayload = route.request().postDataJSON() as Record<string, unknown>;
    }
    return fulfillJson(route, {
      ...currentOntologyViewPayload(),
      materialized: true,
      stale: false,
    });
  });
  await page.route("**/api/nl2sql/ontology/revisions", (route) =>
    fulfillJson(route, {
      revisions: [ontologyView.ontology_graph.revision],
      active_revision_id: ontologyView.ontology_graph.revision.id,
    })
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) => {
    state.buildJobProfileIds.push(profileIdFromUrl(route.request().url()));
    return fulfillJson(route, { jobs: [] });
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-source-documents**", (route) => {
    state.sourceDocumentProfileIds.push(profileIdFromUrl(route.request().url()));
    return fulfillJson(route, { source_documents: [] });
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/draft", (route) => {
    const body = route.request().postDataJSON() as { markdown?: string; base_etag?: string };
    state.savedDraftMarkdown = body.markdown ?? "";
    state.draftMarkdown = state.savedDraftMarkdown;
    state.draftMarkdownEtag = "markdown-etag-2";
    return fulfillJson(route, markdownStatePayload());
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) => {
    state.markdownProfileIds.push(profileIdFromUrl(route.request().url()));
    return fulfillJson(route, markdownStatePayload());
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-build", async (route) => {
    state.startPayloadSeen = true;
    state.startCalls += 1;
    state.idempotencySeen = Boolean(route.request().headers()["idempotency-key"]);
    const postData = route.request().postData() ?? "";
    state.latestBusinessText = multipartFieldValue(postData, "business_text");
    state.latestRunOptions = {
      runSchemaNaming: multipartBooleanValue(postData, "run_schema_naming"),
      runQaExtraction: multipartBooleanValue(postData, "run_qa_extraction"),
      runTextExtraction: multipartBooleanValue(postData, "run_text_extraction"),
    };
    state.sourceFilesSeen = postData.includes("rules.md") && postData.includes("terms.csv");
    state.qaFileSeen = postData.includes("qa_cases.csv");
    await fulfillJson(route, buildJob("queued", "pending"));
  });
  await page.route("**/api/nl2sql/ontology-build/*", async (route) => {
    state.jobPolls += 1;
    if (state.jobPolls < 2) {
      await fulfillJson(route, buildJob("running", "running"));
      return;
    }
    await fulfillJson(route, buildJob("succeeded", "succeeded", ["proposal-1", "proposal-2"]));
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-proposals", (route) =>
    fulfillJson(route, {
      // job 完了までは提案なし(初期表示は空状態)
      proposals:
        state.jobPolls < 2
          ? []
          : proposalsPending.map((proposal) =>
              state.accepted.has(proposal.id) ? { ...proposal, status: "accepted" } : proposal
            ),
    })
  );
  await page.route("**/api/nl2sql/ontology/proposals/batch-accept", async (route) => {
    const body = route.request().postDataJSON() as { proposal_ids?: string[] };
    (body.proposal_ids ?? []).forEach((id) => state.accepted.add(id));
    await fulfillJson(route, {
      proposals: proposalsPending.map((proposal) =>
        state.accepted.has(proposal.id) ? { ...proposal, status: "accepted" } : proposal
      ),
      draft: {
        revision: {
          id: "revision-draft-4",
          version: 4,
          status: "draft",
          schema_fingerprint: "fp",
          etag: "draft-etag-4",
        },
        nodes: [],
        edges: [],
      },
    });
  });
  await page.route("**/api/nl2sql/ontology/proposals/*/accept", async (route) => {
    const url = route.request().url();
    const proposalId = url.split("/proposals/")[1]?.split("/")[0] ?? "";
    state.accepted.add(proposalId);
    await fulfillJson(route, {
      proposal: {
        ...proposalsPending.find((proposal) => proposal.id === proposalId),
        status: "accepted",
      },
      draft: {
        revision: {
          id: "revision-draft-4",
          version: 4,
          status: "draft",
          schema_fingerprint: "fp",
          etag: "draft-etag-4",
        },
        nodes: [],
        edges: [],
      },
    });
  });
  const preparation = () => ({ id: "preparation-1", status: "ready", draft_etag: state.draftMarkdownEtag, expected_head: "", display_version: 4, findings: [], differences: [{ id: "Order", before: null, after: { kind: "object_type", name_ja: "受注", api_name: "Order" } }], error_message_ja: "" });
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/prepare", route => fulfillJson(route, preparation()));
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/preparations/*", route => fulfillJson(route, preparation()));
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/publish", async (route) => {
    state.publishPayload = route.request().postDataJSON() as Record<string, unknown>;
    state.published = true;
    state.publishedMarkdown = state.draftMarkdown;
    await fulfillJson(route, {
      job: {
        id: "publish-job-1",
        revision_id: "revision-draft-4",
        requested_etag: "draft-etag-4",
        status: "queued",
      },
    });
  });
  await page.route("**/api/nl2sql/ontology-publish/*", async (route) => {
    state.publishPolls += 1;
    await fulfillJson(route, {
      job: {
        id: "publish-job-1",
        revision_id: "revision-draft-4",
        requested_etag: "draft-etag-4",
        status: "succeeded",
        rdf_graph_name: "ONT_0123456789ABCDEF",
        inferred_graph_name: "INF_0123456789ABCDEF",
        shacl_conforms: true,
      },
    });
  });
  return state;
}

const profileScopedProfiles = [
  {
    id: "sales",
    name: "販売分析",
    category: "営業",
    description: "営業部門の受注分析",
    allowed_tables: ["ORDERS"],
    allowed_views: [],
    glossary: {},
    sql_rules: [],
    default_row_limit: 100,
    safety_policy: "select_only",
    few_shot_examples: [],
    select_ai_config: null,
    archived: false,
  },
  {
    id: "finance",
    name: "経理分析",
    category: "経理",
    description: "経理部門の請求分析",
    allowed_tables: ["INVOICES"],
    allowed_views: [],
    glossary: {},
    sql_rules: [],
    default_row_limit: 100,
    safety_policy: "select_only",
    few_shot_examples: [],
    select_ai_config: null,
    archived: false,
  },
];

function profileIdFromUrl(url: string): string {
  const match = url.match(/\/api\/nl2sql\/profiles\/([^/?]+)/u);
  return match ? decodeURIComponent(match[1]) : "";
}

function profileScopedOntologyView(profileId: string) {
  return {
    profile_ontology_view: {
      id: `profile-view:${profileId}`,
      profile_id: profileId,
      ontology_revision_id: `revision-${profileId}`,
      etag: `view-etag-${profileId}`,
      node_ids: [],
      edge_ids: [],
      allowed_path_ids: [],
    },
    ontology_graph: {
      revision: {
        id: `revision-${profileId}`,
        version: 1,
        status: "published",
        schema_fingerprint: `fp-${profileId}`,
        etag: `revision-etag-${profileId}`,
      },
      nodes: [],
      edges: [],
    },
    materialized: true,
    stale: false,
    warnings_ja: [],
  };
}

async function mockProfileScopedApi(page: Page) {
  const state = {
    profileDetailCalls: [] as string[],
    ontologyViewCalls: [] as string[],
    buildJobProfileIds: [] as string[],
    sourceDocumentProfileIds: [] as string[],
    markdownProfileIds: [] as string[],
    deletedSourceIds: [] as string[],
    sourceDocumentsByProfile: {
      sales: [
        {
          id: "source-sales",
          profile_id: "sales",
          filename: "sales-rules.md",
          source_role: "source",
          media_type: "text/markdown",
          size_bytes: 2048,
          status: "extracted",
          extracted_chunk_count: 2,
          warnings_ja: [],
          created_at: "2026-07-12T00:00:00Z",
          updated_at: "2026-07-12T00:00:00Z",
        },
      ],
      finance: [
        {
          id: "source-finance",
          profile_id: "finance",
          filename: "finance-rules.xlsx",
          source_role: "source",
          media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          size_bytes: 4096,
          status: "extracted",
          extracted_chunk_count: 4,
          warnings_ja: [],
          created_at: "2026-07-12T00:00:00Z",
          updated_at: "2026-07-12T00:00:00Z",
        },
      ],
    } as Record<string, Array<Record<string, unknown>>>,
  };
  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(route, { refreshed_at: "2026-07-12T00:00:00Z", tables: [] })
  );
  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfillJson(route, { runtime: "deterministic", items: [], warnings: [] })
  );
  await page.route("**/api/nl2sql/db-admin/views", (route) =>
    fulfillJson(route, { runtime: "deterministic", items: [], warnings: [] })
  );
  await page.route("**/api/nl2sql/select-ai/db-profiles**", (route) =>
    fulfillJson(route, { runtime: "deterministic", profiles: [], warnings: [] })
  );
  await page.route("**/api/nl2sql/profiles", (route) => fulfillJson(route, profileScopedProfiles));
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: profileScopedProfiles.map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: profile.archived,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: 0,
        few_shot_count: 0,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-07-12T00:00:00Z",
      })),
      next_cursor: null,
      total: profileScopedProfiles.length,
      change_token: 1,
    })
  );
  await page.route(/\/api\/nl2sql\/profiles\/[^/?]+$/, (route) => {
    const profileId = profileIdFromUrl(route.request().url());
    state.profileDetailCalls.push(profileId);
    fulfillJson(
      route,
      profileScopedProfiles.find((profile) => profile.id === profileId) ??
        profileScopedProfiles[0]
    );
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-view", (route) => {
    state.ontologyViewCalls.push(profileIdFromUrl(route.request().url()));
    fulfillJson(route, profileScopedOntologyView(profileIdFromUrl(route.request().url())));
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) => {
    state.buildJobProfileIds.push(profileIdFromUrl(route.request().url()));
    return fulfillJson(route, { jobs: [] });
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-source-documents**", async (route) => {
    const profileId = profileIdFromUrl(route.request().url());
    const sourceDocumentId = route
      .request()
      .url()
      .match(/\/ontology-source-documents\/([^/?]+)/u)?.[1];
    if (route.request().method() === "DELETE" && sourceDocumentId) {
      const decodedSourceDocumentId = decodeURIComponent(sourceDocumentId);
      state.deletedSourceIds.push(`${profileId}:${decodedSourceDocumentId}`);
      const documents = state.sourceDocumentsByProfile[profileId] ?? [];
      const nextDocuments = documents.filter((source) => source.id !== decodedSourceDocumentId);
      state.sourceDocumentsByProfile[profileId] = nextDocuments;
      if (nextDocuments.length === documents.length) {
        await route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ error: "not found" }),
        });
        return;
      }
      await fulfillJson(route, { source_document_id: decodedSourceDocumentId, deleted: true });
      return;
    }
    state.sourceDocumentProfileIds.push(profileId);
    await fulfillJson(route, {
      source_documents: state.sourceDocumentsByProfile[profileId] ?? [],
    });
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) => {
    state.markdownProfileIds.push(profileIdFromUrl(route.request().url()));
    return fulfillJson(route, emptyMarkdownPayload());
  });
  return state;
}

test("AI オントロジー構築の実行 → 進捗 → Markdown 下書き編集 → 公開の導線が機能する", { tag: "@responsive" }, async ({ context, page }, testInfo) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: `http://127.0.0.1:${process.env.PLAYWRIGHT_PORT ?? "3101"}`,
  });
  const state = await mockApi(page);
  await page.goto("/ontology-build?profile=default");

  await expect(page.locator("#ontology-workspace-not-loaded")).toBeVisible();
  await expect(page.getByTestId("profile-ontology-build")).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-markdown")).toHaveCount(0);
  expect(state.profileDetailCalls).toEqual([]);
  expect(state.buildJobProfileIds).toEqual([]);
  expect(state.sourceDocumentProfileIds).toEqual([]);
  expect(state.markdownProfileIds).toEqual([]);
  expect(state.ontologyViewCalls).toBe(0);

  await loadOntologyBuildWorkspace(page);
  const section = page.getByTestId("profile-ontology-build");
  await expect(section.getByRole("heading", { name: "オントロジー構築" })).toBeVisible();
  await expect(page.getByRole("button", { name: /オントロジー view を/ })).toHaveCount(0);
  await expect(page.getByText("オントロジー連携", { exact: true })).toHaveCount(0);
  await expect(page.getByText("業種テンプレート", { exact: true })).toHaveCount(0);
  await expect(page.getByText("OWL(RDF)エクスポート / インポート", { exact: true })).toHaveCount(
    0
  );
  await expect(page.getByText("60 分以上かかることがあります", { exact: false })).toBeVisible();
  await expectBuildPanelsStackedFullWidth(page);
  await expectSourceDropzoneMatchesQaStyle(page);
  await expectExtractionTargetsHidden(page);
  await expect(page.getByTestId("ontology-view-fetch")).toBeVisible();
  await expectLargeActionButton(page.getByTestId("ontology-view-fetch"));
  await expect(page.getByTestId("ontology-view-fetch").locator("span").first()).toHaveText(
    "情報を取得"
  );

  // 初期状態では Draft / Published とも空
  await expect(section.getByTestId("ontology-markdown-draft-empty")).toBeVisible();
  await section.getByRole("tab", { name: "公開済み Markdown オントロジー" }).click();
  await expect(section.getByText("公開済み Markdown はまだありません。")).toBeVisible();
  await section.getByRole("tab", { name: "Markdown オントロジー下書き" }).click();
  await expect(page.getByTestId("ontology-build-source-files-input")).toHaveAttribute(
    "accept",
    ".pdf,.docx,.txt,.md,.csv,.xlsx,.xls,.xlsm"
  );
  await expect(
    page
      .getByTestId("ontology-build-source-files")
      .getByText(".PDF / .DOCX / .TXT / .MD / .CSV / .XLSX / .XLS / .XLSM", {
        exact: true,
      })
  ).toBeVisible();
  await expect(
    page.getByText(
      "PDF / DOCX / TXT / MD / CSV / XLSX / XLS / XLSM を Q/A ファイルと合わせて最大 5 件まで選択できます。原本と証拠位置を保持します。",
      { exact: true }
    )
  ).toBeVisible();
  await expect(page.getByTestId("ontology-build-qa-file-input")).toHaveAttribute(
    "accept",
    ".csv,.xlsx,.xls,.xlsm"
  );
  await expect(
    page.getByText("CSV / XLSX / XLS / XLSM（QUESTION 列と SQL 列）", { exact: true })
  ).toBeVisible();

  // 実行 → ステップ進捗 → 完了
  await section
    .getByLabel("業務説明(自然言語)")
    .fill("受注は顧客に紐づく。売上は受注金額の合計。");
  const sourceClear = page
    .getByTestId("ontology-build-source-files")
    .getByRole("button", { name: "ファイル選択を解除" });
  const qaClear = page
    .getByTestId("ontology-build-qa-file")
    .getByRole("button", { name: "ファイル選択を解除" });
  await expect(sourceClear).toBeDisabled();
  await expect(qaClear).toBeDisabled();
  await dropFiles(
    page,
    page.getByTestId("ontology-build-source-files-dropzone"),
    Array.from({ length: 6 }, (_, index) => ({
      name: `source-${index + 1}.md`,
      type: "text/markdown",
      content: `# 資料 ${index + 1}`,
      lastModified: 1_700_000_010_000 + index,
    }))
  );
  await expect(
    page.getByText(
      "構築資料は Q/A ファイルと合わせて最大 5 件までアップロードできます。ファイルを減らして再度選択してください。",
      { exact: true }
    )
  ).toBeVisible();
  await expect(section.getByRole("list", { name: "選択した構築資料" })).toHaveCount(0);
  await expect(sourceClear).toBeDisabled();
  await dropFiles(page, page.getByTestId("ontology-build-source-files-dropzone"), [
    {
      name: "rules.md",
      type: "text/markdown",
      content: "# 受注ルール",
      lastModified: 1_700_000_000_000,
    },
    {
      name: "terms.csv",
      type: "text/csv",
      content: "用語,説明\n受注,顧客からの注文",
      lastModified: 1_700_000_001_000,
    },
  ]);
  await expect(sourceClear).toBeEnabled();
  await sourceClear.click();
  await expect(section.getByRole("list", { name: "選択した構築資料" })).toHaveCount(0);
  await expect(sourceClear).toBeDisabled();
  await dropFiles(page, page.getByTestId("ontology-build-source-files-dropzone"), [
    {
      name: "rules.md",
      type: "text/markdown",
      content: "# 受注ルール",
      lastModified: 1_700_000_000_000,
    },
    {
      name: "terms.csv",
      type: "text/csv",
      content: "用語,説明\n受注,顧客からの注文",
      lastModified: 1_700_000_001_000,
    },
  ]);
  await dropFiles(page, page.getByTestId("ontology-build-source-files-dropzone"), [
    {
      name: "rules.md",
      type: "text/markdown",
      content: "# 受注ルール",
      lastModified: 1_700_000_000_000,
    },
  ]);
  await dropFiles(page, page.getByTestId("ontology-build-qa-file-dropzone"), [
    {
      name: "qa_cases.csv",
      type: "text/csv",
      content: "QUESTION,SQL\n受注件数は,SELECT COUNT(*) FROM ORDERS",
    },
  ]);
  const sourceFileList = section.getByRole("list", { name: "選択した構築資料" });
  await expect(sourceFileList.getByText("rules.md", { exact: true })).toHaveCount(1);
  await expect(sourceFileList.getByText("terms.csv", { exact: true })).toBeVisible();
  await section.getByRole("button", { name: "terms.csv を資料一覧から削除" }).click();
  await expect(sourceFileList.getByText("terms.csv", { exact: true })).toHaveCount(0);
  await dropFiles(page, page.getByTestId("ontology-build-source-files-dropzone"), [
    {
      name: "terms.csv",
      type: "text/csv",
      content: "用語,説明\n受注,顧客からの注文",
      lastModified: 1_700_000_001_000,
    },
  ]);
  await expect(sourceFileList.getByText("terms.csv", { exact: true })).toBeVisible();
  await expect(section.getByText("選択済み: qa_cases.csv")).toBeVisible();
  await expect(qaClear).toBeEnabled();
  await expectButtonLabelFits(qaClear);
  await qaClear.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("qa-file-deselect.png") });
  await qaClear.click();
  await expect(section.getByText("選択済み: qa_cases.csv")).toHaveCount(0);
  await expect(qaClear).toBeDisabled();
  await dropFiles(page, page.getByTestId("ontology-build-qa-file-dropzone"), [
    {
      name: "qa_cases.csv",
      type: "text/csv",
      content: "QUESTION,SQL\n受注件数は,SELECT COUNT(*) FROM ORDERS",
    },
  ]);
  await expect(section.getByText("選択済み: qa_cases.csv")).toBeVisible();
  await expect(qaClear).toBeEnabled();
  await section.getByRole("button", { name: "AI 構築を実行" }).click();
  const steps = page.getByTestId("ontology-build-steps");
  await expect(steps.getByText("スキーマ情報の準備")).toBeVisible();
  await expect(steps.getByText("業務エンティティ命名")).toBeVisible();
  // 完了は工程ステッパーの「完了」バッジ(永続)で判定する。完了の“瞬間”通知は toast のため
  // section スコープには残らない(spec §9: 完了は状態表示が担い、瞬間だけ toast)。
  await expect(steps.getByText("完了").first()).toBeVisible({ timeout: 15000 });
  await expect(steps).toContainText("能力の契約（Capability Contracts）");
  expect(state.startPayloadSeen).toBe(true);
  expect(state.idempotencySeen).toBe(true);
  expect(state.latestRunOptions).toEqual({
    runSchemaNaming: true,
    runQaExtraction: true,
    runTextExtraction: true,
  });
  expect(state.sourceFilesSeen).toBe(true);
  expect(state.qaFileSeen).toBe(true);
  // 時刻付きイベントは独立ログ枠ではなく、関連する構築ステップ内の詳細ログとして表示される
  const schemaStep = page.getByTestId("ontology-build-step-schema_context");
  await expect(schemaStep.getByText("スキーマ情報を準備しました", { exact: false })).toBeVisible();
  const proposalStep = page.getByTestId("ontology-build-step-proposal_registration");
  await expect(proposalStep).toHaveAttribute("data-step-status", "succeeded");
  await steps.getByText("Markdown 下書き生成",{exact:true}).first().click();
  await expect(steps.getByText("Markdown 下書き v4 を生成しました", { exact: false })).toBeVisible();
  await expect(steps.getByText("構築リクエストを受け付けました", { exact: false })).toHaveCount(0);
  await expect(steps.getByText("AI オントロジー構築を開始しました", { exact: false })).toHaveCount(0);
  await expect(steps.getByText("構築が完了しました", { exact: false })).toHaveCount(0);
  await expect(page.locator('[aria-label="構築ジョブの補足ログ"]')).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-timeline")).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-history")).toHaveCount(0);
  await expect(steps.getByRole("timer")).toHaveAccessibleName(/処理時間 \d{2}:\d{2}/);
  // スコープ外候補は正常な採用外判定なので、成功 job の警告枠には出さない。
  await expect(steps.locator("summary").filter({ hasText: "警告" })).toHaveCount(0);
  await expect(steps.getByText("APP.SECRET", { exact: false })).toHaveCount(0);
  await expect(steps.getByText("schema_resolved_join_conditions", { exact: false })).toHaveCount(0);

  const markdown = page.getByTestId("ontology-build-markdown");
  await expect(
    markdown.getByTestId("ontology-build-markdown-actions").getByText("Markdown オントロジー", {
      exact: true,
    })
  ).toBeVisible();
  await expect(markdown.getByRole("tab", { name: "Markdown オントロジー下書き" })).toBeVisible();
  await expect(markdown.getByRole("tab", { name: "公開済み Markdown オントロジー" })).toBeVisible();
  await expect(markdown.getByTestId("ontology-markdown-tab-draft-meta")).toHaveText("v4");
  await expect(markdown.getByTestId("ontology-markdown-tab-published-meta")).toHaveText("未公開");
  const draftEditor = markdown.getByTestId("ontology-markdown-draft-editor");
  await expect(draftEditor).toHaveValue(/# オントロジー下書き/);
  await expect(draftEditor).toHaveValue(/## 採用外候補[\s\S]*APP\.SECRET/);
  await expect(draftEditor).toHaveValue(/## 関係 \/ Join/);
  const copyMarkdown = markdown.getByRole("button", { name: "Markdown をコピー" });
  await copyMarkdown.click();
  // 成功はボタンの Check と読み上げ（共有の CopyButton。#2099）。
  await expect(copyMarkdown).toHaveAttribute("data-copy-state", "copied");
  await expect(markdown.getByRole("status").filter({ hasText: "コピーしました" })).toHaveCount(1);

  await markdown.getByRole("tab", { name: "公開済み Markdown オントロジー" }).click();
  await expect(markdown.getByTestId("ontology-markdown-published-viewer")).toContainText(
    "公開済み Markdown はまだありません。"
  );
  await markdown.getByRole("tab", { name: "Markdown オントロジー下書き" }).click();

  // 標準図示は質問の接地確認グラフへ一本化され、公開後は同じ公開 revision へ更新される
  const ontologyQueryPanel = page.locator("#ontology-query-playground-panel");
  await expect(page.getByTestId("ontology-mermaid-panel")).toHaveCount(0);
  await expect(
    ontologyQueryPanel.getByRole("heading", { name: "質問のオントロジー接地確認用グラフ" })
  ).toBeVisible();
  await expect.poll(() => state.ontologyViewCalls).toBe(1);
  await expect(ontologyQueryPanel.getByTestId("ontology-playground-version")).toHaveCount(0);
  await expect(ontologyQueryPanel.getByText("公開済みオントロジーがまだありません", { exact: true })).toBeVisible();
  await expect(ontologyQueryPanel.getByTestId("ontology-graph-mode-physical_er")).toHaveCount(0);
  await expect(ontologyQueryPanel.getByTestId("ontology-playground-question")).toHaveCount(0);

  await draftEditor.fill(`${generatedDraftMarkdown}\n\n## 手動メモ\n- 公開確認済み`);
  await expect(markdown.getByText("未保存")).toBeVisible();
  const ontologyViewCallsBeforePublish = state.ontologyViewCalls;

  await expect(page.getByTestId("ontology-build-proposals")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /承認/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /却下/ })).toHaveCount(0);

  const publishActions = page.getByTestId("ontology-publish-actions");
  const publishButton = publishActions.getByRole("button", { name: "オントロジーを公開" });
  await expectButtonLabelFits(publishButton);
  await publishButton.click();
  await confirmPreparedPublish(page);
  // 公開完了の“瞬間”は toast(document.body 直下)で通知する(spec §9)。section 外なので page スコープで確認。
  await expect(page.getByText("オントロジーを公開しました。")).toBeVisible();
  expect(state.savedDraftMarkdown).toContain("手動メモ");
  expect(state.published).toBe(true);
  expect(state.publishPayload).toMatchObject({ preparation_id: "preparation-1", confirmed: true });
  await expect(page.getByTestId("ontology-publish-status")).toContainText("完了");
  await expect.poll(() => state.ontologyViewCalls).toBeGreaterThan(ontologyViewCallsBeforePublish);
  await expect(ontologyQueryPanel.getByTestId("ontology-playground-question")).toBeVisible();
  await expect(ontologyQueryPanel.getByTestId("ontology-playground-version")).toHaveCount(0);
  await expect(page.getByTestId("ontology-mermaid-panel")).toHaveCount(0);
  await markdown.getByRole("tab", { name: "公開済み Markdown オントロジー" }).click();
  await expect(markdown.getByTestId("ontology-markdown-published-viewer")).toContainText(
    "手動メモ"
  );
  await expect(markdown.getByTestId("ontology-markdown-tab-published-meta")).toHaveText("v4");
  await expect(markdown.getByTestId("ontology-markdown-published-meta")).toContainText(
    /公開日時: 07\/12 \d{2}:00/u
  );

  // 旧「物理・業務モデル編集」は Markdown 下書きに統合され、別編集 UI は表示しない
  await expect(page.locator('section[aria-label="物理・業務モデル編集"]')).toHaveCount(0);
  await expect(page.getByText("Inspector", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Draft を保存", exact: true })).toHaveCount(0);
  await expect(markdown.getByRole("button", { name: "Markdown 下書きを保存" })).toBeVisible();
  await expect(markdown.getByRole("tab")).toHaveCount(2);
  await markdown.getByRole("tab", { name: "Markdown オントロジー下書き" }).click();
  await expect(draftEditor).toHaveValue(/## 物理オブジェクト/);
  await expect(draftEditor).toHaveValue(/## 業務ルール \/ 列挙値/);
  await expect(page.getByTestId("ontology-build-mermaid")).toHaveCount(0);
  await expect(section).toBeVisible();

  // 横スクロールが発生しない
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth
  );
  expect(overflow).toBe(false);

  await ontologyQueryPanel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("ontology-build.png"), fullPage: true });
});

test("オントロジー構築の保存済みファイルは選択プロファイルごとに切り替わる", async ({ page }) => {
  const state = await mockProfileScopedApi(page);

  await page.goto("/ontology-build?profile=sales");

  await expectSelectFieldValue(page.getByTestId("ontology-build-profile-select"), "sales");
  await expect(page.locator("#ontology-workspace-not-loaded")).toBeVisible();
  await expect(page.getByTestId("profile-ontology-build")).toHaveCount(0);
  expect(state.profileDetailCalls).toEqual([]);
  expect(state.ontologyViewCalls).toEqual([]);
  expect(state.buildJobProfileIds).toEqual([]);
  expect(state.sourceDocumentProfileIds).toEqual([]);
  expect(state.markdownProfileIds).toEqual([]);
  await loadOntologyBuildWorkspace(page);
  await expect(page.getByTestId("ontology-build-profile-scope")).toContainText(
    "対象: 販売分析（営業）"
  );
  const savedFiles = page.getByTestId("ontology-build-saved-files");
  await expect(savedFiles).toContainText(
    "販売分析（営業）で過去にオントロジー構築へ使用したファイルです。"
  );
  await expect(savedFiles.getByText("sales-rules.md", { exact: true })).toBeVisible();
  await expect(savedFiles.getByText("finance-rules.xlsx", { exact: true })).toHaveCount(0);

  await chooseSelectFieldOption(page.getByTestId("ontology-build-profile-select"), "finance");

  await expect(page).toHaveURL(/profile=finance/u);
  await expect(page.locator("#ontology-workspace-not-loaded")).toBeVisible();
  await expect(page.getByTestId("profile-ontology-build")).toHaveCount(0);
  await loadOntologyBuildWorkspace(page);
  await expect(page.getByTestId("ontology-build-profile-scope")).toContainText(
    "対象: 経理分析（経理）"
  );
  await expect(savedFiles).toContainText(
    "経理分析（経理）で過去にオントロジー構築へ使用したファイルです。"
  );
  await expect(savedFiles.getByText("finance-rules.xlsx", { exact: true })).toBeVisible();
  await expect(savedFiles.getByText("sales-rules.md", { exact: true })).toHaveCount(0);
  expect(state.sourceDocumentProfileIds).toEqual(expect.arrayContaining(["sales", "finance"]));
});

test("オントロジー構築の保存済みファイルは確認付きで削除できる", async ({ page }) => {
  const state = await mockProfileScopedApi(page);

  await page.goto("/ontology-build?profile=sales");
  await loadOntologyBuildWorkspace(page);

  const savedFiles = page.getByTestId("ontology-build-saved-files");
  await expect(savedFiles.getByText("sales-rules.md", { exact: true })).toBeVisible();
  const deleteButton = savedFiles.getByRole("button", {
    name: "sales-rules.md を保存済みファイルから削除",
  });
  await expect(deleteButton).toBeVisible();

  await deleteButton.click();
  const dialog = page.getByRole("alertdialog", {
    name: "保存済みファイルを削除しますか",
  });
  await expect(dialog).toContainText("sales-rules.md");
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  expect(state.deletedSourceIds).toEqual([]);
  await expect(savedFiles.getByText("sales-rules.md", { exact: true })).toBeVisible();

  await deleteButton.click();
  await page
    .getByRole("alertdialog", { name: "保存済みファイルを削除しますか" })
    .getByRole("button", { name: "削除" })
    .click();

  await expect.poll(() => state.deletedSourceIds).toEqual(["sales:source-sales"]);
  await expect(savedFiles.getByText("sales-rules.md", { exact: true })).toHaveCount(0);
  await expect(savedFiles.getByTestId("ontology-build-saved-files-empty")).toBeVisible();
  await expect(
    page.getByText("「sales-rules.md」を保存済みファイルから削除しました。")
  ).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth
  );
  expect(overflow).toBe(false);
});

test("公開完了後も下書き版を前の version のまま保持する", async ({ page }) => {
  const state = await mockApi(page);
  state.jobPolls = 2;
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(
      route,
      state.published
        ? {
            draft_markdown: state.draftMarkdown,
            published_markdown: state.publishedMarkdown || state.draftMarkdown,
            draft_revision: {
              id: "revision-draft-4",
              version: 4,
              status: "published",
              schema_fingerprint: "fp",
              etag: "draft-etag-4",
              published_at: "2026-07-12T00:00:20Z",
            },
            published_revision: {
              id: "revision-draft-4",
              version: 4,
              status: "published",
              schema_fingerprint: "fp",
              etag: "draft-etag-4",
              published_at: "2026-07-12T00:00:20Z",
            },
            draft_etag: state.draftMarkdownEtag,
            published_at: "2026-07-12T00:00:20Z",
          }
        : markdownDraftPayload(state.draftMarkdown)
    )
  );

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const markdown = page.getByTestId("ontology-build-markdown");
  const draftEditor = markdown.getByTestId("ontology-markdown-draft-editor");
  await expect(draftEditor).toHaveValue(/# オントロジー下書き/);
  await draftEditor.fill(`${generatedDraftMarkdown}\n\n## 手動メモ\n- 公開確認済み`);

  await markdown
    .getByTestId("ontology-publish-actions")
    .getByRole("button", { name: "オントロジーを公開" })
    .click();
  await confirmPreparedPublish(page);

  await expect(page.getByText("オントロジーを公開しました。")).toBeVisible();
  await expect(page.getByTestId("ontology-publish-status")).toContainText("完了");
  await expect(markdown.getByTestId("ontology-markdown-tab-published-meta")).toHaveText("v4");

  await markdown.getByRole("tab", { name: "公開済み Markdown オントロジー" }).click();
  await expect(markdown.getByTestId("ontology-markdown-published-viewer")).toContainText(
    "手動メモ"
  );
  await expect(markdown.getByTestId("ontology-markdown-published-meta")).toContainText(
    /公開日時: 07\/12 \d{2}:00/u
  );

  await markdown.getByRole("tab", { name: "Markdown オントロジー下書き" }).click();
  await expect(markdown.getByTestId("ontology-markdown-tab-draft-meta")).toHaveText("v4");
  await expect(markdown.getByTestId("ontology-markdown-draft-editor")).toHaveValue(/手動メモ/);
  await expect(markdown.getByTestId("ontology-markdown-draft-editor")).toHaveAttribute(
    "readonly",
    ""
  );
  await expect(
    markdown
      .getByTestId("ontology-publish-actions")
      .getByRole("button", { name: "オントロジーを公開" })
  ).toBeDisabled();
});

test("公開直後の Markdown 再読込が一時失敗しても公開済み内容を保持する", async ({ page }) => {
  const state = await mockApi(page);
  state.jobPolls = 2;
  let failFirstPublishedRefresh = true;
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) => {
    if (state.published && failFirstPublishedRefresh) {
      failFirstPublishedRefresh = false;
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ data: null, error_messages: ["一時的な接続エラー"] }),
      });
    }
    return fulfillJson(
      route,
      state.published
        ? {
            draft_markdown: state.draftMarkdown,
            published_markdown: state.publishedMarkdown || state.draftMarkdown,
            draft_revision: {
              id: "revision-draft-4",
              version: 4,
              status: "published",
              schema_fingerprint: "fp",
              etag: "draft-etag-4",
              published_at: "2026-07-12T00:00:20Z",
            },
            published_revision: {
              id: "revision-draft-4",
              version: 4,
              status: "published",
              schema_fingerprint: "fp",
              etag: "draft-etag-4",
              published_at: "2026-07-12T00:00:20Z",
            },
            draft_etag: state.draftMarkdownEtag,
            published_at: "2026-07-12T00:00:20Z",
          }
        : markdownDraftPayload(state.draftMarkdown)
    );
  });

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const markdown = page.getByTestId("ontology-build-markdown");
  const draftEditor = markdown.getByTestId("ontology-markdown-draft-editor");
  await expect(draftEditor).toHaveValue(/# オントロジー下書き/);
  await draftEditor.fill(
    `${generatedDraftMarkdown}\n\n## 手動メモ\n- 公開直後の再読込に失敗しても保持`
  );

  await markdown
    .getByTestId("ontology-publish-actions")
    .getByRole("button", { name: "オントロジーを公開" })
    .click();
  await confirmPreparedPublish(page);

  await expect(page.getByText("オントロジーを公開しました。")).toBeVisible();
  await expect(markdown.getByText("Markdown オントロジーを読み込めませんでした。")).toHaveCount(0);
  await expect(markdown.getByTestId("ontology-markdown-tab-published-meta")).toHaveText("v4");
  await expect(markdown.getByRole("tab", { name: "公開済み Markdown オントロジー" })).toHaveAttribute(
    "aria-selected",
    "true"
  );
  await expect(markdown.getByTestId("ontology-markdown-published-viewer")).toContainText(
    "公開直後の再読込に失敗しても保持"
  );
  await markdown.getByRole("tab", { name: "Markdown オントロジー下書き" }).click();
  await expect(markdown.getByTestId("ontology-markdown-tab-draft-meta")).toHaveText("v4");
  await expect(markdown.getByTestId("ontology-markdown-draft-editor")).toHaveValue(
    /公開直後の再読込に失敗しても保持/
  );
  await expect(markdown.getByTestId("ontology-markdown-draft-editor")).toHaveAttribute(
    "readonly",
    ""
  );
});

test("オントロジー構築の処理状況は折りたたみでき、再実行で自動展開する", async ({ page }) => {
  const state = await mockApi(page);
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const section = page.getByTestId("profile-ontology-build");
  await section.getByLabel("業務説明(自然言語)").fill("受注は顧客に紐づく。");
  await section.getByRole("button", { name: "AI 構築を実行" }).click();

  const steps = page.getByTestId("ontology-build-steps");
  const toggle = page.getByTestId("ontology-build-progress-toggle");
  await expect(steps.getByText("範囲・資料の準備")).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(steps.getByText("オントロジー構築の処理状況")).toBeVisible();
  await expect(steps.getByText("範囲・資料の準備")).toBeHidden();

  await expect(steps).toHaveAttribute("data-job-status", "succeeded", { timeout: 15000 });
  await section.getByRole("button", { name: "AI 構築を実行" }).click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(steps.getByText("範囲・資料の準備")).toBeVisible();
  expect(state.startCalls).toBe(2);

  await page.setViewportSize({ width: 375, height: 812 });
  const mobileOverflow = await steps.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  expect(mobileOverflow.scrollWidth).toBeLessThanOrEqual(mobileOverflow.clientWidth + 1);
});

test("実行する抽出 UI は表示せず、入力有無から抽出対象を自動判定する", async ({ page }) => {
  test.setTimeout(60_000);
  const state = await mockApi(page);
  const expectedSchemaOnly = {
    runSchemaNaming: true,
    runQaExtraction: false,
    runTextExtraction: false,
  };

  async function openBuildForm() {
    state.latestRunOptions = null;
    state.jobPolls = 0;
    await page.goto("/ontology-build?profile=default");
    await loadOntologyBuildWorkspace(page);
    const section = page.getByTestId("profile-ontology-build");
    await expect(section.getByRole("heading", { name: "オントロジー構築" })).toBeVisible();
    await expectExtractionTargetsHidden(page);
    await section.getByLabel("業務説明(自然言語)").clear();
    return section;
  }

  async function submitAndExpect(section: Locator, expected: BuildRunOptions) {
    await section.getByRole("button", { name: "AI 構築を実行" }).click();
    await expect.poll(() => state.latestRunOptions).toEqual(expected);
  }

  await test.step("Profile schema のみなら schema naming だけ実行する", async () => {
    const section = await openBuildForm();
    await submitAndExpect(section, expectedSchemaOnly);
  });

  await test.step("業務説明があれば text extraction を実行する", async () => {
    const section = await openBuildForm();
    await section.getByLabel("業務説明(自然言語)").fill("受注は顧客に紐づく。");
    await submitAndExpect(section, {
      ...expectedSchemaOnly,
      runTextExtraction: true,
    });
  });

  await test.step("構築資料があれば text extraction を実行する", async () => {
    const section = await openBuildForm();
    await dropFiles(page, page.getByTestId("ontology-build-source-files-dropzone"), [
      {
        name: "rules.md",
        type: "text/markdown",
        content: "# 受注ルール",
      },
    ]);
    await submitAndExpect(section, {
      ...expectedSchemaOnly,
      runTextExtraction: true,
    });
  });

  await test.step("Q/A ファイルがあれば QA extraction を実行する", async () => {
    const section = await openBuildForm();
    await dropFiles(page, page.getByTestId("ontology-build-qa-file-dropzone"), [
      {
        name: "qa_cases.csv",
        type: "text/csv",
        content: "QUESTION,SQL\n受注件数は,SELECT COUNT(*) FROM ORDERS",
      },
    ]);
    await submitAndExpect(section, {
      ...expectedSchemaOnly,
      runQaExtraction: true,
    });
  });

  await test.step("すべて入力されていればすべて実行する", async () => {
    const section = await openBuildForm();
    await section.getByLabel("業務説明(自然言語)").fill("売上は確定済み受注の受注金額の合計。");
    await dropFiles(page, page.getByTestId("ontology-build-source-files-dropzone"), [
      {
        name: "rules.md",
        type: "text/markdown",
        content: "# 受注ルール",
      },
    ]);
    await dropFiles(page, page.getByTestId("ontology-build-qa-file-dropzone"), [
      {
        name: "qa_cases.csv",
        type: "text/csv",
        content: "QUESTION,SQL\n売上は,SELECT SUM(AMOUNT) FROM ORDERS",
      },
    ]);
    await submitAndExpect(section, {
      runSchemaNaming: true,
      runQaExtraction: true,
      runTextExtraction: true,
    });
  });
});

test("構築資料の上限 5 件は Q/A ファイルを含む総数で判定する", async ({ page }) => {
  const state = await mockApi(page);
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  const section = page.getByTestId("profile-ontology-build");
  const sourceDropzone = page.getByTestId("ontology-build-source-files-dropzone");
  const qaDropzone = page.getByTestId("ontology-build-qa-file-dropzone");
  const sourceFileList = section.getByRole("list", { name: "選択した構築資料" });
  const limitMessage =
    "構築資料は Q/A ファイルと合わせて最大 5 件までアップロードできます。ファイルを減らして再度選択してください。";
  const source = (index: number) => ({
    name: `source-${index}.md`,
    type: "text/markdown",
    content: `# 資料 ${index}`,
    lastModified: 1_700_000_020_000 + index,
  });
  const qa = { name: "qa_cases.csv", type: "text/csv", content: "QUESTION,SQL\n受注件数は,SELECT COUNT(*) FROM ORDERS" };

  // 構築資料 4 件 + Q/A 1 件 = 5 件は選択できる
  await dropFiles(page, sourceDropzone, [1, 2, 3, 4].map(source));
  await dropFiles(page, qaDropzone, [qa]);
  await expect(section.getByText("選択済み: qa_cases.csv")).toBeVisible();
  // Q/A ありで構築資料を 5 件にすると 6 件になるため拒否する
  await dropFiles(page, sourceDropzone, [source(5)]);
  await expect(page.getByTestId("ontology-build-source-files").getByText(limitMessage, { exact: true })).toBeVisible();
  await expect(sourceFileList.getByRole("listitem")).toHaveCount(4);

  // 構築資料 5 件のときは Q/A ファイルの選択も同じ上限で拒否する
  await page.getByTestId("ontology-build-qa-file").getByRole("button", { name: "ファイル選択を解除" }).click();
  await dropFiles(page, sourceDropzone, [source(5)]);
  await expect(sourceFileList.getByRole("listitem")).toHaveCount(5);
  await dropFiles(page, qaDropzone, [qa]);
  await expect(page.getByTestId("ontology-build-qa-file").getByText(limitMessage, { exact: true })).toBeVisible();
  await expect(section.getByText("選択済み: qa_cases.csv")).toHaveCount(0);

  // 構築資料 5 件だけなら backend と同じく受け付ける
  await section.getByRole("button", { name: "AI 構築を実行" }).click();
  await expect.poll(() => state.latestRunOptions).toEqual({
    runSchemaNaming: true,
    runQaExtraction: false,
    runTextExtraction: true,
  });
  expect(state.startCalls).toBe(1);
});

test("送信直後にプレースホルダーが出て、完了後は Markdown 下書きを表示する", async ({ page }) => {
  await mockApi(page);
  // POST を遅らせて「送信中」プレースホルダーを観測できるようにする
  await page.route("**/api/nl2sql/profiles/*/ontology-build", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 600));
    await fulfillJson(route, buildJob("queued", "pending"));
  });
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const section = page.getByTestId("profile-ontology-build");
  await section.getByRole("button", { name: "AI 構築を実行" }).click();
  const submitting = page.getByTestId("ontology-build-submitting");
  await expect(submitting).toBeVisible();
  // 送信中のスピナーはボタン内 1 つに一本化(ステータス帯に二重表示しない)。
  await expect(submitting.locator(".animate-spin")).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-steps")).toBeVisible({ timeout: 15000 });

  const markdown = page.getByTestId("ontology-build-markdown");
  await expect(markdown.getByTestId("ontology-markdown-draft-editor")).toHaveValue(
    /# オントロジー下書き/,
    { timeout: 15000 }
  );
  await expect(page.getByTestId("ontology-build-proposals")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /すべて承認/ })).toHaveCount(0);
});

test("下書き成果物の保存後は job 完了前でも Markdown 下書きを表示する", async ({ page }) => {
  await mockApi(page);
  let artifactSaved = false;
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(route, artifactSaved ? markdownDraftPayload(generatedDraftMarkdown) : emptyMarkdownPayload())
  );
  await page.unroute("**/api/nl2sql/ontology-build/*");
  await page.route("**/api/nl2sql/ontology-build/*", (route) => {
    artifactSaved = true;
    const running = buildJob("running", "running").job;
    running.steps = running.steps.map((step) =>
      step.name === "proposal_registration"
        ? {
            ...step,
            status: "running",
            detail_ja: "下書き revision v4 を保存しました。Markdown 成果物を保存しました。",
            started_at: "2026-07-12T00:00:03Z",
            finished_at: null,
          }
        : step
    );
    running.events = [
      ...running.events,
      { at: "2026-07-12T00:00:04Z", message_ja: "Markdown 成果物を保存しました。" },
    ];
    running.draft_revision_id = "revision-draft-4";
    running.draft_etag = "markdown-etag-1";
    running.markdown_output = generatedDraftMarkdown;
    return fulfillJson(route, { job: running });
  });

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const section = page.getByTestId("profile-ontology-build");
  await expect(section).toBeVisible({ timeout: 20000 });
  await expect(section.getByTestId("ontology-markdown-draft-empty")).toBeVisible();
  await section.getByRole("button", { name: "AI 構築を実行" }).click();

  await expect(page.getByTestId("ontology-build-steps")).toHaveAttribute(
    "data-job-status",
    "running",
    { timeout: 15000 }
  );
  await expect(page.getByTestId("ontology-markdown-draft-editor")).toHaveValue(
    /# オントロジー下書き/,
    { timeout: 15000 }
  );
  await expect(page.getByTestId("ontology-markdown-draft-editor")).toHaveValue(
    /## 関係 \/ Join/
  );
});

test("保存していない下書きがあるときの構築は確認し、新しい下書きができるまで今の下書きを編集させない", async ({ page }) => {
  const state = await mockApi(page);
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(route, markdownDraftPayload(generatedDraftMarkdown))
  );
  // 構築はまだ新しい下書き revision を出していない（draft_revision_id が空）。
  await page.unroute("**/api/nl2sql/ontology-build/*");
  await page.route("**/api/nl2sql/ontology-build/*", (route) =>
    fulfillJson(route, buildJob("running", "running"))
  );
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const markdown = page.getByTestId("ontology-build-markdown");
  const draftEditor = markdown.getByTestId("ontology-markdown-draft-editor");
  await expect(draftEditor).toHaveValue(/# オントロジー下書き/, { timeout: 20000 });
  await expect(draftEditor).toBeEnabled();
  const editedMarkdown = `${generatedDraftMarkdown}\n\n## 手動メモ`;
  await draftEditor.fill(editedMarkdown);
  await expect(markdown.getByText("未保存")).toBeVisible();

  const section = page.getByTestId("profile-ontology-build");
  const runButton = section.getByRole("button", { name: "AI 構築を実行" });
  await runButton.click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("保存していない下書きの変更があります");
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.startCalls).toBe(0);
  await expect(draftEditor).toHaveValue(editedMarkdown);

  await runButton.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "変更を破棄して構築" }).click();
  await expect.poll(() => state.startCalls).toBe(1);
  await expect(page.getByTestId("ontology-build-steps")).toHaveAttribute(
    "data-job-status",
    "running",
    { timeout: 15000 }
  );
  // 構築の結果が下書きを置き換えるまで、今の下書きは編集・保存できない。理由を欄の下に出す。
  await expect(draftEditor).toBeDisabled();
  await expect(markdown.getByText("AI 構築が新しい下書きを作るまで、今の下書きは編集できません。", { exact: false })).toBeVisible();
  await expect(markdown.getByRole("button", { name: "Markdown 下書きを保存" })).toBeDisabled();
});

test("Markdown 下書き保存後は stale refresh でエディタ値を戻さない", async ({ page }) => {
  await mockApi(page);
  let saved = false;
  let markdownReadsAfterSave = 0;
  let buildPolls = 0;
  await page.unroute("**/api/nl2sql/profiles/*/ontology-build-jobs**");
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) => {
    const running = buildJob("running", "running").job;
    running.draft_revision_id = "revision-draft-4";
    running.draft_etag = "markdown-etag-1";
    return fulfillJson(route, { jobs: [running] });
  });
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) => {
    if (saved) {
      markdownReadsAfterSave += 1;
      return fulfillJson(route, emptyMarkdownPayload());
    }
    return fulfillJson(route, markdownDraftPayload(generatedDraftMarkdown));
  });
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown/draft");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/draft", (route) => {
    const body = route.request().postDataJSON() as { markdown?: string };
    saved = true;
    return fulfillJson(route, markdownDraftPayload(body.markdown ?? "", "markdown-etag-2"));
  });
  await page.unroute("**/api/nl2sql/ontology-build/*");
  await page.route("**/api/nl2sql/ontology-build/*", (route) => {
    buildPolls += 1;
    const running = buildJob("running", "running").job;
    const signal = saved
      ? `Markdown 成果物を保存しました。stale refresh ${buildPolls}`
      : "Markdown 成果物を保存しました。";
    running.steps = running.steps.map((step) =>
      step.name === "proposal_registration"
        ? {
            ...step,
            status: "running",
            detail_ja: signal,
            started_at: "2026-07-12T00:00:03Z",
            finished_at: null,
          }
        : step
    );
    running.events = [
      ...running.events,
      { at: "2026-07-12T00:00:04Z", message_ja: signal },
    ];
    running.draft_revision_id = "revision-draft-4";
    running.draft_etag = saved ? `markdown-etag-stale-${buildPolls}` : "markdown-etag-1";
    return fulfillJson(route, { job: running });
  });
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const markdown = page.getByTestId("ontology-build-markdown");
  const draftEditor = markdown.getByTestId("ontology-markdown-draft-editor");
  await expect(markdown).toBeVisible({ timeout: 20000 });
  await expect(draftEditor).toHaveValue(/# オントロジー下書き/, { timeout: 20000 });
  await expect(draftEditor).toBeEnabled({ timeout: 20000 });
  const savedMarkdown = `${generatedDraftMarkdown}\n\n## 手動メモ\n- 保存後も保持`;
  await draftEditor.fill(savedMarkdown);
  await expect(markdown.getByText("未保存")).toBeVisible();
  await markdown.getByRole("button", { name: "Markdown 下書きを保存" }).click();

  await expect(page.getByText("Markdown 下書きを保存しました。")).toBeVisible();
  await expect(draftEditor).toHaveValue(savedMarkdown);
  await expect(markdown.getByText("未保存")).toHaveCount(0);
  await expect.poll(() => markdownReadsAfterSave).toBeGreaterThan(0);
  await expect(draftEditor).toHaveValue(savedMarkdown);
});

test("Profile の一覧読込と情報取得後の workspace/Markdown 読込では loading を表示する", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-07-29T00:00:00.000Z") });
  await mockApi(page);
  const profilesGate = createRequestGate();
  const ontologyViewGate = createRequestGate();
  const markdownGate = createRequestGate();
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", async (route) => {
    await profilesGate.promise;
    await fulfillJson(route, {
      items: profiles.map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: profile.archived,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: 0,
        few_shot_count: 0,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-07-12T00:00:00Z",
      })),
      next_cursor: null,
      total: 1,
      change_token: 1,
    });
  });
  await page.unroute("**/api/nl2sql/profiles/*/ontology-view");
  await page.route("**/api/nl2sql/profiles/*/ontology-view", async (route) => {
    await ontologyViewGate.promise;
    await fulfillJson(route, {
      ...ontologyView,
      materialized: true,
      stale: false,
    });
  });
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", async (route) => {
    await markdownGate.promise;
    await fulfillJson(route, {
      draft_markdown: "",
      published_markdown: "",
      draft_revision: null,
      published_revision: null,
      draft_etag: "",
      published_at: null,
    });
  });

  await page.goto("/ontology-build?profile=default");
  await expect(page.getByTestId("ontology-profile-compact-skeleton")).toBeVisible();
  profilesGate.release();
  await expect(page.getByTestId("ontology-build-profile-select")).toBeVisible();
  await expect(page.locator("#ontology-workspace-not-loaded")).toBeVisible();
  await expect(page.getByTestId("profile-ontology-build")).toHaveCount(0);
  await expect(page.getByTestId("ontology-markdown-loading")).toHaveCount(0);

  await page.getByTestId("ontology-view-fetch").click();
  const workspaceSkeleton = page.getByTestId("ontology-workspace-loading");
  await expect(workspaceSkeleton).toBeVisible();
  await expect(workspaceSkeleton).toHaveAttribute("data-processing-placement", "panel");
  await expect(workspaceSkeleton.getByRole("timer")).toHaveAccessibleName("経過時間 00:00");
  // スピナーは押した「オントロジーを取得」ボタンの 1 つだけ。読込の表示は経過時間と Skeleton（#416）。
  await expect(visibleSpinners(workspaceSkeleton)).toHaveCount(0);
  await expectSingleSpinner(page, page.getByTestId("ontology-view-fetch"));
  await expect(workspaceSkeleton.getByTestId("db-management-skeleton-block")).toHaveCount(3);
  await expect(page.getByTestId("profile-ontology-build")).toHaveCount(0);
  ontologyViewGate.release();

  const skeleton = page.getByTestId("ontology-markdown-loading");
  await expect(skeleton).toBeVisible();
  await expect(skeleton).toHaveAttribute("data-processing-placement", "panel");
  await expect(skeleton.getByRole("timer")).toHaveAccessibleName("経過時間 00:00");
  // 取得が終わった後の Markdown の読込は、読込の表示がスピナーを 1 つ出す（#416）。
  await expectSingleSpinner(page, skeleton);
  await expect(skeleton.getByTestId("db-management-skeleton-block")).toHaveCount(3);
  await expect(page.getByTestId("ontology-markdown-draft-editor")).toHaveCount(0);
  await expect(page.getByTestId("ontology-publish-actions")).toHaveCount(0);

  await page.clock.fastForward(11_000);
  await expect(skeleton.getByRole("timer")).toHaveAccessibleName("経過時間 00:11");
  await expect(skeleton).toContainText("通常より時間がかかっています");
  await expect(skeleton).not.toContainText(
    "このままお待ちいただくか、取消可能な処理はキャンセルできます。",
  );

  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
        document.body.scrollWidth > document.body.clientWidth + 1,
    ),
  ).toBe(false);

  markdownGate.release();
  await expect(page.getByTestId("ontology-markdown-draft-empty")).toBeVisible();
});

test("Markdown オントロジーの読込はキャンセル操作を表示せず完了時に内容を表示する", async ({ page }, testInfo) => {
  await page.clock.install({ time: new Date("2026-09-12T00:00:00Z") });
  await mockApi(page);
  const markdownGate = createRequestGate();
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", async (route) => {
    await markdownGate.promise;
    await fulfillJson(route, markdownDraftPayload(generatedDraftMarkdown));
  });

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  const skeleton = page.getByTestId("ontology-markdown-loading");
  await expect(skeleton).toBeVisible();
  await expect(skeleton.getByRole("button", { name: "キャンセル" })).toHaveCount(0);
  await page.clock.fastForward(21_000);
  await expect(skeleton.getByRole("timer")).toHaveAccessibleName("経過時間 00:21");
  await expect(skeleton).toContainText("通常より時間がかかっています");
  await expect(skeleton.getByRole("button", { name: "キャンセル" })).toHaveCount(0);
  await skeleton.screenshot({ path: testInfo.outputPath("markdown-loading-without-cancel.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  markdownGate.release();
  await expect(skeleton).toHaveCount(0);
  await expect(page.getByTestId("ontology-markdown-draft-editor")).toHaveValue(generatedDraftMarkdown);
  await expect(page.getByText("Markdown オントロジーを読み込めませんでした。")).toHaveCount(0);
});

test("Profile の読込失敗から再試行できる", async ({ page }) => {
  // 失敗の表示までの TanStack Query の再試行（3 回）を page.clock で進める（#1593）。
  await page.clock.install();
  await mockApi(page);
  let allowProfiles = false;
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", async (route) => {
    if (!allowProfiles) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ data: null, error_messages: ["一時的な接続エラー"] }),
      });
      return;
    }
    await fulfillJson(route, {
      items: profiles.map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: profile.archived,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: 0,
        few_shot_count: 0,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-07-12T00:00:00Z",
      })),
      next_cursor: null,
      total: 1,
      change_token: 1,
    });
  });
  await page.goto("/ontology-build?profile=default");
  const profilePanel = page.getByRole("region", { name: "対象プロファイル" });
  await pollWithClock(page, () => profilePanel.getByText("プロファイルの読込に失敗しました。").count(), 2_000).toBe(1);
  allowProfiles = true;
  await profilePanel.getByRole("button", { name: "再試行" }).click();
  await expect(page.getByTestId("ontology-build-profile-select")).toBeVisible();
});

test("Markdown オントロジーの読込失敗から再試行できる", async ({ page }) => {
  await mockApi(page);
  let allowMarkdown = false;
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", async (route) => {
    if (!allowMarkdown) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ data: null, error_messages: ["一時的な接続エラー"] }),
      });
      return;
    }
    await fulfillJson(route, {
      draft_markdown: "",
      published_markdown: "",
      draft_revision: null,
      published_revision: null,
      draft_etag: "",
      published_at: null,
    });
  });

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const markdownPanel = page.getByTestId("ontology-build-markdown");
  await expect(markdownPanel.getByText("Markdown オントロジーを読み込めませんでした。")).toBeVisible();
  allowMarkdown = true;
  await markdownPanel.getByRole("button", { name: "再試行" }).click();
  await expect(page.getByTestId("ontology-markdown-draft-empty")).toBeVisible();
});

test("AI 提案レビュー UI は表示せず Markdown タブだけを表示する", async ({ page }) => {
  await mockApi(page);
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  await expect(page.getByTestId("ontology-build-proposals")).toHaveCount(0);
  await expect(page.getByText("AI 提案のレビュー")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /すべて承認/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "承認", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "却下", exact: true })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Markdown オントロジー下書き" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "公開済み Markdown オントロジー" })).toBeVisible();
});

test("Markdown オントロジー tabs はキーボードで切り替えできる", async ({ page }) => {
  await mockApi(page);
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const draftTab = page.getByRole("tab", { name: "Markdown オントロジー下書き" });
  const publishedTab = page.getByRole("tab", { name: "公開済み Markdown オントロジー" });
  await expect(draftTab).toHaveAttribute("aria-selected", "true");
  await draftTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(publishedTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("ontology-markdown-published-viewer")).toBeVisible();
  await page.keyboard.press("Home");
  await expect(draftTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("ontology-markdown-draft-empty")).toBeVisible();
});

test("Markdown オントロジー tabs は profile 別 version を優先して表示する", async ({ page }) => {
  await mockApi(page);
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(route, {
      draft_markdown: generatedDraftMarkdown,
      published_markdown: "",
      draft_revision: {
        id: "revision-draft-4",
        version: 4,
        status: "draft",
        schema_fingerprint: "fp",
        etag: "draft-etag-4",
      },
      published_revision: null,
      draft_version: 1,
      published_version: null,
      draft_etag: "markdown-etag-1",
      published_at: null,
    })
  );

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const markdown = page.getByTestId("ontology-build-markdown");
  await expect(markdown.getByTestId("ontology-markdown-tab-draft-meta")).toHaveText("v1");
  await expect(markdown.getByTestId("ontology-markdown-tab-published-meta")).toHaveText("未公開");
});

test("未公開 Markdown 下書きはリロード後も公開ボタンが表示される", async ({ page }) => {
  await mockApi(page);
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(route, {
      draft_markdown: generatedDraftMarkdown,
      published_markdown: "",
      draft_revision: {
          id: "revision-draft-4",
          version: 4,
          status: "draft",
          schema_fingerprint: "fp",
          etag: "draft-etag-4",
      },
      published_revision: null,
      draft_etag: "markdown-etag-1",
      published_at: null,
    })
  );

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const markdown = page.getByTestId("ontology-build-markdown");
  await expect(markdown.getByTestId("ontology-markdown-tab-draft-meta")).toHaveText("v4");
  await expect(markdown.getByTestId("ontology-markdown-tab-published-meta")).toHaveText("未公開");
  await expect(page.getByTestId("ontology-publish-actions").getByText("下書き v4")).toHaveCount(0);
  await expect(page.getByTestId("ontology-publish-actions").getByRole("button", { name: "オントロジーを公開" })).toBeVisible();
});

test("SHACL Violation で公開を止め、修正後の再公開で復旧できる", async ({ page }) => {
  await mockApi(page);
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(route, {
      draft_markdown: generatedDraftMarkdown,
      published_markdown: "",
      draft_revision: {
          id: "revision-draft-4",
          version: 4,
          status: "draft",
          schema_fingerprint: "fp",
          etag: "draft-etag-4",
      },
      published_revision: null,
      draft_etag: "markdown-etag-1",
      published_at: null,
    })
  );
  let publishAttempt = 0;
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/publish", (route) => {
    publishAttempt += 1;
    return fulfillJson(route, {
      job: {
        id: `publish-recovery-${publishAttempt}`,
        revision_id: "revision-draft-4",
        requested_etag: "draft-etag-4",
        status: "queued",
      },
    });
  });
  await page.route("**/api/nl2sql/ontology-publish/*", (route) =>
    fulfillJson(route, {
      job: {
        id: `publish-recovery-${publishAttempt}`,
        revision_id: "revision-draft-4",
        requested_etag: "draft-etag-4",
        status: publishAttempt === 1 ? "failed" : "succeeded",
        shacl_conforms: publishAttempt === 1 ? false : true,
        error_code: publishAttempt === 1 ? "ONTOLOGY_SHACL_VIOLATION" : "",
        error_message_ja:
          publishAttempt === 1
            ? "SHACL Core の Violation があるため公開を中止しました。"
            : "",
      },
    })
  );

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  const publish = page
    .getByTestId("ontology-publish-actions")
    .getByRole("button", { name: "オントロジーを公開" });
  await publish.click();
  await confirmPreparedPublish(page);
  await expect(
    page.getByText("SHACL Core の Violation があるため公開を中止しました。")
  ).toBeVisible();
  await expect(publish).toBeEnabled();

  await publish.click();
  await confirmPreparedPublish(page);
  await expect(page.getByText("オントロジーを公開しました。")).toBeVisible();
  expect(publishAttempt).toBe(2);
});

test("公開ポーリングは一時エラー後も進行状態を維持して復旧する", async ({ page }) => {
  await mockApi(page);
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(route, markdownDraftPayload(generatedDraftMarkdown))
  );
  await page.unroute("**/api/nl2sql/ontology-publish/*");
  let publishPolls = 0;
  await page.route("**/api/nl2sql/ontology-publish/*", (route) => {
    publishPolls += 1;
    if (publishPolls === 1) {
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error_messages: ["一時的な publish poll failure"] }),
      });
    }
    return fulfillJson(route, {
      job: {
        id: "publish-job-1",
        revision_id: "revision-draft-4",
        requested_etag: "draft-etag-4",
        status: "succeeded",
        rdf_graph_name: "ONT_0123456789ABCDEF",
        inferred_graph_name: "INF_0123456789ABCDEF",
        shacl_conforms: true,
      },
    });
  });

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  const publish = page
    .getByTestId("ontology-publish-actions")
    .getByRole("button", { name: "オントロジーを公開" });
  await publish.click();
  await confirmPreparedPublish(page);

  const status = page.getByTestId("ontology-publish-status");
  await expect(status).toContainText("待機中");
  await expect(status).toContainText("公開完了", { timeout: 7000 });
  await expect(page.getByText("オントロジーを公開しました。")).toBeVisible();
  expect(publishPolls).toBeGreaterThanOrEqual(2);
});

test("job 取得が 404 のときポーリングを停止しエラー表示で実行ボタンが復帰する", async ({
  page,
}) => {
  await mockApi(page);
  // job がサーバ再起動等で消えたケース(後勝ちで 404 に上書き)
  let polls = 0;
  await page.route("**/api/nl2sql/ontology-build/*", async (route) => {
    polls += 1;
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: ["AI オントロジー構築 job が見つかりません。"],
      }),
    });
  });
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const section = page.getByTestId("profile-ontology-build");
  await section.getByRole("button", { name: "AI 構築を実行" }).click();
  await expect(
    section.getByText("構築ジョブの状態を取得できませんでした", { exact: false })
  ).toBeVisible({ timeout: 15000 });
  // スピナーが解除され再実行できる
  const runButton = section.getByRole("button", { name: "AI 構築を実行" });
  await expect(runButton).toBeEnabled();
  // ステップ表示は消える(実体の無い job の進捗を残さない)
  await expect(page.getByTestId("ontology-build-steps")).toHaveCount(0);
  // ポーリングが停止している(追加の GET が発生しない)
  const stopped = polls;
  await page.waitForTimeout(2500);
  expect(polls).toBe(stopped);
});

test("job 取得が連続失敗しても長時間猶予内は監視を継続する", async ({ page }) => {
  // 1 秒ごとのポーリングを page.clock で進め、実時間で 10 秒待たない（#1593）。
  await page.clock.install();
  await mockApi(page);
  let polls = 0;
  await page.route("**/api/nl2sql/ontology-build/*", async (route) => {
    polls += 1;
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ data: null, error_messages: ["内部エラー"] }),
    });
  });
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const section = page.getByTestId("profile-ontology-build");
  await section.getByRole("button", { name: "AI 構築を実行" }).click();
  await expect(
    section.getByText("構築状況の取得に連続して失敗した", { exact: false })
  ).toHaveCount(0);
  await pollWithClock(page, async () => polls, 1_000).toBeGreaterThanOrEqual(8);
  const building = section.getByRole("button", { name: "AI 構築を実行" });
  await expect(building).toBeDisabled();
  // job の間は、開始のボタンは loading にしない。スピナーは進行状況の帯の 1 つだけ（#416）。
  await expect(building).not.toHaveAttribute("aria-busy", "true");
  await expect(page.getByTestId("ontology-build-steps")).toBeVisible();
  await expectSingleSpinner(page, page.getByTestId("ontology-build-steps"));
  const before = polls;
  await page.clock.runFor(2_500);
  await expect.poll(() => polls).toBeGreaterThan(before);
});

test("プロファイルが無いときは案内を表示し AI 構築は出さない", async ({ page }) => {
  await mockApi(page);
  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) => fulfillJson(route, []));
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, { items: [], next_cursor: null, total: 0, change_token: 1 })
  );
  await page.goto("/ontology-build");

  await expect(page.getByText("業務プロファイルがありません")).toBeVisible();
  await expect(page.getByTestId("profile-ontology-build")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "AI 構築を実行" })).toHaveCount(0);
});

test("旧 tab URL を正規化し、モバイルでは単一ページを縦積みにする", { tag: "@responsive" }, async ({ page }) => {
  await mockApi(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/ontology-build?profile=default&tab=usage&legacy=1");

  await expect(page).toHaveURL(/\/ontology-build\?profile=default$/);
  await loadOntologyBuildWorkspace(page);
  await expect(page.getByTestId("ontology-build-markdown").getByRole("tab")).toHaveCount(2);
  await expect(page.getByTestId("ontology-mermaid-panel")).toHaveCount(0);
  await expect(page.getByText("利用・コンテキスト")).toHaveCount(0);
  await expect(page.getByTestId("profile-ontology-build")).toBeVisible();
  await expect(page.getByTestId("profile-ontology-editor")).toHaveCount(0);
  await expect(page.locator('section[aria-label="物理・業務モデル編集"]')).toHaveCount(0);
  await expect(page.locator("#ontology-query-playground-panel")).toBeVisible();
  await expectBuildPanelsStackedFullWidth(page);
  await expect(page.getByTestId("fixed-split-pane-profile-ontology-editor")).toHaveCount(0);

  await page.getByTestId("ontology-build-profile-select").focus();
  await expect(page.getByTestId("ontology-build-profile-select")).toBeFocused();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth
  );
  expect(overflow).toBe(false);
});

test("オントロジー View の API エラーを表示し、キーボードで再試行して復旧する", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockApi(page);
  await page.unroute("**/api/nl2sql/profiles/*/ontology-view");
  let ontologyViewReads = 0;
  let releaseRetryOntologyView: () => void = () => undefined;
  const retryOntologyViewGate = new Promise<void>((resolve) => {
    releaseRetryOntologyView = resolve;
  });
  let reportRetryOntologyViewStarted: () => void = () => undefined;
  const retryOntologyViewStarted = new Promise<void>((resolve) => {
    reportRetryOntologyViewStarted = resolve;
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-view", async (route) => {
    if (route.request().method() !== "GET") {
      await fulfillJson(route, ontologyView);
      return;
    }
    ontologyViewReads += 1;
    if (ontologyViewReads === 1) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          data: null,
          error_messages: ["プロファイル範囲の準備に失敗しました。"],
        }),
      });
      return;
    }
    if (ontologyViewReads === 2) {
      reportRetryOntologyViewStarted();
      await retryOntologyViewGate;
    }
    await fulfillJson(route, { ...ontologyView, materialized: true, stale: false });
  });

  await page.goto("/ontology-build?profile=default");
  await expect(page.getByText("オントロジー情報は未取得です")).toBeVisible();
  await expectLargeActionButton(page.getByTestId("ontology-view-fetch"));
  expect(ontologyViewReads).toBe(0);
  await page.getByTestId("ontology-view-fetch").focus();
  await page.getByTestId("ontology-view-fetch").press("Enter");

  const alert = page.getByRole("alert");
  await expect(alert).toContainText("プロファイル範囲の準備に失敗しました。");
  await expect(alert).toContainText("「再試行」で再度読み込んでください。");

  const retry = alert.getByRole("button", { name: "再試行" });
  await retry.focus();
  await page.keyboard.press("Enter");

  await retryOntologyViewStarted;
  await expect(page.getByTestId("ontology-workspace-loading")).toBeVisible();
  await expect(alert).toHaveCount(0);
  releaseRetryOntologyView();
  await expect(page.getByTestId("profile-ontology-build")).toBeVisible();
  await expect(page.getByTestId("profile-ontology-editor")).toHaveCount(0);
  await expect(alert).toHaveCount(0);
  expect(ontologyViewReads).toBe(2);
});

test("リロード後も実行中の構築ジョブを復元して進捗を追跡する", async ({ page }) => {
  const state = await mockApi(page);
  // 直近ジョブが実行中(リロード前に開始済みの想定)
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [buildJob("running", "running").job] })
  );
  await page.goto("/ontology-build?profile=default");
  await expect(page.locator("#ontology-workspace-not-loaded")).toBeVisible();
  expect(state.buildJobProfileIds).toEqual([]);
  await loadOntologyBuildWorkspace(page);

  // フォーム送信なしで進捗カードが復元され、ポーリングで完了まで進む
  const steps = page.getByTestId("ontology-build-steps");
  await expect(steps).toContainText("オントロジー構築の処理状況");
  await expect(steps.getByText("スキーマ情報の準備")).toBeVisible();
  const runningStep = page.getByTestId("ontology-build-stage-prepare");
  await expect(runningStep).toHaveAttribute("data-step-status", "running");
  await expect(runningStep).toHaveAttribute("aria-current", "step");
  await expect(runningStep.locator("svg.animate-spin").first()).toBeVisible();
  await expectSingleSpinner(page, runningStep);
  await expect(steps.getByRole("timer")).toHaveAttribute("aria-live", "off");
  await expect(page.getByTestId("ontology-build-step-progress")).toBeVisible();
  await page.setViewportSize({ width: 375, height: 812 });
  const mobileOverflow = await steps.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  expect(mobileOverflow.scrollWidth).toBeLessThanOrEqual(mobileOverflow.clientWidth + 1);
  await expect(steps).toHaveAttribute("data-job-status", "succeeded", { timeout: 15000 });
  await steps.getByText("Markdown 下書き生成",{exact:true}).first().click();
  await expect(steps.getByText("Markdown 下書き v4 を生成しました", { exact: false })).toBeVisible();
  await expect(steps.getByText("構築が完了しました", { exact: false })).toHaveCount(0);
  await expect(page.locator('[aria-label="構築ジョブの補足ログ"]')).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-timeline")).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-history")).toHaveCount(0);
  expect(state.jobPolls).toBeGreaterThan(0);
});

test("実行中の構築ジョブを確認ダイアログ経由で中止できる", async ({ page }) => {
  await mockApi(page);
  let cancelCalls = 0;
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [] })
  );
  // ポーリングは実行中のまま(完了させない)
  await page.unroute("**/api/nl2sql/ontology-build/*");
  await page.route("**/api/nl2sql/ontology-build/*", (route) =>
    fulfillJson(route, buildJob("running", "running"))
  );
  await page.route("**/api/nl2sql/ontology-build/*/cancel", (route) => {
    cancelCalls += 1;
    const cancelled = buildJob("cancelled", "skipped");
    cancelled.job.error_message_ja = "利用者の操作で構築を中止しました。";
    return fulfillJson(route, cancelled);
  });
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const section = page.getByTestId("profile-ontology-build");
  await section.getByLabel("業務説明(自然言語)").fill("受注は顧客に紐づく。");
  await section.getByRole("button", { name: "AI 構築を実行" }).click();

  await page.getByTestId("ontology-build-cancel").click();
  await page.getByRole("button", { name: "構築を中止" }).click();

  await expect(page.getByText("構築を中止しました。", { exact: false })).toBeVisible();
  await expect(page.getByTestId("ontology-build-retry")).toHaveCount(0);
  await expect(section.getByRole("button", { name: "AI 構築を実行" })).toBeEnabled();
  expect(cancelCalls).toBe(1);
});

test("中止が 409(既に完了)でもポーリングの完了処理で Markdown を再取得する", async ({ page }) => {
  const state = await mockApi(page);
  let cancelRequested = false;
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [] })
  );
  // 中止を押すまでは実行中、押した後は完了済みを返す(中止より先に job が終わった状況)
  await page.unroute("**/api/nl2sql/ontology-build/*");
  await page.route("**/api/nl2sql/ontology-build/*", (route) =>
    fulfillJson(route, cancelRequested ? buildJob("succeeded", "succeeded") : buildJob("running", "running"))
  );
  await page.route("**/api/nl2sql/ontology-build/*/cancel", (route) => {
    cancelRequested = true;
    return route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "構築は既に終了しています。" }),
    });
  });
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const section = page.getByTestId("profile-ontology-build");
  await section.getByLabel("業務説明(自然言語)").fill("受注は顧客に紐づく。");
  await section.getByRole("button", { name: "AI 構築を実行" }).click();
  await page.getByTestId("ontology-build-cancel").click();
  const markdownLoadsBeforeCancel = state.markdownProfileIds.length;
  await page.getByRole("button", { name: "構築を中止" }).click();

  await expect(page.getByText("AI 構築が完了しました。Markdown 下書きを確認してください。")).toBeVisible();
  await expect.poll(() => state.markdownProfileIds.length).toBeGreaterThan(markdownLoadsBeforeCancel);
  await expect(section.getByRole("button", { name: "AI 構築を実行" })).toBeEnabled();
});

test("Markdown 下書き生成が長時間更新されない場合に警告を表示する", async ({ page }) => {
  await mockApi(page);
  await page.unroute("**/api/nl2sql/ontology-build/*");
  await page.route("**/api/nl2sql/ontology-build/*", (route) => {
    const stale = buildJob("running", "succeeded");
    stale.job.steps = stale.job.steps.map((step) =>
      step.name === "proposal_registration"
        ? {
            ...step,
            status: "running",
            detail_ja: "下書き revision を保存しています…",
            started_at: "2026-07-12T00:00:03Z",
            finished_at: null,
          }
        : step
    );
    stale.job.events = [
      ...stale.job.events,
      { at: "2026-07-12T00:00:03Z", message_ja: "下書き revision を保存しています…" },
    ];
    stale.job.finished_at = null;
    return fulfillJson(route, stale);
  });
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const section = page.getByTestId("profile-ontology-build");
  await section.getByLabel("業務説明(自然言語)").fill("受注は顧客に紐づく。");
  await section.getByRole("button", { name: "AI 構築を実行" }).click();

  await expect(
    page.getByText("60 分以上、Markdown 下書き生成の更新がありません。", { exact: false })
  ).toBeVisible();
  await expect(page.getByTestId("ontology-build-cancel")).toBeVisible();
});

test("完了 job に実行中 step が混在しても Markdown 下書き生成を完了表示に寄せる", async ({ page }) => {
  const state = await mockApi(page);
  await page.unroute("**/api/nl2sql/ontology-build/*");
  await page.route("**/api/nl2sql/ontology-build/*", (route) => {
    state.jobPolls = Math.max(state.jobPolls, 2);
    const inconsistent = buildJob("succeeded", "succeeded");
    inconsistent.job.steps = inconsistent.job.steps.map((step) =>
      step.name === "proposal_registration"
        ? {
            ...step,
            status: "running",
            detail_ja: "構築 job の完了状態を保存しています…",
            started_at: "2026-07-12T00:00:03Z",
            finished_at: null,
          }
        : step
    );
    return fulfillJson(route, inconsistent);
  });
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const section = page.getByTestId("profile-ontology-build");
  await section.getByLabel("業務説明(自然言語)").fill("受注は顧客に紐づく。");
  await section.getByRole("button", { name: "AI 構築を実行" }).click();

  const proposalStep = page.getByTestId("ontology-build-step-proposal_registration");
  await expect(page.getByTestId("ontology-build-steps")).toHaveAttribute(
    "data-job-status",
    "succeeded"
  );
  await expect(page.getByTestId("ontology-build-step-progress")).toContainText("5/5");
  await expect(proposalStep).toHaveAttribute("data-step-status", "succeeded");
  await expect(proposalStep.locator(".animate-spin")).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-cancel")).toHaveCount(0);
});

test("profile scope の schema 解決失敗から DB 構造を再取得できる", async ({ page }) => {
  const state = await mockApi(page);
  const failed = buildJob("failed", "failed");
  failed.job.id = "job-schema-scope-failed";
  failed.job.steps = [
    {
      name: "schema_context",
      status: "failed",
      detail_ja: "profile 範囲に DB 表・ビューがありません。",
      started_at: "2026-07-12T00:00:01Z",
      finished_at: "2026-07-12T00:00:03Z",
    },
    { name: "schema_naming", status: "skipped", detail_ja: "" },
    { name: "proposal_registration", status: "skipped", detail_ja: "" },
  ];
  failed.job.events = [
    {
      at: "2026-07-12T00:00:01Z",
      message_ja: "DB から業務プロファイル範囲のスキーマ情報を取得しています。",
    },
  ];
  failed.job.error_message_ja =
    "業務プロファイルの対象オブジェクトを DB schema catalog に解決できません。DB 構造を再取得するか、業務プロファイルの対象 object を確認してから再実行してください。";
  failed.job.warnings_ja = [
    "「APP.INVOICES」を DB schema catalog の table として解決できません。DB 構造を再取得するか、Profile の対象 object 名(owner 付き)を確認してください。",
  ];
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [failed.job] })
  );
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const steps = page.getByTestId("ontology-build-steps");
  await expect(steps.getByText("業務プロファイルの対象の DB の構造（スキーマ）を読み取れません")).toBeVisible();
  await expect(steps.getByText("DB の構造を取り直してから", { exact: false })).toBeVisible();
  await expect(steps.getByText("公開オントロジー", { exact: false })).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-retry")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "AI 構築を実行" })).toBeEnabled();
  await page.getByTestId("ontology-build-schema-refresh").click();

  await expect(page.getByText("DB 構造を再取得しました。")).toBeVisible();
  expect(state.schemaRefreshCalls).toBe(1);
});

function schemaScopeFailedJob() {
  const failed = buildJob("failed", "failed");
  failed.job.id = "job-schema-scope-failed";
  failed.job.steps = [
    {
      name: "schema_context",
      status: "failed",
      detail_ja: "profile 範囲に DB 表・ビューがありません。",
      started_at: "2026-07-12T00:00:01Z",
      finished_at: "2026-07-12T00:00:03Z",
    },
    { name: "schema_naming", status: "skipped", detail_ja: "" },
    { name: "proposal_registration", status: "skipped", detail_ja: "" },
  ];
  failed.job.error_message_ja =
    "業務プロファイルの対象オブジェクトを DB schema catalog に解決できません。DB 構造を再取得するか、業務プロファイルの対象 object を確認してから再実行してください。";
  failed.job.warnings_ja = [
    "「APP.TABLE_01」を DB schema catalog の table として解決できません。DB 構造を再取得するか、Profile の対象 object 名(owner 付き)を確認してください。",
  ];
  return failed;
}

test("DB の構造を読み取れない失敗の案内は 1 つで、再取得の進行はボタンの直下に出て、終わると「再実行」の案内に替わる（#2034）", { tag: "@responsive" }, async ({ page }) => {
  const state = await mockApi(page);
  const failed = schemaScopeFailedJob();
  let retryCalls = 0;
  let released = false;
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [failed.job] })
  );
  await page.route("**/api/nl2sql/ontology-build/*/retry", (route) => {
    retryCalls += 1;
    return fulfillJson(route, buildJob("queued", "pending"));
  });
  const refreshJob = (status: "pending" | "running" | "done") => ({
    job_id: "ontology-schema-recovery-1",
    status,
    mode: "full",
    phase: status === "done" ? "done" : "scanning",
    created_at: "2026-07-12T00:00:00Z",
    started_at: "2026-07-12T00:00:01Z",
    scanned_objects: status === "done" ? 2 : 0,
    changed_objects: status === "done" ? 1 : 0,
    deleted_objects: 0,
    catalog_version: status === "done" ? 2 : 1,
    error_code: "",
  });
  await page.route("**/api/schema/refresh-jobs", (route) => {
    state.schemaRefreshCalls += 1;
    return fulfillJson(route, refreshJob("pending"));
  });
  await page.route("**/api/schema/refresh-jobs/ontology-schema-recovery-1", (route) =>
    fulfillJson(route, refreshJob(released ? "done" : "running"))
  );
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  // 案内は 1 つ。構築の警告は案内の中にだけ出す（同じ趣旨を 2 か所に出さない）。
  const notice = page.getByTestId("ontology-build-schema-recovery");
  await expect(notice).toHaveCount(1);
  await expect(page.getByText("業務プロファイルの対象の DB の構造（スキーマ）を読み取れません")).toHaveCount(1);
  await expect(page.getByText("「APP.TABLE_01」を DB schema catalog", { exact: false })).toHaveCount(1);
  await expect(notice.getByText("「APP.TABLE_01」を DB schema catalog", { exact: false })).toBeVisible();

  const refreshButton = notice.getByTestId("ontology-build-schema-refresh");
  await refreshButton.focus();
  await page.keyboard.press("Enter");

  // 進行は押したボタンの直下（同じ案内の中）。ページの先頭に同じ進行を重ねず、開始の Toast も出さない。
  const processing = notice.getByTestId("ontology-build-schema-recovery-processing");
  await expect(processing).toBeVisible();
  await expect(processing).toContainText("DB 構造を再取得しています");
  await expect(page.getByTestId("ontology-build-schema-refresh-processing")).toHaveCount(0);
  await expect(page.getByText("DB 構造の再取得を受け付けました。")).toHaveCount(0);
  const buttonBox = await refreshButton.boundingBox();
  const processingBox = await processing.boundingBox();
  expect(buttonBox && processingBox).toBeTruthy();
  expect(processingBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height - 1);
  expect(processingBox!.y - (buttonBox!.y + buttonBox!.height)).toBeLessThan(40);
  await expect(refreshButton).toBeDisabled();

  // 再取得が終わったら、再取得を求める案内を消し、同じ場所に「再実行」へ導く案内を出す。
  released = true;
  await expect(notice).toHaveCount(0);
  const recovered = page.getByTestId("ontology-build-schema-recovered");
  await expect(recovered).toBeVisible();
  await expect(page.getByText("DB 構造を再取得しました。")).toBeVisible();
  await expect(page.getByRole("button", { name: "DB 構造を再取得" })).toHaveCount(0);
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  const clientWidth = await page.evaluate(() => document.documentElement.clientWidth);
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth);

  await recovered.getByTestId("ontology-build-schema-recovered-retry").click();
  await expect.poll(() => retryCalls).toBe(1);
  await expect(recovered).toHaveCount(0);
  expect(state.schemaRefreshCalls).toBe(1);
});

test("DB の構造を読み取れない失敗の案内から始めた再取得の失敗は、同じ場所に danger と「詳細」を出す（#2034）", async ({ page }) => {
  await mockApi(page);
  const failed = schemaScopeFailedJob();
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [failed.job] })
  );
  await page.route("**/api/schema/refresh-jobs", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "DB 構造の再取得を開始できませんでした。", error_code: "STUB_UNAVAILABLE" }),
    })
  );
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  const notice = page.getByTestId("ontology-build-schema-recovery");
  await notice.getByTestId("ontology-build-schema-refresh").click();

  await expect(notice.locator('[data-surface-tint="danger"]')).toBeVisible();
  await expect(notice.getByText("詳細", { exact: true })).toBeVisible();
  await expect(notice.getByTestId("ontology-build-schema-refresh")).toBeEnabled();
  await expect(page.getByText("DB 構造の再取得を開始できませんでした。")).toHaveCount(1);
  // ページの先頭に同じ失敗を重ねない。
  await expect(page.getByTestId("profile-ontology-build").locator(":scope > [data-surface-tint]")).toHaveCount(0);
});

test("失敗した構築後は主ボタンで現在の入力を再送信できる", async ({ page }) => {
  const state = await mockApi(page);
  let retryCalls = 0;
  const failed = buildJob("failed", "failed");
  failed.job.id = "job-failed";
  failed.job.error_message_ja = "Enterprise AI が未設定です。";
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [failed.job] })
  );
  await page.route("**/api/nl2sql/ontology-build/*/retry", (route) => {
    retryCalls += 1;
    return fulfillJson(route, buildJob("queued", "pending"));
  });
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);

  // リロード復旧: 失敗ジョブの終端カードに失敗理由が表示される
  await expect(page.getByTestId("ontology-build-retry")).toBeVisible();
  const failedSteps = page.getByTestId("ontology-build-steps");
  await expect(failedSteps.getByText("Enterprise AI が未設定です。")).toBeVisible();
  await expect(failedSteps.getByText("再実行してください", { exact: false })).toBeVisible();
  await expect(page.getByTestId("ontology-build-history")).toHaveCount(0);

  // 主ボタンで現在の入力を再送信 → 新ジョブがポーリングで完了まで進む
  const section = page.getByTestId("profile-ontology-build");
  await section.getByLabel("業務説明(自然言語)").fill("受注は顧客に紐づく。");
  await dropFiles(page, page.getByTestId("ontology-build-source-files-dropzone"), [
    {
      name: "rules.md",
      type: "text/markdown",
      content: "# 受注ルール",
    },
    {
      name: "terms.csv",
      type: "text/csv",
      content: "用語,説明\n受注,顧客からの注文",
    },
  ]);
  await dropFiles(page, page.getByTestId("ontology-build-qa-file-dropzone"), [
    {
      name: "qa_cases.csv",
      type: "text/csv",
      content: "QUESTION,SQL\n受注件数は,SELECT COUNT(*) FROM ORDERS",
    },
  ]);
  await section.getByRole("button", { name: "AI 構築を実行" }).click();
  await expect.poll(() => state.startCalls).toBe(1);
  expect(state.latestBusinessText).toBe("受注は顧客に紐づく。");
  expect(state.sourceFilesSeen).toBe(true);
  expect(state.qaFileSeen).toBe(true);
  expect(state.latestRunOptions).toEqual({
    runSchemaNaming: true,
    runQaExtraction: true,
    runTextExtraction: true,
  });
  const steps = page.getByTestId("ontology-build-steps");
  await expect(steps).toHaveAttribute("data-job-status", "succeeded", { timeout: 15000 });
  await steps.getByText("Markdown 下書き生成",{exact:true}).first().click();
  await expect(steps.getByText("Markdown 下書き v4 を生成しました", { exact: false })).toBeVisible();
  await expect(steps.getByText("構築が完了しました", { exact: false })).toHaveCount(0);
  await expect(page.locator('[aria-label="構築ジョブの補足ログ"]')).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-timeline")).toHaveCount(0);
  expect(retryCalls).toBe(0);
});

test("失敗した構築 job は「再実行」ボタンで retry API から再開できる", async ({ page }) => {
  await mockApi(page);
  const failedJob = buildJob("failed", "failed");
  failedJob.job.error_message_ja = "LLM 抽出に失敗しました。";
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [failedJob.job] })
  );
  let retryCalled = 0;
  const retriedJob = buildJob("running", "running");
  retriedJob.job.id = "job-retried";
  await page.route("**/api/nl2sql/ontology-build/job-1/retry", (route) => {
    retryCalled += 1;
    return fulfillJson(route, retriedJob);
  });
  await page.route("**/api/nl2sql/ontology-build/job-retried", (route) =>
    fulfillJson(route, retriedJob)
  );

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  const retryButton = page.getByTestId("ontology-build-retry");
  await expect(retryButton).toBeVisible();
  await retryButton.click();

  await expect
    .poll(() => retryCalled, { timeout: 8_000 })
    .toBeGreaterThan(0);
  // 新 job の進捗カードへ切り替わる(失敗バナーは消える)
  await expect(page.getByTestId("ontology-build-retry")).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-steps")).toContainText("オントロジー構築の処理状況");
});

test("強制終了した構築は理由と下書きを表示し保存済み入力から再実行できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await existingMarkdown(page);
  const failedJob = buildJob("cancelled", "failed");
  failedJob.job.error_message_ja = "構築処理の実行プロセスとの接続が失われました。保存済み入力と抽出結果を使って再実行してください。";
  Object.assign(failedJob.job, { error_code: "ONTOLOGY_BUILD_WORKER_LOST" });
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [failedJob.job] })
  );
  await page.route("**/api/nl2sql/ontology-build/job-1", route => fulfillJson(route, failedJob));
  let retryCalled = 0;
  const retriedJob = buildJob("running", "running");
  retriedJob.job.id = "job-retried";
  await page.route("**/api/nl2sql/ontology-build/job-1/retry", (route) => {
    retryCalled += 1;
    return fulfillJson(route, retriedJob);
  });
  await page.route("**/api/nl2sql/ontology-build/job-retried", (route) =>
    fulfillJson(route, retriedJob)
  );

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  const retryButton = page.getByTestId("ontology-build-retry");
  await expect(retryButton).toBeVisible();
  await expect(page.getByTestId("ontology-markdown-draft-editor")).toHaveValue(generatedDraftMarkdown);
  await expect(page.getByText(failedJob.job.error_message_ja, { exact: false }).first()).toBeVisible();
  for (const width of (testInfo.project.name === "desktop" ? [1280, 1920] : [375])) {
    await page.setViewportSize({ width, height: 900 });
    await retryButton.focus();
    await expect(retryButton).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath(`build-worker-lost-${width}-light.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  expect(retryCalled).toBe(0);
  await page.keyboard.press("Enter");

  await expect
    .poll(() => retryCalled, { timeout: 8_000 })
    .toBeGreaterThan(0);
  // 新 job の進捗カードへ切り替わる(失敗バナーは消える)
  await expect(page.getByTestId("ontology-build-retry")).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-steps")).toContainText("オントロジー構築の処理状況");
});


test("公開済み Markdown が無いときは公開日時を表示しない(revision だけ公開済み)", async ({ page }) => {
  await mockApi(page);
  await page.unroute("**/api/nl2sql/profiles/*/ontology-markdown");
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(route, {
      draft_markdown: "",
      published_markdown: "",
      draft_revision: null,
      published_revision: {
        id: "revision-published-1",
        version: 1,
        status: "published",
        schema_fingerprint: "fp",
        etag: "published-etag-1",
        published_at: "2026-08-29T02:33:06Z",
      },
      draft_etag: "",
      published_at: "2026-08-29T02:33:06Z",
    })
  );

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  const markdown = page.getByTestId("ontology-build-markdown");
  await markdown.getByRole("tab", { name: "公開済み Markdown オントロジー" }).click();
  await expect(markdown.getByTestId("ontology-markdown-published-viewer")).toContainText(
    "公開済み Markdown はまだありません。"
  );
  await expect(markdown.getByTestId("ontology-markdown-published-meta")).toHaveCount(0);
});

test("失効したProfile URLでは別Profileの情報を取得せず明示選択を待つ", async ({ page }, testInfo) => {
  const state = await mockApi(page);
  await page.goto("/ontology-build?profile=missing");
  const fetch = page.getByRole("button", { name: "情報を取得", exact: true });
  await expect(fetch).toBeDisabled();
  await expect(page.getByText("指定された業務プロファイルが見つかりません。")).toBeVisible();
  expect(state.profileDetailCalls).toEqual([]);
  expect(state.ontologyViewCalls).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("ontology-missing-profile.png") });
  await chooseSelectFieldOption(page.getByTestId("ontology-build-profile-select"), "default");
  await expect(fetch).toBeEnabled();
  await fetch.press("Enter");
  await expect(page.getByTestId("profile-ontology-build")).toBeVisible();
  expect(state.profileDetailCalls).toEqual(["default"]);
});

test("公開前の草稿保存中は編集と構築を固定し失敗後に草稿を保持する", async ({ page }, testInfo) => {
  await mockApi(page);
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) => fulfillJson(route, markdownDraftPayload(generatedDraftMarkdown)));
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  const editor = page.getByTestId("ontology-markdown-draft-editor");
  const draft = `${generatedDraftMarkdown}\n\n## 公開対象\n保存して公開する草稿`;
  await editor.fill(draft);
  let release: (() => void) | undefined;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let saves = 0;
  let publishes = 0;
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/draft", async (route) => {
    saves += 1;
    if (saves === 1) {
      await gate;
      return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "草稿保存テストエラー" }) });
    }
    return fulfillJson(route, markdownDraftPayload(draft, "markdown-etag-2"));
  });
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/publish", async (route) => { publishes += 1; await route.fallback(); });
  const publish = page.getByRole("button", { name: "オントロジーを公開", exact: true });
  await publish.click();
  try {
    await expect(editor).toBeDisabled();
    await expect(page.getByRole("button", { name: "AI 構築を実行", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Markdown 下書きを保存", exact: true })).toBeDisabled();
  } finally { release?.(); }
  await expect(page.getByText("草稿保存テストエラー", { exact: true })).toBeVisible();
  await expect(editor).toBeEnabled();
  await expect(editor).toHaveValue(draft);
  expect(publishes).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("ontology-publish-save-recovery.png") });
  await publish.press("Enter");
  await confirmPreparedPublish(page);
  await expect.poll(() => publishes).toBe(1);
  await expect(page.getByText("オントロジーを公開しました。", { exact: true })).toBeVisible();
  expect(saves).toBe(2);
});

function typedBundle(profileId: string) {
  const kinds = ["object_type", "property", "link_type", "function", "action_type", "interface"];
  return {
    id: `bundle-${profileId}`, profile_id: profileId, display_version: 2, etag: "bundle-etag", status: "draft",
    created_at: "2026-09-11T00:00:00Z", parent_id: "", findings: [], conflicts: [],
    definitions: kinds.map(kind => ({ id: `${profileId}-${kind}`, api_name: `${profileId}_${kind}`,
      kind, name_ja: `${profileId}の定義`, description_ja: "業務資料から構築した定義です。",
      review_status: "unreviewed", missing_information_ja: [], evidence: [], mappings: [],
      ...(kind === "object_type" ? { primary_key: ["orderId"], grain_ja: "受注単位" } : {}),
    })),
    coverage: kinds.map(kind => ({ kind, count: 1, status: "generated", reason_ja: "" })),
  };
}

const unifiedKinds = ["object_type", "property", "link_type", "interface", "function", "action_type", "shared_property", "value_type", "enumeration", "metric", "business_rule", "business_event", "object_set"];
async function unifiedGraphFixture(page: Page) {
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", route => fulfillJson(route, {
    ...markdownDraftPayload(generatedDraftMarkdown),
    published_markdown: generatedDraftMarkdown,
    published_revision: ontologyView.ontology_graph.revision,
  }));
  const nodes = unifiedKinds.filter(kind => kind !== "link_type").map(kind => ({
    id: `concept-${kind}`, revision_id: "revision-1", kind,
    technical_name: `API_${kind}`, business_name_ja: `定義_${kind}`, aliases: [`別名_${kind}`],
    description_ja: `説明_${kind}`, physical_mappings: [],
    provenance: { source_kind: "manual" }, review_status: "approved", confidence: 1,
    metadata: { definition: { kind, api_name: `API_${kind}`, name_ja: `定義_${kind}`, description_ja: `説明_${kind}`, evidence: [{source_id:"markdown",locator:"line:5",excerpt_ja:"公開した定義の根拠"}] } },
  }));
  const edges = [{id:"concept-link_type",revision_id:"revision-1",kind:"link_type",source_node_id:"concept-object_type",target_node_id:"concept-object_type",relationship_name_ja:"定義_link_type",description_ja:"関係の業務説明",cardinality:"one_to_many",provenance:{source_kind:"manual"},review_status:"approved",metadata:{api_name:"API_link_type",definition:{kind:"link_type",api_name:"API_link_type",name_ja:"定義_link_type",source:"API_object_type",target:"API_object_type",description_ja:"関係の業務説明"}}}];
  const referenceEdges = nodes.filter(n => n.kind !== "object_type").map(n => ({
    id: `reference-${n.id}`, revision_id: "revision-1", kind: "uses", source_node_id: n.id,
    target_node_id: "concept-object_type", relationship_name_ja: "対象", review_status: "approved",
  }));
  const allEdges = [...edges, ...referenceEdges];
  await page.route("**/api/nl2sql/profiles/*/ontology-view", route => fulfillJson(route, {...ontologyView, materialized:true, stale:false, profile_ontology_view:{...ontologyView.profile_ontology_view,node_ids:nodes.map(n=>n.id),edge_ids:allEdges.map(e=>e.id)},ontology_graph:{...ontologyView.ontology_graph,nodes,edges:allEdges}}));
}

async function openUnifiedGraph(page: Page) {
  const panel=page.locator("#ontology-query-playground-panel");
  const expand=panel.getByRole("button",{name:"グラフを表示",exact:true});
  const modeAll=panel.getByTestId("ontology-graph-mode-all");
  // 展開ボタンかモード切替のどちらかが描画されるまで待ってから判断する（その瞬間の isVisible だけでは、遅い環境で展開を飛ばす）。
  await expect(expand.or(modeAll).filter({visible:true}).first()).toBeVisible();
  if(await expand.isVisible()) await expand.click();
  await modeAll.click();
  return panel;
}

async function existingMarkdown(page: Page) {
  const state=await mockApi(page);
  state.jobPolls = 2;
  state.draftMarkdown = generatedDraftMarkdown;
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  return state;
}

test("構築段階は一般状態を日本語だけで表示し専門概念の併記を維持する", async ({ page }, testInfo) => {
  await mockApi(page);
  const payload = buildJob("succeeded", "succeeded");
  payload.job.definition_phases.push({name:"markdown",status:"succeeded",detail_ja:"全13分類を出力しました。"},{name:"save",status:"succeeded",detail_ja:"保存しました。"});
  await page.route("**/api/nl2sql/ontology-build/*", route => fulfillJson(route, payload));
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  await page.getByLabel("業務説明(自然言語)").fill("受注と顧客の関係を構築してください。");
  await page.getByRole("button", { name: "AI 構築を実行", exact: true }).click();
  const phases = page.getByTestId("ontology-build-steps");
  await expect(phases).toContainText("範囲・資料の準備");
  await expect(phases).not.toContainText(/Completed|Running|Skipped|Pending|Failed/);
  await expect(phases).toContainText("保存・最終確認");
  const text = await phases.textContent();
  await expect(phases).not.toContainText("基本概念の構築");
  await expect(phases.locator("[data-testid^=ontology-build-stage-]")).toHaveCount(5);
  await expect(page.getByTestId("ontology-build-step-progress")).toContainText("5/5");
  expect(text!.indexOf("Markdown 下書き生成")).toBeLessThan(text!.indexOf("保存・最終確認"));
  await phases.getByText("関連概念の構築",{exact:true}).click();
  await phases.getByText("主要概念（6種類）",{exact:true}).click();
  await phases.getByText("補助概念（7種類）",{exact:true}).click();
  await expect(phases.getByText(/オブジェクト型（Object Type）/)).toBeVisible();
  await expect(phases.getByText(/オブジェクト集合（Object Set）/)).toBeVisible();
  await expect(page.getByTestId("ontology-typed-results")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await phases.screenshot({ path: testInfo.outputPath("ontology-build-statuses.png") });
});

test("統一グラフの13分類を識別し検索・選択・定義詳細を利用できる", async ({ page }, testInfo) => {
  await mockApi(page); await unifiedGraphFixture(page);
  await page.goto("/ontology-build?profile=default"); await loadOntologyBuildWorkspace(page);
  const panel = await openUnifiedGraph(page);
  const category = panel.getByRole("combobox", {name:"概念の種類",exact:true});
  await expect(category.locator("optgroup")).toHaveCount(2);
  await expect(category.locator("option")).toHaveCount(14);
  for (const kind of unifiedKinds) {
    await category.selectOption(kind);
    await expect(category).toHaveValue(kind);
    if (kind !== "link_type") {
      const card=panel.getByTestId(`ontology-node-card-concept-${kind}`);
      await expect(card).toBeVisible();
      await card.click();
      await expect(panel.getByText(`説明_${kind}`,{exact:true}).last()).toBeVisible();
    } else {
      await panel.getByTestId("ontology-graph-search").fill("API_link_type");
      await panel.getByRole("button",{name:"定義_link_type",exact:true}).click();
      await expect(panel.getByText("関係の業務説明",{exact:true}).last()).toBeVisible();
      await panel.getByTestId("ontology-graph-search").clear();
    }
  }
  await category.selectOption("");
  await category.selectOption("function");
  await page.reload();
  await loadOntologyBuildWorkspace(page);
  await openUnifiedGraph(page);
  await expect(category).toHaveValue("function");
  await category.selectOption("");
  await expect(page.getByTestId("ontology-typed-results")).toHaveCount(0);
  await expect(page.getByRole("region",{name:"公開能力（Published Capabilities）",exact:true})).toHaveCount(0);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await category.scrollIntoViewIfNeeded();
  await page.screenshot({path:testInfo.outputPath("unified-13-concepts.png")});
});

test("旧結果 API が停止していても Markdown を取得し Profile を分離する", async ({ page }) => {
  await mockProfileScopedApi(page);
  let obsolete=0;
  await page.route("**/api/nl2sql/profiles/*/ontology-results**",async route=>{obsolete++;await route.fulfill({status:410});});
  await page.goto("/ontology-build?profile=sales"); await loadOntologyBuildWorkspace(page);
  await expect(page.getByTestId("ontology-typed-results")).toHaveCount(0);
  await expect(page.getByTestId("ontology-build-markdown")).toBeVisible();
  await chooseSelectFieldOption(page.getByTestId("ontology-build-profile-select"), "finance"); await loadOntologyBuildWorkspace(page);
  await expect(page.getByTestId("ontology-build-markdown")).toBeVisible();
  expect(obsolete).toBe(0);
});

test("Markdown 解析の競合と行位置を表示し公開を阻止する", async ({ page },testInfo) => {
  await existingMarkdown(page);
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/preparations/*",route=>fulfillJson(route,{id:"preparation-1",status:"failed",findings:[{severity:"error",message_ja:"行 5: 指標の式が競合しています。両方の根拠を確認してください。"}],differences:[],error_message_ja:""}));
  await page.getByRole("button",{name:"オントロジーを公開",exact:true}).click();
  await expect(page.getByText("行 5: 指標の式が競合しています。両方の根拠を確認してください。",{exact:true})).toBeVisible();
  await expect(page.getByRole("button",{name:"確認した内容を公開",exact:true})).toHaveCount(0);
  await expect(page.getByTestId("ontology-markdown-draft-editor")).toHaveValue(generatedDraftMarkdown);
  await page.screenshot({path:testInfo.outputPath("markdown-conflict.png")});
});

test("Markdown の変更差分を確認して公開し図と公開本文を更新する", async ({ page }) => {
  await page.addInitScript(() => Reflect.deleteProperty(Crypto.prototype, "randomUUID"));
  const keys: string[] = [];
  page.on("request", request => {
    if (/ontology-markdown\/(prepare|publish)$/.test(request.url())) keys.push(request.headers()["idempotency-key"]);
  });
  const state=await existingMarkdown(page);
  const editor=page.getByTestId("ontology-markdown-draft-editor");
  await editor.fill(generatedDraftMarkdown+"\n\n## 関数（Function）\n顧客契約を参照する。");
  const before=state.ontologyViewCalls;
  await page.getByRole("button",{name:"オントロジーを公開",exact:true}).click();
  await expect(page.getByRole("button",{name:"確認した内容を公開",exact:true})).toBeVisible();
  expect(state.published).toBe(false);
  await confirmPreparedPublish(page);
  await expect(page.getByText("オントロジーを公開しました。",{exact:true})).toBeVisible();
  expect(state.savedDraftMarkdown).toContain("顧客契約");
  expect(state.publishPayload).toMatchObject({preparation_id:"preparation-1",confirmed:true});
  expect(keys).toHaveLength(2);
  for (const key of keys) expect(key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(new Set(keys).size).toBe(2);
  await expect.poll(()=>state.ontologyViewCalls).toBeGreaterThan(before);
  await page.getByRole("tab",{name:"公開済み Markdown オントロジー",exact:true}).click();
  await expect(page.getByTestId("ontology-markdown-published-viewer")).toContainText("顧客契約");
});

test("Function と Action はグラフ上の定義として表示し操作 API を呼ばない", async ({ page },testInfo) => {
  await mockApi(page);await unifiedGraphFixture(page);
  let capabilityRequests=0;
  await page.route("**/api/nl2sql/profiles/*/ontology-capabilities**",async route=>{capabilityRequests++;await route.fulfill({status:410});});
  await page.goto("/ontology-build?profile=default");await loadOntologyBuildWorkspace(page);
  const panel=await openUnifiedGraph(page);
  for(const kind of ["function","action_type"]){
    await panel.getByRole("combobox",{name:"概念の種類",exact:true}).selectOption(kind);
    await panel.getByTestId(`ontology-node-card-concept-${kind}`).click();
    await expect(panel.getByText(`説明_${kind}`,{exact:true}).last()).toBeVisible();
    await expect(panel.getByRole("button",{name:/実行|プレビュー|実装を設定/})).toHaveCount(0);
  }
  await page.reload();await loadOntologyBuildWorkspace(page);
  expect(capabilityRequests).toBe(0);
  await expect(page.getByRole("region",{name:"公開能力（Published Capabilities）",exact:true})).toHaveCount(0);
  await page.screenshot({path:testInfo.outputPath("functions-read-only.png")});
});

test("旧能力 API の遅延や障害は Markdown ページの読込に影響しない", async ({ page }) => {
  await mockApi(page);
  let requests=0;
  await page.route("**/api/nl2sql/profiles/*/ontology-capabilities**",async route=>{requests++;await route.fulfill({status:503});});
  await page.goto("/ontology-build?profile=default");await loadOntologyBuildWorkspace(page);
  await expect(page.getByTestId("ontology-build-markdown")).toBeVisible();
  await expect(page.getByText("能力を読み込み中",{exact:true})).toHaveCount(0);
  await expect(page.getByRole("region",{name:"公開能力（Published Capabilities）",exact:true})).toHaveCount(0);
  expect(requests).toBe(0);
});

test("Markdown の変更は準備済みの公開確認を失効させる", async ({ page }) => {
 const state=await existingMarkdown(page);
 await page.getByRole("button",{name:"オントロジーを公開",exact:true}).click();
 await expect(page.getByRole("button",{name:"確認した内容を公開",exact:true})).toBeVisible();
 await page.getByTestId("ontology-markdown-draft-editor").fill(generatedDraftMarkdown+"\n新しい定義");
 await expect(page.getByRole("button",{name:"確認した内容を公開",exact:true})).toHaveCount(0);
 expect(state.published).toBe(false);
 await page.reload();await loadOntologyBuildWorkspace(page);
 await expect(page.getByTestId("ontology-markdown-draft-editor")).toHaveValue(/新しい定義/);
 await expect(page.getByRole("alertdialog")).toHaveCount(0);
 expect(state.published).toBe(false);
});

for (const committedBeforeDisconnect of [true, false]) {
  test(`Markdown 公開の応答喪失後は結果照会だけで復旧する (${committedBeforeDisconnect ? "commit 済み" : "未実行"})`, async ({ page },testInfo) => {
    await page.addInitScript(() => Reflect.deleteProperty(Crypto.prototype, "randomUUID"));
    const state=await existingMarkdown(page);let calls=0,lookups=0;let key="";
    await page.route("**/ontology-markdown/publish",route=>{calls++;key=route.request().headers()["idempotency-key"];return route.abort("connectionreset");});
    await page.route("**/ontology-markdown/publication-outcome?*",route=>{lookups++;expect(new URL(route.request().url()).searchParams.get("key")).toBe(key);return fulfillJson(route,{job:committedBeforeDisconnect?{id:"snapshot-1",revision_id:"snapshot-1",requested_etag:state.draftMarkdownEtag,status:"succeeded"}:null});});
    await page.getByRole("button",{name:"オントロジーを公開",exact:true}).click();await confirmPreparedPublish(page);
    await expect(page.getByRole("button",{name:"公開結果を確認",exact:true})).toBeVisible();expect(calls).toBe(1);
    await page.reload();await loadOntologyBuildWorkspace(page);
    await expect(page.getByRole("alertdialog")).toHaveCount(0);expect(calls).toBe(1);
    await page.getByRole("button",{name:"公開結果を確認",exact:true}).click();await expect.poll(()=>lookups).toBe(1);
    if(committedBeforeDisconnect) await expect(page.getByText("オントロジーを公開しました。",{exact:true})).toBeVisible();
    else await expect(page.getByText("公開は完了していません。内容を確認して、もう一度公開してください。",{exact:true})).toBeVisible();
    expect(calls).toBe(1);
    if(!committedBeforeDisconnect){
      // 公開されていなければ照会キーを捨て、同じ確認結果から再度公開できる(公開ボタンを永久に無効化しない)
      await expect(page.getByRole("button",{name:"公開結果を確認",exact:true})).toHaveCount(0);
      const firstKey=key;
      await confirmPreparedPublish(page);
      await expect.poll(()=>calls).toBe(2);
      expect(key).not.toBe(firstKey);
    }
    await page.screenshot({path:testInfo.outputPath(`markdown-publication-recovery-${committedBeforeDisconnect}.png`)});
  });
}

for (const lostFailureResponse of [false, true]) {
  test(`Markdown 公開失敗は草稿と旧公開版を保持する (${lostFailureResponse ? "失敗応答喪失" : "確定失敗"})`, async ({ page }) => {
    const state=await existingMarkdown(page);let calls=0;
    await page.route("**/ontology-markdown/publish",route=>{calls++;return lostFailureResponse?route.abort("connectionreset"):route.fulfill({status:409,contentType:"application/json",body:JSON.stringify({detail:"公開条件が変更されました。再確認してください。"})});});
    await page.getByRole("button",{name:"オントロジーを公開",exact:true}).click();await confirmPreparedPublish(page);
    if(lostFailureResponse) await expect(page.getByRole("button",{name:"公開結果を確認",exact:true})).toBeVisible();
    else await expect(page.getByText("公開条件が変更されました。再確認してください。",{exact:true})).toBeVisible();
    await expect(page.getByTestId("ontology-markdown-draft-editor")).toHaveValue(generatedDraftMarkdown);
    expect(state.published).toBe(false);expect(calls).toBe(1);
    await page.reload();await loadOntologyBuildWorkspace(page);expect(calls).toBe(1);
  });
}

test("統一 Markdown タブとグラフをキーボードで確認する", async ({page},testInfo) => {
  await existingMarkdown(page);await unifiedGraphFixture(page);await page.reload();await loadOntologyBuildWorkspace(page);
  const draft=page.getByRole("tab",{name:"Markdown オントロジー下書き",exact:true});
  await draft.focus();await page.keyboard.press("End");await expect(page.getByRole("tab",{name:"公開済み Markdown オントロジー",exact:true})).toBeFocused();
  await page.keyboard.press("Home");await expect(draft).toBeFocused();
  const panel=await openUnifiedGraph(page);const kinds=panel.getByRole("combobox",{name:"概念の種類",exact:true});
  await kinds.focus();await page.keyboard.press("ArrowDown");await page.keyboard.press("Enter");await expect(kinds).toBeFocused();
  await page.screenshot({path:testInfo.outputPath("unified-markdown.png"),fullPage:true});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});

test("Markdown 公開前の実データ検証と受入テストは入力エラーをその場で示す", async ({page},testInfo) => {
  await existingMarkdown(page);let calls=0;
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/preparations/*/validate-data",route=>{calls++;expect(route.request().postDataJSON().acceptance_cases).toEqual([]);return fulfillJson(route,{});});
  await page.getByRole("button",{name:"オントロジーを公開",exact:true}).click();
  const check=page.getByRole("region",{name:"公開前の確認",exact:true});
  await check.locator("summary").filter({hasText:"実データの検証"}).click();
  const input=check.getByLabel(/受入テスト/);await input.fill("{bad");
  await check.getByRole("button",{name:"実データの検証",exact:true}).click();
  await expect(page.getByText("有効な JSON 配列を入力してください。",{exact:true})).toBeVisible();expect(calls).toBe(0);
  await input.fill("[]");await check.getByRole("button",{name:"実データの検証",exact:true}).click();await expect.poll(()=>calls).toBe(1);
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown/preparations/*",route=>fulfillJson(route,{id:"preparation-1",status:"ready",draft_etag:"draft-etag-4",expected_head:"",display_version:4,findings:[],differences:[],error_message_ja:"",data_report:{errors:1,acceptance_cases:[{passed:false}]}}));
  await check.getByRole("button",{name:"実データの検証",exact:true}).click();
  await expect(check.getByRole("button",{name:"確認した内容を公開",exact:true})).toBeDisabled();
  await expect(check.getByText("実データ検証のエラーを解決してから公開してください。",{exact:true})).toBeVisible();
  await page.screenshot({path:testInfo.outputPath("markdown-data-check.png")});
});

test("Markdown 草稿は往復ナビで保持し DB とユーザーを越えて流用しない",async({page})=>{
  // 往復・再読込で画面を 7 回読み込む長いテストで、2 worker の CI では既定の 30 秒を超えることがある（#1519）。
  test.slow();
  const state=await mockApi(page);state.jobPolls=2;
  let database="database-a", user={...systemAdminMe};
  await page.route("**/api/auth/me",route=>fulfillJson(route,user));
  await page.route("**/api/ready/database",route=>fulfillJson(route,{status:"ok",check:"ok",detail:null,context_id:database}));
  await page.goto("/ontology-build?profile=default");await loadOntologyBuildWorkspace(page);
  const input=page.getByTestId("ontology-markdown-draft-editor");
  await input.fill("最初の利用者と DB の草稿");
  page.on("dialog",dialog=>void dialog.accept());
  await page.goto("/history");await page.goBack();await loadOntologyBuildWorkspace(page);await expect(input).toHaveValue("最初の利用者と DB の草稿");
  await page.goForward();await page.goBack();await loadOntologyBuildWorkspace(page);await expect(input).toHaveValue("最初の利用者と DB の草稿");
  database="database-b";await page.reload();await loadOntologyBuildWorkspace(page);await expect(input).toHaveValue(generatedDraftMarkdown);await input.fill("別 DB の草稿");
  database="database-a";await page.reload();await loadOntologyBuildWorkspace(page);await expect(input).toHaveValue("最初の利用者と DB の草稿");
  user={...systemAdminMe,user_uuid:"other-ontology-user",login_user_id:"OTHER"};await page.reload();await loadOntologyBuildWorkspace(page);await expect(input).toHaveValue(generatedDraftMarkdown);
});

for (const saveExplicitly of [true, false]) {
  test(`公開 snapshot の新 ETag で次の草稿を保存できる (${saveExplicitly ? "保存後に公開" : "公開時に保存"})`, async ({ page }, testInfo) => {
    await mockApi(page);
    let text = generatedDraftMarkdown;
    let etag = "draft-before-save";
    let version = 4;
    let published = false;
    let publishedText = "";
    let saves = 0;
    let refreshedAfterPublish = false;
    const state = () => ({
      ...markdownDraftPayload(text, etag),
      draft_revision: { id: `revision-draft-${version}`, version, status: "draft", etag: `revision-${version}`, schema_fingerprint: "fp" },
      draft_version: version,
      published_revision: published ? { id: "ontology_markdown_snapshot_confirmed", version: 4, status: "published", etag: "snapshot-hash", schema_fingerprint: "fp" } : null,
      published_version: published ? 4 : null,
      published_markdown: publishedText,
    });
    await page.route("**/api/nl2sql/profiles/*/ontology-markdown", route => {
      if (published) refreshedAfterPublish = true;
      return fulfillJson(route, state());
    });
    await page.route("**/api/nl2sql/profiles/*/ontology-markdown/draft", route => {
      const body = route.request().postDataJSON();
      expect(body.base_etag).toBe(etag);
      saves += 1;
      if (published) version = 5;
      text = body.markdown;
      etag = `saved-${saves}`;
      return fulfillJson(route, state());
    });
    const preparation = () => ({ id: "check-current", status: "ready", draft_etag: etag, expected_head: "", display_version: 4, findings: [], differences: [], error_message_ja: "" });
    await page.route("**/ontology-markdown/prepare", route => fulfillJson(route, preparation()));
    await page.route("**/ontology-markdown/preparations/*", route => fulfillJson(route, preparation()));
    await page.route("**/ontology-markdown/publish", route => {
      const body = route.request().postDataJSON();
      expect(body.draft_etag).toBe(etag);
      const requested = etag;
      published = true;
      publishedText = text;
      // 実 backend は草稿を draft のまま保ち、CAS で ETag だけを更新する。
      etag = "draft-after-publication-cas";
      return fulfillJson(route, { job: { id: "ontology_markdown_snapshot_confirmed", revision_id: "ontology_markdown_snapshot_confirmed", requested_etag: requested, status: "succeeded" } });
    });
    await page.goto("/ontology-build?profile=default");
    await loadOntologyBuildWorkspace(page);
    const editor = page.getByTestId("ontology-markdown-draft-editor");
    const save = page.getByRole("button", { name: "Markdown 下書きを保存", exact: true });
    await editor.fill(`${generatedDraftMarkdown}\n公開する手動編集`);
    if (saveExplicitly) {
      await save.click();
      await expect(save).toBeDisabled();
    }
    await page.getByRole("button", { name: "オントロジーを公開", exact: true }).click();
    await confirmPreparedPublish(page);
    await expect.poll(() => refreshedAfterPublish).toBe(true);
    await page.getByRole("tab", { name: "Markdown オントロジー下書き", exact: true }).click();
    await expect(editor).toBeEditable();
    await editor.fill(`${publishedText}\n次の版への編集`);
    await save.focus();
    await page.keyboard.press("Enter");
    await expect(save).toBeDisabled();
    expect(saves).toBe(2);
    await expect(page.getByTestId("ontology-markdown-tab-draft-meta")).toHaveText("v5");
    await expect(page.getByTestId("ontology-markdown-tab-published-meta")).toHaveText("v4");
    await page.reload();
    await loadOntologyBuildWorkspace(page);
    await expect(editor).toHaveValue(/次の版への編集/);
    await expect(save).toBeDisabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await editor.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("markdown-edit-after-publication.png") });
  });
}

for (const width of [1280, 1920]) {
  test(`統合概念生成は5段階とログを一度だけ表示する ${width}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop" && width !== 1280, "375幅は各テーマ1回確認");
    if (testInfo.project.name === "desktop") await page.setViewportSize({width, height: 1000});
    await mockApi(page);
    const payload = buildJob("running", "succeeded");
    payload.job.definition_phases = ["freeze", "evidence", "concepts", "validation", "markdown", "save"].map(name => ({
      name, status: name === "concepts" ? "running" : ["freeze", "evidence"].includes(name) ? "succeeded" : "pending", detail_ja: "",
    }));
    payload.job.steps.find(step => step.name === "schema_naming")!.status = "running";
    payload.job.steps.find(step => step.name === "proposal_registration")!.status = "pending";
    const typedEvents = [
      {at:new Date().toISOString(),message_ja:"13種類の関連概念を抽出しています。",step:"schema_naming",phase:"concepts"},
      {at:new Date().toISOString(),message_ja:"13種類の関連概念を抽出しています。",step:"schema_naming",phase:"concepts"},
    ];
    payload.job.events = typedEvents;
    payload.job.started_at = new Date().toISOString();
    await page.route("**/api/nl2sql/ontology-build/*", route => fulfillJson(route, payload));
    await page.goto("/ontology-build?profile=default");
    await loadOntologyBuildWorkspace(page);
    await page.getByRole("button", {name:"AI 構築を実行",exact:true}).click();
    const progress = page.getByTestId("ontology-build-steps");
    const concepts = progress.getByTestId("ontology-build-stage-concepts");
    await expect(concepts).toHaveAttribute("data-step-status", "running");
    await expect(progress.locator("[data-testid^=ontology-build-stage-]")).toHaveCount(5);
    await expect(page.getByTestId("ontology-build-step-progress")).toContainText("1/5");
    await expect(progress).not.toContainText("基本概念の構築");
    await expect(progress).not.toContainText("共有定義（Shared Definitions）");
    await expect(concepts.getByText("13種類の関連概念を抽出しています。",{exact:true})).toHaveCount(1);
    await expect(concepts.getByText("13種類の関連概念を抽出しています。",{exact:true})).toBeVisible();
    // 共有の Disclosure は見出しの文言を summary の中の span に置く（#397）。getByText は focus できない span を指すため、
    // キーボード操作は focus できる summary に対して行う（#424）。
    const mainConcepts = concepts.locator("summary").filter({hasText:"主要概念（6種類）"});
    await mainConcepts.focus(); await expect(mainConcepts).toBeFocused(); await page.keyboard.press("Enter");
    await expect(concepts.getByText(/オブジェクト型（Object Type）/)).toBeVisible();
    await progress.screenshot({path:testInfo.outputPath(`unified-progress-light-${width}.png`)});
    const savedNote = "Markdown 下書き v2 を生成しました(候補 13 件)。";
    payload.job.steps.find(step => step.name === "proposal_registration")!.detail_ja = savedNote;
    payload.job.events.push({at:new Date().toISOString(), message_ja:savedNote, step:"proposal_registration", phase:"save"} as typeof typedEvents[number]);
    payload.job.events.push({at:new Date().toISOString(), message_ja:"保存後の整合性を確認しました。", step:"proposal_registration", phase:"save"} as typeof typedEvents[number]);
    payload.job.status = "succeeded";
    payload.job.definition_phases.forEach(p => {p.status = "succeeded";});
    payload.job.steps.forEach(step => {step.status = "succeeded";});
    await expect(page.getByTestId("ontology-build-step-progress")).toContainText("5/5");
    const saveStage = progress.getByTestId("ontology-build-stage-save");
    await saveStage.getByText("保存・最終確認",{exact:true}).click();
    await expect(saveStage.getByText(savedNote,{exact:true})).toHaveCount(1);
    await expect(saveStage.getByText(savedNote,{exact:true})).toBeVisible();
    await expect(saveStage.getByText("保存後の整合性を確認しました。",{exact:true})).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

for (const status of ["queued", "running", "succeeded", "failed", "cancelled"]) {
  test(`既存定義取り込みは構築状態にかかわらず表示しない (${status})`, async ({ page }) => {
    const state = await mockApi(page);
    state.jobPolls = 2;
    const payload = buildJob(status, status === "queued" ? "pending" : status);
    await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", route => fulfillJson(route, { jobs: [payload.job] }));
    await page.route("**/api/nl2sql/ontology-build/*", route => fulfillJson(route, payload));
    const imports: string[] = [];
    page.on("request", request => {
      if (/ontology-markdown\/(migration-preview|migrate)$/.test(new URL(request.url()).pathname)) imports.push(request.url());
    });
    await page.goto("/ontology-build?profile=default");
    await loadOntologyBuildWorkspace(page);
    await expect(page.getByTestId("ontology-build-markdown")).toBeVisible();
    await expect(page.getByRole("button", { name: "既存の定義を取り込む", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Markdown 下書きに反映", exact: true })).toHaveCount(0);
    await expect(page.getByTestId("ontology-publish-actions").getByRole("button")).toHaveCount(1);
    expect(imports).toEqual([]);
  });
}

test("既存定義取り込みの廃止後も空の草稿から構築できる", async ({ page }) => {
  await mockApi(page);
  await page.goto("/ontology-build?profile=default");
  await expect(page.getByTestId("ontology-view-fetch")).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "本文へスキップ" })).toBeFocused();
  await loadOntologyBuildWorkspace(page);
  await expect(page.getByTestId("ontology-markdown-draft-empty")).toBeVisible();
  await expect(page.getByRole("button", { name: "既存の定義を取り込む", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "AI 構築を実行", exact: true }).click();
  await expect(page.getByTestId("ontology-markdown-draft-editor")).toHaveValue(generatedDraftMarkdown);
});

test("既存定義取り込みの廃止後も草稿の保存と公開を操作できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const state = await existingMarkdown(page);
  const text = generatedDraftMarkdown + "\n手動で追記した業務定義";
  await page.getByTestId("ontology-markdown-draft-editor").fill(text);
  const publish = page.getByRole("button", { name: "オントロジーを公開", exact: true });
  const widths = testInfo.project.name === "desktop" ? [1280, 1920] : [375];
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByRole("button", { name: "既存の定義を取り込む", exact: true })).toHaveCount(0);
    if (width === 1920) {
      const title = await page.getByRole("heading", { name: "オントロジー構築", exact: true, level: 1 }).boundingBox();
      const content = await page.locator("#ontology-query-playground-panel").boundingBox();
      expect(title).not.toBeNull();
      expect(content).not.toBeNull();
      expect(Math.abs(title!.x - content!.x)).toBeLessThanOrEqual(1);
    }
    await publish.focus();
    await expect(publish).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath(`without-definition-import-${width}-light.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "確認した内容を公開", exact: true })).toBeVisible();
  expect(state.draftMarkdown).toBe(text);
  await expect(page.getByRole("button", { name: "Markdown 下書きに反映", exact: true })).toHaveCount(0);
  await confirmPreparedPublish(page);
  await expect.poll(() => state.published).toBe(true);
});

for (const reason of ["timeout", "restart"] as const) {
  test(`公開準備の${reason}後に再試行し、確認前は公開しない`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    const state = await existingMarkdown(page);
    let attempts = 0;
    let failed = false;
    const message = reason === "timeout"
      ? "Markdown の解析が実行期限を超えました。Enterprise AI の接続・応答状況を確認し、再度公開前の確認を実行してください。"
      : "サーバーの再起動により Markdown の解析が中断されました。再度公開前の確認を実行してください。";
    const preparation = () => ({
      id: `recovery-${attempts}`, status: attempts > 1 ? "ready" : failed ? "failed" : "running",
      draft_etag: state.draftMarkdownEtag, expected_head: "", display_version: 4,
      findings: [], differences: [], error_message_ja: failed && attempts === 1 ? message : "",
    });
    await page.route("**/api/nl2sql/profiles/*/ontology-markdown/prepare", route => {
      attempts++;
      return fulfillJson(route, preparation());
    });
    await page.route("**/api/nl2sql/profiles/*/ontology-markdown/preparations/*", route => fulfillJson(route, preparation()));
    const publish = page.getByRole("button", { name: "オントロジーを公開", exact: true });
    await publish.click();
    await expect(publish).toBeDisabled();
    await expect(page.getByText("Markdown を解析・検証しています", { exact: true })).toBeVisible();
    failed = true;
    await expect(page.getByText(message, { exact: true })).toBeVisible();
    await expect(publish).toBeEnabled();
    const widths = testInfo.project.name === "desktop" ? [1280, 1920] : [375];
    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      await publish.focus();
      await expect(publish).toBeFocused();
      await page.screenshot({ path: testInfo.outputPath(`preparation-${reason}-${width}-light.png`) });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await page.keyboard.press("Enter");
    await expect(page.getByRole("button", { name: "確認した内容を公開", exact: true })).toBeVisible();
    expect(attempts).toBe(2);
    expect(state.published).toBe(false);
    await expect(page.getByTestId("ontology-markdown-draft-editor")).toHaveValue(generatedDraftMarkdown);
  });
}

for (const status of ["failed", "ready"] as const) {
  test(`公開診断を限高・集約しエラーを阻止する (${status})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    const state = await mockApi(page);
    state.jobPolls = 2;
    state.draftMarkdown = generatedDraftMarkdown;
    const message = "証拠の資料・位置・原文を照合できません。";
    const error = "Oracle SQL 式を検証できません: 対象列がありません。";
    const findings = [
      ...Array.from({ length: 100 }, (_, i) => ({ severity: "warning", code: "EVIDENCE_UNRESOLVED", definition_id: `concept-${i}`, field: "evidence", message_ja: message })),
      ...Array.from({ length: 20 }, (_, i) => ({ severity: "warning", message_ja: `追加の確認事項 ${i}` })),
      { severity: "error", code: "SQL_EXPRESSION_INVALID", definition_id: "Order.id", field: "mappings.0.expression_sql", message_ja: error },
    ];
    const preparation = { id: "diagnostics", status, draft_etag: state.draftMarkdownEtag, expected_head: "", display_version: 4, findings, differences: [], error_message_ja: error };
    await page.route("**/ontology-markdown/prepare", route => fulfillJson(route, preparation));
    await page.route("**/ontology-markdown/preparations/*", route => fulfillJson(route, preparation));
    await page.goto("/ontology-build?profile=default");
    await expect(page.getByTestId("ontology-view-fetch")).toBeVisible();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "本文へスキップ" })).toBeFocused();
    await loadOntologyBuildWorkspace(page);
    await page.getByRole("button", { name: "オントロジーを公開", exact: true }).click();
    const region = page.getByRole("region", { name: "公開前のエラー・警告", exact: true });
    await expect(region.getByText(message, { exact: true })).toHaveCount(1);
    await expect(region.getByText(error, { exact: true })).toHaveCount(1);
    await expect(region.getByText("100 件", { exact: true })).toBeVisible();
    const confirm = page.getByRole("button", { name: "確認した内容を公開", exact: true });
    if (status === "ready") await expect(confirm).toBeDisabled();
    else await expect(confirm).toHaveCount(0);
    for (const width of testInfo.project.name === "desktop" ? [1280, 1920] : [375]) {
      await page.setViewportSize({ width, height: 900 });
      await region.scrollIntoViewIfNeeded();
      const metrics = await region.evaluate(element => ({
        height: element.getBoundingClientRect().height, max: 18 * parseFloat(getComputedStyle(document.documentElement).fontSize),
        scroll: element.scrollHeight, client: element.clientHeight,
      }));
      expect(metrics.height).toBeLessThanOrEqual(metrics.max + 1);
      expect(metrics.scroll).toBeGreaterThan(metrics.client);
      await region.focus();
      await expect(region).toBeFocused();
      await pressScrollKey(region, "End");
      await expect.poll(() => region.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
      await expect.poll(() => region.evaluate(element => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThanOrEqual(1);
      await pressScrollKey(region, "Home");
      await expect.poll(() => region.evaluate(element => element.scrollTop)).toBe(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`diagnostics-${status}-${width}-light.png`) });
    }
    await region.locator("summary").first().press("Enter");
    await expect(region.getByText("Order.id / mappings.0.expression_sql / SQL_EXPRESSION_INVALID", { exact: true })).toBeVisible();
    expect(state.published).toBe(false);
  });
}

test("警告のみで公開し再読込後も診断を参照・保存できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const state = await existingMarkdown(page);
  const message = "根拠を確認できない推論です。業務担当者の確認が必要です。";
  const findings = Array.from({ length: 40 }, (_, i) => ({ severity: "warning", code: "INFERENCE_WITHOUT_EVIDENCE", definition_id: `property-${i}`, field: "evidence", message_ja: message }));
  const preparation = { id: "warnings", status: "ready", draft_etag: state.draftMarkdownEtag, expected_head: "", display_version: 4, findings, differences: [], error_message_ja: "" };
  await page.route("**/ontology-markdown/prepare", route => fulfillJson(route, preparation));
  await page.route("**/ontology-markdown/preparations/*", route => fulfillJson(route, preparation));
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", route => fulfillJson(route, {
    ...markdownDraftPayload(state.draftMarkdown), draft_etag: state.draftMarkdownEtag,
    published_markdown: state.published ? state.draftMarkdown : "",
    published_revision: state.published ? { ...ontologyView.ontology_graph.revision, id: "revision-draft-4", version: 4, status: "published" } : null,
    published_version: state.published ? 4 : null,
    published_findings: state.published ? findings : [], published_diagnostics_available: state.published,
    published_data_report: state.published ? { errors: 0, instance_count: 1 } : null,
  }));
  await page.getByRole("button", { name: "オントロジーを公開", exact: true }).click();
  await expect(page.getByText(/警告がありますが公開できます/)).toBeVisible();
  await expect(page.getByRole("region", { name: "公開前のエラー・警告", exact: true }).getByText(message, { exact: true })).toHaveCount(1);
  await confirmPreparedPublish(page);
  await expect(page.getByText("オントロジーを公開しました。", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "公開済み Markdown オントロジー", exact: true }).click();
  const saved = page.getByRole("region", { name: "公開版 v4 の検証記録", exact: true });
  await expect(saved.getByText(message, { exact: true })).toBeVisible();
  await page.reload();
  await loadOntologyBuildWorkspace(page);
  await page.getByRole("tab", { name: "公開済み Markdown オントロジー", exact: true }).click();
  await expect(saved.getByText(message, { exact: true })).toHaveCount(1);
  await saved.locator("summary").first().press("Enter");
  await expect(saved.getByText("property-39 / evidence / INFERENCE_WITHOUT_EVIDENCE", { exact: true })).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await saved.getByRole("button", { name: "検証記録をダウンロード", exact: true }).click();
  const download = await downloadPromise;
  const path = testInfo.outputPath("diagnostics.json");
  await download.saveAs(path);
  const { readFile } = await import("node:fs/promises");
  const record = JSON.parse(await readFile(path, "utf-8"));
  expect(record.findings).toEqual(findings);
  expect(record.data_report).toEqual({ errors: 0, instance_count: 1 });
  expect(record.snapshot_id).toBe("revision-draft-4");
  for (const width of testInfo.project.name === "desktop" ? [1280, 1920] : [375]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await saved.screenshot({ path: testInfo.outputPath(`published-diagnostics-${width}-light.png`) });
  }
});

for (const operationalWarning of [false, true]) {
  test(`構築の資料不足は Markdown の補足に保持する (${operationalWarning})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    const state = await mockApi(page);
    state.jobPolls = 2;
    const notes = [
      "business_text_chunks はすべて検証エラー文のみで正の業務記述なし",
      ...["department_id", "employee_id", "employee_name", "email", "hire_date", "salary"].map(
        field => `${field} / evidence: 証拠の資料・位置・原文を照合できません。`
      ),
    ];
    state.draftMarkdown = generatedDraftMarkdown + "\n\n## 記述範囲と補足\n" + notes.map(note => `- ${note}`).join("\n");
    const payload = buildJob("succeeded", "succeeded");
    payload.job.markdown_output = state.draftMarkdown;
    payload.job.warnings_ja = [...notes, ...(operationalWarning ? ["資料の一部で抽出に失敗しました。"] : [])];
    await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", route => fulfillJson(route, { jobs: [payload.job] }));
    await page.route("**/api/nl2sql/ontology-build/*", route => fulfillJson(route, payload));
    await page.goto("/ontology-build?profile=default");
    await expect(page.getByTestId("ontology-view-fetch")).toBeVisible();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "本文へスキップ" })).toBeFocused();
    await loadOntologyBuildWorkspace(page);
    const progress = page.getByTestId("ontology-build-steps");
    const summary = progress.locator("summary").filter({ hasText: "警告" });
    await expect(summary).toHaveCount(operationalWarning ? 1 : 0);
    if (operationalWarning) {
      await expect(summary).toContainText("(1)");
      await summary.press("Enter");
      await expect(progress.getByText("資料の一部で抽出に失敗しました。", { exact: true })).toBeVisible();
    }
    for (const note of notes) await expect(progress.getByText(note, { exact: true })).toHaveCount(0);
    const markdown = page.getByTestId("ontology-build-markdown");
    const draftTab = markdown.getByRole("tab", { name: "Markdown オントロジー下書き" });
    await draftTab.click();
    const editor = markdown.getByTestId("ontology-markdown-draft-editor");
    await expect(editor).toHaveValue(state.draftMarkdown);
    await draftTab.press("End");
    await expect(markdown.getByRole("tab", { name: "公開済み Markdown オントロジー" })).toBeFocused();
    await page.keyboard.press("Home");
    await expect(draftTab).toBeFocused();
    for (const width of testInfo.project.name === "desktop" ? [1280, 1920] : [375]) {
      await page.setViewportSize({ width, height: 900 });
      await editor.scrollIntoViewIfNeeded();
      await editor.focus();
      await expect(editor).toBeFocused();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`content-notes-${width}-light.png`) });
    }
  });
}


test("従業員 Markdown は業務内容だけを整形表示して公開できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const content = readFileSync(new URL("../fixtures/ontology-employee.md", import.meta.url), "utf8");
  const state = await mockApi(page);
  state.jobPolls = 2;
  state.draftMarkdown = content;
  await page.goto("/ontology-build?profile=default");
  await expect(page.getByTestId("ontology-view-fetch")).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "本文へスキップ" })).toBeFocused();
  await loadOntologyBuildWorkspace(page);
  const markdown = page.getByTestId("ontology-build-markdown");
  const draft = markdown.getByRole("tab", { name: "Markdown オントロジー下書き" });
  await draft.click();
  const editor = markdown.getByTestId("ontology-markdown-draft-editor");
  await expect(editor).toHaveValue(content);
  expect(content).not.toMatch(/profile_concept_|source_id|verified|evidence|検証不能/);
  for (const field of ["従業員番号", "氏名", "所属部門番号", "メールアドレス", "入社日", "給与"]) {
    expect(content).toContain(`##### ${field}`);
  }
  for (const width of testInfo.project.name === "desktop" ? [1280, 1920] : [375]) {
    await page.setViewportSize({ width, height: 900 });
    await editor.scrollIntoViewIfNeeded();
    await editor.focus();
    await expect(editor).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`clean-employee-${width}-light.png`) });
  }
  await draft.press("End");
  await expect(markdown.getByRole("tab", { name: "公開済み Markdown オントロジー" })).toBeFocused();
  await page.keyboard.press("Home");
  await expect(draft).toBeFocused();
  await page.getByRole("button", { name: "オントロジーを公開", exact: true }).click();
  await confirmPreparedPublish(page);
  await expect.poll(() => state.published).toBe(true);
  await markdown.getByRole("tab", { name: "公開済み Markdown オントロジー" }).click();
  await expect(markdown.getByTestId("ontology-markdown-published-viewer")).toContainText("従業員番号");
});

test("公開確認は標準の折りたたみ・フォーム・状態表示を使用する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const state = await mockApi(page);
  state.jobPolls = 2;
  state.draftMarkdown = generatedDraftMarkdown;
  const titles = ["従業員情報", "従業員ID（主キー）", "メールアドレス", "所属部署ID（外部キー）", "従業員氏名", "入社日", "給与"];
  const definitions = titles.map((name_ja, index) => ({ kind: "property", id: `field-${index}`, api_name: `field_${index}`, name_ja, data_type: "number" }));
  const preparation = {
    id: "ui-review", status: "ready", draft_etag: state.draftMarkdownEtag, expected_head: "", display_version: 4,
    findings: [
      ...Array.from({ length: 14 }, (_, index) => ({ severity: "warning", code: "EVIDENCE_UNRESOLVED", definition_id: `Employee.${index}`, field: "evidence", message_ja: "証拠の資料・位置・原文を照合できません。" })),
      { severity: "warning", code: "DEFINITION_CONFLICT", definition_id: "field-1", field: "data_type", message_ja: "『従業員ID（主キー）』の業務の型が 2 通り（整数（integer） と 数値（decimal））作られました。整数（integer） を使います。違う場合は Markdown の『従業員ID（主キー）』の「業務の型」の行を直してください。" },
    ],
    definitions,
    // 2 回目の公開: 1 件は前の公開版から型が変わり（警告あり）、ほかは問題の無い追加。
    differences: definitions.map((after, index) => ({ id: `field-${index}`, before: index === 1 ? { ...after, data_type: "integer" } : null, after })),
    error_message_ja: "",
  };
  await page.route("**/ontology-markdown/prepare", route => fulfillJson(route, preparation));
  await page.route("**/ontology-markdown/preparations/*", route => fulfillJson(route, preparation));
  let validationCalls = 0;
  await page.route("**/ontology-markdown/preparations/*/validate-data", route => {
    validationCalls += 1;
    expect(route.request().postDataJSON()).toEqual({ confirmed: true, acceptance_cases: [] });
    return fulfillJson(route, {});
  });
  await page.goto("/ontology-build?profile=default");
  await expect(page.getByTestId("ontology-view-fetch")).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "本文へスキップ" })).toBeFocused();
  await loadOntologyBuildWorkspace(page);
  await page.getByRole("button", { name: "オントロジーを公開", exact: true }).click();
  const check = page.getByRole("region", { name: "公開前の確認", exact: true });
  await expect(check.getByText("公開すると何が変わるかを確かめる所です。", { exact: false })).toBeVisible();
  // 概念ごとの差分は確認が要る変更だけを上に出し、問題の無い変更は「問題なし N 件」に畳む（#1729）。
  const changes = check.getByRole("region", { name: "概念ごとの差分", exact: true });
  await expect(changes.getByTestId("ontology-publication-change-summary")).toHaveText("追加 6 件・変更 1 件・削除 0 件");
  const review = changes.getByRole("list", { name: "確認が要る変更", exact: true });
  await expect(review.getByTestId("ontology-publication-difference")).toHaveCount(1);
  await expect(review.getByText("従業員ID（主キー）", { exact: true }).first()).toBeVisible();
  await expect(review.getByText("業務の型を変更します。", { exact: true })).toBeVisible();
  await expect(review.getByText(/整数（integer） を使います。/)).toBeVisible();
  // 内部の ID・診断コードは既定で出さず、「技術的な詳細」に畳む。
  await expect(changes.getByText("DEFINITION_CONFLICT")).toBeHidden();
  const unchanged = changes.getByTestId("ontology-publication-unchanged-issues");
  const unchangedSummary = unchanged.locator("summary").first();
  await expect(unchangedSummary).toContainText("問題なし");
  await expect(unchangedSummary).toContainText("6 件");
  await expect(unchanged.getByTestId("ontology-publication-difference").first()).toBeHidden();
  await unchangedSummary.focus();
  await unchangedSummary.press("Enter");
  await expect(unchanged.getByTestId("ontology-publication-difference")).toHaveCount(6);
  const details = review.getByText("技術的な詳細", { exact: true });
  await details.click();
  await expect(review.getByRole("heading", { name: "変更前", exact: true })).toBeVisible();
  await expect(review).toContainText("integer");
  await expect(review).toContainText("DEFINITION_CONFLICT");
  const validation = check.getByTestId("ontology-publication-data-validation");
  const summary = validation.locator("summary").first();
  await summary.press("Space");
  const input = validation.getByRole("textbox", { name: "受入テスト（JSON 配列）" });
  await expect(input).toBeVisible();
  await input.fill("{bad");
  const validate = validation.getByRole("button", { name: "実データの検証", exact: true });
  await validate.click();
  await expect(input).toHaveAttribute("aria-invalid", "true");
  await expect(validation.getByText("有効な JSON 配列を入力してください。", { exact: true })).toBeVisible();
  expect(validationCalls).toBe(0);
  await input.fill("[]");
  for (const width of testInfo.project.name === "desktop" ? [1280, 1920] : [375]) {
    await page.setViewportSize({ width, height: 1000 });
    await validation.scrollIntoViewIfNeeded();
    await input.focus();
    await expect(input).toBeFocused();
    expect(await input.evaluate(element => getComputedStyle(element).boxShadow)).not.toBe("none");
    for (const disclosure of [unchangedSummary, summary]) {
      expect(await disclosure.evaluate(element => getComputedStyle(element).listStyleType)).toBe("none");
      await expect(disclosure.locator("svg").last()).toHaveAttribute("aria-hidden", "true");
      expect((await disclosure.boundingBox())!.height).toBeGreaterThanOrEqual(await disclosure.evaluate(() => 2.75 * parseFloat(getComputedStyle(document.documentElement).fontSize)));
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`publication-standard-${width}-light.png`) });
    await changes.screenshot({ path: testInfo.outputPath(`publication-changes-second-${width}-light.png`) });
  }
  await validate.click();
  await expect.poll(() => validationCalls).toBe(1);
  await expect(check.getByRole("button", { name: "確認した内容を公開", exact: true })).toBeEnabled();
  await summary.press("Enter");
  await expect(input).toBeHidden();
});

test("公開前の確認は graph の構成と、初回は差分の 1 行だけを示し、補うと精度が上がる項目を畳む (#1720, #1729)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const state = await mockApi(page);
  state.jobPolls = 2;
  state.draftMarkdown = generatedDraftMarkdown;
  const object = (api_name: string, name_ja: string, table: string, properties: string[]) =>
    ({ kind: "object_type", api_name, name_ja, properties, mappings: [{ owner: "ADMIN", object_name: table, column_name: "" }] });
  const property = (api_name: string, name_ja: string, object_type: string) => ({ kind: "property", api_name, name_ja, object_type });
  const definitions = [
    object("department", "部門", "DEPARTMENT", ["department_name"]),
    object("employee", "従業員", "EMPLOYEE", ["employee_name"]),
    object("v_emp_dept", "従業員部門ビュー", "V_EMP_DEPT", ["v_emp_dept_department_name"]),
    property("department_name", "部門名", "department"),
    property("employee_name", "従業員名", "employee"),
    property("v_emp_dept_department_name", "部門名", "v_emp_dept"),
    { kind: "link_type", api_name: "employee_department", name_ja: "所属", source: "employee", target: "department" },
    { kind: "value_type", api_name: "string_type", name_ja: "文字列型" },
  ];
  const identity = "主キー（1 行を特定する列）が分かりません。分かれば Markdown のオブジェクト型に「主キー」を書くと、件数・重複の判定が正確になります（何もしなくても公開できます）。";
  const preparation = {
    id: "best-effort", status: "ready", draft_etag: state.draftMarkdownEtag, expected_head: "", display_version: 4,
    findings: [{ severity: "info", code: "OBJECT_IDENTITY_REQUIRED", definition_id: "v_emp_dept", field: "primary_key", message_ja: identity }],
    definitions,
    differences: definitions.map((after, index) => ({ id: `d-${index}`, before: null, after })),
    suggestions: [{ severity: "info", text: "従業員部門ビュー（v_emp_dept）は ADMIN.V_EMP_DEPT のビューで、…" }],
    error_message_ja: "",
  };
  await page.route("**/ontology-markdown/prepare", route => fulfillJson(route, preparation));
  await page.route("**/ontology-markdown/preparations/*", route => fulfillJson(route, preparation));
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  await page.getByRole("button", { name: "オントロジーを公開", exact: true }).click();
  const check = page.getByRole("region", { name: "公開前の確認", exact: true });
  // info だけなら公開でき、警告の帯や「公開前のエラー・警告」の一覧・改善案を出さない。
  await expect(check.getByText("公開できます", { exact: true })).toBeVisible();
  await expect(page.getByText(/警告がありますが公開できます/)).toHaveCount(0);
  await expect(page.getByRole("region", { name: "公開前のエラー・警告", exact: true })).toHaveCount(0);
  await expect(check.getByTestId("ontology-improvement-suggestion")).toHaveCount(0);
  const graph = check.getByTestId("ontology-publication-graph");
  for (const [label, value] of [["オブジェクト型（Object Type）", "3"], ["リンク型（Link Type）", "1"], ["プロパティ（Property）", "3"]]) {
    await expect(graph.getByText(label, { exact: true }).first().locator("xpath=following-sibling::dd")).toHaveText(value);
  }
  await expect(graph.getByText("ADMIN.V_EMP_DEPT", { exact: true })).toBeVisible();
  const objects = graph.getByRole("list", { name: "オブジェクト型の一覧", exact: true });
  await expect(objects.locator(":scope > li")).toHaveCount(3);
  await expect(objects.getByText("プロパティ 1 件・リンク型 1 件", { exact: true })).toHaveCount(2);
  const improvements = check.getByTestId("ontology-improvement-findings");
  await expect(improvements.getByText(identity)).toBeHidden();
  await improvements.locator("summary").first().press("Enter");
  await expect(improvements.getByText(identity)).toBeVisible();
  // 対象は業務の名前で出し、ID・項目・診断コードは技術的な詳細に畳む（#1729）。
  await expect(improvements.getByText("対象: 従業員部門ビュー", { exact: true })).toBeVisible();
  await expect(improvements.getByText("v_emp_dept / primary_key / OBJECT_IDENTITY_REQUIRED", { exact: true })).toBeHidden();
  await improvements.getByText("技術的な詳細", { exact: true }).click();
  await expect(improvements.getByText("v_emp_dept / primary_key / OBJECT_IDENTITY_REQUIRED", { exact: true })).toBeVisible();
  // 初回の公開は「追加」のバッジを並べず、件数の 1 行と確認が要るものだけにする。値型は数えない。
  const changes = check.getByRole("region", { name: "概念ごとの差分", exact: true });
  await expect(changes.getByTestId("ontology-publication-change-summary")).toHaveText("初めての公開です。7 件の概念を追加します。");
  await expect(changes.getByText("確認が要る変更はありません。このまま公開できます。", { exact: true })).toBeVisible();
  await expect(changes.getByTestId("ontology-publication-difference")).toHaveCount(0);
  await expect(changes.getByText("追加", { exact: true })).toHaveCount(0);
  await expect(changes.getByTestId("ontology-publication-unchanged-issues")).toHaveCount(0);
  await expect(check.getByText("文字列型")).toHaveCount(0);
  await expect(check.getByText(/profile_concept_/)).toHaveCount(0);
  await expect(check.getByRole("button", { name: "確認した内容を公開", exact: true })).toBeEnabled();
  for (const width of testInfo.project.name === "desktop" ? [1280, 1920] : [375]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await check.screenshot({ path: testInfo.outputPath(`publication-best-effort-${width}-light.png`) });
  }
});

test("公開を止める問題があるときは、業務説明に追記する改善案をコピーし、押したときだけ追記する (#1729)", { tag: "@responsive" }, async ({ context, page }, testInfo) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: `http://127.0.0.1:${process.env.PLAYWRIGHT_PORT ?? "3101"}`,
  });
  const state = await mockApi(page);
  state.jobPolls = 2;
  state.draftMarkdown = generatedDraftMarkdown;
  const first = "メモ（memo）は（対応する表・ビューの名前を確認して書いてください）の表で、（1 行が何を表すかを確認して書いてください）、主キーは（1 行を特定する列を確認して書いてください）。";
  const second = "従業員（employee）は ADMIN.EMPLOYEE の表で、1 行が 1 つの従業員、主キーは EMPLOYEE_ID、DEPARTMENT_ID で部門に属する（多対 1（many-to-one））。";
  const preparation = {
    id: "blocked", status: "failed", draft_etag: state.draftMarkdownEtag, expected_head: "", display_version: 4,
    findings: [
      { severity: "error", code: "OBJECT_MAPPING_REQUIRED", definition_id: "memo", field: "mappings", message_ja: "対応する表・ビューが決まっていません。業務プロファイルに表・ビューを足すか、Markdown のこのオブジェクト型に「対応する表・列」を書いてください。" },
      { severity: "info", code: "OBJECT_GRAIN_REQUIRED", definition_id: "employee", field: "grain_ja", message_ja: "粒度（1 行が何を表すか）が書かれていません。" },
    ],
    definitions: [
      { kind: "object_type", api_name: "memo", id: "memo", name_ja: "メモ" },
      { kind: "object_type", api_name: "employee", id: "employee", name_ja: "従業員", mappings: [{ owner: "ADMIN", object_name: "EMPLOYEE" }] },
    ],
    differences: [],
    suggestions: [{ severity: "error", text: first }, { severity: "info", text: second }],
    error_message_ja: "",
  };
  await page.route("**/ontology-markdown/prepare", route => fulfillJson(route, preparation));
  await page.route("**/ontology-markdown/preparations/*", route => fulfillJson(route, preparation));
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  const businessText = page.getByRole("textbox", { name: "業務説明(自然言語)", exact: true });
  await businessText.fill("受注の業務です。");
  await page.getByRole("button", { name: "オントロジーを公開", exact: true }).click();
  const check = page.getByRole("region", { name: "公開前の確認", exact: true });
  const banner = check.getByText(/公開を止める問題があります。/);
  await expect(banner).toBeVisible();
  const panel = check.getByRole("region", { name: "業務説明に追記する改善案", exact: true });
  const text = panel.getByTestId("ontology-improvement-suggestion-text");
  await expect(text).toHaveText(`${first}\n${second}`);
  // Banner の下、公開を止める問題の一覧の上に置く。
  const list = check.getByRole("region", { name: "公開前のエラー・警告", exact: true });
  const [bannerBox, panelBox, listBox] = await Promise.all([banner.boundingBox(), panel.boundingBox(), list.boundingBox()]);
  expect(bannerBox!.y).toBeLessThan(panelBox!.y);
  expect(panelBox!.y + panelBox!.height).toBeLessThanOrEqual(listBox!.y);
  // 押すまで業務説明は書き換えない（#1727）。
  await expect(businessText).toHaveValue("受注の業務です。");
  await panel.getByRole("button", { name: "コピー", exact: true }).click();
  await expect(panel.getByRole("button", { name: "コピー", exact: true })).toHaveAttribute("data-copy-state", "copied");
  await expect(panel.getByRole("status").filter({ hasText: "コピーしました" })).toHaveCount(1);
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(`${first}\n${second}`);
  for (const width of testInfo.project.name === "desktop" ? [1280, 1920] : [375]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath(`publication-suggestion-${width}-light.png`) });
  }
  await panel.getByRole("button", { name: "業務説明に追記", exact: true }).click();
  await expect(businessText).toHaveValue(`受注の業務です。\n${first}\n${second}`);
  await expect(businessText).toBeFocused();
  await expect(page.getByText(/業務説明に追記しました。/)).toBeVisible();
  // 追記した文は重ねて出さない。
  await expect(panel).toHaveCount(0);
});

test("一意でない列でつなぐリンク型の警告は具体の結合のキーを示し、改善案は押したときだけ業務説明に追記する (#1760)", { tag: "@responsive" }, async ({ context, page }, testInfo) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: `http://127.0.0.1:${process.env.PLAYWRIGHT_PORT ?? "3101"}`,
  });
  const state = await mockApi(page);
  state.jobPolls = 2;
  state.draftMarkdown = generatedDraftMarkdown;
  const join = "ADMIN.V_EMP_DEPT.EMPLOYEE_ID = ADMIN.EMPLOYEE.EMPLOYEE_ID";
  const warning = `主キー・一意キーではない列（V_EMP_DEPT.EMPLOYEE_NAME、EMPLOYEE.EMPLOYEE_NAME）でつないでいます。行が重なって件数が増えることがあるため、「結合のキー」を EMPLOYEE_ID でつなぐ形（${join}）に直してください。`;
  const suggestion = `従業員部門ビュー（emp_dept_view）と従業員（employee）のリンク型『従業員部門ビューの従業員』（emp_dept_view_to_employee）は、${join} でつなぐ（1 対 1（one-to-one））。`;
  const preparation = {
    id: "nonunique-link", status: "ready", draft_etag: state.draftMarkdownEtag, expected_head: "", display_version: 4,
    findings: [{ severity: "warning", code: "LINK_JOIN_NOT_UNIQUE", definition_id: "link-emp-dept-view", field: "join_expression_sql", message_ja: warning }],
    definitions: [
      { kind: "object_type", api_name: "employee", id: "employee", name_ja: "従業員", mappings: [{ owner: "ADMIN", object_name: "EMPLOYEE" }] },
      { kind: "object_type", api_name: "emp_dept_view", id: "emp_dept_view", name_ja: "従業員部門ビュー", mappings: [{ owner: "ADMIN", object_name: "V_EMP_DEPT" }] },
      { kind: "link_type", api_name: "emp_dept_view_to_employee", id: "link-emp-dept-view", name_ja: "従業員部門ビューの従業員", source: "emp_dept_view", target: "employee", join_expression_sql: "ADMIN.V_EMP_DEPT.EMPLOYEE_NAME = ADMIN.EMPLOYEE.EMPLOYEE_NAME" },
    ],
    differences: [],
    suggestions: [{ severity: "warning", text: suggestion }],
    error_message_ja: "",
  };
  await page.route("**/ontology-markdown/prepare", route => fulfillJson(route, preparation));
  await page.route("**/ontology-markdown/preparations/*", route => fulfillJson(route, preparation));
  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  const businessText = page.getByRole("textbox", { name: "業務説明(自然言語)", exact: true });
  await businessText.fill("従業員の業務です。");
  await page.getByRole("button", { name: "オントロジーを公開", exact: true }).click();
  const check = page.getByRole("region", { name: "公開前の確認", exact: true });
  // 警告だけなら公開でき、警告の 2 文に具体の結合のキーを出す。対象は業務の名前、コードは技術的な詳細。
  await expect(check.getByText(/警告がありますが公開できます/)).toBeVisible();
  const list = check.getByRole("region", { name: "公開前のエラー・警告", exact: true });
  await expect(list.getByText(warning, { exact: true })).toBeVisible();
  await expect(list.getByText("対象: 従業員部門ビューの従業員", { exact: true })).toBeVisible();
  const technical = "link-emp-dept-view / join_expression_sql / LINK_JOIN_NOT_UNIQUE";
  await expect(list.getByText(technical, { exact: true })).toBeHidden();
  await list.getByText("技術的な詳細", { exact: true }).click();
  await expect(list.getByText(technical, { exact: true })).toBeVisible();
  // 改善案にリンク型の正しい結合のキーと多重度を出す。押すまで業務説明は書き換えない（#1727）。
  const panel = check.getByRole("region", { name: "業務説明に追記する改善案", exact: true });
  await expect(panel.getByTestId("ontology-improvement-suggestion-text")).toHaveText(suggestion);
  await expect(businessText).toHaveValue("従業員の業務です。");
  await panel.getByRole("button", { name: "コピー", exact: true }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(suggestion);
  await expect(businessText).toHaveValue("従業員の業務です。");
  for (const width of testInfo.project.name === "desktop" ? [1280, 1920] : [375]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await check.screenshot({ path: testInfo.outputPath(`publication-link-suggestion-${width}-light.png`) });
  }
  await panel.getByRole("button", { name: "業務説明に追記", exact: true }).click();
  await expect(businessText).toHaveValue(`従業員の業務です。\n${suggestion}`);
  await expect(page.getByText(/業務説明に追記しました。/)).toBeVisible();
  await expect(panel).toHaveCount(0);
});

test("取得済みの業務プロファイルの再取得が失敗しても、構築の作業領域と選んだ資料を残す", async ({ page }, testInfo) => {
  await mockApi(page);
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [buildJob("running", "running").job] })
  );
  await page.route("**/api/nl2sql/ontology-build/*", (route) =>
    fulfillJson(route, buildJob("running", "running"))
  );
  let failProfileDetail = false;
  await page.route(/\/api\/nl2sql\/profiles\/[^/?]+$/, async (route) => {
    if (failProfileDetail) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ data: null, error_messages: ["一時的な接続エラー"] }),
      });
      return;
    }
    await fulfillJson(route, profiles[0]);
  });

  await page.goto("/ontology-build?profile=default");
  await loadOntologyBuildWorkspace(page);
  await expect(page.getByTestId("ontology-build-steps")).toHaveAttribute("data-job-status", "running");
  await dropFiles(page, page.getByTestId("ontology-build-source-files-dropzone"), [
    { name: "rules.md", type: "text/markdown", content: "# 受注ルール", lastModified: 1_700_000_000_000 },
  ]);
  const selectedFiles = page.getByRole("list", { name: "選択した構築資料" });
  await expect(selectedFiles).toContainText("rules.md");

  // 「オントロジーを取得」での取り直しが一時的に失敗する。
  failProfileDetail = true;
  await page.getByTestId("ontology-view-fetch").click();
  await expect(page.getByText("一時的な接続エラー", { exact: false }).first()).toBeVisible();

  // 構築の作業領域（実行中の job の進行・選んだ資料）は消さない。
  await expect(page.getByTestId("profile-ontology-build")).toBeVisible();
  await expect(page.getByTestId("ontology-build-steps")).toHaveAttribute("data-job-status", "running");
  await expect(selectedFiles).toContainText("rules.md");
  // 案内は取得の操作（対象プロファイルの欄）の中に出す（messaging.md §10）。
  const profilePanel = page.getByRole("region", { name: "対象プロファイル" });
  await expect(profilePanel.getByText("一時的な接続エラー", { exact: false })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("ontology-profile-refresh-failed.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  // 失敗の案内の「再試行」で取り直せる。
  failProfileDetail = false;
  await profilePanel.getByRole("button", { name: "再試行", exact: true }).click();
  await expect(page.getByText("一時的な接続エラー", { exact: false })).toHaveCount(0);
  await expect(selectedFiles).toContainText("rules.md");
});

test("業務プロファイルの追加読み込みが失敗しても、選択欄を残して追加読み込みだけを再試行できる", async ({ page }) => {
  // 失敗の表示までの TanStack Query の再試行（3 回）を page.clock で進める（#1593）。
  await page.clock.install();
  await mockApi(page);
  const secondPageProfile = { ...profiles[0], id: "sales", name: "SALES_PROFILE", category: "販売" };
  let failNextPage = true;
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    const summary = (profile: typeof profiles[number]) => ({
      id: profile.id,
      name: profile.name,
      category: profile.category,
      description: profile.description,
      archived: false,
      allowed_table_count: profile.allowed_tables.length,
      allowed_view_count: profile.allowed_views.length,
      glossary_count: 0,
      few_shot_count: 0,
      version: 1,
      etag: `etag-${profile.id}`,
      updated_at: "2026-07-12T00:00:00Z",
    });
    if (!cursor) {
      await fulfillJson(route, {
        items: [summary(profiles[0])],
        next_cursor: "page-2",
        total: 2,
        change_token: 1,
      });
      return;
    }
    if (failNextPage) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ data: null, error_messages: ["一時的な接続エラー"] }),
      });
      return;
    }
    await fulfillJson(route, { items: [summary(secondPageProfile)], next_cursor: null, total: 2, change_token: 1 });
  });

  await page.goto("/ontology-build?profile=default");
  const profilePanel = page.getByRole("region", { name: "対象プロファイル" });
  await expect(page.getByTestId("ontology-build-profile-select")).toBeVisible();
  await profilePanel.getByRole("button", { name: "さらに読み込む", exact: true }).click();

  // 追加読み込みの失敗は、読み込み済みの選択欄を残し、追加読み込みの位置に出す。
  await pollWithClock(page, () => profilePanel.getByText("一時的な接続エラー", { exact: false }).count(), 2_000).toBe(1);
  await expect(page.getByTestId("ontology-build-profile-select")).toBeVisible();
  await expect(page.getByTestId("ontology-view-fetch")).toBeVisible();

  failNextPage = false;
  await profilePanel.getByRole("button", { name: "再試行", exact: true }).click();
  await expect(profilePanel.getByText("一時的な接続エラー", { exact: false })).toHaveCount(0);
  await chooseSelectFieldOption(page.getByTestId("ontology-build-profile-select"), "sales");
  await expect(page).toHaveURL(/profile=sales/);
});

test("オントロジー構築は 1280 / 1920 / 375pxで欄の幅と placeholder が崩れない", { tag: "@responsive" }, async ({ page }) => {
  // 業務プロファイルの選択は lg、業務の説明は FormGrid の full、質問は full（#1891）。
  test.slow();
  await mockProfileScopedApi(page);
  const widths = test.info().project.name === "mobile-375" ? [375] : [1280, 1920];
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/ontology-build?profile=sales");
    await expectSelectFieldValue(page.getByTestId("ontology-build-profile-select"), "sales");
    await loadOntologyBuildWorkspace(page);
    await expect(page.locator("#ontology-build-business-text")).toBeVisible();
    const label = `ontology build ${width}px`;
    expect(await measureHorizontalOverflow(page), label).toBeLessThanOrEqual(1);
    expect(await findClippedPlaceholders(page), label).toEqual([]);
    if (width >= 640) {
      // グラフ内の強調の検索（ツールバーの専用の枠。page-archetypes の例外）は段の外のまま。
      const offScale = (await findFieldsOffWidthScale(page)).filter((field) => field.name !== "グラフ内を検索");
      expect(offScale, label).toEqual([]);
    }
  }
});
