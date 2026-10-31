import { expect, test } from "./fixtures/test";
import { mockDatabaseReady, mockLocalAuth, expectNoPageOverflow } from "./_helpers";

for (const width of [1280, 375]) {
  test(`入力と出力の検査を独立に保存する (${width}px)`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await mockDatabaseReady(page); await mockLocalAuth(page);
    let state = { input_enabled: false, output_enabled: false };
    const bodies: unknown[] = [];
    await page.route("**/api/settings/guardrail", async route => {
      if (route.request().method() === "PATCH") { state = route.request().postDataJSON(); bodies.push(state); }
      await route.fulfill({ json: { data: { ...state, oci_configured: true, oci_warning_code: null, config_source: "runtime" }, error_messages: [], warning_messages: [] } });
    });
    await page.goto("/settings/guardrail");
    await expect(page.getByText(/OCI の検出結果に基づき/)).toBeVisible();
    const input = page.getByRole("checkbox", { name: "入力を検査する", exact: true });
    const output = page.getByRole("checkbox", { name: "出力を検査する", exact: true });
    await expect(input).not.toBeChecked(); await expect(output).not.toBeChecked();
    for (const [i, o] of [[true, false], [false, true], [true, true], [false, false]]) {
      await input.setChecked(i); await output.setChecked(o);
      await page.getByRole("button", { name: "保存", exact: true }).click();
      await expect(page.getByRole("button", { name: "保存", exact: true })).toBeDisabled();
      await page.reload(); await expect(input).toBeChecked({ checked: i }); await expect(output).toBeChecked({ checked: o });
    }
    expect(bodies).toEqual([{ input_enabled: true, output_enabled: false }, { input_enabled: false, output_enabled: true }, { input_enabled: true, output_enabled: true }, { input_enabled: false, output_enabled: false }]);
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath("guardrails-independent.png"), fullPage: true });
  });
  test(`保存失敗と破棄で両方の選択を保持する (${width}px)`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 }); await mockDatabaseReady(page); await mockLocalAuth(page);
    await page.route("**/api/settings/guardrail", route => route.fulfill(route.request().method() === "PATCH"
      ? { status: 500, json: { data: null, error_messages: ["保存できませんでした"], warning_messages: [] } }
      : { json: { data: { input_enabled: false, output_enabled: true, oci_configured: false, oci_warning_code: "oci_guardrails_compartment_missing" }, error_messages: [], warning_messages: [] } }));
    await page.goto("/settings/guardrail");
    const input = page.getByRole("checkbox", { name: "入力を検査する", exact: true });
    const output = page.getByRole("checkbox", { name: "出力を検査する", exact: true });
    await input.check(); await output.uncheck();
    await expect(page.getByRole("link", { name: "OCI 認証設定を開く" })).toBeVisible();
    await page.getByRole("button", { name: "保存", exact: true }).click();
    await expect(page.getByText("保存できませんでした", { exact: true })).toBeVisible();
    await expect(input).toBeChecked(); await expect(output).not.toBeChecked();
    await page.getByRole("button", { name: "変更を破棄" }).click();
    await expect(input).not.toBeChecked(); await expect(output).toBeChecked();
    await output.focus(); await page.keyboard.press("Space");
    await expect(output).not.toBeChecked();
    await expect(page.getByRole("link", { name: "OCI 認証設定を開く" })).toHaveCount(0);
    await expectNoPageOverflow(page);
  });
}
