import { expectGrowingList } from "./_list-viewport";
import { expect, type Page, test } from "./fixtures/test";
import { findClippedPlaceholders, findFloatingFormActionBars, jumpToPage } from "../../../platform/packages/ui/e2e/layout-checks";
import {
  expectNoPageOverflow,
  expectSearchablePanelFits,
  mockDatabaseReady,
  mockLocalAuth,
  selectSearchAnswerProfile,
} from "./_helpers";

const GLOBAL_QUERY_FIELDS = [
  "query_strategy", "answer_flow", "neighbor_child_count", "rerank_enabled", "auto_field_filter_enabled",
  "graph_expansion_enabled", "graph_expansion_max_chunks", "screen_linking_enabled", "request_coverage_retrieval_enabled",
  "guide_step_retrieval_enabled", "guide_step_max_queries", "guide_step_deadline_seconds",
];

// #3719: 空・単一ページでも表示し、4一覧のページ操作と作業状態を確かめる。
for (const [tab, paginationId, kind] of [
  ["登録済みQ&A", "approved-faq-pagination", "faq"],
  ["用語・同義語", "runtime-knowledge-terms-pagination", "terms"],
  ["確認・回答ルール", "runtime-knowledge-rules-pagination", "rules"],
  ["業務ガイド", "support-guides-pagination", "guides"],
]) {
  test(`知識一覧のページング: ${tab}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await mockDefaultSearchAnswerProfile(page, () => {});
    let count = 0;
    const rows = () => Array.from({ length: count }, (_, index) => {
      const name = `項目 ${String(index + 1).padStart(3, "0")}`;
      return {
        id: `item-${index}`, question: name, answer: "合成の回答", alternate_questions: [],
        term: name, aliases: [], description: "合成の説明", title: name, content: "合成の指示",
        triggers: [], status: kind === "guides" ? "active" : "approved",
        guide_id: `guide-${index}`, published_revision: null, draft_revision: 1,
        has_unpublished_changes: true, updated_at: "2026-10-01T00:00:00Z", updated_by: null,
      };
    });
    const response = (data: unknown) => ({ json: { data, error_messages: [], warning_messages: [] } });
    await page.route("**/api/search-answer-profiles/bv-default/approved-faq", (route) =>
      route.fulfill(response({ records: kind === "faq" ? rows() : [], enabled: true })));
    await page.route("**/api/search-answer-profiles/bv-default/runtime-knowledge", (route) =>
      route.fulfill(response({ terms: kind === "terms" ? rows() : [], rules: kind === "rules" ? rows() : [] })));
    await page.route("**/api/search-answer-profiles/bv-default/support-guides**", (route) =>
      route.fulfill(response(rows())));
    const open = async () => { await page.getByRole("tab", { name: tab, exact: true }).click(); };
    await page.goto("/search-answer-profiles?id=bv-default");
    await open();
    const pagination = page.getByTestId(paginationId);
    await expect(pagination).toContainText("0–0 / 0 件");
    await expect(pagination.getByRole("button", { name: "前へ", exact: true })).toBeDisabled();
    await expect(pagination.getByRole("button", { name: "次へ", exact: true })).toBeDisabled();
    await pagination.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`${kind}-empty.png`) });

    count = 61;
    await page.reload();
    await open();
    const panel = page.getByRole("tabpanel");
    await expect(pagination).toContainText("1–25 / 61 件");
    await expect(panel.getByRole("rowheader")).toHaveCount(25);
    await expect(panel.getByRole("rowheader").first()).toHaveText("項目 001");
    await jumpToPage(pagination, 3);
    await expect(pagination).toContainText("51–61 / 61 件");
    await expect(panel.getByRole("rowheader")).toHaveCount(11);
    await expect(panel.getByRole("rowheader").first()).toHaveText("項目 051");
    await expect(pagination.getByRole("button", { name: "次へ", exact: true })).toBeDisabled();
    await page.reload();
    await open();
    await expect(pagination).toContainText("51–61 / 61 件");
    await pagination.getByRole("combobox", { name: "表示件数", exact: true }).click();
    await page.getByRole("option", { name: "10 件", exact: true }).click();
    await expect(pagination).toContainText("1–10 / 61 件");
    await expect(panel.getByRole("rowheader")).toHaveCount(10);
    await pagination.getByRole("button", { name: "次へ", exact: true }).click();
    await expect(pagination).toContainText("11–20 / 61 件");
    await expect(panel.getByRole("rowheader").first()).toHaveText("項目 011");
    await pagination.getByRole("button", { name: "前へ", exact: true }).click();
    await expect(pagination).toContainText("1–10 / 61 件");
    await page.screenshot({ path: testInfo.outputPath(`${kind}-paging.png`) });
    await expectNoPageOverflow(page);

    count = 1;
    await page.reload();
    await open();
    await expect(pagination).toContainText("1–1 / 1 件");
    await expect(panel.getByRole("rowheader")).toHaveCount(1);
    await expect(pagination.getByRole("button", { name: "次へ", exact: true })).toBeDisabled();
  });
}

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await mockKnowledgeBases(page);
});

test("言語資源の上書き（検索・回答の項目だけ）を検索・回答プロファイルに保存できる (#2181)", { tag: "@responsive" }, async ({
  page,
}) => {
  let updateBody = null as Record<string, unknown> | null;
  await mockDefaultSearchAnswerProfile(page, (body) => {
    updateBody = body;
  });
  const fields = [
    { section: "numbers", key: "article_units", kind: "words", choices: [], ingest: true, max_chars: 40 },
    { section: "answer_check_cues", key: "guarantee_verbs", kind: "words", choices: [], ingest: false, max_chars: 40 },
  ];
  const effective = { numbers: { article_units: ["条"] }, answer_check_cues: { guarantee_verbs: ["なぜ", "原因"] } };
  await page.route("**/api/settings/language-pack", (route) =>
    route.fulfill({
      json: { data: { fields, builtin: effective, overrides: {}, effective, hash: "a" }, error_messages: [], warning_messages: [] },
    }),
  );
  await page.goto("/search-answer-profiles?id=bv-default");
  const section = page.getByTestId("search-answer-profile-language-pack");
  await expect(section).not.toHaveAttribute("open");
  await section.locator("summary").click();
  await expect(section).toHaveAttribute("open", "");
  // 取込の項目（条の単位）は出さない。
  await expect(section.getByLabel("条の単位", { exact: true })).toHaveCount(0);
  const cause = section.getByLabel("保証の動詞", { exact: true });
  await expect(cause).toHaveValue("なぜ\n原因");
  await cause.fill("なぜ\n原因\n理屈");
  await expect(section.getByText("上書き 1 項目")).toBeVisible();
  await expect(section.getByText("全体の値から変更")).toBeVisible();
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("全社共通の検索設定");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();
  await expect
    .poll(() => (updateBody?.config as { language_pack?: unknown } | undefined)?.language_pack)
    .toEqual({ answer_check_cues: { guarantee_verbs: ["なぜ", "原因", "理屈"] } });
  await section.locator("summary").click();
  await expect(cause).not.toBeVisible();
  await page.screenshot({ path: test.info().outputPath("language-overrides-collapsed.png"), fullPage: true });
  await section.locator("summary").click();
  await expect(cause).toHaveValue("なぜ\n原因\n理屈");
  await page.screenshot({ path: test.info().outputPath("language-overrides-expanded.png"), fullPage: true });
  await expectNoPageOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`検索・回答プロファイルの作成エディタは設定を表示し横崩れしない (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockSearchAnswerProfiles(page, []);

    await page.goto("/search-answer-profiles?id=new");

    await expect(page.getByRole("heading", { name: "検索・回答プロファイルを作成" })).toBeVisible();
    // A 型のエディタの「一覧へ戻る」は左上（パンくずは出さない。page-archetypes.md §1 A、#618）。
    await expect(page.getByTestId("editor-back")).toHaveText("一覧へ戻る");
    await expect(page.getByTestId("editor-back")).toHaveAccessibleName(/の一覧へ戻る$/);
    await expect(page.getByRole("navigation", { name: "パンくず" })).toHaveCount(0);
    await expect(page.getByRole("textbox", { name: "名前", exact: true })).toBeVisible();
    await expect(page.getByText("参照するナレッジベース", { exact: false }).first()).toBeVisible();
    // ナレッジベースはコンボボックスを開くと候補として現れる。
    await page.getByRole("combobox", { name: "参照するナレッジベース" }).click();
    await expect(page.getByRole("option", { name: /社内規程/ })).toBeVisible();
    const settings = page.locator("fieldset").filter({ hasText: "検索・回答設定" });
    // 検索方法・検索オプション・根拠確認・回答スタイル・回答プロンプト（system prompt・既定言語）は削除した(#595)。
    await expect(settings.getByRole("heading", { level: 3 })).toHaveText([
      "1 つの文書から採る根拠の上限",
      "回答モデル",
      "業務ガイドを使用する",
    ]);
    await expect(settings.getByRole("heading", { name: "検索インデックス" })).toHaveCount(0);
    // 品質評価は検索・回答プロファイルで上書きしない(評価はグローバル設定だけで決まる。#301)。
    await expect(settings.getByRole("heading", { name: "品質評価" })).toHaveCount(0);
    // 全文検索の分割方式は 1 つにまとめ、選択を削除した(#588)。
    await expect(settings.getByRole("heading", { name: "全文検索の分割方式" })).toHaveCount(0);
    await expect(settings.getByRole("combobox", { name: "全文検索の分割方式" })).toHaveCount(0);
    // 回答エンジンの選択は削除した(#594)。
    await expect(settings.getByRole("combobox", { name: "回答エンジン" })).toHaveCount(0);
    // 個別に残すのは文書ごとの根拠上限と回答モデルの2選択欄だけ。
    await expect(settings.getByRole("button", { name: "グローバル既定を継承" })).toHaveCount(2);
    await expect(settings.getByRole("button", { name: "検索・回答プロファイルで上書き" })).toHaveCount(2);
    await expect(page.getByLabel("回答の役割・口調")).toHaveCount(0);
    await expect(page.getByLabel("既定の回答言語")).toHaveCount(0);
    await expect(settings.getByTestId("answer-prompt-unused-note")).toHaveCount(0);
    await expect(settings).not.toContainText("DocRAG");
    await expectNoPageOverflow(page);
  });
}

