import { jumpToPage } from "../../../platform/packages/ui/e2e/layout-checks";
import { chooseSelectFieldOption } from "./fixtures/select-field";
import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #770: 業務 Agent の下書き・公開・版の履歴・前の版に戻す。Run の「下書きで実行」。

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

function seedDraftAgent(mockApi: MockApi) {
  mockApi.state.agents.push({
    id: "agent-770",
    name: "経理の Agent",
    description: "経理の質問に答える",
    instructions: "v1 の指示",
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

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
]) {
  test(`下書きを公開し、変更して再公開し、前の版に戻せる (${viewport.name})`, async ({ page, mockApi }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedDraftAgent(mockApi);
    await page.goto("/agents?id=agent-770");

    await expect(page.getByRole("heading", { name: "経理の Agent", level: 1 })).toBeVisible();
    await expect(page.getByText("未公開", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("この業務 Agent はまだ公開していません。", { exact: false })).toBeVisible();
    await expect(page.getByText("まだ公開していません。", { exact: true })).toBeVisible();

    // 公開（対象への操作は概要の ObjectActionBar。確認してから公開する）。
    const actions = page.getByTestId("agent-object-actions");
    const publish = actions.getByRole("button", { name: "公開" });
    if (await publish.count()) {
      await publish.click();
    } else {
      await page.getByTestId("agent-object-actions-more").click();
      await page.getByRole("menuitem", { name: "公開" }).click();
    }
    const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
    await expect(dialog.getByText("v1 として公開しますか?")).toBeVisible();
    await dialog.getByRole("button", { name: "公開", exact: true }).click();
    await expect(page.getByText("v1 として公開しました")).toBeVisible();
    await expect(page.getByText("公開中 v1").first()).toBeVisible();
    const versions = page.getByRole("table", { name: "版の履歴" });
    await expect(versions.getByText("v1", { exact: true })).toBeVisible();
    // 公開者は表示名で出し、ログイン ID（local）は出さない（#1696）。
    await expect(versions).toContainText("ローカル利用者");

    // 下書きを変えて保存すると「公開していない変更あり」。再公開で v2。
    await page.getByLabel("指示").fill("v2 の指示");
    await page.getByRole("button", { name: "保存", exact: true }).click();
    await expect(page.getByText("Agent を保存しました")).toBeVisible();
    await expect(page.getByText("公開していない変更あり").first()).toBeVisible();
    expect(mockApi.state.agents.find((agent) => agent.id === "agent-770")?.instructions).toBe("v2 の指示");

    // v1 に戻す（版の行メニュー → 確認）。
    await page.getByRole("button", { name: "v1 の操作", exact: true }).click();
    await page.getByRole("menuitem", { name: "この版に戻す" }).click();
    await expect(dialog.getByText("v1 に戻しますか?")).toBeVisible();
    await dialog.getByRole("button", { name: "この版に戻す", exact: true }).click();
    await expect(page.getByText("v1 に戻しました")).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/agents/agent-770/versions/1/restore")).toBeDefined();
    await expect(page.getByLabel("指示")).toHaveValue("v1 の指示");
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`agent-versions-${viewport.name}.png`), fullPage: true });
  });
}

