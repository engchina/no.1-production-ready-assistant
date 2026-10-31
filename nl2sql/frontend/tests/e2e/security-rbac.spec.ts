import { expectLocalUiFonts } from "./_helpers/local-fonts";
import {
  expect,
  test,
  type Locator,
  type Page,
  type Route,
} from "./_helpers/test";
import { openSidebarNav } from "./_helpers/sidebar-nav";
import { mockDatabaseGateReady, profileAccessPage, systemAdminMe } from "./_helpers/database-gate";
import { expectSingleSpinner, visibleSpinners } from "./_helpers/single-spinner";
import { chooseSelectFieldOption, expectSelectFieldValue } from "./_helpers/select-field";
import { remToPx, SUBPIXEL_TOLERANCE } from "./_helpers/spacing";
import {
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

function envelope(data: unknown) {
  return { data, error_messages: [], warning_messages: [] };
}

function problemEnvelope({
  status,
  code,
  title,
  detail,
  requestId,
  retryable = false,
  fieldErrors = [],
}: {
  status: number;
  code: string;
  title: string;
  detail: string;
  requestId: string;
  retryable?: boolean;
  fieldErrors?: Array<{ pointer: string; code: string; message: string }>;
}) {
  return {
    data: null,
    error_messages: [detail],
    warning_messages: [],
    error_code: code,
    problem: {
      type: `urn:nl2sql:problem:${code.toLowerCase().replaceAll("_", "-")}`,
      title,
      status,
      detail,
      code,
      request_id: requestId,
      retryable,
      field_errors: fieldErrors,
    },
  };
}

async function fulfill(route: Route, data: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(status >= 400 ? { detail: data } : envelope(data)),
  });
}

async function topLevelPanelStyle(page: Page, id: string, prefix: "security-users" | "security-roles") {
  const panel = page.locator(`#${prefix}-panel-${id}`);
  await expect(panel).toBeVisible();
  return panel.evaluate((node) => {
    const style = window.getComputedStyle(node);
    return {
      backgroundColor: style.backgroundColor,
      borderTopWidth: style.borderTopWidth,
      borderRadius: style.borderRadius,
      paddingTop: style.paddingTop,
      boxShadow: style.boxShadow,
    };
  });
}

async function expectNoPageHorizontalScroll(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1 &&
          document.body.scrollWidth <= document.body.clientWidth + 1
      )
    )
    .toBeTruthy();
}

async function expectNoElementHorizontalOverflow(locator: Locator) {
  await expect
    .poll(() =>
      locator.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)
    )
    .toBeTruthy();
}

/**
 * SelectField（select-only combobox）の選択肢の表示名（#631）。一覧を開いて読み、Escape で閉じる。
 * ネイティブの select の `toContainText` / `option` の数え方の代わり（ボタンは選択中の表示名だけを持つ）。
 */
async function selectFieldOptionLabels(combobox: Locator) {
  const listbox = combobox.page().locator(`[id="${await combobox.getAttribute("aria-controls")}"]`);
  // 選択肢が 0 件の間は開かない（候補の取得中）ので、開けるまで押し直す。
  await expect(async () => {
    if (!(await listbox.isVisible())) await combobox.click();
    await expect(listbox).toBeVisible({ timeout: 1_000 });
  }).toPass();
  const labels = await listbox.getByRole("option").allInnerTexts();
  await combobox.press("Escape");
  await expect(listbox).toBeHidden();
  return labels.map((label) => label.trim());
}

async function expectScopeFilterControlsInsideRow(locator: Locator) {
  await expect
    .poll(() =>
      locator.evaluate((node) => {
        const rowBox = node.getBoundingClientRect();
        const controls = Array.from(node.querySelectorAll("select,input,button")).filter(
          (control) => {
            const style = window.getComputedStyle(control);
            const box = control.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" && box.width > 0;
          }
        );
        return controls.every((control) => {
          const box = control.getBoundingClientRect();
          return box.left >= rowBox.left - 1 && box.right <= rowBox.right + 1;
        });
      })
    )
    .toBeTruthy();
}

async function expectBoundedSecurityTableScroll(page: Page, scrollRegion: Locator) {
  // 一覧 + 詳細（ListDetailLayout。#2282）の一覧の列は sticky で画面の高さの中に収まり、行は列の中の表で
  // 縦にスクロールする（visibleRows の 8 行の固定の高さにしない。表頭は固定）。
  const metrics = await scrollRegion.evaluate((node) => {
    const header = node.querySelector("thead");
    const column = node.closest('[data-testid^="list-detail-"][data-testid$="-list"]');
    if (!header || !column) throw new Error("security table header or list column is missing");
    const computed = window.getComputedStyle(node);
    return {
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      columnHeight: column.getBoundingClientRect().height,
      viewportHeight: window.innerHeight,
      overflowY: computed.overflowY,
      headerPosition: window.getComputedStyle(header).position,
    };
  });
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);
  // 列の高さは画面の高さまで（sticky で本文をスクロールしても一覧が見え続ける）。
  expect(metrics.columnHeight).toBeLessThanOrEqual(metrics.viewportHeight);
  expect(metrics.headerPosition).toBe("sticky");
  expect(metrics.overflowY).toBe("auto");

  await scrollRegion.evaluate((node) => node.scrollTo({ top: 0, left: 0 }));
  await scrollRegion.focus();
  await expect(scrollRegion).toBeFocused();
  await scrollRegion.press("PageDown");
  await expect.poll(() => scrollRegion.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);

  await scrollRegion.evaluate((node) => node.scrollTo({ top: node.scrollHeight }));
  const bottomState = await scrollRegion.evaluate((node) => {
    const regionRect = node.getBoundingClientRect();
    const header = node.querySelector("thead");
    const lastRow = node.querySelector("tbody tr:last-child");
    if (!header || !lastRow) throw new Error("security table rows are missing");
    const headerRect = header.getBoundingClientRect();
    const lastRowRect = lastRow.getBoundingClientRect();
    return {
      headerOffset: Math.abs(headerRect.top - regionRect.top),
      lastRowVisible:
        lastRowRect.top >= headerRect.bottom - 1 &&
        lastRowRect.bottom <= regionRect.bottom + 1,
    };
  });
  expect(bottomState.headerOffset).toBeLessThanOrEqual(1);
  expect(bottomState.lastRowVisible).toBe(true);
  await expectNoPageHorizontalScroll(page);
}

async function waitForAnimationFrames(page: Page) {
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve()));
      })
  );
}

async function setMainScrollTop(page: Page, top: number) {
  const main = page.getByRole("main");
  await main.evaluate((node, value) => {
    node.scrollTop = value;
  }, top);
  await waitForAnimationFrames(page);
}

async function expectMainScrollPreserved(page: Page, action: () => Promise<void>) {
  const main = page.getByRole("main");
  const before = await main.evaluate((node) => node.scrollTop);
  await action();
  await waitForAnimationFrames(page);
  await expect
    .poll(async () => {
      const after = await main.evaluate((node) => node.scrollTop);
      return Math.abs(after - before);
    })
    .toBeLessThanOrEqual(2);
}

async function expectMainScrollPreservedAfterClick(page: Page, locator: Locator) {
  await locator.scrollIntoViewIfNeeded();
  await waitForAnimationFrames(page);
  await expectMainScrollPreserved(page, async () => {
    await locator.click();
  });
}

async function expectScrollPositionsPreserved(
  page: Page,
  containers: Locator[],
  action: () => Promise<void>
) {
  const before = await Promise.all(
    containers.map((container) =>
      container.evaluate((node) => ({ top: node.scrollTop, left: node.scrollLeft }))
    )
  );
  await action();
  await waitForAnimationFrames(page);
  await expect
    .poll(async () => {
      const after = await Promise.all(
        containers.map((container) =>
          container.evaluate((node) => ({ top: node.scrollTop, left: node.scrollLeft }))
        )
      );
      return Math.max(
        ...after.flatMap((position, index) => [
          Math.abs(position.top - before[index].top),
          Math.abs(position.left - before[index].left),
        ])
      );
    })
    .toBeLessThanOrEqual(2);
}

async function setElementScrollTop(locator: Locator, top: number) {
  await locator.evaluate((node, value) => {
    node.scrollTop = value;
  }, top);
  await expect
    .poll(() => locator.evaluate((node) => node.scrollTop >= 0))
    .toBeTruthy();
}

async function expectElementScrollTop(locator: Locator, top: number) {
  await expect
    .poll(() => locator.evaluate((node) => Math.round(node.scrollTop)))
    .toBe(top);
}

async function expectElementCenterUnobscured(locator: Locator) {
  await locator.scrollIntoViewIfNeeded();
  await expect(locator).toBeVisible();
  await expect
    .poll(() =>
      locator.evaluate((node) => {
        const box = node.getBoundingClientRect();
        const topElement = document.elementFromPoint(
          box.left + box.width / 2,
          box.top + box.height / 2
        );
        return Boolean(topElement && (topElement === node || node.contains(topElement)));
      })
    )
    .toBeTruthy();
}

async function expectDeepSecDataGrantRegionsDoNotOverlap(page: Page, selectedRuleTestId: string) {
  await expect
    .poll(() =>
      page.evaluate((ruleId) => {
        const selectors = [
          ["roles", '[data-testid="security-deepsec-entitlement-roles"]'],
          ["rules", '[data-testid="security-deepsec-entitlement-rules-list"]'],
          ["editor", `[data-testid="${ruleId}"]`],
          ["actions", '[data-testid="security-deepsec-entitlement-action-region"]'],
        ] as const;
        const boxes = selectors.flatMap(([name, selector]) => {
          const node = document.querySelector(selector);
          if (!node) return [];
          const rect = node.getBoundingClientRect();
          return [
            {
              name,
              left: rect.left,
              right: rect.right,
              top: rect.top,
              bottom: rect.bottom,
              width: rect.width,
              height: rect.height,
            },
          ];
        });
        const overlaps: string[] = [];
        for (let leftIndex = 0; leftIndex < boxes.length; leftIndex += 1) {
          for (let rightIndex = leftIndex + 1; rightIndex < boxes.length; rightIndex += 1) {
            const left = boxes[leftIndex];
            const right = boxes[rightIndex];
            if (left.width <= 0 || left.height <= 0 || right.width <= 0 || right.height <= 0) {
              overlaps.push(`${left.name}:${right.name}:zero-size`);
              continue;
            }
            const separated =
              left.right <= right.left + 1 ||
              right.right <= left.left + 1 ||
              left.bottom <= right.top + 1 ||
              right.bottom <= left.top + 1;
            if (!separated) {
              overlaps.push(`${left.name}:${right.name}`);
            }
          }
        }
        return overlaps;
      }, selectedRuleTestId)
    )
    .toEqual([]);
}

async function expectFloatingMenuInsideViewport(page: Page, menu: Locator) {
  await expect(menu).toBeVisible();
  const [box, viewport] = await Promise.all([menu.boundingBox(), page.viewportSize()]);
  expect(box).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport!.width + 1);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport!.height + 1);
  await expect
    .poll(() =>
      menu.evaluate((node) => ({
        constrained: node.getAttribute("data-floating-menu-constrained"),
        fitsWithoutScrollbar: node.scrollHeight <= node.clientHeight + 1,
      }))
    )
    .toEqual({ constrained: null, fitsWithoutScrollbar: true });
}

async function sidebarComparableStyle(locator: Locator) {
  await expect(locator).toBeVisible();
  return locator.evaluate((element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return {
      height: Math.round(rect.height),
      borderRadius: style.borderRadius,
      fontSize: style.fontSize,
      paddingLeft: style.paddingLeft,
      paddingRight: style.paddingRight,
      transitionProperty: style.transitionProperty,
    };
  });
}

async function expectSidebarActionMatchesMenuItem(action: Locator, menuItem: Locator) {
  const [actionStyle, menuStyle] = await Promise.all([
    sidebarComparableStyle(action),
    sidebarComparableStyle(menuItem),
  ]);
  expect(actionStyle).toEqual(menuStyle);
}

const systemRole = {
  role_id: "role-system",
  role_code: "SYSTEM_ADMIN",
  display_name: "システム管理者",
  description: "組み込み",
  is_built_in: true,
  archived: false,
  version: 1,
  permissions: [],
  data_entitlements: [],
  allowed_profile_ids: [],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, profileAccessPage(route.request().url(), []))
  );
});

test("ユーザー管理の migration 未適用は初回 ErrorState、再取得失敗は既存データ保持で示す", async ({
  page,
}, testInfo) => {
  await mockDatabaseGateReady(page);
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole])
  );
  const recoveredUser = {
    user_uuid: "migration-recovered-user",
    login_user_id: "migration.recovered",
    display_name: "復旧確認ユーザー",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: [systemRole.role_id],
    is_bootstrap_admin: false,
  };
  let userRequests = 0;
  let allowUsersSuccess = false;
  let backgroundFailure = false;
  await page.route("**/api/security/users", async (route) => {
    userRequests += 1;
    if (allowUsersSuccess && !backgroundFailure) {
      await fulfill(route, [recoveredUser]);
      return;
    }
    const requestId = backgroundFailure
      ? "users-migration-refresh-request"
      : "users-migration-initial-request";
    const response = problemEnvelope({
      status: 409,
      code: "SECURITY_SCHEMA_MIGRATION_REQUIRED",
      title: "セキュリティ初期化が必要です",
      detail:
        "アプリケーション認証/RBAC の schema migration が未適用です。`uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap` を実行してから再試行してください。",
      requestId,
    });
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      headers: { "X-Request-ID": requestId },
      body: JSON.stringify(response),
    });
  });

  await page.goto("/settings/security/users");

  const initialError = page.locator("main").getByRole("alert");
  await expect(initialError).toHaveCount(1);
  await expect(initialError).toContainText(
    "アプリケーション認証/RBAC の schema migration が未適用です。"
  );
  await expect(initialError).toContainText("リクエストID: users-migration-initial-request");
  await expect(page.getByTestId("security-users-grid")).toHaveCount(0);
  await expect(page.getByText("データがありません", { exact: true })).toHaveCount(0);
  await expectNoPageHorizontalScroll(page);
  await page.screenshot({
    path: testInfo.outputPath(`security-users-migration-error-${testInfo.project.name}.png`),
    fullPage: true,
  });

  const retry = initialError.getByRole("button", { name: "再試行" });
  await retry.focus();
  await expect(retry).toBeFocused();
  allowUsersSuccess = true;
  await retry.press("Enter");

  await expect(page.getByRole("button", { name: "復旧確認ユーザー を表示" })).toBeVisible();
  await expect(initialError).toHaveCount(0);
  expect(userRequests).toBeGreaterThanOrEqual(2);

  backgroundFailure = true;
  const userActions = page.getByTestId("security-users-actions");
  const directRefresh = userActions.getByRole("button", { name: "最新情報を取得" });
  if ((await directRefresh.count()) > 0) {
    await directRefresh.click();
  } else {
    await userActions.getByTestId("page-actions-more").click();
    await page.getByRole("menuitem", { name: "最新情報を取得" }).click();
  }
  const refreshError = page
    .locator("main")
    .getByRole("alert")
    .filter({ hasText: "users-migration-refresh-request" });
  await expect(refreshError).toBeVisible();
  await expect(page.getByRole("button", { name: "復旧確認ユーザー を表示" })).toBeVisible();
  await expect(page.getByTestId("security-users-grid")).toBeVisible();

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(refreshError).toBeVisible();
  await expectNoPageHorizontalScroll(page);
});

test("ユーザー・ロール一覧は一覧の列（画面の高さまで）の中で固定表頭の内部スクロールになる", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  await mockDatabaseGateReady(page);
  // 1 ページ（25 件）が一覧の列の高さを超える件数。
  const rowCount = 30;
  const singlePane = testInfo.project.name === "mobile-375";
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "SECURITY_VIEWER",
    display_name: "セキュリティ閲覧",
    description: "表示のみ",
    is_built_in: false,
    permissions: ["menu.security_users", "menu.security_roles"],
  };
  const users = Array.from({ length: rowCount }, (_, index) => {
    const sequence = String(index + 1).padStart(2, "0");
    return {
      user_uuid: `scroll-user-${sequence}`,
      login_user_id: `scroll.user.${sequence}`,
      display_name: `スクロールユーザー ${sequence}`,
      status: "ACTIVE",
      force_password_change: false,
      locked_until: null,
      version: 1,
      role_ids: [viewerRole.role_id],
      is_bootstrap_admin: false,
    };
  });
  const roles = Array.from({ length: rowCount }, (_, index) => {
    const sequence = String(index + 1).padStart(2, "0");
    return {
      ...viewerRole,
      role_id: `scroll-role-${sequence}`,
      role_code: `SCROLL_ROLE_${sequence}`,
      display_name: `スクロールロール ${sequence}`,
      version: index + 1,
    };
  });

  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, viewerRole])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, users));

  await page.goto("/settings/security/users");
  const userScrollRegion = page.getByTestId("security-users-scroll-region");
  await expect(userScrollRegion).toHaveAttribute("role", "region");
  await expect(userScrollRegion).toHaveAttribute(
    "aria-label",
    "ユーザー一覧。必要に応じて縦方向または横方向にスクロールできます。"
  );
  await expect(userScrollRegion.locator("tbody tr")).toHaveCount(25);

  const lastUser = users.at(-1)!;
  await page.getByTestId("security-users-search").fill(lastUser.display_name);
  await expect(userScrollRegion.locator("tbody tr")).toHaveCount(1);
  await expect
    .poll(() =>
      userScrollRegion.evaluate((node) => node.scrollHeight <= node.clientHeight + 1)
    )
    .toBeTruthy();
  await page.getByTestId("security-users-search").fill("");
  await expect(userScrollRegion.locator("tbody tr")).toHaveCount(25);
  await expectBoundedSecurityTableScroll(page, userScrollRegion);

  const lastUserRow = userScrollRegion.locator("tbody tr").last();
  await lastUserRow.locator("td").nth(1).click();
  await expect(page).toHaveURL(/[?&]user=scroll-user-25/u);
  await expect(
    page.getByRole("heading", { name: users[24].display_name, exact: true })
  ).toBeVisible();
  if (singlePane) {
    // xl 未満は 1 ペイン: 選ぶと詳細だけになり、一覧は隠れる。
    await expect(userScrollRegion).toBeHidden();
  } else {
    await expect(lastUserRow).toHaveAttribute("data-selected", "true");
  }
  await expectNoPageHorizontalScroll(page);

  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, roles)
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));

  await page.goto("/settings/security/roles");
  const roleScrollRegion = page.getByTestId("security-roles-scroll-region");
  await expect(roleScrollRegion).toHaveAttribute("role", "region");
  await expect(roleScrollRegion).toHaveAttribute(
    "aria-label",
    "ロール一覧。必要に応じて縦方向または横方向にスクロールできます。"
  );
  await expect(roleScrollRegion.locator("tbody tr")).toHaveCount(25);

  const lastRole = roles.at(-1)!;
  await page.getByTestId("security-roles-search").fill(lastRole.display_name);
  await expect(roleScrollRegion.locator("tbody tr")).toHaveCount(1);
  await expect
    .poll(() =>
      roleScrollRegion.evaluate((node) => node.scrollHeight <= node.clientHeight + 1)
    )
    .toBeTruthy();
  await page.getByTestId("security-roles-search").fill("");
  await expect(roleScrollRegion.locator("tbody tr")).toHaveCount(25);
  await expectBoundedSecurityTableScroll(page, roleScrollRegion);

  const lastRoleRow = roleScrollRegion.locator("tbody tr").last();
  await lastRoleRow.locator("td").nth(1).click();
  await expect(page).toHaveURL(/[?&]role=scroll-role-25/u);
  // 詳細の見出しはロール名（コード・ID は出さない。#1682）。
  await expect(
    page.getByRole("heading", { name: roles[24].display_name, exact: true })
  ).toBeVisible();
  if (!singlePane) await expect(lastRoleRow).toHaveAttribute("data-selected", "true");
  await expectNoPageHorizontalScroll(page);
});

const deepSecTargetObject = {
  name: "EMPLOYEES",
  owner: "HR",
  qualified_name: "HR.EMPLOYEES",
  object_type: "TABLE",
  comment: "社員",
};

const deepSecTargetObjectDetail = {
  ...deepSecTargetObject,
  columns: [
    {
      column_name: "EMPLOYEE_ID",
      logical_name: "社員ID",
      data_type: "NUMBER",
      nullable: false,
      comment: "",
      sample_values: [],
    },
    {
      column_name: "DISPLAY_NAME",
      logical_name: "氏名",
      data_type: "VARCHAR2(80)",
      nullable: false,
      comment: "",
      sample_values: [],
    },
    {
      column_name: "DEPARTMENT_CODE",
      logical_name: "部門",
      data_type: "VARCHAR2(32 CHAR)",
      nullable: false,
      comment: "",
      sample_values: [],
    },
  ],
};

function deepSecPlan(
  applied = false,
  driverMode: "thin" | "thick" = "thin",
  deepsecEnabled = true,
  hasDataUserPassword = true
) {
  return {
    version: "V001",
    driver_mode: driverMode,
    connection_security: "wallet_mtls",
    deepsec_enabled: deepsecEnabled,
    data_user: "DEEPSEC_DATA_USER",
    has_data_user_password: hasDataUserPassword,
    steps: [
      {
        step_no: 1,
        key: "principals_and_roles",
        title: "共有 DATA USER とロール",
        description:
          "保存済みの DATA USER パスワードで DATA USER（END USER）を作り（既にあれば作り直さない）、関連付ける最小 DB role と local DATA ROLE を準備します。",
        checksum: "a".repeat(64),
        status: applied ? "APPLIED" : "PENDING",
        error_message: "",
        executed_at: applied ? "2026-07-19T00:00:00Z" : null,
        sql: [
          "CREATE ROLE NL2SQL_APP_DB_ROLE",
          "GRANT CREATE SESSION TO NL2SQL_APP_DB_ROLE",
          "CREATE DATA ROLE IF NOT EXISTS NL2SQL_APP_DATA_ROLE",
          "GRANT NL2SQL_APP_DB_ROLE TO NL2SQL_APP_DATA_ROLE",
          "CREATE END USER IF NOT EXISTS DEEPSEC_DATA_USER IDENTIFIED BY <secret:NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD> SCHEMA APP_OWNER",
          "GRANT DATA ROLE NL2SQL_APP_DATA_ROLE TO DEEPSEC_DATA_USER",
        ],
      },
      {
        step_no: 2,
        key: "application_context",
        title: "アプリケーションコンテキスト",
        description: "認証済み利用者を session context へ設定します。",
        checksum: "b".repeat(64),
        status: applied ? "APPLIED" : "PENDING",
        error_message: "",
        executed_at: applied ? "2026-07-19T00:01:00Z" : null,
        sql: ["CREATE OR REPLACE CONTEXT NL2SQL_APP_USER_CTX USING NL2SQL_DEEPSEC_CTX_PKG"],
      },
    ],
  };
}

async function mockDeepSecDataEntitlements(page: Page, rows: unknown[] = [systemRole]) {
  await mockDeepSecTargetObjects(page);
  await page.route("**/api/security/deepsec/data-entitlements", (route) => fulfill(route, rows));
}

async function mockDeepSecTargetObjects(page: Page) {
  await page.route("**/api/security/deepsec/target-objects?*", (route) =>
    fulfill(route, {
      runtime: "oracle",
      owner: "",
      items: [deepSecTargetObject],
      total: null,
      counts_included: false,
      next_cursor: null,
      warnings: [],
    })
  );
  await page.route("**/api/security/deepsec/target-objects/HR/EMPLOYEES**", (route) =>
    fulfill(route, deepSecTargetObjectDetail)
  );
}

test("ローカル DEBUG はログインせず SYSTEM_ADMIN として入り、状態を明示する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "00000000-0000-0000-0000-000000000000",
      login_user_id: "local-debug",
      display_name: "ローカル DEBUG 管理者",
      debug_mode: true,
    })
  );

  await page.goto("/settings/appearance");

  // 375px ではナビがドロワー（#367）。開いてから読む。
  const sidebar = await openSidebarNav(page);
  await expect(page).toHaveURL(/\/settings\/appearance$/);
  await expect(page.getByRole("heading", { name: "システムにログイン" })).toHaveCount(0);
  await expect(
    sidebar.getByRole("status", {
      name: "ログイン省略",
    })
  ).toBeVisible();
  const debugColors = await sidebar
    .getByRole("status", { name: "ログイン省略" })
    .evaluate((element) => {
      const style = getComputedStyle(element);
      return { backgroundColor: style.backgroundColor, color: style.color };
    });
  // 共有 Sidebar は常時暗いスラブ(data-surface="inverted" → color-scheme: dark)なので、
  // globals.css の warning トークンはダーク値に解決される(旧実装はライトの琥珀色を直書きしていた)。
  // --color-warning-subtle = light-dark(amber-200, amber-900 #3a2e13)
  // --color-warning-fg     = light-dark(amber-700, amber-300 #f4c95d)
  // (packages/ui/src/styles/tokens/colors.css, palette.css)
  expect(debugColors).toEqual({ backgroundColor: "rgb(58, 46, 19)", color: "rgb(244, 201, 93)" });
  await expect(sidebar.getByRole("button", { name: "パスワード変更" })).toHaveCount(0);
  await expect(sidebar.getByRole("button", { name: "ログアウト" })).toHaveCount(0);
  const viewport = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
});

test("外観と証明書のページは専用権限だけで表示でき、権限なしでは直達できない", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "appearance-viewer",
      login_user_id: "appearance.viewer",
      display_name: "外観閲覧ユーザー",
      role_codes: ["APPEARANCE_VIEWER"],
      is_system_admin: false,
      permissions: ["menu.settings_appearance"],
      password_change_allowed: true,
    })
  );

  await page.goto("/");
  await expect(page).toHaveURL(/\/settings\/appearance$/);
  await expect(page.getByRole("heading", { name: "外観と証明書" })).toBeVisible();
  // 375px ではナビがドロワー（#367）。開いてから読む。
  const sidebar = await openSidebarNav(page);
  await expect(sidebar.getByRole("link", { name: "外観と証明書" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "SQL 生成" })).toHaveCount(0);
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);

  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "no-access",
      login_user_id: "no.access",
      display_name: "権限なしユーザー",
      role_codes: ["NO_ACCESS"],
      is_system_admin: false,
      permissions: [],
      password_change_allowed: true,
    })
  );

  await page.goto("/settings/appearance");
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
});

test("ログイン失敗を一般化して表示し、初回パスワード変更へ誘導する", async ({ page }) => {
  let loginAttempts = 0;
  await page.route("**/api/auth/me", (route) => fulfill(route, "ログインしてください。", 401));
  await page.route("**/api/auth/login", async (route) => {
    loginAttempts += 1;
    if (loginAttempts === 1) {
      // 実バックエンドと同じく request id 付きの 401 を返す(ログイン画面では非表示にする)
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        headers: { "X-Request-ID": "login-failed-request" },
        body: JSON.stringify(
          problemEnvelope({
            status: 401,
            code: "SECURITY_LOGIN_FAILED",
            title: "ログインできません",
            detail: "ログインユーザーIDまたはパスワードを確認してください。",
            requestId: "login-failed-request",
          })
        ),
      });
      return;
    }
    await fulfill(route, { ...systemAdminMe, force_password_change: true, password_change_allowed: true });
  });
  await page.route("**/api/auth/password/change", (route) => fulfill(route, { changed: true }));

  await page.goto("/login");
  await page.getByLabel("ログインユーザーID").fill("SYSTEM");
  await page.getByLabel("パスワード").fill("WrongPass!123");
  await page.getByRole("button", { name: "ログイン" }).click();
  const loginError = page.getByText("ログインユーザーIDまたはパスワードを確認してください。", { exact: true });
  await expect(loginError).toBeVisible();
  await expect(loginError).not.toContainText("リクエストID");
  await expect(loginError).not.toContainText("login-failed-request");

  await page.getByLabel("パスワード").fill("BootstrapPass!123");
  await page.getByRole("button", { name: "ログイン" }).click();
  await expect(page.getByRole("heading", { name: "パスワードの変更" })).toBeVisible();
  await expect(page.getByRole("button", { name: "ログインへ戻る" })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "サイドナビゲーション" })).toHaveCount(0);

  await page.getByLabel("現在のパスワード").fill("BootstrapPass!123");
  await page.locator("#auth-password-new").fill("IndependentPass!456");
  await page.getByLabel("新しいパスワード（確認）").fill("IndependentPass!456");
  await page.getByRole("button", { name: "パスワードを変更" }).click();
  await expect(page).toHaveURL(/\/login$/);
});

test("ログインの試行が多すぎるときは 429 の文をそのまま出し、再送しない", async ({ page }) => {
  let loginAttempts = 0;
  await page.route("**/api/auth/me", (route) => fulfill(route, "ログインしてください。", 401));
  await page.route("**/api/auth/login", async (route) => {
    loginAttempts += 1;
    // 試行の回数の上限（#1087）。backend は request id と Retry-After 付きの 429 を返す。
    await route.fulfill({
      status: 429,
      contentType: "application/json",
      headers: { "X-Request-ID": "login-rate-limited", "Retry-After": "900" },
      body: JSON.stringify(
        problemEnvelope({
          status: 429,
          code: "SECURITY_RATE_LIMITED",
          title: "リクエストが多すぎます",
          detail: "ログインの試行が多すぎます。しばらく待ってから、もう一度お試しください。",
          requestId: "login-rate-limited",
          retryable: true,
        })
      ),
    });
  });

  await page.goto("/login");
  await page.getByLabel("ログインユーザーID").fill("system_admin");
  await page.getByLabel("パスワード").fill("WrongPass!123");
  await page.getByRole("button", { name: "ログイン" }).click();
  const loginError = page.getByText("ログインの試行が多すぎます。しばらく待ってから、もう一度お試しください。", { exact: true });
  await expect(loginError).toBeVisible();
  await expect(loginError).not.toContainText("リクエストID");
  await expect(page).toHaveURL(/\/login$/);
  expect(loginAttempts).toBe(1);
});

