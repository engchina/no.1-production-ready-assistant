"use client";

import { FilePlus2, Files, Unlink } from "lucide-react";
import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { DocumentPicker, type DocumentPickerSelection } from "@/components/documents/DocumentPicker";
import { EmptyState, ApiErrorState } from "@/components/StateViews";
import { EditorTargetState } from "@/components/layout/EntityLayout";
import { useAuth } from "@/components/security/AuthProvider";
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  ClearActionButton,
  DEFAULT_PAGE_SIZE,
  FormSkeleton,
  ListSkeleton,
  ListToolbar,
  OffsetPagination,
  RowActionMenu,
  SearchField,
  TimedLoadingState,
} from "@production-ready/ui";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  api,
  ApiError,
  type DocumentSummary,
  type KnowledgeBaseDetail,
} from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { t } from "@/lib/i18n";
import { isPageSize, listCountProps } from "@/lib/list-paging";
import { paginationLabels } from "@/lib/pagination-labels";
import {
  AssignDocumentsPartialError,
  useAssignDocumentsToKnowledgeBase,
  useDocuments,
  useKnowledgeBase,
  useListTotal,
  useRemoveDocumentFromKnowledgeBase,
} from "@/lib/queries";
import { canOpenDocumentDetail } from "@/lib/route-permissions";
import { APP_ROUTES } from "@/lib/routes";
import { toast } from "@/lib/toast";
import { useWorkspaceState } from "@/lib/workspace-state";
import { KnowledgeBaseEditor } from "./KnowledgeBaseEditor";
import { KnowledgeBaseExtractionFields } from "./KnowledgeBaseExtractionFields";
import { KnowledgeBaseGraphView } from "./KnowledgeBaseGraphView";
import { KnowledgeBaseSearchTestPanel } from "./KnowledgeBaseSearchTestPanel";

/**
 * ナレッジベース詳細ページ（`/knowledge-bases/:id`）。検索・回答プロファイルのエディタと同じ構成にする（#555）:
 * PageHeader（パンくず・状態・件数・一覧へ戻る・保存する）→ 基本情報（名前・説明）→ 所属文書
 * → 検索テスト → 関係情報 → 抽出する項目 → 構築フロー（文書の追加 → 確認 → 構築設定の順）。
 */
export function KnowledgeBaseDetailClient({ knowledgeBaseId }: { knowledgeBaseId: string }) {
  const detail = useKnowledgeBase(knowledgeBaseId);
  const navigate = useNavigate();
  const backToList = (options?: { replace?: boolean }) =>
    navigate(APP_ROUTES.knowledgeBases, { replace: options?.replace });

  if (!detail.data) {
    return (
      <EditorTargetState
        id={knowledgeBaseId}
        listLabel={t("nav.knowledgeBases")}
        error={detail.error}
        loadingLabel={t("knowledgeBases.detail.loading")}
        loadingTestId="knowledge-base-detail-loading"
        errorFallback={t("knowledgeBases.error.load")}
        skeleton={
          <>
            <FormSkeleton fields={2} actions={false} />
            <ListSkeleton rows={3} rowClassName="h-11" />
          </>
        }
        onBack={() => backToList()}
        onRetry={() => void detail.refetch()}
      />
    );
  }

  const kb = detail.data;
  const isActive = kb.status === "ACTIVE";

  return (
    <KnowledgeBaseEditor
      initial={kb}
      onBack={() => backToList()}
      // アーカイブした対象へ戻らないよう、一覧へ履歴を積まずに戻る（検索・回答プロファイルと同じ）。
      onArchived={() => backToList({ replace: true })}
    >
      {/* 所属文書: ツールバー（左に検索、右に「文書を追加」）+ 一覧 + ページ送り（#600）。 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Files className="size-4 text-fg-muted" aria-hidden />
            {t("knowledgeBases.documents.title")}
            <span data-surface-tint="neutral" className="tnum rounded-md bg-surface-hover px-2 py-0.5 text-xs font-medium text-fg-muted">
              {kb.document_count}
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {isActive ? null : (
            <p className="rounded-md border border-border bg-surface-sunken px-3 py-2 text-sm text-fg-muted">
              {t("knowledgeBases.detail.archivedHint")}
            </p>
          )}

          <KnowledgeBaseDocuments knowledgeBase={kb} canAssign={isActive} />
        </CardContent>
      </Card>

      {/* 項目抽出で取り出す項目の定義。無ければ全体の既定を使う。 */}
      <KnowledgeBaseExtractionFields knowledgeBaseId={kb.id} editable={isActive} />

      {/* 項目の定義の後に文書の構造を確認する。展開時のみ subgraph を取得。 */}
      <KnowledgeBaseGraphView knowledgeBaseId={kb.id} />

      {/* このナレッジ単体で検索の手応えを確認する(検索・回答プロファイル不要)。構築の設定(関係情報・抽出する項目)を
          見た後に、最後に検索で確かめる流れにするため、いちばん下に置く(#616)。 */}
      <KnowledgeBaseSearchTestPanel
        knowledgeBaseId={kb.id}
        indexedDocumentCount={kb.indexed_document_count}
        disabled={!isActive}
      />
    </KnowledgeBaseEditor>
  );
}

