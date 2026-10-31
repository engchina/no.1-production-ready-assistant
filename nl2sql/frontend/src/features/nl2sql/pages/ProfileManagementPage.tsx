import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useWorkspaceState } from "@/components/WorkspaceState";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  FileJson,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  UserCog,
} from "lucide-react";
import { useLocation, useSearchParams } from "react-router-dom";

import {
  compareNames,
  Button,
  LoadMoreFooter,
  ListToolbar,
  DataTable,
  EmptyState,
  FormStatus,
  toast,
  PageHeader,
  PageBody,
  useConfirm,
  apiErrorMessage,
  SelectField,
  TextField,
  ProcessingIndicator,
  ObjectActionBar,
  TimedLoadingState,
  TableSkeleton,
  FormSkeleton,
  FormActionBar,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  RowTitleButton,
  ListPicker,
  type ListPickerGroup,
  type ListPickerItem,
  TextareaField,
  FormGrid,
} from "@production-ready/ui";
import { RoleNameBadges } from "@production-ready/system-settings";
import { ErrorState } from "@/components/StateViews";
import { IdentifierText } from "@/components/IdentifierText";

import { PageNotice } from "@/components/page-notice";
import { ApiError, apiDelete, apiPatch, apiPost } from "@/lib/api";
import { useValuesChanged } from "@/lib/render-sync";
import { t } from "@/lib/i18n";
import { appPath } from "@/lib/base-path";
import { listLoadMoreErrorMessage } from "@/lib/load-more-error";
import { listCountLabels } from "@/lib/pagination-labels";
import { useUnsavedChangesGuard } from "@/lib/useUnsavedChangesGuard";
import { useSchemaOwners } from "@/lib/queries";
import { useAuth } from "@/features/security/AuthProvider";
import { MENU_PERMISSIONS } from "@/features/security/menu-permissions";
import { securityApi } from "@/features/security/api";
import type { ProfileAccessProfile } from "@/features/security/types";
import {
  DbManagementSearchField,
  DbObjectManagementPanelShell,
  DbObjectPanelHeader,
  DbObjectSelectorToolbar,
} from "../components/DbObjectManagementShared";
import {
  SchemaRefreshHeaderStatus,
  SchemaRefreshProcessing,
} from "../components/SchemaRefreshFeedback";
import { useSchemaRefreshCoordinator } from "../SchemaRefreshCoordinator";
import {
  nl2sqlIncrementalKeys,
  getSchemaObjectSnapshot,
  useProfileDetail,
  useProfileSummaries,
  useSchemaCatalogHead,
  useSchemaObjects,
} from "../incrementalQueries";
import { isUserVisibleObjectName } from "../objectVisibility";
import {
  applySchemaBulkSelection,
  countSelectedObjectsInOwner,
  normalizeObjectKey,
  profileFormEquals,
  selectedObjectKeys,
  toggleObjectSelection,
} from "../profileObjectSelection";
import type { ProfileListSortKey, ProfileListSortState } from "../profileListState";
import { schemaTableQualifiedName } from "../workbenchState";
import { DbObjectName } from "../components/DbObjectName";
import { formatDbObjectName, formatDbObjectPart } from "../dbObjectIdentity";
import { isProfileSaveTargetCurrent } from "../profileSaveTarget";
import type {
  ClarificationPolicy,
  Nl2SqlProfile,
  ProfileDeleteData,
  ProfileSummary,
  ProfileUpsertPayload,
  SchemaObjectSummary,
  SchemaTable,
} from "../types";

type ActiveView = "list" | "editor";
type ProfileNameError = "required" | "format" | "duplicate" | null;
type ProfileRequiredField = "category";
type ProfileRequiredErrors = Partial<Record<ProfileRequiredField, true>>;

const PROFILE_NAME_PATTERN = /^[A-Z][A-Z0-9_]*$/u;
/** ヘッダーの「スキーマを更新」の起点（SchemaRefreshCoordinator の start(origin)。#821）。 */
const PROFILE_SCHEMA_REFRESH_HEADER = "profile-management-header";

interface ProfileFormState {
  name: string;
  category: string;
  allowedTables: string[];
  allowedViews: string[];
  /** SQL 生成に渡す業務プロファイルのルール（1 行 1 件。保存の payload の `sql_rules`）。 */
  sqlRules: string;
  /** 回答前の確認の方針（#1839）。 */
  clarificationPolicy: ClarificationPolicy;
  /** 確認の上限（往復）。0 はシステムの既定。 */
  clarificationMaxRounds: number;
}

/** 確認の上限（往復）の選択肢。0 はシステムの既定（#1839）。 */
const CLARIFICATION_MAX_ROUNDS_OPTIONS = [0, 2, 3, 4, 5, 6] as const;


const EMPTY_FORM: ProfileFormState = {
  name: "",
  category: "",
  allowedTables: [],
  allowedViews: [],
  sqlRules: "",
  clarificationPolicy: "confirm",
  clarificationMaxRounds: 0,
};

function emptyProfileForm(): ProfileFormState {
  return {
    ...EMPTY_FORM,
    allowedTables: [],
    allowedViews: [],
  };
}

function sqlRulesFromText(value: string) {
  const seen = new Set<string>();
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => {
      if (!line || seen.has(line)) return false;
      seen.add(line);
      return true;
    });
}

function normalizeProfileName(value: string) {
  return value.trim().toUpperCase();
}

function profileNameError(value: string): ProfileNameError {
  const normalized = normalizeProfileName(value);
  if (!normalized) return "required";
  return PROFILE_NAME_PATTERN.test(normalized) ? null : "format";
}

function profileNameErrorMessage(error: Exclude<ProfileNameError, null>) {
  if (error === "required") return t("profiles.error.nameRequired");
  if (error === "duplicate") return t("profiles.error.nameDuplicate");
  return t("profiles.error.nameFormat");
}

function isProfileNameConflictError(error: unknown) {
  if (!(error instanceof ApiError) || error.status !== 422) return false;
  if (error.errorCode === "NL2SQL_PROFILE_NAME_CONFLICT") return true;
  return error.fieldErrors.some(
    (fieldError) =>
      fieldError.pointer === "/name" && fieldError.code === "profile_name_conflict"
  );
}

function profileRequiredErrors(form: ProfileFormState): ProfileRequiredErrors {
  const errors: ProfileRequiredErrors = {};
  if (!form.category.trim()) errors.category = true;
  return errors;
}

function hasProfileRequiredErrors(errors: ProfileRequiredErrors) {
  return Object.keys(errors).length > 0;
}

function schemaObjectQueryTotal(
  pages: Array<{ total: number | null }> | undefined,
  loadedCount: number
) {
  const total = pages?.[0]?.total;
  return typeof total === "number" ? Math.max(total, loadedCount) : loadedCount;
}

function normalizeProfile(profile: Nl2SqlProfile): Nl2SqlProfile {
  return {
    ...profile,
    allowed_tables: (profile.allowed_tables ?? []).filter(isUserVisibleObjectName),
    allowed_views: (profile.allowed_views ?? []).filter(isUserVisibleObjectName),
  };
}

function profileToForm(profile: Nl2SqlProfile): ProfileFormState {
  const normalized = normalizeProfile(profile);
  return {
    name: normalized.name,
    category: normalized.category ?? "",
    allowedTables: normalized.allowed_tables,
    allowedViews: normalized.allowed_views,
    sqlRules: (normalized.sql_rules ?? []).join("\n"),
    clarificationPolicy: normalized.clarification_policy ?? "confirm",
    clarificationMaxRounds: normalized.clarification_max_rounds ?? 0,
  };
}

