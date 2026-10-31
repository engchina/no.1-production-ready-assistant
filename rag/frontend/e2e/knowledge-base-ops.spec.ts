import { expect, type Page, test } from "./fixtures/test";

import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

// #302: 検索・回答プロファイルの参照 KB の警告（アーカイブ済み・見つからない）と、KB の名前・説明の編集。

type KnowledgeBaseStatus = "ACTIVE" | "ARCHIVED";

interface KnowledgeBaseFixture {
  id: string;
  name: string;
  description: string | null;
  status: KnowledgeBaseStatus;
}

const envelope = (data: unknown) => ({ json: { data, error_messages: [], warning_messages: [] } });

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

test("検索・回答プロファイルの一覧は、参照 KB にアーカイブ済み・見つからないものがある行を要確認で示す", async ({
  page,
}) => {
  await mockKnowledgeBases(page, defaultKnowledgeBases());
  await mockSearchAnswerProfiles(page, [
    searchAnswerProfile("bv-1", "経理ビュー", ["kb-1", "kb-old", "kb-gone"]),
    searchAnswerProfile("bv-2", "人事ビュー", ["kb-1"]),
  ]);

  await page.goto("/search-answer-profiles");

  await expect(
    page.getByText("参照するナレッジベースにアーカイブ済み・見つからないものがある検索・回答プロファイルがあります")
  ).toBeVisible();
  const issueRow = page.getByTestId("search-answer-profile-row-bv-1");
  await expect(issueRow.getByText("要確認")).toBeVisible();
  await expect(issueRow.getByText("参照 KB 3 件のうち、アーカイブ済み 1 件・見つからない 1 件")).toBeAttached();
  await expect(page.getByTestId("search-answer-profile-row-bv-2").getByText("要確認")).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("検索・回答プロファイルの編集画面は、検索されない参照 KB を名前付きで警告し、外すと警告が消える", async ({
  page,
}) => {
  await mockKnowledgeBases(page, defaultKnowledgeBases());
  let patchBody: Record<string, unknown> | null = null;
  await mockSearchAnswerProfiles(
    page,
    [searchAnswerProfile("bv-1", "経理ビュー", ["kb-1", "kb-old", "kb-gone"])],
    (body) => {
      patchBody = body;
    }
  );

  await page.goto("/search-answer-profiles?id=bv-1");

  const banner = page.getByTestId("search-answer-profile-kb-issues");
  await expect(page.getByText("参照するナレッジベースの一部が検索対象になっていません")).toBeVisible();
  await expect(banner.getByText("アーカイブ済み（1 件）: 旧規程")).toBeVisible();
  await expect(banner.getByText(/見つからない（1 件。.*）: kb-gone/)).toBeVisible();
  // チップにも状態を文字で添える（色だけに頼らない）。
  const archivedChip = page.getByLabel("旧規程 を選択から外す").locator("..");
  await expect(archivedChip).toContainText("アーカイブ済み");
  const missingChip = page.getByLabel("不明なナレッジベース（kb-gone） を選択から外す").locator("..");
  await expect(missingChip).toContainText("見つかりません");
  await expectNoPageOverflow(page);

  await page.getByLabel("旧規程 を選択から外す").click();
  await page.getByLabel("不明なナレッジベース（kb-gone） を選択から外す").click();
  await expect(page.getByTestId("search-answer-profile-kb-issues")).toHaveCount(0);

  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();
  await expect
    .poll(() => (patchBody?.config as { knowledge_base_ids?: string[] })?.knowledge_base_ids)
    .toEqual(["kb-1"]);
});

test("参照 KB がすべて検索されない検索・回答プロファイルは、結果が 0 件になることを伝える", async ({ page }) => {
  await mockKnowledgeBases(page, defaultKnowledgeBases());
  await mockSearchAnswerProfiles(page, [searchAnswerProfile("bv-1", "旧ビュー", ["kb-old"])]);

  await page.goto("/search-answer-profiles?id=bv-1");

  await expect(
    page.getByText(
      "参照するすべてのナレッジベースが検索対象外のため、この検索・回答プロファイルの検索・回答は結果が 0 件になります。"
    )
  ).toBeVisible();
});

// #555: 名前・説明は詳細の「基本情報」の欄でそのまま編集し、PageHeader の「保存する」で保存する（検索・回答プロファイルと同じ）。
test("ナレッジベースの詳細で名前と説明を編集でき、同名は名前の欄に理由を出す", async ({ page }) => {
  const knowledgeBases = defaultKnowledgeBases();
  const patches: Record<string, unknown>[] = [];
  await mockKnowledgeBases(page, knowledgeBases, patches);
  await mockDocuments(page);

  await page.goto("/knowledge-bases/kb-1");
  await expect(page.getByRole("heading", { name: "社内規程", level: 1 })).toBeVisible();

  const form = page.getByTestId("knowledge-base-form");
  const name = form.getByLabel("名前");
  await expect(name).toHaveValue("社内規程");
  await expectNoPageOverflow(page);

  // 同じ名前の KB があるときは 409 の理由を名前の欄に出し、編集を続けられる。
  await name.fill("製品 FAQ");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();
  await expect(
    form.getByText("同じ名前のナレッジベース（アーカイブ済みを含む）がすでにあります。", {
      exact: false,
    })
  ).toBeVisible();
  await expect(name).toHaveAttribute("aria-invalid", "true");

  await name.fill("就業規則");
  await form.getByLabel("説明").fill("人事・経費の規程");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();

  await expect(page.getByText("ナレッジベースを更新しました。")).toBeVisible();
  await expect(page.getByRole("heading", { name: "就業規則", level: 1 })).toBeVisible();
  await expect(page.locator("header[data-page-header]").getByText("人事・経費の規程")).toBeVisible();
  expect(patches.at(-1)).toEqual({ name: "就業規則", description: "人事・経費の規程" });
  // 取込で使わない構築設定は送らない（adapter_config は 422 で拒否される）。
  expect(patches.every((patch) => !("adapter_config" in patch))).toBe(true);
  // 保存した値が基準になり、確認なしで一覧へ戻れる。
  await page.getByTestId("editor-back").click();
  await expect(page).toHaveURL(/\/knowledge-bases$/);
});

test("DEFAULT は名前を変えられず、説明だけを保存する", async ({ page }) => {
  const patches: Record<string, unknown>[] = [];
  await mockKnowledgeBases(page, defaultKnowledgeBases(), patches);
  await mockDocuments(page);

  await page.goto("/knowledge-bases/kb-default");
  const form = page.getByTestId("knowledge-base-form");
  await expect(form.getByLabel("名前")).toHaveAttribute("readonly", "");
  await expect(form.getByText("DEFAULT の名前は変更できません。説明だけを編集できます。")).toBeVisible();
  await form.getByLabel("説明").fill("未分類の文書");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();

  await expect(page.getByText("ナレッジベースを更新しました。")).toBeVisible();
  expect(patches.at(-1)).toEqual({ description: "未分類の文書" });
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`説明が空のナレッジベースは保存するときに説明を求め、空白だけでは送らない (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const patches: Record<string, unknown>[] = [];
    await mockKnowledgeBases(page, defaultKnowledgeBases(), patches);
    await mockDocuments(page);

    // 説明が必須になる前に作った KB（説明なし）も読み込める（#521）。
    await page.goto("/knowledge-bases/kb-2");
    await expect(page.getByRole("heading", { name: "製品 FAQ", level: 1 })).toBeVisible();

    const form = page.getByTestId("knowledge-base-form");
    const description = form.getByLabel("説明");
    const save = page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true });
    await expect(description).toHaveAttribute("aria-required", "true");
    // 変更しなくても、説明が空なら送らずに入力を求める。
    await save.click();
    await expect(form.getByText("説明を入力してください。")).toBeVisible();
    await expect(description).toHaveAttribute("aria-invalid", "true");
    await expect(description).toBeFocused();

    await description.fill("  ");
    await save.click();
    await expect(form.getByText("説明を入力してください。")).toBeVisible();
    expect(patches).toEqual([]);

    await description.fill(" 製品の問い合わせ ");
    await save.click();
    await expect(page.getByText("ナレッジベースを更新しました。")).toBeVisible();
    expect(patches.at(-1)).toEqual({ name: "製品 FAQ", description: "製品の問い合わせ" });
    await expectNoPageOverflow(page);
  });
}

test("アーカイブ済みのナレッジベースは名前・説明を読み取り専用にし、保存できない", async ({ page }) => {
  await mockKnowledgeBases(page, defaultKnowledgeBases());
  await mockDocuments(page);

  await page.goto("/knowledge-bases/kb-old");
  await expect(page.getByRole("heading", { name: "旧規程", level: 1 })).toBeVisible();
  const form = page.getByTestId("knowledge-base-form");
  await expect(form.getByLabel("名前")).toHaveAttribute("readonly", "");
  await expect(form.getByLabel("説明")).toHaveAttribute("readonly", "");
  await expect(page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true })).toBeDisabled();
});

function defaultKnowledgeBases(): KnowledgeBaseFixture[] {
  return [
    { id: "kb-default", name: "DEFAULT", description: null, status: "ACTIVE" },
    { id: "kb-1", name: "社内規程", description: "経費・人事", status: "ACTIVE" },
    { id: "kb-2", name: "製品 FAQ", description: null, status: "ACTIVE" },
    { id: "kb-old", name: "旧規程", description: null, status: "ARCHIVED" },
  ];
}

function summary(item: KnowledgeBaseFixture) {
  return {
    ...item,
    default_search_mode: "hybrid",
    document_count: 1,
    indexed_document_count: 1,
    error_document_count: 0,
    searchable_chunk_count: 2,
    created_at: "2026-06-15T00:00:00Z",
    updated_at: "2026-06-15T00:00:00Z",
    archived_at: item.status === "ARCHIVED" ? "2026-06-16T00:00:00Z" : null,
  };
}

function detail(item: KnowledgeBaseFixture) {
  return {
    ...summary(item),
    retrieval_config: {},
    adapter_config: { version: 1, ingestion: {}, query: {} },
  };
}

async function mockKnowledgeBases(
  page: Page,
  knowledgeBases: KnowledgeBaseFixture[],
  patches: Record<string, unknown>[] = []
) {
  await page.route("**/api/knowledge-bases**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const parts = url.pathname.split("/").filter(Boolean);
    const id = parts[2];
    if (request.method() === "GET" && url.pathname === "/api/knowledge-bases") {
      const status = url.searchParams.get("status");
      const q = url.searchParams.get("q");
      const ids = url.searchParams.getAll("ids");
      const items = knowledgeBases.filter(
        (item) =>
          (ids.length === 0 || ids.includes(item.id)) &&
          (!status || item.status === status) &&
          (!q || item.name.includes(q))
      );
      await route.fulfill(
        envelope({ items: items.map(summary), total: items.length, limit: 50, offset: 0, has_next: false })
      );
      return;
    }
    const knowledgeBase = knowledgeBases.find((item) => item.id === id);
    if (!knowledgeBase) {
      await route.fulfill({ status: 404, json: { data: null, error_messages: ["not found"], warning_messages: [] } });
      return;
    }
    if (request.method() === "PATCH" && parts.length === 3) {
      const body = request.postDataJSON() as { name?: string; description?: string | null };
      patches.push(body);
      if (
        body.name &&
        knowledgeBases.some(
          (item) => item.id !== id && item.name.toLowerCase() === body.name?.toLowerCase()
        )
      ) {
        await route.fulfill({
          status: 409,
          json: {
            data: null,
            error_messages: [
              "同じ名前のナレッジベース（アーカイブ済みを含む）がすでにあります。別の名前を指定してください。",
            ],
            warning_messages: [],
          },
        });
        return;
      }
      if (body.name !== undefined) knowledgeBase.name = body.name;
      if ("description" in body) knowledgeBase.description = body.description ?? null;
      await route.fulfill(envelope(detail(knowledgeBase)));
      return;
    }
    if (request.method() === "GET" && parts.length === 3) {
      await route.fulfill(envelope(detail(knowledgeBase)));
      return;
    }
    await route.fulfill({ status: 404, json: { detail: "not found" } });
  });
}

async function mockDocuments(page: Page) {
  await page.route("**/api/documents**", async (route) => {
    await route.fulfill(envelope({ items: [], total: 0, limit: 10, offset: 0, has_next: false }));
  });
}

function searchAnswerProfile(id: string, name: string, knowledgeBaseIds: string[]) {
  return { id, name, knowledgeBaseIds };
}

async function mockSearchAnswerProfiles(
  page: Page,
  views: { id: string; name: string; knowledgeBaseIds: string[] }[],
  onPatch?: (body: Record<string, unknown>) => void
) {
  const known = new Map(defaultKnowledgeBases().map((item) => [item.id, item]));
  const toDetail = (view: { id: string; name: string; knowledgeBaseIds: string[] }) => {
    const refs = view.knowledgeBaseIds
      .map((kbId) => known.get(kbId))
      .filter((item): item is KnowledgeBaseFixture => Boolean(item))
      .map((item) => ({ id: item.id, name: item.name, status: item.status }));
    const missing = view.knowledgeBaseIds.filter((kbId) => !known.has(kbId));
    return {
      id: view.id,
      name: view.name,
      description: `${view.name}の用途`,
      status: "ACTIVE",
      knowledge_base_count: view.knowledgeBaseIds.length,
      archived_knowledge_base_count: refs.filter((ref) => ref.status === "ARCHIVED").length,
      missing_knowledge_base_count: missing.length,
      created_at: "2026-06-19T00:00:00Z",
      updated_at: "2026-06-19T00:00:00Z",
      archived_at: null,
      config: {
        version: 1,
        knowledge_base_ids: view.knowledgeBaseIds,
        query: {
          guardrail_policy: null,
          evaluation_suite: null,
        },
      },
      knowledge_bases: refs,
      missing_knowledge_base_ids: missing,
    };
  };
  await page.route("**/api/search-answer-profiles**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const id = pathname.split("/")[3];
    if (pathname.endsWith("/domain-keywords")) {
      await route.fulfill(envelope({ search_answer_profile_id: id, keywords: [] }));
      return;
    }
    if (pathname.endsWith("/approved-faq")) {
      await route.fulfill(envelope({ search_answer_profile_id: id, records: [] }));
      return;
    }
    if (pathname.endsWith("/runtime-knowledge")) {
      await route.fulfill(envelope({ search_answer_profile_id: id, terms: [], rules: [] }));
      return;
    }
    const view = views.find((item) => item.id === id);
    if (id && view) {
      if (request.method() === "PATCH") {
        const body = request.postDataJSON() as Record<string, unknown>;
        onPatch?.(body);
        view.knowledgeBaseIds =
          (body.config as { knowledge_base_ids?: string[] })?.knowledge_base_ids ?? view.knowledgeBaseIds;
      }
      await route.fulfill(envelope(toDetail(view)));
      return;
    }
    const items = views.map(toDetail);
    await route.fulfill(envelope({ items, total: items.length, limit: 20, offset: 0, has_next: false }));
  });
}
