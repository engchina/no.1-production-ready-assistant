import {
  AnswerFooter,
  ChatComposer,
  ChatReadOnlyNotice,
  ChatAnswer,
  ChatAnswerBody,
  ChatHistoryList,
  chatHistoryWindowLabels,
  ChatLayout,
  ChatTurn,
  ClarificationRecord,
  ChatSkeleton,
  PageBody,
  PageHeader,
  Button,
  Banner,
  ProcessFailureNotice,
  TimedLoadingState,
  DEFAULT_PAGE_SIZE,
  useChatHistoryActions,
  ChatUserMessage,
  createOptimisticChatMessage,
  withOptimisticChatStatus,
  type ChatUserMessageStatus,
  type OptimisticChatMessage,
  type ChatProgressStep,
  useChatAutoScroll,
  useChatHistoryPanel,
  chatAttachmentItems,
  type ChatAttachmentItem,
} from "@production-ready/ui";
import {
  RotateCcw,
  Search,
  Square,
} from "lucide-react";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { FeedbackControls } from "@/components/feedback/FeedbackControls";
import { SearchAnswerProfileSelect, SearchAnswerProfileSelectSkeleton } from "@/components/search-answer-profiles/SearchAnswerProfileSelect";
import { AnswerCitations } from "@/components/search/CitationList";
import { AnswerNotices } from "@/components/search/AnswerDetailsPanel";
import { AnswerProcess } from "@/components/search/AnswerProcess";
import { AnswerText } from "@/components/search/AnswerText";
import { useAuth } from "@/components/security/AuthProvider";
import { ApiErrorState, EmptyState, ErrorState } from "@/components/StateViews";
import type {
  ApprovedFaqSuggestionData,
  ClarificationAnswer,
  ClarificationSuggestionData,
  ChatMessage,
  CompareModel,
  ConversationDetail,
  RetrievedChunk,
  StorageFailure,
} from "@/lib/api";
import { api, ApiError } from "@/lib/api";
import {
  chatAnswerFailure,
  processFailureLabels,
  storageFailureNotice,
  type ChatAnswerFailureInfo,
} from "@/lib/process-failure";
import { ApprovedFaqSuggestions } from "@/components/search/ApprovedFaqSuggestions";
import { ClarificationChoice } from "./ClarificationChoice";
import { useRagChatAttachments } from "./useRagChatAttachments";
import { chatSubmitProgressSteps } from "@/lib/chat-progress";
import { useRagTraceProgress } from "@/lib/trace-progress";
import {
  isChatStreamGone,
  newChatClientMessageId,
  resumeChatStream,
  streamChatMessage,
  type ChatColumn,
  type ChatStreamHandlers,
} from "@/lib/chat-stream";
import { formatDateTime } from "@/lib/format";
import { answerAttachedImages, answerModelHeading, unreadSectionLinks } from "@/lib/answer-diagnostics";
import { canOpenDocumentDetail } from "@/lib/route-permissions";
import { t } from "@/lib/i18n";
import { paginationLabels } from "@/lib/pagination-labels";
import { isNullableString, useWorkspaceState } from "@/lib/workspace-state";
import {
  useAllSearchAnswerProfiles,
  useCompareModels,
  useConversation,
  useConversations,
  useCreateConversation,
  useSavedAnswerTraceIds,
  useUpdateConversation,
  useDeleteConversation,
  queryKeys,
} from "@/lib/queries";
import { useQueryClient } from "@tanstack/react-query";
import { MENU_PERMISSIONS } from "@/lib/permissions";
import { APP_ROUTES } from "@/lib/routes";
import { cn } from "@/lib/utils";

/**
 * 回答の作成中に、配信（event・heartbeat）がこの時間届かなければ途絶えたとみなし、接続を張り直す（#1160 /
 * #1175）。backend は event の無い間も 10 秒ごとに heartbeat を送るので、その数回分。
 */
const CHAT_STREAM_STALE_AFTER_MS = 30_000;
/** 配信が途絶えたかを確かめる間隔。 */
const CHAT_STREAM_STALL_CHECK_MS = 1_000;
/**
 * 接続が切れたときに続きを購読し直す回数の上限（#1175）。event を受け取れたら数え直す。超えたら、保存済みの
 * 会話（作成中の回答）に引き継いで polling で待つ。待ちは 1 秒・2 秒・4 秒 … `CHAT_STREAM_RESUME_MAX_DELAY_MS`。
 */
const CHAT_STREAM_RESUME_ATTEMPTS = 6;
const CHAT_STREAM_RESUME_MAX_DELAY_MS = 15_000;

