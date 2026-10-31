import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  DATABASE_SETTINGS_QUERY_KEY,
  type DatabaseSettingsApi,
  type DatabaseSettingsUpdate,
} from "./types";

/** 接続設定が変わったときに製品側の query（DB 接続状態など）を更新する。 */
export type DatabaseChangedHandler = () => Promise<void> | void;

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
