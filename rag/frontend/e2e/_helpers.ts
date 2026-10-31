import { expect, type Locator, type Page } from "@playwright/test";

import { findEscapedAbsoluteElements, measureSearchablePanels } from "../../../platform/packages/ui/e2e/layout-checks";

/**
 * 共通認証の利用者（`GET /api/auth/me` の CurrentUser。#214）。
 * 権限コードは backend `app/security/permissions.py` の権限カタログと同じ（unit テストで一致を確かめる）。
 */
export const MENU_PERMISSION_CODES = [
  "menu.search",
  "menu.chat",
  "menu.history",
  "menu.search_answer_profiles",
  "menu.evaluation",
  "menu.feedback",
  "menu.upload",
  "menu.file_list",
  "menu.knowledge_bases",
  "menu.settings_pipeline",
  "menu.settings_preprocess",
  "menu.settings_parser_adapters",
  "menu.settings_chunking",
  "menu.settings_vector_index",
  "menu.settings_retrieval",
  "menu.settings_prompts",
  "menu.settings_guardrail",
  "menu.settings_evaluation",
  "menu.settings_entity_index",
  "menu.settings_graph",
  "menu.security_permissions",
  "menu.security_users",
  "menu.security_roles",
  "menu.security_api_keys",
  "menu.settings_system_tables",
  "menu.settings_chat_attachments",
  "menu.settings_answer_notice",
  "menu.settings_huggingface",
  "menu.settings_services",
  "menu.settings_oci",
  "menu.settings_upload_storage",
  "menu.settings_model",
  "menu.settings_database",
  "menu.settings_appearance",
] as const;

export const CAPABILITY_PERMISSION_CODES = [
  "rag.search_answer_profiles.manage",
  "rag.knowledge_bases.manage",
  "rag.feedback.manage",
  "rag.system_tables.manage",
  "rag.evaluation_calibration.manage",
] as const;

export const ALL_PERMISSION_CODES: string[] = [
  ...MENU_PERMISSION_CODES,
  ...CAPABILITY_PERMISSION_CODES,
];

export interface CurrentUserPayload {
  user_uuid: string;
  login_user_id: string;
  display_name: string;
  status: string;
  force_password_change: boolean;
  role_codes: string[];
  role_display_names: string[];
  is_system_admin: boolean;
  permissions: string[];
  allowed_search_answer_profile_ids: string[] | null;
  allowed_knowledge_base_ids: string[] | null;
  debug_mode: boolean;
  password_change_allowed: boolean;
}

/** ローカル DEBUG（`auth_mode=local`）の利用者。ログインなしで全画面を使える（backend の local_debug_principal と同じ）。 */
export const LOCAL_CURRENT_USER: CurrentUserPayload = {
  user_uuid: "00000000-0000-0000-0000-000000000000",
  login_user_id: "local",
  display_name: "ローカル利用者",
  status: "ACTIVE",
  force_password_change: false,
  role_codes: ["SYSTEM_ADMIN"],
  role_display_names: ["システム管理者"],
  is_system_admin: true,
  permissions: ALL_PERMISSION_CODES,
  allowed_search_answer_profile_ids: null,
  allowed_knowledge_base_ids: null,
  debug_mode: true,
  password_change_allowed: false,
};

export function apiEnvelope<T>(data: T) {
  return { data, error_messages: [] as string[], warning_messages: [] as string[] };
}

/** `GET /api/auth/me` のローカル DEBUG の応答。 */
export const LOCAL_AUTH_ME = apiEnvelope(LOCAL_CURRENT_USER);

/**
 * `/api/auth/me` をローカル DEBUG の利用者（全権限・範囲の制限なし）にする。
 * DB ゲートを通す画面（RAG 固有の設定を含む。#325）を開けるよう、`/api/ready/database` も
 * 既定で ok にする。DB の状態を変える spec は、この後に `page.route` で上書きする（後の route が優先）。
 */
export async function mockLocalAuth(page: Page): Promise<void> {
  await mockDatabaseReady(page);
  await page.route("**/api/auth/me", (route) => route.fulfill({ json: LOCAL_AUTH_ME }));
}

