import { cn } from "../../lib/utils";

/**
 * 必須のタグの既定の文言。3 製品で同じ語にそろえるため `packages/ui` が持つ（#531）。
 * 条件付きの必須（例:「OCI 運用時必須」）など別の語を出すときだけ `label` / `requiredLabel` で上書きする。
 */
export const DEFAULT_REQUIRED_LABEL = "必須";

/**
 * 入力項目が必須であることを示すテキストのタグ（例:「必須」「OCI 運用時必須」）。
 *
 * - 必須は「状態」ではなく項目の「属性（情報）」なので、状態色（warning / danger）を使わず中立色で出す。
 *   状態色は未入力エラーなど利用者の対応が要るときの FieldError に取っておく。
 * - 記号（`*`）ではなくテキストにする。凡例（「* は必須」）が要らず、色にも頼らない（WCAG 1.4.1 / 3.3.2）。
 * - 地は塗らず、置かれた面の色を透過する。文字は --color-fg-muted（surface 上 4.83:1、sunken 上 4.55:1、ダーク 7:1 以上）。
 *
 * TextField / SelectField / SecretField は `required` で内部的にこれを出す。それらで表せない入力は
 * `FieldLabel`（label 要素）・`FieldLegend` / `Fieldset`（チェックボックスの群・ラジオ・複合入力）を使い、
 * 製品で RequiredBadge を直接ラベルに並べない（並べ方と読み上げの扱いが画面ごとにずれるため）。
 * 既定では読み上げ対象に含む。入力側に aria-required / required を付けて必須を伝えている場合は
 * `aria-hidden` を渡して二重読み上げを避ける。
 */
export function RequiredBadge({
  label = DEFAULT_REQUIRED_LABEL,
  className,
  "aria-hidden": ariaHidden,
}: {
  /** 翻訳済みの文言。既定「必須」。 */
  label?: string;
  className?: string;
  "aria-hidden"?: boolean;
}) {
  return (
    <span
      aria-hidden={ariaHidden || undefined}
      className={cn(
        "inline-flex shrink-0 items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium text-fg-muted",
        // 輪郭は装飾（意味は文字が持つ）。ring-inset なので行の高さを変えない
        "ring-1 ring-inset ring-border-strong",
        className
      )}
    >
      {label}
    </span>
  );
}
