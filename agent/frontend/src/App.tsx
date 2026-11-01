import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";

import { AppShell, PageBody, PageHeader, PageLoadingState } from "@production-ready/ui";
import { RequireAuth, useForbiddenRedirect } from "@production-ready/system-settings";

import { AppSidebar } from "@/components/layout/AppSidebar";
import { ForbiddenPage, LoginPage, PasswordChangePage } from "@/components/security/AuthPages";
import { agentIdentityKey, useAuth } from "@/components/security/AuthProvider";
import { CapabilityGate } from "@/components/security/CapabilityGate";
import { DatabaseGate } from "@/components/system/DatabaseGate";
import { useCapabilities, type AgentCapabilities } from "@/lib/permissions";
import { canOpenRoute, defaultEntryRoute, firstAllowedRoute } from "@/lib/route-permissions";
import { APP_ROUTES } from "@/lib/routes";
import { t, type I18nKey } from "@/lib/i18n";
import { NonPersistentStorageNotice } from "@/components/system/StorageNotice";

// 画面は route ごとに分けて読む（NL2SQL と同じ。#818）。最初に開いた画面のコードだけを読み、残りは移動したときに読む。
const ChatPage = lazy(() =>
  import("@/pages/ChatPage").then((module) => ({ default: module.ChatPage }))
);
const AccountRoute = lazy(() =>
  import("@/components/security/AccountPage").then((module) => ({ default: module.AccountRoute }))
);
const SecurityPermissionsPage = lazy(() =>
  import("@/components/security/SecurityPermissionsPage").then((module) => ({ default: module.SecurityPermissionsPage }))
);
const SecurityApiKeysPage = lazy(() =>
  import("@/components/security/SecurityApiKeysPage").then((module) => ({ default: module.SecurityApiKeysPage }))
);
const SecurityRolesPage = lazy(() =>
  import("@/components/security/SecurityRolesPage").then((module) => ({ default: module.SecurityRolesPage }))
);
const SecurityUsersPage = lazy(() =>
  import("@/components/security/SecurityUsersPage").then((module) => ({ default: module.SecurityUsersPage }))
);
const DatabaseSettingsClient = lazy(() =>
  import("@/components/settings/DatabaseSettingsClient").then((module) => ({ default: module.DatabaseSettingsClient }))
);
const SystemTablesCard = lazy(() =>
  import("@/components/settings/SystemTablesCard").then((module) => ({ default: module.SystemTablesCard }))
);
const ModelSettingsClient = lazy(() =>
  import("@/components/settings/ModelSettingsClient").then((module) => ({ default: module.ModelSettingsClient }))
);
const OciSettingsClient = lazy(() =>
  import("@/components/settings/OciSettingsClient").then((module) => ({ default: module.OciSettingsClient }))
);
const AnswerNoticeSettingsClient = lazy(() =>
  import("@/components/settings/AnswerNoticeSettingsClient").then((module) => ({ default: module.AnswerNoticeSettingsClient }))
);
const TraceRecordingSettingsClient = lazy(() =>
  import("@/components/settings/TraceRecordingSettingsClient").then((module) => ({default: module.TraceRecordingSettingsClient})),
);
const ChatAttachmentSettingsClient = lazy(() =>
  import("@/components/settings/ChatAttachmentSettingsClient").then((module) => ({
    default: module.ChatAttachmentSettingsClient,
  }))
);
const UploadStorageSettingsClient = lazy(() =>
  import("@/components/settings/UploadStorageSettingsClient").then((module) => ({ default: module.UploadStorageSettingsClient }))
);
const FeedbackPage = lazy(() =>
  import("@/pages/FeedbackPage").then((module) => ({ default: module.FeedbackPage }))
);
const EvaluationPage = lazy(() =>
  import("@/pages/EvaluationPage").then((module) => ({ default: module.EvaluationPage }))
);
const UsagePage = lazy(() =>
  import("@/pages/UsagePage").then((module) => ({ default: module.UsagePage }))
);
const AgentsPage = lazy(() =>
  import("@/pages/agents/AgentsPage").then((module) => ({ default: module.AgentsPage }))
);
const ApprovalsPage = lazy(() =>
  import("@/pages/approvals/ApprovalsPage").then((module) => ({ default: module.ApprovalsPage }))
);
const AuditPage = lazy(() =>
  import("@/pages/audit/AuditPage").then((module) => ({ default: module.AuditPage }))
);
const McpConnectionsPage = lazy(() =>
  import("@/pages/settings/McpConnectionsPage").then((module) => ({ default: module.McpConnectionsPage }))
);
const RuntimesPage = lazy(() =>
  import("@/pages/runtimes/RuntimesPage").then((module) => ({ default: module.RuntimesPage }))
);
const RunsPage = lazy(() =>
  import("@/pages/runs/RunsPage").then((module) => ({ default: module.RunsPage }))
);
const SkillsPage = lazy(() =>
  import("@/pages/skills/SkillsPage").then((module) => ({ default: module.SkillsPage }))
);
const ToolPolicySettingsPage = lazy(() =>
  import("@/pages/settings/ToolPolicySettingsPage").then((module) => ({ default: module.ToolPolicySettingsPage }))
);
const ToolsPage = lazy(() =>
  import("@/pages/tools/ToolsPage").then((module) => ({ default: module.ToolsPage }))
);

