import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { FileText, PlayCircle, RefreshCw } from "lucide-react";
import {
  AnswerPassageBadges,
  AnswerNotice,
  Banner,
  Button,
  ChatSqlBlock,
  Disclosure,
  EmptyState,
  FormStatus,
  ListSkeleton,
  ObjectActionBar,
  OpenConversationAction,
  ProcessFailureNotice,
  RunDetailLayout,
  RunUsedModels,
  RunDetailSection,
  RunEventLog,
  RunJobProgress,
  RunSummary,
  RunTimelineSection,
  SqlSafetyBadge,
  StatusBadge,
  runStepDetailsJson,
  runTimelineCurrentStepIndex,
  useTraceDetailsLabels,
  type EntityAction,
  type RunEventLogItem,
  type RunTimelineStep,
} from "@production-ready/ui";
import {
  agentApi,
  type Artifact,
  type RunAuditData,
  type RunState,
  type RunSummary as RunSummaryData,
  type ToolAuditRecord,
} from "@/lib/api";
import { QueryState, listCountLabels } from "@/components/ListViews";
import { useAuth } from "@/components/security/AuthProvider";
import { toolProgressTable } from "@/components/chat/ToolProgressTable";
import { AnswerFeedback } from "@/components/chat/AnswerFeedback";
import { OutcomeBadge } from "@/components/chat/AnswerReview";
import { AnswerBody, ResultTable } from "@/components/chat/ResultTables";
import { AddToEvaluationCase, useCanEditEvaluationSets } from "@/components/evaluation/AddToEvaluationCase";
import { ANSWER_KIND, ANSWER_VALIDATION_KIND, answerOutcome, runAnswerPassages } from "@/lib/answer-review";
import { formatNumber } from "@/lib/format";
import { t } from "@/lib/i18n";
import { processFailureLabels, runFailure } from "@/lib/process-failure";
import { MENU_PERMISSIONS, type AgentCapabilities } from "@/lib/permissions";
import { runAnswerEvidence } from "@/lib/answer-evidence";
import { answerNoticeText, internalRecordArtifacts, userOutputArtifacts } from "@/lib/run-artifacts";
import { answerPassageLabels, answerStructureTitles, noticeHeading, structureAnswer } from "@/lib/answer-structure";
import { AnswerEvidenceBlock, StructuredAnswer } from "@/components/chat/AnswerEvidenceView";
import {
  agentRunDetailLabels,
  commonRunStatus,
  runCancellation,
  runDurationMs,
  runConversationHref,
  runFinishedAt,
  runOutcomeUnknownWrites,
  runStatusLabel,
  runTimelineSteps,
  runToolTitles,
} from "@/lib/run-history";
import { useRunTraceProgress } from "@/lib/run-trace-progress";
import { runSourceLabelOf } from "@/lib/run-source";
import { artifactTable, runToolResultTables, stepResultTable, type ToolResultTable } from "@/lib/run-tables";
import {
  approvalStatusView,
  artifactKindView,
  permissionView,
  policyDecisionView,
  runStatusView,
  stepStatusView,
} from "@/lib/status-labels";
import { type RunEventSourceState } from "@/pages/runs/run-event-stream";
import { JsonPreview, MetricPill, formatDate } from "@/pages/shared/page-helpers";

/** Run の取消・再開の可否。一覧の行メニューと詳細の ObjectActionBar で同じ判定を使う。 */
export function runCapabilities(run: RunState): {
  canCancel: boolean;
  canResume: boolean;
} {
  // 組み込み Runtime（#754）は承認がすべて決まると自動で再開する。手動の再開は、承認が決まり SDK の状態を
  // 保存したまま止まっている Run（待ち）を起動し直すときだけ使う（backend の `builtin_resume_pending`。
  // 旧エンジンの Run の再開は backend が必ず断る。#1119）。
  const resumePending =
    run.runtime_id === "builtin" && run.status === "queued" && typeof run.metadata?._builtin_sdk_state === "string";
  return {
    canCancel: CANCELLABLE_RUN_STATUSES.includes(run.status),
    canResume: resumePending,
  };
}