test("構成管理者はアカウントの欄からアカウントの画面を開け、サイドバー操作は安定した高さを保つ", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.goto("/query");

  // 375px ではナビがドロワー（#367）。開いてから読む。
  const sidebar = await openSidebarNav(page);
  const menuItem = sidebar.getByRole("link", { name: "SQL 生成" });
  // パスワード変更はサイドナビの行に無く、アカウントの欄（行全体のリンク）から開くアカウントの画面のタブ（#1873）。
  await expect(sidebar.getByRole("button", { name: "パスワード変更" })).toHaveCount(0);
  const accountLink = sidebar.getByRole("link", { name: /^アカウント設定を開く/ });
  const logoutButton = sidebar.getByRole("button", { name: "ログアウト" });
  await expect(accountLink).toBeVisible();
  expect((await accountLink.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  await expectSidebarActionMatchesMenuItem(logoutButton, menuItem);

  // 初回・リセット後の強制の変更の単独の画面はそのまま。

  await page.goto("/password/change");
  await expect(page.getByRole("heading", { name: "パスワードの変更" })).toBeVisible();
  await expect(page.getByLabel("現在のパスワード")).toBeVisible();
  await expect(page.locator("#auth-password-new")).toBeVisible();
  await expect(page.getByLabel("新しいパスワード（確認）")).toBeVisible();
  await expect(page.getByRole("button", { name: "戻る" })).toBeVisible();
});

test("通常ユーザーはアカウントの欄からパスワード変更のタブを開き、元の画面へ戻れる", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "query-user",
      login_user_id: "query.user",
      display_name: "SQL 生成ユーザー",
      role_codes: ["QUERY_MENU"],
      is_system_admin: false,
      permissions: ["menu.query"],
      password_change_allowed: true,
    })
  );
  await page.route("**/api/nl2sql/history**", (route) => fulfill(route, { items: [], next_cursor: null }));
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-v1",
      refreshed_at: "2026-08-20T00:00:00Z",
      object_count: 1,
      column_count: 2,
      change_token: 1,
      etag: "schema-v1",
    })
  );
  await page.route("**/api/schema/objects**", (route) =>
    fulfill(route, {
      items: [],
      next_cursor: null,
      total: 0,
      table_count: 0,
      view_count: 0,
      counts_included: true,
      refreshed_at: "2026-08-20T00:00:00Z",
      catalog_version: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/search*", (route) =>
    fulfill(route, {
      items: [
        {
          id: "default",
          name: "標準プロファイル",
          category: "",
          description: "",
          archived: false,
          allowed_table_count: 0,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: "profile-default-v1",
          updated_at: "2026-08-20T00:00:00Z",
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    fulfill(route, {
      id: "default",
      name: "標準プロファイル",
      category: "",
      description: "",
      allowed_tables: [],
      allowed_views: [],
      archived: false,
      object_scope_version: 1,
      version: 1,
      etag: "profile-default-v1",
      updated_at: "2026-08-20T00:00:00Z",
    })
  );

  await page.goto("/query");
  // 375px ではナビがドロワー（#367）。開いてから読む。
  const sidebar = await openSidebarNav(page);
  const menuItem = sidebar.getByRole("link", { name: "SQL 生成" });
  const accountLink = sidebar.getByRole("link", { name: "アカウント設定を開く（query.user）" });
  const logoutButton = sidebar.getByRole("button", { name: "ログアウト" });
  await expect(sidebar.getByRole("button", { name: "パスワード変更" })).toHaveCount(0);
  await expect(accountLink).toBeVisible();
  await expect(logoutButton).toBeVisible();
  await expectSidebarActionMatchesMenuItem(logoutButton, menuItem);

  // アカウントの欄はログアウトの上に重ならずに並び、ログアウトの文言は折り返さない。
  const footerLayout = await Promise.all([accountLink, logoutButton].map((element) => element.boundingBox()));
  expect((footerLayout[1]?.y ?? 0) - ((footerLayout[0]?.y ?? 0) + (footerLayout[0]?.height ?? 0))).toBeGreaterThanOrEqual(0);
  await expect(logoutButton.locator("span").last()).toHaveCSS("white-space", "nowrap");

  // 375px のドロワーは押すと閉じて移る（#1873）。
  await accountLink.click();
  await expect(page).toHaveURL(/\/account$/);
  await expect(page.getByRole("heading", { name: "アカウント", level: 1 })).toBeVisible();
  await page.getByRole("tab", { name: "パスワード変更" }).click();
  await expect(page).toHaveURL(/\/account\?tab=password$/);
  await expect(page.getByRole("heading", { name: "パスワードの変更" })).toBeVisible();
  await expect(page.getByLabel("現在のパスワード")).toBeVisible();
  await expect(page.getByTestId("account-password-submit")).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1))
    .toBe(true);
  await page.goBack();
  await expect(page).toHaveURL(/\/query$/);
});

test("強制パスワード変更中の戻る操作はログインへ戻す", async ({ page }) => {
  await page.route("**/api/auth/me", (route) =>
    fulfill(route, {
      ...systemAdminMe,
      user_uuid: "forced-user",
      login_user_id: "forced.user",
      display_name: "初回ユーザー",
      role_codes: ["QUERY_MENU"],
      is_system_admin: false,
      permissions: ["menu.query"],
      force_password_change: true,
      password_change_allowed: true,
    })
  );
  await page.route("**/api/auth/logout", (route) => fulfill(route, { logged_out: true }));

  await page.goto("/password/change");
  await expect(page.getByRole("button", { name: "ログインへ戻る" })).toBeVisible();
  await page.getByRole("button", { name: "ログインへ戻る" }).click();
  await expect(page).toHaveURL(/\/login$/);
});

test("SQL 生成だけのユーザーは profile を利用できるが管理メニューには入れない", async ({ page }) => {
  test.slow();
  await mockDatabaseGateReady(page);
  const limited = {
    ...systemAdminMe,
    user_uuid: "limited",
    login_user_id: "limited.user",
    display_name: "SQL 生成ユーザー",
    role_codes: ["QUERY_MENU"],
    is_system_admin: false,
    permissions: ["menu.query"],
    allowed_profile_ids: ["default"],
    password_change_allowed: true,
  };
  let usageContextRequested = false;
  let fullProfileRequested = false;
  let persistenceRequested = false;
  await page.route("**/api/auth/me", (route) => fulfill(route, limited));
  await page.route("**/api/nl2sql/persistence", (route) => {
    persistenceRequested = true;
    return fulfill(route, {
      mode: "oracle",
      ready: true,
      durable: true,
      writable: true,
      snapshot_loaded: true,
      reason_code: null,
      checked_at: "2026-08-20T00:00:00Z",
    });
  });
  await page.route("**/api/nl2sql/history**", (route) => fulfill(route, { items: [], next_cursor: null }));
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-v1",
      refreshed_at: "2026-08-20T00:00:00Z",
      object_count: 1,
      column_count: 2,
      change_token: 1,
      etag: "schema-v1",
    })
  );
  await page.route("**/api/schema/objects**", (route) =>
    fulfill(route, {
      items: [
        {
          owner: "APP",
          object_name: "DEPARTMENT",
          object_type: "TABLE",
          logical_name: "部署",
          comment: "部署情報",
          row_count: 3,
          column_count: 2,
          last_ddl_at: "",
        },
      ],
      next_cursor: null,
      total: 1,
      table_count: 1,
      view_count: 0,
      counts_included: true,
      refreshed_at: "2026-08-20T00:00:00Z",
      catalog_version: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/search*", (route) =>
    fulfill(route, {
      items: [
        {
          id: "default",
          name: "標準プロファイル",
          category: "",
          description: "",
          archived: false,
          allowed_table_count: 1,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: "profile-default-v1",
          updated_at: "2026-08-20T00:00:00Z",
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) => {
    usageContextRequested = true;
    return fulfill(route, {
      id: "default",
      name: "標準プロファイル",
      category: "",
      description: "",
      allowed_tables: ["APP.DEPARTMENT"],
      allowed_views: [],
      archived: false,
      object_scope_version: 1,
      version: 1,
      etag: "profile-default-v1",
      updated_at: "2026-08-20T00:00:00Z",
    });
  });
  await page.route("**/api/nl2sql/profiles/default", (route) => {
    fullProfileRequested = true;
    return fulfill(route, "この機能を利用する権限がありません。", 403);
  });
  await page.route("**/api/security/users", (route) =>
    fulfill(route, "この機能を利用する権限がありません。", 403)
  );
  await page.goto("/query");
  // 375px ではナビがドロワー（#367）。開いてから読む。
  const sidebar = await openSidebarNav(page);
  await expect(sidebar.getByRole("link", { name: "SQL 生成" })).toBeVisible();
  await expect(page.getByRole("button", { name: "SQL を生成して実行" })).toBeVisible();
  await expect(page.locator("#nl2sql-profile-select")).toContainText("標準プロファイル");
  await expect(page).toHaveURL(/\/query$/);
  await expect(sidebar.getByText("業務プロファイル", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByText("ユーザー管理", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByText("セキュリティ管理", { exact: true })).toHaveCount(0);
  await expect(sidebar.getByRole("link", { name: "SELECT SQL を実行" })).toHaveCount(0);
  await expect(sidebar.getByText("管理 SQL を実行", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "スキーマを更新" })).toHaveCount(0);
  await expect.poll(() => persistenceRequested).toBeTruthy();
  await expect.poll(() => usageContextRequested).toBeTruthy();
  expect(fullProfileRequested).toBe(false);

  await page.goto("/direct-sql");
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();

  await page.goto("/admin-sql");
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();

  await page.evaluate(() => {
    window.history.pushState({}, "", "/profiles");
    window.dispatchEvent(new PopStateEvent("popstate", { state: {} }));
  });
  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();

  const apiStatus = await page.evaluate(async () => (await fetch("/api/security/users")).status);
  expect(apiStatus).toBe(403);
  const profileSearchStatus = await page.evaluate(
    async () => (await fetch("/api/nl2sql/profiles/search")).status
  );
  expect(profileSearchStatus).toBe(200);
  const fullProfileStatus = await page.evaluate(
    async () => (await fetch("/api/nl2sql/profiles/default")).status
  );
  expect(fullProfileStatus).toBe(403);

  await page.goto("/settings/security/users");
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
});

test("SQL 生成だけのユーザーは結果接地で ontology 管理 API の 403 に遷移しない", async ({ page }) => {
  test.slow();
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/rewrite", (route) => {
    // rewrite 既定 ON。未モックだと submit が最初の rewrite で失敗する。
    const body = route.request().postDataJSON() as { question?: string };
    const original = String(body?.question ?? "");
    return fulfill(route, {
      original_question: original,
      rewritten_question: original,
      source: "deterministic",
      model: "",
      warnings: [],
    });
  });
  const limited = {
    ...systemAdminMe,
    user_uuid: "query-only-ontology-result",
    login_user_id: "query.only.ontology",
    display_name: "SQL 生成ユーザー",
    role_codes: ["QUERY_MENU"],
    is_system_admin: false,
    permissions: ["menu.query"],
    allowed_profile_ids: ["default"],
    password_change_allowed: true,
  };
  const createdAt = "2026-08-20T00:00:00Z";
  const question = "部署ごとの人数を表示";
  const generatedSql =
    'SELECT "d"."DEPARTMENT_NAME", COUNT(*) AS "EMPLOYEE_COUNT" FROM "APP"."DEPARTMENT" "d" GROUP BY "d"."DEPARTMENT_NAME"';
  const sqlGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql: generatedSql,
    ctes: [],
    tables: [
      {
        id: "sql-table-department",
        owner: "APP",
        name: "DEPARTMENT",
        alias: "d",
        qualified_name: "APP.DEPARTMENT",
        source_sql: '"APP"."DEPARTMENT" "d"',
      },
    ],
    columns: [
      {
        id: "sql-column-department-name",
        table: "d",
        name: "DEPARTMENT_NAME",
        expression_sql: '"d"."DEPARTMENT_NAME"',
      },
    ],
    joins: [],
    projections: [],
    filters: [],
    aggregates: [{ id: "sql-aggregate-count", expression_sql: "COUNT(*)" }],
    groups: [],
    having: [],
    orders: [],
    windows: [],
    parse_warnings: [],
  };
  const ontologyGraph = {
    revision_id: "revision-query-result",
    revision: {
      id: "revision-query-result",
      version: 1,
      status: "published",
      schema_fingerprint: "schema-v1",
      etag: "revision-query-result-etag",
    },
    nodes: [
      {
        id: "department-table",
        kind: "table",
        technical_name: "APP.DEPARTMENT",
        business_name_ja: "部署情報",
        review_status: "approved",
        metadata: { owner: "APP", object_name: "DEPARTMENT" },
      },
      {
        id: "department-name",
        kind: "column",
        technical_name: "APP.DEPARTMENT.DEPARTMENT_NAME",
        business_name_ja: "部署名",
        review_status: "approved",
        metadata: { owner: "APP", object_name: "DEPARTMENT", column_name: "DEPARTMENT_NAME" },
      },
    ],
    edges: [
      {
        id: "department-name-column",
        kind: "column",
        source_node_id: "department-table",
        target_node_id: "department-name",
        relationship_name_ja: "列",
        review_status: "approved",
      },
    ],
  };
  let ontologyViewRequests = 0;

  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me**", (route) => fulfill(route, limited));
  await page.route("**/api/nl2sql/persistence", (route) =>
    fulfill(route, {
      mode: "oracle",
      ready: true,
      durable: true,
      writable: true,
      snapshot_loaded: true,
      reason_code: null,
      checked_at: createdAt,
    })
  );
  await page.route("**/api/nl2sql/history**", (route) => fulfill(route, { items: [], next_cursor: null }));
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-v1",
      refreshed_at: createdAt,
      object_count: 1,
      column_count: 2,
      change_token: 1,
      etag: "schema-v1",
    })
  );
  await page.route("**/api/schema/objects**", (route) =>
    fulfill(route, {
      items: [
        {
          owner: "APP",
          object_name: "DEPARTMENT",
          object_type: "TABLE",
          logical_name: "部署",
          comment: "部署情報",
          row_count: 3,
          column_count: 2,
          last_ddl_at: "",
        },
      ],
      next_cursor: null,
      total: 1,
      table_count: 1,
      view_count: 0,
      counts_included: true,
      refreshed_at: createdAt,
      catalog_version: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/search*", (route) =>
    fulfill(route, {
      items: [
        {
          id: "default",
          name: "標準プロファイル",
          category: "",
          description: "",
          archived: false,
          allowed_table_count: 1,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: "profile-default-v1",
          updated_at: createdAt,
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    fulfill(route, {
      id: "default",
      name: "標準プロファイル",
      category: "",
      description: "",
      allowed_tables: ["APP.DEPARTMENT"],
      allowed_views: [],
      archived: false,
      object_scope_version: 1,
      version: 1,
      etag: "profile-default-v1",
      updated_at: createdAt,
    })
  );
  await page.route("**/api/nl2sql/profiles/default/ontology-view", (route) => {
    ontologyViewRequests += 1;
    return fulfill(route, "この機能を利用する権限がありません。", 403);
  });
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfill(route, {
      job_id: "job-query-ontology-403",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );
  await page.route("**/api/nl2sql/jobs/job-query-ontology-403", (route) =>
    fulfill(route, {
      job_id: "job-query-ontology-403",
      status: "done",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: createdAt,
      elapsed_ms: 40,
      error_message: null,
      warning_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "done", elapsed_ms: 20 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 4 },
        { stage: "format_results", status: "done", elapsed_ms: 2 },
      ],
      timing: null,
      result: {
        history_id: "hist-query-ontology-403",
        engine: "select_ai",
        engine_meta: { profile: "NL2SQL_DEFAULT_PROFILE" },
        fallback_reason: "",
        original_question: question,
        rewritten_question: question,
        generated_sql: generatedSql,
        executable_sql: generatedSql,
        explanation: "部署ごとの人数を集計します。",
        safety: {
          is_safe: true,
          is_select_only: true,
          row_limit_applied: null,
          blocked_reason: "",
          warnings: [],
          referenced_tables: ["APP.DEPARTMENT"],
          referenced_columns: ["APP.DEPARTMENT.DEPARTMENT_NAME"],
        },
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: {
          columns: ["DEPARTMENT_NAME", "EMPLOYEE_COUNT"],
          rows: [{ DEPARTMENT_NAME: "開発部", EMPLOYEE_COUNT: 3 }],
          total: 1,
        },
        timing: {
          created_at: createdAt,
          started_at: createdAt,
          finished_at: createdAt,
          elapsed_ms: 40,
          stage_timings: [],
        },
        interpretation: {
          available: true,
          question: {
            available: true,
            source: "deterministic",
            original_question: question,
            rewritten_question: question,
            profile_id: "default",
            profile_name: "標準プロファイル",
            profile_category: "",
            target_objects: ["APP.DEPARTMENT"],
            filters: [],
            group_by: ["DEPARTMENT_NAME"],
            order_by: [],
            aggregations: ["COUNT"],
            row_limit: null,
            confidence: 0.9,
            warnings: [],
          },
          sql: {
            available: true,
            source: "sql_semantics",
            summary: "APP.DEPARTMENT を参照し、部署ごとに集計します。",
            statement_type: "SELECT",
            tables: ["APP.DEPARTMENT"],
            columns: ["APP.DEPARTMENT.DEPARTMENT_NAME"],
            joins: [],
            filters: [],
            aggregations: ["COUNT"],
            group_by: ["APP.DEPARTMENT.DEPARTMENT_NAME"],
            order_by: [],
            limit: null,
            semantic_graph: sqlGraph,
            warnings: [],
          },
          ontology_graph: ontologyGraph,
          warnings: [],
        },
        show_prompt: null,
      },
    })
  );

  await page.goto("/query");
  await expect(page.locator("#nl2sql-profile-select")).toContainText("標準プロファイル");
  await page.locator("#nl2sql-question-input").fill(question);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect(page).toHaveURL(/\/query$/);
  await expect(page.getByText("開発部")).toBeVisible();
  await expect(page.getByTestId("nl2sql-sql-grounding-panel")).toBeVisible();
  await expect(page).toHaveURL(/\/query$/);
  expect(ontologyViewRequests).toBe(0);
});

test("SQL 生成だけのユーザーは profile 未作成時に管理作成ボタンを表示しない", async ({ page }) => {
  test.slow();
  await mockDatabaseGateReady(page);
  const limited = {
    ...systemAdminMe,
    user_uuid: "limited-empty-profile",
    login_user_id: "limited.empty",
    display_name: "SQL 生成ユーザー",
    role_codes: ["QUERY_MENU"],
    is_system_admin: false,
    permissions: ["menu.query"],
    allowed_profile_ids: [],
    password_change_allowed: true,
  };
  await page.route("**/api/auth/me", (route) => fulfill(route, limited));
  await page.route("**/api/nl2sql/history**", (route) => fulfill(route, { items: [], next_cursor: null }));
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-v1",
      refreshed_at: "2026-08-20T00:00:00Z",
      object_count: 0,
      column_count: 0,
      change_token: 1,
      etag: "schema-v1",
    })
  );
  await page.route("**/api/nl2sql/profiles/search*", (route) =>
    fulfill(route, { items: [], next_cursor: null, total: 0, change_token: 1 })
  );

  await page.goto("/query");

  await expect(
    page.getByText("利用可能な業務プロファイルがありません。管理者に権限付与を依頼してください。")
  ).toBeVisible({ timeout: 45_000 });
  await expect(page.getByRole("button", { name: "業務プロファイルを作成" })).toHaveCount(0);
});

test("SQL 生成だけのユーザーは空 schema 失敗時にサンプルデータ投入ボタンを表示しない", async ({ page }) => {
  test.slow();
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/rewrite", (route) => {
    // rewrite 既定 ON。未モックだと submit が最初の rewrite で失敗する。
    const body = route.request().postDataJSON() as { question?: string };
    const original = String(body?.question ?? "");
    return fulfill(route, {
      original_question: original,
      rewritten_question: original,
      source: "deterministic",
      model: "",
      warnings: [],
    });
  });
  const limited = {
    ...systemAdminMe,
    user_uuid: "query-only-sample-readonly",
    login_user_id: "query.only.sample",
    display_name: "SQL 生成ユーザー",
    role_codes: ["QUERY_MENU"],
    is_system_admin: false,
    permissions: ["menu.query"],
    password_change_allowed: true,
  };
  let sampleImportRequested = false;
  await page.route("**/api/auth/me", (route) => fulfill(route, limited));
  await page.route("**/api/nl2sql/history**", (route) => fulfill(route, { items: [], next_cursor: null }));
  await page.route("**/api/schema/catalog", (route) =>
    fulfill(route, { refreshed_at: "2026-08-20T00:00:00Z", tables: [] })
  );
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfill(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-empty",
      refreshed_at: "2026-08-20T00:00:00Z",
      object_count: 0,
      column_count: 0,
      change_token: 1,
      etag: "schema-empty",
    })
  );
  await page.route("**/api/schema/objects**", (route) =>
    fulfill(route, {
      items: [],
      next_cursor: null,
      total: 0,
      table_count: 0,
      view_count: 0,
      counts_included: true,
      refreshed_at: "2026-08-20T00:00:00Z",
      catalog_version: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/search*", (route) =>
    fulfill(route, {
      items: [
        {
          id: "default",
          name: "標準プロファイル",
          category: "",
          description: "",
          archived: false,
          allowed_table_count: 0,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: "profile-default-v1",
          updated_at: "2026-08-20T00:00:00Z",
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    fulfill(route, {
      id: "default",
      name: "標準プロファイル",
      category: "",
      description: "",
      allowed_tables: [],
      allowed_views: [],
      archived: false,
      object_scope_version: 1,
      version: 1,
      etag: "profile-default-v1",
      updated_at: "2026-08-20T00:00:00Z",
    })
  );
  await page.route("**/api/nl2sql/sample-data/import", (route) => {
    sampleImportRequested = true;
    return fulfill(route, { executed: true });
  });

  const createdAt = "2026-08-20T00:00:00Z";
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfill(route, {
      job_id: "job-empty-readonly-001",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );
  await page.route("**/api/nl2sql/jobs/job-empty-readonly-001", (route) =>
    fulfill(route, {
      job_id: "job-empty-readonly-001",
      status: "error",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: createdAt,
      elapsed_ms: 30,
      result: null,
      error_message:
        "NL2SQL ジョブに失敗しました: Schema catalog が空です。Oracle schema を refresh するか、Data Tools から sample data を明示的に import してください。",
      timing: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "error", elapsed_ms: 5 },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );

  await page.goto("/query");
  await expect(page.locator("#nl2sql-profile-select")).toContainText("標準プロファイル");
  await page.locator("#nl2sql-question-input").fill("すべてプロジェクトを教えてください。");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const progress = page.getByTestId("nl2sql-job-progress");
  await expect(progress).toHaveAttribute("data-job-status", "error");
  await expect(progress.getByRole("button", { name: "サンプルデータを投入" })).toHaveCount(0);
  await expect(
    progress.getByText("スキーマが空です。管理者にサンプルデータの投入またはスキーマ更新を依頼してください。")
  ).toBeVisible();
  expect(sampleImportRequested).toBe(false);
});

test("管理者がユーザーを作成して単一ロールを割り当て、一時パスワードを一度だけ確認する", async ({ page, context }) => {
  await mockDatabaseGateReady(page);
  await context.addCookies([{ name: "pr_csrf", value: "csrf-token", url: "http://127.0.0.1:3201" }]);
  await page.addInitScript(() => {
    const state = globalThis as typeof globalThis & {
      __copiedOneTimePassword?: string;
      __copyOneTimePasswordShouldFail?: boolean;
    };
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value: string) => {
          if (state.__copyOneTimePasswordShouldFail) {
            throw new Error("clipboard denied");
          }
          state.__copiedOneTimePassword = value;
        },
      },
    });
  });
  let csrfObserved = false;
  let createRequestCount = 0;
  let successfulCreateCount = 0;
  let failNextCreate = false;
  let holdNextCreate = false;
  let heldCreate = Promise.resolve();
  let releaseHeldCreate: () => void = () => undefined;
  const createdPayloads: Array<{ loginUserId: string; roleIds: string[] }> = [];
  const generatedPasswords = [
    "RandomStrong!Pass123-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdefghijklmnopqrstuvwxyz",
    "SecondStrong!Pass456-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdefghijklmnopqrstuvwxyz",
    "ThirdStrong!Pass789-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdefghijklmnopqrstuvwxyz",
  ];
  let users = [
    {
      user_uuid: "admin-user",
      login_user_id: "SYSTEM",
      display_name: "システム管理者",
      status: "ACTIVE",
      force_password_change: false,
      locked_until: null,
      version: 1,
      role_ids: ["role-system"],
      is_bootstrap_admin: true,
    },
  ];
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "QUERY_VIEWER",
    display_name: "検索閲覧",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  const runnerRole = {
    ...systemRole,
    role_id: "role-runner",
    role_code: "QUERY_RUNNER",
    display_name: "検索実行",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, viewerRole, runnerRole])
  );
  await page.route("**/api/security/users", async (route) => {
    if (route.request().method() === "GET") {
      await fulfill(route, users);
      return;
    }
    createRequestCount += 1;
    csrfObserved = route.request().headers()["x-csrf-token"] === "csrf-token";
    const payload = route.request().postDataJSON() as {
      login_user_id: string;
      display_name: string;
      role_ids: string[];
    };
    createdPayloads.push({
      loginUserId: payload.login_user_id,
      roleIds: payload.role_ids,
    });
    if (holdNextCreate) {
      holdNextCreate = false;
      await heldCreate;
    }
    if (failNextCreate) {
      failNextCreate = false;
      const response = problemEnvelope({
        status: 503,
        code: "SECURITY_SERVICE_UNAVAILABLE",
        title: "サービスを一時的に利用できません",
        detail: "ユーザーを作成できません。時間をおいて再試行してください。",
        requestId: "inline-password-create-failed",
        retryable: true,
      });
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        headers: { "X-Request-ID": response.problem.request_id },
        body: JSON.stringify(response),
      });
      return;
    }
    successfulCreateCount += 1;
    const user = {
      user_uuid: `new-user-${successfulCreateCount}`,
      login_user_id: payload.login_user_id,
      display_name: payload.display_name,
      status: "ACTIVE",
      force_password_change: true,
      locked_until: null,
      version: 1,
      role_ids: payload.role_ids,
      is_bootstrap_admin: false,
    };
    users = [...users, user];
    await fulfill(route, {
      user,
      temporary_password: generatedPasswords[successfulCreateCount - 1],
    });
  });

  await page.goto("/settings/security/users");
  await page.getByTestId("security-users-actions").getByRole("button", { name: "新規作成" }).click();
  await page.getByLabel("ログインユーザーID").fill("001");
  await page.getByLabel("表示名").fill("短いログインユーザーIDユーザー");
  await expect(page.getByTestId("security-users-role-selection-actions")).toHaveCount(0);
  // システム管理者のロールは選択肢に出さず、理由を添える（#1792）。
  await expect(page.getByRole("radio", { name: /システム管理者/ })).toHaveCount(0);
  await expect(page.getByTestId("security-users-role-system-admin-note")).toHaveText("システム管理者のロールは構成管理者（system_admin）だけが持つため、ここでは割り当てられません。");
  const createButton = page.getByTestId("security-users-submit");
  await createButton.click();
  await expect(page.getByText("ロールを選択してください。", { exact: true })).toBeVisible();
  expect(createRequestCount).toBe(0);

  const viewerRadio = page.getByRole("radio", { name: /検索閲覧/ });
  const runnerRadio = page.getByRole("radio", { name: /検索実行/ });
  await expect(viewerRadio).toHaveAttribute("type", "radio");
  await expect(runnerRadio).toHaveAttribute("type", "radio");
  await viewerRadio.check();
  await expect(viewerRadio).toBeChecked();
  await expect(runnerRadio).not.toBeChecked();
  await runnerRadio.check();
  await expect(runnerRadio).toBeChecked();
  await expect(viewerRadio).not.toBeChecked();
  await viewerRadio.check();
  heldCreate = new Promise<void>((resolve) => {
    releaseHeldCreate = resolve;
  });
  holdNextCreate = true;
  await createButton.click();
  await expect(createButton).toBeDisabled();
  await page.keyboard.press("Enter");
  expect(createRequestCount).toBe(1);
  releaseHeldCreate();

  const loginInput = page.getByLabel("ログインユーザーID");
  const displayNameInput = page.getByLabel("表示名");
  const temporaryPasswordInput = page.getByLabel("一時パスワード", { exact: true });
  const copyButton = page.getByRole("button", { name: "一時パスワードをコピー" });
  await expect(page.locator("#security-users-panel-create")).toHaveCount(0);
  await expect(page.locator("#security-users-panel-edit")).toBeVisible();
  await expect(page.getByRole("heading", { name: "ユーザーを編集" })).toBeVisible();
  await expect(page.getByTestId("security-users-one-time-password")).toHaveCount(0);
  await expect(loginInput).toHaveValue("001");
  await expect(loginInput).toBeDisabled();
  await expect(loginInput).toHaveClass(/disabled:bg-surface-disabled/u);
  await expect(displayNameInput).toHaveValue("短いログインユーザーIDユーザー");
  await expect(temporaryPasswordInput).toHaveValue(generatedPasswords[0]);
  await expect(temporaryPasswordInput).toHaveAttribute("readonly", "");
  await expect(temporaryPasswordInput).toHaveClass(/read-only:bg-surface-sunken/u);
  await expect(viewerRadio).toBeChecked();
  await expect(runnerRadio).not.toBeChecked();
  // 作成後は同じ PageHeader の右端のボタンが「保存」になる（#618）。
  await expect(createButton).toHaveText("保存");
  await expect(page.getByText("変更を保存しました。", { exact: true }).last()).toBeVisible();

  await copyButton.click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (globalThis as typeof globalThis & { __copiedOneTimePassword?: string })
            .__copiedOneTimePassword
      )
    )
    .toBe(generatedPasswords[0]);
  // 成功はボタンの Check と読み上げ（共有の CopyButton。#2099）。
  await expect(copyButton).toHaveAttribute("data-copy-state", "copied");
  await expect(page.getByRole("status").filter({ hasText: "コピーしました" })).toHaveCount(1);

  await page.evaluate(() => {
    (
      globalThis as typeof globalThis & { __copyOneTimePasswordShouldFail?: boolean }
    ).__copyOneTimePasswordShouldFail = true;
  });
  await copyButton.click();
  await expect(page.getByTestId("security-user-temporary-password-error")).toContainText(
    "ブラウザのクリップボード権限を確認するか、表示内容を手動で選択してください。"
  );

  await page.evaluate(() => {
    (
      globalThis as typeof globalThis & { __copyOneTimePasswordShouldFail?: boolean }
    ).__copyOneTimePasswordShouldFail = false;
  });
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByTestId("security-users-actions").getByRole("button", { name: "新規作成" }).click();
  await loginInput.fill("002");
  await displayNameInput.fill("連続作成ユーザー");
  await viewerRadio.check();
  await createButton.click();
  await expect(page.locator("#security-users-panel-edit")).toBeVisible();
  await expect(loginInput).toHaveValue("002");
  await expect(displayNameInput).toHaveValue("連続作成ユーザー");
  await expect(temporaryPasswordInput).toHaveValue(generatedPasswords[1]);

  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByTestId("security-users-actions").getByRole("button", { name: "新規作成" }).click();
  await loginInput.fill("003");
  await displayNameInput.fill("再試行ユーザー");
  await viewerRadio.check();
  failNextCreate = true;
  await createButton.click();
  await expect(page.getByTestId("security-users-save-error")).toContainText(
    "ユーザーを作成できません。時間をおいて再試行してください。"
  );
  await expect(page.getByTestId("security-users-one-time-password")).toHaveCount(0);
  await expect(page.getByTestId("security-user-temporary-password")).toHaveValue("");
  await expect(loginInput).toHaveValue("003");
  await expect(displayNameInput).toHaveValue("再試行ユーザー");

  await createButton.click();
  await expect(page.locator("#security-users-panel-edit")).toBeVisible();
  await expect(loginInput).toHaveValue("003");
  await expect(displayNameInput).toHaveValue("再試行ユーザー");
  await expect(temporaryPasswordInput).toHaveValue(generatedPasswords[2]);
  await expect(temporaryPasswordInput).not.toHaveValue(generatedPasswords[1]);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
  await expectNoElementHorizontalOverflow(page.locator("#security-users-panel-edit"));
  await expect(copyButton).toBeVisible();
  await expect(page.getByRole("button", { name: "保存", exact: true })).toBeVisible();
  expect(csrfObserved).toBe(true);
  expect(createRequestCount).toBe(4);
  expect(successfulCreateCount).toBe(3);
  expect(createdPayloads[0]).toEqual({
    loginUserId: "001",
    roleIds: ["role-viewer"],
  });
  expect(createdPayloads.at(-1)).toEqual({
    loginUserId: "003",
    roleIds: ["role-viewer"],
  });
  expect(users.some((user) => user.login_user_id === "001")).toBe(true);
  expect(users.some((user) => user.login_user_id === "003")).toBe(true);
});

test("ユーザー作成の problem field error は入力直下だけに表示し、局所クリアと request ID を保つ", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "QUERY_VIEWER",
    display_name: "検索閲覧",
    is_built_in: false,
    permissions: ["menu.query"],
  };
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [viewerRole])
  );
  let submitCount = 0;
  await page.route("**/api/security/users", async (route) => {
    if (route.request().method() === "GET") {
      await fulfill(route, []);
      return;
    }
    submitCount += 1;
    const response =
      submitCount === 1
        ? problemEnvelope({
            status: 409,
            code: "SECURITY_USER_LOGIN_ID_CONFLICT",
            title: "ユーザーを作成できません",
            detail: "このログインユーザーIDは既に使用されています。別のIDを入力してください。",
            requestId: "user-conflict-request",
            fieldErrors: [
              {
                pointer: "/login_user_id",
                code: "already_exists",
                message: "別のログインユーザーIDを入力してください。",
              },
            ],
          })
        : submitCount === 2
          ? problemEnvelope({
              status: 422,
              code: "REQUEST_VALIDATION_FAILED",
              title: "入力内容を確認してください",
              detail: "入力内容に誤りがあります。該当項目を確認してください。",
              requestId: "user-validation-request",
              fieldErrors: [
                { pointer: "/login_user_id", code: "invalid", message: "IDを確認してください。" },
                { pointer: "/display_name", code: "invalid", message: "表示名を確認してください。" },
              ],
            })
          : problemEnvelope({
              status: 503,
              code: "SECURITY_SERVICE_UNAVAILABLE",
              title: "サービスを一時的に利用できません",
              detail: "ユーザーを作成できません。時間をおいて再試行してください。",
              requestId: "user-service-request",
              retryable: true,
            });
    await route.fulfill({
      status: response.problem.status,
      contentType: "application/json",
      headers: { "X-Request-ID": response.problem.request_id },
      body: JSON.stringify(response),
    });
  });

  await page.goto("/settings/security/users");
  await page.getByTestId("security-users-actions").getByRole("button", { name: "新規作成" }).click();
  const loginInput = page.getByLabel("ログインユーザーID");
  const displayNameInput = page.getByLabel("表示名");
  await loginInput.fill("duplicate.user");
  await displayNameInput.fill("重複ユーザー");
  await page.getByRole("radio", { name: /検索閲覧/ }).check();
  const submit = page.getByTestId("security-users-submit");
  await submit.click();

  const conflictMessage = "このログインユーザーIDは既に使用されています。別のIDを入力してください。";
  await expect(page.getByText(conflictMessage, { exact: true })).toHaveCount(1);
  await expect(loginInput).toHaveAttribute("aria-invalid", "true");
  // エラーは共有の TextField が欄の下に出し、aria-describedby で結び付ける（id は useId を含む。#631）。
  await expect(loginInput).toHaveAccessibleDescription(/このログインユーザーIDは既に使用されています。/);
  await expect(loginInput).toBeFocused();
  await expect(page.getByTestId("security-users-save-error").getByText(conflictMessage, { exact: true })).toHaveCount(0);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
  await expectNoElementHorizontalOverflow(page.locator("#security-users-panel-create"));
  await loginInput.fill("available.user");
  await expect(loginInput).not.toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText(conflictMessage, { exact: true })).toHaveCount(0);

  await submit.click();
  await expect(loginInput).toBeFocused();
  await expect(page.getByText("IDを確認してください。", { exact: true })).toBeVisible();
  await expect(page.getByText("表示名を確認してください。", { exact: true })).toBeVisible();
  await displayNameInput.fill("利用可能な表示名");
  await expect(page.getByText("表示名を確認してください。", { exact: true })).toHaveCount(0);
  await expect(page.getByText("IDを確認してください。", { exact: true })).toBeVisible();
  await loginInput.fill("final.user");
  await expect(page.getByText("IDを確認してください。", { exact: true })).toHaveCount(0);

  await submit.click();
  await expect(page.getByTestId("security-users-save-error")).toContainText(
    "ユーザーを作成できません。時間をおいて再試行してください。"
  );
  await expect(page.getByTestId("security-users-save-error")).toContainText(
    "リクエストID: user-service-request"
  );
  expect(submitCount).toBe(3);
});

test("ユーザー一覧と詳細のパスワードリセット結果を編集フォームに表示する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "QUERY_VIEWER",
    display_name: "検索閲覧",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  const users = [
    {
      user_uuid: "admin-user",
      login_user_id: "SYSTEM",
      display_name: "システム管理者",
      status: "ACTIVE",
      force_password_change: false,
      locked_until: null,
      version: 1,
      role_ids: ["role-system"],
      is_bootstrap_admin: true,
    },
    {
      user_uuid: "sales-user",
      login_user_id: "sales.user",
      display_name: "営業ユーザー",
      status: "ACTIVE",
      force_password_change: true,
      locked_until: null,
      version: 1,
      role_ids: ["role-viewer"],
      is_bootstrap_admin: false,
    },
  ];
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, viewerRole])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, users));
  let resetRequestCount = 0;
  await page.route("**/api/security/users/sales-user/reset-password", async (route) => {
    resetRequestCount += 1;
    await fulfill(route, {
      user: { ...users[1], force_password_change: true },
      temporary_password: `ListResetStrong!Pass-${resetRequestCount}`,
    });
  });

  // 一覧と詳細を並べて行を選び直す操作を確かめる（xl 未満の 1 ペインでは選ぶと一覧が隠れる。#2282）。
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/security/users");
  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const adminRow = grid.locator("tbody tr").filter({ hasText: "システム管理者" });
  await adminRow.locator("td").first().click();
  const detailActions = page.getByTestId("security-users-detail-actions");
  // 詳細の見出しは表示名（ログイン ID を出さない。#1696）。
  await expect(page.getByRole("heading", { name: "システム管理者", exact: true })).toBeVisible();
  await expect(detailActions.getByRole("button", { name: "編集" })).toBeVisible();
  await expect(
    detailActions.getByRole("button", { name: "パスワードをリセット" })
  ).toHaveCount(0);
  const adminMoreButton = detailActions.getByRole("button", { name: "その他の操作" });
  await expect(adminMoreButton).toBeVisible();
  await adminMoreButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: "無効化" })).toBeFocused();
  await expect(page.getByRole("menuitem", { name: "パスワードをリセット" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(adminMoreButton).toBeFocused();
  await expectNoPageHorizontalScroll(page);

  const salesRow = grid.locator("tbody tr").filter({ hasText: "営業ユーザー" });
  await salesRow.locator("td").first().click();
  const salesDetailActions = page.getByTestId("security-users-detail-actions");
  const salesResetButton = salesDetailActions.getByRole("button", { name: "パスワードをリセット" });
  await expect(salesDetailActions.getByRole("button", { name: "編集" })).toBeVisible();
  await expect(salesResetButton).toBeVisible();
  await salesDetailActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "無効化" })).toBeFocused();
  await page.keyboard.press("Escape");
  // Escape の後、メニューは次の frame でトリガーへ focus を戻す（restoreMenuTriggerFocus）。
  // 戻る前に同じ操作の行の「パスワードをリセット」へ移すと、戻す処理に奪い返され、
  // 続く Enter が「その他の操作」のメニューを開き直す。戻ったのを待ってから移す（#1141）。
  await expect(salesDetailActions.getByRole("button", { name: "その他の操作" })).toBeFocused();
  await salesResetButton.focus();
  await page.keyboard.press("Enter");
  await page
    .getByRole("alertdialog", { name: "パスワードをリセット" })
    .getByRole("button", { name: "実行" })
    .click();
  await expect(page.locator("#security-users-panel-edit")).toBeVisible();
  await expect(page.getByLabel("ログインユーザーID")).toHaveValue("sales.user");
  await expect(page.getByLabel("ログインユーザーID")).toBeDisabled();
  await expect(page.getByLabel("一時パスワード", { exact: true })).toHaveValue(
    "ListResetStrong!Pass-1"
  );
  await expect(page.getByLabel("一時パスワード", { exact: true })).toHaveAttribute(
    "readonly",
    ""
  );
  await expect(page.getByTestId("security-users-one-time-password")).toHaveCount(0);

  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await salesRow.locator("td").first().click();
  await expect(salesDetailActions.getByRole("button", { name: "編集" })).toBeVisible();
  await expect(salesDetailActions.getByRole("button", { name: "パスワードをリセット" })).toBeVisible();
  await expect(salesDetailActions.getByRole("button", { name: "その他の操作" })).toBeVisible();
  await salesDetailActions.getByRole("button", { name: "パスワードをリセット" }).click();
  await page
    .getByRole("alertdialog", { name: "パスワードをリセット" })
    .getByRole("button", { name: "実行" })
    .click();
  await expect(page.locator("#security-users-panel-edit")).toBeVisible();
  await expect(page.getByLabel("一時パスワード", { exact: true })).toHaveValue(
    "ListResetStrong!Pass-2"
  );
  expect(resetRequestCount).toBe(2);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoElementHorizontalOverflow(page.locator("#security-users-panel-edit"));
  await expectNoPageHorizontalScroll(page);
});

