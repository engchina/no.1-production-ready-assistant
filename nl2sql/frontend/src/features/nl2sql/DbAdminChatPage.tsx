import { useCallback, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { Check, ListChecks, Play, RefreshCw, Sparkles } from "lucide-react";
import {
  ApiErrorBanner,
  ChatReadOnlyNotice,
  Banner,
  AnswerNotice,
  Button,
  ChatAnswer,
  ChatProgress,
  RunUsedModels,
  RunProcessDetails,
  ChatSkeleton,
  ChatSqlBlock,
  ChatTurn,
  createOptimisticChatMessage,
  DEFAULT_PAGE_SIZE,
  EmptyState,
  Disclosure,
  ExecutionConfirmationField,
  MarkdownText,
  ProcessingIndicator,
  StatusBadge,
  TimedLoadingState,
  useChatAutoScroll,
  useTraceDetailsLabels,
  useChatHistoryActions,
  useChatHistoryPanel,
  withOptimisticChatStatus,
  type OptimisticChatMessage,
} from "@production-ready/ui";

import {
  useWorkspaceActive,
  useWorkspaceIdentity,
  useWorkspaceState,
} from "@/components/WorkspaceState";
import { useAuth } from "@/features/security/AuthProvider";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "@/lib/api";
import { t } from "@/lib/i18n";
import { randomUuid } from "@/lib/randomUuid";
import { API_TIMEOUT_MS } from "@/lib/requestPolicy";

import { CHAT_PROGRESS_LABELS, dbAdminChatTurnTracePath, parseTraceProgressResponse } from "./chatProgress";
import { ProgressLegacyNotice } from "./components/ProgressLegacyNotice";
import {
  Nl2SqlChatComposer,
  Nl2SqlChatHistoryList,
  Nl2SqlChatLayout,
  Nl2SqlChatPage,
  Nl2SqlPendingTurn,
  normalizeHistoryOffset,
  SQL_BLOCK_LABELS,
  othersConversationIds,
} from "./components/ChatScaffold";
import { ChatLastExecutionText, ChatQueryResult, formatLastExecutedAt } from "./components/ChatSqlExecution";
import { DbAdminExecutionResult } from "./components/DbAdminShared";
import { SchemaSyncNotice } from "./components/SchemaRefreshFeedback";
import { DEFAULT_SQL_ROW_LIMIT } from "./components/SqlRowLimitControls";
import {
  DB_ADMIN_CHAT_TURN_LIMIT,
  dbAdminChatBlockedStatementLabels,
  dbAdminChatCanRunAll,
  dbAdminChatCanRunStatement,
  dbAdminChatConfirmed,
  dbAdminChatExecutionFromOutcome,
  dbAdminChatGenerateStep,
  dbAdminChatSavedExecutions,
  dbAdminChatStatementKind,
  dbAdminChatTraceProgress,
  dbAdminExecutionSucceeded,
  type DbAdminChatExecution,
  type DbAdminChatRunAll,
  type DbAdminChatTurnExecutions,
} from "./dbAdminChat";
import { nl2sqlIncrementalKeys } from "./incrementalQueries";
import type {
  ChatHistoryOffsetPage,
  DbAdminChatAnalyzeData,
  DbAdminChatConversation,
  DbAdminChatData,
  DbAdminChatExecuteData,
  DbAdminChatPlan,
  DbAdminChatSendData,
  DbAdminChatStatementPlan,
  DbAdminChatTurn as DbAdminChatTurnData,
} from "./types";
import { useMutationSchemaSync } from "./useMutationSchemaSync";

/** 送った質問（生成の応答の前・送れなかったとき）。ターンの ID は送信の前に決め、送り直しも同じ ID。 */
interface PendingGenerate {
  message: OptimisticChatMessage;
  turnId: string;
  error: unknown;
}

const TEST_ID = "db-admin-chat";
const conversationPath = (id: string) =>
  `/api/nl2sql/db-admin-chat/conversations/${encodeURIComponent(id)}`;

/**
 * DB 管理チャット（#2264）。質問から Oracle の SQL（SELECT・DML・DDL・DCL・PL/SQL）を Enterprise AI Direct で
 * 作り、生成した SQL を必ず見せる。SELECT は「実行」で、それ以外は実行確認語を入力して実行する（自動では
 * 実行しない）。会話とターンはサーバーが保存し、「チャット」と同じ会話の履歴のパネル（開き直し・名前の
 * 変更・削除・新しい会話）を持つ（#2319）。画面の骨格・会話の履歴・入力欄・送信中の往復・SQL の表示・
 * 結果の表・処理の経過は「チャット」と同じ部品（ChatScaffold・ChatQueryResult・共通の ChatProgress）で描く。
 */
export function DbAdminChatPage() {
  const active = useWorkspaceActive();
  const { user } = useAuth();
  // 「AI 活用」と同じ見え方（#1889）: system_admin は全員の会話を見られ、履歴に利用者を出す。
  const seesAllUsers = Boolean(user?.is_system_admin);
  const identity = useWorkspaceIdentity();
  const queryClient = useQueryClient();
  const [conversationId, setConversationId] = useWorkspaceState("conversationId", "");
  const [draft, setDraft] = useWorkspaceState(`draft:${conversationId}`, "");
  const [historyPanelOpen, setHistoryPanelOpen] = useWorkspaceState("historyOpen", false);
  // 開閉の判定・シートの開閉は共通の useChatHistoryPanel（3 製品共通。#1161）。
  const historyPanel = useChatHistoryPanel({
    inlineOpen: historyPanelOpen,
    onInlineOpenChange: setHistoryPanelOpen,
    id: "db-admin-chat-history",
  });
  const [historyOffset, setHistoryOffset] = useWorkspaceState<number>("historyOffset", 0);
  const historyPageOffset = normalizeHistoryOffset(historyOffset);
  const [pending, setPending] = useState<PendingGenerate | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const generating = pending?.message.status === "sending";
  const chatKey = useMemo(
    () => ["nl2sql", "db-admin-chats", identity.owner, identity.context] as const,
    [identity.owner, identity.context],
  );
  const history = useQuery({
    // 会話の key（[...chatKey, 会話の ID]）と重ならないよう、一覧は object の要素で分ける。
    queryKey: [...chatKey, { list: historyPageOffset }],
    enabled: active,
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({
        limit: String(DEFAULT_PAGE_SIZE),
        offset: String(historyPageOffset),
      });
      return apiGet<ChatHistoryOffsetPage<DbAdminChatConversation>>(
        `/api/nl2sql/db-admin-chat/conversations?${params.toString()}`,
        { signal, timeoutMs: API_TIMEOUT_MS.interactiveList },
      );
    },
    placeholderData: keepPreviousData,
  });
  const conversation = useQuery({
    queryKey: [...chatKey, conversationId],
    enabled: active && Boolean(conversationId),
    queryFn: async ({ signal }) => {
      const data = await apiGet<DbAdminChatData>(conversationPath(conversationId), {
        signal,
        timeoutMs: API_TIMEOUT_MS.interactiveDetail,
      });
      // ターンの処理の経過も一緒に読み、会話を 1 回で描く（#3226 の案 A。読めなければ経過を出さない）。
      await prefetchDbAdminChatTurnTraces(queryClient, conversationId, data.turns);
      return data;
    },
  });
  const turns = useMemo(() => conversation.data?.turns ?? [], [conversation.data]);
  // 開いている会話が他の利用者の物か（system_admin だけが開ける）。続きの質問・実行・SQL の置き換え・
  // 名前の変更は持ち主だけなので、入力欄と実行の操作を出さず閲覧だけの案内にする（削除は出す。#3244）。
  const readOnly =
    seesAllUsers && Boolean(conversationId) && conversation.data?.conversation.owned === false;
  const conversationLoading = Boolean(conversationId) && conversation.isPending && !pending;
  const atLimit = turns.length >= DB_ADMIN_CHAT_TURN_LIMIT;
  const blocked =
    generating ||
    atLimit ||
    (Boolean(conversationId) && (conversation.isPending || conversation.isError));

  const autoScroll = useChatAutoScroll({
    contentKey: [
      turns.length,
      turns
        .map(
          (turn) =>
            `${turn.revision}:${turn.last_run_all?.executed_at ?? ""}:${turn.statement_executions
              .map((item) => item.executed_at)
              .join("|")}`,
        )
        .join(","),
      pending?.message.localId ?? "",
      pending?.message.status ?? "",
    ].join(":"),
    resetKey: conversationId,
    enabled: active,
  });

  const generate = useCallback(
    async (question: string, message: OptimisticChatMessage, turnId: string) => {
      const controller = new AbortController();
      abortRef.current = controller;
      setPending({ message, turnId, error: null });
      try {
        // 前の往復は送らない（サーバーが保存した会話から復元する。#2319）。
        const data = await apiPost<DbAdminChatSendData>(
          "/api/nl2sql/db-admin-chat/generate",
          { question, conversation_id: conversationId, client_turn_id: turnId },
          { signal: controller.signal, timeoutMs: API_TIMEOUT_MS.longRunningJob },
        );
        const id = data.conversation.id;
        queryClient.setQueryData<DbAdminChatData>([...chatKey, id], (previous) => ({
          conversation: data.conversation,
          turns: [
            ...(previous?.turns ?? []).filter((turn) => turn.turn_id !== data.turn.turn_id),
            data.turn,
          ],
        }));
        setConversationId(id);
        setPending(null);
        void queryClient.invalidateQueries({ queryKey: chatKey, exact: false });
      } catch (error) {
        if (controller.signal.aborted) {
          // 停止した質問は入力欄に戻す（停止した生成はサーバーも保存しない。生成は副作用が無いので送り直せる）。
          setPending(null);
          setDraft(question);
          return;
        }
        setPending({ message: withOptimisticChatStatus(message, "failed"), turnId, error });
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [chatKey, conversationId, queryClient, setConversationId, setDraft],
  );

  const submit = () => {
    const question = draft.trim();
    if (!question || blocked) return;
    setDraft("");
    autoScroll.scrollToLatest();
    void generate(question, createOptimisticChatMessage(question), randomUuid());
  };

  const resend = () => {
    if (!pending || generating) return;
    // 同じターンの ID で送るので、応答が届かなかっただけならサーバーは保存したターンを返す（二重に生成しない）。
    void generate(
      pending.message.content,
      withOptimisticChatStatus(pending.message, "sending"),
      pending.turnId,
    );
  };

  const resetConversation = () => {
    if (generating) return;
    setConversationId("");
    setPending(null);
    composerRef.current?.focus();
  };

  const openConversation = (item: DbAdminChatConversation) => {
    if (generating) return;
    setConversationId(item.id);
    setPending(null);
    historyPanel.closeSheet();
  };

  // 会話の名前の変更・削除（3 製品共通の useChatHistoryActions。#1817）。消せた後に開いている会話なら
  // 新しい会話に戻す。
  const historyActions = useChatHistoryActions({
    onRename: async (id, title) => {
      await apiPatch(conversationPath(id), { title });
      await queryClient.invalidateQueries({ queryKey: chatKey });
    },
    onDelete: (id) => apiDelete(conversationPath(id)),
    onDeleted: (item) => {
      if (item.id === conversationId) resetConversation();
      queryClient.removeQueries({ queryKey: [...chatKey, item.id] });
      void queryClient.invalidateQueries({ queryKey: chatKey });
    },
    disabled: generating,
    idPrefix: "db-admin-chat-history-title",
    // 他の利用者の会話（system_admin が見る）の名前は持ち主だけが変えられる。削除は出す（#3244）。
    canRename: (item) => !othersConversationIds(history.data).has(item.id),
  });

  const replaceTurn = useCallback(
    (turn: DbAdminChatTurnData) => {
      queryClient.setQueryData<DbAdminChatData>([...chatKey, conversationId], (previous) =>
        previous
          ? {
              ...previous,
              turns: previous.turns.map((item) => (item.turn_id === turn.turn_id ? turn : item)),
            }
          : previous,
      );
    },
    [chatKey, conversationId, queryClient],
  );
  const refreshConversation = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: [...chatKey, conversationId] });
  }, [chatKey, conversationId, queryClient]);

  return (
    // 見出しと本文の高さの渡し方はチャットと共通の Nl2SqlChatPage（#2332）。
    <Nl2SqlChatPage
      title={t("nav.dbAdminChat")}
      subtitle={t("dbAdminChat.subtitle")}
      testId="nl2sql-db-admin-chat"
    >
      <Nl2SqlChatLayout
        history={{
          panel: historyPanel,
          historyDays: history.data?.history_days,
          // 一覧の行・読み込み中・失敗・0 件・ページングはチャットと共通の Nl2SqlChatHistoryList（#2319）。
          content: (
            <Nl2SqlChatHistoryList
              page={history.data}
              loading={history.isPending}
              error={history.isError ? history.error : null}
              onRetry={() => void history.refetch()}
              retrying={history.isFetching}
              currentId={conversationId || null}
              onSelect={openConversation}
              disabled={generating}
              seesAllUsers={seesAllUsers}
              offset={historyPageOffset}
              onOffsetChange={setHistoryOffset}
              actions={historyActions}
              testIdPrefix={TEST_ID}
            />
          ),
        }}
        label={t("dbAdminChat.conversation")}
        // 会話を選んでいる間は、履歴を閉じていても今の会話の名前を出す。
        conversationTitle={conversationId ? conversation.data?.conversation.title : null}
        conversationTitleLoading={Boolean(conversationId) && conversation.isPending}
        onNewConversation={resetConversation}
        newConversationDisabled={generating || (!conversationId && !pending)}
        autoScroll={autoScroll}
        testIdPrefix={TEST_ID}
        composer={
          readOnly ? (
            // 入力欄の代わりの閲覧だけの案内（3 製品共通の ChatReadOnlyNotice。#3244）。生成は送った人の画面の
            // 要求（同期）なので、見ている人の停止は無い。
            <ChatReadOnlyNotice testIdPrefix={`${TEST_ID}-read-only`} />
          ) : (
            <Nl2SqlChatComposer
              id={`${TEST_ID}-composer`}
              value={draft}
              onValueChange={setDraft}
              onSubmit={submit}
              onStop={() => abortRef.current?.abort()}
              running={generating}
              textareaRef={composerRef}
              submitBlocked={blocked}
              sendTestId={`${TEST_ID}-send`}
              footer={atLimit ? <Banner severity="info">{t("chat.limit")}</Banner> : null}
            />
          )
        }
      >
        {conversationLoading ? (
          // 会話の内容の読み込み中は文言と経過時間を出し、会話の形の Skeleton で寸法を予約する（チャットと同じ）。
          <TimedLoadingState
            label={t("chat.loading")}
            operationKey="db-admin-chat-conversation-load"
            framed={false}
            testId={`${TEST_ID}-conversation-loading`}
          >
            <ChatSkeleton />
          </TimedLoadingState>
        ) : null}
        {conversationId && conversation.isError ? (
          <ApiErrorBanner
            error={conversation.error}
            fallback={t("chat.loadFailed")}
            action={
              <Button
                type="button"
                variant="secondary"
                size="sm"
                icon={RefreshCw}
                onClick={() => void conversation.refetch()}
              >
                {t("chat.retry")}
              </Button>
            }
          />
        ) : null}
        {!conversationId && !pending ? (
          <EmptyState title={t("chat.empty")} hint={t("dbAdminChat.emptyHint")} />
        ) : null}
        {turns.map((turn) => (
          <DbAdminChatTurn
            key={`${turn.turn_id}:${turn.revision}`}
            conversationId={conversationId}
            turn={turn}
            readOnly={readOnly}
            onReplaceTurn={replaceTurn}
            onExecuted={refreshConversation}
          />
        ))}
        {pending ? (
          // 送った質問は生成の応答を待たずに出し、失敗しても残して「再送信」を出す（チャットと同じ。#907）。
          <Nl2SqlPendingTurn
            message={pending.message}
            error={pending.message.status === "failed" ? pending.error : null}
            fallback={t("dbAdminChat.sendFailed")}
            onResend={resend}
            resendDisabled={generating}
            steps={[dbAdminChatGenerateStep("running", pending.message.sentAtMs)]}
            testIdPrefix={TEST_ID}
          />
        ) : null}
      </Nl2SqlChatLayout>
    </Nl2SqlChatPage>
  );
}

