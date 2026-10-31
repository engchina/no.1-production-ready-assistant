"use client";

import {
  PageBody,
  PageHeader,
  Button,
  Card,
  Banner,
  DataTable,
  type DataTableColumn,
  type EntityAction,
  RowActionMenu,
  SearchableSelectField,
  type SearchableSelectOption,
  FilterSelect,
  isFilterActive,
  type SelectFieldOption,
  TableSkeleton,
  ClearActionButton,
  SearchField,
  TimedLoadingState,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  StatusBadge as UiStatusBadge,
  OffsetPagination,
  ListToolbar,
  useActionPending,
  ApiErrorBanner,
} from "@production-ready/ui";
import { Link, useSearchParams } from "react-router-dom";
import { CheckSquare, History, RefreshCw, RotateCcw, Sparkles, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { LanguagePackBulkReprocess } from "@/components/documents/LanguagePackBulkReprocess";
import { LanguagePackUseSummary } from "@/components/documents/LanguagePackDocumentPanel";
import { DegradedBanner } from "@/components/DegradedBanner";
import { StatusBadge } from "@/components/StatusBadge";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EmptyState, ApiErrorState } from "@/components/StateViews";
import {
  api,
  ApiError,
  BULK_INGESTION_MAX_DOCUMENTS,
  type BulkIngestionReasonCount,
  type BulkIngestionResult,
  type DocumentDeleteImpact,
  type DocumentSummary,
  type FileStatus,
  type IngestionJob,
  type KnowledgeBaseRef,
} from "@/lib/api";
import {
  useDeleteDocument,
  useDocuments,
  initialLoadError,
  useEnqueueDocumentIngestionJob,
  useKnowledgeBaseChoices,
  useKnowledgeBasesByIds,
  useListTotal,
} from "@/lib/queries";
import {
  knowledgeBaseOptions,
  knowledgeBaseSelectLabels,
} from "@/components/knowledge-bases/KnowledgeBaseMultiSelect";
import { useSelection } from "@/lib/useSelection";
import { APP_ROUTES } from "@/lib/routes";
import { t, type I18nKey } from "@/lib/i18n";
import { queueAheadText, queueReasonText, queueWaitView } from "@/lib/ingestion-queue";
import { formatBytes, formatDateTime, formatNumber } from "@/lib/format";
import { toast } from "@/lib/toast";
import { listToolbarLabels, paginationLabels } from "@/lib/pagination-labels";
import { listCountProps } from "@/lib/list-paging";
import { useWorkspaceState } from "@/lib/workspace-state";
import { documentFileTypeLabel, ingestionSkipReasonLabel } from "@/lib/source-profile-labels";

import {
  bulkIngestSeverity,
  classifyEnqueuedJob,
  FILE_LIST_FILTERS as FILTERS,
  FILE_LIST_QUERY_MAX_LENGTH,
  fileListFilterLabel,
  fileListFilterParams,
  idsToKeepSelected,
  type FileListFilter,
  INITIAL_FILE_LIST_VIEW as INITIAL_VIEW,
  isFileListView,
  orderedBulkReasons,
  summarizeDeleteOutcomes,
  type DeleteOutcome,
} from "./FileListClient.logic";
import { deleteConfirmDescription } from "./document-delete-impact";

const INGESTIBLE: ReadonlySet<FileStatus> = new Set(["UPLOADED", "ERROR"]);

/**
 * 取込を始められる行か。状態はレシピ1の状態（#2012）で、投入直後の job は worker が拾うまで状態が
 * 変わらないため、待機中・実行中の job がある行は除く（投入しても 409 になる）。
 */
function canEnqueue(doc: DocumentSummary): boolean {
  return INGESTIBLE.has(doc.status) && doc.ingestion_active !== true;
}

/** 一覧の状態列の順番待ちの説明（前の件数・理由）。詳細の工程と同じ値（#2026）。 */
function ListQueueWait({ doc }: { doc: DocumentSummary }) {
  const wait = queueWaitView(doc.queue_wait);
  if (!wait) return null;
  const text = [queueAheadText(wait), queueReasonText(wait)].filter(Boolean).join(" · ");
  if (!text) return null;
  return (
    <span
      className="basis-full text-xs text-fg-muted"
      data-testid={`file-list-queue-wait-${doc.id}`}
    >
      {text}
    </span>
  );
}

