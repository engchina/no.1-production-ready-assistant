import {
  Button,
  Disclosure,
  FormActionBar,
  FormGrid,
  FormSkeleton,
  FormStatus,
  RowActionMenu,
  SelectField,
  Switch,
  TextField,
  TimedLoadingState,
  toast,
  type EntityAction,
  type SelectFieldOption,
} from "@production-ready/ui";
import { ArrowDown, ArrowUp, Plus, RotateCcw, Save, Trash2, Undo2 } from "lucide-react";
import { useState } from "react";


import { SECTION_RULES_OPTIONS } from "@/components/documents/DocumentProcessingConfigPanel.values";
import { ApiErrorState } from "@/components/StateViews";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  ApiError,
  type SectionRule,
  type SectionRulesMode,
  type SectionRulesSettingsData,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import {
  useResetSectionRulesSettings,
  useSaveSectionRulesSettings,
  useSectionRulesSettings,
} from "@/lib/queries";

const LEVEL_OPTIONS: SelectFieldOption<string>[] = [1, 2, 3, 4, 5, 6].map((level) => ({
  value: String(level),
  label: t("sectionRules.level", { level }),
}));

type Draft = { mode: SectionRulesMode; rules: SectionRule[] };

function draftFrom(data: SectionRulesSettingsData): Draft {
  return { mode: data.mode, rules: data.rules };
}

/** 正規表現として読めるか（backend の検証と同じく、保存の前に欄の下で知らせる）。 */
function patternError(pattern: string): string | undefined {
  if (!pattern.trim()) return t("sectionRules.error.patternRequired");
  try {
    new RegExp(pattern);
    return undefined;
  } catch {
    return t("sectionRules.error.patternInvalid");
  }
}