const CANCELLABLE_RUN_STATUSES: readonly string[] = ["queued", "running", "waiting_approval", "waiting_confirmation"];

/** 一覧の行（要約）での取消・再開の可否（`runCapabilities` と同じ判定。再開の待ちは backend が判定する。#1897）。 */
export function runSummaryCapabilities(run: RunSummaryData): { canCancel: boolean; canResume: boolean } {
  return { canCancel: CANCELLABLE_RUN_STATUSES.includes(run.status), canResume: run.resume_pending };
}

export function RunDetail({
  sseState,
  run,
  agentName,
  actions,
  capabilities,
}: {
  run: RunState;
  /** 業務 Agent の名前（一覧に無いときは ID）。 */
  agentName: string;
  actions: EntityAction[];
  sseState: RunEventSourceState;
  capabilities: AgentCapabilities;
}) {
  // 監査ログは開いたときだけ読む（閲覧の権限のある利用者だけ。#1955 の前はタブを開いたときだけ）。
  const [auditOpen, setAuditOpen] = useState(false);
  const queryClient = useQueryClient();
  const structured = structuredFallback(run);
  const pendingApproval = run.approvals.find((approval) => approval.status === "pending");
  const canAddCase = useCanEditEvaluationSets();
  const { user, hasPermission } = useAuth();
  const conversationHref = runConversationHref(run, {
    seesAllUsers: user?.is_system_admin === true,
    canOpenChat: hasPermission(MENU_PERMISSIONS.chat),
  });
  const labels = agentRunDetailLabels();
  // 実行者と会話の持ち主（#1954）。引けない利用者は「削除済みのユーザー」、自分は自分の表示名で補う。
  const executedByMe = run.executed_by_me !== false;
  const executorName =
    run.executor_display_name || (executedByMe ? user?.display_name : undefined) || t("run.userUnknown");
  const ownerName =
    run.conversation_owner_display_name || (run.owned_by_me ? user?.display_name : undefined) || t("run.userUnknown");
  // 会話の持ち主は、実行者と違うときだけ出す（run-history.md §4）。
  const showOwner =
    Boolean(run.thread_id) &&
    run.owned_by_me !== undefined &&
    run.owned_by_me !== null &&
    (run.owned_by_me !== executedByMe || ownerName !== executorName);
  // 自分では操作できない待ち（system_admin が自分の会話で実行した Run の承認・確認。#1928・#1954）。
  const waitingForExecutor =
    run.can_decide === false && (run.status === "waiting_approval" || run.status === "waiting_confirmation");
  // 最終の回答は「回答」の節（回答の根拠はその下。#2328）。「成果物」の節は利用者が使う出力（表の結果など）だけで、
  // 支援タスクの状態・資料との照合などの内部の記録は「監査ログ」で元の JSON まで見る（#2403）。
  const answers = run.artifacts.filter(isAnswerArtifact);
  const outputs = userOutputArtifacts(run.artifacts);
  const internalRecords = internalRecordArtifacts(run.artifacts);
  const evidence = runAnswerEvidence(run.artifacts);
  // 保存した根拠のうち、今の権限で見られない根拠の数（#3252）。
  const withheldEvidence = run.withheld_evidence_count ?? 0;
  const validationContent =
    [...run.artifacts].reverse().find((artifact) => artifact.kind === ANSWER_VALIDATION_KIND)?.content ?? null;
  // 段落の印・外した段落・回答全体の注意（#2738）。記録の無い過去の Run は null（今までの表示のまま）。
  const passages = runAnswerPassages(run.artifacts);
  const latestAnswer = answers.at(-1);
  const outcome = latestAnswer ? answerOutcome(latestAnswer.content) : null;
  // 処理の経過は Run の trace（span の木。#3226）の投影で、チャットと同じ記録・名前にする。span は共通の span の表に
  // あり（案 A）、終わっていない Run は読み直す。
  const traceLabels = useTraceDetailsLabels();
  const progress = useRunTraceProgress(run, { active: CANCELLABLE_RUN_STATUSES.includes(run.status) });
  const timelineSteps = runTimelineSteps(run, progress.events, toolProgressTable, { traceLabels });
  const legacyProgress = progress.legacy;
  const failure = runFailure(run, timelineSteps, "detail");
  const usage = run.usage;
  const cancellation = runCancellation(run);
  // 結果不明の外部への書き込み（#72）。失敗の文とは別に、照合を求める警告を出す（未実行とは扱わない）。
  const unknownWrites = runOutcomeUnknownWrites(run);
  const toolTitles = runToolTitles(run);

  return (
    <section className="min-w-0" aria-label={t("run.detail")}>
      <RunDetailLayout
        testId="run-detail"
        summary={
          <RunSummary
            status={commonRunStatus(run.status)}
            statusLabel={runStatusLabel(run.status)}
            badges={
              (outcome && latestAnswer) || passages ? (
                <>
                  {outcome && latestAnswer ? (
                    <OutcomeBadge outcome={outcome} testId={`run-answer-outcome-${latestAnswer.id}`} />
                  ) : null}
                  {/* 印の数のバッジ（チャットの「資料との照合」・RAG と同じ共有の部品。0 件は出さない。#2738）。 */}
                  <AnswerPassageBadges records={passages} labels={answerPassageLabels()} testId="run-answer-passages" />
                </>
              ) : null
            }
            question={run.goal}
            startedAt={run.created_at}
            finishedAt={runFinishedAt(run)}
            durationMs={runDurationMs(run)}
            kind={runSourceLabelOf(run.metadata)}
            executor={{ name: executorName, isSelf: executedByMe }}
            owner={showOwner ? { name: ownerName, isSelf: run.owned_by_me === true } : null}
            cancellation={cancellation}
            target={{ label: t("run.fact.agent"), value: agentName }}
            model={usage?.model || null}
            usage={usage ? { tokens: usage.total_tokens } : null}
            extraFacts={
              usage
                ? [
                    {
                      id: "modelCalls",
                      label: t("run.fact.modelCalls"),
                      value: t("run.fact.modelCallsValue", {
                        requests: formatNumber(usage.requests),
                        input: formatNumber(usage.input_tokens),
                        output: formatNumber(usage.output_tokens),
                      }),
                    },
                  ]
                : []
            }
            runId={run.id}
            labels={labels}
            actions={
              // 「会話を開く」（3 製品共通の部品。チャットから送った Run だけ。#2343）と Run の操作（承認・再開・取消）。
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <OpenConversationAction
                  href={conversationHref}
                  linkComponent={Link}
                  labels={{ label: t("run.openChat"), ariaLabel: t("run.openChatLabel") }}
                  testId="run-open-chat"
                />
                <ObjectActionBar
                  actions={actions}
                  ariaLabel={t("run.actions")}
                  moreLabel={t("common.moreActions")}
                  testId="run-object-actions"
                />
              </div>
            }
            alert={
              <>
                <RunProgressIndicator run={run} steps={timelineSteps} />
                {sseState.status === "failed" && CANCELLABLE_RUN_STATUSES.includes(run.status) ? <Banner severity="warning" title={t("run.stream.stoppedTitle")} action={<Button variant="secondary" size="sm" icon={RefreshCw} onClick={sseState.reconnect}>{t("run.stream.reconnect")}</Button>}>
                  <span data-testid="run-stream-stopped">{t("run.stream.sseFailed")}</span>
                </Banner> : null}
                {waitingForExecutor ? (
                  // 操作のボタンは出さず、待っている相手を出す（messaging.md §10。info の Banner。#1954）。
                  <div data-testid="run-waiting-for-executor">
                    <Banner severity="info" title={t("run.waitingForExecutor", { name: executorName })}>
                      {t("run.waitingForExecutorDetail")}
                    </Banner>
                  </div>
                ) : run.status === "waiting_approval" ? (
                  <Banner severity="warning" title={t("run.waitingApproval")}>
                    {pendingApproval?.tool_call.name}
                  </Banner>
                ) : null}
                {failure ? (
                  // 失敗は 3 製品共通の失敗のカード（チャットと同じ 1 文目・「詳細」。#2342）。
                  <ProcessFailureNotice failure={failure} labels={processFailureLabels()} testId="run-failure" />
                ) : null}
                {unknownWrites.length > 0 ? (
                  <div data-testid="run-outcome-unknown">
                    <Banner severity="warning" title={t("run.outcomeUnknown.title")}>
                      <p>{t("run.outcomeUnknown.detail")}</p>
                      <ul>
                        {unknownWrites.map((item) => (
                          <li key={item.stepId}>
                            {t("run.outcomeUnknown.item", {
                              tool: toolTitles[item.toolName] ?? item.toolName,
                              key: item.operationKey,
                            })}
                          </li>
                        ))}
                      </ul>
                    </Banner>
                  </div>
                ) : null}

              </>
            }
            testId="run-summary"
          />
        }
        answer={
          answers.length ? (
            <RunDetailSection title={t("run.section.answer")} testId="run-answer-section">
              {answers.map((artifact) => {
                const text = String(artifact.content.text ?? artifact.content.answer);
                // 回答の本文はチャットと同じ節の形（根拠の行つき。#2328）。結び付けられなければ Markdown のまま
                // （表は共通の結果の表。#1158 / #1808）。
                const blocks =
                  evidence || passages
                    ? structureAnswer(
                        text,
                        evidence,
                        noticeHeading(validationContent),
                        answerStructureTitles(),
                        passages
                      )
                    : null;
                return blocks ? (
                  <StructuredAnswer
                    key={artifact.id}
                    runId={run.id}
                    blocks={blocks}
                    evidence={evidence}
                    passages={passages}
                    testId={`run-answer-${artifact.id}`}
                  />
                ) : (
                  <AnswerBody key={artifact.id} text={text} testId={`run-answer-${artifact.id}`} />
                );
              })}
              {/* 回答に付けた注意書き（そのときの文。回答の本文の後。#2405）。根拠は別の Section（RAG と同じ。#2707）。 */}
              <AnswerNotice
                notice={answerNoticeText(run.artifacts)}
                label={t("answer.notice.label")}
                className="mt-3"
              />
            </RunDetailSection>
          ) : null
        }
        timeline={
          <>
            <RunTimelineSection
              // 処理の経過と同じ記録を詳細で繰り返さない。
              traceId={progress.traceId}
              key={run.id}
              title={t("run.section.timeline")}
              steps={timelineSteps}
              totalDurationMs={runDurationMs(run)}
              // 実行中は「処理中（N ステップ完了）」と Run の開始からの経過時間（#3445）。
              active={CANCELLABLE_RUN_STATUSES.includes(run.status)}
              startedAt={run.created_at}
              labels={labels}
              // 共通の span の表に書けなかった span がある（#3226 の案 A）。経過の一覧が全部ではないことを示す。
              partial={progress.partial}
              empty={
                legacyProgress ? (
                  // span の無い古い形式の記録（空白にしない。#3226）。
                  <div data-testid="run-timeline-legacy">
                    <EmptyState title={traceLabels.legacy} />
                  </div>
                ) : (
                  <EmptyState title={t("run.timeline.noSteps")} />
                )
              }
              testId="run-timeline-section"
              // 処理の詳細（識別子・イベントの記録・ストリームの接続）は、処理の経過のタイムラインの後ろに既定で閉じて置く
              // （3 製品の実行の詳細とチャットの処理の経過で同じ名前・部品・位置。#2325）。
              processDetailsProps={{ title: t("run.section.processDetails"), metadata: { conversationId: run.thread_id ?? null, messageId: run.id }, testId: "run-technical-section", summaryTestId: "run-process-details" }}
              processDetails={<RunUsedModels models={usage?.model ? [{ label: "LLM", value: usage.model }] : []} events={progress.events} />}
            />
          </>
        }
        evidence={
          <>
            {answers.length && (evidence || withheldEvidence) ? (
              // 回答の根拠は RAG の実行の詳細と同じく別の Section（チャットと同じ「AI に渡した N 件 ／ 回答で引用
              // M 件」の折りたたみ。#2328・#2707）。
              <RunDetailSection title={t("run.section.evidence")} testId="run-evidence-section">
                <AnswerEvidenceBlock
                  runId={run.id}
                  evidence={evidence}
                  withheldCount={withheldEvidence}
                  idPrefix={`run-${run.id}`}
                />
              </RunDetailSection>
            ) : null}
            {run.approvals.length ? <ApprovalsSection run={run} /> : null}
            {outputs.length ? <ArtifactsSection artifacts={outputs} /> : null}
            {structured ? <StructuredResultCard key={run.id} run={run} table={structured} /> : null}
          </>
        }
        feedback={
          capabilities.admin && run.status === "completed" && answers.length ? (
            <RunDetailSection title={t("run.reviewTitle")} testId="run-review-section">
              {/* 管理者の評価（#774）。Agent 管理の権限で、回答が出た Run に付ける（本人の評価とは別）。 */}
              <AnswerFeedback
                runId={run.id}
                current={run.admin_review ?? null}
                mode="admin"
                onSaved={() => {
                  void queryClient.invalidateQueries({ queryKey: ["runs"] });
                  void queryClient.invalidateQueries({
                    queryKey: ["evaluation-case-draft", run.id],
                  });
                }}
              />
              {/* 管理者の評価の下で、この Run の質問を評価ケースにする（品質評価の権限。評価の Run は除く。#810）。 */}
              {canAddCase && !run.metadata?.evaluation_job_id ? (
                <AddToEvaluationCase key={run.id} runId={run.id} testId="run-add-case" />
              ) : null}
            </RunDetailSection>
          ) : null
        }
        technical={
          capabilities.viewAudit ? (
            <RunDetailSection
              title={t("run.audit")}
              description={t("run.auditDescription")}
              collapsible
              open={auditOpen}
              onOpenChange={setAuditOpen}
              testId="run-audit-section"
            >
              {auditOpen ? <AuditPanel runId={run.id} records={internalRecords} /> : null}
            </RunDetailSection>
          ) : null
        }
      />
    </section>
  );
}