function formToPayload(form: ProfileFormState): ProfileUpsertPayload {
  const profileName = normalizeProfileName(form.name);
  return {
    name: profileName,
    category: form.category.trim(),
    allowed_tables: form.allowedTables,
    allowed_views: form.allowedViews,
    sql_rules: sqlRulesFromText(form.sqlRules),
    safety_policy: "select_only",
    clarification_policy: form.clarificationPolicy,
    clarification_max_rounds: form.clarificationMaxRounds,
  };
}

function schemaSummaryToTable(object: SchemaObjectSummary): SchemaTable {
  return {
    table_name: object.object_name,
    qualified_name: formatDbObjectName({ owner: object.owner, name: object.object_name }),
    logical_name: object.logical_name,
    owner: object.owner,
    table_type: object.object_type,
    comment: object.comment,
    row_count: object.row_count,
    columns: [],
    constraints: [],
  };
}

function ProfileList({
  profiles,
  totalCount,
  unfilteredTotal,
  selectedProfileId,
  loading,
  loadingIndicator = true,
  search,
  sort,
  onSearchChange,
  onSortChange,
  onSelect,
  profileHref,
  hasNextPage,
  loadingNextPage,
  loadMoreError,
  onLoadMore,
  onRetryLoadMore,
}: {
  profiles: ProfileSummary[];
  /** 今の検索に一致した全件（API の total）。 */
  totalCount: number;
  /** 絞り込む前の全件（検索なしの同じ query の total。まだ分からないときは undefined）。 */
  unfilteredTotal?: number;
  selectedProfileId: string;
  loading: boolean;
  /** 読み込み中の文言と経過時間を一覧の位置に出すか。作業領域の先頭に同じ処理の表示があるときは false。 */
  loadingIndicator?: boolean;
  search: string;
  sort: ProfileListSortState;
  onSearchChange: (value: string) => void;
  onSortChange: (key: ProfileListSortKey) => void;
  onSelect: (profile: ProfileSummary) => void;
  /** プロファイルの編集画面の URL（名前のリンク。新しいタブで開ける。#583）。 */
  profileHref: (profile: ProfileSummary) => string;
  hasNextPage: boolean;
  loadingNextPage: boolean;
  loadMoreError: string;
  onLoadMore: () => void;
  onRetryLoadMore: () => void;
}) {
  return (
    <section className="grid min-w-0 content-start gap-3" aria-labelledby="profile-list-heading">
      <DbObjectPanelHeader
        headingId="profile-list-heading"
        icon={UserCog}
        title={t("profiles.list.title")}
        description={t("profiles.list.hint")}
      />
      {/* 一覧のツールバー: 左に検索欄、右に件数（ListCount 1 か所。#600 / #1891）。絞り込み中は「N 件（全 M 件）」。 */}
      <ListToolbar
        className="rounded-md border border-border bg-surface-sunken p-3"
        testId="profile-list-toolbar"
        search={
          <DbManagementSearchField
            label={t("profiles.list.search")}
            placeholder={t("profiles.list.searchPlaceholder")}
            value={search}
            onChange={onSearchChange}
          />
        }
        count={
          loading
            ? undefined
            : {
                total: unfilteredTotal ?? totalCount,
                matched: search.trim() ? totalCount : undefined,
                labels: listCountLabels(),
              }
        }
      />
      {loading ? (
        loadingIndicator ? (
          <TimedLoadingState
            label={t("profiles.summary.loading")}
            operationKey="profile-list-load"
            framed={false}
            testId="profile-list-loading"
          >
            <TableSkeleton columns={3} testId="profile-list-skeleton" />
          </TimedLoadingState>
        ) : (
          // 同じ読み込みの経過時間は作業領域の先頭に出ているため、ここでは形だけを出す（messaging.md §3.7）。
          <TableSkeleton columns={3} testId="profile-list-skeleton" />
        )
      ) : profiles.length === 0 ? (
        <EmptyState
          title={search.trim() ? t("profiles.list.noResultsTitle") : t("profiles.empty.title")}
          hint={search.trim() ? t("profiles.list.noResultsHint") : t("profiles.empty.hint")}
        />
      ) : (
        <DataTable
          columns={[
            {
              key: "name",
              header: t("profiles.field.name"),
              sortable: true,
              className: "align-top",
              render: (profile) => (
                <RowTitleButton
                  // 375px では名前列が狭く、区切りのない名前（PROFILE_EMP 等）の min-content が隣の列へはみ出すため、
                  // `_` の位置を優先して折り返し、最後の手段として任意位置で折り返す。
                  title={<IdentifierText value={profile.name} />}
                  subtitle={<span className="line-clamp-2">{profile.category || "-"}</span>}
                  current={profile.id === selectedProfileId}
                  aria-label={t("profiles.action.selectProfile", { name: profile.name })}
                  href={profileHref(profile)}
                  onClick={() => onSelect(profile)}
                />
              ),
            },
            {
              key: "tables",
              header: t("profiles.field.allowedTables"),
              sortable: true,
              align: "right",
              headerClassName: "w-[7rem]",
              className: "align-top font-sans",
              render: (profile) => profile.allowed_table_count,
            },
            {
              key: "views",
              header: t("profiles.field.allowedViews"),
              sortable: true,
              align: "right",
              headerClassName: "w-[7rem]",
              className: "align-top font-sans",
              render: (profile) => profile.allowed_view_count,
            },
          ]}
          rows={profiles}
          getRowKey={(profile) => profile.id}
          sort={sort}
          onSortChange={(next) => onSortChange(next.key as ProfileListSortKey)}
          selectedRowKey={selectedProfileId}
          onRowClick={onSelect}
          rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
          testId="profile-management-grid"
          tableClassName="w-full min-w-0 table-fixed"
          // 名前・カテゴリの 2 行セルが並ぶ一覧。横スクロールは出さず、縦だけ内部スクロールにする。
          // 高さは md 未満 5 行・md 以上 8 行の実測（#403。以前は手書きの 20rem / 30.5rem）。
          className="max-w-full overflow-x-hidden"
          scrollAriaLabel={t("profiles.list.scrollLabel")}
          scrollTestId="profile-management-list"
          stickyHeader
          visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
        />
      )}
      {/* 「さらに読み込む」と失敗の再試行は共通の LoadMoreFooter（#1266。page-archetypes の追加読み込みの例外）。
          件数はツールバーの ListCount で出すので summary は空（二重にしない。#1891）。 */}
      {!loading && profiles.length > 0 && (hasNextPage || loadMoreError) && (
        <LoadMoreFooter
          summary=""
          hasMore={hasNextPage}
          loadingMore={loadingNextPage}
          loadMoreError={loadMoreError || undefined}
          onLoadMore={onLoadMore}
          onRetry={onRetryLoadMore}
          loadMoreLabel={t("profiles.action.loadMore")}
          retryLabel={t("common.retry")}
          testId="profile-management-load-more"
        />
      )}
    </section>
  );
}

function ProfileRulesFields({
  form,
  setForm,
}: {
  form: ProfileFormState;
  setForm: (updater: (current: ProfileFormState) => ProfileFormState) => void;
}) {
  return (
    <section
      id="profile-sql-rules"
      className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3"
      aria-labelledby="profile-sql-rules-heading"
    >
      <h3 id="profile-sql-rules-heading" className="text-sm font-semibold text-fg">
        {t("profiles.editor.sqlRules")}
      </h3>
      {/* 文章の欄は full（FormGrid の 60rem まで）で 1 列に積む。 */}
      <FormGrid columns={1}>
        <TextareaField
          id="profile-sql-rules-input"
          label={t("profiles.field.additionalInstructions")}
          width="full"
          helper={t("profiles.field.additionalInstructionsHint")}
          value={form.sqlRules}
          rows={6}
          onChange={(event) => {
            const value = event.currentTarget.value;
            setForm((current) => ({ ...current, sqlRules: value }));
          }}
          placeholder={t("profiles.placeholder.additionalInstructions")}
        />
      </FormGrid>
    </section>
  );
}

