import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #109: 根拠の付いた回答（段落を節に組み立てる回答）の末尾の「---\n**根拠**\n- 文書 …」の区切り線と箇条書きを、
// 段落の文（行の中の Markdown だけ）にせず、本文と同じ Markdown の規則で描く。段落の根拠の行は今までどおり。
// 内容はすべて合成のデータ。

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-375", width: 375, height: 812 },
];
const THREAD_ID = `thread_${"9".repeat(32)}`;
const GOAL = "合成の規程で、出張の宿泊費の上限はいくらですか？";
const RUN_ID = "run-answer-markdown-rule";
const CHUNK_A = "doc-r:cs_r:0";
const CHUNK_B = "doc-r:cs_r:3";
const LEAD = "合成の規程「勤務・出張・経費規程」の **第3条（出張旅費）** に、宿泊費の上限（1泊）が次のとおり定められています。";
const SOURCE_LINE = "合成の規程.pdf ／「勤務・出張・経費規程」第3条（出張旅費）、p.1（宿泊費の上限の表）";
const ANSWER = [
  "",
  "",
  LEAD,
  "",
  "| 区分 | 宿泊費の上限（1泊） |",
  "|---|---|",
  "| 一般社員 | **12,000円** |",
  "| 課長以上 | **15,000円** |",
  "",
  "（同条には日当として 一般社員 2,000円／課長以上 3,000円 も併記されています。）",
  "",
  "---",
  "**根拠**",
  `- ${SOURCE_LINE}`,
].join("\n");

function evidenceItem(chunkId: string) {
  return {
    document_id: "doc-r",
    chunk_id: chunkId,
    file_name: "合成の規程.pdf",
    excerpt: `合成の本文（${chunkId}）。`,
    truncated: false,
    locator: { page_start: 1, page_end: 1, section_path: ["第3条"] },
    role: "retrieved_anchor",
    score: 0.8,
    rerank_score: 0.6,
    section_title: "第3条",
    cited: true,
    answer_use: "confirmed",
  };
}

function seedRun(mockApi: MockApi) {
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
          valid: true,
          result: { status: "completed", valid: true, claims: [], findings: [], missing_evidence: [], stale_evidence: [] },
          passage_marks: [],
          removed_passages: [],
          answer_warnings: [],
          withheld: { removed: 0, unverified: 0, warnings: 0, all: false, listed: 0 },
        },
      },
      {
        id: "artifact-evidence",
        kind: "answer_evidence",
        name: "回答の根拠",
        created_at: MOCK_NOW,
        content: {
          items: [evidenceItem(CHUNK_A), evidenceItem(CHUNK_B)],
          passed: 2,
          cited: 2,
          markers: [],
          passages: [
            { text: LEAD, chunk_ids: [CHUNK_A] },
            { text: "| 一般社員 | **12,000円** |", chunk_ids: [CHUNK_B] },
          ],
          binding: "validation",
          references: [],
          citation_lines: [],
        },
      },
      { id: "artifact-answer", kind: "answer", name: "回答", created_at: MOCK_NOW, content: { text: ANSWER } },
    ],
  });
}

async function openThread(page: Page, width: number) {
  await page.getByTestId("chat-history-toggle").click();
  if (width >= 1024) {
    await page.getByTestId("chat-history").getByRole("button", { name: new RegExp(`^${GOAL}`) }).click();
    return;
  }
  await page.getByRole("dialog", { name: "会話の履歴" }).getByText(GOAL).click();
}

for (const viewport of VIEWPORTS) {
  test(`根拠の付いた回答の末尾の区切り線と箇条書きを Markdown で描く (${viewport.name})`, { tag: "@responsive" }, async ({ page, mockApi }, testInfo) => {
    seedRun(mockApi);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/chat");
    await openThread(page, viewport.width);
    const body = page.getByTestId(`chat-turn-${RUN_ID}`).getByTestId(`chat-answer-${RUN_ID}`);

    // 段落の根拠の行は今までどおり。
    await expect(body.getByTestId("answer-citation-link").first()).toHaveText("根拠：合成の規程.pdf p.1");
    // 区切り線は線（hr）、「根拠」は太字、出典は箇条書きの項目。記号のまま出さない。
    await expect(body.locator("hr")).toHaveCount(1);
    await expect(body.locator("strong", { hasText: /^根拠$/ })).toBeVisible();
    const item = body.locator("ul > li", { hasText: SOURCE_LINE });
    await expect(item).toHaveCount(1);
    await expect(item).toHaveText(SOURCE_LINE);
    await expect(body).not.toContainText("---");
    await expect(body).not.toContainText(`- ${SOURCE_LINE}`);
    await expect(body).not.toContainText("**");
    // 区切り線は「根拠」の太字の上、項目はその下。
    const [ruleBox, strongBox, itemBox] = await Promise.all([
      body.locator("hr").boundingBox(),
      body.locator("strong", { hasText: /^根拠$/ }).boundingBox(),
      item.boundingBox(),
    ]);
    expect(strongBox!.y).toBeGreaterThan(ruleBox!.y);
    expect(itemBox!.y).toBeGreaterThan(strongBox!.y);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await body.locator("hr").scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`answer-markdown-rule-${viewport.name}.png`), fullPage: true });
  });
}
