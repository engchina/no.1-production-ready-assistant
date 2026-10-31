import { expect, test, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { dropFiles } from "./_helpers/file-dropzone";
import { closeListToolbarFilters, listToolbarUsesSheet, openListToolbarFilters } from "./_helpers/db-object-toolbar";
import { chooseSelectFieldOption } from "./_helpers/select-field";
import {
  findClippedPlaceholders,
  findToolbarFieldWidthMismatch,
  findToolbarMisalignment,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

/**
 * 質問分類モデル管理（#984）: 候補検索のフォーカス・学習データのページ位置・取込の失敗の理由。
 */

function envelope(route: Route, data: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ data }) });
}

function trainingExample(index: number, text?: string) {
  const no = String(index).padStart(2, "0");
  return {
    id: `example-${no}`,
    category: index % 2 === 0 ? "sales" : "hr",
    text: text ?? `学習データ ${no} の質問`,
    profile_id: index % 2 === 0 ? "sales" : "hr",
    profile_name: index % 2 === 0 ? "売上" : "人事",
    profile_category: "",
    source: "training.xlsx",
    source_type: "file",
    source_history_id: "",
    created_at: "2026-07-19T00:00:00Z",
    updated_at: "2026-07-19T00:00:00Z",
  };
}

async function mockClassifierApi(page: Page, options: { candidateDelayMs?: number } = {}) {
  const state = {
    examples: Array.from({ length: 40 }, (_, index) => trainingExample(index + 1)),
    candidateQueries: [] as string[],
    candidateRequests: [] as URL[],
  };
  await mockDatabaseGateReady(page);
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    envelope(route, {
      items: [
        { id: "sales", name: "売上", category: "", description: "", archived: false },
        { id: "hr", name: "人事", category: "", description: "", archived: false },
      ],
      total: 2,
    })
  );
  await page.route("**/api/nl2sql/classifier", (route) =>
    envelope(route, {
      ready: false,
      trained: false,
      stale: false,
      classifier_version: "",
      updated_at: "",
      example_count: state.examples.length,
      category_count: 2,
      categories: ["hr", "sales"],
      embedding_model: "cohere.embed-v4.0",
      vector_dimension: 1536,
      persistence_mode: "oracle",
      recommendation_source: "deterministic",
      metrics: {},
      trained_example_count: 0,
      pending_change_count: 0,
      warnings: [],
    })
  );
  const trainingData = () => ({
    total_examples: state.examples.length,
    categories: ["hr", "sales"],
    warnings: [],
    examples: state.examples,
  });
  await page.route("**/api/nl2sql/classifier/training-data", (route) => envelope(route, trainingData()));
  await page.route("**/api/nl2sql/classifier/training-data/example-*", (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-1);
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON() as { text: string; profile_id: string };
      state.examples = state.examples.map((example) =>
        example.id === id ? { ...example, text: body.text, profile_id: body.profile_id } : example
      );
      return envelope(route, trainingData().examples.find((example) => example.id === id));
    }
    state.examples = state.examples.filter((example) => example.id !== id);
    return envelope(route, trainingData());
  });
  await page.route("**/api/nl2sql/classifier/training-data/import", (route) =>
    route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({
        data: null,
        error_messages: ["TEXT 列が見つかりません。テンプレートの見出しを確認してください。"],
        warning_messages: [],
      }),
    })
  );
  await page.route("**/api/nl2sql/classifier/training-candidates*", async (route) => {
    const url = new URL(route.request().url());
    const q = url.searchParams.get("q") ?? "";
    state.candidateQueries.push(q);
    state.candidateRequests.push(url);
    if (options.candidateDelayMs) await new Promise((resolve) => setTimeout(resolve, options.candidateDelayMs));
    return envelope(route, {
      items: [
        {
          history_id: `history-${q || "all"}`,
          question: q ? `${q} の候補` : "請求金額を確認したい",
          profile_id: "sales",
          profile_name: "売上",
          profile_category: "",
          feedback_rating: "good",
          feedback_comment: "",
          created_at: "2026-07-19T00:00:00Z",
          status: "pending",
          eligible: true,
          training_example_id: "",
          conflict_profile_ids: [],
        },
      ],
      total: 1,
      next_cursor: "",
      pending_count: 1,
      added_count: 0,
      attention_count: 0,
    });
  });
  return state;
}

