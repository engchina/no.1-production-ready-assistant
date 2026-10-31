import { expect, type Page, test } from "./fixtures/test";
import { mockDatabaseReady, mockLocalAuth } from "./_helpers";

// 保存先はローカルのディレクトリだけ（OCI Object Storage は #11 で削除した）。
interface UploadStorageSettingsData {
  local_storage_dir: string;
  readiness: string;
  max_upload_bytes: number;
  config_source: "runtime";
}

const localStorageSettings: UploadStorageSettingsData = {
  local_storage_dir: "/u01/data/production-ready-rag",
  readiness: "ok",
  max_upload_bytes: 200 * 1024 * 1024,
  config_source: "runtime",
};

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

// 画面は platform の共有パッケージ（NL2SQL と同じ画面。#97）。右側の状態パネル / .env プレビューは持たない。
test("アップロード保存先設定でローカルの保存ディレクトリを変えて保存できる", { tag: "@responsive" }, async ({
  page,
}) => {
  let current: UploadStorageSettingsData = { ...localStorageSettings };
  let lastPayload: unknown = null;
  await mockUploadStorageSettings(
    page,
    () => current,
    async (payload) => {
      lastPayload = payload;
      current = {
        ...current,
        ...(payload as Partial<UploadStorageSettingsData>),
        readiness: "ok",
      };
    },
  );

  await page.goto("/settings/upload-storage");

  await expect(
    page.getByRole("heading", { name: "アップロード保存先" }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "保存先", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: ".env をコピー" })).toHaveCount(
    0,
  );
  // 保存先の種類の選択・OCI 認証設定への案内・「接続を確認」は持たない。
  await expect(page.getByRole("radio")).toHaveCount(0);
  await expect(page.getByText("OCI Object Storage")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "接続を確認" })).toHaveCount(0);
  const localDir = page.locator("#upload-storage-local-dir");
  await expect(localDir).toHaveValue("/u01/data/production-ready-rag");

  await localDir.fill("/u01/data/rag-uploads");
  await page.getByRole("button", { name: "保存" }).click();

  await expect(page.getByText("保存しました")).toBeVisible();
  expect(lastPayload).toEqual({ local_storage_dir: "/u01/data/rag-uploads" });
  await expect(localDir).toHaveValue("/u01/data/rag-uploads");
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

test("アップロード保存先は保存ディレクトリが空なら保存前に止める", async ({
  page,
}) => {
  let patchCount = 0;
  await mockUploadStorageSettings(
    page,
    () => localStorageSettings,
    async () => {
      patchCount += 1;
    },
  );

  await page.goto("/settings/upload-storage");

  const localDir = page.locator("#upload-storage-local-dir");
  await expect(localDir).toHaveValue(localStorageSettings.local_storage_dir);
  await localDir.fill("");
  await page.getByRole("button", { name: "保存" }).click();

  await expect(
    page.getByText("ローカル保存ディレクトリを入力してください。"),
  ).toBeVisible();
  await expect(localDir).toBeFocused();
  expect(patchCount).toBe(0);
});

