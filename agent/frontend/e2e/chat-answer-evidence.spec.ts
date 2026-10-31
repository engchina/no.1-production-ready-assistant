import type { Page, Route } from "@playwright/test";

import type { AnswerValidationPassageContent } from "../src/lib/api";
import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #2328: Agent のチャットの回答を RAG のチャットと同じ共有の部品で出す。
// - 本文: 要約 →「資料により確認できた事項」（各項目の下に「根拠：文書 p.N」）→「資料から確認できなかった事項」。
//   本文の根拠の ID の印は文字で出さず、根拠の行にする。
// - 根拠の塊: 「AI に渡した N 件 ／ 回答で引用 M 件 …」の折りたたみ（既定は閉じる）、カード、一定の高さの中のスクロール。
// - 外した内容は本文の節に 1 回だけ全件を出し、「資料との照合」は件数のバッジだけ（節の項目の数と一致。#2361）。
//   項目が多い節は共通の「すべて表示」で畳む（「ほか N 件」で切らない）。
// - 根拠のパネルの下に、RAG と同じ共通の原文のプレビュー（ページの画像・強調・ツールバー）を出す（#2389）。
//   原文は Agent の backend が閲覧者として RAG に作らせた短命の署名つきの URL（`http://rag.e2e.test/**`）で読む。
// - 図の根拠（AI が図を読んだ説明・`image_ref` あり）にも、注記・「図を開く」・文書の詳細へのリンクの帯は出さない（#2462）。
// 内容はすべて合成のデータ。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];
const THREAD_ID = `thread_${"e".repeat(32)}`;
const GOAL = "合成の質問 A は？";
const RUN_ID = "run-evidence-1";
const CHUNK_A = "0f3a9c2e7b1d4e5f:cs_a:7";
const CHUNK_B = "0f3a9c2e7b1d4e5f:cs_a:12";
const CHUNK_C = "doc-c:cs_c:1";
const WITHHELD_QUOTE = "期限の延長は 2 回までです。";
const ANSWER = [
  "合成の質問 A の答えは次のとおりです。",
  "",
  "- 申請は 30 日前までに行います【証拠 ID: 0f3a9c2…:7】。",
  "- 承認は担当の部署が行います。",
  "",
  "**資料で確認できず除いた内容**",
  "",
  "根拠で確かめられなかった次の内容は、回答に載せていません。",
  `- 根拠で確かめられない: 「${WITHHELD_QUOTE}」`,
].join("\n");

// 120×60 の単色の PNG（RAG が返すページの画像の代わり）。
const PAGE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAHgAAAA8CAIAAAAiz+n/AAAAaklEQVR42u3QQQ0AAAgEoEtiMBMbyxbOBxsJSPVwIApEi0a0aNEWRItGtGjRFkSLRrRo0YgWjWjRohEtGtGiRSNaNKJFi0a0aESLFo1o0YgWLRrRohEtWjSiRSNatGhEi0a0aNGIFo3oXxYAgSoOxqX2IQAAAABJRU5ErkJggg==",
  "base64"
);

/** RAG の原文のプレビューの URL（ページの画像・本文）への応答。読んだ URL と Referer を返す。 */
async function serveSourcePreviews(page: Page, text = "") {
  const seen: { url: string; referer: string | undefined }[] = [];
  await page.route("http://rag.e2e.test/**", async (route: Route) => {
    const url = route.request().url();
    seen.push({ url, referer: route.request().headers().referer });
    if (url.includes("/content")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json; charset=utf-8",
        headers: { "Access-Control-Allow-Origin": "*", "Cache-Control": "private, no-store" },
        body: text,
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "image/png",
      headers: { "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer" },
      body: PAGE_PNG,
    });
  });
  return seen;
}

function evidenceItem(chunkId: string, overrides: Record<string, unknown> = {}) {
  return {
    document_id: chunkId.split(":")[0],
    chunk_id: chunkId,
    file_name: `文書 ${chunkId.slice(0, 1).toUpperCase()}.pdf`,
    excerpt: `合成の本文（${chunkId}）。`,
    truncated: false,
    locator: { page_start: 3, page_end: 3, section_path: ["第 1 章"] },
    role: "retrieved_anchor",
    score: 0.812,
    rerank_score: 0.64,
    section_title: "第 1 節",
    cited: false,
    answer_use: null,
    ...overrides,
  };
}

function seedRun(mockApi: MockApi, excerptA?: string) {
  mockApi.state.runs.push({
    id: RUN_ID,
    goal: GOAL,
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: THREAD_ID,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    artifacts: [
      {
        id: "artifact-validation",
        kind: "answer_validation",
        name: "資料との照合",
        created_at: MOCK_NOW,
        content: {
          status: "completed",
          reason: null,
          valid: false,
          result: {
            status: "completed",
            valid: false,
            claims: [
              {
                answer_quote: "- 承認は担当の部署が行います。",
                status: "supported",
                chunk_id: CHUNK_B,
                reason: "合成の理由",
              },
              { answer_quote: WITHHELD_QUOTE, status: "unsupported", reason: "合成の理由" },
            ],
            findings: [],
            missing_evidence: [],
            stale_evidence: [],
          },
          withheld: { claims: 1, findings: 0, all: false },
          notice: { items: 1, heading: "資料で確認できず除いた内容" },
        },
      },
      {
        id: "artifact-evidence",
        kind: "answer_evidence",
        name: "回答の根拠",
        created_at: MOCK_NOW,
        content: {
          items: [
            evidenceItem(CHUNK_A, {
              cited: true,
              answer_use: "confirmed",
              // 図の根拠（AI が図を読んだ説明）。帯を出さないことを確かめる（#2462）。
              evidence_type: "figure_description",
              image_ref: { document_id: "0f3a9c2e7b1d4e5f", chunk_id: CHUNK_A, page: 3, bbox: [60, 80, 300, 200], bbox_unit: "absolute" },
              ...(excerptA ? { excerpt: excerptA } : {}),
            }),
            evidenceItem(CHUNK_B, { cited: true, locator: { page_start: 5, page_end: 5, section_path: [] } }),
            evidenceItem(CHUNK_C, { file_name: "文書 C.pdf", rerank_score: null }),
          ],
          passed: 3,
          cited: 2,
          markers: [{ text: "【証拠 ID: 0f3a9c2…:7】", chunk_ids: [CHUNK_A] }],
          passages: [
            { text: "- 申請は 30 日前までに行います【証拠 ID: 0f3a9c2…:7】。", chunk_ids: [CHUNK_A] },
            { text: "- 承認は担当の部署が行います。", chunk_ids: [CHUNK_B] },
          ],
        },
      },
      { id: "artifact-answer", kind: "answer", name: "回答", created_at: MOCK_NOW, content: { text: ANSWER } },
    ],
  });
}

