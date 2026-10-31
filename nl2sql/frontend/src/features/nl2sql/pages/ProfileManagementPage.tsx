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
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

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
  type SelectFieldOption,
  ClearActionButton,
  ProcessingIndicator,
  ObjectActionBar,
  TimedLoadingState,
  TableSkeleton,
  FormSkeleton,
  ExecutionConfirmationField,
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

import { PageHeaderStatusBadge } from "@/components/PageHeaderStatusBadge";
import { PageNotice } from "@/components/page-notice";
import { ApiError, apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api";
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
import { ProfileSaveProgress } from "../components/ProfileSaveProgress";
import {
  DbProfileListSyncNotice,
  DbProfileListSyncProcessing,
} from "../components/DbProfileListSyncNotice";
import { useDbProfileListSync, type DbProfileListSync } from "../useDbProfileListSync";
import { SelectAiCredentialPrecondition } from "../components/SelectAiCredentialPrecondition";
import { useSelectAiCredentialReadiness } from "../useSelectAiCredentialReadiness";
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
import { BUSINESS_SELECT_AI_DB_PROFILES_URL } from "../selectAiProfileUrls";
import { schemaTableQualifiedName } from "../workbenchState";
import { DbObjectName } from "../components/DbObjectName";
import { formatDbObjectName, formatDbObjectPart } from "../dbObjectIdentity";
import { NL2SQL_ENGINES_URL, defaultEngineOptions } from "../profileEngineOptions";
import { isProfileSaveTargetCurrent } from "../profileSaveTarget";
import type {
  ClarificationPolicy,
  Nl2SqlEngine,
  Nl2SqlEnginesData,
  Nl2SqlProfile,
  ProfileDeleteData,
  ProfileSummary,
  ProfileSelectAiConfig,
  ProfileSyncJobData,
  ProfileUpsertPayload,
  SchemaObjectSummary,
  SchemaTable,
  SelectAiDbProfilesData,
} from "../types";

type ActiveView = "list" | "editor";
type ProfileNameError = "required" | "format" | "duplicate" | null;
type ProfileRequiredField =
  | "category"
  | "region"
  | "model"
  | "maxTokens"
  | "embeddingModel";
type ProfileRequiredErrors = Partial<Record<ProfileRequiredField, true>>;

const SELECT_AI_MAX_TOKENS_MIN = 4096;
const SELECT_AI_MAX_TOKENS_MAX = 32000;
const SELECT_AI_DEFAULT_REGION = "us-chicago-1";
const SELECT_AI_REGION_OPTIONS = [
  { value: "us-chicago-1", label: "us-chicago-1" },
  { value: "ap-osaka-1", label: "ap-osaka-1" },
] as const satisfies readonly SelectFieldOption<string>[];
const PROFILE_NAME_PATTERN = /^[A-Z][A-Z0-9_]*$/u;
/** ヘッダーの「スキーマを更新」の起点（SchemaRefreshCoordinator の start(origin)。#821）。 */
const PROFILE_SCHEMA_REFRESH_HEADER = "profile-management-header";
/** 「保存」の aria-describedby が指す、Select AI の Credential の事前条件の案内（#1683）。 */
const PROFILE_SAVE_CREDENTIAL_PRECONDITION_ID = "profile-save-credential-precondition";

interface ProfileFormState {
  name: string;
  category: string;
  allowedTables: string[];
  allowedViews: string[];
  selectAiConfig: ProfileSelectAiConfig;
  /** 既定の生成方式（#1524）。 */
  defaultEngine: Nl2SqlEngine;
  /**
   * 保存（Oracle 反映）のたびに Select AI Agent アセットも再構築するか（#1690）。業務プロファイルに
   * 保存する設定で、開き直しても保つ（「実行設定・同期表示をリセット」の対象ではない）。
   */
  rebuildAgentAssetsOnSave: boolean;
  /** 回答前の確認の方針（#1839）。 */
  clarificationPolicy: ClarificationPolicy;
  /** 確認の上限（往復）。0 はシステムの既定。 */
  clarificationMaxRounds: number;
}

/** 確認の上限（往復）の選択肢。0 はシステムの既定（#1839）。 */
const CLARIFICATION_MAX_ROUNDS_OPTIONS = [0, 2, 3, 4, 5, 6] as const;


const DEFAULT_SELECT_AI_CONFIG: ProfileSelectAiConfig = {
  profile_name: "",
  region: SELECT_AI_DEFAULT_REGION,
  model: "xai.grok-4.6",
  embedding_model: "cohere.embed-v4.0",
  max_tokens: SELECT_AI_MAX_TOKENS_MAX,
  enforce_object_list: true,
  comments: true,
  // ANNOTATIONS / ドメイン継承 annotation を Select AI の prompt へ渡す(既定 false だと効かない)。
  annotations: true,
  constraints: true,
  role: "",
  additional_instructions: "",
};

const EMPTY_FORM: ProfileFormState = {
  name: "",
  category: "",
  allowedTables: [],
  allowedViews: [],
  selectAiConfig: DEFAULT_SELECT_AI_CONFIG,
  defaultEngine: "select_ai",
  rebuildAgentAssetsOnSave: false,
  clarificationPolicy: "confirm",
  clarificationMaxRounds: 0,
};

function emptyProfileForm(): ProfileFormState {
  return {
    ...EMPTY_FORM,
    allowedTables: [],
    allowedViews: [],
    selectAiConfig: { ...DEFAULT_SELECT_AI_CONFIG },
  };
}

function mergeAdditionalInstructions(instructions: string, rules: string[]) {
  const base = instructions.trim();
  const seen = new Set(base.split("\n").map((line) => line.trim()).filter(Boolean));
  const additions = rules
    .map((rule) => rule.trim())
    .filter((rule) => {
      if (!rule || seen.has(rule)) return false;
      seen.add(rule);
      return true;
    });
  return [base, ...additions].filter(Boolean).join("\n");
}

function normalizeSelectAiMaxTokens(value: unknown) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return SELECT_AI_MAX_TOKENS_MAX;
  return Math.min(SELECT_AI_MAX_TOKENS_MAX, Math.max(SELECT_AI_MAX_TOKENS_MIN, Math.trunc(numeric)));
}

