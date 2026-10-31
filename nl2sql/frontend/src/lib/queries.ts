import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  DATABASE_STATUS_QUERY_KEY,
  SYSTEM_TABLES_QUERY_KEY,
  useDatabaseStatus as useSharedDatabaseStatus,
} from "@production-ready/system-settings";

import {
  api,
  type SelectAiCredentialCreateRequest,
} from "@/lib/api";

export const queryKeys = {
  databaseStatus: DATABASE_STATUS_QUERY_KEY,
  persistenceStatus: ["nl2sql", "persistence"] as const,
  modelSettings: ["settings", "model"] as const,
  databaseSettings: ["settings", "database"] as const,
  selectAiCredential: ["settings", "database", "select-ai-credential"] as const,
  systemTables: SYSTEM_TABLES_QUERY_KEY,
  schemaOwners: ["schema", "owners"] as const,
  adbInfo: ["settings", "database", "adb"] as const,
  uploadStorageSettings: ["settings", "upload-storage"] as const,
};

/** 接続先が変わったら、前の接続先の業務データ（NL2SQL・スキーマ）の cache を捨てる。 */
export async function clearDatabaseContextQueries(qc: QueryClient) {
  const business = {
    predicate: (query: { queryKey: readonly unknown[] }) =>
      ["nl2sql", "schema"].includes(String(query.queryKey[0])),
  };
  await qc.cancelQueries(business);
  qc.removeQueries(business);
}

/** DB の状態（3製品共通の hook。#325）。接続先が変わったら業務データの cache を捨てる。 */
export function useDatabaseStatus({
  enabled = true,
}: { enabled?: boolean } = {}) {
  const qc = useQueryClient();
  return useSharedDatabaseStatus(api, {
    enabled,
    onContextChange: () => clearDatabaseContextQueries(qc),
  });
}

export function usePersistenceStatus({
  enabled = true,
}: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: queryKeys.persistenceStatus,
    queryFn: ({ signal }) => api.getPersistenceStatus({ signal }),
    enabled,
    retry: false,
  });
}

export function useRecoverPersistence() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.recoverPersistence,
    onSuccess: (data) => qc.setQueryData(queryKeys.persistenceStatus, data),
  });
}

export function useSelectAiCredential() {
  return useQuery({
    queryKey: queryKeys.selectAiCredential,
    queryFn: ({ signal }) => api.getSelectAiCredential({ signal }),
    retry: false,
  });
}

export function useCreateSelectAiCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: SelectAiCredentialCreateRequest) =>
      api.createSelectAiCredential(payload),
    onSuccess: (data) => qc.setQueryData(queryKeys.selectAiCredential, data),
    onError: () =>
      qc.invalidateQueries({ queryKey: queryKeys.selectAiCredential }),
  });
}

export function useSchemaOwners() {
  return useQuery({
    queryKey: queryKeys.schemaOwners,
    queryFn: ({ signal }) => api.getSchemaOwners({ signal }),
    staleTime: 30_000,
    retry: false,
  });
}