test("アップロード保存先の読み込み中は経過時間とフォームの形の Skeleton を出す", async ({
  page,
}) => {
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/settings/upload-storage", async (route) => {
    await gate;
    await route.fulfill({
      json: {
        data: localStorageSettings,
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  await page.goto("/settings/upload-storage");

  const loading = page.getByTestId("settings-upload-storage-loading");
  await expect(loading).toBeVisible();
  await expect(loading).toContainText(
    "アップロード保存先設定を読み込んでいます。",
  );
  await expect(loading.locator('[data-skeleton="form"]')).toHaveCount(1);

  release();
  await expect(page.locator("#upload-storage-local-dir")).toHaveValue(
    localStorageSettings.local_storage_dir,
  );
  await expect(loading).toHaveCount(0);
});

test("保存の後の再取得に失敗してもフォームを残し、警告と再試行を出す", async ({
  page,
}) => {
  let current: UploadStorageSettingsData = { ...localStorageSettings };
  let failGet = false;
  await page.route("**/api/settings/upload-storage", async (route) => {
    const request = route.request();
    if (request.method() === "PATCH") {
      current = {
        ...current,
        ...(request.postDataJSON() as Partial<UploadStorageSettingsData>),
      };
      // 保存の応答の後の再取得（invalidate）から失敗させる。
      failGet = true;
    } else if (failGet) {
      await route.fulfill({
        status: 503,
        json: {
          data: null,
          error_messages: ["一時的に利用できません。"],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fulfill({
      json: { data: current, error_messages: [], warning_messages: [] },
    });
  });

  await page.goto("/settings/upload-storage");
  const localDir = page.locator("#upload-storage-local-dir");
  await expect(localDir).toHaveValue(localStorageSettings.local_storage_dir);
  await localDir.fill("/u01/data/changed");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("保存しました")).toBeVisible();

  // React Query の再試行（既定 3 回）の後に失敗になる。
  const warning = page.getByText(
    "最新のアップロード保存先設定を取得できませんでした。表示中の値は前回取得した内容です。再試行してください。",
  );
  await expect(warning).toBeVisible({ timeout: 20_000 });
  await expect(
    page.getByText("アップロード保存先設定の取得に失敗しました。"),
  ).toHaveCount(0);
  await expect(localDir).toHaveValue("/u01/data/changed");

  failGet = false;
  await page.getByRole("button", { name: "再試行" }).click();
  await expect(warning).toHaveCount(0);
  await expect(localDir).toHaveValue("/u01/data/changed");
});

test("アップロード画面は現在の保存先を表示し、保存先設定の導線を出さない", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockUploadStorageSettings(page, () => localStorageSettings);
  await mockKnowledgeBases(page);
  await mockIngestionJobs(page);

  await page.goto("/upload");

  await expect(page.getByText("現在の保存先")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "所属させるナレッジベース" }),
  ).toBeVisible();
  await expect(
    page.getByText("PDF・画像・テキスト・HTML・メール・Office"),
  ).toBeVisible();
  await expect(page.getByText("最大 200 MB")).toBeVisible();
  const acceptedTypes = await page
    .locator('input[type="file"]')
    .getAttribute("accept");
  expect(acceptedTypes).toContain(".docx");
  expect(acceptedTypes).toContain(".pptx");
  expect(acceptedTypes).toContain(".xlsx");
  expect(acceptedTypes).toContain(".html");
  expect(acceptedTypes).toContain(".eml");
  // 音声・動画は取り込まない形式で、ファイル選択に出さない（#3369）。
  expect(acceptedTypes).not.toContain("audio/");
  expect(acceptedTypes).not.toContain(".mp3");
  await expect(page.getByText("現在の保存先: ローカルディレクトリ")).toBeVisible();
  await expect(page.getByText("/u01/data/production-ready-rag")).toBeVisible();
  await expect(page.getByRole("link", { name: "保存先設定" })).toHaveCount(0);
});

test("アップロード時に選択したナレッジベースへ所属できる", async ({ page }) => {
  let uploadBody = "";
  await mockUploadStorageSettings(page, () => localStorageSettings);
  await mockKnowledgeBases(page);
  await mockIngestionJobs(page);
  await mockDocumentUpload(page, (body) => {
    uploadBody = body;
  });

  await page.goto("/upload");

  const kbCombo = page.getByRole("combobox", {
    name: "所属させるナレッジベース",
  });
  await kbCombo.click();
  await page.getByRole("option", { name: /社内規程/ }).click();
  await kbCombo.press("Escape");
  await expect(
    page.getByText("1 件のナレッジベースへ登録します。"),
  ).toBeVisible();
  await page.locator('input[type="file"]').setInputFiles({
    name: "upload.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("本文"),
  });
  await page.getByRole("button", { name: /アップロードを開始/ }).click();

  await expect(
    page.getByRole("link", { name: "upload.txt の文書詳細を開く" }),
  ).toBeVisible();
  expect(uploadBody).toContain('name="knowledge_base_ids"');
  expect(uploadBody).toContain("kb-1");
});

test("アップロード画面で原本の処理情報を確認できる", async ({ page }) => {
  await mockUploadStorageSettings(page, () => localStorageSettings);
  await mockKnowledgeBases(page);
  await mockIngestionJobs(page);
  await mockDocumentUpload(page, () => undefined);

  await page.goto("/upload");

  await page.locator('input[type="file"]').setInputFiles({
    name: "upload.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("本文"),
  });
  await page.getByRole("button", { name: /アップロードを開始/ }).click();

  // 結果の行に原本の解析方式を出し、文書詳細は埋め込まない（#707）。
  await expect(
    page.getByRole("heading", { name: "アップロード結果" }),
  ).toBeVisible();
  await expect(page.getByText("原本判定: テキスト構造化")).toBeVisible();
  await expect(page.getByText("処理の詳細(診断)")).toHaveCount(0);
  await expect(
    page.getByText(
      "取込（ファイル準備・文書解析・文書分割・索引）は、文書インデックスか各文書の詳細から始めます。",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "upload.txt の文書詳細を開く" }),
  ).toHaveAttribute("href", "/documents/doc-upload");
});

test("複数ファイルをまとめてアップロードし結果を確認できる", async ({
  page,
}) => {
  let uploadBody = "";
  await mockUploadStorageSettings(page, () => localStorageSettings);
  await mockKnowledgeBases(page);
  await mockIngestionJobs(page);
  await mockBatchDocumentUpload(page, (body) => {
    uploadBody = body;
  });

  await page.goto("/upload");

  await page.locator('input[type="file"]').setInputFiles([
    {
      name: "policy-a.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("A 本文"),
    },
    {
      name: "policy-b.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("B 本文"),
    },
    {
      name: "policy-c.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("C 本文"),
    },
    {
      name: "policy.exe",
      mimeType: "application/x-msdownload",
      buffer: Buffer.from("MZ"),
    },
  ]);
  // 対応していない形式は送る前に理由を示し、送らない（#701）。
  await expect(
    page
      .getByTestId("upload-selection")
      .getByRole("listitem")
      .filter({ hasText: "policy.exe" }),
  ).toContainText("対応していない形式");
  await page
    .getByRole("button", { name: "アップロードを開始（3 件）" })
    .click();

  await expect(
    page.getByRole("heading", { name: "アップロード結果" }),
  ).toBeVisible();
  await expect(page.getByTitle("policy-a.txt").first()).toBeVisible();
  await expect(page.getByTitle("policy-b.txt").first()).toBeVisible();
  await expect(
    page.getByText("一部のファイルをアップロードできませんでした"),
  ).toBeVisible();
  await expect(page.getByText("policy-c.txt")).toBeVisible();
  // HTTP の状態コードは「詳細」に畳む（一部失敗の warning では閉じる。messaging.md §10.3。#723）。
  const failedItem = page.locator("li").filter({ hasText: "HTTP 415" });
  await expect(failedItem.getByText("HTTP 415")).toBeHidden();
  await failedItem.getByText("詳細", { exact: true }).click();
  await expect(failedItem.getByText("HTTP 415")).toBeVisible();
  await expect(page.getByText("汎用解析")).toBeVisible();
  await expect(page.getByText("未対応")).toBeVisible();
  await expect(
    page.getByText("原本種別を判定できませんでした。"),
  ).toBeVisible();
  await expect(page.getByText("待機中")).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "policy-b.txt の文書詳細を開く" }),
  ).toBeVisible();
  expect(uploadBody).toContain('name="files"');
  expect(uploadBody).toContain("policy-a.txt");
  expect(uploadBody).toContain("policy-b.txt");
  expect(uploadBody).not.toContain("policy.exe");
  // 取込開始方針（ingestion_mode）は廃止した。アップロードは取込 job を作らない（#306）。
  expect(uploadBody).not.toContain('name="ingestion_mode"');

  // 失敗したファイルだけを選択の一覧に戻し、選び直さずに送り直せる（#701）。
  await page.getByRole("button", { name: "失敗した 1 件を選び直す" }).click();
  const selection = page.getByTestId("upload-selection");
  await expect(selection.getByRole("listitem")).toHaveCount(1);
  await expect(selection).toContainText("policy-c.txt");
  await expect(
    page.getByRole("button", { name: "アップロードを開始（1 件）" }),
  ).toBeEnabled();
});

