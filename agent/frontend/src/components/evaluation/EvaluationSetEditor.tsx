import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, FileDown, FlaskConical, Plus, RotateCcw, Save, Trash2, Upload } from "lucide-react";
import {
  Button,
  Card,
  CardContent,
  DEFAULT_PAGE_SIZE,
  EmptyState,
  FormActionBar,
  FormGrid,
  ObjectActionBar,
  PageBody,
  PageHeader,
  Pagination,
  SaveErrorBanner,
  Section,
  SelectField,
  Switch,
  TextareaField,
  TextField,
  toast,
  useConfirm,
  usePagination,
  type EntityAction,
  apiErrorMessage,
} from "@production-ready/ui";

import { agentPaginationLabels } from "@/components/ListViews";
import {
  ClarificationAnswersField,
  clarificationAnswerDraft,
  clarificationAnswerInvalid,
  toClarificationAnswers,
  type ClarificationAnswerDraft,
} from "@/components/evaluation/ClarificationAnswersField";
import {
  EvaluationVersionField,
  usableEvaluationTarget,
  useCanEvaluateDraft,
} from "@/components/evaluation/EvaluationVersionField";
import {
  ExpectedOutputField,
  expectedOutputDrafts,
  toExpectedOutput,
  type ExpectedOutputRowDraft,
} from "@/components/evaluation/ExpectedOutputField";
import {
  agentApi,
  type AgentOutputField,
  type AgentProfile,
  type EvaluationCase,
  type EvaluationSet,
  type EvaluationSetInput,
  type EvaluationTarget,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import { sameDraft, useEditorLeaveGuard } from "@/lib/leave-guard";

export const EVALUATION_MAX_CASES = 50;
/** 評価ケースの 1 ページの件数（一覧の既定の表示件数と同じ。#1952）。 */
const CASES_PAGE_SIZE = DEFAULT_PAGE_SIZE;

/** 画面で編集するケース（期待するツールはカンマ区切りの文字で持つ）。 */
interface CaseDraft {
  key: string;
  id: string;
  question: string;
  expected: string;
  tools: string;
  /** 期待するツールを書いた順に呼ぶ（#1845）。 */
  toolsInOrder: boolean;
  expectedApproval: boolean;
  /** 問い返しへの答え（#1845）。 */
  answers: ClarificationAnswerDraft[];
  /** フィードバック・Run の詳細から追加したケースの元の Run（保存し直しても保つ。#810）。 */
  sourceRunId: string | null;
  /** 期待する出力の項目（#2558）。 */
  output: ExpectedOutputRowDraft[];
}

interface SetDraft {
  agentId: string;
  name: string;
  description: string;
  cases: CaseDraft[];
}

let draftKey = 0;
function nextKey() {
  draftKey += 1;
  return `case-draft-${draftKey}`;
}

function caseDraft(item?: EvaluationCase): CaseDraft {
  return {
    key: nextKey(),
    id: item?.id ?? "",
    question: item?.question ?? "",
    expected: item?.expected ?? "",
    tools: (item?.expected_tools ?? []).join(", "),
    toolsInOrder: item?.expected_tools_in_order ?? false,
    expectedApproval: item?.expected_approval ?? false,
    answers: (item?.clarification_answers ?? []).map((answer) => clarificationAnswerDraft(answer)),
    sourceRunId: item?.source_run_id ?? null,
    output: expectedOutputDrafts(item?.expected_output),
  };
}

function draftOf(set: EvaluationSet | undefined, agentId: string): SetDraft {
  return {
    agentId: set?.agent_id ?? agentId,
    name: set?.name ?? "",
    description: set?.description ?? "",
    cases: set ? set.cases.map((item) => caseDraft(item)) : [caseDraft()],
  };
}

/**
 * 保存で付く ID（ID を省いたケースは、使われていない `case-<番号>` を位置の番号から探す）。
 * backend の `number_cases` と同じ付け方で、ID の欄の placeholder に出す（#965）。
 */
function assignedCaseIds(cases: CaseDraft[]): string[] {
  const used = new Set(cases.map((item) => item.id.trim()).filter(Boolean));
  return cases.map((item, index) => {
    const id = item.id.trim();
    if (id) return id;
    let number = index + 1;
    while (used.has(`case-${number}`)) number += 1;
    used.add(`case-${number}`);
    return `case-${number}`;
  });
}

/** 明示した ID が、ほかのケースの明示した ID と重なるケースの key（保存すると 422 になる。#965）。 */
function duplicateIdKeys(cases: CaseDraft[]): Set<string> {
  const counts = new Map<string, number>();
  for (const item of cases) {
    const id = item.id.trim();
    if (id) counts.set(id, (counts.get(id) ?? 0) + 1);
  }
  return new Set(cases.filter((item) => (counts.get(item.id.trim()) ?? 0) > 1).map((item) => item.key));
}

function caseInvalid(item: CaseDraft, duplicates: Set<string>): boolean {
  return (
    !item.question.trim() ||
    !item.expected.trim() ||
    duplicates.has(item.key) ||
    item.answers.some((answer) => clarificationAnswerInvalid(answer))
  );
}

/** 比べるときは画面だけの key を外す。 */
function comparable(draft: SetDraft) {
  return {
    ...draft,
    cases: draft.cases.map(({ key: _key, answers, output, ...rest }) => ({
      ...rest,
      answers: answers.map(({ key: _answerKey, ...answer }) => answer),
      output: output.map(({ key: _outputKey, ...row }) => row),
    })),
  };
}

/** 評価する業務 Agent の出力の項目（#2558。見つからなければ空）。 */
function outputFieldsOf(agents: AgentProfile[], agentId: string): AgentOutputField[] {
  return agents.find((agent) => agent.id === agentId)?.output_fields ?? [];
}

function toPayload(draft: SetDraft, fields: AgentOutputField[]): EvaluationSetInput {
  return {
    agent_id: draft.agentId,
    name: draft.name.trim(),
    description: draft.description.trim(),
    cases: draft.cases.map((item) => ({
      id: item.id.trim() || undefined,
      question: item.question.trim(),
      expected: item.expected.trim(),
      expected_tools: item.tools
        .split(/[,、]/)
        .map((tool) => tool.trim())
        .filter(Boolean),
      expected_tools_in_order: item.toolsInOrder,
      ...(item.expectedApproval ? { expected_approval: true } : {}),
      clarification_answers: toClarificationAnswers(item.answers),
      source_run_id: item.sourceRunId,
      // 期待する出力は書いたときだけ送る（#2558）。
      ...((output) => (output.length ? { expected_output: output } : {}))(toExpectedOutput(item.output, fields)),
    })),
  };
}

export function downloadBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * 評価セットの全画面エディタ（A 型。`?id=new` / `?id=<評価セット ID>`。#776）。
 * 評価ケースは表で 1 件ずつ編集し、Excel で取り込み・書き出しできる（NL2SQL の SQL生成評価と同じ列）。
 */
export function EvaluationSetEditor({
  evaluationSet,
  agents,
  defaultAgentId,
  onBack,
  onSaved,
  onStart,
  onDeleted,
  starting,
}: {
  evaluationSet?: EvaluationSet;
  agents: AgentProfile[];
  defaultAgentId: string;
  onBack: () => void;
  onSaved: (saved: EvaluationSet, created: boolean) => void;
  /** 評価を始める（評価する版と、評価する業務 Agent を添える。#810・#1845）。 */
  onStart: (set: EvaluationSet, version: EvaluationTarget, agentId: string) => void;
  onDeleted: () => void;
  starting: boolean;
}) {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const fileInput = useRef<HTMLInputElement | null>(null);
  const [baseline, setBaseline] = useState<SetDraft>(() => draftOf(evaluationSet, defaultAgentId));
  const [draft, setDraft] = useState<SetDraft>(baseline);
  const [submitted, setSubmitted] = useState(false);
  const dirty = !sameDraft(comparable(draft), comparable(baseline));
  // 評価する版（#810。実行の意思なので保存しない）。
  const [versionChoice, setVersionChoice] = useState<EvaluationTarget | null>(null);
  // 評価する業務 Agent（#1845。既定は評価セットの業務 Agent。実行の意思なので保存しない）。
  const [evaluatedAgentId, setEvaluatedAgentId] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (payload: EvaluationSetInput) =>
      evaluationSet ? agentApi.updateEvaluationSet(evaluationSet.id, payload) : agentApi.createEvaluationSet(payload),
    onSuccess: async (saved) => {
      const next = draftOf(saved, saved.agent_id);
      setBaseline(next);
      setDraft(next);
      setSubmitted(false);
      toast.success(evaluationSet ? t("evaluation.set.saved") : t("evaluation.set.created"));
      await queryClient.invalidateQueries({ queryKey: ["evaluation-sets"] });
      onSaved(saved, !evaluationSet);
    },
  });
  const importCases = useMutation({
    mutationFn: (file: File) => agentApi.parseEvaluationCasesXlsx(file),
    onError: (error) => toast.error(t("evaluation.set.importFailed"), { description: apiErrorMessage(error, t("common.error.retryLater")) }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => agentApi.deleteEvaluationSet(id),
    onSuccess: async () => {
      toast.success(t("evaluation.set.deleted"));
      await queryClient.invalidateQueries({ queryKey: ["evaluation-sets"] });
      onDeleted();
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("common.error.operation"))),
  });
  const { confirmClose } = useEditorLeaveGuard(dirty, save.isPending);

  const pagination = usePagination(draft.cases, CASES_PAGE_SIZE, { resetKey: evaluationSet?.id ?? "new" });
  const labels = agentPaginationLabels();

  const nameError = submitted && !draft.name.trim() ? t("evaluation.set.nameRequired") : undefined;
  const tooMany = draft.cases.length > EVALUATION_MAX_CASES;
  const caseIds = assignedCaseIds(draft.cases);
  const duplicateIds = duplicateIdKeys(draft.cases);

  function setField<K extends keyof SetDraft>(key: K, value: SetDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function updateCase(key: string, patch: Partial<CaseDraft>) {
    setDraft((current) => ({
      ...current,
      cases: current.cases.map((item) => (item.key === key ? { ...item, ...patch } : item)),
    }));
  }

  function addCase() {
    setDraft((current) => ({ ...current, cases: [...current.cases, caseDraft()] }));
    // 追加したケースが見えるページへ移る。
    pagination.setPage(Math.ceil((draft.cases.length + 1) / CASES_PAGE_SIZE));
  }

  function removeCase(key: string) {
    setDraft((current) => ({ ...current, cases: current.cases.filter((item) => item.key !== key) }));
  }

  async function back() {
    if (await confirmClose()) onBack();
  }

  function discard() {
    setDraft(baseline);
    setSubmitted(false);
  }

  function submit() {
    setSubmitted(true);
    // エラーのケースが別のページにあると、押しても何も見えないため、最初のエラーのページへ移る（#965）。
    const firstInvalid = draft.cases.findIndex((item) => caseInvalid(item, duplicateIds));
    if (firstInvalid >= 0) pagination.setPage(Math.floor(firstInvalid / CASES_PAGE_SIZE) + 1);
    const invalid = !draft.name.trim() || draft.cases.length === 0 || tooMany || firstInvalid >= 0;
    if (invalid) return;
    save.mutate(toPayload(draft, outputFieldsOf(agents, evaluatedAgentId ?? draft.agentId)));
  }

  async function onFile(file: File | undefined) {
    if (!file) return;
    const parsed = await importCases.mutateAsync(file).catch(() => null);
    if (fileInput.current) fileInput.current.value = "";
    if (!parsed) return;
    const hasContent = draft.cases.some((item) => item.question.trim() || item.expected.trim());
    if (hasContent) {
      const ok = await confirm({
        title: t("evaluation.set.importReplaceTitle"),
        description: t("evaluation.set.importReplaceDescription", { count: parsed.cases.length }),
        confirmLabel: t("evaluation.set.importReplace"),
        tone: "warning",
      });
      if (!ok) return;
    }
    setField(
      "cases",
      parsed.cases.map((item) => caseDraft(item))
    );
    pagination.setPage(1);
    toast.success(t("evaluation.set.imported", { count: parsed.cases.length }));
  }

  async function downloadTemplate() {
    try {
      downloadBlob(await agentApi.downloadEvaluationTemplate(), "evaluation-cases-template.xlsx");
    } catch (error) {
      toast.error(apiErrorMessage(error, t("common.error.operation")));
    }
  }

  async function exportCases(set: EvaluationSet) {
    try {
      downloadBlob(await agentApi.downloadEvaluationSetXlsx(set.id), `${set.name}.xlsx`);
    } catch (error) {
      toast.error(apiErrorMessage(error, t("common.error.operation")));
    }
  }

  async function confirmDelete(set: EvaluationSet) {
    const ok = await confirm({
      title: t("evaluation.set.deleteTitle", { name: set.name }),
      description: t("evaluation.set.deleteDescription"),
      confirmLabel: t("evaluation.set.delete"),
      tone: "danger",
    });
    if (ok) remove.mutate(set.id);
  }

  const objectActions: EntityAction[] = evaluationSet
    ? [
        {
          id: "start",
          label: t("evaluation.form.start"),
          icon: FlaskConical,
          // 保存していない変更があるときは、保存した内容で評価することになるため先に保存させる。
          disabled: dirty || starting,
          loading: starting,
          onSelect: () => onStart(evaluationSet, version, evaluatedAgent?.id ?? evaluationSet.agent_id),
          testId: "evaluation-set-start",
        },
        { id: "export", label: t("evaluation.set.export"), icon: Download, onSelect: () => void exportCases(evaluationSet) },
        {
          id: "delete",
          label: t("evaluation.set.delete"),
          icon: Trash2,
          tone: "danger",
          onSelect: () => void confirmDelete(evaluationSet),
        },
      ]
    : [];
  const setAgent = agents.find((agent) => agent.id === draft.agentId);
  const agentName = setAgent?.name ?? draft.agentId;
  const evaluatedAgent = agents.find((agent) => agent.id === (evaluatedAgentId ?? draft.agentId)) ?? setAgent;
  const canEvaluateDraft = useCanEvaluateDraft(evaluatedAgent);
  const version = usableEvaluationTarget(evaluatedAgent, versionChoice, canEvaluateDraft);
  // 期待する出力の欄に出す項目（評価する業務 Agent の出力の項目。#2558）。
  const outputFields = outputFieldsOf(agents, evaluatedAgentId ?? draft.agentId);

  return (
    <>
      <PageHeader
        wide
        title={evaluationSet ? evaluationSet.name : t("evaluation.set.create")}
        subtitle={t("evaluation.set.subtitle")}
        // 一覧へ戻るは左上（#618）。確定（作成・保存と変更を破棄）はフォームの末尾の FormActionBar（#1786）。
        back={{
          label: t("common.backToList"),
          ariaLabel: t("editor.backToListOf", { list: t("nav.evaluation") }),
          onClick: () => void back(),
          testId: "editor-back",
        }}
      />
      <PageBody wide className="space-y-6">
        <Section
          title={t("evaluation.set.basic")}
          actions={
            evaluationSet ? (
              <ObjectActionBar
                actions={objectActions}
                ariaLabel={t("common.entityActions", { name: evaluationSet.name })}
                moreLabel={t("common.moreActions")}
                testId="evaluation-set-actions"
              />
            ) : undefined
          }
        >
          <Card>
            <CardContent className="space-y-4 pt-5">
              {/* 欄は値の種類の幅（名前 md・説明は全幅、FormGrid の 60rem まで。#1891）。 */}
              <FormGrid columns={1}>
                <TextField
                  id="evaluation-set-name"
                  label={t("evaluation.set.name")}
                  width="md"
                  required
                  maxLength={100}
                  value={draft.name}
                  error={nameError}
                  onValueChange={(value) => setField("name", value)}
                />
                <TextareaField
                  id="evaluation-set-description"
                  label={t("evaluation.set.description")}
                  width="full"
                  rows={2}
                  maxLength={500}
                  value={draft.description}
                  onValueChange={(value) => setField("description", value)}
                />
              </FormGrid>
              {evaluationSet ? (
                <>
                  <p className="text-sm text-fg">
                    <span className="text-fg-muted">{`${t("evaluation.form.agent")}: `}</span>
                    {agentName}
                  </p>
                  {/* 「評価を開始」（上の操作）で評価する業務 Agent（#1845）と版（#810）。 */}
                  <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
                    <SelectField<string>
                      id="evaluation-set-evaluated-agent"
                      data-testid="evaluation-set-evaluated-agent"
                      label={t("evaluation.set.evaluatedAgent")}
                      helper={t("evaluation.set.evaluatedAgentHelper")}
                      // 対象の選択（業務 Agent）は lg（#1891）。
                      width="lg"
                      value={evaluatedAgent?.id ?? draft.agentId}
                      options={agents.map((agent) => ({ value: agent.id, label: agent.name }))}
                      onValueChange={(value) => {
                        setEvaluatedAgentId(value);
                        setVersionChoice(null);
                      }}
                    />
                    <EvaluationVersionField
                      id="evaluation-set-version"
                      agent={evaluatedAgent}
                      value={version}
                      onChange={setVersionChoice}
                    />
                  </div>
                </>
              ) : (
                <SelectField<string>
                  id="evaluation-set-agent"
                  label={t("evaluation.form.agent")}
                  helper={t("evaluation.set.agentHelper")}
                  width="lg"
                  value={draft.agentId}
                  options={agents.map((agent) => ({ value: agent.id, label: agent.name }))}
                  onValueChange={(value) => setField("agentId", value)}
                />
              )}
            </CardContent>
          </Card>
        </Section>

        <Section title={t("evaluation.set.cases", { count: draft.cases.length })} description={t("evaluation.set.casesDescription")}>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" icon={Plus} onClick={addCase} disabled={draft.cases.length >= EVALUATION_MAX_CASES}>
              {t("evaluation.set.addCase")}
            </Button>
            <Button
              variant="secondary"
              icon={Upload}
              loading={importCases.isPending}
              onClick={() => fileInput.current?.click()}
              data-testid="evaluation-set-import"
            >
              {t("evaluation.set.import")}
            </Button>
            <Button variant="ghost" icon={FileDown} onClick={() => void downloadTemplate()}>
              {t("evaluation.set.template")}
            </Button>
            {/* Excel の取り込み（ネイティブの file の入力。ボタンから開く）。 */}
            <input
              ref={fileInput}
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              className="sr-only"
              aria-label={t("evaluation.set.import")}
              data-testid="evaluation-set-file"
              onChange={(event) => void onFile(event.target.files?.[0])}
            />
          </div>
          {tooMany ? (
            <p className="text-sm text-danger-fg" role="alert">
              {t("evaluation.form.tooMany")}
            </p>
          ) : null}
          {draft.cases.length === 0 ? (
            <EmptyState title={t("evaluation.set.noCases")} hint={t("evaluation.set.noCasesHint")} />
          ) : (
            <div className="space-y-3">
              {pagination.pageItems.map((item) => {
                const index = draft.cases.indexOf(item);
                const number = index + 1;
                return (
                  <Card key={item.key} data-testid={`evaluation-case-${number}`}>
                    <CardContent className="space-y-3 pt-4">
                      <div className="flex flex-wrap items-end justify-between gap-3">
                        <div className="min-w-0">
                          <p className="text-sm font-semibold text-fg">{t("evaluation.set.caseNumber", { number })}</p>
                          {item.sourceRunId ? (
                            <p className="break-all text-xs text-fg-muted" data-testid={`evaluation-case-${number}-source`}>
                              {t("evaluation.set.caseSource", { run: item.sourceRunId })}
                            </p>
                          ) : null}
                        </div>
                        <div className="flex flex-wrap items-end gap-2">
                          <TextField
                            id={`${item.key}-id`}
                            label={t("evaluation.set.caseId")}
                            width="sm"
                            maxLength={100}
                            // 空のまま保存すると付く ID（#965）。
                            placeholder={caseIds[index]}
                            value={item.id}
                            error={submitted && duplicateIds.has(item.key) ? t("evaluation.set.caseIdDuplicate") : undefined}
                            onValueChange={(value) => updateCase(item.key, { id: value })}
                          />
                          <Button
                            variant="ghost"
                            iconOnly
                            icon={Trash2}
                            aria-label={t("evaluation.set.removeCase", { number })}
                            onClick={() => removeCase(item.key)}
                          />
                        </div>
                      </div>
                      <FormGrid>
                        <TextareaField
                          id={`${item.key}-question`}
                          label={t("evaluation.detail.question")}
                          width="full"
                          required
                          rows={2}
                          maxLength={4000}
                          value={item.question}
                          error={submitted && !item.question.trim() ? t("evaluation.set.questionRequired") : undefined}
                          onValueChange={(value) => updateCase(item.key, { question: value })}
                        />
                        <TextareaField
                          id={`${item.key}-expected`}
                          label={t("evaluation.detail.expected")}
                          width="full"
                          required
                          rows={2}
                          maxLength={8000}
                          value={item.expected}
                          error={submitted && !item.expected.trim() ? t("evaluation.set.expectedRequired") : undefined}
                          onValueChange={(value) => updateCase(item.key, { expected: value })}
                        />
                        <TextField
                          id={`${item.key}-tools`}
                          label={t("evaluation.set.expectedTools")}
                          helper={t("evaluation.set.expectedToolsHelper")}
                          width="full"
                          className="col-span-full"
                          value={item.tools}
                          onValueChange={(value) => updateCase(item.key, { tools: value })}
                        />
                      </FormGrid>
                      {/* 期待するツールの順（先頭が最初に呼ぶツール。#1845）。説明は aria-describedby で結ぶ。 */}
                      <div className="space-y-1">
                        <label className="inline-flex items-center gap-2 text-sm text-fg">
                          <Switch
                            checked={item.toolsInOrder}
                            aria-describedby={`${item.key}-tools-order-helper`}
                            data-testid={`evaluation-case-${number}-tools-order`}
                            onCheckedChange={(checked) => updateCase(item.key, { toolsInOrder: checked })}
                          />
                          {t("evaluation.set.expectedToolsInOrder")}
                        </label>
                        <p id={`${item.key}-tools-order-helper`} className="text-xs text-fg-muted">
                          {t("evaluation.set.expectedToolsInOrderHelper")}
                        </p>
                      </div>
                      <div className="space-y-1">
                        <label className="inline-flex items-center gap-2 text-sm text-fg">
                          <Switch checked={item.expectedApproval} data-testid={`evaluation-case-${number}-expected-approval`}
                            aria-describedby={`${item.key}-approval-helper`}
                            onCheckedChange={(checked) => updateCase(item.key, { expectedApproval: checked })} />
                          {t("evaluation.set.expectedApproval")}
                        </label>
                        <p id={`${item.key}-approval-helper`} className="text-xs text-fg-muted">{t("evaluation.set.expectedApprovalHelper")}</p>
                      </div>
                      <ClarificationAnswersField
                        idPrefix={item.key}
                        caseNumber={number}
                        answers={item.answers}
                        submitted={submitted}
                        onChange={(answers) => updateCase(item.key, { answers })}
                      />
                      <ExpectedOutputField
                        idPrefix={item.key}
                        caseNumber={number}
                        fields={outputFields}
                        rows={item.output}
                        onChange={(output) => updateCase(item.key, { output })}
                      />
                    </CardContent>
                  </Card>
                );
              })}
              {pagination.totalPages > 1 ? (
                <Pagination
                  page={pagination.page}
                  totalPages={pagination.totalPages}
                  onPageChange={pagination.setPage}
                  range={pagination.range}
                  labels={labels}
                  testId="evaluation-cases-pagination"
                />
              ) : null}
            </div>
          )}
        </Section>
        {/* 保存の失敗は確定の行の直上の 1 か所だけ（messaging.md §3.3.1。#585 / #1786）。 */}
        <SaveErrorBanner
          message={save.error ? apiErrorMessage(save.error, t("common.error.save")) : null}
          attemptKey={save.submittedAt}
          testId="evaluation-set-save-error"
        />
        {/* 確定はフォームの末尾の左（作成・保存 → 変更を破棄）。最後のカードの下に置き、画面の下端に貼り付けない（#1786 / #2715）。 */}
        <FormActionBar
          ariaLabel={t("editor.actions.formLabel")}
          testId="evaluation-set-form-actions"
          primaryActions={[
            {
              id: "save",
              label: evaluationSet ? t("common.save") : t("common.create"),
              icon: Save,
              loading: save.isPending,
              onClick: submit,
            },
          ]}
          secondaryActions={[
            // 変更を破棄は常に出し、変更が無いときは disabled（#618。#808）。
            {
              id: "discard",
              label: t("common.discardChanges"),
              icon: RotateCcw,
              disabled: !dirty || save.isPending,
              onClick: discard,
            },
          ]}
        />
      </PageBody>
    </>
  );
}
