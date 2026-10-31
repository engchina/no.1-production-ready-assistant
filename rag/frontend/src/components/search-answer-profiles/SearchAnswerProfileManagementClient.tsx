"use client";

import {
  PageBody,
  PageHeader,
  Banner,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  type DataTableColumn,
  type EntityAction,
  FieldError,
  FormActionBar,
  FormStatus,
  ObjectActionBar,
  RowActionMenu,
  SaveErrorBanner,
  SelectField,
  type SelectFieldOption,
  StatusBadge,
  TableSkeleton,
  FormSkeleton,
  TimedLoadingState,
  ToggleChip,
  Switch,
  FilterChipGroup,
  isFilterActive,
  DEFAULT_PAGE_SIZE,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  OffsetPagination,
  RowTitleButton,
  ClearActionButton,
  SearchField,
  TextField,
  ListToolbar,
  FormGrid,
  type FieldWidth,
} from "@production-ready/ui";
import { Archive, Plus, RotateCcw, Save, Sparkles } from "lucide-react";
import { useMemo, useRef, useState, type FormEvent } from "react";

import { DegradedBanner } from "@/components/DegradedBanner";
import { ContactGuidanceFieldset } from "@/components/search-answer-profiles/ContactGuidanceFieldset";
import { EmptyState, ApiErrorState } from "@/components/StateViews";
import {
  KnowledgeBaseScopePicker,
  useKnowledgeBaseSelectionHealth,
} from "@/components/knowledge-bases/KnowledgeBaseScopePicker";
import {
  EditorDraftNotice,
  EditorTargetState,
} from "@/components/layout/EntityLayout";
import {
  readEditorDraft,
  useEntityEditorDraft,
} from "@/components/layout/use-entity-editor-draft";
import { useAuth } from "@/components/security/AuthProvider";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  api,
  ApiError,
  DEFAULT_SEARCH_ANSWER_PROFILE_NAME,
  type SearchAnswerProfileConfig,
  type SearchAnswerProfileDetail,
  type SearchAnswerProfileStatus,
  type SearchAnswerProfileSummary,
  type KnowledgeBaseQueryConfig,
} from "@/lib/api";
import { useEditorRoute } from "@/lib/editor-route";
import type { KnowledgeBaseSelectionHealth } from "@/lib/knowledge-base-refs";
import { formatDateTime, formatNumber } from "@/lib/format";
import { confirmPendingLeave } from "@/lib/leave-guard";
import { t } from "@/lib/i18n";
import { isPageSize, listCountProps } from "@/lib/list-paging";
import { listToolbarLabels, paginationLabels } from "@/lib/pagination-labels";
import { CAPABILITY_PERMISSIONS } from "@/lib/permissions";
import {
  firstInvalidFieldId,
  focusFirstInvalidField,
  requiredTextError,
} from "@/lib/required-fields";
import {
  useArchiveSearchAnswerProfile,
  useSearchAnswerProfile,
  useSearchAnswerProfiles,
  useProfileAnswerModels,
  useListTotal,
  useCreateSearchAnswerProfile,
  useUpdateSearchAnswerProfile,
} from "@/lib/queries";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { useWorkspaceState } from "@/lib/workspace-state";
import { SearchAnswerProfileKnowledgePanel } from "./SearchAnswerProfileKnowledgePanel";
import { LanguagePackOverridesFieldset } from "@/components/search-answer-profiles/LanguagePackOverridesFieldset";


/**
 * 一覧の絞り込み・検索・ページ。編集対象は URL の `?id=` が唯一の情報源なので、ここには持たない（#147）。
 * #132 の保存値に残る `editingId` は読み捨てる。
 */
interface SearchAnswerProfileListView {
  filter: SearchAnswerProfileStatus | "ALL";
  q: string;
  offset: number;
  /** 1 ページの件数（表示件数。#1891）。 */
  limit: number;
}
const INITIAL_VIEW: SearchAnswerProfileListView = { filter: "ACTIVE", q: "", offset: 0, limit: DEFAULT_PAGE_SIZE };

function isSearchAnswerProfileListView(value: unknown): value is SearchAnswerProfileListView {
  const view = value as SearchAnswerProfileListView;
  return (
    typeof view === "object" &&
    view !== null &&
    (FILTERS as unknown[]).includes(view.filter) &&
    typeof view.q === "string" &&
    Number.isInteger(view.offset) &&
    view.offset >= 0 &&
    isPageSize(view.limit)
  );
}

interface SearchAnswerProfileDraft {
  name: string;
  description: string;
  config: SearchAnswerProfileConfig;
}

function isSearchAnswerProfileDraft(value: unknown): value is SearchAnswerProfileDraft {
  const draft = value as SearchAnswerProfileDraft;
  return (
    typeof draft === "object" &&
    draft !== null &&
    typeof draft.name === "string" &&
    typeof draft.description === "string" &&
    typeof draft.config === "object" &&
    draft.config !== null &&
    Array.isArray(draft.config.knowledge_base_ids) &&
    typeof draft.config.query === "object"
  );
}

/** KB の並び順は意味を持たないため、集合として比べる。 */
function draftSignature(draft: SearchAnswerProfileDraft) {
  return JSON.stringify({
    ...draft,
    name: draft.name.trim(),
    description: draft.description.trim(),
    config: {
      ...draft.config,
      knowledge_base_ids: [...draft.config.knowledge_base_ids].sort(),
    },
  });
}
const FILTERS: (SearchAnswerProfileStatus | "ALL")[] = ["ALL", "ACTIVE", "ARCHIVED"];
const SCOPE_ERROR_ID = "search-answer-profile-scope-error";
// API（SearchAnswerProfileCreateRequest / UpdateRequest）の上限。超えると 422 の英語の検証メッセージに
// なるため入力で止める。
const NAME_MAX_LENGTH = 256;
const DESCRIPTION_MAX_LENGTH = 2000;

