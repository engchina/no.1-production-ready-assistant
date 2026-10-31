/** データベース設定 API の型（backend の pr_system_settings.database と同じ形。#108）。 */
export type DatabaseConnectionSecurity = "wallet_mtls" | "walletless_tls";
export type DatabaseConnectionTestStatus = "success" | "failed";
export type AdbOperationStatus =
  | "success"
  | "not_configured"
  | "error"
  | "accepted"
  | "already_available"
  | "already_stopped"
  | "cannot_start"
  | "cannot_stop";

export interface DatabaseSettingsData {
  user: string;
  dsn: string;
  driver_mode: "thin" | "thick";
  connection_security: DatabaseConnectionSecurity;
  client_lib_dir: string;
  wallet_dir: string;
  wallet_uploaded: boolean;
  available_services: string[];
  has_password: boolean;
  has_wallet_password: boolean;
  readiness: string;
  embedding_dimension: number;
  vector_column: string;
  adb_ocid: string;
  region: string;
  config_source: "runtime";
}

export interface DatabaseSettingsUpdate {
  user: string;
  dsn: string;
  connection_security?: DatabaseConnectionSecurity;
  wallet_dir: string;
  password?: string;
  wallet_password?: string;
  clear_password?: boolean;
  clear_wallet_password?: boolean;
}

export interface DatabasePasswordRevealData {
  password: string;
}

export interface DatabaseWalletDownloadData {
  status: "downloaded" | "already_configured";
  settings: DatabaseSettingsData;
}

export interface DatabaseConnectionTestResult {
  status: DatabaseConnectionTestStatus;
  readiness: string;
  message: string;
  elapsed_ms: number;
  troubleshooting: string[];
  details: Record<string, string | number | boolean | null>;
  checked_at: string;
  error_type: string | null;
}

export interface AdbSettingsUpdate {
  adb_ocid: string;
  region: string;
}

export interface AdbInfoData {
  status: AdbOperationStatus;
  message: string;
  error_code?: string | null;
  id?: string | null;
  display_name?: string | null;
  lifecycle_state?: string | null;
  db_name?: string | null;
  cpu_core_count?: number | null;
  data_storage_size_in_tbs?: number | null;
  region?: string | null;
}

/** 製品側の API 関数（fetch wrapper・認証・エラー形式は製品ごとに異なるため注入する）。 */
export interface DatabaseSettingsApi {
  getDatabaseSettings: (options?: {
    signal?: AbortSignal;
  }) => Promise<DatabaseSettingsData>;
  updateDatabaseSettings: (
    payload: DatabaseSettingsUpdate,
  ) => Promise<DatabaseSettingsData>;
  uploadDatabaseWallet: (file: File) => Promise<DatabaseSettingsData>;
  downloadDatabaseWallet: () => Promise<DatabaseWalletDownloadData>;
  /** 保存済み DB パスワードの表示（backend で有効な製品だけ渡す）。 */
  revealDatabasePassword?: () => Promise<DatabasePasswordRevealData>;
  testDatabaseSettings: (
    payload: DatabaseSettingsUpdate,
  ) => Promise<DatabaseConnectionTestResult>;
  getAdbInfo: (options?: { signal?: AbortSignal }) => Promise<AdbInfoData>;
  updateAdbSettings: (payload: AdbSettingsUpdate) => Promise<AdbInfoData>;
  startAdb: () => Promise<AdbInfoData>;
  stopAdb: () => Promise<AdbInfoData>;
}

/** 3製品で共通の React Query key（各製品の queries.ts と同じ）。 */
export const DATABASE_SETTINGS_QUERY_KEY = ["settings", "database"] as const;
export const ADB_INFO_QUERY_KEY = ["settings", "database", "adb"] as const;
