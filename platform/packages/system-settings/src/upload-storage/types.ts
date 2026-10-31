/** アップロード保存先 API の型（backend の pr_system_settings.upload_storage と同じ形。#97）。 */
export type UploadStorageBackend = "local" | "oci";

export interface UploadStorageSettingsData {
  backend: UploadStorageBackend;
  local_storage_dir: string;
  object_storage_region: string;
  object_storage_namespace: string;
  object_storage_bucket: string;
  readiness: string;
  max_upload_bytes: number;
  config_source: "runtime";
  /**
   * 保存のときに確かめた OCI Object Storage の結果（PATCH で OCI を選んだときだけ。#2101）。
   * 一時的な失敗（接続できない・時間切れ）で確かめられなかったときは、保存して失敗の結果が入る。
   */
  storage_check?: UploadStorageCheckResult | null;
}

export type UploadStorageCheckStageKey = "config" | "bucket" | "write" | "read" | "delete";
export type UploadStorageCheckStageStatus = "success" | "failed" | "skipped";

export interface UploadStorageCheckStage {
  key: UploadStorageCheckStageKey;
  status: UploadStorageCheckStageStatus;
  message: string;
  action?: string | null;
}

/** `POST /api/settings/upload-storage/test` の結果（namespace・bucket・secret の値を含まない）。 */
export interface UploadStorageCheckResult {
  status: "success" | "failed";
  /** 一時的な失敗（接続できない・時間切れ・429・5xx）なら true。設定の誤りなら false。 */
  retryable: boolean;
  message: string;
  action?: string | null;
  stages: UploadStorageCheckStage[];
  elapsed_ms: number;
  checked_at?: string;
  region?: string | null;
  check_object?: string | null;
  http_status?: number | null;
  service_code?: string | null;
  request_id?: string | null;
  error_type?: string | null;
}

export interface UploadStorageSettingsUpdate {
  backend: UploadStorageBackend;
  local_storage_dir?: string;
  object_storage_region?: string;
  object_storage_namespace?: string;
  object_storage_bucket?: string;
}

/** 製品側の API 関数（fetch wrapper・認証・エラー形式は製品ごとに異なるため注入する）。 */
export interface UploadStorageApi {
  get: (options?: { signal?: AbortSignal }) => Promise<UploadStorageSettingsData>;
  update: (payload: UploadStorageSettingsUpdate) => Promise<UploadStorageSettingsData>;
  /** 入力中の値で OCI Object Storage の bucket と権限を確かめる（保存しない。#2101）。 */
  test: (payload: UploadStorageSettingsUpdate) => Promise<UploadStorageCheckResult>;
}

/** 3製品で共通の React Query key（各製品の queries.ts と同じ）。 */
export const UPLOAD_STORAGE_QUERY_KEY = ["settings", "upload-storage"] as const;