// 0〜20(backend の rag_max_anchors_per_document と同じ範囲。0 は上限なし。#2167)。
const MAX_ANCHORS_PER_DOCUMENT_OPTIONS: SelectFieldOption<string>[] = Array.from({ length: 21 }, (_, n) => ({
  value: String(n),
  label: n === 0 ? t("searchAnswerProfiles.field.maxAnchorsPerDocumentUnlimited") : String(n),
}));
/**
 * 全体設定だけで決める検索方法の項目（backend の PROFILE_GLOBAL_QUERY_FIELDS と同じ。#3053・#3523）。
 * 検索・回答プロファイルでは表示も保存もせず、古い保存値は読み込みで捨てる。
 */
const PROFILE_GLOBAL_QUERY_FIELDS = [
  "query_strategy", "answer_flow", "neighbor_child_count", "rerank_enabled", "auto_field_filter_enabled",
  "graph_expansion_enabled", "graph_expansion_max_chunks", "screen_linking_enabled", "request_coverage_retrieval_enabled",
  "guide_step_retrieval_enabled", "guide_step_max_queries", "guide_step_deadline_seconds",
  "entity_expansion_enabled", "entity_expansion_max_chunks",
  "section_expansion_enabled", "section_expansion_max_levels", "section_expansion_input_token_percent",
] as const satisfies readonly (keyof KnowledgeBaseQueryConfig)[];

function emptyQueryConfig(): KnowledgeBaseQueryConfig {
  return {};
}

/** 旧保存 JSON に無い項目は null(継承)で補う。 */
function normalizeSearchAnswerProfileConfig(config: SearchAnswerProfileConfig): SearchAnswerProfileConfig {
  const retained = { ...config };
  delete (retained as Record<string, unknown>).spec_then_contact;
  const query = { ...config.query };
  for (const key of PROFILE_GLOBAL_QUERY_FIELDS) delete query[key];
  return {
    ...retained,
    query,
    clarification_policy: config.clarification_policy ?? "confirm",
    language_pack: config.language_pack ?? {},
    contact_override: config.contact_override ?? "",
  };
}

/** 回答前の確認の方針（#1839）。 */
const CLARIFICATION_POLICY_OPTIONS: SelectFieldOption<"confirm" | "assume">[] = [
  { value: "confirm", label: t("searchAnswerProfiles.clarification.policy.confirm") },
  { value: "assume", label: t("searchAnswerProfiles.clarification.policy.assume") },
];

function emptyConfig(): SearchAnswerProfileConfig {
  return {
    version: 1,
    knowledge_base_ids: [],
    query: emptyQueryConfig(),
    clarification_policy: "confirm",
    language_pack: {},
    contact_override: "",
  };
}


/**
 * 検索・回答プロファイル(Search Answer Profile)管理。複数 KB を業務視点で束ね、回答の設定と安全チェックを上書きする。
 * A 型（一覧 → 全画面エディタ）。編集対象は URL の `?id=`（なし = 一覧 / `new` = 新規 / `<id>` = 編集）。
 */
export function SearchAnswerProfileManagementClient() {
  const editor = useEditorRoute();
  const { target } = editor;
  const canManage = useCanManageSearchAnswerProfiles();

  // 作成は検索・回答プロファイル管理の権限がある利用者だけ（`?id=new` を直接開いても一覧を出す。#214）。
  if (target.kind === "list" || (target.kind === "new" && !canManage)) {
    return (
      <SearchAnswerProfileList
        onOpen={(id) => editor.openItem(id)}
        itemHref={editor.itemHref}
        onCreate={canManage ? editor.openNew : undefined}
      />
    );
  }
  if (target.kind === "new") {
    return (
      <SearchAnswerProfileEditor
        key="new"
        onBack={() => editor.backToList()}
        onSaved={(id) => editor.openItem(id, { replace: true })}
        onArchived={() => editor.backToList({ replace: true })}
      />
    );
  }
  return (
    <SearchAnswerProfileEditRoute
      key={target.id}
      id={target.id}
      onBack={() => editor.backToList()}
      onArchived={() => editor.backToList({ replace: true })}
    />
  );
}

/**
 * 検索・回答プロファイル 1 件の操作。一覧の行（RowActionMenu）とエディタ（ObjectActionBar）で同じ定義を使う
 * （buttons.md §5.1）。編集は選択の導線なので、名前のボタンと行のクリックに置く。
 */
/** 検索・回答プロファイルの作成・アーカイブは `rag.search_answer_profiles.manage` を持つ利用者だけ（#214）。 */
function useCanManageSearchAnswerProfiles(): boolean {
  return useAuth().hasPermission(CAPABILITY_PERMISSIONS.searchAnswerProfilesManage);
}

function useSearchAnswerProfileActions(onArchived?: (id: string) => void) {
  const confirm = useConfirm();
  const archive = useArchiveSearchAnswerProfile();
  const canManage = useCanManageSearchAnswerProfiles();

  const handleArchive = async (view: SearchAnswerProfileSummary) => {
    const ok = await confirm({
      title: t("searchAnswerProfiles.confirm.archive.title"),
      description: t("searchAnswerProfiles.confirm.archive.description", { name: view.name }),
      confirmLabel: t("searchAnswerProfiles.actions.archive"),
      tone: "danger",
      dismissOnOverlay: false,
    });
    if (!ok) return;
    archive.mutate(view.id, {
      onSuccess: () => {
        toast.success(t("searchAnswerProfiles.toast.archived"));
        onArchived?.(view.id);
      },
      onError: (error) =>
        toast.error(error instanceof ApiError ? error.message : t("searchAnswerProfiles.error.archive")),
    });
  };

  return (target: SearchAnswerProfileSummary): EntityAction[] => {
    const isDefault = target.name === DEFAULT_SEARCH_ANSWER_PROFILE_NAME;
    return [
      {
        id: "archive",
        label: t("searchAnswerProfiles.actions.archive"),
        ariaLabel: isDefault ? t("searchAnswerProfiles.default.archiveDisabled") : undefined,
        icon: Archive,
        tone: "danger",
        visible: canManage && target.status !== "ARCHIVED",
        disabled: isDefault || archive.isPending,
        loading: archive.isPending && archive.variables === target.id,
        testId: `search-answer-profile-archive-${target.id}`,
        onSelect: () => handleArchive(target),
      },
    ];
  };
}