const KIND_BADGE_VARIANT = {
  readOnly: "success",
  change: "warning",
  blocked: "danger",
} as const;

/** 文の分類のバッジ（読み取り専用 / 変更あり / 実行できない）と文の種類。 */
function StatementBadges({ statement, total }: { statement: DbAdminChatStatementPlan; total: number }) {
  const kind = dbAdminChatStatementKind(statement);
  return (
    <>
      {total > 1 ? (
        <StatusBadge icon={false} variant="neutral" label={t("dbAdminChat.statement.label", { index: statement.index })} />
      ) : null}
      <StatusBadge variant={KIND_BADGE_VARIANT[kind]} label={t(`dbAdminChat.kind.${kind}`)} />
      <StatusBadge icon={false} variant="neutral" label={statement.statement_type} />
    </>
  );
}

/** 直した SQL（分析の結果）の文ごとの分類のバッジ。 */
function PlanBadges({ plan }: { plan: DbAdminChatPlan }) {
  const kinds = [...new Set(plan.statements.map(dbAdminChatStatementKind))];
  const types = [...new Set(plan.statements.map((item) => item.statement_type))];
  return (
    <>
      {kinds.map((kind) => (
        <StatusBadge key={kind} variant={KIND_BADGE_VARIANT[kind]} label={t(`dbAdminChat.kind.${kind}`)} />
      ))}
      {types.map((type) => (
        <StatusBadge key={type} icon={false} variant="neutral" label={type} />
      ))}
    </>
  );
}

