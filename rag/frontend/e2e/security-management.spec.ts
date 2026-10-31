import { expect, test, type Page, type Route } from "./fixtures/test";
import { findFloatingFormActionBars } from "../../../platform/packages/ui/e2e/layout-checks";

import {
  ALL_PERMISSION_CODES,
  apiEnvelope,
  expectNoPageOverflow,
  mockAuthUser,
  mockDatabaseReady,
} from "./_helpers";

// ユーザー管理・ロール管理（3製品共通の画面）と RAG の権限管理（#214）。
// 権限管理は機能権限に加えて、利用できる検索・回答プロファイル・ナレッジベースを PUT /api/security/roles/{id}/access で保存する。

const SYSTEM_ADMIN_ROLE = {
  role_id: "role-admin",
  role_code: "SYSTEM_ADMIN",
  display_name: "システム管理者",
  description: "すべての権限",
  is_built_in: true,
  archived: false,
  version: 1,
  permissions: [],
  search_answer_profile_ids: [],
  knowledge_base_ids: [],
};

const HR_ROLE = {
  role_id: "role-hr",
  role_code: "HR_USER",
  display_name: "人事利用者",
  description: "人事 FAQ を使う",
  is_built_in: false,
  archived: false,
  version: 4,
  permissions: ["menu.search", "menu.chat"],
  search_answer_profile_ids: [],
  knowledge_base_ids: [],
};

const USERS = [
  {
    user_uuid: "u-admin",
    login_user_id: "admin",
    display_name: "管理 太郎",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: ["role-admin"],
    assigned_roles: [
      { role_id: "role-admin", role_code: "SYSTEM_ADMIN", display_name: "システム管理者", is_built_in: true, archived: false },
    ],
    is_bootstrap_admin: true,
  },
  {
    user_uuid: "u-hr",
    login_user_id: "hr.user",
    display_name: "人事 花子",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 2,
    role_ids: ["role-hr"],
    assigned_roles: [
      { role_id: "role-hr", role_code: "HR_USER", display_name: "人事利用者", is_built_in: false, archived: false },
    ],
    is_bootstrap_admin: false,
  },
];

const PERMISSION_CATALOG = [
  { code: "menu.search", group: "AI 活用", label: "RAG 検索", description: "RAG 検索を表示します。", implies: [] },
  { code: "menu.chat", group: "AI 活用", label: "チャット", description: "チャットを表示します。", implies: [] },
  {
    code: "menu.knowledge_bases",
    group: "ナレッジ構築",
    label: "ナレッジベース",
    description: "ナレッジベースを表示します。",
    implies: [],
  },
  {
    code: "rag.knowledge_bases.manage",
    group: "管理権限",
    label: "ナレッジベース管理",
    description: "ナレッジベースの作成・アーカイブと、すべてのナレッジベースの利用ができます。",
    implies: ["menu.knowledge_bases"],
  },
];

const ACCESS_TARGETS: AccessTargetsFixture = {
  search_answer_profiles: [
    { id: "bv-hr", name: "人事 FAQ", status: "ACTIVE", description: "人事規程の問い合わせ" },
    { id: "bv-old", name: "旧経理", status: "ARCHIVED", description: null },
  ],
  knowledge_bases: [
    { id: "kb-hr", name: "人事規程", status: "ACTIVE", description: null },
    { id: "kb-finance", name: "経理規程", status: "ACTIVE", description: null },
  ],
};

interface AccessTargetsFixture {
  search_answer_profiles: { id: string; name: string; status: string; description: string | null }[];
  knowledge_bases: { id: string; name: string; status: string; description: string | null }[];
}

/**
 * `GET /api/security/access-targets/{search-answer-profiles,knowledge-bases}` を backend と同じ規則で返す（#608）:
 * `q`（名前・説明の部分一致）・`ids`・`limit` / `offset` で絞り、Page（items / total）を返す。
 */