test("ユーザー編集はパスワードリセットと無効化・有効化をフォーム操作に統合する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.addInitScript(() => {
    const state = globalThis as typeof globalThis & { __copiedEditPassword?: string };
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value: string) => {
          state.__copiedEditPassword = value;
        },
      },
    });
  });

  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "QUERY_VIEWER",
    display_name: "検索閲覧",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  let user = {
    user_uuid: "sales-user",
    login_user_id: "sales.user",
    display_name: "営業ユーザー",
    status: "ACTIVE" as "ACTIVE" | "DISABLED",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: ["role-viewer"],
    is_bootstrap_admin: false,
  };
  let resetRequestCount = 0;
  let resetPayload: unknown = null;
  let failNextReset = false;
  let updateRequestCount = 0;
  const disableVersions: number[] = [];
  const enableVersions: number[] = [];
  let failNextDisable = false;

  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, viewerRole])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, [user]));
  await page.route("**/api/security/users/sales-user", async (route) => {
    updateRequestCount += 1;
    await fulfill(route, user);
  });
  await page.route("**/api/security/users/sales-user/*", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/reset-password")) {
      resetRequestCount += 1;
      resetPayload = route.request().postDataJSON();
      if (failNextReset) {
        failNextReset = false;
        await fulfill(
          route,
          "一時パスワードを再発行できませんでした。接続状態と権限を確認して再試行してください。",
          503
        );
        return;
      }
      user = { ...user, force_password_change: true };
      await fulfill(route, {
        user,
        temporary_password: "EditResetStrong!Pass-1",
      });
      return;
    }
    const payload = route.request().postDataJSON() as { version: number };
    if (pathname.endsWith("/disable")) {
      disableVersions.push(payload.version);
      if (failNextDisable) {
        failNextDisable = false;
        await fulfill(route, "最後のシステム管理者は無効化または権限解除できません。", 409);
        return;
      }
      user = { ...user, status: "DISABLED", version: user.version + 1 };
      await fulfill(route, user);
      return;
    }
    if (pathname.endsWith("/enable")) {
      enableVersions.push(payload.version);
      user = { ...user, status: "ACTIVE", version: user.version + 1 };
      await fulfill(route, user);
      return;
    }
    await fulfill(route, "未対応のユーザー操作です。", 404);
  });

  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/settings/security/users");
  const salesRow = page
    .getByTestId("security-users-grid")
    .locator("tbody tr")
    .filter({ hasText: "営業ユーザー" });
  await salesRow.locator("td").first().click();
  await page
    .getByTestId("security-users-detail-actions")
    .getByRole("button", { name: "編集" })
    .click();

  const editPanel = page.locator("#security-users-panel-edit");
  const editActions = page.getByRole("group", { name: "ユーザー編集操作" });
  const displayName = page.getByLabel("表示名");
  const temporaryPassword = page.getByLabel("一時パスワード", { exact: true });
  const copyTemporaryPassword = page.getByRole("button", {
    name: "一時パスワードをコピー",
  });
  const assignedRole = page.getByLabel("検索閲覧");
  await expect(editPanel).toBeVisible();
  await expect(page.getByTestId("security-users-submit")).toHaveText("保存");
  await expect(editActions.getByRole("button", { name: "パスワードをリセット" })).toBeVisible();
  await expect(editActions.getByRole("button", { name: "その他の操作" })).toBeVisible();
  await expect(temporaryPassword).toHaveValue("");
  await expect(temporaryPassword).toHaveAttribute("readonly", "");
  await expect(temporaryPassword).toHaveClass(/read-only:bg-surface-sunken/u);
  await expect(copyTemporaryPassword).toBeDisabled();
  await displayName.fill("未保存の営業ユーザー");

  await editActions.getByRole("button", { name: "パスワードをリセット" }).click();
  await page
    .getByRole("alertdialog", { name: "パスワードをリセット" })
    .getByRole("button", { name: "キャンセル" })
    .click();
  expect(resetRequestCount).toBe(0);
  await expect(displayName).toHaveValue("未保存の営業ユーザー");

  await editActions.getByRole("button", { name: "パスワードをリセット" }).click();
  await page
    .getByRole("alertdialog", { name: "パスワードをリセット" })
    .getByRole("button", { name: "実行" })
    .click();
  await expect(page.getByTestId("security-users-edit-reset-password-region")).toHaveCount(0);
  await expect(temporaryPassword).toHaveValue("EditResetStrong!Pass-1");
  await expect(page.getByText("パスワードをリセットしました。", { exact: true }).last()).toBeVisible();
  expect(resetPayload).toEqual({ temporary_password: null });
  await expect(displayName).toHaveValue("未保存の営業ユーザー");
  await expect(copyTemporaryPassword).toBeEnabled();
  await copyTemporaryPassword.click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (globalThis as typeof globalThis & { __copiedEditPassword?: string })
            .__copiedEditPassword
      )
    )
    .toBe("EditResetStrong!Pass-1");

  failNextReset = true;
  await editActions.getByRole("button", { name: "パスワードをリセット" }).click();
  await page
    .getByRole("alertdialog", { name: "パスワードをリセット" })
    .getByRole("button", { name: "実行" })
    .click();
  await expect(page.getByTestId("security-user-temporary-password-error")).toContainText(
    "接続状態と権限を確認して再試行してください。"
  );
  await expect(temporaryPassword).toHaveValue("EditResetStrong!Pass-1");
  await expect(displayName).toHaveValue("未保存の営業ユーザー");
  await expect(page.locator("main > [role='alert']")).toHaveCount(0);

  const moreButton = editActions.getByRole("button", { name: "その他の操作" });
  await moreButton.focus();
  await page.keyboard.press("Enter");
  const disableItem = page.getByRole("menuitem", { name: "無効化" });
  await expect(disableItem).toBeFocused();
  await expect(disableItem).toHaveAttribute("data-entity-action-tone", "danger");
  await page.keyboard.press("Escape");
  await expect(moreButton).toBeFocused();

  await moreButton.click();
  await page.getByRole("menuitem", { name: "無効化" }).click();
  await page
    .getByRole("alertdialog", { name: "無効化" })
    .getByRole("button", { name: "キャンセル" })
    .click();
  expect(disableVersions).toEqual([]);

  await moreButton.click();
  await page.getByRole("menuitem", { name: "無効化" }).click();
  await page
    .getByRole("alertdialog", { name: "無効化" })
    .getByRole("button", { name: "実行" })
    .click();
  await expect(editActions.getByRole("button", { name: "有効化" })).toBeVisible();
  await expect(page.getByTestId("security-users-submit")).toHaveCount(0);
  await expect(
    editActions.getByRole("button", { name: "パスワードをリセット" })
  ).toHaveCount(0);
  await expect(displayName).toBeDisabled();
  await expect(temporaryPassword).toBeDisabled();
  await expect(copyTemporaryPassword).toBeDisabled();
  // システム管理者のロールは選択肢に出さない（#1792）。
  await expect(page.getByRole("radio")).toHaveCount(1);
  await expect(assignedRole).toBeVisible();
  await expect(assignedRole).toBeChecked();
  await expect(assignedRole).toBeDisabled();
  await expect(editActions.getByRole("button", { name: "その他の操作" })).toBeVisible();
  await page.getByRole("button", { name: "一覧へ戻る" }).focus();
  await page.keyboard.press("Tab");
  await expect(editActions.getByRole("button", { name: "有効化" })).toBeFocused();
  await editPanel.locator("form").evaluate((form) =>
    (form as HTMLFormElement).requestSubmit()
  );
  expect(updateRequestCount).toBe(0);
  expect(resetRequestCount).toBe(2);
  await editActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "削除" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(displayName).toHaveValue("未保存の営業ユーザー");
  await expect(page.getByText("ユーザーを無効化しました。既存セッションは失効しました。", { exact: true }).last()).toBeVisible();
  expect(disableVersions).toEqual([1]);

  await editActions.getByRole("button", { name: "有効化" }).click();
  await expect(editActions.getByRole("button", { name: "その他の操作" })).toBeVisible();
  await expect(editActions.getByRole("button", { name: "有効化" })).toHaveCount(0);
  await expect(page.getByTestId("security-users-submit")).toHaveText("保存");
  await expect(
    editActions.getByRole("button", { name: "パスワードをリセット" })
  ).toBeVisible();
  await expect(displayName).toBeEnabled();
  await expect(temporaryPassword).toBeEnabled();
  await expect(temporaryPassword).toHaveAttribute("readonly", "");
  await expect(copyTemporaryPassword).toBeEnabled();
  await expect(assignedRole).toBeEnabled();
  await expect(assignedRole).toBeChecked();
  await expect(page.getByText("ユーザーを有効化しました。次回ログインから利用できます。", { exact: true }).last()).toBeVisible();
  await expect(displayName).toHaveValue("未保存の営業ユーザー");
  expect(enableVersions).toEqual([2]);

  failNextDisable = true;
  await editActions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "無効化" }).click();
  await page
    .getByRole("alertdialog", { name: "無効化" })
    .getByRole("button", { name: "実行" })
    .click();
  // 操作の失敗もヘッダーの直下の Banner の 1 か所（#585 / #618）。
  await expect(page.getByTestId("security-users-save-error")).toContainText("最後のシステム管理者は無効化または権限解除できません。");
  await expect(displayName).toHaveValue("未保存の営業ユーザー");
  expect(disableVersions).toEqual([1, 3]);

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByTestId("security-users-submit")).toHaveText("保存");
  await expect(editActions.getByRole("button", { name: "パスワードをリセット" })).toBeVisible();
  await expectNoPageHorizontalScroll(page);
});

test("無効ユーザーの削除は確認・フォーカス復帰を保ち、削除後は選択を外す", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const viewerRole = {
    ...systemRole,
    role_id: "role-delete-viewer",
    role_code: "DELETE_VIEWER",
    display_name: "削除確認ロール",
    is_built_in: false,
    permissions: ["menu.query"],
  };
  const activeUser = {
    user_uuid: "active-delete-user",
    login_user_id: "active.user",
    display_name: "有効ユーザー",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 4,
    role_ids: [viewerRole.role_id],
    assigned_roles: [],
    is_bootstrap_admin: false,
  };
  const disabledUser = {
    ...activeUser,
    user_uuid: "disabled-delete-user",
    login_user_id: "disabled.user",
    display_name: "削除対象ユーザー",
    status: "DISABLED",
    version: 7,
  };
  let users = [activeUser, disabledUser];
  let deleteCount = 0;
  let observedIfMatch = "";

  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [viewerRole])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, users));
  await page.route("**/api/security/users/disabled-delete-user", async (route) => {
    deleteCount += 1;
    observedIfMatch = route.request().headers()["if-match"] ?? "";
    users = users.filter((user) => user.user_uuid !== disabledUser.user_uuid);
    await fulfill(route, {
      deleted: true,
      user_uuid: disabledUser.user_uuid,
      login_user_id: disabledUser.login_user_id,
    });
  });

  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/settings/security/users");

  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const activeRow = grid.locator("tbody tr").filter({ hasText: "有効ユーザー" });
  await activeRow.locator("td").first().click();
  const detailActions = page.getByTestId("security-users-detail-actions");
  await detailActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "削除" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  const disabledRow = grid.locator("tbody tr").filter({ hasText: "削除対象ユーザー" });
  await disabledRow.locator("td").first().click();
  // 選択は URL を経て詳細に反映される。前の行（有効ユーザー）の詳細のメニューを開かないよう、
  // 削除対象ユーザーの詳細の操作に置き換わってからフォーカスする。
  const deleteTrigger = detailActions.getByRole("button", {
    name: "その他の操作（操作: 削除対象ユーザー）",
  });
  await expect(deleteTrigger).toBeVisible();
  await deleteTrigger.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("End");
  const deleteMenuItem = page.getByRole("menuitem", { name: "削除" });
  await expect(deleteMenuItem).toBeFocused();
  await page.keyboard.press("Enter");

  const deleteDialog = page.getByRole("alertdialog", { name: "削除" });
  await expect(deleteDialog).toContainText(
    "「削除対象ユーザー」を完全に削除します。"
  );
  await expect(deleteDialog).not.toContainText("disabled.user");
  await expect(deleteDialog).toContainText("割り当てロール、既存セッションは削除され");
  await page.mouse.click(4, 4);
  await expect(deleteDialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(deleteDialog).toHaveCount(0);
  await expect(deleteTrigger).toBeFocused();
  expect(deleteCount).toBe(0);

  await deleteTrigger.click();
  await page.getByRole("menuitem", { name: "削除" }).click();
  await deleteDialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(deleteTrigger).toBeFocused();
  expect(deleteCount).toBe(0);

  await deleteTrigger.click();
  await page.getByRole("menuitem", { name: "削除" }).click();
  await deleteDialog.getByRole("button", { name: "削除", exact: true }).click();

  await expect(page.getByText("削除対象ユーザー", { exact: true })).toHaveCount(0);
  // 削除した対象の選択を外し、ほかの行へ置き換えない（page-archetypes.md §1 B。#2282）。
  await expect(page).not.toHaveURL(/[?&]user=/u);
  await expect(grid.locator('tbody tr[data-selected="true"]')).toHaveCount(0);
  await expect(page.getByTestId("list-detail-security-users-detail")).toContainText("ユーザーを選択してください");
  await expect(page.getByText("削除対象ユーザー を完全に削除しました。", { exact: true }).last()).toBeVisible();
  expect(observedIfMatch).toBe('"7"');
  expect(deleteCount).toBe(1);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
});

test("詳細アクションメニューを閉じた直後に別要素へフォーカスしても奪い返さない", async ({
  page,
}) => {
  await mockDatabaseGateReady(page);
  const viewerRole = {
    ...systemRole,
    role_id: "role-focus-viewer",
    role_code: "FOCUS_VIEWER",
    display_name: "フォーカス確認ロール",
    is_built_in: false,
    permissions: ["menu.query"],
  };
  const firstUser = {
    user_uuid: "focus-first-user",
    login_user_id: "focus.first",
    display_name: "先のユーザー",
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: [viewerRole.role_id],
    assigned_roles: [],
    is_bootstrap_admin: false,
  };
  const secondUser = {
    ...firstUser,
    user_uuid: "focus-second-user",
    login_user_id: "focus.second",
    display_name: "後のユーザー",
    version: 2,
  };
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [viewerRole])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, [firstUser, secondUser]));

  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/settings/security/users");

  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const firstRow = grid.locator("tbody tr").filter({ hasText: "先のユーザー" });
  await firstRow.locator("td").first().click();
  const search = page.getByTestId("security-users-search");
  const trigger = page
    .getByTestId("security-users-detail-actions")
    .getByRole("button", { name: "その他の操作" });
  await trigger.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: "無効化" })).toBeFocused();

  // Escape で閉じた直後（フォーカス復帰の rAF が走る前）に検索欄へフォーカスを移す。
  await page.evaluate(() => {
    document.activeElement?.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true })
    );
    document.querySelector<HTMLInputElement>('[data-testid="security-users-search"]')?.focus();
  });

  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(search).toBeFocused();
  await expect(trigger).not.toBeFocused();
});

test("ユーザー管理は一覧 + 詳細（狭い一覧・広い詳細）で、作成・編集は同じパネルにする", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  const users = [
    {
      user_uuid: "admin-user",
      login_user_id: "SYSTEM",
      display_name: "システム管理者",
      status: "ACTIVE",
      force_password_change: false,
      locked_until: null,
      version: 1,
      role_ids: ["role-system"],
      is_bootstrap_admin: true,
    },
    {
      user_uuid: "sales-user",
      login_user_id: "sales.user",
      display_name: "営業ユーザー",
      status: "DISABLED",
      force_password_change: true,
      locked_until: "2026-07-21T03:00:00Z",
      version: 2,
      role_ids: ["role-viewer"],
      is_bootstrap_admin: false,
    },
  ];
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [
      systemRole,
      {
        ...systemRole,
        role_id: "role-viewer",
        role_code: "QUERY_VIEWER",
        display_name: "検索閲覧",
        is_built_in: false,
        permissions: ["menu.query"],
        data_entitlements: [],
      },
    ])
  );
  await page.route("**/api/security/users", (route) => fulfill(route, users));

  await page.setViewportSize({ width: 2048, height: 1000 });
  await page.goto("/settings/security/users");

  // 一覧 + 詳細（ListDetailLayout。#2282）: xl 以上は 2 ペイン、一覧の列は既定 20rem。
  const usersLayout = page.getByTestId("list-detail-security-users");
  await expect(usersLayout).toHaveAttribute("data-list-detail-layout", "split");
  const usersListWidth = await page.getByTestId("list-detail-security-users-list").evaluate((node) =>
    node.getBoundingClientRect().width / Number.parseFloat(getComputedStyle(document.documentElement).fontSize)
  );
  expect(Math.abs(usersListWidth - 20)).toBeLessThanOrEqual(0.1);
  await expect(page.getByTestId("security-users-grid")).toBeVisible();
  const grid = page.getByTestId("security-users-grid");
  await expect(grid.locator("tbody tr")).toHaveCount(2);
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const salesUserRow = grid.locator("tbody tr").filter({ hasText: "営業ユーザー" });
  const adminUserRow = grid.locator("tbody tr").filter({ hasText: "システム管理者" });
  // 先頭の行を自動で選ばず、詳細は空の状態（#2282）。
  await expect(grid.locator('tbody tr[data-selected="true"]')).toHaveCount(0);
  await expect(page.getByTestId("list-detail-security-users-detail")).toContainText("ユーザーを選択してください");
  await expect(grid).not.toContainText("sales.user");
  await expect(grid.getByRole("button", { name: "編集" })).toHaveCount(0);
  await adminUserRow.locator("td").first().click();
  // 一覧は表示名の順（システム管理者 < 営業ユーザー）、詳細見出しは表示名で、ログイン ID は出さない（#1696）。
  await expect(adminUserRow).toHaveAttribute("data-selected", "true");
  await expect(page).toHaveURL(/[?&]user=admin-user/u);
  await expect(page.getByRole("region", { name: "システム管理者" })).toBeVisible();
  const adminUserDetailActions = page.getByTestId("security-users-detail-actions");
  const adminMoreButton = adminUserDetailActions.getByRole("button", { name: "その他の操作" });
  await adminMoreButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: "パスワードをリセット" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "無効化" })).toBeFocused();
  await page.keyboard.press("End");
  await expect(page.getByRole("menuitem", { name: "無効化" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(adminMoreButton).toBeFocused();
  await expect(adminUserDetailActions.getByRole("button", { name: "編集" })).toBeVisible();
  await expect(adminUserDetailActions.getByRole("button", { name: "パスワードをリセット" })).toHaveCount(0);
  await expect(adminMoreButton).toBeVisible();
  await salesUserRow.locator("td").nth(1).click();
  await expect(salesUserRow).toHaveAttribute("data-selected", "true");
  await expect(page.locator("dl").getByText("ロック中", { exact: true })).toBeVisible();
  await expect(page.getByText("ロック期限", { exact: true })).toHaveCount(0);
  const salesUserDetailActions = page.getByTestId("security-users-detail-actions");
  await expect(page.getByRole("menuitem", { name: "パスワードをリセット" })).toHaveCount(0);
  await expect(
    salesUserDetailActions.getByRole("button", { name: "パスワードをリセット" })
  ).toHaveCount(0);
  await expect(salesUserDetailActions.getByRole("button", { name: "ロック解除" })).toBeVisible();
  await salesUserDetailActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "有効化" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "削除" })).toBeVisible();
  await page.keyboard.press("Escape");
  await salesUserDetailActions.getByRole("button", { name: "編集" }).click();
  const disabledUserEditActions = page.getByRole("group", { name: "ユーザー編集操作" });
  await expect(page.getByTestId("security-users-submit")).toHaveCount(0);
  await expect(
    disabledUserEditActions.getByRole("button", { name: "パスワードをリセット" })
  ).toHaveCount(0);
  await expect(disabledUserEditActions.getByRole("button", { name: "有効化" })).toBeVisible();
  await expect(page.getByLabel("表示名")).toBeDisabled();
  await expect(page.getByLabel("一時パスワード", { exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "一時パスワードをコピー" })).toBeDisabled();
  await expect(page.getByRole("radio")).toHaveCount(1);
  await expect(page.getByLabel("検索閲覧")).toBeVisible();
  await expect(page.getByLabel("検索閲覧")).toBeChecked();
  await expect(page.getByLabel("検索閲覧")).toBeDisabled();
  await expect(page.getByRole("radio", { name: /システム管理者/ })).toHaveCount(0);
  await expect(page.getByTestId("security-users-role-system-admin-note")).toHaveText("システム管理者のロールは構成管理者（system_admin）だけが持つため、ここでは割り当てられません。");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByTestId("security-users-search").fill("sales");
  await expect(page.getByTestId("security-users-grid").getByText("営業ユーザー")).toBeVisible();
  await expect(page.getByTestId("security-users-grid").getByText("システム管理者")).toHaveCount(0);
  await page.getByTestId("security-users-search").fill("");

  await page.getByTestId("security-users-actions").getByRole("button", { name: "新規作成" }).click();
  const formStyle = await topLevelPanelStyle(page, "create", "security-users");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(usersLayout).toBeVisible();

  await adminUserRow.locator("td").first().click();
  // 選択が切り替わるまで待つ。待たずに「編集」を押すと、前に選んだ利用者の編集を開く（#3233）。
  await expect(page).toHaveURL(/[?&]user=admin-user/u);
  await expect(adminUserRow).toHaveAttribute("data-selected", "true");
  await page.getByTestId("security-users-detail-actions").getByRole("button", { name: "編集" }).click();
  expect(await topLevelPanelStyle(page, "edit", "security-users")).toEqual(formStyle);
  // 既存の割り当てを持つユーザーの編集でも、システム管理者のロールは選択肢に出さない（#1792）。
  await expect(page.getByRole("radio", { name: /システム管理者/ })).toHaveCount(0);
  await expect(page.getByTestId("security-users-other-roles")).toContainText("システム管理者");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(usersLayout).toBeVisible();
  await page.setViewportSize({ width: 375, height: 812 });
  // xl 未満は 1 ペイン。選んでいる利用者の詳細だけを出す。
  await expect(usersLayout).toHaveAttribute("data-list-detail-view", "detail");
  await expectNoPageHorizontalScroll(page);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  await expect(page.getByTestId("security-users-detail-actions").getByRole("button", { name: "パスワードをリセット" })).toHaveCount(0);
  await expectNoPageHorizontalScroll(page);
});

test("ユーザー管理は削除済みの割り当てロールを ID（UUID）ではなく「（削除済みのロール）」と表示する（#1682）", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const deletedRoleId = "6f1c2d3e-4b5a-4c6d-8e7f-90a1b2c3d4e5";
  await page.route("**/api/security/roles?include_archived=false", (route) => fulfill(route, [systemRole]));
  await page.route("**/api/security/users", (route) =>
    fulfill(route, [
      {
        user_uuid: "deleted-role-user",
        login_user_id: "deleted.role.user",
        display_name: "削除済みロール利用者",
        status: "ACTIVE",
        force_password_change: false,
        locked_until: null,
        version: 1,
        role_ids: [deletedRoleId],
        // backend の AssignedRoleData.unresolved() の形（コード・表示名は空、deleted）。
        assigned_roles: [
          { role_id: deletedRoleId, role_code: "", display_name: "", is_built_in: false, archived: true, deleted: true },
        ],
        is_bootstrap_admin: false,
      },
    ])
  );

  await page.goto("/settings/security/users");

  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByText("（削除済みのロール）", { exact: true })).toBeVisible();
  await expect(page.locator("main")).not.toContainText(deletedRoleId);
  expect(await page.locator("main").innerHTML()).not.toContain(deletedRoleId);
});

test("ユーザー管理はアーカイブ済み割り当てロールを無効として表示し、新規選択肢には出さない", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "QUERY_VIEWER",
    display_name: "検索閲覧",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  const archivedAssignedRole = {
    role_id: "role-archived-user",
    role_code: "DATA_ADMIN",
    display_name: "データ管理者",
    is_built_in: false,
    archived: true,
  };
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, viewerRole])
  );
  await page.route("**/api/security/users", (route) =>
    fulfill(route, [
      {
        user_uuid: "archived-role-user",
        login_user_id: "archive.user",
        display_name: "アーカイブロール利用者",
        status: "ACTIVE",
        force_password_change: false,
        locked_until: null,
        version: 1,
        role_ids: [archivedAssignedRole.role_id],
        assigned_roles: [archivedAssignedRole],
        is_bootstrap_admin: false,
      },
    ])
  );

  // 一覧と詳細を並べて行を選び直す操作を確かめる（xl 未満の 1 ペインでは選ぶと一覧が隠れる。#2282）。
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/security/users?user=archived-role-user");

  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByText("データ管理者（アーカイブ済み・無効）", { exact: true })).toBeVisible();
  await expect(
    page.getByText("アーカイブ済みロールの権限は、このユーザーの実アクセス権には反映されません。", {
      exact: true,
    })
  ).toBeVisible();

  const inactiveRoleBadge = page.getByText("データ管理者（アーカイブ済み・無効）", { exact: true }).last();
  // ロールバッジは共有 StatusBadge(token 化)へ移行。variant で判定する。
  await expect(inactiveRoleBadge).toHaveAttribute("data-status-variant", "neutral");
  await page.getByTestId("security-users-detail-actions").getByRole("button", { name: "編集" }).click();
  await expect(page.getByRole("radio", { name: /検索閲覧/ })).toBeVisible();
  await expect(page.getByRole("radio", { name: /データ管理者/ })).toHaveCount(0);
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
});

test("ユーザー管理は復元済みロールを通常表示し、割り当て選択肢に戻す", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const restoredRole = {
    ...systemRole,
    role_id: "role-restored-user",
    role_code: "DATA_ADMIN",
    display_name: "データ管理者",
    is_built_in: false,
    archived: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  const restoredAssignedRole = {
    role_id: restoredRole.role_id,
    role_code: restoredRole.role_code,
    display_name: restoredRole.display_name,
    is_built_in: false,
    archived: false,
  };
  await page.route("**/api/security/roles?include_archived=false", (route) =>
    fulfill(route, [systemRole, restoredRole])
  );
  await page.route("**/api/security/users", (route) =>
    fulfill(route, [
      {
        user_uuid: "restored-role-user",
        login_user_id: "restore.user",
        display_name: "復元ロール利用者",
        status: "ACTIVE",
        force_password_change: false,
        locked_until: null,
        version: 1,
        role_ids: [restoredRole.role_id],
        assigned_roles: [restoredAssignedRole],
        is_bootstrap_admin: false,
      },
    ])
  );

  // 一覧と詳細を並べて行を選び直す操作を確かめる（xl 未満の 1 ペインでは選ぶと一覧が隠れる。#2282）。
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/security/users?user=restored-role-user");

  const grid = page.getByTestId("security-users-grid");
  await expect(grid.getByText("データ管理者", { exact: true })).toBeVisible();
  await expect(page.getByText("データ管理者（アーカイブ済み・無効）", { exact: true })).toHaveCount(0);
  await expect(
    page.getByText("アーカイブ済みロールの権限は、このユーザーの実アクセス権には反映されません。", {
      exact: true,
    })
  ).toHaveCount(0);
  await expect(page.getByText("データ管理者", { exact: true }).last()).toHaveAttribute(
    "data-status-variant",
    "info"
  );
  await page.getByTestId("security-users-detail-actions").getByRole("button", { name: "編集" }).click();
  await expect(page.getByRole("radio", { name: /データ管理者/ })).toBeVisible();
});

test("ロール管理はテーブル一覧と詳細で表示し、権限の選択は権限管理で行う", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  const permissionRows = [
    {
      code: "menu.settings_appearance",
      group: "システム設定",
      label: "外観と証明書",
      description: "外観と証明書を表示し、関連操作を利用できます。",
      implies: [],
    },
    {
      code: "menu.security_users",
      group: "セキュリティ管理",
      label: "ユーザー管理",
      description: "ユーザー管理を表示し、関連操作を利用できます。",
      implies: [],
    },
    {
      code: "menu.security_roles",
      group: "セキュリティ管理",
      label: "ロール・権限管理",
      description: "ロール・権限管理を表示し、関連操作を利用できます。",
      implies: [],
    },
  ];
  const viewerRole = {
    ...systemRole,
    role_id: "role-viewer",
    role_code: "SECURITY_VIEWER",
    display_name: "アプリ閲覧",
    description: "表示のみ",
    is_built_in: false,
    permissions: ["menu.security_users"],
    data_entitlements: [],
  };
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole, viewerRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));

  await page.setViewportSize({ width: 2048, height: 1000 });
  await page.goto("/settings/security/roles");

  // 一覧 + 詳細（ListDetailLayout。#2282）: xl 以上は 2 ペイン。
  const rolesLayout = page.getByTestId("list-detail-security-roles");
  await expect(rolesLayout).toHaveAttribute("data-list-detail-layout", "split");
  const grid = page.getByTestId("security-roles-grid");
  await expect(grid).toBeVisible();
  await expect(grid.getByRole("columnheader", { name: "ロール" })).toBeVisible();
  // 一覧の列は狭いので、説明は列にせず名前の下の補足 1 行に出す（#2282）。
  // ロール管理（共通画面）は権限を扱わない。権限は権限管理で設定する（#206）。
  await expect(grid.getByRole("columnheader", { name: "説明" })).toHaveCount(0);
  await expect(grid.getByRole("columnheader", { name: "機能権限" })).toHaveCount(0);
  // 先頭の行を自動で選ばない（#2282）。
  await expect(grid.locator('tbody tr[data-selected="true"]')).toHaveCount(0);
  await expect(page.getByTestId("list-detail-security-roles-detail")).toContainText("ロールを選択してください");
  await grid.locator("tbody tr").filter({ hasText: "アプリ閲覧" }).locator("td").first().click();
  await expect(page).toHaveURL(/[?&]role=role-viewer/u);
  await expect(page.getByTestId("security-roles-permission-summary")).toContainText(
    "付与している機能権限: 1 件"
  );
  await expect(page.getByTestId("security-roles-open-permissions")).toHaveAttribute(
    "href",
    "/settings/security/permissions?role=role-viewer"
  );
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "機能権限" })).toHaveCount(0);
  await expect(page.getByText("構造化データ権限", { exact: true })).toHaveCount(0);
  await expect(grid.locator("tbody tr")).toHaveCount(2);
  const viewerRoleRow = grid.locator("tbody tr").filter({ hasText: "アプリ閲覧" });
  await expect(viewerRoleRow).toHaveAttribute("data-selected", "true");
  // 一覧はロール名の順（アプリ閲覧 < システム管理者）、詳細見出しはロール名（#584・#1682）。
  await expect(page.getByRole("region", { name: "アプリ閲覧" })).toBeVisible();
  await expect(grid).not.toContainText("SECURITY_VIEWER");
  await expect(grid.getByRole("button", { name: "編集" })).toHaveCount(0);
  await expect(page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" })).toBeVisible();
  await viewerRoleRow.locator("td").nth(1).click();
  await expect(viewerRoleRow).toHaveAttribute("data-selected", "true");
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();
  const customRoleEditActions = page.getByRole("group", { name: "ロール編集操作" });
  // 保存は PageHeader の右端、キャンセルは左上の「一覧へ戻る」に置き換えた（#618）。
  await expect(page.getByTestId("security-roles-submit")).toHaveText("保存");
  await expect(customRoleEditActions.getByRole("button", { name: "キャンセル" })).toHaveCount(0);
  await expect(customRoleEditActions.getByRole("button", { name: "アーカイブ" })).toHaveCount(0);
  await customRoleEditActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "アーカイブ" })).toHaveAttribute("data-entity-action-tone", "danger");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByTestId("security-roles-search").fill("閲覧");
  await expect(grid.getByText("アプリ閲覧")).toBeVisible();
  await expect(grid.getByText("システム管理者")).toHaveCount(0);
  await expect(viewerRoleRow).toHaveAttribute("data-selected", "true");
  await page.getByTestId("security-roles-search").fill("");

  await page.getByTestId("security-roles-actions").getByRole("button", { name: "新規作成" }).click();
  const formStyle = await topLevelPanelStyle(page, "create", "security-roles");
  await expect(page.getByLabel("ロールコード")).toBeVisible();
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(rolesLayout).toBeVisible();

  // 権限管理: ロールごとの機能権限を一括選択できる。
  await page.getByTestId("security-roles-open-permissions").click();
  await expect(page).toHaveURL(/\/settings\/security\/permissions\?role=role-viewer$/u);
  const permissionGrid = page.getByTestId("security-permissions-grid");
  await expect(permissionGrid.getByRole("columnheader", { name: "機能権限" })).toBeVisible();
  await expect(
    permissionGrid.locator("tbody tr").filter({ hasText: "アプリ閲覧" })
  ).toHaveAttribute("data-selected", "true");
  await page
    .getByTestId("security-permissions-detail-actions")
    .getByRole("button", { name: "権限を編集" })
    .click();
  await expect(page.getByText("ダッシュボード表示", { exact: true })).toHaveCount(0);
  await expect(page.getByText("security.users.view", { exact: true })).toHaveCount(0);
  await expect(page.getByText("security.users.manage", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: /外観と証明書/ })).toBeVisible();
  const permissionBulkActions = page.getByTestId("security-roles-permission-selection-actions");
  await expect(permissionBulkActions.getByRole("button", { name: "すべて選択" })).toBeEnabled();
  await expect(permissionBulkActions.getByRole("button", { name: "選択をすべて解除" })).toBeEnabled();
  await permissionBulkActions.getByRole("button", { name: "すべて選択" }).click();
  await expect(page.getByRole("checkbox", { name: /外観と証明書/ })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: /ユーザー管理/ })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: /^ロール管理/ })).toBeChecked();
  // 機能の一覧は左のナビのグループ・名前にそろえる（Issue 567）。backend のグループ「セキュリティ管理」・
  // 名前「ロール・権限管理」ではなく、ナビの「ユーザーとロール」「ロール管理」で出す。
  const securityGroupBulkActions = page.getByTestId("security-roles-ユーザーとロール-permission-selection-actions");
  const securityGroupHeading = securityGroupBulkActions
    .locator("..")
    .getByRole("heading", { name: "ユーザーとロール" });
  await expect
    .poll(async () => {
      const [actionsBox, headingBox] = await Promise.all([
        securityGroupBulkActions.boundingBox(),
        securityGroupHeading.boundingBox(),
      ]);
      if (!actionsBox || !headingBox) return Number.POSITIVE_INFINITY;
      return Math.abs(actionsBox.x - headingBox.x);
    })
    .toBeLessThanOrEqual(1);
  await expect(securityGroupBulkActions.getByRole("button", { name: "ユーザーとロール の選択をすべて解除" })).toBeEnabled();
  await securityGroupBulkActions.getByRole("button", { name: "ユーザーとロール の選択をすべて解除" }).click();
  await expect(page.getByRole("checkbox", { name: /ユーザー管理/ })).not.toBeChecked();
  await expect(page.getByRole("checkbox", { name: /^ロール管理/ })).not.toBeChecked();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "破棄して移動" }).click();
  await expect(page.getByTestId("list-detail-security-permissions")).toBeVisible();
  // 組み込みロールの権限は変更できないため、編集操作を出さない。
  await permissionGrid.locator("tbody tr").filter({ hasText: "システム管理者" }).locator("td").first().click();
  await expect(page.getByTestId("security-permissions-detail-actions")).toHaveCount(0);
  await expect(page.getByText("SYSTEM_ADMIN は現在および将来の全機能権限を自動的に持つ組み込みロールです。")).toBeVisible();

  await page.goto("/settings/security/roles");
  const systemRoleRow = grid.locator("tbody tr").filter({ hasText: "システム管理者" });
  await systemRoleRow.locator("td").first().click();
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();
  expect(await topLevelPanelStyle(page, "edit", "security-roles")).toEqual(formStyle);
  const roleEditActions = page.getByRole("group", { name: "ロール編集操作" });
  await expect(page.getByTestId("security-roles-submit")).toHaveCount(0);
  await expect(page.getByTestId("security-roles-back")).toBeVisible();
  await expect(roleEditActions.getByRole("button", { name: "アーカイブ" })).toHaveCount(0);
  await expect(roleEditActions.getByRole("button", { name: "その他の操作" })).toHaveCount(0);
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(rolesLayout).toBeVisible();
  await page.setViewportSize({ width: 375, height: 812 });
  // xl 未満は 1 ペイン。選んでいるロールの詳細だけを出す。
  await expect(rolesLayout).toHaveAttribute("data-list-detail-view", "detail");
  await expectNoPageHorizontalScroll(page);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
});

test("ロールコード競合はコード欄へ結び付き、403 は安全なページ通知と request ID で示す", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole])
  );
  let submitCount = 0;
  await page.route("**/api/security/roles", async (route) => {
    if (route.request().method() === "GET") {
      await fulfill(route, [systemRole]);
      return;
    }
    submitCount += 1;
    const response =
      submitCount === 1
        ? problemEnvelope({
          status: 409,
          code: "SECURITY_ROLE_CODE_CONFLICT",
          title: "ロールを作成できません",
          detail: "このロールコードは既に使用されています。別のコードを入力してください。",
          requestId: "role-conflict-request",
          fieldErrors: [
            {
              pointer: "/role_code",
              code: "already_exists",
              message: "別のロールコードを入力してください。",
            },
          ],
          })
        : submitCount === 2
          ? problemEnvelope({
              status: 500,
              code: "INTERNAL_SERVER_ERROR",
              title: "サーバー内部でエラーが発生しました",
              detail: "サーバー内部でエラーが発生しました。時間をおいて再試行してください。",
              requestId: "role-internal-request",
            })
          : problemEnvelope({
              status: 403,
              // 経路の権限拒否（#224）。権限なしの画面へ移る。
              code: "SECURITY_ROUTE_FORBIDDEN",
              title: "この操作を実行する権限がありません",
              detail: "ロールを作成する権限がありません。システム管理者に確認してください。",
              requestId: "role-forbidden-request",
            });
    await route.fulfill({
      status: response.problem.status,
      contentType: "application/json",
      headers: { "X-Request-ID": response.problem.request_id },
      body: JSON.stringify(response),
    });
  });

  await page.goto("/settings/security/roles");
  await page.getByTestId("security-roles-actions").getByRole("button", { name: "新規作成" }).click();
  const roleCode = page.getByLabel("ロールコード");
  // 作成は PageHeader の右端、保存の失敗はヘッダーの直下の SaveErrorBanner（#585 / #618）。
  const submitRole = page.getByTestId("security-roles-submit");
  const saveError = page.getByTestId("security-roles-save-error");
  await roleCode.fill("system_admin");
  await page.getByLabel("ロール名").fill("予約コードロール");
  await submitRole.click();

  const reservedMessage =
    "SYSTEM_ADMIN は組み込みロール専用のコードです。別のロールコードを入力してください。";
  await expect(page.getByText(reservedMessage, { exact: true })).toHaveCount(1);
  await expect(roleCode).toHaveAttribute("aria-invalid", "true");
  // エラーは共有の TextField が欄の下に出し、aria-describedby で結び付ける（id は useId を含む。#631）。
  await expect(roleCode).toHaveAccessibleDescription(/SYSTEM_ADMIN は組み込みロール専用のコードです。/);
  await expect(roleCode).toBeFocused();
  expect(submitCount).toBe(0);

  await roleCode.fill("DUPLICATE_ROLE");
  await expect(page.getByText(reservedMessage, { exact: true })).toHaveCount(0);
  await page.getByLabel("ロール名").fill("重複ロール");
  await submitRole.click();

  const conflictMessage = "このロールコードは既に使用されています。別のコードを入力してください。";
  await expect(page.getByText(conflictMessage, { exact: true })).toHaveCount(1);
  await expect(roleCode).toHaveAttribute("aria-invalid", "true");
  await expect(roleCode).toHaveAccessibleDescription(/このロールコードは既に使用されています。/);
  await expect(roleCode).toBeFocused();
  await expect(saveError.getByText(conflictMessage, { exact: true })).toHaveCount(0);

  await roleCode.fill("AVAILABLE_ROLE");
  await expect(roleCode).not.toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText(conflictMessage, { exact: true })).toHaveCount(0);
  await submitRole.click();
  await expect(saveError).toContainText("サーバー内部でエラーが発生しました。時間をおいて再試行してください。");
  await expect(saveError).toContainText("リクエストID: role-internal-request");
  await expect(saveError).not.toContainText("ORA-");
  await page.getByLabel("ロール名").fill("再試行ロール");
  await submitRole.click();
  await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("必要なロールが付与されているか、システム管理者に確認してください。");
  await expect(page.getByRole("status")).toContainText("リクエストID: role-forbidden-request");
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
  expect(submitCount).toBe(3);
});

