import type { Locator, Page } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";
import { chooseSelectFieldOption } from "./fixtures/select-field";
import { expectToastPlacement, overlaps } from "./fixtures/toast-placement";

/**
 * ページ全体（documentElement）と本文（`<main>`）が横にはみ出さないこと。
 * main は overflow-y-auto で横のはみ出しも吸収するため、documentElement だけでは見逃す
 * （表の行の中の読み上げ専用ラベルが表の外へはみ出した #1116）。
 */
async function expectNoHorizontalOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(() => {
        const root = document.documentElement;
        const main = document.querySelector("main");
        return Math.max(root.scrollWidth - root.clientWidth, main ? main.scrollWidth - main.clientWidth : 0);
      })
    )
    .toBeLessThanOrEqual(1);
}

/**
 * 行の操作メニュー（RowActionMenu）を開いて項目を選ぶ。
 * 名前は完全一致で探す（#831）。部分一致だと、一覧へ戻る画面の切替（transition）が終わる前は、まだ出ている
 * エディタの「その他の操作（crm の操作）」に当たり、開いたメニューが一覧への切替で消える。
 */
async function chooseRowAction(page: Page, name: string, item: string) {
  const trigger = page.getByRole("button", { name: `${name} の操作`, exact: true });
  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("menuitem", { name: item }).click();
}

async function expectDocumentScrollLocked(page: Page) {
  await page.evaluate(() => window.scrollTo(0, 1000));
  const metrics = await page.evaluate(() => ({
    windowScrollY: window.scrollY,
    documentClientHeight: document.documentElement.clientHeight,
    documentScrollHeight: document.documentElement.scrollHeight,
    rootClientHeight: document.getElementById("root")?.clientHeight ?? 0,
    rootScrollHeight: document.getElementById("root")?.scrollHeight ?? 0,
  }));

  expect(metrics.windowScrollY).toBe(0);
  expect(metrics.documentScrollHeight).toBeLessThanOrEqual(metrics.documentClientHeight + 1);
  expect(metrics.rootScrollHeight).toBeLessThanOrEqual(metrics.rootClientHeight + 1);
}

async function expectElementAbove(page: Page, upperSelector: string, lowerSelector: string) {
  const upperBox = await page.locator(upperSelector).boundingBox();
  const lowerBox = await page.locator(lowerSelector).boundingBox();

  expect(upperBox).not.toBeNull();
  expect(lowerBox).not.toBeNull();
  expect(upperBox!.y).toBeLessThan(lowerBox!.y);
}

/** テキストの欄が Vision の欄より先（同じ行なら左、縦積みなら上）にある（#566）。 */
async function expectTextBeforeVision(text: Locator, vision: Locator) {
  const textBox = await text.boundingBox();
  const visionBox = await vision.boundingBox();
  expect(textBox).not.toBeNull();
  expect(visionBox).not.toBeNull();
  const sameRow = Math.abs(textBox!.y - visionBox!.y) < 1;
  expect(sameRow ? textBox!.x < visionBox!.x : textBox!.y < visionBox!.y).toBe(true);
}

async function fillOrSelectDsn(page: Page, value: string) {
  const dsnControl = page.getByLabel("サービス名 / DSN");
  const tagName = await dsnControl.evaluate((element) => element.tagName.toLowerCase());
  if (tagName === "button") {
    await dsnControl.click();
    await page.getByRole("option", { name: value }).click();
    return;
  }
  await dsnControl.fill(value);
}

async function mockMissingOciRuntimeSettings(page: Page) {
  await page.route("**/api/settings/oci/object-storage/namespace", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          namespace: "mytenancynamespace",
        },
      }),
    });
  });
  await page.route("**/api/settings/oci/object-storage", async (route) => {
    if (route.request().method() !== "PATCH") {
      // 保存などは e2e/fixtures/mock-api.ts が扱う（実 backend へは流さない）。
      await route.fallback();
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          backend: "local",
          local_storage_dir: "/u01/data/production-ready-agent",
          object_storage_region: "",
          object_storage_namespace: "mytenancynamespace",
          object_storage_bucket: "",
          readiness: "ok",
          max_upload_bytes: 104857600,
          config_source: "runtime",
        },
      }),
    });
  });
  await page.route("**/api/settings/oci", async (route) => {
    if (route.request().method() !== "GET") {
      // 保存などは e2e/fixtures/mock-api.ts が扱う（実 backend へは流さない）。
      await route.fallback();
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          config_file: "~/.oci/config",
          profile: "DEFAULT",
          user: "",
          fingerprint: "",
          tenancy: "",
          region: "",
          key_file: "~/.oci/oci_api_key.pem",
          key_file_exists: false,
          config_file_exists: false,
          config_source: "runtime",
        },
      }),
    });
  });
  await page.route("**/api/settings/upload-storage", async (route) => {
    if (route.request().method() !== "GET") {
      // 保存などは e2e/fixtures/mock-api.ts が扱う（実 backend へは流さない）。
      await route.fallback();
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          backend: "local",
          local_storage_dir: "/u01/data/production-ready-agent",
          object_storage_region: "",
          object_storage_namespace: "",
          object_storage_bucket: "",
          readiness: "ok",
          max_upload_bytes: 104857600,
          config_source: "runtime",
        },
      }),
    });
  });
  await page.route("**/api/settings/database", async (route) => {
    const method = route.request().method();
    if (!["GET", "PATCH"].includes(method)) {
      // 保存などは e2e/fixtures/mock-api.ts が扱う（実 backend へは流さない）。
      await route.fallback();
      return;
    }
    const body =
      method === "PATCH"
        ? ((route.request().postDataJSON() ?? {}) as { user?: string; dsn?: string })
        : {};
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          user: body.user ?? "",
          dsn: body.dsn ?? "ragdb_high",
          wallet_dir: "/u01/aipoc/instantclient_23_26/network/admin",
          wallet_uploaded: true,
          available_services: ["ragdb_high"],
          has_password: method === "PATCH",
          has_wallet_password: false,
          readiness: "ok",
          embedding_dimension: 1536,
          vector_column: "VECTOR(1536, FLOAT32)",
          adb_ocid: "ocid1.autonomousdatabase.oc1.ap-osaka-1.agent",
          region: "ap-osaka-1",
          config_source: "runtime",
        },
      }),
    });
  });
}