function SearchAnswerProfileList({
  onOpen,
  itemHref,
  onCreate,
}: {
  onOpen: (id: string) => void;
  /** 検索・回答プロファイルのエディタの URL（名前のリンク。新しいタブで開ける。#583）。 */
  itemHref: (id: string) => string;
  /** 作成できない利用者（検索・回答プロファイル管理の権限なし）では undefined。 */
  onCreate?: () => void;
}) {
  // 絞り込み・検索・ページは、ページを行き来しても再読込しても残す（workspace-state.md）。
  const [view, setView] = useWorkspaceState("searchAnswerProfiles.view", INITIAL_VIEW, isSearchAnswerProfileListView);
  const { filter, q, offset, limit } = view;
  const setFilter = (next: SearchAnswerProfileStatus | "ALL") =>
    setView((current) => ({ ...current, filter: next, offset: 0 }));
  const setQ = (next: string) => setView((current) => ({ ...current, q: next, offset: 0 }));
  const setOffset = (next: number) => setView((current) => ({ ...current, offset: next }));

  const status = filter === "ALL" ? undefined : filter;
  const query = useSearchAnswerProfiles({ status, q: q || undefined, limit, offset });
  // 件数の「N 件（全 M 件）」の M（状態・検索語を外した全件）。絞り込み中だけ 1 件ぶんの取得で数える（#1891）。
  const filtered = q !== "" || filter !== "ALL";
  const allTotal = useListTotal(
    "search-answer-profiles",
    {},
    async () => (await api.listSearchAnswerProfiles({ limit: 1, offset: 0 })).total,
    filtered
  );
  const page = query.data;
  const items = useMemo(() => page?.items ?? [], [page?.items]);
  const actionsFor = useSearchAnswerProfileActions();
  // 参照 KB がアーカイブ・削除されて検索対象から外れている検索・回答プロファイル（#302）。
  const hasKnowledgeBaseIssues = items.some(
    (item) => item.status !== "ARCHIVED" && knowledgeBaseIssueCount(item) > 0
  );
  // 新規作成の下書きはエディタを閉じても同じタブに残る。一覧から再開できるようにする。
  const [newDraft] = useState(() => readEditorDraft("searchAnswerProfiles.draft", "new", isSearchAnswerProfileDraft));

  return (
    <div>
      <PageHeader
        wide
        title={t("nav.searchAnswerProfiles")}
        subtitle={t("searchAnswerProfiles.subtitle")}
        actions={
          onCreate
            ? [
                {
                  id: "create",
                  kind: "primary",
                  label: t("searchAnswerProfiles.actions.newView"),
                  icon: Plus,
                  onClick: onCreate,
                },
              ]
            : []
        }
      />
      <PageBody wide className="grid grid-cols-1 gap-5">
        <DegradedBanner
          messages={page?.warning_messages}
          onRetry={() => void query.refetch()}
          isRetrying={query.isFetching}
        />

        {hasKnowledgeBaseIssues ? (
          <Banner
            severity="warning"
            title={t("searchAnswerProfiles.knowledgeBaseIssues.listTitle")}
          >
            <p>{t("searchAnswerProfiles.knowledgeBaseIssues.listHint")}</p>
          </Banner>
        ) : null}

        {newDraft && onCreate ? (
          <EditorDraftNotice message={t("searchAnswerProfiles.draftPending")} onOpen={onCreate} />
        ) : null}

        {/* 一覧のツールバー: 左に検索、その右に状態の絞り込み（page-archetypes.md「一覧のツールバー」。#600）。
            一覧の絞り込みは入力に合わせて適用する（検索ボタンを置かない。#535）。 */}
        <ListToolbar
          search={
            <SearchField
              id="search-answer-profile-search"
              label={t("searchAnswerProfiles.search.placeholder")}
              labelHidden
              value={q}
              onSearch={(next) => {
                if (next !== q) setQ(next);
              }}
              clearLabel={t("common.clearSearch")}
              resultCountLabel={
                page ? t("common.searchResultCount", { count: formatNumber(page.total) }) : ""
              }
              placeholder={t("searchAnswerProfiles.search.placeholder")}
            />
          }
          filters={
            <FilterChipGroup
              label={t("searchAnswerProfiles.filter.aria")}
              value={filter}
              options={FILTERS.map((item) => ({
                value: item,
                label:
                  item === "ALL"
                    ? t("searchAnswerProfiles.filter.all")
                    : item === "ACTIVE"
                      ? t("searchAnswerProfiles.filter.active")
                      : t("searchAnswerProfiles.filter.archived"),
              }))}
              onValueChange={setFilter}
            />
          }
          activeFilterCount={isFilterActive(filter, "ALL") ? 1 : 0}
          searchActive={q !== ""}
          onClearFilters={() => setView((current) => ({ ...current, q: "", filter: "ALL", offset: 0 }))}
          labels={listToolbarLabels()}
          count={listCountProps({
            matchedTotal: page?.total,
            filtered,
            allTotal: allTotal.data,
          })}
          testId="search-answer-profile-list-toolbar"
        />

        {query.isError ? (
          <ApiErrorState
            error={query.error}
            fallback={t("searchAnswerProfiles.error.title")}
            onRetry={() => void query.refetch()}
          />
        ) : query.isPending ? (
          <TimedLoadingState
            label={t("searchAnswerProfiles.loading")}
            operationKey="search-answer-profiles-load"
            testId="search-answer-profiles-loading"
          >
            <TableSkeleton columns={5} />
          </TimedLoadingState>
        ) : (page?.total ?? 0) === 0 && !query.isFetching ? (
          <Card>
            {q ? (
              <EmptyState
                title={t("searchAnswerProfiles.search.noResultsTitle")}
                hint={t("searchAnswerProfiles.search.noResultsHint")}
                action={
                  <ClearActionButton
                    label={t("common.clearSearch")}
                    onClick={() => setQ("")}
                  />
                }
              />
            ) : (
              <EmptyState
                title={t("searchAnswerProfiles.empty.title")}
                hint={
                  onCreate
                    ? t("searchAnswerProfiles.empty.description")
                    : t("searchAnswerProfiles.empty.restrictedDescription")
                }
                // 空の一覧から次の行動へ進めるよう、作成の入口を空の状態にも置く（ヘッダーの「新規作成」と同じ。#555）。
                action={
                  onCreate ? (
                    <Button variant="secondary" icon={Plus} onClick={onCreate}>
                      {t("searchAnswerProfiles.actions.createFirst")}
                    </Button>
                  ) : undefined
                }
              />
            )}
          </Card>
        ) : (
          <div className="grid gap-2">
            <DataTable<SearchAnswerProfileSummary>
              columns={searchAnswerProfileColumns({ onOpen, itemHref, actionsFor })}
              rows={items}
              // アーカイブなどで件数が減り、保存したページが範囲外になったら OffsetPagination が最後のページへ戻す
              // （ナレッジベースの一覧と同じ）。戻る間は「検索・回答プロファイルがありません」を出さず、
              // 表の形の読み込み中にする。
              loading={items.length === 0}
              getRowKey={(item) => item.id}
              // 行の操作以外の領域のクリックでエディタを開く（page-archetypes.md §0-7）。
              // アーカイブ済みは編集できないため開かない。
              onRowClick={(item) => {
                if (item.status !== "ARCHIVED") onOpen(item.id);
              }}
              rowProps={(item) => ({
                className: cn(
                  INFORMATION_TABLE_ROW_CLASS,
                  "align-top",
                  item.status === "ARCHIVED" && "cursor-default"
                ),
                "data-testid": `search-answer-profile-row-${item.id}`,
              })}
              stickyHeader
              visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
              growToViewport
              scrollAriaLabel={t("searchAnswerProfiles.list.scrollLabel")}
              scrollTestId="search-answer-profiles-scroll-region"
              tableClassName="w-full min-w-[51.43rem] text-sm"
              ariaLabel={t("searchAnswerProfiles.list.aria")}
            />
            <OffsetPagination
              offset={offset}
              limit={limit}
              total={page?.total ?? 0}
              count={items.length}
              onPageChange={(_page, nextOffset) => setOffset(nextOffset)}
              onPageSizeChange={(nextLimit) => setView((current) => ({ ...current, limit: nextLimit, offset: 0 }))}
              labels={paginationLabels()}
              ariaLabel={t("searchAnswerProfiles.paginationLabel")}
              testId="search-answer-profiles-pagination"
            />
          </div>
        )}
      </PageBody>
    </div>
  );
}

