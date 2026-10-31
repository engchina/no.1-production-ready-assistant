import { sameRunStepDetails } from "../data/run-step-details";
import type { ChatProgressStep, ChatProgressStepStatus } from "./chat-progress";

/**
 * チャットの回答の処理の段階の一覧を、処理中は単調に保つ規則（3 製品共通。#1358）。
 *
 * 3 製品は配信（NL2SQL: ジョブの polling、RAG: SSE と保存済みの段階、Agent: Run の polling）の最新の状態から
 * 段階の一覧を毎回作り直す。状態が一時的に戻る（古い snapshot・取り直し・段階が実行中に戻る組み立て）と、
 * 完了した段階が「N ステップ完了」の一覧から消えて、また現れる。ここでは次の規則で一覧を保つ。
 *
 * - 段階は `id` で結ぶ。
 * - 処理中は、一度出した段階を消さない（新しい一覧に無い段階も残す）。順序は前の一覧の順を保ち、
 *   新しい段階は新しい一覧の中の直前の段階の後ろに入れる。
 * - 状態は進む方へだけ変える（待機中 → 実行中 → 完了・失敗・スキップ）。戻る更新は前の段階を残す。
 *   同じ段階の間の更新（実行中の補足・名前・時刻、完了から失敗への確定など）は新しい方を使う。
 * - 終端（処理中でない）と、対象（送信・Run・ジョブ）の切り替えでは、新しい一覧で作り直す。
 *
 * 純粋な関数（React に依存しない）。画面は `useChatProgressSteps`（`ChatProgress` が内部で呼ぶ）を通して使う。
 */

/** 状態の進み具合（完了・失敗・スキップは終わった段階として同じ）。 */
export const CHAT_PROGRESS_STATUS_RANK: Readonly<Record<ChatProgressStepStatus, number>> = {
  pending: 0,
  running: 1,
  done: 2,
  failed: 2,
  skipped: 2,
};

/** 段階の一覧の保持している状態（対象と、その対象で出した一覧）。 */
export interface ChatProgressStepsState {
  /** 対象（送信・Run・ジョブなど）。変わったら一覧を作り直す。 */
  key: string | null;
  steps: ChatProgressStep[];
}

export interface ChatProgressStepsInput {
  key: string | null;
  /** 配信の最新の状態から作った段階の一覧。 */
  steps: readonly ChatProgressStep[];
  /** 処理中か。false（終端）なら新しい一覧で確定する。 */
  active: boolean;
}

function sameStep(a: ChatProgressStep, b: ChatProgressStep): boolean {
  return (
    a.id === b.id &&
    a.label === b.label &&
    a.status === b.status &&
    a.startedAt === b.startedAt &&
    a.finishedAt === b.finishedAt &&
    a.detail === b.detail &&
    a.durationMs === b.durationMs &&
    a.meta === b.meta &&
    a.level === b.level &&
    a.children === b.children &&
    sameRunStepDetails(a.details, b.details)
  );
}

function sameSteps(a: readonly ChatProgressStep[], b: readonly ChatProgressStep[]): boolean {
  return a.length === b.length && a.every((step, index) => sameStep(step, b[index]));
}

/** 同じ id が 2 回あれば最初の 1 つにする（id は一覧の中で一意の契約）。 */
function uniqueSteps(steps: readonly ChatProgressStep[]): ChatProgressStep[] {
  const seen = new Set<string>();
  return steps.filter((step) => {
    if (seen.has(step.id)) return false;
    seen.add(step.id);
    return true;
  });
}

/**
 * 前の一覧に新しい一覧を重ねる（処理中の規則）。変わらなければ `previous` をそのまま返す。
 */
export function mergeChatProgressSteps(
  previous: readonly ChatProgressStep[],
  next: readonly ChatProgressStep[]
): ChatProgressStep[] {
  const incoming = uniqueSteps(next);
  if (previous.length === 0) return incoming;
  const previousById = new Map(previous.map((step) => [step.id, step]));
  const incomingById = new Map(incoming.map((step) => [step.id, step]));

  // 順序: 前の一覧の順を保ち、新しい段階は、新しい一覧の中で直前にある段階の後ろへ入れる。
  const order = previous.map((step) => step.id);
  let anchor = -1;
  for (const step of incoming) {
    const index = order.indexOf(step.id);
    if (index >= 0) {
      anchor = index;
      continue;
    }
    order.splice(anchor + 1, 0, step.id);
    anchor += 1;
  }

  const merged = order.map((id) => {
    const before = previousById.get(id);
    const after = incomingById.get(id);
    if (!after) return before as ChatProgressStep;
    if (!before) return after;
    // 戻る更新（完了 → 実行中・待機中、実行中 → 待機中）は前の段階を残す。
    return CHAT_PROGRESS_STATUS_RANK[after.status] >= CHAT_PROGRESS_STATUS_RANK[before.status] ? after : before;
  });
  return sameSteps(merged, previous) ? (previous as ChatProgressStep[]) : merged;
}

/**
 * 段階の一覧の状態を進める（`mergeChatProgressSteps` に、作り直しの規則を足したもの）。
 *
 * - 初回・対象が変わった・終端: 新しい一覧で作り直す。
 * - 処理中: 前の一覧に重ねる（消さない・戻さない）。
 *
 * 変わらなければ `state` をそのまま返す。
 */
export function reduceChatProgressSteps(
  state: ChatProgressStepsState | null,
  input: ChatProgressStepsInput
): ChatProgressStepsState {
  if (state === null || state.key !== input.key || !input.active) {
    const steps = uniqueSteps(input.steps);
    if (state !== null && state.key === input.key && sameSteps(state.steps, steps)) return state;
    return { key: input.key, steps };
  }
  const steps = mergeChatProgressSteps(state.steps, input.steps);
  return steps === state.steps ? state : { key: input.key, steps };
}
