import { openFileListFilters, closeFileListFilters } from "./_helpers";
import { expectGrowingList } from "./_list-viewport";
import type { SourceProfile } from "../src/lib/api";
import { expect, type Page, test } from "./fixtures/test";
import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

// Issue 281: 文書インデックスの不具合（範囲外のページ・投入結果の通知・検索欄の blur・状態の絞り込み・
// 削除の後始末の警告）の回帰。desktop / mobile の両 project で動く。

type FileStatus = "UPLOADED" | "PREPROCESSED" | "INGESTING" | "INDEXED" | "ERROR";

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
  source_profile: SourceProfile | null;
  layers_rebuild_required?: boolean;
  superseded_by_document_id?: string | null;
  is_superseded?: boolean;
}

interface BulkItem {
  document_id: string;
  outcome: "accepted" | "already_active" | "skipped" | "rejected";
  reason: string | null;
  message: string | null;
  job_id: string | null;
}

interface MockOptions {
  onEnqueue?: (id: string) => { status: number; json: unknown };
  /** 一括投入（#2674）の文書ごとの結果。既定はすべて受付済み。 */
  onBulkItem?: (id: string) => BulkItem;
  /** 一括投入の要求を通信断にする。 */
  abortBulk?: boolean;
  /** 一括投入の要求の本文（document_ids）を記録する。 */
  bulkRequests?: string[][];
  deleteWarnings?: string[];
  /** 一覧の応答を遅らせる（返した Promise が解決するまで応答しない）。 */
  delayList?: (url: URL) => Promise<void> | undefined;
}

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

