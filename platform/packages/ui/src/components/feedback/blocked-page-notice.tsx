import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "../../lib/utils";
import { toneText, type FeedbackTone } from "../ui/feedback-tone";

const TONE_CHIP: Record<FeedbackTone, string> = {
  success: "bg-success-subtle",
  info: "bg-info-subtle",
  warning: "bg-warning-subtle",
  danger: "bg-danger-subtle",
};

export interface BlockedPageNoticeProps {
  /** 見出し（`h1`）。 */
  title: string;
  /** 見出しの id。カードの `aria-labelledby` に使う（E2E が参照するため製品ごとに固定する）。 */
  titleId: string;
  /** 見出しの上に出すアイコン（24px。意味は見出しで伝えるので `aria-hidden`）。 */
  icon: LucideIcon;
  /** アイコンの丸の色。色だけで意味を伝えないため、見出しと本文で状況を書く。 */
  tone?: FeedbackTone;
  /** 見出しの直下の本文（原因 + 次の行動）。 */
  message?: ReactNode;
  /** 本文の下の補足（診断コードなど）。 */
  details?: ReactNode;
  /** 復旧の操作（主操作を先頭に置く。キーボードの Tab 順も同じ）。 */
  actions?: ReactNode;
  /** 区切り線の下の補足（引き続き使える画面の案内など）。 */
  footer?: ReactNode;
  testId?: string;
  className?: string;
}

/**
 * ページ全体が使えない（ブロック状態）ときの案内カード（#325）。
 * DB の未設定・未起動・初期化待ちなど、業務画面の代わりに主領域の中央へ出し、
 * 原因と復旧の導線（設定を開く・再試行）を示す。エラー画面ではなく落ち着いた案内にする。
 * 文言はすべて翻訳済みの文字列で渡す（パッケージは i18n を持たない）。
 */
export function BlockedPageNotice({
  title,
  titleId,
  icon: Icon,
  tone = "warning",
  message,
  details,
  actions,
  footer,
  testId,
  className,
}: BlockedPageNoticeProps) {
  return (
    <div className={cn("grid min-h-dvh place-items-center p-4 sm:p-6", className)} data-testid={testId}>
      <section
        className="pr-message-text w-full max-w-lg rounded-xl border border-border bg-surface p-6 text-center shadow-sm sm:p-8"
        aria-labelledby={titleId}
      >
        <div
          data-surface-tint={tone}
          className={cn("mx-auto grid size-12 place-items-center rounded-full", TONE_CHIP[tone], toneText[tone])}
          aria-hidden
        >
          <Icon size={24} />
        </div>
        <h1 id={titleId} className="mt-5 text-lg font-semibold text-fg">
          {title}
        </h1>
        {message ? (
          <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-fg-muted">{message}</p>
        ) : null}
        {details ? <div className="mt-2 space-y-1 text-xs leading-relaxed text-fg-muted">{details}</div> : null}
        {actions ? (
          <div className="mt-6 flex flex-wrap items-center justify-center gap-2">{actions}</div>
        ) : null}
        {footer ? (
          <p className="mt-6 border-t border-border pt-4 text-xs leading-relaxed text-fg-muted">{footer}</p>
        ) : null}
      </section>
    </div>
  );
}
