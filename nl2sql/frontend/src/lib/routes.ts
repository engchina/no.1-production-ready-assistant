import {
  ACCOUNT_PATH,
  ANSWER_NOTICE_PATH,
  API_KEY_PATHS,
  CHAT_ATTACHMENT_SETTINGS_PATH,
  TRACE_RECORDING_SETTINGS_PATH,
  SYSTEM_SETTINGS_PATHS,
  USER_ROLE_PATHS,
} from "@production-ready/system-settings";

/** NL2SQL コンソールのルート定義。 */
export const APP_ROUTES = {
  login: "/login",
  passwordChange: "/password/change",
  forbidden: "/forbidden",
  home: "/",
  chat: "/chat",
  dbAdminChat: "/db-admin-chat",
  adminSql: "/admin-sql",
  tableManagement: "/table-management",
  viewManagement: "/view-management",
  dataManagement: "/data-management",
  sampleData: "/sample-data",
  commentManagement: "/comment-management",
  annotationManagement: "/annotation-management",
  domainManagement: "/domain-management",
  query: "/query",
  profiles: "/profiles",
  ontologyBuild: "/ontology-build",
  glossaryRules: "/glossary-rules",
  globalRules: "/global-rules",
  sqlToQuestion: "/sql-to-question",
  directSql: "/direct-sql",
  feedbackManagement: "/feedback-management",
  learning: "/learning",
  questionLearning: "/question-learning",
  questionClassifierModels: "/question-classifier-models",
  history: "/history",
  evaluation: "/evaluation",
  settingsOci: "/settings/oci",
  settingsUploadStorage: "/settings/upload-storage",
  settingsModel: SYSTEM_SETTINGS_PATHS.model,
  settingsDatabase: SYSTEM_SETTINGS_PATHS.database,
  settingsSystemTables: "/settings/system-tables",
  settingsSelectAiCredential: "/settings/select-ai-credential",
  settingsAppearance: SYSTEM_SETTINGS_PATHS.appearance,
  // 回答の注意書き（設定は製品ごと。#2405・#2449）
  settingsAnswerNotice: ANSWER_NOTICE_PATH,
  // チャットの添付（設定は製品ごと。#2517）
  settingsChatAttachments: CHAT_ATTACHMENT_SETTINGS_PATH,
  settingsTraceRecording: TRACE_RECORDING_SETTINGS_PATH,
  // ユーザー管理・ロール管理は3製品共通（platform の共有パッケージ。#206）。
  securityUsers: USER_ROLE_PATHS.users,
  securityRoles: USER_ROLE_PATHS.roles,
  // 外部の MCP クライアント向けの API キー（RAG / NL2SQL 共通。#1805）。自分のキーはアカウントの
  // メニューから開き（権限を問わない）、全員のキーは「ユーザーとロール」の管理の画面。
  securityApiKeys: API_KEY_PATHS.admin,
  // アカウントの画面（API キー / パスワード変更のタブ。3 製品で同じ。#1873）。サイドナビのアカウントの欄から開き、
  // メニュー権限を問わない。
  account: ACCOUNT_PATH,
  securityPermissions: "/settings/security/permissions",
  securityDeepSec: "/settings/security/deepsec",
  /** 旧「NL2SQL モデル学習」URL。業務プロファイルの Select AI 設定へ転送する。 */
  legacyNl2sqlModelLearning: "/settings/nl2sql-model",
} as const;

/**
 * 本文の高さいっぱいに描く画面（会話の欄の中だけをスクロールし、入力欄を下端に置くチャット。#2332）。
 * 作業状態の入れ物（`WorkspaceBoundary` / `WorkspacePage`）はこの一覧で高さを渡す。チャットの画面を足したら
 * ここにも足す（無いとページ全体がスクロールし、入力欄が画面の外に出る）。
 */
export const FILL_HEIGHT_ROUTES: ReadonlySet<string> = new Set<string>([
  APP_ROUTES.chat,
  APP_ROUTES.dbAdminChat,
]);
