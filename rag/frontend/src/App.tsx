import { TraceRecordingSettingsClient } from "@/components/settings/TraceRecordingSettingsClient";
import { useLayoutEffect, useRef, type ReactNode, type RefObject } from "react";
import {
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigationType,
  useParams,
} from "react-router-dom";

import { AppShell, PageBody, PageHeader } from "@production-ready/ui";
import { RequireAuth, useForbiddenRedirect } from "@production-ready/system-settings";

import { ForbiddenPage, LoginPage, PasswordChangePage } from "@/components/security/AuthPages";
import { ragIdentityKey, useAuth } from "@/components/security/AuthProvider";
import { SecurityPermissionsPage } from "@/components/security/SecurityPermissionsPage";
import { SecurityRolesPage } from "@/components/security/SecurityRolesPage";
import { SecurityUsersPage } from "@/components/security/SecurityUsersPage";
import { AccountRoute, SecurityApiKeysPage } from "@/components/security/ApiKeysPages";
import { CardErrorBoundary } from "@/components/CardErrorBoundary";
import { DocumentCategoriesClient } from "@/components/document-categories/DocumentCategoriesClient";
import { DocumentDetailPage } from "@/components/documents/DocumentDetailPage";
import { EvaluationClient } from "@/components/evaluation/EvaluationClient";
import { FeedbackClient } from "@/components/feedback/FeedbackClient";
import { FileListClient } from "@/components/file-list/FileListClient";
import { KnowledgeBaseManagementClient } from "@/components/knowledge-bases/KnowledgeBaseManagementClient";
import { KnowledgeBaseDetailClient } from "@/components/knowledge-bases/KnowledgeBaseDetailClient";
import { SearchAnswerProfileManagementClient } from "@/components/search-answer-profiles/SearchAnswerProfileManagementClient";
import { Sidebar } from "@/components/layout/Sidebar";
import { DatabaseGate } from "@/components/system/DatabaseGate";
import { ChatClient } from "@/components/chat/ChatClient";
import { SearchClient } from "@/components/search/SearchClient";
import { RunHistoryClient, RunHistoryDetailClient } from "@/components/history/RunHistoryClient";
import { RememberedSearchParams } from "@/components/RememberedSearchParams";
import { DatabaseSettingsClient } from "@/components/settings/DatabaseSettingsClient";
import { HuggingFaceSettingsClient } from "@/components/settings/HuggingFaceSettingsClient";
import { AnswerNoticeSettingsClient } from "@/components/settings/AnswerNoticeSettingsClient";
import { ChatAttachmentSettingsClient } from "@/components/settings/ChatAttachmentSettingsClient";
import { ModelSettingsClient } from "@/components/settings/ModelSettingsClient";
import { OciSettingsClient } from "@/components/settings/OciSettingsClient";
import { ParserAdapterSettingsClient } from "@/components/settings/ParserAdapterSettingsClient";
import { ChunkingSettingsClient } from "@/components/settings/ChunkingSettingsClient";
import { LanguagePackSettingsClient } from "@/components/settings/LanguagePackSettingsClient";
import { PreprocessSettingsClient } from "@/components/settings/PreprocessSettingsClient";
import { ServicesManagementClient } from "@/components/settings/ServicesManagementClient";
import { SystemTablesCard } from "@/components/settings/SystemTablesCard";
import { RetrievalSettingsClient } from "@/components/settings/RetrievalSettingsClient";
import { AnswerPromptSettingsClient } from "@/components/settings/AnswerPromptSettingsClient";
import { GuardrailSettingsClient } from "@/components/settings/GuardrailSettingsClient";
import { VectorIndexSettingsClient } from "@/components/settings/VectorIndexSettingsClient";
import { EvaluationSettingsClient } from "@/components/settings/EvaluationSettingsClient";
import { EntityIndexSettingsClient } from "@/components/settings/EntityIndexSettingsClient";
import { GraphSettingsClient } from "@/components/settings/GraphSettingsClient";
import { PipelineHubClient } from "@/components/settings/PipelineHubClient";
import { UploadStorageSettingsClient } from "@/components/settings/UploadStorageSettingsClient";
import { UploadWorkspace } from "@/components/upload/UploadWorkspace";
import { APP_ROUTES } from "@/lib/routes";
import {
  canOpenRoute,
  defaultEntryRoute,
  firstAllowedRoute,
  settingsEntryRoute,
} from "@/lib/route-permissions";
import { t } from "@/lib/i18n";

/** 認証画面とルートの保護が使う URL（共通の RequireAuth へ渡す。#214）。 */
const AUTH_ROUTES = {
  login: APP_ROUTES.login,
  passwordChange: APP_ROUTES.passwordChange,
  forbidden: APP_ROUTES.forbidden,
};

