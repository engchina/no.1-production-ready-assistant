import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Disclosure,
  FormActionBar,
  FormStatus,
  StatusBadge,
  Switch,
  TextField,
} from "@production-ready/ui";
import { ListChecks, RotateCcw, Save, Sparkles } from "lucide-react";
import { Fragment, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { canOpenNavRoute } from "@/components/layout/nav-config";
import { useAuth } from "@/components/security/AuthProvider";
import { ApiError, type ParserAdapterSettingsData } from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useExtractionFieldsSettings, useUpdateParserAdapterSettings } from "@/lib/queries";
import { APP_ROUTES } from "@/lib/routes";
import { SETTINGS_ANCHORS } from "@/lib/settings-anchors";
import { toast } from "@/lib/toast";

import { FeaturePromptDisclosure } from "./AnswerPromptEditor";
import { ExtractionFieldsEditor } from "./ExtractionFieldsEditor";
import { SectionRulesEditor } from "./SectionRulesEditor";

type PostParseField =
  | "vision_enabled"
  | "heading_level_inference_enabled"
  | "field_extraction_enabled"
  | "navigation_summary_enabled"
  | "raptor_enabled";
type PostParseForm = Record<PostParseField, boolean> & {
  /** 図・表の説明（Vision）を同時に呼ぶ数の上限（入力中の文字列。#2002）。 */
  vision_concurrency: string;
};

export const VISION_CONCURRENCY_MIN = 1;
export const VISION_CONCURRENCY_MAX = 16;
const VISION_CONCURRENCY_DEFAULT = 4;
const VISION_CONCURRENCY_FIELD_ID = "post-parse-vision-concurrency";

/** 入力の文字列を同時の数にする（整数で範囲の中だけ。ほかは null）。 */
export function parseVisionConcurrency(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number(trimmed);
  return parsed >= VISION_CONCURRENCY_MIN && parsed <= VISION_CONCURRENCY_MAX ? parsed : null;
}

/**
 * 解析の後の処理の順（取込の `_attach_vision` → `_attach_heading_levels` → `_attach_extraction_fields`
 * → `_attach_navigation_tree`）。
 */
const POST_PARSE_STEPS: readonly {
  field: PostParseField;
  label: I18nKey;
  hint: I18nKey;
  anchor: string;
}[] = [
  {
    field: "vision_enabled",
    label: "knowledgeBases.adapter.field.vision",
    hint: "settings.parserAdapters.postParse.vision.hint",
    anchor: SETTINGS_ANCHORS.vision,
  },
  {
    field: "heading_level_inference_enabled",
    label: "knowledgeBases.adapter.field.headingLevels",
    hint: "settings.parserAdapters.postParse.headingLevels.hint",
    anchor: SETTINGS_ANCHORS.headingLevels,
  },
  {
    field: "field_extraction_enabled",
    label: "knowledgeBases.adapter.field.fieldExtraction",
    hint: "settings.parserAdapters.postParse.fieldExtraction.hint",
    anchor: SETTINGS_ANCHORS.fieldExtraction,
  },
  {
    field: "navigation_summary_enabled",
    label: "knowledgeBases.adapter.field.navigationSummary",
    hint: "settings.parserAdapters.postParse.navigationSummary.hint",
    anchor: SETTINGS_ANCHORS.navigationSummary,
  },
  {
    field: "raptor_enabled",
    label: "settings.answerPrompts.stage.raptor_summary",
    hint: "settings.raptor.description",
    anchor: SETTINGS_ANCHORS.raptorSummary,
  },
];

function formFromSettings(settings: ParserAdapterSettingsData): PostParseForm {
  return {
    vision_enabled: settings.vision_enabled,
    heading_level_inference_enabled: settings.heading_level_inference_enabled ?? false,
    field_extraction_enabled: settings.field_extraction_enabled,
    navigation_summary_enabled: settings.navigation_summary_enabled,
    raptor_enabled: settings.raptor_enabled ?? false,
    vision_concurrency: String(settings.vision_concurrency ?? VISION_CONCURRENCY_DEFAULT),
  };
}

function sameForm(left: PostParseForm, right: PostParseForm) {
  return (
    POST_PARSE_STEPS.every((step) => left[step.field] === right[step.field]) &&
    left.vision_concurrency.trim() === right.vision_concurrency.trim()
  );
}

