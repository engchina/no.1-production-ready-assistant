// NL2SQL のチャットの画面の共通部分（#2264）。「チャット」（SqlChatPage）と「DB 管理チャット」
// （DbAdminChatPage）が同じ部品・同じ文言で描くために使う。骨格・入力欄・送信中の往復・SQL の
// 表示・会話の履歴は 3 製品共通の部品（ChatLayout / ChatComposer / ChatPendingTurn / ChatProgress /
// ChatHistoryList）を通し、ここでは NL2SQL の文言と組み合わせだけを持つ（2 つの画面で書き写さない）。
import type { ReactNode } from "react";
import { RotateCcw } from "lucide-react";
import {
  ApiErrorBanner,
  Button,
  ChatComposer,
  ChatHistoryList,
  ChatLayout,
  chatHistoryWindowLabels,
  ChatPendingTurn,
  ChatProgress,
  sqlBlockLabels,
  DEFAULT_PAGE_SIZE,
  PageBody,
  PageHeader,
  type ChatAutoScroll,
  type ChatComposerProps,
  type ChatHistoryActions,
  type ChatHistoryPanel,
  type ChatProgressStep,
  type ChatAttachmentItem,
  type OptimisticChatMessage,
  displayDateTimeFormat,
} from "@production-ready/ui";

import { isTransportError } from "@/lib/api";
import { t } from "@/lib/i18n";
import { paginationLabels } from "@/lib/pagination-labels";

import { CHAT_PROGRESS_LABELS, chatSubmitProgressSteps } from "../chatProgress";
import type { ChatHistoryOffsetPage } from "../types";

/** 送信の応答が届かなかったときは、ジョブが作られている可能性と確かめ方を出す。 */
export function sendFailureText(error: unknown) {
  if (!isTransportError(error) || error.kind !== "timeout" || !error.timeoutMs) return {};
  return {
    summary: t("chat.sendTimeout", { seconds: Math.ceil(error.timeoutMs / 1000) }),
    nextAction: t("chat.sendTimeout.action"),
  };
}

/** 会話の履歴のパネル（`Nl2SqlChatHistoryList` を `content` に置く）。 */
export interface Nl2SqlChatHistory {
  panel: ChatHistoryPanel;
  content: ReactNode;
  toggleDisabled?: boolean;
  /** 一覧を絞った期間の日数（一覧の応答の `history_days`。見出しの横に「最近 N 日」を出す。#2340）。 */
  historyDays?: number;
}