test("SYSTEM_ADMIN は権限管理で業務プロファイル利用権限を設定できる", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/security/profile-access/profiles**");
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, profileAccessPage(route.request().url(), [
      {
        id: "default",
        name: "標準プロファイル",
        category: "共通",
        description: "標準の業務プロファイル",
        archived: false,
        allowed_roles: [{ role_id: "role-query-default", role_code: "QUERY_DEFAULT", display_name: "SQL 利用者" }],
      },
      {
        id: "finance",
        name: "財務プロファイル",
        category: "会計",
        description: "財務部門向け",
        archived: false,
        allowed_roles: [],
      },
      ...Array.from({ length: 18 }, (_, index) => ({
        id: `verification-${String(index + 1).padStart(2, "0")}`,
        name: `検証プロファイル ${String(index + 1).padStart(2, "0")}`,
        category: "検証",
        description: "スクロール領域の表示確認用プロファイル",
        archived: false,
        allowed_roles: [],
      })),
    ]))
  );
  const permissionRows = [
    {
      code: "menu.query",
      group: "メニュー権限",
      label: "SQL 生成",
      description: "SQL 生成を表示し、関連操作を利用できます。",
      implies: ["nl2sql.profiles.read"],
    },
    {
      code: "nl2sql.profiles.read",
      group: "参照権限",
      label: "業務プロファイル参照",
      description: "業務プロファイルの利用コンテキストを参照できます。",
      implies: [],
    },
  ];
  const queryRole = {
    ...systemRole,
    role_id: "role-query-default",
    role_code: "QUERY_DEFAULT",
    display_name: "SQL 利用者",
    description: "標準 profile のみ",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
    allowed_profile_ids: ["default"],
  };
  let savedPayload: Record<string, unknown> | null = null;
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole, queryRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));
  await page.route("**/api/security/roles/role-query-default/permissions", async (route) => {
    expect(route.request().method()).toBe("PUT");
    savedPayload = route.request().postDataJSON() as Record<string, unknown>;
    await fulfill(route, {
      ...queryRole,
      version: 2,
      allowed_profile_ids: savedPayload.allowed_profile_ids,
    });
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/security/permissions");
  await page
    .getByTestId("security-permissions-grid")
    .locator("tbody tr")
    .filter({ hasText: "SQL 利用者" })
    .locator("td")
    .first()
    .click();
  await expect(page.getByText("1 件", { exact: true }).first()).toBeVisible();
  await page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集" }).click();
  // 候補の一覧は共通の ListPicker（#600）: 見出しの名前の区画・検索欄・選択の行・listbox のスクロール領域。
  const profileSearch = page.getByRole("searchbox", { name: "業務プロファイルを検索" });
  const profilePicker = page.getByTestId("security-roles-profile-access-list");
  const profileAccessList = page.getByTestId("security-roles-profile-access-list-scroll-region");
  const readProfileAccessScrollState = () =>
    profileAccessList.evaluate((node) => {
      const computed = window.getComputedStyle(node);
      return {
        clientHeight: node.clientHeight,
        maxHeight: Number.parseFloat(computed.maxHeight),
        overflowX: computed.overflowX,
        overflowY: computed.overflowY,
        rootFontSize: Number.parseFloat(
          window.getComputedStyle(document.documentElement).fontSize
        ),
        scrollHeight: node.scrollHeight,
        noHorizontalOverflow: node.scrollWidth <= node.clientWidth + 1,
      };
    });
  await expect(profileSearch).toBeVisible();
  await expect(profilePicker).toHaveAccessibleName("使用可能な業務プロファイル");
  const profileListbox = profilePicker.getByRole("listbox", { name: "使用可能な業務プロファイル" });
  await expect(profileListbox).toHaveAttribute("tabindex", "0");
  const desktopScrollState = await readProfileAccessScrollState();
  expect(desktopScrollState.maxHeight).toBeCloseTo(
    28 * desktopScrollState.rootFontSize,
    0
  );
  expect(desktopScrollState.clientHeight).toBeLessThanOrEqual(
    Math.ceil(desktopScrollState.maxHeight)
  );
  expect(desktopScrollState.scrollHeight).toBeGreaterThan(desktopScrollState.clientHeight);
  expect(desktopScrollState.noHorizontalOverflow).toBe(true);
  expect(desktopScrollState.overflowY).toBe("auto");
  // Tab で listbox に入り、End で最後の候補へ移ると、スクロール領域の中だけが動く。
  await profileListbox.focus();
  await expect(profileListbox).toBeFocused();
  await profileListbox.press("End");
  await expect
    .poll(() => profileAccessList.evaluate((node) => node.scrollTop))
    .toBeGreaterThan(0);
  await profileSearch.focus();
  await expect(profileSearch).toBeFocused();
  await profileSearch.fill("財務");
  await expect(page.getByRole("option", { name: /財務プロファイル/ })).toBeVisible();
  await expect(page.getByRole("option", { name: /標準プロファイル/ })).toHaveCount(0);
  const filteredScrollState = await readProfileAccessScrollState();
  expect(filteredScrollState.clientHeight).toBeLessThan(filteredScrollState.maxHeight);
  expect(filteredScrollState.scrollHeight).toBeLessThanOrEqual(
    filteredScrollState.clientHeight + 1
  );

  const profileBulkActions = page.getByTestId("security-roles-profile-access-list-bulk-actions");
  await expect
    .poll(async () => {
      const [actionsBox, listBox] = await Promise.all([
        profileBulkActions.boundingBox(),
        profileAccessList.boundingBox(),
      ]);
      if (!actionsBox || !listBox) return Number.POSITIVE_INFINITY;
      return Math.abs(actionsBox.x - listBox.x);
    })
    .toBeLessThanOrEqual(1);
  await profileBulkActions.getByRole("button", { name: "すべて選択" }).click();
  await expect(page.getByRole("option", { name: /財務プロファイル/ })).toBeChecked();
  await profileBulkActions.getByRole("button", { name: "選択をすべて解除" }).click();
  await expect(page.getByRole("option", { name: /財務プロファイル/ })).not.toBeChecked();
  await page.getByRole("option", { name: /財務プロファイル/ }).check();
  await expect(page.getByRole("option", { name: /財務プロファイル/ })).toBeChecked();
  await profileSearch.fill("");
  // 検索語は入力が止まってから適用する（debounce。#535）。一覧が戻るのを待ってから測る。
  await expect(page.getByRole("option", { name: /標準プロファイル/ })).toHaveCount(1);
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
  const mobileScrollState = await readProfileAccessScrollState();
  expect(mobileScrollState.maxHeight).toBeCloseTo(
    17.5 * mobileScrollState.rootFontSize,
    0
  );
  expect(mobileScrollState.clientHeight).toBeLessThanOrEqual(
    Math.ceil(mobileScrollState.maxHeight)
  );
  expect(mobileScrollState.scrollHeight).toBeGreaterThan(mobileScrollState.clientHeight);
  expect(mobileScrollState.noHorizontalOverflow).toBe(true);
  expect(mobileScrollState.overflowY).toBe("auto");
  await expect(profileSearch).toBeVisible();
  await expect
    .poll(async () => {
      const [actionsBox, listBox] = await Promise.all([
        profileBulkActions.boundingBox(),
        profileAccessList.boundingBox(),
      ]);
      if (!actionsBox || !listBox) return Number.POSITIVE_INFINITY;
      return Math.abs(actionsBox.x - listBox.x);
    })
    .toBeLessThanOrEqual(1);
  await page.getByTestId("security-permissions-submit").click();

  await expect
    .poll(() => (savedPayload?.allowed_profile_ids as string[] | undefined)?.sort())
    .toEqual(["default", "finance"]);
  const permissionPayload = savedPayload as Record<string, unknown> | null;
  expect(permissionPayload?.permissions).toEqual(["menu.query"]);
  expect(permissionPayload).not.toHaveProperty("display_name");
});

test("業務プロファイル管理権限のロールは全業務プロファイルとして扱う", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/security/profile-access/profiles**");
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, profileAccessPage(route.request().url(), [
      {
        id: "default",
        name: "標準プロファイル",
        category: "共通",
        description: "標準の業務プロファイル",
        archived: false,
        allowed_roles: [{ role_id: "role-profile-manager", role_code: "PROFILE_MANAGER", display_name: "業務プロファイル管理ロール" }],
      },
      {
        id: "finance",
        name: "財務プロファイル",
        category: "会計",
        description: "財務部門向け",
        archived: false,
        allowed_roles: [{ role_id: "role-profile-manager", role_code: "PROFILE_MANAGER", display_name: "業務プロファイル管理ロール" }],
      },
    ]))
  );
  const permissionRows = [
    {
      code: "menu.profiles",
      group: "メニュー権限",
      label: "業務プロファイル",
      description: "業務プロファイルを表示し、関連操作を利用できます。",
      implies: ["nl2sql.profiles.manage"],
    },
    {
      code: "nl2sql.profiles.manage",
      group: "管理権限",
      label: "業務プロファイル管理",
      description: "業務プロファイルを管理できます。",
      implies: ["nl2sql.profiles.read"],
    },
    {
      code: "nl2sql.profiles.read",
      group: "参照権限",
      label: "業務プロファイル参照",
      description: "業務プロファイルの利用コンテキストを参照できます。",
      implies: [],
    },
  ];
  const profileManagerRole = {
    ...systemRole,
    role_id: "role-profile-manager",
    role_code: "PROFILE_MANAGER",
    display_name: "業務プロファイル管理ロール",
    description: "個別 profile ID は保存しない",
    is_built_in: false,
    permissions: ["menu.profiles"],
    data_entitlements: [],
    allowed_profile_ids: ["default"],
  };
  let savedPayload: Record<string, unknown> | null = null;
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole, profileManagerRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));
  await page.route("**/api/security/roles/role-profile-manager/permissions", async (route) => {
    savedPayload = route.request().postDataJSON() as Record<string, unknown>;
    await fulfill(route, {
      ...profileManagerRole,
      version: 2,
      allowed_profile_ids: savedPayload.allowed_profile_ids,
    });
  });

  await page.goto("/settings/security/permissions");
  const grid = page.getByTestId("security-permissions-grid");
  await grid
    .locator("tbody tr")
    .filter({ hasText: "業務プロファイル管理ロール" })
    .locator("td")
    .first()
    .click();
  await expect(page.getByText("すべての業務プロファイル", { exact: true }).first()).toBeVisible();
  await page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集" }).click();

  await expect(
    page.getByText(
      "業務プロファイル管理権限により、すべての業務プロファイルを利用できます。個別選択は不要です。",
      { exact: true }
    )
  ).toBeVisible();
  await expect(page.getByTestId("security-roles-profile-access-list")).toHaveCount(0);
  await page.getByTestId("security-permissions-submit").click();
  await expect.poll(() => savedPayload?.allowed_profile_ids).toEqual([]);
  await expectNoPageHorizontalScroll(page);
});

test("権限管理は業務プロファイル候補の取得失敗でもロール一覧を表示する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/security/profile-access/profiles**");
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, "業務プロファイル候補の取得に失敗しました。", 500)
  );
  await page.route("**/api/security/permissions", (route) =>
    fulfill(route, [
      {
        code: "menu.query",
        group: "メニュー権限",
        label: "SQL 生成",
        description: "SQL 生成を表示し、関連操作を利用できます。",
        implies: [],
      },
    ])
  );
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [
      systemRole,
      {
        ...systemRole,
        role_id: "role-query-default",
        role_code: "QUERY_DEFAULT",
        display_name: "SQL 利用者",
        description: "標準 profile のみ",
        is_built_in: false,
        permissions: ["menu.query"],
        data_entitlements: [],
        allowed_profile_ids: ["default"],
      },
    ])
  );

  await page.goto("/settings/security/permissions");

  await expect(
    page.getByText("業務プロファイル利用権限の候補を読み込めませんでした。", {
      exact: false,
    })
  ).toBeVisible();
  await expect(page.getByTestId("security-permissions-grid").getByText("SQL 利用者")).toBeVisible();
  await expect(page.getByText("対象データはありません。")).toHaveCount(0);
});

test("権限管理は詳細で権限名を伏せ、編集では SQL 生成由来の参照権限を継承表示する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const permissionRows = [
    {
      code: "menu.query",
      group: "メニュー権限",
      label: "SQL 生成",
      description: "SQL 生成を表示し、関連操作を利用できます。",
      implies: ["nl2sql.profiles.read", "nl2sql.schema.read"],
    },
    {
      code: "nl2sql.profiles.read",
      group: "参照権限",
      label: "業務プロファイル参照",
      description: "SQL 生成で業務プロファイルの利用コンテキストを参照できます。",
      implies: [],
    },
    {
      code: "nl2sql.schema.read",
      group: "参照権限",
      label: "スキーマ参照",
      description: "SQL 生成でスキーマ情報を参照できます。",
      implies: [],
    },
  ];
  const queryRole = {
    ...systemRole,
    role_id: "role-query-only",
    role_code: "QUERY_ONLY",
    display_name: "SQL 利用者",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole, queryRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));

  await page.goto("/settings/security/permissions");
  await page.getByTestId("security-permissions-grid").locator("tbody tr").filter({ hasText: "SQL 利用者" }).locator("td").first().click();

  await expect(page.getByText("業務プロファイル参照 (SQL 生成により付与)")).toHaveCount(0);
  await expect(page.getByText("スキーマ参照 (SQL 生成により付与)")).toHaveCount(0);
  await page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集" }).click();
  await expect(page.getByText("SQL 生成により付与", { exact: true }).first()).toBeVisible();
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
});

test("ロール編集のその他の操作のメニューは画面の下端でも画面の中に開く", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  // ロール管理のフォームは基本情報だけなので、短い viewport で操作バーを下端に置く（#206）。
  await page.setViewportSize({ width: 1365, height: 480 });
  const permissionRows = [
    {
      code: "menu.query",
      group: "RAG",
      label: "SQL 生成",
      description: "SQL 生成を表示し、関連操作を利用できます。",
      implies: [],
    },
    ...Array.from({ length: 28 }, (_, index) => ({
      code: `menu.bottom_menu_check_${index + 1}`,
      group: index % 2 === 0 ? "システム設定" : "管理権限",
      label: `下端検証 ${String(index + 1).padStart(2, "0")}`,
      description: "下端メニューの表示位置を検証するための権限です。",
      implies: [],
    })),
  ];
  const activeRole = {
    ...systemRole,
    role_id: "role-bottom-menu",
    role_code: "BOTTOM_MENU",
    display_name: "下端メニュー検証ロール",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };

  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole, activeRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));

  await page.goto("/settings/security/roles");
  await page.getByTestId("security-roles-grid").locator("tbody tr").filter({ hasText: "下端メニュー検証ロール" }).locator("td").first().click();
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();

  const editActions = page.getByRole("group", { name: "ロール編集操作" });
  await expect(page.getByTestId("security-roles-submit")).toHaveText("保存");
  // 対象への操作はフォームのパネルの見出しの右（#618）。画面の下端に寄せても、メニューは画面の中に開く。
  const trigger = editActions.getByRole("button", { name: /^その他の操作/ });
  await trigger.evaluate((element) => element.scrollIntoView({ block: "end" }));
  const triggerBox = await trigger.boundingBox();
  expect(triggerBox).not.toBeNull();
  await trigger.click();

  const menu = page.getByRole("menu");
  await expectFloatingMenuInsideViewport(page, menu);
  const menuBox = await menu.boundingBox();
  expect(menuBox).not.toBeNull();
  if ((await menu.getAttribute("data-floating-menu-placement")) === "top") {
    expect(menuBox!.y + menuBox!.height).toBeLessThanOrEqual(triggerBox!.y + 1);
  }
});

test("ロール管理の compact header は操作が 2 つなら畳まず、短い viewport でも横にはみ出さない", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.setViewportSize({ width: 375, height: 360 });
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));

  await page.goto("/settings/security/roles");
  const actions = page.getByTestId("security-roles-actions");
  // 共有 PageHeader は lg 未満で「その他の操作」+ 主操作に畳むが、メニューに入るのが 1 つだけなら
  // 畳まず両方を出す（1 項目のメニューを開かせない。#582）。
  await expect(actions.getByRole("button")).toHaveText(["最新情報を取得", "新規作成"]);
  await expect(actions.getByTestId("page-actions-more")).toHaveCount(0);
  const [refreshBox, createBox] = await Promise.all([
    actions.getByRole("button", { name: "最新情報を取得" }).boundingBox(),
    actions.getByRole("button", { name: "新規作成" }).boundingBox(),
  ]);
  // 2 つは 1 行に並ぶ（折り返してヘッダーを高くしない）。
  expect(Math.abs(refreshBox!.y - createBox!.y)).toBeLessThanOrEqual(1);
  await expectNoPageHorizontalScroll(page);
});

test("アーカイブ済みロールは権限が無効であることを明示し、権限管理でも編集できない", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/security/profile-access/profiles**");
  await page.route("**/api/security/profile-access/profiles**", (route) =>
    fulfill(route, profileAccessPage(route.request().url(), [
      {
        id: "default",
        name: "標準プロファイル",
        category: "共通",
        description: "標準の業務プロファイル",
        archived: false,
        allowed_roles: [{ role_id: "role-archived-data", role_code: "DATA_ADMIN", display_name: "データ管理者" }],
      },
      {
        id: "finance",
        name: "財務プロファイル",
        category: "会計",
        description: "財務部門向け",
        archived: false,
        allowed_roles: [],
      },
    ]))
  );
  const permissionRows = [
    {
      code: "menu.query",
      group: "AI 活用",
      label: "SQL 生成",
      description: "SQL 生成を表示し、関連操作を利用できます。",
      implies: [],
    },
    {
      code: "menu.direct_sql",
      group: "AI 活用",
      label: "SELECT SQL を実行",
      description: "SELECT SQL 実行を表示し、関連操作を利用できます。",
      implies: [],
    },
  ];
  const activeRole = {
    ...systemRole,
    role_id: "role-active-data",
    role_code: "DATA_USER",
    display_name: "データユーザー",
    is_built_in: false,
    permissions: ["menu.query"],
    data_entitlements: [],
  };
  const archivedRole = {
    ...systemRole,
    role_id: "role-archived-data",
    role_code: "DATA_ADMIN",
    display_name: "データ管理者",
    is_built_in: false,
    archived: true,
    permissions: ["menu.query", "menu.direct_sql"],
    data_entitlements: [],
    allowed_profile_ids: ["default"],
  };
  let roles: unknown[] = [systemRole, activeRole, archivedRole];
  let updateRoleRequestCount = 0;
  await page.route("**/api/security/roles?include_archived=true", (route) => fulfill(route, roles));
  await page.route("**/api/security/roles/role-archived-data", async (route) => {
    updateRoleRequestCount += 1;
    await fulfill(route, archivedRole);
  });
  await page.route("**/api/security/roles/role-active-data/archive", async (route) => {
    const updated = { ...activeRole, archived: true, version: activeRole.version + 1 };
    roles = [systemRole, updated, archivedRole];
    await fulfill(route, updated);
  });
  await page.route("**/api/security/roles/role-archived-data/restore", async (route) => {
    const restored = { ...archivedRole, archived: false, version: archivedRole.version + 1 };
    roles = [systemRole, activeRole, restored];
    await fulfill(route, restored);
  });
  await page.route("**/api/security/permissions", (route) => fulfill(route, permissionRows));

  // 権限管理: アーカイブ済みロールは権限を編集できず、無効であることを示す。
  // 一覧と詳細を並べて行を選び直す操作を確かめる（xl 未満の 1 ペインでは選ぶと一覧が隠れる。#2282）。
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings/security/permissions");
  const permissionGrid = page.getByTestId("security-permissions-grid");
  await permissionGrid.locator("tbody tr").filter({ hasText: "データ管理者" }).locator("td").first().click();
  await expect(
    page.getByText(
      "このロールはアーカイブ済みです。保存済みの権限は利用者の実アクセス権には反映されません。",
      { exact: true }
    )
  ).toBeVisible();
  await expect(page.getByTestId("security-permissions-detail-actions")).toHaveCount(0);
  await expect(page.getByRole("checkbox")).toHaveCount(0);

  await page.goto("/settings/security/roles");

  const grid = page.getByTestId("security-roles-grid");
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const archivedRow = grid.locator("tbody tr").filter({ hasText: "データ管理者" });
  await archivedRow.locator("td").first().click();
  await expect(archivedRow.getByText("アーカイブ済み", { exact: true })).toBeVisible();
  await expect(
    page.getByText(
      "このロールはアーカイブ済みです。保存済みの権限は利用者の実アクセス権には反映されません。",
      { exact: true }
    )
  ).toBeVisible();

  const archivedDetail = page.locator("section", { has: page.getByRole("heading", { name: "DATA_ADMIN", exact: true }) });
  await expect(archivedDetail.getByText("SQL 生成", { exact: true })).toHaveCount(0);

  await expect(page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "復元" })).toBeVisible();
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();
  const archivedEditPanel = page.locator("#security-roles-panel-edit");
  const archivedEditActions = page.getByRole("group", { name: "ロール編集操作" });
  const roleName = page.getByLabel("ロール名");
  const roleDescription = page.getByLabel("説明");
  await expect(roleName).toBeDisabled();
  await expect(roleDescription).toBeDisabled();
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await page.getByRole("button", { name: "一覧へ戻る" }).focus();
  await page.keyboard.press("Tab");
  await expect(archivedEditActions.getByRole("button", { name: "復元" })).toBeFocused();
  await archivedEditPanel.locator("form").evaluate((form) =>
    (form as HTMLFormElement).requestSubmit()
  );
  expect(updateRoleRequestCount).toBe(0);
  await expect(page.getByTestId("security-roles-submit")).toHaveCount(0);
  await expect(archivedEditActions.getByRole("button", { name: "復元" })).toBeVisible();
  await archivedEditActions.getByRole("button", { name: "復元" }).click();
  const restoreDialog = page.getByRole("alertdialog");
  await expect(restoreDialog).toBeVisible();
  await expect(
    restoreDialog.getByText(
      "このロールを復元すると、このロールに紐づくユーザーへ、このロール由来の権限が次回リクエストから反映されます。",
      { exact: true }
    )
  ).toBeVisible();
  await restoreDialog.getByRole("button", { name: "実行" }).click();
  await expect(page.getByTestId("list-detail-security-roles")).toBeVisible();
  const restoredRow = grid.locator("tbody tr").filter({ hasText: "データ管理者" });
  await expect(restoredRow.getByText("カスタム", { exact: true })).toBeVisible();
  await expect(restoredRow.getByText("アーカイブ済み", { exact: true })).toHaveCount(0);
  await expect(
    page.getByText(
      "このロールはアーカイブ済みです。保存済みの権限は利用者の実アクセス権には反映されません。",
      { exact: true }
    )
  ).toHaveCount(0);
  await expect(page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "復元" })).toHaveCount(0);
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();
  await expect(roleName).toBeEnabled();
  await expect(roleDescription).toBeEnabled();
  await expect(page.getByTestId("security-roles-submit")).toHaveText("保存");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expectNoPageHorizontalScroll(page);

  const activeRow = grid.locator("tbody tr").filter({ hasText: "データユーザー" });
  await activeRow.locator("td").first().click();
  await expect(page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "復元" })).toHaveCount(0);
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "アーカイブ" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集" }).click();
  await expect(page.getByRole("group", { name: "ロール編集操作" }).getByRole("button", { name: "復元" })).toHaveCount(0);
  await page.getByRole("group", { name: "ロール編集操作" }).getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "アーカイブ" }).click();
  const confirmDialog = page.getByRole("alertdialog");
  await expect(confirmDialog).toBeVisible();
  await expect(
    confirmDialog.getByText(
      "このロールをアーカイブすると、このロール由来の権限は利用者に反映されなくなります。ユーザー自体は無効化されません。",
      { exact: true }
    )
  ).toBeVisible();
});

test("アーカイブ済みカスタムロールの削除は409を保持し再試行できる", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const activeRole = {
    ...systemRole,
    role_id: "role-active-delete",
    role_code: "ACTIVE_DELETE",
    display_name: "有効カスタムロール",
    is_built_in: false,
  };
  const archivedRole = {
    ...activeRole,
    role_id: "role-archived-delete",
    role_code: "ARCHIVED_DELETE",
    display_name: "削除対象ロール",
    archived: true,
    version: 9,
  };
  let roles = [systemRole, activeRole, archivedRole];
  let deleteCount = 0;
  const observedIfMatch: string[] = [];

  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, roles)
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));
  await page.route("**/api/security/roles/role-archived-delete", async (route) => {
    deleteCount += 1;
    observedIfMatch.push(route.request().headers()["if-match"] ?? "");
    if (deleteCount === 1) {
      const response = problemEnvelope({
        status: 409,
        code: "SECURITY_ROLE_DELETE_ENTITLEMENTS_PRESENT",
        title: "現在の状態では削除できません",
        detail:
          "このロールにはデータ権限が残っています。Deep Data Security で空の Data Grant を適用してから削除してください。",
        requestId: "role-delete-conflict",
      });
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify(response),
      });
      return;
    }
    roles = roles.filter((role) => role.role_id !== archivedRole.role_id);
    await fulfill(route, {
      deleted: true,
      role_id: archivedRole.role_id,
      role_code: archivedRole.role_code,
    });
  });

  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/settings/security/roles");

  const grid = page.getByTestId("security-roles-grid");
  await expect(grid.getByRole("columnheader", { name: "操作" })).toHaveCount(0);
  await expect(grid.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  const activeRow = grid.locator("tbody tr").filter({ hasText: "有効カスタムロール" });
  await activeRow.locator("td").first().click();
  const detailActions = page.getByTestId("security-roles-detail-actions");
  await detailActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "削除" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  const archivedRow = grid.locator("tbody tr").filter({ hasText: "削除対象ロール" });
  await archivedRow.locator("td").first().click();
  await detailActions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "削除" }).click();
  const deleteDialog = page.getByRole("alertdialog", { name: "削除" });
  await expect(deleteDialog).toContainText(
    "「削除対象ロール」を完全に削除します。"
  );
  await expect(deleteDialog).toContainText("ロールに設定した権限も削除され");
  await deleteDialog.getByRole("button", { name: "削除", exact: true }).click();

  await expect(page.getByText("削除対象ロール", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("main")).toContainText(
    "Deep Data Security で空の Data Grant を適用してから削除してください。"
  );

  await page
    .getByTestId("security-roles-detail-actions")
    .getByRole("button", { name: "編集" })
    .click();
  const editActions = page.getByRole("group", { name: "ロール編集操作" });
  await editActions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "削除" }).click();
  await deleteDialog.getByRole("button", { name: "削除", exact: true }).click();

  await expect(page.getByText("削除対象ロール", { exact: true })).toHaveCount(0);
  await expect(page.getByText("削除対象ロール を完全に削除しました。", { exact: true }).last()).toBeVisible();
  expect(observedIfMatch).toEqual(['"9"', '"9"']);
  expect(deleteCount).toBe(2);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
});

test("ロール・権限管理はオントロジー提案取得が遅延しても読み込み完了する", async ({ page }) => {
  test.slow();
  await mockDatabaseGateReady(page);
  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) => fulfill(route, systemAdminMe));
  const profile = {
    id: "default",
    name: "標準プロファイル",
    category: "",
    description: "",
    archived: false,
    allowed_tables: [],
    allowed_views: [],
    glossary: {},
    few_shot_examples: [],
    version: 1,
    etag: "profile-etag",
    updated_at: "2026-07-21T00:00:00Z",
  };
  let markdownRequests = 0;
  let releaseProposals = () => {};
  const proposalsGate = new Promise<void>((resolve) => {
    releaseProposals = resolve;
  });

  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfill(route, {
      items: [
        {
          id: profile.id,
          name: profile.name,
          category: profile.category,
          description: profile.description,
          archived: profile.archived,
          allowed_table_count: 0,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: profile.version,
          etag: profile.etag,
          updated_at: profile.updated_at,
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default", (route) => fulfill(route, profile));
  await page.route("**/api/nl2sql/profiles/default/ontology-view", (route) =>
    fulfill(route, { profile_ontology_view: null, ontology_graph: null, warnings_ja: [] })
  );
  await page.route("**/api/nl2sql/ontology/revisions", (route) =>
    fulfill(route, { revisions: [], active_revision_id: "" })
  );
  await page.route("**/api/nl2sql/profiles/default/ontology-markdown", async (route) => {
    markdownRequests += 1;
    await proposalsGate;
    try {
      await fulfill(route, {
        draft_markdown: "",
        published_markdown: "",
        draft_revision: null,
        published_revision: null,
        draft_etag: "",
        published_at: null,
      });
    } catch {
      // 画面遷移で abort 済みの request は fulfill できない場合がある。
    }
  });
  await page.route("**/api/nl2sql/profiles/default/ontology-build-jobs**", (route) =>
    fulfill(route, { jobs: [] })
  );
  await page.route("**/api/nl2sql/profiles/default/ontology-source-documents**", (route) =>
    fulfill(route, { source_documents: [] })
  );
  await page.route("**/api/security/roles?include_archived=true", (route) =>
    fulfill(route, [systemRole])
  );
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));

  try {
    await page.goto("/ontology-build?profile=default");
    await page.getByRole("button", { name: "情報を取得", exact: true }).click();
    await expect(page.getByTestId("profile-ontology-build")).toBeVisible({
      timeout: 30_000,
    });
    await expect.poll(() => markdownRequests).toBeGreaterThan(0);

    await page.goto("/settings/security/roles");

    const grid = page.getByTestId("security-roles-grid");
    await expect(grid).toBeVisible();
    await expect(grid.locator("tbody tr")).toHaveCount(1);
    await expect(grid.locator(".animate-pulse")).toHaveCount(0);
    await expect(grid.getByText("システム管理者")).toBeVisible();
    await expectNoPageHorizontalScroll(page);
  } finally {
    releaseProposals();
  }
});

test("DeepSec は3つの管理タブで認証・基盤構成・データ権限を分ける", { tag: "@responsive" }, async ({ page }, testInfo) => {
  if (testInfo.project.name === "desktop") {
    await page.setViewportSize({ width: 1440, height: 900 });
  }
  await mockDatabaseGateReady(page);
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: { data_grants: 2 },
      message: "構成済みです。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan(true)));
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");

  const dataUserTab = page.getByRole("tab", { name: "DATA USER 認証" });
  const foundationTab = page.getByRole("tab", { name: "基盤構成" });
  const dataPermissionsTab = page.getByRole("tab", { name: "データ権限" });
  await expect(page.getByRole("tab")).toHaveCount(3);
  await expect(dataUserTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { name: "DATA USER 認証情報" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "V001.1 共有 DATA USER とロール" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "構造化データ権限" })).toHaveCount(0);

  await dataUserTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(foundationTab).toBeFocused();
  await expect(foundationTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { name: "実行計画", exact: true })).toBeVisible();
  await expect(page.getByText("実行計画 V001.1–V001.2", { exact: true })).toHaveCount(0);
  await expect(page.getByText("共有 DATA USER とアプリケーションコンテキストを、定義済みの順序で準備します。", { exact: true })).toBeVisible();
  const step1 = page.getByTestId("security-deepsec-step-1");
  const step2 = page.getByTestId("security-deepsec-step-2");
  await expect(step1.getByText("V001.1", { exact: true })).toBeVisible();
  await expect(step1.getByRole("heading", { name: "V001.1 共有 DATA USER とロール" })).toBeVisible();
  await expect(step2.getByText("V001.2", { exact: true })).toBeVisible();
  await expect(step2.getByRole("heading", { name: "V001.2 アプリケーションコンテキスト" })).toBeVisible();
  await expect(page.getByTestId("security-deepsec-step-3")).toHaveCount(0);
  await expect(step1.getByRole("button", { name: "このステップを適用" })).toHaveCount(0);
  await expect(step2.getByRole("button", { name: "このステップを適用" })).toHaveCount(0);
  await expect(step1.getByText("適用日時", { exact: true })).toBeVisible();
  await expect(step1.locator("time")).toHaveAttribute("datetime", "2026-07-19T00:00:00Z");
  await expect(step1.locator("time")).toHaveText(/^2026\/07\/19 \d{2}:\d{2}$/);
  await expect(page.locator("pre:visible")).toHaveCount(0);
  await expect(page.getByText("SQL とチェックサムを表示", { exact: true })).toHaveCount(2);

  await page.keyboard.press("End");
  await expect(dataPermissionsTab).toBeFocused();
  await expect(dataPermissionsTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { name: "構造化データ権限" })).toBeVisible();
  await expect(page.getByText(/システム管理者は別の接続で実行するため、Data Grant による行・列の制限は適用されません/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "ロール別 Data Grant ポリシー" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "実行計画", exact: true })).toHaveCount(0);
  await expect(page.getByTestId("security-deepsec-step-3")).toHaveCount(0);
  await expect(page.getByText("NL2SQL_DEEPSEC_PROBE", { exact: false })).toHaveCount(0);
  await expect(page.getByTestId("security-deepsec-step-1")).toHaveCount(0);

  await page.keyboard.press("Home");
  await expect(dataUserTab).toBeFocused();
  await expect(dataUserTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("構成済み", { exact: true }).first()).toBeVisible();
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec は構成状態の確認中でも SQL plan を先に表示する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  let releaseStatus = () => {};
  const statusGate = new Promise<void>((resolve) => {
    releaseStatus = resolve;
  });
  await page.route("**/api/security/deepsec/status", async (route) => {
    await statusGate;
    await fulfill(route, {
      configured: false,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: {},
      message: "未適用です。",
    });
  });
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan()));
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");
  await expect(page.getByText("構成状態を確認中", { exact: true }).first()).toBeVisible();
  await page.getByRole("tab", { name: "基盤構成" }).click();
  const step1 = page.getByTestId("security-deepsec-step-1");
  await expect(page.getByRole("heading", { name: "V001.1 共有 DATA USER とロール" })).toBeVisible();
  await expect(page.locator("pre:visible")).toHaveCount(0);
  await page.getByText("SQL とチェックサムを表示", { exact: true }).first().click();
  await expect(step1.locator("pre:visible")).toHaveCount(6);
  // V001.1 は DATA USER（END USER）を先に作る。パスワードは placeholder のまま表示する（#1676）。
  await expect(
    step1.getByText("CREATE END USER IF NOT EXISTS DEEPSEC_DATA_USER IDENTIFIED BY <secret:NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD>", { exact: false })
  ).toBeVisible();
  await expect(step1.getByText("ALTER END USER", { exact: false })).toHaveCount(0);
  await expect(step1.getByText("GRANT DATA ROLE NL2SQL_APP_DATA_ROLE TO DEEPSEC_DATA_USER", { exact: false })).toBeVisible();
  await page.getByRole("tab", { name: "データ権限" }).click();
  await expect(page.getByText("Data Grant を適用する前に", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "基盤構成へ" })).toBeVisible();
  const verificationCard = page.getByTestId("security-deepsec-verification-card");
  await expect(verificationCard).toBeVisible();
  await expect(verificationCard.getByText("検証を実行すると", { exact: false })).toBeVisible();
  const verifyButton = verificationCard.getByRole("button", { name: "Data Grant を検証" });
  await expect(verifyButton).toBeDisabled();
  await expectNoElementHorizontalOverflow(verifyButton);

  releaseStatus();
  await expect(page.getByText("未構成", { exact: true }).first()).toBeVisible();
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec は構成状態の取得失敗を Header Badge と再読込導線で示す", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, "構成状態を取得できませんでした。接続を確認して再試行してください。", 503)
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan()));
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");

  await expect(page.getByText("状態取得失敗", { exact: true }).first()).toBeVisible();
  // 失敗の通知の取り直しは「再試行」で、ヘッダーの「最新情報を取得」と名前で区別できる（#1868）。
  await expect(page.getByRole("alert").getByRole("button", { name: "再試行" })).toBeVisible();
  await expect(page.getByText("構成状態を取得できませんでした。", { exact: false })).toBeVisible();
  await expect(page.getByRole("tab", { name: "DATA USER 認証" })).toHaveAttribute("aria-selected", "true");
});

test("DeepSec の構成状態の「再試行」は案内を残したまま押したボタンだけを回し、取り直せたら案内を消す（#2034）", async ({ page }) => {
  await mockDatabaseGateReady(page);
  let failing = true;
  let releaseRetry: () => void = () => undefined;
  const retryGate = new Promise<void>((resolve) => {
    releaseRetry = resolve;
  });
  await page.route("**/api/security/deepsec/status", async (route) => {
    if (failing) {
      await fulfill(route, "構成状態を取得できませんでした。接続を確認して再試行してください。", 503);
      return;
    }
    await retryGate;
    await fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: { data_grants: 2 },
      message: "構成済みです。",
    });
  });
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan()));
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");
  const notice = page.getByRole("alert").filter({ hasText: "構成状態を取得できませんでした。" });
  const retry = notice.getByTestId("security-deepsec-status-retry");
  await expect(retry).toBeVisible();
  failing = false;
  await retry.click();
  // 押しても案内は消えず、押したボタンだけが回る。
  await expect(notice).toBeVisible();
  await expect(retry).toHaveAttribute("aria-busy", "true");
  releaseRetry();
  await expect(notice).toHaveCount(0);
  await expect(page.getByText("状態取得失敗", { exact: true })).toHaveCount(0);
});

