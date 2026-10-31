"use client";

import { FormSkeleton, PageBody, TimedLoadingState } from "@production-ready/ui";

import { ErrorState } from "@/components/StateViews";
import { AnsweringSettingsCard } from "@/components/settings/AnsweringSettingsCard";
import { t } from "@/lib/i18n";
import {
  useAnswerPrompts,
  useAnsweringSettings,
} from "@/lib/queries";

import { AnswerPromptStages } from "./AnswerPromptStages";
import { isRetrievalPromptStage } from "./answer-prompt.logic";

/**
 * 検索方法の画面。回答の検索と生成の全体設定（検索・回答プロファイルでは上書きしない）と、検索に使うプロンプトを設定する。
 * 以前の検索モード・検索オプションは、回答エンジンを 1 つにしたときに削除した（#595）。
 */
export function RetrievalSettingsClient() {
  // 各設定とプロンプトの初回の読み込みの経過時間は、ページの先頭の 1 か所だけに出す。カードは読み込み中も
  // 形の Skeleton で寸法を保ち、読めたものから順に出す（1 つの取得の失敗・再試行でほかのカードを待たせない）。
  const answering = useAnsweringSettings();
  const prompts = useAnswerPrompts();
  const loading = answering.isPending || prompts.isPending;
  return (
    <PageBody wide>
      {loading ? (
        <TimedLoadingState
          label={t("settings.loading")}
          operationKey="settings-retrieval-load"
          placement="page"
          testId="settings-retrieval-loading"
        />
      ) : null}
      <AnsweringSettingsCard />
      {prompts.isPending ? <FormSkeleton /> : prompts.isError || !prompts.data ? (
        <ErrorState message={t("settings.answerPrompts.loadError")} onRetry={() => void prompts.refetch()} />
      ) : (
        <AnswerPromptStages
          stages={prompts.data.stages.filter(isRetrievalPromptStage)}
          titleKey="settings.retrieval.prompts.title"
          descriptionKey="settings.retrieval.prompts.description"
        />
      )}
    </PageBody>
  );
}
