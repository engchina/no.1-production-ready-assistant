import {
  ACCOUNT_PATH,
  ANSWER_NOTICE_PATH,
  API_KEY_PATHS,
  CHAT_ATTACHMENT_SETTINGS_PATH,
  TRACE_RECORDING_SETTINGS_PATH,
  SYSTEM_SETTINGS_PATHS,
  USER_ROLE_PATHS,
} from "@production-ready/system-settings";

/** RAG コンソールの画面ルート定義。 */
export const APP_ROUTES = {
  // 回答の注意書き（設定は製品ごと。#2405・#2449）
  settingsAnswerNotice: ANSWER_NOTICE_PATH,
  // チャットの添付（設定は製品ごと。#2517）
  settingsChatAttachments: CHAT_ATTACHMENT_SETTINGS_PATH,
  settingsTraceRecording: TRACE_RECORDING_SETTINGS_PATH,
  // 認証画面（NL2SQL と同じ URL。#214）
  login: "/login",
  passwordChange: "/password/change",
  forbidden: "/forbidden",
  home: "/",
  upload: "/upload",
  fileList: "/file-list",
  knowledgeBases: "/knowledge-bases",
  searchAnswerProfiles: "/search-answer-profiles",
  documentCategories: "/document-categories",
  documents: "/documents",
  chat: "/chat",
  search: "/search",
  // 実行履歴（#1890）。詳細は `/history/<run_id>`。
  history: "/history",
  evaluation: "/evaluation",
  feedback: "/feedback",
  settingsPipeline: "/settings/pipeline",
  settingsOci: "/settings/oci",
  settingsUploadStorage: "/settings/upload-storage",
  settingsPreprocess: "/settings/preprocess",
  settingsParserAdapters: "/settings/parser-adapters",
  settingsChunking: "/settings/chunking",
  settingsLanguagePack: "/settings/language-pack",
  settingsRetrieval: "/settings/retrieval",
  settingsGuardrail: "/settings/guardrail",
  settingsVectorIndex: "/settings/vector-index",
  settingsEvaluation: "/settings/evaluation",
  settingsEntityIndex: "/settings/entity-index",
  settingsGraph: "/settings/graph",
  settingsModel: SYSTEM_SETTINGS_PATHS.model,
  settingsDatabase: SYSTEM_SETTINGS_PATHS.database,
  settingsSystemTables: "/settings/system-tables",
  settingsHuggingface: "/settings/huggingface",
  settingsServices: "/settings/services",
  settingsPrompts: "/settings/prompts",
  // ユーザー管理・ロール管理は3製品共通の画面（#206）。権限管理は RAG 固有（#214）。
  securityUsers: USER_ROLE_PATHS.users,
  securityRoles: USER_ROLE_PATHS.roles,
  // 外部の MCP クライアント向けの API キー（RAG / NL2SQL 共通。#1805）。全員のキーは「ユーザーとロール」の
  // 管理の画面、自分のキーはアカウントの画面の「API キー」のタブ（#1873）。
  securityApiKeys: API_KEY_PATHS.admin,
  // アカウントの画面（API キー / パスワード変更のタブ。3 製品で同じ。#1873）。サイドナビのアカウントの欄から開き、
  // メニュー権限を問わない。
  account: ACCOUNT_PATH,
  securityPermissions: "/settings/security/permissions",
} as const;
