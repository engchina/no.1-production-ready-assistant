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
  "settings.database.cardDescription":
    "アプリが Oracle AI Database へ接続するユーザー・パスワード・DSN を設定します。",
  "settings.database.cardTitle": "データベース設定",
  "settings.database.field.dbPassword": "データベースパスワード",
  "settings.database.field.dbUser": "データベースユーザー",
  "settings.database.field.dsn": "DSN",
  "settings.database.helper.dsn":
    "host:port/service_name の形式か接続記述子を入力します。例: 127.0.0.1:1525/myatp_low.adb.oraclecloud.com",
  "settings.database.helper.passwordRequired":
    "Oracle Database の接続パスワードを入力します。",
  "settings.database.helper.passwordSavedCompact":
    "空欄のまま保存すると既存のパスワードを保持します。",
  "settings.database.hint": "DB設定は `.env` に保存されます。",
  "settings.database.loadError": "データベース設定の取得に失敗しました。",
  "settings.database.loading": "データベース設定を読み込んでいます。",
  "settings.database.placeholder.dbUser": "admin",
  "settings.database.placeholder.dsn": "host:port/service_name",
  "settings.database.placeholder.password": "******",
  "settings.database.placeholder.passwordSaved": "********",
  "settings.database.saveError":
    "データベース設定の保存に失敗しました。入力値と platform/.env の書き込み権限を確認してください。",
  "settings.database.secrets.clearPassword": "保存済みパスワードを削除する",
  "settings.database.secrets.hide": "DB パスワードを隠す",
  "settings.database.secrets.notSet": "未設定",
  "settings.database.secrets.revealError":
    "保存済み DB パスワードの取得に失敗しました。入力値または保存状態を確認してください。",
  "settings.database.secrets.revealingPassword": "DB パスワードを取得中",
  "settings.database.secrets.saved": "保存済み",
  "settings.database.secrets.show": "DB パスワードを表示",
  "settings.database.test.apiError":
    "{message} バックエンドとデータベースの起動状態を確認して再試行してください。",
  "settings.database.test.apiFailed":
    "DB 接続テスト API の呼び出しに失敗しました。バックエンドとデータベースの起動状態を確認して再試行してください。",
  "settings.database.test.running": "データベースへの接続を確認しています",
  "settings.database.validation.passwordRequired":
    "DB設定を保存するにはデータベースパスワードを入力してください。",
  "settings.database.validation.dsnRequired": "DSN を入力してください。",
  "settings.database.validation.userRequired": "データベースユーザーを入力してください。",
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