/** 取込対象ドキュメントの一覧。絞り込み・検索・ページング・一括選択・行内アクション。 */
export function FileListClient() {
  const qc = useQueryClient();
  const confirm = useConfirm();
  // 絞り込み・検索・ページは、ページを行き来しても再読込しても残す（workspace-state.md）。
  // 一括削除につながる行の選択は保存せず、ページを離れたら解除する。
  const [view, setView] = useWorkspaceState("fileList.view", INITIAL_VIEW, isFileListView);
  const { filter, q, knowledgeBaseId, offset, limit } = view;
  const [languageParams, setLanguageParams] = useSearchParams();
  const rawRelation = languageParams.get("language_pack_relation");
  const languageRelation = rawRelation && ["current", "equivalent", "different", "unrecorded"].includes(rawRelation) ? rawRelation as import("@/lib/api").LanguagePackRelation : view.languagePackRelation;
  const languageReleaseId = languageParams.get("current_release_id") ?? view.languagePackReleaseId;
  const languageScope = { language_pack_relation: languageRelation, current_release_id: languageReleaseId };
  const setLanguageRelation = (value: string) => {
    setLanguageParams((current) => { const next = new URLSearchParams(current); next.delete("current_release_id"); if (value === "ALL") next.delete("language_pack_relation"); else next.set("language_pack_relation", value); return next; });
    setView((current) => ({ ...current, offset: 0, languagePackRelation: value === "ALL" ? undefined : value as import("@/lib/api").LanguagePackRelation, languagePackReleaseId: undefined }));
  };
  const languageLinkApplied = useRef(false);
  useEffect(() => {
    if (languageLinkApplied.current || !rawRelation || !["current", "equivalent", "different", "unrecorded"].includes(rawRelation)) return;
    languageLinkApplied.current = true;
    const releaseId = languageParams.get("current_release_id") ?? undefined;
    // 件数からの対象リンクは、以前の検索語・KB・状態を混ぜず同じ対象集合を開く。
    setView((current) => ({ ...current, offset: 0, q: "", filter: "ALL", knowledgeBaseId: "ALL", languagePackRelation: rawRelation as import("@/lib/api").LanguagePackRelation, languagePackReleaseId: releaseId }));
  }, [rawRelation, languageParams, setView]);
  const setFilter = (next: FileListFilter) => setView((current) => ({ ...current, filter: next }));
  const setQ = (next: string) => setView((current) => ({ ...current, q: next }));
  const setKnowledgeBaseId = (next: string) =>
    setView((current) => ({ ...current, knowledgeBaseId: next }));
  const setOffset = (next: number) => setView((current) => ({ ...current, offset: next }));
  // 一括投入（#2674）はサーバーが 1 回の要求で受け付ける。進み具合ではなく対象の件数を出す。
  const [bulkIngest, setBulkIngest] = useState<{ total: number } | null>(null);
  // 一括投入の結果（すべて受け付けたとき以外）と、結果を確かめられなかった失敗。次の実行まで残す。
  const [bulkResult, setBulkResult] = useState<{
    result: BulkIngestionResult;
    notSent: number;
  } | null>(null);
  const [bulkError, setBulkError] = useState<unknown>(null);
  // 表示中のページをすべて選んだ後に「条件に一致する文書をすべて選ぶ」を選んだか（#2674）。
  const [matchingAll, setMatchingAll] = useState(false);
  const [bulkDelete, setBulkDelete] = useState<{ done: number; total: number } | null>(null);
  // 削除の影響（正本を参照する重複文書）を確かめている間は、ほかの操作と同じ削除を止める（#303）。
  const [deleteImpactPending, setDeleteImpactPending] = useState(false);

  const selection = useSelection<string>();
  // 状態の絞り込みは、文書の状態か順番待ちの段のどちらか（#2677）。
  const filterParams = fileListFilterParams(filter);
  // 投入直後は UPLOADED→INGESTING の引き継ぎに数秒かかり、その瞬間はまだ非アクティブ。
  // この窓の間もポーリングを続けて取込開始を確実に拾う。
  const [graceActive, setGraceActive] = useState(false);
  // 窓を開き直すたびに増やす。増えるたびに 30 秒のタイマーを掛け直す（前のタイマーは cleanup で消える）。
  const [graceWindow, setGraceWindow] = useState(0);
  const startGraceWindow = () => {
    setGraceActive(true);
    setGraceWindow((current) => current + 1);
  };
  useEffect(() => {
    if (graceWindow === 0) return;
    const timer = window.setTimeout(() => setGraceActive(false), 30_000);
    return () => window.clearTimeout(timer);
  }, [graceWindow]);
  const query = useDocuments(
    {
      ...filterParams,
      ...languageScope,
      q: q || undefined,
      knowledge_base_id: knowledgeBaseId === "ALL" ? undefined : knowledgeBaseId,
      limit,
      offset,
    },
    { graceActive }
  );
  // 件数の「N 件（全 M 件）」の M。絞り込み中だけ、条件を外した件数を 1 件ぶんの取得で数える（#1891）。
  const filtered = q !== "" || filter !== "ALL" || knowledgeBaseId !== "ALL" || Boolean(languageRelation);
  const allTotal = useListTotal(
    "documents",
    {},
    async () => (await api.listDocuments({ limit: 1, offset: 0 })).total,
    filtered
  );
  // 絞り込みの選択肢: 200 件以下は全件を手元で絞り込み、超えるとサーバー側で検索する（全件を読まない。#578）。
  const [knowledgeBaseQuery, setKnowledgeBaseQuery] = useState("");
  const knowledgeBases = useKnowledgeBaseChoices({ status: "ACTIVE", q: knowledgeBaseQuery });
  // 選んでいる KB は ID で引く（候補のページに無くても名前を出し、削除・アーカイブを判定する）。
  const selectedKnowledgeBaseLookup = useKnowledgeBasesByIds(
    knowledgeBaseId === "ALL" ? [] : [knowledgeBaseId]
  );
  const selectedKnowledgeBase =
    knowledgeBaseId === "ALL"
      ? null
      : (selectedKnowledgeBaseLookup.data?.items.find((item) => item.id === knowledgeBaseId) ?? null);
  // 復元した KB 絞り込みが削除・アーカイブ済みなら、その条件だけ「すべて」に戻す。
  // 別の ID の結果を出している間・DB の縮退応答（warning 付きの空一覧）では判定しない。
  const knowledgeBaseFilterMissing =
    knowledgeBaseId !== "ALL" &&
    selectedKnowledgeBaseLookup.isSuccess &&
    !selectedKnowledgeBaseLookup.isPlaceholderData &&
    (selectedKnowledgeBaseLookup.data.warning_messages?.length ?? 0) === 0 &&
    selectedKnowledgeBase?.status !== "ACTIVE";
  useEffect(() => {
    if (knowledgeBaseFilterMissing) {
      setView((current) => ({ ...current, knowledgeBaseId: "ALL", offset: 0 }));
    }
  }, [knowledgeBaseFilterMissing, setView]);

  const enqueueIngestion = useEnqueueDocumentIngestionJob();
  const deleteDocument = useDeleteDocument();

  const page = query.data;
  const items = page?.items ?? [];
  // 絞り込み・検索・ページを変えて取り直す間は、前の条件の一覧（keepPreviousData。#535）を出したまま。
  // その間の件数・行は今の条件の結果ではないため、選択・一括操作・件数に使わない（#2696）。
  const listStale = query.isPlaceholderData;
  // 全面のエラーは初回の取得（一覧が無い）の失敗だけ。一覧があるときの再取得の失敗は一覧の上に出す（#938）。
  const loadError = initialLoadError(query);
  const refreshError = query.isError && !loadError ? query.error : null;
  // ヘッダーの「最新情報を取得」は押した取り直しの間だけ回す（定期の取り直し・投入や削除の後の invalidate・
  // 絞り込みの切り替えでは回さない。前の一覧は出したまま。#819・#2012）。
  const manualRefresh = useActionPending();
  const [manualRetrying, setManualRetrying] = useState(false);
  const retryList = async () => {
    setManualRetrying(true);
    try {
      await query.refetch();
    } finally {
      setManualRetrying(false);
    }
  };
  const pageIds = items.map((d) => d.id);
  // 再取得で一覧から消えた行（他の画面で削除・状態の絞り込みから外れた）は選択に数えない。
  const selectedDocuments = items.filter((d) => selection.isSelected(d.id));
  const selectedCount = selectedDocuments.length;
  const allSelected = pageIds.length > 0 && selectedCount === pageIds.length;

  const ingestibleSelected = selectedDocuments.filter(canEnqueue);
  // 条件に一致する文書をすべて選ぶのは、表示中のページをすべて選んでいる間だけ（行の選択を変える・ページを
  // 移る・絞り込みを変えると解除する）。件数は今の条件の応答だけから出す（前の条件の値では出さない。#2696）。
  const matchingTotal = page?.total ?? 0;
  const matchingActive = matchingAll && allSelected && !listStale;
  const clearSelection = () => {
    selection.clear();
    setMatchingAll(false);
  };
  // 条件（状態・検索語・ナレッジベース・ページ）が変わったら、どの経路で変わっても選択を解除する
  // （削除・アーカイブ済みの KB の絞り込みを「すべて」に戻すときなど、resetView を通らない変更を含む。#2696）。
  const selectionScope = JSON.stringify([filter, q, knowledgeBaseId, offset, limit, languageRelation, languageReleaseId]);
  const selectionScopeRef = useRef(selectionScope);
  const clearSelected = selection.clear;
  useEffect(() => {
    if (selectionScopeRef.current === selectionScope) return;
    selectionScopeRef.current = selectionScope;
    clearSelected();
    setMatchingAll(false);
  }, [selectionScope, clearSelected]);
  const canSelectAllMatching =
    !listStale && !matchingActive && allSelected && matchingTotal > items.length;
  const bulkIngestCount = matchingActive ? matchingTotal : ingestibleSelected.length;
  const bulkBusy = bulkIngest !== null || bulkDelete !== null || deleteImpactPending;
  const allKnowledgeBasesOption = useMemo<SearchableSelectOption>(
    () => ({ value: "ALL", label: t("fileList.knowledgeBaseFilter.all") }),
    []
  );
  const knowledgeBaseFilterOptions = useMemo<SearchableSelectOption[]>(
    () => [
      // サーバー側の検索では「すべて」は検索語に一致しないため、検索語が無いときだけ先頭に置く。
      ...(knowledgeBases.remote && knowledgeBaseQuery ? [] : [allKnowledgeBasesOption]),
      ...knowledgeBaseOptions(knowledgeBases.items, { local: !knowledgeBases.remote }).map(
        // 絞り込みでは「最多」を付けない（文書数は右端に出す）。
        (option) => ({ ...option, badge: undefined })
      ),
    ],
    [allKnowledgeBasesOption, knowledgeBaseQuery, knowledgeBases.items, knowledgeBases.remote]
  );
  const statusOptions = useMemo<SelectFieldOption<FileListFilter>[]>(
    () => FILTERS.map((value) => ({ value, label: fileListFilterLabel(value) })),
    []
  );

  const resetView = (fn: () => void) => {
    fn();
    setOffset(0);
    clearSelection();
  };

  // 検索語は入力に合わせて適用する（SearchField の debounce・IME 対応。#535）。検索語が変わったときだけ
  // 先頭ページへ戻し選択を解除する（フォーカスを外しただけではページ位置と選択を失わない。#281）。
  const applySearch = (next: string) => {
    if (next === q) return;
    resetView(() => setQ(next));
  };

  const runRowIngest = (doc: DocumentSummary) => {
    enqueueIngestion.mutate(
      { id: doc.id, force: false },
      {
        onSuccess: (job) => {
          startGraceWindow();
          notifyRowEnqueued(doc, job);
        },
        onError: (error) => {
          toast.error(t("fileList.ingest.toast.failed", { name: doc.file_name }), {
            description:
              error instanceof ApiError ? error.message : t("fileList.ingest.toast.failedHint"),
          });
        },
      }
    );
  };

  const runBulkIngest = async () => {
    if (bulkBusy || listStale) return;
    const matching = matchingActive;
    const pageTargets = ingestibleSelected.map((d) => d.id);
    if (!matching && pageTargets.length === 0) return;
    setBulkResult(null);
    setBulkError(null);
    setBulkIngest({ total: matching ? matchingTotal : pageTargets.length });
    try {
      let targets = pageTargets;
      let notSent = 0;
      if (matching) {
        // 一覧と同じ条件の文書を、上限まで新しい順に読む（受付の判定はサーバーが文書ごとに行う）。
        const list = await api.listDocumentIds({
          ...filterParams,
      ...languageScope,
          q: q || undefined,
          knowledge_base_id: knowledgeBaseId === "ALL" ? undefined : knowledgeBaseId,
          limit: BULK_INGESTION_MAX_DOCUMENTS,
        });
        targets = list.ids;
        notSent = Math.max(0, list.total - list.ids.length);
        setBulkIngest({ total: targets.length });
      }
      if (targets.length === 0) return;
      // サーバーが受け付けた文書の job を永続化してから結果を返す。画面を閉じても worker が処理する。
      const result = await api.bulkEnqueueDocumentIngestionJobs(targets);
      setMatchingAll(false);
      // 受付不可の文書だけを選択に残す（理由を確かめてから、もう一度投入できる）。
      selection.replace(idsToKeepSelected(result));
      if (result.accepted_count > 0) startGraceWindow();
      if (bulkIngestSeverity(result) === "success" && notSent === 0) {
        toast.success(
          t("fileList.bulkQueue.toast.accepted", { count: formatNumber(result.accepted_count) }),
          { description: t("fileList.bulkQueue.backgroundHint") }
        );
      } else {
        setBulkResult({ result, notSent });
      }
    } catch (error) {
      // 通信断・timeout では、受け付けたかを確かめられない。選択を残し、再送（二重に登録しない）を案内する。
      setBulkError(error);
    } finally {
      setBulkIngest(null);
      qc.invalidateQueries({ queryKey: ["documents"] });
      qc.invalidateQueries({ queryKey: ["documents", "ingestion-jobs"] });
    }
  };

  const loadDeleteImpacts = async (ids: string[]): Promise<DocumentDeleteImpact[] | null> => {
    setDeleteImpactPending(true);
    try {
      return await api.getDocumentDeleteImpact(ids);
    } catch (error) {
      toast.error(t("fileList.delete.impact.loadFailed"), {
        description:
          error instanceof ApiError ? error.message : t("fileList.delete.impact.loadFailedHint"),
      });
      return null;
    } finally {
      setDeleteImpactPending(false);
    }
  };

  const runBulkDelete = async () => {
    const targets = selectedDocuments.map((doc) => doc.id);
    if (targets.length === 0 || bulkBusy || listStale) return;
    const impacts = await loadDeleteImpacts(targets);
    if (!impacts) return;
    const confirmed = await confirm({
      title: t("fileList.bulkDelete.confirm.title", { count: targets.length }),
      description: deleteConfirmDescription(
        t("fileList.bulkDelete.confirm.description", { count: targets.length }),
        impacts,
        { bulk: true }
      ),
      confirmLabel: t("fileList.bulkDelete.confirm.confirm"),
      tone: "danger",
      dismissOnOverlay: false,
    });
    if (!confirmed) return;

    setBulkDelete({ done: 0, total: targets.length });
    const outcomes: DeleteOutcome[] = [];
    for (const [index, id] of targets.entries()) {
      try {
        const result = await api.deleteDocument(id);
        outcomes.push({ kind: "deleted", warnings: result.warning_messages ?? [] });
      } catch (error) {
        outcomes.push({
          kind: error instanceof ApiError && error.errorCode === "document_delete_unconfirmed" ? "unconfirmed" : "failed",
          message:
            error instanceof ApiError ? error.message : t("fileList.bulkDelete.toast.failedHint"),
        });
      }
      setBulkDelete({ done: index + 1, total: targets.length });
    }
    const { deleted, warned, failed, firstWarning, firstError, unconfirmed, firstUnconfirmed } =
      summarizeDeleteOutcomes(outcomes);
    setBulkDelete(null);
    selection.replace(targets.filter((_, index) => outcomes[index]?.kind === "unconfirmed"));
    setMatchingAll(false);
    qc.invalidateQueries({ queryKey: ["documents"] });
    qc.invalidateQueries({ queryKey: ["documents", "ingestion-jobs"] });
    qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    qc.invalidateQueries({ queryKey: ["documents", "stats"] });

    if (unconfirmed > 0) {
      toast.warning(t("fileList.bulkDelete.toast.unconfirmed", {deleted, failed, count: unconfirmed}), {
        description: [firstUnconfirmed, firstError, firstWarning].filter(Boolean).join(" "),
      });
    } else if (failed === 0 && warned === 0) {
      toast.success(t("fileList.bulkDelete.toast.deleted", { count: deleted }));
    } else if (failed === 0) {
      toast.warning(t("fileList.bulkDelete.toast.deletedWithWarning", { count: deleted, warned }), {
        description: firstWarning ?? undefined,
      });
    } else if (deleted > 0) {
      toast.warning(t("fileList.bulkDelete.toast.partial", { deleted, total: targets.length }), {
        description: firstError ?? t("fileList.bulkDelete.toast.failedHint"),
      });
    } else {
      toast.error(t("fileList.bulkDelete.toast.failed"), {
        description: firstError ?? t("fileList.bulkDelete.toast.failedHint"),
      });
    }
  };

  const runDelete = async (doc: DocumentSummary) => {
    if (deleteImpactPending) return;
    const impacts = await loadDeleteImpacts([doc.id]);
    if (!impacts) return;
    const confirmed = await confirm({
      title: t("fileList.delete.confirm.title"),
      description: deleteConfirmDescription(
        t("fileList.delete.confirm.description", { name: doc.file_name }),
        impacts,
        { bulk: false }
      ),
      confirmLabel: t("fileList.delete.confirm.confirm"),
      tone: "danger",
      dismissOnOverlay: false,
    });
    if (!confirmed) return;
    try {
      const result = await deleteDocument.mutateAsync({id: doc.id, fileName: doc.file_name});
      clearSelection();
      // 文書は消えたが原本・artifact の削除に失敗した警告は、成功として黙らせない（#281）。
      const warnings = result.warning_messages ?? [];
      if (warnings.length > 0) {
        toast.warning(t("fileList.delete.toast.deletedWithWarning", { name: result.file_name }), {
          description: warnings.join(" "),
        });
      } else {
        toast.success(t("fileList.delete.toast.deleted", { name: result.file_name }));
      }
    } catch (error) {
      if (error instanceof ApiError && error.errorCode === "document_delete_unconfirmed") {
        toast.warning(error.message);
        void qc.invalidateQueries({queryKey: ["documents"]});
        return;
      }
      toast.error(
        error instanceof ApiError
          ? error.message
          : t("fileList.delete.toast.failed")
      );
    }
  };

  return (
    <div>
      <PageHeader
        wide
        title={t("nav.fileList")}
        subtitle={t("fileList.subtitle")}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("fileList.refresh"),
            icon: RefreshCw,
            loading: manualRefresh.pending,
            onClick: () => void manualRefresh.track(() => query.refetch()),
            testId: "file-list-refresh",
          },
        ]}
      />
      <PageBody wide>
        {/* DB 停止時の縮退お知らせ(非ブロッキング) */}
        <DegradedBanner
          messages={page?.warning_messages}
          onRetry={() => void query.refetch()}
          isRetrying={query.isFetching}
        />

        {/* 一覧のツールバー: 左に検索、その右に状態・ナレッジベースの絞り込み（page-archetypes.md「一覧のツールバー」。#600）。
            状態は 11 種あり FilterChipGroup の上限（5）を超えるため FilterSelect、ナレッジベースは数百件から選ぶので
            検索できる選択（ラベルはボタンの中）にする（#578・#1933）。 */}
        <ListToolbar
          search={
            <SearchField
              id="file-list-search"
              label={t("fileList.searchPlaceholder")}
              labelHidden
              value={q}
              onSearch={applySearch}
              clearLabel={t("common.clearSearch")}
              resultCountLabel={
                page && !listStale
                  ? t("common.searchResultCount", { count: formatNumber(page.total) })
                  : ""
              }
              maxLength={FILE_LIST_QUERY_MAX_LENGTH}
              placeholder={t("fileList.searchPlaceholder")}
            />
          }
          filters={
            <>
              <FilterSelect
                id="file-list-status"
                label={t("fileList.statusFilter.label")}
                value={filter}
                options={statusOptions}
                onValueChange={(value) => resetView(() => setFilter(value))}
                // 最も長い状態（「状態: Embedding / 索引の順番待ち」）が切れない幅（#613・#2677）。
                width="md"
              />
              <FilterSelect id="file-list-language-relation" label={t("settings.language.release.relationFilter")} value={languageRelation ?? "ALL"} options={[{ value: "ALL", label: t("fileList.filterAll") }, ...(["current", "equivalent", "different", "unrecorded"] as const).map((value) => ({ value, label: t(`settings.language.release.relation.${value}`) }))]} onValueChange={setLanguageRelation} width="lg" />
              <SearchableSelectField
                id="file-list-knowledge-base"
                label={t("fileList.knowledgeBaseFilter.label")}
                value={knowledgeBaseId}
                options={knowledgeBaseFilterOptions}
                selectedOption={
                  knowledgeBaseId === "ALL"
                    ? allKnowledgeBasesOption
                    : selectedKnowledgeBase
                      ? { value: selectedKnowledgeBase.id, label: selectedKnowledgeBase.name }
                      : null
                }
                onValueChange={(value) => resetView(() => setKnowledgeBaseId(value))}
                // ツールバーの中ではラベルをボタンの中に出す（FilterSelect と同じ形。#1933）。
                labelPlacement="inline"
                active={isFilterActive(knowledgeBaseId, "ALL")}
                onQueryChange={setKnowledgeBaseQuery}
                remote={
                  knowledgeBases.remote
                    ? {
                        total: knowledgeBases.matchedTotal,
                        hasMore: knowledgeBases.hasMore,
                        loadingMore: knowledgeBases.loadingMore,
                        searching: knowledgeBases.searching,
                        onLoadMore: knowledgeBases.loadMore,
                      }
                    : undefined
                }
                labels={{
                  ...knowledgeBaseSelectLabels(),
                  // 絞り込みは選ぶだけ（「追加」ではない）。
                  searchPlaceholder: t("knowledgeBasePicker.searchPlaceholder"),
                }}
                // 「ナレッジベース: 利用できるすべてのナレッジベース」が 1 行に収まる幅。
                // 長い名前は 1 行で切り、全文はボタンの title で読む（ツールバーの行の高さをそろえる。#1933）。
                width="lg"
              />
            </>
          }
          filtersLayout="sheet"
          activeFilterCount={[isFilterActive(filter, "ALL"), isFilterActive(knowledgeBaseId, "ALL"), Boolean(languageRelation)].filter(Boolean).length}
          searchActive={q !== ""}
          onClearFilters={() =>
            resetView(() => { setLanguageRelation("ALL"); setView((current) => ({ ...current, q: "", filter: "ALL", knowledgeBaseId: "ALL" })); })
          }
          labels={listToolbarLabels()}
          count={listCountProps({
            // 前の条件の一覧を出している間は、その件数を今の条件の件数として出さない（#2696）。
            matchedTotal: listStale ? undefined : page?.total,
            filtered,
            allTotal: allTotal.data,
          })}
          testId="file-list-toolbar"
        />

        {knowledgeBases.isError ? (
          <Banner severity="warning" title={t("knowledgeBaseScope.loadWarning")}>
            <p>
              {knowledgeBases.error instanceof ApiError
                ? knowledgeBases.error.message
                : t("knowledgeBaseScope.loadWarningHint")}
            </p>
          </Banner>
        ) : null}

        {/* 前回取得した一覧があるときの再取得（自動更新を含む）の失敗は、一覧と選択を残して知らせる
            （全面のエラーにすると、見えない選択行の一括操作だけが残る。messaging.md §3.6・#311・#938）。 */}
        {refreshError ? (
          <div data-testid="file-list-refresh-error">
            <Banner
              severity="warning"
              title={t("fileList.refreshError")}
              action={
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  icon={RefreshCw}
                  // 自動更新・自動の再試行の間も押せるよう、押した再試行の間だけ loading にする。
                  loading={manualRetrying}
                  onClick={() => void retryList()}
                >
                  {t("common.retry")}
                </Button>
              }
            >
              <p>{loadErrorMessage(refreshError)}</p>
            </Banner>
          </div>
        ) : null}

        {/* 一括操作バー。選択で出し入れせず常に出し、選択が無いときは操作を無効にする（表の位置を
            動かさず、一括操作があることを先に見せる。buttons.md §5.1・#699）。 */}
        {items.length > 0 || bulkBusy ? (
          <div
            className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-surface-sunken px-4 py-2.5"
            data-testid="file-list-bulk-actions"
          >
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              {/* 進み具合はボタンのラベルではなくここに出す（loading 中にラベルを差し替えない）。 */}
              <span
                className={`text-sm ${selectedCount > 0 || bulkBusy ? "font-medium text-fg" : "text-fg-muted"}`}
                role="status"
                data-testid="file-list-bulk-status"
              >
                {bulkIngest
                  ? t("fileList.bulkQueueRunning", { total: formatNumber(bulkIngest.total) })
                  : bulkDelete
                    ? t("fileList.bulkDeleteRunning", {
                        done: bulkDelete.done,
                        total: bulkDelete.total,
                      })
                    : matchingActive
                      ? t("fileList.selectedAllMatching", { count: formatNumber(matchingTotal) })
                      : selectedCount > 0
                        ? t("fileList.selected", { count: selectedCount })
                        : listStale
                          ? t("fileList.selectionLoading")
                          : t("fileList.selectionHint")}
              </span>
              {/* 表示中のページをすべて選んだら、全ページの条件に一致する文書を選べる（一括投入の上限まで）。 */}
              {canSelectAllMatching && !bulkBusy ? (
                matchingTotal <= BULK_INGESTION_MAX_DOCUMENTS ? (
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    icon={CheckSquare}
                    onClick={() => setMatchingAll(true)}
                    data-testid="file-list-select-all-matching"
                  >
                    {t("fileList.selectAllMatching", { count: formatNumber(matchingTotal) })}
                  </Button>
                ) : (
                  <span className="text-xs text-fg-muted" data-testid="file-list-select-all-over-limit">
                    {t("fileList.selectAllMatchingOverLimit", {
                      count: formatNumber(matchingTotal),
                      limit: formatNumber(BULK_INGESTION_MAX_DOCUMENTS),
                    })}
                  </span>
                )
              ) : null}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                onClick={() => void runBulkIngest()}
                loading={bulkIngest !== null}
                // 新しい条件の一覧を読み込む間は押せない（前の条件の件数・行で投入させない。#2696）。
                disabled={bulkBusy || listStale || bulkIngestCount === 0} icon={Sparkles}>
                {`${t("fileList.bulkQueue")} (${formatNumber(bulkIngestCount)})`}
              </Button>
              {/* 確認ダイアログを開く起点（確定は ConfirmDialog の danger ボタン）。赤塗りにせず secondary + tone="danger"。 */}
              <Button
                variant="secondary"
                tone="danger"
                size="sm"
                onClick={() => void runBulkDelete()}
                loading={bulkDelete !== null}
                // 条件に一致する文書をすべて選んだ間は一括投入だけ（削除は表示中の行を選んで行う）。
                disabled={bulkBusy || listStale || matchingActive || selectedDocuments.length === 0} icon={Trash2}>
                {`${t("fileList.bulkDelete")} (${selectedDocuments.length})`}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={clearSelection}
                disabled={bulkBusy || selectedCount === 0}
                icon={X}
              >
                {t("fileList.clearSelection")}
              </Button>
            </div>
          </div>
        ) : null}

        <LanguagePackBulkReprocess key={`${selectionScope}:${Array.from(selection.selected).join(",")}`} disabled={bulkBusy || listStale || selectedCount === 0} expectedReleaseId={languageReleaseId ?? items.find((item) => item.language_pack)?.language_pack?.current_release_id} loadDocumentIds={async () => {
          if (!matchingActive) return selectedDocuments.map((document) => document.id);
          const targets = await api.listDocumentIds({ ...filterParams, ...languageScope, q: q || undefined, knowledge_base_id: knowledgeBaseId === "ALL" ? undefined : knowledgeBaseId, limit: BULK_INGESTION_MAX_DOCUMENTS });
          if (targets.total > targets.ids.length) throw new Error(t("settings.language.release.tooManyTargets", { count: targets.total, max: BULK_INGESTION_MAX_DOCUMENTS }));
          return targets.ids;
        }} />
        {/* 一括投入の結果は一括操作のバーの直下に残す（次の実行・閉じるまで。messaging.md §10）。 */}
        {bulkError ? (
          <ApiErrorBanner
            error={bulkError}
            fallback={t("fileList.bulkQueue.error.title")}
            nextAction={t("fileList.bulkQueue.error.hint")}
            testId="file-list-bulk-ingest-error"
          />
        ) : null}
        {bulkResult ? (
          <BulkIngestResultBanner
            result={bulkResult.result}
            notSent={bulkResult.notSent}
            onDismiss={() => setBulkResult(null)}
          />
        ) : null}

        {loadError ? (
          <ApiErrorState
            error={loadError}
            fallback={t("fileList.loadError")}
            onRetry={() => void query.refetch()}
          />
        ) : query.isPending ? (
          <TimedLoadingState
            label={t("fileList.loading")}
            operationKey="file-list-load"
            testId="file-list-loading"
          >
            <TableSkeleton columns={8} />
          </TimedLoadingState>
        ) : (page?.total ?? 0) > 0 ? (
          // 前の条件の一覧を出したまま取り直す間は、読み込み中と伝える（#2696）。
          <div className="grid gap-2" aria-busy={listStale || undefined}>
            <DataTable<DocumentSummary>
              // 表示中のページが範囲外になったら（最後のページの最後の行を削除した等）、OffsetPagination が残っている
              // 最後のページへ移す。移る間は空のページに「該当なし」を出さず、表の形の読み込み中にする（#281）。
              // DB の縮退応答（warning 付きの空一覧）は total が 0 なので、空の案内のままページ位置を保つ。
              loading={items.length === 0}
              columns={documentColumns({
                allSelected,
                // 前の条件の行は選ばせない（今の条件に入らない文書を一括操作に送らない。#2696）。
                selectionDisabled: listStale,
                onToggleAll: () => {
                  setMatchingAll(false);
                  selection.toggleAll(pageIds);
                },
                isSelected: (doc) => selection.isSelected(doc.id),
                onToggle: (doc) => {
                  setMatchingAll(false);
                  selection.toggle(doc.id);
                },
                onIngest: runRowIngest,
                onDelete: (doc) => void runDelete(doc),
                isIngesting: (doc) =>
                  enqueueIngestion.isPending &&
                  enqueueIngestion.variables?.id === doc.id,
                isDeleting: (doc) =>
                  deleteDocument.isPending && deleteDocument.variables?.id === doc.id,
                // 一括選択中は行の操作を止め、一括操作のバーに集める（buttons.md §5.1）。
                actionsDisabled: bulkBusy || selectedCount > 0,
              })}
              rows={items}
              getRowKey={(doc) => doc.id}
              isRowSelected={(doc) => selection.isSelected(doc.id)}
              rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
              stickyHeader
              visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
              growToViewport
              scrollAriaLabel={t("fileList.scrollLabel")}
              scrollTestId="file-list-scroll-region"
              tableClassName="w-full min-w-[70rem] text-sm"
            />
            <OffsetPagination
              offset={offset}
              limit={limit}
              total={page?.total ?? 0}
              count={items.length}
              disabled={listStale}
              onPageChange={(_page, nextOffset) => {
                setOffset(nextOffset);
                clearSelection();
              }}
              onPageSizeChange={(nextLimit) => {
                setView((current) => ({ ...current, limit: nextLimit, offset: 0 }));
                clearSelection();
              }}
              labels={paginationLabels()}
              ariaLabel={t("fileList.paginationLabel")}
              testId="file-list-pagination"
            />
          </div>
        ) : (
          <Card>
            <div className="p-5">
              <EmptyState
                title={t("fileList.empty")}
                action={
                  q ? (
                    <ClearActionButton
                      label={t("common.clearSearch")}
                      onClick={() => applySearch("")}
                    />
                  ) : undefined
                }
              />
            </div>
          </Card>
        )}
      </PageBody>
    </div>
  );
}

