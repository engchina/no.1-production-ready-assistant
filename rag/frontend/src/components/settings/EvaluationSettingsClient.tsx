"use client";

import {
  PageBody,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FormActionBar,
  FormStatus,
  StatusBadge,
  TimedLoadingState,
  FormSkeleton,
} from "@production-ready/ui";
import { useState } from "react";
import { CheckCircle2, ClipboardCheck, RotateCcw, Save } from "lucide-react";

import {
  EVALUATION_PERSPECTIVES,
  EVALUATION_SUITE_NAMES,
  STANDARD_ANSWER_METRICS,
  formatMetricValue,
  metricDescription,
  metricLabel,
  perspectiveDescription,
  perspectiveLabel,
  suiteDescription,
  suiteLabel,
} from "@/components/evaluation/evaluation-metrics";
import { AnswerPromptStages } from "./AnswerPromptStages";

import { ApiErrorState, ErrorState } from "@/components/StateViews";
import {
  ApiError,
  type EvaluationSuiteName,
  type EvaluationSuiteStatusData,
} from "@/lib/api";
import { useLeaveGuard } from "@/lib/leave-guard";
import { t } from "@/lib/i18n";
import { useAnswerPrompts, useEvaluationSettings, useUpdateEvaluationSettings } from "@/lib/queries";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

/**
 * 評価の基準（閾値のプリセット）を選ぶ設定画面（#591）。
 *
 * 基準は「標準」「厳格」の 2 つ。選んだ基準の閾値を、検索・回答・応答の判断の 3 つの観点ごとに
 * 指標の意味と一緒に出す（どの指標を見ればよいかを、この画面で分かるようにする）。
 */
export function EvaluationSettingsClient() {
  const query = useEvaluationSettings();
  const prompts = useAnswerPrompts();
  const save = useUpdateEvaluationSettings();
  const [suite, setSuite] = useState<EvaluationSuiteName | null>(null);
  // 直前に取り込んだ server の suite。server の suite が変わったレンダーで、未編集なら選択をそろえる。
  // 背景再取得で同じ値が返っても未保存の選択を上書きしない。effect ではなくレンダー中にそろえ、
  // 取得の直後に中身の無いレンダー（Skeleton と本文の間の空白）を挟まない（#1029）。
  const [base, setBase] = useState<EvaluationSuiteName | null>(null);
  const serverSuite = query.data?.suite ?? null;
  if (serverSuite !== null && serverSuite !== base) {
    setBase(serverSuite);
    if (suite === null || suite === base) setSuite(serverSuite);
  }

  // 未保存の選択があるときは離脱を確認し、保存中は離脱を止める。
  useLeaveGuard(
    Boolean(query.data && suite !== null && suite !== query.data.suite),
    save.isPending
  );

  if (query.isPending) {
    return (
      <PageBody wide>
        <TimedLoadingState
          label={t("settings.loading")}
          operationKey="settings-evaluation-load"
          placement="page"
          testId="settings-evaluation-loading"
        >
          <FormSkeleton />
        </TimedLoadingState>
      </PageBody>
    );
  }

  if (query.isError) {
    return (
      <PageBody wide>
        <ApiErrorState
          error={query.error}
          fallback={t("settings.evaluation.loadError")}
          onRetry={() => void query.refetch()}
        />
      </PageBody>
    );
  }

  const settings = query.data;
  if (!settings || !suite) return null;

  const dirty = suite !== settings.suite;
  const saveError =
    save.error instanceof ApiError ? save.error.message : t("settings.evaluation.saveError");
  const suites = orderedSuites(settings.suites);
  const selectedSuite = suites.find((item) => item.name === suite);

  function selectSuite(next: EvaluationSuiteName) {
    save.reset();
    setSuite(next);
  }

  function resetForm() {
    save.reset();
    setSuite(settings.suite);
  }

  function submit() {
    if (!suite || save.isPending) return;
    save.mutate(
      { suite },
      {
        onSuccess: (data) => {
          setBase(data.suite);
          setSuite(data.suite);
          // 保存の成功は Toast、失敗は操作の行の FormStatus（messaging.md §10.2）。
          toast.success(t("settings.evaluation.actions.saved"));
        },
      }
    );
  }

  return (
    <PageBody wide>
      <Card>
        <CardHeader>
          <div className="flex items-start gap-3">
            <div data-surface-tint="info" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-info-subtle text-info-fg">
              <ClipboardCheck size={20} aria-hidden />
            </div>
            <div>
              <CardTitle>{t("settings.evaluation.overview.title")}</CardTitle>
              <CardDescription>{t("settings.evaluation.overview.description")}</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <div className="text-sm font-medium text-fg" id="settings-evaluation-suite-label">
              {t("settings.evaluation.suite")}
            </div>
            <div
              role="radiogroup"
              aria-labelledby="settings-evaluation-suite-label"
              className="grid grid-cols-1 gap-2 md:grid-cols-2"
            >
              {suites.map((item) => {
                const selected = suite === item.name;
                return (
                  <div key={item.name} className="relative min-w-0">
                    <input
                      id={`settings-evaluation-suite-${item.name}`}
                      className="peer absolute inset-0 z-10 cursor-pointer opacity-0 disabled:cursor-not-allowed"
                      type="radio"
                      name="settings-evaluation-suite"
                      value={item.name}
                      checked={selected}
                      disabled={save.isPending}
                      onChange={() => selectSuite(item.name)}
                    />
                    <label
                      data-surface-tint={selected ? "accent" : undefined}
                      htmlFor={`settings-evaluation-suite-${item.name}`}
                      className={cn(
                        "block h-full cursor-pointer rounded-md border px-3 py-2 text-left transition-colors peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-focus-ring peer-disabled:cursor-not-allowed peer-disabled:opacity-50",
                        selected
                          ? "border-accent-emphasis bg-accent-subtle text-fg"
                          : "border-border bg-surface text-fg peer-hover:bg-surface-hover"
                      )}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold">{suiteLabel(item.name)}</span>
                        {selected ? (
                          <CheckCircle2 size={16} className="shrink-0 text-accent-fg" aria-hidden />
                        ) : null}
                      </span>
                      <span className="mt-1 block text-xs leading-relaxed text-fg-muted">
                        {suiteDescription(item.name)}
                      </span>
                    </label>
                  </div>
                );
              })}
            </div>
          </div>
          <SuiteMetrics suite={selectedSuite ?? null} />
          {/* 変更が実行済みの評価に効かないことと、実行ごとの指定が優先することを案内する（#1029）。 */}
          <FormStatus tone="info" message={t("settings.evaluation.applyHint")} />
          <FormActionBar
            ariaLabel={t("settings.evaluation.actions.label")}
            primaryActions={[
              {
                id: "save",
                label: t("settings.evaluation.actions.save"),
                icon: Save,
                loading: save.isPending,
                disabled: !dirty,
                onClick: submit,
              },
            ]}
            secondaryActions={[
              {
                id: "reset",
                label: t("settings.evaluation.actions.reset"),
                icon: RotateCcw,
                disabled: !dirty || save.isPending,
                onClick: resetForm,
              },
            ]}
            status={
              save.isError ? (
                <FormStatus tone="danger" message={saveError} />
              ) : dirty ? (
                <FormStatus tone="warning" message={t("settings.evaluation.actions.unsaved")} />
              ) : null
            }
          />
        </CardContent>
      </Card>
      {prompts.isPending ? (
        <TimedLoadingState label={t("settings.loading")} operationKey="settings-evaluation-prompts-load">
          <FormSkeleton />
        </TimedLoadingState>
      ) : prompts.isError || !prompts.data ? (
        <ErrorState message={t("settings.answerPrompts.loadError")} onRetry={() => void prompts.refetch()} />
      ) : (
        <AnswerPromptStages
          stages={prompts.data.stages.filter((stage) => stage.id === "evaluation")}
          titleKey="settings.evaluation.prompts.title"
          descriptionKey="settings.evaluation.prompts.description"
        />
      )}
    </PageBody>
  );
}

