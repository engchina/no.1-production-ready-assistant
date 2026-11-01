import {
  lazy,
  Suspense,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import {
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigationType,
} from "react-router-dom";

import { WorkspaceBoundary, WorkspaceDraftWarning, WorkspacePage } from "@/components/WorkspaceState";
import { useDatabaseStatus } from "@/lib/queries";

import { AppSidebar } from "@/components/layout/AppSidebar";
import { PageHeader, AppShell, PageBody, PageLoadingState } from "@production-ready/ui";
import { DatabaseGate } from "@/components/system/DatabaseGate";
import { SchemaRefreshCoordinator } from "@/features/nl2sql/SchemaRefreshCoordinator";
import { APP_ROUTES } from "@/lib/routes";
import { t } from "@/lib/i18n";
import { useAuth } from "@/features/security/AuthProvider";
import { RequireAuth, useForbiddenRedirect } from "@production-ready/system-settings";
import {
  ROUTE_PERMISSIONS,
  defaultEntryRoute,
  firstAllowedRoute,
} from "@/features/security/route-permissions";

const LoginPage = lazy(() =>
  import("@/features/security/AuthPages").then((module) => ({ default: module.LoginPage }))
);
const PasswordChangePage = lazy(() =>
  import("@/features/security/AuthPages").then((module) => ({
    default: module.PasswordChangePage,
  }))
);
const ForbiddenPage = lazy(() =>
  import("@/features/security/AuthPages").then((module) => ({
    default: module.ForbiddenPage,
  }))
);
const Nl2SqlWorkbench = lazy(() =>
  import("@/features/nl2sql/Nl2SqlWorkbench").then((module) => ({
    default: module.Nl2SqlWorkbench,
  }))
);
const SqlChatPage = lazy(() => import("@/features/nl2sql/SqlChatPage").then((module) => ({ default: module.SqlChatPage })));
const DirectSqlPage = lazy(() =>
  import("@/features/nl2sql/pages/DirectSqlPage").then((module) => ({
    default: module.DirectSqlPage,
  }))
);
const SqlToQuestionPage = lazy(() =>
  import("@/features/nl2sql/pages/SqlToQuestionPage").then((module) => ({
    default: module.SqlToQuestionPage,
  }))
);
const AdminSqlPage = lazy(() =>
  import("@/features/nl2sql/pages/AdminSqlPage").then((module) => ({
    default: module.AdminSqlPage,
  }))
);
const TableManagementPage = lazy(() =>
  import("@/features/nl2sql/pages/TableManagementPage").then((module) => ({
    default: module.TableManagementPage,
  }))
);
const ViewManagementPage = lazy(() =>
  import("@/features/nl2sql/pages/ViewManagementPage").then((module) => ({
    default: module.ViewManagementPage,
  }))
);
const DataManagementPage = lazy(() =>
  import("@/features/nl2sql/pages/DataManagementPage").then((module) => ({
    default: module.DataManagementPage,
  }))
);
const SampleDataPage = lazy(() =>
  import("@/features/nl2sql/pages/SampleDataPage").then((module) => ({
    default: module.SampleDataPage,
  }))
);
const CommentManagementPage = lazy(() =>
  import("@/features/nl2sql/pages/MetadataSqlManagementPage").then((module) => ({
    default: module.CommentManagementPage,
  }))
);
const AnnotationManagementPage = lazy(() =>
  import("@/features/nl2sql/pages/MetadataSqlManagementPage").then((module) => ({
    default: module.AnnotationManagementPage,
  }))
);
const DomainManagementPage = lazy(() =>
  import("@/features/nl2sql/pages/MetadataSqlManagementPage").then((module) => ({
    default: module.DomainManagementPage,
  }))
);
const ProfileManagementPage = lazy(() =>
  import("@/features/nl2sql/pages/ProfileManagementPage").then((module) => ({
    default: module.ProfileManagementPage,
  }))
);
const OntologyBuildPage = lazy(() =>
  import("@/features/nl2sql/pages/OntologyBuildPage").then((module) => ({
    default: module.OntologyBuildPage,
  }))
);
const GlossaryRulesPage = lazy(() =>
  import("@/features/nl2sql/pages/GlossaryRulesPage").then((module) => ({
    default: module.GlossaryRulesPage,
  }))
);
const GlobalRulesPage = lazy(() =>
  import("@/features/nl2sql/pages/GlobalRulesPage").then((module) => ({
    default: module.GlobalRulesPage,
  }))
);
const FeedbackManagementPage = lazy(() =>
  import("@/features/nl2sql/pages/FeedbackManagementPage").then((module) => ({
    default: module.FeedbackManagementPage,
  }))
);
const QuestionClassifierModelsPage = lazy(() =>
  import("@/features/nl2sql/pages/QuestionLearningPage").then((module) => ({
    default: module.QuestionClassifierModelsPage,
  }))
);
const HistoryPage = lazy(() =>
  import("@/features/nl2sql/pages/HistoryPage").then((module) => ({
    default: module.HistoryPage,
  }))
);
const EvaluationPage = lazy(() =>
  import("@/features/nl2sql/pages/EvaluationPage").then((module) => ({
    default: module.EvaluationPage,
  }))
);
const UploadStorageSettingsClient = lazy(() =>
  import("@/components/settings/UploadStorageSettingsClient").then((module) => ({
    default: module.UploadStorageSettingsClient,
  }))
);
const ModelSettingsClient = lazy(() =>
  import("@/components/settings/ModelSettingsClient").then((module) => ({
    default: module.ModelSettingsClient,
  }))
);
const DatabaseSettingsClient = lazy(() =>
  import("@/components/settings/DatabaseSettingsClient").then((module) => ({
    default: module.DatabaseSettingsClient,
  }))
);
const SystemTablesCard = lazy(() =>
  import("@/components/settings/SystemTablesCard").then((module) => ({
    default: module.SystemTablesCard,
  }))
);
const TraceRecordingSettingsClient = lazy(() =>
  import("@/components/settings/TraceRecordingSettingsClient").then((module) => ({default: module.TraceRecordingSettingsClient})),
);
const ChatAttachmentSettingsClient = lazy(() =>
  import("@/components/settings/ChatAttachmentSettingsClient").then((module) => ({
    default: module.ChatAttachmentSettingsClient,
  }))
);
const AnswerNoticeSettingsClient = lazy(() =>
  import("@/components/settings/AnswerNoticeSettingsClient").then((module) => ({
    default: module.AnswerNoticeSettingsClient,
  }))
);
const SecurityUsersPage = lazy(() =>
  import("@/features/security/SecurityUsersPage").then((module) => ({
    default: module.SecurityUsersPage,
  }))
);
const AccountRoute = lazy(() =>
  import("@/features/security/ApiKeysPages").then((module) => ({
    default: module.AccountRoute,
  }))
);
const SecurityApiKeysPage = lazy(() =>
  import("@/features/security/ApiKeysPages").then((module) => ({
    default: module.SecurityApiKeysPage,
  }))
);
const SecurityRolesPage = lazy(() =>
  import("@/features/security/SecurityRolesPage").then((module) => ({
    default: module.SecurityRolesPage,
  }))
);
const DbAdminChatPage = lazy(() =>
  import("@/features/nl2sql/DbAdminChatPage").then((module) => ({
    default: module.DbAdminChatPage,
  }))
);
const SecurityPermissionsPage = lazy(() =>
  import("@/features/security/SecurityPermissionsPage").then((module) => ({
    default: module.SecurityPermissionsPage,
  }))
);

/**
 * ナビ切替で作業中 state を破棄したくない画面。常時マウントし表示のみ切替する。
 * module 直下で JSX を一度だけ生成し、同一 instance を維持する(再マウント=state破棄を防ぐ)。
 */
const KEEP_ALIVE_PAGES = [
  { path: APP_ROUTES.chat, element: <SqlChatPage /> },
  // DB 管理チャット（#2319）も会話の履歴を持つので、チャットと同じく常時マウントする。
  { path: APP_ROUTES.dbAdminChat, element: <DbAdminChatPage /> },
  { path: APP_ROUTES.query, element: <Nl2SqlWorkbench /> },
  { path: APP_ROUTES.sqlToQuestion, element: <SqlToQuestionPage /> },
  { path: APP_ROUTES.directSql, element: <DirectSqlPage /> },
  { path: APP_ROUTES.adminSql, element: <AdminSqlPage /> },
  { path: APP_ROUTES.commentManagement, element: <CommentManagementPage /> },
  { path: APP_ROUTES.annotationManagement, element: <AnnotationManagementPage /> },
  { path: APP_ROUTES.domainManagement, element: <DomainManagementPage /> },
  { path: APP_ROUTES.tableManagement, element: <TableManagementPage /> },
  { path: APP_ROUTES.viewManagement, element: <ViewManagementPage /> },
  { path: APP_ROUTES.dataManagement, element: <DataManagementPage /> },
];
const KEEP_ALIVE_PATHS = new Set<string>(KEEP_ALIVE_PAGES.map((page) => page.path));

/** 認証画面とルートの保護が使う URL（共通の RequireAuth へ渡す。#220）。 */
const AUTH_ROUTES = {
  login: APP_ROUTES.login,
  passwordChange: APP_ROUTES.passwordChange,
  forbidden: APP_ROUTES.forbidden,
};

export function App() {
  // API の 403 は共通のイベントで受け、request ID を載せて権限なしの画面へ移す（#220）。
  useForbiddenRedirect(APP_ROUTES.forbidden);

  return (
    <Routes>
      <Route path={APP_ROUTES.login} element={<PublicRoute element={<LoginPage />} />} />
      <Route
        path={APP_ROUTES.passwordChange}
        element={<PublicRoute element={<PasswordChangePage />} />}
      />
      <Route
        path={APP_ROUTES.forbidden}
        element={<PublicRoute element={<ForbiddenPage />} />}
      />
      <Route path="*" element={<AuthenticatedApplication />} />
    </Routes>
  );
}

function PublicRoute({ element }: { element: ReactNode }) {
  return <Suspense fallback={<FullPageRouteLoadingFallback />}>{element}</Suspense>;
}

function FullPageRouteLoadingFallback() {
  return (
    // 画面の中央に浮かせず、本文の先頭に本文と同じ幅で出す（ほかの読み込み中と同じ。#2510）。
    <main className="min-h-screen bg-canvas">
      <PageLoadingState label={t("app.route.loading")} operationKey="route-loading-full" testId="route-loading" />
    </main>
  );
}

function RouteLoadingFallback() {
  return (
    <PageLoadingState label={t("app.route.loading")} operationKey="route-loading" testId="route-loading" />
  );
}

function AuthenticatedApplication() {
  const location = useLocation();
  // 確認中・未認証・強制パスワード変更・権限なしの振り分けは共通の RequireAuth（#220）。
  return (
    <RequireAuth routes={AUTH_ROUTES} requiredPermission={ROUTE_PERMISSIONS[location.pathname]}>
      <AuthorizedApplication />
    </RequireAuth>
  );
}

function AuthorizedApplication() {
  const auth = useAuth();

  return (
    <AppLayout key={JSON.stringify([auth.user?.user_uuid, auth.user?.permissions, auth.user?.allowed_profile_ids])}>
      <DatabaseGate>
        <SchemaRefreshCoordinator
          discoveryEnabled={auth.hasPermission("nl2sql.schema.read")}
        >
          <ScopedWorkspace>
          <Suspense fallback={<RouteLoadingFallback />}>
            <WorkspaceDraftWarning />
            <KeepAlivePages />
            <Routes>
            <Route path={APP_ROUTES.home} element={<Navigate to={auth.hasPermission(ROUTE_PERMISSIONS[APP_ROUTES.query]) ? APP_ROUTES.query : firstAllowedRoute(auth.hasPermission)} replace />} />
            <Route path={APP_ROUTES.sampleData} element={<SampleDataPage />} />
            {/* 旧ルート互換: スキーマ管理はテーブルの管理へ、データ投入はデータの管理へ */}
            <Route path="/schema" element={<Navigate to={APP_ROUTES.tableManagement} replace />} />
            <Route path="/data-tools" element={<Navigate to={APP_ROUTES.dataManagement} replace />} />
            {/* 実体は KeepAlivePages で常時マウント。ここでは route match だけ成立させ警告を防ぐ。 */}
            {KEEP_ALIVE_PAGES.map((page) => (
              <Route key={page.path} path={page.path} element={null} />
            ))}
            <Route path={APP_ROUTES.profiles} element={<ProfileManagementPage />} />
            <Route path={APP_ROUTES.ontologyBuild} element={<OntologyBuildPage />} />
            <Route path={APP_ROUTES.glossaryRules} element={<GlossaryRulesPage />} />
            <Route path={APP_ROUTES.globalRules} element={<GlobalRulesPage />} />
            <Route path={APP_ROUTES.feedbackManagement} element={<FeedbackManagementPage />} />
            <Route path={APP_ROUTES.learning} element={<Navigate to={APP_ROUTES.feedbackManagement} replace />} />
            <Route
              path={APP_ROUTES.questionLearning}
              element={<Navigate to={APP_ROUTES.questionClassifierModels} replace />}
            />
            <Route path={APP_ROUTES.questionClassifierModels} element={<QuestionClassifierModelsPage />} />
            <Route path={APP_ROUTES.history} element={<HistoryPage />} />
            <Route path={APP_ROUTES.evaluation} element={<EvaluationPage />} />
            <Route
              path={APP_ROUTES.settingsUploadStorage}
              element={<SettingsUploadStorageRoute />}
            />
            <Route path={APP_ROUTES.settingsModel} element={<ModelSettingsClient />} />
            <Route path={APP_ROUTES.settingsDatabase} element={<SettingsDatabaseRoute />} />
            <Route
              path={APP_ROUTES.settingsSystemTables}
              element={<SettingsSystemTablesRoute />}
            />
            <Route path={APP_ROUTES.settingsAnswerNotice} element={<SettingsAnswerNoticeRoute />} />
            <Route path={APP_ROUTES.settingsTraceRecording} element={<SettingsTraceRecordingRoute />} />
            <Route path={APP_ROUTES.settingsChatAttachments} element={<SettingsChatAttachmentsRoute />} />
            <Route path={APP_ROUTES.securityUsers} element={<SecurityUsersPage />} />
            <Route path={APP_ROUTES.securityRoles} element={<SecurityRolesPage />} />
            <Route path={APP_ROUTES.securityApiKeys} element={<SecurityApiKeysPage />} />
            <Route path={APP_ROUTES.account} element={<AccountRoute />} />
            <Route path={APP_ROUTES.securityPermissions} element={<SecurityPermissionsPage />} />
            <Route
              path={APP_ROUTES.legacyNl2sqlModelLearning}
              element={<Navigate to={APP_ROUTES.profiles} replace />}
            />
            <Route path="/settings" element={<Navigate to={APP_ROUTES.settingsUploadStorage} replace />} />
            <Route path="*" element={<Navigate to={defaultEntryRoute(auth.hasPermission)} replace />} />
            </Routes>
          </Suspense>
          </ScopedWorkspace>
        </SchemaRefreshCoordinator>
      </DatabaseGate>
    </AppLayout>
  );
}

/**
 * 対象画面を lazy-mount(初回訪問時のみ mount)し、以後は unmount せず `display` で表示を切替える。
 * 未訪問ページは描画しないので初回ロードでの eager fetch を避けられる。
 */
function ScopedWorkspace({ children }: { children: ReactNode }) {
  const database = useDatabaseStatus({ enabled: false });
  return <WorkspaceBoundary contextId={database.data?.context_id || "legacy"}>{children}</WorkspaceBoundary>;
}

function KeepAlivePages() {
  const { pathname } = useLocation();
  const auth = useAuth();
  // 訪問済みのページは state で持つ（render 中に ref を読み書きしない）。初訪問の render で追加する。
  const [visited, setVisited] = useState<ReadonlySet<string>>(() => new Set());
  let mounted = visited;
  if (
    KEEP_ALIVE_PATHS.has(pathname) &&
    auth.hasPermission(ROUTE_PERMISSIONS[pathname]) &&
    !visited.has(pathname)
  ) {
    mounted = new Set(visited).add(pathname);
    setVisited(mounted);
  }

  return (
    <>
      {KEEP_ALIVE_PAGES.filter(
        (page) =>
          mounted.has(page.path) && auth.hasPermission(ROUTE_PERMISSIONS[page.path])
      ).map((page) => (
        <WorkspacePage key={page.path} page={page.path} active={page.path === pathname}>
          <Suspense fallback={<RouteLoadingFallback />}>{page.element}</Suspense>
        </WorkspacePage>
      ))}
    </>
  );
}

function AppLayout({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigationType = useNavigationType();
  // AppShell が出力する <main id="pr-main">（スキップリンクの移動先と同じ公開 ID）をスクロール復元に使う。
  const mainRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    mainRef.current = document.getElementById("pr-main");
  });

  useEffect(() => () => { mainScrollPositions.clear(); }, []);
  useMainScrollRestoration(mainRef, location, navigationType);

  return (
    <AppShell sidebar={<AppSidebar />} skipLinkLabel={t("common.skipToMain")}>
      {children}
    </AppShell>
  );
}

