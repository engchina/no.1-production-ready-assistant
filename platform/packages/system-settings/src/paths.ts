import {
  Waypoints,
  BrainCog,
  Database,
  HardDrive,
  KeySquare,
  MessageSquareWarning,
  Paperclip,
  Shield,
  Users,
  type LucideIcon,
} from "lucide-react";

/**
 * 共有システム設定画面のパス。3製品で同じ URL にそろえる（#70）。
 * 各製品の lib/routes.ts はこの値を参照する。
 */
export const SYSTEM_SETTINGS_PATHS = {
  uploadStorage: "/settings/upload-storage",
  model: "/settings/model",
  database: "/settings/database",
} as const;

export type SystemSettingsKey = keyof typeof SYSTEM_SETTINGS_PATHS;

export interface SystemSettingsNavItem {
  key: SystemSettingsKey;
  href: string;
  /** 製品の i18n の key（3製品で同じ key を持つ。文言は製品側で上書きできる）。 */
  labelKey: string;
  sidebarLabelKey?: string;
  icon: LucideIcon;
}

/**
 * サイドナビ「システム設定」の共通3項目（#116。OCI 認証は #11、「外観と証明書」は #19 で削除）。項目・並び順・アイコンは共有パッケージが決め、
 * 各製品の nav config はこれをそのまま使う（NL2SQL は各項目に menu 権限を付ける）。
 */
export const SYSTEM_SETTINGS_NAV_ITEMS = [
  {
    key: "uploadStorage",
    href: SYSTEM_SETTINGS_PATHS.uploadStorage,
    labelKey: "nav.settingsUploadStorage",
    icon: HardDrive,
  },
  {
    key: "model",
    href: SYSTEM_SETTINGS_PATHS.model,
    labelKey: "nav.settingsModel",
    sidebarLabelKey: "nav.settingsModel.sidebar",
    icon: BrainCog,
  },
  {
    key: "database",
    href: SYSTEM_SETTINGS_PATHS.database,
    labelKey: "nav.settingsDatabase",
    sidebarLabelKey: "nav.settingsDatabase.sidebar",
    icon: Database,
  },
] as const satisfies readonly SystemSettingsNavItem[];

/** 回答の注意書きの画面のパス（画面は共有パッケージ。設定は製品ごと。#2405・#2449）。 */
export const ANSWER_NOTICE_PATH = "/settings/answer-notice";

/**
 * サイドナビ「運用設定」の「システムテーブル」の下に置く「回答の注意書き」（3 製品で同じ位置とアイコン。#2449）。
 * 設定は製品ごとで、各製品の画面はその製品の回答に付ける注意書きだけを扱う。
 */
export const ANSWER_NOTICE_NAV_ITEM = {
  key: "answerNotice",
  href: ANSWER_NOTICE_PATH,
  labelKey: "nav.settingsAnswerNotice",
  icon: MessageSquareWarning,
} as const;

/** チャットの添付の設定の画面のパス（画面は共有パッケージ。設定は製品ごと。#2517）。 */
export const CHAT_ATTACHMENT_SETTINGS_PATH = "/settings/chat-attachments";

/**
 * サイドナビ「運用設定」の「回答の注意書き」の下に置く「チャットの添付」（3 製品で同じ位置とアイコン。#2517）。
 * 設定は製品ごとで、各製品の画面はその製品のチャットの添付を使うかだけを扱う。
 */
export const CHAT_ATTACHMENT_SETTINGS_NAV_ITEM = {
  key: "chatAttachments",
  href: CHAT_ATTACHMENT_SETTINGS_PATH,
  labelKey: "nav.settingsChatAttachments",
  icon: Paperclip,
} as const;

/**
 * 共有のユーザー管理・ロール管理画面のパス（#206）。NL2SQL の既存 URL をそのまま使う。
 * ロールに付ける権限の管理は製品固有の画面（NL2SQL は権限管理）で行う。
 */
export const USER_ROLE_PATHS = {
  users: "/settings/security/users",
  roles: "/settings/security/roles",
} as const;

export type UserRoleKey = keyof typeof USER_ROLE_PATHS;

export interface UserRoleNavItem {
  key: UserRoleKey;
  href: string;
  labelKey: string;
  icon: LucideIcon;
}

/** サイドナビ「ユーザーとロール」の共通2項目。製品の nav config はこれをそのまま使う。 */
export const USER_ROLE_NAV_ITEMS = [
  { key: "users", href: USER_ROLE_PATHS.users, labelKey: "nav.securityUsers", icon: Users },
  { key: "roles", href: USER_ROLE_PATHS.roles, labelKey: "nav.securityRoles", icon: Shield },
] as const satisfies readonly UserRoleNavItem[];

/**
 * アカウントの画面（3 製品で同じ。#1873）。サイドナビの下部のアカウントの欄から開き、タブ（`?tab=`）で
 * 「API キー」と「パスワード変更」を切り替える。初回のログインのパスワードの変更の強制は、単独の画面のまま。
 */
export const ACCOUNT_PATH = "/account";
export const ACCOUNT_TABS = ["api-keys", "password"] as const;
export type AccountTab = (typeof ACCOUNT_TABS)[number];

/** アカウントの画面のタブの URL（`/account?tab=password` など）。 */
export function accountTabPath(tab: AccountTab): string {
  return `${ACCOUNT_PATH}?tab=${tab}`;
}

/**
 * API キー（外部の MCP クライアント向け。#1805）の管理の画面のパス（全員のキー。「ユーザーとロール」）。
 * 自分のキーはアカウントの画面の「API キー」のタブ（`accountTabPath("api-keys")`）。
 */
export const API_KEY_PATHS = {
  admin: "/settings/security/api-keys",
} as const;

/**
 * サイドナビ「ユーザーとロール」の末尾に置く「API キー」（管理の画面。RAG / NL2SQL / Agent 共通。#1805・#1874・#3123）。
 */
export const API_KEY_ADMIN_NAV_ITEM = {
  key: "apiKeys",
  href: API_KEY_PATHS.admin,
  labelKey: "nav.securityApiKeys",
  icon: KeySquare,
} as const;

/** 運用設定のシステムテーブル直下。3製品で1つの設定を共有する。 */
export const TRACE_RECORDING_SETTINGS_PATH = "/settings/trace-recording";
export const TRACE_RECORDING_SETTINGS_NAV_ITEM = {
  key: "traceRecording", href: TRACE_RECORDING_SETTINGS_PATH, labelKey: "nav.settingsTraceRecording", icon: Waypoints,
} as const;
