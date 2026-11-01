import { readFileSync } from "node:fs";
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures/test";
import {
  enableSearchAnswer,
  expectNoPageOverflow,
  mockDatabaseReady,
  mockLocalAuth,
  selectSearchAnswerProfile,
} from "./_helpers";

// #2710: 本文の根拠の行は、行の主張を支える箇所（同じ親・同じ頁の見出し H ではなく図の説明 F）を開き、根拠の一覧から
// 同じ箇所を開いたときと同じ要約を出す。位置を確定できない行は「位置は確定していません」と候補の数を出す。まとまり（親）
// だけを参照した箇所は「回答で引用」ではなく「まとまりで参照」に数える。内容はすべて合成のデータ。

const PROFILE = {
  id: "profile-a",
  name: "プロファイル A",
  description: null,
  status: "ACTIVE",
  knowledge_base_count: 1,
  created_at: "2026-10-20T00:00:00Z",
  updated_at: "2026-10-20T00:00:00Z",
  archived_at: null,
};

const envelope = (data: unknown) => ({ data, error_messages: [], warning_messages: [] });

const HEADING = "合成の見出し A。注意: 合成の注意書きです。";
const OPERATION = "合成の操作の説明です。";
const FIGURE = "図の説明: 左の枠に値 B を入れると、右の枠に結果 C が出ます。";

function citation(chunkId: string, text: string, metadata: Record<string, unknown>) {
  return {
    document_id: "doc-a",
    chunk_id: chunkId,
    text,
    score: 0.5,
    rerank_score: 0.7,
    file_name: "文書 A.pdf",
    category_name: null,
    metadata: { context_role: "evidence", content_kind: "text", page_start: 3, page_end: 3, ...metadata },
  };
}

// 回答の記録の並び（H が先）。1 行目は F に確定、2 行目は H と O の候補だけ（位置が決まらない）。
const RESOLVED = [
  citation("doc-a:c-h", HEADING, {
    evidence_model_used: false,
    evidence_parent_used: true,
    answer_citation_candidate_lines: [2],
  }),
  citation("doc-a:c-o", OPERATION, { evidence_model_used: false, answer_citation_candidate_lines: [2] }),
  citation("doc-a:c-f", FIGURE, {
    evidence_model_used: true,
    evidence_answer_use: "confirmed",
    answer_citation_lines: [1],
    answer_citation_provenance: "child",
  }),
];

// 以前の回答の記録（確定の印の無い番号）。行の番号は同じ頁の H に付いている。
const LEGACY = [
  citation("doc-a:c-h", HEADING, { evidence_model_used: true, answer_citation_lines: [1] }),
  citation("doc-a:c-o", OPERATION, { evidence_model_used: true }),
  citation("doc-a:c-f", FIGURE, { evidence_model_used: true }),
];

const ANSWER = [
  "結果 C は右の枠に出ます。",
  "",
  "資料により確認できた事項",
  "",
  "・左の枠に値 B を入れると、右の枠に結果 C が出ます。",
  "根拠：文書 A.pdf p.3",
  "・合成の操作を行います。",
  "根拠：文書 A.pdf p.3",
].join("\n");