/**
 * 文書解析の「解析後の処理」（#528）。Vision・見出し階層・項目抽出・章節規則・章節要約の全体の既定を、
 * 取込の処理順に並べて保存する（保存先は backend/.env。文書のレシピで上書きできる）。
 * Vision の読み取りプロンプトと、項目抽出の項目の定義も、それぞれの項目の中で編集する。
 */
export function PostParseSettingsCard({ settings }: { settings: ParserAdapterSettingsData }) {
  const save = useUpdateParserAdapterSettings();
  const saved = formFromSettings(settings);
  const [form, setForm] = useState<PostParseForm>(saved);
  // 保存値が変わったレンダーで、未編集なら編集中の値を保存値へそろえる（ほかの節の保存でも data は変わる）。
  const [base, setBase] = useState<PostParseForm>(saved);
  if (!sameForm(base, saved)) {
    setBase(saved);
    if (sameForm(base, form)) setForm(saved);
  }
  const dirty = !sameForm(form, saved);
  useLeaveGuard(dirty, save.isPending);
  const fieldsQuery = useExtractionFieldsSettings();
  const fieldCount = fieldsQuery.data?.fields.length;
  const usesStandardFields = fieldsQuery.data?.uses_standard === true;

  function toggle(field: PostParseField, checked: boolean) {
    save.reset();
    setForm((current) => ({ ...current, [field]: checked }));
  }

  const concurrency = parseVisionConcurrency(form.vision_concurrency);
  const [showConcurrencyError, setShowConcurrencyError] = useState(false);
  const concurrencyError =
    showConcurrencyError && concurrency === null
      ? t("settings.parserAdapters.postParse.vision.concurrencyError", {
          min: VISION_CONCURRENCY_MIN,
          max: VISION_CONCURRENCY_MAX,
        })
      : undefined;

  function submit() {
    if (save.isPending) return;
    if (concurrency === null) {
      setShowConcurrencyError(true);
      document.getElementById(VISION_CONCURRENCY_FIELD_ID)?.focus();
      return;
    }
    const payload = {
      vision_enabled: form.vision_enabled,
      heading_level_inference_enabled: form.heading_level_inference_enabled,
      field_extraction_enabled: form.field_extraction_enabled,
      navigation_summary_enabled: form.navigation_summary_enabled,
      raptor_enabled: form.raptor_enabled,
      vision_concurrency: concurrency,
    };
    save.mutate(payload, {
      onSuccess: (data) => {
        const next = formFromSettings(data);
        setBase(next);
        setForm(next);
        // 保存の成功は Toast、失敗は操作の行の FormStatus（messaging.md §10.2）。
        toast.success(t("settings.parserAdapters.postParse.saved"));
      },
    });
  }

  function reset() {
    save.reset();
    setShowConcurrencyError(false);
    setForm(saved);
  }

  const extras: Record<PostParseField, ReactNode> = {
    vision_enabled: (
      <>
        <TextField
          id={VISION_CONCURRENCY_FIELD_ID}
          type="number"
          inputMode="numeric"
          min={VISION_CONCURRENCY_MIN}
          max={VISION_CONCURRENCY_MAX}
          width="xs"
          label={t("settings.parserAdapters.postParse.vision.concurrency")}
          helper={t("settings.parserAdapters.postParse.vision.concurrencyHint", {
            min: VISION_CONCURRENCY_MIN,
            max: VISION_CONCURRENCY_MAX,
          })}
          error={concurrencyError}
          required
          disabled={save.isPending}
          value={form.vision_concurrency}
          onValueChange={(value) => {
            save.reset();
            setForm((current) => ({ ...current, vision_concurrency: value }));
          }}
        />
        <VisionExtras />
      </>
    ),
    heading_level_inference_enabled: <FeaturePromptDisclosure promptKey="heading_levels" />,
    field_extraction_enabled: (
      <>
        {form.field_extraction_enabled && fieldCount === 0 ? (
          <FormStatus
            tone="warning"
            className="text-xs"
            message={t("settings.parserAdapters.postParse.fieldExtraction.empty")}
          />
        ) : null}
        <FeaturePromptDisclosure promptKey="field_extraction" />
        <Disclosure
          summary={t("settings.parserAdapters.postParse.fieldExtraction.definitions")}
          icon={ListChecks}
          surface="sunken"
          meta={
            fieldCount !== undefined ? (
              <span className="flex flex-wrap items-center gap-2">
                {/* 一度も保存していない環境は標準の項目を使う（#556）。 */}
                {usesStandardFields ? (
                  <StatusBadge
                    variant="info"
                    label={t("settings.parserAdapters.postParse.fieldExtraction.standard")}
                  />
                ) : null}
                <span className="tnum text-xs text-fg-muted">
                  {t("settings.parserAdapters.postParse.fieldExtraction.count", {
                    count: fieldCount,
                  })}
                </span>
              </span>
            ) : null
          }
        >
          <ExtractionFieldsEditor />
        </Disclosure>
      </>
    ),
    navigation_summary_enabled: <FeaturePromptDisclosure promptKey="navigation_summary" />,
    raptor_enabled: <FeaturePromptDisclosure promptKey="raptor_summary_system" />,
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start gap-3">
          <div data-surface-tint="info" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-info-subtle text-info-fg">
            <Sparkles size={20} aria-hidden />
          </div>
          <div>
            <CardTitle>{t("settings.parserAdapters.postParse.title")}</CardTitle>
            <CardDescription>{t("settings.parserAdapters.postParse.description")}</CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <ol className="space-y-3">
          {POST_PARSE_STEPS.map((step, index) => {
            const labelId = `${step.anchor}-label`;
            const hintId = `${step.anchor}-hint`;
            const name = t(step.label);
            return (
              <Fragment key={step.field}>
                {step.field === "navigation_summary_enabled" ? (
                  <li
                    id={SETTINGS_ANCHORS.sectionRules}
                    className="scroll-mt-24 space-y-3 rounded-md border border-border bg-surface p-3"
                  >
                    <h3 className="text-sm font-semibold text-fg">
                      {t("settings.parserAdapters.postParse.step", {
                        index: 4,
                        name: t("sectionRules.title"),
                      })}
                    </h3>
                    <SectionRulesEditor />
                  </li>
                ) : null}
                <li
                  id={step.anchor}
                  // hash で移動したときに、上端の固定ヘッダーに隠れないようにする。
                  className="scroll-mt-24 space-y-3 rounded-md border border-border bg-surface p-3"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <h3 id={labelId} className="text-sm font-semibold text-fg">
                        {t("settings.parserAdapters.postParse.step", {
                          index: index + (index >= 3 ? 2 : 1),
                          name,
                        })}
                      </h3>
                      <p id={hintId} className="mt-1 text-xs leading-relaxed text-fg-muted">
                        {t(step.hint)}
                        {step.field === "navigation_summary_enabled" && settings.navigation_summary_max_nodes != null ? (
                          <> {t("settings.parserAdapters.postParse.navigationSummary.limit", {count: settings.navigation_summary_max_nodes})}</>
                        ) : null}
                      </p>
                    </div>
                    <Switch
                      checked={form[step.field]}
                      disabled={save.isPending}
                      aria-label={name}
                      aria-describedby={hintId}
                      onCheckedChange={(checked) => toggle(step.field, checked)}
                    />
                  </div>
                  {extras[step.field]}
                </li>
              </Fragment>
            );
          })}
        </ol>
        <FormActionBar
          ariaLabel={t("settings.parserAdapters.postParse.actionsLabel")}
          primaryActions={[
            {
              id: "save",
              label: t("settings.parserAdapters.postParse.save"),
              icon: Save,
              loading: save.isPending,
              disabled: !dirty,
              onClick: submit,
            },
          ]}
          secondaryActions={[
            {
              id: "reset",
              label: t("settings.parserAdapters.actions.reset"),
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
                  save.error instanceof ApiError
                    ? save.error.message
                    : t("settings.parserAdapters.postParse.saveError")
                }
              />
            ) : dirty ? (
              <FormStatus tone="warning" message={t("settings.parserAdapters.actions.unsaved")} />
            ) : null
          }
        />
      </CardContent>
    </Card>
  );
}

/** Vision の項目の補足: 既定の Vision モデルの場所と、図・画像の読み取りプロンプト（全体で 1 つ）。 */
function VisionExtras() {
  const { hasPermission } = useAuth();
  const canOpenModel = canOpenNavRoute(APP_ROUTES.settingsModel, hasPermission);
  return (
    <>
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted">
        {t("settings.parserAdapters.postParse.vision.model")}
        {canOpenModel ? (
          <Link
            to={APP_ROUTES.settingsModel}
            className="font-medium text-accent-fg underline-offset-2 hover:underline"
          >
            {t("settings.parserAdapters.postParse.vision.openModel")}
          </Link>
        ) : null}
      </p>
      {/* 読み取りの指示は Vision を使うときだけ効く。全体で 1 つなので、既定を無効にしていても編集できる（#497）。 */}
      <FeaturePromptDisclosure promptKey="image_retrieval" />
    </>
  );
}