function isAnswerArtifact(artifact: Artifact): boolean {
  return artifact.kind === ANSWER_KIND && typeof (artifact.content.text ?? artifact.content.answer) === "string";
}

/** 承認の記録（ツール・状態・判断した人と日時）。承認待ちの判断は概要の操作から行う。 */
function ApprovalsSection({ run }: { run: RunState }) {
  return (
    <RunDetailSection title={t("run.section.approvals")} testId="run-approvals-section">
      <ul className="grid min-w-0 gap-2">
        {run.approvals.map((approval) => (
          <li
            key={approval.id}
            className="flex min-w-0 flex-wrap items-center justify-between gap-2 rounded-md border border-border p-3"
          >
            <div className="min-w-0">
              <p className="break-all text-sm font-medium text-fg">{approval.tool_call.name}</p>
              {approval.decided_by && approval.decided_at ? (
                <p className="mt-0.5 text-xs text-fg-muted">
                  {t("run.approvalDecidedBy", { name: approval.decided_by, date: formatDate(approval.decided_at) })}
                </p>
              ) : null}
            </div>
            <StatusBadge {...approvalStatusView(approval.status)} />
          </li>
        ))}
      </ul>
    </RunDetailSection>
  );
}

/**
 * 実行中の Run の経過時間（durable job。messaging.md §3.7 の placement="job"。共通の `RunJobProgress`）。
 * 開始時刻はサーバーの created_at を使い、Run を開き直しても経過時間が 0 に戻らない。
 * 実行中は処理の経過の今の段の名前（ツールは表示名。チャットと同じ名前）を出し、ツールの内部名・段の種類のコードは
 * 出さない（#3445）。処理の経過を開いて今の段が回っている間は、この表示は回さない（動くスピナーは 1 つ）。
 * 承認待ちは人の操作を待つため遅延の案内を出さない。
 */
