import { useWorkspaceState, useWorkspaceRevalidation, useWorkspaceDraftWriter, useWorkspaceActive, useTransientDraftGuard, WorkspaceResultNotice } from "@/components/WorkspaceState";
import {
  Button,
  Banner,
  toast,
  StatusBadge,
  PageHeader,
  PageBody,
  useConfirm,
  ActionResultRegion,
  TimedLoadingState,
  Disclosure,
  DisclosureChevron,
  FieldActionRow,
  InfoTip,
  TextareaField,
  SelectField,
  FormStatus,
  ProcessingIndicator,
  useActionPending,
} from "@production-ready/ui";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ListPlus,
  BookOpenText,
  Database,
  Play,
  RefreshCw,
  RotateCcw,
  Sparkles,
  UserCog,
  Wand2,
  X,
} from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { PageNotice } from "@/components/page-notice";
import { EmptyState } from "@/components/StateViews";
import { useAuth } from "@/features/security/AuthProvider";
import {
  CAPABILITY_PERMISSIONS,
  MENU_PERMISSIONS,
} from "@/features/security/menu-permissions";
import { ApiError, apiGet, apiPost, isAbortError, isTransportError } from "@/lib/api";
import { useValuesChanged } from "@/lib/render-sync";
import { t } from "@/lib/i18n";
import { listLoadMoreErrorMessage } from "@/lib/load-more-error";
import { formatDateTime } from "@/lib/format";
import { API_TIMEOUT_MS } from "@/lib/requestPolicy";
import { DbObjectPanelHeader } from "./components/DbObjectManagementShared";
import { EngineSelector } from "./components/EngineSelector";
import { useProfileEngine } from "./useProfileEngine";
import { GuidedClarificationPanel } from "./components/GuidedClarificationPanel";
import { Nl2SqlExecutionOptionsPanel } from "./components/Nl2SqlExecutionOptionsPanel";
import { QueryResultCard } from "./components/QueryResultTable";
import { OperationStatusStrip } from "./components/OperationStatusStrip";
import { QuestionText } from "./components/QuestionText";
import { SchemaReferencePanel } from "./components/SchemaReferencePanel";
import { SchemaRefreshHeaderStatus, SchemaRefreshProcessing } from "./components/SchemaRefreshFeedback";
import { useSchemaRefreshCoordinator } from "./SchemaRefreshCoordinator";
import {
  getSchemaObjectDetail,
  ProfileUnavailableError,
  useProfileUsageContext,
  useProfileSummaries,
  useSchemaCatalogHead,
  useSchemaObjects,
} from "./incrementalQueries";
import { SelectAiFeedbackAddPanel } from "./components/SelectAiFeedbackAddPanel";
import { isJobInFlight } from "./jobPersistence";
import { submitNl2SqlJob } from "./jobSubmission";
import { SelectAiCredentialPrecondition } from "./components/SelectAiCredentialPrecondition";
import { engineUsesSelectAiCredential } from "./selectAiCredentialReadiness";
import { useSelectAiCredentialReadiness } from "./useSelectAiCredentialReadiness";
import { prefillFromSearchParams, type QueryPrefill } from "./queryPrefillState";
import { QUESTION_TEMPLATES } from "./questionTemplates";
import { profileDisplayLabel } from "./profileDisplay";
import type {
  HistoryData,
  HistoryItem,
  JobData,
  Nl2SqlEngine,
  Nl2SqlResult,
  ProfileRecommendationData,
  RewriteData,
  SampleDataMutationData,
  SchemaCatalog,
  SchemaObjectDetail,
  SchemaTable,
  SimilarHistoryData,
  SimilarHistoryItem,
} from "./types";
import { formatDbObjectName } from "./dbObjectIdentity";
import { useNl2SqlJobPolling } from "./useNl2SqlJobPolling";
import { schemaReferenceState } from "./schemaReferenceState";
import {
  emptySelection,
  insertTextAtRange,
  isSchemaEmptyError,
  leadingNewlinePrefix,
  toAllowedObjects,
  type SchemaSelection,
} from "./workbenchState";

// history_id が一致するものだけを結果の履歴とみなす。generated_sql の一致で代用すると、
// 履歴永続化に失敗した回のフィードバックが同じ SQL の別の履歴へ付いてしまう。
function lastMatchingHistory(history: HistoryItem[], result: Nl2SqlResult | null) {
  if (!result?.history_id) return null;
  return history.find((item) => item.id === result.history_id) ?? null;
}

function profileRecommendationSignature(question: string, profileId: string) {
  return `${question}\u0000${profileId}`;
}

function goodFeedbackSimilarHistory(items: SimilarHistoryItem[]): SimilarHistoryItem[] {
  return items;
}

/** 「SQL を生成して実行」の aria-describedby が指す、Select AI の Credential の事前条件（#1688）。 */
const QUERY_CREDENTIAL_PRECONDITION_ID = "nl2sql-credential-precondition";

function engineUsesSimilarHistoryFewShot(engine: Nl2SqlEngine): boolean {
  return engine !== "select_ai" && engine !== "select_ai_agent";
}

type PageErrorSource = "profile-load" | "schema-load" | "schema-refresh" | "sample-import";
type PageError = { source: PageErrorSource; message: string; code?: string } | null;

const PROFILE_RECOMMENDATION_APPLY_THRESHOLD = 0.3;
// URL で受け取るクエリ画面の初期値（一度だけ適用して URL から消す）。
const QUERY_PREFILL_KEYS = ["question", "engine", "profile_id"];
/** 「スキーマを更新」の起点（SchemaRefreshCoordinator の start(origin)。押したボタンだけを回す。#821）。 */
/** DB 構造の再取得の失敗の案内の「DB 構造を再取得」。進行はその案内の中に出す（#2034）。 */
const WORKBENCH_SCHEMA_REFRESH_NOTICE = "workbench-notice";
/** サンプルデータの取込を押した案内（job の失敗の案内 / 操作の結果の案内）。 */
type SampleImportOrigin = "job" | "action";
const WORKBENCH_SCHEMA_REFRESH_HEADER = "workbench-header";
const WORKBENCH_SCHEMA_REFRESH_PANEL = "workbench-schema-panel";