function seedSkillUpdatedAgent(mockApi: MockApi) {
  // 公開中の v1 に「業務 RAG 調査」を固定した後、そのスキルが変わった業務 Agent（#1855）。
  mockApi.state.agents.push({
    id: "agent-1855",
    name: "調査の Agent",
    description: "資料を調べる",
    instructions: "資料を調べて答える。",
    skill_ids: ["business-rag-research"],
    model_id: "",
    enabled: true,
    source: "runtime",
    versioned: true,
    versions: [
      {
        version: 1,
        name: "調査の Agent",
        description: "資料を調べる",
        instructions: "資料を調べて答える。",
        skill_ids: ["business-rag-research"],
        model_id: "",
        data_scopes: {},
        note: "",
        published_at: MOCK_NOW,
        published_by: "local",
        skills: [{ id: "business-rag-research", name: "業務 RAG 調査", description: "", content_hash: "hash-old" }],
      },
    ],
    published_version: 1,
    unpublished_changes: true,
    draft_changed: false,
    skill_updates: ["business-rag-research"],
    skills_pinned: true,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
]) {
  test(`公開中の版に固定したスキルが変わると印を出し、再公開で取り込む (${viewport.name})`, async ({
    page,
    mockApi,
  }, testInfo) => {
    // #1855: 公開の版にスキルの中身を固定する。スキルが変わったら一覧と詳細で知らせる。
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedSkillUpdatedAgent(mockApi);
    await page.goto("/agents");
    const row = page.getByRole("row", { name: /調査の Agent/ });
    await expect(row).toContainText("スキルが更新されています");
    // 下書きの項目は変えていないので「公開していない変更あり」は出さない。
    await expect(row).not.toContainText("公開していない変更あり");
    await expectNoHorizontalOverflow(page);

    await page.goto("/agents?id=agent-1855");
    const banner = page.getByTestId("agent-skills-updated");
    await expect(banner).toContainText("公開中の版（v1）に固定したスキルが、公開の後に変わりました: 業務 RAG 調査");
    await expect(page.getByTestId("agent-skills-not-pinned")).toHaveCount(0);
    const versions = page.getByRole("table", { name: "版の履歴" });
    await expect(versions.getByRole("columnheader", { name: "固定したスキル" })).toBeAttached();
    await expect(versions).toContainText("業務 RAG 調査");
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`agent-skills-updated-${viewport.name}.png`), fullPage: true });

    // 再公開で今のスキルを固定し直し、印が消える。
    const actions = page.getByTestId("agent-object-actions");
    const publish = actions.getByRole("button", { name: "公開" });
    if (await publish.count()) {
      await publish.click();
    } else {
      await page.getByTestId("agent-object-actions-more").click();
      await page.getByRole("menuitem", { name: "公開" }).click();
    }
    const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
    await dialog.getByRole("button", { name: "公開", exact: true }).click();
    await expect(page.getByText("v2 として公開しました")).toBeVisible();
    await expect(page.getByTestId("agent-skills-updated")).toHaveCount(0);
    await expect(page.getByText("スキルが更新されています")).toHaveCount(0);
  });
}

function seedSecondSkillUpdatedAgent(mockApi: MockApi) {
  seedSkillUpdatedAgent(mockApi);
  const first = mockApi.state.agents.find((item) => item.id === "agent-1855");
  if (!first) throw new Error("seed");
  mockApi.state.agents.push({ ...structuredClone(first), id: "agent-1867", name: "経費の Agent" });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
]) {
  test(`スキルの更新を差分で確かめ、選んでまとめて再公開し、取り消せる (${viewport.name})`, async ({
    page,
    mockApi,
  }, testInfo) => {
    // #1867: 何が変わったか → 確かめる（評価への導線）→ まとめて取り込む → 戻せる。
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedSecondSkillUpdatedAgent(mockApi);
    await page.goto("/agents");
    const banner = page.getByTestId("agent-skill-updates-banner");
    await expect(banner).toContainText("スキルの更新があります（2 件の業務 Agent）");
    await banner.getByRole("button", { name: "確かめて取り込む" }).click();
    const sheet = page.getByRole("dialog", { name: "スキルの更新を取り込む" });
    await expect(sheet).toBeVisible();
    const first = sheet.getByTestId("agent-skill-update-agent-1855");
    await expect(first).toContainText("公開中 v1・変わったスキル: 業務 RAG 調査");
    // 評価セットの無い業務 Agent は、そのことを出す。
    await expect(first).toContainText("評価セットがありません");

    // 差分（開いたときだけ取りに行く）。追加・削除は印（+ / −）と色で出す。
    await first.getByTestId("agent-skill-update-diff-agent-1855").click();
    const diff = first.getByTestId("skill-update-diff-agent-1855");
    await expect(diff).toContainText("新しい手順で調べる。");
    await expect(diff).toContainText("前の手順で調べる。");
    await expect(diff).toContainText("+ rag:rag_retrieve_evidence");
    await expect(diff.getByRole("list", { name: "業務 RAG 調査 の指示の差分" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`agent-skill-updates-${viewport.name}.png`) });

    // 1 つを外して、選んだ 1 件を再公開する（確認に対象を出す）。
    await sheet.getByTestId("agent-skill-update-agent-1867").getByRole("checkbox").uncheck();
    await sheet.getByTestId("agent-skill-updates-publish").click();
    const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog", { name: /再公開しますか/ }));
    await expect(dialog).toContainText("調査の Agent（業務 RAG 調査）");
    await expect(dialog).not.toContainText("経費の Agent");
    await dialog.getByRole("button", { name: "選んだ 1 件を再公開", exact: true }).click();
    await expect(page.getByText("1 件の業務 Agent にスキルの更新を取り込みました")).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/agent-skill-updates/publish")?.body).toEqual({
      agent_ids: ["agent-1855"],
      note: "",
    });
    await expect(banner).toContainText("スキルの更新があります（1 件の業務 Agent）");
    const published = mockApi.state.agents.find((item) => item.id === "agent-1855");
    expect(published?.published_version).toBe(2);

    // Toast の「取り消す」で公開中の版を前の版へ戻す（下書きは変えない）。
    await page.getByRole("button", { name: "取り消す", exact: true }).click();
    await expect(page.getByText("1 件の業務 Agent を前の版に戻しました")).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/agent-skill-updates/rollback")?.body).toEqual({
      items: [{ agent_id: "agent-1855", version: 1 }],
    });
    await expect(banner).toContainText("スキルの更新があります（2 件の業務 Agent）");
  });
}

