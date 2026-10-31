/**
 * チャットの回答前の確認（#1442）の e2e の応答。チャットで質問を送ると、SQL を作る前に
 * `POST /api/nl2sql/query-sessions`（`chat: true`）で要件を検査する。
 *
 * 既定は「確認の質問が無い」（`clear`）: 確認の質問は出さないが、確認のカードを出し、「この内容で SQL を生成して
 * 実行」を押すまで SQL の生成のジョブを投入しない（#1761）。押す操作は `confirmChatRequirement`。
 * 確認の流れを確かめる spec は `create` に最初の段階、`answers` に回答ごとの次の段階を渡す。
 */
import { expect, type Page, type Request } from "@playwright/test";

import { requirementSessionData, type RequirementStage } from "./requirement-session";

export interface ChatRequirementMock {
  /** `POST /query-sessions` の本文（送った順）。 */
  creates: Record<string, unknown>[];
  /** `POST /query-sessions` の Idempotency-Key（送った順）。 */
  createKeys: string[];
  answers: Record<string, unknown>[];
  applies: number;
  cancels: number;
  /** 今の session の段階。 */
  stage: RequirementStage | "cancelled";
}

export interface ChatRequirementMockOptions {
  /** 作成の応答の段階（既定は確認が要らない `clear`）。 */
  create?: RequirementStage;
  /** 回答のたびに進む段階（足りなければ最後の段階のまま）。 */
  answers?: RequirementStage[];
  /** 作成の応答を遅らせる（要件の検査の経過を確かめる）。 */
  createDelayMs?: number;
  /** 回答の応答を遅らせる（回答の後の再検査の経過・待たずに進む操作を確かめる。#2303）。 */
  answerDelayMs?: number;
  /** 作成を失敗させる（HTTP status と本文の error。`body` を渡すとその本文で返す。#1715）。 */
  createFailure?: { status: number; error: string; body?: Record<string, unknown> };
}

function path(request: Request) {
  return new URL(request.url()).pathname;
}

export async function mockChatRequirement(
  page: Page,
  options: ChatRequirementMockOptions = {},
): Promise<ChatRequirementMock> {
  const state: ChatRequirementMock = {
    creates: [],
    createKeys: [],
    answers: [],
    applies: 0,
    cancels: 0,
    stage: options.create ?? "clear",
  };
  let version = 1;
  let answerIndex = 0;
  const data = () => {
    if (state.stage === "cancelled") {
      const base = requirementSessionData("needs_answer", version);
      return { ...base, session: { ...base.session, status: "cancelled" }, requirement_state: null };
    }
    return requirementSessionData(state.stage, version);
  };
  await page.route(
    (url) => url.pathname.startsWith("/api/nl2sql/query-sessions"),
    async (route) => {
      const request = route.request();
      const pathname = path(request);
      if (pathname === "/api/nl2sql/query-sessions" && request.method() === "POST") {
        state.creates.push(request.postDataJSON());
        state.createKeys.push((await request.headerValue("idempotency-key")) ?? "");
        if (options.createDelayMs) await new Promise((resolve) => setTimeout(resolve, options.createDelayMs));
        if (options.createFailure) {
          return route.fulfill({
            status: options.createFailure.status,
            json: options.createFailure.body ?? { error: options.createFailure.error },
          });
        }
        state.stage = options.create ?? "clear";
        version = 1;
        answerIndex = 0;
        return route.fulfill({ json: { data: data() } });
      }
      if (pathname.endsWith("/clarification-answers")) {
        state.answers.push(request.postDataJSON());
        if (options.answerDelayMs) await new Promise((resolve) => setTimeout(resolve, options.answerDelayMs));
        // 待っている間に中止した session には、backend と同じく答えを反映しない。
        if (state.stage === "cancelled") return route.fulfill({ status: 409, json: { error: "cancelled" } }).catch(() => undefined);
        state.stage = options.answers?.[answerIndex] ?? options.answers?.at(-1) ?? "ready";
        answerIndex += 1;
        version += 1;
        // 画面が回答の要求を中断した（#2303）ときは応答を返せない。
        return route.fulfill({ json: { data: data() } }).catch(() => undefined);
      }
      if (pathname.endsWith("/apply")) {
        state.applies += 1;
        state.stage = "applied";
        return route.fulfill({ json: { data: data() } });
      }
      if (pathname.endsWith("/cancel")) {
        state.cancels += 1;
        state.stage = "cancelled";
        return route.fulfill({ json: { data: data() } });
      }
      return route.fulfill({ json: { data: data() } });
    },
  );
  return state;
}

/**
 * チャットの確認のカードで「この内容で SQL を生成して実行」（実行の権限が無ければ「この内容で SQL を生成」）を
 * 押す（#1761。確認の質問が 0 件でも、押すまで SQL の生成のジョブを投入しない）。
 */
export async function confirmChatRequirement(page: Page) {
  const apply = page.getByTestId("sql-chat-requirement").getByTestId("nl2sql-clarification-card-confirm");
  await expect(apply).toBeEnabled();
  await apply.click();
}
