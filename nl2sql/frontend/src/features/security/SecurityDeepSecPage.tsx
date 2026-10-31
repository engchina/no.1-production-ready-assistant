import { useWorkspaceState, useWorkspaceDraftWriter, useResetExecutionConsent } from "@/components/WorkspaceState";
import { ScopeExpressionEditor } from "./ScopeExpressionEditor";
import { canonicalExpression, entitlementExpression, expressionError, expressionCounts } from "./scope-expression";
import {
  Button,
  Banner,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Disclosure,
  FormStatus,
  toast,
  StatusBadge,
  PageHeader,
  PageBody,
  useConfirm,
  BulkSelectionActions,
  ProcessingIndicator,
  ExecutionConfirmationField,
  ListSkeleton,
  TimedLoadingState,
  SearchableSelectField,
  type SearchableSelectOption,
  useActionPending,
  FormGrid,
  ListToolbar,
  SelectField,
  TextField,
  FieldLegend,
} from "@production-ready/ui";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useDatabaseStatus } from "@/lib/queries";
import {
  CheckCircle2,
  Clock3,
  Database,
  KeyRound,
  Play,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  ShieldCheck,
  Trash2,
} from "lucide-react";


import { PageHeaderStatusBadge } from "@/components/PageHeaderStatusBadge";
import { ErrorState } from "@/components/StateViews";
import { PageNotice } from "@/components/page-notice";
import {
  ManagementPanelHeader,
  ManagementPanelShell,
  ManagementTabs,
  WorkSection,
} from "@/features/nl2sql/components/DbAdminShared";
import { isAbortError } from "@/lib/api";
import { formatDateTimeWithYear } from "@/lib/format";
import { t } from "@/lib/i18n";
import { useUnsavedChangesGuard } from "@/lib/useUnsavedChangesGuard";
import { listCountLabels } from "@/lib/pagination-labels";
import { useRequestScope } from "@/lib/useRequestScope";
import { cn } from "@/lib/utils";
import { selectedVisibleKey } from "@/lib/visible-selection";
import {
  formatDbObjectName,
  formatDbObjectPart,
  formatEntitlementTargetName,
  normalizeDbIdentifierToken,
} from "@/features/nl2sql/dbObjectIdentity";
import { DbObjectName } from "@/features/nl2sql/components/DbObjectName";
import { DbManagementSearchField } from "@/components/DbObjectFilterFields";
import { useAuth } from "./AuthProvider";
import { MENU_PERMISSIONS } from "./menu-permissions";
import {
  compareRoleDisplayNames,
  roleDisplayName,
} from "@production-ready/system-settings";
import { securityApi } from "./api";
import type {
  DataEntitlement,
  DataEntitlementScopeFilter,
  DataEntitlementScopeOperator,
  DataEntitlementScopeValueSource,
  DataEntitlementScopeValueType,
  DeepSecDataEntitlementPreview,
  DeepSecPlan,
  DeepSecRoleEntitlements,
  DeepSecStatus,
  DeepSecStep,
  DeepSecTargetColumn,
  DeepSecTargetObject,
  DeepSecTargetObjectDetail,
  DeepSecVerification,
} from "./types";

const ENTITLEMENT_CAPABILITIES = ["SELECT"] as const;
const SCOPE_MODES = ["ALL", "FILTERS", "EXPRESSION"] as const;
const NULL_SCOPE_OPERATORS = ["IS_NULL", "IS_NOT_NULL"] as const;
const LITERAL_SCOPE_VALUE_SOURCE = "LITERAL";
const LOGIN_USER_ID_SCOPE_VALUE_SOURCE = "LOGIN_USER_ID";
const LEGACY_APP_USER_ID_SCOPE_VALUE_SOURCE = "APP_USER_ID";
const SCOPE_OPERATORS_BY_VALUE_TYPE: Record<DataEntitlementScopeValueType, DataEntitlementScopeOperator[]> = {
  TEXT: ["EQ", "NE", "CONTAINS", "STARTS_WITH", "IN", "IS_NULL", "IS_NOT_NULL"],
  NUMBER: ["EQ", "NE", "GT", "GTE", "LT", "LTE", "BETWEEN", "IN", "IS_NULL", "IS_NOT_NULL"],
  TEMPORAL: ["EQ", "BEFORE", "ON_OR_BEFORE", "AFTER", "ON_OR_AFTER", "BETWEEN", "IS_NULL", "IS_NOT_NULL"],
};
type DeepSecView = "data-user" | "foundation" | "data-permissions";
type DataEntitlementDraft = DataEntitlement & { client_key: string };
type ScrollPositionSnapshot = {
  element: HTMLElement;
  top: number;
  left: number;
};

const ADMIN_EXECUTE_CONFIRMATION = "ADMIN_EXECUTE";
const ADMIN_RESET_CONFIRMATION = "ADMIN_RESET";
const TARGET_OBJECT_PAGE_SIZE = 50;

function loadErrorMessage(cause: unknown) {
  return cause instanceof Error && cause.message.trim()
    ? cause.message
    : t("security.common.loadError");
}

function stepStatus(step: DeepSecStep) {
  if (step.status === "APPLIED") return { variant: "success" as const, label: t("security.deepsec.complete") };
  if (step.status === "FAILED") return { variant: "danger" as const, label: t("security.deepsec.failed") };
  if (step.status === "RUNNING") return { variant: "info" as const, label: t("security.deepsec.running") };
  return { variant: "neutral" as const, label: t("security.deepsec.pending") };
}

function normalizeScopeFilterValueSource(value: string | undefined): DataEntitlementScopeValueSource {
  const normalized = value?.trim().toUpperCase();
  return normalized === LOGIN_USER_ID_SCOPE_VALUE_SOURCE ||
    normalized === LEGACY_APP_USER_ID_SCOPE_VALUE_SOURCE
    ? LOGIN_USER_ID_SCOPE_VALUE_SOURCE
    : LITERAL_SCOPE_VALUE_SOURCE;
}

function scopeFilterSupportsValueSource(operator: string, valueType: string) {
  return operator === "EQ" && ["TEXT", "NUMBER"].includes(valueType);
}

function isPositiveIntegerScopeValue(value: string | undefined) {
  return /^[1-9]\d*$/.test((value ?? "").trim());
}

function migrateLegacyColumnEqualsEntitlement(item: DataEntitlement): DataEntitlement {
  const scopeMode = (item.scope_mode ?? "ALL").trim().toUpperCase();
  const scopeColumn = normalizeDbIdentifierToken(item.scope_column);
  const scopeCode = (item.scope_code ?? "").trim();
  if (scopeMode !== "COLUMN_EQUALS") {
    return {
      ...item,
      scope_filters: (item.scope_filters ?? []).map((filter) => ({
        ...filter,
        value_source: normalizeScopeFilterValueSource(filter.value_source),
      })),
    };
  }
  return {
    ...item,
    scope_code: "FILTERS",
    scope_mode: "FILTERS",
    scope_column: "",
    scope_filters:
      scopeColumn && scopeCode && scopeCode !== "*"
        ? [
            {
              column_name: scopeColumn,
              operator: "EQ",
              value_type: "TEXT",
              value_source: LITERAL_SCOPE_VALUE_SOURCE,
              value: scopeCode,
              value_to: "",
              values: [],
            },
          ]
        : [],
  };
}

function entitlementDraftClientKey(item: DataEntitlement, index: number) {
  const entitlementId = item.entitlement_id?.trim();
  if (entitlementId) return `saved:${entitlementId}`;
  return `draft:${index}:${formatEntitlementTargetName(item) || "BLANK"}`;
}

function toEntitlementDraft(item: DataEntitlement, index: number): DataEntitlementDraft {
  const migrated = migrateLegacyColumnEqualsEntitlement({ ...item });
  return {
    ...migrated,
    client_key: entitlementDraftClientKey(migrated, index),
  };
}

function blankEntitlementDraft(clientKey: string): DataEntitlementDraft {
  return {
    client_key: clientKey,
    resource_code: "",
    scope_code: "*",
    capability: "SELECT",
    target_owner: "",
    target_object: "",
    target_type: "TABLE",
    column_names: [],
    scope_mode: "ALL",
    scope_column: "",
    scope_filters: [],
    scope_expression: null,
  };
}

function entitlementDraft(role: DeepSecRoleEntitlements | null): DataEntitlementDraft[] {
  return role?.data_entitlements.map((item, index) => toEntitlementDraft(item, index)) ?? [];
}

function entitlementColumnsSummary(
  entitlement: DataEntitlement,
  detail: DeepSecTargetObjectDetail | undefined
) {
  const selectedCount = entitlement.column_names?.length ?? 0;
  if (!detail) {
    return t("security.deepsec.entitlements.columnsCount", { count: selectedCount });
  }
  return t("security.deepsec.entitlements.columnsSummary", {
    selected: selectedCount,
    total: detail.columns.length,
  });
}

function entitlementScopeSummary(entitlement: DataEntitlement) {
  if (entitlement.scope_expression) return t("security.deepsec.entitlements.scopeFilterCount", { count: expressionCounts(entitlement.scope_expression.root).conditions });
  if ((entitlement.scope_mode ?? "ALL") === "FILTERS") {
    return t("security.deepsec.entitlements.scopeFilterCount", {
      count: entitlement.scope_filters?.length ?? 0,
    });
  }
  return t("security.deepsec.entitlements.scopeAll");
}

function entitlementRoleStatus(role: DeepSecRoleEntitlements) {
  if (role.archived) return { variant: "neutral" as const, label: t("security.deepsec.entitlements.archived") };
  if (role.is_built_in) return { variant: "info" as const, label: t("security.deepsec.entitlements.builtIn") };
  return { variant: "success" as const, label: t("security.deepsec.entitlements.editable") };
}

function entitlementRoleSearchText(role: DeepSecRoleEntitlements) {
  return [
    role.role_code,
    role.display_name,
    role.description,
    ...role.data_entitlements.flatMap((item) => [
      item.resource_code,
      item.scope_code,
      item.capability,
      item.target_owner ?? "",
      item.target_object ?? "",
      item.scope_column ?? "",
      ...(item.scope_filters ?? []).flatMap((filter) => [
        filter.column_name,
        filter.operator,
        filter.value_type,
        filter.value_source ?? "",
        filter.value ?? "",
        filter.value_to ?? "",
        ...(filter.values ?? []),
      ]),
    ]),
  ]
    .join(" ")
    .toLowerCase();
}

function targetQualifiedName(item: Pick<DeepSecTargetObject, "name" | "owner" | "qualified_name">) {
  // カタログ値のまま組み立てる。全体を大文字化すると `APP."MixedCase"` が別の表を指してしまう。
  return formatDbObjectName(item);
}

function mergeTargetObjectPages(
  current: DeepSecTargetObject[],
  next: DeepSecTargetObject[]
) {
  const merged = new Map<string, DeepSecTargetObject>();
  for (const item of [...current, ...next]) {
    merged.set(targetQualifiedName(item), item);
  }
  return Array.from(merged.values());
}

function targetObjectTypeLabel(objectType: string) {
  return objectType.replaceAll("_", " ").toUpperCase();
}

function entitlementTargetKey(entitlement: DataEntitlement) {
  return formatEntitlementTargetName(entitlement);
}

function entitlementApplyStatus(entitlement: DataEntitlement) {
  if (entitlement.apply_status === "APPLIED") {
    return { variant: "success" as const, label: t("security.deepsec.entitlements.applied") };
  }
  if (entitlement.apply_status === "FAILED") {
    return { variant: "danger" as const, label: t("security.deepsec.entitlements.failed") };
  }
  if (entitlement.apply_status === "RUNNING") {
    return { variant: "info" as const, label: t("security.deepsec.entitlements.running") };
  }
  return { variant: "neutral" as const, label: t("security.deepsec.entitlements.pending") };
}

function oracleBaseType(dataType: string) {
  const normalized = dataType.trim().toUpperCase();
  if (normalized.startsWith("TIMESTAMP")) return "TIMESTAMP";
  return normalized.split("(", 1)[0].split(" ", 1)[0];
}

function scopeValueType(dataType: string): DataEntitlementScopeValueType | null {
  const baseType = oracleBaseType(dataType);
  if (["CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2"].includes(baseType)) return "TEXT";
  if (["NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE"].includes(baseType)) return "NUMBER";
  if (["DATE", "TIMESTAMP"].includes(baseType)) return "TEMPORAL";
  return null;
}

function isSupportedScopeColumn(column: DeepSecTargetColumn) {
  return scopeValueType(column.data_type) !== null;
}

function defaultScopeOperator(valueType: DataEntitlementScopeValueType): DataEntitlementScopeOperator {
  return SCOPE_OPERATORS_BY_VALUE_TYPE[valueType][0];
}

function blankScopeFilter(columns: DeepSecTargetColumn[] = []): DataEntitlementScopeFilter {
  const column = columns.find(isSupportedScopeColumn);
  const valueType = column ? scopeValueType(column.data_type) ?? "TEXT" : "TEXT";
  return {
    column_name: column?.column_name ?? "",
    operator: defaultScopeOperator(valueType),
    value_type: valueType,
    value_source: LITERAL_SCOPE_VALUE_SOURCE,
    value: "",
    value_to: "",
    values: [],
  };
}

function normalizeScopeFilters(filters: DataEntitlementScopeFilter[] = []) {
  return filters
    .map((filter) => {
      const operator = filter.operator.trim().toUpperCase();
      const valueType = filter.value_type.trim().toUpperCase();
      const valueSource = scopeFilterSupportsValueSource(operator, valueType)
        ? normalizeScopeFilterValueSource(filter.value_source)
        : LITERAL_SCOPE_VALUE_SOURCE;
      return {
        column_name: normalizeDbIdentifierToken(filter.column_name),
        operator,
        value_type: valueType,
        value_source: valueSource,
        value: valueSource === LOGIN_USER_ID_SCOPE_VALUE_SOURCE ? "" : (filter.value ?? "").trim(),
        value_to:
          valueSource === LOGIN_USER_ID_SCOPE_VALUE_SOURCE ? "" : (filter.value_to ?? "").trim(),
        values:
          valueSource === LOGIN_USER_ID_SCOPE_VALUE_SOURCE
            ? []
            : Array.from(
                new Set((filter.values ?? []).map((value) => value.trim()).filter(Boolean))
              ),
      };
    })
    .filter((filter) => filter.column_name);
}