async function ask(
  page: Page,
  citations: unknown[],
  format?: Record<string, string>,
  { text = ANSWER, passages, expectedHeading }: { text?: string; passages?: Record<string, unknown>; expectedHeading?: string } = {}
) {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/search-answer-profiles**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/approved-faq/suggest")) {
      return route.fulfill({ json: envelope({ suggestions: [] }) });
    }
    return route.fulfill({ json: envelope({ items: [PROFILE], total: 1, limit: 50, offset: 0, has_next: false }) });
  });
  await page.route("**/api/search/stream", (route) =>
    route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: [
        `event: metadata\ndata: ${JSON.stringify({
          trace_id: "trace-provenance",
          elapsed_ms: 10,
          diagnostics: {
            search_answer_profile_applied: PROFILE.id,
            retrieval_strategy: "hybrid",
            answer: { confidence: "high", execution_steps: [], evidence_tree: [], answer_format: format, ...passages },
          },
        })}\n\n`,
        `event: delta\ndata: ${JSON.stringify({ text: format && text === ANSWER ? ANSWER.replaceAll("資料により確認できた事項", format.rules_section).replaceAll("根拠：", format.citation_prefix).replaceAll("・", format.item_bullet).replaceAll("p.3", "頁[3]") : text })}\n\n`,
        `event: citations\ndata: ${JSON.stringify(citations)}\n\n`,
        `event: done\ndata: ${JSON.stringify({ trace_id: "trace-provenance" })}\n\n`,
      ].join(""),
    })
  );
  await page.goto("/search");
  await selectSearchAnswerProfile(page, /プロファイル A/);
  await page.keyboard.press("Escape");
  await enableSearchAnswer(page);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("合成の質問");
  await page.getByRole("button", { name: "検索", exact: true }).click();
  const answer = page.getByTestId("answer-text");
  await expect(answer.getByRole("heading", { name: expectedHeading ?? format?.rules_section ?? "資料により確認できた事項" }).first()).toBeVisible();
  return answer;
}

/** 開いた根拠のサイドパネルの要約（番号・文書名・抜粋）と案内。 */
async function openedSheet(page: Page) {
  const sheet = page.getByRole("dialog", { name: "回答の根拠" });
  await expect(sheet).toBeVisible();
  const summary = sheet.getByTestId("evidence-source-summary");
  await summary.getByTestId("evidence-excerpt-toggle").click();
  return {
    sheet,
    excerpt: await summary.getByTestId("citation-text").innerText(),
    number: await summary.getByTestId("citation-main").locator("span").first().innerText(),
  };
}

async function closeSheet(page: Page) {
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "回答の根拠" })).toBeHidden();
}

test.describe("本文の根拠の行と根拠の一覧の対応（issue 2710）", { tag: "@responsive" }, () => {
  test("根拠の行は主張を支える図の説明を開き、根拠の一覧から開いた同じ箇所と同じ要約を出す", async ({ page }, testInfo) => {
    const answer = await ask(page, RESOLVED);
    const lines = answer.getByRole("button", { name: "根拠：文書 A.pdf p.3 の原文を開く" });
    await expect(lines).toHaveCount(2);

    await lines.nth(0).click();
    const fromLine = await openedSheet(page);
    // 引用の並びの先の見出し（H）ではなく、図の説明（F。3 番目）を開き、位置の案内は出さない。
    expect(fromLine.excerpt).toBe(FIGURE);
    expect(fromLine.number).toBe("3");
    await expect(fromLine.sheet.getByTestId("evidence-source-notice")).toHaveCount(0);
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`citation-line-exact-${testInfo.project.name}.png`) });
    await closeSheet(page);

    // 根拠の一覧から同じ箇所（3 番目のカード）を開くと、本文の行から開いたときと同じ要約。
    const summary = page.locator("summary").filter({ hasText: "AI に渡した 3 件" });
    await expect(summary).toHaveText(
      "AI に渡した 3 件 ／ 回答で引用 1 件 ／ うち該当確認済み 1 件 ／ まとまりで参照 1 件"
    );
    const cards = page.getByTestId("evidence-card");
    await expect(cards).toHaveCount(3);
    // 根拠の一覧は既定で閉じている（共有の AnswerEvidence）。
    if (!(await cards.first().isVisible())) await summary.click();
    await expect(cards.nth(2)).toBeVisible();
    await expect(cards.nth(0).getByTestId("citation-main")).toContainText("まとまりで参照");
    await expect(cards.nth(0).getByTestId("citation-main")).not.toContainText("回答で引用");
    await expect(cards.nth(2).getByTestId("citation-main")).toContainText("回答で引用");
    await cards.nth(2).getByRole("button", { name: "文書 A.pdf の根拠を表示" }).click();
    const fromList = await openedSheet(page);
    expect([fromList.excerpt, fromList.number]).toEqual([fromLine.excerpt, fromLine.number]);
    await expect(fromList.sheet.getByTestId("evidence-source-notice")).toHaveCount(0);
    await closeSheet(page);
  });

  test("位置を確定できない根拠の行は、候補を開いて位置が未確定であることと候補の数を出す", async ({ page }, testInfo) => {
    const answer = await ask(page, RESOLVED);
    await answer.getByRole("button", { name: "根拠：文書 A.pdf p.3 の原文を開く" }).nth(1).click();
    const opened = await openedSheet(page);
    const notice = opened.sheet.getByTestId("evidence-source-notice");
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("この根拠の行の位置は確定していません");
    await expect(notice).toContainText("候補 2 件");
    // 案内は要約の上。
    const [noticeBox, summaryBox] = await Promise.all([
      notice.boundingBox(),
      opened.sheet.getByTestId("evidence-source-summary").boundingBox(),
    ]);
    expect(noticeBox!.y).toBeLessThan(summaryBox!.y);
    await expectNoPageOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`citation-line-unresolved-${testInfo.project.name}.png`) });
  });

  test("以前の回答の記録の番号は記録のまま開き、同じ頁に別の箇所があれば位置が未確定と出す", async ({ page }) => {
    const answer = await ask(page, LEGACY);
    await answer.getByRole("button", { name: "根拠：文書 A.pdf p.3 の原文を開く" }).nth(0).click();
    const opened = await openedSheet(page);
    // 保存した対応（H）は書き換えない。
    expect(opened.excerpt).toBe(HEADING);
    const notice = opened.sheet.getByTestId("evidence-source-notice");
    await expect(notice).toContainText("この根拠の行の位置は確定していません");
    await expect(notice).toContainText("この回答の記録は");
    await expect(notice).toContainText("文書名と頁が合う箇所 3 件");
  });
});