async function openThread(page: Page) {
  const viewport = page.viewportSize();
  await page.getByTestId("chat-history-toggle").click();
  if (viewport && viewport.width >= 1024) {
    await page.getByTestId("chat-history").getByRole("button", { name: new RegExp(`^${GOAL}`) }).click();
    return;
  }
  await page.getByRole("dialog", { name: "会話の履歴" }).getByText(GOAL).click();
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

for (const viewport of VIEWPORTS) {
  test(`回答を RAG と同じ節・根拠の行・根拠の塊で出し、外した内容は 1 回だけ出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedRun(mockApi);
    await serveSourcePreviews(page);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);
    const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
    const body = turn.getByTestId(`chat-answer-${RUN_ID}`);

    // 要約の段落 →「資料により確認できた事項」→「資料から確認できなかった事項」。
    await expect(body.locator("p").first()).toHaveText("合成の質問 A の答えは次のとおりです。");
    const sections = body.getByTestId("answer-section");
    await expect(sections.locator("h4")).toHaveText(["資料により確認できた事項", "資料から確認できなかった事項"]);
    // 処理の経過と回答の本文の間に、節の見出しと同じ書式の見出し「回答」（h3）を出す（#3433）。
    const answerHeading = turn.getByRole("heading", { level: 3, name: "回答", exact: true });
    await expect(answerHeading).toBeVisible();
    await expect(turn.getByRole("region", { name: "回答", exact: true }).getByTestId(`chat-answer-${RUN_ID}`)).toBeVisible();
    const [progressBox, answerHeadingBox, bodyBox] = await Promise.all([
      turn.getByTestId("chat-progress").boundingBox(),
      answerHeading.boundingBox(),
      body.boundingBox(),
    ]);
    expect(answerHeadingBox!.y).toBeGreaterThan(progressBox!.y);
    expect(bodyBox!.y).toBeGreaterThan(answerHeadingBox!.y);
    const headingStyle = (locator: typeof answerHeading) =>
      locator.evaluate((element) => {
        const { fontSize, fontWeight, color, lineHeight } = getComputedStyle(element);
        return { fontSize, fontWeight, color, lineHeight };
      });
    expect(await headingStyle(answerHeading)).toEqual(await headingStyle(sections.nth(0).locator("h4")));
    const confirmed = sections.nth(0).getByRole("listitem");
    await expect(confirmed).toHaveCount(2);
    // 根拠の ID の印は文字で出さず、根拠の行（「根拠：文書 p.N」）にする。
    await expect(body).not.toContainText("証拠 ID");
    await expect(body).not.toContainText("0f3a9c2");
    await expect(confirmed.nth(0)).toContainText("申請は 30 日前までに行います。");
    await expect(confirmed.nth(0).getByTestId("answer-citation-link")).toHaveText("根拠：文書 0.pdf p.3");
    await expect(confirmed.nth(1).getByTestId("answer-citation-link")).toHaveText("根拠：文書 0.pdf p.5");
    // 外した内容は本文の節に 1 回だけ。
    await expect(sections.nth(1)).toContainText(WITHHELD_QUOTE);
    await expect(turn.getByText(WITHHELD_QUOTE, { exact: false })).toHaveCount(1);

    // 根拠の行を押すと、その根拠を右のサイドパネル（RAG と同じ共通の部品。#2360）で開く。上部は根拠の要約
    // （番号・文書名・引用の印・Evidence・スコア）と、既定で閉じた「抜粋と頁・章節」（#2502）、その下に RAG と同じ
    // 原文のプレビュー（#2389）。
    const citationLink = confirmed.nth(0).getByTestId("answer-citation-link");
    await citationLink.click();
    const sheet = page.getByRole("dialog", { name: "回答の根拠" });
    const summary = sheet.getByTestId("evidence-source-summary");
    await expect(summary.getByTestId("evidence-file-name")).toHaveText("文書 0.pdf");
    await expect(summary).toContainText("回答で引用");
    await expect(summary.getByTestId("evidence-route-badges")).toContainText("Evidence");
    await expect(summary.getByTestId("citation-score-panel")).toBeVisible();
    // プレビューに場所を空けるため、抜粋と頁・章節のチップは既定で閉じる（RAG と同じ。#2502）。
    const excerptToggle = summary.getByTestId("evidence-excerpt-toggle");
    await expect(excerptToggle).toHaveText("抜粋と頁・章節");
    await expect(summary.getByTestId("evidence-excerpt-details")).not.toHaveAttribute("open", "");
    await expect(summary.getByTestId("citation-text")).toBeHidden();
    await expect(summary.getByTestId("evidence-chips")).toBeHidden();
    await excerptToggle.click();
    await expect(summary.getByTestId("citation-text")).toHaveText(`合成の本文（${CHUNK_A}）。`);
    await expect(summary.getByTestId("evidence-chips")).toContainText("p.3");
    await expect(sheet.getByTestId("evidence-source-preview").getByTestId("preview-viewer")).toBeVisible();
    // 図の根拠でも、要約の下に注記・「図を開く」・文書の詳細へのリンクの帯を出さない（#2462）。
    await expect(summary.getByRole("button", { name: /図を開く/ })).toHaveCount(0);
    await expect(summary.getByRole("link")).toHaveCount(0);
    await expect(sheet).not.toContainText("AI が図を読んだ説明です");
    // 右端に付き、375px は全画面のシート。
    await expect
      .poll(async () => {
        const box = await sheet.boundingBox();
        return box ? Math.round(box.x + box.width) : -1;
      })
      .toBe(viewport.width);
    if (viewport.width < 640) expect(Math.round((await sheet.boundingBox())!.x)).toBe(0);
    await page.screenshot({ path: testInfo.outputPath(`answer-evidence-sheet-${viewport.name}.png`) });
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
    await expect(citationLink).toBeFocused();

    // 根拠の塊: 見出しは件数と内訳、既定は閉じる。開くとカード（引用の印・スコア・場所のチップ）。
    const evidence = turn.getByTestId("answer-citations");
    await expect(evidence.locator("summary")).toHaveText("AI に渡した 3 件 ／ 回答で引用 2 件 ／ うち該当確認済み 1 件");
    await expect(evidence).not.toHaveAttribute("open", "");
    await evidence.locator("summary").click();
    const cards = evidence.getByTestId("evidence-card");
    await expect(cards).toHaveCount(3);
    await expect(cards.nth(0)).toContainText("回答で引用");
    await expect(cards.nth(2)).not.toContainText("回答で引用");
    await expect(cards.nth(2)).toContainText("Rerank 未実行");
    await expect(cards.nth(0).getByTestId("evidence-chips")).toContainText("p.3");
    // 図の根拠のカードも、操作は「根拠を表示」だけ（注記・「図を開く」は出さない。#2462）。
    await expect(cards.nth(0).getByRole("button", { name: /図を開く/ })).toHaveCount(0);
    await expect(cards.nth(0)).not.toContainText("AI が図を読んだ説明です");
    // カードの「根拠を表示」も、根拠の行と同じ右のサイドパネルを開く（RAG と同じ共通の部品のボタン。#2360・#2388）。
    const cardOpen = cards.nth(0).getByRole("button", { name: "文書 0.pdf の根拠を表示" });
    await cardOpen.click();
    await expect(sheet.getByTestId("evidence-source-summary").getByTestId("citation-text")).toHaveText(
      `合成の本文（${CHUNK_A}）。`
    );
    await sheet.getByRole("button", { name: "閉じる" }).click();
    await expect(sheet).toBeHidden();
    await expect(cardOpen).toBeFocused();
    // 一定の高さを超えたら中でスクロールする（チャット・実行の詳細で同じ）。
    await expect(evidence.locator("ul.bounded-scroll-area-lg")).toHaveCount(1);
    // 根拠の塊は回答の評価の前。
    const order = await turn.evaluate((element) => {
      const nodes = Array.from(element.querySelectorAll('[data-testid="answer-citations"], [data-testid^="chat-feedback"], [data-testid^="answer-feedback"]'));
      return nodes.map((node) => node.getAttribute("data-testid"));
    });
    expect(order[0]).toBe("answer-citations");
    // 1 つの塊だけ（以前の「出典」は出さない）。
    await expect(turn.getByText(/^出典（/)).toHaveCount(0);

    // 「資料との照合」は件数のバッジだけ（節の項目の数と一致）。開いて見るものが無いので ⌄ を出さない。
    const review = turn.getByTestId(`chat-review-${RUN_ID}`);
    await expect(review).toHaveText("資料との照合確かめられない点 1 件");
    await expect(turn.getByTestId(`chat-review-${RUN_ID}-summary`)).toHaveCount(0);
    await expect(review.getByTestId("answer-review-unverified")).toHaveCount(0);

    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`answer-evidence-${viewport.name}.png`), fullPage: true });
  });
}

// #2738: 根拠と矛盾した段落だけを本文から外し、確かめられない段落（未確認）と規則の検査の段落（要注意）は本文に
// 残して印を付ける。印を押すと理由を開き、「確認が必要な点」から本文の段落へ移る。回答全体の注意は本文の上。
// 「資料との照合」と実行の詳細は「未確認 N 件」「要注意 N 件」「矛盾で除外 N 件」のバッジ（RAG と同じ共有の部品）。
const MARKED_UNVERIFIED = "申請後の手続きは自動で通知されます。";
const MARKED_ANSWER = [
  "合成の質問 A の答えは次のとおりです。",
  "",
  "- 申請は 30 日前までに行います【証拠 ID: 0f3a9c2…:7】。",
  "- 承認は担当の部署が行います。",
  "",
  MARKED_UNVERIFIED,
].join("\n");

function seedMarkedRun(mockApi: MockApi) {
  seedRun(mockApi);
  const run = mockApi.state.runs.find((item) => item.id === RUN_ID) as {
    artifacts: { kind: string; content: Record<string, unknown> }[];
  };
  run.artifacts = run.artifacts.map((artifact) => {
    if (artifact.kind === "answer") return { ...artifact, content: { text: MARKED_ANSWER } };
    if (artifact.kind !== "answer_validation") return artifact;
    const content: Record<string, unknown> & AnswerValidationPassageContent = {
      ...artifact.content,
      notice: undefined,
      withheld: { removed: 1, unverified: 1, warnings: 1, all: false, listed: 3 },
      passage_marks: [
        // 可視内容がない過去の記録は、項目・件数・移動リンクを作らない。
        ...["\u200b", "**\u200b**", "[\u200b](https://example.invalid)"].map((text) => ({
          text, state: "unverified" as const, reasons: [{ code: "unsupported", label: "根拠で確かめられない", detail: "" }],
        })),
        {
          text: "- 承認は担当の部署が行います。",
          state: "warning",
          reasons: [{ code: "target_mismatch", label: "根拠は別の対象についての記載", detail: "合成の理由 B。" }],
        },
        {
          text: MARKED_UNVERIFIED,
          state: "unverified",
          reasons: [{ code: "unsupported", label: "根拠で確かめられない", detail: "合成の理由 C。" }],
        },
      ],
      removed_passages: [
        { text: WITHHELD_QUOTE, reasons: [{ code: "contradicted", label: "根拠と矛盾", detail: "合成の理由 D。" }] },
      ],
      answer_warnings: [
        { code: "guide_check", label: "業務ガイドの手順の確認で問題が見つかった", detail: "合成の手順の説明がありません。" },
      ],
    };
    return { ...artifact, content };
  });
}

async function expectMarkedAnswer(page: Page, body: ReturnType<Page["getByTestId"]>, viewportWidth: number) {
  // 回答全体の注意は本文の上の warning の Banner。
  const warnings = body.getByTestId("answer-passage-warnings");
  await expect(warnings).toContainText("業務ガイドの手順の確認で問題が見つかった");
  await expect(warnings).toContainText("合成の手順の説明がありません。");
  await expect(warnings.getByRole("status")).toBeVisible();
  // 印の段落は本文に残る（外さない）。印を付けた段落は「資料により確認できた事項」の見出しの下に入れない。
  const marked = body.getByTestId("answer-passage-mark");
  await expect(marked).toHaveCount(2);
  await expect(marked.nth(0)).toHaveAttribute("data-passage-state", "warning");
  await expect(marked.nth(0)).toContainText("承認は担当の部署が行います。");
  await expect(marked.nth(1)).toHaveAttribute("data-passage-state", "unverified");
  await expect(marked.nth(1)).toContainText(MARKED_UNVERIFIED);
  await expect(body.getByRole("heading", { name: "資料により確認できた事項" })).toHaveCount(0);
  await expect(body).not.toContainText("資料から確認できなかった事項");
  // 印は段落の末尾の小さな印（アイコンと短いラベル。色だけに頼らない）。押すと理由を開き、もう一度で閉じる。
  const toggle = marked.nth(1).getByTestId("answer-passage-mark-toggle");
  await expect(toggle).toHaveText("未確認の理由");
  await expect(toggle.locator("svg")).toHaveCount(1);
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const reasons = marked.nth(1).getByTestId("answer-passage-mark-reasons");
  await expect(reasons).toHaveText("根拠で確かめられない合成の理由 C。");
  // キーボードでも開閉できる。
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(reasons).toHaveCount(0);
  const warningToggle = marked.nth(0).getByTestId("answer-passage-mark-toggle");
  await warningToggle.focus();
  await page.keyboard.press("Space");
  await expect(marked.nth(0).getByTestId("answer-passage-mark-reasons")).toHaveText(
    "根拠は別の対象についての記載合成の理由 B。"
  );
  // 印は段落の幅に収まる（375px でも折り返して、はみ出さない）。
  const markedBox = await marked.nth(1).boundingBox();
  const toggleBox = await toggle.boundingBox();
  expect(toggleBox!.x + toggleBox!.width).toBeLessThanOrEqual(markedBox!.x + markedBox!.width + 1);
  expect(toggleBox!.x + toggleBox!.width).toBeLessThanOrEqual(viewportWidth);

  // 本文の後ろの節: 根拠と矛盾し除いた内容 → 確認が必要な点。
  const removed = body.getByTestId("answer-removed-passages");
  await expect(removed.locator("h4")).toHaveText("根拠と矛盾し除いた内容");
  await expect(removed.getByTestId("answer-removed-passage")).toHaveText(`「${WITHHELD_QUOTE}」根拠と矛盾：合成の理由 D。`);
  const points = body.getByTestId("answer-marked-passages");
  await expect(points.locator("h4")).toHaveText("確認が必要な点");
  await expect(points.getByTestId("answer-marked-passage")).toHaveText([
    "「承認は担当の部署が行います。」要注意：根拠は別の対象についての記載",
    `「${MARKED_UNVERIFIED}」未確認：根拠で確かめられない`,
  ]);
  await expect(points.getByTestId("answer-marked-passage-link")).toHaveCount(2);
  await expect(points).not.toContainText("「」");
  // 項目を押すと本文の段落へ移り、フォーカスする。
  await points.getByTestId("answer-marked-passage-link").nth(1).click();
  await expect(marked.nth(1)).toBeFocused();
  await expect(marked.nth(1)).toBeInViewport();
}

for (const viewport of VIEWPORTS) {
  test(`矛盾した段落だけを外し、確かめられない段落に印・理由・確認が必要な点・回答の注意を出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedMarkedRun(mockApi);
    await serveSourcePreviews(page);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);
    const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
    await expectMarkedAnswer(page, turn.getByTestId(`chat-answer-${RUN_ID}`), viewport.width);
    // 確かめた段落には根拠の行が付いたまま。
    await expect(turn.getByTestId(`chat-answer-${RUN_ID}`).getByTestId("answer-citation-link").first()).toHaveText(
      "根拠：文書 0.pdf p.3"
    );
    // 「資料との照合」は印の数のバッジ（0 件は出さない）。「一部を確かめられず省略」は出さない。
    const review = turn.getByTestId(`chat-review-${RUN_ID}`);
    const badges = review.locator("[data-status-variant]");
    await expect(badges).toHaveText(["未確認 1 件", "要注意 1 件", "矛盾で除外 1 件"]);
    await expect(badges.nth(0)).toHaveAttribute("data-status-variant", "warning");
    await expect(badges.nth(1)).toHaveAttribute("data-status-variant", "neutral");
    await expect(badges.nth(2)).toHaveAttribute("data-status-variant", "danger");
    for (const badge of await badges.all()) await expect(badge.locator("svg")).toHaveCount(1);
    await expect(turn).not.toContainText("省略");
    await expectNoHorizontalOverflow(page);
    await turn.getByTestId("answer-marked-passages").scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`answer-marks-chat-${viewport.name}.png`), fullPage: true });

    // 実行の詳細は回答の印を保ち、概要見出しのバッジは出さない。
    await page.goto(`/runs?id=${RUN_ID}`);
    const section = page.getByTestId("run-answer-section");
    await expectMarkedAnswer(page, section.getByTestId("run-answer-artifact-answer"), viewport.width);
    await expect(page.getByTestId("run-answer-passages")).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`answer-marks-run-${viewport.name}.png`), fullPage: true });

    // ダークのテーマでも同じ印・線・節を出す（色は共有のトークン）。
    await page.evaluate(() =>
      window.localStorage.setItem("production-ready-agent.ui", JSON.stringify({ state: { theme: "dark" }, version: 0 }))
    );
    await page.reload();
    await expect(page.getByTestId("run-answer-section").getByTestId("answer-passage-mark")).toHaveCount(2);
    await page.getByTestId("run-answer-section").getByTestId("answer-passage-mark-toggle").nth(1).click();
    await page.screenshot({ path: testInfo.outputPath(`answer-marks-run-dark-${viewport.name}.png`), fullPage: true });
  });
}