type RouterLocation = ReturnType<typeof useLocation>;
type RouterNavigationType = ReturnType<typeof useNavigationType>;

const mainScrollPositions = new Map<string, number>();

function useMainScrollRestoration(
  mainRef: RefObject<HTMLElement | null>,
  location: RouterLocation,
  navigationType: RouterNavigationType
) {
  const pathnameRef = useRef(location.pathname);
  const hashRef = useRef(location.hash);
  const scrollKey = mainScrollPositionKey(location);

  useLayoutEffect(() => {
    const main = mainRef.current;
    if (!main) return;

    const save = () => {
      mainScrollPositions.set(scrollKey, main.scrollTop);
    };
    main.addEventListener("scroll", save, { passive: true });

    return () => {
      main.removeEventListener("scroll", save);
    };
  }, [mainRef, scrollKey]);

  useLayoutEffect(() => {
    const main = mainRef.current;
    if (!main) return;

    const pathnameChanged = pathnameRef.current !== location.pathname;
    const hashChanged = hashRef.current !== location.hash;
    pathnameRef.current = location.pathname;
    hashRef.current = location.hash;

    if (!pathnameChanged && !hashChanged && navigationType !== "POP") return;

    const nextTop =
      mainScrollPositions.get(scrollKey) ?? 0;
    const scroll = () => {
      if (location.hash && scrollHashTargetIntoView(location.hash)) return;
      main.scrollTo({ top: nextTop, left: 0, behavior: "auto" });
    };

    if (pathnameChanged) main.focus({ preventScroll: true });
    scroll();
    const animationFrame = window.requestAnimationFrame(scroll);
    return () => window.cancelAnimationFrame(animationFrame);
  }, [location.hash, location.pathname, mainRef, navigationType, scrollKey]);
}