function scopeFilterNeedsValue(operator: string) {
  return !NULL_SCOPE_OPERATORS.includes(operator as (typeof NULL_SCOPE_OPERATORS)[number]);
}

function scopeFilterNeedsValueTo(operator: string) {
  return operator === "BETWEEN";
}

function scopeFilterNeedsValues(operator: string) {
  return operator === "IN";
}

function normalizeEntitlementRows(rows: DataEntitlement[]) {
  return rows
    .map((item) => {
      const rawScopeMode = (item.scope_mode ?? "ALL").trim().toUpperCase();
      const legacyScopeColumn = normalizeDbIdentifierToken(item.scope_column);
      const legacyScopeValue = item.scope_code.trim();
      const legacyFilters =
        rawScopeMode === "COLUMN_EQUALS" && legacyScopeColumn && legacyScopeValue && legacyScopeValue !== "*"
          ? [
              {
                column_name: legacyScopeColumn,
                operator: "EQ",
                value_type: "TEXT",
                value_source: LITERAL_SCOPE_VALUE_SOURCE,
                value: legacyScopeValue,
                value_to: "",
                values: [],
              },
            ]
          : item.scope_filters ?? [];
      const scopeMode = rawScopeMode === "COLUMN_EQUALS" ? "FILTERS" : rawScopeMode;
      return {
        entitlement_id: item.entitlement_id ?? "",
        // 識別子は backend と同じ canonical token（引用が必要な名前だけ "..."）に揃える。
        // 全体を大文字化すると "Mixed_Case" が大文字の同名表 MIXED_CASE を指してしまう。
        resource_code: formatEntitlementTargetName(item),
        scope_code: item.scope_code.trim(),
        capability: "SELECT",
        target_owner: normalizeDbIdentifierToken(item.target_owner),
        target_object: normalizeDbIdentifierToken(item.target_object),
        target_type: (item.target_type ?? "TABLE").trim().toUpperCase(),
        column_names: Array.from(
          new Set((item.column_names ?? []).map(normalizeDbIdentifierToken).filter(Boolean))
        ),
        scope_mode: scopeMode,
        scope_column: "",
        scope_filters: scopeMode === "FILTERS" ? normalizeScopeFilters(legacyFilters) : [],
        scope_expression: scopeMode === "EXPRESSION" ? item.scope_expression : null,
        ...(item.scope_expression_version ? { scope_expression_version: item.scope_expression_version } : {}),
      };
    })
    .map((item) => ({
      ...item,
      scope_code: item.scope_mode === "ALL" ? "*" : item.scope_mode === "EXPRESSION" ? "EXPRESSION" : "FILTERS",
    }));
}

function entitlementSignature(rows: DataEntitlement[]) {
  return JSON.stringify(normalizeEntitlementRows(rows).map((item) => ({
    ...item,
    column_names: [...item.column_names].sort(),
    scope_expression: canonicalExpression(item.scope_expression),
    scope_filters: item.scope_filters.map((filter) => ({
      ...filter, values: [...(filter.values ?? [])].sort(),
    })).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right))),
  })).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right))));
}

/**
 * 対象 table/view の選択（#608）。1 つを選ぶので、検索できる選択欄（`SearchableSelectField`、#578）にする。
 * 候補は数千件になりうるので、サーバー側で検索し（スキーマ名・`OWNER.OBJECT`・コメント）、続きは一覧の下端までの
 * スクロールか最後の候補からの ↓ で 50 件ずつ読む。候補の一覧はすべての Data Grant の選択欄で共有する
 * （開いている選択欄は 1 つだけで、閉じると検索語を消して全件に戻る）。
 */