test("保存した表示形式の回答でも原文を開ける", { tag: "@responsive" }, async ({ page }) => {
  const answer = await ask(page, RESOLVED, { rules_section: "確認結果[例]", citation_prefix: "出典[例]：", item_bullet: "※", page_location: "頁[{page}]" });
  const lines = answer.getByRole("button", { name: "出典[例]：文書 A.pdf 頁[3] の原文を開く" });
  await expect(lines).toHaveCount(2);
  await lines.first().click();
  const { excerpt } = await openedSheet(page);
  expect(excerpt).toContain(FIGURE);
  await closeSheet(page);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("custom-answer-format.png"), fullPage: true });
});

// issue 3394: 印の付いた説明は「資料により確認できた事項」に入れず、別の節に置く（見出しと印の意味を合わせる）。
test("印の付いた説明は確認できた事項と別の節に出す", { tag: "@responsive" }, async ({ page }) => {
  const note = "次の説明は根拠で確かめきれていないか、注意が必要です。理由は「確認が必要な点」に示します。";
  const marked = "・合成の操作を行います。";
  const text = [
    "結果 C は右の枠に出ます。",
    "",
    "資料により確認できた事項",
    "",
    "・左の枠に値 B を入れると、右の枠に結果 C が出ます。",
    "根拠：文書 A.pdf p.3",
    "",
    "根拠で確かめきれていない事項",
    "",
    note,
    marked,
    "根拠：文書 A.pdf p.3",
  ].join("\n");
  const format = {
    rules_section: "資料により確認できた事項",
    marked_rules_section: "根拠で確かめきれていない事項",
    marked_rules_section_note: note,
    citation_prefix: "根拠：",
    item_bullet: "・",
    page_location: "p.{page}",
  };
  const passages = {
    passage_marks: [
      { text: marked, state: "unverified", reasons: [{ code: "unassessed", label: "確かめが終わっていない", detail: "" }] },
    ],
    removed_passages: [],
    answer_warnings: [],
  };
  const answer = await ask(page, RESOLVED, format, { text, passages });
  await expect(answer.getByRole("heading", { name: "根拠で確かめきれていない事項" })).toBeVisible();
  const confirmed = answer.getByRole("heading", { name: "資料により確認できた事項" });
  await expect(confirmed).toHaveCount(1);
  // 印の付いた説明は、確認できた事項の節ではなく、確かめきれていない事項の節の中にある。
  const [confirmedBox, markedHeadingBox, markedBox] = await Promise.all([
    confirmed.boundingBox(),
    answer.getByRole("heading", { name: "根拠で確かめきれていない事項" }).boundingBox(),
    answer.getByText("合成の操作を行います。", { exact: true }).first().boundingBox(),
  ]);
  expect(confirmedBox!.y).toBeLessThan(markedHeadingBox!.y);
  expect(markedHeadingBox!.y).toBeLessThan(markedBox!.y);
  await expect(answer.getByText(note)).toBeVisible();
  await expectNoPageOverflow(page);
  await answer.screenshot({ path: test.info().outputPath(`marked-rules-section-${test.info().project.name}.png`) });
});