function fulfillAccessTargets(route: Route, fixture: AccessTargetsFixture) {
  const url = new URL(route.request().url());
  const kind = url.pathname.split("/").pop();
  const all = kind === "knowledge-bases" ? fixture.knowledge_bases : fixture.search_answer_profiles;
  const q = (url.searchParams.get("q") ?? "").toLowerCase();
  const ids = url.searchParams.getAll("ids");
  const limit = Number(url.searchParams.get("limit") ?? "50");
  const offset = Number(url.searchParams.get("offset") ?? "0");
  const matched = all.filter(
    (item) =>
      (ids.length === 0 || ids.includes(item.id)) &&
      (!q || `${item.name} ${item.description ?? ""}`.toLowerCase().includes(q))
  );
  return route.fulfill({
    json: apiEnvelope({
      items: matched.slice(offset, offset + limit),
      total: matched.length,
      limit,
      offset,
      has_next: offset + limit < matched.length,
    }),
  });
}

async function mockSecurityApi(page: Page) {
  const saved: Record<string, unknown>[] = [];
  const accessTargetRequests: URL[] = [];
  let roleListCalls = 0;
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 404, json: { data: null, error_messages: ["not mocked"], warning_messages: [] } })
  );
  await mockDatabaseReady(page);
  await mockAuthUser(page, {
    login_user_id: "admin",
    display_name: "管理 太郎",
    role_codes: ["SYSTEM_ADMIN"],
    role_display_names: ["システム管理者"],
    is_system_admin: true,
    permissions: ALL_PERMISSION_CODES,
    allowed_search_answer_profile_ids: null,
    allowed_knowledge_base_ids: null,
  });
  await page.route("**/api/security/users**", (route) => route.fulfill({ json: apiEnvelope(USERS) }));
  await page.route("**/api/security/roles**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/security/roles/role-hr/access") {
      expect(request.method()).toBe("PUT");
      const body = request.postDataJSON() as Record<string, unknown>;
      saved.push(body);
      await route.fulfill({
        json: apiEnvelope({
          ...HR_ROLE,
          version: HR_ROLE.version + 1,
          permissions: body.permissions,
          search_answer_profile_ids: body.search_answer_profile_ids,
          knowledge_base_ids: body.knowledge_base_ids,
        }),
      });
      return;
    }
    roleListCalls += 1;
    await route.fulfill({ json: apiEnvelope([SYSTEM_ADMIN_ROLE, HR_ROLE]) });
  });
  await page.route("**/api/security/permissions", (route) => route.fulfill({ json: apiEnvelope(PERMISSION_CATALOG) }));
  await page.route("**/api/security/access-targets/**", (route) => {
    accessTargetRequests.push(new URL(route.request().url()));
    return fulfillAccessTargets(route, ACCESS_TARGETS);
  });
  return { saved, accessTargetRequests, roleListCalls: () => roleListCalls };
}

test("ユーザー管理とロール管理を開け、ロールの詳細から権限管理へ移れる", async ({ page }) => {
  await mockSecurityApi(page);

  await page.goto("/settings/security/users");
  await expect(page.getByRole("heading", { name: "ユーザー管理", level: 1 })).toBeVisible();
  const users = page.getByTestId("security-users-grid");
  await expect(users.getByText("人事 花子")).toBeVisible();
  await expect(users.getByText("管理 太郎")).toBeVisible();
  await expectNoPageOverflow(page);

  await page.goto("/settings/security/roles");
  await expect(page.getByRole("heading", { name: "ロール管理", level: 1 })).toBeVisible();
  await page.getByTestId("security-roles-grid").locator("tbody tr").filter({ hasText: "人事利用者" }).locator("td").first().click();
  const summary = page.getByTestId("security-roles-permission-summary");
  await expect(summary).toContainText("付与している機能権限: 2 件");
  await summary.getByTestId("security-roles-open-permissions").click();

  await expect(page).toHaveURL(/\/settings\/security\/permissions\?role=role-hr$/);
  await expect(page.getByRole("heading", { name: "権限管理", level: 1 })).toBeVisible();
  await expectNoPageOverflow(page);
});