test("DeepSec 実行計画の取得失敗は標準 ErrorState で余白と再試行導線を示す", { tag: "@responsive" }, async ({
  page,
}, testInfo) => {
  await mockDatabaseGateReady(page);
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: false,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: {},
      message: "未適用です。",
    })
  );
  let planRequests = 0;
  let allowPlanSuccess = false;
  await page.route("**/api/security/deepsec/plan", async (route) => {
    planRequests += 1;
    if (!allowPlanSuccess) {
      const response = problemEnvelope({
        status: 409,
        code: "SECURITY_SCHEMA_MIGRATION_REQUIRED",
        title: "セキュリティ初期化が必要です",
        detail:
          "アプリケーション認証/RBAC の schema migration が未適用です。`uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap` を実行してから再試行してください。",
        requestId: "deepsec-plan-error-request",
      });
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        headers: { "X-Request-ID": response.problem.request_id },
        body: JSON.stringify(response),
      });
      return;
    }
    await fulfill(route, deepSecPlan());
  });
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "基盤構成" }).click();

  const planSection = page.locator(
    'section[aria-labelledby="security-deepsec-foundation-plan-title"]'
  );
  const planTitle = planSection.getByRole("heading", { name: "実行計画", exact: true });
  const errorState = planSection.locator(':scope > [role="alert"]');
  await expect(errorState).toHaveCount(1);
  await expect(errorState).toContainText(
    "アプリケーション認証/RBAC の schema migration が未適用です。"
  );
  await expect(errorState).toContainText("リクエストID: deepsec-plan-error-request");
  // 見出しとエラーの間は区画の gap-4（1rem。14px ルートで 14px）。px で書かず rem から求める（#800）。
  const sectionGap = (await remToPx(page, 1)) - SUBPIXEL_TOLERANCE;
  await expect
    .poll(async () => {
      const [titleBox, errorBox] = await Promise.all([
        planTitle.boundingBox(),
        errorState.boundingBox(),
      ]);
      if (!titleBox || !errorBox) return -1;
      return errorBox.y - (titleBox.y + titleBox.height);
    })
    .toBeGreaterThanOrEqual(sectionGap);
  const errorStateLayout = await errorState.evaluate((node) => {
    const style = window.getComputedStyle(node);
    return {
      parentLabelledBy: node.parentElement?.getAttribute("aria-labelledby"),
      paddingTop: Number.parseFloat(style.paddingTop),
    };
  });
  expect(errorStateLayout.parentLabelledBy).toBe("security-deepsec-foundation-plan-title");
  expect(errorStateLayout.paddingTop).toBeGreaterThanOrEqual(24);
  await expectNoPageHorizontalScroll(page);
  await page.screenshot({
    path: testInfo.outputPath(`deepsec-plan-error-${testInfo.project.name}.png`),
    fullPage: true,
  });

  const retryButton = errorState.getByRole("button", { name: "再試行" });
  await retryButton.focus();
  await expect(retryButton).toBeFocused();
  const planRequestsBeforeRetry = planRequests;
  allowPlanSuccess = true;
  await page.keyboard.press("Enter");

  await expect(errorState).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "V001.1 共有 DATA USER とロール" })).toBeVisible();
  expect(planRequests).toBe(planRequestsBeforeRetry + 1);
});

test("DeepSec の読込中のスピナーは「最新情報を取得」の 1 つだけにする（#416）", async ({ page }) => {
  await mockDatabaseGateReady(page);
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: false,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: {},
      message: "未適用です。",
    })
  );
  let releasePlan = () => {};
  const planGate = new Promise<void>((resolve) => {
    releasePlan = resolve;
  });
  await page.route("**/api/security/deepsec/plan", async (route) => {
    await planGate;
    await fulfill(route, deepSecPlan());
  });
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "基盤構成" }).click();
  const planLoading = page.getByTestId("security-deepsec-plan-loading");
  try {
    await expect(planLoading).toBeVisible();
    await expect(planLoading.getByRole("timer")).toBeVisible();
    // 計画の読込は経過時間と Skeleton だけ。スピナーはヘッダーの「最新情報を取得」の 1 つ。
    await expect(visibleSpinners(planLoading)).toHaveCount(0);
    await expectSingleSpinner(page, page.getByRole("button", { name: "最新情報を取得", exact: true }));
  } finally {
    releasePlan();
  }
  await expect(page.getByRole("heading", { name: "V001.1 共有 DATA USER とロール" })).toBeVisible();
  await expect(visibleSpinners(page)).toHaveCount(0);
});

test("DeepSec は Thick mode でも SQL step をキーボード操作できる", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await mockDatabaseGateReady(page);
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: false,
      driver_mode: "thick",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: {},
      message: "未適用です。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) =>
    fulfill(route, deepSecPlan(false, "thick"))
  );
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");

  await expect(page.getByText("Deep Data Security が無効です。", { exact: false })).toHaveCount(0);
  await page.getByRole("tab", { name: "基盤構成" }).click();
  await expect(page.getByTestId("security-deepsec-step-1").getByTestId("execution-confirmation-field")).toHaveCount(0);
  await expect(page.getByTestId("security-deepsec-step-2").getByTestId("execution-confirmation-field")).toHaveCount(0);
  const applySection = page.getByTestId("security-deepsec-foundation-apply-section");
  const confirmationField = applySection.getByTestId("execution-confirmation-field");
  const confirmationInput = confirmationField.getByRole("textbox", { name: "実行確認語" });
  const applyButton = confirmationField.getByRole("button", { name: "基盤構成を適用" });
  await expect(applyButton).toBeDisabled();
  await confirmationInput.focus();
  await expect(confirmationInput).toBeFocused();
  await page.keyboard.type("ADMIN_EXECUTE");
  await expect(confirmationField.getByText("確認済み")).toBeVisible();
  await expect(applyButton).toBeEnabled();
  await applyButton.focus();
  await expect(applyButton).toBeFocused();
  await expectNoPageHorizontalScroll(page);
});

for (const driverMode of ["thin", "thick"] as const) {
  test(`DeepSec 無効時は ${driverMode} mode で有効化手順を表示する`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await mockDatabaseGateReady(page);
    await page.route("**/api/security/deepsec/status", (route) =>
      fulfill(route, {
        configured: false,
        driver_mode: driverMode,
        connection_security: "wallet_mtls",
        deepsec_enabled: false,
        data_user: "DEEPSEC_DATA_USER",
        has_data_user_password: false,
        objects: {},
        message: "未適用です。",
      })
    );
    await page.route("**/api/security/deepsec/plan", (route) =>
      fulfill(route, deepSecPlan(false, driverMode, false, false))
    );
    await mockDeepSecDataEntitlements(page);

    await page.goto("/settings/security/deepsec");

    const disabledBanner = page
      .getByRole("status")
      .filter({ hasText: "Deep Data Security が無効です。" });
    await expect(disabledBanner).toBeVisible();
    await expect(page.getByText("未構成", { exact: true }).first()).toBeVisible();
    await page.getByRole("tab", { name: "基盤構成" }).click();
    await expect(page.getByText("基盤構成を始める前に、DATA USER パスワードを保存してください。V001.1 はこのパスワードで DATA USER（END USER）を作ります（既にあれば作り直しません）。", { exact: true })).toBeVisible();
    const applySection = page.getByTestId("security-deepsec-foundation-apply-section");
    await expect(applySection.getByRole("button", { name: "基盤構成を適用" })).toBeDisabled();
    await expectNoPageHorizontalScroll(page);
  });
}

test("DeepSec は DATA USER password をページから保存し再起動なしで適用可能にする", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  let hasPassword = false;
  let savedPassword = "";
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: false,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: hasPassword,
      objects: {},
      message: "未適用です。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) =>
    fulfill(route, deepSecPlan(false, "thin", true, hasPassword))
  );
  await page.route("**/api/security/deepsec/config", async (route) => {
    const payload = route.request().postDataJSON() as { data_user_password: string };
    savedPassword = payload.data_user_password;
    hasPassword = true;
    await fulfill(route, {
      configured: false,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: {},
      message: "未適用です。",
    });
  });
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");

  await expect(
    page.getByText("API を再起動せずに次の適用・検証から使用できます。", {
      exact: false,
    })
  ).toBeVisible();
  await expect(page.getByText("未設定", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "基盤構成" }).click();
  await expect(page.getByText("基盤構成を始める前に、DATA USER パスワードを保存してください。V001.1 はこのパスワードで DATA USER（END USER）を作ります（既にあれば作り直しません）。", { exact: true })).toBeVisible();
  await expect(page.getByTestId("security-deepsec-foundation-apply-section").getByRole("button", { name: "基盤構成を適用" })).toBeDisabled();
  await page.getByRole("button", { name: "DATA USER 認証へ" }).click();

  const dataUserPanel = page.locator("#security-deepsec-panel-data-user");
  const configFields = dataUserPanel.getByTestId("security-deepsec-config-fields");
  const configActions = dataUserPanel.getByTestId("security-deepsec-config-actions");
  const password = page.getByLabel("DATA USER パスワード");
  const saveButton = configActions.getByRole("button", { name: "保存", exact: true });
  const syncButton = configActions.getByRole("button", { name: "Oracle へ同期", exact: true });
  await expect(saveButton).toBeDisabled();
  await expect(saveButton).toHaveAttribute("type", "submit");
  await expect(syncButton).toBeDisabled();
  await expect(syncButton).toHaveAttribute("type", "button");

  await password.fill("DeepSecret!789");
  await expect(saveButton).toBeEnabled();
  await expect(syncButton).toBeDisabled();

  const [fieldsBox, actionsBox, saveButtonBox, syncButtonBox] = await Promise.all([
    configFields.boundingBox(),
    configActions.boundingBox(),
    saveButton.boundingBox(),
    syncButton.boundingBox(),
  ]);
  expect(fieldsBox).not.toBeNull();
  expect(actionsBox).not.toBeNull();
  expect(saveButtonBox).not.toBeNull();
  expect(syncButtonBox).not.toBeNull();
  expect(actionsBox!.y).toBeGreaterThanOrEqual(fieldsBox!.y + fieldsBox!.height - 1);
  expect(Math.abs(actionsBox!.x - fieldsBox!.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(saveButtonBox!.x - actionsBox!.x)).toBeLessThanOrEqual(1);

  const viewportWidth = page.viewportSize()?.width ?? 0;
  if (viewportWidth < 640) {
    expect(Math.abs(saveButtonBox!.width - actionsBox!.width)).toBeLessThanOrEqual(1);
    expect(Math.abs(syncButtonBox!.width - actionsBox!.width)).toBeLessThanOrEqual(1);
    expect(saveButtonBox!.height).toBeGreaterThanOrEqual(44);
    expect(syncButtonBox!.height).toBeGreaterThanOrEqual(44);
  } else {
    expect(saveButtonBox!.width).toBeLessThan(actionsBox!.width);
    expect(syncButtonBox!.width).toBeLessThan(actionsBox!.width);
    expect(saveButtonBox!.height).toBe(40);
    expect(syncButtonBox!.height).toBe(40);
  }
  await expectNoPageHorizontalScroll(page);

  // 「${...}」は backend/.env を読み直すと環境変数として展開されるので、送らずに入力の誤りを示す。
  await password.fill("Abc${HOME}defghij");
  await saveButton.click();
  await expect(
    page.getByText("DATA USER パスワードに二重引用符、「${」、改行、制御文字は使用できません。", { exact: true }),
  ).toBeVisible();
  expect(savedPassword).toBe("");
  await password.fill("DeepSecret!789");

  await saveButton.focus();
  await expect(saveButton).toBeFocused();
  await saveButton.press("Enter");

  expect(savedPassword).toBe("DeepSecret!789");
  await expect(password).toHaveValue("");
  await expect(page.getByText("保存済み", { exact: true })).toBeVisible();
  await expect(syncButton).toBeEnabled();
  await expect(page.getByText("API を再起動")).toHaveCount(0);
  await page.getByRole("tab", { name: "基盤構成" }).click();
  await expect(page.getByText("基盤構成を始める前に、DATA USER パスワードを保存してください。V001.1 はこのパスワードで DATA USER（END USER）を作ります（既にあれば作り直しません）。", { exact: true })).toHaveCount(0);
  const applySection = page.getByTestId("security-deepsec-foundation-apply-section");
  const confirmationField = applySection.getByTestId("execution-confirmation-field");
  const confirmationInput = confirmationField.getByRole("textbox", { name: "実行確認語" });
  const applyButton = confirmationField.getByRole("button", { name: "基盤構成を適用" });
  await expect(confirmationInput).toBeEnabled();
  await expect(applyButton).toBeDisabled();
  await confirmationInput.fill("ADMIN_EXECUTE");
  await expect(applyButton).toBeEnabled();
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec は保存済み DATA USER password を変更なしで Oracle へ同期できる", async ({ page }) => {
  await mockDatabaseGateReady(page);
  let syncCalls = 0;
  let syncPostData: string | null = "not-called";
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: {},
      message: "構成済みです。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) =>
    fulfill(route, deepSecPlan(false, "thin", true, true))
  );
  await page.route("**/api/security/deepsec/config/sync-password", async (route) => {
    syncCalls += 1;
    syncPostData = route.request().postData();
    await fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: {},
      message: "構成済みです。",
    });
  });
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");

  const dataUserPanel = page.locator("#security-deepsec-panel-data-user");
  const configActions = dataUserPanel.getByTestId("security-deepsec-config-actions");
  const password = page.getByLabel("DATA USER パスワード");
  const saveButton = configActions.getByRole("button", { name: "保存", exact: true });
  const syncButton = configActions.getByRole("button", { name: "Oracle へ同期", exact: true });
  await expect(saveButton).toBeDisabled();
  await expect(syncButton).toBeEnabled();

  await syncButton.click();

  await expect.poll(() => syncCalls).toBe(1);
  expect(syncPostData).toBeNull();
  await expect(
    page.getByText("保存済み DATA USER パスワードを Oracle END USER へ同期しました。", {
      exact: true,
    })
  ).toBeVisible();

  await password.fill("DeepSecret!789");
  await expect(saveButton).toBeEnabled();
  await expect(syncButton).toBeDisabled();
  await expectNoPageHorizontalScroll(page);
});

// #608: 対象 table/view は 1 つを選ぶので、検索できる選択欄（SearchableSelectField）。候補は数千件になりうるので、
// サーバー側で検索し、続きは 50 件ずつ読む。desktop / 375px（projects）で確かめる。
test(`DeepSec の対象 table/view は大量でも検索できる選択欄で選べる`, async ({ page }, testInfo) => {
  await mockDatabaseGateReady(page);
  const role = {
    ...systemRole,
    role_id: "role-many",
    role_code: "MANY_DATA_ROLE",
    display_name: "大量対象ロール",
    is_built_in: false,
    permissions: ["menu.security_deepsec"],
    data_entitlements: [],
  };
  const objects = Array.from({ length: 3000 }, (_, index) => ({
    ...deepSecTargetObject,
    owner: index % 2 === 0 ? "SALES" : "HR",
    name: `TABLE_${String(index + 1).padStart(4, "0")}`,
    qualified_name: `${index % 2 === 0 ? "SALES" : "HR"}.TABLE_${String(index + 1).padStart(4, "0")}`,
    comment: `対象 ${index + 1}`,
  }));
  const requests: URL[] = [];
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: { data_grants: 0 },
      message: "構成済みです。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan(true)));
  await page.route("**/api/security/deepsec/data-entitlements", (route) => fulfill(route, [role]));
  await page.route("**/api/security/deepsec/target-objects?*", (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    // backend と同じく `OWNER.OBJECT` とコメントを照合し、カーソルで 50 件ずつ返す（件数は返さない）。
    const q = (url.searchParams.get("q") ?? "").toUpperCase();
    const matched = objects.filter((item) => !q || `${item.qualified_name} ${item.comment}`.toUpperCase().includes(q));
    const offset = Number(url.searchParams.get("cursor") ?? "0");
    const limit = Number(url.searchParams.get("limit") ?? "50");
    return fulfill(route, {
      runtime: "oracle",
      owner: "",
      items: matched.slice(offset, offset + limit),
      total: null,
      counts_included: false,
      next_cursor: offset + limit < matched.length ? String(offset + limit) : null,
      warnings: [],
    });
  });

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限" }).click();
  const entitlementForm = page.getByTestId("security-deepsec-entitlement-form");
  await entitlementForm.getByRole("button", { name: "データ権限を追加" }).click();
  const objectButton = entitlementForm.locator("#deepsec-entitlement-resource-0");
  await expect(objectButton).toContainText("対象 object を選択");
  await objectButton.click();
  const popover = page.getByRole("dialog", { name: "対象 table/view" });
  const search = popover.getByRole("combobox");
  await expect(search).toBeFocused();
  await expect(popover.getByRole("option")).toHaveCount(50);
  await expect(popover.getByText("50 件", { exact: true })).toBeVisible();

  // 一覧の下端までスクロールすると続きの 50 件を読む（「さらに読み込む」のボタンは置かない）。
  await popover.getByRole("listbox").evaluate((node) => node.scrollTo({ top: node.scrollHeight }));
  await expect(popover.getByRole("option")).toHaveCount(100);
  expect(requests.at(-1)?.searchParams.get("cursor")).toBe("50");

  // スキーマ名を付けた名前でもサーバー側で検索する。↓ と Enter で選び、選択欄へ戻る。
  await search.fill("hr.table_2999");
  await expect(popover.getByRole("option")).toHaveCount(0);
  await search.fill("hr.table_300");
  await expect(popover.getByRole("option")).toHaveCount(1);
  expect(requests.at(-1)?.searchParams.get("q")).toBe("hr.table_300");
  expect(requests.at(-1)?.searchParams.has("owner_prefix")).toBe(false);
  await search.press("Enter");
  await expect(popover).toHaveCount(0);
  await expect(objectButton).toBeFocused();
  await expect(objectButton).toContainText("HR.TABLE_3000");
  await expect(entitlementForm.getByTestId("security-deepsec-entitlement-editor-title-0")).toHaveText("HR.TABLE_3000");

  // 閉じると検索語を消し、開き直すと 1 ページ目の候補に戻る。
  await objectButton.click();
  await expect(search).toHaveValue("");
  await expect(popover.getByRole("option")).toHaveCount(50);
  await page.screenshot({ path: testInfo.outputPath(`deepsec-object-select-light.png`) });
  await search.press("Escape");
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec Data Grant picker は live metadata 0 件時に総件数を誤表示しない", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const emptyRole = {
    ...systemRole,
    role_id: "role-empty",
    role_code: "EMPTY_DATA_ROLE",
    display_name: "空データロール",
    is_built_in: false,
    permissions: ["menu.security_deepsec"],
    data_entitlements: [],
  };
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: { data_grants: 0 },
      message: "構成済みです。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan(true)));
  await page.route("**/api/security/deepsec/data-entitlements", (route) =>
    fulfill(route, [emptyRole])
  );
  await page.route("**/api/security/deepsec/target-objects?*", (route) =>
    fulfill(route, {
      runtime: "oracle",
      owner: "",
      items: [],
      total: null,
      counts_included: false,
      next_cursor: null,
      warnings: [],
    })
  );

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限" }).click();
  const entitlementForm = page.getByTestId("security-deepsec-entitlement-form");
  await entitlementForm.getByRole("button", { name: "データ権限を追加" }).click();
  // 対象 table/view は検索できる選択欄（#608）。候補の一覧は開いた選択欄（dialog）の中に出す。
  const objectPicker = entitlementForm.getByTestId("security-deepsec-object-picker-0");
  await objectPicker.locator("#deepsec-entitlement-resource-0").click();
  const objectPopover = page.getByRole("dialog", { name: "対象 table/view" });

  await expect(objectPopover.getByText("0 / 0 件", { exact: true })).toHaveCount(0);
  await expect(objectPopover.getByText("0 件", { exact: true })).toBeVisible();
  await expect(objectPopover.getByText(/参照可能な対象 object がありません。/)).toBeVisible();
  await expect(
    objectPopover.getByText(/Oracle 接続ユーザーの参照権限、所有者、NL2SQL_SCHEMA_OWNER_ALLOWLIST を確認してください。/)
  ).toBeVisible();

  await objectPopover.getByRole("combobox").fill("ORDERS");
  await expect(objectPopover.getByText("条件に一致する object はありません。")).toBeVisible();
  await expect(objectPopover.getByText(/参照可能な対象 object がありません。/)).toHaveCount(0);
  await objectPopover.getByRole("combobox").press("Escape");
  await expectNoPageHorizontalScroll(page);
});

