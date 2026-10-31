import { expect, test, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { expectSpinnerStable } from "./_helpers/spinner-stability";

/**
 * ボタンの loading スピナーが「一定の中心」で回転することを実画面で検証する。
 *
 * 旧実装は lucide `Loader2`（288 度の欠けた円弧）をそのまま回していたため、
 * インクの重心とシルエットが回転角ごとに動き、中心がずれて上下に揺れて見えていた。
 * デザインシステム移行（#529）後は共有パッケージ `@production-ready/ui` の
 * `Spinner`（全周トラック circle + 270° arc、`svg.animate-spin`）を使う。
 * 移行のときに旧 `StableLoadingIcon` の「180° 対称 active arc の重心」の検証を削除したため、共有 Spinner の
 * 270° の 1 本のアークで重心が回り、上下に揺れて見える状態に戻っていた（#1180）。共有 Spinner を 180° 対称の
 * 2 本のアークにし、角度ごとに撮った見た目の重心が動かないことをここで再び確かめる。
 */

// 共有 Button の loading は先頭アイコンを共有 Spinner（svg.animate-spin）へ置き換える
const LOADING_ICON_SELECTOR = "svg.animate-spin";

const profile = {
  id: "default",
  name: "既定プロファイル",
  category: "既定",
  description: "請求を扱うプロファイル",
  allowed_tables: ["APP.INVOICES"],
  allowed_views: [],
  glossary: {},
  sql_rules: [],
  default_row_limit: 100,
  safety_policy: "select_only",
  few_shot_examples: [],
  select_ai_config: {
    profile_name: "NL2SQL_DEFAULT_PROFILE",
    region: "ap-osaka-1",
    model: "cohere.command-r-plus",
    embedding_model: "cohere.embed-v4.0",
    max_tokens: 32000,
    enforce_object_list: true,
    comments: true,
    annotations: false,
    constraints: false,
    role: "",
    additional_instructions: "",
  },
  archived: false,
  version: 1,
  etag: "profile-etag",
  updated_at: "2026-08-16T00:00:00.000Z",
};

async function fulfillJson(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data }),
  });
}

/**
 * ジョブ作成の要求（POST /api/nl2sql/jobs）に応答せず、実行ボタンを送信中（loading）で固定する。
 * 実行ボタンが loading なのは送信中だけで、ジョブの実行中は disabled になりスピナーは進行状況が 1 つ出す（#416）。
 */
async function mockWorkbenchWithPendingJob(page: Page) {
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/profiles/search?*", (route) =>
    fulfillJson(route, {
      items: [
        {
          id: profile.id,
          name: profile.name,
          category: profile.category,
          description: profile.description,
          archived: false,
          allowed_table_count: 1,
          allowed_view_count: 0,
          glossary_count: 0,
          few_shot_count: 0,
          version: 1,
          etag: profile.etag,
          updated_at: profile.updated_at,
        },
      ],
      next_cursor: null,
      total: 1,
      change_token: 1,
    })
  );
  await page.route("**/api/nl2sql/profiles/default", (route) => fulfillJson(route, profile));
  await page.route("**/api/nl2sql/profiles/default/usage-context", (route) =>
    fulfillJson(route, {
      id: profile.id,
      name: profile.name,
      category: profile.category,
      description: profile.description,
      allowed_tables: profile.allowed_tables,
      allowed_views: profile.allowed_views,
      archived: false,
      object_scope_version: 1,
      version: profile.version,
      etag: profile.etag,
      updated_at: profile.updated_at,
    })
  );
  await page.route("**/api/schema/catalog/head", (route) =>
    fulfillJson(route, {
      catalog_version: 1,
      schema_fingerprint: "schema-fixture",
      refreshed_at: "2026-08-16T00:00:00.000Z",
      object_count: 1,
      column_count: 1,
      change_token: 1,
      etag: "schema-etag",
    })
  );
  await page.route("**/api/schema/objects?*", (route) =>
    fulfillJson(route, { items: [], next_cursor: null, total: 0, catalog_version: 1 })
  );
  await page.route("**/api/nl2sql/history**", (route) => fulfillJson(route, { items: [], total: 0 }));
  await page.route("**/api/nl2sql/similar-history", (route) => fulfillJson(route, { items: [] }));
  await page.route("**/api/nl2sql/recommend-profile", (route) =>
    fulfillJson(route, {
      recommended_profile_id: "default",
      recommended_profile_name: profile.name,
      confidence: 0.2,
      recommendation_source: "deterministic",
      reasons: [],
      recommended_allowed_objects: { table_names: [], columns: {} },
    })
  );
  await page.route("**/api/nl2sql/rewrite", (route) =>
    fulfillJson(route, {
      original_question: "請求金額を確認したい",
      rewritten_question: "請求金額を確認したい",
      source: "deterministic",
      model: "",
      warnings: [],
    })
  );
  // 応答しないことで送信中（submitting）のまま保ち、ボタンのスピナーを表示したままにする（#431）。
  await page.route("**/api/nl2sql/jobs", () => new Promise<void>(() => undefined));
}

