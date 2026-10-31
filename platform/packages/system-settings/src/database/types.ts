/** データベース設定 API の型（backend の pr_system_settings.database と同じ形。#108）。 */
export type DatabaseConnectionSecurity = "wallet_mtls" | "walletless_tls";
export type DatabaseConnectionTestStatus = "success" | "failed";

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

/** 製品側の API 関数（fetch wrapper・認証・エラー形式は製品ごとに異なるため注入する）。 */
export interface DatabaseSettingsApi {
  getDatabaseSettings: (options?: {
    signal?: AbortSignal;
  }) => Promise<DatabaseSettingsData>;
  updateDatabaseSettings: (
    payload: DatabaseSettingsUpdate,
  ) => Promise<DatabaseSettingsData>;
  uploadDatabaseWallet: (file: File) => Promise<DatabaseSettingsData>;
  /** 保存済み DB パスワードの表示（backend で有効な製品だけ渡す）。 */
  revealDatabasePassword?: () => Promise<DatabasePasswordRevealData>;
  testDatabaseSettings: (
    payload: DatabaseSettingsUpdate,
  ) => Promise<DatabaseConnectionTestResult>;
}

/** 3製品で共通の React Query key（各製品の queries.ts と同じ）。 */
export const DATABASE_SETTINGS_QUERY_KEY = ["settings", "database"] as const;