function RunProgressIndicator({ run, steps }: { run: RunState; steps: readonly RunTimelineStep[] }) {
  if (!CANCELLABLE_RUN_STATUSES.includes(run.status)) return null;
  const currentStep = steps[runTimelineCurrentStepIndex(steps)];
  const label =
    run.status === "queued"
      ? t("run.progress.queued")
      : run.status === "waiting_approval"
        ? t("run.progress.waitingApproval")
        : run.status === "waiting_confirmation"
          ? t("run.progress.waitingConfirmation")
          : (currentStep?.label ?? t("run.progress.running"));
  return (
    <RunJobProgress
      active
      label={label}
      operationKey={run.id}
      startedAt={run.created_at}
      showSlowMessage={run.status !== "waiting_approval" && run.status !== "waiting_confirmation"}
      className="rounded-md border border-border bg-surface-sunken px-3 py-2"
      testId="run-progress"
    />
  );
}

function compactTimelineDetails(items: Array<[string, string | null]>): Array<{ label: string; value: string }> {
  return items.filter((item): item is [string, string] => Boolean(item[1])).map(([label, value]) => ({ label, value }));
}

/**
 * 実行の詳細の監査ログ（#2403）。ツールの呼び出しの記録（判断・権限・承認・処理時間・エラー）と、実行が残した内部の
 * 記録（支援タスクの状態・資料との照合・検索の途中の結果など。成果物の種類で `internalRecordArtifacts` が分ける）を、
 * イベントの記録と同じ共通の `RunEventLog` で 1 行ずつ時間の順に並べる（行を開いて詳細と元の JSON。内側だけを
 * スクロールする）。ツールの記録は開いたときだけ読む。
 */