// システムの設定の時間帯で書く（#3265）。
const historyDateFormatter = displayDateTimeFormat({
  month: "numeric",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

/** 会話の履歴の 1 行の元（チャットと DB 管理チャットの会話の共通の項目）。 */
export interface Nl2SqlConversationItem {
  id: string;
  title: string;
  created_at: string;
  /** 見ている利用者の会話か（system_admin が他の利用者の会話を見るときは false。#1889）。 */
  owned?: boolean;
  /** system_admin の一覧だけに付く利用者の表示名（#1889・#1696）。 */
  actor_display_name?: string;
}

/**
 * 会話の履歴の一覧（3 製品共通の `ChatHistoryList` に NL2SQL の文言・日時・持ち主・offset 型のページングを
 * 付ける）。「チャット」と「DB 管理チャット」（#2319）で同じ形にする（2 つの画面で書き写さない）。
 */
export function Nl2SqlChatHistoryList<T extends Nl2SqlConversationItem>({
  page,
  loading,
  error,
  onRetry,
  retrying,
  currentId,
  onSelect,
  disabled,
  seesAllUsers,
  offset,
  onOffsetChange,
  actions,
  testIdPrefix,
}: {
  /** 表示中のページ（新しい順）。 */
  page: ChatHistoryOffsetPage<T> | undefined;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  retrying: boolean;
  currentId: string | null;
  onSelect: (item: T) => void;
  disabled?: boolean;
  /** system_admin（全員の会話を見る）。他の利用者の会話に持ち主の表示名を添える。 */
  seesAllUsers: boolean;
  offset: number;
  onOffsetChange: (offset: number) => void;
  /** 行の「名前の変更」「削除」（共通の `useChatHistoryActions`）。 */
  actions: Pick<ChatHistoryActions, "renderActions" | "renderEditor">;
  testIdPrefix: string;
}) {
  const items = page?.items ?? [];
  return (
    <ChatHistoryList
      items={items.map((item) => ({
        id: item.id,
        title: item.title,
        meta:
          seesAllUsers && item.owned === false
            ? // system_admin が見る他の利用者の会話には、日時に利用者の表示名を添える（#1889）。
              t("chat.historyOwner", {
                date: historyDateFormatter.format(new Date(item.created_at)),
                name: item.actor_display_name || t("chat.historyOwnerMissing"),
              })
            : historyDateFormatter.format(new Date(item.created_at)),
      }))}
      currentId={currentId}
      onSelect={(row) => {
        const selected = items.find((candidate) => candidate.id === row.id);
        if (selected) onSelect(selected);
      }}
      disabled={disabled}
      loading={loading}
      error={error}
      onRetry={onRetry}
      retrying={retrying}
      labels={{
        list: t("chat.history"),
        loading: t("chat.historyLoading"),
        error: t("chat.historyLoadFailed"),
        retry: t("chat.retry"),
        // 期間で絞った一覧の 0 件は期間を含めた文（3 製品共通。#2340）。
        empty: chatHistoryWindowLabels(page?.history_days)?.empty ?? t("chat.historyEmpty"),
      }}
      renderActions={actions.renderActions}
      renderEditor={actions.renderEditor}
      operationKey={`${testIdPrefix}-history-load`}
      testIds={{
        loading: `${testIdPrefix}-history-loading`,
        error: `${testIdPrefix}-history-error`,
        list: `${testIdPrefix}-history-list`,
        pagination: `${testIdPrefix}-history-pagination`,
      }}
      // 3 製品共通の会話の履歴のページング（offset 型。ページ番号を入れて移れる。#1265 / #1266 / #1891）。
      // 会話が減って今のページが空になったら、共通の OffsetPagination が最後のページへ戻す。
      pagination={
        page
          ? {
              type: "offset",
              offset,
              limit: DEFAULT_PAGE_SIZE,
              total: page.total,
              count: items.length,
              onPageChange: (_page, nextOffset) => onOffsetChange(nextOffset),
              labels: paginationLabels(),
              ariaLabel: t("chat.historyPagination.label"),
            }
          : undefined
      }
    />
  );
}

/**
 * 一覧のページの中の、他の利用者の会話（system_admin が見る）の id。名前の変更は持ち主だけなので、
 * 共通の `useChatHistoryActions` の `canRename` でこの行の「名前の変更」を出さない（削除は出す。#3244）。
 */
export function othersConversationIds(page: { items: Nl2SqlConversationItem[] } | undefined): Set<string> {
  return new Set((page?.items ?? []).filter((item) => item.owned === false).map((item) => item.id));
}

/** 会話の履歴の位置（offset）を作業状態の値から読む（不正な値は先頭）。 */
export function normalizeHistoryOffset(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : 0;
}

/**
 * NL2SQL のチャットの画面の外枠（見出しと本文）。本文を画面の高さに収め、会話の欄（`Nl2SqlChatLayout`）の
 * 中だけをスクロールさせる（入力欄は会話のカードの下端、履歴のパネルは左に固定）。「チャット」と「DB 管理チャット」
 * はこの部品で描き、高さの渡し方を画面ごとに書き写さない（#2332）。画面の route は `FILL_HEIGHT_ROUTES` にも置く。
 */
export function Nl2SqlChatPage({
  title,
  subtitle,
  testId,
  children,
}: {
  title: string;
  subtitle: string;
  testId?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <PageHeader wide title={title} subtitle={subtitle} />
      <PageBody wide className="flex min-h-0 flex-1 flex-col gap-4" data-testid={testId}>
        {children}
      </PageBody>
    </div>
  );
}

/**
 * NL2SQL のチャットの骨格（共通の `ChatLayout` に NL2SQL の文言を付ける）。履歴のパネルと開閉ボタンは
 * `history` で渡す。
 */
export function Nl2SqlChatLayout({
  history,
  label,
  conversationTitle,
  targetSelector,
  conversationTitleLoading,
  onNewConversation,
  newConversationDisabled,
  autoScroll,
  testIdPrefix,
  composer,
  children,
}: {
  history: Nl2SqlChatHistory;
  /** 会話の領域の名前（翻訳済み）。 */
  label: string;
  conversationTitle?: string | null;
  targetSelector?: ReactNode;
  conversationTitleLoading?: boolean;
  onNewConversation: () => void;
  newConversationDisabled?: boolean;
  autoScroll: ChatAutoScroll;
  testIdPrefix: string;
  composer: ReactNode;
  children: ReactNode;
}) {
  return (
    <ChatLayout
      history={history.panel}
      historyTitle={t("chat.history")}
      historyCloseLabel={t("chat.closeHistory")}
      historyCaption={chatHistoryWindowLabels(history.historyDays)?.caption}
      historyContent={history.content}
      historyToggleDisabled={history.toggleDisabled}
      label={label}
      conversationTitle={conversationTitle}
      targetSelector={targetSelector}
      conversationTitleLoading={conversationTitleLoading}
      newConversation={{
        label: t("chat.new"),
        onClick: onNewConversation,
        disabled: newConversationDisabled,
      }}
      logLabel={t("chat.messages")}
      logRef={autoScroll.logRef}
      latest={{
        visible: autoScroll.showLatest,
        label: t("chat.latest"),
        onClick: () => autoScroll.scrollToLatest("smooth"),
      }}
      testIdPrefix={testIdPrefix}
      composer={composer}
    >
      {children}
    </ChatLayout>
  );
}

/**
 * NL2SQL のチャットの入力欄（共通の `ChatComposer`。Enter で送信・Shift+Enter で改行・送信中は停止）。
 * 名前・placeholder・送信 / 停止の文言と、入力の上限は 2 つの画面で同じ。
 */
export function Nl2SqlChatComposer(
  props: Omit<ChatComposerProps, "label" | "placeholder" | "sendLabel" | "stopLabel" | "maxLength">,
) {
  return (
    <ChatComposer
      {...props}
      label={t("chat.query")}
      placeholder={t("chat.placeholder")}
      sendLabel={t("chat.send")}
      stopLabel={t("chat.stop")}
      maxLength={10000}
    />
  );
}

/**
 * 送った質問（応答の前・送れなかったとき）。共通の `ChatPendingTurn` に、処理の段階（既定は「質問を
 * 送信しています」）と、送れなかったときの原因と「再送信」を付ける。
 */
export function Nl2SqlPendingTurn({
  message,
  error,
  fallback,
  onResend,
  resendDisabled,
  steps,
  testIdPrefix,
  attachments,
}: {
  message: OptimisticChatMessage;
  /** 質問に添付したファイル（#2517）。 */
  attachments?: ChatAttachmentItem[];
  /** 送れなかったときの原因（送信中・送れたときは null）。 */
  error: unknown;
  /** 原因の文が無いときの文（翻訳済み）。 */
  fallback: string;
  onResend: () => void;
  resendDisabled?: boolean;
  /** 応答を待つ間の段階（省略時は「質問を送信しています」）。 */
  steps?: ChatProgressStep[];
  testIdPrefix: string;
}) {
  return (
    <ChatPendingTurn
      message={message}
      attachments={attachments}
      attachmentsLabel={t("chat.attachments.list")}
      failedLabel={t("chat.sendFailedStatus")}
      testId={`${testIdPrefix}-pending-turn`}
      progress={
        <ChatProgress
          key={message.localId}
          steps={steps ?? chatSubmitProgressSteps(message.sentAtMs)}
          labels={CHAT_PROGRESS_LABELS}
          testId={`${testIdPrefix}-progress`}
        />
      }
      failure={
        error ? (
          // 失敗の理由は送った質問の直下（会話の中）に出す（messaging.md §10.1 のチャットの扱い）。
          <ApiErrorBanner
            error={error}
            fallback={fallback}
            testId={`${testIdPrefix}-send-error`}
            {...sendFailureText(error)}
            action={
              <Button
                type="button"
                variant="secondary"
                size="sm"
                icon={RotateCcw}
                disabled={resendDisabled}
                onClick={onResend}
              >
                {t("chat.resend")}
              </Button>
            }
          />
        ) : null
      }
    />
  );
}

/**
 * 回答の中の SQL は 3 製品で共通の `ChatSqlBlock`（`@production-ready/ui`。#2346）。文言は NL2SQL の辞書から渡す。
 */
export const SQL_BLOCK_LABELS = sqlBlockLabels({
  copy: t("chat.copySql"),
  copied: t("chat.sqlCopied"),
  copyFailed: t("chat.copyFailed"),
  safe: t("chat.safe"),
  safeExecuted: t("chat.safeExecuted"),
  unchecked: t("chat.safetySkipped"),
  uncheckedExecuted: t("chat.safetySkippedExecuted"),
  blocked: t("chat.blocked"),
});
