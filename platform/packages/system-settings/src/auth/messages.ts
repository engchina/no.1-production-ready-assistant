/**
 * ログイン・パスワード変更・権限なし画面とサイドバーのアカウント欄の既定の文言（NL2SQL の文言を基準に移設。#220）。
 * 製品は `messages` prop で一部だけ上書きできる。`{name}` は差し込み。
 */
export const AUTH_MESSAGES = {
  loading: "認証状態を確認しています。",
  sessionCheckError: "ログインの状態を確認できませんでした。",
  sessionCheckErrorFallback: "サーバーに接続できないか、一時的に利用できません。少し待ってから再試行してください。",
  required: "必須",
  requestId: "リクエストID",
  loginTitle: "システムにログイン",
  loginSubtitle: "登録済みのアプリケーションユーザーで認証します。",
  loginUserId: "ログインユーザーID",
  loginPassword: "パスワード",
  loginSubmit: "ログイン",
  loginError: "ログインできませんでした。入力内容を確認して再試行してください。",
  loginRequired: "ログインユーザーIDとパスワードを入力してください。",
  passwordTitle: "パスワードの変更",
  passwordSubtitle: "初回利用または管理者によるリセット後は、パスワード変更が必要です。",
  passwordCurrent: "現在のパスワード",
  passwordNew: "新しいパスワード",
  passwordConfirm: "新しいパスワード（確認）",
  passwordRule: "12～128文字で、大文字・小文字・数字・記号をすべて含めてください。",
  passwordRequired: "現在のパスワードと新しいパスワード（確認を含む）を入力してください。",
  passwordMismatch: "新しいパスワードが一致しません。再入力してください。",
  passwordChanged: "パスワードを変更しました。新しいパスワードでログインしてください。",
  passwordSaveError: "保存に失敗しました。入力内容を確認して再試行してください。",
  passwordSubmit: "パスワードを変更",
  passwordBack: "戻る",
  passwordBackToLogin: "ログインへ戻る",
  passwordNotAllowedSubtitle: "このログイン方式ではパスワード変更を利用できません。",
  passwordNotAllowed: "このアカウントのパスワードはアプリケーション内では変更できません。",
  forbiddenTitle: "この機能を利用する権限がありません",
  forbiddenDescription: "必要なロールが付与されているか、システム管理者に確認してください。",
  forbiddenBack: "利用可能な画面へ戻る",
  /** アカウントの欄（名前・ロール）のリンクの読み上げ名（#1873）。 */
  sidebarOpenAccount: "アカウント設定を開く（{name}）",
  // アカウントの画面（#1873）。サイドナビのアカウントの欄から開き、タブで API キーとパスワード変更を切り替える。
  accountTitle: "アカウント",
  accountSubtitle: "{name}（ロール: {roles}）",
  accountTabsLabel: "アカウントの設定",
  accountTabApiKeys: "API キー",
  accountTabPassword: "パスワード変更",
  accountPasswordDescription: "新しいパスワードを設定します。変更すると、新しいパスワードでログインし直します。",
  accountPasswordLocal: "ローカルの認証（ログインの省略）ではパスワードを使いません。",
  // ログインの画面の「パスワードを忘れた場合」（#1873。メールの送信は無い）。
  loginForgotPassword: "パスワードを忘れた場合",
  loginForgotPasswordHelp:
    "管理者にパスワードのリセット（一時パスワードの発行）を依頼してください。一時パスワードでログインすると、新しいパスワードの設定に進みます。",
  sidebarLogout: "ログアウト",
  /** アカウント欄のロールの行の全文（title と読み上げ）。見える行はロールの表示名だけ（#1644）。 */
  sidebarRoles: "ロール: {roles}",
  /** 複数のロールの表示名の区切り。 */
  sidebarRoleSeparator: "、",
  sidebarDebugMode: "ログイン省略",
};

export type AuthMessages = typeof AUTH_MESSAGES;

/** `{name}` を params の値で置き換える。 */
export function formatMessage(template: string, params?: Record<string, string | number>): string {
  if (!params) return template;
  let value = template;
  for (const [name, replacement] of Object.entries(params)) {
    value = value.replace(new RegExp(`\\{${name}\\}`, "g"), String(replacement));
  }
  return value;
}