function normalizeSelectAiRegion(value: unknown) {
  const region = typeof value === "string" ? value.trim() : "";
  return SELECT_AI_REGION_OPTIONS.some((option) => option.value === region)
    ? region
    : SELECT_AI_DEFAULT_REGION;
}

function parseMaxTokensInput(value: string) {
  const numeric = Number(value);
  return value.trim() && Number.isFinite(numeric) ? numeric : SELECT_AI_MAX_TOKENS_MAX;
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
  if (!form.selectAiConfig.region.trim()) errors.region = true;
  if (!form.selectAiConfig.model.trim()) errors.model = true;
  if (!Number.isFinite(Number(form.selectAiConfig.max_tokens))) errors.maxTokens = true;
  if (!form.selectAiConfig.embedding_model.trim()) errors.embeddingModel = true;
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
  const selectAiConfig = { ...DEFAULT_SELECT_AI_CONFIG, ...profile.select_ai_config };
  return {
    ...profile,
    allowed_tables: (profile.allowed_tables ?? []).filter(isUserVisibleObjectName),
    allowed_views: (profile.allowed_views ?? []).filter(isUserVisibleObjectName),
    select_ai_config: {
      ...selectAiConfig,
      region: normalizeSelectAiRegion(selectAiConfig.region),
      max_tokens: normalizeSelectAiMaxTokens(selectAiConfig.max_tokens),
    },
  };
}