test("文書種別は未分類でも原本の PDF・Word・Excel を表示する", async ({ page }, testInfo) => {
  const documents = [
    documentSummary("type-pdf", "sample.pdf", "INDEXED"),
    documentSummary("type-word", "sample.docx", "UPLOADED"),
    documentSummary("type-excel", "sample.xlsx", "INDEXED"),
    documentSummary("type-slides", "sample.pptx", "ERROR"),
    documentSummary("type-unknown", "source", "UPLOADED"),
  ];
  for (const document of documents) { document.category_name = null; document.content_type = null; }
  documents[0].source_profile = {
    original_file_name: "sample.pdf", sanitized_file_name: "sample.pdf", extension: ".pdf",
    content_type: "application/pdf", inferred_content_type: "application/pdf", modality: "pdf",
    file_size_bytes: 1024, content_sha256: "", parser_profile: "enterprise_ai_pdf_layout",
    parser_backend: "docling", parser_version: "1", preview_kind: "pdf", text_charset: null,
    duplicate_of_document_id: null, unsupported_reason: null, quality_status: "ready", quality_warnings: [],
  };
  // 旧カテゴリがあってもファイル形式とは混同しない。
  documents[1].category_name = "カテゴリ A";
  await mockFileListApi(page, documents);
  await page.goto("/file-list");
  await expect(page.getByRole("columnheader", { name: "文書種別", exact: true })).toBeVisible();
  for (const [index, label] of ["PDF", "Word", "Excel", "PowerPoint", "不明"].entries()) {
    const row = page.getByRole("row").filter({ has: page.getByRole("link", {name: documents[index].file_name, exact: true}) });
    await expect(row.getByRole("cell", { name: label, exact: true })).toBeVisible();
    await expect(row).not.toContainText("カテゴリ A");
  }
  await expectNoPageOverflow(page);
  await page.getByRole("columnheader", { name: "文書種別", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({path: testInfo.outputPath("document-types.png"), fullPage: true});
});

test("最後のページの最後の 1 件を削除したら、1 つ前のページへ戻る", async ({ page }) => {
  const documents = Array.from({ length: 51 }, (_, index) =>
    documentSummary(`doc-${index + 1}`, `file-${String(index + 1).padStart(2, "0")}.txt`, "INDEXED")
  );
  await mockFileListApi(page, documents);

  await page.goto("/file-list");
  // 共通の Pagination（25 件/ページ、#265・#1952）。3 ページ目へ移る。
  const pagination = page.getByTestId("file-list-pagination");
  await pagination.getByRole("button", { name: "次へ" }).click();
  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(pagination).toContainText("51–51 / 51 件");

  const last = documents[50];
  await page.getByRole("button", { name: `${last.file_name} の操作` }).click();
  await page.getByRole("menuitem", { name: `${last.file_name} を削除` }).click();
  await page
    .getByRole("alertdialog", { name: "このドキュメントを削除しますか？" })
    .getByRole("button", { name: "削除" })
    .click();

  // 空の 3 ページ目に「該当なし」とだけ出してページ送りが消える状態にしない。
  await expect(pagination).toContainText("26–50 / 50 件");
  await expect(page.getByRole("link", { name: "file-26.txt" })).toBeVisible();
  await expect(page.getByText("該当するドキュメントがありません。")).toHaveCount(0);
});

test("ファイル準備の投入の失敗・スキップを通知する", async ({ page }) => {
  const documents = [
    documentSummary("doc-queued", "queued.txt", "UPLOADED"),
    documentSummary("doc-dup", "duplicate.txt", "UPLOADED"),
  ];
  await mockFileListApi(page, documents, {
    onEnqueue: (id) =>
      id === "doc-queued"
        ? {
            status: 409,
            json: {
              data: null,
              error_messages: [
                "このドキュメントは取込待ちまたは取込中です。完了してから再実行してください。",
              ],
              warning_messages: [],
            },
          }
        : {
            status: 200,
            json: {
              data: ingestionJob(id, "SKIPPED", "duplicate_content"),
              error_messages: [],
              warning_messages: [],
            },
          },
  });

  await page.goto("/file-list");

  await page.getByRole("button", { name: "queued.txt の操作" }).click();
  await page.getByRole("menuitem", { name: "ファイル準備を実行" }).click();
  await expect(
    page.getByText("「queued.txt」のファイル準備を開始できませんでした。").first()
  ).toBeVisible();
  await expect(
    page.getByText("このドキュメントは取込待ちまたは取込中です。完了してから再実行してください。").first()
  ).toBeVisible();

  await page.getByRole("button", { name: "duplicate.txt の操作" }).click();
  await page.getByRole("menuitem", { name: "ファイル準備を実行" }).click();
  await expect(
    page.getByText("「duplicate.txt」はファイル準備をスキップしました。").first()
  ).toBeVisible();
  await expect(page.getByText("同一内容の文書が既に登録されています。").first()).toBeVisible();
});

// #2674: 一括投入はサーバーの一括の受付 API を 1 回呼び、受付済み・処理中・スキップ・受付不可の件数と理由を出す。
test("一括投入は件数と理由を出し、受付不可の文書だけを選択に残す", async ({ page }) => {
  const documents = [
    documentSummary("doc-ok", "ok.txt", "UPLOADED"),
    documentSummary("doc-ng", "ng.txt", "ERROR"),
  ];
  const bulkRequests: string[][] = [];
  await mockFileListApi(page, documents, {
    bulkRequests,
    onBulkItem: (id) =>
      id === "doc-ok"
        ? bulkItem(id, "accepted")
        : bulkItem(id, "rejected", "conflict", "このドキュメントは現在取込中です。"),
  });

  await page.goto("/file-list");
  await page.getByRole("checkbox", { name: "すべて選択" }).check();
  await page.getByRole("button", { name: "一括投入 (2)" }).click();

  const result = page.getByTestId("file-list-bulk-ingest-result");
  await expect(result).toContainText("2 件のうち 1 件のファイル準備を受け付けました。");
  await expect(result).toContainText("受付済み 1 件・処理中 0 件・スキップ 0 件・受付不可 1 件");
  await expect(result).toContainText("受付不可: このドキュメントは現在取込中です。（1 件）");
  await expect(result).toContainText("受付不可の文書は選択したままにしています。");
  // 1 回の要求で送り、受付不可の文書だけを選択に残す（失敗分まで選択を解除しない）。
  expect(bulkRequests).toEqual([["doc-ok", "doc-ng"]]);
  await expect(page.getByTestId("file-list-bulk-actions")).toContainText("1 件選択中");
  await expect(
    page.locator("tbody tr").filter({ hasText: "ng.txt" }).getByRole("checkbox")
  ).toBeChecked();
  await expect(
    page.locator("tbody tr").filter({ hasText: "ok.txt" }).getByRole("checkbox")
  ).not.toBeChecked();
  // 残した文書は理由を確かめてから、もう一度投入できる。
  await expect(page.getByRole("button", { name: "一括投入 (1)" })).toBeEnabled();

  await result.getByRole("button", { name: "一括投入の結果を閉じる" }).click();
  await expect(result).toHaveCount(0);
});

test(
  "表示中をすべて選んだら、条件に一致する全件を 1 回の操作で投入できる",
  { tag: "@responsive" },
  async ({ page }) => {
    const documents = Array.from({ length: 60 }, (_, index) =>
      documentSummary(`doc-${index + 1}`, `file-${String(index + 1).padStart(2, "0")}.txt`, "UPLOADED")
    );
    const bulkRequests: string[][] = [];
    await mockFileListApi(page, documents, { bulkRequests });

    await page.goto("/file-list");
    const bulkActions = page.getByTestId("file-list-bulk-actions");
    await page.getByRole("checkbox", { name: "すべて選択" }).check();
    await expect(bulkActions).toContainText("25 件選択中");
    await bulkActions.getByRole("button", { name: "条件に一致する 60 件をすべて選択" }).click();

    await expect(bulkActions).toContainText("条件に一致する 60 件をすべて選択中");
    // 全ページを選んだ間は一括投入だけ（削除は表示中の行を選んで行う）。
    await expect(bulkActions.getByRole("button", { name: /一括削除/ })).toBeDisabled();
    await expectNoPageOverflow(page);
    await bulkActions.getByRole("button", { name: "一括投入 (60)" }).click();

    await expect(
      page.getByText("60 件のファイル準備を受け付けました。").first()
    ).toBeVisible();
    expect(bulkRequests).toHaveLength(1);
    expect(bulkRequests[0]).toHaveLength(60);
    expect(new Set(bulkRequests[0])).toEqual(new Set(documents.map((item) => item.id)));
    await expect(bulkActions).toContainText("行を選ぶと、まとめて取込・削除できます。");
    await expect(page.getByTestId("file-list-bulk-ingest-result")).toHaveCount(0);
  }
);

test(
  "絞り込みを変えた直後は、前の条件の件数で全件の選択・一括投入を出さない（#2696）",
  { tag: "@responsive" },
  async ({ page }) => {
    const documents = [
      ...Array.from({ length: 40 }, (_, index) =>
        documentSummary(`doc-u-${index + 1}`, `upload-${String(index + 1).padStart(2, "0")}.txt`, "UPLOADED")
      ),
      ...Array.from({ length: 20 }, (_, index) =>
        documentSummary(`doc-e-${index + 1}`, `error-${String(index + 1).padStart(2, "0")}.txt`, "ERROR")
      ),
    ];
    // 状態で絞り込んだ一覧の応答だけを、手で解除するまで遅らせる（遅い一覧の API の再現）。
    let releaseFiltered: () => void = () => undefined;
    const filteredGate = new Promise<void>((resolve) => {
      releaseFiltered = resolve;
    });
    const bulkRequests: string[][] = [];
    await mockFileListApi(page, documents, {
      bulkRequests,
      delayList: (url) => (url.searchParams.get("status") === "UPLOADED" ? filteredGate : undefined),
    });

    await page.goto("/file-list");
    const bulkActions = page.getByTestId("file-list-bulk-actions");
    const selectAll = page.getByRole("checkbox", { name: "すべて選択" });
    await selectAll.check();
    await bulkActions.getByRole("button", { name: "条件に一致する 60 件をすべて選択" }).click();
    await expect(bulkActions).toContainText("条件に一致する 60 件をすべて選択中");

    // 条件を変えたら全件の選択を解除し、新しい一覧を読み込む間は前の条件の件数を出さず、押せない。
    await openFileListFilters(page);
    await page.getByRole("combobox", { name: "状態" }).click();
    await page.getByRole("option", { name: "ファイル準備待ち", exact: true }).click();
    await closeFileListFilters(page);
    await expect(bulkActions).toContainText("新しい条件の一覧を読み込んでいます。");
    await expect(bulkActions).not.toContainText("60 件");
    await expect(bulkActions.getByTestId("file-list-select-all-matching")).toHaveCount(0);
    await expect(bulkActions.getByRole("button", { name: /一括投入/ })).toBeDisabled();
    await expect(bulkActions.getByRole("button", { name: /一括削除/ })).toBeDisabled();
    await expect(selectAll).toBeDisabled();
    await expect(page.getByTestId("file-list-toolbar")).not.toContainText("60 件");
    await expectNoPageOverflow(page);

    releaseFiltered();
    await expect(page.getByTestId("file-list-toolbar")).toContainText("40 件（全 60 件）");
    await expect(bulkActions).toContainText("行を選ぶと、まとめて取込・削除できます。");
    await selectAll.check();
    await bulkActions.getByRole("button", { name: "条件に一致する 40 件をすべて選択" }).click();
    await bulkActions.getByRole("button", { name: "一括投入 (40)" }).click();

    await expect(page.getByText("40 件のファイル準備を受け付けました。").first()).toBeVisible();
    expect(bulkRequests).toHaveLength(1);
    expect(new Set(bulkRequests[0])).toEqual(
      new Set(documents.filter((item) => item.status === "UPLOADED").map((item) => item.id))
    );
  }
);

test("一括投入の結果を確かめられないときは選択を残し、再送を案内する", async ({ page }) => {
  const documents = [
    documentSummary("doc-a", "a.txt", "UPLOADED"),
    documentSummary("doc-b", "b.txt", "UPLOADED"),
  ];
  await mockFileListApi(page, documents, { abortBulk: true });

  await page.goto("/file-list");
  await page.getByRole("checkbox", { name: "すべて選択" }).check();
  await page.getByRole("button", { name: "一括投入 (2)" }).click();

  const error = page.getByTestId("file-list-bulk-ingest-error");
  await expect(error).toContainText("受付済みの文書は二重に登録せず、残りだけを受け付けます。");
  await expect(page.getByTestId("file-list-bulk-actions")).toContainText("2 件選択中");
});

test("検索欄から focus を外しただけでは選択とページを解除しない", async ({ page }) => {
  const documents = Array.from({ length: 51 }, (_, index) =>
    documentSummary(`doc-${index + 1}`, `file-${String(index + 1).padStart(2, "0")}.txt`, "UPLOADED")
  );
  await mockFileListApi(page, documents);

  await page.goto("/file-list");
  const pagination = page.getByTestId("file-list-pagination");
  await pagination.getByRole("button", { name: "次へ" }).click();
  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(pagination).toContainText("51–51 / 51 件");
  await page.locator("tbody tr").filter({ hasText: "file-51.txt" }).getByRole("checkbox").check();
  await expect(page.getByText("1 件選択中")).toBeVisible();

  const search = page.getByRole("searchbox", { name: "ファイル名で検索" });
  await search.focus();
  await search.blur();

  await expect(page.getByText("1 件選択中")).toBeVisible();
  await expect(pagination).toContainText("51–51 / 51 件");
  await expect(search).toHaveAttribute("maxlength", "200");
});

test("ファイル準備確認待ちで絞り込める", async ({ page }) => {
  const documents = [
    documentSummary("doc-pre", "prepared.pdf", "PREPROCESSED"),
    documentSummary("doc-idx", "indexed.pdf", "INDEXED"),
  ];
  const requested: (string | null)[] = [];
  await mockFileListApi(page, documents, {}, (url) => requested.push(url.searchParams.get("status")));

  await page.goto("/file-list");
  // 状態の絞り込みは選択の欄（#578）。
  await openFileListFilters(page);
    await page.getByRole("combobox", { name: "状態" }).click();
  await page.getByRole("option", { name: "ファイル準備確認待ち" }).click();
  await closeFileListFilters(page);

  await expect(page.getByRole("link", { name: "prepared.pdf" })).toBeVisible();
  await expect(page.getByRole("link", { name: "indexed.pdf" })).toHaveCount(0);
  expect(requested).toContain("PREPROCESSED");
});

test("削除の後始末に失敗した警告を成功として黙らせない", async ({ page }) => {
  const documents = [documentSummary("doc-1", "policy.txt", "INDEXED")];
  await mockFileListApi(page, documents, {
    deleteWarnings: [
      "文書は削除しましたが、原本ファイルの削除に失敗しました。保存先を確認してください。",
    ],
  });

  await page.goto("/file-list");
  await page.getByRole("button", { name: "policy.txt の操作" }).click();
  await page.getByRole("menuitem", { name: "policy.txt を削除" }).click();
  await page
    .getByRole("alertdialog", { name: "このドキュメントを削除しますか？" })
    .getByRole("button", { name: "削除" })
    .click();

  await expect(
    page
      .getByText("「policy.txt」を削除しましたが、保存先のファイルの後始末に一部失敗しました。")
      .first()
  ).toBeVisible();
  await expect(
    page.getByText("原本ファイルの削除に失敗しました。保存先を確認してください。").first()
  ).toBeVisible();
});

test("自動更新の失敗では一覧と選択を残し、失敗を知らせて再試行できる（#938）", async ({ page }) => {
  // ポーリング（4 秒）と TanStack Query の再試行（1・2・4 秒）を待たずに進める。
  await page.clock.install();
  const documents = [
    documentSummary("doc-1", "running.txt", "INGESTING"),
    documentSummary("doc-2", "policy.txt", "UPLOADED"),
  ];
  await mockFileListApi(page, documents);
  let failing = false;
  // 後から登録した route が先に当たる。一覧の取得だけを失敗させる。
  await page.route("**/api/documents?**", async (route) => {
    if (!failing) {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 500,
      json: { data: null, error_messages: ["DB に接続できません。"], warning_messages: [] },
    });
  });

  await page.goto("/file-list");
  await expect(page.getByRole("link", { name: "policy.txt" })).toBeVisible();
  await page.getByRole("checkbox", { name: "この行を選択" }).nth(1).check();
  await expect(page.getByTestId("file-list-bulk-actions")).toContainText("1 件選択中");

  failing = true;
  await page.clock.runFor(30_000);

  // 前回取得した一覧と選択は残し、全面のエラーに置き換えない。
  const warning = page.getByTestId("file-list-refresh-error");
  await expect(warning).toContainText("最新の状態を取得できませんでした");
  await expect(warning).toContainText("DB に接続できません。");
  await expect(page.getByRole("link", { name: "policy.txt" })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "この行を選択" }).nth(1)).toBeChecked();
  await expect(page.getByTestId("file-list-bulk-actions")).toContainText("1 件選択中");
  await expectNoPageOverflow(page);

  // 取得が戻ったら、再試行で失敗の表示を消す。
  failing = false;
  await warning.getByRole("button", { name: "再試行" }).click();
  await expect(warning).toHaveCount(0);
  await expect(page.getByRole("link", { name: "policy.txt" })).toBeVisible();
});

