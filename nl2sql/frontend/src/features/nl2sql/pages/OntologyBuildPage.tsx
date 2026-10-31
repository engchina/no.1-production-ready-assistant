import {
  Button,
  Banner,
  EmptyState,
  PageHeader,
  PageBody,
  SelectField,
} from "@production-ready/ui";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useValuesChanged } from "@/lib/render-sync";
import { ListPlus, RefreshCw, Target } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { ErrorState } from "@/components/StateViews";
import { t } from "@/lib/i18n";
import { listLoadMoreErrorMessage } from "@/lib/load-more-error";
import { DbManagementLoadingSkeleton, DbObjectManagementPanelShell, DbObjectPanelHeader } from "../components/DbObjectManagementShared";
import {
  SchemaRefreshHeaderStatus,
  SchemaRefreshProcessing,
} from "../components/SchemaRefreshFeedback";
import { useSchemaRefreshCoordinator } from "../SchemaRefreshCoordinator";
import {
  useProfileDetail,
  useProfileOntologyView,
  useProfileSummaries,
} from "../incrementalQueries";
import { classifyOntologyWorkspaceError, ontologyWorkspaceErrorPresentation } from "../ontologyWorkspaceError";
import { profileDisplayLabel } from "../profileDisplay";
import { ONTOLOGY_SCHEMA_REFRESH_BUILD, OntologyBuildSection } from "../ontology/OntologyBuildSection";
import {
  ONTOLOGY_SCHEMA_REFRESH_PLAYGROUND,
  OntologyQueryPlayground,
} from "../ontology/OntologyQueryPlayground";
import type { OntologyMarkdownState } from "../ontology/types";

/**
 * 案内の中の「DB 構造を再取得」の起点。その再取得の進行と失敗は案内の中だけに出し、ページの先頭に重ねない
 * （messaging §10.2、#2034）。
 */
const ONTOLOGY_NOTICE_SCHEMA_REFRESH_ORIGINS: readonly string[] = [
  ONTOLOGY_SCHEMA_REFRESH_BUILD,
  ONTOLOGY_SCHEMA_REFRESH_PLAYGROUND,
];

/**
 * AI 構築、Markdown 下書き確認、質問の接地確認を一続きで扱う単一ページ。
 * 旧 tab URL は profile だけを残す正規 URL へ置き換える。
 */
