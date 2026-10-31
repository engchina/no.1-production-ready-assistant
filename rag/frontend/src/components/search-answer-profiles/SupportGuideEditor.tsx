import {
  Archive,
  ArchiveRestore,
  ArrowDown,
  ArrowUp,
  CheckCheck,
  Eye,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { useState, type ReactNode } from "react";

import {
  Banner,
  Button,
  EmptyState,
  Fieldset,
  FormGrid,
  FormSkeleton,
  FormStatus,
  ObjectActionBar,
  RowActionMenu,
  SearchableMultiSelect,
  SelectField,
  StatusBadge,
  Switch,
  TableSkeleton,
  TextareaField,
  TextField,
  TimedLoadingState,
  toast,
  useConfirm,
  type SearchableSelectOption,
  type SelectFieldOption,
  ListToolbar,
  PagedDataTable,
  PAGE_SIZE_OPTIONS,
  publishedVersionColumns,
} from "@production-ready/ui";

import { listCountProps, useClientListPaging } from "@/lib/list-paging";
import { paginationLabels } from "@/lib/pagination-labels";
import { ErrorState } from "@/components/StateViews";
import {
  api,
  ApiError,
  type SupportGuideBranchOperator,
  type SupportGuideConditionSource,
  type SupportGuideConditionType,
  type SupportGuideDetail,
  type SupportGuideImpactScope,
  type SupportGuideIssue,
  type SupportGuideRevisionSummary,
  type SupportGuideUnknownHandling,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import {
  initialLoadError,
  useSaveSupportGuide,
  useSupportGuide,
  useSupportGuideMutation,
  useSupportGuideRevision,
  useValidateSupportGuide,
} from "@/lib/queries";
import {
  booleanConditionValue,
  emptyGuideForm,
  guideContentFromForm,
  guideFormFromContent,
  guideFormIssues,
  guideFormSnapshot,
  MAX_GUIDE_REFERENCES,
  MAX_STEP_REFERENCES,
  moveItem,
  newBranchRow,
  newCompletionRow,
  newConditionRow,
  newStepRow,
  removeStep,
  renameConditionId,
  renameStepId,
  splitLines,
  SUPPORT_GUIDE_STATE_VARIANT,
  SUPPORT_GUIDE_TOOLS,
  supportGuideState,
  type BranchForm,
  type CompletionForm,
  type ConditionForm,
  type GuideForm,
  type GuideRowSection,
  type ReferenceForm,
  type StepForm,
} from "@/lib/support-guide-form";

import { SupportGuideContentView } from "./SupportGuideContentView";
import { SupportGuideDraftTry } from "./SupportGuideDraftTry";
import { SupportGuideHandoffContactField } from "./SupportGuideHandoffContactField";
import { SupportGuideIssueList } from "./SupportGuideIssueList";
import { SupportGuideReferencePicker } from "./SupportGuideReferencePicker";

function options<T extends string>(values: readonly T[], prefix: string): SelectFieldOption<T>[] {
  return values.map((value) => ({ value, label: t(`${prefix}.${value}` as I18nKey) }));
}

const CONDITION_TYPE_OPTIONS = options<SupportGuideConditionType>(
  ["enum", "text", "boolean"],
  "supportGuides.conditionType",
);
const SOURCE_OPTIONS = options<SupportGuideConditionSource>(
  ["user", "document", "tool"],
  "supportGuides.source",
);
const UNKNOWN_OPTIONS = options<SupportGuideUnknownHandling>(
  ["ask", "branch", "handoff"],
  "supportGuides.unknownHandling",
);
const OPERATOR_OPTIONS = options<SupportGuideBranchOperator>(
  ["equals", "in", "unknown"],
  "supportGuides.operator",
);
const SCOPE_OPTIONS = options<SupportGuideImpactScope>(
  ["individual", "group", "all"],
  "supportGuides.scope",
);

/** 共通の資料の選択を開いているときの `picking` の値（手順の行の key と重ならない）。 */
const REFERENCES_PICKER = "references";

const ROW_LABEL: Record<GuideRowSection, I18nKey> = {
  conditions: "supportGuides.row.condition",
  steps: "supportGuides.row.step",
  branches: "supportGuides.row.branch",
  references: "supportGuides.row.reference",
  completion: "supportGuides.row.completion",
};

/** 欄の DOM の id。path は検証の位置と同じ形（`steps[1].id`）で、行は index ではなく行の key で引く。 */
function fieldDomId(form: GuideForm, path: string): string {
  const withKey = path.replace(/^([a-z_]+)\[(\d+)\]/, (_, section: string, index: string) => {
    const rows = form[section as GuideRowSection] as readonly { key: string }[] | undefined;
    return `${section}-${rows?.[Number(index)]?.key ?? index}`;
  });
  return `support-guide-${withKey.replace(/[.[\]]+/g, "-").replace(/-$/, "")}`;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

type Failure = {
  kind: "conflict" | "refused" | "error";
  message: string;
  details: string[];
};

/** 保存・公開・ロールバックの失敗を、画面に出す形にする（409 は読み込み直しを案内する）。 */
function failureOf(error: unknown, fallback: string): Failure {
  if (error instanceof ApiError) {
    if (error.status === 409) return { kind: "conflict", message: error.message, details: [] };
    if (error.status === 422 && error.messages.length > 1) {
      // 公開・ロールバックの検証の失敗は、1 つ目が要約、2 つ目以降が問題の文。
      return { kind: "refused", message: error.messages[0], details: error.messages.slice(1) };
    }
    return {
      kind: "error",
      message: error.message,
      details: error.messages.slice(1).concat(
        error.fieldErrors.map((field) => field.message).filter((text) => !error.messages.includes(text)),
      ),
    };
  }
  return { kind: "error", message: fallback, details: [] };
}

type CheckResult = { source: "saved" | "validated"; issues: SupportGuideIssue[] };

/**
 * 業務ガイドの編集（下書きの保存・検証・公開・アーカイブ）と公開の履歴。
 * guideId が null なら新しく作る。読み込みの間と失敗はここで出し、フォームは読み込んだ内容から作る。
 */
export function SupportGuideEditor({
  searchAnswerProfileId,
  guideId,
  onCreated,
  onClose,
}: {
  searchAnswerProfileId: string;
  guideId: string | null;
  onCreated: (guideId: string) => void;
  onClose: () => void;
}) {
  const query = useSupportGuide(searchAnswerProfileId, guideId);
  // 409 のあと「最新の内容を読み込む」で、読み直した内容からフォームを作り直す。
  const [reloadToken, setReloadToken] = useState(0);
  if (guideId && query.isPending) {
    return (
      <TimedLoadingState
        label={t("supportGuides.editor.loading")}
        operationKey={`support-guide-${guideId}`}
        testId="support-guide-editor-loading"
      >
        <FormSkeleton fields={6} />
      </TimedLoadingState>
    );
  }
  const loadError = guideId ? initialLoadError(query) : null;
  if (guideId && (loadError || !query.data)) {
    return (
      <ErrorState
        message={errorMessage(loadError, t("supportGuides.editor.loadError"))}
        onRetry={() => void query.refetch()}
      />
    );
  }
  return (
    <SupportGuideForm
      key={`${guideId ?? "new"}-${reloadToken}`}
      searchAnswerProfileId={searchAnswerProfileId}
      detail={guideId ? (query.data ?? null) : null}
      onCreated={onCreated}
      onClose={onClose}
      onReload={async () => {
        await query.refetch();
        setReloadToken((value) => value + 1);
      }}
    />
  );
}

function SupportGuideForm({
  searchAnswerProfileId,
  detail,
  onCreated,
  onClose,
  onReload,
}: {
  searchAnswerProfileId: string;
  detail: SupportGuideDetail | null;
  onCreated: (guideId: string) => void;
  onClose: () => void;
  onReload: () => Promise<void>;
}) {
  const confirm = useConfirm();
  const [form, setForm] = useState<GuideForm>(() =>
    detail ? guideFormFromContent(detail.draft) : emptyGuideForm(),
  );
  const [baseline, setBaseline] = useState(() => guideFormSnapshot(form));
  const [clientIssues, setClientIssues] = useState<SupportGuideIssue[]>([]);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [check, setCheck] = useState<CheckResult | null>(() =>
    detail && detail.issues.length > 0 ? { source: "saved", issues: detail.issues } : null,
  );
  const [viewRevision, setViewRevision] = useState<number | null>(null);
  // 開いている資料の選択（共通の資料は REFERENCES_PICKER、手順の資料はその手順の行の key）。一度に 1 つだけ開く。
  const [picking, setPicking] = useState<string | null>(null);

  const save = useSaveSupportGuide(searchAnswerProfileId);
  const validate = useValidateSupportGuide(searchAnswerProfileId);
  const publish = useSupportGuideMutation(searchAnswerProfileId, (args: { guideId: string; base: number }) =>
    api.publishSupportGuide(searchAnswerProfileId, args.guideId, args.base),
  );
  const rollback = useSupportGuideMutation(
    searchAnswerProfileId,
    (args: { guideId: string; revision: number; base: number }) =>
      api.rollbackSupportGuide(searchAnswerProfileId, args.guideId, args.revision, args.base),
  );
  const setArchived = useSupportGuideMutation(
    searchAnswerProfileId,
    (args: { guideId: string; archived: boolean }) =>
      api.setSupportGuideArchived(searchAnswerProfileId, args.guideId, args.archived),
  );

  const dirty = guideFormSnapshot(form) !== baseline;
  const busy =
    save.isPending || validate.isPending || publish.isPending || rollback.isPending || setArchived.isPending;
  const confirmLeave = useLeaveGuard(dirty, busy);

  const archived = detail?.status === "archived";
  const state = detail ? supportGuideState(detail) : null;

  const errorAt = (path: string) =>
    clientIssues.find((issue) => issue.path === path)?.message ?? undefined;
  const domId = (path: string) => fieldDomId(form, path);

  const update = (patch: Partial<GuideForm>) => setForm((current) => ({ ...current, ...patch }));
  const updateRow = <S extends GuideRowSection>(
    section: S,
    index: number,
    patch: Partial<GuideForm[S][number]>,
  ) =>
    setForm((current) => ({
      ...current,
      [section]: (current[section] as GuideForm[S][number][]).map((row, i) =>
        i === index ? { ...row, ...patch } : row,
      ),
    }));
  const moveRow = (section: GuideRowSection, index: number, delta: number) =>
    setForm((current) => ({
      ...current,
      [section]: moveItem(current[section] as readonly unknown[], index, delta),
    }));
  const removeRow = (section: GuideRowSection, index: number) =>
    setForm((current) =>
      section === "steps"
        ? removeStep(current, index)
        : {
            ...current,
            [section]: (current[section] as readonly unknown[]).filter((_, i) => i !== index),
          },
    );

  /** 保存した内容（backend が整えた形）を新しい基準にする。 */
  const loadSaved = (saved: SupportGuideDetail) => {
    const next = guideFormFromContent(saved.draft);
    setForm(next);
    setBaseline(guideFormSnapshot(next));
  };

  const resetMessages = () => {
    setFailure(null);
    setClientIssues([]);
  };

  const runSave = () => {
    resetMessages();
    const issues = guideFormIssues(form);
    setClientIssues(issues);
    if (issues.length > 0) {
      // 最初の問題の欄へフォーカスを移す（欄の直下にも同じ文を出す）。
      document.getElementById(domId(issues[0].path))?.focus();
      return;
    }
    save.mutate(
      {
        guideId: detail?.guide_id ?? null,
        draft: guideContentFromForm(form),
        baseRevision: detail?.draft_revision ?? null,
      },
      {
        onSuccess: (saved) => {
          setCheck(saved.issues.length > 0 ? { source: "saved", issues: saved.issues } : null);
          if (!detail) {
            toast.success(t("supportGuides.created"));
            // 作ったガイドの編集に切り替える（フォームは保存した内容から作り直す）。
            setBaseline(guideFormSnapshot(form));
            onCreated(saved.guide_id);
            return;
          }
          loadSaved(saved);
          toast.success(t("supportGuides.saved"));
        },
        onError: (error) => setFailure(failureOf(error, t("supportGuides.saveError"))),
      },
    );
  };

  const runValidate = () => {
    if (!detail) return;
    resetMessages();
    validate.mutate(detail.guide_id, {
      onSuccess: (result) => setCheck({ source: "validated", issues: result.issues }),
      onError: (error) => setFailure(failureOf(error, t("supportGuides.validateError"))),
    });
  };

  const runPublish = async () => {
    if (!detail) return;
    resetMessages();
    const ok = await confirm({
      title: t("supportGuides.publishConfirm.title"),
      description: t("supportGuides.publishConfirm.description"),
      confirmLabel: t("supportGuides.publish"),
      tone: "info",
    });
    if (!ok) return;
    publish.mutate(
      { guideId: detail.guide_id, base: detail.draft_revision },
      {
        onSuccess: (published) => {
          loadSaved(published);
          setCheck(null);
          toast.success(t("supportGuides.published", { revision: published.published_revision ?? "" }));
        },
        onError: (error) => setFailure(failureOf(error, t("supportGuides.publishError"))),
      },
    );
  };

  const runRollback = async (revision: number) => {
    if (!detail) return;
    resetMessages();
    const ok = await confirm({
      title: t("supportGuides.rollbackConfirm.title", { revision }),
      description: t(
        dirty ? "supportGuides.rollbackConfirm.dirty" : "supportGuides.rollbackConfirm.description",
        { revision },
      ),
      confirmLabel: t("supportGuides.history.rollback"),
      tone: "warning",
      dismissOnOverlay: false,
    });
    if (!ok) return;
    rollback.mutate(
      { guideId: detail.guide_id, revision, base: detail.draft_revision },
      {
        onSuccess: (rolled) => {
          loadSaved(rolled);
          setCheck(null);
          setViewRevision(null);
          toast.success(
            t("supportGuides.rolledBack", { from: revision, revision: rolled.published_revision ?? "" }),
          );
        },
        onError: (error) => setFailure(failureOf(error, t("supportGuides.rollbackError"))),
      },
    );
  };

  const runArchive = async (next: boolean) => {
    if (!detail) return;
    resetMessages();
    if (next) {
      const ok = await confirm({
        title: t("supportGuides.archiveConfirm.title"),
        description: t("supportGuides.archiveConfirm.description", { name: detail.title }),
        confirmLabel: t("supportGuides.archive"),
        tone: "warning",
        dismissOnOverlay: false,
      });
      if (!ok) return;
    }
    setArchived.mutate(
      { guideId: detail.guide_id, archived: next },
      {
        onSuccess: () => toast.success(t(next ? "supportGuides.archived" : "supportGuides.restored")),
        onError: (error) => setFailure(failureOf(error, t("supportGuides.statusError"))),
      },
    );
  };

  const reloadLatest = async () => {
    if (dirty) {
      const ok = await confirm({
        title: t("supportGuides.discardConfirm.title"),
        description: t("supportGuides.conflict.reloadConfirm.description"),
        confirmLabel: t("supportGuides.conflict.reload"),
        tone: "warning",
      });
      if (!ok) return;
    }
    // 読み直す前に基準をそろえ、作り直しの間に離脱の確認が出ないようにする。
    setBaseline(guideFormSnapshot(form));
    await onReload();
    toast.success(t("supportGuides.reloaded"));
  };

  // 閉じるときは、未保存の変更を製品の離脱の確認（共通の文言）で確かめる。
  const close = async () => {
    if (!(await confirmLeave())) return;
    onClose();
  };

  const stepOptions = (self: StepForm): SearchableSelectOption[] => {
    const known = form.steps
      .filter((step) => step.key !== self.key && step.id.trim())
      .map((step) => ({ value: step.id, label: step.title || step.id, description: step.id }));
    const missing = self.dependsOn
      .filter((id) => !known.some((option) => option.value === id))
      .map((id) => ({ value: id, label: t("supportGuides.missingOption", { id }) }));
    return [...known, ...missing];
  };
  // 影響範囲・承認が係る手順（#1320）。消えた手順の id も選んだまま見せ、検証で知らせる。
  const impactStepOptions: SearchableSelectOption[] = [
    ...form.steps
      .filter((step) => step.id.trim())
      .map((step) => ({ value: step.id, label: step.title || step.id, description: step.id })),
    ...form.impactSteps
      .filter((id) => !form.steps.some((step) => step.id === id))
      .map((id) => ({ value: id, label: t("supportGuides.missingOption", { id }) })),
  ];
  const gotoOptions: SelectFieldOption[] = form.steps
    .filter((step) => step.id.trim())
    .map((step) => ({ value: step.id, label: step.title ? `${step.title}（${step.id}）` : step.id }));
  const conditionOptions: SelectFieldOption[] = form.conditions
    .filter((condition) => condition.id.trim())
    .map((condition) => ({
      value: condition.id,
      label: condition.label ? `${condition.label}（${condition.id}）` : condition.id,
    }));

  const errors = check?.issues.filter((issue) => issue.severity === "error") ?? [];
  const warnings = check?.issues.filter((issue) => issue.severity === "warning") ?? [];

  return (
    <section
      className="min-w-0 space-y-5 rounded-md border border-border p-3 sm:p-4"
      aria-labelledby="support-guide-editor-title"
      data-testid="support-guide-editor"
    >
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div className="min-w-0 space-y-1">
          <h4 id="support-guide-editor-title" className="break-words text-sm font-semibold text-fg">
            {detail
              ? t("supportGuides.editor.editTitle", { name: detail.title })
              : t("supportGuides.editor.newTitle")}
          </h4>
          {state ? (
            <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
              <StatusBadge
                variant={SUPPORT_GUIDE_STATE_VARIANT[state]}
                label={t(`supportGuides.state.${state}` as I18nKey)}
              />
              {detail?.published_revision != null ? (
                <span className="tnum">
                  {t("supportGuides.revisionLabel", { revision: detail.published_revision })}
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
        {detail ? (
          <ObjectActionBar
            ariaLabel={t("supportGuides.editor.actionsAria")}
            moreLabel={t("common.objectActions.more")}
            testId="support-guide-object-actions"
            actions={[
              {
                id: "archive",
                label: t("supportGuides.archive"),
                icon: Archive,
                visible: !archived,
                disabled: busy,
                loading: setArchived.isPending,
                onSelect: () => runArchive(true),
              },
              {
                id: "restore",
                label: t("supportGuides.restore"),
                icon: ArchiveRestore,
                visible: archived,
                disabled: busy,
                loading: setArchived.isPending,
                onSelect: () => runArchive(false),
              },
            ]}
          />
        ) : null}
      </div>

      {archived ? <Banner severity="info">{t("supportGuides.editor.archivedNotice")}</Banner> : null}

      {/* アーカイブしたガイドは読むだけ（戻すと編集・公開できる）。 */}
      <fieldset disabled={archived || busy} className="min-w-0 space-y-6">
        <FormSection title={t("supportGuides.section.basic")} help={t("supportGuides.section.basicHelp")}>
          <FormGrid>
            <TextField
              id={domId("title")}
              width="lg"
              label={t("supportGuides.field.title")}
              value={form.title}
              maxLength={200}
              required
              error={errorAt("title")}
              onChange={(event) => update({ title: event.target.value })}
            />
            <TextField
              id={domId("goal.expected_result")}
              className="col-span-full"
              width="full"
              label={t("supportGuides.field.expectedResult")}
              value={form.expectedResult}
              maxLength={2000}
              required
              error={errorAt("goal.expected_result")}
              onChange={(event) => update({ expectedResult: event.target.value })}
            />
            <div className="col-span-full">
              <TextareaField
                id={domId("description")}
                width="full"
                label={t("supportGuides.field.description")}
                value={form.description}
                maxLength={2000}
                rows={2}
                onChange={(event) => update({ description: event.target.value })}
              />
            </div>
            <TextareaField
              id={domId("goal.intent_examples")}
              className="col-span-full"
              width="full"
              label={t("supportGuides.field.intentExamples")}
              helper={t("supportGuides.field.goalHelp")}
              value={form.intentExamples}
              rows={3}
              onChange={(event) => update({ intentExamples: event.target.value })}
            />
            <TextareaField
              id={domId("goal.match_terms")}
              className="col-span-full"
              width="full"
              label={t("supportGuides.field.matchTerms")}
              helper={t("supportGuides.editor.linesHelp")}
              value={form.matchTerms}
              rows={3}
              onChange={(event) => update({ matchTerms: event.target.value })}
            />
          </FormGrid>
        </FormSection>

        <FormSection
          title={t("supportGuides.section.applicability")}
          help={t("supportGuides.section.applicabilityHelp")}
        >
          <FormGrid>
            {(
              [
                ["applicability.business_domains", "businessDomains", "supportGuides.field.businessDomains"],
                ["applicability.object_types", "objectTypes", "supportGuides.field.objectTypes"],
                ["applicability.versions", "versions", "supportGuides.field.versions"],
              ] as const
            ).map(([path, field, label]) => (
              <TextareaField
                key={path}
                id={domId(path)}
                className="col-span-full"
                width="full"
                label={t(label)}
                helper={t("supportGuides.editor.linesHelp")}
                value={form[field]}
                rows={2}
                onChange={(event) => update({ [field]: event.target.value })}
              />
            ))}
          </FormGrid>
          <FormGrid>
            <TextField
              id={domId("applicability.effective_from")}
              width="sm"
              type="date"
              label={t("supportGuides.field.effectiveFrom")}
              value={form.effectiveFrom}
              error={errorAt("applicability.effective_from")}
              onChange={(event) => update({ effectiveFrom: event.target.value })}
            />
            <TextField
              id={domId("applicability.effective_to")}
              width="sm"
              type="date"
              label={t("supportGuides.field.effectiveTo")}
              value={form.effectiveTo}
              error={errorAt("applicability.effective_to")}
              onChange={(event) => update({ effectiveTo: event.target.value })}
            />
          </FormGrid>
        </FormSection>

        <RowsSection
          section="conditions"
          title={t("supportGuides.section.conditions")}
          help={t("supportGuides.section.conditionsHelp")}
          rows={form.conditions}
          addLabel={t("supportGuides.add.condition")}
          onAdd={() => update({ conditions: [...form.conditions, newConditionRow(form)] })}
          rowTitle={(row) => row.label}
          onMove={(index, delta) => moveRow("conditions", index, delta)}
          onRemove={(index) => removeRow("conditions", index)}
          renderRow={(row: ConditionForm, index) => {
            const path = (field: string) => `conditions[${index}].${field}`;
            return (
              <FormGrid>
                <TextField
                  id={domId(path("id"))}
                  width="md"
                  label={t("supportGuides.field.id")}
                  value={row.id}
                  maxLength={64}
                  required
                  error={errorAt(path("id"))}
                  onChange={(event) => {
                    const next = event.target.value;
                    setForm((current) => ({
                      ...renameConditionId(current, row.id, next),
                      conditions: current.conditions.map((item, i) =>
                        i === index ? { ...item, id: next } : item,
                      ),
                    }));
                  }}
                />
                <TextField
                  id={domId(path("label"))}
                  width="md"
                  label={t("supportGuides.field.conditionLabel")}
                  value={row.label}
                  maxLength={200}
                  required
                  error={errorAt(path("label"))}
                  onChange={(event) => updateRow("conditions", index, { label: event.target.value })}
                />
                <SelectField<SupportGuideConditionType>
                  id={domId(path("type"))}
                  width="sm"
                  label={t("supportGuides.field.conditionType")}
                  value={row.type}
                  options={CONDITION_TYPE_OPTIONS}
                  onValueChange={(value) => updateRow("conditions", index, { type: value })}
                />
                {row.type === "enum" ? (
                  <>
                    <TextareaField
                      id={domId(path("allowed_values"))}
                      className="col-span-full"
                      width="full"
                      label={t("supportGuides.field.allowedValues")}
                      helper={t("supportGuides.field.allowedValuesHelp")}
                      value={row.allowedValues}
                      rows={3}
                      required
                      error={errorAt(path("allowed_values"))}
                      onChange={(event) =>
                        updateRow("conditions", index, { allowedValues: event.target.value })
                      }
                    />
                    <TextareaField
                      id={domId(path("value_aliases"))}
                      className="col-span-full"
                      width="full"
                      label={t("supportGuides.field.valueAliases")}
                      helper={t("supportGuides.field.valueAliasesHelp")}
                      value={row.valueAliases}
                      rows={2}
                      error={errorAt(path("value_aliases"))}
                      onChange={(event) =>
                        updateRow("conditions", index, { valueAliases: event.target.value })
                      }
                    />
                  </>
                ) : null}
                <SelectField<SupportGuideConditionSource>
                  id={domId(path("source"))}
                  width="sm"
                  label={t("supportGuides.field.source")}
                  value={row.source}
                  options={SOURCE_OPTIONS}
                  onValueChange={(value) => updateRow("conditions", index, { source: value })}
                />
                <SelectField<SupportGuideUnknownHandling>
                  id={domId(path("unknown_handling"))}
                  width="sm"
                  label={t("supportGuides.field.unknownHandling")}
                  value={row.unknownHandling}
                  options={UNKNOWN_OPTIONS}
                  onValueChange={(value) => updateRow("conditions", index, { unknownHandling: value })}
                />
                <div className="col-span-full">
                  <TextField
                    id={domId(path("question"))}
                    width="full"
                    label={t("supportGuides.field.question")}
                    helper={t("supportGuides.field.questionHelp")}
                    value={row.question}
                    maxLength={200}
                    required={row.unknownHandling === "ask" && row.source === "user"}
                    error={errorAt(path("question"))}
                    onChange={(event) => updateRow("conditions", index, { question: event.target.value })}
                  />
                </div>
                <SwitchRow
                  id={domId(path("required"))}
                  label={t("supportGuides.field.required")}
                  checked={row.required}
                  onCheckedChange={(checked) => updateRow("conditions", index, { required: checked })}
                />
              </FormGrid>
            );
          }}
        />

        <RowsSection
          section="steps"
          title={t("supportGuides.section.steps")}
          help={t("supportGuides.section.stepsHelp")}
          rows={form.steps}
          addLabel={t("supportGuides.add.step")}
          onAdd={() => update({ steps: [...form.steps, newStepRow(form)] })}
          rowTitle={(row) => row.title}
          onMove={(index, delta) => moveRow("steps", index, delta)}
          onRemove={(index) => removeRow("steps", index)}
          renderRow={(row: StepForm, index) => {
            const path = (field: string) => `steps[${index}].${field}`;
            return (
              <FormGrid>
                <TextField
                  id={domId(path("id"))}
                  width="md"
                  label={t("supportGuides.field.id")}
                  helper={t("supportGuides.editor.idHelp")}
                  value={row.id}
                  maxLength={64}
                  required
                  error={errorAt(path("id"))}
                  onChange={(event) => {
                    const next = event.target.value;
                    setForm((current) => {
                      const renamed = renameStepId(current, row.id, next);
                      return {
                        ...renamed,
                        steps: renamed.steps.map((item, i) => (i === index ? { ...item, id: next } : item)),
                      };
                    });
                  }}
                />
                <TextField
                  id={domId(path("title"))}
                  width="lg"
                  label={t("supportGuides.field.stepTitle")}
                  value={row.title}
                  maxLength={200}
                  required
                  error={errorAt(path("title"))}
                  onChange={(event) => updateRow("steps", index, { title: event.target.value })}
                />
                <div className="col-span-full">
                  <TextareaField
                    id={domId(path("purpose"))}
                    width="full"
                    label={t("supportGuides.field.purpose")}
                    value={row.purpose}
                    maxLength={2000}
                    rows={2}
                    onChange={(event) => updateRow("steps", index, { purpose: event.target.value })}
                  />
                </div>
                <SearchableMultiSelect
                  id={domId(path("depends_on"))}
                  width="lg"
                  label={t("supportGuides.field.dependsOn")}
                  options={stepOptions(row)}
                  value={row.dependsOn}
                  disabled={archived || busy}
                  onValueChange={(value) => updateRow("steps", index, { dependsOn: value })}
                />
                <Fieldset
                  legend={t("supportGuides.field.allowedTools")}
                  helper={t("supportGuides.field.allowedToolsHelp")}
                >
                  <div className="grid grid-cols-1 gap-1.5">
                    {SUPPORT_GUIDE_TOOLS.map((tool) => (
                      <label key={tool} className="flex cursor-pointer items-center gap-2 text-sm text-fg">
                        <input
                          type="checkbox"
                          className="h-4 w-4 rounded border-border accent-accent-emphasis"
                          checked={row.allowedTools.includes(tool)}
                          onChange={(event) =>
                            updateRow("steps", index, {
                              allowedTools: event.target.checked
                                ? [...row.allowedTools, tool]
                                : row.allowedTools.filter((item) => item !== tool),
                            })
                          }
                        />
                        {t(`supportGuides.tool.${tool}` as I18nKey)}
                      </label>
                    ))}
                  </div>
                </Fieldset>
                <TextareaField
                  id={domId(path("retrieval_hints"))}
                  className="col-span-full"
                  width="full"
                  label={t("supportGuides.field.retrievalHints")}
                  helper={t("supportGuides.editor.linesHelp")}
                  value={row.retrievalHints}
                  rows={2}
                  onChange={(event) => updateRow("steps", index, { retrievalHints: event.target.value })}
                />
                <TextareaField
                  id={domId(path("evidence_requirements"))}
                  className="col-span-full"
                  width="full"
                  label={t("supportGuides.field.evidenceRequirements")}
                  helper={t("supportGuides.editor.linesHelp")}
                  value={row.evidenceRequirements}
                  rows={2}
                  onChange={(event) =>
                    updateRow("steps", index, { evidenceRequirements: event.target.value })
                  }
                />
                <Fieldset
                  className="col-span-full"
                  legend={t("supportGuides.field.stepReferences")}
                  helper={t("supportGuides.field.stepReferencesHelp")}
                >
                  {row.references.length > 0 ? (
                    <ol className="space-y-2" data-testid={`support-guide-step-references-${index}`}>
                      {row.references.map((reference, refIndex) => {
                        const refPath = (field: string) => path(`references[${refIndex}].${field}`);
                        const updateReference = (patch: Partial<ReferenceForm>) =>
                          updateRow("steps", index, {
                            references: row.references.map((item, i) => (i === refIndex ? { ...item, ...patch } : item)),
                          });
                        const label = t("supportGuides.stepReference.row", { step: index + 1, n: refIndex + 1 });
                        return (
                          <li
                            key={reference.key}
                            aria-label={label}
                            className="min-w-0 rounded-md border border-border bg-surface p-3"
                          >
                            <div className="flex min-w-0 items-start gap-2">
                              <FormGrid className="min-w-0 flex-1">
                                <ReferenceDocumentField
                                  id={domId(refPath("document_id"))}
                                  reference={reference}
                                  error={errorAt(refPath("document_id"))}
                                />
                                <TextField
                                  id={domId(refPath("page_ranges"))}
                                  width="sm"
                                  label={t("supportGuides.field.pageRanges")}
                                  helper={t("supportGuides.field.pageRangesHelp")}
                                  value={reference.pageRanges}
                                  inputMode="numeric"
                                  error={errorAt(refPath("page_ranges"))}
                                  onChange={(event) => updateReference({ pageRanges: event.target.value })}
                                />
                                <TextareaField
                                  id={domId(refPath("sheet_ranges"))}
                                  className="col-span-full"
                                  width="full"
                                  label={t("supportGuides.field.sheetRanges")}
                                  helper={t("supportGuides.field.sheetRangesHelp")}
                                  value={reference.sheetRanges}
                                  rows={2}
                                  error={errorAt(refPath("sheet_ranges"))}
                                  onChange={(event) => updateReference({ sheetRanges: event.target.value })}
                                />
                              </FormGrid>
                              <Button
                                size="sm"
                                variant="ghost"
                                tone="danger"
                                iconOnly
                                icon={Trash2}
                                aria-label={t("supportGuides.stepReference.remove", { step: index + 1, n: refIndex + 1 })}
                                onClick={() =>
                                  updateRow("steps", index, {
                                    references: row.references.filter((_, i) => i !== refIndex),
                                  })
                                }
                              />
                            </div>
                          </li>
                        );
                      })}
                    </ol>
                  ) : null}
                  <Button
                    size="sm"
                    variant="secondary"
                    icon={Plus}
                    aria-expanded={picking === row.key}
                    aria-controls={picking === row.key ? `support-guide-step-reference-picker-${row.key}` : undefined}
                    disabled={row.references.length >= MAX_STEP_REFERENCES}
                    onClick={() => setPicking((current) => (current === row.key ? null : row.key))}
                  >
                    {t("supportGuides.add.stepReference")}
                  </Button>
                  {picking === row.key ? (
                    <SupportGuideReferencePicker
                      id={`support-guide-step-reference-picker-${row.key}`}
                      testId={`support-guide-step-reference-picker-${index}`}
                      searchAnswerProfileId={searchAnswerProfileId}
                      references={row.references}
                      maxSelection={MAX_STEP_REFERENCES - row.references.length}
                      onAdd={(references) => {
                        updateRow("steps", index, { references: [...row.references, ...references] });
                        setPicking(null);
                      }}
                      onClose={() => setPicking(null)}
                    />
                  ) : null}
                </Fieldset>
                <div className="col-span-full">
                  <TextareaField
                    id={domId(path("done_when"))}
                    width="full"
                    label={t("supportGuides.field.doneWhen")}
                    value={row.doneWhen}
                    maxLength={2000}
                    rows={2}
                    onChange={(event) => updateRow("steps", index, { doneWhen: event.target.value })}
                  />
                </div>
              </FormGrid>
            );
          }}
        />

        <RowsSection
          section="branches"
          title={t("supportGuides.section.branches")}
          help={t("supportGuides.section.branchesHelp")}
          rows={form.branches}
          addLabel={t("supportGuides.add.branch")}
          onAdd={() => update({ branches: [...form.branches, newBranchRow(form)] })}
          rowTitle={(row) => row.note}
          onMove={(index, delta) => moveRow("branches", index, delta)}
          onRemove={(index) => removeRow("branches", index)}
          renderRow={(row: BranchForm, index) => {
            const path = (field: string) => `branches[${index}].${field}`;
            const condition = form.conditions.find((item) => item.id === row.conditionId);
            const selectableValues = condition?.type === "enum" ? splitLines(condition.allowedValues) : condition?.type === "boolean" ? ["はい", "いいえ"] : null;
            const values = splitLines(row.values).map((value) => condition?.type === "boolean" ? booleanConditionValue(value) ?? value : value);
            return (
              <FormGrid>
                <TextField
                  id={domId(path("id"))}
                  width="md"
                  label={t("supportGuides.field.id")}
                  value={row.id}
                  maxLength={64}
                  required
                  error={errorAt(path("id"))}
                  onChange={(event) => updateRow("branches", index, { id: event.target.value })}
                />
                <SelectField
                  id={domId(path("when.condition_id"))}
                  width="md"
                  label={t("supportGuides.field.branchCondition")}
                  value={row.conditionId}
                  options={conditionOptions}
                  placeholder={t("supportGuides.select.choose")}
                  required
                  error={errorAt(path("when.condition_id"))}
                  onValueChange={(value) => updateRow("branches", index, { conditionId: value })}
                />
                <SelectField<SupportGuideBranchOperator>
                  id={domId(path("when.operator"))}
                  width="md"
                  label={t("supportGuides.field.operator")}
                  value={row.operator}
                  options={OPERATOR_OPTIONS}
                  onValueChange={(value) => updateRow("branches", index, { operator: value })}
                />
                {row.operator === "unknown" ? null : row.operator === "equals" && selectableValues ? (
                  <SelectField
                    id={domId(path("when.values"))}
                    width="md"
                    label={t("supportGuides.field.values")}
                    value={values[0] ?? ""}
                    options={selectableValues.map((value) => ({ value, label: value }))}
                    placeholder={t("supportGuides.select.choose")}
                    required
                    error={errorAt(path("when.values"))}
                    onValueChange={(value) => updateRow("branches", index, { values: value })}
                  />
                ) : row.operator === "in" && selectableValues ? (
                  <SearchableMultiSelect
                    id={domId(path("when.values"))}
                    width="lg"
                    label={t("supportGuides.field.values")}
                    options={selectableValues.map((value) => ({ value, label: value }))}
                    value={values}
                    required
                    error={errorAt(path("when.values"))}
                    disabled={archived || busy}
                    onValueChange={(value) => updateRow("branches", index, { values: value.join("\n") })}
                  />
                ) : row.operator === "equals" ? (
                  <TextField
                    id={domId(path("when.values"))}
                    width="md"
                    label={t("supportGuides.field.values")}
                    value={row.values}
                    required
                    error={errorAt(path("when.values"))}
                    onChange={(event) => updateRow("branches", index, { values: event.target.value })}
                  />
                ) : (
                  <TextareaField
                    id={domId(path("when.values"))}
                    className="col-span-full"
                    width="full"
                    label={t("supportGuides.field.valuesLines")}
                    value={row.values}
                    rows={2}
                    required
                    error={errorAt(path("when.values"))}
                    onChange={(event) => updateRow("branches", index, { values: event.target.value })}
                  />
                )}
                <SelectField
                  id={domId(path("goto_step"))}
                  width="md"
                  label={t("supportGuides.field.gotoStep")}
                  value={row.gotoStep}
                  options={gotoOptions}
                  placeholder={t("supportGuides.select.choose")}
                  required
                  error={errorAt(path("goto_step"))}
                  onValueChange={(value) => updateRow("branches", index, { gotoStep: value })}
                />
                <TextField
                  id={domId(path("note"))}
                  width="lg"
                  label={t("supportGuides.field.note")}
                  value={row.note}
                  maxLength={200}
                  onChange={(event) => updateRow("branches", index, { note: event.target.value })}
                />
              </FormGrid>
            );
          }}
        />

        <RowsSection
          section="references"
          title={t("supportGuides.section.references")}
          help={t("supportGuides.section.referencesHelp")}
          rows={form.references}
          adder={
            <>
              <Button
                size="sm"
                variant="secondary"
                icon={Plus}
                aria-expanded={picking === REFERENCES_PICKER}
                aria-controls={picking === REFERENCES_PICKER ? "support-guide-reference-picker" : undefined}
                disabled={form.references.length >= MAX_GUIDE_REFERENCES}
                onClick={() => setPicking((current) => (current === REFERENCES_PICKER ? null : REFERENCES_PICKER))}
              >
                {t("supportGuides.add.reference")}
              </Button>
              {picking === REFERENCES_PICKER ? (
                <SupportGuideReferencePicker
                  id="support-guide-reference-picker"
                  testId="support-guide-reference-picker"
                  searchAnswerProfileId={searchAnswerProfileId}
                  references={form.references}
                  maxSelection={MAX_GUIDE_REFERENCES - form.references.length}
                  onAdd={(references) => {
                    update({ references: [...form.references, ...references] });
                    setPicking(null);
                  }}
                  onClose={() => setPicking(null)}
                />
              ) : null}
            </>
          }
          rowTitle={(row) => row.title || row.documentId}
          onMove={(index, delta) => moveRow("references", index, delta)}
          onRemove={(index) => removeRow("references", index)}
          renderRow={(row: ReferenceForm, index) => {
            const path = (field: string) => `references[${index}].${field}`;
            return (
              <FormGrid>
                <ReferenceDocumentField
                  id={domId(path("document_id"))}
                  reference={row}
                  error={errorAt(path("document_id"))}
                />
                <TextareaField
                  id={domId(path("section_path"))}
                  className="col-span-full"
                  width="full"
                  label={t("supportGuides.field.sectionPath")}
                  value={row.sectionPath}
                  rows={2}
                  onChange={(event) => updateRow("references", index, { sectionPath: event.target.value })}
                />
                <TextField
                  id={domId(path("version"))}
                  width="sm"
                  label={t("supportGuides.field.version")}
                  value={row.version}
                  maxLength={64}
                  onChange={(event) => updateRow("references", index, { version: event.target.value })}
                />
                <TextField
                  id={domId(path("page_ranges"))}
                  width="sm"
                  label={t("supportGuides.field.pageRanges")}
                  helper={t("supportGuides.field.pageRangesHelp")}
                  value={row.pageRanges}
                  inputMode="numeric"
                  error={errorAt(path("page_ranges"))}
                  onChange={(event) => updateRow("references", index, { pageRanges: event.target.value })}
                />
                <TextareaField
                  id={domId(path("sheet_ranges"))}
                  className="col-span-full"
                  width="full"
                  label={t("supportGuides.field.sheetRanges")}
                  helper={t("supportGuides.field.sheetRangesHelp")}
                  value={row.sheetRanges}
                  rows={2}
                  error={errorAt(path("sheet_ranges"))}
                  onChange={(event) => updateRow("references", index, { sheetRanges: event.target.value })}
                />
              </FormGrid>
            );
          }}
        />

        <RowsSection
          section="completion"
          title={t("supportGuides.section.completion")}
          help={t("supportGuides.section.completionHelp")}
          rows={form.completion}
          addLabel={t("supportGuides.add.completion")}
          onAdd={() => update({ completion: [...form.completion, newCompletionRow(form)] })}
          rowTitle={(row) => row.description}
          onMove={(index, delta) => moveRow("completion", index, delta)}
          onRemove={(index) => removeRow("completion", index)}
          renderRow={(row: CompletionForm, index) => {
            const path = (field: string) => `completion[${index}].${field}`;
            return (
              <FormGrid>
                <TextField
                  id={domId(path("id"))}
                  width="md"
                  label={t("supportGuides.field.id")}
                  value={row.id}
                  maxLength={64}
                  required
                  error={errorAt(path("id"))}
                  onChange={(event) => updateRow("completion", index, { id: event.target.value })}
                />
                <TextField
                  id={domId(path("description"))}
                  className="col-span-full"
                  width="full"
                  label={t("supportGuides.field.completionDescription")}
                  value={row.description}
                  maxLength={2000}
                  required
                  error={errorAt(path("description"))}
                  onChange={(event) => updateRow("completion", index, { description: event.target.value })}
                />
                <div className="col-span-full">
                  <TextField
                    id={domId(path("check_method"))}
                    width="full"
                    label={t("supportGuides.field.checkMethod")}
                    value={row.checkMethod}
                    maxLength={2000}
                    onChange={(event) => updateRow("completion", index, { checkMethod: event.target.value })}
                  />
                </div>
              </FormGrid>
            );
          }}
        />

        <FormSection title={t("supportGuides.section.impact")} help={t("supportGuides.section.impactHelp")}>
          <FormGrid>
            <SelectField<SupportGuideImpactScope>
              id={domId("impact.scope")}
              width="sm"
              label={t("supportGuides.field.impactScope")}
              value={form.impactScope}
              options={SCOPE_OPTIONS}
              onValueChange={(value) => update({ impactScope: value })}
            />
            <SwitchRow
              id={domId("impact.approval_required")}
              label={t("supportGuides.field.approvalRequired")}
              checked={form.approvalRequired}
              onCheckedChange={(checked) => update({ approvalRequired: checked })}
            />
            <div className="col-span-full">
              <TextareaField
                id={domId("impact.approval_note")}
                width="full"
                label={t("supportGuides.field.approvalNote")}
                value={form.approvalNote}
                maxLength={2000}
                rows={2}
                onChange={(event) => update({ approvalNote: event.target.value })}
              />
            </div>
            <div className="col-span-full">
              <SearchableMultiSelect
                id={domId("impact.steps")}
                width="lg"
                label={t("supportGuides.field.impactSteps")}
                helper={t("supportGuides.field.impactStepsHelp")}
                options={impactStepOptions}
                value={form.impactSteps}
                error={errorAt("impact.steps")}
                disabled={archived || busy}
                onValueChange={(value) => update({ impactSteps: value })}
              />
            </div>
            <TextareaField
              id={domId("handoff.conditions")}
              className="col-span-full"
              width="full"
              label={t("supportGuides.field.handoffConditions")}
              helper={t("supportGuides.editor.linesHelp")}
              value={form.handoffConditions}
              rows={3}
              onChange={(event) => update({ handoffConditions: event.target.value })}
            />
            {/* 空のときに継承する窓口と、引き継ぎのときの有効な窓口を示す（#2683）。 */}
            <SupportGuideHandoffContactField
              id={domId("handoff.contact")}
              searchAnswerProfileId={searchAnswerProfileId}
              value={form.handoffContact}
              onChange={(value) => update({ handoffContact: value })}
            />
          </FormGrid>
        </FormSection>
      </fieldset>

      {/* カード末尾の操作行（README §4。主操作を先頭に左寄せ、640px 未満は縦に並べる）。 */}
      <div className="space-y-3 border-t border-border pt-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
          <Button
            size="sm"
            icon={Save}
            className="w-full sm:w-auto"
            loading={save.isPending}
            disabled={archived || (busy && !save.isPending)}
            onClick={runSave}
          >
            {t("supportGuides.save")}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            icon={CheckCheck}
            className="w-full sm:w-auto"
            loading={validate.isPending}
            disabled={!detail || dirty || archived || (busy && !validate.isPending)}
            onClick={runValidate}
          >
            {t("supportGuides.validate")}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            icon={Send}
            className="w-full sm:w-auto"
            loading={publish.isPending}
            disabled={
              !detail ||
              dirty ||
              archived ||
              !detail.has_unpublished_changes ||
              (busy && !publish.isPending)
            }
            onClick={() => void runPublish()}
          >
            {t("supportGuides.publish")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            icon={X}
            className="w-full sm:ml-auto sm:w-auto"
            disabled={busy}
            onClick={() => void close()}
          >
            {t("supportGuides.close")}
          </Button>
        </div>
        {detail && dirty && !archived ? (
          <p className="text-xs text-fg-muted" data-testid="support-guide-save-first">
            {t("supportGuides.saveFirst")}
          </p>
        ) : detail && !dirty && !archived && !detail.has_unpublished_changes ? (
          <p className="text-xs text-fg-muted">{t("supportGuides.upToDate")}</p>
        ) : null}

        {/* 操作の結果は操作行の直下に出し、次の操作まで残す（UX 契約 messaging.md §10）。 */}
        <div aria-live="polite" className="space-y-3" data-testid="support-guide-result">
          {clientIssues.length > 0 ? (
            <Banner severity="danger" title={t("supportGuides.result.blocked", { count: clientIssues.length })}>
              <SupportGuideIssueList issues={clientIssues} testId="support-guide-client-issues" />
            </Banner>
          ) : null}
          {failure?.kind === "conflict" ? (
            <Banner
              severity="warning"
              title={t("supportGuides.conflict.title")}
              action={
                <Button size="sm" variant="secondary" icon={RefreshCw} onClick={() => void reloadLatest()}>
                  {t("supportGuides.conflict.reload")}
                </Button>
              }
            >
              {failure.message}
            </Banner>
          ) : failure?.kind === "refused" ? (
            <Banner severity="danger" title={failure.message}>
              <ul className="list-disc space-y-1 pl-5" data-testid="support-guide-refused-issues">
                {failure.details.map((detailText, index) => (
                  <li key={`${index}-${detailText}`} className="break-words">
                    {detailText}
                  </li>
                ))}
              </ul>
            </Banner>
          ) : failure ? (
            <div className="space-y-1">
              <FormStatus tone="danger" message={failure.message} />
              {failure.details.length > 0 ? (
                <ul className="list-disc space-y-1 pl-5 text-sm text-fg">
                  {failure.details.map((detailText, index) => (
                    <li key={`${index}-${detailText}`} className="break-words">
                      {detailText}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
          {check ? (
            <div className="space-y-3" data-testid="support-guide-check">
              {check.source === "validated" && check.issues.length === 0 ? (
                <Banner severity="success">{t("supportGuides.result.valid")}</Banner>
              ) : null}
              {errors.length > 0 ? (
                <Banner severity="danger" title={t("supportGuides.result.errors", { count: errors.length })}>
                  <SupportGuideIssueList issues={errors} testId="support-guide-errors" />
                </Banner>
              ) : null}
              {warnings.length > 0 ? (
                <Banner
                  severity="warning"
                  title={t("supportGuides.result.warnings", { count: warnings.length })}
                >
                  <SupportGuideIssueList issues={warnings} testId="support-guide-warnings" />
                </Banner>
              ) : null}
              {check.issues.length > 0 ? (
                <p className="text-xs text-fg-muted">
                  {t(
                    check.source === "validated"
                      ? "supportGuides.result.validatedAt"
                      : "supportGuides.result.savedCheck",
                  )}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>

      {/* 下書きで試す（#1288）。保存した下書きで回答を作る（公開の版は変えない）。 */}
      {detail && !archived ? (
        <SupportGuideDraftTry
          searchAnswerProfileId={searchAnswerProfileId}
          detail={detail}
          dirty={dirty}
          disabled={busy}
        />
      ) : null}

      {detail ? (
        <SupportGuideHistory
          searchAnswerProfileId={searchAnswerProfileId}
          detail={detail}
          viewRevision={viewRevision}
          onView={setViewRevision}
          onRollback={(revision) => void runRollback(revision)}
          rollbackPending={rollback.isPending ? (rollback.variables?.revision ?? null) : null}
          disabled={busy || archived}
        />
      ) : null}
    </section>
  );
}

function FormSection({ title, help, children }: { title: string; help: string; children: ReactNode }) {
  return (
    <section className="min-w-0 space-y-3">
      <div className="space-y-0.5">
        <h5 className="text-sm font-semibold text-fg">{title}</h5>
        <p className="text-xs leading-relaxed text-fg-muted">{help}</p>
      </div>
      {children}
    </section>
  );
}

function SwitchRow({
  id,
  label,
  checked,
  onCheckedChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 self-end sm:justify-start">
      <span id={`${id}-label`} className="text-sm text-fg">
        {label}
      </span>
      <Switch id={id} checked={checked} aria-labelledby={`${id}-label`} onCheckedChange={onCheckedChange} />
    </div>
  );
}

/** 資料の文書（選んだ文書の表示名と ID を読み取り専用で出す。文書は「資料を足す」の選択で入れる。#2749）。 */
function ReferenceDocumentField({
  id,
  reference,
  error,
}: {
  id: string;
  reference: ReferenceForm;
  error?: string;
}) {
  return (
    <TextField
      id={id}
      width="lg"
      label={t("supportGuides.field.referenceDocument")}
      value={reference.title || reference.documentId}
      helper={t("supportGuides.field.referenceDocumentId", { id: reference.documentId })}
      readOnly
      error={error}
    />
  );
}

function RowsSection<R extends { key: string }>({
  section,
  title,
  help,
  rows,
  addLabel,
  onAdd,
  adder,
  rowTitle,
  onMove,
  onRemove,
  renderRow,
}: {
  section: GuideRowSection;
  title: string;
  help: string;
  rows: readonly R[];
  /** 行を足す操作。`adder` を渡したときは、ボタンの代わりにそれを出す（資料は文書を選んで足す）。 */
  addLabel?: string;
  onAdd?: () => void;
  adder?: ReactNode;
  rowTitle: (row: R) => string;
  onMove: (index: number, delta: number) => void;
  onRemove: (index: number) => void;
  renderRow: (row: R, index: number) => ReactNode;
}) {
  return (
    <FormSection title={title} help={help}>
      {rows.length === 0 ? <p className="text-xs text-fg-muted">{t("supportGuides.row.empty")}</p> : null}
      <ol className="space-y-3" data-testid={`support-guide-${section}`}>
        {rows.map((row, index) => {
          const name = t(ROW_LABEL[section], { n: index + 1 });
          const subtitle = rowTitle(row).trim();
          return (
            <li
              key={row.key}
              className="min-w-0 space-y-3 rounded-md border border-border bg-surface-sunken p-3"
              aria-label={name}
            >
              <div className="flex min-w-0 items-center gap-2">
                <p className="min-w-0 flex-1 truncate text-sm font-medium text-fg">
                  {name}
                  {subtitle ? <span className="font-normal text-fg-muted">「{subtitle}」</span> : null}
                </p>
                <Button
                  size="sm"
                  variant="ghost"
                  iconOnly
                  icon={ArrowUp}
                  aria-label={t("supportGuides.row.moveUp", { name })}
                  disabled={index === 0}
                  onClick={() => onMove(index, -1)}
                />
                <Button
                  size="sm"
                  variant="ghost"
                  iconOnly
                  icon={ArrowDown}
                  aria-label={t("supportGuides.row.moveDown", { name })}
                  disabled={index === rows.length - 1}
                  onClick={() => onMove(index, 1)}
                />
                <Button
                  size="sm"
                  variant="ghost"
                  tone="danger"
                  iconOnly
                  icon={Trash2}
                  aria-label={t("supportGuides.row.remove", { name })}
                  onClick={() => onRemove(index)}
                />
              </div>
              {renderRow(row, index)}
            </li>
          );
        })}
      </ol>
      {adder ?? (
        <Button size="sm" variant="secondary" icon={Plus} onClick={onAdd}>
          {addLabel}
        </Button>
      )}
    </FormSection>
  );
}

function SupportGuideHistory({
  searchAnswerProfileId,
  detail,
  viewRevision,
  onView,
  onRollback,
  rollbackPending,
  disabled,
}: {
  searchAnswerProfileId: string;
  detail: SupportGuideDetail;
  viewRevision: number | null;
  onView: (revision: number | null) => void;
  onRollback: (revision: number) => void;
  rollbackPending: number | null;
  disabled: boolean;
}) {
  const revision = useSupportGuideRevision(searchAnswerProfileId, detail.guide_id, viewRevision);
  const revisionError = initialLoadError(revision);
  // ガイドごとに 1 ページ目から。ページと表示件数は作業状態に残す（#1891）。
  const paging = useClientListPaging("supportGuides.revisionsPaging", `${searchAnswerProfileId}:${detail.guide_id}`);
  return (
    <section className="min-w-0 space-y-3 border-t border-border pt-4" aria-labelledby="support-guide-history">
      <div className="space-y-0.5">
        <h5 id="support-guide-history" className="text-sm font-semibold text-fg">
          {t("supportGuides.history.title")}
        </h5>
        <p className="text-xs leading-relaxed text-fg-muted">
          {t("supportGuides.history.hint")}
          {/* 公開の履歴は直近 N 版（RAG_SUPPORT_GUIDE_MAX_PUBLISHED_REVISIONS。公開中の版は常に残る。#2356）。 */}
          {detail.max_published_revisions
            ? t("supportGuides.history.retention", { count: detail.max_published_revisions })
            : null}
        </p>
      </div>
      {/* 件数は一覧の上の右に 1 か所（ListCount。#1891）。 */}
      <ListToolbar
        count={listCountProps({ matchedTotal: detail.revisions.length, filtered: false, live: true })}
        testId="support-guide-history-toolbar"
      />
      <PagedDataTable<SupportGuideRevisionSummary>
        paginationLabels={paginationLabels()}
        {...paging}
        pageSizeOptions={PAGE_SIZE_OPTIONS}
        paginationAriaLabel={t("supportGuides.history.paginationLabel")}
        // 列の並びとバッジは Agent の業務 Agent の版の履歴と同じ共通の列（#2356）。
        columns={publishedVersionColumns<SupportGuideRevisionSummary>({
          getVersion: (row) => row.revision,
          currentVersion: detail.published_revision,
          formatVersion: (revision) => t("supportGuides.revisionLabel", { revision }),
          labels: {
            version: t("supportGuides.history.revision"),
            current: t("supportGuides.history.current"),
            publishedAt: t("supportGuides.history.publishedAt"),
            publishedBy: t("supportGuides.history.publishedBy"),
            note: t("supportGuides.history.note"),
            actions: t("supportGuides.history.actions"),
          },
          renderPublishedAt: (row) => <span className="text-xs">{formatDateTime(row.published_at)}</span>,
          renderPublishedBy: (row) => <span className="text-xs">{row.published_by || "-"}</span>,
          extraColumns: [
            {
              key: "title",
              header: t("supportGuides.column.title"),
              render: (row) => <span className="break-words">{row.title}</span>,
            },
          ],
          renderNote: (row) =>
            row.rollback_from != null ? (
              <span className="text-xs">{t("supportGuides.history.rollbackFrom", { revision: row.rollback_from })}</span>
            ) : null,
          renderActions: (row) => (
            <RowActionMenu
              actions={[
                {
                  id: "view",
                  label: t("supportGuides.history.view"),
                  icon: Eye,
                  onSelect: () => onView(row.revision),
                },
                {
                  id: "rollback",
                  label: t("supportGuides.history.rollback"),
                  icon: RotateCcw,
                  visible: row.revision !== detail.published_revision,
                  disabled,
                  loading: rollbackPending === row.revision,
                  onSelect: () => onRollback(row.revision),
                },
              ]}
              ariaLabel={t("common.objectActions.aria", {
                name: t("supportGuides.revisionLabel", { revision: row.revision }),
              })}
              loading={rollbackPending === row.revision}
              testId={`support-guide-revision-actions-${row.revision}`}
            />
          ),
        })}
        rows={detail.revisions}
        getRowKey={(row) => String(row.revision)}
        selectedRowKey={viewRevision == null ? null : String(viewRevision)}
        dense
        empty={<EmptyState title={t("supportGuides.history.empty")} />}
        ariaLabel={t("supportGuides.history.aria")}
        scrollAriaLabel={t("supportGuides.history.scrollLabel")}
        scrollTestId="support-guide-history-scroll-region"
        paginationTestId="support-guide-history-pagination"
      />
      {viewRevision != null ? (
        <section
          className="min-w-0 space-y-3 rounded-md border border-border p-3"
          aria-labelledby="support-guide-revision-view"
          data-testid="support-guide-revision-view"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h6 id="support-guide-revision-view" className="text-sm font-semibold text-fg">
              {t("supportGuides.history.viewTitle", { revision: viewRevision })}
            </h6>
            <div className="flex flex-wrap gap-2">
              {viewRevision !== detail.published_revision ? (
                <Button
                  size="sm"
                  variant="secondary"
                  icon={RotateCcw}
                  disabled={disabled}
                  loading={rollbackPending === viewRevision}
                  onClick={() => onRollback(viewRevision)}
                >
                  {t("supportGuides.history.rollback")}
                </Button>
              ) : null}
              <Button size="sm" variant="ghost" icon={X} onClick={() => onView(null)}>
                {t("supportGuides.history.closeView")}
              </Button>
            </div>
          </div>
          {revision.isPending ? (
            <TimedLoadingState
              label={t("supportGuides.history.viewLoading")}
              operationKey={`support-guide-revision-${viewRevision}`}
              testId="support-guide-revision-loading"
            >
              <TableSkeleton columns={2} rows={{ base: 3, md: 4 }} />
            </TimedLoadingState>
          ) : revisionError || !revision.data ? (
            <ErrorState
              message={errorMessage(revisionError, t("supportGuides.history.viewError"))}
              onRetry={() => void revision.refetch()}
            />
          ) : (
            <SupportGuideContentView content={revision.data.content} />
          )}
        </section>
      ) : null}
    </section>
  );
}
