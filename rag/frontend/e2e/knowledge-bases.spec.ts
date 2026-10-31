import { expectGrowingList } from "./_list-viewport";
import { expect, type Page, test } from "./fixtures/test";
import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

type KnowledgeBaseStatus = "ACTIVE" | "ARCHIVED";
type SearchMode = "hybrid" | "vector" | "keyword";
type FileStatus = "UPLOADED" | "INGESTING" | "INDEXED" | "ERROR";

interface KnowledgeBaseSummary {
  id: string;
  name: string;
  description: string | null;
  status: KnowledgeBaseStatus;
  default_search_mode: SearchMode;
  document_count: number;
  indexed_document_count: number;
  error_document_count: number;
  searchable_chunk_count: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

interface DocumentSummary {
  id: string;
  file_name: string;
  status: FileStatus;
  category_name: string | null;
  content_type: string | null;
  file_size_bytes: number | null;
  content_sha256: string | null;
  duplicate_of_document_id: string | null;
  uploaded_at: string;
  indexed_at: string | null;
  knowledge_bases: { id: string; name: string }[];
}

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

test("ナレッジベース一覧は DEFAULT と名前の自然順をページをまたいで保つ", async ({ page }, testInfo) => {
  // API の並びをページ内で並べ直さず表示する。SQL 自体の順序は実 Oracle のテストで確認する。
  const names = ["DEFAULT", ...Array.from({ length: 26 }, (_, i) => `${i + 1}_サンプル`), "a2", "a10", "あ", "漢字"];
  const state = createKnowledgeBaseState();
  state.knowledgeBases = names.map((name, i) => makeKnowledgeBase({
    id: `kb-order-${i}`, name, document_count: names.length - i,
    updated_at: `2026-06-${String((i % 28) + 1).padStart(2, "0")}T00:00:00Z`,
  }));
  await mockKnowledgeBaseApi(page, state);
  await page.goto("/knowledge-bases");
  const links = page.locator("tbody tr").getByRole("link");
  await expect(links).toHaveText(names.slice(0, 25));
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("knowledge-base-order.png"), fullPage: true });
  await page.getByTestId("knowledge-bases-pagination").getByRole("button", { name: "次へ" }).click();
  await expect(links).toHaveText(names.slice(25));
  await page.reload();
  await expect(links).toHaveText(names.slice(25));
  await expectNoPageOverflow(page);
});

