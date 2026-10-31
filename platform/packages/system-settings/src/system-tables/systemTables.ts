import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";

import { DATABASE_STATUS_QUERY_KEY } from "../database-gate/types";
import {
  SYSTEM_TABLES_QUERY_KEY,
  type SystemObjectMetadata,
  type SystemTableOperationStatus,
  type SystemTablesApi,
  type SystemTablesDeleteOrphansRequest,
  type SystemTablesInitializeRequest,
  type SystemTablesStatusData,
} from "./types";

/** 操作中（lease が running）の間は状態を定期的に取り直す間隔。 */
export const SYSTEM_TABLES_RUNNING_REFETCH_MS = 4000;

/** API の mutation か DB の lease が実行中なら、重複する操作を止める。 */
export function systemTableControlsBusy(
  mutationPending: boolean,
  operationStatus: SystemTableOperationStatus | undefined,
): boolean {
  return mutationPending || operationStatus === "running";
}

/** 全再作成の確認語は、前後の空白を除いた完全一致だけを許す（backend は完全一致で再検証する）。 */
export function isSystemTableRecreateConfirmationValid(value: string, phrase: string): boolean {
  return value.trim() === phrase;
}

/**
 * 状態の payload が画面の描画に必要な項目を満たすか（浅い検査）。
 * 想定外の形を描画して throw すると、同じページの兄弟カードごと消えるため、描画前に弾いて
 * 取得失敗の表示へ落とす。深い階層の破綻は製品の error boundary が受け止める。
 */
export function isSystemTablesStatusData(value: unknown): value is SystemTablesStatusData {
  if (typeof value !== "object" || value === null) return false;
  const data = value as Partial<SystemTablesStatusData>;
  const operationState = data.operation_state;
  return (
    typeof data.status === "string" &&
    typeof operationState === "object" &&
    operationState !== null &&
    typeof operationState.status === "string" &&
    Array.isArray(data.missing_objects) &&
    (data.retired_objects === undefined || Array.isArray(data.retired_objects)) &&
    (data.missing_columns === undefined || Array.isArray(data.missing_columns)) &&
    (data.missing_foreign_keys === undefined || Array.isArray(data.missing_foreign_keys)) &&
    (data.orphaned_foreign_keys === undefined || Array.isArray(data.orphaned_foreign_keys)) &&
    (data.mismatched_foreign_keys === undefined || Array.isArray(data.mismatched_foreign_keys)) &&
    (data.disabled_foreign_keys === undefined || Array.isArray(data.disabled_foreign_keys)) &&
    (data.pending_destructive_migrations === undefined || Array.isArray(data.pending_destructive_migrations)) &&
    Array.isArray(data.tables)
  );
}

/** 詳細の表の行。全管理 object が無い応答（RAG・旧 NL2SQL）はテーブルだけを並べる。 */
export function systemTableObjects(data: SystemTablesStatusData): SystemObjectMetadata[] {
  if (data.objects) return data.objects;
  return data.tables.map((table) => ({ ...table, object_type: "TABLE" }));
}

/** 詳細の見出しの件数（全管理 object が無いときはテーブルの件数）。 */
export function systemTableDetailCounts(data: SystemTablesStatusData): {
  existing: number;
  expected: number;
} {
  return data.objects
    ? { existing: data.existing_object_count, expected: data.expected_object_count }
    : { existing: data.existing_table_count, expected: data.expected_table_count };
}

/** 状態（DDL を実行しない）。操作中は定期的に取り直す。失敗は自動で再試行しない。 */
export function useSystemTablesStatus(api: SystemTablesApi) {
  return useQuery({
    queryKey: SYSTEM_TABLES_QUERY_KEY,
    queryFn: ({ signal }) => api.getSystemTablesStatus({ signal }),
    retry: false,
    refetchInterval: (query) =>
      isSystemTablesStatusData(query.state.data) && query.state.data.operation_state.status === "running"
        ? SYSTEM_TABLES_RUNNING_REFETCH_MS
        : false,
  });
}

export interface UseInitializeSystemTablesOptions {
  /** 成功したときに捨てる製品の cache（DB の状態は常に捨てる）。 */
  invalidateQueryKeys?: readonly QueryKey[];
}

/** 作成・更新 / 全再作成。成功したら結果で状態を置き換え、DB の状態（ゲート）を取り直す。 */
export function useInitializeSystemTables(
  api: SystemTablesApi,
  { invalidateQueryKeys = [] }: UseInitializeSystemTablesOptions = {},
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: SystemTablesInitializeRequest) => api.initializeSystemTables(payload),
    onMutate: () => qc.cancelQueries({ queryKey: SYSTEM_TABLES_QUERY_KEY }),
    onSuccess: (data) => {
      qc.setQueryData(SYSTEM_TABLES_QUERY_KEY, data);
      void qc.invalidateQueries({ queryKey: DATABASE_STATUS_QUERY_KEY });
      for (const queryKey of invalidateQueryKeys) void qc.invalidateQueries({ queryKey });
    },
    onError: () => {
      void qc.invalidateQueries({ queryKey: SYSTEM_TABLES_QUERY_KEY });
    },
  });
}

/**
 * 参照先のない行の削除（#511。API を持つ製品だけ）。成功したら結果で状態を置き換える。
 * 失敗（件数が増えた・前提が変わった等）のときは状態を取り直し、最新の件数で確認し直させる。
 */
export function useDeleteSystemTableOrphanedRows(
  api: SystemTablesApi,
  { invalidateQueryKeys = [] }: UseInitializeSystemTablesOptions = {},
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: SystemTablesDeleteOrphansRequest) => {
      if (!api.deleteSystemTableOrphanedRows) {
        return Promise.reject(new Error("参照先のない行の削除はこの製品では使えません。"));
      }
      return api.deleteSystemTableOrphanedRows(payload);
    },
    onMutate: () => qc.cancelQueries({ queryKey: SYSTEM_TABLES_QUERY_KEY }),
    onSuccess: (data) => {
      qc.setQueryData(SYSTEM_TABLES_QUERY_KEY, data);
      void qc.invalidateQueries({ queryKey: DATABASE_STATUS_QUERY_KEY });
      for (const queryKey of invalidateQueryKeys) void qc.invalidateQueries({ queryKey });
    },
    onError: () => {
      void qc.invalidateQueries({ queryKey: SYSTEM_TABLES_QUERY_KEY });
    },
  });
}
