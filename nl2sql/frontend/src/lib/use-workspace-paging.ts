import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS } from "@production-ready/ui";
import { useCallback, useMemo } from "react";

import { useWorkspaceState } from "@/components/WorkspaceState";

interface StoredPaging {
  page: number;
  pageSize: number;
}

/** 保存していた値が壊れていても（古い版・手で書き換えた値）、1 ページ目・既定の件数として読む。 */
function normalizePaging(value: unknown, defaultPageSize: number): StoredPaging {
  const record = value && typeof value === "object" ? (value as Partial<StoredPaging>) : {};
  const page = typeof record.page === "number" && Number.isSafeInteger(record.page) && record.page >= 1 ? record.page : 1;
  const pageSize =
    typeof record.pageSize === "number" && (PAGE_SIZE_OPTIONS as readonly number[]).includes(record.pageSize)
      ? record.pageSize
      : defaultPageSize;
  return { page, pageSize };
}

/**
 * 一覧のページ番号と表示件数を作業状態に残す（#1891。UX 契約 workspace-state.md「検索・フィルター・ソート・ページ」）。
 *
 * - 再読込・画面の移動のあとも同じページ・同じ表示件数に戻る（同じタブ・同じユーザー・同じ DB の一時草稿）。
 * - 表示件数を変えたら 1 ページ目へ戻す。絞り込み・並べ替えを変えたら呼び出し側が `reset()` で 1 ページ目へ戻す。
 * - サーバー側のページング（`OffsetPagination`）は `offset` / `pageSize` を API に渡し、クライアント側の一覧
 *   （`PagedDataTable` / `usePagination`）は `page` / `pageSize` を制御式で渡す。
 * - 保存したページが範囲の外になったとき（行が減った）は、共通の部品が最後のページへ寄せる。
 */
export function useWorkspacePaging(field: string, defaultPageSize: number = DEFAULT_PAGE_SIZE) {
  const [stored, setStored] = useWorkspaceState<StoredPaging>(field, { page: 1, pageSize: defaultPageSize });
  const { page, pageSize } = useMemo(() => normalizePaging(stored, defaultPageSize), [stored, defaultPageSize]);

  const setPage = useCallback(
    (next: number) =>
      setStored((current) => {
        const normalized = normalizePaging(current, defaultPageSize);
        const target = Math.max(1, Math.floor(next));
        return normalized.page === target ? current : { ...normalized, page: target };
      }),
    [defaultPageSize, setStored]
  );
  const setPageSize = useCallback(
    (next: number) =>
      setStored((current) => {
        const normalized = normalizePaging(current, defaultPageSize);
        if (normalized.pageSize === next && normalized.page === 1) return current;
        return { page: 1, pageSize: next };
      }),
    [defaultPageSize, setStored]
  );
  const reset = useCallback(
    () =>
      setStored((current) => {
        const normalized = normalizePaging(current, defaultPageSize);
        return normalized.page === 1 ? current : { ...normalized, page: 1 };
      }),
    [defaultPageSize, setStored]
  );

  return { page, pageSize, offset: (page - 1) * pageSize, setPage, setPageSize, reset };
}

export type WorkspacePaging = ReturnType<typeof useWorkspacePaging>;