test("検索・回答プロファイルを作成すると参照 KB と方針を含めて POST し、作成した検索・回答プロファイルのエディタへ置き換えて移る", async ({
  page,
}) => {
  let createBody = null as Record<string, unknown> | null;
  await mockSearchAnswerProfiles(page, [], (body) => {
    createBody = body;
  });

  await page.goto("/search-answer-profiles");
  await page.getByRole("button", { name: "新規作成" }).click();
  await expect(page).toHaveURL(/\/search-answer-profiles\?id=new$/);

  await page.getByRole("combobox", { name: "参照するナレッジベース" }).click();
  await page.getByRole("option", { name: /社内規程/ }).click();
  await page.getByRole("combobox", { name: "参照するナレッジベース" }).press("Escape");
  await page.getByRole("textbox", { name: "名前", exact: true }).fill("経理ビュー");
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("経理規程の問い合わせに回答します");
  await expect(page.getByRole("heading", { name: "安全チェック", level: 3 })).toHaveCount(0);
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "作成", exact: true }).click();

  await expect
    .poll(() => (createBody?.config as { knowledge_base_ids?: string[] })?.knowledge_base_ids)
    .toEqual(["kb-1"]);
  expect(createBody?.name).toBe("経理ビュー");
  expect(createBody?.description).toBe("経理規程の問い合わせに回答します");
  expect(
    (createBody?.config as { query?: { guardrail_policy?: string } })?.query?.guardrail_policy
  ).toBeUndefined();
  // 削除した項目（#595）は送らない。
  const config = (createBody?.config ?? {}) as Record<string, unknown>;
  expect("system_prompt" in config).toBe(false);
  expect("default_language" in config).toBe(false);
  for (const removed of ["retrieval_strategy", "post_retrieval_pipeline", "generation_profile"]) {
    expect(removed in ((config.query as Record<string, unknown>) ?? {})).toBe(false);
  }
  expect(
    "vector_index_profile" in
      ((createBody?.config as { query?: Record<string, unknown> })?.query ?? {})
  ).toBe(false);
  // 配信モード（serving_mode）は持たない（文書の全 active レシピを常に融合する。#1331）。
  expect("serving_mode" in config).toBe(false);

  // 作成に成功したら作成した検索・回答プロファイルのエディタへ replace で移る（戻るで空の新規フォームへ戻らない）。
  await expect(page).toHaveURL(/\/search-answer-profiles\?id=bv-new$/);
  await expect(page.getByRole("heading", { name: "経理ビュー", level: 1 })).toBeVisible();
  await expect(page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/search-answer-profiles$/);
  await expect(page.getByRole("button", { name: "新規作成" })).toBeVisible();
});

test("個別設定は残した項目だけを保存する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  let body = null as Record<string, unknown> | null;
  await mockSearchAnswerProfiles(page, [], (value) => { body = value; });
  await page.goto("/search-answer-profiles?id=new");
  await page.getByRole("combobox", { name: "参照するナレッジベース" }).click();
  await page.getByRole("option", { name: /社内規程/ }).click();
  await page.getByRole("combobox", { name: "参照するナレッジベース" }).press("Escape");
  await page.getByRole("textbox", { name: "名前", exact: true }).fill("プロファイル A");
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("合成の設定の検証用");
  const perDocument = page.getByRole("heading", { name: "1 つの文書から採る根拠の上限", level: 3 }).locator("..");
  await expect(perDocument.getByText(/^既定は 5 です。並べ替えの後、/)).toBeVisible();
  await perDocument.getByRole("button", { name: "検索・回答プロファイルで上書き" }).click();
  await perDocument.getByRole("combobox").click();
  await page.getByRole("option", { name: "上限なし" }).click();
  const model = page.getByRole("heading", { name: "回答モデル", level: 3 }).locator("..");
  await model.getByRole("button", { name: "検索・回答プロファイルで上書き" }).click();
  await model.getByRole("combobox").click();
  await page.getByRole("option", { name: "モデル B", exact: true }).click();
  await page.getByRole("switch", { name: "業務ガイドを使用する" }).click();
  await page.getByTestId("search-answer-profile-guide-matching").screenshot({ path: testInfo.outputPath("guide-usage-switch.png") });
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "作成", exact: true }).click();
  await expect.poll(() => body?.name).toBe("プロファイル A");
  const config = body?.config as { answer_model_id?: string; query?: Record<string, unknown> };
  expect(config.answer_model_id).toBe("model-b");
  expect(config.query?.max_anchors_per_document).toBe(0);
  expect(config.query?.support_guide_enabled).toBe(true);
  for (const key of [...GLOBAL_QUERY_FIELDS, "entity_expansion_enabled", "entity_expansion_max_chunks", "section_expansion_enabled", "section_expansion_max_levels", "section_expansion_input_token_percent"]) expect(key in (config.query ?? {})).toBe(false);
});

test("業務ガイドは既定 OFF の単一スイッチで保存・再表示する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  let body = null as Record<string, unknown> | null;
  await mockDefaultSearchAnswerProfile(page, (value) => { body = value; });
  await page.goto("/search-answer-profiles?id=bv-default");
  const panel = page.getByTestId("search-answer-profile-guide-matching");
  const toggle = panel.getByRole("switch", { name: "業務ガイドを使用する" });
  const save = page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true });
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  await expect(panel).toContainText("既定値は OFF です");
  await expect(panel.getByRole("switch")).toHaveCount(1);
  await expect(page.getByRole("group", { name: "業務ガイドの手順ごとに探す", exact: true })).toHaveCount(0);
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("合成の設定の検証用");
  for (const enabled of [true, false]) {
    await toggle.click();
    await save.click();
    await expect.poll(() => (body?.config as { query?: { support_guide_enabled?: boolean } })?.query?.support_guide_enabled).toBe(enabled);
    await page.reload();
    await expect(toggle).toHaveAttribute("aria-checked", String(enabled));
    await panel.screenshot({ path: testInfo.outputPath(`guide-usage-${enabled ? "on" : "off"}.png`) });
    await expectNoPageOverflow(page);
  }
});