/** 一覧の取得の失敗の文。backend の理由（ApiError）があればそれを出す。 */
function loadErrorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : t("fileList.loadError");
}

/** 行の「ファイル準備を実行」の結果を知らせる。SKIPPED は状態が変わらないため理由を出す。 */
function notifyRowEnqueued(doc: DocumentSummary, job: IngestionJob) {
  const outcome = classifyEnqueuedJob(job);
  if (outcome.kind === "skipped") {
    toast.warning(t("fileList.ingest.toast.skipped", { name: doc.file_name }), {
      description: ingestionSkipReasonLabel(outcome.job.skip_reason),
    });
    return;
  }
  // 1 文目は何が起きたかだけにし、補足は description に分ける（#899）。
  toast.info(t("fileList.ingest.toast.queued", { name: doc.file_name }), {
    description: t("fileList.queue.toast.statusHint"),
  });
}

const BULK_REASON_KEYS: Partial<Record<string, I18nKey>> = {
  invalid_document_id: "fileList.bulkQueue.reason.invalid_document_id",
  not_found: "fileList.bulkQueue.reason.not_found",
  processing: "fileList.bulkQueue.reason.processing",
  job_active: "fileList.bulkQueue.reason.job_active",
  already_indexed: "fileList.bulkQueue.reason.already_indexed",
  awaiting_confirmation: "fileList.bulkQueue.reason.awaiting_confirmation",
  temporary_failure: "fileList.bulkQueue.reason.temporary_failure",
};

