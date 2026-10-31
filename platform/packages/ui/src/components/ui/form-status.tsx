import { cn } from "../../lib/utils";

import { toneIcon, toneRole, toneText, type FeedbackTone } from "./feedback-tone";
import { MessageText } from "./message-text";

/**
 * FormStatus（フォーム/アクション結果）。
 * 保存・接続テスト等のボタン近傍に直近結果を 1 行で表示する。
 * `message` が空のときは何も描画しない。
 */
export function FormStatus({
  tone,
  message,
  className,
}: {
  tone: FeedbackTone;
  message?: string | null;
  className?: string;
}) {
  if (!message) return null;
  const Icon = toneIcon[tone];

  return (
    <p
      role={toneRole(tone)}
      className={cn(
        "pr-message-text inline-flex min-w-0 items-start gap-1.5 text-sm font-medium leading-relaxed",
        toneText[tone],
        className
      )}
    >
      <span className="pr-icon-line">
        <Icon size={16} aria-hidden />
      </span>
      <MessageText text={message} />
    </p>
  );
}
