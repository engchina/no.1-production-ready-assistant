import { expect, test, type Page } from "./fixtures/test";
import { expectedControlHeight, expectNoPageOverflow, mockLocalAuth } from "./_helpers";

type ServiceStatus =
  | "running"
  | "degraded"
  | "starting"
  | "failed"
  | "stopped"
  | "not_installed"
  | "unconfigured";

interface ServiceRow {
  service_id: string;
  category: "preprocess" | "parser";
  profile: "cpu" | "gpu";
  label_key: string;
  execution_policy: "selected_adapter" | "default_adapter";
  status: ServiceStatus;
  configured: boolean;
}

function defaultServices(): ServiceRow[] {
  return [
    {
      service_id: "preprocess-office-to-pdf",
      category: "preprocess",
      profile: "cpu",
      label_key: "settings.services.item.preprocessOfficeToPdf",
      execution_policy: "selected_adapter",
      status: "running",
      configured: true,
    },
    {
      service_id: "parser-docling",
      category: "parser",
      profile: "cpu",
      label_key: "settings.services.item.parserDocling",
      execution_policy: "default_adapter",
      status: "stopped",
      configured: true,
    },
    {
      service_id: "parser-unstructured",
      category: "parser",
      profile: "cpu",
      label_key: "settings.services.item.parserUnstructured",
      execution_policy: "selected_adapter",
      status: "stopped",
      configured: true,
    },
  ];
}