test("権限管理で検索・回答プロファイルと KB を選んで保存し、KB 管理権限では KB が全件対象になる", async ({ page }) => {
  const api = await mockSecurityApi(page);

  await page.goto("/settings/security/permissions?role=role-hr");
  await expect(page.getByRole("heading", { name: "権限管理", level: 1 })).toBeVisible();
  // 候補は全件を読まない（#608）。一覧では、ロールに選択済みの対象の名前だけを ids で読む（このロールは未選択）。
  await expect(page.getByTestId("security-permissions-grid")).toContainText("人事利用者");
  expect(api.accessTargetRequests).toEqual([]);

  await page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集" }).click();
  // 編集を開くと、対象ごとに 1 ページ目（50 件）を読む。
  await expect
    .poll(() => api.accessTargetRequests.map((url) => `${url.pathname}?${url.searchParams.toString()}`).sort())
    .toEqual(
      expect.arrayContaining([
        "/api/security/access-targets/search-answer-profiles?limit=50&offset=0",
        "/api/security/access-targets/knowledge-bases?limit=50&offset=0",
      ])
    );

  // 機能の一覧は左のナビと同じグループ・並び順・名前（Issue 567）。backend のカタログが RAG 検索 → チャットの
  // 順でも、ナビの順（チャット → RAG 検索）に並べ、ナビに無い capability は後ろに置く。
  // チャット・RAG 検索のグループはナビの先頭のセクション「AI 活用」（#860 で 3 製品の名前にそろえた）。
  const featureList = page.locator('form[aria-labelledby="security-permissions-form-heading"] fieldset').first();
  await expect(featureList.locator("h3")).toHaveText(["AI 活用", "ナレッジ構築", "管理権限"]);
  await expect(featureList.getByRole("checkbox").nth(0)).toHaveAccessibleName(/^チャット/);
  await expect(featureList.getByRole("checkbox").nth(1)).toHaveAccessibleName(/^RAG 検索/);

  const views = page.getByTestId("security-roles-search-answer-profile-access-list");
  const bases = page.getByTestId("security-roles-knowledge-base-access-list");
  await expect(views).toHaveAccessibleName("利用できる検索・回答プロファイル");
  await expect(bases).toHaveAccessibleName("利用できるナレッジベース");
  // アーカイブ済みの対象は状態を示す。
  await expect(views.getByText("アーカイブ済み")).toBeVisible();

  // 候補は共通の ListPicker の選択肢（role=option・aria-checked。#600）。
  await views.getByRole("option", { name: /人事 FAQ/ }).check();
  await bases.getByRole("option", { name: /人事規程/ }).check();
  await page.getByRole("checkbox", { name: /^チャット/ }).uncheck();
  await expectNoPageOverflow(page);
  await page.getByTestId("security-permissions-submit").click();

  await expect.poll(() => api.saved.length).toBe(1);
  expect(api.saved[0]).toEqual({
    version: 4,
    permissions: ["menu.search"],
    search_answer_profile_ids: ["bv-hr"],
    knowledge_base_ids: ["kb-hr"],
  });

  // 保存後も編集を続けられる。ナレッジベース管理を付けると KB は全件が対象になり、
  // 個別選択は出さずに空の一覧を送る。
  await expect(page.getByText("変更を保存しました。")).toBeVisible();
  await page.getByRole("checkbox", { name: /^ナレッジベース管理/ }).check();
  await expect(page.getByText("ナレッジベース管理の権限により、すべてのナレッジベースを利用できます。")).toBeVisible();
  await expect(page.getByTestId("security-roles-knowledge-base-access-list")).toHaveCount(0);
  await page.getByTestId("security-permissions-submit").click();

  await expect.poll(() => api.saved.length).toBe(2);
  expect(api.saved[1]).toMatchObject({
    version: 5,
    search_answer_profile_ids: ["bv-hr"],
    knowledge_base_ids: [],
  });
  // implies で付く menu.knowledge_bases は送らない（backend が展開する）。
  expect((api.saved[1].permissions as string[]).sort()).toEqual(
    ["menu.search", "rag.knowledge_bases.manage"].sort()
  );
});

// #521: 検索・回答プロファイル・KB の候補の行は名前と説明を出し、内部の ID は出さない。長い説明は 1 行で省略し
// （共通の ListPicker の行。#600）、行の高さをそろえて重ねない（375px でも）。
const LONG_DESCRIPTION =
  "人事規程・就業規則・勤怠管理・福利厚生・評価制度・出張旅費・経費精算・情報セキュリティに関する社内の問い合わせにまとめて回答するための検索・回答プロファイルです。" +
  "説明が長い場合は 2 行で省略し、全文は title で確かめられることを確かめます。";
