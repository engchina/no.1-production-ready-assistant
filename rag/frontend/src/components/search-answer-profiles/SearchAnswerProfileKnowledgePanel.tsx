import { Save } from "lucide-react";
import { useMemo, useState } from "react";

import {
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Skeleton,
  Tabs,
  TextareaField,
  toast,
  TimedLoadingState,
} from "@production-ready/ui";

import { ErrorState } from "@/components/StateViews";
import { ApiError } from "@/lib/api";
import { t } from "@/lib/i18n";
import { confirmPendingLeave, useLeaveGuard } from "@/lib/leave-guard";
import { useValuesChanged } from "@/lib/render-sync";
import {
  initialLoadError,
  useDomainKeywords,
  useSaveDomainKeywords,
  useImportDomainKeywords,
} from "@/lib/queries";

import { QuestionUnderstandingPanel } from "./QuestionUnderstandingPanel";
import { DomainKeywordExcelImport } from "./DomainKeywordExcelImport";
import { ApprovedFaqManager } from "./ApprovedFaqManager";
import { RuntimeKnowledgeManager, runtimeKindLabel } from "./RuntimeKnowledgeManager";
import { SupportGuideManager } from "./SupportGuideManager";

type KnowledgeTab = "approvedFaq" | "questionUnderstanding" | "terms" | "domainKeywords" | "rules" | "supportGuides";

/** 検索・回答プロファイル単位の知識(ドメインキーワード等)。編集中の検索・回答プロファイルにだけ表示する。 */
export function SearchAnswerProfileKnowledgePanel({
  searchAnswerProfileId,
  readOnly = false,
}: {
  searchAnswerProfileId: string;
  readOnly?: boolean;
}) {
  // よく使う Approved FAQ を先頭・既定のタブにする(#636)。
  const [tab, setTab] = useState<KnowledgeTab>("approvedFaq");
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("searchAnswerProfiles.knowledge.title")}</CardTitle>
        <CardDescription>
          {t("searchAnswerProfiles.knowledge.description")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <Tabs
          idPrefix="search-answer-profile-knowledge"
          ariaLabel={t("searchAnswerProfiles.knowledge.title")}
          value={tab}
          onChange={async (value) => {
            // 業務ガイドは編集中の入力が多いので、タブを離れる前に未保存の変更を確かめる。
            if (!(await confirmPendingLeave())) return;
            setTab(value as KnowledgeTab);
          }}
          // 最初の主な利用段階の順。分岐や後の再利用で実行する段階は変わる。
          items={[
            { id: "approvedFaq", label: t("searchAnswerProfiles.faq.title") },
            { id: "questionUnderstanding", label: t("searchAnswerProfiles.questionUnderstanding.title") },
            { id: "terms", label: runtimeKindLabel("terms") },
            { id: "rules", label: runtimeKindLabel("rules") },
            { id: "supportGuides", label: t("supportGuides.title") },
            { id: "domainKeywords", label: t("searchAnswerProfiles.domainKeywords.title") },
          ]}
        />
        <div
          role="tabpanel"
          id={`search-answer-profile-knowledge-panel-${tab}`}
          aria-labelledby={`search-answer-profile-knowledge-tab-${tab}`}
        >
          {tab === "questionUnderstanding" ? (
            <QuestionUnderstandingPanel key={searchAnswerProfileId} searchAnswerProfileId={searchAnswerProfileId} readOnly={readOnly} />
          ) : tab === "domainKeywords" ? (
            <DomainKeywordsEditor searchAnswerProfileId={searchAnswerProfileId} />
          ) : tab === "approvedFaq" ? (
            <ApprovedFaqManager searchAnswerProfileId={searchAnswerProfileId} />
          ) : tab === "supportGuides" ? (
            <SupportGuideManager searchAnswerProfileId={searchAnswerProfileId} />
          ) : (
            // 種類ごとに編集中の入力を持つので、タブを切り替えたら作り直す。
            <RuntimeKnowledgeManager key={tab} searchAnswerProfileId={searchAnswerProfileId} kind={tab} />
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function parseKeywords(text: string): string[] {
  return [
    ...new Set(
      text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
    ),
  ];
}

function DomainKeywordsEditor({ searchAnswerProfileId }: { searchAnswerProfileId: string }) {
  const query = useDomainKeywords(searchAnswerProfileId);
  const save = useSaveDomainKeywords(searchAnswerProfileId);
  const importKeywords = useImportDomainKeywords(searchAnswerProfileId);
  const [text, setText] = useState("");
  const saved = useMemo(
    () => query.data?.keywords ?? [],
    [query.data?.keywords],
  );

  // 保存済みの語句が変わったレンダーで、編集欄を保存値に戻す。
  const savedChanged = useValuesChanged([saved]);
  if (savedChanged) {
    setText(saved.join("\n"));
  }

  const current = parseKeywords(text);
  const dirty = current.join("\n") !== saved.join("\n");
  useLeaveGuard(dirty);

  if (query.isPending) {
    return (
      <TimedLoadingState
        label={t("searchAnswerProfiles.knowledge.loading")}
        operationKey="search-answer-profile-knowledge-load"
        testId="search-answer-profile-knowledge-loading"
      >
        <Skeleton className="h-40 w-full" />
      </TimedLoadingState>
    );
  }

  // 初回の読み込みに失敗したら、空の編集欄を出さない（保存は全置換なので、空から保存すると登録済みの語を消す）。
  const loadError = initialLoadError(query);
  if (loadError) {
    return (
      <ErrorState
        message={
          loadError instanceof ApiError
            ? loadError.message
            : t("searchAnswerProfiles.domainKeywords.loadError")
        }
        onRetry={() => void query.refetch()}
      />
    );
  }

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="min-w-0 space-y-2">
        <TextareaField
          id="domain-keywords-editor"
          width="full"
          label={t("searchAnswerProfiles.domainKeywords.editorLabel")}
          helper={t("searchAnswerProfiles.domainKeywords.help")}
          value={text}
          onChange={(event) => setText(event.target.value)}
          rows={12}
          placeholder={t("searchAnswerProfiles.domainKeywords.placeholder")}
          disabled={save.isPending || importKeywords.isPending}
        />
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            icon={Save}
            loading={save.isPending}
            disabled={!dirty || importKeywords.isPending}
            onClick={() =>
              save.mutate(current, {
                onSuccess: (data) =>
                  toast.success(
                    t("searchAnswerProfiles.domainKeywords.saved", {
                      count: data.keywords.length,
                    }),
                  ),
                onError: (error) =>
                  toast.error(
                    error instanceof ApiError
                      ? error.message
                      : t("searchAnswerProfiles.domainKeywords.saveError"),
                  ),
              })
            }
          >
            {t("searchAnswerProfiles.domainKeywords.save")}
          </Button>
          <span className="tnum text-xs text-fg-muted">
            {t("searchAnswerProfiles.domainKeywords.count", { count: current.length })}
          </span>
        </div>
      </div>
      <DomainKeywordExcelImport
        searchAnswerProfileId={searchAnswerProfileId}
        disabled={dirty || save.isPending}
        dirty={dirty}
        pending={importKeywords.isPending}
        onImport={async (file, mode) => {
          const data = await importKeywords.mutateAsync({ file, mode });
          toast.success(t("searchAnswerProfiles.domainKeywords.imported", {
            count: data.keywords.length,
            inserted: data.inserted_count,
          }));
        }}
      />
    </div>
  );
}
