import { expect, test, type Page } from "./fixtures/test";

import {
  apiEnvelope,
  expectNoPageOverflow,
  mockAuthUser,
  mockDatabaseReady,
  mockLocalAuth,
} from "./_helpers";

// 利用できる KB が制限された利用者のアップロード（#214）。
// KB を指定しないアップロードは backend で 400 になるため、画面は送信前に KB の選択を求める。

const KNOWLEDGE_BASES = [
  {
    id: "kb-1",
    name: "社内規程",
    description: "経費・人事",
    status: "ACTIVE",
    default_search_mode: "hybrid",
    document_count: 2,
    indexed_document_count: 2,
    error_document_count: 0,
    searchable_chunk_count: 8,
    created_at: "2026-06-15T00:00:00Z",
    updated_at: "2026-06-15T00:00:00Z",
    archived_at: null,
  },
];

async function mockUploadPage(page: Page) {
  const uploads: { body: string; csrf: string | null }[] = [];
  await mockDatabaseReady(page);
  await page.route("**/api/settings/upload-storage", (route) =>
    route.fulfill({
      json: apiEnvelope({
        backend: "local",
        local_storage_dir: "/tmp/rag-uploads",
        object_storage_namespace: null,
        object_storage_bucket: null,
        object_storage_region: null,
        readiness: "ready",
        source: "runtime",
        max_upload_bytes: 209715200,
      }),
    })
  );
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: apiEnvelope({ items: KNOWLEDGE_BASES, total: 1, limit: 50, offset: 0, has_next: false }),
    })
  );
  await page.route("**/api/documents/ingestion-jobs**", (route) =>
    route.fulfill({ json: apiEnvelope({ items: [], total: 0, limit: 5, offset: 0, has_next: false }) })
  );
  await page.route("**/api/documents/upload", async (route) => {
    const request = route.request();
    uploads.push({ body: request.postData() ?? "", csrf: request.headers()["x-csrf-token"] ?? null });
    // 送信内容だけを確かめる。文書ワークスペースへは進めない。
    await route.fulfill({
      status: 503,
      json: { data: null, error_messages: ["テスト用にアップロードを止めました。"], warning_messages: [] },
    });
  });
  return uploads;
}

function uploadFile() {
  return { name: "policy.txt", mimeType: "text/plain", buffer: Buffer.from("経費規程") };
}

test("KB が制限された利用者は、KB を選ばずにアップロードすると送信前に案内する", async ({
  page,
  context,
  baseURL,
}) => {
  const uploads = await mockUploadPage(page);
  await mockAuthUser(page, {
    permissions: ["menu.upload"],
    allowed_knowledge_base_ids: ["kb-1"],
  });
  await context.addCookies([{ name: "pr_csrf", value: "csrf-upload", url: baseURL ?? "http://127.0.0.1:3100" }]);

  await page.goto("/upload");
  const picker = page.getByTestId("upload-knowledge-base-picker");
  await expect(picker.getByText("必須")).toBeVisible();
  await expect(picker.getByText("登録先のナレッジベースを 1 件以上選択してください。")).toBeVisible();

  await page.locator('input[type="file"]').setInputFiles(uploadFile());
  await page.getByRole("button", { name: /アップロードを開始/ }).click();
  await expect(
    picker.getByRole("alert").filter({ hasText: "所属させるナレッジベースを 1 件以上選択してください。" })
  ).toBeVisible();
  expect(uploads).toHaveLength(0);
  await expectNoPageOverflow(page);

  // KB を選ぶと案内が消え、選んだ KB を付けて送る。
  await page.getByRole("combobox", { name: "所属させるナレッジベース" }).click();
  await page.getByRole("listbox", { name: "所属させるナレッジベース" }).getByRole("option", { name: /社内規程/ }).click();
  await page.keyboard.press("Escape");
  await expect(picker.getByRole("alert")).toHaveCount(0);
  await expect(picker.getByText("1 件のナレッジベースへ登録します。")).toBeVisible();

  // 選んだファイルは送るまで一覧に残るので、選び直さずに開始できる（#701）。
  await page.getByRole("button", { name: /アップロードを開始/ }).click();
  await expect.poll(() => uploads.length).toBe(1);
  expect(uploads[0].body).toMatch(/name="knowledge_base_ids"\r\n\r\nkb-1\r\n/);
  expect(uploads[0].csrf).toBe("csrf-upload");
});

test("ローカル DEBUG（範囲の制限なし）は KB の選択を必須にしない", async ({ page }) => {
  const uploads = await mockUploadPage(page);
  await mockLocalAuth(page);

  await page.goto("/upload");
  const picker = page.getByTestId("upload-knowledge-base-picker");
  await expect(picker.getByText("未選択の場合は DEFAULT へ登録します。")).toBeVisible();
  await expect(picker.getByText("必須")).toHaveCount(0);

  await page.locator('input[type="file"]').setInputFiles(uploadFile());
  await page.getByRole("button", { name: /アップロードを開始/ }).click();
  await expect.poll(() => uploads.length).toBe(1);
  expect(uploads[0].body).not.toContain('name="knowledge_base_ids"');
});
