import { ArrowRight, RefreshCw, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Banner } from "../ui/banner";
import { Button } from "../ui/button";
import { ButtonLink, type ButtonLinkComponent } from "../ui/button-link";

export interface PreconditionBannerLink {
  /** 前提を満たす画面（設定の画面など）。 */
  to: string;
  /** 翻訳済みのラベル（「〇〇を開く」。〇〇はサイドナビの表示名）。 */
  label: string;
  /** 移動先の画面のナビのアイコン（同じ機能は同じアイコン）。 */
  icon: LucideIcon;
  linkComponent?: ButtonLinkComponent;
  testId?: string;
}

export interface PreconditionBannerProps {
  /**
   * 案内の id。押せなくした操作の `aria-describedby` に渡し、押せない理由を操作に結ぶ。
   */
  id?: string;
  /** 何が足りないか（翻訳済み。例「Select AI Credential が作成されていません」）。 */
  title: string;
  /**
   * 何に要るか・いつまで操作できないか・次の行動（翻訳済み）。前提を満たす画面を開けない利用者には、
   * 「システム管理者に連絡して、…を依頼してください。」を書き、`settingsLink` を渡さない。
   */
  message: ReactNode;
  /** 前提を満たす画面へのリンク。その画面を開ける利用者のときだけ渡す（押しても開けない導線を出さない）。 */
  settingsLink?: PreconditionBannerLink;
  /** 前提を確かめ直す（設定を済ませた後）。省略すると出さない。 */
  onRetry?: () => void;
  /** 翻訳済みのラベル（既定「再試行」）。 */
  retryLabel?: string;
  /** 押した「再試行」の処理中だけ true にする（画面のフォーカスでの取り直しでは回さない）。 */
  retrying?: boolean;
  retryTestId?: string;
  testId?: string;
  className?: string;
}

/**
 * 操作の前提（設定・資源）が足りないので、その操作ができないことを示す常設の案内（#1688）。
 * UX 契約 messaging.md §3.4（「設定が終わっていないので機能が使えない」状況の Banner）と §3.4.1
 * （導線はその画面を開ける利用者だけ・導線 → 再試行の順・開けない利用者には管理者への連絡）を、
 * ページ全体ではなく 1 つの操作に当てはめたもの。ページ全体が使えないときは `BlockedPageNotice`。
 *
 * - warning の `Banner`（`role="status"`）。閉じる × は付けない（前提を満たすまで残す）。
 * - 操作の区画の先頭（押せなくした操作の直前）に全幅で置き、操作は `disabled` にして `id` を
 *   `aria-describedby` で結ぶ（README §4: `disabled` のボタンはフォーカスを受けないので理由を文言で示す）。
 * - 導線は `ButtonLink`（secondary・sm・移動先のナビのアイコン・`ArrowRight`）、続けて「再試行」
 *   （secondary・sm・`RefreshCw`）。DB の縮退の Banner（`DatabaseUnavailableNotice` の `mode="banner"`）と同じ形。
 */
export function PreconditionBanner({
  id,
  title,
  message,
  settingsLink,
  onRetry,
  retryLabel = "再試行",
  retrying = false,
  retryTestId,
  testId,
  className,
}: PreconditionBannerProps) {
  const actions =
    settingsLink || onRetry ? (
      <>
        {settingsLink ? (
          <ButtonLink
            to={settingsLink.to}
            linkComponent={settingsLink.linkComponent}
            variant="secondary"
            size="sm"
            icon={settingsLink.icon}
            trailingIcon={ArrowRight}
            testId={settingsLink.testId}
          >
            {settingsLink.label}
          </ButtonLink>
        ) : null}
        {onRetry ? (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            icon={RefreshCw}
            loading={retrying}
            onClick={onRetry}
            data-testid={retryTestId}
          >
            {retryLabel}
          </Button>
        ) : null}
      </>
    ) : undefined;
  return (
    <div id={id} data-testid={testId} className={className}>
      <Banner severity="warning" title={title} action={actions}>
        {message}
      </Banner>
    </div>
  );
}