// #2387: 文書の表を解析した chunk の抜粋（HTML の表）は、タグの文字を出さず、全文の表示では表、畳んだ表示では「セル | セル」の行。
const TABLE_EXCERPT =
  "見出し A<br><table><tbody><tr><td>項目</td><td>記録内容</td><td>備考</td></tr>" +
  "<tr><td>区分 X</td><td>合成の記録の本文。長い文の折り返しを確かめるための合成の文です。</td><td>なし</td></tr>" +
  "<tr><td>区分 Y</td><td>A&amp;B<br>二行目</td><td>合成</td></tr></tbody></table>" +
  "<script>window.__xss = 1</script><img src=x onerror=\"window.__xss = 2\">";

for (const viewport of VIEWPORTS) {
  test(`根拠の抜粋の表の HTML をサイドパネルでは表、カードでは行の文字で出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedRun(mockApi, TABLE_EXCERPT);
    await serveSourcePreviews(page);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);
    const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
    const body = turn.getByTestId(`chat-answer-${RUN_ID}`);
    await body.getByTestId("answer-section").nth(0).getByTestId("answer-citation-link").first().click();
    const sheet = page.getByRole("dialog", { name: "回答の根拠" });
    const excerpt = sheet.getByTestId("evidence-source-summary").getByTestId("citation-text");
    // 原文のプレビュー（#2389）に場所を空けるため抜粋は既定で閉じる（RAG と同じ。#2502）。開くと表を出す。
    await expect(excerpt).toBeHidden();
    await sheet.getByTestId("evidence-excerpt-toggle").click();
    await expect(excerpt).toBeVisible();
    await expect(excerpt.locator("p").first()).toHaveText("見出し A");
    const table = excerpt.getByRole("table", { name: "抜粋の表" });
    await expect(table.getByRole("columnheader")).toHaveText(["項目", "記録内容", "備考"]);
    await expect(table.getByRole("row")).toHaveCount(3);
    await expect(table.getByRole("row").nth(2).getByRole("cell").nth(1)).toHaveText("A&B\n二行目");
    await expect(excerpt).not.toContainText("<");
    expect(await page.evaluate(() => (window as { __xss?: number }).__xss)).toBeUndefined();
    // 表はサイドパネルの幅に収め、収まらない分は表の中の横スクロールにする（パネルと画面は横にはみ出さない）。
    const sheetBox = (await sheet.boundingBox())!;
    const tableScroller = (await table.locator("xpath=..").boundingBox())!;
    expect(tableScroller.x + tableScroller.width).toBeLessThanOrEqual(sheetBox.x + sheetBox.width + 1);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`evidence-table-sheet-${viewport.name}.png`) });
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();

    // 根拠のカードの 3 行の抜粋は「セル | セル」の行の文字。
    const evidence = turn.getByTestId("answer-citations");
    await evidence.locator("summary").click();
    const card = evidence.getByTestId("evidence-card").nth(0);
    await expect(card.getByTestId("citation-text")).toHaveText(
      "見出し A\n項目 | 記録内容 | 備考\n区分 X | 合成の記録の本文。長い文の折り返しを確かめるための合成の文です。 | なし\n区分 Y | A&B 二行目 | 合成"
    );
    await expect(card.getByRole("table")).toHaveCount(0);
    await card.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`evidence-table-card-${viewport.name}.png`) });
  });
}

test("段落に根拠を結び付けられない回答は今までの Markdown のまま出す", async ({ page, mockApi }) => {
  seedRun(mockApi);
  const run = mockApi.state.runs.find((item) => item.id === RUN_ID) as {
    artifacts: { kind: string; content: Record<string, unknown> }[];
  };
  run.artifacts = run.artifacts.map((artifact) =>
    artifact.kind === "answer_evidence"
      ? { ...artifact, content: { ...artifact.content, markers: [], passages: [] } }
      : artifact
  );
  await page.goto("/chat");
  await openThread(page);
  const body = page.getByTestId(`chat-answer-${RUN_ID}`);
  await expect(body.getByTestId("answer-section")).toHaveCount(0);
  await expect(body).toContainText("申請は 30 日前までに行います");
  // 根拠の塊は出す。
  await expect(page.getByTestId("answer-citations")).toHaveCount(1);
});

test("実行の詳細の回答も同じ節で出し、根拠の塊は RAG と同じ別の Section「回答の根拠」に出す", async ({ page, mockApi }) => {
  seedRun(mockApi);
  await page.goto(`/runs?id=${RUN_ID}`);
  const section = page.getByTestId("run-answer-section");
  await expect(section.getByTestId("answer-section").locator("h4")).toHaveText([
    "資料により確認できた事項",
    "資料から確認できなかった事項",
  ]);
  // 根拠の塊は「回答」の Section の中ではなく、別の Section「回答の根拠」（RAG の実行の詳細と同じ。#2707）。
  await expect(section.getByTestId("answer-citations")).toHaveCount(0);
  const evidenceSection = page.getByTestId("run-evidence-section");
  await expect(evidenceSection.getByRole("heading", { name: "回答の根拠" })).toBeVisible();
  await expect(evidenceSection.getByTestId("answer-citations").locator("summary")).toHaveText(
    "AI に渡した 3 件 ／ 回答で引用 2 件 ／ うち該当確認済み 1 件"
  );
  const [answerBox, evidenceBox] = await Promise.all([section.boundingBox(), evidenceSection.boundingBox()]);
  expect(evidenceBox!.y).toBeGreaterThan(answerBox!.y + answerBox!.height - 1);
  // 回答の根拠・資料との照合は「成果物」に重ねない（内部の記録は監査ログで見る。#2403）。
  await expect(page.getByTestId("run-artifact-artifact-evidence")).toHaveCount(0);
  await expect(page.getByTestId("run-artifacts-section")).toHaveCount(0);
});

// #3422: 章節の階層（section_path）がとても長い根拠でも、根拠のカードの一覧が横にスクロールしない（チャットと実行の詳細）。
// 章節のチップはカードの幅で省略（…）し、全文は title で読める。
const LONG_SECTION_PATH = Array.from({ length: 12 }, (_, index) => `第 ${index + 1} 章 合成の章節の見出し`);

async function expectEvidenceListFits(citations: ReturnType<Page["getByTestId"]>) {
  await citations.locator("summary").click();
  const list = citations.locator(".bounded-scroll-area-lg");
  const chip = list.getByTestId("evidence-chips").locator("dd", { hasText: "第 12 章" });
  await expect(chip).toHaveAttribute("title", LONG_SECTION_PATH.join(" > "));
  expect(await list.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(0);
  const [chipBox, cardBox] = await Promise.all([chip.boundingBox(), list.getByTestId("evidence-card").first().boundingBox()]);
  expect(chipBox!.x + chipBox!.width).toBeLessThanOrEqual(cardBox!.x + cardBox!.width);
}

for (const viewport of VIEWPORTS) {
  test(`章節の階層が長い根拠でも根拠の一覧が横にスクロールしない (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedRun(mockApi);
    const run = mockApi.state.runs.find((item) => item.id === RUN_ID)!;
    const artifacts = run.artifacts as { kind: string; content: { items: Record<string, unknown>[] } }[];
    const evidence = artifacts.find((item) => item.kind === "answer_evidence")!.content;
    evidence.items[0].locator = { page_start: 3, page_end: 3, section_path: LONG_SECTION_PATH };

    await page.goto("/chat");
    await openThread(page);
    await expectEvidenceListFits(page.getByTestId(`chat-turn-${RUN_ID}`).getByTestId("answer-citations"));
    await expectNoHorizontalOverflow(page);

    await page.goto(`/runs?id=${RUN_ID}`);
    await expectEvidenceListFits(page.getByTestId("run-evidence-section").getByTestId("answer-citations"));
    await expectNoHorizontalOverflow(page);
  });
}