function profileToForm(profile: Nl2SqlProfile): ProfileFormState {
  const normalized = normalizeProfile(profile);
  const selectAiConfig: ProfileSelectAiConfig = {
    ...normalized.select_ai_config,
    additional_instructions: mergeAdditionalInstructions(
      normalized.select_ai_config.additional_instructions,
      normalized.sql_rules
    ),
  };
  // 旧名の印（previous_profile_name）は名称の変更の Oracle 反映のために backend が付け外しする値で、
  // 利用者は編集しない（PATCH も受け付けない）。フォームに持つと、反映の job が印を消した後の
  // 最新版と比べて「未保存の変更」に見えるため、フォームと未保存の判定から外す。
  delete selectAiConfig.previous_profile_name;
  return {
    name: normalized.name,
    category: normalized.category ?? "",
    allowedTables: normalized.allowed_tables,
    allowedViews: normalized.allowed_views,
    selectAiConfig,
    defaultEngine: normalized.default_engine ?? "select_ai",
    rebuildAgentAssetsOnSave: normalized.rebuild_agent_assets_on_save ?? false,
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
    sql_rules: [],
    safety_policy: "select_only",
    default_engine: form.defaultEngine,
    rebuild_agent_assets_on_save: form.rebuildAgentAssetsOnSave,
    clarification_policy: form.clarificationPolicy,
    clarification_max_rounds: form.clarificationMaxRounds,
    select_ai_config: {
      ...form.selectAiConfig,
      profile_name: profileName,
      region: normalizeSelectAiRegion(form.selectAiConfig.region),
      model: form.selectAiConfig.model.trim(),
      embedding_model: form.selectAiConfig.embedding_model.trim() || "cohere.embed-v4.0",
      max_tokens: normalizeSelectAiMaxTokens(form.selectAiConfig.max_tokens),
      role: form.selectAiConfig.role.trim(),
      additional_instructions: form.selectAiConfig.additional_instructions.trim(),
    },
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

function updateSelectAiConfig(
  setForm: (updater: (current: ProfileFormState) => ProfileFormState) => void,
  patch: Partial<ProfileSelectAiConfig>
) {
  setForm((current) => ({
    ...current,
    selectAiConfig: { ...current.selectAiConfig, ...patch },
  }));
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

function SelectAiConfigFields({
  form,
  setForm,
  requiredErrors,
  onRequiredErrorClear,
}: {
  form: ProfileFormState;
  setForm: (updater: (current: ProfileFormState) => ProfileFormState) => void;
  requiredErrors: ProfileRequiredErrors;
  onRequiredErrorClear: (field: ProfileRequiredField) => void;
}) {
  return (
    <section
      id="profile-select-ai"
      className="grid scroll-mt-4 gap-3 rounded-md border border-border bg-surface-sunken p-3"
      aria-label={t("profiles.editor.selectAi")}
      tabIndex={-1}
    >
      {/* 共通のモデル設定のうち Select AI に効くもの・効かないものの違い（#1434）。保存の影響なので常設する
          （messaging.md §3.8）。推論の量は AI profile に属性がなく効かない。 */}
      <p className="text-xs leading-relaxed text-fg-muted" data-testid="profile-select-ai-reasoning-note">
        {t("profiles.selectAi.reasoningNote")}
      </p>
      {/* 欄は値の種類の幅（リージョン sm・モデル lg・数値 xs。#1891）で、FormGrid（最大 60rem）に並べる。 */}
      <FormGrid>
        <SelectField
          id="profile-select-ai-region"
          label={t("profiles.field.region")}
          width="sm"
          value={form.selectAiConfig.region}
          options={SELECT_AI_REGION_OPTIONS}
          required
          error={requiredErrors.region ? t("profiles.error.regionRequired") : undefined}
          onValueChange={(value) => {
            updateSelectAiConfig(setForm, { region: value });
            if (requiredErrors.region) onRequiredErrorClear("region");
          }}
          className="min-w-0"
        />
        <TextField
          id="profile-select-ai-model"
          label={t("profiles.field.model")}
          width="lg"
          required
          value={form.selectAiConfig.model}
          error={requiredErrors.model ? t("profiles.error.modelRequired") : undefined}
          onValueChange={(value) => {
            updateSelectAiConfig(setForm, { model: value });
            if (requiredErrors.model) onRequiredErrorClear("model");
          }}
          className="min-w-0"
        />
        <TextField
          id="profile-select-ai-max-tokens"
          label={t("profiles.field.maxTokens")}
          width="xs"
          required
          type="number"
          min={SELECT_AI_MAX_TOKENS_MIN}
          max={SELECT_AI_MAX_TOKENS_MAX}
          step={1}
          value={form.selectAiConfig.max_tokens}
          error={requiredErrors.maxTokens ? t("profiles.error.maxTokensRequired") : undefined}
          onValueChange={(value) => {
            updateSelectAiConfig(setForm, { max_tokens: parseMaxTokensInput(value) });
            if (requiredErrors.maxTokens) onRequiredErrorClear("maxTokens");
          }}
          onBlur={(event) =>
            updateSelectAiConfig(setForm, {
              max_tokens: normalizeSelectAiMaxTokens(event.currentTarget.value),
            })
          }
          className="min-w-0"
        />
        <TextField
          id="profile-select-ai-embedding-model"
          label={t("profiles.field.embeddingModel")}
          width="lg"
          required
          value={form.selectAiConfig.embedding_model}
          error={
            requiredErrors.embeddingModel ? t("profiles.error.embeddingModelRequired") : undefined
          }
          onValueChange={(value) => {
            updateSelectAiConfig(setForm, { embedding_model: value });
            if (requiredErrors.embeddingModel) onRequiredErrorClear("embeddingModel");
          }}
          className="min-w-0"
        />
      </FormGrid>
      <div className="grid gap-2 md:grid-cols-4">
        {([
          ["enforce_object_list", "profiles.field.enforceObjectList"],
          ["comments", "profiles.field.comments"],
          ["annotations", "profiles.field.annotations"],
          ["constraints", "profiles.field.constraints"],
        ] as const).map(([key, labelKey]) => (
          <label key={key} className="flex min-h-11 items-center gap-2 rounded-md border border-border bg-surface p-3 text-sm text-fg">
            <input
              type="checkbox"
              checked={Boolean(form.selectAiConfig[key])}
              onChange={(event) => updateSelectAiConfig(setForm, { [key]: event.currentTarget.checked })}
              className="h-4 w-4 rounded border-border text-accent-fg"
            />
            <span>{t(labelKey)}</span>
          </label>
        ))}
      </div>
      {/* 文章の欄は full（FormGrid の 60rem まで）で 1 列に積む。 */}
      <FormGrid columns={1}>
        <TextareaField
          id="profile-select-ai-role"
          label={t("profiles.field.role")}
          width="full"
          helper={t("profiles.field.roleHint")}
          value={form.selectAiConfig.role}
          rows={6}
          onChange={(event) => updateSelectAiConfig(setForm, { role: event.currentTarget.value })}
          placeholder={t("profiles.placeholder.role")}
        />
        <TextareaField
          id="profile-select-ai-additional-instructions"
          label={t("profiles.field.additionalInstructions")}
          width="full"
          helper={t("profiles.field.additionalInstructionsHint")}
          value={form.selectAiConfig.additional_instructions}
          rows={6}
          onChange={(event) =>
            updateSelectAiConfig(setForm, {
              additional_instructions: event.currentTarget.value,
            })
          }
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
  engineOptions,
  oracleConfirmation,
  rebuildAgentAssets,
  canClearOracleExecution,
  oracleSyncJob,
  oracleSyncSubmissionError,
  oracleSyncSubmissionErrorCode,
  selectAiCredentialMissing,
  selectAiCredentialName,
  canOpenSelectAiCredential,
  recheckingSelectAiCredential,
  onRecheckSelectAiCredential,
  saveError,
  saveConflict,
  reloadingLatest,
  onReloadLatest,
  retryingOracleSync,
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
  onOracleConfirmationChange,
  onOracleExecutionClear,
  onRebuildAgentAssetsChange,
  onRetryOracleSync,
  dbProfileListSync,
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
  /** 既定の生成方式の選択肢（この環境で使える方式。#1524）。 */
  engineOptions: readonly SelectFieldOption<Nl2SqlEngine>[];
  oracleConfirmation: string;
  rebuildAgentAssets: boolean;
  canClearOracleExecution: boolean;
  oracleSyncJob: ProfileSyncJobData | null;
  oracleSyncSubmissionError: string;
  oracleSyncSubmissionErrorCode: string;
  /** 保存（Oracle 反映）の事前条件の Select AI の Credential が無いと分かった（#1683）。 */
  selectAiCredentialMissing: boolean;
  selectAiCredentialName: string;
  /** 運用設定の Select AI Credential 管理を開けるか（無ければリンクの代わりに管理者への依頼を案内する）。 */
  canOpenSelectAiCredential: boolean;
  recheckingSelectAiCredential: boolean;
  onRecheckSelectAiCredential: () => void;
  /** プロファイルの保存（PATCH / POST）の失敗。保存ボタンの直下だけに出す（messaging.md §3.3.1。#585）。 */
  saveError: string;
  /** 保存の失敗がほかの更新との競合（409）。失敗の文の下に「最新の内容を読み込む」を出す（#1111）。 */
  saveConflict: boolean;
  reloadingLatest: boolean;
  onReloadLatest: () => void;
  retryingOracleSync: boolean;
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
  onOracleConfirmationChange: (value: string) => void;
  onOracleExecutionClear: () => void;
  onRebuildAgentAssetsChange: (value: boolean) => void;
  onRetryOracleSync: () => void;
  /** 反映の後の DB Profile 一覧の同期（案内・進行は保存の結果の直下に出す。#2034）。 */
  dbProfileListSync: DbProfileListSync;
}) {
  const oracleConfirmed = oracleConfirmation.trim() === "ADMIN_EXECUTE";
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
          {/* 既定の生成方式（#1524）。SQL 生成・チャットの初期の選択と、方式を指定しない問い合わせ（Agent の MCP）が使う。 */}
          <SelectField
            id="profile-default-engine"
            label={t("profiles.field.defaultEngine")}
            value={form.defaultEngine}
            options={engineOptions}
            width="md"
            helper={t("profiles.field.defaultEngineHint")}
            onValueChange={(value) => onFormChange((current) => ({ ...current, defaultEngine: value }))}
            className="order-4 mt-2 min-w-0 @2xl:order-none"
          />
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

      <SelectAiConfigFields
        form={form}
        setForm={onFormChange}
        requiredErrors={requiredErrors}
        onRequiredErrorClear={onRequiredErrorClear}
      />

      <section className="grid gap-3 rounded-md border border-border bg-surface p-3" aria-labelledby="profile-engine-assets-heading">
        <div>
          <h3 id="profile-engine-assets-heading" className="text-sm font-semibold text-fg">
            {t("profiles.oracle.assets.title")}
          </h3>
          <p className="mt-1 text-sm text-fg-muted">{t("profiles.oracle.assets.hint")}</p>
        </div>
        <label className="flex min-h-11 items-center gap-2 rounded-md border border-border bg-surface-sunken p-3 text-sm text-fg">
          <input
            type="checkbox"
            checked={rebuildAgentAssets}
            onChange={(event) => onRebuildAgentAssetsChange(event.currentTarget.checked)}
            className="h-4 w-4 rounded border-border text-accent-fg"
          />
          <span>{t("profiles.oracle.assets.refreshAgent")}</span>
        </label>
      </section>

      {/* 保存（Oracle 反映）の事前条件。保存の操作の直前に出し、満たすまで「保存」を無効にする
          （DeepSec の事前条件と同じ出し方。messaging.md §10。#1683）。 */}
      {selectAiCredentialMissing ? (
        <SelectAiCredentialPrecondition
          id={PROFILE_SAVE_CREDENTIAL_PRECONDITION_ID}
          testId="profile-save-credential-precondition"
          operation={t("profiles.oracle.precondition.operation")}
          action={t("profiles.action.save")}
          credentialName={selectAiCredentialName}
          canOpenSettings={canOpenSelectAiCredential}
          rechecking={recheckingSelectAiCredential}
          onRecheck={onRecheckSelectAiCredential}
        />
      ) : null}
      <ExecutionConfirmationField
        value={oracleConfirmation}
        onChange={onOracleConfirmationChange}
        confirmed={oracleConfirmed}
        placeholder="ADMIN_EXECUTE"
        expectedLabel="ADMIN_EXECUTE"
        helper={t("profiles.oracle.executeHint")}
        actions={
          <>
            <Button
              type="button"
              variant="primary"
              size="lg"
              loading={saving}
              disabled={!oracleConfirmed || saving || selectAiCredentialMissing}
              aria-describedby={
                selectAiCredentialMissing ? PROFILE_SAVE_CREDENTIAL_PRECONDITION_ID : undefined
              }
              onClick={onSave} icon={Save}>
              <span>{t("profiles.action.save")}</span>
            </Button>
            <ClearActionButton
              label={t("profiles.oracle.actions.clear")}
              size="lg"
              className="w-full sm:w-auto"
              disabled={!canClearOracleExecution || saving}
              onClick={onOracleExecutionClear}
            />
          </>
        }
      />

      {/* 保存ボタンはフォームの中（確認語と並ぶ）なので、欄に結び付かない保存の失敗はボタンの直下の
          FormStatus の 1 か所だけに出す（Toast に重ねない。messaging.md §3.3.1。#585）。 */}
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
      <ProfileSaveResultRegion
        rebuildAgentAssets={rebuildAgentAssets}
        oracleSyncJob={oracleSyncJob}
        oracleSyncSubmissionError={oracleSyncSubmissionError}
        oracleSyncSubmissionErrorCode={oracleSyncSubmissionErrorCode}
        retryingOracleSync={retryingOracleSync}
        onRetryOracleSync={onRetryOracleSync}
        dbProfileListSync={dbProfileListSync}
      />
    </fieldset>
  );
}

function ProfileSaveResultRegion({
  rebuildAgentAssets,
  oracleSyncJob,
  oracleSyncSubmissionError,
  oracleSyncSubmissionErrorCode,
  retryingOracleSync,
  onRetryOracleSync,
  dbProfileListSync,
}: {
  rebuildAgentAssets: boolean;
  oracleSyncJob: ProfileSyncJobData | null;
  oracleSyncSubmissionError: string;
  oracleSyncSubmissionErrorCode: string;
  retryingOracleSync: boolean;
  onRetryOracleSync: () => void;
  dbProfileListSync: DbProfileListSync;
}) {
  if (!oracleSyncJob && !oracleSyncSubmissionError) {
    return null;
  }
  return (
    <section className="min-w-0" data-testid="profile-save-result-region">
      <ProfileSaveProgress
        job={oracleSyncJob}
        submissionError={oracleSyncSubmissionError}
        submissionErrorCode={oracleSyncSubmissionErrorCode}
        // 工程の表示は、実行したジョブが再構築を含むかで決める（保存の後にチェックを変えても変えない）。
        rebuildAgentAssets={oracleSyncJob?.rebuild_agent_assets ?? rebuildAgentAssets}
        retrying={retryingOracleSync}
        onRetry={onRetryOracleSync}
      />
      {/* 反映の後の DB Profile 一覧の同期（差分同期の進行・再取得の案内）は、反映の結果の直下に 1 か所だけ出す。
          案内の「DB Profile 一覧を再取得」の進行は、そのボタンの直下に出す（messaging §10.2、#2034）。 */}
      <DbProfileListSyncNotice
        sync={dbProfileListSync}
        className="mt-3"
        testId="profile-db-profile-sync-notice"
        actionTestId="profile-db-profile-sync-refresh"
        processingTestId="profile-db-profile-sync-processing"
      />
    </section>
  );
}

export function ProfileManagementPage() {
  const location = useLocation();
  const navigate = useNavigate();
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
  const [oracleConfirmation, setOracleConfirmation] = useState("");
  // 「保存時に Select AI Agent アセットも再構築する」は業務プロファイルの属性（#1690）。フォームに持ち、
  // 保存の payload と未保存の判定に入れる。
  const rebuildAgentAssets = form.rebuildAgentAssetsOnSave;
  const setRebuildAgentAssets = (value: boolean) =>
    setForm((current) => ({ ...current, rebuildAgentAssetsOnSave: value }));
  const [oracleSyncJobId, setOracleSyncJobId] = useState("");
  const [oracleSyncProfileId, setOracleSyncProfileId] = useState("");
  const [oracleSyncSubmissionError, setOracleSyncSubmissionError] = useState("");
  // Oracle 反映の開始の失敗の code（Credential が無い 409 なら、ジョブの失敗と同じ案内とリンクを出す。#1683）。
  const [oracleSyncSubmissionErrorCode, setOracleSyncSubmissionErrorCode] = useState("");
  const [profileSaveError, setProfileSaveError] = useState("");
  // 完了を通知済みの Oracle 同期 job ID。render 中に比べるため state で持つ。
  const [reportedOracleSyncJobId, setReportedOracleSyncJobId] = useState("");
  // 終了した Oracle 同期 job（成功の通知と再取得は effect で行う）。
  const [finishedOracleSyncJob, setFinishedOracleSyncJob] = useState<{
    jobId: string;
    profileId: string;
    succeeded: boolean;
    invalidateSelectAi: boolean;
  } | null>(null);
  const lastOracleConfirmationRef = useRef("");
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
  const syncJobParam = searchParams.get("syncJobId") ?? "";
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
  // DB Profile 一覧の再取得・差分同期。反映・削除の後の案内と進行は起点の結果の直下（編集の画面は保存の結果の
  // 直下、一覧の削除とヘッダーの再取得の失敗はページの先頭）に 1 か所だけ出す（messaging §10.2、#2034）。
  const dbProfileListSync = useDbProfileListSync();
  const dbProfileRefreshing = dbProfileListSync.jobActive;
  // 既定の生成方式の選択肢（この環境で使える方式。#1524）。
  const enginesQuery = useQuery({
    queryKey: ["nl2sql", "engines"],
    queryFn: ({ signal }) => apiGet<Nl2SqlEnginesData>(NL2SQL_ENGINES_URL, { signal }),
    staleTime: 60_000,
  });
  const engineOptions = useMemo(
    () => defaultEngineOptions(form.defaultEngine, enginesQuery.data),
    [form.defaultEngine, enginesQuery.data]
  );
  const dbProfilesQuery = useQuery({
    queryKey: ["nl2sql", "select-ai", "business-profiles"],
    queryFn: () => apiGet<SelectAiDbProfilesData>(BUSINESS_SELECT_AI_DB_PROFILES_URL),
    staleTime: 5_000,
  });
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
  // 保存（Oracle 反映）の事前条件: Select AI の Credential が作成済みか（#1683 / #1688）。
  const selectAiCredential = useSelectAiCredentialReadiness();
  const selectAiCredentialMissing = selectAiCredential.missing;
  const oracleSyncJobQuery = useQuery({
    queryKey: ["nl2sql", "oracle-sync-job", oracleSyncJobId],
    queryFn: () => apiGet<ProfileSyncJobData>(`/api/nl2sql/oracle-sync-jobs/${oracleSyncJobId}`),
    enabled: Boolean(oracleSyncJobId),
    refetchInterval: (query) => {
      const status = (query.state.data as ProfileSyncJobData | undefined)?.status;
      return status && ["succeeded", "failed", "cancelled"].includes(status) ? false : 1_000;
    },
  });

  // URL の syncJobId を追跡対象へ render 中に反映する。
  if (useValuesChanged([selectedProfileId, syncJobParam]) && syncJobParam) {
    setOracleSyncJobId(syncJobParam);
    if (selectedProfileId) setOracleSyncProfileId(selectedProfileId);
  }
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
  const oracleSyncJob = oracleSyncJobQuery.data ?? null;

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
    setOracleSyncJobId("");
    setOracleSyncProfileId("");
    setOracleSyncSubmissionError("");
    setOracleSyncSubmissionErrorCode("");
    setProfileSaveError("");
    lastOracleConfirmationRef.current = "";
    setReportedOracleSyncJobId("");
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
      dbProfilesQuery.refetch(),
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

  const trackDbProfileRefreshSignal = dbProfileListSync.track;

  // 編集対象の切替時にフォームと編集付帯 state を同期する(deep link 初回ロード後も含む)
  const editTargetKey = selectedProfile?.id ?? (profileParam === "new" ? "new" : "");
  const formInitializationKey = editTargetKey;
  if (useValuesChanged([formInitializationKey]) && formInitializationKey) {
    setForm(
      selectedProfile
        ? profileToForm(selectedProfile)
        : emptyProfileForm()
    );
    setOracleConfirmation("");
    setNameError(null);
    setRequiredErrors({});
  }

  // 終了した Oracle 同期 job を render 中に一度だけ state へ反映し、通知と再取得は effect で行う。
  if (useValuesChanged([oracleSyncJobQuery.data, queryClient, trackDbProfileRefreshSignal])) {
    const job = oracleSyncJobQuery.data;
    if (
      job &&
      ["succeeded", "failed", "cancelled"].includes(job.status) &&
      reportedOracleSyncJobId !== job.job_id
    ) {
      setReportedOracleSyncJobId(job.job_id);
      const succeeded = job.status === "succeeded";
      // 反映が失敗しても（DBMS_CLOUD_AI Profile の範囲の不一致など）、DB Profile 一覧の同期を求める結果は
      // 反映の結果の直下に出す（#2034）。
      const trackingRefresh = trackDbProfileRefreshSignal(job.oracle_result);
      setFinishedOracleSyncJob({
        jobId: job.job_id,
        profileId: job.profile_id,
        succeeded,
        invalidateSelectAi: succeeded && !trackingRefresh,
      });
    }
  }
  useEffect(() => {
    if (!finishedOracleSyncJob) return;
    // 反映の job は名称の変更の後始末で業務プロファイルを更新し、ETag を進める（旧名の印の消去）。
    // 成功・失敗のどちらでも最新版を取り直し、次の保存の If-Match が古い ETag で 409 にならないようにする。
    void queryClient.invalidateQueries({
      queryKey: nl2sqlIncrementalKeys.profile(finishedOracleSyncJob.profileId),
    });
    if (!finishedOracleSyncJob.succeeded) return;
    if (finishedOracleSyncJob.invalidateSelectAi) {
      void queryClient.invalidateQueries({ queryKey: ["nl2sql", "select-ai"] });
    }
    toast.success(t("profiles.oracle.sync.succeeded"));
  }, [finishedOracleSyncJob, queryClient]);



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

  // legacy hash 導線: 旧 #profile-learning は Select AI 設定へ正規化する
  useEffect(() => {
    if (location.hash !== "#profile-learning") return;
    if (!profileParam && !profilesLoaded) return;
    const target = profiles.find((profile) => !profile.archived)?.id ?? "new";
    navigate(
      {
        pathname: location.pathname,
        search: profileParam ? location.search : `?profile=${target}`,
        hash: "#profile-select-ai",
      },
      { replace: true }
    );
  }, [
    location.hash,
    location.pathname,
    location.search,
    navigate,
    profileParam,
    profiles,
    profilesLoaded,
  ]);

  useEffect(() => {
    if (location.hash !== "#profile-select-ai" || profileParam || !profilesLoaded) return;
    const target = profiles.find((profile) => !profile.archived)?.id ?? "new";
    navigate(
      { pathname: location.pathname, search: `?profile=${target}`, hash: location.hash },
      { replace: true }
    );
  }, [location.hash, location.pathname, navigate, profileParam, profiles, profilesLoaded]);

  useEffect(() => {
    if (location.hash !== "#profile-select-ai" || activeView !== "editor") return;
    const frame = window.requestAnimationFrame(() => {
      const target = document.getElementById("profile-select-ai");
      target?.scrollIntoView({ block: "start", inline: "nearest", behavior: "auto" });
      target?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeView, location.hash, selectedProfile?.id]);

  const startNew = () => {
    setMessage("");
    setRefreshError("");
    setOracleSyncJobId("");
    setOracleSyncProfileId("");
    setOracleSyncSubmissionError("");
    setOracleSyncSubmissionErrorCode("");
    setProfileSaveError("");
    setProfileSaveConflict(false);
    lastOracleConfirmationRef.current = "";
    setReportedOracleSyncJobId("");
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
    if (selectAiCredentialMissing) return;
    setNameError(null);
    setRequiredErrors({});
    setOracleSyncJobId("");
    setOracleSyncSubmissionError("");
    setOracleSyncSubmissionErrorCode("");
    setProfileSaveError("");
    setProfileSaveConflict(false);
    setReportedOracleSyncJobId("");
    mutationBusyRef.current = true;
    setLoading("save");
    let saved: Nl2SqlProfile;
    try {
      const payload = formToPayload(form);
      saved = selectedProfile
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
      if (isProfileSaveTargetCurrent(editTargetRef.current, target, saved.id)) {
        setForm(profileToForm(normalizeProfile(saved)));
        setOracleSyncProfileId(saved.id);
        setRequiredErrors({});
        // 破壊的操作のゲートなので、保存が通ったら必ず再入力を求める
        // (既存 profile の保存では編集対象が変わらず初期化 effect が走らない)。
        setOracleConfirmation("");
        if (!selectedProfile) {
          setSearchParams({ profile: saved.id }, { replace: true });
        }
      }
    } catch (err) {
      if (isProfileNameConflictError(err)) {
        setNameError("duplicate");
        setNameFocusRequest((current) => current + 1);
        setLoading("");
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
      setLoading("");
      return;
    }

    try {
      setOracleSyncSubmissionError("");
      setOracleSyncSubmissionErrorCode("");
      const submittedOracleConfirmation = oracleConfirmation.trim();
      lastOracleConfirmationRef.current = submittedOracleConfirmation;
      const job = await apiPost<ProfileSyncJobData>(
        `/api/nl2sql/profiles/${saved.id}/oracle-sync-jobs`,
        {
          confirmation: submittedOracleConfirmation,
          reason: "ui-profile-management-save",
          rebuild_agent_assets: rebuildAgentAssets,
        },
        {
          headers: {
            "Idempotency-Key": `profile-save-${saved.id}-${saved.etag || "new"}`,
          },
        }
      );
      setReportedOracleSyncJobId("");
      // 新規作成では、ジョブの開始の応答を待つ間に URL が作成した ID（?profile=<id>）へ変わる。
      // 作成した業務プロファイルを開いている間は、同じ保存の続きとして経過を追跡する（#1784）。
      if (isProfileSaveTargetCurrent(editTargetRef.current, target, saved.id)) {
        setOracleSyncJobId(job.job_id);
        setOracleSyncProfileId(job.profile_id);
      }
      queryClient.setQueryData(["nl2sql", "oracle-sync-job", job.job_id], job);
      // 1 文目は何が起きたかだけにし、Oracle Profile の反映の続きは description に分ける（#899）。
      // 反映を始められなかったとき（Credential が無い 409 など）は「バックグラウンドで続きます」と
      // 言えないので出さず、保存したことと失敗は結果の領域の 1 か所で示す（messaging.md §10.1。#1683）。
      toast.success(t("profiles.message.saved"), {
        description: t("profiles.message.savedOracleSyncing"),
      });
    } catch (err) {
      // 開始できなかった理由は、保存した業務プロファイルの結果の領域に出す（新規作成でも。#1784）。
      if (isProfileSaveTargetCurrent(editTargetRef.current, target, saved.id)) {
        setOracleSyncSubmissionError(
          err instanceof Error ? err.message : t("profiles.oracle.sync.failed")
        );
        setOracleSyncSubmissionErrorCode(err instanceof ApiError ? (err.errorCode ?? "") : "");
      }
      selectAiCredential.refreshAfterError(err);
    } finally {
      setLoading("");
    }
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
      setOracleConfirmation("");
      setNameError(null);
      setRequiredErrors({});
      setProfileSaveError("");
      setProfileSaveConflict(false);
    } finally {
      setLoading("");
    }
  };

  const retryOracleSync = async () => {
    if (mutationBusyRef.current) return;
    const profileId = oracleSyncJob?.profile_id || oracleSyncProfileId || selectedProfile?.id;
    if (!profileId) return;
    setLoading("retry-oracle-sync");
    try {
      const retryConfirmation =
        oracleConfirmation.trim() || lastOracleConfirmationRef.current.trim();
      const job = oracleSyncJob?.status === "failed"
        ? await apiPost<ProfileSyncJobData>(
            `/api/nl2sql/oracle-sync-jobs/${oracleSyncJob.job_id}/retry`
          )
        : await apiPost<ProfileSyncJobData>(
            `/api/nl2sql/profiles/${profileId}/oracle-sync-jobs`,
            {
              confirmation: retryConfirmation,
              reason: "ui-profile-management-retry",
              rebuild_agent_assets: rebuildAgentAssets,
            },
            { headers: { "Idempotency-Key": `profile-retry-${profileId}-${Date.now()}` } }
          );
      lastOracleConfirmationRef.current = retryConfirmation;
      setOracleSyncSubmissionError("");
      setOracleSyncSubmissionErrorCode("");
      setReportedOracleSyncJobId("");
      setOracleSyncJobId(job.job_id);
      setOracleSyncProfileId(job.profile_id);
      queryClient.setQueryData(["nl2sql", "oracle-sync-job", job.job_id], job);
    } catch (err) {
      setOracleSyncSubmissionError(
        err instanceof Error ? err.message : t("profiles.oracle.sync.failed")
      );
      setOracleSyncSubmissionErrorCode(err instanceof ApiError ? (err.errorCode ?? "") : "");
      selectAiCredential.refreshAfterError(err);
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
      // 削除の後は一覧の画面に戻るので、DB Profile 一覧の同期の案内はページの先頭に出す。
      const trackingRefresh = deleted.oracle_cleanup.some((item) =>
        trackDbProfileRefreshSignal(item, { scope: "page" })
      );
      if (!trackingRefresh) {
        await queryClient.invalidateQueries({ queryKey: ["nl2sql", "select-ai"] });
      }
      if (editTargetRef.current === profile.id) {
        setSearchParams({}, { replace: true });
      }
      const cleanupWarnings = deleted.oracle_cleanup.filter((item) => item.warning.trim());
      const cleanupExecuted = deleted.oracle_cleanup.some((item) => item.executed);
      toast.success(
        t(
          cleanupExecuted && cleanupWarnings.length === 0
            ? "profiles.message.deletedWithOracleCleanup"
            : "profiles.message.deleted",
          { name: deletedProfile.name }
        )
      );
      if (cleanupWarnings.length > 0) {
        toast.warning(
          t("profiles.message.oracleCleanupWarning", { count: cleanupWarnings.length })
        );
      }
    } catch (err) {
      const fallback =
        err instanceof ApiError && err.status === 502
          ? t("profiles.error.deleteOracleCleanup")
          : t("profiles.error.delete");
      toast.error(err instanceof Error && err.message ? err.message : fallback);
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
  const canClearOracleExecution = Boolean(
    oracleConfirmation ||
      oracleSyncJobId ||
      oracleSyncProfileId ||
      oracleSyncJob ||
      oracleSyncSubmissionError ||
      syncJobParam
  );

  const clearOracleExecution = () => {
    setOracleConfirmation("");
    setOracleSyncJobId("");
    setOracleSyncProfileId("");
    setOracleSyncSubmissionError("");
    setOracleSyncSubmissionErrorCode("");
    setProfileSaveError("");
    lastOracleConfirmationRef.current = "";
    setReportedOracleSyncJobId("");
    if (syncJobParam) {
      const nextParams = new URLSearchParams(searchParams);
      nextParams.delete("syncJobId");
      setSearchParams(nextParams, { replace: true });
    }
  };

  const editor = (
    <ProfileEditor
      selectedProfile={selectedProfile}
      engineOptions={engineOptions}
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
      oracleConfirmation={oracleConfirmation}
      rebuildAgentAssets={rebuildAgentAssets}
      canClearOracleExecution={canClearOracleExecution}
      oracleSyncJob={oracleSyncJob}
      oracleSyncSubmissionError={oracleSyncSubmissionError}
      oracleSyncSubmissionErrorCode={oracleSyncSubmissionErrorCode}
      selectAiCredentialMissing={selectAiCredentialMissing}
      selectAiCredentialName={selectAiCredential.credentialName}
      canOpenSelectAiCredential={selectAiCredential.canOpenSettings}
      recheckingSelectAiCredential={selectAiCredential.rechecking}
      onRecheckSelectAiCredential={() => void selectAiCredential.recheck()}
      saveError={profileSaveError}
      saveConflict={profileSaveConflict}
      reloadingLatest={loading === "reload-latest"}
      onReloadLatest={() => void reloadLatestProfile()}
      retryingOracleSync={loading === "retry-oracle-sync"}
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
      onOracleConfirmationChange={setOracleConfirmation}
      onOracleExecutionClear={clearOracleExecution}
      onRebuildAgentAssetsChange={setRebuildAgentAssets}
      onRetryOracleSync={() => void retryOracleSync()}
      dbProfileListSync={dbProfileListSync}
    />
  );
  const schemaRefreshing = sharedSchemaRefresh.isRefreshing;
  // スキーマの更新・業務プロファイルの再取得は durable job。開始のボタンは押した側だけを送信の間 loading にし、
  // job の間は無効にするだけ。job の間のスピナーは一覧の上の進行の表示が 1 つだけ出す（messaging §3.7、#821）。
  const headerSchemaRefreshStarting = sharedSchemaRefresh.startingOrigin === PROFILE_SCHEMA_REFRESH_HEADER;
  const dbProfileRefreshBusy = dbProfileListSync.busy;
  const headerDbProfileRefreshStarting = dbProfileListSync.externalStarting;
  // 案内（保存の結果の直下）が進行を出しているときは、作業領域の先頭に同じ進行を重ねない（messaging §3.7）。
  const workspaceDbProfileRefreshing = dbProfileRefreshing && !dbProfileListSync.ownsProgress;
  const profileListRefreshing = profilesQuery.isFetching && !profilesQuery.isFetchingNextPage;
  const profileWorkspaceProcessing = schemaRefreshing ? (
      <SchemaRefreshProcessing testId="profile-management-workspace-processing" />
    ) : workspaceDbProfileRefreshing ? (
      <DbProfileListSyncProcessing
        sync={dbProfileListSync}
        testId="profile-management-workspace-processing"
      />
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
    (profiles.length > 0 || loading === "load" || schemaRefreshing || workspaceDbProfileRefreshing);
  const profileListLoading = !profilesLoaded || (loading === "load" && profiles.length === 0);
  // 初回の読込は一覧の読込表示がスピナーを出す。ヘッダーの「最新情報を取得」は回さない（狭い画面では「その他の操作」の
  // 中で見えない。同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
  const profileListShowsSpinner = profileListLoading && !showProfileWorkspaceProcessing;
  // 失敗はページの先頭か保存の結果の直下の案内が出すので、見出しのバッジは実行中だけにする（同じ失敗を 2 か所に
  // 出さない。#2034）。
  const headerRefreshStatus = dbProfileRefreshing ? (dbProfileListSync.job?.status ?? "pending") : "";
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
          ) : activeView === "list" && headerRefreshStatus ? (
            <PageHeaderStatusBadge
              variant={headerRefreshStatus === "error" ? "danger" : "info"}
              label={t(`profiles.dbProfileRefresh.status.${headerRefreshStatus}`)}
            />
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
                {
                  id: "db-profile-refresh",
                  kind: "utility",
                  label: t("profiles.action.dbProfileRefresh"),
                  icon: RefreshCw,
                  onClick: () => dbProfileListSync.refreshFrom(),
                  loading: headerDbProfileRefreshStarting,
                  disabled: dbProfileRefreshBusy && !headerDbProfileRefreshStarting,
                },
              ]
            : []
        }
        actionsTestId="profile-management-actions"
        // 編集の画面の「一覧へ戻る」はページの左上（#618）。保存は Oracle への反映を伴うため、確認語欄の操作行に置く
        // （確認語が要る保存の例外。design-system README §4「詳細・作成・編集の画面の操作」）。
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
        {/* ヘッダーの「DB Profile 一覧を再取得」の失敗と、一覧の削除の後の同期の案内（#2034）。 */}
        <DbProfileListSyncNotice
          sync={dbProfileListSync}
          scope="page"
          testId="profile-management-db-profile-sync-notice"
          actionTestId="profile-management-db-profile-sync-refresh"
          processingTestId="profile-management-db-profile-sync-processing"
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
