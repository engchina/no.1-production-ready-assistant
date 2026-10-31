import type { Locator, Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";
import { measureHorizontalOverflow } from "../../../platform/packages/ui/e2e/layout-checks";

// 管理一覧は #2947 の全件の名前自然順。業務 Agent の編集の選択は今回の対象外で、
// #1866 の組み込み優先・backend の順を保持する。

const AGENT_ID = "agent-1866";

const BUILTIN_NAMES = [
  "業務 RAG 調査",
  "構造化データ照会",
  "資料とデータの横断調査",
];

/** backend が返す順（組み込み → 組み込みでないスキルの表示名の順）で、実行時のスキルを足す。 */
function seedRuntimeSkills(mockApi: MockApi) {
  for (const [id, name] of [
    // ID の文字の順なら「業務 RAG 調査」と「資料とデータの横断調査」の間に入る。
    ["github_repo_research", "GitHub リポジトリ調査（テスト）"],
    ["accounting_1866", "経理の手順"],
  ]) {
    mockApi.state.skills.push({
      id,
      name,
      description: `${name}の説明`,
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
}

function seedAgent(mockApi: MockApi) {
  mockApi.state.agents.push({
    id: AGENT_ID,
    name: "並びの確認の Agent",
    description: "",
    instructions: "指示",
    skill_ids: [],
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
}

/** スキルの一覧の行の ID（表示の順）。 */
async function listedSkillIds(page: Page) {
  return page
    .locator('tr[data-testid^="skill-row-"]')
    .evaluateAll((rows) =>
      rows.map((row) =>
        row.getAttribute("data-testid")?.replace("skill-row-", ""),
      ),
    );
}

/** ListPicker の候補の名前（表示の順）。 */
async function optionNames(picker: Locator) {
  return picker.locator('[role="option"] [id$="-label"]').allTextContents();
}

test.describe("スキルの並び", { tag: "@responsive" }, () => {
  test("全由来の名前自然順をページ境界・検索・刷新・再読込でも保つ", async ({ page, mockApi }) => {
    const base = mockApi.state.skills[0];
    const names = [
      ["number-2", "2_検証"], ["number-10", "10_検証"], ["number-20", "20_検証"], ["number-21", "21_検証"],
      ["latin-2", "Skill 2"], ["latin-10", "Skill 10"], ["kana", "あいう"],
      ...Array.from({ length: 30 }, (_, index) => index + 1).flatMap((number) => {
        const row = [`skill-${number}`, `検証スキル ${number}`];
        return number === 10 ? [["aa-same", row[1]], row, ["zz-same", row[1]]] : [row];
      }),
    ];
    const sources = ["runtime", "env", "builtin", "project"];
    const ordered = names.map(([id, name], index) => ({
      ...base, id, name, description: "", enabled: index % 2 === 0,
      source: sources[index % sources.length], tags: index < 2 ? ["絞込タグ"] : [],
    }));
    mockApi.state.skills = [...ordered].reverse();
    await page.goto("/skills");
    const pager = page.getByRole("navigation", { name: "ページ送り", exact: true });
    const expected = ordered.map((row) => row.id);
    await expect.poll(() => listedSkillIds(page)).toEqual(expected.slice(0, 25));
    await expect(pager).toContainText("1–25 / 39 件");
    await pager.getByRole("button", { name: "次へ", exact: true }).click();
    await expect.poll(() => listedSkillIds(page)).toEqual(expected.slice(25));

    mockApi.state.skills.reverse();
    const reloaded = page.waitForResponse((response) => response.url().endsWith("/api/skills") && response.request().method() === "GET");
    const refresh = page.getByRole("button", { name: "最新情報を取得", exact: true });
    if (await refresh.isVisible()) await refresh.click();
    else {
      await page.getByRole("button", { name: "その他の操作", exact: true }).click();
      await page.getByRole("menuitem", { name: "最新情報を取得", exact: true }).click();
    }
    await reloaded;
    await expect.poll(() => listedSkillIds(page)).toEqual(expected.slice(25));
    await page.reload();
    await expect.poll(() => listedSkillIds(page)).toEqual(expected.slice(25));
    await page.getByRole("searchbox").fill("絞込タグ");
    await expect.poll(() => listedSkillIds(page)).toEqual(["number-2", "number-10"]);
    await expect(pager).toHaveCount(0);
    await page.getByRole("searchbox").fill("");
    await expect.poll(() => listedSkillIds(page)).toEqual(expected.slice(0, 25));
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(0);
    await page.screenshot({ path: test.info().outputPath("skill-name-order.png"), fullPage: true });
  });

  test("スキルの管理一覧は組み込みも含めた全件を名前の順にする", async ({
    page,
    mockApi,
  }) => {
    seedRuntimeSkills(mockApi);
    await page.goto("/skills");

    await expect(page.locator('tr[data-testid^="skill-row-"]')).toHaveCount(5);
    expect(await listedSkillIds(page)).toEqual([
      "github_repo_research",
      "business-rag-research",
      "accounting_1866",
      "structured-data-query",
      "rag-and-structured-data",
    ]);

    // 絞り込んでも同じ順。
    await page.getByRole("searchbox").fill("調査");
    await expect(page.locator('tr[data-testid^="skill-row-"]')).toHaveCount(3);
    expect(await listedSkillIds(page)).toEqual([
      "github_repo_research",
      "business-rag-research",
      "rag-and-structured-data",
    ]);
  });

  test("業務 Agent の編集の選択は従来の組み込み優先順を保持し、絞り込み・選択中だけ表示でも変わらない", async ({
    page,
    mockApi,
  }) => {
    seedRuntimeSkills(mockApi);
    seedAgent(mockApi);
    await page.goto(`/agents?id=${AGENT_ID}`);
    await expect(
      page.getByRole("heading", { name: "並びの確認の Agent", level: 1 }),
    ).toBeVisible();

    const picker = page.getByTestId("agent-skill-picker");
    await expect(picker.getByRole("option")).toHaveCount(5);
    expect(await optionNames(picker)).toEqual([
      ...BUILTIN_NAMES,
      "GitHub リポジトリ調査（テスト）",
      "経理の手順",
    ]);

    // 選ぶ順を逆にしても、選択中だけ表示は一覧の順。
    for (const name of [
      "経理の手順",
      "資料とデータの横断調査",
      "業務 RAG 調査",
    ]) {
      await picker.getByRole("option", { name }).click();
    }
    await picker.getByTestId("agent-skill-picker-show-selected").click();
    await expect(picker.getByRole("option")).toHaveCount(3);
    expect(await optionNames(picker)).toEqual([
      "業務 RAG 調査",
      "資料とデータの横断調査",
      "経理の手順",
    ]);
    await picker.getByTestId("agent-skill-picker-show-selected").click();

    // 絞り込んでも同じ順。
    await picker.getByRole("searchbox").fill("調査");
    await expect(picker.getByRole("option")).toHaveCount(3);
    expect(await optionNames(picker)).toEqual([
      "業務 RAG 調査",
      "資料とデータの横断調査",
      "GitHub リポジトリ調査（テスト）",
    ]);
  });
});
