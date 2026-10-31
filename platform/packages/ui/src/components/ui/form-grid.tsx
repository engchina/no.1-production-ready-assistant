import type { ComponentProps } from "react";

import { cn } from "../../lib/utils";

/**
 * 2 列にする置き場所の最小の幅（rem）= `lg` の段 28rem × 2 + 列の間 1.5rem（`gap-x-6`）。
 * これより狭いと 1 列にし、どのセルにも `lg` の欄が段の幅のまま入る（#1891 段 5）。
 * クラス `@[57.5rem]:grid-cols-2` と同じ値（Tailwind が dist から拾えるよう、クラスは文字列のまま書く）。
 */
export const FORM_GRID_TWO_COLUMN_MIN_REM = 57.5;

export type FormGridProps = ComponentProps<"div"> & {
  /**
   * 段組み（既定 2）。2 は置き場所の幅が 57.5rem（`lg` の欄 2 つと列の間）以上で 2 列、それより狭いと 1 列。
   * 1 は常に 1 列（項目の少ないフォーム・長い値が主のフォーム）。
   * 画面固有の列（`className` の `grid-cols-*`。例: NL2SQL の CSV の取り込みの「ファイル | モード」）を渡したときは、
   * 既定の 2 列のクラスを付けず、渡した列に任せる（container query の境が違うと、広いほうの既定が後から効いて上書きする）。
   */
  columns?: 1 | 2;
};

/** `className` が列（`grid-cols-*`。variant 付きを含む）を持つか。 */
function hasCustomColumns(className: string | undefined) {
  return /(^|[\s:])grid-cols-/.test(className ?? "");
}

/**
 * フォームの本文（入力欄の並び）の型（#1891）。最大幅は読みやすい `--form-max-width`（60rem）で、左に寄せる。
 *
 * - カード・`Section` は 100% の幅のまま（README §4「wide 画面の 100% 充填」）。止めるのはフォームの欄の並びだけで、
 *   1920px の画面でも 1 つの欄が 1,500px を超えて伸びない（GOV.UK の 2/3 の列、Baymard の欄の幅の研究）。
 * - 欄は値の種類の `width`（`xs`〜`lg`）を渡す。2 列は各セルが `lg`（28rem）以上になる置き場所（57.5rem 以上）だけで、
 *   それより狭いと 1 列になるので、欄はどのセルでも段の幅のまま（セルで切られない。#1891 段 5）。URL・OCID・文章・
 *   SQL・`TextareaField` は `full` のまま（このグリッドの幅まで）にし、セルを 2 列にまたがせる（`className="col-span-full"`）。
 * - 意味のペア（ユーザー / パスワード・リージョン / テナンシ）は同じ行に並べ、入力の順は崩さない。
 * - 確定の行（`FormActionBar`）はこのグリッドの外（直後）に置く。左端は欄の左端にそろう。
 */
export function FormGrid({ columns = 2, className, children, ...props }: FormGridProps) {
  return (
    <div className="@container w-full min-w-0 max-w-[var(--form-max-width)]" data-form-grid={columns}>
      {/* 1 列のときも grid-cols-1（minmax(0, 1fr)）を明示する。暗黙の列（auto）は中身の最小幅（長い 1 語・ファイルの
          ドロップ領域など）まで広がり、375px で横にはみ出す（#1891 段 4）。
          2 列の境は 57.5rem（FORM_GRID_TWO_COLUMN_MIN_REM）。以前の 42rem（@2xl）では、42〜57.5rem の間のセル
          （約 20〜28rem）に lg の欄が入らず、セルの幅に切られた（#1891 段 5）。 */}
      <div
        {...props}
        className={cn(
          "grid min-w-0 grid-cols-1 gap-x-6 gap-y-4",
          columns === 2 && !hasCustomColumns(className) && "@[57.5rem]:grid-cols-2",
          className
        )}
      >
        {children}
      </div>
    </div>
  );
}
