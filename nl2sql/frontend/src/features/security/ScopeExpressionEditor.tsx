import { useWorkspaceActive } from "@/components/WorkspaceState";
import { useDatabaseStatus } from "@/lib/queries";
import {
  FieldError,
  Button,
  ProcessingIndicator,
  SelectField,
  TextField,
  useConfirm,
} from "@production-ready/ui";
import { ErrorState } from "@/components/StateViews";
import { useId, useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { t } from "@/lib/i18n";
import {
  formatEntitlementTargetName,
  splitDbObjectName,
} from "@/features/nl2sql/dbObjectIdentity";
import { securityApi } from "./api";
import { useAuth } from "./AuthProvider";
import {
  blankCondition,
  columnValueType,
  expressionCounts,
  expressionError,
  expressionSummary,
  filterError,
  scopeOperators,
} from "./scope-expression";
import type {
  DataEntitlementScopeFilter,
  DeepSecTargetColumn,
  ScopeExpression,
  ScopeGroup,
  ScopeNode,
  ScopeRelatedExists,
} from "./types";

const text = (key: string) => t(`security.deepsec.entitlements.${key}`);
// 入力欄・選択欄は共有の SelectField / TextField（ラベル・高さ・幅・無効の見た目を共有する。#613 / #631）。
// 読み取り専用のときは、囲む <fieldset disabled> がボタン（SelectField）と入力欄をネイティブに無効にする。
// 欄には min-w-0 を付ける（grid の中で、選択中の長い表示名が欄の最小幅を押し広げて 375px ではみ出さないように）。
/** 追加した条件・グループ・関連カードの最初の選択欄（SelectField のボタン）へフォーカスを移す。 */
const SELECT_FIELD_SELECTOR = 'button[role="combobox"]';
function InlineError({ message, id }: { message: string; id?: string }) {
  const fallbackId = useId();
  return message ? (
    <FieldError id={id ?? fallbackId} message={text(message)} />
  ) : null;
}

function ConditionEditor({
  filter,
  columns,
  onChange,
  id,
}: {
  filter: DataEntitlementScopeFilter;
  columns: DeepSecTargetColumn[];
  onChange: (filter: DataEntitlementScopeFilter) => void;
  id: string;
}) {
  const suffix = id.replace("security-deepsec-scope-filter-", "");
  const patch = (change: Partial<DataEntitlementScopeFilter>) =>
    onChange({ ...filter, ...change });
  const operators = scopeOperators[filter.value_type] ?? [];
  const valueSource = filter.value_source ?? "LITERAL";
  const hasValueSource =
    filter.operator === "EQ" && ["TEXT", "NUMBER"].includes(filter.value_type);
  const error = filterError(filter);
  // 条件の誤りは条件の下の InlineError に 1 つだけ出し、値の入力欄から aria-describedby で指す。
  const inputProps = {
    className: "min-w-0",
    "aria-invalid": Boolean(error),
    "aria-describedby": error ? id + "-error" : undefined,
  };
  return (
    <div className="grid gap-2">
      {/* 条件の欄の並び。幅は値の種類（列名・値・ログインユーザー ID の選択は md、演算子は sm、値リストは lg。#1891）。
          FormGrid（container query）にしない: 入れ子の fieldset の中で値を変えると、Chromium が container の中を
          描き直さず欄の幅・高さが 0 のままになった（e2e の「未選択へ戻す」で確認）。 */}
      <div className="grid min-w-0 gap-2 md:grid-cols-2">
        <SelectField
          id={`deepsec-scope-filter-column-${suffix}`}
          label={text("scopeFilterColumn")}
          width="md"
          className="min-w-0"
          value={filter.column_name}
          placeholder={text("scopeFilterColumnPlaceholder")}
          // 任意の欄。誤って選んだ列を未選択に戻せるよう、一覧の先頭に「未選択」を出す（#647）。
          emptyOptionLabel={text("expression.unselected")}
          options={columns
            .filter((c) => columnValueType(c.data_type))
            .map((c) => ({
              value: c.column_name,
              label: `${c.column_name} · ${c.data_type}`,
            }))}
          onValueChange={(columnName) => {
            const column = columns.find((c) => c.column_name === columnName);
            onChange({
              column_name: columnName,
              operator: "EQ",
              value_type: columnValueType(column?.data_type ?? "") ?? "TEXT",
              value_source: "LITERAL",
              value: "",
              value_to: "",
              values: [],
            });
          }}
        />
        <SelectField
          id={`deepsec-scope-filter-operator-${suffix}`}
          label={text("scopeFilterOperator")}
          width="sm"
          className="min-w-0"
          value={filter.operator}
          options={operators.map((op) => ({
            value: op,
            label: text("operator." + op),
          }))}
          onValueChange={(operator) =>
            patch({
              operator,
              value_source: "LITERAL",
              value: "",
              value_to: "",
              values: [],
            })
          }
        />
        {hasValueSource && (
          <SelectField
            id={`deepsec-scope-filter-value-source-${suffix}`}
            label={text("scopeFilterValueSource")}
            width="md"
            className="min-w-0"
            value={valueSource}
            options={[
              { value: "LITERAL", label: text("scopeFilterValueLiteral") },
              {
                value: "LOGIN_USER_ID",
                label: text("scopeFilterValueLoginUserId"),
              },
            ]}
            onValueChange={(source) =>
              patch({
                value_source: source,
                value: "",
                value_to: "",
                values: [],
              })
            }
          />
        )}
        {!["IS_NULL", "IS_NOT_NULL"].includes(filter.operator) &&
          valueSource !== "LOGIN_USER_ID" && (
            <>
              {filter.operator === "IN" ? (
                <TextField
                  id={`deepsec-scope-filter-values-${suffix}`}
                  label={text("scopeFilterValues")}
                  width="lg"
                  {...inputProps}
                  value={(filter.values ?? []).join(",")}
                  onValueChange={(value) => patch({ values: value.split(",") })}
                />
              ) : (
                <TextField
                  id={`deepsec-scope-filter-value-${suffix}`}
                  label={text("scopeFilterValue")}
                  width="md"
                  {...inputProps}
                  inputMode={
                    filter.value_type === "NUMBER"
                      ? filter.operator === "EQ"
                        ? "numeric"
                        : "decimal"
                      : "text"
                  }
                  value={filter.value ?? ""}
                  onValueChange={(value) => patch({ value })}
                />
              )}
              {filter.operator === "BETWEEN" && (
                <TextField
                  id={`deepsec-scope-filter-value-to-${suffix}`}
                  label={text("scopeFilterValueTo")}
                  width="md"
                  {...inputProps}
                  value={filter.value_to ?? ""}
                  onValueChange={(value) => patch({ value_to: value })}
                />
              )}
            </>
          )}
      </div>
      {valueSource === "LOGIN_USER_ID" && (
        <p
          className="text-sm text-fg-muted"
          data-testid={`security-deepsec-scope-filter-login-user-id-${suffix}`}
        >
          {text("scopeFilterValueLoginUserId")}
        </p>
      )}
      <InlineError id={id + "-error"} message={error} />
    </div>
  );
}

type GroupProps = {
  group: ScopeGroup;
  columns: DeepSecTargetColumn[];
  onChange: (group: ScopeGroup) => void;
  owner: string;
  objectName: string;
  depth: number;
  inRelation: boolean;
  budget: { conditions: number; related: number };
  path: string;
};
function GroupEditor({
  group,
  columns,
  onChange,
  owner,
  objectName,
  depth,
  inRelation,
  budget,
  path,
}: GroupProps) {
  const container = useRef<HTMLFieldSetElement>(null);
  const confirm = useConfirm();
  const changeChildren = (children: ScopeNode[], focusLast = false) => {
    onChange({ ...group, children });
    requestAnimationFrame(() => {
      if (focusLast) {
        const children = container.current?.querySelectorAll<HTMLElement>(":scope > [data-scope-child]");
        children?.item(children.length - 1)?.querySelector<HTMLElement>(SELECT_FIELD_SELECTOR)?.focus();
      } else
        container.current?.querySelector<HTMLElement>(SELECT_FIELD_SELECTOR)?.focus();
    });
  };
  return (
    <fieldset
      ref={container}
      className="grid min-w-0 gap-3 rounded-md border border-border bg-surface p-1 sm:p-3"
      data-testid={`scope-group-${path}`}
    >
      <legend className="max-w-full break-words px-1 text-sm font-medium">
        {text("expression.group")}
      </legend>
      <SelectField<"AND" | "OR">
        id={`deepsec-scope-group-match-${path}`}
        label={text("expression.match")}
        width="md"
        className="min-w-0"
        value={group.operator}
        options={[
          { value: "AND", label: text("expression.and") },
          { value: "OR", label: text("expression.or") },
        ]}
        onValueChange={(operator) => onChange({ ...group, operator })}
      />
      {!group.children.length && <InlineError message="expression.empty" />}
      {group.children.map((node, index) => (
        <div
          key={index}
          data-testid={
            node.kind === "condition"
              ? `security-deepsec-scope-filter-${path}-${index}`
              : undefined
          }
          data-scope-child
          className="grid min-w-0 gap-2 border-l-2 border-border pl-0 sm:pl-2"
        >
          {node.kind === "condition" ? (
            <ConditionEditor
              id={`security-deepsec-scope-filter-${path}-${index}`}
              filter={node.filter}
              columns={columns}
              onChange={(filter) =>
                onChange({
                  ...group,
                  children: group.children.map((n, i) =>
                    i === index ? { kind: "condition", filter } : n,
                  ),
                })
              }
            />
          ) : node.kind === "group" ? (
            <GroupEditor
              group={node}
              columns={columns}
              owner={owner}
              objectName={objectName}
              depth={depth + 1}
              inRelation={inRelation}
              budget={budget}
              path={`${path}-${index}`}
              onChange={(next) =>
                onChange({
                  ...group,
                  children: group.children.map((n, i) =>
                    i === index ? next : n,
                  ),
                })
              }
            />
          ) : (
            <RelatedEditor
              depth={depth}
              node={node}
              columns={columns}
              owner={owner}
              objectName={objectName}
              budget={budget}
              path={`${path}-${index}`}
              onChange={(next) =>
                onChange({
                  ...group,
                  children: group.children.map((n, i) =>
                    i === index ? next : n,
                  ),
                })
              }
            />
          )}
          <Button
            iconOnly
            size="sm"
            variant="ghost"
            type="button"
            className="justify-self-end"
            aria-label={text(
              node.kind === "condition"
                ? "scopeFilterRemove"
                : "expression.removeGroup",
            )}
            onClick={async () => {
              if (
                node.kind !== "condition" &&
                !(await confirm({
                  title: text("expression.removeGroup"),
                  description: text("expression.removeConfirm"),
                  confirmLabel: text("expression.removeGroup"),
                  tone: "danger",
                }))
              )
                return;
              changeChildren(group.children.filter((_, i) => i !== index));
            }} icon={Trash2}>
            </Button>
        </div>
      ))}
      <div className="flex min-w-0 flex-wrap gap-2">
        <Button
          size="sm"
          variant="secondary"
          type="button"
          disabled={budget.conditions >= 20 || !columns.length}
          onClick={() =>
            changeChildren([...group.children, blankCondition(columns)], true)
          } icon={Plus}>
          {text("scopeFilterAdd")}
        </Button>
        <Button
          size="sm"
          variant="secondary"
          type="button"
          disabled={depth >= 3 || budget.conditions >= 20 || !columns.length}
          onClick={() =>
            changeChildren(
              [
                ...group.children,
                {
                  kind: "group",
                  operator: "AND",
                  children: [blankCondition(columns)],
                },
              ],
              true,
            )
          }
        >
          {text("expression.addGroup")}
        </Button>
        {!inRelation && (
          <Button
            size="sm"
            variant="secondary"
            type="button"
            disabled={
              depth >= 3 || budget.related >= 3 || budget.conditions >= 20
            }
            onClick={() =>
              changeChildren(
                [
                  ...group.children,
                  {
                    kind: "related_exists",
                    profile_id: "",
                    object_scope_version: 1,
                    target_owner: "",
                    target_object: "",
                    target_type: "TABLE",
                    relation_source: "MANUAL",
                    relation_id: "",
                    relation_version: "",
                    join_keys: [{ source_column: "", target_column: "" }],
                    condition: {
                      kind: "group",
                      operator: "AND",
                      children: [blankCondition([])],
                    },
                  },
                ],
                true,
              )
            }
          >
            {text("expression.addRelated")}
          </Button>
        )}
      </div>
    </fieldset>
  );
}

function QueryStatus({
  loading,
  error,
  retry,
}: {
  loading: boolean;
  error: Error | null;
  retry: () => void;
}) {
  // 入力欄は出したまま候補を取得するため、Skeleton ではなく経過時間つきの処理中の表示にする（#265）。
  return loading ? (
    <ProcessingIndicator
      active
      label={text("expression.loading")}
      placement="panel"
      className="rounded-md border border-border bg-surface-sunken px-3 py-2"
      testId="scope-expression-loading"
    />
  ) : error ? (
    <ErrorState message={error.message} onRetry={retry} />
  ) : null;
}
function RelatedEditor({
  depth,
  node,
  columns,
  onChange,
  owner,
  objectName,
  budget,
  path,
}: {
  depth: number;
  node: ScopeRelatedExists;
  columns: DeepSecTargetColumn[];
  onChange: (node: ScopeRelatedExists) => void;
  owner: string;
  objectName: string;
  budget: GroupProps["budget"];
  path: string;
}) {
  const { user } = useAuth();
  const active = useWorkspaceActive();
  const { data: database } = useDatabaseStatus({ enabled: active });
  const cacheScope = ["nl2sql", "deepsec", user?.user_uuid, database?.context_id];
  const profiles = useQuery({
    retry: false,
    queryKey: [...cacheScope, "scope-profiles"],
    enabled: active,
    queryFn: securityApi.deepSecScopeProfiles,
  });
  const catalog = useQuery({
    retry: false,
    queryKey: [
      ...cacheScope,
      "relations",
      node.profile_id,
      owner,
      objectName,
    ],
    queryFn: () =>
      securityApi.deepSecRelations(node.profile_id, owner, objectName),
    enabled: active && Boolean(node.profile_id),
  });
  const detail = useQuery({
    retry: false,
    queryKey: [
      ...cacheScope,
      "relation-columns",
      node.target_owner,
      node.target_object,
    ],
    queryFn: () =>
      securityApi.deepSecTargetObjectDetail({
        owner: node.target_owner,
        name: node.target_object,
        object_type: "",
        comment: "",
      }),
    enabled: active && Boolean(node.target_object),
  });
  const patch = (change: Partial<ScopeRelatedExists>) =>
    onChange({ ...node, ...change });
  useEffect(() => {
    const type = detail.data?.object_type;
    if (active && (type === "TABLE" || type === "VIEW" || type === "MATERIALIZED VIEW") && type !== node.target_type) {
      onChange({ ...node, target_type: type });
    }
  }, [active, detail.data, node, onChange]);
  // 関連表のキーは DeepSec の保存キーと同じ canonical `OWNER.OBJECT`（引用が必要な部分だけ "..."）。
  // backend の relation catalog / Profile の object もこの形で返る。
  const sourceTarget = formatEntitlementTargetName({
    target_owner: owner,
    target_object: objectName,
  });
  const eligible =
    profiles.data?.filter((p) =>
      p.objects.some(
        (object) => formatEntitlementTargetName({ resource_code: object }) === sourceTarget,
      ),
    ) ?? [];
  const relatedColumns = detail.data?.columns ?? [];
  const selectedTarget = node.target_object
    ? formatEntitlementTargetName({
        target_owner: node.target_owner,
        target_object: node.target_object,
      })
    : "";
  const stale = Boolean(
    catalog.data &&
      (catalog.data.object_scope_version !== node.object_scope_version ||
        (selectedTarget && !catalog.data.objects.includes(selectedTarget))),
  );
  return (
    <fieldset
      className="grid min-w-0 gap-3 rounded-md border border-border p-1 sm:p-3"
      data-testid={`scope-related-${path}`}
    >
      <legend className="max-w-full break-words px-1 text-sm font-medium">
        {text("expression.exists")}
      </legend>
      <p className="text-xs text-fg-muted">{text("expression.sameRecord")}</p>
      <p className="text-xs text-fg-muted">
        {text("expression.protectedRelated")}
      </p>
      <QueryStatus
        loading={profiles.isLoading || catalog.isLoading || detail.isLoading}
        error={profiles.error ?? catalog.error ?? detail.error}
        retry={() => {
          void profiles.refetch();
          if (node.profile_id) void catalog.refetch();
          if (node.target_object) void detail.refetch();
        }}
      />
      <SelectField
        id={`deepsec-scope-related-profile-${path}`}
        label={text("expression.profile")}
        width="md"
        className="min-w-0"
        value={node.profile_id}
        placeholder={text("expression.select")}
        emptyOptionLabel={text("expression.unselected")}
        options={[
          ...eligible.map((p) => ({ value: p.id, label: p.name })),
          // 保存済みの Profile が候補から外れても値を残し、利用できないことを示す。
          ...(node.profile_id && !eligible.some((p) => p.id === node.profile_id)
            ? [{ value: node.profile_id, label: text("expression.unavailable") }]
            : []),
        ]}
        onValueChange={(profileId) =>
            patch({
              profile_id: profileId,
              object_scope_version:
                eligible.find((p) => p.id === profileId)
                  ?.object_scope_version ?? 1,
              target_owner: "",
              target_object: "",
              relation_source: "MANUAL",
              relation_id: "",
              relation_version: "",
              join_keys: [{ source_column: "", target_column: "" }],
              condition: {
                kind: "group",
                operator: "AND",
                children: [blankCondition([])],
              },
            })
          }
      />
      {!profiles.isLoading && !profiles.error && !eligible.length && (
        <p className="text-sm text-fg-muted">{text("expression.noProfiles")}</p>
      )}
      <SelectField
        id={`deepsec-scope-related-table-${path}`}
        label={text("expression.relatedTable")}
        width="lg"
        className="min-w-0"
        disabled={!catalog.data || catalog.isError}
        value={selectedTarget}
        placeholder={text("expression.select")}
        emptyOptionLabel={text("expression.unselected")}
        options={[
          ...(catalog.data?.objects.map((name) => ({ value: name, label: name })) ?? []),
          ...(selectedTarget && !catalog.data?.objects.includes(selectedTarget)
            ? [
                {
                  value: selectedTarget,
                  label: `${selectedTarget} — ${text("expression.unavailable")}`,
                },
              ]
            : []),
        ]}
        onValueChange={(name) => {
            // 引用名は dot を含み得るため、単純な split(".") ではなく引用規則どおりに分ける。
            const target = splitDbObjectName(name);
            patch({
              target_owner: target?.owner ?? "",
              target_object: target?.name ?? "",
              object_scope_version: catalog.data!.object_scope_version,
              relation_source: "MANUAL",
              relation_id: "",
              relation_version: "",
              join_keys: [{ source_column: "", target_column: "" }],
              condition: {
                kind: "group",
                operator: "AND",
                children: [blankCondition([])],
              },
            });
          }}
      />
      {stale && <InlineError message="expression.stale" />}
      {node.target_object && (
        <>
          <SelectField
            id={`deepsec-scope-related-relation-${path}`}
            label={text("expression.relation")}
            width="lg"
            className="min-w-0"
            value={node.relation_id}
            // 「列から手動で設定」は選べる値（空文字）。未選択の placeholder ではない。
            options={[
              { value: "", label: text("expression.manual") },
              ...(catalog.data?.relations
                .filter((r) => r.target === selectedTarget)
                .map((r) => ({ value: r.id, label: `${r.source} · ${r.id}` })) ?? []),
            ]}
            onValueChange={(relationId) => {
                const relation = catalog.data?.relations.find(
                  (r) => r.id === relationId,
                );
                patch(
                  relation
                    ? {
                        relation_source: relation.source,
                        relation_id: relation.id,
                        relation_version: relation.version,
                        join_keys: relation.join_keys,
                      }
                    : {
                        relation_source: "MANUAL",
                        relation_id: "",
                        relation_version: "",
                      },
                );
              }}
          />
          {node.join_keys.map((key, index) => (
            <div
              key={index}
              // 列名の選択欄（md）・「=」・列名の選択欄（md）・削除を左に寄せて並べる（欄をセルいっぱいに伸ばさない。#1891）。
              className="grid min-w-0 gap-2 md:grid-cols-[minmax(0,auto)_auto_minmax(0,auto)_auto] md:items-end md:justify-start"
            >
              <SelectField
                id={`deepsec-scope-related-source-key-${path}-${index}`}
                label={text("expression.sourceKey")}
                width="md"
                className="min-w-0"
                disabled={node.relation_source !== "MANUAL"}
                value={key.source_column}
                placeholder={text("expression.select")}
                emptyOptionLabel={text("expression.unselected")}
                options={columns
                  .filter((c) => columnValueType(c.data_type))
                  .map((c) => ({ value: c.column_name, label: c.column_name }))}
                onValueChange={(column) =>
                  patch({
                    join_keys: node.join_keys.map((k, i) =>
                      i === index ? { ...k, source_column: column } : k,
                    ),
                  })
                }
              />
              <span className="self-center" aria-hidden>
                =
              </span>
              <SelectField
                id={`deepsec-scope-related-target-key-${path}-${index}`}
                label={text("expression.targetKey")}
                width="md"
                className="min-w-0"
                disabled={node.relation_source !== "MANUAL"}
                value={key.target_column}
                placeholder={text("expression.select")}
                emptyOptionLabel={text("expression.unselected")}
                options={relatedColumns
                  .filter((c) => columnValueType(c.data_type))
                  .map((c) => ({ value: c.column_name, label: c.column_name }))}
                onValueChange={(column) =>
                  patch({
                    join_keys: node.join_keys.map((k, i) =>
                      i === index ? { ...k, target_column: column } : k,
                    ),
                  })
                }
              />
              <Button
                iconOnly
                variant="ghost"
                size="sm"
                type="button"
                disabled={
                  node.relation_source !== "MANUAL" ||
                  node.join_keys.length === 1
                }
                aria-label={text("expression.removeKey")}
                onClick={() =>
                  patch({
                    join_keys: node.join_keys.filter((_, i) => i !== index),
                  })
                } icon={Trash2}>
                </Button>
            </div>
          ))}
          <Button
            variant="secondary"
            size="sm"
            type="button"
            disabled={
              node.relation_source !== "MANUAL" || node.join_keys.length >= 8
            }
            onClick={() =>
              patch({
                join_keys: [
                  ...node.join_keys,
                  { source_column: "", target_column: "" },
                ],
              })
            }
          >
            {text("expression.addKey")}
          </Button>
          <GroupEditor
            group={node.condition}
            columns={relatedColumns}
            onChange={(condition) =>
              patch({
                condition,
                target_type:
                  (detail.data
                    ?.object_type as ScopeRelatedExists["target_type"]) ??
                  node.target_type,
              })
            }
            owner={owner}
            objectName={objectName}
            depth={depth + 1}
            inRelation
            budget={budget}
            path={path}
          />
        </>
      )}
    </fieldset>
  );
}

export function ScopeExpressionEditor({
  expression,
  columns,
  onChange,
  owner,
  objectName,
  disabled,
  index,
}: {
  expression: ScopeExpression;
  columns: DeepSecTargetColumn[];
  onChange: (expression: ScopeExpression) => void;
  owner: string;
  objectName: string;
  disabled: boolean;
  index: number;
}) {
  const id = useId();
  return (
    <fieldset
      disabled={disabled}
      className="grid min-w-0 gap-3"
      aria-describedby={id}
      data-testid={`security-deepsec-scope-filters-${index}`}
    >
      <p id={id} className="text-sm text-fg-muted">
        {text("expression.global")}
      </p>
      <GroupEditor
        group={expression.root}
        columns={columns}
        onChange={(root) => onChange({ version: 1, root })}
        owner={owner}
        objectName={objectName}
        depth={1}
        inRelation={false}
        budget={expressionCounts(expression.root)}
        path={String(index)}
      />
      <InlineError message={expressionError(expression)} />
      <p className="break-words text-sm" data-testid="scope-expression-summary">
        {text("expression.summary")}: {expressionSummary(expression.root, text)}
      </p>
      <p className="text-xs text-fg-muted">{text("expression.union")}</p>
    </fieldset>
  );
}