async function mockFileListApi(
  page: Page,
  documents: DocumentSummary[],
  options: MockOptions = {},
  onList?: (url: URL) => void
) {
  await page.route("**/api/knowledge-bases**", async (route) => {
    await route.fulfill({
      json: {
        data: { items: [], total: 0, limit: 100, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  await page.route("**/api/documents**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const parts = url.pathname.split("/").filter(Boolean);

    if (request.method() === "GET" && url.pathname === "/api/documents") {
      onList?.(url);
      await options.delayList?.(url);
      const status = url.searchParams.get("status");
      const limit = Number(url.searchParams.get("limit") ?? "25");
      const offset = Number(url.searchParams.get("offset") ?? "0");
      const filtered = documents.filter((document) => !status || document.status === status);
      await route.fulfill({
        json: {
          data: {
            items: filtered.slice(offset, offset + limit),
            total: filtered.length,
            limit,
            offset,
            has_next: offset + limit < filtered.length,
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (request.method() === "GET" && url.pathname === "/api/documents/ids") {
      const status = url.searchParams.get("status");
      const filtered = documents.filter((document) => !status || document.status === status);
      await route.fulfill({
        json: {
          data: { ids: filtered.map((item) => item.id), total: filtered.length, limit: 10000 },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (request.method() === "POST" && url.pathname === "/api/documents/ingestion-jobs/bulk") {
      if (options.abortBulk) {
        await route.abort("connectionreset");
        return;
      }
      const ids = (request.postDataJSON() as { document_ids: string[] }).document_ids;
      options.bulkRequests?.push(ids);
      const items = ids.map((id) => options.onBulkItem?.(id) ?? bulkItem(id, "accepted"));
      const count = (outcome: BulkItem["outcome"]) =>
        items.filter((item) => item.outcome === outcome).length;
      const reasons = new Map<string, { outcome: string; reason: string; count: number; message: string | null }>();
      for (const item of items) {
        if (!item.reason) continue;
        const key = `${item.outcome}:${item.reason}:${item.message ?? ""}`;
        const entry = reasons.get(key);
        if (entry) entry.count += 1;
        else reasons.set(key, { outcome: item.outcome, reason: item.reason, count: 1, message: item.message });
      }
      await route.fulfill({
        json: {
          data: {
            requested_count: ids.length,
            unique_count: ids.length,
            accepted_count: count("accepted"),
            already_active_count: count("already_active"),
            skipped_count: count("skipped"),
            rejected_count: count("rejected"),
            reasons: [...reasons.values()],
            items,
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }

    if (request.method() === "POST" && parts[3] === "ingestion-jobs") {
      const result = options.onEnqueue?.(parts[2]) ?? {
        status: 200,
        json: { data: ingestionJob(parts[2], "QUEUED"), error_messages: [], warning_messages: [] },
      };
      await route.fulfill(result);
      return;
    }

    if (request.method() === "DELETE" && parts.length === 3) {
      const id = parts[2];
      const index = documents.findIndex((item) => item.id === id);
      const document = documents[index];
      documents.splice(index, 1);
      await route.fulfill({
        json: {
          data: {
            id,
            file_name: document.file_name,
            object_storage_path: `local://uploaded/${document.file_name}`,
            object_deleted: (options.deleteWarnings ?? []).length === 0,
            artifact_deleted_count: 0,
            artifact_delete_failed_count: 0,
          },
          error_messages: [],
          warning_messages: options.deleteWarnings ?? [],
        },
      });
      return;
    }

    // 投入後の再取得（ジョブ一覧など）は空で返す。
    await route.fulfill({ json: { data: [], error_messages: [], warning_messages: [] } });
  });
}

function bulkItem(
  documentId: string,
  outcome: BulkItem["outcome"],
  reason: string | null = null,
  message: string | null = null
): BulkItem {
  return {
    document_id: documentId,
    outcome,
    reason,
    message,
    job_id: outcome === "accepted" ? `job-${documentId}` : null,
  };
}

function ingestionJob(documentId: string, status: string, skipReason: string | null = null) {
  return {
    id: `job-${documentId}`,
    document_id: documentId,
    recipe_id: null,
    recipe_revision: null,
    status,
    phase: "PREPROCESS",
    parser_profile: "local_text_structure",
    quality_warnings: [],
    skip_reason: skipReason,
    error_message: null,
    attempt_count: 0,
    max_attempts: 3,
    queued_at: "2026-09-28T00:00:00Z",
    started_at: null,
    finished_at: status === "SKIPPED" ? "2026-09-28T00:00:00Z" : null,
  };
}

for (const theme of ["light", "dark"] as const) {
  test(`作り直しが必要な派生情報がある文書に、状態の横へ印を出す (${theme})`, async ({
    page,
  }, testInfo) => {
    // 外観の選好（共有 UI の ui-store が保存する値）を読み込みの前に入れておく。
    await page.addInitScript((value) => {
      window.localStorage.setItem(
        "production-ready-rag.ui",
        JSON.stringify({ state: { theme: value }, version: 0 })
      );
    }, theme);
    const stale = { ...documentSummary("doc-stale", "stale.pdf", "INDEXED"), layers_rebuild_required: true };
    const fresh = documentSummary("doc-fresh", "fresh.pdf", "INDEXED");
    await mockFileListApi(page, [stale, fresh]);

    await page.goto("/file-list");

    // 状態（索引済み）はそのままに、警告の印をアイコンと文言で添える（#550）。
    const badge = page.getByTestId("file-list-rebuild-doc-stale");
    await expect(badge).toContainText("作り直しが必要");
    await expect(badge.locator("svg")).toHaveCount(1);
    await expect(badge).toHaveAttribute("title", /「再処理」すると作り直せます/);
    await expect(page.getByTestId("file-list-rebuild-doc-fresh")).toHaveCount(0);
    await expect(badge.locator("..")).toContainText("索引済み");
    await expectNoPageOverflow(page);
    await page.screenshot({
      path: testInfo.outputPath(`file-list-rebuild-${theme}.png`),
      fullPage: true,
    });
  });
}

test("新しい版に置き換えた文書（旧版）に印を出す（#1248）", async ({ page }) => {
  const old = {
    ...documentSummary("doc-v1", "manual-v1.pdf", "INDEXED"),
    superseded_by_document_id: "doc-v2",
    is_superseded: true,
  };
  const current = documentSummary("doc-v2", "manual-v2.pdf", "INDEXED");
  await mockFileListApi(page, [old, current]);

  await page.goto("/file-list");

  // 状態（索引済み）はそのままに、旧版の印をアイコンと文言で添える。
  const badge = page.getByTestId("file-list-superseded-doc-v1");
  await expect(badge).toContainText("旧版");
  await expect(badge.locator("svg")).toHaveCount(1);
  await expect(badge).toHaveAttribute("title", /回答の検索の対象から外れています/);
  await expect(badge.locator("..")).toContainText("索引済み");
  await expect(page.getByTestId("file-list-superseded-doc-v2")).toHaveCount(0);
  await expectNoPageOverflow(page);
});

function documentSummary(id: string, fileName: string, status: FileStatus): DocumentSummary {
  return {
    id,
    file_name: fileName,
    status,
    category_name: "社内規程",
    content_type: "text/plain",
    file_size_bytes: 1024,
    content_sha256: "a".repeat(64),
    duplicate_of_document_id: null,
    uploaded_at: "2026-06-16T09:00:00Z",
    indexed_at: status === "INDEXED" ? "2026-06-16T09:05:00Z" : null,
    knowledge_bases: [],
    source_profile: null,
  };
}


test("削除は 30 秒を超える応答を待ち、未確認なら選択を残す", async ({ page }, testInfo) => {
  await page.clock.install();
  const documents = [documentSummary("doc-slow", "slow.txt", "INDEXED")];
  await mockFileListApi(page, documents);
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let deleteRequests = 0;
  let lookups = 0;
  await page.route("**/api/documents/doc-slow", async (route) => {
    if (route.request().method() === "DELETE") {
      deleteRequests++;
      await pending;
      await route.abort("connectionreset");
      return;
    }
    lookups++;
    await route.fulfill({json: {data: documents[0], error_messages: [], warning_messages: []}});
  });
  await page.goto("/file-list");
  await page.getByRole("checkbox", {name: "この行を選択"}).check();
  await page.getByRole("button", {name: "一括削除 (1)"}).click();
  await page.getByRole("alertdialog").getByRole("button", {name: "一括削除", exact: true}).click();
  await expect.poll(() => deleteRequests).toBe(1);
  await page.clock.runFor(45_000);
  await expect(page.getByRole("link", {name: "slow.txt"})).toBeVisible();
  await expect(page.getByText("サーバーの応答が 30 秒以内に返りませんでした。", {exact: false})).toHaveCount(0);
  release?.();
  for (let attempt = 1; attempt <= 10; attempt++) {
    await expect.poll(() => lookups).toBeGreaterThanOrEqual(attempt);
    await page.clock.runFor(2_000);
  }
  await expect(page.getByText(/結果未確認 1 件/).first()).toBeVisible();
  await expect(page.getByRole("checkbox", {name: "この行を選択"})).toBeChecked();
  expect(deleteRequests).toBe(1);
  await expectNoPageOverflow(page);
  await page.screenshot({path: testInfo.outputPath("delete-unconfirmed.png"), fullPage: true});
});


for (const width of [1280, 375]) {
  test(`混在形式の受付不可理由を形式ごとに表示する (${width}px)`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await mockFileListApi(page, [documentSummary("doc-a", "input.xlsx", "UPLOADED"), documentSummary("doc-b", "input.md", "UPLOADED")], {
      onBulkItem: id => bulkItem(id, "rejected", "parser_unsupported_source", id === "doc-a" ? "Excel 形式は別の解析エンジンを選んでください。" : "Markdown 形式は別の解析エンジンを選んでください。"),
    });
    await page.goto("/file-list");
    await page.getByRole("checkbox", { name: "すべて選択" }).check();
    await page.getByRole("button", { name: "一括投入 (2)" }).click();
    const result = page.getByTestId("file-list-bulk-ingest-result");
    await expect(result).toContainText("Excel 形式は別の解析エンジンを選んでください。");
    await expect(result).toContainText("Markdown 形式は別の解析エンジンを選んでください。");
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath("bulk-mixed-reasons.png"), fullPage: true });
  });
}

test("主一覧は高い画面の余白へ拡張し、ページ送りを維持する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockFileListApi(page, Array.from({ length: 60 }, (_, i) => documentSummary(`viewport-${i}`, `sample-${i}.pdf`, "INDEXED")));
  await page.goto("/file-list");
  await expectGrowingList(page, testInfo, "file-list");
});


test("言語資源の対象条件・版の内訳を一覧とページ送りで維持する", { tag: "@responsive" }, async ({ page }) => {
  const documents = Array.from({ length: 30 }, (_, index) => ({
    ...documentSummary(`release-doc-${index}`, `sample-${index}.txt`, "INDEXED"), category_name: null,
    language_pack: { current_release_id: "release-current", current_version: 3, recipes: [
      { recipe_id: "recipe-1", chunk_set_id: "chunks-1", release_id: "release-old", version: 1, ingest_hash: "old", relation: "different", built_at: null, job_id: null, target_release_id: null },
      { recipe_id: "recipe-2", chunk_set_id: "chunks-2", release_id: "release-current", version: 3, ingest_hash: "new", relation: "current", built_at: null, job_id: null, target_release_id: null },
    ] },
  }));
  const requests: URL[] = [];
  await mockFileListApi(page, documents, {}, url => requests.push(url));
  await page.goto("/file-list?language_pack_relation=different&current_release_id=release-current");
  await expect.poll(() => requests.some(url => url.searchParams.get("language_pack_relation") === "different" && url.searchParams.get("current_release_id") === "release-current")).toBe(true);
  await expect(page.getByText("複数の版").first()).toBeVisible();
  await expect(page.getByRole("link", { name: "使用版と差分" }).first()).toHaveAttribute("href", "/documents/release-doc-0?language_pack=1");
  await page.getByTestId("file-list-pagination").getByRole("button", { name: "次へ", exact: true }).click();
  await expect.poll(() => requests.filter(url => url.searchParams.get("language_pack_relation") === "different").at(-1)?.searchParams.get("offset")).toBe("25");
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("document-language-versions.png"), fullPage: true });
});


test("ページをまたぐ選択から実在のレシピを確認し、固定版と設定版で一括再処理して部分失敗を残す", { tag: "@responsive" }, async ({ page }) => {
  const documents = Array.from({ length: 30 }, (_, index) => ({ ...documentSummary(`batch-doc-${index}`, `sample-${index}.txt`, "INDEXED"), category_name: null, language_pack: { current_release_id: "release-current", current_version: 3, recipes: [] } }));
  await mockFileListApi(page, documents);
  const submissions: Record<string, unknown>[] = [];
  const idsQueries: URL[] = [];
  page.on("request", request => { if (new URL(request.url()).pathname.endsWith("/documents/ids")) idsQueries.push(new URL(request.url())); });
  await page.route("**/api/documents/language-pack/reprocessing-targets", async route => {
    const payload = route.request().postDataJSON();
    expect(payload.document_ids).toHaveLength(30); expect(payload.expected_release_id).toBe("release-current");
    await route.fulfill({ json: { data: { current_release: { id: "release-current", version: 3, effective: {}, hash: "current", ingest_hash: "current", published_at: "2026-10-17T00:00:00Z", published_by: null, previous_id: null, restored_from: null, change_note: "" }, targets: [
      { document_id: "batch-doc-0", file_name: "sample-0.txt", recipe_id: "recipe-2", slot_no: 2, config_revision: 4, used_release_id: "release-old", used_ingest_hash: "old", relation: "different" },
      { document_id: "batch-doc-29", file_name: "sample-29.txt", recipe_id: "recipe-3", slot_no: 3, config_revision: 7, used_release_id: "release-old", used_ingest_hash: "old", relation: "different" },
    ], unavailable_count: 28 }, error_messages: [], warning_messages: [] } });
  });
  const comparisons: URL[] = [];
  await page.route("**/api/settings/language-pack/releases/compare?*", async route => {
    comparisons.push(new URL(route.request().url()));
    await route.fulfill({ json: { data: { before_id: "release-old", after_id: "release-current", differences: [{ section: "numbers", key: "article_units", before: ["条"], after: ["条", "款"], ingest: true }], ingest_changed: true, impact_requires_review: false, affected_stages: ["EXTRACT", "CHUNK", "INDEX"] }, error_messages: [], warning_messages: [] } });
  });
  await page.route("**/api/documents/language-pack/reprocessing-jobs", async route => {
    submissions.push(route.request().postDataJSON());
    await route.fulfill({ json: { data: { accepted_count: 1, rejected_count: 1, items: [
      { document_id: "batch-doc-0", recipe_id: "recipe-2", job_id: "job-accepted", outcome: "accepted", reason: null },
      { document_id: "batch-doc-29", recipe_id: "recipe-3", job_id: null, outcome: "rejected", reason: "レシピの設定版が更新されました。" },
    ] }, error_messages: [], warning_messages: [] } });
  });
  await page.goto("/file-list?language_pack_relation=different&current_release_id=release-current");
  await page.getByRole("checkbox", { name: "すべて選択", exact: true }).check();
  await page.getByRole("button", { name: "条件に一致する 30 件をすべて選択", exact: true }).click();
  const panel = page.getByTestId("language-pack-bulk-reprocess");
  await panel.getByRole("button", { name: "選択文書の再処理を確認する" }).click();
  await expect(panel.getByText("対象を確認できない文書 28 件")).toBeVisible();
  expect(idsQueries.at(-1)?.searchParams.get("language_pack_relation")).toBe("different");
  expect(idsQueries.at(-1)?.searchParams.get("current_release_id")).toBe("release-current");
  const targets = panel.getByRole("table", { name: "再処理するレシピを選ぶ" });
  await expect(targets.getByRole("checkbox", { checked: true })).toHaveCount(0);
  await targets.getByRole("checkbox", { name: "すべて選択", exact: true }).check();
  await panel.getByRole("button", { name: "選択レシピの差分と影響を確認する" }).click();
  await expect(panel.getByText("sample-0.txt / レシピ2、sample-29.txt / レシピ3", { exact: true })).toBeVisible();
  expect(comparisons).toHaveLength(1);
  expect(comparisons[0].searchParams.get("before_id")).toBe("release-old");
  expect(comparisons[0].searchParams.get("after_id")).toBe("release-current");
  await panel.getByRole("combobox", { name: "再処理を始める工程" }).click();
  await page.getByRole("option", { name: "Chunk 作成", exact: true }).click();
  await panel.getByRole("button", { name: "再処理する (2)", exact: true }).click();
  const dialog = page.getByRole("alertdialog"); await expect(dialog).toContainText("2 文書・2 レシピ");
  await dialog.getByRole("button", { name: "再処理する", exact: true }).click();
  await expect.poll(() => submissions).toEqual([{ expected_release_id: "release-current", targets: [
    { document_id: "batch-doc-0", recipe_id: "recipe-2", phase: "CHUNK", expected_config_revision: 4 },
    { document_id: "batch-doc-29", recipe_id: "recipe-3", phase: "CHUNK", expected_config_revision: 7 },
  ] }]);
  await expect(panel.getByText("受付 1 レシピ・受付不可 1 レシピ。処理結果は各文書の状態で確認してください。", { exact: true })).toBeVisible();
  await expect(panel.getByText("sample-29.txt: レシピの設定版が更新されました。", { exact: true })).toBeVisible();
  await expectNoPageOverflow(page);
});