// #1708: 1 ページ目の候補が一覧の高さに満たず続きがあるとき（以前の backend は共通基盤・他製品のシステムテーブルを
// 取った後に落とし、1 ページ目が表 3 件だけになった）、スクロールを待たずに続きを読み、後ろに並ぶ view まで出す。
test("DeepSec の対象 table/view は 1 ページ目が短くても続きの view まで出す", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const role = {
    ...systemRole,
    role_id: "role-short-page",
    role_code: "SHORT_PAGE_ROLE",
    display_name: "短いページのロール",
    is_built_in: false,
    permissions: ["menu.security_deepsec"],
    data_entitlements: [],
  };
  const object = (name: string, objectType: string) => ({
    ...deepSecTargetObject,
    owner: "ADMIN",
    name,
    qualified_name: `ADMIN.${name}`,
    object_type: objectType,
    comment: "",
  });
  const requests: URL[] = [];
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: { data_grants: 0 },
      message: "構成済みです。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan(true)));
  await page.route("**/api/security/deepsec/data-entitlements", (route) => fulfill(route, [role]));
  await page.route("**/api/security/deepsec/target-objects?*", (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    const second = url.searchParams.get("cursor") === "page-2";
    return fulfill(route, {
      runtime: "oracle",
      owner: "",
      items: second
        ? [object("V_DEPT_PROJECT", "VIEW"), object("V_EMP_DEPT", "VIEW")]
        : [object("DEPARTMENT", "TABLE"), object("EMPLOYEE", "TABLE"), object("PROJECT", "TABLE")],
      total: null,
      counts_included: false,
      next_cursor: second ? null : "page-2",
      warnings: [],
    });
  });

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限" }).click();
  const entitlementForm = page.getByTestId("security-deepsec-entitlement-form");
  await entitlementForm.getByRole("button", { name: "データ権限を追加" }).click();
  await entitlementForm.locator("#deepsec-entitlement-resource-0").click();
  const popover = page.getByRole("dialog", { name: "対象 table/view" });

  // スクロールも ↓ もせずに、続きの view まで出る。
  await expect(popover.getByRole("option")).toHaveCount(5);
  await expect(popover.getByRole("option", { name: /ADMIN\.V_EMP_DEPT/ })).toBeVisible();
  await expect(popover.getByText("5 件", { exact: true })).toBeVisible();
  // 続きは 1 回だけ読む（続きが無くなったら止まる）。
  expect(requests.filter((url) => url.searchParams.get("cursor") === "page-2")).toHaveLength(1);
  expect(requests.at(-1)?.searchParams.get("cursor")).toBe("page-2");
  await popover.getByRole("option", { name: /ADMIN\.V_EMP_DEPT/ }).click();
  await expect(entitlementForm.getByTestId("security-deepsec-entitlement-editor-title-0")).toHaveText("ADMIN.V_EMP_DEPT");
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec は構造化データ権限をロール別に編集する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockDatabaseGateReady(page);
  const queryRole = {
    role_id: "role-query",
    // ロール一覧はロールコード順（#584）。既定選択を先頭にするためコードで先頭に並べる。
    role_code: "APP_QUERY_VIEWER",
    display_name: "アプリ検索閲覧",
    description: "業務データ参照",
    is_built_in: false,
    archived: false,
    version: 3,
    data_entitlements: [],
  };
  const archivedRole = {
    ...queryRole,
    role_id: "role-archived",
    role_code: "ARCHIVED_VIEWER",
    display_name: "廃止ロール",
    archived: true,
    version: 1,
  };
  const extraRoles = Array.from({ length: 5 }, (_, index) => ({
    ...queryRole,
    role_id: `role-extra-${index}`,
    role_code: `EXTRA_${index}`,
    display_name: `追加ロール${index + 1}`,
    version: 1,
    data_entitlements: [],
  }));
  let entitlementRoles: unknown[] = [systemRole, queryRole, archivedRole, ...extraRoles];
  type DataEntitlementPayload = {
    entitlement_id?: string;
    resource_code: string;
    scope_code: string;
    capability: string;
    target_owner: string;
    target_object: string;
    target_type: string;
    column_names: string[];
    scope_mode: string;
    scope_column: string;
    scope_filters: Array<Record<string, unknown>>;
  };
  let previewPayload: {
    version: number;
    data_entitlements: DataEntitlementPayload[];
  } | null = null;
  const applyPayloads: Array<{
    version: number;
    confirmation: string;
    data_entitlements: DataEntitlementPayload[];
  }> = [];
  let failNextApply = false;
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: { data_grants: 2 },
      message: "構成済みです。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan(true)));
  const salesObject = {
    name: "ORDERS",
    owner: "SALES",
    qualified_name: "SALES.ORDERS",
    object_type: "TABLE",
    comment: "受注",
  };
  const scrollableSalesObjects = [
    salesObject,
    ...Array.from({ length: 6 }, (_, index) => ({
      ...salesObject,
      name: `ORDER_HISTORY_${index + 1}`,
      qualified_name: `SALES.ORDER_HISTORY_${index + 1}`,
      comment: `受注履歴${index + 1}`,
    })),
  ];
  const salesObjectDetail = {
    ...salesObject,
    columns: [
      {
        column_name: "ORDER_ID",
        logical_name: "受注ID",
        data_type: "NUMBER",
        nullable: false,
        comment: "",
        sample_values: [],
      },
      {
        column_name: "CUSTOMER_NAME",
        logical_name: "顧客名",
        data_type: "VARCHAR2(120)",
        nullable: false,
        comment: "",
        sample_values: [],
      },
      {
        column_name: "REGION_CODE",
        logical_name: "地域",
        data_type: "VARCHAR2(32)",
        nullable: false,
        comment: "",
        sample_values: [],
      },
      ...Array.from({ length: 12 }, (_, index) => ({
        column_name: `AUDIT_COLUMN_${String(index + 1).padStart(2, "0")}`,
        logical_name: `監査列${index + 1}`,
        data_type: "VARCHAR2(64)",
        nullable: true,
        comment: "",
        sample_values: [],
      })),
    ],
  };
  const expectedScopeFilters = [
    {
      column_name: "REGION_CODE",
      operator: "IN",
      value_type: "TEXT",
      value_source: "LITERAL",
      value: "",
      value_to: "",
      values: ["SALES", "HR"],
    },
    {
      column_name: "ORDER_ID",
      operator: "EQ",
      value_type: "NUMBER",
      value_source: "LOGIN_USER_ID",
      value: "",
      value_to: "",
      values: [],
    },
  ];
  const objectRequests: Array<{
    limit: string | null;
    cursor: string | null;
    ownerPrefix: string | null;
    q: string | null;
  }> = [];
  await page.route("**/api/security/deepsec/target-objects?*", async (route) => {
    const url = new URL(route.request().url());
    const cursor = url.searchParams.get("cursor");
    const ownerPrefix = url.searchParams.get("owner_prefix");
    const q = url.searchParams.get("q");
    objectRequests.push({
      limit: url.searchParams.get("limit"),
      cursor,
      ownerPrefix,
      q,
    });
    const items =
      ownerPrefix === "SAL" || q === "ORDERS"
        ? scrollableSalesObjects
        : cursor === "deepsec-page-2"
          ? scrollableSalesObjects
          : [deepSecTargetObject];
    await fulfill(route, {
      runtime: "oracle",
      owner: ownerPrefix ?? "",
      items,
      total: null,
      counts_included: false,
      next_cursor: ownerPrefix || q || cursor ? null : "deepsec-page-2",
      warnings: [],
    });
  });
  await page.route("**/api/security/deepsec/target-objects/HR/EMPLOYEES**", (route) =>
    fulfill(route, deepSecTargetObjectDetail)
  );
  await page.route("**/api/security/deepsec/target-objects/SALES/ORDERS**", (route) =>
    fulfill(route, salesObjectDetail)
  );
  await page.route("**/api/security/deepsec/data-entitlements", (route) =>
    fulfill(route, entitlementRoles)
  );
  await page.route("**/api/security/deepsec/data-entitlements/role-query/preview", async (route) => {
    const payload = route.request().postDataJSON() as {
      version: number;
      data_entitlements: DataEntitlementPayload[];
    };
    previewPayload = payload;
    const cleanupSql = payload.data_entitlements.length
      ? []
      : [
          "SET USE DATA GRANTS ONLY ON SALES.ORDERS DISABLED",
          "DROP DATA GRANT IF EXISTS APP_OWNER.NL2SQL_DG_PREVIEW",
        ];
    await fulfill(route, {
      role_id: "role-query",
      version: payload.version,
      data_entitlements: payload.data_entitlements.map((item) => ({
        entitlement_id: item.entitlement_id ?? "preview-0",
        data_grant_name: "NL2SQL_DG_PREVIEW",
        sql_checksum: "f".repeat(64),
        apply_status: "PENDING",
        apply_error_message: "",
        applied_at: null,
        sql: [`GRANT SELECT ON ${String(item.resource_code)} TO NL2SQL_APP_DB_ROLE`],
        checksum: "f".repeat(64),
        ...item,
      })),
      cleanup_sql: cleanupSql,
      checksum: "a".repeat(64),
    });
  });
  await page.route("**/api/security/deepsec/data-entitlements/role-query/apply", async (route) => {
    const payload = route.request().postDataJSON() as {
      version: number;
      confirmation: string;
      data_entitlements: DataEntitlementPayload[];
    };
    applyPayloads.push(payload);
    if (failNextApply) {
      failNextApply = false;
      await fulfill(route, "Oracle で Data Grant の適用に失敗しました。", 500);
      return;
    }
    const currentRole = entitlementRoles.find(
      (role) => (role as { role_id?: string }).role_id === "role-query"
    ) as Record<string, unknown> & { data_entitlements: Array<Record<string, unknown>> };
    const updated = {
      ...currentRole,
      version: payload.version + 1,
      data_entitlements: payload.data_entitlements.map((item, index) => ({
        entitlement_id: item.entitlement_id ?? `saved-${index}`,
        data_grant_name: item.entitlement_id ? "NL2SQL_DG_PREVIEW" : "NL2SQL_DG_SAVED",
        sql_checksum: "e".repeat(64),
        apply_status: "APPLIED",
        apply_error_message: "",
        applied_at: "2026-08-28T00:00:00Z",
        sql: [],
        checksum: "e".repeat(64),
        ...item,
      })),
    };
    entitlementRoles = [
      systemRole,
      updated,
      archivedRole,
    ];
    await fulfill(route, {
      role: updated,
      status: "APPLIED",
      checksum: "b".repeat(64),
      cleanup_count: 0,
      applied_count: updated.data_entitlements.length,
    });
  });

  await page.goto("/settings/security/deepsec");

  await page.getByRole("tab", { name: "データ権限" }).click();
  await expect(page.getByRole("heading", { name: "構造化データ権限" })).toBeVisible();
  const roleList = page.getByTestId("security-deepsec-entitlement-roles");
  await expect
    .poll(() =>
      roleList.evaluate((node) => ({
        scrolls: node.scrollHeight > node.clientHeight + 1,
        bounded: node.clientHeight <= 300,
      }))
    )
    .toEqual({ scrolls: true, bounded: true });
  await expect.poll(() => objectRequests[0]).toMatchObject({
    limit: "50",
    cursor: null,
    ownerPrefix: null,
  });
  const entitlementForm = page.getByTestId("security-deepsec-entitlement-form");
  await expect(page.getByTestId("security-deepsec-entitlement-role-role-query")).toHaveAttribute(
    "aria-pressed",
    "true"
  );
  await expect(
    entitlementForm.getByText("組み込みロールの構造化データ権限は変更できません。", {
      exact: true,
    })
  ).toHaveCount(0);
  await expect(entitlementForm.getByRole("button", { name: "Data Grant を適用" })).toBeDisabled();

  await page.getByTestId("security-deepsec-entitlement-role-role-query").click();
  await entitlementForm.getByRole("button", { name: "データ権限を追加" }).click();
  const firstRuleTab = entitlementForm.getByTestId("security-deepsec-entitlement-rule-tab-0");
  const firstRule = entitlementForm.getByTestId("security-deepsec-entitlement-rule-0");
  await expect(firstRuleTab).toHaveAttribute("aria-pressed", "true");
  await expect(firstRule.getByText("Data Grant", { exact: true })).toBeVisible();
  await expect(firstRule.getByText("Data Grant 1", { exact: true })).toHaveCount(0);
  const objectPicker = firstRule.getByTestId("security-deepsec-object-picker-0");
  await expect(objectPicker).toBeVisible();
  // 対象 table/view は検索できる選択欄（SearchableSelectField。#608）。1 つを選ぶので、手書きの一覧・所有者の欄・
  // 「さらに読み込む」のボタンは持たない。続きは最後の候補から ↓（または一覧の下端までのスクロール）で読む。
  const objectButton = objectPicker.locator("#deepsec-entitlement-resource-0");
  // ボタン（role=button）は aria-required を持てない（axe の aria-allowed-attr。#1595）。
  await expect(objectButton).not.toHaveAttribute("aria-required");
  await expect(objectPicker.getByRole("searchbox")).toHaveCount(0);
  await objectButton.click();
  const objectPopover = page.getByRole("dialog", { name: "対象 table/view" });
  const objectSearch = objectPopover.getByRole("combobox");
  await expect(objectSearch).toBeFocused();
  await expect(objectSearch).toHaveAttribute("placeholder", "スキーマ名・名前・コメントで検索");
  await expect(objectPopover.getByRole("option", { name: /HR\.EMPLOYEES/ })).toBeVisible();
  await objectSearch.press("ArrowDown");
  await expect.poll(() => objectRequests.some((request) => request.cursor === "deepsec-page-2")).toBeTruthy();
  await expect(objectPopover.getByRole("option", { name: /SALES\.ORDERS/ })).toBeVisible();
  // 検索は入力に合わせてサーバーの q で絞る（親で遅延させない）。所有者の接頭辞は送らない。
  await objectSearch.fill("ORDERS");
  await expect.poll(() => objectRequests.some((request) => request.q === "ORDERS")).toBeTruthy();
  expect(objectRequests.every((request) => request.ownerPrefix === null)).toBe(true);
  await objectPopover.getByRole("option", { name: /SALES\.ORDERS/ }).click();
  await expect(objectPopover).toHaveCount(0);
  await expect(objectButton).toBeFocused();
  await expect(objectButton).toContainText("SALES.ORDERS");
  await expect(entitlementForm.getByText("CUSTOMER_NAME", { exact: true })).toBeVisible();
  const scopeModeSelect = entitlementForm.getByLabel("行 scope");
  const columnsFieldset = entitlementForm.getByRole("group", { name: /許可列/ });
  const columnsLegend = columnsFieldset.locator("legend");
  const columnActions = firstRule.getByTestId("security-deepsec-entitlement-column-selection-actions-0");
  const columnsGrid = firstRule.getByTestId("security-deepsec-entitlement-columns-grid-0");
  const main = page.getByRole("main");
  const selectAllColumnsButton = columnActions.getByTestId(
    "security-deepsec-entitlement-column-selection-actions-0-select"
  );
  const clearAllColumnsButton = columnActions.getByTestId(
    "security-deepsec-entitlement-column-selection-actions-0-clear"
  );
  await expect(scopeModeSelect).toBeVisible();
  await expect(columnsFieldset).toBeVisible();
  await expect(columnActions).toBeVisible();
  await expect(columnsGrid).toBeVisible();
  await expect(selectAllColumnsButton).toBeEnabled();
  await expect(clearAllColumnsButton).toBeDisabled();
  await setElementScrollTop(firstRule, 64);
  await selectAllColumnsButton.scrollIntoViewIfNeeded();
  if ((await main.evaluate((node) => node.scrollTop)) === 0) {
    await setMainScrollTop(page, 32);
  }
  await selectAllColumnsButton.scrollIntoViewIfNeeded();
  await setElementScrollTop(columnsGrid, 72);
  await expect
    .poll(() =>
      Promise.all(
        [main, firstRule, columnsGrid].map((container) =>
          container.evaluate((node) => node.scrollTop)
        )
      ).then((positions) => positions.every((position) => position > 0))
    )
    .toBeTruthy();
  const preservedScrollContainers = [main, firstRule, columnsGrid];
  await expectScrollPositionsPreserved(page, preservedScrollContainers, () =>
    selectAllColumnsButton.click()
  );
  await expect(selectAllColumnsButton).toBeDisabled();
  await expect(clearAllColumnsButton).toBeEnabled();
  await expect(firstRule.getByRole("checkbox", { name: /ORDER_ID/ })).toBeChecked();
  await expect(firstRule.getByRole("checkbox", { name: /CUSTOMER_NAME/ })).toBeChecked();
  await expect(firstRule.getByRole("checkbox", { name: /REGION_CODE/ })).toBeChecked();
  await expectScrollPositionsPreserved(page, preservedScrollContainers, () =>
    clearAllColumnsButton.click()
  );
  await expect(selectAllColumnsButton).toBeEnabled();
  await expect(clearAllColumnsButton).toBeDisabled();
  await expect(firstRule.getByRole("checkbox", { name: /ORDER_ID/ })).not.toBeChecked();
  await expect(firstRule.getByRole("checkbox", { name: /CUSTOMER_NAME/ })).not.toBeChecked();
  await expect(firstRule.getByRole("checkbox", { name: /REGION_CODE/ })).not.toBeChecked();
  await selectAllColumnsButton.focus();
  await expectScrollPositionsPreserved(page, preservedScrollContainers, () =>
    selectAllColumnsButton.press("Enter")
  );
  await expect(firstRule.getByRole("checkbox", { name: /ORDER_ID/ })).toBeChecked();
  await clearAllColumnsButton.focus();
  await expectScrollPositionsPreserved(page, preservedScrollContainers, () =>
    clearAllColumnsButton.press("Space")
  );
  await expect(firstRule.getByRole("checkbox", { name: /ORDER_ID/ })).not.toBeChecked();
  // 対象 object は検索できる選択欄（#608）。ボタンは aria-required を持てないので、必須はラベルの「必須」のタグを
  // 読み上げて伝える（タグは aria-hidden にしない。#1595）。
  await expect(objectPicker.locator("label")).toHaveText("対象 table/view必須");
  await expect(objectPicker.locator("label [aria-hidden='true']")).toHaveCount(0);
  await expect(objectButton).not.toHaveAttribute("aria-required");
  await expect(firstRule.getByTestId("security-deepsec-entitlement-editor-title-0")).toHaveText("SALES.ORDERS");
  await expect(firstRule.getByText("Data Grant 1", { exact: true })).toHaveCount(0);
  const scopeModeLabelText = entitlementForm.locator("label[for='deepsec-entitlement-scope-mode-0']");
  const scopeModeRequired = entitlementForm.locator(
    "label[for='deepsec-entitlement-scope-mode-0'] [aria-hidden='true']"
  );
  await expect(scopeModeLabelText).toHaveText("行 scope必須");
  await expect(scopeModeSelect).toHaveAttribute("aria-required", "true");
  await expect(scopeModeRequired).toHaveText("必須");
  // legend には aria-required が無いので、必須バッジは読み上げ対象のまま。
  await expect(columnsFieldset.locator("legend")).toContainText("必須");
  await expect(columnsFieldset.locator("legend [aria-hidden='true']")).toHaveCount(0);
  await expect
    .poll(async () => {
      const [
        targetBox,
        columnsBox,
        columnsLegendBox,
        columnActionsBox,
        columnsGridBox,
        scopeBox,
        scopeLabelTextBox,
        scopeRequiredBox,
      ] = await Promise.all([
        objectPicker.boundingBox(),
        columnsFieldset.boundingBox(),
        columnsLegend.boundingBox(),
        columnActions.boundingBox(),
        columnsGrid.boundingBox(),
        scopeModeSelect.boundingBox(),
        scopeModeLabelText.boundingBox(),
        scopeModeRequired.boundingBox(),
      ]);
      const labelCenterY = scopeLabelTextBox
        ? scopeLabelTextBox.y + scopeLabelTextBox.height / 2
        : null;
      const requiredCenterY = scopeRequiredBox
        ? scopeRequiredBox.y + scopeRequiredBox.height / 2
        : null;

      return {
        columnsBelowTarget: Boolean(targetBox && columnsBox && columnsBox.y >= targetBox.y + targetBox.height - 1),
        columnActionsBelowLegend: Boolean(
          columnsLegendBox &&
            columnActionsBox &&
            columnActionsBox.y >= columnsLegendBox.y + columnsLegendBox.height + 3
        ),
        columnActionsStartWithGrid: Boolean(
          columnActionsBox &&
            columnsGridBox &&
            Math.abs(columnActionsBox.x - columnsGridBox.x) <= 1
        ),
        columnsGridBelowActions: Boolean(
          columnActionsBox &&
            columnsGridBox &&
            columnsGridBox.y >= columnActionsBox.y + columnActionsBox.height - 1
        ),
        scopeBelowColumns: Boolean(columnsBox && scopeBox && scopeBox.y >= columnsBox.y + columnsBox.height - 1),
        scopeStartsWithTarget: Boolean(targetBox && scopeBox && scopeBox.x <= targetBox.x + 1),
        scopeRequiredAboveSelect: Boolean(scopeRequiredBox && scopeBox && scopeRequiredBox.y + scopeRequiredBox.height <= scopeBox.y),
        scopeRequiredInlineWithLabel: Boolean(
          labelCenterY !== null &&
          requiredCenterY !== null &&
          Math.abs(labelCenterY - requiredCenterY) <= 3
        ),
      };
    })
    .toEqual({
      columnsBelowTarget: true,
      columnActionsBelowLegend: true,
      columnActionsStartWithGrid: true,
      columnsGridBelowActions: true,
      scopeBelowColumns: true,
      scopeStartsWithTarget: true,
      scopeRequiredAboveSelect: true,
      scopeRequiredInlineWithLabel: true,
    });
  expect(await selectFieldOptionLabels(scopeModeSelect)).toEqual(["全行", "条件で制限"]);
  await chooseSelectFieldOption(scopeModeSelect, "FILTERS");
  const filterRow = entitlementForm.getByTestId("security-deepsec-scope-filter-0-0");
  expect(await selectFieldOptionLabels(filterRow.locator("#deepsec-scope-filter-column-0-0"))).toContain(
    "REGION_CODE · VARCHAR2(32)"
  );
  await chooseSelectFieldOption(filterRow.locator("#deepsec-scope-filter-column-0-0"), "REGION_CODE");
  const valueSourceSelect = filterRow.locator("#deepsec-scope-filter-value-source-0-0");
  expect(await selectFieldOptionLabels(valueSourceSelect)).toContain("ログインユーザーID");
  await chooseSelectFieldOption(valueSourceSelect, "LOGIN_USER_ID");
  await expect(
    filterRow.getByTestId("security-deepsec-scope-filter-login-user-id-0-0")
  ).toBeVisible();
  await chooseSelectFieldOption(valueSourceSelect, "LITERAL");
  await expect
    .poll(async () => {
      const columnSelect = filterRow.locator("#deepsec-scope-filter-column-0-0");
      const operatorSelect = filterRow.locator("#deepsec-scope-filter-operator-0-0");
      const sourceSelect = filterRow.locator("#deepsec-scope-filter-value-source-0-0");
      const valueInput = filterRow.locator("#deepsec-scope-filter-value-0-0");
      const removeButton = filterRow.getByRole("button", { name: "条件を削除" });
      const [rowBox, columnBox, operatorBox, sourceBox, valueBox, removeBox] = await Promise.all([
        filterRow.boundingBox(),
        columnSelect.boundingBox(),
        operatorSelect.boundingBox(),
        sourceSelect.boundingBox(),
        valueInput.boundingBox(),
        removeButton.boundingBox(),
      ]);
      const separated = (
        left: NonNullable<typeof columnBox> | null,
        right: NonNullable<typeof columnBox> | null
      ) =>
        Boolean(
          left &&
            right &&
            (left.x + left.width <= right.x + 1 ||
              right.x + right.width <= left.x + 1 ||
              left.y + left.height <= right.y + 1 ||
              right.y + right.height <= left.y + 1)
        );

      return {
        columnInsideRow: Boolean(
          rowBox &&
            columnBox &&
            columnBox.x >= rowBox.x - 1 &&
            columnBox.x + columnBox.width <= rowBox.x + rowBox.width + 1
        ),
        // 列の選択欄は 2 列の段組みで行の半分程度の幅を保つ（16rem 以上、狭い画面では行幅）。旧基準の 240px は、
        // スクロールバーを隠す無頭ブラウザーでだけ満たしていた。本文の scrollbar-gutter: stable（#867）で
        // 1280px の欄は 236px になる（通常の scrollbar のブラウザーで本文が縦にスクロールするときは以前から同じ幅）。
        columnReadableWidth: Boolean(
          rowBox && columnBox && columnBox.width >= Math.min(224, rowBox.width - 24)
        ),
        columnOperatorSeparated: separated(columnBox, operatorBox),
        operatorSourceSeparated: separated(operatorBox, sourceBox),
        sourceValueSeparated: separated(sourceBox, valueBox),
        valueDeleteSeparated: separated(valueBox, removeBox),
        removeInsideRow: Boolean(
          rowBox &&
            removeBox &&
            removeBox.x >= rowBox.x - 1 &&
            removeBox.x + removeBox.width <= rowBox.x + rowBox.width + 1
        ),
      };
    })
    .toEqual({
      columnInsideRow: true,
      columnReadableWidth: true,
      columnOperatorSeparated: true,
      operatorSourceSeparated: true,
      sourceValueSeparated: true,
      valueDeleteSeparated: true,
      removeInsideRow: true,
    });
  await chooseSelectFieldOption(filterRow.locator("#deepsec-scope-filter-operator-0-0"), "IN");
  await filterRow.locator("#deepsec-scope-filter-values-0-0").fill("SALES, HR");
  await entitlementForm.getByRole("button", { name: "条件を追加", exact: true }).click();
  const numberFilterRow = entitlementForm.getByTestId("security-deepsec-scope-filter-0-1");
  expect(await selectFieldOptionLabels(numberFilterRow.locator("#deepsec-scope-filter-column-0-1"))).toContain(
    "ORDER_ID · NUMBER"
  );
  await chooseSelectFieldOption(numberFilterRow.locator("#deepsec-scope-filter-column-0-1"), "ORDER_ID");
  const numberValueSourceSelect = numberFilterRow.locator(
    "#deepsec-scope-filter-value-source-0-1"
  );
  expect(await selectFieldOptionLabels(numberValueSourceSelect)).toContain("ログインユーザーID");
  await expect(numberFilterRow.locator("#deepsec-scope-filter-value-0-1")).toHaveAttribute(
    "inputmode",
    "numeric"
  );
  await chooseSelectFieldOption(numberValueSourceSelect, "LOGIN_USER_ID");
  await expect(
    numberFilterRow.getByTestId("security-deepsec-scope-filter-login-user-id-0-1")
  ).toBeVisible();
  if (testInfo.project.name === "desktop") {
    await page.setViewportSize({ width: 1088, height: 959 });
    await expectNoPageHorizontalScroll(page);
    await expectNoElementHorizontalOverflow(entitlementForm);
    await expectNoElementHorizontalOverflow(filterRow);
    await expectNoElementHorizontalOverflow(numberFilterRow);
    await expectScopeFilterControlsInsideRow(filterRow);
    await expectScopeFilterControlsInsideRow(numberFilterRow);
  }
  await entitlementForm.getByRole("checkbox", { name: /CUSTOMER_NAME/ }).check();
  await entitlementForm.getByRole("checkbox", { name: /REGION_CODE/ }).check();
  await entitlementForm.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
  const sqlPreview = entitlementForm.getByTestId("security-deepsec-sql-preview");
  const sqlPreviewButton = entitlementForm.getByTestId("security-deepsec-sql-preview-generate");
  await expect(sqlPreviewButton).toBeVisible();
  await expect
    .poll(async () => {
      const [previewBox, buttonBox] = await Promise.all([
        sqlPreview.boundingBox(),
        sqlPreviewButton.boundingBox(),
      ]);

      return Boolean(
        previewBox &&
          buttonBox &&
          buttonBox.x >= previewBox.x - 1 &&
          buttonBox.x + buttonBox.width <= previewBox.x + previewBox.width + 1
      );
    })
    .toBeTruthy();
  await expectNoPageHorizontalScroll(page);
  await sqlPreviewButton.click();

  await expect.poll(() => previewPayload).toEqual({
    version: 3,
    data_entitlements: [
      {
        resource_code: "SALES.ORDERS",
        scope_code: "EXPRESSION",
        capability: "SELECT",
        target_owner: "SALES",
        target_object: "ORDERS",
        target_type: "TABLE",
        column_names: ["CUSTOMER_NAME", "REGION_CODE"],
        scope_mode: "EXPRESSION",
        scope_column: "",
        scope_filters: [],
        scope_expression: { version: 1, root: { kind: "group", operator: "AND", children: expectedScopeFilters.map((filter) => ({ kind: "condition", filter })) } },
      },
    ],
  });
  expect(applyPayloads).toHaveLength(0);
  await expect(page.getByText("SQL プレビューを生成しました。", { exact: true })).toBeVisible();
  await expect(entitlementForm.getByText("GRANT SELECT ON SALES.ORDERS TO NL2SQL_APP_DB_ROLE")).toBeVisible();
  await expect(entitlementForm.getByRole("button", { name: "ポリシーを保存" })).toHaveCount(0);
  const applyField = entitlementForm.getByTestId("execution-confirmation-field");
  await applyField.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN");
  await expect(applyField.getByRole("button", { name: "Data Grant を適用" })).toBeDisabled();
  await applyField.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await expect(applyField.getByRole("button", { name: "Data Grant を適用" })).toBeEnabled();
  await applyField.getByRole("button", { name: "Data Grant を適用" }).click();
  await expect.poll(() => applyPayloads.at(-1)).toEqual({
    version: 3,
    confirmation: "ADMIN_EXECUTE",
    data_entitlements: [
      {
        entitlement_id: "preview-0",
        resource_code: "SALES.ORDERS",
        scope_code: "EXPRESSION",
        capability: "SELECT",
        target_owner: "SALES",
        target_object: "ORDERS",
        target_type: "TABLE",
        column_names: ["CUSTOMER_NAME", "REGION_CODE"],
        scope_mode: "EXPRESSION",
        scope_column: "",
        scope_filters: [],
        scope_expression: { version: 1, root: { kind: "group", operator: "AND", children: expectedScopeFilters.map((filter) => ({ kind: "condition", filter })) } },
      },
    ],
  });
  await expect(page.getByText("Data Grant を適用しました。", { exact: true }).last()).toBeVisible();

  const removeEntitlementButton = entitlementForm.getByRole("button", { name: "データ権限を削除" });
  await expect(removeEntitlementButton).toContainText("削除");
  await removeEntitlementButton.click();
  await expect(entitlementForm.getByText("Data Grant 1", { exact: true })).toHaveCount(0);
  await entitlementForm.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
  await expect(sqlPreviewButton).toBeEnabled();
  await sqlPreviewButton.click();
  await expect.poll(() => previewPayload).toEqual({ version: 4, data_entitlements: [] });
  await expect(
    entitlementForm.getByText("DROP DATA GRANT IF EXISTS APP_OWNER.NL2SQL_DG_PREVIEW")
  ).toBeVisible();
  await applyField.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await expect(applyField.getByRole("button", { name: "Data Grant を適用" })).toBeEnabled();
  await applyField.getByRole("button", { name: "Data Grant を適用" }).click();
  await expect.poll(() => applyPayloads.at(-1)).toEqual({
    version: 4,
    confirmation: "ADMIN_EXECUTE",
    data_entitlements: [],
  });
  await expect(page.getByText("Data Grant を適用しました。", { exact: true }).last()).toBeVisible();

  await entitlementForm.getByRole("button", { name: "データ権限を追加" }).click();
  const replacementRule = entitlementForm.getByTestId("security-deepsec-entitlement-rule-0");
  const replacementPicker = replacementRule.getByTestId("security-deepsec-object-picker-0");
  await expect(replacementRule.getByText("Data Grant", { exact: true })).toBeVisible();
  // 選択欄を閉じると検索語は消え、候補は 1 ページ目に戻る。検索して選ぶ。
  await replacementPicker.locator("#deepsec-entitlement-resource-0").click();
  const replacementPopover = page.getByRole("dialog", { name: "対象 table/view" });
  await replacementPopover.getByRole("combobox").fill("ORDERS");
  await replacementPopover.getByRole("option", { name: /SALES\.ORDERS/ }).click();
  await replacementRule.getByRole("checkbox", { name: /ORDER_ID/ }).check();
  await applyField.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  failNextApply = true;
  await applyField.getByRole("button", { name: "Data Grant を適用" }).click();
  await expect(
    entitlementForm.getByText("Oracle で Data Grant の適用に失敗しました。", { exact: true })
  ).toBeVisible();
  await expect(replacementRule.getByRole("checkbox", { name: /ORDER_ID/ })).toBeChecked();
  await expect(applyField.getByRole("textbox", { name: "実行確認語" })).toHaveValue(
    "ADMIN_EXECUTE"
  );
  await applyField.getByRole("button", { name: "Data Grant を適用" }).click();
  await expect.poll(() => applyPayloads.at(-1)).toEqual({
    version: 5,
    confirmation: "ADMIN_EXECUTE",
    data_entitlements: [
      {
        resource_code: "SALES.ORDERS",
        scope_code: "*",
        capability: "SELECT",
        target_owner: "SALES",
        target_object: "ORDERS",
        target_type: "TABLE",
        column_names: ["ORDER_ID"],
        scope_mode: "ALL",
        scope_column: "",
        scope_filters: [],
      },
    ],
  });
  await expect(page.getByText("Data Grant を適用しました。", { exact: true }).last()).toBeVisible();

  await page.getByTestId("security-deepsec-entitlement-role-role-archived").click();
  await expect(
    entitlementForm.getByText("アーカイブ済みロールの構造化データ権限は変更できません。", {
      exact: true,
    })
  ).toBeVisible();
  await expect(entitlementForm.getByRole("button", { name: "Data Grant を適用" })).toBeDisabled();

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec Data Grant は3件追加しても下部操作と選択編集を維持する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  if (testInfo.project.name === "desktop") {
    await page.setViewportSize({ width: 1600, height: 948 });
  }
  await mockDatabaseGateReady(page);
  const dataRole = {
    role_id: "role-data",
    role_code: "DATA_USER",
    display_name: "データユーザー",
    description: "構造化データ参照",
    is_built_in: false,
    archived: false,
    version: 1,
    data_entitlements: [
      {
        entitlement_id: "entitlement-existing-1",
        data_grant_name: "NL2SQL_DG_EMPLOYEES_READ",
        resource_code: "HR.EMPLOYEES",
        scope_code: "*",
        capability: "SELECT",
        target_owner: "HR",
        target_object: "EMPLOYEES",
        target_type: "TABLE",
        column_names: ["EMPLOYEE_ID"],
        scope_mode: "ALL",
        scope_column: "",
        scope_filters: [],
        apply_status: "PENDING",
        apply_error_message: "",
        applied_at: null,
        sql: [],
        checksum: "a".repeat(64),
      },
      {
        entitlement_id: "entitlement-existing-2",
        data_grant_name: "NL2SQL_DG_EMPLOYEES_PROFILE",
        resource_code: "HR.EMPLOYEES",
        scope_code: "*",
        capability: "SELECT",
        target_owner: "HR",
        target_object: "EMPLOYEES",
        target_type: "TABLE",
        column_names: ["DISPLAY_NAME"],
        scope_mode: "ALL",
        scope_column: "",
        scope_filters: [],
        apply_status: "PENDING",
        apply_error_message: "",
        applied_at: null,
        sql: [],
        checksum: "b".repeat(64),
      },
    ],
  };
  const auditRole = {
    ...dataRole,
    role_id: "role-audit",
    role_code: "AUDIT_USER",
    display_name: "監査ユーザー",
    data_entitlements: [
      {
        entitlement_id: "entitlement-audit-1",
        data_grant_name: "NL2SQL_DG_AUDIT_EMPLOYEES",
        resource_code: "HR.EMPLOYEES",
        scope_code: "*",
        capability: "SELECT",
        target_owner: "HR",
        target_object: "EMPLOYEES",
        target_type: "TABLE",
        column_names: ["EMPLOYEE_ID", "DISPLAY_NAME"],
        scope_mode: "ALL",
        scope_column: "",
        scope_filters: [],
        apply_status: "PENDING",
        apply_error_message: "",
        applied_at: null,
        sql: [],
        checksum: "c".repeat(64),
      },
    ],
  };
  let roleWidePreviewPayload: {
    version: number;
    data_entitlements: Array<Record<string, unknown>>;
  } | null = null;
  await page.route("**/api/security/deepsec/target-objects?*", (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    fulfill(route, {
      runtime: "oracle",
      owner: "",
      items: [deepSecTargetObject],
      total: null,
      counts_included: false,
      next_cursor: cursor ? null : "deepsec-page-2",
      warnings: [],
    });
  });
  await page.route("**/api/security/deepsec/target-objects/HR/EMPLOYEES**", (route) =>
    fulfill(route, deepSecTargetObjectDetail)
  );
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: { data_grants: 0 },
      message: "構成済みです。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan(true)));
  await page.route("**/api/security/deepsec/data-entitlements", (route) =>
    fulfill(route, [dataRole, auditRole])
  );
  await page.route("**/api/security/deepsec/data-entitlements/role-data/preview", async (route) => {
    roleWidePreviewPayload = route.request().postDataJSON() as {
      version: number;
      data_entitlements: Array<Record<string, unknown>>;
    };
    await fulfill(route, {
      role_id: dataRole.role_id,
      version: dataRole.version,
      data_entitlements: dataRole.data_entitlements.map((item) => ({
        ...item,
        sql: [
          `-- ${item.data_grant_name}`,
          `GRANT SELECT ON ${item.resource_code} TO NL2SQL_APP_DB_ROLE`,
          `DROP DATA GRANT IF EXISTS APP_OWNER.${item.data_grant_name}`,
          `SET USE DATA GRANTS ONLY ON ${item.resource_code} ENABLED`,
        ],
        checksum: item.checksum,
      })),
      cleanup_sql: [],
      checksum: "d".repeat(64),
    });
  });

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限" }).click();
  const entitlementForm = page.getByTestId("security-deepsec-entitlement-form");
  const addGrantButton = entitlementForm.getByRole("button", { name: "データ権限を追加" });
  const dataRoleCard = page.getByTestId("security-deepsec-entitlement-role-role-data");
  const auditRoleCard = page.getByTestId("security-deepsec-entitlement-role-role-audit");

  await expect(dataRoleCard).toBeVisible();
  await expect(auditRoleCard).toBeVisible();
  await setMainScrollTop(page, 0);
  await expectMainScrollPreservedAfterClick(page, dataRoleCard);
  await expect(dataRoleCard).toHaveAttribute("aria-pressed", "true");
  await expectElementScrollTop(entitlementForm.getByTestId("security-deepsec-entitlement-rule-0"), 0);
  await setElementScrollTop(entitlementForm.getByTestId("security-deepsec-entitlement-rule-0"), 96);
  await setMainScrollTop(page, 0);
  await expectMainScrollPreservedAfterClick(page, auditRoleCard);
  await expect(auditRoleCard).toHaveAttribute("aria-pressed", "true");
  await expectElementScrollTop(entitlementForm.getByTestId("security-deepsec-entitlement-rule-0"), 0);
  await setMainScrollTop(page, 0);
  await expectMainScrollPreservedAfterClick(page, dataRoleCard);
  await expect(dataRoleCard).toHaveAttribute("aria-pressed", "true");
  await expect(entitlementForm.getByTestId("security-deepsec-entitlement-rule-tab-0")).toBeVisible();
  await expect(entitlementForm.getByTestId("security-deepsec-entitlement-rule-tab-1")).toBeVisible();
  const secondSavedRuleTab = entitlementForm.getByTestId("security-deepsec-entitlement-rule-tab-1");
  await secondSavedRuleTab.click();
  await expect(secondSavedRuleTab).toHaveAttribute("aria-pressed", "true");
  const workspaceFrame = entitlementForm.getByTestId(
    "security-deepsec-entitlement-workspace-frame"
  );
  const sqlPreview = entitlementForm.getByTestId("security-deepsec-sql-preview");
  const workspaceHeightBeforePreview = Math.round(
    await workspaceFrame.evaluate((node) => node.getBoundingClientRect().height)
  );
  await sqlPreview.locator("summary").focus();
  await sqlPreview.locator("summary").press("Enter");
  await expect(sqlPreview).toHaveAttribute("open", "");
  await expect
    .poll(() =>
      workspaceFrame.evaluate((node) => Math.round(node.getBoundingClientRect().height))
    )
    .toBe(workspaceHeightBeforePreview);
  await entitlementForm.getByTestId("security-deepsec-sql-preview-generate").click();
  await expect.poll(() => roleWidePreviewPayload).toMatchObject({
    version: 1,
    data_entitlements: [
      { entitlement_id: "entitlement-existing-1" },
      { entitlement_id: "entitlement-existing-2" },
    ],
  });
  await expect(entitlementForm.getByText("-- NL2SQL_DG_EMPLOYEES_READ")).toBeVisible();
  await expect(entitlementForm.getByText("-- NL2SQL_DG_EMPLOYEES_PROFILE")).toBeVisible();
  await expect
    .poll(() =>
      workspaceFrame.evaluate((node) => Math.round(node.getBoundingClientRect().height))
    )
    .toBe(workspaceHeightBeforePreview);
  await expect
    .poll(() =>
      sqlPreview.evaluate((node) => ({
        scrolls: node.scrollHeight > node.clientHeight + 1,
        overflowY: window.getComputedStyle(node).overflowY,
      }))
    )
    .toEqual({ scrolls: true, overflowY: "auto" });
  await entitlementForm.getByTestId("security-deepsec-entitlement-rule-tab-0").click();
  await setElementScrollTop(entitlementForm.getByTestId("security-deepsec-entitlement-rule-0"), 96);
  await setMainScrollTop(page, 0);
  await expectMainScrollPreservedAfterClick(page, addGrantButton);

  const rulesList = entitlementForm.getByTestId("security-deepsec-entitlement-rules-list");
  const firstRuleTab = entitlementForm.getByTestId("security-deepsec-entitlement-rule-tab-0");
  const secondRuleTab = entitlementForm.getByTestId("security-deepsec-entitlement-rule-tab-1");
  const thirdRuleTab = entitlementForm.getByTestId("security-deepsec-entitlement-rule-tab-2");
  const actionRegion = entitlementForm.getByTestId("security-deepsec-entitlement-action-region");

  await expect(rulesList).toBeVisible();
  await expect(firstRuleTab).toHaveAttribute("aria-pressed", "false");
  await expect(secondRuleTab).toHaveAttribute("aria-pressed", "false");
  await expect(thirdRuleTab).toHaveAttribute("aria-pressed", "true");
  await expectElementScrollTop(entitlementForm.getByTestId("security-deepsec-entitlement-rule-2"), 0);
  if (testInfo.project.name === "desktop") {
    await expect
      .poll(() =>
        entitlementForm.evaluate((node) => {
          const workspace = node.querySelector(
            '[data-testid="security-deepsec-entitlement-workspace"]'
          );
          const editor = node.querySelector('[data-testid="security-deepsec-entitlement-rule-2"]');
          const workspaceBox = workspace?.getBoundingClientRect();
          const editorBox = editor?.getBoundingClientRect();
          return {
            workspaceHeightIsExpected: Boolean(
              workspaceBox && workspaceBox.height >= 447 && workspaceBox.height <= 449
            ),
            editorMatchesWorkspace: Boolean(
              workspaceBox && editorBox && Math.abs(workspaceBox.height - editorBox.height) <= 1
            ),
            // 編集欄は固定の高さで、溢れたら中をスクロールする。対象の選択は 1 行の選択欄（#608）になり、
            // 対象未選択の新しいルールは溢れないことがあるので、スクロールできる設定であることだけを確かめる。
            editorScrollable: Boolean(editor && window.getComputedStyle(editor).overflowY === "auto"),
          };
        })
      )
      .toEqual({
        workspaceHeightIsExpected: true,
        editorMatchesWorkspace: true,
        editorScrollable: true,
      });
  }
  await expect(entitlementForm.getByTestId("security-deepsec-entitlement-rule-2").getByTestId("security-deepsec-object-picker-2")).toBeVisible();
  await expectDeepSecDataGrantRegionsDoNotOverlap(page, "security-deepsec-entitlement-rule-2");
  const thirdObjectPicker = entitlementForm
    .getByTestId("security-deepsec-entitlement-rule-2")
    .getByTestId("security-deepsec-object-picker-2");
  await expectElementCenterUnobscured(thirdObjectPicker.locator("#deepsec-entitlement-resource-2"));
  await expectElementCenterUnobscured(entitlementForm.getByRole("button", { name: "データ権限を削除" }));
  // 開いた選択欄の候補は、ルールの編集欄のスクロール領域に隠れない（画面の最前面に重ねる）。
  await thirdObjectPicker.locator("#deepsec-entitlement-resource-2").click();
  await expectElementCenterUnobscured(
    page.getByRole("dialog", { name: "対象 table/view" }).getByRole("option", { name: /HR\.EMPLOYEES/u })
  );
  await page.getByRole("dialog", { name: "対象 table/view" }).getByRole("combobox").press("Escape");
  await setElementScrollTop(entitlementForm.getByTestId("security-deepsec-entitlement-rule-2"), 96);
  await setMainScrollTop(page, 0);
  await expectMainScrollPreservedAfterClick(page, firstRuleTab);
  await expect(firstRuleTab).toHaveAttribute("aria-pressed", "true");
  await expect(thirdRuleTab).toHaveAttribute("aria-pressed", "false");
  await expectElementScrollTop(entitlementForm.getByTestId("security-deepsec-entitlement-rule-0"), 0);
  await expect(entitlementForm.getByTestId("security-deepsec-entitlement-rule-0").getByTestId("security-deepsec-object-picker-0")).toBeVisible();
  await expect(entitlementForm.getByTestId("security-deepsec-entitlement-rule-2")).toHaveCount(0);
  await expectDeepSecDataGrantRegionsDoNotOverlap(page, "security-deepsec-entitlement-rule-0");

  await expect(actionRegion.getByTestId("security-deepsec-sql-preview")).toBeVisible();
  await expect(actionRegion.getByTestId("execution-confirmation-field")).toBeVisible();
  const applyField = actionRegion.getByTestId("execution-confirmation-field");
  const applyButton = actionRegion.getByRole("button", { name: "Data Grant を適用" });
  await expect(applyButton).toBeVisible();
  await applyField.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  // 未完成の3件目が残る間は、確認語を入力しても適用できない。
  await expect(applyButton).toBeDisabled();
  await expectElementCenterUnobscured(applyButton);
  await expect
    .poll(() =>
      entitlementForm.evaluate((node) => {
        const list = node.querySelector('[data-testid="security-deepsec-entitlement-rules-list"]');
        const actions = node.querySelector('[data-testid="security-deepsec-entitlement-action-region"]');
        const formStyle = window.getComputedStyle(node);
        const listStyle = list ? window.getComputedStyle(list) : null;
        const formBox = node.getBoundingClientRect();
        const actionBox = actions?.getBoundingClientRect();
        return {
          formOverflow: formStyle.overflow,
          listOverflowY: listStyle?.overflowY,
          actionsInsideForm: Boolean(
            actionBox &&
              actionBox.y >= formBox.y - 1 &&
              actionBox.y + actionBox.height <= formBox.y + formBox.height + 1
          ),
        };
      })
    )
    .toEqual({
      formOverflow: "visible",
      listOverflowY: "auto",
      actionsInsideForm: true,
    });
  await expectNoElementHorizontalOverflow(entitlementForm);
  await expectNoPageHorizontalScroll(page);

  if (testInfo.project.name === "desktop") {
    await page.setViewportSize({ width: 1440, height: 900 });
    await expect
      .poll(async () =>
        Math.round(await workspaceFrame.evaluate((node) => node.getBoundingClientRect().height))
      )
      .toBe(448);
    await expectNoPageHorizontalScroll(page);
    await expectNoElementHorizontalOverflow(entitlementForm);
    await expectDeepSecDataGrantRegionsDoNotOverlap(page, "security-deepsec-entitlement-rule-0");
    await expectElementCenterUnobscured(applyButton);
  }

  await page.setViewportSize({ width: 375, height: 812 });
  await expect
    .poll(async () =>
      Math.round(await workspaceFrame.evaluate((node) => node.getBoundingClientRect().height))
    )
    .toBe(504);
  await expectNoPageHorizontalScroll(page);
  await expectNoElementHorizontalOverflow(entitlementForm);
  await expectDeepSecDataGrantRegionsDoNotOverlap(page, "security-deepsec-entitlement-rule-0");
  await expectElementCenterUnobscured(applyButton);
});

test("DeepSec は基盤構成からDB構成を確認語つきで解除する", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  let applied = true;
  let statusRequests = 0;
  let planRequests = 0;
  let entitlementRequests = 0;
  let resetRequests = 0;
  let resetPayload: unknown = null;
  await page.route("**/api/security/deepsec/status", async (route) => {
    statusRequests += 1;
    await fulfill(route, {
      configured: applied,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: applied ? { managed_data_grants: 1 } : {},
      message: applied ? "Deep Data Security の基盤構成は適用済みです。" : "未適用です。",
    });
  });
  await page.route("**/api/security/deepsec/plan", async (route) => {
    planRequests += 1;
    await fulfill(route, deepSecPlan(applied));
  });
  await mockDeepSecTargetObjects(page);
  await page.route("**/api/security/deepsec/plan/V001/reset", async (route) => {
    resetRequests += 1;
    resetPayload = route.request().postDataJSON();
    applied = false;
    await fulfill(route, { version: "V001", status: "RESET", step_numbers: [1, 2, 3, 4] });
  });
  await page.route("**/api/security/deepsec/data-entitlements", async (route) => {
    entitlementRequests += 1;
    await fulfill(route, [systemRole]);
  });

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "基盤構成" }).click();

  const foundationPanel = page.locator("#security-deepsec-panel-foundation");
  await expect(foundationPanel.getByText("適用済み", { exact: true }).first()).toBeVisible();
  const foundationHeader = foundationPanel.locator(":scope > div").first();
  await expect(foundationHeader.getByRole("button", { name: "DeepSec 構成を解除" })).toHaveCount(0);

  const planTitle = foundationPanel.getByRole("heading", { name: "実行計画" });
  const resetSection = foundationPanel.getByTestId("security-deepsec-reset-section");
  await expect(resetSection).toBeVisible();
  await expect(resetSection).toHaveText(/構成解除/u);
  await expect(
    resetSection.getByTestId("execution-confirmation-field")
  ).toBeHidden();
  const planBox = await planTitle.boundingBox();
  const resetBox = await resetSection.boundingBox();
  expect(planBox).not.toBeNull();
  expect(resetBox).not.toBeNull();
  expect(resetBox!.y).toBeGreaterThan(planBox!.y);

  const resetSummary = resetSection.locator("summary");
  await resetSummary.click();
  expect(await resetSummary.evaluate((node) => node.matches(":focus-visible"))).toBe(false);
  await expect(
    resetSection.getByText("Data Grants、Data Grants Only、コンテキスト", { exact: false })
  ).toBeVisible();

  const resetConfirmationField = resetSection
    .getByTestId("execution-confirmation-field")
    .filter({ hasText: "ADMIN_RESET" });
  const resetInput = resetConfirmationField.getByRole("textbox", { name: "実行確認語" });
  const resetButton = resetConfirmationField.getByRole("button", { name: "DeepSec 構成を解除" });
  await expect(resetButton).toBeDisabled();
  await resetInput.fill("ADMIN");
  await expect(resetConfirmationField.getByText("不一致")).toBeVisible();
  await expect(resetButton).toBeDisabled();
  expect(resetRequests).toBe(0);

  await resetInput.fill("ADMIN_RESET");
  await expect(resetConfirmationField.getByText("確認済み")).toBeVisible();
  await expect(resetButton).toBeEnabled();
  await resetButton.click();

  await expect(page.getByText("DeepSec 構成を解除しました。", { exact: true })).toBeVisible();
  await expect(foundationPanel.getByText("未適用", { exact: true }).first()).toBeVisible();
  await expect(foundationPanel.getByTestId("security-deepsec-reset-section")).toHaveCount(0);
  expect(resetRequests).toBe(1);
  expect(resetPayload).toEqual({ confirmation: "ADMIN_RESET" });
  expect(statusRequests).toBeGreaterThanOrEqual(2);
  expect(planRequests).toBeGreaterThanOrEqual(2);
  expect(entitlementRequests).toBeGreaterThanOrEqual(2);
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec は版管理 SQL を読み取り専用で順次適用し、検証結果を表示する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  let applied = false;
  const applyRequests: number[] = [];
  const applyPayloads: Record<number, unknown> = {};
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: applied,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: applied ? { managed_data_grants: 1 } : {},
      message: applied ? "Deep Data Security の基盤構成は適用済みです。" : "未適用です。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) =>
    fulfill(route, deepSecPlan(applied))
  );
  await page.route("**/api/security/deepsec/plan/V001/steps/1/apply", async (route) => {
    applyRequests.push(1);
    applyPayloads[1] = route.request().postDataJSON();
    await fulfill(route, { version: "V001", step_no: 1, status: "APPLIED" });
  });
  await page.route("**/api/security/deepsec/plan/V001/steps/2/apply", async (route) => {
    applyRequests.push(2);
    applyPayloads[2] = route.request().postDataJSON();
    applied = true;
    await fulfill(route, { version: "V001", step_no: 2, status: "APPLIED" });
  });
  await page.route("**/api/security/deepsec/verify", (route) =>
    fulfill(route, {
      version: "V001",
      passed: true,
      checked_at: "2026-07-19T00:00:00Z",
      checks: [
        { key: "no_context", passed: true, detail: "context 未設定の取得行数: 0" },
        { key: "limited_subject", passed: true, detail: "sensitive_masked=true" },
        { key: "foundation", passed: true, detail: "Deep Data Security の基盤構成は適用済みです。" },
        {
          key: "predicate_table_grants",
          passed: true,
          detail: "Data Grant predicate 用 app table SELECT grants are applied.",
        },
        {
          key: "vpd_policy:department",
          passed: true,
          detail: "ADMIN.DEPARTMENT: enabled VPD/RLS policies are not present.",
        },
        {
          key: "data_grant:department",
          passed: true,
          detail: "NL2SQL_DG_DEPARTMENT -> ADMIN.DEPARTMENT: rows=4, data_role_rows=4",
        },
        {
          key: "data_grant:employee",
          passed: true,
          detail: "NL2SQL_DG_EMPLOYEE -> ADMIN.EMPLOYEE: rows=6, data_role_rows=6",
        },
      ],
    })
  );
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "基盤構成" }).click();
  await expect(page.locator("pre:visible")).toHaveCount(0);
  await expect(page.locator("textarea")).toHaveCount(0);
  await page.getByText("SQL とチェックサムを表示", { exact: true }).first().click();
  // SQL は 1 文ごとに <pre>(aria-label 付き)で描画される。開いたのは step 1 だけであること。
  const step1SqlCount = deepSecPlan().steps[0].sql.length;
  await expect(page.locator("pre:visible")).toHaveCount(step1SqlCount);
  await expect(page.getByTestId("security-deepsec-step-1").locator("pre:visible")).toHaveCount(step1SqlCount);
  await expect(page.getByTestId("security-deepsec-step-2").locator("pre:visible")).toHaveCount(0);
  await expect(page.getByTestId("security-deepsec-step-1").getByTestId("execution-confirmation-field")).toHaveCount(0);
  await expect(page.getByTestId("security-deepsec-step-2").getByTestId("execution-confirmation-field")).toHaveCount(0);
  const applySection = page.getByTestId("security-deepsec-foundation-apply-section");
  await expect(applySection).toBeVisible();
  const confirmationField = applySection.getByTestId("execution-confirmation-field");
  const confirmationInput = confirmationField.getByRole("textbox", { name: "実行確認語" });
  const applyButton = confirmationField.getByRole("button", { name: "基盤構成を適用" });
  await expect(applyButton).toBeDisabled();
  await confirmationInput.fill("ADMIN");
  await expect(confirmationField.getByText("不一致")).toBeVisible();
  await expect(applyButton).toBeDisabled();
  expect(applyRequests).toEqual([]);
  await confirmationInput.fill("ADMIN_EXECUTE");
  await expect(confirmationField.getByText("確認済み")).toBeVisible();
  await expect(applyButton).toBeEnabled();
  await applyButton.click();
  const appliedStep = page.getByTestId("security-deepsec-step-1");
  await expect(appliedStep.getByText("適用済み", { exact: true })).toBeVisible();
  await expect(appliedStep.getByTestId("execution-confirmation-field")).toHaveCount(0);
  await expect(appliedStep.getByText("適用日時", { exact: true })).toBeVisible();
  await expect(appliedStep.locator("time")).toHaveAttribute("datetime", "2026-07-19T00:00:00Z");
  await expect(appliedStep.locator("time")).toHaveText(/^2026\/07\/19 \d{2}:\d{2}$/);
  await expect(page.getByTestId("security-deepsec-foundation-apply-section")).toHaveCount(0);
  expect(applyRequests).toEqual([1, 2]);
  expect(applyPayloads[1]).toEqual({
    checksum: "a".repeat(64),
    confirmation: "ADMIN_EXECUTE",
  });
  expect(applyPayloads[2]).toEqual({
    checksum: "b".repeat(64),
    confirmation: "ADMIN_EXECUTE",
  });

  await page.getByRole("tab", { name: "データ権限" }).click();
  const dataPermissionsPanel = page.locator("#security-deepsec-panel-data-permissions");
  const dataPermissionsHeader = dataPermissionsPanel.locator("xpath=./div[1]");
  const verificationCard = page.getByTestId("security-deepsec-verification-card");
  await expect(dataPermissionsHeader.getByRole("button", { name: "Data Grant を検証" })).toHaveCount(0);
  await expect(verificationCard.getByText("検証を実行すると", { exact: false })).toBeVisible();
  const verifyButton = verificationCard.getByRole("button", { name: "Data Grant を検証" });
  await expect(verifyButton).toBeEnabled();
  await expectNoElementHorizontalOverflow(verifyButton);
  await verifyButton.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "実行" }).click();
  await expect(verificationCard.getByText("no_context", { exact: true })).toBeVisible();
  await expect(verificationCard.getByText("sensitive_masked=true", { exact: true })).toBeVisible();
  await expect(verificationCard.getByText(/2026\/07\/19/u)).toHaveCount(0);
  const verificationResults = verificationCard.getByTestId(
    "security-deepsec-verification-results"
  );
  await expect(verificationResults).toHaveAttribute("role", "region");
  await expect(verificationResults).toHaveAttribute(
    "aria-label",
    "Data Grant 検証結果。必要に応じて縦方向にスクロールできます。"
  );
  await expect(verificationResults).toHaveAttribute("tabindex", "0");
  const verificationScrollState = await verificationResults.evaluate((node) => {
    const computed = window.getComputedStyle(node);
    return {
      clientHeight: node.clientHeight,
      maxHeight: Number.parseFloat(computed.maxHeight),
      overflowY: computed.overflowY,
      rootFontSize: Number.parseFloat(
        window.getComputedStyle(document.documentElement).fontSize
      ),
      scrollHeight: node.scrollHeight,
    };
  });
  expect(verificationScrollState.maxHeight).toBeCloseTo(
    23.25 * verificationScrollState.rootFontSize,
    0
  );
  expect(verificationScrollState.clientHeight).toBeLessThanOrEqual(
    Math.ceil(verificationScrollState.maxHeight)
  );
  expect(verificationScrollState.scrollHeight).toBeGreaterThan(
    verificationScrollState.clientHeight
  );
  expect(verificationScrollState.overflowY).toBe("auto");
  await verificationResults.focus();
  await expect(verificationResults).toBeFocused();
  await verificationResults.press("End");
  await expect
    .poll(() => verificationResults.evaluate((node) => node.scrollTop))
    .toBeGreaterThan(0);

  const hasHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth
  );
  expect(hasHorizontalOverflow).toBe(false);
});