test("ナレッジベース管理で作成、文書追加、文書解除、アーカイブができる", async ({ page }) => {
  const state = createKnowledgeBaseState();
  await mockKnowledgeBaseApi(page, state);

  await page.goto("/knowledge-bases");

  // 一覧は list 専用(行は詳細ページへのリンク)。作成は検索・回答プロファイルと同じく PageHeader の「新規作成」から
  // 作成の画面(`?id=new`)へ移る(#555)。
  await expect(page.getByRole("heading", { name: "ナレッジベース", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "社内規程" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveCount(0);
  await expectNoPageOverflow(page);

  await page.getByRole("button", { name: "新規作成" }).click();
  await expect(page).toHaveURL(/\/knowledge-bases\?id=new$/);
  await expect(page.getByRole("heading", { name: "ナレッジベースを作成", level: 1 })).toBeVisible();
  // 「一覧へ戻る」は左上（パンくずは出さない。#618）。読み上げ名は戻り先を足す。
  await expect(page.getByTestId("editor-back")).toHaveText("一覧へ戻る");
  await expect(page.getByTestId("editor-back")).toHaveAccessibleName("ナレッジベースの一覧へ戻る");
  await expect(page.getByRole("navigation", { name: "パンくず" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toBeFocused();
  await expectNoPageOverflow(page);

  // 作成すると新 KB の詳細ページへ履歴を積まずに移る。
  await page.getByRole("textbox", { name: "名前", exact: true }).fill("設計資料");
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("設計レビュー用の資料");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "作成", exact: true }).click();

  // 作成成功 = 新 KB 詳細ページへの遷移 + 見出しで担保(作成トーストはナビと競合し
  // 自動消滅するため、ここでは判定しない)。
  await expect(page).toHaveURL(/\/knowledge-bases\/kb-2$/);
  await expect(page.getByRole("heading", { name: "設計資料", level: 1 })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveValue("設計資料");

  // 詳細ページで文書を追加する（「文書を追加」で候補の一覧を開き、選んで追加する。#600）。
  await page.getByRole("button", { name: "文書を追加", exact: true }).click();
  await page
    .getByRole("listbox", { name: "追加する文書の候補" })
    .getByRole("option", { name: "guide.txt" })
    .click();
  await page.getByRole("button", { name: "選択した 1 件を追加" }).click();

  await expect(page.getByText("文書をナレッジベースに追加しました。").first()).toBeVisible();
  await expect(page.getByRole("link", { name: "guide.txt" })).toBeVisible();

  // 詳細ページで文書を外す。
  const assignedDocument = page.locator("li").filter({ hasText: "guide.txt" });
  await assignedDocument.getByRole("button", { name: "guide.txt の操作" }).click();
  await page.getByRole("menuitem", { name: "外す" }).click();
  const removeDialog = page.getByRole("alertdialog", { name: "所属から外しますか？" });
  await expect(removeDialog).toBeVisible();
  await removeDialog.getByRole("button", { name: "外す" }).click();

  await expect(page.getByText("文書をナレッジベースから外しました。").first()).toBeVisible();
  await expect(page.getByRole("link", { name: "guide.txt" })).toHaveCount(0);

  // 一覧へ戻ってアーカイブする。
  await page.goto("/knowledge-bases");
  const createdRow = page.locator("tr").filter({ hasText: "設計資料" });
  await createdRow.getByRole("button", { name: "設計資料 の操作" }).click();
  await page.getByRole("menuitem", { name: "アーカイブ" }).click();
  const archiveDialog = page.getByRole("alertdialog", { name: "ナレッジベースをアーカイブしますか？" });
  await expect(archiveDialog).toBeVisible();
  await archiveDialog.getByRole("button", { name: "アーカイブ" }).click();

  await expect(page.getByText("ナレッジベースをアーカイブしました。").first()).toBeVisible();
  // 既定 ACTIVE フィルタなので、アーカイブ済みは一覧から消える。
  await expect(page.getByRole("link", { name: "設計資料" })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

// #131: 一覧の行（RowActionMenu）と同じ操作の定義を、詳細では ObjectActionBar で出す。
for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`詳細の ObjectActionBar からアーカイブでき、メニューは Esc でフォーカスを戻す (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const state = createKnowledgeBaseState();
    await mockKnowledgeBaseApi(page, state);

    await page.goto("/knowledge-bases/kb-1");
    await expect(page.getByRole("heading", { name: "社内規程", level: 1 })).toBeVisible();

    const bar = page.getByRole("group", { name: "社内規程 の操作" });
    // 危険な操作は常時表示せず「その他の操作」へ入れる（buttons.md §5.1）。
    await expect(bar.getByRole("button", { name: "アーカイブ" })).toHaveCount(0);
    const more = bar.getByRole("button", { name: "その他の操作" });
    await more.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("menuitem", { name: "アーカイブ" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expect(more).toBeFocused();

    await more.click();
    await page.getByRole("menuitem", { name: "アーカイブ" }).click();
    const dialog = page.getByRole("alertdialog", { name: "ナレッジベースをアーカイブしますか？" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "キャンセル" }).click();
    expect(state.knowledgeBases[0].status).toBe("ACTIVE");

    await more.click();
    await page.getByRole("menuitem", { name: "アーカイブ" }).click();
    await dialog.getByRole("button", { name: "アーカイブ" }).click();
    await expect(page.getByText("ナレッジベースをアーカイブしました。").first()).toBeVisible();
    expect(state.knowledgeBases[0].status).toBe("ARCHIVED");
    // 検索・回答プロファイルと同じく、アーカイブしたら一覧へ履歴を積まずに戻る（#555）。
    await expect(page).toHaveURL(/\/knowledge-bases$/);
    await expectNoPageOverflow(page);
  });
}

test("狭い画面幅(375px)でもページ全体が横スクロール(崩れ)しない", { tag: "@responsive" }, async ({ page }) => {
  const state = createKnowledgeBaseState();
  await mockKnowledgeBaseApi(page, state);

  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto("/knowledge-bases");

  await expect(page.getByRole("heading", { name: "ナレッジベース", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "社内規程" })).toBeVisible();
  // documentElement と main の双方で横はみ出し(崩れ)が無いこと。
  // テーブルの min-width はテーブル内の overflow-x-auto に閉じ込める前提。
  await expectNoPageOverflow(page);

  // 検索入力は狭幅で全幅に流動化し、固定幅(w-64)のはみ出しを起こさない。
  const search = page.getByPlaceholder("名前・説明で検索");
  const searchBox = await search.boundingBox();
  expect(searchBox).not.toBeNull();
  expect(searchBox!.width).toBeLessThanOrEqual(375);
});

test("一覧のスクロール領域はホイールの scroll chaining を遮断しない", async ({ page }) => {
  const state = createKnowledgeBaseState();
  await mockKnowledgeBaseApi(page, state);

  await page.goto("/knowledge-bases");
  await expect(page.getByRole("link", { name: "社内規程" })).toBeVisible();

  // overscroll-behavior: contain が付くと、中身が少なくスクロール余地が無いときも
  // カード上の wheel がページへ伝播しなくなる(RAG検索の引用カードで実害)。復活をガードする。
  // 一覧は DataTable の表示行数（visibleRows）で表の中を縦スクロールにする（#265）。
  const area = page.getByTestId("knowledge-bases-scroll-region");
  await expect(area).toBeVisible();
  const behavior = await area.evaluate((el) => getComputedStyle(el).overscrollBehaviorY);
  expect(behavior).toBe("auto");
});

test("DEFAULT は先頭表示され、予約名として保護される", async ({ page }) => {
  const state = createKnowledgeBaseState();
  state.knowledgeBases.unshift(makeKnowledgeBase({ id: "kb-default", name: "DEFAULT" }));
  await mockKnowledgeBaseApi(page, state);

  await page.goto("/knowledge-bases");

  const rows = page.locator("tbody tr");
  await expect(rows.first()).toContainText("DEFAULT");
  // アーカイブは行の RowActionMenu に入り、DEFAULT では理由付きで無効（#131）。
  await rows.first().getByRole("button", { name: "DEFAULT の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "DEFAULT はアーカイブできません" })).toBeDisabled();
  await page.keyboard.press("Escape");

  await page.goto("/knowledge-bases?id=new");
  await page.getByRole("textbox", { name: "名前", exact: true }).fill("default");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "作成", exact: true }).click();
  await expect(page.getByText("DEFAULT は予約名のため使用できません。")).toBeVisible();
  await expect(page).toHaveURL(/\/knowledge-bases\?id=new$/);
  await expectNoPageOverflow(page);
});

function emptyAdapterConfig() {
  return {
    version: 1,
    ingestion: {
      preprocess_profile: null,
      parser_adapter_backend: null,
      parser_docling_enabled: null,
      parser_unstructured_enabled: null,
      chunking_strategy: null,
      chunk_size: null,
      chunk_overlap: null,
      chunk_min_chars: null,
      chunk_context_header_enabled: null,
      graph_profile: null,
      field_extraction_enabled: null,
      vision_enabled: null,
      navigation_summary_enabled: null,
    },
    query: {
      guardrail_policy: null,
    },
  };
}

async function mockKnowledgeBaseApi(
  page: Page,
  state: {
    knowledgeBases: KnowledgeBaseSummary[];
    documents: DocumentSummary[];
  }
) {
  await page.route("**/api/knowledge-bases**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const parts = url.pathname.split("/").filter(Boolean);
    const id = parts[2];

    if (request.method() === "GET" && url.pathname === "/api/knowledge-bases") {
      await route.fulfill({
        json: {
          data: pageKnowledgeBases(state.knowledgeBases, url),
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (request.method() === "GET" && parts.length === 3) {
      const knowledgeBase = state.knowledgeBases.find((item) => item.id === id);
      if (!knowledgeBase) {
        await route.fulfill({ status: 404, json: { detail: "not found" } });
        return;
      }
      await route.fulfill({
        json: {
          data: {
            ...knowledgeBase,
            retrieval_config: {},
            adapter_config: emptyAdapterConfig(),
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (request.method() === "PATCH" && parts.length === 3) {
      const knowledgeBase = state.knowledgeBases.find((item) => item.id === id);
      if (!knowledgeBase) {
        await route.fulfill({ status: 404, json: { detail: "not found" } });
        return;
      }
      const payload = request.postDataJSON() as { adapter_config?: unknown };
      await route.fulfill({
        json: {
          data: {
            ...knowledgeBase,
            retrieval_config: {},
            adapter_config: payload.adapter_config ?? emptyAdapterConfig(),
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (request.method() === "POST" && url.pathname === "/api/knowledge-bases") {
      const payload = request.postDataJSON() as {
        name: string;
        description?: string | null;
        default_search_mode?: SearchMode;
      };
      if (
        state.knowledgeBases.some(
          (item) => item.name.toLowerCase() === payload.name.toLowerCase()
        )
      ) {
        await route.fulfill({
          status: 409,
          json: {
            data: null,
            error_messages: [DUPLICATE_NAME_MESSAGE],
            warning_messages: [],
          },
        });
        return;
      }
      const knowledgeBase = makeKnowledgeBase({
        id: `kb-${state.knowledgeBases.length + 1}`,
        name: payload.name,
        description: payload.description ?? null,
        default_search_mode: payload.default_search_mode ?? "hybrid",
      });
      state.knowledgeBases.push(knowledgeBase);
      await route.fulfill({
        json: {
          data: { ...knowledgeBase, retrieval_config: {}, adapter_config: emptyAdapterConfig() },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (request.method() === "POST" && parts.length === 4 && parts[3] === "archive") {
      const knowledgeBase = state.knowledgeBases.find((item) => item.id === id);
      if (!knowledgeBase) {
        await route.fulfill({ status: 404, json: { detail: "not found" } });
        return;
      }
      knowledgeBase.status = "ARCHIVED";
      knowledgeBase.archived_at = "2026-06-15T00:05:00Z";
      knowledgeBase.updated_at = "2026-06-15T00:05:00Z";
      await route.fulfill({
        json: {
          data: { ...knowledgeBase, retrieval_config: {} },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (request.method() === "POST" && parts.length === 4 && parts[3] === "documents") {
      const payload = request.postDataJSON() as { document_ids: string[] };
      assignDocuments(state, id, payload.document_ids);
      await route.fulfill({
        json: {
          data: {
            ...state.knowledgeBases.find((item) => item.id === id),
            retrieval_config: {},
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (request.method() === "DELETE" && parts.length === 5 && parts[3] === "documents") {
      removeDocument(state, id, parts[4]);
      await route.fulfill({
        json: {
          data: null,
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    await route.fulfill({ status: 404, json: { detail: "not found" } });
  });

  await page.route("**/api/documents**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/documents") {
      await route.fulfill({ status: 404, json: { detail: "not found" } });
      return;
    }
    const knowledgeBaseId = url.searchParams.get("knowledge_base_id");
    const q = url.searchParams.get("q")?.trim().toLowerCase();
    const limit = Number(url.searchParams.get("limit") ?? 50);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    const documents = (
      knowledgeBaseId
        ? state.documents.filter((document) =>
            document.knowledge_bases.some((knowledgeBase) => knowledgeBase.id === knowledgeBaseId)
          )
        : state.documents
    ).filter((document) => !q || document.file_name.toLowerCase().includes(q));
    await route.fulfill({
      json: {
        data: {
          items: documents.slice(offset, offset + limit),
          total: documents.length,
          limit,
          offset,
          has_next: offset + limit < documents.length,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

const DUPLICATE_NAME_MESSAGE =
  "同じ名前のナレッジベース（アーカイブ済みを含む）がすでにあります。別の名前を指定してください。";

// #282: 所属文書は件数で打ち切らずページングし、外して空になったページから最後のページへ戻る。
test("所属文書は 25 件ずつページングし、外して空になったページから戻る", async ({ page }) => {
  const state = createKnowledgeBaseState();
  const kb = state.knowledgeBases[0];
  for (let index = 2; index <= 26; index += 1) {
    state.documents.push(
      makeDocument({
        id: `doc-member-${index}`,
        file_name: `member-${String(index).padStart(2, "0")}.txt`,
        status: "INDEXED",
        knowledge_bases: [{ id: kb.id, name: kb.name }],
      })
    );
  }
  refreshKnowledgeBaseCounts(state, kb.id);
  await mockKnowledgeBaseApi(page, state);

  await page.goto("/knowledge-bases/kb-1");
  const pagination = page.getByTestId("knowledge-base-documents-pagination");
  await expect(pagination).toContainText("1–25 / 26 件");
  await expect(page.getByRole("link", { name: /^member-|^policy/ })).toHaveCount(25);

  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(pagination).toContainText("26–26 / 26 件");
  const lastRow = page.locator("li").filter({ hasText: "member-26.txt" });
  await expect(lastRow).toBeVisible();

  // 2 ページ目の唯一の文書を外すと、空の案内ではなく 1 ページ目へ戻る。
  await lastRow.getByRole("button", { name: "member-26.txt の操作" }).click();
  await page.getByRole("menuitem", { name: "外す" }).click();
  const dialog = page.getByRole("alertdialog", { name: "所属から外しますか？" });
  await expect(dialog).toContainText("ほかのナレッジベースに所属していない文書は DEFAULT へ移ります。");
  await dialog.getByRole("button", { name: "外す" }).click();

  await expect(page.getByRole("link", { name: /^member-|^policy/ })).toHaveCount(25);
  await expect(page.getByText("所属文書がありません。")).toHaveCount(0);
  // 1 ページに収まったのでページ送りは出さない（25 件は最も少ない表示件数の段の 10 件より多いので、範囲と表示件数は出す）。
  await expect(pagination).toContainText("1–25 / 25 件");
  await expect(pagination.getByRole("button", { name: "次へ" })).toHaveCount(0);
  await expectNoPageOverflow(page);
});

function addCandidateDocuments(state: ReturnType<typeof createKnowledgeBaseState>, count: number) {
  for (let index = 1; index <= count; index += 1) {
    const number = String(index).padStart(4, "0");
    state.documents.push(
      makeDocument({ id: `doc-bulk-${number}`, file_name: `bulk-${number}.pdf`, status: "INDEXED" })
    );
  }
}

// #600: 数千〜数万件の文書から、検索して複数を選び、一度に追加する（共通の ListPicker）。
for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`文書を追加は 3,000 件から検索して複数を選び、一度に追加できる (${viewport.name})`, { tag: "@responsive" }, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const state = createKnowledgeBaseState();
    addCandidateDocuments(state, 3000);
    await mockKnowledgeBaseApi(page, state);
    const candidateRequests: { q: string | null; offset: string | null; limit: string | null }[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/api/documents" && !url.searchParams.has("knowledge_base_id")) {
        candidateRequests.push({
          q: url.searchParams.get("q"),
          offset: url.searchParams.get("offset"),
          limit: url.searchParams.get("limit"),
        });
      }
    });

    await page.goto("/knowledge-bases/kb-1");
    await expect(page.getByRole("heading", { name: "社内規程", level: 1 })).toBeVisible();
    const toggle = page.getByRole("button", { name: "文書を追加", exact: true });
    // 一覧のツールバー: 左に所属文書の検索、右に「文書を追加」（page-archetypes.md「一覧のツールバー」）。
    const memberSearch = page.getByRole("searchbox", { name: "所属文書を検索" });
    const [memberSearchBox, toggleBox] = await Promise.all([memberSearch.boundingBox(), toggle.boundingBox()]);
    if (viewport.width >= 1024) expect(memberSearchBox!.x).toBeLessThan(toggleBox!.x);
    // 狭い幅では縦に積み、検索欄が先頭。
    else expect(memberSearchBox!.y).toBeLessThan(toggleBox!.y);
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    // 候補は開くまで読まない。
    expect(candidateRequests).toHaveLength(0);

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    const picker = page.getByTestId("knowledge-base-add-documents");
    const search = picker.getByRole("searchbox", { name: "追加する文書を検索" });
    await expect(search).toBeFocused();
    const listbox = picker.getByRole("listbox", { name: "追加する文書の候補" });
    await expect(listbox).toBeVisible();
    // 数千件を一度に読まず、100 件ずつ読む。
    expect(candidateRequests[0]).toEqual({ q: null, offset: "0", limit: "100" });
    const footer = picker.getByTestId("knowledge-base-add-documents-footer");
    await expect(footer).toContainText("100 / 3,002 件を表示、選択 0 件");
    // すでに所属している文書は「追加済み」で選べない。
    const assigned = listbox.getByRole("option", { name: "policy.txt" });
    await expect(assigned).toHaveAttribute("aria-disabled", "true");
    await expect(assigned).toContainText("追加済み");

    // キーボード: listbox に入り、↓ と Space で選ぶ。
    await listbox.focus();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Space");
    await expect(listbox.getByRole("option", { name: "guide.txt" })).toHaveAttribute("aria-checked", "true");
    await expect(footer).toContainText("選択 1 件");

    // 続きを読み込む（offset 100）。
    await picker.getByRole("button", { name: "さらに読み込む" }).click();
    await expect(footer).toContainText("200 / 3,002 件を表示、選択 1 件");
    expect(candidateRequests.some((request) => request.offset === "100" && request.q === null)).toBe(true);
    // 100 行を超えたら、描く行は見えている範囲だけ（仮想スクロール）。
    expect(await listbox.getByRole("option").count()).toBeLessThan(60);

    // 文書名でサーバー側を検索する（入力に合わせて。検索ボタンは無い）。
    await search.fill("bulk-295");
    await expect.poll(() => candidateRequests.some((request) => request.q === "bulk-295")).toBe(true);
    await expect(footer).toContainText("10 / 10 件を表示、選択 1 件");
    await listbox.getByRole("option", { name: "bulk-2950.pdf" }).click();
    await listbox.getByRole("option", { name: "bulk-2959.pdf" }).click();
    await expect(footer).toContainText("選択 3 件");
    // 検索語を変えても選択は残り、「選択中だけ表示」で確かめられる。
    await search.fill("");
    // 検索前に読み込んだ分（2 ページ）はキャッシュから戻る。
    await expect(footer).toContainText("200 / 3,002 件を表示、選択 3 件");
    const showSelected = picker.getByRole("button", { name: "選択中だけ表示（3）" });
    await showSelected.click();
    await expect(showSelected).toHaveAttribute("aria-pressed", "true");
    await expect(listbox.getByRole("option")).toHaveCount(3);
    await expect(listbox.getByRole("option", { name: "bulk-2950.pdf" })).toBeVisible();
    await expectNoPageOverflow(page);

    await picker.getByRole("button", { name: "選択した 3 件を追加" }).click();
    await expect(page.getByText("文書をナレッジベースに追加しました。").first()).toBeVisible();
    // 追加したら閉じて、起点のボタンへフォーカスを戻す。
    await expect(picker).toHaveCount(0);
    await expect(toggle).toBeFocused();
    await expect(page.getByRole("link", { name: "bulk-2959.pdf" })).toBeVisible();
    await expect(page.getByRole("link", { name: "guide.txt" })).toBeVisible();
    await expectNoPageOverflow(page);
  });
}

// 200 件を超える追加は 200 件ずつ送る。2 回目以降で失敗しても、それまでの分は追加済みなので、
// 所属文書の一覧を取り直し、何件まで追加できたかを出して、残りだけを選択に残す。
for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`文書の追加が途中で失敗しても、追加できた分を一覧に出し件数を知らせる (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const state = createKnowledgeBaseState();
    addCandidateDocuments(state, 300);
    await mockKnowledgeBaseApi(page, state);
    const batches: number[] = [];
    // 2 回目の送信（201 件目以降）だけ失敗させる。1 回目は通常の mock（所属を追加する）へ渡す。
    await page.route("**/api/knowledge-bases/kb-1/documents", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      const payload = route.request().postDataJSON() as { document_ids: string[] };
      batches.push(payload.document_ids.length);
      if (batches.length === 1) return route.fallback();
      await route.fulfill({
        status: 503,
        json: { data: null, error_messages: ["一時的に利用できません。"], warning_messages: [] },
      });
    });

    await page.goto("/knowledge-bases/kb-1");
    const pagination = page.getByTestId("knowledge-base-documents-pagination");
    const toggle = page.getByRole("button", { name: "文書を追加", exact: true });
    await toggle.click();
    const picker = page.getByTestId("knowledge-base-add-documents");
    const footer = picker.getByTestId("knowledge-base-add-documents-footer");
    await expect(footer).toContainText("100 / 302 件を表示");
    await picker.getByRole("button", { name: "さらに読み込む" }).click();
    await expect(footer).toContainText("200 / 302 件を表示");
    await picker.getByRole("button", { name: "さらに読み込む" }).click();
    await expect(footer).toContainText("300 / 302 件を表示");
    await picker.getByRole("button", { name: "表示中をすべて選択" }).click();
    // policy.txt は追加済みで選べないため 299 件。
    await expect(footer).toContainText("選択 299 件");

    await picker.getByRole("button", { name: "選択した 299 件を追加" }).click();
    await expect
      .poll(() => batches)
      .toEqual([200, 99]);
    await expect(
      page.getByText("299 件のうち 200 件を追加しました。残りの 99 件は追加できませんでした。").first()
    ).toBeVisible();
    await expect(page.getByText("一時的に利用できません。").first()).toBeVisible();
    // 追加できた分は所属文書の一覧に出る（失敗しても一覧を取り直す）。
    await expect(pagination).toContainText("/ 201 件");
    // 追加できなかった残りだけを選択に残し、もう一度追加できる。
    await expect(picker).toBeVisible();
    await expect(footer).toContainText("選択 99 件");
    await expect(picker.getByRole("button", { name: "選択した 99 件を追加" })).toBeEnabled();
    await expectNoPageOverflow(page);
  });
}

test("所属文書は一覧の中を検索でき、検索語を変えると 1 ページ目に戻る", async ({ page }) => {
  const state = createKnowledgeBaseState();
  const kb = state.knowledgeBases[0];
  for (let index = 2; index <= 55; index += 1) {
    state.documents.push(
      makeDocument({
        id: `doc-member-${index}`,
        file_name: `member-${String(index).padStart(2, "0")}.txt`,
        status: "INDEXED",
        knowledge_bases: [{ id: kb.id, name: kb.name }],
      })
    );
  }
  refreshKnowledgeBaseCounts(state, kb.id);
  await mockKnowledgeBaseApi(page, state);
  const memberQueries: (string | null)[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/documents" && url.searchParams.get("knowledge_base_id") === "kb-1") {
      memberQueries.push(url.searchParams.get("q"));
    }
  });

  await page.goto("/knowledge-bases/kb-1");
  const pagination = page.getByTestId("knowledge-base-documents-pagination");
  await expect(pagination).toContainText("1–25 / 55 件");
  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(pagination).toContainText("26–50 / 55 件");

  const search = page.getByRole("searchbox", { name: "所属文書を検索" });
  await search.fill("member-1");
  await expect.poll(() => memberQueries.includes("member-1")).toBe(true);
  // 検索語が変わったら 1 ページ目へ戻る。一致は 10 件（member-10〜19）で 1 ページなのでページ送りは出さない。
  await expect(page.getByRole("link", { name: /^member-1/ })).toHaveCount(10);
  await expect(pagination).toHaveCount(0);

  await search.fill("zzz");
  await expect(page.getByText("検索に一致する所属文書がありません。")).toBeVisible();
  await page.getByRole("button", { name: "検索語をクリア" }).last().click();
  await expect(pagination).toContainText("1–25 / 55 件");
  await expect(search).toHaveValue("");
  await expectNoPageOverflow(page);
});

test("ナレッジベースの名前と説明は必須で、空・空白だけでは作成せず最初の不正な欄へフォーカスする", async ({
  page,
}) => {
  const state = createKnowledgeBaseState();
  await mockKnowledgeBaseApi(page, state);
  const before = state.knowledgeBases.length;

  await page.goto("/knowledge-bases?id=new");
  const name = page.getByRole("textbox", { name: "名前", exact: true });
  const description = page.getByRole("textbox", { name: "説明", exact: true });
  await expect(name).toHaveAttribute("aria-required", "true");
  await expect(description).toHaveAttribute("aria-required", "true");
  await expect(description).toHaveAttribute("maxlength", "2000");

  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "作成", exact: true }).click();
  await expect(page.getByText("名前を入力してください。")).toBeVisible();
  await expect(page.getByText("説明を入力してください。")).toBeVisible();
  await expect(name).toBeFocused();

  await name.fill("設計資料");
  await description.fill(" \u3000 ");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "作成", exact: true }).click();
  await expect(page.getByText("説明を入力してください。")).toBeVisible();
  await expect(description).toBeFocused();
  expect(state.knowledgeBases).toHaveLength(before);
  await expect(page).toHaveURL(/\/knowledge-bases\?id=new$/);
  await expectNoPageOverflow(page);
});

