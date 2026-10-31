// 3製品（RAG / NL2SQL / Agent）共通のシステム設定画面（#70）。
export {
  ACCOUNT_PATH,
  ACCOUNT_TABS,
  ANSWER_NOTICE_NAV_ITEM,
  ANSWER_NOTICE_PATH,
  CHAT_ATTACHMENT_SETTINGS_NAV_ITEM,
  CHAT_ATTACHMENT_SETTINGS_PATH,
  TRACE_RECORDING_SETTINGS_PATH,
  TRACE_RECORDING_SETTINGS_NAV_ITEM,
  API_KEY_ADMIN_NAV_ITEM,
  API_KEY_PATHS,
  accountTabPath,
  type AccountTab,
  SYSTEM_SETTINGS_NAV_ITEMS,
  SYSTEM_SETTINGS_PATHS,
  USER_ROLE_NAV_ITEMS,
  USER_ROLE_PATHS,
  type SystemSettingsKey,
  type SystemSettingsNavItem,
  type UserRoleKey,
  type UserRoleNavItem,
} from "./paths";

// 外観と証明書（#95。HTTPS の CA 証明書は #1316）
export { AppearanceSettingsPage, type AppearanceSettingsPageProps } from "./appearance/AppearanceSettingsPage";
export { CA_CERTIFICATE_PATH, probeCaCertificate } from "./appearance/CaCertificateCard";
export { APPEARANCE_MESSAGES, type AppearanceMessages } from "./appearance/messages";

// 回答の注意書き（設定は製品ごと。#2405・#2449）
export {
  AnswerNoticeSettingsPage,
  answerNoticePayload,
  composeAnswerNotice,
  normalizeAnswerNoticeContact,
  normalizeAnswerNoticeText,
  type AnswerNoticeSettingsPageProps,
} from "./answer-notice/AnswerNoticeSettingsPage";
export { ANSWER_NOTICE_MESSAGES, type AnswerNoticeMessages } from "./answer-notice/messages";
export {
  ANSWER_NOTICE_QUERY_KEY,
  type AnswerNoticeApi,
  type AnswerNoticeSettingsData,
  type AnswerNoticeSettingsUpdate,
} from "./answer-notice/types";

// チャットの添付（設定は製品ごと。#2517）
export {
  ChatAttachmentSettingsPage,
  type ChatAttachmentSettingsPageProps,
} from "./chat-attachments/ChatAttachmentSettingsPage";
export {
  CHAT_ATTACHMENT_SETTINGS_MESSAGES,
  type ChatAttachmentSettingsMessages,
} from "./chat-attachments/messages";
export {
  CHAT_ATTACHMENT_SETTINGS_QUERY_KEY,
  type ChatAttachmentSettingsApi,
  type ChatAttachmentSettingsData,
  type ChatAttachmentSettingsUpdate,
} from "./chat-attachments/types";

// 未保存変更の離脱ガード（NL2SQL から移設。#97）。戻る / 進むの blocker はアプリで 1 つ（#586）。
export {
  confirmUnsavedChanges,
  UnsavedChangesBlocker,
  useUnsavedChangesGuard,
} from "./guards/useUnsavedChangesGuard";
export {
  DRAFT_GUARD_MESSAGES,
  useSettingsDraftGuard,
  type DraftGuardMessages,
} from "./guards/useSettingsDraftGuard";
// 確認ダイアログを閉じる画面遷移の識別子（ConfirmProvider の navigationKey。#833）
export { useConfirmNavigationKey } from "./guards/useConfirmNavigationKey";

// アップロード保存先（#97。ローカルのディレクトリだけ。#11）
export {
  UploadStorageSettingsPage,
  validateUploadStorageForm,
  type UploadStorageSettingsPageProps,
} from "./upload-storage/UploadStorageSettingsPage";
export { UPLOAD_STORAGE_MESSAGES, type UploadStorageMessages } from "./upload-storage/messages";
export {
  UPLOAD_STORAGE_QUERY_KEY,
  type UploadStorageApi,
  type UploadStorageSettingsData,
  type UploadStorageSettingsUpdate,
} from "./upload-storage/types";