/**
 * 削除した設定画面の URL（根拠確認・回答スタイル・高度な検索。#595）。回答の設定は「検索方法」へ移ったため、
 * ブックマークから開かれたら検索方法へ置き換えで移す（未知の URL と同じ既定の入口へは送らない）。
 */
const REMOVED_ANSWER_SETTINGS_ROUTES = [
  "/settings/grounding",
  "/settings/generation",
  "/settings/agentic",
] as const;

export function App() {
  // API の 403 は共通のイベントで受け、request ID を載せて権限なしの画面へ移す（#214）。
  useForbiddenRedirect(APP_ROUTES.forbidden);

  return (
    <Routes>
      <Route path={APP_ROUTES.login} element={<LoginPage />} />
      <Route path={APP_ROUTES.passwordChange} element={<PasswordChangePage />} />
      <Route path={APP_ROUTES.forbidden} element={<ForbiddenPage />} />
      <Route element={<ProtectedLayout />}>
        <Route path={APP_ROUTES.home} element={<HomeRedirect />} />
        {/* 旧ルート互換: 廃止したダッシュボード（#261）のブックマークは既定の入口へ */}
        <Route path="/dashboard" element={<EntryRedirect />} />
        <Route path={APP_ROUTES.upload} element={<UploadWorkspace />} />
        <Route path={APP_ROUTES.fileList} element={<FileListClient />} />
        <Route path={APP_ROUTES.knowledgeBases} element={<KnowledgeBaseManagementClient />} />
        <Route
          path={`${APP_ROUTES.knowledgeBases}/:id`}
          element={<KnowledgeBaseDetailRoute />}
        />
        <Route path={APP_ROUTES.searchAnswerProfiles} element={<SearchAnswerProfileManagementClient />} />
        <Route path={APP_ROUTES.documentCategories} element={<DocumentCategoriesClient />} />
        <Route path={`${APP_ROUTES.documents}/:id`} element={<DocumentDetailRoute />} />
        <Route path={APP_ROUTES.chat} element={<ChatClient />} />
        <Route path={APP_ROUTES.search} element={<SearchClient />} />
        {/* 実行履歴（チャット・RAG 検索の実行。#1890）。詳細は全幅（Agent の実行履歴と同じ型）。 */}
        <Route path={APP_ROUTES.history} element={<RunHistoryClient />} />
        <Route path={`${APP_ROUTES.history}/:runId`} element={<RunHistoryDetailRoute />} />
        <Route path={APP_ROUTES.evaluation} element={<EvaluationClient />} />
        <Route
          path={APP_ROUTES.feedback}
          element={
            <RememberedSearchParams field="feedback.search">
              <FeedbackClient />
            </RememberedSearchParams>
          }
        />
        <Route path={APP_ROUTES.settingsPipeline} element={<SettingsPipelineRoute />} />
        <Route path={APP_ROUTES.settingsOci} element={<SettingsOciRoute />} />
        <Route
          path={APP_ROUTES.settingsUploadStorage}
          element={<SettingsUploadStorageRoute />}
        />
        <Route
          path={APP_ROUTES.settingsParserAdapters}
          element={<SettingsParserAdaptersRoute />}
        />
        <Route path={APP_ROUTES.settingsPreprocess} element={<SettingsPreprocessRoute />} />
        <Route path={APP_ROUTES.settingsChunking} element={<SettingsChunkingRoute />} />
        <Route path={APP_ROUTES.settingsLanguagePack} element={<SettingsLanguagePackRoute />} />
        <Route path={APP_ROUTES.settingsRetrieval} element={<SettingsRetrievalRoute />} />
        {/* 削除した根拠確認・回答スタイル・高度な検索（#595）のブックマークは、回答の設定が移った検索方法へ。
            移動先の権限は移った後の URL で判定する（検索方法の権限が無ければ権限なしの画面）。 */}
        {REMOVED_ANSWER_SETTINGS_ROUTES.map((path) => (
          <Route
            key={path}
            path={path}
            element={<Navigate to={APP_ROUTES.settingsRetrieval} replace />}
          />
        ))}
        <Route path={APP_ROUTES.settingsPrompts} element={<SettingsPromptsRoute />} />
        <Route path={APP_ROUTES.settingsGuardrail} element={<SettingsGuardrailRoute />} />
        <Route path={APP_ROUTES.settingsVectorIndex} element={<SettingsVectorIndexRoute />} />
        <Route path={APP_ROUTES.settingsEvaluation} element={<SettingsEvaluationRoute />} />
        <Route path={APP_ROUTES.settingsEntityIndex} element={<SettingsEntityIndexRoute />} />
        <Route path={APP_ROUTES.settingsGraph} element={<SettingsGraphRoute />} />
        <Route path={APP_ROUTES.settingsModel} element={<ModelSettingsClient />} />
        <Route path={APP_ROUTES.settingsDatabase} element={<SettingsDatabaseRoute />} />
        <Route path={APP_ROUTES.settingsSystemTables} element={<SettingsSystemTablesRoute />} />
        <Route path={APP_ROUTES.settingsHuggingface} element={<SettingsHuggingfaceRoute />} />
        <Route path={APP_ROUTES.settingsServices} element={<SettingsServicesRoute />} />
        <Route path={APP_ROUTES.settingsAnswerNotice} element={<SettingsAnswerNoticeRoute />} />
        <Route path={APP_ROUTES.settingsTraceRecording} element={<SettingsTraceRecordingRoute />} />
        <Route path={APP_ROUTES.settingsChatAttachments} element={<SettingsChatAttachmentsRoute />} />
        <Route path={APP_ROUTES.securityUsers} element={<SecurityUsersPage />} />
        <Route path={APP_ROUTES.securityRoles} element={<SecurityRolesPage />} />
        <Route path={APP_ROUTES.securityApiKeys} element={<SecurityApiKeysPage />} />
        <Route path={APP_ROUTES.account} element={<AccountRoute />} />
        <Route path={APP_ROUTES.securityPermissions} element={<SecurityPermissionsPage />} />
        <Route path="/settings" element={<SettingsEntryRedirect />} />
        <Route path="*" element={<EntryRedirect />} />
      </Route>
    </Routes>
  );
}

