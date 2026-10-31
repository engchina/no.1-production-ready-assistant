import { expect, type Page } from "./fixtures/test";
import { expectNoPageOverflow } from "./_helpers";

export async function mockFeaturePrompts(page: Page) {
  const keys = ["heading_levels", "field_extraction", "navigation_summary", "raptor_summary_system"];
  const values = new Map<string, string>();
  const requests: { key: string; method: string }[] = [];
  await page.route("**/api/settings/answer-prompts**", async (route) => {
    const method = route.request().method();
    const key = route.request().url().split("/").at(-1)!;
    if (method === "PUT") values.set(key, route.request().postDataJSON().content);
    if (method === "DELETE") values.delete(key);
    if (method !== "GET") requests.push({ key, method });
    await route.fulfill({ json: { data: { prompts: keys.map((key) => ({
      key, content: values.get(key) ?? "合成の既定指示", default_content: "合成の既定指示",
      customized: values.has(key), required_placeholders: [], updated_at: null,
    })), stages: [] }, error_messages: [], warning_messages: [] } });
  });
  return requests;
}

export async function verifyFeaturePrompt(page: Page, key: string, title: string) {
  const summary = page.locator("summary").filter({ hasText: title });
  await summary.click();
  const disclosure = summary.locator("..");
  const editor = page.locator(`#answer-prompt-${key}`);
  await expect(editor).toBeEditable();
  await expect(editor).toHaveValue("合成の既定指示");
  const actions = disclosure.getByRole("group", { name: "編集内容の操作" });
  await expect(actions.getByRole("button")).toHaveText(["プロンプトを保存", "変更を破棄", "既定に戻す"]);
  await editor.fill("破棄する指示");
  await disclosure.getByRole("button", { name: "変更を破棄" }).click();
  await expect(editor).toHaveValue("合成の既定指示");
  await editor.fill("合成の保存指示");
  await disclosure.getByRole("button", { name: "プロンプトを保存" }).click();
  await expect(disclosure.getByRole("button", { name: "プロンプトを保存" })).toBeDisabled();
  await disclosure.getByRole("button", { name: "既定に戻す", exact: true }).click();
  await disclosure.getByRole("button", { name: "プロンプトを保存" }).click();
  await expect(editor).toHaveValue("合成の既定指示");
  await expectNoPageOverflow(page);
}

/** 配置先ごとに、工程の編集内容が実際の key へ保存されることを確かめる。 */
export async function mockEditableStage(page: Page, stageId: string, phase: string, partId: string) {
  const key = `${stageId}.${partId}`;
  const requests: { key: string; method: string; content: string | null }[] = [];
  let content: string | null = null;
  await page.route("**/api/settings/answer-prompts**", async (route) => {
    const method = route.request().method();
    if (method !== "GET") {
      content = method === "DELETE" ? null : route.request().postDataJSON().content;
      requests.push({ key: route.request().url().split("/").at(-1)!, method, content });
    }
    const builtin = "## Step 1: 合成の既定指示";
    await route.fulfill({ json: { data: {
      prompts: [{ key, content: content ?? builtin, default_content: builtin, customized: content !== null, required_placeholders: [], updated_at: null }],
      stages: [{ id: stageId, phase, prompts: [{ id: partId, content: content ?? builtin, prompt_key: key }] }],
    }, error_messages: [], warning_messages: [] } });
  });
  return requests;
}
