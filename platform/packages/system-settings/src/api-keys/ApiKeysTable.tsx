import type { ReactNode } from "react";
import { Trash2 } from "lucide-react";
import {
  displayDateTimeFormat,
  PAGE_SIZE_OPTIONS,
  PagedDataTable,
  RowActionMenu,
  StatusBadge,
  type DataTableColumn,
} from "@production-ready/ui";

import { usePersistedListPaging, type ListPagingKey } from "../lists/listPaging";
import type { apiKeysTranslator } from "./messages";
import type { ApiKey } from "./types";

type Translate = ReturnType<typeof apiKeysTranslator>;

// システムの設定の時間帯で書く（#3265）。
const dateTimeFormat = displayDateTimeFormat({
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

/** ISO 文字列を「YYYY/MM/DD HH:mm」へ。無効値はダッシュ。 */
export function formatApiKeyDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : dateTimeFormat.format(date);
}

export interface ApiKeysTableProps {
  keys: ApiKey[];
  t: Translate;
  /** 管理の画面は利用者の列を出す。 */
  showOwner: boolean;
  onRevoke: (key: ApiKey) => void;
  resetKey?: unknown;
  empty: ReactNode;
  /** ページ番号と表示件数を残す作業状態の名前（自分のキー・全員のキー）。 */
  pagingKey: ListPagingKey;
  /** ページ送りの名前（「API キー一覧のページ」など）。 */
  paginationAriaLabel: string;
  paginationTestId?: string;
}

/**
 * API キーの一覧（自分の画面・管理の画面で共通。#1805）。一覧の標準形（表頭の固定・縦スクロール・
 * 25 件/ページ。`PagedDataTable`）。秘密は表示しない（見分けるための先頭だけ）。
 */
export function ApiKeysTable({
  keys,
  t,
  showOwner,
  onRevoke,
  resetKey,
  empty,
  pagingKey,
  paginationAriaLabel,
  paginationTestId,
}: ApiKeysTableProps) {
  // ページ番号と表示件数（10 / 25 / 50 / 100）は作業状態に残す（#1891）。
  const paging = usePersistedListPaging(pagingKey);
  const columns: DataTableColumn<ApiKey>[] = [
    {
      key: "name",
      header: t("apiKeys.column.name"),
      rowHeader: true,
      className: "min-w-48",
      render: (key) => (
        <>
          <span className="block break-words font-medium text-fg">{key.name}</span>
          <span className="block break-all font-mono text-xs text-fg-muted">{`${key.token_prefix}…`}</span>
        </>
      ),
    },
  ];
  if (showOwner) {
    columns.push({
      key: "owner",
      header: t("apiKeys.column.owner"),
      className: "text-xs text-fg",
      render: (key) => key.owner_display_name || t("apiKeys.deletedUser"),
    });
  }
  columns.push(
    {
      key: "products",
      header: t("apiKeys.column.products"),
      className: "whitespace-nowrap text-xs text-fg",
      // 対象を絞った製品は件数を添える（#1874。例: 「Agent（2 件に限定）」）。
      render: (key) =>
        key.products
          .map((product) => {
            const label = t(`apiKeys.product.${product}`);
            const scope = key.resource_scopes?.[product];
            return scope ? t("apiKeys.scope.count", { product: label, count: scope.length }) : label;
          })
          .join("・"),
    },
    {
      key: "expires",
      header: t("apiKeys.column.expires"),
      render: (key) =>
        key.expired ? (
          <StatusBadge variant="danger" label={t("apiKeys.expired")} />
        ) : (
          <span className="whitespace-nowrap text-xs tabular-nums text-fg">
            {key.expires_at ? formatApiKeyDateTime(key.expires_at) : t("apiKeys.noExpiry")}
          </span>
        ),
    },
    {
      key: "lastUsed",
      header: t("apiKeys.column.lastUsed"),
      className: "whitespace-nowrap text-xs tabular-nums text-fg-muted",
      render: (key) => (key.last_used_at ? formatApiKeyDateTime(key.last_used_at) : t("apiKeys.neverUsed")),
    },
    {
      key: "created",
      header: t("apiKeys.column.created"),
      className: "whitespace-nowrap text-xs tabular-nums text-fg-muted",
      render: (key) => formatApiKeyDateTime(key.created_at),
    },
    {
      key: "actions",
      header: t("apiKeys.column.actions"),
      align: "right",
      render: (key) => (
        <RowActionMenu
          actions={[
            { id: "revoke", label: t("apiKeys.revoke"), icon: Trash2, tone: "danger", onSelect: () => onRevoke(key) },
          ]}
          ariaLabel={t("apiKeys.rowActions", { name: key.name })}
          testId={`api-key-row-actions-${key.id}`}
        />
      ),
    }
  );
  return (
    <PagedDataTable
      resetKey={resetKey}
      page={paging.page}
      onPageChange={paging.setPage}
      pageSize={paging.pageSize}
      pageSizeOptions={PAGE_SIZE_OPTIONS}
      onPageSizeChange={paging.setPageSize}
      paginationAriaLabel={paginationAriaLabel}
      paginationTestId={paginationTestId}
      rows={keys}
      columns={columns}
      getRowKey={(key) => key.id}
      ariaLabel={t("apiKeys.list.label")}
      tableClassName={showOwner ? "w-full min-w-[56rem]" : "w-full min-w-[48rem]"}
      empty={empty}
    />
  );
}
