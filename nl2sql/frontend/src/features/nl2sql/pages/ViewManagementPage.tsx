import { WarningsBanner } from "@/components/WarningsBanner";
import {
  useWorkspaceState,
  useWorkspaceRevalidation,
  useResetExecutionConsent,
  useWorkspaceActive,
  useWorkspaceUrlSelection,
} from "@/components/WorkspaceState";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Code2, Eye, RefreshCw, Sparkles } from "lucide-react";

import {
  Banner,
  Button,
  Disclosure,
  EmptyState,
  toast,
  StatusBadge,
  PageHeader,
  PageBody,
  ContentActionBar,
  ProcessingIndicator,
  TextareaField,
  useActionPending,
} from "@production-ready/ui";

import { RetryPageNotice } from "@/components/page-notice";
import { apiFetch, apiPost, isTimeoutError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t } from "@/lib/i18n";
import { listLoadMoreErrorMessage } from "@/lib/load-more-error";
import { API_TIMEOUT_MS, requestTimeoutSeconds } from "@/lib/requestPolicy";
import {
  DbManagementLoadingSkeleton,
  DbObjectDetailPanel,
  DbObjectListDetail,
  DbObjectManagementPanelShell,
  DbObjectPanelHeader,
  DbObjectStepIndicator,
  DropDbObjectDialog,
  dbAdminExecuteFailureMessage,
  formatDbObjectName,
  compareDbObjectSortValues,
  dbObjectSortValue,
  parseDbAdminObjectTarget,
  type DbObjectDetailTab,
  type DbObjectOwnerPrefix,
  type DbObjectSortKey,
  type DbObjectSortState,
} from "../components/DbObjectManagementShared";
import { DbObjectName } from "../components/DbObjectName";
import { StatementRunnerCard, downloadBlob } from "../components/DbAdminShared";
import type {
  DbAdminExecuteData,
  DbAdminJoinWhereData,
  DbAdminJoinWherePromptProfile,
  DbAdminObjectDetail,
} from "../types";
import { useDbAdminObjects } from "../incrementalQueries";
import { useSchemaRefreshCoordinator } from "../SchemaRefreshCoordinator";
import {
  SchemaRefreshHeaderStatus,
  SchemaRefreshProcessing,
  SchemaSyncNotice,
} from "../components/SchemaRefreshFeedback";
import { useMutationSchemaSync } from "../useMutationSchemaSync";
import { dbAdminObjectCountsFromPage } from "../dbAdminObjectCounts";
import { useDbObjectDetailRequest } from "../useDbObjectDetailRequest";

/** ヘッダーの「スキーマを更新」の起点（SchemaRefreshCoordinator の start(origin)。#821）。 */
const VIEW_SCHEMA_REFRESH_HEADER = "view-management-header";
/** 一覧の操作（削除）の後の、ページの先頭の案内の「DB 構造を再取得」の起点（#2031）。 */
const VIEW_SCHEMA_REFRESH_NOTICE = "view-management-notice";
/** 作成の結果の直下の案内の「DB 構造を再取得」の起点（#2031）。 */
const VIEW_CREATE_SCHEMA_REFRESH_NOTICE = "view-management-create-notice";

type ActiveView = "list" | "create" | "joinWhere";

const VIEW_MANAGEMENT_ID = "view-management";
const JOIN_WHERE_PROMPT_PROFILE: DbAdminJoinWherePromptProfile = "sql_structure";
const JOIN_WHERE_OUTPUT_SCOPE_KEYS = ["join", "where", "structure"] as const;

function joinWherePromptProfileLabel() {
  return t("viewMgmt.joinWhere.profile.sqlStructure");
}

function joinWhereSourceLabel(source: string) {
  return source === "enterprise_ai"
    ? t("viewMgmt.joinWhere.source.enterpriseAi")
    : t("viewMgmt.joinWhere.source.deterministic");
}

function joinWherePromptProfileDescription() {
  return t("viewMgmt.joinWhere.profile.sqlStructureHint");
}