/** signal が中止されるか `ms` が過ぎるまで待つ（中止なら false）。 */
function waitOrAbort(ms: number, signal: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve(false);
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve(true);
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      resolve(false);
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** ref に今の時刻を入れる（送信・配信の event の処理から呼ぶ。描画中には呼ばない）。 */
function stampNow(ref: { current: number | null }) {
  ref.current = Date.now();
}
const EMPTY_TRACE_IDS: ReadonlySet<string> = new Set();
const EMPTY_COMPARE_MODELS: readonly CompareModel[] = [];

/** 会話一覧のページ（検索・回答プロファイルごと。#403）。別の検索・回答プロファイルに移ったら 1 ページ目から。 */
interface ConversationsPage {
  searchAnswerProfileId: string | null;
  offset: number;
}

function isConversationsPage(value: unknown): value is ConversationsPage {
  const page = value as ConversationsPage;
  return (
    typeof page === "object" &&
    page !== null &&
    isNullableString(page.searchAnswerProfileId) &&
    Number.isInteger(page.offset) &&
    page.offset >= 0
  );
}

interface LiveColumn {
  model_id: string;
  label: string;
  answer: string;
  citations: RetrievedChunk[];
  /** `stopped` は利用者の停止（別の画面・タブからの停止を含む。#1175）。 */
  status: "streaming" | "done" | "error" | "stopped";
  traceId: string | null;
  errorMessage: string | null;
  /** 失敗の「詳細」（エラーの種類・コード・止まった工程・時刻。#2342）。 */
  failure?: ColumnFailureDetail | null;
  /** 作ったが保存できなかった記録（#2342）。 */
  storageFailures?: StorageFailure[];
  /** 回答の根拠・実行記録(無い回答では null)。 */
  answerDiagnostics: unknown;
  /** 回答の注意書き（#2405。届くまで・無いときは null）。 */
  notice?: string | null;
  /** 作成中の回答のメッセージ（処理の経過の trace の API の対象）。`start` が届くまでは null。 */
  messageId: string | null;
  startedAtMs: number;
}

/** 回答の失敗の「詳細」（SSE の `error` の項目。#2342）。 */
type ColumnFailureDetail = Pick<ChatAnswerFailureInfo, "errorType" | "errorCode" | "stage" | "occurredAt">;

/** 送信の条件（再送信で同じ条件のまま送り直す）。 */
interface DeliverRequest {
  content: string;
  questionContextToken?: string;
  approvedFaqId?: string;
  clarification?: ClarificationAnswer;
  /** 回答前の確認を「確認せずに回答」で飛ばした確認の id（会話に残すだけ。#1839）。 */
  clarificationSkippedRuleId?: string;
}

/**
 * 送信中・生成中・失敗・停止の 1 往復（#907）。
 *
 * 送信した瞬間に仮の質問（`pending`）で作り、会話の作成・サーバーの `start` を待たずに会話の欄の末尾へ出す。
 * `start` で保存済みの質問（`user`）が届いたら置き換え、`all_done` で取り直した会話に引き継いで外す
 * （同じ質問を二重に出さない）。失敗・停止のときは外さずに残す（送った質問を失わない）。
 */
interface LiveTurn {
  pending: OptimisticChatMessage;
  /**
   * 画面が決めた質問の id（#1175）。サーバーは同じ id で質問を保存する。`start` の前の停止（取消の API）・
   * 接続が切れた後の再購読に使う。送り直すたびに新しくする。
   */
  clientMessageId: string;
  /** サーバーが保存した質問。`start` が届くまでは null。 */
  user: ChatMessage | null;
  columns: LiveColumn[];
  /** 送信できなかった理由（`pending.status` が `failed` のとき）。 */
  failureMessage: string | null;
  request: DeliverRequest;
  /** 質問に添付したファイル（#2517）。`start` の前も送った質問の下に出す。 */
  attachments: ChatAttachmentItem[];
}

interface Turn {
  user: ChatMessage;
  replies: ChatMessage[];
}

/** メッセージ列を「ユーザー発話 + その回答群」のターンへまとめる。 */
function buildTurns(messages: ChatMessage[]): Turn[] {
  const byReply = new Map<string, ChatMessage[]>();
  for (const message of messages) {
    if (message.role === "ASSISTANT" && message.reply_to_message_id) {
      const list = byReply.get(message.reply_to_message_id) ?? [];
      list.push(message);
      byReply.set(message.reply_to_message_id, list);
    }
  }
  return messages
    .filter((message) => message.role === "USER")
    .map((user) => ({ user, replies: byReply.get(user.message_id) ?? [] }));
}

/**
 * 列の処理の経過の読み出し先（#1146 / #3226）。行は回答の trace の API（共通の span の表の snapshot）から作る。
 * 作成中は読み直し、終わったら 1 回だけ読む。`start` の前（回答のメッセージが無い）は読まない。
 */
interface ColumnProgressSource {
  conversationId: string | null;
  messageId: string | null;
  /** 作成中か（snapshot を読み直す）。 */
  active: boolean;
  startedAtMs: number;
  /** 回答の配信が途絶え、接続を張り直している（#1160）。 */
  reconnecting?: boolean;
}

/**
 * 列の処理の経過（#1146 / #3226）。`traceId` は「処理の詳細」に出す trace id、`legacy` は span の無い古い形式の
 * 記録（工程の行の代わりに案内を出す）、`partial` は書けなかった span がある記録。
 */
interface ColumnProgress {
  steps: ChatProgressStep[];
  startedAtMs: number;
  reconnecting?: boolean;
  traceId?: string;
  legacy?: boolean;
  partial?: boolean;
}

/** 列の処理の経過を回答の trace の API で読む（回答ごとに 1 つ。比較の列もそれぞれ読む）。 */
function useColumnProgress(source: ColumnProgressSource | null): ColumnProgress | null {
  const conversationId = source?.conversationId ?? null;
  const messageId = source?.messageId ?? null;
  const active = source?.active ?? false;
  const snapshot = useRagTraceProgress({
    // 対象は回答のメッセージ（snapshot のイベントの `target_id`）。会話が決まるまでは読まない。
    key: conversationId && messageId ? messageId : null,
    active,
    fetchSnapshot: (signal) => api.getChatAnswerTrace(conversationId ?? "", messageId ?? "", signal),
  });
  if (source === null) return null;
  return {
    steps: snapshot.steps,
    startedAtMs: source.startedAtMs,
    // 配信が途絶えた・読み直しに失敗している間は「接続を確認しています」（#1160）。
    reconnecting: active && (Boolean(source.reconnecting) || snapshot.failed),
    traceId: snapshot.traceId,
    // 作成中の列は古い形式にしない（span が書かれる前）。
    legacy: !active && snapshot.legacy,
    partial: snapshot.partial,
  };
}

/** 回答 1 カラム（モデル単位）。ストリーミング中はカーソルを出す。 */
function AssistantColumn({
  label,
  answer,
  citations,
  withheldCount = 0,
  traceId,
  searchAnswerProfileId,
  messageId,
  streaming,
  errorMessage,
  failure = null,
  storageFailures,
  answerDiagnostics = null,
  passageSource = null,
  notice = null,
  savedAnswer = false,
  progressSource = null,
  stoppedMessage = null,
  onRetry,
  readOnly = false,
  className,
}: {
  label: string | null;
  answer: string;
  citations: RetrievedChunk[];
  /** 保存した引用のうち、見ている利用者が今の権限で見られない根拠の数（#3252）。 */
  withheldCount?: number;
  traceId: string | null;
  searchAnswerProfileId: string;
  messageId: string | null;
  streaming: boolean;
  errorMessage: string | null;
  /** 失敗の「詳細」（#2342）。保存済みの ERROR の回答は持たない（本文と trace_id だけ）。 */
  failure?: ColumnFailureDetail | null;
  /** 作ったが保存できなかった記録（回答は出したまま warning を出す。#2342）。 */
  storageFailures?: StorageFailure[];
  answerDiagnostics?: unknown;
  /**
   * 段落の印の記録（#2738）を読む、診断の無い保存した回答の会話のメッセージ（会話を開き直したとき）。診断があれば
   * 診断から読む。
   */
  passageSource?: unknown;
  /** 回答の注意書き（回答の本文と根拠の後に出す。#2405）。 */
  notice?: string | null;
  /** 保存された回答がある(trace_id から回答の実行記録を開ける)。 */
  savedAnswer?: boolean;
  /**
   * 処理の経過の読み出し先（#1146 / #3226）。生成中は今の段階と経過時間、完了後は回答の上に「処理の経過」の 1 行を
   * 出す。段階がまだ無い（送信の応答待ち）ときは「質問を送信しています」を出す。
   */
  progressSource?: ColumnProgressSource | null;
  /** 停止した回答の文（利用者の停止。#1175）。失敗の Banner ではなく、停止の 1 行で出す。 */
  stoppedMessage?: string | null;
  /** 失敗した回答をもう一度送信する（最新の質問だけ）。 */
  onRetry?: () => void;
  /** 他の利用者の会話（system_admin が見る）。回答と根拠の評価を出さない（持ち主だけ。#3244）。 */
  readOnly?: boolean;
  className?: string;
}) {
  // 「回答に未反映の関連章節」の行から章節の原文を開くリンクは、文書の詳細を開ける利用者だけ（#2040・#303）。
  const canOpenSections = canOpenDocumentDetail(useAuth().hasPermission);
  const progress = useColumnProgress(progressSource);
  const waitingForAnswer = streaming && !answer && !errorMessage && progress !== null;
  const finished = !streaming && !errorMessage && !stoppedMessage;
  const storageNotice = storageFailureNotice(storageFailures, traceId);
  // 回答は列のモデルだけで作る（#3444）。そのモデルへ根拠の画像を添付した回答は、見出しにそのことを添える。
  const attachedImages = finished ? answerAttachedImages(answerDiagnostics, passageSource) : 0;
  const heading = answerModelHeading(label, attachedImages);
  const listId = useId();
  const answerBody = (
    <AnswerText
      text={answer}
      streaming={streaming}
      citations={citations}
      unreadSections={canOpenSections ? unreadSectionLinks(answerDiagnostics) : []}
      diagnostics={finished ? (answerDiagnostics ?? passageSource) : null}
      live
      cursor={
        streaming ? <span className="ml-0.5 inline-block animate-pulse motion-reduce:animate-none">▍</span> : null
      }
    />
  );
  return (
    // 回答の枠は 3 製品共通の ChatAnswer（#1161）。比較（複数モデル）では列ごとに 1 つの枠にし、列の高さをそろえる。
    <ChatAnswer id={messageId ? `message-${messageId}` : undefined} className={cn("h-full", className)}>
      {heading ? (
        // 1 列（既定のモデル）でも、どのモデルの回答かを出す（#649）。
        <h3
          className="min-w-0 break-words border-b border-border pb-2 text-sm font-semibold text-fg [overflow-wrap:anywhere]"
          title={heading}
          data-testid="chat-answer-model-heading"
        >
          {heading}
        </h3>
      ) : null}
      {progress && !finished ? (
        // 処理の経過（3 製品共通の ChatProgress。#1145 / #1146 / #2096）。回答の本文は生成と検査が終わってから
        // まとめて届くので、それまでは回答の場所に工程の一覧（行が増える）と今の工程・経過時間を出す。失敗の
        // 原因は段階ではなく下の Banner に出す。
        <AnswerProcess
          steps={progress.steps.length > 0 ? progress.steps : chatSubmitProgressSteps(progress.startedAtMs)}
          // 送信の段階から回答の段階に移ったら一覧を作り直す。回答の段階の間は、一度出した段階を消さない（#1358）。
          progressKey={progress.steps.length > 0 ? "answer" : "submit"}
          // 経過時間は送信から数え続ける（段階ごとに 0 に戻さない。#1176）。
          startedAt={progress.startedAtMs || undefined}
          reconnecting={progress.reconnecting}
          // 保存した失敗・停止の回答の、span の無い古い形式の記録（作成中は古い形式にしない）。
          legacy={progress.legacy}
          partial={progress.partial}
          testId="chat-answer-progress"
        />
      ) : finished && (progress || answerDiagnostics || (savedAnswer && traceId)) ? (
        // 回答ができた後も同じ場所（回答の上。作成中の「N ステップ完了」の場所）に、処理の経過（工程の一覧。完了したら
        // 閉じる）と、その中の末尾の「処理の詳細」（回答の記録の詳細。技術的な詳細は畳む。messaging.md §10。#2325）を出す。名前は
        // 「処理の経過」1 つで、回答の下に出し直さない（#2096 / #2115 / #2118。RAG 検索と同じ部品）。
        <AnswerProcess
          steps={progress?.steps ?? []}
          testId="chat-answer-progress"
          traceId={progress?.traceId}
          conversationId={progressSource?.conversationId ?? null}
          messageId={messageId}
          legacy={progress?.legacy ?? Boolean(savedAnswer && traceId)}
          partial={progress?.partial}
          diagnostics={answerDiagnostics}
          savedRecord={savedAnswer && traceId ? { traceId, searchAnswerProfileId } : null}
        />
      ) : null}
      {stoppedMessage ? (
        // 停止した回答（保存済み）。色だけに頼らず、停止のアイコンを添える（送信中の停止と同じ形）。
        <p className="flex items-center gap-1.5 text-sm text-fg-muted" data-testid="chat-answer-stopped">
          <Square size={14} aria-hidden="true" />
          {stoppedMessage}
        </p>
      ) : errorMessage ? (
        // 失敗は 3 製品共通の失敗のカード（何が起きたか → 原因 → 次の操作 →「詳細」。#2342）。
        <ProcessFailureNotice
          failure={chatAnswerFailure({ message: errorMessage, traceId, ...failure })}
          labels={processFailureLabels()}
          testId="chat-answer-failure"
          action={
            onRetry ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                icon={RotateCcw}
                onClick={onRetry}
              >
                {t("chat.error.retry")}
              </Button>
            ) : undefined
          }
        />
      ) : waitingForAnswer ? null : (
        <>
          {/*
            作った回答を保存できなかった（#2342）。回答は出したまま、読む前に欠けたもの（読み込み直すと消える等）と
            理由が分かるよう、処理の経過の直下・回答の本文の上に 1 つだけ出す。
          */}
          {finished && storageNotice ? (
            <ProcessFailureNotice
              failure={storageNotice}
              severity="warning"
              labels={processFailureLabels()}
              testId="chat-answer-storage-failure"
            />
          ) : null}
          {/*
            回答の本文が届いたら、本文の前に見出し「回答」を出す（本文の中の節の見出しと同じ書式。3 製品共通の
            ChatAnswerBody。#3433）。本文の届く前（カーソルだけ）は出さない。
          */}
          {answer.trim() ? <ChatAnswerBody testId="chat-answer-body">{answerBody}</ChatAnswerBody> : answerBody}
          {/* 回答を読むときに要る案内（資料では決まらない値など）は回答の本文の直下に出す（#2096）。 */}
          {finished && (answerDiagnostics || passageSource) ? (
            <AnswerNotices diagnostics={answerDiagnostics} passageSource={passageSource} />
          ) : null}
        </>
      )}
      {citations.length > 0 ||
      withheldCount > 0 ||
      (finished && (traceId || answerDiagnostics || progress || notice)) ? (
        // 回答の下は 3 製品共通の並び（#2707）: 回答の本文 → 回答の注意書き（#2405）→ 根拠の一覧（AI に渡した
        // N 件。#1202 / #1417）→ 評価（処理の経過は回答の上。#2115。Agent のチャットと同じ）。比較の列の高さを
        // そろえるため、回答の枠の下端にそろえる。
        <AnswerFooter
          className="mt-auto"
          notice={finished ? notice : null}
          noticeLabel={t("answer.notice.label")}
          evidence={
            <AnswerCitations
              citations={citations}
              withheldCount={withheldCount}
              // まとまり（根拠の木）は回答が終わってから届く診断にある（#2097）。
              idPrefix={messageId ? `message-${messageId}` : listId}
              // 他の利用者の会話では根拠の評価を出さない（trace が無ければ評価を出さない。#3244）。
              cardProps={{ traceId: readOnly ? null : traceId, searchAnswerProfileId, sourceSurface: "chat", messageId }}
              className="border-t border-border px-2 pt-1"
            />
          }
          feedback={
            finished && !readOnly ? (
              <FeedbackControls
                traceId={traceId}
                searchAnswerProfileId={searchAnswerProfileId}
                targetType="answer"
                sourceSurface="chat"
                messageId={messageId}
              />
            ) : null
          }
        />
      ) : null}
    </ChatAnswer>
  );
}

