import { ProcessingIndicator, type ProcessingActivityIcon } from "@production-ready/ui";

import { answerProgressLabel, type AnswerStageEvent } from "@/lib/answer-progress";
import { t } from "@/lib/i18n";

/**
 * 回答生成（RAG 検索・チャット）の処理中の表示（#375）。共有の `ProcessingIndicator`（NL2SQL と同じ）で
 * 「回答を生成しています（今の工程）」と経過時間を出し、10 秒を超えたら共通の遅延の案内（「通常より時間が
 * かかっています。」。3 製品で同じ文言。#2426）を処理の名前の直下に出す。
 * 完了後は `finalLabel` と処理時間を出す。
 */
export function AnswerProgress({
  active,
  stages,
  startedAtMs,
  finishedAtMs = null,
  finalLabel,
  activityIcon = "spinner",
  generateAnswer = true,
  testId,
  className,
}: {
  active: boolean;
  stages: readonly AnswerStageEvent[];
  startedAtMs: number;
  finishedAtMs?: number | null;
  finalLabel?: string;
  /** 起点のボタンが loading を出しているときは "none"（動くスピナーは 1 つだけ）。 */
  activityIcon?: ProcessingActivityIcon;
  /** 回答を作らない RAG 検索では false（「検索しています」。#649）。 */
  generateAnswer?: boolean;
  testId?: string;
  className?: string;
}) {
  return (
    <ProcessingIndicator
      active={active}
      label={answerProgressLabel(stages, { generateAnswer })}
      finalLabel={finalLabel}
      operationKey={startedAtMs}
      startedAt={startedAtMs}
      finishedAt={active ? null : finishedAtMs}
      placement="result"
      activityIcon={activityIcon}
      testId={testId}
      className={className}
      labels={{
        elapsed: t("answer.progress.elapsed"),
        duration: t("answer.progress.duration"),
      }}
    />
  );
}
