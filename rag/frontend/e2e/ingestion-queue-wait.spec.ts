import { openFileListFilters, closeFileListFilters } from "./_helpers";
import { expect, type Page, test } from "./fixtures/test";
import { mockDatabaseReady, mockLocalAuth } from "./_helpers";

// #2026: 取込の job が worker の空きを待っている（順番待ち）ときは、人の確認待ちと分けて
// 「〈工程〉の順番待ち」と前の件数・理由・経過を出す。自動進行が無効で確認が要るときは確認待ちのまま。

const DOC_ID = "doc-queue-wait";
const FILE_NAME = "document-a.pdf";

type StepStatus = "PENDING" | "QUEUED" | "RUNNING" | "NEEDS_REVIEW" | "SUCCEEDED";

const ok = (data: unknown) => ({ json: { data, error_messages: [], warning_messages: [] } });

function queueWait(queuedAt: string) {
  return { phase: "CHUNK", job_id: "job-chunk", queued_at: queuedAt, jobs_ahead: 3, concurrency: 4 };
}

function documentDetail() {
  return {
    id: DOC_ID,
    file_name: FILE_NAME,
    status: "UPLOADED",
    category_name: null,
    content_type: "application/pdf",
    file_size_bytes: 2048,
    content_sha256: "b".repeat(64),
    duplicate_of_document_id: null,
    uploaded_at: "2026-10-15T00:00:00Z",
    indexed_at: null,
    object_storage_path: `local://${FILE_NAME}`,
    error_message: null,
    extraction: null,
    knowledge_bases: [],
    source_profile: null,
  };
}

function step(phase: string, status: StepStatus, extra: Record<string, unknown> = {}) {
  return { phase, status, started_at: null, finished_at: null, error_message: null, ...extra };
}

/** 抽出が終わり（レシピは REVIEW）、Chunk 作成の job が順番待ち、または確認待ちのレシピ。 */
function recipeView(queued: boolean, queuedAt: string) {
  return {
    recipe_id: "recipe-1",
    document_id: DOC_ID,
    slot_no: 1 as const,
    status: "REVIEW",
    failed_phase: null,
    processing_config: {},
    effective_processing_config: {},
    preprocess_artifact: null,
    active_extraction_recipe_id: null,
    active_chunk_set_id: null,
    chunk_count: 0,
    vector_count: 0,
    config_revision: 1,
    materialized_revision: null,
    searchable: false,
    needs_reprocessing: false,
    error_message: null,
    steps: [
      step("PREPROCESS", "SUCCEEDED"),
      step("EXTRACT", queued ? "SUCCEEDED" : "NEEDS_REVIEW"),
      queued ? step("CHUNK", "QUEUED", { queue_wait: queueWait(queuedAt) }) : step("CHUNK", "PENDING"),
      step("INDEX", "PENDING"),
    ],
    created_at: "2026-10-15T00:00:00Z",
    updated_at: "2026-10-15T00:00:05Z",
    started_at: null,
    finished_at: null,
  };
}

function chunkJob(queuedAt: string) {
  return {
    id: "job-chunk",
    document_id: DOC_ID,
    recipe_id: "recipe-1",
    recipe_revision: 1,
    status: "QUEUED",
    phase: "CHUNK",
    parser_profile: "local_text_structure",
    quality_warnings: [],
    skip_reason: null,
    error_message: null,
    attempt_count: 0,
    max_attempts: 3,
    queued_at: queuedAt,
    started_at: null,
    finished_at: null,
  };
}

async function mockDetail(page: Page, queued: boolean) {
  // 経過の表示を確かめるため、投入から 90 秒ほど前の時刻にする。
  const queuedAt = new Date(Date.now() - 90_000).toISOString();
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill(ok({ items: [], total: 0, limit: 100, offset: 0, has_next: false }))
  );
  await page.route(`**/api/documents/${DOC_ID}/**`, (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/recipes")) return route.fulfill(ok([recipeView(queued, queuedAt)]));
    if (path.endsWith("/ingestion-jobs")) return route.fulfill(ok(queued ? [chunkJob(queuedAt)] : []));
    if (path.endsWith("/knowledge-bases")) return route.fulfill(ok([]));
    if (path.includes("/content")) {
      return route.fulfill({ status: 200, contentType: "text/plain", body: "本文" });
    }
    return route.fulfill(ok([]));
  });
  await page.route(`**/api/documents/${DOC_ID}`, (route) => route.fulfill(ok(documentDetail())));
}

