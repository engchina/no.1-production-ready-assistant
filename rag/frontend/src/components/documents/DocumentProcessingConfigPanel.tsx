"use client";

import { RotateCcw, Save, SlidersHorizontal } from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import {
  Banner,
  Button,
  DisclosureChevron,
  FormStatus,
  SelectField,
  type SelectFieldOption,
  Skeleton,
  TextField,
  ToggleChip,
  TimedLoadingState,
} from "@production-ready/ui";
import { canOpenNavRoute } from "@/components/layout/nav-config";
import { useAuth } from "@/components/security/AuthProvider";
import {
  ApiError,
  type DocumentProcessingConfigData,
  type DocumentProcessingConfig,
} from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useValuesChanged } from "@/lib/render-sync";
import {
  findParserCapability,
  formatSupportedFormats,
  parserSupportsDocument,
} from "@/lib/parser-capabilities";
import {
  useExtractionFieldsSettings,
  useParserAdapterSettings,
  useUpdateDocumentRecipe,
} from "@/lib/queries";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

import {
  RECIPE_CONFIG_FIELDS,
  globalSettingsHref,
  isStructuredRecordsPreprocess,
  preprocessSelectionPatch,
  recipeConfigGroups,
  type RecipeConfigField,
  type RecipeConfigGroup,
  type RecipeConfigItem,
} from "./DocumentProcessingConfigPanel.logic";
import {
  CHUNKING_OPTIONS,
  GRAPH_OPTIONS,
  SECTION_RULES_OPTIONS,
  PARSER_OPTIONS,
  PARSER_VALUES,
  PREPROCESS_OPTIONS,
  boolLabel,
  optionLabel,
  recipeConfigValueLabel,
} from "./DocumentProcessingConfigPanel.values";
import { EntityIndexColumnsRow } from "./EntityIndexColumnsRow";
import { ExcelOptionsRow } from "./ExcelOptionsRow";

function emptyConfig(): DocumentProcessingConfig {
  return {
    preprocess_profile: null,
    excel_options: null,
    parser_adapter_backend: null,
    parser_docling_enabled: null,
    parser_unstructured_enabled: null,
    parser_structured_records_jsonl_enabled: null,
    parser_xml_tree_json_enabled: null,
    vision_enabled: null,
    heading_level_inference_enabled: null,
    chunking_strategy: null,
    chunk_size: null,
    chunk_overlap: null,
    chunk_min_chars: null,
    chunk_delimiter: null,
    graph_profile: null,
    entity_index_enabled: null,
    entity_name_columns: null,
    entity_attribute_columns: null,
    section_rules_mode: null,
    field_extraction_enabled: null,
    navigation_summary_enabled: null,
    auto_parse_after_preprocess_enabled: null,
    auto_chunk_after_extract_enabled: null,
    auto_index_after_chunk_enabled: null,
  };
}

function resolvedConfigs(data: DocumentProcessingConfigData) {
  return { processing: data.processing_config, effective: data.effective_processing_config };
}

/** 上書きの一覧の各行の DOM id の接尾辞（`document-<接尾辞>-<documentId>`）。 */
const EDITOR_ID_SUFFIX: Record<RecipeConfigField, string> = {
  preprocess_profile: "preprocess",
  auto_parse_after_preprocess_enabled: "auto-parse",
  parser_adapter_backend: "parser",
  vision_enabled: "vision",
  heading_level_inference_enabled: "heading-levels",
  field_extraction_enabled: "field",
  navigation_summary_enabled: "navigation",
  auto_chunk_after_extract_enabled: "auto-chunk",
  chunking_strategy: "chunking",
  auto_index_after_chunk_enabled: "auto-index",
  entity_index_enabled: "entity-index",
  graph_profile: "graph",
  section_rules_mode: "section-rules",
};

const CONFIG_GROUPS = recipeConfigGroups();

