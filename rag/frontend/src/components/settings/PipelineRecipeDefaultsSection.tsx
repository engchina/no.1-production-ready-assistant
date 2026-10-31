import {
  ButtonLink,
  FormActionBar,
  FormStatus,
  Skeleton,
  Switch,
  TimedLoadingState,
} from "@production-ready/ui";
import { ArrowDown, RotateCcw, Save } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import {
  globalSettingsHref,
  recipeConfigGroups,
  type RecipeConfigGroup,
  type RecipeConfigItem,
} from "@/components/documents/DocumentProcessingConfigPanel.logic";
import { recipeConfigValueLabel } from "@/components/documents/DocumentProcessingConfigPanel.values";
import { canOpenNavRoute } from "@/components/layout/nav-config";
import { useAuth } from "@/components/security/AuthProvider";
import { ApiErrorState } from "@/components/StateViews";
import {
  ApiError,
  type PipelineAutoAdvanceField,
  type PipelineSettingsData,
} from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { usePipelineSettings, useUpdatePipelineSettings } from "@/lib/queries";
import { APP_ROUTES } from "@/lib/routes";
import { SETTINGS_ANCHORS } from "@/lib/settings-anchors";
import { toast } from "@/lib/toast";

import { isAutoAdvanceItem, splitGateItems, type AutoAdvanceForm } from "./PipelineRecipeDefaults.logic";

const GROUPS = recipeConfigGroups();

function formFromSettings(data: PipelineSettingsData): AutoAdvanceForm {
  return {
    auto_parse_after_preprocess_enabled: data.auto_parse_after_preprocess_enabled,
    auto_chunk_after_extract_enabled: data.auto_chunk_after_extract_enabled,
    auto_index_after_chunk_enabled: data.auto_index_after_chunk_enabled,
  };
}

function sameForm(left: AutoAdvanceForm, right: AutoAdvanceForm) {
  return (
    left.auto_parse_after_preprocess_enabled === right.auto_parse_after_preprocess_enabled &&
    left.auto_chunk_after_extract_enabled === right.auto_chunk_after_extract_enabled &&
    left.auto_index_after_chunk_enabled === right.auto_index_after_chunk_enabled
  );
}

/**
 * 設定の概要の「取込の流れと全体の既定」（#528）。
 *
 * レシピ 11 項目を、選択中レシピの設定と同じ定義・同じ処理順（RECIPE_CONFIG_ITEMS。#523）で出す。
 * 工程（ファイル準備 → 抽出 → Chunk 作成 → Embedding / 索引）を上から下へ並べ、工程の間の
 * 自動進行のゲートはここで切り替えて保存する。ほかの項目は全体の既定の値と、変える画面へのリンクを出す。
 */
export function PipelineRecipeDefaultsSection() {
  const query = usePipelineSettings();
  const title = t("settings.pipeline.flow.title");
  return (
    <section
      id={SETTINGS_ANCHORS.pipelineFlow}
      aria-labelledby={`${SETTINGS_ANCHORS.pipelineFlow}-title`}
      className="scroll-mt-24 space-y-3"
    >
      <div>
        <h2 id={`${SETTINGS_ANCHORS.pipelineFlow}-title`} className="text-sm font-semibold text-fg">
          {title}
        </h2>
        <p className="mt-0.5 text-xs leading-relaxed text-fg-muted">
          {t("settings.pipeline.flow.description")}
        </p>
      </div>
      {query.isPending ? (
        <TimedLoadingState
          label={t("settings.pipeline.flow.loading")}
          operationKey="settings-pipeline-flow-load"
          testId="settings-pipeline-flow-loading"
        >
          <div className="space-y-3">
            {GROUPS.map((group) => (
              <Skeleton key={group.phase} className="h-24 w-full" />
            ))}
          </div>
        </TimedLoadingState>
      ) : query.isError || !query.data ? (
        <ApiErrorState
          error={query.error}
          fallback={t("settings.pipeline.flow.loadError")}
          onRetry={() => void query.refetch()}
        />
      ) : (
        <RecipeDefaultsFlow data={query.data} />
      )}
    </section>
  );
}

