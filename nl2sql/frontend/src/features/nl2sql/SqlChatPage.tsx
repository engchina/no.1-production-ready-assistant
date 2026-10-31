import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Play,
  RefreshCw,
} from "lucide-react";
import {
  ApiErrorBanner,
  ChatReadOnlyNotice,
  Banner,
  Button,
  ChatAnswer,
  AnswerNotice,
  ChatProgress,
  RunUsedModels,
  RunProcessDetails,
  ChatSkeleton,
  ChatSqlBlock,
  ChatTurn,
  ClarificationRecord,
  createOptimisticChatMessage,
  DEFAULT_PAGE_SIZE,
  Disclosure,
  EmptyState,
  MessageText,
  ProcessFailureNotice,
  SearchableSelectField,
  Skeleton,
  SqlSafetyBadge,
  TimedLoadingState,
  useChatAutoScroll,
  useChatHistoryActions,
  useChatHistoryPanel,
  useChatProgressTracker,
  withOptimisticChatStatus,
  chatAttachmentItems,
  type ChatAttachmentItem,
  type OptimisticChatMessage,
} from "@production-ready/ui";
import {
  useWorkspaceActive,
  useWorkspaceIdentity,
  useWorkspaceState,
} from "@/components/WorkspaceState";
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  isTransportError,
} from "@/lib/api";
import { useSearchParams } from "react-router-dom";
import { t } from "@/lib/i18n";
import { randomUuid } from "@/lib/randomUuid";
import { API_TIMEOUT_MS } from "@/lib/requestPolicy";
import { useAuth } from "@/features/security/AuthProvider";
import {
  CAPABILITY_PERMISSIONS,
  MENU_PERMISSIONS,
} from "@/features/security/menu-permissions";
import {
  ChatSqlExecutionResult,
  useChatSqlExecution,
} from "./components/ChatSqlExecution";
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
import {
  ChatRequirementTurn,
  useChatRequirementFlow,
} from "./components/ChatRequirementTurn";
import { requirementClarificationLabels } from "./components/RequirementClarification";
import {
  decodeChatRequirementCheck,
  encodeChatRequirementCheck,
  type ChatJobRequest,
  type ChatRequirementCheck,
} from "./chatRequirement";
import { useNl2SqlChatAttachments } from "./useNl2SqlChatAttachments";
import { chatJobFailure, processFailureLabels } from "./processFailure";
import {
  useProfileUsageContext,
  useProfileSummaries,
} from "./incrementalQueries";
import {
  CHAT_PROGRESS_LABELS,
  chatJobElapsedMs,
  chatQueueProgressSteps,
  jobTracePath,
} from "./chatProgress";
import { ProgressLegacyNotice } from "./components/ProgressLegacyNotice";
import { useNl2SqlTraceProgress } from "./useTraceProgress";
import type { ChatHistoryOffsetPage, JobCreateData, JobData, Nl2SqlEngine } from "./types";

interface Conversation {
  id: string;
  title: string;
  profile_id: string;
  created_at: string;
  /**
   * 見ている利用者の会話か。system_admin が他の利用者の会話を見るときは false（#1889）。
   * system_admin は他の利用者の会話でも続きの質問・SQL の実行ができる（#1928）。
   */
  owned?: boolean;
  /** system_admin の一覧だけに付く利用者の表示名（引けない利用者は空。#1889・#1696）。 */
  actor_display_name?: string;
}
/** 会話の履歴の 1 ページ（新しい順。offset 型。#1265 / #1891）。 */
type ConversationPage = ChatHistoryOffsetPage<Conversation>;
interface ConversationData {
  conversation: Conversation;
  turns: JobData[];
}
const inFlight = (job: JobData | undefined) =>
  job?.status === "pending" || job?.status === "running";
/** 会話の取り直しの間隔。 */
const CHAT_POLL_INTERVAL_MS = 1500;
/**
 * 会話の取り直しが届かないまま、この時間が過ぎたら取り直す（#1160）。会話の取り直しは
 * {@link CHAT_POLL_INTERVAL_MS} ごとなので、その数回分。応答しない取得を打ち切って新しく取り直す。
 */
const CHAT_PROGRESS_STALE_AFTER_MS = 10_000;
const errorMessage = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

/** 送った質問（#907）と、その送信の内容。 */
interface PendingSend {
  message: OptimisticChatMessage;
  request: ChatJobRequest;
  /** 質問に添付したファイル（#2517。送った質問の下に出す）。 */
  attachments: ChatAttachmentItem[];
}

/**
 * SQL の生成のジョブを投入する。job ID は送信の前に画面が決める（#900）。
 *
 * 投入の応答が上限までに届かない（timeout・通信断）ときも、backend はジョブを作り終えていることが
 * ある。そのときは同じ ID でジョブを取り直し、会話に表示して生成の完了を待つ（二重に送らない）。
 * 再送信も同じ ID で送るので、backend が作り終えていれば作成済みのジョブが返る（二重に生成しない）。
 */
async function submitChatJob(
  request: ChatJobRequest,
): Promise<JobCreateData | JobData> {
  const clientJobId = request.client_job_id;
  try {
    return await apiPost<JobCreateData>(
      "/api/nl2sql/jobs",
      // チャットも SQL 生成の画面と同じく、生成した SQL を同じジョブで実行する（#1176）。実行の権限が
      // 無い利用者は backend が生成だけにする。生成の prompt には公開版のオントロジーを使い、画面に出さない
      // 生成後の接地確認は求めない（#1172）。
      { ...request, chat: true, use_ontology_context: true, include_ontology_grounding: false },
      { timeoutMs: API_TIMEOUT_MS.jobSubmit },
    );
  } catch (cause) {
    if (!isTransportError(cause)) throw cause;
    try {
      return await apiGet<JobData>(
        `/api/nl2sql/jobs/${encodeURIComponent(clientJobId)}`,
        { timeoutMs: API_TIMEOUT_MS.interactiveDetail },
      );
    } catch {
      throw cause;
    }
  }
}