function AuditPanel({ runId, records }: { runId: string; records: Artifact[] }) {
  const audit = useQuery({
    queryKey: ["runs", runId, "audit"],
    queryFn: () => agentApi.getRunAudit(runId),
  });

  return (
    <div className="min-w-0" data-testid="run-audit">
      <QueryState
        query={audit}
        loadingLabel={t("loading.runAudit")}
        skeleton={<ListSkeleton rows={3} rowClassName="h-10" />}
      >
        {audit.data ? (
          <RunEventLog
            items={auditLogItems(audit.data, records)}
            labels={{
              title: t("run.audit"),
              filter: t("run.events.filter"),
              all: t("common.all"),
              first: t("run.events.first"),
              latest: t("run.events.latest"),
              empty: t("run.noAudit"),
              count: listCountLabels(),
              detailsTruncated: t("run.events.truncated"),
            }}
            testId="run-audit-log"
          />
        ) : null}
      </QueryState>
    </div>
  );
}

/** 監査ログの行（ツールの記録と内部の記録を時間の順に。時刻の無い記録は直前の記録の時刻で並べる）。 */
function auditLogItems(audit: RunAuditData, records: Artifact[]): RunEventLogItem[] {
  const keyed = [
    ...withSortKeys(audit.records.map((record) => auditToolItem(audit, record))),
    ...withSortKeys(records.map(auditRecordItem)),
  ];
  return keyed
    .map((entry, index) => ({ ...entry, index }))
    .sort((a, b) => a.key - b.key || a.index - b.index)
    .map((entry) => entry.item);
}