type AnalysisState =
  | { status: "running" }
  | { status: "done"; data: DbAdminChatAnalyzeData }
  | { status: "failed"; error: unknown };

/**
 * 1 往復の実行の状態（文ごとの実行と「すべて実行」）。この画面で受け取った結果（行を持つ）は TanStack Query の
 * キャッシュ（メモリ）に置き、会話を切り替えて戻っても同じ画面の中では出し直す（チャットと同じ。sessionStorage
 * などには保存しない）。受け取っていない文（開き直した会話）はサーバーが保存した最後の実行の要約（行は無い）。
 */
function useDbAdminChatExecutions(turn: DbAdminChatTurnData) {
  const identity = useWorkspaceIdentity();
  const queryClient = useQueryClient();
  const key = useMemo(
    () =>
      [
        "nl2sql",
        "db-admin-chat-execution",
        identity.owner,
        identity.context,
        turn.turn_id,
        turn.revision,
      ] as const,
    [identity.owner, identity.context, turn.turn_id, turn.revision],
  );
  const cached = useQuery<DbAdminChatTurnExecutions | null>({
    queryKey: key,
    queryFn: () => null,
    enabled: false,
    staleTime: Infinity,
    gcTime: 30 * 60_000,
  });
  const saved = useMemo(() => dbAdminChatSavedExecutions(turn), [turn]);
  const executions: DbAdminChatTurnExecutions = {
    statements: { ...saved.statements, ...cached.data?.statements },
    runAll: cached.data?.runAll ?? saved.runAll,
  };
  const update = useCallback(
    (change: (current: DbAdminChatTurnExecutions) => DbAdminChatTurnExecutions) =>
      queryClient.setQueryData<DbAdminChatTurnExecutions | null>(key, (previous) =>
        change(previous ?? { statements: {}, runAll: null }),
      ),
    [key, queryClient],
  );
  return { executions, update };
}

