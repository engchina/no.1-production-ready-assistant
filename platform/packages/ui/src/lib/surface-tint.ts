/**
 * 淡い色の面の系統（tokens/colors.css の `[data-surface-tint]`。platform #64 / #1671）。
 *
 * 淡い面（`bg-accent-subtle` / `bg-accent-muted` / `bg-<状態色>-subtle` / 灰色の `bg-surface-hover`）を塗った要素に
 * `data-surface-tint` を付けると、
 * 内側の `--color-fg-muted`・`--color-accent-fg`・他の系統の success / warning の文字がライトで 4.5:1 を満たす値になる。
 */
export type SurfaceTint = "accent" | "info" | "success" | "warning" | "danger" | "neutral";

/**
 * 淡い面の class。adherence の lint（docs/design-system/adherence.oxlintrc.json）と同じ判定で、
 * 変種なし・`has-[…]:` / `aria-*:` / `data-*:` / 画面幅の変種だけを数える（`hover:` / `focus:` だけの面は数えない）。
 */
const SURFACE_TINT_CLASS =
  /(?:^|\s)(?:(?:sm|md|lg|xl|2xl|max-(?:sm|md|lg|xl|2xl)|has-\[[^\]\s]*\]|(?:group-|peer-)?aria-[\w-]+|(?:group-|peer-)?data-\[[^\]\s]*\]|(?:group-|peer-)?data-[\w-]+):)*!?bg-(?:(accent)-(?:subtle|muted)|(info|success|warning|danger)-subtle|surface-hover)(?![\w-])/;

/**
 * class の文字列から淡い面の系統を返す（淡い面でなければ `undefined`）。
 * 状態ごとの class を対応表・関数で組み立てる箇所で、`data-surface-tint={surfaceTintFor(className)}` と使う
 * （JSX の className に直接書く箇所は lint が `data-surface-tint` を求める）。
 */
export function surfaceTintFor(className: string | null | undefined | false): SurfaceTint | undefined {
  if (!className) return undefined;
  const match = SURFACE_TINT_CLASS.exec(className);
  if (!match) return undefined;
  return (match[1] ?? match[2] ?? "neutral") as SurfaceTint;
}