function withSortKeys(items: RunEventLogItem[]): Array<{ item: RunEventLogItem; key: number }> {
  let last = Number.NEGATIVE_INFINITY;
  return items.map((item) => {
    const time = item.at ? Date.parse(item.at) : Number.NaN;
    if (Number.isFinite(time)) last = time;
    return { item, key: last };
  });
}

/** ツールの呼び出しの記録。行は ツール名・状態・処理時間、判断・権限・承認・ID・メタデータは開いた詳細に置く。 */
function auditToolItem(audit: RunAuditData, record: ToolAuditRecord): RunEventLogItem {
  const status = stepStatusView(record.status);
  const metadata = runStepDetailsJson(record.audit_metadata);
  const hasMetadata = Object.keys(record.audit_metadata ?? {}).length > 0;
  const warnings = record.guardrail_warnings;
  const fields: Array<[string, string | null]> = [
    [t("run.auditPolicy"), record.policy_decision ? policyDecisionView(record.policy_decision).label : null],
    [t("common.permission"), record.permission_level ? permissionView(record.permission_level).label : null],
    [t("run.auditApproval"), record.approval_status ? approvalStatusView(record.approval_status).label : null],
    [t("run.auditRunStatus"), runStatusView(audit.status).label],
  ];
  const ids: Array<[string, string | null]> = [
    [t("run.timeline.step"), record.step_id],
    [t("run.auditTrace"), record.trace_id ?? null],
    [t("run.auditArtifacts"), record.artifact_ids.length ? record.artifact_ids.join(", ") : null],
    [t("audit.errorCode"), record.error_code ?? null],
  ];
  return {
    id: `tool-${record.step_id}`,
    title: record.tool_name,
    at: record.started_at ?? record.completed_at ?? null,
    category: { id: "tool", label: t("status.eventCategory.tool") },
    icon: PlayCircle,
    status: { variant: status.variant, label: status.label },
    durationMs: record.duration_ms ?? null,
    details: {
      fields: [
        ...compactTimelineDetails(fields),
        ...compactTimelineDetails(ids).map((item) => ({ ...item, mono: true })),
      ],
      code: hasMetadata ? [{ label: t("run.auditMetadata"), language: "json" as const, text: metadata.text }] : undefined,
      truncated: hasMetadata && metadata.truncated,
    },
    children:
      record.error || warnings.length ? (
        // エラーと警告はアイコン付きの FormStatus で出す（色だけに頼らない）。失敗・警告の行は最初から開く。
        <div className="min-w-0 space-y-1 break-words [overflow-wrap:anywhere]">
          {record.error ? <FormStatus tone="danger" message={record.error} /> : null}
          {warnings.map((warning) => (
            <FormStatus key={warning} tone="warning" message={warning} />
          ))}
        </div>
      ) : undefined,
    defaultOpen: Boolean(record.error) || warnings.length > 0,
  };
}