test("同じ名前で作成すると理由を名前の欄に表示し、詳細へ移らない", async ({ page }) => {
  const state = createKnowledgeBaseState();
  await mockKnowledgeBaseApi(page, state);

  await page.goto("/knowledge-bases?id=new");
  const name = page.getByRole("textbox", { name: "名前", exact: true });
  await name.fill("社内規程");
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("就業規則");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "作成", exact: true }).click();

  // 同名（409）は名前の欄の下に理由を出し、名前の欄へフォーカスを戻す（編集と同じ。#555）。
  await expect(page.getByText(DUPLICATE_NAME_MESSAGE)).toHaveCount(1);
  await expect(name).toHaveAttribute("aria-invalid", "true");
  await expect(name).toBeFocused();
  await expect(page).toHaveURL(/\/knowledge-bases\?id=new$/);
  await expect(name).toHaveAttribute("maxlength", "256");
  // 名前を直すと古い理由は消える。
  await name.fill("社内規程 2");
  await expect(page.getByText(DUPLICATE_NAME_MESSAGE)).toHaveCount(0);
});

test("最後のページの KB をアーカイブすると、空の案内ではなく前のページへ戻る", async ({ page }) => {
  const state = createKnowledgeBaseState();
  for (let index = 2; index <= 51; index += 1) {
    state.knowledgeBases.push(
      makeKnowledgeBase({ id: `kb-${index}`, name: `KB ${String(index).padStart(2, "0")}` })
    );
  }
  await mockKnowledgeBaseApi(page, state);

  await page.goto("/knowledge-bases");
  // 状態は色だけでなくアイコン付きの StatusBadge で出す。
  await expect(
    page.locator("tbody tr").first().locator('[data-status-variant="success"] svg')
  ).toBeVisible();
  // 共通の Pagination（25 件/ページ、#265・#1952）。最後の 3 ページ目へ移る。
  const pagination = page.getByTestId("knowledge-bases-pagination");
  await pagination.getByRole("button", { name: "次へ" }).click();
  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(page.locator("tbody tr")).toHaveCount(1);

  const lastRow = page.locator("tbody tr").first();
  const name = (await lastRow.getByRole("link").textContent())?.trim() ?? "";
  await lastRow.getByRole("button", { name: `${name} の操作` }).click();
  await page.getByRole("menuitem", { name: "アーカイブ" }).click();
  await page
    .getByRole("alertdialog", { name: "ナレッジベースをアーカイブしますか？" })
    .getByRole("button", { name: "アーカイブ" })
    .click();

  await expect(page.locator("tbody tr")).toHaveCount(25);
  await expect(page.getByText("ナレッジベースがありません。")).toHaveCount(0);
  await expect(pagination).toContainText("26–50 / 50 件");
});