/** ターンの処理の経過の cache の key（ターンの span は生成の後に変わらない。会話の取り直しで無効にしない）。 */
function dbAdminChatTraceKey(conversationId: string, turnId: string) {
  return ["nl2sql", "db-admin-chat-trace", conversationId, turnId] as const;
}

async function fetchDbAdminChatTurnTrace(conversationId: string, turnId: string, signal?: AbortSignal) {
  return parseTraceProgressResponse(
    await apiGet<unknown>(dbAdminChatTurnTracePath(conversationId, turnId), {
      signal,
      timeoutMs: API_TIMEOUT_MS.interactiveDetail,
    }),
  );
}

/** 会話のターンの処理の経過を読んでおく（読めなかったターンは経過を出さない。会話の表示は止めない）。 */
async function prefetchDbAdminChatTurnTraces(
  queryClient: QueryClient,
  conversationId: string,
  turns: readonly DbAdminChatTurnData[],
) {
  await Promise.all(
    turns.map((turn) =>
      queryClient.prefetchQuery({
        queryKey: dbAdminChatTraceKey(conversationId, turn.turn_id),
        staleTime: Number.POSITIVE_INFINITY,
        queryFn: ({ signal }) => fetchDbAdminChatTurnTrace(conversationId, turn.turn_id, signal),
      }),
    ),
  );
}

/**
 * 1 往復の回答（生成した SQL の文ごとの分類・実行・結果、「すべて実行」、失敗の分析）。
 * 文ごとの区画は SQL Developer などのワークシートと同じく、番号・分類・SQL・コピー・「実行」・その文の結果（#2333）。
 */