/**
 * 許可する表・ビューの選択（#600）。大量の候補から複数を選ぶ共通の `ListPicker` に、スキーマ（owner）ごとのグループと
 * スキーマ単位の一括選択を渡す。検索欄は表・ビューで共通のツールバー（`DbObjectSelectorToolbar`）が持つ。
 * 行の描画・仮想スクロール・キーボード操作・追加読み込みのフッターは ListPicker が持つ（NL2SQL で二重に持たない）。
 */
function SchemaGroupedSelectionPanel({
  title,
  objects,
  totalCount,
  selectedItems,
  dataTestId,
  emptyTitle,
  emptyHint,
  onToggle,
  loading,
  disabled,
  hasActiveFilter,
  hasNextPage,
  loadingNextPage,
  loadMoreError,
  onLoadMore,
  onRetryLoadMore,
  ownerTotals,
  onToggleSchema,
}: {
  title: string;
  objects: SchemaTable[];
  totalCount: number;
  selectedItems: string[];
  dataTestId: string;
  emptyTitle: string;
  emptyHint: string;
  onToggle: (name: string) => void;
  loading: boolean;
  disabled: boolean;
  hasActiveFilter: boolean;
  hasNextPage: boolean;
  loadingNextPage: boolean;
  loadMoreError: string;
  onLoadMore: () => void;
  onRetryLoadMore: () => void;
  ownerTotals: Record<string, number>;
  onToggleSchema: (owner: string, select: boolean) => Promise<void>;
}) {
  const selectedSet = useMemo(() => selectedObjectKeys(selectedItems), [selectedItems]);
  const { items, owners } = useMemo(() => {
    const grouped = new Map<string, SchemaTable[]>();
    for (const object of objects) {
      // 小文字を含む owner（`"Sales"`）を大文字の同名 owner と同じグループにしない（#563）。
      grouped.set(object.owner, [...(grouped.get(object.owner) ?? []), object]);
    }
    const sortedOwners = [...grouped.keys()].sort((left, right) => compareNames(left, right));
    const pickerItems: ListPickerItem[] = sortedOwners.flatMap((owner) =>
      (grouped.get(owner) ?? [])
        .slice()
        .sort((left, right) => compareNames(schemaTableQualifiedName(left), schemaTableQualifiedName(right)))
        .map((object) => {
          const qualified = schemaTableQualifiedName(object);
          return {
            key: normalizeObjectKey(qualified),
            label: <DbObjectName value={qualified} size="xs" truncate className="block" />,
            textValue: qualified,
            description: object.logical_name || object.comment || object.table_name,
            groupKey: owner,
          };
        })
    );
    return { items: pickerItems, owners: sortedOwners };
  }, [objects]);

  const groups: ListPickerGroup[] = owners.map((owner) => {
    const selectedCount = countSelectedObjectsInOwner(selectedSet, owner);
    const total = ownerTotals[owner] ?? items.filter((item) => item.groupKey === owner).length;
    // 表示・読み上げは SQL と同じ表記（`"Sales"` と `SALES` を見分けられる、#563）。
    const ownerLabel = formatDbObjectPart(owner);
    return {
      key: owner,
      label: (
        <span className="rounded border border-border bg-surface px-2 py-0.5 font-mono text-xs font-semibold text-fg">
          {ownerLabel}
        </span>
      ),
      textValue: ownerLabel,
      countLabel: t("profiles.objects.schemaCount", { selected: selectedCount, total }),
      onSelectAll: () => onToggleSchema(owner, true),
      onClearAll: () => onToggleSchema(owner, false),
      selectAllDisabled: total > 0 && selectedCount >= total,
      clearAllDisabled: selectedCount === 0,
      testId: `${ownerLabel.toLowerCase()}-schema`,
    };
  });

  return (
    <ListPicker
      id={dataTestId}
      title={title}
      headingLevel={4}
      label={title}
      items={items}
      groups={groups}
      selectedKeys={selectedSet}
      onToggle={(item) => onToggle(item.textValue)}
      total={totalCount}
      hasActiveFilter={hasActiveFilter}
      loading={loading}
      disabled={disabled}
      hasMore={hasNextPage}
      loadingMore={loadingNextPage}
      loadMoreError={loadMoreError || undefined}
      onLoadMore={loadMoreError ? onRetryLoadMore : onLoadMore}
      fixedHeight
      labels={{
        resultCount: ({ visible, total, selected }) =>
          t("objectSelector.resultCountWithSelected", { visible, total, selected }),
        loading: t("profiles.objects.loading"),
        loadMore: t("profiles.action.loadMore"),
        retry: t("common.retry"),
        emptyTitle,
        emptyHint,
        noResultsTitle: emptyTitle,
        noResultsHint: emptyHint,
        clearSearch: t("common.clearSearch"),
        groupSelectAll: t("profiles.objects.selectSchemaAction"),
        groupClearAll: t("profiles.objects.clearSchema"),
        groupSelectAllAria: (name) => t("common.selection.selectGroup", { name }),
        groupClearAllAria: (name) => t("common.selection.clearGroup", { name }),
        keyboardHint: t("objectSelector.keyboardHint"),
      }}
      testId={dataTestId}
    />
  );
}