function mainScrollPositionKey(location: RouterLocation) {
  return `${location.pathname}${location.search}${location.hash}`;
}

function scrollHashTargetIntoView(hash: string) {
  const id = decodeHashId(hash);
  if (!id) return false;

  const target = document.getElementById(id);
  if (!target) return false;

  target.scrollIntoView({ block: "start", inline: "nearest", behavior: "auto" });
  return true;
}

function decodeHashId(hash: string) {
  const id = hash.slice(1);
  try {
    return decodeURIComponent(id);
  } catch {
    return id;
  }
}

function SettingsTraceRecordingRoute() {
  return (
    <>
      <PageHeader wide title={t("nav.settingsTraceRecording")} subtitle={t("settings.traceRecording.subtitle")} />
      <TraceRecordingSettingsClient />
    </>
  );
}

function SettingsChatAttachmentsRoute() {
  return (
    <>
      <PageHeader wide title={t("nav.settingsChatAttachments")} subtitle={t("settings.chatAttachments.subtitle")} />
      <ChatAttachmentSettingsClient />
    </>
  );
}

function SettingsAnswerNoticeRoute() {
  return (
    <>
      <PageHeader wide title={t("nav.settingsAnswerNotice")} subtitle={t("settings.answerNotice.subtitle")} />
      <AnswerNoticeSettingsClient />
    </>
  );
}

function SettingsUploadStorageRoute() {
  return (
    <>
      <PageHeader wide
        title={t("nav.settingsUploadStorage")}
        subtitle={t("settings.uploadStorage.subtitle")}
      />
      <UploadStorageSettingsClient />
    </>
  );
}

function SettingsDatabaseRoute() {
  return (
    <>
      <PageHeader wide title={t("nav.settingsDatabase")} subtitle={t("settings.database.subtitle")} />
      <DatabaseSettingsClient />
    </>
  );
}

function SettingsSystemTablesRoute() {
  return (
    <>
      <PageHeader wide
        title={t("nav.settingsSystemTables")}
        subtitle={t("settings.systemTables.subtitle")}
      />
      <PageBody wide>
        <SystemTablesCard />
      </PageBody>
    </>
  );
}
