import type { ReactNode } from "react";

import { cn } from "../../lib/utils";

export interface FieldActionRowProps {
  /** 入力欄（TextField / TextareaField / SearchField / SelectField など 1 つ）。行の残りの幅を埋める。 */
  children: ReactNode;
  /**
   * 入力欄の右に並べる操作（Button。入力欄と同じ size にする）。null / false なら操作の列を描かず、
   * 入力欄が行の幅いっぱいになる（状態によって操作が無い欄も同じ構造のまま書ける。#631）。
   */
  actions: ReactNode;
  /** 行の下に出す補足・エラー（欄の helper / error の代わり。欄の中に入れると操作の下端がずれるため）。 */
  footer?: ReactNode;
  /**
   * 入力欄に値の種類の幅（`width` の xs〜lg。#1891）を渡したとき true。欄はその幅のまま置き、操作を欄の直後に
   * 並べる（既定 false は、欄が行の残りの幅を埋める。チャットの入力欄など `full` の欄）。
   */
  fit?: boolean;
  className?: string;
  "data-testid"?: string;
}

/**
 * 入力欄と、その値に対する操作（送信・実行・取得・接続テスト）を 1 行に並べる（#613）。
 *
 * - 操作は入力欄の**下端にそろえる**（`items-end`）。上に見えるラベルがある欄でも、1 行の入力欄と
 *   複数行の入力欄（チャットの入力欄）でも、ボタンの下端が入力欄の下端に合う。
 * - 高さは呼び出し側が入力欄と Button に**同じ `size`** を渡してそろえる（既定はどちらも md）。
 * - sm（640px）未満は縦に積み、操作は全幅にする（製品で `w-full sm:w-auto` を書かない）。
 * - 欄の `helper` / `error` は使わず `footer` に渡す（欄の下に文があると、下端でそろえた操作が文の下端に合ってしまう）。
 */
export function FieldActionRow({ children, actions, footer, fit = false, className, ...props }: FieldActionRowProps) {
  return (
    <div className={cn("min-w-0 space-y-1.5", className)} data-testid={props["data-testid"]}>
      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-end" data-field-action-row="">
        <div className={fit ? "min-w-0 max-w-full" : "min-w-0 flex-1 [&>*]:w-full"}>{children}</div>
        {actions === null || actions === undefined || actions === false ? null : (
          <div className="flex shrink-0 flex-col gap-2 sm:flex-row sm:items-end [&>*]:w-full sm:[&>*]:w-auto">
            {actions}
          </div>
        )}
      </div>
      {footer}
    </div>
  );
}