const hexId = (index: number) => index.toString(16).padStart(32, "0");
const MANY_TARGETS: AccessTargetsFixture = {
  search_answer_profiles: Array.from({ length: 8 }, (_, index) => ({
    id: hexId(index + 1),
    name:
      index === 1
        ? "人事と総務と経理をまとめて扱う全社共通の問い合わせ窓口の検索・回答プロファイル"
        : `検索・回答プロファイル ${index + 1}`,
    status: index === 3 ? "ARCHIVED" : "ACTIVE",
    description: index % 3 === 0 ? null : index % 3 === 1 ? LONG_DESCRIPTION : "短い説明",
  })),
  knowledge_bases: Array.from({ length: 8 }, (_, index) => ({
    id: hexId(index + 101),
    name: `ナレッジベース ${index + 1}`,
    status: index === 2 ? "ARCHIVED" : "ACTIVE",
    description: index % 2 === 0 ? LONG_DESCRIPTION : "規程集",
  })),
};

test(`権限管理の検索・回答プロファイル・KB の候補は名前と説明を出し、ID を出さず、行が重ならない`, async ({
  page,
}) => {
  await mockSecurityApi(page);
  await page.route("**/api/security/access-targets/**", (route) => fulfillAccessTargets(route, MANY_TARGETS));

  await page.goto("/settings/security/permissions?role=role-hr");
  await page
    .getByTestId("security-permissions-detail-actions")
    .getByRole("button", { name: "権限を編集" })
    .click();

  for (const [key, targets] of [
    ["search-answer-profile-access", MANY_TARGETS.search_answer_profiles],
    ["knowledge-base-access", MANY_TARGETS.knowledge_bases],
  ] as const) {
    const list = page.getByTestId(`security-roles-${key}-list`);
    await expect(list).toBeVisible();
    // 内部の ID（32 桁）は出さない。名前・説明・アーカイブ済みは出す。
    for (const target of targets) await expect(list).not.toContainText(target.id);
    const longDescription = list.getByText(LONG_DESCRIPTION).first();
    await expect(longDescription).toBeVisible();
    await expect(list.getByText("アーカイブ済み")).toBeVisible();
    // 省略した全文は title で確かめられ、選択肢の名前は全文のまま。
    await expect(longDescription).toHaveAttribute("title", LONG_DESCRIPTION);
    await expect(list.getByRole("option", { name: new RegExp(targets[1].name) })).toHaveCount(1);

    const boxes = await list
      .getByRole("option")
      .evaluateAll((rows) =>
        rows.map((row) => {
          const { top, bottom, left, right, height } = row.getBoundingClientRect();
          return { top, bottom, left, right, height };
        })
      );
    expect(boxes).toHaveLength(targets.length);
    // 行の高さはそろい（説明の有無・長さによらない）、どの 2 行も重ならない。
    const heights = boxes.map((box) => Math.round(box.height));
    expect(Math.max(...heights) - Math.min(...heights)).toBeLessThanOrEqual(1);
    for (const [index, a] of boxes.entries()) {
      for (const b of boxes.slice(index + 1)) {
        const overlapX = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const overlapY = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        expect(overlapX > 0.5 && overlapY > 0.5).toBe(false);
      }
    }
    // 長い説明は 1 行で省略する。
    const clamped = await longDescription.evaluate((element) => ({
      scroll: element.scrollHeight,
      client: element.clientHeight,
      lineHeight: Number.parseFloat(getComputedStyle(element).lineHeight),
    }));
    const truncated = await longDescription.evaluate((element) => ({
      scroll: element.scrollWidth,
      client: element.clientWidth,
    }));
    expect(clamped.client).toBeLessThanOrEqual(Math.ceil(clamped.lineHeight) + 1);
    expect(truncated.scroll).toBeGreaterThan(truncated.client);
  }
  await expectNoPageOverflow(page);
});

// #608: 候補が大量（数千件）でも全件を読まない。検索はサーバーの q で絞り、続きは「さらに読み込む」で 50 件ずつ足す。
// 保存済みの対象の名前は ids で読み、一覧の詳細と「選択中だけ表示」に出す。
const LARGE_TARGETS: AccessTargetsFixture = {
  search_answer_profiles: [],
  knowledge_bases: Array.from({ length: 3000 }, (_, index) => ({
    id: `kb-${String(index + 1).padStart(4, "0")}`,
    name: `ナレッジベース ${String(index + 1).padStart(4, "0")}`,
    status: "ACTIVE",
    description: index % 2 === 0 ? "規程集" : null,
  })),
};

