import type { ReactNode } from "react";
import {
  ApiErrorBanner,
  ClarificationCard,
  ClarificationRecord,
  type ClarificationBusyAction,
} from "@production-ready/ui";

import type { ConfirmationAction, ConfirmationAnswer, ConfirmationRequest } from "@/lib/api";
import {
  clarificationItems,
  confirmationAnswers,
  confirmationRound,
  type ConfirmationRecordView,
} from "@/lib/confirmation";
import { t } from "@/lib/i18n";

/**
 * 回答前の確認のカード（#1839）。部品・文言・操作は 3 製品共通の `ClarificationCard`（「この内容で回答」「確認せずに
 * 回答」と「その他（自由入力）」）。確認を終えるのは「確認せずに回答」だけで、「確認をやめる」は置かない（質問を
 * 取り消すのは入力欄の「停止」。#2311）。「この内容で回答」を押すと、カードは答えた内容と経過の行 1 つに変わる。
 * 回答の枠の中に出すので `variant="plain"`。
 *
 * - Agent 自身の確認（`ask_user_confirmation`）: 答え・スキップで同じ回答の続きが動く。
 * - ツールの結果の確認（NL2SQL・RAG）: 確認事項は呼び先が作ったまま。答え・スキップで次の回答を作る（NL2SQL の
 *   スキップは `skip_clarification` で呼び直す）。製品の操作（「データベースの検索は不要」など）は `actions` で確定の
 *   右に置く。スキップを出すかは backend の `can_skip`。
 */
export function PendingConfirmationCard({
  confirmation,
  disabled,
  busyAction,
  error,
  actions,
  autoFocus = false,
  onDecide,
}: {
  confirmation: ConfirmationRequest;
  disabled: boolean;
  busyAction: ClarificationBusyAction | null;
  error: unknown;
  actions?: ReactNode;
  /** 出たときに確認事項の文へフォーカスを移す（送信の続きで出たとき。UX 契約 §7）。 */
  autoFocus?: boolean;
  onDecide: (action: ConfirmationAction, answers?: ConfirmationAnswer[]) => void;
}) {
  const sourceNote =
    confirmation.source === "nl2sql"
      ? t("chat.confirmation.source.nl2sql")
      : confirmation.source === "rag"
        ? t("chat.confirmation.source.rag")
        : null;
  return (
    <ClarificationCard
      items={clarificationItems(confirmation)}
      variant="plain"
      round={confirmationRound(confirmation)}
      busyAction={busyAction}
      disabled={disabled}
      onConfirm={(answers) => onDecide("answer", confirmationAnswers(answers))}
      onSkip={confirmation.can_skip ? () => onDecide("skip") : undefined}
      error={error ? <ApiErrorBanner error={error} fallback={t("chat.confirmation.failed")} testId="chat-confirmation-error" /> : null}
      actions={actions}
      autoFocus={autoFocus}
      idPrefix={`chat-${confirmation.id}`}
      testId="chat-confirmation"
    >
      {sourceNote ? (
        <p className="text-xs text-fg-muted" data-testid="chat-confirmation-source">
          {sourceNote}
        </p>
      ) : null}
    </ClarificationCard>
  );
}

/** 回答前に確認した内容（会話の利用者の側。3 製品共通の `ClarificationRecord` の吹き出し）。 */
export function ConfirmationRecordBubble({ record, testId }: { record: ConfirmationRecordView; testId: string }) {
  return <ClarificationRecord entries={record.entries} outcome={record.outcome} variant="bubble" testId={testId} />;
}
