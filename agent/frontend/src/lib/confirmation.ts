import type {
  ClarificationAnswer,
  ClarificationItem,
  ClarificationRecordEntry,
} from "@production-ready/ui";

import type {
  ConfirmationAnswer,
  ConfirmationRequest,
  ConfirmationResponseRecord,
  RunState,
} from "@/lib/api";

/**
 * 回答前の確認（#1839。3 製品共通の UX 契約 `platform/docs/ux-contracts/confirm-before-answer.md`）の、Run の記録 →
 * 共通の `ClarificationCard` / `ClarificationRecord` の props。
 *
 * 確認事項・選択肢は呼び先（NL2SQL・RAG）か Agent のモデルが作ったもので、ここは形を変えるだけ（文言を足さない）。
 * 読めない記録は無いものとして扱う（古い Run・壊れた記録で画面を止めない）。
 */

/** Agent 自身の回答前の確認のツールの名前（backend の `CONFIRMATION_TOOL`）。イベントの記録の名前に使う。 */
export const CONFIRMATION_TOOL = "ask_user_confirmation";

function records<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value.filter((item) => item && typeof item === "object") as T[]) : [];
}

/** Run の回答前の確認（古い Run は空）。 */
export function runConfirmations(run: Pick<RunState, "confirmations">): ConfirmationRequest[] {
  return records<ConfirmationRequest>(run.confirmations);
}

/**
 * 答えていない確認（カードを出す確認）。Agent 自身の確認は Run が確認待ちの間だけ、ツールの結果の確認は完了した
 * Run のものだけ（会話が先へ進んだ確認は backend が古い確認にする）。
 */
export function pendingConfirmation(run: RunState): ConfirmationRequest | null {
  return (
    runConfirmations(run).find(
      (item) =>
        item.status === "pending" &&
        (item.source === "agent" ? run.status === "waiting_confirmation" : run.status === "completed")
    ) ?? null
  );
}

/** 共通の確認のカードの確認事項。 */
export function clarificationItems(confirmation: ConfirmationRequest): ClarificationItem[] {
  return records<ConfirmationRequest["items"][number]>(confirmation.items).map((item) => ({
    id: item.id,
    prompt: item.prompt,
    reason: item.reason || undefined,
    selection: item.selection === "multiple" ? "multiple" : "single",
    options: records<ConfirmationRequest["items"][number]["options"][number]>(item.options).map((option) => ({
      id: option.id,
      label: option.label,
      description: option.description || undefined,
    })),
  }));
}

/** カードの答え → API の答え。 */
export function confirmationAnswers(answers: ClarificationAnswer[]): ConfirmationAnswer[] {
  return answers.map((answer) => ({
    item_id: answer.itemId,
    option_ids: answer.optionIds,
    other_text: answer.otherText,
  }));
}

/** 往復の表示（上限を超えた確認では出さない。NL2SQL の確認は NL2SQL の方針で出ることがある）。 */
export function confirmationRound(confirmation: ConfirmationRequest): { current: number; max: number } | undefined {
  const current = Number(confirmation.round);
  const max = Number(confirmation.max_rounds);
  if (!Number.isInteger(current) || !Number.isInteger(max) || current < 1 || max < 1 || current > max) return undefined;
  return { current, max };
}

export interface ConfirmationRecordView {
  key: string;
  entries: ClarificationRecordEntry[];
  outcome: "answered" | "skipped";
}

function recordOf(confirmation: ConfirmationRequest): ConfirmationRecordView | null {
  if (confirmation.status !== "answered" && confirmation.status !== "skipped") return null;
  const answers = new Map(records<ConfirmationAnswer>(confirmation.answers).map((answer) => [answer.item_id, answer]));
  return {
    key: confirmation.id,
    outcome: confirmation.status,
    entries: records<ConfirmationRequest["items"][number]>(confirmation.items).map((item) => {
      const answer = answers.get(item.id);
      const options = records<{ id: string; label: string }>(item.options);
      const labels = new Map(options.map((option) => [option.id, option.label]));
      return {
        id: item.id,
        prompt: item.prompt,
        selected: (answer?.option_ids ?? []).map((id) => labels.get(id) ?? "").filter(Boolean),
        otherText: answer?.other_text || undefined,
        // 選択肢の無い確認事項の答えに「その他: 」を付けない（#3366）。
        freeTextOnly: options.length === 0,
      };
    }),
  };
}

/** Agent 自身の確認に答えた内容（同じ Run の利用者の側に残す。古い順）。 */
export function agentConfirmationRecords(run: RunState): ConfirmationRecordView[] {
  return runConfirmations(run)
    .filter((item) => item.source === "agent")
    .map(recordOf)
    .filter((item): item is ConfirmationRecordView => item !== null);
}

/**
 * ツールの結果の確認に答えて作った Run の答え（metadata `clarification_response`）。会話の利用者の側に、質問の
 * 吹き出しの代わりに出す。答えで作った Run でなければ null。
 */
export function responseRecord(run: Pick<RunState, "metadata">): ConfirmationRecordView | null {
  const value = run.metadata?.clarification_response as Partial<ConfirmationResponseRecord> | undefined;
  if (!value || typeof value !== "object" || (value.action !== "answer" && value.action !== "skip")) return null;
  return {
    key: String(value.confirmation_id ?? "response"),
    outcome: value.action === "skip" ? "skipped" : "answered",
    entries: records<ConfirmationResponseRecord["entries"][number]>(value.entries).map((entry, index) => ({
      id: entry.item_id || String(index),
      prompt: String(entry.prompt ?? ""),
      selected: Array.isArray(entry.selected) ? entry.selected.map(String) : [],
      otherText: entry.other_text || undefined,
      // 選択肢の無い確認事項の答えに「その他: 」を付けない（記録は答えた時点の写し。値の無い古い記録は付ける。#3410）。
      freeTextOnly: entry.free_text_only === true,
    })),
  };
}
