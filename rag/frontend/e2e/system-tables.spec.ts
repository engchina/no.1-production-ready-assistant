import { expect, test, type Page } from "./fixtures/test";
import { mockAuthUser, mockLocalAuth } from "./_helpers";

type SchemaStatus = "missing" | "partial" | "outdated" | "ready";

const databaseSettings = {
  user: "rag_app",
  dsn: "ragdb_high",
  wallet_dir: "/wallet",
  wallet_uploaded: true,
  available_services: ["ragdb_high"],
  has_password: true,
  has_wallet_password: false,
  readiness: "ok",
  embedding_dimension: 1536,
  vector_column: "VECTOR(1536, FLOAT32)",
  adb_ocid: "",
  region: "ap-osaka-1",
  config_source: "runtime",
};

function systemTables(status: SchemaStatus) {
  const complete = status === "ready" || status === "outdated";
  return {
    status,
    schema_version: "2",
    schema_head: "20260703_002_feedback_details",
    applied_versions: complete ? ["20260703_002_feedback_details"] : [],
    pending_versions: status === "outdated" ? ["20260703_002_feedback_details"] : [],
    expected_object_count: 97,
    existing_object_count: status === "missing" ? 0 : status === "partial" ? 54 : 97,
    expected_table_count: 28,
    existing_table_count: status === "missing" ? 0 : status === "partial" ? 18 : 28,
    missing_objects:
      status === "missing" || status === "partial"
        ? [{ name: "RAG_DOCUMENTS", object_type: "TABLE" }]
        : [],
    retired_objects: [],
    tables: [
      {
        name: "RAG_DOCUMENTS",
        exists: status !== "missing",
        estimated_rows: status === "ready" ? 12 : null,
        created_at: status === "missing" ? null : "2026-07-23T00:00:00+09:00",
        last_analyzed_at: null,
      },
      {
        name: "RAG_CHUNKS",
        exists: complete,
        estimated_rows: status === "ready" ? 240 : null,
        created_at: complete ? "2026-07-23T00:00:00+09:00" : null,
        last_analyzed_at: null,
      },
    ],
    operation_state: {
      status: "idle",
      operation_kind: null,
      lease_expires_at: null,
      last_error_code: null,
      schema_epoch: 3,
      updated_at: "2026-07-23T00:00:00+09:00",
    },
  };
}

const chunkSetsDocumentForeignKey = {
  name: "RAG_CHUNK_SETS_DOCUMENT_FK",
  table_name: "RAG_CHUNK_SETS",
  columns: ["DOCUMENT_ID"],
  referenced_table_name: "RAG_DOCUMENTS",
  referenced_columns: ["DOCUMENT_ID"],
  delete_rule: "CASCADE",
  orphan_rows: 0,
};

