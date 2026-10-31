import { CHAT_PROGRESS_LABELS, historyTracePath } from "../chatProgress";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";

import {
  CopyButton,
  ApiErrorBanner,
  Banner,
  Button,
  ContentActionBar,
  EmptyState,
  OpenConversationAction,
  OpenRunInputAction,
  PageBody,
  PageHeader,
  RunDetailLayout,
  RunUsedModels,
  RunDetailSection,
  RunDetailSkeleton,
  RunFactList,
  RunSummary,
  RunTimelineSection,
  StatusBadge,
  TimedLoadingState,
  chatProgressTimelineStep,
  toast,
  type RunFact,
} from "@production-ready/ui";
import { useAuth } from "@/features/security/AuthProvider";
import { canOpenRoute } from "@/features/security/route-permissions";
import { ApiError, apiGet, isAbortError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { t } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";
import { useRequestScope } from "@/lib/useRequestScope";
import { feedbackRatingLabel, userFeedbackRatingBadgeLabel } from "../feedbackLabels";
import {
  historyCancellation,
  historyConversationHref,
  historyExecutor,
  historyKindDisplay,
  historyOwner,
  historyRunStatus,
  historySqlGenerationHref,
  historyUsageView,
  isCancelledHistory,
} from "../historyManagementState";
import { engineLabel } from "../labels";
import { profileRecordDisplayLabel } from "../profileDisplay";
import type { HistoryItem } from "../types";
import { useNl2SqlTraceProgress } from "../useTraceProgress";
import { ProgressLegacyNotice } from "./ProgressLegacyNotice";

/*
 * 実行履歴の全幅の詳細（`?id=<run_id>`。#1955。UX 契約 run-history.md「詳細」）。
 * 概要 → 処理の経過 → SQL と結果 → フィードバック → 技術的な詳細（畳む）を縦に 1 枚で読む（タブで隠さない）。
 * NL2SQL は回答の文を記録しないので「回答」の節は出さない。
 */

function SqlBlock({ sql, label, testId }: { sql: string; label: string; testId: string }) {
  return (
    // スクロール領域はキーボードでも操作できるよう focus 可能にする（WCAG 2.1.1）。
    <pre
      data-surface="code"
      tabIndex={0}
      role="region"
      aria-label={label}
      className="max-h-96 overflow-auto rounded-md border border-border bg-surface p-4 font-mono text-sm leading-6 text-fg"
      data-testid={testId}
    >
      <code>{sql}</code>
    </pre>
  );
}

function HistoryRunView({ item }: { item: HistoryItem }) {
  const { hasPermission, user } = useAuth();
  // 「会話を開く」（3 製品共通。チャットの往復だけ。会話を見られる人〔system_admin か持ち主〕で、チャットを開ける
  // 権限があるとき。SQL 生成・MCP・品質評価の実行は開く元の会話が無いので出さない。再実行は置かない。#2343）。
  const chatHref = historyConversationHref(item, {
    seesAllUsers: user?.is_system_admin === true,
    canOpenChat: canOpenRoute(APP_ROUTES.chat, hasPermission),
  });
  // 「SQL 生成で開く」（#3309）: 本人の SQL 生成の実行を、質問と条件を入れて SQL 生成の画面に開く（実行はしない）。
  const queryHref = historySqlGenerationHref(item, { canOpenQuery: canOpenRoute(APP_ROUTES.query, hasPermission) });
  const status = historyRunStatus(item);
  const sql = item.executable_sql || item.generated_sql;
  // 処理の経過は実行したジョブの span の木（チャット・SQL 生成と同じ記録・工程の定義。#3226 の案 A）。span は共通の
  // span の表にあり、行の処理の経過の API で 1 回読む（行は終わった実行）。span の無い古い形式の記録は案内の文を出す。
  const progress = useNl2SqlTraceProgress({ targetId: item.id, path: historyTracePath(item.id), active: false });
  const timelineSteps = useMemo(() => progress.steps.map(chatProgressTimelineStep), [progress.steps]);
  // 停止の行（#3348）は SQL・実行結果・評価を持たない（「SQL と結果」の節を出さない）。
  const cancelled = isCancelledHistory(item);
  const executed = !item.generation_only && item.safety_is_safe && !cancelled;

  // モデル・token・モデルの呼び出し（ジョブの中の LLM の呼び出し。RAG の実行の詳細と同じ形・文言。#3543）。計測できない
  // 呼び出しは数を作らず、「未計測の呼び出し」に出どころを出す。
  const usageView = historyUsageView(item);
  const extraFacts: RunFact[] = [
    // 「モデル」「token」の直後（RAG の概要と同じ並び）。
    { id: "modelCalls", label: t("history.detail.modelCalls"), value: usageView.modelCalls },
    ...(usageView.unmeasuredCalls
      ? [{ id: "unmeasuredCalls", label: t("history.detail.unmeasuredCalls"), value: usageView.unmeasuredCalls }]
      : []),
    { id: "engine", label: t("history.detail.engine"), value: engineLabel(item.engine) },
    {
      id: "rows",
      label: t("history.detail.rows"),
      value: executed ? formatNumber(item.result_row_count) : t("history.notExecuted"),
    },
  ];

  const resultFacts: RunFact[] = [];
  if (item.rewritten_question.trim()) {
    resultFacts.push({ id: "rewritten", label: t("history.rewritten"), value: item.rewritten_question, wide: true });
  }
  resultFacts.push({
    id: "rows",
    label: t("history.detail.rows"),
    value: executed ? formatNumber(item.result_row_count) : t("history.notExecuted"),
  });
  if (item.result_columns.length > 0) {
    resultFacts.push({ id: "columns", label: t("history.detail.columns"), value: item.result_columns.join(", "), mono: true, wide: true });
  }

  const hasFeedback = Boolean(item.feedback_rating || item.feedback_comment.trim());
  const processDetails = <RunUsedModels models={(item.usage?.models ?? []).map(value => ({ label: "LLM", value }))} events={progress.events} />;

  return (
    <>
      <RunDetailLayout
        testId="history-detail"
        summary={
          <RunSummary
            testId="history-detail-summary"
            status={status}
            badges={
              item.feedback_rating ? (
                <StatusBadge
                  variant={item.feedback_rating === "good" ? "success" : "warning"}
                  label={userFeedbackRatingBadgeLabel(item.feedback_rating)}
                />
              ) : null
            }
            question={item.question}
            startedAt={item.created_at}
            durationMs={item.elapsed_ms ?? null}
            kind={historyKindDisplay(item)}
            executor={historyExecutor(item)}
            owner={historyOwner(item)}
            cancellation={historyCancellation(item)}
            target={{ label: t("history.detail.target"), value: profileRecordDisplayLabel(item) || "—" }}
            model={usageView.model}
            usage={usageView.usage}
            runId={item.id}
            extraFacts={extraFacts}
            actions={
              // 「会話を開く」（チャット。#2343）と「SQL 生成で開く」（SQL 生成。#3309）は 3 製品共通の部品で、
              // 実行のきっかけでどちらか 1 つだけが出る。
              <>
                <OpenConversationAction
                  href={chatHref}
                  linkComponent={Link}
                  labels={{ label: t("history.action.openChat"), ariaLabel: t("history.action.openChatLabel") }}
                  testId="history-detail-open-chat"
                />
                <OpenRunInputAction
                  href={queryHref}
                  linkComponent={Link}
                  labels={{ label: t("history.action.openQuery"), ariaLabel: t("history.action.openQueryLabel") }}
                  testId="history-detail-open-query"
                />
              </>
            }
            alert={
              status === "blocked" ? (
                <div data-testid="history-detail-blocked">
                  <Banner severity="warning" title={t("history.detail.blocked.title")}>
                    {t("history.detail.blocked.body")}
                  </Banner>
                </div>
              ) : null
            }
            labels={{ self: t("history.self") }}
          />
        }
        timeline={
          timelineSteps.length > 0 || progress.legacy || progress.partial || processDetails ? (
            // 処理の経過は実行の span の木（#3226）。span の無い古い形式の記録は案内の文、書けなかった span がある
            // 記録は一部だけの記録の案内を出す（処理の経過の中。成功と見なさない）。
            <RunTimelineSection
              traceId={progress.traceId}
              title={t("history.detail.timeline")}
              steps={progress.legacy ? [] : timelineSteps}
              empty={progress.legacy && !progress.partial ? <ProgressLegacyNotice testId="history-detail-timeline-legacy" /> : undefined}
              // 表に書けなかった span がある（#3226。3 製品共通の案内を段の一覧の後ろに出す）。
              partial={progress.partial}
              labels={{ stepStatus: CHAT_PROGRESS_LABELS.status }}
              totalDurationMs={item.elapsed_ms ?? null}
              processDetailsProps={{ metadata: { conversationId: item.kind === "chat" ? item.session_id ?? null : null, messageId: item.id }, title: t("history.detail.technical"), testId: "history-detail-technical", summaryTestId: "history-detail-process-details" }}
              processDetails={processDetails}
              testId="history-detail-timeline"
            />
          ) : null
        }
        evidence={
          cancelled ? null : (
          <RunDetailSection
            title={t("history.detail.sqlSection")}
            testId="history-detail-sql"
          >
            {sql.trim() ? (
              <>
                <ContentActionBar ariaLabel={t("history.detail.sqlActions")} testId="history-detail-sql-actions">
                  <CopyButton
                    size="sm"
                    text={sql}
                    label={t("history.action.copySql")}
                    copiedMessage={t("history.copied")}
                    failedMessage={t("history.copyFailed")}
                  />
                </ContentActionBar>
                <SqlBlock sql={sql} label={t("history.detail.sqlRegion")} testId="history-detail-sql-code" />
              </>
            ) : (
              <p className="text-sm text-fg-muted">{t("history.detail.noSql")}</p>
            )}
            <RunFactList facts={resultFacts} testId="history-detail-result" />
          </RunDetailSection>
          )
        }
        feedback={
          hasFeedback ? (
            <RunDetailSection title={t("history.detail.feedback")} testId="history-detail-feedback">
              <RunFactList
                facts={[
                  { id: "rating", label: t("history.column.rating"), value: feedbackRatingLabel(item.feedback_rating) },
                  ...(item.feedback_comment.trim()
                    ? [{ id: "comment", label: t("history.detail.feedbackComment"), value: item.feedback_comment, wide: true }]
                    : []),
                ]}
              />
            </RunDetailSection>
          ) : null
        }
      />
    </>
  );
}

export function HistoryRunDetail({ historyId, onBack }: { historyId: string; onBack: () => void }) {
  const [item, setItem] = useState<HistoryItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [announce, setAnnounce] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const sequence = useRef(0);
  const { abortAll, run } = useRequestScope();

  const fetchItem = (notify: boolean) => {
    const current = sequence.current + 1;
    sequence.current = current;
    return run(async (signal) => {
      const data = await apiGet<HistoryItem>(`/api/nl2sql/history/${encodeURIComponent(historyId)}`, { signal });
      if (signal.aborted || current !== sequence.current) return;
      setItem(data);
      setError(null);
      if (notify) toast.success(t("common.action.refreshed"));
    })
      .catch((err: unknown) => {
        if (isAbortError(err) || current !== sequence.current) return;
        setError(err);
      })
      .finally(() => {
        if (current === sequence.current) setLoading(false);
      });
  };
  const fetchRef = useRef(fetchItem);
  useLayoutEffect(() => {
    fetchRef.current = fetchItem;
  });
  useEffect(() => {
    void fetchRef.current(false);
    return () => {
      sequence.current += 1;
      abortAll();
    };
  }, [historyId, abortAll]);

  const refresh = () => {
    setLoading(true);
    setAnnounce(true);
    void fetchItem(true);
  };

  const notFound = error instanceof ApiError && (error.status === 404 || error.status === 403);

  return (
    <>
      <PageHeader
        wide
        title={t("history.detail.title")}
        back={{
          label: t("history.detail.back"),
          ariaLabel: t("history.detail.backAria"),
          onClick: onBack,
          testId: "history-detail-back",
        }}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            onClick: refresh,
            loading: loading && announce,
            disabled: loading && !announce,
          },
        ]}
      />
      <PageBody wide>
        {loading && !item ? (
          <TimedLoadingState
            label={t("history.detail.loading")}
            operationKey={`history-detail-${historyId}`}
            placement="panel"
            testId="history-detail-loading"
            framed={false}
            activityIcon="spinner"
          >
            <RunDetailSkeleton />
          </TimedLoadingState>
        ) : notFound ? (
          <div data-testid="history-detail-not-found">
            <EmptyState
              title={t("history.detail.notFound.title")}
              hint={t("history.detail.notFound.hint")}
              action={
                <Button type="button" variant="secondary" size="sm" onClick={onBack}>
                  {t("history.detail.back")}
                </Button>
              }
            />
          </div>
        ) : (
          <div className="grid min-w-0 gap-4">
            {error !== null ? (
              <ApiErrorBanner
                error={error}
                fallback={t("history.error.detail")}
                testId="history-detail-error"
                action={
                  <Button type="button" variant="secondary" size="sm" icon={RefreshCw} loading={loading} onClick={refresh}>
                    {t("common.retry")}
                  </Button>
                }
              />
            ) : null}
            {item ? <HistoryRunView item={item} /> : null}
          </div>
        )}
      </PageBody>
    </>
  );
}
