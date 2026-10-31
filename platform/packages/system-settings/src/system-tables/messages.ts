/**
 * システムテーブルの管理カードの既定の文言（NL2SQL の文言が基準。#325）。
 * key は製品の i18n key と同じにして、製品は自分の辞書の値で上書きする（製品名が入る文言など）。
 */
export const SYSTEM_TABLES_MESSAGES = {
  "settings.database.systemTables.title": "システムテーブル",
  "settings.database.systemTables.description":
    "製品が使う Oracle のテーブル・索引と migration の状態を確認し、管理者の明示操作で準備します。アプリ起動時に DDL は実行しません。",
  "settings.database.systemTables.loading": "システムテーブルの状態を読み込んでいます",
  "settings.database.systemTables.status.ready": "初期化済み",
  "settings.database.systemTables.status.missing": "未初期化",
  "settings.database.systemTables.status.partial": "一部不足",
  "settings.database.systemTables.status.outdated": "更新必要",
  "settings.database.systemTables.statusHint.missing":
    "主要テーブルがまだありません。「作成・更新」で初期化できます。",
  "settings.database.systemTables.statusHint.partial":
    "必須オブジェクトが {count} 件不足しています。「作成・更新」で既存データを保持したまま補完できます。",
  "settings.database.systemTables.statusHint.outdated":
    "テーブルは存在しますが migration version または checksum が古くなっています。無損失で更新できます。",
  "settings.database.systemTables.statusHint.outdatedDestructive":
    "テーブルは存在しますが migration version または checksum が古くなっています。",
  "settings.database.systemTables.destructive.title": "データを削除する更新があります",
  "settings.database.systemTables.destructive.description":
    "次の migration は「作成・更新」でテーブルや行を削除します。削除したデータは元に戻せません。残すデータを書き出し、データベースのバックアップを確認してから実行してください。",
  "settings.database.systemTables.destructive.approvalHint":
    "「作成・更新」を押すと、削除の確認を求めます。",
  "settings.database.systemTables.destructive.confirmTitle": "データを削除する更新を実行しますか？",
  "settings.database.systemTables.destructive.confirmDescription":
    "データを削除する migration {count} 件（{names}）を適用します。削除したテーブルと行は元に戻せません。残すデータを書き出してから実行してください。",
  "settings.database.systemTables.destructive.confirmLabel": "削除して更新",
  "settings.database.systemTables.columns.missing":
    "既存のテーブルに必須の列が {count} 件ありません。「作成・更新」で、列を補う migration を適用します。適用した後も不足するときは、配備手順の既存環境の更新手順を確認してください。",
  "settings.database.systemTables.foreignKeys.missing":
    "既存のテーブルに外部キーが {count} 件ありません。「作成・更新」で追加します。参照先のない既存の行がある外部キーは、既存の行を検査せずに追加し（新しい行から適用）、既存の行は削除しません。",
  "settings.database.systemTables.foreignKeys.orphanedTitle": "参照先のない既存の行があります",
  "settings.database.systemTables.foreignKeys.orphaned":
    "次の外部キーは既存の行を検査せずに追加したため、参照先のない行が残っています。新しい行には外部キーが適用されます。既存の行は自動では削除しません。",
  "settings.database.systemTables.foreignKeys.mismatched":
    "削除規則が正本と異なる外部キーが {count} 件あります。「作成・更新」で外部キーを正本の定義で作り直します。既存の行は削除しません。",
  "settings.database.systemTables.foreignKeys.disabled":
    "無効になっている外部キーが {count} 件あります。「作成・更新」で有効にします。参照先のない既存の行がある外部キーは、既存の行を検査せずに有効にし（新しい行から適用）、既存の行は削除しません。",
  "settings.database.systemTables.foreignKeys.deleteRule": "削除規則 {current} → {expected}",
  "settings.database.systemTables.foreignKeys.orphanedDeleteHint":
    "件数を確認してから「参照先のない行を削除」で削除できます。削除した後に、外部キーで既存の行も検査します。",
  "settings.database.systemTables.foreignKeys.item": "{table} ({columns}) → {referenced}",
  "settings.database.systemTables.foreignKeys.orphanRows": "参照先のない行 {count} 件",
  "settings.database.systemTables.action.deleteOrphans": "参照先のない行を削除",
  "settings.database.systemTables.action.deleteOrphansLabel": "参照先のない行を削除 {name}",
  "settings.database.systemTables.deleteOrphans.confirmTitle": "参照先のない行を {count} 件削除しますか？",
  "settings.database.systemTables.deleteOrphans.confirmDescription":
    "表 {table} の行のうち、外部キー {name}（{columns} → {referenced}）の参照先がない {count} 件を削除し、外部キーで既存の行を検査します。これらの行を ON DELETE CASCADE の外部キーで参照している他の表の行も削除されます。削除した行は復元できません。",
  "settings.database.systemTables.deleteOrphans.done":
    "{table} の参照先のない行を {count} 件削除し、外部キー {name} を検査済みにしました。",
  "settings.database.systemTables.deleteOrphans.noOp":
    "削除する行はありませんでした。外部キー {name} は検査済みです。",
  "settings.database.systemTables.summary.tables": "存在テーブル / 必須テーブル",
  "settings.database.systemTables.summary.objects": "存在オブジェクト / 必須オブジェクト",
  "settings.database.systemTables.summary.objectsHint": "テーブル・索引などの合計",
  "settings.database.systemTables.summary.head": "Migration head",
  "settings.database.systemTables.summary.epoch": "Schema epoch",
  "settings.database.systemTables.action.initialize": "作成・更新",
  "settings.database.systemTables.action.refresh": "最新情報を取得",
  "settings.database.systemTables.action.recreate": "すべて再作成",
  "settings.database.systemTables.refreshed": "最新の状態に更新しました。",
  "settings.database.systemTables.operation.running": "操作中",
  "settings.database.systemTables.operation.no_op": "システムテーブルは最新です。変更はありません。",
  "settings.database.systemTables.operation.initialized": "システムテーブルを初期作成しました。",
  "settings.database.systemTables.operation.migrated": "システムテーブルを更新しました。",
  "settings.database.systemTables.operation.recreated": "システムテーブルをすべて再作成しました。",
  "settings.database.systemTables.restartRequired.title": "{operation}再起動が必要なプロセスがあります",
  "settings.database.systemTables.restartRequired.processes": "再起動するプロセス",
  "settings.database.systemTables.readOnly":
    "状態は参照できます。作成・更新するにはシステムテーブル管理権限が必要です。",
  "settings.database.systemTables.previousFailure": "前回の操作が完了していません",
  "settings.database.systemTables.previousFailureDetail":
    "状態を再取得し、「作成・更新」で再試行してください。",
  "settings.database.systemTables.previousFailureLockDetail":
    "Oracle の対象オブジェクトのロックが待機時間内に解放されませんでした。実行中の処理を完了または停止してから、状態を再取得して再試行してください。",
  "settings.database.systemTables.previousFailureDetails": "詳細",
  "settings.database.systemTables.previousFailureErrorCode": "エラーコード: {code}",
  "settings.database.systemTables.error.operationTitle": "システムテーブル操作に失敗しました",
  "settings.database.systemTables.error.operation": "システムテーブル操作に失敗しました。",
  "settings.database.systemTables.error.recovery":
    "Oracle の接続状態と実行中 job を確認し、状態を再取得してから再試行してください。",
  "settings.database.systemTables.details.title":
    "管理オブジェクトの詳細を表示（存在 {existing} / 必須 {expected}）",
  "settings.database.systemTables.details.versions":
    "適用済みの migration {applied} 件 / 未適用・不一致 {pending} 件",
  "settings.database.systemTables.details.pendingVersions": "未適用・不一致: {versions}",
  "settings.database.systemTables.table.scrollLabel":
    "管理オブジェクト一覧（存在 {existing} / 必須 {expected}）。必要に応じて縦方向または横方向にスクロールできます。",
  "settings.database.systemTables.table.name": "オブジェクト名",
  "settings.database.systemTables.table.type": "種類",
  "settings.database.systemTables.table.objectType.table": "テーブル",
  "settings.database.systemTables.table.objectType.index": "索引",
  "settings.database.systemTables.table.objectType.package": "パッケージ（Package）",
  "settings.database.systemTables.table.objectType.package body": "パッケージ本体（Package Body）",
  "settings.database.systemTables.table.objectType.sequence": "シーケンス",
  "settings.database.systemTables.table.status": "状態",
  "settings.database.systemTables.table.rows": "推定行数",
  "settings.database.systemTables.table.created": "作成日時",
  "settings.database.systemTables.table.analyzed": "最終統計日時",
  "settings.database.systemTables.table.exists": "存在",
  "settings.database.systemTables.table.missing": "不足",
  "settings.database.systemTables.table.notApplicable": "対象外",
  "settings.database.systemTables.recreate.sectionTitle": "破壊的な再作成",
  "settings.database.systemTables.recreate.sectionDescription":
    "管理対象のテーブルを削除して migration を最初から適用します。削除したデータは復元できません。",
  "settings.database.systemTables.confirmation.label": "実行確認語",
  "settings.database.systemTables.confirmation.required": "必須",
  "settings.database.systemTables.confirmation.expected": "入力条件: {phrase}",
  "settings.database.systemTables.confirmation.status.pending": "未入力",
  "settings.database.systemTables.confirmation.status.mismatch": "不一致",
  "settings.database.systemTables.confirmation.status.confirmed": "確認済み",
  "settings.database.systemTables.confirmation.helper":
    "この操作は取り消せません。{phrase} を入力して実行してください。",
} as const;

export type SystemTablesMessageKey = keyof typeof SYSTEM_TABLES_MESSAGES;
export type SystemTablesMessages = Record<SystemTablesMessageKey, string>;

/** object の種類の表示名の key（無い種類は undefined。種類の値をそのまま出す）。 */
export function systemObjectTypeMessageKey(objectType: string): SystemTablesMessageKey | undefined {
  const key = `settings.database.systemTables.table.objectType.${objectType.toLowerCase()}`;
  return Object.prototype.hasOwnProperty.call(SYSTEM_TABLES_MESSAGES, key)
    ? (key as SystemTablesMessageKey)
    : undefined;
}
