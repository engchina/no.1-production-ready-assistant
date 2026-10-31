import { useQuery, useQueryClient } from "@tanstack/react-query";

import { DATABASE_STATUS_QUERY_KEY, type DatabaseStatusApi, type DatabaseStatusData } from "./types";

/** 接続先が変わったとき（`context_id` が変わったとき）に呼ぶ。業務データの cache を捨てる等。 */
export type DatabaseContextChangeHandler = (
  previous: DatabaseStatusData,
  next: DatabaseStatusData,
) => Promise<void> | void;

export interface UseDatabaseStatusOptions {
  enabled?: boolean;
  onContextChange?: DatabaseContextChangeHandler;
}

/** 短時間は cache し、画面の移動ごとに DB を確かめ直さない。 */
export const DATABASE_STATUS_STALE_TIME_MS = 15_000;

/**
 * DB の状態（`GET /api/ready/database`）。3 製品で同じ query key・cache 時間にする（#325）。
 * 失敗は自動で再試行しない（ゲートの「再試行」で利用者が確かめ直す）。
 */
export function useDatabaseStatus(
  api: DatabaseStatusApi,
  { enabled = true, onContextChange }: UseDatabaseStatusOptions = {},
) {
  const qc = useQueryClient();
  return useQuery({
    queryKey: DATABASE_STATUS_QUERY_KEY,
    queryFn: async ({ signal }) => {
      const next = await api.getDatabaseStatus({ signal });
      const previous = qc.getQueryData<DatabaseStatusData>(DATABASE_STATUS_QUERY_KEY);
      if (
        onContextChange &&
        previous?.context_id &&
        next.context_id &&
        previous.context_id !== next.context_id
      ) {
        await onContextChange(previous, next);
      }
      return next;
    },
    enabled,
    staleTime: DATABASE_STATUS_STALE_TIME_MS,
    retry: false,
  });
}
