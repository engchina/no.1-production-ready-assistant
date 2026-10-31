import { expect, test, type Page } from "./fixtures/test";
import { mockLocalAuth } from "./_helpers";

// platform/docs/ux-contracts/messaging.md §3.5 ConfirmDialog の振る舞いを検証する。
// モデル設定のモデル削除（破壊的操作）に確認ゲートが入っていることを確認する。

function createModelSettings() {
  return {
    settings: {
      enterprise_ai: {
        endpoint: "",
        project_ocid: "ocid1.generativeaiproject.oc1.us-chicago-1.example",
        api_key: "",
        has_api_key: true,
        clear_api_key: false,
        models: [
          { model_id: "enterprise-llm", display_name: "標準 LLM", vision_enabled: false },
          { model_id: "enterprise-vision", display_name: "Vision LLM", vision_enabled: true },
        ],
        default_text_model_id: "enterprise-llm",
        default_vision_model_id: "enterprise-vision",
        api_path: "/responses",
        text_payload_template: '{"input":{"messages":"${messages}","params":"${parameters}"}}',
        vision_payload_template: '{"input":{"document":"${data_base64}"}}',
        text_response_path: "",
        vision_response_path: "",
        timeout_seconds: 60,
        max_retries: 3,
      },
      generative_ai: {
        embedding_model: "cohere.embed-v4.0",
        embedding_dim: 1536,
        rerank_model: "cohere.rerank-v4.0-fast",
      },
    },
    checks: { enterprise_ai: "ok", generative_ai: "ok", embedding_dim: "ok" },
    source: "runtime",
  };
}

async function mockModelSettings(page: Page) {
  await page.route("**/api/settings/model", async (route) => {
    await route.fulfill({
      json: { data: createModelSettings(), error_messages: [], warning_messages: [] },
    });
  });
}

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
  await mockModelSettings(page);
});

test("削除はキャンセルするとモデルを残す", async ({ page }) => {
  await page.goto("/settings/model");
  await expect(page.getByLabel("モデル ID 2")).toHaveValue("enterprise-vision");

  await page.getByRole("button", { name: "モデルを削除 2" }).click();

  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("enterprise-vision");

  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(dialog).toHaveCount(0);
  // キャンセルしたのでモデルは 2 件のまま。
  await expect(page.getByLabel("モデル ID 2")).toHaveValue("enterprise-vision");
});

test("削除を確定するとモデルが取り除かれる", async ({ page }) => {
  await page.goto("/settings/model");
  await expect(page.getByLabel("モデル ID 2")).toHaveValue("enterprise-vision");

  await page.getByRole("button", { name: "モデルを削除 2" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "削除" }).click();

  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect(page.getByLabel("モデル ID 2")).toHaveCount(0);
  await expect(page.getByLabel("モデル ID 1")).toHaveValue("enterprise-llm");
});

test("Esc キーで確認ダイアログを閉じる（キャンセル扱い）", async ({ page }) => {
  await page.goto("/settings/model");
  await page.getByRole("button", { name: "モデルを削除 2" }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect(page.getByLabel("モデル ID 2")).toHaveValue("enterprise-vision");
});
