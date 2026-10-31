import { SearchX } from "lucide-react";
import { Button } from "@production-ready/ui";

import type { ClarificationKind } from "@/lib/api";
import { t } from "@/lib/i18n";

/**
 * 確認の取り消し（#1441・#1445）。NL2SQL の SQL の生成前の確認と RAG の確認（画面・機能・業務など）で同じ部品を使う。
 *
 * - 操作: 利用者の答えを待っている確認の質問（回答）の下に「データベースの検索は不要」「資料の検索は不要」を置く。
 *   押すとその文を利用者の発言として送り、その質問ではそのツールを使わずに続ける（backend が Run に記録する）。
 * - 補足: 取り消した往復（操作でも、会話の文でも）には「データベースを使わずに回答を続けます」などを出す。
 *   色だけに頼らず、アイコンと文で示す。
 */
export function ClarificationCancelActions({
  kinds,
  disabled,
  onCancel,
  testId,
}: {
  kinds: ClarificationKind[];
  disabled: boolean;
  onCancel: (kind: ClarificationKind) => void;
  testId: string;
}) {
  if (kinds.length === 0) return null;
  return (
    <div
      role="group"
      aria-label={t("chat.clarificationCancel.group")}
      className="flex flex-wrap items-center gap-2"
      data-testid={testId}
    >
      {kinds.map((kind) => (
        <Button
          key={kind}
          type="button"
          variant="secondary"
          size="sm"
          icon={SearchX}
          disabled={disabled}
          onClick={() => onCancel(kind)}
          data-testid={`${testId}-${kind}`}
        >
          {t(`chat.clarificationCancel.${kind}`)}
        </Button>
      ))}
    </div>
  );
}

export function ClarificationCancelNote({ kinds, testId }: { kinds: ClarificationKind[]; testId: string }) {
  if (kinds.length === 0) return null;
  return (
    <ul className="space-y-1" data-testid={testId}>
      {kinds.map((kind) => (
        <li key={kind} className="flex items-center gap-1.5 text-sm text-fg-muted">
          <SearchX size={14} className="shrink-0" aria-hidden="true" />
          <span className="min-w-0 break-words">{t(`chat.clarificationCancel.note.${kind}`)}</span>
        </li>
      ))}
    </ul>
  );
}