/**
 * 「文書を追加」（#600）。共通の文書の選択（`DocumentPicker`）で複数を選び、一度に追加する。
 * すでに所属している文書は「追加済み」で選べなくする。
 */
function DocumentAssignment({
  id,
  knowledgeBase,
  onClose,
}: {
  id: string;
  knowledgeBase: KnowledgeBaseDetail;
  onClose: (options?: { assigned?: boolean }) => void;
}) {
  const assign = useAssignDocumentsToKnowledgeBase();

  const handleAssign = (documents: DocumentSummary[], selection: DocumentPickerSelection) => {
    assign.mutate(
      { id: knowledgeBase.id, documentIds: documents.map((document) => document.id) },
      {
        onSuccess: () => {
          toast.success(t("knowledgeBases.toast.assigned"));
          onClose({ assigned: true });
        },
        onError: (error) => {
          if (error instanceof AssignDocumentsPartialError) {
            // 追加できた分は選択から外し、追加できなかった残りだけを残す（もう一度追加できる）。
            selection.deselect(error.assignedIds);
            const reason =
              error.cause instanceof ApiError ? error.cause.message : t("knowledgeBases.error.assign");
            toast.error(
              t("knowledgeBases.error.assignPartial", {
                total: formatNumber(error.total),
                assigned: formatNumber(error.assignedIds.length),
                remaining: formatNumber(error.total - error.assignedIds.length),
                reason,
              })
            );
            return;
          }
          toast.error(error instanceof ApiError ? error.message : t("knowledgeBases.error.assign"));
        },
      }
    );
  };

  return (
    <DocumentPicker
      id={id}
      testId="knowledge-base-add-documents"
      description={t("knowledgeBases.assignment.hint")}
      unavailableReason={(document) =>
        documentHasKnowledgeBase(document, knowledgeBase.id) ? t("documentPicker.alreadyAdded") : undefined
      }
      submitting={assign.isPending}
      onSubmit={handleAssign}
      onClose={() => onClose()}
    />
  );
}

