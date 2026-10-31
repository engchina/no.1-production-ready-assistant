"use client";

import {
  compareNames,
  type FieldWidth,
  SearchableMultiSelect,
  type SearchableMultiSelectLabels,
  type SearchableSelectLabels,
  type SearchableSelectOption,
} from "@production-ready/ui";
import { useMemo } from "react";

import { DEFAULT_KNOWLEDGE_BASE_NAME, type KnowledgeBaseSummary } from "@/lib/api";
import { t } from "@/lib/i18n";
import type { ResolvedKnowledgeBaseRef } from "@/lib/knowledge-base-refs";
import type { useKnowledgeBaseChoices } from "@/lib/queries";

export type KnowledgeBaseChoices = ReturnType<typeof useKnowledgeBaseChoices>;

/** 検索できる選択部品の文言（単一・複数で共通の部分）。 */
export function knowledgeBaseSelectLabels(): SearchableSelectLabels {
  return {
    searchPlaceholder: t("knowledgeBasePicker.addPlaceholder"),
    searchLabel: () => t("knowledgeBasePicker.searchLabel"),
    clearSearch: t("common.clearSearch"),
    count: (shown, total) => t("knowledgeBasePicker.count", { shown, total }),
    noMatch: (query) => t("knowledgeBasePicker.noMatch", { query }),
    empty: t("knowledgeBasePicker.emptyList"),
    searching: t("knowledgeBasePicker.searching"),
    loadMore: t("knowledgeBasePicker.loadMore"),
  };
}

function knowledgeBaseMultiSelectLabels(): SearchableMultiSelectLabels {
  return {
    ...knowledgeBaseSelectLabels(),
    toggleList: t("knowledgeBasePicker.toggleListAria"),
    selectedCount: (count) => t("knowledgeBasePicker.selectedCount", { count }),
    selectedList: () => t("knowledgeBasePicker.selectedList"),
    removeChip: (name) => t("knowledgeBasePicker.removeChip", { name }),
    selectAllVisible: t("knowledgeBasePicker.selectAllVisible"),
    clear: t("knowledgeBasePicker.clear"),
    done: t("knowledgeBasePicker.done"),
  };
}

/**
 * KB の選択肢（名前・説明・文書数）。全件を手元に持つとき（`local`）だけ、既定の KB を先頭に名前の順（共通の名前の順。#2753）へ並べ、
 * 文書の最も多い KB に「最多」を付ける（ページの途中では決められない）。
 * 文書のない KB も隠さない（作ったばかりの KB へ登録・所属させるため。件数は右端の「0 文書」で分かる。#578）。
 */
export function knowledgeBaseOptions(
  items: readonly KnowledgeBaseSummary[],
  { local }: { local: boolean }
): SearchableSelectOption[] {
  const sorted = local ? sortKnowledgeBases(items) : [...items];
  const top = local
    ? sorted.reduce<KnowledgeBaseSummary | null>(
        (best, item) => (!best || item.document_count > best.document_count ? item : best),
        null
      )
    : null;
  const topId = items.length > 1 && top && top.document_count > 0 ? top.id : null;
  return sorted.map((kb) => ({
    value: kb.id,
    label: kb.name,
    description: kb.description ?? undefined,
    meta: t("knowledgeBasePicker.documentCount", { count: kb.document_count }),
    badge: kb.id === topId ? t("knowledgeBasePicker.mostDocs") : undefined,
  }));
}

/** 選択済みの KB の chip。検索対象にならない KB は状態を文字で添える（色だけに頼らない）。 */
export function knowledgeBaseChipOptions(
  items: readonly ResolvedKnowledgeBaseRef[]
): SearchableSelectOption[] {
  return items.map((kb) => ({
    value: kb.id,
    label: kb.name,
    badge: kb.missing
      ? t("knowledgeBasePicker.missingBadge")
      : kb.status === "ARCHIVED"
        ? t("knowledgeBasePicker.archivedBadge")
        : undefined,
  }));
}

/**
 * ナレッジベースの複数選択（検索 ＋ 候補の一覧 ＋ 選択済みの chip。#578）。
 * アップロードの登録先・検索範囲・評価・検索・回答プロファイル・文書詳細の所属 KB で同じ操作にそろえる。
 * 候補は `useKnowledgeBaseChoices`（200 件以下は画面側で絞り込み、超えるとサーバー側の検索）から渡す。
 */
export function KnowledgeBaseMultiSelect({
  id,
  label,
  labelHidden = false,
  labelledBy,
  helper,
  required = false,
  invalid = false,
  describedBy,
  choices,
  onQueryChange,
  selectedIds,
  onChange,
  selectedItems,
  disabled = false,
  width,
}: {
  id: string;
  label: string;
  labelHidden?: boolean;
  /** 外に出している見出し（FieldLabel）の id。 */
  labelledBy?: string;
  helper?: string;
  required?: boolean;
  invalid?: boolean;
  describedBy?: string;
  choices: KnowledgeBaseChoices;
  onQueryChange: (query: string) => void;
  selectedIds: string[];
  onChange: (ids: string[]) => void;
  /** 選択済みの KB の名前・状態（候補のページに無い選択済みも chip に出すため）。 */
  selectedItems?: readonly ResolvedKnowledgeBaseRef[];
  disabled?: boolean;
  /** 欄の幅（呼び出し側が置き場所で選ぶ。対象の選択なので通常は lg。#1891）。 */
  width?: FieldWidth;
}) {
  const options = useMemo(
    () => knowledgeBaseOptions(choices.items, { local: !choices.remote }),
    [choices.items, choices.remote]
  );
  const selectedOptions = useMemo(
    () => (selectedItems ? knowledgeBaseChipOptions(selectedItems) : undefined),
    [selectedItems]
  );

  return (
    <SearchableMultiSelect
      id={id}
      label={label}
      labelHidden={labelHidden}
      labelledBy={labelledBy}
      helper={helper}
      required={required}
      invalid={invalid}
      describedBy={describedBy}
      options={options}
      value={selectedIds}
      onValueChange={onChange}
      selectedOptions={selectedOptions}
      onQueryChange={onQueryChange}
      remote={
        choices.remote
          ? {
              total: choices.matchedTotal,
              hasMore: choices.hasMore,
              loadingMore: choices.loadingMore,
              searching: choices.searching,
              onLoadMore: choices.loadMore,
            }
          : undefined
      }
      disabled={disabled}
      width={width}
      labels={knowledgeBaseMultiSelectLabels()}
    />
  );
}

function sortKnowledgeBases(items: readonly KnowledgeBaseSummary[]) {
  return [...items].sort((a, b) => {
    if (a.name === DEFAULT_KNOWLEDGE_BASE_NAME) return -1;
    if (b.name === DEFAULT_KNOWLEDGE_BASE_NAME) return 1;
    return compareNames(a.name, b.name);
  });
}
