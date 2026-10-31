import { useState } from "react";
import { History } from "lucide-react";
import { Link } from "react-router-dom";
import {
  Button,
  ButtonLink,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  EmptyState,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  ListDetailLayout,
  ListDetailPager,
  ListToolbar,
  OffsetPagination,
  PAGE_SIZE_OPTIONS,
  RowTitleButton,
  Section,
  Skeleton,
  StatusBadge,
  TableSkeleton,
  type DataTableColumn,
  type StatusVariant,
  adjacentListItems,
} from "@production-ready/ui";

import { AnswerBody } from "@/components/chat/ResultTables";
import { AGENT_LIST_DETAIL_STORAGE_PREFIX } from "@/components/EntityLayout";
import { listCount } from "@/components/ListFilters";
import { agentPaginationLabels, listScrollLabel } from "@/components/ListViews";
import { ReportSourceNote } from "@/components/ReportSourceNote";
import { AddToEvaluationCase, useCanEditEvaluationSets } from "@/components/evaluation/AddToEvaluationCase";
import type { RunFeedback, SignalItem, SignalKind, SignalReport, SignalSummary } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { useCapabilities } from "@/lib/permissions";
import { APP_ROUTES } from "@/lib/routes";
import { runStatusView } from "@/lib/status-labels";
import { countDelta, formatRate, Metric, rateDelta } from "@/pages/feedbackMetrics";

// 確かめるべき実行（暗黙のシグナル。#1822）。評価の付いていない実行も、回答の検証・回答の対応・ツール・承認・
// 実行の状態から集め、評価ケースに足して直す流れ（feedback → triage → eval → fix）の入口にする。
// シグナルの定義は backend の `run_signals.py`。状態は色だけで表さない（バッジはアイコンと文言を持つ）。

/** シグナル → バッジの色（ドメインの値 → variant の対応表は製品が持つ。ARCHITECTURE §4）。 */
const SIGNAL_VARIANT: Record<SignalKind, StatusVariant> = {
  not_helpful: "danger",
  validation_failed: "warning",
  unvalidated: "neutral",
  ungrounded_no_tools: "warning",
  unanswered: "warning",
  approval_rejected: "neutral",
  tool_failed: "danger",
  run_failed: "danger",
};

/** 回答の対応（#1305）の表示名。チャットの回答の詳細と同じ文言。 */
const OUTCOME_LABEL: Record<string, I18nKey> = {
  insufficient_evidence: "chat.review.outcome.insufficientEvidence",
  needs_human: "chat.review.outcome.needsHuman",
};

export function signalLabel(kind: SignalKind): string {
  return t(`feedback.signal.${kind}` as I18nKey);
}

function signalHint(kind: SignalKind): string {
  return t(`feedback.signalHint.${kind}` as I18nKey);
}

function SignalBadge({ kind }: { kind: SignalKind }) {
  return <StatusBadge variant={SIGNAL_VARIANT[kind]} label={signalLabel(kind)} />;
}