test.describe("取込の順番待ち", { tag: "@responsive" }, () => {
  test("次の工程の job が順番待ちなら、確認待ちでなく順番待ちと前の件数・理由・経過を出す", async ({ page }) => {
    await mockDetail(page, true);
    await page.goto(`/documents/${DOC_ID}`);

    const header = page.locator("header").filter({ hasText: FILE_NAME }).first();
    await expect(header.getByText("Chunk 作成の順番待ち")).toBeVisible();
    await expect(page.getByText("抽出確認待ち")).toHaveCount(0);
    await expect(page.getByText("確認待ち", { exact: true })).toHaveCount(0);

    const note = page.getByTestId("recipe-queue-wait");
    await expect(note).toContainText("Chunk 作成の順番待ち");
    await expect(note).toContainText("前に 3 件");
    await expect(note).toContainText("同時に処理できる数（4）の空きを待っています");
    // 経過は投入の時刻から数える（mm:ss）。
    await expect(page.getByTestId("recipe-queue-wait-elapsed")).toHaveText(/経過 0[12]:\d{2}/);

    // 工程: 抽出は完了、Chunk 作成は順番待ち（ラベルは文字でも読める）。
    const steps = page.getByRole("list", { name: "処理レシピ" });
    await expect(steps).toBeVisible();

    // 横にはみ出さない（375px でも説明が折り返す）。
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("自動進行が無効で確認が要るときは、確認待ちのまま順番待ちを出さない", async ({ page }) => {
    await mockDetail(page, false);
    await page.goto(`/documents/${DOC_ID}`);

    const header = page.locator("header").filter({ hasText: FILE_NAME }).first();
    await expect(header.getByText("抽出確認待ち")).toBeVisible();
    await expect(page.getByTestId("recipe-queue-wait")).toHaveCount(0);
    await expect(page.getByText("Chunk 作成の順番待ち")).toHaveCount(0);
  });

  test("一覧の状態列も順番待ちと前の件数・理由を出す", async ({ page }) => {
    await mockDatabaseReady(page);
    await mockLocalAuth(page);
    await page.route("**/api/knowledge-bases**", (route) =>
      route.fulfill(ok({ items: [], total: 0, limit: 100, offset: 0, has_next: false }))
    );
    const summary = (id: string, fileName: string, wait: boolean) => ({
      id,
      file_name: fileName,
      status: "REVIEW",
      category_name: null,
      content_type: "application/pdf",
      file_size_bytes: 2048,
      content_sha256: null,
      duplicate_of_document_id: null,
      uploaded_at: "2026-10-15T00:00:00Z",
      indexed_at: null,
      knowledge_bases: [],
      source_profile: null,
      ingestion_active: wait,
      queue_wait: wait ? queueWait("2026-10-15T00:00:10Z") : null,
    });
    await page.route(
      (url) => url.pathname === "/api/documents",
      (route) =>
        route.fulfill(
          ok({
            items: [summary("doc-a", "document-a.pdf", true), summary("doc-b", "document-b.pdf", false)],
            total: 2,
            limit: 25,
            offset: 0,
            has_next: false,
          })
        )
    );

    await page.goto("/file-list");

    const queuedRow = page.locator("tbody tr").filter({ hasText: "document-a.pdf" });
    await expect(queuedRow).toContainText("Chunk 作成の順番待ち");
    await expect(queuedRow).not.toContainText("抽出確認待ち");
    await expect(page.getByTestId("file-list-queue-wait-doc-a")).toHaveText(
      "前に 3 件 · 同時に処理できる数（4）の空きを待っています"
    );
    // 待っている job の無い行は確認待ちのまま。
    const reviewRow = page.locator("tbody tr").filter({ hasText: "document-b.pdf" });
    await expect(reviewRow).toContainText("抽出確認待ち");
    await expect(page.getByTestId("file-list-queue-wait-doc-b")).toHaveCount(0);
  });

  // #2677: 状態の絞り込みに行の表示と同じ「〈工程〉の順番待ち」を処理の順に並べ、backend の queued_phase で絞る。
  test("状態の絞り込みは順番待ちを処理の順に並べ、順番待ちの段で絞り込んだ件数を出す", async ({ page }) => {
    await mockDatabaseReady(page);
    await mockLocalAuth(page);
    await page.route("**/api/knowledge-bases**", (route) =>
      route.fulfill(ok({ items: [], total: 0, limit: 100, offset: 0, has_next: false }))
    );
    const summary = (id: string, status: string, phase: string | null) => ({
      id,
      file_name: `${id}.pdf`,
      status,
      category_name: null,
      content_type: "application/pdf",
      file_size_bytes: 2048,
      content_sha256: null,
      duplicate_of_document_id: null,
      uploaded_at: "2026-10-15T00:00:00Z",
      indexed_at: null,
      knowledge_bases: [],
      source_profile: null,
      ingestion_active: phase !== null,
      queue_wait: phase
        ? { phase, job_id: `job-${id}`, queued_at: "2026-10-15T00:00:10Z", jobs_ahead: 2, concurrency: 1 }
        : null,
    });
    const documents = [
      summary("doc-queued", "UPLOADED", "PREPROCESS"),
      summary("doc-waiting", "UPLOADED", null),
      summary("doc-indexed", "INDEXED", null),
    ];
    const requests: URL[] = [];
    await page.route(
      (url) => url.pathname === "/api/documents",
      (route) => {
        const url = new URL(route.request().url());
        requests.push(url);
        const status = url.searchParams.get("status");
        const queuedPhase = url.searchParams.get("queued_phase");
        // backend と同じく、status は順番待ちでない文書、queued_phase は順番待ちの段で絞る。
        const filtered = documents.filter((document) =>
          queuedPhase
            ? document.queue_wait?.phase === queuedPhase
            : !status || (document.status === status && document.queue_wait === null)
        );
        const limit = Number(url.searchParams.get("limit") ?? "25");
        return route.fulfill(
          ok({ items: filtered.slice(0, limit), total: filtered.length, limit, offset: 0, has_next: false })
        );
      }
    );

    await page.goto("/file-list");
    const toolbar = page.getByTestId("file-list-toolbar");
    await expect(toolbar).toContainText("全 3 件");

    await openFileListFilters(page);
    const status = page.getByRole("combobox", { name: "状態" });
    await status.click();
    await expect(page.getByRole("option")).toHaveText([
      "すべて",
      "ファイル準備待ち",
      "ファイル準備の順番待ち",
      "ファイル準備中",
      "ファイル準備確認待ち",
      "抽出の順番待ち",
      "解析（抽出）中",
      "抽出確認待ち",
      "Chunk 作成の順番待ち",
      "Chunk 作成中",
      "Chunk 確認待ち",
      "Embedding / 索引の順番待ち",
      "Embedding / 索引中",
      "索引済み",
      "エラー",
    ]);
    await page.getByRole("option", { name: "ファイル準備の順番待ち" }).click();

    await expect(status).toHaveAttribute("data-value", "QUEUED_PREPROCESS");
    await expect(status).toContainText("ファイル準備の順番待ち");
    await expect(toolbar).toContainText("1 件（全 3 件）");
    await expect(page.locator("tbody tr")).toHaveCount(1);
    await expect(page.locator("tbody tr").first()).toContainText("doc-queued.pdf");
    await expect(page.locator("tbody tr").first()).toContainText("ファイル準備の順番待ち");
    const filteredRequest = requests.findLast((url) => url.searchParams.has("queued_phase"));
    expect(filteredRequest?.searchParams.get("queued_phase")).toBe("PREPROCESS");
    expect(filteredRequest?.searchParams.has("status")).toBe(false);

    // ファイル準備待ちには順番待ちの行を入れない（行の表示とそろえる）。
    await status.click();
    await page.getByRole("option", { name: "ファイル準備待ち", exact: true }).click();
    await expect(toolbar).toContainText("1 件（全 3 件）");
    await expect(page.locator("tbody tr").first()).toContainText("doc-waiting.pdf");
    expect(requests.at(-1)?.searchParams.get("status")).toBe("UPLOADED");
    expect(requests.at(-1)?.searchParams.has("queued_phase")).toBe(false);

    // 最も長い選択肢でも、ボタンの中の値が切れない。
    await status.click();
    await page.getByRole("option", { name: "Embedding / 索引の順番待ち" }).click();
    await expect(status).toContainText("Embedding / 索引の順番待ち");
    const clipped = await status.evaluate((element) =>
      Array.from(element.querySelectorAll<HTMLElement>(".truncate")).some(
        (span) => span.scrollWidth > span.clientWidth
      )
    );
    expect(clipped).toBe(false);
    await closeFileListFilters(page);
  });
});