// #555: 一覧 → 作成 / 詳細の構成を検索・回答プロファイルにそろえる（page-archetypes.md §1 A）。
test("行のクリックで詳細を開き、詳細の PageHeader に 一覧へ戻る（左上）・状態・件数を、フォームの末尾に保存・変更を破棄を出す", async ({
  page,
}) => {
  const state = createKnowledgeBaseState();
  await mockKnowledgeBaseApi(page, state);
  await page.goto("/knowledge-bases");

  // 行の操作以外の領域（文書数の列）のクリックで開く。
  await page.getByTestId("knowledge-base-row-kb-1").getByRole("cell").nth(2).click();
  await expect(page).toHaveURL(/\/knowledge-bases\/kb-1$/);
  await expect(page.getByRole("heading", { name: "社内規程", level: 1 })).toBeVisible();
  await expect(page.getByTestId("editor-back")).toBeVisible();
  const header = page.locator("header[data-page-header]");
  await expect(header.getByText("有効")).toBeVisible();
  await expect(page.getByTestId("knowledge-base-meta")).toContainText("文書 1 件");
  await expect(page.getByTestId("knowledge-base-meta")).toContainText("索引済み 1 件");
  await expect(page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true })).toBeVisible();
  // 手書きの「戻る」リンクの帯とカード内の見出しは無い（h1 は PageHeader の 1 つだけ）。
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  await expectNoPageOverflow(page);

  await clickBackToList(page);
  await expect(page).toHaveURL(/\/knowledge-bases$/);
});