function KnowledgeBaseDocuments({
  knowledgeBase,
  canAssign,
}: {
  knowledgeBase: KnowledgeBaseDetail;
  canAssign: boolean;
}) {
  const confirm = useConfirm();
  // 所属文書はサーバー側でページングする（件数の上限で打ち切らない）。一覧の中を文書名で検索できる（#600）。
  // 検索語とページは作業状態として残す（workspace-state.md）。別のナレッジベースに移ったら 1 ページ目・検索なしから。
  const [documentsPage, setDocumentsPage] = useWorkspaceState(
    "knowledgeBases.documentsPage",
    { knowledgeBaseId: knowledgeBase.id, offset: 0, q: "", limit: DEFAULT_PAGE_SIZE },
    isKnowledgeBaseDocumentsPage
  );
  const sameKnowledgeBase = documentsPage.knowledgeBaseId === knowledgeBase.id;
  const offset = sameKnowledgeBase ? documentsPage.offset : 0;
  const query = sameKnowledgeBase ? documentsPage.q ?? "" : "";
  // 表示件数は利用者の選び方なので、別のナレッジベースに移っても保つ（#1891）。
  const limit = documentsPage.limit;
  const setOffset = (next: number) =>
    setDocumentsPage({ knowledgeBaseId: knowledgeBase.id, offset: next, q: query, limit });
  // 検索語が変わったら 1 ページ目へ戻す（page-archetypes.md「一覧の絞り込みの検索」5）。
  const setQuery = (next: string) =>
    setDocumentsPage({ knowledgeBaseId: knowledgeBase.id, offset: 0, q: next, limit });
  const setLimit = (next: number) =>
    setDocumentsPage({ knowledgeBaseId: knowledgeBase.id, offset: 0, q: query, limit: next });
  const documents = useDocuments({
    knowledge_base_id: knowledgeBase.id,
    q: query || undefined,
    limit,
    offset,
  });
  // 件数の「N 件（全 M 件）」の M（このナレッジベースの所属文書の全件）。検索中だけ 1 件ぶんの取得で数える（#1891）。
  const allTotal = useListTotal(
    "documents",
    { knowledge_base_id: knowledgeBase.id },
    async () => (await api.listDocuments({ knowledge_base_id: knowledgeBase.id, limit: 1, offset: 0 })).total,
    query !== ""
  );
  const remove = useRemoveDocumentFromKnowledgeBase();
  const [assigning, setAssigning] = useState(false);
  const assignButtonRef = useRef<HTMLButtonElement | null>(null);
  const page = documents.data;
  const total = page?.total ?? 0;

  const closeAssignment = () => {
    setAssigning(false);
    // 閉じたら起点の「文書を追加」へフォーカスを戻す（位置を失わない）。
    requestAnimationFrame(() => assignButtonRef.current?.focus());
  };

  const handleRemove = async (document: DocumentSummary) => {
    const ok = await confirm({
      title: t("knowledgeBases.confirm.remove.title"),
      description: t("knowledgeBases.confirm.remove.description", {
        fileName: document.file_name,
        name: knowledgeBase.name,
      }),
      confirmLabel: t("knowledgeBases.actions.remove"),
      tone: "warning",
    });
    if (!ok) return;
    remove.mutate(
      { knowledgeBaseId: knowledgeBase.id, documentId: document.id },
      {
        onSuccess: () => toast.success(t("knowledgeBases.toast.removed")),
        onError: (error) =>
          toast.error(error instanceof ApiError ? error.message : t("knowledgeBases.error.remove")),
      }
    );
  };

  return (
    <div className="space-y-3">
      {/* 一覧のツールバー: 左に検索、右に一覧への操作（page-archetypes.md「一覧のツールバー」）。 */}
      <ListToolbar
        search={
          <SearchField
            id="knowledge-base-documents-search"
            label={t("knowledgeBases.documents.search")}
            labelHidden
            placeholder={t("knowledgeBases.documents.searchPlaceholder")}
            value={query}
            onSearch={setQuery}
            clearLabel={t("common.clearSearch")}
            resultCountLabel={t("common.searchResultCount", { count: formatNumber(total) })}
          />
        }
        actions={
          canAssign ? (
            <Button
              ref={assignButtonRef}
              type="button"
              variant="secondary"
              icon={FilePlus2}
              aria-expanded={assigning}
              aria-controls={assigning ? "knowledge-base-add-documents" : undefined}
              onClick={() => (assigning ? closeAssignment() : setAssigning(true))}
              data-testid="knowledge-base-add-documents-toggle"
            >
              {t("knowledgeBases.assignment.title")}
            </Button>
          ) : undefined
        }
        count={listCountProps({
          matchedTotal: page?.total,
          filtered: query !== "",
          allTotal: allTotal.data,
        })}
        testId="knowledge-base-documents-toolbar"
      />
      {canAssign && assigning ? (
        <DocumentAssignment id="knowledge-base-add-documents" knowledgeBase={knowledgeBase} onClose={closeAssignment} />
      ) : null}
      {documents.isError ? (
        <ApiErrorState
          error={documents.error}
          fallback={t("knowledgeBases.error.documents")}
          onRetry={() => void documents.refetch()}
        />
      ) : documents.isPending ? (
        <KnowledgeBaseDocumentsSkeleton />
      ) : total > 0 ? (
        <>
          {documents.data.items.length === 0 ? (
            // 外した結果いまのページが空になったら、OffsetPagination が最後のページへ戻す。戻る間は空の案内を出さない。
            <KnowledgeBaseDocumentsSkeleton />
          ) : (
            <ul
              className="bounded-scroll-area divide-y divide-border rounded-md border border-border"
              aria-busy={documents.isPlaceholderData || undefined}
              data-testid="knowledge-base-documents-list"
            >
              {documents.data.items.map((document) => (
                <KnowledgeBaseDocumentRow
                  key={document.id}
                  document={document}
                  // アーカイブ済みは文書の追加・解除ができない（案内のとおり、「外す」も出さない）。
                  onRemove={canAssign ? () => void handleRemove(document) : undefined}
                  removing={remove.isPending && remove.variables?.documentId === document.id}
                />
              ))}
            </ul>
          )}
          <OffsetPagination
            offset={offset}
            limit={limit}
            total={total}
            count={documents.data.items.length}
            onPageChange={(_page, nextOffset) => setOffset(nextOffset)}
            onPageSizeChange={setLimit}
            labels={paginationLabels()}
            ariaLabel={t("knowledgeBases.documents.paginationLabel")}
            testId="knowledge-base-documents-pagination"
          />
        </>
      ) : query ? (
        <EmptyState
          title={t("knowledgeBases.documents.noResults.title")}
          hint={t("knowledgeBases.documents.noResults.hint")}
          action={<ClearActionButton label={t("common.clearSearch")} onClick={() => setQuery("")} />}
        />
      ) : (
        <EmptyState
          title={t("knowledgeBases.documents.empty.title")}
          hint={t("knowledgeBases.documents.empty.hint")}
        />
      )}
    </div>
  );
}