async function startPendingRun(page: Page) {
  await mockWorkbenchWithPendingJob(page);
  await page.goto("/query");
  const button = page.getByRole("button", { name: "SQL を生成して実行" });
  // 負荷が高いと初期化（プロファイル/下書きの復元）が fill の後に走り、入力が空へ戻ってボタンが
  // disabled のまま残ることがある。入力が反映されボタンが有効になるまで fill をやり直す。
  await expect(async () => {
    await page.locator("#nl2sql-question-input").fill("請求金額を確認したい");
    await expect(button).toBeEnabled({ timeout: 1_000 });
  }).toPass({ timeout: 15_000 });
  await button.click();

  const spinner = button.locator(LOADING_ICON_SELECTOR);
  await expect(spinner).toBeVisible();
  return { button, spinner };
}

test("実行ボタンのスピナーは 16px で回転レイヤーが固定されている", { tag: "@responsive" }, async ({ page }) => {
  const { spinner } = await startPendingRun(page);

  const style = await spinner.evaluate((node) => {
    const computed = getComputedStyle(node);
    return {
      width: computed.width,
      height: computed.height,
      willChange: computed.willChange,
      transformBox: computed.transformBox,
      animationName: computed.animationName,
    };
  });

  // children 側の先頭アイコン（Play size=16）と同寸法。loading 切替で寸法が変わらない
  expect(style.width).toBe("16px");
  expect(style.height).toBe("16px");
  // 継続回転する要素として独立レイヤーで合成し、回転原点を図形中心へ固定する
  expect(style.willChange).toContain("transform");
  expect(style.transformBox).toBe("view-box");
  expect(style.animationName).not.toBe("none");
});

test("スピナーは全周トラックを持ちシルエットが回転角に依存しない", { tag: "@responsive" }, async ({ page }) => {
  const { button, spinner } = await startPendingRun(page);

  const shape = await spinner.evaluate((node) => {
    const tracks = Array.from(node.querySelectorAll("circle"));
    return {
      trackCount: tracks.length,
      trackCenter: tracks.map((track) => [track.getAttribute("cx"), track.getAttribute("cy")]),
      arcCount: node.querySelectorAll("path").length,
      viewBox: node.getAttribute("viewBox"),
      ariaHidden: node.getAttribute("aria-hidden"),
    };
  });

  // 共有 Spinner: viewBox 中心に全周トラックを 1 本敷き、その上を 180° 対称の 2 本のアーク（1 つの path）が回る。
  // 閉じた円が常に外形を決めるため、回転角でシルエットが変わらない。
  expect(shape.trackCount).toBe(1);
  expect(shape.trackCenter).toEqual([["12", "12"]]);
  expect(shape.arcCount).toBe(1);
  expect(shape.viewBox).toBe("0 0 24 24");
  expect(shape.ariaHidden).toBe("true");
  // loading 中は aria-busy + disabled。先頭アイコンはスピナー 1 つだけ（二重表示しない）
  await expect(button).toHaveAttribute("aria-busy", "true");
  await expect(button.locator("svg:visible")).toHaveCount(1);
});

test("回転しても見た目の重心が上下・左右に動かず、箱とボタンの位置・高さも変わらない（#1180）", { tag: "@responsive" }, async ({ page }) => {
  const { button } = await startPendingRun(page);
  const box = button.locator(".pr-spinner");
  await expectSpinnerStable(box);
  // 箱はボタンの先頭アイコンと同じ 16px の正方形
  await expect(box).toHaveCSS("width", "16px");
  await expect(box).toHaveCSS("height", "16px");
});

