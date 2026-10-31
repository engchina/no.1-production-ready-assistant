/**
 * ユーザー管理・ロール管理の既定の文言（NL2SQL の文言を基準に移設。#206）。
 * key は NL2SQL の i18n key と同じにして、移設したコードをそのまま使えるようにしている。
 * ロールに付ける権限は製品ごとに違うため、ロール管理の文言は権限の種類に触れない。
 */
export const USERS_ROLES_MESSAGES = {
  "common.action.copied": "コピーしました",
  "common.action.refresh": "最新情報を取得",
  "common.action.refreshed": "最新の状態に更新しました。",
  "common.delete": "削除",
  "common.retry": "再試行",
  "nav.securityRoles": "ロール管理",
  "nav.securityUsers": "ユーザー管理",
  "security.common.actions": "操作",
  "security.common.active": "有効",
  "security.common.backToList": "一覧へ戻る",
  "security.common.cancel": "キャンセル",
  "security.common.clearSearch": "検索語をクリア",
  // 一覧の「新規作成」（作成の画面を開く）。作成の画面の確定は submitCreate の「作成」（#1786）。
  "security.common.create": "新規作成",
  "security.common.disabled": "無効",
  "security.common.discardChanges": "変更を破棄",
  "security.common.discardConfirm": "破棄して移動",
  "security.common.discardDescription":
    "保存されていない変更があります。移動すると編集内容は破棄されます。",
  "security.common.discardTitle": "変更を破棄しますか",
  "security.common.edit": "編集",
  "security.common.empty": "対象データはありません。",
  "security.common.filteredCount": "{filtered} / {total} 件",
  "security.common.filteredCountWithSelected": "{filtered} / {total} 件、選択 {selected} 件",
  "security.common.listScrollLabel":
    "{list}。必要に応じて縦方向または横方向にスクロールできます。",
  // 一覧 + 詳細（ListDetailLayout）の一覧の列の幅の分割線（#2282）。
  "security.common.listWidth": "一覧の幅",
  "security.common.listWidthHint":
    "ドラッグ・左右の矢印キーで一覧の幅を変更・ダブルクリックか Home で既定の幅に戻す",
  "security.common.loadError":
    "読み込みに失敗しました。接続状態と権限を確認して再試行してください。",
  "security.users.loading": "ユーザーを読込中",
  "security.roles.loading": "ロールを読込中",
  // 一覧 + 詳細の詳細の上端の「前へ / 次へ」（#2282）。
  "security.common.next": "次へ",
  "security.common.no": "いいえ",
  "security.common.none": "なし",
  "security.common.previous": "前へ",
  "security.common.save": "保存",
  "security.common.saveError": "保存に失敗しました。入力内容を確認して再試行してください。",
  "security.common.saved": "変更を保存しました。",
  "security.common.search": "検索",
  "security.common.submitCreate": "作成",
  "security.common.status": "状態",
  "security.common.version": "バージョン",
  "security.common.yes": "はい",
  "security.roles.actionsLabel": "ロール管理表示切替",
  "security.roles.archive": "アーカイブ",
  "security.roles.archiveConfirm":
    "このロールをアーカイブすると、このロール由来の権限は利用者に反映されなくなります。ユーザー自体は無効化されません。",
  // 一覧の列（狭い）の状態のバッジ（#2282）。詳細は archivedDisabled（権限が無効なことまで）を出す。
  "security.roles.archived": "アーカイブ済み",
  "security.roles.archivedDisabled": "アーカイブ済み・権限無効",
  "security.roles.archivedPermissionNotice":
    "このロールはアーカイブ済みです。保存済みの権限は利用者の実アクセス権には反映されません。",
  "security.roles.builtIn": "組み込み",
  "security.roles.code": "ロールコード",
  // 削除済み等で参照できないロール。ID（UUID）は出さない（#1682）。
  "security.roles.deletedRole": "（削除済みのロール）",
  "security.roles.codeConflict":
    "このロールコードは既に使用されています。別のコードを入力してください。",
  "security.roles.codeInvalid":
    "ロールコードは英大文字で始め、英大文字・数字・アンダースコアで入力してください。",
  "security.roles.codeRequired": "ロールコードを入力してください。",
  "security.roles.codeTooShort": "ロールコードは 2 文字以上で入力してください。",
  "security.roles.codeReserved":
    "SYSTEM_ADMIN は組み込みロール専用のコードです。別のロールコードを入力してください。",
  "security.roles.column.role": "ロール",
  "security.roles.custom": "カスタム",
  "security.roles.delete": "削除",
  "security.roles.deleteConfirm":
    "「{name}」を完全に削除します。ロールに設定した権限も削除され、元に戻せません。",
  "security.roles.deleteError":
    "ロールを削除できませんでした。割り当てユーザーを解除し、最新の表示で再試行してください。",
  "security.roles.deleteSuccess": "{name} を完全に削除しました。",
  "security.roles.description": "説明",
  "security.roles.detail": "ロールの詳細",
  "security.roles.editActions": "ロール編集操作",
  "security.roles.form.create": "ロールを作成",
  "security.roles.form.edit": "ロールを編集",
  "security.roles.formActionsLabel": "ロールの確定",
  "security.roles.formHint":
    "ロールコード・ロール名・説明を管理します。ロールに付ける権限は権限管理で設定します。",
  "security.roles.list": "ロール一覧",
  "security.roles.listHint": "検索・並び替え・行アクションから対象ロールを確認できます。",
  "security.roles.name": "ロール名",
  "security.roles.nameRequired": "ロール名を入力してください。",
  "security.roles.noResultsHint": "検索語を変更してください。",
  "security.roles.noResultsTitle": "条件に一致するロールがありません",
  "security.roles.pagination": "ロール一覧のページ",
  "security.roles.noSelectionHint": "一覧のロールを選ぶと、状態や基本情報を確認できます。",
  "security.roles.notFoundHint": "削除されたか、URL が正しくありません。一覧から選び直してください。",
  "security.roles.notFoundTitle": "ロールが見つかりません",
  "security.roles.noSelectionTitle": "ロールを選択してください",
  "security.roles.openPermissions": "権限管理で設定",
  "security.roles.permissionSummary": "付与している機能権限: {count} 件",
  "security.roles.permissionSummarySystemAdmin": "SYSTEM_ADMIN はすべての機能権限を持ちます。",
  "security.roles.restore": "復元",
  "security.roles.restoreConfirm":
    "このロールを復元すると、このロールに紐づくユーザーへ、このロール由来の権限が次回リクエストから反映されます。",
  "security.roles.searchPlaceholder": "ロール名・説明で絞り込み",
  "security.roles.showRole": "{name} を表示",
  "security.roles.subtitle":
    "ロールの作成・名称・説明・アーカイブを管理します。ロールに付ける権限は権限管理で設定します。",
  "security.roles.systemAdminNotice":
    "SYSTEM_ADMIN は現在および将来の全機能権限を自動的に持つ組み込みロールです。",
  "security.roles.taskPanelLabel": "ロール管理タスク",
  "security.roles.workspaceLabel": "ロール一覧と詳細作業領域",
  "security.users.actionsLabel": "ユーザー管理表示切替",
  "security.users.archivedRoleLabel": "{role}（アーカイブ済み・無効）",
  "security.users.archivedRoleNotice":
    "アーカイブ済みロールの権限は、このユーザーの実アクセス権には反映されません。",
  "security.users.column.user": "ユーザー",
  "security.users.createActions": "ユーザー作成操作",
  "security.users.delete": "削除",
  // 削除済み・取得できない利用者。UUID は出さない（#1696）。
  "security.users.deletedUser": "（削除済みのユーザー）",
  "security.users.deleteConfirm":
    "「{name}」を完全に削除します。ユーザー本体、割り当てロール、既存セッションは削除され、元に戻せません。",
  "security.users.deleteError":
    "ユーザーを削除できませんでした。無効状態、保護対象、最新バージョンを確認して再試行してください。",
  "security.users.deleteSuccess": "{name} を完全に削除しました。",
  "security.users.disable": "無効化",
  "security.users.disableConfirm":
    "このユーザーを無効化し、既存セッションを失効させます。続行しますか。",
  "security.users.disableSuccess": "ユーザーを無効化しました。既存セッションは失効しました。",
  "security.users.displayName": "表示名",
  "security.users.detail": "ユーザーの詳細",
  "security.users.displayNameRequired": "表示名を入力してください。",
  "security.users.editActions": "ユーザー編集操作",
  "security.users.enable": "有効化",
  "security.users.enableSuccess": "ユーザーを有効化しました。次回ログインから利用できます。",
  "security.users.forceChange": "次回ログイン時に変更必須",
  "security.users.form.create": "ユーザーを作成",
  "security.users.form.edit": "ユーザーを編集",
  "security.users.formActionsLabel": "ユーザーの確定",
  "security.users.formHint": "ログインユーザーID・表示名・割り当てロールを管理します。",
  "security.users.list": "ユーザー一覧",
  "security.users.listHint": "検索・並び替え・行アクションから対象ユーザーを確認できます。",
  "security.users.locked": "ロック中",
  "security.users.loginUserId": "ログインユーザーID",
  "security.users.loginUserIdRequired": "ログインユーザーIDを入力してください。",
  "security.users.loginUserIdConflict":
    "このログインユーザーIDは既に使用されています。別のIDを入力してください。",
  "security.users.noResultsHint": "検索語を変更してください。",
  "security.users.noResultsTitle": "条件に一致するユーザーがありません",
  "security.users.pagination": "ユーザー一覧のページ",
  "security.users.noRole": "利用可能なロールがありません。先にロールを作成してください。",
  "security.users.otherRoles": "ほかに割り当てられているロール（保存しても変わりません）: {roles}",
  "security.users.noSelectionHint":
    "一覧のユーザーを選ぶと、状態と割り当てロールを確認できます。",
  "security.users.noSelectionTitle": "ユーザーを選択してください",
  "security.users.notFoundHint": "削除されたか、URL が正しくありません。一覧から選び直してください。",
  "security.users.notFoundTitle": "ユーザーが見つかりません",
  "security.users.oneTimePassword.copy": "一時パスワードをコピー",
  "security.users.oneTimePassword.copyError":
    "一時パスワードをコピーできませんでした。ブラウザのクリップボード権限を確認するか、表示内容を手動で選択してください。",
  "security.users.oneTimePassword.resetError":
    "一時パスワードを再発行できませんでした。接続状態と権限を確認して再試行してください。",
  "security.users.oneTimePassword.resetSuccess": "パスワードをリセットしました。",
  "security.users.oneTimePassword.valueLabel": "一時パスワード",
  "security.users.resetConfirm":
    "一時パスワードを再発行し、既存セッションを失効させます。続行しますか。",
  "security.users.resetPassword": "パスワードをリセット",
  "security.users.roleRequired": "ロールを選択してください。",
  "security.users.roles": "割り当てるロール",
  "security.users.searchPlaceholder": "名前・ID・ロールで絞り込み",
  "security.users.showUser": "{name} を表示",
  "security.users.subtitle": "ローカルユーザー、割り当てロール、ロック状態を管理します。",
  "security.users.systemAdminConfigOnly":
    "システム管理者のロールは構成管理者（system_admin）だけが持つため、ここでは割り当てられません。",
  "security.users.taskPanelLabel": "ユーザー管理タスク",
  "security.users.tempPassword": "一時パスワード（空欄の場合は自動生成）",
  "security.users.unlock": "ロック解除",
  "security.users.workspaceLabel": "ユーザー一覧と詳細作業領域",
} as const;

export type UsersRolesMessageKey = keyof typeof USERS_ROLES_MESSAGES;

/** `{name}` を params の値で置き換える。 */
export function t(
  key: UsersRolesMessageKey,
  params?: Record<string, string | number>,
): string {
  let value: string = USERS_ROLES_MESSAGES[key] ?? key;
  if (params) {
    for (const [name, replacement] of Object.entries(params)) {
      value = value.replace(new RegExp(`\\{${name}\\}`, "g"), String(replacement));
    }
  }
  return value;
}