// desktop（1440px）と 375px は playwright.config の projects で確かめる。
test("権限管理の対象が大量でもサーバー側で検索し、50 件ずつ読む", async ({ page }) => {
  const api = await mockSecurityApi(page);
  const requests: URL[] = [];
  await page.route("**/api/security/roles**", (route) =>
    route.request().method() === "GET"
      ? route.fulfill({ json: apiEnvelope([SYSTEM_ADMIN_ROLE, { ...HR_ROLE, knowledge_base_ids: ["kb-2999"] }]) })
      : route.fallback()
  );
  await page.route("**/api/security/access-targets/**", (route) => {
    requests.push(new URL(route.request().url()));
    return fulfillAccessTargets(route, LARGE_TARGETS);
  });

  await page.goto("/settings/security/permissions?role=role-hr");
  // 一覧では選択済みの対象の名前だけを ids で読み、詳細に出す。
  await expect(page.getByText("ナレッジベース 2999")).toBeVisible();
  expect(requests.map((url) => url.searchParams.getAll("ids"))).toContainEqual(["kb-2999"]);
  expect(requests.every((url) => url.searchParams.getAll("ids").length > 0)).toBe(true);

  await page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集" }).click();
  const bases = page.getByTestId("security-roles-knowledge-base-access-list");
  await expect(bases.getByRole("option")).toHaveCount(50);
  await expect(bases).toContainText("50 / 3,000 件、選択 1 件");
  await bases.getByRole("button", { name: "さらに読み込む" }).click();
  await expect(bases).toContainText("100 / 3,000 件、選択 1 件");
  expect(requests.at(-1)?.searchParams.get("offset")).toBe("50");

  // 検索はサーバーの q で絞る（1 ページ目から読み直す）。
  await bases.getByRole("searchbox").fill("2999");
  await expect(bases.getByRole("option")).toHaveCount(1);
  await expect(bases.getByRole("option", { name: /ナレッジベース 2999/ })).toHaveAttribute("aria-checked", "true");
  const searched = requests.at(-1);
  expect(searched?.searchParams.get("q")).toBe("2999");
  expect(searched?.searchParams.get("offset")).toBe("0");

  // 検索語を消すと 1 ページ目に戻り、保存済みの選択は「選択中だけ表示」で確かめられる（読み込んだ範囲の外でも）。
  await bases.getByRole("button", { name: "検索語をクリア" }).first().click();
  await expect(bases.getByRole("option")).toHaveCount(50);
  await bases.getByRole("button", { name: /選択中だけ表示/ }).click();
  await expect(bases.getByRole("option")).toHaveCount(1);
  await expect(bases.getByRole("option", { name: /ナレッジベース 2999/ })).toBeVisible();

  await page.getByTestId("security-permissions-submit").click();
  await expect.poll(() => api.saved.length).toBe(1);
  expect(api.saved[0]).toMatchObject({ knowledge_base_ids: ["kb-2999"] });
  await expectNoPageOverflow(page);
});