/** ユーザー発話 + 回答カラム群を 1 ターンとして表示。 */
function MessageTurn({
  user,
  userStatus = "sent",
  columns,
  searchAnswerProfileId,
  onRetry,
  onAskUnscoped,
  footer,
  liveAttachments,
  readOnly = false,
  testId,
}: {
  user: Pick<ChatMessage, "content" | "clarification"> &
    Partial<Pick<ChatMessage, "sent_by_owner" | "sender_display_name" | "attachments">>;
  /** 送る前（`start` の前）の質問の添付（#2517）。保存した質問は `user.attachments`。 */
  liveAttachments?: ChatAttachmentItem[];
  /** 質問の状態（送信中・失敗・停止。#907）。保存済みの質問は `sent`。 */
  userStatus?: ChatUserMessageStatus;
  /** 回答の後に出す送信の失敗・停止の表示（#907）。 */
  footer?: ReactNode;
  testId?: string;
  searchAnswerProfileId: string;
  /** 失敗した回答があるときに同じ質問をもう一度送る（最新のターンだけ渡す）。 */
  onRetry?: () => void;
  /** 確認で範囲を絞った回答のとき、範囲を指定せずに同じ質問を送る（最新のターンだけ渡す。#721）。 */
  onAskUnscoped?: () => void;
  /** 他の利用者の会話（system_admin が見る）。評価を出さない（#3244）。 */
  readOnly?: boolean;
  columns: {
    key: string;
    label: string | null;
    answer: string;
    citations: RetrievedChunk[];
    withheldCount?: number;
    traceId: string | null;
    messageId: string | null;
    streaming: boolean;
    errorMessage: string | null;
    failure?: ColumnFailureDetail | null;
    storageFailures?: StorageFailure[];
    answerDiagnostics?: unknown;
    passageSource?: unknown;
    notice?: string | null;
    savedAnswer?: boolean;
    progressSource?: ColumnProgressSource | null;
    stoppedMessage?: string | null;
  }[];
}) {
  const compare = columns.length > 1;
  // 範囲を絞った回答は末尾に「（対象: …）」が付く（#717）。
  const scoped =
    onAskUnscoped !== undefined &&
    columns.some(
      (column) => !column.streaming && !column.errorMessage && column.answer.includes("（対象:")
    );
  return (
    // 1 往復の入れ物（質問の吹き出し）は 3 製品共通の ChatTurn（#1161）。
    <ChatTurn
      question={user.content}
      questionStatus={userStatus}
      failedLabel={t("chat.send.failed")}
      // 会話の持ち主以外（#3244 より前の system_admin）が送った質問には、送った人を添える。
      questionSender={
        user.sent_by_owner === false
          ? t("chat.messages.sender", {
              name: user.sender_display_name || t("chat.sessions.ownerMissing"),
            })
          : undefined
      }
      questionAttachments={user.attachments?.length ? chatAttachmentItems(user.attachments) : liveAttachments}
      attachmentsLabel={t("chat.attachments.list")}
      testId={testId}
    >
      {/* 回答前に確認した内容（#1839）。利用者の答え・飛ばしたことを、利用者の発言として質問の下に残す。 */}
      {user.clarification ? (
        <ClarificationRecord
          outcome={user.clarification.outcome}
          entries={user.clarification.entries.map((entry, index) => ({
            id: String(index),
            prompt: entry.prompt,
            selected: entry.selected ?? [],
            otherText: entry.other_text ?? "",
            freeTextOnly: entry.free_text_only === true,
          }))}
          testId="chat-clarification-record"
        />
      ) : null}
      {columns.length > 0 ? (
        <div
          className="grid grid-cols-1 gap-3"
          style={
            compare
              ? { gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 40rem), 1fr))" }
              : undefined
          }
        >
          {columns.map((column, index) => (
            <AssistantColumn
              key={column.key}
              label={column.label}
              answer={column.answer}
              citations={column.citations}
              withheldCount={column.withheldCount}
              traceId={column.traceId}
              searchAnswerProfileId={searchAnswerProfileId}
              messageId={column.messageId}
              streaming={column.streaming}
              errorMessage={column.errorMessage}
              failure={column.failure}
              storageFailures={column.storageFailures}
              answerDiagnostics={column.answerDiagnostics}
              passageSource={column.passageSource}
              notice={column.notice}
              savedAnswer={column.savedAnswer}
              progressSource={column.progressSource}
              stoppedMessage={column.stoppedMessage}
              onRetry={column.errorMessage ? onRetry : undefined}
              readOnly={readOnly}
              className={
                compare && columns.length % 2 === 1 && index === columns.length - 1
                  ? "col-span-full"
                  : undefined
              }
            />
          ))}
        </div>
      ) : null}
      {footer}
      {scoped ? (
        <div className="flex justify-end">
          <Button type="button" variant="secondary" size="sm" icon={Search} onClick={onAskUnscoped}>
            {t("chat.clarify.askUnscoped")}
          </Button>
        </div>
      ) : null}
    </ChatTurn>
  );
}

