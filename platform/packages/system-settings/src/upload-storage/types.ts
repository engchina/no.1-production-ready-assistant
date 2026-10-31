/** アップロード保存先 API の型（backend の pr_system_settings.upload_storage と同じ形。#97）。 */
export interface UploadStorageSettingsData {
  local_storage_dir: string;
  readiness: string;
  max_upload_bytes: number;
  config_source: "runtime";
}

export interface UploadStorageSettingsUpdate {
  local_storage_dir: string;
}

/** 製品側の API 関数（fetch wrapper・認証・エラー形式は製品ごとに異なるため注入する）。 */
export interface UploadStorageApi {
  get: (options?: { signal?: AbortSignal }) => Promise<UploadStorageSettingsData>;
  update: (payload: UploadStorageSettingsUpdate) => Promise<UploadStorageSettingsData>;
}

/** 3製品で共通の React Query key（各製品の queries.ts と同じ）。 */
export const UPLOAD_STORAGE_QUERY_KEY = ["settings", "upload-storage"] as const;