function DbAdminChatTurn({
  conversationId,
  turn,
  readOnly,
  onReplaceTurn,
  onExecuted,
}: {
  conversationId: string;
  turn: DbAdminChatTurnData;
  /** 他の利用者の会話を見ている（system_admin）。実行・分析・置き換えを出さない（持ち主だけ。#3244）。 */
  readOnly: boolean;
  onReplaceTurn: (turn: DbAdminChatTurnData) => void;
  onExecuted: () => void;
}) {
  const queryClient = useQueryClient();
  const { plan } = turn;
  // 生成したときの span の木（#3226 の案 A）。span は共通の span の表にあり、ターンの処理の経過の API で 1 回読む
  // （生成は同期の要求なので、表示するときには終わっていて変わらない）。会話を開くときに会話と一緒に読んでおき
  // （`prefetchDbAdminChatTurnTraces`）、ターンを 1 回で描く（ターンごとに後から高さが変わると、会話の欄が末尾から
  // ずれる）。
  const traceLabels = useTraceDetailsLabels();
  const trace = useQuery({
    queryKey: dbAdminChatTraceKey(conversationId, turn.turn_id),
    enabled: Boolean(conversationId),
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: ({ signal }) => fetchDbAdminChatTurnTrace(conversationId, turn.turn_id, signal),
  });
  const progress = useMemo(() => dbAdminChatTraceProgress(turn.turn_id, trace.data, traceLabels), [
    turn.turn_id,
    trace.data,
    traceLabels,
  ]);
  const [runAllConfirmation, setRunAllConfirmation] = useState("");
  const { executions, update } = useDbAdminChatExecutions(turn);
  const refreshSchemaReadModels = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["schema", "objects"] });
    void queryClient.invalidateQueries({ queryKey: ["nl2sql", "db-admin", "objects"] });
    void queryClient.invalidateQueries({ queryKey: nl2sqlIncrementalKeys.schemaHead });
  }, [queryClient]);
  // DDL の後の DB 構造の同期の案内と進行は、実行の結果の後に出す（管理 SQL を実行と同じ。#2031）。
  const schemaSync = useMutationSchemaSync({
    noticeOrigin: `db-admin-chat-notice-${turn.turn_id}`,
    onDone: refreshSchemaReadModels,
  });
  const runAll = executions.runAll;
  const running =
    runAll?.status === "running" ||
    Object.values(executions.statements).some((item) => item.status === "running");
  const total = plan.statements.length;
  const hasSql = total > 0;

  const execute = async (
    body: { mode: "statement"; statement_index: number } | { mode: "all" },
    confirmation: string,
  ) => {
    const startedAtMs = Date.now();
    if (body.mode === "all") {
      update((current) => ({ ...current, runAll: { status: "running", startedAtMs } }));
    } else {
      update((current) => ({
        ...current,
        statements: { ...current.statements, [body.statement_index]: { status: "running", startedAtMs } },
      }));
    }
    schemaSync.reset();
    try {
      // SQL は送らない（サーバーがターンに保存した SQL を実行する。#2319）。
      const data = await apiPost<DbAdminChatExecuteData>("/api/nl2sql/db-admin-chat/execute", {
        conversation_id: conversationId,
        turn_id: turn.turn_id,
        ...body,
        confirmation,
        row_limit: DEFAULT_SQL_ROW_LIMIT,
      });
      const finishedAtMs = Date.now();
      update((current) => {
        const statements = { ...current.statements };
        for (const outcome of data.statements) {
          statements[outcome.index] = dbAdminChatExecutionFromOutcome(outcome, data, startedAtMs, finishedAtMs);
        }
        const failed = data.statements.some((outcome) => outcome.status !== "succeeded");
        return {
          statements,
          runAll:
            body.mode === "all"
              ? { status: failed ? "failed" : "done", startedAtMs, finishedAtMs, data }
              : current.runAll,
        };
      });
      onExecuted();
      const tracked = schemaSync.track(data);
      if (data.committed && !tracked) {
        void queryClient.invalidateQueries({ queryKey: ["nl2sql", "db-admin", "objects"] });
      }
    } catch (error) {
      const failure = { status: "failed" as const, startedAtMs, finishedAtMs: Date.now(), requestError: error };
      if (body.mode === "all") {
        update((current) => ({ ...current, runAll: failure }));
      } else {
        update((current) => ({
          ...current,
          statements: { ...current.statements, [body.statement_index]: { ...failure, result: null } },
        }));
      }
    }
  };

  const runAllAllowed = dbAdminChatCanRunAll(plan, runAllConfirmation) && !running;
  const runAllButton = (
    <Button
      type="button"
      variant={plan.requires_confirmation ? "danger" : "secondary"}
      size="sm"
      icon={ListChecks}
      loading={runAll?.status === "running"}
      disabled={!runAllAllowed}
      onClick={() => {
        if (!runAllAllowed) return;
        // 実行の確認は 1 回ごと（もう一度実行するときは入力し直す）。
        const confirmation = plan.requires_confirmation ? runAllConfirmation.trim() : "";
        setRunAllConfirmation("");
        void execute({ mode: "all" }, confirmation);
      }}
      data-testid={`${TEST_ID}-run-all`}
    >
      {t("dbAdminChat.runAll")}
    </Button>
  );

  return (
    <ChatTurn
      question={turn.question}
      // 会話の持ち主以外（system_admin）が送った質問は、送った人を添える（チャットと同じ。#1928）。
      questionSender={
        turn.sender_display_name == null
          ? undefined
          : t("chat.sentBy", { name: turn.sender_display_name || t("chat.historyOwnerMissing") })
      }
      testId={`${TEST_ID}-turn`}
    >
      <ChatAnswer>
        {progress.legacy ? (
          <><ProgressLegacyNotice testId={`${TEST_ID}-progress-legacy`} />
            <RunProcessDetails metadata={{ conversationId, messageId: turn.turn_id }}><RunUsedModels models={turn.model ? [{ label: "LLM", value: turn.model }] : []} /></RunProcessDetails>
          </>
        ) : (
          <ChatProgress
            // 生成したときの span の木（実行は経過に足さない。実行の表示は文の区画。#3226）。
            steps={progress.steps}
            traceId={progress.traceId}
            processDetailsProps={{ metadata: { conversationId, messageId: turn.turn_id } }}
            footer={<RunUsedModels models={turn.model ? [{ label: "LLM", value: turn.model }] : []} />}
            labels={CHAT_PROGRESS_LABELS}
            partial={progress.partial}
            testId={`${TEST_ID}-progress`}
          />
        )}
        {turn.explanation ? (
          // 説明は Markdown を含みうるので、共通の Markdown の表示で描く（記号を生で出さない）。
          <MarkdownText text={turn.explanation} />
        ) : !hasSql ? (
          <p className="text-sm text-fg-muted">{t("dbAdminChat.noSql")}</p>
        ) : null}
        {turn.context_tables.length > 0 ? (
          <p className="break-words text-xs text-fg-muted" data-testid={`${TEST_ID}-context-tables`}>
            {t("dbAdminChat.contextTables", { tables: turn.context_tables.join(", ") })}
          </p>
        ) : null}
        {turn.corrected && hasSql ? (
          <p className="text-xs text-fg-muted">{t("dbAdminChat.correctedApplied")}</p>
        ) : null}
        {total > 1 ? (
          <section
            aria-label={t("dbAdminChat.runAll.region")}
            className="min-w-0 space-y-3"
            data-testid={`${TEST_ID}-run-all-region`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="min-w-0 text-sm text-fg-muted">{t("dbAdminChat.runAll.hint", { count: total })}</p>
              {/* 確認語の要る「すべて実行」のボタンは確認語の欄の横に出す。 */}
              {readOnly || (plan.requires_confirmation && !plan.blocked) ? null : runAllButton}
            </div>
            {plan.blocked ? (
              // 止める文を黙って飛ばさない: 「すべて実行」は押せず、理由をボタンの直下に出す。
              <p className="text-xs text-fg-muted" data-testid={`${TEST_ID}-run-all-blocked`}>
                {t("dbAdminChat.runAll.blocked", { statements: dbAdminChatBlockedStatementLabels(plan) })}
              </p>
            ) : null}
            {plan.requires_confirmation && !plan.blocked && !readOnly ? (
              <>
                <Banner severity="warning">
                  <p>{t("dbAdminChat.runAll.transaction")}</p>
                  {plan.implicit_commit ? <p className="mt-1">{t("dbAdminChat.warning.implicitCommit")}</p> : null}
                </Banner>
                <ExecutionConfirmationField
                  id={`${TEST_ID}-run-all-confirmation-${turn.turn_id}`}
                  value={runAllConfirmation}
                  onChange={setRunAllConfirmation}
                  confirmed={dbAdminChatConfirmed(plan, runAllConfirmation)}
                  expectedLabel={plan.confirmation_target}
                  placeholder={plan.confirmation_target}
                  helper={t("dbAdminChat.runAll.confirmationHelper", { target: plan.confirmation_target })}
                  disabled={running}
                  actions={runAllButton}
                />
              </>
            ) : null}
            {runAll ? <RunAllSummary runAll={runAll} turnId={turn.turn_id} /> : null}
          </section>
        ) : null}
        {plan.statements.map((statement) => (
          <DbAdminChatStatement
            key={`${statement.index}:${statement.sql}`}
            conversationId={conversationId}
            turn={turn}
            statement={statement}
            total={total}
            execution={executions.statements[statement.index] ?? null}
            busy={running}
            readOnly={readOnly}
            onRun={(confirmation) =>
              void execute({ mode: "statement", statement_index: statement.index }, confirmation)
            }
            onReplaceTurn={onReplaceTurn}
          />
        ))}
        <SchemaSyncNotice
          sync={schemaSync}
          testId={`${TEST_ID}-schema-sync-notice`}
          processingTestId={`${TEST_ID}-schema-refresh-processing`}
        />
        {turn.warnings.map((warning) => (
          <Banner key={warning} severity="warning">
            {warning}
          </Banner>
        ))}
        {/* 回答の注意書きは SQL と実行の結果の後（3 製品共通の AnswerNotice。#2405）。 */}
        <AnswerNotice notice={turn.notice} label={t("answer.notice.label")} />
      </ChatAnswer>
    </ChatTurn>
  );
}