// #3252: 保存した根拠は見る人の今の権限で確かめ直し、見られない根拠は backend が中身を返さず数（`withheld_evidence_count`）
// だけを返す。画面は RAG と同じ共通の行「権限の無い根拠 N 件」を折りたたみに出し、「AI に渡した N 件」に含める。
for (const viewport of VIEWPORTS) {
  test(`今の権限で見られない根拠は数と理由だけを出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    seedRun(mockApi);
    const run = mockApi.state.runs.find((item) => item.id === RUN_ID)!;
    Object.assign(run, { withheld_evidence_count: 2 });

    await page.goto("/chat");
    await openThread(page);
    const citations = page.getByTestId(`chat-turn-${RUN_ID}`).getByTestId("answer-citations");
    await expect(citations.locator("summary")).toHaveText(
      "AI に渡した 5 件 ／ 回答で引用 2 件 ／ うち該当確認済み 1 件 ／ 権限の無い根拠 2 件"
    );
    await citations.locator("summary").click();
    const withheld = citations.getByTestId("evidence-withheld");
    await expect(withheld).toContainText("権限の無い根拠 2 件");
    await expect(withheld).toContainText("抜粋・資料名・原文は表示しません");
    await expect(citations.getByTestId("evidence-card")).toHaveCount(3);
    await expectNoHorizontalOverflow(page);

    // 実行の詳細の「回答の根拠」も同じ。見られる根拠が 1 件も無くても、数の行だけを出す。
    const artifacts = run.artifacts as { kind: string; content: Record<string, unknown> }[];
    const items = artifacts.find((item) => item.kind === "answer_evidence")!.content;
    Object.assign(items, { items: [], passed: 0, cited: 0, markers: [], passages: [] });
    Object.assign(run, { withheld_evidence_count: 3 });
    await page.goto(`/runs?id=${RUN_ID}`);
    const section = page.getByTestId("run-evidence-section");
    await expect(section.getByTestId("answer-citations").locator("summary")).toHaveText(
      "AI に渡した 3 件 ／ 権限の無い根拠 3 件"
    );
    await section.getByTestId("answer-citations").locator("summary").click();
    await expect(section.getByTestId("evidence-withheld")).toContainText("権限の無い根拠 3 件");
    await expect(section.getByTestId("evidence-card")).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
  });
}

// #2707: 最終の検証が時間切れ・失敗した回答（`answer_validation` が unvalidated）。backend は決定的な照合で根拠を結び付け
// （`binding: "text"`）、画面は「資料により確認できた事項」を作らず、RAG と同じ「参考情報」（説明の文・根拠の原文・
// 「根拠：文書 p.N」）を出す。根拠に結び付いた平文の出典の行と、中身が無くなった見出しは本文に出さない。
const UNVERIFIED_NOTICE = "この回答は資料との照合を終えられず、本質問の条件への該当は確認できていません。";
const REFERENCE_NOTE = "以下は資料の原文です。本質問の条件への該当は確認できていません。";
const UNVALIDATED_ANSWER = [
  "合成の質問 A の答えは次のとおりです。",
  "",
  "- 申請は 30 日前までに行います。",
  "- 承認は担当の部署が行います。",
  "",
  "**根拠**",
  "",
  "- 文書 0.pdf p.3",
  "- 文書 0.pdf p.5",
  "",
  UNVERIFIED_NOTICE,
].join("\n");

function seedUnvalidatedRun(mockApi: MockApi) {
  seedRun(mockApi);
  const run = mockApi.state.runs.find((item) => item.id === RUN_ID) as { artifacts: Record<string, unknown>[] };
  run.artifacts = [
    {
      id: "artifact-validation",
      kind: "answer_validation",
      name: "資料との照合",
      created_at: MOCK_NOW,
      content: { status: "unvalidated", reason: "mcp.timeout", message: "時間切れ", valid: null, result: null },
    },
    {
      id: "artifact-evidence",
      kind: "answer_evidence",
      name: "回答の根拠",
      created_at: MOCK_NOW,
      content: {
        items: [
          evidenceItem(CHUNK_A, { cited: true, answer_use: "reference" }),
          evidenceItem(CHUNK_B, {
            cited: true,
            answer_use: "reference",
            locator: { page_start: 5, page_end: 5, section_path: [] },
          }),
          evidenceItem(CHUNK_C, { file_name: "文書 C.pdf" }),
        ],
        passed: 3,
        cited: 2,
        markers: [],
        passages: [
          { text: "- 申請は 30 日前までに行います。", chunk_ids: [CHUNK_A] },
          { text: "- 文書 0.pdf p.3", chunk_ids: [CHUNK_A] },
          { text: "- 文書 0.pdf p.5", chunk_ids: [CHUNK_B] },
        ],
        binding: "text",
        references: [
          { quote: "合成の原文 A。申請は 30 日前までに行う。", chunk_ids: [CHUNK_A] },
          { quote: "合成の原文 B。承認は担当の部署が行う。", chunk_ids: [CHUNK_B] },
        ],
        citation_lines: ["- 文書 0.pdf p.3", "- 文書 0.pdf p.5"],
      },
    },
    { id: "artifact-answer", kind: "answer", name: "回答", created_at: MOCK_NOW, content: { text: UNVALIDATED_ANSWER } },
  ];
}

async function expectReferenceAnswer(body: ReturnType<Page["getByTestId"]>) {
  const sections = body.getByTestId("answer-section");
  // 「資料により確認できた事項」は作らず、「参考情報」の節だけ（モデルの見出し「根拠」は中身が無くなったので出さない）。
  await expect(sections.locator("h4")).toHaveText(["参考情報"]);
  await expect(body).not.toContainText("資料により確認できた事項");
  await expect(body.getByText("文書 0.pdf p.3", { exact: true })).toHaveCount(0);
  // 本文の段落には根拠の行を付けない（照合で確かめていない）。
  await expect(body.locator("p").first()).toHaveText("合成の質問 A の答えは次のとおりです。");
  await expect(body.getByText(UNVERIFIED_NOTICE)).toBeVisible();
  const reference = sections.last();
  await expect(reference.getByText(REFERENCE_NOTE)).toBeVisible();
  const items = reference.locator("li:not(.list-none)");
  await expect(items).toHaveCount(2);
  await expect(items.nth(0)).toContainText("「合成の原文 A。申請は 30 日前までに行う。」");
  await expect(items.nth(0).getByTestId("answer-citation-link")).toHaveText("根拠：文書 0.pdf p.3");
  await expect(items.nth(1).getByTestId("answer-citation-link")).toHaveText("根拠：文書 0.pdf p.5");
  // 根拠の行は「参考情報」の項目の下だけ。
  await expect(body.getByTestId("answer-citation-link")).toHaveCount(2);
}

for (const viewport of VIEWPORTS) {
  test(`照合が時間切れの回答は「参考情報」に根拠の原文とリンクを出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedUnvalidatedRun(mockApi);
    await serveSourcePreviews(page);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);
    const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
    const body = turn.getByTestId(`chat-answer-${RUN_ID}`);
    await expectReferenceAnswer(body);
    // 根拠の行は RAG と同じ右のサイドパネル（原文のプレビュー）を開く。
    const link = body.getByTestId("answer-citation-link").first();
    await link.click();
    const sheet = page.getByRole("dialog", { name: "回答の根拠" });
    await expect(sheet.getByTestId("evidence-source-summary").getByTestId("evidence-file-name")).toHaveText("文書 0.pdf");
    await expect(sheet.getByTestId("evidence-source-preview").getByTestId("preview-viewer")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
    // 根拠の塊は引用を「参考」に数え、「該当確認済み」は数えない（照合で確かめていない。RAG の参考の引用と同じ表示）。
    await expect(turn.getByTestId("answer-citations").locator("summary")).toHaveText("AI に渡した 3 件 ／ 回答で引用 2 件 ／ 参考 2 件");
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`answer-reference-chat-${viewport.name}.png`), fullPage: true });

    await page.goto(`/runs?id=${RUN_ID}`);
    const section = page.getByTestId("run-answer-section");
    await expectReferenceAnswer(section.getByTestId(`run-answer-artifact-answer`));
    await expect(page.getByTestId("run-evidence-section").getByTestId("answer-citations").locator("summary")).toHaveText(
      "AI に渡した 3 件 ／ 回答で引用 2 件 ／ 参考 2 件"
    );
    await expectNoHorizontalOverflow(page);
    await section.screenshot({ path: testInfo.outputPath(`answer-reference-run-${viewport.name}.png`) });
    await page
      .getByTestId("run-evidence-section")
      .screenshot({ path: testInfo.outputPath(`answer-reference-run-evidence-${viewport.name}.png`) });
  });
}

