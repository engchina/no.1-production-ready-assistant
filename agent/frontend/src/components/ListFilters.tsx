import { ClearActionButton, EmptyState, SearchField, type ListCountProps } from "@production-ready/ui";

import { listCountLabels } from "@/components/ListViews";
import { t } from "@/lib/i18n";
import { isString, useWorkspaceState, type WorkspaceField } from "@/lib/workspace-state";

/**
 * 一覧の絞り込みの検索（UX 契約 page-archetypes.md「一覧の絞り込みの検索（#535）」「一覧のツールバー（#600）」。#808）。
 * 全件を持つ一覧を画面側で絞る。検索語は作業状態（このタブの sessionStorage）に残し、変わったら 1 ページ目へ戻す
 * （`PagedDataTable` の `resetKey` に渡す）。debounce・IME・Enter は `SearchField` の 1 か所だけが持つ。
 */
export type ListSearchKey = WorkspaceField<"listSearch">;

export function useListSearch(key: ListSearchKey): [string, (next: string) => void] {
  const [query, setQuery] = useWorkspaceState("listSearch", key, "", isString);
  return [query, setQuery];
}

function normalize(value: string): string {
  return value.normalize("NFKC").toLowerCase();
}

/** 検索語（空白区切りの語をすべて含む）が、いずれかの欄に一致するか。大文字・小文字と全角・半角は区別しない。 */
export function matchesSearch(query: string, fields: ReadonlyArray<string | null | undefined>): boolean {
  const terms = normalize(query).split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const haystack = normalize(fields.filter((field): field is string => Boolean(field)).join("\n"));
  return terms.every((term) => haystack.includes(term));
}

/**
 * 一覧のツールバーの検索欄。ラベルは見せない（placeholder と同じ文言を読み上げる）。
 * 件数はツールバーの `ListCount`（`listCount`）が読み上げるので、ここでは読ませない（二重にしない。#1891）。
 */
export function ListSearchField({
  id,
  label,
  value,
  onSearch,
}: {
  id: string;
  /** 「名前・ID で絞り込み」など、何で絞るかを書いた文言（i18n 済み。検索欄の幅 lg に収まる長さ）。 */
  label: string;
  value: string;
  onSearch: (next: string) => void;
}) {
  return (
    <SearchField
      id={id}
      label={label}
      labelHidden
      placeholder={label}
      value={value}
      onSearch={onSearch}
      clearLabel={t("common.clearSearch")}
    />
  );
}

/**
 * ツールバーの右の件数（共通の `ListCount` の props。#1891）。絞り込んでいないとき「全 N 件」、
 * 検索語・絞り込みがあるとき「N 件（全 M 件）」（N は一致した全件、M は絞り込む前の全件）。
 */
export function listCount(total: number, matched: number, filtered: boolean): ListCountProps {
  return { total, matched: filtered ? matched : undefined, labels: listCountLabels() };
}

/** 検索・絞り込みの無い一覧の件数（「全 N 件」。共通の `ListCount` の props。#1891）。 */
export function listTotal(total: number): ListCountProps {
  return listCount(total, total, false);
}

/** 検索・絞り込みに一致する行が無いとき（データがそもそも無いときの空の状態とは文言を分ける）。 */
export function NoMatchState({
  title,
  onClear,
  clearLabel = t("common.clearSearch"),
}: {
  title: string;
  onClear: () => void;
  clearLabel?: string;
}) {
  return (
    <EmptyState
      title={title}
      hint={t("list.noMatchHint")}
      action={<ClearActionButton label={clearLabel} onClick={onClear} />}
    />
  );
}
