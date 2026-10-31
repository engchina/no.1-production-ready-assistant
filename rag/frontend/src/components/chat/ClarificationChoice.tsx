import { ClarificationCard, type ClarificationBusyAction } from "@production-ready/ui";

import type {
  ClarificationAnswer,
  ClarificationSection,
  ClarificationSuggestionData,
} from "@/lib/api";
import { t } from "@/lib/i18n";

function sectionLabel(section: ClarificationSection): string {
  const { page_start: start, page_end: end } = section;
  const pages =
    start === null ? "" : end === null || end === start ? ` p.${start}` : ` p.${start}–${end}`;
  return `「${section.document_name || section.document_id}」${section.title}${pages}`;
}

/**
 * 回答前の確認（#717 / #1238。3 製品共通の `ClarificationCard` に載せた。#1839）。検索・回答プロファイルのルールの
 * 確認と業務ガイドの条件の確認で同じカードを出す。選択肢は複数選択（チェック）か 1 つ（ラジオ）で、選択肢の
 * 章節は選択肢の補足に出す。「その他（自由入力）」は常に出す（ルールに保存した `allow_other` は読まない）。
 * 確認を終えるのは「確認せずに回答」（確認を使わずに前提を示して回答する）だけで、カードに「確認をやめる」は
 * 置かない（質問を取り消すのは入力欄の「停止」。#2311）。
 */
export function ClarificationChoice({
  suggestion,
  busyAction = null,
  disabled = false,
  onAnswer,
  onSkip,
}: {
  suggestion: ClarificationSuggestionData;
  busyAction?: ClarificationBusyAction | null;
  disabled?: boolean;
  onAnswer: (answer: ClarificationAnswer) => void;
  onSkip: () => void;
}) {
  const { clarification } = suggestion;
  return (
    <ClarificationCard
      testId="chat-clarification-choice"
      autoFocus
      items={[
        {
          id: suggestion.rule_id,
          prompt: clarification.question,
          selection: clarification.multiple ? "multiple" : "single",
          options: clarification.options.map((option) => ({
            id: option.id,
            label: option.label,
            description: option.description || undefined,
            detail:
              option.sections.length > 0
                ? t("chat.clarify.sections", { sections: option.sections.map(sectionLabel).join("、") })
                : undefined,
          })),
        },
      ]}
      busyAction={busyAction}
      disabled={disabled}
      onConfirm={([answer]) =>
        onAnswer({
          rule_id: suggestion.rule_id,
          option_ids: answer?.optionIds ?? [],
          other_text: answer?.otherText ?? "",
        })
      }
      onSkip={onSkip}
    />
  );
}
