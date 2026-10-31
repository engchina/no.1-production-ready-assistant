import { Save, Search, Trash2, X } from "lucide-react";
import { RuleClarificationEditor, storedClarification } from "./RuleClarificationEditor";
import { useState } from "react";

import {
  Button,
  EmptyState,
  FormGrid,
  FormStatus,
  ListToolbar,
  PAGE_SIZE_OPTIONS,
  RowTitleButton,
  SelectField,
  StatusBadge,
  Switch,
  TableSkeleton,
  TextareaField,
  TextField,
  TimedLoadingState,
  toast,
  useConfirm,
  PagedDataTable,
} from "@production-ready/ui";

import {
  api,
  ApiError,
  type JsonValue,
  type RuntimeKnowledgeKind,
  type RuntimeKnowledgePreviewData,
} from "@/lib/api";
import { listCountProps, useClientListPaging } from "@/lib/list-paging";
import { paginationLabels } from "@/lib/pagination-labels";
import { ErrorState } from "@/components/StateViews";
import { t } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { focusFirstInvalidField, requiredTextError } from "@/lib/required-fields";
import { initialLoadError, useEditRuntimeKnowledge, useRuntimeKnowledge } from "@/lib/queries";

type Row = Record<string, JsonValue>;

type FormState = {
  kind: RuntimeKnowledgeKind;
  selected: string | null;
  name: string;
  title: string;
  labels: string;
  content: string;
  enabled: boolean;
  purpose?: "confirmation" | "generation" | "both";
  instructionKind?: "presentation" | "requirement" | "verification";
  exclusiveGroup?: string;
  priority?: number;
};

function emptyForm(kind: RuntimeKnowledgeKind): FormState {
  return { kind, selected: null, name: "", title: "", labels: "", content: "", enabled: true, purpose: "both", instructionKind: "requirement", exclusiveGroup: "", priority: 0 };
}

/** 種類の表示名（タブ・通知の文言に使う。#682）。 */
export function runtimeKindLabel(kind: RuntimeKnowledgeKind): string {
  return t(kind === "terms" ? "searchAnswerProfiles.runtime.kind.terms" : "searchAnswerProfiles.runtime.kind.rules");
}

const ACTIVE_STATUSES = new Set(["", "approved", "stale_review_needed"]);

function text(value: JsonValue | undefined): string {
  return typeof value === "string" ? value : "";
}

function list(value: JsonValue | undefined): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

/** 入力値と編集対象を比べて dirty を判定する。 */
function editableSnapshot({ selected, name, title, labels, content, enabled, purpose, instructionKind, exclusiveGroup, priority }: FormState) {
  return JSON.stringify([selected, name, title, labels, content, enabled, purpose, instructionKind, exclusiveGroup, priority]);
}

function formFromRow(kind: RuntimeKnowledgeKind, row: Row): FormState {
  const identity = kind === "terms" ? text(row.term) : text(row.id);
  return {
    kind,
    selected: identity,
    name: identity,
    title: text(row.title),
    labels: list(kind === "terms" ? row.aliases : row.triggers).join("\n"),
    content: text(kind === "terms" ? row.description : row.content),
    enabled: ACTIVE_STATUSES.has(text(row.status)),
    purpose: text(row.purpose) === "confirmation" ? "confirmation" : text(row.purpose) === "generation" ? "generation" : "both",
    instructionKind: text(row.instruction_kind) === "presentation" ? "presentation" : text(row.instruction_kind) === "verification" ? "verification" : "requirement",
    exclusiveGroup: text(row.exclusive_group),
    priority: typeof row.priority === "number" ? row.priority : 0,
  };
}

function rowKey(kind: RuntimeKnowledgeKind, row: Row): string {
  return kind === "terms" ? text(row.term) : text(row.id);
}

function rowName(kind: RuntimeKnowledgeKind, row: Row): string {
  return kind === "terms" ? text(row.term) : text(row.title) || text(row.id);
}

type RequiredField = "name" | "title" | "labels" | "content";
type RequiredErrors = Partial<Record<RequiredField, string | null>>;

const FIELD_IDS: Record<RequiredField, string> = {
  name: "runtime-knowledge-name",
  title: "runtime-knowledge-title",
  labels: "runtime-knowledge-labels",
  content: "runtime-knowledge-content",
};

/**
 * 保存前の必須の検証（#540）。規則と文言は backend（rag_engine の edit_knowledge）と同じ:
 * 用語は「用語」、ルールは「ルール名」「照合キーワード」「ルール内容」が必須。
 */
