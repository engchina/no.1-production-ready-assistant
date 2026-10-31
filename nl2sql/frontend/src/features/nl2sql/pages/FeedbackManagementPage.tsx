import {
  type ChangeEvent,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";
import {
  Copy,
  DatabaseZap,
  Link2,
  MessageSquareText,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";

import {
  apiErrorMessage,
  Button,
  Banner,
  EmptyState,
  FormSkeleton,
  Skeleton,
  toast,
  StatusBadge,
  PageHeader,
  PageBody,
  useConfirm,
  SelectField,
  ProcessingIndicator,
  TimedLoadingState,
  ListDetailLayout,
  ListDetailPager,
  adjacentListItems,
  FormActionBar,
  FormGrid,
  DataTable,
  INFORMATION_TABLE_ROW_CLASS,
  lastPageOffset,
  ListToolbar,
  FilterSelect,
  isFilterActive,
  OffsetPagination,
  RowTitleButton,
  FieldError,
  FieldLabel,
  FormStatus,
  type FeedbackTone,
  TextareaField,
  TextField,
} from "@production-ready/ui";

import { LIST_DETAIL_STORAGE_PREFIX } from "@/lib/ui-store";

import { DbManagementSearchField } from "@/components/DbObjectFilterFields";
import { PageNotice } from "@/components/page-notice";
import { useWorkspaceState, useWorkspaceUrlSelection } from "@/components/WorkspaceState";
import { ApiError, apiDelete, apiGet, apiPatch, apiPost, isAbortError } from "@/lib/api";
import { useValuesChanged } from "@/lib/render-sync";
import { formatDateTime } from "@/lib/format";
import { t } from "@/lib/i18n";
import { listCountLabels, listToolbarLabels, paginationLabels } from "@/lib/pagination-labels";
import { useWorkspacePaging } from "@/lib/use-workspace-paging";
import { APP_ROUTES } from "@/lib/routes";
import { appPath } from "@/lib/base-path";
import { useUnsavedChangesGuard } from "@/lib/useUnsavedChangesGuard";
import { useRequestScope } from "@/lib/useRequestScope";
import {
  DbObjectManagementPanelShell,
  DbObjectManagementTabs,
  DbObjectPanelHeader,
  type DbObjectTab,
} from "../components/DbObjectManagementShared";
import { HistoryRowListSkeleton } from "../components/HistoryRowList";
import { QuestionText } from "../components/QuestionText";
import { engineLabel } from "../labels";
import { profileDisplayLabel, profileRecordDisplayLabel } from "../profileDisplay";
import { feedbackSearchConfigOrDefault } from "../feedbackSearchConfig";
import {
  adminFeedbackReviewBadgeLabel,
  feedbackRatingLabel,
  userFeedbackRatingBadgeLabel,
} from "../feedbackLabels";
import type {
  AdminFeedbackReviewData,
  FeedbackClearData,
  FeedbackRating,
  FeedbackListData,
  FeedbackRecord,
  FeedbackSearchConfigData,
  ProfileSummary,
  ProfileSummaryPage,
} from "../types";
import { formatElapsedDuration as formatElapsed } from "@/lib/operationTiming";

type FeedbackManagementView = "appFeedback" | "similarityIndex";
type AppFeedbackFilter = "all" | FeedbackRating | "unrated";
type AppFeedbackFilters = {
  rating?: AppFeedbackFilter;
  profileId?: string;
  query?: string;
};
/** 取り直しの契機。reset: 絞り込みの変更（先頭のページへ）、page: ページ送り、current: 同じページを取り直す。 */
type AppFeedbackRefreshDirection = "reset" | "page" | "current";
/** フィードバック履歴の取得の位置（offset 型のページング。#1891）。 */
type ListPosition = { offset: number; limit: number };
/**
 * 操作の結果（失敗・未実行・警告）を出す位置。起点の操作の直下に出し、ページ先頭へ送らない
 * （messaging.md §10.1、#724）。ページ先頭の PageNotice は画面全体の読み込みの失敗だけにする。
 * appFeedback / similarityIndex = 各タブの操作の行（FormActionBar の status）、
 * appFeedbackList = 履歴の絞り込みの下。
 */
type ActionResultOrigin =
  | "appFeedbackList"
  | "appFeedback"
  | "similarityIndex";
type ActionResult = { origin: ActionResultOrigin; tone: FeedbackTone; message: string } | null;

const DEFAULT_FEEDBACK_MANAGEMENT_VIEW: FeedbackManagementView = "appFeedback";
/** フィードバック履歴の一覧 + 詳細（ListDetailLayout）の識別子（幅の保存 key と data-testid。#2283）。 */
const APP_FEEDBACK_LIST_DETAIL_ID = "feedback-management";

const FEEDBACK_MANAGEMENT_TABS: Array<DbObjectTab<FeedbackManagementView>> = [
  { id: "appFeedback", label: t("feedbackManagement.tabs.appFeedback"), icon: MessageSquareText },
  { id: "similarityIndex", label: t("feedbackManagement.tabs.similarityIndex"), icon: DatabaseZap },
];

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function resolveFeedbackManagementView(tab: string | null): FeedbackManagementView {
  if (tab === "appFeedback" || tab === "similarityIndex") {
    return tab;
  }
  return DEFAULT_FEEDBACK_MANAGEMENT_VIEW;
}

export function FeedbackManagementPage() {
  const confirm = useConfirm();
  const [searchParams] = useSearchParams();
  const requestedView = resolveFeedbackManagementView(searchParams.get("tab"));
  const [activeView, setActiveView] = useState<FeedbackManagementView>(requestedView);
  const [history, setHistory] = useState<FeedbackRecord[]>([]);
  const [appProfiles, setAppProfiles] = useState<ProfileSummary[]>([]);
  // フィードバック履歴で選んだ履歴は URL の `?feedback=` が唯一の情報源（共有 ListDetailLayout。#2283）。
  // 先頭の行を自動で選ばない。keep-alive の間はほかの画面の URL を読まない（useWorkspaceUrlSelection）。
  const selection = useWorkspaceUrlSelection("feedback");
  const selectedFeedbackId = selection.selectedId ?? "";
  const [adminFeedbackRating, setAdminFeedbackRating] = useState<FeedbackRating>("good");
  const [adminFeedbackContent, setAdminFeedbackContent] = useState("");
  // 管理者レビューの欄のエラー（欄の直下に出す。#541）。
  const [reviewErrors, setReviewErrors] = useState<{ adminContent?: string }>({});
  // 絞り込みとページの位置は作業状態に残し、再読込・画面の移動のあとも同じ一覧に戻る（#1764）。
  const [feedbackFilter, setFeedbackFilter] = useWorkspaceState<AppFeedbackFilter>("appFeedbackRating", "all");
  const [feedbackSearch, setFeedbackSearch] = useWorkspaceState("appFeedbackSearch", "");
  const appFeedbackLoadSequence = useRef(0);
  const [appProfileFilter, setAppProfileFilter] = useWorkspaceState("appFeedbackProfile", "");
  const [feedbackTotal, setFeedbackTotal] = useState(0);
  // 絞り込む前の全件（「N 件（全 M 件）」の M。#1891）。
  const [feedbackUnfilteredTotal, setFeedbackUnfilteredTotal] = useState(0);
  // ページ番号と表示件数（offset 型。作業状態に残す。#1764 / #1891）。
  const feedbackPaging = useWorkspacePaging("appFeedbackPaging");
  const [feedbackConfig, setFeedbackConfig] = useState<FeedbackSearchConfigData | null>(null);
  const [savedFeedbackConfig, setSavedFeedbackConfig] = useState<FeedbackSearchConfigData | null>(null);
  const [savedReview, setSavedReview] = useState("");
  const reviewDirtyRef = useRef(false);
  // 初回の読込は mount 時の effect で始まるため、最初から読込中にしておく。
  const [loading, setLoading] = useState("load");
  // どのボタンが始めた読込か。スピナーは押したボタンだけが出す（#819）。初回の読込は §3.7 のとおり
  // ヘッダーの「最新情報を取得」が出す。業務プロファイルの切り替え・保存の後の取り直しは null（領域の表示だけ）。
  const [refreshOrigin, setRefreshOrigin] = useState<"header" | "notice" | null>("header");
  // 一覧を 1 度でも取得できたか。最初の読み込みだけ一覧の領域を Skeleton で覆い、「最新情報を取得」は前の内容を
  // 出したままヘッダーのボタンの loading で示す（実行履歴と同じ。messaging.md §3.6・§3.7、#1777）。
  const [loadedOnce, setLoadedOnce] = useState(false);
  const [message, setMessage] = useState("");
  const [actionResult, setActionResult] = useState<ActionResult>(null);
  const showActionError = (origin: ActionResultOrigin, err: unknown, fallback: string) =>
    setActionResult({ origin, tone: "danger", message: apiErrorMessage(err, fallback) });
  const actionResultFor = (origin: ActionResultOrigin) =>
    actionResult?.origin === origin ? actionResult : null;
  const loadSequence = useRef(0);
  // 初回の読み込みを始めたか（render では ref の連番ではなく state を見る）。
  // 編集欄へ反映済みのフィードバック ID。render 中に比べるため state で持つ。
  const [syncedAppFeedbackId, setSyncedAppFeedbackId] = useState<string | null>(null);
  const adminFeedbackContentRef = useRef<HTMLTextAreaElement | null>(null);
  const { abortAll, run: runScopedRequest } = useRequestScope();

  const appFeedbackItems = history;
  const feedbackConfigDirty = Boolean(
    feedbackConfig &&
      savedFeedbackConfig &&
      (feedbackConfig.similarity_threshold !== savedFeedbackConfig.similarity_threshold ||
        feedbackConfig.match_limit !== savedFeedbackConfig.match_limit)
  );
  // 選んだ履歴が今のページに無い（絞り込み・ページ送りで外れた）ときは、別の行へ置き換えずに詳細で知らせる。
  const selectedAppFeedback = useMemo(
    () => appFeedbackItems.find((item) => item.id === selectedFeedbackId) ?? null,
    [appFeedbackItems, selectedFeedbackId]
  );
  // 前へ / 次へ（詳細の上端の行）の移り先。一覧の今のページの並びの前後。
  const { previous: previousAppFeedback, next: nextAppFeedback } = adjacentListItems(
    appFeedbackItems,
    selectedFeedbackId || null,
    (item) => item.id
  );
  const reviewSignature = JSON.stringify([selectedAppFeedback?.id, adminFeedbackRating,
    adminFeedbackContent]);
  const reviewDirty = Boolean(selectedAppFeedback && savedReview && reviewSignature !== savedReview);
  // 最新の未保存状態を commit 時に入れる（render 中に ref を書かない）。
  useLayoutEffect(() => { reviewDirtyRef.current = reviewDirty; });
  const confirmDiscard = () => confirm({
    title: t("feedbackManagement.discard.title"),
    description: t("feedbackManagement.discard.description"),
    confirmLabel: t("feedbackManagement.discard.confirm"),
    tone: "danger",
    dismissOnOverlay: false,
  });
  useUnsavedChangesGuard(reviewDirty || feedbackConfigDirty, confirmDiscard);
  const selectReview = async (id: string) => {
    if (loading || id === selectedFeedbackId) return;
    if (reviewDirty && !(await confirmDiscard())) return;
    reviewDirtyRef.current = false;
    selection.select(id);
  };
  // 1 ペイン（xl 未満）の「一覧へ戻る」。未保存のレビューは先に破棄を確認し、確認した編集は保存済みの扱いにしてから
  // 戻る（戻る / 進むの確認〔UnsavedChangesBlocker〕を重ねて出さない）。
  const backToFeedbackList = async () => {
    if (reviewDirty) {
      if (!(await confirmDiscard())) return;
      reviewDirtyRef.current = false;
      flushSync(() => setSavedReview(reviewSignature));
    }
    selection.clear();
  };

  const adminFeedbackContentRequired = adminFeedbackRating === "bad";
  const appFeedbackFiltersActive =
    Boolean(feedbackSearch.trim()) || feedbackFilter !== "all" || Boolean(appProfileFilter);
  // 最初の読み込み（まだ一覧を取得できていない間）。各タブの一覧の領域だけを Skeleton で覆う（#1777）。
  const initialLoading = loading === "load" && !loadedOnce;
  // 「最新情報を取得」の取り直し。前の内容を出したまま、作業領域の先頭に処理中を出す。
  const refreshing = loading === "load" && loadedOnce;

  const fetchAppFeedback = (
    position: ListPosition,
    signal?: AbortSignal,
    filters: AppFeedbackFilters = {}
  ) => {
    const rating = filters.rating ?? feedbackFilter;
    const profileId = filters.profileId ?? appProfileFilter;
    const query = filters.query ?? feedbackSearch;
    const params = new URLSearchParams({
      limit: String(position.limit),
      offset: String(position.offset),
      rating,
    });
    if (profileId) params.set("profile_id", profileId);
    if (query.trim()) params.set("q", query.trim());
    return apiGet<FeedbackListData>(`/api/nl2sql/feedback?${params.toString()}`, {
      signal,
    });
  };

  // 取得したページの位置が範囲の外（削除・絞り込みで減った）なら、最後のページを取り直す（空のページを出さない）。
  const fetchAppFeedbackInRange = async (
    position: ListPosition,
    signal?: AbortSignal,
    filters: AppFeedbackFilters = {}
  ): Promise<{ data: FeedbackListData; position: ListPosition }> => {
    const data = await fetchAppFeedback(position, signal, filters);
    if (data.items.length > 0 || position.offset === 0) return { data, position };
    const last = { offset: lastPageOffset(data.total, position.limit), limit: position.limit };
    return { data: await fetchAppFeedback(last, signal, filters), position: last };
  };

  // 表示したページの位置を作業状態に残す（取得に成功したときだけ。失敗したら表示していたページに留まる）。
  const commitAppFeedbackPosition = (position: ListPosition) => {
    if (position.limit !== feedbackPaging.pageSize) feedbackPaging.setPageSize(position.limit);
    feedbackPaging.setPage(Math.floor(position.offset / Math.max(1, position.limit)) + 1);
  };

  /**
   * フィードバック履歴を取り直す。reset: 絞り込みの変更（先頭のページ）、page: ページ送り・表示件数の変更、
   * current: 同じページを取り直す。成功したらページの位置を残して `onLoaded` を呼び、true を返す。
   */
  const refreshAppFeedback = async (
    position: ListPosition = { offset: 0, limit: feedbackPaging.pageSize },
    direction: AppFeedbackRefreshDirection = "reset",
    filters: AppFeedbackFilters = {},
    onLoaded?: () => void
  ): Promise<boolean> => {
    if (direction === "reset" && reviewDirty && !(await confirmDiscard())) return true;
    // 条件を続けて変えたとき、遅れて返った古い条件の応答で新しい条件の一覧を上書きしない（#535）。
    const sequence = ++appFeedbackLoadSequence.current;
    setLoading("app-feedback-load");
    setActionResult(null);
    try {
      const { data, position: loaded } = await fetchAppFeedbackInRange(position, undefined, filters);
      if (sequence !== appFeedbackLoadSequence.current) return true;
      reviewDirtyRef.current = false;
      if (filters.rating !== undefined) setFeedbackFilter(filters.rating);
      if (filters.profileId !== undefined) setAppProfileFilter(filters.profileId);
      setHistory(data.items);
      setFeedbackTotal(data.total);
      setFeedbackUnfilteredTotal(data.unfiltered_total ?? data.total);
      commitAppFeedbackPosition(loaded);
      onLoaded?.();
      return true;
    } catch (err) {
      if (sequence !== appFeedbackLoadSequence.current) return true;
      showActionError("appFeedbackList", err, t("feedbackManagement.error.load"));
      return false;
    } finally {
      if (sequence === appFeedbackLoadSequence.current) setLoading("");
    }
  };

  const currentAppFeedbackPosition = (): ListPosition => ({
    offset: feedbackPaging.offset,
    limit: feedbackPaging.pageSize,
  });

  // 最初の読み込み・「最新情報を取得」は今のページを取る。復元したページが無くなった（履歴が減った）ときは最後の
  // ページ、位置を読めない（422）ときは先頭のページを取り直す。
  const fetchCurrentAppFeedback = async (signal: AbortSignal) => {
    const position = currentAppFeedbackPosition();
    try {
      const loaded = await fetchAppFeedbackInRange(position, signal);
      commitAppFeedbackPosition(loaded.position);
      return loaded.data;
    } catch (err) {
      if (!(position.offset > 0 && err instanceof ApiError && err.status === 422)) throw err;
    }
    feedbackPaging.reset();
    return fetchAppFeedback({ offset: 0, limit: position.limit }, signal);
  };

  // 絞り込みを変えたら先頭のページから取り直す（成功したらページの位置も先頭に残す）。
  const applyAppFeedbackFilters = (filters: AppFeedbackFilters) =>
    void refreshAppFeedback({ offset: 0, limit: feedbackPaging.pageSize }, "reset", filters);

  const load = async (announce = false, origin: "header" | "notice" = "header") => {
    if (loading) return;
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    setLoading("load");
    setRefreshOrigin(origin);
    // 失敗の案内の「再試行」では、取り直せるまで案内を残し、その中に進行を出す（押すと案内が消えて、進行が
    // 離れたパネルの先頭に出るのを防ぐ。messaging §10.2、#2034）。
    if (origin === "header") setMessage("");
    setActionResult(null);
    await requestData(sequence, announce);
  };

  // 読込中の表示とメッセージの初期化は呼び出し側で行う（初回表示は初期 state が読込中）。
  const requestData = async (sequence: number, announce: boolean) => {
    try {
      await runScopedRequest(async (signal) => {
        const [
          appProfileData,
          appFeedbackData,
          configData,
        ] = await Promise.all([
          apiGet<ProfileSummaryPage>("/api/nl2sql/profiles/search?limit=100", {
            signal,
          }),
          fetchCurrentAppFeedback(signal),
          apiGet<FeedbackSearchConfigData>("/api/nl2sql/feedback-config", { signal }),
        ]);
        if (signal.aborted || sequence !== loadSequence.current) return;
        setAppProfiles(appProfileData.items);
        if (reviewDirtyRef.current && selectedAppFeedback &&
            !appFeedbackData.items.some((item) => item.id === selectedAppFeedback.id)) {
          setMessage(t("feedbackManagement.discard.missing"));
          return;
        }
        setHistory(appFeedbackData.items);
        setFeedbackTotal(appFeedbackData.total);
        setFeedbackUnfilteredTotal(appFeedbackData.unfiltered_total ?? appFeedbackData.total);
        if (!feedbackConfigDirty) {
          setFeedbackConfig(configData);
          setSavedFeedbackConfig(configData);
        }
        setLoadedOnce(true);
        setMessage("");
        if (announce) toast.success(t("common.action.refreshed"));
      });
    } catch (err) {
      if (isAbortError(err)) {
        return;
      }
      setMessage(apiErrorMessage(err, t("feedbackManagement.error.load")));
    } finally {
      if (sequence === loadSequence.current) setLoading("");
    }
  };

  // ページ送り・表示件数の変更。編集中のレビューがあれば先に破棄の確認をしてから取り直し、取得できたら位置を残す
  // （失敗したら表示していたページに留まり、同じ操作でやり直せる）。
  const goToAppFeedbackPage = async (offset: number) => {
    if (reviewDirty && !(await confirmDiscard())) return;
    await refreshAppFeedback({ offset, limit: feedbackPaging.pageSize }, "page");
  };

  const changeAppFeedbackPageSize = async (limit: number) => {
    if (reviewDirty && !(await confirmDiscard())) return;
    await refreshAppFeedback({ offset: 0, limit }, "page");
  };

  const saveAppFeedback = async () => {
    if (loading) return;
    if (!selectedAppFeedback) return;
    const trimmedAdminFeedbackContent = adminFeedbackContent.trim();
    // 未入力はページ先頭ではなく欄の直下に出し、画面の並び順で最初のエラーの欄へフォーカスする（#541）。
    const nextErrors = {
      adminContent:
        adminFeedbackRating === "bad" && !trimmedAdminFeedbackContent
          ? t("feedbackManagement.appFeedback.adminFeedbackRequired")
          : undefined,
    };
    setReviewErrors(nextErrors);
    if (nextErrors.adminContent) {
      setActionResult(null);
      const target = adminFeedbackContentRef.current;
      window.requestAnimationFrame(() => target?.focus());
      return;
    }
    setLoading("app-feedback");
    setActionResult(null);
    try {
      const data = await apiPost<AdminFeedbackReviewData>("/api/nl2sql/feedback/admin-review", {
        history_id: selectedAppFeedback.id,
        rating: adminFeedbackRating,
        feedback_content: trimmedAdminFeedbackContent,
      });
      reviewDirtyRef.current = false;
      setSavedReview(reviewSignature);
      await refreshAppFeedback(currentAppFeedbackPosition(), "current");
      const publishWarnings = data.similar_history_publish?.warnings ?? [];
      if (publishWarnings.length > 0) {
        // 保存は済んでいるので、類似履歴への公開の警告は warning で操作の行に出す。
        setActionResult({ origin: "appFeedback", tone: "warning", message: publishWarnings.join(" ") });
      }
      const publishStatus = data.similar_history_publish?.status ?? "published";
      const publishedToSimilarHistory =
        data.rating === "good" && publishStatus !== "skipped" && publishStatus !== "unpublished";
      toast.success(
        publishedToSimilarHistory
          ? t("feedbackManagement.appFeedback.adminSavedAndPublished")
          : t("feedbackManagement.appFeedback.adminSaved")
      );
    } catch (err) {
      showActionError("appFeedback", err, t("feedbackManagement.error.appFeedback"));
    } finally {
      setLoading("");
    }
  };

  const clearAppFeedback = async () => {
    if (loading) return;
    if (!selectedAppFeedback) return;
    const ok = await confirm({
      title: t("feedbackManagement.appFeedback.clearTitle"),
      description: t("feedbackManagement.appFeedback.clearDescription"),
      confirmLabel: t("feedbackManagement.appFeedback.clear"),
      tone: "danger",
    });
    if (!ok) return;
    setLoading("app-feedback-clear");
    setActionResult(null);
    try {
      await apiDelete<FeedbackClearData>(`/api/nl2sql/feedback/${selectedAppFeedback.id}`);
      setSyncedAppFeedbackId(null);
      reviewDirtyRef.current = false;
      setSavedReview(reviewSignature);
      await refreshAppFeedback(currentAppFeedbackPosition(), "current");
      toast.success(t("feedbackManagement.appFeedback.cleared"));
    } catch (err) {
      showActionError("appFeedback", err, t("feedbackManagement.error.appFeedback"));
    } finally {
      setLoading("");
    }
  };

  const saveFeedbackConfig = async () => {
    if (loading) return;
    if (!feedbackConfig) return;
    setLoading("feedback-config");
    setActionResult(null);
    try {
      const nextConfig = await apiPatch<FeedbackSearchConfigData>("/api/nl2sql/feedback-config", feedbackConfig);
      setFeedbackConfig(nextConfig);
      setSavedFeedbackConfig(nextConfig);
      toast.success(t("feedbackManagement.similarityIndex.configSaved"));
    } catch (err) {
      showActionError("similarityIndex", err, t("feedbackManagement.error.feedbackConfig"));
    } finally {
      setLoading("");
    }
  };

  // 初回ロードは mount 時だけ行う。最新の requestData を commit 時に ref へ入れて呼ぶ
  // （requestData は Profile・カーソル・編集中の状態を読むため、deps に入れると毎レンダーで再取得になる）。
  const requestDataRef = useRef(requestData);
  useLayoutEffect(() => {
    requestDataRef.current = requestData;
  });
  useEffect(() => {
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    void requestDataRef.current(sequence, false);
    return () => {
      loadSequence.current += 1;
      abortAll();
    };
  }, [abortAll]);

  // URL の tab が変わったら表示を合わせる（render 中に同期する）。
  if (useValuesChanged([requestedView])) setActiveView(requestedView);

  // 選択中のフィードバックが変わったら、編集欄を render 中に合わせる。
  // 同じフィードバックの更新では、未保存の編集（reviewDirty）を上書きしない。
  if (useValuesChanged([selectedAppFeedback])) {
    if (!selectedAppFeedback) {
      setSyncedAppFeedbackId(null);
      setSavedReview("");
      setAdminFeedbackRating("good");
      setAdminFeedbackContent("");
      setReviewErrors({});
    } else {
      const switchedFeedback = syncedAppFeedbackId !== selectedAppFeedback.id;
      if (switchedFeedback || !reviewDirty) {
        if (switchedFeedback) setReviewErrors({});
        setSyncedAppFeedbackId(selectedAppFeedback.id);
        setSavedReview(JSON.stringify([selectedAppFeedback.id,
          selectedAppFeedback.admin_feedback_rating ?? "good",
          selectedAppFeedback.admin_feedback_content ?? ""]));
        setAdminFeedbackRating(selectedAppFeedback.admin_feedback_rating ?? "good");
        setAdminFeedbackContent(selectedAppFeedback.admin_feedback_content ?? "");
      }
    }
  }


  // 「最新情報を取得」の間は前の内容を出したまま、作業領域の先頭に経過時間を 1 か所だけ出す。スピナーは押した
  // ボタン（ヘッダーの「最新情報を取得」・エラーの通知の「再試行」）が出す（messaging.md §3.7、#416・#1777）。
  const noticeRefreshing = refreshing && refreshOrigin === "notice" && Boolean(message);
  const refreshProcessing = refreshing && !noticeRefreshing ? (
    <ProcessingIndicator
      active
      label={t("common.processing.refreshing")}
      operationKey="feedback-management-refresh"
      placement="workspace"
      className="rounded-md border border-border bg-surface-sunken px-3 py-2"
      testId="feedback-management-refresh-processing"
      activityIcon="none"
    />
  ) : undefined;

  return (
    <>
      <PageHeader wide
        title={t("nav.feedbackManagement")}
        subtitle={t("feedbackManagement.subtitle")}
        // 1 ペイン（xl 未満）でフィードバック履歴の詳細を開いているときだけ、左上に「一覧へ戻る」（#2283）。
        back={
          activeView === "appFeedback" && !selection.split && selectedFeedbackId
            ? {
                label: t("common.backToList"),
                onClick: () => void backToFeedbackList(),
                testId: "app-feedback-detail-back",
              }
            : undefined
        }
        actionsTestId="feedback-management-actions"
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            onClick: () => load(true, "header"),
            loading: loading === "load" && refreshOrigin === "header",
            disabled: Boolean(loading),
          },
        ]}
      />

      <PageBody wide>
        {/* 絞り込み・ページ送りの読み込み（app-feedback-load）では外側を無効にしない。無効にすると入力中の
            履歴検索の欄からフォーカスが外れ、続けて打った文字が入らない（#968）。その間は一覧の行・ページ送り・
            編集欄だけを内側の fieldset で無効にする。 */}
        <fieldset
          disabled={Boolean(loading) && !(loading === "app-feedback-load" && activeView === "appFeedback")}
          className="m-0 grid min-w-0 gap-4 border-0 p-0"
        >
        <PageNotice
          notice={message ? { tone: "danger", message } : null}
          action={
            message ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                loading={loading === "load" && refreshOrigin === "notice"}
                disabled={Boolean(loading) && !(loading === "load" && refreshOrigin === "notice")}
                onClick={() => void load(false, "notice")} icon={RefreshCw}>
                <span>{t("common.retry")}</span>
              </Button>
            ) : undefined
          }
          progress={
            noticeRefreshing ? (
              <ProcessingIndicator
                active
                label={t("common.processing.refreshing")}
                operationKey="feedback-management-retry"
                placement="action"
                testId="feedback-management-retry-processing"
                activityIcon="none"
              />
            ) : null
          }
        />

        <DbObjectManagementTabs
          idPrefix="feedback-management"
          tabs={FEEDBACK_MANAGEMENT_TABS}
          activeView={activeView}
          ariaLabel={t("feedbackManagement.tabs.label")}
          onViewChange={setActiveView}
        />

        {activeView === "appFeedback" && (
          <DbObjectManagementPanelShell
            id="feedback-management-panel-appFeedback"
            labelledBy="feedback-management-tab-appFeedback"
            ariaLabel={t("feedbackManagement.workspace.appFeedback")}
            idPrefix="feedback-management-app-feedback"
            processing={refreshProcessing}
            // 一覧と詳細の列がそれぞれ枠を持つので、外側の枠を付けない（#2281・#2283）。
            framed={false}
          >
            {/* フィードバック履歴の一覧 + 管理者レビュー（B 型。共有 ListDetailLayout。#2283）。xl 以上は
                「狭い一覧 | 広い詳細」、xl 未満は 1 ペイン（一覧 → 詳細 → 「一覧へ戻る」）。 */}
            <ListDetailLayout
              id={APP_FEEDBACK_LIST_DETAIL_ID}
              listLabel={t("feedbackManagement.appFeedback.historyList")}
              detailLabel={t("feedbackManagement.appFeedback.detailLabel")}
              storagePrefix={LIST_DETAIL_STORAGE_PREFIX}
              listHeader={
                <div className="grid gap-3" data-testid="feedback-history-pane">
                  <h2 className="flex items-center gap-2 text-sm font-semibold text-fg">
                    <MessageSquareText size={16} aria-hidden="true" />
                    {t("feedbackManagement.appFeedback.historyList")}
                  </h2>
                  {/* 一覧の絞り込みは条件を変えたらすぐ適用する（検索語は SearchField の debounce・IME 対応。
                      「絞り込み」ボタンを置かない。#535）。検索・絞り込み・件数は共通の ListToolbar（#1891）。
                      一覧の列は 28rem 未満なので縦に積み、絞り込みは「絞り込み（N）」のシート（#1933）。 */}
                  <ListToolbar
                    testId="feedback-app-filters"
                    count={
                      initialLoading
                        ? undefined
                        : {
                            total: feedbackUnfilteredTotal,
                            matched: appFeedbackFiltersActive ? feedbackTotal : undefined,
                            labels: listCountLabels(),
                            testId: "app-feedback-list-count",
                          }
                    }
                    search={
                      <DbManagementSearchField
                        label={t("feedbackManagement.appFeedback.search")}
                        placeholder={t("feedbackManagement.appFeedback.searchPlaceholder")}
                        value={feedbackSearch}
                        onChange={(value) => {
                          setFeedbackSearch(value);
                          applyAppFeedbackFilters({ query: value });
                        }}
                      />
                    }
                    filters={
                      <>
                        <FilterSelect<AppFeedbackFilter>
                          id="app-feedback-rating-filter"
                          label={t("feedbackManagement.appFeedback.filter")}
                          value={feedbackFilter}
                          options={[
                            { value: "all", label: t("feedbackManagement.appFeedback.filterAll") },
                            { value: "good", label: t("nl2sql.feedback.good") },
                            { value: "bad", label: t("nl2sql.feedback.bad") },
                            { value: "unrated", label: t("feedbackManagement.appFeedback.unrated") },
                          ]}
                          onValueChange={(rating) => applyAppFeedbackFilters({ rating })}
                        />
                        <FilterSelect
                          id="app-feedback-profile-filter"
                          label={t("feedbackManagement.appFeedback.profileFilter")}
                          value={appProfileFilter}
                          options={[
                            { value: "", label: t("feedbackManagement.appFeedback.profileAll") },
                            ...appProfiles
                              .filter((profile) => !profile.archived)
                              .map((profile) => ({ value: profile.id, label: profileDisplayLabel(profile) })),
                          ]}
                          onValueChange={(profileId) => applyAppFeedbackFilters({ profileId })}
                          // 業務プロファイル（対象の選択）は lg（#1891 の幅の段）。
                          width="lg"
                        />
                      </>
                    }
                    activeFilterCount={
                      (isFilterActive(feedbackFilter, "all") ? 1 : 0) + (isFilterActive(appProfileFilter, "") ? 1 : 0)
                    }
                    searchActive={Boolean(feedbackSearch.trim())}
                    onClearFilters={() => {
                      setFeedbackSearch("");
                      applyAppFeedbackFilters({ query: "", rating: "all", profileId: "" });
                    }}
                    filtersLayout="sheet"
                    labels={listToolbarLabels()}
                  />
                  {loading === "app-feedback-load" ? (
                    // 絞り込み・ページ送りの読込中は、前の一覧を出したまま経過時間を示す（操作したボタンがないため。#535）。
                    <ProcessingIndicator
                      active
                      label={t("feedbackManagement.appFeedback.loading")}
                      operationKey="app-feedback-load"
                      placement="panel"
                      testId="app-feedback-load-processing"
                    />
                  ) : null}
                  <ActionResultBanner result={actionResultFor("appFeedbackList")} />
                </div>
              }
              list={
                <fieldset
                  disabled={loading === "app-feedback-load"}
                  aria-busy={loading === "app-feedback-load"}
                  className="m-0 flex min-h-0 min-w-0 flex-col border-0 p-0"
                >
                  {initialLoading ? (
                    // 最初の読み込みは一覧の領域だけを一覧の形の Skeleton で覆い、経過時間をここに 1 か所だけ出す。
                    // スピナーはヘッダーの「最新情報を取得」が出す（操作が 1 つのヘッダー。messaging.md §3.7）。
                    <div className="p-3">
                      <HistoryRowListSkeleton
                        label={t("feedbackManagement.appFeedback.loading")}
                        operationKey="feedback-management-initial-load"
                        testId="app-feedback-list-loading"
                        skeletonTestId="app-feedback-list-skeleton"
                        activityIcon="none"
                      />
                    </div>
                  ) : appFeedbackItems.length > 0 ? (
                    <FeedbackHistoryList
                      items={appFeedbackItems}
                      selectedId={selectedFeedbackId}
                      onSelect={(id) => void selectReview(id)}
                    />
                  ) : (
                    <div className="p-3">
                      {appFeedbackFiltersActive ? (
                        <EmptyState
                          title={t("feedbackManagement.appFeedback.noMatchesTitle")}
                          hint={t("feedbackManagement.appFeedback.noMatchesHint")}
                        />
                      ) : (
                        <EmptyState
                          title={t("feedbackManagement.appFeedback.emptyTitle")}
                          hint={t("feedbackManagement.appFeedback.emptyHint")}
                        />
                      )}
                    </div>
                  )}
                </fieldset>
              }
              listFooter={
                initialLoading ? undefined : (
                  <OffsetPagination
                    offset={feedbackPaging.offset}
                    limit={feedbackPaging.pageSize}
                    total={feedbackTotal}
                    count={history.length}
                    onPageChange={(_page, offset) => void goToAppFeedbackPage(offset)}
                    onPageSizeChange={(limit) => void changeAppFeedbackPageSize(limit)}
                    disabled={loading === "app-feedback-load"}
                    labels={paginationLabels()}
                    ariaLabel={t("feedbackManagement.appFeedback.pagination.label")}
                    testId="app-feedback-pagination"
                  />
                )
              }
              hasSelection={Boolean(selectedFeedbackId)}
              detail={
                <section className="grid min-w-0 content-start gap-4" data-testid="app-feedback-editor-pane">
                  <DbObjectPanelHeader
                    title={t("feedbackManagement.appFeedback.title")}
                    description={t("feedbackManagement.appFeedback.hint")}
                    icon={MessageSquareText}
                  />
                  {/* 一覧の読み込み中は編集欄も無効にする（読み込みの後に対象の履歴が変わることがあるため）。 */}
                  <fieldset
                    disabled={loading === "app-feedback-load"}
                    className="m-0 grid min-w-0 content-start gap-4 border-0 p-0"
                  >
                  {initialLoading ? (
                    <AppFeedbackEditorSkeleton />
                  ) : selectedAppFeedback ? (
                    <>
                  {/* 欄の並びは共通の FormGrid（1 列。最大 60rem。#1891）。確定の行（FormActionBar）はその外。 */}
                  <FormGrid columns={1}>
                  <section className="rounded-md border border-border bg-surface p-3">
                    <p className="text-xs font-medium text-fg-muted">{t("feedbackManagement.appFeedback.history")}</p>
                    <QuestionText
                      value={selectedAppFeedback.question}
                      variant="detail"
                      maxLines={3}
                      expandable
                      className="mt-1"
                      testId="app-feedback-selected-question"
                    />
                  </section>
                  <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                    <div className="rounded-md border border-border bg-surface p-3">
                      <p className="text-xs font-medium text-fg-muted">{t("feedbackManagement.appFeedback.profile")}</p>
                      <p className="mt-1 break-words text-sm font-semibold text-fg">
                        {profileRecordDisplayLabel(selectedAppFeedback)}
                      </p>
                    </div>
                    <div className="rounded-md border border-border bg-surface p-3">
                      <p className="text-xs font-medium text-fg-muted">{t("feedbackManagement.appFeedback.createdAt")}</p>
                      <p className="mt-1 text-sm font-semibold text-fg">{formatDateTime(selectedAppFeedback.feedback_updated_at || selectedAppFeedback.created_at)}</p>
                      {/* 実行エンジンと処理時間（一覧の行には出さない。#2283）。 */}
                      <p className="mt-1 text-xs text-fg-muted" data-testid="app-feedback-selected-engine">
                        {engineLabel(selectedAppFeedback.engine)} · {formatElapsed(selectedAppFeedback.elapsed_ms)}
                      </p>
                      {selectedAppFeedback.training_status && (
                        <div className="mt-2"><StatusBadge variant="info" label={t(`qcm.candidates.status.${selectedAppFeedback.training_status}`)} /></div>
                      )}
                    </div>
                  </div>
                  <TextareaField
                    id="app-feedback-generated-sql"
                    label={t("feedbackManagement.appFeedback.generatedSql")}
                    surface="code"
                    width="full"
                    value={selectedAppFeedback.executable_sql || selectedAppFeedback.generated_sql}
                    readOnly
                    rows={5}
                    textareaClassName="min-w-0 max-w-full"
                  />
                  <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                    <div className="rounded-md border border-border bg-surface p-3">
                      <p className="text-xs font-medium text-fg-muted">{t("feedbackManagement.appFeedback.userRating")}</p>
                      <div className="mt-2">
                        <StatusBadge
                          variant={selectedAppFeedback.feedback_rating ? "success" : "neutral"}
                          label={feedbackRatingLabel(selectedAppFeedback.feedback_rating)}
                        />
                      </div>
                    </div>
                    <div className="rounded-md border border-border bg-surface p-3">
                      <p className="text-xs font-medium text-fg-muted">{t("feedbackManagement.appFeedback.adminRatingStatus")}</p>
                      <div className="mt-2">
                        <StatusBadge
                          variant={selectedAppFeedback.admin_feedback_rating === "good" ? "success" : "neutral"}
                          label={feedbackRatingLabel(selectedAppFeedback.admin_feedback_rating)}
                        />
                      </div>
                    </div>
                  </div>
                  <TextareaField
                    id="app-feedback-user-content"
                    label={t("feedbackManagement.appFeedback.userFeedbackContent")}
                    width="full"
                    value={selectedAppFeedback.feedback_comment}
                    readOnly
                    rows={3}
                    textareaClassName="min-w-0 max-w-full"
                    placeholder={t("feedbackManagement.appFeedback.userFeedbackEmpty")}
                  />
                  <SelectField<FeedbackRating>
                    id="app-feedback-admin-rating"
                    label={t("feedbackManagement.appFeedback.adminRating")}
                    value={adminFeedbackRating}
                    options={[
                      { value: "good", label: t("nl2sql.feedback.good") },
                      { value: "bad", label: t("nl2sql.feedback.bad") },
                    ]}
                    onValueChange={(rating) => {
                      setAdminFeedbackRating(rating);
                      setReviewErrors((current) => ({ ...current, adminContent: undefined }));
                    }}
                    // 「合っている / 違う」の短い選択（#1891 の幅の段）。
                    width="sm"
                  />
                  <div className="grid min-w-0 gap-1 text-sm font-medium text-fg">
                    <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                      {/* 管理者評価が「違う」のときだけ必須（saveAppFeedback のガード）。 */}
                      <FieldLabel
                        htmlFor="app-feedback-admin-content"
                        label={t("feedbackManagement.appFeedback.adminFeedbackContent")}
                        required={adminFeedbackContentRequired}
                      />
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        disabled={!selectedAppFeedback.feedback_comment.trim()}
                        onClick={() => setAdminFeedbackContent(selectedAppFeedback.feedback_comment)} icon={Copy}>
                        <span>{t("feedbackManagement.appFeedback.copyUserContent")}</span>
                      </Button>
                    </div>
                    {/* oxlint-disable-next-line design-system/restricted-syntax -- ラベルの行に「利用者コメントを反映」ボタンを並べるため、ラベルと入力を分けて置く（TextareaField のラベルには要素を並べられない） */}
                    <textarea
                      ref={adminFeedbackContentRef}
                      id="app-feedback-admin-content"
                      aria-label={t("feedbackManagement.appFeedback.adminFeedbackContent")}
                      aria-required={adminFeedbackContentRequired}
                      aria-invalid={reviewErrors.adminContent ? "true" : undefined}
                      aria-describedby={reviewErrors.adminContent ? "app-feedback-admin-content-error" : undefined}
                      value={adminFeedbackContent}
                      onChange={(event) => {
                        setAdminFeedbackContent(event.currentTarget.value);
                        setReviewErrors((current) => ({ ...current, adminContent: undefined }));
                      }}
                      required={adminFeedbackContentRequired}
                      rows={4}
                      className={`min-h-28 w-full min-w-0 max-w-full rounded-md border bg-surface px-3 py-2 text-sm leading-6 focus:border-focus-ring ${
                        reviewErrors.adminContent ? "border-danger-fg" : "border-border-control"
                      }`}
                      placeholder={t("feedbackManagement.appFeedback.adminFeedbackPlaceholder")}
                    />
                    <FieldError id="app-feedback-admin-content-error" message={reviewErrors.adminContent} />
                  </div>
                  </FormGrid>
                  <FormActionBar
                    ariaLabel={t("feedbackManagement.appFeedback.actions")}
                    testId="feedback-app-actions"
                    status={<ActionResultStatus result={actionResultFor("appFeedback")} />}
                    primaryActions={[
                      {
                        id: "save",
                        label: t("feedbackManagement.appFeedback.save"),
                        icon: Save,
                        loading: loading === "app-feedback",
                        disabled: loading === "app-feedback",
                        onClick: () => void saveAppFeedback(),
                      },
                    ]}
                    secondaryActions={
                      selectedAppFeedback.admin_feedback_rating === "good"
                        ? [
                            {
                              id: "open-candidate",
                              label: t("feedbackManagement.appFeedback.openCandidate"),
                              icon: Link2,
                              // FormActionBar の href は `<a>` のまま描く（router を通らない）ため、配信の前置きを付ける（#1316）。
                              href: appPath(`${APP_ROUTES.questionClassifierModels}?tab=candidates&history_id=${encodeURIComponent(selectedAppFeedback.id)}`),
                            },
                          ]
                        : []
                    }
                    dangerActions={[
                      {
                        id: "clear",
                        label: t("feedbackManagement.appFeedback.clear"),
                        icon: Trash2,
                        loading: loading === "app-feedback-clear",
                        onClick: () => void clearAppFeedback(),
                      },
                    ]}
                  />
                    </>
                  ) : (
                    <EmptyState
                      title={t("feedbackManagement.appFeedback.notInListTitle")}
                      hint={t("feedbackManagement.appFeedback.notInListHint")}
                    />
                  )}
                  </fieldset>
                </section>
              }
              emptyDetail={
                <EmptyState
                  title={t("feedbackManagement.appFeedback.detailEmptyTitle")}
                  hint={t("feedbackManagement.appFeedback.detailEmptyHint")}
                />
              }
              detailToolbar={
                selectedFeedbackId ? (
                  <ListDetailPager
                    onPrevious={previousAppFeedback ? () => void selectReview(previousAppFeedback.id) : null}
                    onNext={nextAppFeedback ? () => void selectReview(nextAppFeedback.id) : null}
                    disabled={Boolean(loading)}
                    labels={{ previous: t("listDetail.previous"), next: t("listDetail.next") }}
                    testId="app-feedback-detail-pager"
                  />
                ) : undefined
              }
              labels={{ resize: t("listDetail.resize"), resizeHint: t("listDetail.resizeHint") }}
            />
          </DbObjectManagementPanelShell>
        )}

        {activeView === "similarityIndex" && (
          <DbObjectManagementPanelShell
            id="feedback-management-panel-similarityIndex"
            labelledBy="feedback-management-tab-similarityIndex"
            ariaLabel={t("feedbackManagement.workspace.similarityIndex")}
            idPrefix="feedback-management-similarity-index"
            processing={refreshProcessing}
          >
            <DbObjectPanelHeader
              title={t("feedbackManagement.similarityIndex.title")}
              description={t("feedbackManagement.similarityIndex.hint")}
              icon={DatabaseZap}
            />
            {initialLoading ? (
              <SettingsFormSkeleton
                label={t("feedbackManagement.similarityIndex.loading")}
                testId="feedback-similarity-index-loading"
              />
            ) : (
            <>
            {/* 設定の欄の並びは共通の FormGrid（最大 60rem・2 列。#1891）。 */}
            <FormGrid>
              <SimilarityConfigField
                id="feedback-similarity-threshold"
                label={t("feedbackManagement.similarityIndex.threshold")}
                hint={t("feedbackManagement.similarityIndex.thresholdHint")}
                min={0}
                max={1}
                step={0.05}
                value={feedbackSearchConfigOrDefault(feedbackConfig).similarity_threshold}
                onChange={(value) => {
                  setFeedbackConfig((current) => ({
                    similarity_threshold: clamp(value, 0, 1),
                    match_limit: feedbackSearchConfigOrDefault(current).match_limit,
                  }));
                }}
              />
              <SimilarityConfigField
                id="feedback-similarity-match-limit"
                label={t("feedbackManagement.similarityIndex.matchLimit")}
                hint={t("feedbackManagement.similarityIndex.matchLimitHint")}
                min={1}
                max={20}
                step={1}
                value={feedbackSearchConfigOrDefault(feedbackConfig).match_limit}
                onChange={(value) => {
                  setFeedbackConfig((current) => ({
                    similarity_threshold: feedbackSearchConfigOrDefault(current).similarity_threshold,
                    match_limit: Math.round(clamp(value, 1, 20)),
                  }));
                }}
              />
            </FormGrid>
            <FormActionBar
              ariaLabel={t("feedbackManagement.similarityIndex.configActions")}
              testId="feedback-similarity-index-actions"
              status={
                actionResultFor("similarityIndex") ? (
                  <ActionResultStatus result={actionResultFor("similarityIndex")} />
                ) : feedbackConfigDirty ? (
                  <p className="text-sm text-fg-muted">
                    {t("feedbackManagement.similarityIndex.configDirty")}
                  </p>
                ) : null
              }
              primaryActions={[
                {
                  id: "save-config",
                  label: t("feedbackManagement.similarityIndex.saveConfig"),
                  icon: Save,
                  loading: loading === "feedback-config",
                  disabled: !feedbackConfig || !feedbackConfigDirty,
                  onClick: () => void saveFeedbackConfig(),
                },
              ]}
            />
            </>
            )}
          </DbObjectManagementPanelShell>
        )}
        </fieldset>
      </PageBody>
    </>
  );
}