// #3053・#3523: 実体・章節・関係情報の拡張と業務ガイドの手順ごとの検索（上限・期限を含む）は全体設定だけで決め、
// 検索・回答プロファイルは表示も保存もしない。
test("検索方法の全体設定の項目は表示せず、古い個別設定を保存しない", { tag: "@responsive" }, async ({ page }, testInfo) => {
  let body = null as Record<string, unknown> | null;
  await mockDefaultSearchAnswerProfile(page, (value) => { body = value; }, {
    query_strategy: "hyde", answer_flow: "crag", neighbor_child_count: 0,
    rerank_enabled: false, auto_field_filter_enabled: true, graph_expansion_enabled: true, graph_expansion_max_chunks: 3,
    screen_linking_enabled: true, request_coverage_retrieval_enabled: false,
    entity_expansion_enabled: false, entity_expansion_max_chunks: 2, section_expansion_enabled: false, section_expansion_max_levels: 2, section_expansion_input_token_percent: 30,
    guide_step_retrieval_enabled: false, guide_step_max_queries: 0, guide_step_deadline_seconds: 10,
  });
  await page.goto("/search-answer-profiles?id=bv-default");
  for (const name of ["質問の拡張", "回答の生成方式", "根拠の前後から加える数", "回答の検索のオプション", "手順ごとの検索の上限", "手順ごとの検索の時間上限（秒）"]) {
    await expect(page.getByRole("heading", { name, exact: true })).toHaveCount(0);
  }
  for (const name of ["Rerank で検索候補を並べ替える", "質問から項目の条件を読み取る", "文書構造による検索拡張", "画面目録で操作画面を探す", "根拠の無い要求を探し直す", "業務ガイドの手順ごとに探す"]) {
    await expect(page.getByRole("group", { name, exact: true })).toHaveCount(0);
  }
  for (const name of ["実体・別名による検索拡張", "章節で広げる根拠"]) {
    await expect(page.getByText(name, { exact: true })).toHaveCount(0);
  }
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("合成の設定の検証用");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => body).not.toBeNull();
  const query = (body?.config as { query?: Record<string, unknown> })?.query ?? {};
  for (const key of [...GLOBAL_QUERY_FIELDS, "entity_expansion_enabled", "entity_expansion_max_chunks", "section_expansion_enabled", "section_expansion_max_levels", "section_expansion_input_token_percent"]) expect(key in query).toBe(false);
  await page.reload();
  await page.getByTestId("search-answer-profile-guide-matching").scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("global-search-options.png") });
  await expectNoPageOverflow(page);
});

test("回答前の確認の方針（曖昧な質問の扱い）を検索・回答プロファイルに保存できる (#1839)", { tag: "@responsive" }, async ({
  page,
}) => {
  let updateBody = null as Record<string, unknown> | null;
  await mockDefaultSearchAnswerProfile(page, (body) => {
    updateBody = body;
  });
  await page.goto("/search-answer-profiles?id=bv-default");
  const section = page.getByTestId("search-answer-profile-clarification");
  await expect(section.getByText("回答前の確認", { exact: true })).toBeVisible();
  const policy = section.getByRole("combobox", { name: "曖昧な質問の扱い" });
  // 古い保存（方針が無い）は「確認してから回答する」。
  await expect(policy).toContainText("確認してから回答する");
  await policy.click();
  await page.getByRole("option", { name: "前提を示して回答する" }).click();
  await expect(policy).toContainText("前提を示して回答する");
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("全社共通の検索設定");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();
  await expect
    .poll(() => (updateBody?.config as { clarification_policy?: string } | undefined)?.clarification_policy)
    .toBe("assume");
  await expectNoPageOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`DEFAULT は参照 KB と名前を固定し設定だけ保存できる (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    let updateBody = null as Record<string, unknown> | null;
    await mockDefaultSearchAnswerProfile(page, (body) => {
      updateBody = body;
    });

    await page.goto("/search-answer-profiles");

    const row = page.getByTestId("search-answer-profile-row-bv-default");
    // アーカイブは行の RowActionMenu に入り、DEFAULT では理由付きで無効（#131）。
    await row.getByRole("button", { name: "DEFAULT の操作" }).click();
    await expect(page.getByRole("menuitem", { name: "DEFAULT はアーカイブできません" })).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(row.getByRole("button", { name: "DEFAULT の操作" })).toBeFocused();
    // キーボードは先頭セルの名前のボタンで全画面エディタを開く（page-archetypes.md §0-7）。
    await row.getByRole("link", { name: "DEFAULT を編集" }).focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/search-answer-profiles\?id=bv-default$/);
    await expect(page.getByRole("heading", { name: "DEFAULT", level: 1 })).toBeVisible();
    // エディタの操作のバーにも同じ定義（DEFAULT はアーカイブ不可）を出す。
    await page
      .getByTestId("search-answer-profile-detail-actions")
      .getByRole("button", { name: "その他の操作" })
      .click();
    await expect(page.getByRole("menuitem", { name: "DEFAULT はアーカイブできません" })).toBeDisabled();
    await page.keyboard.press("Escape");

    await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveAttribute("readonly", "");
    await expect(page.getByText("DEFAULT の名前は変更できません。")).toBeVisible();
    await expect(page.getByRole("combobox", { name: "参照するナレッジベース" })).toBeDisabled();
    await expect(page.getByText(/DEFAULT ナレッジベースだけを参照します/)).toBeVisible();

    // 説明は必須（#521）。説明が空の既存の検索・回答プロファイルは、保存するときに入力を求めて送らない。
    const description = page.getByRole("textbox", { name: "説明", exact: true });
    await expect(description).toHaveAttribute("aria-required", "true");
    await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();
    await expect(page.getByText("説明を入力してください。")).toBeVisible();
    await expect(description).toHaveAttribute("aria-invalid", "true");
    await expect(description).toBeFocused();
    expect(updateBody).toBeNull();

    await description.fill("全社共通の検索設定");
    await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();

    await expect.poll(() => updateBody?.name).toBeUndefined();
    await expect
      .poll(() => (updateBody?.config as { knowledge_base_ids?: string[] })?.knowledge_base_ids)
      .toEqual(["kb-default"]);
    expect(
      (updateBody?.config as { query?: { guardrail_policy?: string } })?.query?.guardrail_policy
    ).toBeUndefined();
    expect(updateBody?.description).toBe("全社共通の検索設定");
    await expectNoPageOverflow(page);
  });
}

test("検索・回答プロファイルの名前と説明は必須で、空・空白だけでは作成せず最初の不正な欄へフォーカスする", async ({
  page,
}) => {
  let createBody = null as Record<string, unknown> | null;
  await mockSearchAnswerProfiles(page, [], (body) => {
    createBody = body;
  });
  await page.goto("/search-answer-profiles?id=new");

  const name = page.getByRole("textbox", { name: "名前", exact: true });
  const description = page.getByRole("textbox", { name: "説明", exact: true });
  await expect(name).toHaveAttribute("aria-required", "true");
  await expect(description).toHaveAttribute("aria-required", "true");
  // placeholder に「任意」を出さない。
  await expect(description).toHaveAttribute("placeholder", "例: 経理規程の問い合わせに回答します");

  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "作成", exact: true }).click();
  await expect(page.getByText("名前を入力してください。")).toBeVisible();
  await expect(page.getByText("説明を入力してください。")).toBeVisible();
  await expect(name).toBeFocused();

  await name.fill("経理ビュー");
  await description.fill("   ");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "作成", exact: true }).click();
  await expect(page.getByText("名前を入力してください。")).toHaveCount(0);
  await expect(page.getByText("説明を入力してください。")).toBeVisible();
  await expect(description).toBeFocused();
  expect(createBody).toBeNull();
  await expect(page).toHaveURL(/\/search-answer-profiles\?id=new$/);
  await expectNoPageOverflow(page);
});

test("検索・回答プロファイル作成では DEFAULT を予約名として拒否する", async ({ page }) => {
  await mockSearchAnswerProfiles(page, []);
  await page.goto("/search-answer-profiles?id=new");

  await page.getByRole("textbox", { name: "名前", exact: true }).fill(" default ");
  await page.getByRole("textbox", { name: "名前", exact: true }).blur();

  await expect(page.getByText("DEFAULT は予約名のため使用できません。")).toBeVisible();
});

test("RAG 検索は検索・回答プロファイルを 1 つ選び、search_answer_profile_id で送る（#635）", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockSearchAnswerProfiles(page, [
    {
      id: "bv-1",
      name: "経理ビュー",
      description: null,
      status: "ACTIVE",
      knowledge_base_count: 1,
      created_at: "2026-06-19T00:00:00Z",
      updated_at: "2026-06-19T00:00:00Z",
      archived_at: null,
    },
    {
      id: "bv-2",
      name: "人事ビュー",
      description: null,
      status: "ACTIVE",
      knowledge_base_count: 2,
      created_at: "2026-06-19T00:00:00Z",
      updated_at: "2026-06-19T00:00:00Z",
      archived_at: null,
    },
  ]);

  let searchPayload = null as Record<string, unknown> | null;
  await page.route("**/api/search/stream", async (route) => {
    searchPayload = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: searchStreamBody(),
    });
  });

  await page.goto("/search");

  // 選び直すと置き換わる（複数を選ばない）。
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await selectSearchAnswerProfile(page, /人事ビュー/);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("経費精算の上限");
  await page.getByRole("button", { name: "検索", exact: true }).click();

  await expect.poll(() => searchPayload?.search_answer_profile_id).toBe("bv-2");
  expect(searchPayload).not.toHaveProperty("search_answer_profile_ids");
  expect(searchPayload).not.toHaveProperty("knowledge_base_ids");
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`RAG 検索は検索・回答プロファイルが無いと作成導線の空状態を出す (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockSearchAnswerProfiles(page, []);

    await page.goto("/search");

    // 作成を促す空状態と CTA を表示する。
    await expect(page.getByText("検索・回答プロファイルを作成してください")).toBeVisible();
    await expect(page.getByRole("button", { name: "検索・回答プロファイルを作成" })).toBeVisible();

    // 検索入力・ナレッジベースピッカーは出さない(検索・回答プロファイル一本化)。
    await expect(page.getByRole("textbox", { name: "RAG 検索" })).toHaveCount(0);
    await expect(page.getByText("ナレッジベース名で絞り込み")).toHaveCount(0);

    await expectNoPageOverflow(page);

    // CTA は検索・回答プロファイル管理へ遷移する。
    await page.getByRole("button", { name: "検索・回答プロファイルを作成" }).click();
    await expect(page).toHaveURL(/\/search-answer-profiles\?id=new$/);
    await expect(page.getByRole("heading", { name: "検索・回答プロファイルを作成" })).toBeVisible();
  });
}