/** DB ユーザー（ログイン済み）の CurrentUser。既定は権限なし・範囲なし。 */
export function dbUser(overrides: Partial<CurrentUserPayload> = {}): CurrentUserPayload {
  return {
    user_uuid: "11111111-1111-1111-1111-111111111111",
    login_user_id: "user01",
    display_name: "利用者 一郎",
    status: "ACTIVE",
    force_password_change: false,
    role_codes: ["RAG_USER"],
    is_system_admin: false,
    permissions: [],
    allowed_search_answer_profile_ids: [],
    allowed_knowledge_base_ids: [],
    debug_mode: false,
    password_change_allowed: true,
    ...overrides,
    // ロールの表示名を渡さずに role_codes だけを変えた利用者は、表示名を code にする（表示名の無いロールと同じ）。
    role_display_names: overrides.role_display_names ?? [...(overrides.role_codes ?? ["RAG_USER"])],
  };
}

/**
 * `/api/auth/me` を、任意の権限・対象範囲を持つログイン済みの DB ユーザーにする。
 * 返した関数で利用者を差し替えられる（null で 401 = 未ログイン）。
 */
export async function mockAuthUser(
  page: Page,
  overrides: Partial<CurrentUserPayload> = {}
): Promise<(next: CurrentUserPayload | null) => void> {
  let current: CurrentUserPayload | null = dbUser(overrides);
  await page.route("**/api/auth/me", (route) =>
    current
      ? route.fulfill({ json: apiEnvelope(current) })
      : route.fulfill({
          status: 401,
          json: { data: null, error_messages: ["ログインが必要です。"], warning_messages: [] },
        })
  );
  return (next) => {
    current = next;
  };
}

/**
 * DB ゲート用の共通モック。
 * 設定ページ以外を開く前に呼ばれる `/api/ready/database` を「利用可能」にして、
 * ゲートに塞がれず本来のページを描画させる。
 */
export const DB_STATUS_OK = {
  data: { status: "ok", check: "ok", detail: null },
  error_messages: [],
  warning_messages: [],
};

/** `/api/ready/database` を ok 応答にして DB ゲートを通過させる。 */
export async function mockDatabaseReady(page: Page): Promise<void> {
  await page.route("**/api/ready/database", (route) => route.fulfill({ json: DB_STATUS_OK }));
}

/**
 * `/settings/system-tables`（運用設定。#658）の SystemTablesCard 用 status stub。
 *
 * `/api/settings/database/system-tables` は `**\/api/settings/database**` に一致するため、
 * 設定ページの mock が catch-all で別 payload を返すとカードがエラー表示になる。
 */
export const SYSTEM_TABLES_STATUS_OK = {
  data: {
    status: "ready",
    schema_version: "0003",
    schema_head: "0003",
    applied_versions: ["0001", "0002", "0003"],
    pending_versions: [],
    expected_object_count: 96,
    existing_object_count: 96,
    expected_table_count: 24,
    existing_table_count: 24,
    missing_objects: [],
    retired_objects: [],
    tables: [],
    operation_state: {
      status: "idle",
      operation_kind: null,
      lease_expires_at: null,
      last_error_code: null,
      schema_epoch: 3,
      updated_at: "2026-07-23T00:00:00+09:00",
    },
  },
  error_messages: [],
  warning_messages: [],
};

/**
 * ページ全体が横スクロールせず、document に第2の縦スクロールがなく、AppShell の外枠に隠れたあふれがないことを検証する。
 *
 * `documentElement` だけでなく **`main`(`overflow-y-auto` で overflow-x も auto になる
 * スクロール領域)の内部はみ出し**も検査する。広いテーブルの `min-w-[…]` がグリッド子の
 * `min-w-0` 欠落でカラム幅を押し広げると、`main` が横スクロールを内部吸収してしまい
 * `documentElement` 基準のチェックだけでは見逃すため(ナレッジベース管理ページの崩れの実例)。
 * テーブル等の意図的な横スクロールは各自の `overflow-x-auto` の箱に閉じ込める前提。
 *
 * `expect.poll` で短時間リトライし、サイドバー折りたたみ等の **UI 遷移中の一過性のはみ出し**は
 * 吸収する(例: viewport を desktop→375 にリサイズした直後の width transition 200ms)。
 * 静的な実バグ(グリッド崩れ・scroll container の伝播)は沈静後も残るため確実に検出する。
 */
