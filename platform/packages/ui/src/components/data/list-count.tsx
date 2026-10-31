import { cn } from "../../lib/utils";
import { formatPaginationNumber } from "./pagination";

/**
 * 一覧の件数の文言（#1891）。パッケージは i18n に依存しないため、既定は日本語（`DEFAULT_LIST_COUNT_LABELS`）で、
 * 製品は i18n の文字列で上書きできる。数値は `formatPaginationNumber`（桁区切り）で書く。
 */
export interface ListCountLabels {
  /** 絞り込んでいないとき（例「全 123 件」）。 */
  total: (total: string) => string;
  /** 絞り込んでいるとき（例「12 件（全 123 件）」）。 */
  filtered: (matched: string, total: string) => string;
}

export const DEFAULT_LIST_COUNT_LABELS: ListCountLabels = {
  total: (total) => `全 ${total} 件`,
  filtered: (matched, total) => `${matched} 件（全 ${total} 件）`,
};

export interface ListCountProps {
  /** 一覧の全件数（絞り込む前。サーバー側のページングなら API の total）。 */
  total: number;
  /**
   * 絞り込み（検索語・状態など）に一致した件数。絞り込んでいるときだけ渡す（`undefined` は絞り込みなし）。
   * 絞り込みの結果の全件（今のページの件数ではない）。
   */
  matched?: number;
  labels?: Partial<ListCountLabels>;
  /**
   * 件数の変化を読み上げる（`role="status"`。既定 true）。同じ件数を `SearchField` の `resultCountLabel` でも
   * 読み上げているときは false にする（二重に読ませない）。
   */
  live?: boolean;
  className?: string;
  testId?: string;
}

/** `ListCount` の文言を作る（テスト・読み上げの文言に使う）。 */
export function listCountText({
  total,
  matched,
  labels: labelOverrides,
}: Pick<ListCountProps, "total" | "matched" | "labels">) {
  const labels = { ...DEFAULT_LIST_COUNT_LABELS, ...labelOverrides };
  const totalText = formatPaginationNumber(Math.max(0, total));
  if (matched === undefined) return labels.total(totalText);
  return labels.filtered(formatPaginationNumber(Math.max(0, matched)), totalText);
}

/**
 * 一覧の件数（#1891）。`ListToolbar` の右（`count`）に 1 か所だけ出す。見出しのバッジと二重に出さない。
 *
 * - 絞り込んでいないとき「全 123 件」、絞り込んでいるとき「12 件（全 123 件）」。1 ページに収まっても出す。
 * - 等幅数字（`tnum`）。件数が変わったら `role="status"` で読み上げる（`live={false}` で止める）。
 * - 今のページの範囲（「1–10 / 123 件」）はページ送り（`Pagination`）の左が出す。
 */
export function ListCount({ total, matched, labels, live = true, className, testId }: ListCountProps) {
  return (
    <p
      role={live ? "status" : undefined}
      className={cn("tnum whitespace-nowrap text-xs text-fg-muted", className)}
      data-list-count=""
      data-testid={testId}
    >
      {listCountText({ total, matched, labels })}
    </p>
  );
}