/** `/` はナビの並び順で最初に開ける画面へ（NL2SQL と同じ。#261）。 */
function HomeRedirect() {
  const { hasPermission } = useAuth();
  return <Navigate to={firstAllowedRoute(hasPermission)} replace />;
}

/** 未知の URL と旧 URL は既定の入口へ（チャット、開けなければ `/` 経由で最初に開ける画面）。 */
function EntryRedirect() {
  const { hasPermission } = useAuth();
  return <Navigate to={defaultEntryRoute(hasPermission)} replace />;
}

function SettingsEntryRedirect() {
  const { hasPermission } = useAuth();
  return <Navigate to={settingsEntryRoute(hasPermission)} replace />;
}

/**
 * 認証が必要な画面の入口。確認中・未認証・強制パスワード変更の振り分けは共通の RequireAuth（#220）、
 * URL ごとの権限（詳細画面のように複数の権限のどれかで開けるものを含む）はここで判定する（#214）。
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
  return <AppLayout key={user ? ragIdentityKey(user) : ""} />;
}

function AppLayout() {
  const location = useLocation();
  const navigationType = useNavigationType();
  // AppShell が出力する <main id="pr-main">（スキップリンクの移動先と同じ公開 ID）をスクロール復元に使う。
  const mainRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    mainRef.current = document.getElementById("pr-main");
  });
  useMainScrollRestoration(mainRef, location, navigationType);

  return (
    <AppShell
      className="fixed inset-0 h-auto"
      mainClassName="min-h-0"
      skipLinkLabel={t("common.skipToMain")}
      sidebar={
        <Sidebar />
      }
    >
      <DatabaseGate>
          {/* ページ本体の描画例外を main 内に閉じ込め、サイドナビと画面遷移を維持する(#67)。
              location をキーにして、別ページへ移動したらエラー状態を自動で解除する。 */}
          <CardErrorBoundary key={location.pathname}>
            <Outlet />
          </CardErrorBoundary>
      </DatabaseGate>
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
      navigationType === "POP" ? mainScrollPositions.get(scrollKey) ?? 0 : 0;
    const scroll = () => {
      if (location.hash && scrollHashTargetIntoView(location.hash)) return;
      main.scrollTo({ top: nextTop, left: 0, behavior: "auto" });
    };

    if (pathnameChanged) main.focus({ preventScroll: true });
    // 共有設定画面などは戻ったあとに非同期で読み込み直し、高さが後から伸びる。
    // 目標位置に届くまで最大 2 秒フレームごとに再試行し、利用者が操作したら止める。
    const deadline = performance.now() + 2000;
    let animationFrame = 0;
    const restore = () => {
      scroll();
      // hash の移動先は、読み込みの後に描画されることがある（設定画面の節など。#528）。
      // 移動先が現れるまで同じ期限まで待つ。
      const settled = location.hash
        ? document.getElementById(decodeHashId(location.hash)) !== null
        : Math.abs(main.scrollTop - nextTop) <= 1;
      if (settled) return;
      if (performance.now() > deadline) return;
      animationFrame = window.requestAnimationFrame(restore);
    };
    const stop = () => window.cancelAnimationFrame(animationFrame);
    const userEvents = ["wheel", "touchstart", "keydown", "pointerdown"] as const;
    userEvents.forEach((type) => main.addEventListener(type, stop, { passive: true }));
    scroll();
    animationFrame = window.requestAnimationFrame(restore);
    return () => {
      stop();
      userEvents.forEach((type) => main.removeEventListener(type, stop));
    };
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