function messageFromError(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

/** 業務プロファイルが削除済み・権限なしで使えない（利用の情報の 404 / 403）。 */
function isProfileUnavailableError(error: unknown) {
  return (
    error instanceof ProfileUnavailableError ||
    (error instanceof ApiError && (error.status === 404 || error.status === 403))
  );
}

/** ApiError の機械判定用 code(problem.code)。PageError の分類に使う。 */
function codeFromError(error: unknown): string | undefined {
  return error instanceof ApiError ? error.errorCode : undefined;
}

function messageWithRetryHint(message: string) {
  const retryHint = t("nl2sql.error.retryHint");
  return message.includes(retryHint) ? message : `${message} ${retryHint}`;
}

function actionErrorMessage(error: unknown, fallback: string) {
  // 通信の失敗の文は「何が起きたか + 次にできること」を持つので、再試行の案内を重ねない（#916）。
  if (isTransportError(error)) return error.message;
  return messageWithRetryHint(messageFromError(error, fallback));
}

function sampleDataMutationFailureMessage(result: SampleDataMutationData) {
  return (
    result.warnings.find((message) => message.trim()) ||
    result.statements.find((statement) => statement.error_message.trim())?.error_message ||
    t("nl2sql.sample.importFailed")
  );
}

export function Nl2SqlWorkbench() {
  const { hasPermission } = useAuth();
  const canExecute = hasPermission(MENU_PERMISSIONS.query);
  if (!canExecute) {
    return (
      <>
        <PageHeader wide title={t("nav.query")} subtitle={t("page.query.subtitle")} />
        <PageBody wide>
          <Banner severity="info">{t("nl2sql.permission.executeRequired")}</Banner>
        </PageBody>
      </>
    );
  }
  return <ExecutableNl2SqlWorkbench />;
}

/** 未確定のプロファイル（`question:`）の質問を、新しいプロファイルの草稿が空のときだけ引き継ぐ（#168・#3357）。 */
function carryUnresolvedProfileQuestion({ from, value, stored }: { from: string; value: string; stored: string }) {
  return from === "question:" && Boolean(value.trim()) && !stored.trim();
}

function ExecutableNl2SqlWorkbench() {
  const confirmDiscard = useConfirm();
  const writeDraft = useWorkspaceDraftWriter();
  const { hasPermission } = useAuth();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const workspaceActive = useWorkspaceActive();
  const [history, setHistory] = useState<HistoryItem[]>([]);
  useWorkspaceRevalidation();
  const [profileId, setProfileId] = useWorkspaceState("profileId", "");
  // プロファイル未確定（""）の間に入力した質問は、確定した時点で新しいプロファイルの下書きへ引き継ぐ（#168）。
  // 質問はプロファイルごとに保存するため、引き継がないと key が変わって入力が消える。確定と同じ render で起きた
  // 入力も失わないよう、key が変わる render の中で引き継ぐ（#3357）。
  const [question, setQuestion] = useWorkspaceState<string>(`question:${profileId}`, "", {
    carryOver: carryUnresolvedProfileQuestion,
  });
  const [selection, setSelection] = useState<SchemaSelection>(() => emptySelection());
  const [result, setResult] = useState<Nl2SqlResult | null>(null);
  // 結果の SQL を実行して結果を整形したか（ジョブが done で終わったか）。遮断・実行の失敗のジョブも
  // 生成 SQL を確かめられるよう空の results を持つ result を返すので、結果の表はこれで出し分ける（#917）。
  const [resultExecuted, setResultExecuted] = useState(false);
  const [recommendation, setRecommendation] = useState<ProfileRecommendationData | null>(null);
  const [similarHistory, setSimilarHistory] = useState<SimilarHistoryItem[]>([]);
  const [similarHistoryLoading, setSimilarHistoryLoading] = useState(false);
  const [similarHistorySearchCompleted, setSimilarHistorySearchCompleted] = useState(false);
  const [similarHistoryPanelVisible, setSimilarHistoryPanelVisible] = useState(false);
  const [similarHistoryUsedForGeneration, setSimilarHistoryUsedForGeneration] = useState(true);
  const [rewriteData, setRewriteData] = useState<RewriteData | null>(null);
  const [rewriteUseGlossary, setRewriteUseGlossary] = useWorkspaceState("rewriteUseGlossary", false);
  const [useOntologyContext, setUseOntologyContext] = useWorkspaceState("useOntologyContext", true);
  const [includeInterpretation, setIncludeInterpretation] = useWorkspaceState("includeInterpretation", true);
  // Show Prompt は Select AI への追加 round-trip を伴うため既定 OFF(必要な人だけ ON にする)。
  const [includeShowPrompt, setIncludeShowPrompt] = useWorkspaceState("includeShowPrompt", false);
  const [executionOptionsOpen, setExecutionOptionsOpen] = useWorkspaceState("executionOptionsOpen", false);
  const [selectAiAdvancedOpen, setSelectAiAdvancedOpen] = useWorkspaceState("selectAiAdvancedOpen", false);
  const [selectAiRoleAdvancedOpen, setSelectAiRoleAdvancedOpen] = useWorkspaceState("selectAiRoleAdvancedOpen", false);
  const [similarHistoryOpen, setSimilarHistoryOpen] = useState(false);
  const [selectAiRoleOverride, setSelectAiRoleOverride] = useWorkspaceState(`selectAiRoleOverride:${profileId}`, "");
  const [selectAiInstructionsOverride, setSelectAiInstructionsOverride] = useWorkspaceState(`selectAiInstructionsOverride:${profileId}`, "");
  const [schemaSearch, setSchemaSearch] = useWorkspaceState("schemaSearch", "");
  const [schemaDetails, setSchemaDetails] = useState<Record<string, SchemaObjectDetail>>({});
  const [submitting, setSubmitting] = useState(false);
  const [pageError, setPageError] = useState<PageError>(null);
  const [actionError, setActionError] = useState("");
  const [schemaDetailError, setSchemaDetailError] = useState("");
  const [autoDetectLowConfidence, setAutoDetectLowConfidence] = useState(false);
  const [actionOperationKey, setActionOperationKey] = useState(0);
  // サンプルデータの取込を押した場所（job の失敗の案内 / 操作の結果の案内）。進行と失敗はその案内の中だけに出し、
  // Toast とページの先頭に重ねない（messaging §10.1・§10.2、#2034）。成功は Toast だけ。
  const [sampleImport, setSampleImport] = useState<{
    origin: SampleImportOrigin;
    importing: boolean;
    error: string;
  } | null>(null);
  const importingSample = Boolean(sampleImport?.importing);
  const [detecting, setDetecting] = useState(false);
  const [guidedClarificationOpen, setGuidedClarificationOpen] = useState(false);
  // 回答前の確認で反映した要件（#1439）。質問の文（利用者の入力）と業務プロファイルが確かめた時のままなら、
  // job の作成で session の ID を送り、サーバーが構造化した要件と確かめた質問の文を SQL の生成に渡す。
  // 確かめた質問の文（clarifiedQuestion）は要件の表示にだけ出し、質問の欄は書き換えない（#1727）。
  const [appliedRequirement, setAppliedRequirement] = useState<
    { sessionId: string; question: string; clarifiedQuestion: string; profileId: string } | null
  >(null);
  // 「この内容で SQL を生成して実行」で確定し、まだ実行していない要件の session の ID（#1761）。
  const [pendingRequirementRun, setPendingRequirementRun] = useState("");
  const questionTextareaRef = useRef<HTMLTextAreaElement>(null);
  const schemaDetailRequests = useRef(new Set<string>());
  const suppressedRecommendationSignaturesRef = useRef(new Set<string>());

  useLayoutEffect(() => {
    const textarea = questionTextareaRef.current;
    if (!textarea) return;
    const minHeight = 144;
    const maxHeight = 266;
    textarea.style.height = "0px";
    const contentHeight = textarea.scrollHeight;
    textarea.style.height = `${Math.min(maxHeight, Math.max(minHeight, contentHeight))}px`;
    textarea.style.overflowY = contentHeight > maxHeight ? "auto" : "hidden";
  }, [question]);

  const profilesQuery = useProfileSummaries("");
  const profiles = useMemo(
    () => profilesQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [profilesQuery.data]
  );
  const profileLoadMoreError =
    profilesQuery.isFetchNextPageError && profilesQuery.error
      ? listLoadMoreErrorMessage(profilesQuery.error, "profiles.error.load")
      : "";
  const noProfiles = profilesQuery.isSuccess && profiles.length === 0;
  const [pendingPrefill, setPendingPrefill] = useState<QueryPrefill | null>(null);
  const selectedProfileQuery = useProfileUsageContext(profileId);
  const selectedProfile = selectedProfileQuery.data?.profile ?? null;
  // 生成の方式は業務プロファイルごとに残し、選んでいなければ業務プロファイルの既定の生成方式にする（#1524）。
  const [engine, setEngine] = useProfileEngine(profileId, selectedProfile?.default_engine);
  // Select AI / Select AI Agent の生成は Select AI の Credential が前提（#1688）。無いと分かったら
  // 「SQL を生成して実行」の直前に事前条件を示し、実行を止める（OCI Enterprise AI の生成は止めない）。
  const selectAiCredential = useSelectAiCredentialReadiness();
  const selectAiCredentialBlocked =
    engineUsesSelectAiCredential(engine) && selectAiCredential.missing;
  const profileOptions = useMemo(() => {
    if (!selectedProfile || profiles.some((profile) => profile.id === selectedProfile.id)) {
      return profiles;
    }
    return [
      ...profiles,
      {
        id: selectedProfile.id,
        name: selectedProfile.name,
        category: selectedProfile.category,
        description: selectedProfile.description,
        archived: selectedProfile.archived,
        allowed_table_count: selectedProfile.allowed_tables.length,
        allowed_view_count: selectedProfile.allowed_views.length,
        glossary_count: 0,
        few_shot_count: 0,
        version: selectedProfile.version,
        etag: selectedProfile.etag,
        updated_at: selectedProfile.updated_at,
      },
    ];
  }, [profiles, selectedProfile]);
  const canManageProfiles = hasPermission(CAPABILITY_PERMISSIONS.profilesManage);
  const canRefreshSchema = hasPermission(CAPABILITY_PERMISSIONS.schemaRefresh);
  const canImportSampleData = hasPermission(CAPABILITY_PERMISSIONS.sampleDataManage);
  const profileSelectionReady =
    !noProfiles &&
    Boolean(profileId) &&
    (profiles.some((profile) => profile.id === profileId) || selectedProfileQuery.isSuccess);
  const schemaObjectsQuery = useSchemaObjects(
    schemaSearch,
    "",
    profileId,
    "",
    profileSelectionReady
  );
  // スキーマ参照の件数（#1891）。総数は先頭のページの total（検索語と業務プロファイルで API が絞った件数）、
  // 絞り込む前の全件は検索語の無い問い合わせの total。検索語が無いときは同じ queryKey なのでキャッシュを共有し、
  // 追加の要求は出ない（テーブル管理と同じ形）。
  const allSchemaObjectsQuery = useSchemaObjects("", "", profileId, "", profileSelectionReady);
  const schemaTotalCount = schemaObjectsQuery.data?.pages[0]?.total ?? null;
  const schemaUnfilteredTotalCount = allSchemaObjectsQuery.data?.pages[0]?.total ?? null;
  const schemaLoadMoreError =
    schemaObjectsQuery.isFetchNextPageError && schemaObjectsQuery.error
      ? listLoadMoreErrorMessage(schemaObjectsQuery.error, "dataMgmt.objectList.error")
      : "";
  const schemaHeadQuery = useSchemaCatalogHead();
  const schemaRefresh = useSchemaRefreshCoordinator();
  const schemaObjects = useMemo(
    () => schemaObjectsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [schemaObjectsQuery.data]
  );
  const catalog = useMemo<SchemaCatalog>(() => {
    return {
      refreshed_at: schemaHeadQuery.data?.refreshed_at ?? "",
      schema_fingerprint: schemaHeadQuery.data?.schema_fingerprint ?? "",
      tables: schemaObjects.map((object) => {
        // 引用名の表を大文字の同名表と同じキーにしない（#561）。
        const key = formatDbObjectName({ owner: object.owner, name: object.object_name });
        return (
          schemaDetails[key]?.table ?? {
            table_name: object.object_name,
            qualified_name: formatDbObjectName({ owner: object.owner, name: object.object_name }),
            logical_name: object.logical_name,
            owner: object.owner,
            table_type: object.object_type,
            comment: object.comment,
            row_count: object.row_count,
            columns: [],
            constraints: [],
          }
        );
      }),
    };
  }, [schemaDetails, schemaHeadQuery.data, schemaObjects]);
  // 無効の query（業務プロファイルが未選択・使えない）の pending を読み込み中にしない（#3413）。
  const schemaPanelState = schemaReferenceState({
    profileId,
    profilesPending: profilesQuery.isPending,
    prefillPending: pendingPrefill !== null,
    profileReady: profileSelectionReady,
    profileFailed: selectedProfileQuery.isError,
    schemaPending: schemaObjectsQuery.isPending && !schemaObjectsQuery.data,
  });
  const loadingCatalog = schemaPanelState === "loading";
  // 押した「スキーマを更新」だけを送信の間 loading にする。job の間はどちらも無効にするだけで、スピナーは
  // スキーマ参照の進行の表示が出す（durable job の規則。messaging §3.7、#821）。
  const headerSchemaRefreshStarting = schemaRefresh.startingOrigin === WORKBENCH_SCHEMA_REFRESH_HEADER;
  const panelSchemaRefreshStarting = schemaRefresh.startingOrigin === WORKBENCH_SCHEMA_REFRESH_PANEL;
  const schemaCatalogHasObjects =
    catalog.tables.length > 0 || (schemaHeadQuery.data?.object_count ?? 0) > 0;
  const currentScopedSchemaEmpty =
    schemaObjectsQuery.isSuccess &&
    catalog.tables.length === 0 &&
    (schemaHeadQuery.data?.object_count ?? 0) === 0;
  const visiblePageError =
    pageError && !(isSchemaEmptyError(pageError) && schemaCatalogHasObjects)
      ? pageError
      : null;

  // 画面 entry は summary/object page だけを独立取得する。refresh は persistent job を投入し、
  // 前 catalog を表示したまま job 完了時にこの画面で必要な read model だけを取り直す。
  const loadCatalog = useCallback(
    async (refresh = false, announce = false, refreshOrigin = "") => {
      try {
        if (refresh) {
          await schemaRefresh.start(refreshOrigin);
          return;
        }
        // スキーマの一覧は使える業務プロファイルがあるときだけ取り直す。無効の query の refetch は enabled を見ずに
        // 実行するため、未選択の間に業務プロファイルで絞らない一覧を取りに行かない（#3413）。
        const schemaTasks = [
          ...(profileSelectionReady ? [schemaObjectsQuery.refetch()] : []),
          schemaHeadQuery.refetch(),
        ];
        const results = await Promise.allSettled([
          ...schemaTasks,
          profilesQuery.refetch(),
          ...(profileId ? [selectedProfileQuery.refetch()] : []),
        ]);
        const failedResult = results.find(
          (result) => result.status === "rejected" || result.value.isError
        );
        if (failedResult) {
          const profileFailed = results.indexOf(failedResult) >= schemaTasks.length;
          const fallback = profileFailed ? t("profiles.error.load") : t("nl2sql.error.loadFailed");
          const cause =
            failedResult.status === "rejected" ? failedResult.reason : failedResult.value.error;
          setPageError({
            source: profileFailed ? "profile-load" : "schema-load",
            message: messageFromError(cause, fallback),
            code: codeFromError(cause),
          });
          return;
        }
        setPageError(null);
        if (
          announce &&
          results.every(
            (result) => result.status === "fulfilled" && !result.value.isError
          )
        ) {
          toast.success(t("common.action.refreshed"));
        }
      } catch (err) {
        setPageError({
          source: refresh ? "schema-refresh" : "schema-load",
          message: messageFromError(err, t("nl2sql.error.loadFailed")),
          code: codeFromError(err),
        });
      }
    },
    [
      profileId,
      profileSelectionReady,
      profilesQuery,
      selectedProfileQuery,
      schemaHeadQuery,
      schemaObjectsQuery,
      schemaRefresh,
    ]
  );

  // 業務プロファイルつきの初期値（実行履歴の「SQL 生成で開く」。#3355）は、業務プロファイルの一覧で今も使えるかを
  // 確かめてから選ぶ（確かめている途中の pendingPrefill は、スキーマ参照の状態に使うので上で宣言する）。
  // 削除済み・権限の無い業務プロファイルは利用の情報（usage-context）を取りに行かず、質問だけを
  // 入れて業務プロファイルを空にし、選び直しを案内する（404 のエラーにしない）。
  const [prefillProfileUnavailable, setPrefillProfileUnavailable] = useWorkspaceState(
    "prefillProfileUnavailable",
    false
  );
  // 一覧に無いが後のページにあるかもしれない業務プロファイル。利用の情報の 404 / 403 を「使えない」として扱う。
  const [prefillProfileToVerify, setPrefillProfileToVerify] = useState("");
  /** 初期値を入れる（別の業務プロファイルの質問・方式は、その業務プロファイルの草稿へ先に書く）。草稿の失敗で true。 */
  const applyPrefill = (prefill: QueryPrefill): boolean => {
    const otherProfile = Boolean(prefill.profileId && prefill.profileId !== profileId);
    if (prefill.question) {
      if (otherProfile) {
        if (!writeDraft(`question:${prefill.profileId}`, prefill.question)) {
          setActionError(t("workspace.storageFailed"));
          return true;
        }
      } else setQuestion(prefill.question);
    }
    // 生成の方式は業務プロファイルごとに残すので、別の業務プロファイルの方式はその草稿へ書く（#1524）。
    if (prefill.engine) {
      if (!otherProfile) setEngine(prefill.engine);
      else if (!writeDraft(`engine:${prefill.profileId}`, prefill.engine)) {
        setActionError(t("workspace.storageFailed"));
      }
    }
    if (prefill.profileId) setProfileId(prefill.profileId);
    return false;
  };
  /** 受け取った業務プロファイルが使えないとき: 質問を未確定の草稿（`question:`）に置き、業務プロファイルを空にする。 */
  const applyUnavailablePrefillProfile = (unresolvedQuestion: string) => {
    // 選び直した業務プロファイルへ、未確定の草稿の質問を引き継ぐ（carryUnresolvedProfileQuestion。#3357）。
    if (!profileId) {
      if (unresolvedQuestion) setQuestion(unresolvedQuestion);
    } else if (unresolvedQuestion && !writeDraft("question:", unresolvedQuestion)) {
      setActionError(t("workspace.storageFailed"));
    }
    setProfileId("");
    setSelection(emptySelection());
    setRecommendation(null);
    setPageError(null);
    setPrefillProfileUnavailable(true);
  };
  // 読込エラーは query の状態から render 中に pageError へ反映する。
  if (
    useValuesChanged([
      profileId,
      profilesQuery.data,
      profilesQuery.error,
      profilesQuery.isError,
      schemaHeadQuery.data,
      schemaHeadQuery.error,
      schemaHeadQuery.isError,
      schemaObjectsQuery.data,
      schemaObjectsQuery.error,
      schemaObjectsQuery.isError,
      selectedProfileQuery.error,
      selectedProfileQuery.isError,
    ])
  ) {
    if (profilesQuery.isError && !profilesQuery.data) {
      setPageError({
        source: "profile-load",
        message: messageFromError(profilesQuery.error, t("profiles.error.load")),
        code: codeFromError(profilesQuery.error),
      });
    } else if (
      selectedProfileQuery.isError &&
      profileId &&
      profileId === prefillProfileToVerify &&
      isProfileUnavailableError(selectedProfileQuery.error)
    ) {
      // 一覧で確かめられなかった受け取った業務プロファイルが使えない（#3355）。エラーにせず選び直しを案内する。
      setPrefillProfileToVerify("");
      applyUnavailablePrefillProfile(question);
    } else if (selectedProfileQuery.isError && profileId) {
      setPageError({
        source: "profile-load",
        message: messageFromError(selectedProfileQuery.error, t("profiles.error.load")),
        code: codeFromError(selectedProfileQuery.error),
      });
    } else if (schemaObjectsQuery.isError && !schemaObjectsQuery.data) {
      setPageError({
        source: "schema-load",
        message: messageFromError(schemaObjectsQuery.error, t("nl2sql.error.loadFailed")),
        code: codeFromError(schemaObjectsQuery.error),
      });
    } else if (schemaHeadQuery.isError && !schemaHeadQuery.data) {
      setPageError({
        source: "schema-load",
        message: messageFromError(schemaHeadQuery.error, t("nl2sql.error.loadFailed")),
        code: codeFromError(schemaHeadQuery.error),
      });
    }
  }

  // 読込が回復したら、その読込エラーを render 中に消す。
  if (
    useValuesChanged([
      pageError,
      noProfiles,
      profileId,
      profilesQuery.isSuccess,
      schemaCatalogHasObjects,
      selectedProfileQuery.isSuccess,
    ]) &&
    pageError
  ) {
    if (
      pageError.source === "profile-load" &&
      profilesQuery.isSuccess &&
      (noProfiles || !profileId || selectedProfileQuery.isSuccess)
    ) {
      setPageError(null);
    } else if (pageError.source === "schema-load" && schemaCatalogHasObjects) {
      setPageError(null);
    }
  }

  if (useValuesChanged([schemaRefresh.error]) && schemaRefresh.error) {
    setPageError({ source: "schema-refresh", message: schemaRefresh.error });
  }
  // DB 構造の再取得が完了したら、再取得の失敗の案内を消す（解消した案内を残さない。messaging §10.2、#2034）。
  if (
    useValuesChanged([schemaRefresh.completedJob]) &&
    schemaRefresh.completedJob?.status === "done" &&
    pageError?.source === "schema-refresh"
  ) {
    setPageError(null);
  }
  // 失敗の案内の「再試行」「DB 構造を再取得」。押したボタンだけを回し、進行は同じ案内の中に出す（#2034）。
  const noticeRetry = useActionPending();
  const noticeSchemaRefreshStarting = schemaRefresh.startingOrigin === WORKBENCH_SCHEMA_REFRESH_NOTICE;
  const noticeOwnsSchemaRefresh =
    schemaRefresh.isRefreshing &&
    (noticeSchemaRefreshStarting || schemaRefresh.jobOrigin === WORKBENCH_SCHEMA_REFRESH_NOTICE);

  const refreshHistory = useCallback(async () => {
    const historyData = await apiGet<HistoryData>("/api/nl2sql/history");
    setHistory(historyData.items);
  }, []);

  const handleJobResult = useCallback((data: Nl2SqlResult, finishedJob: JobData) => {
    setResult(data);
    setResultExecuted(finishedJob.status === "done");
  }, []);

  // job 自体のエラーは OperationStatusStrip（job.error_message）へ一本化して表示する。
  // ページ共通 Banner（catalog/execute 用）には流さず、重複表示を避ける。
  const handleJobFailed = useCallback(() => {}, []);

  // ポーリング通信の断念/job 消失は error_message を持たないため、
  // 実行ボタン直下の ActionResultRegion を正本として表示する（Toast とは併用しない）。
  const handlePollingLost = useCallback((message: string) => {
    setActionError(messageWithRetryHint(message));
    setActionOperationKey((current) => current + 1);
  }, []);

  // 結果自体は成功しているため、履歴更新の失敗は warning に留める。
  const handleHistoryRefreshFailed = useCallback(() => {
    toast.warning(t("nl2sql.history.refreshFailed"));
  }, []);

  const { job, jobStartedAt, jobStorageUnavailable, legacyJobSnapshotIgnored, trackJob, clearTrackedJob } = useNl2SqlJobPolling({
    onResult: handleJobResult,
    onJobFailed: handleJobFailed,
    onPollingLost: handlePollingLost,
    onHistoryRefresh: refreshHistory,
    onHistoryRefreshFailed: handleHistoryRefreshFailed,
  });
  // 実行中 job の協調キャンセル要求。停止自体は polling が terminal 遷移で拾う。
  // backend は実行中の段階が終わってから止めるので、受け付けたジョブを覚えて、止まるまで受付を出し
  // 「実行を中止」を押せなくする。要求の失敗は中止のボタンの行に出す（messaging §10.1。#918）。
  const [cancelRequesting, setCancelRequesting] = useState(false);
  const [cancelAcceptedJobId, setCancelAcceptedJobId] = useState("");
  const [cancelFailure, setCancelFailure] = useState<{ jobId: string; message: string } | null>(null);
  const requestJobCancel = useCallback(async () => {
    if (!job || !isJobInFlight(job.status)) return;
    const jobId = job.job_id;
    setCancelRequesting(true);
    setCancelFailure(null);
    try {
      await apiPost(`/api/nl2sql/jobs/${encodeURIComponent(jobId)}/cancel`, {}, {
        timeoutMs: API_TIMEOUT_MS.jobControl,
      });
      setCancelAcceptedJobId(jobId);
    } catch (err) {
      setCancelFailure({ jobId, message: actionErrorMessage(err, t("nl2sql.job.cancelFailed")) });
    } finally {
      setCancelRequesting(false);
    }
  }, [job]);

  const clearGeneratedOutput = useCallback(() => {
    setResult(null);
    clearTrackedJob();
    setRewriteData(null);
    setActionError("");
  }, [clearTrackedJob]);
  const previousProfileId = useRef(profileId);
  useEffect(() => {
    const previous = previousProfileId.current;
    previousProfileId.current = profileId;
    if (previous === profileId) return;
    setSelection(emptySelection());
    // 初回の自動選択では再読込から復元した進行中 job を消さない。
    if (previous) clearGeneratedOutput();
  }, [clearGeneratedOutput, profileId]);
  const jobActive = isJobInFlight(job?.status) || submitting;
  const showJobStorageWarning = jobStorageUnavailable && jobActive;
  useTransientDraftGuard(showJobStorageWarning);
  const active = jobActive || detecting || guidedClarificationOpen;
  const actionBusy = submitting;
  const showSimilarHistoryPanel =
    engineUsesSimilarHistoryFewShot(engine) &&
    (similarHistoryPanelVisible ||
      similarHistoryLoading ||
      similarHistorySearchCompleted ||
      similarHistory.length > 0);
  const beginActionFeedback = useCallback(() => {
    setActionError("");
    setActionOperationKey((current) => current + 1);
  }, []);
  const showActionError = useCallback((err: unknown, fallback: string) => {
    setActionError(actionErrorMessage(err, fallback));
    setActionOperationKey((current) => current + 1);
  }, []);
  const latestHistory = useMemo(() => lastMatchingHistory(history, result), [history, result]);
  const selectAiOverrides = useMemo(() => {
    if (engine !== "select_ai") return null;
    const role = selectAiRoleOverride.trim();
    const additionalInstructions = selectAiInstructionsOverride.trim();
    if (!role && !additionalInstructions) return null;
    return {
      role,
      additional_instructions: additionalInstructions,
    };
  }, [engine, selectAiInstructionsOverride, selectAiRoleOverride]);
  // 送信 payload と結果の比較条件を同じ値から作り、実行条件の追加漏れを防ぐ。
  const requirementActive = Boolean(
    appliedRequirement
      && appliedRequirement.profileId === profileId
      && appliedRequirement.question === question.trim()
  );
  const generationRequest = {
    question: question.trim(),
    engine,
    profile_id: profileId || null,
    ...(requirementActive && appliedRequirement
      ? { requirement_session_id: appliedRequirement.sessionId }
      : {}),
    allowed_objects: toAllowedObjects(selection),
    select_ai_overrides: selectAiOverrides,
    use_glossary: rewriteUseGlossary,
    use_ontology_context: useOntologyContext,
    include_interpretation: includeInterpretation,
    include_show_prompt: includeShowPrompt,
  };
  const selectAiRoleHasOverride = Boolean(selectAiRoleOverride.trim());
  const hasSelectAiOverrideInputs = useMemo(
    () => Boolean(selectAiRoleHasOverride || selectAiInstructionsOverride.trim()),
    [selectAiInstructionsOverride, selectAiRoleHasOverride]
  );
  const selectAiRolePanelOpen = selectAiRoleAdvancedOpen;
  const profileAllowedTableNames = useMemo(() => {
    if (!selectedProfile) return null;
    const names = [...selectedProfile.allowed_tables, ...selectedProfile.allowed_views];
    return names.length > 0 ? names : null;
  }, [selectedProfile]);

  // URL の初期値（question / engine / profile_id）は render 中に state へ反映し、URL からの削除は effect で行う。
  // 別 Profile の質問は、その Profile の草稿（sessionStorage）へ先に書いてから Profile を切り替える。
  const [prefillDraftFailed, setPrefillDraftFailed] = useState(false);
  if (useValuesChanged([searchParams, workspaceActive])) {
    let draftFailed = false;
    if (workspaceActive && QUERY_PREFILL_KEYS.some((key) => searchParams.has(key))) {
      const prefill = prefillFromSearchParams(searchParams);
      if (prefill.profileId) setPendingPrefill(prefill);
      else draftFailed = applyPrefill(prefill);
    }
    if (draftFailed !== prefillDraftFailed) setPrefillDraftFailed(draftFailed);
  }
  // 業務プロファイルの一覧が揃ったら、受け取った業務プロファイルを選ぶか、使えないと案内する。一覧が後のページへ
  // 続く・一覧を読めなかったときは選んで、利用の情報の結果で決める。
  if (pendingPrefill && !profilesQuery.isPending) {
    const listed = profiles.some((profile) => profile.id === pendingPrefill.profileId);
    const unknown = !listed && (profilesQuery.hasNextPage || profilesQuery.isError);
    setPendingPrefill(null);
    if (listed || unknown) {
      setPrefillProfileUnavailable(false);
      setPrefillProfileToVerify(unknown ? pendingPrefill.profileId : "");
      applyPrefill(pendingPrefill);
    } else {
      applyUnavailablePrefillProfile(pendingPrefill.question);
    }
  }
  // 業務プロファイルを選んだら、使えない業務プロファイルの案内を消す。
  if (prefillProfileUnavailable && profileId) setPrefillProfileUnavailable(false);
  useEffect(() => {
    if (prefillDraftFailed) return;
    if (!workspaceActive || !QUERY_PREFILL_KEYS.some((key) => searchParams.has(key))) return;
    // 初期値は一度だけ適用し、以降の再読込ではユーザーの草稿を復元する。
    const remainingParams = new URLSearchParams(searchParams);
    QUERY_PREFILL_KEYS.forEach((key) => remainingParams.delete(key));
    setSearchParams(remainingParams, { replace: true });
  }, [prefillDraftFailed, searchParams, setSearchParams, workspaceActive]);

  // Profile 一覧・選択の変化に合わせて、選択を render 中に直す。
  if (
    useValuesChanged([
      noProfiles,
      profileId,
      profiles,
      profilesQuery.isPending,
      selectedProfileQuery.isPending,
      selectedProfileQuery.isSuccess,
    ]) &&
    !profilesQuery.isPending
  ) {
    if (noProfiles) {
      setSelection(emptySelection());
      setRecommendation(null);
    } else if (!profileId && !prefillProfileUnavailable && !pendingPrefill) {
      // 受け取った業務プロファイルが使えないときは、先頭へ黙って切り替えず空のままにする（#3355）。
      const nextProfileId = profiles[0]?.id ?? "";
      if (nextProfileId) setProfileId(nextProfileId);
    } else if (
      !profiles.some((profile) => profile.id === profileId) &&
      !selectedProfileQuery.isPending &&
      !selectedProfileQuery.isSuccess
    ) {
      // 選択済み Profile の失効はエラーとして表示し、別 Profile へ黙って切り替えない。
      setSelection(emptySelection());
      setRecommendation(null);
    }
  }

  // 実行中かどうかは ref で参照し、effect の再実行トリガーにしない。
  // active を依存に入れると job 完了(active: true → false)のたびに同じ質問で推薦 API が再実行される。
  const activeRef = useRef(active);
  // 最新の active を commit 時に入れる（render 中に ref を書かない）。
  useLayoutEffect(() => { activeRef.current = active; });
  // 質問・Profile が変わったら、自動判定の注意と（短い質問では）推薦を render 中に消す。
  if (useValuesChanged([profileId, profiles.length, question])) {
    setAutoDetectLowConfidence(false);
    if (question.trim().length < 4 || profiles.length === 0) setRecommendation(null);
  }
  useEffect(() => {
    const trimmed = question.trim();
    if (trimmed.length < 4 || profiles.length === 0) {
      suppressedRecommendationSignaturesRef.current.clear();
      return undefined;
    }
    const signature = profileRecommendationSignature(trimmed, profileId);
    if (suppressedRecommendationSignaturesRef.current.delete(signature)) {
      return undefined;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (activeRef.current) return;
      if (suppressedRecommendationSignaturesRef.current.delete(signature)) return;
      void apiPost<ProfileRecommendationData>("/api/nl2sql/recommend-profile", {
        question: trimmed,
        current_profile_id: profileId || null,
      }, { signal: controller.signal })
        .then((data) => {
          if (!controller.signal.aborted) setRecommendation(data);
        })
        .catch((cause: unknown) => {
          if (!controller.signal.aborted && !isAbortError(cause)) {
            setRecommendation(null);
          }
        });
    }, 500);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [profileId, profiles.length, question]);

  // 類似履歴を検索しない条件・実行中は、表示を render 中に直す（検索は下の effect で行う）。
  const similarHistoryDisabled =
    !engineUsesSimilarHistoryFewShot(engine) || question.trim().length < 4 || profiles.length === 0;
  if (useValuesChanged([active, engine, profileId, profiles.length, question])) {
    if (similarHistoryDisabled) {
      setSimilarHistory([]);
      setSimilarHistorySearchCompleted(false);
      setSimilarHistoryPanelVisible(false);
      setSimilarHistoryLoading(false);
      setSimilarHistoryOpen(false);
      setSimilarHistoryUsedForGeneration(true);
    } else if (active) {
      // 実行開始時は in-flight を abort するため .finally が走らない。
      // 履歴パネル自体は残し、検索中ラベルだけを解除する。
      setSimilarHistoryLoading(false);
    }
  }
  useEffect(() => {
    const trimmed = question.trim();
    if (!engineUsesSimilarHistoryFewShot(engine) || trimmed.length < 4 || profiles.length === 0) {
      return undefined;
    }
    if (active) return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setSimilarHistoryPanelVisible(true);
      setSimilarHistoryLoading(true);
      void apiPost<SimilarHistoryData>("/api/nl2sql/similar-history", {
        question: trimmed,
        profile_id: profileId || null,
        engine,
      }, { signal: controller.signal })
        .then((data) => {
          if (!controller.signal.aborted) {
            const usedForGeneration =
              data.used_for_generation ?? engineUsesSimilarHistoryFewShot(engine);
            const goodHistory = usedForGeneration ? goodFeedbackSimilarHistory(data.items) : [];
            setSimilarHistory(goodHistory);
            setSimilarHistoryUsedForGeneration(usedForGeneration);
            setSimilarHistoryOpen(goodHistory.length > 0);
            setSimilarHistorySearchCompleted(true);
            setSimilarHistoryPanelVisible(true);
          }
        })
        .catch((cause: unknown) => {
          if (!controller.signal.aborted && !isAbortError(cause)) {
            setSimilarHistory([]);
            setSimilarHistorySearchCompleted(false);
            setSimilarHistoryPanelVisible(false);
            setSimilarHistoryOpen(false);
            setSimilarHistoryUsedForGeneration(true);
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setSimilarHistoryLoading(false);
        });
    }, 650);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [active, engine, profileId, profiles.length, question]);

  const insertSchemaText = (text: string) => {
    setRewriteData(null);
    const el = questionTextareaRef.current;
    if (!el) {
      // ref 未取得（フォーカス外）のときは末尾へ追記。各項目を改行区切りにする。
      setQuestion((current) => `${current}${leadingNewlinePrefix(current, current.length)}${text}`);
      return;
    }
    const source = question;
    const start = el.selectionStart ?? source.length;
    const end = el.selectionEnd ?? source.length;
    // 各項目を改行区切りにする（先頭・直前が改行のときは付けない）。
    const prefixed = `${leadingNewlinePrefix(source, start)}${text}`;
    const nextValue = insertTextAtRange(source, prefixed, start, end);
    setQuestion(nextValue);
    requestAnimationFrame(() => {
      // preventScroll: 挿入クリックのたびにページが textarea まで飛ばないようにする。
      el.focus({ preventScroll: true });
      el.setSelectionRange(start + prefixed.length, start + prefixed.length);
      // textarea 内部だけ、いま挿入した行へ追従スクロールする。
      // ponytail: 折返し行は無視した近似。挿入項目は 1 行想定で実用十分。
      const caretLine = nextValue.slice(0, start + prefixed.length).split("\n").length;
      const lineHeight = 24; // leading-6
      el.scrollTop = Math.max(0, caretLine * lineHeight - el.clientHeight + lineHeight);
    });
  };

  const loadSchemaDetail = useCallback(async (table: SchemaTable, signal?: AbortSignal) => {
    const key = formatDbObjectName({ owner: table.owner, name: table.table_name });
    if (schemaDetails[key] || schemaDetailRequests.current.has(key)) return;
    schemaDetailRequests.current.add(key);
    try {
      // 応答・失敗の反映は then / catch の callback で行う（effect から呼んでも同期の setState にしない）。
      await getSchemaObjectDetail(table.owner, table.table_name, signal)
        .then((detail) => {
          if (signal?.aborted) return;
          setSchemaDetails((current) => ({ ...current, [key]: detail }));
          setSchemaDetailError("");
        })
        .catch((err: unknown) => {
          if (!signal?.aborted && !isAbortError(err)) {
            setSchemaDetailError(messageFromError(err, t("nl2sql.error.loadFailed")));
          }
        });
    } finally {
      schemaDetailRequests.current.delete(key);
    }
  }, [schemaDetails]);

  useEffect(() => {
    if (!schemaSearch.trim()) return;
    const controller = new AbortController();
    for (const table of catalog.tables) {
      if (table.columns.length === 0) void loadSchemaDetail(table, controller.signal);
    }
    return () => controller.abort();
  }, [catalog.tables, loadSchemaDetail, schemaSearch]);

  const applyRecommendation = () => {
    if (!recommendation) return;
    setAutoDetectLowConfidence(false);
    const trimmed = question.trim();
    if (trimmed) {
      suppressedRecommendationSignaturesRef.current.add(
        profileRecommendationSignature(trimmed, recommendation.recommended_profile_id)
      );
    }
    writeDraft(`question:${recommendation.recommended_profile_id}`, question);
    setProfileId(recommendation.recommended_profile_id);
    setSelection(emptySelection());
    setActionError("");
    setSchemaDetailError("");
  };

  const detectionContext = useRef("");
  // 最新の判定条件を commit 時に入れる（render 中に ref を書かない）。
  useLayoutEffect(() => { detectionContext.current = profileRecommendationSignature(question, profileId); });

  // 質問から業務プロファイルを自動判定（学習済み分類器 → 決定論フォールバック）して選択する。
  const detectProfile = useCallback(async () => {
    const trimmed = question.trim();
    if (!trimmed || active || !profileSelectionReady) return;
    suppressedRecommendationSignaturesRef.current.add(
      profileRecommendationSignature(trimmed, profileId)
    );
    setDetecting(true);
    const submittedContext = detectionContext.current;
    try {
      const data = await apiPost<ProfileRecommendationData>("/api/nl2sql/recommend-profile", {
        question: trimmed,
        current_profile_id: profileId || null,
      });
      if (detectionContext.current !== submittedContext) return;
      setRecommendation(data);
      const recommendedProfileLabel = profileDisplayLabel({
        name: data.recommended_profile_name,
        category: data.recommended_profile_category,
      });
      const applyThreshold =
        data.confidence_threshold ?? PROFILE_RECOMMENDATION_APPLY_THRESHOLD;
      if (data.confidence < applyThreshold) {
        // 継続的な状況提示なので固定面の warning Banner に一本化する
        // (同文の Toast と二重表示しない: messaging spec §0.6)。
        setAutoDetectLowConfidence(true);
        return;
      }
      setAutoDetectLowConfidence(false);
      if (data.recommended_profile_id === profileId) {
        const sourceLabel =
          data.recommendation_source === "classifier"
            ? t("nl2sql.recommend.sourceClassifier")
            : t("nl2sql.recommend.sourceDeterministic");
        toast.info(
          t("nl2sql.recommend.autoDetectAlreadySelected", {
            name: recommendedProfileLabel,
            source: sourceLabel,
            confidence: Math.round(data.confidence * 100),
          })
        );
        return;
      }
      suppressedRecommendationSignaturesRef.current.add(
        profileRecommendationSignature(trimmed, data.recommended_profile_id)
      );
      writeDraft(`question:${data.recommended_profile_id}`, question);
      setProfileId(data.recommended_profile_id);
      setSelection(emptySelection());
      setActionError("");
      setSchemaDetailError("");
      const sourceLabel =
        data.recommendation_source === "classifier"
          ? t("nl2sql.recommend.sourceClassifier")
          : t("nl2sql.recommend.sourceDeterministic");
      toast.success(
        t("nl2sql.recommend.autoDetectApplied", {
          name: recommendedProfileLabel,
          source: sourceLabel,
          confidence: Math.round(data.confidence * 100),
        })
      );
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("nl2sql.recommend.autoDetectFailed"));
    } finally {
      setDetecting(false);
    }
  }, [active, profileId, profileSelectionReady, question, setProfileId, writeDraft]);

  // schema catalog が空（サンプル未投入）のとき、エラーからワンクリックで投入して解消する。
  const importSampleData = useCallback(async (origin: SampleImportOrigin) => {
    setSampleImport({ origin, importing: true, error: "" });
    try {
      const result = await apiPost<SampleDataMutationData>("/api/nl2sql/sample-data/import", {
        step: "all",
        confirmation: "ADMIN_EXECUTE",
      });
      if (!result.executed) {
        setSampleImport({ origin, importing: false, error: sampleDataMutationFailureMessage(result) });
        return;
      }
      setSampleImport(null);
      toast.success(t("nl2sql.sample.importSuccess"));
      setPageError(null);
      setSchemaDetailError("");
      clearTrackedJob();
      await loadCatalog();
    } catch (err) {
      setSampleImport({
        origin,
        importing: false,
        error: err instanceof Error ? err.message : t("nl2sql.sample.importFailed"),
      });
    }
  }, [clearTrackedJob, loadCatalog]);
  const sampleImportProgress = (origin: SampleImportOrigin) =>
    sampleImport?.origin === origin && sampleImport.importing ? (
      <ProcessingIndicator
        active
        label={t("nl2sql.sample.importing")}
        operationKey={`sample-import-${origin}`}
        placement="action"
        testId={`nl2sql-sample-import-processing-${origin}`}
        activityIcon="none"
      />
    ) : null;
  const sampleImportError = (origin: SampleImportOrigin) =>
    sampleImport?.origin === origin && sampleImport.error ? sampleImport.error : "";

  const applyRewrittenQuestion = async () => {
    if (!rewriteData) return;
    setQuestion(rewriteData.rewritten_question);
    setRewriteData(null);
  };

  // 用語・同義語の置換が起きていない（= 無変換）ときはカードを出さない。
  // 変更前後が同一なのに「生成に使用される質問」を並べると、書き換えられた誤解を招く。
  // 「抑止した」等の内部処理 warning だけではカードを開かない（無変換の報告は余計な情報）。
  const rewriteChanged =
    !!rewriteData && rewriteData.rewritten_question.trim() !== question.trim();

  const submit = async (options: { closingClarification?: boolean } = {}) => {
    const trimmed = question.trim();
    // 回答前の確認のパネルから「確認せずに SQL を生成」したときは、閉じる途中のパネルを開いている扱いにしない。
    const busy = options.closingClarification ? jobActive || detecting : active;
    if (!trimmed || busy || !profileSelectionReady || selectAiCredentialBlocked) return;
    beginActionFeedback();
    clearGeneratedOutput();
    setSubmitting(true);
    const startedAt = Date.now();
    try {
      // チェックが ON のときだけ書き換えプレビューを表示する。生成側の適用は job runner が 1 回だけ行う。
      if (rewriteUseGlossary) {
        const rewrite = await apiPost<RewriteData>("/api/nl2sql/rewrite", {
          question: trimmed,
          profile_id: profileId || null,
          use_glossary: rewriteUseGlossary,
        }, { timeoutMs: API_TIMEOUT_MS.interactiveDetail });
        setRewriteData(rewrite);
      }
      // 送信前に job ID を決め、応答が届かなくても作られたジョブを取り直す（二重に投入しない。#916）。
      const data = await submitNl2SqlJob(generationRequest);
      // 追跡開始後の取得・リトライ・断念は useNl2SqlJobPolling が担う。
      // ここで初回 poll を await すると、その失敗が「SQL の生成と実行の開始の失敗」と誤表示され
      // 成功した job の追跡まで破棄されるため、try 節は job 作成までとする。
      trackJob(data, startedAt);
    } catch (err) {
      showActionError(err, t("nl2sql.error.submitFailed"));
      selectAiCredential.refreshAfterError(err);
      clearTrackedJob();
    } finally {
      setSubmitting(false);
    }
  };

  // 回答前の確認の「確認せずに SQL を生成」（チャットと同じ操作。#1715）。パネルを閉じ、入力した質問のまま生成する。
  const skipGuidedClarification = () => {
    setGuidedClarificationOpen(false);
    void submit({ closingClarification: true });
  };
  // 「回答前の確認」を押せない理由（未入力）。操作前の案内なので赤いエラーにしない。質問が SQL の文でも押せる
  // （利用者の明示の操作なので確かめる。SQL の文で確認を飛ばすのはチャットの送信だけ。#1727）。
  const guidedClarificationHintId = !question.trim() ? "nl2sql-guided-query-required" : undefined;

  // 回答前の確認の「この内容で SQL を生成して実行」（チャットと同じ操作。#1761）。確定した要件で SQL を生成して実行
  // する（反映の後に「SQL を生成して実行」を押し直させない）。利用者が入力した質問（質問の欄の値）は書き換えない
  // （#1727）。確かめた質問の文は要件の表示に出し、生成にだけ使う。業務プロファイルが変わるときは、切り替えた
  // 業務プロファイルの選択の準備ができてから実行する（下の effect）。
  const handleApplyRequirement = useCallback((
    sourceQuestion: string,
    clarifiedQuestion: string,
    confirmedProfileId: string,
    sessionId: string,
  ): boolean => {
    if (confirmedProfileId !== profileId) {
      // 業務プロファイルの草稿へ、利用者の質問をそのまま先に書く（切替時の復元が質問の欄を別の文にしない）。
      if (!writeDraft(`question:${confirmedProfileId}`, sourceQuestion)) {
        setActionError(t("workspace.storageFailed"));
        return false;
      }
      setProfileId(confirmedProfileId);
    }
    setAppliedRequirement({
      sessionId,
      question: sourceQuestion.trim(),
      clarifiedQuestion: clarifiedQuestion.trim(),
      profileId: confirmedProfileId,
    });
    setRewriteData(null);
    setActionError("");
    setGuidedClarificationOpen(false);
    setPendingRequirementRun(sessionId);
    return true;
  }, [profileId, setProfileId, writeDraft]);
  // 確定した要件での実行は、その要件が今の質問・業務プロファイルに当たり、パネルが閉じて実行できる状態になって
  // から、確定ごとに 1 回だけ行う（最新の submit を commit 時に ref へ入れて呼ぶ）。
  const submitRef = useRef(submit);
  useLayoutEffect(() => {
    submitRef.current = submit;
  });
  const pendingRequirementReady =
    Boolean(pendingRequirementRun)
    && requirementActive
    && appliedRequirement?.sessionId === pendingRequirementRun
    && !guidedClarificationOpen
    && !jobActive
    && !detecting
    && profileSelectionReady;
  // 同じ確定（session）で 2 回実行しない（実行の後に条件がまた揃っても、押し直すまで実行しない）。
  const requirementRunDoneRef = useRef("");
  useEffect(() => {
    if (!pendingRequirementReady || requirementRunDoneRef.current === pendingRequirementRun) return;
    requirementRunDoneRef.current = pendingRequirementRun;
    void submitRef.current();
  }, [pendingRequirementReady, pendingRequirementRun]);

  return (
    <>
      <PageHeader wide
        title={t("nav.query")}
        subtitle={t("page.query.subtitle")}
        status={<SchemaRefreshHeaderStatus testId="query-schema-refresh-status" />}
        meta={
          catalog.refreshed_at
            ? t("common.schemaRefreshedAt", {
                date: formatDateTime(catalog.refreshed_at),
              })
            : undefined
        }
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            onClick: () => loadCatalog(false, true),
            // 参照スキーマの初回の読込は、スキーマ参照の読込表示がスピナーを出す（このボタンは狭い画面では
            // 「その他の操作」の中で見えない。同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
            disabled: active || loadingCatalog,
          },
          ...(canRefreshSchema
            ? [
                {
                  id: "schema-refresh",
                  kind: "utility" as const,
                  label: t("common.action.schemaRefresh"),
                  icon: RefreshCw,
                  onClick: () => loadCatalog(true, false, WORKBENCH_SCHEMA_REFRESH_HEADER),
                  // 押したときの送信の間だけ回す。job の間・スキーマ参照のボタンで始めた更新では無効にするだけ。
                  // 送信中の押したボタンはネイティブの disabled にせず、フォーカスを保つ（#355 / #821）。
                  loading: headerSchemaRefreshStarting,
                  disabled: active || (schemaRefresh.isRefreshing && !headerSchemaRefreshStarting),
                },
              ]
            : []),
        ]}
      />

      <PageBody wide className="grid gap-4">
        <PageNotice
          notice={
            visiblePageError
              ? { tone: "danger", message: messageWithRetryHint(visiblePageError.message) }
              : null
          }
          action={
            visiblePageError?.source === "schema-refresh" ? (
              // 失敗したのは DB 構造の再取得なので、一覧の取り直しではなく再取得をやり直す（#2034）。
              <Button
                type="button"
                variant="secondary"
                size="sm"
                loading={noticeSchemaRefreshStarting}
                disabled={schemaRefresh.isRefreshing && !noticeSchemaRefreshStarting}
                onClick={() =>
                  void schemaRefresh
                    .start(WORKBENCH_SCHEMA_REFRESH_NOTICE, { announce: false })
                    .catch(() => undefined)
                }
                icon={RefreshCw}
                data-testid="query-page-notice-schema-refresh"
              >
                <span>{t("common.action.schemaRefresh")}</span>
              </Button>
            ) : visiblePageError ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                loading={noticeRetry.pending}
                onClick={() => {
                  if (!noticeRetry.pending) void noticeRetry.track(() => loadCatalog());
                }}
                icon={RefreshCw}
                data-testid="query-page-notice-retry"
              >
                <span>{t("common.retry")}</span>
              </Button>
            ) : undefined
          }
          progress={
            visiblePageError?.source === "schema-refresh" && noticeOwnsSchemaRefresh ? (
              <SchemaRefreshProcessing
                placement="job"
                className=""
                testId="query-page-notice-schema-refresh-processing"
              />
            ) : null
          }
        />

        {noProfiles ? (
          <Banner
            severity="info"
            title={t("nl2sql.profile.empty.title")}
            action={
              canManageProfiles ? (
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => navigate("/profiles?profile=new")} icon={UserCog}>
                  <span>{t("nl2sql.profile.empty.action")}</span>
                </Button>
              ) : undefined
            }
          >
            {canManageProfiles
              ? t("nl2sql.profile.empty.description")
              : t("nl2sql.profile.empty.readOnlyDescription")}
          </Banner>
        ) : null}

        <section
          className="grid gap-4 rounded-md border border-border bg-surface p-4 shadow-sm"
          aria-label={t("nl2sql.workspace.label")}
          data-testid="nl2sql-workspace-shell"
        >
          <section className="grid min-w-0 grid-cols-[minmax(0,1fr)] content-start gap-4" aria-labelledby="nl2sql-query-heading">
                <DbObjectPanelHeader
                  headingId="nl2sql-query-heading"
                  icon={Sparkles}
                  title={t("nl2sql.workbench.title")}
                  description={t("nl2sql.workbench.description")}
                />
                <div className="space-y-5">
                  <EngineSelector
                    value={engine}
                    onChange={(nextEngine) => {
                      setEngine(nextEngine);
                      setActionError("");
                    }}
                    disabled={active}
                  />

              <div className="grid gap-4">
                <div className="grid gap-1">
                  {/* 業務プロファイルは空にできない選択欄（読み込み後に先頭を選ぶ）で、API（MCP を含む）で省略したときも
                      backend が既定の業務プロファイルを使うので、「必須」は付けない（UX 契約 messaging.md §3.2.1。#540）。 */}
                  {/* 業務プロファイルの欄は lg の幅のまま置き、操作は欄の直後に並べる（fit。#1891）。 */}
                  <FieldActionRow
                    fit
                    actions={
                      <>
                        <Button
                          type="button"
                          variant="secondary"
                          size="md"
                          loading={detecting}
                          disabled={!question.trim() || active || !profileSelectionReady}
                          onClick={() => void detectProfile()} icon={Wand2}>
                          <span>{t("nl2sql.recommend.autoDetect")}</span>
                        </Button>
                        {profilesQuery.hasNextPage && (
                          <Button icon={ListPlus}
                            type="button"
                            variant="secondary"
                            size="md"
                            // 読み込みに失敗した後は、案内の「再試行」だけを回す（同じ処理のスピナーは 1 つ。#416）。
                            loading={profilesQuery.isFetchingNextPage && !profileLoadMoreError}
                            disabled={active || (profilesQuery.isFetchingNextPage && Boolean(profileLoadMoreError))}
                            onClick={() => void profilesQuery.fetchNextPage()}
                          >
                            {t("profiles.action.loadMore")}
                          </Button>
                        )}
                      </>
                    }
                  >
                    {/* 隣の「自動判定」（md）と同じ高さ（#613）。読み込み中は今の値を「読み込み中」の選択肢として出す。 */}
                    <SelectField
                      id="nl2sql-profile-select"
                      label={t("nl2sql.profile.label")}
                      value={profileId}
                      // 受け取った業務プロファイルが使えず空のとき（#3355）に出す。
                      placeholder={t("nl2sql.profile.selectPlaceholder")}
                      options={
                        profilesQuery.isPending
                          ? [{ value: profileId, label: t("profiles.summary.loading") }]
                          : profileOptions.map((profile) => ({ value: profile.id, label: profileDisplayLabel(profile) }))
                      }
                      onValueChange={(nextProfileId) => {
                        setProfileId(nextProfileId);
                        suppressedRecommendationSignaturesRef.current.clear();
                        setSelection(emptySelection());
                        setAutoDetectLowConfidence(false);
                        setActionError("");
                        setPageError(null);
                        setSchemaDetailError("");
                      }}
                      disabled={active || profilesQuery.isPending || noProfiles}
                      // 対象の選択（業務プロファイル）は lg（#1891）。
                      width="lg"
                    />
                  </FieldActionRow>
                  {prefillProfileUnavailable && !profileId && !noProfiles ? (
                    // 実行履歴から開いた実行の業務プロファイルが削除済み・権限なしで使えない（#3355）。選択欄の直下に出す。
                    <div data-testid="nl2sql-prefill-profile-unavailable">
                      <Banner severity="warning">{t("nl2sql.prefill.profileUnavailable")}</Banner>
                    </div>
                  ) : null}
                  {profileLoadMoreError && (
                    <Banner
                      severity="danger"
                      action={<Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        className="w-full sm:w-auto"
                        loading={profilesQuery.isFetchingNextPage}
                        disabled={active}
                        onClick={() => void profilesQuery.fetchNextPage()} icon={RefreshCw}>
                        <span>{t("common.retry")}</span>
                      </Button>}
                    >
                      {profileLoadMoreError}
                    </Banner>
                  )}
                </div>
                {recommendation &&
                  recommendation.confidence >=
                    (recommendation.confidence_threshold ??
                      PROFILE_RECOMMENDATION_APPLY_THRESHOLD) &&
                  recommendation.recommended_profile_id !== profileId && (
                    <div
                      data-surface-tint="accent"
                      className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-accent-emphasis bg-accent-subtle px-3 py-2 text-sm"
                      data-testid="nl2sql-recommend-hint"
                    >
                      {/* 折り返しても文字の 1 行目の中心にそろえる（#2371）。 */}
                      <span className="flex min-w-0 items-start gap-2 text-fg">
                        <span className="pr-icon-line text-accent-fg">
                          <Sparkles size={16} aria-hidden="true" />
                        </span>
                        <span className="min-w-0 [overflow-wrap:anywhere]">
                          {t("nl2sql.recommend.switchHint", {
                            name: profileDisplayLabel({
                              name: recommendation.recommended_profile_name,
                              category: recommendation.recommended_profile_category,
                            }),
                            confidence: Math.round(recommendation.confidence * 100),
                          })}
                        </span>
                      </span>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={active}
                        onClick={applyRecommendation}
                      >
                        {t("nl2sql.recommend.switchApply")}
                      </Button>
                    </div>
                  )}
                {autoDetectLowConfidence &&
                  recommendation &&
                  recommendation.confidence <
                    (recommendation.confidence_threshold ??
                      PROFILE_RECOMMENDATION_APPLY_THRESHOLD) && (
                    <div data-testid="nl2sql-recommend-low-confidence">
                      <Banner severity="warning">
                        {t("nl2sql.recommend.autoDetectLowConfidence", {
                          name: profileDisplayLabel({
                            name: recommendation.recommended_profile_name,
                            category: recommendation.recommended_profile_category,
                          }),
                          confidence: Math.round(recommendation.confidence * 100),
                        })}
                      </Banner>
                    </div>
                  )}
              </div>

                  {/* クエリ（左）× スキーマ参照（右・常時表示）: 書きながら参照して即クリック挿入。 */}
                  <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)] lg:items-start">
                    {/* 質問の欄は主な問い合わせの入力で、右のスキーマ参照と並ぶ列の幅いっぱいに置く（チャットの入力欄と同じく
                        値の種類の段の外。#1891 の幅の検査から外す）。 */}
                    <div className="grid gap-2" data-field-width-exempt="">
                      {/* クエリの入力を補助するテンプレート行（選択時は全文置換）。 */}
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-xs font-medium text-fg-muted">{t("dbAdmin.runner.templates")}</span>
                        {QUESTION_TEMPLATES.map((template) => (
                          <Button
                            key={template.labelKey}
                            type="button"
                            variant="ghost"
                            size="sm"
                            disabled={active}
                            onClick={() => {
                              setQuestion(template.body);
                              setRewriteData(null);
                              setActionError("");
                              const el = questionTextareaRef.current;
                              if (el) {
                                requestAnimationFrame(() => {
                                  el.focus({ preventScroll: true });
                                  // 1 行目「対象テーブル：」の直後にカーソルを置き、すぐ記入できるようにする
                                  const firstLineEnd = template.body.indexOf("\n");
                                  const caret = firstLineEnd === -1 ? template.body.length : firstLineEnd;
                                  el.setSelectionRange(caret, caret);
                                });
                              }
                            }}
                          >
                            {t(template.labelKey)}
                          </Button>
                        ))}
                      </div>
                      <div className="grid gap-2">
                        <TextareaField
                          id="nl2sql-question-input"
                          label={t("nl2sql.question.label")}
                          required
                          ref={questionTextareaRef}
                          value={question}
                          onChange={(event) => {
                            setQuestion(event.currentTarget.value);
                            setRewriteData(null);
                            setActionError("");
                          }}
                          disabled={active}
                          // 質問の入力欄は 3 製品で 3.5 行（#2008）。
                          rows={3.5}
                          width="full"
                          resize="none"
                          textareaClassName="max-h-[16.625rem]"
                          placeholder={t("nl2sql.question.placeholder")}
                        />
                        {guidedClarificationOpen ? (
                          <GuidedClarificationPanel
                            question={question.trim()}
                            profileId={profileId}
                            engine={engine}
                            allowedObjects={toAllowedObjects(selection)}
                            onApplyRequirement={handleApplyRequirement}
                            onSkip={skipGuidedClarification}
                          />
                        ) : (
                          <div className="flex flex-wrap items-center gap-2">
                            <Button
                              type="button"
                              variant="secondary"
                              size="md"

                              disabled={!question.trim() || active || !profileSelectionReady}
                              aria-describedby={guidedClarificationHintId}
                              onClick={() => {
                                setActionError("");
                                setGuidedClarificationOpen(true);
                              }} icon={Sparkles}>
                              <span>{t("nl2sql.clarification.start")}</span>
                            </Button>
                            {/* 補足の説明は常設せず、ボタンの横の info アイコンから出す（#901）。 */}
                            <InfoTip
                              label={t("nl2sql.clarification.infoLabel")}
                              content={t("nl2sql.clarification.description")}
                              contentTestId="nl2sql-clarification-description"
                              data-testid="nl2sql-clarification-info"
                            />
                          </div>
                        )}
                        {appliedRequirement && !guidedClarificationOpen ? (
                          requirementActive ? (
                            <div className="grid gap-1" data-testid="nl2sql-requirement-active">
                              <div className="flex flex-wrap items-center gap-2">
                                <StatusBadge
                                  variant="info"
                                  label={t("nl2sql.clarification.requirementActive")}
                                />
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  icon={X}
                                  disabled={active}
                                  onClick={() => setAppliedRequirement(null)}
                                >
                                  {t("nl2sql.clarification.requirementRemove")}
                                </Button>
                              </div>
                              {/* 確かめた質問の文は質問の欄に入れず、ここに出す（チャットの回答と同じ畳んだ詳細。#1727）。 */}
                              {appliedRequirement.clarifiedQuestion ? (
                                <Disclosure
                                  variant="plain"
                                  size="sm"
                                  summary={t("chat.requirement.applied")}
                                  summaryProps={{ "data-testid": "nl2sql-requirement-question" }}
                                >
                                  <p className="text-xs leading-5 text-fg-muted">
                                    {t("nl2sql.clarification.clarifiedQuestion")}
                                  </p>
                                  <p className="break-words text-sm leading-6 text-fg">
                                    {appliedRequirement.clarifiedQuestion}
                                  </p>
                                </Disclosure>
                              ) : null}
                            </div>
                          ) : (
                            <p
                              className="text-xs leading-5 text-fg-muted"
                              data-testid="nl2sql-requirement-dropped"
                            >
                              {t("nl2sql.clarification.requirementDropped")}
                            </p>
                          )
                        ) : null}
                        {/* 未入力はエラーではなく「ボタンが使えない理由」の案内。操作前に赤字や alert の読み上げで出さない。 */}
                        {!question.trim() && !guidedClarificationOpen ? (
                          <p id="nl2sql-guided-query-required" className="text-xs leading-5 text-fg-muted">
                            {t("nl2sql.clarification.queryRequired")}
                          </p>
                        ) : null}
                        {engine === "select_ai" && (
                          <section className="overflow-hidden rounded-md border border-dashed border-border bg-surface-sunken">
                            <Button className="w-full justify-between"
                              type="button"
                              variant="ghost"
                              size="md"
                              aria-expanded={selectAiAdvancedOpen}
                              aria-controls="select-ai-request-overrides"
                              onClick={() => setSelectAiAdvancedOpen((current) => !current)}
                              disabled={active}
                            >
                              <span className="flex min-w-0 items-center gap-2">
                                <Sparkles size={16} className="shrink-0 text-fg" aria-hidden="true" />
                                <span className="min-w-0 [overflow-wrap:anywhere]">
                                  {t("nl2sql.selectAiOverrides.title")}
                                </span>
                                {hasSelectAiOverrideInputs && (
                                  <span data-surface-tint="accent" className="shrink-0 rounded-full bg-accent-subtle px-2 py-0.5 text-xs font-medium text-accent-fg">
                                    {t("nl2sql.selectAiOverrides.activeBadge")}
                                  </span>
                                )}
                              </span>
                              <DisclosureChevron
                                expanded={selectAiAdvancedOpen}
                                size={16}
                              />
                            </Button>
                            {/* aria-controls の参照先を常時レンダし、閉時は hidden にする
                                (Nl2SqlExecutionOptionsPanel と同方式)。 */}
                            <div
                              id="select-ai-request-overrides"
                              hidden={!selectAiAdvancedOpen}
                              className="grid gap-3 border-t border-border p-3"
                            >
                                <p className="text-xs leading-5 text-fg-muted">
                                  {t("nl2sql.selectAiOverrides.hint")}
                                </p>
                                <TextareaField
                                  id="nl2sql-select-ai-instructions-override"
                                  label={t("nl2sql.selectAiOverrides.additionalInstructions")}
                                  value={selectAiInstructionsOverride}
                                  onChange={(event) => setSelectAiInstructionsOverride(event.currentTarget.value)}
                                  disabled={active}
                                  rows={3}
                                  width="full"
                                  placeholder={t("nl2sql.selectAiOverrides.additionalInstructionsPlaceholder")}
                                />
                                <div className="overflow-hidden rounded-md border border-border bg-surface">
                                  <Button className="w-full justify-between"
                                    type="button"
                                    variant="ghost"
                                    size="sm"
                                    aria-expanded={selectAiRolePanelOpen}
                                    aria-controls="select-ai-role-override"
                                    onClick={() => setSelectAiRoleAdvancedOpen((current) => !current)}
                                    disabled={active}
                                  >
                                    <span className="flex min-w-0 items-center gap-2">
                                      <UserCog size={14} className="shrink-0 text-fg-muted" aria-hidden="true" />
                                      <span>{t("nl2sql.selectAiOverrides.roleToggle")}</span>
                                      {selectAiRoleHasOverride && (
                                        <span data-surface-tint="neutral" className="rounded-full bg-surface-hover px-2 py-0.5 text-xs font-medium text-fg-muted">
                                          {t("nl2sql.selectAiOverrides.activeBadge")}
                                        </span>
                                      )}
                                    </span>
                                    <DisclosureChevron
                                      expanded={selectAiRolePanelOpen}
                                      size={16}
                                    />
                                  </Button>
                                  <div
                                    id="select-ai-role-override"
                                    hidden={!selectAiRolePanelOpen}
                                    className="border-t border-border p-3"
                                  >
                                    <TextareaField
                                      id="nl2sql-select-ai-role-override-input"
                                      label={t("nl2sql.selectAiOverrides.role")}
                                      value={selectAiRoleOverride}
                                      onChange={(event) => setSelectAiRoleOverride(event.currentTarget.value)}
                                      disabled={active}
                                      rows={2}
                                      width="full"
                                      placeholder={t("nl2sql.selectAiOverrides.rolePlaceholder")}
                                    />
                                  </div>
                                </div>
                              </div>
                          </section>
                        )}
                      </div>
                    </div>
                    <div className="rounded-md border border-border bg-surface-sunken p-3">
                      <SchemaReferencePanel
                        catalog={catalog}
                        loading={loadingCatalog}
                        profileUnselected={schemaPanelState === "profile-unselected"}
                        disabled={active}
                        insertMode="logical"
                        allowedTableNames={profileAllowedTableNames}
                        // API に profile_id を渡すので、件数（total）は業務プロファイルの対象で絞った数（#1891）。
                        scopeAppliedByServer
                        totalCount={schemaTotalCount}
                        unfilteredTotalCount={schemaUnfilteredTotalCount}
                        listMaxHeightClass="max-h-[30rem]"
                        onRefreshSchema={
                          canRefreshSchema
                            ? () => void loadCatalog(true, false, WORKBENCH_SCHEMA_REFRESH_PANEL)
                            : undefined
                        }
                        refreshing={schemaRefresh.isRefreshing}
                        refreshStarting={panelSchemaRefreshStarting}
                        showRefreshProgress={!noticeOwnsSchemaRefresh}
                        searchQuery={schemaSearch}
                        onSearchQueryChange={(value) => {
                          setSchemaSearch(value);
                          setSchemaDetailError("");
                        }}
                        hasMore={Boolean(schemaObjectsQuery.hasNextPage)}
                        loadingMore={schemaObjectsQuery.isFetchingNextPage}
                        loadMoreError={schemaLoadMoreError}
                        onLoadMore={() => void schemaObjectsQuery.fetchNextPage()}
                        onRetryLoadMore={() => void schemaObjectsQuery.fetchNextPage()}
                        onExpandTable={(table) => void loadSchemaDetail(table)}
                        detailLoadError={schemaDetailError}
                        onDismissDetailLoadError={() => setSchemaDetailError("")}
                        onInsert={insertSchemaText}
                      />
                    </div>
                  </div>

                  <Nl2SqlExecutionOptionsPanel
                    disabled={active}
                    engine={engine}
                    includeInterpretation={includeInterpretation}
                    includeShowPrompt={includeShowPrompt}
                    open={executionOptionsOpen}
                    useOntologyContext={useOntologyContext}
                    onIncludeInterpretationChange={setIncludeInterpretation}
                    onIncludeShowPromptChange={setIncludeShowPrompt}
                    onOpenChange={setExecutionOptionsOpen}
                    onRewriteUseGlossaryChange={setRewriteUseGlossary}
                    onUseOntologyContextChange={setUseOntologyContext}
                    rewriteUseGlossary={rewriteUseGlossary}
                    selectAiOverridesInactive={engine !== "select_ai" && hasSelectAiOverrideInputs}
                  />
                  {rewriteData && rewriteChanged && (
                    <div
                      className="grid gap-3 rounded-md border border-accent-emphasis bg-surface p-3"
                      data-testid="nl2sql-rewrite-card"
                    >
                      <dl className="grid gap-2 text-sm">
                        <div>
                          <dt className="font-medium text-fg-muted">{t("nl2sql.session.originalQuestion")}</dt>
                          <dd className="mt-1">
                            <QuestionText value={question} variant="detail" maxLines={3} expandable />
                          </dd>
                        </div>
                        <div>
                          <dt className="font-medium text-fg-muted">{t("nl2sql.session.suggestedQuestion")}</dt>
                          <dd className="mt-1">
                            <QuestionText value={rewriteData.rewritten_question} variant="detail" maxLines={3} expandable />
                          </dd>
                        </div>
                      </dl>
                      {/* API 応答に warnings が無くても画面全体を落とさない(防御)。 */}
                      {(rewriteData.warnings ?? []).length > 0 && (
                        <div className="grid gap-2">
                          {(rewriteData.warnings ?? []).map((warning) => (
                            <Banner key={warning} severity="warning">
                              {warning}
                            </Banner>
                          ))}
                        </div>
                      )}
                      <div className="flex flex-wrap items-center gap-2">
                        {/* source（deterministic 等）は内部識別子なので画面には出さない。 */}
                        {rewriteData.model && (
                          <span data-surface-tint="neutral" className="rounded-md bg-surface-hover px-2 py-1 text-xs font-medium text-fg">
                            {rewriteData.model}
                          </span>
                        )}
                        <Button
                          type="button"
                          variant="secondary"
                          size="sm"
                          disabled={active}
                          onClick={() => void applyRewrittenQuestion()}
                        >
                          {t("nl2sql.session.applyPatch")}
                        </Button>
                      </div>
                    </div>
                  )}

                  {showSimilarHistoryPanel && (
                    <section className="w-full min-w-0 max-w-full overflow-hidden rounded-md border border-border bg-surface">
                      <Button className="w-full justify-between"
                        type="button"
                        variant="ghost"
                        size="md"
                        aria-expanded={similarHistoryOpen}
                        aria-controls="nl2sql-similar-history"
                        onClick={() => setSimilarHistoryOpen((current) => !current)}
                        disabled={active}
                      >
                        <span className="flex min-w-0 flex-wrap items-center gap-2">
                          <span className="flex min-w-0 items-center gap-2">
                            <BookOpenText size={16} className="shrink-0 text-fg" aria-hidden="true" />
                            <span className="truncate">{t("nl2sql.similar.title")}</span>
                          </span>
                          {/* 検索中は件数がまだ無いので出さない（0 件と読み違えないように）。 */}
                          {similarHistoryLoading ? null : (
                            <StatusBadge
                              icon={false}
                              variant={similarHistory.length > 0 ? "info" : "neutral"}
                              label={t("nl2sql.similar.count", { count: similarHistory.length })}
                            />
                          )}
                          <StatusBadge icon={false} variant="success" label={t("nl2sql.similar.goodOnly")} />
                          {!similarHistoryUsedForGeneration && (
                            <StatusBadge variant="warning" label={t("nl2sql.similar.notUsedBadge")} />
                          )}
                        </span>
                        <DisclosureChevron
                          expanded={similarHistoryOpen}
                          size={16}
                        />
                      </Button>
                      {/* 検索中は見出しの文言を差し替えず、ほかの読み込み中と同じく見出しの直下に全幅の行で出す（#2510）。 */}
                      {similarHistoryLoading ? (
                        <div className="border-t border-border px-3 py-2">
                          <ProcessingIndicator
                            active
                            label={t("nl2sql.similar.loading")}
                            operationKey="similar-history"
                            placement="panel"
                            testId="nl2sql-similar-history-loading"
                          />
                        </div>
                      ) : null}
                      {/* aria-controls の参照先を常時レンダし、閉時は hidden にする。 */}
                      <div
                        id="nl2sql-similar-history"
                        data-testid="nl2sql-similar-history"
                        hidden={!similarHistoryOpen}
                        className="grid gap-3 border-t border-border p-3 text-sm text-fg"
                      >
                        {!similarHistoryUsedForGeneration && !similarHistoryLoading && (
                          <Banner severity="info">{t("nl2sql.similar.notUsedHint")}</Banner>
                        )}
                        {similarHistory.length === 0 && !similarHistoryLoading ? (
                          <EmptyState
                            title={t("nl2sql.similar.emptyTitle")}
                            hint={t("nl2sql.similar.emptyHint")}
                          />
                        ) : (
                          similarHistory.map((entry) => (
                            <article
                              key={entry.history_id}
                              data-testid="nl2sql-similar-history-item"
                              className="grid w-full min-w-0 max-w-full gap-2 overflow-hidden rounded-md bg-surface-sunken p-3"
                            >
                              <div className="grid w-full min-w-0 max-w-full gap-2 sm:flex sm:flex-wrap sm:items-start sm:justify-between">
                                <div className="min-w-0 sm:flex-1 sm:basis-0">
                                  <QuestionText
                                    value={entry.question}
                                    variant="select"
                                    maxLines={1}
                                    testId="nl2sql-similar-history-question"
                                  />
                                  <p className="mt-1 text-xs text-fg-muted">{entry.reason}</p>
                                </div>
                                <span className="w-fit rounded-md bg-surface px-2 py-1 text-xs font-medium text-fg sm:shrink-0">
                                  {t("nl2sql.similar.score", {
                                    score: Math.round(entry.score * 100),
                                  })}
                                </span>
                              </div>
                              {/* スクロール領域はキーボードでも操作できるよう focus 可能にする
                                  (WCAG 2.1.1)。 */}
                              <pre
                                tabIndex={0}
                                role="region"
                                aria-label={t("nl2sql.similar.title")}
                                className="max-h-28 overflow-auto rounded-md border border-border bg-surface p-2 text-sm leading-6 text-fg"
                              >
                                <code>{entry.sql}</code>
                              </pre>
                            </article>
                          ))
                        )}
                      </div>
                    </section>
                  )}

                  {/* Select AI の Credential の事前条件（#1688）。実行の操作の直前に全幅で出す。 */}
                  {selectAiCredentialBlocked ? (
                    <SelectAiCredentialPrecondition
                      id={QUERY_CREDENTIAL_PRECONDITION_ID}
                      testId="nl2sql-credential-precondition"
                      operation={t("selectAiCredential.precondition.operation.generate")}
          action={t("nl2sql.action.run")}
          credentialName={selectAiCredential.credentialName}
                      canOpenSettings={selectAiCredential.canOpenSettings}
                      rechecking={selectAiCredential.rechecking}
                      onRecheck={() => void selectAiCredential.recheck()}
                    />
                  ) : null}
                  {/* 主アクションバー(button spec §2/§4): size lg、primary → secondary、
                      border-t で末尾に区切る。 */}
                  <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
                    <Button
                      type="button"
                      variant="primary"
                      size="lg"
                      // 送信中だけ loading にする。job の間は disabled にし、スピナーは下の進行状況
                      // （OperationStatusStrip）が 1 つ出す（同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
                      loading={submitting}
                      disabled={!question.trim() || active || !profileSelectionReady || selectAiCredentialBlocked}
                      aria-describedby={
                        selectAiCredentialBlocked ? QUERY_CREDENTIAL_PRECONDITION_ID : undefined
                      }
                      onClick={() => void submit()} icon={Play}>
                      <span>{t("nl2sql.action.run")}</span>
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      size="lg"
                      disabled={active}
                      onClick={async () => {
                        if ((question.trim() || selectAiRoleOverride || selectAiInstructionsOverride) && !await confirmDiscard({ title: t("workspace.discardTitle"), description: t("nl2sql.query.discardDescription"), confirmLabel: t("nl2sql.query.actions.startNew"), tone: "warning" })) return;
                        setSelection(emptySelection());
                        setQuestion("");
                        setResult(null);
                        setRewriteData(null);
                        setRewriteUseGlossary(false);
                        setUseOntologyContext(true);
                        setIncludeInterpretation(true);
                        setIncludeShowPrompt(false);
                        setExecutionOptionsOpen(false);
                        clearTrackedJob();
                        setSelectAiRoleOverride("");
                        setSelectAiInstructionsOverride("");
                        setSelectAiAdvancedOpen(false);
                        setSelectAiRoleAdvancedOpen(false);
                        setPageError(null);
                        setSchemaDetailError("");
                        setActionError("");
                        setGuidedClarificationOpen(false);
                        setAppliedRequirement(null);
                      }} icon={RotateCcw}>
                      <span>{t("nl2sql.query.actions.startNew")}</span>
                    </Button>
                  </div>

                  <ActionResultRegion
                    loading={actionBusy}
                    operationKey={actionOperationKey}
                    errorMessage={actionError}
                    errorAction={
                      actionError && currentScopedSchemaEmpty && canImportSampleData ? (
                        <Button
                          type="button"
                          variant="primary"
                          size="sm"
                          loading={importingSample && sampleImport?.origin === "action"}
                          disabled={active || importingSample}
                          onClick={() => void importSampleData("action")} icon={Database}>
                          <span>{t("nl2sql.sample.import")}</span>
                        </Button>
                      ) : undefined
                    }
                    testId="nl2sql-action-feedback"
                  />
                  {/* 操作の結果の案内から始めた取込の進行と失敗は、その案内の直下に出す（#2034）。 */}
                  {sampleImportProgress("action")}
                  {sampleImportError("action") ? (
                    <div data-testid="nl2sql-sample-import-error">
                      <FormStatus tone="danger" message={sampleImportError("action")} />
                    </div>
                  ) : null}

                </div>
              </section>
        </section>

        {submitting || detecting ? (
          <TimedLoadingState
            label={submitting ? t("nl2sql.action.run") : t("nl2sql.recommend.autoDetect")}
            operationKey={submitting ? "submit" : "profile-detect"}
            placement="result"
            testId="nl2sql-foreground-processing"
            activityIcon="none"
          />
        ) : null}

        {legacyJobSnapshotIgnored && !job && (
          <Banner severity="info">{t("nl2sql.job.legacySnapshotIgnored")}</Banner>
        )}
        {showJobStorageWarning && (
          <Banner severity="warning">{t("nl2sql.job.storageUnavailable")}</Banner>
        )}
        <OperationStatusStrip
          job={job}
          profileId={profileId}
          startedAtMs={jobStartedAt}
          catalogEmpty={catalog !== null && catalog.tables.length === 0}
          importingSample={importingSample && sampleImport?.origin === "job"}
          onImportSample={canImportSampleData ? () => void importSampleData("job") : undefined}
          sampleImportProgress={sampleImportProgress("job")}
          sampleImportError={sampleImportError("job")}
          sampleImportUnavailableHint={
            canImportSampleData ? "" : t("nl2sql.sample.importReadOnlyHint")
          }
          onCancelJob={requestJobCancel}
          cancelRequesting={cancelRequesting}
          cancelAccepted={Boolean(job) && cancelAcceptedJobId === job?.job_id}
          cancelErrorMessage={cancelFailure && cancelFailure.jobId === job?.job_id ? cancelFailure.message : ""}
        />

        <WorkspaceResultNotice result={result} inputSignature={JSON.stringify(generationRequest)} finishedAt={result?.timing?.finished_at} />
        {/* 実行していない SQL（遮断・実行の失敗）の空の results を「0 件」と見せない（#917）。 */}
        <QueryResultCard results={resultExecuted ? result?.results ?? null : null} csvFilePrefix="nl2sql-sql-generation-result" />
        <SelectAiFeedbackAddPanel
          result={result}
          history={latestHistory}
          questionText={question}
          onSaved={refreshHistory}
        />
      </PageBody>
    </>
  );
}