test("RAG 検索は検索・回答プロファイル未選択だと必須エラーを出し送信しない", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockSearchAnswerProfiles(page, [
    {
      id: "bv-1",
      name: "経理ビュー",
      description: null,
      status: "ACTIVE",
      knowledge_base_count: 1,
      created_at: "2026-06-19T00:00:00Z",
      updated_at: "2026-06-19T00:00:00Z",
      archived_at: null,
    },
  ]);

  let searchCalled = false;
  await page.route("**/api/search/stream", async (route) => {
    searchCalled = true;
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: searchStreamBody(),
    });
  });

  await page.goto("/search");

  // 検索・回答プロファイルを選ばずに検索すると必須エラー。
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("経費精算の上限");
  await page.getByRole("button", { name: "検索", exact: true }).click();

  await expect(page.getByText("対象の検索・回答プロファイルを選択してください。")).toBeVisible();
  expect(searchCalled).toBe(false);
});

test("RAG 検索は DEFAULT を候補表示するが自動選択しない", async ({ page }) => {
  await mockSearchAnswerProfiles(page, [
    {
      id: "bv-default",
      name: "DEFAULT",
      description: null,
      status: "ACTIVE",
      knowledge_base_count: 1,
      created_at: "2026-06-30T00:00:00Z",
      updated_at: "2026-06-30T00:00:00Z",
      archived_at: null,
    },
  ]);
  await page.goto("/search");

  await page.getByRole("button", { name: /検索・回答プロファイル/ }).click();
  await expect(page.getByRole("option", { name: /DEFAULT/ })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("全社規程");
  await page.getByRole("button", { name: "検索", exact: true }).click();

  await expect(page.getByText("対象の検索・回答プロファイルを選択してください。")).toBeVisible();
});

const accountingView: SearchAnswerProfileSummaryFixture = {
  id: "bv-1",
  name: "経理ビュー",
  description: "経費精算の相談",
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: "2026-06-19T00:00:00Z",
  updated_at: "2026-06-19T00:00:00Z",
  archived_at: null,
};

// #2091: 編集の画面でも「参照するナレッジベース」の開いた一覧の下部（N 件選択中・表示中をすべて選択・クリア・完了）が 1 行に並ぶ。
test("検索・回答プロファイルの編集: 参照するナレッジベースの開いた一覧の下部が 1 行に並び、画面からはみ出さない", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockSearchAnswerProfiles(page, [accountingView]);
  await page.goto("/search-answer-profiles?id=bv-1");
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveValue(accountingView.name);

  await page.getByRole("combobox", { name: "参照するナレッジベース" }).click();
  await expect(page.locator("[data-searchable-panel-footer]")).toContainText(/\d+ 件選択中/);
  await expectSearchablePanelFits(page);
  await expectNoPageOverflow(page);
});

test("検索・回答プロファイルは行のクリックで ?id= の全画面エディタを開き、再読込・戻る / 進むで同じ対象を開く", async ({
  page,
}) => {
  await mockSearchAnswerProfiles(page, [accountingView]);
  await page.goto("/search-answer-profiles");

  const row = page.getByTestId("search-answer-profile-row-bv-1");
  // 行の操作以外の領域（参照 KB の列）のクリックで開く。
  await row.getByRole("cell").nth(2).click();
  await expect(page).toHaveURL(/\/search-answer-profiles\?id=bv-1$/);
  await expect(page.getByRole("heading", { name: "経理ビュー", level: 1 })).toBeVisible();
  await expect(page.getByTestId("editor-back")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveValue("経理ビュー");
  await expect(page.getByRole("heading", { name: "プロファイルの知識" })).toBeVisible();
  await expectNoPageOverflow(page);

  await page.reload();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveValue("経理ビュー");

  await page.goBack();
  await expect(page).toHaveURL(/\/search-answer-profiles$/);
  await expect(page.getByTestId("search-answer-profile-row-bv-1")).toBeVisible();
  await page.goForward();
  await expect(page).toHaveURL(/\/search-answer-profiles\?id=bv-1$/);
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveValue("経理ビュー");

  // 一覧へ戻るボタンは履歴に積んで一覧へ移る。
  await clickBackToList(page);
  await expect(page).toHaveURL(/\/search-answer-profiles$/);
});

test("検索・回答プロファイルのエディタは未保存の変更があると一覧へ戻るの移動を確認し、下書きを対象ごとに残す", async ({
  page,
}) => {
  await mockSearchAnswerProfiles(page, [accountingView]);
  await page.goto("/search-answer-profiles?id=bv-1");
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("経費と出張の相談");

  await page.getByTestId("editor-back").click();
  const dialog = page.getByRole("alertdialog", { name: "保存していない変更があります" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/\?id=bv-1$/);

  await clickBackToList(page);
  await page
    .getByRole("alertdialog", { name: "保存していない変更があります" })
    .getByRole("button", { name: "移動する" })
    .click();
  await expect(page).toHaveURL(/\/search-answer-profiles$/);

  // 同じ対象を開き直すと下書きを復元する。新規（?id=new）には持ち込まない。
  await page.getByRole("link", { name: "経理ビュー を編集" }).click();
  await expect(page.getByRole("textbox", { name: "説明", exact: true })).toHaveValue("経費と出張の相談");
  await expect(page.getByText("保存していない下書きを復元しました。")).toBeVisible();
  await page.getByRole("button", { name: "変更を破棄" }).click();
  await expect(page.getByRole("textbox", { name: "説明", exact: true })).toHaveValue("経費精算の相談");
  await page.goto("/search-answer-profiles?id=new");
  await expect(page.getByRole("textbox", { name: "説明", exact: true })).toHaveValue("");
});

// #586: エディタには検索・回答プロファイルのフォームのほかに知識の節のフォーム（それぞれ離脱の確認を持つ）がある。
// どのフォームが未保存でも、ブラウザの戻る・進むで確認し、キャンセルで留まる。
test("検索・回答プロファイルのエディタは未保存の変更があるとブラウザの戻る・進むでも確認する", async ({ page }) => {
  const consoleWarnings: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "warning") consoleWarnings.push(message.text());
  });
  await mockSearchAnswerProfiles(page, [accountingView]);
  await page.goto("/search-answer-profiles");
  await page.getByRole("link", { name: "経理ビュー を編集" }).click();
  await expect(page).toHaveURL(/\?id=bv-1$/);
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("経費と出張の相談");

  await page.goBack();
  const dialog = page.getByRole("alertdialog", { name: "保存していない変更があります" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/\?id=bv-1$/);
  await expect(page.getByRole("textbox", { name: "説明", exact: true })).toHaveValue("経費と出張の相談");

  await page.goBack();
  await dialog.getByRole("button", { name: "移動する" }).click();
  await expect(page).toHaveURL(/\/search-answer-profiles$/);

  // 検索・回答プロファイルのフォームは未変更のまま、知識の節（重要語句）だけを変えても、戻るで確認する。
  // （移動しても下書きはこのタブに残るため、開き直したら先に元に戻す。）
  await page.goForward();
  await expect(page).toHaveURL(/\?id=bv-1$/);
  await page.getByRole("button", { name: "変更を破棄" }).click();
  await page.getByRole("tab", { name: "ドメインキーワード" }).click();
  await page.locator("#domain-keywords-editor").fill("経費精算");
  await page.goBack();
  const discardDialog = page.getByRole("alertdialog", { name: "変更を破棄しますか" });
  await expect(discardDialog).toBeVisible();
  await discardDialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/\?id=bv-1$/);
  await expect(page.locator("#domain-keywords-editor")).toHaveValue("経費精算");
  // サイドナビのリンクとリロード（beforeunload）も同じく確認する。
  await page.getByTestId("editor-back").click();
  await expect(discardDialog).toBeVisible();
  await discardDialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/\?id=bv-1$/);
  const reloadPrevented = await page.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  });
  expect(reloadPrevented).toBe(true);
  // 1 画面に離脱の確認が複数あっても、router の blocker は 1 つだけにする。
  expect(consoleWarnings.filter((text) => text.includes("only supports one blocker"))).toEqual([]);
});