test.describe("Agent Runtime settings", () => {
  test("RAG 由来のシステム設定 4 画面を表示・保存できる", { tag: "@responsive" }, async ({ page, mockApi }) => {
    await mockMissingOciRuntimeSettings(page);
    await page.goto("/settings/oci");
    await expect(page.getByRole("heading", { name: "OCI 認証設定", level: 1 })).toBeVisible();
    await expectDocumentScrollLocked(page);
    await expectElementAbove(page, "#oci-config-file", "#oci-user-ocid");
    await expectElementAbove(page, "#oci-config-profile", "#oci-tenancy-ocid");
    await expect(page.getByLabel("ユーザー OCID")).toHaveValue("");
    await expect(page.getByLabel("テナンシ OCID")).toHaveValue("");
    await expect(page.getByLabel("フィンガープリント")).toHaveValue("");
    await expect(page.getByRole("combobox", { name: "リージョン", exact: true })).toContainText("選択してください");
    await expect(page.getByText("PLATFORM_OCI_REGION=ap-osaka-1")).toHaveCount(0);
    await expect(page.getByText(/=None/)).toHaveCount(0);
    await page.locator("main").evaluate((main) => {
      main.scrollTop = main.scrollHeight;
    });
    await expectDocumentScrollLocked(page);
    await page.getByLabel("ユーザー OCID").fill("ocid1.user.oc1..aaaaaaaa");
    await page.getByLabel("テナンシ OCID").fill("ocid1.tenancy.oc1..aaaaaaaa");
    await page.getByLabel("フィンガープリント").fill("12:34:56:78:90:ab:cd:ef");
    await page.getByRole("combobox", { name: "リージョン", exact: true }).click();
    await page.getByRole("option", { name: "ap-osaka-1" }).click();
    await page.getByRole("button", { name: /OCI 設定を保存/ }).click();
    await expect(page.getByText("保存しました").first()).toBeVisible();
    expect(mockApi.lastRequest("PATCH", "/api/settings/oci")?.body).toMatchObject({
      user: "ocid1.user.oc1..aaaaaaaa",
      tenancy: "ocid1.tenancy.oc1..aaaaaaaa",
      fingerprint: "12:34:56:78:90:ab:cd:ef",
      region: "ap-osaka-1",
    });
    await page.getByRole("combobox", { name: "Object Storage リージョン" }).click();
    await page.getByRole("option", { name: "ap-osaka-1" }).click();
    await page.getByRole("button", { name: /Object Storage ネームスペース: 取得/ }).click();
    await expect(
      page.getByRole("textbox", { name: /Object Storage ネームスペース/ })
    ).toHaveValue(
      "mytenancynamespace"
    );
    await expect(page.getByText("namespace の取得に失敗しました。")).toHaveCount(0);
    await page.getByRole("button", { name: /Object Storage: 保存/ }).click();
    await expect(page.getByText("Object Storage 設定を保存しました。").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.goto("/settings/upload-storage");
    await expect(page.getByRole("heading", { name: "アップロード保存先", level: 1 })).toBeVisible();
    await page.getByLabel("ローカル保存ディレクトリ").fill("/u01/data/production-ready-agent");
    await page.getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("保存しました")).toBeVisible();
    expect(mockApi.lastRequest("PATCH", "/api/settings/upload-storage")?.body).toMatchObject({
      backend: "local",
      local_storage_dir: "/u01/data/production-ready-agent",
    });
    await expectNoHorizontalOverflow(page);

    await page.goto("/settings/model");
    await expect(page.getByRole("heading", { name: "モデル設定", level: 1 })).toBeVisible();
    // OCI Enterprise AI の接続はプライマリ接続・セカンダリ接続・ターシャリ接続のタブ（この順）。
    // 登録モデルごとに接続を選ぶ（#533 / #542 / #786）。
    const connectionTabs = page.getByRole("tablist", { name: "OCI Enterprise AI の接続" });
    await expect(connectionTabs.getByRole("tab")).toHaveText([
      "プライマリ接続",
      "セカンダリ接続",
      "ターシャリ接続",
    ]);
    await expect(connectionTabs.getByRole("tab", { name: "プライマリ接続" })).toHaveAttribute(
      "aria-selected",
      "true"
    );
    await connectionTabs.getByRole("tab", { name: "セカンダリ接続" }).click();
    await expect(page.getByRole("button", { name: "セカンダリ接続を設定" })).toBeVisible();
    // ターシャリ接続（OpenAI / OpenAI 互換 API 向け）は Project OCID が任意。
    await connectionTabs.getByRole("tab", { name: "ターシャリ接続" }).click();
    await page.getByRole("button", { name: "ターシャリ接続を設定" }).click();
    await expect(page.locator("#enterprise-tertiary-endpoint")).toHaveAttribute(
      "placeholder",
      "https://api.openai.com/v1"
    );
    await expect(page.locator("#enterprise-tertiary-endpoint")).toHaveAttribute(
      "aria-required",
      "true"
    );
    await expect(page.locator("#enterprise-tertiary-project-ocid")).not.toHaveAttribute(
      "aria-required",
      "true"
    );
    await page.getByRole("button", { name: "ターシャリ接続を削除" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "削除" }).click();
    await expect(page.getByRole("button", { name: "ターシャリ接続を設定" })).toBeFocused();
    await connectionTabs.getByRole("tab", { name: "プライマリ接続" }).click();
    await expect(page.getByRole("combobox", { name: "モデル 1 の接続" })).toContainText(
      "プライマリ接続"
    );
    await page.getByRole("textbox", { name: "API key" }).fill("test-api-key");
    await page.getByRole("button", { name: "OCI Enterprise AI: 保存" }).click();
    await expect(page.getByText("OCI Enterprise AI 接続設定を保存しました。").first()).toBeVisible();
    expect(mockApi.lastRequest("PATCH", "/api/settings/model")?.body).toMatchObject({
      // 接続情報の節の保存は、既定のモデル 2 つを保存済みの値のまま送る（#499）。
      enterprise_ai: {
        connections: [{ connection_id: "primary", api_key: "test-api-key" }],
        default_text_model_id: "enterprise-llm",
        default_vision_model_id: "enterprise-llm",
      },
    });
    const textDefault = page.getByRole("combobox", { name: "既定のテキストモデル" });
    const visionDefault = page.getByRole("combobox", { name: "既定の画像対応モデル" });
    await expect(visionDefault).toContainText("業務 RAG 標準");
    await expect(textDefault).toContainText("業務 RAG 標準");
    // 並びはテキスト → Vision で、2 つとも必須（#566）。
    await expectTextBeforeVision(textDefault, visionDefault);
    await expect(textDefault).toHaveAttribute("aria-required", "true");
    await expect(visionDefault).toHaveAttribute("aria-required", "true");
    await expectNoHorizontalOverflow(page);

    await page.goto("/settings/database");
    await expect(page.getByRole("heading", { name: "データベース設定", level: 1 })).toBeVisible();
    await page.getByLabel("データベースユーザー").fill("rag_app");
    await fillOrSelectDsn(page, "ragdb_high");
    await page.getByLabel("データベースパスワード").fill("secret-password");
    await page.getByRole("button", { name: /DB設定を保存/ }).click();
    await expect(page.getByText("保存しました")).toBeVisible();
    // DB の起動・停止は docker 側の責務（#10）。ADB の運用パネルは出さない。
    await expect(page.getByText("Autonomous Database 管理")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "起動", exact: true })).toHaveCount(0);
    await expectNoHorizontalOverflow(page);

    await page.setViewportSize({ width: 375, height: 812 });
    for (const route of [
      "/settings/oci",
      "/settings/upload-storage",
      "/settings/model",
      "/settings/database",
    ]) {
      await page.goto(route);
      await expectNoHorizontalOverflow(page);
      if (route === "/settings/model") {
        // 375px の縦積みでもテキスト → Vision の順（#566）。
        await expectTextBeforeVision(
          page.getByRole("combobox", { name: "既定のテキストモデル" }),
          page.getByRole("combobox", { name: "既定の画像対応モデル" })
        );
      }
    }
  });

  for (const viewport of [
    { name: "desktop", width: 1280, height: 900 },
    { name: "mobile-375", width: 375, height: 812 },
  ]) {
    test(`MCP 接続のツールのポリシーを選んで保存し、取得できない接続を知らせる (${viewport.name})`, async ({
      page,
      mockApi,
    }, testInfo) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      // NL2SQL は URL を設定済み、RAG は設定済みだが tools/list を取れない（#1814）。
      for (const connection of mockApi.state.mcpConnections.connections) {
        Object.assign(connection, { configured: true, base_url: `http://${connection.server_id}.test/mcp` });
      }
      mockApi.state.mcpConnectionTools.nl2sql = [
        { name: "nl2sql_query", description: "質問から SQL を作って実行する", read_only: false },
        { name: "nl2sql_get_job", description: "ジョブの結果を返す", read_only: true },
        { name: "nl2sql_cancel_requirement", description: "確認を取り消す", read_only: false },
      ];
      mockApi.state.mcpConnectionErrors.rag = "MCP 接続「ナレッジ検索（RAG）」に接続できません。";
      // 取得できない接続のツールで保存したポリシーは、一覧に無い行として残す。
      Object.assign(mockApi.state.toolPolicy as Record<string, unknown>, { deny: ["rag__rag_search"] });
      await page.goto("/settings/tool-policy");

      const errors = page.getByTestId("tool-policy-connection-errors");
      await expect(errors).toContainText("一部の MCP 接続のツールを取得できませんでした");
      await expect(errors).toContainText("ナレッジ検索（RAG）: MCP 接続「ナレッジ検索（RAG）」に接続できません。");
      await expect(page.getByTestId("tool-policy-open-mcp-connections")).toHaveAttribute("href", /\/settings\/mcp-connections$/);

      const table = page.getByRole("table", { name: "ツール権限" });
      const stale = table.getByRole("row", { name: /rag__rag_search/ });
      await expect(stale).toContainText("一覧にありません");
      await expect(stale).toContainText("ナレッジ検索（RAG）");

      await page.getByRole("searchbox", { name: "ツール名・説明・接続で絞り込み" }).fill("NL2SQL");
      await expect(page.getByTestId("tool-policy-toolbar")).toContainText("3 件");
      const query = table.getByRole("row", { name: /nl2sql__nl2sql_query/ });
      await expect(query).toContainText("データ問い合わせ（NL2SQL）");
      await expect(query).toContainText("書き込み");
      // 「既定に従う」には、選んだときの判定とその理由を添える（#1832）。nl2sql_query は標準の接続の
      // 既定で自動実行、readOnlyHint の無いほかのツールは承認を要求。
      const policy = page.getByRole("combobox", { name: "nl2sql__nl2sql_query のポリシー", exact: true });
      await expect(policy).toContainText("既定に従う（自動実行）");
      await expect(page.getByTestId("tool-policy-reason-nl2sql__nl2sql_query")).toHaveText(
        "標準の接続で Agent が承認なしと決めたツール"
      );
      await expect(
        page.getByRole("combobox", { name: "nl2sql__nl2sql_cancel_requirement のポリシー", exact: true })
      ).toContainText("既定に従う（承認を要求）");
      await expect(page.getByTestId("tool-policy-reason-nl2sql__nl2sql_cancel_requirement")).toHaveText(
        "未指定ツールの既定動作"
      );
      await expect(
        page.getByRole("combobox", { name: "nl2sql__nl2sql_get_job のポリシー", exact: true })
      ).toContainText("既定に従う（自動実行）");
      await expect(page.getByTestId("tool-policy-reason-nl2sql__nl2sql_get_job")).toHaveText(
        "ツールの注釈（readOnlyHint）が読み取り専用"
      );
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`tool-policy-mcp-${viewport.name}.png`), fullPage: true });

      // 既存の環境のように、nl2sql_query を明示で「承認を要求」にできる（既定より優先）。
      await chooseSelectFieldOption(policy, { label: "承認を要求" });
      await expect(page.getByTestId("tool-policy-reason-nl2sql__nl2sql_query")).toHaveText("ツール権限で指定");
      const saved = page.waitForResponse(
        (response) =>
          response.url().endsWith("/api/settings/tool-policy") && response.request().method() === "PATCH" && response.ok()
      );
      await page.getByRole("button", { name: "保存" }).click();
      await saved;
      expect(mockApi.lastRequest("PATCH", "/api/settings/tool-policy")?.body).toMatchObject({
        ask: ["nl2sql__nl2sql_query"],
        deny: ["rag__rag_search"],
      });
    });
  }

  test("ツール権限を表示して保存できる", async ({ page }) => {
    await page.goto("/settings/tool-policy");

    await expect(page.getByRole("heading", { name: "ツール権限", level: 1 })).toBeVisible();
    await expect(page.getByLabel("未指定ツールの既定動作")).toBeVisible();
    // 組み込みのツールと、URL を設定した MCP 接続のツール（#1814）。既定の RAG / NL2SQL は URL が未設定なので並ばず、失敗にもしない。
    await expect(page.getByText("echo", { exact: true })).toBeVisible();
    await expect(page.getByText("rag__rag_search")).toHaveCount(0);
    await expect(page.getByTestId("tool-policy-connection-errors")).toHaveCount(0);
    await expect(page.getByText("sandbox_command_run")).toHaveCount(0);
    // 権限レベルと side_effects は tool の分類（状態ではない）なので、StatusBadge のアイコンを付けない
    const badges = page.locator("main [data-status-variant]");
    await expect(badges.filter({ hasText: /^(読み取り|書き込み|機密|副作用あり)$/ }).first()).toBeVisible();
    await expect(badges.filter({ hasText: /^(読み取り|書き込み|機密|副作用あり)$/ }).locator("svg")).toHaveCount(0);

    // ツールは検索・ページングの表（#818）。ポリシーの読み上げ名はツール名を含む。
    const table = page.getByRole("table", { name: "ツール権限" });
    await expect(table.getByRole("columnheader", { name: "ポリシー" })).toBeVisible();
    const search = page.getByRole("searchbox", { name: "ツール名・説明・接続で絞り込み" });
    await search.fill("該当なし");
    await expect(page.getByText("検索に一致するツールがありません")).toBeVisible();
    await page.getByRole("button", { name: "検索語をクリア" }).last().click();
    await search.fill("echo");
    await expect(page.getByTestId("tool-policy-toolbar")).toContainText("1 件");
    await expectNoHorizontalOverflow(page);
    const firstPolicy = page.getByRole("combobox", { name: "echo のポリシー", exact: true });
    // 連続保存では前回のトーストが残るため、保存 API の成功を待ってから最新のトーストを確認する。
    const savePolicy = async () => {
      const saved = page.waitForResponse(
        (response) =>
          response.url().endsWith("/api/settings/tool-policy") &&
          response.request().method() === "PATCH" &&
          response.ok()
      );
      await page.getByRole("button", { name: "保存" }).click();
      await saved;
      await expect(page.getByText("設定を保存しました").last()).toBeVisible();
    };
    await chooseSelectFieldOption(firstPolicy, { label: "自動実行" });
    await savePolicy();

    await chooseSelectFieldOption(firstPolicy, { label: "既定に従う（自動実行）" });
    await savePolicy();
  });

  test("MCP 接続を API キーで追加し、ツールを取得して削除できる", { tag: "@responsive" }, async ({ page, mockApi }) => {
    await page.goto("/settings/mcp-connections");

    await expect(page.getByRole("heading", { name: "MCP 接続", level: 1 })).toBeVisible();
    // RAG / NL2SQL は標準の接続として、用途の分かる名前で最初から並ぶ（接続 ID は rag / nl2sql。#1325）。
    const table = page.getByRole("table", { name: "MCP 接続" });
    await expect(table.getByRole("link", { name: /ナレッジ検索（RAG） rag/ })).toBeVisible();
    await expect(table.getByRole("link", { name: /データ問い合わせ（NL2SQL） nl2sql/ })).toBeVisible();
    await expect(table.getByRole("row", { name: /ナレッジ検索（RAG）/ }).getByText("標準", { exact: true })).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.getByRole("button", { name: "接続を追加" }).click();
    await expect(page).toHaveURL(/\/settings\/mcp-connections\?id=new$/);
    await page.locator("#mcp-server-id").fill("crm");
    await page.locator("#mcp-server-label").fill("CRM Gateway");
    await page.locator("#mcp-server-base-url").fill("http://mcp.example.test/jsonrpc");
    await page.locator("#mcp-server-timeout").fill("7");
    await chooseSelectFieldOption(page.locator("#mcp-server-auth-mode"), "api_key");
    await page.locator("#mcp-server-api-key").fill("crm-secret");
    await page.getByRole("button", { name: "作成" }).click();
    await expect(page.getByText("MCP 接続を追加しました")).toBeVisible();
    // 作成後は作成した対象のエディタへ移る。API キーの値は送るだけで、画面にも応答にも残らない。
    await expect(page).toHaveURL(/\?id=crm$/);
    await expect(page.getByRole("heading", { name: "CRM Gateway", level: 1 })).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/settings/mcp-connections")?.body).toMatchObject({
      server_id: "crm",
      auth_mode: "api_key",
      api_key: "crm-secret",
    });
    await expect(page.locator("#mcp-server-api-key")).toHaveValue("");
    await expect(page.getByText("この接続はまだ使えません")).toHaveCount(0);

    // ツールの取得は保存した接続で呼び、結果はボタンの直下に出す（読み取り専用か、承認が必要か）。
    await page.getByRole("button", { name: "ツールを取得" }).click();
    // 結果は結果パネル（何が起きたか・所要時間・詳細は畳む）で出し、その下に一覧を出す（messaging.md §10。#814）。
    const toolsResult = page.getByTestId("mcp-tools-result");
    await expect(toolsResult).toHaveAttribute("data-tone", "success");
    await expect(toolsResult).toContainText(/CRM Gateway から \d+ 個のツールを取得しました。/);
    await expect(toolsResult).toContainText(/所要時間: \d+ ms/);
    await expect(toolsResult.locator("details")).not.toHaveAttribute("open", "");
    // md 以上は表、md 未満（mobile-375 の project）はカードの一覧（#823）。
    const tools = (page.viewportSize()?.width ?? 1280) >= 768
      ? page.getByRole("table", { name: "ツール" })
      : page.getByRole("region", { name: /^ツール/ });
    await expect(tools.getByText("lookup_customer", { exact: true })).toBeVisible();
    await expect(tools.getByText("crm__update_order")).toBeVisible();
    // 新しい外部の接続は注釈を信頼しない（#1875）ので、readOnlyHint のツールも承認が必要と示す。
    await expect(tools.getByText("読み取り専用の注釈（承認が必要）")).toBeVisible();
    await expect(tools.getByText("承認が必要", { exact: true })).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.setViewportSize({ width: 375, height: 812 });
    await expectNoHorizontalOverflow(page);
    await page.setViewportSize({ width: 1280, height: 800 });

    // 取得の失敗は、原因・確認ポイントと、開いた「詳細」（status / error code）を同じ位置に出す。一覧は出さない。
    await page.route("**/api/settings/mcp-connections/crm/tools", (route) =>
      route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({ detail: "MCP サーバーに接続できませんでした。", error_code: "mcp.unreachable" }),
      })
    );
    await page.getByRole("button", { name: "ツールを取得" }).click();
    await expect(toolsResult).toHaveAttribute("data-tone", "danger");
    await expect(toolsResult).toContainText("CRM Gateway からツールを取得できませんでした。");
    await expect(toolsResult).toContainText("MCP サーバーに接続できませんでした。");
    await expect(toolsResult.locator("details")).toHaveAttribute("open", "");
    await expect(toolsResult).toContainText("502");
    await expect(tools).toHaveCount(0);
    await page.unroute("**/api/settings/mcp-connections/crm/tools");

    // 削除（行メニュー → 確認）。
    await page.getByTestId("editor-back").click();
    await chooseRowAction(page, "crm", "削除");
    await page.getByRole("button", { name: "削除", exact: true }).click();
    await expect(page.getByText("MCP 接続を削除しました")).toBeVisible();
    await expect(page.getByRole("button", { name: "crm の操作" })).toHaveCount(0);
  });

  test("外部の接続のツールの注釈の信頼は既定でオフで、確認してオンにでき、標準の接続は常に使う（#1832・#1875）", { tag: "@responsive" }, async ({
    page,
    mockApi,
  }) => {
    // 値を保存していない既存の外部の接続（#1832 より前に登録）。backend は信頼しない（false）として返す。
    mockApi.state.mcpConnections.connections.push({
      server_id: "wiki",
      label: "Wiki",
      base_url: "http://wiki.example.test/mcp",
      auth_mode: "none",
      timeout_seconds: 10,
      source: "runtime",
      removable: true,
      configured: true,
      api_key_configured: false,
      oauth_configured: false,
      session_configured: false,
      service_token_configured: false,
      trust_tool_annotations: false,
    });
    // 一覧は md 以上で表、md 未満でカードを出し（もう一方は CSS で隠す。#823）、どちらの幅でも判定のバッジは
    // 見える。見えている方で確かめる（#1859）。
    const toolsList = () =>
      (page.viewportSize()?.width ?? 1280) >= 768
        ? page.getByRole("table", { name: "ツール" })
        : page.getByRole("region", { name: /^ツール/ });

    // 新しい接続はオフで始める。
    await page.goto("/settings/mcp-connections?id=new");
    const trust = page.getByRole("switch", { name: "ツールの注釈（readOnlyHint）を信頼する" });
    await expect(trust).not.toBeChecked();
    await expect(trust).toBeEnabled();

    await page.goto("/settings/mcp-connections?id=wiki");
    await expect(trust).not.toBeChecked();
    const helper = page.getByTestId("mcp-trust-annotations");
    await expect(helper).toContainText("オフ（既定）では");
    await expect(helper).toContainText("MCP の仕様では");
    await expect(helper).toContainText("信頼できる接続だけオンにしてください");
    // オフの接続では、読み取り専用の注釈のツールも承認が必要と示す。
    await page.getByRole("button", { name: "ツールを取得" }).click();
    await expect(toolsList().getByText("読み取り専用の注釈（承認が必要）")).toBeVisible();
    // ツール権限の画面の「判定の理由」も、注釈を信頼しない外部の接続と示す。
    await page.goto("/settings/tool-policy");
    await page.getByRole("searchbox", { name: "ツール名・説明・接続で絞り込み" }).fill("wiki__lookup_customer");
    await expect(page.getByTestId("tool-policy-reason-wiki__lookup_customer")).toHaveText(
      "読み取り専用の注釈はあるが、接続が注釈を信頼していないため未指定ツールの既定動作（外部の接続は既定で信頼しない）"
    );
    await expect(
      page.getByRole("combobox", { name: "wiki__lookup_customer のポリシー", exact: true })
    ).toContainText("既定に従う（承認を要求）");

    // オンにする前に、MCP の仕様の注意を確認のダイアログで示す。キャンセルならオフのまま。
    await page.goto("/settings/mcp-connections?id=wiki");
    await trust.click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("ツールの注釈を信頼しますか？");
    await expect(dialog).toContainText("Wiki が読み取り専用（readOnlyHint）と申告したツールは");
    await expect(dialog).toContainText("承認なしで実行されます");
    await expect(dialog).toContainText("MCP の仕様では");
    await expectNoHorizontalOverflow(page);
    await dialog.getByRole("button", { name: "キャンセル" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(trust).not.toBeChecked();

    // 確認してオンにし、保存する。
    await trust.click();
    await page.getByRole("alertdialog").getByRole("button", { name: "信頼する" }).click();
    await expect(trust).toBeChecked();
    await page.getByRole("button", { name: "保存", exact: true }).click();
    await expect(page.getByText("MCP 接続を保存しました")).toBeVisible();
    expect(mockApi.lastRequest("PATCH", "/api/settings/mcp-connections/wiki")?.body).toMatchObject({
      trust_tool_annotations: true,
    });
    await page.getByRole("button", { name: "ツールを取得" }).click();
    await expect(toolsList().getByText("読み取り専用", { exact: true }).first()).toBeVisible();
    await expect(toolsList().getByText("読み取り専用の注釈（承認が必要）")).toHaveCount(0);

    // オフに戻すのは安全側なので確かめない。
    await trust.click();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    await expect(trust).not.toBeChecked();

    // 標準の接続（RAG / NL2SQL）は注釈を常に使う（オフにできない）。
    await page.goto("/settings/mcp-connections?id=nl2sql");
    await expect(trust).toBeChecked();
    await expect(trust).toBeDisabled();
    await expect(helper).toContainText("注釈を常に使います");
  });

  test("ツールの取得に失敗した後に API キーを保存し直すと、前の設定での結果を消す（#1014）", async ({ page, mockApi }) => {
    mockApi.state.mcpConnections.connections.push({
      server_id: "crm",
      label: "CRM Gateway",
      base_url: "http://mcp.example.test/jsonrpc",
      auth_mode: "api_key",
      service_audience: null,
      timeout_seconds: 10,
      source: "runtime",
      removable: true,
      configured: true,
      api_key_configured: true,
      oauth_configured: false,
      session_configured: false,
      service_token_configured: false,
    });
    await page.route("**/api/settings/mcp-connections/crm/tools", (route) =>
      route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({ detail: "MCP 接続「CRM Gateway」が HTTP 401 を返しました（認証・権限の設定を確認してください）。" }),
      })
    );
    await page.goto("/settings/mcp-connections?id=crm");
    await page.getByRole("button", { name: "ツールを取得" }).click();
    const toolsResult = page.getByTestId("mcp-tools-result");
    await expect(toolsResult).toHaveAttribute("data-tone", "danger");
    await expect(toolsResult).toContainText("HTTP 401");

    // 新しい API キーを保存したら、前のキーでの失敗は消える（URL・認証方式は変わらない。messaging.md §10.4）。
    await page.locator("#mcp-server-api-key").fill("new-secret");
    await page.getByRole("button", { name: "保存", exact: true }).click();
    await expect(page.getByText("MCP 接続を保存しました")).toBeVisible();
    expect(mockApi.lastRequest("PATCH", "/api/settings/mcp-connections/crm")?.body).toMatchObject({ api_key: "new-secret" });
    await expect(toolsResult).toHaveCount(0);
    await expect(page.getByRole("button", { name: "ツールを取得" })).toBeEnabled();
  });

  test("RAG / NL2SQL の接続は URL を保存し、サービストークンの準備の状態を表示する", async ({ page, mockApi }) => {
    await page.goto("/settings/mcp-connections?id=rag");

    await expect(page.getByRole("heading", { name: "ナレッジ検索（RAG）", level: 1 })).toBeVisible();
    // 標準の接続は「標準」の印と説明を出し、名前は変えられない。配備で URL が無い構成では URL を設定できる（#1325）。
    await expect(page.getByText("標準", { exact: true })).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: "標準の接続" })).toContainText(
      "配備で URL が設定されていないため、URL はここで設定できます。"
    );
    await expect(page.locator("#mcp-server-label")).toBeDisabled();
    await expect(page.locator("#mcp-server-base-url")).toBeEnabled();
    // 未設定のあいだは、足りないものと直し方を warning の Banner で示す。
    const notice = page.getByRole("status").filter({ hasText: "この接続はまだ使えません" });
    await expect(notice).toContainText("MCP の URL が未設定です");
    await expect(notice).toContainText("PLATFORM_SERVICE_TOKEN_SECRET");
    // 認証方式は Run の利用者のサービストークンで固定し、API キーの欄は無い。
    await expect(page.locator("#mcp-server-auth-mode")).toBeDisabled();
    await expect(page.locator("#mcp-server-api-key")).toHaveCount(0);
    const authStatus = page.getByRole("list", { name: "サービス間認証の状態" });
    // 状態は署名鍵だけ（利用者の無い呼び出しを置き換えるサービス利用者の設定は無い。#3251）。
    await expect(authStatus.getByRole("listitem")).toHaveCount(1);
    await expect(authStatus).toContainText("PLATFORM_SERVICE_TOKEN_SECRET");
    await expect(authStatus).not.toContainText("AGENT_MCP_SERVICE_USER_LOGIN_ID");
    await expect(page.locator("#mcp-server-timeout")).toHaveValue("60");
    // RAG / NL2SQL は削除できない（対象の操作が無い）。
    await expect(page.getByTestId("mcp-server-object-actions").getByRole("button")).toHaveCount(0);

    await page.locator("#mcp-server-base-url").fill("http://rag-host/api/mcp");
    await page.getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("MCP 接続を保存しました")).toBeVisible();
    await expect(notice).not.toContainText("MCP の URL が未設定です");
    await expect(notice).toContainText("PLATFORM_SERVICE_TOKEN_SECRET");
    // 標準の接続の名前は送らない（配備が決める）。
    expect(mockApi.lastRequest("PATCH", "/api/settings/mcp-connections/rag")?.body).toEqual({
      base_url: "http://rag-host/api/mcp",
      timeout_seconds: 60,
    });

    // すべて設定済みなら Banner は出さず、状態は StatusBadge で示す。
    const nl2sql = mockApi.state.mcpConnections.connections.find((item) => item.server_id === "nl2sql")!;
    Object.assign(nl2sql, {
      base_url: "http://nl2sql-host/api/mcp",
      configured: true,
      service_token_configured: true,
    });
    await page.goto("/settings/mcp-connections?id=nl2sql");
    await expect(page.getByRole("heading", { name: "データ問い合わせ（NL2SQL）", level: 1 })).toBeVisible();
    await expect(page.locator("#mcp-server-base-url")).toHaveValue("http://nl2sql-host/api/mcp");
    await expect(page.getByText("この接続はまだ使えません")).toHaveCount(0);
    await expect(
      page.getByRole("list", { name: "サービス間認証の状態" }).getByText("設定済み")
    ).toHaveCount(1);

    await page.setViewportSize({ width: 375, height: 812 });
    await expect(page.locator("#mcp-server-base-url")).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });

  test("配備で URL を設定した標準の接続は、URL・名前を変えられず削除もできない（desktop / 375px。#1325）", async ({ page, mockApi }, testInfo) => {
    const rag = mockApi.state.mcpConnections.connections.find((item) => item.server_id === "rag")!;
    Object.assign(rag, {
      base_url: "http://127.0.0.1:8000/api/mcp",
      base_url_locked: true,
      configured: true,
      service_token_configured: true,
    });
    for (const viewport of [
      { name: "desktop", width: 1280, height: 800 },
      { name: "375", width: 375, height: 812 },
    ]) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.goto("/settings/mcp-connections");
      const table = page.getByRole("table", { name: "MCP 接続" });
      const ragRow = table.getByRole("row", { name: /ナレッジ検索（RAG）/ });
      await expect(ragRow.getByText("標準", { exact: true })).toBeVisible();
      // 削除できないので、行の操作メニューは使えない。
      await expect(ragRow.locator('[aria-haspopup="menu"]')).toHaveCount(0);
      await expectNoHorizontalOverflow(page);

      await page.goto("/settings/mcp-connections?id=rag");
      await expect(page.getByRole("heading", { name: "ナレッジ検索（RAG）", level: 1 })).toBeVisible();
      const notice = page.getByRole("status").filter({ hasText: "標準の接続" });
      await expect(notice).toContainText("配備で設定した接続です。削除と、名前・URL・認証方式の変更はできません。");
      await expect(page.locator("#mcp-server-base-url")).toBeDisabled();
      await expect(page.locator("#mcp-server-base-url")).toHaveValue("http://127.0.0.1:8000/api/mcp");
      await expect(page.locator("#mcp-server-base-url")).toHaveAccessibleDescription(/AGENT_EXTERNAL_RAG_MCP_URL/);
      await expect(page.locator("#mcp-server-label")).toBeDisabled();
      await expect(page.locator("#mcp-server-auth-mode")).toBeDisabled();
      await expect(page.getByTestId("mcp-server-object-actions").getByRole("button")).toHaveCount(0);
      // 準備ができていれば「ツールを取得」は使える。
      await expect(page.getByRole("button", { name: "ツールを取得" })).toBeEnabled();
      await expectNoHorizontalOverflow(page);
      await page.screenshot({ path: testInfo.outputPath(`mcp-builtin-locked-${viewport.name}.png`), fullPage: true });
    }

    // タイムアウトは変えられ、URL と名前は送らない。
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.locator("#mcp-server-timeout").fill("90");
    await page.getByRole("button", { name: "保存", exact: true }).click();
    await expect(page.getByText("MCP 接続を保存しました")).toBeVisible();
    expect(mockApi.lastRequest("PATCH", "/api/settings/mcp-connections/rag")?.body).toEqual({ timeout_seconds: 90 });
  });

  test("スキルを追加・編集・削除でき、ビルトインは保護される", async ({ page }) => {
    await page.goto("/skills");

    await expect(page.getByRole("heading", { name: "スキル", level: 1 })).toBeVisible();
    // ビルトインは詳細を開けるが、行メニューは書き出しだけで削除を持たない（#1874）
    await page.getByRole("button", { name: "業務 RAG 調査 の操作" }).click();
    await expect(page.getByRole("menuitem", { name: "Agent Skills の形で書き出す" })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: "削除" })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.getByRole("link", { name: /業務 RAG 調査/ }).click();
    await expect(page.getByText("このスキルは読み取り専用です", { exact: false })).toBeVisible();
    await expect(page.getByRole("button", { name: "保存" })).toHaveCount(0);
    await page.getByTestId("editor-back").click();
    await expectNoHorizontalOverflow(page);

    // 追加
    await page.getByRole("button", { name: "スキルを追加" }).click();
    // 必須の欄はラベルの後ろに「必須」のタグ（aria-hidden）が付き、getByLabel の exact はタグの文字も含めて比べるため、
    // 支援技術と同じアクセシブルネームで探す（#531）。
    await page.getByRole("textbox", { name: "ID", exact: true }).fill("e2e_custom");
    await page.getByLabel("名前").fill("E2E カスタム");
    await page
      .getByLabel("MCP 依存 (JSON)")
      .fill('[{"server_id":"rag","tool_names":["rag_search"]}]');
    await page.getByRole("button", { name: "作成" }).click();
    await expect(page.getByText("スキルを追加しました")).toBeVisible();
    await expect(page).toHaveURL(/\/skills\?id=e2e_custom$/);
    // URL の更新は画面の切替（transition）より先に終わるため、編集のエディタが出たことを待つ。
    await expect(page.getByRole("heading", { name: "E2E カスタム", level: 1 })).toBeVisible();
    await expect(page.locator("#skill-id")).toBeDisabled();
    await expect(page.getByLabel("MCP 依存 (JSON)")).toHaveValue(/rag_search/);

    // 編集
    await page.getByLabel("名前").fill("E2E カスタム改");
    await page.getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("スキルを更新しました")).toBeVisible();

    // 宣言の再読込（一覧のページ操作）
    await page.getByTestId("editor-back").click();
    await page.getByRole("button", { name: "最新情報を取得" }).click();
    await expect(page.getByText("スキルの最新情報を取得しました")).toBeVisible();

    // 削除（行メニュー → 確認）
    await chooseRowAction(page, "E2E カスタム改", "削除");
    await page.getByRole("button", { name: "削除", exact: true }).click();
    await expect(page.getByText("スキルを削除しました")).toBeVisible();
    await expect(page.getByRole("link", { name: /E2E カスタム改/ })).toHaveCount(0);

    await page.setViewportSize({ width: 375, height: 812 });
    await expectNoHorizontalOverflow(page);
  });

  // 保存の失敗は保存ボタンの上の Banner ではなく、操作の行の FormStatus に出す（messaging.md §10.2。#725）。
  for (const viewport of [
    { name: "desktop", width: 1280, height: 800 },
    { name: "mobile", width: 375, height: 812 },
  ]) {
    test(`設定の保存の失敗は操作の行に出す (${viewport.name})`, async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.goto("/settings/tool-policy");
      await expect(page.getByRole("heading", { name: "ツール権限", level: 1 })).toBeVisible();
      await page.route("**/api/settings/tool-policy", (route) =>
        route.request().method() === "PATCH"
          ? route.fulfill({
              status: 500,
              contentType: "application/json",
              body: JSON.stringify({ data: null, error_messages: ["設定ファイルに書き込めません。"], warning_messages: [] }),
            })
          : route.fallback()
      );
      await chooseSelectFieldOption(page.locator("#tool-policy-default-mode"), "deny");
      await page.getByRole("button", { name: "保存", exact: true }).click();

      const actions = page.getByRole("group", { name: "ツール権限 の保存" });
      await expect(actions.getByRole("alert")).toHaveText("保存できませんでした。設定ファイルに書き込めません。");
      await expect(page.getByRole("alert")).toHaveCount(1);
      await expectNoHorizontalOverflow(page);
    });
  }

  // #1728: 通知は画面幅ごとに決まった 1 か所（desktop は画面の右下で下端から 8rem、375px は上端のバー）に出し、
  // 主操作を覆わない。ページの末尾（スクロールしきると画面の下端）の保存ボタンは通知より下に来るので、
  // ポインタが通知に乗って自動の消去が止まって（#351）も押せる。
  for (const viewport of [
    { name: "desktop", width: 1280, height: 720 },
    { name: "mobile", width: 375, height: 812 },
  ]) {
    test(`通知を出したまま、ページの末尾の保存を押せる (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      // 保存ボタンが画面の下端に来るよう、ツールの一覧を長くする（MCP 接続のツールは並ばないため。#757）。
      for (let index = 1; index <= 12; index += 1) {
        mockApi.state.tools.push({
          name: `e2e_tool_${index}`,
          description: "スクロールの確認用のツール",
          input_schema: { type: "object" },
          output_schema: { type: "object" },
          permission_level: "read",
          side_effects: false,
          timeout_seconds: 10,
          max_retries: 0,
          audit_tags: [],
        });
      }
      await page.goto("/settings/tool-policy");
      await expect(page.getByRole("heading", { name: "ツール権限", level: 1 })).toBeVisible();
      const saves = () =>
        mockApi.requests.filter((request) => request.method === "PATCH" && request.path.endsWith("/settings/tool-policy")).length;
      const defaultMode = page.locator("#tool-policy-default-mode");

      await chooseSelectFieldOption(defaultMode, "deny");
      const save = page.getByRole("button", { name: "保存", exact: true });
      await save.click();
      const notice = page.getByRole("region", { name: "通知" }).getByRole("status").filter({ hasText: "設定を保存しました" });
      await expect(notice).toBeVisible();
      await expect.poll(saves).toBe(1);
      await expectToastPlacement(page);

      // ページの末尾までスクロールすると、保存ボタンは画面の下端に来る。
      await chooseSelectFieldOption(defaultMode, "approval");
      await page.locator("main").evaluate((main) => {
        main.scrollTop = main.scrollHeight;
      });
      await expect(save).toBeInViewport();
      const box = (await save.boundingBox())!;
      expect(viewport.height - (box.y + box.height)).toBeLessThan(120);

      // 通知にポインタを乗せて止めたまま（一時停止は維持）、覆われていない保存ボタンを押せる。
      await notice.hover();
      await expect(notice).toBeVisible();
      const hit = await save.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
        return top !== null && element.contains(top);
      });
      expect(hit).toBe(true);
      expect(overlaps((await notice.boundingBox())!, box)).toBe(false);
      // スクロールしても通知の位置は変わらない。
      await expectToastPlacement(page);
      await save.click({ timeout: 5_000 });
      await expect.poll(saves).toBe(2);
      await expectNoHorizontalOverflow(page);
    });
  }

  test("業務 Agent は Skill だけを選択して保存できる", async ({ page }) => {
    await page.goto("/agents");

    await expect(page.getByRole("heading", { name: "業務 Agent", level: 1 })).toBeVisible();
    await page.getByRole("button", { name: "業務 Agent を作成" }).click();
    await expect(page).toHaveURL(/\/agents\?id=new$/);
    await page.locator("#new-agent-name").fill("RAG Skill Agent");
    await page.locator("#new-agent-description").fill("Skill で能力を選択する");
    // スキルは ListPicker（listbox）で選ぶ（#808）。
    const skillOption = page.getByRole("option", { name: /^業務 RAG 調査/ });
    await skillOption.click();
    await expect(skillOption).toHaveAttribute("aria-checked", "true");
    await page.getByRole("button", { name: "作成" }).first().click();
    await expect(page.getByText("Agent を作成しました")).toBeVisible();
    await expect(page.getByRole("heading", { name: "RAG Skill Agent", level: 1 })).toBeVisible();
    await expect(page.getByText("Command allowed prefixes")).toHaveCount(0);
    await expectNoHorizontalOverflow(page);

    await page.setViewportSize({ width: 375, height: 812 });
    await expect(page.getByText("業務 RAG 調査").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });
});