function RecipeDefaultsFlow({ data }: { data: PipelineSettingsData }) {
  const save = useUpdatePipelineSettings();
  const saved = formFromSettings(data);
  const [form, setForm] = useState<AutoAdvanceForm>(saved);
  // 保存値が変わったレンダーで、未編集なら編集中の値を保存値へそろえる。
  const [base, setBase] = useState<AutoAdvanceForm>(saved);
  if (!sameForm(base, saved)) {
    setBase(saved);
    if (sameForm(base, form)) setForm(saved);
  }
  const dirty = !sameForm(form, saved);
  useLeaveGuard(dirty, save.isPending);

  function toggle(field: PipelineAutoAdvanceField, checked: boolean) {
    save.reset();
    setForm((current) => ({ ...current, [field]: checked }));
  }

  function submit() {
    if (save.isPending) return;
    save.mutate(form, {
      onSuccess: (next) => {
        const nextForm = formFromSettings(next);
        setBase(nextForm);
        setForm(nextForm);
        // 保存の成功は Toast、失敗は操作の行の FormStatus（messaging.md §10.2。#992）。
        toast.success(t("settings.pipeline.flow.saved"));
      },
    });
  }

  function reset() {
    save.reset();
    setForm(saved);
  }

  return (
    <div className="space-y-4">
      <ol className="space-y-0" data-testid="pipeline-recipe-defaults">
        {GROUPS.map((group) => {
          const { items, gate } = splitGateItems(group.items);
          return (
            <li key={group.phase} data-config-phase={group.phase}>
              <PhaseCard group={group} items={items} data={data} />
              {gate && isAutoAdvanceItem(gate) ? (
                <GateConnector
                  item={gate}
                  checked={form[gate.field]}
                  disabled={save.isPending}
                  onChange={(checked) => toggle(gate.field, checked)}
                />
              ) : null}
            </li>
          );
        })}
      </ol>
      <FormActionBar
        ariaLabel={t("settings.pipeline.flow.actionsLabel")}
        primaryActions={[
          {
            id: "save",
            label: t("settings.pipeline.flow.save"),
            icon: Save,
            loading: save.isPending,
            disabled: !dirty,
            onClick: submit,
          },
        ]}
        secondaryActions={[
          {
            id: "reset",
            label: t("settings.pipeline.flow.reset"),
            icon: RotateCcw,
            disabled: !dirty || save.isPending,
            onClick: reset,
          },
        ]}
        status={
          save.isError ? (
            <FormStatus
              tone="danger"
              message={
                save.error instanceof ApiError ? save.error.message : t("settings.pipeline.flow.saveError")
              }
            />
          ) : dirty ? (
            <FormStatus tone="warning" message={t("settings.pipeline.flow.unsaved")} />
          ) : null
        }
      />
    </div>
  );
}