/** 一覧・詳細の操作の結果（失敗）。起点の操作の直下に Banner で出す（messaging.md §10.1 / §10.2）。 */
function ActionResultBanner({ result }: { result: ActionResult }) {
  if (!result) return null;
  return <Banner severity={result.tone}>{result.message}</Banner>;
}

/** フォームの操作の行（FormActionBar の status）に出す操作の結果（messaging.md §3.3 / §10.2）。 */
function ActionResultStatus({ result }: { result: ActionResult }) {
  if (!result) return null;
  return <FormStatus tone={result.tone} message={result.message} />;
}

/** フィードバック履歴の編集欄の Skeleton（最初の読み込み）。経過時間は一覧の領域が 1 か所だけ出す。 */
function AppFeedbackEditorSkeleton() {
  return (
    <div
      className="grid min-w-0 content-start gap-4"
      aria-label={t("feedbackManagement.appFeedback.loading")}
      aria-busy="true"
      data-testid="app-feedback-editor-skeleton"
    >
      <Skeleton className="h-16" />
      <Skeleton className="h-20" />
      <div className="grid min-w-0 gap-3 sm:grid-cols-2">
        <Skeleton className="h-16" />
        <Skeleton className="h-16" />
      </div>
      <Skeleton className="h-40" />
      <Skeleton className="h-24" />
    </div>
  );
}

