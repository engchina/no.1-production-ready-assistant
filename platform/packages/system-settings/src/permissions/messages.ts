/**
 * 権限管理（ロールごとの機能権限と利用できる対象）の既定の文言（NL2SQL の文言を基準に移設。#220）。
 * 製品は `messages` prop で一部だけ上書きできる。`{name}` は差し込み。
 * 利用できる対象（業務プロファイル・検索・回答プロファイルなど）の文言は、対象ごとに `targets[].messages` で渡す。
 */
export const ROLE_PERMISSIONS_MESSAGES = {
  title: "権限管理",
  subtitle:
    "ロールごとに、使える画面（機能権限）と利用できる対象を設定します。ロールの作成・名称変更・アーカイブはロール管理で行います。",
  actionsLabel: "権限管理表示切替",
  workspaceLabel: "ロール別の権限一覧と詳細作業領域",
  taskPanelLabel: "権限管理タスク",
  listTitle: "ロール一覧",
  listHint: "ロールを選ぶと、付与している機能権限の件数と利用できる対象を確認できます。",
  // placeholder は一覧の列（既定 20rem）の検索欄に収める（#1891 / #2282）。
  searchPlaceholder: "ロール・権限・対象で絞り込み",
  pagination: "ロール一覧のページ",
  noSelectionTitle: "ロールを選択してください",
  noSelectionHint: "一覧のロールを選ぶと、付与している権限を確認できます。",
  // 一覧 + 詳細の詳細の列の名前と、URL のロールが見つからないとき（#2282）。
  detailTitle: "ロールの権限",
  notFoundTitle: "ロールが見つかりません",
  notFoundHint: "削除されたか、URL が正しくありません。一覧から選び直してください。",
  noResultsTitle: "条件に一致するロールがありません",
  noResultsHint: "検索語を変更してください。",
  showRole: "{name} を表示",
  columnRole: "ロール",
  status: "状態",
  roleCode: "ロールコード",
  roleName: "ロール名",
  edit: "権限を編集",
  editActions: "権限編集操作",
  formTitle: "{role} の権限を編集",
  formHint:
    "機能権限と利用できる対象を設定します。保存すると、このロールのユーザーへ次回リクエストから反映されます。",
  permissions: "機能権限",
  permissionsHint:
    "メニュー権限は画面表示を許可します。一部の権限は、必要な参照権限を自動的に付与します。",
  permissionCount: "{count} 件",
  allPermissions: "すべての機能権限",
  permissionInherited: "{source}により付与",
  systemAdminNotice: "SYSTEM_ADMIN は現在および将来の全機能権限を自動的に持つ組み込みロールです。",
  archivedPermissionNotice:
    "このロールはアーカイブ済みです。保存済みの権限は利用者の実アクセス権には反映されません。",
  refresh: "最新情報を取得",
  refreshed: "最新の状態に更新しました。",
  loading: "権限を読込中",
  loadError: "読み込みに失敗しました。接続状態と権限を確認して再試行してください。",
  saveError: "保存に失敗しました。入力内容を確認して再試行してください。",
  saved: "変更を保存しました。",
  discardTitle: "変更を破棄しますか",
  discardDescription: "保存されていない変更があります。移動すると編集内容は破棄されます。",
  discardConfirm: "破棄して移動",
  backToList: "一覧へ戻る",
  save: "保存",
  discardChanges: "変更を破棄",
  formActionsLabel: "権限の確定",
  cancel: "キャンセル",
  actions: "操作",
  search: "検索",
  empty: "対象データはありません。",
  listScrollLabel: "{list}。必要に応じて縦方向または横方向にスクロールできます。",
  selectAll: "すべて選択",
  clearAll: "選択をすべて解除",
  selectGroup: "{name} をすべて選択",
  clearGroup: "{name} の選択をすべて解除",
};

export type RolePermissionsMessages = typeof ROLE_PERMISSIONS_MESSAGES;

/** 利用できる対象 1 種類分の文言。製品が対象ごとに渡す（既定値は無い）。 */
export interface RolePermissionTargetMessages {
  /** 見出し。一覧の列名・詳細の項目名・編集の legend にも使う（例: 使用可能な業務プロファイル）。 */
  title: string;
  /** 編集画面の説明。 */
  hint?: string;
  /** 全件が対象のときの表示（例: すべての業務プロファイル）。 */
  all: string;
  /** 件数の表示。既定は「{count} 件」。 */
  count?: string;
  searchLabel: string;
  searchPlaceholder: string;
  /** 候補が 0 件のとき。 */
  empty: string;
  /** 検索で 0 件のとき。 */
  noResults: string;
  /** 候補の読み込みに失敗したときの警告（`{message}` に原因）。ロール一覧は表示を続ける。 */
  loadWarning: string;
  /** 有効な権限により全件が対象になるときの説明。 */
  grantsAllByPermission: string;
  /** SYSTEM_ADMIN のときの説明。 */
  grantsAllSystemAdmin: string;
}
