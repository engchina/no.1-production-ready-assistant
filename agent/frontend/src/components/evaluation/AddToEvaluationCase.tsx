import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardPlus } from "lucide-react";
import {
  Disclosure,
  FormActionBar,
  FormGrid,
  FormSkeleton,
  FormStatus,
  SelectField,
  TextareaField,
  TextField,
  TimedLoadingState,
  toast,
  ApiErrorState,
  apiErrorMessage,
} from "@production-ready/ui";
import { useAuth } from "@production-ready/system-settings";
import { useNavigate } from "react-router-dom";

import { agentApi, type EvaluationCase, type EvaluationCaseDraft, type EvaluationSetItem } from "@/lib/api";
import { t } from "@/lib/i18n";
import { MENU_PERMISSIONS } from "@/lib/permissions";

/** 評価セットの選択肢の「新しい評価セットを作る」。評価セットの ID（`evset_…`）とは重ならない。 */
const NEW_SET = "__new__";
const QUESTION_MAX_CHARS = 4000;
const EXPECTED_MAX_CHARS = 8000;

/** 品質評価（評価セットの編集）の権限を持つか。「評価ケースに追加」はこの権限の利用者だけに出す。 */
export function useCanEditEvaluationSets(): boolean {
  return useAuth().hasPermission(MENU_PERMISSIONS.evaluation);
}

/**
 * 「評価ケースに追加」（#810）。フィードバックの詳細と Run の詳細で、Run の質問を品質評価の評価セットへ足す
 * （LangSmith・Braintrust の「本番のトレースからデータセットへ追加」と同じ考え方）。
 *
 * 開いたときに Run から下書き（質問・管理者の評価のコメント・呼んだツール）を作り、評価セットを選んで
 * （または新しく作って）追加する。期待する回答の要点は必須で、管理者のコメントが無ければ空から入力する。
 */
export function AddToEvaluationCase({ runId, testId = "add-evaluation-case" }: { runId: string; testId?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Disclosure
      summary={t("evaluation.add.title")}
      description={t("evaluation.add.description")}
      icon={ClipboardPlus}
      open={open}
      onOpenChange={setOpen}
      summaryProps={{ "data-testid": `${testId}-toggle` }}
      contentClassName="@container"
    >
      {/* 開いたときだけ下書きを取得する（閉じたら入力も捨てる）。 */}
      {open ? <AddToEvaluationForm runId={runId} testId={testId} onDone={() => setOpen(false)} /> : null}
    </Disclosure>
  );
}

function AddToEvaluationForm({ runId, testId, onDone }: { runId: string; testId: string; onDone: () => void }) {
  const draft = useQuery({
    queryKey: ["evaluation-case-draft", runId],
    queryFn: () => agentApi.getRunEvaluationCase(runId),
    retry: false,
  });
  const agentId = draft.data?.agent_id ?? "";
  const sets = useQuery({
    queryKey: ["evaluation-sets", agentId],
    queryFn: () => agentApi.listEvaluationSets(agentId),
    enabled: Boolean(agentId),
  });

  if (draft.isLoading || (agentId && sets.isLoading)) {
    return (
      <TimedLoadingState label={t("evaluation.add.loading")} testId={`${testId}-loading`}>
        <FormSkeleton fields={4} title={false} />
      </TimedLoadingState>
    );
  }
  const error = draft.error ?? sets.error;
  if (error || !draft.data) {
    return (
      <ApiErrorState
        error={error}
        fallback={t("evaluation.add.loadError")}
        onRetry={() => void (draft.error ? draft.refetch() : sets.refetch())}
        retryLabel={t("common.retry")}
      />
    );
  }
  return <CaseForm draft={draft.data} sets={sets.data?.sets ?? []} testId={testId} onDone={onDone} />;
}

function toolsText(tools: string[]): string {
  return tools.join(", ");
}

function parseTools(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[,、]/)
        .map((tool) => tool.trim())
        .filter(Boolean)
    ),
  ];
}