// #2700・#2707: 最終の検証が時間の上限までに一部の段落だけを確かめた回答（RAG の status=partial。成果物の
// `binding: "partial"`）。確かめ終わった段落には根拠の行、確かめ終わらなかった段落は本文に残して根拠の行を付けず、
// その根拠は「参考情報」に出し、確かめきれなかった点は「資料から確認できなかった事項」に出す（RAG と同じ順）。
const PENDING = "- 承認は担当の部署が行います。";
const PARTIAL_ANSWER = [
  "合成の質問 A の答えは次のとおりです。",
  "",
  "- 申請は 30 日前までに行います。",
  PENDING,
  "",
  "**資料で確認できず除いた内容**",
  "",
  "次の内容は、資料との照合が時間の上限までに終わらず確かめきれませんでした。根拠と矛盾する・根拠に無いという判定ではないため、回答の本文は載せています。",
  "- 確かめが終わっていない: 「承認は担当の部署が行います。」",
].join("\n");

function seedPartialRun(mockApi: MockApi) {
  seedRun(mockApi);
  const run = mockApi.state.runs.find((item) => item.id === RUN_ID) as { artifacts: Record<string, unknown>[] };
  run.artifacts = [
    {
      id: "artifact-validation",
      kind: "answer_validation",
      name: "資料との照合",
      created_at: MOCK_NOW,
      content: {
        status: "completed",
        reason: null,
        valid: false,
        result: {
          status: "partial",
          valid: false,
          timed_out_passage_ids: ["p2"],
          claims: [
            { answer_quote: "- 申請は 30 日前までに行います。", status: "supported", chunk_id: CHUNK_A, reason: "合成" },
            { answer_quote: PENDING, status: "unassessed", reason: "合成" },
          ],
          findings: [],
          missing_evidence: [],
          stale_evidence: [],
        },
        withheld: { claims: 0, findings: 0, all: false, unverified: 1 },
        notice: { items: 1, heading: "資料で確認できず除いた内容" },
      },
    },
    {
      id: "artifact-evidence",
      kind: "answer_evidence",
      name: "回答の根拠",
      created_at: MOCK_NOW,
      content: {
        items: [
          evidenceItem(CHUNK_A, { cited: true, answer_use: "confirmed" }),
          evidenceItem(CHUNK_B, {
            cited: true,
            answer_use: "reference",
            locator: { page_start: 5, page_end: 5, section_path: [] },
          }),
          evidenceItem(CHUNK_C, { file_name: "文書 C.pdf" }),
        ],
        passed: 3,
        cited: 2,
        markers: [],
        passages: [{ text: "- 申請は 30 日前までに行います。", chunk_ids: [CHUNK_A] }],
        binding: "partial",
        references: [{ quote: "合成の原文 B。承認は担当の部署が行う。", chunk_ids: [CHUNK_B] }],
        citation_lines: [],
      },
    },
    { id: "artifact-answer", kind: "answer", name: "回答", created_at: MOCK_NOW, content: { text: PARTIAL_ANSWER } },
  ];
}