/** 所属文書 1 行。3 層モデルでは KB はスコープのみ。レシピ/チャンク構成は文書詳細で扱う。 */
function KnowledgeBaseDocumentRow({
  document,
  onRemove,
  removing,
}: {
  document: DocumentSummary;
  /** 所属から外す。外せない（アーカイブ済みの KB）ときは undefined で、行の操作を出さない。 */
  onRemove?: () => void;
  removing: boolean;
}) {
  // 文書の詳細を開けない利用者（KB の権限だけ）には、名前をリンクにしない（#303）。
  const canOpenDetail = canOpenDocumentDetail(useAuth().hasPermission);
  return (
    <li className="flex items-center gap-2 px-3 py-2">
      <Files className="size-4 shrink-0 text-fg-muted" aria-hidden />
      {canOpenDetail ? (
        <Link
          to={`${APP_ROUTES.documents}/${document.id}`}
          className="min-w-0 flex-1 truncate text-sm font-medium text-accent-fg hover:underline"
          title={document.file_name}
        >
          {document.file_name}
        </Link>
      ) : (
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-fg" title={document.file_name}>
          {document.file_name}
        </span>
      )}
      {onRemove ? (
        <RowActionMenu
          actions={[
            {
              id: "remove",
              label: t("knowledgeBases.actions.remove"),
              icon: Unlink,
              loading: removing,
              onSelect: onRemove,
            },
          ]}
          ariaLabel={t("common.objectActions.aria", { name: document.file_name })}
          loading={removing}
          testId={`knowledge-base-document-actions-${document.id}`}
        />
      ) : null}
    </li>
  );
}

function documentHasKnowledgeBase(document: DocumentSummary, knowledgeBaseId: string) {
  return document.knowledge_bases.some((knowledgeBase) => knowledgeBase.id === knowledgeBaseId);
}

function KnowledgeBaseDocumentsSkeleton() {
  return (
    <TimedLoadingState
      label={t("knowledgeBases.documents.loading")}
      operationKey="knowledge-base-documents-load"
      testId="knowledge-base-documents-loading"
    >
      <ListSkeleton rowClassName="h-11" />
    </TimedLoadingState>
  );
}

interface KnowledgeBaseDocumentsPage {
  knowledgeBaseId: string;
  offset: number;
  /** 所属文書の検索語（#600）。前の版で保存した値には無い。 */
  q?: string;
  /** 1 ページの件数（表示件数。#1891）。 */
  limit: number;
}

function isKnowledgeBaseDocumentsPage(value: unknown): value is KnowledgeBaseDocumentsPage {
  const page = value as KnowledgeBaseDocumentsPage;
  return (
    typeof page === "object" &&
    page !== null &&
    typeof page.knowledgeBaseId === "string" &&
    Number.isInteger(page.offset) &&
    page.offset >= 0 &&
    (page.q === undefined || typeof page.q === "string") &&
    isPageSize(page.limit)
  );
}
