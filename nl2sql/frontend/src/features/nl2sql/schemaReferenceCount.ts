import type { ListCountProps } from "@production-ready/ui";

import { listCountLabels } from "../../lib/pagination-labels";

/**
 * スキーマ参照の表の件数（`ListToolbar` の `count`。#1891）。件数が確かなときだけ返す。
 *
 * - 読み込んだ範囲の外に続きが無い（!hasMore）とき: 表示している表の数がそのまま件数。
 * - 続きがあるとき: API の total（`totalCount`）を使う。業務プロファイルの許可する表で画面の側だけが絞るとき
 *   （scoped で、API が同じ業務プロファイルで絞っていない = !serverScoped）は API の total と表示が合わないので出さない
 *   （「さらに読み込む」のフッターが続きを示す）。SQL 生成の画面は API に `profile_id` を渡すので serverScoped。
 * - 検索中は「N 件（全 M 件）」。M（絞り込む前の全件）が分からないときは出さない。
 */
export function schemaReferenceListCount({
  loading,
  searching,
  hasMore,
  scoped,
  serverScoped = false,
  shown,
  scopedTotal,
  serverSearch,
  totalCount,
  unfilteredTotalCount,
}: {
  loading: boolean;
  searching: boolean;
  hasMore: boolean;
  scoped: boolean;
  /** API が同じ業務プロファイルの許可する表で絞っている（total がそのまま表示の件数になる）。 */
  serverScoped?: boolean;
  shown: number;
  scopedTotal: number;
  serverSearch: boolean;
  totalCount?: number | null;
  unfilteredTotalCount?: number | null;
}): ListCountProps | undefined {
  if (loading) return undefined;
  const labels = listCountLabels();
  const totalUsable = !scoped || serverScoped;
  const matched = hasMore ? (totalUsable ? (totalCount ?? null) : null) : shown;
  if (matched === null) return undefined;
  if (!searching) return { total: Math.max(matched, shown), labels };
  // 画面側で絞るとき（検索語を API に渡さない）は、読み込んだ範囲の表が絞り込む前の全件。
  const total = !serverSearch && !hasMore ? scopedTotal : unfilteredTotalCount;
  if (total == null) return undefined;
  return { total: Math.max(total, matched), matched, labels };
}