/** 内部の記録（成果物の種類と ID、開くと元の JSON の全体）。 */
function auditRecordItem(artifact: Artifact): RunEventLogItem {
  const kind = artifactKindView(artifact.kind);
  // 元の JSON は切り詰めずに出す（箱の中で縦横にスクロールする）。
  const json = runStepDetailsJson(artifact.content, Number.POSITIVE_INFINITY);
  return {
    id: `record-${artifact.id}`,
    title: kind.label,
    summary: artifact.name !== kind.label ? artifact.name : undefined,
    at: artifact.created_at,
    category: { id: "record", label: t("run.auditRecords") },
    icon: FileText,
    details: {
      fields: [
        { label: t("run.auditRecordKind"), value: artifact.kind, mono: true },
        { label: t("run.auditRecordId"), value: artifact.id, mono: true },
      ],
      code: [{ label: t("run.rawJson"), language: "json", text: json.text }],
    },
  };
}

/** 利用者が使う出力（表の結果など。種類は `USER_OUTPUT_ARTIFACT_KINDS`。#2403）。元の順序で並べる。 */
function ArtifactsSection({ artifacts }: { artifacts: Artifact[] }) {
  return (
    <RunDetailSection title={t("run.artifacts")} description={t("run.artifactsDescription")} testId="run-artifacts-section">
      {artifacts.map((artifact) => (
        <div key={artifact.id} className="min-w-0 rounded-md border border-border p-3" data-testid={`run-artifact-${artifact.id}`}>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="break-all text-sm font-medium text-fg">{artifact.name}</p>
              <p className="mt-0.5 text-xs text-fg-muted">{formatDate(artifact.created_at)}</p>
            </div>
            <StatusBadge {...artifactKindView(artifact.kind)} icon={false} />
          </div>
          {artifact.kind === "structured_table" ? (
            <StructuredArtifactSummary artifact={artifact} />
          ) : (
            <ToolOutput
              output={artifact.content}
              table={artifactTable(artifact)}
              name={artifact.name}
              testId={`run-artifact-table-${artifact.id}`}
            />
          )}
        </div>
      ))}
    </RunDetailSection>
  );
}

