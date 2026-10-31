/**
 * データベース設定画面の既定の文言（NL2SQL の文言。#108）。
 * key は NL2SQL の i18n key と同じにして、移設したコードをそのまま使えるようにしている。
 */
export const DATABASE_MESSAGES = {
  "settings.database.actions.save": "保存",
  "settings.database.actions.label": "データベース設定の操作",
  "settings.database.actions.saveDb": "DB設定を保存",
  "settings.database.actions.saved": "保存しました",
  "settings.database.actions.testDb": "DB接続テスト",
  "settings.database.actions.uploadingWallet": "アップロード中…",
  "settings.database.actions.walletUploaded":
    "Wallet ZIP をアップロードしました: {fileName}",
  "settings.database.cardDescription":
    "アプリが Oracle AI Database へ接続するユーザー・パスワード・Wallet・サービス名を設定します。",
  "settings.database.cardTitle": "データベース設定",
  "settings.database.connectionSecurity.walletMtlS": "Wallet mTLS",
  "settings.database.connectionSecurity.walletMtlS.description":
    "mTLS 必須 ADB / private endpoint",
  "settings.database.connectionSecurity.wallet_mtls.helper":
    "Thin mode でも mTLS 必須 ADB へ接続できます。Wallet ZIP と Wallet サービス名を使用します。",
  "settings.database.connectionSecurity.walletlessTls": "Walletless TLS",
  "settings.database.connectionSecurity.walletlessTls.description":
    "mTLS 不要 ADB の一方向 TLS",
  "settings.database.connectionSecurity.walletless_tls.helper":
    "mTLS が不要な ADB だけで使用します。Wallet は渡さず、ACL で許可された接続元から直接 DSN へ接続します。",
  "settings.database.field.connectionSecurity": "接続セキュリティ",
  "settings.database.field.dbPassword": "データベースパスワード",
  "settings.database.field.dbUser": "データベースユーザー",
  "settings.database.field.directDsn": "接続 DSN",
  "settings.database.field.serviceDsn": "サービス名 / DSN",
  "settings.database.field.walletPassword": "Wallet パスワード",
  "settings.database.helper.directDsn":
    "Walletless TLS では ADB の TCPS 接続文字列または host:port/service_name 形式を入力します。",
  "settings.database.helper.dsnService":
    "Wallet の tnsnames.ora から検出したサービス名を選択します。",
  "settings.database.helper.dsnServiceManual":
    "Wallet のサービス名を入力します。Wallet ZIP をアップロードすると候補から選べます。",
  "settings.database.helper.passwordRequired":
    "Oracle Database の接続パスワードを入力します。",
  "settings.database.helper.passwordSavedCompact":
    "空欄のまま保存すると既存のパスワードを保持します。",
  "settings.database.helper.walletPasswordEmpty":
    "暗号化 Wallet を使う場合のみ入力します。",
  "settings.database.helper.walletPasswordSaved":
    "保存済み Wallet パスワードがあります。空欄のまま保存すると既存値を保持し、削除する場合は下のチェックボックスをオンにします。",
  "settings.database.hint":
    "DB設定は `.env` の接続文字列に保存されます。Wallet と DSN が一致しているか確認してください。",
  "settings.database.loadError": "データベース設定の取得に失敗しました。",
  "settings.database.loading": "データベース設定を読み込んでいます。",
  "settings.database.placeholder.dbUser": "admin",
  "settings.database.placeholder.directDsn":
    "adb.example.oraclecloud.com:1522/service_name",
  "settings.database.placeholder.password": "******",
  "settings.database.placeholder.passwordSaved": "********",
  "settings.database.placeholder.secret": "更新する場合のみ入力",
  "settings.database.placeholder.serviceDsn": "DSN を選択してください",
  "settings.database.placeholder.serviceDsnManual": "ragdb_high",
  "settings.database.saveError":
    "データベース設定の保存に失敗しました。入力値と platform/.env の書き込み権限を確認してください。",
  "settings.database.secrets.clearPassword": "保存済みパスワードを削除する",
  "settings.database.secrets.clearWalletPassword":
    "保存済み Wallet パスワードを削除する",
  "settings.database.secrets.hide": "DB パスワードを隠す",
  "settings.database.secrets.hideWalletPassword": "Wallet パスワードを隠す",
  "settings.database.secrets.notSet": "未設定",
  "settings.database.secrets.revealError":
    "保存済み DB パスワードの取得に失敗しました。入力値または保存状態を確認してください。",
  "settings.database.secrets.revealingPassword": "DB パスワードを取得中",
  "settings.database.secrets.revealingWalletPassword":
    "Wallet パスワードを取得中",
  "settings.database.secrets.saved": "保存済み",
  "settings.database.secrets.show": "DB パスワードを表示",
  "settings.database.secrets.showWalletPassword": "Wallet パスワードを表示",
  "settings.database.test.apiError":
    "{message} バックエンドとデータベースの起動状態を確認して再試行してください。",
  "settings.database.test.apiFailed":
    "DB 接続テスト API の呼び出しに失敗しました。バックエンドとデータベースの起動状態を確認して再試行してください。",
  "settings.database.test.running": "データベースへの接続を確認しています",
  "settings.database.validation.invalidWalletZip":
    "ZIP 形式の Wallet ファイルを選択してください。",
  "settings.database.validation.passwordRequired":
    "DB設定を保存するにはデータベースパスワードを入力してください。",
  "settings.database.validation.directDsnRequired": "接続 DSN を入力してください。",
  "settings.database.validation.serviceDsnRequired": "サービス名 / DSN を入力してください。",
  "settings.database.validation.serviceDsnSelectRequired": "サービス名 / DSN を選択してください。",
  "settings.database.validation.userRequired": "データベースユーザーを入力してください。",
  "settings.database.wallet.autoDownload.error":
    "OCI から Wallet を取得できませんでした。OCI 認証、ADB OCID、IAM 権限を確認して再試行するか、Wallet ZIP を手動アップロードしてください。",
  "settings.database.wallet.autoDownload.missingOcid":
    "ADB OCID が未設定のため自動取得は行いません。platform/.env に ADB OCID を設定するか、下の領域から Wallet ZIP を手動アップロードしてください。",
  "settings.database.wallet.autoDownload.pending":
    "OCI から Wallet を取得し、サーバーへ安全に設定しています…",
  "settings.database.wallet.autoDownload.retry": "OCI から再取得",
  "settings.database.wallet.autoDownload.retryAria": "OCI から Wallet を再取得",
  "settings.database.wallet.autoDownload.success":
    "Oracle Wallet を OCI から取得し、サーバーへ設定しました。",
  "settings.database.wallet.help":
    "Oracle Autonomous Database の Wallet ZIP をアップロードしてください。",
  "settings.database.wallet.location": "Wallet保存先",
  "settings.database.wallet.replaceCta": "Wallet ZIP を差し替える",
  "settings.database.wallet.status": "Wallet状態",
  "settings.database.wallet.statusConfigured": "設定済み",
  "settings.database.wallet.statusNotConfigured": "未設定",
  "settings.database.wallet.title": "Wallet（ZIP）",
  "settings.database.walletInput.aria": "Wallet ZIP ファイルを選択",
  "settings.database.walletUploadError":
    "Wallet ZIP のアップロードに失敗しました。ZIP 形式とファイルサイズを確認してください。",
  "settings.database.walletlessTls.walletSkipped":
    "Walletless TLS では Wallet を接続 kwargs に渡しません。mTLS 必須 ADB では Wallet mTLS を選択してください。",
} as const;

export type DatabaseMessageKey = keyof typeof DATABASE_MESSAGES;

/** `{name}` を params の値で置き換える。 */
export function t(
  key: DatabaseMessageKey,
  params?: Record<string, string | number>,
): string {
  let value: string = DATABASE_MESSAGES[key] ?? key;
  if (params) {
    for (const [name, replacement] of Object.entries(params)) {
      value = value.replace(
        new RegExp(`\\{${name}\\}`, "g"),
        String(replacement),
      );
    }
  }
  return value;
}
