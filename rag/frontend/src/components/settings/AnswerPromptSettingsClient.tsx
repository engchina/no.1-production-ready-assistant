"use client";

import { FormSkeleton, PageBody, TimedLoadingState } from "@production-ready/ui";

import { ErrorState } from "@/components/StateViews";
import { useAnswerPrompts } from "@/lib/queries";
import { t } from "@/lib/i18n";

import { isRetrievalPromptStage } from "./answer-prompt.logic";

import { AnswerPromptStages } from "./AnswerPromptStages";

/**
 * 回答プロンプトの画面。回答を作る指示のテンプレートと、回答の各工程（読み取り専用）を出す。
 * 以前の system prompt の版（作成・有効化）は、回答エンジンを 1 つにしたときに削除した（#595）。
 */
export function AnswerPromptSettingsClient() {
  // 初回の読み込みはページの先頭で経過時間と形の Skeleton を出す（カードの中の Skeleton だけにしない）。
  // 取得の結果はカードの編集欄が同じ query から読む。
  const query = useAnswerPrompts();
  if (query.isPending) {
    return (
      <PageBody wide>
        <TimedLoadingState
          label={t("settings.loading")}
          operationKey="settings-prompts-load"
          placement="page"
          testId="settings-prompts-loading"
        >
          <FormSkeleton />
        </TimedLoadingState>
      </PageBody>
    );
  }
  if (query.isError) {
    return (
      <PageBody wide>
        <ErrorState message={t("settings.answerPrompts.loadError")} onRetry={() => void query.refetch()} />
      </PageBody>
    );
  }
  const stages = query.data.stages.filter((stage) => stage.phase !== "ingestion" && stage.phase !== "indexing" && !isRetrievalPromptStage(stage) && stage.id !== "evaluation");
  // 編集欄は回答生成の位置に置く。固定の段が返らない場合も編集欄への導線を残す。
  const withGeneration = [...stages];
  if (!stages.some((stage) => stage.id === "generation")) {
    const afterGeneration = stages.findIndex((stage) => stage.phase === "audit" || stage.phase === "evaluation");
    withGeneration.splice(afterGeneration < 0 ? stages.length : afterGeneration, 0, {
      id: "generation",
      phase: "generation",
      prompts: [],
    });
  }
  return (
    <PageBody wide>
      <AnswerPromptStages stages={withGeneration} />
    </PageBody>
  );
}
