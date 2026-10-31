import {
  type ChangeEvent,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";
import {
  Code2,
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
  TableSkeleton,
  toast,
  type DataTableColumn,
  StatusBadge,
  PageHeader,
  PageBody,
  useConfirm,
  SelectField,
  ProcessingIndicator,
  TimedLoadingState,
  ObjectActionBar,
  type EntityAction,
  FixedSplitPane,
  ListDetailLayout,
  ListDetailPager,
  adjacentListItems,
  FormActionBar,
  FormGrid,
  DataTable,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
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

import { FIXED_SPLIT_STORAGE_PREFIX, LIST_DETAIL_STORAGE_PREFIX } from "@/lib/ui-store";

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
import { DbObjectName } from "../components/DbObjectName";
import type { DbObjectNameSource } from "../dbObjectIdentity";
import { HistoryRowListSkeleton } from "../components/HistoryRowList";
import { QuestionText } from "../components/QuestionText";
import { engineLabel } from "../labels";
import { profileDisplayLabel, profileRecordDisplayLabel } from "../profileDisplay";
import { BUSINESS_SELECT_AI_DB_PROFILES_URL } from "../selectAiProfileUrls";
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
  SelectAiDbProfile,
  SelectAiDbProfilesData,
  SelectAiFeedbackEntriesData,
  SelectAiFeedbackEntry,
  SelectAiFeedbackMutationData,
} from "../types";
import { formatElapsedDuration as formatElapsed } from "@/lib/operationTiming";

type FeedbackManagementView = "entries" | "vectorIndex" | "appFeedback" | "similarityIndex";
type AppFeedbackFilter = "all" | FeedbackRating | "unrated";
type AppFeedbackFilters = {
  rating?: AppFeedbackFilter;
  profileId?: string;
  query?: string;
};
/** 取り直しの契機。reset: 絞り込みの変更（先頭のページへ）、page: ページ送り、current: 同じページを取り直す。 */
type AppFeedbackRefreshDirection = "reset" | "page" | "current";
/** 一覧（フィードバック履歴・Select AI feedback）の取得の位置（offset 型のページング。#1891）。 */
type ListPosition = { offset: number; limit: number };
/**
 * 操作の結果（失敗・未実行・警告）を出す位置。起点の操作の直下に出し、ページ先頭へ送らない
 * （messaging.md §10.1、#724）。ページ先頭の PageNotice は画面全体の読み込みの失敗だけにする。
 * entries = Select AI feedback の「再読み込み」の下、entryDetail = 選んだ SQL の操作の下、
 * vectorIndex / appFeedback / similarityIndex = 各タブの操作の行（FormActionBar の status）、
 * appFeedbackList = 履歴の絞り込みの下。
 */
type ActionResultOrigin =
  | "entries"
  | "entryDetail"
  | "vectorIndex"
  | "appFeedbackList"
  | "appFeedback"
  | "similarityIndex";
type ActionResult = { origin: ActionResultOrigin; tone: FeedbackTone; message: string } | null;

const DEFAULT_FEEDBACK_MANAGEMENT_VIEW: FeedbackManagementView = "appFeedback";
/** フィードバック履歴の一覧 + 詳細（ListDetailLayout）の識別子（幅の保存 key と data-testid。#2283）。 */
const APP_FEEDBACK_LIST_DETAIL_ID = "feedback-management";
/** Select AI feedback の既定の表示件数（一覧の既定は 25 件。選択肢は 10 / 25 / 50 / 100。#1891）。 */
const SELECT_AI_FEEDBACK_PAGE_SIZE = 25;

const FEEDBACK_MANAGEMENT_TABS: Array<DbObjectTab<FeedbackManagementView>> = [
  { id: "appFeedback", label: t("feedbackManagement.tabs.appFeedback"), icon: MessageSquareText },
  { id: "entries", label: t("feedbackManagement.tabs.entries"), icon: MessageSquareText },
  { id: "vectorIndex", label: t("feedbackManagement.tabs.vectorIndex"), icon: DatabaseZap },
  { id: "similarityIndex", label: t("feedbackManagement.tabs.similarityIndex"), icon: DatabaseZap },
];

function profileOptionLabel(profile: SelectAiDbProfile) {
  return profile.owner ? `${profile.name} (${profile.owner})` : profile.name;
}

function defaultSelectAiResponse(item: FeedbackRecord) {
  return item.executable_sql || item.generated_sql;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function roundThreshold(value: number) {
  return Number(clamp(value, 0.1, 0.95).toFixed(2));
}

function resolveFeedbackManagementView(tab: string | null): FeedbackManagementView {
  if (tab === "appFeedback" || tab === "entries" || tab === "vectorIndex" || tab === "similarityIndex") {
    return tab;
  }
  return DEFAULT_FEEDBACK_MANAGEMENT_VIEW;
}

export function FeedbackManagementPage() {
  const confirm = useConfirm();
  const [searchParams] = useSearchParams();
  const requestedView = resolveFeedbackManagementView(searchParams.get("tab"));
  const [activeView, setActiveView] = useState<FeedbackManagementView>(requestedView);
  const [dbProfiles, setDbProfiles] = useState<SelectAiDbProfilesData | null>(null);
  const [profileName, setProfileName] = useState("");
  const [feedback, setFeedback] = useState<SelectAiFeedbackEntriesData | null>(null);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [similarityThreshold, setSimilarityThreshold] = useState(0.9);
  const [matchLimit, setMatchLimit] = useState(3);
  const [history, setHistory] = useState<FeedbackRecord[]>([]);
  const [appProfiles, setAppProfiles] = useState<ProfileSummary[]>([]);
  // フィードバック履歴で選んだ履歴は URL の `?feedback=` が唯一の情報源（共有 ListDetailLayout。#2283）。
  // 先頭の行を自動で選ばない。keep-alive の間はほかの画面の URL を読まない（useWorkspaceUrlSelection）。
  const selection = useWorkspaceUrlSelection("feedback");
  const selectedFeedbackId = selection.selectedId ?? "";
  const [adminFeedbackRating, setAdminFeedbackRating] = useState<FeedbackRating>("good");
  const [adminFeedbackContent, setAdminFeedbackContent] = useState("");
  const [registerSelectAiFeedback, setRegisterSelectAiFeedback] = useState(false);
  const [selectAiResponse, setSelectAiResponse] = useState("");
  // 管理者レビューの欄のエラー（欄の直下に出す。#541）。
  const [reviewErrors, setReviewErrors] = useState<{ adminContent?: string; selectAiResponse?: string }>({});
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
  // Select AI feedback のページ番号と表示件数（サーバー側の offset 型。作業状態に残す。#1891）。
  const entriesPaging = useWorkspacePaging("selectAiFeedbackEntriesPaging", SELECT_AI_FEEDBACK_PAGE_SIZE);
  const [feedbackConfig, setFeedbackConfig] = useState<FeedbackSearchConfigData | null>(null);
  const [savedFeedbackConfig, setSavedFeedbackConfig] = useState<FeedbackSearchConfigData | null>(null);
  const [savedReview, setSavedReview] = useState("");
  const reviewDirtyRef = useRef(false);
  // 初回の読込は mount 時の effect で始まるため、最初から読込中にしておく。
  const [loading, setLoading] = useState("load");
  // どのボタンが始めた読込か。スピナーは押したボタンだけが出す（#819）。初回の読込は §3.7 のとおり
  // ヘッダーの「最新情報を取得」が出す。業務プロファイルの切り替え・保存の後の取り直しは null（領域の表示だけ）。
  const [refreshOrigin, setRefreshOrigin] = useState<"header" | "notice" | "entries" | null>("header");
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

  const profiles = dbProfiles?.profiles ?? [];
  // feedback が変わったときだけ新しい配列になるよう、空配列の既定値も useMemo で持つ。
  const selectAiFeedbackItems = useMemo(() => feedback?.items ?? [], [feedback]);
  const selectedSelectAiFeedback = useMemo(
    () => selectAiFeedbackItems[selectedIndex] ?? selectAiFeedbackItems[0] ?? null,
    [selectAiFeedbackItems, selectedIndex]
  );
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
    adminFeedbackContent, registerSelectAiFeedback, selectAiResponse]);
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
  // Select AI feedback のエントリがまだ無い間（最初の読み込みと、profile を切り替えた直後。#265）。
  const initialEntriesLoading =
    feedback === null && (loading === "feedback" || initialLoading);

  const currentEntriesPosition = (): ListPosition => ({
    offset: entriesPaging.offset,
    limit: entriesPaging.pageSize,
  });

  const fetchSelectAiFeedbackPage = (name: string, position: ListPosition, signal?: AbortSignal) => {
    const params = new URLSearchParams({
      profile_name: name,
      limit: String(position.limit),
      offset: String(position.offset),
    });
    return apiGet<SelectAiFeedbackEntriesData>(`/api/nl2sql/select-ai/feedback?${params.toString()}`, {
      signal,
    });
  };

  // Select AI feedback の 1 ページ（サーバー側の offset 型。#1891）。取得したページが範囲の外（削除・profile の
  // 切り替えで減った）なら、最後のページを取り直す（空のページを出さない）。
  const fetchSelectAiFeedback = async (
    name: string,
    position: ListPosition = currentEntriesPosition(),
    signal?: AbortSignal
  ): Promise<SelectAiFeedbackEntriesData> => {
    const data = await fetchSelectAiFeedbackPage(name, position, signal);
    if (data.items.length > 0 || position.offset === 0 || data.total === 0) return data;
    return fetchSelectAiFeedbackPage(
      name,
      { offset: lastPageOffset(data.total, position.limit), limit: position.limit },
      signal
    );
  };

  // 表示した Select AI feedback のページを作業状態に残し、そのページの先頭の行を選ぶ（取得に成功したときだけ）。
  const showSelectAiFeedback = (data: SelectAiFeedbackEntriesData | null) => {
    setFeedback(data);
    setSelectedIndex(0);
    if (!data) return;
    const limit = data.limit || entriesPaging.pageSize;
    if (limit !== entriesPaging.pageSize) entriesPaging.setPageSize(limit);
    entriesPaging.setPage(Math.floor((data.offset ?? 0) / Math.max(1, limit)) + 1);
  };

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
          dbProfileData,
          appProfileData,
          appFeedbackData,
          configData,
        ] = await Promise.all([
          apiGet<SelectAiDbProfilesData>(BUSINESS_SELECT_AI_DB_PROFILES_URL, {
            signal,
          }),
          apiGet<ProfileSummaryPage>("/api/nl2sql/profiles/search?limit=100", {
            signal,
          }),
          fetchCurrentAppFeedback(signal),
          apiGet<FeedbackSearchConfigData>("/api/nl2sql/feedback-config", { signal }),
        ]);
        const hasCurrentProfile = dbProfileData.profiles.some(
          (profile) => profile.name === profileName
        );
        const nextProfile = !profileName
          ? dbProfileData.profiles[0]?.name || ""
          : hasCurrentProfile
            ? profileName
            : "";
        const feedbackData = nextProfile
          ? await fetchSelectAiFeedback(nextProfile, currentEntriesPosition(), signal)
          : null;
        if (signal.aborted || sequence !== loadSequence.current) return;
        setDbProfiles(dbProfileData);
        setProfileName(nextProfile);
        showSelectAiFeedback(feedbackData);
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

  /**
   * Select AI feedback を取り直す。position の既定は今のページ（「再読み込み」・登録の後）。ページ送り・表示件数の
   * 変更はその位置、業務プロファイルの切り替えは先頭のページを渡す。失敗したら表示していたページに留まる。
   */
  const refreshSelectAiFeedback = async (
    name = profileName,
    announce = false,
    origin: "entries" | null = null,
    position: ListPosition = currentEntriesPosition()
  ) => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setLoading("feedback");
    setRefreshOrigin(origin);
    setActionResult(null);
    try {
      showSelectAiFeedback(await fetchSelectAiFeedback(trimmed, position));
      if (announce) toast.success(t("common.action.refreshed"));
    } catch (err) {
      showActionError("entries", err, t("feedbackManagement.error.load"));
    } finally {
      setLoading("");
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

  const changeProfile = (nextProfile: string) => {
    setProfileName(nextProfile);
    // 前の profile のエントリを、切り替えた profile の見出しの下に出したままにしない。
    setFeedback(null);
    void refreshSelectAiFeedback(nextProfile, false, null, { offset: 0, limit: entriesPaging.pageSize });
  };

  const deleteSelectedFeedback = async () => {
    if (loading) return;
    if (!selectedSelectAiFeedback || !profileName.trim()) return;
    const ok = await confirm({
      title: t("feedbackManagement.deleteConfirmTitle"),
      description: t("feedbackManagement.deleteConfirmDescription"),
      confirmLabel: t("feedbackManagement.delete"),
      tone: "danger",
      dismissOnOverlay: false,
    });
    if (!ok) return;
    setLoading("delete");
    setActionResult(null);
    try {
      const data = await apiPost<SelectAiFeedbackMutationData>("/api/nl2sql/select-ai/feedback/delete", {
        profile_name: profileName,
        sql_text: selectedSelectAiFeedback.sql_text,
      });
      const resultMessage = data.warnings.join(" ") || t("feedbackManagement.deleted");
      if (data.executed) toast.success(resultMessage);
      else setActionResult({ origin: "entryDetail", tone: "danger", message: resultMessage });
      showSelectAiFeedback(await fetchSelectAiFeedback(profileName));
    } catch (err) {
      showActionError("entryDetail", err, t("feedbackManagement.error.delete"));
    } finally {
      setLoading("");
    }
  };

  const updateVectorIndex = async () => {
    if (loading) return;
    if (!profileName.trim()) return;
    setLoading("vector-index");
    setActionResult(null);
    try {
      const data = await apiPost<SelectAiFeedbackMutationData>("/api/nl2sql/select-ai/feedback/vector-index", {
        profile_name: profileName,
        similarity_threshold: similarityThreshold,
        match_limit: matchLimit,
      });
      const resultMessage = data.warnings.join(" ") || t("feedbackManagement.index.updated");
      if (data.executed) toast.success(resultMessage);
      else setActionResult({ origin: "vectorIndex", tone: "danger", message: resultMessage });
      showSelectAiFeedback(await fetchSelectAiFeedback(profileName));
    } catch (err) {
      showActionError("vectorIndex", err, t("feedbackManagement.error.update"));
    } finally {
      setLoading("");
    }
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
      selectAiResponse:
        registerSelectAiFeedback && !selectAiResponse.trim()
          ? t("feedbackManagement.appFeedback.selectAiResponseRequired")
          : undefined,
    };
    setReviewErrors(nextErrors);
    if (nextErrors.adminContent || nextErrors.selectAiResponse) {
      setActionResult(null);
      const target = nextErrors.adminContent
        ? adminFeedbackContentRef.current
        : document.getElementById("app-feedback-select-ai-response");
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
        register_select_ai_feedback: registerSelectAiFeedback,
        select_ai_response: selectAiResponse.trim(),
        // 登録先は対象の履歴の業務プロファイルの Select AI profile（空なら backend が履歴から決める）。
        // 「Select AI feedback」タブの profile の選択は、このタブに表示も選択欄も無いので使わない（#968）。
        select_ai_profile_name: "",
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
      if (data.select_ai_feedback) {
        const selectAiMessage = data.select_ai_feedback.warnings.join(" ");
        if (data.select_ai_feedback.executed) {
          toast.success(t("feedbackManagement.appFeedback.adminSavedAndRegistered"));
          await refreshSelectAiFeedback(profileName);
        } else {
          setActionResult({
            origin: "appFeedback",
            tone: "danger",
            message: selectAiMessage || t("feedbackManagement.appFeedback.selectAiRegistrationFailed"),
          });
        }
      } else {
        toast.success(
          publishedToSimilarHistory
            ? t("feedbackManagement.appFeedback.adminSavedAndPublished")
            : t("feedbackManagement.appFeedback.adminSaved")
        );
      }
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
      setRegisterSelectAiFeedback(false);
      setSelectAiResponse("");
      setReviewErrors({});
    } else {
      const switchedFeedback = syncedAppFeedbackId !== selectedAppFeedback.id;
      if (switchedFeedback || !reviewDirty) {
        if (switchedFeedback) setReviewErrors({});
        setSyncedAppFeedbackId(selectedAppFeedback.id);
        setSavedReview(JSON.stringify([selectedAppFeedback.id,
          selectedAppFeedback.admin_feedback_rating ?? "good",
          selectedAppFeedback.admin_feedback_content ?? "",
          switchedFeedback ? false : registerSelectAiFeedback,
          switchedFeedback ? defaultSelectAiResponse(selectedAppFeedback) : selectAiResponse]));
        setAdminFeedbackRating(selectedAppFeedback.admin_feedback_rating ?? "good");
        setAdminFeedbackContent(selectedAppFeedback.admin_feedback_content ?? "");
        if (switchedFeedback) {
          setRegisterSelectAiFeedback(false);
          setSelectAiResponse(defaultSelectAiResponse(selectedAppFeedback));
        }
      }
    }
  }

  const profileSelect = (
    <ProfileSelect
      profiles={profiles}
      value={profileName}
      disabled={loading !== ""}
      onChange={changeProfile}
    />
  );
  const entriesProfileSelect = (
    <ProfileSelect
      profiles={profiles}
      value={profileName}
      disabled={loading !== ""}
      onChange={changeProfile}
    />
  );

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
            message && activeView === "entries" ? (
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

        {activeView === "entries" && (
          <DbObjectManagementPanelShell
            id="feedback-management-panel-entries"
            labelledBy="feedback-management-tab-entries"
            ariaLabel={t("feedbackManagement.workspace.entries")}
            idPrefix="feedback-management-entries"
            processing={refreshProcessing}
          >
            <section className="grid min-w-0 gap-4" data-testid="feedback-management-entries-workspace">
              <DbObjectPanelHeader
                title={t("feedbackManagement.entries.title")}
                description={t("feedbackManagement.entries.hint")}
                icon={MessageSquareText}
              />

              {/* profile の選択（1）と実行環境の情報（2）を、広い画面では同じ行に置く。 */}
              <section
                className="grid min-w-0 gap-3 rounded-md border border-border bg-surface-sunken p-3 2xl:grid-cols-[minmax(0,1fr)_minmax(0,2fr)] 2xl:items-end 2xl:gap-x-6"
                aria-label={t("feedbackManagement.entries.context")}
                data-testid="feedback-management-entries-toolbar"
              >
                <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end">
                  {entriesProfileSelect}
                  <Button
                    type="button"
                    variant="secondary"
                    // 隣の業務プロファイルの選択（md）と同じ高さ（#613）。
                    size="md"
                    className="w-full whitespace-nowrap sm:w-auto"
                    // 押したときの取り直しだけ回す。業務プロファイルの切り替え・保存の後は下の領域の表示だけ（#819）。
                    loading={loading === "feedback" && refreshOrigin === "entries"}
                    disabled={
                      !profileName.trim() ||
                      (Boolean(loading) && !(loading === "feedback" && refreshOrigin === "entries"))
                    }
                    onClick={() => void refreshSelectAiFeedback(profileName, false, "entries")} icon={RefreshCw}>
                    <span>{t("feedbackManagement.action.refresh")}</span>
                  </Button>
                </div>

                <dl
                  className="grid min-w-0 gap-2 sm:grid-cols-[auto_minmax(0,1fr)_minmax(0,1fr)]"
                  aria-label={t("feedbackManagement.entries.runtimeInfo")}
                  data-testid="feedback-management-entries-runtime-info"
                >
                  <div className="min-w-0 self-start">
                    <dt className="sr-only">{t("feedbackManagement.entries.runtime")}</dt>
                    <dd>
                      <StatusBadge
                        icon={false}
                        variant={feedback?.runtime === "oracle" ? "success" : "neutral"}
                        label={t("feedbackManagement.entries.runtimeBadge", {
                          value: feedback?.runtime || dbProfiles?.runtime || "-",
                        })}
                      />
                    </dd>
                  </div>
                  <TechnicalRuntimeFact
                    label={t("feedbackManagement.entries.vectorIndex")}
                    value={feedback?.index_name || "-"}
                  />
                  <TechnicalRuntimeFact
                    label={t("feedbackManagement.entries.vectorTable")}
                    value={feedback?.table_name || "-"}
                    objectName={
                      feedback?.table_name
                        ? {
                            owner: feedback.table_owner,
                            name: feedback.table_name,
                            qualified_name: feedback.table_qualified_name,
                          }
                        : undefined
                    }
                  />
                </dl>
              </section>

              <ActionResultBanner result={actionResultFor("entries")} />
              <FeedbackWarnings warnings={feedback?.warnings ?? dbProfiles?.warnings ?? []} />
              {loading === "feedback" ? (
                <ProcessingIndicator
                  active
                  label={t(feedback ? "feedbackManagement.entries.refreshing" : "feedbackManagement.entries.loading")}
                  operationKey={profileName}
                  placement="workspace"
                  className="rounded-md border border-border bg-surface-sunken px-3 py-2"
                  testId="feedback-management-entries-processing"
                  // ボタンが回っていないとき（業務プロファイルの切り替えなど）は、この表示がスピナーを出す（#819）。
                  activityIcon={refreshOrigin === "entries" ? "none" : "spinner"}
                />
              ) : null}

              <FixedSplitPane
                storagePrefix={FIXED_SPLIT_STORAGE_PREFIX}
                splitId="feedback-management-entries-split"
                minLeftPaneWidthPx={560}
                minRightPaneWidthPx={400}
                left={
                  initialEntriesLoading ? (
                    <FeedbackEntriesListSkeleton timed={initialLoading} />
                  ) : (
                    <FeedbackEntriesList
                      entries={selectAiFeedbackItems}
                      total={feedback?.total ?? selectAiFeedbackItems.length}
                      offset={feedback?.offset ?? entriesPaging.offset}
                      limit={feedback?.limit || entriesPaging.pageSize}
                      // 取り直しの間（ページ送り・削除の後など）は、ページ送りを押せない。
                      pagingDisabled={Boolean(loading)}
                      onPageChange={(offset) =>
                        void refreshSelectAiFeedback(profileName, false, null, {
                          offset,
                          limit: feedback?.limit || entriesPaging.pageSize,
                        })
                      }
                      onPageSizeChange={(limit) =>
                        void refreshSelectAiFeedback(profileName, false, null, { offset: 0, limit })
                      }
                      selectedIndex={selectedSelectAiFeedback ? selectedIndex : null}
                      onSelect={setSelectedIndex}
                    />
                  )
                }
                right={
                  initialEntriesLoading ? (
                    <FeedbackEntryDetailSkeleton />
                  ) : (
                    <FeedbackEntryDetail
                      entry={selectedSelectAiFeedback}
                      profileName={feedback?.profile_name || profileName}
                      deleting={loading === "delete"}
                      result={actionResultFor("entryDetail")}
                      onDelete={() => void deleteSelectedFeedback()}
                    />
                  )
                }
              />
            </section>
          </DbObjectManagementPanelShell>
        )}

        {activeView === "vectorIndex" && (
          <DbObjectManagementPanelShell
            id="feedback-management-panel-vectorIndex"
            labelledBy="feedback-management-tab-vectorIndex"
            ariaLabel={t("feedbackManagement.workspace.vectorIndex")}
            idPrefix="feedback-management-vector-index"
            processing={refreshProcessing}
          >
            <DbObjectPanelHeader
              title={t("feedbackManagement.index.title")}
              description={t("feedbackManagement.index.hint")}
              icon={DatabaseZap}
              action={profileSelect}
            />
            {initialLoading ? (
              <SettingsFormSkeleton
                label={t("feedbackManagement.index.loading")}
                testId="feedback-vector-index-loading"
              />
            ) : (
            <>
            <FeedbackWarnings warnings={feedback?.warnings ?? dbProfiles?.warnings ?? []} />
            {/* 設定の欄の並びは共通の FormGrid（最大 60rem・2 列。#1891）。 */}
            <FormGrid>
              <SliderNumberField
                label={t("feedbackManagement.index.threshold")}
                min={0.1}
                max={0.95}
                step={0.05}
                value={similarityThreshold}
                onChange={(value) => setSimilarityThreshold(roundThreshold(value))}
              />
              <SliderNumberField
                label={t("feedbackManagement.index.matchLimit")}
                min={1}
                max={5}
                step={1}
                value={matchLimit}
                onChange={(value) => setMatchLimit(Math.round(clamp(value, 1, 5)))}
              />
            </FormGrid>
            <div className="flex flex-wrap gap-2">
              <StatusBadge icon={false} variant="neutral" label={feedback?.runtime ?? dbProfiles?.runtime ?? "-"} />
              {feedback?.index_name && <StatusBadge icon={false} variant="info" label={feedback.index_name} />}
              {feedback?.table_name && (
                <DbObjectName
                  object={{
                    owner: feedback.table_owner,
                    name: feedback.table_name,
                    qualified_name: feedback.table_qualified_name,
                  }}
                  size="xs"
                  className="self-center"
                  data-testid="feedback-vector-index-table-name"
                />
              )}
            </div>
            <FormActionBar
              ariaLabel={t("feedbackManagement.index.actions")}
              testId="feedback-vector-index-actions"
              status={<ActionResultStatus result={actionResultFor("vectorIndex")} />}
              primaryActions={[
                {
                  id: "update",
                  label: t("feedbackManagement.index.update"),
                  icon: Save,
                  loading: loading === "vector-index",
                  disabled: !profileName.trim(),
                  onClick: () => void updateVectorIndex(),
                },
              ]}
            />
            {loading === "vector-index" ? (
              // ベクトル索引の再作成は件数に比例して数十秒かかる（messaging.md §3.7）。
              <ProcessingIndicator
                active
                label={t("feedbackManagement.index.progress")}
                operationKey="feedback-vector-index"
                placement="action"
                activityIcon="none"
                testId="feedback-vector-index-processing"
              />
            ) : null}
            </>
            )}
          </DbObjectManagementPanelShell>
        )}

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
                  <label className="flex min-h-11 min-w-0 items-center gap-3 rounded-md border border-border bg-surface px-3 py-2 text-sm font-medium text-fg">
                    <input
                      type="checkbox"
                      checked={registerSelectAiFeedback}
                      onChange={(event) => setRegisterSelectAiFeedback(event.currentTarget.checked)}
                      aria-describedby="app-feedback-register-select-ai-hint"
                      className="h-4 w-4 shrink-0 accent-accent-emphasis"
                    />
                    <span className="grid min-w-0 gap-0.5">
                      <span>{t("feedbackManagement.appFeedback.registerSelectAi")}</span>
                      {/* 登録先を明示する（「Select AI feedback」タブの profile の選択とは関係しない。#968）。 */}
                      <span
                        id="app-feedback-register-select-ai-hint"
                        className="text-xs font-normal leading-5 text-fg-muted"
                      >
                        {t("feedbackManagement.appFeedback.registerSelectAiHint")}
                      </span>
                    </span>
                  </label>
                  {registerSelectAiFeedback && (
                    // Select AI feedback に登録するときは response SQL が必須（saveAppFeedback のガード）。
                    <TextareaField
                      id="app-feedback-select-ai-response"
                      label={t("feedbackManagement.appFeedback.selectAiResponse")}
                      required
                      error={reviewErrors.selectAiResponse}
                      surface="code"
                      width="full"
                      value={selectAiResponse}
                      onChange={(event) => {
                        setSelectAiResponse(event.currentTarget.value);
                        setReviewErrors((current) => ({ ...current, selectAiResponse: undefined }));
                      }}
                      rows={5}
                      textareaClassName="min-w-0 max-w-full"
                      placeholder={t("feedbackManagement.appFeedback.selectAiResponsePlaceholder")}
                    />
                  )}
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

function ProfileSelect({
  profiles,
  value,
  disabled,
  onChange,
}: {
  profiles: SelectAiDbProfile[];
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const id = useId();
  return (
    <SelectField
      id={`feedback-db-profile-${id}`}
      label={t("feedbackManagement.profile")}
      value={value}
      options={profiles.map((profile) => ({ value: profile.name, label: profileOptionLabel(profile) }))}
      disabled={disabled || profiles.length === 0}
      onValueChange={onChange}
      // Select AI のプロファイル（対象の選択）は lg（#613 / #1891 の幅の段）。
      width="lg"
    />
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

function FeedbackWarnings({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null;
  return (
    <div className="grid gap-2">
      {warnings.map((warning) => (
        <Banner key={warning} severity="warning">
          {warning}
        </Banner>
      ))}
    </div>
  );
}

function feedbackEntryRowKey(entry: SelectAiFeedbackEntry, index: number) {
  return `${entry.sql_id || entry.sql_text || "feedback"}-${index}`;
}

function FeedbackEntriesList({
  entries,
  total,
  offset,
  limit,
  pagingDisabled,
  onPageChange,
  onPageSizeChange,
  selectedIndex,
  onSelect,
}: {
  /** 今のページの行（サーバー側の offset 型のページング。#1891）。 */
  entries: SelectAiFeedbackEntry[];
  /** Select AI の feedback の全件（API の total）。 */
  total: number;
  offset: number;
  limit: number;
  pagingDisabled: boolean;
  onPageChange: (offset: number) => void;
  onPageSizeChange: (limit: number) => void;
  selectedIndex: number | null;
  onSelect: (index: number) => void;
}) {
  const columns: Array<DataTableColumn<SelectAiFeedbackEntry>> = [
    {
      key: "content",
      header: t("feedbackManagement.entries.content"),
      className: "align-top",
      headerClassName: "w-[42%] uppercase",
      render: (entry) => (
        <RowTitleButton
          title={entry.content || "-"}
          // 長い質問は 3 行で切り詰め、全文はホバー・フォーカスの Tooltip と右の詳細で見せる。
          maxLines={3}
          current={entries.indexOf(entry) === selectedIndex}
          aria-label={t("feedbackManagement.entries.select", {
            content: entry.content || entry.sql_text || "-",
          })}
          onClick={(event) => {
            event.stopPropagation();
            onSelect(entries.indexOf(entry));
          }}
        />
      ),
    },
    {
      key: "sql_text",
      header: t("feedbackManagement.entries.sqlText"),
      className: "align-top",
      headerClassName: "w-[58%] uppercase",
      render: (entry) => (
        <p className="line-clamp-3 break-words font-mono text-xs leading-5 text-fg">
          {entry.sql_text || "-"}
        </p>
      ),
    },
  ];

  return (
    <section className="grid min-w-0 content-start gap-3" aria-labelledby="feedback-entries-list-heading">
      <DbObjectPanelHeader
        headingId="feedback-entries-list-heading"
        title={t("feedbackManagement.entries.listTitle")}
        description={t("feedbackManagement.entries.listHint")}
        icon={MessageSquareText}
      />
      {/* 件数は一覧の上の 1 か所（ListCount。#1891）。総数は API の total（サーバー側のページング）。 */}
      {total > 0 ? (
        <ListToolbar
          count={{ total: Math.max(total, offset + entries.length), labels: listCountLabels(), testId: "feedback-management-entry-count" }}
        />
      ) : null}
      <div className="grid min-w-0 gap-2">
      <DataTable<SelectAiFeedbackEntry>
        columns={columns}
        rows={entries}
        getRowKey={(entry) => feedbackEntryRowKey(entry, entries.indexOf(entry))}
        selectedRowKey={
          selectedIndex == null || !entries[selectedIndex]
            ? null
            : feedbackEntryRowKey(entries[selectedIndex], selectedIndex)
        }
        onRowClick={(entry) => {
          const index = entries.indexOf(entry);
          if (index >= 0) onSelect(index);
        }}
        rowProps={(entry) => ({
          className: INFORMATION_TABLE_ROW_CLASS,
          "aria-label": t("feedbackManagement.entries.select", {
            content: entry.content || entry.sql_text || "-",
          }),
        })}
        dense
        empty={
          <EmptyState
            title={t("feedbackManagement.entries.emptyTitle")}
            hint={t("feedbackManagement.entries.emptyHint")}
          />
        }
        ariaLabel={t("feedbackManagement.entries.listAria")}
        testId="feedback-management-entries-table"
        scrollTestId="feedback-management-entries-scroll-region"
        tableClassName="w-full min-w-[640px] table-fixed"
        stickyHeader
        visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
      />
      {/* サーバー側のページング（advanced。表示件数・ページ番号の入力。#1891）。 */}
      <OffsetPagination
        offset={offset}
        limit={limit}
        total={total}
        count={entries.length}
        onPageChange={(_page, nextOffset) => onPageChange(nextOffset)}
        onPageSizeChange={onPageSizeChange}
        labels={paginationLabels()}
        disabled={pagingDisabled}
        ariaLabel={t("feedbackManagement.entries.pagination")}
        testId="feedback-management-entries-pagination"
      />
      </div>
    </section>
  );
}

function FeedbackEntryDetail({
  entry,
  profileName,
  deleting,
  result,
  onDelete,
}: {
  entry: SelectAiFeedbackEntry | null;
  profileName: string;
  deleting: boolean;
  result: ActionResult;
  onDelete: () => void;
}) {
  const actions: EntityAction[] = entry
    ? [
        {
          id: "delete",
          label: t("feedbackManagement.delete"),
          icon: Trash2,
          tone: "danger",
          loading: deleting,
          onSelect: onDelete,
        },
      ]
    : [];

  return (
    <section
      className="grid min-w-0 content-start gap-3 rounded-md border border-border bg-surface-sunken p-4"
      aria-labelledby="feedback-entry-detail-heading"
      data-testid="feedback-management-entry-detail"
    >
      <DbObjectPanelHeader
        headingId="feedback-entry-detail-heading"
        title={t("feedbackManagement.entries.selectedSql")}
        description={entry ? t("feedbackManagement.entries.detailHint") : undefined}
        icon={Code2}
        action={
          actions.length > 0 ? (
            <ObjectActionBar
              ariaLabel={t("feedbackManagement.entries.selectedSqlActions")}
              testId="feedback-selected-sql-actions"
              actions={actions}
            />
          ) : undefined
        }
      />
      <ActionResultBanner result={result} />

      {entry ? (
        <>
          <div className="grid min-w-0 gap-3 sm:grid-cols-2">
            <CompactFact label={t("feedbackManagement.entries.sqlId")} value={entry.sql_id || "-"} />
            <CompactFact label={t("feedbackManagement.profile")} value={profileName || "-"} />
          </div>
          <section className="rounded-md border border-border bg-surface p-3">
            <p className="text-xs font-medium text-fg-muted">{t("feedbackManagement.entries.content")}</p>
            <p className="mt-1 break-words text-sm leading-6 text-fg">{entry.content || "-"}</p>
          </section>
          <section className="grid min-w-0 gap-2">
            <p className="text-xs font-medium text-fg-muted">{t("feedbackManagement.entries.sqlText")}</p>
            <pre data-surface="code"
              aria-label={t("feedbackManagement.entries.selectedSql")}
              data-testid="feedback-management-entry-sql"
              className="max-h-[30.5rem] min-h-44 overflow-auto rounded-md border border-border bg-surface p-3 font-mono text-sm leading-6 text-fg"
            >
              <code>{entry.sql_text || "-"}</code>
            </pre>
          </section>
        </>
      ) : (
        <div
          className="grid min-h-52 place-items-center rounded-md border border-border bg-surface p-4"
          data-testid="feedback-management-entry-detail-empty"
        >
          <EmptyState
            title={t("feedbackManagement.entries.detailEmptyTitle")}
            hint={t("feedbackManagement.entries.detailEmptyHint")}
          />
        </div>
      )}
    </section>
  );
}

function FeedbackEntriesListSkeleton({ timed = false }: { timed?: boolean }) {
  const skeleton = (
    <section
      className="grid min-w-0 content-start gap-3"
      aria-label={t("feedbackManagement.entries.loading")}
      aria-busy="true"
      data-testid="feedback-management-entries-list-skeleton"
    >
      {/* profile の切り替えでは、読み込み中の文言と経過時間は分割の上の ProcessingIndicator が 1 か所だけ出す。 */}
      <Skeleton className="h-6 w-44" />
      <Skeleton className="h-5 w-72 max-w-full" />
      <TableSkeleton columns={2} />
    </section>
  );
  if (!timed) return skeleton;
  // 最初の読み込みは一覧の領域の先頭に経過時間を 1 か所だけ出す（スピナーはヘッダーの「最新情報を取得」。#1777）。
  return (
    <TimedLoadingState
      label={t("feedbackManagement.entries.loading")}
      operationKey="feedback-management-initial-load"
      placement="panel"
      className="content-start"
      testId="feedback-management-entries-loading"
      framed={false}
      activityIcon="none"
    >
      {skeleton}
    </TimedLoadingState>
  );
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

function TechnicalRuntimeFact({
  label,
  value,
  objectName,
}: {
  label: string;
  value: string;
  /** 表名の場合は所有者付きの修飾名を等幅で示す。 */
  objectName?: DbObjectNameSource;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-baseline gap-x-1 rounded-md border border-border bg-surface px-2 py-1 text-xs leading-5">
      <dt className="shrink-0 font-medium text-fg-muted">{label}:</dt>
      <dd className="min-w-0 break-all font-medium text-fg">
        {objectName ? <DbObjectName object={objectName} size="xs" /> : value}
      </dd>
    </div>
  );
}

function FeedbackEntryDetailSkeleton() {
  return (
    <section
      className="grid min-w-0 content-start gap-3 rounded-md border border-border bg-surface-sunken p-4"
      aria-label={t("feedbackManagement.entries.loading")}
      aria-busy="true"
      data-testid="feedback-management-entry-detail-skeleton"
    >
      <Skeleton className="h-6 w-52 max-w-full" />
      <Skeleton className="h-5 w-64 max-w-full" />
      <div className="grid gap-3 sm:grid-cols-2">
        <Skeleton className="h-16" />
        <Skeleton className="h-16" />
      </div>
      <Skeleton className="h-24" />
      <Skeleton className="h-56" />
    </section>
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

function SliderNumberField({
  label,
  min,
  max,
  step,
  value,
  onChange,
}: {
  label: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (value: number) => void;
}) {
  const id = useId();
  const numberDraft = useNumberDraft(value, min, max, onChange);
  return (
    <fieldset className="grid gap-3 rounded-md border border-border bg-surface-sunken p-4 text-sm font-medium text-fg">
      <legend className="px-1">{label}</legend>
      <input
        type="range"
        aria-label={`${label} slider`}
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.currentTarget.value))}
        className="w-full accent-accent-emphasis"
      />
      {/* 見出しは fieldset の legend が持つので、数値欄のラベルは読み上げだけにする（#631）。 */}
      <TextField
        id={id}
        label={label}
        labelHidden
        type="number"
        min={min}
        max={max}
        step={step}
        // 数値の欄（#1891 の幅の段）。
        width="xs"
        {...numberDraft}
      />
    </fieldset>
  );
}

function CompactFact({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-surface p-3">
      <p className="text-xs font-medium text-fg-muted">{label}</p>
      <p className="mt-1 break-words text-sm font-semibold text-fg">{value}</p>
      {hint ? <p className="mt-1 text-xs leading-5 text-fg-muted">{hint}</p> : null}
    </div>
  );
}
