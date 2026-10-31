import { inspectTraceStep } from "../../../../platform/packages/ui/e2e/trace-explorer";
import {timelineSnapshot, timelineTypography} from "../../../../platform/packages/ui/e2e/timeline-snapshot";
import { FONT_MONO_FAMILY } from "../../../../platform/packages/ui/e2e/font-checks";
import {jobProgressEvents, jobProgressTraceId, withJobProgress, withRecordedTrace, type JobProgressStep} from "./_helpers/job-progress";
import { mockTabularPreview } from "./_helpers/tabular-preview";
import { readFile } from "node:fs/promises";
import { draftKey, WORKSPACE_DRAFT_PREFIX } from "../../src/lib/workspace-drafts";
import { expectLocalUiFonts } from "./_helpers/local-fonts";
import { pollWithClock } from "./_helpers/clock";
import { expect, test, type Locator, type Page, type Route, type TestInfo } from "./_helpers/test";
import { closeSidebarNav, openSidebarNav } from "./_helpers/sidebar-nav";
import { measuredVisibleRowsHeight } from "./_helpers/data-table";
import { mockDatabaseGateReady, systemAdminMe } from "./_helpers/database-gate";
import {
  expectSplitPaneReservedTrack,
} from "./_helpers/fixed-split-pane";
import { dropFiles } from "./_helpers/file-dropzone";
import { expectLargeActionButton } from "./_helpers/action-button";
import { expectedControlHeight } from "./_helpers/control-height";
import { expectSingleSpinner } from "./_helpers/single-spinner";
import { clickOntologyEdge, expectLegacyOntologyControls } from "./_helpers/ontology-controls";
import { chooseSelectFieldOption, expectSelectFieldValue } from "./_helpers/select-field";
import { expectFieldLabelOnOneLine } from "./_helpers/field-label";
import { expectDbObjectToolbarLayout, withListToolbarFilters } from "./_helpers/db-object-toolbar";
import {
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  jumpToPage,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

test.beforeEach(async ({ page }) => mockDatabaseGateReady(page));

const jobSnapshotKeys = {
  id: draftKey(systemAdminMe.user_uuid, "legacy", "/query", "nl2sql.activeJobId"),
  startedAt: draftKey(systemAdminMe.user_uuid, "legacy", "/query", "nl2sql.activeJobStartedAt"),
};

/** SelectField（#631）の一覧を開き、選択肢（role=option）を返す。一覧は Portal で描かれるので aria-controls から引く。 */
async function openSelectFieldOptions(combobox: Locator) {
  await combobox.click();
  const listboxId = await combobox.getAttribute("aria-controls");
  const listbox = combobox.page().locator(`[id="${listboxId}"]`);
  await expect(listbox).toBeVisible();
  return listbox.getByRole("option");
}

/** openSelectFieldOptions で開いた一覧を Escape で閉じる（選択は変えない）。 */
async function closeSelectFieldOptions(combobox: Locator) {
  await combobox.press("Escape");
  await expect(combobox).toHaveAttribute("aria-expanded", "false");
}

/** 用語・同義語 / 共通ルールの取込（全置換）の確認ダイアログで「置き換えて取り込む」を押す（#953）。 */
async function confirmMaterialReplace(page: Page) {
  const dialog = page.getByRole("alertdialog");
  await dialog.getByRole("button", { name: "置き換えて取り込む", exact: true }).click();
  await expect(dialog).toHaveCount(0);
}

async function readActiveJobState(page: Page) {
  return page.evaluate((keys) => ({ jobId: sessionStorage.getItem(keys.id), startedAt: sessionStorage.getItem(keys.startedAt) }), jobSnapshotKeys);
}
async function seedActiveJob(page: Page, jobId: string, ageMs = 0) {
  await page.addInitScript(({ keys, owner, ownerKey, jobId, ageMs }) => {
    sessionStorage.setItem(ownerKey, owner);
    sessionStorage.setItem(keys.id, jobId);
    sessionStorage.setItem(keys.startedAt, String(Date.now() - ageMs));
  }, { keys: jobSnapshotKeys, owner: systemAdminMe.user_uuid, ownerKey: WORKSPACE_DRAFT_PREFIX + "owner", jobId, ageMs });
}

async function expectBoundedRowLimit(input: Locator, action: Locator) {
  await expect(input).toHaveAttribute("min", "1");
  await expect(input).toHaveAttribute("max", "100000");
  await expect(input).toHaveAttribute("step", "1");
  // 欄は xs の幅でも、ラベル「取得件数上限」と「必須」は 1 行（#1746）。
  await expectFieldLabelOnOneLine(input);
  // 取得件数上限は共有の TextField（#631）。補足・エラーは TextField の外（行のセルの幅）に置く（input → 枠の div → TextField → セル）。
  const label = input.locator("xpath=../../..");
  const helper = label.locator("p").first();
  await expect(helper).toContainText("1〜100000 の整数。");
  // sm（640px）以上は 1 行、375px では折り返して横にスクロールさせない（#1671）。
  const narrow = (input.page().viewportSize()?.width ?? 1280) < 640;
  await expect(helper).toHaveCSS("white-space", narrow ? "normal" : "nowrap");
  expect(await helper.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  for (const value of ["", "0", "-1", "1.5", "100001"]) {
    await input.fill(value);
    await expect(input).toHaveAttribute("aria-invalid", "true");
    await expect(label.getByRole("alert")).toContainText("取得件数上限は 1 以上 100000 以下の整数を入力してください。");
    await expect(action).toBeDisabled();
  }
  for (const value of ["1", "100000"]) {
    await input.fill(value);
    await expect(input).not.toHaveAttribute("aria-invalid", "true");
    await expect(action).toBeEnabled();
  }
  await input.focus();
  await input.press("ArrowDown");
  await expect(input).toHaveValue("99999");
  await input.fill("100");
  await label.screenshot({ path: test.info().outputPath("row-limit.png") });
}

async function clickPageHeaderAction(page: Page, testId: string, name: string) {
  const actions = page.getByTestId(testId);
  await expect(actions).toBeVisible();
  const visibleButton = actions.getByRole("button", { name, exact: true });
  if (await visibleButton.isVisible()) {
    await visibleButton.click();
    return;
  }
  await actions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name, exact: true }).click();
}

async function clickRowAction(page: Page, testId: string, name: string) {
  const trigger = page.getByTestId(`${testId}-trigger`);
  await expect(trigger).toBeVisible();
  await trigger.click();
  await page.getByRole("menuitem", { name, exact: true }).click();
}

async function clickObjectDetailAction(page: Page, testId: string, name: string) {
  const actions = page.getByTestId(testId);
  await expect(actions).toBeVisible();
  const visibleButton = actions.getByRole("button", { name, exact: true });
  if (await visibleButton.isVisible()) {
    await visibleButton.click();
    return;
  }
  await actions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name, exact: true }).click();
}

async function expectContentActionsRightAligned(actions: Locator) {
  const metrics = await actions.evaluate((node) => {
    const group = node.querySelector('[role="group"]');
    const firstButton = group?.querySelector("button");
    const panel = node.closest('[role="tabpanel"]') ?? node.parentElement;
    const code = panel?.querySelector("pre, textarea");
    if (!group || !firstButton || !panel || !code) return null;
    const buttonRect = firstButton.getBoundingClientRect();
    const groupRect = group.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    const codeRect = code.getBoundingClientRect();
    return {
      codeLeft: codeRect.left,
      firstButtonLeft: buttonRect.left,
      groupRight: groupRect.right,
      panelRight: panelRect.right,
    };
  });
  expect(metrics).not.toBeNull();
  expect(metrics!.groupRight).toBeGreaterThanOrEqual(metrics!.panelRight - 20);
  expect(metrics!.firstButtonLeft).toBeGreaterThan(metrics!.codeLeft);
}

// 取得件数上限は広い画面（lg 以上）で実行ボタンと同じ操作行に置き（1:2）、狭い画面では入力の下にボタンを置く（#575）。
async function expectRowLimitActionRow(input: Locator, button: Locator) {
  await expect(input).toBeVisible();
  await expect(button).toBeVisible();
  const inputBox = await input.boundingBox();
  const buttonBox = await button.boundingBox();
  expect(inputBox).not.toBeNull();
  expect(buttonBox).not.toBeNull();
  // 操作ボタンは取得件数上限（とヘルパーテキスト）の下の行。
  expect(buttonBox!.y).toBeGreaterThan(inputBox!.y + inputBox!.height);
  // 欄は xs の幅でも、ラベル「取得件数上限」と「必須」は 1 行（#1746）。
  await expectFieldLabelOnOneLine(input);
  expect(buttonBox!.x).toBeLessThanOrEqual(inputBox!.x + 1);
  // 取得件数上限の欄（input → 枠の div → TextField → 補足を含むセル）の親が操作の行（#631 で共有の TextField に変わった）。
  const rowWidth = await input.evaluate((element) => element.parentElement!.parentElement!.parentElement!.parentElement!.clientWidth);
  const viewport = input.page().viewportSize();
  if (viewport && viewport.width >= 640) {
    // 件数は短い数値なので xs の幅（8rem。README §4「操作部品の高さと幅」、#613）。
    const xsWidth = await input.evaluate(
      () => 8 * Number.parseFloat(getComputedStyle(document.documentElement).fontSize)
    );
    expect(inputBox!.width).toBeLessThanOrEqual(xsWidth + 1);
  } else {
    expect(inputBox!.width).toBeGreaterThan(rowWidth - 1);
  }
}

async function expectButtonBelowInput(input: Locator, button: Locator) {
  await expect(input).toBeVisible();
  await expect(button).toBeVisible();
  const inputBox = await input.boundingBox();
  const buttonBox = await button.boundingBox();
  expect(inputBox).not.toBeNull();
  expect(buttonBox).not.toBeNull();
  expect(buttonBox!.y).toBeGreaterThan(inputBox!.y + inputBox!.height);
}

async function expectButtonsSameHeight(primary: Locator, secondary: Locator) {
  await expect(primary).toBeVisible();
  await expect(secondary).toBeVisible();
  await expect
    .poll(async () => {
      const [primaryBox, secondaryBox] = await Promise.all([
        primary.boundingBox(),
        secondary.boundingBox(),
      ]);
      if (!primaryBox || !secondaryBox) {
        return Number.POSITIVE_INFINITY;
      }
      return Math.abs(primaryBox.height - secondaryBox.height);
    })
    .toBeLessThanOrEqual(1);
}

function nl2sqlQuestionInput(scope: Page | Locator) {
  return scope.locator("#nl2sql-question-input");
}

/**
 * 業務プロファイルが決まってから質問を入れ、「SQL を生成して実行」が押せることを確かめる（#1519）。
 * 質問の草稿はプロファイル・DB・利用者ごとに保存するため、決まる前に入れた質問は遅い runner で
 * 草稿の切り替えと重なって消え、ボタンが押せないまま 30 秒を超えることがあった。
 */
async function fillQuestionWhenReady(page: Page, text: string, profileId = "default") {
  await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), profileId);
  await nl2sqlQuestionInput(page).fill(text);
  await expect(nl2sqlQuestionInput(page)).toHaveValue(text);
  await expect(page.getByRole("button", { name: "SQL を生成して実行" })).toBeEnabled();
}

async function openNl2SqlExecutionOptions(page: Page) {
  const disclosure = page.getByRole("button", { name: /実行オプション/ });
  if ((await disclosure.getAttribute("aria-expanded")) !== "true") {
    await disclosure.click();
  }
  await expect(disclosure).toHaveAttribute("aria-expanded", "true");
}

function directSqlInput(scope: Page | Locator) {
  return scope.locator("#direct-sql-input");
}

function adminSqlInput(scope: Page | Locator) {
  return scope.locator("#admin-sql-input");
}

const ADMIN_SQL_MUTATION_TOKEN =
  /\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|begin|declare|call)\b/i;
const ADMIN_SQL_Q_QUOTE_CLOSERS: Record<string, string> = {
  "[": "]",
  "(": ")",
  "{": "}",
  "<": ">",
};

function stripAdminSqlLeadingComments(sql: string): string {
  let rest = sql.trim();
  let changed = true;
  while (changed) {
    changed = false;
    if (rest.startsWith("--")) {
      const nextLine = rest.indexOf("\n");
      rest = nextLine >= 0 ? rest.slice(nextLine + 1).trimStart() : "";
      changed = true;
    }
    if (rest.startsWith("/*")) {
      const close = rest.indexOf("*/");
      if (close < 0) return rest;
      rest = rest.slice(close + 2).trimStart();
      changed = true;
    }
  }
  return rest;
}

function maskAdminSqlLiteralsAndComments(sql: string): string {
  const text = String(sql || "");
  let out = "";
  let index = 0;
  while (index < text.length) {
    const char = text[index] ?? "";
    const nextChar = text[index + 1] ?? "";
    if (char === "-" && nextChar === "-") {
      const newline = text.indexOf("\n", index);
      const end = newline >= 0 ? newline : text.length;
      out += " ".repeat(end - index);
      index = end;
      continue;
    }
    if (char === "/" && nextChar === "*") {
      const close = text.indexOf("*/", index + 2);
      if (close < 0) {
        out += text.slice(index);
        break;
      }
      const end = close + 2;
      out += " ".repeat(end - index);
      index = end;
      continue;
    }
    const previous = index > 0 ? text[index - 1] ?? "" : "";
    if (
      (char === "q" || char === "Q") &&
      nextChar === "'" &&
      index + 2 < text.length &&
      !/[A-Za-z0-9_$#]/u.test(previous)
    ) {
      const opener = text[index + 2] ?? "";
      const closer = ADMIN_SQL_Q_QUOTE_CLOSERS[opener] ?? opener;
      const close = text.indexOf(`${closer}'`, index + 3);
      if (close < 0) {
        out += text.slice(index);
        break;
      }
      const end = close + 2;
      out += " ".repeat(end - index);
      index = end;
      continue;
    }
    if (char === "'" || char === '"') {
      let end = index + 1;
      let closed = false;
      while (end < text.length) {
        if (text[end] !== char) {
          end += 1;
          continue;
        }
        if (char === "'" && text[end + 1] === "'") {
          end += 2;
          continue;
        }
        closed = true;
        break;
      }
      if (!closed) {
        out += text.slice(index);
        break;
      }
      end += 1;
      out += " ".repeat(end - index);
      index = end;
      continue;
    }
    out += char;
    index += 1;
  }
  return out;
}

function isMockAdminSqlSelect(sql: string): boolean {
  const stripped = stripAdminSqlLeadingComments(sql);
  const masked = maskAdminSqlLiteralsAndComments(stripped);
  const normalized = masked.trim().replace(/;+$/g, "").trim();
  if (!normalized || normalized.includes(";")) return false;
  if (ADMIN_SQL_MUTATION_TOKEN.test(normalized)) return false;
  return /^(select|with)\b/i.test(normalized.replace(/^\(+/u, "").trimStart());
}

function sqlToQuestionInput(scope: Page | Locator) {
  return scope.locator("#sql-to-question-sql-input");
}

/**
 * SQL を入力して「SQL 分析・質問生成」を押す。再読み込みの直後は初期化（下書きの復元など）が fill の後に走り、
 * 入力が空へ戻ることがあるため、入力が残り、ボタンが押せるようになるまで fill をやり直す（#431）。
 * ボタンは SQL が空でも押せる（押すと欄の直下に理由を出す。#541）ので、入力の値そのものを確かめる。
 */
async function fillSqlAndGenerateQuestions(page: Page, sql: string) {
  const button = page.getByRole("button", { name: "SQL 分析・質問生成", exact: true });
  await expect(async () => {
    await sqlToQuestionInput(page).fill(sql);
    await expect(button).toBeEnabled({ timeout: 1_000 });
    await expect(sqlToQuestionInput(page)).toHaveValue(sql, { timeout: 1_000 });
  }).toPass({ timeout: 15_000 });
  await button.click();
}

async function expectRequiredTextarea(scope: Page | Locator, id: string, label: string) {
  const field = scope.locator(`#${id}`);
  const fieldLabel = scope.locator(`label[for="${id}"]`);
  await expect(fieldLabel).toContainText(label);
  // 必須は共有 RequiredBadge（中立色の「必須」）。入力側の aria-required で伝えるのでバッジは読み上げない。
  await expect(fieldLabel.locator('[aria-hidden="true"]')).toHaveText("必須");
  await expect(field).toHaveAccessibleName(label);
  // 共有 TextareaField はネイティブの required 検証を使わず、実行時にアプリ側で検証する（TextField と同じ。#584）。
  await expect(field).not.toHaveAttribute("required");
  await expect(field).toHaveAttribute("aria-required", "true");
}

async function expectSameVisualWidth(actual: Locator, expected: Locator, tolerance = 4) {
  await expect(actual).toBeVisible();
  await expect(expected).toBeVisible();
  const actualBox = await actual.boundingBox();
  const expectedBox = await expected.boundingBox();
  expect(actualBox).not.toBeNull();
  expect(expectedBox).not.toBeNull();
  expect(Math.abs(actualBox!.width - expectedBox!.width)).toBeLessThanOrEqual(tolerance);
}

async function expectTopToBottomOrder(...locators: Locator[]) {
  const boxes: Array<{ x: number; y: number; width: number; height: number }> = [];
  for (const locator of locators) {
    await expect(locator).toBeVisible();
    const box = await locator.boundingBox();
    expect(box).not.toBeNull();
    boxes.push(box!);
  }
  for (let index = 1; index < boxes.length; index += 1) {
    expect(boxes[index].y).toBeGreaterThanOrEqual(boxes[index - 1].y + boxes[index - 1].height - 1);
  }
}

async function expectCsvUploadLayout(csvPanel: Locator) {
  const tableSection = csvPanel.getByTestId("data-csv-table-section");
  const fileField = csvPanel.getByTestId("data-csv-file-field");
  const modeField = csvPanel.getByTestId("data-csv-mode-field");
  const executionFieldset = csvPanel.getByTestId("data-csv-execution-fieldset");

  await expectSameVisualWidth(executionFieldset, tableSection);
  const viewport = csvPanel.page().viewportSize();
  if (viewport && viewport.width >= 1024) {
    // 広い画面ではファイル選択とアップロードモードを FormGrid（最大 60rem・2 列）の同じ行に置き、
    // モードは値の長さの md（20rem）にする（#575 / #1891）。
    await expectTopToBottomOrder(tableSection, fileField, executionFieldset);
    const modeCombobox = modeField.getByRole("combobox", { name: "アップロードモード" });
    const [file, modeCell, mode] = await Promise.all([
      fileField.boundingBox(),
      modeField.boundingBox(),
      modeCombobox.boundingBox(),
    ]);
    expect(Math.abs(file!.y - modeCell!.y)).toBeLessThanOrEqual(1);
    expect(mode!.x).toBeGreaterThan(file!.x + file!.width);
    expect(Math.abs(mode!.width - 20 * 14)).toBeLessThanOrEqual(2);
    expect(file!.width).toBeGreaterThan(mode!.width);
  } else {
    await expectSameVisualWidth(tableSection, fileField);
    await expectSameVisualWidth(modeField, fileField);
    await expectTopToBottomOrder(tableSection, fileField, modeField, executionFieldset);
  }
  const modeSelect = modeField.getByRole("combobox", { name: "アップロードモード" });
  await expect((await openSelectFieldOptions(modeSelect)).filter({ hasText: "DELETE & INSERT(全置換)" })).toHaveCount(1);
  await closeSelectFieldOptions(modeSelect);
}

function mainScroller(page: Page) {
  // 共有 AppShell の <main id="pr-main"> は唯一の main ランドマークなので aria-label を持たない。
  return page.getByRole("main");
}

async function mainScrollTop(page: Page) {
  return mainScroller(page).evaluate((node) => node.scrollTop);
}

async function expectMainScrolledBelowTop(page: Page) {
  await expect.poll(() => mainScrollTop(page)).toBeGreaterThan(0);
}

export async function expectNeutralDangerConfirmationSurface(surface: Locator) {
  await expect(surface).toHaveClass(/max-w-md/);
  // 旧 bg-card → 共有ダイアログ面 bg-surface-overlay。
  await expect(surface).toHaveClass(/\bbg-surface-overlay\b/);
  await expect(surface).toHaveClass(/border-border/);
  await expect(surface).not.toHaveClass(/border-l-danger/);
  await expect(surface).not.toHaveClass(/bg-danger-subtle/);
  await expect(surface.locator('[class*="border-l-danger"]')).toHaveCount(0);
  await expect(surface.getByRole("button", { name: "閉じる" })).toHaveCount(0);
}

// デザインシステム移行後の確認ダイアログ検証(#529)。
// 暗幕は共有トークン --scrim(packages/ui/src/styles/tokens/elevation.css: light-dark(rgb(0 0 0 / 0.5), rgb(0 0 0 / 0.7)))。
// 確認語フィールドは旧 bg-card / bg-background から bg-surface-sunken へ移行し、左アクセントは border-l-danger-fg。
async function expectSharedScrimOverlayCoversViewport(page: Page) {
  const overlay = page.getByTestId("app-dialog-overlay");
  await expect(overlay).toBeVisible();
  const metrics = await overlay.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return {
      rect: [rect.left, rect.top, window.innerWidth - rect.right, window.innerHeight - rect.bottom],
      dark: document.documentElement.classList.contains("dark"),
      backgroundColor: window.getComputedStyle(node).backgroundColor,
    };
  });
  for (const edge of metrics.rect) expect(Math.abs(edge)).toBeLessThanOrEqual(1);
  const alpha = metrics.dark ? "0\\.7" : "0\\.5";
  expect(metrics.backgroundColor).toMatch(new RegExp(`^(rgba\\(0, 0, 0, ${alpha}\\)|oklab\\(0 0 0 \\/ ${alpha}\\))$`, "u"));
}

async function expectNeutralConfirmationFieldSurface(surface: Locator) {
  await expect(surface).toHaveClass(/\bbg-surface-sunken\b/);
  await expect(surface).toHaveClass(/\bborder-border\b/);
  await expect(surface).not.toHaveClass(/border-l-danger|bg-danger-subtle/);
}

async function expectOnlyNeutralConfirmationFieldInDialog(dialog: Locator) {
  const confirmationField = dialog.getByTestId("execution-confirmation-field");
  await expect(confirmationField).toHaveCount(1);
  await expectNeutralConfirmationFieldSurface(confirmationField);
  await expect(dialog.locator('[class*="border-l-danger"]')).toHaveCount(0);
  await expect(dialog.locator(".bg-danger-subtle")).toHaveCount(0);
}

type JsonValue = Record<string, unknown> | unknown[];

interface MockApiState {
  /** 完了したジョブの処理の経過（span のイベント）。省略時は `steps` から mock の backend が作る。 */
  progressEvents?: unknown[];
  previewPayload: Record<string, unknown> | null;
  executePayload: Record<string, unknown> | null;
  jobPayload: Record<string, unknown> | null;
  adminExecutePayload: Record<string, unknown> | null;
  feedbackPayload: Record<string, unknown> | null;
  adminFeedbackPayload: Record<string, unknown> | null;
  feedbackConfigPayload: Record<string, unknown> | null;
  feedbackEntriesDeletePayload: Record<string, unknown> | null;
  profilePatchPayload: Record<string, unknown> | null;
  commentApplyPayload: Record<string, unknown> | null;
  commentGeneratePayload: Record<string, unknown> | null;
  metadataSamplesPayload: Record<string, unknown> | null;
  dbProfileDropPayload: Record<string, unknown> | null;
  dropTablePayload: Record<string, unknown> | null;
  truncateTablePayload: Record<string, unknown> | null;
  evaluationSetPayload: Record<string, unknown> | null;
  samplePayload: Record<string, unknown> | null;
  sampleImportError: boolean;
  previewDataPayload: Record<string, unknown> | null;
  previewDataExportPayload: Record<string, unknown> | null;
  csvUploadPayload: Record<string, unknown> | null;
  importTabularPayload: Record<string, unknown> | null;
  statementsPayload: Record<string, unknown> | null;
  dropViewPayload: Record<string, unknown> | null;
  extractJoinWherePayload: Record<string, unknown> | null;
  analyzePayload: Record<string, unknown> | null;
  reversePayload: Record<string, unknown> | null;
  reverseDeepPayload: Record<string, unknown> | null;
  classifierTrainingImportBody: string | null;
  classifierFeedbackImportPayload: Record<string, unknown> | null;
  classifierTrainingDeleteId: string | null;
  classifierModelListRequests: number;
}

const safety = {
  is_safe: true,
  is_select_only: true,
  row_limit_applied: 0,
  blocked_reason: "",
  warnings: [],
  referenced_tables: ["INVOICES"],
  referenced_columns: ["TOTAL_AMOUNT"],
};

const timing = {
  created_at: "2026-06-21T10:00:00.000Z",
  started_at: "2026-06-21T10:00:00.010Z",
  finished_at: "2026-06-21T10:00:00.180Z",
  elapsed_ms: 170,
  stage_timings: [{ stage: "mock", elapsed_ms: 170 }],
};

const schemaCatalog = {
  refreshed_at: "2026-06-21T10:00:00.000Z",
  tables: [
    {
      table_name: "INVOICES",
      logical_name: "請求",
      owner: "APP",
      table_type: "TABLE",
      comment: "請求情報",
      row_count: 2,
      constraints: ["PK_INVOICES"],
      columns: [
        {
          column_name: "CUSTOMER_NAME",
          logical_name: "取引先名",
          data_type: "VARCHAR2(120)",
          nullable: false,
          comment: "取引先名",
          sample_values: ["架空商事"],
        },
        {
          column_name: "TOTAL_AMOUNT",
          logical_name: "請求金額",
          data_type: "NUMBER",
          nullable: false,
          comment: "税込請求金額",
          sample_values: ["1200000"],
        },
      ],
    },
  ],
};

const overflowSchemaCatalog = {
  ...schemaCatalog,
  tables: [
    {
      table_name:
        "DENPYO_ACTIVITY_LOG_WITH_AN_INTENTIONALLY_LONG_SCHEMA_IDENTIFIER_THAT_USED_TO_FORCE_THE_LEFT_PANE_OVER_THE_DIVIDER",
      logical_name:
        "伝票活動ログ参照用の非常に長い論理テーブル名が分割ペインの境界内で折り返されることを確認する表",
      owner: "APP",
      table_type: "TABLE",
      comment:
        "コメントも長い日本語文として表示され、右側の「質問から SQL を生成して実行」へ重ならないことを確認します。",
      row_count: 266,
      constraints: ["PK_DENPYO_ACTIVITY_LOG_WITH_AN_INTENTIONALLY_LONG_NAME"],
      columns: [
        {
          column_name:
            "CUSTOMER_PAYMENT_RECONCILIATION_STATUS_WITH_A_VERY_LONG_COLUMN_NAME_THAT_USED_TO_OVERFLOW",
          logical_name:
            "入金消込ステータス確認用の非常に長い論理列名がペイン内で折り返されることを確認する項目",
          data_type: "VARCHAR2(4000)",
          nullable: false,
          comment: "長い列コメント",
          sample_values: [
            "未消込かつ確認待ちの非常に長いサンプル値が表示されても横方向へはみ出さない",
          ],
        },
        {
          column_name: "UPDATED_AT",
          logical_name: "更新日時",
          data_type: "TIMESTAMP",
          nullable: true,
          comment: "更新日時",
          sample_values: ["2026-06-21 10:00:00"],
        },
      ],
    },
    ...schemaCatalog.tables,
  ],
};

const profiles = [
  {
    id: "default",
    name: "既定プロファイル",
    category: "既定プロファイル",
    description: "請求・顧客を扱う既定プロファイル",
    allowed_tables: ["INVOICES"],
    allowed_views: [],
    glossary: { 請求金額: "INVOICES.TOTAL_AMOUNT" },
    sql_rules: ["SELECT のみ"],
    default_row_limit: 100,
    safety_policy: "select_only",
    few_shot_examples: [],
    archived: false,
  },
];

const historySql =
  "SELECT i.INVOICE_ID, c.CUSTOMER_NAME, i.DUE_DATE, p.PAID_AT, i.CUSTOMER_PAYMENT_RECONCILIATION_STATUS_WITH_A_VERY_LONG_COLUMN_NAME FROM INVOICES i JOIN CUSTOMERS c ON c.CUSTOMER_ID = i.CUSTOMER_ID LEFT JOIN PAYMENTS p ON p.INVOICE_ID = i.INVOICE_ID WHERE p.PAID_AT IS NULL OR p.PAID_AT > i.DUE_DATE FETCH FIRST 100 ROWS ONLY";

const historyItem = {
  id: "hist-001",
  question: "履歴から再実行したい請求金額",
  engine: "enterprise_ai_direct",
  generated_sql: historySql,
  executable_sql: historySql,
  created_at: "2026-06-21T10:00:00.000Z",
  elapsed_ms: 210,
  feedback_rating: null,
  profile_id: "default",
  profile_name: "既定プロファイル",
  profile_category: "既定プロファイル",
  rewritten_question: "履歴から再実行したい請求金額",
  safety_is_safe: true,
  result_row_count: 1,
  result_columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
  feedback_comment: "",
  admin_feedback_rating: null,
  admin_feedback_content: "",
  admin_feedback_updated_at: "",
};

const longQuestionText =
  '対象テーブル："部署情報を管理するテーブル" 抽出項目："DEPARTMENT_ID", "DEPARTMENT_NAME", "LOCATION", "CREATED_AT" 抽出条件：管理部門および地方拠点を含むすべての部署を確認し、VERY_LONG_UNBROKEN_BUSINESS_QUERY_IDENTIFIER_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ も条件として扱う';

const longHistoryItem = {
  ...historyItem,
  id: "hist-long-query",
  question: longQuestionText,
  rewritten_question: longQuestionText,
  elapsed_ms: 9400,
  feedback_rating: "good",
  feedback_comment: "長い質問でも一覧と詳細の表示が崩れないことを確認します。",
  admin_feedback_rating: "good",
  admin_feedback_content: "管理者確認済みの長い質問です。",
  admin_feedback_updated_at: "2026-06-21T10:02:00.000Z",
};

const classifierTrainingExamples = Array.from({ length: 27 }, (_, index) => {
  const number = index + 1;
  const paddedNumber = String(number).padStart(2, "0");
  return {
    id: `example-${String(number).padStart(3, "0")}`,
    category: index % 2 === 0 ? "既定プロファイル" : "入金管理",
    text:
      index === 0
        ? "ページング対象 01: 請求金額が大きい取引先を見たい"
        : index === 1
          ? "ページング対象 02: 未入金の請求を確認したい"
          : `ページング対象 ${paddedNumber}: 訓練データ確認 ${number}`,
    profile_id: index % 2 === 0 ? "default" : "payment",
    profile_name: index % 2 === 0 ? "既定プロファイル" : "入金管理",
    profile_category: index % 2 === 0 ? "既定プロファイル" : "入金管理",
    source: "training_data.xlsx",
  };
});

/**
 * ジョブの応答（`job_id`・`status`・`steps`）の処理の経過の span を、mock の backend と同じく mock の span の表に書く
 * （#3226 の案 A。画面は `.../trace` で読む）。実行履歴の行（`id` と `progress_events`）も行の ID で書く。
 */
function withMockJobProgress(data: JsonValue): JsonValue {
  if (!data || typeof data !== "object" || Array.isArray(data)) return data;
  const record = data as Record<string, unknown>;
  const isJob = typeof record.job_id === "string" && typeof record.status === "string" && Array.isArray(record.steps);
  if (isJob) return withJobProgress(record) as JsonValue;
  if (typeof record.id === "string" && "progress_events" in record) return withRecordedTrace(record.id, record) as JsonValue;
  return data;
}

function fulfillJson(route: Route, data: JsonValue) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data: withMockJobProgress(data) }),
  });
}

function createRequestGate() {
  let release: () => void = () => undefined;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

async function createFileDataTransfer(
  page: Page,
  files: Array<{ name: string; type: string; content: string }>
) {
  return page.evaluateHandle((items) => {
    const dataTransfer = new DataTransfer();
    for (const item of items) {
      dataTransfer.items.add(new File([item.content], item.name, { type: item.type }));
    }
    return dataTransfer;
  }, files);
}

async function mockNl2SqlApi(page: Page): Promise<MockApiState> {
  const state: MockApiState = {
    previewPayload: null,
    executePayload: null,
    jobPayload: null,
    adminExecutePayload: null,
    feedbackPayload: null,
    adminFeedbackPayload: null,
    feedbackConfigPayload: null,
    feedbackEntriesDeletePayload: null,
    profilePatchPayload: null,
    commentApplyPayload: null,
    commentGeneratePayload: null,
    metadataSamplesPayload: null,
    dbProfileDropPayload: null,
    dropTablePayload: null,
    truncateTablePayload: null,
    evaluationSetPayload: null,
    samplePayload: null,
    sampleImportError: false,
    previewDataPayload: null,
    previewDataExportPayload: null,
    csvUploadPayload: null,
    importTabularPayload: null,
    statementsPayload: null,
    dropViewPayload: null,
    extractJoinWherePayload: null,
    analyzePayload: null,
    reversePayload: null,
    reverseDeepPayload: null,
    classifierTrainingImportBody: null,
    classifierFeedbackImportPayload: null,
    classifierTrainingDeleteId: null,
    classifierModelListRequests: 0,
  };
  let classifierExamples: Record<string, unknown>[] = [...classifierTrainingExamples];
  let classifierIsStale = false;
  let feedbackCandidateAdded = false;
  let appAdminFeedbackRating: "good" | "bad" | null = "good";
  let appAdminFeedbackContent = "管理者確認済み";
  let appAdminFeedbackUpdatedAt = "2026-06-21T10:03:00.000Z";
  const classifierTrainingDataResponse = () => ({
    total_examples: classifierExamples.length,
    categories: Array.from(new Set(classifierExamples.map((example) => String(example.category)))),
    warnings: [],
    examples: classifierExamples,
  });
  const sampleObjects = ["DEPARTMENT", "EMPLOYEE", "PROJECT", "V_EMP_DEPT", "V_DEPT_PROJECT"];
  const sampleSql = {
    tables: [
      "CREATE TABLE DEPARTMENT (DEPARTMENT_ID NUMBER PRIMARY KEY, DEPARTMENT_NAME VARCHAR2(100) NOT NULL)",
      "CREATE TABLE EMPLOYEE (EMPLOYEE_ID NUMBER PRIMARY KEY, EMPLOYEE_NAME VARCHAR2(120) NOT NULL, DEPARTMENT_ID NUMBER)",
      "CREATE TABLE PROJECT (PROJECT_ID NUMBER PRIMARY KEY, PROJECT_NAME VARCHAR2(160) NOT NULL, DEPARTMENT_ID NUMBER)",
    ],
    views: [
      "CREATE OR REPLACE VIEW V_EMP_DEPT AS SELECT E.EMPLOYEE_ID, E.EMPLOYEE_NAME, D.DEPARTMENT_NAME FROM EMPLOYEE E JOIN DEPARTMENT D ON D.DEPARTMENT_ID = E.DEPARTMENT_ID",
      "CREATE OR REPLACE VIEW V_DEPT_PROJECT AS SELECT D.DEPARTMENT_NAME, P.PROJECT_NAME FROM DEPARTMENT D JOIN PROJECT P ON P.DEPARTMENT_ID = D.DEPARTMENT_ID",
    ],
    data: ["INSERT INTO DEPARTMENT (DEPARTMENT_ID, DEPARTMENT_NAME) VALUES (10, '開発部')"],
    delete: [
      "DROP VIEW V_EMP_DEPT",
      "DROP VIEW V_DEPT_PROJECT",
      "DROP TABLE EMPLOYEE PURGE",
      "DROP TABLE PROJECT PURGE",
      "DROP TABLE DEPARTMENT PURGE",
    ],
  };
  let sampleImportedObjects: string[] = [];
  let legacyMaterial: { glossary: Record<string, string>; rules: string[] } = {
    glossary: Object.fromEntries([
      ["売上", "INVOICES.TOTAL_AMOUNT"],
      ...Array.from({ length: 50 }, (_, index) => [
        `用語${index + 2}`,
        `INVOICES.COLUMN_${index + 2}`,
      ]),
    ]),
    rules: ["SELECT のみ", ...Array.from({ length: 20 }, (_, index) => `グローバルルール${index + 2}`)],
  };
  let evaluationSets: Record<string, unknown>[] = [
    {
      id: "eval-001",
      name: "請求ベンチマーク",
      description: "保存済み請求ケース",
      profile_id: "default",
      profile_name: "既定プロファイル",
      engine: "enterprise_ai_direct",
      cases: [
        {
          question: "保存済み請求金額",
          expected_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
          profile_id: "default",
        },
      ],
      created_at: "2026-06-21T10:00:00.000Z",
      updated_at: "2026-06-21T10:00:00.000Z",
      archived: false,
    },
  ];
  let evaluationRuns: Record<string, unknown>[] = [
    {
      id: "eval-run-001",
      created_at: "2026-06-21T10:02:00.000Z",
      evaluation_set_id: "eval-001",
      evaluation_set_name: "請求ベンチマーク",
      profile_id: "default",
      profile_name: "既定プロファイル",
      engine: "enterprise_ai_direct",
      cases: [
        {
          question: "保存済み請求金額",
          expected_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
          profile_id: "default",
        },
      ],
      result: {
        evaluation_suite: "deterministic_mock",
        total_cases: 1,
        executable_rate: 1,
        select_only_rate: 1,
        findings: [],
      },
      report: "NL2SQL deterministic evaluation\nSuite: deterministic_mock\nCases: 1",
    },
  ];

  await page.route("**/api/schema/catalog", (route) => fulfillJson(route, schemaCatalog));
  await page.route("**/api/schema/owners", (route) =>
    fulfillJson(route, {
      current_owner: "APP",
      owners: [{ owner: "APP", is_current: true, table_count: 3, view_count: 1 }],
      excluded_oracle_maintained_count: 0,
    })
  );
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfillJson(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-mock",
      refreshed_at: schemaCatalog.refreshed_at,
      object_count: schemaCatalog.tables.length,
      column_count: schemaCatalog.tables.reduce((total, table) => total + table.columns.length, 0),
      change_token: 1,
      etag: "schema-mock",
    })
  );
  await page.route("**/api/schema/objects?*", (route) =>
    fulfillJson(route, {
      items: schemaCatalog.tables.map((table) => ({
        owner: table.owner,
        object_name: table.table_name,
        object_type: table.table_type,
        logical_name: table.logical_name,
        comment: table.comment,
        row_count: table.row_count,
        column_count: table.columns.length,
        last_ddl_at: "",
      })),
      next_cursor: null,
      total: schemaCatalog.tables.length,
      catalog_version: 1,
    })
  );
  await page.route("**/api/schema/objects/*/*", (route) => {
    const parts = new URL(route.request().url()).pathname.split("/");
    const owner = decodeURIComponent(parts.at(-2) ?? "");
    const objectName = decodeURIComponent(parts.at(-1) ?? "");
    const table = schemaCatalog.tables.find(
      (item) => item.owner === owner && item.table_name === objectName
    );
    return fulfillJson(route, {
      table: table ?? schemaCatalog.tables[0],
      dependencies: [],
      catalog_version: 1,
      etag: "schema-mock",
    });
  });
  await page.route("**/api/nl2sql/sample-data", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      profile_id: "",
      confirmation: "ADMIN_EXECUTE",
      objects: sampleObjects,
      imported_objects: sampleImportedObjects,
      sql: sampleSql,
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/sample-data/import", (route) => {
    state.samplePayload = route.request().postDataJSON() as Record<string, unknown>;
    if (state.sampleImportError) {
      return fulfillJson(route, {
        operation: "import",
        step: state.samplePayload.step ?? "all",
        runtime: "oracle",
        executed: false,
        objects: sampleObjects,
        statements: [
          {
            index: 1,
            statement_type: "CREATE",
            status: "error",
            sql: sampleSql.tables[0],
            error_message: "ORA-00922: missing or invalid option Help: https://docs.oracle.com/error-help/db/ora-00922/",
            elapsed_ms: 30,
          },
        ],
        warnings: [],
        profile_id: "",
        timing,
      });
    }
    sampleImportedObjects = [...sampleObjects];
    return fulfillJson(route, {
      operation: "import",
      step: state.samplePayload.step ?? "all",
      runtime: "deterministic",
      executed: true,
      objects: sampleObjects,
      statements: [{ index: 1, statement_type: "CREATE", status: "applied_to_local_state", sql: sampleSql.tables[0], error_message: "" }],
      warnings: [],
      profile_id: "",
      timing,
    });
  });
  await page.route("**/api/nl2sql/sample-data/delete", (route) => {
    state.samplePayload = route.request().postDataJSON() as Record<string, unknown>;
    sampleImportedObjects = [];
    return fulfillJson(route, {
      operation: "delete",
      step: "all",
      runtime: "deterministic",
      executed: true,
      objects: sampleObjects,
      statements: [{ index: 1, statement_type: "DROP", status: "applied_to_local_state", sql: sampleSql.delete[0], error_message: "" }],
      warnings: [],
      profile_id: "",
      timing,
    });
  });
  await page.route("**/api/nl2sql/db-admin/tables", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      items: [
        {
          name: "INVOICES",
          owner: "APP",
          qualified_name: "APP.INVOICES",
          object_type: "table",
          row_count: 2,
          comment: "請求情報",
        },
        {
          name: "PAYMENTS",
          owner: "APP",
          qualified_name: "APP.PAYMENTS",
          object_type: "table",
          row_count: 1,
          comment: "入金情報",
        },
        {
          name: "AUDIT_LOG",
          owner: "APP",
          qualified_name: "APP.AUDIT_LOG",
          object_type: "table",
          row_count: 1,
          comment: "監査ログ",
        },
      ],
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/views", (route) =>
    fulfillJson(route, {
      runtime: "deterministic",
      items: [
        {
          name: "V_EMP_DEPT",
          owner: "APP",
          qualified_name: "APP.V_EMP_DEPT",
          object_type: "view",
          row_count: null,
          comment: "社員と部署",
        },
      ],
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
    const url = new URL(route.request().url());
    const objectType = url.searchParams.get("type") ?? "all";
    const rowState = url.searchParams.get("row_state") ?? "all";
    const ownerPrefix = (url.searchParams.get("owner_prefix") ?? "").toUpperCase();
    const query = (url.searchParams.get("q") ?? "").toLowerCase();
    const cursor = url.searchParams.get("cursor") ?? "";
    const allItems = [
      { name: "INVOICES", owner: "APP", qualified_name: "APP.INVOICES", object_type: "table", row_count: 2, comment: "請求情報" },
      { name: "PAYMENTS", owner: "APP", qualified_name: "APP.PAYMENTS", object_type: "table", row_count: 1, comment: "入金情報" },
      { name: "AUDIT_LOG", owner: "APP", qualified_name: "APP.AUDIT_LOG", object_type: "table", row_count: 1, comment: "監査ログ" },
      { name: "V_EMP_DEPT", owner: "APP", qualified_name: "APP.V_EMP_DEPT", object_type: "view", row_count: null, comment: "社員と部署" },
      { name: "DBTOOLS$EXECUTION_HISTORY", owner: "APP", qualified_name: "APP.DBTOOLS$EXECUTION_HISTORY", object_type: "table", row_count: 4, comment: "内部履歴" },
      { name: "SYS#AUDIT_VIEW", owner: "APP", qualified_name: "APP.SYS#AUDIT_VIEW", object_type: "view", row_count: null, comment: "内部監査" },
    ];
    const items = allItems.filter((item) => {
      if (ownerPrefix && !item.owner.toUpperCase().startsWith(ownerPrefix)) return false;
      if (objectType !== "all" && item.object_type !== objectType) return false;
      if (rowState === "with_rows" && !(typeof item.row_count === "number" && item.row_count > 0)) return false;
      if (rowState === "empty_rows" && item.row_count !== 0) return false;
      if (rowState === "unknown_rows" && item.row_count !== null) return false;
      return !query || `${item.name} ${item.owner} ${item.comment}`.toLowerCase().includes(query);
    });
    const pagedItems =
      objectType === "table" && !query
        ? cursor === "tables-page-2"
          ? items.slice(2)
          : items.slice(0, 2)
        : items;
    return fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items: pagedItems,
      total: items.length,
      table_count: items.filter((item) => item.object_type === "table").length,
      view_count: items.filter((item) => item.object_type === "view").length,
      next_cursor: objectType === "table" && !query && !cursor && items.length > 2 ? "tables-page-2" : null,
      refreshed_at: schemaCatalog.refreshed_at,
      catalog_version: 1,
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/db-admin/tables/INVOICES?*", (route) =>
    {
      const includeDdl = new URL(route.request().url()).searchParams.get("include_ddl") === "1";
      return fulfillJson(route, {
        name: "INVOICES",
        owner: "APP",
        qualified_name: "APP.INVOICES",
        object_type: "table",
        row_count: 2,
        comment: "請求情報",
        columns: schemaCatalog.tables[0].columns,
        ddl: includeDdl
          ? 'CREATE TABLE "INVOICES" ("CUSTOMER_NAME" VARCHAR2(120), "TOTAL_AMOUNT" NUMBER);\nCOMMENT ON TABLE "INVOICES" IS \'請求情報\';'
          : "",
        warnings: [],
      });
    }
  );
  await page.route("**/api/nl2sql/db-admin/tables/INVOICES", (route) =>
    fulfillJson(route, {
      name: "INVOICES",
      owner: "APP",
      qualified_name: "APP.INVOICES",
      object_type: "table",
      row_count: 2,
      comment: "請求情報",
      columns: schemaCatalog.tables[0].columns,
      ddl: 'CREATE TABLE "INVOICES" ("CUSTOMER_NAME" VARCHAR2(120), "TOTAL_AMOUNT" NUMBER);\nCOMMENT ON TABLE "INVOICES" IS \'請求情報\';',
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/tables/INVOICES/export.xlsx**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      headers: {
        "Content-Disposition": 'attachment; filename="invoices_columns.xlsx"',
      },
      body: "xlsx",
    })
  );
  await page.route("**/api/nl2sql/db-admin/views/V_EMP_DEPT", (route) =>
    fulfillJson(route, {
      name: "V_EMP_DEPT",
      owner: "APP",
      qualified_name: "APP.V_EMP_DEPT",
      object_type: "view",
      row_count: null,
      comment: "社員と部署",
      columns: [
        {
          column_name: "EMPLOYEE_NAME",
          logical_name: "社員名",
          data_type: "VARCHAR2(120)",
          nullable: false,
          comment: "社員名",
          sample_values: [],
        },
      ],
      ddl: 'CREATE OR REPLACE VIEW "V_EMP_DEPT" AS SELECT E.EMPLOYEE_NAME FROM EMPLOYEE E JOIN DEPARTMENT D ON D.DEPARTMENT_ID = E.DEPARTMENT_ID;',
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/views/V_EMP_DEPT?*", (route) =>
    {
      const includeDdl = new URL(route.request().url()).searchParams.get("include_ddl") === "1";
      return fulfillJson(route, {
        name: "V_EMP_DEPT",
        owner: "APP",
        qualified_name: "APP.V_EMP_DEPT",
        object_type: "view",
        row_count: null,
        comment: "社員と部署",
        columns: [
          {
            column_name: "EMPLOYEE_NAME",
            logical_name: "社員名",
            data_type: "VARCHAR2(120)",
            nullable: false,
            comment: "社員名",
            sample_values: [],
          },
        ],
        ddl: includeDdl
          ? 'CREATE OR REPLACE VIEW "V_EMP_DEPT" AS SELECT E.EMPLOYEE_NAME FROM EMPLOYEE E JOIN DEPARTMENT D ON D.DEPARTMENT_ID = E.DEPARTMENT_ID;'
          : "",
        warnings: [],
      });
    }
  );
  await page.route("**/api/nl2sql/db-admin/views/V_EMP_DEPT/export.xlsx**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      headers: {
        "Content-Disposition": 'attachment; filename="v_emp_dept_columns.xlsx"',
      },
      body: "xlsx",
    })
  );
  await page.route("**/api/nl2sql/db-admin/preview-data", (route) => {
    state.previewDataPayload = route.request().postDataJSON() as Record<string, unknown>;
    const owner = String(state.previewDataPayload.owner ?? "").trim();
    const objectName = String(state.previewDataPayload.object_name ?? "INVOICES");
    const rowLimit = Number(state.previewDataPayload.limit ?? 100);
    const returnedRowCount = rowLimit === 0 ? 25 : rowLimit;
    const objectRef = owner ? `"${owner}"."${objectName}"` : `"${objectName}"`;
    const rows = Array.from({ length: returnedRowCount }, (_, index) => ({
      CUSTOMER_NAME: `顧客${String(index + 1).padStart(2, "0")}`,
      TOTAL_AMOUNT: (index + 1) * 1000,
    }));
    return fulfillJson(route, {
      runtime: "deterministic",
      sql:
        rowLimit === 0
          ? `SELECT * FROM ${objectRef}`
          : `SELECT * FROM ${objectRef} FETCH FIRST ${rowLimit} ROWS ONLY`,
      results: {
        columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
        rows,
        total: rows.length,
      },
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/db-admin/preview-data/export.xlsx", (route) => {
    state.previewDataExportPayload = route.request().postDataJSON() as Record<string, unknown>;
    return route.fulfill({
      status: 200,
      contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      headers: {
        "Content-Disposition": 'attachment; filename="invoices_preview.xlsx"',
      },
      body: "xlsx",
    });
  });
  await page.route("**/api/nl2sql/db-admin/upload-csv", (route) => {
    state.csvUploadPayload = route.request().postDataJSON() as Record<string, unknown>;
    const executed = true;
    const uploadOwner = String(state.csvUploadPayload.owner ?? "").trim();
    const uploadTable = String(state.csvUploadPayload.table_name ?? "INVOICES");
    return fulfillJson(route, {
      table_name: uploadOwner ? `${uploadOwner}.${uploadTable}` : uploadTable,
      filename: "upload.csv",
      mode: "insert",
      matched_columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
      unmatched_csv_columns: ["UNKNOWN_COLUMN"],
      row_count: 2,
      success_count: executed ? 2 : 0,
      error_count: 0,
      row_errors: [],
      hint: "",
      executed,
      runtime: "deterministic",
      sample_rows: [{ CUSTOMER_NAME: "架空商事", TOTAL_AMOUNT: "1200000", UNKNOWN_COLUMN: "x" }],
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/nl2sql/db-admin/import-tabular", (route) => {
    state.importTabularPayload = route.request().postDataJSON() as Record<string, unknown>;
    const tableName = String(state.importTabularPayload.table_name ?? "IMPORTED_ORDERS");
    return fulfillJson(route, {
      table_name: tableName,
      filename: state.importTabularPayload.filename ?? "orders.csv",
      sheet_name: state.importTabularPayload.sheet_name ?? "",
      mode: state.importTabularPayload.mode ?? "create",
      columns: [
        { source_name: "ORDER_ID", column_name: "ORDER_ID", data_type: "NUMBER", nullable: false },
        {
          source_name: "ORDER_NAME",
          column_name: "ORDER_NAME",
          data_type: "VARCHAR2(4 CHAR)",
          nullable: true,
        },
      ],
      row_count: 1,
      executed: true,
      ddl: `CREATE TABLE ${tableName} (ORDER_ID NUMBER, ORDER_NAME VARCHAR2(4 CHAR))`,
      insert_sql: `INSERT INTO ${tableName} (ORDER_ID, ORDER_NAME) VALUES (:1, :2)`,
      warnings: [],
      sample_rows: [{ ORDER_ID: "1", ORDER_NAME: "架空商事" }],
      timing,
    });
  });
  await page.route("**/api/nl2sql/db-admin/statements", (route) => {
    state.statementsPayload = route.request().postDataJSON() as Record<string, unknown>;
    const sql = String(state.statementsPayload.sql ?? "INSERT INTO INVOICES (CUSTOMER_NAME) VALUES ('架空商事')");
    const policy = String(state.statementsPayload.policy ?? "data_dml");
    const executed = true;
    const invalidAnnotationName =
      policy === "annotation_sql" && /ADD\s+IF\s+NOT\s+EXISTS\s+COMMENT\b/i.test(sql);
    return fulfillJson(route, {
      executed: invalidAnnotationName ? false : executed,
      runtime: "deterministic",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: policy,
          status: invalidAnnotationName ? "blocked" : "executed",
          sql,
          row_count: null,
          message: invalidAnnotationName ? "" : "executed",
          elapsed_ms: 0,
          error_message: invalidAnnotationName
            ? "ORA-11548 相当: annotation 名 COMMENT は Oracle の予約語です。説明には \"DESCRIPTION\" を使用するか、意図的な名前であれば \"COMMENT\" と二重引用符で囲んでください。"
            : "",
          error_code: invalidAnnotationName ? "ORA-11548" : "",
        },
      ],
      committed: invalidAnnotationName ? false : executed,
      rolled_back: false,
      warnings: invalidAnnotationName
        ? ["禁止された操作が含まれるため実行しませんでした。"]
        : [],
      timing,
    });
  });
  await page.route("**/api/nl2sql/db-admin/execute", (route) => {
    state.adminExecutePayload = route.request().postDataJSON() as Record<string, unknown>;
    const sql = String(state.adminExecutePayload.sql ?? "");
    const confirmation = String(state.adminExecutePayload.confirmation ?? "");
    const isSelect = isMockAdminSqlSelect(sql);
    const isPartialDml = /\bMISSING_TABLE\b/i.test(sql);
    if (!isSelect && confirmation !== "ADMIN_EXECUTE") {
      return fulfillJson(route, {
        executed: false,
        runtime: "oracle",
        select_result: null,
        statements: [
          {
            index: 1,
            statement_type: "UPDATE",
            status: "confirmation_required",
            sql,
            row_count: null,
            message: "",
            elapsed_ms: 0,
            error_message: "ADMIN_EXECUTE が必要です。",
          },
        ],
        committed: false,
        rolled_back: false,
        warnings: ["ADMIN_EXECUTE が必要です。"],
        timing,
      });
    }
    if (isPartialDml) {
      const [successfulSql, failedSql] = sql.split(";").map((statement) => statement.trim());
      return fulfillJson(route, {
        executed: true,
        runtime: "oracle",
        select_result: null,
        statements: [
          {
            index: 1,
            statement_type: "INSERT",
            status: "success",
            sql: successfulSql,
            row_count: 1,
            message: "1 rows affected",
            elapsed_ms: 1,
            error_message: "",
          },
          {
            index: 2,
            statement_type: "UPDATE",
            status: "error",
            sql: failedSql,
            row_count: null,
            message: "",
            elapsed_ms: 2,
            error_message: "ORA-00942: table or view does not exist",
          },
        ],
        committed: true,
        rolled_back: false,
        warnings: ["部分的に成功しました（1/2 件）。成功した SQL はコミット済みです。"],
        timing,
      });
    }
    return fulfillJson(route, {
      executed: true,
      runtime: "oracle",
      select_result: isSelect
        ? {
            columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
            rows: [{ CUSTOMER_NAME: "架空商事", TOTAL_AMOUNT: 1200000 }],
            total: 1,
          }
        : null,
      statements: [
        {
          index: 1,
          statement_type: isSelect ? "SELECT" : "UPDATE",
          status: isSelect ? "executed" : "success",
          sql,
          row_count: isSelect ? 1 : 2,
          message: isSelect ? "1 rows" : "2 rows affected",
          elapsed_ms: 0,
          error_message: "",
        },
      ],
      committed: !isSelect,
      rolled_back: false,
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/nl2sql/db-admin/drop-table", (route) => {
    state.dropTablePayload = route.request().postDataJSON() as Record<string, unknown>;
    const tableName = String(state.dropTablePayload.table_name ?? "INVOICES");
    const owner = String(state.dropTablePayload.owner ?? "").trim();
    const tableRef = owner ? `"${owner}"."${tableName}"` : `"${tableName}"`;
    const executed = true;
    return fulfillJson(route, {
      executed,
      runtime: "deterministic",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "DROP",
          status: "executed",
          sql: `DROP TABLE ${tableRef} PURGE`,
          row_count: null,
          message: "executed",
          elapsed_ms: 0,
          error_message: "",
        },
      ],
      committed: executed,
      rolled_back: false,
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/nl2sql/db-admin/truncate-table", (route) => {
    state.truncateTablePayload = route.request().postDataJSON() as Record<string, unknown>;
    const tableName = String(state.truncateTablePayload.table_name ?? "INVOICES");
    const owner = String(state.truncateTablePayload.owner ?? "").trim();
    const tableRef = owner ? `"${owner}"."${tableName}"` : `"${tableName}"`;
    return fulfillJson(route, {
      executed: true,
      runtime: "deterministic",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "TRUNCATE",
          status: "success",
          sql: `TRUNCATE TABLE ${tableRef}`,
          row_count: 0,
          message: "executed",
          elapsed_ms: 0,
          error_message: "",
        },
      ],
      committed: true,
      rolled_back: false,
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/nl2sql/db-admin/drop-view", (route) => {
    state.dropViewPayload = route.request().postDataJSON() as Record<string, unknown>;
    const viewName = String(state.dropViewPayload.view_name ?? "V_EMP_DEPT");
    const owner = String(state.dropViewPayload.owner ?? "").trim();
    const viewRef = owner ? `"${owner}"."${viewName}"` : `"${viewName}"`;
    const executed = true;
    return fulfillJson(route, {
      executed,
      runtime: "deterministic",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "DROP",
          status: "executed",
          sql: `DROP VIEW ${viewRef}`,
          row_count: null,
          message: "executed",
          elapsed_ms: 0,
          error_message: "",
        },
      ],
      committed: executed,
      rolled_back: false,
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/nl2sql/db-admin/extract-join-where", (route) => {
    state.extractJoinWherePayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      join_text:
        "JOIN: EMPLOYEE(e) JOIN DEPARTMENT(d)\nON: EMPLOYEE(e).DEPARTMENT_ID = DEPARTMENT(d).DEPARTMENT_ID",
      where_text: "EMPLOYEE(e).STATUS = 'A'",
      source: "deterministic",
      warnings: [],
      prompt_profile: "sql_structure",
      structure_markdown:
        "## SQL構造分析\n\n### JOIN句\n- JOIN: EMPLOYEE(e) JOIN DEPARTMENT(d)\n\n### WHERE句\n- EMPLOYEE(e).STATUS = 'A'",
    });
  });
  await page.route("**/api/nl2sql/profiles", (route) => fulfillJson(route, profiles));
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: profiles.map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: profile.archived,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: Object.keys(profile.glossary).length,
        few_shot_count: profile.few_shot_examples.length,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-06-21T10:00:00.000Z",
      })),
      next_cursor: null,
      total: profiles.length,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/legacy-learning-material", (route) =>
    fulfillJson(route, legacyMaterial)
  );
  await page.route("**/api/nl2sql/legacy-learning-material/terms/import", (route) => {
    legacyMaterial = {
      ...legacyMaterial,
      glossary: { 粗利: "INVOICES.PROFIT" },
    };
    return fulfillJson(route, legacyMaterial);
  });
  await page.route("**/api/nl2sql/legacy-learning-material/rules/import", (route) => {
    legacyMaterial = {
      ...legacyMaterial,
      rules: ["集計時は NULL を除外する"],
    };
    return fulfillJson(route, legacyMaterial);
  });
  await page.route("**/api/nl2sql/legacy-learning-material/terms/export.xlsx", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      body: "terms",
    })
  );
  await page.route("**/api/nl2sql/legacy-learning-material/rules/export.xlsx", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      body: "rules",
    })
  );
  await page.route("**/api/nl2sql/profiles/default", (route) => {
    if (route.request().method() === "PATCH") {
      state.profilePatchPayload = route.request().postDataJSON() as Record<string, unknown>;
      return fulfillJson(route, {
        ...profiles[0],
        ...state.profilePatchPayload,
        id: "default",
        archived: false,
      });
    }
    return fulfillJson(route, profiles[0]);
  });
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) => {
    const profileId = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-2) ?? "");
    const profile = profiles.find((item) => item.id === profileId) ?? profiles[0];
    return fulfillJson(route, {
      id: profile.id,
      name: profile.name,
      category: profile.category,
      description: profile.description,
      allowed_tables: profile.allowed_tables,
      allowed_views: profile.allowed_views,
      archived: profile.archived,
      object_scope_version: 1,
      version: 1,
      etag: `etag-${profile.id}`,
      updated_at: "2026-06-21T10:00:00.000Z",
    });
  });
  // 実行履歴の一覧と、`?id=` の詳細の 1 行（GET /api/nl2sql/history/{id}。#1955）。
  await page.route("**/api/nl2sql/history**", (route) =>
    /\/api\/nl2sql\/history\/[^/?]+$/.test(new URL(route.request().url()).pathname)
      ? fulfillJson(route, historyItem)
      : fulfillJson(route, { items: [historyItem], total: 1, limit: 25, offset: 0, has_next: false, unfiltered_total: 1 })
  );
  await page.route(/\/api\/nl2sql\/feedback(?:\?.*)?$/, (route) => {
    if (route.request().method() === "GET") {
      const url = new URL(route.request().url());
      const rating = url.searchParams.get("rating") ?? "all";
      const profileId = url.searchParams.get("profile_id") ?? "";
      const query = (url.searchParams.get("q") ?? "").toLowerCase();
      const appFeedbackRows = [
        {
          ...historyItem,
          feedback_rating: "good",
          feedback_comment: "SQL は期待通りです",
          admin_feedback_rating: appAdminFeedbackRating,
          admin_feedback_content: appAdminFeedbackContent,
          admin_feedback_updated_at: appAdminFeedbackUpdatedAt,
          training_status: feedbackCandidateAdded ? "added" : "pending",
          training_example_id: feedbackCandidateAdded ? "feedback-hist-001" : "",
        },
        {
          ...historyItem,
          id: "hist-002",
          question: "別プロファイルの請求確認",
          generated_sql: "SELECT 2 FROM DUAL",
          executable_sql: "SELECT 2 FROM DUAL",
          profile_id: "payment",
          profile_name: "入金管理",
          profile_category: "入金管理",
          feedback_rating: "bad",
          feedback_comment: "条件が違います",
          admin_feedback_rating: null,
          admin_feedback_content: "",
          admin_feedback_updated_at: "",
          training_status: "pending",
          training_example_id: "",
        },
      ];
      const items = appFeedbackRows.filter((item) => {
        if (rating === "unrated" && item.feedback_rating) return false;
        if (rating !== "all" && rating !== "unrated" && item.feedback_rating !== rating) {
          return false;
        }
        if (profileId && item.profile_id !== profileId) return false;
        if (!query) return true;
        return [
          item.question,
          item.generated_sql,
          item.feedback_comment,
          item.admin_feedback_content,
        ].some((value) => String(value ?? "").toLowerCase().includes(query));
      });
      // offset 型（#1891）。絞り込む前の全件（unfiltered_total）は「N 件（全 M 件）」に使う。
      return fulfillJson(route, {
        items,
        total: items.length,
        limit: 25,
        offset: 0,
        has_next: false,
        unfiltered_total: appFeedbackRows.length,
      });
    }
    state.feedbackPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      history_id: "hist-001",
      rating: "good",
      saved: true,
      comment: "SQL は期待通りです",
      feedback_content: "SQL は期待通りです",
    });
  });
  await page.route("**/api/nl2sql/feedback/admin-review", (route) => {
    state.adminFeedbackPayload = route.request().postDataJSON() as Record<string, unknown>;
    const content = String(state.adminFeedbackPayload.feedback_content ?? "");
    const rating = state.adminFeedbackPayload.rating === "bad" ? "bad" : "good";
    appAdminFeedbackRating = rating;
    appAdminFeedbackContent = content;
    appAdminFeedbackUpdatedAt = "2026-06-21T10:04:00.000Z";
    return fulfillJson(route, {
      history_id: state.adminFeedbackPayload.history_id ?? "hist-001",
      rating,
      saved: true,
      feedback_content: content,
      similar_history_publish: {
        history_id: state.adminFeedbackPayload.history_id ?? "hist-001",
        status: rating === "good" ? "published" : "unpublished",
        runtime: "deterministic",
        executed: false,
        table_name: "",
        index_name: "",
        warnings: [],
      },
    });
  });
  await page.route("**/api/nl2sql/feedback/*", (route) => {
    if (new URL(route.request().url()).pathname.endsWith("/admin-review")) {
      return route.fallback();
    }
    if (route.request().method() === "DELETE") {
      appAdminFeedbackRating = null;
      appAdminFeedbackContent = "";
      appAdminFeedbackUpdatedAt = "";
    }
    return fulfillJson(route, { history_id: "hist-001", cleared: true });
  });
  await page.route("**/api/nl2sql/demo/learning", (route) =>
    fulfillJson(route, {
      seeded_history_count: 3,
      seeded_feedback_count: 3,
      history_ids: [
        "demo-learning-invoice-total",
        "demo-learning-customer-sales",
        "demo-learning-payment-delay",
      ],
      profile_ids: ["default"],
      message: "Demo 学習データを投入しました。",
    })
  );
  await page.route("**/api/nl2sql/feedback-index", (route) =>
    fulfillJson(route, {
      operation: "status",
      status: "stale",
      executed: false,
      runtime: "deterministic",
      source_history_count: 1,
      indexable_count: 1,
      indexed_count: 0,
      vector_dimension: 1536,
      vector_backend: "oracle_26ai",
      embedding_provider: "oci_genai",
      embedding_model: "cohere.embed-v4.0",
      embedding_configured: false,
      ddl: [
        "CREATE TABLE NL2SQL_FEEDBACK_VECTORS (EMBEDDING VECTOR(1536, FLOAT32))",
        "CREATE VECTOR INDEX NL2SQL_FEEDBACK_VEC_IDX ON NL2SQL_FEEDBACK_VECTORS (EMBEDDING)",
      ],
      warnings: [],
      timing,
    })
  );
  await page.route("**/api/nl2sql/feedback-index/rebuild", (route) =>
    fulfillJson(route, {
      operation: "rebuild",
      status: "ready",
      executed: false,
      runtime: "deterministic",
      source_history_count: 1,
      indexable_count: 1,
      indexed_count: 1,
      vector_dimension: 1536,
      vector_backend: "oracle_26ai",
      embedding_provider: "oci_genai",
      embedding_model: "cohere.embed-v4.0",
      embedding_configured: false,
      ddl: [
        "CREATE TABLE NL2SQL_FEEDBACK_VECTORS (EMBEDDING VECTOR(1536, FLOAT32))",
        "CREATE VECTOR INDEX NL2SQL_FEEDBACK_VEC_IDX ON NL2SQL_FEEDBACK_VECTORS (EMBEDDING)",
      ],
      warnings: ["Feedback vector index の rebuild 実行には NL2SQL_RUNTIME_MODE=oracle が必要です。"],
      timing,
    })
  );
  await page.route("**/api/nl2sql/feedback-index/clear", (route) =>
    fulfillJson(route, {
      operation: "clear",
      status: "empty",
      executed: false,
      runtime: "deterministic",
      source_history_count: 1,
      indexable_count: 1,
      indexed_count: 0,
      vector_dimension: 1536,
      vector_backend: "oracle_26ai",
      embedding_provider: "oci_genai",
      embedding_model: "cohere.embed-v4.0",
      embedding_configured: false,
      ddl: ["CREATE TABLE NL2SQL_FEEDBACK_VECTORS (EMBEDDING VECTOR(1536, FLOAT32))"],
      warnings: ["Feedback vector index の clear 実行には NL2SQL_RUNTIME_MODE=oracle が必要です。"],
      timing,
    })
  );
  await page.route("**/api/nl2sql/feedback-entries", (route) =>
    fulfillJson(route, {
      items: [
        {
          history_id: "hist-001",
          question: historyItem.question,
          generated_sql: historyItem.generated_sql,
          profile_id: "default",
          profile_name: "既定プロファイル",
          feedback_rating: null,
          feedback_comment: "",
          admin_feedback_rating: "good",
          admin_feedback_content: "管理者確認済み",
          admin_feedback_updated_at: "2026-06-21T10:03:00.000Z",
          indexed: false,
          created_at: historyItem.created_at,
        },
      ],
      total: 1,
      indexed_count: 0,
    })
  );
  await page.route("**/api/nl2sql/feedback-entries/delete", (route) => {
    state.feedbackEntriesDeletePayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      items: [],
      total: 0,
      indexed_count: 0,
    });
  });
  await page.route("**/api/nl2sql/feedback-config", (route) => {
    if (route.request().method() === "PATCH") {
      state.feedbackConfigPayload = route.request().postDataJSON() as Record<string, unknown>;
      return fulfillJson(route, route.request().postDataJSON() as Record<string, unknown>);
    }
    // 未保存の環境の既定（#1721）。
    return fulfillJson(route, {
      similarity_threshold: 0.9,
      match_limit: 3,
    });
  });
  await page.route("**/api/nl2sql/classifier", (route) =>
    fulfillJson(route, {
      ready: true,
      trained: true,
      stale: classifierIsStale,
      classifier_version: "classifier-001",
      updated_at: "2026-06-21T10:00:00.000Z",
      example_count: classifierExamples.length,
      trained_example_count: classifierTrainingExamples.length,
      pending_change_count: classifierExamples.length - classifierTrainingExamples.length,
      category_count: 2,
      categories: ["既定プロファイル", "入金管理"],
      embedding_model: "deterministic-hash-1536",
      vector_dimension: 1536,
      persistence_mode: "memory",
      recommendation_source: "classifier",
      metrics: { training_accuracy: 1 },
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/classifier/models", (route) => {
    state.classifierModelListRequests += 1;
    return route.abort();
  });
  await page.route("**/api/nl2sql/classifier/training-data", (route) =>
    fulfillJson(route, classifierTrainingDataResponse())
  );
  await page.route("**/api/nl2sql/classifier/training-data/*", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.endsWith("/from-feedback") || url.pathname.endsWith("/import")) {
      return route.fallback();
    }

    const id = decodeURIComponent(url.pathname.split("/").at(-1) ?? "");
    if (request.method() === "PATCH") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      const profile = profiles.find((item) => item.id === payload.profile_id);
      classifierExamples = classifierExamples.map((example) =>
        example.id === id
          ? {
              ...example,
              text: payload.text ?? example.text,
              profile_id: payload.profile_id ?? example.profile_id,
              profile_name: profile?.name ?? example.profile_name,
              profile_category: profile?.category ?? example.profile_category,
              category: profile?.category ?? example.category,
              updated_at: "2026-06-21T10:07:00.000Z",
            }
          : example
      );
      classifierIsStale = true;
      return fulfillJson(route, classifierTrainingDataResponse());
    }
    if (request.method() === "DELETE") {
      state.classifierTrainingDeleteId = id;
      classifierExamples = classifierExamples.filter((example) => example.id !== id);
      classifierIsStale = true;
      return fulfillJson(route, classifierTrainingDataResponse());
    }

    return route.fallback();
  });
  await page.route("**/api/nl2sql/classifier/training-candidates*", (route) =>
    fulfillJson(route, {
      items: [
        {
          history_id: "hist-001",
          question: "履歴から再実行したい請求金額",
          profile_id: "default",
          profile_name: "既定プロファイル",
          feedback_rating: "good",
          feedback_comment: "SQL は期待通りです",
          created_at: historyItem.created_at,
          status: feedbackCandidateAdded ? "added" : "pending",
          training_example_id: feedbackCandidateAdded ? "feedback-hist-001" : "",
          conflict_profile_ids: [],
        },
        {
          history_id: "hist-conflict",
          question: "競合している請求分類を確認したい",
          profile_id: "default",
          profile_name: "既定プロファイル",
          feedback_rating: "good",
          feedback_comment: "Profile の確認が必要です",
          created_at: historyItem.created_at,
          status: "conflict",
          training_example_id: "",
          conflict_profile_ids: ["payment"],
        },
        {
          history_id: "hist-source-changed",
          question: "元 feedback が変更された質問",
          profile_id: "default",
          profile_name: "既定プロファイル",
          feedback_rating: "bad",
          feedback_comment: "後から bad に変更",
          created_at: historyItem.created_at,
          status: "source_changed",
          training_example_id: "feedback-source-changed",
          conflict_profile_ids: [],
        },
      ],
      total: 3,
      next_cursor: "",
      pending_count: feedbackCandidateAdded ? 0 : 1,
      added_count: feedbackCandidateAdded ? 1 : 0,
      attention_count: 2,
    })
  );
  await page.route("**/api/nl2sql/classifier/training-data/from-feedback", (route) => {
    state.classifierFeedbackImportPayload = route.request().postDataJSON() as Record<string, unknown>;
    feedbackCandidateAdded = true;
    classifierIsStale = true;
    classifierExamples = [
      ...classifierExamples,
      {
        id: "feedback-hist-001",
        category: "既定プロファイル",
        text: "履歴から再実行したい請求金額",
        profile_id: "default",
        profile_name: "既定プロファイル",
        source: "feedback:hist-001",
        source_type: "feedback",
        source_history_id: "hist-001",
        created_at: "2026-06-21T10:06:00.000Z",
        updated_at: "2026-06-21T10:06:00.000Z",
      },
    ];
    return fulfillJson(route, {
      imported_count: 1,
      skipped_count: 0,
      conflict_count: 0,
      results: [
        {
          history_id: "hist-001",
          status: "added",
          training_example_id: "feedback-hist-001",
          profile_id: "default",
          message: "",
        },
      ],
    });
  });
  await page.route("**/api/nl2sql/classifier/training-data/import", (route) => {
    state.classifierTrainingImportBody = route.request().postDataBuffer()?.toString("utf8") ?? "";
    return fulfillJson(route, {
      imported_count: 1,
      skipped_count: 0,
      total_examples: classifierTrainingExamples.length + 1,
      categories: ["既定プロファイル", "入金管理"],
      warnings: [],
      examples: [],
    });
  });
  await page.route("**/api/nl2sql/classifier/train", (route) => {
    classifierIsStale = false;
    return fulfillJson(route, {
      ready: true,
      trained: true,
      stale: false,
      classifier_version: "classifier-002",
      updated_at: "2026-06-21T10:05:00.000Z",
      example_count: classifierExamples.length,
      trained_example_count: classifierExamples.length,
      pending_change_count: 0,
      category_count: 2,
      categories: ["既定プロファイル", "入金管理"],
      embedding_model: "deterministic-hash-1536",
      vector_dimension: 1536,
      persistence_mode: "memory",
      recommendation_source: "classifier",
      metrics: { training_accuracy: 1 },
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/classifier/predict", (route) =>
    fulfillJson(route, {
      recommendation_source: "classifier",
      classifier_version: "classifier-002",
      predicted_category: "既定プロファイル",
      confidence: 0.92,
      candidates: [
        { category: "既定プロファイル", score: 0.92, profile_id: "default", profile_name: "既定プロファイル" },
      ],
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/rewrite", (route) => {
    // 入力質問を反映した決定論変換で返す(rewrite を ON にしても payload の質問を判別できる)。
    const body = route.request().postDataJSON() as { question?: string };
    const original = String(body?.question ?? "");
    return fulfillJson(route, {
      original_question: original,
      rewritten_question: `${original}（請求金額=INVOICES.TOTAL_AMOUNT）`,
      source: "deterministic",
      model: "",
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/recommend-profile", (route) =>
    fulfillJson(route, {
      recommended_profile_id: "default",
      recommended_profile_name: "既定プロファイル",
      recommended_profile_category: "既定プロファイル",
      confidence: 0.94,
      reason: "請求関連の語彙に一致しました。",
      rewritten_question: "請求金額を一覧で見たい",
      recommended_allowed_objects: {
        table_names: ["INVOICES"],
        columns: { INVOICES: ["TOTAL_AMOUNT"] },
      },
      candidates: [
        {
          profile_id: "default",
          profile_name: "既定プロファイル",
          category: "既定プロファイル",
          score: 0.94,
          matched_terms: ["請求金額"],
          allowed_tables: ["INVOICES"],
        },
      ],
    })
  );
  await page.route("**/api/nl2sql/similar-history", (route) =>
    fulfillJson(route, {
      items: [
        {
          history_id: historyItem.id,
          question: historyItem.question,
          sql: historyItem.executable_sql || historyItem.generated_sql,
          profile_id: historyItem.profile_id,
          profile_name: historyItem.profile_name,
          score: 0.9,
          reason: "請求金額の履歴と近い質問です。管理者レビュー結果が良い履歴です。",
        },
      ],
    })
  );
  await page.route("**/api/nl2sql/analyze", (route) => {
    state.analyzePayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      safety,
      explanation: "SELECT 文として安全に実行できます。",
      recommendations: ["許可された表だけを参照しています。"],
      executable_sql: "SELECT TOTAL_AMOUNT FROM INVOICES FETCH FIRST 100 ROWS ONLY",
      repaired_sql: "",
      optimization_hints: ["TOTAL_AMOUNT の索引を確認できます。"],
    });
  });
  await page.route("**/api/nl2sql/reverse/deep", (route) => {
    state.reverseDeepPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      question: "請求金額を条件付きで一覧確認したい",
      explanation: "請求情報を対象に、一覧の取得を行います。(SQL 構造: SELECT)",
      logical_structure: "SQL 論理構造\n- SELECT: 請求金額\n- FROM: INVOICES",
      logical_structure_items: [
        {
          kind: "summary",
          business: "請求情報を対象に、一覧の取得を行います。",
          technical: "INVOICES を参照し、SELECT 操作を行います。",
        },
        { kind: "statement", business: "データを取り出すだけの参照 SQL です", technical: "SELECT" },
        { kind: "operations", business: "一覧の取得", technical: "SELECT" },
      ],
      referenced_tables: ["INVOICES"],
      logical_steps: ["INVOICES を参照", "請求金額を選択"],
      logical_step_details: [
        {
          kind: "summary",
          business: "請求情報を対象に、一覧の取得を行います。",
          technical: "INVOICES を参照",
        },
        { kind: "aggregation", business: "合計を計算します", technical: "集計: SUM" },
      ],
      source: "oci_enterprise_ai",
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/reverse", (route) => {
    state.reversePayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      question: "請求金額を一覧で確認したい",
      explanation: "INVOICES から請求金額を取得します。",
      logical_structure: "SQL 論理構造\n- SELECT: TOTAL_AMOUNT\n- FROM: INVOICES",
      referenced_tables: ["INVOICES"],
      logical_steps: ["INVOICES を参照", "TOTAL_AMOUNT を選択"],
      source: "deterministic",
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/evaluate", (route) => {
    const payload = route.request().postDataJSON() as {
      cases?: Record<string, unknown>[];
      engine?: string;
      profile_id?: string;
      evaluation_set_id?: string;
    };
    const result = {
      evaluation_suite: "deterministic_mock",
      total_cases: payload.cases?.length ?? 0,
      executable_rate: 1,
      select_only_rate: 1,
      findings: [],
    };
    evaluationRuns = [
      {
        id: "eval-run-new",
        created_at: "2026-06-21T10:06:00.000Z",
        evaluation_set_id: payload.evaluation_set_id ?? "",
        evaluation_set_name: payload.evaluation_set_id ? "請求ベンチマーク" : "",
        profile_id: payload.profile_id ?? "default",
        profile_name: "既定プロファイル",
        engine: payload.engine ?? "enterprise_ai_direct",
        cases: payload.cases ?? [],
        result,
        report: "NL2SQL deterministic evaluation\nSuite: deterministic_mock\nCases: 1",
      },
      ...evaluationRuns,
    ];
    return fulfillJson(route, result);
  });
  await page.route("**/api/nl2sql/evaluation-runs**", (route) =>
    fulfillJson(route, { items: evaluationRuns })
  );
  await page.route("**/api/nl2sql/evaluation-sets", (route) => {
    if (route.request().method() === "POST") {
      state.evaluationSetPayload = route.request().postDataJSON() as Record<string, unknown>;
      const saved = {
        id: "eval-new",
        ...(state.evaluationSetPayload as Record<string, unknown>),
        profile_name: "既定プロファイル",
        created_at: "2026-06-21T10:05:00.000Z",
        updated_at: "2026-06-21T10:05:00.000Z",
        archived: false,
      };
      evaluationSets = [saved, ...evaluationSets];
      return fulfillJson(route, saved);
    }
    return fulfillJson(route, { items: evaluationSets });
  });
  await page.route("**/api/nl2sql/evaluation-sets/eval-001", (route) => {
    if (route.request().method() === "PATCH") {
      state.evaluationSetPayload = route.request().postDataJSON() as Record<string, unknown>;
      const saved = {
        id: "eval-001",
        ...(state.evaluationSetPayload as Record<string, unknown>),
        profile_name: "既定プロファイル",
        created_at: "2026-06-21T10:00:00.000Z",
        updated_at: "2026-06-21T10:08:00.000Z",
        archived: false,
      };
      evaluationSets = [saved, ...evaluationSets.filter((item) => item.id !== "eval-001")];
      return fulfillJson(route, saved);
    }
    return fulfillJson(route, evaluationSets.find((item) => item.id === "eval-001") ?? evaluationSets[0]);
  });
  await page.route("**/api/nl2sql/evaluation-sets/eval-001/archive", (route) => {
    evaluationSets = evaluationSets.filter((item) => item.id !== "eval-001");
    return fulfillJson(route, {
      id: "eval-001",
      name: "請求ベンチマーク",
      description: "保存済み請求ケース",
      profile_id: "default",
      profile_name: "既定プロファイル",
      engine: "enterprise_ai_direct",
      cases: [],
      created_at: "2026-06-21T10:00:00.000Z",
      updated_at: "2026-06-21T10:09:00.000Z",
      archived: true,
    });
  });
  await page.route("**/api/nl2sql/comments/suggest", (route) =>
    fulfillJson(route, {
      suggestions: [
        {
          object_name: "INVOICES.TOTAL_AMOUNT",
          object_type: "COLUMN",
          suggested_comment: "税込請求金額",
        },
      ],
      source: "deterministic",
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/metadata-samples", (route) => {
    state.metadataSamplesPayload = route.request().postDataJSON() as Record<string, unknown>;
    const sampleLimit = state.metadataSamplesPayload.sample_limit;
    return fulfillJson(route, {
      sample_text: sampleLimit === 0 ? "" : "OBJECT: APP.INVOICES\nCUSTOMER_NAME: 架空商事, 見本商店",
      sample_count: sampleLimit === 0 ? 0 : 2,
      runtime: "oracle",
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/comments/generate-sql", (route) => {
    state.commentGeneratePayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      sql: "COMMENT ON COLUMN \"APP\".\"INVOICES\".\"TOTAL_AMOUNT\" IS '税込請求金額';",
      source: "deterministic",
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/nl2sql/comments/apply", (route) => {
    state.commentApplyPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      executed: false,
      runtime: "deterministic",
      statements: [
        {
          object_name: "INVOICES.TOTAL_AMOUNT",
          object_type: "column",
          comment: "税込請求金額",
          sql: "COMMENT ON COLUMN \"APP\".\"INVOICES\".\"TOTAL_AMOUNT\" IS '税込請求金額';",
          status: "requires_oracle",
          error_message: "",
        },
      ],
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/nl2sql/annotations/generate", (route) =>
    fulfillJson(route, {
      suggestions: [
        {
          object_name: "INVOICES.TOTAL_AMOUNT",
          object_type: "column",
          annotation_name: "Display",
          annotation_value: "税込請求金額",
        },
      ],
      source: "deterministic",
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/annotations/generate-sql", (route) =>
    fulfillJson(route, {
      sql: "ALTER TABLE \"APP\".\"INVOICES\" MODIFY (\"TOTAL_AMOUNT\" ANNOTATIONS (ADD IF NOT EXISTS UI_Display '税込請求金額'));",
      source: "deterministic",
      warnings: [],
      timing,
    })
  );
  await page.route("**/api/nl2sql/annotations/apply", (route) =>
    fulfillJson(route, {
      executed: false,
      runtime: "deterministic",
      statements: [
        {
          object_name: "INVOICES.TOTAL_AMOUNT",
          object_type: "column",
          annotation_name: "DISPLAY",
          annotation_value: "税込請求金額",
          sql: "ALTER TABLE \"APP\".\"INVOICES\" MODIFY \"TOTAL_AMOUNT\" ANNOTATIONS (DISPLAY '税込請求金額');",
          status: "requires_oracle",
          error_message: "",
        },
      ],
      warnings: [],
      timing,
    })
  );
  await page.route("**/api/nl2sql/domains/inventory", (route) =>
    fulfillJson(route, {
      domains: [
        {
          owner: "APP",
          name: "TOTAL_AMOUNT_D",
          qualified_name: "APP.TOTAL_AMOUNT_D",
          domain_type: "single",
          data_type: "NUMBER",
          strict: false,
          nullable: true,
          constraints: [],
          display: "",
          order: "",
          annotations: [{ name: "DESCRIPTION", value: "税込請求金額" }],
          columns: [{ owner: "APP", table_name: "INVOICES", column_name: "TOTAL_AMOUNT" }],
        },
      ],
      domain_text:
        "DOMAIN: APP.TOTAL_AMOUNT_D\nTYPE: NUMBER\nANNOTATIONS: \"DESCRIPTION\"='税込請求金額'\nCOLUMNS: APP.INVOICES.TOTAL_AMOUNT",
      runtime: "oracle",
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/domains/generate-sql", (route) =>
    fulfillJson(route, {
      sql:
        "CREATE DOMAIN IF NOT EXISTS \"APP\".\"TOTAL_AMOUNT_D\" AS NUMBER ANNOTATIONS (\"DESCRIPTION\" '税込請求金額');\n" +
        "ALTER TABLE \"APP\".\"INVOICES\" MODIFY (\"TOTAL_AMOUNT\") ADD DOMAIN \"APP\".\"TOTAL_AMOUNT_D\";",
      source: "deterministic",
      warnings: [],
      timing,
    })
  );
  await page.route("**/api/nl2sql/preview", (route) => {
    state.previewPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
      is_safe: true,
      row_limit: 0,
      note: "mock preview",
      engine: "enterprise_ai_direct",
      engine_meta: { profile: "mock_agent_profile" },
      fallback_reason: "",
      rewritten_question: "請求金額を一覧で見たい",
      executable_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
      safety,
      recommendations: ["許可された表だけを参照しています。"],
      repaired_sql: "",
      optimization_hints: ["TOTAL_AMOUNT に索引を検討できます。"],
      timing,
    });
  });
  await page.route("**/api/nl2sql/execute", (route) => {
    state.executePayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
      rows: [{ CUSTOMER_NAME: "架空商事", TOTAL_AMOUNT: 1200000 }],
      total: 1,
    });
  });
  await page.route("**/api/nl2sql/jobs", (route) => {
    state.jobPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      job_id: "job-default-001",
      status: "running",
      created_at: "2026-06-21T10:00:00.000Z",
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    });
  });
  await page.route("**/api/nl2sql/jobs/job-default-001", (route) => {
    const question = String(state.jobPayload?.question ?? "請求金額を一覧で見たい");
    const engine = String(state.jobPayload?.engine ?? "enterprise_ai_direct");
    return fulfillJson(route, {
      job_id: "job-default-001",
      ...(state.progressEvents ? { progress_events: state.progressEvents } : {}),
      status: "done",
      created_at: "2026-06-21T10:00:00.000Z",
      started_at: "2026-06-21T10:00:00.000Z",
      finished_at: "2026-06-21T10:00:00.050Z",
      elapsed_ms: 50,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "done", elapsed_ms: 20 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 12 },
        { stage: "format_results", status: "done", elapsed_ms: 6 },
      ],
      timing: null,
      result: {
        history_id: "hist-001",
        engine,
        engine_meta: {},
        fallback_reason: "",
        original_question: question,
        rewritten_question: question,
        generated_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
        executable_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
        explanation: "請求情報を取得します。",
        safety,
        recommendations: ["許可された表だけを参照しています。"],
        repaired_sql: "",
        optimization_hints: ["TOTAL_AMOUNT に索引を検討できます。"],
        results: {
          columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
          rows: [{ CUSTOMER_NAME: "架空商事", TOTAL_AMOUNT: 1200000 }],
          total: 1,
        },
        timing,
      },
    });
  });

  return state;
}

async function expectNoHorizontalScroll(page: Page) {
  const size = await page.evaluate(() => ({
    width: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    documentOverflow: document.documentElement.scrollHeight - window.innerHeight,
  }));
  expect(size.scrollWidth).toBeLessThanOrEqual(size.width + 1);
  expect(size.documentOverflow).toBeLessThanOrEqual(1);
}

async function expectMainScrollDoesNotExposeTrailingBlank(panel: Locator) {
  const metrics = await panel.evaluate((node) => {
    const main = document.querySelector<HTMLElement>("main");
    if (!main) return null;
    main.scrollTop = main.scrollHeight;
    const mainRect = main.getBoundingClientRect();
    const panelRect = node.getBoundingClientRect();
    const panelBottomInScrollContent = panelRect.bottom - mainRect.top + main.scrollTop;
    return {
      clientHeight: main.clientHeight,
      scrollHeight: main.scrollHeight,
      trailingBlank: main.scrollHeight - panelBottomInScrollContent,
    };
  });
  expect(metrics).not.toBeNull();
  if (metrics!.scrollHeight > metrics!.clientHeight + 48) {
    expect(metrics!.trailingBlank).toBeLessThanOrEqual(48);
  }
}

async function expectNoElementHorizontalOverflow(locator: Locator) {
  const size = await locator.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  expect(size.scrollWidth).toBeLessThanOrEqual(size.clientWidth + 1);
}

async function expectQuestionClamp(locator: Locator, fullText: string, lines: number) {
  await expect(locator).toHaveAttribute("title", fullText);
  await expect(locator).toHaveAttribute("aria-label", fullText);
  const metrics = await locator.evaluate((element, expectedLines) => {
    const style = window.getComputedStyle(element);
    const lineHeight = Number.parseFloat(style.lineHeight);
    const height = element.getBoundingClientRect().height;
    return {
      clamp: style.getPropertyValue("-webkit-line-clamp"),
      fontWeight: Number.parseInt(style.fontWeight, 10),
      height,
      maxHeight: Number.isFinite(lineHeight) ? lineHeight * expectedLines + 8 : null,
      overflow: style.overflow,
    };
  }, lines);
  expect(metrics.clamp).toBe(String(lines));
  expect(metrics.overflow).toBe("hidden");
  expect(metrics.fontWeight).toBeLessThan(600);
  if (metrics.maxHeight !== null) {
    expect(metrics.height).toBeLessThanOrEqual(metrics.maxHeight);
  }
}

const SPLIT_PANE_RENDER_TIMEOUT_MS = 15_000;

function expectedInformationRows(testInfo: TestInfo) {
  return testInfo.project.name === "mobile-375" ? 5 : 8;
}

async function expectInformationTableRowLimit(
  list: Locator,
  rowSelector: string,
  visibleRows: number
) {
  const fit = await list.evaluate(
    (node, { rowSelector: selector, visibleRows: limit }) => {
      const listBox = node.getBoundingClientRect();
      const rows = Array.from(node.querySelectorAll(selector)).map((row) =>
        row.getBoundingClientRect()
      );
      const header = node.querySelector("thead");
      const computed = window.getComputedStyle(node);
      const maxHeight = Number.parseFloat(computed.maxHeight);
      const rootFontSize = Number.parseFloat(
        window.getComputedStyle(document.documentElement).fontSize
      );
      const limitRow = rows[limit - 1];
      const nextRow = rows[limit];
      return {
        listHeight: listBox.height,
        maxHeight,
        rootFontSize,
        headerPosition: header ? window.getComputedStyle(header).position : "",
        limitInside: Boolean(limitRow && limitRow.bottom <= listBox.bottom + 1),
        nextBelow: Boolean(nextRow && nextRow.bottom > listBox.bottom + 1),
        scrollHeight: node.scrollHeight,
        clientHeight: node.clientHeight,
      };
    },
    { rowSelector, visibleRows }
  );

  const expectedMaxHeight = await measuredVisibleRowsHeight(list, rowSelector, visibleRows);
  expect(Math.abs(fit.maxHeight - expectedMaxHeight)).toBeLessThanOrEqual(1);
  expect(Math.abs(fit.listHeight - fit.maxHeight)).toBeLessThanOrEqual(2);
  expect(fit.limitInside).toBe(true);
  expect(fit.nextBelow).toBe(true);
  expect(fit.scrollHeight).toBeGreaterThan(fit.clientHeight);
  expect(fit.headerPosition).toBe("sticky");
}

async function expectCompactExecutionActivity(activity: Locator) {
  await expect(activity).toHaveAttribute("role", "status");
  await expect(
    activity.locator('[data-testid$="-summary"], [data-testid$="-steps"], [data-testid$="-step"]')
  ).toHaveCount(0);
  for (const label of ["実行基盤", "SQL 文", "結果行", "取得上限"]) {
    await expect(activity.getByText(label, { exact: true })).toHaveCount(0);
  }
  await expect(
    activity.getByText(/^\s*\d+\.\s+(SELECT|CREATE|INSERT|UPDATE|DELETE|ALTER|DROP|MERGE|TRUNCATE)/u)
  ).toHaveCount(0);
  await expect(activity.getByText(/処理時間\s+\d+ms/u)).toHaveCount(0);
}

async function expectHorizontallyContained(content: Locator, container: Locator) {
  const [contentBox, containerBox] = await Promise.all([content.boundingBox(), container.boundingBox()]);
  expect(contentBox).not.toBeNull();
  expect(containerBox).not.toBeNull();
  expect(contentBox!.x).toBeGreaterThanOrEqual(containerBox!.x - 1);
  expect(contentBox!.x + contentBox!.width).toBeLessThanOrEqual(containerBox!.x + containerBox!.width + 1);
}

/** 初期折りたたみのサイドナビセクションを必要に応じて展開してからリンクを開く。 */
async function openSidebarLink(page: Page, name: string | RegExp) {
  // 375px ではナビがドロワー（#367）。開いてからサイドナビの中で探す。
  const sidebar = await openSidebarNav(page);
  const link = sidebar.getByRole("link", { name }).first();
  if (!(await link.isVisible())) {
    // クリックすると「を展開」→「を折りたたむ」に変わるため、残数が 0 になるまで先頭を開く。
    const expandButtons = sidebar.getByRole("button", { name: /を展開/ });
    while ((await expandButtons.count()) > 0) {
      await expandButtons.first().click();
    }
  }
  await link.click();
}

async function useOverflowSchemaCatalog(page: Page) {
  // スキーマ参照は選択 profile の許可表でフィルタされるため、overflow 表も許可に含める。
  await page.unroute("**/api/nl2sql/profiles/*/usage-context");
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) => {
    const routeProfileId = decodeURIComponent(
      new URL(route.request().url()).pathname.split("/").at(-2) ?? ""
    );
    const profile = profiles.find((item) => item.id === routeProfileId) ?? profiles[0];
    return fulfillJson(route, {
      id: profile.id,
      name: profile.name,
      category: profile.category,
      description: profile.description,
      allowed_tables: overflowSchemaCatalog.tables.map((table) => table.table_name),
      allowed_views: [],
      archived: profile.archived,
      object_scope_version: 1,
      version: 1,
      etag: `etag-${profile.id}`,
      updated_at: "2026-08-14T00:00:00.000Z",
    });
  });
  await page.unroute("**/api/schema/catalog");
  await page.route("**/api/schema/catalog", (route) => fulfillJson(route, overflowSchemaCatalog));
  await page.unroute("**/api/schema/objects?*");
  await page.route("**/api/schema/objects?*", (route) =>
    fulfillJson(route, {
      items: overflowSchemaCatalog.tables.map((table) => ({
        owner: table.owner,
        object_name: table.table_name,
        object_type: table.table_type,
        logical_name: table.logical_name,
        comment: table.comment,
        row_count: table.row_count,
        column_count: table.columns.length,
        last_ddl_at: "",
      })),
      next_cursor: null,
      total: overflowSchemaCatalog.tables.length,
      catalog_version: 1,
    })
  );
  await page.unroute("**/api/schema/objects/*/*");
  await page.route("**/api/schema/objects/*/*", (route) => {
    const objectName = decodeURIComponent(
      new URL(route.request().url()).pathname.split("/").at(-1) ?? ""
    );
    return fulfillJson(route, {
      table:
        overflowSchemaCatalog.tables.find((table) => table.table_name === objectName) ??
        overflowSchemaCatalog.tables[0],
      dependencies: [],
      catalog_version: 1,
      etag: "schema-overflow",
    });
  });
  // スキーマ参照はプロファイルの allowed_tables で絞り込むため、レイアウト検証用の
  // 長い名前の表を表示できるよう、既定プロファイルを全表表示（allowed 空）に上書きする。
  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) =>
    fulfillJson(route, [{ ...profiles[0], allowed_tables: [], allowed_views: [] }])
  );
  await page.unroute("**/api/nl2sql/profiles/default");
  await page.route("**/api/nl2sql/profiles/default", (route) =>
    fulfillJson(route, { ...profiles[0], allowed_tables: [], allowed_views: [] })
  );
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [
        {
          id: profiles[0].id,
          name: profiles[0].name,
          category: profiles[0].category,
          description: profiles[0].description,
          archived: profiles[0].archived,
          allowed_table_count: 0,
          allowed_view_count: 0,
          glossary_count: Object.keys(profiles[0].glossary).length,
          few_shot_count: profiles[0].few_shot_examples.length,
          version: 1,
          etag: `etag-${profiles[0].id}`,
          updated_at: "2026-06-21T10:00:00.000Z",
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
}

async function openSchemaPicker(page: Page) {
  // スキーマ参照は質問の右に常時表示（トグルなし）。可視確認のみ行う。
  await expect(page.getByTestId("nl2sql-schema-reference")).toBeVisible();
}

async function expectQuerySingleColumnLayout(page: Page) {
  // 単一カラム化: 分割ペインの testid は存在しない。
  await expect(page.getByTestId("fixed-split-pane-nl2sql-workbench")).toHaveCount(0);
  const shell = page.getByTestId("nl2sql-workspace-shell");
  await expect(shell).toBeVisible();

  // スキーマ参照は質問の直下の折りたたみ補助ツール。開いて内容を検証。
  await openSchemaPicker(page);
  const schema = page.getByTestId("nl2sql-schema-reference");
  const firstTable = page.getByTestId("nl2sql-schema-table-item").first();
  await expect(schema).toBeVisible();
  await expect(firstTable).toBeVisible();

  // 長い識別子でも入力カード幅を超えず（折り返す）、横スクロールを起こさない。
  const shellBox = await shell.boundingBox();
  const tableBox = await firstTable.boundingBox();
  expect(shellBox).not.toBeNull();
  expect(tableBox).not.toBeNull();
  expect(tableBox!.x + tableBox!.width).toBeLessThanOrEqual(shellBox!.x + shellBox!.width + 1);
  await expectNoHorizontalScroll(page);
}

test("ローカルコードフォントで英日混在 SQL を表示し業務数字は UI フォントを使う", async ({ page, context, baseURL }, testInfo) => {
  const origin = new URL(baseURL!).origin;
  const external: string[] = [];
  const fonts: { url: string; ok: boolean }[] = [];
  await context.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin !== origin) {
      external.push(route.request().url());
      await route.abort();
    } else {
      await route.fallback();
    }
  });
  page.on("response", (response) => {
    if (response.request().resourceType() === "font") fonts.push({ url: response.url(), ok: response.ok() });
  });
  await mockNl2SqlApi(page);
  await page.goto("/direct-sql");
  const sql = directSqlInput(page);
  await expect(sql).toBeVisible();
  await sql.fill('SELECT CUSTOMER_NAME AS "顧客名", 1200000 AS "金額" FROM INVOICES');
  const loaded = await page.evaluate(async () => {
    const counts = [];
    for (const weight of [400, 500, 600, 700]) {
      const faces = await document.fonts.load(`${weight} 14px "Google Sans Code"`, "SELECT 1200000");
      counts.push(faces.filter((face) => face.status === "loaded").length);
    }
    await document.fonts.ready;
    return counts;
  });
  expect(loaded.every((count) => count > 0)).toBe(true);
  await expect(sql).toHaveCSS("font-family", FONT_MONO_FAMILY);
  const rowLimit = page.getByTestId("nl2sql-direct-sql").getByLabel("取得件数上限");
  await expect(rowLimit).toHaveCSS("font-family", await page.locator("body").evaluate((node) => getComputedStyle(node).fontFamily));
  const cdp = await context.newCDPSession(page);
  try {
    await cdp.send("DOM.enable");
    await cdp.send("CSS.enable");
    const { root } = await cdp.send("DOM.getDocument");
    const { nodeId } = await cdp.send("DOM.querySelector", { nodeId: root.nodeId, selector: "#direct-sql-input" });
    // textarea の文字は UA shadow root 内に描画され、textarea 自身の字体一覧は空になる。
    const { node } = await cdp.send("DOM.describeNode", { nodeId, depth: -1, pierce: true });
    const editors = (node.shadowRoots ?? []).flatMap((root) => root.children ?? [])
      .filter((child) => child.nodeName === "DIV" && !child.attributes?.includes("-webkit-input-placeholder"));
    expect(editors).toHaveLength(1);
    const { nodeIds } = await cdp.send("DOM.pushNodesByBackendIdsToFrontend", {
      backendNodeIds: editors.map((editor) => editor.backendNodeId),
    });
    const { fonts: rendered } = await cdp.send("CSS.getPlatformFontsForNode", { nodeId: nodeIds[0] });
    expect(rendered.some((font) => font.familyName.startsWith("Google Sans Code") && font.isCustomFont)).toBe(true);
    expect(rendered.some((font) => font.familyName.startsWith("Noto Sans JP") && font.isCustomFont)).toBe(true);
    expect(rendered.every((font) => font.isCustomFont)).toBe(true);
  } finally {
    await cdp.detach();
  }
  expect(fonts.some(({ url, ok }) => url.includes("google-sans-code") && ok)).toBe(true);
  expect(fonts.every(({ url, ok }) => new URL(url).origin === origin && ok)).toBe(true);
  expect(external).toEqual([]);
  await sql.focus();
  await expect(sql).toBeFocused();
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("local-code-fonts.png"), fullPage: true });
});

test("スキーマ参照の読込状態は利用者向けの日本語ラベルを表示する", async ({ page }) => {
  await mockNl2SqlApi(page);
  const schemaGate = createRequestGate();
  await page.unroute("**/api/schema/objects?*");
  await page.route("**/api/schema/objects?*", async (route) => {
    await schemaGate.promise;
    return fulfillJson(route, {
      items: schemaCatalog.tables.map((table) => ({
        owner: table.owner,
        object_name: table.table_name,
        object_type: table.table_type,
        logical_name: table.logical_name,
        comment: table.comment,
        row_count: table.row_count,
        column_count: table.columns.length,
        last_ddl_at: "",
      })),
      next_cursor: null,
      total: schemaCatalog.tables.length,
      catalog_version: 1,
    });
  });

  await page.goto("/query");

  const loading = page.getByTestId("schema-reference-loading");
  await expect(loading).toBeVisible();
  await expect(loading).toHaveAccessibleName("スキーマ情報を読み込んでいます");
  await expect(loading).toContainText("スキーマ情報を読み込んでいます");
  await expect(page.getByText("nl2sql.schema.loading", { exact: true })).toHaveCount(0);
  // 初回の読込のスピナーはスキーマ参照の読込表示の 1 つだけ（ヘッダーの「最新情報を取得」は無効にするだけ。#416）。
  await expectSingleSpinner(page, loading);

  schemaGate.release();
  await expect(loading).toBeHidden();
});

test("query workbench waits for a real profile id before profile detail and schema requests", async ({ page }) => {
  const profileId = "0f2767ed-da07-4d35-bee0-52b5a92ec694";
  const profile = {
    ...profiles[0],
    id: profileId,
    name: "部門分析プロファイル",
    allowed_tables: ["APP.DEPARTMENT"],
    allowed_views: [],
  };
  const unexpectedDefaultRequests: string[] = [];
  const schemaProfileIds: string[] = [];
  let profileDetailRequests = 0;

  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      url.pathname === "/api/nl2sql/profiles/default" ||
      url.searchParams.get("profile_id") === "default"
    ) {
      unexpectedDefaultRequests.push(request.url());
    }
  });

  // 認証・DB gate・schema refresh discovery は共通 helper で満たす(未モックだとログイン画面で止まる)。
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/persistence", (route) =>
    fulfillJson(route, { mode: "oracle", ready: true, writable: true, reason_code: "" })
  );
  await page.route("**/api/nl2sql/history**", (route) => fulfillJson(route, { items: [] }));
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfillJson(route, {
      catalog_version: 1,
      schema_fingerprint: "uuid-profile-catalog",
      refreshed_at: "2026-08-14T00:00:00.000Z",
      object_count: 1,
      column_count: 0,
      change_token: 1,
      etag: "uuid-profile-catalog",
    })
  );
  await page.route(`**/api/nl2sql/profiles/${profileId}/usage-context`, (route) => {
    profileDetailRequests += 1;
    return fulfillJson(route, {
      id: profile.id,
      name: profile.name,
      category: profile.category ?? "",
      description: profile.description,
      allowed_tables: profile.allowed_tables,
      allowed_views: profile.allowed_views,
      archived: false,
      object_scope_version: 1,
      version: 1,
      etag: `etag-${profile.id}`,
      updated_at: "2026-08-14T00:00:00.000Z",
    });
  });
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "unexpected default profile request" }),
    })
  );
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [
        {
          id: profile.id,
          name: profile.name,
          category: profile.category,
          description: profile.description,
          archived: false,
          allowed_table_count: profile.allowed_tables.length,
          allowed_view_count: profile.allowed_views.length,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: `etag-${profile.id}`,
          updated_at: "2026-08-14T00:00:00.000Z",
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route(`**/api/nl2sql/profiles/${profileId}`, (route) => fulfillJson(route, profile));
  await page.route("**/api/nl2sql/profiles/default", (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "unexpected default profile request" }),
    })
  );
  await page.route("**/api/schema/objects?*", (route) => {
    const url = new URL(route.request().url());
    schemaProfileIds.push(url.searchParams.get("profile_id") ?? "");
    return fulfillJson(route, {
      items: [
        {
          owner: "APP",
          object_name: "DEPARTMENT",
          object_type: "TABLE",
          logical_name: "部門",
          comment: "部門情報",
          row_count: null,
          column_count: 0,
          last_ddl_at: "",
        },
      ],
      next_cursor: null,
      total: 1,
      catalog_version: 1,
    });
  });

  await page.goto("/query");

  await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), profileId);
  await expect.poll(() => profileDetailRequests).toBe(1);
  await expect.poll(() => schemaProfileIds.includes(profileId)).toBe(true);
  expect(schemaProfileIds).not.toContain("");
  expect(schemaProfileIds).not.toContain("default");
  expect(unexpectedDefaultRequests).toEqual([]);
});

test("query workbench keeps the schema picker inside a single column with long identifiers", { tag: "@responsive" }, async ({ page }) => {
  await mockNl2SqlApi(page);
  await useOverflowSchemaCatalog(page);

  await page.goto("/query");
  await expect(page.getByText("スキーマ参照")).toBeVisible();
  await expectQuerySingleColumnLayout(page);

  const viewport = page.viewportSize();
  if ((viewport?.width ?? 0) >= 1280) {
    await page.setViewportSize({ width: 2048, height: 900 });
    await page.reload();
    await expect(page.getByText("スキーマ参照")).toBeVisible();
    await expectQuerySingleColumnLayout(page);
  }
});

test("SQL 系の必須入力欄は共有の必須バッジと required 属性で統一する", async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.goto("/query");
  await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), "default");
  // 業務プロファイルは空にできない選択欄で、API で省略しても backend が既定を使うので「必須」を付けない（#540）。
  await expect(page.locator('label[for="nl2sql-profile-select"]')).not.toContainText("必須");
  await expect(page.locator("#nl2sql-profile-select")).not.toHaveAttribute("aria-required", "true");
  await expectRequiredTextarea(page, "nl2sql-question-input", "質問");
  const runQueryButton = page.getByRole("button", { name: "SQL を生成して実行" });
  await expect(runQueryButton).toBeDisabled();
  await nl2sqlQuestionInput(page).fill("未入金の請求を確認したい");
  await expect(runQueryButton).toBeEnabled();

  await page.goto("/direct-sql");
  const directSql = page.getByTestId("nl2sql-direct-sql");
  await expectRequiredTextarea(directSql, "direct-sql-input", "SQL");
  const directExecuteButton = directSql.getByRole("button", { name: "SQL 実行" });
  // SQL の未入力は押せないボタンだけにせず、押したときに欄の直下へ理由を出す（#541）。
  await expect(directExecuteButton).toBeEnabled();
  await directExecuteButton.click();
  await expect(directSqlInput(directSql)).toHaveAccessibleDescription("SQL を入力してください。");
  await expect(directSqlInput(directSql)).toBeFocused();
  await directSqlInput(directSql).fill("SELECT 1 FROM DUAL");
  await expect(directSqlInput(directSql)).not.toHaveAttribute("aria-invalid", "true");
  await expect(directSql.getByRole("spinbutton", { name: "取得件数上限", exact: true })).toHaveAttribute("aria-required", "true");
  await directSql.getByLabel("取得件数上限").fill("100");
  await expect(directExecuteButton).toBeEnabled();

  await page.goto("/sql-to-question");
  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
  await expect(page.getByRole("combobox", { name: "業務プロファイル", exact: true })).not.toHaveAttribute("aria-required", "true");
  await expectRequiredTextarea(page, "sql-to-question-sql-input", "対象 SQL");
  const generateButton = page.getByRole("button", { name: "SQL 分析・質問生成" });
  await expect(generateButton).toBeEnabled();
  await generateButton.click();
  await expect(sqlToQuestionInput(page)).toHaveAccessibleDescription("対象 SQL を入力してください。");
  await expect(sqlToQuestionInput(page)).toBeFocused();
  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await expect(generateButton).toBeEnabled();

  await page.goto("/admin-sql");
  const adminSql = page.getByTestId("nl2sql-admin-sql");
  await expectRequiredTextarea(adminSql, "admin-sql-input", "管理 SQL");
  const adminExecuteButton = adminSql.getByRole("button", { name: "SQL 実行" });
  await expect(adminExecuteButton).toBeEnabled();
  await adminExecuteButton.click();
  await expect(adminSqlInput(adminSql)).toHaveAccessibleDescription("管理 SQL を入力してください。");
  await expect(adminSqlInput(adminSql)).toBeFocused();
  await adminSqlInput(adminSql).fill("SELECT 1 FROM DUAL");
  await expect(adminExecuteButton).toBeEnabled();
});

test("SQL のツールの画面の欄は値の種類の幅で、placeholder が欄に収まる（#1891）", { tag: "@responsive" }, async ({ page }) => {
  // 5 画面 × 2 つの幅 × ライト / ダークを 1 つの test で回る。
  test.setTimeout(180_000);
  await mockNl2SqlApi(page);
  const projectWidth = page.viewportSize()?.width ?? 1280;
  const height = page.viewportSize()?.height ?? 900;
  // 375px の project は 375px だけ、desktop は 1280px と 1920px（1920px で欄が段の幅を超えて伸びないこと）。
  const widths = projectWidth < 640 ? [projectWidth] : [1280, 1920];
  const rootFont = 14;
  const screens: Array<{ path: string; ready: string; open?: (page: Page) => Promise<void>; fields: Array<[string, number | "form"]> }> = [
    {
      path: "/query",
      ready: "#nl2sql-question-input",
      open: async (target) => {
        await expectSelectFieldValue(target.locator("#nl2sql-profile-select"), "default");
      },
      fields: [["#nl2sql-profile-select", 28]],
    },
    { path: "/direct-sql", ready: "#direct-sql-input", fields: [["#direct-sql-input", "form"]] },
    { path: "/admin-sql", ready: "#admin-sql-input", fields: [["#admin-sql-input", "form"]] },
    {
      path: "/sql-to-question",
      ready: "#sql-to-question-sql-input",
      // 業務プロファイルとスキーマの読み込みの後（Skeleton ではなく内容）で確かめる。
      open: async (target) => {
        await expectSelectFieldValue(target.locator("#sql-to-question-profile"), "default");
        await expect(target.getByTestId("sql-to-question-schema-skeleton")).toHaveCount(0);
      },
      fields: [["#sql-to-question-profile", 28]],
    },
    { path: "/sample-data", ready: "#sample-data-step", fields: [["#sample-data-dataset", 20], ["#sample-data-step", 12]] },
  ];
  for (const width of widths) {
    await page.setViewportSize({ width, height });
    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      for (const screen of screens) {
        const where = `${screen.path} ${width}px ${colorScheme}`;
        await page.goto(screen.path);
        await expect(page.locator(screen.ready)).toBeVisible();
        await screen.open?.(page);
        expect(await measureHorizontalOverflow(page), where).toBeLessThanOrEqual(1);
        expect(await findClippedPlaceholders(page), where).toEqual([]);
        if (width < 640) continue;
        expect(await findFieldsOffWidthScale(page), where).toEqual([]);
        for (const [selector, expected] of screen.fields) {
          const box = await page.locator(selector).boundingBox();
          expect(box, `${where} ${selector}`).not.toBeNull();
          if (expected === "form") {
            // SQL の欄は full でも FormGrid の読みやすい幅（60rem）まで（1920px でカードの幅に伸びない）。
            expect(box!.width, `${where} ${selector}`).toBeLessThanOrEqual(60 * rootFont + 1);
          } else {
            expect(Math.abs(box!.width - expected * rootFont), `${where} ${selector}`).toBeLessThanOrEqual(1);
          }
        }
      }
    }
  }
});

test("管理 SQL は SQL ファイルを読み込むと未入力のエラーを消す", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.goto("/admin-sql");
  const adminSql = page.getByTestId("nl2sql-admin-sql");
  const sqlInput = adminSqlInput(adminSql);
  await adminSql.getByRole("button", { name: "SQL 実行" }).click();
  await expect(sqlInput).toHaveAccessibleDescription("管理 SQL を入力してください。");
  await expect(sqlInput).toHaveAttribute("aria-invalid", "true");

  // 手入力と同じく、ファイルで SQL を入れたら未入力のエラーは残さない（#933）。
  await adminSql.getByLabel("SQL ファイル読込 (.sql/.txt)").setInputFiles({
    name: "select-invoices.sql",
    mimeType: "text/plain",
    buffer: Buffer.from("SELECT CUSTOMER_NAME FROM INVOICES"),
  });
  await expect(sqlInput).toHaveValue("SELECT CUSTOMER_NAME FROM INVOICES");
  await expect(sqlInput).not.toHaveAttribute("aria-invalid", "true");
  await expect(adminSql.getByText("管理 SQL を入力してください。")).toHaveCount(0);
});

test("スキーマ参照から連続挿入すると各項目が改行区切りになる", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.goto("/query");
  const question = nl2sqlQuestionInput(page);
  await openSchemaPicker(page);
  const tableToggle = page.getByRole("button", { name: "請求 を開閉" });
  const tableChevron = tableToggle.locator('svg[data-state]');
  await expect(tableChevron).toHaveAttribute("data-state", "collapsed");
  await expect(tableChevron).toHaveClass(/rotate-90/);
  await tableToggle.click();
  await expect(tableChevron).toHaveAttribute("data-state", "expanded");
  await expect(tableChevron).toHaveClass(/rotate-0/);
  const column = page.getByRole("button", { name: /^請求金額 TOTAL_AMOUNT/ });
  await column.click();
  await expect(question).toHaveValue("\"請求\".\"請求金額\"");
  // 2 回目は先頭に改行が入り、項目が行分割される
  await column.click();
  await expect(question).toHaveValue("\"請求\".\"請求金額\"\n\"請求\".\"請求金額\"");
});

test("スキーマ参照は続きがあっても業務プロファイルで絞った総数を「全 N 件」、検索中は「N 件（全 M 件）」で出す (#1891)", { tag: "@responsive" }, async ({ page }) => {
  await mockNl2SqlApi(page);
  const requests: URL[] = [];
  const invoices = {
    owner: "APP",
    object_name: "INVOICES",
    object_type: "TABLE",
    logical_name: "請求",
    comment: "請求情報",
    row_count: 2,
    column_count: 2,
    last_ddl_at: "",
  };
  await page.unroute("**/api/schema/objects?*");
  await page.route("**/api/schema/objects?*", (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    const query = url.searchParams.get("q") ?? "";
    // API は profile_id の業務プロファイルの対象で絞った件数を total に返す（続きのページは件数なし）。
    if (url.searchParams.get("cursor")) {
      return fulfillJson(route, { items: [], next_cursor: null, total: null, catalog_version: 1 });
    }
    return fulfillJson(route, {
      items: [invoices],
      next_cursor: query ? null : "page-2",
      total: query ? 1 : 120,
      catalog_version: 1,
    });
  });

  await page.goto("/query");
  await openSchemaPicker(page);
  const toolbar = page.getByTestId("nl2sql-schema-toolbar");
  // 1 ページ目だけを読んだ段階（続きがある）でも、API の総数で「全 120 件」を出す。
  await expect(toolbar.getByRole("status")).toHaveText("全 120 件");
  expect(requests.every((url) => url.searchParams.get("profile_id") === "default")).toBe(true);
  await expect(page.getByTestId("nl2sql-schema-reference").getByRole("button", { name: "さらに読み込む" })).toBeVisible();

  // 検索中は一致した数と、絞り込む前の全件（検索語の無い問い合わせの total）。
  await toolbar.getByRole("searchbox", { name: "表・項目検索" }).fill("請求");
  await expect(toolbar.getByRole("status")).toHaveText("1 件（全 120 件）");
  expect(requests.some((url) => url.searchParams.get("q") === "請求")).toBe(true);
  await expectNoHorizontalScroll(page);
});

for (const theme of ["light", "dark"]) {
  for (const wide of [false, true]) {
    test(`クエリのテンプレートは全置換し、自由入力で空に戻す: ${theme} ${wide ? "1920px" : "標準幅"}`, { tag: "@responsive" }, async ({ page, isMobile }, testInfo) => {
      test.skip(wide && isMobile, "1920px は desktop project で検証する");
      if (wide) await page.setViewportSize({ width: 1920, height: 1000 });
      await page.addInitScript(value => {
        localStorage.setItem("production-ready-nl2sql.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
      }, theme);
      await mockNl2SqlApi(page);
      let jobs = 0;
      page.on("request", (request) => {
        if (request.method() === "POST" && new URL(request.url()).pathname === "/api/nl2sql/jobs") jobs += 1;
      });
      await page.goto("/query");
      await expect(page.locator("html")).toHaveCSS("color-scheme", theme);
      const question = nl2sqlQuestionInput(page);
      const freeInput = page.getByRole("button", { name: "自由入力", exact: true });
      await expect(page.getByText("テンプレート:", { exact: true })).toBeVisible();
      await freeInput.click();
      await expect(question).toHaveValue("");

      const original = "請求金額を一覧で見たい\n  2025年の請求を対象にする  ";
      const templates = [
        ["項目抽出", "対象テーブル：\n抽出項目：\n抽出条件："],
        ["集計・グループ化", "対象テーブル：\n集計内容（件数・合計・平均など）：\n集計単位（グループ化）：\n抽出条件："],
        ["上位N件・並び替え", "対象テーブル：\n抽出項目：\n並び替え（項目と昇順／降順）：\n表示件数（上位N件）：\n抽出条件："],
        ["複数テーブル結合", "対象テーブル（複数可）：\nテーブル間の関連：\n抽出項目：\n抽出条件："],
      ];
      for (const [label, body] of templates) {
        await question.fill(original);
        // 選択範囲の有無によらず、質問の全体をテンプレートで置き換える。
        await question.focus();
        await question.press("ControlOrMeta+A");
        await page.getByRole("button", { name: label, exact: true }).click();
        await expect(question).toHaveValue(body);
        await expect(question).toBeFocused();
        const caret = await question.evaluate((el) => (el as HTMLTextAreaElement).selectionStart);
        expect(caret).toBe(body.indexOf("\n"));
        await freeInput.click();
        await expect(question).toHaveValue("");
        await expect(question).toBeFocused();
      }

      // キーボードでも全置換し、スキーマ挿入は先頭の空欄に入る。
      await question.fill(`${original}\n`);
      const basic = page.getByRole("button", { name: "項目抽出", exact: true });
      await basic.focus();
      await page.keyboard.press("Enter");
      const basicText = templates[0][1];
      await expect(question).toHaveValue(basicText);
      await openSchemaPicker(page);
      await page.getByRole("button", { name: "請求 を開閉" }).click();
      await page.getByRole("button", { name: /^請求金額 TOTAL_AMOUNT/ }).click();
      const filled = `対象テーブル："請求"."請求金額"\n抽出項目：\n抽出条件：`;
      await expect(question).toHaveValue(filled);
      await basic.click();
      await expect(question).toHaveValue(basicText);
      await page.getByRole("button", { name: "集計・グループ化", exact: true }).click();
      await expect(question).toHaveValue(templates[1][1]);
      await page.reload();
      await expect(question).toHaveValue(templates[1][1]);
      await freeInput.click();
      await expect(question).toHaveValue("");
      await page.reload();
      await expect(question).toHaveValue("");

      // 空欄と長い入力のどちらからでもテンプレートだけに置き換える。
      await question.fill("");
      await basic.click();
      await expect(question).toHaveValue(basicText);
      const longQuestion = Array.from({ length: 25 }, (_, index) => `確認したい内容 ${index + 1}`).join("\n");
      await question.fill(longQuestion);
      await basic.click();
      await expect(question).toHaveValue(basicText);
      await expect.poll(() => question.evaluate((el) => el.scrollTop)).toBe(0);
      await question.screenshot({ path: testInfo.outputPath("replaced-question-template.png") });
      await expectNoHorizontalScroll(page);
      expect(jobs).toBe(0);
    });
  }
}

test("スキーマピッカーは compact（checkbox なし・挿入でページがスクロールしない）", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.goto("/query");
  await openSchemaPicker(page);

  // checkbox は存在しない（クリック=挿入の 1 アクション）
  const picker = page.getByTestId("nl2sql-schema-reference");
  await expect(picker.locator('input[type="checkbox"]')).toHaveCount(0);

  // 検索入力で一致テーブルが自動展開され、列がそのまま見える
  await picker.getByLabel("表・項目検索").fill("請求金額");
  const column = page.getByRole("button", { name: /^請求金額 TOTAL_AMOUNT/ });
  await expect(column).toBeVisible();

  // 挿入してもページスクロールが飛ばない（focus の preventScroll）
  await column.scrollIntoViewIfNeeded();
  const before = await page.evaluate(() => window.scrollY);
  await column.click();
  await expect(nl2sqlQuestionInput(page)).toHaveValue("\"請求\".\"請求金額\"");
  const after = await page.evaluate(() => window.scrollY);
  expect(Math.abs(after - before)).toBeLessThanOrEqual(2);
});

test("スキーマ参照はアコーディオンで、表名クリックで表名を挿入できる", async ({ page }) => {
  await mockNl2SqlApi(page);
  await useOverflowSchemaCatalog(page); // 2 表（伝票活動ログ… + 請求）
  await page.goto("/query");
  await openSchemaPicker(page);

  // アコーディオン: 1 表目を開いた後に 2 表目を開くと 1 表目が自動で閉じる
  const firstToggle = page.getByRole("button", { name: /伝票活動ログ.*を開閉/ });
  const secondToggle = page.getByRole("button", { name: "請求 を開閉" });
  await firstToggle.click();
  await expect(firstToggle).toHaveAttribute("aria-expanded", "true");
  await secondToggle.click();
  await expect(secondToggle).toHaveAttribute("aria-expanded", "true");
  await expect(firstToggle).toHaveAttribute("aria-expanded", "false");

  // 展開した表の全列に到達できる（列数が catalog と一致し、最後の列も可視化できる）
  const columns = page
    .getByTestId("nl2sql-schema-table-item")
    .filter({ has: secondToggle })
    .getByRole("listitem");
  await expect(columns).toHaveCount(2); // 取引先名 + 請求金額
  const lastColumn = page.getByRole("button", { name: /^請求金額 TOTAL_AMOUNT/ });
  await lastColumn.scrollIntoViewIfNeeded();
  await expect(lastColumn).toBeVisible();

  // 表名クリック=表名（論理名）を挿入。chevron クリックでは挿入されない。
  const question = nl2sqlQuestionInput(page);
  await expect(question).toHaveValue("");
  // 表名は所有者付きの修飾名で表示し、所有者を別バッジで重ねない（#555）。
  await expect(
    page.getByTestId("nl2sql-schema-table-item").filter({ has: secondToggle }).getByTestId("nl2sql-schema-table-qualified-name")
  ).toHaveText("APP.INVOICES");
  await page.getByRole("button", { name: /^請求 APP\.INVOICES/ }).click();
  await expect(question).toHaveValue("\"請求\"");
  await secondToggle.click();
  await expect(question).toHaveValue("\"請求\""); // chevron では変化しない
});

test("クエリとスキーマ参照は desktop で左右並置、mobile で縦積みになる", { tag: "@responsive" }, async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.goto("/query");
  const question = nl2sqlQuestionInput(page);
  const picker = page.getByTestId("nl2sql-schema-reference");
  await expect(question).toBeVisible();
  await expect(question).toHaveAttribute("placeholder", "質問を入力");
  await expect(question).toHaveAccessibleName("質問");
  await expect(question).toHaveValue("");
  await expect(picker).toBeVisible();

  const questionBox = await question.boundingBox();
  const pickerBox = await picker.boundingBox();
  expect(questionBox).not.toBeNull();
  expect(pickerBox).not.toBeNull();

  const viewport = page.viewportSize();
  if ((viewport?.width ?? 0) >= 1024) {
    // 左右並置: ピッカーが textarea の右にあり、縦方向が重なる
    expect(pickerBox!.x).toBeGreaterThan(questionBox!.x + questionBox!.width - 1);
    expect(pickerBox!.y).toBeLessThan(questionBox!.y + questionBox!.height);
  } else {
    // 縦積み: ピッカーが textarea の下
    expect(pickerBox!.y).toBeGreaterThan(questionBox!.y);
  }
  await expectNoHorizontalScroll(page);
  await question.screenshot({ path: test.info().outputPath("query-guidance.png") });
});

test("クエリは内容に応じて最大10行まで自動拡張し、挿入行へ内部スクロールする", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.goto("/query");
  const question = nl2sqlQuestionInput(page);

  // 少量入力では既定高（5 行相当 = min-h-36 = 144px）付近
  await question.fill("1行だけ");
  const smallHeight = await question.evaluate((el) => el.clientHeight);
  expect(smallHeight).toBeLessThanOrEqual(160);

  // 12 行入力 → 10 行相当上限（max-h 264px）で止まり、内部スクロールが発生
  await question.fill(Array.from({ length: 12 }, (_, i) => `行 ${i + 1}`).join("\n"));
  const grown = await question.evaluate((el) => ({
    clientHeight: el.clientHeight,
    scrollHeight: el.scrollHeight,
  }));
  expect(grown.clientHeight).toBeLessThanOrEqual(270);
  // field-sizing 対応ブラウザでは自動拡張し、内容が上限を超えるので内部スクロールになる
  expect(grown.scrollHeight).toBeGreaterThan(grown.clientHeight);

  // スキーマ挿入すると、textarea 内部がいま挿入した行（末尾）まで追従する
  await openSchemaPicker(page);
  await page.getByRole("button", { name: "請求 を開閉" }).click();
  await page.getByRole("button", { name: /^請求金額 TOTAL_AMOUNT/ }).click();
  await expect(question).toHaveValue(/請求金額"$/);
  const scrollTop = await question.evaluate((el) => el.scrollTop);
  expect(scrollTop).toBeGreaterThan(0);
});

test("標準プロファイル削除後は最初の利用可能なプロファイルを選択する", async ({ page }) => {
  await mockNl2SqlApi(page);
  const alternateProfile = {
    ...profiles[0],
    id: "alternate",
    name: "代替プロファイル",
    category: "代替",
  };
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [
        {
          id: alternateProfile.id,
          name: alternateProfile.name,
          category: alternateProfile.category,
          description: alternateProfile.description,
          archived: false,
          allowed_table_count: alternateProfile.allowed_tables.length,
          allowed_view_count: alternateProfile.allowed_views.length,
          glossary_count: Object.keys(alternateProfile.glossary).length,
          few_shot_count: alternateProfile.few_shot_examples.length,
          version: 1,
          etag: "etag-alternate",
          updated_at: "2026-06-21T10:00:00.000Z",
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 2,
    })
  );
  await page.unroute("**/api/nl2sql/profiles/default");
  await page.route("**/api/nl2sql/profiles/default", (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "指定された業務プロファイルが見つかりません。" }),
    })
  );
  await page.route("**/api/nl2sql/profiles/alternate", (route) =>
    fulfillJson(route, alternateProfile)
  );

  await page.goto("/query");

  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル" }), 
    "alternate"
  );
  await expect(page.getByText("業務プロファイルがありません")).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("最後のプロファイル削除後は作成案内を表示して検索操作を無効化する", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [],
      next_cursor: null,
      total: 0,
      change_token: 2,
    })
  );
  await page.unroute("**/api/nl2sql/profiles/default");
  await page.route("**/api/nl2sql/profiles/default", (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "指定された業務プロファイルが見つかりません。" }),
    })
  );

  await page.goto("/query");
  await expect(page.getByText("業務プロファイルがありません")).toBeVisible();
  await expect(
    page.getByText("SQL を生成して実行するには、先に業務プロファイルを作成してください。")
  ).toBeVisible();

  const workspace = page.getByTestId("nl2sql-workspace-shell");
  await nl2sqlQuestionInput(page).fill("未入金の請求を確認したい");
  await expect(
    workspace.getByRole("button", { name: "プロファイルを自動判定" })
  ).toBeDisabled();
  await expect(workspace.getByRole("button", { name: "SQL プレビュー" })).toHaveCount(0);
  await expect(workspace.getByRole("button", { name: "質問を解釈" })).toHaveCount(0);
  await expect(workspace.getByRole("button", { name: "SQL を生成して実行" })).toBeDisabled();
  // 業務プロファイルが無い間、スキーマ参照は読み込み中にせず未選択の案内を出す（#3413）。
  const schema = page.getByTestId("nl2sql-schema-reference");
  await expect(schema.getByTestId("nl2sql-schema-profile-unselected")).toContainText(
    "業務プロファイルを選ぶと、使える表と列を表示します。"
  );
  await expect(schema.getByTestId("schema-reference-loading")).toHaveCount(0);
  await expect(schema.getByRole("searchbox", { name: "表・項目検索" })).toBeDisabled();
  await expect(schema.getByText("スキーマ未取得です", { exact: false })).toHaveCount(0);

  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoHorizontalScroll(page);
  await expect(schema.getByTestId("nl2sql-schema-profile-unselected")).toBeVisible();
  await page.getByRole("button", { name: "業務プロファイルを作成" }).click();
  await expect(page).toHaveURL(/\/profiles\?profile=new$/);
});

test("質問から業務プロファイルを自動判定して選択できる", async ({ page }) => {
  await mockNl2SqlApi(page);
  const paymentProfile = {
    ...profiles[0],
    id: "payment",
    name: "入金管理",
    category: "入金管理",
    allowed_tables: ["INVOICES"],
    allowed_views: [],
  };
  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) =>
    fulfillJson(route, [profiles[0], paymentProfile])
  );
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [profiles[0]].map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: false,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: Object.keys(profile.glossary).length,
        few_shot_count: profile.few_shot_examples.length,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-06-21T10:00:00.000Z",
      })),
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.unroute("**/api/nl2sql/profiles/*/usage-context");
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) => {
    const profileId = decodeURIComponent(
      new URL(route.request().url()).pathname.split("/").at(-2) ?? ""
    );
    const profile = profileId === "payment" ? paymentProfile : profiles[0];
    return fulfillJson(route, {
      id: profile.id,
      name: profile.name,
      category: profile.category,
      description: profile.description,
      allowed_tables: profile.allowed_tables,
      allowed_views: profile.allowed_views,
      archived: profile.archived,
      object_scope_version: profileId === "payment" ? 2 : 1,
      version: 1,
      etag: `etag-${profile.id}`,
      updated_at: "2026-06-21T10:00:00.000Z",
    });
  });
  await page.route("**/api/nl2sql/profiles/payment", (route) => fulfillJson(route, paymentProfile));
  let recommendRequests = 0;
  await page.unroute("**/api/nl2sql/recommend-profile");
  await page.route("**/api/nl2sql/recommend-profile", (route) => {
    recommendRequests += 1;
    return fulfillJson(route, {
      recommended_profile_id: "payment",
      recommended_profile_name: "入金管理",
      recommended_profile_category: "入金管理",
      confidence: 0.82,
      reason: "入金関連の語彙に一致しました。",
      rewritten_question: "",
      recommended_allowed_objects: { table_names: ["INVOICES"], columns: {} },
      candidates: [],
      recommendation_source: "classifier",
    });
  });

  await page.goto("/query");
  const profileSelect = page.locator("#nl2sql-profile-select");
  await expectSelectFieldValue(profileSelect, "default");

  const detect = page.getByRole("button", { name: "プロファイルを自動判定" });
  await expect(detect).toBeDisabled(); // 質問未入力では押せない
  await nl2sqlQuestionInput(page).fill("未入金の請求を確認したい");
  await detect.click();

  await expectSelectFieldValue(profileSelect, "payment");
  await expect(profileSelect).toHaveText("入金管理（入金管理）");
  await expect(page.getByText(/^選択中の表/)).toHaveCount(0);
  await expect(page.getByText(/^参照可能な表/)).toHaveCount(0);
  await expect(page.getByText(/入金管理（入金管理） を選択しました/)).toBeVisible();
  await page.waitForTimeout(700);
  expect(recommendRequests).toBe(1);
});

test("業務プロファイル自動判定が現在の profile と同じなら selection を変更しない", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/recommend-profile");
  await page.route("**/api/nl2sql/recommend-profile", (route) =>
    fulfillJson(route, {
      recommended_profile_id: "default",
      recommended_profile_name: "既定プロファイル",
      recommended_profile_category: "既定プロファイル",
      confidence: 0.91,
      reason: "請求関連の語彙に一致しました。",
      rewritten_question: "",
      recommended_allowed_objects: { table_names: ["INVOICES"], columns: {} },
      candidates: [],
      recommendation_source: "deterministic",
    })
  );

  await page.goto("/query");
  await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), "default");
  await expect(page.getByText(/^選択中の表/)).toHaveCount(0);

  await nl2sqlQuestionInput(page).fill("請求金額を一覧で確認したい");
  await page.getByRole("button", { name: "プロファイルを自動判定" }).click();

  await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), "default");
  await expect(page.getByText(/^選択中の表/)).toHaveCount(0);
  await expect(page.getByText(/既定プロファイル（既定プロファイル） は既に選択されています/)).toBeVisible();
  await expect(page.getByText(/既定プロファイル（既定プロファイル） を選択しました/)).toHaveCount(0);
});

test("推薦適用後に手動で profile を切り替えると古い選択表を送信しない", async ({ page }) => {
  const api = await mockNl2SqlApi(page);
  const hrProfile = {
    ...profiles[0],
    id: "hr",
    name: "人事プロファイル",
    category: "人事",
    allowed_tables: ["APP.EMPLOYEE"],
    allowed_views: [],
  };
  const projectProfile = {
    ...profiles[0],
    id: "pm",
    name: "プロジェクト管理",
    category: "案件",
    allowed_tables: ["APP.PROJECT"],
    allowed_views: [],
  };
  const customProfiles = [profiles[0], hrProfile, projectProfile];
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: customProfiles.map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: false,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: Object.keys(profile.glossary).length,
        few_shot_count: profile.few_shot_examples.length,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-06-21T10:00:00.000Z",
      })),
      next_cursor: null,
      total: customProfiles.length,
      change_token: 1,
    })
  );
  await page.unroute("**/api/nl2sql/profiles/*/usage-context");
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) => {
    const profileId = decodeURIComponent(
      new URL(route.request().url()).pathname.split("/").at(-2) ?? ""
    );
    const profile = customProfiles.find((item) => item.id === profileId) ?? profiles[0];
    return fulfillJson(route, {
      id: profile.id,
      name: profile.name,
      category: profile.category,
      description: profile.description,
      allowed_tables: profile.allowed_tables,
      allowed_views: profile.allowed_views,
      archived: profile.archived,
      object_scope_version: 2,
      version: 1,
      etag: `etag-${profile.id}`,
      updated_at: "2026-06-21T10:00:00.000Z",
    });
  });
  await page.unroute("**/api/nl2sql/recommend-profile");
  await page.route("**/api/nl2sql/recommend-profile", (route) =>
    fulfillJson(route, {
      recommended_profile_id: "hr",
      recommended_profile_name: "人事プロファイル",
      recommended_profile_category: "人事",
      confidence: 0.84,
      reason: "従業員情報に一致しました。",
      rewritten_question: "",
      recommended_allowed_objects: { table_names: ["APP.EMPLOYEE"], columns: {} },
      candidates: [],
      recommendation_source: "deterministic",
    })
  );

  await page.goto("/query");
  const profileSelect = page.locator("#nl2sql-profile-select");
  await nl2sqlQuestionInput(page).fill("従業員情報の一覧");
  await page.getByRole("button", { name: "プロファイルを自動判定" }).click();
  await expectSelectFieldValue(profileSelect, "hr");
  await expect(page.getByText(/^選択中の表/)).toHaveCount(0);

  await chooseSelectFieldOption(profileSelect, "pm");
  await expectSelectFieldValue(profileSelect, "pm");
  await expect(page.getByText(/^選択中の表/)).toHaveCount(0);
  await nl2sqlQuestionInput(page).fill("プロジェクト情報の一覧");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect.poll(() => api.jobPayload?.profile_id).toBe("pm");
  expect(api.jobPayload?.allowed_objects).toEqual({ table_names: [], columns: {} });
});

test("低信頼度の業務プロファイル自動判定は選択を変更しない", async ({ page }) => {
  await mockNl2SqlApi(page);
  const paymentProfile = {
    ...profiles[0],
    id: "payment",
    name: "入金管理",
    category: "入金管理",
    allowed_tables: ["INVOICES"],
    allowed_views: [],
  };
  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) =>
    fulfillJson(route, [profiles[0], paymentProfile])
  );
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [profiles[0], paymentProfile].map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: false,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: Object.keys(profile.glossary).length,
        few_shot_count: profile.few_shot_examples.length,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-06-21T10:00:00.000Z",
      })),
      next_cursor: null,
      total: 2,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/payment", (route) => fulfillJson(route, paymentProfile));
  await page.unroute("**/api/nl2sql/recommend-profile");
  await page.route("**/api/nl2sql/recommend-profile", (route) =>
    fulfillJson(route, {
      recommended_profile_id: "payment",
      recommended_profile_name: "入金管理",
      recommended_profile_category: "入金管理",
      confidence: 0.2,
      reason: "入金関連の語彙に少し一致しました。",
      rewritten_question: "",
      recommended_allowed_objects: { table_names: ["INVOICES"], columns: {} },
      candidates: [],
      recommendation_source: "deterministic",
    })
  );

  await page.goto("/query");
  const profileSelect = page.locator("#nl2sql-profile-select");
  await expectSelectFieldValue(profileSelect, "default");

  const detect = page.getByRole("button", { name: "プロファイルを自動判定" });
  await nl2sqlQuestionInput(page).fill("未入金の請求を確認したい");
  await expect(detect).toBeEnabled();
  await detect.click();

  await expectSelectFieldValue(profileSelect, "default");
  await expect(page.getByTestId("nl2sql-recommend-low-confidence")).toContainText(
    "十分な信頼度で自動判定できませんでした"
  );
  // 固定面の warning Banner に一本化(同文の Toast と二重表示しない: messaging spec §0.6)。
  await expect(page.getByText(/十分な信頼度で自動判定できませんでした/)).toHaveCount(1);
  await expect(
    page.getByRole("region", { name: "通知" }).getByRole("status")
  ).toHaveCount(0);
  await expect(page.getByText(/入金管理（入金管理） を選択しました/)).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

for (const statusCode of [404, 410, 501, 503]) {
  test(`schema 更新の開始失敗(${statusCode})は旧同期 API へ fallback せず固定面に表示する`, async ({
    page,
  }) => {
    await mockNl2SqlApi(page);
    const refreshError = "DB 構造の再取得を開始できませんでした。";
    let legacyRefreshRequests = 0;
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (request.method() === "POST" && url.pathname === "/api/schema/refresh") {
        legacyRefreshRequests += 1;
      }
    });
    await page.route("**/api/schema/refresh", (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "旧同期 API は呼び出されない想定です。" }),
      })
    );
    await page.route("**/api/schema/refresh-jobs", (route) =>
      route.fulfill({
        status: statusCode,
        contentType: "application/json",
        body: JSON.stringify({ detail: refreshError }),
      })
    );

    await page.goto("/query");
    await expect(page.getByRole("region", { name: "SQL 生成ワークスペース" })).toBeVisible();
    // mobile 幅ではヘッダー操作が「その他の操作」メニューへ収納される。
    const refreshButton = page.getByRole("button", { name: "DB 構造を再取得" });
    if (await refreshButton.isVisible()) {
      await refreshButton.click();
    } else {
      await page.getByRole("button", { name: "その他の操作" }).click();
      await page.getByRole("menuitem", { name: "DB 構造を再取得", exact: true }).click();
    }

    // 固定面(PageNotice)が正本。永続 Toast との二重表示はしない(messaging spec §0.6)。
    await expect(page.getByText(refreshError)).toHaveCount(1);
    await expect(
      page.getByRole("region", { name: "通知" }).getByRole("status")
    ).toHaveCount(0);
    expect(legacyRefreshRequests).toBe(0);
  });
}

test("catalog 空のときスキーマ参照からスキーマを更新して表を取得できる", async ({ page }) => {
  await mockNl2SqlApi(page);
  // 初回 GET は空、更新 job 完了後に実表を返す。
  let refreshed = false;
  await page.unroute("**/api/schema/catalog");
  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(
      route,
      refreshed ? schemaCatalog : { refreshed_at: "2026-06-21T10:00:00.000Z", tables: [] }
    )
  );
  await page.unroute("**/api/schema/objects?*");
  await page.route("**/api/schema/objects?*", (route) => {
    const tables = refreshed ? schemaCatalog.tables : [];
    return fulfillJson(route, {
      items: tables.map((table) => ({
        owner: table.owner,
        object_name: table.table_name,
        object_type: table.table_type,
        logical_name: table.logical_name,
        comment: table.comment,
        row_count: table.row_count,
        column_count: table.columns.length,
        last_ddl_at: "",
      })),
      next_cursor: null,
      total: tables.length,
      catalog_version: refreshed ? 2 : 1,
    });
  });
  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) =>
    fulfillJson(route, [{ ...profiles[0], allowed_tables: [], allowed_views: [] }])
  );
  await page.route("**/api/schema/refresh-jobs", (route) => {
    refreshed = true;
    return fulfillJson(route, {
      job_id: "schema-refresh-test",
      status: "done",
      created_at: "2026-06-21T10:00:00.000Z",
      scanned_objects: schemaCatalog.tables.length,
      changed_objects: schemaCatalog.tables.length,
      deleted_objects: 0,
      catalog_version: 2,
      error_code: "",
    });
  });
  await page.route("**/api/schema/refresh-jobs/schema-refresh-test", (route) =>
    fulfillJson(route, {
      job_id: "schema-refresh-test",
      status: "done",
      created_at: "2026-06-21T10:00:00.000Z",
      scanned_objects: schemaCatalog.tables.length,
      changed_objects: schemaCatalog.tables.length,
      deleted_objects: 0,
      catalog_version: 2,
      error_code: "",
    })
  );

  await page.goto("/query");
  await openSchemaPicker(page);
  await expect(page.getByText(/スキーマ未取得/)).toBeVisible();

  await page.getByRole("button", { name: "スキーマを更新" }).click();
  await expect(page.getByRole("button", { name: "請求 を開閉" })).toBeVisible();
  expect(refreshed).toBe(true);
});

test("スキーマの更新は押したボタンだけが送信の間回り、job の間は進行の表示だけがスピナーを出す (#821)", async ({
  page,
}) => {
  await mockNl2SqlApi(page);
  // job の完了まで catalog は空のまま（ヘッダーの「DB 構造を再取得」とスキーマ参照の「スキーマを更新」が両方出る）。
  let catalogFilled = false;
  await page.unroute("**/api/schema/catalog");
  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(
      route,
      catalogFilled ? schemaCatalog : { refreshed_at: "2026-06-21T10:00:00.000Z", tables: [] }
    )
  );
  await page.unroute("**/api/schema/objects?*");
  await page.route("**/api/schema/objects?*", (route) => {
    const tables = catalogFilled ? schemaCatalog.tables : [];
    return fulfillJson(route, {
      items: tables.map((table) => ({
        owner: table.owner,
        object_name: table.table_name,
        object_type: table.table_type,
        logical_name: table.logical_name,
        comment: table.comment,
        row_count: table.row_count,
        column_count: table.columns.length,
        last_ddl_at: "",
      })),
      next_cursor: null,
      total: tables.length,
      catalog_version: catalogFilled ? 2 : 1,
    });
  });
  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) =>
    fulfillJson(route, [{ ...profiles[0], allowed_tables: [], allowed_views: [] }])
  );
  const jobBody = (jobId: string, status: "running" | "done") => ({
    job_id: jobId,
    status,
    mode: "full",
    source: "manual",
    target_objects: [],
    requires_full_refresh: false,
    phase: status === "done" ? "done" : "fetching",
    created_at: "2026-06-21T10:00:00.000Z",
    started_at: "2026-06-21T10:00:01.000Z",
    scanned_objects: status === "done" ? schemaCatalog.tables.length : 0,
    changed_objects: status === "done" ? schemaCatalog.tables.length : 0,
    deleted_objects: 0,
    catalog_version: status === "done" ? 2 : null,
    error_code: "",
  });
  // 送信（POST）と job の完了を、テストの側で順に進める。
  let submitted = 0;
  let submitGate = createRequestGate();
  const finishedJobs = new Set<string>();
  await page.route("**/api/schema/refresh-jobs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    submitted += 1;
    const jobId = `schema-refresh-821-${submitted}`;
    await submitGate.promise;
    await fulfillJson(route, jobBody(jobId, "running"));
  });
  await page.route("**/api/schema/refresh-jobs/schema-refresh-821-*", (route) => {
    const jobId = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    return fulfillJson(route, jobBody(jobId, finishedJobs.has(jobId) ? "done" : "running"));
  });

  await page.goto("/query");
  await openSchemaPicker(page);
  const schemaPanel = page.getByTestId("nl2sql-schema-reference");
  await expect(schemaPanel.getByText(/スキーマ未取得/)).toBeVisible();
  const headerButton = page
    .locator("[data-page-header]")
    .getByRole("button", { name: "DB 構造を再取得", exact: true });
  const panelButton = schemaPanel.getByRole("button", { name: "スキーマを更新", exact: true });
  const processing = page.getByTestId("schema-reference-refreshing");
  await expect(headerButton).toBeVisible();
  await expect(panelButton).toBeVisible();

  const expectJobPhase = async () => {
    // job の間はどちらのボタンも回さず無効にし、スピナーは進行の表示の 1 つだけ。
    await expect(processing).toContainText("DB 構造を再取得しています");
    for (const button of [headerButton, panelButton]) {
      await expect(button).toBeDisabled();
      await expect(button).not.toHaveAttribute("aria-busy", "true");
    }
    await expectSingleSpinner(page, processing);
  };

  // 1) ページヘッダーで押す: 送信の間はヘッダーのボタンだけが回り、スキーマ参照のボタンは無効にするだけ。
  await headerButton.click();
  await expect.poll(() => submitted).toBe(1);
  await expect(headerButton).toHaveAttribute("aria-busy", "true");
  await expect(panelButton).toBeDisabled();
  await expect(panelButton).not.toHaveAttribute("aria-busy", "true");
  await expectSingleSpinner(page, headerButton);
  submitGate.release();
  await expectJobPhase();
  finishedJobs.add("schema-refresh-821-1");
  await expect(processing).toHaveCount(0);
  await expect(headerButton).toBeEnabled();
  await expect(panelButton).toBeEnabled();
  await expect(page.locator("svg.animate-spin:visible")).toHaveCount(0);

  // 2) スキーマ参照でキーボードで押す: 送信の間はそのボタンだけが回り、フォーカスはボタンに残る（#355）。
  submitGate = createRequestGate();
  await panelButton.focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => submitted).toBe(2);
  await expect(panelButton).toHaveAttribute("aria-busy", "true");
  await expect(panelButton).toBeFocused();
  await expect(headerButton).toBeDisabled();
  await expect(headerButton).not.toHaveAttribute("aria-busy", "true");
  await expectSingleSpinner(page, panelButton);
  submitGate.release();
  await expectJobPhase();
  catalogFilled = true;
  finishedJobs.add("schema-refresh-821-2");
  await expect(page.getByRole("button", { name: "請求 を開閉" })).toBeVisible();
  await expect(processing).toHaveCount(0);
  await expect(headerButton).toBeEnabled();
  await expectNoHorizontalScroll(page);
});

test("別の画面で始めたスキーマの更新は、どの更新のボタンも回さず進行の表示がスピナーを出す (#821)", async ({
  page,
}) => {
  await mockNl2SqlApi(page);
  let finished = false;
  const activeJob = () => ({
    job_id: "schema-refresh-821-elsewhere",
    status: finished ? "done" : "running",
    mode: "full",
    source: "manual",
    target_objects: [],
    requires_full_refresh: false,
    phase: finished ? "done" : "fetching",
    created_at: "2026-06-21T10:00:00.000Z",
    started_at: "2026-06-21T10:00:01.000Z",
    scanned_objects: 0,
    changed_objects: 0,
    deleted_objects: 0,
    catalog_version: null,
    error_code: "",
  });
  await page.unroute("**/api/schema/refresh-jobs/active");
  await page.route("**/api/schema/refresh-jobs/active", (route) =>
    fulfillJson(route, { active_job: finished ? null : activeJob() })
  );
  await page.route("**/api/schema/refresh-jobs/schema-refresh-821-elsewhere", (route) =>
    fulfillJson(route, activeJob())
  );

  await page.goto("/query");
  const headerButton = page
    .locator("[data-page-header]")
    .getByRole("button", { name: "DB 構造を再取得", exact: true });
  const processing = page.getByTestId("schema-reference-refreshing");
  await expect(processing).toContainText("DB 構造を再取得しています");
  await expect(headerButton).toBeDisabled();
  await expect(headerButton).not.toHaveAttribute("aria-busy", "true");
  await expectSingleSpinner(page, processing);
  finished = true;
  await expect(processing).toHaveCount(0);
  await expect(headerButton).toBeEnabled();
});

test("owner 付きの許可表でもスキーマ参照が対象表に絞り込める", async ({ page }) => {
  await mockNl2SqlApi(page);
  // allowed_tables に owner 修飾（APP.INVOICES）が入っていても INVOICES にスコープされる
  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) =>
    fulfillJson(route, [{ ...profiles[0], allowed_tables: ["APP.INVOICES"], allowed_views: [] }])
  );

  await page.goto("/query");
  await openSchemaPicker(page);
  await expect(page.getByRole("button", { name: "請求 を開閉" })).toBeVisible();
});

test("引用名の許可表ではスキーマ参照を大文字の同名表まで広げない (#561, #563)", async ({ page }) => {
  await mockNl2SqlApi(page);
  const baseTable = schemaCatalog.tables[0];
  const quotedCatalog = {
    ...schemaCatalog,
    tables: [
      { ...baseTable, table_name: "MIXED_CASE", logical_name: "大文字の請求" },
      { ...baseTable, table_name: "Mixed_Case", logical_name: "引用名の請求" },
    ],
  };
  const quotedProfile = { ...profiles[0], allowed_tables: ['APP."Mixed_Case"'], allowed_views: [] };
  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) => fulfillJson(route, [quotedProfile]));
  await page.unroute("**/api/nl2sql/profiles/default");
  await page.route("**/api/nl2sql/profiles/default", (route) =>
    fulfillJson(route, { ...quotedProfile, etag: "etag-default" })
  );
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    fulfillJson(route, {
      id: "default",
      name: quotedProfile.name,
      category: quotedProfile.category,
      description: quotedProfile.description,
      allowed_tables: quotedProfile.allowed_tables,
      allowed_views: [],
      archived: false,
      object_scope_version: 2,
      version: 1,
      etag: "etag-default",
      updated_at: "2026-06-21T10:00:00.000Z",
    })
  );
  await page.unroute("**/api/schema/catalog");
  await page.route("**/api/schema/catalog", (route) => fulfillJson(route, quotedCatalog));
  await page.unroute("**/api/schema/objects?*");
  await page.route("**/api/schema/objects?*", (route) =>
    fulfillJson(route, {
      items: quotedCatalog.tables.map((table) => ({
        owner: table.owner,
        object_name: table.table_name,
        object_type: table.table_type,
        logical_name: table.logical_name,
        comment: table.comment,
        row_count: table.row_count,
        column_count: table.columns.length,
        last_ddl_at: "",
      })),
      next_cursor: null,
      total: quotedCatalog.tables.length,
      catalog_version: 1,
    })
  );

  const detailPaths: string[] = [];
  await page.unroute("**/api/schema/objects/*/*");
  await page.route("**/api/schema/objects/*/*", (route) => {
    const parts = new URL(route.request().url()).pathname.split("/");
    const rawName = decodeURIComponent(parts.at(-1) ?? "");
    detailPaths.push(rawName);
    // backend と同じく path は Oracle の引用規則で解釈する（#563）。
    const objectName = /^".*"$/u.test(rawName) ? rawName.slice(1, -1) : rawName.toUpperCase();
    const table = quotedCatalog.tables.find((item) => item.table_name === objectName);
    return table
      ? fulfillJson(route, { table, dependencies: [], catalog_version: 1, etag: "schema-mock" })
      : route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });

  await page.goto("/query");
  await openSchemaPicker(page);
  await expect(page.getByRole("button", { name: "引用名の請求 を開閉" })).toBeVisible();
  await expect(page.getByRole("button", { name: "大文字の請求 を開閉" })).toHaveCount(0);
  // 検索で列詳細を読み込んでも、大文字の同名表の定義で置き換えない。
  await page.getByRole("searchbox", { name: "表・項目検索" }).fill("請求");
  await expect(page.getByRole("button", { name: "引用名の請求 を開閉" })).toBeVisible();
  await expect(page.getByRole("button", { name: "大文字の請求 を開閉" })).toHaveCount(0);
  // 引用名の表の列詳細は token で要求する。引用なしの `Mixed_Case` は backend が `MIXED_CASE` と
  // 解釈するため送らない（#563）。
  await expect.poll(() => detailPaths).toContain('"Mixed_Case"');
  expect(detailPaths).not.toContain("Mixed_Case");
});

test("query workbench generates SQL through the job flow and shows results", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);

  await page.goto("/query");
  await expect(page.getByText("スキーマ参照")).toBeVisible();
  await expect(page.getByRole("region", { name: "SQL 生成ワークスペース" })).toBeVisible();
  await expect(page.getByRole("tab")).toHaveCount(0);

  const removedSamples = [
    "登録済みの表から主要な列を一覧して",
    "社員と部署の一覧を確認したい",
    "部署別のプロジェクト数を集計して",
  ];
  for (const sample of removedSamples) {
    await expect(page.getByRole("button", { name: sample, exact: true })).toHaveCount(0);
  }

  const question = nl2sqlQuestionInput(page);
  const placeholder = (await question.getAttribute("placeholder")) ?? "";
  for (const sample of removedSamples) expect(placeholder).not.toContain(sample);
  await openSchemaPicker(page);
  await page.getByRole("button", { name: "請求 を開閉" }).click();
  await page.getByRole("button", { name: /^請求金額 TOTAL_AMOUNT/ }).click();
  await expect(question).toHaveValue("\"請求\".\"請求金額\"");

  await question.fill("請求金額を一覧で見たい");
  // 推薦は現在の profile（default）と同一のため、progressive-disclosure によりヒントは出さない。
  await expect(page.getByTestId("nl2sql-recommend-hint")).toHaveCount(0);

  const generateAndExecuteButton = page.getByRole("button", { name: "SQL を生成して実行", exact: true });
  await expectLargeActionButton(generateAndExecuteButton);
  await generateAndExecuteButton.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("generate-and-execute-button.png") });
  await generateAndExecuteButton.focus();
  await page.keyboard.press("Enter");

  const generatedSqlStep = page.getByTestId("nl2sql-generated-sql");
  await expect(generatedSqlStep.getByRole("code")).toContainText(
    "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
  );

  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "1200000" })).toBeVisible();
  // 生成 SQL の pre(role=region「生成 SQL コード」)と区別するため textbox を明示する。
  const feedbackResponse = page.getByRole("textbox", { name: "生成 SQL" });
  await expect(page.getByRole("heading", { name: "アプリ内フィードバック" })).toBeVisible();
  await expect(
    page.getByText(
      "この SQL 生成結果に対する利用者からの評価をアプリ内 DB に保存します。参考履歴には管理者レビュー結果が良い履歴だけを使用します。"
    )
  ).toBeVisible();
  await expect(feedbackResponse).toHaveValue("SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES");
  // 結果フィードバックカードは廃止され、良い/違うボタンへ統合された。
  await expect(page.getByRole("heading", { name: "結果フィードバック" })).toHaveCount(0);
  await expect(feedbackResponse).toHaveJSProperty("readOnly", true);
  await feedbackResponse.scrollIntoViewIfNeeded();
  await feedbackResponse.focus();
  await expect(feedbackResponse).toBeFocused();
  const sqlDisplayBox = await feedbackResponse.boundingBox();
  expect(sqlDisplayBox).not.toBeNull();
  // 生成 SQL は 12 行を見せる（#465）。高さは TextareaField の rows で決まる（textareaClassName の
  // min-h-72 は #800 で外した）ので、18rem ではなく「行の高さ × 12 + 上下の余白と枠」を下限にする。
  const sqlDisplayMetrics = await feedbackResponse.evaluate((node) => {
    const style = getComputedStyle(node);
    const px = (value: string) => Number.parseFloat(value);
    return {
      rows: (node as HTMLTextAreaElement).rows,
      minimumHeight:
        12 * px(style.lineHeight) +
        px(style.paddingTop) +
        px(style.paddingBottom) +
        px(style.borderTopWidth) +
        px(style.borderBottomWidth),
    };
  });
  expect(sqlDisplayMetrics.rows).toBe(12);
  expect(sqlDisplayBox!.height).toBeGreaterThanOrEqual(sqlDisplayMetrics.minimumHeight - 0.5);
  expect(sqlDisplayBox!.x).toBeGreaterThanOrEqual(0);
  expect(sqlDisplayBox!.x + sqlDisplayBox!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  await feedbackResponse.screenshot({ path: testInfo.outputPath("generated-sql-height.png") });

  // 「良い」は利用者からの評価としてアプリ DB にだけ保存する。
  await page.getByLabel("利用者コメント（feedback_content）").fill("期待どおりの SQL です");
  await page.getByRole("button", { name: "良い", exact: true }).click();
  await expect(page.getByText("フィードバックを保存しました。")).toBeVisible();
  expect(api.feedbackPayload).toEqual({
    history_id: "hist-001",
    rating: "good",
    feedback_content: "期待どおりの SQL です",
    comment: "期待どおりの SQL です",
  });

  // 「違う」= negative。利用者コメント未入力なら送信をブロックする。
  await page.getByLabel("利用者コメント（feedback_content）").fill("");
  await page.getByRole("button", { name: "違う", exact: true }).click();
  // 未入力の理由は欄の直下に出し、その欄へフォーカスする（#541）。
  const userComment = page.getByLabel("利用者コメント（feedback_content）");
  await expect(userComment).toHaveAccessibleDescription("「違う」のときは利用者コメントを入力してください。");
  await expect(userComment).toHaveAttribute("aria-invalid", "true");
  await expect(userComment).toBeFocused();

  await page.getByLabel("利用者コメント（feedback_content）").fill("列を請求金額だけに修正");
  await page.getByRole("button", { name: "違う", exact: true }).click();
  expect(api.feedbackPayload).toEqual({
    history_id: "hist-001",
    rating: "bad",
    feedback_content: "列を請求金額だけに修正",
    comment: "列を請求金額だけに修正",
  });
  // checkbox 廃止後、スコープは profile / 推薦適用が決める（手動選択なしは空）。
  expect(api.jobPayload?.allowed_objects).toEqual({
    table_names: [],
    columns: {},
  });
  expect(api.executePayload).toBeNull();
  await expectNoHorizontalScroll(page);
});

test("query workbench shows job action errors below the execution buttons", { tag: "@responsive" }, async ({ page }) => {
  await mockNl2SqlApi(page);
  const agentError =
    "OCI Enterprise AI の呼び出しに失敗しました: 応答がありませんでした。";
  await page.route("**/api/nl2sql/jobs", (route) =>
    route.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({ detail: agentError }),
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  const runButton = page.getByRole("button", { name: "SQL を生成して実行" });
  await runButton.click();

  const actionError = page.getByTestId("nl2sql-action-feedback-error");
  await expect(actionError).toContainText(agentError);
  await expect(actionError).toContainText("入力内容と接続状態を確認して再試行してください。");
  await expect(page.getByText(agentError)).toHaveCount(1);
  // 参考履歴の遅延挿入や自動スクロールで 2 回の boundingBox が別レイアウトを見るレースを避け、
  // 単一 evaluate で原子的に上下関係を検証する。
  await expect
    .poll(() =>
      page.evaluate(() => {
        const buttons = Array.from(document.querySelectorAll("button"));
        const run = buttons.find((el) => el.textContent?.includes("SQL を生成して実行"));
        const error = document.querySelector('[data-testid="nl2sql-action-feedback-error"]');
        if (!run || !error) return "missing";
        const runBox = run.getBoundingClientRect();
        const errorBox = error.getBoundingClientRect();
        return errorBox.top >= runBox.bottom - 1 ? "below" : `above:${errorBox.top}<${runBox.bottom}`;
      })
    )
    .toBe("below");
});

test("実行中の job は「実行を中止」でキャンセルでき、警告トーンで表示して UI を解放する", async ({ page }) => {
  await mockNl2SqlApi(page);
  let cancelRequested = false;
  // backend は実行中の段階が終わってから止まる。テストが受付の表示を確かめてから止める。
  let stopped = false;
  await page.route("**/api/nl2sql/jobs/job-default-001/cancel", (route) => {
    cancelRequested = true;
    return fulfillJson(route, {
      job_id: "job-default-001",
      status: "running",
      created_at: "2026-06-21T10:00:00.000Z",
      steps: [],
    });
  });
  await page.unroute("**/api/nl2sql/jobs/job-default-001");
  await page.route("**/api/nl2sql/jobs/job-default-001", (route) =>
    fulfillJson(
      route,
      stopped
        ? {
            job_id: "job-default-001",
            status: "error",
            created_at: "2026-06-21T10:00:00.000Z",
            error_message: "利用者の要求によりジョブをキャンセルしました。",
            error_code: "JOB_CANCELLED",
            steps: [],
          }
        : {
            job_id: "job-default-001",
            status: "running",
            created_at: "2026-06-21T10:00:00.000Z",
            steps: [
              { stage: "prepare_context", status: "done", elapsed_ms: 8 },
              { stage: "generate_sql", status: "running", elapsed_ms: null },
              { stage: "safety_check", status: "pending", elapsed_ms: null },
              { stage: "execute_sql", status: "pending", elapsed_ms: null },
              { stage: "format_results", status: "pending", elapsed_ms: null },
            ],
          }
    )
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  const runButton = page.getByRole("button", { name: "SQL を生成して実行" });
  await runButton.click();

  const cancelButton = page.getByRole("button", { name: "実行を中止" });
  await expect(cancelButton).toBeVisible();
  await cancelButton.click();
  await expect.poll(() => cancelRequested).toBe(true);

  // 受け付けた中止は、止まるまでボタンの行に受付を出し、もう一度押せなくする（#918）。
  await expect(page.getByTestId("nl2sql-job-cancel-feedback")).toContainText(
    "中止を受け付けました。実行中の段階が終わると停止します。"
  );
  await expect(cancelButton).toBeDisabled();
  await expect(page.getByTestId("nl2sql-action-feedback-error")).toHaveCount(0);
  stopped = true;

  // キャンセルは失敗ではなく警告トーンの固定面で表示し、UI ロックを解除する。
  await expect(page.getByTestId("nl2sql-job-cancelled")).toContainText(
    "利用者の要求によりジョブをキャンセルしました。"
  );
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "error");
  await expect(runButton).toBeEnabled();
  await expect(cancelButton).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("「実行を中止」の要求の失敗は中止のボタンの行に出し、もう一度押せる", async ({ page }) => {
  await mockNl2SqlApi(page);
  let cancelPosts = 0;
  await page.route("**/api/nl2sql/jobs/job-default-001/cancel", (route) => {
    cancelPosts += 1;
    return route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "ジョブの状態を保存できませんでした。" }),
    });
  });
  await page.unroute("**/api/nl2sql/jobs/job-default-001");
  await page.route("**/api/nl2sql/jobs/job-default-001", (route) =>
    fulfillJson(route, {
      job_id: "job-default-001",
      status: "running",
      created_at: "2026-06-21T10:00:00.000Z",
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
      ],
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  const cancelButton = page.getByRole("button", { name: "実行を中止" });
  await cancelButton.click();

  const feedback = page.getByTestId("nl2sql-job-cancel-feedback");
  await expect(feedback.getByRole("alert")).toContainText("ジョブの状態を保存できませんでした。");
  // 実行のボタンの下（別の起点）には出さない。
  await expect(page.getByTestId("nl2sql-action-feedback-error")).toHaveCount(0);
  await expect(cancelButton).toBeEnabled();
  await cancelButton.click();
  await expect.poll(() => cancelPosts).toBe(2);
});

test("job ポーリングの通信断が続くと追跡を停止しエラー表示と UI ロック解除を行う", async ({ page }) => {
  // 2.5 秒ごとの状態確認を page.clock で進め、実時間で待たない（#1593）。
  await page.clock.install();
  await mockNl2SqlApi(page);
  // 作成は成功するが、以後の状態確認がネットワーク断で失敗し続けるケース。
  await page.route("**/api/nl2sql/jobs/job-default-001", (route) => route.abort("failed"));

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  const runButton = page.getByRole("button", { name: "SQL を生成して実行" });
  const resetButton = page.getByRole("button", { name: "新しい質問を開始", exact: true });
  await runButton.click();

  // 2.5s 間隔 × 連続 3 回失敗(即時 tick 含む)で追跡を断念する。
  const actionError = page.getByTestId("nl2sql-action-feedback-error");
  await pollWithClock(page, async () => (await actionError.count()) ? actionError.textContent() : "", 2_500)
    .toContain("ジョブの状態確認に連続して失敗したため");
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
  await expect(runButton).toBeEnabled();
  await expect(resetButton).toBeEnabled();
  // 追跡解除後は sessionStorage の snapshot も消え、リロードで復元されない。
  expect((await readActiveJobState(page)).jobId).toBeNull();
});

test("ジョブの投入の応答が届かなくても、作られたジョブを取り直して二重に投入しない", async ({ page }) => {
  await mockNl2SqlApi(page);
  const submitted: Array<Record<string, unknown>> = [];
  await page.route("**/api/nl2sql/jobs", (route) => {
    // backend はジョブを作り終えたが、応答が画面に届かなかった（proxy の切断・通信断）。
    submitted.push(route.request().postDataJSON() as Record<string, unknown>);
    return route.abort("failed");
  });
  await page.route(/\/api\/nl2sql\/jobs\/[^/]+$/, (route) => {
    const jobId = new URL(route.request().url()).pathname.split("/").pop() ?? "";
    const created = submitted.find((payload) => payload.client_job_id === jobId);
    if (!created) {
      return route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "指定されたジョブが見つかりません。" }),
      });
    }
    return fulfillJson(route, {
      job_id: jobId,
      status: "done",
      created_at: "2026-06-21T10:00:00.000Z",
      finished_at: "2026-06-21T10:00:00.050Z",
      elapsed_ms: 50,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "done", elapsed_ms: 20 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 12 },
        { stage: "format_results", status: "done", elapsed_ms: 6 },
      ],
      timing: null,
      result: {
        history_id: "hist-001",
        engine: "enterprise_ai_direct",
        engine_meta: {},
        fallback_reason: "",
        original_question: String(created.question),
        rewritten_question: String(created.question),
        generated_sql: "SELECT CUSTOMER_NAME FROM INVOICES",
        executable_sql: "SELECT CUSTOMER_NAME FROM INVOICES",
        explanation: "",
        safety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: { columns: ["CUSTOMER_NAME"], rows: [{ CUSTOMER_NAME: "架空商事" }], total: 1 },
        timing,
      },
    });
  });

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  // 送信前に決めた job ID で作られたジョブを取り直し、進行と結果を出す（失敗にしない）。
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "done");
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
  await expect(page.getByTestId("nl2sql-action-feedback-error")).toHaveCount(0);
  expect(submitted).toHaveLength(1);
  expect(String(submitted[0]?.client_job_id ?? "")).toMatch(/^[0-9a-f-]{36}$/);
});

test("ジョブの投入の通信断でジョブが作られていなければ、日本語の案内を出して再実行できる", async ({ page }) => {
  await mockNl2SqlApi(page);
  let posts = 0;
  await page.route("**/api/nl2sql/jobs", (route) => {
    posts += 1;
    return route.abort("failed");
  });
  await page.route(/\/api\/nl2sql\/jobs\/[^/]+$/, (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "指定されたジョブが見つかりません。" }),
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  const runButton = page.getByRole("button", { name: "SQL を生成して実行" });
  await runButton.click();

  const actionError = page.getByTestId("nl2sql-action-feedback-error");
  await expect(actionError).toContainText("サーバーに接続できませんでした。");
  await expect(actionError).not.toContainText("Failed to fetch");
  await expect(actionError).not.toContainText("見つかりません");
  await expect(runButton).toBeEnabled();
  expect(posts).toBe(1);
});

for (const failure of [
  {
    name: "生成した SQL の実行の失敗",
    errorCode: "SQL_EXECUTION_FAILED",
    errorMessage: "生成した SQL の実行に失敗しました: ORA-00904: \"TOTAL\": 無効な識別子です。",
    safety,
    executeStatus: "error",
  },
  {
    name: "安全検査での遮断",
    errorCode: "SQL_BLOCKED",
    errorMessage: "許可されていない表を参照しています。",
    safety: { ...safety, is_safe: false, blocked_reason: "許可されていない表を参照しています。" },
    executeStatus: "skipped",
  },
]) {
  test(`${failure.name}では、実行していない SQL の結果を「0 件」と表示しない`, async ({ page }) => {
    await mockNl2SqlApi(page);
    await page.route("**/api/nl2sql/jobs/job-default-001", (route) =>
      fulfillJson(route, {
        job_id: "job-default-001",
        status: "error",
        created_at: "2026-06-21T10:00:00.000Z",
        finished_at: "2026-06-21T10:00:00.050Z",
        elapsed_ms: 50,
        error_message: failure.errorMessage,
        error_code: failure.errorCode,
        steps: [
          { stage: "prepare_context", status: "done", elapsed_ms: 8 },
          { stage: "generate_sql", status: "done", elapsed_ms: 20 },
          { stage: "safety_check", status: failure.executeStatus === "skipped" ? "error" : "done", elapsed_ms: 4 },
          { stage: "execute_sql", status: failure.executeStatus, elapsed_ms: 12 },
          { stage: "format_results", status: "done", elapsed_ms: 6 },
        ],
        timing: null,
        result: {
          history_id: "hist-001",
          engine: "enterprise_ai_direct",
          engine_meta: {},
          fallback_reason: "",
          original_question: "請求金額を一覧で見たい",
          rewritten_question: "請求金額を一覧で見たい",
          generated_sql: "SELECT TOTAL FROM INVOICES",
          executable_sql: "SELECT TOTAL FROM INVOICES",
          explanation: "",
          safety: failure.safety,
          recommendations: [],
          repaired_sql: "",
          optimization_hints: [],
          results: { columns: [], rows: [], total: 0 },
          timing,
        },
      })
    );

    await page.goto("/query");
    await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
    await page.getByRole("button", { name: "SQL を生成して実行" }).click();

    const progress = page.getByTestId("nl2sql-job-progress");
    await expect(progress).toHaveAttribute("data-job-status", "error");
    await expect(progress).toContainText(failure.errorMessage);
    // 生成した SQL は確かめられる（フィードバックにも使える）が、実行していない結果の表は出さない。
    await expect(page.getByTestId("nl2sql-generated-sql").getByRole("code")).toContainText(
      "SELECT TOTAL FROM INVOICES"
    );
    await expect(page.getByRole("heading", { name: "アプリ内フィードバック" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "実行結果（0 行）" })).toHaveCount(0);
    await expect(page.getByTestId("query-results")).toHaveCount(0);
  });
}

test("job が 404 のときは即座に追跡を解除して案内を表示する", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.route("**/api/nl2sql/jobs/job-default-001", (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "job not found" }),
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  const runButton = page.getByRole("button", { name: "SQL を生成して実行" });
  await runButton.click();

  const actionError = page.getByTestId("nl2sql-action-feedback-error");
  await expect(actionError).toContainText("実行中のジョブが見つかりませんでした");
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
  await expect(runButton).toBeEnabled();
});

test("失効した job スナップショットは復元時の 404 で破棄されリクエストが打ち止めになる", async ({ page }) => {
  await mockNl2SqlApi(page);
  let staleJobRequests = 0;
  await page.route("**/api/nl2sql/jobs/job-stale-001", (route) => {
    staleJobRequests += 1;
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "job not found" }),
    });
  });
  await seedActiveJob(page, "job-stale-001", 1000);

  await page.goto("/query");

  const actionError = page.getByTestId("nl2sql-action-feedback-error");
  await expect(actionError).toContainText("実行中のジョブが見つかりませんでした");
  expect(staleJobRequests).toBe(1);
  expect((await readActiveJobState(page)).jobId).toBeNull();
  expect(
    (await readActiveJobState(page)).startedAt
  ).toBeNull();
});

test("TTL を超えた job スナップショットは復元ポーリング自体を行わない", async ({ page }) => {
  await mockNl2SqlApi(page);
  let staleJobRequests = 0;
  await page.route("**/api/nl2sql/jobs/job-expired-001", (route) => {
    staleJobRequests += 1;
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "job not found" }),
    });
  });
  await seedActiveJob(page, "job-expired-001", 3960000);

  await page.goto("/query");
  await expect(page.getByTestId("nl2sql-schema-reference")).toBeVisible();

  expect(staleJobRequests).toBe(0);
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
  expect((await readActiveJobState(page)).jobId).toBeNull();
});

test("履歴更新の失敗は warning に留め、成功した実行結果を失敗表示に変えない", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.route("**/api/nl2sql/history**", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "履歴の読込に失敗しました。" }),
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  // 結果領域が正本: job は成功しているので結果表と進捗はそのまま表示される。
  await expect(page.getByTestId("nl2sql-job-progress")).toContainText(
    "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES"
  );
  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toBeVisible();
  await expect(page.getByTestId("nl2sql-action-feedback-error")).toHaveCount(0);
  // 履歴の失敗は warning toast 1 回のみ(messaging spec: 固定面の正本と二重にしない)。
  const toastRegion = page.getByRole("region", { name: "通知" });
  await expect(toastRegion.getByRole("status")).toContainText("履歴の更新に失敗しました");
});

test("query workbench keeps schema detail errors inside the schema panel when schema list is populated", async ({ page }) => {
  await mockNl2SqlApi(page);
  const schemaEmptyError =
    "Schema catalog が空です。Oracle schema を refresh するか、Data Tools から sample data を明示的に import してください。";
  await page.unroute("**/api/schema/objects/*/*");
  await page.route("**/api/schema/objects/*/*", (route) =>
    route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({ detail: schemaEmptyError }),
    })
  );

  await page.goto("/query");
  await page.getByLabel("表・項目検索").fill("請求");

  const schemaDetailError = page.getByTestId("nl2sql-schema-detail-error");
  await expect(schemaDetailError).toContainText(schemaEmptyError);
  await expect(page.getByText("Schema catalog が空です")).toHaveCount(1);
  await expect(page.getByTestId("nl2sql-schema-reference")).toContainText("請求");
  await expectNoHorizontalScroll(page);
});

test("Enterprise AI Direct job does not show schema-empty when schema reference is populated", async ({ page }) => {
  const api = await mockNl2SqlApi(page);

  await page.goto("/query");
  await expect(page.getByTestId("nl2sql-schema-reference")).toBeVisible();
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect.poll(() => api.jobPayload?.engine).toBe("enterprise_ai_direct");
  await expect(page.getByTestId("nl2sql-job-progress")).toContainText(
    "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES"
  );
  await expect(page.getByText("Schema catalog が空です")).toHaveCount(0);
  await expect(page.getByTestId("nl2sql-action-feedback-error")).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("SQL の生成と実行の開始時に前回の生成 SQL・実行結果を先に消す", async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.goto("/query");
  const question = nl2sqlQuestionInput(page);
  await fillQuestionWhenReady(page, "請求金額を一覧で見たい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect(page.getByTestId("nl2sql-job-progress")).toContainText(
    "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES"
  );
  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "アプリ内フィードバック" })).toBeVisible();

  const jobsGate = createRequestGate();
  await page.route("**/api/nl2sql/jobs", async (route) => {
    await jobsGate.promise;
    return fulfillJson(route, {
      job_id: "job-preview-reset-001",
      status: "running",
      created_at: "2026-06-21T10:00:00.000Z",
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    });
  });
  await page.route("**/api/nl2sql/jobs/job-preview-reset-001", (route) =>
    fulfillJson(route, {
      job_id: "job-preview-reset-001",
      status: "done",
      created_at: "2026-06-21T10:00:00.000Z",
      started_at: "2026-06-21T10:00:00.000Z",
      finished_at: "2026-06-21T10:00:00.040Z",
      elapsed_ms: 40,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "done", elapsed_ms: 18 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 6 },
        { stage: "format_results", status: "done", elapsed_ms: 4 },
      ],
      timing: null,
      result: {
        engine: "enterprise_ai_direct",
        original_question: "請求金額だけを確認したい",
        rewritten_question: "請求金額だけを確認したい",
        generated_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
        executable_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
        explanation: "請求金額だけを取得します。",
        safety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: { columns: ["TOTAL_AMOUNT"], rows: [{ TOTAL_AMOUNT: 1200000 }], total: 1 },
        timing,
      },
    })
  );

  await question.fill("請求金額だけを確認したい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "アプリ内フィードバック" })).toHaveCount(0);
  await expect(question).toHaveValue("請求金額だけを確認したい");

  jobsGate.release();
  await expect(page.getByTestId("nl2sql-job-progress")).toContainText(
    "SELECT TOTAL_AMOUNT FROM INVOICES"
  );
  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toBeVisible();
});

test("SQL の生成と実行の開始時に前回の結果表を先に消す", async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.goto("/query");
  const question = nl2sqlQuestionInput(page);
  await question.fill("請求金額を一覧で見たい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "アプリ内フィードバック" })).toBeVisible();

  const createdAt = "2026-06-21T10:00:00.000Z";
  const jobsGate = createRequestGate();
  let jobPayload: Record<string, unknown> | null = null;
  await page.route("**/api/nl2sql/jobs", async (route) => {
    jobPayload = route.request().postDataJSON() as Record<string, unknown>;
    await jobsGate.promise;
    return fulfillJson(route, {
      job_id: "job-reset-001",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    });
  });
  await page.route("**/api/nl2sql/jobs/job-reset-001", (route) =>
    fulfillJson(route, {
      job_id: "job-reset-001",
      status: "done",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: createdAt,
      elapsed_ms: 40,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "done", elapsed_ms: 20 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 6 },
        { stage: "format_results", status: "done", elapsed_ms: 2 },
      ],
      timing: null,
      result: {
        engine: "enterprise_ai_direct",
        engine_meta: {},
        fallback_reason: "",
        original_question: "請求件数を数えたい",
        rewritten_question: "請求件数を数えたい",
        generated_sql: "SELECT COUNT(*) AS INVOICE_COUNT FROM INVOICES",
        executable_sql: "SELECT COUNT(*) AS INVOICE_COUNT FROM INVOICES",
        explanation: "請求件数を集計します。",
        safety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: {
          columns: ["INVOICE_COUNT"],
          rows: [{ INVOICE_COUNT: 2 }],
          total: 1,
        },
        timing: null,
      },
    })
  );

  await question.fill("請求件数を数えたい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  // 用語・同義語は既定 off なので、入力そのままの質問で job を作る。
  await expect.poll(() => jobPayload?.question).toBe("請求件数を数えたい");
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "アプリ内フィードバック" })).toHaveCount(0);
  await expect(question).toHaveValue("請求件数を数えたい");

  jobsGate.release();
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "done");
  await expect(page.getByTestId("nl2sql-job-progress")).toContainText(
    "SELECT COUNT(*) AS INVOICE_COUNT FROM INVOICES"
  );
  await expect(page.getByRole("cell", { name: "2" })).toBeVisible();
});

test("SQL の生成と実行の開始時に前回の生成結果を先に消し、現在の質問で job を作成できる", async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.goto("/query");
  const question = nl2sqlQuestionInput(page);
  await question.fill("請求金額を一覧で見たい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "アプリ内フィードバック" })).toBeVisible();

  const jobsGate = createRequestGate();
  let jobPayload: Record<string, unknown> | null = null;
  await page.route("**/api/nl2sql/jobs", async (route) => {
    jobPayload = route.request().postDataJSON() as Record<string, unknown>;
    await jobsGate.promise;
    return fulfillJson(route, {
      job_id: "job-current-question-001",
      status: "running",
      created_at: "2026-06-21T10:00:00.000Z",
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    });
  });
  await page.route("**/api/nl2sql/jobs/job-current-question-001", (route) =>
    fulfillJson(route, {
      job_id: "job-current-question-001",
      status: "done",
      created_at: "2026-06-21T10:00:00.000Z",
      started_at: "2026-06-21T10:00:00.000Z",
      finished_at: "2026-06-21T10:00:00.040Z",
      elapsed_ms: 40,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "done", elapsed_ms: 18 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 6 },
        { stage: "format_results", status: "done", elapsed_ms: 4 },
      ],
      timing: null,
      result: {
        engine: "enterprise_ai_direct",
        original_question: "請求金額を業務用語で解釈したい",
        rewritten_question: "請求金額を業務用語で解釈したい",
        generated_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
        executable_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
        explanation: "請求情報を取得します。",
        safety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: {
          columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
          rows: [{ CUSTOMER_NAME: "架空商事", TOTAL_AMOUNT: 1200000 }],
          total: 1,
        },
        timing,
      },
    })
  );

  await question.fill("請求金額を業務用語で解釈したい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "アプリ内フィードバック" })).toHaveCount(0);
  await expect(question).toHaveValue("請求金額を業務用語で解釈したい");
  // 用語・同義語は既定 off なので、入力そのままの質問で job を作る。
  await expect.poll(() => jobPayload?.question).toBe("請求金額を業務用語で解釈したい");

  jobsGate.release();
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "done");
  expect(jobPayload).toMatchObject({
    question: "請求金額を業務用語で解釈したい",
    profile_id: "default",
    engine: "enterprise_ai_direct",
  });
});

test("参考履歴は件数を表示し、候補があれば自動で展開する", async ({ page }) => {
  await mockNl2SqlApi(page);
  let similarHistoryPayload: Record<string, unknown> | null = null;
  await page.unroute("**/api/nl2sql/similar-history");
  await page.route("**/api/nl2sql/similar-history", (route) => {
    similarHistoryPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      used_for_generation: true,
      engine: "enterprise_ai_direct",
      items: [0.9, 0.88, 0.86].map((score, index) => ({
        history_id: `hist-direct-${index + 1}`,
        question: `Direct 参考履歴 ${index + 1}`,
        sql: historySql,
        profile_id: historyItem.profile_id,
        profile_name: historyItem.profile_name,
        score,
        reason: "請求金額の履歴と近い質問です。",
      })),
    });
  });

  await page.goto("/query");
  await expect(page.getByRole("region", { name: "SQL 生成ワークスペース" })).toBeVisible();

  // 4 文字以上の質問を入力すると参考履歴を取得する（debounce 650ms）。
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  await expect.poll(() => similarHistoryPayload).toMatchObject({
    question: "請求金額を一覧で見たい",
    profile_id: "default",
    engine: "enterprise_ai_direct",
  });
  expect(similarHistoryPayload ?? {}).not.toHaveProperty("limit");

  const header = page.getByRole("button", { name: /参考履歴/ });
  await expect(header).toBeVisible();
  await expect(header).toContainText("3 件");
  await expect(header).toContainText("管理者レビュー結果: 良いのみ");
  // 候補がある検索完了後は自動で展開する。
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByTestId("nl2sql-similar-history")).toBeAttached();
  await expect(page.getByTestId("nl2sql-similar-history")).toBeVisible();
  await expect(page.getByText("類似度 90%")).toBeVisible();
  await expect(page.getByText("類似度 88%")).toBeVisible();
  await expect(page.getByText("類似度 86%")).toBeVisible();
  await expect(page.getByTestId("nl2sql-similar-history-item")).toHaveCount(3);
  await expect(page.getByText("請求金額の履歴と近い質問です。")).toHaveCount(3);
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByTestId("nl2sql-similar-history")).toBeHidden();
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");

  const jobsGate = createRequestGate();
  await page.unroute("**/api/nl2sql/jobs");
  await page.route("**/api/nl2sql/jobs", async (route) => {
    await jobsGate.promise;
    return fulfillJson(route, { job_id: "job-default-001", status: "running", steps: [] });
  });

  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect(nl2sqlQuestionInput(page)).toBeDisabled();
  await expect(header).toBeVisible();
  await expect(header).toBeDisabled();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("類似度 90%")).toBeVisible();
  await expectNoHorizontalScroll(page);
  jobsGate.release();
});

test("参考履歴は API が空の場合も表示し、空状態を展開できる", async ({ page }) => {
  await mockNl2SqlApi(page);
  let requested = false;
  await page.unroute("**/api/nl2sql/similar-history");
  await page.route("**/api/nl2sql/similar-history", (route) => {
    requested = true;
    return fulfillJson(route, { items: [] });
  });

  await page.goto("/query");
  await expect(page.getByRole("region", { name: "SQL 生成ワークスペース" })).toBeVisible();

  await nl2sqlQuestionInput(page).fill('対象テーブル："PROJECT"\n抽出項目：PROJECT_ID\n抽出条件：');

  await expect.poll(() => requested).toBe(true);
  const header = page.getByRole("button", { name: /参考履歴/ });
  await expect(header).toBeVisible();
  await expect(header).toContainText("0 件");
  await expect(header).toContainText("管理者レビュー結果: 良いのみ");
  await expect(header).toHaveAttribute("aria-expanded", "false");

  const panel = page.getByTestId("nl2sql-similar-history");
  await expect(panel).toBeAttached();
  await expect(panel).toBeHidden();
  await expect(panel.getByTestId("nl2sql-similar-history-item")).toHaveCount(0);

  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("参考履歴はありません");
  await expect(panel).toContainText("質問またはプロファイルを変更すると自動で再検索します。");
  await expect(panel.getByTestId("nl2sql-similar-history-item")).toHaveCount(0);

  const jobsGate = createRequestGate();
  await page.unroute("**/api/nl2sql/jobs");
  await page.route("**/api/nl2sql/jobs", async (route) => {
    await jobsGate.promise;
    return fulfillJson(route, { job_id: "job-default-001", status: "running", steps: [] });
  });

  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect(nl2sqlQuestionInput(page)).toBeDisabled();
  await expect(header).toBeVisible();
  await expect(header).toBeDisabled();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("参考履歴はありません");
  jobsGate.release();
});

test("参考履歴の「検索中」は SQL 生成の実行中に固まって残らない", async ({ page }) => {
  await mockNl2SqlApi(page);
  // 参考履歴の応答を握って in-flight のまま実行へ進める（abort で .finally が走らない経路）。
  const similarGate = createRequestGate();
  await page.unroute("**/api/nl2sql/similar-history");
  await page.route("**/api/nl2sql/similar-history", async (route) => {
    await similarGate.promise;
    return fulfillJson(route, { items: [] });
  });
  const jobsGate = createRequestGate();
  await page.route("**/api/nl2sql/jobs", async (route) => {
    await jobsGate.promise;
    return fulfillJson(route, { job_id: "job-similar-stuck", status: "running", steps: [] });
  });

  await page.goto("/query");
  await expect(page.getByRole("region", { name: "SQL 生成ワークスペース" })).toBeVisible();

  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  const header = page.getByRole("button", { name: /参考履歴/ });
  await expect(page.getByTestId("nl2sql-similar-history-loading")).toContainText("参考履歴を検索しています");

  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  // 実行中は入力系が無効化される。参考履歴は「検索中」のまま残らず、非活性で表示を維持する。
  await expect(nl2sqlQuestionInput(page)).toBeDisabled();
  await expect(page.getByTestId("nl2sql-similar-history-loading")).toHaveCount(0);
  await expect(header).toBeVisible();
  await expect(header).toBeDisabled();
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByTestId("nl2sql-similar-history")).toBeAttached();
  await expect(page.getByTestId("nl2sql-similar-history")).toBeHidden();

  similarGate.release();
  jobsGate.release();
});

test("参考履歴は管理者レビュー結果が良い履歴だけを表示する", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/similar-history");
  await page.route("**/api/nl2sql/similar-history", (route) =>
    fulfillJson(route, {
      items: [
        {
          history_id: "hist-good",
          question: longHistoryItem.question,
          sql: longHistoryItem.executable_sql || longHistoryItem.generated_sql,
          profile_id: longHistoryItem.profile_id,
          profile_name: longHistoryItem.profile_name,
          score: 0.92,
          reason: "請求金額が一致し、管理者レビュー結果が良い履歴です。",
        },
      ],
    })
  );

  await page.goto("/query");
  const workspace = page.getByRole("region", { name: "SQL 生成ワークスペース" });
  await expect(workspace).toBeVisible();

  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  const header = page.getByRole("button", { name: /参考履歴/ });
  await expect(header).toBeVisible();
  await expect(header).toContainText("管理者レビュー結果: 良いのみ");
  await expect(header).toHaveAttribute("aria-expanded", "true");

  const panel = page.getByTestId("nl2sql-similar-history");
  const rows = panel.getByTestId("nl2sql-similar-history-item");
  const similarQuestion = rows.first().getByTestId("nl2sql-similar-history-question");
  await expect(rows).toHaveCount(1);
  await expect(similarQuestion).toContainText("対象テーブル");
  await expectQuestionClamp(similarQuestion, longHistoryItem.question, 1);
  await expect(panel).toContainText("類似度 92%");
  await expect(panel).not.toContainText("利用者だけ良い評価の請求金額");
  await expect(panel).not.toContainText("違う評価の請求金額");
  await expect(panel).not.toContainText("未評価の請求金額");
  await expectNoHorizontalScroll(page);
  await expectNoElementHorizontalOverflow(header);
  await expectNoElementHorizontalOverflow(panel);
  await expectNoElementHorizontalOverflow(rows.first());
  await expectNoElementHorizontalOverflow(similarQuestion);

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(header).toBeVisible();
  await expect(panel).toBeVisible();
  await expectNoHorizontalScroll(page);
  await expectNoElementHorizontalOverflow(header);
  await expectNoElementHorizontalOverflow(panel);
  await expectNoElementHorizontalOverflow(rows.first());
  await expectNoElementHorizontalOverflow(similarQuestion);
});

test("実行結果は要約・表の中のスクロールのプレビューと、すべての行のシート（25 件ごとのページ送り）・CSV で出す（#1178）", async ({ page }) => {
  await mockNl2SqlApi(page);
  // 実行結果を 27 件へ上書き（後勝ちルートで mockNl2SqlApi の job result を差し替える）
  const rows = Array.from({ length: 27 }, (_, index) => ({
    CUSTOMER_NAME: `顧客${String(index + 1).padStart(2, "0")}`,
    TOTAL_AMOUNT: (index + 1) * 1000,
  }));
  await page.route("**/api/nl2sql/jobs/job-default-001", (route) =>
    fulfillJson(route, {
      job_id: "job-default-001",
      status: "done",
      created_at: "2026-06-21T10:00:00.000Z",
      started_at: "2026-06-21T10:00:00.000Z",
      finished_at: "2026-06-21T10:00:00.050Z",
      elapsed_ms: 50,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "done", elapsed_ms: 20 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 12 },
        { stage: "format_results", status: "done", elapsed_ms: 6 },
      ],
      timing: null,
      result: {
        engine: "enterprise_ai_direct",
        original_question: "請求金額を一覧で見たい",
        rewritten_question: "請求金額を一覧で見たい",
        generated_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
        executable_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
        explanation: "請求情報を取得します。",
        safety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: { columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"], rows, total: rows.length },
        timing,
      },
    })
  );

  await page.goto("/query");
  await fillQuestionWhenReady(page, "請求金額を一覧で見たい");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect(page.getByRole("heading", { name: "実行結果（27 行）" })).toBeVisible();
  // 要約の 1 行（行数・列数）。共通の結果の表（ResultTable。#1178）。
  await expect(page.getByTestId("query-results-summary")).toHaveText("27 行・2 列");
  // プレビューは先頭の行（50 行まで）を表頭固定・表の中の縦スクロールで出す（ページ全体を伸ばさない）。
  const resultRegion = page.getByRole("region", { name: "実行結果の表。スクロールできます。" });
  await expect(resultRegion).toBeVisible();
  await expect(resultRegion.locator("thead")).toHaveCSS("position", "sticky");
  await expect(resultRegion.locator("tbody tr")).toHaveCount(27);
  expect(await resultRegion.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
  // 数値の列は右寄せ。
  await expect(resultRegion.getByRole("cell", { name: "3000", exact: true })).toHaveCSS("text-align", "right");
  // CSV は取得した行（ヘッダー + 27 行）を、画面の接頭辞と時刻の名前でダウンロードする。
  const downloadPromise = page.waitForEvent("download");
  await page.getByTestId("query-results-csv").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^nl2sql-sql-generation-result-\d{8}-\d{6}\.csv$/u);
  const csv = await readFile((await download.path())!, "utf8");
  expect(csv.trimEnd().split("\r\n")).toHaveLength(28);

  // すべての行は広いシートで 25 件ごとにページを送る。
  await page.getByTestId("query-results-view-all").click();
  const sheet = page.getByRole("dialog", { name: "実行結果（27 行）" });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByRole("button", { name: "CSV をダウンロード" })).toBeVisible();
  const pagination = page.getByTestId("query-results-all-pagination");
  await expect(pagination.getByRole("button", { name: "次へ" })).toBeInViewport();
  await expect(pagination).toContainText("1–25 / 27 件");
  await expect(pagination).toContainText("1 / 2 ページ");
  const allTable = page.getByTestId("query-results-all-table");
  await expect(allTable.getByRole("cell", { name: "顧客25" })).toBeVisible();
  await expect(allTable.getByRole("cell", { name: "顧客26" })).toHaveCount(0);

  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(pagination).toContainText("26–27 / 27 件");
  await expect(allTable.getByRole("cell", { name: "顧客27" })).toBeVisible();
  await expect(allTable.getByRole("cell", { name: "顧客01" })).toHaveCount(0);

  await sheet.getByRole("button", { name: "実行結果を閉じる" }).click();
  await expect(sheet).toBeHidden();
  await expect(page.getByTestId("query-results-view-all")).toBeFocused();
});

test("プロファイルの読み込み中に入力した質問は、プロファイルの確定後も残り実行できる（#168）", async ({ page }) => {
  await mockNl2SqlApi(page);
  // プロファイルの一覧と既定の応答を止め、確定する前に質問を入力する（遅い環境の再現）。
  let releaseProfiles!: () => void;
  const profilesGate = new Promise<void>((resolve) => {
    releaseProfiles = resolve;
  });
  for (const pattern of [
    "**/api/nl2sql/profiles",
    "**/api/nl2sql/profiles/default",
    "**/api/nl2sql/profiles/search?*",
  ]) {
    await page.route(pattern, async (route) => {
      await profilesGate;
      await route.fallback();
    });
  }

  await page.goto("/query");
  const input = nl2sqlQuestionInput(page);
  await input.fill("読み込み中に入力した質問");
  releaseProfiles();

  await expect(page.getByRole("button", { name: "SQL を生成して実行" })).toBeEnabled();
  await expect(input).toHaveValue("読み込み中に入力した質問");
});

test("SQL を生成して実行すると実処理の段階別進捗と結果を表示する", async ({ page, context, baseURL }) => {
  const api = await mockNl2SqlApi(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: new URL(baseURL!).origin,
  });

  const questionText = "今月の請求金額を確認したい";
  const createdAt = "2026-06-21T10:00:00.000Z";
  // 実行結果の generated_sql に一致する履歴を用意し、良い/違う の同時保存を検証できるようにする。
  const runHistoryItem = {
    ...historyItem,
    id: "hist-run-001",
    question: questionText,
    generated_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
    executable_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
    feedback_rating: null,
    feedback_comment: "",
  };
  await page.route("**/api/nl2sql/history**", (route) =>
    fulfillJson(route, { items: [runHistoryItem] })
  );
  const stageTimings = [
    { stage: "prepare_context", elapsed_ms: 10 },
    { stage: "generate_sql", elapsed_ms: 100 },
    { stage: "safety_check", elapsed_ms: 20 },
    { stage: "execute_sql", elapsed_ms: 30 },
    { stage: "format_results", elapsed_ms: 10 },
  ];
  const finishedTiming = {
    created_at: createdAt,
    started_at: "2026-06-21T10:00:00.010Z",
    finished_at: "2026-06-21T10:00:00.180Z",
    elapsed_ms: 170,
    stage_timings: stageTimings,
  };
  // 処理の経過は span のイベント（#3226）。工程の span の時刻は工程の所要時間を順に積む。
  const at = (ms: number) => new Date(Date.parse(finishedTiming.started_at) + ms).toISOString();
  let offset = 0;
  const spanTimes = Object.fromEntries(
    stageTimings.map((item) => {
      const range = { startedAt: at(offset), finishedAt: at(offset + item.elapsed_ms) };
      offset += item.elapsed_ms;
      return [item.stage, range];
    })
  );
  const stageDetails: Record<string, Record<string, unknown>> = {
    prepare_context: { fields: [{ label: "生成に使った質問", value: questionText }] },
    safety_check: { fields: [{ label: "結果", value: "安全" }, { label: "参照した表", value: "INVOICES", mono: true }, { label: "参照した列", value: "CUSTOMER_NAME, TOTAL_AMOUNT", mono: true }] },
  };
  const runningEvents = jobProgressEvents("job-step-001", {
    at: createdAt,
    steps: {
      queue: { status: "done", startedAt: createdAt, finishedAt: finishedTiming.started_at },
      prepare_context: { status: "done", ...spanTimes.prepare_context, details: stageDetails.prepare_context },
      generate_sql: { status: "running", startedAt: spanTimes.generate_sql.startedAt, params: { engine: "enterprise_ai_direct" } },
    },
  });
  const doneEvents = jobProgressEvents("job-step-001", {
    at: finishedTiming.finished_at,
    terminal: "done",
    steps: {
      queue: { status: "done", startedAt: createdAt, finishedAt: finishedTiming.started_at },
      ...Object.fromEntries(
        stageTimings.map((item) => [
          item.stage,
          {
            status: "done",
            ...spanTimes[item.stage],
            ...(item.stage === "generate_sql" ? { params: { engine: "enterprise_ai_direct" } } : {}),
            ...(stageDetails[item.stage] ? { details: stageDetails[item.stage] } : {}),
          },
        ])
      ),
    },
  });
  let jobPayload: Record<string, unknown> | null = null;
  let finishJob!: () => void;
  const terminalGate = new Promise<void>((resolve) => {
    finishJob = resolve;
  });

  await page.route("**/api/nl2sql/jobs", (route) => {
    jobPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      job_id: "job-step-001",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
      progress_events: runningEvents,
    });
  });
  await page.route("**/api/nl2sql/jobs/job-step-001", async (route) => {
    await terminalGate;
    return fulfillJson(route, {
      job_id: "job-step-001",
      status: "done",
      created_at: createdAt,
      started_at: finishedTiming.started_at,
      finished_at: finishedTiming.finished_at,
      elapsed_ms: finishedTiming.elapsed_ms,
      error_message: null,
      steps: stageTimings.map((item) => ({ ...item, status: "done" })),
      progress_events: doneEvents,
      timing: finishedTiming,
      result: {
        history_id: "hist-run-001",
        engine: "enterprise_ai_direct",
        engine_meta: { team_name: "mock_team" },
        fallback_reason: "",
        original_question: questionText,
        rewritten_question: questionText,
        generated_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
        executable_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
        explanation: "請求情報を取得します。",
        safety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: {
          columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
          rows: [{ CUSTOMER_NAME: "架空商事", TOTAL_AMOUNT: 1200000 }],
          total: 1,
        },
        timing: finishedTiming,
      },
    });
  });

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill(questionText);
  // 用語・同義語は既定 off のため、書き換え後の質問を検証する本テストでは明示 ON にする。
  await openNl2SqlExecutionOptions(page);
  await page.getByLabel("用語・同義語を使う").check();
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const progress = page.getByTestId("nl2sql-job-progress");
  const prepare = page.getByTestId("nl2sql-job-step-prepare_context");
  const generate = page.getByTestId("nl2sql-job-step-generate_sql");
  const safetyStep = page.getByTestId("nl2sql-job-step-safety_check");

  try {
    await expect(progress).toBeVisible();
    await expect(progress).toHaveAttribute("data-job-status", "running");
    await expect(prepare).toHaveAttribute("data-status", "done");
    await expect(prepare).toContainText("質問と参照できる表を整理しました");
    // 実行中の span の行（#3226）。
    await expect(generate).toHaveAttribute("data-status", "running");
    await expect(generate).toHaveAttribute("aria-current", "step");
    await expect(generate).toContainText("SQL を生成しています");
    await expect(progress.getByRole("timer")).toHaveAccessibleName(/経過時間 \d{2}:\d{2}/);
    await expect(progress.getByRole("timer")).toHaveAttribute("aria-live", "off");
    const runningIcon = generate.locator("svg.animate-spin");
    await expect(runningIcon).toBeVisible();
    // job の間のスピナーは実行中の工程の 1 つだけ。「SQL を生成して実行」は無効にするだけで回さない（#416）。
    await expectSingleSpinner(page, generate);
    await expect(page.getByRole("button", { name: "SQL を生成して実行" })).not.toHaveAttribute("aria-busy", "true");
    // スピナーは reduced motion でも回転を止めない（#440）。
    expect(await runningIcon.evaluate((element) => getComputedStyle(element).animationName)).toBe("spin");
    await expect(safetyStep).toHaveCount(0);
    expect(jobPayload).toMatchObject({
      // 用語・同義語を ON にしたため、job runner 側で一度だけ辞書適用する。
      question: questionText,
      engine: "enterprise_ai_direct",
      profile_id: "default",
      allowed_objects: { table_names: [], columns: {} },
      use_glossary: true,
    });
  } finally {
    finishJob();
  }

  await expect(progress).toHaveAttribute("data-job-status", "done");
  for (const item of stageTimings) {
    const step = page.getByTestId(`nl2sql-job-step-${item.stage}`);
    await expect(step).toHaveAttribute("data-status", "done");
    await expect(step).toContainText(item.elapsed_ms < 100 ? "0.1 秒未満" : "0.1 秒");
  }
  await page.getByTestId("nl2sql-job-summary").click();
  await expect(page.getByTestId("nl2sql-job-summary")).toContainText("0.1 秒");
  await prepare.getByRole("button").first().click();
  await expect(prepare).toContainText("今月の請求金額を確認したい");
  // 参照表・列は生成前の推測ではなく、生成 SQL を解析した safety step に表示する。
  await safetyStep.getByRole("button").first().click();
  await expect(safetyStep).toContainText("INVOICES");
  await expect(safetyStep).toContainText("TOTAL_AMOUNT");
  // 生成 SQL は処理の経過を閉じても確認できる。
  const generated = page.getByTestId("nl2sql-generated-sql");
  await expect(generated).toContainText("SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES");
  await expect(generated).toContainText("請求情報を取得します。");
  await expect(generated.getByText("安全", { exact: true })).toBeVisible();
  const copySql = generated.getByRole("button", { name: "コピー" });
  await expect(copySql).toBeVisible();
  await expectContentActionsRightAligned(generated.getByTestId("generated-sql-content-actions"));
  await copySql.click();
  // コピーの成功はボタンのアイコンの Check と読み上げ（共有の CopyButton。成功の Toast は出さない。#2099）。
  await expect(copySql).toHaveAttribute("data-copy-state", "copied");
  await expect(generated.getByRole("status").filter({ hasText: "コピーしました" })).toHaveCount(1);
  await expect(page.getByRole("region", { name: "通知" })).not.toContainText("コピーしました");
  await expect(copySql).toHaveAttribute("data-copy-state", "idle");
  await page.evaluate(() => {
    const state = globalThis as typeof globalThis & { __legacyCopiedSql?: string };
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: false,
    });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: (command: string) => {
        if (command !== "copy") return false;
        state.__legacyCopiedSql = (document.activeElement as HTMLTextAreaElement | null)?.value ?? "";
        return true;
      },
    });
  });
  await copySql.click();
  await expect(copySql).toHaveAttribute("data-copy-state", "copied");
  await expect
    .poll(() =>
      page.evaluate(
        () => (globalThis as typeof globalThis & { __legacyCopiedSql?: string }).__legacyCopiedSql
      )
    )
    .toBe("SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES");
  await page.evaluate(() => {
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: () => Promise.reject(new Error("clipboard denied")),
      },
    });
  });
  await copySql.click();
  const copyFailedToast = page.getByRole("alert").filter({ hasText: "コピーできませんでした。" });
  await expect(copyFailedToast).toBeVisible();
  // danger toast は自動消滅しない(duration 0)。mobile 幅で後続の「良い」クリックを遮るため閉じる。
  await copyFailedToast.getByRole("button", { name: "閉じる" }).click();
  await expect(copyFailedToast).toHaveCount(0);
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();

  // 実行結果の「良い」は利用者からの評価としてアプリ DB に保存する。
  await expect(page.getByRole("heading", { name: "アプリ内フィードバック" })).toBeVisible();
  await page.getByLabel("利用者コメント（feedback_content）").fill("想定どおりの SQL です");
  await page.getByRole("button", { name: "良い", exact: true }).click();
  await expect(page.getByText("フィードバックを保存しました。")).toBeVisible();
  expect(api.feedbackPayload).toEqual({
    history_id: "hist-run-001",
    rating: "good",
    feedback_content: "想定どおりの SQL です",
    comment: "想定どおりの SQL です",
  });

  await expectNoHorizontalScroll(page);
});

test("結果整形が完了した job は SQL 実行ステップを処理中のまま残さない", async ({ page }) => {
  await mockNl2SqlApi(page);
  const questionText = "該当データがない条件で請求金額を確認したい";
  const createdAt = "2026-06-21T10:00:00.000Z";

  await page.unroute("**/api/nl2sql/jobs");
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfillJson(route, {
      job_id: "job-format-done-execute-running",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );
  await page.route("**/api/nl2sql/jobs/job-format-done-execute-running", (route) =>
    fulfillJson(route, {
      job_id: "job-format-done-execute-running",
      status: "done",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: "2026-06-21T10:00:01.900Z",
      elapsed_ms: 1900,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 20 },
        { stage: "generate_sql", status: "done", elapsed_ms: 1200 },
        { stage: "safety_check", status: "done", elapsed_ms: 114 },
        { stage: "execute_sql", status: "running", elapsed_ms: null },
        { stage: "format_results", status: "done", elapsed_ms: 1900 },
      ],
      timing: {
        created_at: createdAt,
        started_at: createdAt,
        finished_at: "2026-06-21T10:00:01.900Z",
        elapsed_ms: 1900,
        stage_timings: [
          { stage: "prepare_context", elapsed_ms: 20 },
          { stage: "generate_sql", elapsed_ms: 1200 },
          { stage: "safety_check", elapsed_ms: 114 },
          { stage: "format_results", elapsed_ms: 1900 },
        ],
      },
      result: {
        history_id: "hist-format-done-execute-running",
        engine: "enterprise_ai_direct",
        engine_meta: { profile: "mock_agent_profile" },
        fallback_reason: "",
        original_question: questionText,
        rewritten_question: questionText,
        generated_sql: "SELECT TOTAL_AMOUNT FROM INVOICES WHERE CUSTOMER_NAME = '該当なし'",
        executable_sql: "SELECT TOTAL_AMOUNT FROM INVOICES WHERE CUSTOMER_NAME = '該当なし'",
        explanation: "条件に一致する請求金額を取得します。",
        safety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: { columns: ["TOTAL_AMOUNT"], rows: [], total: 0 },
        timing,
      },
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill(questionText);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const progress = page.getByTestId("nl2sql-job-progress");
  const executeStep = page.getByTestId("nl2sql-job-step-execute_sql");
  const formatStep = page.getByTestId("nl2sql-job-step-format_results");

  await expect(progress).toHaveAttribute("data-job-status", "done");
  await page.getByTestId("nl2sql-job-summary").click();
  await expect(executeStep).toHaveAttribute("data-status", "done");
  await expect(executeStep).not.toHaveAttribute("aria-current", "step");
  await expect(executeStep.locator("svg.animate-spin")).toHaveCount(0);
  await expect(executeStep).toContainText("完了");
  await expect(formatStep).toHaveAttribute("data-status", "done");
  await expect(page.getByRole("heading", { name: "実行結果（0 行）" })).toBeVisible();
  // 0 行は要約だけ（表・すべての行・CSV は出さない。#1178）。
  await expect(page.getByTestId("query-results-summary")).toContainText("該当する行はありません");
  await expect(page.getByTestId("query-results-view-all")).toHaveCount(0);
  await expect(page.getByTestId("query-results-csv")).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("保存警告がある完了 job は結果を表示し、赤エラーではなく黄色 warning を出す", async ({ page }) => {
  await page.unroute("**/api/auth/me").catch(() => undefined);
  await page.route("**/api/auth/me**", (route) => fulfillJson(route, systemAdminMe));
  await page.route("**/api/auth/login**", (route) => fulfillJson(route, systemAdminMe));
  await mockNl2SqlApi(page);
  const questionText = "請求金額を確認したい";
  const createdAt = "2026-06-21T10:00:00.000Z";
  const warningMessage = "結果は生成されましたが、履歴/ジョブ保存に失敗しました。";
  const doneSteps = [
    { stage: "prepare_context", status: "done", elapsed_ms: 8 },
    { stage: "generate_sql", status: "done", elapsed_ms: 20 },
    { stage: "safety_check", status: "done", elapsed_ms: 4 },
    { stage: "execute_sql", status: "done", elapsed_ms: 12 },
    { stage: "format_results", status: "done", elapsed_ms: 6 },
  ];

  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfillJson(route, {
      job_id: "job-warning-001",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );
  await page.route("**/api/nl2sql/jobs/job-warning-001", (route) =>
    fulfillJson(route, {
      job_id: "job-warning-001",
      status: "done",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: createdAt,
      elapsed_ms: 50,
      error_message: null,
      warning_message: warningMessage,
      steps: doneSteps,
      timing,
      result: {
        history_id: "hist-warning-001",
        engine: "enterprise_ai_direct",
        engine_meta: { profile: "mock_profile" },
        fallback_reason: "",
        original_question: questionText,
        rewritten_question: questionText,
        generated_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
        executable_sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
        explanation: "請求情報を取得します。",
        safety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: {
          columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
          rows: [{ CUSTOMER_NAME: "架空商事", TOTAL_AMOUNT: 1200000 }],
          total: 1,
        },
        timing,
      },
    })
  );

  await page.goto("/query");
  const loginHeading = page.getByRole("heading", { name: "システムにログイン" });
  if (await loginHeading.isVisible({ timeout: 3000 }).catch(() => false)) {
    await page.getByLabel("ログインユーザーID").fill("SYSTEM");
    await page.getByLabel("パスワード").fill("password");
    await page.getByRole("button", { name: "ログイン" }).click();
  }
  await expect(nl2sqlQuestionInput(page)).toBeVisible();
  await nl2sqlQuestionInput(page).fill(questionText);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const progress = page.getByTestId("nl2sql-job-progress");
  await expect(progress).toHaveAttribute("data-job-status", "done");
  await expect(progress.getByRole("status").filter({ hasText: warningMessage })).toBeVisible();
  await expect(progress).not.toContainText("処理を完了できませんでした");
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test("SQL の生成と実行のジョブの失敗段階を示し、入力を保持して再実行できる", async ({ page }) => {
  await mockNl2SqlApi(page);
  const questionText = "未入金の請求を確認したい";
  const createdAt = "2026-06-21T10:00:00.000Z";

  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfillJson(route, {
      job_id: "job-error-001",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 12 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );
  await page.route("**/api/nl2sql/jobs/job-error-001", (route) =>
    fulfillJson(route, {
      job_id: "job-error-001",
      status: "error",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: createdAt,
      elapsed_ms: 80,
      result: null,
      error_message: "SQL 生成サービスに接続できませんでした。",
      timing: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 12 },
        { stage: "generate_sql", status: "error", elapsed_ms: 68 },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );

  await page.goto("/query");
  const question = nl2sqlQuestionInput(page);
  await question.fill(questionText);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const progress = page.getByTestId("nl2sql-job-progress");
  await expect(progress).toHaveAttribute("data-job-status", "error");
  await expect(progress).not.toHaveAttribute("role", "alert");
  await expect(progress.getByRole("alert")).toContainText("SQL 生成サービスに接続できませんでした。");
  await expect(page.getByTestId("nl2sql-job-step-generate_sql")).toHaveAttribute(
    "data-status",
    "failed"
  );
  await expect(progress).toContainText("SQL 生成サービスに接続できませんでした。");
  await expect(question).toHaveValue(questionText);
  await expect(page.getByRole("button", { name: "SQL を生成して実行" })).toBeEnabled();
  await expectNoHorizontalScroll(page);
});

test("Query Rewrite の用語・同義語は既定 off で、Schema オプションを表示しない", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.goto("/query");
  await expect(page.getByText("スキーマ参照")).toBeVisible();

  await expect(page.getByLabel("用語・同義語を使う")).not.toBeChecked();
  await expect(page.getByLabel("Schema を使う")).toHaveCount(0);

  // 独立した「質問を書き換え」ボタンは廃止済み（「SQL を生成して実行」に統合）
  await expect(page.getByRole("button", { name: "質問を書き換え" })).toHaveCount(0);

  // default プロファイル（allowed_tables=["INVOICES"]）で対象表に絞り込まれる
  await openSchemaPicker(page);
  await expect(page.getByRole("button", { name: "請求 を開閉" })).toBeVisible();
});

test("補助フラグ ON のとき、SQL を生成して実行すると原文と glossary flag でジョブを投入する", async ({ page }) => {
  await mockNl2SqlApi(page);
  const questionText = "請求金額を一覧で見たい";
  const rewrittenText = "請求金額を一覧で見たい（請求金額=INVOICES.TOTAL_AMOUNT）";
  const createdAt = "2026-06-21T10:00:00.000Z";

  let jobPayload: Record<string, unknown> | null = null;
  await page.route("**/api/nl2sql/jobs", (route) => {
    jobPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      job_id: "job-rewrite-001",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    });
  });
  await page.route("**/api/nl2sql/jobs/job-rewrite-001", (route) =>
    fulfillJson(route, {
      job_id: "job-rewrite-001",
      status: "done",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: createdAt,
      elapsed_ms: 20,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "done", elapsed_ms: 5 },
        { stage: "safety_check", status: "done", elapsed_ms: 2 },
        { stage: "execute_sql", status: "done", elapsed_ms: 2 },
        { stage: "format_results", status: "done", elapsed_ms: 1 },
      ],
      timing: null,
      result: {
        engine: "enterprise_ai_direct",
        original_question: questionText,
        rewritten_question: rewrittenText,
        generated_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
        executable_sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
        explanation: "請求金額を取得します。",
        safety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: { columns: ["TOTAL_AMOUNT"], rows: [{ TOTAL_AMOUNT: 1200000 }], total: 1 },
        timing: null,
      },
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill(questionText);
  await openNl2SqlExecutionOptions(page);
  await page.getByLabel("用語・同義語を使う").check();
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "done");
  // ジョブへ渡す question は原文のままにし、server 側で 1 回だけ glossary を適用する。
  expect(jobPayload).toMatchObject({
    question: questionText,
    engine: "enterprise_ai_direct",
    use_glossary: true,
  });
  // 入力欄は書き換えずユーザー入力のまま保持する
  await expect(nl2sqlQuestionInput(page)).toHaveValue(questionText);
});

test("空の抽出条件では rewrite カードを出さず、条件を増やさずジョブ投入する", async ({ page }) => {
  await mockNl2SqlApi(page);
  const questionText = '対象テーブル："部署情報を管理するテーブル"\n抽出項目：\n抽出条件：';
  const warning = "抽出条件が空欄のため条件追加を抑止しました。";
  const blockedReason = "抽出条件が空欄の質問に対して WHERE 条件が生成されたため、SQL を実行しません。";
  const generatedWhere = 'UPPER("DEPARTMENT_NAME") LIKE \'%管理部門%\'';
  const blockedSafety = {
    ...safety,
    is_safe: false,
    blocked_reason: blockedReason,
    warnings: [blockedReason],
    referenced_tables: ["ADMIN.DEPARTMENT"],
    referenced_columns: ["ADMIN.DEPARTMENT.DEPARTMENT_ID", "ADMIN.DEPARTMENT.DEPARTMENT_NAME"],
  };
  const createdAt = "2026-06-21T10:00:00.000Z";

  let jobPayload: Record<string, unknown> | null = null;
  await page.unroute("**/api/nl2sql/rewrite");
  await page.route("**/api/nl2sql/rewrite", (route) =>
    fulfillJson(route, {
      original_question: questionText,
      rewritten_question: questionText,
      source: "deterministic",
      model: "",
      warnings: [warning],
    })
  );
  await page.route("**/api/nl2sql/jobs", (route) => {
    jobPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      job_id: "job-empty-filter-rewrite-001",
      status: "running",
      created_at: createdAt,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    });
  });
  await page.route("**/api/nl2sql/jobs/job-empty-filter-rewrite-001", (route) =>
    fulfillJson(route, {
      job_id: "job-empty-filter-rewrite-001",
      status: "error",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: createdAt,
      elapsed_ms: 20,
      error_message: blockedReason,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 10 },
        { stage: "generate_sql", status: "done", elapsed_ms: 5 },
        { stage: "safety_check", status: "error", elapsed_ms: 2 },
        { stage: "execute_sql", status: "skipped", elapsed_ms: 0 },
        { stage: "format_results", status: "done", elapsed_ms: 1 },
      ],
      timing: null,
      result: {
        engine: "enterprise_ai_direct",
        original_question: questionText,
        rewritten_question: questionText,
        generated_sql: `SELECT "DEPARTMENT_ID", "DEPARTMENT_NAME" FROM "ADMIN"."DEPARTMENT" WHERE ${generatedWhere}`,
        executable_sql: "",
        explanation: blockedReason,
        safety: blockedSafety,
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: { columns: ["DEPARTMENT_ID", "DEPARTMENT_NAME"], rows: [], total: 0 },
        timing: null,
        interpretation: {
          available: true,
          warnings: [blockedReason],
          question: {
            available: true,
            source: "deterministic",
            original_question: questionText,
            rewritten_question: questionText,
            profile_id: "default",
            profile_name: "PROFILE_DEPT",
            target_objects: ["ADMIN.DEPARTMENT"],
            filters: [],
            group_by: [],
            order_by: [],
            aggregations: [],
            row_limit: null,
            confidence: 0.4,
            warnings: [blockedReason],
          },
          sql: {
            available: true,
            source: "sql_semantics",
            summary: "ADMIN.DEPARTMENT を参照し、SELECT 操作を行います。",
            statement_type: "SELECT",
            tables: ["ADMIN.DEPARTMENT"],
            columns: ["ADMIN.DEPARTMENT.DEPARTMENT_ID", "ADMIN.DEPARTMENT.DEPARTMENT_NAME"],
            joins: [],
            filters: [generatedWhere],
            aggregations: [],
            group_by: [],
            order_by: [],
            limit: null,
            semantic_graph: {},
            warnings: [blockedReason],
          },
        },
        show_prompt: null,
      },
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill(questionText);
  await openNl2SqlExecutionOptions(page);
  await page.getByLabel("用語・同義語を使う").check();
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  // 質問が無変換のときは rewrite カード自体を出さない（内部処理の warning も表に出さない）。
  await expect(page.getByText("生成に使用される質問")).toHaveCount(0);
  await expect(page.getByText(warning)).toHaveCount(0);
  await expect(page.getByText("deterministic", { exact: true })).toHaveCount(0);
  expect(jobPayload).toMatchObject({
    question: questionText,
    engine: "enterprise_ai_direct",
    use_glossary: true,
  });
  const submittedJobPayload = jobPayload as Record<string, unknown> | null;
  expect(String(submittedJobPayload?.question ?? "")).not.toContain("管理部門");
  const interpretation = page.getByTestId("nl2sql-interpretation-panel");
  await expect(interpretation.getByText("入力と SQL が一致していません")).toBeVisible();
  await expect(interpretation.getByText("入力の抽出条件は空欄ですが")).toBeVisible();
  await expect(page.getByText("入力と生成 SQL の対応")).toHaveCount(0);
  await expect(page.getByText("入力テンプレート")).toHaveCount(0);
  await expect(page.getByText("生成 SQL の意味")).toHaveCount(0);

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(page.getByText("生成に使用される質問")).toHaveCount(0);
  await expect(page.getByText(warning)).toHaveCount(0);
  await expect(interpretation.getByText("入力と SQL が一致していません")).toBeVisible();
  await expect(interpretation.getByText("入力の抽出条件は空欄ですが")).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test("生成 SQL を読み取り専用 Ontology グラフへ接地して確認できる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  const questionText = "部署ごとの従業員氏名を表示";
  const generatedSql =
    'SELECT "d"."DEPARTMENT_NAME" AS "部署名", "e"."EMPLOYEE_NAME" AS "従業員氏名", "p"."PROJECT_NAME" AS "プロジェクト名" FROM "ADMIN"."DEPARTMENT" "d" JOIN "ADMIN"."EMPLOYEE" "e" ON "e"."DEPARTMENT_ID"="d"."DEPARTMENT_ID" JOIN "ADMIN"."PROJECT" "p" ON "p"."DEPARTMENT_ID"="d"."DEPARTMENT_ID"';
  const sqlGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql: generatedSql,
    ctes: [],
    tables: [
      {
        id: "table-department",
        owner: "ADMIN",
        name: "DEPARTMENT",
        alias: "d",
        qualified_name: "ADMIN.DEPARTMENT",
        source_sql: '"ADMIN"."DEPARTMENT" "d"',
      },
      {
        id: "table-employee",
        owner: "ADMIN",
        name: "EMPLOYEE",
        alias: "e",
        qualified_name: "ADMIN.EMPLOYEE",
        source_sql: '"ADMIN"."EMPLOYEE" "e"',
      },
      {
        id: "table-project",
        owner: "ADMIN",
        name: "PROJECT",
        alias: "p",
        qualified_name: "ADMIN.PROJECT",
        source_sql: '"ADMIN"."PROJECT" "p"',
      },
    ],
    columns: [
      {
        id: "column-department-name",
        table: "d",
        name: "DEPARTMENT_NAME",
        expression_sql: '"d"."DEPARTMENT_NAME"',
      },
      {
        id: "column-employee-name",
        table: "e",
        name: "EMPLOYEE_NAME",
        expression_sql: '"e"."EMPLOYEE_NAME"',
      },
      {
        id: "column-employee-department-id",
        table: "e",
        name: "DEPARTMENT_ID",
        expression_sql: '"e"."DEPARTMENT_ID"',
      },
      {
        id: "column-department-id",
        table: "d",
        name: "DEPARTMENT_ID",
        expression_sql: '"d"."DEPARTMENT_ID"',
      },
      {
        id: "column-project-name",
        table: "p",
        name: "PROJECT_NAME",
        expression_sql: '"p"."PROJECT_NAME"',
      },
      {
        id: "column-project-department-id",
        table: "p",
        name: "DEPARTMENT_ID",
        expression_sql: '"p"."DEPARTMENT_ID"',
      },
    ],
    joins: [
      {
        id: "join-department-employee",
        left_source: '"ADMIN"."DEPARTMENT" "d"',
        right_source: '"ADMIN"."EMPLOYEE" "e"',
        join_type: "inner",
        condition_sql: '"e"."DEPARTMENT_ID"="d"."DEPARTMENT_ID"',
      },
      {
        // star join。過去 artifact 互換のため位置ベースの誤った左端点を保持する。
        id: "join-department-project",
        left_source: '"ADMIN"."EMPLOYEE" "e"',
        right_source: '"ADMIN"."PROJECT" "p"',
        join_type: "inner",
        condition_sql: '"p"."DEPARTMENT_ID"="d"."DEPARTMENT_ID"',
      },
    ],
    projections: [
      {
        id: "projection-dept",
        output_name: "部署名",
        expression_sql: '"d"."DEPARTMENT_NAME" AS "部署名"',
        referenced_columns: ["d.DEPARTMENT_NAME"],
      },
      {
        id: "projection-employee",
        output_name: "従業員氏名",
        expression_sql: '"e"."EMPLOYEE_NAME" AS "従業員氏名"',
        referenced_columns: ["e.EMPLOYEE_NAME"],
      },
      {
        id: "projection-project",
        output_name: "プロジェクト名",
        expression_sql: '"p"."PROJECT_NAME" AS "プロジェクト名"',
        referenced_columns: ["p.PROJECT_NAME"],
      },
    ],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
    parse_warnings: [],
  };
  const ontologyGraph = {
    revision: {
      id: "revision-admin-hr",
      version: 1,
      status: "published",
      schema_fingerprint: "hr-schema",
      etag: "revision-admin-hr-etag",
    },
    nodes: [
      {
        id: "department-business",
        kind: "business_entity",
        business_name_ja: "部署",
        technical_name: "department",
        review_status: "approved",
        physical_mappings: [
          {
            object_ref: {
              node_id: "department-table",
              owner: "ADMIN",
              object_name: "DEPARTMENT",
              object_type: "table",
            },
          },
        ],
      },
      {
        id: "employee-business",
        kind: "business_entity",
        business_name_ja: "従業員",
        technical_name: "employee",
        review_status: "approved",
        physical_mappings: [
          {
            object_ref: {
              node_id: "employee-table",
              owner: "ADMIN",
              object_name: "EMPLOYEE",
              object_type: "table",
            },
          },
        ],
      },
      {
        id: "department-table",
        kind: "table",
        technical_name: "ADMIN.DEPARTMENT",
        business_name_ja: "部署情報",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "DEPARTMENT" },
      },
      {
        id: "employee-table",
        kind: "table",
        technical_name: "ADMIN.EMPLOYEE",
        business_name_ja: "従業員情報",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "EMPLOYEE" },
      },
      {
        id: "project-table",
        kind: "table",
        technical_name: "ADMIN.PROJECT",
        business_name_ja: "プロジェクト情報",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "PROJECT" },
      },
      {
        id: "department-name",
        kind: "property",
        technical_name: "ADMIN.DEPARTMENT.DEPARTMENT_NAME",
        business_name_ja: "部署名",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_NAME" },
      },
      {
        id: "employee-name",
        kind: "property",
        technical_name: "ADMIN.EMPLOYEE.EMPLOYEE_NAME",
        business_name_ja: "従業員氏名",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "EMPLOYEE_NAME" },
      },
      {
        id: "department-id",
        kind: "column",
        technical_name: "ADMIN.DEPARTMENT.DEPARTMENT_ID",
        business_name_ja: "部署ID",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_ID" },
      },
      {
        id: "employee-department-id",
        kind: "column",
        technical_name: "ADMIN.EMPLOYEE.DEPARTMENT_ID",
        business_name_ja: "従業員部署ID",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "DEPARTMENT_ID" },
      },
      {
        id: "project-name",
        kind: "property",
        technical_name: "ADMIN.PROJECT.PROJECT_NAME",
        business_name_ja: "プロジェクト名",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "PROJECT", column_name: "PROJECT_NAME" },
      },
      {
        id: "project-department-id",
        kind: "column",
        technical_name: "ADMIN.PROJECT.DEPARTMENT_ID",
        business_name_ja: "部門ID",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "PROJECT", column_name: "DEPARTMENT_ID" },
      },
    ],
    edges: [
      {
        id: "department-business-map",
        kind: "physical_mapping",
        source_node_id: "department-business",
        target_node_id: "department-table",
        relationship_name_ja: "物理マッピング",
        review_status: "approved",
      },
      {
        id: "employee-business-map",
        kind: "physical_mapping",
        source_node_id: "employee-business",
        target_node_id: "employee-table",
        relationship_name_ja: "物理マッピング",
        review_status: "approved",
      },
      {
        id: "department-name-column",
        kind: "column",
        source_node_id: "department-table",
        target_node_id: "department-name",
        relationship_name_ja: "列",
        review_status: "approved",
      },
      {
        id: "employee-name-column",
        kind: "column",
        source_node_id: "employee-table",
        target_node_id: "employee-name",
        relationship_name_ja: "列",
        review_status: "approved",
      },
      {
        id: "department-employee-join",
        kind: "foreign_key",
        source_node_id: "department-table",
        target_node_id: "employee-table",
        relationship_name_ja: "部署と従業員の Join",
        review_status: "approved",
        join_conditions: [
          {
            left: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_ID" },
            right: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "DEPARTMENT_ID" },
            operator: "=",
            ordinal: 1,
          },
        ],
      },
      {
        id: "project-name-column",
        kind: "column",
        source_node_id: "project-table",
        target_node_id: "project-name",
        relationship_name_ja: "列",
        review_status: "approved",
      },
      {
        id: "department-project-join",
        kind: "foreign_key",
        source_node_id: "department-table",
        target_node_id: "project-table",
        relationship_name_ja: "部署とプロジェクトの Join",
        review_status: "approved",
        join_conditions: [
          {
            left: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_ID" },
            right: { owner: "ADMIN", object_name: "PROJECT", column_name: "DEPARTMENT_ID" },
            operator: "=",
            ordinal: 1,
          },
        ],
      },
    ],
  };

  await page.unroute("**/api/nl2sql/jobs");
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfillJson(route, {
      job_id: "job-ontology-grounding-001",
      status: "running",
      created_at: "2026-06-21T10:00:00.000Z",
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );
  const groundingJobDetail = {
      job_id: "job-ontology-grounding-001",
      status: "done",
      created_at: "2026-06-21T10:00:00.000Z",
      started_at: "2026-06-21T10:00:00.000Z",
      finished_at: "2026-06-21T10:00:00.050Z",
      elapsed_ms: 50,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "done", elapsed_ms: 20 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 12 },
        { stage: "format_results", status: "done", elapsed_ms: 6 },
      ],
      timing: null,
      result: {
        history_id: "hist-ontology-grounding-001",
        engine: "enterprise_ai_direct",
        engine_meta: { profile: "mock_agent_profile" },
        fallback_reason: "",
        original_question: questionText,
        rewritten_question: questionText,
        generated_sql: generatedSql,
        executable_sql: generatedSql,
        explanation: "部署と従業員を結合して氏名を取得します。",
        safety: {
          ...safety,
          referenced_tables: ["ADMIN.DEPARTMENT", "ADMIN.EMPLOYEE"],
          referenced_columns: [
            "ADMIN.DEPARTMENT.DEPARTMENT_NAME",
            "ADMIN.EMPLOYEE.EMPLOYEE_NAME",
            "ADMIN.EMPLOYEE.DEPARTMENT_ID",
            "ADMIN.DEPARTMENT.DEPARTMENT_ID",
          ],
        },
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: {
          columns: ["部署名", "従業員氏名"],
          rows: [{ 部署名: "開発部", 従業員氏名: "山田太郎" }],
          total: 1,
        },
        timing,
        interpretation: {
          available: true,
          question: {
            available: true,
            source: "deterministic",
            original_question: questionText,
            rewritten_question: questionText,
            profile_id: "default",
            profile_name: "PROFILE_ALL",
            profile_category: "HR_ALL",
            target_objects: ["ADMIN.DEPARTMENT", "ADMIN.EMPLOYEE"],
            filters: [],
            group_by: [],
            order_by: [],
            aggregations: [],
            row_limit: null,
            confidence: 0.9,
            warnings: [],
          },
          sql: {
            available: true,
            source: "sql_semantics",
            summary: "ADMIN.DEPARTMENT と ADMIN.EMPLOYEE を参照し、SELECT 操作を行います。",
            statement_type: "SELECT",
            tables: ["ADMIN.DEPARTMENT", "ADMIN.EMPLOYEE", "ADMIN.PROJECT"],
            columns: [
              "ADMIN.DEPARTMENT.DEPARTMENT_NAME",
              "ADMIN.EMPLOYEE.EMPLOYEE_NAME",
              "ADMIN.PROJECT.PROJECT_NAME",
            ],
            joins: [
              '"e"."DEPARTMENT_ID"="d"."DEPARTMENT_ID"',
              '"p"."DEPARTMENT_ID"="d"."DEPARTMENT_ID"',
            ],
            filters: [],
            aggregations: [],
            group_by: [],
            order_by: [],
            limit: null,
            logical_steps: [
              "ADMIN.DEPARTMENT と ADMIN.EMPLOYEE を参照し、SELECT 操作を行います。",
              '結合: "e"."DEPARTMENT_ID"="d"."DEPARTMENT_ID"',
              '結合: "p"."DEPARTMENT_ID"="d"."DEPARTMENT_ID"',
            ],
            semantic_graph: sqlGraph,
            warnings: [],
          },
          ontology_graph: ontologyGraph,
          warnings: [],
        },
        show_prompt: null,
      },
  };
  await page.route("**/api/nl2sql/jobs/job-ontology-grounding-001", (route) =>
    fulfillJson(route, groundingJobDetail)
  );
  await page.route("**/api/nl2sql/profiles/default/ontology-view", (route) =>
    fulfillJson(route, {
      profile_ontology_view: {
        id: "profile-view-admin-hr",
        profile_id: "default",
        ontology_revision_id: "revision-admin-hr",
        node_ids: ontologyGraph.nodes.map((node) => node.id),
        edge_ids: ontologyGraph.edges.map((edge) => edge.id),
      },
      ontology_graph: ontologyGraph,
      materialized: true,
      stale: false,
      warnings_ja: [],
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill(questionText);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const panel = page.getByTestId("nl2sql-sql-grounding-panel");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("SQL 全要素を接地");
  // 処理手順パネルは接地確認の下に独立表示し、番号付きで手順を示す。
  const stepsPanel = page.getByTestId("nl2sql-logical-steps-panel");
  await expect(stepsPanel).toBeVisible();
  await expect(stepsPanel).toContainText("SQL の処理手順");
  await expect(stepsPanel).toContainText(
    "ADMIN.DEPARTMENT と ADMIN.EMPLOYEE を参照し、SELECT 操作を行います。"
  );
  await expect(stepsPanel.locator("ol > li")).toHaveCount(3);
  await stepsPanel.screenshot({ path: testInfo.outputPath("sql-logical-steps.png") });
  await expect(page.getByText("入力と生成 SQL の対応")).toHaveCount(0);
  await expect(page.getByText("入力テンプレート")).toHaveCount(0);
  await expect(page.getByText("生成 SQL の意味")).toHaveCount(0);
  await expect(page.getByTestId("ontology-node-card-department-table")).toContainText("部署情報");
  await expect(page.getByTestId("ontology-node-card-employee-table")).toContainText("従業員情報");
  await expect(panel.getByTestId("nl2sql-sql-grounding-list")).toContainText("ADMIN.DEPARTMENT");
  await expect(panel.getByTestId("nl2sql-sql-grounding-list")).toContainText("部署と従業員の Join");
  // star join の 2 本目も ON 句の列対で接地する(left_source は誤ったまま)。
  await expect(page.getByTestId("ontology-node-card-project-table")).toContainText(
    "プロジェクト情報"
  );
  await expect(panel.getByTestId("nl2sql-sql-grounding-list")).toContainText(
    "部署とプロジェクトの Join"
  );

  const graphSearch = panel.getByTestId("ontology-graph-search");
  await expectLegacyOntologyControls(page, panel);
  await panel.screenshot({ path: testInfo.outputPath("sql-ontology-controls-restored.png") });
  await graphSearch.focus();
  await expect(graphSearch).toBeFocused();
  const zoomIn = panel.getByLabel("グラフを拡大");
  await zoomIn.focus();
  await expect(zoomIn).toBeFocused();

  // 線（関係）とノードの詳細は、オントロジー構築と同じ 1 つの詳細のパネルに出す（#1788）。
  const details = panel.getByTestId("nl2sql-sql-grounding-details");
  await expect(details.getByRole("heading", { name: "詳細", exact: true })).toBeVisible();
  await clickOntologyEdge(page, panel.locator('.react-flow__edge[data-id="department-employee-join"]'));
  await expect(details.getByRole("heading", { name: "関係の詳細", exact: true })).toBeVisible();
  await expect(details.getByTestId("ontology-edge-details")).toContainText("部署と従業員の Join");
  await expect(details.getByTestId("ontology-edge-details")).toContainText("結合のキー（Join Key）");
  await expect(details.getByTestId("ontology-edge-details")).toContainText("部署情報");
  await expect(details.getByTestId("ontology-edge-details")).not.toContainText("未設定");
  await details.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("sql-grounding-edge-details.png") });
  await page.getByTestId("ontology-node-card-employee-table").click();
  await expect(details.getByRole("heading", { name: "ノード詳細", exact: true })).toBeVisible();
  await expect(details.getByTestId("ontology-node-details")).toContainText("従業員情報");
  await expect(details.getByTestId("ontology-edge-details")).toHaveCount(0);

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(panel).toBeVisible();
  await expect(panel.getByText("SQL 全要素を接地")).toBeVisible();
  await expect(stepsPanel).toBeVisible();
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("sql-ontology-grounding.png"), fullPage: true });

  // 「Ontology を使う」OFF(backend echo)のときは接地確認を出さず、処理手順は残す。
  await page.unroute("**/api/nl2sql/jobs/job-ontology-grounding-001");
  await page.route("**/api/nl2sql/jobs/job-ontology-grounding-001", (route) =>
    fulfillJson(route, {
      ...groundingJobDetail,
      result: {
        ...groundingJobDetail.result,
        interpretation: {
          ...groundingJobDetail.result.interpretation,
          ontology_grounding_enabled: false,
          ontology_graph: null,
        },
      },
    })
  );
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect(page.getByTestId("nl2sql-sql-grounding-panel")).toHaveCount(0);
  await expect(stepsPanel).toBeVisible();
  await expect(stepsPanel).toContainText("SQL の処理手順");
});

test("公開されたオントロジーが無い業務プロファイルでは、接地確認をしていないことを失敗と区別して示す", async ({
  page,
}, testInfo) => {
  // #1168: 公開版が無い job はその場でオントロジーを同期・構築せず、接地確認をしない。
  await mockNl2SqlApi(page);
  const questionText = "従業員の一覧を表示";
  const generatedSql = 'SELECT "EMP"."EMPLOYEE_NAME" AS "EMPLOYEE_NAME" FROM "ADMIN"."EMPLOYEE" "EMP"';
  const sqlGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql: generatedSql,
    ctes: [],
    tables: [
      {
        id: "table-employee",
        scope_id: "scope_1",
        owner: "ADMIN",
        name: "EMPLOYEE",
        alias: "EMP",
        qualified_name: "ADMIN.EMPLOYEE",
        source_sql: '"ADMIN"."EMPLOYEE" "EMP"',
      },
    ],
    columns: [
      {
        id: "column-EMPLOYEE_NAME",
        scope_id: "scope_1",
        owner: "ADMIN",
        table: "EMPLOYEE",
        name: "EMPLOYEE_NAME",
        clause: "select",
        expression_sql: '"EMP"."EMPLOYEE_NAME"',
      },
    ],
    projections: [],
    joins: [],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
    limit: null,
  };
  const jobId = "job-grounding-not-published-001";
  const steps = [
    { stage: "prepare_context", status: "done", elapsed_ms: 8 },
    { stage: "generate_sql", status: "done", elapsed_ms: 20 },
    { stage: "safety_check", status: "done", elapsed_ms: 4 },
    { stage: "execute_sql", status: "done", elapsed_ms: 12 },
    { stage: "format_results", status: "done", elapsed_ms: 6 },
  ];

  await page.unroute("**/api/nl2sql/jobs");
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfillJson(route, {
      job_id: jobId,
      status: "running",
      created_at: "2026-06-21T10:00:00.000Z",
      steps: steps.map((step) => ({ ...step, status: "pending", elapsed_ms: null })),
    })
  );
  await page.route(`**/api/nl2sql/jobs/${jobId}`, (route) =>
    fulfillJson(route, {
      job_id: jobId,
      status: "done",
      business_release_id: "",
      created_at: "2026-06-21T10:00:00.000Z",
      started_at: "2026-06-21T10:00:00.000Z",
      finished_at: "2026-06-21T10:00:00.050Z",
      elapsed_ms: 50,
      error_message: null,
      steps,
      timing: null,
      result: {
        history_id: "hist-grounding-not-published-001",
        engine: "enterprise_ai_direct",
        engine_meta: { profile: "mock_agent_profile" },
        fallback_reason: "",
        original_question: questionText,
        rewritten_question: questionText,
        generated_sql: generatedSql,
        executable_sql: generatedSql,
        explanation: "従業員の氏名を取得します。",
        safety: {
          ...safety,
          referenced_tables: ["ADMIN.EMPLOYEE"],
          referenced_columns: ["ADMIN.EMPLOYEE.EMPLOYEE_NAME"],
        },
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: { columns: ["EMPLOYEE_NAME"], rows: [{ EMPLOYEE_NAME: "山田太郎" }], total: 1 },
        timing,
        interpretation: {
          available: true,
          question: {
            available: true,
            source: "deterministic",
            original_question: questionText,
            rewritten_question: questionText,
            profile_id: "default",
            profile_name: "PROFILE_ALL",
            profile_category: "HR_ALL",
            target_objects: ["ADMIN.EMPLOYEE"],
            filters: [],
            group_by: [],
            order_by: [],
            aggregations: [],
            row_limit: null,
            confidence: 0.9,
            warnings: [],
          },
          sql: {
            available: true,
            source: "sql_semantics",
            summary: "ADMIN.EMPLOYEE を参照し、SELECT 操作を行います。",
            statement_type: "SELECT",
            tables: ["ADMIN.EMPLOYEE"],
            columns: ["ADMIN.EMPLOYEE.EMPLOYEE_NAME"],
            joins: [],
            filters: [],
            aggregations: [],
            group_by: [],
            order_by: [],
            limit: null,
            logical_steps: [],
            semantic_graph: sqlGraph,
            warnings: [],
          },
          ontology_graph: null,
          ontology_grounding_enabled: true,
          ontology_grounding_skip_reason: "no_published_ontology",
          warnings: [],
        },
        show_prompt: null,
      },
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill(questionText);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const panel = page.getByTestId("nl2sql-sql-grounding-panel");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("公開版なし");
  await expect(panel).toContainText(
    "この業務プロファイルには公開されたオントロジーが無いため、接地確認をしていません。"
  );
  // 失敗・空のグラフ・未接地には見せない。
  await expect(panel).not.toContainText("オントロジーグラフを読み込めませんでした");
  await expect(panel).not.toContainText("公開済みオントロジーグラフがありません");
  await expect(panel).not.toContainText("未接地");
  await expect(panel).not.toContainText("未取得");
  await expect(panel.getByTestId("nl2sql-sql-grounding-graph")).toHaveCount(0);
  await expect(panel.getByRole("alert")).toHaveCount(0);
  await panel.screenshot({ path: testInfo.outputPath("sql-grounding-not-published.png") });

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(panel.getByText("公開版なし")).toBeVisible();
  await expectNoHorizontalScroll(page);
  await panel.screenshot({ path: testInfo.outputPath("sql-grounding-not-published-375.png") });

  // ダークテーマでも情報の面とバッジが読める（色だけに頼らずアイコンと文言で示す）。
  await page.emulateMedia({ colorScheme: "dark" });
  await page.evaluate(() => document.documentElement.classList.add("dark"));
  await expect(page.locator("html")).toHaveClass(/dark/);
  await expect(panel.getByText("公開版なし")).toBeVisible();
  await panel.screenshot({ path: testInfo.outputPath("sql-grounding-not-published-dark.png") });
});

test("未修飾列の単一表 SELECT は FROM 句の表だけを接地する", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  const questionText = "従業員の一覧を表示";
  // 実 SQL 相当: 別名 EMP を付けながら SELECT 列は未修飾。列名だけで ontology 全体を
  // 横断一致させると DEPARTMENT / PROJECT の同名列まで接地扱いになる回帰を防ぐ。
  const generatedSql =
    'SELECT "EMPLOYEE_ID" AS "EMPLOYEE_ID","DEPARTMENT_ID" AS "DEPARTMENT_ID","EMPLOYEE_NAME" AS "EMPLOYEE_NAME","SALARY" AS "SALARY" FROM "ADMIN"."EMPLOYEE" "EMP"';
  const columnNames = ["EMPLOYEE_ID", "DEPARTMENT_ID", "EMPLOYEE_NAME", "SALARY"];
  const sqlGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql: generatedSql,
    ctes: [],
    tables: [
      {
        id: "table-employee",
        scope_id: "scope_1",
        owner: "ADMIN",
        name: "EMPLOYEE",
        alias: "EMP",
        qualified_name: "ADMIN.EMPLOYEE",
        source_sql: '"ADMIN"."EMPLOYEE" "EMP"',
      },
    ],
    // backend の parse_oracle_sql は未修飾列を table:"" で返し、projections に同じ列を再掲する。
    columns: columnNames.map((name) => ({
      id: `column-${name}`,
      scope_id: "scope_1",
      owner: "",
      table: "",
      name,
      clause: "select",
      expression_sql: `"${name}"`,
    })),
    projections: columnNames.map((name) => ({
      id: `projection-${name}`,
      scope_id: "scope_1",
      output_name: name,
      expression_sql: `"${name}" AS "${name}"`,
      referenced_columns: [name],
    })),
    joins: [],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
    limit: null,
  };

  const businessNode = (id: string, label: string, objectName: string, nodeId: string) => ({
    id,
    kind: "business_entity",
    business_name_ja: label,
    review_status: "approved",
    physical_mappings: [
      {
        object_ref: { node_id: nodeId, owner: "ADMIN", object_name: objectName, object_type: "table" },
      },
    ],
  });
  const tableNode = (id: string, objectName: string, label: string) => ({
    id,
    kind: "table",
    technical_name: `ADMIN.${objectName}`,
    business_name_ja: label,
    review_status: "approved",
    metadata: { owner: "ADMIN", object_name: objectName },
  });
  const columnNode = (id: string, objectName: string, columnName: string, label: string) => ({
    id,
    kind: "column",
    technical_name: `ADMIN.${objectName}.${columnName}`,
    business_name_ja: label,
    review_status: "approved",
    metadata: { owner: "ADMIN", object_name: objectName, column_name: columnName },
  });
  const ontologyGraph = {
    id: "revision-admin-hr",
    nodes: [
      businessNode("employee-business", "従業員", "EMPLOYEE", "employee-table"),
      businessNode("department-business", "部署", "DEPARTMENT", "department-table"),
      businessNode("project-business", "プロジェクト", "PROJECT", "project-table"),
      tableNode("employee-table", "EMPLOYEE", "従業員情報"),
      tableNode("department-table", "DEPARTMENT", "部署情報"),
      tableNode("project-table", "PROJECT", "プロジェクト情報"),
      columnNode("employee-id", "EMPLOYEE", "EMPLOYEE_ID", "従業員ID(主キー)"),
      columnNode("employee-department-id", "EMPLOYEE", "DEPARTMENT_ID", "所属部署ID(外部キー)"),
      columnNode("employee-name", "EMPLOYEE", "EMPLOYEE_NAME", "従業員氏名"),
      columnNode("employee-salary", "EMPLOYEE", "SALARY", "給与"),
      // 同名 DEPARTMENT_ID を持つだけの無関係な列。接地してはならない。
      columnNode("department-id", "DEPARTMENT", "DEPARTMENT_ID", "部署ID(主キー)"),
      columnNode("project-department-id", "PROJECT", "DEPARTMENT_ID", "部門ID"),
    ],
    edges: [
      {
        id: "fk-employee-department",
        source_node_id: "employee-table",
        target_node_id: "department-table",
        kind: "foreign_key",
        relationship_name_ja: "所属部署",
        cardinality: "many_to_one",
        review_status: "approved",
        join_conditions: [
          {
            left: { owner: "ADMIN", object_name: "EMPLOYEE", column_name: "DEPARTMENT_ID" },
            right: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_ID" },
            operator: "=",
            ordinal: 1,
          },
        ],
      },
      {
        id: "fk-project-department",
        source_node_id: "project-table",
        target_node_id: "department-table",
        kind: "foreign_key",
        relationship_name_ja: "担当部署",
        cardinality: "many_to_one",
        review_status: "approved",
        join_conditions: [
          {
            left: { owner: "ADMIN", object_name: "PROJECT", column_name: "DEPARTMENT_ID" },
            right: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: "DEPARTMENT_ID" },
            operator: "=",
            ordinal: 1,
          },
        ],
      },
    ],
  };

  await page.unroute("**/api/nl2sql/jobs");
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfillJson(route, {
      job_id: "job-unqualified-grounding-001",
      status: "running",
      created_at: "2026-06-21T10:00:00.000Z",
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );
  await page.route("**/api/nl2sql/jobs/job-unqualified-grounding-001", (route) =>
    fulfillJson(route, {
      job_id: "job-unqualified-grounding-001",
      status: "done",
      created_at: "2026-06-21T10:00:00.000Z",
      started_at: "2026-06-21T10:00:00.000Z",
      finished_at: "2026-06-21T10:00:00.050Z",
      elapsed_ms: 50,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "done", elapsed_ms: 20 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 12 },
        { stage: "format_results", status: "done", elapsed_ms: 6 },
      ],
      timing: null,
      result: {
        history_id: "hist-unqualified-grounding-001",
        engine: "enterprise_ai_direct",
        engine_meta: { profile: "mock_agent_profile" },
        fallback_reason: "",
        original_question: questionText,
        rewritten_question: questionText,
        generated_sql: generatedSql,
        executable_sql: generatedSql,
        explanation: "従業員テーブルの一覧を取得します。",
        safety: {
          ...safety,
          referenced_tables: ["ADMIN.EMPLOYEE"],
          referenced_columns: columnNames.map((name) => `ADMIN.EMPLOYEE.${name}`),
        },
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: {
          columns: columnNames,
          rows: [
            {
              EMPLOYEE_ID: 1,
              DEPARTMENT_ID: 10,
              EMPLOYEE_NAME: "山田太郎",
              SALARY: 500000,
            },
          ],
          total: 1,
        },
        timing,
        interpretation: {
          available: true,
          question: {
            available: true,
            source: "deterministic",
            original_question: questionText,
            rewritten_question: questionText,
            profile_id: "default",
            profile_name: "PROFILE_ALL",
            profile_category: "HR_ALL",
            target_objects: ["ADMIN.EMPLOYEE"],
            filters: [],
            group_by: [],
            order_by: [],
            aggregations: [],
            row_limit: null,
            confidence: 0.9,
            warnings: [],
          },
          sql: {
            available: true,
            source: "sql_semantics",
            summary: "ADMIN.EMPLOYEE を参照し、SELECT 操作を行います。",
            statement_type: "SELECT",
            tables: ["ADMIN.EMPLOYEE"],
            columns: columnNames.map((name) => `ADMIN.EMPLOYEE.${name}`),
            joins: [],
            filters: [],
            aggregations: [],
            group_by: [],
            order_by: [],
            limit: null,
            logical_steps: ["ADMIN.EMPLOYEE を参照し、SELECT 操作を行います。"],
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
  await page.route("**/api/nl2sql/profiles/default/ontology-view", (route) =>
    fulfillJson(route, {
      profile_ontology_view: {
        id: "profile-view-admin-hr",
        profile_id: "default",
        ontology_revision_id: "revision-admin-hr",
        node_ids: ontologyGraph.nodes.map((node) => node.id),
        edge_ids: ontologyGraph.edges.map((edge) => edge.id),
      },
      ontology_graph: ontologyGraph,
      materialized: true,
      stale: false,
      warnings_ja: [],
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill(questionText);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const panel = page.getByTestId("nl2sql-sql-grounding-panel");
  await expect(panel).toBeVisible();
  // 表 1 + 列 4 = 5 件。projections[] の再掲を二重に数えない。
  await expect(panel).toContainText("SQL 全要素を接地");
  await expect(panel).toContainText("SQL 要素 5 件を接地");

  const grounded = (nodeId: string) =>
    panel.getByTestId(`ontology-node-card-${nodeId}`).getAttribute("data-ontology-node-grounded");
  for (const nodeId of [
    "employee-business",
    "employee-table",
    "employee-id",
    "employee-department-id",
    "employee-name",
    "employee-salary",
  ]) {
    await expect
      .poll(() => grounded(nodeId), { message: `接地すべきノード: ${nodeId}` })
      .toBe("true");
  }
  // FK で隣接するだけの表は文脈ノードとして残るが、接地はしない。
  await expect(panel.getByTestId("ontology-node-card-department-table")).toBeVisible();
  for (const nodeId of ["department-table", "department-id", "project-table", "project-department-id"]) {
    const card = panel.getByTestId(`ontology-node-card-${nodeId}`);
    if ((await card.count()) === 0) continue;
    await expect
      .poll(() => grounded(nodeId), { message: `接地してはならないノード: ${nodeId}` })
      .toBe("false");
  }

  await panel.screenshot({
    path: testInfo.outputPath("sql-grounding-unqualified-columns.png"),
  });

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(panel).toBeVisible();
  await expectNoHorizontalScroll(page);
  await page.screenshot({
    path: testInfo.outputPath("sql-grounding-unqualified-columns-375.png"),
    fullPage: true,
  });
});

test("別名.* の全列の SELECT は表の接地として数え、未接地の警告を出さない (#1787)", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  const questionText = "部門の全列を表示";
  const generatedSql = 'SELECT dept.* FROM "ADMIN"."DEPARTMENT" dept';
  // backend の parse_oracle_sql は `dept.*` を列名 `*` の columns[] と、contains_wildcard の
  // projection で返す。列名 `*` の列として照合すると「一部未接地」になる回帰を防ぐ。
  const sqlGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql: generatedSql,
    ctes: [],
    tables: [
      {
        id: "table-department",
        scope_id: "scope_1",
        owner: "ADMIN",
        name: "DEPARTMENT",
        alias: "dept",
        qualified_name: "ADMIN.DEPARTMENT",
        source_sql: '"ADMIN"."DEPARTMENT" dept',
        owner_quoted: true,
        name_quoted: true,
      },
    ],
    columns: [
      {
        id: "column-dept-star",
        scope_id: "scope_1",
        owner: "",
        table: "dept",
        name: "*",
        clause: "select",
        expression_sql: "dept.*",
        owner_quoted: false,
        table_quoted: false,
        name_quoted: false,
      },
    ],
    projections: [
      {
        id: "projection-dept-star",
        scope_id: "scope_1",
        output_name: "*",
        expression_sql: "dept.*",
        referenced_columns: ["dept.*"],
        contains_wildcard: true,
      },
    ],
    joins: [],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
    limit: null,
  };
  const ontologyGraph = {
    id: "revision-admin-department",
    nodes: [
      {
        id: "department-business",
        kind: "object_type",
        business_name_ja: "部門",
        review_status: "approved",
        physical_mappings: [
          {
            object_ref: {
              node_id: "department-table",
              owner: "ADMIN",
              object_name: "DEPARTMENT",
              object_type: "table",
            },
          },
        ],
      },
      {
        id: "department-table",
        kind: "table",
        technical_name: "ADMIN.DEPARTMENT",
        business_name_ja: "部門情報",
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "DEPARTMENT" },
      },
      ...[
        ["department-id", "DEPARTMENT_ID", "部門ID"],
        ["department-name", "DEPARTMENT_NAME", "部門名"],
      ].map(([id, columnName, label]) => ({
        id,
        kind: "column",
        technical_name: `ADMIN.DEPARTMENT.${columnName}`,
        business_name_ja: label,
        review_status: "approved",
        metadata: { owner: "ADMIN", object_name: "DEPARTMENT", column_name: columnName },
      })),
    ],
    edges: [],
  };
  const jobId = "job-wildcard-grounding-001";
  const steps = ["prepare_context", "generate_sql", "safety_check", "execute_sql", "format_results"];

  await page.unroute("**/api/nl2sql/jobs");
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfillJson(route, {
      job_id: jobId,
      status: "running",
      created_at: "2026-10-14T10:00:00.000Z",
      steps: steps.map((stage, index) => ({
        stage,
        status: index === 0 ? "done" : index === 1 ? "running" : "pending",
        elapsed_ms: index === 0 ? 8 : null,
      })),
    })
  );
  await page.route(`**/api/nl2sql/jobs/${jobId}`, (route) =>
    fulfillJson(route, {
      job_id: jobId,
      status: "done",
      created_at: "2026-10-14T10:00:00.000Z",
      started_at: "2026-10-14T10:00:00.000Z",
      finished_at: "2026-10-14T10:00:00.050Z",
      elapsed_ms: 50,
      error_message: null,
      steps: steps.map((stage) => ({ stage, status: "done", elapsed_ms: 8 })),
      timing: null,
      result: {
        history_id: "hist-wildcard-grounding-001",
        engine: "enterprise_ai_direct",
        engine_meta: { profile: "mock_agent_profile" },
        fallback_reason: "",
        original_question: questionText,
        rewritten_question: questionText,
        generated_sql: generatedSql,
        executable_sql: generatedSql,
        explanation: "部門の表の全列を取得します。",
        safety: {
          ...safety,
          referenced_tables: ["ADMIN.DEPARTMENT"],
          referenced_columns: ["ADMIN.DEPARTMENT.*"],
        },
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: {
          columns: ["DEPARTMENT_ID", "DEPARTMENT_NAME"],
          rows: [{ DEPARTMENT_ID: 10, DEPARTMENT_NAME: "営業部" }],
          total: 1,
        },
        timing,
        interpretation: {
          available: true,
          question: {
            available: true,
            source: "deterministic",
            original_question: questionText,
            rewritten_question: questionText,
            profile_id: "default",
            profile_name: "PROFILE_ALL",
            profile_category: "HR_ALL",
            target_objects: ["ADMIN.DEPARTMENT"],
            filters: [],
            group_by: [],
            order_by: [],
            aggregations: [],
            row_limit: null,
            confidence: 0.9,
            warnings: [],
          },
          sql: {
            available: true,
            source: "sql_semantics",
            summary: "ADMIN.DEPARTMENT を参照し、SELECT 操作を行います。",
            statement_type: "SELECT",
            tables: ["ADMIN.DEPARTMENT"],
            columns: ["ADMIN.DEPARTMENT.*"],
            joins: [],
            filters: [],
            aggregations: [],
            group_by: [],
            order_by: [],
            limit: null,
            logical_steps: ["ADMIN.DEPARTMENT を参照し、SELECT 操作を行います。"],
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
  await page.route("**/api/nl2sql/profiles/default/ontology-view", (route) =>
    fulfillJson(route, {
      profile_ontology_view: {
        id: "profile-view-admin-department",
        profile_id: "default",
        ontology_revision_id: "revision-admin-department",
        node_ids: ontologyGraph.nodes.map((node) => node.id),
        edge_ids: [],
      },
      ontology_graph: ontologyGraph,
      materialized: true,
      stale: false,
      warnings_ja: [],
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill(questionText);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const panel = page.getByTestId("nl2sql-sql-grounding-panel");
  await expect(panel).toBeVisible();
  // `dept.*` は表 ADMIN.DEPARTMENT の接地として数える（列の要素を別に足さない）。
  await expect(panel).toContainText("SQL 全要素を接地");
  await expect(panel).toContainText("SQL 要素 1 件を接地");
  await expect(panel).not.toContainText("一部の SQL 要素をオントロジーに接地できません");
  await expect(panel.getByTestId("nl2sql-sql-grounding-list")).toContainText("ADMIN.DEPARTMENT");
  await expect(panel.getByTestId("nl2sql-sql-grounding-list")).not.toContainText("dept.*");

  // グラフでは、表の全列を使われた列として示す。
  const grounded = (nodeId: string) =>
    panel.getByTestId(`ontology-node-card-${nodeId}`).getAttribute("data-ontology-node-grounded");
  for (const nodeId of ["department-business", "department-table"]) {
    await expect
      .poll(() => grounded(nodeId), { message: `接地すべきノード: ${nodeId}` })
      .toBe("true");
  }

  await panel.screenshot({ path: testInfo.outputPath("sql-grounding-wildcard.png") });
});

test("引用名の表を参照する生成 SQL は大文字の同名表・同名列を接地しない (#573)", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  const questionText = "引用名の表の金額を表示";
  const generatedSql = 'SELECT m."Amount", m.AMOUNT FROM SALES."Mixed_Case" m';
  const columnNames = ["Amount", "AMOUNT"];
  // backend の parse_oracle_sql は識別子ごとに引用の有無（*_quoted）を返す。
  const sqlGraph = {
    dialect: "oracle",
    statement_type: "SELECT",
    raw_sql: generatedSql,
    ctes: [],
    tables: [
      {
        id: "table-mixed-case",
        scope_id: "scope_1",
        owner: "SALES",
        name: "Mixed_Case",
        alias: "m",
        qualified_name: "SALES.Mixed_Case",
        source_sql: 'SALES."Mixed_Case" m',
        owner_quoted: false,
        name_quoted: true,
        resolved_owner: "SALES",
        resolved_qualified_name: 'SALES."Mixed_Case"',
      },
    ],
    columns: [
      {
        id: "column-quoted-amount",
        scope_id: "scope_1",
        owner: "",
        table: "m",
        name: "Amount",
        clause: "select",
        expression_sql: 'm."Amount"',
        owner_quoted: false,
        table_quoted: false,
        name_quoted: true,
      },
      {
        id: "column-upper-amount",
        scope_id: "scope_1",
        owner: "",
        table: "m",
        name: "AMOUNT",
        clause: "select",
        expression_sql: "m.AMOUNT",
        owner_quoted: false,
        table_quoted: false,
        name_quoted: false,
      },
    ],
    projections: [
      {
        id: "projection-quoted-amount",
        scope_id: "scope_1",
        output_name: "Amount",
        expression_sql: 'm."Amount"',
        referenced_columns: ['m."Amount"'],
      },
      {
        id: "projection-upper-amount",
        scope_id: "scope_1",
        output_name: "AMOUNT",
        expression_sql: "m.AMOUNT",
        referenced_columns: ["m.AMOUNT"],
      },
    ],
    joins: [],
    filters: [],
    aggregates: [],
    groups: [],
    having: [],
    orders: [],
    windows: [],
    limit: null,
  };
  // technical_name は backend の physical_identity_part と同じく、小文字を含む名前だけ引用する。
  const identityPart = (name: string) => (name === name.toUpperCase() ? name : `"${name}"`);
  const tableNode = (id: string, objectName: string, label: string) => ({
    id,
    kind: "table",
    technical_name: `SALES.${identityPart(objectName)}`,
    business_name_ja: label,
    review_status: "approved",
    metadata: { owner: "SALES", object_name: objectName },
  });
  const columnNode = (id: string, objectName: string, columnName: string, label: string) => ({
    id,
    kind: "column",
    technical_name: `SALES.${identityPart(objectName)}.${identityPart(columnName)}`,
    business_name_ja: label,
    review_status: "approved",
    metadata: { owner: "SALES", object_name: objectName, column_name: columnName },
  });
  const ontologyGraph = {
    id: "revision-quoted",
    nodes: [
      tableNode("quoted-table", "Mixed_Case", "引用名の売上"),
      tableNode("upper-table", "MIXED_CASE", "大文字の売上"),
      columnNode("quoted-amount", "Mixed_Case", "Amount", "引用名の金額"),
      columnNode("quoted-upper-amount", "Mixed_Case", "AMOUNT", "引用名の表の大文字列"),
      columnNode("upper-amount", "MIXED_CASE", "AMOUNT", "大文字の金額"),
    ],
    edges: [],
  };
  const jobId = "job-quoted-grounding-573";

  await page.unroute("**/api/nl2sql/jobs");
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfillJson(route, {
      job_id: jobId,
      status: "running",
      created_at: "2026-09-14T10:00:00.000Z",
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "running", elapsed_ms: null },
        { stage: "safety_check", status: "pending", elapsed_ms: null },
        { stage: "execute_sql", status: "pending", elapsed_ms: null },
        { stage: "format_results", status: "pending", elapsed_ms: null },
      ],
    })
  );
  await page.route(`**/api/nl2sql/jobs/${jobId}`, (route) =>
    fulfillJson(route, {
      job_id: jobId,
      status: "done",
      created_at: "2026-09-14T10:00:00.000Z",
      started_at: "2026-09-14T10:00:00.000Z",
      finished_at: "2026-09-14T10:00:00.050Z",
      elapsed_ms: 50,
      error_message: null,
      steps: [
        { stage: "prepare_context", status: "done", elapsed_ms: 8 },
        { stage: "generate_sql", status: "done", elapsed_ms: 20 },
        { stage: "safety_check", status: "done", elapsed_ms: 4 },
        { stage: "execute_sql", status: "done", elapsed_ms: 12 },
        { stage: "format_results", status: "done", elapsed_ms: 6 },
      ],
      timing: null,
      result: {
        history_id: "hist-quoted-grounding-573",
        engine: "enterprise_ai_direct",
        engine_meta: { profile: "mock_agent_profile" },
        fallback_reason: "",
        original_question: questionText,
        rewritten_question: questionText,
        generated_sql: generatedSql,
        executable_sql: generatedSql,
        explanation: "引用名の表の金額を取得します。",
        safety: {
          ...safety,
          referenced_tables: ['SALES."Mixed_Case"'],
          referenced_columns: ['SALES."Mixed_Case"."Amount"', 'SALES."Mixed_Case".AMOUNT'],
        },
        recommendations: [],
        repaired_sql: "",
        optimization_hints: [],
        results: { columns: columnNames, rows: [{ Amount: 100, AMOUNT: 200 }], total: 1 },
        timing,
        interpretation: {
          available: true,
          question: {
            available: true,
            source: "deterministic",
            original_question: questionText,
            rewritten_question: questionText,
            profile_id: "default",
            profile_name: "PROFILE_ALL",
            profile_category: "HR_ALL",
            target_objects: ['SALES."Mixed_Case"'],
            filters: [],
            group_by: [],
            order_by: [],
            aggregations: [],
            row_limit: null,
            confidence: 0.9,
            warnings: [],
          },
          sql: {
            available: true,
            source: "sql_semantics",
            summary: 'SALES."Mixed_Case" を参照し、SELECT 操作を行います。',
            statement_type: "SELECT",
            tables: ['SALES."Mixed_Case"'],
            columns: ['SALES."Mixed_Case"."Amount"', 'SALES."Mixed_Case".AMOUNT'],
            joins: [],
            filters: [],
            aggregations: [],
            group_by: [],
            order_by: [],
            limit: null,
            logical_steps: ['SALES."Mixed_Case" を参照し、SELECT 操作を行います。'],
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
  await page.route("**/api/nl2sql/profiles/default/ontology-view", (route) =>
    fulfillJson(route, {
      profile_ontology_view: {
        id: "profile-view-quoted",
        profile_id: "default",
        ontology_revision_id: "revision-quoted",
        node_ids: ontologyGraph.nodes.map((node) => node.id),
        edge_ids: [],
      },
      ontology_graph: ontologyGraph,
      materialized: true,
      stale: false,
      warnings_ja: [],
    })
  );

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill(questionText);
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const panel = page.getByTestId("nl2sql-sql-grounding-panel");
  await expect(panel).toBeVisible();
  // 表 1 + 列 2 = 3 件。`m.AMOUNT` は引用名の表の大文字列 AMOUNT に接地する。
  await expect(panel).toContainText("SQL 全要素を接地");
  await expect(panel).toContainText("SQL 要素 3 件を接地");

  const grounded = (nodeId: string) =>
    panel.getByTestId(`ontology-node-card-${nodeId}`).getAttribute("data-ontology-node-grounded");
  for (const nodeId of ["quoted-table", "quoted-amount", "quoted-upper-amount"]) {
    await expect
      .poll(() => grounded(nodeId), { message: `接地すべきノード: ${nodeId}` })
      .toBe("true");
  }
  for (const nodeId of ["upper-table", "upper-amount"]) {
    const card = panel.getByTestId(`ontology-node-card-${nodeId}`);
    if ((await card.count()) === 0) continue;
    await expect
      .poll(() => grounded(nodeId), { message: `接地してはならないノード: ${nodeId}` })
      .toBe("false");
  }
  const list = panel.getByTestId("nl2sql-sql-grounding-list");
  await expect(list).toContainText("引用名の売上");
  await expect(list).not.toContainText("大文字の売上");
  await expect(list).not.toContainText("大文字の金額");

  await panel.screenshot({ path: testInfo.outputPath("sql-grounding-quoted-same-name.png") });

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(panel).toBeVisible();
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("sql-grounding-quoted-same-name-375.png"), fullPage: true });
});

test("schema catalog が空のとき、ジョブ失敗からサンプルデータ投入で復旧できる", async ({ page }) => {
  await mockNl2SqlApi(page);
  let catalogPopulated = false;
  await page.unroute("**/api/schema/catalog");
  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(
      route,
      catalogPopulated ? schemaCatalog : { refreshed_at: "2026-06-21T10:00:00.000Z", tables: [] }
    )
  );
  await page.unroute("**/api/schema/objects?*");
  await page.route("**/api/schema/objects?*", (route) => {
    const tables = catalogPopulated ? schemaCatalog.tables : [];
    return fulfillJson(route, {
      items: tables.map((table) => ({
        owner: table.owner,
        object_name: table.table_name,
        object_type: table.table_type,
        logical_name: table.logical_name,
        comment: table.comment,
        row_count: table.row_count,
        column_count: table.columns.length,
        last_ddl_at: "",
      })),
      next_cursor: null,
      total: tables.length,
      catalog_version: catalogPopulated ? 2 : 1,
    });
  });
  // 絞り込みの影響を無くすため全表表示（allowed 空）のプロファイルにする
  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) =>
    fulfillJson(route, [{ ...profiles[0], allowed_tables: [], allowed_views: [] }])
  );
  await page.unroute("**/api/nl2sql/sample-data/import");
  await page.route("**/api/nl2sql/sample-data/import", (route) => {
    // SQL 生成の画面からのワンクリック投入も、サンプルデータ管理と同じ確認語で送る。
    if (route.request().postDataJSON()?.confirmation !== "ADMIN_EXECUTE") {
      return route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "unexpected confirmation" }) });
    }
    catalogPopulated = true;
    return fulfillJson(route, {
      operation: "import",
      step: "all",
      runtime: "deterministic",
      executed: true,
      objects: [],
      statements: [],
      warnings: [],
      profile_id: "default",
      timing,
    });
  });

  const createdAt = "2026-06-21T10:00:00.000Z";
  const errorSteps = [
    { stage: "prepare_context", status: "done", elapsed_ms: 10 },
    { stage: "generate_sql", status: "error", elapsed_ms: 5 },
    { stage: "safety_check", status: "pending", elapsed_ms: null },
    { stage: "execute_sql", status: "pending", elapsed_ms: null },
    { stage: "format_results", status: "pending", elapsed_ms: null },
  ];
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfillJson(route, {
      job_id: "job-empty-001",
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
  await page.route("**/api/nl2sql/jobs/job-empty-001", (route) =>
    fulfillJson(route, {
      job_id: "job-empty-001",
      status: "error",
      created_at: createdAt,
      started_at: createdAt,
      finished_at: createdAt,
      elapsed_ms: 30,
      result: null,
      error_message:
        "NL2SQL ジョブに失敗しました: Schema catalog が空です。Oracle schema を refresh するか、Data Tools から sample data を明示的に import してください。",
      timing: null,
      steps: errorSteps,
    })
  );

  await page.goto("/query");
  await openSchemaPicker(page);
  // catalog 空のときは「スキーマ未取得」+「スキーマを更新」導線を表示する。
  await expect(page.getByText(/スキーマ未取得/)).toBeVisible();

  await nl2sqlQuestionInput(page).fill("すべてプロジェクトを教えてください。");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const progress = page.getByTestId("nl2sql-job-progress");
  await expect(progress).toHaveAttribute("data-job-status", "error");
  const importButton = progress.getByRole("button", { name: "サンプルデータを投入" });
  await expect(importButton).toBeVisible();
  await importButton.click();

  // 投入後、catalog が populate され表が表示される（＝復旧）
  await expect(page.getByRole("button", { name: "請求 を開閉" })).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test("Workbench のサンプルデータ投入は executed=false を成功表示にしない", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/schema/catalog");
  await page.route("**/api/schema/catalog", (route) =>
    fulfillJson(route, { refreshed_at: "2026-06-21T10:00:00.000Z", tables: [] })
  );
  await page.unroute("**/api/schema/objects?*");
  await page.route("**/api/schema/objects?*", (route) =>
    fulfillJson(route, {
      items: [],
      next_cursor: null,
      total: 0,
      catalog_version: 1,
    })
  );
  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) =>
    fulfillJson(route, [{ ...profiles[0], allowed_tables: [], allowed_views: [] }])
  );
  await page.unroute("**/api/nl2sql/sample-data/import");
  await page.route("**/api/nl2sql/sample-data/import", (route) =>
    fulfillJson(route, {
      operation: "import",
      step: "all",
      runtime: "deterministic",
      executed: false,
      objects: [],
      statements: [],
      warnings: ["Admin SQL 実行には NL2SQL_RUNTIME_MODE=oracle が必要です。"],
      profile_id: "",
      timing,
    })
  );

  const createdAt = "2026-06-21T10:00:00.000Z";
  await page.route("**/api/nl2sql/jobs", (route) =>
    fulfillJson(route, {
      job_id: "job-empty-failed-import",
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
  await page.route("**/api/nl2sql/jobs/job-empty-failed-import", (route) =>
    fulfillJson(route, {
      job_id: "job-empty-failed-import",
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
  await nl2sqlQuestionInput(page).fill("すべてプロジェクトを教えてください。");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();

  const progress = page.getByTestId("nl2sql-job-progress");
  await expect(progress).toHaveAttribute("data-job-status", "error");
  const importButton = progress.getByRole("button", { name: "サンプルデータを投入" });
  await importButton.click();

  // 取込の失敗は押したボタンの直下（同じ失敗の案内の中）に 1 か所だけ出し、Toast とページの先頭に重ねない（#2034）。
  const importError = progress.getByTestId("nl2sql-sample-import-error");
  await expect(importError).toContainText("Admin SQL 実行には NL2SQL_RUNTIME_MODE=oracle が必要です。");
  await expect(
    page.getByText("Admin SQL 実行には NL2SQL_RUNTIME_MODE=oracle が必要です。", { exact: false })
  ).toHaveCount(1);
  const buttonBox = await importButton.boundingBox();
  const errorBox = await importError.boundingBox();
  expect(errorBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height - 1);
  await expect(page.getByText("サンプルデータを投入しました。")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "請求 を開閉" })).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("AI 活用の SELECT SQL 画面は通常 API だけを使用し、更新 SQL を拒否する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  await page.route("**/api/nl2sql/execute", (route) => {
    api.executePayload = route.request().postDataJSON() as Record<string, unknown>;
    const sql = String(api.executePayload.sql ?? "");
    if (!/^\s*(?:select|with)\b/i.test(sql)) {
      return route.fulfill({
        status: 400,
        contentType: "application/json",
        body: JSON.stringify({
          data: null,
          error_messages: ["SELECT/WITH のみ実行できます。SQL を修正して再試行してください。"],
          warning_messages: [],
        }),
      });
    }
    if (/\bEMPLOYEE\b/i.test(sql)) {
      return route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "SELECT の実行に失敗しました: ORA-01031: insufficient privileges",
        }),
      });
    }
    // 取得の上限が 1 行のときは、backend と同じく上限 + 1 行目があることを has_more で返す。
    const rowLimit = Number(api.executePayload.row_limit ?? 0);
    return fulfillJson(route, {
      columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
      rows: [{ CUSTOMER_NAME: "架空商事", TOTAL_AMOUNT: 1200000 }],
      total: 1,
      has_more: rowLimit === 1,
    });
  });

  await page.goto("/query");
  await (await openSidebarNav(page)).getByRole("link", { name: "SELECT SQL を実行" }).click();
  await expect(page).toHaveURL(/\/direct-sql$/);
  await expect(page.getByRole("heading", { level: 1, name: "SELECT SQL を実行" })).toBeVisible();

  const directSql = page.getByTestId("nl2sql-direct-sql");
  await expect(directSql).toBeVisible();

  const sqlInput = directSqlInput(page);
  const rowLimitInput = directSql.getByLabel("取得件数上限");
  const rowLimitHelper = directSql.getByText(
    "1〜100000 の整数。直接 SQL 実行では取得上限を明示してください。"
  );
  await expect(rowLimitInput).toHaveValue("100");
  await expect(rowLimitHelper).toBeVisible();
  await expect(directSql.getByRole("button", { name: "SQL 入力・結果をリセット" })).toBeDisabled();
  // SQL が空でも押せる（押すと欄の直下に理由を出す。#541）。
  await expect(directSql.getByRole("button", { name: "SQL 実行" })).toBeEnabled();
  const helperLayout = await rowLimitHelper.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    return {
      lines: range.getClientRects().length,
      height: element.clientHeight,
      lineHeight: parseFloat(getComputedStyle(element).lineHeight),
      overflowX: element.scrollWidth > element.clientWidth,
    };
  });
  // 横にスクロールさせない（375px では折り返す。横スクロールの領域はキーボードで届かず、axe の
  // scrollable-region-focusable・WCAG 1.4.10 の reflow に反する。#1671）。desktop は 1 行に収まる。
  expect(helperLayout.overflowX).toBe(false);
  if (testInfo.project.name === "mobile-375") {
    expect(helperLayout.lines).toBeGreaterThanOrEqual(1);
    await rowLimitInput.focus();
    await rowLimitInput.press("Tab");
    await expect(rowLimitHelper).not.toBeFocused();
  } else {
    expect(helperLayout.lines).toBe(1);
    expect(helperLayout.height).toBe(Math.round(helperLayout.lineHeight));
  }
  await rowLimitHelper.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("direct-sql-default-row-limit.png"), fullPage: true });
  for (const field of [rowLimitInput, rowLimitHelper]) {
    const bounds = await field.boundingBox();
    const panelBounds = await directSql.boundingBox();
    expect(bounds).not.toBeNull();
    expect(panelBounds).not.toBeNull();
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(panelBounds!.x + panelBounds!.width);
  }
  await expectRowLimitActionRow(rowLimitInput, directSql.getByRole("button", { name: "SQL 実行" }));
  await sqlInput.fill("SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES");
  await expect(directSql.getByRole("button", { name: "SQL 実行" })).toBeEnabled();
  await expect(directSql.getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: "SQL 実行" }).click();

  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toBeVisible();
  await expect(directSql.getByTestId("query-results-summary")).toHaveText("1 行・2 列");
  await expect(directSql.getByTestId("query-results-meta")).toContainText("取得上限 100 件");
  await expect(directSql.getByTestId("query-results-truncated")).toHaveCount(0);
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
  expect(api.executePayload).toEqual({
    sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
    allowed_objects: { table_names: [], columns: {} },
    row_limit: 100,
  });
  expect(api.adminExecutePayload).toBeNull();

  await sqlInput.fill("");
  const clearButton = directSql.getByRole("button", { name: "SQL 入力・結果をリセット" });
  await expect(clearButton).toBeEnabled();
  await expect(directSql.getByTestId("direct-sql-execution-activity")).toBeVisible();
  await clearButton.click();
  await expect(sqlInput).toHaveValue("");
  await expect(rowLimitInput).toHaveValue("100");
  await expect(clearButton).toBeDisabled();
  await expect(page.getByRole("heading", { name: "実行結果（1 行）" })).toHaveCount(0);
  await expect(directSql.getByTestId("direct-sql-execution-activity")).toHaveCount(0);

  // この画面は直接 SQL 実行のため、利用者が取得上限を明示してから /api/nl2sql/execute へ送る。
  await sqlInput.fill("SELECT CUSTOMER_NAME FROM INVOICES");
  await rowLimitInput.fill("");
  await expect(directSql.getByRole("button", { name: "SQL 実行" })).toBeDisabled();
  await rowLimitInput.fill("-1");
  await expect(directSql.getByRole("alert")).toContainText("取得件数上限は 1 以上 100000 以下の整数を入力してください。");
  await expect(directSql.getByRole("button", { name: "SQL 実行" })).toBeDisabled();
  await rowLimitInput.fill("0");
  await sqlInput.fill("SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES");
  await expect(directSql.getByRole("alert")).toContainText("取得件数上限は 1 以上 100000 以下の整数を入力してください。");
  await expect(directSql.getByRole("button", { name: "SQL 実行" })).toBeDisabled();
  await rowLimitInput.fill("100001");
  await expect(directSql.getByRole("alert")).toContainText("取得件数上限は 1 以上 100000 以下の整数を入力してください。");
  await expect(directSql.getByRole("button", { name: "SQL 実行" })).toBeDisabled();

  await clearButton.click();
  await rowLimitInput.fill("1");
  await sqlInput.fill("SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES");
  await page.getByRole("button", { name: "SQL 実行" }).click();
  // 上限で打ち切った結果は、要約と案内で明示する（#1164 の規則。#1178）。
  await expect(directSql.getByTestId("query-results-summary")).toContainText("先頭の 1 行を取得しました（さらに行があります）");
  await expect(directSql.getByTestId("query-results-truncated")).toContainText("1 回に取得するのは先頭の 1 行までです。");
  await expectBoundedRowLimit(rowLimitInput, directSql.getByRole("button", { name: "SQL 実行" }));
  await rowLimitInput.fill("100000");
  await directSql.getByRole("button", { name: "SQL 実行" }).click();
  await expect(directSql.getByTestId("query-results-meta")).toContainText("取得上限 100000 件");

  await clearButton.click();
  await rowLimitInput.fill("100");
  await sqlInput.fill("UPDATE INVOICES SET STATUS = 'REVIEWED' WHERE INVOICE_ID = 1");
  await page.getByRole("button", { name: "SQL 実行" }).click();
  await expect(page.getByRole("alert")).toContainText("SELECT/WITH のみ実行できます。");
  await expect(directSql.getByTestId("direct-sql-processing-error")).toContainText(
    "SELECT/WITH のみ実行できます。"
  );
  expect(api.adminExecutePayload).toBeNull();

  await clearButton.click();
  await rowLimitInput.fill("100");
  await sqlInput.fill("select * from employee");
  await page.getByRole("button", { name: "SQL 実行" }).click();
  await expect(page).toHaveURL(/\/direct-sql$/);
  await expect(directSql.getByTestId("direct-sql-execution-activity")).toContainText("失敗");
  await expect(directSql.getByTestId("direct-sql-processing-error")).toContainText(
    "SELECT の実行に失敗しました: ORA-01031"
  );
  await expect(page).not.toHaveURL(/\/forbidden$/);

  await expectNoHorizontalScroll(page);
  await page.setViewportSize({ width: 375, height: 900 });
  await expect(rowLimitHelper).toBeVisible();
  for (const field of [rowLimitInput, rowLimitHelper]) {
    const bounds = await field.boundingBox();
    const panelBounds = await directSql.boundingBox();
    expect(bounds).not.toBeNull();
    expect(panelBounds).not.toBeNull();
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(panelBounds!.x + panelBounds!.width);
  }
  await expectNoHorizontalScroll(page);
});

test("SQL 実行中は主ボタンだけが動的 spinner を表示する", async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.unroute("**/api/nl2sql/execute");
  const directGate = createRequestGate();
  await page.route("**/api/nl2sql/execute", async (route) => {
    await directGate.promise;
    return fulfillJson(route, {
      columns: ["CUSTOMER_NAME"],
      rows: [{ CUSTOMER_NAME: "架空商事" }],
      total: 1,
    });
  });

  await page.goto("/direct-sql");
  const directSql = page.getByTestId("nl2sql-direct-sql");
  await directSqlInput(directSql).fill("SELECT CUSTOMER_NAME FROM INVOICES");
  await directSql.getByLabel("取得件数上限").fill("100");
  const directExecuteButton = directSql.getByRole("button", { name: "SQL 実行" });
  const directRequest = page.waitForRequest("**/api/nl2sql/execute");
  await directExecuteButton.click();
  await directRequest;
  try {
    const directProcessing = directSql.getByTestId("direct-sql-execution-activity");
    await expect(directExecuteButton.locator("svg.animate-spin")).toHaveCount(1);
    await expect(directProcessing).toContainText("SQL を実行しています");
    await expect(directProcessing.locator("svg.animate-spin")).toHaveCount(0);
    await expect(directProcessing.getByRole("timer")).toHaveAccessibleName(/経過時間 \d{2}:\d{2}/);
    await expectCompactExecutionActivity(directProcessing);
    await expect(directSql.getByTestId("direct-sql-processing-region")).toHaveCount(0);
    await expectNoHorizontalScroll(page);
  } finally {
    directGate.release();
  }
  await expect(directSql.getByRole("heading", { name: "実行結果（1 行）" })).toBeVisible();
  await expect(directSql.getByTestId("direct-sql-execution-activity").getByRole("timer")).toHaveAccessibleName(
    /処理時間 \d{2}:\d{2}/
  );
  await expectCompactExecutionActivity(directSql.getByTestId("direct-sql-execution-activity"));

  await page.unroute("**/api/nl2sql/db-admin/execute");
  const adminGate = createRequestGate();
  await page.route("**/api/nl2sql/db-admin/execute", async (route) => {
    await adminGate.promise;
    return fulfillJson(route, {
      executed: true,
      runtime: "oracle",
      select_result: {
        columns: ["CUSTOMER_NAME"],
        rows: [{ CUSTOMER_NAME: "架空商事" }],
        total: 1,
      },
      statements: [
        {
          index: 1,
          statement_type: "SELECT",
          status: "executed",
          sql: "SELECT CUSTOMER_NAME FROM INVOICES",
          row_count: 1,
          message: "1 rows",
          elapsed_ms: 0,
          error_message: "",
        },
      ],
      committed: false,
      rolled_back: false,
      warnings: [],
      timing,
    });
  });

  await page.goto("/admin-sql");
  const adminSql = page.getByTestId("nl2sql-admin-sql");
  await adminSqlInput(adminSql).fill("SELECT CUSTOMER_NAME FROM INVOICES");
  const adminExecuteButton = adminSql.getByRole("button", { name: "SQL 実行" });
  const adminRequest = page.waitForRequest("**/api/nl2sql/db-admin/execute");
  await adminExecuteButton.focus();
  await adminExecuteButton.press("Enter");
  await adminRequest;
  try {
    const adminProcessing = adminSql.getByTestId("admin-sql-execution-activity");
    await expect(adminExecuteButton.locator("svg.animate-spin")).toHaveCount(1);
    await expect(adminProcessing).toContainText("SQL を実行しています");
    await expect(adminProcessing.locator("svg.animate-spin")).toHaveCount(0);
    await expect(adminProcessing.getByRole("timer")).toHaveAccessibleName(/経過時間 \d{2}:\d{2}/);
    await expectCompactExecutionActivity(adminProcessing);
    await expect(adminSql.getByTestId("admin-sql-processing-region")).toHaveCount(0);
    await expectNoHorizontalScroll(page);
  } finally {
    adminGate.release();
  }
  await expect(adminSql.getByTestId("query-results-table")).toBeVisible();
  await expect(adminSql.getByTestId("admin-sql-execution-activity").getByRole("timer")).toHaveAccessibleName(
    /処理時間 \d{2}:\d{2}/
  );
  await expectCompactExecutionActivity(adminSql.getByTestId("admin-sql-execution-activity"));
});

test("SQL 再実行は main スクロールを先頭へ戻さず結果領域を更新する", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 430 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockNl2SqlApi(page);

  const rows = Array.from({ length: 16 }, (_, index) => ({
    CUSTOMER_NAME: `架空商事 ${String(index + 1).padStart(2, "0")}`,
    TOTAL_AMOUNT: 1_200_000 + index,
  }));

  await page.unroute("**/api/nl2sql/execute");
  let directRequestCount = 0;
  const directGate = createRequestGate();
  await page.route("**/api/nl2sql/execute", async (route) => {
    directRequestCount += 1;
    if (directRequestCount === 2) await directGate.promise;
    return fulfillJson(route, {
      columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
      rows,
      total: rows.length,
    });
  });

  await page.goto("/direct-sql");
  const directSql = page.getByTestId("nl2sql-direct-sql");
  await directSqlInput(directSql).fill("SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES");
  await directSql.getByLabel("取得件数上限").fill("100");
  const directExecuteButton = directSql.getByRole("button", { name: "SQL 実行" });
  await directExecuteButton.click();
  await expect(directSql.getByRole("heading", { name: "実行結果（16 行）" })).toBeVisible();

  await directExecuteButton.scrollIntoViewIfNeeded();
  await expectMainScrolledBelowTop(page);
  const directRequest = page.waitForRequest("**/api/nl2sql/execute");
  await directExecuteButton.click();
  await directRequest;
  try {
    await expect(directSql.getByTestId("direct-sql-execution-activity")).toBeVisible();
    await expectMainScrolledBelowTop(page);
  } finally {
    directGate.release();
  }
  await expect(directSql.getByRole("heading", { name: "実行結果（16 行）" })).toBeVisible();
  await expectMainScrolledBelowTop(page);

  await page.unroute("**/api/nl2sql/db-admin/execute");
  let adminRequestCount = 0;
  const adminGate = createRequestGate();
  await page.route("**/api/nl2sql/db-admin/execute", async (route) => {
    adminRequestCount += 1;
    if (adminRequestCount === 2) await adminGate.promise;
    return fulfillJson(route, {
      executed: true,
      runtime: "oracle",
      select_result: {
        columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
        rows,
        total: rows.length,
      },
      statements: [
        {
          index: 1,
          statement_type: "SELECT",
          status: "executed",
          sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
          row_count: rows.length,
          message: `${rows.length} rows`,
          elapsed_ms: 0,
          error_message: "",
        },
      ],
      committed: false,
      rolled_back: false,
      warnings: [],
      timing,
    });
  });

  await page.goto("/admin-sql");
  const adminSql = page.getByTestId("nl2sql-admin-sql");
  await adminSqlInput(adminSql).fill("SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES");
  const adminExecuteButton = adminSql.getByRole("button", { name: "SQL 実行" });
  await adminExecuteButton.click();
  await expect(adminSql.getByTestId("query-results-table")).toBeVisible();

  await adminExecuteButton.scrollIntoViewIfNeeded();
  await expectMainScrolledBelowTop(page);
  const adminRequest = page.waitForRequest("**/api/nl2sql/db-admin/execute");
  await adminExecuteButton.click();
  await adminRequest;
  try {
    await expect(adminSql.getByTestId("admin-sql-execution-activity")).toBeVisible();
    await expectMainScrolledBelowTop(page);
  } finally {
    adminGate.release();
  }
  await expect(adminSql.getByTestId("query-results-table")).toBeVisible();
  await expectMainScrolledBelowTop(page);
});

// #1746: 取得件数上限の欄は xs の幅（8rem）。ラベル「取得件数上限」と「必須」は欄の幅で折り返さず 1 行に出る。
// 欄の幅は xs のまま（375px は全幅）で、ラベルが画面をはみ出さない。
test("管理 SQL・SELECT SQL の取得件数上限のラベルは欄の幅で折り返さず 1 行に出る", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  const narrow = (page.viewportSize()?.width ?? 1280) < 640;
  for (const [path, testId] of [
    ["/admin-sql", "nl2sql-admin-sql"],
    ["/direct-sql", "nl2sql-direct-sql"],
  ] as const) {
    await page.goto(path);
    const panel = page.getByTestId(testId);
    const rowLimitInput = panel.getByRole("spinbutton", { name: "取得件数上限", exact: true });
    await expect(rowLimitInput).toHaveValue("100");
    await expectFieldLabelOnOneLine(rowLimitInput);
    const inputWidth = (await rowLimitInput.boundingBox())!.width;
    if (narrow) {
      // 375px は全幅（行のセルの幅）。
      const cellWidth = await rowLimitInput.evaluate((element) => element.parentElement!.parentElement!.parentElement!.clientWidth);
      expect(inputWidth).toBeGreaterThan(cellWidth - 1);
    } else {
      // 欄の幅は xs（8rem）のまま。ラベルの幅に合わせて広げない。
      const xsWidth = await page.evaluate(() => 8 * Number.parseFloat(getComputedStyle(document.documentElement).fontSize));
      expect(Math.abs(inputWidth - xsWidth)).toBeLessThanOrEqual(1);
      const label = page.locator(`label[for="${await rowLimitInput.getAttribute("id")}"]`);
      expect((await label.boundingBox())!.width).toBeGreaterThan(xsWidth);
    }
    await rowLimitInput.locator("xpath=../../..").screenshot({ path: testInfo.outputPath(`${testId}-row-limit.png`) });
  }
});

test("データ準備の管理 SQL 画面は SELECT と確認済み更新 SQL を実行する", { tag: "@responsive" }, async ({ page }) => {
  const api = await mockNl2SqlApi(page);

  await page.goto("/query");
  await openSidebarLink(page, "管理 SQL を実行");
  await expect(page).toHaveURL(/\/admin-sql$/);
  await expect(page.getByRole("heading", { level: 1, name: "管理 SQL を実行" })).toBeVisible();

  const adminSql = page.getByTestId("nl2sql-admin-sql");
  const sqlInput = adminSqlInput(adminSql);
  const rowLimitInput = adminSql.getByLabel("取得件数上限");
  await expect(rowLimitInput).toHaveValue("100");
  await expectRowLimitActionRow(rowLimitInput, adminSql.getByRole("button", { name: "SQL 実行" }));
  await sqlInput.fill("SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES");
  await adminSql.getByRole("button", { name: "SQL 実行" }).click();
  await expect(adminSql.getByTestId("query-results-table")).toBeVisible();
  await expect(adminSql.getByTestId("query-results-summary")).toContainText("1 行・");
  await expect(adminSql.getByTestId("query-results-meta")).toContainText("取得上限 100 件");
  await expect(adminSql.getByRole("cell", { name: "架空商事" })).toBeVisible();
  await expect(
    adminSql.locator("code").filter({ hasText: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES" })
  ).toHaveCount(0);
  expect(api.adminExecutePayload).toEqual({
    sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
    row_limit: 100,
    confirmation: "",
    reason: "admin-sql-select",
  });

  await adminSql.getByRole("button", { name: "SQL 入力・結果をリセット" }).click();
  await expect(sqlInput).toHaveValue("");
  await expect(rowLimitInput).toHaveValue("100");
  await expect(adminSql.getByTestId("query-results-table")).toHaveCount(0);

  await sqlInput.fill("SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES");
  await expectBoundedRowLimit(rowLimitInput, adminSql.getByRole("button", { name: "SQL 実行" }));
  await rowLimitInput.fill("100000");
  await adminSql.getByRole("button", { name: "SQL 実行" }).click();
  await expect(adminSql.getByTestId("query-results-meta")).toContainText("取得上限 100000 件");
  expect(api.adminExecutePayload).toEqual({
    sql: "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES",
    row_limit: 100000,
    confirmation: "",
    reason: "admin-sql-select",
  });

  await adminSql.getByRole("button", { name: "SQL 入力・結果をリセット" }).click();
  const literalSelectSql =
    "SELECT CUSTOMER_NAME, TOTAL_AMOUNT FROM INVOICES " +
    "WHERE MEMO = 'a;b' AND STATUS = 'delete'";
  await sqlInput.fill(literalSelectSql);
  await expect(adminSql.getByLabel("実行確認語")).toHaveCount(0);
  await expect(rowLimitInput).toBeVisible();
  await rowLimitInput.fill("250");
  await adminSql.getByRole("button", { name: "SQL 実行" }).click();
  await expect(adminSql.getByTestId("query-results-meta")).toContainText("取得上限 250 件");
  expect(api.adminExecutePayload).toEqual({
    sql: literalSelectSql,
    row_limit: 250,
    confirmation: "",
    reason: "admin-sql-select",
  });

  await adminSql.getByRole("button", { name: "SQL 入力・結果をリセット" }).click();
  await expect(rowLimitInput).toHaveValue("100");
  await adminSql.getByLabel("SQL ファイル読込 (.sql/.txt)").setInputFiles({
    name: "review-invoices.sql",
    mimeType: "text/plain",
    buffer: Buffer.from("UPDATE INVOICES SET STATUS = 'REVIEWED' WHERE INVOICE_ID = 1"),
  });
  await expect(sqlInput).toHaveValue(
    "UPDATE INVOICES SET STATUS = 'REVIEWED' WHERE INVOICE_ID = 1"
  );
  const removedAdminHint = adminSql.getByText(
    /非 SELECT \/ 複数 statement は管理 SQL として扱います/
  );
  await expect(removedAdminHint).toHaveCount(0);
  await expect(
    adminSql.getByText(
      "非 SELECT / 複数 statement は ADMIN_EXECUTE を入力すると実行できます。INSERT / UPDATE / DELETE / MERGE / TRUNCATE のみの場合は、成功した SQL をコミットします。"
    )
  ).toBeVisible();
  await expect(adminSql.getByLabel("取得件数上限")).toHaveCount(0);
  for (const label of ["INSERT(単一行)", "INSERT(複数行)", "UPDATE", "DELETE", "MERGE"]) {
    await expect(adminSql.getByRole("button", { name: label, exact: true })).toHaveCount(0);
  }
  await expect(adminSql.getByLabel("実行確認語")).toBeVisible();
  const executeButton = adminSql.getByRole("button", { name: "SQL 実行" });
  await expect(executeButton).toBeDisabled();
  await adminSql.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await executeButton.focus();
  await expect(executeButton).toBeFocused();
  await executeButton.press("Enter");

  await expect(adminSql.getByText("コミット済み")).toBeVisible();
  await expect(adminSql.getByText("影響件数 2 件")).toBeVisible();
  expect(api.adminExecutePayload).toEqual({
    sql: "UPDATE INVOICES SET STATUS = 'REVIEWED' WHERE INVOICE_ID = 1",
    row_limit: 100,
    confirmation: "ADMIN_EXECUTE",
    reason: "admin-sql-admin",
  });

  await adminSql.getByRole("button", { name: "SQL 入力・結果をリセット" }).click();
  const withUpdateSql =
    "WITH TARGET AS (SELECT INVOICE_ID FROM INVOICES WHERE STATUS = 'NEW') " +
    "UPDATE INVOICES SET STATUS = 'REVIEWED' WHERE INVOICE_ID IN (SELECT INVOICE_ID FROM TARGET)";
  await sqlInput.fill(withUpdateSql);
  await expect(adminSql.getByLabel("実行確認語")).toBeVisible();
  await expect(executeButton).toBeDisabled();
  await adminSql.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await executeButton.click();
  await expect(adminSql.getByText("コミット済み")).toBeVisible();
  expect(api.adminExecutePayload).toEqual({
    sql: withUpdateSql,
    row_limit: 100,
    confirmation: "ADMIN_EXECUTE",
    reason: "admin-sql-admin",
  });

  for (const managedSql of [
    "CREATE TABLE REVIEW_QUEUE (ID NUMBER)",
    "UPDATE INVOICES SET STATUS = 'REVIEWED'; DELETE FROM REVIEW_QUEUE WHERE ID = 1",
  ]) {
    await adminSql.getByRole("button", { name: "SQL 入力・結果をリセット" }).click();
    await sqlInput.fill(managedSql);
    await expect(removedAdminHint).toHaveCount(0);
    await expect(adminSql.getByLabel("実行確認語")).toBeVisible();
    await expect(executeButton).toBeDisabled();
  }

  await expectNoHorizontalScroll(page);
  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
});

test("管理 SQL の純 DML バッチは部分成功を警告し、成功文だけコミットする", async ({ page }) => {
  const api = await mockNl2SqlApi(page);
  await page.goto("/admin-sql");

  const adminSql = page.getByTestId("nl2sql-admin-sql");
  const sqlInput = adminSqlInput(adminSql);
  const partialSql =
    "INSERT INTO INVOICES (CUSTOMER_NAME) VALUES ('架空商事'); " +
    "UPDATE MISSING_TABLE SET STATUS = 'REVIEWED' WHERE ID = 1";
  await sqlInput.fill(partialSql);
  await adminSql.getByLabel("実行確認語").fill("ADMIN_EXECUTE");

  const executeButton = adminSql.getByRole("button", { name: "SQL 実行" });
  await executeButton.focus();
  await executeButton.press("Enter");

  await expect(adminSql.getByText("一部実行", { exact: true })).toBeVisible();
  await expect(adminSql.getByText("コミット済み", { exact: true })).toBeVisible();
  const partialWarning = adminSql.getByRole("status").filter({
    hasText: "部分的に成功しました（1/2 件）。成功した SQL はコミット済みです。",
  });
  await expect(partialWarning).toBeVisible();
  await expect(partialWarning.locator("svg")).toHaveCount(1);
  await expect(adminSql.getByText("成功", { exact: true })).toBeVisible();
  await expect(adminSql.getByText("エラー", { exact: true })).toBeVisible();
  await expect(
    adminSql.getByText("ORA-00942: table or view does not exist", { exact: true }).first()
  ).toBeVisible();
  expect(api.adminExecutePayload).toEqual({
    sql: partialSql,
    row_limit: 100,
    confirmation: "ADMIN_EXECUTE",
    reason: "admin-sql-admin",
  });

  await expectNoHorizontalScroll(page);
  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
});

test("管理 SQL のコミット後はデータ管理のオブジェクト一覧を再取得する", async ({ page }) => {
  let objectRequestCount = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/nl2sql/db-admin/objects") {
      objectRequestCount += 1;
    }
  });
  await mockNl2SqlApi(page);
  await page.goto("/data-management");
  await expect(page.getByTestId("data-management-preview-grid")).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(objectRequestCount).toBeGreaterThan(0);
  const initialObjectRequestCount = objectRequestCount;

  await openSidebarLink(page, "管理 SQL を実行");
  const adminSql = page.getByTestId("nl2sql-admin-sql");
  await adminSqlInput(adminSql).fill("UPDATE INVOICES SET STATUS = 'REVIEWED' WHERE INVOICE_ID = 1");
  await adminSql.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await adminSql.getByRole("button", { name: "SQL 実行" }).click();
  await expect(adminSql.getByText("コミット済み", { exact: true })).toBeVisible();

  await (await openSidebarNav(page)).getByRole("link", { name: "データの管理" }).click();
  await expect(page).toHaveURL(/\/data-management$/);
  await expect.poll(() => objectRequestCount).toBeGreaterThan(initialObjectRequestCount);
});

test("管理 SQL の CREATE TABLE 後はDB構造差分同期を表示し完了後に消す", async ({ page }) => {
  await mockNl2SqlApi(page);
  let schemaRefreshPolls = 0;
  await page.unroute("**/api/nl2sql/db-admin/execute");
  await page.route("**/api/nl2sql/db-admin/execute", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    const sql = String(payload.sql ?? "CREATE TABLE ADMIN_SQL_TABLE (ID NUMBER)");
    await fulfillJson(route, {
      executed: true,
      runtime: "oracle",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "CREATE",
          status: "success",
          sql,
          row_count: null,
          message: "OK",
          elapsed_ms: 1,
          error_message: "",
        },
      ],
      committed: true,
      rolled_back: false,
      schema_refresh_job_id: "admin-sql-schema-refresh-done",
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/schema/refresh-jobs/admin-sql-schema-refresh-done", async (route) => {
    schemaRefreshPolls += 1;
    const done = schemaRefreshPolls >= 2;
    await fulfillJson(route, {
      job_id: "admin-sql-schema-refresh-done",
      status: done ? "done" : "running",
      mode: "targeted",
      source: "db_admin_execute",
      target_objects: [
        {
          owner: "APP",
          object_name: "ADMIN_SQL_TABLE",
          object_type: "table",
          expected_state: "present",
        },
      ],
      requires_full_refresh: false,
      phase: done ? "done" : "fetching",
      created_at: "2026-07-22T00:00:00.000Z",
      scanned_objects: done ? 1 : 0,
      changed_objects: done ? 1 : 0,
      deleted_objects: 0,
      catalog_version: done ? 2 : 0,
      error_code: "",
    });
  });

  await page.goto("/admin-sql");
  const adminSql = page.getByTestId("nl2sql-admin-sql");
  await adminSqlInput(adminSql).fill("CREATE TABLE ADMIN_SQL_TABLE (ID NUMBER)");
  await adminSql.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await adminSql.getByRole("button", { name: "SQL 実行" }).click();

  await expect(adminSql.getByText("コミット済み", { exact: true })).toBeVisible();
  await expect(
    adminSql.getByTestId("admin-sql-processing-region").getByText("CREATE TABLE ADMIN_SQL_TABLE")
  ).toBeVisible();
  const schemaSync = adminSql.getByTestId("admin-sql-schema-refresh-processing");
  await expect(schemaSync).toContainText("DB 構造の差分を同期しています");
  await expect.poll(() => schemaRefreshPolls).toBeGreaterThanOrEqual(2);
  await expect(schemaSync).toHaveCount(0);
});

test("管理 SQL の差分同期不整合はDB構造再取得CTAを表示する", async ({ page }) => {
  await mockNl2SqlApi(page);
  let manualRefreshStarted = 0;
  let manualRefreshPolls = 0;
  const manualRefreshGate = createRequestGate();
  await page.unroute("**/api/nl2sql/db-admin/execute");
  await page.route("**/api/nl2sql/db-admin/execute", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    const sql = String(payload.sql ?? "CREATE TABLE ADMIN_SQL_TABLE (ID NUMBER)");
    await fulfillJson(route, {
      executed: true,
      runtime: "oracle",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "CREATE",
          status: "success",
          sql,
          row_count: null,
          message: "OK",
          elapsed_ms: 1,
          error_message: "",
        },
      ],
      committed: true,
      rolled_back: false,
      schema_refresh_job_id: "admin-sql-schema-refresh-error",
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/schema/refresh-jobs/admin-sql-schema-refresh-error", async (route) => {
    await fulfillJson(route, {
      job_id: "admin-sql-schema-refresh-error",
      status: "error",
      mode: "targeted",
      source: "db_admin_execute",
      target_objects: [
        {
          owner: "APP",
          object_name: "ADMIN_SQL_TABLE",
          object_type: "table",
          expected_state: "present",
        },
      ],
      requires_full_refresh: true,
      phase: "fetching",
      created_at: "2026-07-22T00:00:00.000Z",
      scanned_objects: 1,
      changed_objects: 0,
      deleted_objects: 0,
      catalog_version: 0,
      error_code: "schema_refresh_full_required",
    });
  });
  await page.route("**/api/schema/refresh-jobs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    manualRefreshStarted += 1;
    await fulfillJson(route, {
      job_id: "admin-sql-manual-full-refresh",
      status: "pending",
      mode: "full",
      source: "manual",
      target_objects: [],
      requires_full_refresh: false,
      phase: "queued",
      created_at: "2026-07-22T00:00:01.000Z",
      scanned_objects: 0,
      changed_objects: 0,
      deleted_objects: 0,
      catalog_version: null,
      error_code: "",
    });
  });
  await page.route("**/api/schema/refresh-jobs/admin-sql-manual-full-refresh", async (route) => {
    manualRefreshPolls += 1;
    if (manualRefreshPolls === 1) {
      await fulfillJson(route, {
        job_id: "admin-sql-manual-full-refresh",
        status: "running",
        mode: "full",
        source: "manual",
        target_objects: [],
        requires_full_refresh: false,
        phase: "fetching",
        created_at: "2026-07-22T00:00:01.000Z",
        scanned_objects: 1,
        changed_objects: 0,
        deleted_objects: 0,
        catalog_version: null,
        error_code: "",
      });
      return;
    }
    await manualRefreshGate.promise;
    await fulfillJson(route, {
      job_id: "admin-sql-manual-full-refresh",
      status: "done",
      mode: "full",
      source: "manual",
      target_objects: [],
      requires_full_refresh: false,
      phase: "done",
      created_at: "2026-07-22T00:00:01.000Z",
      scanned_objects: 2,
      changed_objects: 1,
      deleted_objects: 0,
      catalog_version: 2,
      error_code: "",
    });
  });

  await page.goto("/admin-sql");
  const adminSql = page.getByTestId("nl2sql-admin-sql");
  await adminSqlInput(adminSql).fill("CREATE TABLE ADMIN_SQL_TABLE (ID NUMBER)");
  await adminSql.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await adminSql.getByRole("button", { name: "SQL 実行" }).click();

  await expect(adminSql.getByText("コミット済み", { exact: true })).toBeVisible();
  await expect(adminSql).toContainText("DB 構造の差分同期で不整合を検出しました。");
  const refreshButton = adminSql.getByRole("button", { name: "DB 構造を再取得" });
  await expect(refreshButton).toBeVisible();
  await refreshButton.click();
  await expect.poll(() => manualRefreshStarted).toBe(1);
  await expect(refreshButton).toBeDisabled();
  const schemaSync = adminSql.getByTestId("admin-sql-schema-refresh-processing");
  await expect(schemaSync).toContainText("DB 構造を再取得しています");
  // 進行は押したボタンと同じ案内の中に出る（ページの末尾に出さない。#2031）。
  const notice = adminSql.getByTestId("admin-sql-schema-sync-notice");
  await expect(notice.getByTestId("admin-sql-schema-refresh-processing")).toBeVisible();
  // job の間はボタンを回さず無効にするだけ。スピナーは進行の表示の 1 つだけ（#821）。
  await expect(refreshButton).not.toHaveAttribute("aria-busy", "true");
  await expectSingleSpinner(page, schemaSync);
  await expectNoHorizontalScroll(page);

  manualRefreshGate.release();
  await expect.poll(() => manualRefreshPolls).toBeGreaterThanOrEqual(2);
  await expect(schemaSync).toHaveCount(0);
  // 再取得が終わったら、再取得を求める案内も消える（#2031）。
  await expect(notice).toHaveCount(0);
  await expect(adminSql.getByText("DB 構造の差分同期で不整合を検出しました。")).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

/** 管理 SQL の実行が「差分同期の対象を特定できない」（schema_refresh_required）を返す。 */
async function routeAdminSqlTargetUnresolved(page: Page) {
  await page.unroute("**/api/nl2sql/db-admin/execute");
  await page.route("**/api/nl2sql/db-admin/execute", async (route) => {
    await fulfillJson(route, {
      executed: true,
      runtime: "oracle",
      select_result: null,
      statements: [
        { index: 1, statement_type: "DROP", status: "success", sql: "DROP INDEX SAMPLE_IDX", row_count: null, message: "OK", elapsed_ms: 1, error_message: "" },
        { index: 2, statement_type: "CREATE", status: "success", sql: "CREATE VECTOR INDEX SAMPLE_IDX ON SAMPLE_TABLE (EMBEDDING)", row_count: null, message: "OK", elapsed_ms: 1, error_message: "" },
      ],
      committed: true,
      rolled_back: false,
      schema_refresh_job_id: "",
      schema_refresh_required: true,
      schema_refresh_reason_code: "schema_refresh_target_unresolved",
      warnings: [],
      timing,
    });
  });
}

function fullRefreshJob(status: "pending" | "running" | "done" | "error", errorCode = "") {
  return {
    job_id: "admin-sql-unresolved-full-refresh",
    status,
    mode: "full",
    source: "manual",
    target_objects: [],
    requires_full_refresh: false,
    phase: status === "done" ? "done" : status === "pending" ? "queued" : "scanning",
    created_at: "2026-07-22T00:00:01.000Z",
    scanned_objects: 0,
    changed_objects: 0,
    deleted_objects: 0,
    catalog_version: null,
    error_code: errorCode,
  };
}

async function runUnresolvedAdminSql(page: Page) {
  await page.goto("/admin-sql");
  const adminSql = page.getByTestId("nl2sql-admin-sql");
  await adminSqlInput(adminSql).fill("DROP INDEX SAMPLE_IDX;\nCREATE VECTOR INDEX SAMPLE_IDX ON SAMPLE_TABLE (EMBEDDING);");
  await adminSql.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await adminSql.getByRole("button", { name: "SQL 実行" }).click();
  await expect(adminSql.getByText("コミット済み", { exact: true })).toBeVisible();
  return adminSql;
}

test("管理 SQL の差分同期対象を特定できない案内は結果の直下の 1 つだけで、再取得の進行はボタンの直下に出て完了で消える（#2031）", { tag: "@responsive" }, async ({ page }) => {
  await mockNl2SqlApi(page);
  await routeAdminSqlTargetUnresolved(page);
  let polls = 0;
  const gate = createRequestGate();
  await page.route("**/api/schema/refresh-jobs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await fulfillJson(route, fullRefreshJob("pending"));
  });
  await page.route("**/api/schema/refresh-jobs/admin-sql-unresolved-full-refresh", async (route) => {
    polls += 1;
    if (polls >= 2) await gate.promise;
    await fulfillJson(route, fullRefreshJob(polls >= 2 ? "done" : "running"));
  });

  const adminSql = await runUnresolvedAdminSql(page);
  const notice = adminSql.getByTestId("admin-sql-schema-sync-notice");
  // 同じ案内を 2 か所（ページの先頭と結果の中）に出さない。SQL は成功しているので warning（role=status）。
  await expect(notice).toBeVisible();
  await expect(page.getByText(/差分同期対象を(安全に)?特定できませんでした/u)).toHaveCount(1);
  await expect(notice.getByRole("status")).toContainText("DB 構造を再取得してください。");
  await expect(page.getByRole("alert").filter({ hasText: "差分同期対象" })).toHaveCount(0);
  // 案内は実行の結果の後（下）に出る（結果の表の描画が落ち着くまで待つ）。
  const resultRegion = adminSql.getByTestId("admin-sql-processing-region");
  await expect
    .poll(async () => {
      const [resultBox, noticeBox] = await Promise.all([resultRegion.boundingBox(), notice.boundingBox()]);
      return resultBox && noticeBox ? noticeBox.y - (resultBox.y + resultBox.height) : -999;
    })
    .toBeGreaterThanOrEqual(-1);

  const refreshButton = notice.getByRole("button", { name: "DB 構造を再取得" });
  await refreshButton.click();
  const processing = notice.getByTestId("admin-sql-schema-refresh-processing");
  await expect(processing).toContainText("DB 構造を再取得しています");
  await expect(refreshButton).toBeDisabled();
  // 進行は押したボタンの直下（同じ案内の中）に出る。ページの別の場所には重ねない。
  const buttonBox = await refreshButton.boundingBox();
  const processingBox = await processing.boundingBox();
  expect(buttonBox).not.toBeNull();
  expect(processingBox).not.toBeNull();
  expect(processingBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height - 1);
  expect(processingBox!.y - (buttonBox!.y + buttonBox!.height)).toBeLessThan(40);
  expect(Math.abs(processingBox!.x - buttonBox!.x)).toBeLessThan(8);
  await expect(page.getByTestId("admin-sql-schema-refresh-processing")).toHaveCount(1);
  await expectSingleSpinner(page, processing);
  // 進行が起点の直下に出るので、開始の Toast は出さない。
  await expect(page.getByText("DB 構造の再取得を受け付けました。")).toHaveCount(0);
  await expectNoHorizontalScroll(page);

  gate.release();
  await expect(notice).toHaveCount(0);
  await expect(page.getByText(/差分同期対象を(安全に)?特定できませんでした/u)).toHaveCount(0);
  await expect(page.getByText("DB 構造を再取得しました。")).toBeVisible();
  await expect(page.getByTestId("admin-sql-schema-refresh-status")).toHaveCount(0);
});

test("管理 SQL の案内から始めた再取得の失敗は、同じ場所に danger の案内と「詳細」を出す（#2031）", async ({ page }) => {
  await mockNl2SqlApi(page);
  await routeAdminSqlTargetUnresolved(page);
  await page.route("**/api/schema/refresh-jobs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await fulfillJson(route, fullRefreshJob("pending"));
  });
  await page.route("**/api/schema/refresh-jobs/admin-sql-unresolved-full-refresh", async (route) => {
    await fulfillJson(route, fullRefreshJob("error", "schema_refresh_failed"));
  });

  const adminSql = await runUnresolvedAdminSql(page);
  const notice = adminSql.getByTestId("admin-sql-schema-sync-notice");
  await notice.getByRole("button", { name: "DB 構造を再取得" }).click();
  const failure = notice.getByRole("alert");
  await expect(failure).toContainText("DB 構造の再取得に失敗しました。再試行してください。");
  await expect(failure).toContainText("schema_refresh_failed");
  await expect(notice.getByRole("button", { name: "DB 構造を再取得" })).toBeEnabled();
  await expect(page.getByRole("alert").filter({ hasText: "DB 構造の再取得に失敗しました" })).toHaveCount(1);
});

test("SQL ファイル入力は入力欄と同じ高さのまま選択とドラッグ＆ドロップで読み込める", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockNl2SqlApi(page);
  await page.goto("/query");
  await openSidebarLink(page, "管理 SQL を実行");

  const adminSql = page.getByTestId("nl2sql-admin-sql");
  const sqlInput = adminSqlInput(adminSql);
  const fileInput = adminSql.getByLabel("SQL ファイル読込 (.sql/.txt)");
  const dropzone = adminSql.getByTestId("sql-file-input-dropzone");

  await expect(dropzone).toHaveClass(/\bborder-dashed\b/);
  await expect(dropzone).toHaveAttribute("data-drag-active", "false");
  await expect(dropzone.getByText(".SQL / .TXT", { exact: true })).toBeVisible();
  await expect
    .poll(() =>
      dropzone.evaluate((element) => {
        const duration = getComputedStyle(element).transitionDuration;
        return duration.endsWith("ms")
          ? Number.parseFloat(duration)
          : Number.parseFloat(duration) * 1_000;
      })
    )
    .toBeLessThan(1);
  const desktopBox = await dropzone.boundingBox();
  expect(desktopBox).not.toBeNull();
  // ファイルの選択は入力欄と同じ md（タッチ端末は 44px。#613）。
  expect(desktopBox!.height).toBe(await expectedControlHeight(page));

  await fileInput.focus();
  await expect(fileInput).toBeFocused();
  expect(await dropzone.evaluate((element) => element.matches(":focus-within"))).toBe(true);

  await fileInput.setInputFiles({
    name: "selected-query.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("SELECT CUSTOMER_NAME FROM INVOICES"),
  });
  await expect(sqlInput).toHaveValue("SELECT CUSTOMER_NAME FROM INVOICES");
  await expect(dropzone.getByText("selected-query.txt", { exact: true })).toBeVisible();

  const activeTransfer = await createFileDataTransfer(page, [
    {
      name: "drag-active.sql",
      type: "text/plain",
      content: "SELECT TOTAL_AMOUNT FROM INVOICES",
    },
  ]);
  await dropzone.dispatchEvent("dragenter", { dataTransfer: activeTransfer });
  await dropzone.dispatchEvent("dragover", { dataTransfer: activeTransfer });
  await expect(dropzone).toHaveAttribute("data-drag-active", "true");
  await expect(dropzone.getByText("ここにドロップして読み込む", { exact: true })).toBeVisible();
  await dropzone.dispatchEvent("dragleave", { dataTransfer: activeTransfer });
  await expect(dropzone).toHaveAttribute("data-drag-active", "false");
  await activeTransfer.dispose();

  const validTransfer = await createFileDataTransfer(page, [
    {
      name: "dragged-query.SQL",
      type: "text/plain",
      content: "SELECT TOTAL_AMOUNT FROM INVOICES",
    },
  ]);
  await dropzone.dispatchEvent("drop", { dataTransfer: validTransfer });
  await validTransfer.dispose();
  await expect(sqlInput).toHaveValue("SELECT TOTAL_AMOUNT FROM INVOICES");
  await expect(dropzone.getByText("dragged-query.SQL", { exact: true })).toBeVisible();
  await expect(adminSql.getByRole("alert")).toHaveCount(0);

  const invalidTransfer = await createFileDataTransfer(page, [
    {
      name: "not-sql.csv",
      type: "text/csv",
      content: "CUSTOMER_NAME,TOTAL_AMOUNT",
    },
  ]);
  await dropzone.dispatchEvent("drop", { dataTransfer: invalidTransfer });
  await invalidTransfer.dispose();
  await expect(adminSql.getByRole("alert")).toContainText(
    "このファイルは読み込めません。.sql または .txt ファイルを選択してください。"
  );
  await expect(fileInput).toHaveAttribute("aria-invalid", "true");
  await expect(fileInput).toHaveAttribute("aria-describedby", /-error/);
  await expect(sqlInput).toHaveValue("SELECT TOTAL_AMOUNT FROM INVOICES");

  const multipleTransfer = await createFileDataTransfer(page, [
    { name: "one.sql", type: "text/plain", content: "SELECT 1 FROM DUAL" },
    { name: "two.txt", type: "text/plain", content: "SELECT 2 FROM DUAL" },
  ]);
  await dropzone.dispatchEvent("drop", { dataTransfer: multipleTransfer });
  await multipleTransfer.dispose();
  await expect(adminSql.getByRole("alert")).toContainText(
    "一度に読み込めるファイルは 1 件です。.sql または .txt ファイルを 1 件だけドロップしてください。"
  );
  await expect(sqlInput).toHaveValue("SELECT TOTAL_AMOUNT FROM INVOICES");

  await fileInput.setInputFiles({
    name: "enabled-query.sql",
    mimeType: "text/plain",
    buffer: Buffer.from("SELECT 1 FROM DUAL"),
  });
  await expect(adminSql.getByRole("alert")).toHaveCount(0);
  await expect(fileInput).toHaveAttribute("aria-invalid", "false");
  await expect(sqlInput).toHaveValue("SELECT 1 FROM DUAL");

  await page.unroute("**/api/nl2sql/db-admin/execute");
  const executionGate = createRequestGate();
  await page.route("**/api/nl2sql/db-admin/execute", async (route) => {
    await executionGate.promise;
    return fulfillJson(route, {
      executed: true,
      runtime: "oracle",
      select_result: {
        columns: ["RESULT"],
        rows: [{ RESULT: 1 }],
        total: 1,
      },
      statements: [
        {
          index: 1,
          statement_type: "SELECT",
          status: "executed",
          sql: "SELECT 1 FROM DUAL",
          row_count: 1,
          message: "1 rows",
          elapsed_ms: 0,
          error_message: "",
        },
      ],
      committed: false,
      rolled_back: false,
      warnings: [],
      timing,
    });
  });
  const executionRequest = page.waitForRequest("**/api/nl2sql/db-admin/execute");
  await adminSql.getByRole("button", { name: "SQL 実行" }).click();
  await executionRequest;
  try {
    await expect(fileInput).toBeDisabled();
    const disabledTransfer = await createFileDataTransfer(page, [
      { name: "ignored.sql", type: "text/plain", content: "SELECT 9 FROM DUAL" },
    ]);
    await dropzone.dispatchEvent("drop", { dataTransfer: disabledTransfer });
    await disabledTransfer.dispose();
    await expect(sqlInput).toHaveValue("SELECT 1 FROM DUAL");
  } finally {
    executionGate.release();
  }
  await expect(fileInput).toBeEnabled();

  const selectedBox = await dropzone.boundingBox();
  expect(selectedBox).not.toBeNull();
  expect(selectedBox!.height).toBe(await expectedControlHeight(page));
  await page.setViewportSize({ width: 375, height: 812 });
  const mobileBox = await dropzone.boundingBox();
  expect(mobileBox).not.toBeNull();
  expect(mobileBox!.height).toBe(await expectedControlHeight(page));
  await expect(dropzone.getByText(".SQL / .TXT", { exact: true })).toBeHidden();
  await expectNoHorizontalScroll(page);

  const lightBackground = await dropzone.evaluate(
    (element) => getComputedStyle(element).backgroundColor
  );
  await page.locator("html").evaluate((element) => element.classList.add("dark"));
  await expect
    .poll(() => dropzone.evaluate((element) => getComputedStyle(element).backgroundColor))
    .not.toBe(lightBackground);
});

test("AI 活用の 4 画面はナビ切替で入力を保持し、リセットで消える", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  await page.goto("/query");

  // SQL 生成に質問を入力する。
  const question = nl2sqlQuestionInput(page);
  await question.fill("保持テスト: 未入金の請求金額を確認したい");

  // SPA ナビで SELECT SQL 実行へ移動し、そちらにも入力する。
  await (await openSidebarNav(page)).getByRole("link", { name: "SELECT SQL を実行" }).click();
  await expect(page).toHaveURL(/\/direct-sql$/);
  const directSql = directSqlInput(page);
  await directSql.fill("SELECT CUSTOMER_NAME FROM INVOICES");

  // SQL 生成へ戻ると入力が残っている(unmount で破棄されない = keep-alive)。
  await (await openSidebarNav(page)).getByRole("link", { name: /SQL 生成/ }).first().click();
  await expect(page).toHaveURL(/\/query$/);
  await expect(nl2sqlQuestionInput(page)).toHaveValue("保持テスト: 未入金の請求金額を確認したい");

  // SELECT SQL 実行へ再び移動しても入力が残っている。
  await (await openSidebarNav(page)).getByRole("link", { name: "SELECT SQL を実行" }).click();
  await expect(page).toHaveURL(/\/direct-sql$/);
  await expect(directSqlInput(page)).toHaveValue("SELECT CUSTOMER_NAME FROM INVOICES");

  // クリアは明示ボタンでのみ行われる(ナビ切替では消えない)。
  await page.getByTestId("nl2sql-direct-sql").getByRole("button", { name: "SQL 入力・結果をリセット" }).click();
  await expect(directSqlInput(page)).toHaveValue("");

  await (await openSidebarNav(page)).getByRole("link", { name: /SQL 生成/ }).first().click();
  const newQueryButton = page.getByRole("button", { name: "新しい質問を開始", exact: true });
  await expectLargeActionButton(newQueryButton);
  await newQueryButton.focus();
  await page.keyboard.press("Enter");
  const discardDialog = page.getByRole("alertdialog", { name: "未保存の入力を破棄しますか？" });
  await expect(discardDialog).toContainText("入力した質問と表示中の結果をクリアし、今回の生成条件・実行オプションを初期値に戻します。");
  await page.keyboard.press("Escape");
  await expect(discardDialog).toBeHidden();
  await expect(nl2sqlQuestionInput(page)).toHaveValue("保持テスト: 未入金の請求金額を確認したい");
  await expect(newQueryButton).toBeFocused();
  await page.keyboard.press("Enter");
  await discardDialog.getByRole("button", { name: "新しい質問を開始" }).click();
  await expect(nl2sqlQuestionInput(page)).toHaveValue("");
  expect(api.jobPayload).toBeNull();
  expect(api.executePayload).toBeNull();
  await expectNoHorizontalScroll(page);
  await newQueryButton.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("contextual-new-query.png") });
});

test("history detail shows the SQL without a rerun action (#2343)", async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.goto("/history");
  // 一覧の題名のリンクで ?id= の全幅の詳細を開く（#1955）。
  await page.getByRole("link", { name: "履歴から再実行したい請求金額（詳細を開く）" }).click();
  const sqlBlock = page.getByTestId("history-detail-sql-code");
  await expect(sqlBlock).toContainText("CUSTOMER_PAYMENT_RECONCILIATION_STATUS");
  await expect(sqlBlock).toHaveCSS("overflow-x", "auto");
  await expectNoHorizontalScroll(page);
  // 再実行は実行の詳細・一覧の行に置かない（3 製品の実行の詳細を RAG と同じ「会話を開く」にそろえる。#2343）。
  await expect(page.getByRole("button", { name: "この質問で再実行" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "履歴から再実行したい請求金額の操作" })).toHaveCount(0);
});

test("本人の SQL 生成の実行は「SQL 生成で開く」で質問と業務プロファイルを入れた画面を開き、実行はしない (#3309)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  const question = "合成の質問 A を確認";
  const own = { ...historyItem, question, engine: "enterprise_ai_direct", kind: "query", executed_by_me: true };
  // system_admin が見る他の人の実行（#3244）には出さない。
  const others = { ...own, id: "hist-other", executed_by_me: false };
  await page.route("**/api/nl2sql/history/*", (route) =>
    fulfillJson(route, new URL(route.request().url()).pathname.endsWith("/hist-other") ? others : own)
  );

  await page.goto("/history?id=hist-other");
  await expect(page.getByTestId("history-detail-summary")).toBeVisible();
  await expect(page.getByTestId("history-detail-open-query")).toHaveCount(0);

  await page.goto("/history?id=hist-001");
  const open = page.getByTestId("history-detail-open-query");
  await expect(open).toHaveText("SQL 生成で開く");
  await expect(open).toHaveAccessibleName("この実行の質問と条件を SQL 生成の画面に入れて開く");
  await expect(page.getByTestId("history-detail-open-chat")).toHaveCount(0);
  expect((await open.boundingBox())?.height).toBe(await expectedControlHeight(page));
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("history-open-query.png") });
  await open.click();

  // 初期値は一度だけ入れて URL から外す。SQL の生成と実行は利用者が押す。
  await expect(page).toHaveURL(/\/query$/);
  await expect(nl2sqlQuestionInput(page)).toHaveValue(question);
  await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), "default");
  await page.waitForTimeout(300);
  expect(api.jobPayload).toBeNull();
  expect(api.executePayload).toBeNull();
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("query-opened-from-history.png") });
});

for (const listed of ["complete", "paged"] as const) {
  test(`業務プロファイルが削除済みの実行の「SQL 生成で開く」は質問だけを入れ、選び直しを案内する（一覧 ${listed}）(#3355)`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    const api = await mockNl2SqlApi(page);
    const question = "合成の質問 B を確認";
    const own = { ...historyItem, question, kind: "query", executed_by_me: true, profile_id: "deleted-profile" };
    await page.route("**/api/nl2sql/history/*", (route) => fulfillJson(route, own));
    if (listed === "paged") {
      // 一覧が後のページへ続くときは一覧だけでは決められないので、利用の情報の 404 で使えないと分かる。
      await page.unroute("**/api/nl2sql/profiles/search?*");
      await page.route("**/api/nl2sql/profiles/search?*", (route) =>
        fulfillJson(route, {
          items: [
            {
              id: "default",
              name: "既定",
              category: "",
              description: "",
              archived: false,
              allowed_table_count: 1,
              allowed_view_count: 0,
              glossary_count: 0,
              few_shot_count: 0,
              version: 1,
              etag: "etag-default",
              updated_at: "2026-06-21T10:00:00.000Z",
            },
          ],
          next_cursor: "next",
          total: 60,
          change_token: 1,
        })
      );
    }
    const usageContextRequests: string[] = [];
    await page.route("**/api/nl2sql/profiles/deleted-profile/usage-context", (route) => {
      usageContextRequests.push(route.request().url());
      return route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "指定された業務プロファイルが見つからないか、利用できません。" }),
      });
    });
    const consoleErrors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().includes("404")) consoleErrors.push(message.text());
    });

    await page.goto("/history?id=hist-001");
    await page.getByTestId("history-detail-open-query").click();

    await expect(page).toHaveURL(/\/query$/);
    await expect(nl2sqlQuestionInput(page)).toHaveValue(question);
    const notice = page.getByTestId("nl2sql-prefill-profile-unavailable");
    await expect(notice).toHaveText("元の業務プロファイルは使えません。選び直してください。");
    // 先頭の業務プロファイルへ黙って切り替えず、空のまま選び直してもらう。404 のエラーの案内は出さない。
    await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), "");
    await expect(page.locator("#nl2sql-profile-select")).toContainText("業務プロファイルを選択");
    await expect(page.getByText(/見つかりません|profile/)).toHaveCount(0);
    expect(usageContextRequests).toHaveLength(listed === "paged" ? 1 : 0);
    expect(consoleErrors).toEqual([]);
    expect(api.jobPayload).toBeNull();
    await expectNoHorizontalScroll(page);
    await page.screenshot({ path: testInfo.outputPath(`query-profile-unavailable-${listed}.png`), fullPage: true });

    // 業務プロファイルが未選択の間、スキーマ参照は読み込み中にせず、選ぶと表と列を出すと案内する（#3413）。
    const schema = page.getByTestId("nl2sql-schema-reference");
    const schemaIdle = schema.getByTestId("nl2sql-schema-profile-unselected");
    await expect(schemaIdle).toContainText("業務プロファイルを選んでください");
    await expect(schemaIdle).toContainText("業務プロファイルを選ぶと、使える表と列を表示します。");
    await expect(schema.getByTestId("schema-reference-loading")).toHaveCount(0);
    await expect(schema.getByRole("searchbox", { name: "表・項目検索" })).toBeDisabled();
    await expect(schema.getByTestId("nl2sql-schema-table-item")).toHaveCount(0);
    await expect(schema.getByText("全 0 件")).toHaveCount(0);
    await expect(page.getByTestId("nl2sql-schema-toolbar")).toBeVisible();
    await schema.screenshot({ path: testInfo.outputPath(`query-schema-profile-unselected-${listed}.png`) });
    // 「最新情報を取得」は押せ、業務プロファイルで絞らないスキーマの一覧を取りに行かない。
    const unscopedSchemaRequests: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/api/schema/objects" && !url.searchParams.has("profile_id")) {
        unscopedSchemaRequests.push(request.url());
      }
    });
    const refresh = page.getByRole("button", { name: "最新情報を取得", exact: true });
    await expect(refresh).toBeEnabled();
    await refresh.click();
    await expect(page.getByText("最新の状態に更新しました。")).toBeVisible();
    await expect(schemaIdle).toBeVisible();
    expect(unscopedSchemaRequests).toEqual([]);

    // 選び直すと案内を消し、質問は選んだ業務プロファイルへ引き継ぐ。
    await chooseSelectFieldOption(page.locator("#nl2sql-profile-select"), "default");
    await expect(notice).toHaveCount(0);
    await expectSelectFieldValue(page.locator("#nl2sql-profile-select"), "default");
    await expect(nl2sqlQuestionInput(page)).toHaveValue(question);
    // 選んだ業務プロファイルの表を読み込み、未選択の案内を消す。
    await expect(schemaIdle).toHaveCount(0);
    await expect(schema.getByTestId("nl2sql-schema-table-item").first()).toBeVisible();
    await expect(schema.getByRole("searchbox", { name: "表・項目検索" })).toBeEnabled();
  });
}

test("root route opens SQL generation and sidebar exposes feature surfaces", async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.goto("/");

  await expect(page).toHaveURL(/\/query$/);
  await expect(page.getByRole("heading", { name: "質問から SQL を生成して実行", exact: true })).toBeVisible();
  // 375px ではナビがドロワー（#367）。開いてから読む（背面は inert のため、以降のリンクはサイドナビのもの）。
  await openSidebarNav(page);
  await expect(page.getByText("データ準備", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("AI 活用", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("改善・運用", { exact: true }).first()).toBeVisible();
  // データ準備 / 改善・運用 は初期折りたたみ(initiallyCollapsed)のため、リンク断言前に展開する。
  // ドロワーの中も展開した幅で描くため、mobile 幅でも展開ボタンを押す（展開ボタンがある時だけ押す）。
  const prepareExpand = page.getByRole("button", { name: "データ準備 を展開" });
  if (await prepareExpand.isVisible()) {
    await prepareExpand.click();
    await page.getByRole("button", { name: "改善・運用 を展開" }).click();
  }
  await expect(page.getByRole("link", { name: /ダッシュボード/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /テーブルの管理/ }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /ビューの管理/ }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /データの管理/ }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /サンプルデータ管理/ }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /検証用サンプルデータ/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /業務プロファイル/ }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /用語・同義語/ }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /SQL 生成/ }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /SQL 確認・修復/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /SQL から質問を生成/ }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: "SELECT SQL を実行" })).toBeVisible();
  await expect(page.getByRole("link", { name: "管理 SQL を実行" })).toBeVisible();
  await expect(page.getByRole("link", { name: /フィードバック管理/ }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /フィードバック学習/ })).toHaveCount(0);
  const navTexts = await page.locator("nav a").allTextContents();
  expect(navTexts.findIndex((label) => label.includes("フィードバック管理"))).toBeLessThan(
    navTexts.findIndex((label) => label.includes("質問分類モデル管理"))
  );
  await expect(page.getByRole("link", { name: /質問分類モデル管理/ }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /質問学習/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /エンジン運用/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /NL2SQL 接続診断/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /モデル学習/ })).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("dark theme keeps the SQL workbench text, controls and active states legible", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  // theme.ts は ui-store の変更ごとに html.dark を toggle し直すため、classList を直接触ると
  // store 更新(サイドバー状態等)で light へ戻る。永続化されたテーマ選好として dark を与える。
  await page.addInitScript(() => {
    const key = "production-ready-nl2sql.ui";
    const stored = JSON.parse(window.localStorage.getItem(key) ?? "null") ?? { state: {}, version: 0 };
    stored.state = { ...stored.state, theme: "dark" };
    window.localStorage.setItem(key, JSON.stringify(stored));
  });
  await page.goto("/query");
  await expect(page.locator("html")).toHaveClass(/\bdark\b/);

  const heading = page.getByRole("heading", { name: "質問から SQL を生成して実行", exact: true });
  const question = nl2sqlQuestionInput(page);
  const executeButton = page.getByRole("button", { name: "SQL を生成して実行" });
  const activeQueryLink = page
    .getByRole("complementary", { name: "サイドナビゲーション" })
    .getByRole("link", { name: "SQL 生成" });

  // 期待値は packages/ui tokens/colors.css + palette.css の dark 側:
  // --color-fg = neutral-100 #f2f4f7 / 入力の輪郭 --color-border-control = neutral-650 #5d6878 (3:1, WCAG 1.4.11)
  await expect(heading).toHaveCSS("color", "rgb(242, 244, 247)");
  await expect(question).toHaveCSS("border-color", "rgb(93, 104, 120)");
  // 375px ではナビがドロワー（#367）。開いて現在地の色を確かめ、閉じてから本文を操作する。
  await openSidebarNav(page);
  await expect(activeQueryLink).toHaveCSS("background-color", "rgb(40, 106, 189)");
  await expect(activeQueryLink).toHaveCSS("color", "rgb(255, 255, 255)");
  await closeSidebarNav(page);
  await expect(executeButton).toBeDisabled();
  await expect(executeButton).toHaveCSS("background-color", "rgb(37, 43, 52)");
  await expect(executeButton).toHaveCSS("color", "rgb(116, 127, 142)");

  await question.focus();
  await expect(question).toHaveCSS("border-color", "rgb(124, 187, 255)");
  await expectNoHorizontalScroll(page);
  await page.screenshot({
    path: testInfo.outputPath(`nl2sql-dark-workbench-${testInfo.project.name}.png`),
    fullPage: true,
  });
});

test("SQL 確認・修復 surface is removed and legacy URL falls back", async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.goto("/");
  await expect(page.getByRole("link", { name: /SQL 確認・修復/ })).toHaveCount(0);

  await page.goto("/sql-analysis");
  await expect(page).toHaveURL(/\/query$/);
  await expect(page.getByRole("heading", { name: "質問から SQL を生成して実行", exact: true })).toBeVisible();
  await expect(page.locator("#sql-analysis-panel-analysis")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "SQL を分析" })).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("sql to question page reverse-generates a business question with one primary action", async ({ page }) => {
  const api = await mockNl2SqlApi(page);

  await page.goto("/sql-to-question");
  await expect(page.getByRole("heading", { name: "SQL から質問を生成" })).toBeVisible();
  await expect(page.locator("#sql-to-question-panel-input")).toBeVisible();
  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル" }), "default");
  await expect(page.getByText("請求情報")).toBeVisible();
  await expect(page.getByTestId("sql-to-question-table-count")).toHaveText("参照表 1");

  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await expect(page.getByLabel("用語・同義語を使う")).toHaveCount(0);
  const generateButton = page.getByRole("button", { name: "SQL 分析・質問生成" });
  await expect(generateButton).toBeEnabled();
  await expect(page.getByRole("button", { name: "SQL 構造を分析" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Deep 逆生成" })).toHaveCount(0);
  await expect(sqlToQuestionInput(page)).toHaveValue("SELECT TOTAL_AMOUNT FROM INVOICES");
  await generateButton.click();
  await expect(page.locator("#sql-to-question-panel-structure")).toBeVisible();
  await expect(page.getByRole("tab", { name: "SQL分析・質問候補" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: "SQL分析・質問候補" })).toBeFocused();

  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  const structurePanel = page.locator("#sql-to-question-panel-structure");
  await expect(structurePanel.getByText("SQL 論理構造").first()).toBeVisible();
  // SQL 論理構造は「見出し + 業務者向け説明 + 技術詳細」で併記する。
  const structureList = structurePanel.getByTestId("sql-to-question-structure-list");
  await expect(structureList).toBeVisible();
  await expect(structureList).toContainText("SQL 種別");
  await expect(structureList).toContainText("データを取り出すだけの参照 SQL です");
  await expect(structureList.getByText("SELECT").first()).toBeVisible();
  const resultPanel = page.getByRole("region", { name: "質問候補", exact: true });
  await expect(resultPanel).toBeVisible();
  await expect(resultPanel.getByText("自然言語の質問", { exact: true })).toBeVisible();
  await expect(resultPanel.getByText("SQL の処理手順", { exact: true })).toHaveCount(0);
  await expect(resultPanel.getByText("説明", { exact: true })).toHaveCount(0);
  await expect.poll(() => api.reverseDeepPayload).toEqual({
    sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
    profile_id: "default",
    use_glossary: false,
  });
  expect(api.reversePayload).toBeNull();
  expect(api.analyzePayload).toBeNull();
  await expectNoHorizontalScroll(page);
});

test("SQL から質問は保存した業務プロファイルが一覧に無ければ先頭のプロファイルへ切り替える (#913)", async ({ page }) => {
  const api = await mockNl2SqlApi(page);
  const usageContextIds: string[] = [];
  // 削除・アーカイブ・利用権限の解除で一覧から消えたプロファイルは、usage-context が 404 になる。
  await page.route("**/api/nl2sql/profiles/*/usage-context", async (route) => {
    const profileId = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-2) ?? "");
    usageContextIds.push(profileId);
    if (profileId === "deleted-profile") {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "業務プロファイルが見つかりません。" }),
      });
      return;
    }
    await route.fallback();
  });
  // 前回の訪問で選んだ業務プロファイル（作業状態）が、今は一覧に無い。
  await page.addInitScript(
    ({ ownerKey, profileKey, sqlKey, owner }) => {
      if (sessionStorage.getItem("e2e-seeded")) return;
      sessionStorage.setItem("e2e-seeded", "1");
      sessionStorage.setItem(ownerKey, owner);
      sessionStorage.setItem(profileKey, JSON.stringify({ at: Date.now(), value: "deleted-profile" }));
      sessionStorage.setItem(sqlKey, JSON.stringify({ at: Date.now(), value: "SELECT 1 FROM OLD_TABLE" }));
    },
    {
      ownerKey: `${WORKSPACE_DRAFT_PREFIX}owner`,
      profileKey: draftKey(systemAdminMe.user_uuid, "legacy", "/sql-to-question", "selectedProfileId"),
      sqlKey: draftKey(systemAdminMe.user_uuid, "legacy", "/sql-to-question", "sql:deleted-profile"),
      owner: systemAdminMe.user_uuid,
    }
  );

  await page.goto("/sql-to-question");
  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル" }), "default");
  await expect(page.getByTestId("sql-to-question-table-count")).toHaveText("参照表 1");
  await expect(page.getByText("業務プロファイルが見つかりません。")).toHaveCount(0);
  expect(usageContextIds).not.toContain("deleted-profile");
  // 消えたプロファイルの下書きは使わず、切り替え先の下書き（空）を出す。
  await expect(sqlToQuestionInput(page)).toHaveValue("");

  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await page.getByRole("button", { name: "SQL 分析・質問生成" }).click();
  await expect.poll(() => api.reverseDeepPayload).toEqual({
    sql: "SELECT TOTAL_AMOUNT FROM INVOICES",
    profile_id: "default",
    use_glossary: false,
  });
  await expectNoHorizontalScroll(page);
});

test("sql to question page uses shared tabs, panel styling and a step indicator", async ({ page }) => {
  await mockNl2SqlApi(page);

  // テーブル管理の作業（作成）のパネルと同じ枠（一覧は ListDetailLayout の列が枠を持つ。#2281）。
  await page.goto("/table-management");
  await clickPageHeaderAction(page, "table-management-actions", "テーブル作成");
  const tablePanelStyle = await page.locator("#table-management-panel-create").evaluate((node) => {
    const style = window.getComputedStyle(node);
    return {
      backgroundColor: style.backgroundColor,
      borderTopWidth: style.borderTopWidth,
      borderRadius: style.borderRadius,
      paddingTop: style.paddingTop,
      boxShadow: style.boxShadow,
    };
  });

  await page.goto("/sql-to-question");
  const inputPanel = page.locator("#sql-to-question-panel-input");
  await expect(inputPanel).toBeVisible();
  const inputPanelStyle = await inputPanel.evaluate((node) => {
    const style = window.getComputedStyle(node);
    return {
      backgroundColor: style.backgroundColor,
      borderTopWidth: style.borderTopWidth,
      borderRadius: style.borderRadius,
      paddingTop: style.paddingTop,
      boxShadow: style.boxShadow,
    };
  });
  expect(inputPanelStyle).toEqual(tablePanelStyle);

  const tabs = page.getByRole("tab");
  await expect(tabs).toHaveText(["SQL入力・生成", "SQL分析・質問候補"]);
  await expect(page.getByRole("tab", { name: "SQL入力・生成" })).toHaveAttribute("aria-selected", "true");

  const steps = page.getByTestId("sql-to-question-steps");
  await expect(steps).toBeVisible();
  await expect(steps.getByText("SQL入力・生成")).toBeVisible();
  await expect(steps.getByText("SQL分析・質問候補")).toBeVisible();
  await expect(steps.locator(":scope > li")).toHaveCount(2);
  await expect(page.getByRole("tab", { name: "質問候補", exact: true })).toHaveCount(0);
  await expect(page.locator("#sql-to-question-panel-structure")).toBeHidden();
  await expect(page.getByRole("region", { name: "質問候補", exact: true })).toBeHidden();

  await page.getByRole("tab", { name: "SQL入力・生成" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "SQL分析・質問候補" })).toBeFocused();
  await expect(page.locator("#sql-to-question-panel-structure")).toBeVisible();
  await expect(page.locator("#sql-to-question-panel-input")).toBeHidden();
  await expect(page.getByText("SQL 構造は未分析です")).toBeVisible();

  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(page.getByRole("region", { name: "質問候補", exact: true })).toBeVisible();
  await expect(page.locator("#sql-to-question-panel-input")).toBeHidden();
  await expect(page.locator("#sql-to-question-panel-structure")).toBeVisible();
  await expect(page.getByText("質問候補は未生成です")).toBeVisible();
  await expectMainScrollDoesNotExposeTrailingBlank(page.locator("#sql-to-question-panel-structure"));
  await expect(page.locator('[role="tabpanel"][id^="sql-to-question-panel-"]')).toHaveCount(2);
  await expect(page.locator("#sql-to-question-panel-result")).toHaveCount(0);
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "SQL入力・生成" })).toHaveAttribute("aria-selected", "true");
  await expectNoHorizontalScroll(page);
});

test("sql to question keeps the SQL typed while business profiles are loading (#455)", async ({ page }) => {
  await mockNl2SqlApi(page);
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/nl2sql/profiles/search?*", async (route) => {
    await gate;
    await route.fallback();
  });
  await page.goto("/sql-to-question");
  const input = sqlToQuestionInput(page);
  await expect(input).toBeVisible();
  // 業務プロファイルが決まるまでは入力させない（決まった時点で下書きのキーが変わり、入力が消えるため）。
  await expect(input).toBeDisabled();
  release();
  await expect(input).toBeEnabled();
  await input.fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await expect(page.getByRole("button", { name: "SQL 分析・質問生成", exact: true })).toBeEnabled();
  await expect(input).toHaveValue("SELECT TOTAL_AMOUNT FROM INVOICES");
});

test("sql to question page shows a reserved loading state and retries reference-data errors", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/schema/objects?*");
  let failObjectRequests = true;
  let releaseFirstObjectRequest: (() => void) | undefined;
  const firstObjectRequestGate = new Promise<void>((resolve) => {
    releaseFirstObjectRequest = resolve;
  });
  await page.route("**/api/schema/objects?*", async (route) => {
    const shouldFail = failObjectRequests;
    if (shouldFail) {
      await firstObjectRequestGate;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Schema objects unavailable" }),
      });
      return;
    }
    await fulfillJson(route, {
      items: schemaCatalog.tables.map((table) => ({
        owner: table.owner,
        object_name: table.table_name,
        object_type: table.table_type,
        logical_name: table.logical_name,
        comment: table.comment,
        row_count: table.row_count,
        column_count: table.columns.length,
        last_ddl_at: "",
      })),
      next_cursor: null,
      total: schemaCatalog.tables.length,
      catalog_version: 1,
    });
  });

  await page.goto("/sql-to-question");
  await expect(page.getByTestId("sql-to-question-schema-skeleton")).toBeVisible();
  await expect(page.getByRole("button", { name: "SQL 分析・質問生成" })).toBeDisabled();
  // 参照データの読込のスピナーは「最新情報を取得」の 1 つだけ（スキーマの Skeleton は経過時間だけ。#416）。
  await expectSingleSpinner(page, page.getByRole("button", { name: "最新情報を取得", exact: true }));
  releaseFirstObjectRequest?.();

  const errorBanner = page.getByRole("alert");
  await expect(errorBanner).toContainText("接続状態と入力内容を確認して再試行してください。");
  failObjectRequests = false;
  await errorBanner.getByRole("button", { name: "プロファイル・スキーマを再読込" }).click();
  await expect(errorBanner).toHaveCount(0);
  await expect(page.getByText("請求情報")).toBeVisible();
  await expect(page.getByTestId("sql-to-question-table-count")).toHaveText("参照表 1");
  await expectNoHorizontalScroll(page);
});

test("sql to question page shows a guided empty schema state", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/schema/objects?*");
  await page.route("**/api/schema/objects?*", (route) =>
    fulfillJson(route, {
      items: [],
      next_cursor: null,
      total: 0,
      catalog_version: 1,
    })
  );

  await page.goto("/sql-to-question");

  await expect(page.getByText("参照できる表がありません")).toBeVisible();
  await expect(page.getByText("選択プロファイルで参照できるスキーマ情報がありません。")).toBeVisible();
  await expect(page.getByTestId("sql-to-question-table-count")).toHaveText("参照表 0");
  await expectNoHorizontalScroll(page);
});

test("sql to question page invalidates stale results when inputs change", async ({ page }) => {
  await mockNl2SqlApi(page);
  const alternateProfile = {
    ...profiles[0],
    id: "alternate",
    name: "代替プロファイル",
  };
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [...profiles, alternateProfile].map((profile) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: profile.archived,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: Object.keys(profile.glossary).length,
        few_shot_count: profile.few_shot_examples.length,
        version: 1,
        etag: `etag-${profile.id}`,
        updated_at: "2026-06-21T10:00:00.000Z",
      })),
      next_cursor: null,
      total: 2,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/alternate", (route) =>
    fulfillJson(route, alternateProfile)
  );
  await page.goto("/sql-to-question");

  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await page.getByRole("button", { name: "SQL 分析・質問生成" }).click();
  await expect(page.locator("#sql-to-question-panel-structure")).toBeVisible();
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(page.getByText("請求金額を条件付きで一覧確認したい")).toBeVisible();

  // 入力を変えると生成済み結果は無効化され、質問セクションは空状態へ戻る。
  await page.getByRole("tab", { name: "SQL入力・生成" }).click();
  await chooseSelectFieldOption(page.getByRole("combobox", { name: "業務プロファイル" }), "alternate");
  await expect(sqlToQuestionInput(page)).toHaveValue("");
  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(page.getByText("質問候補は未生成です")).toBeVisible();

  await page.getByRole("tab", { name: "SQL入力・生成" }).click();
  await page.getByRole("button", { name: "SQL 分析・質問生成" }).click();
  await expect(page.locator("#sql-to-question-panel-structure")).toBeVisible();
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(page.getByText("請求金額を条件付きで一覧確認したい")).toBeVisible();
  await page.getByRole("tab", { name: "SQL入力・生成" }).click();
  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES WHERE TOTAL_AMOUNT > 0");
  await expect(page.getByRole("button", { name: "SQL 分析・質問生成" })).toBeEnabled();
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(page.getByText("SQL 構造は未分析です")).toBeVisible();
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(page.getByText("質問候補は未生成です")).toBeVisible();
  // 別 Profile の編集で、元 Profile の候補を消去しない。
  await page.getByRole("tab", { name: "SQL入力・生成" }).click();
  await chooseSelectFieldOption(page.getByRole("combobox", { name: "業務プロファイル" }), "default");
  await expect(sqlToQuestionInput(page)).toHaveValue("SELECT TOTAL_AMOUNT FROM INVOICES");
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(page.getByRole("region", { name: "質問候補", exact: true })).toContainText("請求金額を条件付きで一覧確認したい");
  await page.reload();
  await expect(page.getByRole("region", { name: "質問候補", exact: true })).toContainText("請求金額を条件付きで一覧確認したい");

});

test("sql to question page keeps controls usable without page overflow at 375px", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await mockNl2SqlApi(page);
  await page.goto("/sql-to-question");

  await expect(page.getByTestId("sql-to-question-steps")).toBeVisible();
  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await expect(page.getByLabel("用語・同義語を使う")).toHaveCount(0);
  const generateButton = page.getByRole("button", { name: "SQL 分析・質問生成" });
  await expect(generateButton).toBeVisible();
  const box = await generateButton.boundingBox();
  expect(box?.width ?? 0).toBeGreaterThan(250);
  await expect(page.getByRole("button", { name: "SQL 構造を分析" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Deep 逆生成" })).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("sql to question page remains usable at 150 percent zoom", async ({ page }) => {
  await page.setViewportSize({ width: 1365, height: 900 });
  await mockNl2SqlApi(page);
  await page.goto("/sql-to-question");
  await page.evaluate(() => {
    document.documentElement.style.zoom = "1.5";
  });

  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await expect(page.getByLabel("用語・同義語を使う")).toHaveCount(0);
  const steps = page.getByTestId("sql-to-question-steps");
  for (const label of ["SQL入力・生成", "SQL分析・質問候補"]) {
    await expect(steps.getByText(label)).toBeVisible();
  }
  const generateButton = page.getByRole("button", { name: "SQL 分析・質問生成" });
  await expect(generateButton).toBeVisible();
  await expect(generateButton).toHaveCSS("white-space", "nowrap");
  await expect(page.getByRole("button", { name: "SQL 構造を分析" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Deep 逆生成" })).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("sql to question page reports generation errors beside the action bar", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/reverse/deep");
  let releaseReverse: (() => void) | undefined;
  const reverseGate = new Promise<void>((resolve) => {
    releaseReverse = resolve;
  });
  await page.route("**/api/nl2sql/reverse/deep", async (route) => {
    await reverseGate;
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "SQL reverse unavailable" }),
    });
  });
  await page.goto("/sql-to-question");

  const inputPanel = page.locator("#sql-to-question-panel-input");
  await sqlToQuestionInput(inputPanel).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await inputPanel.getByRole("button", { name: "SQL 分析・質問生成" }).click();
  await expect(page.getByTestId("sql-to-question-analysis-loading")).toBeVisible();
  await page.getByRole("tab", { name: "SQL入力・生成" }).click();
  await expect(sqlToQuestionInput(inputPanel)).toBeDisabled();
  await expect(inputPanel.getByRole("combobox", { name: "業務プロファイル" })).toBeDisabled();
  await expect(inputPanel.getByLabel("用語・同義語を使う")).toHaveCount(0);
  releaseReverse?.();

  await expect(inputPanel.getByRole("alert")).toContainText("接続状態と入力内容を確認して再試行してください。");
  await expect(sqlToQuestionInput(inputPanel)).toBeEnabled();
});

/** フィードバック履歴の一覧の列（共有 ListDetailLayout。#2283）。 */
function feedbackHistoryList(page: Page) {
  return page.getByTestId("list-detail-feedback-management-list");
}

/** 1 ペイン（xl 未満）で詳細を開いているときだけ「一覧へ戻る」で一覧へ戻る（2 ペインでは何もしない）。 */
async function backToFeedbackHistoryList(page: Page) {
  const back = page.getByTestId("app-feedback-detail-back");
  if (await back.isVisible()) {
    await back.click();
    await expect(feedbackHistoryList(page)).toBeVisible();
  }
}

test("feedback management defaults to app feedback and keeps explicit tab deep links", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.goto("/feedback-management");
  await expect(page.getByRole("heading", { name: "フィードバック管理" })).toBeVisible();
  await expect(page.getByRole("tab")).toHaveText([
    "アプリ内フィードバック",
    "類似検索インデックス",
  ]);
  await expect(page.getByRole("tab", { name: "アプリ内フィードバック" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: "類似検索インデックス" })).toHaveAttribute("aria-selected", "false");
  await expect(feedbackHistoryList(page)).toBeVisible();

  await page.evaluate(() => {
    window.history.pushState(null, "", "/feedback-management?tab=similarityIndex");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.getByRole("tab", { name: "類似検索インデックス" })).toHaveAttribute("aria-selected", "true");

  await page.evaluate(() => {
    window.history.pushState(null, "", "/feedback-management?tab=unknown");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.getByRole("tab", { name: "アプリ内フィードバック" })).toHaveAttribute("aria-selected", "true");
});

test("feedback management initial load covers only the history list with the list skeleton (#1777)", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockNl2SqlApi(page);
  const feedbackGate = createRequestGate();
  await page.route(/\/api\/nl2sql\/feedback(?:\?.*)?$/, async (route) => {
    if (route.request().method() === "GET") await feedbackGate.promise;
    await route.fallback();
  });

  await page.setViewportSize({ width: 1280, height: 900 });
  // 選んだ履歴を URL で開く（先頭の行は自動で選ばない。#2283）。
  await page.goto("/feedback-management?tab=appFeedback&feedback=hist-001");
  const panel = page.locator("#feedback-management-panel-appFeedback");
  const historyPane = feedbackHistoryList(page);
  // タブ・一覧の見出し・絞り込みは隠さず、一覧の領域だけを TimedLoadingState と一覧の形の Skeleton にする。
  await expect(page.getByRole("tab", { name: "アプリ内フィードバック" })).toHaveAttribute("aria-selected", "true");
  await expect(historyPane).toBeVisible();
  await expect(historyPane.getByRole("searchbox", { name: "履歴検索" })).toBeVisible();
  const loadingState = historyPane.getByTestId("app-feedback-list-loading");
  await expect(loadingState).toBeVisible();
  await expect(loadingState).toHaveAttribute("aria-busy", "true");
  await expect(loadingState).toHaveAttribute("data-processing-placement", "panel");
  await expect(loadingState).toContainText("フィードバック履歴を読み込んでいます");
  await expect(loadingState.getByRole("timer")).toHaveAccessibleName(/経過時間 00:0\d/);
  await expect(historyPane.getByTestId("app-feedback-list-skeleton")).toBeVisible();
  // reduced-motion では Skeleton の明滅を止める。
  await expect(historyPane.getByTestId("app-feedback-list-skeleton").locator("[data-skeleton-row]").first()).toHaveCSS(
    "animation-name",
    "none"
  );
  // 編集欄は一覧に依存するので、空の状態を出さず形の Skeleton だけにする（経過時間は一覧の 1 か所だけ）。
  await expect(panel.getByTestId("app-feedback-editor-skeleton")).toBeVisible();
  await expect(panel.getByRole("timer")).toHaveCount(1);
  await expect(panel.getByText("フィードバックはまだありません")).toHaveCount(0);
  // 件数は取得してから出す（読み込み中に 0 件と見せない）。
  await expect(historyPane.getByText(/アプリ内フィードバック \d+/)).toHaveCount(0);
  await expect(page.getByTestId("feedback-management-refresh-processing")).toHaveCount(0);
  // スピナーはヘッダーの「最新情報を取得」の 1 つだけ（操作が 1 つのヘッダー。messaging §3.7）。
  await expectSingleSpinner(page, page.getByTestId("feedback-management-actions"));
  await expectNoHorizontalScroll(page);

  // 375px は 1 ペイン。選んだ履歴を開いているので、詳細（編集欄の Skeleton）だけを出す。
  await page.setViewportSize({ width: 375, height: 900 });
  await expect(panel.getByTestId("app-feedback-editor-skeleton")).toBeVisible();
  await expect(loadingState).toBeHidden();
  await expectNoHorizontalScroll(page);

  feedbackGate.release();
  await expect(loadingState).toHaveCount(0);
  await expect(panel.getByTestId("app-feedback-editor-skeleton")).toHaveCount(0);
  await expect(panel.getByText("生成 SQL", { exact: true })).toBeVisible();
  await backToFeedbackHistoryList(page);
  await expect(panel.getByTestId("feedback-history-row").first()).toBeVisible();
});

test("feedback management refresh keeps the previous content and shows the header button loading (#1777)", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockNl2SqlApi(page);
  const refreshGate = createRequestGate();
  let holdNextRefresh = false;
  let markRefreshStarted: () => void = () => undefined;
  const refreshStarted = new Promise<void>((resolve) => {
    markRefreshStarted = resolve;
  });

  await page.route(/\/api\/nl2sql\/feedback(?:\?.*)?$/, async (route) => {
    if (route.request().method() === "GET" && holdNextRefresh) {
      holdNextRefresh = false;
      markRefreshStarted();
      await refreshGate.promise;
    }
    await route.fallback();
  });

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/feedback-management?tab=appFeedback&feedback=hist-001");
  const panel = page.locator("#feedback-management-panel-appFeedback");
  await expect(feedbackHistoryList(page)).toBeVisible();
  await expect(panel.getByTestId("app-feedback-editor-pane")).toBeVisible();
  await expect(panel.getByText("生成 SQL", { exact: true })).toBeVisible();
  const firstQuestion = (await panel.getByTestId("feedback-history-question").first().textContent()) ?? "";
  expect(firstQuestion.trim()).not.toBe("");

  holdNextRefresh = true;
  await clickPageHeaderAction(page, "feedback-management-actions", "最新情報を取得");
  await refreshStarted;

  // 前の一覧・編集欄を出したまま、作業領域の先頭に「最新情報を取得しています」と経過時間を出す。
  const processing = panel.getByTestId("feedback-management-refresh-processing");
  await expect(processing).toBeVisible();
  await expect(processing).toContainText("最新情報を取得しています");
  await expect(processing).toHaveAttribute("data-processing-placement", "workspace");
  await expect(processing).toHaveAttribute("data-processing-activity-icon", "none");
  await expect(panel).toHaveAttribute("aria-busy", "true");
  await expect(feedbackHistoryList(page)).toBeVisible();
  await expect(panel.getByTestId("app-feedback-editor-pane")).toBeVisible();
  await expect(panel.getByText("生成 SQL", { exact: true })).toBeVisible();
  await expect(panel.getByTestId("feedback-history-question").first()).toHaveText(firstQuestion);
  await expect(panel.getByTestId("app-feedback-list-loading")).toHaveCount(0);
  await expect(panel.getByTestId("app-feedback-editor-skeleton")).toHaveCount(0);
  // スピナーは押した「最新情報を取得」の 1 つだけ。
  const refreshButton = page.getByTestId("feedback-management-actions").getByRole("button", { name: "最新情報を取得" });
  await expect(refreshButton).toHaveAttribute("aria-busy", "true");
  await expectSingleSpinner(page, page.getByTestId("feedback-management-actions"));
  await expectNoHorizontalScroll(page);

  // 375px は 1 ペイン（選んだ履歴の詳細だけ）。
  await page.setViewportSize({ width: 375, height: 900 });
  await expect(processing).toBeVisible();
  await expect(panel.getByTestId("app-feedback-editor-pane")).toBeVisible();
  await expectNoHorizontalScroll(page);

  refreshGate.release();
  await expect(processing).toHaveCount(0);
  await expect(panel).not.toHaveAttribute("aria-busy", "true");
  await expect(panel.getByTestId("app-feedback-editor-pane")).toBeVisible();
  await backToFeedbackHistoryList(page);
  await expect(feedbackHistoryList(page)).toBeVisible();
});

test("admin good feedback is available as similar history without manual index rebuild", async ({ page }) => {
  await mockNl2SqlApi(page);
  let rebuildRequested = false;
  await page.route("**/api/nl2sql/feedback-index/rebuild", async (route) => {
    rebuildRequested = true;
    await route.fallback();
  });

  await page.goto("/feedback-management?tab=appFeedback&feedback=hist-001");
  await chooseSelectFieldOption(page.getByRole("combobox", { name: "管理者レビュー結果", exact: true }), "good");
  await page.getByRole("button", { name: "フィードバック保存" }).click();
  await expect(page.getByText("管理者レビューを保存し、類似検索に公開しました。")).toBeVisible();
  expect(rebuildRequested).toBe(false);

  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill("履歴から再実行したい請求金額");
  const similarHistoryHeader = page.getByRole("button", { name: /参考履歴/ });
  await expect(similarHistoryHeader).toBeVisible();
  await expect(similarHistoryHeader).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByTestId("nl2sql-similar-history-item")).toContainText(historySql);
  expect(rebuildRequested).toBe(false);
});

test("feedback management keeps long query text contained in app feedback and similarity settings", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop project covers split-pane resizing and mobile stacking");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockNl2SqlApi(page);
  await page.route(/\/api\/nl2sql\/feedback(?:\?.*)?$/, (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return fulfillJson(route, {
      items: [
        {
          ...longHistoryItem,
          training_status: "pending",
          training_example_id: "",
        },
      ],
      total: 1,
      next_cursor: "",
    });
  });

  await page.goto(`/feedback-management?tab=appFeedback&feedback=${longHistoryItem.id}`);
  const historyPane = feedbackHistoryList(page);
  const editorPane = page.getByTestId("app-feedback-editor-pane");
  const row = page.getByTestId("feedback-history-row").first();
  const rowQuestion = row.getByTestId("feedback-history-question");
  const selectedQuestion = page.getByTestId("app-feedback-selected-question");
  await expect(rowQuestion).toContainText("対象テーブル");
  await expect(selectedQuestion).toContainText("対象テーブル");
  // 一覧の行（狭い列）の質問は 2 行まで折り返す（#1764・#2283）。切り詰めは題名のボタンの題名の要素が持つ。
  await expect(rowQuestion.locator("xpath=..")).toHaveCSS("-webkit-line-clamp", "2");
  await expectQuestionClamp(selectedQuestion, longHistoryItem.question, 3);
  await expectHorizontallyContained(row, historyPane);
  await expectHorizontallyContained(rowQuestion.locator("xpath=.."), row);
  await expectHorizontallyContained(selectedQuestion, editorPane);
  // 編集欄の上に履歴の選択欄は置かない（一覧と前へ / 次へ で選ぶ。#2283）。
  await expect(page.getByRole("combobox", { name: "対象履歴" })).toHaveCount(0);
  const questionExpandButton = page.getByRole("button", { name: "全文表示" });
  await expect(questionExpandButton.locator('svg[data-state]')).toHaveAttribute("data-state", "collapsed");
  await questionExpandButton.click();
  const questionCollapseButton = page.getByRole("button", { name: "閉じる" });
  await expect(questionCollapseButton).toBeVisible();
  await expect(questionCollapseButton.locator('svg[data-state]')).toHaveAttribute("data-state", "expanded");
  await expectNoHorizontalScroll(page);

  await page.getByRole("tab", { name: "類似検索インデックス" }).click();
  const similarityPanel = page.locator("#feedback-management-panel-similarityIndex");
  await expect(similarityPanel.getByLabel("最低スコア", { exact: true })).toBeVisible();
  // 未保存の環境の既定は 0.9（#1721）。
  await expect(similarityPanel.getByLabel("最低スコア", { exact: true })).toHaveValue("0.9");
  await expect(similarityPanel.getByLabel("最大候補数", { exact: true })).toBeVisible();
  await expect(similarityPanel.getByText("索引候補", { exact: true })).toHaveCount(0);
  await expect(similarityPanel.getByTestId("feedback-vector-entry")).toHaveCount(0);
  await expectNoHorizontalScroll(page);

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(similarityPanel.getByLabel("最低スコア", { exact: true })).toBeVisible();
  await expect(similarityPanel.getByLabel("最大候補数", { exact: true })).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test("app feedback uses the shared responsive pagination for offset pages", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const api = await mockNl2SqlApi(page);
  await page.unroute(/\/api\/nl2sql\/feedback(?:\?.*)?$/);

  const singlePageItems = Array.from({ length: 40 }, (_, index) => ({
    ...historyItem,
    id: `single-page-feedback-${index + 1}`,
    question: `単一ページのフィードバック ${index + 1}`,
    training_status: "pending",
    training_example_id: "",
  }));
  const cursorPageItems = Array.from({ length: 51 }, (_, index) => ({
    ...historyItem,
    id: `cursor-feedback-${index + 1}`,
    question: `ページング対象 ${index + 1}`,
    training_status: "pending",
    training_example_id: "",
  }));

  await page.route(/\/api\/nl2sql\/feedback(?:\?.*)?$/, (route) => {
    const url = new URL(route.request().url());
    const source = url.searchParams.get("q") === "ページング対象" ? cursorPageItems : singlePageItems;
    // 1 ページは共通の 25 件（#1764 / #1952）。offset 型（#1891）。
    const limit = Number(url.searchParams.get("limit") ?? "25");
    const offset = Number(url.searchParams.get("offset") ?? "0");
    const items = source.slice(offset, offset + limit);
    return fulfillJson(route, {
      items,
      total: source.length,
      limit,
      offset,
      has_next: offset + items.length < source.length,
      unfiltered_total: singlePageItems.length + cursorPageItems.length,
    });
  });

  await page.goto("/feedback-management?tab=appFeedback");

  const historyPane = feedbackHistoryList(page);
  const rows = historyPane.getByTestId("feedback-history-row");
  const pagination = historyPane.getByTestId("app-feedback-pagination");
  // 40 件は 25 件ずつの 2 ページ。
  await expect(rows).toHaveCount(25);
  await expect(pagination).toContainText("1–25 / 40 件");

  // 検索語は入力に合わせて適用する（debounce。#535）。
  await page.getByRole("searchbox", { name: "履歴検索" }).fill("ページング対象");

  await expect(rows).toHaveCount(25);
  await expect(pagination).toBeVisible();
  await expect(
    historyPane.getByRole("navigation", { name: "フィードバック履歴一覧のページ切替" })
  ).toBeVisible();
  await expect(pagination).toContainText("1–25 / 51 件");
  await expect(pagination).toContainText("1 / 3 ページ");
  const previousButton = pagination.getByRole("button", { name: "前へ" });
  const nextButton = pagination.getByRole("button", { name: "次へ" });
  await expect(previousButton).toBeDisabled();
  await expect(nextButton).toBeEnabled();
  await pagination.scrollIntoViewIfNeeded();
  await expectHorizontallyContained(pagination, historyPane);
  await expectNoHorizontalScroll(page);

  await nextButton.click();
  await expect(rows).toHaveCount(25);
  await expect(pagination).toContainText("2 / 3 ページ");
  await nextButton.click();
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("ページング対象 51");
  // 先頭の行を自動で選ばない（#2283）。行を選ぶと詳細に出る。
  await expect(rows.first()).not.toHaveAttribute("aria-current", /.*/);
  await rows.first().click();
  await expect(rows.first()).toHaveAttribute("aria-current", "true");
  await expect(page.getByTestId("app-feedback-selected-question")).toContainText("ページング対象 51");
  await expect(pagination).toContainText("51–51 / 51 件");
  await expect(pagination).toContainText("3 / 3 ページ");
  await expect(previousButton).toBeEnabled();
  await expect(nextButton).toBeDisabled();

  await page.getByRole("button", { name: "フィードバック保存" }).click();
  await expect(page.getByText("管理者レビューを保存し、類似検索に公開しました。")).toBeVisible();
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("ページング対象 51");
  await expect(rows.first()).toHaveAttribute("aria-current", "true");
  await expect(page.getByTestId("app-feedback-selected-question")).toContainText("ページング対象 51");
  expect(api.adminFeedbackPayload).toMatchObject({
    history_id: "cursor-feedback-51",
  });
  await expect(pagination).toContainText("51–51 / 51 件");
  await expect(pagination).toContainText("3 / 3 ページ");

  await previousButton.focus();
  await expect(previousButton).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(rows).toHaveCount(25);
  await expect(pagination).toContainText("26–50 / 51 件");
  await expect(previousButton).toBeEnabled();

  // 375px は 1 ペイン。選んだ履歴（前のページ）の詳細から一覧へ戻ってページ送りを確かめる。
  await page.setViewportSize({ width: 375, height: 900 });
  await backToFeedbackHistoryList(page);
  await pagination.scrollIntoViewIfNeeded();
  await expect(pagination).toBeVisible();
  await expectHorizontallyContained(pagination, historyPane);
  await expectNoHorizontalScroll(page);
});

test("feedback management keeps utility actions usable in empty and load error states", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  await page.route("**/api/nl2sql/feedback-config", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Feedback 類似検索設定を取得できません。" }),
    })
  );

  await page.goto("/feedback-management?tab=appFeedback");

  await expect(page.getByRole("alert")).toContainText("Feedback 類似検索設定を取得できません。");
  // 失敗の通知は「再試行」、ヘッダーは「最新情報を取得」で、名前で区別できる（#1868）。
  await expect(page.getByRole("alert").getByRole("button", { name: "再試行" })).toBeVisible();
  // 共有 PageHeader の utility 操作は secondary / md(36px、タッチ端末 44px)。
  const reloadButton = page.getByTestId("feedback-management-actions").getByRole("button", { name: "最新情報を取得", exact: true });
  await expect(reloadButton).toHaveCSS("height", testInfo.project.name === "mobile-375" ? "44px" : "36px");
  await expect(reloadButton).not.toHaveCSS("border-top-color", "rgba(0, 0, 0, 0)");
  await reloadButton.focus();
  await expect(reloadButton).toBeFocused();
  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
});

test("app feedback keeps the history list narrow beside the editor and switches to one pane below xl (#2283)", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop project covers desktop widths and the 375px single pane");
  await page.setViewportSize({ width: 2048, height: 1100 });
  await page.addInitScript(() => {
    if (window.sessionStorage.getItem("feedback-list-detail-cleared")) return;
    window.localStorage.removeItem("production-ready-nl2sql.listDetail.feedback-management");
    window.sessionStorage.setItem("feedback-list-detail-cleared", "true");
  });
  await mockNl2SqlApi(page);
  await page.goto("/feedback-management?tab=appFeedback");

  const layout = page.getByTestId("list-detail-feedback-management");
  const list = feedbackHistoryList(page);
  const detail = page.getByTestId("list-detail-feedback-management-detail");
  const historyRow = page.getByTestId("feedback-history-row").first();
  const editorPane = page.getByTestId("app-feedback-editor-pane");
  const generatedSql = page.getByLabel("生成 SQL");

  await expect(layout).toHaveAttribute("data-list-detail-layout", "split");
  await expect(list.getByRole("heading", { name: "フィードバック履歴" })).toBeVisible();
  // 先頭の行を自動で選ばない。未選択の詳細は選び方を案内する。
  await expect(detail).toContainText("フィードバックが未選択です");
  await expect(page).not.toHaveURL(/feedback=/);
  await historyRow.click();
  await expect(page).toHaveURL(/feedback=hist-001/);
  await expect(detail.getByRole("heading", { name: "アプリ内フィードバック" })).toBeVisible();
  await expectHorizontallyContained(historyRow, list);
  await expectHorizontallyContained(editorPane, detail);
  await expectHorizontallyContained(generatedSql, editorPane);
  // 一覧は既定 20rem（280px）の狭い列のまま、詳細は残りの広い列。
  const [listBox, detailBox] = await Promise.all([list.boundingBox(), detail.boundingBox()]);
  expect(listBox!.width).toBeCloseTo(280, 0);
  expect(detailBox!.x).toBeGreaterThan(listBox!.x + listBox!.width);
  expect(detailBox!.width).toBeGreaterThan(listBox!.width * 2);
  await expectNoHorizontalScroll(page);

  // 1440px: 一覧を最大（32rem）まで広げても、行・編集欄が列の外にはみ出さない。
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(layout).toHaveAttribute("data-list-detail-layout", "split");
  const resizer = page.getByTestId("list-detail-feedback-management-resizer");
  await resizer.focus();
  for (let step = 0; step < 4; step += 1) await page.keyboard.press("Shift+ArrowRight");
  await expect(resizer).toHaveAttribute("aria-valuenow", "32");
  await expectHorizontallyContained(historyRow, list);
  await expectHorizontallyContained(editorPane, detail);
  await expectHorizontallyContained(generatedSql, editorPane);
  await expectNoHorizontalScroll(page);
  await resizer.press("Home");
  await expect(resizer).toHaveAttribute("aria-valuenow", "20");

  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.evaluate(() => document.documentElement.classList.add("dark"));
  await expectNoHorizontalScroll(page);

  // 375px: 1 ペイン。選んだ履歴の詳細だけを出し、確定の行の操作は全幅。
  await page.setViewportSize({ width: 375, height: 900 });
  await expect(layout).toHaveAttribute("data-list-detail-layout", "single");
  await expect(list).toBeHidden();
  await expect(detail).toBeVisible();
  const mobileActionBar = page.getByTestId("feedback-app-actions");
  const mobileActionControls = [
    mobileActionBar.getByRole("button", { name: "フィードバック保存" }),
    mobileActionBar.getByRole("link", { name: "学習候補で確認" }),
    mobileActionBar.getByRole("button", { name: "その他の操作" }),
  ];
  const mobileActionBarBox = await mobileActionBar.boundingBox();
  expect(mobileActionBarBox).not.toBeNull();
  for (const action of mobileActionControls) {
    const actionBox = await action.boundingBox();
    expect(actionBox).not.toBeNull();
    expect(actionBox!.width).toBeCloseTo(mobileActionBarBox!.width, 0);
    await action.focus();
    await expect(action).toBeFocused();
    await expectHorizontallyContained(action, editorPane);
  }
  await mobileActionBar.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "フィードバックを解除" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expectNoHorizontalScroll(page);

  // 「一覧へ戻る」（左上）で一覧へ戻り、選んでいた行へフォーカスを戻す。検索欄は一覧の列の全幅。
  await page.getByTestId("app-feedback-detail-back").click();
  await expect(page).not.toHaveURL(/feedback=/);
  await expect(list).toBeVisible();
  await expect(detail).toBeHidden();
  await expect(historyRow).toBeFocused();
  const mobileFeedbackFilters = page.getByTestId("feedback-app-filters");
  const mobileFeedbackSearch = mobileFeedbackFilters.getByRole("searchbox", { name: "履歴検索" });
  const [mobileFilterFormBox, mobileSearchBox] = await Promise.all([
    mobileFeedbackFilters.boundingBox(),
    mobileFeedbackSearch.boundingBox(),
  ]);
  expect(mobileSearchBox!.width).toBeCloseTo(mobileFilterFormBox!.width, 0);
  expect(mobileSearchBox!.height).toBe(await expectedControlHeight(page));
  // 「絞り込み」ボタンは置かない（#535）。
  await expect(mobileFeedbackFilters.locator("button:not([aria-haspopup=dialog])").filter({ hasText: /^絞り込み$/ })).toHaveCount(0); // 狭い画面のシートを開くボタン（#1933）は除く
  await expectNoHorizontalScroll(page);
});

test("shared split panes reserve their divider track across NL2SQL management pages", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop-only cross-page geometry audit");
  // 8 つの管理画面を順に読み込む長いテストで、2 worker の nightly では 30 秒を超えた（retry を含めて失敗。#1519）。
  test.slow();
  await page.setViewportSize({ width: 2048, height: 1000 });
  await mockNl2SqlApi(page);

  const scenarios: Array<{ path: string; splitIds: string[] }> = [
    // テーブル・ビューの管理は ListDetailLayout に移した（#2281。table-management-list-detail.spec.ts）。
    // データの管理のデータの表示も ListDetailLayout に移した（#2315。data-management-list-detail.spec.ts）。
    { path: "/sample-data", splitIds: ["sample-data-import"] },
    { path: "/sql-to-question", splitIds: ["sql-to-question-input"] },
    // フィードバック履歴（アプリ内フィードバック）は ListDetailLayout に移した（#2283）。
  ];

  for (const scenario of scenarios) {
    await page.goto(scenario.path);
    for (const splitId of scenario.splitIds) {
      const pane = page.getByTestId(`fixed-split-pane-${splitId}`);
      await expect(pane).toHaveAttribute("data-split-layout", "split", {
        timeout: SPLIT_PANE_RENDER_TIMEOUT_MS,
      });
      await expectSplitPaneReservedTrack(pane);
    }
    await expectNoHorizontalScroll(page);
    await expectLocalUiFonts(page);
  }
});

test("legacy learning route redirects to feedback management", async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.goto("/learning");
  await expect(page).toHaveURL(/\/feedback-management$/);
  await expect(page.getByRole("heading", { name: "フィードバック管理" })).toBeVisible();
  await expect(page.getByRole("link", { name: /フィードバック学習/ })).toHaveCount(0);
});

test("question classifier training data follows the CATEGORY/TEXT contract", async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);

  await page.goto("/question-classifier-models");
  await page.getByRole("tab", { name: "訓練データ" }).click();
  const trainingWorkspace = page.getByRole("tabpanel", { name: "訓練データ" });

  await expect(trainingWorkspace.getByRole("heading", { name: "訓練データ一覧" })).toBeVisible();
  await expect(trainingWorkspace.getByRole("combobox", { name: "業務プロファイル" })).toHaveCount(0);
  await expect(trainingWorkspace.getByRole("link", { name: "Training JSONL 出力" })).toHaveCount(0);
  await expect(trainingWorkspace.getByRole("link", { name: "Training XLSX 出力" })).toBeVisible();
  await expect(
    trainingWorkspace.getByText("旧モデル管理と同じ CATEGORY / TEXT 形式の訓練データを一覧・取込・出力します。")
  ).toHaveCount(0);
  await expect(trainingWorkspace.getByText("既存 training data を置き換える")).toBeVisible();
  await expect(trainingWorkspace.getByTestId("qcm-training-file-field-input")).toHaveAttribute(
    "accept",
    ".xlsx"
  );
  await expect(trainingWorkspace.getByText(".XLSX", { exact: true })).toBeVisible();
  const clearFilename = trainingWorkspace.getByRole("button", { name: "ファイル名表示を消去", exact: true });
  await expect(clearFilename).toHaveText("ファイル名表示を消去");
  await expect(clearFilename).toBeDisabled();

  const replaceTrainingData = trainingWorkspace.getByRole("checkbox", {
    name: "既存 training data を置き換える",
  });
  await replaceTrainingData.check();
  await dropFiles(page, trainingWorkspace.getByTestId("qcm-training-file-field-dropzone"), [
    {
      name: "replacement.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock xlsx",
    },
  ]);
  const replaceDialog = page.getByRole("alertdialog");
  await expect(replaceDialog.getByRole("heading", { name: "訓練データを置き換えますか" })).toBeVisible();
  await expect(replaceDialog.getByText(/既存の訓練データ 27 件/u)).toBeVisible();
  await replaceDialog.getByRole("button", { name: "キャンセル" }).click();
  expect(api.classifierTrainingImportBody).toBeNull();
  await expect(trainingWorkspace.getByText("選択中: replacement.xlsx")).toHaveCount(0);
  await expect(trainingWorkspace.getByText("ページング対象 01: 請求金額が大きい取引先を見たい")).toBeVisible();
  await replaceTrainingData.uncheck();

  await dropFiles(page, trainingWorkspace.getByTestId("qcm-training-file-field-dropzone"), [
    {
      name: "training_data.csv",
      type: "text/csv",
      content: "CATEGORY,TEXT\n監査,監査ログを確認したい\n",
    },
  ]);
  await expect(
    trainingWorkspace.getByText(
      "このファイル形式は使用できません。.XLSX ファイルを選択してください。"
    )
  ).toBeVisible();
  await dropFiles(page, trainingWorkspace.getByTestId("qcm-training-file-field-dropzone"), [
    {
      name: "training_data.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock xlsx",
    },
  ]);
  await expect(page.getByText("1 件の training data を取り込みました（合計 28 件）。")).toBeVisible();
  expect(api.classifierTrainingImportBody).toContain('name="file"');
  expect(api.classifierTrainingImportBody).toContain('name="replace"');
  expect(api.classifierTrainingImportBody).not.toContain('name="profile_id"');
  await expect(trainingWorkspace.getByText("選択中: training_data.xlsx")).toBeVisible();
  const mutationsAfterImport: string[] = [];
  page.on("request", request => {
    if (request.method() !== "GET" && request.url().includes("/classifier/training-data")) mutationsAfterImport.push(request.url());
  });
  await clearFilename.focus();
  await expect(clearFilename).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("training-filename-clear.png"), fullPage: true });
  await clearFilename.press("Enter");
  await expect(trainingWorkspace.getByText("選択中: training_data.xlsx")).toHaveCount(0);
  await expect(clearFilename).toBeDisabled();
  await expect(page.getByText("1 件の training data を取り込みました（合計 28 件）。")).toBeVisible();
  await expect(trainingWorkspace.getByText("ページング対象 01: 請求金額が大きい取引先を見たい")).toBeVisible();
  expect(mutationsAfterImport).toEqual([]);
  await expectNoHorizontalScroll(page);

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(page.getByRole("link", { name: "Training XLSX 出力" })).toBeVisible();
  await expectNoHorizontalScroll(page);

  await page.getByRole("tab", { name: "学習候補" }).click();
  await expect(page.getByText("フィードバック学習候補", { exact: true })).toBeVisible();
  await expect(page.getByText("履歴から再実行したい請求金額")).toBeVisible();
  const conflictCandidate = page
    .getByTestId("qcm-training-candidate")
    .filter({ hasText: "競合している請求分類を確認したい" });
  const changedCandidate = page
    .getByTestId("qcm-training-candidate")
    .filter({ hasText: "元 feedback が変更された質問" });
  await expect(conflictCandidate.getByText("業務プロファイルの競合", { exact: true })).toBeVisible();
  await expect(changedCandidate.getByText("元 feedback 変更あり", { exact: true })).toBeVisible();
  await expect(page.getByLabel("競合している請求分類を確認したい を選択")).toBeDisabled();
  await expect(page.getByLabel("元 feedback が変更された質問 を選択")).toBeDisabled();
  await expect(page.getByRole("button", { name: "推薦・書き換え" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "類似履歴検索" })).toHaveCount(0);
});

test("question classifier model management page trains classifier and finds learning candidates", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/classifier/predict");
  await page.route("**/api/nl2sql/classifier/predict", (route) =>
    fulfillJson(route, {
      recommendation_source: "classifier",
      classifier_version: "classifier-002",
      predicted_category: "既定プロファイル",
      confidence: 0.92,
      candidates: Array.from({ length: 30 }, (_, index) => ({
        category: index === 0 ? "既定プロファイル" : `候補カテゴリ ${index + 1}`,
        score: index === 0 ? 0.92 : Math.max(0.1, 0.9 - index * 0.02),
        profile_id: index === 0 ? "default" : `profile-${index + 1}`,
        profile_name: index === 0 ? "既定プロファイル" : `候補プロファイル ${index + 1}`,
      })),
      warnings: [],
    })
  );

  await page.goto("/question-learning");
  await expect(page).toHaveURL(/\/question-classifier-models$/);
  await expect(page.getByRole("heading", { name: "質問分類モデル管理" })).toBeVisible();
  await expect(page.getByText("質問学習", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Model registry")).toHaveCount(0);
  await expect(page.getByText("Legacy artifact 取込")).toHaveCount(0);
  await expect(page.getByText("Model artifact 取込")).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "モデル一覧" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "訓練データ" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "モデル学習" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "モデルテスト" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "学習候補" })).toBeVisible();
  await expect(page.getByText("フィードバック保存")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "訓練データ一覧" })).toBeVisible();
  const classifierStatus = page.getByTestId("qcm-model-status");
  // PageHeaderStatusBadge は可視ラベル + sr-only 通知文の 2 重構造(意図的な a11y 設計)。
  // 共有 StatusBadge 内のアイコン svg も aria-hidden を持つため、直下の可視ラベル wrapper に限定する。
  await expect(classifierStatus.locator('> [aria-hidden="true"]')).toHaveText("学習済み");
  await expect(page.getByText(/最終更新日時:/)).toBeVisible();
  expect(api.classifierModelListRequests).toBe(0);

  const trainingDataTab = page.getByRole("tab", { name: "訓練データ" });
  await trainingDataTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "モデル学習" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { name: "モデル学習" })).toBeVisible();

  await trainingDataTab.click();
  await expect(page.getByRole("heading", { name: "訓練データ一覧" })).toBeVisible();
  await expect(page.getByTestId("qcm-training-data-table").getByText("CATEGORY")).toBeVisible();
  await expect(page.getByText("請求金額が大きい取引先を見たい")).toBeVisible();
  await expect(page.getByText("ページング対象 26: 訓練データ確認 26")).toHaveCount(0);
  await expect(page.getByTestId("qcm-training-data-pagination")).toContainText("1–25 / 27 件");
  await expect(page.getByTestId("qcm-training-data-pagination")).toContainText("1 / 2 ページ");
  await page.getByTestId("qcm-training-data-pagination").getByRole("button", { name: "次へ" }).click();
  await expect(page.getByText("ページング対象 26: 訓練データ確認 26")).toBeVisible();
  await expect(page.getByText("請求金額が大きい取引先を見たい")).toHaveCount(0);
  await expect(page.getByTestId("qcm-training-data-pagination")).toContainText("26–27 / 27 件");
  await page.getByTestId("qcm-training-data-pagination").getByRole("button", { name: "前へ" }).click();
  await expect(page.getByText("請求金額が大きい取引先を見たい")).toBeVisible();
  // ページ番号を入れて移る（共通の Pagination。#1891）。
  await jumpToPage(page.getByTestId("qcm-training-data-pagination"), 2);
  await expect(page.getByTestId("qcm-training-data-pagination")).toContainText("26–27 / 27 件");
  await page.getByPlaceholder("CATEGORY / TEXT / SOURCE で絞り込み").fill("ページング対象");
  await expect(page.getByTestId("qcm-training-data-pagination")).toContainText("1–25 / 27 件");
  await expect(page.getByTestId("qcm-training-data-pagination")).toContainText("1 / 2 ページ");
  await expect(page.getByText("請求金額が大きい取引先を見たい")).toBeVisible();
  await expect(page.getByText("ページング対象 26: 訓練データ確認 26")).toHaveCount(0);
  // ヘッダーの「最新情報を取得」と、訓練データのパネルの「訓練データの最新情報を取得」は名前で区別できる（#1868）。
  await expect(page.getByRole("button", { name: "最新情報を取得", exact: true })).toHaveCount(1);
  await page.getByRole("button", { name: "訓練データの最新情報を取得" }).click();

  await page.getByRole("tab", { name: "モデルテスト" }).click();
  await page.getByRole("button", { name: "分類を試す" }).click();
  await expect(page.getByText("信頼度 92%")).toBeVisible();
  await expect(page.getByText("予測カテゴリ", { exact: true })).toBeVisible();
  await expect(page.locator("td").filter({ hasText: /^92%$/ }).first()).toBeVisible();
  const testCandidates = page.getByTestId("qcm-test-candidates-scroll-region");
  await expect(testCandidates.locator("tbody tr")).toHaveCount(30);
  await expectInformationTableRowLimit(
    testCandidates,
    "tbody tr",
    expectedInformationRows(testInfo)
  );

  await page.getByRole("tab", { name: "学習候補" }).click();
  await expect(page.getByText("フィードバック学習候補", { exact: true })).toBeVisible();
  await expect(page.getByText("履歴から再実行したい請求金額")).toBeVisible();
  await page.getByLabel("履歴から再実行したい請求金額 を選択").check();
  await page.getByRole("button", { name: "選択した 1 件を追加" }).click();
  const addDialog = page.getByRole("alertdialog", { name: "訓練データへ追加しますか" });
  await expect(addDialog).toBeVisible();
  await addDialog.getByRole("button", { name: "選択した候補を追加" }).click();
  await expect(page.getByText("1 件を訓練データへ追加しました。")).toBeVisible();
  expect(api.classifierFeedbackImportPayload).toEqual({
    items: [{ history_id: "hist-001", profile_id: "default" }],
  });
  await expect(classifierStatus.locator('> [aria-hidden="true"]')).toHaveText("学習済み・再学習待ち");
  await expect(page.getByRole("button", { name: "推薦・書き換え" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "類似履歴検索" })).toHaveCount(0);

  await page.getByRole("tab", { name: "訓練データ" }).click();
  await page.getByPlaceholder("CATEGORY / TEXT / SOURCE で絞り込み").fill("履歴から再実行");
  await expect(page.getByText("SQL feedback", { exact: true })).toBeVisible();
  await expect(page.getByText("feedback:hist-001", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "モデル学習" }).click();
  await page.getByRole("button", { name: "Classifier 学習" }).click();
  await expect(page.getByText("LogisticRegression classifier を学習しました。")).toBeVisible();
  await expect(classifierStatus.locator('> [aria-hidden="true"]')).toHaveText("学習済み");

  await page.setViewportSize({ width: 375, height: 900 });
  await page.getByRole("tab", { name: "訓練データ" }).click();
  await expect(page.getByRole("link", { name: "Training XLSX 出力" })).toBeVisible();
  await expect(page.getByText("Legacy artifact 取込")).toHaveCount(0);
  await expect(page.getByText("Model artifact 取込")).toHaveCount(0);
  await expectNoHorizontalScroll(page);

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/question-classifier-models");
  await expect(page.getByRole("heading", { name: "訓練データ一覧" })).toBeVisible();
  expect(api.classifierModelListRequests).toBe(0);
  // 検索語は作業状態に残る（#1891）。画面を開き直しても前の検索語のままなので、消してから全件を見る。
  const trainingSearch = page.getByPlaceholder("CATEGORY / TEXT / SOURCE で絞り込み");
  await expect(trainingSearch).toHaveValue("履歴から再実行");
  await trainingSearch.fill("");
  await trainingSearch.press("Enter");

  const firstTrainingRow = page
    .getByTestId("qcm-training-data-table")
    .locator("tbody tr")
    .filter({ hasText: "請求金額が大きい取引先を見たい" });
  await expect(firstTrainingRow).toBeVisible();
  await expect(firstTrainingRow.getByRole("button", { name: "編集", exact: true })).toHaveCount(0);
  await expect(firstTrainingRow.getByRole("button", { name: "削除", exact: true })).toHaveCount(0);
  await firstTrainingRow.getByTestId("qcm-training-row-actions-example-001-trigger").click();
  const editTrainingMenuItem = page.getByRole("menuitem", { name: "編集", exact: true });
  const deleteTrainingMenuItem = page.getByRole("menuitem", { name: "削除", exact: true });
  await expect(editTrainingMenuItem).toBeVisible();
  await expect(deleteTrainingMenuItem).toBeVisible();
  await expect(deleteTrainingMenuItem).toHaveAttribute("data-entity-action-tone", "danger");
  await editTrainingMenuItem.click();
  await expect(firstTrainingRow.getByRole("button", { name: "保存", exact: true })).toBeVisible();
  await firstTrainingRow.getByRole("button", { name: "キャンセル", exact: true }).click();
  await clickRowAction(page, "qcm-training-row-actions-example-001", "削除");
  const deleteTrainingDialog = page.getByRole("alertdialog", { name: "訓練データを削除しますか" });
  await expect(deleteTrainingDialog).toBeVisible();
  await deleteTrainingDialog.getByRole("button", { name: "削除", exact: true }).click();
  await expect(page.getByText("訓練データを削除しました。モデルは再学習待ちです。")).toBeVisible();
  expect(api.classifierTrainingDeleteId).toBe("example-001");
  await expect(page.getByText("請求金額が大きい取引先を見たい")).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("question classifier model management shows the training failure under the train button", { tag: "@responsive" }, async ({
  page,
}) => {
  await mockNl2SqlApi(page);
  const trainingError = "LogisticRegression には 2 category 以上の training data が必要です。";
  await page.unroute("**/api/nl2sql/classifier/train");
  await page.route("**/api/nl2sql/classifier/train", (route) =>
    route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: [trainingError],
      }),
    })
  );

  await page.goto("/question-classifier-models");
  await page.getByRole("tab", { name: "モデル学習" }).click();
  await page.getByRole("button", { name: "Classifier 学習" }).click();

  // 失敗は学習のボタンの直下（モデル学習のタブの中）に 1 か所だけ出し、ページ先頭・Toast に重ねない（messaging.md §10.1、#724）。
  const trainPanel = page.getByRole("tabpanel", { name: "モデル学習" });
  await expect(trainPanel.getByRole("alert")).toContainText(trainingError);
  await expect(page.locator("main").getByRole("alert")).toHaveCount(1);
  await expect(
    page.getByRole("region", { name: "通知" }).getByRole("alert").filter({ hasText: trainingError })
  ).toHaveCount(0);
  const trainButtonBox = await trainPanel.getByRole("button", { name: "Classifier 学習" }).boundingBox();
  const alertBox = await trainPanel.getByRole("alert").boundingBox();
  expect(trainButtonBox && alertBox && alertBox.y > trainButtonBox.y).toBeTruthy();
  await expectNoHorizontalScroll(page);
});

test("feedback management shows the similarity config save failure in its action row", async ({ page }) => {
  await mockNl2SqlApi(page);
  const saveError = "Feedback 類似検索設定を保存できません。";
  await page.route("**/api/nl2sql/feedback-config", (route) =>
    route.request().method() === "PATCH"
      ? route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: saveError }) })
      : route.fallback()
  );

  await page.goto("/feedback-management?tab=similarityIndex");
  await page.getByLabel("最低スコア", { exact: true }).fill("0.85");
  await page.getByRole("button", { name: "設定保存" }).click();

  // 保存の失敗は操作の行（FormActionBar の status）に出し、ページ先頭の通知には出さない（messaging.md §10.1、#724）。
  const actions = page.getByTestId("feedback-similarity-index-actions");
  await expect(actions.getByRole("alert")).toContainText(saveError);
  await expect(page.locator("main").getByRole("alert")).toHaveCount(1);
  await expectNoHorizontalScroll(page);
});

test("question classifier training shows the elapsed time while the model is trained", async ({
  page,
}) => {
  await mockNl2SqlApi(page);
  // 学習の応答を止めて、実行中の表示（#376）を確かめてから既定の mock へ渡す。
  let release: () => void = () => {};
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/nl2sql/classifier/train", async (route) => {
    await released;
    await route.fallback();
  });

  await page.goto("/question-classifier-models");
  await page.getByRole("tab", { name: "モデル学習" }).click();
  await page.getByRole("button", { name: "Classifier 学習" }).click();

  const processing = page.getByTestId("qcm-train-processing");
  await expect(processing).toContainText("質問の embedding を作成し、分類モデルを学習しています");
  await expect(processing.getByRole("timer")).toHaveAccessibleName(/経過時間 \d{2}:\d{2}/u);
  await expect(processing).toHaveAttribute("data-processing-activity-icon", "none");
  await expectNoHorizontalScroll(page);

  release();
  await expect(processing).toHaveCount(0);
});

test("learning candidates keep long query text clear of selection controls", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop project covers desktop and 375px geometry");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockNl2SqlApi(page);
  await page.route("**/api/nl2sql/classifier/training-candidates*", (route) =>
    fulfillJson(route, {
      items: [
        {
          history_id: longHistoryItem.id,
          question: longHistoryItem.question,
          profile_id: "default",
          profile_name: "既定プロファイル",
          profile_category: "既定プロファイル",
          feedback_rating: "good",
          feedback_comment: longHistoryItem.feedback_comment,
          created_at: longHistoryItem.created_at,
          status: "pending",
          training_example_id: "",
          conflict_profile_ids: [],
        },
      ],
      total: 1,
      next_cursor: "",
      pending_count: 1,
      added_count: 0,
      attention_count: 0,
    })
  );

  await page.goto("/question-classifier-models?tab=candidates");
  const candidate = page.getByTestId("qcm-training-candidate").first();
  const question = candidate.getByTestId("qcm-candidate-question");
  const checkbox = candidate.getByRole("checkbox");
  const profile = candidate.getByRole("combobox", { name: "追加する業務プロファイル" });
  const candidateBulkBar = page.getByTestId("qcm-candidate-bulk-actions");
  const candidateBulkActions = page.getByTestId("qcm-candidate-page-selection-actions");
  const addSelectedButton = page.getByRole("button", { name: "選択した 0 件を追加" });
  const expectCandidateBulkLayout = async (expectTrailingAction: boolean) => {
    const [barBox, actionsBox, addButtonBox] = await Promise.all([
      candidateBulkBar.boundingBox(),
      candidateBulkActions.boundingBox(),
      addSelectedButton.boundingBox(),
    ]);
    expect(barBox).not.toBeNull();
    expect(actionsBox).not.toBeNull();
    expect(addButtonBox).not.toBeNull();
    expect(Math.abs(actionsBox!.x - (barBox!.x + 12))).toBeLessThanOrEqual(1);
    if (expectTrailingAction) {
      expect(
        Math.abs(addButtonBox!.x + addButtonBox!.width - (barBox!.x + barBox!.width - 12))
      ).toBeLessThanOrEqual(1);
    }
  };
  await expect(question).toContainText("対象テーブル");
  await expectQuestionClamp(question, longHistoryItem.question, 1);
  await expect(checkbox).toBeVisible();
  await expect(profile).toBeVisible();
  await expectHorizontallyContained(question, candidate);
  await expectHorizontallyContained(profile, candidate);
  await expectCandidateBulkLayout(true);
  await expectNoHorizontalScroll(page);

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(question).toBeVisible();
  await expect(checkbox).toBeVisible();
  await expect(profile).toBeVisible();
  await expectHorizontallyContained(question, candidate);
  await expectHorizontallyContained(profile, candidate);
  await expectCandidateBulkLayout(false);
  await expectNoHorizontalScroll(page);
});

test("learning candidates use the shared responsive list, filters, paging, and recovery states", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop context covers both 1440px and 375px geometry");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockNl2SqlApi(page);

  await page.unroute("**/api/nl2sql/profiles");
  await page.route("**/api/nl2sql/profiles", (route) =>
    fulfillJson(route, [
      { ...profiles[0], category: "請求カテゴリ" },
      {
        ...profiles[0],
        id: "payment",
        name: "入金管理",
        category: "入金カテゴリ",
      },
    ])
  );
  // 候補の Profile picker は増分 API(/profiles/search)から一覧を取るため、こちらも同じ内容で上書きする。
  await page.unroute("**/api/nl2sql/profiles/search?*");
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [
        { ...profiles[0], category: "請求カテゴリ" },
        { ...profiles[0], id: "payment", name: "入金管理", category: "入金カテゴリ" },
      ].map((profile, index) => ({
        id: profile.id,
        name: profile.name,
        category: profile.category,
        description: profile.description,
        archived: false,
        allowed_table_count: profile.allowed_tables.length,
        allowed_view_count: profile.allowed_views.length,
        glossary_count: Object.keys(profile.glossary).length,
        few_shot_count: profile.few_shot_examples.length,
        version: 1,
        etag: `etag-search-${index}`,
        updated_at: "2026-06-21T10:00:00.000Z",
      })),
      next_cursor: null,
      total: 2,
      change_token: 1,
    })
  );

  const initialCandidate = {
    history_id: "hist-001",
    question: "履歴から再実行したい請求金額",
    profile_id: "default",
    profile_name: "既定プロファイル",
    profile_category: "請求カテゴリ",
    feedback_rating: "good",
    feedback_comment: "SQL は期待通りです",
    created_at: historyItem.created_at,
    status: "pending",
    training_example_id: "",
    conflict_profile_ids: [],
  };
  const secondPageCandidate = {
    ...initialCandidate,
    history_id: "hist-026",
    question: "26 件目のページング対象候補",
    feedback_comment: "次ページの候補です",
  };
  const initialGate = createRequestGate();
  let holdInitialRequests = true;
  let holdNoResultsRequest = false;
  let failRecoveryRequest = true;
  const candidateRequests: URL[] = [];
  const noResultsGate = createRequestGate();

  await page.unroute("**/api/nl2sql/classifier/training-candidates*");
  await page.route("**/api/nl2sql/classifier/training-candidates*", async (route) => {
    const url = new URL(route.request().url());
    candidateRequests.push(url);
    if (holdInitialRequests) {
      await initialGate.promise;
    }
    if (url.searchParams.get("q") === "error" && failRecoveryRequest) {
      failRecoveryRequest = false;
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          data: null,
          error_messages: ["学習候補を取得できません。接続状態を確認して再試行してください。"],
        }),
      });
    }
    if (url.searchParams.get("q") === "一致なし") {
      if (holdNoResultsRequest) {
        await noResultsGate.promise;
      }
      return fulfillJson(route, {
        items: [],
        total: 0,
        next_cursor: "",
        pending_count: 0,
        added_count: 0,
        attention_count: 0,
      });
    }
    if (url.searchParams.get("offset") === "25") {
      return fulfillJson(route, {
        items: [secondPageCandidate],
        total: 51,
        pending_count: 51,
        added_count: 0,
        attention_count: 0,
      });
    }
    return fulfillJson(route, {
      items: [initialCandidate],
      total: 51,
      pending_count: 51,
      added_count: 0,
      attention_count: 0,
    });
  });

  let failedImportPayload: Record<string, unknown> | null = null;
  await page.unroute("**/api/nl2sql/classifier/training-data/from-feedback");
  await page.route("**/api/nl2sql/classifier/training-data/from-feedback", (route) => {
    failedImportPayload = route.request().postDataJSON() as Record<string, unknown>;
    return route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: ["学習候補を追加できません。Profile を確認して再試行してください。"],
      }),
    });
  });

  const navigation = page.goto("/question-classifier-models?tab=candidates");
  await expect(page.getByTestId("qcm-candidates-list-skeleton")).toBeVisible();
  // 初回の読込のスピナーは PageHeader の「最新情報を取得」の 1 つだけ（候補の Skeleton は経過時間だけ。#416）。
  await expectSingleSpinner(page, page.getByRole("button", { name: "最新情報を取得", exact: true }));
  holdInitialRequests = false;
  initialGate.release();
  await navigation;

  const candidateList = page.getByTestId("qcm-candidate-list");
  const firstCandidate = page.getByTestId("qcm-training-candidate").filter({
    hasText: initialCandidate.question,
  });
  await expect(candidateList).toBeVisible();
  // 件数は一覧のツールバーの右の 1 か所（ListCount。#1891）。
  await expect(page.getByTestId("qcm-candidate-count")).toHaveText("全 51 件");
  await expect(page.getByTestId("qcm-candidate-pagination")).toContainText("1 / 3 ページ");
  const candidateBulkBar = page.getByTestId("qcm-candidate-bulk-actions");
  const candidateBulkActions = page.getByTestId("qcm-candidate-page-selection-actions");
  const addSelectedButton = page.getByRole("button", { name: "選択した 0 件を追加" });
  const [candidateBarBox, candidateActionsBox, addSelectedBox] = await Promise.all([
    candidateBulkBar.boundingBox(),
    candidateBulkActions.boundingBox(),
    addSelectedButton.boundingBox(),
  ]);
  expect(candidateBarBox).not.toBeNull();
  expect(candidateActionsBox).not.toBeNull();
  expect(addSelectedBox).not.toBeNull();
  expect(Math.abs(candidateActionsBox!.x - (candidateBarBox!.x + 12))).toBeLessThanOrEqual(1);
  if ((page.viewportSize()?.width ?? 0) >= 640) {
    expect(
      Math.abs(
        addSelectedBox!.x + addSelectedBox!.width - (candidateBarBox!.x + candidateBarBox!.width - 12)
      )
    ).toBeLessThanOrEqual(1);
  }
  await expect(candidateBulkActions.getByRole("button", { name: "表示中をすべて選択" })).toBeEnabled();
  await expect(candidateBulkActions.getByRole("button", { name: "表示中の選択をすべて解除" })).toBeDisabled();
  await candidateBulkActions.getByRole("button", { name: "表示中をすべて選択" }).click();
  await expect(page.getByRole("button", { name: "選択した 1 件を追加" })).toBeEnabled();
  await expect(candidateBulkActions.getByRole("button", { name: "表示中をすべて選択" })).toBeDisabled();
  await expect(candidateBulkActions.getByRole("button", { name: "表示中の選択をすべて解除" })).toBeEnabled();
  await candidateBulkActions.getByRole("button", { name: "表示中の選択をすべて解除" }).click();
  await expect(page.getByRole("button", { name: "選択した 0 件を追加" })).toBeDisabled();

  const candidateProfile = firstCandidate.getByRole("combobox", { name: "追加する業務プロファイル" });
  await expect(candidateProfile).toHaveText("既定プロファイル");
  await expect(candidateProfile).not.toContainText("default");
  await candidateProfile.focus();
  await page.keyboard.press("ArrowDown");
  await expect(candidateProfile).toHaveAttribute("aria-expanded", "true");
  // SelectField の一覧は body へ Portal で描くため、ページから引く（#352）。
  const profileListbox = page.getByRole("listbox", { name: "追加する業務プロファイル" });
  await expect(profileListbox.getByText("default", { exact: true })).toHaveCount(0);
  await expect(profileListbox.getByText("payment", { exact: true })).toHaveCount(0);
  await expect(profileListbox.getByText("請求カテゴリ", { exact: true })).toBeVisible();
  await expect(profileListbox.getByText("入金カテゴリ", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(candidateProfile).toHaveAttribute("aria-expanded", "false");
  await expect(candidateProfile).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await expect(candidateProfile).toHaveText("入金管理");
  await expect(candidateProfile).not.toContainText("payment");
  await expect(candidateProfile).toBeFocused();

  await page.getByLabel("履歴から再実行したい請求金額 を選択").check();
  await page.getByRole("button", { name: "選択した 1 件を追加" }).click();
  const failedAddDialog = page.getByRole("alertdialog", { name: "訓練データへ追加しますか" });
  await failedAddDialog.getByRole("button", { name: "選択した候補を追加" }).click();
  await expect(
    page.getByTestId("qcm-candidate-bulk-actions").getByText(
      "学習候補を追加できません。Profile を確認して再試行してください。"
    )
  ).toBeVisible();
  expect(failedImportPayload).toEqual({
    items: [{ history_id: "hist-001", profile_id: "payment" }],
  });

  const search = page.getByPlaceholder("質問・コメントで絞り込み");
  await search.fill("error");
  await search.press("Enter");
  const candidateError = page.getByRole("alert").filter({
    hasText: "学習候補を取得できません。接続状態を確認して再試行してください。",
  });
  await expect(candidateError).toBeVisible();
  await candidateError.getByRole("button", { name: "再試行" }).click();
  await expect(candidateList).toBeVisible();

  // 375px は検索・状態・業務プロファイルの 3 つをシート（「絞り込み」）にまとめる（#1933）。
  const candidateToolbar = page.getByTestId("qcm-candidate-filters");
  await withListToolbarFilters(candidateToolbar, async (filters) => {
    await filters.getByRole("combobox", { name: "状態", exact: true }).click();
    await page.getByRole("option", { name: "確認待ち", exact: true }).click();
    await filters.getByRole("combobox", { name: "業務プロファイル", exact: true }).click();
    await page.getByRole("option", { name: /入金管理/ }).click();
  });
  holdNoResultsRequest = true;
  await search.fill("一致なし");
  await search.press("Enter");
  const filteredSkeleton = page.getByTestId("qcm-candidates-list-skeleton");
  await expect(filteredSkeleton).toBeVisible();
  // 絞り込みは条件を変えたらすぐ適用する（「絞り込み」ボタンを置かない。#535）。読込のスピナーは Skeleton の 1 つだけ。
  await expect(page.locator("button:not([aria-haspopup=dialog])").filter({ hasText: /^絞り込み$/ })).toHaveCount(0); // 狭い画面のシートを開くボタン（#1933）は除く
  await expect(filteredSkeleton.locator("svg.animate-spin")).toHaveCount(1);
  noResultsGate.release();
  await expect(page.getByText("条件に一致する学習候補がありません", { exact: true })).toBeVisible();
  holdNoResultsRequest = false;
  const lastFilteredRequest = candidateRequests.at(-1);
  expect(lastFilteredRequest?.searchParams.get("q")).toBe("一致なし");
  expect(lastFilteredRequest?.searchParams.get("status")).toBe("pending");
  expect(lastFilteredRequest?.searchParams.get("profile_id")).toBe("payment");

  await page.getByRole("button", { name: "絞り込みを解除" }).click();
  await expect(search).toHaveValue("");
  await withListToolbarFilters(candidateToolbar, async (filters) => {
    await expect(filters.getByRole("combobox", { name: "状態", exact: true })).toContainText(/状態:\s*すべて/);
    await expect(filters.getByRole("combobox", { name: "業務プロファイル", exact: true })).toContainText(/業務プロファイル:\s*すべて/);
  });
  await expect(candidateList).toBeVisible();
  await page.getByTestId("qcm-candidate-pagination").getByRole("button", { name: "次へ" }).click();
  await expect(page.getByText(secondPageCandidate.question, { exact: true })).toBeVisible();
  await expect(page.getByTestId("qcm-candidate-pagination")).toContainText("26–26 / 51 件");
  await page.getByTestId("qcm-candidate-pagination").getByRole("button", { name: "前へ" }).click();
  await expect(page.getByText(initialCandidate.question, { exact: true })).toBeVisible();

  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
  const mobileCandidate = page.getByTestId("qcm-training-candidate").first();
  const mobileProfile = mobileCandidate.getByRole("combobox", { name: "追加する業務プロファイル" });
  await mobileProfile.click();
  await expectHorizontallyContained(page.getByRole("listbox", { name: "追加する業務プロファイル" }), mobileCandidate);
  await page.keyboard.press("Escape");
  await expect(mobileCandidate.getByRole("link", { name: "フィードバック管理で確認" })).toBeVisible();
  await expectNoHorizontalScroll(page);

  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.evaluate(() => document.documentElement.classList.add("dark"));
  await expect(candidateList).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test("question classifier model management handles untrained, empty, and load error states", async ({ page }) => {
  const api = await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/classifier");
  await page.route("**/api/nl2sql/classifier", (route) =>
    fulfillJson(route, {
      ready: false,
      trained: false,
      classifier_version: "",
      updated_at: "",
      example_count: 0,
      category_count: 0,
      categories: [],
      embedding_model: "deterministic-hash-1536",
      vector_dimension: 1536,
      persistence_mode: "memory",
      recommendation_source: "deterministic",
      metrics: {},
      warnings: ["LogisticRegression classifier は未学習です。"],
    })
  );
  await page.unroute("**/api/nl2sql/classifier/training-data");
  await page.route("**/api/nl2sql/classifier/training-data", (route) =>
    fulfillJson(route, {
      total_examples: 0,
      categories: [],
      warnings: ["分類器の training data が未登録です。"],
      examples: [],
    })
  );

  await page.goto("/question-classifier-models");
  const classifierStatus = page.getByTestId("qcm-model-status");
  await expect(classifierStatus.locator('> [aria-hidden="true"]')).toHaveText("未学習");
  await expect(page.getByText("訓練データは未登録です")).toBeVisible();
  await page.getByRole("tab", { name: "モデルテスト" }).click();
  await expect(page.getByRole("button", { name: "分類を試す" })).toBeDisabled();
  expect(api.classifierModelListRequests).toBe(0);

  await page.unroute("**/api/nl2sql/classifier");
  await page.route("**/api/nl2sql/classifier", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: ["分類モデル状態を取得できません。接続を確認して再試行してください。"],
      }),
    })
  );
  await page.goto("/question-classifier-models");
  await expect(
    page.getByText("分類モデル状態を取得できません。接続を確認して再試行してください。")
  ).toBeVisible();
  await expect(
    page.getByRole("alert").getByRole("button", { name: "再試行" })
  ).toBeVisible();
});

test("glossary page manages global terms only", async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.goto("/glossary-rules");
  await expect(page.getByRole("heading", { level: 1, name: "用語・同義語" })).toBeVisible();
  await expect(page.getByLabel("用語・同義語管理ステータス")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "最新情報を取得", exact: true })).toBeVisible();
  await expect(page.getByRole("tab")).toHaveCount(0);
  await expect(page.getByRole("heading", { level: 2, name: "用語・同義語", exact: true })).toBeVisible();
  // 件数は表の上のツールバーの ListCount 1 か所（見出しのバッジに出さない。#1891）。
  await expect(page.getByTestId("glossary-terms-toolbar").getByRole("status")).toHaveText("全 51 件");
  await expect(page.getByText("用語・同義語 51", { exact: true })).toHaveCount(0);
  await expect(page.getByText(/取込は既存データを全置換します/)).toBeVisible();
  await expect(page.locator("main").getByText(/グローバル用語(?:集)?/)).toHaveCount(0);
  await expect(page.locator("main").getByText(/語彙/)).toHaveCount(0);
  // 「グローバルルール」は独立ナビ/専用ページ(/global-rules)として存在するため、
  // 用語・同義語ページ本文(main)に混在しないことのみ検証(サイドバーのナビ項目は除外)。
  await expect(page.locator("main").getByText("グローバルルール")).toHaveCount(0);
  await expect(page.getByTestId("glossary-terms-preview").getByRole("columnheader", { name: "No." })).toBeVisible();
  await expect(page.getByTestId("glossary-terms-row-number").first()).toHaveText("1");
  await expect(page.getByTestId("glossary-terms-preview").getByRole("cell", { name: "売上" })).toBeVisible();
  await expect(page.getByTestId("glossary-term-preview-cell").first().locator("xpath=ancestor::td[1]")).toHaveCSS("vertical-align", "middle");
  await expect(page.getByTestId("glossary-terms-preview").getByRole("cell", { name: "INVOICES.TOTAL_AMOUNT" })).toBeVisible();
  await expect(page.getByTestId("glossary-terms-preview").getByRole("cell", { name: "用語26" })).toHaveCount(0);
  await expect(page.getByTestId("glossary-terms-pagination")).toContainText("1–25 / 51 件");
  await expect(page.getByLabel("用語・同義語データのページ切替")).toBeVisible();
  await page.getByTestId("glossary-terms-pagination").getByRole("button", { name: "次へ" }).click();
  await expect(page.getByTestId("glossary-terms-row-number").first()).toHaveText("26");
  await expect(page.getByTestId("glossary-terms-preview").getByRole("cell", { name: "用語26" })).toBeVisible();

  await page.getByRole("button", { name: "最新情報を取得", exact: true }).click();
  await expect(page.getByText("サーバーの最新内容を読み込みました。")).toBeVisible();
  // ページ番号は作業状態に残すので、再読み込みしても同じページのまま（#1891）。
  await expect(page.getByTestId("glossary-terms-pagination")).toContainText("2 / 3 ページ");
  await expect(page.getByTestId("glossary-rules-panel-heading-file-input")).toHaveAttribute(
    "accept",
    ".xlsx"
  );
  await expect(page.getByText(".XLSX", { exact: true })).toBeVisible();

  await dropFiles(page, page.getByTestId("glossary-rules-panel-heading-file-dropzone"), [
    {
      name: "terms.csv",
      type: "text/csv",
      content: "TERM,DEFINITION\n粗利,INVOICES.PROFIT\n",
    },
  ]);
  await expect(page.getByText(".XLSX ファイルを選択してください")).toBeVisible();
  await dropFiles(page, page.getByTestId("glossary-rules-panel-heading-file-dropzone"), [
    {
      name: "terms.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock",
    },
  ]);
  // 取込は登録済みの 51 件を置き換えるので、件数とファイル名を示して確認する（#953）。
  await expect(page.getByRole("alertdialog")).toContainText("登録済みの用語・同義語 51 件を、terms.xlsx の内容ですべて置き換えます。");
  await confirmMaterialReplace(page);
  await expect(page.getByText("用語・同義語を 1 件取り込みました。")).toBeVisible();
  await expect(page.getByTestId("glossary-terms-preview").getByRole("cell", { name: "粗利" })).toBeVisible();
  await expect(page.getByTestId("glossary-terms-preview").getByRole("cell", { name: "INVOICES.PROFIT" })).toBeVisible();
  await expect(page.getByLabel("グローバルルール Excel 取込")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "グローバルルール Excel 出力" })).toHaveCount(0);

  const termsDownloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "用語・同義語 Excel 出力" }).click();
  const termsDownload = await termsDownloadPromise;
  expect(termsDownload.suggestedFilename()).toBe("terms.xlsx");

  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await expectNoHorizontalScroll(page);
});

test("glossary previews preserve Excel cell newlines and wrap long content", async ({ page }) => {
  await mockNl2SqlApi(page);
  const multilineDefinition = [
    "INVOICES.TOTAL_AMOUNT",
    "",
    "    AS 売上金額",
    ...Array.from({ length: 16 }, (_, index) => `    detail_${index + 1}`),
  ].join("\n");
  await page.route("**/api/nl2sql/legacy-learning-material", (route) =>
    fulfillJson(route, {
      glossary: { 売上: multilineDefinition },
      rules: [],
    })
  );

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/glossary-rules");

  const definitionText = page.getByTestId("glossary-definition-preview-text").first();
  await expect(definitionText).toHaveCSS("white-space", "pre-wrap");
  await expect(definitionText).toHaveCSS("overflow-y", "auto");
  expect(await definitionText.textContent()).toBe(multilineDefinition);
  const definitionMetrics = await definitionText.evaluate((element) => {
    const style = getComputedStyle(element);
    const lineHeight = Number.parseFloat(style.lineHeight);
    return {
      height: element.getBoundingClientRect().height,
      lineHeight,
      maxHeight: Number.parseFloat(style.maxHeight),
      scrollHeight: element.scrollHeight,
    };
  });
  expect(definitionMetrics.height).toBeLessThanOrEqual(definitionMetrics.lineHeight * 10 + 1);
  expect(definitionMetrics.maxHeight).toBeCloseTo(definitionMetrics.lineHeight * 10, 1);
  expect(definitionMetrics.scrollHeight).toBeGreaterThan(definitionMetrics.height);
  await expectNoHorizontalScroll(page);

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(definitionText).toHaveCSS("overflow-wrap", "anywhere");
  expect(await definitionText.textContent()).toBe(multilineDefinition);
  await expectNoHorizontalScroll(page);
});

test("glossary global data shows empty, loading, and server error states", async ({ page }) => {
  await mockNl2SqlApi(page);
  let requestCount = 0;
  let releaseReload: () => void = () => undefined;
  let markReloadStarted: () => void = () => undefined;
  const reloadStarted = new Promise<void>((resolve) => {
    markReloadStarted = resolve;
  });
  await page.route("**/api/nl2sql/legacy-learning-material", async (route) => {
    requestCount += 1;
    if (requestCount === 1) {
      return fulfillJson(route, { glossary: {}, rules: [] });
    }
    markReloadStarted();
    await new Promise<void>((resolve) => {
      releaseReload = resolve;
    });
    return route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "サーバー読込エラー" }),
    });
  });

  await page.goto("/glossary-rules");
  await expect(page.getByText("データがありません。")).toBeVisible();

  const reloadButton = page.getByRole("button", { name: "最新情報を取得" });
  const reloadClick = reloadButton.click();
  await reloadStarted;
  await expect(reloadButton).toBeDisabled();
  await expect(page.getByText("データがありません。")).toBeVisible();
  await expect(page.getByTestId("glossary-terms-list-skeleton")).toHaveCount(0);
  releaseReload();
  await reloadClick;
  await expect(page.getByRole("alert")).toContainText("サーバー読込エラー");
  await expectNoHorizontalScroll(page);
});

test("glossary and global rules do not show an empty list when the first load fails", async ({ page }) => {
  // #953: 取得の失敗を「データがありません」（空）と取り違えさせず、取込（全置換）は件数が分からない旨を示して確認する。
  await mockNl2SqlApi(page);
  let fail = true;
  let imports = 0;
  await page.route("**/api/nl2sql/legacy-learning-material", (route) => {
    if (fail) {
      return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "読込エラー" }) });
    }
    return fulfillJson(route, { glossary: { 売上: "INVOICES.TOTAL_AMOUNT" }, rules: ["SELECT のみ"] });
  });
  page.on("request", (request) => {
    if (/\/legacy-learning-material\/(terms|rules)\/import$/u.test(new URL(request.url()).pathname)) imports += 1;
  });

  for (const material of [
    { path: "/glossary-rules", unavailable: "glossary-terms-unavailable", title: "用語・同義語を表示できません", empty: "データがありません。", count: /^全 \d+ 件$/u, dropzone: "glossary-rules-panel-heading-file-dropzone", preview: "glossary-terms-preview", previewText: "売上" },
    { path: "/global-rules", unavailable: "global-rules-unavailable", title: "共通ルールを表示できません", empty: "共通ルールがありません。", count: /^全 \d+ 件$/u, dropzone: "global-rules-file-dropzone", preview: "global-rules-preview", previewText: "SELECT のみ" },
  ]) {
    fail = true;
    await page.goto(material.path);
    await expect(page.getByRole("alert")).toContainText("読込エラー");
    await expect(page.getByTestId(material.unavailable)).toContainText(material.title);
    await expect(page.getByText(material.empty, { exact: true })).toHaveCount(0);
    await expect(page.getByText(material.count)).toHaveCount(0);
    await expectNoHorizontalScroll(page);

    await dropFiles(page, page.getByTestId(material.dropzone), [
      { name: "replace.xlsx", type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", content: "mock" },
    ]);
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("今の登録内容を取得できていないため、件数を確認できません。");
    await dialog.getByRole("button", { name: "キャンセル", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(imports).toBe(0);

    fail = false;
    await page.getByRole("button", { name: "最新情報を取得", exact: true }).click();
    await expect(page.getByTestId(material.preview)).toContainText(material.previewText);
    await expect(page.getByTestId(material.unavailable)).toHaveCount(0);
  }
});

test("glossary and global rules imports use stable errors for non JSON responses", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/legacy-learning-material/terms/import");
  await page.route("**/api/nl2sql/legacy-learning-material/terms/import", (route) =>
    route.fulfill({
      status: 413,
      contentType: "text/plain",
      body: "payload too large",
    })
  );

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/glossary-rules");
  await dropFiles(page, page.getByTestId("glossary-rules-panel-heading-file-dropzone"), [
    {
      name: "terms.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock",
    },
  ]);
  await confirmMaterialReplace(page);
  const glossaryAlert = page.getByRole("alert");
  await expect(glossaryAlert).toContainText("用語・同義語データの取込に失敗しました。");
  await expect(glossaryAlert).not.toContainText("Unexpected");
  await expectNoHorizontalScroll(page);

  await page.unroute("**/api/nl2sql/legacy-learning-material/rules/import");
  await page.route("**/api/nl2sql/legacy-learning-material/rules/import", (route) =>
    route.fulfill({
      status: 500,
      contentType: "text/plain",
      body: "server error",
    })
  );

  await page.goto("/global-rules");
  await dropFiles(page, page.getByTestId("global-rules-file-dropzone"), [
    {
      name: "rules.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock rules",
    },
  ]);
  await confirmMaterialReplace(page);
  const rulesAlert = page.getByRole("alert");
  await expect(rulesAlert).toContainText("共通ルールの取込に失敗しました。");
  await expect(rulesAlert).not.toContainText("Unexpected");
  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
});

test("data preparation read results use the shared detail skeleton without stale or empty overlap", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });

  const objectsGate = createRequestGate();
  await page.route("**/api/nl2sql/db-admin/objects?*", async (route) => {
    await objectsGate.promise;
    return fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items: [
        { name: "INVOICES", owner: "APP", object_type: "table", row_count: 2, comment: "請求情報" },
        { name: "V_EMP_DEPT", owner: "APP", object_type: "view", row_count: null, comment: "社員と部署" },
      ],
      total: 2,
      table_count: 1,
      view_count: 1,
      next_cursor: null,
      refreshed_at: schemaCatalog.refreshed_at,
      catalog_version: 1,
      warnings: [],
    });
  });

  const firstPreviewGate = createRequestGate();
  const failedPreviewGate = createRequestGate();
  let previewAttempts = 0;
  const previewResponse = {
    runtime: "deterministic",
    sql: 'SELECT * FROM "INVOICES" FETCH FIRST 10 ROWS ONLY',
    results: {
      columns: ["CUSTOMER_NAME", "TOTAL_AMOUNT"],
      rows: [{ CUSTOMER_NAME: "スケルトン確認顧客", TOTAL_AMOUNT: 1200000 }],
      total: 1,
    },
    warnings: [],
  };
  await page.route("**/api/nl2sql/db-admin/preview-data", async (route) => {
    previewAttempts += 1;
    if (previewAttempts === 1) {
      await firstPreviewGate.promise;
      return fulfillJson(route, previewResponse);
    }
    if (previewAttempts === 3) {
      await failedPreviewGate.promise;
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "表示結果の取得に失敗しました" }),
      });
    }
    return fulfillJson(route, previewResponse);
  });

  await page.goto("/data-management");
  await expect(page.getByTestId("data-management-preview-list-skeleton")).toBeVisible();
  await expect(page.getByTestId("data-management-preview-grid")).toHaveCount(0);
  await expect(page.getByText("対象テーブル/ビューがありません")).toHaveCount(0);
  objectsGate.release();
  await expect(page.getByTestId("data-management-preview-grid")).toBeVisible();

  // データの表示は一覧 + 詳細（#2315）。詳細の列に対象の見出し・取得件数上限・表示結果を出す。
  const resultsPanel = page.getByTestId("data-preview-detail");
  const invoicesSelectButton = page.getByRole("button", { name: "APP.INVOICES を選択" });
  const viewSelectButton = page.getByRole("button", { name: "APP.V_EMP_DEPT を選択" });
  const showPreviewButton = resultsPanel.getByRole("button", { name: "データを表示", exact: true });
  const resultsActions = resultsPanel.getByTestId("data-preview-results-actions");
  // 行を選ぶと、その対象のデータを今の取得件数上限で表示する。
  await invoicesSelectButton.click();
  const dataSkeleton = page.getByTestId("data-preview-results-detail-skeleton");
  await expect(dataSkeleton).toBeVisible();
  await expect(showPreviewButton).toBeDisabled();
  // 行の選択で始めた表示のスピナーは結果の領域の 1 つだけ（ボタンは回さない。#416・#819）。
  await expectSingleSpinner(page, dataSkeleton);
  await expect(invoicesSelectButton).toBeEnabled();
  await expect(viewSelectButton).toBeEnabled();
  await expect(resultsActions.getByRole("button", { name: "XLSX ダウンロード" })).toBeDisabled();
  await expect(resultsActions.getByRole("button", { name: "その他の操作" })).toBeVisible();
  await resultsActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "APP.INVOICES のデータを空にする" })).toBeDisabled();
  await resultsActions.getByRole("button", { name: "その他の操作" }).click();
  await expect(page.getByTestId("data-management-preview-grid").getByRole("button", { name: /^操作: / })).toHaveCount(0);
  await expect(resultsPanel.getByRole("button", { name: "APP.V_EMP_DEPT のデータを空にする" })).toHaveCount(0);
  await expect(resultsPanel.getByText("データ未表示")).toHaveCount(0);
  await expect(resultsPanel.getByText("スケルトン確認顧客")).toHaveCount(0);

  const dataSkeletonShape = await dataSkeleton
    .getByTestId("db-management-skeleton-block")
    .evaluateAll((elements) =>
      elements.map((element) => {
        const style = window.getComputedStyle(element);
        return {
          height: Math.round(Number.parseFloat(style.height)),
          backgroundColor: style.backgroundColor,
          borderRadius: style.borderRadius,
        };
      })
    );
  expect(dataSkeletonShape.map((block) => block.height)).toEqual([63, 40, 287]);
  await expect(dataSkeleton.getByTestId("db-management-skeleton-block").first()).toHaveCSS(
    "animation-name",
    "none"
  );
  expect(
    await dataSkeleton
      .getByTestId("db-management-skeleton-block")
      .evaluateAll((elements) => elements.every((element) => !element.hasAttribute("tabindex")))
  ).toBe(true);

  firstPreviewGate.release();
  await expect(resultsPanel.getByRole("cell", { name: "スケルトン確認顧客" })).toBeVisible();

  await showPreviewButton.click();
  await expect(resultsPanel.getByRole("cell", { name: "スケルトン確認顧客" })).toBeVisible();

  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
  await showPreviewButton.click();
  await expect(dataSkeleton).toBeVisible();
  await expect(resultsPanel.getByRole("cell", { name: "スケルトン確認顧客" })).toHaveCount(0);
  failedPreviewGate.release();
  await expect(resultsPanel.getByRole("alert")).toContainText("表示結果の取得に失敗しました");
  await expect(dataSkeleton).toHaveCount(0);
  await resultsPanel.getByRole("button", { name: "再試行" }).click();
  await expect(resultsPanel.getByRole("cell", { name: "スケルトン確認顧客" })).toBeVisible();

  await page.setViewportSize({ width: 1280, height: 900 });
  const tableDetailGate = createRequestGate();
  await page.route("**/api/nl2sql/db-admin/tables/INVOICES*", async (route) => {
    await tableDetailGate.promise;
    return fulfillJson(route, {
      name: "INVOICES",
      owner: "APP",
      object_type: "table",
      row_count: 2,
      comment: "請求情報",
      columns: schemaCatalog.tables[0].columns,
      ddl: 'CREATE TABLE "INVOICES" ("CUSTOMER_NAME" VARCHAR2(120), "TOTAL_AMOUNT" NUMBER)',
      warnings: [],
    });
  });
  await page.goto("/table-management");
  await expect(page.getByTestId("table-management-grid")).toBeVisible();
  await page.getByRole("button", { name: "APP.INVOICES を表示" }).click();
  const tableSkeleton = page.getByTestId("table-management-detail-skeleton");
  await expect(tableSkeleton).toBeVisible();
  const tableSkeletonShape = await tableSkeleton
    .getByTestId("db-management-skeleton-block")
    .evaluateAll((elements) =>
      elements.map((element) => {
        const style = window.getComputedStyle(element);
        return {
          height: Math.round(Number.parseFloat(style.height)),
          backgroundColor: style.backgroundColor,
          borderRadius: style.borderRadius,
        };
      })
    );
  expect(tableSkeletonShape).toEqual(dataSkeletonShape);
  tableDetailGate.release();
  await expect(page.getByText('CREATE TABLE "INVOICES"')).toHaveCount(0);
});

for (const pageId of ["comment-management", "annotation-management", "domain-management"]) {
  test(`${pageId} の取得・SQL 生成ボタンは共通の主操作サイズでキーボード実行できる`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await mockNl2SqlApi(page);
    await page.goto(`/${pageId}`);
    // ヘッダーの操作群は説明文の長さに関わらずタイトルと同じ行(右上)に並ぶ(#672)。1280 / 1920 の両幅で確認する。
    if (testInfo.project.name === "desktop") {
      for (const width of [1280, 1920]) {
        await page.setViewportSize({ width, height: 900 });
        const headingBox = await page.getByRole("heading", { level: 1 }).first().boundingBox();
        const schemaRefreshBox = await page
          .getByRole("button", { name: "DB 構造を再取得", exact: true })
          .boundingBox();
        if (!headingBox || !schemaRefreshBox) throw new Error("header layout not measurable");
        expect(schemaRefreshBox.y, `width=${width}`).toBeLessThan(headingBox.y + headingBox.height);
        expect(schemaRefreshBox.x, `width=${width}`).toBeGreaterThan(headingBox.x + headingBox.width);
      }
      await page.setViewportSize({ width: 1280, height: 900 });
    }
    const fetchButton = page.getByRole("button", { name: "情報を取得", exact: true });
    await expectLargeActionButton(fetchButton);
    await expect(fetchButton).toBeDisabled();
    await page.getByRole("option", { name: /INVOICES/ }).check();
    await expect(fetchButton).toBeEnabled();
    await fetchButton.focus();
    await expect(fetchButton).toBeFocused();
    await fetchButton.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`${pageId}-fetch.png`) });
    await fetchButton.press("Enter");
    const inputPanel = page.locator(`#${pageId}-panel-input`);
    await expect(inputPanel.getByLabel("構造情報")).toHaveValue(/APP\.INVOICES/);
    if (pageId === "domain-management") {
      // ドメイン管理だけ: 操作種別と既存ドメイン(定義・関連付け先)を入力確認で見せる。
      // 包み込み label は option 文言も含むため、accessible name(role)で特定する。
      const operation = inputPanel.getByRole("combobox", { name: "操作", exact: true });
      await expectSelectFieldValue(operation, "create");
      await expect(inputPanel.getByRole("textbox", { name: "既存ドメイン", exact: true })).toHaveValue(/DOMAIN: APP\.TOTAL_AMOUNT_D/);
      await chooseSelectFieldOption(operation, "rebuild");
      await expectSelectFieldValue(operation, "rebuild");
    } else {
      await expect(inputPanel.getByRole("combobox", { name: "操作", exact: true })).toHaveCount(0);
    }
    const generateButton = inputPanel.getByRole("button", { name: "SQL 生成", exact: true });
    await expectLargeActionButton(generateButton);
    await generateButton.focus();
    await expect(generateButton).toBeFocused();
    await generateButton.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`${pageId}-generate.png`) });
    await generateButton.press("Enter");
    const executePanel = page.locator(`#${pageId}-panel-execute`);
    await expect(executePanel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue(/COMMENT ON|ALTER TABLE|CREATE DOMAIN/);
    await expectLargeActionButton(executePanel.getByRole("button", { name: "SQL 実行", exact: true }));
    await expectNoHorizontalScroll(page);
  });

  test(`${pageId} は再読込後に選択対象の詳細を取り直し、読込中のまま止まらない`, async ({ page }) => {
    // 開発サーバー(StrictMode)で effect が二重実行されても、再活性化時の 1 回の応答で loading が解除される(#675)。
    await mockNl2SqlApi(page);
    await page.goto(`/${pageId}`);
    await page.getByRole("option", { name: /INVOICES/ }).check();
    await page.getByRole("button", { name: "情報を取得", exact: true }).click();
    const inputPanel = page.locator(`#${pageId}-panel-input`);
    await expect(inputPanel.getByLabel("構造情報")).toHaveValue(/APP\.INVOICES/);

    await page.reload();
    await expect(page.getByRole("tab", { name: "入力確認・SQL生成", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(inputPanel.getByLabel("構造情報")).toHaveValue(/APP\.INVOICES/, { timeout: 15_000 });
    await expect(page.getByTestId(`${pageId}-input-detail-skeleton`)).toHaveCount(0);
    await expect(page.getByText("選択対象は未確認です")).toHaveCount(0);
    await expect(inputPanel.getByRole("button", { name: "SQL 生成", exact: true })).toBeEnabled();
  });

  test(`${pageId} はヘッダーの直下に選択対象の状態の Banner を出さず、未取得の間は実行の理由をボタンの直上に出す (#2314)`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await mockNl2SqlApi(page);
    await page.goto(`/${pageId}`);
    const expectNoTargetBanner = async () => {
      await expect(page.getByText("選択対象は未確認です")).toHaveCount(0);
      await expect(page.getByText("参照情報は取得時点の内容です")).toHaveCount(0);
      await expect(page.getByRole("button", { name: "選択対象の最新情報を取得" })).toHaveCount(0);
    };

    // 対象を選んだだけ（詳細は未取得）で「SQL実行」へ移っても Banner は出さない。実行は押せず、理由は確認語の欄の説明に出す。
    await page.getByRole("option", { name: /INVOICES/ }).check();
    await page.getByRole("tab", { name: "SQL実行", exact: true }).click();
    const executePanel = page.locator(`#${pageId}-panel-execute`);
    await expectNoTargetBanner();
    await executePanel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)").fill("COMMENT ON TABLE APP.INVOICES IS 'x'");
    await executePanel.getByPlaceholder("ADMIN_EXECUTE").fill("ADMIN_EXECUTE");
    await expect(executePanel.getByText("対象情報を取得し、ADMIN_EXECUTE を入力すると実行できます。")).toBeVisible();
    await expect(executePanel.getByRole("button", { name: "SQL 実行", exact: true })).toBeDisabled();
    await page.screenshot({ path: testInfo.outputPath(`${pageId}-unverified-no-banner.png`) });

    // 詳細を取得した後も Banner と取得時刻の表示は出さない。説明は確認語の案内に戻る。
    await page.getByRole("tab", { name: "対象選択", exact: true }).click();
    await page.getByRole("button", { name: "情報を取得", exact: true }).click();
    const inputPanel = page.locator(`#${pageId}-panel-input`);
    await expect(inputPanel.getByLabel("構造情報")).toHaveValue(/APP\.INVOICES/);
    await expectNoTargetBanner();
    await page.getByRole("tab", { name: "SQL実行", exact: true }).click();
    await expectNoTargetBanner();
    await expect(executePanel.getByText("ADMIN_EXECUTE を入力すると実行できます。", { exact: true })).toBeVisible();
    await expect(executePanel.getByText("対象情報を取得し、ADMIN_EXECUTE を入力すると実行できます。")).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`${pageId}-fetched-no-banner.png`) });
    await expectNoHorizontalScroll(page);
  });
}

test("JOIN WHERE and metadata read result branches replace their result areas with shared skeletons", { tag: "@responsive" }, async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.setViewportSize({ width: 1280, height: 900 });

  const commentDetailGate = createRequestGate();
  await page.route("**/api/nl2sql/db-admin/tables/INVOICES*", async (route) => {
    await commentDetailGate.promise;
    return fulfillJson(route, {
      name: "INVOICES",
      owner: "APP",
      object_type: "table",
      row_count: 2,
      comment: "請求情報",
      columns: schemaCatalog.tables[0].columns,
      ddl: 'CREATE TABLE "INVOICES" ("CUSTOMER_NAME" VARCHAR2(120), "TOTAL_AMOUNT" NUMBER)',
      warnings: [],
    });
  });

  await page.goto("/comment-management");
  const commentTargetToolbar = page.getByTestId("comment-management-target-toolbar");
  await expect(commentTargetToolbar).toBeVisible();
  await expect(page.getByTestId("comment-management-target-footer")).toContainText("選択 0 件");
  await commentTargetToolbar.getByRole("searchbox", { name: "検索" }).fill("INVO");
  await expect(page.getByRole("option", { name: /INVOICES/ })).toBeVisible();
  await expect(page.getByTestId("comment-management-target-footer")).toContainText("1 / 1 件を表示");
  // 一括選択は ListPicker の選択の行（#608）。
  const commentBulkActions = page.getByTestId("comment-management-target-bulk-actions");
  const commentTargetList = page.getByTestId("comment-management-target-scroll-region");
  await expect
    .poll(async () => {
      const [actionsBox, listBox] = await Promise.all([
        commentBulkActions.boundingBox(),
        commentTargetList.boundingBox(),
      ]);
      if (!actionsBox || !listBox) return Number.POSITIVE_INFINITY;
      return actionsBox.x - listBox.x;
    })
    .toBeGreaterThanOrEqual(0);
  await expect(commentBulkActions.getByRole("button", { name: "表示中をすべて選択" })).toBeEnabled();
  await expect(commentBulkActions.getByRole("button", { name: "選択をすべて解除" })).toBeDisabled();
  await commentBulkActions.getByRole("button", { name: "表示中をすべて選択" }).click();
  await expect(page.getByTestId("comment-management-target-footer")).toContainText("選択 1 件");
  await expect(commentBulkActions.getByRole("button", { name: "表示中をすべて選択" })).toBeDisabled();
  await expect(commentBulkActions.getByRole("button", { name: "選択をすべて解除" })).toBeEnabled();
  await commentBulkActions.getByRole("button", { name: "選択をすべて解除" }).click();
  await expect(page.getByTestId("comment-management-target-footer")).toContainText("選択 0 件");
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await expect(page.getByTestId("comment-management-target-footer")).toContainText("選択 1 件");
  const commentFetchButton = page.getByRole("button", { name: "情報を取得", exact: true });
  await expectButtonBelowInput(page.getByTestId("comment-management-target-footer"), commentFetchButton);
  await commentFetchButton.click();
  const commentInputSkeleton = page.getByTestId("comment-management-input-detail-skeleton");
  await expect(page.getByRole("tab", { name: "入力確認・SQL生成", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(commentInputSkeleton).toBeVisible();
  await expect(commentInputSkeleton.locator("svg.animate-spin")).toHaveCount(0);
  await expect(page.locator("#comment-management-panel-input").getByText("対象情報が未取得です")).toHaveCount(0);
  commentDetailGate.release();
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.INVOICES/);
  await expect(page.getByRole("region", { name: "通知" })).toContainText("対象情報を取得しました。1 件を確認できます。");
  const commentInputPanel = page.locator("#comment-management-panel-input");
  await expect(commentInputPanel).toBeVisible();

  const commentGenerateGate = createRequestGate();
  await page.route("**/api/nl2sql/metadata-samples", async (route) => {
    await commentGenerateGate.promise;
    return fulfillJson(route, {
      sample_text: "OBJECT: APP.INVOICES\nCUSTOMER_NAME: 架空商事",
      sample_count: 1,
      runtime: "oracle",
      warnings: [],
    });
  });
  const commentGenerateButton = commentInputPanel.getByRole("button", { name: "SQL 生成" });
  await expectButtonBelowInput(commentInputPanel.getByLabel("追加入力"), commentGenerateButton);
  await commentGenerateButton.click();
  const commentExecuteSkeleton = page.getByTestId("comment-management-execute-result-detail-skeleton");
  await expect(page.getByRole("tab", { name: "SQL実行", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(commentExecuteSkeleton).toBeVisible();
  await expect(commentExecuteSkeleton.locator("svg.animate-spin")).toHaveCount(0);
  await expect(page.locator("#comment-management-panel-execute").getByText("生成済み SQL がありません")).toHaveCount(0);
  commentGenerateGate.release();
  await expect(page.locator("#comment-management-panel-execute").getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue(/COMMENT ON COLUMN/);
  await expect(page.getByRole("region", { name: "通知" })).toContainText("SQL 生成が完了しました。");

  await page.goto("/annotation-management");
  await page.getByRole("option", { name: /INVOICES/ }).check();
  const annotationFetchButton = page.getByRole("button", { name: "情報を取得", exact: true });
  await expectButtonBelowInput(page.getByTestId("annotation-management-target-footer"), annotationFetchButton);
  await annotationFetchButton.click();
  await expect(page.getByRole("tab", { name: "入力確認・SQL生成", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.INVOICES/);
  await expect(page.getByRole("region", { name: "通知" })).toContainText("対象情報を取得しました。1 件を確認できます。");
  const annotationInputPanel = page.locator("#annotation-management-panel-input");
  await expect(annotationInputPanel).toBeVisible();
  const annotationGenerateGate = createRequestGate();
  await page.route("**/api/nl2sql/annotations/generate-sql", async (route) => {
    await annotationGenerateGate.promise;
    return fulfillJson(route, {
      sql: "ALTER TABLE \"APP\".\"INVOICES\" MODIFY (\"TOTAL_AMOUNT\" ANNOTATIONS (ADD IF NOT EXISTS UI_Display '税込請求金額'));",
      source: "deterministic",
      warnings: [],
      timing,
    });
  });
  const annotationGenerateButton = annotationInputPanel.getByRole("button", { name: "SQL 生成" });
  await expectButtonBelowInput(annotationInputPanel.getByLabel("追加入力"), annotationGenerateButton);
  await annotationGenerateButton.click();
  const annotationExecuteSkeleton = page.getByTestId("annotation-management-execute-result-detail-skeleton");
  await expect(page.getByRole("tab", { name: "SQL実行", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(annotationExecuteSkeleton).toBeVisible();
  await expect(annotationExecuteSkeleton.locator("svg.animate-spin")).toHaveCount(0);
  annotationGenerateGate.release();
  await expect(page.locator("#annotation-management-panel-execute").getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue(/ALTER TABLE/);
  await expect(page.getByRole("region", { name: "通知" })).toContainText("SQL 生成が完了しました。");

  await page.goto("/view-management");
  await expect(page.getByTestId("view-management-grid")).toBeVisible();
  await page.getByRole("button", { name: "APP.V_EMP_DEPT を表示" }).click();
  await expect(page.getByRole("button", { name: /XLSX ダウンロード/ })).toBeVisible();
  const viewColumnsDownloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: /XLSX ダウンロード/ }).click();
  const viewColumnsDownload = await viewColumnsDownloadPromise;
  expect(viewColumnsDownload.suggestedFilename()).toBe("app_v_emp_dept_columns.xlsx");
  const autoDdlResponse = page.waitForResponse((response) =>
    response.url().includes("/api/nl2sql/db-admin/views/V_EMP_DEPT?include_ddl=1") && response.ok()
  );
  await clickPageHeaderAction(page, "view-management-actions", "JOIN/WHERE 条件抽出");
  await autoDdlResponse;
  await expect(page.getByTestId("view-join-where-actions")).toContainText("DDL 取得済み");
  let joinWhereGate = createRequestGate();
  await page.route("**/api/nl2sql/db-admin/extract-join-where", async (route) => {
    await joinWhereGate.promise;
    return fulfillJson(route, {
      join_text:
        "JOIN: EMPLOYEE(e) JOIN DEPARTMENT(d)\nON: EMPLOYEE(e).DEPARTMENT_ID = DEPARTMENT(d).DEPARTMENT_ID",
      where_text: "EMPLOYEE(e).STATUS = 'A'",
      source: "deterministic",
      warnings: [],
      prompt_profile: "sql_structure",
      structure_markdown:
        "## SQL構造分析\n\n### JOIN句\n- JOIN: EMPLOYEE(e) JOIN DEPARTMENT(d)\n\n### WHERE句\n- EMPLOYEE(e).STATUS = 'A'",
    });
  });
  const joinWhereButton = page.getByRole("button", { name: "AI で抽出" });
  await expectLargeActionButton(joinWhereButton);
  await expectButtonBelowInput(page.getByTestId("view-join-where-advanced-settings"), joinWhereButton);
  await joinWhereButton.click();
  const joinWhereSkeleton = page.getByTestId("view-join-where-result-detail-skeleton");
  await expect(joinWhereSkeleton).toBeVisible();
  await expect(joinWhereButton.locator("svg.animate-spin")).toHaveCount(1);
  await expectLargeActionButton(joinWhereButton);
  await expect(joinWhereSkeleton.locator("svg.animate-spin")).toHaveCount(0);
  await expect(page.getByLabel("結合条件 (JOIN)")).toHaveCount(0);
  joinWhereGate.release();
  await expect(page.getByLabel("結合条件 (JOIN)")).toHaveValue(/EMPLOYEE/);
  await page.setViewportSize({ width: 375, height: 900 });
  // 共有 Button の 44px 化はタッチ端末(pointer: coarse)だけで、幅 375px でもマウス環境は lg=40px のまま。
  // expectLargeActionButton は旧仕様の max-width: 639px も 44px 扱いするため、ここでは DS 仕様で直接測る。
  await expect(joinWhereButton).toBeVisible();
  const narrowJoinWhereMetrics = await joinWhereButton.evaluate((element) => ({
    height: element.getBoundingClientRect().height,
    expectedHeight: window.matchMedia("(pointer: coarse)").matches ? 44 : 40,
    clipped: element.scrollWidth > element.clientWidth,
  }));
  expect(narrowJoinWhereMetrics.height).toBe(narrowJoinWhereMetrics.expectedHeight);
  expect(narrowJoinWhereMetrics.clipped).toBe(false);
  joinWhereGate = createRequestGate();
  await joinWhereButton.click();
  const mobileJoinWhereSkeleton = page.getByTestId("view-join-where-result-detail-skeleton");
  await expect(mobileJoinWhereSkeleton).toBeVisible();
  await expect(joinWhereButton.locator("svg.animate-spin")).toHaveCount(1);
  await expect(mobileJoinWhereSkeleton.locator("svg.animate-spin")).toHaveCount(0);
  await expectTopToBottomOrder(page.getByTestId("view-join-where-actions"), mobileJoinWhereSkeleton);
  await expectNoHorizontalScroll(page);
  joinWhereGate.release();
  await expect(page.getByLabel("結合条件 (JOIN)")).toHaveValue(/EMPLOYEE/);
  await expectNoHorizontalScroll(page);
});

test("metadata management target lists load more tables and views before SQL generation", async ({ page }) => {
  const api = await mockNl2SqlApi(page);
  await page.setViewportSize({ width: 1280, height: 900 });

  const columns = [
    {
      column_name: "ID",
      logical_name: "識別子",
      data_type: "NUMBER",
      nullable: false,
      comment: "識別子",
      sample_values: ["1"],
    },
  ];
  const baseItems = Array.from({ length: 100 }, (_, index) => {
    const suffix = String(index + 1).padStart(3, "0");
    return {
      name: `META_TABLE_${suffix}`,
      owner: "APP",
      object_type: "table",
      row_count: 1,
      comment: `メタデータ確認 ${suffix}`,
    };
  });
  const routePagedObjects = async (secondItem: {
    name: string;
    owner: string;
    object_type: "table" | "view";
    row_count: number | null;
    comment: string;
  }) => {
    await page.unroute("**/api/nl2sql/db-admin/objects?*");
    await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
      const url = new URL(route.request().url());
      const objectType = url.searchParams.get("type") ?? "all";
      const query = (url.searchParams.get("q") ?? "").toLowerCase();
      const cursor = url.searchParams.get("cursor") ?? "";
      const allItems = [...baseItems, secondItem];
      const items = allItems.filter((item) => {
        if (objectType !== "all" && item.object_type !== objectType) return false;
        return !query || `${item.name} ${item.owner} ${item.comment}`.toLowerCase().includes(query);
      });
      const pagedItems = query
        ? items
        : cursor === "metadata-page-2"
          ? items.slice(100)
          : items.slice(0, 100);
      return fulfillJson(route, {
        runtime: "deterministic",
        owner: "APP",
        items: pagedItems,
        total: items.length,
        table_count: items.filter((item) => item.object_type === "table").length,
        view_count: items.filter((item) => item.object_type === "view").length,
        next_cursor: !query && !cursor && items.length > 100 ? "metadata-page-2" : null,
        refreshed_at: schemaCatalog.refreshed_at,
        catalog_version: 1,
        warnings: [],
      });
    });
  };

  await page.route("**/api/nl2sql/db-admin/tables/PAGE_101_TABLE*", (route) =>
    fulfillJson(route, {
      name: "PAGE_101_TABLE",
      owner: "APP",
      object_type: "table",
      row_count: 1,
      comment: "101件目のテーブル",
      columns,
      ddl: "",
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/views/V_PAGE_101_VIEW*", (route) =>
    fulfillJson(route, {
      name: "V_PAGE_101_VIEW",
      owner: "APP",
      object_type: "view",
      row_count: null,
      comment: "101件目のビュー",
      columns,
      ddl: "",
      warnings: [],
    })
  );

  await routePagedObjects({
    name: "PAGE_101_TABLE",
    owner: "APP",
    object_type: "table",
    row_count: 1,
    comment: "101件目のテーブル",
  });
  await page.goto("/comment-management");
  const commentFooter = page.getByTestId("comment-management-target-footer");
  await expect(commentFooter).toContainText("100 / 101 件を表示");
  const commentBulkActions = page.getByTestId("comment-management-target-bulk-actions");
  await commentBulkActions.getByRole("button", { name: "表示中をすべて選択" }).click();
  await expect(commentFooter).toContainText("選択 100 件");
  await commentFooter.getByRole("button", { name: "さらに読み込む" }).click();
  await expect(commentFooter).toContainText("101 / 101 件を表示");
  // 100 行を超えると見えている行だけを描く（ListPicker の仮想スクロール。#600）。末尾の行までスクロールする。
  await page
    .getByTestId("comment-management-target-scroll-region")
    .evaluate((node) => node.scrollTo({ top: node.scrollHeight }));
  await page.getByRole("option", { name: /PAGE_101_TABLE/ }).click();
  await expect(page.getByText("一度に選択できる対象は 100 件までです。")).toBeVisible();
  await expect(page.getByRole("option", { name: /PAGE_101_TABLE/ })).not.toBeChecked();
  await commentBulkActions.getByRole("button", { name: "選択をすべて解除" }).click();
  await expect(commentFooter).toContainText("選択 0 件");
  await page.getByRole("option", { name: /PAGE_101_TABLE/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.PAGE_101_TABLE/);
  await page.getByRole("button", { name: "SQL 生成" }).click();
  await expect(page.locator("#comment-management-panel-execute").getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue(/COMMENT ON COLUMN/);
  expect(api.metadataSamplesPayload?.targets).toEqual([
    {
      object_name: "PAGE_101_TABLE",
      owner: "APP",
      object_type: "table",
      columns: ["ID"],
    },
  ]);

  await routePagedObjects({
    name: "V_PAGE_101_VIEW",
    owner: "APP",
    object_type: "view",
    row_count: null,
    comment: "101件目のビュー",
  });
  await page.goto("/annotation-management");
  const annotationToolbar = page.getByTestId("annotation-management-target-toolbar");
  const annotationFooter = page.getByTestId("annotation-management-target-footer");
  await expect(annotationFooter).toContainText("100 / 101 件を表示");
  await annotationToolbar.getByRole("searchbox", { name: "検索" }).fill("V_PAGE_101_VIEW");
  await expect(annotationFooter).toContainText("1 / 1 件を表示");
  await page.getByRole("option", { name: /V_PAGE_101_VIEW/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.V_PAGE_101_VIEW/);
  await page.getByRole("button", { name: "SQL 生成" }).click();
  await expect(page.locator("#annotation-management-panel-execute").getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue(/ALTER TABLE/);
});

test("metadata SQL regeneration resets the edited execution SQL", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.setViewportSize({ width: 1280, height: 900 });

  const generatedSql = 'COMMENT ON TABLE "APP"."INVOICES" IS \'請求情報\';';
  let generationCount = 0;
  await page.unroute("**/api/nl2sql/comments/generate-sql");
  await page.route("**/api/nl2sql/comments/generate-sql", (route) => {
    generationCount += 1;
    return fulfillJson(route, {
      sql: generatedSql,
      source: "deterministic",
      warnings: [],
      timing,
    });
  });

  await page.goto("/comment-management");
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.INVOICES/);
  const inputPanel = page.locator("#comment-management-panel-input");
  await expect(inputPanel).toBeVisible();
  await inputPanel.getByRole("button", { name: "SQL 生成" }).click();

  const sqlTextarea = page.locator("#comment-management-panel-execute").getByLabel(
    "SQL(セミコロン区切りで複数文を入力可能)"
  );
  await expect(sqlTextarea).toHaveValue(generatedSql);
  await sqlTextarea.fill("COMMENT ON TABLE BROKEN IS '手編集';");
  await page.getByRole("tab", { name: "入力確認・SQL生成", exact: true }).click();
  await page.getByRole("button", { name: "SQL 生成" }).click();
  await expect(page.getByRole("tab", { name: "SQL実行", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect.poll(() => generationCount).toBe(2);
  await expect(sqlTextarea).toHaveValue(generatedSql);
});

for (const metadata of [
  { mode: "comment", path: "comments", source: "deterministic", label: "規則ベース（AI 未使用）" },
  { mode: "annotation", path: "annotations", source: "oci_enterprise_ai", label: "OCI Enterprise AI" },
  { mode: "domain", path: "domains", source: "deterministic", label: "規則ベース（AI 未使用）" },
] as const) {
  test(`${metadata.mode}-management は SQL の生成方式を内部値ではなく文言で出す`, async ({ page }) => {
    // #962: generated.source（oci_enterprise_ai / deterministic）を英語の内部値のまま出さない。
    await mockNl2SqlApi(page);
    await page.unroute(`**/api/nl2sql/${metadata.path}/generate-sql`);
    await page.route(`**/api/nl2sql/${metadata.path}/generate-sql`, (route) =>
      fulfillJson(route, { sql: "COMMENT ON TABLE INVOICES IS '請求';", source: metadata.source, warnings: [], timing })
    );
    await page.goto(`/${metadata.mode}-management`);
    await page.getByRole("option", { name: /INVOICES/ }).check();
    await page.getByRole("button", { name: "情報を取得", exact: true }).click();
    const inputPanel = page.locator(`#${metadata.mode}-management-panel-input`);
    await expect(inputPanel.getByRole("button", { name: "SQL 生成" })).toBeEnabled();
    await inputPanel.getByRole("button", { name: "SQL 生成" }).click();
    const executePanel = page.locator(`#${metadata.mode}-management-panel-execute`);
    await expect(executePanel.getByText(metadata.label, { exact: true })).toBeVisible();
    await expect(executePanel.getByText(metadata.source, { exact: true })).toHaveCount(0);
    await expectNoHorizontalScroll(page);
  });
}

const METADATA_FALLBACK_DETAIL =
  "モデル「xai.grok-4.3」の応答が上限を広げて送り直しても出力の上限に達して未完了でした" +
  "（status=incomplete・理由=max_output_tokens・出力の上限=32768 tokens・使った出力=32768 tokens（うち推論 31000））。";

for (const metadata of [
  { mode: "comment", path: "comments", sql: "COMMENT ON TABLE APP.INVOICES IS 'INVOICES';" },
  { mode: "annotation", path: "annotations", sql: "ALTER TABLE APP.INVOICES ANNOTATIONS (ADD OR REPLACE \"DESCRIPTION\" 'INVOICES');" },
  { mode: "domain", path: "domains", sql: "CREATE DOMAIN IF NOT EXISTS APP.INVOICE_ID_D AS NUMBER;" },
] as const) {
  test(
    `${metadata.mode}-management は AI で作れずに規則ベースにした SQL を、理由と詳細つきの danger の Banner で出す (#1719)`,
    { tag: "@responsive" },
    async ({ page }) => {
      await mockNl2SqlApi(page);
      await page.unroute(`**/api/nl2sql/${metadata.path}/generate-sql`);
      await page.route(`**/api/nl2sql/${metadata.path}/generate-sql`, (route) =>
        fulfillJson(route, {
          sql: metadata.sql,
          source: "deterministic",
          warnings: [],
          fallback: true,
          fallback_objects: [],
          fallback_reason:
            "OCI Enterprise AI の応答が出力の上限に達して未完了でした。システム設定 › モデル で既定のテキストモデルの「出力の上限（tokens）」を大きくするか、対象の表を減らしてやり直してください。",
          fallback_detail: METADATA_FALLBACK_DETAIL,
          timing,
        })
      );
      await page.goto(`/${metadata.mode}-management`);
      await page.getByRole("option", { name: /INVOICES/ }).check();
      await page.getByRole("button", { name: "情報を取得", exact: true }).click();
      const inputPanel = page.locator(`#${metadata.mode}-management-panel-input`);
      await inputPanel.getByRole("button", { name: "SQL 生成" }).click();
      const executePanel = page.locator(`#${metadata.mode}-management-panel-execute`);
      const fallback = executePanel.getByTestId(`${metadata.mode}-management-ai-fallback`);
      await expect(fallback).toBeVisible();
      await expect(fallback.getByRole("alert")).toContainText(
        "AI で SQL を生成できなかったため、規則ベースの SQL を表示しています"
      );
      await expect(fallback).toContainText("表・列の名前をそのまま説明に入れた仮のもの");
      await expect(fallback).toContainText("「出力の上限（tokens）」を大きくする");
      // 失敗の内容（理由・上限・使った token）は「詳細」に畳み、失敗なので開いて出す。
      await expect(fallback.getByText("理由=max_output_tokens", { exact: false })).toBeVisible();
      // AI を使わなかった場合（未設定）と分けて、AI で作れなかったことを状態の文言で示す。
      await expect(executePanel.getByText("規則ベース（AI で生成できず）", { exact: true })).toBeVisible();
      await expect(executePanel.getByText("規則ベース（AI 未使用）", { exact: true })).toHaveCount(0);
      await expect(executePanel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue(metadata.sql);
      // 規則ベースの結果を「生成が完了しました」の成功の Toast と重ねない。
      await expect(page.getByText("SQL 生成が完了しました。")).toHaveCount(0);
      const box = await fallback.boundingBox();
      const panelBox = await executePanel.boundingBox();
      expect(box && panelBox && box.width).toBeGreaterThan((panelBox?.width ?? 0) - 64);
      await expectNoHorizontalScroll(page);
      if (process.env.METADATA_FALLBACK_SCREENSHOT_DIR) {
        await page.screenshot({
          path: `${process.env.METADATA_FALLBACK_SCREENSHOT_DIR}/${metadata.mode}-fallback-${test.info().project.name}.png`,
          fullPage: true,
        });
      }
    }
  );
}

test("comment-management は一部の表だけ規則ベースにした結果を、表の名前つきで出す (#1719)", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/comments/generate-sql");
  await page.route("**/api/nl2sql/comments/generate-sql", (route) =>
    fulfillJson(route, {
      sql: "COMMENT ON TABLE APP.INVOICES IS '請求';",
      source: "oci_enterprise_ai",
      warnings: [],
      fallback: true,
      fallback_objects: ["APP.INVOICES"],
      fallback_reason: "OCI Enterprise AI の呼び出しに失敗しました。時間をおいてやり直してください。",
      fallback_detail: "APP.INVOICES: timeout after 600.0s",
      timing,
    })
  );
  await page.goto("/comment-management");
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await page.locator("#comment-management-panel-input").getByRole("button", { name: "SQL 生成" }).click();
  const executePanel = page.locator("#comment-management-panel-execute");
  const fallback = executePanel.getByTestId("comment-management-ai-fallback");
  await expect(fallback).toContainText("一部の表は AI で SQL を生成できず、規則ベースの SQL にしています");
  await expect(fallback).toContainText("規則ベースにした表: APP.INVOICES");
  await expect(executePanel.getByText("OCI Enterprise AI（一部は規則ベース）", { exact: true })).toBeVisible();
});

test("ドメイン管理は SQL 実行後に構造と既存ドメインを取り直す", async ({ page }) => {
  // 実行前の関連付けのまま更新/削除を生成しないよう、実行後に inventory を再取得する。
  const api = await mockNl2SqlApi(page);
  let inventoryCount = 0;
  page.on("request", (request) => {
    if (request.url().includes("/api/nl2sql/domains/inventory")) inventoryCount += 1;
  });
  await page.goto("/domain-management");
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const inputPanel = page.locator("#domain-management-panel-input");
  await expect(inputPanel.getByLabel("構造情報")).toHaveValue(/APP\.INVOICES/);
  await expect.poll(() => inventoryCount).toBe(1);
  await inputPanel.getByRole("button", { name: "SQL 生成", exact: true }).click();
  const executePanel = page.locator("#domain-management-panel-execute");
  await expect(executePanel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue(/CREATE DOMAIN/);
  await executePanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await executePanel.getByRole("button", { name: "SQL 実行" }).click();
  await expect.poll(() => api.statementsPayload?.policy).toBe("domain_sql");
  await expect.poll(() => inventoryCount).toBe(2);
  await expect(executePanel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue(/CREATE DOMAIN/);
});

test("コメント管理は画面遷移後も生成 SQL と実行結果を保持する", async ({ page }) => {
  const api = await mockNl2SqlApi(page);
  await page.setViewportSize({ width: 1280, height: 900 });

  await page.goto("/comment-management");
  await expect(page.getByRole("heading", { name: "コメント管理" })).toBeVisible();
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.INVOICES/);
  const inputPanel = page.locator("#comment-management-panel-input");
  await expect(inputPanel).toBeVisible();
  await inputPanel.getByRole("button", { name: "SQL 生成" }).click();

  const executePanel = page.locator("#comment-management-panel-execute");
  const sqlTextarea = executePanel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)");
  await expect(sqlTextarea).toHaveValue(
    /COMMENT ON COLUMN "APP"\."INVOICES"\."TOTAL_AMOUNT" IS '税込請求金額';/
  );
  await executePanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await executePanel.getByRole("button", { name: "SQL 実行" }).click();
  await expect.poll(() => api.statementsPayload?.policy).toBe("comment_sql");
  await expect(executePanel.getByTestId("statement-runner-comment_sql-execution-activity")).toContainText(
    "SQL 実行が完了しました。"
  );
  await expect(executePanel.getByText("実行済み", { exact: true }).first()).toBeVisible();

  await page.locator('a[href="/table-management"]').first().evaluate((node: HTMLElement) => node.click());
  await expect(page.getByRole("heading", { name: "テーブル一覧", exact: true })).toBeVisible();
  await page.locator('a[href="/comment-management"]').first().evaluate((node: HTMLElement) => node.click());
  await expect(page.getByRole("heading", { name: "コメント管理" })).toBeVisible();

  const restoredExecutePanel = page.locator("#comment-management-panel-execute");
  await expect(page.getByTestId("comment-management-target-footer")).toContainText("選択 1 件");
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.INVOICES/);
  await expect(
    restoredExecutePanel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)")
  ).toHaveValue(/COMMENT ON COLUMN "APP"\."INVOICES"\."TOTAL_AMOUNT" IS '税込請求金額';/);
  await expect(
    restoredExecutePanel.getByTestId("statement-runner-comment_sql-execution-activity")
  ).toContainText("SQL 実行が完了しました。");
  await expect(restoredExecutePanel.getByText("実行済み", { exact: true }).first()).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test("table and view management object lists load more and find unloaded objects", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.setViewportSize({ width: 1280, height: 900 });

  type PagedObject = {
    name: string;
    owner: string;
    object_type: "table" | "view";
    row_count: number | null;
    comment: string;
  };
  const columns = [
    {
      column_name: "ID",
      logical_name: "識別子",
      data_type: "NUMBER",
      nullable: false,
      comment: "識別子",
      sample_values: ["1"],
    },
  ];
  const tableObjects: PagedObject[] = Array.from({ length: 101 }, (_, index) => {
    const suffix = String(index + 1).padStart(3, "0");
    return {
      name: `PAGE_TABLE_${suffix}`,
      owner: "APP",
      object_type: "table",
      row_count: index + 1,
      comment: `${index + 1}件目のテーブル`,
    };
  });
  const viewObjects: PagedObject[] = Array.from({ length: 101 }, (_, index) => {
    const suffix = String(index + 1).padStart(3, "0");
    return {
      name: `V_PAGE_VIEW_${suffix}`,
      owner: "APP",
      object_type: "view",
      row_count: null,
      comment: `${index + 1}件目のビュー`,
    };
  });

  const routePagedObjects = async (
    objects: PagedObject[],
    expectedType: "table" | "view",
    nextCursor: string
  ) => {
    await page.unroute("**/api/nl2sql/db-admin/objects?*");
    await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
      const url = new URL(route.request().url());
      const objectType = url.searchParams.get("type") ?? "all";
      const rowState = url.searchParams.get("row_state") ?? "all";
      const query = (url.searchParams.get("q") ?? "").toLowerCase();
      const cursor = url.searchParams.get("cursor") ?? "";
      const items = objects.filter((item) => {
        if (objectType !== "all" && item.object_type !== objectType) return false;
        if (rowState === "with_rows" && !(typeof item.row_count === "number" && item.row_count > 0)) return false;
        if (rowState === "empty_rows" && item.row_count !== 0) return false;
        return !query || `${item.name} ${item.owner} ${item.comment}`.toLowerCase().includes(query);
      });
      const pageItems = query ? items : cursor === nextCursor ? items.slice(100) : items.slice(0, 100);
      return fulfillJson(route, {
        runtime: "deterministic",
        owner: "APP",
        items: pageItems,
        total: items.length,
        table_count: expectedType === "table" ? items.length : 0,
        view_count: expectedType === "view" ? items.length : 0,
        next_cursor: !query && !cursor && items.length > 100 ? nextCursor : null,
        refreshed_at: schemaCatalog.refreshed_at,
        catalog_version: 1,
        warnings: [],
      });
    });
  };
  const routeObjectDetail = async (kind: "table" | "view") => {
    const collection = kind === "table" ? "tables" : "views";
    const namePattern = kind === "table" ? "PAGE_*" : "V_PAGE_*";
    await page.route(`**/api/nl2sql/db-admin/${collection}/${namePattern}`, (route) => {
      const url = new URL(route.request().url());
      const name = decodeURIComponent(url.pathname.split("/").pop() ?? "");
      const includeDdl = url.searchParams.get("include_ddl") === "1";
      return fulfillJson(route, {
        name,
        owner: "APP",
        object_type: kind,
        row_count: kind === "table" ? 1 : null,
        comment: name.endsWith("_101") ? "101件目の対象" : "ページング対象",
        columns,
        ddl: includeDdl
          ? kind === "table"
            ? `CREATE TABLE "${name}" ("ID" NUMBER);`
            : `CREATE OR REPLACE VIEW "${name}" AS SELECT ID FROM PAGE_TABLE_001;`
          : "",
        warnings: [],
      });
    });
  };
  await routeObjectDetail("table");
  await routeObjectDetail("view");

  await routePagedObjects(tableObjects, "table", "table-management-page-2");
  await page.goto("/table-management");
  const tableFooter = page.getByTestId("table-management-footer");
  const tableSearch = page.getByRole("searchbox", { name: "検索" });
  await expect(tableFooter).toContainText("100 / 101 件を表示");
  await tableSearch.fill("PAGE_TABLE_101");
  await expect(tableFooter).toContainText("1 / 1 件を表示");
  await page.getByRole("button", { name: "PAGE_TABLE_101 を表示" }).click();
  await expect(page.getByTestId("table-management-detail-header")).toContainText("PAGE_TABLE_101");
  await expect(page.getByTestId("db-admin-detail-columns").getByRole("rowheader", { name: "ID" })).toBeVisible();
  await tableSearch.fill("");
  await expect(tableFooter).toContainText("100 / 101 件を表示");
  await tableFooter.getByRole("button", { name: "さらに読み込む" }).click();
  await expect(tableFooter).toContainText("101 / 101 件を表示");
  await expect(page.getByRole("button", { name: "PAGE_TABLE_101 を表示" })).toBeVisible();

  await routePagedObjects(viewObjects, "view", "view-management-page-2");
  await page.goto("/view-management");
  const viewFooter = page.getByTestId("view-management-footer");
  const viewSearch = page.getByRole("searchbox", { name: "検索" });
  await expect(viewFooter).toContainText("100 / 101 件を表示");
  await viewFooter.getByRole("button", { name: "さらに読み込む" }).click();
  await expect(viewFooter).toContainText("101 / 101 件を表示");
  await page.getByRole("button", { name: "V_PAGE_VIEW_101 を表示" }).click();
  await expect(page.getByTestId("view-management-detail-header")).toContainText("V_PAGE_VIEW_101");
  await expect(page.getByTestId("db-admin-detail-columns").getByRole("rowheader", { name: "ID" })).toBeVisible();
  await page.goto("/view-management");
  await expect(viewFooter).toContainText("100 / 101 件を表示");
  await viewSearch.fill("V_PAGE_VIEW_101");
  await expect(viewFooter).toContainText("1 / 1 件を表示");
  await expect(page.getByRole("button", { name: "V_PAGE_VIEW_101 を表示" })).toBeVisible();
});

test("glossary and global rules use an initial list skeleton and preserve fetched rows on background reload", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.unroute("**/api/nl2sql/legacy-learning-material");

  const glossaryInitialGate = createRequestGate();
  const glossaryReloadGate = createRequestGate();
  let glossaryRequests = 0;
  await page.route("**/api/nl2sql/legacy-learning-material", async (route) => {
    glossaryRequests += 1;
    if (glossaryRequests === 1) await glossaryInitialGate.promise;
    if (glossaryRequests === 2) await glossaryReloadGate.promise;
    return fulfillJson(route, {
      glossary: { 売上: "INVOICES.TOTAL_AMOUNT" },
      rules: ["SELECT のみ"],
    });
  });

  await page.goto("/glossary-rules");
  await expect(page.getByTestId("glossary-terms-list-skeleton")).toBeVisible();
  await expect(page.getByTestId("glossary-terms-preview")).toHaveCount(0);
  await expect(page.getByText("データがありません。")).toHaveCount(0);
  glossaryInitialGate.release();
  await expect(page.getByTestId("glossary-terms-preview").getByRole("cell", { name: "売上" })).toBeVisible();

  await page.getByRole("button", { name: "最新情報を取得" }).click();
  await expect.poll(() => glossaryRequests).toBe(2);
  await expect(page.getByTestId("glossary-terms-preview").getByRole("cell", { name: "売上" })).toBeVisible();
  await expect(page.getByTestId("glossary-terms-list-skeleton")).toHaveCount(0);
  glossaryReloadGate.release();
  await expect(page.getByRole("button", { name: "最新情報を取得" })).toBeEnabled();

  await page.unroute("**/api/nl2sql/legacy-learning-material");
  const rulesInitialGate = createRequestGate();
  const rulesReloadGate = createRequestGate();
  let rulesRequests = 0;
  await page.route("**/api/nl2sql/legacy-learning-material", async (route) => {
    rulesRequests += 1;
    if (rulesRequests === 1) await rulesInitialGate.promise;
    if (rulesRequests === 2) await rulesReloadGate.promise;
    return fulfillJson(route, {
      glossary: { 売上: "INVOICES.TOTAL_AMOUNT" },
      rules: ["SELECT のみ"],
    });
  });

  await page.goto("/global-rules");
  await expect(page.getByText(/取込は既存データを全置換します/)).toBeVisible();
  await expect(page.getByTestId("global-rules-file-input")).toHaveAttribute(
    "accept",
    ".xlsx"
  );
  await expect(page.getByText(".XLSX", { exact: true })).toBeVisible();
  await expect(page.getByTestId("global-rules-list-skeleton")).toBeVisible();
  await expect(page.getByTestId("global-rules-preview")).toHaveCount(0);
  await expect(page.getByText("共通ルールがありません。")).toHaveCount(0);
  rulesInitialGate.release();
  await expect(page.getByTestId("global-rules-preview").getByText("SELECT のみ")).toBeVisible();

  await page.getByRole("button", { name: "最新情報を取得" }).click();
  await expect.poll(() => rulesRequests).toBe(2);
  await expect(page.getByTestId("global-rules-preview").getByText("SELECT のみ")).toBeVisible();
  await expect(page.getByTestId("global-rules-list-skeleton")).toHaveCount(0);
  rulesReloadGate.release();
  await expect(page.getByRole("button", { name: "最新情報を取得" })).toBeEnabled();
  await dropFiles(page, page.getByTestId("global-rules-file-dropzone"), [
    {
      name: "rules.csv",
      type: "text/csv",
      content: "RULE\n集計時は NULL を除外する\n",
    },
  ]);
  await expect(page.getByText(".XLSX ファイルを選択してください")).toBeVisible();
  let ruleImports = 0;
  page.on("request", (request) => {
    if (request.url().includes("/legacy-learning-material/rules/import")) ruleImports += 1;
  });
  const rulesWorkbook = [
    {
      name: "rules.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock rules",
    },
  ];
  // 取込は登録済みのルールを置き換えるので確認する。キャンセルなら送らない（#953）。
  await dropFiles(page, page.getByTestId("global-rules-file-dropzone"), rulesWorkbook);
  const replaceDialog = page.getByRole("alertdialog");
  await expect(replaceDialog).toContainText("登録済みの共通ルール 1 件を、rules.xlsx の内容ですべて置き換えます。");
  await replaceDialog.getByRole("button", { name: "キャンセル", exact: true }).click();
  await expect(replaceDialog).toHaveCount(0);
  await expect(page.getByTestId("global-rules-preview").getByText("SELECT のみ")).toBeVisible();
  expect(ruleImports).toBe(0);
  await dropFiles(page, page.getByTestId("global-rules-file-dropzone"), rulesWorkbook);
  await confirmMaterialReplace(page);
  await expect(page.getByText(/共通ルールを取り込みました/)).toBeVisible();
  expect(ruleImports).toBe(1);
  await expectNoHorizontalScroll(page);

  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
});

test("data management CSV upload layout keeps table, file, mode, and execution in full-width rows", { tag: "@responsive" }, async ({ page }) => {
  await mockNl2SqlApi(page);

  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/data-management");
  await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)" }).click();
  const desktopCsvPanel = page.locator("#data-management-panel-csv");
  await expect(desktopCsvPanel).toBeVisible();
  await expectCsvUploadLayout(desktopCsvPanel);
  await expect(desktopCsvPanel.getByText("CSV / XLSX / XLS ファイルを選択してください。")).toHaveCount(0);
  await expectNoHorizontalScroll(page);

  await page.setViewportSize({ width: 375, height: 900 });
  await page.goto("/data-management");
  await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)" }).click();
  const mobileCsvPanel = page.locator("#data-management-panel-csv");
  await expect(mobileCsvPanel).toBeVisible();
  await expectCsvUploadLayout(mobileCsvPanel);
  await expect(mobileCsvPanel.getByText("CSV / XLSX / XLS ファイルを選択してください。")).toHaveCount(0);
  await expectNoHorizontalScroll(page);
});

test("data management CSV upload hides unmatched columns when Oracle reports none", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/db-admin/upload-csv");
  await page.route("**/api/nl2sql/db-admin/upload-csv", (route) => {
    return fulfillJson(route, {
      table_name: "INVOICES",
      filename: "book2.csv",
      mode: "insert",
      matched_columns: ["ID", "NAME"],
      unmatched_csv_columns: [],
      row_count: 2,
      success_count: 2,
      error_count: 0,
      row_errors: [],
      hint: "",
      executed: true,
      runtime: "oracle",
      sample_rows: [
        { ID: "123", NAME: "456" },
        { ID: "666", NAME: "777" },
      ],
      warnings: [],
      timing,
    });
  });

  await page.goto("/data-management");
  await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)" }).click();
  const csvPanel = page.locator("#data-management-panel-csv");
  await expect(csvPanel.getByTestId("data-csv-table-list").getByText("APP.INVOICES", { exact: true })).toBeVisible();
  await csvPanel.getByLabel("実行確認語").fill("APP.INVOICES");
  await dropFiles(page, page.getByTestId("data-csv-file-field-dropzone"), [
    {
      name: "book2.csv",
      type: "text/csv",
      content: "ID,NAME\n123,456\n666,777\n",
    },
  ]);

  await expect(csvPanel.getByLabel("実行確認語")).toHaveValue("");
  await csvPanel.getByLabel("実行確認語").fill("APP.INVOICES");
  await csvPanel.getByRole("button", { name: "アップロード実行" }).click();

  await expect(csvPanel.getByText(/一致列:\s*ID, NAME/)).toBeVisible();
  await expect(csvPanel.getByText("不一致列", { exact: false })).toHaveCount(0);
});

const domainSamples = [
  // 実行確認語は種類によらず ADMIN_EXECUTE に統一。旧確認語は受け付けない。
  { dataset: "sales", label: "売上サンプルデータ", legacyPhrase: "NL2SQL_SALES_SAMPLE", object: "SALES_ORDER" },
  { dataset: "inquiries", label: "問い合わせサンプルデータ", legacyPhrase: "NL2SQL_INQUIRY_SAMPLE", object: "INQUIRY_TICKET" },
] as const;

for (const sample of domainSamples) {
  test(`${sample.label}を選んで投入・削除し、種類の切替で確認を解除する`, async ({ page }, testInfo) => {
    await mockNl2SqlApi(page);
    let imported = false;
    const requests: Array<Record<string, unknown>> = [];
    await page.route(`**/api/nl2sql/sample-data?dataset=${sample.dataset}`, (route) => fulfillJson(route, {
      dataset: sample.dataset, runtime: "deterministic", profile_id: "", confirmation: "ADMIN_EXECUTE",
      objects: [sample.object], imported_objects: imported ? [sample.object] : [], warnings: [],
      sql: { tables: [`CREATE TABLE ${sample.object} (ID NUMBER)`], views: [], data: [`INSERT INTO ${sample.object} VALUES (1)`], delete: [`DROP TABLE ${sample.object}`] },
    }));
    for (const operation of ["import", "delete"]) {
      await page.route(`**/api/nl2sql/sample-data/${operation}`, (route) => {
        const payload = route.request().postDataJSON();
        if (payload.dataset !== sample.dataset) return route.fallback();
        requests.push({ operation, ...payload });
        imported = operation === "import";
        return fulfillJson(route, { dataset: sample.dataset, operation, step: payload.step, runtime: "deterministic", executed: true,
          objects: [sample.object], statements: [], warnings: [], profile_id: "", timing });
      });
    }
    await page.goto("/sample-data");
    const selector = page.getByRole("combobox", { name: "サンプルデータの種類" });
    await expectSelectFieldValue(selector, "hr");
    // 右に出す種類の説明を aria-describedby で読み上げる（#647）。
    await expect(selector).toHaveAccessibleDescription("部署・社員・プロジェクトと、それらを結び付けたビューを作成します。");
    await expect(await openSelectFieldOptions(selector)).toHaveText(["人事サンプルデータ", "売上サンプルデータ", "問い合わせサンプルデータ"]);
    await closeSelectFieldOptions(selector);
    const confirmation = page.getByLabel("実行確認語");
    const execute = page.getByRole("button", { name: "取り込み実行", exact: true });
    await confirmation.fill("ADMIN_EXECUTE");
    await execute.click();
    await expect(page.getByTestId("sample-data-imported-count")).toHaveText("5");
    await chooseSelectFieldOption(selector, sample.dataset);
    // 種類を切り替えると、読み上げる説明も切り替えた種類のものになる。
    await expect(selector).toHaveAccessibleDescription(/^(顧客・商品・受注|顧客・問い合わせ分類)/);
    await expect(page.getByTestId("sample-data-imported-count")).toHaveText("0");
    await expect(confirmation).toHaveValue("");
    await expect(confirmation).toHaveAttribute("placeholder", "ADMIN_EXECUTE");
    await expect(page.locator("pre")).toContainText(`CREATE TABLE ${sample.object}`);
    await expect(page.locator("pre")).not.toContainText("DEPARTMENT");
    await expect(page.getByText(`dataTools.sample.dataset.${sample.dataset}`)).toHaveCount(0);
    await expectNoHorizontalScroll(page);
    await page.getByRole("region", { name: "サンプルデータの種類" }).screenshot({ path: testInfo.outputPath(`${sample.dataset}-selector.png`) });
    for (const legacyPhrase of ["SQL_ASSIST_SAMPLE", sample.legacyPhrase]) {
      await confirmation.fill(legacyPhrase);
      await expect(execute).toBeDisabled();
    }
    await confirmation.fill("ADMIN_EXECUTE");
    await execute.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("sample-data-imported-count")).toHaveText("1");
    expect(requests).toEqual([{ operation: "import", dataset: sample.dataset, step: "all", confirmation: "ADMIN_EXECUTE", reason: "ui-sample-import" }]);
    await page.getByRole("tab", { name: "削除実行", exact: true }).click();
    await expect(confirmation).toHaveValue("");
    await expect(page.getByText(`${sample.label}のテーブル・ビューと格納されたデータをすべて削除します。他の種類のサンプルデータは保持します。`)).toBeVisible();
    await expect(page.locator("pre")).toHaveText(`DROP TABLE ${sample.object}`);
    await confirmation.fill("ADMIN_EXECUTE");
    await page.reload();
    await expectSelectFieldValue(selector, sample.dataset);
    await expect(page.getByRole("tab", { name: "削除実行", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(confirmation).toHaveValue("");
    expect(requests).toHaveLength(1);
    await confirmation.fill("ADMIN_EXECUTE");
    await page.getByRole("button", { name: "削除実行", exact: true }).click();
    await expect(page.getByTestId("sample-data-imported-count")).toHaveText("0");
    expect(requests[1]).toMatchObject({ operation: "delete", dataset: sample.dataset, confirmation: "ADMIN_EXECUTE" });
    await chooseSelectFieldOption(selector, "hr");
    await expect(confirmation).toHaveValue("");
    await expect(page.getByTestId("sample-data-imported-count")).toHaveText("5");
    await expectNoHorizontalScroll(page);
  });
}

test("サンプルの種類変更中・取得失敗時は旧 SQL を実行できず再取得で復旧する", async ({ page }) => {
  await mockNl2SqlApi(page);
  const gate = createRequestGate();
  let fail = true;
  let mutations = 0;
  page.on("request", (request) => {
    if (/\/sample-data\/(import|delete)$/.test(new URL(request.url()).pathname)) mutations += 1;
  });
  await page.route("**/api/nl2sql/sample-data?dataset=sales", async (route) => {
    if (fail) {
      await gate.promise;
      return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "サンプルの取得に失敗しました。表示を更新してください。" }) });
    }
    return fulfillJson(route, { dataset: "sales", runtime: "deterministic", profile_id: "", confirmation: "ADMIN_EXECUTE",
      objects: ["SALES_ORDER"], imported_objects: [], warnings: [],
      sql: { tables: ["CREATE TABLE SALES_ORDER (ID NUMBER)"], views: [], data: [], delete: [] } });
  });
  await page.goto("/sample-data");
  const selector = page.getByRole("combobox", { name: "サンプルデータの種類" });
  await page.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await chooseSelectFieldOption(selector, "sales");
  try {
    await expect(page.getByTestId("sample-data-workspace-refresh-skeleton")).toBeVisible();
    await expect(selector).toBeDisabled();
    await expect(page.locator("pre")).toHaveCount(0);
  } finally { gate.release(); }
  await expect(page.getByText("サンプルの取得に失敗しました。表示を更新してください。", { exact: true })).toBeVisible();
  await expectSelectFieldValue(selector, "sales");
  await expect(page.getByRole("button", { name: "取り込み実行", exact: true })).toBeDisabled();
  await expect(page.locator("pre")).not.toContainText("DEPARTMENT");
  fail = false;
  await clickPageHeaderAction(page, "sample-data-actions", "最新情報を取得");
  await expect(page.locator("pre")).toContainText("SALES_ORDER");
  await expect(page.getByLabel("実行確認語")).toHaveValue("");
  expect(mutations).toBe(0);
  await expectNoHorizontalScroll(page);
});

test("サンプルの取込が成功した後の取り直しの失敗は、実行の失敗として出さずスキーマの更新を追う", async ({ page }) => {
  // #948: 取り直しの失敗を実行のボタンの直下に出すと、成功した取込が失敗したように見え、再実行を招く。
  await mockNl2SqlApi(page);
  let infoFails = false;
  const jobRequests: string[] = [];
  await page.route("**/api/nl2sql/sample-data?dataset=sales", (route) => {
    if (infoFails) {
      return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "サンプルの状態を取得できませんでした。" }) });
    }
    return fulfillJson(route, { dataset: "sales", runtime: "oracle", profile_id: "", confirmation: "ADMIN_EXECUTE",
      objects: ["SALES_ORDER"], imported_objects: jobRequests.length > 0 ? ["SALES_ORDER"] : [], warnings: [],
      sql: { tables: ["CREATE TABLE SALES_ORDER (ID NUMBER)"], views: [], data: [], delete: ["DROP TABLE SALES_ORDER"] } });
  });
  await page.route("**/api/nl2sql/sample-data/import", (route) => {
    infoFails = true;
    return fulfillJson(route, { dataset: "sales", operation: "import", step: "all", runtime: "oracle", executed: true,
      objects: ["SALES_ORDER"], profile_id: "", timing, warnings: [], schema_refresh_job_id: "schema-refresh-948",
      statements: [{ index: 1, statement_type: "CREATE", status: "success", sql: "CREATE TABLE SALES_ORDER (ID NUMBER)", error_message: "" }] });
  });
  await page.route("**/api/schema/refresh-jobs/schema-refresh-948", (route) => {
    jobRequests.push(route.request().url());
    return fulfillJson(route, { job_id: "schema-refresh-948", status: "done", created_at: "2026-10-04T00:00:00.000Z",
      scanned_objects: 1, changed_objects: 1, deleted_objects: 0, catalog_version: 2, error_code: "" });
  });
  await page.goto("/sample-data");
  await chooseSelectFieldOption(page.getByRole("combobox", { name: "サンプルデータの種類" }), "sales");
  await expect(page.locator("pre")).toContainText("SALES_ORDER");
  await page.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await page.getByRole("button", { name: "取り込み実行", exact: true }).click();

  const actionSection = page.locator('section[aria-labelledby="sample-data-action-heading"]');
  await expect(page.getByText("サンプルの状態を取得できませんでした。", { exact: true })).toBeVisible();
  // 実行のボタンの直下（実行の失敗の場所）には出さない。読込の失敗としてページ上部の案内に出す。
  await expect(actionSection.getByRole("alert")).toHaveCount(0);
  await expect(page.getByText("実行済み", { exact: true }).first()).toBeVisible();
  // 取り直しに失敗しても、backend が投入したスキーマの更新の job は追う。
  await expect.poll(() => jobRequests.length).toBeGreaterThan(0);
  await expectNoHorizontalScroll(page);

  infoFails = false;
  await clickPageHeaderAction(page, "sample-data-actions", "最新情報を取得");
  await expect(page.getByText("サンプルの状態を取得できませんでした。", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("sample-data-imported-count")).toHaveText("1");
});

test("同名の既存オブジェクトと衝突するサンプルは警告し、旧名の残存を案内する", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.route("**/api/nl2sql/sample-data?dataset=sales", (route) => fulfillJson(route, {
    dataset: "sales", runtime: "oracle", profile_id: "", confirmation: "ADMIN_EXECUTE",
    objects: ["SALES_CUSTOMER", "SALES_ORDER"], imported_objects: ["SALES_CUSTOMER"],
    owner: "APP",
    object_refs: [
      { name: "SALES_CUSTOMER", owner: "APP", qualified_name: "APP.SALES_CUSTOMER" },
      { name: "SALES_ORDER", owner: "APP", qualified_name: "APP.SALES_ORDER" },
    ],
    conflicting_objects: ["SALES_ORDER"], legacy_objects: ["SAMPLE_NL2SQL_SALES_ORDER"],
    warnings: [
      "サンプルデータと同名で構成が異なるオブジェクト（SALES_ORDER）が現在のスキーマにあります。",
      "旧名のサンプルデータ（SAMPLE_NL2SQL_SALES_ORDER）が残っています。削除を実行すると旧名のオブジェクトも削除します。",
    ],
    sql: { tables: ["CREATE TABLE SALES_ORDER (ID NUMBER)"], views: [], data: [], delete: ["DROP TABLE SAMPLE_NL2SQL_SALES_ORDER CASCADE CONSTRAINTS PURGE", "DROP TABLE SALES_ORDER CASCADE CONSTRAINTS PURGE"] },
  }));
  await page.goto("/sample-data");
  await chooseSelectFieldOption(page.getByRole("combobox", { name: "サンプルデータの種類" }), "sales");
  // 対象オブジェクトは所有者付きの修飾名と、アイコン付きの状態で示す（#556）。
  const sampleObjects = page.getByTestId("sample-data-object");
  await expect(sampleObjects.filter({ hasText: "APP.SALES_ORDER" })).toContainText("同名の既存オブジェクト");
  await expect(sampleObjects.filter({ hasText: "APP.SALES_CUSTOMER" })).toContainText("導入済み");
  await expect(page.getByText(/同名で構成が異なるオブジェクト（SALES_ORDER）/)).toBeVisible();
  await expect(page.getByText(/旧名のサンプルデータ（SAMPLE_NL2SQL_SALES_ORDER）/)).toBeVisible();
  await page.getByRole("tab", { name: "削除実行", exact: true }).click();
  await expect(page.locator("pre")).toContainText("DROP TABLE SAMPLE_NL2SQL_SALES_ORDER");
  await expectNoHorizontalScroll(page);
});

test("sample data and data management run imported workflows", { tag: "@responsive" }, async ({ page }) => {
  // 1 回に 21〜28 秒かかり、既定の 30 秒に近い。並列で負荷が高いと時間切れになるため延ばす。
  test.slow();
  const api = await mockNl2SqlApi(page);
  const currentPreviewDataPayload = () => api.previewDataPayload;
  const dbAdminObjectOwnerPrefixRequests: string[] = [];
  const dbAdminObjectExactOwnerRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/nl2sql/db-admin/objects") {
      dbAdminObjectOwnerPrefixRequests.push(url.searchParams.get("owner_prefix") ?? "");
      if (url.searchParams.has("owner")) {
        dbAdminObjectExactOwnerRequests.push(url.searchParams.get("owner") ?? "");
      }
    }
  });

  await page.goto("/sample-data");
  await expect(page.getByRole("heading", { name: "サンプルデータ管理", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "取り込み実行", exact: true })).toBeVisible();
  await expect(page.getByText("DEPARTMENT").first()).toBeVisible();
  await expect(page.getByLabel("実行する", { exact: true })).toHaveCount(0);

  const importPanel = page.locator("#sample-data-panel-import");
  const importConfirmationField = importPanel.getByTestId("execution-confirmation-field");
  await expect(importConfirmationField).toHaveClass(/border-border/);
  await expect(importConfirmationField).not.toHaveClass(/border-l-danger/);
  await expect(importConfirmationField).not.toHaveClass(/bg-danger-bg/);
  await expect(importPanel.getByText("未入力", { exact: true })).toHaveCount(1);
  const importButton = page.getByRole("button", { name: "取り込み実行" }).last();
  await expect(importButton).toBeDisabled();
  await page.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await expect(importConfirmationField.getByText("確認済み", { exact: true })).toBeVisible();
  await expect(importPanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  await expect(importButton).toBeEnabled();
  await importButton.click();
  await expect.poll(() => api.samplePayload?.confirmation).toBe("ADMIN_EXECUTE");
  await expect(page.getByTestId("sample-data-imported-count")).toHaveText("5");
  expect(api.samplePayload?.confirmation).toBe("ADMIN_EXECUTE");

  await page.getByRole("tab", { name: "削除実行" }).click();
  const deletePanel = page.locator("#sample-data-panel-delete");
  const deleteConfirmationField = deletePanel.getByTestId("execution-confirmation-field");
  await expectNeutralConfirmationFieldSurface(deleteConfirmationField);
  await expect(page.getByLabel("実行確認語")).toHaveValue("");
  await expect(page.getByRole("button", { name: "削除実行", exact: true })).toBeDisabled();
  await page.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await expect(deleteConfirmationField.getByText("確認済み", { exact: true })).toBeVisible();
  await expect(deletePanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  const deleteButton = page.getByRole("button", { name: "削除実行" }).last();
  await expect(deleteButton).toBeEnabled();
  await deleteButton.click();
  await expect.poll(() => api.samplePayload?.confirmation).toBe("ADMIN_EXECUTE");
  await expect(page.getByTestId("sample-data-imported-count")).toHaveText("0");
  await expectNoHorizontalScroll(page);
  await page.setViewportSize({ width: 375, height: 900 });
  await page.goto("/sample-data");
  await expectNoHorizontalScroll(page);
  await page.setViewportSize({ width: 1280, height: 720 });
  api.sampleImportError = true;
  await page.getByRole("tab", { name: "取り込み実行", exact: true }).click();
  await page.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await page.getByRole("button", { name: "取り込み実行" }).last().click();
  await expect(page.getByText("実行エラー").first()).toBeVisible();
  // 文の失敗は 3 製品共通の失敗のカード 1 枚（何が起きたか・DB の文・原因の説明・次の対応・「詳細」。#2342）。
  const statementFailure = page.getByTestId("db-admin-statement-failure").first();
  await expect(statementFailure.getByRole("alert")).toContainText("この文の実行に失敗しました。");
  await expect(statementFailure.locator("[data-process-failure-explanation]")).toBeVisible();
  await expect(statementFailure.locator("[data-process-failure-next]")).toBeVisible();
  await expect(page.getByText("ORA-00922: missing or invalid option", { exact: true })).toBeVisible();
  api.sampleImportError = false;

  const dataPreviewObjectRequests: Array<{
    ownerPrefix: string | null;
    exactOwner: string | null;
    queryScope: string | null;
  }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname !== "/api/nl2sql/db-admin/objects") return;
    dataPreviewObjectRequests.push({
      ownerPrefix: url.searchParams.get("owner_prefix"),
      exactOwner: url.searchParams.get("owner"),
      queryScope: url.searchParams.get("query_scope"),
    });
  });
  await page.goto("/data-management");
  const dataPreviewTab = page.getByRole("tab", { name: "テーブル・ビューデータの表示" });
  const dataCsvTab = page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)" });
  await expect(page.getByRole("tab")).toHaveCount(2);
  await expect(page.getByRole("tab", { name: "SQL 一括実行" })).toHaveCount(0);
  for (const label of ["INSERT(単一行)", "INSERT(複数行)", "UPDATE", "DELETE", "MERGE"]) {
    await expect(page.getByRole("button", { name: label, exact: true })).toHaveCount(0);
  }
  await expect(dataPreviewTab).toHaveAttribute("aria-selected", "true");
  await expect(dataCsvTab).toHaveAttribute("aria-selected", "false");
  await dataPreviewTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(dataCsvTab).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowLeft");
  await expect(dataPreviewTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { name: "対象テーブル/ビュー" })).toBeVisible();
  const dataPreviewPanel = page.locator("#data-management-panel-preview");
  await expect(page.getByText("Excel/CSV 取込(新規テーブル)", { exact: true })).toHaveCount(0);
  await expect(dataPreviewPanel.getByText("サンプルデータ管理", { exact: true })).toHaveCount(0);

  // 件数は一覧のツールバーの右の 1 か所（ListCount。#1891）。
  await expect(dataPreviewPanel.locator("[data-list-count]")).toHaveText("全 4 件");
  const previewSearch = dataPreviewPanel.getByRole("searchbox", { name: "検索" });
  await expect(previewSearch).toHaveAttribute("placeholder", "名前・コメントを入力");
  // 375px は検索・所有者・種別の 3 つをシート（「絞り込み」）にまとめる（#1933）。広い画面ではツールバーの中。
  const previewToolbar = page.getByTestId("data-management-preview-toolbar");
  await withListToolbarFilters(previewToolbar, async (filters) => {
    const previewOwnerFilter = filters.getByRole("searchbox", { name: "所有者" });
    await expect(previewOwnerFilter).toBeVisible();
    await expect(previewOwnerFilter).toHaveAttribute("placeholder", "所有者の先頭（例：ADM）");
    // 検索欄は lg、所有者は md の幅で並ぶ（#1891）。
    await expectDbObjectToolbarLayout(previewSearch, previewOwnerFilter);
    await expect(filters.getByRole("combobox", { name: "所有者" })).toHaveCount(0);
    if (filters === previewToolbar) {
      await previewSearch.focus();
      await page.keyboard.press("Tab");
      await expect(previewOwnerFilter).toBeFocused();
    }
    await expect
      .poll(() => dataPreviewObjectRequests.some((request) => request.queryScope === "name_comment"))
      .toBe(true);
    await previewOwnerFilter.fill("app");
    await expect(previewOwnerFilter).toHaveValue("APP");
    await expect
      .poll(() => dataPreviewObjectRequests.some((request) => request.ownerPrefix === "APP"))
      .toBe(true);
    expect(dataPreviewObjectRequests.some((request) => request.exactOwner !== null)).toBe(false);
    await previewOwnerFilter.clear();
    await expect(filters.getByRole("combobox", { name: "種別", exact: true })).toBeVisible();
  });
  await expect(dataPreviewPanel.getByLabel("行数フィルタ")).toHaveCount(0);
  // 一覧 + 詳細（#2315）: 対象を選ぶまで詳細（取得件数上限・表示結果）を出さず、ステップの表示も持たない。
  await expect(dataPreviewPanel.getByTestId("data-preview-steps")).toHaveCount(0);
  await expect(dataPreviewPanel.getByLabel("取得件数上限")).toHaveCount(0);
  const previewRowLimitInput = dataPreviewPanel.getByLabel("取得件数上限");
  await expect(dataPreviewPanel.getByLabel(/WHERE 条件/)).toHaveCount(0);
  await expect(dataPreviewPanel.getByText("選択中", { exact: true })).toHaveCount(0);
  await expect(dataPreviewPanel.getByText("統計未取得").filter({ visible: true })).toBeVisible();
  await expect(dataPreviewPanel.getByRole("button", { name: /^操作: / })).toHaveCount(0);
  await expect(dataPreviewPanel.getByRole("button", { name: / を選択/ })).toHaveCount(4);
  await expect(dataPreviewPanel.getByRole("button", { name: / のデータを表示$/ })).toHaveCount(0);
  const previewShowButton = dataPreviewPanel.getByRole("button", { name: "データを表示", exact: true });
  const previewResultsActions = dataPreviewPanel.getByTestId("data-preview-results-actions");
  const previewExportButton = previewResultsActions.getByRole("button", { name: "XLSX ダウンロード" });
  const previewMoreButton = previewResultsActions.getByRole("button", { name: "その他の操作" });
  // xl 未満（375px）は 1 ペイン。一覧を操作する前に、詳細から「一覧へ戻る」で戻る。
  const backToPreviewList = async () => {
    const back = page.getByTestId("data-management-detail-back");
    if (await back.isVisible()) await back.click();
  };
  api.previewDataPayload = null;
  await dataPreviewPanel.getByRole("button", { name: "APP.INVOICES を選択" }).click();
  await expect(page).toHaveURL(/[?&]object=APP\.INVOICES/);
  await expect(dataPreviewPanel.getByRole("button", { name: "APP.INVOICES を選択" })).toHaveAttribute("aria-current", "true");
  // 行を選ぶと今の取得件数上限（既定 100）でデータを表示する。
  await expect.poll(() => currentPreviewDataPayload()?.object_name).toBe("INVOICES");
  await expect(previewRowLimitInput).toHaveValue("100");
  await expect(dataPreviewPanel.getByText("1〜100000 の整数。取得上限を明示してください。")).toBeVisible();
  await expect(dataPreviewPanel.getByTestId("query-results-summary")).toContainText("100 行・");
  // 取得のたびに結果を置き換えるので、取得件数上限と結果を既定に戻すボタンは持たない（#2330）。
  await expect(dataPreviewPanel.getByRole("button", { name: "表示件数・結果をリセット", exact: true })).toHaveCount(0);
  await previewRowLimitInput.fill("-1");
  await expect(dataPreviewPanel.getByText("取得件数上限は 1 以上 100000 以下の整数を入力してください。")).toBeVisible();
  await expect(previewShowButton).toBeDisabled();
  await expect(previewExportButton).toBeDisabled();
  await expect(dataPreviewPanel.getByRole("button", { name: "APP.INVOICES を選択" })).toBeEnabled();
  await previewRowLimitInput.fill("100");
  await expect(dataPreviewPanel.getByText("取得件数上限は 1 以上 100000 以下の整数を入力してください。")).toHaveCount(0);
  await previewShowButton.click();
  await expect(previewExportButton).toBeVisible();
  await expect(previewMoreButton).toBeVisible();
  await expect(dataPreviewPanel.getByTestId("query-results-summary")).toContainText("100 行・");
  await expect(dataPreviewPanel.getByTestId("query-results-meta")).toContainText("取得上限 100 件");
  // 表示結果は共通の結果の表（#1178）: 先頭 50 行のプレビューを表の中のスクロールで出し、すべての行はシートで送る。
  await expect(dataPreviewPanel.getByTestId("query-results-table").locator("tbody tr")).toHaveCount(50);
  await expect(dataPreviewPanel.getByTestId("query-results-preview-note")).toContainText("先頭の 50 行");
  await expect(dataPreviewPanel.getByTestId("query-results-csv")).toBeVisible();
  await dataPreviewPanel.getByTestId("query-results-view-all").click();
  const previewSheet = page.getByRole("dialog", { name: "表示結果（100 行）" });
  await expect(previewSheet).toBeVisible();
  const previewSheetPagination = page.getByTestId("query-results-all-pagination");
  await expect(previewSheetPagination).toContainText("1–25 / 100 件");
  await expect(previewSheet.getByRole("cell", { name: "顧客26" })).toHaveCount(0);
  await previewSheetPagination.getByRole("button", { name: "次へ" }).click();
  await expect(previewSheet.getByRole("cell", { name: "顧客26" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(previewSheet).toBeHidden();
  await expect(dataPreviewPanel.getByRole("button", { name: "APP.INVOICES を選択" })).toHaveAttribute("aria-current", "true");
  await expect(previewShowButton).toBeEnabled();
  await expectBoundedRowLimit(previewRowLimitInput, previewShowButton);
  api.previewDataPayload = null;
  await previewShowButton.click();
  await expect.poll(() => currentPreviewDataPayload()?.object_name).toBe("INVOICES");
  expect(currentPreviewDataPayload()?.owner).toBe("APP");
  expect(currentPreviewDataPayload()?.limit).toBe(100);
  expect(currentPreviewDataPayload()?.where_clause).toBe("");
  // runtime は API の内部値（deterministic）を出さず、文言で出す（#936）。
  await expect(dataPreviewPanel.getByText("決定論", { exact: true })).toBeVisible();
  await expect(dataPreviewPanel.getByText("deterministic", { exact: true })).toHaveCount(0);
  const tableDownloadPromise = page.waitForEvent("download");
  await previewExportButton.click();
  const tableDownload = await tableDownloadPromise;
  expect(tableDownload.suggestedFilename()).toBe("app_invoices_preview.xlsx");
  await expect.poll(() => api.previewDataExportPayload?.object_name).toBe("INVOICES");
  expect(api.previewDataExportPayload?.owner).toBe("APP");
  expect(api.previewDataExportPayload?.limit).toBe(100);
  expect(api.previewDataExportPayload?.where_clause).toBe("");
  const [exportBox, moreBox] = await Promise.all([
    previewExportButton.boundingBox(),
    previewMoreButton.boundingBox(),
  ]);
  expect(exportBox).not.toBeNull();
  expect(moreBox).not.toBeNull();
  if (Math.abs(moreBox!.y - exportBox!.y) <= 2) {
    expect(moreBox!.x).toBeGreaterThan(exportBox!.x);
  } else {
    expect(moreBox!.y).toBeGreaterThan(exportBox!.y);
  }
  await clickObjectDetailAction(page, "data-preview-results-actions", "APP.INVOICES のデータを空にする");
  const truncateDialog = page.getByRole("dialog", { name: "TRUNCATE TABLE の確認" });
  await expect(truncateDialog).toBeVisible();
  await mainScroller(page).evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
  await expectSharedScrimOverlayCoversViewport(page);
  await expectOnlyNeutralConfirmationFieldInDialog(truncateDialog);
  const truncateButton = truncateDialog.getByRole("button", { name: "データを空にする" });
  await expect(truncateButton).toBeDisabled();
  await truncateDialog.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await expect(truncateButton).toBeDisabled();
  await truncateDialog.getByLabel("実行確認語").fill("APP.INVOICES");
  await truncateButton.click();
  await expect.poll(() => api.truncateTablePayload?.confirmation).toBe("APP.INVOICES");
  expect(api.truncateTablePayload?.table_name).toBe("INVOICES");
  expect(api.truncateTablePayload?.owner).toBe("APP");
  expect(api.truncateTablePayload?.reason).toBe("ui-data-management-truncate");
  await expect(page.getByRole("dialog", { name: "TRUNCATE TABLE の確認" })).toHaveCount(0);

  await backToPreviewList();
  await previewSearch.fill("EMP");
  await expect(dataPreviewPanel.getByRole("button", { name: "APP.V_EMP_DEPT を選択" })).toBeVisible();
  await expect(dataPreviewPanel.getByRole("button", { name: "APP.INVOICES を選択" })).toHaveCount(0);
  await previewSearch.fill("ZZZ");
  await expect(dataPreviewPanel.getByText("条件に一致する対象がありません")).toBeVisible();
  await previewSearch.fill("");
  await withListToolbarFilters(previewToolbar, async (filters) => {
    const previewOwnerFilter = filters.getByRole("searchbox", { name: "所有者" });
    await previewOwnerFilter.fill("ap");
    await expect(previewOwnerFilter).toHaveValue("AP");
    await expect.poll(() => dbAdminObjectOwnerPrefixRequests.includes("AP")).toBe(true);
    expect(dbAdminObjectExactOwnerRequests).toEqual([]);
    await expect(dataPreviewPanel.getByRole("button", { name: "APP.INVOICES を選択" })).toBeVisible();
    await previewOwnerFilter.fill("ZZZ");
    await expect(dataPreviewPanel.getByText("条件に一致する対象がありません")).toBeVisible();
    await previewOwnerFilter.fill("");
    await expect(dataPreviewPanel.getByRole("button", { name: "APP.INVOICES を選択" })).toBeVisible();
    await chooseSelectFieldOption(filters.getByRole("combobox", { name: "種別", exact: true }), "view");
  });
  await expect(dataPreviewPanel.getByRole("button", { name: "APP.V_EMP_DEPT を選択" })).toBeVisible();
  await expect(dataPreviewPanel.getByRole("button", { name: "APP.INVOICES を選択" })).toHaveCount(0);
  const viewSelectTarget = dataPreviewPanel.getByRole("button", { name: "APP.V_EMP_DEPT を選択" });
  await expect(viewSelectTarget).toBeEnabled();
  await viewSelectTarget.focus();
  await page.keyboard.press("Enter");
  await expect(viewSelectTarget).toHaveAttribute("aria-current", "true");
  await expect.poll(() => currentPreviewDataPayload()?.object_name).toBe("V_EMP_DEPT");
  // ビューにはデータを空にする操作を出さない。
  await expect(dataPreviewPanel.getByRole("button", { name: "APP.V_EMP_DEPT のデータを空にする" })).toHaveCount(0);
  await expect(dataPreviewPanel.getByRole("button", { name: "データを空にする", exact: true })).toHaveCount(0);
  await expect(previewResultsActions.getByRole("button", { name: "その他の操作" })).toHaveCount(0);
  await previewRowLimitInput.fill("25");
  await expect(dataPreviewPanel.getByText("データ未表示")).toBeVisible();
  await expect(previewShowButton).toBeEnabled();

  api.previewDataPayload = null;
  await previewShowButton.click();
  await expect(page.getByRole("cell", { name: "顧客01" })).toBeVisible();
  await expect(dataPreviewPanel.getByTestId("query-results-meta")).toContainText("取得上限 25 件");
  await expect(dataPreviewPanel.getByTestId("query-results-summary")).toContainText("25 行・");
  // 50 行以下はプレビューにすべての行を出す（補足の文は出さない）。
  await expect(dataPreviewPanel.getByTestId("query-results-table").locator("tbody tr")).toHaveCount(25);
  await expect(dataPreviewPanel.getByTestId("query-results-preview-note")).toHaveCount(0);
  await expect.poll(() => currentPreviewDataPayload()?.object_name).toBe("V_EMP_DEPT");
  expect(currentPreviewDataPayload()?.owner).toBe("APP");
  expect(currentPreviewDataPayload()?.limit).toBe(25);
  expect(currentPreviewDataPayload()?.where_clause).toBe("");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "XLSX ダウンロード" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("app_v_emp_dept_preview.xlsx");
  await expect.poll(() => api.previewDataExportPayload?.object_name).toBe("V_EMP_DEPT");
  expect(api.previewDataExportPayload?.owner).toBe("APP");
  expect(api.previewDataExportPayload?.limit).toBe(25);
  expect(api.previewDataExportPayload?.where_clause).toBe("");
  await expectNoHorizontalScroll(page);

  await dataCsvTab.click();
  await expect(dataCsvTab).toHaveAttribute("aria-selected", "true");
  const csvPanel = page.locator("#data-management-panel-csv");
  await expect(csvPanel).toBeVisible();
  await expectCsvUploadLayout(csvPanel);
  const csvTableSearch = csvPanel.getByRole("searchbox", { name: "検索" });
  await expect(csvPanel.getByTestId("data-csv-table-list").getByText("APP.INVOICES", { exact: true })).toBeVisible();
  await expect(csvPanel.getByTestId("data-csv-table-list").getByText("APP.AUDIT_LOG", { exact: true })).toHaveCount(0);
  await csvTableSearch.fill("PAY");
  await expect(csvPanel.getByTestId("data-csv-table-list").getByText("APP.PAYMENTS", { exact: true })).toBeVisible();
  await csvPanel.getByRole("button", { name: "APP.PAYMENTS を選択" }).click();
  await expect(csvPanel.getByText("選択中")).toBeVisible();
  await expect(csvPanel.getByText("APP.PAYMENTS", { exact: true }).first()).toBeVisible();
  await csvTableSearch.clear();
  await expect(csvPanel.getByTestId("data-csv-table-list").getByText("APP.INVOICES", { exact: true })).toBeVisible();
  const csvConfirmationInput = csvPanel.getByLabel("実行確認語");
  const csvUploadButton = csvPanel.getByRole("button", { name: "アップロード実行" });
  await csvConfirmationInput.fill("APP.PAYMENTS");
  await expect(csvPanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  const csvUploadClearButton = csvPanel
    .getByTestId("execution-confirmation-field")
    .getByRole("button", { name: "取込設定・結果をリセット", exact: true });
  await expect(csvUploadClearButton).toBeEnabled();
  await expectButtonsSameHeight(csvUploadButton, csvUploadClearButton);
  await csvUploadClearButton.click();
  await expect(csvConfirmationInput).toHaveValue("");
  await expect(csvPanel.getByText("APP.PAYMENTS", { exact: true }).first()).toBeVisible();
  await expect(csvUploadClearButton).toBeDisabled();
  await csvConfirmationInput.fill("APP.PAYMENTS");
  await expect(csvPanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  await csvPanel.getByTestId("data-csv-table-footer").getByRole("button", { name: "さらに読み込む" }).click();
  await expect(csvPanel.getByTestId("data-csv-table-list").getByText("APP.AUDIT_LOG", { exact: true })).toBeVisible();
  await expect(csvPanel.getByText("APP.PAYMENTS", { exact: true }).first()).toBeVisible();
  await expect(csvPanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  const dataTabularInput = page.getByTestId("data-csv-file-field-input");
  await expect(dataTabularInput).toHaveAttribute("accept", ".csv,.xlsx,.xls");
  await expect(page.getByText(".CSV / .XLSX / .XLS", { exact: true })).toBeVisible();
  await dropFiles(page, page.getByTestId("data-csv-file-field-dropzone"), [
    {
      name: "invoices.XLS",
      type: "application/vnd.ms-excel",
      content: "legacy-xls",
    },
  ]);
  await expect(page.getByText("選択中: invoices.XLS")).toBeVisible();
  await expect(csvConfirmationInput).toHaveValue("");
  await csvConfirmationInput.fill("APP.PAYMENTS");
  await expect(csvUploadButton).toBeEnabled();
  await csvUploadButton.click();
  await expect(page.getByText("UNKNOWN_COLUMN", { exact: false }).first()).toBeVisible();
  await expect.poll(() => api.csvUploadPayload?.table_name).toBe("PAYMENTS");
  expect(api.csvUploadPayload?.owner).toBe("APP");
  expect(api.csvUploadPayload?.mode).toBe("insert");
  expect(api.csvUploadPayload?.confirmation).toBe("APP.PAYMENTS");
  expect(api.csvUploadPayload?.filename).toBe("invoices.XLS");
  await expect(csvUploadClearButton).toBeEnabled();
  await csvUploadClearButton.click();
  await expect(csvConfirmationInput).toHaveValue("");
  await expect(page.getByText("選択中: invoices.XLS")).toHaveCount(0);
  await expect(page.getByText("UNKNOWN_COLUMN", { exact: false })).toHaveCount(0);
  await expect(csvPanel.getByRole("button", { name: "アップロード実行" })).toBeDisabled();

  await expectNoHorizontalScroll(page);
});

test("sample data refresh replaces the whole workspace with the shared skeleton", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockNl2SqlApi(page);
  const refreshGate = createRequestGate();
  let holdNextRefresh = false;
  let markRefreshStarted: () => void = () => undefined;
  const refreshStarted = new Promise<void>((resolve) => {
    markRefreshStarted = resolve;
  });
  const sampleDataResponse = {
    runtime: "deterministic",
    profile_id: "",
    confirmation: "ADMIN_EXECUTE",
    objects: ["DEPARTMENT", "EMPLOYEE", "PROJECT"],
    imported_objects: ["DEPARTMENT"],
    sql: {
      tables: ["CREATE TABLE DEPARTMENT (DEPARTMENT_ID NUMBER PRIMARY KEY)"],
      views: ["CREATE OR REPLACE VIEW V_EMP_DEPT AS SELECT 1 AS ID FROM DUAL"],
      data: ["INSERT INTO DEPARTMENT (DEPARTMENT_ID) VALUES (10)"],
      delete: ["DROP TABLE DEPARTMENT PURGE"],
    },
    warnings: [],
  };

  await page.route("**/api/nl2sql/sample-data", async (route) => {
    if (holdNextRefresh) {
      holdNextRefresh = false;
      markRefreshStarted();
      await refreshGate.promise;
    }
    await fulfillJson(route, sampleDataResponse);
  });

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/sample-data");
  const panel = page.locator("#sample-data-panel-import");
  await expect(page.getByRole("heading", { name: "サンプルデータ管理" })).toBeVisible();
  await expect(panel.getByText("対象オブジェクト", { exact: true })).toBeVisible();
  await expect(panel.getByText("SQL プレビュー", { exact: true })).toBeVisible();

  holdNextRefresh = true;
  await clickPageHeaderAction(page, "sample-data-actions", "最新情報を取得");
  await refreshStarted;

  const skeleton = page.getByTestId("sample-data-workspace-refresh-skeleton");
  await expect(skeleton).toBeVisible();
  await expect(skeleton).toHaveAttribute("aria-busy", "true");
  await expect(skeleton).toHaveAttribute("data-processing-placement", "workspace");
  await expect(skeleton).toContainText("最新情報を取得しています");
  await expect(skeleton.getByTestId("sample-data-workspace-refresh-skeleton-processing")).toHaveAttribute(
    "data-processing-activity-icon",
    "none"
  );
  await expect(skeleton.getByTestId("db-management-skeleton-block")).toHaveCount(3);
  await expect(skeleton.getByTestId("db-management-skeleton-block").first()).toHaveCSS(
    "animation-name",
    "none"
  );
  await expect(panel.getByText("対象オブジェクト", { exact: true })).toHaveCount(0);
  await expect(panel.getByText("SQL プレビュー", { exact: true })).toHaveCount(0);
  await expect(panel.getByText("DEPARTMENT", { exact: true })).toHaveCount(0);
  await expectNoHorizontalScroll(page);

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(skeleton).toBeVisible();
  await expectNoHorizontalScroll(page);

  refreshGate.release();
  await expect(skeleton).toHaveCount(0);
  await expect(panel.getByText("対象オブジェクト", { exact: true })).toBeVisible();
  await expect(panel.getByText("SQL プレビュー", { exact: true })).toBeVisible();
});

test("SampleData schema refresh recovery disables CTA while workspace processing is visible", async ({ page }) => {
  await mockNl2SqlApi(page);
  let manualRefreshStarted = 0;
  let manualRefreshPolls = 0;
  const manualRefreshGate = createRequestGate();

  await page.unroute("**/api/nl2sql/sample-data/import");
  await page.route("**/api/nl2sql/sample-data/import", (route) =>
    fulfillJson(route, {
      operation: "import",
      step: "all",
      runtime: "oracle",
      executed: false,
      objects: [],
      statements: [],
      warnings: [],
      profile_id: "",
      schema_refresh_required: true,
      schema_refresh_reason_code: "schema_refresh_full_required",
      timing,
    })
  );
  await page.route("**/api/schema/refresh-jobs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    manualRefreshStarted += 1;
    await fulfillJson(route, {
      job_id: "sample-data-manual-full-refresh",
      status: "pending",
      mode: "full",
      source: "manual",
      target_objects: [],
      requires_full_refresh: false,
      phase: "queued",
      created_at: "2026-07-22T00:00:01.000Z",
      scanned_objects: 0,
      changed_objects: 0,
      deleted_objects: 0,
      catalog_version: null,
      error_code: "",
    });
  });
  await page.route("**/api/schema/refresh-jobs/sample-data-manual-full-refresh", async (route) => {
    manualRefreshPolls += 1;
    if (manualRefreshPolls === 1) {
      await fulfillJson(route, {
        job_id: "sample-data-manual-full-refresh",
        status: "running",
        mode: "full",
        source: "manual",
        target_objects: [],
        requires_full_refresh: false,
        phase: "fetching",
        created_at: "2026-07-22T00:00:01.000Z",
        scanned_objects: 1,
        changed_objects: 0,
        deleted_objects: 0,
        catalog_version: null,
        error_code: "",
      });
      return;
    }
    await manualRefreshGate.promise;
    await fulfillJson(route, {
      job_id: "sample-data-manual-full-refresh",
      status: "done",
      mode: "full",
      source: "manual",
      target_objects: [],
      requires_full_refresh: false,
      phase: "done",
      created_at: "2026-07-22T00:00:01.000Z",
      scanned_objects: 2,
      changed_objects: 1,
      deleted_objects: 0,
      catalog_version: 2,
      error_code: "",
    });
  });

  await page.goto("/sample-data");
  await page.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await page.getByRole("button", { name: "取り込み実行" }).last().click();

  await expect(page.getByText("DB 構造の差分同期で不整合を検出しました。")).toBeVisible();
  const refreshButton = page.getByRole("button", { name: "DB 構造を再取得" });
  await expect(refreshButton).toBeVisible();
  await refreshButton.click();
  await expect.poll(() => manualRefreshStarted).toBe(1);
  await expect(refreshButton).toBeDisabled();
  const processing = page.getByTestId("sample-data-workspace-processing");
  await expect(processing).toContainText("DB 構造を再取得しています");
  // job の間はボタンを回さず無効にするだけ。スピナーは作業領域の進行の表示の 1 つだけ（#821）。
  await expect(refreshButton).not.toHaveAttribute("aria-busy", "true");
  await expectSingleSpinner(page, processing);
  await expectNoHorizontalScroll(page);

  manualRefreshGate.release();
  await expect.poll(() => manualRefreshPolls).toBeGreaterThanOrEqual(2);
  await expect(processing).toHaveCount(0);
  await expect(page.getByText("DB 構造を再取得しました。")).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test("DB 構造再取得は旧30秒上限を超えても三つの管理画面で追跡を続ける", async ({
  page,
}) => {
  await page.unroute("**/api/auth/me").catch(() => undefined);
  await page.route("**/api/auth/me**", (route) => fulfillJson(route, systemAdminMe));
  await page.route("**/api/auth/login**", (route) => fulfillJson(route, systemAdminMe));
  await mockNl2SqlApi(page);

  let currentJobId = "";
  let completed = false;
  let failNextPoll = false;
  let submitted = 0;
  let objectRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/nl2sql/db-admin/objects") {
      objectRequests += 1;
    }
  });
  await page.route("**/api/schema/refresh-jobs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    submitted += 1;
    await fulfillJson(route, {
      job_id: currentJobId,
      status: "pending",
      mode: "full",
      source: "manual",
      target_objects: [],
      requires_full_refresh: false,
      phase: "queued",
      created_at: "2026-08-29T00:00:00.000Z",
      scanned_objects: 0,
      changed_objects: 0,
      deleted_objects: 0,
      catalog_version: null,
      error_code: "",
    });
  });
  await page.route("**/api/schema/refresh-jobs/schema-refresh-long-*", async (route) => {
    const jobId = new URL(route.request().url()).pathname.split("/").at(-1) ?? currentJobId;
    if (failNextPoll) {
      failNextPoll = false;
      await route.fulfill({ status: 504, body: "temporary poll failure" });
      return;
    }
    await fulfillJson(route, {
      job_id: jobId,
      status: completed ? "done" : "running",
      mode: "full",
      source: "manual",
      target_objects: [],
      requires_full_refresh: false,
      phase: completed ? "done" : "persisting",
      created_at: "2026-08-29T00:00:00.000Z",
      started_at: "2026-08-29T00:00:01.000Z",
      finished_at: completed ? "2026-08-29T00:00:32.000Z" : null,
      processed_objects: 218,
      total_objects: 218,
      scanned_objects: completed ? 218 : 0,
      changed_objects: completed ? 3 : 0,
      deleted_objects: 0,
      catalog_version: completed ? 2 : null,
      error_code: "",
    });
  });

  const targets = [
    {
      id: "table",
      path: "/table-management",
      ready: "table-management-grid",
      processing: "table-management-workspace-processing",
    },
    {
      id: "view",
      path: "/view-management",
      ready: "view-management-grid",
      processing: "view-management-workspace-processing",
    },
    {
      id: "metadata",
      path: "/comment-management",
      ready: "comment-management-steps",
      processing: "comment-management-workspace-processing",
    },
  ] as const;

  for (const [index, target] of targets.entries()) {
    currentJobId = `schema-refresh-long-${target.id}`;
    completed = false;
    if (index === 0) {
      await page.goto(target.path);
      const loginHeading = page.getByRole("heading", { name: "システムにログイン" });
      if (await loginHeading.isVisible({ timeout: 1_000 }).catch(() => false)) {
        await page.getByLabel("ログインユーザーID").fill("SYSTEM");
        await page.getByLabel("パスワード").fill("password");
        await page.getByRole("button", { name: "ログイン", exact: true }).click();
      }
    } else {
      await page.evaluate((path) => {
        window.history.pushState({}, "", path);
        window.dispatchEvent(new PopStateEvent("popstate"));
      }, target.path);
    }
    await expect(page.getByTestId(target.ready)).toBeVisible();
    if (index === 0) {
      await page.waitForLoadState("networkidle");
      await page.clock.install({ time: new Date("2026-08-29T00:00:00.000Z") });
    }

    const header = page.locator("header");
    const directAction = header.getByRole("button", {
      name: "DB 構造を再取得",
      exact: true,
    });
    if (await directAction.isVisible()) {
      await directAction.click();
    } else {
      await header.getByRole("button", { name: "その他の操作" }).click();
      await page.getByRole("menuitem", { name: "DB 構造を再取得", exact: true }).click();
    }
    await expect.poll(() => submitted).toBe(index + 1);

    const processing = page.getByTestId(target.processing);
    await expect(processing).toBeVisible();
    await expect(processing).toContainText("DB 構造を再取得しています");
    await expect(page.getByText("DB 構造再取得: 保存中 218/218", { exact: true }).filter({ visible: true })).toBeVisible();

    await page.clock.fastForward(31_000);
    await expect(processing).toBeVisible();
    await expect(processing).toContainText("通常より時間がかかっています");
    await expect(
      page.getByText("DB 構造の再取得に時間がかかっています。完了後に一覧を自動更新します。", {
        exact: false,
      }),
    ).toHaveCount(0);

    if (await directAction.isVisible()) {
      await expect(directAction).toBeDisabled();
    } else {
      await header.getByRole("button", { name: "その他の操作" }).click();
      await expect(
        page.getByRole("menuitem", { name: "DB 構造を再取得", exact: true }),
      ).toBeDisabled();
      await page.keyboard.press("Escape");
    }
    await expectNoHorizontalScroll(page);

    failNextPoll = true;
    await page.clock.fastForward(1_100);
    await expect(processing).toBeVisible();
    await expect(page.getByText("temporary poll failure", { exact: false })).toHaveCount(0);

    const objectRequestsBeforeCompletion = objectRequests;
    completed = true;
    await page.clock.fastForward(1_100);
    await expect(processing).toHaveCount(0);
    await expect(page.getByText("DB 構造を再取得しました。", { exact: true })).toHaveCount(1);
    await expect.poll(() => objectRequests).toBeGreaterThan(objectRequestsBeforeCompletion);
    await page.clock.fastForward(5_000);
  }
});

test("実行中 DB 構造再取得を全10ルートと再読込後に復元して完了時に一度だけ通知する", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await mockNl2SqlApi(page);
  await page.unroute("**/api/schema/refresh-jobs/active");

  const jobId = "schema-refresh-route-recovery";
  const startedAt = new Date(Date.now() - 12_000).toISOString();
  let completed = false;
  const responseJob = () => ({
    job_id: jobId,
    status: completed ? "done" : "running",
    mode: "full",
    source: "manual",
    target_objects: [],
    requires_full_refresh: false,
    phase: completed ? "done" : "persisting",
    created_at: startedAt,
    started_at: startedAt,
    finished_at: completed ? new Date().toISOString() : null,
    processed_objects: 218,
    total_objects: 218,
    scanned_objects: completed ? 218 : 0,
    changed_objects: completed ? 3 : 0,
    deleted_objects: 0,
    catalog_version: completed ? 2 : null,
    error_code: "",
  });

  await page.route("**/api/schema/refresh-jobs/active", (route) =>
    fulfillJson(route, { active_job: completed ? null : responseJob() })
  );
  await page.route(`**/api/schema/refresh-jobs/${jobId}`, (route) =>
    fulfillJson(route, responseJob())
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-view", (route) =>
    fulfillJson(route, {
      profile_ontology_view: null,
      ontology_graph: { nodes: [], edges: [] },
      materialized: false,
      stale: false,
      warnings_ja: [],
    })
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-build-jobs**", (route) =>
    fulfillJson(route, { jobs: [] })
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-source-documents**", (route) =>
    fulfillJson(route, { source_documents: [] })
  );
  await page.route("**/api/nl2sql/profiles/*/ontology-markdown", (route) =>
    fulfillJson(route, {
      draft_markdown: "",
      published_markdown: "",
      draft_revision: null,
      published_revision: null,
      draft_etag: "",
      published_at: null,
    })
  );

  const targets = [
    ["/query", "query-schema-refresh-status", "schema-reference-refreshing", true],
    ["/table-management", "table-schema-refresh-status", "table-management-workspace-processing", true],
    ["/view-management", "view-schema-refresh-status", "view-management-workspace-processing", true],
    ["/data-management", "data-management-schema-refresh-status", "data-management-workspace-processing", true],
    ["/sample-data", "sample-data-schema-refresh-status", "sample-data-workspace-processing", false],
    ["/comment-management", "comment-management-schema-refresh-status", "comment-management-workspace-processing", true],
    ["/annotation-management", "annotation-management-schema-refresh-status", "annotation-management-workspace-processing", true],
    ["/admin-sql", "admin-sql-schema-refresh-status", "admin-sql-schema-refresh-processing", false],
    ["/profiles", "profile-management-schema-refresh-status", "profile-management-workspace-processing", true],
    ["/ontology-build", "ontology-build-schema-refresh-status", "ontology-build-schema-refresh-processing", false],
  ] as const;

  for (const [index, [path, statusTestId, processingTestId, hasHeaderRefreshAction]] of targets.entries()) {
    if (index === 0) {
      await page.goto(path);
    } else {
      await page.evaluate((nextPath) => {
        window.history.pushState({}, "", nextPath);
        window.dispatchEvent(new PopStateEvent("popstate"));
      }, path);
    }
    const status = page.getByTestId(statusTestId);
    await expect(status).toContainText("DB 構造再取得: 保存中 218/218");
    const processing = page.getByTestId(processingTestId);
    await expect(processing).toContainText("DB 構造を再取得しています");
    await expect(processing).toContainText("通常より時間がかかっています");
    await expect(processing.getByRole("timer")).toHaveAttribute("aria-live", "off");

    if (hasHeaderRefreshAction) {
      const header = page.locator("header").filter({ has: status }).first();
      const directAction = header.getByRole("button", {
        name: "DB 構造を再取得",
        exact: true,
      });
      if (await directAction.isVisible()) {
        await expect(directAction).toBeDisabled();
      } else {
        await header.getByRole("button", { name: "その他の操作" }).click();
        await expect(
          page.getByRole("menuitem", { name: "DB 構造を再取得", exact: true }),
        ).toBeDisabled();
        await page.keyboard.press("Escape");
      }
    }
    await expectNoHorizontalScroll(page);
  }

  await page.evaluate(() => {
    window.history.pushState({}, "", "/query");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.getByTestId("query-schema-refresh-status")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("query-schema-refresh-status")).toContainText(
    "DB 構造再取得: 保存中 218/218",
  );
  await expect(page.getByTestId("schema-reference-refreshing")).toBeVisible();

  completed = true;
  await expect(page.getByTestId("query-schema-refresh-status")).toHaveCount(0);
  await expect(page.getByTestId("schema-reference-refreshing")).toHaveCount(0);
  await expect(page.getByText("DB 構造を再取得しました。", { exact: true })).toHaveCount(1);
  await expectNoHorizontalScroll(page);
});

test("data management avoids full catalog and tracks schema refresh jobs", async ({ page }) => {
  await mockNl2SqlApi(page);
  let catalogRequests = 0;
  let jobPolls = 0;
  let submitErrorJob = false;
  page.on("request", (request) => {
    const url = request.url();
    if (url.endsWith("/api/schema/catalog")) catalogRequests += 1;
  });
  await page.unroute("**/api/schema/catalog");
  await page.route("**/api/schema/catalog", (route) => {
    catalogRequests += 1;
    return route.fulfill({ status: 500, body: "full catalog must not be called" });
  });
  await page.route("**/api/schema/refresh-jobs", (route) =>
    fulfillJson(route, {
      job_id: submitErrorJob ? "data-schema-job-error" : "data-schema-job",
      status: "pending",
      phase: "queued",
      created_at: "2026-07-22T00:00:00.000Z",
      scanned_objects: 0,
      changed_objects: 0,
      deleted_objects: 0,
      catalog_version: null,
      error_code: "",
    })
  );
  await page.route("**/api/schema/refresh-jobs/data-schema-job", (route) => {
    jobPolls += 1;
    const done = jobPolls >= 2;
    return fulfillJson(route, {
      job_id: "data-schema-job",
      status: done ? "done" : "running",
      phase: done ? "done" : "fetching",
      processed_objects: done ? 4 : 2,
      total_objects: 4,
      created_at: "2026-07-22T00:00:00.000Z",
      scanned_objects: done ? 4 : 0,
      changed_objects: done ? 1 : 0,
      deleted_objects: 0,
      catalog_version: done ? 2 : null,
      error_code: "",
    });
  });
  await page.route("**/api/schema/refresh-jobs/data-schema-job-error", (route) =>
    fulfillJson(route, {
      job_id: "data-schema-job-error",
      status: "error",
      phase: "fetching",
      created_at: "2026-07-22T00:00:00.000Z",
      scanned_objects: 0,
      changed_objects: 0,
      deleted_objects: 0,
      catalog_version: null,
      error_code: "schema_refresh_failed",
    })
  );

  await page.goto("/data-management");
  await expect(page.getByTestId("data-management-preview-grid")).toBeVisible();
  expect(catalogRequests).toBe(0);

  await clickPageHeaderAction(
    page,
    "data-management-actions",
    "最新情報を取得"
  );
  await expect(page.getByText("最新の状態に更新しました。")).toBeVisible();
  await expect(page.getByTestId("data-management-preview-grid")).toBeVisible();
  expect(catalogRequests).toBe(0);

  await clickPageHeaderAction(
    page,
    "data-management-actions",
    "DB 構造を再取得"
  );
  await expect(page.getByText("DB 構造を再取得しました。")).toBeVisible();
  expect(jobPolls).toBeGreaterThanOrEqual(2);
  expect(catalogRequests).toBe(0);

  submitErrorJob = true;
  // 375px では重なった通知がページ上部の操作に掛かり、ポインターが通知の上にあると消えるのを止める。
  // ポインターを通知の外へ動かし、最初の通知が消えてから押す（通知の表示時間に依存しない）。
  await page.mouse.move(1, page.viewportSize()!.height - 1);
  await expect(page.getByText("最新の状態に更新しました。")).toHaveCount(0);
  await clickPageHeaderAction(
    page,
    "data-management-actions",
    "DB 構造を再取得"
  );
  await expect(
    mainScroller(page).getByText("DB 構造の再取得に失敗しました。再試行してください。", {
      exact: false,
    }),
  ).toBeVisible();

  await expectNoHorizontalScroll(page);
});

test("data management keeps loaded object rows when load more times out and retries in place", async ({ page }) => {
  await page.addInitScript(() => {
    const nativeTimeout = AbortSignal.timeout.bind(AbortSignal);
    let shortenTimeouts = false;
    window.addEventListener("test-enable-short-timeout", () => { shortenTimeouts = true; });
    Object.defineProperty(AbortSignal, "timeout", {
      configurable: true,
      value: (delay: number) => nativeTimeout(shortenTimeouts && delay === 60_000 ? 150 : delay),
    });
  });
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/db-admin/objects?*");
  const timeoutGate = createRequestGate();
  let cursorAttempts = 0;
  let allowSuccess = false;
  await page.route("**/api/nl2sql/db-admin/objects?*", async (route) => {
    const url = new URL(route.request().url());
    const cursor = url.searchParams.get("cursor");
    if (cursor) {
      expect(url.searchParams.get("include_counts")).toBe("false");
      cursorAttempts += 1;
      if (!allowSuccess) {
        await timeoutGate.promise;
      }
      try {
        await fulfillJson(route, {
          runtime: "deterministic",
          owner: "APP",
          items: [
            { name: "PAYMENTS", owner: "APP", object_type: "table", row_count: 3, comment: "入金情報" },
          ],
          total: 0,
          table_count: 0,
          view_count: 0,
          counts_included: false,
          next_cursor: null,
          refreshed_at: schemaCatalog.refreshed_at,
          catalog_version: 0,
          warnings: [],
        });
      } catch {
        // timeout で browser が最初の cursor request を破棄した場合は retry request を待つ。
      }
      return;
    }
    await fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items: [
        { name: "INVOICES", owner: "APP", object_type: "table", row_count: 2, comment: "請求情報" },
      ],
      total: 2,
      table_count: 2,
      view_count: 0,
      counts_included: true,
      next_cursor: "page-2",
      refreshed_at: schemaCatalog.refreshed_at,
      catalog_version: 1,
      warnings: [],
    });
  });

  await page.goto("/data-management");
  await expect(page.getByRole("button", { name: "APP.INVOICES を選択" })).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("test-enable-short-timeout")));
  await page.getByTestId("data-management-preview-footer").getByRole("button", { name: "さらに読み込む" }).click();
  await expect(page.getByRole("button", { name: "APP.INVOICES を選択" })).toBeVisible();
  await expect(page.getByText(/追加読み込みが60秒以内に完了しませんでした/)).toBeVisible();
  allowSuccess = true;
  timeoutGate.release();
  const retry = page.getByTestId("data-management-preview-footer").getByRole("button", { name: "再試行" });
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "APP.PAYMENTS を選択" })).toBeVisible();
  expect(cursorAttempts).toBe(2);
  await expectNoHorizontalScroll(page);
});

test("system objects stay hidden across database object management pages", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/schema/objects?*");
  await page.route("**/api/schema/objects?*", (route) => {
    const objectType = new URL(route.request().url()).searchParams.get("type") ?? "";
    const allItems = [
      { owner: "APP", object_name: "INVOICES", object_type: "TABLE", logical_name: "請求", comment: "請求情報", row_count: 2, column_count: 2, last_ddl_at: "" },
      { owner: "APP", object_name: "DBTOOLS$EXECUTION_HISTORY", object_type: "TABLE", logical_name: "内部履歴", comment: "内部履歴", row_count: 4, column_count: 2, last_ddl_at: "" },
      { owner: "APP", object_name: "SYS#AUDIT_VIEW", object_type: "VIEW", logical_name: "内部監査", comment: "内部監査", row_count: null, column_count: 2, last_ddl_at: "" },
    ];
    const items = allItems.filter(
      (item) => !objectType || item.object_type === objectType.toUpperCase()
    );
    return fulfillJson(route, {
      items,
      next_cursor: null,
      total: items.length,
      table_count: items.filter((item) => item.object_type === "TABLE").length,
      view_count: items.filter((item) => item.object_type === "VIEW").length,
      catalog_version: 1,
    });
  });
  const hiddenNames = ["DBTOOLS$EXECUTION_HISTORY", "SYS#AUDIT_VIEW"];
  const pages = [
    { path: "/data-management", ready: "data-management-preview-grid" },
    { path: "/table-management", ready: "table-management-grid" },
    { path: "/view-management", ready: "view-management-grid" },
    { path: "/comment-management", ready: "comment-management-steps" },
    { path: "/annotation-management", ready: "annotation-management-steps" },
    { path: "/profiles?profile=new", ready: "profile-allowed-object-list" },
  ];

  for (const target of pages) {
    await page.goto(target.path);
    await expect(page.getByTestId(target.ready)).toBeVisible();
    for (const hiddenName of hiddenNames) {
      await expect(page.getByText(hiddenName, { exact: true })).toHaveCount(0);
    }
  }

  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/data-management");
  await expect(page.getByTestId("data-management-preview-grid")).toBeVisible();
  for (const hiddenName of hiddenNames) {
    await expect(page.getByText(hiddenName, { exact: true })).toHaveCount(0);
  }
  await expectNoHorizontalScroll(page);
});

test("Excel/CSV取込の列幅エラーは取込フォーム内に表示して入力を保持する", async ({
  page,
}) => {
  await mockNl2SqlApi(page);
  const errorMessage =
    "TEST_TABLE.NAME: ファイル2行目は16バイトで、取込先列の上限6バイトを超えています。" +
    "値を短くするか列定義を拡張して再試行してください。";
  let submittedMode = "";
  await page.unroute("**/api/nl2sql/db-admin/import-tabular");
  await page.route("**/api/nl2sql/db-admin/import-tabular", async (route) => {
    const payload = route.request().postDataJSON() as { mode?: string };
    submittedMode = payload.mode ?? "";
    await route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: [errorMessage],
        warning_messages: [],
      }),
    });
  });

  await page.goto("/table-management");
  await clickPageHeaderAction(
    page,
    "table-management-actions",
    "Excel/CSV 取込(新規テーブル)"
  );
  const importPanel = page.locator("#table-management-panel-import");
  await importPanel.getByLabel("Oracle 表名").fill("TEST_TABLE");
  await dropFiles(page, importPanel.getByTestId("table-import-file-field-dropzone"), [
    {
      name: "oversized.csv",
      type: "text/csv",
      content: "ID,NAME\n1,株式会社架空\n",
    },
  ]);
  await importPanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");

  const executeButton = importPanel.getByRole("button", { name: "取込を実行" });
  await mockTabularPreview(page);
  await importPanel.getByTestId("table-import-preview-button").click();
  await executeButton.focus();
  await executeButton.press("Enter");

  const errorStatus = importPanel.getByRole("alert").filter({ hasText: "16バイト" });
  await expect(errorStatus).toBeVisible();
  await expect(errorStatus).toContainText("列定義を拡張して再試行してください");
  await expect(errorStatus.locator("svg")).toHaveCount(1);
  await expect(page.getByText(errorMessage, { exact: true })).toHaveCount(1);
  expect(submittedMode).toBe("create");
  await expect(importPanel.getByLabel("Oracle 表名")).toHaveValue("TEST_TABLE");
  await expect(importPanel.getByText("取込方法", { exact: true })).toHaveCount(0);
  await expect(importPanel.getByRole("combobox")).toHaveCount(0);
  await expect(importPanel.getByText("選択中: oversized.csv")).toBeVisible();
  await expect(importPanel.getByLabel("実行確認語")).toHaveValue("ADMIN_EXECUTE");
  await expect(executeButton).toBeEnabled();

  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
  await importPanel.getByLabel("Oracle 表名").fill("TEST_TABLE_FIXED");
  await expect(errorStatus).toHaveCount(0);
});

test("Excel/CSV取込の重複名は対象と安全な復旧手順を表示する", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/db-admin/import-tabular");
  await page.route("**/api/nl2sql/db-admin/import-tabular", async (route) => {
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: ["「TEST_TABLE」は既に存在するため、新規作成できません。"],
        error_code: "ORA-00955",
        error_details: {
          summary: "「TEST_TABLE」は既に存在するため、新規作成できません。",
          cause: "既存のTABLE と名前が重複しています。",
          actions: ["表名を変更して再実行してください。", "一覧へ戻り、同名の既存オブジェクトを確認してください。"],
          target_name: "TEST_TABLE",
          target_type: "TABLE",
          operation: "tabular_import",
          raw_message: "ORA-00955: name is already used by an existing object",
        },
      }),
    });
  });

  await page.goto("/table-management");
  await clickPageHeaderAction(page, "table-management-actions", "Excel/CSV 取込(新規テーブル)");
  const importPanel = page.locator("#table-management-panel-import");
  await importPanel.getByLabel("Oracle 表名").fill("TEST_TABLE");
  await dropFiles(page, importPanel.getByTestId("table-import-file-field-dropzone"), [
    { name: "test.csv", type: "text/csv", content: "ID\n1\n" },
  ]);
  await importPanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await mockTabularPreview(page);
  await importPanel.getByTestId("table-import-preview-button").click();
  await importPanel.getByRole("button", { name: "取込を実行" }).click();

  const alert = importPanel.getByRole("alert");
  await expect(alert).toContainText("TEST_TABLE");
  await expect(alert).toContainText("表名を変更して再実行してください");
  await expect(importPanel.getByLabel("Oracle 表名")).toHaveValue("TEST_TABLE");
  await alert.getByText("詳細ログ").click();
  // 詳細ログはサニタイズ済みのエラーコードを表示する(生の Oracle 文言は expose しない)。
  await expect(alert).toContainText("ORA-00955");
  await expect(alert.getByRole("button", { name: "一覧へ戻る" })).toBeVisible();
  await page.setViewportSize({ width: 375, height: 812 });
  await expectNoHorizontalScroll(page);
});

test("Excel/CSV取込はSheet不一致をエラーで止め旧結果を残さない", async ({ page }) => {
  await mockNl2SqlApi(page);
  let schemaRefreshRequests = 0;
  page.on("request", (request) => {
    // SchemaRefreshCoordinator の active-job discovery(GET /active)は refresh 開始ではない。
    const url = request.url();
    if (url.includes("/api/schema/refresh-jobs/") && !url.includes("/refresh-jobs/active")) {
      schemaRefreshRequests += 1;
    }
  });
  await page.unroute("**/api/nl2sql/db-admin/import-tabular");
  await page.route("**/api/nl2sql/db-admin/import-tabular", async (route) => {
    const payload = route.request().postDataJSON() as { table_name?: string; sheet_name?: string; preview?: boolean };
    const tableName = payload.table_name ?? "TABLE_2026081301";
    if (payload.sheet_name === "Sheet1") {
      await fulfillJson(route, {
        table_name: tableName,
        filename: "Book1.xlsx",
        sheet_name: "Sheet1",
        mode: "create",
        columns: [
          { source_name: "A", column_name: "A", data_type: "NUMBER", nullable: true },
          { source_name: "B", column_name: "B", data_type: "NUMBER", nullable: true },
        ],
        row_count: 2,
        executed: !payload.preview,
        ddl: `CREATE TABLE "${tableName}" ("A" NUMBER, "B" NUMBER)`,
        insert_sql: `INSERT INTO "${tableName}" ("A", "B") VALUES (:c0, :c1)`,
        schema_refresh_job_id: "",
        warnings: [],
        sample_rows: [
          { A: "123", B: "456" },
          { A: "666", B: "777" },
        ],
        timing,
      });
      return;
    }
    await route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: [
          "Shell1: Sheet が見つかりません。Sheet 名を修正するか、ファイル内の Sheet 名を確認して再試行してください。利用可能な Sheet: Sheet1。",
        ],
        warning_messages: [],
      }),
    });
  });

  await page.goto("/table-management");
  await clickPageHeaderAction(page, "table-management-actions", "Excel/CSV 取込(新規テーブル)");
  const importPanel = page.locator("#table-management-panel-import");
  await importPanel.getByLabel("Oracle 表名").fill("TABLE_2026081301");
  await importPanel.getByLabel("Sheet 名").fill("Sheet1");
  await dropFiles(page, importPanel.getByTestId("table-import-file-field-dropzone"), [
    {
      name: "Book1.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock workbook",
    },
  ]);
  await importPanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await importPanel.getByTestId("table-import-preview-button").click();
  await importPanel.getByRole("button", { name: "取込を実行" }).click();
  await expect(importPanel.getByTestId("table-import-result-panel")).toContainText(
    'CREATE TABLE "TABLE_2026081301"'
  );

  await importPanel.getByLabel("Sheet 名").fill("Shell1");
  await expect(importPanel.getByLabel("実行確認語")).toHaveValue("");
  await importPanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await importPanel.getByTestId("table-import-preview-button").click();

  const alert = importPanel.getByRole("alert").filter({ hasText: "Sheet が見つかりません" });
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("Sheet 名を修正するか");
  await expect(importPanel.getByTestId("table-import-result-panel")).toHaveCount(0);
  await expect(importPanel.getByTestId("table-import-schema-refresh-processing")).toHaveCount(0);
  await expect(importPanel.getByText(/active または先頭 Sheet/)).toHaveCount(0);
  await expect(importPanel.getByLabel("Oracle 表名")).toHaveValue("TABLE_2026081301");
  await expect(importPanel.getByLabel("Sheet 名")).toHaveValue("Shell1");
  await expect(importPanel.getByText("選択中: Book1.xlsx")).toBeVisible();
  await expect(importPanel.getByLabel("実行確認語")).toHaveValue("ADMIN_EXECUTE");
  expect(schemaRefreshRequests).toBe(0);
  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
});

test("Excel/CSV取込はDB構造再取得を待たず結果を実行枠下に表示する", { tag: "@responsive" }, async ({ page }) => {
  await mockNl2SqlApi(page);
  let schemaRefreshPolls = 0;
  let dbAdminObjectRequests = 0;
  page.on("request", (request) => {
    if (request.url().includes("/api/nl2sql/db-admin/objects?")) dbAdminObjectRequests += 1;
  });
  await page.unroute("**/api/nl2sql/db-admin/import-tabular");
  await page.route("**/api/nl2sql/db-admin/import-tabular", async (route) => {
    const payload = route.request().postDataJSON() as { table_name?: string; filename?: string; mode?: string };
    const tableName = payload.table_name ?? "IMPORTED_ORDERS";
    await fulfillJson(route, {
      table_name: tableName,
      filename: payload.filename ?? "orders.csv",
      sheet_name: "",
      mode: payload.mode ?? "create",
      columns: [
        { source_name: "ORDER_ID", column_name: "ORDER_ID", data_type: "NUMBER", nullable: false },
        { source_name: "ORDER_NAME", column_name: "ORDER_NAME", data_type: "VARCHAR2(4 CHAR)", nullable: true },
      ],
      row_count: 1,
      executed: true,
      ddl: `CREATE TABLE "${tableName}" ("ORDER_ID" NUMBER, "ORDER_NAME" VARCHAR2(4 CHAR))`,
      insert_sql: `INSERT INTO "${tableName}" ("ORDER_ID", "ORDER_NAME") VALUES (:c0, :c1)`,
      schema_refresh_job_id: "table-import-refresh-pending",
      warnings: [],
      sample_rows: [{ ORDER_ID: "1", ORDER_NAME: "架空商事" }],
      timing,
    });
  });
  await page.route("**/api/schema/refresh-jobs/table-import-refresh-pending", async (route) => {
    schemaRefreshPolls += 1;
    const done = schemaRefreshPolls >= 2;
    await fulfillJson(route, {
      job_id: "table-import-refresh-pending",
      status: done ? "done" : "running",
      mode: "targeted",
      source: "db_admin_import_tabular",
      target_objects: [
        {
          owner: "APP",
          object_name: "IMPORTED_ORDERS",
          object_type: "table",
          expected_state: "present",
        },
      ],
      phase: done ? "done" : "fetching",
      created_at: "2026-07-22T00:00:00.000Z",
      scanned_objects: done ? 1 : 0,
      changed_objects: done ? 1 : 0,
      deleted_objects: 0,
      catalog_version: done ? 2 : 0,
      error_code: "",
    });
  });

  await page.goto("/table-management");
  await clickPageHeaderAction(page, "table-management-actions", "Excel/CSV 取込(新規テーブル)");
  const importPanel = page.locator("#table-management-panel-import");
  const executionFieldset = importPanel.getByTestId("table-import-execution-fieldset");
  const executeButton = importPanel.getByRole("button", { name: "取込を実行" });
  await importPanel.getByLabel("Oracle 表名").fill("IMPORTED_ORDERS");
  await dropFiles(page, importPanel.getByTestId("table-import-file-field-dropzone"), [
    { name: "orders.csv", type: "text/csv", content: "ORDER_ID,ORDER_NAME\n1,架空商事\n" },
  ]);
  await importPanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await mockTabularPreview(page);
  await importPanel.getByTestId("table-import-preview-button").click();
  await executeButton.click();

  const resultPanel = executionFieldset.getByTestId("table-import-result-panel");
  await expect(resultPanel).toBeVisible();
  await expect(resultPanel).toContainText('CREATE TABLE "IMPORTED_ORDERS"');
  await expect(importPanel.getByTestId("table-import-schema-refresh-processing")).toBeVisible();
  await expect(importPanel.getByTestId("table-import-schema-refresh-processing")).toContainText(
    "DB 構造の差分を同期しています",
  );
  await expect(executeButton).toBeEnabled();
  await expect.poll(() => schemaRefreshPolls).toBeGreaterThan(0);
  await expect.poll(() => schemaRefreshPolls).toBeGreaterThanOrEqual(2);
  await expect(importPanel.getByTestId("table-import-schema-refresh-processing")).toHaveCount(0);
  await page.waitForTimeout(200);
  const dbAdminObjectRequestsAfterDone = dbAdminObjectRequests;
  await page.waitForTimeout(1_500);
  expect(dbAdminObjectRequests).toBe(dbAdminObjectRequestsAfterDone);

  const fieldsetBox = await executionFieldset.boundingBox();
  const buttonBox = await executeButton.boundingBox();
  const resultBox = await resultPanel.boundingBox();
  expect(fieldsetBox).not.toBeNull();
  expect(buttonBox).not.toBeNull();
  expect(resultBox).not.toBeNull();
  expect(resultBox!.y).toBeGreaterThan(buttonBox!.y + buttonBox!.height);
  expect(resultBox!.y).toBeGreaterThanOrEqual(fieldsetBox!.y);
  expect(resultBox!.y + resultBox!.height).toBeLessThanOrEqual(fieldsetBox!.y + fieldsetBox!.height + 1);

  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
});

test("Excel/CSV取込後の差分同期不整合はDB構造再取得CTAを表示する", async ({ page }) => {
  await mockNl2SqlApi(page);
  let manualRefreshStarted = 0;
  await page.unroute("**/api/nl2sql/db-admin/import-tabular");
  await page.route("**/api/nl2sql/db-admin/import-tabular", async (route) => {
    const payload = route.request().postDataJSON() as { table_name?: string; filename?: string; mode?: string };
    const tableName = payload.table_name ?? "IMPORTED_ORDERS";
    await fulfillJson(route, {
      table_name: tableName,
      filename: payload.filename ?? "orders.csv",
      sheet_name: "",
      mode: payload.mode ?? "create",
      columns: [
        { source_name: "ORDER_ID", column_name: "ORDER_ID", data_type: "NUMBER", nullable: false },
      ],
      row_count: 1,
      executed: true,
      ddl: `CREATE TABLE "${tableName}" ("ORDER_ID" NUMBER)`,
      insert_sql: `INSERT INTO "${tableName}" ("ORDER_ID") VALUES (:c0)`,
      schema_refresh_job_id: "table-import-refresh-error",
      schema_refresh_required: false,
      schema_refresh_reason_code: "",
      warnings: [],
      sample_rows: [{ ORDER_ID: "1" }],
      timing,
    });
  });
  await page.route("**/api/schema/refresh-jobs/table-import-refresh-error", async (route) => {
    await fulfillJson(route, {
      job_id: "table-import-refresh-error",
      status: "error",
      mode: "targeted",
      source: "db_admin_import_tabular",
      target_objects: [
        {
          owner: "APP",
          object_name: "IMPORTED_ORDERS",
          object_type: "table",
          expected_state: "present",
        },
      ],
      requires_full_refresh: true,
      phase: "fetching",
      created_at: "2026-07-22T00:00:00.000Z",
      scanned_objects: 1,
      changed_objects: 0,
      deleted_objects: 0,
      catalog_version: 0,
      error_code: "schema_refresh_full_required",
    });
  });
  await page.route("**/api/schema/refresh-jobs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    manualRefreshStarted += 1;
    await fulfillJson(route, {
      job_id: "manual-full-refresh",
      status: "done",
      mode: "full",
      source: "manual",
      target_objects: [],
      requires_full_refresh: false,
      phase: "done",
      created_at: "2026-07-22T00:00:01.000Z",
      scanned_objects: 2,
      changed_objects: 1,
      deleted_objects: 0,
      catalog_version: 2,
      error_code: "",
    });
  });
  await page.route("**/api/schema/refresh-jobs/manual-full-refresh", async (route) => {
    await fulfillJson(route, {
      job_id: "manual-full-refresh",
      status: "done",
      mode: "full",
      source: "manual",
      target_objects: [],
      requires_full_refresh: false,
      phase: "done",
      created_at: "2026-07-22T00:00:01.000Z",
      scanned_objects: 2,
      changed_objects: 1,
      deleted_objects: 0,
      catalog_version: 2,
      error_code: "",
    });
  });

  await page.goto("/table-management");
  await clickPageHeaderAction(page, "table-management-actions", "Excel/CSV 取込(新規テーブル)");
  const importPanel = page.locator("#table-management-panel-import");
  await importPanel.getByLabel("Oracle 表名").fill("IMPORTED_ORDERS");
  await dropFiles(page, importPanel.getByTestId("table-import-file-field-dropzone"), [
    { name: "orders.csv", type: "text/csv", content: "ORDER_ID\n1\n" },
  ]);
  await importPanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await mockTabularPreview(page);
  await importPanel.getByTestId("table-import-preview-button").click();
  await importPanel.getByRole("button", { name: "取込を実行" }).click();

  await expect(importPanel).toContainText("DB 構造の差分同期で不整合を検出しました。");
  const refreshButton = importPanel.getByRole("button", { name: "DB 構造を再取得" });
  await expect(refreshButton).toBeVisible();
  await refreshButton.click();
  await expect.poll(() => manualRefreshStarted).toBe(1);
});

test("テーブル作成はDB構造再取得を待たずSQL結果と操作可能状態を戻す", async ({ page }) => {
  await mockNl2SqlApi(page);
  let schemaRefreshPolls = 0;
  const statementsGate = createRequestGate();
  await page.unroute("**/api/nl2sql/db-admin/statements");
  await page.route("**/api/nl2sql/db-admin/statements", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    const sql = String(payload.sql ?? "CREATE TABLE T_PENDING (ID NUMBER)");
    await statementsGate.promise;
    await fulfillJson(route, {
      executed: true,
      runtime: "oracle",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "CREATE",
          status: "success",
          sql,
          row_count: null,
          message: "OK",
          elapsed_ms: 1,
          error_message: "",
        },
      ],
      committed: true,
      rolled_back: false,
      schema_refresh_job_id: "table-create-refresh-running",
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/schema/refresh-jobs/table-create-refresh-running", async (route) => {
    schemaRefreshPolls += 1;
    await fulfillJson(route, {
      job_id: "table-create-refresh-running",
      status: "running",
      mode: "targeted",
      source: "db_admin_statements",
      target_objects: [
        {
          owner: "APP",
          object_name: "T_PENDING",
          object_type: "table",
          expected_state: "present",
        },
      ],
      phase: "fetching",
      created_at: "2026-07-22T00:00:00.000Z",
      started_at: "2026-07-22T00:00:01.000Z",
      heartbeat_at: "2026-07-22T00:00:02.000Z",
      lease_expires_at: "2026-07-22T00:15:02.000Z",
      worker_id: "api:test",
      attempt: 1,
      processed_objects: 1,
      total_objects: 2,
      scanned_objects: 0,
      changed_objects: 0,
      deleted_objects: 0,
      catalog_version: 0,
      error_code: "",
    });
  });

  await page.goto("/table-management");
  await clickPageHeaderAction(page, "table-management-actions", "テーブル作成");
  const createPanel = page.locator("#table-management-panel-create");
  const executeButton = createPanel.getByRole("button", { name: "SQL 実行" });
  await createPanel
    .getByLabel("SQL(セミコロン区切りで複数文を入力可能)")
    .fill("CREATE TABLE T_PENDING (ID NUMBER)");
  await createPanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  const statementsRequest = page.waitForRequest("**/api/nl2sql/db-admin/statements");
  await executeButton.click();
  await statementsRequest;
  try {
    const executionActivity = createPanel.getByTestId("statement-runner-table_ddl-execution-activity");
    await expect(executionActivity).toContainText("SQL を実行しています");
    await expect(executionActivity.locator("svg.animate-spin")).toHaveCount(0);
    await expect(executionActivity.getByRole("timer")).toHaveAccessibleName(/経過時間 \d{2}:\d{2}/);
    await expectCompactExecutionActivity(executionActivity);
    await expect(createPanel.getByTestId("statement-runner-table_ddl-processing-region")).toHaveCount(0);
    await expectNoHorizontalScroll(page);
  } finally {
    statementsGate.release();
  }

  await expect(
    createPanel.getByTestId("statement-runner-table_ddl-processing-region").getByText("CREATE TABLE T_PENDING")
  ).toBeVisible();
  await expect(createPanel.getByText("コミット済み")).toBeVisible();
  const completedExecutionActivity = createPanel.getByTestId("statement-runner-table_ddl-execution-activity");
  await expect(completedExecutionActivity.getByRole("timer")).toHaveAccessibleName(/処理時間 \d{2}:\d{2}/);
  await expectCompactExecutionActivity(completedExecutionActivity);
  await expect(executeButton).toBeEnabled();
  await expect.poll(() => schemaRefreshPolls).toBeGreaterThan(0);
  await expect(page.getByTestId("table-schema-refresh-status")).toContainText(
    "DB 構造差分同期: 構造取得中",
  );
  await expectNoHorizontalScroll(page);
});

test("ビュー作成後はDB構造差分同期を作成パネル内に表示し完了後に消す", async ({ page }) => {
  await mockNl2SqlApi(page);
  let schemaRefreshPolls = 0;
  await page.unroute("**/api/nl2sql/db-admin/statements");
  await page.route("**/api/nl2sql/db-admin/statements", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    const sql = String(
      payload.sql ?? "CREATE OR REPLACE VIEW V_PENDING AS SELECT 1 AS ID FROM DUAL"
    );
    await fulfillJson(route, {
      executed: true,
      runtime: "oracle",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "CREATE",
          status: "success",
          sql,
          row_count: null,
          message: "OK",
          elapsed_ms: 1,
          error_message: "",
        },
      ],
      committed: true,
      rolled_back: false,
      schema_refresh_job_id: "view-create-refresh-done",
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/schema/refresh-jobs/view-create-refresh-done", async (route) => {
    schemaRefreshPolls += 1;
    const done = schemaRefreshPolls >= 2;
    await fulfillJson(route, {
      job_id: "view-create-refresh-done",
      status: done ? "done" : "running",
      mode: "targeted",
      source: "db_admin_statements",
      target_objects: [
        {
          owner: "APP",
          object_name: "V_PENDING",
          object_type: "view",
          expected_state: "present",
        },
      ],
      requires_full_refresh: false,
      phase: done ? "done" : "fetching",
      created_at: "2026-07-22T00:00:00.000Z",
      scanned_objects: done ? 1 : 0,
      changed_objects: done ? 1 : 0,
      deleted_objects: 0,
      catalog_version: done ? 2 : 0,
      error_code: "",
    });
  });

  await page.goto("/view-management");
  await clickPageHeaderAction(page, "view-management-actions", "ビュー作成");
  const createPanel = page.locator("#view-management-panel-create");
  await createPanel
    .getByLabel("SQL(セミコロン区切りで複数文を入力可能)")
    .fill("CREATE OR REPLACE VIEW V_PENDING AS SELECT 1 AS ID FROM DUAL");
  await createPanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await createPanel.getByRole("button", { name: "SQL 実行" }).click();

  const resultRegion = createPanel.getByTestId("statement-runner-view_ddl-processing-region");
  await expect(resultRegion.getByText("CREATE OR REPLACE VIEW V_PENDING")).toBeVisible();
  await expect(createPanel.getByText("コミット済み", { exact: true })).toBeVisible();
  const schemaSync = createPanel.getByTestId("view-create-schema-refresh-processing");
  await expect(schemaSync).toContainText("DB 構造の差分を同期しています");
  await expect.poll(() => schemaRefreshPolls).toBeGreaterThanOrEqual(2);
  await expect(schemaSync).toHaveCount(0);
  await expect(resultRegion.getByText("CREATE OR REPLACE VIEW V_PENDING")).toBeVisible();
  await expect(createPanel.getByText("コミット済み", { exact: true })).toBeVisible();
});

test("ビュー作成の差分同期不整合はDB構造再取得CTAを表示する", async ({ page }) => {
  await mockNl2SqlApi(page);
  let manualRefreshStarted = 0;
  await page.unroute("**/api/nl2sql/db-admin/statements");
  await page.route("**/api/nl2sql/db-admin/statements", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    const sql = String(
      payload.sql ?? "CREATE OR REPLACE VIEW V_NEEDS_REFRESH AS SELECT 1 AS ID FROM DUAL"
    );
    await fulfillJson(route, {
      executed: true,
      runtime: "oracle",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "CREATE",
          status: "success",
          sql,
          row_count: null,
          message: "OK",
          elapsed_ms: 1,
          error_message: "",
        },
      ],
      committed: true,
      rolled_back: false,
      schema_refresh_job_id: "view-create-refresh-error",
      warnings: [],
      timing,
    });
  });
  await page.route("**/api/schema/refresh-jobs/view-create-refresh-error", async (route) => {
    await fulfillJson(route, {
      job_id: "view-create-refresh-error",
      status: "error",
      mode: "targeted",
      source: "db_admin_statements",
      target_objects: [
        {
          owner: "APP",
          object_name: "V_NEEDS_REFRESH",
          object_type: "view",
          expected_state: "present",
        },
      ],
      requires_full_refresh: true,
      phase: "fetching",
      created_at: "2026-07-22T00:00:00.000Z",
      scanned_objects: 1,
      changed_objects: 0,
      deleted_objects: 0,
      catalog_version: 0,
      error_code: "schema_refresh_full_required",
    });
  });
  await page.route("**/api/schema/refresh-jobs", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    manualRefreshStarted += 1;
    await fulfillJson(route, {
      job_id: "view-create-manual-full-refresh",
      status: "done",
      mode: "full",
      source: "manual",
      target_objects: [],
      requires_full_refresh: false,
      phase: "done",
      created_at: "2026-07-22T00:00:01.000Z",
      scanned_objects: 2,
      changed_objects: 1,
      deleted_objects: 0,
      catalog_version: 2,
      error_code: "",
    });
  });
  await page.route("**/api/schema/refresh-jobs/view-create-manual-full-refresh", async (route) => {
    await fulfillJson(route, {
      job_id: "view-create-manual-full-refresh",
      status: "done",
      mode: "full",
      source: "manual",
      target_objects: [],
      requires_full_refresh: false,
      phase: "done",
      created_at: "2026-07-22T00:00:01.000Z",
      scanned_objects: 2,
      changed_objects: 1,
      deleted_objects: 0,
      catalog_version: 2,
      error_code: "",
    });
  });

  await page.goto("/view-management");
  await clickPageHeaderAction(page, "view-management-actions", "ビュー作成");
  const createPanel = page.locator("#view-management-panel-create");
  await createPanel
    .getByLabel("SQL(セミコロン区切りで複数文を入力可能)")
    .fill("CREATE OR REPLACE VIEW V_NEEDS_REFRESH AS SELECT 1 AS ID FROM DUAL");
  await createPanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await createPanel.getByRole("button", { name: "SQL 実行" }).click();

  const resultRegion = createPanel.getByTestId("statement-runner-view_ddl-processing-region");
  await expect(resultRegion.getByText("CREATE OR REPLACE VIEW V_NEEDS_REFRESH")).toBeVisible();
  await expect(createPanel.getByText("コミット済み", { exact: true })).toBeVisible();
  await expect(page.locator("body")).toContainText("DB 構造の差分同期で不整合を検出しました。");
  const refreshButton = page.getByRole("button", { name: "DB 構造を再取得" });
  await expect(refreshButton).toBeVisible();
  await refreshButton.click();
  await expect.poll(() => manualRefreshStarted).toBe(1);
});

test("table and view management pages run guarded DDL and AI workflows", { tag: "@responsive" }, async ({ page }) => {
  test.slow();
  const api = await mockNl2SqlApi(page);

  await page.goto("/table-management");
  // 一覧が既定。作成はアクションボタンで開き、一覧に戻るで戻る。
  await expect(page.getByTestId("table-management-grid")).toBeVisible();
  await clickPageHeaderAction(page, "table-management-actions", "テーブル作成");
  await expect(page.getByTestId("table-management-grid")).toHaveCount(0);
  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("table-management-grid")).toBeVisible();
  await expect(page.getByText("テーブル数", { exact: true })).toHaveCount(0);
  await expect(page.getByText("取得元", { exact: true })).toHaveCount(0);
  await expect(page.getByText(/DB 構造の最終取得:/)).toBeVisible();
  const tablePageActions = page.getByTestId("table-management-actions");
  // 共有 PageHeader は lg 未満で主操作以外を「その他の操作」メニューへ畳む。
  if ((page.viewportSize()?.width ?? 0) < 1024) {
    await tablePageActions.getByRole("button", { name: "その他の操作" }).click();
    const tableMenu = page.getByRole("menu", { name: "その他の操作" });
    await expect(tableMenu.getByRole("menuitem", { name: "最新情報を取得" })).toBeVisible();
    await expect(tableMenu.getByRole("menuitem", { name: "DB 構造を再取得" })).toBeVisible();
    await page.keyboard.press("Escape");
  } else {
    await expect(tablePageActions.getByRole("button", { name: "その他の操作" })).toHaveCount(0);
    await expect(tablePageActions.getByRole("button", { name: "最新情報を取得" })).toBeVisible();
    await expect(tablePageActions.getByRole("button", { name: "DB 構造を再取得" })).toBeVisible();
  }
  await page.getByLabel("検索").fill("請求");
  await expect(page.getByTestId("table-management-grid").getByText("APP.INVOICES")).toBeVisible();
  await page.getByRole("button", { name: "APP.INVOICES を表示" }).click();
  const columnsPanel = page.getByTestId("db-admin-detail-columns");
  // 論理名(業務名)とコメント(生カラムコメント)は別列で表示される。
  await expect(columnsPanel.getByRole("columnheader", { name: "論理名" })).toBeVisible();
  await expect(columnsPanel.getByRole("columnheader", { name: "コメント" })).toBeVisible();
  await expect(columnsPanel.getByRole("cell", { name: "取引先名" }).first()).toBeVisible();
  await expect(columnsPanel.getByRole("cell", { name: "税込請求金額" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
  await expect(page.getByText("2 列")).toBeVisible();
  await expect(page.getByRole("button", { name: /XLSX ダウンロード/ })).toBeVisible();
  const columnsDownloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: /XLSX ダウンロード/ }).click();
  const columnsDownload = await columnsDownloadPromise;
  expect(columnsDownload.suggestedFilename()).toBe("app_invoices_columns.xlsx");
  await expect
    .poll(() =>
      page.getByTestId("db-admin-detail-columns").evaluate((wrapper) => {
        const table = wrapper.querySelector("table");
        if (!table) return false;
        return table.getBoundingClientRect().width >= wrapper.clientWidth - 1;
      })
    )
    .toBeTruthy();

  const columnsTab = page.getByRole("tab", { name: "列情報" });
  const ddlTab = page.getByRole("tab", { name: "DDL" });
  await columnsTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(ddlTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText('CREATE TABLE "INVOICES"')).toBeVisible();
  await page.keyboard.press("ArrowLeft");
  await expect(columnsTab).toHaveAttribute("aria-selected", "true");
  await ddlTab.click();
  await expect(page.getByText('CREATE TABLE "INVOICES"')).toBeVisible();
  await expectContentActionsRightAligned(page.getByTestId("table-management-ddl-actions"));
  const ddlDownloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "SQL 出力" }).click();
  const ddlDownload = await ddlDownloadPromise;
  expect(ddlDownload.suggestedFilename()).toBe("app_invoices_ddl.sql");

  await expect(page.getByTestId("table-management-grid").getByRole("button", { name: /^操作: / })).toHaveCount(0);
  await clickObjectDetailAction(page, "table-management-detail-actions", "削除");
  const dropTableDialog = page.getByRole("dialog", { name: "DROP TABLE の確認" });
  await expect(dropTableDialog).toBeVisible();
  await mainScroller(page).evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
  await expectSharedScrimOverlayCoversViewport(page);
  await expectOnlyNeutralConfirmationFieldInDialog(dropTableDialog);
  await dropTableDialog.getByLabel("実行確認語").fill("APP.INVOICES");
  await expect(dropTableDialog.getByText("確認済み", { exact: true })).toHaveCount(1);
  await page.getByRole("button", { name: "Drop 実行" }).click();
  await expect.poll(() => api.dropTablePayload?.confirmation).toBe("APP.INVOICES");
  expect(api.dropTablePayload?.table_name).toBe("INVOICES");
  expect(api.dropTablePayload?.owner).toBe("APP");
  await expect(page.getByRole("dialog", { name: "DROP TABLE の確認" })).toHaveCount(0);

  await clickPageHeaderAction(page, "table-management-actions", "テーブル作成");
  const createPanel = page.locator("#table-management-panel-create");
  await expect(page.getByTestId("table-management-grid")).toHaveCount(0);
  await expect(page.getByTestId("db-admin-detail-columns")).toHaveCount(0);
  await expect(createPanel).toBeVisible();
  const createSqlInput = createPanel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)");
  const createConfirmationInput = createPanel.getByLabel("実行確認語");
  await createSqlInput.fill("CREATE TABLE T1 (ID NUMBER)");
  await expect(createPanel.getByText("ADMIN_EXECUTE を入力すると実行できます。")).toBeVisible();
  await expect(createPanel.getByRole("button", { name: "SQL プレビュー" })).toHaveCount(0);
  await expect(createPanel.getByLabel("Oracle に実行する")).toHaveCount(0);
  await expect(createPanel.getByText("入力条件: ADMIN_EXECUTE")).toBeVisible();
  await createConfirmationInput.fill("ADMIN_EXECUTE");
  await expect(createPanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  const createExecuteButton = createPanel.getByRole("button", { name: "SQL 実行" });
  const createClearButton = createPanel
    .getByTestId("execution-confirmation-field")
    .getByRole("button", { name: "SQL 入力・結果をリセット", exact: true });
  await expect(createClearButton).toBeEnabled();
  await expectButtonsSameHeight(createExecuteButton, createClearButton);
  await createClearButton.click();
  await expect(createSqlInput).toHaveValue("");
  await expect(createConfirmationInput).toHaveValue("");
  await expect(createExecuteButton).toBeDisabled();
  await expect(createClearButton).toBeDisabled();
  await createSqlInput.fill("CREATE TABLE T1 (ID NUMBER)");
  await createConfirmationInput.fill("ADMIN_EXECUTE");
  await expect(createPanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  await createExecuteButton.click();
  await expect.poll(() => api.statementsPayload?.policy).toBe("table_ddl");
  expect(api.statementsPayload?.confirmation).toBe("ADMIN_EXECUTE");

  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await clickPageHeaderAction(
    page,
    "table-management-actions",
    "Excel/CSV 取込(新規テーブル)"
  );
  const importPanel = page.locator("#table-management-panel-import");
  await expect(page.getByTestId("table-management-grid")).toHaveCount(0);
  await expect(page.getByTestId("db-admin-detail-columns")).toHaveCount(0);
  await expect(importPanel).toBeVisible();
  // 必須はテキストのバッジで示すので、凡例文（「* は必須入力項目です。」）は出さない。
  await expect(importPanel.getByText(/必須入力項目です。/)).toHaveCount(0);
  const importExecuteButton = importPanel.getByRole("button", { name: "取込を実行" });
  await expect(importPanel.getByText("入力条件: ADMIN_EXECUTE")).toBeVisible();
  const importConfirmationInput = importPanel.getByLabel("実行確認語");
  await importConfirmationInput.fill("ADMIN_EXECUTE");
  await expect(importPanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  const importActionClearButton = importPanel
    .getByTestId("table-import-execution-fieldset")
    .getByRole("button", { name: "取込設定・結果をリセット", exact: true });
  await expect(importActionClearButton).toBeEnabled();
  await expectButtonsSameHeight(importExecuteButton, importActionClearButton);
  await importActionClearButton.click();
  await expect(importConfirmationInput).toHaveValue("");
  await expect(importExecuteButton).toBeDisabled();
  await expect(importActionClearButton).toBeDisabled();
  await importConfirmationInput.fill("ADMIN_EXECUTE");
  await expect(importPanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  await expect(importExecuteButton).toBeDisabled();
  await importPanel.getByLabel("Oracle 表名").fill("IMPORTED_ORDERS");
  await importPanel.getByLabel("Sheet 名").fill("Sheet1");
  await expect(importPanel.locator('label[for="table-import-table-name"] span[aria-hidden="true"]')).toHaveText("必須");
  await expect(importPanel.locator('label[for="table-import-sheet-name"] span[aria-hidden="true"]')).toHaveText("必須");
  const importFileClearButton = importPanel.getByRole("button", { name: "取込ファイル選択を解除" });
  const importFileInput = importPanel.getByTestId("table-import-file-field-input");
  await expect(importFileClearButton).toBeDisabled();
  await expect(importFileInput).toHaveAttribute("accept", ".csv,.xlsx,.xls");
  await expect(importFileInput).toHaveAttribute("required", "");
  await expect(importFileInput).toHaveAttribute("aria-required", "true");
  // 表名・シート名は共有の TextField（#631）。ネイティブ検証は使わず aria-required で伝える。
  await expect(importPanel.locator("#table-import-table-name")).toHaveAttribute("aria-required", "true");
  await expect(importPanel.locator("#table-import-sheet-name")).toHaveAttribute("aria-required", "true");
  await expect(importPanel.getByText(".CSV / .XLSX / .XLS", { exact: true })).toHaveCount(1);
  await importPanel.getByLabel("CSV/XLSX/XLS 選択", { exact: true }).setInputFiles({
    name: "orders.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("ORDER_ID,ORDER_NAME\n1,架空商事\n"),
  });
  await expect(importPanel.getByText("選択中: orders.csv")).toBeVisible();
  await expect(importPanel.locator("#table-import-sheet-name")).not.toHaveAttribute("aria-required", "true");
  await expect(importFileClearButton).toBeEnabled();
  await importFileClearButton.click();
  await expect(importPanel.getByText("ドラッグ＆ドロップまたは選択")).toBeVisible();
  await expect(importFileClearButton).toBeDisabled();
  await importPanel.getByLabel("CSV/XLSX/XLS 選択").setInputFiles({
    name: "orders.XLS",
    mimeType: "application/vnd.ms-excel",
    buffer: Buffer.from("legacy-xls"),
  });
  await expect(importPanel.getByText("選択中: orders.XLS")).toBeVisible();
  await expect(importPanel.locator("#table-import-sheet-name")).toHaveAttribute("aria-required", "true");
  await dropFiles(page, importPanel.getByTestId("table-import-file-field-dropzone"), [
    {
      name: "orders.exe",
      type: "application/x-msdownload",
      content: "unsupported",
    },
  ]);
  await expect(importPanel.getByRole("alert")).toContainText(
    ".CSV / .XLSX / .XLS ファイルを選択してください"
  );
  await expect(importPanel.getByText("選択中: orders.XLS")).toBeVisible();
  const importExecuteButtonBox = await importExecuteButton.boundingBox();
  const importConfirmationBox = await importPanel.getByLabel("実行確認語").boundingBox();
  expect(importExecuteButtonBox).not.toBeNull();
  expect(importConfirmationBox).not.toBeNull();
  // 取込を実行ボタンは確認ゲート(ExecutionConfirmationField)の actions スロット、
  // すなわち実行確認語 input の下に置く(CSV/Sample 取込パネルと統一)。
  expect(importExecuteButtonBox!.y).toBeGreaterThan(importConfirmationBox!.y);
  await expect(importPanel.getByRole("button", { name: "確認語に表名を入れる" })).toHaveCount(0);
  await expect(importConfirmationInput).toHaveValue("");
  await importConfirmationInput.fill("ADMIN_EXECUTE");
  await expect(importPanel.getByText("入力条件: ADMIN_EXECUTE")).toBeVisible();
  await expect(importPanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  await mockTabularPreview(page);
  await importPanel.getByTestId("table-import-preview-button").click();
  await expect(importExecuteButton).toBeEnabled();
  await importExecuteButton.click();
  await expect(importPanel.getByText("IMPORTED_ORDERS", { exact: true })).toBeVisible();
  await expect(importPanel.getByTestId("table-import-steps").getByText("実行確認")).toBeVisible();
  await expect.poll(() => api.importTabularPayload?.table_name).toBe("IMPORTED_ORDERS");
  expect(api.importTabularPayload?.filename).toBe("orders.XLS");
  expect(api.importTabularPayload?.mode).toBe("create");
  expect(api.importTabularPayload?.confirmation).toBe("ADMIN_EXECUTE");

  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("table-management-grid")).toBeVisible();
  await expect(page.getByText('CREATE TABLE "INVOICES"')).toHaveCount(0);
  // 削除した対象の選択は外れ、先頭を自動で選ばない（#2281）ので、一覧から選び直す。
  await page.getByRole("button", { name: "APP.INVOICES を表示" }).click();
  await page.getByRole("tab", { name: "DDL" }).click();
  await expect(page.getByText('CREATE TABLE "INVOICES"')).toBeVisible();
  await expectContentActionsRightAligned(page.getByTestId("table-management-ddl-actions"));
  await expectNoHorizontalScroll(page);

  await page.goto("/comment-management");
  await expect(page.getByRole("heading", { name: "コメント管理" })).toBeVisible();
  await expect(page.getByTestId("comment-management-steps")).toBeVisible();
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.INVOICES/);
  await page.getByLabel("サンプル件数").fill("10");
  await page.getByRole("button", { name: "SQL 生成" }).click();
  await expect.poll(() => api.metadataSamplesPayload?.sample_limit).toBe(10);
  await expect.poll(() => api.commentGeneratePayload?.sample_text).toContain(
    "CUSTOMER_NAME: 架空商事, 見本商店"
  );
  const commentExecutePanel = page.locator("#comment-management-panel-execute");
  await expect(page.getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue(
    /COMMENT ON COLUMN "APP"\."INVOICES"\."TOTAL_AMOUNT" IS '税込請求金額';/
  );
  await expect(commentExecutePanel.getByRole("button", { name: "SQL プレビュー" })).toHaveCount(0);
  await expect(commentExecutePanel.getByLabel("Oracle に実行する")).toHaveCount(0);
  await expect(commentExecutePanel.getByText("入力条件: ADMIN_EXECUTE")).toBeVisible();
  await commentExecutePanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await expect(commentExecutePanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  await commentExecutePanel.getByRole("button", { name: "SQL 実行" }).click();
  await expect.poll(() => api.statementsPayload?.policy).toBe("comment_sql");
  expect(api.statementsPayload?.confirmation).toBe("ADMIN_EXECUTE");

  await page.goto("/annotation-management");
  await expect(page.getByRole("heading", { name: "アノテーション管理" })).toBeVisible();
  await expect(page.getByTestId("annotation-management-steps")).toBeVisible();
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.INVOICES/);
  const inputPanel = page.locator("#annotation-management-panel-input");
  await expect(inputPanel).toBeVisible();
  await inputPanel.getByRole("button", { name: "SQL 生成" }).click();
  const annotationExecutePanel = page.locator("#annotation-management-panel-execute");
  await expect(page.getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue(
    /ALTER TABLE "APP"\."INVOICES" MODIFY/
  );
  await expect(annotationExecutePanel.getByRole("button", { name: "SQL プレビュー" })).toHaveCount(0);
  await expect(annotationExecutePanel.getByLabel("Oracle に実行する")).toHaveCount(0);
  await expect(annotationExecutePanel.getByText("入力条件: ADMIN_EXECUTE")).toBeVisible();
  await annotationExecutePanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await expect(annotationExecutePanel.getByText("確認済み", { exact: true })).toHaveCount(1);
  await annotationExecutePanel.getByRole("button", { name: "SQL 実行" }).click();
  await expect.poll(() => api.statementsPayload?.policy).toBe("annotation_sql");
  expect(api.statementsPayload?.confirmation).toBe("ADMIN_EXECUTE");
  await expectNoHorizontalScroll(page);

  await page.goto("/view-management");
  await expect(page.getByRole("heading", { name: "ビュー一覧", exact: true })).toBeVisible();
  await expect(page.getByText("ビュー数", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("view-management-grid")).toBeVisible();
  await page.getByRole("button", { name: "APP.V_EMP_DEPT を表示" }).click();
  const viewColumnsTab = page.getByRole("tab", { name: "列情報" });
  const viewDdlTab = page.getByRole("tab", { name: "DDL" });
  await expect(viewColumnsTab).toHaveAttribute("aria-selected", "true");
  await viewColumnsTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(viewDdlTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText('CREATE OR REPLACE VIEW "V_EMP_DEPT"')).toBeVisible();
  await expectContentActionsRightAligned(page.getByTestId("view-management-ddl-actions"));

  await clickPageHeaderAction(page, "view-management-actions", "JOIN/WHERE 条件抽出");
  const stepIndicatorBox = await page.getByTestId("view-join-where-steps").boundingBox();
  const selectedViewBox = await page.getByTestId("view-join-where-selected-view").boundingBox();
  const advancedSettings = page.getByTestId("view-join-where-advanced-settings");
  const advancedSettingsBox = await advancedSettings.boundingBox();
  const joinWhereActionsBox = await page.getByTestId("view-join-where-actions").boundingBox();
  expect(stepIndicatorBox).not.toBeNull();
  expect(selectedViewBox).not.toBeNull();
  expect(advancedSettingsBox).not.toBeNull();
  expect(joinWhereActionsBox).not.toBeNull();
  expect(stepIndicatorBox!.y).toBeLessThan(advancedSettingsBox!.y);
  expect(selectedViewBox!.y).toBeLessThan(advancedSettingsBox!.y);
  expect(advancedSettingsBox!.y).toBeLessThan(joinWhereActionsBox!.y);
  await expect(page.getByTestId("view-join-where-actions")).toContainText("DDL 取得済み");
  await expect(page.getByText("提示詞プロファイル")).toHaveCount(0);
  await expect(page.getByText("標準: JOIN/WHERE のみ抽出")).toHaveCount(0);
  await expect(advancedSettings.getByText("解析設定")).toBeVisible();
  await expect(advancedSettings.getByText("詳細解析: SQL構造も解析")).toBeVisible();
  await expect(advancedSettings.getByRole("list", { name: "出力範囲" })).toContainText("JOIN");
  await expect(advancedSettings.getByRole("list", { name: "出力範囲" })).toContainText("WHERE");
  await expect(advancedSettings.getByRole("list", { name: "出力範囲" })).toContainText("SQL構造");
  await expect(page.getByRole("combobox", { name: "抽出モード" })).toHaveCount(0);
  await page.getByRole("button", { name: "AI で抽出" }).click();
  await expect.poll(() => api.extractJoinWherePayload?.prompt_profile).toBe("sql_structure");
  await expect(page.getByLabel("結合条件 (JOIN)")).toHaveValue(/EMPLOYEE.*DEPARTMENT/);
  await expect(page.getByLabel("抽出条件 (WHERE)")).toHaveValue("EMPLOYEE(e).STATUS = 'A'");
  // 抽出の方式は API の内部値（deterministic）を出さず、文言で出す（#934）。
  const joinWhereResult = page.getByRole("region", { name: "JOIN/WHERE 条件抽出結果" });
  await expect(joinWhereResult.getByText("規則ベース（AI 未使用）", { exact: true })).toBeVisible();
  await expect(joinWhereResult.getByText("deterministic", { exact: true })).toHaveCount(0);
  await page.getByText("SQL構造解析結果").click();
  await expect(page.getByText("## SQL構造分析")).toBeVisible();

  await page.getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page.getByTestId("view-management-grid").getByRole("button", { name: /^操作: / })).toHaveCount(0);
  await clickObjectDetailAction(page, "view-management-detail-actions", "削除");
  const dropViewDialog = page.getByRole("dialog", { name: "DROP VIEW の確認" });
  await expect(dropViewDialog).toBeVisible();
  await mainScroller(page).evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
  await expectSharedScrimOverlayCoversViewport(page);
  await expectOnlyNeutralConfirmationFieldInDialog(dropViewDialog);
  await expect(page.getByRole("button", { name: "Drop 実行" })).toBeDisabled();
  await page.getByLabel("実行確認語").fill("APP.V_EMP_DEPT");
  await page.getByRole("button", { name: "Drop 実行" }).click();
  await expect.poll(() => api.dropViewPayload?.confirmation).toBe("APP.V_EMP_DEPT");
  expect(api.dropViewPayload?.view_name).toBe("V_EMP_DEPT");
  expect(api.dropViewPayload?.owner).toBe("APP");

  await clickPageHeaderAction(page, "view-management-actions", "ビュー作成");
  await page.getByLabel("SQL(セミコロン区切りで複数文を入力可能)").fill("CREATE OR REPLACE VIEW V1 AS SELECT 1 FROM DUAL");
  await expect(page.getByText("Oracle への SQL 実行")).toBeVisible();
  await expect(page.getByRole("button", { name: "SQL 実行" })).toBeDisabled();
  await page.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await page.getByRole("button", { name: "SQL 実行" }).click();
  await expect.poll(() => api.statementsPayload?.policy).toBe("view_ddl");
  await expectNoHorizontalScroll(page);
  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
});

test("table and view management show DROP failures in the confirmation dialog", async ({ page }) => {
  const api = await mockNl2SqlApi(page);

  await page.route("**/api/nl2sql/db-admin/objects?*", (route) => {
    const objectType = new URL(route.request().url()).searchParams.get("type") ?? "table";
    const items =
      objectType === "view"
        ? [
            {
              name: "V_EMP_DEPT",
              owner: "APP",
              qualified_name: "APP.V_EMP_DEPT",
              object_type: "view",
              row_count: null,
              comment: "社員と部署",
            },
          ]
        : [
            {
              name: "lower",
              owner: "APP",
              qualified_name: 'APP."lower"',
              object_type: "table",
              row_count: 2,
              comment: "小文字テーブル",
            },
          ];
    return fulfillJson(route, {
      runtime: "deterministic",
      owner: "APP",
      items,
      total: items.length,
      table_count: objectType === "view" ? 0 : items.length,
      view_count: objectType === "view" ? items.length : 0,
      next_cursor: null,
      refreshed_at: schemaCatalog.refreshed_at,
      catalog_version: 1,
      warnings: [],
    });
  });
  await page.route("**/api/nl2sql/db-admin/tables/%22lower%22**", (route) =>
    fulfillJson(route, {
      name: "lower",
      owner: "APP",
      qualified_name: 'APP."lower"',
      object_type: "table",
      row_count: 2,
      comment: "小文字テーブル",
      columns: schemaCatalog.tables[0].columns,
      ddl: 'CREATE TABLE "APP"."lower" ("CUSTOMER_NAME" VARCHAR2(120), "TOTAL_AMOUNT" NUMBER);',
      warnings: [],
    })
  );
  await page.route("**/api/nl2sql/db-admin/drop-table", (route) => {
    api.dropTablePayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      executed: false,
      runtime: "oracle",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "DROP",
          status: "error",
          sql: 'DROP TABLE "APP"."lower" PURGE',
          row_count: null,
          message: "",
          elapsed_ms: 0,
          error_message: "ORA-02449: 表の一意/主キーが外部キーに参照されています。",
        },
      ],
      committed: false,
      rolled_back: true,
      warnings: ["ORA-02449: 表の一意/主キーが外部キーに参照されています。"],
      timing,
    });
  });

  await page.goto("/table-management");
  await expect(page.getByTestId("table-management-grid")).toBeVisible();
  await expect(page.getByTestId("table-management-grid")).toContainText('APP."lower"');
  await page.getByRole("button", { name: 'APP."lower" を表示' }).click();
  await clickObjectDetailAction(page, "table-management-detail-actions", "削除");
  const dropTableDialog = page.getByRole("dialog", { name: "DROP TABLE の確認" });
  await dropTableDialog.getByLabel("実行確認語").fill('APP."lower"');
  await dropTableDialog.getByRole("button", { name: "Drop 実行" }).click();

  await expect.poll(() => api.dropTablePayload?.confirmation).toBe('APP."lower"');
  expect(api.dropTablePayload?.table_name).toBe('"lower"');
  expect(api.dropTablePayload?.owner).toBe("APP");
  await expect(dropTableDialog.getByText(/ORA-02449/)).toBeVisible();
  await expect(dropTableDialog).toBeVisible();

  await page.route("**/api/nl2sql/db-admin/drop-view", (route) => {
    api.dropViewPayload = route.request().postDataJSON() as Record<string, unknown>;
    return fulfillJson(route, {
      executed: false,
      runtime: "oracle",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "DROP",
          status: "error",
          sql: 'DROP VIEW "APP"."V_EMP_DEPT"',
          row_count: null,
          message: "",
          elapsed_ms: 0,
          error_message: "ORA-04043: オブジェクト APP.V_EMP_DEPT は存在しません。",
        },
      ],
      committed: false,
      rolled_back: true,
      warnings: [],
      timing,
    });
  });

  await page.goto("/view-management");
  await expect(page.getByTestId("view-management-grid")).toBeVisible();
  await page.getByRole("button", { name: "APP.V_EMP_DEPT を表示" }).click();
  await clickObjectDetailAction(page, "view-management-detail-actions", "削除");
  const dropViewDialog = page.getByRole("dialog", { name: "DROP VIEW の確認" });
  await dropViewDialog.getByLabel("実行確認語").fill("APP.V_EMP_DEPT");
  await dropViewDialog.getByRole("button", { name: "Drop 実行" }).click();

  await expect.poll(() => api.dropViewPayload?.confirmation).toBe("APP.V_EMP_DEPT");
  await expect(dropViewDialog.getByText(/ORA-04043/)).toBeVisible();
  await expect(dropViewDialog).toBeVisible();
});

test("テーブル・ビューの削除のダイアログは、実行中は閉じられない（データの切り詰めとそろえる）", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.setViewportSize({ width: 375, height: 812 });
  const pending: Array<() => void> = [];
  const holdUntilReleased = (path: string, sql: string) =>
    page.route(path, async (route) => {
      await new Promise<void>((resolve) => pending.push(resolve));
      await fulfillJson(route, {
        executed: false,
        runtime: "oracle",
        select_result: null,
        statements: [
          {
            index: 1,
            statement_type: "DROP",
            status: "error",
            sql,
            row_count: null,
            message: "",
            elapsed_ms: 0,
            error_message: "ORA-00054: リソース・ビジー",
          },
        ],
        committed: false,
        rolled_back: true,
        warnings: [],
        timing,
      });
    });
  await holdUntilReleased("**/api/nl2sql/db-admin/drop-table", 'DROP TABLE "APP"."INVOICES" PURGE');
  await holdUntilReleased("**/api/nl2sql/db-admin/drop-view", 'DROP VIEW "APP"."V_EMP_DEPT"');

  const cases = [
    { path: "/table-management", grid: "table-management-grid", target: "APP.INVOICES", actions: "table-management-detail-actions", dialogName: "DROP TABLE の確認" },
    { path: "/view-management", grid: "view-management-grid", target: "APP.V_EMP_DEPT", actions: "view-management-detail-actions", dialogName: "DROP VIEW の確認" },
  ];
  for (const item of cases) {
    await page.goto(item.path);
    await expect(page.getByTestId(item.grid)).toBeVisible();
    await page.getByRole("button", { name: `${item.target} を表示` }).click();
    await clickObjectDetailAction(page, item.actions, "削除");
    const dialog = page.getByRole("dialog", { name: item.dialogName });
    await dialog.getByLabel("実行確認語").fill(item.target);
    await dialog.getByRole("button", { name: "Drop 実行" }).click();
    await expect.poll(() => pending.length).toBe(1);

    // 実行中は「閉じる」「キャンセル」を押せず、ダイアログは開いたまま（結果を見失わない）。
    const close = dialog.getByRole("button", { name: "閉じる" });
    const cancel = dialog.getByRole("button", { name: "キャンセル" });
    await expect(close).toBeDisabled();
    await expect(cancel).toBeDisabled();
    await close.click({ force: true });
    await cancel.click({ force: true });
    await expect(dialog).toBeVisible();

    pending.shift()!();
    await expect(dialog.getByText(/ORA-00054/)).toBeVisible();
    await expect(close).toBeEnabled();
    await cancel.click();
    await expect(dialog).toHaveCount(0);
  }
});

test("annotation management explains ORA-11548 before Oracle execution", async ({ page }) => {
  const api = await mockNl2SqlApi(page);
  await page.setViewportSize({ width: 1280, height: 900 });

  await page.goto("/annotation-management");
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.INVOICES/);
  await page.getByRole("button", { name: "SQL 生成" }).click();

  const executePanel = page.locator("#annotation-management-panel-execute");
  await executePanel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)").fill(
    "ALTER TABLE INVOICES ANNOTATIONS " +
      "(ADD IF NOT EXISTS COMMENT '請求情報を管理するテーブル');"
  );
  await executePanel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await executePanel.getByRole("button", { name: "SQL 実行" }).click();

  await expect.poll(() => api.statementsPayload?.policy).toBe("annotation_sql");
  await expect(executePanel.getByText("ブロック", { exact: true }).first()).toBeVisible();
  await expect(
    executePanel.getByText(/ANNOTATIONS 句の annotation 名が不足しているか/)
  ).toBeVisible();
  await expect(executePanel.getByText('説明用の annotation 名は "DESCRIPTION" に変更してください。'))
    .toBeVisible();
  await expect(executePanel.getByText(/"COMMENT" のように二重引用符/)).toBeVisible();

  // 実行の前に止めた文は「この文は実行できません。」のカード。技術的な詳細は「詳細」に畳む（#2342）。
  await expect(executePanel.getByTestId("db-admin-statement-failure").getByRole("alert")).toContainText(
    "この文は実行できません。"
  );
  const details = executePanel.getByTestId("db-admin-statement-failure").locator("details");
  const summary = details.locator("summary");
  await expect(summary).toContainText("詳細");
  // 失敗のカードの「詳細」は開いて出す（messaging.md §10.3）。閉じてから開き直せる。
  await summary.focus();
  await page.keyboard.press("Enter");
  await expect(details).not.toHaveAttribute("open", "");
  await summary.focus();
  await page.keyboard.press("Enter");
  await expect(details).toHaveAttribute("open", "");
  await expect(details.getByText(/ORA-11548/)).toBeVisible();
  await expectNoHorizontalScroll(page);

  await page.setViewportSize({ width: 375, height: 900 });
  await expectNoHorizontalScroll(page);
  await expect(executePanel.getByText('説明用の annotation 名は "DESCRIPTION" に変更してください。'))
    .toBeVisible();
});

for (const scenario of [
  {
    pageId: "comment-management",
    code: "DB_ADMIN_COMMENT_SQL_POLICY_VIOLATION",
    message: "禁止された操作です。COMMENT ON TABLE/COLUMN/MATERIALIZED VIEW のみ実行できます。",
    sql: "COMMENT ON VIEW APP.V_EMP_DEPT IS '社員と部署';",
    exampleLabel: "テーブル・ビューのコメント",
    exampleSql: 'COMMENT ON TABLE "EXAMPLE_SCHEMA"."EXAMPLE_TABLE_OR_VIEW"\n  IS \'対象の説明\';',
    count: 3,
  },
  {
    pageId: "annotation-management",
    code: "DB_ADMIN_ANNOTATION_SQL_POLICY_VIOLATION",
    message: "この文はアノテーション管理で実行できません。",
    sql: "ALTER TABLE APP.INVOICES ADD X NUMBER;",
    exampleLabel: "テーブルのアノテーション",
    exampleSql: 'ALTER TABLE "EXAMPLE_SCHEMA"."EXAMPLE_TABLE"\n  ANNOTATIONS (ADD IF NOT EXISTS "DESCRIPTION" \'対象の説明\');',
    count: 2,
  },
]) {
  test(`${scenario.pageId} の拒否理由とコピー可能な SQL 例で具体的な復旧方法を示す`, async ({ page, context }, testInfo) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await mockNl2SqlApi(page);
    let executions = 0;
    await page.route("**/api/nl2sql/db-admin/statements", (route) => {
      executions += 1;
      return fulfillJson(route, {
        executed: false,
        committed: false,
        runtime: "oracle",
        statements: [{ index: 1, status: "blocked", statement_type: "UNKNOWN", sql: scenario.sql, error_message: scenario.message, error_code: scenario.code }],
        warnings: [],
        timing,
      });
    });
    await page.goto(`/${scenario.pageId}`);
    await page.getByRole("tab", { name: "SQL実行", exact: true }).click();
    const panel = page.locator(`#${scenario.pageId}-panel-execute`);
    const sqlInput = panel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)");
    await sqlInput.fill(scenario.sql);
    await panel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
    await panel.getByRole("button", { name: "SQL 実行", exact: true }).click();
    const examples = panel.getByRole("region", { name: "具体的な対応例" });
    await expect(examples).toBeVisible();
    await expect(examples.locator("pre")).toHaveCount(scenario.count);
    await expect(examples).toContainText("以下は構文例です。");
    await expect(examples).toContainText("実際の内容に置き換え");
    if (scenario.pageId === "comment-management") {
      await expect(panel).toContainText("ビューのコメントも COMMENT ON TABLE を使用します。");
      await expect(panel).not.toContainText("SQL 実行または Oracle 側の処理でエラーが発生しました。");
    }
    const copyButton = examples.getByRole("button", { name: `${scenario.exampleLabel}の SQL 例をコピー`, exact: true });
    await copyButton.focus();
    await copyButton.press("Enter");
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(scenario.exampleSql);
    // 成功はボタンの Check と読み上げ（成功の Toast は出さない。#2099）。
    await expect(copyButton).toHaveAttribute("data-copy-state", "copied");
    await expect(examples.getByRole("status").filter({ hasText: "コピーしました" })).toHaveCount(1);
    await copyButton.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`${scenario.pageId}-sql-recovery.png`) });
    await expect(sqlInput).toHaveValue(scenario.sql);
    expect(executions).toBe(1);
    await expectNoHorizontalScroll(page);

    await page.evaluate(() => {
      Object.defineProperty(navigator.clipboard, "writeText", {
        configurable: true,
        value: () => Promise.reject(new Error("clipboard unavailable")),
      });
    });
    await copyButton.click();
    await expect(page.getByRole("region", { name: "通知" })).toContainText("コピーできませんでした");
    await expect(copyButton).toBeEnabled();
    await expect(sqlInput).toHaveValue(scenario.sql);
    expect(executions).toBe(1);
  });
}

test("未知の実行エラーでは SQL の修正例を推測表示しない", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.route("**/api/nl2sql/db-admin/statements", (route) => fulfillJson(route, {
    executed: false,
    runtime: "oracle",
    statements: [{ index: 1, status: "blocked", statement_type: "UNKNOWN", sql: "SELECT 1 FROM DUAL", error_message: "禁止された操作です。COMMENT ON TABLE/COLUMN/MATERIALIZED VIEW のみ実行できます。", error_code: "UNRECOGNIZED_ERROR" }],
    warnings: [],
    timing,
  }));
  await page.goto("/comment-management");
  await page.getByRole("tab", { name: "SQL実行", exact: true }).click();
  const panel = page.locator("#comment-management-panel-execute");
  await panel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)").fill("SELECT 1 FROM DUAL");
  await panel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await panel.getByRole("button", { name: "SQL 実行", exact: true }).click();
  await expect(panel.getByRole("alert")).toBeVisible();
  await expect(panel.getByRole("region", { name: "具体的な対応例" })).toHaveCount(0);
});

test("metadata sample limit zero omits samples and reports retrieval errors", async ({ page }) => {
  const api = await mockNl2SqlApi(page);

  await page.goto("/comment-management");
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await page.getByLabel("サンプル件数").fill("0");
  const commentInputPanel = page.locator("#comment-management-panel-input");
  await expect(commentInputPanel).toBeVisible();
  await commentInputPanel.getByRole("button", { name: "SQL 生成" }).click();

  await expect.poll(() => api.metadataSamplesPayload?.sample_limit).toBe(0);
  await expect.poll(() => api.commentGeneratePayload?.sample_text).toBe("");

  await page.route("**/api/nl2sql/metadata-samples", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "取得失敗" }),
    })
  );
  await page.goto("/annotation-management");
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByLabel("構造情報")).toHaveValue(/OBJECT: APP\.INVOICES/);
  const annotationInputPanel = page.locator("#annotation-management-panel-input");
  await expect(annotationInputPanel).toBeVisible();
  await annotationInputPanel.getByRole("button", { name: "SQL 生成" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
});

test("件数の数値の欄は、空にしても入力中の文字を残し、範囲内の整数になったときだけ値を変える", async ({ page }) => {
  const api = await mockNl2SqlApi(page);
  await page.setViewportSize({ width: 375, height: 812 });

  // コメント管理のサンプル件数（0〜100）。空にしても即 0 にせず、範囲外は欄を離れると直前の値に戻す。
  await page.goto("/comment-management");
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  const sampleLimit = page.getByLabel("サンプル件数");
  await sampleLimit.fill("");
  await expect(sampleLimit).toHaveValue("");
  await sampleLimit.pressSequentially("25");
  await expect(sampleLimit).toHaveValue("25");
  await sampleLimit.fill("150");
  await expect(sampleLimit).toHaveValue("150");
  await sampleLimit.blur();
  await expect(sampleLimit).toHaveValue("25");
  await page.locator("#comment-management-panel-input").getByRole("button", { name: "SQL 生成" }).click();
  await expect.poll(() => api.metadataSamplesPayload?.sample_limit).toBe(25);
});

test("テーブル管理の再読込の失敗の「再試行」は案内を残したまま押したボタンだけを回し、取り直せたら案内を消す（#2034）", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.goto("/table-management");
  await expect(page.locator("#table-management-panel-list")).toBeVisible();
  await expect(page.locator("#table-management-panel-list").getByText("INVOICES", { exact: false }).first()).toBeVisible();

  let mode: "fail" | "gate" = "fail";
  let releaseRetry: () => void = () => undefined;
  const retryGate = new Promise<void>((resolve) => {
    releaseRetry = resolve;
  });
  await page.route("**/api/nl2sql/db-admin/objects?*", async (route) => {
    if (mode === "fail") {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "一時的に読み込めません。" }),
      });
      return;
    }
    await retryGate;
    await route.fallback();
  });
  const header = page.getByTestId("table-management-actions");
  const headerRefresh = header.getByRole("button", { name: "最新情報を取得" });
  if (await headerRefresh.isVisible()) {
    await headerRefresh.click();
  } else {
    await header.getByRole("button", { name: "その他の操作" }).click();
    await page.getByRole("menuitem", { name: "最新情報を取得" }).click();
  }
  const retry = page.getByTestId("table-management-notice-retry");
  await expect(retry).toBeVisible();
  mode = "gate";
  await retry.click();
  // 押しても案内は消えず、進行は案内の中のボタンの直下だけに出る（作業領域の先頭に重ねない）。
  const processing = page.getByTestId("table-management-notice-retry-processing");
  await expect(processing).toBeVisible();
  await expect(retry).toHaveAttribute("aria-busy", "true");
  await expect(page.getByTestId("table-management-workspace-processing")).toHaveCount(0);
  const buttonBox = await retry.boundingBox();
  const processingBox = await processing.boundingBox();
  expect(processingBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height - 1);
  releaseRetry();
  await expect(retry).toHaveCount(0);
  await expect(processing).toHaveCount(0);
});

test("SQL 生成の DB 構造の再取得の失敗の案内は再取得をやり直し、進行は案内の中、完了で案内を消す（#2034）", { tag: "@responsive" }, async ({ page }) => {
  await mockNl2SqlApi(page);
  let mode: "fail" | "pending" = "fail";
  let released = false;
  const refreshJob = (status: "pending" | "running" | "done") => ({
    job_id: "workbench-notice-refresh",
    status,
    mode: "full",
    phase: status === "done" ? "done" : "scanning",
    created_at: "2026-06-21T10:00:00.000Z",
    started_at: "2026-06-21T10:00:01.000Z",
    scanned_objects: 0,
    changed_objects: 0,
    deleted_objects: 0,
    catalog_version: 2,
    error_code: "",
  });
  await page.route("**/api/schema/refresh-jobs", (route) => {
    if (mode === "fail") {
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "DB 構造の再取得を開始できませんでした。" }),
      });
    }
    return fulfillJson(route, refreshJob("pending"));
  });
  await page.route("**/api/schema/refresh-jobs/workbench-notice-refresh", (route) =>
    fulfillJson(route, refreshJob(released ? "done" : "running"))
  );

  await page.goto("/query");
  await expect(page.getByRole("heading", { level: 1, name: "SQL 生成" })).toBeVisible();
  const headerAction = page.getByRole("button", { name: "DB 構造を再取得", exact: true }).first();
  if (await headerAction.isVisible()) {
    await headerAction.click();
  } else {
    await page.getByRole("button", { name: "その他の操作" }).first().click();
    await page.getByRole("menuitem", { name: "DB 構造を再取得", exact: true }).click();
  }

  // 失敗したのは再取得なので、案内のボタンは一覧の取り直しではなく再取得をやり直す。
  const noticeAction = page.getByTestId("query-page-notice-schema-refresh");
  await expect(noticeAction).toBeVisible();
  await expect(page.getByTestId("query-page-notice-retry")).toHaveCount(0);
  mode = "pending";
  await noticeAction.click();
  const processing = page.getByTestId("query-page-notice-schema-refresh-processing");
  await expect(processing).toContainText("DB 構造を再取得しています");
  await expect(page.getByTestId("schema-reference-refreshing")).toHaveCount(0);
  await expect(page.getByText("DB 構造の再取得を受け付けました。")).toHaveCount(0);
  const buttonBox = await noticeAction.boundingBox();
  const processingBox = await processing.boundingBox();
  expect(processingBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height - 1);

  released = true;
  await expect(noticeAction).toHaveCount(0);
  await expect(processing).toHaveCount(0);
  await expect(page.getByText("DB 構造を再取得しました。", { exact: true })).toHaveCount(1);
  await expectNoHorizontalScroll(page);
});

test("全 NL2SQL ルートのページヘッダーは 1440px / 375px で横方向に溢れない", { tag: "@responsive" }, async ({
  page,
}) => {
  test.setTimeout(120_000);
  // 未モック API を実バックエンド(proxy)へ漏らさないための fallback。実バックエンドが
  // 起動していると 401 が返り、AuthProvider がログイン画面へ戻してしまう。
  // Playwright の route は後勝ちなので、この後に登録する具体 mock が優先される。
  await page.route("**/api/**", (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ data: null, error_messages: ["e2e unmocked"], warning_messages: [] }),
    })
  );
  await mockDatabaseGateReady(page);
  await mockNl2SqlApi(page);
  const routes = [
    "/table-management",
    "/view-management",
    "/data-management",
    "/sample-data",
    "/comment-management",
    "/annotation-management",
    "/query",
    "/profiles",
    "/ontology-build",
    "/glossary-rules",
    "/global-rules",
    "/sql-to-question",
    "/direct-sql",
    "/admin-sql",
    "/feedback-management",
    "/question-classifier-models",
    "/history",
    "/evaluation",
  ];
  const mobile = test.info().project.name === "mobile-375";
  await page.setViewportSize(
    mobile ? { width: 375, height: 812 } : { width: 1440, height: 900 }
  );

  for (const path of routes) {
    await page.goto(path);
    const heading = page.getByRole("heading", { level: 1 }).first();
    await expect(heading, `${path} にページタイトルがある`).toBeVisible();
    const header = page.locator("header").filter({ has: heading }).first();
    await expect(header, `${path} にローカル PageHeader がある`).toBeVisible();
    const headerBox = await header.boundingBox();
    expect(headerBox, `${path} の PageHeader bounds`).not.toBeNull();
    expect(headerBox!.height, `${path} の PageHeader が首屏を占有しすぎない`).toBeLessThan(240);
    // 共有 PageHeader は data-page-action-kind を出さない。primary variant は bg-accent-emphasis で判別する。
    expect(
      await header.locator('[role="group"] button.bg-accent-emphasis:visible').count(),
      `${path} の primary は最大 1 件`
    ).toBeLessThanOrEqual(1);
    await expectNoHorizontalScroll(page);
    await expectLocalUiFonts(page);
    await page.screenshot({ path: test.info().outputPath(`font-scan-${path.slice(1)}.png`), fullPage: true });
  }
});

// Issue #298: 入力・閲覧位置と実行意思を別々に復元する。
test("workspace: 管理 SQL の草稿を往復と再読込で復元し確認と実行結果を区別する", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  let executions = 0;
  await page.route("**/api/nl2sql/db-admin/execute", (route) => {
    executions += 1;
    return fulfillJson(route, { executed: true, runtime: "oracle", committed: true, statements: [], warnings: [], timing });
  });
  await page.goto("/admin-sql");
  const input = adminSqlInput(page);
  const panel = page.getByTestId("nl2sql-admin-sql");
  const confirmation = panel.getByLabel("実行確認語");
  const sql = "UPDATE INVOICES SET TOTAL_AMOUNT = 100 WHERE INVOICE_ID = 1";
  await input.fill(sql);
  await confirmation.fill("ADMIN_EXECUTE");
  await input.fill(sql + ";");
  await expect(confirmation).toHaveValue("");
  await confirmation.fill("ADMIN_EXECUTE");
  await panel.getByRole("button", { name: "SQL 実行", exact: true }).click();
  await expect.poll(() => executions).toBe(1);
  await expect(panel.getByTestId("admin-sql-execution-activity")).toContainText("実行日時:");
  await (await openSidebarNav(page)).locator('a[href="/table-management"]').first().click();
  // data router の遷移は非同期。離れたことを確かめてから戻る（確認語の解除はページを離れたときに起きる）。
  await expect(page).toHaveURL(/\/table-management$/);
  await (await openSidebarNav(page)).locator('a[href="/admin-sql"]').first().click();
  await expect(input).toHaveValue(sql + ";");
  await expect(confirmation).toHaveValue("");
  await expect(panel.getByTestId("admin-sql-execution-activity")).toContainText("前回の実行結果");
  await input.fill(sql + " -- 編集");
  await expect(panel).toContainText("現在の入力は未実行です。");
  await panel.getByTestId("admin-sql-execution-activity").scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("workspace-previous-result.png") });
  await page.reload();
  await expect(input).toHaveValue(sql + " -- 編集");
  await expect(confirmation).toHaveValue("");
  await expect(panel.getByTestId("admin-sql-execution-activity")).toHaveCount(0);
  expect(executions).toBe(1);
  const stored = await page.evaluate(() => Object.entries(sessionStorage).filter(([key]) => key.startsWith("production-ready-nl2sql.draft.v1:")));
  expect(JSON.stringify(stored)).not.toContain("ADMIN_EXECUTE");
  expect(JSON.stringify(stored)).not.toContain('"committed"');
  await expectNoHorizontalScroll(page);
});

test("workspace: コメントの手編集を保持して対象の失効を再検証し自動実行しない", async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  await page.goto("/comment-management");
  await page.getByRole("option", { name: /INVOICES/ }).check();
  await page.getByRole("button", { name: "情報を取得", exact: true }).click();
  await expect(page.getByLabel("構造情報")).toHaveValue(/INVOICES/);
  await page.getByRole("button", { name: "SQL 生成", exact: true }).click();
  const panel = page.locator("#comment-management-panel-execute");
  const sql = panel.getByLabel("SQL(セミコロン区切りで複数文を入力可能)");
  await expect(sql).toHaveValue(/COMMENT ON/);
  await sql.fill("COMMENT ON TABLE APP.INVOICES IS '作業中の説明';");
  await panel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await (await openSidebarNav(page)).locator('a[href="/table-management"]').first().click();
  await page.route("**/api/nl2sql/db-admin/tables/INVOICES**", (route) => route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "対象が見つかりません。最新の対象を確認してください。" }) }));
  await (await openSidebarNav(page)).locator('a[href="/comment-management"]').first().click();
  await expect(sql).toHaveValue("COMMENT ON TABLE APP.INVOICES IS '作業中の説明';");
  await expect(page.getByText(/対象が見つかりません。最新の対象を確認してください。/).first()).toBeVisible();
  await expect(panel.getByLabel("実行確認語")).toHaveValue("");
  await panel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  await expect(panel.getByRole("button", { name: "SQL 実行", exact: true })).toBeDisabled();
  expect(api.statementsPayload).toBeNull();
  await page.screenshot({ path: testInfo.outputPath("workspace-target-unavailable.png") });
  await page.reload();
  await expect(sql).toHaveValue("COMMENT ON TABLE APP.INVOICES IS '作業中の説明';");
  await expect(panel.getByLabel("実行確認語")).toHaveValue("");
  await expectNoHorizontalScroll(page);
});

test("workspace: 一覧の検索条件は戻る・進む・再読込でも保持する", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.goto("/table-management");
  const search = page.getByRole("searchbox", { name: "検索", exact: true });
  await search.fill("INVOICES");
  await page.getByTestId("list-detail-table-management-list").getByLabel("所有者", { exact: true }).fill("APP");
  await (await openSidebarNav(page)).locator('a[href="/view-management"]').first().click();
  await page.goBack();
  await expect(search).toHaveValue("INVOICES");
  await expect(page.getByTestId("list-detail-table-management-list").getByLabel("所有者", { exact: true })).toHaveValue("APP");
  await page.goForward();
  await (await openSidebarNav(page)).locator('a[href="/table-management"]').first().click();
  await expect(search).toHaveValue("INVOICES");
  await page.reload();
  await expect(search).toHaveValue("INVOICES");
  await expect(page.getByTestId("list-detail-table-management-list").getByLabel("所有者", { exact: true })).toHaveValue("APP");
  await expectNoHorizontalScroll(page);
  // 入力してから絞り込みの確定（debounce）の前に再読込しても、入力した検索語を失わない（pagehide で確定する。#3385）。
  await search.fill("INVOICE");
  await page.reload();
  await expect(search).toHaveValue("INVOICE");

  // 選んだテーブルは URL（?table=）に持ち、サイドナビで離れて戻っても同じ対象を開く（keep-alive。#2281）。
  await page.getByRole("button", { name: "APP.INVOICES を表示" }).click();
  await expect(page).toHaveURL(/\?table=APP\.INVOICES$/);
  await (await openSidebarNav(page)).locator('a[href="/view-management"]').first().click();
  await expect(page).toHaveURL(/\/view-management$/);
  await (await openSidebarNav(page)).locator('a[href="/table-management"]').first().click();
  await expect(page).toHaveURL(/\/table-management\?table=APP\.INVOICES$/);
  await expect(page.getByTestId("table-management-detail-header")).toContainText("APP.INVOICES");
});

test("workspace: DB context とアカウントを跨いで SQL 草稿と確認を流用しない", async ({ page }) => {
  await mockNl2SqlApi(page);
  let database = "database-a";
  let user = systemAdminMe;
  await page.route("**/api/ready/database", (route) => fulfillJson(route, { status: "ok", check: "ok", detail: null, context_id: database }));
  await page.route("**/api/auth/me", (route) => fulfillJson(route, user));
  await page.goto("/admin-sql");
  await adminSqlInput(page).fill("DELETE FROM APP.INVOICES WHERE INVOICE_ID = 1");
  database = "database-b";
  await page.reload();
  await expect(adminSqlInput(page)).toHaveValue("");
  await adminSqlInput(page).fill("SELECT 2 FROM DUAL");
  database = "database-a";
  await page.reload();
  await expect(adminSqlInput(page)).toHaveValue("DELETE FROM APP.INVOICES WHERE INVOICE_ID = 1");
  await expect(page.getByTestId("nl2sql-admin-sql").getByLabel("実行確認語")).toHaveValue("");
  user = { ...systemAdminMe, user_uuid: "other-user", login_user_id: "OTHER" };
  await page.reload();
  await expect(adminSqlInput(page)).toHaveValue("");
  await adminSqlInput(page).fill("SELECT 3 FROM DUAL");
  await page.evaluate(() => window.dispatchEvent(new Event("app-auth-unauthorized")));
  await expect(page).toHaveURL(/\/login/);
  expect(await page.evaluate(() => Object.keys(sessionStorage).filter((key) => key.startsWith("production-ready-nl2sql.draft.v1:")))).toEqual([]);
});

test("workspace: 一時保存不可を明示し再読込で入力を失う前に確認する", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.addInitScript(() => {
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key: string, value: string) {
      if (key.startsWith("production-ready-nl2sql.draft.v1:")) throw new DOMException("quota", "QuotaExceededError");
      setItem.call(this, key, value);
    };
  });
  await page.goto("/admin-sql");
  await adminSqlInput(page).fill("SELECT 1 FROM DUAL");
  await expect(page.getByText(/このタブへの下書き保存ができません/)).toBeVisible();
  const dialog = page.waitForEvent("dialog");
  await page.evaluate(() => { setTimeout(() => window.location.reload(), 0); });
  const prompt = await dialog;
  expect(prompt.type()).toBe("beforeunload");
  await prompt.dismiss();

  await expect(adminSqlInput(page)).toHaveValue("SELECT 1 FROM DUAL");
});


test("sql to question roundtrip uses the edited structure and preserves drafts on failure and reload", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  let payload: Record<string, unknown> | null = null;
  let fail = false;
  let calls = 0;
  let executeCalls = 0;
  page.on("request", (request) => { if (/\/api\/nl2sql\/(execute|jobs)$/.test(request.url())) executeCalls += 1; });
  await page.route("**/api/nl2sql/reverse/sql", async (route) => {
    calls += 1;
    payload = route.request().postDataJSON();
    if (fail) return route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "許可されていない表を参照しています。" }) });
    return fulfillJson(route, { sql: "SELECT TOTAL_AMOUNT FROM APP.INVOICES WHERE TOTAL_AMOUNT >= 100", explanation: "100 以上の請求金額を取得します。", warnings: [] });
  });
  await page.goto("/sql-to-question");
  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await page.getByRole("button", { name: "SQL 分析・質問生成" }).click();
  const editor = page.getByRole("textbox", { name: "再生成に使う SQL 論理構造" });
  await expect(editor).toHaveValue(/SELECT: 請求金額/);
  const edited = "SELECT: 請求金額\nFROM: INVOICES\nWHERE: 請求金額 >= 100";
  await editor.fill(edited);
  const generate = page.getByRole("button", { name: "論理構造から SQL を生成" });
  await generate.focus();
  await page.keyboard.press("Enter");
  const output = page.getByRole("region", { name: "再生成 SQL", exact: true });
  await expect(output).toContainText("WHERE TOTAL_AMOUNT >= 100");
  await expect(output).toContainText("未実行");
  expect(payload).toEqual({ logical_structure: edited, profile_id: "default", use_glossary: false });
  await editor.fill(edited + " AND 請求金額 < 500");
  await expect(output).toContainText("変更前の生成結果");
  fail = true;
  await generate.click();
  await expect(page.getByRole("alert")).toContainText("許可されていない表");
  await expect(editor).toHaveValue(edited + " AND 請求金額 < 500");
  await expect(output).toContainText("WHERE TOTAL_AMOUNT >= 100");
  await expectNoHorizontalScroll(page);
  await output.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("sql-structure-roundtrip.png"), fullPage: true });
  await page.getByRole("region", { name: "質問候補", exact: true }).scrollIntoViewIfNeeded();
  await expect(page.getByText("論理構造が編集されています。この質問候補は編集前の SQL から生成したものです。")).toBeVisible();
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await page.reload();
  await expect(editor).toHaveValue(edited + " AND 請求金額 < 500");
  await expect(generate).toBeEnabled();
  expect(calls).toBe(2);
  expect(executeCalls).toBe(0);
  await editor.fill("");
  await expect(editor).toBeVisible();
  // 空の論理構造は押せないボタンだけにせず、押したときに欄の直下へ理由を出す（#541）。
  await expect(generate).toBeEnabled();
  await generate.click();
  await expect(editor).toHaveAccessibleDescription("再生成に使う SQL 論理構造を入力してください。");
  await expect(editor).toBeFocused();
  expect(calls).toBe(2);
  await editor.fill(edited);
  await expect(editor).not.toHaveAttribute("aria-invalid", "true");
  await expect(generate).toBeEnabled();
});


test("sql to question merges candidates after structure and restores the legacy result tab", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  await page.goto("/sql-to-question");
  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await page.getByRole("button", { name: "SQL 分析・質問生成" }).click();
  const panel = page.locator("#sql-to-question-panel-structure");
  const candidates = panel.getByRole("region", { name: "質問候補", exact: true });
  const editor = panel.getByRole("textbox", { name: "再生成に使う SQL 論理構造" });
  await expect(page.getByRole("tab")).toHaveText(["SQL入力・生成", "SQL分析・質問候補"]);
  await expect(candidates).toContainText("請求金額を条件付きで一覧確認したい");
  await expect(page.getByRole("button", { name: "質問候補を確認" })).toHaveCount(0);
  expect(await candidates.evaluate((element) => {
    const structure = document.getElementById("sql-to-question-structure-input");
    return !!structure && !!(structure.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING)
      && element.parentElement?.lastElementChild === element;
  })).toBe(true);
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("sql-analysis-tabs.png") });
  await candidates.scrollIntoViewIfNeeded();
  await candidates.screenshot({ path: testInfo.outputPath("sql-analysis-candidates.png") });

  const draft = "SELECT: 請求金額\nFROM: INVOICES\nWHERE: 請求金額 >= 100";
  await editor.fill(draft);
  await page.getByRole("tab", { name: "SQL入力・生成" }).click();
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(editor).toHaveValue(draft);
  await expect.poll(() => page.evaluate(() => {
    const prefix = "production-ready-nl2sql.draft.v1:";
    const key = Object.keys(sessionStorage).find((key) => {
      if (!key.startsWith(prefix) || key.endsWith("owner")) return false;
      const parts = JSON.parse(key.slice(prefix.length)) as string[];
      return parts[2] === "/sql-to-question" && parts[3] === "activePanel";
    });
    if (!key) return false;
    const saved = JSON.parse(sessionStorage.getItem(key)!);
    sessionStorage.setItem(key, JSON.stringify({ ...saved, value: "result" }));
    return true;
  })).toBe(true);
  let generationRequests = 0;
  page.on("request", (request) => { if (/\/api\/nl2sql\/reverse\/(deep|sql)$/.test(request.url())) generationRequests += 1; });
  await page.reload();
  await expect(page.getByRole("tab", { name: "SQL分析・質問候補" })).toHaveAttribute("aria-selected", "true");
  await expect(editor).toHaveValue(draft);
  await expect(candidates).toContainText("請求金額を条件付きで一覧確認したい");
  await expect(candidates).toContainText("前回の実行結果");
  await expect(candidates).toContainText("実行日時:");
  await expect(candidates).toContainText("論理構造が編集されています");
  await expect(page.getByRole("button", { name: "論理構造から SQL を生成" })).toBeEnabled();
  expect(generationRequests).toBe(0);
  await expectNoHorizontalScroll(page);
});

test("SELECT SQL の上限だけの入力をクリアでき、上限変更は未実行と表示する", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  await page.goto("/direct-sql");
  const limit = page.getByLabel("取得件数上限");
  const clear = page.getByRole("button", { name: "SQL 入力・結果をリセット", exact: true });
  await expect(clear).toHaveText("SQL 入力・結果をリセット");
  await expect(limit).toHaveValue("100");
  await limit.fill("200");
  await page.reload();
  await expect(limit).toHaveValue("200");
  await expect(clear).toBeEnabled();
  await clear.press("Enter");
  await expect(limit).toHaveValue("100");
  await directSqlInput(page).fill("SELECT CUSTOMER_NAME FROM INVOICES");
  await limit.fill("100");
  let calls = 0;
  page.on("request", (request) => { if (request.url().endsWith("/api/nl2sql/execute")) calls += 1; });
  await page.getByRole("button", { name: "SQL 実行", exact: true }).click();
  const activity = page.getByTestId("direct-sql-execution-activity");
  await expect(activity).toHaveAttribute("data-execution-activity-status", "success");
  await limit.fill("200");
  await expect(activity).toContainText("現在の入力は未実行です");
  await expect(page.getByTestId("query-results-meta")).toContainText("取得上限 100 件");
  await activity.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("direct-sql-limit-changed.png") });
  await limit.fill("100");
  await expect(activity).not.toContainText("現在の入力は未実行です");
  expect(calls).toBe(1);
});

test("自動判定中はクエリの競合操作を停止し、失敗後に再試行できる", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  await page.goto("/query");
  const input = nl2sqlQuestionInput(page);
  await input.fill("従業員一覧を取得");
  await page.waitForTimeout(800);
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/nl2sql/recommend-profile", async (route) => {
    await pending;
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "分類サービスを利用できません" }) });
  });
  await page.getByRole("button", { name: "プロファイルを自動判定", exact: true }).click();
  await page.screenshot({ path: testInfo.outputPath("profile-detect-pending.png") });
  try {
    await expect(input).toBeDisabled();
    await expect(page.getByRole("button", { name: "回答前の確認", exact: true })).toBeDisabled();
    await expect(page.locator("#nl2sql-profile-select")).toBeDisabled();
    await expect(page.getByRole("button", { name: "SQL を生成して実行", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "新しい質問を開始", exact: true })).toBeDisabled();
  } finally { release(); }
  await expect(input).toBeEnabled();
  await expect(input).toHaveValue("従業員一覧を取得");
  await expect(page.getByRole("button", { name: "プロファイルを自動判定", exact: true })).toBeEnabled();
});

test("分類結果は入力変更時に前回結果を明示し再予測失敗時に残さない", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  let fail = false;
  await page.route("**/api/nl2sql/classifier/predict", async (route) => {
    if (fail) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "予測サービスを利用できません" }) });
    return fulfillJson(route, { recommendation_source: "classifier", classifier_version: "classifier-001", predicted_category: "確認用カテゴリ", confidence: 0.91, candidates: [], warnings: [] });
  });
  await page.goto("/question-classifier-models?tab=test");
  await page.getByRole("tab", { name: "モデルテスト", exact: true }).click();
  const input = page.getByRole("textbox", { name: "テキスト", exact: true });
  await input.fill("最初の分類対象");
  const predict = page.getByRole("button", { name: "分類を試す", exact: true });
  await predict.click();
  await expect(page.getByText("信頼度 91%", { exact: true })).toBeVisible();
  await input.fill("次の分類対象");
  await expect(page.getByText(/現在の入力は未実行です/)).toBeVisible();
  await page.getByText(/現在の入力は未実行です/).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("classifier-previous-result.png") });
  fail = true;
  await predict.press("Enter");
  await expect(page.getByText(/予測サービスを利用できません/)).toBeVisible();
  await expect(page.getByText("信頼度 91%", { exact: true })).toHaveCount(0);
  await expect(input).toHaveValue("次の分類対象");
  await expect(predict).toBeEnabled();
});

test("フィードバックの前ページ取得失敗後も同じボタンで再試行できる", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  let rejectPrevious = false;
  let failed = false;
  await page.route(/\/api\/nl2sql\/feedback(?:\?.*)?$/, (route) => {
    const second = new URL(route.request().url()).searchParams.get("offset") === "25";
    if (!second && rejectPrevious) {
      rejectPrevious = false;
      failed = true;
      return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "一時的な一覧取得エラー" }) });
    }
    return fulfillJson(route, { items: [{ ...historyItem, id: second ? "second" : "first", question: second ? "2ページ目の質問" : "1ページ目の質問" }], total: 26 });
  });
  await page.goto("/feedback-management?tab=appFeedback");
  const pagination = page.getByTestId("app-feedback-pagination");
  await pagination.getByRole("button", { name: "次へ", exact: true }).click();
  await expect(pagination).toContainText("2 / 2 ページ");
  rejectPrevious = true;
  const previous = pagination.getByRole("button", { name: "前へ", exact: true });
  await previous.click();
  await expect.poll(() => failed).toBe(true);
  await expect(page.getByText(/一時的な一覧取得エラー/)).toBeVisible();
  await expect(pagination).toContainText("2 / 2 ページ");
  await previous.press("Enter");
  await expect(pagination).toContainText("1 / 2 ページ");
  await expect(page.getByTestId("feedback-history-question")).toHaveText("1ページ目の質問");
  await pagination.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("feedback-page-recovered.png") });
});

test("管理 SQL の上限変更はクリアでき前回結果の条件と区別する", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  await page.goto("/admin-sql");
  const scope = page.getByTestId("nl2sql-admin-sql");
  const limit = scope.getByLabel("取得件数上限");
  const clear = scope.getByRole("button", { name: "SQL 入力・結果をリセット", exact: true });
  await expect(clear).toBeDisabled();
  await limit.fill("200");
  await expect(clear).toBeEnabled();
  await clear.press("Enter");
  await expect(limit).toHaveValue("100");
  await expect(clear).toBeDisabled();
  await adminSqlInput(scope).fill("SELECT CUSTOMER_NAME FROM INVOICES");
  let calls = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/api/nl2sql/db-admin/execute")) calls += 1;
  });
  await scope.getByRole("button", { name: "SQL 実行", exact: true }).click();
  const activity = scope.getByTestId("admin-sql-execution-activity");
  await expect(activity).toHaveAttribute("data-execution-activity-status", "success");
  await limit.fill("200");
  await expect(activity).toContainText("現在の入力は未実行です");
  await expect(scope.getByTestId("query-results-meta")).toContainText("取得上限 100 件");
  await activity.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("admin-limit-changed.png") });
  await limit.fill("100");
  await expect(activity).not.toContainText("現在の入力は未実行です");
  expect(calls).toBe(1);
  await adminSqlInput(scope).fill("");
  await expect(clear).toBeEnabled();
  await clear.click();
  await expect(activity).toHaveCount(0);
});

test("テーブル取込中は対象とファイルの変更を停止し失敗後に再試行できる", async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  await page.goto("/table-management");
  await clickPageHeaderAction(page, "table-management-actions", "Excel/CSV 取込(新規テーブル)");
  const panel = page.locator("#table-management-panel-import");
  const table = panel.getByLabel("Oracle 表名");
  const sheet = panel.getByLabel("Sheet 名");
  const file = panel.getByTestId("table-import-file-field-input");
  const consent = panel.getByLabel("実行確認語");
  const execute = panel.getByRole("button", { name: "取込を実行", exact: true });
  await table.fill("IMPORTED_ORDERS");
  await file.setInputFiles({ name: "orders.csv", mimeType: "text/csv", buffer: Buffer.from("ID\n1\n") });
  await expect(panel.getByText("選択中: orders.csv")).toBeVisible();
  await consent.fill("ADMIN_EXECUTE");
  await mockTabularPreview(page);
  await panel.getByTestId("table-import-preview-button").click();
  const gate = createRequestGate();
  await page.route("**/api/nl2sql/db-admin/import-tabular", async (route) => {
    await gate.promise;
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "取込サービスは一時停止中です" }) });
  });
  await execute.click();
  try {
    await expect(table).toBeDisabled();
    await expect(sheet).toBeDisabled();
    await expect(file).toBeDisabled();
    await expect(consent).toBeDisabled();
    await expect(panel.getByRole("button", { name: "取込ファイル選択を解除" })).toBeDisabled();
    await expect(panel.getByRole("button", { name: "取込設定・結果をリセット" })).toBeDisabled();
    await expect(execute).toBeDisabled();
    await page.screenshot({ path: testInfo.outputPath("table-import-pending.png") });
  } finally { gate.release(); }
  await expect(table).toBeEnabled();
  await expect(table).toHaveValue("IMPORTED_ORDERS");
  await expect(file).toBeEnabled();
  await expect(consent).toHaveValue("ADMIN_EXECUTE");
  await page.unroute("**/api/nl2sql/db-admin/import-tabular");
  await page.route("**/api/nl2sql/db-admin/import-tabular", (route) => fulfillJson(route, {
    columns: [{ source_name: "ID", column_name: "ID", data_type: "NUMBER", nullable: true }],
    executed: true, table_name: "IMPORTED_ORDERS", owner: "APP", qualified_name: "APP.IMPORTED_ORDERS", row_count: 1, mode: "create", warnings: [],
    ddl: "CREATE TABLE IMPORTED_ORDERS (ID NUMBER)", insert_sql: "INSERT INTO IMPORTED_ORDERS VALUES (1)", sample_rows: [{ ID: 1 }],
  }));
  await execute.press("Enter");
  // 取込先の表は所有者付きの修飾名で表示する（#556）。
  await expect(panel.getByTestId("table-import-result-table-name")).toHaveText("APP.IMPORTED_ORDERS");
  // 実行の状態・モードは API の内部値（executed / create）を出さず、文言で出す（#935）。
  const importResult = panel.getByTestId("table-import-result-panel");
  await expect(importResult.getByText("実行済み", { exact: true })).toBeVisible();
  await expect(importResult.getByText("新規テーブルを作成", { exact: true })).toBeVisible();
  await expect(importResult.getByText("executed", { exact: true })).toHaveCount(0);
  await expect(importResult.getByText("create", { exact: true })).toHaveCount(0);
  expect(api.importTabularPayload).toBeNull();
});

test("ビュー抽出の旧応答は選択変更後に表示せず再抽出失敗時も旧結果を残さない", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  await page.route("**/api/nl2sql/db-admin/objects?*", (route) => fulfillJson(route, {
    items: ["V_EMP_DEPT", "V_OTHER"].map((name) => ({ name, owner: "APP", qualified_name: `APP.${name}`, object_type: "view", row_count: null, comment: name })),
    total: 2, next_cursor: null, runtime: "oracle", refreshed_at: "2026-09-11T00:00:00Z",
  }));
  await page.route("**/api/nl2sql/db-admin/views/V_OTHER?*", (route) => fulfillJson(route, {
    name: "V_OTHER", owner: "APP", qualified_name: "APP.V_OTHER", object_type: "view", row_count: null, comment: "別ビュー",
    columns: [{ column_name: "ID", logical_name: "ID", data_type: "NUMBER", nullable: false, comment: "", sample_values: [] }],
    ddl: new URL(route.request().url()).searchParams.get("include_ddl") === "1" ? "CREATE VIEW V_OTHER AS SELECT ID FROM PROJECT" : "", warnings: [],
  }));
  const gate = createRequestGate();
  let calls = 0;
  await page.route("**/api/nl2sql/db-admin/extract-join-where", async (route) => {
    calls += 1;
    if (calls === 1) await gate.promise;
    if (calls === 3) {
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "抽出サービスを利用できません" }) });
      return;
    }
    await fulfillJson(route, { join_text: calls === 1 ? "OLD_VIEW_JOIN" : "CURRENT_VIEW_JOIN", where_text: "", source: "deterministic", warnings: [], prompt_profile: "sql_structure", structure_markdown: "構造" });
  });
  await page.goto("/view-management");
  await page.getByRole("button", { name: "APP.V_EMP_DEPT を表示" }).click();
  await clickPageHeaderAction(page, "view-management-actions", "JOIN/WHERE 条件抽出");
  await page.getByRole("button", { name: "AI で抽出", exact: true }).click();
  await expect.poll(() => calls).toBe(1);
  await page.getByTestId("view-management-back").click();
  // 1 ペイン（xl 未満）では選んだビューの詳細へ戻るので、詳細の「一覧へ戻る」で一覧を開く（#2281）。
  if (testInfo.project.name === "mobile-375") await page.getByTestId("view-management-detail-back").click();
  await page.getByRole("button", { name: "APP.V_OTHER を表示" }).click();
  await clickPageHeaderAction(page, "view-management-actions", "JOIN/WHERE 条件抽出");
  const response = page.waitForResponse("**/api/nl2sql/db-admin/extract-join-where");
  gate.release();
  await response;
  await expect(page.getByRole("button", { name: "AI で抽出", exact: true })).toBeEnabled();
  await expect(page.getByLabel("結合条件 (JOIN)")).toHaveCount(0);
  await page.getByRole("button", { name: "AI で抽出", exact: true }).click();
  await expect(page.getByLabel("結合条件 (JOIN)")).toHaveValue("CURRENT_VIEW_JOIN");
  await page.getByRole("button", { name: "AI で抽出", exact: true }).click();
  await expect(page.getByText(/抽出サービスを利用できません/)).toBeVisible();
  await expect(page.getByLabel("結合条件 (JOIN)")).toHaveCount(0);
});

test("データ取込中は対象・ファイル・モードを固定し失敗後に再試行できる", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  await page.goto("/data-management");
  await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)", exact: true }).click();
  const panel = page.locator("#data-management-panel-csv");
  const target = panel.getByRole("button", { name: "APP.INVOICES を選択" });
  await target.click();
  const file = panel.getByTestId("data-csv-file-field-input");
  await file.setInputFiles({ name: "orders.csv", mimeType: "text/csv", buffer: Buffer.from("ID\n1\n") });
  const confirmation = panel.getByLabel("実行確認語");
  await confirmation.fill("APP.INVOICES");
  const execute = panel.getByRole("button", { name: "アップロード実行", exact: true });
  const gate = createRequestGate();
  await page.route("**/api/nl2sql/db-admin/upload-csv", async (route) => {
    await gate.promise;
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "取込サービスは一時停止中です" }) });
  });
  await execute.click();
  try {
    await expect(target).toBeDisabled();
    await expect(file).toBeDisabled();
    await expect(panel.getByTestId("data-csv-mode-field").getByRole("combobox")).toBeDisabled();
    await expect(confirmation).toBeDisabled();
    await expect(panel.getByRole("button", { name: "取込ファイル選択を解除" })).toBeDisabled();
    await expect(execute).toBeDisabled();
    await page.screenshot({ path: testInfo.outputPath("data-upload-pending.png") });
  } finally { gate.release(); }
  await expect(target).toBeEnabled();
  await expect(confirmation).toHaveValue("APP.INVOICES");
  await expect(execute).toBeEnabled();
  await page.unroute("**/api/nl2sql/db-admin/upload-csv");
  let retryPayload: Record<string, unknown> | null = null;
  await page.route("**/api/nl2sql/db-admin/upload-csv", (route) => {
    retryPayload = route.request().postDataJSON();
    return fulfillJson(route, { executed: true, runtime: "oracle", table_name: "INVOICES", mode: "insert", row_count: 1, success_count: 1, error_count: 0, matched_columns: ["ID"], unmatched_csv_columns: [], row_errors: [], sample_rows: [], warnings: [] });
  });
  await execute.press("Enter");
  await expect.poll(() => retryPayload).toMatchObject({ table_name: "INVOICES", owner: "APP", mode: "insert", confirmation: "APP.INVOICES", filename: "orders.csv" });
  // 実行の状態・runtime・モードは API の内部値を出さず、文言で出す（#936）。
  const uploadResult = panel.getByRole("region", { name: "表形式アップロード結果" });
  await expect(uploadResult.getByText("実行済み", { exact: true })).toBeVisible();
  await expect(uploadResult.getByText("Oracle", { exact: true })).toBeVisible();
  await expect(uploadResult.getByText("INSERT(追記)", { exact: true })).toBeVisible();
  await expect(uploadResult.getByText(/^(executed|oracle|insert)$/)).toHaveCount(0);
});

test("データの管理は CSV アップロードの失敗を danger の表示で原因とともに出す", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/db-admin/upload-csv");
  // upload-csv は Oracle のエラーを例外にせず、executed: false と warnings で返す。
  await page.route("**/api/nl2sql/db-admin/upload-csv", (route) =>
    fulfillJson(route, {
      table_name: "APP.INVOICES",
      filename: "orders.csv",
      mode: "truncate_insert",
      matched_columns: ["ID"],
      unmatched_csv_columns: [],
      row_count: 1,
      success_count: 0,
      error_count: 0,
      row_errors: [],
      hint: "",
      executed: false,
      runtime: "oracle",
      sample_rows: [{ ID: "1" }],
      warnings: ["ORA-00942: table or view does not exist Help: https://docs.oracle.com/error-help/db/ora-00942/"],
      timing,
    })
  );
  await page.goto("/data-management");
  await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)" }).click();
  const panel = page.locator("#data-management-panel-csv");
  await expect(panel.getByTestId("data-csv-table-list").getByText("APP.INVOICES", { exact: true })).toBeVisible();
  await panel.getByTestId("data-csv-file-field-input").setInputFiles({
    name: "orders.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("ID\n1\n"),
  });
  await panel.getByLabel("実行確認語").fill("APP.INVOICES");
  await panel.getByRole("button", { name: "アップロード実行", exact: true }).click();

  const uploadResult = panel.getByRole("region", { name: "表形式アップロード結果" });
  await expect(uploadResult.getByText("未実行", { exact: true })).toBeVisible();
  await expect(uploadResult.getByText("DELETE & INSERT(全置換)", { exact: true })).toBeVisible();
  await expect(uploadResult.getByText(/^(not executed|oracle|truncate_insert)$/)).toHaveCount(0);
  // 失敗は warning ではなく danger の Banner で、1 文目に何が起きたか・原因・次の操作を出す（messaging.md §10）。
  const failure = uploadResult.getByRole("alert").filter({ hasText: "表形式データをアップロードできませんでした。" });
  await expect(failure).toBeVisible();
  await expect(failure).toContainText("ORA-00942: table or view does not exist");
  await expect(failure).toContainText("参照先のテーブルまたはビューが存在しない");
  await expect(failure.getByRole("link", { name: "Oracle エラーヘルプを開く" })).toBeHidden();
  await expectNoHorizontalScroll(page);
  await uploadResult.screenshot({ path: test.info().outputPath("csv-upload-failure.png") });
});

test("データの管理は切り詰めの Oracle のエラーの原因をダイアログに出す", async ({ page }) => {
  await mockNl2SqlApi(page);
  await page.unroute("**/api/nl2sql/db-admin/truncate-table");
  // 切り詰めは部分成功の経路で実行し、Oracle のエラーは文の error_message にだけ入る（warnings は空）。
  await page.route("**/api/nl2sql/db-admin/truncate-table", (route) =>
    fulfillJson(route, {
      executed: false,
      runtime: "oracle",
      execution_context: "admin_control_plane",
      select_result: null,
      statements: [
        {
          index: 1,
          statement_type: "TRUNCATE",
          status: "error",
          sql: 'TRUNCATE TABLE "APP"."INVOICES"',
          row_count: null,
          message: "",
          elapsed_ms: 0,
          error_message: "ORA-02266: 表には有効な外部キーによって参照される一意キー/主キーが含まれています",
        },
      ],
      committed: false,
      rolled_back: true,
      warnings: [],
      timing,
    })
  );
  await page.goto("/data-management");
  const preview = page.locator("#data-management-panel-preview");
  await preview.getByRole("button", { name: "APP.INVOICES を選択" }).click();
  // 選択は URL が持つ（375px の 1 ペインでは一覧が隠れるので、行の aria-current ではなく URL と詳細の見出しで確かめる。#2315）。
  await expect(page).toHaveURL(/[?&]object=APP\.INVOICES/);
  await expect(page.getByTestId("data-preview-detail-header")).toContainText("APP.INVOICES");
  await clickObjectDetailAction(page, "data-preview-results-actions", "APP.INVOICES のデータを空にする");
  const dialog = page.getByRole("dialog", { name: "TRUNCATE TABLE の確認" });
  await dialog.getByLabel("実行確認語").fill("APP.INVOICES");
  await dialog.getByRole("button", { name: "データを空にする" }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "データを空にできませんでした。ORA-02266: 表には有効な外部キーによって参照される一意キー/主キーが含まれています"
  );
  await expect(dialog.getByLabel("実行確認語")).toHaveValue("APP.INVOICES");
});

for (const mode of ["comment", "annotation"] as const) {
  for (const phase of ["samples", "generation"] as const) {
    test(`${mode} SQL生成の${phase}待機中に入力が変われば旧応答を破棄する`, async ({ page }) => {
      await mockNl2SqlApi(page);
      await page.goto(`/${mode}-management`);
      await page.getByRole("option", { name: /INVOICES/ }).check();
      await page.getByRole("button", { name: "情報を取得", exact: true }).click();
      await expect(page.getByLabel("構造情報")).toHaveValue(/INVOICES/);
      const gate = createRequestGate();
      const generationPath = `/api/nl2sql/${mode === "comment" ? "comments" : "annotations"}/generate-sql`;
      const delayedPath = phase === "samples" ? "/api/nl2sql/metadata-samples" : generationPath;
      let calls = 0;
      page.on("request", (request) => { if (request.url().endsWith(generationPath)) calls += 1; });
      await page.route(`**${delayedPath}`, async (route) => { await gate.promise; await route.fallback(); });
      const request = page.waitForRequest(`**${delayedPath}`);
      await page.getByRole("button", { name: "SQL 生成", exact: true }).click();
      await request;
      if (phase === "samples") {
        await page.getByRole("tab", { name: "対象選択", exact: true }).click();
        await page.getByRole("option", { name: /INVOICES/ }).uncheck();
      } else {
        await page.getByRole("tab", { name: "入力確認・SQL生成", exact: true }).click();
        await page.getByLabel("追加入力").fill("入力を変更したので旧結果を採用しない");
      }
      const response = page.waitForResponse(`**${delayedPath}`);
      gate.release();
      await response;
      await page.getByRole("tab", { name: "SQL実行", exact: true }).click();
      await expect(page.getByTestId(`${mode}-management-execute-result-detail-skeleton`)).toHaveCount(0);
      await expect(page.getByLabel("SQL(セミコロン区切りで複数文を入力可能)")).toHaveValue("");
      if (phase === "samples") expect(calls).toBe(0);
      await expect(page.getByRole("button", { name: "SQL 実行", exact: true })).toBeDisabled();
    });
  }
}

for (const material of [
  { path: "/glossary-rules", fileId: "glossary-rules-panel-heading-file-input", exportLabel: "用語・同義語 Excel 出力", importPath: "terms", preview: "glossary-terms-preview" },
  { path: "/global-rules", fileId: "global-rules-file-input", exportLabel: "共通ルール Excel 出力", importPath: "rules", preview: "global-rules-preview" },
]) {
  test(`${material.path} の読込と取込は競合せず失敗後も再試行できる`, async ({ page }) => {
    await mockNl2SqlApi(page);
    const readGate = createRequestGate();
    await page.route("**/api/nl2sql/legacy-learning-material", async (route) => {
      await readGate.promise;
      await fulfillJson(route, { glossary: { OLD: "古い定義" }, rules: ["古いルール"] });
    });
    await page.goto(material.path);
    const file = page.getByTestId(material.fileId);
    const refresh = page.getByRole("button", { name: "最新情報を取得", exact: true });
    const download = page.getByRole("button", { name: material.exportLabel, exact: true });
    try {
      await expect(file).toBeDisabled();
      await expect(download).toBeDisabled();
      // 初回の読込のスピナーは「最新情報を取得」の 1 つだけ（一覧の Skeleton は経過時間だけ。#416）。
      await expectSingleSpinner(page, refresh);
    } finally { readGate.release(); }
    await expect(file).toBeEnabled();
    const importGate = createRequestGate();
    let attempts = 0;
    await page.route(`**/api/nl2sql/legacy-learning-material/${material.importPath}/import`, async (route) => {
      attempts += 1;
      if (attempts === 1) {
        await importGate.promise;
        await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "取込を再試行してください" }) });
      } else {
        await fulfillJson(route, { glossary: { NEW: "新しい定義" }, rules: ["新しいルール"] });
      }
    });
    const workbook = { name: "replacement.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: Buffer.from("fixture") };
    await file.setInputFiles(workbook);
    await confirmMaterialReplace(page);
    try {
      await expect(refresh).toBeDisabled();
      await expect(file).toBeDisabled();
      await expect(download).toBeDisabled();
      // 取込のスピナーはファイル選択の 1 つだけ。出力のボタンは無効にするだけで回さない（#416）。
      await expectSingleSpinner(page, page.getByTestId(material.fileId.replace(/-input$/u, "-dropzone")));
      await expect(download).not.toHaveAttribute("aria-busy", "true");
    } finally { importGate.release(); }
    await expect(file).toBeEnabled();
    await expect(page.getByText("取込を再試行してください", { exact: true })).toBeVisible();
    await file.setInputFiles(workbook);
    await confirmMaterialReplace(page);
    await expect(page.getByTestId(material.preview)).toContainText("新しい");
    await expect(page.getByTestId(material.preview)).not.toContainText("古い");
    await expect(refresh).toBeEnabled();
    expect(attempts).toBe(2);
  });
}

test("サンプルの操作・対象変更で確認を解除し実行中は競合操作を停止する", async ({ page }) => {
  const api = await mockNl2SqlApi(page);
  await page.goto("/sample-data");
  const confirmation = page.getByLabel("実行確認語");
  const step = page.getByRole("combobox", { name: /^取り込み対象/ });
  await confirmation.fill("ADMIN_EXECUTE");
  await page.getByRole("tab", { name: "削除実行", exact: true }).click();
  await expect(confirmation).toHaveValue("");
  await expect(page.getByRole("button", { name: "削除実行", exact: true })).toBeDisabled();
  await confirmation.fill("ADMIN_EXECUTE");
  await page.getByRole("tab", { name: "取り込み実行", exact: true }).press("Home");
  await expect(confirmation).toHaveValue("");
  await confirmation.fill("ADMIN_EXECUTE");
  await chooseSelectFieldOption(step, "tables");
  await expect(confirmation).toHaveValue("");
  await confirmation.fill("ADMIN_EXECUTE");
  const gate = createRequestGate();
  let attempts = 0;
  await page.route("**/api/nl2sql/sample-data/import", async (route) => {
    attempts += 1;
    if (attempts === 1) {
      await gate.promise;
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "サンプル処理を再試行してください" }) });
    } else await route.fallback();
  });
  const execute = page.getByRole("button", { name: "取り込み実行", exact: true });
  await execute.click();
  try {
    await expect(page.getByRole("tab", { name: "削除実行", exact: true })).toBeDisabled();
    await expect(step).toBeDisabled();
    await expect(page.getByRole("combobox", { name: "サンプルデータの種類" })).toBeDisabled();
    await expect(confirmation).toBeDisabled();
    await expect(page.getByRole("button", { name: "最新情報を取得", exact: true })).toBeDisabled();
  } finally { gate.release(); }
  await expect(page.getByText("サンプル処理を再試行してください", { exact: true })).toBeVisible();
  await expect(step).toBeEnabled();
  await execute.press("Enter");
  await expect.poll(() => api.samplePayload).toMatchObject({ step: "tables", confirmation: "ADMIN_EXECUTE" });
  expect(attempts).toBe(2);
});

async function delayReviewFileReads(page: Page) {
  await page.addInitScript(() => {
    const original = File.prototype.arrayBuffer;
    File.prototype.arrayBuffer = async function () {
      if (this.name.startsWith("broken")) throw new Error("file access failed");
      const bytes = await original.call(this);
      if (this.name.startsWith("slow")) {
        await new Promise<void>((resolve) => {
          (window as unknown as { releaseReviewRead: () => void }).releaseReviewRead = resolve;
        });
      }
      return bytes;
    };
  });
}

async function releaseReviewFileRead(page: Page) {
  await page.waitForFunction(() => typeof (window as unknown as { releaseReviewRead?: unknown }).releaseReviewRead === "function");
  await page.evaluate(async () => {
    const state = window as unknown as { releaseReviewRead?: () => void };
    state.releaseReviewRead?.();
    delete state.releaseReviewRead;
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  });
}

for (const kind of ["table", "data"] as const) {
  test(`${kind} 取込ファイルの遅延読込はクリアや別ファイル選択を巻き戻さない`, async ({ page }) => {
    const api = await mockNl2SqlApi(page);
    await delayReviewFileReads(page);
    await page.goto(`/${kind}-management`);
    if (kind === "table") await clickPageHeaderAction(page, "table-management-actions", "Excel/CSV 取込(新規テーブル)");
    else await page.getByRole("tab", { name: "Excel/CSV アップロード(既存テーブル)", exact: true }).click();
    const panel = page.locator(kind === "table" ? "#table-management-panel-import" : "#data-management-panel-csv");
    if (kind === "table") await panel.getByLabel("Oracle 表名").fill("REVIEW_FILES");
    else await panel.getByRole("button", { name: "APP.INVOICES を選択" }).click();
    const file = panel.getByTestId(kind === "table" ? "table-import-file-field-input" : "data-csv-file-field-input");
    const consent = panel.getByLabel("実行確認語");
    const phrase = kind === "table" ? "ADMIN_EXECUTE" : "APP.INVOICES";
    const execute = panel.getByRole("button", { name: kind === "table" ? "取込を実行" : "アップロード実行", exact: true });
    const select = async (name: string, value: number) => file.setInputFiles({ name, mimeType: "text/csv", buffer: Buffer.from(`ID\n${value}\n`) });
    await select("old.csv", 1);
    await consent.fill(phrase);
    if (kind === "table") {
      await mockTabularPreview(page);
      await panel.getByTestId("table-import-preview-button").click();
    }
    await expect(execute).toBeEnabled();
    await select("slow.csv", 2);
    await expect(consent).toHaveValue("");
    await consent.fill(phrase);
    await expect(execute).toBeDisabled();
    await panel.getByRole("button", { name: "取込ファイル選択を解除", exact: true }).click();
    await releaseReviewFileRead(page);
    await expect(panel.getByText("選択中: slow.csv")).toHaveCount(0);
    await expect(execute).toBeDisabled();
    await select("slow-again.csv", 3);
    await select("new.csv", 4);
    await consent.fill(phrase);
    if (kind === "table") {
      await mockTabularPreview(page);
      await panel.getByTestId("table-import-preview-button").click();
    }
    await expect(execute).toBeEnabled();
    await releaseReviewFileRead(page);
    await execute.click();
    await expect.poll(() => kind === "table" ? api.importTabularPayload : api.csvUploadPayload).toMatchObject({ filename: "new.csv", content_base64: Buffer.from("ID\n4\n").toString("base64") });
    await expect(file).toBeEnabled();
    await select("broken.csv", 5);
    await expect(panel.getByText(/ファイルを読み込めませんでした/)).toBeVisible();
    await expect(execute).toBeDisabled();
    await select("retry.csv", 6);
    await consent.fill(phrase);
    if (kind === "table") {
      await mockTabularPreview(page);
      await panel.getByTestId("table-import-preview-button").click();
    }
    await expect(execute).toBeEnabled();
  });
}

test("SQL ファイルの遅延読込はクリアと再選択を巻き戻さない", async ({ page }) => {
  await mockNl2SqlApi(page);
  await delayReviewFileReads(page);
  await page.goto("/admin-sql");
  const sql = page.locator("#admin-sql-input");
  await sql.fill("SELECT 1 FROM DUAL");
  const file = page.getByTestId("sql-file-input-input");
  const select = async (name: string, value: number) => file.setInputFiles({ name, mimeType: "text/plain", buffer: Buffer.from(`SELECT ${value} FROM DUAL`) });
  await select("slow.sql", 2);
  await page.getByRole("button", { name: "SQL 入力・結果をリセット", exact: true }).click();
  await releaseReviewFileRead(page);
  await expect(sql).toHaveValue("");
  await select("slow-again.sql", 3);
  await select("new.sql", 4);
  await expect(sql).toHaveValue("SELECT 4 FROM DUAL");
  await releaseReviewFileRead(page);
  await expect(sql).toHaveValue("SELECT 4 FROM DUAL");
  await select("broken.sql", 5);
  await expect(page.getByText(/ファイルを読み込めませんでした/)).toBeVisible();
  await select("retry.sql", 6);
  await expect(sql).toHaveValue("SELECT 6 FROM DUAL");
});


test("論理構造の再生成不能理由を表示し編集内容を保持して再試行できる", async ({ page }) => {
  await mockNl2SqlApi(page);
  let calls = 0;
  await page.route("**/api/nl2sql/reverse/sql", async (route) => {
    calls += 1;
    if (calls === 1) return route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "対象の列名を論理構造に追加してください。" }) });
    await fulfillJson(route, { sql: "SELECT ID FROM APP.INVOICES", explanation: "指定された列を取得します。", source: "oci_enterprise_ai", warnings: [] });
  });
  await page.goto("/sql-to-question");
  await sqlToQuestionInput(page).fill("select * from INVOICES");
  await page.getByRole("button", { name: "SQL 分析・質問生成" }).click();
  const editor = page.getByRole("textbox", { name: "再生成に使う SQL 論理構造" });
  await editor.fill("請求の一覧を取得");
  const generate = page.getByRole("button", { name: "論理構造から SQL を生成" });
  await generate.press("Enter");
  await expect(page.getByRole("alert")).toContainText("対象の列名を論理構造に追加してください。");
  await expect(page.getByRole("alert")).not.toContainText("validation error");
  await expect(editor).toHaveValue("請求の一覧を取得");
  await editor.fill("APP.INVOICES の ID を取得");
  await generate.press("Enter");
  await expect(page.getByRole("region", { name: "再生成 SQL", exact: true })).toContainText("SELECT ID FROM APP.INVOICES");
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(calls).toBe(2);
  await expectNoHorizontalScroll(page);
});

test("フィードバック保存成功と履歴再取得失敗を分離し送信中のコメントを固定する", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  await page.goto("/query");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  await page.getByRole("button", { name: "SQL を生成して実行", exact: true }).click();
  const comment = page.getByLabel("利用者コメント（feedback_content）");
  await expect(comment).toBeVisible();
  await comment.fill("保存するコメント");
  const gate = createRequestGate();
  let posts = 0;
  await page.route("**/api/nl2sql/feedback", async (route) => {
    posts += 1;
    await gate.promise;
    await route.fallback();
  });
  await page.route("**/api/nl2sql/history**", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "履歴再取得テストエラー" }) }));
  const good = page.getByRole("button", { name: "良い", exact: true });
  await good.click();
  try {
    await expect(comment).toBeDisabled();
    await expect(good).toBeDisabled();
    await expect(page.getByRole("button", { name: "違う", exact: true })).toBeDisabled();
  } finally { gate.release(); }
  await expect(page.getByText("フィードバックは保存済みですが、履歴の更新に失敗しました。実行履歴で最新情報を取得してください。", { exact: true })).toBeVisible();
  await expect(page.getByText("履歴再取得テストエラー", { exact: true })).toHaveCount(0);
  await expect(comment).toBeEnabled();
  await expect(comment).toHaveValue("保存するコメント");
  expect(posts).toBe(1);
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("feedback-saved-refresh-warning.png") });
  await page.route("**/api/nl2sql/feedback", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "保存テストエラー" }) }));
  await good.press("Enter");
  await expect(page.getByText("保存テストエラー", { exact: true })).toBeVisible();
  await expect(comment).toHaveValue("保存するコメント");
  await expect(comment).toBeEnabled();
});

test("SELECT SQL の遅延ファイル読込は手入力を上書きしない", async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  await delayReviewFileReads(page);
  await page.goto("/direct-sql");
  const sql = directSqlInput(page);
  const file = page.getByTestId("sql-file-input-input");
  await file.setInputFiles({ name: "slow.sql", mimeType: "text/plain", buffer: Buffer.from("SELECT 2 FROM DUAL") });
  await sql.fill("SELECT 3 FROM DUAL");
  await releaseReviewFileRead(page);
  await expect(sql).toHaveValue("SELECT 3 FROM DUAL");
  await page.getByLabel("取得件数上限").fill("10");
  await page.getByRole("button", { name: "SQL 実行", exact: true }).press("Enter");
  await expect.poll(() => api.executePayload).toMatchObject({ sql: "SELECT 3 FROM DUAL", row_limit: 10 });
  await expect(file).toBeEnabled();
  await file.setInputFiles({ name: "new.sql", mimeType: "text/plain", buffer: Buffer.from("SELECT 4 FROM DUAL") });
  await expect(sql).toHaveValue("SELECT 4 FROM DUAL");
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("direct-sql-file-edit.png") });
});

test("SQL から質問のスキーマ参照は別 owner の同名表を区別する", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  const tables = ["A", "B"].map((owner) => ({ ...schemaCatalog.tables[0], owner, table_name: "EMPLOYEE", qualified_name: `${owner}.EMPLOYEE`, logical_name: `${owner} 部門の従業員`, columns: [{ ...schemaCatalog.tables[0].columns[0], column_name: `${owner}_ID`, logical_name: `${owner} 部門の識別子` }] }));
  let reversed = false;
  await page.route("**/api/schema/objects?*", (route) => fulfillJson(route, {
    items: (reversed ? [...tables].reverse() : tables).map((table) => ({ owner: table.owner, object_name: table.table_name, object_type: table.table_type, logical_name: table.logical_name, column_count: 1 })),
    next_cursor: null, total: 2, catalog_version: 1,
  }));
  await page.route("**/api/schema/objects/*/*", (route) => {
    const owner = new URL(route.request().url()).pathname.split("/").at(-2);
    return fulfillJson(route, { table: tables.find((table) => table.owner === owner), dependencies: [], catalog_version: 1 });
  });
  await page.goto("/sql-to-question");
  const checkTables = async () => {
    for (const owner of ["A", "B"]) {
      const card = page.locator("section").filter({ has: page.getByText(`${owner} 部門の従業員`, { exact: false }) }).last();
      await expect(card).toContainText(`${owner}.EMPLOYEE`);
      await expect(card).toContainText(`${owner} 部門の識別子`);
      await expect(card).not.toContainText(`${owner === "A" ? "B" : "A"} 部門の識別子`);
    }
  };
  await checkTables();
  reversed = true;
  await page.getByRole("button", { name: "最新情報を取得", exact: true }).click();
  await checkTables();
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("sql-question-qualified-schema.png") });
});

test("クエリ初期値だけを消費し他の URL 条件と編集後の草稿を保持する", async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  const params = new URLSearchParams({ question: "初期クエリ", engine: "enterprise_ai_direct", profile_id: "default", review: "keep" });
  await page.goto(`/query?${params}`);
  await expect(nl2sqlQuestionInput(page)).toHaveValue("初期クエリ");
  await expect(page).toHaveURL(/\/query\?review=keep$/);
  await nl2sqlQuestionInput(page).fill("編集済みのクエリ");
  await page.reload();
  await expect(nl2sqlQuestionInput(page)).toHaveValue("編集済みのクエリ");
  await expect(page).toHaveURL(/\/query\?review=keep$/);
  expect(api.jobPayload).toBeNull();
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("query-prefill-draft-reload.png") });
});

test("feedback refresh preserves drafts and pending review locks competing actions", async ({ page }, testInfo) => {
  // 一覧の絞り込みと管理者レビューの欄を同時に操作するので 2 ペインの幅で確かめる（xl 未満は 1 ペイン。#2283）。
  await page.setViewportSize({ width: 1280, height: 900 });
  await mockNl2SqlApi(page);
  // 選んだ履歴は URL の `?feedback=` で開く（先頭の行は自動で選ばない。#2283）。
  await page.goto("/feedback-management?feedback=hist-001");
  const comment = page.getByLabel("管理者レビューコメント（feedback_content）");
  await comment.fill("未保存のレビューを保持");
  await clickPageHeaderAction(page, "feedback-management-actions", "最新情報を取得");
  await expect(comment).toHaveValue("未保存のレビューを保持");
  await page.getByRole("tab", { name: "類似検索インデックス", exact: true }).click();
  await page.getByLabel("最大候補数", { exact: true }).fill("7");
  await clickPageHeaderAction(page, "feedback-management-actions", "最新情報を取得");
  await expect(page.getByLabel("最大候補数", { exact: true })).toHaveValue("7");
  await page.getByRole("tab", { name: "アプリ内フィードバック" }).click();
  await page.getByRole("link", { name: "学習候補で確認" }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(comment).toHaveValue("未保存のレビューを保持");
  const gate = createRequestGate();
  await page.route("**/api/nl2sql/feedback/admin-review", async (route) => {
    await gate.promise;
    await route.fulfill({ status: 503, json: { detail: "レビュー保存失敗" } });
  });
  await page.getByRole("button", { name: "フィードバック保存", exact: true }).click();
  await expect(comment).toBeDisabled();
  await expect(page.getByRole("tab", { name: "類似検索インデックス", exact: true })).toBeDisabled();
  gate.release();
  await expect(page.getByText("レビュー保存失敗", { exact: true })).toBeVisible();
  await expect(comment).toBeEnabled();
  await expect(comment).toHaveValue("未保存のレビューを保持");
  // 未保存の下書きがあるので確認を挟み、キャンセルすると絞り込みは変わらない（選んだ値にならないので表示名で選ぶ）。
  await withListToolbarFilters(page.getByTestId("feedback-app-filters"), async (filters) => {
    const ratingFilter = filters.getByRole("combobox", { name: "利用者評価", exact: true });
    await chooseSelectFieldOption(ratingFilter, { label: "違う" });
    await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル" }).click();
    await expectSelectFieldValue(ratingFilter, "all");
  });
  await expect(comment).toHaveValue("未保存のレビューを保持");
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("feedback-draft-protection.png"), fullPage: true });
  await page.unroute("**/api/nl2sql/feedback/admin-review");
  await page.route("**/api/nl2sql/feedback/admin-review", (route) => fulfillJson(route, {
    history_id: "hist-001", rating: "good", feedback_content: "未保存のレビューを保持",
  }));
  await page.getByRole("button", { name: "フィードバック保存", exact: true }).click();
  await expect(page.getByText("管理者レビューを保存し、類似検索に公開しました。", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "類似検索インデックス", exact: true }).click();
  await page.getByRole("button", { name: "設定保存", exact: true }).click();
  await expect(page.getByText("Feedback 類似検索設定を保存しました。", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "アプリ内フィードバック" }).click();
  await page.getByRole("link", { name: "学習候補で確認" }).click();
  await expect(page).toHaveURL(/question-classifier-models\?tab=candidates&history_id=hist-001/);
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  // 未保存の編集が無いときのリンクは `<a>` の文書の遷移になる（FormActionBar の href）。URL が変わった直後は
  // 新しい文書の script が未実行で styles も無いので、移動先の画面が描かれるまで待つ（afterEach の字体の確認が
  // 読み込み途中の文書を見ないため。#1738）。
  await expect(page.getByRole("heading", { level: 1, name: "質問分類モデル管理", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: "学習候補", exact: true })).toHaveAttribute("aria-selected", "true");
});

test("learning candidate failed previous page preserves the page for retry", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  let failPrevious = false;
  const requests: string[] = [];
  await page.route("**/api/nl2sql/classifier/training-candidates*", (route) => {
    const offset = new URL(route.request().url()).searchParams.get("offset") ?? "0";
    requests.push(offset);
    if (failPrevious && offset === "25") {
      failPrevious = false;
      return route.fulfill({ status: 503, json: { detail: "前ページ取得失敗" } });
    }
    const number = Number(offset) / 25 + 1;
    return fulfillJson(route, {
      items: [{ history_id: `history-${number}`, question: `候補ページ ${number}`,
        profile_id: "default", profile_name: "既定プロファイル", profile_category: "請求",
        feedback_rating: "good", feedback_comment: "", created_at: historyItem.created_at,
        status: "pending", training_example_id: "", conflict_profile_ids: [] }],
      total: 51, limit: 25, offset: Number(offset), has_next: number < 3,
      pending_count: 51, added_count: 0, attention_count: 0,
    });
  });
  await page.goto("/question-classifier-models?tab=candidates");
  const pagination = page.getByTestId("qcm-candidate-pagination");
  const next = pagination.getByRole("button", { name: "次へ" });
  const previous = pagination.getByRole("button", { name: "前へ" });
  await next.click();
  await expect(pagination).toContainText("2 / 3 ページ");
  await next.click();
  await expect(pagination).toContainText("3 / 3 ページ");
  failPrevious = true;
  await previous.click();
  await expect(page.getByText("前ページ取得失敗", { exact: true })).toBeVisible();
  const retry = page.getByTestId("management-panel-shell").getByRole("button", { name: "再試行" });
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("qcm-training-candidate")).toContainText("候補ページ 2");
  expect(requests.slice(-2)).toEqual(["25", "25"]);
  await previous.click();
  await expect(pagination).toContainText("1 / 3 ページ");
  await expect(previous).toBeDisabled();
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("classifier-cursor-recovery.png"), fullPage: true });
});

test("classifier training save locks edits and retains draft after failure", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  await page.goto("/question-classifier-models");
  await clickRowAction(page, "qcm-training-row-actions-example-001", "編集");
  const input = page.getByLabel("訓練データの質問", { exact: true });
  await input.fill("保存前の訓練データ");
  const gate = createRequestGate();
  await page.route("**/api/nl2sql/classifier/training-data/example-001", async (route) => {
    await gate.promise;
    await route.fulfill({ status: 503, json: { detail: "訓練データ保存失敗" } });
  });
  const row = page.getByTestId("qcm-training-data-table").locator("tbody tr").filter({ has: input });
  await row.getByRole("button", { name: "保存", exact: true }).click();
  await expect(input).toBeDisabled();
  await expect(row.getByRole("button", { name: "キャンセル", exact: true })).toBeDisabled();
  await expect(page.getByRole("tab", { name: "モデル学習", exact: true })).toBeDisabled();
  await expect(page.getByTestId("management-panel-shell").getByRole("button", { name: "訓練データの最新情報を取得" })).toBeDisabled();
  gate.release();
  await expect(page.getByText("訓練データ保存失敗", { exact: true })).toBeVisible();
  await expect(input).toHaveValue("保存前の訓練データ");
  await expect(input).toBeEnabled();
  await row.getByRole("button", { name: "キャンセル", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル", exact: true }).click();
  await expect(input).toHaveValue("保存前の訓練データ");
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("classifier-edit-protection.png"), fullPage: true });
  await page.getByRole("tab", { name: "学習候補", exact: true }).click();
  await page.getByRole("link", { name: "フィードバック管理で確認", exact: true }).first().click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.getByRole("alertdialog").getByRole("button", { name: "キャンセル", exact: true }).click();
  await page.getByRole("tab", { name: "訓練データ", exact: true }).click();
  await expect(input).toHaveValue("保存前の訓練データ");
  await page.unroute("**/api/nl2sql/classifier/training-data/example-001");
  await page.route("**/api/nl2sql/classifier/training-data/example-001", (route) => fulfillJson(route, {}));
  await row.getByRole("button", { name: "保存", exact: true }).click();
  await expect(input).toHaveCount(0);
});

for (const theme of ["light", "dark"] as const) {
  // #535: 一覧の絞り込みは入力に合わせて適用し、「絞り込み」ボタンを置かない。IME の変換中は問い合わせない。
  test(`learning candidate search applies as you type without a filter button and waits for IME commit (${theme})`, async ({ page }, testInfo) => {
    await page.addInitScript((value) => {
      localStorage.setItem("production-ready-nl2sql.ui", JSON.stringify({
        state: { sidebarCollapsed: false, collapsedSections: {}, theme: value }, version: 0,
      }));
    }, theme);
    await mockNl2SqlApi(page);
    const queries: string[] = [];
    await page.route("**/api/nl2sql/classifier/training-candidates*", (route) => {
      const url = new URL(route.request().url());
      const q = url.searchParams.get("q") ?? "";
      queries.push(q);
      const all = [
        { history_id: "history-1", question: "請求金額を確認" },
        { history_id: "history-2", question: "人事の申請件数" },
      ].filter((item) => !q || item.question.includes(q));
      return fulfillJson(route, {
        items: all.map((item) => ({ ...item,
          profile_id: "default", profile_name: "既定プロファイル", profile_category: "請求",
          feedback_rating: "good", feedback_comment: "", created_at: historyItem.created_at,
          status: "pending", training_example_id: "", conflict_profile_ids: [] })),
        total: all.length, next_cursor: "", pending_count: all.length, added_count: 0, attention_count: 0,
      });
    });
    await page.goto("/question-classifier-models?tab=candidates");
    const filters = page.getByTestId("qcm-candidate-filters");
    const search = filters.getByRole("searchbox", { name: "候補検索" });
    const candidates = page.getByTestId("qcm-training-candidate");
    await expect(candidates).toHaveCount(2);
    await expect(filters.locator("button:not([aria-haspopup=dialog])").filter({ hasText: /^絞り込み$/ })).toHaveCount(0); // 狭い画面のシートを開くボタン（#1933）は除く

    // 変換中の読みでは問い合わせず、確定した値で問い合わせる。
    await page.evaluate(() => {
      const input = document.querySelector<HTMLInputElement>('[data-testid="qcm-candidate-filters"] input[type="search"]')!;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      input.focus();
      input.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true, data: "" }));
      for (const step of ["j", "じ", "じん", "じんじ", "人事"]) {
        setter.call(input, step);
        input.dispatchEvent(new InputEvent("input", { bubbles: true, isComposing: true, data: step }));
      }
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, isComposing: true }));
    });
    await page.waitForTimeout(700);
    expect(queries.filter((q) => q !== "")).toEqual([]);
    await page.evaluate(() => {
      const input = document.querySelector<HTMLInputElement>('[data-testid="qcm-candidate-filters"] input[type="search"]')!;
      input.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: "人事" }));
    });
    await expect(candidates).toHaveCount(1);
    await expect(candidates).toContainText("人事の申請件数");
    expect(queries.filter((q) => q !== "")).toEqual(["人事"]);

    // 入力に合わせて絞り込む（Enter もボタンも押さない）。
    await search.fill("請求");
    await expect(candidates).toHaveCount(1);
    await expect(candidates).toContainText("請求金額を確認");
    await expectNoHorizontalScroll(page);
    await page.screenshot({ path: testInfo.outputPath(`qcm-candidate-search-${theme}.png`), fullPage: true });
  });
}

test("classifier refresh retains the applied candidate filter and current page", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  const requests: URL[] = [];
  await page.route("**/api/nl2sql/classifier/training-candidates*", (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    const second = url.searchParams.get("offset") === "25";
    return fulfillJson(route, {
      items: [{ history_id: second ? "history-26" : "history-1", question: second ? "請求 26" : "請求 1",
        profile_id: "default", profile_name: "既定プロファイル", profile_category: "請求",
        feedback_rating: "good", feedback_comment: "", created_at: historyItem.created_at,
        status: "pending", training_example_id: "", conflict_profile_ids: [] }],
      total: 51, pending_count: 51, added_count: 0, attention_count: 0,
    });
  });
  await page.goto("/question-classifier-models?tab=candidates");
  await page.getByLabel("候補検索", { exact: true }).fill("請求");
  // Enter は debounce を待たずにすぐ適用する（#535）。
  await page.getByLabel("候補検索", { exact: true }).press("Enter");
  await expect.poll(() => requests.at(-1)?.searchParams.get("q")).toBe("請求");
  const pagination = page.getByTestId("qcm-candidate-pagination");
  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(pagination).toContainText("2 / 3 ページ");
  await page.getByRole("button", { name: "最新情報を取得", exact: true }).click();
  await expect(page.getByText("最新の状態に更新しました。", { exact: true })).toBeVisible();
  await expect(page.getByTestId("qcm-training-candidate")).toContainText("請求 26");
  await expect(pagination).toContainText("2 / 3 ページ");
  expect(requests.at(-1)?.searchParams.get("offset")).toBe("25");
  expect(requests.at(-1)?.searchParams.get("q")).toBe("請求");
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("classifier-refresh-page.png"), fullPage: true });
  await pagination.getByRole("button", { name: "前へ" }).click();
  await expect(pagination).toContainText("1 / 3 ページ");
  expect(requests.at(-1)?.searchParams.get("q")).toBe("請求");
});

for (const questionFailed of [false, true]) {
  test(`sql to question displays SQL Assist logical structure and natural question when later generation ${questionFailed ? "fails" : "succeeds"}`, async ({ page }, testInfo) => {
    await mockNl2SqlApi(page);
    const analysis = "## 📊 SQL構造分析\n\n### 📋 SELECT句\n- *\n\n### 📁 FROM句\n- employee";
    const logical = analysis.replace("SQL構造分析", "SQL論理構造").replace("employee", "[従業員情報]");
    const warning = "業務質問の生成を完了できませんでした。完成した SQL 論理構造を表示しています。再試行してください。";
    await page.route("**/api/nl2sql/reverse/deep", (route) => fulfillJson(route, {
      question: "従業員情報を取得したい", explanation: "従業員を参照します。", referenced_tables: ["employee"],
      sql_structure: analysis, logical_structure: logical,
      logical_structure_items: [],
      source: questionFailed ? "deterministic" : "oci_enterprise_ai", warnings: questionFailed ? [warning] : [],
    }));
    let payload: Record<string, unknown> | null = null;
    await page.route("**/api/nl2sql/reverse/sql", (route) => {
      payload = route.request().postDataJSON();
      return fulfillJson(route, { sql: "select * from employee", explanation: "構造から再生成しました。", warnings: [] });
    });
    await page.goto("/sql-to-question");
    await sqlToQuestionInput(page).fill("select * from employee");
    await page.getByRole("button", { name: "SQL 分析・質問生成" }).click();
    const editor = page.getByRole("textbox", { name: "再生成に使う SQL 論理構造" });
    await expect(editor).toHaveValue(logical);
    await expect(page.getByTestId("sql-to-question-structure-list")).toHaveCount(0);
    await expect(page.getByText(/論理構造が編集されています/)).toHaveCount(0);
    if (questionFailed) await expect(page.getByText(warning).first()).toBeVisible();
    const candidate = page.getByRole("region", { name: "質問候補", exact: true });
    await expect(candidate).toContainText("従業員情報を取得したい");
    await expect(candidate).not.toContainText("oci_enterprise_ai");
    await expect(candidate).not.toContainText("SQL の処理手順");
    await expectNoHorizontalScroll(page);
    await page.screenshot({ path: testInfo.outputPath("sql-assist-analysis.png"), fullPage: true });
    await candidate.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("natural-question.png"), fullPage: true });
    const edited = logical.replace("- *", "- 従業員ID");
    await editor.fill(edited);
    await expect(page.getByText(/論理構造が編集されています/)).toBeVisible();
    await page.getByRole("button", { name: "論理構造から SQL を生成" }).press("Enter");
    await expect.poll(() => payload?.logical_structure).toBe(edited);
    await expect.poll(() => payload?.use_glossary).toBe(false);
    await page.reload();
    await expect(editor).toHaveValue(edited);
  });
}

test("sql to question generates SQL independently from the natural question and retries without execution", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  const payloads: Record<string, unknown>[] = [];
  let fail = false;
  let release: (() => void) | undefined;
  let gate: Promise<void> | undefined;
  let executions = 0;
  page.on("request", (request) => { if (/\/api\/nl2sql\/(execute|jobs)$/.test(request.url())) executions += 1; });
  await page.route("**/api/nl2sql/reverse/sql", (route) => fulfillJson(route, { sql: "SELECT ID FROM APP.INVOICES", explanation: "論理構造から生成", warnings: [] }));
  await page.route("**/api/nl2sql/reverse/question-sql", async (route) => {
    payloads.push(route.request().postDataJSON());
    if (gate) await gate;
    if (fail) return route.fulfill({ status: 502, contentType: "application/json", body: JSON.stringify({ detail: "SQL を生成できませんでした。再試行してください。" }) });
    return fulfillJson(route, { sql: "SELECT TOTAL_AMOUNT FROM APP.INVOICES", explanation: "質問文とスキーマから生成", warnings: [] });
  });
  await page.goto("/sql-to-question");
  await expect(page.getByRole("button", { name: "自然言語の質問から SQL を生成" })).toHaveCount(0);
  await sqlToQuestionInput(page).fill("SELECT * FROM INVOICES");
  await page.getByRole("button", { name: "SQL 分析・質問生成", exact: true }).click();
  const structureAction = page.getByRole("button", { name: "論理構造から SQL を生成", exact: true });
  await structureAction.click();
  const structureResult = page.getByRole("region", { name: "再生成 SQL", exact: true });
  await expect(structureResult).toContainText("SELECT ID FROM APP.INVOICES");
  const candidates = page.getByRole("region", { name: "質問候補", exact: true });
  const questionAction = candidates.getByRole("button", { name: "自然言語の質問から SQL を生成" });
  gate = new Promise<void>((resolve) => { release = resolve; });
  await questionAction.press("Enter");
  await expect.poll(() => payloads.length).toBe(1);
  await expect(questionAction).toBeDisabled();
  await expect(structureAction).toBeDisabled();
  await expect(page.getByRole("textbox", { name: "再生成に使う SQL 論理構造" })).toBeDisabled();
  release!();
  gate = undefined;
  const questionResult = candidates.getByRole("region", { name: "自然言語の質問から生成した SQL" });
  await expect(questionResult).toContainText("SELECT TOTAL_AMOUNT FROM APP.INVOICES");
  await expect(questionResult).toContainText("生成日時:");
  await expect(questionResult).toContainText("未実行");
  expect(payloads[0]).toEqual({ question: "請求金額を条件付きで一覧確認したい", profile_id: "default" });
  await expect(structureResult).toContainText("SELECT ID FROM APP.INVOICES");
  fail = true;
  await questionAction.click();
  await expect(candidates.getByRole("alert")).toContainText("再試行してください");
  await expect(questionResult).toContainText("SELECT TOTAL_AMOUNT FROM APP.INVOICES");
  await expect(structureResult).toContainText("SELECT ID FROM APP.INVOICES");
  fail = false;
  await questionAction.click();
  await expect(candidates.getByRole("alert")).toHaveCount(0);
  await expect(questionAction).toBeEnabled();
  await questionResult.scrollIntoViewIfNeeded();
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("question-to-sql.png"), fullPage: true });
  await page.getByRole("tab", { name: "SQL入力・生成" }).click();
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(questionResult).toContainText("SELECT TOTAL_AMOUNT FROM APP.INVOICES");
  expect(payloads).toHaveLength(3);
  await page.getByRole("tab", { name: "SQL入力・生成" }).click();
  await sqlToQuestionInput(page).fill("SELECT ID FROM INVOICES");
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(questionResult).toHaveCount(0);
  await expect(questionAction).toHaveCount(0);
  await page.reload();
  await expect(questionAction).toHaveCount(0);
  expect(payloads).toHaveLength(3);
  expect(executions).toBe(0);
});

// Issue #427: 質問は明示登録した最小スナップショットとして復元する。
test("sql to question retains candidates through navigation, reload and failed reference revalidation", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  let mutations = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/api\/nl2sql\//.test(request.url())) mutations += 1;
  });
  await page.goto("/sql-to-question");
  await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
  await page.getByRole("button", { name: "SQL 分析・質問生成", exact: true }).click();
  const candidates = page.getByRole("region", { name: "質問候補", exact: true });
  const question = "請求金額を条件付きで一覧確認したい";
  await expect(candidates).toContainText(question);
  const editor = page.getByRole("textbox", { name: "再生成に使う SQL 論理構造" });
  const draft = "SELECT: 請求金額\nFROM: INVOICES\nWHERE: 請求金額 >= 100";
  await editor.fill(draft);
  await (await openSidebarNav(page)).locator('a[href="/direct-sql"]').first().click();
  await expect(directSqlInput(page)).toBeVisible();
  await page.goBack();
  await expect(candidates).toContainText(question);
  await expect(candidates).toContainText("前回の実行結果");
  const timestamp = await candidates.getByText(/実行日時:/).innerText();
  await page.goForward();
  await expect(directSqlInput(page)).toBeVisible();
  await (await openSidebarNav(page)).locator('a[href="/sql-to-question"]').first().click();
  await expect(editor).toHaveValue(draft);
  await expect(candidates).toContainText(timestamp);
  await page.reload();
  await expect(editor).toHaveValue(draft);
  await expect(candidates).toContainText(question);
  await expect(candidates).toContainText(timestamp);
  await expect(candidates).toContainText("論理構造が編集されています");
  const generate = candidates.getByRole("button", { name: "自然言語の質問から SQL を生成" });
  await expect(generate).toBeEnabled();
  await generate.focus();
  await expect(generate).toBeFocused();
  await candidates.scrollIntoViewIfNeeded();
  await expectNoHorizontalScroll(page);
  await candidates.screenshot({ path: testInfo.outputPath("restored-question-candidate.png") });

  // 対象の参照権限が再検証できなければ候補を残し、生成は停止する。
  let unavailable = true;
  await page.route("**/api/nl2sql/profiles/default/usage-context", async (route) => {
    if (unavailable) return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "プロファイルが見つかりません。" }) });
    return route.fallback();
  });
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("プロファイルが見つかりません");
  await expect(candidates).toContainText(question);
  await expect(candidates).toContainText(timestamp);
  await expect(generate).toBeDisabled();
  await expect(editor).toHaveValue(draft);
  unavailable = false;
  await page.getByRole("button", { name: "プロファイル・スキーマを再読込" }).click();
  await expect(generate).toBeEnabled();
  expect(mutations).toBe(1);
});

test("sql to question isolates candidate snapshots by DB and account and clears them on auth expiry", async ({ page }) => {
  await mockNl2SqlApi(page);
  let database = "question-db-a";
  let user = systemAdminMe;
  await page.route("**/api/ready/database", (route) => fulfillJson(route, { status: "ok", check: "ok", detail: null, context_id: database }));
  await page.route("**/api/auth/me", (route) => fulfillJson(route, user));
  await page.goto("/sql-to-question");
  await fillSqlAndGenerateQuestions(page, "SELECT TOTAL_AMOUNT FROM INVOICES");
  const candidates = page.getByRole("region", { name: "質問候補", exact: true });
  await expect(candidates).toContainText("請求金額を条件付きで一覧確認したい");
  database = "question-db-b";
  await page.reload();
  await expect(sqlToQuestionInput(page)).toHaveValue("");
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(candidates).toContainText("質問候補は未生成です");
  database = "question-db-a";
  await page.reload();
  await expect(candidates).toContainText("請求金額を条件付きで一覧確認したい");
  await expect(candidates).toContainText("前回の実行結果");
  user = { ...systemAdminMe, user_uuid: "other-question-user", login_user_id: "OTHER" };
  await page.reload();
  await expect(sqlToQuestionInput(page)).toHaveValue("");
  await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
  await expect(candidates).toContainText("質問候補は未生成です");
  await page.getByRole("tab", { name: "SQL入力・生成" }).click();
  await fillSqlAndGenerateQuestions(page, "SELECT TOTAL_AMOUNT FROM INVOICES");
  await expect(candidates).toContainText("請求金額を条件付きで一覧確認したい");
  await page.evaluate(() => window.dispatchEvent(new Event("app-auth-unauthorized")));
  await expect(page).toHaveURL(/\/login/);
  expect(await page.evaluate(() => Object.keys(sessionStorage).filter((key) => key.startsWith("production-ready-nl2sql.draft.v1:")))).toEqual([]);
});

for (const invalid of ["expired", "malformed", "different-sql"] as const) {
  test(`sql to question does not restore ${invalid} candidate snapshots`, { tag: "@responsive" }, async ({ page }) => {
    await mockNl2SqlApi(page);
    await page.goto("/sql-to-question");
    await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
    await page.getByRole("button", { name: "SQL 分析・質問生成", exact: true }).click();
    const candidates = page.getByRole("region", { name: "質問候補", exact: true });
    await expect(candidates).toContainText("請求金額を条件付きで一覧確認したい");
    await page.evaluate((invalid) => {
      const key = Object.keys(sessionStorage).find((key) => key.includes('questionSnapshot:default'))!;
      const saved = JSON.parse(sessionStorage.getItem(key)!);
      if (invalid === "expired") saved.at = Date.now() - 8 * 60 * 60 * 1000;
      if (invalid === "malformed") saved.value.warnings = [123];
      if (invalid === "different-sql") saved.value.sourceSql = "SELECT 1 FROM DUAL";
      sessionStorage.setItem(key, JSON.stringify(saved));
    }, invalid);
    let mutations = 0;
    page.on("request", (request) => { if (request.method() === "POST" && /\/api\/nl2sql\//.test(request.url())) mutations += 1; });
    await page.reload();
    await expect(candidates).toContainText("質問候補は未生成です");
    await expect(page.getByRole("textbox", { name: "再生成に使う SQL 論理構造" })).toHaveValue(/SELECT: 請求金額/);
    expect(mutations).toBe(0);
  });
}

for (const existing of [false, true]) {
  test(`sql to question shows gray result loading and retains prior drafts on failure existing=${existing}`, async ({ page }, testInfo) => {
    await mockNl2SqlApi(page);
    await page.goto("/sql-to-question");
    await sqlToQuestionInput(page).fill("SELECT TOTAL_AMOUNT FROM INVOICES");
    const generate = page.getByRole("button", { name: "SQL 分析・質問生成", exact: true });
    const editor = page.getByRole("textbox", { name: "再生成に使う SQL 論理構造" });
    const candidates = page.getByRole("region", { name: "質問候補", exact: true });
    if (existing) {
      await generate.click();
      await expect(candidates).toContainText("請求金額を条件付きで一覧確認したい");
      await editor.fill("SELECT: 手編集した請求金額\nFROM: INVOICES");
      await page.getByRole("tab", { name: "SQL入力・生成" }).click();
    }
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let calls = 0;
    await page.route("**/api/nl2sql/reverse/deep", async (route) => {
      calls += 1;
      await gate;
      return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "接続を確認してください。" }) });
    });
    await generate.focus();
    await generate.press("Enter");
    const loading = page.getByTestId("sql-to-question-analysis-loading");
    await expect(page.getByRole("tab", { name: "SQL分析・質問候補" })).toHaveAttribute("aria-selected", "true");
    await expect(loading).toBeVisible();
    await expect(page.getByTestId("sql-to-question-analysis-content")).toHaveAttribute("aria-busy", "true");
    await expect(editor).not.toBeVisible();
    await expect(candidates).not.toBeVisible();
    await expect(page.getByText("質問候補は未生成です")).not.toBeVisible();
    await expect(page.getByRole("button", { name: "自然言語の質問から SQL を生成" })).toHaveCount(0);
    await expectNoHorizontalScroll(page);
    await loading.screenshot({ path: testInfo.outputPath("sql-analysis-gray-loading.png") });
    await page.getByRole("tab", { name: "SQL入力・生成" }).click();
    await expect(generate).toBeDisabled();
    await expect(sqlToQuestionInput(page)).toBeDisabled();
    await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
    release();
    await expect(page.locator("#sql-to-question-panel-input").getByRole("alert")).toContainText("接続を確認");
    await expect(generate).toBeEnabled();
    await page.getByRole("tab", { name: "SQL分析・質問候補" }).click();
    await expect(loading).toHaveCount(0);
    if (existing) {
      await expect(editor).toHaveValue("SELECT: 手編集した請求金額\nFROM: INVOICES");
      await expect(candidates).toContainText("請求金額を条件付きで一覧確認したい");
    } else {
      await expect(candidates).toContainText("質問候補は未生成です");
    }
    expect(calls).toBe(1);
  });
}

// 各主要導線の最終状態で全テキスト・入力欄の字体継承を確認する。
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === "skipped") return;
  await expectLocalUiFonts(page);
});

// Issue #477: SQL 生成の非同期回復と結果の条件・Profile 境界。
test("query recovery: 2.5秒より遅い状態取得を中断せず結果を表示する", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  let reads = 0;
  let creates = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/nl2sql/jobs") creates += 1;
  });
  await page.route("**/api/nl2sql/jobs/job-default-001", async (route) => {
    reads += 1;
    await new Promise((resolve) => setTimeout(resolve, 3200));
    await route.fallback();
  });
  await page.goto("/query");
  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  const run = page.getByRole("button", { name: "SQL を生成して実行", exact: true });
  await run.focus();
  await run.press("Enter");
  await expect(run).toBeDisabled();
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible({ timeout: 12_000 });
  await expect(run).toBeEnabled();
  expect(reads).toBe(1);
  expect(creates).toBe(1);
  await expectNoHorizontalScroll(page);
  await page.getByTestId("nl2sql-job-progress").screenshot({ path: testInfo.outputPath("query-slow-poll-completed.png") });
});

for (const failure of ["get", "set", "remove"] as const) {
  test(`query recovery: sessionStorage ${failure} 失敗でも作成済み job を追跡する`, async ({ page }, testInfo) => {
    await mockNl2SqlApi(page);
    let release!: () => void;
    const completion = new Promise<void>((resolve) => { release = resolve; });
    let creates = 0;
    page.on("request", (request) => {
      if (request.method() === "POST" && new URL(request.url()).pathname === "/api/nl2sql/jobs") creates += 1;
    });
    await page.route("**/api/nl2sql/jobs/job-default-001", async (route) => {
      await completion;
      await route.fallback();
    });
    await page.addInitScript((failure) => {
      if (failure === "get") {
        Object.defineProperty(window, "sessionStorage", { get() { throw new DOMException("blocked", "SecurityError"); } });
        return;
      }
      const method = failure === "set" ? "setItem" : "removeItem";
      const original = Storage.prototype[method];
      Object.defineProperty(Storage.prototype, method, { value: function (key: string, value?: string) {
        if (this === window.sessionStorage && key.includes("nl2sql.activeJob")) {
          throw new DOMException("blocked", "QuotaExceededError");
        }
        return original.call(this, key, value!);
      } });
    }, failure);
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.goto("/query");
    await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
    await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
    const run = page.getByRole("button", { name: "SQL を生成して実行", exact: true });
    await run.click();
    await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "running");
    await expect(run).toBeDisabled();
    if (failure !== "remove") {
      await expect(page.getByText(/実行中のジョブをブラウザに保存できません/)).toBeVisible();
      expect(await page.evaluate(() => {
        const event = new Event("beforeunload", { cancelable: true });
        window.dispatchEvent(event);
        return event.defaultPrevented;
      })).toBe(true);
      await page.getByText(/実行中のジョブをブラウザに保存できません/).scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath(`query-storage-${failure}.png`) });
    }
    release();
    await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
    await expect(run).toBeEnabled();
    await expect(page.getByTestId("nl2sql-action-feedback-error")).toHaveCount(0);
    await expect(page.getByText(/実行中のジョブをブラウザに保存できません/)).toHaveCount(0);
    expect(await page.evaluate(() => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    // sessionStorage getter 全体の障害では job 完了後も未保存草稿の離脱保護を残す。
    })).toBe(failure === "get");
    expect(creates).toBe(1);
    expect(pageErrors).toEqual([]);
    await expectNoHorizontalScroll(page);
  });
}

test("query recovery: Profile 切替で旧 SQL・進捗・結果を破棄し草稿は保持する", async ({ page }) => {
  const api = await mockNl2SqlApi(page);
  await page.route("**/api/nl2sql/profiles/search?*", (route) => fulfillJson(route, {
    items: [
      { ...profiles[0], allowed_table_count: 1, allowed_view_count: 0 },
      { ...profiles[0], id: "other", name: "別プロファイル", category: "別プロファイル", allowed_table_count: 1, allowed_view_count: 0 },
    ], next_cursor: null, total: 2,
  }));
  await page.goto("/query");
  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
  await nl2sqlQuestionInput(page).fill("Profile A の請求を確認");
  await page.getByRole("button", { name: "SQL を生成して実行", exact: true }).click();
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
  api.jobPayload = null;
  await chooseSelectFieldOption(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "other");
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "生成 SQL", exact: true })).toHaveCount(0);
  await expect(page.getByRole("cell", { name: "架空商事" })).toHaveCount(0);
  await expect(nl2sqlQuestionInput(page)).toHaveValue("");
  await nl2sqlQuestionInput(page).fill("Profile B の草稿");
  await chooseSelectFieldOption(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
  await expect(nl2sqlQuestionInput(page)).toHaveValue("Profile A の請求を確認");
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
  expect(api.jobPayload).toBeNull();
});

test("query recovery: usage-context の再試行で失敗した Profile だけでなく参照情報も回復する", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  let reads = 0;
  let failing = true;
  await page.route("**/api/nl2sql/profiles/default/usage-context", async (route) => {
    reads += 1;
    if (failing) {
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Profile の一時取得エラー" }) });
    } else await route.fallback();
  });
  await page.goto("/query");
  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
  await nl2sqlQuestionInput(page).fill("再試行で消してはいけない草稿");
  const error = page.getByRole("alert").filter({ hasText: "Profile の一時取得エラー" });
  await expect(error).toBeVisible();
  await error.screenshot({ path: testInfo.outputPath("query-profile-retry-error.png") });
  await error.getByRole("button").click();
  await expect.poll(() => reads).toBe(2);
  await expect(error).toBeVisible();
  failing = false;
  await error.getByRole("button").focus();
  await error.getByRole("button").press("Enter");
  await expect.poll(() => reads).toBe(3);
  await expect(error).toHaveCount(0);
  await expect(nl2sqlQuestionInput(page)).toHaveValue("再試行で消してはいけない草稿");
  await expectNoHorizontalScroll(page);
});

test("query recovery: すべての生成条件変更で前回結果と未実行を区別する", async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  await page.goto("/query");
  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
  await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
  await page.getByRole("button", { name: "SQL を生成して実行", exact: true }).click();
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
  api.jobPayload = null;
  const changed = page.getByText(/入力が変更されています。現在の入力は未実行です。/);
  await page.getByRole("button", { name: /実行オプション/ }).click();
  await expect(changed).toHaveCount(0);
  for (const label of ["用語・同義語を使う", "公開版オントロジーを使う", "処理手順を表示"]) {
    const option = page.getByRole("checkbox", { name: label, exact: true });
    const initial = await option.isChecked();
    await option.setChecked(!initial);
    await expect(changed).toBeVisible();
    await option.setChecked(initial);
    await expect(changed).toHaveCount(0);
  }
  await page.screenshot({ path: testInfo.outputPath("query-previous-conditions.png") });
  expect(api.jobPayload).toBeNull();
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
});

test("query recovery: 初回 Profile 選択でも保存された進行中 job を復元する", async ({ page }) => {
  await mockNl2SqlApi(page);
  let creates = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/nl2sql/jobs") creates += 1;
  });
  await seedActiveJob(page, "job-default-001", 0);
  await page.route("**/api/nl2sql/jobs/job-default-001", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.fallback();
  });
  await page.goto("/query");
  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
  await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
  expect(creates).toBe(0);
});

test("query recovery: 状態取得の timeout が続くと追跡を解除する", async ({ page }) => {
  await mockNl2SqlApi(page);
  let reads = 0;
  await page.route("**/api/nl2sql/jobs/job-default-001", async (route) => {
    reads += 1;
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.fallback();
  });
  await page.goto("/query");
  await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
  // 30秒の HTTP deadline だけ短縮する。polling 自体は実際の2.5秒間隔を使う。
  await page.evaluate(() => {
    const timeout = AbortSignal.timeout.bind(AbortSignal);
    AbortSignal.timeout = (ms) => timeout(ms === 30_000 ? 100 : ms);
  });
  await nl2sqlQuestionInput(page).fill("応答 timeout 時も入力を保持する");
  const run = page.getByRole("button", { name: "SQL を生成して実行", exact: true });
  await run.click();
  await expect(page.getByTestId("nl2sql-action-feedback-error")).toContainText("ジョブの状態確認に連続して失敗したため", { timeout: 15_000 });
  await expect(run).toBeEnabled();
  await expect(nl2sqlQuestionInput(page)).toHaveValue("応答 timeout 時も入力を保持する");
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
  expect(reads).toBe(3);
  expect((await readActiveJobState(page)).jobId).toBeNull();
});

// Issue #479 / #481: タブごとの復元情報を、別タブや旧 job の cleanup から保護する。
test.describe("query snapshot ownership", () => {
  async function openIdleTabs(page: Page) {
    const other = await page.context().newPage();
    await mockDatabaseGateReady(other);
    for (const tab of [page, other]) {
      await mockNl2SqlApi(tab);
      await tab.route("**/api/nl2sql/profiles/search?*", (route) => fulfillJson(route, {
        items: [
          { ...profiles[0], allowed_table_count: 1, allowed_view_count: 0 },
          { ...profiles[0], id: "other", name: "別プロファイル", category: "別プロファイル", allowed_table_count: 1, allowed_view_count: 0 },
        ], next_cursor: null, total: 2,
      }));
      await tab.goto("/query");
      await expectSelectFieldValue(tab.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
    }
    return other;
  }

  const runningJob = (jobId: string) => ({
    job_id: jobId, status: "running", created_at: new Date().toISOString(),
    result: null, error_message: null, warning_message: null, timing: null, steps: [],
  });
  const readSnapshot = readActiveJobState;
  async function startJob(page: Page) {
    await nl2sqlQuestionInput(page).fill("請求金額を一覧で見たい");
    const run = page.getByRole("button", { name: "SQL を生成して実行", exact: true });
    await run.focus();
    await run.press("Enter");
    await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "running");
    await expect(run).toBeDisabled();
  }

  test("待機中タブの Profile 切替後も実行タブの再読込で job を復元する", async ({ page }, testInfo) => {
    const other = await openIdleTabs(page);
    let creates = 0;
    let reads = 0;
    for (const tab of [page, other]) {
      tab.on("request", (request) => {
        if (request.method() === "POST" && new URL(request.url()).pathname === "/api/nl2sql/jobs") creates += 1;
      });
    }
    await page.route("**/api/nl2sql/jobs/job-default-001", (route) => {
      reads += 1;
      return fulfillJson(route, runningJob("job-default-001"));
    });
    await startJob(page);
    const snapshot = await readSnapshot(page);
    expect(snapshot.jobId).toBe("job-default-001");
    expect(snapshot.startedAt).not.toBeNull();
    for (const profileId of ["other", "default"]) {
      await chooseSelectFieldOption(other.getByRole("combobox", { name: "業務プロファイル", exact: true }), profileId);
      await expect(other.getByTestId("nl2sql-job-progress")).toHaveCount(0);
      expect(await readSnapshot(other)).toEqual({ jobId: null, startedAt: null });
      expect(await readSnapshot(page)).toEqual(snapshot);
    }
    const readsBeforeReload = reads;
    await page.reload();
    await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "running");
    await expect.poll(() => reads).toBeGreaterThan(readsBeforeReload);
    await expect(page.getByRole("button", { name: "SQL を生成して実行", exact: true })).toBeDisabled();
    expect(creates).toBe(1);
    expect(await readSnapshot(page)).toEqual(snapshot);
    await expectNoHorizontalScroll(page);
    await page.getByTestId("nl2sql-job-progress").screenshot({ path: testInfo.outputPath("cross-tab-job-restored.png") });
    await other.close();
  });

  test("旧 job の完了と Profile 切替が別タブの新 job を削除しない", async ({ page }) => {
    const other = await openIdleTabs(page);
    let release!: () => void;
    const completion = new Promise<void>((resolve) => { release = resolve; });
    await page.route("**/api/nl2sql/jobs/job-default-001", async (route) => {
      await completion;
      await route.fallback();
    });
    await startJob(page);
    await other.route("**/api/nl2sql/jobs", (route) => fulfillJson(route, runningJob("job-other-tab")));
    await other.route("**/api/nl2sql/jobs/job-other-tab", (route) => fulfillJson(route, runningJob("job-other-tab")));
    await startJob(other);
    const snapshot = await readSnapshot(other);
    expect(snapshot.jobId).toBe("job-other-tab");
    release();
    await expect(page.getByRole("cell", { name: "架空商事" })).toBeVisible();
    expect(await readSnapshot(page)).toEqual({ jobId: null, startedAt: null });
    expect(await readSnapshot(other)).toEqual(snapshot);
    await chooseSelectFieldOption(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "other");
    await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
    expect(await readSnapshot(page)).toEqual({ jobId: null, startedAt: null });
    expect(await readSnapshot(other)).toEqual(snapshot);
    await other.reload();
    await expect(other.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "running");
    await expect(other.getByRole("button", { name: "SQL を生成して実行", exact: true })).toBeDisabled();
    await other.close();
  });

  test("別タブの保存失敗 cleanup が既存 job の復元情報を削除しない", async ({ page }) => {
    const other = await openIdleTabs(page);
    await page.route("**/api/nl2sql/jobs/job-default-001", (route) => fulfillJson(route, runningJob("job-default-001")));
    await startJob(page);
    const snapshot = await readSnapshot(page);
    await other.evaluate(() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key: string, value: string) {
        if (this === sessionStorage && key.includes("nl2sql.activeJobId")) throw new DOMException("blocked", "QuotaExceededError");
        return original.call(this, key, value);
      };
    });
    await other.route("**/api/nl2sql/jobs", (route) => fulfillJson(route, runningJob("job-other-tab")));
    await other.route("**/api/nl2sql/jobs/job-other-tab", (route) => fulfillJson(route, runningJob("job-other-tab")));
    await startJob(other);
    await expect(other.getByText(/実行中のジョブをブラウザに保存できません/)).toBeVisible();
    expect(await readSnapshot(page)).toEqual(snapshot);
    await page.reload();
    await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "running");
    await expect(page.getByRole("button", { name: "SQL を生成して実行", exact: true })).toBeDisabled();
    await other.close({ runBeforeUnload: false });
  });
  test("双タブの別 job は再読込・往復後も独立して進捗と結果を復元する", async ({ page }, testInfo) => {
    // 3 つのタブで再読込・往復を繰り返す長いテストで、2 worker の CI では既定の 30 秒を超えることがある（#1489）。
    test.slow();
    const other = await openIdleTabs(page);
    const done = new Set<string>();
    const seen: string[][] = [[], []];
    let creates = 0;
    for (const [index, tab] of [page, other].entries()) {
      const jobId = `job-tab-${index}`;
      tab.on("request", (request) => {
        if (request.method() === "POST" && new URL(request.url()).pathname === "/api/nl2sql/jobs") creates += 1;
      });
      await tab.route("**/api/nl2sql/jobs", (route) => fulfillJson(route, runningJob(jobId)));
      await tab.route("**/api/nl2sql/jobs/*", (route) => {
        const requested = new URL(route.request().url()).pathname.split("/").pop()!;
        seen[index].push(requested);
        return fulfillJson(route, {
          ...runningJob(requested), status: done.has(requested) ? "done" : "running",
          result: done.has(requested) ? {
            engine: "enterprise_ai_direct", engine_meta: {}, fallback_reason: "", original_question: "請求金額を一覧で見たい",
            rewritten_question: "請求金額を一覧で見たい", generated_sql: "SELECT CUSTOMER_NAME FROM INVOICES",
            executable_sql: "SELECT CUSTOMER_NAME FROM INVOICES", explanation: "請求情報", safety, timing,
            recommendations: [], repaired_sql: "", optimization_hints: [],
            results: { columns: ["CUSTOMER_NAME"], rows: [{ CUSTOMER_NAME: `タブ${index}の結果` }], total: 1 },
          } : null,
        });
      });
      await startJob(tab);
    }
    // 既に job がある状態で開く新規タブも、他タブの追跡を引き継がない。
    const fresh = await page.context().newPage();
    await mockDatabaseGateReady(fresh);
    await mockNl2SqlApi(fresh);
    await fresh.goto("/query");
    await expectSelectFieldValue(fresh.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
    await expect(fresh.getByTestId("nl2sql-job-progress")).toHaveCount(0);
    expect(await readSnapshot(fresh)).toEqual({ jobId: null, startedAt: null });
    await fresh.close();
    for (const [index, tab] of [page, other].entries()) {
      expect((await readSnapshot(tab)).jobId).toBe(`job-tab-${index}`);
      await tab.reload();
      await expect(tab.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "running");
      await (await openSidebarNav(tab)).locator('a[href="/history"]').first().click();
      await tab.goBack();
      await expect(tab.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "running");
      await tab.goForward();
      await tab.goBack();
      await expect(tab.getByRole("button", { name: "SQL を生成して実行", exact: true })).toBeDisabled();
      expect((await readSnapshot(tab)).jobId).toBe(`job-tab-${index}`);
    }
    done.add("job-tab-0");
    await expect(page.getByRole("cell", { name: "タブ0の結果", exact: true })).toBeVisible();
    expect((await readSnapshot(page)).jobId).toBeNull();
    expect((await readSnapshot(other)).jobId).toBe("job-tab-1");
    await other.reload();
    await expect(other.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "running");
    done.add("job-tab-1");
    await expect(other.getByRole("cell", { name: "タブ1の結果", exact: true })).toBeVisible();
    await expect(page.getByRole("cell", { name: "タブ1の結果", exact: true })).toHaveCount(0);
    await expect(other.getByRole("cell", { name: "タブ0の結果", exact: true })).toHaveCount(0);
    expect((await readSnapshot(other)).jobId).toBeNull();
    expect(creates).toBe(2);
    expect(new Set(seen[0])).toEqual(new Set(["job-tab-0"]));
    expect(new Set(seen[1])).toEqual(new Set(["job-tab-1"]));
    for (const [index, tab] of [page, other].entries()) {
      await expectNoHorizontalScroll(tab);
      await tab.getByRole("cell", { name: `タブ${index}の結果`, exact: true }).scrollIntoViewIfNeeded();
      await tab.screenshot({ path: testInfo.outputPath(`independent-tab-${index}.png`) });
    }
    await other.close();
  });

  test("旧共有記録を自動復元せず理由を案内する", async ({ page }, testInfo) => {
    await mockNl2SqlApi(page);
    let reads = 0;
    await page.addInitScript(() => {
      localStorage.setItem("nl2sql.activeJobId", "legacy-job");
      localStorage.setItem("nl2sql.activeJobStartedAt", String(Date.now()));
    });
    await page.route("**/api/nl2sql/jobs/*", (route) => {
      reads += 1;
      return fulfillJson(route, runningJob("legacy-job"));
    });
    await page.goto("/query");
    const notice = page.getByText(/旧バージョンの共有ジョブ記録があります/);
    await expect(notice).toBeVisible();
    await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
    expect(reads).toBe(0);
    expect((await readSnapshot(page)).jobId).toBeNull();
    expect(await page.evaluate(() => localStorage.getItem("nl2sql.activeJobId"))).toBe("legacy-job");
    await notice.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("legacy-job-notice.png") });
    await expectNoHorizontalScroll(page);
  });

  test("job 復元は DB・ユーザーを分離し認証失効時に消去する", async ({ page }) => {
    await mockNl2SqlApi(page);
    let database = "db-a";
    let user = systemAdminMe;
    const reads: string[] = [];
    await page.route("**/api/ready/database", (route) => fulfillJson(route, { status: "ok", check: "ok", detail: null, context_id: database }));
    await page.route("**/api/auth/me", (route) => fulfillJson(route, user));
    await page.route("**/api/nl2sql/jobs", (route) => fulfillJson(route, runningJob(`job-${database}`)));
    await page.route("**/api/nl2sql/jobs/*", (route) => {
      const id = new URL(route.request().url()).pathname.split("/").pop()!;
      reads.push(id);
      return fulfillJson(route, runningJob(id));
    });
    await page.goto("/query");
    await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
    await startJob(page);
    database = "db-b";
    await page.reload();
    await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
    await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
    await startJob(page);
    database = "db-a";
    reads.length = 0;
    await page.reload();
    await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "running");
    await expect.poll(() => reads.includes("job-db-a")).toBe(true);
    expect(reads).not.toContain("job-db-b");
    user = { ...systemAdminMe, user_uuid: "other-user" };
    await page.reload();
    await expectSelectFieldValue(page.getByRole("combobox", { name: "業務プロファイル", exact: true }), "default");
    await expect(page.getByTestId("nl2sql-job-progress")).toHaveCount(0);
    await startJob(page);
    await page.evaluate(() => window.dispatchEvent(new Event("app-auth-unauthorized")));
    await expect(page).toHaveURL(/\/login/);
    expect(await page.evaluate(() => Object.keys(sessionStorage).filter((key) => key.includes("nl2sql.activeJob")))).toEqual([]);
  });

});

test("SELECT SQL の重名列は別々の値を表示し予約済み列名を保持する", async ({ page }, testInfo) => {
  await mockNl2SqlApi(page);
  await page.route("**/api/nl2sql/execute", (route) => fulfillJson(route, {
    columns: ["ID", "ID_3", "ID_2"],
    rows: [{ ID: 11, ID_3: 22, ID_2: 33 }],
    total: 1,
    returned_count: 1,
    has_more: false,
    truncated: false,
  }));
  await page.goto("/direct-sql");
  await directSqlInput(page).fill("SELECT 11 AS ID, 22 AS ID, 33 AS ID_2 FROM DUAL; -- 重名列");
  await page.getByRole("button", { name: "SQL 実行", exact: true }).press("Enter");
  const table = page.getByRole("table");
  await expect(table.getByRole("columnheader")).toHaveText(["ID", "ID_3", "ID_2"]);
  await expect(table.getByRole("cell")).toHaveText(["11", "22", "33"]);
  await table.scrollIntoViewIfNeeded();
  await expectNoHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath("direct-sql-duplicate-columns.png") });
});


for (const theme of ["light", "dark"]) {
  test(`header utility outline matches import across management pages: ${theme}`, { tag: "@responsive" }, async ({ page, isMobile }, testInfo) => {
    await mockNl2SqlApi(page);
    for (const path of ["/table-management", "/view-management", "/data-management"]) {
      await page.goto(path);
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      const header = page.locator("main header").first();
      const inMenu = isMobile && path !== "/data-management";
      await expect(header.getByRole("heading", { level: 1 })).toBeVisible();
      if (inMenu) await header.getByRole("button", { name: "その他の操作" }).click();
      // 「その他の操作」メニューは body へ Portal で描くため、ヘッダーの外から探す（#363）。
      const refresh = inMenu
        ? page.getByRole("menu", { name: "その他の操作" }).getByRole("menuitem", { name: "最新情報を取得", exact: true })
        : header.getByRole("button", { name: "最新情報を取得", exact: true });
      await expect(refresh).toBeEnabled();
      if (!isMobile) {
        for (const label of ["最新情報を取得", "DB 構造を再取得"]) {
          const button = header.getByRole("button", { name: label, exact: true });
          await expect(button).toHaveCSS("border-top-width", "1px");
          await expect(button).not.toHaveCSS("border-top-color", "rgba(0, 0, 0, 0)");
          if (path === "/table-management") {
            const importButton = header.getByRole("button", { name: "Excel/CSV 取込(新規テーブル)" });
            await expect(button).toHaveCSS("border-top-color", await importButton.evaluate(el => getComputedStyle(el).borderTopColor));
            await expect(button).toHaveCSS("background-color", await importButton.evaluate(el => getComputedStyle(el).backgroundColor));
          }
        }
      }
      await refresh.click();
      if (inMenu) await header.getByRole("button", { name: "その他の操作" }).click();
      await expect(refresh).toBeEnabled();
      if (inMenu) await page.keyboard.press("Escape");
      await expectNoHorizontalScroll(page);
      await page.screenshot({ path: testInfo.outputPath(`${path.slice(1)}-${theme}.png`), fullPage: true });
    }
  });
}

for (const theme of ["light", "dark"] as const) {
  test(`action roles and placement remain consistent in metadata workflows ${theme}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await mockNl2SqlApi(page);
    await page.addInitScript(() => sessionStorage.clear());
    const mobile = testInfo.project.name === "mobile-375";
    for (const width of mobile ? [375] : [1280, 1920]) {
      await page.setViewportSize({ width, height: 1000 });
      for (const path of ["comment-management", "annotation-management"]) {
        await page.goto(`/${path}`);
        await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
        await page.getByRole("tab", { name: "対象選択", exact: true }).click();
        const checkbox = page.getByRole("option", { name: /INVOICES/ });
        await checkbox.setChecked(false);
        const fetch = page.getByRole("button", { name: "情報を取得", exact: true });
        await expect(fetch).toBeDisabled();
        await expect(fetch.locator("svg")).toHaveCount(1);
        await expect(fetch.locator("svg")).toHaveAttribute("width", "16");
        await expect(fetch).toHaveCSS("height", mobile ? "44px" : "40px");
        const before = await fetch.boundingBox();
        await checkbox.check();
        await expect(fetch).toBeEnabled();
        await expect(fetch).toHaveClass(/bg-accent-emphasis/);
        await expect(fetch.locator("svg")).toHaveCount(1);
        expect((await fetch.boundingBox())?.width).toBe(before?.width);
        if (!mobile) {
          const refresh = page.getByRole("button", { name: "最新情報を取得", exact: true });
          await expect(refresh).toHaveCSS("height", "36px");
          await expect(refresh).toHaveClass(/border-border-control/);
          await expect(refresh.locator("svg")).toHaveAttribute("width", "16");
        }
        await fetch.scrollIntoViewIfNeeded();
        await expectNoHorizontalScroll(page);
        await page.screenshot({ path: testInfo.outputPath(`${path}-${width}-${theme}-actions.png`) });
        await page.keyboard.press("Tab");
        await fetch.focus();
        await expect(fetch).toHaveCSS("outline-style", "solid");
        await page.keyboard.press("Enter");
        await expect(page.getByLabel("構造情報")).toHaveValue(/INVOICES/);
      }
    }
  });
}


test("新規テーブル取込は編集した型を再プレビューしてから実行する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  await mockTabularPreview(page);
  const submitted: Record<string, unknown>[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.endsWith("/db-admin/import-tabular")) submitted.push(request.postDataJSON());
  });
  await page.goto("/table-management");
  await clickPageHeaderAction(page, "table-management-actions", "Excel/CSV 取込(新規テーブル)");
  const panel = page.locator("#table-management-panel-import");
  await panel.getByLabel("Oracle 表名").fill("TABLE_A");
  await panel.getByTestId("table-import-file-field-input").setInputFiles({ name: "table-a.csv", mimeType: "text/csv", buffer: Buffer.from("ID\n1\n") });
  await panel.getByLabel("実行確認語").fill("ADMIN_EXECUTE");
  const execute = panel.getByRole("button", { name: "取込を実行", exact: true });
  await expect(execute).toBeDisabled();
  await panel.getByTestId("table-import-preview-button").click();
  await expect(panel.getByTestId("table-import-result-panel")).toContainText('CREATE TABLE "TABLE_A"');
  await expect(execute).toBeEnabled();
  await panel.getByLabel("ID", { exact: true }).fill("VARCHAR2(1000 CHAR)");
  await expect(execute).toBeDisabled();
  await panel.getByTestId("table-import-preview-button").click();
  await expect(panel.getByTestId("table-import-result-panel")).toContainText("VARCHAR2(1000 CHAR)");
  await expect(execute).toBeEnabled();
  expect(submitted).toHaveLength(2);
  expect(submitted.every((request) => request.preview === true && request.confirmation === "")).toBe(true);
  await panel.getByTestId("table-import-result-panel").scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("table-import-preview.png") });
  await expectNoHorizontalScroll(page);
  await execute.click();
  await expect.poll(() => api.importTabularPayload?.preview).toBe(false);
  expect(api.importTabularPayload?.column_types).toEqual({ ID: "VARCHAR2(1000 CHAR)" });
});

test("SQL 生成の処理の経過は span の木で、段の中のモデルの呼び出しを入れ子で出し、Trace ID を処理の詳細に出す (#3226)", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const api = await mockNl2SqlApi(page);
  api.progressEvents = jobProgressEvents("job-default-001", { at: "2026-06-21T10:00:00.050Z", terminal: "done", steps: {
    queue: "done",
    prepare_context: "done",
    generate_sql: { status: "done", params: { engine: "enterprise_ai_direct" }, children: [{ name: "gen_ai.chat", status: "done", attributes: { "gen_ai.request.model": "fixture-model" } }] },
    safety_check: "done",
    execute_sql: { status: "done", params: { rows: 1 } },
    format_results: "done",
  } });
  await page.goto("/query");
  await fillQuestionWhenReady(page, "合成データの件数");
  await page.getByRole("button", { name: "SQL を生成して実行" }).click();
  await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", "done");
  await expect(page.getByTestId("nl2sql-job-summary")).toHaveText(/^処理の経過（7 ステップ・/);
  await page.getByTestId("nl2sql-job-summary").click();
  const progress = page.getByTestId("nl2sql-job");
  await expect(progress.getByTestId("nl2sql-job-step-generate_sql.1")).toContainText("モデルを呼び出す（用途の記録なし）");
  await inspectTraceStep(page, progress, "generate_sql.1");
  await expect(page.locator("[data-trace-technical]")).toContainText("fixture-model");
  await page.getByRole("button", {name: "経過に戻る", exact: true}).click();
  // 「トレースの詳細」の 2 つ目の表示は無い。Trace ID は処理の詳細に出す。
  await expect(page.getByText("トレースの詳細")).toHaveCount(0);
  await expect(page.getByTestId("trace-details-toggle")).toHaveCount(0);
  const details = page.locator("[data-run-process-details]");
  await expect(details).not.toHaveAttribute("open", "");
  await details.locator(":scope > summary").click();
  await expect(details.locator("[data-trace-id-row]")).toContainText(`Trace ID: ${jobProgressTraceId("job-default-001")}`);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("sql-generate-span-tree.png"), fullPage: true });
});

for (const scenario of ["正常", "プレビュー", "安全性の確認で停止", "取消"] as const) {
  test(`SQL 生成のイベントを履歴と同じ工程に表示する: ${scenario}`, {tag: "@responsive"}, async ({page}, testInfo) => {
    await mockNl2SqlApi(page);
    const start = "2026-10-23T00:00:00.000Z";
    const end = "2026-10-23T00:00:00.600Z";
    const cancelled = scenario === "取消";
    const blocked = scenario === "安全性の確認で停止";
    const preview = scenario === "プレビュー";
    const eventSteps = Object.fromEntries((["queue", "prepare_context", "generate_sql", "safety_check", "execute_sql", "format_results"] as JobProgressStep[]).map((stage, index) => {
      const skipped = (stage === "execute_sql" && (preview || blocked || cancelled)) || (stage === "format_results" && cancelled) || (stage === "safety_check" && cancelled);
      return [stage, {
        status: skipped ? "skipped" : stage === "safety_check" && blocked ? "failed" : "done",
        ...(!skipped ? {startedAt: new Date(Date.parse(start) + index * 100).toISOString(), finishedAt: new Date(Date.parse(start) + index * 100 + 50).toISOString()} : {}),
        params: {engine: "enterprise_ai_direct", rows: 1, ...(cancelled ? {stopped: true} : {})},
      }];
    }));
    const events = jobProgressEvents("fixture-3198", {at: end, steps: eventSteps, terminal: cancelled ? "cancelled" : blocked ? "failed" : "done"});
    const result = {
      history_id: "fixture-3198", engine: "enterprise_ai_direct", engine_meta: {}, fallback_reason: "",
      original_question: "合成の質問", rewritten_question: "合成の質問", generated_sql: "SELECT 1 AS VALUE FROM DUAL", executable_sql: "SELECT 1 AS VALUE FROM DUAL",
      explanation: "合成の回答", safety: {...safety, is_safe: !blocked, referenced_tables: [], referenced_columns: [], blocked_reason: blocked ? "合成の停止理由" : ""},
      results: {columns: ["VALUE"], rows: [{VALUE: 1}], total: 1}, recommendations: [], repaired_sql: "", optimization_hints: [], timing: {...timing, elapsed_ms: 600},
    };
    const job = {job_id: "fixture-3198", generation_only: preview, status: cancelled || blocked ? "error" : "done", created_at: start, finished_at: end, elapsed_ms: 600,
      error_code: cancelled ? "JOB_CANCELLED" : null, error_message: cancelled ? "停止しました。" : blocked ? "合成の停止理由" : null,
      steps: [], progress_events: events, result: cancelled ? null : result};
    await page.route("**/api/nl2sql/jobs", (route) => fulfillJson(route, {job_id: job.job_id, status: "running", created_at: start, steps: []}));
    await page.route("**/api/nl2sql/jobs/fixture-3198", (route) => fulfillJson(route, job));
    await page.goto("/query");
    await fillQuestionWhenReady(page, "合成の質問");
    await page.getByRole("button", {name: "SQL を生成して実行", exact: true}).click();
    await expect(page.getByTestId("nl2sql-job-progress")).toHaveAttribute("data-job-status", job.status);
    const progress = page.getByTestId("nl2sql-job");
    const rows = progress.locator("[data-run-timeline-step]");
    // 終端の一覧（見出しは nl2sql-job-summary）に切り替わってから読む。終端の状態の属性が変わった瞬間は、実行中の
    // 一覧のままか、段階の失敗の反映の前のことがある（#3329）。既定の開閉は、失敗は開いたまま・それ以外は閉じる。
    const summary = page.getByTestId("nl2sql-job-summary");
    const details = progress.locator("details").filter({ has: summary });
    if (blocked) {
      await expect(details).toHaveAttribute("open", "");
    } else {
      await expect(details).not.toHaveAttribute("open", "");
      await summary.click();
      await expect(details).toHaveAttribute("open", "");
    }
    await expect(rows).toHaveCount(6);
    await expect(rows.last()).toBeVisible();
    const live = await timelineSnapshot(progress);
  const liveTypography = await timelineTypography(progress);
  expect(liveTypography.steps.length).toBeGreaterThan(0);
  expect(liveTypography.steps.every(style => style?.fontSize === "12px" && style.fontFamily.includes("Noto Sans JP"))).toBe(true);
  expect(liveTypography.summary?.fontSize).toBe("12px");
    expect(live).toHaveLength(6);
    await progress.screenshot({path: testInfo.outputPath("sql-generation-process.png")});
    if (cancelled) {
      await expect(page.getByTestId("nl2sql-job-cancelled")).toContainText("停止しました。");
      expect(live.some((row) => row.status === "skipped")).toBe(true);
      return;
    }
    await page.route("**/api/nl2sql/history/fixture-3198", (route) => fulfillJson(route, {
      id: "fixture-3198", question: "合成の質問", engine: "enterprise_ai_direct", generated_sql: result.generated_sql, executable_sql: result.executable_sql,
      created_at: start, elapsed_ms: 600, generation_only: preview, engine_timings: [], stage_timings: [], profile_id: "default", profile_name: "プロファイル A",
      rewritten_question: "合成の質問", safety_is_safe: !blocked, result_row_count: preview || blocked ? 0 : 1, result_columns: ["VALUE"], feedback_comment: "", feedback_updated_at: "", progress_events: events,
    }));
    await page.goto("/history?id=fixture-3198");
    await page.getByTestId("history-detail-timeline-summary").click();
    expect(await timelineSnapshot(page.getByTestId("history-detail-timeline"))).toEqual(live);
  expect(await timelineTypography(page.getByTestId("history-detail-timeline"))).toEqual(liveTypography);
    await page.getByTestId("history-detail-timeline").screenshot({path: testInfo.outputPath("history-process.png")});
    await expectNoHorizontalScroll(page);
  });
}

test("SQL 生成は最初の取得が終わる前から開始待ちを表示する", {tag: "@responsive"}, async ({page}, testInfo) => {
  await mockNl2SqlApi(page);
  const at = new Date().toISOString();
  // worker が受け取るまで span は無い（backend の start_job と同じ）。画面は開始待ちの仮の行を出す（#3226）。
  const events: unknown[] = [];
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {release = resolve;});
  await page.route("**/api/nl2sql/jobs", (route) => fulfillJson(route, {job_id: "fixture-queue", status: "pending", created_at: at, steps: [], progress_events: events}));
  await page.route("**/api/nl2sql/jobs/fixture-queue", async (route) => {
    await gate;
    await fulfillJson(route, {job_id: "fixture-queue", status: "pending", created_at: at, steps: [], progress_events: events, result: null});
  });
  try {
    await page.goto("/query");
    await fillQuestionWhenReady(page, "合成の質問");
    await page.getByRole("button", {name: "SQL を生成して実行", exact: true}).click();
    await expect(page.getByTestId("nl2sql-job-current")).toHaveAttribute("data-step-id", "queue");
    await expect(page.getByTestId("nl2sql-job-current")).toContainText("待っています");
    await expect(page.getByTestId("nl2sql-job-running")).toBeVisible();
    await expect(page.getByTestId("nl2sql-job-step-queue")).toBeVisible();
    const progress = page.getByTestId("nl2sql-job");
    await progress.screenshot({path: testInfo.outputPath("sql-generation-queue.png")});
    await expectNoHorizontalScroll(page);
  } finally {
    release();
  }
});