const BULK_OUTCOME_KEYS: Record<BulkIngestionReasonCount["outcome"], I18nKey> = {
  accepted: "fileList.bulkQueue.result.outcome.already_active",
  already_active: "fileList.bulkQueue.result.outcome.already_active",
  skipped: "fileList.bulkQueue.result.outcome.skipped",
  rejected: "fileList.bulkQueue.result.outcome.rejected",
};

/** 一括投入の理由の文。スキップは 1 件の投入と同じ文、解析エンジン・競合はサーバーの文を出す。 */
function bulkReasonLabel(entry: BulkIngestionReasonCount): string {
  const key = BULK_REASON_KEYS[entry.reason];
  if (key) return t(key);
  if (entry.outcome === "skipped") return ingestionSkipReasonLabel(entry.reason);
  return entry.message ?? entry.reason;
}

/**
 * 一括投入の結果（すべて受け付けたとき以外。#2674）。受付済み・処理中・スキップ・受付不可の件数と理由を
 * 出し、受付不可が残るときは完了と見せない（warning / danger）。
 */
function BulkIngestResultBanner({
  result,
  notSent,
  onDismiss,
}: {
  result: BulkIngestionResult;
  notSent: number;
  onDismiss: () => void;
}) {
  const severity = bulkIngestSeverity(result);
  const total = formatNumber(result.unique_count + notSent);
  return (
    <div data-testid="file-list-bulk-ingest-result">
      <Banner
        severity={severity === "success" ? "info" : severity}
        title={
          result.accepted_count > 0
            ? t("fileList.bulkQueue.result.title", {
                total,
                accepted: formatNumber(result.accepted_count),
              })
            : t("fileList.bulkQueue.result.titleNone", { total })
        }
        onDismiss={onDismiss}
        dismissLabel={t("fileList.bulkQueue.result.dismiss")}
      >
        <p>
          {t("fileList.bulkQueue.result.counts", {
            accepted: formatNumber(result.accepted_count),
            active: formatNumber(result.already_active_count),
            skipped: formatNumber(result.skipped_count),
            rejected: formatNumber(result.rejected_count),
          })}
        </p>
        {result.reasons.length > 0 ? (
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {orderedBulkReasons(result.reasons).map((entry) => (
              <li key={`${entry.outcome}:${entry.reason}:${entry.message ?? ""}`}>
                {t("fileList.bulkQueue.result.reason", {
                  outcome: t(BULK_OUTCOME_KEYS[entry.outcome]),
                  label: bulkReasonLabel(entry),
                  count: formatNumber(entry.count),
                })}
              </li>
            ))}
          </ul>
        ) : null}
        {notSent > 0 ? (
          <p className="mt-1">
            {t("fileList.bulkQueue.result.notSent", {
              count: formatNumber(notSent),
              limit: formatNumber(BULK_INGESTION_MAX_DOCUMENTS),
            })}
          </p>
        ) : null}
        {result.rejected_count > 0 ? (
          <p className="mt-1">{t("fileList.bulkQueue.result.keptSelection")}</p>
        ) : null}
        {result.accepted_count > 0 ? (
          <p className="mt-1">{t("fileList.bulkQueue.backgroundHint")}</p>
        ) : null}
      </Banner>
    </div>
  );
}

