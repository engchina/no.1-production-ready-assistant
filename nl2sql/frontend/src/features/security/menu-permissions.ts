import type { CurrentUser } from "./types";

export const MENU_PERMISSIONS = {
  chat: "menu.chat",
  query: "menu.query",
  directSql: "menu.direct_sql",
  sqlToQuestion: "menu.sql_to_question",
  history: "menu.history",
  // DB 管理チャット（#2264）。既定はどのロールにも付けない（system_admin だけ）。
  dbAdminChat: "menu.db_admin_chat",
  adminSql: "menu.admin_sql",
  tableManagement: "menu.table_management",
  viewManagement: "menu.view_management",
  dataManagement: "menu.data_management",
  commentManagement: "menu.comment_management",
  annotationManagement: "menu.annotation_management",
  domainManagement: "menu.domain_management",
  glossaryRules: "menu.glossary_rules",
  globalRules: "menu.global_rules",
  sampleData: "menu.sample_data",
  profiles: "menu.profiles",
  ontologyBuild: "menu.ontology_build",
  feedbackManagement: "menu.feedback_management",
  questionClassifierModels: "menu.question_classifier_models",
  evaluation: "menu.evaluation",
  securityUsers: "menu.security_users",
  securityRoles: "menu.security_roles",
  // 全員の API キー（管理の画面）。自分のキーはメニュー権限なしで使える（#1805）。
  securityApiKeys: "menu.security_api_keys",
  securityPermissions: "menu.security_permissions",
  settingsOci: "menu.settings_oci",
  settingsUploadStorage: "menu.settings_upload_storage",
  settingsModel: "menu.settings_model",
  settingsDatabase: "menu.settings_database",
  settingsSystemTables: "menu.settings_system_tables",
  settingsSelectAiCredential: "menu.settings_select_ai_credential",
  settingsAppearance: "menu.settings_appearance",
  settingsAnswerNotice: "menu.settings_answer_notice",
  settingsTraceRecording: "menu.settings_trace_recording",
  settingsChatAttachments: "menu.settings_chat_attachments",
} as const;

export const CAPABILITY_PERMISSIONS = {
  profilesRead: "nl2sql.profiles.read",
  profilesManage: "nl2sql.profiles.manage",
  schemaRead: "nl2sql.schema.read",
  schemaRefresh: "nl2sql.schema.refresh",
  queryGenerate: "nl2sql.query.generate",
  sqlExecute: "nl2sql.sql.execute",
  feedbackWrite: "nl2sql.feedback.write",
  feedbackManage: "nl2sql.feedback.manage",
  selectAiAssetsRead: "nl2sql.select_ai_assets.read",
  selectAiAssetsRefresh: "nl2sql.select_ai_assets.refresh",
  selectAiAssetsManage: "nl2sql.select_ai_assets.manage",
  sampleDataManage: "nl2sql.sample_data.manage",
  learningMaterialManage: "nl2sql.learning_material.manage",
  systemStatusRead: "nl2sql.system_status.read",
  persistenceRecover: "nl2sql.persistence.recover",
} as const;

export type MenuPermission = (typeof MENU_PERMISSIONS)[keyof typeof MENU_PERMISSIONS];
export type CapabilityPermission =
  (typeof CAPABILITY_PERMISSIONS)[keyof typeof CAPABILITY_PERMISSIONS];

