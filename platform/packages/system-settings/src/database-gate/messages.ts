/**
 * DB ゲートの既定の文言（NL2SQL の文言が基準。#325）。
 * key は製品の i18n key と同じにして、製品は自分の辞書の値で上書きする（製品名が入る文言など）。
 */
export const DATABASE_GATE_MESSAGES = {
  "common.retry": "再試行",
  "dbGate.checking": "データベースの状態を確認しています…",
  // 未設定（not_configured）
  "dbGate.notConfigured.title": "データベースの接続情報が未設定です",
  "dbGate.notConfigured.message":
    "この画面を利用するには、まずデータベースの接続情報を設定してください。設定が完了すると、この画面は自動的に利用できるようになります。",
  "dbGate.notConfigured.contactAdmin":
    "この画面を利用するには、データベースの接続情報の設定が必要です。システム管理者に連絡して、設定を依頼してください。",
  // 接続できない（unreachable）。ADB の状態が分かるときは、停止中・起動中・利用できない・起動済みを分ける（#820）。
  "dbGate.unreachable.title": "データベースに接続できません",
  "dbGate.unreachable.message":
    "データベースが停止しているか、ネットワーク経由で到達できません。データベース設定で起動状態と接続情報を確認してから、再試行してください。",
  "dbGate.unreachable.contactAdmin":
    "データベースが停止しているか、ネットワーク経由で到達できません。システム管理者に連絡して、データベースの起動と接続の確認を依頼してください。",
  "dbGate.adbStopped.title": "Autonomous Database が停止しています",
  "dbGate.adbStopped.message":
    "データベース設定の「Autonomous Database 管理」で起動してください。起動の完了には数分かかります。完了してから再試行してください。",
  "dbGate.adbStopped.contactAdmin":
    "システム管理者に連絡して、Autonomous Database の起動を依頼してください。起動が完了したら再試行してください。",
  "dbGate.adbStarting.title": "Autonomous Database を起動しています",
  "dbGate.adbStarting.message": "起動の完了には数分かかります。状態が「起動済み」になってから再試行してください。",
  "dbGate.adbStarting.contactAdmin":
    "起動の完了には数分かかります。しばらく待ってから再試行してください。解消しない場合は、システム管理者に連絡してください。",
  "dbGate.adbUnavailable.title": "Autonomous Database を利用できない状態です",
  "dbGate.adbUnavailable.message":
    "Autonomous Database の状態が「起動済み」に戻ってから再試行してください。状態はデータベース設定で確認できます。",
  "dbGate.adbUnavailable.contactAdmin":
    "Autonomous Database の状態が戻るまで利用できません。解消しない場合は、システム管理者に連絡してください。",
  "dbGate.adbAvailable.message":
    "Autonomous Database は起動していますが、接続できません。データベース設定で接続情報（サービス名・Wallet）とネットワークを確認してから、再試行してください。",
  "dbGate.adbAvailable.contactAdmin":
    "Autonomous Database は起動していますが、接続できません。システム管理者に連絡して、接続情報とネットワークの確認を依頼してください。",
  "dbGate.adbState": "Autonomous Database: {state}",
  // 初期化が必要（setup_required）
  "dbGate.setupRequired.title": "システムテーブルの作成・更新が必要です",
  "dbGate.setupRequired.message":
    "データベースには接続できています。システムテーブルが作成されていないか、未適用の更新があります。システムテーブルの画面で作成・更新してから、再試行してください。",
  "dbGate.setupRequired.contactAdmin":
    "データベースには接続できていますが、システムテーブルの作成・更新が済んでいません。システム管理者に連絡して、システムテーブルの作成・更新を依頼してください。",
  // 状態を確認できない（check_failed。状態 API 自体の失敗）
  "dbGate.checkFailed.title": "データベースの状態を確認できません",
  "dbGate.checkFailed.message": "バックエンドの起動状態を確認して再試行してください。",
  "dbGate.checkFailed.contactAdmin":
    "しばらく待ってから再試行してください。解消しない場合は、システム管理者に連絡してください。",
  "dbGate.openDatabaseSettings": "データベース設定を開く",
  "dbGate.openSystemTables": "システムテーブルを開く",
  "dbGate.settingsHint":
    "OCI 認証・アップロード保存先・モデル・データベース・外観と証明書の各設定ページは引き続き利用できます。",
  "dbGate.setupRequired.settingsHint":
    "OCI 認証・アップロード保存先・モデル・データベース・外観と証明書の各設定ページは引き続き利用できます。",
  "dbGate.contactAdmin.footer":
    "データベースの起動・接続の設定とシステムテーブルの作成・更新は、システム管理者が行います。",
  "dbGate.reasonCode": "診断コード: {code}",
  // 診断コード（`check`）ごとの補足。無いコードは診断コードだけを出す。
  "dbGate.check.missing": "接続ユーザーまたはサービス名（DSN）が設定されていません。",
  "dbGate.check.missing_credentials": "DB パスワードが設定されていません。",
  "dbGate.check.wallet_not_found":
    "Wallet の保存先に接続用のファイルがそろっていません。Wallet をアップロードし直してください。",
  "dbGate.check.wallet_password_invalid":
    "暗号化された Wallet を現在の Wallet パスワードで復号できません。Wallet パスワードを確認してください。",
  "dbGate.check.invalid":
    "サービス名（DSN）が現在の Wallet の tnsnames.ora にありません。サービス名を選び直してください。",
  "dbGate.check.walletless_tls_dsn_required":
    "Walletless TLS では、Wallet のサービス名ではなく ADB の TCPS 接続文字列または Easy Connect の DSN を指定してください。",
  "dbGate.check.invalid_configuration":
    "接続設定の組み合わせがこの製品で使えません。バックエンドの設定を確認してください。",
  "dbGate.check.schema_check_failed":
    "システムテーブルの状態を確認できませんでした。再試行しても解消しない場合は、バックエンドのログを確認してください。",
  "dbGate.check.migration_required": "システムテーブルの作成・更新（migration の適用）が必要です。",
  "dbGate.check.migration_check_failed":
    "システムテーブルの状態を確認できませんでした。再試行しても解消しない場合は、バックエンドのログを確認してください。",
} as const;

export type DatabaseGateMessageKey = keyof typeof DATABASE_GATE_MESSAGES;
export type DatabaseGateMessages = Record<DatabaseGateMessageKey, string>;

/** 診断コードの補足の key（無いコードは undefined）。 */
export function databaseCheckMessageKey(code: string): DatabaseGateMessageKey | undefined {
  const key = `dbGate.check.${code}`;
  return Object.prototype.hasOwnProperty.call(DATABASE_GATE_MESSAGES, key)
    ? (key as DatabaseGateMessageKey)
    : undefined;
}