export function DocumentProcessingConfigPanel({
  documentId,
  recipeId,
  data,
  loading,
  error,
  onRetry,
  disabled,
  sourceModality = null,
}: {
  documentId: string;
  recipeId: string;
  data: DocumentProcessingConfigData | null;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  disabled: boolean;
  sourceModality?: string | null;
}) {
  const saveRecipe = useUpdateDocumentRecipe();
  const { hasPermission } = useAuth();
  const savePending = saveRecipe.isPending;
  const saveError = saveRecipe.error;
  const [expanded, setExpanded] = useState(false);
  const configs = useMemo(() => (data ? resolvedConfigs(data) : null), [data]);
  const [form, setForm] = useState<DocumentProcessingConfig>(emptyConfig);

  // 設定が変わったレンダーで、フォームを保存値に戻す。
  const configsChanged = useValuesChanged([configs]);
  if (configsChanged && configs) setForm(configs.processing);

  const dirty = configs ? JSON.stringify(form) !== JSON.stringify(configs.processing) : false;
  // レシピの未保存の上書き設定があるときだけ離脱を確認する。
  useLeaveGuard(dirty);
  const overrideCount = RECIPE_CONFIG_FIELDS.filter((field) => form[field] != null).length;

  // 項目抽出が実効 ON のときだけ全体の既定の定義を引き、標準の項目で動くことを案内し（#556）、
  // 0 件で保存されていれば無言 no-op を警告する。
  const fieldExtractionEffective =
    form.field_extraction_enabled ?? configs?.effective.field_extraction_enabled ?? false;
  const fieldSchemaQuery = useExtractionFieldsSettings(expanded && fieldExtractionEffective);
  const fieldSchemaEmpty =
    fieldExtractionEffective && fieldSchemaQuery.data?.fields.length === 0;
  const fieldSchemaStandard =
    fieldExtractionEffective && fieldSchemaQuery.data?.uses_standard === true;

  // 対応形式の宣言(capabilities 正本)は編集展開時のみ取得する。
  const adapterSettingsQuery = useParserAdapterSettings(expanded);
  const capabilities = adapterSettingsQuery.data?.capabilities;
  const effectiveParserBackend =
    form.parser_adapter_backend ?? configs?.effective.parser_adapter_backend ?? null;
  const parserRemoved = Boolean(
    effectiveParserBackend &&
      !(PARSER_VALUES as readonly string[]).includes(effectiveParserBackend)
  );
  const effectivePreprocessProfile =
    form.preprocess_profile ?? configs?.effective.preprocess_profile ?? null;
  const parserFormats = formatSupportedFormats(
    findParserCapability(capabilities, effectiveParserBackend)
  );
  const parserSupported = parserSupportsDocument({
    capabilities,
    backend: effectiveParserBackend,
    modality: sourceModality,
    preprocessProfile: effectivePreprocessProfile,
  });
  const parserHint = parserFormats
    ? t("documents.processingConfig.parserFormats", { formats: parserFormats })
    : null;
  const parserWarning =
    parserSupported === false && sourceModality
      ? t("documents.processingConfig.parserUnsupported", {
          modality: t(`sourceProfile.modality.${sourceModality}` as I18nKey),
          formats: parserFormats,
        })
      : null;

  const update = (patch: Partial<DocumentProcessingConfig>) =>
    setForm((current) => ({ ...current, ...patch }));
  // 実体の索引が実効 ON のときだけ「実体の索引の列」を出す（#1388）。
  const entityIndexSelected =
    form.entity_index_enabled ?? configs?.effective.entity_index_enabled ?? false;
  // 前処理が excel_to_jsonl のときだけ「Excel の読み方」を出す（#1221）。
  const excelSelected =
    (form.preprocess_profile ?? configs?.effective.preprocess_profile ?? null) === "excel_to_jsonl";
  // 構造化レコードの JSONL を作るファイル準備を選んでいるとき、解析・分割の組み合わせを案内する（#2457）。
  const structuredRecordsSelected = isStructuredRecordsPreprocess(effectivePreprocessProfile);
  // 分割方式が固定分割のときだけ「固定分割符」を出す（#2457）。
  const fixedDelimiterSelected =
    (form.chunking_strategy ?? configs?.effective.chunking_strategy ?? null) === "fixed_delimiter";

  const renderEditorRow = (item: RecipeConfigItem) => {
    if (!configs) return null;
    const id = `document-${EDITOR_ID_SUFFIX[item.field]}-${documentId}`;
    const label = t(item.label);
    // 「グローバル設定に従う」の値を変える画面（#528）。権限のない画面へのリンクは出さない。
    const globalHref =
      item.globalSettings && canOpenNavRoute(item.globalSettings.route, hasPermission)
        ? globalSettingsHref(item)
        : null;
    switch (item.field) {
      case "preprocess_profile":
        return (
          <SelectRow
            key={item.field}
            field={item.field}
            id={id}
            label={label}
            globalHref={globalHref}
            value={form.preprocess_profile}
            effectiveValue={configs.effective.preprocess_profile}
            options={PREPROCESS_OPTIONS}
            defaultValue="passthrough"
            disabled={disabled}
            onChange={(value) =>
              setForm((current) => ({ ...current, ...preprocessSelectionPatch(current, value) }))
            }
            hint={
              structuredRecordsSelected
                ? t("documents.processingConfig.structuredRecordsHint")
                : null
            }
          />
        );
      case "parser_adapter_backend":
        return (
          <SelectRow
            key={item.field}
            field={item.field}
            id={id}
            label={label}
            globalHref={globalHref}
            value={form.parser_adapter_backend}
            effectiveValue={configs.effective.parser_adapter_backend}
            options={PARSER_OPTIONS}
            defaultValue="docling"
            disabled={disabled}
            onChange={(value) => update({ parser_adapter_backend: value })}
            hint={parserHint}
            warning={parserWarning}
          />
        );
      case "chunking_strategy":
        return (
          <SelectRow
            key={item.field}
            field={item.field}
            id={id}
            label={label}
            globalHref={globalHref}
            value={form.chunking_strategy}
            effectiveValue={configs.effective.chunking_strategy}
            options={CHUNKING_OPTIONS}
            defaultValue="small_to_big"
            disabled={disabled}
            onChange={(value) => update({ chunking_strategy: value })}
          />
        );
      case "graph_profile":
        return (
          <SelectRow
            key={item.field}
            field={item.field}
            id={id}
            label={label}
            globalHref={globalHref}
            value={form.graph_profile}
            effectiveValue={configs.effective.graph_profile}
            options={GRAPH_OPTIONS}
            defaultValue="off"
            disabled={disabled}
            onChange={(value) => update({ graph_profile: value })}
          />
        );
      case "section_rules_mode":
        return (
          <SelectRow
            key={item.field}
            field={item.field}
            id={id}
            label={label}
            globalHref={globalHref}
            value={form.section_rules_mode ?? null}
            effectiveValue={configs.effective.section_rules_mode ?? null}
            options={SECTION_RULES_OPTIONS}
            defaultValue="parser"
            disabled={disabled}
            onChange={(value) => update({ section_rules_mode: value })}
          />
        );
      default: {
        const field = item.field;
        return (
          <BooleanRow
            key={field}
            field={field}
            id={id}
            label={label}
            globalHref={globalHref}
            value={form[field] ?? null}
            effectiveValue={configs.effective[field] ?? null}
            disabled={disabled}
            onChange={(value) => update({ [field]: value })}
            // Vision は解析エンジンに関係なく使える(#497)。全体の既定は env だけで決める。
            hint={
              field === "vision_enabled"
                ? t("knowledgeBases.adapter.field.vision.hint")
                : field === "entity_index_enabled"
                  ? t("knowledgeBases.adapter.field.entityIndex.hint")
                : field === "heading_level_inference_enabled"
                  ? t("knowledgeBases.adapter.field.headingLevels.hint")
                : field === "field_extraction_enabled" && fieldSchemaStandard
                  ? t("documents.processingConfig.fieldSchemaStandard")
                  : null
            }
            warning={
              field === "field_extraction_enabled" && fieldSchemaEmpty
                ? t("documents.processingConfig.fieldSchemaEmpty")
                : null
            }
          />
        );
      }
    }
  };

  const handleSave = () => {
    const options = {
      onSuccess: () => toast.success(t("documents.processingConfig.toast.saved")),
    };
    saveRecipe.mutate({ id: documentId, recipeId, config: form }, options);
  };

  return (
    <section
      aria-label={t("flow.buildConfig.title")}
      className="rounded-md border border-border bg-surface-sunken p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-fg">
            <SlidersHorizontal size={16} className="text-accent-fg" aria-hidden />
            {t("flow.buildConfig.title")}
          </h3>
          <p className="mt-1 text-xs text-fg-muted">{t("documents.processingConfig.subtitle")}</p>
        </div>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          aria-expanded={expanded}
          aria-controls="document-processing-config-editor"
          onClick={() => setExpanded((value) => !value)}
          disabled={loading || Boolean(error) || !data}
          className="shrink-0">
          {t(expanded ? "documents.processingConfig.actions.close" : "documents.processingConfig.actions.edit")}
          {/* 開閉の状態は向きの変わる Chevron で示す（静的な icon={ChevronDown} は閉じる操作でも下向きのままだった。#397）。 */}
          <DisclosureChevron expanded={expanded} size={16} />
        </Button>
      </div>

      {loading ? (
        <TimedLoadingState
          label={t("flow.buildConfig.loading")}
          operationKey="document-build-config-load"
          framed={false}
          className="mt-3"
          testId="document-build-config-loading"
        >
          <Skeleton className="h-20 w-full" />
        </TimedLoadingState>
      ) : error ? (
        <Banner severity="warning" title={t("flow.buildConfig.loadError")} className="mt-3">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <p>{error instanceof ApiError ? error.message : t("flow.buildConfig.loadErrorHint")}</p>
            <Button type="button" variant="secondary" size="sm" onClick={onRetry} icon={RotateCcw}>
              {t("common.retry")}
            </Button>
          </div>
        </Banner>
      ) : configs ? (
        <>
          {/* 要約も上書きの一覧も CONFIG_GROUPS（処理順）だけから作る(#523)。 */}
          <div className="mt-3 space-y-3" data-testid="document-processing-config-summary">
            {CONFIG_GROUPS.map((group) => (
              <PhaseGroup key={group.phase} group={group} idPrefix={`summary-${documentId}`}>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-5">
                  {group.items.map((item) => {
                    const overridden = form[item.field] != null;
                    return (
                      <div
                        key={item.field}
                        data-surface-tint={overridden ? "info" : undefined}
                        data-config-field={item.field}
                        className={cn(
                          "min-w-0 rounded-md border px-2.5 py-2",
                          overridden ? "border-info-border bg-info-subtle" : "border-border bg-surface"
                        )}
                      >
                        <div className="flex min-w-0 items-start justify-between gap-1">
                          <span className="min-w-0 text-xs leading-4 text-fg-muted">{t(item.label)}</span>
                          {overridden ? (
                            <span data-surface-tint="info" className="shrink-0 rounded-sm bg-info-subtle px-1 text-xs font-medium text-info-fg">
                              {t("knowledgeBases.adapter.ribbon.overrideBadge")}
                            </span>
                          ) : null}
                        </div>
                        <span className="mt-0.5 block break-words text-xs font-medium text-fg">
                          {recipeConfigValueLabel(item, configs.effective)}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </PhaseGroup>
            ))}
          </div>

          {expanded ? (
            <div id="document-processing-config-editor" className="mt-4 space-y-4 border-t border-border pt-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-fg-muted">{t("documents.processingConfig.editHint")}</p>
                <span data-surface-tint="neutral" className="rounded-md bg-surface-hover px-2 py-1 text-xs font-medium text-fg-muted">
                  {overrideCount > 0
                    ? t("knowledgeBases.adapter.overrideCount", {
                        count: overrideCount,
                        total: RECIPE_CONFIG_FIELDS.length,
                      })
                    : t("knowledgeBases.adapter.overrideNone")}
                </span>
              </div>

              {disabled ? (
                <FormStatus tone="info" message={t("documents.processingConfig.blocked")} />
              ) : null}
              {parserRemoved && effectiveParserBackend ? (
                <FormStatus
                  tone="danger"
                  message={t("documents.processingConfig.parserRemovedDetail", {
                    engine: effectiveParserBackend,
                  })}
                />
              ) : null}

              <div className="space-y-4" data-testid="document-processing-config-editor-items">
                {CONFIG_GROUPS.map((group) => (
                  <PhaseGroup key={group.phase} group={group} idPrefix={`editor-${documentId}`}>
                    {/* 2 列でも左から右・上から下で処理順に読める(#523)。 */}
                    <div className="grid gap-3 lg:grid-cols-2">
                      {group.items.map((item) => (
                        <Fragment key={item.field}>
                          {renderEditorRow(item)}
                          {item.field === "preprocess_profile" && configs && excelSelected ? (
                            <ExcelOptionsRow
                              key={`excel-options-${recipeId}`}
                              documentId={documentId}
                              value={form.excel_options ?? null}
                              effectiveValue={configs.effective.excel_options ?? null}
                              disabled={disabled}
                              onChange={(value) => update({ excel_options: value })}
                            />
                          ) : null}
                          {item.field === "chunking_strategy" && configs && fixedDelimiterSelected ? (
                            <ChunkDelimiterRow
                              key={`chunk-delimiter-${recipeId}`}
                              id={`document-chunk-delimiter-${documentId}`}
                              value={form.chunk_delimiter ?? null}
                              effectiveValue={configs.effective.chunk_delimiter ?? null}
                              disabled={disabled}
                              onChange={(value) => update({ chunk_delimiter: value })}
                            />
                          ) : null}
                          {item.field === "entity_index_enabled" && configs && entityIndexSelected ? (
                            <EntityIndexColumnsRow
                              key={`entity-columns-${recipeId}`}
                              documentId={documentId}
                              value={{
                                entity_name_columns: form.entity_name_columns ?? null,
                                entity_attribute_columns: form.entity_attribute_columns ?? null,
                              }}
                              effectiveValue={{
                                entity_name_columns: configs.effective.entity_name_columns ?? null,
                                entity_attribute_columns:
                                  configs.effective.entity_attribute_columns ?? null,
                              }}
                              disabled={disabled}
                              onChange={update}
                            />
                          ) : null}
                        </Fragment>
                      ))}
                    </div>
                  </PhaseGroup>
                ))}
              </div>

              {saveError ? (
                <FormStatus
                  tone="danger"
                  message={
                    saveError instanceof ApiError
                      ? saveError.message
                      : t("documents.processingConfig.error.save")
                  }
                />
              ) : null}

              <div className="flex flex-wrap items-center justify-end gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="md"
                  onClick={() => setForm(configs.processing)}
                  disabled={!dirty || savePending || disabled} icon={RotateCcw}>
                  {t("knowledgeBases.adapter.actions.reset")}
                </Button>
                <Button
                  type="button"
                  size="md"
                  onClick={handleSave}
                  loading={savePending}
                  disabled={!dirty || disabled} icon={Save}>
                  {t("knowledgeBases.adapter.actions.save")}
                </Button>
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

/** 工程ごとの区切り。見出しは上のレシピの工程表示と同じ工程名に番号を付ける。 */
function PhaseGroup({
  group,
  idPrefix,
  children,
}: {
  group: RecipeConfigGroup;
  idPrefix: string;
  children: ReactNode;
}) {
  const headingId = `${idPrefix}-phase-${group.phase.toLowerCase()}`;
  return (
    <section aria-labelledby={headingId} data-config-phase={group.phase}>
      <h4 id={headingId} className="mb-1.5 text-xs font-medium text-fg-muted">
        {t("documents.processingConfig.phaseHeading", {
          index: group.index,
          phase: t(group.label),
        })}
      </h4>
      {children}
    </section>
  );
}

function SelectRow<T extends string>({
  field,
  id,
  label,
  globalHref,
  value,
  effectiveValue,
  options,
  defaultValue,
  disabled,
  onChange,
  hint = null,
  warning = null,
}: {
  field: RecipeConfigField;
  id: string;
  label: string;
  globalHref: string | null;
  value: T | null;
  effectiveValue: T | null;
  options: readonly SelectFieldOption<T>[];
  defaultValue: T;
  disabled: boolean;
  onChange: (value: T | null) => void;
  hint?: string | null;
  warning?: string | null;
}) {
  const overriding = value !== null;
  const lastOverride = useRef<T>(value ?? effectiveValue ?? defaultValue);
  useEffect(() => {
    if (value !== null) lastOverride.current = value;
  }, [value]);
  return (
    <div
      data-config-field={field}
      className="space-y-2 rounded-lg border border-border bg-surface p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-medium text-fg">{label}</span>
        <div className="flex flex-wrap gap-1" role="group" aria-label={label}>
          <ToggleChip selected={!overriding} disabled={disabled} onClick={() => onChange(null)}>
            {t("knowledgeBases.adapter.inherit")}
          </ToggleChip>
          <ToggleChip
            selected={overriding}
            disabled={disabled}
            onClick={() => !overriding && onChange(lastOverride.current)}
          >
            {t("knowledgeBases.adapter.override")}
          </ToggleChip>
        </div>
      </div>
      {overriding ? (
        <SelectField
          id={id}
          label={label}
          value={value}
          options={options}
          onValueChange={onChange}
          className="[&>label]:sr-only"
          // 工程の方式・エンジンの選択（一般の選択。#1891）。
          width="md"
        />
      ) : (
        <InheritResolved
          value={optionLabel(effectiveValue, options)}
          label={label}
          globalHref={globalHref}
        />
      )}
      {hint ? <p className="text-xs text-fg-muted">{hint}</p> : null}
      {warning ? <FormStatus tone="warning" className="text-xs" message={warning} /> : null}
    </div>
  );
}

function BooleanRow({
  field,
  id,
  label,
  globalHref,
  value,
  effectiveValue,
  disabled,
  onChange,
  hint = null,
  warning = null,
}: {
  field: RecipeConfigField;
  id: string;
  label: string;
  globalHref: string | null;
  value: boolean | null;
  effectiveValue: boolean | null;
  disabled: boolean;
  onChange: (value: boolean | null) => void;
  hint?: string | null;
  warning?: string | null;
}) {
  const overriding = value !== null;
  const lastOverride = useRef(value ?? effectiveValue ?? true);
  useEffect(() => {
    if (value !== null) lastOverride.current = value;
  }, [value]);
  return (
    <div
      data-config-field={field}
      className="space-y-2 rounded-lg border border-border bg-surface p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span id={id} className="text-sm font-medium text-fg">
          {label}
        </span>
        <div className="flex flex-wrap gap-1" role="group" aria-labelledby={id}>
          <ToggleChip selected={!overriding} disabled={disabled} onClick={() => onChange(null)}>
            {t("knowledgeBases.adapter.inherit")}
          </ToggleChip>
          <ToggleChip
            selected={overriding}
            disabled={disabled}
            onClick={() => !overriding && onChange(lastOverride.current)}
          >
            {t("knowledgeBases.adapter.override")}
          </ToggleChip>
        </div>
      </div>
      {overriding ? (
        <div className="flex flex-wrap gap-1" role="group" aria-labelledby={id}>
          <ToggleChip selected={value === true} disabled={disabled} onClick={() => onChange(true)}>
            {t("knowledgeBases.adapter.bool.enabled")}
          </ToggleChip>
          <ToggleChip selected={value === false} disabled={disabled} onClick={() => onChange(false)}>
            {t("knowledgeBases.adapter.bool.disabled")}
          </ToggleChip>
        </div>
      ) : (
        <InheritResolved value={boolLabel(effectiveValue)} label={label} globalHref={globalHref} />
      )}
      {hint ? <p className="text-xs text-fg-muted">{hint}</p> : null}
      {warning ? <FormStatus tone="warning" className="text-xs" message={warning} /> : null}
    </div>
  );
}

/** 「グローバル設定に従う: 値」と、その全体の既定を変える画面へのリンク（#528）。 */
/** 固定分割の分割符（グローバルを継承 / 上書き）。分割方式が固定分割のときだけ出す（#2457）。 */
function ChunkDelimiterRow({
  id,
  value,
  effectiveValue,
  disabled,
  onChange,
}: {
  id: string;
  value: string | null;
  effectiveValue: string | null;
  disabled: boolean;
  onChange: (value: string | null) => void;
}) {
  const label = t("settings.chunking.params.delimiter");
  const overriding = value !== null;
  const lastOverride = useRef<string>(value ?? effectiveValue ?? "");
  useEffect(() => {
    if (value !== null) lastOverride.current = value;
  }, [value]);
  return (
    <div
      data-config-field="chunk_delimiter"
      className="space-y-2 rounded-lg border border-border bg-surface p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-medium text-fg">{label}</span>
        <div className="flex flex-wrap gap-1" role="group" aria-label={label}>
          <ToggleChip selected={!overriding} disabled={disabled} onClick={() => onChange(null)}>
            {t("knowledgeBases.adapter.inherit")}
          </ToggleChip>
          <ToggleChip
            selected={overriding}
            disabled={disabled}
            onClick={() => !overriding && onChange(lastOverride.current)}
          >
            {t("knowledgeBases.adapter.override")}
          </ToggleChip>
        </div>
      </div>
      {overriding ? (
        <TextField
          id={id}
          label={label}
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
          className="[&>label]:sr-only"
          // 分割符の文字列（短い記号から境界のマーカーまで。#1891）。
          width="md"
        />
      ) : (
        <InheritResolved value={effectiveValue ?? "—"} label={label} globalHref={null} />
      )}
      <p className="text-xs text-fg-muted">{t("settings.chunking.params.delimiterHint")}</p>
    </div>
  );
}

function InheritResolved({
  value,
  label,
  globalHref,
}: {
  value: string;
  label: string;
  globalHref: string | null;
}) {
  return (
    <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-muted">
      <span>{t("knowledgeBases.adapter.inheritResolved", { value })}</span>
      {globalHref ? (
        <Link
          to={globalHref}
          aria-label={t("knowledgeBases.adapter.openGlobalSettingsAria", { name: label })}
          className="inline-flex min-h-6 items-center font-medium text-accent-fg underline-offset-2 hover:underline"
        >
          {t("knowledgeBases.adapter.openGlobalSettings")}
        </Link>
      ) : null}
    </p>
  );
}