/** 一覧の列定義。先頭列は一括選択のチェックボックス、ファイル名列を行見出しにする。 */
function documentColumns({
  allSelected,
  selectionDisabled,
  onToggleAll,
  isSelected,
  onToggle,
  onIngest,
  onDelete,
  isIngesting,
  isDeleting,
  actionsDisabled,
}: {
  allSelected: boolean;
  selectionDisabled: boolean;
  onToggleAll: () => void;
  isSelected: (doc: DocumentSummary) => boolean;
  onToggle: (doc: DocumentSummary) => void;
  onIngest: (doc: DocumentSummary) => void;
  onDelete: (doc: DocumentSummary) => void;
  isIngesting: (doc: DocumentSummary) => boolean;
  isDeleting: (doc: DocumentSummary) => boolean;
  actionsDisabled: boolean;
}): DataTableColumn<DocumentSummary>[] {
  return [
    {
      key: "select",
      header: (
        <input
          type="checkbox"
          checked={allSelected}
          onChange={onToggleAll}
          disabled={selectionDisabled}
          aria-label={t("fileList.selectAllAria")}
          className="cursor-pointer accent-accent-emphasis disabled:cursor-not-allowed"
        />
      ),
      headerClassName: "w-10",
      render: (doc) => (
        <input
          type="checkbox"
          checked={isSelected(doc)}
          onChange={() => onToggle(doc)}
          disabled={selectionDisabled}
          aria-label={t("fileList.selectRowAria")}
          className="cursor-pointer accent-accent-emphasis disabled:cursor-not-allowed"
        />
      ),
    },
    {
      key: "fileName",
      header: t("fileList.col.fileName"),
      rowHeader: true,
      className: "max-w-[18.57rem]",
      render: (doc) => (
        <Link
          to={`${APP_ROUTES.documents}/${doc.id}`}
          className="block truncate font-medium text-accent-fg hover:underline"
          title={doc.file_name}
        >
          {doc.file_name}
        </Link>
      ),
    },
    {
      key: "knowledgeBases",
      header: t("fileList.col.knowledgeBases"),
      className: "max-w-[17.14rem]",
      render: (doc) => <KnowledgeBaseChips knowledgeBases={doc.knowledge_bases ?? []} />,
    },
    {
      key: "category",
      header: t("fileList.col.category"),
      className: "text-fg-muted",
      render: (doc) => documentFileTypeLabel(doc),
    },
    {
      key: "status",
      header: t("fileList.col.status"),
      render: (doc) => (
        <div className="flex flex-wrap items-center gap-1.5">
          {/* 取込の job が worker の空きを待っている間は「〈工程〉の順番待ち」と前の件数・理由（#2026）。 */}
          <StatusBadge status={doc.status} queuedPhase={doc.queue_wait?.phase} />
          <ListQueueWait doc={doc} />
          {/* 新しい版に置き換えた文書（旧版）は回答の検索の対象から外れる（#1248）。 */}
          {doc.is_superseded ? (
            <span title={t("fileList.supersededTitle")} data-testid={`file-list-superseded-${doc.id}`}>
              <UiStatusBadge variant="warning" icon={History} label={t("fileList.superseded")} />
            </span>
          ) : null}
          {/* 作成後に項目の定義などが変わった派生情報がある（#550）。作り直しは文書の詳細の「再処理」。 */}
          {doc.layers_rebuild_required ? (
            <span title={t("fileList.layersRebuildTitle")} data-testid={`file-list-rebuild-${doc.id}`}>
              <UiStatusBadge
                variant="warning"
                icon={RefreshCw}
                label={t("fileList.layersRebuild")}
              />
            </span>
          ) : null}
        </div>
      ),
    },
    { key: "language_pack", header: t("settings.language.release.usedVersion"), render: (doc) => <LanguagePackUseSummary use={doc.language_pack} documentId={doc.id} /> },
    {
      key: "size",
      header: t("fileList.col.size"),
      align: "right",
      className: "tnum text-fg-muted",
      render: (doc) => formatBytes(doc.file_size_bytes),
    },
    {
      key: "uploadedAt",
      header: t("fileList.col.uploadedAt"),
      className: "tnum text-fg-muted",
      render: (doc) => formatDateTime(doc.uploaded_at),
    },
    {
      key: "actions",
      header: t("fileList.col.actions"),
      align: "right",
      render: (doc) => (
        <RowActionMenu
          actions={documentActions(doc, { onIngest, onDelete, isIngesting, isDeleting })}
          ariaLabel={t("common.objectActions.aria", { name: doc.file_name })}
          loading={isIngesting(doc) || isDeleting(doc)}
          disabled={actionsDisabled}
          testId={`file-list-row-actions-${doc.id}`}
        />
      ),
    },
  ];
}