async function expectPartialAnswer(body: ReturnType<Page["getByTestId"]>) {
  const sections = body.getByTestId("answer-section");
  await expect(sections.locator("h4")).toHaveText(["参考情報", "資料から確認できなかった事項"]);
  const items = sections.first().locator("li");
  // 確かめ終わった段落だけに根拠の行。確かめ終わらなかった段落は本文に残すが、根拠の行は付けない。
  await expect(items.nth(0)).toContainText("申請は 30 日前までに行います。");
  await expect(items.nth(0).getByTestId("answer-citation-link")).toHaveText("根拠：文書 0.pdf p.3");
  await expect(items.nth(1)).toContainText("承認は担当の部署が行います。");
  await expect(items.nth(1).getByTestId("answer-citation-link")).toHaveCount(0);
  const reference = sections.nth(1);
  await expect(reference.getByText(REFERENCE_NOTE)).toBeVisible();
  await expect(reference.locator("li:not(.list-none)")).toHaveCount(1);
  await expect(reference.getByTestId("answer-citation-link")).toHaveText("根拠：文書 0.pdf p.5");
  await expect(sections.nth(2)).toContainText("時間の上限までに終わらず確かめきれませんでした");
}

for (const viewport of VIEWPORTS) {
  test(`照合が一部の段落だけを確かめた回答は、確かめた段落に根拠の行、残りの根拠を「参考情報」に出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedPartialRun(mockApi);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);
    const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
    await expectPartialAnswer(turn.getByTestId(`chat-answer-${RUN_ID}`));
    await expect(turn.getByTestId("answer-citations").locator("summary")).toHaveText(
      "AI に渡した 3 件 ／ 回答で引用 2 件 ／ うち該当確認済み 1 件 ／ 参考 1 件"
    );
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`answer-partial-chat-${viewport.name}.png`), fullPage: true });

    await page.goto(`/runs?id=${RUN_ID}`);
    const section = page.getByTestId("run-answer-section");
    await expectPartialAnswer(section.getByTestId("run-answer-artifact-answer"));
    await expect(page.getByTestId("run-evidence-section").getByTestId("answer-citations")).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await section.screenshot({ path: testInfo.outputPath(`answer-partial-run-${viewport.name}.png`) });
  });
}

// #2361: 除いた内容が多い回答も、本文の節に全件を同じ書式で出し（「ほか N 件」で切らない）、件数のバッジと一致させる。
// 項目が多い節は先頭だけを出し、「すべて表示」で残りを開く。
for (const viewport of VIEWPORTS) {
  test(`除いた内容が多い回答は本文の節に全件を出し、件数のバッジと一致させて「すべて表示」で畳む (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedRun(mockApi);
    const quotes = Array.from({ length: 12 }, (_, index) => `合成の段落 ${index + 1} です。`);
    const run = mockApi.state.runs.find((item) => item.id === RUN_ID) as {
      artifacts: { kind: string; content: Record<string, unknown> }[];
    };
    const answer = [
      ...ANSWER.split("\n").slice(0, 8),
      ...quotes.map((quote) => `- 根拠で確かめられない: 「${quote}」`),
    ].join("\n");
    run.artifacts = run.artifacts.map((artifact) => {
      if (artifact.kind === "answer") return { ...artifact, content: { text: answer } };
      if (artifact.kind !== "answer_validation") return artifact;
      const result = artifact.content.result as Record<string, unknown>;
      return {
        ...artifact,
        content: {
          ...artifact.content,
          result: {
            ...result,
            claims: [
              (result.claims as unknown[])[0],
              ...quotes.map((quote) => ({ answer_quote: quote, status: "unsupported", reason: "合成の理由" })),
            ],
          },
          withheld: { claims: quotes.length, findings: 0, all: false },
          notice: { items: quotes.length, heading: "資料で確認できず除いた内容" },
        },
      };
    });
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);
    const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
    const section = turn.getByTestId(`chat-answer-${RUN_ID}`).getByTestId("answer-section").nth(1);
    await expect(section.locator("h4")).toHaveText("資料から確認できなかった事項");
    const items = section.locator("li:not(.list-none)");
    await expect(items).toHaveCount(10);
    const toggle = section.getByTestId("answer-section-toggle");
    await expect(toggle).toHaveText(`すべて表示（全 ${quotes.length} 件）`);
    await expect(turn.getByTestId(`chat-review-${RUN_ID}`).locator("[data-status-variant]")).toHaveText(
      `確かめられない点 ${quotes.length} 件`
    );
    await page.screenshot({ path: testInfo.outputPath(`answer-withheld-collapsed-${viewport.name}.png`), fullPage: true });
    await toggle.click();
    await expect(items).toHaveText(quotes.map((quote) => `根拠で確かめられない: 「${quote}」`));
    await expect(toggle).toHaveText("折りたたむ");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(section).not.toContainText("ほか");
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`answer-withheld-expanded-${viewport.name}.png`), fullPage: true });
  });
}