test("スピナーはボタンの垂直中心に配置される", { tag: "@responsive" }, async ({ page }) => {
  const { button, spinner } = await startPendingRun(page);

  const metrics = await spinner.evaluate((node) => {
    const svg = node.getBoundingClientRect();
    const host = (node.closest("button") as HTMLElement).getBoundingClientRect();
    return {
      spinnerCenterY: svg.y + svg.height / 2,
      buttonCenterY: host.y + host.height / 2,
    };
  });

  expect(Math.abs(metrics.spinnerCenterY - metrics.buttonCenterY)).toBeLessThan(0.02);
  await expect(button).toBeDisabled();
});

test("回転中もスピナーの中心座標がフレーム間でドリフトしない", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const { button, spinner } = await startPendingRun(page);

  const samples = await spinner.evaluate(
    (node) =>
      new Promise<{ x: number; y: number }[]>((resolve) => {
        const collected: { x: number; y: number }[] = [];
        const tick = () => {
          const rect = node.getBoundingClientRect();
          collected.push({ x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 });
          if (collected.length >= 8) {
            resolve(collected);
            return;
          }
          requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      })
  );

  const spread = (values: number[]) => Math.max(...values) - Math.min(...values);
  // 外接矩形は回転角で変わるが、中心は固定でなければならない
  expect(spread(samples.map((sample) => sample.x))).toBeLessThan(0.05);
  expect(spread(samples.map((sample) => sample.y))).toBeLessThan(0.05);

  await testInfo.attach(`spinner-${testInfo.project.name}.png`, {
    body: await button.screenshot(),
    contentType: "image/png",
  });
});

test("prefers-reduced-motion でもスピナーは回転を続ける（#440）", { tag: "@responsive" }, async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const { spinner } = await startPendingRun(page);

  await expect
    .poll(() =>
      spinner.evaluate((node) => {
        const style = getComputedStyle(node);
        return `${style.animationName} ${style.animationDuration}`;
      })
    )
    .toBe("spin 1s");
});

test("sm/md/lg と icon-only の loading button でもスピナーは固定寸法で中央に残る", { tag: "@responsive" }, async ({
  page,
}) => {
  const { spinner } = await startPendingRun(page);
  const iconHtml = await spinner.evaluate((node) => node.outerHTML);

  await page.evaluate((stableIconHtml) => {
    const fixture = document.createElement("section");
    fixture.setAttribute("data-testid", "loading-button-size-fixture");
    fixture.className = "fixed left-4 top-4 z-50 flex flex-wrap items-center gap-2 bg-background p-2";
    fixture.innerHTML = [
      { id: "sm", label: "small", height: 32, width: 92 },
      { id: "md", label: "medium", height: 36, width: 112 },
      { id: "lg", label: "large", height: 40, width: 120 },
      { id: "icon", label: "", height: 36, width: 36 },
    ]
      .map(
        (item) => `
          <button
            type="button"
            disabled
            data-testid="loading-button-fixture-${item.id}"
            data-expected-height="${item.height}"
            class="rounded-md border border-border bg-surface-disabled text-sm font-medium leading-5 text-fg-disabled"
            style="display:inline-flex;align-items:center;justify-content:center;gap:6px;height:${item.height}px;width:${item.width}px;overflow:hidden;white-space:nowrap;"
            aria-label="${item.label || "icon-only"}"
          >
            ${stableIconHtml}
            ${item.label ? `<span>${item.label}</span>` : ""}
          </button>
        `
      )
      .join("");
    document.body.appendChild(fixture);
  }, iconHtml);

  const buttons = page.locator("[data-testid^='loading-button-fixture-']");
  await expect(buttons).toHaveCount(4);

  for (const id of ["sm", "md", "lg", "icon"]) {
    const button = page.getByTestId(`loading-button-fixture-${id}`);
    const icon = button.locator(LOADING_ICON_SELECTOR);
    const metrics = await icon.evaluate((node) => {
      const svg = node.getBoundingClientRect();
      const host = (node.closest("button") as HTMLElement).getBoundingClientRect();
      const computed = getComputedStyle(node);
      return {
        width: computed.width,
        height: computed.height,
        spinnerCenterY: svg.y + svg.height / 2,
        buttonCenterY: host.y + host.height / 2,
        buttonHeight: host.height,
        expectedHeight: Number((node.closest("button") as HTMLElement).dataset.expectedHeight),
      };
    });

    expect(metrics.width).toBe("16px");
    expect(metrics.height).toBe("16px");
    expect(metrics.buttonHeight).toBe(metrics.expectedHeight);
    expect(Math.abs(metrics.spinnerCenterY - metrics.buttonCenterY)).toBeLessThan(0.02);
  }
});