export function ChatClient() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const searchAnswerProfilesQuery = useAllSearchAnswerProfiles({ status: "ACTIVE" });
  const searchAnswerProfiles = searchAnswerProfilesQuery.data?.items ?? [];
  // 選択中の検索・回答プロファイル・会話・入力中の下書きは、ページを行き来しても再読込しても残す
  // （workspace-state.md）。URL の deep-link があればそちらを優先する。
  const urlSearchAnswerProfileId = searchParams.get("search_answer_profile_id");
  const urlConversationId = searchParams.get("conversation_id");
  const [searchAnswerProfileId, setSearchAnswerProfileId] = useWorkspaceState<string | null>(
    "chat.searchAnswerProfileId",
    null,
    isNullableString,
    urlSearchAnswerProfileId ?? undefined
  );
  // 復元した検索・回答プロファイルがアーカイブ・削除済みなら選択を外す（別の検索・回答プロファイルへ置き換えない）。
  const searchAnswerProfileMissing =
    Boolean(searchAnswerProfileId) &&
    Boolean(searchAnswerProfilesQuery.data) &&
    !searchAnswerProfilesQuery.data?.has_next &&
    !searchAnswerProfiles.some((view) => view.id === searchAnswerProfileId);
  useEffect(() => {
    if (searchAnswerProfileMissing) setSearchAnswerProfileId(null);
  }, [searchAnswerProfileMissing, setSearchAnswerProfileId]);
  // 参照 KB が 0 件の検索・回答プロファイルではチャットしない（利用者の全 KB を検索しない。#304）。
  // backend も送信時に 409 で理由を返すが、送信する前にこの場で理由を示す。
  const auth = useAuth();
  const canOpenSearchAnswerProfiles = auth.hasPermission(MENU_PERMISSIONS.searchAnswerProfiles);
  // 「AI 活用」の規則（#1889）: system_admin は全員の会話を見られ、履歴に利用者を出す。
  const seesAllUsers = Boolean(auth.user?.is_system_admin);
  const selectedSearchAnswerProfile = searchAnswerProfiles.find((view) => view.id === searchAnswerProfileId);
  const searchAnswerProfileWithoutKnowledgeBases = selectedSearchAnswerProfile?.knowledge_base_count === 0;
  // 会話一覧はサーバー側でページングする（50 件で打ち切らない。#403）。
  // ページは作業状態として残す（workspace-state.md）。別の検索・回答プロファイルに移ったら 1 ページ目から。
  const [conversationsPage, setConversationsPage] = useWorkspaceState<ConversationsPage>(
    "chat.conversationsPage",
    { searchAnswerProfileId, offset: 0 },
    isConversationsPage
  );
  const conversationOffset =
    conversationsPage.searchAnswerProfileId === searchAnswerProfileId ? conversationsPage.offset : 0;
  const setConversationOffset = (offset: number) =>
    setConversationsPage({ searchAnswerProfileId, offset });
  const conversationsQuery = useConversations({
    search_answer_profile_id: searchAnswerProfileId ?? undefined,
    limit: DEFAULT_PAGE_SIZE,
    offset: conversationOffset,
  });
  const conversations = conversationsQuery.data?.items ?? [];
  // 他の利用者の会話（system_admin が見る）。名前の変更は持ち主だけ（#3244）。
  const othersConversationIds = new Set(
    conversations.filter((conversation) => conversation.owned === false).map((conversation) => conversation.id)
  );
  // 会話が減って今のページが空になったら、ChatHistoryList の共通の OffsetPagination が最後のページへ戻す（#1265 / #1266）。
  const conversationsData = conversationsQuery.data;

  const [activeId, setActiveId] = useWorkspaceState<string | null>(
    "chat.conversationId",
    null,
    isNullableString,
    urlConversationId ?? (urlSearchAnswerProfileId ? null : undefined)
  );
  const [sending, setSending] = useState(false);
  // 作成中（STREAMING）の回答は、この画面の配信（SSE）で受け取っていない間（再読込の後・購読し直せないとき）だけ
  // 保存済みの会話を取り直して完了を待つ（#1175）。
  const conversationQuery = useConversation(activeId, { pollStreaming: !sending });
  // 画面の前提（検索・回答プロファイルの一覧・開いている会話の内容）が揃うまでは、会話の欄に空の状態を出さず
  // 会話の形の Skeleton で覆い、送信を止める（messaging.md §11.7、#1153）。入力欄・新しい会話・履歴の開閉を
  // 無効にするのは検索・回答プロファイルの一覧の読み込み中だけ。会話の内容の読み込み中は入力欄に書ける
  // （書いている途中で無効にしてフォーカスと入力を失わせない。#1188）。
  // 会話の内容を読めなかったときも送信しない（内容の分からない会話に続けて送らない）。
  const conversationLoading = Boolean(activeId) && conversationQuery.isLoading;
  const conversationFailed = Boolean(activeId) && conversationQuery.isError && !conversationQuery.data;
  const prerequisitesLoading = searchAnswerProfilesQuery.isLoading || conversationLoading;
  const persistedMessages = useMemo(
    () => conversationQuery.data?.messages ?? [],
    [conversationQuery.data]
  );
  // 保存された回答がある回答(trace_id)。該当する回答だけ実行記録を開ける。
  // 回答履歴のページングに依存しないよう、開いている会話の回答の trace_id で引き当てる（#304）。
  const replyTraceIds = useMemo(
    () =>
      persistedMessages
        .filter((message) => message.role === "ASSISTANT" && message.trace_id)
        .map((message) => message.trace_id as string),
    [persistedMessages]
  );
  const savedAnswerQuery = useSavedAnswerTraceIds(searchAnswerProfileId, replyTraceIds);
  const answerTraceIds = savedAnswerQuery.data ?? EMPTY_TRACE_IDS;

  const createConversation = useCreateConversation();
  const updateConversation = useUpdateConversation();
  const deleteConversation = useDeleteConversation();
  // 会話の名前の変更・削除（3 製品共通の useChatHistoryActions。#1817）。
  const historyActions = useChatHistoryActions({
    onRename: (id, title) => updateConversation.mutateAsync({ id, payload: { title } }),
    onDelete: (id) => deleteConversation.mutateAsync(id),
    // 開いている会話を消すときは、送信・生成を止めて選択を外す（消した会話を開いたままにしない）。
    onBeforeDelete: (item) => {
      if (item.id !== activeId) return;
      cancelSending();
      setLiveTurn(null);
      setErrorText("");
      setActiveId(null);
    },
    errorMessage: (error, fallback) =>
      error instanceof ApiError ? error.messages.join(" / ") : fallback,
    // 他の利用者の会話（system_admin が見る）の名前は持ち主だけが変えられる。削除は出す（#3244）。
    canRename: (item) => !othersConversationIds.has(item.id),
  });
  const { cancelRename } = historyActions;
  const compareModelsQuery = useCompareModels();
  const compareModels = compareModelsQuery.data ?? EMPTY_COMPARE_MODELS;
  const modelLabels = new Map(compareModels.map((model) => [model.model_id, model.display_name]));

  const [composer, setComposer] = useWorkspaceState("chat.composer", "");
  // 回答の前に選ぶ類似の承認済み FAQ（選ぶまで送らない。#684）。選ぶ前に画面を離れても、戻ったとき・
  // 再読込で同じ質問と候補を出す（作業状態。#702）。検索・回答プロファイルを変えたら消す。
  const [faqChoice, setFaqChoice] = useWorkspaceState<FaqChoice | null>(
    "chat.faqChoice",
    null,
    isFaqChoice
  );
  // 回答の前に答える確認の質問（検索・回答プロファイルのルール。#717）。類似問の提示の後に出し、作業状態に残す。
  const [clarifyChoice, setClarifyChoice] = useWorkspaceState<ClarifyChoice | null>(
    "chat.clarifyChoice",
    null,
    isClarifyChoice
  );
  const pendingChoice = faqChoice !== null || clarifyChoice !== null;
  // 会話の履歴は既定で閉じる（ChatGPT・Claude・Gemini・Copilot と同じ。多くの利用者は履歴を使わないので、
  // チャットに面積を渡す。#664）。lg 以上のインラインのパネルの開閉は作業状態に残す。
  // lg 未満のモーダルの side sheet は残さない（戻ったとき・再読込で画面を塞がない。workspace-state.md）。
  // 開閉の判定・シートの開閉は共通の useChatHistoryPanel（3 製品共通。#1161）。
  const [historyPanelOpen, setHistoryPanelOpen] = useWorkspaceState("chat.historyOpen", false);
  const history = useChatHistoryPanel({
    inlineOpen: historyPanelOpen,
    onInlineOpenChange: setHistoryPanelOpen,
  });
  const [liveTurn, setLiveTurn] = useState<LiveTurn | null>(null);
  // 会話の添付（#2517。運用設定で有効なときだけ入力欄に出す）。送った質問の添付は送信まで持つ。
  const chatAttachments = useRagChatAttachments();
  const outgoingAttachmentsRef = useRef<{ ids: string[]; items: ChatAttachmentItem[] }>({ ids: [], items: [] });
  // 配信で受け取った失敗の「詳細」と保存できなかった記録（回答のメッセージごと）。取り直した会話（保存済みの回答）
  // には無いので、この画面の間は同じ回答に引き継ぐ（#2342）。
  const [answerOutcomes, setAnswerOutcomes] = useState<
    Record<string, { failure?: ColumnFailureDetail; storageFailures?: StorageFailure[] }>
  >({});
  const [errorText, setErrorText] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const previousSearchAnswerProfileIdRef = useRef(searchAnswerProfileId);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  // 検索・回答プロファイルを切り替えたら会話選択と進行中ストリームをリセットする。
  useEffect(() => {
    if (previousSearchAnswerProfileIdRef.current === searchAnswerProfileId) return;
    previousSearchAnswerProfileIdRef.current = searchAnswerProfileId;
    // 送信・生成を打ち切り、送信中の状態も解く（cancelSending と同じ。照会の応答を待たない）。
    abortRef.current?.abort();
    abortRef.current = null;
    setSending(false);
    setActiveId(null);
    setLiveTurn(null);
    setFaqChoice(null);
    setClarifyChoice(null);
    setErrorText("");
    cancelRename();
  }, [searchAnswerProfileId, setActiveId, setFaqChoice, setClarifyChoice, cancelRename]);

  // 会話の欄の自動スクロール（3 製品共通の useChatAutoScroll。#1161）。末尾を見ている間は新しいメッセージ・受信中の回答に
  // 合わせて末尾へ追い、上を読んでいる間は引き戻さず「最新へ」を出す。会話を開いたら末尾から読み始める。
  const scrollContentKey = useMemo(
    () => ({}),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 変わったことだけを知らせる値（中身は使わない）
    [persistedMessages.length, liveTurn, faqChoice, clarifyChoice]
  );
  const autoScroll = useChatAutoScroll({
    contentKey: scrollContentKey,
    resetKey: `${searchAnswerProfileId ?? ""}:${activeId ?? ""}`,
  });

  useEffect(() => {
    const targetId = location.hash.slice(1);
    if (!targetId || !persistedMessages.length) return;
    const animationFrame = window.requestAnimationFrame(() => {
      const container = autoScroll.logElementRef.current;
      const target = document.getElementById(targetId);
      if (!container || !target || !container.contains(target)) return;
      const offset = target.getBoundingClientRect().top - container.getBoundingClientRect().top;
      container.scrollTo({ top: container.scrollTop + offset - (container.clientHeight - target.clientHeight) / 2 });
    });
    return () => window.cancelAnimationFrame(animationFrame);
  }, [location.hash, persistedMessages.length, autoScroll.logElementRef]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const liveTurnRef = useRef<LiveTurn | null>(liveTurn);
  const activeIdRef = useRef(activeId);
  useLayoutEffect(() => {
    liveTurnRef.current = liveTurn;
    activeIdRef.current = activeId;
  });
  /** 今の配信の接続（張り直すときに閉じる）。送信・停止の中止（`abortRef`）とは別。 */
  const connectionRef = useRef<AbortController | null>(null);
  /** 最後に配信のバイト（event・heartbeat）が届いた時刻。 */
  const streamActivityAtRef = useRef(0);
  /** 配信が途絶え、続きを購読し直している（「接続を確認しています」。#1160）。 */
  const [streamReconnecting, setStreamReconnecting] = useState(false);

  /** 配信を閉じて送信中の状態を解く（回答がそろった・保存済みの会話に引き継いだとき）。 */
  function finishStream() {
    const controller = abortRef.current;
    abortRef.current = null;
    controller?.abort();
    setSending(false);
    setStreamReconnecting(false);
    void queryClient.invalidateQueries({ queryKey: ["answer-records"] });
    void queryClient.invalidateQueries({ queryKey: ["conversations"] });
  }

  /**
   * 配信が途絶えた（接続は開いたまま、event・heartbeat が届かない）ときに、今の接続を閉じる。送信の処理
   * （`deliver`）が続きから購読し直す（#1175）。接続を張り直している間（応答を待つ・次の試行を待つ）は何もしない。
   */
  function reconnectStalledStream() {
    const connection = connectionRef.current;
    if (!connection) return;
    if (Date.now() - streamActivityAtRef.current < CHAT_STREAM_STALE_AFTER_MS) return;
    connection.abort();
  }

  /**
   * 続きを購読できない（別の worker・再起動の後・続けて失敗した）ときに、保存済みの会話（作成中の回答）へ
   * 引き継ぐ（#1175）。作成中の回答は会話の欄に「作成中」で出て、会話の取り直し（polling）で完了に変わる。
   * 会話を取り直せなければ、理由と「再送信」を出す。
   */
  async function handOffToSavedAnswer(conversationId: string, userMessageId: string) {
    let saved = false;
    try {
      // 取り直せなかったときに会話の欄を「読み込めませんでした」にしない（下の理由と「再送信」を出す）よう、
      // キャッシュの取得ではなく直接取り、取れたときだけキャッシュに入れる。
      const detail = await api.getConversation(conversationId);
      queryClient.setQueryData(queryKeys.conversation(conversationId), detail);
      saved = detail.messages.some((message) => message.message_id === userMessageId);
    } catch {
      // 会話を取り直せない（通信が戻らない）。下で理由と「再送信」を出す。
    }
    // 取り直している間に別の送信・停止に移っていたら何もしない。
    if (liveTurnRef.current?.user?.message_id !== userMessageId || abortRef.current === null) return;
    finishStream();
    if (saved) {
      setLiveTurn(null);
      return;
    }
    setLiveTurn((current) =>
      current
        ? {
            ...current,
            pending: withOptimisticChatStatus(current.pending, "failed"),
            columns: current.columns.filter((column) => column.status !== "streaming"),
            failureMessage: t("chat.stream.lost"),
          }
        : current
    );
  }


  // 送信中・停止した質問は、保存済みの会話の同じ質問を出さない（画面が決めた id で `start` の前から除く。#1175）。
  const liveUserMessageId = liveTurn ? (liveTurn.user?.message_id ?? liveTurn.clientMessageId) : undefined;
  const turns = useMemo(
    () =>
      buildTurns(persistedMessages).filter(
        (turn) => turn.user.message_id !== liveUserMessageId
      ),
    [persistedMessages, liveUserMessageId]
  );
  // 保存済みの作成中の回答がある質問（再読込の後・配信を購読し直せなかったとき。#1175）。完了まで送信を止め、
  // 送信のボタンは「停止」（取消の API）にする。
  const savedStreamingUserId =
    turns.find((turn) => turn.replies.some((reply) => reply.status === "STREAMING"))?.user.message_id ?? null;
  const answerInProgress = sending || savedStreamingUserId !== null;

  // 処理の経過は回答ごとの trace の API（共通の span の表の snapshot。#3226）を列ごとに読む（`useColumnProgress`）。
  // 回答の配信（SSE）は回答の本文と終わりだけを運ぶ。配信のバイト（event・heartbeat）が途絶えたら接続を張り直し、
  // 続きから購読し直す（#1160 / #1175）。
  const liveStreaming = sending && liveTurn?.user != null;
  useEffect(() => {
    if (!liveStreaming) return;
    const timer = window.setInterval(reconnectStalledStream, CHAT_STREAM_STALL_CHECK_MS);
    return () => window.clearInterval(timer);
  }, [liveStreaming]);

  function selectConversation(id: string) {
    // lg 未満のシートは、会話を選んだら閉じる（開閉ボタンへフォーカスを戻す）。
    history.closeSheet();
    if (id === activeId) return;
    cancelSending();
    setLiveTurn(null);
    setErrorText("");
    setActiveId(id);
  }

  function focusComposer() {
    requestAnimationFrame(() => composerRef.current?.focus());
  }

  async function startNewConversation() {
    if (!searchAnswerProfileId) return;
    setErrorText("");
    // 送れなかった・止めた質問は、新しい会話に持ち越さない（#907）。
    if (liveTurn && liveTurn.pending.status !== "sending") setLiveTurn(null);
    const emptyConversation = conversations.find(
      (conversation) => conversation.status === "ACTIVE" && conversation.message_count === 0
    );
    if (emptyConversation) {
      // 空と分かっている会話なので、内容（空）を先に入れて読み込み中にしない（#1188）。
      if (!queryClient.getQueryData(queryKeys.conversation(emptyConversation.id))) {
        queryClient.setQueryData<ConversationDetail>(queryKeys.conversation(emptyConversation.id), {
          ...emptyConversation,
          messages: [],
        });
      }
      selectConversation(emptyConversation.id);
      focusComposer();
      return;
    }
    try {
      const created = await createConversation.mutateAsync({ search_answer_profile_id: searchAnswerProfileId });
      // 作った会話の内容（空）を先に入れる。読み込み中の間だけ入力欄が無効になり、その間に書いた文字が
      // 入らない（#413 の e2e の不安定の原因）ことを防ぐ（最初の送信で会話を作るときと同じ。#664）。
      queryClient.setQueryData(queryKeys.conversation(created.id), created);
      // 新しい会話は一覧の先頭（更新日時の新しい順）に入るので、1 ページ目に戻して見せる。
      setConversationOffset(0);
      // 前の会話への送信・生成は、別の会話を選んだときと同じく止める（新しい会話を送信中のままにしない）。
      cancelSending();
      setActiveId(created.id);
      setLiveTurn(null);
      focusComposer();
    } catch {
      setErrorText(t("chat.error.send"));
    }
  }

  function updateColumn(modelId: string, patch: Partial<LiveColumn>) {
    setLiveTurn((current) => {
      if (!current) return current;
      return {
        ...current,
        columns: current.columns.map((column) =>
          column.model_id === modelId ? { ...column, ...patch } : column
        ),
      };
    });
  }

  /** 回答の場所（作成中の表示）。`start` で列の構成が届くまでは、既定のモデルで仮に作る。 */
  function placeholderColumns(startedAtMs: number): LiveColumn[] {
    const modelIds = [""];
    return modelIds.map((modelId) => ({
      model_id: modelId,
      label: modelId ? (modelLabels.get(modelId) ?? modelId) : "",
      answer: "",
      citations: [],
      status: "streaming",
      traceId: null,
      errorMessage: null,
      answerDiagnostics: null,
      messageId: null,
      startedAtMs,
    }));
  }

  /** 送った質問を、サーバーの応答を待たずに会話の欄の末尾へ出す 1 往復（#907）。 */
  function newLiveTurn(request: DeliverRequest, clientMessageId = newChatClientMessageId()): LiveTurn {
    const pending = createOptimisticChatMessage(request.content);
    return {
      pending,
      clientMessageId,
      user: null,
      columns: placeholderColumns(pending.sentAtMs),
      failureMessage: null,
      request,
      attachments: outgoingAttachmentsRef.current.items,
    };
  }

  /**
   * 送信・生成を打ち切り、送信中の状態をすぐ解く（別の会話・検索・回答プロファイルに移るとき）。
   * 類似問・確認の照会は中止できず、応答を待つ間は後始末（`releaseController`）に届かないため、
   * ここで `sending` を戻す（戻さないと移った先でも「停止」のままになり送信できない）。
   */
  function cancelSending() {
    abortRef.current?.abort();
    abortRef.current = null;
    setSending(false);
  }

  /** 終わった・止めた送信の後始末（後から始めた送信の状態は変えない）。 */
  function releaseController(controller: AbortController) {
    if (abortRef.current !== controller) return;
    abortRef.current = null;
    setSending(false);
  }

  /**
   * 送信する。`retryContent` を渡すと、入力欄ではなくその質問（失敗した回答の質問）を送り直す。
   * 送った質問は、類似問・確認の照会や会話の作成を待たずにすぐ会話の欄へ出し、入力欄を空にする（#907）。
   * 検索・回答プロファイルの類似問の提示がオンで近い承認済み FAQ があれば、回答を作る前に最大 3 件と「どれでもない」を
   * 出し、どれかを選ぶまで送らない（#684）。照会に失敗したときは類似問を使わずに送る。
   */
  async function send(retryContent?: string) {
    const content = (retryContent ?? composer).trim();
    if (
      !content ||
      !searchAnswerProfileId ||
      answerInProgress ||
      pendingChoice ||
      searchAnswerProfileWithoutKnowledgeBases ||
      prerequisitesLoading ||
      conversationFailed ||
      chatAttachments.uploading
    ) {
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setSending(true);
    setErrorText("");
    // 添付は入力欄から外し、送った質問の下に出す（#2517）。同じ質問の送り直しは添付を付けない。
    outgoingAttachmentsRef.current =
      retryContent === undefined ? chatAttachments.take() : { ids: [], items: [] };
    if (retryContent === undefined) setComposer("");
    setLiveTurn(newLiveTurn({ content }));
    // 送った質問は会話の欄の末尾に出す。上を読んでいても末尾へ戻る（messaging.md §11.1）。
    autoScroll.scrollToLatest();
    let suggestions: ApprovedFaqSuggestionData[] = [];
    try {
      suggestions = (await api.suggestApprovedFaq(searchAnswerProfileId, content, "chat")).suggestions ?? [];
    } catch {
      // 類似問は補助。照会できなくても回答は作る。
    }
    if (controller.signal.aborted) return;
    if (suggestions.length > 0) {
      // 選ぶまで送らない。質問は類似問の選択と一緒に同じ位置へ出す。
      setLiveTurn(null);
      setFaqChoice({ content, retryContent, suggestions });
      releaseController(controller);
      return;
    }
    await clarifyOrDeliver(content, controller);
  }

  /** 類似問の選択を確定して送る（`faqId` が無ければ「どれでもない」）。 */
  function chooseFaq(faqId?: string) {
    if (!faqChoice) return;
    const { content, retryContent } = faqChoice;
    setFaqChoice(null);
    setSending(true);
    // 類似問を選ぶと資料を検索しないので、確認の質問は「どれでもない」のときだけ出す（#717）。
    if (faqId) {
      void deliver({ content, approvedFaqId: faqId });
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setLiveTurn(newLiveTurn({ content }));
    void clarifyOrDeliver(content, controller, retryContent);
  }

  /** 確認と正式送信が同じ会話を使うよう、文脈の準備より前に会話を確定する。 */
  async function conversationForSend(controller: AbortController): Promise<string | null> {
    if (controller.signal.aborted || !searchAnswerProfileId) return null;
    if (activeIdRef.current) return activeIdRef.current;
    const created = await createConversation.mutateAsync({ search_answer_profile_id: searchAnswerProfileId });
    if (controller.signal.aborted) return null;
    queryClient.setQueryData(queryKeys.conversation(created.id), created);
    setConversationOffset(0);
    activeIdRef.current = created.id;
    setActiveId(created.id);
    return created.id;
  }

  /**
   * 質問が検索・回答プロファイルのルールの確認に当たれば、回答の前に確認の質問を出す（#717）。1 つの質問で聞き返すのは
   * 1 回だけ。照会に失敗したときは確認を使わずに送る。
   */
  async function clarifyOrDeliver(content: string, controller: AbortController, retryContent?: string) {
    if (!searchAnswerProfileId) return;
    let conversationId: string | null;
    try {
      conversationId = await conversationForSend(controller);
    } catch (error) {
      if (!controller.signal.aborted) {
        setLiveTurn((current) => current ? {
          ...current,
          pending: withOptimisticChatStatus(current.pending, "failed"),
          failureMessage: error instanceof ApiError ? error.message : t("chat.error.send"),
        } : current);
        releaseController(controller);
      }
      return;
    }
    if (!conversationId || controller.signal.aborted) return;
    let suggestion: ClarificationSuggestionData | null = null;
    let questionContextToken: string | undefined;
    try {
      const prepared = await api.suggestClarification(searchAnswerProfileId, content, conversationId);
      suggestion = prepared.suggestion ?? null;
      questionContextToken = prepared.question_context_token ?? undefined;
    } catch {
      // 確認は補助。照会できなくても回答は作る。
    }
    if (controller.signal.aborted) return;
    if (suggestion) {
      setLiveTurn(null);
      setClarifyChoice({ content, retryContent, suggestion, questionContextToken });
      releaseController(controller);
      return;
    }
    await deliver({ content, questionContextToken }, controller);
  }

  /** 確認の答えを確定して送る（`answer` が無ければ「確認せずに回答」。前提を示して回答する。#1839）。 */
  function chooseClarification(answer?: ClarificationAnswer) {
    if (!clarifyChoice) return;
    const { content, suggestion, questionContextToken } = clarifyChoice;
    setClarifyChoice(null);
    setSending(true);
    void deliver(
      answer
        ? { content, clarification: answer, questionContextToken }
        : { content, clarificationSkippedRuleId: suggestion.rule_id, questionContextToken }
    );
  }

  /** 範囲を指定せずに同じ質問を送る（類似問・確認の質問は出さない。#721）。 */
  function askUnscoped(content: string) {
    setSending(true);
    setErrorText("");
    void deliver({ content });
  }

  /** 送信できなかった質問を、同じ条件でもう一度送る（類似問・確認は聞き直さない。#907）。 */
  function resend() {
    if (!liveTurn || sending || pendingChoice) return;
    setSending(true);
    setErrorText("");
    // 同じ質問（同じ localId）を送信中に戻す。経過時間は 0 から数え直す。
    const pending = withOptimisticChatStatus(liveTurn.pending, "sending");
    setLiveTurn({
      ...liveTurn,
      pending,
      user: null,
      columns: placeholderColumns(pending.sentAtMs),
      failureMessage: null,
    });
    void deliver(liveTurn.request);
  }

  async function deliver(request: DeliverRequest, existingController?: AbortController) {
    if (!searchAnswerProfileId) return;
    const { content, approvedFaqId, clarification, clarificationSkippedRuleId, questionContextToken } = request;
    const controller = existingController ?? new AbortController();
    abortRef.current = controller;
    // 質問の id は画面が決める（#1175）。送り直すたびに新しくする（同じ id の二重の保存は 409）。
    const clientMessageId = newChatClientMessageId();
    // 送信の直後に出した質問はそのまま使う。類似問・確認の選択の後は、ここで会話の欄に出す（#907）。
    setLiveTurn((current) =>
      current &&
      current.user === null &&
      current.pending.status === "sending" &&
      current.pending.content === content
        ? { ...current, request, clientMessageId }
        : newLiveTurn(request, clientMessageId)
    );
    let started = false;
    // 最後に処理した event の連番（再購読はこの次から。#1175）と、今の接続で受け取った event の数。
    let lastEventId = 0;
    let eventsOnConnection = 0;
    stampNow(streamActivityAtRef);
    const handlers: ChatStreamHandlers = {
      onStart: ({ user_message, columns }) => {
        started = true;
        // 送った会話は一覧の先頭へ移るので、1 ページ目に戻して選択中の行を見せる（#403）。
        setConversationOffset(0);
        void queryClient.invalidateQueries({ queryKey: ["conversations"] });
        // 仮の質問を保存済みの質問に置き換える（取り直した会話の同じ質問は turns から除く）。
        // 経過時間は送信した時刻から数え続ける。
        setLiveTurn((current) =>
          current
            ? {
                ...current,
                user: user_message,
                columns: columns.map((column: ChatColumn) => ({
                  model_id: column.model_id,
                  label: column.label,
                  answer: "",
                  citations: [],
                  status: "streaming",
                  traceId: null,
                  errorMessage: null,
                  answerDiagnostics: null,
                  messageId: column.message_id ?? null,
                  startedAtMs: current.pending.sentAtMs,
                })),
              }
            : current
        );
      },
      onDelta: (modelId, text) => {
        setLiveTurn((current) => {
          if (!current) return current;
          return {
            ...current,
            columns: current.columns.map((column) =>
              column.model_id === modelId ? { ...column, answer: column.answer + text } : column
            ),
          };
        });
      },
      onMetadata: ({ model_id, message_id, trace_id, answer_diagnostics, storage_failures, notice }) => {
        updateColumn(model_id, {
          traceId: trace_id,
          answerDiagnostics: answer_diagnostics ?? null,
          storageFailures: storage_failures ?? [],
          notice: notice ?? null,
        });
        if (message_id && storage_failures?.length) {
          setAnswerOutcomes((current) => ({ ...current, [message_id]: { storageFailures: storage_failures } }));
        }
      },
      onCitations: (modelId, citations) => updateColumn(modelId, { citations }),
      onModelDone: ({ model_id, message_id }) => {
        updateColumn(model_id, { status: "done", ...(message_id ? { messageId: message_id } : {}) });
      },
      onModelError: ({ model_id, message_id, message, cancelled, trace_id, error_type, error_code, stage, occurred_at }) => {
        const failure = { errorType: error_type, errorCode: error_code, stage, occurredAt: occurred_at };
        updateColumn(model_id, {
          ...(cancelled ? { status: "stopped" as const } : { status: "error" as const }),
          errorMessage: message,
          failure,
          ...(trace_id ? { traceId: trace_id } : {}),
        });
        if (message_id && !cancelled) {
          setAnswerOutcomes((current) => ({ ...current, [message_id]: { failure } }));
        }
      },
      onAllDone: async () => {
        void queryClient.invalidateQueries({ queryKey: ["answer-records"] });
        // 取り直した会話に引き継いでから外す（質問と回答を一瞬消したり、二重に出したりしない）。
        await queryClient.invalidateQueries({ queryKey: ["conversations"] });
        // 会話に保存できなかった回答は、取り直した会話に無い（保存できなかった理由の文になる）ので、この画面の
        // 間は作った回答を出したままにする（#2342）。次の送信で新しい往復に置き換わる。
        setLiveTurn((current) =>
          current?.columns.some((column) =>
            column.storageFailures?.some((failure) => failure.step === "chat_message_save")
          )
            ? current
            : null
        );
      },
      onActivity: () => {
        stampNow(streamActivityAtRef);
        setStreamReconnecting(false);
      },
      onEventId: (id) => {
        lastEventId = id;
        eventsOnConnection += 1;
      },
    };
    let resumeFailures = 0;
    try {
      const conversationId = await conversationForSend(controller);
      if (!conversationId || controller.signal.aborted) return;
      const attachmentIds = outgoingAttachmentsRef.current.ids;
      outgoingAttachmentsRef.current = { ids: [], items: [] };
      const body = {
        content,
        ...(questionContextToken ? { question_context_token: questionContextToken } : {}),
        model_ids: [],
        client_message_id: clientMessageId,
        ...(attachmentIds.length ? { attachment_ids: attachmentIds } : {}),
        ...(approvedFaqId ? { approved_faq_id: approvedFaqId } : {}),
        ...(clarification ? { clarification } : {}),
        ...(clarificationSkippedRuleId
          ? { clarification_skipped_rule_id: clarificationSkippedRuleId }
          : {}),
      };
      // 回答の作成は接続から切り離されている（#1175）。`all_done` の前に接続が切れたら、続きから購読し直す。
      while (!controller.signal.aborted) {
        const connection = new AbortController();
        const closeConnection = () => connection.abort();
        controller.signal.addEventListener("abort", closeConnection, { once: true });
        connectionRef.current = connection;
        eventsOnConnection = 0;
        let gone = false;
        try {
          const outcome = started
            ? await resumeChatStream(conversationId, clientMessageId, lastEventId, handlers, connection.signal)
            : await streamChatMessage(conversationId, body, handlers, connection.signal);
          if (outcome.completed) return;
          // `all_done` の前に配信が終わった。質問の保存の前なら送信の失敗として扱う。
          if (!started) throw new Error("chat stream ended before start");
        } catch (error) {
          if (controller.signal.aborted) return;
          // 質問の保存の前の失敗は、送信の失敗（理由と「再送信」）。
          if (!started) throw error;
          gone = isChatStreamGone(error);
        } finally {
          controller.signal.removeEventListener("abort", closeConnection);
          if (connectionRef.current === connection) connectionRef.current = null;
        }
        // 質問の保存の後に接続が切れた。回答の作成は続いているので、「接続を確認しています」を出して購読し直す。
        setStreamReconnecting(true);
        resumeFailures = eventsOnConnection > 0 ? 1 : resumeFailures + 1;
        if (gone || resumeFailures > CHAT_STREAM_RESUME_ATTEMPTS) {
          // この接続先では続きを購読できない。保存済みの会話（作成中の回答）に引き継いで polling で待つ。
          await handOffToSavedAnswer(conversationId, clientMessageId);
          return;
        }
        const delay = Math.min(CHAT_STREAM_RESUME_MAX_DELAY_MS, 1_000 * 2 ** (resumeFailures - 1));
        if (!(await waitOrAbort(delay, controller.signal))) return;
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        // 送った質問は会話の欄に残し、理由と「再送信」を出す（入力を失わない。#907）。
        const message =
          error instanceof ApiError ? error.messages.join(" / ") : t("chat.send.failedHint");
        setLiveTurn((current) =>
          current
            ? {
                ...current,
                pending: withOptimisticChatStatus(current.pending, "failed"),
                // 質問の保存の前の失敗なので、回答の場所（作成中の表示）は外す。
                columns: [],
                failureMessage: message,
              }
            : current
        );
      }
    } finally {
      releaseController(controller);
    }
  }

  /**
   * 生成を止める。送った質問は会話の欄に残す（#907）。接続を切っても回答の作成は止まらないので、取消の API で
   * 止める（#1175）。質問の保存の前（`start` の前）でも、画面が決めた質問の id で取り消せる。
   */
  function stop() {
    if (!liveTurn && savedStreamingUserId && activeId) {
      // 保存済みの作成中の回答（再読込の後など）を止める。
      void cancelAnswer(activeId, savedStreamingUserId);
      return;
    }
    const conversationId = activeIdRef.current;
    const messageId = liveTurn?.user?.message_id ?? liveTurn?.clientMessageId ?? null;
    abortRef.current?.abort();
    abortRef.current = null;
    setSending(false);
    setLiveTurn((current) =>
      current
        ? {
            ...current,
            pending: withOptimisticChatStatus(current.pending, "stopped"),
            columns: [],
          }
        : current
    );
    if (conversationId && messageId) void cancelAnswer(conversationId, messageId);
  }

  /** 回答の作成を取り消し、保存済みの内容（停止した回答）を取り直す。 */
  async function cancelAnswer(conversationId: string, messageId: string) {
    try {
      await api.cancelChatAnswer(conversationId, messageId);
    } catch {
      // 取消に失敗しても、作成は上限の時間で終わる。会話の取り直しで今の状態を出す。
    }
    void queryClient.invalidateQueries({ queryKey: ["conversations"] });
  }

  const searchAnswerProfileLoading = searchAnswerProfilesQuery.isLoading;
  // 一覧を読めなかったときは「0 件」と同じに扱わない（空の状態を出さず、失敗と再試行を出す。#1153）。
  const searchAnswerProfilesFailed = searchAnswerProfilesQuery.isError && !searchAnswerProfilesQuery.data;
  const noSearchAnswerProfiles =
    !searchAnswerProfileLoading && !searchAnswerProfilesFailed && searchAnswerProfiles.length === 0;
  const liveColumns = liveTurn
    ? liveTurn.columns.map((column) => ({
        key: column.model_id || "default",
        label: column.label,
        answer: column.answer,
        citations: column.citations,
        traceId: column.traceId,
        messageId: null,
        streaming: column.status === "streaming",
        errorMessage: column.status === "error" ? column.errorMessage : null,
        failure: column.status === "error" ? (column.failure ?? null) : null,
        storageFailures: column.storageFailures,
        stoppedMessage: column.status === "stopped" ? column.errorMessage : null,
        answerDiagnostics: column.answerDiagnostics,
        notice: column.status === "done" ? (column.notice ?? null) : null,
        progressSource: {
          conversationId: activeId,
          messageId: column.messageId,
          active: column.status === "streaming",
          startedAtMs: column.startedAtMs,
          // 配信が途絶え、接続を張り直している間は「接続を確認しています」（#1160）。
          reconnecting: streamReconnecting && column.status === "streaming",
        },
      }))
    : [];
  const lastTurnId = turns.length ? turns[turns.length - 1].user.message_id : null;

  // 会話の履歴に出す期間（最後の更新が直近 N 日。3 製品共通の文言。#2340）。
  const historyWindow = chatHistoryWindowLabels(conversationsData?.history_days);
  // 会話の履歴の中身（読み込み中・失敗・0 件・一覧・ページ送り）。lg 以上はインラインのパネル、
  // lg 未満はモーダルの side sheet に入れる（どちらか一方だけを描く）。
  const historyContent = (
    // 一覧の行・読み込み中・失敗・0 件は 3 製品共通の ChatHistoryList（#1161）。名前の変更・削除も 3 製品共通（#1817）。
    <ChatHistoryList
      items={conversations.map((conversation) => ({
        id: conversation.id,
        title: conversation.title ?? t("chat.sessions.untitled"),
        meta: seesAllUsers && conversation.owned === false
          ? // system_admin が見る他の利用者の会話には、利用者の表示名を添える（#1889）。
            t("chat.sessions.owner", {
              meta: t("chat.sessions.metadata", {
                count: conversation.message_count,
                updatedAt: formatDateTime(conversation.updated_at),
              }),
              name: conversation.actor_display_name || t("chat.sessions.ownerMissing"),
            })
          : t("chat.sessions.metadata", {
              count: conversation.message_count,
              updatedAt: formatDateTime(conversation.updated_at),
            }),
      }))}
      currentId={activeId}
      onSelect={(item) => selectConversation(item.id)}
      // 検索・回答プロファイルの読み込み中は会話の一覧をまだ取得できない。「まだ会話がありません」と出さず、
      // 一覧の形だけを出す（経過時間は上のカードが出しているので重ねない。#1153）。
      waiting={searchAnswerProfileLoading}
      loading={conversationsQuery.isLoading}
      error={conversationsQuery.isError ? conversationsQuery.error : null}
      onRetry={() => void conversationsQuery.refetch()}
      retrying={conversationsQuery.isFetching}
      labels={{
        list: t("chat.sessions.title"),
        loading: t("chat.sessions.loading"),
        error: t("chat.sessions.error"),
        retry: t("common.retry"),
        // 期間で絞った一覧の 0 件は期間を含めた文（3 製品共通。#2340）。
        empty: historyWindow?.empty ?? t("chat.sessions.empty"),
      }}
      operationKey="chat-conversations-load"
      testIds={{
        skeleton: "chat-conversations-skeleton",
        loading: "chat-conversations-loading",
        error: "chat-conversations-error",
        list: "chat-conversation-list",
        pagination: "chat-conversations-pagination",
      }}
      renderEditor={historyActions.renderEditor}
      renderActions={historyActions.renderActions}
      // 3 製品共通の会話の履歴のページング（#1265 / #1266）。次のページを取得している間は、表示中のページの範囲を出す。
      pagination={
        conversationsData
          ? {
              type: "offset",
              offset: conversationsData.offset,
              limit: DEFAULT_PAGE_SIZE,
              total: conversationsData.total,
              count: conversations.length,
              onPageChange: (_page, nextOffset) => setConversationOffset(nextOffset),
              labels: paginationLabels(),
              ariaLabel: t("chat.sessions.pagination"),
            }
          : undefined
      }
    />
  );
  const activeConversation =
    conversationQuery.data ?? conversations.find((conversation) => conversation.id === activeId);
  const activeTitle = activeConversation?.title ?? t("chat.sessions.untitled");
  // system_admin が開いた他の利用者の会話は閲覧と停止・削除だけ（#3244）。続きの質問・再実行・確認への回答・
  // 評価は持ち主だけなので、入力欄の代わりに閲覧だけの案内を出し、続きの操作を出さない。
  const readOnly = Boolean(activeId) && activeConversation?.owned === false;

  const targetSelector = (<>

            {searchAnswerProfileLoading ? (
              <TimedLoadingState
                label={t("chat.searchAnswerProfile.loading")}
                operationKey="chat-search-answer-profiles-load"
                framed={false}
                testId="chat-search-answer-profiles-loading"
              >
                <SearchAnswerProfileSelectSkeleton />
              </TimedLoadingState>
            ) : searchAnswerProfilesFailed ? (
              <ApiErrorState
                error={searchAnswerProfilesQuery.error}
                fallback={t("chat.searchAnswerProfile.error")}
                onRetry={() => void searchAnswerProfilesQuery.refetch()}
              />
            ) : noSearchAnswerProfiles ? (
              <EmptyState
                title={t("chat.searchAnswerProfile.empty")}
                action={
                  <Button onClick={() => navigate(APP_ROUTES.searchAnswerProfiles)} variant="secondary">
                    {t("chat.searchAnswerProfile.open")}
                  </Button>
                }
              />
            ) : (
              <div className="space-y-4">
                {/* RAG 検索と同じ部品・文言・幅（#635）。 */}
                <SearchAnswerProfileSelect
                  id="chat-search-answer-profile"
                  compact
                  items={searchAnswerProfiles}
                  value={searchAnswerProfileId}
                  onChange={setSearchAnswerProfileId}
                />
                {searchAnswerProfileWithoutKnowledgeBases && searchAnswerProfileId ? (
                  <Banner
                    severity="warning"
                    action={
                      canOpenSearchAnswerProfiles ? (
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() =>
                            navigate(
                              `${APP_ROUTES.searchAnswerProfiles}?id=${encodeURIComponent(searchAnswerProfileId)}`
                            )
                          }
                        >
                          {t("chat.searchAnswerProfile.openSettings")}
                        </Button>
                      ) : undefined
                    }
                  >
                    {t("chat.searchAnswerProfile.noKnowledgeBases")}
                  </Banner>
                ) : null}
              </div>
            )}

  </>);

  return (
    <div className="flex min-h-full shrink-0 flex-col lg:h-full lg:min-h-0">
      <PageHeader wide title={t("chat.title")} subtitle={t("chat.subtitle")} />

      <PageBody wide className="flex min-h-0 flex-1 flex-col gap-4">


        {/* 検索・回答プロファイルの読み込み中も会話の領域を描き（寸法を予約し、読み込み後に現れて押し下げない）、
            空の状態の代わりに会話の形の Skeleton を出す（messaging.md §11.7、#1153）。 */}
        {searchAnswerProfilesFailed || noSearchAnswerProfiles ? targetSelector : null}
        {searchAnswerProfilesFailed || noSearchAnswerProfiles ? null : (
          // 骨格（履歴のパネル・シート、会話の領域の上端の行・会話の欄・入力欄の領域）は 3 製品共通の ChatLayout（#1161）。
          <ChatLayout
            targetSelector={targetSelector}
            history={history}
            historyTitle={t("chat.sessions.title")}
            historyCloseLabel={t("chat.sessions.close")}
            historyCaption={historyWindow?.caption}
            historyContent={historyContent}
            historyToggleDisabled={searchAnswerProfileLoading}
            label={t("chat.title")}
            // 会話を選んでいる間は、履歴を閉じていても今の会話の名前を出す。
            conversationTitle={activeId && activeConversation ? activeTitle : null}
            conversationTitleLoading={Boolean(activeId) && !activeConversation}
            newConversation={{
              label: t("chat.sessions.new"),
              onClick: () => void startNewConversation(),
              disabled: createConversation.isPending || searchAnswerProfileLoading || !searchAnswerProfileId,
            }}
            logLabel={t("chat.messages.label")}
            logRef={autoScroll.logRef}
            latest={{
              visible: autoScroll.showLatest,
              label: t("chat.messages.latest"),
              onClick: () => autoScroll.scrollToLatest("smooth"),
            }}
            testIds={{ log: "chat-messages" }}
            composer={
              readOnly ? (
                // 入力欄の代わりの閲覧だけの案内（3 製品共通の ChatReadOnlyNotice。#3244）。停止は作成中の回答が
                // あるときだけ出す。
                <ChatReadOnlyNotice
                  onStop={answerInProgress ? stop : undefined}
                  footer={
                    errorText ? (
                      <p className="text-sm text-danger-fg" role="alert">
                        {errorText}
                      </p>
                    ) : null
                  }
                />
              ) : (
              <>
                {/* 入力欄の領域（設定の行・入力欄と送信 / 停止）は 3 製品共通の ChatComposer（#1161）。
                    送信と停止は同じボタンで、生成中は同じ位置で「停止」になる（buttons.md §3.1、#413）。
                    生成中も入力できる（次の質問を書ける）。生成中の Enter は送らず、停止もしない（#413）。
                    IME の変換を確定する Enter では送信しない（#459）。会話を選んでいなくても入力でき、最初の送信で会話を作る（#664）。
                    検索・回答プロファイルの一覧の読み込み中は書けない（書いた文字は作業状態に残す。#1153）。
                    会話の内容の読み込み中は書けて、送信だけを止める（#1188）。 */}
                <ChatComposer
                  id="chat-composer"
                  textareaRef={composerRef}
                  value={composer}
                  onValueChange={setComposer}
                  onSubmit={() => void send()}
                  onStop={stop}
                  // 保存済みの作成中の回答（再読込の後など）がある間も「停止」（取消の API）にする（#1175）。
                  running={answerInProgress}
                  submitBlocked={
                    !searchAnswerProfileId ||
                    searchAnswerProfileWithoutKnowledgeBases ||
                    pendingChoice ||
                    conversationLoading ||
                    conversationFailed
                  }
                  disabled={searchAnswerProfileLoading}
                  label={t("chat.composer.label")}
                  placeholder={t("chat.composer.placeholder")}
                  sendLabel={t("chat.composer.send")}
                  stopLabel={t("chat.composer.stop")}
                  sendTestId="chat-run-stop"
                  // 会話の添付（#2517。運用設定で有効なときだけ）。
                  attachments={chatAttachments.composer}
                />
                {errorText ? (
                  <p className="text-sm text-danger-fg" role="alert">
                    {errorText}
                  </p>
                ) : null}
              </>
              )
            }
          >
            {/* 会話を選んでいない間は新しい会話の下書き。最初の送信で会話を作る（#664）。 */}
            {conversationLoading && !liveTurn ? (
              <TimedLoadingState
                label={t("chat.messages.loading")}
                operationKey="chat-conversation-load"
                framed={false}
                testId="chat-messages-loading"
              >
                {/* 質問と回答の吹き出しの寸法を予約する（3 製品で同じ形。#1153）。 */}
                <ChatSkeleton />
              </TimedLoadingState>
            ) : searchAnswerProfileLoading ? (
              // 検索・回答プロファイルの読み込み中は空の状態を出さず、会話の形の Skeleton だけを出す。
              // 経過時間は上のカードが出しているので重ねない（同じ取得の経過時間は 1 か所。#1153）。
              <ChatSkeleton testId="chat-messages-skeleton" />
            ) : activeId && conversationQuery.isError ? (
              <ErrorState
                message={t("chat.messages.error")}
                onRetry={() => void conversationQuery.refetch()}
              />
            ) : !searchAnswerProfileId ? (
              <EmptyState title={t("chat.searchAnswerProfile.required")} />
            ) : turns.length === 0 && !liveTurn && !pendingChoice ? (
              <EmptyState title={t("chat.messages.empty")} hint={t("chat.messages.emptyHint")} />
            ) : (
              <>
                {turns.map((turn) => (
                  <MessageTurn
                    key={turn.user.message_id}
                    user={turn.user}
                    searchAnswerProfileId={searchAnswerProfileId ?? ""}
                    readOnly={readOnly}
                    onRetry={
                      // 最新の質問の失敗（時間切れなど）だけ、同じ質問をもう一度送れる（#375）。他の利用者の会話では出さない。
                      !readOnly &&
                      turn.user.message_id === lastTurnId &&
                      turn.user.status !== "ERROR" &&
                      !liveTurn &&
                      !answerInProgress
                        ? () => void send(turn.user.content)
                        : undefined
                    }
                    onAskUnscoped={
                      !readOnly &&
                      turn.user.message_id === lastTurnId && !liveTurn && !answerInProgress && !pendingChoice
                        ? () => askUnscoped(turn.user.content)
                        : undefined
                    }
                    testId={turn.user.message_id === savedStreamingUserId ? "chat-saved-streaming-turn" : undefined}
                    columns={turn.replies.map((reply) => {
                      const streaming = reply.status === "STREAMING";
                      return {
                        key: reply.message_id,
                        // 保存した回答は model_id を持つ。生成中と同じ表示名に引き直す（#649）。
                        label: reply.model ? (modelLabels.get(reply.model) ?? reply.model) : null,
                        answer: streaming ? "" : reply.content,
                        citations: reply.citations,
                        withheldCount: reply.withheld_evidence_count ?? 0,
                        traceId: reply.trace_id,
                        messageId: reply.message_id,
                        // 作成中の回答（再読込の後など）は「作成中」で出し、会話の取り直しで完了に変わる（#1175）。
                        streaming,
                        errorMessage: reply.status === "ERROR" ? reply.content : null,
                        failure: reply.status === "ERROR" ? (answerOutcomes[reply.message_id]?.failure ?? null) : null,
                        storageFailures: answerOutcomes[reply.message_id]?.storageFailures,
                        stoppedMessage: reply.status === "CANCELLED" ? reply.content : null,
                        notice: reply.status === "COMPLETE" ? (reply.notice ?? null) : null,
                        // 段落の印の記録（#2738）。会話のメッセージに記録があるときだけ読む（無ければ今までの表示）。
                        passageSource: reply.status === "COMPLETE" ? reply : null,
                        savedAnswer: Boolean(
                          reply.trace_id && answerTraceIds.has(reply.trace_id)
                        ),
                        // 処理の経過（回答の trace の API。#3226）。作成中の回答（再読込の後・別の worker に引き継いだ後）は
                        // 読み直し、会話の取り直し（polling）で完了に変わる。
                        progressSource: {
                          conversationId: activeId,
                          messageId: reply.message_id,
                          active: streaming,
                          startedAtMs: Date.parse(reply.created_at) || Date.now(),
                        },
                      };
                    })}
                  />
                ))}
                {liveTurn ? (
                  // 送った質問はサーバーの応答を待たずに出し、失敗・停止のときも残す（#907）。
                  <MessageTurn
                    user={liveTurn.user ?? { content: liveTurn.pending.content }}
                    userStatus={
                      liveTurn.pending.status === "sending"
                        ? liveTurn.user
                          ? "sent"
                          : "sending"
                        : liveTurn.pending.status
                    }
                    columns={liveColumns}
                    liveAttachments={liveTurn.attachments}
                    searchAnswerProfileId={searchAnswerProfileId ?? ""}
                    testId="chat-live-turn"
                    footer={
                      liveTurn.pending.status === "failed" ? (
                        <div data-testid="chat-send-failure">
                          <Banner
                            severity="danger"
                            action={
                              <Button
                                type="button"
                                variant="secondary"
                                size="sm"
                                icon={RotateCcw}
                                disabled={sending || pendingChoice}
                                onClick={resend}
                              >
                                {t("chat.send.retry")}
                              </Button>
                            }
                          >
                            {liveTurn.failureMessage ?? t("chat.send.failedHint")}
                          </Banner>
                        </div>
                      ) : liveTurn.pending.status === "stopped" ? (
                        // 止めた状態。色だけに頼らず、停止のアイコンを添える（Agent のチャットと同じ）。
                        <p
                          className="flex items-center gap-1.5 text-sm text-fg-muted"
                          data-testid="chat-stopped"
                        >
                          <Square size={14} aria-hidden="true" />
                          {t("chat.send.stopped")}
                        </p>
                      ) : null
                    }
                  />
                ) : null}
                {/* 送る前の質問と、選んでから回答する類似問（#684）。質問の吹き出しは送信後と同じ形。 */}
                {faqChoice && !readOnly ? (
                  <div className="space-y-2" data-testid="chat-approved-faq-choice">
                    <ChatUserMessage>{faqChoice.content}</ChatUserMessage>
                    <ApprovedFaqSuggestions
                      mode="chat"
                      suggestions={faqChoice.suggestions}
                      onUse={(suggestion) => chooseFaq(suggestion.id)}
                      onSkip={() => chooseFaq()}
                    />
                  </div>
                ) : null}
                {/* 類似問の後に出す回答前の確認（#717 / #1839）。 */}
                {clarifyChoice && !readOnly ? (
                  <div className="space-y-2">
                    <ChatUserMessage>{clarifyChoice.content}</ChatUserMessage>
                    <ClarificationChoice
                      suggestion={clarifyChoice.suggestion}
                      onAnswer={(answer) => chooseClarification(answer)}
                      onSkip={() => chooseClarification()}
                    />
                  </div>
                ) : null}
              </>
            )}
          </ChatLayout>
        )}
      </PageBody>
    </div>
  );
}

interface FaqChoice {
  content: string;
  retryContent?: string;
  suggestions: ApprovedFaqSuggestionData[];
}

/** 保存した類似問の選択待ちが今の形か（古い版・壊れた値は捨てる。#702）。 */
function isFaqChoice(value: unknown): value is FaqChoice | null {
  if (value === null) return true;
  if (typeof value !== "object") return false;
  const choice = value as Partial<FaqChoice>;
  return (
    typeof choice.content === "string" &&
    (choice.retryContent === undefined || typeof choice.retryContent === "string") &&
    Array.isArray(choice.suggestions) &&
    choice.suggestions.every(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        typeof item.id === "string" &&
        typeof item.question === "string" &&
        typeof item.answer === "string" &&
        typeof item.score === "number"
    )
  );
}

interface ClarifyChoice {
  content: string;
  questionContextToken?: string;
  retryContent?: string;
  suggestion: ClarificationSuggestionData;
}

/** 保存した確認の選択待ちが今の形か（古い版・壊れた値は捨てる。#717）。 */
function isClarifyChoice(value: unknown): value is ClarifyChoice | null {
  if (value === null) return true;
  if (typeof value !== "object") return false;
  const choice = value as Partial<ClarifyChoice>;
  const suggestion = choice.suggestion as Partial<ClarificationSuggestionData> | undefined;
  return (
    typeof choice.content === "string" &&
    (choice.retryContent === undefined || typeof choice.retryContent === "string") &&
    typeof suggestion === "object" &&
    suggestion !== null &&
    typeof suggestion.rule_id === "string" &&
    typeof suggestion.clarification === "object" &&
    suggestion.clarification !== null &&
    Array.isArray(suggestion.clarification.options)
  );
}