function StructuredArtifactSummary({ artifact }: { artifact: Artifact }) {
  const table = artifactTable(artifact);
  const rowCount = numericValue(artifact.content.row_count);
  const truncated = typeof artifact.content.truncated === "boolean" ? artifact.content.truncated : null;
  const warnings = arrayOfText(artifact.content.warnings);
  const sql = structuredSql(artifact.content);

  return (
    <div className="mt-3 space-y-3">
      {table ? null : (
        // 表の形でない構造化データ（ジョブの実行中など）は、今までどおり件数の要約と JSON で出す。
        <div className="grid gap-2 text-sm sm:grid-cols-3">
          <MetricPill label={t("run.rowCount")} value={rowCount === null ? "-" : String(rowCount)} />
          <MetricPill
            label={t("run.truncated")}
            value={truncated === null ? "-" : truncated ? t("common.yes") : t("common.no")}
          />
          <MetricPill label={t("run.columns")} value={String(arrayOfRecords(artifact.content.columns).length)} />
        </div>
      )}
      {sql ? (
        // SQL は NL2SQL のチャットと同じ共有の部品（安全検査・実行の状態・コピー。#2346）。
        <ChatSqlBlock
          sql={sql}
          badges={<SqlSafetyBadge safe={artifact.content.is_safe !== false} executed={artifact.content.status === "done"} />}
          testId={`run-artifact-sql-${artifact.id}`}
        />
      ) : null}
      {warnings.length ? (
        <Banner severity="warning">
          <div className="space-y-1">
            {warnings.map((warning) => (
              <p key={warning}>{warning}</p>
            ))}
          </div>
        </Banner>
      ) : null}
      <ToolOutput
        output={artifact.content}
        table={table}
        name={artifact.name}
        testId={`run-artifact-table-${artifact.id}`}
      />
    </div>
  );
}

/**
 * ツールの結果・成果物の内容。表の形なら共通の結果の表で出し、元の JSON は「元の JSON」に畳む。
 * 表でなければ今までどおり JSON で出す（#1158）。
 */
function ToolOutput({
  output,
  table,
  name,
  testId,
}: {
  output: unknown;
  table: ReturnType<typeof stepResultTable>;
  name: string;
  testId: string;
}) {
  if (!table) return <JsonPreview value={output} />;
  return (
    <div className="mt-3 min-w-0 space-y-2">
      <ResultTable data={table} name={name} testId={testId} />
      <Disclosure variant="plain" size="sm" summary={t("run.rawJson")}>
        <JsonPreview value={output} />
      </Disclosure>
    </div>
  );
}

/**
 * 「構造化データ」の節。表の形の成果物（NL2SQL のツールの結果など）が無い Run で、表の形のツールの結果の
 * 最後の 1 つを出す（成果物の表と同じものを二度出さない。#1158）。
 */
function StructuredResultCard({ run, table }: { run: RunState; table: ToolResultTable }) {
  const output = run.steps.find((step) => step.id === table.stepId)?.tool_result?.output ?? {};
  const sql = table.sql;
  return (
    <RunDetailSection
      title={t("run.structuredResult")}
      description={sql || structuredSql(output) ? undefined : t("run.sqlHidden")}
      testId="run-structured-section"
    >
      {sql ? (
        <div className="mb-3 space-y-2">
          <ChatSqlBlock
            sql={sql.text}
            badges={<SqlSafetyBadge safe={sql.safe} executed={sql.executed} />}
            testId="run-structured-sql"
          />
        </div>
      ) : null}
      <ResultTable
        data={table.data}
        name={t("resultTable.toolName", { tool: table.toolName })}
        testId="run-structured-table"
      />
    </RunDetailSection>
  );
}

function structuredFallback(run: RunState): ToolResultTable | null {
  if (run.artifacts.some((artifact) => artifactTable(artifact))) return null;
  return runToolResultTables(run).at(-1) ?? null;
}

/** 構造化データの SQL（NL2SQL の MCP の出力は `executable_sql` / `generated_sql`）。 */
function structuredSql(content: Record<string, unknown>): string | null {
  return textValue(content.sql) ?? textValue(content.executable_sql) ?? textValue(content.generated_sql);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function arrayOfRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function arrayOfText(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function textValue(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return null;
}

function numericValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