/** 選んだ基準の閾値を、観点ごとに指標の意味と一緒に出す。 */
function SuiteMetrics({ suite }: { suite: EvaluationSuiteStatusData | null }) {
  if (!suite) return null;
  return (
    <section
      aria-labelledby="settings-evaluation-metrics-title"
      className="rounded-md border border-border bg-surface-sunken p-3"
      data-testid="settings-evaluation-metrics"
    >
      <h2 id="settings-evaluation-metrics-title" className="text-sm font-semibold text-fg">
        {t("settings.evaluation.metrics.title", { suite: suiteLabel(suite.name) })}
      </h2>
      <p className="mt-1 text-xs text-fg-muted">{t("settings.evaluation.metrics.description")}</p>
      <div className="mt-3 grid gap-4 lg:grid-cols-2">
        {EVALUATION_PERSPECTIVES.map((perspective) => (
          <div key={perspective.id} className={cn("min-w-0", perspective.id === "decision" && "lg:col-start-2")}>
            <h3 className="text-sm font-semibold text-fg">{perspectiveLabel(perspective.id)}</h3>
            <p className="text-xs text-fg-muted">{perspectiveDescription(perspective.id)}</p>
            <dl className="mt-2 space-y-2">
              {perspective.metrics.map((metric) => (
                <div
                  key={metric}
                  className="rounded-md border border-border bg-surface px-3 py-2"
                  data-testid={`settings-evaluation-metric-${metric}`}
                >
                  <dt className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm font-medium text-fg">{metricLabel(metric)}</span>
                    <span className="tnum text-sm font-semibold text-accent-fg">
                      {t("evaluation.metric.threshold", {
                        value: formatMetricValue(suite.thresholds[metric] ?? null),
                      })}
                    </span>
                  </dt>
                  <dd className="mt-1 space-y-1.5 text-xs leading-relaxed text-fg-muted">
                    <p>{metricDescription(metric)}</p>
                    {STANDARD_ANSWER_METRICS.has(metric) ? (
                      <StatusBadge
                        variant="info"
                        label={t("evaluation.metric.needsStandardAnswer")}
                      />
                    ) : null}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      </div>
    </section>
  );
}

function orderedSuites(suites: EvaluationSuiteStatusData[]): EvaluationSuiteStatusData[] {
  const byName = new Map(suites.map((item) => [item.name, item]));
  const ordered = EVALUATION_SUITE_NAMES.map((name) => byName.get(name)).filter(
    (item): item is EvaluationSuiteStatusData => Boolean(item)
  );
  return ordered.length ? ordered : suites;
}