function DeepSecTargetObjectPicker({
  index,
  value,
  selectedObject,
  objects,
  total,
  hasMore,
  loading,
  loadingMore,
  error,
  disabled,
  onSearchChange,
  onSelect,
  onLoadMore,
  onRetry,
}: {
  index: number;
  value: string;
  selectedObject: DeepSecTargetObject | null;
  objects: DeepSecTargetObject[];
  total: number | null;
  hasMore: boolean;
  loading: boolean;
  loadingMore: boolean;
  error: string;
  disabled: boolean;
  onSearchChange: (value: string) => void;
  onSelect: (value: string) => void;
  onLoadMore: () => void;
  onRetry: () => void;
}) {
  const options = objects.map(targetObjectOption);
  return (
    <div className="grid min-w-0 gap-2" data-testid={`security-deepsec-object-picker-${index}`}>
      <SearchableSelectField
        id={`deepsec-entitlement-resource-${index}`}
        label={t("security.deepsec.entitlements.resource")}
        // 対象の選択（表・ビュー）は lg（#1891）。
        width="lg"
        required
        value={value}
        options={options}
        selectedOption={
          selectedObject ? targetObjectOption(selectedObject) : value ? { value, label: value } : null
        }
        onValueChange={onSelect}
        onQueryChange={onSearchChange}
        remote={{
          total: total ?? objects.length,
          hasMore,
          loadingMore,
          searching: loading,
          onLoadMore,
        }}
        placeholder={t("security.deepsec.entitlements.objectPlaceholder")}
        helper={t("security.deepsec.entitlements.oracleHelper")}
        disabled={disabled}
        labels={{
          searchPlaceholder: t("security.deepsec.entitlements.objectSearchPlaceholder"),
          clearSearch: t("common.clearSearch"),
          // 件数を返さない問い合わせ（total が null）では、読み込んだ件数だけを出す。
          count: (shown, matched) =>
            typeof total === "number"
              ? t("security.deepsec.entitlements.objectCount", { loaded: shown, total: matched })
              : t("security.deepsec.entitlements.objectLoadedCount", { loaded: shown }),
          noMatch: () => t("security.deepsec.entitlements.objectEmpty"),
          empty: `${t("security.deepsec.entitlements.objectEmptyUnfiltered")}${t(
            "security.deepsec.entitlements.objectEmptyHint"
          )}`,
          searching: t("security.deepsec.entitlements.objectsLoading"),
          loadMore: t("security.deepsec.entitlements.objectLoadMore"),
        }}
      />
      {error ? (
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <FormStatus
            tone="danger"
            message={t("security.deepsec.entitlements.objectLoadMoreError", { message: error })}
          />
          <Button
            icon={RefreshCw}
            type="button"
            variant="secondary"
            size="sm"
            disabled={disabled}
            data-testid={`security-deepsec-object-picker-retry-${index}`}
            onClick={onRetry}
          >
            {t("common.retry")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/** 対象 object の選択肢（名前は SQL と同じ表記、下に種類とコメント）。 */
function targetObjectOption(object: DeepSecTargetObject): SearchableSelectOption {
  const qualifiedName = targetQualifiedName(object);
  return {
    value: qualifiedName,
    label: qualifiedName,
    description: [targetObjectTypeLabel(object.object_type), object.comment].filter(Boolean).join(" · "),
  };
}

function DeepSecPlanSteps({
  plan,
  stepNumbers,
  loading,
  headerSpinning,
  loadError,
  onRetry,
}: {
  plan: DeepSecPlan | null;
  stepNumbers: readonly number[];
  loading: boolean;
  /** PageHeader の「最新情報を取得」が回っているか（押した読込・初回の読込）。 */
  headerSpinning: boolean;
  loadError: string;
  onRetry: () => void;
}) {
  if (loadError) {
    return <ErrorState message={loadError} onRetry={onRetry} />;
  }

  if (loading && !plan) {
    // 文字だけにせず、経過時間と手順の形の Skeleton を出す（#265）。
    return (
      // 計画の読込を PageHeader の「最新情報を取得」から始めたときは、そのボタンがスピナーを出す（同じ処理の
      // スピナーは 1 つ。messaging §3.7、#416）。「再試行」など他の起点ではこの表示が出す（#819）。
      <TimedLoadingState
        label={t("security.deepsec.planLoading")}
        testId="security-deepsec-plan-loading"
        activityIcon={headerSpinning ? "none" : "spinner"}
      >
        <ListSkeleton rows={Math.max(1, stepNumbers.length)} rowClassName="h-24" />
      </TimedLoadingState>
    );
  }

  const steps = plan?.steps.filter((step) => stepNumbers.includes(step.step_no)) ?? [];
  if (plan && steps.length === 0) {
    return (
      <Card>
        <CardContent>
          <p className="text-sm text-fg-muted">{t("security.deepsec.planEmpty")}</p>
        </CardContent>
      </Card>
    );
  }

  return steps.map((step) => {
    const statusBadge = stepStatus(step);
    const versionLabel = `${plan?.version ?? "V001"}.${step.step_no}`;
    return (
      <Card key={step.step_no} data-testid={`security-deepsec-step-${step.step_no}`}>
        <CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 flex-1">
            <CardTitle
              className="flex min-w-0 flex-wrap items-center gap-2"
              aria-label={`${versionLabel} ${step.title}`}
            >
              <span data-surface-tint="neutral" className="inline-flex shrink-0 items-center rounded-md border border-border bg-surface-hover px-2 py-0.5 font-sans text-xs font-semibold leading-5 tabular-nums text-fg-muted">
                {versionLabel}
              </span>
              <span className="min-w-0 break-words">{step.title}</span>
            </CardTitle>
            <p className="mt-1 text-sm leading-6 text-fg-muted">{step.description}</p>
          </div>
          <div className="flex shrink-0 flex-col items-start gap-2 sm:items-end">
            <StatusBadge variant={statusBadge.variant} label={statusBadge.label} />
            {step.status === "APPLIED" && step.executed_at ? (
              <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-fg-muted">
                <Clock3 size={14} className="shrink-0" aria-hidden />
                <span>{t("security.deepsec.appliedAt")}</span>
                <time
                  className="whitespace-nowrap font-sans tabular-nums text-fg"
                  dateTime={step.executed_at}
                >
                  {formatDateTimeWithYear(step.executed_at)}
                </time>
              </span>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {step.error_message ? <Banner severity="danger">{step.error_message}</Banner> : null}
          <Disclosure surface="sunken" summary={t("security.deepsec.sqlDetails")} contentClassName="space-y-4">
              <p className="text-sm leading-6 text-fg-muted">{t("security.deepsec.sqlReadonly")}</p>
              <div className="space-y-1">
                <p className="text-xs font-medium text-fg-muted">{t("security.deepsec.checksum")}</p>
                <code className="block break-all rounded-md bg-surface p-2 text-xs">{step.checksum}</code>
              </div>
              <div className="space-y-3">
                {step.sql.map((sql, index) => (
                  <pre
                    key={`${step.step_no}-${index}`}
                    data-surface="code"
                    className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-surface p-3 text-fg text-xs leading-5"
                    tabIndex={0}
                    aria-label={`${step.title} SQL ${index + 1}`}
                  >
                    {sql}
                  </pre>
                ))}
              </div>
            </Disclosure>
        </CardContent>
      </Card>
    );
  });
}

export function SecurityDeepSecPage() {
  const confirm = useConfirm();
  const { user, hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const { data: database } = useDatabaseStatus({ enabled: false });
  const mayApply = hasPermission(MENU_PERMISSIONS.securityDeepSec);
  const mayVerify = hasPermission(MENU_PERMISSIONS.securityDeepSec);
  const mayManageEntitlements = hasPermission(MENU_PERMISSIONS.securityDeepSec);
  const [activeView, setActiveView] = useWorkspaceState<DeepSecView>("deepsec-view", "data-user");
  const [storedDraft, setStoredDraft] = useWorkspaceState("deepsec-rule-draft", "");
  const writeWorkspaceDraft = useWorkspaceDraftWriter();
  const restoredDraft = useRef<{
    roleId: string; version: number; rows: DataEntitlementDraft[]; baseline: DataEntitlement[];
  } | null>(null);
  const draftRead = useRef(false);
  if (!draftRead.current) {
    draftRead.current = true;
    try {
      const candidate = JSON.parse(storedDraft);
      if (typeof candidate.roleId === "string" && Number.isInteger(candidate.version) &&
          Array.isArray(candidate.rows) && Array.isArray(candidate.baseline)) restoredDraft.current = candidate;
    } catch { /* 草稿がない場合はサーバーの現行設定を使う。 */ }
  }
  const [status, setStatus] = useState<DeepSecStatus | null>(null);
  const [plan, setPlan] = useState<DeepSecPlan | null>(null);
  const [verification, setVerification] = useState<DeepSecVerification | null>(null);
  const [entitlementBaseline, setEntitlementBaseline] = useState<DeepSecRoleEntitlements | null>(null);
  const [entitlementRoles, setEntitlementRoles] = useState<DeepSecRoleEntitlements[]>([]);
  const [selectedEntitlementRoleId, setSelectedEntitlementRoleId] = useState<string | null>(restoredDraft.current?.roleId ?? null);
  const [entitlementDraftRows, setEntitlementDraftRows] = useState<DataEntitlementDraft[]>([]);
  const [selectedEntitlementDraftKey, setSelectedEntitlementDraftKey] = useState<string | null>(null);
  const [entitlementPreview, setEntitlementPreview] =
    useState<DeepSecDataEntitlementPreview | null>(null);
  const [entitlementSqlPreviewOpen, setEntitlementSqlPreviewOpen] = useState(false);
  // 対象ロールの一覧の検索語は作業状態に残す（#1933）。
  const [entitlementSearch, setEntitlementSearch] = useWorkspaceState("entitlementSearch", "");
  const [targetObjectSearch, setTargetObjectSearch] = useState("");
  const [targetObjects, setTargetObjects] = useState<DeepSecTargetObject[]>([]);
  const [targetObjectNextCursor, setTargetObjectNextCursor] = useState<string | null>(null);
  const [targetObjectTotal, setTargetObjectTotal] = useState<number | null>(null);
  const [targetDetails, setTargetDetails] = useState<Record<string, DeepSecTargetObjectDetail>>({});
  const [targetDetailLoading, setTargetDetailLoading] = useState<Record<string, boolean>>({});
  const [targetDetailErrors, setTargetDetailErrors] = useState<Record<string, string>>({});
  const [statusLoading, setStatusLoading] = useState(true);
  const [planLoading, setPlanLoading] = useState(true);
  const [entitlementLoading, setEntitlementLoading] = useState(true);
  const [targetObjectsLoading, setTargetObjectsLoading] = useState(true);
  const [targetObjectsLoadingMore, setTargetObjectsLoadingMore] = useState(false);
  const [relatedMetadataRefreshing, setRelatedMetadataRefreshing] = useState(false);
  const [foundationApplying, setFoundationApplying] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [entitlementPreviewing, setEntitlementPreviewing] = useState(false);
  const [entitlementApplying, setEntitlementApplying] = useState(false);
  const [dataUserPassword, setDataUserPassword] = useState("");
  const [configSaving, setConfigSaving] = useState(false);
  const [configSyncing, setConfigSyncing] = useState(false);
  const [configError, setConfigError] = useState("");
  const [statusLoadError, setStatusLoadError] = useState("");
  const [planLoadError, setPlanLoadError] = useState("");
  const [entitlementLoadError, setEntitlementLoadError] = useState("");
  const [targetObjectLoadError, setTargetObjectLoadError] = useState("");
  const [entitlementFormError, setEntitlementFormError] = useState("");
  const [entitlementApplyConfirmation, setEntitlementApplyConfirmation] = useState("");
  const [actionError, setActionError] = useState("");
  const [foundationApplyError, setFoundationApplyError] = useState("");
  const [resetError, setResetError] = useState("");
  const [foundationApplyPanelOpen, setFoundationApplyPanelOpen] = useState(true);
  const [foundationApplyConfirmation, setFoundationApplyConfirmation] = useState("");
  const [resetPanelOpen, setResetPanelOpen] = useState(false);
  const [resetConfirmation, setResetConfirmation] = useState("");
  const statusLoadSequence = useRef(0);
  const planLoadSequence = useRef(0);
  const entitlementLoadSequence = useRef(0);
  const targetObjectsLoadSequence = useRef(0);
  const entitlementDraftKeySequence = useRef(0);
  const entitlementEditorScrollRef = useRef<HTMLDivElement | null>(null);
  const pendingEntitlementScrollPositionsRef = useRef<ScrollPositionSnapshot[]>([]);
  const targetObjectFilterMounted = useRef(false);
  const { abortAll: abortStatusRequests, run: runStatusRequest } = useRequestScope();
  const { abortAll: abortPlanRequests, run: runPlanRequest } = useRequestScope();
  const { abortAll: abortEntitlementRequests, run: runEntitlementRequest } = useRequestScope();
  const { abortAll: abortTargetObjectRequests, run: runTargetObjectRequest } = useRequestScope();
  // ヘッダーの「最新情報を取得」は、押した読込（と初回の読込。messaging §3.7）の間だけ回す。操作の後の読み直し・
  // 検索語の変更・候補の続きの読込では回さない（#819）。refreshing は各領域の読込をまとめたもの。
  const headerRefresh = useActionPending();
  const statusRetry = useActionPending();
  const refreshing =
    statusLoading ||
    planLoading ||
    entitlementLoading ||
    targetObjectsLoading ||
    relatedMetadataRefreshing;

  const operationBusy = foundationApplying || resetting || verifying || entitlementPreviewing ||
    entitlementApplying || configSaving || configSyncing;
  const actionBlocked = operationBusy || statusLoading || planLoading || entitlementLoading || relatedMetadataRefreshing;

  const filteredEntitlementRoles = useMemo(() => {
    const q = entitlementSearch.trim().toLowerCase();
    return entitlementRoles
      .filter((role) => (q ? entitlementRoleSearchText(role).includes(q) : true))
      // ロール名を出すため、ロール名で並べる（#1682）。
      .sort(compareRoleDisplayNames);
  }, [entitlementRoles, entitlementSearch]);
  const visibleSelectedEntitlementRoleId =
    activeView === "data-permissions"
      ? selectedEntitlementRoleId ?? selectedVisibleKey(filteredEntitlementRoles, null, (role) => role.role_id)
      : selectedEntitlementRoleId;
  const currentEntitlementRole = useMemo(
    () => entitlementRoles.find((role) => role.role_id === visibleSelectedEntitlementRoleId) ?? null,
    [entitlementRoles, visibleSelectedEntitlementRoleId]
  );
  const selectedRoleMissing = Boolean(visibleSelectedEntitlementRoleId && !currentEntitlementRole);
  const selectedEntitlementRole = currentEntitlementRole ?? (
    entitlementBaseline?.role_id === visibleSelectedEntitlementRoleId ? entitlementBaseline : null
  );
  const targetObjectMap = useMemo(() => {
    return new Map(targetObjects.map((item) => [targetQualifiedName(item), item]));
  }, [targetObjects]);
  const visibleTargetObjects = useMemo(
    () =>
      targetObjects.filter((item) => {
        const name = item.name.toUpperCase();
        const owner = item.owner.toUpperCase();
        return !(
          owner === "SYS" ||
          owner === "SYSTEM" ||
          name.startsWith("NL2SQL_APP_") ||
          name.startsWith("NL2SQL_AUTH_") ||
          name.startsWith("NL2SQL_DEEPSEC_")
        );
      }),
    [targetObjects]
  );
  const entitlementReadOnly = Boolean(
    !mayManageEntitlements || selectedRoleMissing || selectedEntitlementRole?.is_built_in || selectedEntitlementRole?.archived
  );
  const normalizedEntitlementDraftRows = useMemo(
    () => normalizeEntitlementRows(entitlementDraftRows),
    [entitlementDraftRows]
  );
  const savedEntitlementRows = useMemo(
    () => normalizeEntitlementRows(entitlementBaseline?.data_entitlements ?? []),
    [entitlementBaseline]
  );
  const draftSignature = entitlementSignature(entitlementDraftRows);
  const entitlementDraftChanged = draftSignature !== entitlementSignature(savedEntitlementRows);
  const confirmDiscard = (confirmLabel = t("security.common.discardConfirm")) => confirm({
    title: t("security.common.discardTitle"),
    description: t("security.common.discardDescription"),
    confirmLabel,
    tone: "danger",
    dismissOnOverlay: false,
  });
  useUnsavedChangesGuard(operationBusy || entitlementDraftChanged || Boolean(dataUserPassword), async () => {
    if (operationBusy) return false;
    if (!(entitlementDraftChanged || dataUserPassword)) return true;
    if (!(await confirmDiscard())) return false;
    // 明示的な破棄は一時保存と現在の入力の両方へ反映する。
    // setState の effect は直後の unmount で実行されない場合があるため、先に同期保存する。
    if (!writeWorkspaceDraft("deepsec-rule-draft", "")) {
      setEntitlementFormError(t("workspace.storageFailed"));
      return false;
    }
    const baseline = currentEntitlementRole ?? entitlementBaseline;
    const rows = entitlementDraft(baseline);
    setEntitlementBaseline(baseline);
    setEntitlementDraftRows(rows);
    setSelectedEntitlementDraftKey(rows[0]?.client_key ?? null);
    setEntitlementPreview(null);
    setEntitlementSqlPreviewOpen(false);
    setEntitlementApplyConfirmation("");
    setDataUserPassword("");
    setStoredDraft("");
    return true;
  });
  const selectEntitlementRole = async (roleId: string) => {
    if (operationBusy || roleId === visibleSelectedEntitlementRoleId) return;
    if (entitlementDraftChanged && !(await confirmDiscard())) return;
    setSelectedEntitlementRoleId(roleId);
    setEntitlementFormError("");
  };
  useResetExecutionConsent(() => setEntitlementApplyConfirmation(""), draftSignature);
  useEffect(() => {
    if (!entitlementBaseline || entitlementLoading) return;
    // 一時保存はルール入力と比較 baseline のみ。SQL preview・credential・確認語は除外する。
    setStoredDraft(JSON.stringify({
      roleId: entitlementBaseline.role_id,
      version: entitlementBaseline.version,
      rows: normalizeEntitlementRows(entitlementDraftRows).map((row, i) => ({ ...row, client_key: entitlementDraftRows[i].client_key })),
      baseline: normalizeEntitlementRows(entitlementBaseline.data_entitlements),
    }));
  }, [draftSignature, entitlementBaseline, entitlementDraftRows, entitlementLoading, setStoredDraft]);
  const draftVersionChanged = Boolean(entitlementDraftChanged && currentEntitlementRole &&
    entitlementBaseline?.role_id === currentEntitlementRole.role_id &&
    entitlementBaseline.version !== currentEntitlementRole.version);
  const restartEntitlementDraft = async () => {
    if (actionBlocked || !currentEntitlementRole || !(await confirmDiscard(t("security.deepsec.entitlements.restartDraft")))) return;
    const rows = entitlementDraft(currentEntitlementRole);
    setEntitlementBaseline(currentEntitlementRole);
    setEntitlementDraftRows(rows);
    setSelectedEntitlementDraftKey(rows[0]?.client_key ?? null);
    setEntitlementPreview(null);
    setEntitlementSqlPreviewOpen(false);
    setEntitlementFormError("");
    setEntitlementApplyConfirmation("");
  };
  const planSignature = JSON.stringify(plan?.steps.map((step) => [step.checksum, step.status]));
  useEffect(() => {
    setEntitlementApplyConfirmation("");
  }, [draftSignature, visibleSelectedEntitlementRoleId, selectedEntitlementRole?.version, activeView]);
  useEffect(() => {
    setFoundationApplyConfirmation("");
    setResetConfirmation("");
  }, [activeView, planSignature]);
  const entitlementApplyConfirmed =
    entitlementApplyConfirmation.trim() === ADMIN_EXECUTE_CONFIRMATION;
  const selectedRolePreviewRows = entitlementPreview?.data_entitlements ?? [];
  const selectedRoleCleanupSql = entitlementPreview?.cleanup_sql ?? [];
  const selectedRolePreviewSqlCount =
    selectedRoleCleanupSql.length +
    selectedRolePreviewRows.reduce((count, item) => count + (item.sql?.length ?? 0), 0);
  const selectedDraftTargetKeys = useMemo(
    () => Array.from(new Set(entitlementDraftRows.map(entitlementTargetKey).filter(Boolean))),
    [entitlementDraftRows]
  );
  const visibleSelectedEntitlementDraftKey =
    selectedEntitlementDraftKey &&
    entitlementDraftRows.some((item) => item.client_key === selectedEntitlementDraftKey)
      ? selectedEntitlementDraftKey
      : entitlementDraftRows[0]?.client_key ?? null;
  const selectedEntitlementDraftIndex = visibleSelectedEntitlementDraftKey
    ? entitlementDraftRows.findIndex((item) => item.client_key === visibleSelectedEntitlementDraftKey)
    : -1;
  const selectedEntitlementDraft =
    selectedEntitlementDraftIndex >= 0 ? entitlementDraftRows[selectedEntitlementDraftIndex] : null;
  useLayoutEffect(() => {
    const snapshots = pendingEntitlementScrollPositionsRef.current;
    if (snapshots.length === 0) return;
    pendingEntitlementScrollPositionsRef.current = [];

    const restore = () => {
      for (const snapshot of snapshots) {
        if (!snapshot.element.isConnected) continue;
        snapshot.element.scrollTo({
          top: snapshot.top,
          left: snapshot.left,
          behavior: "auto",
        });
      }
    };

    restore();
    const frame = window.requestAnimationFrame(restore);
    return () => window.cancelAnimationFrame(frame);
  }, [entitlementDraftRows]);
  useEffect(() => {
    if (!visibleSelectedEntitlementDraftKey || typeof window === "undefined") return;
    const frame = window.requestAnimationFrame(() => {
      entitlementEditorScrollRef.current?.scrollTo({ top: 0, left: 0, behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [visibleSelectedEntitlementDraftKey]);
  const foundationSteps = useMemo(
    () =>
      (plan?.steps.filter((step) => step.step_no === 1 || step.step_no === 2) ?? []).sort(
        (left, right) => left.step_no - right.step_no
      ),
    [plan]
  );
  const foundationReady = foundationSteps.length === 2 && foundationSteps.every((step) => step.status === "APPLIED");
  const foundationPendingSteps = foundationSteps.filter((step) => step.status !== "APPLIED");
  const foundationApplyVisible = Boolean(mayApply && foundationPendingSteps.length > 0);
  const foundationApplyConfirmed =
    foundationApplyConfirmation.trim() === ADMIN_EXECUTE_CONFIRMATION;
  const foundationApplyBlocked = Boolean(
    !plan?.deepsec_enabled ||
      !plan.has_data_user_password ||
      foundationPendingSteps.length === 0 ||
      foundationSteps.some((step) => step.status === "RUNNING")
  );
  const resetAvailable = Boolean(
    plan?.steps.some((step) => step.status === "APPLIED" || step.status === "FAILED")
  );
  const resetConfirmed = resetConfirmation.trim() === ADMIN_RESET_CONFIRMATION;
  const hasSavedDataUserPassword = Boolean(status?.has_data_user_password ?? plan?.has_data_user_password);
  const passwordSyncDisabled = Boolean(
    !hasSavedDataUserPassword || dataUserPassword || configSaving || configSyncing
  );

  // keepError: 失敗の案内の「再試行」から呼んだ。取り直せるまで案内を残し、押したボタンだけを回す
  // （押すと案内が消えて、進行がヘッダーのバッジだけになるのを防ぐ。messaging §10.2、#2034）。
  const loadStatus = async ({ keepError = false }: { keepError?: boolean } = {}) => {
    const sequence = statusLoadSequence.current + 1;
    let completed = false;
    statusLoadSequence.current = sequence;
    setStatusLoading(true);
    if (!keepError) setStatusLoadError("");
    try {
      await runStatusRequest(async (signal) => {
        const nextStatus = await securityApi.deepSecStatus({ signal });
        if (signal.aborted || sequence !== statusLoadSequence.current) return;
        setStatus(nextStatus);
        setStatusLoadError("");
        completed = true;
      });
    } catch (cause) {
      if (isAbortError(cause)) {
        return false;
      }
      if (sequence === statusLoadSequence.current) {
        setStatusLoadError(loadErrorMessage(cause));
      }
    } finally {
      if (sequence === statusLoadSequence.current) setStatusLoading(false);
    }
    return completed;
  };

  const loadPlan = async () => {
    const sequence = planLoadSequence.current + 1;
    let completed = false;
    planLoadSequence.current = sequence;
    setPlanLoading(true);
    setPlanLoadError("");
    try {
      await runPlanRequest(async (signal) => {
        const nextPlan = await securityApi.deepSecPlan({ signal });
        if (signal.aborted || sequence !== planLoadSequence.current) return;
        setPlan(nextPlan);
        completed = true;
      });
    } catch (cause) {
      if (isAbortError(cause)) {
        return false;
      }
      if (sequence === planLoadSequence.current) {
        setPlanLoadError(loadErrorMessage(cause));
      }
    } finally {
      if (sequence === planLoadSequence.current) setPlanLoading(false);
    }
    return completed;
  };

  const loadEntitlements = async () => {
    const sequence = entitlementLoadSequence.current + 1;
    let completed = false;
    entitlementLoadSequence.current = sequence;
    setEntitlementLoading(true);
    setEntitlementLoadError("");
    try {
      await runEntitlementRequest(async (signal) => {
        const rows = await securityApi.deepSecDataEntitlements({ signal });
        if (signal.aborted || sequence !== entitlementLoadSequence.current) return;
        setEntitlementRoles(rows);
        completed = true;
      });
    } catch (cause) {
      if (isAbortError(cause)) {
        return false;
      }
      if (sequence === entitlementLoadSequence.current) {
        setEntitlementLoadError(loadErrorMessage(cause));
      }
    } finally {
      if (sequence === entitlementLoadSequence.current) setEntitlementLoading(false);
    }
    return completed;
  };

  const loadTargetObjects = async ({
    cursor = null,
    append = false,
  }: {
    cursor?: string | null;
    append?: boolean;
  } = {}) => {
    const sequence = targetObjectsLoadSequence.current + 1;
    let completed = false;
    targetObjectsLoadSequence.current = sequence;
    if (append) {
      setTargetObjectsLoadingMore(true);
    } else {
      setTargetObjectsLoading(true);
    }
    setTargetObjectLoadError("");
    try {
      await runTargetObjectRequest(async (signal) => {
        const page = await securityApi.deepSecTargetObjects({
          signal,
          q: targetObjectSearch,
          cursor,
          limit: TARGET_OBJECT_PAGE_SIZE,
        });
        if (signal.aborted || sequence !== targetObjectsLoadSequence.current) return;
        setTargetObjects((current) =>
          append ? mergeTargetObjectPages(current, page.items) : page.items
        );
        setTargetObjectNextCursor(page.next_cursor ?? null);
        setTargetObjectTotal(typeof page.total === "number" ? page.total : null);
        completed = true;
      });
    } catch (cause) {
      if (isAbortError(cause)) {
        return false;
      }
      if (sequence === targetObjectsLoadSequence.current) {
        setTargetObjectLoadError(loadErrorMessage(cause));
      }
    } finally {
      if (sequence === targetObjectsLoadSequence.current) {
        if (append) {
          setTargetObjectsLoadingMore(false);
        } else {
          setTargetObjectsLoading(false);
        }
      }
    }
    return completed;
  };

  const loadTargetDetail = async (object: DeepSecTargetObject) => {
    const key = targetQualifiedName(object);
    if (targetDetails[key] || targetDetailLoading[key]) return;
    setTargetDetailLoading((current) => ({ ...current, [key]: true }));
    setTargetDetailErrors((current) => ({ ...current, [key]: "" }));
    try {
      const detail = await securityApi.deepSecTargetObjectDetail(object);
      setTargetDetails((current) => ({ ...current, [key]: detail }));
    } catch (cause) {
      setTargetDetailErrors((current) => ({ ...current, [key]: loadErrorMessage(cause) }));
    } finally {
      setTargetDetailLoading((current) => ({ ...current, [key]: false }));
    }
  };

  const refreshRelatedMetadata = async () => {
    const queryKey = ["nl2sql", "deepsec", user?.user_uuid, database?.context_id];
    setRelatedMetadataRefreshing(true);
    setEntitlementApplyConfirmation("");
    setEntitlementPreview(null);
    try {
      // 非表示ルールのキャッシュも無効化し、表示中の全問い合わせの完了を待つ。
      // 個別の取得エラーはカード内に表示し、更新全体を成功として通知しない。
      await queryClient.invalidateQueries({ queryKey, refetchType: "active" });
      return queryClient.getQueryCache().findAll({ queryKey, type: "active" })
        .every((query) => query.state.status !== "error");
    } finally {
      setRelatedMetadataRefreshing(false);
    }
  };

  const loadTargetObjectsRef = useRef(loadTargetObjects);
  const loadTargetDetailRef = useRef(loadTargetDetail);
  useLayoutEffect(() => {
    loadTargetObjectsRef.current = loadTargetObjects;
    loadTargetDetailRef.current = loadTargetDetail;
  });

  const load = async (announce = false) => {
    setActionError("");
    const results = await Promise.all([
      loadStatus(),
      loadPlan(),
      loadEntitlements(),
      loadTargetObjects(),
      announce ? refreshRelatedMetadata() : Promise.resolve(true),
    ]);
    if (announce && results.every(Boolean)) {
      toast.success(t("common.action.refreshed"));
    }
  };

  // 初回ロードは mount 時だけ行う。最新の load を commit 時に ref へ入れて呼ぶ（load を deps に入れると毎レンダーで再取得になる）。
  const loadRef = useRef(load);
  useLayoutEffect(() => {
    loadRef.current = load;
  });
  const trackHeaderRefreshRef = useRef(headerRefresh.track);
  useEffect(() => {
    // 初回の読込は PageHeader の唯一の操作「最新情報を取得」がスピナーを出す（messaging §3.7）。
    void trackHeaderRefreshRef.current(() => loadRef.current());
    return () => {
      statusLoadSequence.current += 1;
      planLoadSequence.current += 1;
      entitlementLoadSequence.current += 1;
      targetObjectsLoadSequence.current += 1;
      abortStatusRequests();
      abortPlanRequests();
      abortEntitlementRequests();
      abortTargetObjectRequests();
    };
  }, [abortEntitlementRequests, abortPlanRequests, abortStatusRequests, abortTargetObjectRequests]);

  useEffect(() => {
    if (activeView !== "data-permissions" || entitlementLoading) return;
    setSelectedEntitlementRoleId((current) =>
      current ?? selectedVisibleKey(filteredEntitlementRoles, null, (role) => role.role_id)
    );
  }, [activeView, entitlementLoading, filteredEntitlementRoles]);

  useEffect(() => {
    if (!targetObjectFilterMounted.current) {
      targetObjectFilterMounted.current = true;
      return;
    }
    // 検索語は選択欄の SearchField が確定した値（300ms・Enter・消去。IME の変換中は確定しない）なので、
    // ここで遅延させずに問い合わせる（UX 契約「一覧の絞り込みの検索」7。#608）。
    void loadTargetObjectsRef.current();
  }, [targetObjectSearch]);

  // 選択中の role / version が変わったときだけ草稿を作り直す。他の値は最新を読むが、変化では実行しない。
  const syncEntitlementRole = () => {
    // 背景再取得は編集中の baseline/version を更新しない。適用時は旧 version で競合を検出する。
    if (entitlementDraftChanged && entitlementBaseline?.role_id === visibleSelectedEntitlementRoleId) return;
    if (selectedEntitlementRole && restoredDraft.current?.roleId === selectedEntitlementRole.role_id) {
      const saved = restoredDraft.current;
      restoredDraft.current = null;
      // 未編集の保存状態は最新のサーバー設定を使う。実際の草稿だけ旧 version を保持する。
      if (entitlementSignature(saved.rows) !== entitlementSignature(saved.baseline)) {
        setEntitlementBaseline({ ...selectedEntitlementRole, version: saved.version, data_entitlements: saved.baseline });
        setEntitlementDraftRows(saved.rows);
        setSelectedEntitlementDraftKey(saved.rows[0]?.client_key ?? null);
        setEntitlementApplyConfirmation("");
        return;
      }
    }
    const nextDraftRows = entitlementDraft(selectedEntitlementRole);
    setEntitlementBaseline(selectedEntitlementRole);
    setEntitlementDraftRows(nextDraftRows);
    setSelectedEntitlementDraftKey(nextDraftRows[0]?.client_key ?? null);
    setEntitlementPreview(null);
    setEntitlementSqlPreviewOpen(false);
    setEntitlementFormError("");
    setEntitlementApplyConfirmation("");
  };
  const syncEntitlementRoleRef = useRef(syncEntitlementRole);
  useLayoutEffect(() => {
    syncEntitlementRoleRef.current = syncEntitlementRole;
  });
  const syncedEntitlementRoleId = selectedEntitlementRole?.role_id;
  const syncedEntitlementRoleVersion = selectedEntitlementRole?.version;
  useEffect(() => {
    syncEntitlementRoleRef.current();
  }, [syncedEntitlementRoleId, syncedEntitlementRoleVersion]);

  // 対象の集合（signature）が変わったときに詳細を取る。最新の loadTargetDetail は ref から呼ぶ。
  const selectedDraftTargetSignature = selectedDraftTargetKeys.join("|");
  useEffect(() => {
    for (const key of selectedDraftTargetSignature.split("|").filter(Boolean)) {
      const draft = entitlementDraftRows.find((item) => entitlementTargetKey(item) === key);
      const object =
        targetObjectMap.get(key) ??
        (draft?.target_owner && draft?.target_object
          ? {
              owner: draft.target_owner,
              name: draft.target_object,
              qualified_name: key,
              object_type: draft.target_type ?? "TABLE",
              comment: "",
            }
          : null);
      if (object) void loadTargetDetailRef.current(object);
    }
  }, [selectedDraftTargetSignature, targetObjectMap, entitlementDraftRows]);

  useEffect(() => {
    if (foundationApplyVisible) {
      setFoundationApplyPanelOpen(true);
      return;
    }
    setFoundationApplyPanelOpen(false);
    setFoundationApplyConfirmation("");
    setFoundationApplyError("");
  }, [foundationApplyVisible]);

  useEffect(() => {
    if (!resetAvailable) {
      setResetPanelOpen(false);
      setResetConfirmation("");
      setResetError("");
    }
  }, [resetAvailable]);

  const validateConfigForm = () => {
    if (dataUserPassword.length < 12 || dataUserPassword.length > 256) {
      return t("security.deepsec.config.passwordLength");
    }
    // 「${」は backend/.env を読むときに環境変数として展開され、保存した値と変わるので拒否する（backend と同じ。#1000）。
    // 制御文字を含むパスワードを拒否するため、制御文字の範囲指定は意図どおり。
    if (
      dataUserPassword.includes("\"") ||
      dataUserPassword.includes("${") ||
      // oxlint-disable-next-line no-control-regex
      /[\x00-\x1f\x7f-\x9f]/.test(dataUserPassword)
    ) {
      return t("security.deepsec.config.passwordChars");
    }
    return "";
  };

  const handleSaveConfig = async () => {
    if (actionBlocked) return;
    const validationError = validateConfigForm();
    if (validationError) {
      setConfigError(validationError);
      return;
    }
    setConfigSaving(true);
    setConfigError("");
    setActionError("");
    try {
      const nextStatus = await securityApi.updateDeepSecConfig(dataUserPassword);
      setStatus(nextStatus);
      setDataUserPassword("");
      toast.success(t("security.deepsec.config.saved"));
      await loadPlan();
    } catch (cause) {
      setConfigError(cause instanceof Error ? cause.message : t("security.common.saveError"));
    } finally {
      setConfigSaving(false);
    }
  };

  const handleSyncConfig = async () => {
    if (actionBlocked) return;
    if (!hasSavedDataUserPassword) {
      setConfigError(t("security.deepsec.config.syncMissing"));
      return;
    }
    if (dataUserPassword) return;
    setConfigSyncing(true);
    setConfigError("");
    setActionError("");
    try {
      const nextStatus = await securityApi.syncDeepSecConfigPassword();
      setStatus(nextStatus);
      toast.success(t("security.deepsec.config.synced"));
      await loadPlan();
    } catch (cause) {
      setConfigError(cause instanceof Error ? cause.message : t("security.common.saveError"));
    } finally {
      setConfigSyncing(false);
    }
  };

  const handleApplyFoundation = async () => {
    if (!plan || actionBlocked) return;
    const confirmation = foundationApplyConfirmation.trim();
    if (confirmation !== ADMIN_EXECUTE_CONFIRMATION) {
      setFoundationApplyError(t("security.deepsec.applyFoundationConfirmationRequired"));
      return;
    }
    const stepsToApply = foundationPendingSteps
      .filter((step) => step.status !== "RUNNING")
      .sort((left, right) => left.step_no - right.step_no);
    if (stepsToApply.length === 0) return;
    setFoundationApplying(true);
    setFoundationApplyError("");
    try {
      for (const step of stepsToApply) {
        await securityApi.applyDeepSecStep(plan.version, step, confirmation);
      }
      setFoundationApplyConfirmation("");
      setFoundationApplyPanelOpen(false);
      toast.success(t("security.deepsec.applyFoundationDone"));
      await Promise.all([loadStatus(), loadPlan(), loadEntitlements()]);
    } catch (cause) {
      setFoundationApplyError(cause instanceof Error ? cause.message : t("security.common.saveError"));
      await Promise.all([loadStatus(), loadPlan()]);
    } finally {
      setFoundationApplying(false);
    }
  };

  const handleReset = async () => {
    if (!plan || actionBlocked) return;
    if (resetConfirmation.trim() !== ADMIN_RESET_CONFIRMATION) {
      setResetError(t("security.deepsec.resetConfirmationRequired"));
      return;
    }
    setResetting(true);
    setResetError("");
    try {
      await securityApi.resetDeepSecPlan(plan.version, resetConfirmation.trim());
      setResetConfirmation("");
      setResetPanelOpen(false);
      setVerification(null);
      toast.success(t("security.deepsec.resetDone"));
      await load();
    } catch (cause) {
      setResetError(cause instanceof Error ? cause.message : t("security.common.saveError"));
    } finally {
      setResetting(false);
    }
  };

  const handleVerify = async () => {
    if (actionBlocked) return;
    if (
      !(await confirm({
        title: t("security.deepsec.verify"),
        description: t("security.deepsec.verifyConfirm"),
        tone: "info",
      }))
    ) {
      return;
    }
    setVerifying(true);
    setActionError("");
    try {
      const result = await securityApi.verifyDeepSec();
      setVerification(result);
      toast.success(t("security.deepsec.verified"));
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : t("security.common.saveError"));
    } finally {
      setVerifying(false);
    }
  };

  const addEntitlement = () => {
    const prefix = `new:${selectedEntitlementRole?.role_id ?? "role"}:`;
    let clientKey: string;
    do {
      clientKey = `${prefix}${++entitlementDraftKeySequence.current}`;
    } while (entitlementDraftRows.some((row) => row.client_key === clientKey));
    const nextDraft = blankEntitlementDraft(clientKey);
    setEntitlementPreview(null);
    setEntitlementSqlPreviewOpen(false);
    setEntitlementDraftRows((current) => [...current, nextDraft]);
    setSelectedEntitlementDraftKey(nextDraft.client_key);
    setEntitlementFormError("");
  };

  const patchEntitlement = (index: number, patch: Partial<DataEntitlement>) => {
    setEntitlementPreview(null);
    setEntitlementSqlPreviewOpen(false);
    setEntitlementDraftRows((current) =>
      current.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item))
    );
    setEntitlementFormError("");
  };

  const updateEntitlementTarget = (index: number, value: string) => {
    const object = targetObjectMap.get(value);
    if (!object) {
      patchEntitlement(index, {
        resource_code: value,
        target_owner: "",
        target_object: "",
        target_type: "TABLE",
        column_names: [],
        scope_mode: "ALL",
        scope_column: "",
        scope_filters: [],
        scope_expression: null,
        ...(entitlementDraftRows[index]?.scope_mode === "EXPRESSION" ? { scope_expression_version: 1 as const } : {}),
        scope_code: "*",
      });
      return;
    }
    patchEntitlement(index, {
      resource_code: targetQualifiedName(object),
      // 一覧の owner / name はカタログ上の値。保存値は引用が必要な部分だけ "..." にした token。
      target_owner: formatDbObjectPart(object.owner),
      target_object: formatDbObjectPart(object.name),
      target_type: object.object_type,
      column_names: [],
      scope_mode: "ALL",
      scope_column: "",
      scope_filters: [],
      scope_expression: null,
      ...(entitlementDraftRows[index]?.scope_mode === "EXPRESSION" ? { scope_expression_version: 1 as const } : {}),
      scope_code: "*",
      capability: "SELECT",
    });
    void loadTargetDetail(object);
  };

  const toggleEntitlementColumn = (index: number, columnName: string, checked: boolean) => {
    setEntitlementPreview(null);
    setEntitlementDraftRows((current) =>
      current.map((item, itemIndex) => {
        if (itemIndex !== index) return item;
        const columns = new Set((item.column_names ?? []).map(normalizeDbIdentifierToken));
        if (checked) {
          columns.add(normalizeDbIdentifierToken(columnName));
        } else {
          columns.delete(normalizeDbIdentifierToken(columnName));
        }
        return { ...item, column_names: Array.from(columns) };
      })
    );
    setEntitlementFormError("");
  };

  const setEntitlementColumns = (index: number, columnNames: string[]) => {
    patchEntitlement(index, {
      column_names: Array.from(
        new Set(columnNames.map(normalizeDbIdentifierToken).filter(Boolean))
      ),
    });
  };

  const setEntitlementColumnsPreservingScroll = (index: number, columnNames: string[]) => {
    const editor = entitlementEditorScrollRef.current;
    if (editor) {
      const main = editor.closest<HTMLElement>("main");
      const internalContainers = Array.from(
        editor.querySelectorAll<HTMLElement>("[data-entitlement-scroll-container]")
      );
      const seen = new Set<HTMLElement>();
      pendingEntitlementScrollPositionsRef.current = [main, editor, ...internalContainers]
        .filter((element): element is HTMLElement => {
          if (!element || seen.has(element)) return false;
          seen.add(element);
          return true;
        })
        .map((element) => {
          return {
            element,
            top: element.scrollTop,
            left: element.scrollLeft,
          };
        });
    }
    setEntitlementColumns(index, columnNames);
  };

  const removeEntitlement = (index: number) => {
    const nextRows = entitlementDraftRows.filter((_, itemIndex) => itemIndex !== index);
    const removedKey = entitlementDraftRows[index]?.client_key ?? null;
    setEntitlementPreview(null);
    setEntitlementSqlPreviewOpen(false);
    setEntitlementDraftRows(nextRows);
    setSelectedEntitlementDraftKey((current) => {
      if (current && current !== removedKey && nextRows.some((item) => item.client_key === current)) {
        return current;
      }
      return nextRows[Math.min(index, nextRows.length - 1)]?.client_key ?? null;
    });
    setEntitlementFormError("");
  };

  const validateEntitlements = () => {
    for (const item of normalizedEntitlementDraftRows) {
      if (
        !item.resource_code ||
        !item.target_owner ||
        !item.target_object ||
        !item.scope_code ||
        item.column_names.length === 0 ||
        !ENTITLEMENT_CAPABILITIES.includes(
          item.capability as (typeof ENTITLEMENT_CAPABILITIES)[number]
        ) ||
        !SCOPE_MODES.includes(item.scope_mode as (typeof SCOPE_MODES)[number])
      ) {
        return t("security.deepsec.entitlements.validation");
      }
      if (item.scope_mode === "EXPRESSION") {
        const error = expressionError(item.scope_expression);
        if (error) return t(`security.deepsec.entitlements.${error}`);
      }
      if (item.scope_mode === "FILTERS") {
        if (!item.scope_filters.length) {
          return t("security.deepsec.entitlements.scopeFilterValidation");
        }
        for (const filter of item.scope_filters) {
          const valueSource = normalizeScopeFilterValueSource(filter.value_source);
          if (
            !filter.column_name ||
            !filter.operator ||
            !["TEXT", "NUMBER", "TEMPORAL"].includes(filter.value_type)
          ) {
            return t("security.deepsec.entitlements.scopeFilterValidation");
          }
          if (valueSource === LOGIN_USER_ID_SCOPE_VALUE_SOURCE) {
            if (!scopeFilterSupportsValueSource(filter.operator, filter.value_type)) {
              return t("security.deepsec.entitlements.scopeFilterValidation");
            }
            continue;
          }
          if (scopeFilterNeedsValues(filter.operator) && !filter.values?.length) {
            return t("security.deepsec.entitlements.scopeFilterValidation");
          }
          if (
            scopeFilterNeedsValue(filter.operator) &&
            !scopeFilterNeedsValues(filter.operator) &&
            !filter.value
          ) {
            return t("security.deepsec.entitlements.scopeFilterValidation");
          }
          if (
            filter.value_type === "NUMBER" &&
            filter.operator === "EQ" &&
            !isPositiveIntegerScopeValue(filter.value)
          ) {
            return t("security.deepsec.entitlements.scopeFilterPositiveIntegerValidation");
          }
          if (scopeFilterNeedsValueTo(filter.operator) && !filter.value_to) {
            return t("security.deepsec.entitlements.scopeFilterValidation");
          }
        }
      }
    }
    return "";
  };

  const handlePreviewEntitlements = async () => {
    if (!selectedEntitlementRole || entitlementReadOnly || actionBlocked) return;
    const validationError = validateEntitlements();
    if (validationError) {
      setEntitlementFormError(validationError);
      return;
    }
    setEntitlementPreviewing(true);
    setEntitlementFormError("");
    setActionError("");
    try {
      const preview = await securityApi.previewDeepSecDataEntitlements(
        {
          ...selectedEntitlementRole,
          version: entitlementBaseline?.version ?? selectedEntitlementRole.version,
          data_entitlements: normalizedEntitlementDraftRows,
        }
      );
      setEntitlementPreview(preview);
      setEntitlementSqlPreviewOpen(true);
      setEntitlementDraftRows((current) =>
        preview.data_entitlements.map((item, index) => {
          const currentDraft = current[index];
          return {
            ...currentDraft,
            ...item,
            client_key: currentDraft?.client_key ?? entitlementDraftClientKey(item, index),
          };
        })
      );
      toast.success(t("security.deepsec.entitlements.previewed"));
    } catch (cause) {
      setEntitlementFormError(cause instanceof Error ? cause.message : t("security.common.loadError"));
    } finally {
      setEntitlementPreviewing(false);
    }
  };

  const handleApplyEntitlements = async () => {
    if (!selectedEntitlementRole || entitlementReadOnly || actionBlocked) return;
    const validationError = validateEntitlements();
    if (validationError) {
      setEntitlementFormError(validationError);
      return;
    }
    if (entitlementApplyConfirmation.trim() !== ADMIN_EXECUTE_CONFIRMATION) {
      setEntitlementFormError(t("security.deepsec.entitlements.applyConfirmationRequired"));
      return;
    }
    setEntitlementApplying(true);
    setEntitlementFormError("");
    setActionError("");
    try {
      const result = await securityApi.applyDeepSecDataEntitlements(
        {
          ...selectedEntitlementRole,
          version: entitlementBaseline?.version ?? selectedEntitlementRole.version,
          data_entitlements: normalizedEntitlementDraftRows,
        },
        entitlementApplyConfirmation.trim()
      );
      const updated = result.role;
      const nextDraftRows = entitlementDraft(updated);
      setEntitlementBaseline(updated);
      setEntitlementDraftRows(nextDraftRows);
      setSelectedEntitlementDraftKey(nextDraftRows[0]?.client_key ?? null);
      setEntitlementRoles((rows) =>
        rows.map((role) => (role.role_id === updated.role_id ? updated : role))
      );
      setEntitlementApplyConfirmation("");
      setEntitlementPreview(null);
      setVerification(null);
      toast.success(t("security.deepsec.entitlements.appliedToast"));
      await load();
    } catch (cause) {
      setEntitlementFormError(cause instanceof Error ? cause.message : t("security.common.saveError"));
    } finally {
      setEntitlementApplying(false);
    }
  };

  return (
    <>
      <PageHeader wide
        title={t("nav.securityDeepSec")}
        subtitle={t("security.deepsec.subtitle")}
        status={
          statusLoadError ? (
            <PageHeaderStatusBadge variant="danger" label={t("security.deepsec.statusLoadFailed")} />
          ) : statusLoading && !status ? (
            <PageHeaderStatusBadge variant="info" label={t("security.deepsec.statusChecking")} />
          ) : (
            <PageHeaderStatusBadge
              variant={status?.configured ? "success" : "warning"}
              label={status?.configured ? t("security.deepsec.statusConfigured") : t("security.deepsec.statusUnconfigured")}
            />
          )
        }
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            // 押した読込の間は loading（aria-disabled）だけにし、ネイティブの disabled でフォーカスを外さない。
            disabled: !headerRefresh.pending && (actionBlocked || targetObjectsLoadingMore || refreshing),
            onClick: () => {
              if (!actionBlocked && !targetObjectsLoadingMore) return headerRefresh.track(() => load(true));
            },
            loading: headerRefresh.pending,
          },
        ]}
      />
      <PageBody wide className="grid gap-4">
        <fieldset disabled={operationBusy} className="contents" aria-busy={operationBusy} data-testid="security-deepsec-controls">
        <PageNotice
          notice={statusLoadError ? { tone: "danger", message: statusLoadError } : null}
          action={
            statusLoadError ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                loading={statusRetry.pending}
                onClick={() => {
                  if (!statusRetry.pending) void statusRetry.track(() => loadStatus({ keepError: true }));
                }}
                icon={RefreshCw}
                data-testid="security-deepsec-status-retry"
              >
                {t("common.retry")}
              </Button>
            ) : null
          }
        />
        {plan && !plan.deepsec_enabled ? (
          <Banner severity="warning">{t("security.deepsec.banner.disabled")}</Banner>
        ) : null}
        {plan?.deepsec_enabled && !plan.has_data_user_password ? (
          <Banner severity="warning">{t("security.deepsec.banner.passwordMissing")}</Banner>
        ) : null}
        <ManagementTabs
          activeView={activeView}
          tabs={[
            { id: "data-user", label: t("security.deepsec.tabs.dataUser"), icon: KeyRound },
            { id: "foundation", label: t("security.deepsec.tabs.foundation"), icon: Settings2 },
            { id: "data-permissions", label: t("security.deepsec.tabs.dataPermissions"), icon: ShieldCheck },
          ]}
          idPrefix="security-deepsec"
          ariaLabel={t("security.deepsec.tabs.label")}
          onViewChange={(view) => {
            setActiveView(view);
            setActionError("");
            setResetError("");
          }}
        />

        {activeView === "data-user" ? (
          <ManagementPanelShell
            id="security-deepsec-panel-data-user"
            labelledBy="security-deepsec-tab-data-user"
            ariaLabel={t("security.deepsec.tabs.dataUser")}
          >
            <ManagementPanelHeader
              title={t("security.deepsec.config.title")}
              description={t("security.deepsec.tabs.dataUserHint")}
              icon={KeyRound}
            />
            <form
              className="grid gap-4"
              onSubmit={(event) => {
                event.preventDefault();
                void handleSaveConfig();
              }}
            >
              {/* フォームの本文は FormGrid（最大 60rem・置き場所 42rem 以上で 2 列。#1891）。 */}
              <FormGrid data-testid="security-deepsec-config-fields">
                <dl className="rounded-md border border-border bg-surface-sunken p-3 text-sm">
                  <dt className="text-fg-muted">{t("security.deepsec.dataUser")}</dt>
                  <dd className="mt-1 break-all font-mono">
                    {status?.data_user ?? plan?.data_user ?? "DEEPSEC_DATA_USER"}
                  </dd>
                </dl>
                <div className="space-y-2">
                  {/* 未保存のときは DeepSec の適用にパスワードの保存が必要。保存済みなら変更時だけ入力する（#531）。
                      保存済みの状態は入力欄の補足（aria-describedby）で伝える。SecretField は表示の切り替えの文言キーが
                      この画面に無いため、同じ見た目の TextField（type="password"）にする（#631）。 */}
                  <TextField
                    id="deepsec-data-user-password"
                    type="password"
                    label={t("security.deepsec.config.password")}
                    width="md"
                    required={!hasSavedDataUserPassword}
                    autoComplete="new-password"
                    className="min-w-0"
                    value={dataUserPassword}
                    onValueChange={(value) => {
                      setDataUserPassword(value);
                      setConfigError("");
                    }}
                    placeholder={
                      hasSavedDataUserPassword
                        ? t("security.deepsec.config.passwordPlaceholderSaved")
                        : t("security.deepsec.config.passwordPlaceholderNew")
                    }
                    helper={
                      hasSavedDataUserPassword
                        ? t("security.deepsec.config.secretSaved")
                        : t("security.deepsec.config.secretMissing")
                    }
                    aria-invalid={Boolean(configError)}
                  />
                  {configError ? <FormStatus tone="danger" message={configError} /> : null}
                </div>
              </FormGrid>
              <div
                className="flex flex-wrap items-center gap-2 border-t border-border pt-4"
                data-testid="security-deepsec-config-actions"
              >
                <Button
                  type="submit"
                  size="lg"
                  loading={configSaving}
                  disabled={!dataUserPassword || actionBlocked}
                  className="w-full whitespace-nowrap sm:w-auto" icon={Save}>
                  {t("security.deepsec.config.save")}
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  size="lg"
                  loading={configSyncing}
                  disabled={passwordSyncDisabled || actionBlocked}
                  className="w-full whitespace-nowrap sm:w-auto"
                  onClick={() => void handleSyncConfig()} icon={RefreshCw}>
                  {t("security.deepsec.config.sync")}
                </Button>
              </div>
              {configSaving || configSyncing ? (
                // 保存と同期は Oracle のユーザーとパスワードを更新するため数秒かかる（messaging.md §3.7）。
                <ProcessingIndicator
                  active
                  label={t(configSyncing ? "security.deepsec.progress.syncingConfig" : "security.deepsec.progress.savingConfig")}
                  operationKey={configSyncing ? "security-deepsec-config-sync" : "security-deepsec-config-save"}
                  placement="action"
                  activityIcon="none"
                  testId="security-deepsec-config-processing"
                />
              ) : null}
            </form>
          </ManagementPanelShell>
        ) : null}

        {activeView === "foundation" ? (
          <ManagementPanelShell
            id="security-deepsec-panel-foundation"
            labelledBy="security-deepsec-tab-foundation"
            ariaLabel={t("security.deepsec.tabs.foundation")}
          >
            <ManagementPanelHeader
              title={t("security.deepsec.tabs.foundation")}
              description={t("security.deepsec.tabs.foundationHint")}
              icon={Settings2}
            />
            {actionError ? <FormStatus tone="danger" message={actionError} /> : null}
            {plan && !plan.has_data_user_password ? (
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0 flex-1">
                  <Banner severity="info">{t("security.deepsec.prerequisite.password")}</Banner>
                </div>
                <Button type="button" variant="secondary" size="sm" onClick={() => setActiveView("data-user")}>
                  {t("security.deepsec.actions.openDataUser")}
                </Button>
              </div>
            ) : null}
            <section className="grid gap-4" aria-labelledby="security-deepsec-foundation-plan-title">
              <h3 id="security-deepsec-foundation-plan-title" className="text-base font-semibold">
                {t("security.deepsec.plan")}
              </h3>
              <DeepSecPlanSteps
                plan={plan}
                stepNumbers={[1, 2]}
                loading={planLoading}
                headerSpinning={headerRefresh.pending}
                loadError={planLoadError}
                onRetry={() => void loadPlan()}
              />
              {foundationApplyVisible ? (
                <WorkSection
                  title={t("security.deepsec.applySectionTitle")}
                  description={t("security.deepsec.applySectionDescription")}
                  open={foundationApplyPanelOpen}
                  dataTestId="security-deepsec-foundation-apply-section"
                  onOpenChange={(open) => {
                    setFoundationApplyPanelOpen(open);
                    if (!open) {
                      setFoundationApplyConfirmation("");
                      setFoundationApplyError("");
                    }
                  }}
                >
                  <div className="grid gap-3">
                    {foundationApplyError ? <FormStatus tone="danger" message={foundationApplyError} /> : null}
                    <ExecutionConfirmationField
                      value={foundationApplyConfirmation}
                      onChange={(value) => {
                        setFoundationApplyConfirmation(value);
                        setFoundationApplyError("");
                      }}
                      confirmed={foundationApplyConfirmed}
                      placeholder={ADMIN_EXECUTE_CONFIRMATION}
                      expectedLabel={ADMIN_EXECUTE_CONFIRMATION}
                      helper={t("security.deepsec.applyFoundationHelper", {
                        phrase: ADMIN_EXECUTE_CONFIRMATION,
                      })}
                      disabled={foundationApplyBlocked || actionBlocked}
                      actions={
                        <>
                          <Button
                            type="button"
                            size="lg"
                            className="w-full sm:w-auto"
                            loading={foundationApplying}
                            disabled={
                              !foundationApplyConfirmed ||
                              foundationApplyBlocked ||
                              actionBlocked
                            }
                            onClick={() => void handleApplyFoundation()} icon={Play}>
                            {t("security.deepsec.applyFoundation")}
                          </Button>
                          <Button
                            type="button"
                            variant="secondary"
                            size="lg"
                            className="w-full sm:w-auto"
                            disabled={foundationApplying}
                            onClick={() => {
                              setFoundationApplyPanelOpen(false);
                              setFoundationApplyConfirmation("");
                              setFoundationApplyError("");
                            }}
                          >
                            {t("common.cancel")}
                          </Button>
                        </>
                      }
                    />
                    {foundationApplying ? (
                      // 基盤の適用は DDL とポリシーの作成を順に実行し、数十秒かかる（messaging.md §3.7）。
                      <ProcessingIndicator
                        active
                        label={t("security.deepsec.progress.applyingFoundation")}
                        operationKey="security-deepsec-apply-foundation"
                        placement="action"
                        activityIcon="none"
                        testId="security-deepsec-apply-foundation-processing"
                      />
                    ) : null}
                  </div>
                </WorkSection>
              ) : null}
              {mayApply && resetAvailable ? (
                <WorkSection
                  title={t("security.deepsec.resetSectionTitle")}
                  description={t("security.deepsec.resetSectionDescription")}
                  tone="danger"
                  open={resetPanelOpen}
                  dataTestId="security-deepsec-reset-section"
                  onOpenChange={(open) => {
                    setResetPanelOpen(open);
                    if (open) setActionError("");
                    if (!open) {
                      setResetConfirmation("");
                      setResetError("");
                    }
                  }}
                >
                  <div className="grid gap-3">
                    <Banner severity="danger">{t("security.deepsec.resetWarning")}</Banner>
                    {resetError ? <FormStatus tone="danger" message={resetError} /> : null}
                    <ExecutionConfirmationField
                      value={resetConfirmation}
                      onChange={(value) => {
                        setResetConfirmation(value);
                        setResetError("");
                      }}
                      confirmed={resetConfirmed}
                      placeholder={ADMIN_RESET_CONFIRMATION}
                      expectedLabel={ADMIN_RESET_CONFIRMATION}
                      helper={t("security.deepsec.resetHelper", {
                        phrase: ADMIN_RESET_CONFIRMATION,
                      })}
                      disabled={resetting}
                      actions={
                        <>
                          <Button
                            type="button"
                            variant="danger"
                            size="lg"
                            className="w-full sm:w-auto"
                            loading={resetting}
                            disabled={!resetConfirmed || actionBlocked}
                            onClick={() => void handleReset()} icon={Trash2}>
                            {t("security.deepsec.reset")}
                          </Button>
                          <Button
                            type="button"
                            variant="secondary"
                            size="lg"
                            className="w-full sm:w-auto"
                            disabled={resetting}
                            onClick={() => {
                              setResetPanelOpen(false);
                              setResetConfirmation("");
                              setResetError("");
                            }}
                          >
                            {t("common.cancel")}
                          </Button>
                        </>
                      }
                    />
                    {resetting ? (
                      <ProcessingIndicator
                        active
                        label={t("security.deepsec.progress.resetting")}
                        operationKey="security-deepsec-reset"
                        placement="action"
                        activityIcon="none"
                        testId="security-deepsec-reset-processing"
                      />
                    ) : null}
                  </div>
                </WorkSection>
              ) : null}
            </section>
          </ManagementPanelShell>
        ) : null}

        {activeView === "data-permissions" ? (
          <ManagementPanelShell
            id="security-deepsec-panel-data-permissions"
            labelledBy="security-deepsec-tab-data-permissions"
            ariaLabel={t("security.deepsec.tabs.dataPermissions")}
          >
            <ManagementPanelHeader
              title={t("security.deepsec.tabs.dataPermissions")}
              description={t("security.deepsec.tabs.dataPermissionsHint")}
              icon={ShieldCheck}
            />
            {actionError ? <FormStatus tone="danger" message={actionError} /> : null}
            {plan && !foundationReady ? (
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0 flex-1">
                  <Banner severity="info">{t("security.deepsec.prerequisite.foundation")}</Banner>
                </div>
                <Button type="button" variant="secondary" size="sm" onClick={() => setActiveView("foundation")}>
                  {t("security.deepsec.actions.openFoundation")}
                </Button>
              </div>
            ) : null}

            <Card>
              <CardHeader>
                <CardTitle className="flex min-w-0 items-center gap-2">
                  <Database size={20} aria-hidden />
                  <span className="min-w-0 break-words">{t("security.deepsec.entitlements.title")}</span>
                </CardTitle>
                <p className="text-sm leading-6 text-fg-muted">{t("security.deepsec.entitlements.hint")}</p>
              </CardHeader>
              <CardContent className="space-y-4">
                {entitlementLoadError ? <Banner severity="danger">{entitlementLoadError}</Banner> : null}
                {!entitlementLoading && selectedRoleMissing ? (
                  <Banner severity="warning">{t("security.deepsec.entitlements.selectionMissing")}</Banner>
                ) : null}
                {draftVersionChanged ? (
                  <Banner severity="warning">
                    <p>{t("security.deepsec.entitlements.versionChanged")}</p>
                    <Button type="button" variant="secondary" size="sm" disabled={actionBlocked} onClick={() => void restartEntitlementDraft()}>
                      {t("security.deepsec.entitlements.restartDraft")}
                    </Button>
                  </Banner>
                ) : null}
                <div className="grid gap-4 xl:grid-cols-[minmax(15rem,22rem)_minmax(0,1fr)]">
                  <section className="grid min-w-0 content-start gap-3" aria-labelledby="deepsec-entitlement-role-list-title">
                    <h3 id="deepsec-entitlement-role-list-title" className="text-sm font-semibold">
                      {t("security.deepsec.entitlements.roles")}
                    </h3>
                    {/* 一覧のツールバー: 検索欄と件数（ListCount 1 か所。絞り込み中は「N 件（全 M 件）」。#1891）。
                        狭い列（22rem まで）なので、部品が検索欄を全幅にして件数を下に積む。 */}
                    <ListToolbar
                      testId="security-deepsec-entitlement-toolbar"
                      search={
                        // ツールバーの中では欄の上にラベルを置かない（読み上げ名だけ。#1933）。
                        <DbManagementSearchField
                          label={t("security.common.search")}
                          placeholder={t("security.deepsec.entitlements.searchPlaceholder")}
                          value={entitlementSearch}
                          testId="security-deepsec-entitlement-search"
                          onChange={setEntitlementSearch}
                        />
                      }
                      count={
                        entitlementLoading && entitlementRoles.length === 0
                          ? undefined
                          : {
                              total: entitlementRoles.length,
                              matched: entitlementSearch.trim() ? filteredEntitlementRoles.length : undefined,
                              labels: listCountLabels(),
                            }
                      }
                    />
                    {entitlementLoading ? (
                      <ProcessingIndicator
                        active
                        label={t("security.deepsec.entitlements.loading")}
                        operationKey="security-deepsec-entitlements"
                        placement="panel"
                        testId="security-deepsec-entitlements-loading"
                        // ヘッダーの「最新情報を取得」が回っていない読込（操作の後の読み直しなど）はここが出す（#819）。
                        activityIcon={headerRefresh.pending ? "none" : "spinner"}
                      />
                    ) : null}
                    <div
                      className="grid max-h-[17.5rem] gap-1.5 overflow-auto pr-1"
                      data-testid="security-deepsec-entitlement-roles"
                    >
                      {!entitlementLoading && filteredEntitlementRoles.length === 0 ? (
                        <p className="rounded-md border border-dashed border-border p-3 text-sm text-fg-muted">
                          {entitlementSearch ? t("security.deepsec.entitlements.noResults") : t("security.common.empty")}
                        </p>
                      ) : null}
                      {filteredEntitlementRoles.map((role) => {
                        const selected = role.role_id === visibleSelectedEntitlementRoleId;
                        const statusBadge = entitlementRoleStatus(role);
                        return (
                          <button
                            key={role.role_id}
                            data-surface-tint={selected ? "accent" : undefined}
                            type="button"
                            className={cn(
                              "min-w-0 rounded-md border border-border bg-surface-sunken p-2.5 text-left transition hover:border-accent-emphasis",
                              selected && "border-accent-emphasis bg-accent-subtle"
                            )}
                            aria-pressed={selected}
                            data-testid={`security-deepsec-entitlement-role-${role.role_id}`}
                            onClick={() => void selectEntitlementRole(role.role_id)}
                          >
                            <span className="flex min-w-0 flex-wrap items-start justify-between gap-2">
                              <span className="min-w-0">
                                {/* ロール名だけを出す（コード・ID は出さない。#1682） */}
                                <span className="block break-words text-sm font-medium">{roleDisplayName(role)}</span>
                              </span>
                              <StatusBadge variant={statusBadge.variant} label={statusBadge.label} />
                            </span>
                            <span className="mt-1.5 block text-xs text-fg-muted">
                              {t("security.deepsec.entitlements.count", { count: role.data_entitlements.length })}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </section>

                  <section className="min-w-0 rounded-md border border-border bg-surface-sunken p-4" aria-labelledby="deepsec-entitlement-editor-title">
                    {!selectedEntitlementRole ? (
                      <div className="py-10 text-center">
                        <h3 id="deepsec-entitlement-editor-title" className="text-sm font-semibold">
                          {t("security.deepsec.entitlements.noSelectionTitle")}
                        </h3>
                        <p className="mt-1 text-sm text-fg-muted">{t("security.deepsec.entitlements.noSelectionHint")}</p>
                      </div>
                    ) : (
                      <div
                        className="grid min-w-0 gap-3"
                        data-testid="security-deepsec-entitlement-form"
                        data-selected-entitlement-rule={selectedEntitlementDraft?.client_key}
                      >
                        <div className="grid min-w-0 gap-3 border-b border-border pb-3">
                          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                            <div className="min-w-0">
                              <h3 id="deepsec-entitlement-editor-title" className="break-words text-base font-semibold">
                                {roleDisplayName(selectedEntitlementRole)}
                              </h3>
                            </div>
                            <div className="flex flex-wrap gap-2">
                              <StatusBadge
                                variant={entitlementRoleStatus(selectedEntitlementRole).variant}
                                label={entitlementRoleStatus(selectedEntitlementRole).label}
                              />
                              <StatusBadge
                                icon={false}
                                variant="info"
                                label={t("security.deepsec.entitlements.count", {
                                  count: entitlementDraftRows.length,
                                })}
                              />
                            </div>
                          </div>
                          {selectedEntitlementRole.is_built_in ? (
                            <Banner severity="info">{t("security.deepsec.entitlements.readOnlyBuiltIn")}</Banner>
                          ) : null}
                          {selectedEntitlementRole.archived ? (
                            <Banner severity="info">{t("security.deepsec.entitlements.readOnlyArchived")}</Banner>
                          ) : null}
                          {entitlementDraftChanged ? (
                            <Banner severity="info">{t("security.deepsec.entitlements.unsavedPreview")}</Banner>
                          ) : null}
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <h4 className="text-sm font-semibold">{t("security.deepsec.entitlements.rows")}</h4>
                            <div className="flex flex-wrap gap-2">
                              {selectedEntitlementDraft ? (
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="ghost"
                                  aria-label={t("security.deepsec.entitlements.remove")}
                                  onClick={() => removeEntitlement(selectedEntitlementDraftIndex)}
                                  disabled={entitlementReadOnly || selectedEntitlementDraftIndex < 0} icon={Trash2}>
                                  {t("security.deepsec.entitlements.removeButtonLabel")}
                                </Button>
                              ) : null}
                              <Button
                                type="button"
                                size="sm"
                                variant="secondary"
                                onClick={addEntitlement}
                                disabled={entitlementReadOnly} icon={Plus}>
                                {t("security.deepsec.entitlements.add")}
                              </Button>
                            </div>
                          </div>
                        </div>
                        <div
                          className="h-[36rem] min-h-0 md:h-[32rem]"
                          data-testid="security-deepsec-entitlement-workspace-frame"
                        >
                          {entitlementDraftRows.length === 0 ? (
                            <div
                              className="grid h-full content-center rounded-md border border-dashed border-border p-4 text-center"
                              data-testid="security-deepsec-entitlement-rules-empty"
                            >
                              <h4 className="text-sm font-semibold">
                                {t("security.deepsec.entitlements.emptyRulesTitle")}
                              </h4>
                              <p className="mt-1 text-sm text-fg-muted">
                                {t("security.deepsec.entitlements.emptyRulesHint")}
                              </p>
                            </div>
                          ) : (
                            <div
                              className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-3 overflow-hidden 2xl:grid-cols-[minmax(12rem,18rem)_minmax(0,1fr)] 2xl:grid-rows-1"
                              data-testid="security-deepsec-entitlement-workspace"
                            >
                            <div
                              className="grid max-h-36 min-h-0 content-start gap-2 overflow-y-auto pr-1 2xl:max-h-none"
                              aria-label={t("security.deepsec.entitlements.ruleList")}
                              data-testid="security-deepsec-entitlement-rules-list"
                            >
                              {entitlementDraftRows.map((entitlement, index) => {
                                const targetKey = entitlementTargetKey(entitlement);
                                const detail = targetDetails[targetKey];
                                const statusBadge = entitlementApplyStatus(entitlement);
                                const ruleTitle =
                                  targetKey || t("security.deepsec.entitlements.ruleTitle");
                                const selected = entitlement.client_key === visibleSelectedEntitlementDraftKey;
                                return (
                                  <button
                                    key={entitlement.client_key}
                                    data-surface-tint={selected ? "accent" : undefined}
                                    type="button"
                                    className={cn(
                                      "min-w-0 rounded-md border border-border bg-surface p-3 text-left transition hover:border-accent-emphasis",
                                      selected && "border-accent-emphasis bg-accent-subtle"
                                    )}
                                    aria-pressed={selected}
                                    data-testid={`security-deepsec-entitlement-rule-tab-${index}`}
                                    onClick={() => {
                                      setSelectedEntitlementDraftKey(entitlement.client_key);
                                      setEntitlementFormError("");
                                    }}
                                  >
                                    <span className="flex min-w-0 flex-wrap items-start justify-between gap-2">
                                      <span className="min-w-0">
                                        {targetKey ? (
                                          <DbObjectName value={targetKey} size="sm" interactive className="block" />
                                        ) : (
                                          <span className="block break-all text-sm font-semibold">{ruleTitle}</span>
                                        )}
                                        <span className="mt-1 block break-all font-mono text-xs text-fg-muted">
                                          {entitlement.data_grant_name ||
                                            t("security.deepsec.entitlements.notGenerated")}
                                        </span>
                                      </span>
                                      <StatusBadge variant={statusBadge.variant} label={statusBadge.label} />
                                    </span>
                                    <span className="mt-2 flex min-w-0 flex-wrap gap-2 text-xs text-fg-muted">
                                      <span>{entitlementColumnsSummary(entitlement, detail)}</span>
                                      <span aria-hidden="true">/</span>
                                      <span>{entitlementScopeSummary(entitlement)}</span>
                                    </span>
                                  </button>
                                );
                              })}
                            </div>
                            <div
                              className="min-h-0 overflow-y-auto pr-1"
                              ref={entitlementEditorScrollRef}
                              data-testid={
                                selectedEntitlementDraft
                                  ? `security-deepsec-entitlement-rule-${selectedEntitlementDraftIndex}`
                                  : "security-deepsec-entitlement-editor-empty"
                              }
                            >
                              {selectedEntitlementDraft ? (
                                (() => {
                                  const entitlement = selectedEntitlementDraft;
                                  const index = selectedEntitlementDraftIndex;
                                  const targetKey = entitlementTargetKey(entitlement);
                                  const detail = targetDetails[targetKey];
                                  const loadingDetail = Boolean(targetDetailLoading[targetKey]);
                                  const detailError = targetDetailErrors[targetKey] ?? "";
                                  const selectedColumns = new Set(
                                    (entitlement.column_names ?? []).map(normalizeDbIdentifierToken)
                                  );
                                  const availableColumnNames =
                                    detail?.columns.map((column) => normalizeDbIdentifierToken(column.column_name)) ?? [];
                                  const selectedAvailableColumnCount = availableColumnNames.filter(
                                    (columnName) => selectedColumns.has(columnName)
                                  ).length;
                                  const statusBadge = entitlementApplyStatus(entitlement);
                                  const supportedScopeColumns =
                                    detail?.columns.filter(isSupportedScopeColumn) ?? [];
                                  const ruleTitle =
                                    targetKey || t("security.deepsec.entitlements.ruleTitle");
                                return (
                                  <section
                                    className="min-w-0 rounded-md border border-border bg-surface-sunken"
                                  >
                                    <div className="flex min-h-12 items-start justify-between gap-3 border-b border-border px-3 py-3">
                                      <div className="min-w-0">
                                        <p
                                          className="break-all text-sm font-semibold"
                                          data-testid={`security-deepsec-entitlement-editor-title-${index}`}
                                        >
                                          {targetKey ? <DbObjectName value={targetKey} size="sm" /> : ruleTitle}
                                        </p>
                                        <p className="mt-1 break-all font-mono text-xs text-fg-muted">
                                          {entitlement.data_grant_name ||
                                            t("security.deepsec.entitlements.notGenerated")}
                                        </p>
                                        <p className="mt-1 text-xs text-fg-muted">
                                          {entitlementColumnsSummary(entitlement, detail)}
                                          {" / "}
                                          {entitlementScopeSummary(entitlement)}
                                        </p>
                                      </div>
                                      <div className="flex shrink-0 items-center gap-2">
                                        <StatusBadge variant={statusBadge.variant} label={statusBadge.label} />
                                      </div>
                                    </div>
                                    <div className="grid gap-3 p-3">
                                      {entitlement.apply_error_message ? (
                                        <FormStatus tone="danger" message={entitlement.apply_error_message} />
                                      ) : null}
                                      <DeepSecTargetObjectPicker
                                        index={index}
                                        value={targetKey}
                                        selectedObject={targetObjectMap.get(targetKey) ?? null}
                                        objects={visibleTargetObjects}
                                        total={targetObjectTotal}
                                        hasMore={Boolean(targetObjectNextCursor)}
                                        loading={targetObjectsLoading}
                                        loadingMore={targetObjectsLoadingMore}
                                        error={targetObjectLoadError}
                                        disabled={entitlementReadOnly}
                                        onSearchChange={setTargetObjectSearch}
                                        onSelect={(value) => updateEntitlementTarget(index, value)}
                                        onLoadMore={() =>
                                          void loadTargetObjects({
                                            cursor: targetObjectNextCursor,
                                            append: true,
                                          })
                                        }
                                        onRetry={() =>
                                          void loadTargetObjects(
                                            targetObjectNextCursor && targetObjects.length > 0
                                              ? { cursor: targetObjectNextCursor, append: true }
                                              : {}
                                          )
                                        }
                                      />
                                      <fieldset className="grid gap-2">
                                        <FieldLegend className="text-xs font-medium" required>
                                          {t("security.deepsec.entitlements.columns")}
                                        </FieldLegend>
                                        <div className="mt-1 flex min-w-0 justify-start">
                                          <BulkSelectionActions
                                            selectLabel={t("common.selection.selectAll")}
                                            clearLabel={t("common.selection.clearAll")}
                                            selectDisabled={
                                              entitlementReadOnly || actionBlocked ||
                                              loadingDetail ||
                                              Boolean(detailError) ||
                                              availableColumnNames.length === 0 ||
                                              selectedAvailableColumnCount === availableColumnNames.length
                                            }
                                            clearDisabled={
                                              entitlementReadOnly || actionBlocked ||
                                              loadingDetail ||
                                              Boolean(detailError) ||
                                              availableColumnNames.length === 0 ||
                                              selectedAvailableColumnCount === 0
                                            }
                                            dataTestId={`security-deepsec-entitlement-column-selection-actions-${index}`}
                                            onSelectAll={() =>
                                              setEntitlementColumnsPreservingScroll(
                                                index,
                                                availableColumnNames
                                              )
                                            }
                                            onClearAll={() =>
                                              setEntitlementColumnsPreservingScroll(index, [])
                                            }
                                          />
                                        </div>
                                        {loadingDetail ? (
                                          // 文字だけにせず、経過時間と列の候補の形の Skeleton を出す（#265）。
                                          <TimedLoadingState
                                            label={t("security.deepsec.entitlements.columnsLoading")}
                                            testId={`security-deepsec-columns-loading-${index}`}
                                          >
                                            <ListSkeleton rows={3} rowClassName="h-8" />
                                          </TimedLoadingState>
                                        ) : detailError ? (
                                          <p data-surface-tint="danger" className="rounded-md border border-danger-border bg-danger-subtle p-3 text-sm text-danger-fg">
                                            {detailError}
                                          </p>
                                        ) : detail?.columns.length ? (
                                          <div
                                            className="mt-1 grid max-h-48 gap-2 overflow-auto rounded-md border border-border bg-surface-sunken p-2 sm:grid-cols-2 xl:grid-cols-3"
                                            data-entitlement-scroll-container
                                            data-testid={`security-deepsec-entitlement-columns-grid-${index}`}
                                          >
                                            {detail.columns.map((column) => (
                                              <label
                                                key={column.column_name}
                                                className="flex min-h-11 min-w-0 items-start gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-surface-hover"
                                              >
                                                <input
                                                  type="checkbox"
                                                  className="mt-1 h-4 w-4 shrink-0 accent-accent-emphasis"
                                                  disabled={entitlementReadOnly}
                                                  checked={selectedColumns.has(
                                                    normalizeDbIdentifierToken(column.column_name)
                                                  )}
                                                  onChange={(event) =>
                                                    toggleEntitlementColumn(
                                                      index,
                                                      column.column_name,
                                                      event.target.checked
                                                    )
                                                  }
                                                />
                                                <span className="min-w-0">
                                                  <span className="block break-all font-mono text-xs">
                                                    {column.column_name}
                                                  </span>
                                                  <span className="block text-xs text-fg-muted">
                                                    {column.data_type}
                                                  </span>
                                                </span>
                                              </label>
                                            ))}
                                          </div>
                                        ) : (
                                          <p className="rounded-md border border-dashed border-border p-3 text-sm text-fg-muted">
                                            {t("security.deepsec.entitlements.selectObjectForColumns")}
                                          </p>
                                        )}
                                      </fieldset>
                                    <SelectField
                                      id={`deepsec-entitlement-scope-mode-${index}`}
                                      label={t("security.deepsec.entitlements.scopeMode")}
                                      required
                                      width="sm"
                                      className="min-w-0"
                                      disabled={entitlementReadOnly}
                                      value={entitlement.scope_mode === "EXPRESSION" ? "FILTERS" : entitlement.scope_mode ?? "ALL"}
                                      options={[
                                        { value: "ALL", label: t("security.deepsec.entitlements.scopeAll") },
                                        { value: "FILTERS", label: t("security.deepsec.entitlements.scopeFilters") },
                                      ]}
                                      onValueChange={(nextMode) => {
                                          patchEntitlement(index, {
                                            scope_mode: nextMode === "ALL" ? "ALL" : "EXPRESSION",
                                            ...(entitlement.scope_mode === "EXPRESSION" && nextMode === "ALL" ? { scope_expression_version: 1 } : {}),
                                            scope_expression: nextMode === "ALL" ? null : { version: 1, root: { kind: "group", operator: "AND", children: [{ kind: "condition", filter: blankScopeFilter(supportedScopeColumns) }] } },
                                            scope_code: nextMode === "ALL" ? "*" : nextMode === "FILTERS" ? "FILTERS" : "",
                                            scope_column: "",
                                            scope_filters: [],
                                          });
                                        }}
                                    />
                                    {["FILTERS", "EXPRESSION"].includes(entitlement.scope_mode ?? "") ? (
                                      <ScopeExpressionEditor
                                        expression={entitlementExpression(entitlement)}
                                        columns={supportedScopeColumns}
                                        owner={entitlement.target_owner ?? ""}
                                        objectName={entitlement.target_object ?? ""}
                                        disabled={entitlementReadOnly}
                                        index={index}
                                        onChange={(scope_expression) => patchEntitlement(index, { scope_mode: "EXPRESSION", scope_expression, scope_filters: [], scope_column: "", scope_code: "EXPRESSION" })}
                                      />
                                    ) : null}
                                  </div>
                                  </section>
                                );
                                })()
                              ) : (
                                <div className="grid content-center rounded-md border border-dashed border-border p-4 text-center">
                                  <p className="text-sm text-fg-muted">
                                    {t("security.deepsec.entitlements.noSelectionHint")}
                                  </p>
                                </div>
                              )}
                            </div>
                            </div>
                          )}
                        </div>
                        <div
                          className="grid min-h-0 min-w-0 gap-3 border-t border-border bg-surface pt-3"
                          data-testid="security-deepsec-entitlement-action-region"
                        >
                          {entitlementFormError ? <FormStatus tone="danger" message={entitlementFormError} /> : null}
                          <Disclosure
                            open={entitlementSqlPreviewOpen}
                            onOpenChange={setEntitlementSqlPreviewOpen}
                            surface="sunken"
                            summary={t("security.deepsec.entitlements.sqlPreview")}
                            // 長い SQL は枠の中でスクロールし、見出しは上端に留める。
                            className="max-h-[min(28rem,45dvh)] overflow-y-auto overscroll-contain"
                            summaryClassName="sticky top-0 z-10 bg-surface-sunken"
                            contentClassName="space-y-3"
                            data-testid="security-deepsec-sql-preview"
                          >
                              <div
                                className="grid min-w-0 gap-2"
                                data-testid="security-deepsec-sql-preview-toolbar"
                              >
                                <p className="min-w-0 text-sm leading-6 text-fg-muted">
                                  {t("security.deepsec.entitlements.sqlReadonly")}
                                </p>
                                <div className="flex min-w-0 justify-end">
                                  <Button
                                    type="button"
                                    variant="secondary"
                                    size="sm"
                                    className="w-full min-w-0 justify-center lg:w-auto"
                                    loading={entitlementPreviewing}
                                    disabled={
                                      entitlementReadOnly || actionBlocked || Boolean(validateEntitlements()) ||
                                      entitlementPreviewing ||
                                      entitlementApplying ||
                                      (normalizedEntitlementDraftRows.length === 0 &&
                                        savedEntitlementRows.length === 0)
                                    }
                                    data-testid="security-deepsec-sql-preview-generate"
                                    onClick={() => void handlePreviewEntitlements()} icon={RefreshCw}>
                                    {t("security.deepsec.entitlements.generatePreview")}
                                  </Button>
                                </div>
                              </div>
                              {entitlementPreview ? (
                                <div className="grid min-w-0 gap-4">
                                  <p className="text-sm leading-6 text-fg-muted" role="status">
                                    {t("security.deepsec.entitlements.sqlScopeSummary", {
                                      role: selectedEntitlementRole.display_name,
                                      grantCount: selectedRolePreviewRows.length,
                                      cleanupCount: selectedRoleCleanupSql.length,
                                      sqlCount: selectedRolePreviewSqlCount,
                                    })}
                                  </p>
                                  <div className="space-y-1">
                                    <p className="text-xs font-medium text-fg-muted">
                                      {t("security.deepsec.checksum")}
                                    </p>
                                    <code className="block break-all rounded-md bg-surface p-2 text-xs">
                                      {entitlementPreview.checksum}
                                    </code>
                                  </div>
                                  {selectedRoleCleanupSql.length ? (
                                    <section className="grid min-w-0 gap-2" aria-labelledby="deepsec-cleanup-sql-title">
                                      <h5 id="deepsec-cleanup-sql-title" className="text-sm font-semibold text-danger-fg">
                                        {t("security.deepsec.entitlements.sqlCleanupTitle")}
                                      </h5>
                                      {selectedRoleCleanupSql.map((sql, index) => (
                                        <pre
                                          key={`${selectedEntitlementRole.role_id}-cleanup-${index}`}
                                          data-surface="code"
                    className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border border-danger-border bg-surface p-3 text-fg text-xs leading-5"
                                          tabIndex={0}
                                          aria-label={t("security.deepsec.entitlements.sqlCleanupAria", {
                                            index: index + 1,
                                          })}
                                        >
                                          {sql}
                                        </pre>
                                      ))}
                                    </section>
                                  ) : null}
                                  {selectedRolePreviewRows.length ? (
                                    <section className="grid min-w-0 gap-3" aria-labelledby="deepsec-apply-sql-title">
                                      <h5 id="deepsec-apply-sql-title" className="text-sm font-semibold">
                                        {t("security.deepsec.entitlements.sqlApplyTitle")}
                                      </h5>
                                      {selectedRolePreviewRows.map((item) => {
                                        const target = entitlementTargetKey(item);
                                        const dataGrant =
                                          item.data_grant_name ||
                                          t("security.deepsec.entitlements.notGenerated");
                                        return (
                                          <article
                                            key={item.entitlement_id ?? `${target}-${dataGrant}`}
                                            className="grid min-w-0 gap-2 rounded-md border border-border p-3"
                                          >
                                            <h6 className="min-w-0 break-words text-xs font-semibold">
                                              {t("security.deepsec.entitlements.sqlGrantHeading", {
                                                target,
                                                dataGrant,
                                              })}
                                            </h6>
                                            {(item.sql ?? []).map((sql, index) => (
                                              <pre
                                                key={`${item.entitlement_id ?? dataGrant}-sql-${index}`}
                                                data-surface="code"
                    className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-surface p-3 text-fg text-xs leading-5"
                                                tabIndex={0}
                                                aria-label={t("security.deepsec.entitlements.sqlGrantAria", {
                                                  target,
                                                  index: index + 1,
                                                })}
                                              >
                                                {sql}
                                              </pre>
                                            ))}
                                          </article>
                                        );
                                      })}
                                    </section>
                                  ) : null}
                                  {selectedRolePreviewSqlCount === 0 ? (
                                    <p className="rounded-md border border-dashed border-border p-3 text-sm text-fg-muted">
                                      {t("security.deepsec.entitlements.sqlNoChanges")}
                                    </p>
                                  ) : null}
                                </div>
                              ) : (
                                <p className="rounded-md border border-dashed border-border p-3 text-sm text-fg-muted">
                                  {t("security.deepsec.entitlements.sqlEmpty")}
                                </p>
                              )}
                            </Disclosure>
                          <ExecutionConfirmationField
                            value={entitlementApplyConfirmation}
                            onChange={(value) => {
                              setEntitlementApplyConfirmation(value);
                              setEntitlementFormError("");
                            }}
                            confirmed={entitlementApplyConfirmed}
                            placeholder={ADMIN_EXECUTE_CONFIRMATION}
                            expectedLabel={ADMIN_EXECUTE_CONFIRMATION}
                            helper={t("security.deepsec.entitlements.applyHelper", {
                              phrase: ADMIN_EXECUTE_CONFIRMATION,
                            })}
                            disabled={
                              entitlementReadOnly || actionBlocked ||
                              entitlementPreviewing ||
                              entitlementApplying ||
                              !status?.configured
                            }
                            actions={
                              <Button
                                type="button"
                                variant="danger"
                                size="lg"
                                className="w-full sm:w-auto"
                                loading={entitlementApplying}
                                disabled={
                                  entitlementReadOnly || actionBlocked ||
                                  entitlementPreviewing ||
                                  entitlementApplying ||
                                  !status?.configured ||
                                  !entitlementApplyConfirmed || Boolean(validateEntitlements())
                                }
                                onClick={() => void handleApplyEntitlements()} icon={ShieldCheck}>
                                {t("security.deepsec.entitlements.apply")}
                              </Button>
                            }
                          />
                          {entitlementApplying ? (
                            <ProcessingIndicator
                              active
                              label={t("security.deepsec.progress.applyingEntitlements")}
                              operationKey="security-deepsec-apply-entitlements"
                              placement="action"
                              activityIcon="none"
                              testId="security-deepsec-apply-entitlements-processing"
                            />
                          ) : null}
                        </div>
                      </div>
                    )}
                  </section>
                </div>
              </CardContent>
            </Card>

            {mayVerify ? (
              <Card data-testid="security-deepsec-verification-card">
                <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <CardTitle>{t("security.deepsec.result")}</CardTitle>
                    <p className="mt-1 text-sm leading-6 text-fg-muted">
                      {t("security.deepsec.resultEmpty")}
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-col gap-2 sm:items-end">
                    {verification ? (
                      <StatusBadge
                        variant={verification.passed ? "success" : "warning"}
                        label={verification.passed ? t("security.deepsec.complete") : t("security.deepsec.failed")}
                      />
                    ) : null}
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      className="w-full sm:w-auto"
                      loading={verifying}
                      disabled={!status?.configured || actionBlocked}
                      data-testid="security-deepsec-verify-action"
                      onClick={() => void handleVerify()} icon={ShieldCheck}>
                      {t("security.deepsec.verify")}
                    </Button>
                  </div>
                </CardHeader>
                <CardContent className="space-y-2">
                  {verifying ? (
                    <ProcessingIndicator
                      active
                      label={t("security.deepsec.progress.verifying")}
                      operationKey="security-deepsec-verify"
                      placement="result"
                      activityIcon="none"
                      testId="security-deepsec-verify-processing"
                    />
                  ) : null}
                  {verification ? (
                    <div
                      role="region"
                      aria-label={t("security.deepsec.resultListAriaLabel")}
                      tabIndex={0}
                      className="grid max-h-[23.25rem] gap-2 overflow-y-auto overscroll-contain pr-1"
                      data-testid="security-deepsec-verification-results"
                    >
                      {verification.checks.map((check) => (
                        <div
                          key={check.key}
                          className="flex min-h-[4.25rem] items-start gap-2 rounded-md border border-border p-3 text-sm"
                        >
                          <CheckCircle2 size={16} className={check.passed ? "text-success-fg" : "text-warning-fg"} aria-hidden />
                          <div className="min-w-0">
                            <p className="font-mono text-xs font-medium">{check.key}</p>
                            <p className="mt-1 break-words text-fg-muted">{check.detail}</p>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="rounded-md border border-dashed border-border p-3 text-sm leading-6 text-fg-muted">
                      {t("security.deepsec.resultPending")}
                    </p>
                  )}
                </CardContent>
              </Card>
            ) : null}
          </ManagementPanelShell>
        ) : null}
        </fieldset>
      </PageBody>
    </>
  );
}