// #585 / #1786: 全画面のエディタは、欄に結び付かない保存の失敗を確定の行（フォームの末尾）の直上の 1 か所だけに出す
// （Toast とフォームの下の FormStatus に重ねない。UX 契約 messaging.md §3.3.1）。
test("検索・回答プロファイルの保存に失敗すると、理由を確定の行の直上だけに出す", async ({
  page,
}) => {
  const message = "アーカイブ済みのナレッジベースは参照できません。";
  await mockSearchAnswerProfiles(page, [accountingView]);
  let attempts = 0;
  await page.route("**/api/search-answer-profiles/bv-1", async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    attempts += 1;
    await route.fulfill({
      status: 409,
      json: { data: null, error_messages: [message], warning_messages: [] },
    });
  });
  await page.goto("/search-answer-profiles?id=bv-1");
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("経費と出張の相談");
  // 確定の行はフォームの末尾（画面の下端に貼り付けない。#2715）。行までスクロールして保存する。
  const actions = page.getByRole("group", { name: "確定の操作" });
  await actions.getByRole("button", { name: "保存", exact: true }).click();

  const banner = page.getByTestId("search-answer-profile-save-error");
  await expect(banner.getByRole("alert")).toHaveText(message);
  await expect(banner).toBeInViewport();
  await expect.poll(() => attempts).toBe(1);
  // 確定の行の直上に置く（ボタンと失敗が並んで見える）。
  const [bannerBottom, actionsTop] = await Promise.all([
    banner.evaluate((element) => element.getBoundingClientRect().bottom),
    actions.evaluate((element) => element.getBoundingClientRect().top),
  ]);
  expect(actionsTop).toBeGreaterThanOrEqual(bannerBottom);
  expect(actionsTop - bannerBottom).toBeLessThan(48);
  // 同じ失敗を Toast とフォームの下に重ねない。
  await expect(page.getByText(message)).toHaveCount(1);
  // 入力は残り、未保存のまま（離脱の確認の対象）。
  await expect(page.getByRole("textbox", { name: "説明", exact: true })).toHaveValue("経費と出張の相談");
  await expectNoPageOverflow(page);
});

test("URL の検索・回答プロファイルが存在しないときは別の対象へ置き換えず、一覧へ戻る導線を出す", async ({ page }) => {
  await mockSearchAnswerProfiles(page, [accountingView]);
  await page.goto("/search-answer-profiles?id=bv-missing");

  await expect(page.getByText("対象が見つかりません")).toBeVisible();
  await expect(page.getByText("「bv-missing」は削除されたか、存在しません。")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveCount(0);
  await expect(page).toHaveURL(/\?id=bv-missing$/);
  await expectNoPageOverflow(page);

  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page).toHaveURL(/\/search-answer-profiles$/);
  await expect(page.getByTestId("search-answer-profile-row-bv-1")).toBeVisible();
});

test("エディタからアーカイブすると確認のうえ一覧へ置き換えて戻る", async ({ page }) => {
  let archived = false;
  await mockSearchAnswerProfiles(page, [accountingView], undefined, () => {
    archived = true;
  });
  await page.goto("/search-answer-profiles");
  await page.getByRole("link", { name: "経理ビュー を編集" }).click();
  await expect(page).toHaveURL(/\?id=bv-1$/);

  await page
    .getByTestId("search-answer-profile-detail-actions")
    .getByRole("button", { name: "その他の操作" })
    .click();
  await page.getByRole("menuitem", { name: "アーカイブ" }).click();
  const dialog = page.getByRole("alertdialog", { name: "検索・回答プロファイルをアーカイブしますか?" });
  await dialog.getByRole("button", { name: "アーカイブ" }).click();
  await expect.poll(() => archived).toBe(true);
  await expect(page).toHaveURL(/\/search-answer-profiles$/);
  // アーカイブ後は replace で一覧へ戻るため、戻るで消えた対象のエディタへ戻らない。
  await page.goBack();
  await expect(page).toHaveURL(/\/search-answer-profiles$/);
});

// #555: エディタの PageHeader に状態と件数・更新日時を出し、アーカイブ済みは入力できないようにする。
test("エディタの見出しに状態と参照 KB の件数を出し、アーカイブ済みは読み取り専用で保存できない", async ({
  page,
}) => {
  await mockSearchAnswerProfiles(page, [
    accountingView,
    { ...accountingView, id: "bv-old", name: "旧ビュー", status: "ARCHIVED" },
  ]);
  await page.goto("/search-answer-profiles?id=bv-1");
  const header = page.locator("header[data-page-header]");
  await expect(header.getByText("有効")).toBeVisible();
  await expect(page.getByTestId("search-answer-profile-meta")).toContainText("参照 KB 1 件");

  await page.goto("/search-answer-profiles?id=bv-old");
  await expect(header.getByText("アーカイブ済み")).toBeVisible();
  await expect(page.getByText("アーカイブ済みの検索・回答プロファイルは編集・保存できません。")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toHaveAttribute("readonly", "");
  await expect(page.getByRole("textbox", { name: "説明", exact: true })).toHaveAttribute("readonly", "");
  await expect(page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true })).toBeDisabled();
  await expect(
    page.getByRole("group", { name: "1 つの文書から採る根拠の上限" }).getByRole("button", { name: "検索・回答プロファイルで上書き" })
  ).toBeDisabled();
  await expectNoPageOverflow(page);
});

test("検索・回答プロファイルが無いときは、空の状態から作成エディタへ進める", async ({ page }) => {
  await mockSearchAnswerProfiles(page, []);
  await page.goto("/search-answer-profiles");
  await expect(page.getByText("検索・回答プロファイルがありません")).toBeVisible();
  await page.getByRole("button", { name: "最初の検索・回答プロファイルを作成" }).click();
  await expect(page).toHaveURL(/\/search-answer-profiles\?id=new$/);
});

/**
 * エディタの「一覧へ戻る」。ページの左上（タイトルの上）にあり、375px でも 1 タップで押せる。
 * 確定（保存・変更を破棄）はフォームの末尾にあり、ヘッダーの右の操作の列には何も置かない（#618 / #1786）。
 */
async function clickBackToList(page: Page) {
  const actions = page.getByRole("group", { name: "ページ操作" });
  await expect(actions.getByRole("button", { name: /^その他の操作/ })).toHaveCount(0);
  await expect(actions.getByTestId("editor-back")).toHaveCount(0);
  await page.getByTestId("editor-back").click();
}

interface SearchAnswerProfileSummaryFixture {
  id: string;
  name: string;
  description: string | null;
  status: string;
  knowledge_base_count: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

async function mockSearchAnswerProfiles(
  page: Page,
  items: SearchAnswerProfileSummaryFixture[],
  onCreate?: (body: Record<string, unknown>) => void,
  onArchive?: (id: string) => void
) {
  const details = new Map<string, Record<string, unknown>>(
    items.map((item) => [
      item.id,
      {
        ...item,
        config: {
          version: 1,
          knowledge_base_ids: ["kb-1"],
          query: {
          },
        },
        knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
      },
    ])
  );
  const envelope = (data: unknown) => ({ json: { data, error_messages: [], warning_messages: [] } });
  await page.route("**/api/search-answer-profiles**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname === "/api/search-answer-profiles/answer-models") {
      return route.fulfill({ json: { data: [
        { model_id: "model-a", display_name: "モデル A", kind: "text" },
        { model_id: "model-b", display_name: "モデル B", kind: "text_vision" },
      ], error_messages: [], warning_messages: [] } });
    }
    const id = pathname.split("/")[3];
    if (request.method() === "POST" && pathname === "/api/search-answer-profiles") {
      const body = request.postDataJSON() as Record<string, unknown>;
      onCreate?.(body);
      const created = {
        id: "bv-new",
        name: body.name,
        description: body.description ?? null,
        status: "ACTIVE",
        knowledge_base_count:
          (body.config as { knowledge_base_ids?: string[] })?.knowledge_base_ids?.length ?? 0,
        config: body.config,
        knowledge_bases: [],
        created_at: "2026-06-19T00:00:00Z",
        updated_at: "2026-06-19T00:00:00Z",
        archived_at: null,
      };
      details.set("bv-new", created);
      await route.fulfill(envelope(created));
      return;
    }
    if (pathname.endsWith("/archive")) {
      onArchive?.(id);
      await route.fulfill(envelope({ ...details.get(id), status: "ARCHIVED" }));
      return;
    }
    if (pathname.endsWith("/domain-keywords")) {
      await route.fulfill(envelope({ search_answer_profile_id: id, keywords: [] }));
      return;
    }
    if (pathname.endsWith("/approved-faq")) {
      await route.fulfill(envelope({ search_answer_profile_id: id, records: [] }));
      return;
    }
    if (pathname.endsWith("/runtime-knowledge")) {
      await route.fulfill(envelope({ search_answer_profile_id: id, terms: [], rules: [] }));
      return;
    }
    if (id) {
      const detail = details.get(id);
      if (!detail) {
        await route.fulfill({
          status: 404,
          json: { data: null, error_messages: ["検索・回答プロファイルが見つかりません。"], warning_messages: [] },
        });
        return;
      }
      await route.fulfill(envelope(detail));
      return;
    }
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get("limit") ?? 25);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    await route.fulfill(
      envelope({ items: items.slice(offset, offset + limit), total: items.length, limit, offset, has_next: offset + limit < items.length })
    );
  });
}