/** 一覧の列定義。名前列を行見出しにし、操作列は右寄せにする。 */
function searchAnswerProfileColumns({
  onOpen,
  itemHref,
  actionsFor,
}: {
  onOpen: (id: string) => void;
  itemHref: (id: string) => string;
  actionsFor: (view: SearchAnswerProfileSummary) => EntityAction[];
}): DataTableColumn<SearchAnswerProfileSummary>[] {
  return [
    {
      key: "name",
      header: t("searchAnswerProfiles.col.name"),
      rowHeader: true,
      className: "max-w-[22rem]",
      render: (view) =>
        view.status === "ARCHIVED" ? (
          <div className="min-w-0">
            <p className="break-words font-medium text-fg-muted">{view.name}</p>
            {view.description ? (
              <p className="mt-0.5 line-clamp-2 text-xs text-fg-muted">{view.description}</p>
            ) : null}
          </div>
        ) : (
          <RowTitleButton
            title={view.name}
            subtitle={view.description ?? undefined}
            aria-label={t("searchAnswerProfiles.actions.editNamed", { name: view.name })}
            href={itemHref(view.id)}
            onClick={() => onOpen(view.id)}
          />
        ),
    },
    {
      key: "status",
      header: t("searchAnswerProfiles.col.status"),
      render: (view) => (
        <StatusBadge
          variant={view.status === "ARCHIVED" ? "neutral" : "success"}
          label={t(`searchAnswerProfiles.status.${view.status}` as const)}
        />
      ),
    },
    {
      key: "knowledgeBases",
      header: t("searchAnswerProfiles.col.knowledgeBases"),
      align: "right",
      className: "tnum text-fg-muted",
      render: (view) => {
        const archived = view.archived_knowledge_base_count ?? 0;
        const missing = view.missing_knowledge_base_count ?? 0;
        // アーカイブ済みの検索・回答プロファイルは検索に使われないため、参照 KB の警告は出さない。
        if (view.status === "ARCHIVED" || archived + missing === 0) {
          return formatNumber(view.knowledge_base_count);
        }
        return (
          <span
            className="inline-flex items-center justify-end gap-2"
            data-testid={`search-answer-profile-kb-issues-${view.id}`}
          >
            <StatusBadge variant="warning" label={t("searchAnswerProfiles.knowledgeBaseIssues.badge")} />
            <span aria-hidden>{formatNumber(view.knowledge_base_count)}</span>
            <span className="sr-only">
              {t("searchAnswerProfiles.knowledgeBaseIssues.badgeAria", {
                total: view.knowledge_base_count,
                archived,
                missing,
              })}
            </span>
          </span>
        );
      },
    },
    {
      key: "updated",
      header: t("searchAnswerProfiles.col.updated"),
      className: "tnum whitespace-nowrap text-fg-muted",
      render: (view) => formatDateTime(view.updated_at),
    },
    {
      key: "actions",
      header: t("searchAnswerProfiles.col.actions"),
      align: "right",
      render: (view) => (
        <RowActionMenu
          actions={actionsFor(view)}
          ariaLabel={t("common.objectActions.aria", { name: view.name })}
          testId={`search-answer-profile-row-actions-${view.id}`}
        />
      ),
    },
  ];
}