export async function expectNoPageOverflow(page: Page): Promise<void> {
  const measure = () =>
    page.evaluate(() => {
      const root = document.documentElement;
      const main = document.querySelector("main");
      return {
        horizontal: Math.max(
          root.scrollWidth - root.clientWidth,
          main ? main.scrollWidth - main.clientWidth : 0
        ),
        documentVertical: root.scrollHeight - root.clientHeight,
        // AppShell の外枠（overflow: hidden）の隠れたあふれ。本文の absolute の要素（sr-only など）が外枠を
        // 包含ブロックにすると外枠が隠れてスクロールでき、外枠ごとずれて本文の下端が画面の外に切れる。
        shell: (() => {
          const shell = document.getElementById("pr-main")?.parentElement;
          if (!shell) return 0;
          return Math.max(shell.scrollHeight - shell.clientHeight, shell.scrollWidth - shell.clientWidth, shell.scrollTop);
        })(),
      };
    });
  // 1px はスクロールバー/小数丸めの許容。遷移沈静まで最大 2s リトライ。
  await expect
    .poll(async () => (await measure()).horizontal, {
      message: "ページ全体(documentElement / main)の横はみ出し",
      timeout: 2000,
    })
    .toBeLessThanOrEqual(1);
  await expect
    .poll(async () => (await measure()).documentVertical, {
      message: "documentElement に第2の縦スクロールがないこと",
      timeout: 2000,
    })
    .toBeLessThanOrEqual(1);
  await expect
    .poll(async () => (await measure()).shell, {
      message: "AppShell の外枠に隠れたあふれがないこと（本文の下端・右端が画面の外に切れない）",
      timeout: 2000,
    })
    .toBeLessThanOrEqual(1);
  // 表・一覧のスクロール領域から sr-only などの absolute の要素が抜け出すと、main のスクロールの範囲が伸びて
  // 本文の末尾に空白ができる（#1116・#2064）。スクロール領域に relative を付ける。
  await expect
    .poll(() => findEscapedAbsoluteElements(page), {
      message: "スクロール領域の外へ抜け出した absolute の要素がないこと",
      timeout: 2000,
    })
    .toEqual([]);
}

/**
 * 開いている検索つきの複数選択（`SearchableMultiSelect`）のパネルが、欄の幅以上で画面からはみ出さず、
 * 下部の「N 件選択中」が折り返さず、操作の組（表示中をすべて選択・クリア・完了）が 1 行にそろうことを確かめる（#2091）。
 * 画面の幅が sm（640px）以上なら、件数の文と操作の組も 1 行に並ぶ（375px は 2 段まで）。
 */
export async function expectSearchablePanelFits(page: Page): Promise<void> {
  const panels = await measureSearchablePanels(page);
  expect(panels).toHaveLength(1);
  const [panel] = panels;
  const wide = (page.viewportSize()?.width ?? 0) >= 640;
  expect(panel, JSON.stringify(panel)).toMatchObject({
    countWrapped: false,
    actionsSplit: false,
    ...(wide ? { footerOneRow: true } : {}),
  });
  expect(panel.panelWidth).toBeGreaterThanOrEqual(panel.fieldWidth);
  expect(panel.left).toBeGreaterThanOrEqual(0);
  expect(panel.right).toBeLessThanOrEqual(panel.viewport);
}

/** main を末尾までスクロールしたとき、実コンテンツの後ろに空白が残らないことを検証する。 */
export async function expectMainScrollEndsAtContent(page: Page): Promise<void> {
  const main = page.getByRole("main");
  await expect
    .poll(
      () =>
        main.evaluate((element) => {
          const content = element.firstElementChild;
          if (!content) return Number.POSITIVE_INFINITY;
          element.scrollTo({ top: element.scrollHeight, left: 0, behavior: "auto" });
          return Math.max(
            0,
            element.getBoundingClientRect().bottom - content.getBoundingClientRect().bottom
          );
        }),
      { message: "main の末尾に実コンテンツを超える空白がないこと", timeout: 2000 }
    )
    .toBeLessThanOrEqual(1);
}

/**
 * 一覧の各行で、セルの内容（要素と文字）が列の境界を超えないことを実測する。
 *
 * - 次のセルが同じ行に並ぶ場合は「内容の右端 <= 次のセルの左端」、最後のセルと縦に積まれる場合は「<= 自セルの右端」。
 * - 内容の左端も自セルの左端を下回らないこと（右寄せの nowrap が左の列へはみ出す場合）。
 * - `overflow: hidden` の祖先（truncate / line-clamp）で切り取られる部分は見えないので数えない。
 * 違反を「列見出し: 内容 はみ出し量」の配列で返す（空配列が合格）。
 */
