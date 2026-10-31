import { FileKey } from "lucide-react";
import { Link } from "react-router-dom";

import { PreconditionBanner } from "@production-ready/ui";

import { t } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";

/**
 * Select AI の Credential が無いときの事前条件の案内（#1683 / #1688）。共有の `PreconditionBanner`
 * （messaging.md §3.4 / §3.4.1）に、NL2SQL の文言と導線（運用設定の「Select AI Credential」。ナビと同じ
 * `FileKey`）を渡す。操作（保存・実行・送信・生成・評価）の区画の先頭に置き、操作は `disabled` にして
 * `id` を `aria-describedby` で結ぶ。Credential の画面を開けない利用者には導線を出さず、
 * システム管理者への依頼を案内する。
 */
export function SelectAiCredentialPrecondition({
  id,
  testId,
  operation,
  action,
  credentialName,
  canOpenSettings,
  rechecking,
  onRecheck,
}: {
  id: string;
  testId: string;
  /** 何の操作に要るか（翻訳済み。例「業務プロファイルの保存（DBMS_CLOUD_AI Profile への反映）」）。 */
  operation: string;
  /** 押せなくした操作の名前（翻訳済み。ボタンのラベルと同じ語）。 */
  action: string;
  credentialName: string;
  canOpenSettings: boolean;
  rechecking: boolean;
  onRecheck: () => void;
}) {
  return (
    <PreconditionBanner
      id={id}
      testId={testId}
      title={t("selectAiCredential.precondition.title")}
      message={t(
        canOpenSettings
          ? "selectAiCredential.precondition.message"
          : "selectAiCredential.precondition.contactAdmin",
        { operation, action, name: credentialName || "NL2SQL_OCI_CRED" },
      )}
      settingsLink={
        canOpenSettings
          ? {
              to: APP_ROUTES.settingsSelectAiCredential,
              label: t("selectAiCredential.precondition.open"),
              icon: FileKey,
              linkComponent: Link,
              testId: `${testId}-open-settings`,
            }
          : undefined
      }
      onRetry={onRecheck}
      retryLabel={t("common.retry")}
      retrying={rechecking}
      retryTestId={`${testId}-retry`}
    />
  );
}