function DocumentDetailRoute() {
  const { id } = useParams<{ id: string }>();
  if (!id) return <Navigate to={APP_ROUTES.fileList} replace />;

  // 見出し・パンくず・状態・一覧へ戻るは詳細の PageHeader が持つ（ナレッジベース・検索・回答プロファイルと同じ構成。#581）。
  return <DocumentDetailPage documentId={id} />;
}

function KnowledgeBaseDetailRoute() {
  const { id } = useParams<{ id: string }>();
  if (!id) return <Navigate to={APP_ROUTES.knowledgeBases} replace />;

  // 見出し・パンくず・一覧へ戻る・保存は詳細の PageHeader が持つ（検索・回答プロファイルと同じ構成。#555）。
  // 対象が変わったら下書きと入力の状態を持ち越さない。
  return <KnowledgeBaseDetailClient key={id} knowledgeBaseId={id} />;
}

function RunHistoryDetailRoute() {
  const { runId } = useParams<{ runId: string }>();
  if (!runId) return <Navigate to={APP_ROUTES.history} replace />;
  return <RunHistoryDetailClient key={runId} runId={runId} />;
}

function SettingsPipelineRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsPipeline")}
        subtitle={t("settings.pipeline.subtitle")}
      />
      <PipelineHubClient />
    </div>
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

function SettingsParserAdaptersRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsParserAdapters")}
        subtitle={t("settings.parserAdapters.subtitle")}
      />
      <ParserAdapterSettingsClient />
    </div>
  );
}

function SettingsPreprocessRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsPreprocess")}
        subtitle={t("settings.preprocess.subtitle")}
      />
      <PreprocessSettingsClient />
    </div>
  );
}

function SettingsServicesRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsServices")}
        subtitle={t("settings.services.subtitle")}
      />
      <ServicesManagementClient />
    </div>
  );
}

function SettingsChunkingRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsChunking")}
        subtitle={t("settings.chunking.subtitle")}
      />
      <ChunkingSettingsClient />
    </div>
  );
}

function SettingsLanguagePackRoute() {
  return (
    <div>
      <PageHeader wide title={t("nav.settingsLanguagePack")} subtitle={t("settings.languagePack.subtitle")} />
      <LanguagePackSettingsClient />
    </div>
  );
}

function SettingsRetrievalRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsRetrieval")}
        subtitle={t("settings.retrieval.subtitle")}
      />
      <RetrievalSettingsClient />
    </div>
  );
}

function SettingsPromptsRoute() {
  return (
    <div>
      <PageHeader wide title={t("nav.settingsPrompts")} subtitle={t("settings.prompts.subtitle")} />
      <AnswerPromptSettingsClient />
    </div>
  );
}

function SettingsGuardrailRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsGuardrail")}
        subtitle={t("settings.guardrail.subtitle")}
      />
      <GuardrailSettingsClient />
    </div>
  );
}

function SettingsVectorIndexRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsVectorIndex")}
        subtitle={t("settings.vectorIndex.subtitle")}
      />
      <VectorIndexSettingsClient />
    </div>
  );
}

function SettingsEvaluationRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsEvaluation")}
        subtitle={t("settings.evaluation.subtitle")}
      />
      <EvaluationSettingsClient />
    </div>
  );
}

function SettingsEntityIndexRoute() {
  return (
    <div>
      <PageHeader wide title={t("nav.settingsEntityIndex")} subtitle={t("settings.entityIndex.subtitle")} />
      <EntityIndexSettingsClient />
    </div>
  );
}

function SettingsGraphRoute() {
  return (
    <div>
      <PageHeader wide title={t("nav.settingsGraph")} subtitle={t("settings.graph.subtitle")} />
      <GraphSettingsClient />
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

/** システムテーブル管理（運用設定。構成は NL2SQL と同じ。#658）。 */
function SettingsSystemTablesRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsSystemTables")}
        subtitle={t("settings.systemTables.subtitle")}
      />
      <PageBody wide>
        {/* カードの描画例外はカードだけに閉じ込め、見出しとナビを残す（#67）。 */}
        <CardErrorBoundary label={t("settings.database.systemTables.title")}>
          <SystemTablesCard />
        </CardErrorBoundary>
      </PageBody>
    </div>
  );
}

function SettingsHuggingfaceRoute() {
  return (
    <div>
      <PageHeader
        wide
        title={t("nav.settingsHuggingface")}
        subtitle={t("settings.huggingface.subtitle")}
      />
      <HuggingFaceSettingsClient />
    </div>
  );
}