export async function measureTableCellOverflow(page: Page, tableSelector: string): Promise<string[]> {
  return page.locator(tableSelector).evaluateAll((tables) => {
    const violations: string[] = [];
    for (const table of tables) {
      const headers = Array.from(table.querySelectorAll("thead th, [role='columnheader']")).map(
        (th) => th.textContent?.trim() ?? ""
      );
      for (const row of Array.from(table.querySelectorAll("tr, [role='row']"))) {
        const cells = Array.from(row.querySelectorAll("td, th, [role='cell'], [role='columnheader']")).filter(
          (cell) => cell.parentElement?.closest("tr, [role='row']") === row && cell.getBoundingClientRect().width > 0
        );
        cells.forEach((cell, index) => {
          const cellRect = cell.getBoundingClientRect();
          const nextRect = cells[index + 1]?.getBoundingClientRect();
          const limitRight =
            nextRect && nextRect.top < cellRect.bottom - 1 && nextRect.left >= cellRect.left ? nextRect.left : cellRect.right;
          const walker = document.createTreeWalker(cell, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
          for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            const element = node.nodeType === Node.TEXT_NODE ? node.parentElement : (node as Element);
            if (!element) continue;
            if (node.nodeType === Node.TEXT_NODE && !node.textContent?.trim()) continue;
            let rect: DOMRect;
            if (node.nodeType === Node.TEXT_NODE) {
              const range = document.createRange();
              range.selectNodeContents(node);
              rect = range.getBoundingClientRect();
            } else {
              rect = element.getBoundingClientRect();
            }
            if (rect.width <= 1 || rect.height <= 1) continue;
            let right = rect.right;
            let left = rect.left;
            for (let ancestor = element; ancestor && ancestor !== row; ancestor = ancestor.parentElement!) {
              if (ancestor === element && node.nodeType !== Node.TEXT_NODE) continue;
              if (getComputedStyle(ancestor).overflowX !== "visible") {
                const clip = ancestor.getBoundingClientRect();
                right = Math.min(right, clip.right);
                left = Math.max(left, clip.left);
              }
            }
            const label = `${headers[index] ?? index}: ${(node.textContent ?? "").trim().slice(0, 24)}`;
            if (right > limitRight + 0.5) violations.push(`${label} right +${(right - limitRight).toFixed(1)}px`);
            if (left < cellRect.left - 0.5) violations.push(`${label} left -${(cellRect.left - left).toFixed(1)}px`);
          }
        });
      }
    }
    return Array.from(new Set(violations));
  });
}

/**
 * サイドナビ（`<aside>`）を返す。md 未満（767px 以下）ではナビがドロワーのため（#367）、
 * 閉じていれば上端のバーの「メニュー」を押して開いてから返す。md 以上はそのまま返す。
 * ナビのリンクを押すとドロワーは閉じるので、続けて操作するときは再び呼ぶ。
 */
export async function openSidebarNav(page: Page) {
  const trigger = page.getByTestId("nav-drawer-trigger");
  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  // 画面の描画を待ってから、どちらの形かを見る（シェルが出る前に判定しない）。
  await expect(trigger.or(sidebar).first()).toBeVisible();
  if ((await trigger.isVisible()) && (await trigger.getAttribute("aria-expanded")) !== "true") {
    await trigger.click();
    await expect(page.getByTestId("nav-drawer")).toHaveAttribute("data-state", "open");
  }
  await expect(sidebar).toBeVisible();
  return sidebar;
}

/** 操作部品の高さの段（README §4「操作部品の高さと幅」、#613）。 */
export const CONTROL_HEIGHT = { sm: 32, md: 36, lg: 40 } as const;

/**
 * 入力欄・選択欄・ボタンの期待する高さ（px）。タッチ端末（pointer: coarse）では段によらず 44px。
 * 画面幅ではなく入力方式で決まる（desktop の project で 375px に縮めた画面は 32 / 36 / 40px のまま）。
 */
export async function expectedControlHeight(page: Page, size: keyof typeof CONTROL_HEIGHT = "md") {
  const coarse = await page.evaluate(() => window.matchMedia("(pointer: coarse)").matches);
  return coarse ? 44 : CONTROL_HEIGHT[size];
}