for (const viewport of [
  { name: "1920", width: 1920, height: 1000 },
  { name: "1280", width: 1280, height: 800 },
  { name: "375", width: 375, height: 812 },
]) {
  test(`詳細の PageHeader のタイトルと本文のカードの左端がそろう (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockKnowledgeBaseApi(page, createKnowledgeBaseState());
    await page.goto("/knowledge-bases/kb-1");
    await expect(page.getByTestId("knowledge-base-form")).toBeVisible();
    const lefts = await page.evaluate(() => {
      const title = document.querySelector("main h1")?.getBoundingClientRect().left ?? -1;
      let card = document.querySelector('[data-testid="knowledge-base-form"]')?.parentElement ?? null;
      while (card && !card.className.includes("border")) card = card.parentElement;
      return { title: Math.round(title), card: Math.round(card?.getBoundingClientRect().left ?? -100) };
    });
    expect(Math.abs(lefts.title - lefts.card)).toBeLessThanOrEqual(1);
    await expectNoPageOverflow(page);
  });
}

test("`?id=<id>` で開くと詳細の URL へ置き換え、存在しない詳細は別の対象へ置き換えず一覧へ戻る導線を出す", async ({
  page,
}) => {
  const state = createKnowledgeBaseState();
  await mockKnowledgeBaseApi(page, state);

  await page.goto("/knowledge-bases?id=kb-1");
  await expect(page).toHaveURL(/\/knowledge-bases\/kb-1$/);
  await expect(page.getByRole("heading", { name: "社内規程", level: 1 })).toBeVisible();

  await page.goto("/knowledge-bases/kb-missing");
  // 404 は再試行せず、すぐ「対象が見つかりません」を出す。
  await expect(page.getByText("対象が見つかりません")).toBeVisible();
  await expect(page.getByText("「kb-missing」は削除されたか、存在しません。")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveCount(0);
  await expectNoPageOverflow(page);
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page).toHaveURL(/\/knowledge-bases$/);
});

test("作成の画面は未保存の入力があると離脱を確認し、下書きを一覧から再開できる", async ({ page }) => {
  const state = createKnowledgeBaseState();
  await mockKnowledgeBaseApi(page, state);
  await page.goto("/knowledge-bases?id=new");
  await page.getByRole("textbox", { name: "名前", exact: true }).fill("設計資料");

  await page.getByTestId("editor-back").click();
  const dialog = page.getByRole("alertdialog", { name: "保存していない変更があります" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/\?id=new$/);

  await clickBackToList(page);
  await page
    .getByRole("alertdialog", { name: "保存していない変更があります" })
    .getByRole("button", { name: "移動する" })
    .click();
  await expect(page).toHaveURL(/\/knowledge-bases$/);

  await expect(page.getByText("作成中のナレッジベースに保存していない下書きがあります。")).toBeVisible();
  await page.getByRole("button", { name: "下書きを開く" }).click();
  await expect(page).toHaveURL(/\?id=new$/);
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveValue("設計資料");
  await expect(page.getByText("保存していない下書きを復元しました。")).toBeVisible();
  await page.getByRole("button", { name: "変更を破棄" }).click();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveValue("");
  // 下書きが無くなったので、確認なしで一覧へ戻れる。
  await clickBackToList(page);
  await expect(page).toHaveURL(/\/knowledge-bases$/);
  await expect(page.getByRole("button", { name: "下書きを開く" })).toHaveCount(0);
});

test("詳細で名前・説明を変えると離脱を確認し、同じ詳細を開き直すと下書きを復元する", async ({ page }) => {
  const state = createKnowledgeBaseState();
  await mockKnowledgeBaseApi(page, state);
  await page.goto("/knowledge-bases/kb-1");
  const description = page.getByRole("textbox", { name: "説明", exact: true });
  await description.fill("就業規則と経費精算");

  await clickBackToList(page);
  await page
    .getByRole("alertdialog", { name: "保存していない変更があります" })
    .getByRole("button", { name: "移動する" })
    .click();
  await expect(page).toHaveURL(/\/knowledge-bases$/);

  await page.getByRole("link", { name: "社内規程" }).click();
  await expect(description).toHaveValue("就業規則と経費精算");
  await expect(page.getByText("保存していない下書きを復元しました。")).toBeVisible();
});

// #585 / #1786: 欄に結び付かない保存の失敗は確定の行の直上の 1 か所だけ（Toast・フォームの下に重ねない）。
test("詳細の保存に失敗すると、理由を確定の行の直上だけに出し、次の保存で消える", async ({ page }) => {
  const message = "ナレッジベースを更新できませんでした。時間をおいて再試行してください。";
  const state = createKnowledgeBaseState();
  await mockKnowledgeBaseApi(page, state);
  let fail = true;
  await page.route("**/api/knowledge-bases/kb-1", async (route) => {
    if (route.request().method() !== "PATCH" || !fail) return route.fallback();
    await route.fulfill({
      status: 503,
      json: { data: null, error_messages: [message], warning_messages: [] },
    });
  });
  await page.goto("/knowledge-bases/kb-1");
  const description = page.getByRole("textbox", { name: "説明", exact: true });
  await description.fill("就業規則と経費精算");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();

  const banner = page.getByTestId("knowledge-base-save-error");
  await expect(banner.getByRole("alert")).toHaveText(message);
  await expect(banner).toBeInViewport();
  await expect(page.getByText(message)).toHaveCount(1);
  await expect(description).toHaveValue("就業規則と経費精算");
  // 失敗の Banner の直下が確定の行（#1786）。
  expect(await banner.evaluate((element) => element.nextElementSibling?.getAttribute("data-testid"))).toBe(
    "knowledge-base-form-actions"
  );

  fail = false;
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("ナレッジベースを更新しました")).toBeVisible();
  await expect(banner).toHaveCount(0);
});

test("ナレッジベースが無いときは、空の状態から作成の画面へ進める", async ({ page }) => {
  await mockKnowledgeBaseApi(page, { knowledgeBases: [], documents: [] });
  await page.goto("/knowledge-bases");
  await expect(page.getByText("ナレッジベースがありません。")).toBeVisible();
  await page.getByRole("button", { name: "最初のナレッジベースを作成" }).click();
  await expect(page).toHaveURL(/\?id=new$/);
});

test("アーカイブ済みの詳細は読み取り専用で、保存できない", async ({ page }) => {
  const state = createKnowledgeBaseState();
  state.knowledgeBases.push(
    makeKnowledgeBase({ id: "kb-old", name: "旧規程", description: "旧版", status: "ARCHIVED" })
  );
  await mockKnowledgeBaseApi(page, state);
  await page.goto("/knowledge-bases/kb-old");

  await expect(page.getByText("アーカイブ済みのナレッジベースは、名前・説明の変更と文書の追加・解除ができません。")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveAttribute("readonly", "");
  await expect(page.getByRole("textbox", { name: "説明", exact: true })).toHaveAttribute("readonly", "");
  await expect(page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true })).toBeDisabled();
  // アーカイブ済みには出せる操作がないため、操作のバーごと出さない。
  await expect(page.getByTestId("knowledge-base-detail-actions")).toHaveCount(0);
});

// アーカイブ済みは文書の追加・解除ができない（案内のとおり、所属文書の行に「外す」を出さない）。
// 検索テストは索引済みの文書があっても使えないので、理由はアーカイブ済みであることを示す。
test("アーカイブ済みの詳細は所属文書を外す操作を出さず、検索テストはアーカイブ済みを理由に示す", async ({
  page,
}) => {
  const state = createKnowledgeBaseState();
  state.knowledgeBases.push(
    makeKnowledgeBase({
      id: "kb-old",
      name: "旧規程",
      description: "旧版",
      status: "ARCHIVED",
      document_count: 1,
      indexed_document_count: 1,
    })
  );
  state.documents.push(
    makeDocument({
      id: "doc-old",
      file_name: "old-policy.txt",
      status: "INDEXED",
      knowledge_bases: [{ id: "kb-old", name: "旧規程" }],
    })
  );
  await mockKnowledgeBaseApi(page, state);
  await page.goto("/knowledge-bases/kb-old");

  await expect(page.getByTestId("knowledge-base-documents-list")).toContainText("old-policy.txt");
  await expect(page.getByTestId("knowledge-base-document-actions-doc-old")).toHaveCount(0);
  await expect(page.getByTestId("knowledge-base-add-documents-toggle")).toHaveCount(0);
  await expect(page.getByText("アーカイブ済みのナレッジベースでは検索テストを行えません。")).toBeVisible();
  await expect(page.getByText("索引済みの文書がありません。")).toHaveCount(0);
});

/** エディタの「一覧へ戻る」。ページの左上（タイトルの上）にあり、375px でもメニューに畳まない（#618）。 */
async function clickBackToList(page: Page) {
  const back = page.getByTestId("editor-back");
  // ヘッダーの右の操作の列には入れない（確定の保存・変更を破棄もフォームの末尾。#618 / #1786）。
  await expect(page.getByRole("group", { name: "ページ操作" }).getByTestId("editor-back")).toHaveCount(0);
  await back.click();
}

function createKnowledgeBaseState() {
  const knowledgeBases = [
    makeKnowledgeBase({
      id: "kb-1",
      name: "社内規程",
      description: "人事・経費・情報管理の規程",
      document_count: 1,
      indexed_document_count: 1,
      searchable_chunk_count: 8,
    }),
  ];
  const documents = [
    makeDocument({
      id: "doc-1",
      file_name: "policy.txt",
      status: "INDEXED",
      knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
    }),
    makeDocument({
      id: "doc-2",
      file_name: "guide.txt",
      status: "UPLOADED",
      knowledge_bases: [],
    }),
  ];
  return { knowledgeBases, documents };
}

function pageKnowledgeBases(knowledgeBases: KnowledgeBaseSummary[], url: URL) {
  const status = url.searchParams.get("status") as KnowledgeBaseStatus | null;
  const q = url.searchParams.get("q")?.trim();
  const limit = Number(url.searchParams.get("limit") ?? 20);
  const offset = Number(url.searchParams.get("offset") ?? 0);
  const filtered = knowledgeBases.filter((knowledgeBase) => {
    if (status && knowledgeBase.status !== status) return false;
    if (q) {
      return [knowledgeBase.name, knowledgeBase.description ?? ""].some((value) =>
        value.includes(q)
      );
    }
    return true;
  });
  const items = filtered.slice(offset, offset + limit);
  return {
    items,
    total: filtered.length,
    limit,
    offset,
    has_next: offset + limit < filtered.length,
  };
}

function assignDocuments(
  state: { knowledgeBases: KnowledgeBaseSummary[]; documents: DocumentSummary[] },
  knowledgeBaseId: string,
  documentIds: string[]
) {
  const knowledgeBase = state.knowledgeBases.find((item) => item.id === knowledgeBaseId);
  if (!knowledgeBase) return;
  for (const document of state.documents) {
    if (!documentIds.includes(document.id)) continue;
    if (document.knowledge_bases.some((item) => item.id === knowledgeBaseId)) continue;
    document.knowledge_bases.push({ id: knowledgeBase.id, name: knowledgeBase.name });
  }
  refreshKnowledgeBaseCounts(state, knowledgeBaseId);
}

function removeDocument(
  state: { knowledgeBases: KnowledgeBaseSummary[]; documents: DocumentSummary[] },
  knowledgeBaseId: string,
  documentId: string
) {
  const document = state.documents.find((item) => item.id === documentId);
  if (!document) return;
  document.knowledge_bases = document.knowledge_bases.filter((item) => item.id !== knowledgeBaseId);
  refreshKnowledgeBaseCounts(state, knowledgeBaseId);
}

function refreshKnowledgeBaseCounts(
  state: { knowledgeBases: KnowledgeBaseSummary[]; documents: DocumentSummary[] },
  knowledgeBaseId: string
) {
  const knowledgeBase = state.knowledgeBases.find((item) => item.id === knowledgeBaseId);
  if (!knowledgeBase) return;
  const documents = state.documents.filter((document) =>
    document.knowledge_bases.some((item) => item.id === knowledgeBaseId)
  );
  knowledgeBase.document_count = documents.length;
  knowledgeBase.indexed_document_count = documents.filter((document) => document.status === "INDEXED").length;
  knowledgeBase.error_document_count = documents.filter((document) => document.status === "ERROR").length;
  knowledgeBase.updated_at = "2026-06-15T00:04:00Z";
}

function makeKnowledgeBase(
  overrides: Partial<KnowledgeBaseSummary> & { id: string; name: string }
): KnowledgeBaseSummary {
  return {
    description: null,
    status: "ACTIVE",
    default_search_mode: "hybrid",
    document_count: 0,
    indexed_document_count: 0,
    error_document_count: 0,
    searchable_chunk_count: 0,
    created_at: "2026-06-15T00:00:00Z",
    updated_at: "2026-06-15T00:00:00Z",
    archived_at: null,
    ...overrides,
  };
}

function makeDocument(overrides: Partial<DocumentSummary> & { id: string; file_name: string }): DocumentSummary {
  return {
    status: "UPLOADED",
    category_name: null,
    content_type: "text/plain",
    file_size_bytes: 128,
    content_sha256: "a".repeat(64),
    duplicate_of_document_id: null,
    uploaded_at: "2026-06-15T00:00:00Z",
    indexed_at: null,
    knowledge_bases: [],
    ...overrides,
  };
}

test("主一覧は高い画面の余白へ拡張し、ページ送りを維持する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const state = createKnowledgeBaseState();
  state.knowledgeBases = Array.from({ length: 60 }, (_, i) => makeKnowledgeBase({ id: `viewport-${i}`, name: `ナレッジベース ${i}` }));
  await mockKnowledgeBaseApi(page, state);
  await page.goto("/knowledge-bases");
  await expectGrowingList(page, testInfo, "knowledge-bases");
});