test("候補検索は一覧の読み込み中もフォーカスを保ち、続けて入力できる (#984)", async ({ page }) => {
  const state = await mockClassifierApi(page, { candidateDelayMs: 600 });
  await page.goto("/question-classifier-models?tab=candidates");
  const candidates = page.getByTestId("qcm-training-candidate");
  await expect(candidates).toHaveCount(1);

  const search = page.getByTestId("qcm-candidate-filters").getByRole("searchbox", { name: "候補検索" });
  await search.click();
  await page.keyboard.type("請求");
  await expect.poll(() => state.candidateQueries.at(-1)).toBe("請求");
  await expect(search).toBeEnabled();
  await expect(search).toBeFocused();

  await page.keyboard.type("書");
  await expect(search).toHaveValue("請求書");
  await expect(candidates).toContainText("請求書 の候補");
  await expect(search).toBeFocused();
});

test("学習データの 2 ページ目で行を編集・保存しても 2 ページ目に留まる (#984)", async ({ page }) => {
  await mockClassifierApi(page);
  await page.goto("/question-classifier-models");
  const table = page.getByTestId("qcm-training-data-table");
  const pagination = page.getByTestId("qcm-training-data-pagination");
  await expect(pagination).toContainText("1 / 2 ページ");

  await pagination.getByRole("button", { name: "次へ" }).click();
  await expect(pagination).toContainText("2 / 2 ページ");
  await expect(table).toContainText("学習データ 26 の質問");

  await page.getByRole("button", { name: "学習データ 27 の質問 の行操作" }).click();
  await page.getByRole("menuitem", { name: "編集" }).click();
  await page.getByLabel("訓練データの質問").fill("学習データ 27 の質問（修正）");
  await table.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("訓練データを更新しました。モデルは再学習待ちです。")).toBeVisible();
  await expect(table).toContainText("学習データ 27 の質問（修正）");
  await expect(pagination).toContainText("2 / 2 ページ");

  // 検索語を変えたときは 1 ページ目へ戻す。
  await page.getByRole("searchbox", { name: "検索" }).fill("学習データ");
  await expect(pagination).toContainText("1 / 2 ページ");
});

test("学習データの取込の失敗は backend の理由を取込の欄の下に出す (#984)", async ({ page }) => {
  await mockClassifierApi(page);
  await page.goto("/question-classifier-models");
  await dropFiles(page, page.getByTestId("qcm-training-file-field-dropzone"), [
    {
      name: "training_data.xlsx",
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      content: "mock xlsx",
    },
  ]);
  await expect(
    page.getByText("TEXT 列が見つかりません。テンプレートの見出しを確認してください。")
  ).toBeVisible();
  await expect(page.getByText("Classifier 操作に失敗しました。")).toHaveCount(0);
});