/** 1 工程の項目（ゲートを除く）。値は全体の既定、リンクは変える画面（権限のある画面だけ）。 */
function PhaseCard({
  group,
  items,
  data,
}: {
  group: RecipeConfigGroup;
  items: RecipeConfigItem[];
  data: PipelineSettingsData;
}) {
  const { hasPermission } = useAuth();
  const headingId = `pipeline-phase-${group.phase.toLowerCase()}`;
  return (
    <section
      aria-labelledby={headingId}
      className="rounded-lg border border-border bg-surface p-3"
    >
      <h3 id={headingId} className="text-sm font-semibold text-fg">
        {t("documents.processingConfig.phaseHeading", { index: group.index, phase: t(group.label) })}
      </h3>
      <dl className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {group.phase === "INDEX" ? (
          <div data-config-field="vector_index" className="min-w-0 rounded-md border border-border bg-surface-sunken px-3 py-2">
            <dt className="text-xs text-fg-muted">{t("nav.settingsVectorIndex")}</dt>
            <dd className="mt-0.5 text-sm font-medium text-fg">{t(`settings.vectorIndex.profile.${data.vector_index_profile}`)}</dd>
            {canOpenNavRoute(APP_ROUTES.settingsVectorIndex, hasPermission) ? (
              <dd className="mt-1">
                <ButtonLink linkComponent={Link} to={APP_ROUTES.settingsVectorIndex} variant="ghost" size="sm" className="text-accent-fg underline underline-offset-2" aria-label={t("settings.pipeline.flow.openSettingsAria", { name: t("nav.settingsVectorIndex") })}>
                  {t("settings.pipeline.flow.openSettings")}
                </ButtonLink>
              </dd>
            ) : null}
          </div>
        ) : null}
        {items.map((item) => {
          const name = t(item.label);
          // 権限のある設定画面への導線だけを出す。
          const href =
            item.globalSettings && canOpenNavRoute(item.globalSettings.route, hasPermission)
              ? globalSettingsHref(item)
              : null;
          return (
            <div
              key={item.field}
              data-config-field={item.field}
              className="min-w-0 rounded-md border border-border bg-surface-sunken px-3 py-2"
            >
              <dt className="text-xs text-fg-muted">{name}</dt>
              <dd className="mt-0.5 break-words text-sm font-medium text-fg">
                {recipeConfigValueLabel(item, data.recipe_defaults)}
              </dd>
              {href ? (
                <dd className="mt-1">
                  <ButtonLink
                    linkComponent={Link}
                    to={href}
                    aria-label={t("settings.pipeline.flow.openSettingsAria", { name })}
                    variant="ghost"
                    className="text-accent-fg underline underline-offset-2"
                    size="sm"
                  >
                    {t("settings.pipeline.flow.openSettings")}
                  </ButtonLink>
                </dd>
              ) : null}
            </div>
          );
        })}
      </dl>
    </section>
  );
}

/** 工程の間の自動進行のゲート。流れの矢印の上に、次の工程へ自動で進むかのスイッチを置く。 */
function GateConnector({
  item,
  checked,
  disabled,
  onChange,
}: {
  item: RecipeConfigItem & { field: PipelineAutoAdvanceField };
  checked: boolean;
  disabled: boolean;
  onChange: (checked: boolean) => void;
}) {
  // hash の移動先（レシピの「グローバル設定を開く」。#528）は、このゲートの行。
  const anchor = item.globalSettings?.anchor ?? `pipeline-gate-${item.field}`;
  const labelId = `${anchor}-label`;
  const hintId = `${anchor}-hint`;
  return (
    <div
      id={anchor}
      className="flex scroll-mt-24 items-stretch gap-3 py-1 pl-4"
      data-config-field={item.field}
    >
      {/* 工程と工程をつなぐ線と矢印（装飾。読み上げは見出しとスイッチで伝える）。 */}
      <div className="flex w-5 shrink-0 flex-col items-center" aria-hidden>
        <span className="w-px flex-1 bg-border-control" />
        <ArrowDown size={16} className="shrink-0 text-fg-muted" />
        <span className="w-px flex-1 bg-border-control" />
      </div>
      <div className="my-1 flex min-w-0 flex-1 items-start justify-between gap-4 rounded-md border border-dashed border-border-control bg-surface px-3 py-2">
        <div className="min-w-0">
          <p id={labelId} className="text-sm font-medium text-fg">
            {t(item.label)}
          </p>
          <p id={hintId} className="mt-0.5 text-xs leading-relaxed text-fg-muted">
            {t(`settings.pipeline.flow.gate.${item.field}` as I18nKey)}
          </p>
        </div>
        <Switch
          checked={checked}
          disabled={disabled}
          aria-labelledby={labelId}
          aria-describedby={hintId}
          onCheckedChange={onChange}
        />
      </div>
    </div>
  );
}