/** SQL の生成の方式（OCI Enterprise AI の 1 つだけ）。 */
const CHAT_ENGINE: Nl2SqlEngine = "enterprise_ai_direct";

/**
 * 1 往復を永続ジョブにし、前文はサーバーで復元する。SQL 生成の画面と同じく、送信のジョブが生成・安全性の
 * 確認・実行まで行う（#1176）。結果の行は会話に残さず、画面が 1 回だけ受け取る。
 */
export function SqlChatPage() {
  const active = useWorkspaceActive();
  const { hasPermission, user } = useAuth();
  // 「AI 活用」の規則（#1889）: system_admin は全員の会話を見られ、履歴に利用者を出す。
  const seesAllUsers = Boolean(user?.is_system_admin);
  // 生成した SQL の実行（#1154）は SQL 生成の画面・SELECT SQL の実行と同じ実行の権限が要る。
  const canExecuteSql = hasPermission(CAPABILITY_PERMISSIONS.sqlExecute);
  const canOpenDirectSql = hasPermission(MENU_PERMISSIONS.directSql);
  const identity = useWorkspaceIdentity();
  const queryClient = useQueryClient();
  const [profileId, setProfileId] = useWorkspaceState("profileId", "");
  const [conversationId, setConversationId] = useWorkspaceState(
    "conversationId",
    "",
  );
  // 実行の詳細の「会話を開く」（`?profile_id=&conversation_id=`。#2343）から開いた会話。URL の会話を一度だけ選び
  // （作業状態に残す）、URL からは外す（読み込み直しや戻るで、選び直した会話を URL の会話に戻さない）。
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedConversationId = searchParams.get("conversation_id");
  const linkedProfileId = searchParams.get("profile_id");
  const [appliedLink, setAppliedLink] = useState<string | null>(null);
  if (linkedConversationId && linkedConversationId !== appliedLink) {
    setAppliedLink(linkedConversationId);
    if (linkedProfileId) setProfileId(linkedProfileId);
    setConversationId(linkedConversationId);
  } else if (!linkedConversationId && appliedLink) {
    setAppliedLink(null);
  }
  useEffect(() => {
    if (!linkedConversationId) return;
    const next = new URLSearchParams(searchParams);
    next.delete("conversation_id");
    next.delete("profile_id");
    setSearchParams(next, { replace: true });
  }, [linkedConversationId, searchParams, setSearchParams]);
  const [draft, setDraft] = useWorkspaceState(
    `draft:${profileId}:${conversationId}`,
    "",
  );
  const [historyPanelOpen, setHistoryPanelOpen] = useWorkspaceState(
    "historyOpen",
    false,
  );
  // 開閉の判定・シートの開閉は共通の useChatHistoryPanel（3 製品共通。#1161）。
  const historyPanel = useChatHistoryPanel({
    inlineOpen: historyPanelOpen,
    onInlineOpenChange: setHistoryPanelOpen,
    id: "sql-chat-history",
  });
  const [profileSearch, setProfileSearch] = useState("");
  // 送った質問（#907）。ジョブの投入の応答を待たずに会話の欄の末尾へ出し、投入できたら会話のジョブに置き換える。
  // 投入できなかったときは残して「再送信」を出す。
  const [pendingSend, setPendingSend] = useState<PendingSend | null>(null);
  // 送信した時刻（job ID ごと）。処理の経過の経過時間を、送信から数え続ける（#1176。送信の応答の後に
  // ジョブの作成の時刻から数え直さない）。画面を開き直したときはジョブの作成の時刻から数える。
  const [sentAtByJob, setSentAtByJob] = useState<Record<string, number>>({});
  const pending = pendingSend?.message ?? null;
  // 会話の添付（#2517。運用設定で有効なときだけ入力欄に出す）。添付の表は生成方法「Enterprise AI」だけが使う。
  const chatAttachments = useNl2SqlChatAttachments();
  // 送信の確認（回答前の確認）の間も、送った質問の添付を持つ（client_job_id ごと）。
  const [attachmentsByJob, setAttachmentsByJob] = useState<Record<string, ChatAttachmentItem[]>>({});
  // 送った質問の要件の検査・確認（#1442）。SQL を作る前に自動で行い、確認が要るときは会話の中で 1 問ずつ聞く。
  // 送った質問と session の ID は作業状態に残し、再読込・画面の移動の後も同じ確認を続ける。
  // 作業状態は既定値と同じ型の値だけを戻すので、JSON の文字列で持つ（空は確認なし）。
  const [storedRequirementCheck, setStoredRequirementCheck] = useWorkspaceState(
    "requirementCheck",
    "",
  );
  const requirementCheck = useMemo(
    () => decodeChatRequirementCheck(storedRequirementCheck),
    [storedRequirementCheck],
  );
  const setRequirementCheck = useCallback(
    (check: ChatRequirementCheck | null) =>
      setStoredRequirementCheck(encodeChatRequirementCheck(check)),
    [setStoredRequirementCheck],
  );
  const composerRef = useRef<HTMLTextAreaElement>(null);
  // 確かめた質問の文（job ID ごと）。投入したジョブを会話のキャッシュに入れるときに付け、回答の「回答前の確認で
  // 確かめた質問」を会話の取り直しを待たずに出す（後から増えて末尾の追従が外れない）。
  const requirementQuestionByJob = useRef<Record<string, string>>({});
  const sendingRef = useRef(false);
  const profiles = useProfileSummaries(profileSearch);
  const profileOptions =
    profiles.data?.pages.flatMap((page) => page.items) ?? [];
  if (!profileId && !profileSearch && profileOptions[0])
    setProfileId(profileOptions[0].id);
  const selectedProfileId = profileId;
  // 最初の読み込みの間だけ欄の形の Skeleton を出す（候補の検索中は欄を出したまま一覧の中で示す）。
  const profilesLoading = profiles.isPending && !profileSearch;
  const detail = useProfileUsageContext(selectedProfileId);
  const selectedProfile =
    detail.data?.profile ??
    profileOptions.find((profile) => profile.id === selectedProfileId);
  const chatKey = [
    "nl2sql",
    "chats",
    identity.owner,
    identity.context,
  ] as const;
  const conversationKey = [...chatKey, conversationId] as const;
  // 会話の履歴は 3 製品と同じく 1 ページずつ出す（offset 型。#1265 / #1891）。ページの位置は作業状態に残す
  // （再読込・画面の移動で同じページに戻る）。
  const [historyOffset, setHistoryOffset] = useWorkspaceState<number>("historyOffset", 0);
  const historyPageOffset = normalizeHistoryOffset(historyOffset);
  const history = useQuery({
    // 会話の key（[...chatKey, 会話の ID]）と重ならないよう、一覧は object の要素で分ける。
    queryKey: [...chatKey, { list: historyPageOffset }],
    enabled: active,
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({
        limit: String(DEFAULT_PAGE_SIZE),
        offset: String(historyPageOffset),
      });
      return apiGet<ConversationPage>(`/api/nl2sql/chats?${params.toString()}`, {
        signal,
        timeoutMs: API_TIMEOUT_MS.interactiveList,
      });
    },
    // 次のページを取得している間は、表示中のページを出したままにする。
    placeholderData: keepPreviousData,
  });
  const conversation = useQuery({
    queryKey: conversationKey,
    enabled: active && Boolean(conversationId),
    queryFn: async ({ signal }) =>
      apiGet<ConversationData>(
        `/api/nl2sql/chats/${encodeURIComponent(conversationId)}`,
        { signal, timeoutMs: API_TIMEOUT_MS.interactiveDetail },
      ),
    refetchInterval: (query) =>
      active && query.state.data?.turns.some(inFlight)
        ? CHAT_POLL_INTERVAL_MS
        : false,
  });
  // 処理の経過の取り直し（#1160）。応答しない取得（取得中は interval が次を始めない）を打ち切って取り直す。
  const refetchConversation = conversation.refetch;
  const refreshConversation = () =>
    refetchConversation({ cancelRefetch: true, throwOnError: false });
  const turns = conversation.data?.turns ?? [];
  // 開いている会話が他の利用者の物か（system_admin だけが開ける。backend の `owned`）。続きの質問・実行・
  // 名前の変更は持ち主だけなので、入力欄と「実行」を出さず閲覧だけの案内にする（停止・削除は出す。#3244）。
  const readOnly =
    seesAllUsers && Boolean(conversationId) && conversation.data?.conversation.owned === false;
  const latest = turns.at(-1);
  const generating = turns.some(inFlight);
  // 応答が届かなかった送信のジョブが、取り直した会話に入っていたら（backend は作り終えていた）、
  // 送った質問の表示を外す（質問を二重に出さず、再送信で二重に生成しない）。
  if (
    pendingSend &&
    turns.some((turn) => turn.job_id === pendingSend.request.client_job_id)
  )
    setPendingSend(null);
  const send = useMutation({
    mutationFn: (request: ChatJobRequest) => submitChatJob(request),
    onSuccess: async (job, variables) => {
      const { question, client_job_id: clientJobId } = variables;
      const id = conversationId || job.job_id;
      // 応答の本文は保存しない。会話 ID と未送信の草稿だけを一時保存する。
      // 草稿は送信の時点で空にしている（投入中に書き始めた次の質問は消さない。#907）。
      setProfileId(selectedProfileId);
      setConversationId(id);
      // 送った質問を、投入できたジョブ（job ID はサーバーと同じ client_job_id）に置き換える。二重に出さない。
      setPendingSend(null);
      queryClient.setQueryData<ConversationData>(
        [...chatKey, id],
        (previous) => ({
          conversation: previous?.conversation ?? {
            id,
            title: question,
            profile_id: selectedProfileId,
            created_at: job.created_at,
          },
          turns: [
            ...(previous?.turns ?? []),
            {
              ...job,
              // worker が受け取るまで span は無い（処理の経過は開始待ちの仮の行。#3226）。
              question,
              requirement_question: requirementQuestionByJob.current[clientJobId] ?? "",
              requirement_skipped: Boolean(variables.requirement_skipped),
              profile_id: selectedProfileId,
              conversation_id: id,
              chat: true,
              generation_only: !canExecuteSql,
            },
          ],
        }),
      );
      await queryClient.invalidateQueries({ queryKey: chatKey });
    },
    onError: () => {
      // 送った質問は残し、理由と「再送信」を出す（入力を失わない。#907）。
      setPendingSend((current) =>
        current
          ? {
              ...current,
              message: withOptimisticChatStatus(current.message, "failed"),
            }
          : current,
      );
      // 応答が届かなくてもジョブが作られていることがある。履歴と会話を取り直して表示に反映する。
      void queryClient.invalidateQueries({ queryKey: chatKey });
    },
    onSettled: () => {
      sendingRef.current = false;
    },
  });
  const stop = useMutation({
    mutationFn: () =>
      apiPost<JobData>(
        `/api/nl2sql/jobs/${turns.find(inFlight)!.job_id}/cancel`,
        {},
        { timeoutMs: API_TIMEOUT_MS.interactiveDetail },
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: conversationKey });
    },
  });
  /** 要件の検査・確認を終えて、SQL の生成のジョブを投入する（`sessionId` が無ければ確認せずに作る）。 */
  const submitJob = (
    check: ChatRequirementCheck,
    sessionId: string | null,
    requirementQuestion?: string,
    skipped = false,
  ) => {
    const request: ChatJobRequest = sessionId
      ? { ...check.request, requirement_session_id: sessionId }
      : skipped
        ? { ...check.request, requirement_skipped: true }
        : check.request;
    if (sessionId && requirementQuestion)
      requirementQuestionByJob.current[request.client_job_id] = requirementQuestion;
    setRequirementCheck(null);
    if (sendingRef.current) return;
    sendingRef.current = true;
    // 生成の経過は投入の時刻から数える（確認に答えていた時間を生成の時間に含めない）。
    const message = createOptimisticChatMessage(request.question);
    setSentAtByJob((current) => ({ ...current, [request.client_job_id]: message.sentAtMs }));
    setPendingSend({ message, request, attachments: attachmentsByJob[request.client_job_id] ?? [] });
    autoScroll.scrollToLatest();
    send.mutate(request);
    // 確認のカードの「この内容で SQL を生成して実行」で投入したときは、押したボタンが消えてフォーカスが
    // 失われるので、送信の直後と同じく入力欄へ戻す（#1761。ほかの所にあるフォーカスは動かさない）。
    const focused = document.activeElement;
    if (
      !focused ||
      focused === document.body ||
      focused.closest('[data-testid="sql-chat-requirement-turn"]')
    ) {
      requestAnimationFrame(() => composerRef.current?.focus());
    }
  };
  const requirementFlow = useChatRequirementFlow({
    check: requirementCheck,
    setCheck: setRequirementCheck,
    onSubmit: submitJob,
    // 確認を中止したら、送った質問を入力欄に戻す（書き始めた次の質問は上書きしない）。
    onCancelled: (check) => {
      setRequirementCheck(null);
      if (!draft.trim()) setDraft(check.request.question);
      requestAnimationFrame(() => composerRef.current?.focus());
    },
    enabled: active,
  });
  const busy = send.isPending || stop.isPending;
  // 使える業務プロファイルが無いときは、RAG・Agent のチャットと同じく会話の欄を出さない。
  const noProfiles =
    profiles.isSuccess &&
    profileOptions.length === 0 &&
    !profileSearch &&
    !selectedProfile;
  // 業務プロファイルの一覧を読めなかった（最初の読み込み）。上のカードに失敗と再試行を出し、会話の欄は出さない
  // （空の状態を出さない。messaging.md §11.7、#1153）。候補の検索の失敗は欄の中で示す。
  const profilesFailed =
    profiles.isError && !profiles.data && !profileSearch && !selectedProfile;
  // 開いている会話の内容を読み込んでいる（送った質問を出している間は除く）。
  const conversationLoading =
    Boolean(conversationId) && conversation.isPending && !pending;
  // 画面の前提（業務プロファイルの一覧・開いている会話の内容）が揃うまでは、会話の欄に空の状態を出さず
  // 会話の形の Skeleton で覆い、送信を止める（`blocked`。#1153）。入力欄・新しい会話・履歴の開閉を
  // 無効にするのは業務プロファイルの一覧の読み込み中だけ。会話の内容の読み込み中は入力欄に書ける
  // （書いている途中で無効にしてフォーカスと入力を失わせない。messaging.md §11.7、#1188）。
  const blocked =
    busy ||
    generating ||
    profilesLoading ||
    !selectedProfile ||
    detail.isError ||
    (Boolean(conversationId) &&
      (conversation.isPending || conversation.isError)) ||
    // 要件の確認の間は次の質問を送らない（確認に答えるか、中止してから送る。#1442）。
    Boolean(requirementCheck) ||
    turns.length >= 50 ||
    chatAttachments.uploading;
  function submit() {
    const question = draft.trim();
    if (!question || blocked || sendingRef.current) return;
    sendingRef.current = true;
    // 添付は入力欄から外し、送った質問の下に出す（#2517）。
    const sentAttachments = chatAttachments.take();
    const request: ChatJobRequest = {
      question,
      profile_id: selectedProfileId,
      engine: CHAT_ENGINE,
      previous_job_id: latest?.job_id ?? null,
      client_job_id: randomUuid(),
      ...(sentAttachments.ids.length ? { attachment_ids: sentAttachments.ids } : {}),
    };
    if (sentAttachments.items.length) {
      setAttachmentsByJob((current) => ({ ...current, [request.client_job_id]: sentAttachments.items }));
    }
    // 送った質問はすぐ会話の欄に出し、入力欄を空にする（#907）。SQL を作る前に、要件の検査を自動で行う
    // （#1442。確認が要らなければそのまま SQL を作り、要れば会話の中で 1 問ずつ聞く）。
    sendingRef.current = false;
    const sent = createOptimisticChatMessage(question);
    const check: ChatRequirementCheck = { request, sentAtMs: sent.sentAtMs, sessionId: "" };
    setDraft("");
    // 業務プロファイルの方針が「前提を示して SQL を生成する」なら確認事項を聞かずに作る（#1839）。
    if (detail.data?.profile.clarification_policy === "assume") {
      submitJob(check, null);
      return;
    }
    setRequirementCheck(check);
    // 送った質問は会話の欄の末尾に出す。上を読んでいても末尾へ戻る（messaging.md §11.1）。
    autoScroll.scrollToLatest();
  }
  /**
   * 送信できなかった質問を、そのままもう一度送る（#907）。同じ job ID・同じ内容で送るので、
   * 応答が届かなかっただけで backend がジョブを作り終えていれば、作成済みのジョブが返る（#900）。
   */
  function resend() {
    if (!pendingSend || blocked || sendingRef.current) return;
    sendingRef.current = true;
    setPendingSend({
      ...pendingSend,
      message: withOptimisticChatStatus(pendingSend.message, "sending"),
    });
    send.mutate(pendingSend.request);
  }
  function resetConversation() {
    if (busy) return;
    requirementFlow.discard();
    setConversationId("");
    setPendingSend(null);
    send.reset();
    stop.reset();
  }
  function openConversation(item: Conversation) {
    if (busy) return;
    requirementFlow.discard();
    setProfileId(item.profile_id);
    setConversationId(item.id);
    historyPanel.closeSheet();
    setPendingSend(null);
    send.reset();
    stop.reset();
  }
  // 会話の欄の自動スクロール（3 製品共通の useChatAutoScroll。#1161）。keep-alive で隠れている間は動かさない。
  // 末尾を見ている間は新しいジョブ・状態の変化に合わせて末尾へ追い、上を読んでいる間は「最新へ」を出す。
  const autoScroll = useChatAutoScroll({
    contentKey: [
      turns.length,
      latest?.status ?? "",
      pending?.localId ?? "",
      pending?.status ?? "",
      // 要件の確認の質問・まとめが変わったら末尾へ追う（#1442）。
      requirementCheck?.request.client_job_id ?? "",
      requirementFlow.scrollKey,
      requirementFlow.controller.session?.current_intent_version ?? "",
      // 回答前に確認した内容（#1839）は会話の取り直しで届くので、届いたら末尾へ追う。
      latest?.requirement_answers?.length ?? 0,
      latest?.requirement_skipped ? "skipped" : "",
    ].join(":"),
    resetKey: `${selectedProfileId}:${conversationId}`,
    enabled: active,
  });
  // 会話の名前の変更・削除（3 製品共通の useChatHistoryActions。#1817）。SQL の生成中の会話は backend が
  // 削除しない（409）。消せた後に開いている会話なら新しい会話に戻す。
  const historyActions = useChatHistoryActions({
    onRename: async (id, title) => {
      await apiPatch(`/api/nl2sql/chats/${encodeURIComponent(id)}`, { title });
      await queryClient.invalidateQueries({ queryKey: chatKey });
    },
    onDelete: (id) => apiDelete(`/api/nl2sql/chats/${encodeURIComponent(id)}`),
    onDeleted: (item) => {
      if (item.id === conversationId) resetConversation();
      queryClient.removeQueries({ queryKey: [...chatKey, item.id] });
      void queryClient.invalidateQueries({ queryKey: chatKey });
    },
    disabled: busy,
    // 他の利用者の会話（system_admin が見る）の名前は持ち主だけが変えられる。削除は出す（#3244）。
    canRename: (item) => !othersConversationIds(history.data).has(item.id),
  });
  // 一覧の行・読み込み中・失敗・0 件・ページングは DB 管理チャットと共通の Nl2SqlChatHistoryList
  // （3 製品共通の ChatHistoryList。#1161・#2319）。
  const historyContent = (
    <Nl2SqlChatHistoryList
      page={history.data}
      loading={history.isPending}
      error={history.isError ? history.error : null}
      onRetry={() => void history.refetch()}
      retrying={history.isFetching}
      currentId={conversationId || null}
      onSelect={openConversation}
      disabled={busy}
      seesAllUsers={seesAllUsers}
      offset={historyPageOffset}
      onOffsetChange={setHistoryOffset}
      actions={historyActions}
      testIdPrefix="sql-chat"
    />
  );
  const targetSelector = (<>

          {profilesLoading ? (
            <TimedLoadingState
              label={t("chat.profile.loading")}
              operationKey="sql-chat-profiles-load"
              framed={false}
              testId="sql-chat-profiles-loading"
            >
              {/* ラベルと欄の寸法を予約する。 */}
              <div className="space-y-1.5" aria-hidden>
                <Skeleton className="h-4 w-32" />
                <Skeleton className="h-[var(--button-height-md)] w-full" />
              </div>
            </TimedLoadingState>
          ) : profilesFailed ? (
            <ApiErrorBanner
              error={profiles.error}
              fallback={t("chat.profile.loadFailed")}
              testId="sql-chat-profiles-error"
              action={
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  icon={RefreshCw}
                  loading={profiles.isFetching}
                  onClick={() => void profiles.refetch()}
                >
                  {t("chat.retry")}
                </Button>
              }
            />
          ) : noProfiles ? (
            <EmptyState
              title={t("chat.noProfiles")}
              hint={t("chat.noProfilesHint")}
            />
          ) : (
            <SearchableSelectField
              id="sql-chat-profile"
              labelPlacement="compact"
              size="sm"
              label={t("chat.profile")}
              required
              value={selectedProfileId}
              width="full"
              placeholder={t("chat.profile.placeholder")}
              options={profileOptions.map((profile) => ({
                value: profile.id,
                label: profile.name,
                description: profile.description || undefined,
                meta: t("chat.profile.objectCount", {
                  count:
                    profile.allowed_table_count + profile.allowed_view_count,
                }),
              }))}
              selectedOption={
                selectedProfile
                  ? { value: selectedProfile.id, label: selectedProfile.name }
                  : null
              }
              onQueryChange={setProfileSearch}
              remote={{
                total: profiles.data?.pages[0]?.total ?? 0,
                searching:
                  profiles.isFetching && !profiles.isFetchingNextPage,
                hasMore: Boolean(profiles.hasNextPage),
                loadingMore: profiles.isFetchingNextPage,
                onLoadMore: () => void profiles.fetchNextPage(),
              }}
              disabled={busy}
              onValueChange={(id) => {
                setProfileId(id);
                resetConversation();
              }}
              labels={{
                searchPlaceholder: t("chat.profile.searchPlaceholder"),
                clearSearch: t("common.clearSearch"),
                count: (shown, total) =>
                  t("chat.profile.count", { shown, total }),
                noMatch: (query) => t("chat.profile.noMatch", { query }),
                empty: t("chat.profile.emptyList"),
                searching: t("chat.profile.searching"),
                loadMore: t("chat.profile.loadMore"),
              }}
              error={
                profiles.isError || detail.isError
                  ? errorMessage(
                      profiles.error || detail.error,
                      t("profiles.error.load"),
                    )
                  : undefined
              }
            />
          )}

  </>);
  return (
    // 見出しと本文の高さの渡し方は DB 管理チャットと共通の Nl2SqlChatPage（#2332）。
    <Nl2SqlChatPage title={t("nav.chat")} subtitle={t("chat.subtitle")}>

      {noProfiles || profilesFailed ? targetSelector : null}
      {noProfiles || profilesFailed ? null : (
        // 骨格（履歴のパネル・シート、会話の領域の上端の行・会話の欄・入力欄の領域）は 3 製品共通の ChatLayout（#1161）。
        // NL2SQL の文言は DB 管理チャットと共通の Nl2SqlChatLayout が付ける（#2264）。
        <Nl2SqlChatLayout
          targetSelector={targetSelector}
          history={{
            panel: historyPanel,
            content: historyContent,
            toggleDisabled: profilesLoading,
            historyDays: history.data?.history_days,
          }}
          label={t("chat.conversation")}
          // 会話を選んでいる間は、履歴を閉じていても今の会話の名前を出す。
          conversationTitle={
            conversationId ? conversation.data?.conversation.title : null
          }
          conversationTitleLoading={
            Boolean(conversationId) && conversation.isPending
          }
          onNewConversation={resetConversation}
          newConversationDisabled={busy || profilesLoading}
          autoScroll={autoScroll}
          testIdPrefix="sql-chat"
          composer={
            readOnly ? (
              // 入力欄の代わりの閲覧だけの案内（3 製品共通の ChatReadOnlyNotice。#3244）。停止は生成中だけ出す。
              <ChatReadOnlyNotice
                onStop={
                  generating
                    ? () => {
                        if (!stop.isPending && !conversation.isError) stop.mutate();
                      }
                    : undefined
                }
                stopping={stop.isPending}
                footer={
                  stop.isError ? (
                    <ApiErrorBanner
                      error={stop.error}
                      fallback={t("chat.stopFailed")}
                      testId="sql-chat-stop-error"
                    />
                  ) : null
                }
                testIdPrefix="sql-chat-read-only"
              />
            ) : (
              <>
                {/* 入力欄の領域（設定の行・入力欄と送信 / 停止）は 3 製品共通の ChatComposer（#1161）。
                    業務プロファイルの一覧の読み込み中は書けない（書いた文字は作業状態に残す。#1153）。
                    会話の内容の読み込み中は書けて、送信だけを止める（#1188）。 */}
                <Nl2SqlChatComposer
                  id="sql-chat-composer"
                  value={draft}
                  onValueChange={setDraft}
                  onSubmit={submit}
                  onStop={() => {
                    // 要件の検査の最中は、確認を中止する（質問は入力欄に戻す。#1442）。
                    if (requirementFlow.running) {
                      requirementFlow.cancel();
                      return;
                    }
                    if (!stop.isPending && !conversation.isError) stop.mutate();
                  }}
                  running={generating || requirementFlow.running}
                  textareaRef={composerRef}
                  submitBlocked={blocked}
                  disabled={profilesLoading}
                  sendTestId="sql-chat-send"
                  // 会話の添付（#2517。運用設定で有効なときだけ）。
                  attachments={chatAttachments.composer}
                  footer={
                    <>
                      {stop.isError ? (
                        <ApiErrorBanner
                          error={stop.error}
                          fallback={t("chat.stopFailed")}
                          testId="sql-chat-stop-error"
                        />
                      ) : null}
                      {turns.length >= 50 ? (
                        <Banner severity="info">{t("chat.limit")}</Banner>
                      ) : null}
                    </>
                  }
                />
              </>
            )
          }
        >
          {conversationLoading ? (
            // 会話の内容の読み込み中は文言と経過時間を出し、会話の形の Skeleton で寸法を予約する
            // （3 製品で同じ。messaging.md §3.6 / §11.7）。
            <TimedLoadingState
              label={t("chat.loading")}
              operationKey="sql-chat-conversation-load"
              framed={false}
              testId="sql-chat-conversation-loading"
            >
              <ChatSkeleton />
            </TimedLoadingState>
          ) : profilesLoading ? (
            // 業務プロファイルの一覧の読み込み中は、空の状態（はじめの案内）を出さず会話の形の Skeleton で覆う。
            // 経過時間は上のカードが出しているので重ねない（同じ取得の経過時間は 1 か所。#1153）。
            <ChatSkeleton testId="sql-chat-conversation-skeleton" />
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
          {!conversationId && !pending && !requirementCheck && !profilesLoading ? (
            <EmptyState
              title={t("chat.empty")}
              hint={t("chat.emptyHint")}
            />
          ) : null}
          {turns.map((turn) => (
            <SqlChatTurn
              key={turn.job_id}
              turn={turn}
              canExecuteSql={canExecuteSql}
              canOpenDirectSql={canOpenDirectSql}
              readOnly={readOnly}
              sentAtMs={sentAtByJob[turn.job_id]}
              tracking={active}
              receivedAt={conversation.dataUpdatedAt}
              refresh={refreshConversation}
            />
          ))}
          {requirementCheck ? (
            // 送った質問の要件の検査・会話の中の確認・まとめ（#1442）。SQL 生成の画面の回答前の確認と同じ部品と API。
            <ChatRequirementTurn flow={requirementFlow} canExecuteSql={canExecuteSql} />
          ) : null}
          {pending ? (
            // 送った質問はジョブの投入の応答を待たずに出す。失敗しても残す（#907）。
            // 1 往復の入れ物・送信中の回答の場所・失敗の出し方は 3 製品共通の ChatPendingTurn（#1161）。
            <Nl2SqlPendingTurn
              message={pending}
              attachments={pendingSend?.attachments}
              error={send.isError ? send.error : null}
              fallback={t("chat.sendFailed")}
              onResend={resend}
              resendDisabled={blocked}
              testIdPrefix="sql-chat"
            />
          ) : null}
        </Nl2SqlChatLayout>
      )}
    </Nl2SqlChatPage>
  );
}
function SqlChatTurn({
  turn,
  canExecuteSql,
  canOpenDirectSql,
  readOnly,
  sentAtMs,
  tracking,
  receivedAt,
  refresh,
}: {
  turn: JobData;
  canExecuteSql: boolean;
  canOpenDirectSql: boolean;
  /** 他の利用者の会話を見ている（system_admin）。「実行」を出さない（実行は持ち主だけ。#3244）。 */
  readOnly: boolean;
  /** この画面で送信した時刻（処理の経過の経過時間の起点。#1176）。開き直した会話には無い。 */
  sentAtMs?: number;
  /** 画面が表示されている（keep-alive で隠れていない）。 */
  tracking: boolean;
  /** 会話を最後に取得できた時刻（TanStack Query の dataUpdatedAt）。 */
  receivedAt: number;
  /** 会話を取り直す（応答しない取得は打ち切る）。 */
  refresh: () => Promise<unknown>;
}) {
  const [copyError, setCopyError] = useState("");
  const result = turn.result;
  const running = inFlight(turn);
  const jobId = turn.job_id;
  // 処理の経過は実行の span の木（3 製品共通。#3226 の案 A）。span は worker が共通の span の表に書くので、処理中は
  // ジョブの処理の経過の API（span の表の snapshot）を読み直し、段の中の span（モデルの呼び出しなど）まで実行中に出す。
  // 最終の状態は会話のターン（ジョブの状態）が正本。snapshot の終端が先に届いたら会話を取り直す。読み直しが失敗して
  // いる間は「接続を確認しています」を出す（前の内容は残す）。
  const progress = useNl2SqlTraceProgress({
    targetId: jobId,
    path: jobTracePath(jobId),
    active: running && tracking,
  });
  const snapshotTerminal = progress.terminal;
  useEffect(() => {
    if (running && snapshotTerminal) void refresh();
  }, [running, snapshotTerminal, refresh]);
  // worker が受け取るまでは span が無いので、開始待ちの仮の行を出す（最初の span で木に置き換わる。#3226）。
  const queuePlaceholder = progress.steps.length === 0 && running;
  const progressSteps = queuePlaceholder ? chatQueueProgressSteps(turn.created_at) : progress.steps;
  // 引き継いだ実行（試行が変わった。#1358）と、仮の行から span の木に替わったときは一覧を作り直す（処理中に出した行を
  // 残す規則で前の試行の行・仮の行が残らないように）。
  const progressKey = `${jobId}#${turn.attempt ?? 0}`;
  // 終端は会話のターン（ジョブの状態）で決まるので、会話の取り直しが途絶えたら取り直し、「接続を確認しています」を出す
  // （#1160。応答しない取得を打ち切って新しく取り直す）。処理の経過の読み直しの失敗も同じ案内にする。
  const tracker = useChatProgressTracker({
    key: jobId,
    steps: progressSteps,
    active: running,
    elapsedMs: chatJobElapsedMs(turn),
    receivedAt,
    refresh: () => refresh(),
    staleAfterMs: CHAT_PROGRESS_STALE_AFTER_MS,
    enabled: tracking,
  });
  const legacyProgress = !running && progress.legacy;
  // 送信のジョブが実行まで行い（#1176）、結果の行はここで 1 回だけ受け取る。「もう一度実行」は明示の
  // 操作（#1154）。安全検査を通った生成 SQL だけを実行できる。
  // 他の利用者の会話（readOnly）の行は実行した人だけが受け取るので、受け取りに行かない（#3244）。
  const execution = useChatSqlExecution(
    turn.job_id,
    turn.status === "done" && canExecuteSql && !readOnly
      ? turn.last_execution?.result_expires_at
      : null,
  );
  const executable = Boolean(
    turn.status === "done" &&
      result?.safety.is_safe &&
      result.generated_sql.trim(),
  );
  const executed = Boolean(execution.data || turn.last_execution);
  return (
    // 1 往復の入れ物（質問の吹き出し・回答の枠）は 3 製品共通の ChatTurn / ChatAnswer（#1161）。
    <ChatTurn
      question={turn.question || result?.original_question}
      // 会話の持ち主以外（system_admin）が送った質問は、送った人を添える（#1928）。
      questionSender={
        turn.sender_display_name == null
          ? undefined
          : t("chat.sentBy", {
              name: turn.sender_display_name || t("chat.historyOwnerMissing"),
            })
      }
      questionAttachments={chatAttachmentItems(turn.attachments)}
      attachmentsLabel={t("chat.attachments.list")}
      testId="sql-chat-turn"
    >
      {/* 回答前の確認で答えた内容・飛ばしたことは、利用者の発言として質問の下に残す（#1839）。 */}
      {turn.requirement_answers?.length || turn.requirement_skipped ? (
        <ClarificationRecord
          entries={(turn.requirement_answers ?? []).map((record, index) => ({
            id: String(index),
            prompt: record.prompt,
            selected: [record.answer],
          }))}
          outcome={turn.requirement_answers?.length ? "answered" : "skipped"}
          labels={requirementClarificationLabels()}
          testId="sql-chat-turn-clarification"
        />
      ) : null}
      <ChatAnswer>
          {/* 処理の経過（#1145・#3226）は span の木。実行中は今の段階と開いた一覧、完了後は回答の上に「処理の経過」の
              1 行（押すと開く。3 製品で同じ。#2265）。span の無い古い形式の記録は案内の文を出す。 */}
          {legacyProgress ? (
            <><ProgressLegacyNotice testId="sql-chat-progress-legacy" />
              <RunProcessDetails metadata={{ conversationId: turn.conversation_id ?? null, messageId: turn.job_id }}><RunUsedModels models={[]} /></RunProcessDetails>
            </>
          ) : (
            <ChatProgress
              cancelled={turn.error_code === "JOB_CANCELLED"}
              traceId={progress.traceId}
              processDetailsProps={{ metadata: { conversationId: turn.conversation_id ?? null, messageId: turn.job_id } }}
              footer={<RunUsedModels models={[]} events={progress.events} />}
              {...tracker.progressProps}
              reconnecting={tracker.reconnecting || (running && progress.failed)}
              steps={progressSteps}
              progressKey={queuePlaceholder ? `${progressKey}:queue` : progressKey}
              startedAt={sentAtMs}
              labels={CHAT_PROGRESS_LABELS}
              partial={progress.partial}
              testId="sql-chat-progress"
            />
          )}
          {turn.error_message && turn.error_code === "JOB_CANCELLED" ? (
            <Banner severity="info">{t("chat.cancelled")}</Banner>
          ) : turn.error_message ? (
            // 失敗は 3 製品共通の失敗のカード（止まった段・原因・次の操作・「詳細」。#2342）。進めない理由を先に
            // 読めるよう、処理の経過の直下（SQL・説明・結果の上）に 1 つだけ出す。
            <ProcessFailureNotice
              failure={chatJobFailure(turn, progressSteps)}
              labels={processFailureLabels()}
              testId="sql-chat-turn-failure"
            />
          ) : null}
          {turn.requirement_question ? (
            // 回答前の確認で確かめた質問の文（#1442）。生成はこの文と構造化した要件で行った（回答の根拠なので SQL の前）。
            <Disclosure
              variant="plain"
              size="sm"
              summary={t("chat.requirement.applied")}
              summaryProps={{ "data-testid": "sql-chat-turn-requirement" }}
            >
              <p className="text-xs leading-5 text-fg-muted">{t("nl2sql.clarification.clarifiedQuestion")}</p>
              <p className="break-words text-sm leading-6 text-fg">{turn.requirement_question}</p>
            </Disclosure>
          ) : null}
          {result ? (
            <>
              {/* SQL の表示（状態・コピー・実行・本文）は DB 管理チャットと共通（#2264）。 */}
              <ChatSqlBlock
                labels={SQL_BLOCK_LABELS}
                sql={result.generated_sql}
                onCopied={() => setCopyError("")}
                onCopyError={setCopyError}
                badges={
                  <SqlSafetyBadge
                    safe={result.safety.is_safe}
                    checked={result.safety.safety_checked}
                    executed={executed}
                    labels={SQL_BLOCK_LABELS}
                  />
                }
                actions={
                  executable && canExecuteSql && !readOnly ? (
                    // 吹き出しの中の操作なので secondary。実行中はラベルを変えず、アイコンがスピナーになる。
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      icon={Play}
                      loading={execution.running}
                      onClick={execution.run}
                      data-testid="sql-chat-execute"
                    >
                      {executed ? t("chat.execute.again") : t("chat.execute")}
                    </Button>
                  ) : null
                }
              />
              {result.explanation ? (
                <MessageText text={result.explanation} />
              ) : null}
              {executable && !canExecuteSql && !readOnly ? (
                <p className="text-xs text-fg-muted">
                  {t("chat.execute.permissionRequired")}
                </p>
              ) : null}
              {executable ? (
                <ChatSqlExecutionResult
                  turn={turn}
                  execution={execution}
                  canOpenDirectSql={canOpenDirectSql}
                  readOnly={readOnly}
                />
              ) : null}
              {/* 回答の注意書きは SQL と結果の後（3 製品共通の AnswerNotice。#2405）。 */}
              {turn.status === "done" ? (
                <AnswerNotice notice={result.notice} label={t("answer.notice.label")} />
              ) : null}
            </>
          ) : null}
          {turn.warning_message ? (
            <Banner severity="warning">{turn.warning_message}</Banner>
          ) : null}
          {copyError ? <Banner severity="danger">{copyError}</Banner> : null}
      </ChatAnswer>
    </ChatTurn>
  );
}
