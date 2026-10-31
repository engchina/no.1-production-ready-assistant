/**
 * システムテーブルの管理 API（`GET /api/settings/database/system-tables` と
 * `POST /api/settings/database/system-tables/initialize`。#325）と、参照先のない行の削除
 * （RAG の `POST /api/settings/database/system-tables/orphaned-rows/delete`。#511）の契約。
 * backend の骨格は `pr_system_settings.system_schema`、manifest と DDL の正本は製品が持つ。
 */
export type SystemTableSchemaStatus = "missing" | "partial" | "outdated" | "ready";
export type SystemTableOperationStatus = "idle" | "running" | "failed";
export type SystemTableOperationResult = "no_op" | "initialized" | "migrated" | "recreated";
/** 参照先のない行の削除の結果（削除も検査も要らなかったときは `no_op`。#511）。 */
export type SystemTableOrphanOperationResult = "no_op" | "orphans_deleted";
/** Oracle の object の種類（NL2SQL は TABLE / INDEX / SEQUENCE / PACKAGE / PACKAGE BODY）。 */
export type SystemObjectType = string;

export interface SystemTableObjectRef {
  name: string;
  object_type: SystemObjectType;
}

/** 既存の表に無い正本の列の 1 件（#2450 / #2458。3 製品が返す）。 */
export interface SystemTableColumnRef {
  table_name: string;
  column_name: string;
}

/** 外部キーの差分の 1 件（#505。RAG だけが返す）。 */
export interface SystemTableForeignKey {
  name: string;
  table_name: string;
  columns: string[];
  referenced_table_name: string;
  referenced_columns: string[];
  delete_rule: string;
  /** 参照先の無い既存の行の件数（数えられなかったときは null）。 */
  orphan_rows: number | null;
  /** 削除規則が正本と違う FK（#511）の、既存の FK の名前と削除規則（それ以外は null / 無し）。 */
  current_name?: string | null;
  current_delete_rule?: string | null;
}

/** 未適用の、データを消す（テーブルの DROP・行の DELETE を含む）migration（RAG だけが返す。#619）。 */
export interface SystemTableDestructiveMigration {
  name: string;
  /** 消えるデータと、適用の前にすること（書き出しなど）の説明。 */
  description: string;
}

export interface SystemTableMetadata {
  name: string;
  /** 接続ユーザーの schema と所有者付きの名前（NL2SQL だけが返す。無ければ名前だけを出す）。 */
  owner?: string;
  qualified_name?: string;
  exists: boolean;
  estimated_rows: number | null;
  created_at: string | null;
  last_analyzed_at: string | null;
}

export interface SystemObjectMetadata extends SystemTableMetadata {
  object_type: SystemObjectType;
}

export interface SystemTableOperationState {
  status: SystemTableOperationStatus;
  operation_kind: "initialize" | "recreate" | null;
  lease_expires_at: string | null;
  last_error_code: string | null;
  schema_epoch: number;
  updated_at: string | null;
}

export interface SystemTablesStatusData {
  status: SystemTableSchemaStatus;
  /** 最新の migration（RAG は名前、NL2SQL は番号）。 */
  schema_head: string | number;
  applied_versions: Array<string | number>;
  pending_versions: Array<string | number>;
  /** 未適用の、データを消す migration（「作成・更新」の前に承認が要る。RAG。#619）。 */
  pending_destructive_migrations?: SystemTableDestructiveMigration[];
  expected_object_count: number;
  existing_object_count: number;
  expected_table_count: number;
  existing_table_count: number;
  missing_objects: SystemTableObjectRef[];
  /** 廃止したが残っている object（RAG）。 */
  retired_objects?: SystemTableObjectRef[];
  /** 既存の表に無い正本の列（台帳が適用済みでも欠けた列。3 製品。#2450 / #2458）。 */
  missing_columns?: SystemTableColumnRef[];
  /** 既存の表に無い正本の外部キー（「作成・更新」で追加する。RAG。#505）。 */
  missing_foreign_keys?: SystemTableForeignKey[];
  /** 既存の行を検査せずに追加した外部キーに残る、参照先の無い行（RAG。#505）。 */
  orphaned_foreign_keys?: SystemTableForeignKey[];
  /** 削除規則が正本と違う外部キー（「作成・更新」で作り直す。RAG。#511）。 */
  mismatched_foreign_keys?: SystemTableForeignKey[];
  /** 無効化（DISABLED）された外部キー（「作成・更新」で有効にする。RAG。#511）。 */
  disabled_foreign_keys?: SystemTableForeignKey[];
  tables: SystemTableMetadata[];
  /** 全管理 object（NL2SQL）。無ければ `tables` をテーブルとして並べる。 */
  objects?: SystemObjectMetadata[];
  operation_state: SystemTableOperationState;
}

export interface SystemTablesInitializeRequest {
  recreate: boolean;
  confirmation?: string;
  /** データを消す未適用の migration の適用を承認する（確認ダイアログの後だけ true。#619）。 */
  allow_destructive?: boolean;
}

/**
 * 保存済みのデータを書き換えた後に、再起動が要るプロセス（Agent だけが返す。#1844）。
 * 操作を処理した backend のメモリは読み直し済みで、読み直せない別のプロセスだけを挙げる。
 */
export interface SystemTableRestartRequired {
  /** 再起動が要る理由（製品が日本語で返す）。 */
  message: string;
  /** 再起動するプロセス（名前と、systemd の unit・起動の command）。 */
  processes: string[];
}

export interface SystemTablesOperationData extends SystemTablesStatusData {
  operation: SystemTableOperationResult;
  dropped_object_count: number;
  created_object_count: number;
  /** 再起動が要るプロセス（無ければ null か無し。Agent。#1844）。 */
  restart_required?: SystemTableRestartRequired | null;
}

/** 参照先のない行の削除（#511）。`expected_orphan_rows` は利用者が確認した件数。 */
export interface SystemTablesDeleteOrphansRequest {
  constraint_name: string;
  expected_orphan_rows: number;
}

export interface SystemTablesOrphanDeletionData extends SystemTablesStatusData {
  operation: SystemTableOrphanOperationResult;
  deleted_row_count: number;
  foreign_key: SystemTableForeignKey;
}

export interface SystemTablesApi {
  getSystemTablesStatus: (options?: { signal?: AbortSignal }) => Promise<SystemTablesStatusData>;
  initializeSystemTables: (body: SystemTablesInitializeRequest) => Promise<SystemTablesOperationData>;
  /** 参照先のない行の削除（RAG だけが持つ。無ければ削除の操作を出さない。#511）。 */
  deleteSystemTableOrphanedRows?: (
    body: SystemTablesDeleteOrphansRequest,
  ) => Promise<SystemTablesOrphanDeletionData>;
}

/** 3 製品で同じ query key にする。 */
export const SYSTEM_TABLES_QUERY_KEY = ["settings", "database", "system-tables"] as const;