test("DeepSec 基盤構成の一括適用は未適用 step だけを順番に実行する", async ({ page }) => {
  await mockDatabaseGateReady(page);
  let step2Applied = false;
  const applyRequests: number[] = [];
  const partialPlan = () => {
    const nextPlan = deepSecPlan(false);
    nextPlan.steps[0] = {
      ...nextPlan.steps[0],
      status: "APPLIED",
      executed_at: "2026-07-19T00:00:00Z",
    };
    if (step2Applied) {
      nextPlan.steps[1] = {
        ...nextPlan.steps[1],
        status: "APPLIED",
        executed_at: "2026-07-19T00:01:00Z",
      };
    }
    return nextPlan;
  };

  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: step2Applied,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: step2Applied ? { managed_data_grants: 1 } : {},
      message: step2Applied ? "Deep Data Security の基盤構成は適用済みです。" : "未適用です。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, partialPlan()));
  await page.route("**/api/security/deepsec/plan/V001/steps/1/apply", async (route) => {
    applyRequests.push(1);
    await fulfill(route, { version: "V001", step_no: 1, status: "APPLIED" });
  });
  await page.route("**/api/security/deepsec/plan/V001/steps/2/apply", async (route) => {
    applyRequests.push(2);
    step2Applied = true;
    await fulfill(route, { version: "V001", step_no: 2, status: "APPLIED" });
  });
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "基盤構成" }).click();

  const applySection = page.getByTestId("security-deepsec-foundation-apply-section");
  const confirmationField = applySection.getByTestId("execution-confirmation-field");
  await confirmationField.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await confirmationField.getByRole("button", { name: "基盤構成を適用" }).click();

  expect(applyRequests).toEqual([2]);
  await expect(page.getByTestId("security-deepsec-step-2").getByText("適用済み", { exact: true })).toBeVisible();
  await expect(page.getByTestId("security-deepsec-foundation-apply-section")).toHaveCount(0);
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec 基盤構成の一括適用は step 失敗時に後続を実行しない", async ({ page }) => {
  await mockDatabaseGateReady(page);
  let step2Requests = 0;

  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: false,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: {},
      message: "未適用です。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan(false)));
  await page.route("**/api/security/deepsec/plan/V001/steps/1/apply", (route) =>
    fulfill(route, "V001.1 の適用に失敗しました。", 500)
  );
  await page.route("**/api/security/deepsec/plan/V001/steps/2/apply", async (route) => {
    step2Requests += 1;
    await fulfill(route, { version: "V001", step_no: 2, status: "APPLIED" });
  });
  await mockDeepSecDataEntitlements(page);

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "基盤構成" }).click();

  const applySection = page.getByTestId("security-deepsec-foundation-apply-section");
  const confirmationField = applySection.getByTestId("execution-confirmation-field");
  await confirmationField.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await confirmationField.getByRole("button", { name: "基盤構成を適用" }).click();

  await expect(applySection.getByText("V001.1 の適用に失敗しました。", { exact: true })).toBeVisible();
  expect(step2Requests).toBe(0);
  await expect(applySection).toBeVisible();
  await expectNoPageHorizontalScroll(page);
});

async function mockReviewedUsers(page: Page, locked = false) {
  await mockDatabaseGateReady(page);
  const role = { ...systemRole, role_id: "review-role", role_code: "REVIEW", display_name: "レビュー", is_built_in: false };
  const user = { user_uuid: "review-user", login_user_id: "review.user", display_name: "レビュー利用者", status: "ACTIVE", force_password_change: false, locked_until: locked ? "2099-01-01T00:00:00Z" : null, version: 1, role_ids: [role.role_id], is_bootstrap_admin: false };
  await page.route("**/api/security/roles?include_archived=false", route => fulfill(route, [role]));
  await page.route("**/api/security/users", route => fulfill(route, [user]));
  return user;
}

test("ユーザー管理レビュー: リセット待機中は編集対象と入力を固定する", async ({ page }) => {
  const user = await mockReviewedUsers(page);
  let pending: Route | undefined;
  await page.route("**/api/security/users/review-user/reset-password", route => { pending = route; });
  await page.goto("/settings/security/users?user=review-user");
  await page.getByTestId("security-users-detail-actions").getByRole("button", { name: "編集", exact: true }).click();
  await page.getByRole("button", { name: "パスワードをリセット", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "実行", exact: true }).click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  await expect(page.getByRole("button", { name: "一覧へ戻る" })).toBeDisabled();
  await expect(page.getByLabel("表示名", { exact: false })).toBeDisabled();
  await fulfill(pending!, { user, temporary_password: "SyntheticOnly!123" });
  await expect(page.getByLabel("一時パスワード", { exact: true })).toHaveValue("SyntheticOnly!123");
  await expect(page.getByLabel("ログインユーザーID")).toHaveValue("review.user");
  await expect(page.getByRole("button", { name: "一覧へ戻る" })).toBeEnabled();
});

test("ユーザー管理レビュー: ロック解除の重複送信を抑止し失敗後に再試行する", async ({ page }) => {
  const user = await mockReviewedUsers(page, true);
  let pending: Route | undefined;
  let calls = 0;
  await page.route("**/api/security/users/review-user/unlock", route => { pending = route; calls += 1; });
  await page.goto("/settings/security/users?user=review-user");
  const actions = page.getByTestId("security-users-detail-actions");
  await actions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "ロック解除" }).click();
  await expect.poll(() => calls).toBe(1);
  await expect(actions.getByRole("button", { name: "編集", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "新規作成", exact: true })).toBeDisabled();
  await fulfill(pending!, "解除に失敗しました", 503);
  await expect(page.getByText("解除に失敗しました", { exact: true })).toBeVisible();
  await actions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "ロック解除" }).click();
  await expect.poll(() => calls).toBe(2);
  await fulfill(pending!, { ...user, locked_until: null });
  await expect(page.getByText("解除に失敗しました", { exact: true })).toHaveCount(0);
  await expect(actions.getByRole("button", { name: "編集", exact: true })).toBeEnabled();
});

test("ユーザー管理レビュー: 未保存入力は戻ると内部リンクで破棄を確認する", async ({ page }) => {
  await mockReviewedUsers(page);
  await page.goto("/settings/security/users");
  await page.getByRole("button", { name: "新規作成", exact: true }).click();
  await page.getByLabel("表示名", { exact: false }).fill("未保存の利用者");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(page.getByLabel("表示名", { exact: false })).toHaveValue("未保存の利用者");
  await page.evaluate(() => {
    const link = document.createElement("a");
    link.href = "/settings/security/roles";
    link.textContent = "レビュー用内部リンク";
    // AppShell の外枠は fixed inset-0 で視口を覆う（#855）。body の末尾のリンクは外枠の下に隠れて押せないため、
    // 利用者が押せる本文（main#pr-main）の中に置く。
    document.getElementById("pr-main")!.append(link);
  });
  await page.getByRole("link", { name: "レビュー用内部リンク" }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/security\/users/);
  const protectedReload = await page.evaluate(() => !window.dispatchEvent(new Event("beforeunload", { cancelable: true })));
  expect(protectedReload).toBe(true);
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "破棄して移動" }).click();
  await expect(page.getByTestId("security-users-grid")).toBeVisible();
});

async function mockReviewedRoles(page: Page, archived = false) {
  await mockDatabaseGateReady(page);
  const role = { ...systemRole, role_id: "review-role", role_code: "REVIEW", display_name: "レビュー役割", is_built_in: false, archived, permissions: ["menu.query", "menu.history"], allowed_profile_ids: [] };
  await page.route("**/api/security/roles?include_archived=true", route => fulfill(route, [role]));
  await page.route("**/api/security/permissions", route => fulfill(route, [
    { code: "menu.query", label: "SQL 生成", group: "AI 活用", description: "", implies: [] },
    { code: "menu.history", label: "実行履歴", group: "AI 活用", description: "", implies: [] },
  ]));
  return role;
}

for (const action of ["archive", "restore"] as const) {
  test(`ロール管理レビュー: ${action} 待機中の対象固定と失敗後の再試行`, async ({ page }) => {
    const role = await mockReviewedRoles(page, action === "restore");
    const label = action === "archive" ? "アーカイブ" : "復元";
    let pending: Route | undefined;
    let calls = 0;
    await page.route(`**/api/security/roles/review-role/${action}`, route => { pending = route; calls += 1; });
    await page.goto("/settings/security/roles?role=review-role");
    const actions = page.getByTestId("security-roles-detail-actions");
    const invoke = async () => {
      const direct = actions.getByRole("button", { name: label, exact: true });
      if (await direct.count()) await direct.click();
      else {
        await actions.getByRole("button", { name: "その他の操作" }).click();
        await page.getByRole("menuitem", { name: label, exact: true }).click();
      }
      await page.getByRole("alertdialog").getByRole("button", { name: "実行", exact: true }).click();
    };
    await expect(actions).toBeVisible();
    await invoke();
    await expect.poll(() => calls).toBe(1);
    await expect(actions.getByRole("button", { name: "編集", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "新規作成", exact: true })).toBeDisabled();
    await fulfill(pending!, "ロール操作に失敗しました", 503);
    await expect(page.getByText("ロール操作に失敗しました", { exact: true })).toBeVisible();
    await invoke();
    await expect.poll(() => calls).toBe(2);
    await fulfill(pending!, { ...role, archived: action === "archive", version: role.version + 1 });
    await expect(actions.getByRole("button", { name: "編集", exact: true })).toBeEnabled();
    await expect(page.getByText("ロール操作に失敗しました", { exact: true })).toHaveCount(0);
  });
}

test("ロール管理レビュー: 未保存変更を保護し保存中は入力を固定する", async ({ page }) => {
  const role = await mockReviewedRoles(page);
  let pending: Route | undefined;
  await page.route("**/api/security/roles/review-role", route => { pending = route; });
  await page.goto("/settings/security/roles?role=review-role");
  const edit = () => page.getByTestId("security-roles-detail-actions").getByRole("button", { name: "編集", exact: true }).click();
  await edit();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("list-detail-security-roles")).toBeVisible();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await edit();
  const name = page.getByLabel("ロール名", { exact: false });
  await name.fill("未保存ロール");
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(name).toHaveValue("未保存ロール");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  // ロール管理の保存は基本情報だけを送る（#206）。
  expect(pending!.request().postDataJSON()).toEqual({
    version: role.version,
    display_name: "未保存ロール",
    description: role.description,
  });
  await expect(name).toBeDisabled();
  await expect(page.getByRole("button", { name: "一覧へ戻る" })).toBeDisabled();
  await fulfill(pending!, { ...role, display_name: "未保存ロール", version: role.version + 1 });
  await expect(name).toBeEnabled();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("list-detail-security-roles")).toBeVisible();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
});

test("権限管理レビュー: 選択順を除いて未保存変更を保護し保存中は入力を固定する", async ({ page }) => {
  const role = await mockReviewedRoles(page);
  let pending: Route | undefined;
  await page.route("**/api/security/roles/review-role/permissions", route => { pending = route; });
  await page.goto("/settings/security/permissions?role=review-role");
  const edit = () => page.getByTestId("security-permissions-detail-actions").getByRole("button", { name: "権限を編集", exact: true }).click();
  await edit();
  const query = page.getByRole("checkbox", { name: /SQL 生成/ });
  await query.uncheck();
  await query.check();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("list-detail-security-permissions")).toBeVisible();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await edit();
  await query.uncheck();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(query).not.toBeChecked();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  expect(pending!.request().method()).toBe("PUT");
  expect(pending!.request().postDataJSON()).toEqual({
    version: role.version,
    permissions: ["menu.history"],
    allowed_profile_ids: [],
  });
  await expect(query).toBeDisabled();
  await expect(page.getByRole("button", { name: "一覧へ戻る" })).toBeDisabled();
  await fulfill(pending!, { ...role, permissions: ["menu.history"], version: role.version + 1 });
  await expect(query).toBeEnabled();
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("list-detail-security-permissions")).toBeVisible();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
});

async function mockReviewedDeepSec(page: Page) {
  await mockDatabaseGateReady(page);
  const grant = { entitlement_id: "review-grant", resource_code: "HR.EMPLOYEES", scope_code: "*", capability: "SELECT", target_owner: "HR", target_object: "EMPLOYEES", target_type: "TABLE", column_names: ["EMPLOYEE_ID"], scope_mode: "ALL", scope_column: "", scope_filters: [] };
  const role = { ...systemRole, role_id: "review-a", role_code: "REVIEW_A", display_name: "A レビュー", is_built_in: false, archived: false, version: 1, data_entitlements: [grant] };
  const other = { ...role, role_id: "review-b", role_code: "REVIEW_B", display_name: "B レビュー", data_entitlements: [] };
  const status = { configured: true, driver_mode: "thin", connection_security: "wallet_mtls", deepsec_enabled: true, data_user: "DEEPSEC_DATA_USER", has_data_user_password: true, objects: {}, message: "構成済みです。" };
  await page.route("**/api/security/deepsec/status", route => fulfill(route, status));
  await page.route("**/api/security/deepsec/plan", route => fulfill(route, deepSecPlan(true)));
  await mockDeepSecDataEntitlements(page, [role, other]);
  return { role, other, grant, status };
}

test("DeepSec レビュー: プレビュー待機中は対象と列を固定する", async ({ page }, testInfo) => {
  const { role, grant } = await mockReviewedDeepSec(page);
  let pending: Route | undefined;
  await page.route("**/api/security/deepsec/data-entitlements/review-a/preview", route => { pending = route; });
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  await page.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
  await page.getByTestId("security-deepsec-sql-preview-generate").click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  await expect(page.getByTestId("security-deepsec-entitlement-role-review-b")).toBeDisabled();
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).toBeDisabled();
  await expect(page.getByRole("tab", { name: "基盤構成", exact: true })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("deepsec-preview-pending.png"), fullPage: true });
  await fulfill(pending!, { role_id: role.role_id, version: role.version, data_entitlements: [{ ...grant, sql: ["SELECT EMPLOYEE_ID FROM HR.EMPLOYEES"], checksum: "review" }], cleanup_sql: [], checksum: "review" });
  await expect(page.getByTestId("security-deepsec-entitlement-role-review-a")).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("checkbox", { name: /EMPLOYEE_ID/ })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).toBeEnabled();
});

test("DeepSec レビュー: 列とタブの変更で実行確認を解除する", async ({ page }) => {
  await mockReviewedDeepSec(page);
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  const confirmation = page.getByRole("textbox", { name: "実行確認語" });
  await confirmation.fill("ADMIN_EXECUTE");
  await page.getByRole("checkbox", { name: /DISPLAY_NAME/ }).check();
  await expect(confirmation).toHaveValue("");
  await expect(page.getByRole("button", { name: "Data Grant を適用", exact: true })).toBeDisabled();
  await confirmation.fill("ADMIN_EXECUTE");
  await page.getByRole("tab", { name: "基盤構成", exact: true }).click();
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  await expect(confirmation).toHaveValue("");
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).toBeChecked();
});

test("DeepSec レビュー: 未保存権限は検索と再取得で保持しロール切替で破棄確認する", async ({ page }, testInfo) => {
  const { role, other } = await mockReviewedDeepSec(page);
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  await page.getByRole("checkbox", { name: /DISPLAY_NAME/ }).check();
  await page.getByTestId("security-deepsec-entitlement-search").fill("B レビュー");
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).toBeChecked();
  await page.getByTestId("security-deepsec-entitlement-search").fill("");
  await page.route("**/api/security/deepsec/data-entitlements", route => fulfill(route, [{ ...role, version: 2 }, other]));
  const refresh = page.getByRole("button", { name: "最新情報を取得", exact: true });
  await refresh.click();
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).toBeChecked();
  let pendingApply: Route | undefined;
  await page.route("**/api/security/deepsec/data-entitlements/review-a/apply", route => { pendingApply = route; });
  await page.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await page.getByRole("button", { name: "Data Grant を適用", exact: true }).click();
  await expect.poll(() => Boolean(pendingApply)).toBe(true);
  expect(pendingApply!.request().postDataJSON().version).toBe(1);
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).toBeDisabled();
  await expect(page.getByTestId("security-deepsec-entitlement-role-review-b")).toBeDisabled();
  await fulfill(pendingApply!, "ロールが別の操作で更新されています。", 409);
  await expect(page.getByText("ロールが別の操作で更新されています。", { exact: true })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).toBeChecked();
  const restart = page.getByRole("button", { name: "最新設定で編集をやり直す", exact: true });
  await restart.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).toBeChecked();
  await restart.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "最新設定で編集をやり直す" }).click();
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).not.toBeChecked();
  await page.getByRole("checkbox", { name: /DISPLAY_NAME/ }).check();
  await page.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  pendingApply = undefined;
  await page.getByRole("button", { name: "Data Grant を適用", exact: true }).click();
  await expect.poll(() => Boolean(pendingApply)).toBe(true);
  expect(pendingApply!.request().postDataJSON().version).toBe(2);
  const appliedRole = { ...role, version: 3, data_entitlements: pendingApply!.request().postDataJSON().data_entitlements };
  await page.route("**/api/security/deepsec/data-entitlements", route => fulfill(route, [appliedRole, other]));
  await fulfill(pendingApply!, { role: appliedRole, status: "APPLIED", checksum: "review", cleanup_count: 0, applied_count: 1 });
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).toBeEnabled();
  // 保存後は新しい baseline から次の編集を始める。
  await page.getByRole("checkbox", { name: /DEPARTMENT_CODE/ }).check();
  await page.getByTestId("security-deepsec-entitlement-role-review-b").click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("deepsec-discard-dialog.png"), fullPage: true, animations: "disabled" });
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(page.getByRole("checkbox", { name: /DISPLAY_NAME/ })).toBeChecked();
  await page.getByTestId("security-deepsec-entitlement-role-review-b").click();
  await page.getByRole("alertdialog").getByRole("button", { name: "破棄して移動" }).click();
  await expect(page.getByTestId("security-deepsec-entitlement-role-review-b")).toHaveAttribute("aria-pressed", "true");
});

test("DeepSec レビュー: パスワード保存中は後続入力と他の操作を固定する", async ({ page }) => {
  const { status } = await mockReviewedDeepSec(page);
  let pending: Route | undefined;
  await page.route("**/api/security/deepsec/config", route => { pending = route; });
  await page.goto("/settings/security/deepsec");
  const password = page.locator("#deepsec-data-user-password");
  await password.fill("SyntheticOnly!123");
  await page.getByTestId("security-deepsec-config-actions").getByRole("button", { name: /保存/ }).click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  await expect(password).toBeDisabled();
  await expect(page.getByRole("tab", { name: "基盤構成", exact: true })).toBeDisabled();
  await fulfill(pending!, status);
  await expect(password).toHaveValue("");
  await expect(password).toBeEnabled();
});


test("DeepSec レビュー: 取得対象から消えたロールの草稿は読み取り専用で保護する", async ({ page }) => {
  const { other } = await mockReviewedDeepSec(page);
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  const column = page.getByRole("checkbox", { name: /DISPLAY_NAME/ });
  await column.check();
  await page.route("**/api/security/deepsec/data-entitlements", route => fulfill(route, [other]));
  await page.getByRole("button", { name: "最新情報を取得", exact: true }).click();
  await expect(page.getByText("選択していたロールを取得できません。", { exact: false })).toBeVisible();
  await expect(column).toBeChecked();
  await expect(column).toBeDisabled();
  await expect(page.getByRole("button", { name: "Data Grant を適用", exact: true })).toBeDisabled();
  await page.getByTestId("security-deepsec-entitlement-role-review-b").click();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(column).toBeChecked();
  await page.getByTestId("security-deepsec-entitlement-role-review-b").click();
  await page.getByRole("alertdialog").getByRole("button", { name: "破棄して移動" }).click();
  await expect(page.getByTestId("security-deepsec-entitlement-role-review-b")).toHaveAttribute("aria-pressed", "true");
});

test("セキュリティレビュー: 成功通知が残っていても確認ダイアログを即座に操作できる", async ({ page }, testInfo) => {
  const { status } = await mockReviewedDeepSec(page);
  await page.route("**/api/security/deepsec/config", route => fulfill(route, status));
  await page.goto("/settings/security/deepsec");
  const password = page.locator("#deepsec-data-user-password");
  await password.fill("SyntheticOnly!123");
  await page.getByTestId("security-deepsec-config-actions").getByRole("button", { name: /保存/ }).click();
  await expect(password).toHaveValue("");
  await password.fill("UnsavedSynthetic!123");
  await page.evaluate(() => {
    const link = document.createElement("a");
    link.href = "/settings/security/users";
    link.textContent = "セキュリティ画面へ移動";
    // AppShell の外枠は fixed inset-0 で視口を覆う（#855）。body の末尾のリンクは外枠の下に隠れて押せないため、
    // 利用者が押せる本文（main#pr-main）の中に置く。
    document.getElementById("pr-main")!.append(link);
  });
  await page.getByRole("link", { name: "セキュリティ画面へ移動" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await dialog.evaluate(async node => {
    await Promise.all(node.getAnimations({ subtree: true }).map(animation => animation.finished));
  });
  const notification = page.getByRole("region", { name: "通知", exact: true });
  await expect(notification.getByRole("status")).toBeVisible();
  // 共有 ConfirmDialog の暗幕（alertdialog の親）。通知は共有トークンで暗幕の下（--z-toast < --z-dialog）。
  const dialogLayer = await dialog.locator("xpath=..").evaluate(node => Number(getComputedStyle(node).zIndex));
  const toastLayer = await notification.evaluate(node => Number(getComputedStyle(node).zIndex));
  expect(dialogLayer).toBeGreaterThan(toastLayer);
  for (const button of await dialog.getByRole("button").all()) {
    expect(await button.evaluate(node => {
      const rect = node.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
      return hit === node || node.contains(hit);
    })).toBe(true);
  }
  await page.screenshot({ path: testInfo.outputPath("toast-below-confirmation.png"), animations: "disabled" });
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(password).toHaveValue("UnsavedSynthetic!123");
  await expect(notification.getByRole("status")).toBeVisible();
  await expect(page.getByRole("link", { name: "セキュリティ画面へ移動" })).toBeFocused();
});

// 各主要導線の最終状態で全テキスト・入力欄の字体継承を確認する。
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === "skipped") return;
  await expectLocalUiFonts(page);
});

test("DeepSec 条件グループ: OR・括弧・関連条件・草稿復元と確認解除", async ({ page }, testInfo) => {
  const { role } = await mockReviewedDeepSec(page);
  await page.route("**/api/security/deepsec/scope-profiles", route => fulfill(route, [{ id: "hr", name: "人事", object_scope_version: 1, objects: ["HR.EMPLOYEES", "HR.DEPARTMENTS"] }]));
  await page.route("**/api/security/deepsec/relations?*", route => fulfill(route, { profile_id: "hr", object_scope_version: 1, objects: ["HR.DEPARTMENTS"], relations: [{ id: "HR.FK_DEPT", source: "FOREIGN_KEY", version: "fk-v1", target: "HR.DEPARTMENTS", join_keys: [{ source_column: "DEPARTMENT_CODE", target_column: "CODE" }] }] }));
  await page.route("**/api/security/deepsec/target-objects/HR/DEPARTMENTS?*", route => fulfill(route, { owner: "HR", name: "DEPARTMENTS", object_type: "TABLE", comment: "部署", columns: [{ column_name: "CODE", data_type: "VARCHAR2", logical_name: "", nullable: false, comment: "" }, { column_name: "LOCATION", data_type: "VARCHAR2", logical_name: "", nullable: true, comment: "" }] }));
  let submitted: Record<string, any> | null = null;
  await page.route("**/api/security/deepsec/data-entitlements/review-a/preview", route => {
    submitted = route.request().postDataJSON();
    return fulfill(route, { role_id: role.role_id, version: 1, data_entitlements: submitted!.data_entitlements.map((item: object) => ({ ...item, sql: ["SELECT 1"], checksum: "expression" })), cleanup_sql: [], checksum: "expression" });
  });
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  await chooseFiltersScopeMode(page);
  const root = page.getByTestId("scope-group-0");
  await chooseSelectFieldOption(root.getByLabel("条件の組み合わせ"), "OR");
  const first = page.getByTestId("security-deepsec-scope-filter-0-0");
  await chooseSelectFieldOption(first.getByLabel("値の種類"), "LOGIN_USER_ID");
  await root.getByRole("button", { name: "グループを追加", exact: true }).click();
  const nested = page.getByTestId("scope-group-0-1");
  await chooseSelectFieldOption(nested.getByLabel("列", { exact: true }), "DEPARTMENT_CODE");
  await nested.getByLabel("値", { exact: true }).fill("SALES");
  await nested.getByRole("button", { name: "関連テーブル条件を追加" }).click();
  const related = page.getByTestId("scope-related-0-1-1");
  await chooseSelectFieldOption(related.getByLabel("候補を選ぶ業務プロファイル"), "hr");
  await chooseSelectFieldOption(related.getByLabel("関連テーブル", { exact: true }), "HR.DEPARTMENTS");
  await chooseSelectFieldOption(related.getByLabel("関連キーの設定方法"), "HR.FK_DEPT");
  await expectSelectFieldValue(related.getByLabel("対象テーブルの列"), "DEPARTMENT_CODE");
  await expectSelectFieldValue(related.getByLabel("関連テーブルの列"), "CODE");
  await chooseSelectFieldOption(related.getByLabel("列", { exact: true }), "LOCATION");
  await related.getByLabel("値", { exact: true }).fill("東京");
  await expect(page.getByTestId("scope-expression-summary")).toContainText("OR");
  await expect(page.getByTestId("scope-expression-summary")).toContainText("AND");
  await expect(page.getByTestId("scope-expression-summary")).toContainText("東京");
  await page.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await related.getByLabel("値", { exact: true }).fill("大阪");
  await expect(page.getByRole("textbox", { name: "実行確認語" })).toHaveValue("");
  await expectNoPageHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("deepsec-expression.png"), fullPage: true });
  await expectNoElementHorizontalOverflow(root);
  await page.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
  await page.getByTestId("security-deepsec-sql-preview-generate").click();
  await expect.poll(() => submitted?.data_entitlements[0].scope_expression.root.operator).toBe("OR");
  const tree = submitted!.data_entitlements[0].scope_expression;
  expect(tree.root.children[1].children[1].join_keys).toEqual([{ source_column: "DEPARTMENT_CODE", target_column: "CODE" }]);
  // reload で SQL preview・確認語は再利用せず、入力ツリーと baseline/version のみ復元。
  page.on("dialog", dialog => dialog.accept());
  await page.reload();
  await expect(page.getByTestId("scope-expression-summary")).toContainText("大阪");
  await expect(page.getByRole("textbox", { name: "実行確認語" })).toHaveValue("");
  await expectSelectFieldValue(page.getByTestId("scope-group-0").getByLabel("条件の組み合わせ").first(), "OR");
  await page.getByTestId("security-deepsec-scope-filter-0-0").getByRole("button", { name: "条件を削除" }).click();
  await page.getByTestId("scope-group-0").getByRole("button", { name: "グループを削除" }).last().click();
  await page.getByRole("alertdialog").getByRole("button", { name: "グループを削除" }).click();
  await expect(page.getByTestId("scope-group-0").getByText("条件を 1 件以上追加してください。空の条件では保存・適用できません。", { exact: true })).toBeVisible();
  await page.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
  await expect(page.getByTestId("security-deepsec-sql-preview-generate")).toBeDisabled();
});


test("DeepSec 関連条件: 候補の読込・失敗再試行と手動複合キー", async ({ page }) => {
  await mockReviewedDeepSec(page);
  let pending: Route | undefined;
  let attempts = 0;
  await page.route("**/api/security/deepsec/scope-profiles", route => {
    attempts += 1;
    if (attempts === 1) { pending = route; return; }
    return fulfill(route, [{ id: "hr", name: "人事", object_scope_version: 1, objects: ["HR.EMPLOYEES", "HR.DEPARTMENTS"] }]);
  });
  await page.route("**/api/security/deepsec/relations?*", route => fulfill(route, { profile_id: "hr", object_scope_version: 1, objects: ["HR.DEPARTMENTS"], relations: [] }));
  await page.route("**/api/security/deepsec/target-objects/HR/DEPARTMENTS?*", route => fulfill(route, { owner: "HR", name: "DEPARTMENTS", object_type: "TABLE", comment: "", columns: ["CODE", "TENANT", "LOCATION"].map(column_name => ({ column_name, data_type: "VARCHAR2", nullable: false, logical_name: "", comment: "" })) }));
  let submitted: any;
  await page.route("**/api/security/deepsec/data-entitlements/review-a/preview", route => {
    submitted = route.request().postDataJSON();
    return fulfill(route, { role_id: "review-a", version: 1, data_entitlements: submitted.data_entitlements, cleanup_sql: [], checksum: "manual" });
  });
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  await chooseFiltersScopeMode(page);
  await chooseSelectFieldOption(page.getByTestId("security-deepsec-scope-filter-0-0").getByLabel("値の種類"), "LOGIN_USER_ID");
  const addRelated = page.getByRole("button", { name: "関連テーブル条件を追加", exact: true });
  await addRelated.focus();
  await addRelated.press("Enter");
  const related = page.getByTestId("scope-related-0-1");
  await expect(related.getByRole("status")).toBeVisible();
  await expect.poll(() => Boolean(pending)).toBe(true);
  await fulfill(pending!, "関連候補を取得できませんでした", 503);
  await expect(related.getByText("関連候補を取得できませんでした", { exact: true })).toBeVisible();
  await related.getByRole("button", { name: "再試行", exact: true }).click();
  await chooseSelectFieldOption(related.getByLabel("候補を選ぶ業務プロファイル"), "hr");
  await chooseSelectFieldOption(related.getByLabel("関連テーブル", { exact: true }), "HR.DEPARTMENTS");
  await expectSelectFieldValue(related.getByLabel("関連キーの設定方法"), "");
  await chooseSelectFieldOption(related.getByLabel("対象テーブルの列"), "DEPARTMENT_CODE");
  await chooseSelectFieldOption(related.getByLabel("関連テーブルの列"), "CODE");
  await related.getByRole("button", { name: "関連キーを追加", exact: true }).click();
  await chooseSelectFieldOption(related.getByLabel("対象テーブルの列").nth(1), "DISPLAY_NAME");
  await chooseSelectFieldOption(related.getByLabel("関連テーブルの列").nth(1), "TENANT");
  await chooseSelectFieldOption(related.getByLabel("列", { exact: true }), "LOCATION");
  await related.getByLabel("値", { exact: true }).fill("東京");
  await page.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
  await page.getByTestId("security-deepsec-sql-preview-generate").click();
  await expect.poll(() => submitted?.data_entitlements[0].scope_expression.root.children[1].join_keys.length).toBe(2);
  expect(submitted.data_entitlements[0].scope_expression.root.children[1]).toMatchObject({ relation_source: "MANUAL", join_keys: [{ source_column: "DEPARTMENT_CODE", target_column: "CODE" }, { source_column: "DISPLAY_NAME", target_column: "TENANT" }] });
  await expectNoPageHorizontalScroll(page);
});

for (const dirty of [false, true]) {
  test(`DeepSec 修正回帰: ${dirty ? "未保存草稿は旧版を保持" : "未編集の復元は最新サーバー版を使用"}`, async ({ page }) => {
    const { role, other } = await mockReviewedDeepSec(page);
    await page.goto("/settings/security/deepsec");
    await page.getByRole("tab", { name: "データ権限", exact: true }).click();
    const displayName = page.getByRole("checkbox", { name: /DISPLAY_NAME/ });
    await expect(displayName).toBeEnabled();
    if (dirty) await displayName.check();
    await page.route("**/api/security/deepsec/data-entitlements", route => fulfill(route, [{
      ...role, version: 2,
      data_entitlements: [{ ...role.data_entitlements[0], column_names: ["EMPLOYEE_ID", "DEPARTMENT_CODE"] }],
    }, other]));
    const versions: number[] = [];
    await page.route("**/api/security/deepsec/data-entitlements/review-a/preview", route => {
      const payload = route.request().postDataJSON();
      versions.push(payload.version);
      return fulfill(route, { role_id: role.role_id, version: payload.version, data_entitlements: payload.data_entitlements, cleanup_sql: [], checksum: "latest" });
    });
    page.on("dialog", dialog => dialog.accept());
    await page.reload();
    await expect(displayName).toBeChecked({ checked: dirty });
    await expect(page.getByRole("checkbox", { name: /DEPARTMENT_CODE/ })).toBeChecked({ checked: !dirty });
    const restart = page.getByRole("button", { name: "最新設定で編集をやり直す", exact: true });
    if (dirty) await expect(restart).toBeVisible();
    else await expect(restart).not.toBeVisible();
    await page.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
    await page.getByTestId("security-deepsec-sql-preview-generate").click();
    await expect.poll(() => versions.length).toBe(1);
    expect(versions).toEqual([dirty ? 1 : 2]);
    await page.getByRole("button", { name: "最新情報を取得", exact: true }).click();
    await page.getByTestId("security-deepsec-sql-preview-generate").click();
    await expect.poll(() => versions.length).toBe(2);
    expect(versions).toEqual(dirty ? [1, 1] : [2, 2]);
  });
}

test("DeepSec 修正回帰: 新規草稿を復元した後もルールを独立して追加・選択できる", async ({ page }) => {
  await mockReviewedDeepSec(page);
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  const add = page.getByRole("button", { name: "データ権限を追加", exact: true });
  await add.click();
  await expect(page.getByTestId("security-deepsec-entitlement-rule-1")).toBeVisible();
  page.on("dialog", dialog => dialog.accept());
  await page.reload();
  await expect(page.getByTestId("security-deepsec-entitlement-rule-tab-1")).toBeVisible();
  await add.focus();
  await add.press("Enter");
  await expect(page.getByTestId("security-deepsec-entitlement-rule-2")).toBeVisible();
  await expect(page.getByTestId("security-deepsec-entitlement-rule-tab-1")).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByTestId("security-deepsec-entitlement-rule-tab-2")).toHaveAttribute("aria-pressed", "true");
  await page.getByTestId("security-deepsec-entitlement-rule-tab-1").click();
  await expect(page.getByTestId("security-deepsec-entitlement-rule-1")).toBeVisible();
  await expect(page.getByTestId("security-deepsec-entitlement-rule-tab-2")).toHaveAttribute("aria-pressed", "false");
  await page.getByRole("button", { name: "データ権限を削除", exact: true }).click();
  await expect(page.getByTestId("security-deepsec-entitlement-rule-tab-2")).toHaveCount(0);
  await expect(page.getByTestId("security-deepsec-entitlement-rule-1")).toBeVisible();
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec 修正回帰: 離脱のキャンセルは保持し明示的な破棄は草稿からも消す", async ({ page }) => {
  await mockReviewedDeepSec(page);
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  const column = page.getByRole("checkbox", { name: /DISPLAY_NAME/ });
  await column.check();
  await page.evaluate(() => {
    const link = document.createElement("a");
    link.href = "/settings/security/users";
    link.textContent = "レビュー用の内部リンク";
    // AppShell の外枠は fixed inset-0 で視口を覆う（#855）。body の末尾のリンクは外枠の下に隠れて押せないため、
    // 利用者が押せる本文（main#pr-main）の中に置く。
    document.getElementById("pr-main")!.append(link);
  });
  const link = page.getByRole("link", { name: "レビュー用の内部リンク" });
  await link.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(column).toBeChecked();
  await link.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "破棄して移動" }).click();
  await expect(page).toHaveURL(/\/settings\/security\/users$/);
  await page.goBack();
  await expect(column).not.toBeChecked();
  await page.reload();
  await expect(column).not.toBeChecked();
  await column.check();
  page.on("dialog", dialog => dialog.accept());
  await page.reload();
  await expect(column).toBeChecked();
});

