import type { ReactNode } from "react";
import { useCallback, useState } from "react";

import { RefreshCw } from "lucide-react";

import type { FeedbackTone } from "@production-ready/ui";

import { Banner, Button, ProcessingIndicator } from "@production-ready/ui";

import { t } from "@/lib/i18n";

/**
 * ページ/セクション常設通知（Messaging Spec Channel 4 Banner）の正準状態。
 * tone は 4 トーン固定で "error" ではなく "danger" を使う。
 */
export type Notice = { tone: FeedbackTone; message: string } | null;

/**
 * 各ページで発散していた `{message, messageTone}` state を一元化する hook。
 * 空文字は null に正規化する（spec §9 P4: 空メッセージ面を作らない）。
 */
export function usePageNotice(initial: Notice = null) {
  const [notice, setNotice] = useState<Notice>(initial);
  const showNotice = useCallback((tone: FeedbackTone, message: string) => {
    setNotice(message ? { tone, message } : null);
  }, []);
  const clearNotice = useCallback(() => setNotice(null), []);
  return { notice, showNotice, clearNotice } as const;
}

/**
 * PageNotice（Channel 4 Banner）。PageHeader 直後・<main> 先頭の正準位置に置く。
 * notice が null / 空なら何も描画しない（P4 ガード。raw Banner は空でもアイコン箱を出す）。
 * action は JSX なので Notice state ではなく slot props で透過する。
 * progress は action で始めた処理の進行（`ProcessingIndicator` など）。同じ Banner の中の action の直下に出す
 * （「再試行」の進行をページの別の場所に出さない。messaging §10.2、#2034）。
 */
export function PageNotice({
  notice,
  onDismiss,
  action,
  progress,
  className,
}: {
  notice: Notice;
  onDismiss?: () => void;
  action?: ReactNode;
  progress?: ReactNode;
  className?: string;
}) {
  if (!notice || !notice.message) return null;
  return (
    <Banner
      severity={notice.tone}
      action={action}
      progress={progress}
      onDismiss={onDismiss}
      className={className}
    >
      {notice.message}
    </Banner>
  );
}

/**
 * 取得の失敗の案内（PageNotice）と「再試行」。押すと案内を残したまま、押したボタンだけを `loading` にし、
 * 経過時間を同じ案内の中のボタンの直下に出す。取り直せたら呼び出し側が notice を消す（案内が押した瞬間に
 * 消えて、進行がほかの場所に出るのを防ぐ。messaging §10.2、#2034）。
 */
export function RetryPageNotice({
  notice,
  retrying,
  onRetry,
  disabled = false,
  progressLabel = t("common.processing.refreshing"),
  operationKey,
  testId,
}: {
  notice: Notice;
  /** 案内の「再試行」で始めた取り直しの間 true（`useActionPending` の pending）。 */
  retrying: boolean;
  onRetry: () => void;
  /** ほかの操作の間に押せなくする。 */
  disabled?: boolean;
  progressLabel?: string;
  operationKey: string;
  /** 「再試行」のボタンの data-testid（進行は `<testId>-processing`）。 */
  testId?: string;
}) {
  return (
    <PageNotice
      notice={notice}
      action={
        <Button
          type="button"
          variant="secondary"
          size="sm"
          loading={retrying}
          disabled={disabled && !retrying}
          onClick={() => {
            if (!retrying) onRetry();
          }}
          icon={RefreshCw}
          data-testid={testId}
        >
          <span>{t("common.retry")}</span>
        </Button>
      }
      progress={
        retrying ? (
          <ProcessingIndicator
            active
            label={progressLabel}
            operationKey={operationKey}
            placement="action"
            testId={testId ? `${testId}-processing` : undefined}
            activityIcon="none"
          />
        ) : null
      }
    />
  );
}