// テスト・接続確認の結果パネル（UX 契約 messaging.md §10。製品の接続テストも同じ部品で出す。#705）
export {
  SettingsTestResultPanel,
  toSettingsTestResultDetails,
  type SettingsTestResultDetail,
  type SettingsTestResultPanelProps,
  type SettingsTestResultTone,
  type SettingsTestResultValue,
} from "./shared/SettingsTestResultPanel";

// モデル設定（#103）
export { ModelSettingsPage, type ModelSettingsPageProps } from "./model/ModelSettingsPage";
export { MODEL_MESSAGES, type ModelMessageKey } from "./model/messages";
export {
  DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS,
  DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS,
  DEFAULT_ENTERPRISE_AI_REASONING_EFFORT,
  ENTERPRISE_AI_CONNECTION_IDS,
  ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX,
  ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN,
  ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MAX,
  ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN,
  ENTERPRISE_AI_REASONING_EFFORTS,
  MAX_ENTERPRISE_AI_CONNECTIONS,
  MODEL_SETTINGS_QUERY_KEY,
  type EnterpriseAiConfiguredModel,
  type EnterpriseAiConnectionId,
  type EnterpriseAiReasoningEffort,
  type EnterpriseAiConnectionSettings,
  type EnterpriseAiModelSettings,
  type EnterpriseAiVlmInputMode,
  type GenerativeAiModelSettings,
  type ModelSettingsApi,
  type ModelSettingsData,
  type ModelSettingsPayload,
  type ModelSettingsSecretSource,
  type ModelSettingsTestRequest,
  type ModelSettingsTestResult,
  type ModelSettingsTestStatus,
  type ModelSettingsTestTargetType,
  type ModelSettingsUpdatePayload,
} from "./model/types";

// データベース設定（#108）
export { DatabaseSettingsPage, type DatabaseSettingsPageProps } from "./database/DatabaseSettingsPage";
export { DATABASE_MESSAGES, type DatabaseMessageKey } from "./database/messages";
export type { DatabaseChangedHandler } from "./database/hooks";
export {
  DATABASE_SETTINGS_QUERY_KEY,
  type DatabaseConnectionSecurity,
  type DatabaseConnectionTestResult,
  type DatabaseConnectionTestStatus,
  type DatabasePasswordRevealData,
  type DatabaseSettingsApi,
  type DatabaseSettingsData,
  type DatabaseSettingsUpdate,
} from "./database/types";

// DB ゲート（DB の状態 API・全画面の案内・banner。RAG / NL2SQL から共通化。#325）
export {
  DATABASE_GATE_LOADING_TEST_ID,
  DatabaseGate,
  DatabaseGateChecking,
  databaseGateView,
  databaseNoticeStatus,
  isDatabaseGateExemptPath,
  type DatabaseGateNoticeOptions,
  type DatabaseGateProps,
  type DatabaseGateView,
  type DatabaseSecondaryGateProps,
} from "./database-gate/DatabaseGate";
export {
  DATABASE_UNAVAILABLE_TITLE_ID,
  DatabaseUnavailableNotice,
  databaseNoticeContent,
  databaseReasonCode,
  databaseSettingsHref,
  type DatabaseNoticeAction,
  type DatabaseNoticeContent,
  type DatabaseNoticeContentInput,
  type DatabaseNoticeKind,
  type DatabaseUnavailableNoticeProps,
} from "./database-gate/DatabaseUnavailableNotice";
export {
  DATABASE_GATE_MESSAGES,
  databaseCheckMessageKey,
  type DatabaseGateMessageKey,
  type DatabaseGateMessages,
} from "./database-gate/messages";
export {
  DATABASE_STATUS_STALE_TIME_MS,
  useDatabaseStatus,
  type DatabaseContextChangeHandler,
  type UseDatabaseStatusOptions,
} from "./database-gate/useDatabaseStatus";
export {
  DATABASE_STATUS_QUERY_KEY,
  DATABASE_UNAVAILABLE_EVENT,
  type DatabaseAvailability,
  type DatabaseGateRoutes,
  type DatabaseNoticeStatus,
  type DatabaseStatusApi,
  type DatabaseStatusData,
  type DatabaseUnavailableEventDetail,
} from "./database-gate/types";

