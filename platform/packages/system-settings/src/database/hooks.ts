import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ADB_INFO_QUERY_KEY,
  DATABASE_SETTINGS_QUERY_KEY,
  type AdbSettingsUpdate,
  type DatabaseSettingsApi,
  type DatabaseSettingsUpdate,
} from "./types";

/** 接続設定が変わったときに製品側の query（DB 接続状態など）を更新する。 */
export type DatabaseChangedHandler = () => Promise<void> | void;

const ACTIVE_REFETCH_INTERVAL_MS = 4000;

const ADB_TRANSITIONAL_STATES: ReadonlySet<string> = new Set([
  "STARTING",
  "STOPPING",
  "PROVISIONING",
  "TERMINATING",
  "UPDATING",
  "RESTORING",
  "BACKUP_IN_PROGRESS",
  "MAINTENANCE_IN_PROGRESS",
  "ROLE_CHANGE_IN_PROGRESS",
]);

export function useDatabaseSettings(api: DatabaseSettingsApi) {
  return useQuery({
    queryKey: DATABASE_SETTINGS_QUERY_KEY,
    queryFn: ({ signal }) => api.getDatabaseSettings({ signal }),
  });
}

export function useUpdateDatabaseSettings(
  api: DatabaseSettingsApi,
  onChanged?: DatabaseChangedHandler,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: DatabaseSettingsUpdate) =>
      api.updateDatabaseSettings(payload),
    onSuccess: async () => {
      await onChanged?.();
      void qc.invalidateQueries({ queryKey: DATABASE_SETTINGS_QUERY_KEY });
    },
  });
}

export function useUploadDatabaseWallet(
  api: DatabaseSettingsApi,
  onChanged?: DatabaseChangedHandler,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => api.uploadDatabaseWallet(file),
    onSuccess: () => {
      // Wallet を置き換えると接続の可否が変わる。DB ゲートの状態（製品の query）も確かめ直す
      // （OCI からの取得・接続設定の保存と同じ。戻った画面で古い「未設定」の案内を出さない）。
      void onChanged?.();
      void qc.invalidateQueries({ queryKey: DATABASE_SETTINGS_QUERY_KEY });
    },
  });
}

export function useDownloadDatabaseWallet(
  api: DatabaseSettingsApi,
  onChanged?: DatabaseChangedHandler,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.downloadDatabaseWallet(),
    onSuccess: (data) => {
      qc.setQueryData(DATABASE_SETTINGS_QUERY_KEY, data.settings);
      void onChanged?.();
    },
  });
}

export function useRevealDatabasePassword(api: DatabaseSettingsApi) {
  return useMutation({
    mutationFn: async () => {
      if (!api.revealDatabasePassword)
        throw new Error("password reveal is not available");
      return api.revealDatabasePassword();
    },
  });
}

export function useTestDatabaseSettings(api: DatabaseSettingsApi) {
  return useMutation({
    mutationFn: (payload: DatabaseSettingsUpdate) =>
      api.testDatabaseSettings(payload),
  });
}

export function useAdbInfo(api: DatabaseSettingsApi) {
  return useQuery({
    queryKey: ADB_INFO_QUERY_KEY,
    queryFn: ({ signal }) => api.getAdbInfo({ signal }),
    retry: false,
    refetchInterval: (query) => {
      const state = query.state.data?.lifecycle_state;
      return state != null && ADB_TRANSITIONAL_STATES.has(state)
        ? ACTIVE_REFETCH_INTERVAL_MS
        : false;
    },
  });
}

export function useUpdateAdbSettings(api: DatabaseSettingsApi) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: AdbSettingsUpdate) => api.updateAdbSettings(payload),
    onSuccess: (data) => {
      qc.setQueryData(ADB_INFO_QUERY_KEY, data);
      void qc.invalidateQueries({ queryKey: DATABASE_SETTINGS_QUERY_KEY });
    },
  });
}

export function useStartAdb(
  api: DatabaseSettingsApi,
  onChanged?: DatabaseChangedHandler,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.startAdb(),
    // 操作開始前の ADB 情報リクエストが遅れて STOPPED を書き戻すと、
    // STARTING のポーリングが開始されないため in-flight query を無効化する。
    onMutate: () => qc.cancelQueries({ queryKey: ADB_INFO_QUERY_KEY }),
    onSuccess: (data) => {
      qc.setQueryData(ADB_INFO_QUERY_KEY, data);
      void onChanged?.();
    },
  });
}

export function useStopAdb(api: DatabaseSettingsApi) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.stopAdb(),
    onMutate: () => qc.cancelQueries({ queryKey: ADB_INFO_QUERY_KEY }),
    onSuccess: (data) => qc.setQueryData(ADB_INFO_QUERY_KEY, data),
  });
}