export function SignalsSkeleton() {
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="grid grid-cols-2 gap-4 pt-4 xl:grid-cols-3">
          {Array.from({ length: 3 }, (_, index) => (
            <Skeleton key={index} className="h-16" />
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardContent className="pt-4">
          <TableSkeleton columns={6} />
        </CardContent>
      </Card>
    </div>
  );
}

export function SignalsContent({
  report,
  filtered,
  onClearFilters,
  selectedId,
  onSelect,
  onPageChange,
  onPageSizeChange,
}: {
  report: SignalReport;
  filtered: boolean;
  onClearFilters: () => void;
  /** 選んだ実行（URL の `?signal=`。#2283）。 */
  selectedId: string | null;
  onSelect: (runId: string) => void;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}) {
  const { summary, previous } = report;
  // 選んだ実行が今のページに無い（絞り込み・ページ送りで外れた）ときは、最後に見た内容を出したままにし、
  // 別の行へ置き換えない（page-archetypes.md §1 B「選択」）。
  const found = report.items.find((item) => item.run_id === selectedId) ?? null;
  const [lastSeen, setLastSeen] = useState<SignalItem | null>(found);
  if (found && found !== lastSeen) setLastSeen(found);
  const selected = found ?? (lastSeen && lastSeen.run_id === selectedId ? lastSeen : null);
  const { previous: previousItem, next: nextItem } = adjacentListItems(report.items, selectedId, (item) => item.run_id);
  if (summary.runs === 0 && !selectedId) {
    return (
      <Card>
        <CardContent className="pt-4">
          <EmptyState title={t("feedback.signals.empty.title")} hint={t("feedback.signals.empty.hint")} />
          <ReportSourceNote source={report.source} />
        </CardContent>
      </Card>
    );
  }
  return (
    <div className="space-y-4">
      <Card className={selectedId ? "hidden" : undefined}>
        <CardHeader>
          <CardTitle>{t("feedback.signals.summary.title")}</CardTitle>
          <CardDescription>{t("feedback.signals.summary.description")}</CardDescription>
          <ReportSourceNote source={report.source} />
        </CardHeader>
        <CardContent className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
          <div className="grid grid-cols-2 gap-x-5 gap-y-4 self-start sm:grid-cols-3 lg:grid-cols-2" data-testid="signals-summary">
            <Metric
              label={t("feedback.signals.summary.flagged")}
              value={formatNumber(summary.flagged)}
              delta={countDelta(summary.flagged, previous.flagged)}
            />
            <Metric
              label={t("feedback.signals.summary.flaggedRate")}
              value={formatRate(summary.flagged_rate)}
              delta={rateDelta(summary.flagged_rate, previous.flagged_rate, t("feedback.signals.summary.noPrevious"))}
            />
            <Metric
              label={t("feedback.signals.summary.runs")}
              value={formatNumber(summary.runs)}
              delta={countDelta(summary.runs, previous.runs)}
            />
          </div>
          <SignalCounts summary={summary} previous={previous} />
        </CardContent>
      </Card>

      {report.total === 0 && !selectedId ? (
        <Card className="min-w-0">
          <CardContent className="pt-4">
            {filtered ? (
              <EmptyState
                title={t("feedback.signals.noMatch.title")}
                action={
                  <Button variant="secondary" size="sm" onClick={onClearFilters}>
                    {t("feedback.noMatch.clear")}
                  </Button>
                }
              />
            ) : (
              <EmptyState title={t("feedback.signals.none.title")} hint={t("feedback.signals.none.hint")} />
            )}
          </CardContent>
        </Card>
      ) : (
        // 質問とシグナルを全幅の一覧で比較し、選んだ実行は全幅の詳細で読む。
        <ListDetailLayout
          id="feedback-signals"
          layout="single"
          listVariant="table"
          listLabel={t("feedback.signals.list.title")}
          detailLabel={t("feedback.signals.detail.title")}
          storagePrefix={AGENT_LIST_DETAIL_STORAGE_PREFIX}
          listHeader={
            <Section title={t("feedback.signals.list.title")}>
              {/* 件数: 種類の絞り込み中は「N 件（全 M 件）」。M は期間・業務 Agent の確かめるべき実行の全件（#1891）。 */}
              <ListToolbar count={listCount(summary.flagged, report.total, filtered)} testId="signals-list-toolbar" />
            </Section>
          }
          list={
            report.items.length ? (
              <SignalList items={report.items} selectedId={selectedId} onSelect={onSelect} />
            ) : (
              <div className="p-3">
                <EmptyState title={t("feedback.signals.noMatch.title")} />
              </div>
            )
          }
          listFooter={
            <OffsetPagination
              offset={report.offset}
              limit={report.limit}
              total={report.total}
              count={report.items.length}
              onPageChange={(next) => onPageChange(next)}
              onPageSizeChange={onPageSizeChange}
              pageSizeOptions={PAGE_SIZE_OPTIONS}
              labels={agentPaginationLabels()}
              ariaLabel={t("feedback.signals.pagerLabel")}
              testId="signals-pagination"
            />
          }
          hasSelection={selectedId !== null}
          detail={
            selected ? (
              <SignalDetail item={selected} />
            ) : (
              <EmptyState
                title={t("feedback.signals.detail.notInListTitle")}
                hint={t("feedback.signals.detail.notInListHint")}
              />
            )
          }
          emptyDetail={
            <EmptyState title={t("feedback.signals.detail.emptyTitle")} hint={t("feedback.signals.detail.emptyHint")} />
          }
          detailToolbar={
            selectedId ? (
              <ListDetailPager
                onPrevious={previousItem ? () => onSelect(previousItem.run_id) : null}
                onNext={nextItem ? () => onSelect(nextItem.run_id) : null}
                labels={{ previous: t("listDetail.previous"), next: t("listDetail.next") }}
                testId="signals-detail-pager"
              />
            ) : undefined
          }
          labels={{ resize: t("listDetail.resize"), resizeHint: t("listDetail.resizeHint") }}
        />
      )}
    </div>
  );
}

/** 種類ごとの件数（確かめるべき実行に占める割合の棒と、前の期間からの増減）。 */
function SignalCounts({ summary, previous }: { summary: SignalSummary; previous: SignalSummary }) {
  const before = new Map(previous.counts.map((item) => [item.kind, item.count]));
  return (
    <section aria-labelledby="signals-kinds-heading" className="min-w-0" data-testid="signals-kinds">
      <h3 id="signals-kinds-heading" className="text-sm font-semibold text-fg">
        {t("feedback.signals.kinds.title")}
      </h3>
      <p className="mb-3 mt-0.5 text-xs text-fg-muted">{t("feedback.signals.kinds.note")}</p>
      <ul className="space-y-2">
        {summary.counts.map((item) => {
          const ratio = summary.flagged ? item.count / summary.flagged : 0;
          return (
            <li
              key={item.kind}
              className="grid grid-cols-[minmax(7rem,1fr)_minmax(3rem,1fr)_auto] items-center gap-x-2 gap-y-0.5 text-xs sm:grid-cols-[minmax(7rem,1fr)_minmax(3rem,1fr)_auto_minmax(7rem,auto)]"
              data-testid={`signals-kind-${item.kind}`}
            >
              <span className="truncate text-fg" title={signalHint(item.kind)}>
                {signalLabel(item.kind)}
              </span>
              <span data-surface-tint="neutral" className="h-1.5 overflow-hidden rounded-full bg-surface-hover" aria-hidden>
                <span className="block h-full rounded-full bg-danger-emphasis" style={{ width: `${Math.round(ratio * 100)}%` }} />
              </span>
              <span className="text-right tabular-nums text-fg">{formatNumber(item.count)}</span>
              <span className="col-span-3 text-right tabular-nums text-fg-muted sm:col-span-1">
                {countDelta(item.count, before.get(item.kind) ?? 0)}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * 全幅の比較一覧。desktop は質問・シグナル・実行日時を列に分け、狭い画面は質問の下へ補足をまとめる。
 */
function SignalList({
  items,
  selectedId,
  onSelect,
}: {
  items: SignalItem[];
  selectedId: string | null;
  onSelect: (runId: string) => void;
}) {
  const columns: DataTableColumn<SignalItem>[] = [
    {
      key: "question",
      header: t("feedback.column.question"),
      rowHeader: true,
      className: "py-2.5 align-top",
      headerClassName: "lg:w-[50%]",
      render: (item) => (
        <RowTitleButton
          title={item.question}
          maxLines={2}
          subtitle={
            <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 lg:hidden">
              {item.signals.map((kind) => (
                <SignalBadge key={kind} kind={kind} />
              ))}
              <span className="tabular-nums">{formatDateTime(item.created_at)}</span>
            </span>
          }
          current={item.run_id === selectedId}
          aria-label={t("feedback.list.selectNamed", { name: item.question })}
          onClick={() => onSelect(item.run_id)}
        />
      ),
    },
    {
      key: "signals",
      header: t("feedback.signals.column.signals"),
      headerClassName: "hidden lg:table-cell lg:w-[32%]",
      className: "hidden lg:table-cell align-top",
      render: (item) => (
        <span className="flex flex-wrap gap-1">
          {item.signals.map((kind) => <SignalBadge key={kind} kind={kind} />)}
        </span>
      ),
    },
    {
      key: "created_at",
      header: t("feedback.signals.column.createdAt"),
      headerClassName: "hidden lg:table-cell lg:w-[18%]",
      className: "hidden lg:table-cell align-top tabular-nums",
      render: (item) => formatDateTime(item.created_at),
    },
  ];
  return (
    <DataTable<SignalItem>
      rows={items}
      columns={columns}
      getRowKey={(item) => item.run_id}
      onRowClick={(item) => onSelect(item.run_id)}
      selectedRowKey={selectedId}
      rowProps={(item) => ({ className: INFORMATION_TABLE_ROW_CLASS, "data-testid": `signals-row-${item.run_id}` })}
      ariaLabel={t("feedback.signals.list.label")}
      scrollAriaLabel={listScrollLabel(t("feedback.signals.list.label"))}
      stickyHeader
      visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
      tableClassName="w-full table-fixed"
    />
  );
}

/** シグナルの補足（件数・回答の対応）。 */
function signalDetail(item: SignalItem, kind: SignalKind): string | null {
  if (kind === "tool_failed") return t("feedback.signals.detail.toolFailures", { count: formatNumber(item.tool_failures) });
  if (kind === "approval_rejected") {
    return t("feedback.signals.detail.approvalsRejected", { count: formatNumber(item.approvals_rejected) });
  }
  if (kind === "unanswered" && item.answer_outcome && OUTCOME_LABEL[item.answer_outcome]) {
    return t("feedback.signals.detail.outcome", { label: t(OUTCOME_LABEL[item.answer_outcome]) });
  }
  return null;
}

function SignalDetail({ item }: { item: SignalItem }) {
  const capabilities = useCapabilities();
  const canAddCase = useCanEditEvaluationSets();
  const status = runStatusView(item.status);
  return (
    <div className="space-y-4 text-sm" data-testid="signals-detail">
      <h2 className="text-base font-semibold text-fg">{t("feedback.signals.detail.title")}</h2>
      <dl className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
        <dt className="text-fg-muted">{t("feedback.column.agent")}</dt>
        <dd className="break-words text-fg">{item.agent_name || t("feedback.agentDeleted")}</dd>
        <dt className="text-fg-muted">{t("feedback.column.user")}</dt>
        <dd className="break-words text-fg">{item.display_name || t("feedback.userUnknown")}</dd>
        <dt className="text-fg-muted">{t("feedback.signals.column.createdAt")}</dt>
        <dd className="tabular-nums text-fg">{formatDateTime(item.created_at)}</dd>
        <dt className="text-fg-muted">{t("feedback.signals.column.status")}</dt>
        <dd>
          <StatusBadge variant={status.variant} label={status.label} />
        </dd>
        <dt className="text-fg-muted">{t("feedback.detail.run")}</dt>
        <dd className="break-all font-mono text-fg">{item.run_id}</dd>
      </dl>
      <section className="space-y-2" data-testid="signals-detail-signals">
        <h3 className="text-xs font-semibold text-fg-muted">{t("feedback.signals.column.signals")}</h3>
        <ul className="space-y-2">
          {item.signals.map((kind) => {
            const detail = signalDetail(item, kind);
            return (
              <li key={kind} className="space-y-1">
                <SignalBadge kind={kind} />
                <p className="text-xs text-fg">{signalHint(kind)}</p>
                {detail ? <p className="text-xs text-fg-muted">{detail}</p> : null}
              </li>
            );
          })}
        </ul>
      </section>
      <section className="space-y-1">
        <h3 className="text-xs font-semibold text-fg-muted">{t("feedback.detail.question")}</h3>
        <p data-surface-tint="accent" className="whitespace-pre-wrap break-words rounded-md bg-accent-subtle px-3 py-2 text-fg">
          {item.question}
        </p>
      </section>
      {item.answer ? (
        <section className="space-y-1">
          <h3 className="text-xs font-semibold text-fg-muted">{t("feedback.detail.answer")}</h3>
          <div className="rounded-md border border-border p-3">
            <AnswerBody text={item.answer} className="text-sm text-fg" />
          </div>
        </section>
      ) : null}
      <RatingNote title={t("feedback.detail.ownerRating")} rating={item.feedback} />
      <RatingNote title={t("feedback.detail.adminReview")} rating={item.admin_review} />
      {capabilities.viewRuns ? (
        <ButtonLink
          to={`${APP_ROUTES.runs}?id=${encodeURIComponent(item.run_id)}`}
          linkComponent={Link}
          variant="secondary"
          icon={History}
          testId="signals-open-run"
        >
          {t("feedback.signals.detail.openRun")}
        </ButtonLink>
      ) : null}
      {/* 確かめるべき実行は、どのシグナルでも品質評価の評価ケースにできる（品質評価の権限。#810）。 */}
      {canAddCase ? <AddToEvaluationCase key={item.run_id} runId={item.run_id} testId="signals-add-case" /> : null}
    </div>
  );
}

/** 評価があるときだけ出す（評価の操作はフィードバックの「利用者の評価」と実行の詳細で行う）。 */
function RatingNote({ title, rating }: { title: string; rating: RunFeedback | null }) {
  if (!rating) return null;
  return (
    <section className="space-y-1">
      <h3 className="text-xs font-semibold text-fg-muted">{title}</h3>
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge
          variant={rating.rating === "helpful" ? "success" : "danger"}
          label={t(`feedback.rating.${rating.rating}` as I18nKey)}
        />
        {rating.reason ? <span className="text-fg">{t(`feedback.reason.${rating.reason}` as I18nKey)}</span> : null}
      </div>
      {rating.comment ? <p className="whitespace-pre-wrap break-words text-fg">{rating.comment}</p> : null}
    </section>
  );
}