/** `?id=<id>` の対象を読み込んでエディタを出す。見つからないときは一覧へ戻る導線を出す（別の対象へ置き換えない）。 */
function SearchAnswerProfileEditRoute({
  id,
  onBack,
  onArchived,
}: {
  id: string;
  onBack: () => void;
  onArchived: () => void;
}) {
  const detail = useSearchAnswerProfile(id);

  if (detail.data) {
    return (
      <SearchAnswerProfileEditor
        initial={detail.data}
        onBack={onBack}
        onSaved={() => undefined}
        onArchived={onArchived}
      />
    );
  }

  return (
    <EditorTargetState
      id={id}
      listLabel={t("nav.searchAnswerProfiles")}
      error={detail.error}
      loadingLabel={t("searchAnswerProfiles.detail.loading")}
      loadingTestId="search-answer-profile-detail-loading"
      errorFallback={t("searchAnswerProfiles.error.title")}
      skeleton={<FormSkeleton fields={6} />}
      onBack={onBack}
      onRetry={() => void detail.refetch()}
    />
  );
}

/** 検索・回答プロファイルの全画面エディタ（新規 / 編集）。上部に 戻る / 保存、エディタの見出しに対象の操作を置く。 */
function SearchAnswerProfileEditor({
  initial,
  onBack,
  onSaved,
  onArchived,
}: {
  initial?: SearchAnswerProfileDetail;
  onBack: () => void;
  onSaved: (id: string) => void;
  onArchived: () => void;
}) {
  const mode: "create" | "edit" = initial ? "edit" : "create";
  const answerModels = useProfileAnswerModels();
  const registeredModels = Array.isArray(answerModels.data) ? answerModels.data : [];
  const create = useCreateSearchAnswerProfile();
  const update = useUpdateSearchAnswerProfile();
  const actionsFor = useSearchAnswerProfileActions(onArchived);
  const formRef = useRef<HTMLFormElement>(null);
  // 未保存の下書き（`?id=` の値ごと）と離脱の確認は、ナレッジベースのエディタと共有の部品で持つ（#555）。
  const editor = useEntityEditorDraft<SearchAnswerProfileDraft>({
    field: "searchAnswerProfiles.draft",
    scope: initial?.id ?? "new",
    initial: {
      name: initial?.name ?? "",
      description: initial?.description ?? "",
      config: initial?.config ? normalizeSearchAnswerProfileConfig(initial.config) : emptyConfig(),
    },
    isDraft: isSearchAnswerProfileDraft,
    signature: draftSignature,
    leaveDescription: t("searchAnswerProfiles.leaveGuard.description"),
  });
  const { draft, setDraft, dirty } = editor;
  const { name, description, config } = draft;
  const setName = (value: string) => setDraft((current) => ({ ...current, name: value }));
  const setDescription = (value: string) =>
    setDraft((current) => ({ ...current, description: value }));
  const setConfig = (next: SearchAnswerProfileConfig | ((current: SearchAnswerProfileConfig) => SearchAnswerProfileConfig)) =>
    setDraft((current) => ({
      ...current,
      config: typeof next === "function" ? next(current.config) : next,
    }));
  const [touched, setTouched] = useState(false);
  // 説明はフォーカスが外れたとき・送信したときから検証結果を出す（messaging.md §3.2。#521）。
  const [descriptionTouched, setDescriptionTouched] = useState(false);

  // 左上の「一覧へ戻る」は、エディタのフォームだけでなくページのほかの未保存（知識の節・構築設定など）も
  // リンク・戻るボタンと同じく 1 回だけ確認する（#586 / #618）。
  const back = async () => {
    if (await confirmPendingLeave()) onBack();
  };

  const discard = () => {
    editor.discard();
    setTouched(false);
    setDescriptionTouched(false);
  };

  const isDefault = initial?.name === DEFAULT_SEARCH_ANSWER_PROFILE_NAME;
  const isArchived = initial?.status === "ARCHIVED";
  // 選択中（下書きを含む）の参照 KB のうち、アーカイブ済み・見つからないもの（#302）。
  // 範囲外の KB も名前で出せるよう、詳細が返す tenant 内の参照を併せて使う。
  const knowledgeBaseHealth = useKnowledgeBaseSelectionHealth(config.knowledge_base_ids, {
    known: initial?.knowledge_bases,
    knownMissingIds: initial?.missing_knowledge_base_ids,
  });

  const pending = create.isPending || update.isPending;
  // 保存の失敗は欄に結び付かないため、確定の行の直上の 1 か所だけに出す（messaging.md §3.3.1。#585）。
  const saveMutation = mode === "edit" ? update : create;
  const saveError = saveMutation.isError
    ? saveMutation.error instanceof ApiError
      ? saveMutation.error.message
      : t(mode === "edit" ? "searchAnswerProfiles.error.update" : "searchAnswerProfiles.error.create")
    : null;
  // アーカイブ済みは保存できないので、入力できないようにする（入力しても保存できない欄を出さない。#555）。
  const locked = pending || isArchived;
  const nameError = touched && !isDefault ? validateSearchAnswerProfileName(name) : null;
  // 説明は必須（#521）。説明が空の既存の検索・回答プロファイル（DEFAULT を含む）は、保存するときに入力を求める。
  const descriptionError = descriptionTouched ? validateSearchAnswerProfileDescription(description) : null;
  const scopeError =
    touched && config.knowledge_base_ids.length === 0
      ? t("searchAnswerProfiles.knowledgeBasesRequired")
      : null;

  const updateQuery = (patch: Partial<KnowledgeBaseQueryConfig>) =>
    setConfig((current) => ({ ...current, query: { ...current.query, ...patch } }));

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isArchived) return;
    setTouched(true);
    setDescriptionTouched(true);
    const fieldErrors = [
      ["search-answer-profile-name", validateSearchAnswerProfileName(name, isDefault)],
      ["search-answer-profile-description", validateSearchAnswerProfileDescription(description)],
    ] as const;
    if (firstInvalidFieldId(fieldErrors)) {
      focusFirstInvalidField(fieldErrors);
      return;
    }
    if (config.knowledge_base_ids.length === 0) return;
    if (mode === "edit" && initial) {
      update.mutate(
        {
          id: initial.id,
          payload: {
            ...(!isDefault ? { name: name.trim() } : {}),
            description: description.trim(),
            config,
          },
        },
        {
          onSuccess: (detail) => {
            // 保存した値を基準にして dirty を判定し直す（下書きも消える）。
            editor.markSaved(draft);
            toast.success(t("searchAnswerProfiles.toast.updated"));
            onSaved(detail.id);
          },
        }
      );
      return;
    }
    create.mutate(
      { name: name.trim(), description: description.trim(), config },
      {
        onSuccess: (detail) => {
          editor.markSaved(draft);
          toast.success(t("searchAnswerProfiles.toast.created"));
          // 作成した対象のエディタへ履歴を積まずに移る（戻るで空の新規フォームへ戻さない）。
          onSaved(detail.id);
        },
      }
    );
  };

  const title = initial?.name ?? t("searchAnswerProfiles.create.title");

  return (
    <div>
      <PageHeader
        wide
        title={title}
        status={
          initial ? (
            <StatusBadge
              variant={initial.status === "ARCHIVED" ? "neutral" : "success"}
              label={t(`searchAnswerProfiles.status.${initial.status}` as const)}
            />
          ) : undefined
        }
        subtitle={initial ? initial.description || t("searchAnswerProfiles.subtitle") : t("searchAnswerProfiles.subtitle")}
        meta={
          initial ? (
            <span className="tnum flex flex-wrap gap-x-3 gap-y-1" data-testid="search-answer-profile-meta">
              <span>
                {t("searchAnswerProfiles.meta.knowledgeBases", {
                  count: formatNumber(initial.knowledge_base_count),
                })}
              </span>
              <span>{t("editor.meta.updated", { date: formatDateTime(initial.updated_at) })}</span>
            </span>
          ) : undefined
        }
        // 一覧へ戻るは左上（#618）。確定（作成・保存と変更を破棄）はフォームの末尾の FormActionBar（#1786）。
        back={{
          label: t("common.backToList"),
          ariaLabel: t("editor.backToListOf", { list: t("nav.searchAnswerProfiles") }),
          onClick: () => void back(),
          testId: "editor-back",
        }}
      />
      <PageBody wide className="grid grid-cols-1 gap-5">
        {isArchived ? (
          <Banner severity="warning">{t("searchAnswerProfiles.archivedReadonly")}</Banner>
        ) : (
          <KnowledgeBaseIssuesBanner
            health={knowledgeBaseHealth}
            selectedCount={config.knowledge_base_ids.length}
          />
        )}
        <Card>
          <CardHeader className="flex-row flex-wrap items-start justify-between gap-3">
            <CardTitle className="flex items-center gap-2">
              <Sparkles size={20} className="text-accent-fg" aria-hidden />
              {t("searchAnswerProfiles.form.title")}
            </CardTitle>
            {initial ? (
              <ObjectActionBar
                actions={actionsFor(initial)}
                ariaLabel={t("common.objectActions.aria", { name: initial.name })}
                moreLabel={t("common.objectActions.more")}
                testId="search-answer-profile-detail-actions"
              />
            ) : null}
          </CardHeader>
          <CardContent>
            <form ref={formRef} onSubmit={handleSubmit} className="space-y-5">
              {editor.restored ? (
                <FormStatus tone="info" message={t("editor.draftRestored")} />
              ) : null}
              {/* 名前は名前の幅（md）、説明は文章（全幅）。フォームの読みやすい最大幅（FormGrid）で止める（#1891）。 */}
              <FormGrid columns={1}>
                <TextField
                  id="search-answer-profile-name"
                  width="md"
                  label={t("searchAnswerProfiles.field.name")}
                  required={!isDefault && !isArchived}
                  value={name}
                  onValueChange={setName}
                  onBlur={() => setTouched(true)}
                  readOnly={isDefault || isArchived}
                  aria-readonly={isDefault || isArchived || undefined}
                  placeholder={isArchived ? undefined : t("searchAnswerProfiles.field.namePlaceholder")}
                  helper={isDefault ? t("searchAnswerProfiles.default.nameFixed") : undefined}
                  error={nameError || undefined}
                  maxLength={NAME_MAX_LENGTH}
                  inputClassName={isDefault || isArchived ? "cursor-default text-fg-muted" : undefined}
                />
                <TextField
                  id="search-answer-profile-description"
                  width="full"
                  label={t("searchAnswerProfiles.field.description")}
                  required={!isArchived}
                  value={description}
                  onValueChange={setDescription}
                  onBlur={() => setDescriptionTouched(true)}
                  readOnly={isArchived}
                  aria-readonly={isArchived || undefined}
                  placeholder={isArchived ? undefined : t("searchAnswerProfiles.field.descriptionPlaceholder")}
                  helper={t("searchAnswerProfiles.field.descriptionHelper")}
                  error={descriptionError || undefined}
                  maxLength={DESCRIPTION_MAX_LENGTH}
                  inputClassName={isArchived ? "cursor-default text-fg-muted" : undefined}
                />
              </FormGrid>

              <div>
                <KnowledgeBaseScopePicker
                  knownKnowledgeBases={initial?.knowledge_bases}
                  knownMissingIds={initial?.missing_knowledge_base_ids}
                  selectedIds={config.knowledge_base_ids}
                  onChange={(ids) => setConfig((current) => ({ ...current, knowledge_base_ids: ids }))}
                  disabled={locked || isDefault}
                  // 対象の選択（ナレッジベース）は lg（#1891）。
                  width="lg"
                  label={t("searchAnswerProfiles.field.knowledgeBases")}
                  helper={
                    isDefault
                      ? t("searchAnswerProfiles.default.knowledgeBaseFixed")
                      : t("searchAnswerProfiles.field.knowledgeBasesHelper")
                  }
                  emptySelectionText={t("searchAnswerProfiles.knowledgeBasesRequired")}
                  required={!isDefault}
                  errorId={scopeError ? SCOPE_ERROR_ID : undefined}
                />
                <FieldError id={SCOPE_ERROR_ID} message={scopeError} className="mt-1" />
              </div>

              <fieldset className="space-y-3 rounded-lg border border-border p-4">
                <legend className="px-1 text-sm font-semibold text-fg">
                  {t("searchAnswerProfiles.query.title")}
                </legend>
                <p className="text-xs text-fg-muted">{t("searchAnswerProfiles.query.helper")}</p>
                <div className="space-y-3">
                  <QuerySelectRow
                    id="search-answer-profile-max-anchors-per-document"
                    label={t("searchAnswerProfiles.field.maxAnchorsPerDocument")}
                    helper={t("searchAnswerProfiles.field.maxAnchorsPerDocumentHelper")}
                    value={
                      config.query.max_anchors_per_document == null
                        ? null
                        : String(config.query.max_anchors_per_document)
                    }
                    options={MAX_ANCHORS_PER_DOCUMENT_OPTIONS}
                    width="sm"
                    defaultOnOverride="5"
                    disabled={locked}
                    onChange={(value) =>
                      updateQuery({
                        max_anchors_per_document: value === null ? null : Number(value),
                      })
                    }
                  />
                  <QuerySelectRow
                    id="search-answer-profile-answer-model"
                    width="md"
                    label={t("searchAnswerProfiles.field.answerModel")}
                    helper={answerModels.isError ? t("searchAnswerProfiles.field.answerModelLoadFailed") : t("searchAnswerProfiles.field.answerModelHelper")}
                    value={config.answer_model_id ?? null}
                    options={[
                      ...registeredModels.map((model) => ({ value: model.model_id, label: model.display_name })),
                      ...(config.answer_model_id && !registeredModels.some((model) => model.model_id === config.answer_model_id)
                        ? [{ value: config.answer_model_id, label: t("searchAnswerProfiles.field.unavailableModel", { model: config.answer_model_id }) }]
                        : []),
                    ]}
                    defaultOnOverride={registeredModels[0]?.model_id ?? ""}
                    disabled={locked || (!config.answer_model_id && !registeredModels.length)}
                    onChange={(value) => setConfig((current) => ({ ...current, answer_model_id: value || null }))}
                  />
                  <div
                    className="flex items-start justify-between gap-4 rounded-lg border border-border p-3"
                    data-testid="search-answer-profile-guide-matching"
                  >
                    <div className="min-w-0 space-y-1">
                      <h3 className="text-sm font-medium text-fg">
                        {t("searchAnswerProfiles.field.guideEnabled")}
                      </h3>
                      <p id="search-answer-profile-guide-enabled-helper" className="text-xs leading-relaxed text-fg-muted">
                        {t("searchAnswerProfiles.field.guideEnabledHelper")}
                      </p>
                      <p className="text-sm font-medium text-fg" aria-live="polite">
                        {config.query.support_guide_enabled === true ? t("searchAnswerProfiles.field.guideEnabledOn") : t("searchAnswerProfiles.field.guideEnabledOff")}
                      </p>
                    </div>
                    <Switch
                      aria-label={t("searchAnswerProfiles.field.guideEnabled")}
                      aria-describedby="search-answer-profile-guide-enabled-helper"
                      checked={config.query.support_guide_enabled === true}
                      disabled={locked}
                      onCheckedChange={(value) => updateQuery({ support_guide_enabled: value })}
                      className="shrink-0"
                    />
                  </div>
                </div>
              </fieldset>
              {/* 回答前の確認（#1839。3 製品で業務の単位に置く設定）。ルールごとの確認は下の「プロファイルの知識」。 */}
              <fieldset
                className="space-y-3 rounded-lg border border-border p-4"
                data-testid="search-answer-profile-clarification"
              >
                <legend className="px-1 text-sm font-semibold text-fg">
                  {t("searchAnswerProfiles.clarification.title")}
                </legend>
                <p className="text-xs text-fg-muted">{t("searchAnswerProfiles.clarification.helper")}</p>
                <div className="grid gap-3 rounded-lg border border-border bg-surface-sunken p-3 md:grid-cols-[minmax(10rem,14rem)_minmax(0,1fr)]">
                  <h3 className="text-sm font-medium text-fg">{t("searchAnswerProfiles.clarification.policy")}</h3>
                  <SelectField
                    id="search-answer-profile-clarification-policy"
                    label={t("searchAnswerProfiles.clarification.policy")}
                    value={config.clarification_policy ?? "confirm"}
                    options={CLARIFICATION_POLICY_OPTIONS}
                    helper={t("searchAnswerProfiles.clarification.policyHelper")}
                    disabled={locked}
                    onValueChange={(value) =>
                      setConfig((current) => ({ ...current, clarification_policy: value }))
                    }
                    width="md"
                    className="min-w-0 [&>label]:sr-only"
                  />
                </div>
              </fieldset>
              {/* 回答の末尾の窓口の案内（#2400。回答の注意書きの窓口を、このプロファイルの回答だけ上書きする）。 */}
              <ContactGuidanceFieldset
                contactOverride={config.contact_override ?? ""}
                disabled={locked}
                onChange={(patch) => setConfig((current) => ({ ...current, ...patch }))}
              />
              {/* 言語資源の上書き（#2181。検索・回答の項目だけ。全体の値に重ねる）。 */}
              <LanguagePackOverridesFieldset
                value={config.language_pack ?? {}}
                disabled={locked}
                onChange={(overrides) => setConfig((current) => ({ ...current, language_pack: overrides }))}
              />
              {/* 保存の失敗は確定の行の直上の 1 か所だけ（messaging.md §3.3.1。#585 / #1786）。 */}
              <SaveErrorBanner
                message={saveError}
                attemptKey={saveMutation.submittedAt}
                testId="search-answer-profile-save-error"
              />
              {/* 確定はフォームの末尾の左（作成・保存 → 変更を破棄）。画面の下端に貼り付けず、内容と
                  一緒にスクロールする（#1786 / #2715）。下の「プロファイルの知識」は節ごとに保存する。 */}
              <FormActionBar
                ariaLabel={t("editor.actions.formLabel")}
                testId="search-answer-profile-form-actions"
                primaryActions={[
                  {
                    id: "save",
                    label: mode === "edit" ? t("searchAnswerProfiles.actions.save") : t("searchAnswerProfiles.actions.create"),
                    icon: mode === "edit" ? Save : Sparkles,
                    type: "submit",
                    loading: pending,
                    disabled: isArchived,
                  },
                ]}
                secondaryActions={
                  isArchived
                    ? []
                    : [
                        {
                          id: "discard",
                          label: t("editor.actions.discard"),
                          icon: RotateCcw,
                          disabled: !dirty || pending,
                          onClick: discard,
                        },
                      ]
                }
              />
            </form>
          </CardContent>
        </Card>
        {initial ? (
          <SearchAnswerProfileKnowledgePanel key={`knowledge-${initial.id}`} searchAnswerProfileId={initial.id} readOnly={isArchived} />
        ) : null}
      </PageBody>
    </div>
  );
}

