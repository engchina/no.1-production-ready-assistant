import type { ReactNode } from "react";

import { ProcessingIndicator } from "../feedback/processing-state";
import { ListSkeleton } from "./skeleton";

/**
 * 検索つきの選択欄の、選択肢が無いときの一覧の本文。取得中は、ほかの読み込み中と同じく一覧の先頭に一覧と同じ幅の
 * 行（スピナー・文言・経過時間）を左寄せで出し、その下を選択肢の形の Skeleton で覆う（中央寄せの文字だけにしない。
 * #2510）。取得中でなければ、該当なし・空の文言を出す。
 */
export function SelectListEmpty({
  searching,
  searchingLabel,
  children,
}: {
  searching: boolean;
  searchingLabel: string;
  children: ReactNode;
}) {
  if (searching) {
    return (
      <div className="grid gap-2 px-3 py-2" data-testid="select-list-loading">
        {/* 件数の欄が同じ文言を出すので、ここでは読み上げない。遅延の案内の行は一覧を高くするため出さない。 */}
        <ProcessingIndicator active label={searchingLabel} showSlowMessage={false} announceActivity={false} />
        <ListSkeleton rows={3} rowClassName="h-7" />
      </div>
    );
  }
  return <p className="px-3 py-6 text-center text-xs text-fg-muted">{children}</p>;
}