// 引用の抽出失敗と意味の判定を混ぜず、backend の label/detail をそのまま読む。
test("引用の原因別の表示を原文不一致と意味の未確認に分ける", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const contract = JSON.parse(readFileSync(`${__dirname}/../../../platform/contracts/answer-passages/passage-marks.json`, "utf8")) as { reason_codes: Record<string, { label: string }> };
  const cases = [
    ["citation_id_invalid", "参照された ID は今回の根拠集合にありません。"],
    ["citation_id_ambiguous", "同じ ID に複数の根拠が対応しています。"],
    ["citation_range_missing", "引用対象の行が指定されていません。"],
    ["citation_range_invalid", "開始行と終了行の順序が不正です。"],
    ["citation_range_out_of_bounds", "指定行は送信した根拠の行数を超えています。"],
    ["citation_extraction_failed", "指定範囲のテキスト抽出が完了しませんでした。"],
    ["citation_heading_only", "取得した範囲には見出しだけが含まれています。"],
    ["citation_version_mismatch", "引用元の内容と今回の確認対象の版が一致していません。"],
    ["unsupported", "原文は一致していますが、この操作を支持していません。"],
    ["unassessed", "意味の照合は実行していません。"],
    ["unassessed", "意味の照合に失敗し、支持は未確認です。"],
  ] as const;
  const marks = cases.map(([code, detail], index) => ({ text: `合成の説明 ${index + 1}。`, state: "unverified", reasons: [{ code, label: contract.reason_codes[code].label, detail }] }));
  expect(new Set(marks.slice(0, 8).map(mark => mark.reasons[0].label)).size).toBe(8);
  const answer = await ask(page, [], undefined, {
    text: ["資料により確認できた事項", "・確認済みの合成説明。", "根拠で確かめきれていない事項", ...marks.map(mark => "・" + mark.text)].join("\n\n"),
    passages: { passage_marks: marks, removed_passages: [], answer_warnings: [] },
  });
  const toggles = answer.getByTestId("answer-passage-mark-toggle");
  await expect(toggles).toHaveCount(cases.length);
  for (let index = 0; index < cases.length; index += 1) {
    await toggles.nth(index).click();
    const reason = answer.getByTestId("answer-passage-mark-reasons").nth(index);
    await expect(reason).toContainText(marks[index].reasons[0].label);
    await expect(reason).toContainText(cases[index][1]);
    await expect(reason).not.toContainText(contract.reason_codes.quote_unmatched.label);
  }
  await expectNoPageOverflow(page);
  await toggles.first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("citation-cause-reference.png") });
  await toggles.last().scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("citation-cause-semantic.png") });
});