/** 参照 KB のうち検索対象にならない件数（一覧の要約）。 */
function knowledgeBaseIssueCount(view: SearchAnswerProfileSummary): number {
  return (view.archived_knowledge_base_count ?? 0) + (view.missing_knowledge_base_count ?? 0);
}

// 警告に名前を並べる上限。超えた分は件数だけにする。
const ISSUE_NAMES_LIMIT = 5;

function issueNames(names: string[]): string {
  const shown = names.slice(0, ISSUE_NAMES_LIMIT).join("、");
  const rest = names.length - ISSUE_NAMES_LIMIT;
  return rest > 0 ? `${shown}${t("searchAnswerProfiles.knowledgeBaseIssues.more", { count: rest })}` : shown;
}

/**
 * 参照 KB にアーカイブ済み・見つからないものがあるときの警告（#302）。これらは検索されず、
 * すべてが該当すると検索・回答は 0 件になる。
 */
function KnowledgeBaseIssuesBanner({
  health,
  selectedCount,
}: {
  health: KnowledgeBaseSelectionHealth;
  selectedCount: number;
}) {
  const { archived, missing } = health;
  if (archived.length + missing.length === 0) return null;
  const allUnavailable = selectedCount > 0 && archived.length + missing.length >= selectedCount;
  return (
    <Banner
      severity="warning"
      title={t("searchAnswerProfiles.knowledgeBaseIssues.title")}
    >
      <div className="space-y-1" data-testid="search-answer-profile-kb-issues">
        {archived.length > 0 ? (
          <p className="break-words">
            {t("searchAnswerProfiles.knowledgeBaseIssues.archived", {
              count: archived.length,
              names: issueNames(archived.map((item) => item.name)),
            })}
          </p>
        ) : null}
        {missing.length > 0 ? (
          <p className="break-words">
            {t("searchAnswerProfiles.knowledgeBaseIssues.missing", {
              count: missing.length,
              names: issueNames(missing.map((item) => item.id)),
            })}
          </p>
        ) : null}
        {allUnavailable ? (
          <p className="font-medium">{t("searchAnswerProfiles.knowledgeBaseIssues.allUnavailable")}</p>
        ) : null}
        <p>{t("searchAnswerProfiles.knowledgeBaseIssues.hint")}</p>
      </div>
    </Banner>
  );
}

