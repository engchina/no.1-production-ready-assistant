import {
  ACCOUNT_PATH,
  API_KEY_PATHS,
  ANSWER_NOTICE_PATH,
  CHAT_ATTACHMENT_SETTINGS_PATH,
  TRACE_RECORDING_SETTINGS_PATH,
  SYSTEM_SETTINGS_PATHS,
  USER_ROLE_PATHS,
} from "@production-ready/system-settings";

/** Agent コンソールのルート定義。 */
export const APP_ROUTES = {
  // 認証（AppShell の外に出す画面。#215）。
  login: "/login",
  passwordChange: "/password/change",
  forbidden: "/forbidden",
  settingsAppearance: SYSTEM_SETTINGS_PATHS.appearance,
  // 回答の注意書き（設定は製品ごと。#2405・#2449）
  settingsAnswerNotice: ANSWER_NOTICE_PATH,
  // チャットの添付（設定は製品ごと。#2517）
  settingsChatAttachments: CHAT_ATTACHMENT_SETTINGS_PATH,
  settingsTraceRecording: TRACE_RECORDING_SETTINGS_PATH,
  // `/` は画面を持たない入口（ナビの並び順で最初に開ける画面へ移す。ダッシュボードは廃止。#262）。
  home: "/",
  // 業務利用者のチャット（#768）。
  chat: "/chat",
  agents: "/agents",
  runtimes: "/runtimes",
  runs: "/runs",
  approvals: "/approvals",
  audit: "/audit",
  tools: "/tools",
  skills: "/skills",
  // 改善・運用（#776 / #774 / #772）。
  evaluation: "/evaluation",
  feedback: "/feedback",
  usage: "/usage",
  settingsSystemTables: "/settings/system-tables",
  settingsOci: "/settings/oci",
  settingsUploadStorage: "/settings/upload-storage",
  settingsModel: SYSTEM_SETTINGS_PATHS.model,
  settingsDatabase: SYSTEM_SETTINGS_PATHS.database,
  // MCP 接続（RAG / NL2SQL / 外部 MCP。#757）。
  settingsMcpConnections: "/settings/mcp-connections",
  settingsToolPolicy: "/settings/tool-policy",
  // 共通のユーザー管理・ロール管理（#206）と、Agent 固有の権限管理（#215）。
  securityUsers: USER_ROLE_PATHS.users,
  securityRoles: USER_ROLE_PATHS.roles,
  securityApiKeys: API_KEY_PATHS.admin,
  securityPermissions: "/settings/security/permissions",
  // アカウントの画面（API キー / パスワード変更のタブ。3 製品で同じ。#1873）。サイドナビのアカウントの欄から開き、
  // メニュー権限を問わない。
  account: ACCOUNT_PATH,
} as const;

/** AppShell の外に出す認証の画面（ログイン・パスワード変更・権限なし）。 */
export const AUTH_ROUTE_PATHS: readonly string[] = [APP_ROUTES.login, APP_ROUTES.passwordChange, APP_ROUTES.forbidden];