// システムテーブルの管理（状態・作成 / 更新・確認語付きの全再作成・台帳。RAG / NL2SQL から共通化。#325）
export {
  SystemTablesCard,
  type SystemTablesCardProps,
  type SystemTablesConfirmRequest,
} from "./system-tables/SystemTablesCard";
export {
  SYSTEM_TABLES_MESSAGES,
  systemObjectTypeMessageKey,
  type SystemTablesMessageKey,
  type SystemTablesMessages,
} from "./system-tables/messages";
export {
  SYSTEM_TABLES_RUNNING_REFETCH_MS,
  isSystemTableRecreateConfirmationValid,
  isSystemTablesStatusData,
  systemTableControlsBusy,
  systemTableDetailCounts,
  systemTableObjects,
  useDeleteSystemTableOrphanedRows,
  useInitializeSystemTables,
  useSystemTablesStatus,
  type UseInitializeSystemTablesOptions,
} from "./system-tables/systemTables";
export {
  SYSTEM_TABLES_QUERY_KEY,
  type SystemObjectMetadata,
  type SystemObjectType,
  type SystemTableColumnRef,
  type SystemTableDestructiveMigration,
  type SystemTableForeignKey,
  type SystemTableMetadata,
  type SystemTableObjectRef,
  type SystemTableOperationResult,
  type SystemTableOperationState,
  type SystemTableOperationStatus,
  type SystemTableOrphanOperationResult,
  type SystemTableRestartRequired,
  type SystemTableSchemaStatus,
  type SystemTablesApi,
  type SystemTablesDeleteOrphansRequest,
  type SystemTablesInitializeRequest,
  type SystemTablesOperationData,
  type SystemTablesOrphanDeletionData,
  type SystemTablesStatusData,
} from "./system-tables/types";

// ユーザー管理・ロール管理（NL2SQL から移設。#206）
export { UserManagementPage, type UserManagementPageProps } from "./users-roles/UserManagementPage";
export {
  RoleManagementPage,
  RoleListStatusBadge,
  RoleStatusBadges,
  type RoleManagementPageProps,
  type RolePermissionSummary,
} from "./users-roles/RoleManagementPage";
export { USERS_ROLES_MESSAGES, type UsersRolesMessageKey } from "./users-roles/messages";
// ロールの表示（ロール名。表示名が空ならコード、ID は出さない。#1682）
export {
  RoleNameBadges,
  RoleNameRowTitleButton,
  compareRoleDisplayNames,
  deletedRoleLabel,
  roleDisplayName,
  type RoleNameBadgesProps,
  type RoleNameReference,
} from "./users-roles/RoleNameBadges";
// ユーザーの表示（サイドナビ以外は表示名。ログイン ID・UUID は出さない。#1696）
export {
  deletedUserLabel,
  userDisplayName,
  type UserNameReference,
} from "./users-roles/userDisplayName";
export {
  SecurityDetailField,
  SecurityIdentityLines,
  SecurityIdentityRowTitleButton,
  SecurityManagementPanelShell,
  SecurityManagementStatusBar,
  SecurityPanelHeader,
  SecuritySearchField,
  SecurityClearSearchAction,
  identityInlineLabel,
  identitySecondaryName,
  securityFilteredCount,
  type SecurityManagementMetric,
} from "./users-roles/shared";
export {
  SYSTEM_ADMIN_ROLE_CODE,
  type ApiErrorDetails,
  type ApiFieldProblem,
  type AssignedRole,
  type DescribeApiError,
  type RoleDraft,
  type RoleManagementApi,
  type SecurityRole,
  type SecurityUser,
  type UserDraft,
  type UserManagementApi,
  type UserWithTemporaryPassword,
} from "./users-roles/types";