/** 説明の検証（作成・編集で共通。#521）。空・空白だけは入力を求める。 */
function validateSearchAnswerProfileDescription(description: string) {
  return requiredTextError(description, t("searchAnswerProfiles.descriptionRequired"));
}

function validateSearchAnswerProfileName(name: string, allowDefault = false) {
  const cleaned = name.trim();
  if (!cleaned) return t("searchAnswerProfiles.nameRequired");
  if (!allowDefault && cleaned.toUpperCase() === DEFAULT_SEARCH_ANSWER_PROFILE_NAME) {
    return t("searchAnswerProfiles.nameReserved");
  }
  return null;
}

/** 継承/上書きトグル + 上書き時のみ表示する選択欄(段階的開示)。 */
function QuerySelectRow<T extends string>({
  id,
  label,
  value,
  options,
  width,
  defaultOnOverride,
  helper,
  disabled = false,
  onChange,
}: {
  id: string;
  label: string;
  /** 項目の説明（継承・上書きのどちらでも出す）。 */
  helper?: string;
  value: T | null;
  options: readonly SelectFieldOption<T>[];
  /** 選択欄の幅（値の種類。#1891）。 */
  width: FieldWidth;
  defaultOnOverride: T;
  disabled?: boolean;
  onChange: (value: T | null) => void;
}) {
  const overriding = value !== null;
  return (
    <div className="grid gap-3 rounded-lg border border-border bg-surface-sunken p-3 md:grid-cols-[minmax(10rem,14rem)_minmax(0,1fr)]">
      <h3 className="text-sm font-medium text-fg">{label}</h3>
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap gap-1" role="group" aria-label={label}>
          <ToggleChip selected={!overriding} disabled={disabled} onClick={() => onChange(null)}>
            {t("searchAnswerProfiles.inherit")}
          </ToggleChip>
          <ToggleChip
            selected={overriding}
            disabled={disabled}
            onClick={() => {
              if (!overriding) onChange(defaultOnOverride);
            }}
          >
            {t("searchAnswerProfiles.override")}
          </ToggleChip>
        </div>
        {helper ? <p className="text-xs text-fg-muted">{helper}</p> : null}
        {overriding ? (
          <SelectField
            id={id}
            label={label}
            value={value}
            options={options}
            onValueChange={(next) => onChange(next)}
            // 継承 / 上書きの切り替えと同じく、保存中・アーカイブ済みは値も変えられないようにする
            // （読み取り専用の画面で未保存の変更を作らない）。
            disabled={disabled}
            width={width}
            className="[&>label]:sr-only"
          />
        ) : null}
      </div>
    </div>
  );
}
