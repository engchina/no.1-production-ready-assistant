import { DatabaseUnavailableNotice } from "@production-ready/system-settings";

import {
  DATABASE_GATE_ROUTES,
  databaseGateMessages,
  useDatabaseGatePermissions,
} from "@/components/system/DatabaseGate";
import { t } from "@/lib/i18n";

/**
 * DB 停止/応答不良で閲覧系 API が縮退応答(空データ + warning)したときに、
 * ページ上部へ常設する非ブロッキングなお知らせ。
 *
 * 全画面エラーにはせず、ページ本体(空状態)はそのまま表示しつつ、
 * 落ち着いた warning トーンで状況と復旧導線(再試行 / DB 設定)を示す。
 * 見た目と導線は3製品共通の DB の案内（banner）を使う（#325）。
 * platform/docs/ux-contracts/messaging.md の Banner(状況提示)チャネルに従う。
 */
export function DegradedBanner({
  messages,
  onRetry,
  isRetrying = false,
  className,
}: {
  /** バックエンドが返した warning_messages。空なら何も表示しない。 */
  messages: readonly string[] | undefined;
  onRetry?: () => void;
  isRetrying?: boolean;
  className?: string;
}) {
  // データベース設定へのリンクは、その画面を開ける利用者だけに出す（#820）。
  const { canManageDatabase } = useDatabaseGatePermissions();
  if (!messages || messages.length === 0) return null;

  return (
    <DatabaseUnavailableNotice
      canManageDatabase={canManageDatabase}
      mode="banner"
      routes={DATABASE_GATE_ROUTES}
      messages={databaseGateMessages()}
      title={t("common.degraded.title")}
      onRetry={onRetry}
      isRetrying={isRetrying}
      settingsLink
      className={className}
      message={
        messages.length === 1 ? (
          <p>{messages[0]}</p>
        ) : (
          <ul className="list-disc space-y-0.5 pl-4">
            {messages.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        )
      }
    />
  );
}