export function runtimeKnowledgeRequiredErrors(form: FormState): RequiredErrors {
  if (form.kind === "terms") {
    return { name: requiredTextError(form.name, t("searchAnswerProfiles.runtime.error.termRequired")) };
  }
  return {
    labels: requiredTextError(
      form.labels.split(/[\n,、，]/).map((value) => value.trim()).filter(Boolean).join(""),
      t("searchAnswerProfiles.runtime.error.triggersRequired"),
    ),
    title: requiredTextError(form.title, t("searchAnswerProfiles.runtime.error.ruleTitleRequired")),
    content: form.purpose === "confirmation" ? null : requiredTextError(form.content, t("searchAnswerProfiles.runtime.error.ruleContentRequired")),
  };
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof ApiError ? error.message : fallback;
}

/**
 * 検索・回答プロファイルの用語・同義語（同義語・説明）または回答ルール（照合キーワード・内容）。回答フローで使う。
 * 種類ごとにタブを分け、表・追加 / 編集のフォーム・照合テストはその種類だけを扱う（#682）。
 */
export function RuntimeKnowledgeManager({
  searchAnswerProfileId,
  kind,
}: {
  searchAnswerProfileId: string;
  kind: RuntimeKnowledgeKind;
}) {
  const query = useRuntimeKnowledge(searchAnswerProfileId);
  const save = useEditRuntimeKnowledge(searchAnswerProfileId);
  const confirm = useConfirm();
  const [form, setForm] = useState<FormState>(() => emptyForm(kind));
  // 読み込んだ行（または空の新規）を基準に、未保存の入力だけを離脱ガードの対象にする。
  const [baseline, setBaseline] = useState<FormState>(() => emptyForm(kind));
  const kindLabel = runtimeKindLabel(kind);
  const enabledLabel = t(kind === "rules" ? "searchAnswerProfiles.runtime.ruleEnabledLabel" : "searchAnswerProfiles.runtime.enabledLabel");
  const load = (next: FormState) => {
    setForm(next);
    setBaseline(next);
    setErrors({});
  };
  useLeaveGuard(editableSnapshot(form) !== editableSnapshot(baseline));
  const [question, setQuestion] = useState("");
  const [preview, setPreview] = useState<RuntimeKnowledgePreviewData | null>(
    null,
  );
  const [previewing, setPreviewing] = useState(false);
  const [errors, setErrors] = useState<RequiredErrors>({});
  const update = (patch: Partial<FormState>) => {
    setForm((current) => ({ ...current, ...patch }));
    // 直した欄のエラーだけを消す。
    setErrors((current) =>
      Object.fromEntries(Object.entries(current).filter(([field]) => !(field in patch))),
    );
  };

  // 保存のボタンは押せる状態のまま、未入力は押したときに欄の直下へ出す（UX 契約 messaging.md §3.2.1。#541）。
  const saveForm = () => {
    const nextErrors = runtimeKnowledgeRequiredErrors(form);
    setErrors(nextErrors);
    const order: RequiredField[] = ["name", "title", "labels", "content"];
    if (focusFirstInvalidField(order.map((field) => [FIELD_IDS[field], nextErrors[field]] as const))) return;
    submit();
  };

  const submit = (remove = false) =>
    save.mutate(
      {
        kind,
        selected: form.selected,
        name: form.name.trim(),
        title: form.title.trim(),
        labels: form.labels,
        content: form.content,
        ...(kind === "rules" ? { purpose: form.purpose ?? "both", instruction_kind: form.instructionKind ?? "requirement", exclusive_group: form.exclusiveGroup ?? "", priority: form.priority ?? 0 } : {}),
        enabled: form.enabled,
        delete: remove,
      },
      {
        onSuccess: () => {
          load(emptyForm(kind));
          toast.success(
            t(remove ? "searchAnswerProfiles.runtime.deleted" : "searchAnswerProfiles.runtime.saved", {
              kind: kindLabel,
            }),
          );
        },
        onError: (error) =>
          toast.error(
            errorMessage(error, t("searchAnswerProfiles.runtime.saveError", { kind: kindLabel })),
          ),
      },
    );

  // variables は完了後も残るため、isPending のときだけ読む。
  const deletePending = save.isPending && save.variables?.delete === true;
  const savePending = save.isPending && !deletePending;

  const confirmDelete = async () => {
    const ok = await confirm({
      title: t("searchAnswerProfiles.runtime.deleteConfirm.title", { kind: kindLabel }),
      description: t("searchAnswerProfiles.runtime.deleteConfirm.description", {
        name: kind === "rules" ? form.title : form.selected ?? "",
      }),
      confirmLabel: t("searchAnswerProfiles.faq.delete"),
      tone: "danger",
      dismissOnOverlay: false,
    });
    if (ok) submit(true);
  };

  const runPreview = async () => {
    setPreviewing(true);
    try {
      setPreview(
        await api.previewRuntimeKnowledge(searchAnswerProfileId, question.trim()),
      );
    } catch (error) {
      toast.error(errorMessage(error, t("searchAnswerProfiles.runtime.previewError")));
    } finally {
      setPreviewing(false);
    }
  };

  const rows = (kind === "terms" ? query.data?.terms : query.data?.rules) ?? [];
  const loadError = initialLoadError(query);
  const paging = useClientListPaging("runtimeKnowledge.listPaging", `${searchAnswerProfileId}:${kind}`);
  const table = query.isPending ? (
    <TimedLoadingState
      label={t("searchAnswerProfiles.runtime.loading")}
      operationKey={`runtime-knowledge-${searchAnswerProfileId}`}
      testId="runtime-knowledge-loading"
    >
      <TableSkeleton columns={3} rows={{ base: 3, md: 5 }} />
    </TimedLoadingState>
  ) : loadError ? (
    // 初回の読み込みの失敗は、空（0 件）と見せずに失敗と再試行を出す。
    <ErrorState
      message={errorMessage(loadError, t("searchAnswerProfiles.runtime.loadError"))}
      onRetry={() => void query.refetch()}
    />
  ) : (
    <PagedDataTable<Row>
      showSinglePagePagination
      paginationLabels={paginationLabels()}
      {...paging}
      pageSizeOptions={PAGE_SIZE_OPTIONS}
      paginationAriaLabel={t(
        kind === "terms"
          ? "searchAnswerProfiles.runtime.termsPaginationLabel"
          : "searchAnswerProfiles.runtime.rulesPaginationLabel",
      )}
      columns={[
        {
          key: "name",
          header: t(
            kind === "terms"
              ? "searchAnswerProfiles.runtime.term"
              : "searchAnswerProfiles.runtime.ruleTitle",
          ),
          rowHeader: true,
          // 名前のボタンと行のクリックで編集フォームへ読み込む（page-archetypes.md §0-7）。
          render: (row) => (
            <RowTitleButton
              title={rowName(kind, row)}
              current={form.selected === rowKey(kind, row)}
              aria-label={t("searchAnswerProfiles.runtime.editNamed", {
                name: rowName(kind, row),
              })}
              onClick={() => load(formFromRow(kind, row))}
            />
          ),
        },
        {
          key: "labels",
          header: t(
            kind === "terms"
              ? "searchAnswerProfiles.runtime.aliases"
              : "searchAnswerProfiles.runtime.triggers",
          ),
          render: (row) => (
            <span className="break-words">
              {list(kind === "terms" ? row.aliases : row.triggers).join("、")}
            </span>
          ),
        },
        {
          key: "status",
          header: t("searchAnswerProfiles.runtime.status"),
          render: (row) =>
            ACTIVE_STATUSES.has(text(row.status)) ? (
              <StatusBadge
                variant="success"
                label={t("searchAnswerProfiles.runtime.enabled")}
              />
            ) : (
              <StatusBadge
                variant="neutral"
                label={t("searchAnswerProfiles.runtime.disabled")}
              />
            ),
        },
      ]}
      rows={rows}
      getRowKey={(row) => rowKey(kind, row)}
      onRowClick={(row) => load(formFromRow(kind, row))}
      selectedRowKey={form.selected}
      dense
      empty={<EmptyState title={t("searchAnswerProfiles.runtime.empty")} />}
      scrollAriaLabel={t(
        kind === "terms"
          ? "searchAnswerProfiles.runtime.termsScrollLabel"
          : "searchAnswerProfiles.runtime.rulesScrollLabel",
      )}
      scrollTestId={`runtime-knowledge-${kind}-scroll-region`}
      paginationTestId={`runtime-knowledge-${kind}-pagination`}
    />
  );

  return (
    <div className="space-y-5">
      <p className="text-xs leading-relaxed text-fg-muted">
        {t(kind === "terms" ? "searchAnswerProfiles.runtime.terms.hint" : "searchAnswerProfiles.runtime.rules.hint")}
      </p>
      {/* 件数は一覧の上の右に 1 か所（ListCount。#1891）。 */}
      <ListToolbar
        count={query.data ? listCountProps({ matchedTotal: rows.length, filtered: false, live: true }) : undefined}
        testId={`runtime-knowledge-${kind}-toolbar`}
      />
      {table}

      <div className="grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="min-w-0 space-y-3 rounded-md border border-border p-3">
          <h4 className="text-sm font-semibold text-fg">
            {form.selected
              ? t("searchAnswerProfiles.runtime.editTitle", { name: kind === "rules" ? form.title : form.selected })
              : t("searchAnswerProfiles.runtime.addTitle", { kind: kindLabel })}
          </h4>
          {/* 編集の欄（#1891）: 用語、ルール名は名前の幅（md）、別名・内容は文章（全幅）。 */}
          <FormGrid columns={1}>
          {kind === "terms" ? (
            <TextField
            id="runtime-knowledge-name"
            width="md"
            label={t("searchAnswerProfiles.runtime.term")}
            value={form.name}
            maxLength={160}
            onChange={(event) => update({ name: event.target.value })}
            error={errors.name ?? undefined}
            required
            />
          ) : null}
          {kind === "rules" ? (
            <TextField
              id="runtime-knowledge-title"
              width="md"
              label={t("searchAnswerProfiles.runtime.ruleTitle")}
              helper={t("searchAnswerProfiles.runtime.ruleTitleHelp")}
              value={form.title}
              maxLength={160}
              onChange={(event) => update({ title: event.target.value })}
              error={errors.title ?? undefined}
              required
            />
          ) : null}
          {kind === "rules" ? (
            <>
              <SelectField
                id="runtime-knowledge-purpose"
                width="md"
                label={t("searchAnswerProfiles.runtime.purpose")}
                helper={t("searchAnswerProfiles.runtime.purposeHelp")}
                value={form.purpose ?? "both"}
                options={(["confirmation", "generation", "both"] as const).map((value) => ({ value, label: t(`searchAnswerProfiles.runtime.purpose.${value}`) }))}
                onValueChange={(purpose) => update({ purpose })}
              />
              <SelectField
                id="runtime-knowledge-instruction-kind"
                width="md"
                label={t("searchAnswerProfiles.runtime.instructionKind")}
                helper={t("searchAnswerProfiles.runtime.instructionKindHelp")}
                value={form.instructionKind ?? "requirement"}
                options={(["presentation", "requirement", "verification"] as const).map((value) => ({ value, label: t(`searchAnswerProfiles.runtime.instructionKind.${value}`) }))}
                onValueChange={(instructionKind) => update({ instructionKind })}
                disabled={form.purpose === "confirmation"}
              />
              <TextField
                id="runtime-knowledge-exclusive-group"
                width="md"
                label={t("searchAnswerProfiles.runtime.exclusiveGroup")}
                helper={t("searchAnswerProfiles.runtime.exclusiveGroupHelp")}
                value={form.exclusiveGroup ?? ""}
                maxLength={160}
                onChange={(event) => update({ exclusiveGroup: event.target.value })}
              />
              <TextField
                id="runtime-knowledge-priority"
                width="xs"
                type="number"
                min={0}
                max={100}
                step={1}
                label={t("searchAnswerProfiles.runtime.priority")}
                helper={t("searchAnswerProfiles.runtime.priorityHelp")}
                value={form.priority ?? 0}
                onChange={(event) => update({ priority: Number(event.target.value) })}
              />
            </>
          ) : null}
          {(["labels", "content"] as const).map((field) => {
            const required = kind === "rules" && (field === "labels" || form.purpose !== "confirmation");
            const error = errors[field];
            return (
              <TextareaField
                key={field}
                id={`runtime-knowledge-${field}`}
                required={required}
                label={t(
                  field === "labels"
                    ? kind === "terms"
                      ? "searchAnswerProfiles.runtime.aliasesInput"
                      : "searchAnswerProfiles.runtime.triggersInput"
                    : kind === "terms"
                      ? "searchAnswerProfiles.runtime.description"
                      : "searchAnswerProfiles.runtime.content",
                )}
                helper={kind === "rules"
                  ? t(field === "labels"
                    ? "searchAnswerProfiles.runtime.triggersHelp"
                    : "searchAnswerProfiles.runtime.contentHelp")
                  : undefined}
                error={error ?? undefined}
                value={form[field]}
                onChange={(event) => update({ [field]: event.target.value })}
                rows={field === "labels" ? 3 : 4}
                width="full"
              />
            );
          })}
          </FormGrid>
          <div className="flex items-center justify-between gap-3">
            <div>
              <span className="text-sm text-fg">{enabledLabel}</span>
              {kind === "rules" ? (
                <p id="runtime-knowledge-enabled-help" className="text-xs leading-relaxed text-fg-muted">
                  {t("searchAnswerProfiles.runtime.enabledHelp")}
                </p>
              ) : null}
            </div>
            <Switch
              checked={form.enabled}
              aria-label={enabledLabel}
              aria-describedby={kind === "rules" ? "runtime-knowledge-enabled-help" : undefined}
              onCheckedChange={(checked) => update({ enabled: checked })}
            />
          </div>
          <div className="flex flex-wrap gap-2">
            {/* 保存と削除は同じ mutation（delete で区別）。スピナーは押した側だけが出し、他は無効にする（#819）。 */}
            <Button
              size="sm"
              icon={Save}
              loading={savePending}
              disabled={deletePending}
              onClick={saveForm}
            >
              {t("searchAnswerProfiles.runtime.save")}
            </Button>
            {form.selected ? (
              <>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={X}
                  disabled={save.isPending}
                  onClick={() => load(emptyForm(kind))}
                >
                  {t("searchAnswerProfiles.runtime.cancel")}
                </Button>
                {/* 確認ダイアログを開く起点。確定は ConfirmDialog の danger ボタンで行う。
                    保存（主操作）の隣に置かず、操作行の反対の端に ghost + tone="danger" で置く（README §4 カード内の操作行）。 */}
                <Button
                  size="sm"
                  variant="ghost"
                  tone="danger"
                  icon={Trash2}
                  className="sm:ml-auto"
                  loading={deletePending}
                  disabled={savePending}
                  onClick={() => void confirmDelete()}
                >
                  {t("searchAnswerProfiles.faq.delete")}
                </Button>
              </>
            ) : null}
          </div>
        </section>

        <section className="min-w-0 space-y-3 rounded-md border border-border p-3">
          <h4 className="text-sm font-semibold text-fg">
            {t("searchAnswerProfiles.runtime.previewTitle")}
          </h4>
          <p className="text-xs leading-relaxed text-fg-muted">
            {t(kind === "terms" ? "searchAnswerProfiles.runtime.previewHelp.terms" : "searchAnswerProfiles.runtime.previewHelp.rules")}
          </p>
          <TextField
            id="runtime-knowledge-question"
            // 質問の文は文章（全幅。#1891）。
            width="full"
            label={t("searchAnswerProfiles.runtime.previewQuestion")}
            value={question}
            maxLength={2000}
            onChange={(event) => setQuestion(event.target.value)}
            required
          />
          <Button
            size="sm"
            variant="secondary"
            icon={Search}
            loading={previewing}
            disabled={!question.trim()}
            onClick={() => void runPreview()}
          >
            {t("searchAnswerProfiles.runtime.previewRun")}
          </Button>
          {preview?.conflicting_rules?.length ? (
            <FormStatus tone="warning" message={t("searchAnswerProfiles.runtime.conflictHelp", { names: preview.conflicting_rules.join("、") })} />
          ) : null}
          {preview ? (
            <dl className="space-y-1 text-xs">
              <div>
                <dt className="font-medium text-fg">
                  {t(
                    kind === "terms"
                      ? "searchAnswerProfiles.runtime.matchedTerms"
                      : "searchAnswerProfiles.runtime.matchedRules",
                  )}
                </dt>
                <dd className="break-words text-fg-muted">
                  {(kind === "terms" ? preview.matched_terms : preview.matched_rules).join("、") ||
                    "—"}
                </dd>
              </div>
              <div>
                <dt className="font-medium text-fg">
                  {t("searchAnswerProfiles.runtime.expanded")}
                </dt>
                <dd className="break-words text-fg-muted">
                  {preview.expanded_question}
                </dd>
              </div>
            </dl>
          ) : null}
          {save.isError ? (
            <FormStatus
              tone="danger"
              message={errorMessage(
                save.error,
                t("searchAnswerProfiles.runtime.saveError"),
              )}
            />
          ) : null}
        </section>
      </div>
      {/* 既存のルールには確認の質問を設定できる（チャットの確認。#717）。ルールの保存とは別に保存する。 */}
      {kind === "rules" && form.selected && form.purpose !== "generation" ? (
        <RuleClarificationEditor
          key={form.selected}
          searchAnswerProfileId={searchAnswerProfileId}
          ruleId={form.selected}
          ruleTitle={form.title}
          stored={storedClarification(
            rows.find((row) => rowKey(kind, row) === form.selected)?.clarification
          )}
        />
      ) : null}
    </div>
  );
}