async function mockKnowledgeBases(page: Page) {
  await page.route("**/api/knowledge-bases**", async (route) => {
    await route.fulfill({
      json: {
        data: {
          items: [
            {
              id: "kb-default",
              name: "DEFAULT",
              description: null,
              status: "ACTIVE",
              default_search_mode: "hybrid",
              document_count: 0,
              indexed_document_count: 0,
              error_document_count: 0,
              searchable_chunk_count: 0,
              created_at: "2026-06-30T00:00:00Z",
              updated_at: "2026-06-30T00:00:00Z",
              archived_at: null,
            },
            {
              id: "kb-1",
              name: "社内規程",
              description: "経費・人事・情報管理",
              status: "ACTIVE",
              default_search_mode: "hybrid",
              document_count: 3,
              indexed_document_count: 3,
              error_document_count: 0,
              searchable_chunk_count: 16,
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

async function mockDefaultSearchAnswerProfile(
  page: Page,
  onUpdate: (body: Record<string, unknown>) => void,
  initialQuery: Record<string, unknown> = {},
  initialConfig: Record<string, unknown> = {}
) {
  const summary: SearchAnswerProfileSummaryFixture = {
    id: "bv-default",
    name: "DEFAULT",
    description: null,
    status: "ACTIVE",
    knowledge_base_count: 1,
    created_at: "2026-06-30T00:00:00Z",
    updated_at: "2026-06-30T00:00:00Z",
    archived_at: null,
  };
  const config = {
    version: 1,
    knowledge_base_ids: ["kb-default"],
    query: initialQuery,
    ...initialConfig,
  };
  let savedConfig: Record<string, unknown> = config;
  let savedDescription = summary.description;

  await page.route("**/api/search-answer-profiles**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname === "/api/search-answer-profiles/answer-models") {
      return route.fulfill({ json: { data: [
        { model_id: "model-a", display_name: "モデル A", kind: "text" },
        { model_id: "model-b", display_name: "モデル B", kind: "text_vision" },
      ], error_messages: [], warning_messages: [] } });
    }
    if (request.method() === "PATCH") {
      const body = request.postDataJSON() as Record<string, unknown>;
      onUpdate(body);
      savedConfig = body.config as Record<string, unknown>;
      savedDescription = body.description as string;
      await route.fulfill({
        json: {
          data: { ...summary, description: body.description, config: body.config, knowledge_bases: [] },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    // 継承するシステム設定の窓口（#2683）。
    if (pathname.endsWith("/inherited-contact")) {
      await route.fulfill({
        json: { data: { enabled: true, contact: "窓口 A" }, error_messages: [], warning_messages: [] },
      });
      return;
    }
    if (pathname.endsWith("/bv-default")) {
      await route.fulfill({
        json: {
          data: {
            ...summary,
            config: savedConfig,
            description: savedDescription,
            knowledge_bases: [{ id: "kb-default", name: "DEFAULT" }],
          },
          error_messages: [],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fulfill({
      json: {
        data: { items: [summary], total: 1, limit: 50, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

function searchStreamBody(answer = "上限額を確認しました。"): string {
  return [
    `event: metadata\ndata: ${JSON.stringify({
      trace_id: "trace-bv",
      elapsed_ms: 10,
      guardrail_warnings: [],
      diagnostics: {
        retrieval_strategy: "hybrid",
        retrieval_strategy_adapter: "grounded",
        filter_keys: [],
        knowledge_base_count: 1,
        search_answer_profile_applied: "bv-1",
        config_fingerprint: "fp-bv",
      },
    })}\n\n`,
    `event: delta\ndata: ${JSON.stringify({ text: answer })}\n\n`,
    `event: citations\ndata: ${JSON.stringify([])}\n\n`,
    `event: done\ndata: ${JSON.stringify({ trace_id: "trace-bv" })}\n\n`,
  ].join("");
}

// アーカイブ済みは読み取り専用。上書き中の項目の選択欄も操作できず、未保存の変更を作らない。
test("アーカイブ済みの検索・回答プロファイルは上書き中の項目の選択欄も変更できない", { tag: "@responsive" }, async ({ page }) => {
  const archived = {
    ...accountingView,
    id: "bv-old",
    name: "旧ビュー",
    status: "ARCHIVED",
    archived_at: "2026-06-20T00:00:00Z",
    config: {
      version: 1,
      knowledge_base_ids: ["kb-1"],
      query: { query_strategy: "hyde" },
    },
    knowledge_bases: [{ id: "kb-1", name: "社内規程", status: "ACTIVE" }],
  };
  await page.route("**/api/search-answer-profiles**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    const envelope = (data: unknown) => ({ json: { data, error_messages: [], warning_messages: [] } });
    if (pathname.endsWith("/domain-keywords")) {
      await route.fulfill(envelope({ search_answer_profile_id: "bv-old", keywords: [] }));
      return;
    }
    if (pathname.endsWith("/approved-faq")) {
      await route.fulfill(envelope({ search_answer_profile_id: "bv-old", records: [] }));
      return;
    }
    if (pathname.endsWith("/runtime-knowledge")) {
      await route.fulfill(envelope({ search_answer_profile_id: "bv-old", terms: [], rules: [] }));
      return;
    }
    await route.fulfill(envelope(archived));
  });

  await page.route("**/api/search-answer-profiles/bv-old/question-understanding", (route) => route.fulfill({ json: {
    data: {
      fields: [{ section: "question_cues", key: "cause_cues", kind: "words", choices: [], ingest: false, max_chars: 40 }],
      builtin: { question_cues: { cause_cues: ["なぜ"] } }, overrides: {},
      effective: { question_cues: { cause_cues: ["なぜ"] } }, hash: "synthetic",
    }, error_messages: [], warning_messages: [],
  } }));
  await page.goto("/search-answer-profiles?id=bv-old");
  await expect(page.getByText("アーカイブ済みの検索・回答プロファイルは編集・保存できません。")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "安全チェック" })).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "質問の拡張" })).toHaveCount(0);
  await page.getByRole("tab", { name: "質問の理解", exact: true }).click();
  const understanding = page.getByTestId("question-understanding-panel");
  await expect(understanding.getByRole("textbox", { name: "原因を尋ねる語", exact: true })).toBeDisabled();
  await expect(understanding.getByRole("button", { name: "質問の理解を保存", exact: true })).toBeDisabled();
  await expect(understanding.getByRole("textbox", { name: "テストする質問", exact: true })).toBeEnabled();
  await expectNoPageOverflow(page);
});

// 知識（登録済みQ&A・用語・ドメインキーワード・ルール）の読み込みに失敗したら、空（0 件）と見せずに
// 失敗と再試行を出す。空の一覧・既定のオンのスイッチ・空の編集欄を出すと、保存済みの知識が無いと誤解する。
test("検索・回答プロファイルの知識の読み込みに失敗すると、空ではなく失敗と再試行を出す", async ({ page }) => {
  // 取得ごとに既定の再試行（3 回）を待つため長めにする。
  test.setTimeout(60_000);
  await mockSearchAnswerProfiles(page, [accountingView]);
  let failures = true;
  const failOrEmpty = (empty: unknown) => async (route: import("@playwright/test").Route) => {
    if (failures) {
      await route.fulfill({
        status: 500,
        json: { data: null, error_messages: ["知識を読み込めませんでした。"], warning_messages: [] },
      });
      return;
    }
    await route.fulfill({ json: { data: empty, error_messages: [], warning_messages: [] } });
  };
  await page.route(
    "**/api/search-answer-profiles/bv-1/approved-faq",
    failOrEmpty({ search_answer_profile_id: "bv-1", enabled: true, records: [] })
  );
  await page.route(
    "**/api/search-answer-profiles/bv-1/runtime-knowledge",
    failOrEmpty({ search_answer_profile_id: "bv-1", terms: [], rules: [] })
  );
  await page.route(
    "**/api/search-answer-profiles/bv-1/domain-keywords",
    failOrEmpty({ search_answer_profile_id: "bv-1", keywords: [] })
  );

  await page.goto("/search-answer-profiles?id=bv-1");
  const panel = page.getByRole("tabpanel");
  // 既定の再試行（3 回）の後に失敗を出す。
  await expect(panel.getByText("知識を読み込めませんでした。")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("approved-faq-enabled")).toBeDisabled();
  await expect(panel.getByText("登録済みQ&Aはまだありません")).toHaveCount(0);

  // 用語・同義語と回答ルールは同じ取得・同じ部品なので、用語・同義語で確かめる。
  await page.getByRole("tab", { name: "用語・同義語" }).click();
  await expect(panel.getByText("知識を読み込めませんでした。")).toBeVisible({ timeout: 15_000 });
  // ドメインキーワードの保存は全置換なので、読めていない空の編集欄を出さない。
  await page.getByRole("tab", { name: "ドメインキーワード" }).click();
  await expect(panel.getByText("知識を読み込めませんでした。")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("textbox", { name: /登録キーワード/ })).toHaveCount(0);

  // 再試行で読み直せる。
  failures = false;
  await panel.getByRole("button", { name: "再試行" }).click();
  await expect(panel.getByText("知識を読み込めませんでした。")).toHaveCount(0);
});

// アーカイブで件数が減り、保存したページが範囲外になったら最後のページへ戻す（ナレッジベースの一覧と同じ）。
test("検索・回答プロファイルの一覧は最後のページの行をアーカイブすると前のページへ戻り、空の案内を出さない", async ({
  page,
}) => {
  const profiles = Array.from({ length: 26 }, (_, index) => ({
    ...accountingView,
    id: `bv-${index + 1}`,
    name: `ビュー ${String(index + 1).padStart(2, "0")}`,
  }));
  let active = profiles;
  await page.route("**/api/search-answer-profiles**", async (route) => {
    const url = new URL(route.request().url());
    const envelope = (data: unknown) => ({ json: { data, error_messages: [], warning_messages: [] } });
    if (url.pathname.endsWith("/archive")) {
      const id = url.pathname.split("/")[3];
      active = active.filter((item) => item.id !== id);
      await route.fulfill(envelope({ ...profiles.find((item) => item.id === id), status: "ARCHIVED" }));
      return;
    }
    const limit = Number(url.searchParams.get("limit") ?? 50);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    await route.fulfill(
      envelope({
        items: active.slice(offset, offset + limit),
        total: active.length,
        limit,
        offset,
        has_next: offset + limit < active.length,
      })
    );
  });

  await page.goto("/search-answer-profiles");
  const pagination = page.getByTestId("search-answer-profiles-pagination");
  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(page.locator("tbody tr")).toHaveCount(1);
  await page.getByRole("button", { name: "ビュー 26 の操作" }).click();
  await page.getByRole("menuitem", { name: "アーカイブ" }).click();
  await page
    .getByRole("alertdialog", { name: "検索・回答プロファイルをアーカイブしますか?" })
    .getByRole("button", { name: "アーカイブ" })
    .click();

  await expect(page.locator("tbody tr")).toHaveCount(25);
  await expect(page.getByText("検索・回答プロファイルがありません")).toHaveCount(0);
  // 1 ページだけになったのでページ送りは出さない（25 件は最も少ない表示件数の段の 10 件より多いので、範囲と表示件数は出す）。
  await expect(pagination).toContainText("1–25 / 25 件");
  await expect(pagination.getByRole("button", { name: "次へ" })).toHaveCount(0);
});

// #1786 / #2715: 確定の行はフォームの末尾のふつうの行（言語資源の画面と同じ）。画面の下端に貼り付けず、内容と一緒に
// スクロールする。下の「プロファイルの知識」（節ごとに保存）まで進んでも上へ流れて PageHeader の上に描かれない。
test("確定の行はフォームの末尾に置き、スクロールしても貼り付かず PageHeader に重ならない", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockSearchAnswerProfiles(page, [accountingView]);
  await page.goto("/search-answer-profiles?id=bv-1");
  const actions = page.getByRole("group", { name: "確定の操作" });
  await expect(actions.getByRole("button")).toHaveText(["保存", "変更を破棄"]);
  await expect(page.locator("[data-page-header-actions]").getByRole("button", { name: "保存", exact: true })).toHaveCount(0);

  // 先頭: 長いフォームの末尾の行は画面の外にある（画面の下端に貼り付かない）。
  await expect(page.getByRole("textbox", { name: "名前", exact: true })).toBeInViewport();
  await expect(actions).not.toBeInViewport();

  // フォームの末尾（最後の設定の行。言語資源の上書き。#2181）の直下に行がある。
  const lastRow = page.getByTestId("search-answer-profile-language-pack");
  await actions.scrollIntoViewIfNeeded();
  const save = actions.getByRole("button", { name: "保存", exact: true });
  const [rowBox, barBox, saveBox, nameBox] = await Promise.all([
    lastRow.boundingBox(),
    actions.boundingBox(),
    save.boundingBox(),
    page.getByRole("textbox", { name: "名前", exact: true }).boundingBox(),
  ]);
  expect(barBox!.y).toBeGreaterThanOrEqual(rowBox!.y + rowBox!.height);
  // primary の左端は入力欄の左端（名前の欄）にそろう。
  expect(Math.abs(saveBox!.x - nameBox!.x)).toBeLessThanOrEqual(1);

  // 下の「プロファイルの知識」までスクロールしても、行はフォームのカードの中（知識の節の上）に留まる。
  await page.locator("#pr-main").evaluate((main) => main.scrollTo(0, main.scrollHeight));
  const [endBarBox, knowledgeBox] = await Promise.all([
    actions.boundingBox(),
    page.getByRole("heading", { name: "プロファイルの知識" }).boundingBox(),
  ]);
  expect(endBarBox!.y + endBarBox!.height).toBeLessThanOrEqual(knowledgeBox!.y);

  // 上から下まで、行は画面に貼り付かず、上端の PageHeader（lg 以上）の上に描かれない。
  expect(await findFloatingFormActionBars(page)).toEqual([]);
  await expectNoPageOverflow(page);
});

// 固定文の窓口の案内は上書きだけを提供し、旧方針を再保存しない。
for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`窓口の上書きを保存し、旧案内の方針を再保存しない (${viewport.name})`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    let updateBody = null as Record<string, unknown> | null;
    await mockDefaultSearchAnswerProfile(page, (body) => {
      updateBody = body;
    }, {}, { spec_then_contact: true });
    await page.goto("/search-answer-profiles?id=bv-default");
    const section = page.getByTestId("search-answer-profile-contact");
    await expect(section.getByText("回答の末尾の窓口の案内", { exact: true })).toBeVisible();
    const contact = section.getByRole("textbox", { name: "窓口の上書き" });
    // 古い保存（上書きが無い）は空で、システム設定の窓口を継承する。継承する窓口は placeholder と有効な窓口で
    // 示し、欄にはコピーしない（#2683）。
    await expect(contact).toHaveValue("");
    await expect(contact).toHaveAttribute("placeholder", "未設定（窓口 A を継承）");
    const effective = section.getByTestId("search-answer-profile-contact-effective");
    await expect(effective).toHaveText("有効な窓口: 窓口 A（システム設定の窓口を継承）");
    await expect(contact).toHaveAccessibleDescription(/有効な窓口: 窓口 A/);
    expect(await findClippedPlaceholders(page)).toEqual([]);
    await expect(section.getByRole("switch")).toHaveCount(0);
    await expect(section).toContainText("解決しない場合は、〈窓口〉へお問い合わせください。");

    // システム設定と同じ値は、作業を止めずに「空にすると継承する」と案内する。
    const same = section.getByTestId("search-answer-profile-contact-same");
    await contact.fill("窓口 A");
    await expect(same).toHaveText("システム設定と同じ窓口です。空にすると、システム設定の窓口を継承します。");
    await section.screenshot({ path: testInfo.outputPath("search-answer-profile-contact-same.png") });
    await contact.fill("窓口 B（内線 1234）");
    await expect(same).toHaveCount(0);
    await expect(effective).toHaveText("有効な窓口: 窓口 B（内線 1234）（このプロファイルの上書き）");
    await page.getByRole("textbox", { name: "説明", exact: true }).fill("全社共通の検索設定");
    await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();
    await expect
      .poll(() => updateBody?.config as { contact_override?: string } | undefined)
      .toMatchObject({ contact_override: "窓口 B（内線 1234）" });
    expect(updateBody?.config).not.toHaveProperty("spec_then_contact");
    await expectNoPageOverflow(page);
  });
}


test("言語資源の全体再取得は継承欄だけを更新し、編集中と保存済みの上書きを保持する", { tag: "@responsive" }, async ({ page }) => {
  let body = null as Record<string, unknown> | null;
  await mockDefaultSearchAnswerProfile(page, (value) => { body = value; });
  const fields = ["guarantee_verbs", "guarantee_endings", "guarantee_exceptions"].map((key) => ({
    section: "answer_check_cues", key, kind: "words", choices: [], ingest: false, max_chars: 40,
  }));
  let causeValue = "なぜ";
  let pastValue = "以前";
  let calls = 0;
  await page.route("**/api/settings/language-pack", (route) => {
    calls++;
    const effective = { answer_check_cues: { guarantee_verbs: [causeValue], guarantee_endings: [pastValue], guarantee_exceptions: ["以後"] } };
    return route.fulfill({ json: { data: { fields, effective, builtin: effective, overrides: {}, hash: causeValue }, error_messages: [], warning_messages: [] } });
  });
  await page.goto("/search-answer-profiles?id=bv-default");
  const section = page.getByTestId("search-answer-profile-language-pack");
  await expect(section).not.toHaveAttribute("open");
  await section.locator("summary").click();
  await expect(section).toHaveAttribute("open", "");
  const cause = section.locator("textarea").nth(0);
  const past = section.locator("textarea").nth(1);
  const future = section.locator("textarea").nth(2);
  await expect(cause).toHaveValue("なぜ");
  await past.fill("過去\n");
  causeValue = "どうして";
  const before = calls;
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    window.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    window.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => calls).toBeGreaterThan(before);
  await expect(cause).toHaveValue("どうして");
  await expect(past).toHaveValue("過去\n");
  await future.fill("今後");
  await page.getByRole("textbox", { name: "説明", exact: true }).fill("合成の説明");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => (body?.config as { language_pack?: unknown } | undefined)?.language_pack)
    .toEqual({ answer_check_cues: { guarantee_endings: ["過去"], guarantee_exceptions: ["今後"] } });
  causeValue = "理由";
  pastValue = "過去";
  const savedCalls = calls;
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    window.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    window.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => calls).toBeGreaterThan(savedCalls);
  await expect(cause).toHaveValue("理由");
  await expect(past).toHaveValue(/過去/);
  await future.fill("変更中");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "変更を破棄", exact: true }).click();
  await expect(future).toHaveValue("今後");
  await expect(cause).toHaveValue("理由");
  // 全体と同じになっても、別の欄の保存で明示的な上書きを消さない。
  await future.fill("将来");
  await page.getByRole("group", { name: "確定の操作" }).getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => (body?.config as { language_pack?: unknown } | undefined)?.language_pack)
    .toEqual({ answer_check_cues: { guarantee_endings: ["過去"], guarantee_exceptions: ["将来"] } });
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("inherited-language-refresh.png"), fullPage: true });
});

test("主一覧は高い画面の余白へ拡張し、ページ送りを維持する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockSearchAnswerProfiles(page, Array.from({ length: 60 }, (_, i) => ({ ...accountingView, id: `viewport-${i}`, name: `プロファイル ${i}` })));
  await page.goto("/search-answer-profiles");
  await expectGrowingList(page, testInfo, "search-answer-profiles");
});

test("質問の理解は段階順の独立 Tab で差分を保存し、認識結果と継承を確認できる", { tag: "@responsive" }, async ({ page }) => {
  await mockDefaultSearchAnswerProfile(page, () => {});
  const fields = [
    { section: "question_cues", key: "cause_cues", kind: "words", choices: [], ingest: false, max_chars: 40 },
  ];
  const builtin = { question_cues: { cause_cues: ["なぜ", "原因"] } };
  let overrides: Record<string, Record<string, string[]>> = {};
  let savedBody: unknown;
  let previewBody: unknown;
  let saves = 0;
  const metadata = () => ({ fields, builtin, overrides, effective: { question_cues: { ...builtin.question_cues, ...overrides.question_cues } }, hash: `synthetic-${saves}` });
  await page.route("**/api/search-answer-profiles/bv-default/question-understanding", async (route) => {
    if (route.request().method() === "PUT") {
      expect(route.request().headers()["content-type"]).toBe("application/json");
      savedBody = route.request().postDataJSON();
      overrides = (savedBody as { overrides: typeof overrides }).overrides;
      saves++;
    }
    await route.fulfill({ json: { data: metadata(), error_messages: [], warning_messages: [] } });
  });
  await page.route("**/api/search-answer-profiles/bv-default/question-understanding/preview", async (route) => {
    expect(route.request().headers()["content-type"]).toBe("application/json");
    previewBody = route.request().postDataJSON();
    const result = { inquiry: { request_kind: "cause" }, task_contract: { requests: ["合成の要求"] }, hash: "synthetic-preview" };
    await route.fulfill({ json: { data: { original_text: (previewBody as { text: string }).text, saved: result, draft: result }, error_messages: [], warning_messages: [] } });
  });
  await page.goto("/search-answer-profiles?id=bv-default");
  const tabs = page.getByRole("tablist", { name: "プロファイルの知識", exact: true });
  await expect(tabs.getByRole("tab")).toHaveText(["登録済みQ&A", "質問の理解", "用語・同義語", "確認・回答ルール", "業務ガイド", "ドメインキーワード"]);
  await tabs.getByRole("tab", { name: "質問の理解", exact: true }).click();
  const panel = page.getByTestId("question-understanding-panel");
  const cause = panel.getByRole("textbox", { name: "原因を尋ねる語", exact: true });
  await expect(cause).toHaveValue("なぜ\n原因");
  await cause.fill("あ".repeat(41));
  await expect(panel.getByRole("button", { name: "質問の理解を保存", exact: true })).toBeDisabled();
  await cause.fill("なぜ\n原因\nどうして");
  await tabs.getByRole("tab", { name: "用語・同義語", exact: true }).click();
  const leave = page.getByRole("alertdialog", { name: "変更を破棄しますか" });
  await expect(leave).toBeVisible();
  await leave.getByRole("button", { name: "キャンセル", exact: true }).click();
  await expect(cause).toHaveValue("なぜ\n原因\nどうして");
  await panel.getByRole("textbox", { name: "テストする質問" }).fill("対象 A が失敗した原因は何ですか。対処方法も教えてください。");
  await panel.getByRole("button", { name: "試す", exact: true }).click();
  await expect(panel.getByTestId("question-understanding-result")).toBeVisible();
  expect(previewBody).toEqual({ text: "対象 A が失敗した原因は何ですか。対処方法も教えてください。", overrides: { question_cues: { cause_cues: ["なぜ", "原因", "どうして"] } } });
  expect(saves).toBe(0);
  await page.screenshot({ path: test.info().outputPath("question-understanding-preview.png"), fullPage: true });
  await panel.getByRole("button", { name: "質問の理解を保存", exact: true }).click();
  await expect.poll(() => saves).toBe(1);
  expect(savedBody).toEqual({ overrides: { question_cues: { cause_cues: ["なぜ", "原因", "どうして"] } } });
  await expect(cause).toHaveValue("なぜ\n原因\nどうして");
  await panel.getByRole("button", { name: "全体の値に戻す", exact: true }).click();
  await expect(cause).toHaveValue("なぜ\n原因");
  await panel.getByRole("button", { name: "質問の理解を保存", exact: true }).click();
  await expect.poll(() => saves).toBe(2);
  expect(savedBody).toEqual({ overrides: {} });
  // 空欄は継承へのリセットとは異なり、明示的な空の一覧として保存する。
  await cause.fill("");
  await panel.getByRole("button", { name: "質問の理解を保存", exact: true }).click();
  await expect.poll(() => saves).toBe(3);
  expect(savedBody).toEqual({ overrides: { question_cues: { cause_cues: [] } } });
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("question-understanding.png"), fullPage: true });
});

test("質問の理解の読込失敗は空の保存欄を出さず、再実行できる", { tag: "@responsive" }, async ({ page }) => {
  await mockDefaultSearchAnswerProfile(page, () => {});
  await page.route("**/api/search-answer-profiles/bv-default/question-understanding", (route) => route.fulfill({ status: 503, json: { detail: "合成の一時障害" } }));
  await page.goto("/search-answer-profiles?id=bv-default");
  await page.getByRole("tab", { name: "質問の理解", exact: true }).click();
  await expect(page.getByRole("button", { name: "再試行", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "質問の理解を保存" })).toHaveCount(0);
  await expectNoPageOverflow(page);
});
