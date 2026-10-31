/**
 * DB の状態 API（`GET /api/ready/database`。#325）の契約。
 * backend は `pr_system_settings.database_status` が 3 製品共通で返す。
 */
export type DatabaseAvailability = "ok" | "not_configured" | "unreachable" | "setup_required";

export interface DatabaseStatusData {
  status: DatabaseAvailability;
  /** 設定の判定（`database_readiness`）の値、または製品の準備状態の確認が付けたコード。 */
  check: string;
  /** 公開してよい補足（ORA コード等）。画面には出さない（#320）。 */
  detail: string | null;
  /** 接続先が変わったことを知るための識別子（接続先の値の hash）。 */
  context_id?: string;
  /** 製品のシステムテーブルの状態（RAG の `missing` / `partial` / `outdated` / `ready`）。 */
  schema_status?: string | null;
  /**
   * ADB のライフサイクル状態（`STOPPED` / `STARTING` / `AVAILABLE` など）。`unreachable` のときだけ、
   * ADB OCID が設定されていて取得できた場合に入る（#820）。
   */
  adb_lifecycle_state?: string | null;
}

export interface DatabaseStatusApi {
  getDatabaseStatus: (options?: { signal?: AbortSignal }) => Promise<DatabaseStatusData>;
}

/** 3 製品で同じ query key にする（DB 設定の保存後に製品が invalidate する）。 */
export const DATABASE_STATUS_QUERY_KEY = ["ready", "database"] as const;

/**
 * 業務 API の失敗から DB の不通を確かめたときに通知する window event（NL2SQL から移設）。
 * `detail` が `{ kind: "database", database }` のとき、ゲートは状態を置き換えて案内を出す。
 * それ以外の `kind`（NL2SQL の保存領域など）は `secondaryGate` が扱う。
 */
export const DATABASE_UNAVAILABLE_EVENT = "app-database-unavailable";

export type DatabaseUnavailableEventDetail =
  | { kind: "database"; database: DatabaseStatusData }
  | { kind: string; [key: string]: unknown };

/** ゲートの案内の種類。`check_failed` は状態 API 自体が失敗したとき。 */
export type DatabaseNoticeStatus = "not_configured" | "setup_required" | "unreachable" | "check_failed";

/** データベース設定の画面の ADB 管理のカードの id（ADB が停止中などのときの案内先。#820）。 */
export const ADB_MANAGEMENT_ANCHOR_ID = "adb-management";

export interface DatabaseGateRoutes {
  /**
   * データベース設定（例: `/settings/database`）。hash は付けなくてよい。ADB が停止中・起動中などの
   * ときは案内が ADB 管理のカード（`#adb-management`）を付ける（#820）。
   */
  databaseSettings: string;
  /**
   * システムテーブルの管理（`setup_required` の案内先）。省略するとデータベース設定へ案内する。
   * この path（hash を除く）では `setup_required` でも画面を開ける（作成・更新をする場所なので塞がない）。
   */
  systemTables?: string;
}
