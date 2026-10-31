import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #926: 業務 Agent が使うスキルは削除できない。スキルの ID は URL に置ける形だけを受け付ける。

function seedRuntimeSkill(mockApi: MockApi) {
  mockApi.state.skills.push({
    id: "skill_926",
    name: "経理の手順",
    description: "経理の質問の手順",
    instructions: "",
    mcp_requirements: [],
    references: [],
    enabled: true,
    tags: [],
    source: "runtime",
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

test("業務 Agent が使うスキルの削除は理由を出して断り、スキルを残す", async ({ page, mockApi }) => {
  seedRuntimeSkill(mockApi);
  mockApi.state.agents.push({
    id: "agent-926",
    name: "経理の Agent",
    description: "",
    instructions: "",
    skill_ids: ["skill_926"],
    model_id: "",
    enabled: true,
    source: "runtime",
    versioned: true,
    versions: [],
    published_version: null,
    unpublished_changes: true,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
  await page.goto("/skills");
  await page.getByTestId("skill-row-actions-skill_926").click();
  await page.getByRole("menuitem", { name: "削除" }).click();
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
  await dialog.getByRole("button", { name: "削除", exact: true }).click();

  await expect(
    page.getByText(
      "このスキルは業務 Agent（経理の Agent）が使っています。業務 Agent のスキルから外してから削除してください。"
    )
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "経理の手順 skill_926", exact: true })).toBeVisible();
  expect(mockApi.state.skills.some((skill) => skill.id === "skill_926")).toBe(true);
  await expectNoHorizontalOverflow(page);
});

test("スキルの ID に / や空白を入れると欄の下にエラーを出して送らない", async ({ page, mockApi }) => {
  await page.goto("/skills?id=new");
  await expect(page.getByText("英数字・_・-・. で入力します（作成後は変更できません）。")).toBeVisible();
  await page.locator("#skill-id").fill("経理/手順 1");
  await page.locator("#skill-name").fill("経理の手順");
  await page.getByRole("button", { name: "作成", exact: true }).click();

  const idField = page.locator("#skill-id");
  await expect(idField).toBeFocused();
  await expect(idField).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText("ID は英数字で始め、英数字・_・-・. の 100 文字以内にしてください。")).toBeVisible();
  expect(mockApi.lastRequest("POST", "/api/skills")).toBeUndefined();

  await idField.fill("keiri_procedure-1.0");
  await page.getByRole("button", { name: "作成", exact: true }).click();
  await expect(page.getByText("スキルを追加しました")).toBeVisible();
  expect(mockApi.lastRequest("POST", "/api/skills")?.body).toMatchObject({ id: "keiri_procedure-1.0" });
  await expectNoHorizontalOverflow(page);
});