async function mockServices(
  page: Page,
  options: {
    controlEnabled?: boolean;
    deploymentMode?: "local" | "staging" | "production";
    services?: ServiceRow[];
  } = {}
) {
  const state = {
    control_enabled: options.controlEnabled ?? false,
    deployment_mode: options.deploymentMode ?? "production",
    services: options.services ?? defaultServices(),
  };
  await page.route("**/api/services/catalog", async (route) => {
    const catalogServices = state.services.map((service) => ({
      service_id: service.service_id,
      category: service.category,
      profile: service.profile,
      label_key: service.label_key,
      execution_policy: service.execution_policy,
      configured: service.configured,
      systemd_unit: `production-ready-rag-${service.service_id}.service`,
      model_cache:
        service.service_id === "parser-docling"
          ? { path: "/var/lib/production-ready-rag/.cache", editable: false }
          : null,
    }));
    await route.fulfill({
      json: {
        data: { ...state, services: catalogServices },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/services/*/status", async (route) => {
    const id = route.request().url().match(/services\/([^/]+)\/status/)?.[1] ?? "";
    const target = state.services.find((s) => s.service_id === decodeURIComponent(id));
    await route.fulfill({
      status: target ? 200 : 404,
      json: {
        data: target ?? null,
        error_messages: target ? [] : ["指定したサービスが見つかりません。"],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/services/*/logs**", async (route) => {
    const id = route.request().url().match(/services\/([^/]+)\/logs/)?.[1] ?? "";
    const serviceId = decodeURIComponent(id);
    await route.fulfill({
      json: {
        data: {
          service_id: serviceId,
          source: "journald",
          lines: 200,
          content: `${serviceId} boot complete\nGET /health 200 OK`,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/services/*/start", async (route) => {
    const id = route.request().url().match(/services\/([^/]+)\/start/)?.[1] ?? "";
    const target = state.services.find((s) => s.service_id === decodeURIComponent(id));
    if (target) target.status = "running";
    await route.fulfill({
      json: {
        data: { service_id: id, action: "start", status: "running" },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/services/*/stop", async (route) => {
    const id = route.request().url().match(/services\/([^/]+)\/stop/)?.[1] ?? "";
    const target = state.services.find((s) => s.service_id === decodeURIComponent(id));
    if (target) target.status = "stopped";
    await route.fulfill({
      json: {
        data: { service_id: id, action: "stop", status: "stopped" },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/services/*/restart", async (route) => {
    const id = route.request().url().match(/services\/([^/]+)\/restart/)?.[1] ?? "";
    const target = state.services.find((s) => s.service_id === decodeURIComponent(id));
    if (target) target.status = "running";
    await route.fulfill({
      json: {
        data: { service_id: id, action: "restart", status: "running" },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
}

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`サービス管理は稼働状態を表示する (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockServices(page);

    await page.goto("/settings/services");

    await expect(page.getByRole("heading", { name: "マイクロサービス" })).toBeVisible();
    // 工程の見出しはサイドナビ（取込・検索・回答設定）と同じ並び・名前（#638）。
    // ナビのリンクと区別するため heading role で限定する。
    await expect(page.getByTestId("service-row-pipeline-chunking")).toHaveCount(0);
    // backend の中で動く段（文書分割・検索インデックス・文書構造索引・安全チェック・評価の基準）は
    // サービスを持たない（#3218）。
    for (const internal of ["文書分割", "検索インデックス", "文書構造索引", "安全チェック", "評価の基準"]) {
      await expect(page.getByRole("heading", { name: internal, exact: true })).toHaveCount(0);
    }
    await expect(page.getByText("backend 内処理", { exact: false })).toHaveCount(0);
    // 文書解析は CPU のグループだけなので、節の名前に(CPU)を付けない（#22・#21）。
    const stageHeadings = ["ファイル準備", "文書解析"];
    for (const name of stageHeadings) {
      await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
    }
    const headingTops = await Promise.all(
      stageHeadings.map(async (name) => {
        const box = await page.getByRole("heading", { name, exact: true }).boundingBox();
        return box?.y ?? -1;
      })
    );
    expect(headingTops).toEqual([...headingTops].sort((a, b) => a - b));
    await page.locator("main").screenshot({ path: test.info().outputPath("services-without-chunking.png") });
    for (const removed of ["前処理 (Preprocess)", "解析 (Parser)(CPU)", "品質評価", "関係情報"]) {
      await expect(page.getByRole("heading", { name: removed, exact: true })).toHaveCount(0);
    }
    // 工程の説明は、対応する設定画面の説明と同じ文（複数のグループに分かれる工程は最初のグループにだけ出す）。
    await expect(
      page.getByText("文書解析の前にファイルを変換する方式を選択します。", { exact: true })
    ).toBeVisible();
    await expect(
      page.getByText("文書解析に使う方式を選び、必要な解析エンジンの現在状態を確認します。", { exact: true })
    ).toHaveCount(1);
    // 構造化レコード JSONL・XML ツリー JSON の解析のサービスは削除した（#21）。
    for (const removed of ["構造化レコード JSONL", "XML ツリー JSON"]) {
      await expect(page.getByText(removed, { exact: true })).toHaveCount(0);
    }
    // GPU の文書解析（ASR）と OCI Generative AI (Vision) は削除した（#2042）。GPU の節は出さない。
    await expect(page.getByRole("heading", { name: "文書解析(GPU)", exact: true })).toHaveCount(0);
    for (const removed of [
      "Marker",
      "Unlimited-OCR",
      "MinerU",
      "Dots.OCR",
      "GLM-OCR",
      "ASR(音声文字起こし)",
      "OCI Generative AI (Vision)",
      "OCI Document Understanding",
    ]) {
      await expect(page.getByText(removed, { exact: true })).toHaveCount(0);
    }
    // 単一プロファイルの工程は接尾辞なし（上の見出しの確認に含む）。
    await expect(page.getByText("選択時のみ使用").first()).toBeVisible();
    await expect(
      page.getByText("ファイル準備・文書解析の設定か処理レシピでこのサービスを選んだ場合だけ", { exact: false }).first()
    ).toBeVisible();
    // 稼働状態バッジ。
    await expect(page.getByText("稼働中").first()).toBeVisible();
    await expect(page.getByText("停止").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`サービスログを行内で確認できる (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockServices(page);

    await page.goto("/settings/services");

    // ログは行の RowActionMenu に入っている（#158）。
    await page.getByRole("button", { name: "Docling の操作" }).click();
    await page.getByRole("menuitem", { name: "ログ" }).click();
    await expect(page.getByText("Docling のログ")).toBeVisible();
    await expect(
      page.getByText("journalctl -u production-ready-rag-parser-docling.service / 最新 200 行")
    ).toBeVisible();
    await expect(page.getByText("parser-docling boot complete")).toBeVisible();
    await expect(page.locator('[data-surface="code"]').getByRole("button", { name: "ログの最新情報を取得" })).toBeVisible();
    // サービスの一覧とログの取り直しは名前で区別する（同じ名前のボタンを 2 つ出さない。#1868）。
    await expect(page.getByRole("button", { name: "最新情報を取得", exact: true })).toHaveCount(1);
    await expect(page.getByRole("button", { name: "コピー" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });
}

test("制御無効時(prod)は起動/停止ボタンが disabled", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockServices(page, { controlEnabled: false, deploymentMode: "production" });

  await page.goto("/settings/services");

  await expect(page.getByText("本番 (systemd)")).toBeVisible();
  await expect(page.getByText("無効(可視化のみ)")).toBeVisible();
  await expect(
    page.getByText("起動/停止は無効です。", { exact: false })
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Docling 起動" })
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Office→PDF 停止" })
  ).toBeDisabled();
  // メニュー内の再起動も無効。
  await page.getByRole("button", { name: "Office→PDF の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "再起動" })).toBeDisabled();
});

test("dev モードは systemd バッジと有効化された制御を表示する", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockServices(page, { controlEnabled: true, deploymentMode: "local" });

  await page.goto("/settings/services");

  await expect(page.getByText("開発 (systemd)")).toBeVisible();
  await expect(
    page.getByText("開発モード", { exact: false })
  ).toBeVisible();
  // dev は制御が有効なので起動ボタンが押せる。
  await expect(
    page.getByRole("button", { name: "Docling 起動" })
  ).toBeEnabled();
  await expect(page.getByRole("button", { name: "Unstructured 起動" })).toBeEnabled();
});

test("稼働中のサービスは操作メニューから確認なしで再起動できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockServices(page, { controlEnabled: true, deploymentMode: "local" });

  await page.goto("/settings/services");

  await page.getByRole("button", { name: "Office→PDF の操作" }).click();
  await page.getByRole("menuitem", { name: "再起動" }).click();
  await expect(page.getByText("Office→PDF を再起動しました。")).toBeVisible();
  // Docker のイメージ build / コンテナ削除の操作は出さない（#286）。
  await page.getByRole("button", { name: "Office→PDF の操作" }).click();
  await expect(page.getByRole("menuitem", { name: "ビルド" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "削除" })).toHaveCount(0);
});

test("unit が未登録・起動失敗のサービスは状態と案内を出す", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  const services = defaultServices().map((service) =>
    service.service_id === "parser-unstructured"
      ? { ...service, status: "not_installed" as const }
      : service.service_id === "parser-docling"
        ? { ...service, status: "failed" as const }
        : service
  );
  await mockServices(page, { controlEnabled: true, deploymentMode: "production", services });

  await page.goto("/settings/services");

  const unstructured = page.getByTestId("service-row-parser-unstructured");
  await expect(unstructured.getByText("未登録")).toBeVisible();
  await expect(
    unstructured.getByText("systemd の unit が登録されていません", { exact: false })
  ).toBeVisible();
  // unit が無いので起動できない。
  await expect(page.getByRole("button", { name: "Unstructured 起動" })).toBeDisabled();
  const docling = page.getByTestId("service-row-parser-docling");
  await expect(docling.getByText("起動失敗")).toBeVisible();
  await expect(docling.getByText("ログで原因を確認", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Docling 起動" })).toBeEnabled();
  await expect(docling.getByText("/var/lib/production-ready-rag/.cache")).toBeVisible();
});

for (const mode of ["local", "staging", "production"] as const) {
test(`実行コマンドはディレクトリと対象範囲を正確に案内する（${mode}）`, { tag: "@responsive" }, async ({
  page,
}) => {
  await mockServices(page, { controlEnabled: true, deploymentMode: mode });

  await page.goto("/settings/services");

  const status = "systemctl list-units --all 'production-ready-rag-*'";
  // 既定では閉じている(コマンドは描画されない)。
  await expect(page.getByText(status, { exact: false })).toHaveCount(0);

  // 開閉の見出しは共有の Disclosure の <summary>。高さは lg 40px・タッチ端末 44px（以前は手書きで 34px。#1135）。
  const commandsSummary = page.locator("summary", { hasText: "実行コマンド" });
  await expect(commandsSummary).toBeVisible();
  expect(Math.round((await commandsSummary.boundingBox())?.height ?? 0)).toBe(
    await expectedControlHeight(page, "lg")
  );
  await commandsSummary.click();

  await expect(page.getByText(status, { exact: false }).first()).toBeVisible();
  await expect(
    page.getByText("sudo journalctl -u production-ready-rag-parser-docling.service -f").first()
  ).toBeVisible();
  // dev は unit と sudoers を登録するコマンドも出す。
  const panel = page.locator("#service-commands");
  await expect(panel).toContainText("状態とログの確認は、どの作業ディレクトリでも使えます。");
  await expect(panel).toContainText("ロード済みの RAG サービスの状態を確認");
  if (mode === "local") {
    await expect(panel.getByText("scripts/rag-services.sh install", { exact: true })).toBeVisible();
    await expect(panel).toContainText("CPU サービス、rag/ で実行");
  } else {
    await expect(panel.getByText("scripts/rag-services.sh install", { exact: true })).toHaveCount(0);
    await expect(panel).not.toContainText("rag/ で実行");
  }
  await expectNoPageOverflow(page);
  // GPU のサービスは無い（ASR は削除した。#2042）。
  await expect(page.getByText("scripts/rag-services.sh install --gpu", { exact: true })).toHaveCount(0);
  await expect(page.getByText("docker", { exact: false })).toHaveCount(0);
});

}

test("制御有効時は確認ダイアログを経て停止できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockServices(page, { controlEnabled: true });

  await page.goto("/settings/services");

  // 起動中の Office→PDF を停止 → 確認ダイアログ → トースト。
  await page.getByRole("button", { name: "Office→PDF 停止" }).click();
  await expect(page.getByRole("heading", { name: "サービスを停止しますか?" })).toBeVisible();
  await page.getByRole("button", { name: "停止する" }).click();
  await expect(page.getByText("Office→PDF を停止しました。")).toBeVisible();
  // 停止後は行の主操作が「起動」に切り替わる。
  await expect(page.getByRole("button", { name: "Office→PDF 起動" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Office→PDF 停止" })).toHaveCount(0);
});

test("制御有効時は確認なしで起動できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockServices(page, { controlEnabled: true });

  await page.goto("/settings/services");

  await page.getByRole("button", { name: "Docling 起動" }).click();
  await expect(page.getByText("Docling を起動しました。")).toBeVisible();
  // 起動後は行の主操作が「停止」に切り替わる。
  await expect(page.getByRole("button", { name: "Docling 停止" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Docling 起動" })).toHaveCount(0);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`起動の間は行に経過時間を出す (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockServices(page, { controlEnabled: true });
    // 起動の応答を止めて、実行中の表示（#376）を確かめてから mockServices の応答へ渡す。
    let release: () => void = () => {};
    const released = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route("**/api/services/*/start", async (route) => {
      await released;
      await route.fallback();
    });

    await page.goto("/settings/services");
    await page.getByRole("button", { name: "Docling 起動" }).click();

    const processing = page.getByTestId("service-processing-parser-docling");
    await expect(processing).toContainText("Docling を起動しています");
    await expect(processing.getByRole("timer")).toHaveAccessibleName(/経過時間 \d{2}:\d{2}/);
    await expect(processing).toHaveAttribute("data-processing-activity-icon", "none");
    await expectNoPageOverflow(page);

    release();
    await expect(page.getByText("Docling を起動しました。")).toBeVisible();
    await expect(processing).toHaveCount(0);
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`各行は状態に応じた主操作 1 つと操作メニュー 1 つだけを出す (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockServices(page, { controlEnabled: true, deploymentMode: "local" });

    await page.goto("/settings/services");
    await expect(page.getByRole("heading", { name: "マイクロサービス" })).toBeVisible();

    const expected: Record<string, "起動" | "停止"> = {
      "preprocess-office-to-pdf": "停止",
      "parser-docling": "起動",
      "parser-unstructured": "起動",
    };
    for (const [serviceId, primary] of Object.entries(expected)) {
      const row = page.getByTestId(`service-row-${serviceId}`);
      await expect(row).toBeVisible();
      const buttons = row.getByRole("button");
      // 主操作のボタン + メニューのトリガーの 2 個だけ。
      await expect(buttons).toHaveCount(2);
      await expect(row.locator('[aria-haspopup="menu"]')).toHaveCount(1);
      await expect(row.getByTestId(`service-primary-action-${serviceId}`)).toHaveText(primary);
      for (const moved of ["ログ", "再起動"]) {
        await expect(row.getByRole("button", { name: moved, exact: true })).toHaveCount(0);
      }
    }
    await expectNoHorizontalOverflow(page);

    // 稼働中の行のメニューには ログ / 再起動 が入る（停止中はログだけ）。
    await page.getByRole("button", { name: "Office→PDF の操作" }).click();
    const menu = page.getByRole("menu");
    await expect(menu.getByRole("menuitem")).toHaveText(["ログ", "再起動"]);
    await expectNoHorizontalOverflow(page);
    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Office→PDF の操作" })).toBeFocused();
    await page.getByRole("button", { name: "Docling の操作" }).click();
    await expect(page.getByRole("menu").getByRole("menuitem")).toHaveText(["ログ"]);
    await page.keyboard.press("Escape");
  });
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`文書解析の節は 1 つで、Docling → Unstructured（#2042・#22・#21, ${viewport.name}）`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockServices(page, { controlEnabled: true, deploymentMode: "local" });

    await page.goto("/settings/services");
    await expect(page.getByTestId("service-row-parser-docling")).toBeVisible();

    const parserRows = await page
      .locator('[data-testid^="service-row-parser-"]')
      .evaluateAll((elements) =>
        elements.map((element) => ({
          id: element.getAttribute("data-testid"),
          top: element.getBoundingClientRect().top,
        }))
      );
    expect(parserRows.map((row) => row.id)).toEqual([
      "service-row-parser-docling",
      "service-row-parser-unstructured",
    ]);
    for (let index = 1; index < parserRows.length; index += 1) {
      expect(parserRows[index].top).toBeGreaterThan(parserRows[index - 1].top);
    }
    // CPU の行だけなので節は 1 つ（名前に(CPU)を付けない）。GPU・OCI の節は出さない。
    await expect(page.getByRole("heading", { name: "文書解析", exact: true })).toBeVisible();
    for (const suffix of ["CPU", "GPU", "OCI"]) {
      await expect(page.getByRole("heading", { name: `文書解析(${suffix})`, exact: true })).toHaveCount(0);
    }
    await expect(page.getByText(/ASR|OCI Generative AI/)).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
  });
}

test("取得に失敗したら再試行できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await page.route("**/api/services/catalog", async (route) => {
    await route.fulfill({
      status: 503,
      json: {
        data: null,
        error_messages: ["サービス一覧を取得できませんでした。"],
        warning_messages: [],
      },
    });
  });

  await page.goto("/settings/services");

  await expect(page.getByRole("alert")).toContainText("サービス一覧を取得できませんでした。");
  await expect(page.getByRole("button", { name: "再試行" })).toBeVisible();
});

test("別のサービスの操作を続けて始めても、先の操作の実行中の表示と結果を失わない", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockServices(page, { controlEnabled: true });
  const releases = new Map<string, () => void>();
  await page.route("**/api/services/*/start", async (route) => {
    const id = decodeURIComponent(route.request().url().match(/services\/([^/]+)\/start/)?.[1] ?? "");
    await new Promise<void>((resolve) => releases.set(id, resolve));
    await route.fallback();
  });

  await page.goto("/settings/services");
  await page.getByRole("button", { name: "Docling 起動" }).click();
  await expect(page.getByTestId("service-processing-parser-docling")).toBeVisible();
  await page.getByRole("button", { name: "Unstructured 起動" }).click();
  await expect(page.getByTestId("service-processing-parser-unstructured")).toBeVisible();
  // 先に始めた Docling の起動はまだ終わっていない。
  await expect(page.getByTestId("service-processing-parser-docling")).toBeVisible();
  await expect(page.getByRole("button", { name: "Docling 起動" })).toBeDisabled();

  releases.get("parser-docling")?.();
  await expect(page.getByText("Docling を起動しました。")).toBeVisible();
  await expect(page.getByTestId("service-processing-parser-docling")).toHaveCount(0);
  // 後から始めた Unstructured の起動は続いている。
  await expect(page.getByTestId("service-processing-parser-unstructured")).toBeVisible();
  releases.get("parser-unstructured")?.();
  await expect(page.getByText("Unstructured を起動しました。")).toBeVisible();
  await expect(page.getByTestId("service-processing-parser-unstructured")).toHaveCount(0);
});

// 処理の失敗は Toast ではなく、操作した行の直下に danger の Banner で出し、技術的な詳細は
// 開いた「詳細」に畳む（UX 契約 messaging.md §10）。次の操作を始めたら消す。
for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`起動の失敗は操作した行の直下に Banner で出す (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockServices(page, { controlEnabled: true });
    let fail = true;
    await page.route("**/api/services/*/start", async (route) => {
      if (!fail) return route.fallback();
      await route.fulfill({
        status: 502,
        json: {
          data: null,
          error_messages: ["systemd の unit を起動できませんでした。ログを確認してください。"],
          error_code: "service_control_failed",
          warning_messages: [],
        },
      });
    });

    await page.goto("/settings/services");
    await page.getByRole("button", { name: "Docling 起動" }).click();

    const row = page.getByTestId("service-row-parser-docling");
    const failure = row.getByTestId("service-failure-parser-docling");
    await expect(failure).toBeVisible();
    await expect(failure).toContainText("Docling を起動できませんでした。");
    await expect(failure).toContainText("systemd の unit を起動できませんでした。ログを確認してください。");
    const details = failure.locator("details");
    await expect(details).toHaveAttribute("open", "");
    await expect(details).toContainText("502");
    await expect(details).toContainText("service_control_failed");
    // 失敗は Toast に出さない（同じ結果を 1 か所に出す）。
    await expect(page.locator("[data-toast-placement]")).not.toContainText("Docling");
    // ほかのサービスの行には出さない。
    await expect(
      page
        .getByTestId("service-row-parser-unstructured")
        .getByTestId("service-failure-parser-unstructured")
    ).toHaveCount(0);
    await expectNoHorizontalOverflow(page);

    // もう一度実行したら前の失敗を消し、成功は Toast で知らせる。
    fail = false;
    await page.getByRole("button", { name: "Docling 起動" }).click();
    await expect(page.getByText("Docling を起動しました。")).toBeVisible();
    await expect(failure).toHaveCount(0);
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(1);
}


test("既定の解析サービスには使用方針と停止の影響を表示する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const services = defaultServices().map((service) => service.service_id === "parser-docling"
    ? { ...service, status: "running" as const } : service);
  await mockServices(page, { controlEnabled: true, services });
  await page.goto("/settings/services");
  const row = page.getByTestId("service-row-parser-docling");
  await expect(row.getByText("既定で使用", { exact: true })).toBeVisible();
  await expect(row.getByText("選択時のみ使用", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Docling 停止" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("Docling は既定の解析エンジンです");
  await expect(dialog).toContainText("全体設定を継承する取込の解析が失敗します");
  await page.screenshot({ path: testInfo.outputPath("default-parser-stop.png"), fullPage: true });
  await dialog.getByRole("button", { name: "停止する", exact: true }).click();
  await expect(row.getByText("停止中です。このエンジン", { exact: false })).toBeVisible();
  await expect(row).toContainText("取込前に起動してください");
  await expectNoPageOverflow(page);
});


test("検証環境の表示と制御制限が画面幅に依存しない", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockServices(page, { controlEnabled: false, deploymentMode: "staging" });
  await page.goto("/settings/services");
  await expect(page.getByText("検証 (systemd)")).toBeVisible();
  await expect(page.getByRole("button", { name: "Docling 起動" })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("staging-services.png"), fullPage: true });
});