async function mockSettings(
  page: Page,
  options: {
    initialStatus?: SchemaStatus;
    initializeFails?: boolean;
    statusDelayMs?: number;
    /** 古い版の表に外部キーが無い（#505）。更新後は参照先のない行が残る警告を返す。 */
    foreignKeyDrift?: boolean;
    /** 状態の応答に足す項目（削除規則の違い・無効化・参照先のない行。#511）。 */
    statusExtra?: Record<string, unknown>;
    /** 参照先のない行の削除の応答（#511）。conflict は件数が増えたときの 409。 */
    orphanDeletion?: "ok" | "conflict";
    orphanDeletionDelayMs?: number;
  } = {}
) {
  let status = options.initialStatus ?? "missing";
  let initializeCalls = 0;
  let recreatePayload: Record<string, unknown> | null = null;
  const orphanDeletePayloads: Record<string, unknown>[] = [];
  let orphansDeleted = false;

  await mockLocalAuth(page);
  await page.route("**/api/settings/database**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/settings/database/system-tables/orphaned-rows/delete") {
      orphanDeletePayloads.push(JSON.parse(request.postData() ?? "{}"));
      if (options.orphanDeletionDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.orphanDeletionDelayMs));
      }
      if (options.orphanDeletion === "conflict") {
        await route.fulfill({
          status: 409,
          json: {
            data: null,
            error_messages: [
              "参照先のない行の件数が確認したときより増えています（確認時 240 件、現在 241 件）。状態を再取得し、件数を確認してから再実行してください。",
            ],
            warning_messages: [],
            error_code: "SCHEMA_ORPHAN_ROWS_CHANGED",
          },
        });
        return;
      }
      orphansDeleted = true;
      await route.fulfill({
        json: {
          data: {
            ...systemTables("ready"),
            orphaned_foreign_keys: [],
            operation: "orphans_deleted",
            deleted_row_count: 240,
            foreign_key: { ...chunkSetsDocumentForeignKey, orphan_rows: 0 },
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    if (url.pathname === "/api/settings/database/system-tables/initialize") {
      initializeCalls += 1;
      recreatePayload = JSON.parse(request.postData() ?? "{}");
      if (options.initializeFails) {
        await route.fulfill({
          status: 409,
          json: {
            data: null,
            error_messages: ["Oracle の対象オブジェクトがロックされています。"],
            warning_messages: [],
            error_code: "ORA-00054",
          },
        });
        return;
      }
      status = "ready";
      await route.fulfill({
        json: {
          data: {
            ...systemTables("ready"),
            orphaned_foreign_keys: options.foreignKeyDrift
              ? [{ ...chunkSetsDocumentForeignKey, orphan_rows: 240 }]
              : [],
            operation: recreatePayload?.recreate ? "recreated" : "initialized",
            dropped_object_count: recreatePayload?.recreate ? 96 : 0,
            created_object_count: 96,
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    if (url.pathname === "/api/settings/database/system-tables") {
      if (options.statusDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.statusDelayMs));
      }
      await route.fulfill({
        json: {
          data:
            options.foreignKeyDrift && status === "outdated"
              ? {
                  ...systemTables(status),
                  pending_versions: [],
                  missing_foreign_keys: [
                    { ...chunkSetsDocumentForeignKey, orphan_rows: 240 },
                    {
                      ...chunkSetsDocumentForeignKey,
                      name: "RAG_DOC_EXT_DOCUMENT_FK",
                      table_name: "RAG_DOCUMENT_EXTRACTIONS",
                      orphan_rows: 0,
                    },
                  ],
                }
              : options.statusExtra && !orphansDeleted
                ? { ...systemTables(status), ...options.statusExtra }
                : systemTables(status),
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    if (url.pathname === "/api/settings/database/adb") {
      await route.fulfill({
        json: {
          data: {
            status: "not_configured",
            message: "ADB OCID が未設定です。",
            id: null,
            display_name: null,
            lifecycle_state: null,
            db_name: null,
            cpu_core_count: null,
            data_storage_size_in_tbs: null,
            region: null,
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fulfill({
      json: {
        data: databaseSettings,
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  return {
    initializeCalls: () => initializeCalls,
    recreatePayload: () => recreatePayload,
    orphanDeletePayloads: () => orphanDeletePayloads,
  };
}

test("状態取得中は loading feedback を表示する", async ({ page }) => {
  await mockSettings(page, { statusDelayMs: 600 });
  await page.goto("/settings/system-tables");
  // 3 製品共通のカード（#325）: 経過時間付きの読み込み表示と Skeleton。
  await expect(
    page.getByRole("region", { name: "システムテーブルの状態を読み込んでいます" })
  ).toBeVisible();
  await expect(page.locator("#system-tables").getByText("未初期化").first()).toBeVisible();
});

const statusLabels: Record<SchemaStatus, string> = {
  missing: "未初期化",
  partial: "一部不足",
  outdated: "更新必要",
  ready: "初期化済み",
};

for (const status of Object.keys(statusLabels) as SchemaStatus[]) {
  test(`schema 状態 ${status} を表示する`, async ({ page }) => {
    await mockSettings(page, { initialStatus: status });
    await page.goto("/settings/system-tables");
    const card = page.locator("#system-tables");
    await expect(
      card.getByText(statusLabels[status], { exact: true }).first()
    ).toBeVisible();
  });
}

test("作成・更新で missing から ready になる", async ({ page }) => {
  const mock = await mockSettings(page, { initialStatus: "missing" });
  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  await card.getByRole("button", { name: "作成・更新" }).click();
  await expect(card.getByText("初期化済み", { exact: true })).toBeVisible();
  expect(mock.initializeCalls()).toBe(1);
});

test("システムテーブル管理の権限が無い利用者は状態だけを確認でき、作成・再作成を出さない", async ({
  page,
}) => {
  const mock = await mockSettings(page, { initialStatus: "missing" });
  // システムテーブルの画面権限だけを持つ DB ユーザー（rag.system_tables.manage なし。#214 / #658）。
  await mockAuthUser(page, { permissions: ["menu.settings_system_tables"] });
  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  await expect(card.getByText("未初期化", { exact: true }).first()).toBeVisible();
  await expect(
    card.getByText("システムテーブルの作成・更新と全再作成には「システムテーブル管理」の権限が必要です。", {
      exact: false,
    })
  ).toBeVisible();
  await expect(card.getByRole("button", { name: "作成・更新" })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "すべて再作成" })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "最新情報を取得" })).toBeVisible();
  expect(mock.initializeCalls()).toBe(0);
});

test("全再作成は確認語と ConfirmDialog の二段階で保護する", async ({
  page,
}) => {
  const mock = await mockSettings(page, { initialStatus: "ready" });
  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  const recreate = card.getByRole("button", { name: "すべて再作成" });
  await expect(recreate).toBeDisabled();

  await card.getByRole("textbox", { name: "実行確認語" }).fill("RECREATE_RAG_SYSTEM_TABLES");
  await expect(recreate).toBeEnabled();
  await recreate.click();

  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("RAG の DB データを削除しますか？");
  await dialog.getByRole("button", { name: "すべて再作成" }).click();

  await expect(dialog).toHaveCount(0);
  await expect(card.getByText("初期化済み", { exact: true })).toBeVisible();
  expect(mock.recreatePayload()).toEqual({
    recreate: true,
    confirmation: "RECREATE_RAG_SYSTEM_TABLES",
  });
});

test("操作失敗後にエラーへフォーカスし、375px でページ横溢れしない", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await mockSettings(page, {
    initialStatus: "partial",
    initializeFails: true,
  });
  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  await card.getByRole("button", { name: "作成・更新" }).click();

  const error = page.getByTestId("system-tables-operation-error");
  await expect(error).toBeFocused();
  await expect(error).toContainText("ロック");

  const pageOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(pageOverflow).toBeLessThanOrEqual(1);

  await card.getByText("管理オブジェクトの詳細").click();
  await card.getByTestId("system-tables-scroll-region").focus();
  await expect(card.getByTestId("system-tables-scroll-region")).toBeFocused();
});

test("不足している外部キーを更新必要として並べ、更新後は参照先のない行を警告する（#505）", async ({
  page,
}) => {
  const mock = await mockSettings(page, { initialStatus: "outdated", foreignKeyDrift: true });
  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  await expect(card.getByText("更新必要", { exact: true }).first()).toBeVisible();
  const missing = card.getByTestId("system-tables-missing-foreign-keys");
  await expect(missing).toContainText("既存のテーブルに外部キーが 2 件ありません");
  await expect(missing).toContainText("RAG_CHUNK_SETS (DOCUMENT_ID) → RAG_DOCUMENTS");
  await expect(missing).toContainText("参照先のない行 240 件");
  await expect(missing).toContainText("RAG_DOCUMENT_EXTRACTIONS (DOCUMENT_ID) → RAG_DOCUMENTS");

  await card.getByRole("button", { name: "作成・更新" }).click();
  await expect(card.getByText("初期化済み", { exact: true })).toBeVisible();
  expect(mock.initializeCalls()).toBe(1);
  await expect(missing).toHaveCount(0);
  const orphaned = card.getByTestId("system-tables-orphaned-foreign-keys");
  await expect(orphaned).toContainText("既存の行は自動では削除しません");
  await expect(orphaned).toContainText("参照先のない行 240 件");

  const pageOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(pageOverflow).toBeLessThanOrEqual(1);
});

// ---- 削除規則の違い・無効化・参照先のない行の削除（#511） ----------------------------

const mismatchedForeignKey = {
  ...chunkSetsDocumentForeignKey,
  name: "RAG_DOC_RECIPES_DOCUMENT_FK",
  table_name: "RAG_DOCUMENT_RECIPES",
  orphan_rows: 0,
  current_name: "RAG_DOC_RECIPES_DOC_FK_OLD",
  current_delete_rule: "NO ACTION",
};
const disabledForeignKey = {
  ...chunkSetsDocumentForeignKey,
  name: "RAG_ARTIFACT_LAYERS_DOCUMENT_FK",
  table_name: "RAG_ARTIFACT_LAYERS",
  orphan_rows: 12,
};
const orphanedForeignKey = { ...chunkSetsDocumentForeignKey, orphan_rows: 240 };

/** アプリの外観の設定（localStorage）でテーマを切り替える（`emulateMedia` では切り替わらない）。 */
async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem(
      "production-ready-rag.ui",
      JSON.stringify({ state: { theme: value }, version: 0 })
    );
  }, theme);
}

/**
 * 領域の中の文字（段落・一覧・ボタン）の、背景に対するコントラスト比の最小値。
 * 色は canvas に塗って sRGB に直し、半透明の文字・背景は祖先の背景に重ねて求める（WCAG 2.x）。
 */
async function minimumTextContrast(page: Page, selector: string) {
  return page.evaluate((rootSelector) => {
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) throw new Error("canvas を使えません");
    type Rgba = [number, number, number, number];
    const toRgba = (color: string): Rgba => {
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = color;
      context.fillRect(0, 0, 1, 1);
      const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data;
      return [r, g, b, a / 255];
    };
    const over = (top: Rgba, bottom: Rgba): Rgba => [
      top[0] * top[3] + bottom[0] * (1 - top[3]),
      top[1] * top[3] + bottom[1] * (1 - top[3]),
      top[2] * top[3] + bottom[2] * (1 - top[3]),
      1,
    ];
    const background = (element: Element): Rgba => {
      const layers: Rgba[] = [];
      for (let node: Element | null = element; node; node = node.parentElement) {
        const layer = toRgba(getComputedStyle(node).backgroundColor);
        if (layer[3] > 0) layers.push(layer);
        if (layer[3] >= 1) break;
      }
      return layers.reduceRight<Rgba>((under, layer) => over(layer, under), [255, 255, 255, 1]);
    };
    const luminance = ([r, g, b]: Rgba) =>
      [r, g, b]
        .map((value) => value / 255)
        .map((value) => (value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4))
        .reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
    const results: { text: string; ratio: number }[] = [];
    for (const root of document.querySelectorAll(rootSelector)) {
      for (const element of root.querySelectorAll("p, span, li, button")) {
        const text = Array.from(element.childNodes)
          .filter((node) => node.nodeType === Node.TEXT_NODE)
          .map((node) => node.textContent ?? "")
          .join("")
          .trim();
        if (!text) continue;
        const back = background(element);
        const fore = over(toRgba(getComputedStyle(element).color), back);
        const [lighter, darker] = [luminance(fore), luminance(back)].sort((a, b) => b - a);
        results.push({ text, ratio: (lighter + 0.05) / (darker + 0.05) });
      }
    }
    results.sort((a, b) => a.ratio - b.ratio);
    return { count: results.length, minimum: results[0] };
  }, selector);
}

const screenshotDir = process.env.SYSTEM_TABLES_SCREENSHOT_DIR;

for (const theme of ["light", "dark"] as const) {
  test(`外部キーの差分と参照先のない行の警告を並べ、文字のコントラストを保つ（#511・${theme}）`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    await useTheme(page, theme);
    await mockSettings(page, {
      initialStatus: "outdated",
      statusExtra: {
        pending_versions: [],
        missing_foreign_keys: [{ ...chunkSetsDocumentForeignKey, orphan_rows: 3 }],
        mismatched_foreign_keys: [mismatchedForeignKey],
        disabled_foreign_keys: [disabledForeignKey],
        orphaned_foreign_keys: [
          {
            ...orphanedForeignKey,
            name: "RAG_DOC_EXT_DOCUMENT_FK",
            table_name: "RAG_DOCUMENT_EXTRACTIONS",
          },
        ],
      },
    });
    await page.goto("/settings/system-tables");
    await expect
      .poll(() => page.evaluate(() => document.documentElement.classList.contains("dark")))
      .toBe(theme === "dark");
    const card = page.locator("#system-tables");

    const mismatched = card.getByTestId("system-tables-mismatched-foreign-keys");
    await expect(mismatched).toContainText("削除規則が正本と異なる外部キーが 1 件あります");
    await expect(mismatched).toContainText("RAG_DOCUMENT_RECIPES (DOCUMENT_ID) → RAG_DOCUMENTS");
    await expect(mismatched).toContainText("削除規則 NO ACTION → CASCADE");
    const disabled = card.getByTestId("system-tables-disabled-foreign-keys");
    await expect(disabled).toContainText("無効になっている外部キーが 1 件あります");
    await expect(disabled).toContainText("参照先のない行 12 件");
    await expect(card.getByTestId("system-tables-missing-foreign-keys")).toContainText(
      "参照先のない行 3 件"
    );
    await expect(
      card.getByRole("button", { name: "参照先のない行を削除 RAG_DOC_EXT_DOCUMENT_FK" })
    ).toBeVisible();

    const contrast = await minimumTextContrast(page, "#system-tables [role='status']");
    expect(contrast.count).toBeGreaterThan(5);
    expect(contrast.minimum.ratio, contrast.minimum.text).toBeGreaterThanOrEqual(4.5);

    const pageOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(pageOverflow).toBeLessThanOrEqual(1);

    // 目視用の画像（環境変数を指定したときだけ。CI では撮らない）。
    if (screenshotDir) {
      const widths = testInfo.project.name === "mobile" ? [375] : [1280, 1920];
      const banners = card.locator("[role='status']");
      for (const width of widths) {
        await page.setViewportSize({ width, height: 900 });
        for (const [index, name] of ["outdated", "orphaned"].entries()) {
          await banners.nth(index).screenshot({
            path: `${screenshotDir}/system-tables-fk-${name}-${theme}-${width}.png`,
            animations: "disabled",
          });
        }
      }
      await card.getByRole("button", { name: "参照先のない行を削除 RAG_DOC_EXT_DOCUMENT_FK" }).click();
      const dialog = page.getByRole("alertdialog");
      await expect(dialog.getByRole("button", { name: "キャンセル" })).toBeVisible();
      await dialog.screenshot({
        path: `${screenshotDir}/system-tables-fk-dialog-${theme}-${widths[widths.length - 1]}.png`,
        animations: "disabled",
      });
    }
  });
}

test("参照先のない行の削除は確認ダイアログで表・外部キー・件数を示し、承認したときだけ確認した件数で削除する（#511）", async ({
  page,
}) => {
  const mock = await mockSettings(page, {
    initialStatus: "ready",
    statusExtra: { orphaned_foreign_keys: [orphanedForeignKey] },
    orphanDeletionDelayMs: 300,
  });
  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  const orphaned = card.getByTestId("system-tables-orphaned-foreign-keys");
  await expect(orphaned).toContainText("「参照先のない行を削除」で削除できます");
  const trigger = orphaned.getByRole("button", {
    name: "参照先のない行を削除 RAG_CHUNK_SETS_DOCUMENT_FK",
  });

  // 取り消したときは削除しない。
  await trigger.click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("参照先のない行を 240 件削除しますか？");
  await expect(dialog).toContainText("表 RAG_CHUNK_SETS");
  await expect(dialog).toContainText("外部キー RAG_CHUNK_SETS_DOCUMENT_FK（DOCUMENT_ID → RAG_DOCUMENTS）");
  await expect(dialog).toContainText("削除した行は復元できません");
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(dialog).toHaveCount(0);
  expect(mock.orphanDeletePayloads()).toEqual([]);

  await trigger.click();
  await dialog.getByRole("button", { name: "参照先のない行を削除" }).click();
  await expect(dialog).toHaveCount(0);
  // 実行中は他の操作を止める（ラベルは変えずに loading）。
  await expect(card.getByRole("button", { name: "作成・更新" })).toBeDisabled();
  await expect(
    page.getByText(
      "RAG_CHUNK_SETS の参照先のない行を 240 件削除し、外部キー RAG_CHUNK_SETS_DOCUMENT_FK を検査済みにしました。"
    )
  ).toBeVisible();
  await expect(orphaned).toHaveCount(0);
  expect(mock.orphanDeletePayloads()).toEqual([
    { constraint_name: "RAG_CHUNK_SETS_DOCUMENT_FK", expected_orphan_rows: 240 },
  ]);
});

test("参照先のない行が確認時より増えていたら削除せず、理由へフォーカスする（#511）", async ({
  page,
}) => {
  const mock = await mockSettings(page, {
    initialStatus: "ready",
    statusExtra: { orphaned_foreign_keys: [orphanedForeignKey] },
    orphanDeletion: "conflict",
  });
  await page.goto("/settings/system-tables");
  const card = page.locator("#system-tables");
  await card
    .getByRole("button", { name: "参照先のない行を削除 RAG_CHUNK_SETS_DOCUMENT_FK" })
    .click();
  await page.getByRole("alertdialog").getByRole("button", { name: "参照先のない行を削除" }).click();

  const error = page.getByTestId("system-tables-operation-error");
  await expect(error).toBeFocused();
  await expect(error).toContainText("確認時 240 件、現在 241 件");
  expect(mock.orphanDeletePayloads()).toHaveLength(1);
});

test("システムテーブル管理の権限が無い利用者には参照先のない行の削除を出さない（#511）", async ({
  page,
}) => {
  await mockSettings(page, {
    initialStatus: "ready",
    statusExtra: { orphaned_foreign_keys: [orphanedForeignKey] },
  });
  await mockAuthUser(page, { permissions: ["menu.settings_system_tables"] });
  await page.goto("/settings/system-tables");
  const orphaned = page.locator("#system-tables").getByTestId("system-tables-orphaned-foreign-keys");
  await expect(orphaned).toContainText("参照先のない行 240 件");
  await expect(orphaned.getByRole("button")).toHaveCount(0);
  await expect(orphaned).not.toContainText("「参照先のない行を削除」で削除できます");
});

const destructiveMigration = {
  name: "20260930_005_retire_standard_engine_objects",
  description:
    "旧い標準の回答フローのテーブル rag_agent_memories・rag_prompt_versions・rag_generation_settings を削除し（PURGE のため復元できません）、ロールに付いた廃止済みのメニュー権限の行を削除します。残す行は先に app.rag.legacy_export で書き出してください。",
};

for (const theme of ["light", "dark"] as const) {
  test(`データを削除する未適用の migration は警告し、確認ダイアログで承認したときだけ適用する（#619・${theme}）`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    await useTheme(page, theme);
    const mock = await mockSettings(page, {
      initialStatus: "outdated",
      statusExtra: {
        pending_versions: [destructiveMigration.name],
        pending_destructive_migrations: [destructiveMigration],
      },
    });
    await page.goto("/settings/system-tables");
    await expect
      .poll(() => page.evaluate(() => document.documentElement.classList.contains("dark")))
      .toBe(theme === "dark");
    const card = page.locator("#system-tables");

    const warning = card.getByTestId("system-tables-destructive-migrations");
    await expect(warning).toContainText("削除したデータは元に戻せません");
    await expect(warning).toContainText(destructiveMigration.name);
    await expect(warning).toContainText("rag_agent_memories");
    await expect(warning).toContainText("「作成・更新」を押すと、削除の確認を求めます。");
    await expect(card).not.toContainText("無損失で更新できます");

    const contrast = await minimumTextContrast(page, "#system-tables [role='status']");
    expect(contrast.minimum.ratio, contrast.minimum.text).toBeGreaterThanOrEqual(4.5);
    const pageOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(pageOverflow).toBeLessThanOrEqual(1);

    // 目視用の画像（環境変数を指定したときだけ。CI では撮らない）。
    const width = testInfo.project.name === "mobile" ? 375 : 1280;
    if (screenshotDir) {
      await warning.screenshot({
        path: `${screenshotDir}/system-tables-destructive-${theme}-${width}.png`,
        animations: "disabled",
      });
    }

    // 取り消したときは送らない。
    await card.getByRole("button", { name: "作成・更新" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("データを削除する更新を実行しますか？");
    await expect(dialog).toContainText(destructiveMigration.name);
    await expect(dialog).toContainText("元に戻せません");
    if (screenshotDir) {
      await dialog.screenshot({
        path: `${screenshotDir}/system-tables-destructive-dialog-${theme}-${width}.png`,
        animations: "disabled",
      });
    }
    await dialog.getByRole("button", { name: "キャンセル" }).click();
    await expect(dialog).toHaveCount(0);
    expect(mock.initializeCalls()).toBe(0);

    await card.getByRole("button", { name: "作成・更新" }).click();
    await dialog.getByRole("button", { name: "削除して更新" }).click();
    await expect(dialog).toHaveCount(0);
    await expect.poll(() => mock.initializeCalls()).toBe(1);
    expect(mock.recreatePayload()).toEqual({ recreate: false, allow_destructive: true });
  });
}

// ---- 台帳が最新でも欠けた列（#2450） --------------------------------------------------

for (const theme of ["light", "dark"] as const) {
  test(`台帳が最新でも既存の表に無い列を更新必要として並べる（#2450・${theme}）`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    await useTheme(page, theme);
    const mock = await mockSettings(page, {
      initialStatus: "outdated",
      statusExtra: {
        pending_versions: [],
        missing_columns: [
          { table_name: "RAG_DOCUMENTS", column_name: "CLASSIFICATION" },
        ],
      },
    });
    await page.goto("/settings/system-tables");
    await expect
      .poll(() => page.evaluate(() => document.documentElement.classList.contains("dark")))
      .toBe(theme === "dark");
    const card = page.locator("#system-tables");
    await expect(card.getByText("更新必要", { exact: true }).first()).toBeVisible();
    const missing = card.getByTestId("system-tables-missing-columns");
    await expect(missing).toContainText("既存のテーブルに必須の列が 1 件ありません");
    await expect(missing).toContainText("RAG_DOCUMENTS.CLASSIFICATION");

    const contrast = await minimumTextContrast(page, "#system-tables [role='status']");
    expect(contrast.count).toBeGreaterThan(2);
    expect(contrast.minimum.ratio, contrast.minimum.text).toBeGreaterThanOrEqual(4.5);
    const pageOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(pageOverflow).toBeLessThanOrEqual(1);

    if (screenshotDir) {
      const width = testInfo.project.name === "mobile" ? 375 : 1280;
      await page.setViewportSize({ width, height: 900 });
      await card.locator("[role='status']").first().screenshot({
        path: `${screenshotDir}/system-tables-columns-${theme}-${width}.png`,
        animations: "disabled",
      });
    }

    await card.getByRole("button", { name: "作成・更新" }).click();
    await expect(card.getByText("初期化済み", { exact: true })).toBeVisible();
    expect(mock.initializeCalls()).toBe(1);
    await expect(missing).toHaveCount(0);
  });
}