test("候補の絞り込みは FilterSelect で 1 行にそろい、狭い画面はシート、条件をクリアで既定に戻す (#1933)", { tag: "@responsive" }, async ({ page, isMobile }) => {
  const state = await mockClassifierApi(page);
  const widths = isMobile ? [375] : [1280, 1920];
  for (const theme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: theme });
    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/question-classifier-models?tab=candidates");
      const toolbar = page.getByTestId("qcm-candidate-filters");
      await expect(page.getByTestId("qcm-training-candidate")).toHaveCount(1);
      // 検索・状態・業務プロファイルの 3 つ。ツールバーが 28rem 未満ならシートにまとめる。
      expect(await listToolbarUsesSheet(toolbar), `${width}px`).toBe(isMobile);
      expect(await findToolbarMisalignment(page), `${width}px・${theme}`).toEqual([]);
      expect(await findToolbarFieldWidthMismatch(page), `${width}px・${theme}`).toEqual([]);
      expect(await findClippedPlaceholders(page), `${width}px・${theme}`).toEqual([]);
      expect(await measureHorizontalOverflow(page), `${width}px・${theme}`).toBeLessThanOrEqual(1);
    }
  }

  const toolbar = page.getByTestId("qcm-candidate-filters");
  const filters = await openListToolbarFilters(toolbar);
  const status = filters.getByRole("combobox", { name: "状態", exact: true });
  await expect(status).toContainText(/状態:\s*すべて/);
  await expect(filters.getByRole("combobox", { name: "業務プロファイル", exact: true })).toContainText(/業務プロファイル:\s*すべて/);
  await chooseSelectFieldOption(status, "pending");
  await expect.poll(() => state.candidateRequests.at(-1)?.searchParams.get("status")).toBe("pending");
  await closeListToolbarFilters(page);

  // 絞り込みは作業状態に残す（再読込の後も同じ条件）。
  await page.reload();
  await expect.poll(() => state.candidateRequests.at(-1)?.searchParams.get("status")).toBe("pending");
  const clear = toolbar.getByRole("button", { name: "条件をクリア", exact: true });
  await clear.click();
  await expect.poll(() => state.candidateRequests.at(-1)?.searchParams.get("status")).toBe("all");
  await expect(clear).toHaveCount(0);
});

test("学習の結果の警告はボタンの直下の 1 か所だけに出し、状態の欄に重ねない (#2034)", async ({ page }) => {
  await mockClassifierApi(page);
  const warning = "学習データが 1 件だけの分類があるため、学習を完了できませんでした。";
  await page.route("**/api/nl2sql/classifier/train", (route) =>
    envelope(route, {
      ready: false,
      trained: false,
      stale: false,
      classifier_version: "",
      updated_at: "",
      example_count: 40,
      category_count: 2,
      categories: ["hr", "sales"],
      embedding_model: "cohere.embed-v4.0",
      vector_dimension: 1536,
      persistence_mode: "oracle",
      recommendation_source: "deterministic",
      metrics: {},
      trained_example_count: 0,
      pending_change_count: 0,
      warnings: [warning],
    })
  );
  await page.goto("/question-classifier-models");
  await page.getByRole("tab", { name: "モデル学習" }).click();
  const trainButton = page.getByRole("button", { name: "Classifier 学習" });
  await trainButton.click();
  await expect(page.getByText(warning)).toHaveCount(1);
  const warningBox = await page.getByText(warning).boundingBox();
  const buttonBox = await trainButton.boundingBox();
  expect(warningBox!.y).toBeGreaterThan(buttonBox!.y);
});

test("読み込みの失敗の「再試行」は案内を残したまま押したボタンだけを回し、取り直せたら案内を消す (#2034)", async ({ page }) => {
  await mockClassifierApi(page);
  let failing = true;
  let releaseRetry: () => void = () => undefined;
  const retryGate = new Promise<void>((resolve) => {
    releaseRetry = resolve;
  });
  let calls = 0;
  await page.route("**/api/nl2sql/classifier/training-data", async (route) => {
    calls += 1;
    if (failing) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "一時的に読み込めません。" }),
      });
      return;
    }
    await retryGate;
    await envelope(route, { total_examples: 0, categories: [], warnings: [], examples: [] });
  });
  await page.goto("/question-classifier-models");
  const retry = page.getByRole("button", { name: "再試行" });
  await expect(retry).toBeVisible();
  failing = false;
  await retry.click();
  // 押しても案内は消えず、進行は案内の中のボタンの直下に出る。
  const processing = page.getByTestId("question-learning-retry-processing");
  await expect(processing).toBeVisible();
  await expect(retry).toHaveAttribute("aria-busy", "true");
  await expect(page.getByTestId("question-learning-workspace-processing")).toHaveCount(0);
  const buttonBox = await retry.boundingBox();
  const processingBox = await processing.boundingBox();
  expect(processingBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height - 1);
  releaseRetry();
  await expect(retry).toHaveCount(0);
  await expect(processing).toHaveCount(0);
  expect(calls).toBeGreaterThanOrEqual(2);
});