async function mockUploadStorageSettings(
  page: Page,
  getCurrent: () => UploadStorageSettingsData,
  onPatch?: (payload: unknown) => Promise<void>,
) {
  await page.route("**/api/settings/upload-storage", async (route) => {
    const request = route.request();
    if (request.method() === "PATCH") {
      const payload = request.postDataJSON();
      await onPatch?.(payload);
    }

    await route.fulfill({
      json: {
        data: getCurrent(),
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

async function mockKnowledgeBases(page: Page) {
  await page.route("**/api/knowledge-bases**", async (route) => {
    await route.fulfill({
      json: {
        data: {
          items: [
            {
              id: "kb-1",
              name: "社内規程",
              description: "検索対象の規程",
              status: "ACTIVE",
              default_search_mode: "hybrid",
              document_count: 3,
              indexed_document_count: 3,
              error_document_count: 0,
              searchable_chunk_count: 12,
              created_at: "2026-06-15T00:00:00Z",
              updated_at: "2026-06-15T00:00:00Z",
              archived_at: null,
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

async function mockDocumentUpload(
  page: Page,
  onUpload: (body: string) => void,
) {
  await page.route("**/api/documents/upload", async (route) => {
    const body = route.request().postData() ?? "";
    onUpload(body);
    await route.fulfill({
      json: {
        data: {
          id: "doc-upload",
          file_name: "upload.txt",
          status: "UPLOADED",
          file_size_bytes: 6,
          content_sha256: "b".repeat(64),
          duplicate_of_document_id: null,
          knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
          source_profile: uploadSourceProfile(),
          ingestion_started: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await mockDocumentDetail(page, "doc-upload", "upload.txt");
}

async function mockBatchDocumentUpload(
  page: Page,
  onUpload: (body: string) => void,
) {
  await page.route("**/api/documents/batch-upload", async (route) => {
    const body = route.request().postData() ?? "";
    onUpload(body);
    await route.fulfill({
      json: {
        data: {
          items: [
            uploadResult("doc-batch-1", "policy-a.txt"),
            uploadResult("doc-batch-2", "policy-b.txt"),
          ],
          failed_items: [
            {
              file_name: "policy-c.txt",
              status_code: 415,
              message: "原本種別を判定できないため保存しませんでした。",
              source_profile: failedUploadSourceProfile(),
            },
          ],
          total_count: 3,
          uploaded_count: 2,
          failed_count: 1,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await mockDocumentDetail(page, "doc-batch-1", "policy-a.txt");
  await mockDocumentDetail(page, "doc-batch-2", "policy-b.txt");
}

async function mockDocumentDetail(
  page: Page,
  documentId: string,
  fileName: string,
  sourceProfile: UploadSourceProfileFixture = uploadSourceProfile(fileName),
) {
  await page.route(`**/api/documents/${documentId}`, async (route) => {
    await route.fulfill({
      json: {
        data: {
          id: documentId,
          file_name: fileName,
          status: "UPLOADED",
          category_name: null,
          content_type: sourceProfile.content_type,
          file_size_bytes: sourceProfile.file_size_bytes,
          content_sha256: sourceProfile.content_sha256,
          duplicate_of_document_id: null,
          uploaded_at: "2026-06-15T00:00:00Z",
          indexed_at: null,
          object_storage_path: "local://upload.txt",
          extraction: {
            raw_text: "本文",
            document_type: "テキスト",
            confidence: 0.98,
            warnings: [],
            elements: [],
          },
          error_message: null,
          knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
          source_profile: sourceProfile,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route(`**/api/documents/${documentId}/content`, async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/plain" },
      body: "本文",
    });
  });
  await page.route(
    `**/api/documents/${documentId}/knowledge-bases`,
    async (route) => {
      await route.fulfill({
        json: {
          data: [{ id: "kb-1", name: "社内規程" }],
          error_messages: [],
          warning_messages: [],
        },
      });
    },
  );
  await page.route(
    `**/api/documents/${documentId}/ingestion-segments`,
    async (route) => {
      await route.fulfill({
        json: {
          data: [
            {
              segment_id: `${documentId}:source`,
              document_id: documentId,
              status: "UPLOADED",
              parser_backend: "local_partition",
              parser_profile: "local_text_structure",
              page_start: null,
              page_end: null,
              attempt_count: 0,
              artifact_path: "local://upload.txt",
              error_code: null,
              error_message: null,
            },
          ],
          error_messages: [],
          warning_messages: [],
        },
      });
    },
  );
}

async function mockIngestionJobs(
  page: Page,
  jobs: ReturnType<typeof ingestionJob>[] = [],
  options: {
    onDrain?: () => void;
    onRetry?: (jobId: string) => void;
  } = {},
) {
  await page.route("**/api/documents/ingestion-jobs**", async (route) => {
    const request = route.request();
    const url = request.url();
    if (request.method() === "POST" && url.includes("/drain")) {
      options.onDrain?.();
      await route.fulfill({
        json: {
          data: jobs.filter((job) => job.status === "QUEUED"),
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    if (request.method() === "POST" && url.includes("/retry")) {
      const match = url.match(/\/ingestion-jobs\/([^/]+)\/retry/);
      options.onRetry?.(decodeURIComponent(match?.[1] ?? ""));
      await route.fulfill({
        json: {
          data: ingestionJob("job-retry", "doc-failed", "QUEUED"),
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fulfill({
      json: {
        data: {
          items: jobs,
          total: jobs.length,
          limit: 5,
          offset: 0,
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

function uploadResult(id: string, fileName: string) {
  return {
    id,
    file_name: fileName,
    status: "UPLOADED",
    file_size_bytes: 6,
    content_sha256: "b".repeat(64),
    duplicate_of_document_id: null,
    knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
    source_profile: uploadSourceProfile(fileName),
    ingestion_started: false,
  };
}

function ingestionJob(
  id: string,
  documentId: string,
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "SKIPPED" = "QUEUED",
) {
  return {
    id,
    document_id: documentId,
    status,
    parser_profile: "local_text_structure",
    quality_warnings: [] as string[],
    skip_reason: null as string | null,
    error_message: null as string | null,
    attempt_count: status === "QUEUED" ? 0 : 1,
    max_attempts: 3,
    queued_at: "2026-06-15T00:00:00Z",
    started_at:
      status === "QUEUED" || status === "SKIPPED"
        ? null
        : "2026-06-15T00:00:01Z",
    finished_at:
      status === "QUEUED" || status === "RUNNING"
        ? null
        : "2026-06-15T00:00:03Z",
  };
}

type UploadSourceProfileFixture = ReturnType<typeof uploadSourceProfile>;

function uploadSourceProfile(fileName = "upload.txt") {
  return {
    original_file_name: fileName,
    sanitized_file_name: fileName,
    extension: ".txt",
    content_type: "text/plain",
    inferred_content_type: "text/plain",
    file_size_bytes: 6,
    content_sha256: "b".repeat(64),
    modality: "text",
    parser_profile: "local_text_structure",
    parser_backend: "local_partition",
    parser_version: "v1",
    preview_kind: "text",
    text_charset: "utf-8",
    duplicate_of_document_id: null,
    unsupported_reason: null,
    quality_status: "ready",
    quality_warnings: [],
  };
}

function failedUploadSourceProfile() {
  return {
    original_file_name: "policy.exe",
    sanitized_file_name: "policy.exe",
    extension: ".exe",
    content_type: "application/x-msdownload",
    inferred_content_type: "application/x-msdownload",
    file_size_bytes: 2,
    content_sha256: "c".repeat(64),
    modality: "unknown",
    parser_profile: "enterprise_ai_generic",
    parser_backend: "enterprise_ai",
    parser_version: "v1",
    preview_kind: "unsupported",
    text_charset: null,
    duplicate_of_document_id: null,
    unsupported_reason: "unknown_file_type",
    quality_status: "warning",
    quality_warnings: ["unknown_modality"],
  };
}