// 区切り線の原文は存在するが、操作の裏付けとしては使わない。本文と閲覧経路は残す。
test("区切り線だけの引用は局所的に案内し、存在する原文を引き続き開ける", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const contract = JSON.parse(readFileSync(`${__dirname}/../../../platform/contracts/answer-passages/passage-marks.json`, "utf8")) as { reason_codes: Record<string, { label: string }> };
  const code = "citation_content_unusable";
  const operation = "合成の操作を行います。";
  const separator = "========================================";
  const detail = "指定範囲の原文は取得できましたが、区切り線・書式記号だけのため、この説明の根拠として扱っていません。";
  await page.route("**/api/documents/doc-a/content**", (route) =>
    route.fulfill({ status: 200, contentType: "text/plain; charset=utf-8", body: separator })
  );
  const rawSource = citation("doc-a:c-separator", separator, {
    evidence_model_used: false,
    evidence_parent_used: false,
  });
  const answer = await ask(page, [rawSource], undefined, {
    text: [
      "資料により確認できた事項",
      "・合成の確認済み説明。",
      "",
      "根拠で確かめきれていない事項",
      "・" + operation,
    ].join("\n"),
    passages: {
      passage_marks: [{
        text: operation,
        state: "unverified",
        reasons: [{ code, label: contract.reason_codes[code].label, detail }],
      }],
      removed_passages: [],
      answer_warnings: [],
    },
  });
  await expect(answer.getByText(operation, { exact: true })).toBeVisible();
  await answer.getByTestId("answer-passage-mark-toggle").click();
  const reasons = answer.getByTestId("answer-passage-mark-reasons");
  await expect(reasons).toContainText(contract.reason_codes[code].label);
  await expect(reasons).toContainText(detail);
  await expect(answer).not.toContainText(contract.reason_codes.quote_unmatched.label);
  await expect(answer).not.toContainText("資料が不足");
  // 有効な出典の行には結び付けず、実際に渡した箇所の一覧からは閲覧できる。
  await expect(answer.getByRole("button", { name: /の原文を開く/ })).toHaveCount(0);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("citation-content-unusable-mark.png") });

  const summary = page.locator("summary").filter({ hasText: "AI に渡した 1 件" });
  await expect(summary).not.toContainText("回答で引用");
  const card = page.getByTestId("evidence-card");
  if (!(await card.isVisible())) await summary.click();
  await card.getByRole("button", { name: "文書 A.pdf の根拠を表示" }).click();
  const opened = await openedSheet(page);
  expect(opened.excerpt).toBe(separator);
  await expect(opened.sheet).not.toContainText(contract.reason_codes.quote_unmatched.label);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("citation-content-unusable-source.png") });
  await closeSheet(page);
  await expect(answer.getByText(operation, { exact: true })).toBeVisible();
});


test("意味確認未実施の操作は本文と引用を保ち、該当確認済みと数えない", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const body = "合成の操作を行います。";
  const source = citation("doc-a:c-o", OPERATION, {
    evidence_model_used: true,
    evidence_answer_use: "confirmed",
    evidence_claim_support: { scope: "answer", execution: "not_run", status: "unknown", policy: "recorded" },
    answer_citation_lines: [1],
    answer_citation_provenance: "child",
  });
  const answer = await ask(page, [source], undefined, { text: ["操作手順", "・" + body, "根拠：文書 A.pdf p.3"].join("\n"), expectedHeading: "操作手順" });
  await expect(answer.getByText(body, { exact: true })).toBeVisible();
  await expect(answer).not.toContainText("未確認");
  const summary = page.locator("summary").filter({ hasText: "AI に渡した 1 件" });
  await expect(summary).toContainText("回答で引用 1 件");
  await expect(summary).not.toContainText("該当確認済み");
  await expect(summary).not.toContainText("参考");
  await summary.click();
  await expect(page.getByText("内容の裏付け確認は未実施", { exact: true })).toBeVisible();
  await expectNoPageOverflow(page);
  await page.getByText("内容の裏付け確認は未実施", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("citation-support-not-run.png") });
  await page.getByTestId("evidence-card").getByRole("button", { name: "文書 A.pdf の根拠を表示" }).click();
  const opened = await openedSheet(page);
  expect(opened.excerpt).toBe(OPERATION);
  await closeSheet(page);
  await expect(answer.getByText(body, { exact: true })).toBeVisible();
});