const PERMISSION_IMPLIES: Record<string, string[]> = {
  [MENU_PERMISSIONS.chat]: [
    CAPABILITY_PERMISSIONS.queryGenerate,
    CAPABILITY_PERMISSIONS.profilesRead,
    CAPABILITY_PERMISSIONS.schemaRead,
  ],
  [MENU_PERMISSIONS.query]: [
    CAPABILITY_PERMISSIONS.queryGenerate,
    CAPABILITY_PERMISSIONS.sqlExecute,
    CAPABILITY_PERMISSIONS.feedbackWrite,
    CAPABILITY_PERMISSIONS.profilesRead,
    CAPABILITY_PERMISSIONS.schemaRead,
  ],
  [MENU_PERMISSIONS.directSql]: [
    CAPABILITY_PERMISSIONS.sqlExecute,
    CAPABILITY_PERMISSIONS.schemaRead,
  ],
  [MENU_PERMISSIONS.sqlToQuestion]: [
    CAPABILITY_PERMISSIONS.profilesRead,
    CAPABILITY_PERMISSIONS.schemaRead,
  ],
  [MENU_PERMISSIONS.dbAdminChat]: [
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
  ],
  [MENU_PERMISSIONS.adminSql]: [
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
    CAPABILITY_PERMISSIONS.systemStatusRead,
    CAPABILITY_PERMISSIONS.persistenceRecover,
  ],
  [MENU_PERMISSIONS.tableManagement]: [
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
  ],
  [MENU_PERMISSIONS.viewManagement]: [
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
  ],
  [MENU_PERMISSIONS.dataManagement]: [
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
    CAPABILITY_PERMISSIONS.selectAiAssetsRead,
    CAPABILITY_PERMISSIONS.selectAiAssetsRefresh,
  ],
  // コメント・アノテーション・ドメインの管理も DDL を実行し、「スキーマを更新」と実行後の job の追跡を使う（#972）。
  [MENU_PERMISSIONS.commentManagement]: [
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
  ],
  [MENU_PERMISSIONS.annotationManagement]: [
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
  ],
  [MENU_PERMISSIONS.domainManagement]: [
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
  ],
  // 業務プロファイル管理と学習素材管理は含めない（backend の PERMISSION_CATALOG と同じ。#1006）。
  [MENU_PERMISSIONS.glossaryRules]: [CAPABILITY_PERMISSIONS.schemaRead],
  [MENU_PERMISSIONS.globalRules]: [CAPABILITY_PERMISSIONS.schemaRead],
  [MENU_PERMISSIONS.sampleData]: [
    CAPABILITY_PERMISSIONS.sampleDataManage,
    CAPABILITY_PERMISSIONS.schemaRead,
  ],
  [MENU_PERMISSIONS.profiles]: [
    CAPABILITY_PERMISSIONS.profilesManage,
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
    CAPABILITY_PERMISSIONS.selectAiAssetsRead,
    CAPABILITY_PERMISSIONS.selectAiAssetsRefresh,
    CAPABILITY_PERMISSIONS.selectAiAssetsManage,
    CAPABILITY_PERMISSIONS.learningMaterialManage,
  ],
  [MENU_PERMISSIONS.ontologyBuild]: [
    CAPABILITY_PERMISSIONS.profilesManage,
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
    CAPABILITY_PERMISSIONS.learningMaterialManage,
  ],
  [MENU_PERMISSIONS.feedbackManagement]: [
    CAPABILITY_PERMISSIONS.profilesRead,
    CAPABILITY_PERMISSIONS.feedbackWrite,
    CAPABILITY_PERMISSIONS.feedbackManage,
    CAPABILITY_PERMISSIONS.selectAiAssetsRead,
    CAPABILITY_PERMISSIONS.selectAiAssetsManage,
  ],
  [MENU_PERMISSIONS.questionClassifierModels]: [
    CAPABILITY_PERMISSIONS.profilesRead,
  ],
  [MENU_PERMISSIONS.evaluation]: [
    CAPABILITY_PERMISSIONS.profilesRead,
    CAPABILITY_PERMISSIONS.queryGenerate,
  ],
  [MENU_PERMISSIONS.settingsDatabase]: [
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
    CAPABILITY_PERMISSIONS.systemStatusRead,
    CAPABILITY_PERMISSIONS.persistenceRecover,
  ],
  [MENU_PERMISSIONS.settingsSystemTables]: [
    CAPABILITY_PERMISSIONS.schemaRead,
    CAPABILITY_PERMISSIONS.schemaRefresh,
    CAPABILITY_PERMISSIONS.systemStatusRead,
    CAPABILITY_PERMISSIONS.persistenceRecover,
  ],
  [CAPABILITY_PERMISSIONS.profilesManage]: [CAPABILITY_PERMISSIONS.profilesRead],
  [CAPABILITY_PERMISSIONS.schemaRefresh]: [CAPABILITY_PERMISSIONS.schemaRead],
  [CAPABILITY_PERMISSIONS.feedbackManage]: [
    CAPABILITY_PERMISSIONS.feedbackWrite,
    CAPABILITY_PERMISSIONS.profilesRead,
  ],
  [CAPABILITY_PERMISSIONS.selectAiAssetsRefresh]: [
    CAPABILITY_PERMISSIONS.selectAiAssetsRead,
  ],
  [CAPABILITY_PERMISSIONS.selectAiAssetsManage]: [
    CAPABILITY_PERMISSIONS.selectAiAssetsRead,
    CAPABILITY_PERMISSIONS.selectAiAssetsRefresh,
  ],
  [CAPABILITY_PERMISSIONS.persistenceRecover]: [
    CAPABILITY_PERMISSIONS.systemStatusRead,
  ],
};