// #1786: 詳細・作成・編集の画面の確定（作成・保存）は、フォームの末尾の左（入力欄の左端）に置き、その右に「変更を破棄」。
// PageHeader の右上には置かない。一覧の「新規作成」（作成の画面を開く）はヘッダーのまま。
test("ユーザー・ロールの作成と編集の確定は、フォームの末尾の左に「作成」「保存」→「変更を破棄」の順で出す", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockSecurityApi(page);
  const headerActions = page.locator("[data-page-header-actions]");

  /** 確定の行がフォームの最後の欄の下にあり、primary の左端が入力欄の左端にそろうこと。 */
  async function expectConfirmAtFormEnd(groupName: string, firstField: string, lastField: string, labels: string[]) {
    const actions = page.getByRole("group", { name: groupName });
    await expect(actions).toBeVisible();
    await expect(actions.getByRole("button")).toHaveText(labels);
    // ヘッダーの右上には確定を置かない。
    for (const label of labels) {
      await expect(headerActions.getByRole("button", { name: label, exact: true })).toHaveCount(0);
    }
    // フォームの末尾まで見えるようにする（確定の行は画面の下端に貼り付けない。#2715）。
    await page.locator(lastField).evaluate((element) => element.scrollIntoView({ block: "center" }));
    const primary = actions.getByRole("button", { name: labels[0], exact: true });
    const discard = actions.getByRole("button", { name: labels[1], exact: true });
    const [primaryBox, discardBox, fieldBox, lastBox] = await Promise.all([
      primary.boundingBox(),
      discard.boundingBox(),
      page.getByLabel(firstField, { exact: false }).first().boundingBox(),
      page.locator(lastField).boundingBox(),
    ]);
    // primary の左端は入力欄の左端（GOV.UK / Carbon のフォームと同じく左寄せ）。
    expect(Math.abs(primaryBox!.x - fieldBox!.x)).toBeLessThanOrEqual(1);
    // 変更を破棄は primary の右の同じ行。sm（640px）未満は言語資源の画面などのフォームの末尾の行と同じく、
    // primary の下に全幅で積む（#2715）。
    if (page.viewportSize()!.width >= 640) {
      expect(discardBox!.x).toBeGreaterThan(primaryBox!.x + primaryBox!.width - 1);
      expect(Math.abs(discardBox!.y - primaryBox!.y)).toBeLessThanOrEqual(1);
    } else {
      expect(discardBox!.y).toBeGreaterThanOrEqual(primaryBox!.y + primaryBox!.height);
      expect(Math.abs(discardBox!.x - primaryBox!.x)).toBeLessThanOrEqual(1);
    }
    // 確定の行は最後の欄の下（フォームの末尾）。
    expect(primaryBox!.y).toBeGreaterThanOrEqual(lastBox!.y + lastBox!.height);
    // 上から下までスクロールしても、行は画面に貼り付かず PageHeader の上に描かれない（#2715）。
    expect(await findFloatingFormActionBars(page)).toEqual([]);
    // 変更が無いときの変更を破棄は disabled。
    await expect(discard).toBeDisabled();
    return actions;
  }

  await page.goto("/settings/security/users");
  await expect(page.getByTestId("security-users-grid").getByText("人事 花子")).toBeVisible();
  await headerActions.getByRole("button", { name: "新規作成", exact: true }).click();
  let actions = await expectConfirmAtFormEnd("ユーザーの確定", "ログインユーザーID", "[role=radiogroup][aria-labelledby=security-users-role-legend]", ["作成", "変更を破棄"]);
  // 入力すると変更を破棄が押せ、押すと開いたときの値に戻す。
  const displayName = page.getByRole("textbox", { name: "表示名" });
  await displayName.fill("採用 次郎");
  await actions.getByRole("button", { name: "変更を破棄", exact: true }).click();
  await expect(displayName).toHaveValue("");

  await page.goto("/settings/security/users");
  await page.getByTestId("security-users-grid").getByText("人事 花子").click();
  await page.getByTestId("security-users-detail-actions").getByRole("button", { name: "編集", exact: true }).click();
  actions = await expectConfirmAtFormEnd("ユーザーの確定", "ログインユーザーID", "[role=radiogroup][aria-labelledby=security-users-role-legend]", ["保存", "変更を破棄"]);

  await page.goto("/settings/security/roles");
  await expect(page.getByTestId("security-roles-grid")).toContainText("人事利用者");
  await headerActions.getByRole("button", { name: "新規作成", exact: true }).click();
  await expectConfirmAtFormEnd("ロールの確定", "ロールコード", "#security-role-description", ["作成", "変更を破棄"]);
  await expectNoPageOverflow(page);
});


test("管理画面の読込表示は対象を短く示して一覧の幅に収まる", { tag: "@responsive" }, async ({ page }) => {
  await mockSecurityApi(page);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/security/**", async route => { await pending; await route.fallback(); });
  try {
    for (const [kind, label] of [["users", "ユーザーを読込中"], ["roles", "ロールを読込中"], ["permissions", "権限を読込中"]]) {
      await page.goto(`/settings/security/${kind}`);
      const loading = page.getByTestId(`security-${kind}-loading`);
      await expect(loading.locator("[data-processing-label]")).toHaveText(label);
      await expect(loading.locator("[data-processing-label]")).toBeVisible();
      await expect(loading).toContainText("経過時間");
      const size = await loading.locator("[data-processing-label]").evaluate(node => ({height: node.getBoundingClientRect().height, line: parseFloat(getComputedStyle(node).lineHeight)}));
      expect(size.height).toBeLessThanOrEqual(size.line * 2 + 1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    }
  } finally { release(); }
});
