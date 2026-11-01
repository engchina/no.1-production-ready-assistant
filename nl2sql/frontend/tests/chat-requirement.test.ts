// チャットの回答前の確認（#1442）の決まりごと: 自動の検査の要求（会話の続き・業務プロファイル）、確認の
// 質問が 0 件でも直行しないこと（#1761）、確認の質問・まとめ・終わった確認の見分け、作業状態から戻した値の検査。
import assert from "node:assert/strict";
import test from "node:test";

import {
  chatRequirementIdempotencyKey,
  chatRequirementSessionRequest,
  decodeChatRequirementCheck,
  encodeChatRequirementCheck,
  parseChatRequirementCheck,
  type ChatJobRequest,
} from "../src/features/nl2sql/chatRequirement.ts";
import {
  isRequirementCheckUnavailable,
  requirementNextStep as chatRequirementStep,
} from "../src/features/nl2sql/requirementCheck.ts";
import type {
  QuerySession,
  RequirementItem,
  RequirementState,
} from "../src/features/nl2sql/ontology/types.ts";

const request: ChatJobRequest = {
  question: "多い順にして",
  profile_id: "sales",
  engine: "enterprise_ai_direct",
  previous_job_id: "job-1",
  client_job_id: "11111111-1111-4111-8111-111111111111",
};

const item: RequirementItem = {
  id: "item-1",
  key: "where_status",
  section: "where",
  label_ja: "受注状態",
  value_ja: "",
  status: "ambiguous",
  answer_kind: "single_select",
  candidates: [],
  source: "question",
  tables: [],
  columns: [],
  relationship_ids: [],
  answered: false,
};

function state(overrides: Partial<RequirementState> = {}): RequirementState {
  return {
    status: "ready",
    current_item: null,
    unresolved_items: [],
    answered_count: 0,
    review_round: 0,
    max_review_rounds: 3,
    review_limit_reached: false,
    select_from_clear: true,
    minimum_missing_ja: [],
    can_apply: true,
    summary_ja: "",
    sql_skeleton: "SELECT …",
    message_ja: "",
    ...overrides,
  };
}

function session(overrides: Partial<QuerySession> = {}): QuerySession {
  return {
    id: "query_session_1",
    profile_id: "sales",
    ontology_revision_id: "rev",
    status: "awaiting_intent_confirmation",
    requirement_state: state(),
    ...overrides,
  };
}

test("自動の検査は会話の続き（前のターン）とチャットの業務プロファイルで session を作る", () => {
  assert.deepEqual(chatRequirementSessionRequest(request), {
    question: "多い順にして",
    profile_id: "sales",
    engine: "enterprise_ai_direct",
    clarification_mode: "guided",
    chat: true,
    previous_job_id: "job-1",
    attachment_ids: undefined,
  });
  // 同じ送信は同じ key（再読込で作り直しても同じ session）。
  assert.equal(
    chatRequirementIdempotencyKey(request.client_job_id),
    chatRequirementIdempotencyKey(request.client_job_id)
  );
  assert.notEqual(chatRequirementIdempotencyKey(request.client_job_id), chatRequirementIdempotencyKey("other"));
});

test("確認の質問が 0 件でも直行せず、確認のカードで利用者が確かめてから SQL を作る (#1761)", () => {
  assert.equal(chatRequirementStep(session()), "summary");
});

test("確認の質問がある・答えられない・最低の基準を満たさないときは会話の中で聞く", () => {
  assert.equal(
    chatRequirementStep(
      session({
        requirement_state: state({
          status: "needs_answer",
          current_item: item,
          unresolved_items: [item],
          can_apply: false,
        }),
      })
    ),
    "ask"
  );
  assert.equal(
    chatRequirementStep(session({ requirement_state: state({ status: "unanswerable", can_apply: false }) })),
    "ask"
  );
  assert.equal(
    chatRequirementStep(
      session({
        requirement_state: state({
          select_from_clear: false,
          minimum_missing_ja: ["FROM"],
          can_apply: false,
        }),
      })
    ),
    "ask"
  );
});

test("全部に答えたら（再検査の後）まとめを確かめてから SQL を作る", () => {
  assert.equal(
    chatRequirementStep(session({ requirement_state: state({ answered_count: 2, review_round: 1 }) })),
    "summary"
  );
  // 上限で残った項目を AI に任せられるときも、まとめを出す。
  assert.equal(
    chatRequirementStep(
      session({
        requirement_state: state({
          status: "needs_answer",
          unresolved_items: [item],
          review_limit_reached: true,
          answered_count: 3,
          review_round: 3,
        }),
      })
    ),
    "summary"
  );
});

test("取り消し・期限切れ・反映済みを見分ける", () => {
  assert.equal(chatRequirementStep(session({ status: "cancelled" })), "ended");
  assert.equal(chatRequirementStep(session({ status: "error" })), "ended");
  assert.equal(chatRequirementStep(session({ status: "done" })), "ended");
  assert.equal(
    chatRequirementStep(session({ status: "done", requirement_applied_at: "2026-10-12T00:00:00Z" })),
    "applied"
  );
});

test("作業状態から戻した値は形を検査し、古い・壊れた値は捨てる", () => {
  const stored = { request, sentAtMs: 1, sessionId: "query_session_1" };
  assert.deepEqual(parseChatRequirementCheck(stored), stored);
  assert.deepEqual(
    parseChatRequirementCheck({ ...stored, request: { ...request, previous_job_id: null } })?.request
      .previous_job_id,
    null
  );
  assert.equal(parseChatRequirementCheck(null), null);
  assert.equal(parseChatRequirementCheck({ ...stored, request: { ...request, question: " " } }), null);
  assert.equal(parseChatRequirementCheck({ ...stored, request: { ...request, engine: "other" } }), null);
  assert.equal(parseChatRequirementCheck({ ...stored, sessionId: 1 }), null);
  // 反映した session の ID は作業状態から受け取らない（ジョブの投入のときに付ける）。
  assert.equal(
    parseChatRequirementCheck({ ...stored, request: { ...request, requirement_session_id: "x" } })?.request
      .requirement_session_id,
    undefined
  );
});

test("要件の検査を使えない設定（OCI Enterprise AI が無い）だけは確認せずに SQL を作る", () => {
  const unavailable = Object.assign(new Error("OCI Enterprise AI が設定されていません(REQUIREMENT_AI_NOT_CONFIGURED)"), {
    status: 503,
  });
  const failed = Object.assign(new Error("LLM の呼び出しに失敗しました(REQUIREMENT_AI_FAILED)"), { status: 503 });
  assert.equal(isRequirementCheckUnavailable(unavailable), true);
  assert.equal(isRequirementCheckUnavailable(failed), false);
  assert.equal(isRequirementCheckUnavailable(new Error("x")), false);
});

test("作業状態には JSON の文字列で残し、空・壊れた文字列は確認なしに戻す", () => {
  const check = { request, sentAtMs: 1, sessionId: "" };
  assert.deepEqual(decodeChatRequirementCheck(encodeChatRequirementCheck(check)), check);
  assert.equal(encodeChatRequirementCheck(null), "");
  assert.equal(decodeChatRequirementCheck(""), null);
  assert.equal(decodeChatRequirementCheck("{"), null);
});



test("回答前の確認へ添付 ID を渡す", () => {
  assert.deepEqual(chatRequirementSessionRequest({ ...request, engine: "enterprise_ai_direct", attachment_ids: ["attachment-a"] }).attachment_ids, ["attachment-a"]);
});