test("業務 Agent の詳細から差分を見て、評価へ移り、スキルの更新だけを公開できる。方針を「常に最新」にできる", async ({
  page,
  mockApi,
}) => {
  seedSkillUpdatedAgent(mockApi);
  await page.goto("/agents?id=agent-1855");
  const banner = page.getByTestId("agent-skills-updated");
  await banner.getByTestId("agent-skill-updates-diff").click();
  await expect(banner.getByTestId("skill-update-diff-agent-1855")).toContainText("新しい手順で調べる。");
  await expect(banner.getByTestId("agent-skill-updates-evaluate")).toHaveAttribute("href", /\/evaluation\?agent=agent-1855$/);

  // スキルの更新の扱い（版の項目。保存して公開で反映する）。
  const policy = page.getByTestId("agent-skill-policy");
  await expect(policy).toContainText("公開の版に固定する（推奨）");
  await policy.click();
  await page.getByRole("option", { name: "常に最新のスキルを使う" }).click();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("Agent を保存しました")).toBeVisible();
  expect(mockApi.lastRequest("PATCH", "/api/agents/agent-1855")?.body).toMatchObject({ skill_policy: "latest" });

  // スキルの更新だけを公開（下書きの変更は公開しない）。
  await banner.getByTestId("agent-publish-skill-updates").click();
  await expect(page.getByText("1 件の業務 Agent にスキルの更新を取り込みました")).toBeVisible();
  await expect(page.getByTestId("agent-skills-updated")).toHaveCount(0);

  // 品質評価で比べる: その業務 Agent を選んだ状態で開く。
  await page.goto("/evaluation?agent=agent-1855");
  await expect(page.locator("#evaluation-agent")).toContainText("調査の Agent");
  await expect(page).toHaveURL(/\/evaluation$/);
});

test("スキルを固定していない公開中の版は、公開で固定することを案内する", async ({ page, mockApi }) => {
  seedSkillUpdatedAgent(mockApi);
  const agent = mockApi.state.agents.find((item) => item.id === "agent-1855");
  if (!agent) throw new Error("seed");
  Object.assign(agent, { skills_pinned: false, skill_updates: [], unpublished_changes: false });
  (agent.versions as Record<string, unknown>[])[0].skills = null;
  await page.goto("/agents?id=agent-1855");
  await expect(page.getByTestId("agent-skills-not-pinned")).toContainText("スキルの中身を固定していません");
  await expect(page.getByRole("table", { name: "版の履歴" })).toContainText("固定なし（今のスキル）");
});

test("一覧に公開の状態を出す", async ({ page, mockApi }) => {
  seedDraftAgent(mockApi);
  await page.goto("/agents");
  const table = page.getByRole("table");
  await expect(table.getByRole("row", { name: /経理の Agent/ }).getByText("未公開")).toBeVisible();
  await expect(table.getByRole("row", { name: /資料・データ横断アシスタント/ }).getByText("公開中 v1")).toBeVisible();
});