/** 設定のタブ（ベクトルインデックス・類似検索インデックス）の最初の読み込み。欄と操作の形の Skeleton。 */
function SettingsFormSkeleton({ label, testId }: { label: string; testId: string }) {
  return (
    <TimedLoadingState
      label={label}
      operationKey="feedback-management-initial-load"
      placement="panel"
      className="content-start"
      testId={testId}
      framed={false}
      activityIcon="none"
    >
      <FormSkeleton fields={2} title={false} />
    </TimedLoadingState>
  );
}

/**
 * フィードバック履歴の一覧の列（ListDetailLayout の狭い列。#2283）。質問（2 行まで）と補足 1 行（評価の日時・
 * 利用者評価・管理者レビュー）だけにし、実行エンジン・処理時間・業務プロファイル・コメントは詳細で見る
 * （page-archetypes.md §1 B「一覧の行」）。行は列の残りの高さの中で縦にスクロールする。
 */
function FeedbackHistoryList({
  items,
  selectedId,
  onSelect,
}: {
  items: FeedbackRecord[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  return (
    <DataTable<FeedbackRecord>
      columns={[
        {
          key: "question",
          header: t("feedbackManagement.appFeedback.history"),
          rowHeader: true,
          className: "py-2.5 align-top",
          render: (item) => (
            <RowTitleButton
              title={<span data-testid="feedback-history-question">{item.question}</span>}
              fullTitle={item.question}
              maxLines={2}
              subtitle={
                <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-sans tabular-nums text-fg">
                    {formatDateTime(item.feedback_updated_at || item.created_at)}
                  </span>
                  <StatusBadge
                    variant={item.feedback_rating ? "success" : "neutral"}
                    label={userFeedbackRatingBadgeLabel(item.feedback_rating)}
                  />
                  <StatusBadge
                    variant={item.admin_feedback_rating === "good" ? "success" : "neutral"}
                    label={adminFeedbackReviewBadgeLabel(item.admin_feedback_rating)}
                  />
                </span>
              }
              current={item.id === selectedId}
              aria-label={t("feedbackManagement.appFeedback.selectNamed", { question: item.question })}
              onClick={() => onSelect(item.id)}
              data-testid="feedback-history-row"
            />
          ),
        },
      ]}
      rows={items}
      getRowKey={(item) => item.id}
      onRowClick={(item) => onSelect(item.id)}
      selectedRowKey={selectedId || null}
      rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
      ariaLabel={t("feedbackManagement.appFeedback.historyList")}
      scrollTestId="feedback-history-list"
      stickyHeader
      // 一覧の列の高さの中で縦にスクロールし、表頭を固定する。列の枠と二重にしない。
      className="min-h-0 rounded-none border-0"
      tableClassName="w-full table-fixed"
    />
  );
}

/**
 * 数値欄の入力中の文字列を欄の中だけで持つ（#968）。打鍵ごとに範囲へ丸めて表示を上書きすると、
 * 途中の文字列（空・`0`・`0.`）が最小値に置き換わり、`0.5` のような値をキーボードで入力できない。
 * 範囲内の数として読めたときだけ親の値を変え、欄を離れたら確定した値の表示に戻す。
 */
function useNumberDraft(
  value: number,
  min: number,
  max: number,
  onChange: (value: number) => void
) {
  const [draft, setDraft] = useState<string | null>(null);
  return {
    value: draft ?? String(value),
    onChange: (event: ChangeEvent<HTMLInputElement>) => {
      const text = event.currentTarget.value;
      setDraft(text);
      const next = Number(text);
      if (text.trim() !== "" && Number.isFinite(next) && next >= min && next <= max) onChange(next);
    },
    onBlur: () => setDraft(null),
  };
}

function SimilarityConfigField({
  id,
  label,
  hint,
  min,
  max,
  step,
  value,
  onChange,
}: {
  id: string;
  label: string;
  hint: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (value: number) => void;
}) {
  const hintId = `${id}-hint`;
  const numberDraft = useNumberDraft(value, min, max, onChange);
  return (
    <fieldset className="grid gap-2 text-sm font-medium text-fg">
      <legend className="mb-1">{label}</legend>
      <input
        type="range"
        aria-label={`${label} slider`}
        aria-describedby={hintId}
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.currentTarget.value))}
        className="w-full accent-accent-emphasis"
      />
      {/* 見出しは fieldset の legend が持つので、数値欄のラベルは読み上げだけにする（スライダーと 2 つ目のラベルを出さない。#631）。 */}
      <TextField
        id={id}
        label={label}
        labelHidden
        type="number"
        min={min}
        max={max}
        step={step}
        inputMode="decimal"
        aria-describedby={hintId}
        // 数値の欄（#1891 の幅の段）。
        width="xs"
        {...numberDraft}
      />
      <span id={hintId} className="text-xs font-normal leading-5 text-fg-muted">
        {hint}
      </span>
    </fieldset>
  );
}