for (const viewport of VIEWPORTS) {
  test(`根拠のパネルに RAG と同じ原文のプレビュー（ページの画像・強調・ツールバー）を出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedRun(mockApi);
    const seen = await serveSourcePreviews(page);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);
    const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
    const citationLink = turn.getByTestId("answer-citation-link").first();
    await citationLink.click();
    const sheet = page.getByRole("dialog", { name: "回答の根拠" });
    const preview = sheet.getByTestId("evidence-source-preview");
    const viewer = preview.getByTestId("preview-viewer");
    await expect(viewer).toBeVisible();
    // RAG と同じツールバー（ページ送り・拡大縮小・幅に合わせる・全体表示・回転・強調の位置へ移動）。
    const toolbar = viewer.getByRole("toolbar", { name: "プレビューの操作" });
    for (const name of ["前のページ", "次のページ", "縮小", "拡大", "横幅に合わせる", "全体を表示", "左に回転", "右に回転", "強調した位置へ移動"]) {
      await expect(toolbar.getByRole("button", { name })).toBeVisible();
    }
    await expect(viewer.getByTestId("preview-page-status")).toHaveText("1 / 2");
    // 根拠の bbox を強調し、「p.N の M か所を強調しています」。
    await expect(viewer.getByTestId("preview-highlight-status")).toContainText("p.1 の 1 か所を強調しています");
    const overlay = viewer.getByTestId("bbox-content-overlay");
    await expect(overlay).toHaveCount(1);
    await expect(overlay).toHaveAttribute("data-highlight-tone", "primary");
    await expect(viewer.getByRole("img", { name: "合成の文書.pdf p.1" })).toBeVisible();
    // ページの画像は RAG の署名つきの URL で読み、Referer を送らない。Agent の API は Cookie の画面の API だけ。
    await expect.poll(() => seen.filter((item) => item.url.includes("/pages/1?dpi=")).length).toBeGreaterThan(0);
    expect(seen.every((item) => item.referer === undefined)).toBe(true);
    // ページを送ると、強調は別のページにある案内に変わる。
    await toolbar.getByRole("button", { name: "次のページ" }).click();
    await expect(viewer.getByTestId("preview-page-status")).toHaveText("2 / 2");
    await expect(viewer.getByTestId("preview-highlight-status")).toContainText("強調した位置は別のページにあります");
    await viewer.getByRole("button", { name: "強調のある 1 ページ目を表示" }).click();
    await expect(viewer.getByTestId("preview-page-status")).toHaveText("1 / 2");
    // 拡大縮小。
    await toolbar.getByRole("button", { name: "拡大" }).click();
    await expect(viewer.getByTestId("preview-zoom-status")).not.toHaveText("100%");
    await toolbar.getByRole("button", { name: "横幅に合わせる" }).click();
    await expect(viewer.getByTestId("preview-zoom-status")).toHaveText("100%");
    // 右端に付き、375px は全画面のシート。プレビューはパネルの幅に収まる（横にあふれない）。
    await expect
      .poll(async () => {
        const box = await sheet.boundingBox();
        return box ? Math.round(box.x + box.width) : -1;
      })
      .toBe(viewport.width);
    if (viewport.width < 640) expect(Math.round((await sheet.boundingBox())!.x)).toBe(0);
    const sheetBox = (await sheet.boundingBox())!;
    const viewerBox = (await viewer.boundingBox())!;
    expect(viewerBox.x + viewerBox.width).toBeLessThanOrEqual(sheetBox.x + sheetBox.width + 1);
    await page.screenshot({ path: testInfo.outputPath(`agent-source-preview-${viewport.name}.png`) });
    // 閉じると、開いた操作へフォーカスを戻す。
    await sheet.getByRole("button", { name: "閉じる" }).click();
    await expect(sheet).toBeHidden();
    await expect(citationLink).toBeFocused();
    await expectNoHorizontalOverflow(page);
  });
}

test("原文を出せない根拠は理由と再試行を出し、本文の資料は本文を出す", { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
  seedRun(mockApi);
  await serveSourcePreviews(page, '{"合成": "本文"}');
  mockApi.state.sourcePreviews[CHUNK_A] = {
    error: { status: 404, message: "資料が見つからないか、この資料を見る権限がありません。" },
  };
  mockApi.state.sourcePreviews[CHUNK_B] = {
    kind: "text",
    file_name: "合成の文書.json",
    pages: [],
    page_url_template: null,
    highlight_metadata: {},
  };
  await page.goto("/chat");
  await openThread(page);
  const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
  const links = turn.getByTestId("answer-citation-link");
  await links.nth(0).click();
  const sheet = page.getByRole("dialog", { name: "回答の根拠" });
  const preview = sheet.getByTestId("evidence-source-preview");
  // 理由を短く出し、要約はそのまま読める。
  await expect(preview).toContainText("資料が見つからないか、この資料を見る権限がありません。");
  await expect(preview.getByRole("button", { name: "再試行" })).toBeVisible();
  await expect(sheet.getByTestId("evidence-source-summary")).toContainText(`合成の本文（${CHUNK_A}）。`);
  await page.screenshot({ path: testInfo.outputPath(`agent-source-preview-error-${testInfo.project.name}.png`) });
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  // 本文の資料（JSON など）は本文をそのまま出す。
  await links.nth(1).click();
  await expect(preview.locator("pre")).toHaveText('{"合成": "本文"}');
});

// #2512: 抜粋の長い根拠で「抜粋と頁・章節」を開いても、原文のプレビューを画面の外へ押し出さない。抜粋は要約の中の
// 高さに上限のある領域でスクロールし（Tab で届き、下端のフェードで続きを示す）、テキストの原文は引用した箇所へ
// 移って強調する。内容はすべて合成のデータ。
const LONG_LINES = Array.from({ length: 80 }, (_, index) => `合成の項目 ${index + 1} の本文です。`);
const LONG_DOCUMENT = [
  "{",
  '  "items": [',
  ...LONG_LINES.map((line, index) => `    { "id": ${index + 1}, "text": "${line}" },`),
  "  ]",
  "}",
].join("\n");
const LONG_EXCERPT = LONG_LINES.slice(49, 79).join("\n");

for (const viewport of VIEWPORTS) {
  test(`抜粋の長い根拠を開いてもプレビューは画面に残り、テキストの原文は引用の箇所を強調する (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedRun(mockApi, LONG_EXCERPT);
    await serveSourcePreviews(page, LONG_DOCUMENT);
    mockApi.state.sourcePreviews[CHUNK_A] = {
      kind: "text",
      file_name: "合成の文書.json",
      pages: [],
      page_url_template: null,
      highlight_metadata: {},
    };
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);
    await page.getByTestId(`chat-turn-${RUN_ID}`).getByTestId("answer-citation-link").first().click();
    const sheet = page.getByRole("dialog", { name: "回答の根拠" });
    const summary = sheet.getByTestId("evidence-source-summary");
    const toggle = summary.getByTestId("evidence-excerpt-toggle");
    await toggle.click();
    const scroll = summary.getByRole("region", { name: "抜粋と頁・章節" });
    await expect(scroll.getByTestId("citation-text")).toContainText(LONG_LINES[49]);
    // 抜粋は上限の高さの中でスクロールし、続きがあることを下端のフェードで示す。
    await expect(scroll).toHaveAttribute("data-overflowing", "true");
    await expect(scroll.getByTestId("evidence-excerpt-fade")).toHaveCSS("opacity", "1");
    const scrollBox = (await scroll.boundingBox())!;
    expect(scrollBox.height).toBeLessThanOrEqual(viewport.height * 0.3);
    // プレビューは押し出されず、画面の中に十分な高さで残る。
    const preview = sheet.getByTestId("evidence-source-preview");
    const previewBox = (await preview.boundingBox())!;
    expect(previewBox.y + previewBox.height / 2).toBeLessThanOrEqual(viewport.height);
    expect(viewport.height - previewBox.y).toBeGreaterThanOrEqual(viewport.height * 0.35);
    // テキストの原文は、引用した箇所（抜粋の 50 行目から）へ移って強調する。
    const text = preview.getByRole("region", { name: "合成の文書.json のプレビュー" });
    const quote = text.getByTestId("preview-text-quote");
    await expect(quote).toContainText(LONG_LINES[49]);
    await expect(quote).toContainText(LONG_LINES[78].replace("。", ""));
    await expect(quote).not.toContainText(LONG_LINES[79]);
    await expect(preview.getByTestId("preview-text-highlight")).toContainText("引用した箇所を強調しています");
    await expect.poll(() => text.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
    const textBox = (await text.boundingBox())!;
    const quoteBox = (await quote.boundingBox())!;
    expect(quoteBox.y).toBeGreaterThanOrEqual(textBox.y - 1);
    expect(quoteBox.y).toBeLessThan(textBox.y + textBox.height);
    await page.screenshot({ path: testInfo.outputPath(`agent-long-excerpt-${viewport.name}.png`) });
    // 抜粋の領域は Tab で届き、キーボードで最後まで読める（読み終えるとフェードを消す）。
    await toggle.focus();
    await page.keyboard.press("Tab");
    await expect(scroll).toBeFocused();
    await page.keyboard.press("End");
    await expect.poll(() => scroll.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
    await expect(scroll.getByTestId("evidence-excerpt-fade")).toHaveCSS("opacity", "0");
    // 本文の外へ移したあとも、「強調した位置へ移動」で引用の箇所へ戻れる。
    await text.evaluate((element) => {
      element.scrollTop = 0;
    });
    await preview.getByRole("button", { name: "強調した位置へ移動" }).click();
    await expect.poll(() => text.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
    await expectNoHorizontalOverflow(page);
  });
}

test("前の RAG のツールの呼び出しの根拠も、原文のプレビューを開ける", async ({ page, mockApi }) => {
  // 成果物「検索の根拠」は rag_search の結果（根拠 A）だけを持ち、前の rag_retrieve_evidence の根拠 C は
  // step の出力にだけある。画面の根拠の一覧と同じ集合で確かめるので、C も開ける（#2427）。
  seedRun(mockApi);
  const run = mockApi.state.runs.find((item) => item.id === RUN_ID)!;
  const step = (tool: string, chunkId: string) => ({
    id: `step-${tool}`,
    run_id: RUN_ID,
    kind: "tool",
    status: "completed",
    tool_call: { name: `rag__${tool}`, arguments: {} },
    tool_result: { name: `rag__${tool}`, success: true, output: { evidence: [evidenceItem(chunkId)] } },
  });
  run.steps = [step("rag_retrieve_evidence", CHUNK_C), step("rag_search", CHUNK_A)];
  (run.artifacts as Record<string, unknown>[]).push({
    id: "artifact-rag-search",
    kind: "rag_evidence",
    name: "rag__rag_search:step-rag_search",
    created_at: MOCK_NOW,
    content: { evidence: [evidenceItem(CHUNK_A)] },
  });
  await serveSourcePreviews(page);
  await page.goto("/chat");
  await openThread(page);
  const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
  await turn.getByTestId("answer-citations").locator("summary").click();
  await turn.getByRole("button", { name: "文書 C.pdf の根拠を表示" }).click();
  const sheet = page.getByRole("dialog", { name: "回答の根拠" });
  const preview = sheet.getByTestId("evidence-source-preview");
  await expect(preview.getByTestId("preview-viewer")).toBeVisible();
  await expect(preview).not.toContainText("この実行の根拠に、その資料はありません。");
  const request = mockApi.lastRequest("GET", `/api/runs/${RUN_ID}/source-preview`);
  expect(request?.searchParams.get("chunk_id")).toBe(CHUNK_C);
});

// #2405: 回答の注意書き（設定は製品ごと。#2449）。backend が回答の成果物の `notice` に付け、画面は回答の本文の後・
// 根拠の塊の前に出す（RAG のチャットと同じ共通の並び「注意書き → 根拠 → 評価」。#2707）。
const NOTICE = "この回答は AI が生成したものです。\n連絡先: https://example.com/help";

function withNotice(mockApi: MockApi) {
  const run = mockApi.state.runs.find((item) => item.id === RUN_ID) as {
    artifacts: { kind: string; content: Record<string, unknown> }[];
  };
  run.artifacts = run.artifacts.map((artifact) =>
    artifact.kind === "answer" ? { ...artifact, content: { ...artifact.content, notice: NOTICE } } : artifact
  );
}

for (const viewport of VIEWPORTS) {
  test(`回答のカードは 本文 → 注意書き → 根拠の塊 → 評価 の順に出す (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }) => {
    seedRun(mockApi);
    withNotice(mockApi);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page);
    const turn = page.getByTestId(`chat-turn-${RUN_ID}`);
    const notice = turn.getByRole("complementary", { name: "回答の注意書き" });
    await expect(notice).toBeVisible();
    await expect(notice.getByRole("link", { name: "https://example.com/help" })).toHaveAttribute(
      "href",
      "https://example.com/help"
    );
    // 本文 → 注意書き → 根拠の塊 → 評価 の順（注意書きは本文には混ぜない）。
    const body = turn.getByTestId(`chat-answer-${RUN_ID}`);
    const citations = turn.getByTestId("answer-citations");
    const feedback = turn.getByRole("group", { name: "この回答は役に立ちましたか？" });
    await expect(feedback).toBeVisible();
    const [bodyBox, noticeBox, citationsBox, feedbackBox] = await Promise.all([
      body.boundingBox(),
      notice.boundingBox(),
      citations.boundingBox(),
      feedback.boundingBox(),
    ]);
    expect(noticeBox!.y).toBeGreaterThan(bodyBox!.y + bodyBox!.height - 1);
    expect(citationsBox!.y).toBeGreaterThan(noticeBox!.y + noticeBox!.height - 1);
    expect(feedbackBox!.y).toBeGreaterThan(citationsBox!.y + citationsBox!.height - 1);
    await expect(body).not.toContainText("AI が生成");
    await expectNoHorizontalOverflow(page);
  });
}

test("実行の詳細の回答にも注意書きを出し、付けていない回答には出さない", async ({ page, mockApi }) => {
  seedRun(mockApi);
  await page.goto(`/runs?id=${RUN_ID}`);
  const section = page.getByTestId("run-answer-section");
  await expect(section.getByTestId("answer-section").first()).toBeVisible();
  await expect(section.getByRole("complementary", { name: "回答の注意書き" })).toHaveCount(0);

  withNotice(mockApi);
  await page.reload();
  await expect(page.getByTestId("run-answer-section").getByRole("complementary", { name: "回答の注意書き" })).toContainText(
    "この回答は AI が生成したものです。"
  );
});

// #2554: 会話の添付を読むツールの結果も根拠の塊に出す。添付は RAG の文書ではないので、「添付ファイル」の印を付け、
// 原文を開く「根拠を表示」は出さない（本文はカードに出ている。RAG のチャットの添付の根拠と同じ）。
const ATTACHMENT_RUN_ID = "run-evidence-attachment";
const ATTACHMENT_GOAL = "合成の添付の質問 B は？";

function seedAttachmentRun(mockApi: MockApi) {
  mockApi.state.runs.push({
    id: ATTACHMENT_RUN_ID,
    goal: ATTACHMENT_GOAL,
    agent_id: "default",
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: `thread_${"f".repeat(32)}`,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
    artifacts: [
      {
        id: "artifact-attachment-evidence",
        kind: "answer_evidence",
        name: "回答の根拠",
        created_at: MOCK_NOW,
        content: {
          items: [
            {
              document_id: "attachment:att-1",
              chunk_id: "attachment:att-1:s3",
              file_name: "manual-a.pdf",
              excerpt: "合成の本文。費用は 1 回あたり 100 です。",
              truncated: false,
              locator: { page_start: 3, page_end: 3 },
              content_kind: "text",
              evidence_type: "chat_attachment",
              chat_attachment_id: "att-1",
              cited: true,
              answer_use: null,
            },
            {
              document_id: "attachment:att-2",
              chunk_id: "attachment:att-2:table-b:1-2",
              file_name: "table-b.csv",
              excerpt: "code: A / amount: 100\ncode: B / amount: 200",
              truncated: false,
              locator: { sheet_name: "table-b", row_start: 1, row_end: 2 },
              content_kind: "sheet",
              evidence_type: "chat_attachment",
              chat_attachment_id: "att-2",
              cited: false,
              answer_use: null,
            },
          ],
          passed: 2,
          cited: 1,
          markers: [],
          passages: [],
        },
      },
      {
        id: "artifact-attachment-answer",
        kind: "answer",
        name: "回答",
        created_at: MOCK_NOW,
        content: { text: "費用は 1 回あたり 100 です（出典：manual-a.pdf p.3）。" },
      },
    ],
  });
}

for (const viewport of VIEWPORTS) {
  test(`添付の根拠は「添付ファイル」の印で根拠の塊に出し、原文は開かない (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedAttachmentRun(mockApi);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await page.getByTestId("chat-history-toggle").click();
    if (viewport.width >= 1024) {
      await page.getByTestId("chat-history").getByRole("button", { name: new RegExp(`^${ATTACHMENT_GOAL}`) }).click();
    } else {
      await page.getByRole("dialog", { name: "会話の履歴" }).getByText(ATTACHMENT_GOAL).click();
    }
    const turn = page.getByTestId(`chat-turn-${ATTACHMENT_RUN_ID}`);
    const evidence = turn.getByTestId("answer-citations");
    await expect(evidence.locator("summary")).toHaveText("AI に渡した 2 件 ／ 回答で引用 1 件");
    await evidence.locator("summary").click();
    const cards = evidence.getByTestId("evidence-card");
    await expect(cards).toHaveCount(2);
    await expect(cards.nth(0)).toContainText("manual-a.pdf");
    await expect(cards.nth(0)).toContainText("添付ファイル");
    await expect(cards.nth(0)).toContainText("回答で引用");
    await expect(cards.nth(0).getByTestId("evidence-chips")).toContainText("p.3");
    await expect(cards.nth(1)).toContainText("添付ファイル");
    await expect(cards.nth(1).getByTestId("evidence-chips")).toContainText("table-b · 1–2 行");
    await expect(evidence.getByRole("button", { name: /の根拠を表示$/ })).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`answer-evidence-attachment-${viewport.name}.png`), fullPage: true });
  });
}