function ViewJoinWherePanel({
  detail,
  result,
  loading,
  ddlLoading,
  ddlError,
  extractError,
  onExtract,
  onRetryDdl,
}: {
  detail: DbAdminObjectDetail | null;
  result: DbAdminJoinWhereData | null;
  loading: boolean;
  ddlLoading: boolean;
  ddlError: string;
  /** 抽出の失敗。抽出のボタンの直下に出す（messaging.md §10.1、#724）。 */
  extractError: string;
  onExtract: () => void;
  onRetryDdl: () => void;
}) {
  const ddlStatusId = "view-join-where-ddl-status";
  const ddlReady = Boolean(detail?.ddl);
  const ddlStatus = !detail
    ? t("viewMgmt.joinWhere.ddlStatus.noView")
    : ddlLoading
      ? t("viewMgmt.joinWhere.ddlStatus.loading")
      : ddlError
        ? t("viewMgmt.joinWhere.ddlStatus.failed", { message: ddlError })
        : ddlReady
          ? t("viewMgmt.joinWhere.ddlStatus.ready")
          : t("viewMgmt.joinWhere.ddlStatus.missing");

  return (
    <div className="grid gap-4">
      <DbObjectPanelHeader
        icon={Sparkles}
        title={t("viewMgmt.joinWhere.title")}
        description={t("viewMgmt.joinWhere.subtitle")}
      />

      <DbObjectStepIndicator
        steps={[t("viewMgmt.joinWhere.stepSelect"), t("viewMgmt.joinWhere.stepExtract")]}
        activeIndex={result ? 2 : detail ? 0 : -1}
        ariaLabel={t("viewMgmt.joinWhere.steps")}
        dataTestId="view-join-where-steps"
      />

      {detail ? (
        <section
          className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3"
          data-testid="view-join-where-selected-view"
        >
          <div className="flex flex-wrap items-center gap-2">
            <DbObjectName object={detail} size="sm" data-testid="view-join-where-selected-view-name" />
            <StatusBadge icon={false} variant="neutral" label={detail.object_type} />
          </div>
          <p className="text-sm text-fg-muted">{t("viewMgmt.joinWhere.selectedHint")}</p>
        </section>
      ) : (
        <EmptyState title={t("viewMgmt.joinWhere.emptyTitle")} hint={t("viewMgmt.joinWhere.empty")} />
      )}

      <section
        aria-label={t("viewMgmt.joinWhere.advancedSettings")}
        className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3"
        data-testid="view-join-where-advanced-settings"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="grid min-w-0 gap-1">
            <h3 className="text-sm font-semibold text-fg">
              {t("viewMgmt.joinWhere.advancedSettings")}
            </h3>
            <p className="max-w-prose text-xs leading-5 text-fg-muted">
              {t("viewMgmt.joinWhere.advancedSettingsHint")}
            </p>
          </div>
          <StatusBadge icon={false} variant="info" label={joinWherePromptProfileLabel()} />
        </div>
        <div className="grid gap-2 rounded-md border border-border bg-surface p-3">
          <p className="text-sm font-medium text-fg">
            {t("viewMgmt.joinWhere.profileLabel")}
          </p>
          <p className="max-w-prose text-xs leading-5 text-fg-muted">
            {joinWherePromptProfileDescription()}
          </p>
          <div
            role="list"
            className="flex flex-wrap gap-2"
            aria-label={t("viewMgmt.joinWhere.outputScope")}
          >
            {JOIN_WHERE_OUTPUT_SCOPE_KEYS.map((scope) => (
              <span
                key={scope}
                data-surface-tint="info"
                role="listitem"
                className="inline-flex min-h-8 items-center rounded-full border border-info-border bg-info-subtle px-3 text-xs font-medium text-info-fg"
              >
                {t(`viewMgmt.joinWhere.outputScope.${scope}`)}
              </span>
            ))}
          </div>
        </div>
      </section>

      <ContentActionBar
        ariaLabel={t("viewMgmt.joinWhere.actions")}
        title={t("viewMgmt.joinWhere.ddlStatus.title")}
        description={
          <span
            id={ddlStatusId}
            className={ddlError ? "text-warning-fg" : undefined}
            aria-live={ddlLoading || ddlError ? "polite" : undefined}
          >
            {ddlStatus}
          </span>
        }
        actionsClassName="w-full sm:w-auto"
        testId="view-join-where-actions"
      >
        {detail && ddlError ? (
          <Button
            type="button"
            variant="secondary"
            size="lg"
            className="w-full sm:w-auto"
            disabled={ddlLoading}
            onClick={onRetryDdl} icon={RefreshCw}>
            <span>{t("viewMgmt.joinWhere.ddlRetry")}</span>
          </Button>
        ) : null}
        <Button
          type="button"
          variant="secondary"
          size="lg"
          className="w-full sm:w-auto"
          loading={loading}
          disabled={!ddlReady || ddlLoading}
          aria-describedby={ddlStatusId}
          onClick={onExtract} icon={Sparkles}>
          <span>{t("viewMgmt.joinWhere.extract")}</span>
        </Button>
      </ContentActionBar>

      {!loading && extractError ? <Banner severity="danger">{extractError}</Banner> : null}
      {loading ? (
        <DbManagementLoadingSkeleton
          idPrefix="view-join-where-result"
          ariaLabel={t("viewMgmt.joinWhere.loading")}
          variant="detail"
          placement="result"
        />
      ) : result ? (
        <section className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3 text-sm" aria-label={t("viewMgmt.joinWhere.result")}>
          <div className="flex flex-wrap gap-2">
            {/* 抽出の方式は API の内部値（enterprise_ai / deterministic）を出さず、文言で出す（#934）。 */}
            <StatusBadge
              icon={false}
              variant={result.source === "enterprise_ai" ? "success" : "neutral"}
              label={joinWhereSourceLabel(result.source)}
            />
            <StatusBadge
              icon={false}
              variant="info"
              label={joinWherePromptProfileLabel()}
            />
          </div>
          <WarningsBanner warnings={result.warnings} />
          <div className="grid gap-3 lg:grid-cols-2">
            <TextareaField
              id="view-join-where-join-result"
              label={t("viewMgmt.joinWhere.join")}
              readOnly
              value={result.join_text}
              rows={5}
              monospace
              width="full"
            />
            <TextareaField
              id="view-join-where-where-result"
              label={t("viewMgmt.joinWhere.where")}
              readOnly
              value={result.where_text}
              rows={5}
              monospace
              width="full"
            />
          </div>
          {result.structure_markdown ? (
            <Disclosure summary={t("viewMgmt.joinWhere.structureResult")}>
              <pre data-surface="code" className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-surface p-3 font-mono text-sm leading-6 text-fg">
                {result.structure_markdown}
              </pre>
            </Disclosure>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

function objectListErrorMessage(error: unknown, fallbackKey: Parameters<typeof t>[0]) {
  if (isTimeoutError(error)) {
    return t("dataMgmt.objectList.timeout", {
      seconds: requestTimeoutSeconds(API_TIMEOUT_MS.interactiveList),
    });
  }
  return error instanceof Error ? error.message : t(fallbackKey);
}

export function ViewManagementPage() {
  useWorkspaceRevalidation();
  const [detailTab, setDetailTab] = useWorkspaceState<DbObjectDetailTab>("detailTab", "columns");
  const [activeView, setActiveView] = useWorkspaceState<ActiveView>("activeView", "list");
  const [viewSearch, setViewSearch] = useWorkspaceState("viewSearch", "");
  const [viewOwnerPrefix, setViewOwnerPrefix] = useWorkspaceState<DbObjectOwnerPrefix>("viewOwnerPrefix", "");
  const [viewSort, setViewSort] = useWorkspaceState<DbObjectSortState>("viewSort", { key: "name", direction: "asc" });
  const [dropTargetName, setDropTargetName] = useState("");
  const [dropConfirmation, setDropConfirmation] = useState("");
  const [dropError, setDropError] = useState("");
  useResetExecutionConsent(() => { setDropConfirmation(""); setDropTargetName(""); }, "");
  const [joinWhere, setJoinWhere] = useState<DbAdminJoinWhereData | null>(null);
  const joinWhereRequest = useRef(0);
  useEffect(() => () => { joinWhereRequest.current += 1; }, []);
  const [loading, setLoading] = useState("");
  // ヘッダーの「最新情報を取得」を押した取り直しの間だけ true（検索・絞り込み・他の操作の後の取り直しでは回さない。#819）。
  const manualRefresh = useActionPending();
  // 失敗の案内の「再試行」。案内を残したまま押したボタンだけを回し、進行は案内の中に出す（#2034）。
  const noticeRetry = useActionPending();
  const [message, setMessage] = useState("");
  const [joinWhereError, setJoinWhereError] = useState("");
  const autoJoinWhereDdlName = useRef("");
  const sharedSchemaRefresh = useSchemaRefreshCoordinator();
  // 検索語・所有者の接頭辞は SearchField が確定した値（入力が止まって 300ms・Enter・消去。IME の変換中は
  // 確定しない）なので、ここでは遅延させずにそのまま問い合わせに使う（#535）。
  const viewObjectsQuery = useDbAdminObjects(
    viewSearch,
    "view",
    "all",
    viewOwnerPrefix,
    "name_comment"
  );
  const schemaRefreshing = sharedSchemaRefresh.isRefreshing;
  const headerSchemaRefreshStarting = sharedSchemaRefresh.startingOrigin === VIEW_SCHEMA_REFRESH_HEADER;
  const viewItems = useMemo(
    () => (viewObjectsQuery.data?.pages ?? []).flatMap((page) => page.items),
    [viewObjectsQuery.data]
  );
  const firstViewPage = viewObjectsQuery.data?.pages[0];
  const totalViewCount = dbAdminObjectCountsFromPage(firstViewPage, viewItems).totalCount;
  // 絞り込む前の全件（「N 件（全 M 件）」の M。#1891）。絞り込みの無いときは上の問い合わせと同じ queryKey なので
  // キャッシュを共有し、追加の要求は出ない。
  const allViewObjectsQuery = useDbAdminObjects("", "view", "all", "", "name_comment");
  const unfilteredViewCount = allViewObjectsQuery.data
    ? dbAdminObjectCountsFromPage(
        allViewObjectsQuery.data.pages[0],
        allViewObjectsQuery.data.pages.flatMap((page) => page.items)
      ).totalCount
    : undefined;
  const detailRequest = useDbObjectDetailRequest({
    collectionPath: "/api/nl2sql/db-admin/views",
    loadErrorMessage: t("viewMgmt.error.detail"),
    timeoutErrorMessage: t("dbAdmin.detail.timeout", {
      seconds: requestTimeoutSeconds(API_TIMEOUT_MS.interactiveDetail),
    }),
  });
  const {
    detail,
    ddlLoading: detailDdlLoading,
    ddlError: detailDdlError,
    loadDdl: loadDetailDdl,
    clear: clearDetail,
  } = detailRequest;
  // 選んだビューは URL の `?view=` が唯一の情報源（#2281）。先頭の行を自動で選ばない。
  const selection = useWorkspaceUrlSelection("view");
  const selectedViewName = selection.selectedId ?? "";

  const fetchDetail = async (name: string) => {
    joinWhereRequest.current += 1;
    setLoading((current) => current === "join-where" ? "" : current);
    autoJoinWhereDdlName.current = "";
    setDetailTab("columns");
    setJoinWhere(null);
    // 別のビューを選んだら、前のビューの抽出の失敗を消す（messaging.md §10.4）。
    setJoinWhereError("");
    await detailRequest.load(name);
  };

  // DDL は重い GET_DDL を伴うため列タブでは取得せず、DDL タブ初回表示時に後追いで取得する。
  const handleDetailTabChange = (nextTab: DbObjectDetailTab) => {
    setDetailTab(nextTab);
    if (nextTab !== "ddl" || !detail || detail.ddl) return;
    void detailRequest.loadDdl(formatDbObjectName(detail));
  };

  const returnToList = () => {
    setActiveView("list");
    setDetailTab("columns");
  };

  // 一覧を取り直し、失敗を message に出す（message は呼び出し側で空にしておく）。
  // state の更新は応答の callback の中だけで行う（effect からも呼ぶため）。
  // refetch は query の observer ごとに固定の関数なので、この関数も固定になる（終端の effect の発火の条件は変わらない）。
  const { refetch: refetchViewObjects } = viewObjectsQuery;
  const refetchObjects = useCallback((announce = false) =>
    refetchViewObjects().then((result) => {
      if (result.error) {
        setMessage(result.error instanceof Error ? result.error.message : t("viewMgmt.error.load"));
        return;
      }
      // 失敗の案内の「再試行」で取り直せたら、案内を消す（#2034）。
      setMessage("");
      if (announce) {
        toast.success(t("common.action.refreshed"));
      }
    }), [refetchViewObjects]);

  const refreshObjects = async (announce = false) => {
    setMessage("");
    await refetchObjects(announce);
  };

  // 変更の後の DB 構造の同期。一覧の操作（削除）はページの先頭、作成は結果の直下に案内と進行を 1 か所だけ
  // 出す。案内の中の「DB 構造を再取得」の進行は、そのボタンの直下に出す（#2031）。
  const schemaSync = useMutationSchemaSync({
    noticeOrigin: VIEW_SCHEMA_REFRESH_NOTICE,
    onDone: refetchObjects,
  });
  const createSchemaSync = useMutationSchemaSync({
    noticeOrigin: VIEW_CREATE_SCHEMA_REFRESH_NOTICE,
    onDone: refetchObjects,
  });

  // loadDdl は detail を deps に持つ useCallback なので、detail が変わるたびに作り直される。
  // deps を detail?.name / detail?.ddl から detail に広げても、発火の条件は元と同じ。
  useEffect(() => {
    if (
      activeView !== "joinWhere" ||
      !detail ||
      detail.ddl ||
      detailDdlLoading ||
      detailDdlError ||
      autoJoinWhereDdlName.current === formatDbObjectName(detail)
    ) {
      return;
    }
    autoJoinWhereDdlName.current = formatDbObjectName(detail);
    void loadDetailDdl(formatDbObjectName(detail));
  }, [
    activeView,
    detail,
    detailDdlLoading,
    detailDdlError,
    loadDetailDdl,
  ]);

  const reloadAfterMutation = (result: {
    schema_refresh_job_id?: string;
    schema_refresh_required?: boolean;
    schema_refresh_reason_code?: string;
  }) => {
    if (activeView === "create") createSchemaSync.track(result);
    else schemaSync.track(result, { scope: "page" });
    void refreshObjects();
  };

  const filteredViews = useMemo(() => {
    const q = viewSearch.trim().toLowerCase();
    const ownerPrefixKey = viewOwnerPrefix.trim().toUpperCase();
    return viewItems
      .filter((item) => {
        if (ownerPrefixKey && !item.owner.toUpperCase().startsWith(ownerPrefixKey)) return false;
        if (!q) return true;
        return (
          item.name.toLowerCase().includes(q) ||
          item.comment.toLowerCase().includes(q)
        );
      })
      .sort((left, right) => {
        return compareDbObjectSortValues(
          dbObjectSortValue(left, viewSort.key),
          dbObjectSortValue(right, viewSort.key),
          viewSort.direction
        );
      });
  }, [viewItems, viewOwnerPrefix, viewSearch, viewSort]);

  // 選択（URL）が変わったら詳細を取り直し（抽出の結果も消す）、外れたら詳細を消す。画面が表に戻ったとき（keep-alive）は
  // 詳細だけを取り直す。fetchDetail は毎レンダー作り直すので、最新のものを ref から呼ぶ。
  const fetchDetailRef = useRef(fetchDetail);
  useLayoutEffect(() => { fetchDetailRef.current = fetchDetail; });
  const workspaceActive = useWorkspaceActive();
  const lastSelectedRef = useRef("");
  const { load: loadDetail } = detailRequest;
  useEffect(() => {
    if (!workspaceActive) return;
    const changed = lastSelectedRef.current !== selectedViewName;
    lastSelectedRef.current = selectedViewName;
    if (!selectedViewName) {
      clearDetail();
      return;
    }
    if (changed) void fetchDetailRef.current(selectedViewName);
    else void loadDetail(selectedViewName);
  }, [workspaceActive, selectedViewName, clearDetail, loadDetail]);

  const toggleSort = (key: DbObjectSortKey) => {
    setViewSort((current) => ({
      key,
      direction: current.key === key && current.direction === "asc" ? "desc" : "asc",
    }));
  };

  const downloadColumnsXlsx = async (name: string) => {
    const target = parseDbAdminObjectTarget(name);
    const params = new URLSearchParams();
    if (target.owner) params.set("owner", target.owner);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    setLoading("view-export");
    setMessage("");
    try {
      const response = await apiFetch(`/api/nl2sql/db-admin/views/${encodeURIComponent(target.name)}/export.xlsx${suffix}`, {
        headers: {
          Accept: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
      });
      if (!response.ok) {
        throw new Error(t("viewMgmt.error.export"));
      }
      downloadBlob(`${target.qualifiedName.toLowerCase().replace(".", "_")}_columns.xlsx`, await response.blob());
      toast.success(t("common.action.downloaded"));
    } catch (err) {
      setMessage(err instanceof Error ? err.message : t("viewMgmt.error.export"));
    } finally {
      setLoading("");
    }
  };

  const openDropDialog = (name: string) => {
    setDropTargetName(name);
    setDropConfirmation("");
    setDropError("");
  };

  const dropView = async () => {
    if (!dropTargetName) return;
    const target = parseDbAdminObjectTarget(dropTargetName);
    setLoading("drop");
    setMessage("");
    setDropError("");
    try {
      const result = await apiPost<DbAdminExecuteData>("/api/nl2sql/db-admin/drop-view", {
        view_name: target.name,
        owner: target.owner,
        confirmation: dropConfirmation,
        reason: "ui-view-management-drop",
      });
      if (result.executed) {
        const dropped = dropTargetName;
        setDropTargetName("");
        setDropConfirmation("");
        setDropError("");
        toast.success(t("viewMgmt.drop.success", { name: dropped }));
        // 削除した対象を選んでいたら選択を外す（詳細の空の状態・1 ペインは一覧へ）。
        if (dropped === selectedViewName) selection.replace(null);
        await reloadAfterMutation(result);
        return;
      }
      setDropError(dbAdminExecuteFailureMessage(result, t("viewMgmt.error.drop")));
    } catch (err) {
      setDropError(err instanceof Error ? err.message : t("viewMgmt.error.drop"));
    } finally {
      setLoading("");
    }
  };

  const extractJoinWhere = async () => {
    if (!detail?.ddl || loading === "join-where") return;
    const sequence = ++joinWhereRequest.current;
    setLoading("join-where");
    setJoinWhereError("");
    setJoinWhere(null);
    try {
      const result = await apiPost<DbAdminJoinWhereData>("/api/nl2sql/db-admin/extract-join-where", {
          ddl: detail.ddl,
          prompt_profile: JOIN_WHERE_PROMPT_PROFILE,
        });
      if (sequence === joinWhereRequest.current) setJoinWhere(result);
    } catch (err) {
      if (sequence === joinWhereRequest.current) {
        setJoinWhereError(err instanceof Error ? err.message : t("viewMgmt.error.extract"));
      }
    } finally {
      if (sequence === joinWhereRequest.current) setLoading("");
    }
  };

  const taskContent =
    activeView === "create" ? (
      <StatementRunnerCard
        policy="view_ddl"
        title={t("viewMgmt.create.title")}
        description={t("viewMgmt.create.note")}
        placeholder={t("viewMgmt.create.placeholder")}
        progress={({ hasSql }) => (
          <DbObjectStepIndicator
            steps={[t("viewMgmt.create.stepSql"), t("viewMgmt.create.stepExecute")]}
            activeIndex={hasSql ? 1 : 0}
            ariaLabel={t("viewMgmt.create.steps")}
            dataTestId="view-create-steps"
          />
        )}
        confirmationTitle={t("viewMgmt.create.executeTitle")}
        footerProcessing={
          <SchemaSyncNotice
            sync={createSchemaSync}
            testId="view-create-schema-sync-notice"
            processingTestId="view-create-schema-refresh-processing"
            fallbackProgress={!schemaSync.ownsProgress}
          />
        }
        executeOnly
        framed={false}
        onExecuted={reloadAfterMutation}
      />
    ) : activeView === "joinWhere" ? (
      <ViewJoinWherePanel
        detail={detail}
        result={joinWhere}
        loading={loading === "join-where"}
        ddlLoading={detailRequest.ddlLoading}
        ddlError={detailRequest.ddlError}
        extractError={joinWhereError}
        onExtract={() => void extractJoinWhere()}
        onRetryDdl={() => {
          if (detail) {
            autoJoinWhereDdlName.current = "";
            void detailRequest.loadDdl(formatDbObjectName(detail));
          }
        }}
      />
    ) : null;

  return (
    <>
      <PageHeader wide
        title={t("nav.viewManagement")}
        subtitle={t("viewMgmt.subtitle")}
        meta={
          firstViewPage?.refreshed_at
            ? t("common.schemaRefreshedAt", { date: formatDateTime(firstViewPage.refreshed_at) })
            : undefined
        }
        status={<SchemaRefreshHeaderStatus testId="view-schema-refresh-status" />}
        // 作業（作成）の画面と、1 ペイン（xl 未満）の詳細の「一覧へ戻る」はページの左上（#618・#2281）。
        back={
          activeView !== "list"
            ? { label: t("viewMgmt.action.backToList"), onClick: returnToList, testId: "view-management-back" }
            : !selection.split && selectedViewName
              ? { label: t("viewMgmt.action.backToList"), onClick: selection.clear, testId: "view-management-detail-back" }
              : undefined
        }
        actionsLabel={t("viewMgmt.tabs.label")}
        actionsTestId="view-management-actions"
        actions={
          activeView === "list"
            ? [
                {
                  id: "create-view",
                  kind: "primary",
                  label: t("viewMgmt.create.title"),
                  icon: Code2,
                  onClick: () => setActiveView("create"),
                },
                {
                  id: "extract-view-conditions",
                  kind: "secondary",
                  label: t("viewMgmt.joinWhere.title"),
                  icon: Sparkles,
                  onClick: () => setActiveView("joinWhere"),
                },
                {
                  id: "refresh-view-list",
                  kind: "utility",
                  label: t("common.action.refresh"),
                  icon: RefreshCw,
                  // 初回の読込は一覧の Skeleton がスピナーを出す（このボタンは狭い画面では「その他の操作」の中で
                  // 見えない）。ボタンは押した再読込の間だけ回す（同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
                  // 検索・絞り込み・他の操作の後の取り直しでは回さず、一覧の処理中の表示がスピナーを出す（#819）。
                  loading: manualRefresh.pending,
                  disabled: !viewObjectsQuery.data && viewObjectsQuery.isFetching,
                  onClick: () => void manualRefresh.track(() => refreshObjects(true)),
                },
                {
                  id: "refresh-view-schema",
                  kind: "utility",
                  label: t("common.action.schemaRefresh"),
                  icon: RefreshCw,
                  // 押したときの送信の間だけ回し、job の間は無効にするだけ（スピナーは進行の表示。#821）。
                  loading: headerSchemaRefreshStarting,
                  disabled: schemaRefreshing && !headerSchemaRefreshStarting,
                  onClick: () => void schemaSync.refreshFrom(VIEW_SCHEMA_REFRESH_HEADER),
                },
              ]
            : []
        }
      />
      <PageBody wide className="grid gap-4">
        <RetryPageNotice
          notice={
            message
              ? { tone: "danger", message: `${message} ${t("viewMgmt.error.retryHint")}` }
              : null
          }
          retrying={noticeRetry.pending}
          disabled={manualRefresh.pending}
          onRetry={() => void noticeRetry.track(() => refetchObjects())}
          operationKey="view-list-retry"
          testId="view-management-notice-retry"
        />
        <SchemaSyncNotice
          sync={schemaSync}
          scope="page"
          testId="view-management-schema-sync-notice"
          processingTestId="view-management-schema-sync-processing"
        />
        {activeView === "list" ? (
          <>
            <DbObjectManagementPanelShell
              id="view-management-panel-list"
              role="region"
              idPrefix={VIEW_MANAGEMENT_ID}
              ariaLabel={t("viewMgmt.workspace.label")}
              framed={false}
              processing={
                schemaRefreshing && !schemaSync.ownsProgress ? (
                  <SchemaRefreshProcessing testId="view-management-workspace-processing" />
                ) : viewObjectsQuery.data && !noticeRetry.pending &&
                  viewObjectsQuery.isFetching && !viewObjectsQuery.isFetchingNextPage ? (
                  <ProcessingIndicator
                    active
                    label={t("viewMgmt.workspace.refreshing")}
                    operationKey="view-list-refresh"
                    placement="workspace"
                    className="rounded-md border border-border bg-surface-sunken px-3 py-2"
                    testId="view-management-workspace-processing"
                    activityIcon={manualRefresh.pending ? "none" : "spinner"}
                  />
                ) : undefined
              }
            >
            <DbObjectListDetail
              showComments
              idPrefix={VIEW_MANAGEMENT_ID}
              headingId="view-grid-heading"
              icon={Eye}
              items={filteredViews}
              selectedName={selectedViewName}
              loading={viewObjectsQuery.isPending && !viewObjectsQuery.data}
              error={
                viewObjectsQuery.error && !viewObjectsQuery.data
                  ? objectListErrorMessage(viewObjectsQuery.error, "viewMgmt.error.load")
                  : ""
              }
              search={viewSearch}
              ownerPrefix={viewOwnerPrefix}
              sort={viewSort}
              totalCount={totalViewCount}
              unfilteredTotalCount={unfilteredViewCount}
              hasNextPage={Boolean(viewObjectsQuery.hasNextPage)}
              loadingNextPage={viewObjectsQuery.isFetchingNextPage}
              loadMoreError={
                viewObjectsQuery.isFetchNextPageError && viewObjectsQuery.error
                  ? listLoadMoreErrorMessage(viewObjectsQuery.error, "viewMgmt.error.load")
                  : ""
              }
              labels={{
                title: t("viewMgmt.list.title"),
                hint: t("viewMgmt.grid.hint"),
                loading: t("viewMgmt.list.loading"),
                emptyTitle: t("viewMgmt.list.emptyTitle"),
                emptyHint: t("viewMgmt.list.emptyHint"),
                noResultsTitle: t("viewMgmt.list.noResultsTitle"),
                noResultsHint: t("viewMgmt.list.noResultsHint"),
                objectName: t("viewMgmt.grid.viewName"),
                rows: t("viewMgmt.grid.rows"),
                owner: t("viewMgmt.grid.owner"),
                showObject: (name) => t("viewMgmt.grid.showView", { name }),
              }}
              onSearchChange={setViewSearch}
              onOwnerPrefixChange={setViewOwnerPrefix}
              onSortChange={toggleSort}
              // 選んでいる行をもう一度押すと詳細を取り直す（取消・失敗の後の読み直し）。
              onSelect={(name) => (name === selectedViewName ? void fetchDetail(name) : selection.select(name))}
              onLoadMore={() => void viewObjectsQuery.fetchNextPage()}
              onRetryLoadMore={() => void viewObjectsQuery.fetchNextPage()}
              onRetry={() => void refreshObjects()}
              detailLabel={t("viewMgmt.detail.label")}
              detail={
                <DbObjectDetailPanel
                  idPrefix={VIEW_MANAGEMENT_ID}
                  operationKey={selectedViewName}
                  headingId="view-detail-heading"
                  detail={detail}
                  loading={detailRequest.loading || (viewObjectsQuery.isPending && !viewObjectsQuery.data)}
                  // 一覧の初回の読込では、スピナーは一覧の Skeleton の 1 つだけにする（#416）。
                  loadingActivityIcon={viewObjectsQuery.isPending && !viewObjectsQuery.data ? "none" : undefined}
                  ddlLoading={detailRequest.ddlLoading}
                  error={detailRequest.error}
                  ddlError={detailRequest.ddlError}
                  exporting={loading === "view-export"}
                  tab={detailTab}
                  labels={{
                    actions: t("viewMgmt.grid.actions"),
                    loading: t("viewMgmt.detail.loading"),
                    ddlLoading: t("viewMgmt.detail.ddlLoading"),
                    tabsLabel: t("viewMgmt.detailTabs.label"),
                    columns: t("viewMgmt.detailTabs.columns"),
                    ddl: t("viewMgmt.detailTabs.ddl"),
                    export: t("viewMgmt.export"),
                    exportAria: t("viewMgmt.exportColumns"),
                    drop: t("viewMgmt.grid.drop"),
                  }}
                  onTabChange={handleDetailTabChange}
                  onRetry={() => void fetchDetail(selectedViewName)}
                  onRetryDdl={() => {
                    if (detail) void detailRequest.loadDdl(formatDbObjectName(detail));
                  }}
                  onCancel={detailRequest.cancel}
                  onExport={(name) => void downloadColumnsXlsx(name)}
                  onDrop={openDropDialog}
                />
              }
            />
            </DbObjectManagementPanelShell>
          </>
        ) : (
          <>
            <DbObjectManagementPanelShell
              id={`view-management-panel-${activeView}`}
              role="region"
              idPrefix={VIEW_MANAGEMENT_ID}
              ariaLabel={t("viewMgmt.toolbar.taskPanel")}
            >
              {taskContent}
            </DbObjectManagementPanelShell>
          </>
        )}
      </PageBody>

      {dropTargetName && (
        <DropDbObjectDialog
          objectName={dropTargetName}
          confirmation={dropConfirmation}
          loading={loading === "drop"}
          error={dropError}
          labels={{
            title: t("viewMgmt.dropDialog.title"),
            subtitle: t("viewMgmt.dropDialog.subtitle"),
            close: t("viewMgmt.dropDialog.close"),
            target: t("viewMgmt.dropDialog.target"),
            executeTitle: t("viewMgmt.dropDialog.executeTitle"),
            executeHint: t("viewMgmt.dropDialog.executeHint"),
            cancel: t("viewMgmt.dropDialog.cancel"),
            run: t("viewMgmt.drop.run"),
          }}
          onConfirmationChange={(value) => {
            setDropConfirmation(value);
            if (dropError) setDropError("");
          }}
          onExecute={() => void dropView()}
          onClose={() => {
            setDropTargetName("");
            setDropError("");
          }}
        />
      )}
    </>
  );
}