test("DeepSec 修正回帰: 関連 VIEW と TABLE の実種別で列と送信内容を更新する", async ({ page }, testInfo) => {
  await mockReviewedDeepSec(page);
  const objects = ["HR.DEPARTMENTS_VIEW", "HR.DEPARTMENTS"];
  await page.route("**/api/security/deepsec/scope-profiles", route => fulfill(route, [{ id: "hr", name: "人事", object_scope_version: 1, objects: ["HR.EMPLOYEES", ...objects] }]));
  await page.route("**/api/security/deepsec/relations?*", route => fulfill(route, { profile_id: "hr", object_scope_version: 1, objects, relations: [] }));
  for (const name of ["DEPARTMENTS_VIEW", "DEPARTMENTS"]) {
    await page.route(`**/api/security/deepsec/target-objects/HR/${name}?*`, route => {
      const type = name.endsWith("_VIEW") ? "VIEW" : "TABLE";
      const requestedType = new URL(route.request().url()).searchParams.get("object_type");
      if (requestedType && requestedType !== type) return fulfill(route, "対象 object が見つかりません。", 404);
      return fulfill(route, { owner: "HR", name, object_type: type, comment: "", columns: ["CODE", "LOCATION"].map(column_name => ({ column_name, data_type: "VARCHAR2", nullable: false, logical_name: "", comment: "" })) });
    });
  }
  let submitted: any;
  await page.route("**/api/security/deepsec/data-entitlements/review-a/preview", route => {
    submitted = route.request().postDataJSON();
    return fulfill(route, { role_id: "review-a", version: 1, data_entitlements: submitted.data_entitlements, cleanup_sql: [], checksum: "view" });
  });
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  await chooseFiltersScopeMode(page);
  await chooseSelectFieldOption(page.getByTestId("security-deepsec-scope-filter-0-0").getByLabel("値の種類"), "LOGIN_USER_ID");
  await page.getByRole("button", { name: "関連テーブル条件を追加", exact: true }).click();
  const related = page.getByTestId("scope-related-0-1");
  await chooseSelectFieldOption(related.getByLabel("候補を選ぶ業務プロファイル"), "hr");
  for (const target of objects) {
    await chooseSelectFieldOption(related.getByLabel("関連テーブル", { exact: true }), target);
    await chooseSelectFieldOption(related.getByLabel("対象テーブルの列"), "DEPARTMENT_CODE");
    await chooseSelectFieldOption(related.getByLabel("関連テーブルの列"), "CODE");
    await chooseSelectFieldOption(related.getByLabel("列", { exact: true }), "LOCATION");
    await related.getByLabel("値", { exact: true }).fill("東京");
    await page.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
    await page.getByTestId("security-deepsec-sql-preview-generate").click();
    await expect.poll(() => submitted?.data_entitlements[0].scope_expression.root.children[1].target_object).toBe(target.split(".")[1]);
    expect(submitted.data_entitlements[0].scope_expression.root.children[1].target_type).toBe(target.endsWith("_VIEW") ? "VIEW" : "TABLE");
  }
  await expectNoPageHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("related-object-types.png"), fullPage: true });
});

test("DeepSec 未選択へ戻す: 任意の選択欄は先頭の「未選択」で空の値に戻せる（#647）", async ({ page }) => {
  await mockRefreshableRelatedMetadata(page);
  const related = await openReviewedRelatedCondition(page);
  // 関連テーブル条件の「列」（ScopeFilterRow の「列を選択」）: マウスで「未選択」を選ぶ。
  const column = related.getByLabel("列", { exact: true });
  await expectSelectFieldValue(column, "LOCATION");
  expect((await selectFieldOptionLabels(column))[0]).toBe("未選択");
  await chooseSelectFieldOption(column, "");
  await expect(column).toHaveText("列を選択");
  // 選び直せる（未選択は一時的な状態）。
  await chooseSelectFieldOption(column, "LOCATION");
  await expect(column).toHaveText("LOCATION · VARCHAR2");
  // 関連テーブルの列: キーボード（↓ で開き Home で先頭の「未選択」、Enter で選ぶ）でも戻せる。
  const targetKey = related.getByLabel("関連テーブルの列");
  await expectSelectFieldValue(targetKey, "CODE");
  await targetKey.focus();
  await targetKey.press("ArrowDown");
  await expect(targetKey).toHaveAttribute("aria-expanded", "true");
  await targetKey.press("Home");
  await targetKey.press("Enter");
  await expect(targetKey).toHaveAttribute("aria-expanded", "false");
  await expectSelectFieldValue(targetKey, "");
  await expect(targetKey).toHaveText("選択してください");
  await expect(targetKey).toBeFocused();
  // 空の値を取らない選択欄（値の種類など）には「未選択」を出さない。
  const valueSource = page.getByTestId("security-deepsec-scope-filter-0-0").getByLabel("値の種類");
  expect(await selectFieldOptionLabels(valueSource)).not.toContain("未選択");
  await expectNoPageHorizontalScroll(page);
});

// 行 scope を「条件で制限」に切り替える。最初の条件の列は、対象の列の一覧（許可列の checkbox と同じ取得）の
// 先頭の列で埋まる（blankScopeFilter）。一覧の取得が終わる前に切り替えると列が空のまま残り、検証エラーで
// SQL プレビューを生成できないため、許可列の checkbox が出てから切り替える（#1141）。
async function chooseFiltersScopeMode(page: Page) {
  await expect(
    page.getByRole("group", { name: /^許可列/ }).getByRole("checkbox").first()
  ).toBeVisible();
  await chooseSelectFieldOption(page.locator("#deepsec-entitlement-scope-mode-0"), "FILTERS");
}

async function openReviewedRelatedCondition(page: Page) {
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  await chooseFiltersScopeMode(page);
  await chooseSelectFieldOption(page.getByTestId("security-deepsec-scope-filter-0-0").getByLabel("値の種類"), "LOGIN_USER_ID");
  await page.getByRole("button", { name: "関連テーブル条件を追加", exact: true }).click();
  const related = page.getByTestId("scope-related-0-1");
  await chooseSelectFieldOption(related.getByLabel("候補を選ぶ業務プロファイル"), "hr");
  await chooseSelectFieldOption(related.getByLabel("関連テーブル", { exact: true }), "HR.DEPARTMENTS");
  await chooseSelectFieldOption(related.getByLabel("対象テーブルの列"), "DEPARTMENT_CODE");
  await chooseSelectFieldOption(related.getByLabel("関連テーブルの列"), "CODE");
  await chooseSelectFieldOption(related.getByLabel("列", { exact: true }), "LOCATION");
  await related.getByLabel("値", { exact: true }).fill("東京");
  return related;
}

async function mockRefreshableRelatedMetadata(page: Page) {
  await mockReviewedDeepSec(page);
  const state = { version: 1, pending: undefined as Route | undefined, hold: false, reads: { profiles: 0, relations: 0, columns: 0 } };
  await page.route("**/api/security/deepsec/scope-profiles", route => {
    state.reads.profiles += 1;
    return fulfill(route, [{ id: "hr", name: "人事", object_scope_version: state.version, objects: ["HR.EMPLOYEES", "HR.DEPARTMENTS"] }]);
  });
  await page.route("**/api/security/deepsec/relations?*", route => {
    state.reads.relations += 1;
    if (state.hold) { state.pending = route; return; }
    return fulfill(route, { profile_id: "hr", object_scope_version: state.version, objects: ["HR.DEPARTMENTS"], relations: [] });
  });
  await page.route("**/api/security/deepsec/target-objects/HR/DEPARTMENTS?*", route => {
    state.reads.columns += 1;
    return fulfill(route, { owner: "HR", name: "DEPARTMENTS", object_type: "TABLE", comment: "", columns: ["CODE", "LOCATION", ...(state.version === 2 ? ["REGION"] : [])].map(column_name => ({ column_name, data_type: "VARCHAR2", nullable: false, logical_name: "", comment: "" })) });
  });
  return state;
}

test("DeepSec 再取得修正: 関連メタデータを更新し草稿保持と明示的な再選択を両立する", async ({ page }, testInfo) => {
  const state = await mockRefreshableRelatedMetadata(page);
  const related = await openReviewedRelatedCondition(page);
  const versions: number[] = [];
  await page.route("**/api/security/deepsec/data-entitlements/review-a/preview", route => {
    const data = route.request().postDataJSON();
    const version = data.data_entitlements[0].scope_expression.root.children[1].object_scope_version;
    versions.push(version);
    if (version !== state.version) return fulfill(route, "Profile の対象範囲が変更されました。", 409);
    return fulfill(route, { role_id: "review-a", version: 1, data_entitlements: data.data_entitlements, cleanup_sql: [], checksum: "refreshed" });
  });
  await page.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  state.version = 2;
  await page.getByRole("button", { name: "最新情報を取得", exact: true }).click();
  await expect.poll(() => state.reads).toEqual({ profiles: 2, relations: 2, columns: 2 });
  await expect(related.getByLabel("値", { exact: true })).toHaveValue("東京");
  await expectSelectFieldValue(related.getByLabel("対象テーブルの列"), "DEPARTMENT_CODE");
  await expect.poll(() => selectFieldOptionLabels(related.getByLabel("列", { exact: true }))).toContain("REGION · VARCHAR2");
  await expect(related.getByText("業務プロファイルまたは対象が変更されました。関連条件を再選択してください。", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "実行確認語" })).toHaveValue("");
  await page.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
  await page.getByTestId("security-deepsec-sql-preview-generate").click();
  await expect.poll(() => versions).toEqual([1]);
  await expect(page.getByText("Profile の対象範囲が変更されました。", { exact: true })).toBeVisible();
  await chooseSelectFieldOption(related.getByLabel("関連テーブル", { exact: true }), "HR.DEPARTMENTS");
  await chooseSelectFieldOption(related.getByLabel("対象テーブルの列"), "DEPARTMENT_CODE");
  await chooseSelectFieldOption(related.getByLabel("関連テーブルの列"), "CODE");
  await chooseSelectFieldOption(related.getByLabel("列", { exact: true }), "REGION");
  await related.getByLabel("値", { exact: true }).fill("東日本");
  await page.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
  await page.getByTestId("security-deepsec-sql-preview-generate").click();
  await expect.poll(() => versions).toEqual([1, 2]);
  await expectNoPageHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("related-metadata-refreshed.png"), fullPage: true });
});

test("DeepSec 再取得修正: 関連情報の待機と失敗を成功扱いせず再試行できる", async ({ page }) => {
  const state = await mockRefreshableRelatedMetadata(page);
  const related = await openReviewedRelatedCondition(page);
  state.hold = true;
  const refresh = page.getByRole("button", { name: "最新情報を取得", exact: true });
  await refresh.click();
  await expect.poll(() => Boolean(state.pending)).toBe(true);
  await expect(refresh).toBeDisabled();
  await expect(page.getByRole("button", { name: "Data Grant を適用", exact: true })).toBeDisabled();
  await expect(page.getByRole("region", { name: "通知", exact: true }).getByRole("status")).toHaveCount(0);
  state.hold = false;
  await fulfill(state.pending!, "関連メタデータの取得失敗", 503);
  await expect(related.getByText("関連メタデータの取得失敗", { exact: true })).toBeVisible();
  await expect(refresh).toBeEnabled();
  await expect(page.getByRole("region", { name: "通知", exact: true }).getByRole("status")).toHaveCount(0);
  await expect(related.getByLabel("値", { exact: true })).toHaveValue("東京");
  await refresh.click();
  await expect.poll(() => state.reads.relations).toBe(3);
  await expect(related.getByText("関連メタデータの取得失敗", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "通知", exact: true }).getByRole("status")).toHaveCount(1);
});

test("DeepSec フォーカス修正: 条件・入れ子グループ・関連カードの追加先へ移動する", async ({ page }) => {
  await mockRefreshableRelatedMetadata(page);
  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  await chooseFiltersScopeMode(page);
  const root = page.getByTestId("scope-group-0");
  const actions = root.locator(":scope > div").last();
  const add = async (container: Locator, name: string) => {
    const button = container.getByRole("button", { name, exact: true });
    await button.focus();
    await button.press("Enter");
  };
  await add(actions, "条件を追加");
  await expect(page.getByTestId("security-deepsec-scope-filter-0-1").getByLabel("列", { exact: true })).toBeFocused();
  await add(actions, "グループを追加");
  const nested = page.getByTestId("scope-group-0-2");
  await expect(nested.getByLabel("条件の組み合わせ")).toBeFocused();
  await add(nested.locator(":scope > div").last(), "条件を追加");
  await expect(page.getByTestId("security-deepsec-scope-filter-0-2-1").getByLabel("列", { exact: true })).toBeFocused();
  await add(actions, "関連テーブル条件を追加");
  await expect(page.getByTestId("scope-related-0-3").getByLabel("候補を選ぶ業務プロファイル")).toBeFocused();
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec は引用が必要な表名を対象に選び、大文字の同名表と取り違えずに適用する (#560)", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const quotedRole = {
    ...systemRole,
    role_id: "role-quoted",
    role_code: "QUOTED_VIEWER",
    display_name: "引用名テーブル閲覧",
    is_built_in: false,
    version: 2,
    data_entitlements: [] as Array<Record<string, unknown>>,
  };
  let entitlementRoles: unknown[] = [quotedRole];
  // カタログ上の値（引用符なし・大文字小文字を保持）。同じ所有者に大文字の同名表もある。
  const upperObject = {
    name: "MIXED_CASE",
    owner: "SALES",
    qualified_name: "SALES.MIXED_CASE",
    object_type: "TABLE",
    comment: "大文字の表",
  };
  const quotedObject = {
    name: "Mixed_Case",
    owner: "SALES",
    qualified_name: 'SALES."Mixed_Case"',
    object_type: "TABLE",
    comment: "引用名の表",
  };
  const column = (column_name: string, data_type: string) => ({
    column_name,
    logical_name: "",
    data_type,
    nullable: true,
    comment: "",
    sample_values: [],
  });
  const detailRequests: string[] = [];
  let previewPayload: Record<string, unknown> | null = null;
  let applyPayload: Record<string, unknown> | null = null;
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: { data_grants: 0 },
      message: "構成済みです。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan(true)));
  await page.route("**/api/security/deepsec/data-entitlements", (route) =>
    fulfill(route, entitlementRoles)
  );
  await page.route("**/api/security/deepsec/target-objects?*", (route) =>
    fulfill(route, {
      runtime: "oracle",
      owner: "",
      items: [upperObject, quotedObject],
      total: 2,
      counts_included: true,
      next_cursor: null,
      warnings: [],
    })
  );
  await page.route(/\/api\/security\/deepsec\/target-objects\/[^?]+\/[^?]+(\?.*)?$/u, (route) => {
    const path = new URL(route.request().url()).pathname;
    const objectName = decodeURIComponent(path.split("/").at(-1) ?? "");
    detailRequests.push(objectName);
    if (objectName === '"Mixed_Case"') {
      return fulfill(route, {
        ...quotedObject,
        columns: [column('"Amount"', "NUMBER"), column("AMOUNT", "NUMBER"), column("ORDER_ID", "NUMBER")],
      });
    }
    return fulfill(route, { ...upperObject, columns: [column("AMOUNT", "NUMBER")] });
  });
  await page.route("**/api/security/deepsec/data-entitlements/role-quoted/preview", async (route) => {
    const payload = route.request().postDataJSON() as {
      version: number;
      data_entitlements: Array<Record<string, unknown>>;
    };
    previewPayload = payload;
    await fulfill(route, {
      role_id: "role-quoted",
      version: payload.version,
      data_entitlements: payload.data_entitlements.map((item) => ({
        entitlement_id: "quoted-0",
        data_grant_name: "NL2SQL_DG_QUOTED",
        sql_checksum: "f".repeat(64),
        apply_status: "PENDING",
        apply_error_message: "",
        applied_at: null,
        sql: ['GRANT SELECT ON SALES."Mixed_Case" TO NL2SQL_APP_DB_ROLE'],
        checksum: "f".repeat(64),
        ...item,
      })),
      cleanup_sql: [],
      checksum: "a".repeat(64),
    });
  });
  await page.route("**/api/security/deepsec/data-entitlements/role-quoted/apply", async (route) => {
    const payload = route.request().postDataJSON() as {
      version: number;
      data_entitlements: Array<Record<string, unknown>>;
    };
    applyPayload = payload;
    const updated = {
      ...quotedRole,
      version: payload.version + 1,
      data_entitlements: payload.data_entitlements.map((item) => ({
        data_grant_name: "NL2SQL_DG_QUOTED",
        sql_checksum: "e".repeat(64),
        apply_status: "APPLIED",
        apply_error_message: "",
        applied_at: "2026-09-14T00:00:00Z",
        sql: [],
        checksum: "e".repeat(64),
        ...item,
      })),
    };
    entitlementRoles = [updated];
    await fulfill(route, {
      role: updated,
      status: "APPLIED",
      checksum: "b".repeat(64),
      cleanup_count: 0,
      applied_count: 1,
    });
  });

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  const entitlementForm = page.getByTestId("security-deepsec-entitlement-form");
  await entitlementForm.getByRole("button", { name: "データ権限を追加" }).click();
  const rule = entitlementForm.getByTestId("security-deepsec-entitlement-rule-0");
  const objectPicker = rule.getByTestId("security-deepsec-object-picker-0");
  const objectButton = objectPicker.locator("#deepsec-entitlement-resource-0");
  const objectPopover = page.getByRole("dialog", { name: "対象 table/view" });
  const upperOption = objectPopover.getByRole("option", { name: /SALES\.MIXED_CASE/u });
  const quotedOption = objectPopover.getByRole("option", { name: /SALES\."Mixed_Case"/u });
  await objectButton.click();
  await expect(upperOption).toBeVisible();
  await expect(quotedOption).toBeVisible();

  await quotedOption.click();
  await expect(objectButton).toContainText('SALES."Mixed_Case"');
  await expect(rule.getByTestId("security-deepsec-entitlement-editor-title-0")).toHaveText(
    'SALES."Mixed_Case"'
  );
  // 詳細 API にはカタログ値ではなく引用付き token を送り、大文字の同名表の詳細を読まない。
  await expect.poll(() => detailRequests).toContain('"Mixed_Case"');
  expect(detailRequests).not.toContain("MIXED_CASE");
  expect(detailRequests).not.toContain("Mixed_Case");
  // 開き直すと、選んだ引用名の表だけが選択中（大文字の同名表は選択中にならない）。
  await objectButton.click();
  await expect(objectButton).toHaveAttribute("aria-expanded", "true");
  await expect(quotedOption).toHaveAttribute("aria-selected", "true");
  await expect(upperOption).toHaveAttribute("aria-selected", "false");
  await objectPopover.getByRole("combobox").press("Escape");
  await expect(objectPopover).toHaveCount(0);
  const quotedAmount = rule.getByRole("checkbox", { name: /^"Amount"/u });
  const upperAmount = rule.getByRole("checkbox", { name: /^AMOUNT/u });
  await quotedAmount.check();
  await expect(quotedAmount).toBeChecked();
  await expect(upperAmount).not.toBeChecked();

  await entitlementForm.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
  await entitlementForm.getByTestId("security-deepsec-sql-preview-generate").click();
  const expectedEntitlement = {
    resource_code: 'SALES."Mixed_Case"',
    scope_code: "*",
    capability: "SELECT",
    target_owner: "SALES",
    target_object: '"Mixed_Case"',
    target_type: "TABLE",
    column_names: ['"Amount"'],
    scope_mode: "ALL",
    scope_column: "",
    scope_filters: [],
  };
  await expect.poll(() => previewPayload).toEqual({
    version: 2,
    data_entitlements: [expectedEntitlement],
  });
  await expect(
    entitlementForm.getByText('GRANT SELECT ON SALES."Mixed_Case" TO NL2SQL_APP_DB_ROLE')
  ).toBeVisible();
  const applyField = entitlementForm.getByTestId("execution-confirmation-field");
  await applyField.getByRole("textbox", { name: "実行確認語" }).fill("ADMIN_EXECUTE");
  await applyField.getByRole("button", { name: "Data Grant を適用" }).click();
  await expect.poll(() => applyPayload).toEqual({
    version: 2,
    confirmation: "ADMIN_EXECUTE",
    data_entitlements: [{ entitlement_id: "quoted-0", ...expectedEntitlement }],
  });
  await expect(page.getByText("Data Grant を適用しました。", { exact: true }).last()).toBeVisible();

  // 保存済みの引用名は再表示後も同じ表・列として選択状態が復元され、未保存変更にならない。
  const savedTab = entitlementForm.getByTestId("security-deepsec-entitlement-rule-tab-0");
  await expect(savedTab).toContainText('SALES."Mixed_Case"');
  await expect(savedTab.getByText("適用済み", { exact: true })).toBeVisible();
  await expect(objectButton).toContainText('SALES."Mixed_Case"');
  await objectButton.click();
  await expect(quotedOption).toHaveAttribute("aria-selected", "true");
  await expect(upperOption).toHaveAttribute("aria-selected", "false");
  await objectPopover.getByRole("combobox").press("Escape");
  await expect(quotedAmount).toBeChecked();
  await expect(upperAmount).not.toBeChecked();
  await expect(applyField.getByRole("button", { name: "Data Grant を適用" })).toBeDisabled();
  await expectNoPageHorizontalScroll(page);
});

test("DeepSec 関連テーブル条件は引用名の表を Profile の候補から選び、大文字の同名表と取り違えない (#561)", async ({ page }) => {
  await mockDatabaseGateReady(page);
  // 対象は引用名の表 SALES."Mixed_Case"（#560 で保存される token）。
  const quotedGrant = {
    entitlement_id: "quoted-grant",
    resource_code: 'SALES."Mixed_Case"',
    scope_code: "*",
    capability: "SELECT",
    target_owner: "SALES",
    target_object: '"Mixed_Case"',
    target_type: "TABLE",
    column_names: ['"Region"'],
    scope_mode: "ALL",
    scope_column: "",
    scope_filters: [],
  };
  const quotedRole = {
    ...systemRole,
    role_id: "role-quoted-relation",
    role_code: "QUOTED_RELATION",
    display_name: "引用名の関連条件",
    is_built_in: false,
    archived: false,
    version: 3,
    data_entitlements: [quotedGrant],
  };
  const column = (column_name: string, data_type = "VARCHAR2") => ({
    column_name,
    logical_name: "",
    data_type,
    nullable: true,
    comment: "",
    sample_values: [],
  });
  await page.route("**/api/security/deepsec/status", (route) =>
    fulfill(route, {
      configured: true,
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      deepsec_enabled: true,
      data_user: "DEEPSEC_DATA_USER",
      has_data_user_password: true,
      objects: { data_grants: 1 },
      message: "構成済みです。",
    })
  );
  await page.route("**/api/security/deepsec/plan", (route) => fulfill(route, deepSecPlan(true)));
  await mockDeepSecDataEntitlements(page, [quotedRole]);
  await page.route("**/api/security/deepsec/target-objects?*", (route) =>
    fulfill(route, {
      runtime: "oracle",
      owner: "",
      items: [
        { name: "MIXED_CASE", owner: "SALES", qualified_name: "SALES.MIXED_CASE", object_type: "TABLE", comment: "" },
        { name: "Mixed_Case", owner: "SALES", qualified_name: 'SALES."Mixed_Case"', object_type: "TABLE", comment: "" },
      ],
      total: 2,
      counts_included: true,
      next_cursor: null,
      warnings: [],
    })
  );
  const detailRequests: string[] = [];
  await page.route(/\/api\/security\/deepsec\/target-objects\/[^?]+\/[^?]+(\?.*)?$/u, (route) => {
    const objectName = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1) ?? "");
    detailRequests.push(objectName);
    if (objectName === '"Regions"') {
      return fulfill(route, {
        owner: "SALES",
        name: "Regions",
        qualified_name: 'SALES."Regions"',
        object_type: "TABLE",
        comment: "引用名の地域",
        columns: [column('"Id"'), column('"Name"'), column("NAME")],
      });
    }
    if (objectName === '"Mixed_Case"') {
      return fulfill(route, {
        owner: "SALES",
        name: "Mixed_Case",
        qualified_name: 'SALES."Mixed_Case"',
        object_type: "TABLE",
        comment: "引用名の受注",
        columns: [column('"Region"'), column("REGION"), column("LOGIN_ID")],
      });
    }
    return fulfill(route, "対象が見つかりません", 404);
  });
  // backend は #561 以降、Profile の対象表を引用規則どおりの修飾名で返す。
  await page.route("**/api/security/deepsec/scope-profiles", (route) =>
    fulfill(route, [
      { id: "sales-quoted", name: "営業（引用名）", object_scope_version: 2, objects: ['SALES."Mixed_Case"', 'SALES."Regions"', "SALES.REGIONS"] },
      { id: "sales-upper", name: "営業（大文字）", object_scope_version: 2, objects: ["SALES.MIXED_CASE", "SALES.REGIONS"] },
    ])
  );
  const relationRequests: URLSearchParams[] = [];
  await page.route("**/api/security/deepsec/relations?*", (route) => {
    relationRequests.push(new URL(route.request().url()).searchParams);
    return fulfill(route, {
      profile_id: "sales-quoted",
      object_scope_version: 2,
      objects: ['SALES."Regions"', "SALES.REGIONS"],
      relations: [
        {
          id: 'SALES."FK_Region"',
          source: "FOREIGN_KEY",
          version: "fk-quoted-v1",
          target: 'SALES."Regions"',
          join_keys: [{ source_column: '"Region"', target_column: '"Id"' }],
        },
      ],
    });
  });
  let submitted: any = null;
  await page.route("**/api/security/deepsec/data-entitlements/role-quoted-relation/preview", (route) => {
    submitted = route.request().postDataJSON();
    return fulfill(route, {
      role_id: quotedRole.role_id,
      version: submitted.version,
      data_entitlements: submitted.data_entitlements.map((item: object) => ({ ...item, sql: ["SELECT 1"], checksum: "quoted" })),
      cleanup_sql: [],
      checksum: "quoted",
    });
  });

  await page.goto("/settings/security/deepsec");
  await page.getByRole("tab", { name: "データ権限", exact: true }).click();
  await chooseFiltersScopeMode(page);
  await chooseSelectFieldOption(page.getByTestId("security-deepsec-scope-filter-0-0").getByLabel("値の種類"), "LOGIN_USER_ID");
  await page.getByRole("button", { name: "関連テーブル条件を追加", exact: true }).click();
  const related = page.getByTestId("scope-related-0-1");
  const profileSelect = related.getByLabel("候補を選ぶ業務プロファイル");
  // 大文字の同名表だけを含む Profile は、引用名の表の関連候補に出さない。
  await expect.poll(() => selectFieldOptionLabels(profileSelect)).toContain("営業（引用名）");
  const profileLabels = await selectFieldOptionLabels(profileSelect);
  expect(profileLabels.filter((label) => label === "営業（引用名）")).toHaveLength(1);
  expect(profileLabels).not.toContain("営業（大文字）");
  await chooseSelectFieldOption(profileSelect, "sales-quoted");
  await expect.poll(() => relationRequests.at(-1)?.get("object_name")).toBe('"Mixed_Case"');
  expect(relationRequests.at(-1)?.get("owner")).toBe("SALES");

  const relatedTable = related.getByLabel("関連テーブル", { exact: true });
  await expect(relatedTable).toBeEnabled();
  const relatedTableLabels = await selectFieldOptionLabels(relatedTable);
  expect(relatedTableLabels.filter((label) => label === 'SALES."Regions"')).toHaveLength(1);
  expect(relatedTableLabels.filter((label) => label === "SALES.REGIONS")).toHaveLength(1);
  await chooseSelectFieldOption(relatedTable, { label: 'SALES."Regions"' });
  await expectSelectFieldValue(relatedTable, 'SALES."Regions"');
  // 関連表の列は引用付き token で取得し、大文字の同名表 REGIONS の詳細を読まない。
  await expect.poll(() => detailRequests).toContain('"Regions"');
  expect(detailRequests).not.toContain("REGIONS");
  expect(detailRequests).not.toContain("Regions");

  const relationSelect = related.getByLabel("関連キーの設定方法");
  await chooseSelectFieldOption(relationSelect, { label: 'FOREIGN_KEY · SALES."FK_Region"' });
  await expectSelectFieldValue(relationSelect, 'SALES."FK_Region"');
  await expectSelectFieldValue(related.getByLabel("対象テーブルの列"), '"Region"');
  await expectSelectFieldValue(related.getByLabel("関連テーブルの列"), '"Id"');
  const relatedColumn = related.getByLabel("列", { exact: true });
  const nameLabel = (await selectFieldOptionLabels(relatedColumn)).find((label) => label.startsWith('"Name" · '));
  expect(nameLabel).toBeTruthy();
  await chooseSelectFieldOption(relatedColumn, { label: nameLabel! });
  await expectSelectFieldValue(relatedColumn, '"Name"');
  await related.getByLabel("値", { exact: true }).fill("関東");
  await expect(related.getByText("業務プロファイルまたは対象が変更されました。", { exact: false })).toHaveCount(0);

  await page.getByText("ロール全体の SQL プレビュー", { exact: true }).click();
  await page.getByTestId("security-deepsec-sql-preview-generate").click();
  await expect.poll(() => submitted?.data_entitlements?.[0]?.scope_expression?.root?.children?.length).toBe(2);
  const entitlement = submitted.data_entitlements[0];
  expect(entitlement).toMatchObject({ target_owner: "SALES", target_object: '"Mixed_Case"', resource_code: 'SALES."Mixed_Case"' });
  expect(entitlement.scope_expression.root.children[1]).toMatchObject({
    profile_id: "sales-quoted",
    target_owner: "SALES",
    target_object: '"Regions"',
    relation_source: "FOREIGN_KEY",
    relation_id: 'SALES."FK_Region"',
    relation_version: "fk-quoted-v1",
    join_keys: [{ source_column: '"Region"', target_column: '"Id"' }],
  });
  expect(JSON.stringify(entitlement.scope_expression.root.children[1].condition)).toContain('"\\"Name\\""');
  await expectNoPageHorizontalScroll(page);
});

test("ユーザー / ロール一覧の選択行は文字が 4.5:1 以上になり、左バーでも選択を示す (#571)", async ({ page }) => {
  await mockDatabaseGateReady(page);
  const customRole = { ...systemRole, role_id: "role-contrast", role_code: "CONTRAST_VIEWER", display_name: "コントラスト確認", description: "表示のみ", is_built_in: false };
  const users = ["contrast.first", "contrast.second"].map((login, index) => ({
    user_uuid: `contrast-user-${index}`,
    login_user_id: login,
    display_name: `コントラスト確認ユーザー ${index + 1}`,
    status: "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: [customRole.role_id],
    is_bootstrap_admin: false,
  }));
  await page.route("**/api/security/roles?include_archived=*", (route) => fulfill(route, [systemRole, customRole]));
  await page.route("**/api/security/users", (route) => fulfill(route, users));
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));
  // 選択行と一覧を並べて見る 2 ペイン（xl 以上）で測る（xl 未満は選ぶと一覧が隠れる。#2282）。
  await page.setViewportSize({ width: 1440, height: 900 });

  for (const prefix of ["security-users", "security-roles"] as const) {
    // 選択は URL が持ち、先頭の行を自動で選ばない（#2282）。
    await page.goto(
      prefix === "security-users"
        ? "/settings/security/users?user=contrast-user-0"
        : "/settings/security/roles?role=role-contrast"
    );
    const grid = page.getByTestId(`${prefix}-grid`);
    const selected = grid.locator('tbody tr[data-selected="true"]');
    await expect(selected).toHaveCount(1);
    const unselected = grid.locator('tbody tr[data-selected="false"]').first();
    await unselected.hover();

    const measured = await selected.evaluate((row) => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 1;
      const context = canvas.getContext("2d", { willReadFrequently: true })!;
      const rgb = (color: string) => {
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = color;
        context.fillRect(0, 0, 1, 1);
        return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3));
      };
      const luminance = (color: number[]) => {
        const [r, g, b] = color.map((v) => (v / 255 <= 0.03928 ? v / 255 / 12.92 : ((v / 255 + 0.055) / 1.055) ** 2.4));
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      const contrast = (a: number[], b: number[]) => {
        const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
        return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
      };
      const rowBg = rgb(getComputedStyle(row).backgroundColor);
      const button = row.querySelector("button")!;
      const firstCellShadow = getComputedStyle(row.firstElementChild!).boxShadow;
      const bar = firstCellShadow.match(/(rgba?\([^)]*\)) [\d.]+px 0px 0px 0px inset/);
      const subtitle = button.querySelector(":scope > span:nth-of-type(2)");
      const code = button.querySelector("span.font-mono");
      return {
        // ユーザーは表示名（ログイン ID・UUID を出さない。#1696）、ロールはロール名（コード・ID は出さない。#1682）。
        // 2 行目の補足は割り当てロール・説明（狭い一覧の列。#2282）。
        firstLineIsCode: button.firstElementChild?.querySelector(".font-mono") != null,
        hasSubtitle: subtitle != null,
        title: contrast(rgb(getComputedStyle(button.firstElementChild!).color), rowBg),
        name: subtitle ? contrast(rgb(getComputedStyle(subtitle).color), rowBg) : 0,
        code: code ? contrast(rgb(getComputedStyle(code).color), rowBg) : 0,
        body: contrast(rgb(getComputedStyle(row).color), rowBg),
        bar: bar ? contrast(rgb(bar[1]), rowBg) : 0,
      };
    });
    expect(measured.title, `${prefix} 題名`).toBeGreaterThanOrEqual(4.5);
    // ユーザーの行は表示名（#1696）、ロールの行はロール名（#1682）。ID・コードの mono は出さない。
    // 補足の行（割り当てロール・説明。#2282）も 4.5:1 以上。
    expect(measured.firstLineIsCode, `${prefix} ID・コードを出さない`).toBe(false);
    expect(measured.hasSubtitle, `${prefix} 補足あり`).toBe(true);
    expect(measured.name, `${prefix} 補足`).toBeGreaterThanOrEqual(4.5);
    expect(measured.body, `${prefix} 本文`).toBeGreaterThanOrEqual(4.5);
    // 選択を背景色の差だけで示さない（WCAG 1.4.1）。左バーは非テキストの 3:1 以上。
    expect(measured.bar, `${prefix} 左バー`).toBeGreaterThanOrEqual(3);
    await expect(unselected).not.toHaveAttribute("data-surface-tint");
  }
});

test("DeepSec と権限管理は 1280 / 1920 / 375px で欄の幅・placeholder・件数が崩れない", { tag: "@responsive" }, async ({ page }) => {
  // ロールの一覧の件数はツールバーの ListCount、欄は値の種類の幅・FormGrid（#1891）。
  test.slow();
  await mockReviewedDeepSec(page);
  await page.route("**/api/security/roles?include_archived=true", (route) => fulfill(route, [systemRole]));
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));
  const widths = test.info().project.name === "mobile-375" ? [375] : [1280, 1920];
  const check = async (label: string) => {
    expect(await measureHorizontalOverflow(page), label).toBeLessThanOrEqual(1);
    expect(await findClippedPlaceholders(page), label).toEqual([]);
    if ((page.viewportSize()?.width ?? 0) >= 640) {
      // DeepSec のロールの一覧は xl 以上で 22rem の列に置き、検索欄はその列の全幅（ListToolbar が 28rem 未満で全幅にする）。
      // 列の幅いっぱいは段の外ではないが、検査は列より広い祖先と比べるので除く。
      const offScale = (await findFieldsOffWidthScale(page)).filter(
        (field) => !(field.name === "検索" && Math.abs(field.width - 22 * 14) <= 1)
      );
      expect(offScale, label).toEqual([]);
    }
  };
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    const suffix = `${width}px`;
    await page.goto("/settings/security/deepsec");
    await page.getByRole("tab", { name: "データ権限", exact: true }).click();
    const toolbar = page.getByTestId("security-deepsec-entitlement-toolbar");
    await expect(toolbar.getByRole("status")).toHaveText("全 2 件");
    await check(`deepsec data permissions ${suffix}`);
    const entitlementSearch = page.getByTestId("security-deepsec-entitlement-search");
    await entitlementSearch.fill("B レビュー");
    await expect(toolbar.getByRole("status")).toHaveText("1 件（全 2 件）");
    // 検索語は作業状態に残る（#1933）。次の幅で開き直す前に消す。
    await entitlementSearch.fill("");
    await expect(toolbar.getByRole("status")).toHaveText("全 2 件");
    await page.getByRole("tab", { name: "DATA USER 認証", exact: true }).click();
    await expect(page.locator("#deepsec-data-user-password")).toBeVisible();
    await check(`deepsec data user ${suffix}`);
    await page.goto("/settings/security/permissions");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await check(`permissions ${suffix}`);
  }
});