function CaseForm({
  draft,
  sets,
  testId,
  onDone,
}: {
  draft: EvaluationCaseDraft;
  sets: EvaluationSetItem[];
  testId: string;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const existing = new Set(draft.existing_set_ids);
  // 既定の追加先は、同じ質問をまだ持たない最近の評価セット。無ければ新しい評価セット。
  const [setId, setSetId] = useState(() => sets.find((item) => !existing.has(item.id))?.id ?? NEW_SET);
  const [newName, setNewName] = useState(() => t("evaluation.add.newSetNameDefault", { agent: draft.agent_name }));
  const [question, setQuestion] = useState(draft.question);
  const [expected, setExpected] = useState(draft.expected);
  const [tools, setTools] = useState(toolsText(draft.expected_tools));
  const [submitted, setSubmitted] = useState(false);

  const add = useMutation({
    mutationFn: async () => {
      const item: EvaluationCase = {
        question: question.trim(),
        expected: expected.trim(),
        expected_tools: parseTools(tools),
        source_run_id: draft.source_run_id,
        // Run が記録した出力の項目を期待する出力にする（#2558。評価セットで直せる）。
        ...(draft.expected_output?.length ? { expected_output: draft.expected_output } : {}),
      };
      if (setId === NEW_SET) {
        return agentApi.createEvaluationSet({ agent_id: draft.agent_id, name: newName.trim(), description: "", cases: [item] });
      }
      return agentApi.appendEvaluationCase(setId, item);
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: ["evaluation-sets"] });
      void queryClient.invalidateQueries({ queryKey: ["evaluation-case-draft", draft.source_run_id] });
      queryClient.setQueryData(["evaluation-set", saved.id], saved);
      toast.success(t("evaluation.add.added", { set: saved.name }), {
        action: {
          label: t("evaluation.add.openSet"),
          onClick: () => navigate(`/evaluation?id=${encodeURIComponent(saved.id)}`),
        },
      });
      onDone();
    },
  });

  const duplicate = setId !== NEW_SET && existing.has(setId);
  const errors = {
    set: duplicate ? t("evaluation.add.duplicate") : undefined,
    newName: setId === NEW_SET && !newName.trim() ? t("evaluation.set.nameRequired") : undefined,
    question: !question.trim() ? t("evaluation.set.questionRequired") : undefined,
    expected: !expected.trim() ? t("evaluation.set.expectedRequired") : undefined,
  };

  function submit() {
    setSubmitted(true);
    // 画面の並び順で最初のエラーの欄へフォーカスする（messaging.md §3.2）。
    const first = (
      [
        ["set", errors.set],
        ["new-set-name", errors.newName],
        ["question", errors.question],
        ["expected", errors.expected],
      ] as const
    ).find(([, message]) => message);
    if (first) {
      document.getElementById(`${testId}-${first[0]}`)?.focus();
      return;
    }
    add.mutate();
  }

  const options = [
    ...sets.map((item) => ({
      value: item.id,
      label: t("evaluation.add.setOption", { name: item.name, count: item.case_count }),
    })),
    { value: NEW_SET, label: t("evaluation.add.newSet") },
  ];

  return (
    <form
      className="space-y-3"
      noValidate
      data-testid={`${testId}-form`}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <FormGrid>
        <SelectField<string>
          id={`${testId}-set`}
          label={t("evaluation.add.set")}
          width="lg"
          required
          value={setId}
          options={options}
          // 同じ質問をすでに持つ評価セットは、選んだ時点で知らせる（追加すると 409 になる）。
          error={duplicate || submitted ? errors.set : undefined}
          onValueChange={(value) => {
            setSetId(value);
            add.reset();
          }}
          data-testid={`${testId}-set`}
        />
        {setId === NEW_SET ? (
          <TextField
            id={`${testId}-new-set-name`}
            label={t("evaluation.add.newSetName")}
            width="md"
            required
            maxLength={100}
            value={newName}
            error={submitted ? errors.newName : undefined}
            onValueChange={setNewName}
          />
        ) : null}
        <TextareaField
          id={`${testId}-question`}
          label={t("evaluation.detail.question")}
          required
          rows={2}
          maxLength={QUESTION_MAX_CHARS}
          value={question}
          error={submitted ? errors.question : undefined}
          onValueChange={setQuestion}
          width="full"
          className="col-span-full"
        />
        <TextareaField
          id={`${testId}-expected`}
          label={t("evaluation.detail.expected")}
          helper={draft.expected ? t("evaluation.add.expectedFromReview") : t("evaluation.add.expectedHelper")}
          required
          rows={3}
          maxLength={EXPECTED_MAX_CHARS}
          value={expected}
          error={submitted ? errors.expected : undefined}
          onValueChange={setExpected}
          width="full"
          className="col-span-full"
        />
        <TextField
          id={`${testId}-tools`}
          label={t("evaluation.set.expectedTools")}
          helper={draft.expected_tools.length ? t("evaluation.add.toolsFromRun") : t("evaluation.set.expectedToolsHelper")}
          value={tools}
          onValueChange={setTools}
          width="full"
          className="col-span-full"
        />
        {draft.expected_output?.length ? (
          <p className="col-span-full text-xs text-fg-muted" data-testid={`${testId}-output`}>
            {t("evaluation.add.outputFromRun", { count: draft.expected_output.length })}
          </p>
        ) : null}
      </FormGrid>
      <FormActionBar
        ariaLabel={t("evaluation.add.actions")}
        primaryActions={[
          {
            id: "add",
            label: t("evaluation.add.submit"),
            icon: ClipboardPlus,
            type: "submit",
            loading: add.isPending,
            testId: `${testId}-submit`,
          },
        ]}
        secondaryActions={[{ id: "cancel", label: t("common.cancel"), onClick: onDone }]}
        status={<FormStatus tone="danger" message={add.error ? apiErrorMessage(add.error, t("common.error.save")) : null} />}
        testId={`${testId}-actions`}
      />
    </form>
  );
}