function move<T>(items: readonly T[], from: number, to: number): T[] {
  const next = [...items];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

/**
 * 章節の抽出規則の全体の既定（#715）。方式（解析エンジンの見出し / プリセット / 独自の規則）を選び、
 * 独自の規則は名前・正規表現・階層・有効を一覧で編集する（並びが優先順位）。規則は章節ナビゲーションの
 * 章節だけに当て、取込のやり直しは要らない。
 */
export function SectionRulesEditor() {
  const query = useSectionRulesSettings();
  const save = useSaveSectionRulesSettings();
  const reset = useResetSectionRulesSettings();
  const confirm = useConfirm();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState("");

  if (query.isPending) {
    return (
      <TimedLoadingState label={t("sectionRules.loading")} operationKey="section-rules">
        <FormSkeleton fields={3} />
      </TimedLoadingState>
    );
  }
  if (query.isError) {
    return (
      <ApiErrorState
        error={query.error}
        fallback={t("sectionRules.error.load")}
        onRetry={() => void query.refetch()}
      />
    );
  }
  const data = query.data;
  const current = draft ?? draftFrom(data);
  const dirty = draft !== null;
  const presetRules = current.mode === "custom" ? [] : (data.presets[current.mode] ?? []);
  const update = (next: Partial<Draft>) => setDraft({ ...current, ...next });
  const updateRule = (index: number, patch: Partial<SectionRule>) =>
    update({ rules: current.rules.map((rule, i) => (i === index ? { ...rule, ...patch } : rule)) });
  const errors = current.mode === "custom" ? current.rules.map((rule) => ({
    name: rule.name.trim() ? undefined : t("sectionRules.error.nameRequired"),
    pattern: patternError(rule.pattern),
  })) : [];
  const hasErrors = errors.some((item) => item.name || item.pattern);

  const submit = () => {
    setSubmitted(true);
    if (hasErrors) {
      setError(t("sectionRules.error.fix"));
      return;
    }
    setError("");
    save.mutate(current, {
      onSuccess: () => {
        setDraft(null);
        setSubmitted(false);
        toast.success(t("sectionRules.toast.saved"));
      },
      onError: (failure) =>
        setError(failure instanceof ApiError ? failure.message : t("sectionRules.error.save")),
    });
  };

  const resetToDefault = async () => {
    const confirmed = await confirm({
      title: t("sectionRules.reset.confirm.title"),
      description: t("sectionRules.reset.confirm.description"),
      confirmLabel: t("sectionRules.reset.action"),
      tone: "danger",
    });
    if (!confirmed) return;
    reset.mutate(undefined, {
      onSuccess: () => {
        setDraft(null);
        setSubmitted(false);
        toast.success(t("sectionRules.toast.reset"));
      },
      onError: (failure) =>
        setError(failure instanceof ApiError ? failure.message : t("sectionRules.error.save")),
    });
  };

  const rowActions = (index: number): EntityAction[] => [
    {
      id: "up",
      label: t("sectionRules.action.moveUp"),
      icon: ArrowUp,
      disabled: index === 0,
      onSelect: () => update({ rules: move(current.rules, index, index - 1) }),
    },
    {
      id: "down",
      label: t("sectionRules.action.moveDown"),
      icon: ArrowDown,
      disabled: index === current.rules.length - 1,
      onSelect: () => update({ rules: move(current.rules, index, index + 1) }),
    },
    {
      id: "delete",
      label: t("sectionRules.action.delete"),
      icon: Trash2,
      tone: "danger",
      onSelect: () => update({ rules: current.rules.filter((_, i) => i !== index) }),
    },
  ];

  return (
    <div className="space-y-4" data-testid="section-rules-editor">
      <p className="text-xs text-fg-muted">{t("sectionRules.description")}</p>
      <p className="text-xs text-fg-muted">{t("sectionRules.scopeHint")}</p>
      <Disclosure summary={t("sectionRules.example.title")}>
        <div className="space-y-2 text-sm text-fg-muted">
          <p>{t("sectionRules.example.input")}</p>
          <pre className="whitespace-pre-wrap text-sm text-fg">{t("sectionRules.example.tree")}</pre>
          <p>{t("sectionRules.example.output")}</p>
        </div>
      </Disclosure>
      {/* 方式と規則の行はフォームの最大幅まで（パターンの欄が 1920px で伸びすぎない。#1891）。 */}
      <FormGrid columns={1}>
        <SelectField
          id="section-rules-mode"
          label={t("sectionRules.mode.label")}
          value={current.mode}
          options={SECTION_RULES_OPTIONS}
          width="lg"
          wrapOptions
          onValueChange={(mode) => update({ mode })}
        />

        {current.mode === "parser" ? (
          <p className="text-sm text-fg-muted">{t("sectionRules.parserHint")}</p>
        ) : current.mode !== "custom" ? (
          <div className="space-y-2">
            <p className="text-sm text-fg-muted">{t("sectionRules.presetHint")}</p>
            <ul className="divide-y divide-border rounded-md border border-border bg-surface-sunken">
              {presetRules.map((rule) => (
                <li key={rule.name} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
                  <span className="font-medium text-fg">{rule.name}</span>
                  <span className="text-xs text-fg-muted">{t("sectionRules.level", { level: rule.level })}</span>
                  <code className="break-all text-xs text-fg-muted">{rule.pattern}</code>
                </li>
              ))}
            </ul>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              icon={Plus}
              onClick={() => update({ mode: "custom", rules: presetRules })}
            >
              {t("sectionRules.action.copyPreset")}
            </Button>
          </div>
        ) : (
          <Disclosure summary={t("sectionRules.customDetails")} defaultOpen>
          <div className="space-y-2">
            <p className="text-sm text-fg-muted">{t("sectionRules.customHint")}</p>
            {current.rules.length > 0 ? (
              // 1 行に 1 規則の欄を並べる表の形の編集の行（名前 sm・パターンは残りの幅・階層 xs）なので、欄の幅の段の
              // 検査（findFieldsOffWidthScale）の対象から外す（表の中の欄と同じ扱い。#1891）。
              <ol className="space-y-2" aria-label={t("sectionRules.list.label")} data-field-width-exempt="">
                {current.rules.map((rule, index) => {
                  const rowErrors = submitted ? errors[index] : undefined;
                  const name = rule.name.trim() || t("sectionRules.untitled");
                  return (
                    // 並べ替えで行が動くので、位置と名前を key にする（同じ名前の規則も並べられる）。
                    <li
                      key={`${index}-${rule.name}`}
                      className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-start gap-2 rounded-md border border-border bg-surface p-2 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)_8rem_auto_auto]"
                    >
                      <div className="col-span-3 sm:col-span-1">
                        <TextField
                          id={`section-rule-name-${index}`}
                          label={t("sectionRules.field.name")}
                          width="sm"
                          size="sm"
                          value={rule.name}
                          error={rowErrors?.name}
                          onValueChange={(value) => updateRule(index, { name: value })}
                        />
                      </div>
                      <div className="col-span-3 sm:col-span-1">
                        <TextField
                          id={`section-rule-pattern-${index}`}
                          label={t("sectionRules.field.pattern")}
                          width="full"
                          size="sm"
                          value={rule.pattern}
                          placeholder="^第[0-9]+章"
                          error={rowErrors?.pattern}
                          onValueChange={(value) => updateRule(index, { pattern: value })}
                        />
                      </div>
                      <SelectField
                        id={`section-rule-level-${index}`}
                        label={t("sectionRules.field.level")}
                        width="xs"
                        size="sm"
                        value={String(rule.level)}
                        options={LEVEL_OPTIONS}
                        onValueChange={(value) => updateRule(index, { level: Number(value) })}
                      />
                      <label className="flex items-center gap-2 pt-6 text-xs text-fg-muted">
                        <Switch
                          checked={rule.enabled}
                          aria-label={t("sectionRules.field.enabledAria", { name })}
                          onCheckedChange={(enabled) => updateRule(index, { enabled })}
                        />
                        {t("sectionRules.field.enabled")}
                      </label>
                      <div className="pt-6">
                        <RowActionMenu
                          actions={rowActions(index)}
                          ariaLabel={t("sectionRules.actions.aria", { name })}
                          testId={`section-rule-actions-${index}`}
                        />
                      </div>
                    </li>
                  );
                })}
              </ol>
            ) : (
              <p className="text-sm text-fg-muted">{t("sectionRules.empty")}</p>
            )}
            <Button
              type="button"
              variant="secondary"
              size="sm"
              icon={Plus}
              onClick={() =>
                update({ rules: [...current.rules, { name: "", pattern: "", level: 1, enabled: true }] })
              }
            >
              {t("sectionRules.action.add")}
            </Button>
          </div>
          </Disclosure>
        )}
      </FormGrid>

      <FormActionBar
        ariaLabel={t("sectionRules.actions.form")}
        primaryActions={[
          {
            id: "save",
            label: t("sectionRules.action.save"),
            icon: Save,
            loading: save.isPending,
            disabled: !dirty || reset.isPending,
            onClick: submit,
          },
        ]}
        secondaryActions={[
          {
            id: "discard",
            label: t("sectionRules.action.discard"),
            icon: Undo2,
            disabled: !dirty,
            onClick: () => {
              setDraft(null);
              setSubmitted(false);
              setError("");
            },
          },
        ]}
        dangerActions={[
          {
            id: "reset",
            label: t("sectionRules.reset.action"),
            icon: RotateCcw,
            loading: reset.isPending,
            // 保存の処理中は押せないだけにする（スピナーは押した側だけ。#819）。
            disabled: save.isPending,
            onClick: () => void resetToDefault(),
          },
        ]}
        status={error ? <FormStatus tone="danger" message={error} /> : null}
      />

    </div>
  );
}