function ProfileEditor({
  selectedProfile,
  profileAccessProfile,
  form,
  tableObjects,
  viewObjects,
  tableObjectTotal,
  viewObjectTotal,
  tableOwnerTotals,
  viewOwnerTotals,
  tableObjectsLoading,
  viewObjectsLoading,
  tableHasNextPage,
  viewHasNextPage,
  tableLoadingNextPage,
  viewLoadingNextPage,
  tableLoadMoreError,
  viewLoadMoreError,
  objectFilter,
  saving,
  busy,
  nameError,
  requiredErrors,
  dirty,
  saveError,
  saveConflict,
  reloadingLatest,
  onReloadLatest,
  deleting,
  onObjectFilterChange,
  onFormChange,
  onNameErrorClear,
  onRequiredErrorClear,
  onToggleTable,
  onToggleView,
  onToggleTableSchema,
  onToggleViewSchema,
  onLoadMoreTables,
  onLoadMoreViews,
  onRetryLoadMoreTables,
  onRetryLoadMoreViews,
  onSave,
  onDelete,
  onDiscard,
}: {
  selectedProfile: Nl2SqlProfile | null;
  profileAccessProfile: ProfileAccessProfile | null;
  form: ProfileFormState;
  tableObjects: SchemaTable[];
  viewObjects: SchemaTable[];
  tableObjectTotal: number;
  viewObjectTotal: number;
  tableOwnerTotals: Record<string, number>;
  viewOwnerTotals: Record<string, number>;
  tableObjectsLoading: boolean;
  viewObjectsLoading: boolean;
  tableHasNextPage: boolean;
  viewHasNextPage: boolean;
  tableLoadingNextPage: boolean;
  viewLoadingNextPage: boolean;
  tableLoadMoreError: string;
  viewLoadMoreError: string;
  objectFilter: string;
  saving: boolean;
  busy: boolean;
  nameError: ProfileNameError;
  requiredErrors: ProfileRequiredErrors;
  /** 最後に保存した内容から変わったか（「変更を破棄」の可否）。 */
  dirty: boolean;
  /** プロファイルの保存（PATCH / POST）の失敗。保存ボタンの直下だけに出す（messaging.md §3.3.1。#585）。 */
  saveError: string;
  /** 保存の失敗がほかの更新との競合（409）。失敗の文の下に「最新の内容を読み込む」を出す（#1111）。 */
  saveConflict: boolean;
  reloadingLatest: boolean;
  onReloadLatest: () => void;
  deleting: boolean;
  onObjectFilterChange: (value: string) => void;
  onFormChange: (updater: (current: ProfileFormState) => ProfileFormState) => void;
  onNameErrorClear: () => void;
  onRequiredErrorClear: (field: ProfileRequiredField) => void;
  onToggleTable: (name: string) => void;
  onToggleView: (name: string) => void;
  onToggleTableSchema: (owner: string, select: boolean) => Promise<void>;
  onToggleViewSchema: (owner: string, select: boolean) => Promise<void>;
  onLoadMoreTables: () => void;
  onLoadMoreViews: () => void;
  onRetryLoadMoreTables: () => void;
  onRetryLoadMoreViews: () => void;
  onSave: () => void;
  onDelete: () => void;
  onDiscard: () => void;
}) {
  return (
    <fieldset disabled={busy} className="grid min-w-0 content-start gap-4" aria-labelledby="profile-editor-heading">
      <DbObjectPanelHeader
        headingId="profile-editor-heading"
        icon={FileJson}
        title={
          selectedProfile
            ? t("profiles.editor.editNamed", { name: selectedProfile.name })
            : t("profiles.editor.new")
        }
        description={t("profiles.editor.hint")}
        action={
          selectedProfile ? (
            <ObjectActionBar
              ariaLabel={t("profiles.editor.actions")}
              testId="profile-editor-actions"
              actions={[
                {
                  id: "delete",
                  label: t("profiles.action.delete"),
                  icon: Trash2,
                  tone: "danger",
                  loading: deleting,
                  onSelect: onDelete,
                },
              ]}
            />
          ) : undefined
        }
      />

      <section className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3">
        <h3 className="text-sm font-semibold text-fg">{t("profiles.editor.basic")}</h3>
        {/* フォームの本文は FormGrid（最大 60rem・置き場所 42rem 以上で 2 列。#1891）。 */}
        <FormGrid className="gap-y-1.5">
          {/* 名称の補足は 2 列（名称・カテゴリ）にまたがる 1 行で出すため、欄の helper ではなく下の行に置き、
              aria-describedby で名称の欄につなぐ。1 列のときは名称 → 補足 → カテゴリの順に積む（#631）。
              並びの切り替えは FormGrid の段組みと同じ container query（@2xl）で行う。 */}
          <TextField
            id="profile-name"
            label={t("profiles.field.name")}
            width="md"
            required
            value={form.name}
            onChange={(event) => {
              const value = event.currentTarget.value.toUpperCase();
              onFormChange((current) => ({ ...current, name: value }));
              if (nameError) onNameErrorClear();
            }}
            onBlur={(event) => {
              const value = normalizeProfileName(event.currentTarget.value);
              onFormChange((current) => ({ ...current, name: value }));
            }}
            error={nameError ? profileNameErrorMessage(nameError) : undefined}
            aria-describedby="profile-name-helper"
            className="order-1 min-w-0 @2xl:order-none"
          />
          <TextField
            id="profile-category"
            label={t("profiles.field.category")}
            width="md"
            required
            value={form.category}
            onChange={(event) => {
              const value = event.currentTarget.value;
              onFormChange((current) => ({ ...current, category: value }));
              if (requiredErrors.category) onRequiredErrorClear("category");
            }}
            error={requiredErrors.category ? t("profiles.error.categoryRequired") : undefined}
            className="order-3 min-w-0 @2xl:order-none"
          />
          <p
            id="profile-name-helper"
            className="order-2 col-span-full text-xs font-normal leading-5 text-fg-muted @2xl:order-none"
          >
            {t("profiles.field.nameHint")}
          </p>
        </FormGrid>
      </section>

      {/* 回答前の確認（#1839。3 製品で業務の単位に置く設定）。チャットと Agent の MCP の問い合わせに効く。 */}
      <section
        aria-labelledby="profile-clarification-title"
        className="grid gap-3"
        data-testid="profile-clarification-settings"
      >
        <div>
          <h3 id="profile-clarification-title" className="text-sm font-semibold text-fg">
            {t("profiles.clarification.title")}
          </h3>
          <p className="mt-1 text-sm text-fg-muted">{t("profiles.clarification.description")}</p>
        </div>
        <FormGrid>
          <SelectField
            id="profile-clarification-policy"
            label={t("profiles.clarification.policy")}
            width="md"
            value={form.clarificationPolicy}
            options={[
              { value: "confirm", label: t("profiles.clarification.policy.confirm") },
              { value: "assume", label: t("profiles.clarification.policy.assume") },
            ]}
            helper={t("profiles.clarification.policyHint")}
            onValueChange={(value) =>
              onFormChange((current) => ({ ...current, clarificationPolicy: value as ClarificationPolicy }))
            }
            className="min-w-0"
          />
          <SelectField
            id="profile-clarification-max-rounds"
            label={t("profiles.clarification.maxRounds")}
            width="sm"
            value={String(form.clarificationMaxRounds)}
            options={CLARIFICATION_MAX_ROUNDS_OPTIONS.map((rounds) => ({
              value: String(rounds),
              label:
                rounds === 0
                  ? t("profiles.clarification.maxRounds.default")
                  : t("profiles.clarification.maxRounds.value", { count: rounds }),
            }))}
            helper={t("profiles.clarification.maxRoundsHint")}
            disabled={form.clarificationPolicy === "assume"}
            onValueChange={(value) =>
              onFormChange((current) => ({ ...current, clarificationMaxRounds: Number(value) }))
            }
            className="min-w-0"
          />
        </FormGrid>
      </section>

      {selectedProfile && profileAccessProfile ? (
        <section className="grid gap-2 rounded-md border border-border bg-surface-sunken p-3">
          <h3 className="text-sm font-semibold text-fg">{t("profiles.access.title")}</h3>
          <p className="text-sm text-fg-muted">{t("profiles.access.hint")}</p>
          {profileAccessProfile.allowed_roles.length > 0 ? (
            // ロールはロール名で出す。ID（UUID）・コードは出さない（#1682）。
            <RoleNameBadges
              roles={profileAccessProfile.allowed_roles}
              ariaLabel={t("profiles.access.title")}
            />
          ) : (
            <p className="rounded-md border border-dashed border-border p-3 text-sm text-fg-muted">
              {t("profiles.access.none")}
            </p>
          )}
        </section>
      ) : null}

      <section data-testid="profile-allowed-object-list" className="grid gap-3">
        <div>
          <h3 className="text-sm font-semibold text-fg">{t("profiles.editor.objects")}</h3>
          <p className="mt-1 text-sm text-fg-muted">{t("profiles.field.allowedObjects")}</p>
        </div>
        <DbObjectSelectorToolbar
          searchLabel={t("profiles.objects.filter")}
          searchPlaceholder={t("profiles.objects.filterPlaceholder")}
          searchValue={objectFilter}
          onSearchChange={onObjectFilterChange}
          resultLabel={t("objectSelector.resultCountWithSelected", {
            visible: tableObjects.length + viewObjects.length,
            total: tableObjectTotal + viewObjectTotal,
            selected: form.allowedTables.length + form.allowedViews.length,
          })}
          dataTestId="profile-object-search-toolbar"
        />
        <div className="grid gap-3 xl:grid-cols-2">
          <SchemaGroupedSelectionPanel
            title={t("profiles.objects.tablesTitle")}
            objects={tableObjects}
            totalCount={tableObjectTotal}
            selectedItems={form.allowedTables}
            dataTestId="profile-allowed-table-list"
            emptyTitle={t("profiles.objects.emptyTables")}
            emptyHint={t("profiles.objects.emptyTablesHint")}
            onToggle={onToggleTable}
            loading={tableObjectsLoading}
            disabled={busy}
            hasActiveFilter={Boolean(objectFilter.trim())}
            hasNextPage={tableHasNextPage}
            loadingNextPage={tableLoadingNextPage}
            loadMoreError={tableLoadMoreError}
            onLoadMore={onLoadMoreTables}
            onRetryLoadMore={onRetryLoadMoreTables}
            ownerTotals={tableOwnerTotals}
            onToggleSchema={onToggleTableSchema}
          />
          <SchemaGroupedSelectionPanel
            title={t("profiles.objects.viewsTitle")}
            objects={viewObjects}
            totalCount={viewObjectTotal}
            selectedItems={form.allowedViews}
            dataTestId="profile-allowed-view-list"
            emptyTitle={t("profiles.objects.emptyViews")}
            emptyHint={t("profiles.objects.emptyViewsHint")}
            onToggle={onToggleView}
            loading={viewObjectsLoading}
            disabled={busy}
            hasActiveFilter={Boolean(objectFilter.trim())}
            hasNextPage={viewHasNextPage}
            loadingNextPage={viewLoadingNextPage}
            loadMoreError={viewLoadMoreError}
            onLoadMore={onLoadMoreViews}
            onRetryLoadMore={onRetryLoadMoreViews}
            ownerTotals={viewOwnerTotals}
            onToggleSchema={onToggleViewSchema}
          />
        </div>
      </section>

      <ProfileRulesFields form={form} setForm={onFormChange} />

      {/* 保存の失敗は確定の行の直上の 1 か所だけに出す（Toast に重ねない。messaging.md §3.3.1。#585 / #1786）。 */}
      {saveError ? (
        <div data-testid="profile-save-error" className="grid justify-items-start gap-2">
          <FormStatus tone="danger" message={saveError} />
          {saveConflict ? (
            <Button
              type="button"
              variant="secondary"
              icon={RefreshCw}
              loading={reloadingLatest}
              onClick={onReloadLatest}
            >
              {t("profiles.conflict.reload")}
            </Button>
          ) : null}
        </div>
      ) : null}
      {/* 確定はフォームの末尾の左（design-system README §4「詳細・作成・編集の画面の操作」。#1786）。 */}
      <FormActionBar
        ariaLabel={t("profiles.editor.saveActions")}
        testId="profile-save-actions"
        primaryActions={[
          {
            id: "save",
            label: t("profiles.action.save"),
            icon: Save,
            loading: saving,
            disabled: saving,
            onClick: onSave,
            testId: "profile-save",
          },
        ]}
        secondaryActions={[
          {
            id: "discard",
            label: t("profiles.action.discard"),
            disabled: !dirty || saving,
            onClick: onDiscard,
            testId: "profile-discard",
          },
        ]}
      />
    </fieldset>
  );
}