// ログイン・パスワード変更・権限なし・ルートの保護・CSRF・401/403 イベント（NL2SQL から移設。#220）
export {
  AuthProvider,
  defaultIdentityKey,
  useAuth,
  type AuthContextValue,
  type AuthProviderProps,
} from "./auth/AuthProvider";
export {
  ForbiddenPage,
  LoginPage,
  requestedPathFrom,
  PasswordChangePage,
  type AuthBrand,
  type AuthPageProps,
  type ForbiddenPageProps,
  type LoginPageProps,
  type PasswordChangePageProps,
} from "./auth/AuthPages";
export {
  RequireAuth,
  useSidebarAccount,
  type RequireAuthProps,
  type SidebarAccount,
  type SidebarAccountRoutes,
} from "./auth/RequireAuth";
export { SidebarAccountSection, type SidebarAccountSectionProps } from "./auth/SidebarAccountSection";
export { AUTH_MESSAGES, formatMessage, type AuthMessages } from "./auth/messages";
export {
  AUTH_FORBIDDEN_EVENT,
  AUTH_UNAUTHORIZED_EVENT,
  notifyAuthStatus,
  notifyAuthResponse,
  responseErrorCode,
  isRouteForbidden,
  ROUTE_FORBIDDEN_ERROR_CODES,
  useForbiddenRedirect,
  type AuthForbiddenDetail,
} from "./auth/events";
export { CSRF_COOKIE_NAME, CSRF_HEADER_NAME, csrfHeader, readCookie } from "./auth/csrf";
export {
  AUTH_BROADCAST_CHANNEL,
  type AuthBroadcastMessage,
  type AuthBroadcastType,
} from "./auth/broadcast";
export {
  createPermissionCheck,
  expandPermissions,
  firstAllowedRoute,
  routePermissionMap,
  type PermissionRouteItem,
} from "./auth/permissions";
export type {
  AuthApi,
  AuthRequestOptions,
  AuthRoutes,
  AuthStatus,
  BaseCurrentUser,
  HasPermission,
} from "./auth/types";

// 権限管理（ロールごとの機能権限と、製品固有の利用できる対象。NL2SQL から移設。#220）
export {
  RolePermissionsPage,
  effectivePermissionCodes,
  normalizeCustomTargetId,
  permissionInheritanceSources,
  targetItemLabel,
  targetItemsWithCustomIds,
  resolveTargetItems,
  rolePermissionTargetSearchParams,
  TARGET_PAGE_SIZE,
  type RolePermissionsPageProps,
} from "./permissions/RolePermissionsPage";
// 権限管理の機能の一覧を左のナビにそろえる（#567）
export {
  arrangePermissionsByNav,
  permissionNavSections,
  type NavConfigSectionLike,
  type PermissionNavSection,
} from "./permissions/navLayout";
export {
  ROLE_PERMISSIONS_MESSAGES,
  type RolePermissionTargetMessages,
  type RolePermissionsMessages,
} from "./permissions/messages";
export type {
  PermissionDefinition,
  PermissionRole,
  RolePermissionCustomIdOptions,
  RolePermissionTargetItem,
  RolePermissionTargetPage,
  RolePermissionTargetQuery,
  RolePermissionTargetSection,
  RolePermissionsApi,
  RolePermissionsDraft,
} from "./permissions/types";

// API キー（外部の MCP クライアント向け。3 製品。#1805・#1874）
export { MyApiKeysPanel, type MyApiKeysPanelProps } from "./api-keys/MyApiKeysPanel";
// アカウントの画面（API キー / パスワード変更のタブ。3 製品で同じ。#1873）
export { AccountPage, type AccountPageProps } from "./account/AccountPage";
export { ApiKeysAdminPage, type ApiKeysAdminPageProps } from "./api-keys/ApiKeysAdminPage";
export { caCertificateUrl, mcpEndpointRows, mcpEndpoints, mcpRegistrationExample } from "./api-keys/endpoints";
export {
  API_KEYS_MESSAGES,
  type ApiKeysMessageKey,
  type ApiKeysMessages,
} from "./api-keys/messages";
export {
  API_KEY_EXPIRY_DAYS,
  API_KEY_MAX_PER_USER,
  API_KEY_PRODUCTS,
  API_KEYS_ADMIN_QUERY_KEY,
  API_KEYS_QUERY_KEY,
  type ApiKey,
  type ApiKeyCreateRequest,
  type ApiKeyCreated,
  type ApiKeyExpiryDays,
  type ApiKeyProduct,
  type ApiKeyResourceScopes,
  type ApiKeyScopeOption,
  type ApiKeysAdminApi,
  type ApiKeysList,
  type McpEndpoint,
  type McpEndpointRow,
  type MyApiKeysApi,
  type MyApiKeysList,
} from "./api-keys/types";

export { TraceRecordingSettingsPage, type TraceRecordingSettingsPageProps } from "./trace-recording/TraceRecordingSettingsPage";
export { TRACE_RECORDING_SETTINGS_MESSAGES } from "./trace-recording/messages";
export { TRACE_RECORDING_SETTINGS_QUERY_KEY, type TraceRecordingSettingsData, type TraceRecordingSettingsApi } from "./trace-recording/types";