/**
 * RAG 検索・チャットの「対象の検索・回答プロファイル」を 1 つ選ぶ（#635。共有の SearchableSelectField）。
 * ボタンを押して候補の一覧を開き、候補を選ぶ（選ぶと一覧は閉じてボタンへ戻る）。
 */
export async function selectSearchAnswerProfile(page: Page, name: RegExp | string) {
  await page.getByRole("button", { name: /検索・回答プロファイル/ }).click();
  await page.getByRole("listbox", { name: /検索・回答プロファイル/ }).getByRole("option", { name }).click();
  await expect(page.getByRole("listbox", { name: /検索・回答プロファイル/ })).toHaveCount(0);
}

/** RAG 検索の「LLM で回答を生成する」をオンにする（既定はオフで、検索結果までを出す。#649）。 */
export async function enableSearchAnswer(page: Page) {
  const toggle = page.getByRole("switch", { name: "LLM で回答を生成する" });
  if ((await toggle.getAttribute("aria-checked")) !== "true") await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "true");
}

/**
 * 回答（チャット・RAG 検索）の「処理の詳細」を開き、その領域を返す（#2325）。「処理の詳細」は処理の経過の中の末尾に
 * あり、完了後は閉じた処理の経過を開いてから押す。段階の記録の無い回答は処理の経過の行が無く、単独で出る。
 * `testIdPrefix` は `AnswerProcess` の testId（チャットは `chat-answer-progress`、RAG 検索は `search-run-progress`）。
 */
export async function openAnswerProcessDetails(page: Page, testIdPrefix: string) {
  const summary = page.getByTestId(`${testIdPrefix}-summary`);
  const details = page.getByTestId(`${testIdPrefix}-details`);
  // 完了の 1 行（処理の経過）か、単独の「処理の詳細」が出るまで待つ（実行中はどちらも無い）。
  await expect(summary.or(details).first()).toBeVisible();
  if ((await summary.count()) > 0 && !(await details.isVisible())) await summary.click();
  await details.click();
  return page.getByRole("region", { name: "処理の経過" }).getByRole("region", { name: "処理の詳細" });
}

/**
 * チャットの会話の履歴を開き、その領域を返す（#664）。履歴は既定で閉じている。
 * lg 以上はチャットの左のパネル（`complementary`）、lg 未満はモーダルの side sheet（`dialog`）。
 */
export async function openChatHistory(page: Page) {
  const toggle = page.getByTestId("chat-history-toggle");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const history = page.getByTestId("chat-history");
  await expect(history).toBeVisible();
  return history;
}

/**
 * サイドナビのセクションを開く。保存値が無いときは「AI 活用」と今のページのセクションだけが開いている（#1563）。
 * `titles` を渡すとその見出しのセクションだけ、省くと閉じているすべてのセクションを開く（開いていれば何もしない）。
 * 開閉は ui-store（localStorage）に保存されるので、同じテストの中の再読み込みでも開いたまま。
 */
export async function expandSidebarSections(sidebar: Locator, ...titles: string[]) {
  if (titles.length > 0) {
    for (const title of titles) {
      const toggle = sidebar.getByRole("button", { name: new RegExp(`^${title} を(展開|折りたたむ)`) });
      if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
      await expect(toggle).toHaveAttribute("aria-expanded", "true");
    }
    return;
  }
  await expect(sidebar.locator('button[aria-controls^="nav-section-"]').first()).toBeVisible();
  const collapsed = sidebar.locator('button[aria-controls^="nav-section-"][aria-expanded="false"]');
  while ((await collapsed.count()) > 0) {
    await collapsed.first().click();
  }
}

/** 文書一覧は 375px では 3 つの絞り込みを共有シートへまとめる。 */
export async function openFileListFilters(page: Page) {
  const toolbar = page.getByTestId("file-list-toolbar");
  if ((await toolbar.getAttribute("data-list-toolbar-filters-layout")) === "sheet" && !(await page.getByTestId("file-list-toolbar-filters-sheet").isVisible())) {
    await toolbar.getByRole("button", { name: /^絞り込み/ }).click();
  }
}

export async function closeFileListFilters(page: Page) {
  const sheet = page.getByTestId("file-list-toolbar-filters-sheet");
  if (await sheet.isVisible()) await sheet.getByRole("button", { name: "絞り込みを閉じる" }).click();
}