test("公開していない Agent は、管理者が「下書きで実行」にしたときだけ Run で選べる", async ({ page, mockApi }) => {
  seedDraftAgent(mockApi);
  const bodies: unknown[] = [];
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    bodies.push(route.request().postDataJSON());
    await route.fulfill({
      json: {
        data: {
          id: "run-draft-770",
          goal: "試す",
          agent_id: "agent-770",
          runtime_id: "builtin",
          status: "queued",
          steps: [],
          events: [],
          approvals: [],
          artifacts: [],
          pending_tool_calls: [],
          metadata: { agent_version: "draft" },
          created_at: MOCK_NOW,
          updated_at: MOCK_NOW,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.goto("/runs?id=new");
  const agentSelect = page.locator("#run-agent");
  await agentSelect.click();
  await expect(page.getByRole("option", { name: "経理の Agent" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  // 「下書きで実行」の補足の説明は常設せず、ラベルの横の info アイコンを押すと出す（#901）。
  const draftHint = page.getByTestId("run-draft-hint");
  await expect(draftHint).toBeHidden();
  await page.getByRole("button", { name: "下書きで実行の説明", exact: true }).click();
  await expect(draftHint).toBeVisible();
  await expect(draftHint).toHaveText("公開前の下書きの内容で試します（Agent 管理の権限）。");
  await page.keyboard.press("Escape");
  await expect(draftHint).toBeHidden();

  await page.getByTestId("run-draft").click();
  await agentSelect.click();
  await page.getByRole("option", { name: "経理の Agent" }).click();
  await page.locator("#run-goal").fill("試す");
  await page.getByRole("button", { name: "実行を作成" }).click();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toEqual({ goal: "試す", agent_id: "agent-770", draft: true });
});

test("版の公開者は表示名で出し、ログイン ID・削除済みの利用者の ID を出さない（#1696）", async ({ page, mockApi }, testInfo) => {
  seedDraftAgent(mockApi);
  const agent = mockApi.state.agents.find((item) => item.id === "agent-770")!;
  agent.versions = [
    { version: 1, name: "経理の Agent", description: "", instructions: "v1", skill_ids: [], model_id: "", note: "", published_at: MOCK_NOW, published_by: "system_admin" },
    { version: 2, name: "経理の Agent", description: "", instructions: "v2", skill_ids: [], model_id: "", note: "", published_at: MOCK_NOW, published_by: "gone.user" },
  ];
  agent.published_version = 2;
  agent.unpublished_changes = false;
  await page.goto("/agents?id=agent-770");

  const versions = page.getByRole("table", { name: "版の履歴" });
  await expect(versions.getByRole("row").filter({ hasText: "v1" })).toContainText("システム管理者");
  await expect(versions.getByRole("row").filter({ hasText: "v2" })).toContainText("（削除済みのユーザー）");
  await expect(versions).not.toContainText("system_admin");
  await expect(versions).not.toContainText("gone.user");
  await versions.screenshot({ path: testInfo.outputPath("agent-versions-published-by.png") });
});

function seedAgentWithVersions(mockApi: MockApi, count: number, publishedVersion: number) {
  const versions = Array.from({ length: count }, (_, index) => ({
    version: index + 1,
    name: "Agent A",
    description: "",
    instructions: `v${index + 1}`,
    skill_ids: [],
    model_id: "",
    data_scopes: {},
    note: `メモ ${index + 1}`,
    published_at: MOCK_NOW,
    published_by: "local",
    skills: [],
  }));
  mockApi.state.agents.push({
    id: "agent-2356",
    name: "Agent A",
    description: "",
    instructions: "下書き",
    skill_ids: [],
    model_id: "",
    enabled: true,
    source: "runtime",
    versioned: true,
    versions,
    published_version: publishedVersion,
    unpublished_changes: true,
    draft_changed: true,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
]) {
  test(`版の履歴は直近 5 版を残し、公開すると古い版から消える（#2356） (${viewport.name})`, async ({
    page,
    mockApi,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedAgentWithVersions(mockApi, 5, 5);
    await page.goto("/agents?id=agent-2356");
    await expect(page.getByRole("heading", { name: "Agent A", level: 1 })).toBeVisible();

    // 上限は API の値（max_published_versions）で案内する。件数は表の上の 1 か所。
    await expect(page.getByText("直近 5 版を保存します（公開中の版は常に残ります）。", { exact: false })).toBeVisible();
    const toolbar = page.getByTestId("agent-version-toolbar");
    await expect(toolbar).toContainText("全 5 件");
    const versions = page.getByRole("table", { name: "版の履歴" });
    await expect(versions.getByRole("rowheader", { name: /^v1/ })).toBeAttached();

    const actions = page.getByTestId("agent-object-actions");
    const publish = actions.getByRole("button", { name: "公開" });
    if (await publish.count()) {
      await publish.click();
    } else {
      await page.getByTestId("agent-object-actions-more").click();
      await page.getByRole("menuitem", { name: "公開" }).click();
    }
    const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
    await dialog.getByRole("button", { name: "公開", exact: true }).click();
    await expect(page.getByText("v6 として公開しました")).toBeVisible();

    // v1 は消え、番号は振り直さない（v2〜v6）。公開中のバッジは v6 の行。
    await expect(versions.getByRole("rowheader", { name: /^v1/ })).toHaveCount(0);
    for (const number of [2, 3, 4, 5, 6]) {
      await expect(versions.getByRole("rowheader", { name: new RegExp(`^v${number}`) })).toBeAttached();
    }
    await expect(versions.getByRole("row").filter({ hasText: "v6" })).toContainText("公開中");
    await expect(toolbar).toContainText("全 5 件");
    await expectNoHorizontalOverflow(page);
    await toolbar.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`agent-versions-retention-${viewport.name}.png`) });
  });
}


test("スキル更新の標準ページングで選択を保ち、表示件数とページを復元できる", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  seedSkillUpdatedAgent(mockApi);
  const base = mockApi.state.agents.find((item) => item.id === "agent-1855")!;
  const agents = Array.from({ length: 31 }, (_, index) => ({
    ...structuredClone(base), id: `paging-agent-${index}`, name: `検証 Agent ${index}`,
  }));
  mockApi.state.agents.splice(0, mockApi.state.agents.length, ...agents);
  await page.goto("/agents");
  await page.getByTestId("agent-skill-updates-open").click();
  const sheet = page.getByRole("dialog", { name: "スキルの更新を取り込む" });
  const pager = sheet.getByTestId("agent-skill-updates-pagination");
  const cards = sheet.locator("li[data-testid^='agent-skill-update-']");
  await expect(cards).toHaveCount(25);
  await expect(sheet.getByTestId("agent-skill-updates-toolbar")).toContainText("全 31 件");
  await expect(pager).toContainText("1–25 / 31 件");
  await sheet.getByTestId("agent-skill-update-paging-agent-0").getByRole("checkbox").uncheck();
  await jumpToPage(pager, 2);
  await expect(cards).toHaveCount(6);
  await sheet.getByTestId("agent-skill-update-paging-agent-30").getByRole("checkbox").uncheck();
  await expect(sheet.getByTestId("agent-skill-updates-publish")).toHaveText("選んだ 29 件を再公開");
  await pager.getByRole("button", { name: "前へ", exact: true }).click();
  await expect(sheet.getByTestId("agent-skill-update-paging-agent-0").getByRole("checkbox")).not.toBeChecked();
  await chooseSelectFieldOption(pager.getByRole("combobox"), "10");
  await expect(cards).toHaveCount(10);
  await jumpToPage(pager, 4);
  await expect(cards).toHaveCount(1);
  await sheet.getByRole("button", { name: "閉じる", exact: true }).click();
  await page.reload();
  await page.getByTestId("agent-skill-updates-open").click();
  await expect(pager).toContainText("31–31 / 31 件");
  await expect(pager.getByRole("combobox")).toHaveAttribute("data-value", "10");
  await sheet.getByRole("button", { name: "選択を外す", exact: true }).click();
  await sheet.getByTestId("agent-skill-update-paging-agent-30").getByRole("checkbox").check();
  await jumpToPage(pager, 1);
  await sheet.getByTestId("agent-skill-update-paging-agent-0").getByRole("checkbox").check();
  await sheet.getByTestId("agent-skill-updates-publish").click();
  const confirm = page.getByRole("alertdialog");
  await confirm.getByRole("button", { name: "選んだ 2 件を再公開", exact: true }).click();
  await expect(sheet).toBeHidden();
  expect(mockApi.lastRequest("POST", "/api/agent-skill-updates/publish")?.body).toEqual({
    agent_ids: ["paging-agent-0", "paging-agent-30"], note: "",
  });
  await page.getByTestId("agent-skill-updates-open").click();
  await sheet.getByRole("button", { name: "すべて選ぶ", exact: true }).click();
  await expect(sheet.getByTestId("agent-skill-updates-publish")).toHaveText("選んだ 29 件を再公開");
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("skill-updates-paging.png"), fullPage: true });
});