/**
 * 文書 1 件に対する操作（buttons.md §5.1）。行は RowActionMenu 1 個にまとめ、
 * 削除は danger の項目として確認ダイアログ（runDelete の useConfirm）を通す。
 */
function documentActions(
  doc: DocumentSummary,
  {
    onIngest,
    onDelete,
    isIngesting,
    isDeleting,
  }: {
    onIngest: (doc: DocumentSummary) => void;
    onDelete: (doc: DocumentSummary) => void;
    isIngesting: (doc: DocumentSummary) => boolean;
    isDeleting: (doc: DocumentSummary) => boolean;
  }
): EntityAction[] {
  const ingesting = isIngesting(doc);
  const deleting = isDeleting(doc);
  return [
    {
      id: "ingest",
      label: t(doc.status === "ERROR" ? "flow.retry.preprocess" : "action.enqueueIngestion"),
      icon: doc.status === "ERROR" ? RotateCcw : Sparkles,
      visible: canEnqueue(doc),
      loading: ingesting,
      disabled: deleting,
      onSelect: () => onIngest(doc),
    },
    {
      id: "delete",
      label: t("fileList.delete.action"),
      ariaLabel: t("fileList.delete.aria", { name: doc.file_name }),
      icon: Trash2,
      tone: "danger",
      loading: deleting,
      disabled: ingesting,
      onSelect: () => onDelete(doc),
    },
  ];
}

function KnowledgeBaseChips({ knowledgeBases }: { knowledgeBases: KnowledgeBaseRef[] }) {
  if (knowledgeBases.length === 0) {
    return <span className="text-fg-muted">—</span>;
  }

  return (
    <div className="flex flex-wrap gap-1.5">
      {knowledgeBases.map((knowledgeBase) => (
        <span
          key={knowledgeBase.id}
          className="max-w-[12rem] truncate rounded-full border border-border bg-surface-sunken px-2 py-0.5 text-xs font-medium text-fg"
          title={knowledgeBase.name}
        >
          {knowledgeBase.name}
        </span>
      ))}
    </div>
  );
}
