import type { ReactNode } from "react";

import type { DataTableColumn } from "./data-table";
import { StatusBadge } from "./status-badge";

/** 公開した版の履歴の表の見出し・バッジの文言（製品の i18n の文字列を渡す）。 */
export interface PublishedVersionColumnLabels {
  version: string;
  /** 公開中の版のバッジ。 */
  current: string;
  publishedAt: string;
  publishedBy: string;
  note: string;
  actions: string;
}

export interface PublishedVersionColumnsOptions<T> {
  getVersion: (row: T) => number;
  /** 公開中の版（無ければ null）。その行に「公開中」のバッジを出す。 */
  currentVersion: number | null | undefined;
  /** 版の番号の表示（例 `v3` / `版 3`）。 */
  formatVersion: (version: number) => string;
  labels: PublishedVersionColumnLabels;
  renderPublishedAt: (row: T) => ReactNode;
  renderPublishedBy: (row: T) => ReactNode;
  renderNote: (row: T) => ReactNode;
  /** 公開者とメモの間に足す、製品の列（固定したスキル・版のタイトルなど）。 */
  extraColumns?: DataTableColumn<T>[];
  /** 行の操作（`RowActionMenu`）。 */
  renderActions: (row: T) => ReactNode;
}

/**
 * 公開した版の履歴（Agent の業務 Agent・RAG の業務ガイド）の表の列（#2356）。
 *
 * 版（公開中のバッジ）→ 公開日時 → 公開者 → 製品の列 → メモ → 操作 の順を 2 製品でそろえる。表そのものは
 * 製品の `PagedDataTable`（ページ送りの文言・表示件数の作業状態）に渡す。保存する版の数の案内
 * （「直近 N 版を保存します」）は、表の見出しの説明に製品の i18n で書く（N は API の値）。
 */
export function publishedVersionColumns<T>({
  getVersion,
  currentVersion,
  formatVersion,
  labels,
  renderPublishedAt,
  renderPublishedBy,
  renderNote,
  extraColumns = [],
  renderActions,
}: PublishedVersionColumnsOptions<T>): DataTableColumn<T>[] {
  return [
    {
      key: "version",
      header: labels.version,
      rowHeader: true,
      render: (row) => {
        const version = getVersion(row);
        return (
          <span className="inline-flex flex-wrap items-center gap-2">
            <span className="tnum font-medium text-fg">{formatVersion(version)}</span>
            {/* 公開中は色だけでなく文言のバッジで示す。 */}
            {version === currentVersion ? <StatusBadge variant="success" label={labels.current} /> : null}
          </span>
        );
      },
    },
    {
      key: "published_at",
      header: labels.publishedAt,
      className: "tnum text-fg-muted",
      render: (row) => renderPublishedAt(row),
    },
    {
      key: "published_by",
      header: labels.publishedBy,
      className: "break-words text-fg-muted",
      render: (row) => renderPublishedBy(row),
    },
    ...extraColumns,
    {
      key: "note",
      header: labels.note,
      className: "max-w-sm break-words text-fg-muted",
      render: (row) => renderNote(row),
    },
    {
      key: "actions",
      header: labels.actions,
      align: "right",
      render: (row) => renderActions(row),
    },
  ];
}