export function ProfileManagementPage() {
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const confirm = useConfirm();
  const queryClient = useQueryClient();
  const auth = useAuth();
  const [form, setForm] = useState<ProfileFormState>(EMPTY_FORM);
  // 一覧の検索語は作業状態に残す（再読込・画面の移動の後も同じ絞り込みに戻る。#1933）。
  const [profileSearch, setProfileSearch] = useWorkspaceState("profileSearch", "");
  const [objectFilter, setObjectFilter] = useState("");
  const [profileSort, setProfileSort] = useState<ProfileListSortState>({
    key: "name",
    direction: "asc",
  });
  const [profileSaveError, setProfileSaveError] = useState("");
  const [loading, setLoading] = useState("");
  // 「最新情報を取得」をどこで押したか（ヘッダー / 失敗の案内）。スピナーは押したボタンだけが出す（#819）。
  const [loadOrigin, setLoadOrigin] = useState<"header" | "notice">("header");
  // message は初回ロード失敗の常設 Banner 専用(クエリ状態を監視する effect が所有する)。
  // 保存/削除の成否は toast、名前検証は nameError で扱う。
  const [message, setMessage] = useState("");
  // 手動更新の失敗は message とは別に保持する。同じ state に載せると、
  // 直後に走るクエリ監視 effect が空文字で上書きして警告が消えてしまう。
  const [refreshError, setRefreshError] = useState("");
  const [nameError, setNameError] = useState<ProfileNameError>(null);
  const [requiredErrors, setRequiredErrors] = useState<ProfileRequiredErrors>({});
  // 保存の失敗がほかの更新との競合（ETag の不一致の 409）か。文に合わせて「最新の内容を読み込む」を出す（#1111）。
  const [profileSaveConflict, setProfileSaveConflict] = useState(false);

  // ?profile= が唯一の情報源: null=一覧 / "new"=新規 / <id>=編集
  const profileParam = searchParams.get("profile");
  const editTargetRef = useRef(profileParam);
  // 最新の編集対象を commit 時に入れる（render 中に ref を書かない）。
  useLayoutEffect(() => { editTargetRef.current = profileParam; });
  const [bulkSelecting, setBulkSelecting] = useState(false);
  const mutationBusy = bulkSelecting || (loading !== "" && loading !== "load");
  // 名称の重複で保存が失敗したら名称の欄へフォーカスを戻す。保存中は編集の fieldset が disabled なので、
  // 保存の状態が解けて欄が有効になってから移す（requestAnimationFrame だと再描画の前に走り、無効の欄に当たる）。
  const [nameFocusRequest, setNameFocusRequest] = useState(0);
  useEffect(() => {
    if (nameFocusRequest === 0 || mutationBusy) return;
    document.getElementById("profile-name")?.focus();
  }, [nameFocusRequest, mutationBusy]);
  const mutationBusyRef = useRef(mutationBusy);
  // 最新の実行中状態を commit 時に入れる（render 中に ref を書かない）。handler は直前に true を先に入れる。
  useLayoutEffect(() => { mutationBusyRef.current = mutationBusy; });
  const activeView: ActiveView = profileParam ? "editor" : "list";
  const selectedProfileId = profileParam && profileParam !== "new" ? profileParam : "";
  // 1 打鍵ごとに一覧 / オブジェクト検索 API を叩かないよう、query key へはデバウンス値を渡す。
  // 検索語・所有者の接頭辞は SearchField が確定した値（入力が止まって 300ms・Enter・消去。IME の変換中は
  // 確定しない）なので、ここでは遅延させずにそのまま問い合わせに使う（#535）。
  const profilesQuery = useProfileSummaries(profileSearch, profileSort);
  // 件数の「全 M 件」（絞り込む前の全件）は、検索なしの同じ query（同じ queryKey でキャッシュを共有する）の total から読む。
  // 検索していないときは上と同じ queryKey なので、問い合わせは増えない（#1891）。
  const unfilteredProfilesQuery = useProfileSummaries("", profileSort);
  const profileDetailQuery = useProfileDetail(selectedProfileId);
  const tableObjectsQuery = useSchemaObjects(objectFilter, "TABLE");
  const viewObjectsQuery = useSchemaObjects(objectFilter, "VIEW");
  const schemaOwnersQuery = useSchemaOwners();
  const schemaHeadQuery = useSchemaCatalogHead();
  const sharedSchemaRefresh = useSchemaRefreshCoordinator();
  // 利用可能ロールの一覧 API は権限管理（menu.security_permissions）の担当（#206）。
  const canViewProfileAccess = auth.hasPermission(MENU_PERMISSIONS.securityPermissions);
  // 候補は全件を読まない（#608）。選択中の業務プロファイルだけを ids で読み、利用できるロールを出す。
  const profileAccessProfilesQuery = useQuery({
    queryKey: ["security", "profile-access", "profiles", selectedProfileId],
    queryFn: ({ signal }) =>
      securityApi.profileAccessProfiles(
        { q: "", limit: 1, offset: 0, ids: [selectedProfileId] },
        { signal }
      ),
    enabled: canViewProfileAccess && Boolean(selectedProfileId),
    staleTime: 10_000,
    retry: false,
  });
  const profiles = useMemo(
    () => profilesQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [profilesQuery.data]
  );
  const profilesLoaded = !profilesQuery.isPending;
  const selectedProfile = profileDetailQuery.data?.profile ?? null;
  // API 応答が想定外の形でも一覧画面全体を落とさない(配列以外は空扱い)。
  const profileAccessProfiles = Array.isArray(profileAccessProfilesQuery.data?.items)
    ? profileAccessProfilesQuery.data.items
    : [];
  const selectedProfileAccessProfile =
    profileAccessProfiles.find((profile) => profile.id === selectedProfile?.id) ?? null;

  const tableObjects = useMemo(
    () =>
      (tableObjectsQuery.data?.pages.flatMap((page) => page.items) ?? [])
        .map(schemaSummaryToTable),
    [tableObjectsQuery.data]
  );
  const viewObjects = useMemo(
    () =>
      (viewObjectsQuery.data?.pages.flatMap((page) => page.items) ?? [])
        .map(schemaSummaryToTable),
    [viewObjectsQuery.data]
  );
  const tableObjectTotal = schemaObjectQueryTotal(tableObjectsQuery.data?.pages, tableObjects.length);
  const viewObjectTotal = schemaObjectQueryTotal(viewObjectsQuery.data?.pages, viewObjects.length);
  const profileTotal = schemaObjectQueryTotal(profilesQuery.data?.pages, profiles.length);
  const unfilteredProfileTotal = profileSearch.trim()
    ? unfilteredProfilesQuery.data?.pages[0]?.total
    : profileTotal;
  const profileLoadMoreError =
    profilesQuery.isFetchNextPageError && profilesQuery.error
      ? listLoadMoreErrorMessage(profilesQuery.error, "profiles.error.load")
      : "";
  const tableLoadMoreError =
    tableObjectsQuery.isFetchNextPageError && tableObjectsQuery.error
      ? listLoadMoreErrorMessage(tableObjectsQuery.error, "profiles.error.load")
      : "";
  const viewLoadMoreError =
    viewObjectsQuery.isFetchNextPageError && viewObjectsQuery.error
      ? listLoadMoreErrorMessage(viewObjectsQuery.error, "profiles.error.load")
      : "";
  const tableOwnerTotals = useMemo(
    () =>
      Object.fromEntries(
        (schemaOwnersQuery.data?.owners ?? []).map((item) => [
          item.owner,
          item.table_count,
        ])
      ),
    [schemaOwnersQuery.data]
  );
  const viewOwnerTotals = useMemo(
    () =>
      Object.fromEntries(
        (schemaOwnersQuery.data?.owners ?? []).map((item) => [
          item.owner,
          item.view_count,
        ])
      ),
    [schemaOwnersQuery.data]
  );
  const selectProfile = (profile: ProfileSummary) => {
    setMessage("");
    setRefreshError("");
    setProfileSaveError("");
    setSearchParams({ profile: profile.id });
  };

  const load = async (announce = false, origin: "header" | "notice" = "header") => {
    setLoading("load");
    setLoadOrigin(origin);
    setRefreshError("");
    const results = await Promise.allSettled([
      profilesQuery.refetch(),
      tableObjectsQuery.refetch(),
      viewObjectsQuery.refetch(),
      schemaHeadQuery.refetch(),
      schemaOwnersQuery.refetch(),
    ]);
    const succeeded = results.every(
      (result) => result.status === "fulfilled" && !result.value.isError
    );
    if (!succeeded) {
      setRefreshError(t("profiles.error.load"));
    } else if (announce) {
      toast.success(t("common.action.refreshed"));
    }
    setLoading("");
  };

  const runSchemaRefresh = async () => {
    try {
      await sharedSchemaRefresh.start(PROFILE_SCHEMA_REFRESH_HEADER);
    } catch {
      // 共通 Coordinator が失敗状態と Toast を一度だけ管理する。
    }
  };

  // 編集対象の切替時にフォームと編集付帯 state を同期する(deep link 初回ロード後も含む)
  const editTargetKey = selectedProfile?.id ?? (profileParam === "new" ? "new" : "");
  const formInitializationKey = editTargetKey;
  if (useValuesChanged([formInitializationKey]) && formInitializationKey) {
    setForm(
      selectedProfile
        ? profileToForm(selectedProfile)
        : emptyProfileForm()
    );
    setNameError(null);
    setRequiredErrors({});
  }




  // 一覧・スキーマの読込エラーを render 中にメッセージへ反映する。
  if (
    useValuesChanged([
      profilesQuery.data,
      profilesQuery.error,
      schemaHeadQuery.data,
      schemaHeadQuery.error,
      tableObjectsQuery.data,
      tableObjectsQuery.error,
      viewObjectsQuery.data,
      viewObjectsQuery.error,
    ])
  ) {
    const error =
      (profilesQuery.error && !profilesQuery.data ? profilesQuery.error : null) ??
      (tableObjectsQuery.error && !tableObjectsQuery.data ? tableObjectsQuery.error : null) ??
      (viewObjectsQuery.error && !viewObjectsQuery.data ? viewObjectsQuery.error : null) ??
      (schemaHeadQuery.error && !schemaHeadQuery.data ? schemaHeadQuery.error : null);
    setMessage(error instanceof Error ? error.message : "");
  }


  const startNew = () => {
    setMessage("");
    setRefreshError("");
    setProfileSaveError("");
    setProfileSaveConflict(false);
    setSearchParams({ profile: "new" });
  };

  // dirty 判定: 読み込み時と同じ変換を再計算して比較する(追加 state 不要)。
  // 許可オブジェクトは選択順で配列が入れ替わるため集合として比較する。
  const isDirty = useMemo(() => {
    const baseline = selectedProfile
      ? profileToForm(selectedProfile)
      : emptyProfileForm();
    return !profileFormEquals(form, baseline);
  }, [form, selectedProfile]);

  const confirmDiscard = useCallback(
    () =>
      confirm({
        title: t("profiles.discard.confirm.title"),
        description: t("profiles.discard.confirm.description"),
        confirmLabel: t("profiles.discard.confirm.confirm"),
        tone: "danger",
        dismissOnOverlay: false,
      }),
    [confirm]
  );

  // 「一覧に戻る」だけでなく、サイドナビ等のルート遷移とタブを閉じる操作も保護する。
  useUnsavedChangesGuard(activeView === "editor" && isDirty, confirmDiscard);

  const backToList = async () => {
    if (mutationBusyRef.current) return;
    if (isDirty && !(await confirmDiscard())) return;
    setSearchParams({});
  };

  const toggleObject = (kind: "table" | "view", name: string) => {
    setForm((current) => {
      const key = kind === "table" ? "allowedTables" : "allowedViews";
      return { ...current, [key]: toggleObjectSelection(current[key], name) };
    });
  };

  const toggleSchemaSnapshot = async (
    kind: "table" | "view",
    owner: string,
    select: boolean
  ) => {
    if (mutationBusyRef.current) return;
    const target = profileParam;
    mutationBusyRef.current = true;
    setBulkSelecting(true);
    const key = kind === "table" ? "allowedTables" : "allowedViews";
    // 表示中の一覧と同じ条件で一括操作するため、デバウンス後の値を使う。
    const filter = objectFilter.trim();
    const filtered = Boolean(filter);
    try {
      // フィルタ適用中は「表示されている(=ヒットした)object」だけを一括対象にする。
      // 解除もスキーマ全体へ波及させないため、この場合は snapshot が必要。
      const snapshot =
        select || filtered
          ? await getSchemaObjectSnapshot(owner, kind === "table" ? "TABLE" : "VIEW", filter)
          : [];
      if (editTargetRef.current !== target) return;
      setForm((current) => ({
        ...current,
        [key]: applySchemaBulkSelection({
          current: current[key],
          snapshot,
          ownerPrefix: `${owner}.`,
          select,
          filtered,
        }),
      }));
    } catch (error) {
      if (editTargetRef.current === target) toast.error(error instanceof Error ? error.message : t("profiles.error.load"));
    } finally {
      setBulkSelecting(false);
    }
  };

  const save = async () => {
    if (mutationBusyRef.current) return;
    const target = profileParam;
    const nextNameError = profileNameError(form.name);
    const nextRequiredErrors = profileRequiredErrors(form);
    setNameError(nextNameError);
    setRequiredErrors(nextRequiredErrors);
    if (nextNameError || hasProfileRequiredErrors(nextRequiredErrors)) {
      return;
    }
    setNameError(null);
    setRequiredErrors({});
    setProfileSaveError("");
    setProfileSaveConflict(false);
    mutationBusyRef.current = true;
    setLoading("save");
    try {
      const payload = formToPayload(form);
      const saved = selectedProfile
        ? await apiPatch<Nl2SqlProfile>(
            `/api/nl2sql/profiles/${selectedProfile.id}`,
            payload,
            { "If-Match": `"${profileDetailQuery.data?.etag || selectedProfile.etag || ""}"` }
          )
        : await apiPost<Nl2SqlProfile>("/api/nl2sql/profiles", payload);
      queryClient.setQueryData(nl2sqlIncrementalKeys.profile(saved.id), {
        profile: saved,
        etag: saved.etag ?? "",
      });
      void queryClient.invalidateQueries({ queryKey: ["nl2sql", "profiles", "search"] });
      toast.success(t("profiles.message.saved"));
      if (isProfileSaveTargetCurrent(editTargetRef.current, target, saved.id)) {
        setForm(profileToForm(normalizeProfile(saved)));
        setRequiredErrors({});
        if (!selectedProfile) {
          setSearchParams({ profile: saved.id }, { replace: true });
        }
      }
    } catch (err) {
      if (isProfileNameConflictError(err)) {
        setNameError("duplicate");
        setNameFocusRequest((current) => current + 1);
        return;
      }
      if (editTargetRef.current === target) {
        // 既存の業務プロファイルの更新で、名称の重複以外の 409 は、ほかの保存で版（ETag）が進んだ競合。
        const conflict = Boolean(selectedProfile) && err instanceof ApiError && err.status === 409;
        setProfileSaveConflict(conflict);
        setProfileSaveError(
          conflict
            ? t("profiles.error.saveConflict")
            : err instanceof Error && err.message
              ? err.message
              : t("profiles.error.save")
        );
      }
    } finally {
      setLoading("");
    }
  };

  // 「変更を破棄」: 最後に保存した内容（新規作成は空のフォーム）へ戻す。
  const discardChanges = () => {
    if (mutationBusyRef.current) return;
    setForm(selectedProfile ? profileToForm(selectedProfile) : emptyProfileForm());
    setNameError(null);
    setRequiredErrors({});
    setProfileSaveError("");
    setProfileSaveConflict(false);
  };

  // 競合した保存の後に、最新の版を読み直して編集の内容を置き換える（未保存の変更は確認の後に破棄する）。
  const reloadLatestProfile = async () => {
    if (mutationBusyRef.current || !selectedProfile) return;
    const target = profileParam;
    const ok = await confirm({
      title: t("profiles.conflict.reload.confirm.title"),
      description: t("profiles.conflict.reload.confirm.description"),
      confirmLabel: t("profiles.conflict.reload.confirm.confirm"),
      tone: "danger",
      dismissOnOverlay: false,
    });
    if (!ok || editTargetRef.current !== target) return;
    mutationBusyRef.current = true;
    setLoading("reload-latest");
    try {
      const latest = await profileDetailQuery.refetch();
      if (editTargetRef.current !== target) return;
      if (latest.isError || !latest.data) {
        setProfileSaveError(apiErrorMessage(latest.error, t("profiles.error.load")));
        return;
      }
      setForm(profileToForm(latest.data.profile));
      setNameError(null);
      setRequiredErrors({});
      setProfileSaveError("");
      setProfileSaveConflict(false);
    } finally {
      setLoading("");
    }
  };

  const deleteProfile = async (profile: Pick<Nl2SqlProfile, "id" | "name" | "etag">) => {
    if (mutationBusyRef.current) return;
    const ok = await confirm({
      title: t("profiles.delete.confirm.title"),
      description: t("profiles.delete.confirm.description", { name: profile.name }),
      confirmLabel: t("common.delete"),
      tone: "danger",
      dismissOnOverlay: false,
    });
    if (!ok) return;

    setLoading(`delete-profile-${profile.id}`);
    try {
      const deleted = await apiDelete<ProfileDeleteData>(
        `/api/nl2sql/profiles/${encodeURIComponent(profile.id)}`,
        { "If-Match": `"${profile.etag || profileDetailQuery.data?.etag || ""}"` }
      );
      const deletedProfile = deleted.profile;
      queryClient.removeQueries({ queryKey: nl2sqlIncrementalKeys.profile(deletedProfile.id) });
      await queryClient.invalidateQueries({ queryKey: ["nl2sql", "profiles", "search"] });
      if (editTargetRef.current === profile.id) {
        setSearchParams({}, { replace: true });
      }
      toast.success(t("profiles.message.deleted", { name: deletedProfile.name }));
    } catch (err) {
      toast.error(err instanceof Error && err.message ? err.message : t("profiles.error.delete"));
    } finally {
      setLoading("");
    }
  };

  const toggleSort = (key: ProfileListSortKey) => {
    setProfileSort((current) => ({
      key,
      direction: current.key === key && current.direction === "asc" ? "desc" : "asc",
    }));
  };
  const clearRequiredError = useCallback((field: ProfileRequiredField) => {
    setRequiredErrors((current) => {
      if (!current[field]) return current;
      const next = { ...current };
      delete next[field];
      return next;
    });
  }, []);

  const editor = (
    <ProfileEditor
      selectedProfile={selectedProfile}
      profileAccessProfile={selectedProfileAccessProfile}
      form={form}
      tableObjects={tableObjects}
      viewObjects={viewObjects}
      tableObjectTotal={tableObjectTotal}
      viewObjectTotal={viewObjectTotal}
      tableOwnerTotals={tableOwnerTotals}
      viewOwnerTotals={viewOwnerTotals}
      tableObjectsLoading={tableObjectsQuery.isPending}
      viewObjectsLoading={viewObjectsQuery.isPending}
      tableHasNextPage={Boolean(tableObjectsQuery.hasNextPage)}
      viewHasNextPage={Boolean(viewObjectsQuery.hasNextPage)}
      tableLoadingNextPage={tableObjectsQuery.isFetchingNextPage}
      viewLoadingNextPage={viewObjectsQuery.isFetchingNextPage}
      tableLoadMoreError={tableLoadMoreError}
      viewLoadMoreError={viewLoadMoreError}
      objectFilter={objectFilter}
      saving={loading === "save"}
      busy={mutationBusy}
      nameError={nameError}
      requiredErrors={requiredErrors}
      dirty={isDirty}
      saveError={profileSaveError}
      saveConflict={profileSaveConflict}
      reloadingLatest={loading === "reload-latest"}
      onReloadLatest={() => void reloadLatestProfile()}
      deleting={selectedProfile ? loading === `delete-profile-${selectedProfile.id}` : false}
      onObjectFilterChange={setObjectFilter}
      onFormChange={setForm}
      onToggleTable={(name) => toggleObject("table", name)}
      onToggleView={(name) => toggleObject("view", name)}
      onToggleTableSchema={(owner, select) =>
        toggleSchemaSnapshot("table", owner, select)
      }
      onToggleViewSchema={(owner, select) =>
        toggleSchemaSnapshot("view", owner, select)
      }
      onLoadMoreTables={() => void tableObjectsQuery.fetchNextPage()}
      onLoadMoreViews={() => void viewObjectsQuery.fetchNextPage()}
      onRetryLoadMoreTables={() => void tableObjectsQuery.fetchNextPage()}
      onRetryLoadMoreViews={() => void viewObjectsQuery.fetchNextPage()}
      onNameErrorClear={() => setNameError(null)}
      onRequiredErrorClear={clearRequiredError}
      onSave={() => void save()}
      onDelete={() => {
        if (selectedProfile) void deleteProfile(selectedProfile);
      }}
      onDiscard={discardChanges}
    />
  );
  const schemaRefreshing = sharedSchemaRefresh.isRefreshing;
  // スキーマの更新・業務プロファイルの再取得は durable job。開始のボタンは押した側だけを送信の間 loading にし、
  // job の間は無効にするだけ。job の間のスピナーは一覧の上の進行の表示が 1 つだけ出す（messaging §3.7、#821）。
  const headerSchemaRefreshStarting = sharedSchemaRefresh.startingOrigin === PROFILE_SCHEMA_REFRESH_HEADER;
  const profileListRefreshing = profilesQuery.isFetching && !profilesQuery.isFetchingNextPage;
  const profileWorkspaceProcessing = schemaRefreshing ? (
      <SchemaRefreshProcessing testId="profile-management-workspace-processing" />
    ) : loading === "load" || profileListRefreshing ? (
      <ProcessingIndicator
        active
        label={t("common.processing.refreshing")}
        operationKey="profile-refresh"
        placement="workspace"
        className="rounded-md border border-border bg-surface-sunken px-3 py-2"
        testId="profile-management-workspace-processing"
        // 押した「最新情報を取得」が回っている間はスピナーを出さない。定期・他の操作の後の一覧の取り直しの間は
        // ボタンを回さないため、この表示がスピナーを出す（#819 / #821）。
        activityIcon={loading === "load" ? "none" : "spinner"}
      />
    ) : undefined;
  const showProfileWorkspaceProcessing =
    Boolean(profileWorkspaceProcessing) &&
    (profiles.length > 0 || loading === "load" || schemaRefreshing);
  const profileListLoading = !profilesLoaded || (loading === "load" && profiles.length === 0);
  // 初回の読込は一覧の読込表示がスピナーを出す。ヘッダーの「最新情報を取得」は回さない（狭い画面では「その他の操作」の
  // 中で見えない。同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
  const profileListShowsSpinner = profileListLoading && !showProfileWorkspaceProcessing;
  const workspaceNotice = sharedSchemaRefresh.error
      ? { tone: "danger" as const, message: sharedSchemaRefresh.error }
    : refreshError
      ? { tone: "danger" as const, message: `${refreshError} ${t("profiles.error.retryHint")}` }
    : message
      ? { tone: "danger" as const, message: `${message} ${t("profiles.error.retryHint")}` }
      : null;
  const workspaceNoticeAction = (
      <Button
        type="button"
        variant="secondary"
        size="sm"
        // 押した再読込の間だけ回す。ヘッダーの「最新情報を取得」の再読込の間は無効にするだけ（#819）。
        loading={loading === "load" && loadOrigin === "notice"}
        disabled={mutationBusy || (loading === "load" && loadOrigin !== "notice")}
        onClick={() => void load(false, "notice")}
        icon={RefreshCw}
      >
        <span>{t("common.retry")}</span>
      </Button>
    );

  return (
    <>
      <PageHeader wide
        title={t("nav.profiles")}
        subtitle={t("profiles.subtitle")}
        status={
          activeView === "list" && (schemaRefreshing || sharedSchemaRefresh.error) ? (
            <SchemaRefreshHeaderStatus testId="profile-management-schema-refresh-status" />
          ) : undefined
        }
        actions={
          activeView === "list"
            ? [
                {
                  id: "create-profile",
                  kind: "primary",
                  label: t("profiles.action.new"),
                  icon: Plus,
                  onClick: startNew,
                },
                {
                  id: "refresh",
                  kind: "utility",
                  label: t("common.action.refresh"),
                  icon: RefreshCw,
                  onClick: () => load(true, "header"),
                  // 押した再読込の間だけ回す。定期・他の操作の後の一覧の取り直し（profileListRefreshing）と、
                  // 失敗の案内の「再試行」の再読込では回さない（#819）。
                  loading: loading === "load" && loadOrigin === "header" && !profileListShowsSpinner,
                  disabled: profileListShowsSpinner || (loading === "load" && loadOrigin !== "header"),
                },
                {
                  id: "schema-refresh",
                  kind: "utility",
                  label: t("common.action.schemaRefresh"),
                  icon: RefreshCw,
                  onClick: runSchemaRefresh,
                  loading: headerSchemaRefreshStarting,
                  disabled: schemaRefreshing && !headerSchemaRefreshStarting,
                },
              ]
            : []
        }
        actionsTestId="profile-management-actions"
        // 編集の画面の「一覧へ戻る」はページの左上（#618）。保存はフォームの末尾の FormActionBar（#1786）。
        back={
          activeView === "list"
            ? undefined
            : {
                label: t("profiles.action.backToList"),
                onClick: () => void backToList(),
                disabled: mutationBusy,
                testId: "profile-management-back",
              }
        }
      />

      <PageBody wide className="grid gap-4">
        <PageNotice
          notice={workspaceNotice}
          action={workspaceNoticeAction}
        />

        {activeView === "list" ? (
          <DbObjectManagementPanelShell
              id="profile-management-panel-list"
              role="region"
              idPrefix="profile-management"
              ariaLabel={t("profiles.workspace.label")}
              processing={showProfileWorkspaceProcessing ? profileWorkspaceProcessing : undefined}
            >
              <ProfileList
                profiles={profiles}
                totalCount={profileTotal}
                unfilteredTotal={unfilteredProfileTotal}
                selectedProfileId={selectedProfileId}
                loading={profileListLoading}
                loadingIndicator={!showProfileWorkspaceProcessing}
                search={profileSearch}
                sort={profileSort}
                onSearchChange={setProfileSearch}
                onSortChange={toggleSort}
                onSelect={selectProfile}
                profileHref={(profile) => appPath(`${location.pathname}?${new URLSearchParams({ profile: profile.id })}`)}
                hasNextPage={Boolean(profilesQuery.hasNextPage)}
                loadingNextPage={profilesQuery.isFetchingNextPage}
                loadMoreError={profileLoadMoreError}
                onLoadMore={() => void profilesQuery.fetchNextPage()}
                onRetryLoadMore={() => void profilesQuery.fetchNextPage()}
              />
            </DbObjectManagementPanelShell>
        ) : (
          <>
            <DbObjectManagementPanelShell
              id="profile-management-panel-editor"
              role="region"
              idPrefix="profile-management"
              ariaLabel={selectedProfile ? t("profiles.editor.edit") : t("profiles.editor.new")}
              processing={profileWorkspaceProcessing}
            >
              {selectedProfile || profileParam === "new" ? (
                editor
              ) : profileDetailQuery.isError ? (
                <ErrorState
                  message={profileDetailQuery.error instanceof Error ? profileDetailQuery.error.message : t("profiles.error.load")}
                  onRetry={() => void profileDetailQuery.refetch()}
                />
              ) : (
                <TimedLoadingState
                  label={t("profiles.detail.loading")}
                  operationKey="profile-detail-load"
                  framed={false}
                  testId="profile-editor-loading"
                >
                  <FormSkeleton fields={5} testId="profile-editor-skeleton" />
                </TimedLoadingState>
              )}
            </DbObjectManagementPanelShell>
          </>
        )}
      </PageBody>
    </>
  );
}