/** 認証画面とルートの保護が使う URL（共通の RequireAuth へ渡す。#215）。 */
const AUTH_ROUTES = {
  login: APP_ROUTES.login,
  passwordChange: APP_ROUTES.passwordChange,
  forbidden: APP_ROUTES.forbidden,
};

export function App() {
  // API の 403 は共通のイベントで受け、request ID を載せて権限なしの画面へ移す（#215）。
  useForbiddenRedirect(APP_ROUTES.forbidden);

  return (
    <Routes>
      <Route path={APP_ROUTES.login} element={<LoginPage />} />
      <Route path={APP_ROUTES.passwordChange} element={<PasswordChangePage />} />
      <Route path={APP_ROUTES.forbidden} element={<ForbiddenPage />} />
      <Route element={<ProtectedLayout />}>
        {/* `/` は画面を持たず、ナビの並び順で最初に開ける画面へ移す（ダッシュボードは廃止。#262）。 */}
        <Route path={APP_ROUTES.home} element={<HomeRedirect />} />
        <Route
          path={APP_ROUTES.agents}
          element={
            <Capability need="viewRuns" titleKey="nav.agents">
              <AgentsPage />
            </Capability>
          }
        />
        {/* 実行環境の状態は Run のデータではないので、メニュー権限（route の保護）だけで開ける（#1041）。 */}
        <Route path={APP_ROUTES.runtimes} element={<RuntimesPage />} />
        <Route
          path={APP_ROUTES.chat}
          element={
            <Capability need="operateRuns" titleKey="nav.chat">
              <ChatPage />
            </Capability>
          }
        />
        <Route
          path={APP_ROUTES.runs}
          element={
            <Capability need="viewRuns" titleKey="nav.runs">
              <RunsPage />
            </Capability>
          }
        />
        <Route
          path={APP_ROUTES.approvals}
          element={
            <Capability need="viewRuns" titleKey="nav.approvals">
              <ApprovalsPage />
            </Capability>
          }
        />
        <Route
          path={APP_ROUTES.audit}
          element={
            <Capability need="viewAudit" titleKey="nav.audit">
              <AuditPage />
            </Capability>
          }
        />
        <Route path={APP_ROUTES.tools} element={<ToolsPage />} />
        <Route
          path={APP_ROUTES.skills}
          element={
            <Capability need="viewRuns" titleKey="nav.skills">
              <SkillsPage />
            </Capability>
          }
        />
        <Route path={APP_ROUTES.feedback} element={<FeedbackPage />} />
        <Route path={APP_ROUTES.evaluation} element={<EvaluationPage />} />
        <Route path={APP_ROUTES.usage} element={<UsagePage />} />
        <Route path={APP_ROUTES.settingsSystemTables} element={<SettingsSystemTablesRoute />} />
        <Route path={APP_ROUTES.settingsOci} element={<SettingsOciRoute />} />
        <Route path={APP_ROUTES.settingsUploadStorage} element={<SettingsUploadStorageRoute />} />
        <Route path={APP_ROUTES.settingsModel} element={<ModelSettingsClient />} />
        <Route path={APP_ROUTES.settingsDatabase} element={<SettingsDatabaseRoute />} />
        <Route path={APP_ROUTES.settingsMcpConnections} element={<McpConnectionsPage />} />
        <Route path={APP_ROUTES.settingsToolPolicy} element={<ToolPolicySettingsPage />} />
        <Route path={APP_ROUTES.settingsAnswerNotice} element={<SettingsAnswerNoticeRoute />} />
        <Route path={APP_ROUTES.settingsTraceRecording} element={<SettingsTraceRecordingRoute />} />
        <Route path={APP_ROUTES.settingsChatAttachments} element={<SettingsChatAttachmentsRoute />} />
        <Route path={APP_ROUTES.securityUsers} element={<SecurityUsersPage />} />
        <Route path={APP_ROUTES.securityRoles} element={<SecurityRolesPage />} />
        <Route path={APP_ROUTES.securityApiKeys} element={<SecurityApiKeysPage />} />
        <Route path={APP_ROUTES.securityPermissions} element={<SecurityPermissionsPage />} />
        {/* アカウントの画面（API キー / パスワード変更のタブ。#1873）。ログインだけで開ける。 */}
        <Route path={APP_ROUTES.account} element={<AccountRoute />} />
        <Route path="*" element={<EntryRedirect />} />
      </Route>
    </Routes>
  );
}

/** `/` は、ナビの並び順で最初に開ける画面へ（NL2SQL と同じ。#262）。 */
function HomeRedirect() {
  const { hasPermission } = useAuth();
  return <Navigate to={firstAllowedRoute(hasPermission)} replace />;
}