const LEGACY_PERMISSION_ALIASES: Record<string, string[]> = {
  "dashboard.view": [MENU_PERMISSIONS.settingsAppearance],
  "documents.view": [
    MENU_PERMISSIONS.tableManagement,
    MENU_PERMISSIONS.viewManagement,
    MENU_PERMISSIONS.dataManagement,
    MENU_PERMISSIONS.commentManagement,
    MENU_PERMISSIONS.annotationManagement,
    MENU_PERMISSIONS.domainManagement,
    MENU_PERMISSIONS.sampleData,
  ],
  "documents.upload": [MENU_PERMISSIONS.dataManagement, MENU_PERMISSIONS.sampleData],
  "documents.preview": [
    MENU_PERMISSIONS.tableManagement,
    MENU_PERMISSIONS.viewManagement,
    MENU_PERMISSIONS.dataManagement,
  ],
  "documents.approve": [MENU_PERMISSIONS.dataManagement],
  "documents.ingest": [MENU_PERMISSIONS.dataManagement],
  "documents.delete": [
    MENU_PERMISSIONS.tableManagement,
    MENU_PERMISSIONS.viewManagement,
    MENU_PERMISSIONS.dataManagement,
  ],
  "knowledge_bases.view": [
    MENU_PERMISSIONS.profiles,
    MENU_PERMISSIONS.ontologyBuild,
    MENU_PERMISSIONS.glossaryRules,
    MENU_PERMISSIONS.globalRules,
  ],
  "knowledge_bases.manage": [
    MENU_PERMISSIONS.profiles,
    MENU_PERMISSIONS.ontologyBuild,
    MENU_PERMISSIONS.glossaryRules,
    MENU_PERMISSIONS.globalRules,
  ],
  "business_views.view": [MENU_PERMISSIONS.profiles],
  "business_views.manage": [MENU_PERMISSIONS.profiles],
  "business_views.use": [MENU_PERMISSIONS.query, MENU_PERMISSIONS.profiles],
  "search.view": [
    MENU_PERMISSIONS.query,
    MENU_PERMISSIONS.directSql,
    MENU_PERMISSIONS.sqlToQuestion,
    MENU_PERMISSIONS.history,
  ],
  "search.execute": [MENU_PERMISSIONS.query, MENU_PERMISSIONS.directSql],
  "search.export": [
    MENU_PERMISSIONS.query,
    MENU_PERMISSIONS.directSql,
    MENU_PERMISSIONS.history,
  ],
  "evaluation.view": [
    MENU_PERMISSIONS.feedbackManagement,
    MENU_PERMISSIONS.questionClassifierModels,
    MENU_PERMISSIONS.evaluation,
  ],
  "evaluation.run": [MENU_PERMISSIONS.evaluation],
  "evaluation.manage": [
    MENU_PERMISSIONS.feedbackManagement,
    MENU_PERMISSIONS.questionClassifierModels,
    MENU_PERMISSIONS.evaluation,
  ],
  "settings.oci.view": [MENU_PERMISSIONS.settingsOci],
  "settings.oci.manage": [MENU_PERMISSIONS.settingsOci],
  "settings.object_storage.view": [MENU_PERMISSIONS.settingsUploadStorage],
  "settings.object_storage.manage": [MENU_PERMISSIONS.settingsUploadStorage],
  "settings.models.view": [MENU_PERMISSIONS.settingsModel],
  "settings.models.manage": [MENU_PERMISSIONS.settingsModel],
  "settings.database.view": [
    MENU_PERMISSIONS.settingsDatabase,
    MENU_PERMISSIONS.settingsSystemTables,
  ],
  "settings.database.manage": [
    MENU_PERMISSIONS.settingsDatabase,
    MENU_PERMISSIONS.settingsSelectAiCredential,
  ],
  "settings.database.sql_execute": [
    MENU_PERMISSIONS.adminSql,
    MENU_PERMISSIONS.settingsSystemTables,
  ],
  "settings.appearance.view": [MENU_PERMISSIONS.settingsAppearance],
  "security.users.view": [MENU_PERMISSIONS.securityUsers],
  "security.users.manage": [MENU_PERMISSIONS.securityUsers],
  "security.roles.view": [MENU_PERMISSIONS.securityRoles],
  "security.roles.manage": [MENU_PERMISSIONS.securityRoles, MENU_PERMISSIONS.securityPermissions],
};

export function normalizePermissionCodes(permissions: string[]): Set<string> {
  const normalized = new Set<string>();
  const pending = permissions.map((permission) => permission.trim()).filter(Boolean);
  while (pending.length > 0) {
    const permission = pending.pop();
    if (!permission) continue;
    const aliases = LEGACY_PERMISSION_ALIASES[permission];
    if (aliases) {
      pending.push(...aliases);
      continue;
    }
    if (permission.startsWith("menu.") || permission.startsWith("nl2sql.")) {
      normalized.add(permission);
      continue;
    }
    normalized.add(permission);
  }
  return normalized;
}

export function normalizeMenuPermissions(permissions: string[]): Set<string> {
  const normalized = normalizePermissionCodes(permissions);
  const pending = [...normalized];
  while (pending.length > 0) {
    const permission = pending.pop();
    if (!permission) continue;
    for (const implied of PERMISSION_IMPLIES[permission] ?? []) {
      if (normalized.has(implied)) continue;
      normalized.add(implied);
      pending.push(implied);
    }
  }
  return normalized;
}

export function currentUserHasPermission(
  user: Pick<CurrentUser, "is_system_admin" | "permissions"> | null | undefined,
  permission: string,
  normalizedPermissions = user ? normalizeMenuPermissions(user.permissions) : new Set<string>()
): boolean {
  if (!user) return false;
  if (user.is_system_admin) return true;
  const requestedPermissions = normalizePermissionCodes([permission]);
  return (
    normalizedPermissions.has(permission) ||
    [...requestedPermissions].some((code) => normalizedPermissions.has(code))
  );
}
