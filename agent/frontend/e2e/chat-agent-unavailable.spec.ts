import type { Page } from "@playwright/test";

import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #3356: 業務 Agent が削除された・使えない会話を「会話を開く」で開いたら、既定の業務 Agent の新しい会話へ黙って
// 切り替えず、案内を出して会話の履歴を閲覧だけで出す（#3244 の閲覧だけの会話と同じ形）。

const THREAD_ID = "thread_3356";

function seedOrphanRun(mockApi: MockApi) {
  mockApi.state.runs.push({
    id: "run-3356",
    goal: "合成の質問 C",
    agent_id: "agent-deleted",
    runtime_id: "builtin",
    status: "completed",
    steps: [],
    events: [],
    approvals: [],
    artifacts: [
      { id: "run-3356-answer", name: "回答", kind: "answer", created_at: MOCK_NOW, content: { text: "合成の回答です。" } },
    ],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: THREAD_ID,
    thread_owner_user_uuid: "local",
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

test.describe("業務 Agent が使えない会話（#3356）", { tag: "@responsive" }, () => {
  test("「会話を開く」で開くと案内を出し、会話は閲覧だけで出す。新しい会話で既定の業務 Agent に戻る", async (
    { page, mockApi },
    testInfo
  ) => {
    seedOrphanRun(mockApi);
    await page.goto(`/chat?agent_id=agent-deleted&thread_id=${THREAD_ID}`);

    // 会話の履歴（質問と回答）は出す。
    const log = page.getByRole("log", { name: "会話" });
    await expect(log).toContainText("合成の質問 C");
    await expect(log).toContainText("合成の回答です。");
    // 既定の業務 Agent へ黙って切り替えない。入力欄の代わりに案内を出す。
    const notice = page.getByTestId("chat-agent-unavailable-notice");
    await expect(notice).toContainText("この会話の業務 Agent は削除されたか、使えません");
    await expect(notice).toContainText("業務 Agent を選ぶか新しい会話を始めてください");
    await expect(page.getByRole("textbox", { name: "質問" })).toHaveCount(0);
    await expect(page.getByTestId("chat-send")).toHaveCount(0);
    const agentSelect = page.locator("#chat-agent");
    await expect(agentSelect).toContainText("業務 Agent を選択");
    // 閲覧だけの会話なので、回答の評価は出さない（#3244）。
    await expect(page.getByRole("group", { name: "この回答は役に立ちましたか？" })).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: testInfo.outputPath("chat-agent-unavailable.png"), fullPage: true });

    // 新しい会話を始めると、使える業務 Agent の入力欄に戻る。
    await page.getByRole("button", { name: "新しい会話" }).click();
    await expect(notice).toHaveCount(0);
    await expect(page.getByRole("textbox", { name: "質問" })).toBeVisible();
    await expect(agentSelect).not.toContainText("業務 Agent を選択");
  });
});