export function OntologyBuildPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [pageError, setPageError] = useState("");
  const [markdownRefreshVersion, setMarkdownRefreshVersion] = useState(0);
  const [publishedMarkdownState, setPublishedMarkdownState] = useState<{
    profileId: string;
    revisionId: string;
  }>({ profileId: "", revisionId: "" });
  const [ontologyViewRequestedProfileId, setOntologyViewRequestedProfileId] = useState("");
  const sharedSchemaRefresh = useSchemaRefreshCoordinator();
  // 処理済みの schema refresh の終端（`<job_id>:<status>`）。
  const [handledSchemaRefreshJob, setHandledSchemaRefreshJob] = useState("");

  const profilesQuery = useProfileSummaries("");
  const activeProfiles = useMemo(
    () => profilesQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [profilesQuery.data]
  );
  const profileLoadMoreError =
    profilesQuery.isFetchNextPageError && profilesQuery.error
      ? listLoadMoreErrorMessage(profilesQuery.error, "profiles.error.load")
      : "";
  const profileParam = searchParams.get("profile");
  const selectedProfileSummary = useMemo(() => {
    if (activeProfiles.length === 0) return null;
    if (!profileParam) return activeProfiles[0];
    return (
      activeProfiles.find((profile) => profile.id === profileParam) ?? null
    );
  }, [activeProfiles, profileParam]);
  const selectedProfileId = selectedProfileSummary?.id ?? "";
  // profile が変わったレンダーで、別の profile 向けの表示要求を消す（effect で setState しない）。
  const selectedProfileChanged = useValuesChanged([selectedProfileId]);
  if (
    selectedProfileChanged &&
    ontologyViewRequestedProfileId &&
    ontologyViewRequestedProfileId !== selectedProfileId
  ) {
    setOntologyViewRequestedProfileId("");
  }
  const workspaceRequested =
    Boolean(selectedProfileId) && ontologyViewRequestedProfileId === selectedProfileId;
  const profileDetailQuery = useProfileDetail(workspaceRequested ? selectedProfileId : "");
  const ontologyViewQuery = useProfileOntologyView(selectedProfileId, workspaceRequested);
  const workspaceFetching = profileDetailQuery.isFetching || ontologyViewQuery.isFetching;
  // 「オントロジーを取得」を押して始めた取得か。スピナーは押したボタンだけが出し、定期・他の操作の後の
  // 取り直しでは回さない（領域の読込表示がスピナーを出す。#819）。
  // "initial": 初めての取得（query が有効になったレンダーから取得中になる）。取得が終わったレンダーで下ろす。
  // "refetch": 取得済みの取り直し。refetch の Promise が終わったら下ろす（取得中の通知はレンダーより遅れて届くため）。
  const [workspaceFetchPressed, setWorkspaceFetchPressed] = useState<"initial" | "refetch" | null>(null);
  if (workspaceFetchPressed === "initial" && !(workspaceRequested && workspaceFetching)) {
    setWorkspaceFetchPressed(null);
  }
  const workspaceButtonLoading =
    workspaceFetchPressed === "refetch" ||
    (workspaceFetchPressed === "initial" && workspaceRequested && workspaceFetching);
  const { refetch: refetchOntologyView } = ontologyViewQuery;
  const { refetch: refetchProfileDetail } = profileDetailQuery;
  const selectedProfile = profileDetailQuery.data?.profile ?? null;
  const loadedOntologyGraph = workspaceRequested
    ? ontologyViewQuery.data?.ontology_graph ?? null
    : null;
  const ontologyWarnings = workspaceRequested
    ? ontologyViewQuery.data?.warnings_ja ?? []
    : [];
  const publishedRevisionId = publishedMarkdownState.profileId === selectedProfileId
    ? publishedMarkdownState.revisionId : "";
  const hasPublishedOntology = Boolean(publishedRevisionId);
  // ontology-view は構築用の物理 Schema にも fallback する。公開版の存在と同一性を別に確認する。
  const publishedGraphMatches = hasPublishedOntology &&
    (loadedOntologyGraph?.revision?.id ?? loadedOntologyGraph?.revision_id) === publishedRevisionId;
  const ontologyGraph = publishedGraphMatches ? loadedOntologyGraph : null;
  const visibleOntologyWarnings = hasPublishedOntology ? ontologyWarnings : [];
  // 案内の中のボタンで始めた再取得は、進行と失敗をその案内が出す。ページの先頭には、別の起点（ほかの画面・
  // ヘッダー）の再取得の進行だけを出す（同じ処理の詳細表示は 1 つ。messaging §3.7・§10.2、#2034）。
  const noticeOwnsSchemaRefresh = ONTOLOGY_NOTICE_SCHEMA_REFRESH_ORIGINS.includes(
    sharedSchemaRefresh.isStarting ? sharedSchemaRefresh.startingOrigin : sharedSchemaRefresh.jobOrigin
  );
  const refreshing = sharedSchemaRefresh.isRefreshing && !noticeOwnsSchemaRefresh;
  useEffect(() => {
    if (!searchParams.has("tab")) return;
    const next = new URLSearchParams();
    const profileId = searchParams.get("profile");
    if (profileId) next.set("profile", profileId);
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  // fetchNextPage / refetch は query の observer ごとに固定の関数なので、deps に入れても発火の条件は変わらない。
  const { fetchNextPage: fetchNextProfilesPage, refetch: refetchProfiles } = profilesQuery;
  useEffect(() => {
    if (
      profileParam &&
      !activeProfiles.some((profile) => profile.id === profileParam) &&
      profilesQuery.hasNextPage &&
      !profilesQuery.isFetchingNextPage &&
      !profilesQuery.isFetchNextPageError
    ) {
      void fetchNextProfilesPage();
    }
  }, [activeProfiles, profileParam, profilesQuery.hasNextPage, profilesQuery.isFetchingNextPage, profilesQuery.isFetchNextPageError, fetchNextProfilesPage]);

  const refreshOntologyView = useCallback(async () => {
    if (!selectedProfileId) return;
    if (!workspaceRequested) return;
    await refetchOntologyView();
  }, [refetchOntologyView, selectedProfileId, workspaceRequested]);

  // fromFetchButton: 「オントロジーを取得」から呼んだか。取得の失敗の「再試行」から呼んだときは、
  // ボタンを回さず領域の読込表示がスピナーを出す（#819）。
  const loadOntologyView = useCallback((fromFetchButton: boolean) => {
    if (!selectedProfileId) return;
    setPageError("");
    if (workspaceRequested) {
      if (fromFetchButton) setWorkspaceFetchPressed("refetch");
      setMarkdownRefreshVersion(version => version + 1);
      void Promise.all([refetchProfileDetail(), refetchOntologyView()]).finally(() => {
        if (fromFetchButton) setWorkspaceFetchPressed(current => (current === "refetch" ? null : current));
      });
      return;
    }
    if (fromFetchButton) setWorkspaceFetchPressed("initial");
    setOntologyViewRequestedProfileId(selectedProfileId);
  }, [refetchOntologyView, refetchProfileDetail, selectedProfileId, workspaceRequested]);

  // schema refresh の終端を初めて見たレンダーで、error 表示を直す（effect で setState しない）。
  // 一覧と ontology の再取得は、終端を処理した後の effect で行う。
  const completedSchemaRefreshJob = sharedSchemaRefresh.completedJob;
  if (completedSchemaRefreshJob) {
    const reportKey = `${completedSchemaRefreshJob.job_id}:${completedSchemaRefreshJob.status}`;
    if (handledSchemaRefreshJob !== reportKey) {
      setHandledSchemaRefreshJob(reportKey);
      if (completedSchemaRefreshJob.status === "error" && !noticeOwnsSchemaRefresh) {
        setPageError(sharedSchemaRefresh.error || t("profiles.schemaRefresh.error"));
      }
    }
  }
  // 再取得は終端を処理したときだけ行う。refreshOntologyView は profile の切り替えで作り直されるので、
  // deps に入れず最新のものを ref から呼ぶ（profile を切り替えただけで ontology を取り直さない）。
  const refreshOntologyViewRef = useRef(refreshOntologyView);
  useLayoutEffect(() => { refreshOntologyViewRef.current = refreshOntologyView; });
  useEffect(() => {
    if (!handledSchemaRefreshJob.endsWith(":done")) return;
    void Promise.all([refetchProfiles(), refreshOntologyViewRef.current()]);
  }, [handledSchemaRefreshJob, refetchProfiles]);

  const selectProfile = (id: string) => {
    setPageError(""); // 前 profile のスキーマ更新エラーを持ち越さない
    setOntologyViewRequestedProfileId("");
    const next = new URLSearchParams();
    if (id) next.set("profile", id);
    setSearchParams(next, { replace: true });
  };

  const handleMarkdownStateChange = useCallback(
    (state: OntologyMarkdownState | null) => {
      setPublishedMarkdownState({
        profileId: selectedProfileId,
        revisionId: state?.published_revision?.id ?? "",
      });
    },
    [selectedProfileId]
  );
  const handleOntologyPublished = useCallback(async () => {
    await refreshOntologyView();
  }, [refreshOntologyView]);

  // 作業領域を ErrorState に置き換えるのは、業務プロファイルをまだ取得できていないときだけ。
  // 取得済みの後の取り直し（「オントロジーを取得」・タブへ戻ったときの再取得）の失敗で置き換えると、
  // 実行中の構築の進行・選んだ資料が消えるため、取得済みの内容を残して取得の操作の下に案内を出す。
  const workspaceFailure = workspaceRequested && !profileDetailQuery.data
    ? classifyOntologyWorkspaceError(profileDetailQuery.error, null)
    : null;
  const workspaceRefreshFailure =
    workspaceRequested && profileDetailQuery.data && profileDetailQuery.isError
      ? classifyOntologyWorkspaceError(profileDetailQuery.error, null)
      : null;
  const ontologyFailure = classifyOntologyWorkspaceError(
    null,
    workspaceRequested ? ontologyViewQuery.error : null
  );
  const workspaceRefreshingAfterFailure =
    Boolean(workspaceFailure) && profileDetailQuery.isFetching;
  const workspaceInitialOntologyLoading =
    workspaceRequested &&
    ontologyViewQuery.isFetching &&
    !ontologyViewQuery.data &&
    !ontologyFailure;
  const workspaceLoading =
    workspaceRequested &&
    Boolean(selectedProfileId) &&
    (profileDetailQuery.isLoading ||
      workspaceRefreshingAfterFailure ||
      workspaceInitialOntologyLoading);
  const workspaceErrorPresentation = workspaceFailure
    ? ontologyWorkspaceErrorPresentation(workspaceFailure)
    : null;
  const ontologyErrorPresentation = ontologyFailure
    ? ontologyWorkspaceErrorPresentation(ontologyFailure)
    : null;
  const workspaceErrorMessage = workspaceErrorPresentation
    ? t(workspaceErrorPresentation.key, workspaceErrorPresentation.params)
    : "";
  const workspaceRefreshErrorPresentation = workspaceRefreshFailure
    ? ontologyWorkspaceErrorPresentation(workspaceRefreshFailure)
    : null;
  const workspaceRefreshErrorMessage = workspaceRefreshErrorPresentation
    ? t(workspaceRefreshErrorPresentation.key, workspaceRefreshErrorPresentation.params)
    : "";
  const publishedGraphMismatch = hasPublishedOntology && !publishedGraphMatches;
  const ontologyErrorMessage = publishedGraphMismatch && !ontologyErrorPresentation
    ? t("ontologyPlayground.publishedGraphMismatch")
    : ontologyErrorPresentation
    ? t(ontologyErrorPresentation.key, ontologyErrorPresentation.params)
    : "";
  const handleWorkspaceRetry = useCallback(() => {
    void Promise.all([refetchProfileDetail(), refetchOntologyView()]);
  }, [refetchOntologyView, refetchProfileDetail]);
  const ontologyLoadState = !workspaceRequested
    ? "not_loaded"
    : ontologyViewQuery.isFetching && (!ontologyViewQuery.data || publishedGraphMismatch)
      ? "loading"
      : ontologyFailure || publishedGraphMismatch
        ? "error"
        : "ready";
  return (
    <>
      <PageHeader wide
        title={t("nav.ontologyBuild")}
        subtitle={t("ontologyBuild.subtitle")}
        status={<SchemaRefreshHeaderStatus testId="ontology-build-schema-refresh-status" />}
      />
      <PageBody wide className="grid min-w-0 gap-4">
        {pageError ? <Banner severity="danger">{pageError}</Banner> : null}
        {refreshing ? (
          <SchemaRefreshProcessing testId="ontology-build-schema-refresh-processing" />
        ) : null}

        <DbObjectManagementPanelShell
          id="ontology-profile-panel"
          role="region"
          ariaLabel={t("ontologyBuild.profile.label")}
          idPrefix="ontology-profile"
        >
          <DbObjectPanelHeader
            icon={Target}
            title={t("ontologyBuild.profile.label")}
            description={t("ontologyBuild.profile.hint")}
          />
          {profilesQuery.isLoading ? (
            <DbManagementLoadingSkeleton
              idPrefix="ontology-profile"
              ariaLabel={t("ontologyBuild.profile.loading")}
              variant="compact"
            />
          ) : profilesQuery.isError && !profilesQuery.data ? (
            // 一覧を 1 件も取得できていないときだけ置き換える。追加読み込み（fetchNextPage）の失敗も
            // isError になるが、そのときは読み込み済みの選択欄を残し、下の追加読み込みの案内で再試行する。
            <ErrorState
              message={t("profiles.error.load")}
              onRetry={() => void profilesQuery.refetch()}
            />
          ) : activeProfiles.length === 0 ? (
            <EmptyState title={t("ontologyBuild.empty.title")} hint={t("ontologyBuild.empty.hint")} />
          ) : (
            <div className="grid gap-3">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
                <SelectField
                  id="ontology-build-profile"
                  label={t("ontologyBuild.profile.selectLabel")}
                  value={selectedProfileId}
                  placeholder={t("nl2sql.workspace.profileUnavailable")}
                  options={activeProfiles.map((profile) => ({ value: profile.id, label: profileDisplayLabel(profile) }))}
                  onValueChange={selectProfile}
                  data-testid="ontology-build-profile-select"
                  // grid の外で操作ボタンと並べる単独の選択欄。対象の選択（業務プロファイル）は lg（#613 / #1891）。
                  width="lg"
                  className="min-w-0"
                />
                {profilesQuery.hasNextPage ? (
                  <Button icon={ListPlus}
                    type="button"
                    variant="secondary"
                    size="sm"
                    // 読み込みに失敗した後は、案内の「再試行」だけを回す（同じ処理のスピナーは 1 つ。#416）。
                    loading={profilesQuery.isFetchingNextPage && !profileLoadMoreError}
                    disabled={profilesQuery.isFetchingNextPage && Boolean(profileLoadMoreError)}
                    onClick={() => void profilesQuery.fetchNextPage()}
                  >
                    {t("profiles.action.loadMore")}
                  </Button>
                ) : null}
                <div className="sm:ml-auto">
                  <Button
                    type="button"
                    variant="primary"
                    size="lg"
                    className="w-full sm:w-auto"
                    loading={workspaceButtonLoading}
                    disabled={!selectedProfileId || profileDetailQuery.isLoading}
                    data-testid="ontology-view-fetch"
                    onClick={() => loadOntologyView(true)} icon={RefreshCw}>
                    <span>{t("ontologyBuild.workspace.fetchAction")}</span>
                  </Button>
                </div>
              </div>
              {profileParam && !selectedProfileId && !profilesQuery.hasNextPage && (
                <Banner severity="danger">{t("profiles.error.notFound")}</Banner>
              )}
              {workspaceRefreshErrorMessage ? (
                <Banner
                  severity="danger"
                  action={
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      className="w-full sm:w-auto"
                      loading={profileDetailQuery.isFetching && !workspaceButtonLoading}
                      disabled={workspaceButtonLoading}
                      onClick={handleWorkspaceRetry}
                      icon={RefreshCw}
                    >
                      <span>{t("common.retry")}</span>
                    </Button>
                  }
                >
                  {workspaceRefreshErrorMessage}
                </Banner>
              ) : null}
              {profileLoadMoreError ? (
                <div>
                  <Banner
                    severity="danger"
                    action={
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      className="w-full sm:w-auto"
                      loading={profilesQuery.isFetchingNextPage}
                      onClick={() => void profilesQuery.fetchNextPage()} icon={RefreshCw}>
                      <span>{t("common.retry")}</span>
                    </Button>
                    }
                  >
                    {profileLoadMoreError}
                  </Banner>
                </div>
              ) : null}
            </div>
          )}
        </DbObjectManagementPanelShell>

        {selectedProfileId && !workspaceRequested ? (
          <DbObjectManagementPanelShell
            id="ontology-workspace-not-loaded"
            role="region"
            ariaLabel={t("ontologyBuild.workspace.notLoadedTitle")}
            idPrefix="ontology-workspace-not-loaded"
          >
            <EmptyState
              title={t("ontologyBuild.workspace.notLoadedTitle")}
              hint={t("ontologyBuild.workspace.notLoadedHint")}
            />
          </DbObjectManagementPanelShell>
        ) : workspaceLoading ? (
          <DbManagementLoadingSkeleton
            idPrefix="ontology-workspace"
            ariaLabel={t("ontologyBuild.workspace.loading")}
            variant="detail"
            operationKey={selectedProfileId}
            testId="ontology-workspace-loading"
            // 直上の「オントロジーを取得」ボタンの loading がスピナーを出す（同じ処理のスピナーは 1 つ。
            // messaging §3.7、#416）。
            activityIcon={workspaceButtonLoading ? "none" : "spinner"}
          />
        ) : workspaceFailure ? (
          <ErrorState
            message={workspaceErrorMessage}
            onRetry={handleWorkspaceRetry}
          />
        ) : selectedProfileId ? (
          <>
            <OntologyBuildSection
              profileId={selectedProfileId}
              profileLabel={
                selectedProfileSummary ? profileDisplayLabel(selectedProfileSummary) : ""
              }
              hasProfileSchemaInput={
                (selectedProfile?.allowed_tables?.length ?? 0) > 0 ||
                (selectedProfile?.allowed_views?.length ?? 0) > 0
              }
              markdownRefreshVersion={markdownRefreshVersion}
              workspaceFetching={workspaceButtonLoading}
              onPublished={handleOntologyPublished}
              onMarkdownStateChange={handleMarkdownStateChange}
            />
            <OntologyQueryPlayground
              key={selectedProfileId}
              graph={ontologyGraph}
              profileId={selectedProfileId}
              warningsJa={visibleOntologyWarnings}
              loadState={ontologyLoadState}
              workspaceFetching={workspaceButtonLoading}
              loadErrorMessage={ontologyErrorMessage}
              onRetryLoad={() => loadOntologyView(false)}
            />
          </>
        ) : null}
      </PageBody>
    </>
  );
}