/** 「すべて実行」の結果の要約（成功の数・止めた文・commit / rollback・警告）。文ごとの結果は各文の区画。 */
function RunAllSummary({ runAll, turnId }: { runAll: DbAdminChatRunAll; turnId: string }) {
  if (runAll.status === "running") {
    // スピナーは押した「すべて実行」のボタンが出す（同じ処理のスピナーは 1 つ。messaging.md §3.7）。
    return (
      <ProcessingIndicator
        active
        label={t("dbAdminChat.runAll.running")}
        operationKey={`db-admin-chat-run-all-${turnId}`}
        placement="result"
        activityIcon="none"
        testId={`${TEST_ID}-run-all-running`}
      />
    );
  }
  if (runAll.requestError) {
    return <ApiErrorBanner error={runAll.requestError} fallback={t("dbAdminChat.runAll.failed")} />;
  }
  const data = runAll.data;
  const saved = runAll.saved;
  const count = data ? data.statements.length : (saved?.statement_count ?? 0);
  const succeeded = data
    ? data.statements.filter((item) => item.status === "succeeded").length
    : (saved?.succeeded_count ?? 0);
  const notRun = data ? data.statements.filter((item) => item.status === "not_run").length : 0;
  const stoppedAt = data ? data.stopped_at : (saved?.stopped_at ?? null);
  const committed = data ? data.committed : Boolean(saved?.committed);
  const rolledBack = data ? data.rolled_back : Boolean(saved?.rolled_back);
  const warnings = data ? data.warnings : (saved?.warnings ?? []);
  const ok = runAll.status === "done";
  return (
    <div className="grid gap-2" data-testid={`${TEST_ID}-run-all-summary`}>
      {saved && !data ? (
        <p className="text-xs text-fg-muted">
          {t("dbAdminChat.runAll.last", { at: formatLastExecutedAt(saved.executed_at) })}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        {/* バッジは短い語にし（375px でも 1 行に収める）、数の内訳は下の文に書く。 */}
        <StatusBadge
          variant={ok ? "success" : "danger"}
          label={
            ok
              ? t("dbAdminChat.runAll.badge.succeeded")
              : t("dbAdminChat.runAll.badge.stopped", { index: stoppedAt ?? "-" })
          }
        />
        {committed ? <StatusBadge variant="success" label={t("dbAdmin.result.summary.committed")} /> : null}
        {rolledBack ? <StatusBadge variant="danger" label={t("dbAdmin.result.summary.rolledBack")} /> : null}
      </div>
      <p className="text-sm text-fg-muted">
        {ok
          ? t("dbAdminChat.runAll.succeeded", { count })
          : t("dbAdminChat.runAll.stopped", {
              index: stoppedAt ?? "-",
              succeeded,
              notRun: data ? notRun : Math.max(count - succeeded - 1, 0),
            })}
      </p>
      {warnings.map((warning) => (
        <Banner key={warning} severity="warning">
          {warning}
        </Banner>
      ))}
    </div>
  );
}

/** 1 文の区画（番号・分類・SQL・コピー・「実行」・確認語・その文の結果・失敗の分析）。 */
function DbAdminChatStatement({
  conversationId,
  turn,
  statement,
  total,
  execution,
  busy,
  readOnly,
  onRun,
  onReplaceTurn,
}: {
  conversationId: string;
  turn: DbAdminChatTurnData;
  statement: DbAdminChatStatementPlan;
  total: number;
  execution: DbAdminChatExecution | null;
  /** このターンのどれかの実行の途中（同時に 2 つを実行しない）。 */
  busy: boolean;
  /** 他の利用者の会話を見ている（実行・分析を出さない。#3244）。 */
  readOnly: boolean;
  onRun: (confirmation: string) => void;
  onReplaceTurn: (turn: DbAdminChatTurnData) => void;
}) {
  const [confirmation, setConfirmation] = useState("");
  const [analysis, setAnalysis] = useState<AnalysisState | null>(null);
  const [copyError, setCopyError] = useState("");
  const running = execution?.status === "running";
  const result = execution?.result ?? null;
  const executeError = execution?.requestError ?? null;
  const executed = Boolean(result && dbAdminExecutionSucceeded(result));
  // 開き直した SELECT の成功は行を持たない（要約だけ）。
  const savedSelect = !result?.select_result && executed && execution?.saved?.row_count != null ? execution.saved : null;
  const failed = Boolean(execution && execution.status === "failed" && (executeError || (result && !executed)));
  const canRun = dbAdminChatCanRunStatement(statement, confirmation) && !busy;
  const blocked = Boolean(statement.blocked_reason);
  // 参照する object に無い列で止めた文（#2352）は、AI のエラー分析で直せる。
  const unknownColumns = statement.unknown_columns.length > 0;

  const run = () => {
    if (!canRun) return;
    setAnalysis(null);
    // 実行の確認は 1 回ごと（もう一度実行するときは入力し直す）。
    const value = statement.requires_confirmation ? confirmation.trim() : "";
    setConfirmation("");
    onRun(value);
  };

  const analyze = async () => {
    setAnalysis({ status: "running" });
    try {
      // 失敗した文だけを分析に渡す（直した SQL はこの文だけを置き換える。#2333）。
      const data = await apiPost<DbAdminChatAnalyzeData>(
        `${conversationPath(conversationId)}/turns/${encodeURIComponent(turn.turn_id)}/analyze-error`,
        { statement_index: statement.index },
        { timeoutMs: API_TIMEOUT_MS.longRunningJob },
      );
      setAnalysis({ status: "done", data });
    } catch (error) {
      setAnalysis({ status: "failed", error });
    }
  };

  const applyCorrected = async (corrected: DbAdminChatPlan) => {
    // サーバーがこの文を置き換えて分類し直す（版が進み、確認語からやり直す）。
    const updated = await apiPut<DbAdminChatTurnData>(
      `${conversationPath(conversationId)}/turns/${encodeURIComponent(turn.turn_id)}/plan`,
      { sql: corrected.sql, statement_index: statement.index },
    );
    onReplaceTurn(updated);
  };

  const executeButton = (
    <Button
      type="button"
      variant={statement.requires_confirmation ? "danger" : "secondary"}
      size="sm"
      icon={Play}
      loading={running}
      disabled={!canRun}
      onClick={run}
      data-testid={`${TEST_ID}-execute`}
    >
      {execution && !running && execution.status !== "not_run" ? t("chat.execute.again") : t("chat.execute")}
    </Button>
  );

  return (
    <section
      aria-label={t("dbAdminChat.statement.region", { index: statement.index, total })}
      className={total > 1 ? "min-w-0 space-y-3 rounded-md border border-border p-3" : "min-w-0 space-y-3"}
      data-testid={`${TEST_ID}-statement`}
    >
      <ChatSqlBlock
        labels={SQL_BLOCK_LABELS}
        sql={statement.sql}
        testId={`${TEST_ID}-sql`}
        onCopied={() => setCopyError("")}
        onCopyError={setCopyError}
        badges={<StatementBadges statement={statement} total={total} />}
        // SELECT は SQL の横の「実行」で実行する（チャットと同じ位置）。確認語の要る文は確認語の欄の横。
        actions={!blocked && !statement.requires_confirmation && !readOnly ? executeButton : null}
      />
      {blocked ? (
        <Banner severity="danger" title={t("dbAdminChat.blocked.title")}>
          <p className="break-words">{statement.blocked_reason}</p>
          <p className="mt-2 text-xs">
            {t(unknownColumns ? "dbAdminChat.blocked.unknownColumnHint" : "dbAdminChat.blocked.hint")}
          </p>
        </Banner>
      ) : null}
      {statement.requires_confirmation ? (
        <>
          <Banner severity="warning">
            <p>{t("dbAdminChat.warning.mutation")}</p>
            {statement.implicit_commit ? <p className="mt-1">{t("dbAdminChat.warning.implicitCommit")}</p> : null}
          </Banner>
          {readOnly ? null : (
            <ExecutionConfirmationField
              id={`${TEST_ID}-confirmation-${turn.turn_id}-${statement.index}`}
              value={confirmation}
              onChange={setConfirmation}
              confirmed={dbAdminChatConfirmed(statement, confirmation)}
              expectedLabel={statement.confirmation_target}
              placeholder={statement.confirmation_target}
              helper={t("dbAdminChat.confirmation.helper", { target: statement.confirmation_target })}
              disabled={running}
              actions={executeButton}
            />
          )}
        </>
      ) : null}
      {execution ? (
        <section
          aria-label={t("chat.execute.region")}
          className="min-w-0 space-y-3 border-t border-border pt-3"
          data-testid={`${TEST_ID}-execution`}
        >
          {running ? (
            // スピナーは押した「実行」のボタンが出す（同じ処理のスピナーは 1 つ。messaging.md §3.7）。
            <ProcessingIndicator
              active
              label={t("chat.execute.running")}
              operationKey={`db-admin-chat-execute-${turn.turn_id}-${statement.index}`}
              placement="result"
              activityIcon="none"
              testId={`${TEST_ID}-execution-running`}
            />
          ) : null}
          {execution.status === "not_run" ? (
            <div className="flex flex-wrap items-center gap-2" data-testid={`${TEST_ID}-not-run`}>
              <StatusBadge variant="neutral" label={t("dbAdminChat.statement.notRun")} />
              <p className="text-sm text-fg-muted">{t("dbAdminChat.statement.notRunHint")}</p>
            </div>
          ) : null}
          {!running && executeError ? (
            <ApiErrorBanner
              error={executeError}
              fallback={t("dbAdminChat.execute.failed")}
              testId={`${TEST_ID}-execution-request-error`}
            />
          ) : null}
          {!running && result?.select_result && executed ? (
            <ChatQueryResult
              results={result.select_result}
              rowLimit={DEFAULT_SQL_ROW_LIMIT}
              elapsedMs={result.statements[0]?.elapsed_ms ?? result.timing.elapsed_ms}
              executedAt={new Date(execution.finishedAtMs ?? execution.startedAtMs).toISOString()}
              fullResult={{ hint: t("dbAdminChat.fullResultHint") }}
              testId={`${TEST_ID}-result`}
            />
          ) : null}
          {!running && savedSelect ? (
            // 開き直した会話の SELECT は、前回の実行の要約と「もう一度実行」（行は保存しない。チャットと同じ）。
            <ChatLastExecutionText
              summary={{
                status: "done",
                executed_at: savedSelect.executed_at,
                row_count: savedSelect.row_count ?? 0,
                column_count: savedSelect.column_count ?? 0,
                has_more: savedSelect.has_more,
              }}
              readOnly={readOnly}
              testId={`${TEST_ID}-last-execution`}
            />
          ) : null}
          {!running && result && !(result.select_result && executed) && !savedSelect ? (
            // 更新系の結果・失敗は管理 SQL を実行と同じ表示（状態・影響行数・commit / rollback）。
            <DbAdminExecutionResult result={result} hideStatementSql context="db_admin_chat" />
          ) : null}
        </section>
      ) : null}
      {(failed || unknownColumns) && !running && !readOnly ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            icon={Sparkles}
            loading={analysis?.status === "running"}
            onClick={() => void analyze()}
            data-testid={`${TEST_ID}-analyze`}
          >
            {t("dbAdminChat.analyze")}
          </Button>
        </div>
      ) : null}
      {analysis ? (
        <AnalysisPanel
          analysis={analysis}
          operationKey={`${turn.turn_id}-${statement.index}`}
          onUseCorrected={applyCorrected}
        />
      ) : null}
      {copyError ? <Banner severity="danger">{copyError}</Banner> : null}
    </section>
  );
}

function AnalysisPanel({
  analysis,
  operationKey,
  onUseCorrected,
}: {
  analysis: AnalysisState;
  operationKey: string;
  onUseCorrected: (plan: DbAdminChatPlan) => Promise<void>;
}) {
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<unknown>(null);
  const apply = async (plan: DbAdminChatPlan) => {
    setApplying(true);
    setApplyError(null);
    try {
      await onUseCorrected(plan);
    } catch (error) {
      setApplyError(error);
    } finally {
      setApplying(false);
    }
  };
  return (
    <section
      aria-label={t("dbAdminChat.analyze.title")}
      className="min-w-0 space-y-3 border-t border-border pt-3"
      data-testid={`${TEST_ID}-analysis`}
    >
      {analysis.status === "running" ? (
        <ProcessingIndicator
          active
          label={t("dbAdminChat.analyze.running")}
          operationKey={`db-admin-chat-analyze-${operationKey}`}
          placement="result"
          activityIcon="none"
          testId={`${TEST_ID}-analysis-running`}
        />
      ) : null}
      {analysis.status === "failed" ? (
        <ApiErrorBanner error={analysis.error} fallback={t("dbAdminChat.analyze.failed")} />
      ) : null}
      {analysis.status === "done" ? (
        <>
          <p className="text-sm font-medium text-fg">{t("dbAdminChat.analyze.title")}</p>
          {(["cause", "fix", "conclusion"] as const).map((part) => (
            <section key={part} className="grid min-w-0 gap-2" aria-label={t(`dbAdminChat.analyze.${part}`)}>
              <h3 className="text-sm font-medium text-fg">{t(`dbAdminChat.analyze.${part}`)}</h3>
              <MarkdownText text={analysis.data[part]} />
            </section>
          ))}
          {(analysis.data.previous_attempts?.length ?? 0) > 0 ? (
            <Disclosure summary={t("dbAdminChat.analyze.history", { count: analysis.data.previous_attempts.length })}>
              <div className="min-w-0 space-y-3">
                {analysis.data.previous_attempts.map((attempt) => (
                  <div key={`${attempt.statement_index}-${attempt.revision}`} className="min-w-0 space-y-2">
                    <p className="text-sm font-medium">{t("dbAdminChat.analyze.revision", { revision: attempt.revision })}</p>
                    <p className="break-words text-sm text-fg-muted">{attempt.error_summary}</p>
                    <ChatSqlBlock labels={SQL_BLOCK_LABELS} sql={attempt.sql} badges={null} />
                  </div>
                ))}
              </div>
            </Disclosure>
          ) : null}
          {analysis.data.corrected_plan && analysis.data.corrected_plan.statements.length > 0 ? (
            <>
              <p className="text-sm font-medium text-fg">{t("dbAdminChat.analyze.corrected")}</p>
              <ChatSqlBlock
                labels={SQL_BLOCK_LABELS}
                sql={analysis.data.corrected_plan.sql}
                testId={`${TEST_ID}-corrected-sql`}
                badges={<PlanBadges plan={analysis.data.corrected_plan} />}
                actions={
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    icon={Check}
                    loading={applying}
                    onClick={() => {
                      if (analysis.data.corrected_plan) void apply(analysis.data.corrected_plan);
                    }}
                    data-testid={`${TEST_ID}-use-corrected`}
                  >
                    {t("dbAdminChat.analyze.useCorrected")}
                  </Button>
                }
              />
              <p className="text-xs text-fg-muted">{t("dbAdminChat.analyze.correctedHint")}</p>
              {applyError ? (
                <ApiErrorBanner error={applyError} fallback={t("dbAdminChat.analyze.useCorrectedFailed")} />
              ) : null}
            </>
          ) : null}
          {analysis.data.warnings.map((warning) => (
            <Banner key={warning} severity="info">
              {warning}
            </Banner>
          ))}
        </>
      ) : null}
    </section>
  );
}
