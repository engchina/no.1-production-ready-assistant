import type { UploadStorageSettingsData } from "../upload-storage/types";

/** OCI 認証 API の型（backend の pr_system_settings.oci と同じ形。#100）。 */
export type OciConfigField = "user" | "fingerprint" | "tenancy" | "region" | "key_file";
export type OciConfigTestStatus = "success" | "failed";

export interface OciSettingsData {
  config_file: string;
  profile: string;
  user: string;
  fingerprint: string;
  tenancy: string;
  region: string;
  key_file: string;
  key_file_exists: boolean;
  config_file_exists: boolean;
  config_source: "runtime";
  /** config ファイルはあるが読み取れない（形式・文字コード・サイズ・権限）ときの理由。読めたとき・未作成のときは null（#1067）。 */
  config_error?: string | null;
}

export interface OciSettingsUpdate {
  user: string;
  fingerprint: string;
  tenancy: string;
  region: string;
}

export interface OciObjectStorageSettingsUpdate {
  object_storage_region: string;
  object_storage_namespace: string;
}

export interface OciConfigReadRequest {
  config_file: string;
  profile: string;
}

export interface OciConfigReadData {
  profile: string;
  user: string;
  fingerprint: string;
  tenancy: string;
  region: string;
  key_file: string;
  applied_fields: OciConfigField[];
}

export type OciConfigTestStageKey = "config_format" | "key_file" | "region" | "authentication";
export type OciConfigTestStageStatus = "success" | "failed" | "skipped";

export interface OciConfigTestStage {
  key: OciConfigTestStageKey;
  status: OciConfigTestStageStatus;
  message: string;
  action: string | null;
}

export interface OciConfigTestResult {
  status: OciConfigTestStatus;
  profile: string;
  config_file: string;
  key_file: string;
  config_file_exists: boolean;
  key_file_exists: boolean;
  missing_fields: OciConfigField[];
  permission_issues: string[];
  oci_directory_mode: string | null;
  config_file_mode: string | null;
  key_file_mode: string | null;
  message: string;
  elapsed_ms: number;
  checked_at: string;
  error_type: string | null;
  stages: OciConfigTestStage[];
  region: string | null;
  auth_check_operation: string | null;
  http_status: number | null;
  service_code: string | null;
  request_id: string | null;
}

export interface OciObjectStorageNamespaceRequest {
  config_file: string;
  profile: string;
  region: string;
}

export interface OciObjectStorageNamespaceData {
  namespace: string;
}

export interface OciPrivateKeyUploadData {
  key_file: string;
  saved: boolean;
}

/** 製品側の API 関数（NL2SQL の api オブジェクトと同じ名前。fetch wrapper は製品ごとに異なるため注入する）。 */
export interface OciSettingsApi {
  getOciSettings: (options?: { signal?: AbortSignal }) => Promise<OciSettingsData>;
  getUploadStorageSettings: (options?: { signal?: AbortSignal }) => Promise<UploadStorageSettingsData>;
  updateOciSettings: (body: OciSettingsUpdate) => Promise<OciSettingsData>;
  updateOciObjectStorageSettings: (
    body: OciObjectStorageSettingsUpdate,
  ) => Promise<UploadStorageSettingsData>;
  readOciConfig: (body: OciConfigReadRequest) => Promise<OciConfigReadData>;
  testOciConfig: () => Promise<OciConfigTestResult>;
  readOciObjectStorageNamespace: (
    body: OciObjectStorageNamespaceRequest,
  ) => Promise<OciObjectStorageNamespaceData>;
  uploadOciPrivateKey: (file: File) => Promise<OciPrivateKeyUploadData>;
}
