import { cn } from "../../lib/utils";

import { MessageText } from "./message-text";

/**
 * FieldError（フィールド検証エラー）。
 * 該当入力欄の直下に置き、入力側の `aria-describedby={id}` と対応させる。
 * `message` が空のときは何も描画しない。
 */
export function FieldError({
  id,
  message,
  className,
}: {
  id: string;
  message?: string | null;
  className?: string;
}) {
  if (!message) return null;
  return (
    <p id={id} role="alert" className={cn("text-xs leading-relaxed text-danger-fg", className)}>
      <MessageText text={message} />
    </p>
  );
}