/** 未知の URL は既定入口（チャットを開ければチャット、開けなければ `/` 経由で最初に開ける画面。#215 / #262 / #791）。 */
function EntryRedirect() {
  const { hasPermission } = useAuth();
  return <Navigate to={defaultEntryRoute(hasPermission)} replace />;
}

/**
 * 認証が必要な画面の入口。確認中・未認証・強制パスワード変更の振り分けは共通の RequireAuth（#220）、
 * URL ごとの権限（ナビに出さない画面を含む）はここで判定する（#215）。
 */
function ProtectedLayout() {
  return (
    <RequireAuth routes={AUTH_ROUTES}>
      <RoutePermissionGuard>
        <AuthorizedLayout />
      </RoutePermissionGuard>
    </RequireAuth>
  );
}

function RoutePermissionGuard({ children }: { children: ReactNode }) {
  const { hasPermission } = useAuth();
  const { pathname } = useLocation();
  if (!canOpenRoute(pathname, hasPermission)) {
    return <Navigate to={APP_ROUTES.forbidden} replace />;
  }
  return <>{children}</>;
}

function AuthorizedLayout() {
  const { user } = useAuth();
  // 利用者や権限・対象範囲が変わったら、画面の state ごと作り直す（cache は AuthProvider が破棄する）。
  return (
    <AppShell
      key={user ? agentIdentityKey(user) : ""}
      sidebar={<AppSidebar />}
      mainClassName="[contain:layout]"
      skipLinkLabel={t("common.skipToMain")}
    >
      {/* DB が使えるまで本文だけを案内に替える（サイドナビは残し、システム設定の 5 画面は開ける。#325）。 */}
      <DatabaseGate>
        <Suspense fallback={<RouteLoadingFallback />}>
          <Outlet />
        </Suspense>
      </DatabaseGate>
    </AppShell>
  );
}

/** 画面のコードを読んでいる間（UX 契約 messaging.md §3.7 の placement="page"。白い画面にしない）。 */
function RouteLoadingFallback() {
  return (
    <PageLoadingState label={t("app.route.loading")} operationKey="route-loading" testId="route-loading" />
  );
}

const CAPABILITY_REQUIREMENTS: Record<keyof AgentCapabilities, I18nKey> = {
  viewRuns: "capability.viewRuns",
  operateRuns: "capability.operateRuns",
  decideApprovals: "capability.decideApprovals",
  viewAudit: "capability.viewAudit",
  buildAgents: "capability.buildAgents",
  admin: "capability.admin",
};

/** 画面の実データの閲覧に必要な capability が無ければ、データを取らずに権限不足を説明する（#215）。 */
function Capability({
  need,
  titleKey,
  children,
}: {
  need: keyof AgentCapabilities;
  titleKey: I18nKey;
  children: ReactNode;
}) {
  const capabilities = useCapabilities();
  return (
    <CapabilityGate allowed={capabilities[need]} title={t(titleKey)} requirement={t(CAPABILITY_REQUIREMENTS[need])}>
      {children}
    </CapabilityGate>
  );
}

function SettingsOciRoute() {
  return (
    <div>
      <PageHeader wide title={t("nav.settingsOci")} subtitle={t("settings.oci.subtitle")} />
      <OciSettingsClient />
    </div>
  );
}

function SettingsAnswerNoticeRoute() {
  return (
    <div>
      <PageHeader wide title={t("nav.settingsAnswerNotice")} subtitle={t("settings.answerNotice.subtitle")} />
      <AnswerNoticeSettingsClient />
    </div>
  );
}

function SettingsTraceRecordingRoute() {
  return (
    <div>
      <PageHeader wide title={t("nav.settingsTraceRecording")} subtitle={t("settings.traceRecording.subtitle")} />
      <TraceRecordingSettingsClient />
    </div>
  );
}

function SettingsChatAttachmentsRoute() {
  return (
    <div>
      <PageHeader wide title={t("nav.settingsChatAttachments")} subtitle={t("settings.chatAttachments.subtitle")} />
      <ChatAttachmentSettingsClient />
    </div>
  );
}

function SettingsUploadStorageRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsUploadStorage")}
        subtitle={t("settings.uploadStorage.subtitle")}
      />
      <UploadStorageSettingsClient />
    </div>
  );
}

/** システムテーブル（運用設定の先頭。構成は RAG / NL2SQL と同じ。#658 / #751）。 */
function SettingsSystemTablesRoute() {
  return (
    <div>
      <PageHeader wide title={t("nav.settingsSystemTables")} subtitle={t("settings.systemTables.subtitle")} />
      <PageBody wide>
        {/* テーブルを作成済みでも、保存先がメモリなら使われない（#839）。 */}
        <NonPersistentStorageNotice />
        <SystemTablesCard />
      </PageBody>
    </div>
  );
}

function SettingsDatabaseRoute() {
  return (
    <div>
      <PageHeader wide title={t("nav.settingsDatabase")} subtitle={t("settings.database.subtitle")} />
      <DatabaseSettingsClient />
    </div>
  );
}
