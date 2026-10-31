import type { ListPickerLabels } from "@production-ready/ui";

import { formatNumber } from "./format";
import { t } from "./i18n";

/** 共通の ListPicker（大量の候補から複数を選ぶ一覧。#600）に渡す RAG の文言。画面ごとの文言は呼び出し側で上書きする。 */
export function listPickerLabels(overrides: Partial<ListPickerLabels> = {}): Partial<ListPickerLabels> {
  return {
    resultCount: ({ visible, total, selected }) =>
      t("listPicker.resultCount", {
        visible: formatNumber(visible),
        total: formatNumber(total),
        selected: formatNumber(selected),
      }),
    searchResultCount: (total) => t("common.searchResultCount", { count: formatNumber(total) }),
    refreshing: t("listPicker.refreshing"),
    loadMore: t("listPicker.loadMore"),
    retry: t("common.retry"),
    clearSearch: t("common.clearSearch"),
    showSelected: (selected) => t("listPicker.showSelected", { count: formatNumber(selected) }),
    selectedEmpty: t("listPicker.selectedEmpty"),
    selectVisible: t("listPicker.selectVisible"),
    clearSelection: t("listPicker.clearSelection"),
    keyboardHint: t("listPicker.keyboardHint"),
    ...overrides,
  };
}
