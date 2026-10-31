import {
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  FormActionBar,
  ListToolbar,
  PagedDataTable,
  PAGE_SIZE_OPTIONS,
  RowTitleButton,
  Section,
  Skeleton,
  StatusBadge,
  Switch,
  TextField,
  TimedLoadingState,
} from "@production-ready/ui";
import { FileUp, Plus, Scale, X } from "lucide-react";
import { type FormEvent, useRef, useState } from "react";

import { EmptyState, ErrorState } from "@/components/StateViews";
import { ApiError, type LabelSetSummary } from "@/lib/api";
import { useAuth } from "@/components/security/AuthProvider";
import {
  useCalibrationSources,
  useCreateLabelSet,
  useImportLabelSet,
  useLabelSets,
} from "@/lib/calibration-queries";
import { listCountProps, useClientListPaging } from "@/lib/list-paging";
import { paginationLabels } from "@/lib/pagination-labels";
import { formatDateTime } from "@/lib/format";
import { type I18nKey, t } from "@/lib/i18n";
import { CAPABILITY_PERMISSIONS } from "@/lib/permissions";
import { toast } from "@/lib/toast";
import { isNullableString, useWorkspaceState } from "@/lib/workspace-state";

import { LabelSetWorkspace } from "./LabelSetWorkspace";

const NAME_ID = "calibration-label-set-name";
const IMPORT_INPUT_ID = "calibration-label-set-import";

/**
 * 品質評価 › 判定の校正（#3578）。品質評価の LLM の判定（回答の評価器）を人手の判定で校正する。
 * 手順は suite/evaluation/docs/judge-calibration.md と同じ（記入の集合 → 記入 → 一致と裁定 → 校正）で、
 * 記入用のブックの代わりに画面で記入する。記入者は自分の判定だけを見る（blind）。
 */
export function JudgeCalibrationTab() {
  const canManage = useAuth().hasPermission(
    CAPABILITY_PERMISSIONS.evaluationCalibrationManage,
  );
  const setsQuery = useLabelSets();
  const [selectedId, setSelectedId] = useWorkspaceState<string | null>(
    "evaluation.calibration.labelSetId",
    null,
    isNullableString,
  );
  const [creating, setCreating] = useState(false);
  const importMutation = useImportLabelSet();
  const importInput = useRef<HTMLInputElement>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const sets = setsQuery.data?.items ?? [];
  const selected =
    sets.find((item) => item.label_set_id === selectedId) ?? null;

  const importFile = (file: File | null) => {
    if (!file) return;
    setImportError(null);
    importMutation.mutate(file, {
      onSuccess: (data) => {
        setSelectedId(data.label_set.label_set_id);
        toast.success(
          t("evaluation.calibration.import.done", {
            items: data.imported_items,
            labels: data.imported_labels,
          }),
        );
      },
      onError: (error) =>
        setImportError(
          error instanceof ApiError
            ? error.message
            : t("evaluation.calibration.import.error"),
        ),
    });
  };

  return (
    <div className="grid min-w-0 gap-6" data-testid="judge-calibration">
      <CalibrationSteps />
      <Section
        title={t("evaluation.calibration.sets.title")}
        description={t("evaluation.calibration.sets.description")}
        actions={
          canManage ? (
            <div className="flex flex-wrap gap-2">
              <input
                ref={importInput}
                id={IMPORT_INPUT_ID}
                type="file"
                accept=".json,application/json"
                className="sr-only"
                tabIndex={-1}
                aria-label={t("evaluation.calibration.import.action")}
                onChange={(event) => {
                  importFile(event.target.files?.[0] ?? null);
                  event.target.value = "";
                }}
              />
              <Button
                type="button"
                variant="secondary"
                icon={FileUp}
                loading={importMutation.isPending}
                onClick={() => importInput.current?.click()}
              >
                {t("evaluation.calibration.import.action")}
              </Button>
              <Button
                type="button"
                icon={creating ? X : Plus}
                variant={creating ? "secondary" : "primary"}
                onClick={() => setCreating((value) => !value)}
                aria-expanded={creating}
              >
                {creating
                  ? t("evaluation.calibration.create.close")
                  : t("evaluation.calibration.create.open")}
              </Button>
            </div>
          ) : null
        }
      >
        {importError ? <Banner severity="danger">{importError}</Banner> : null}
        {creating && canManage ? (
          <CreateLabelSetForm
            onCreated={(summary) => {
              setCreating(false);
              setSelectedId(summary.label_set_id);
            }}
          />
        ) : null}
        <LabelSetTable
          query={setsQuery}
          selectedId={selected?.label_set_id ?? null}
          canManage={canManage}
          onSelect={setSelectedId}
        />
      </Section>
      {selected ? (
        <LabelSetWorkspace
          key={selected.label_set_id}
          labelSetId={selected.label_set_id}
          onDeleted={() => setSelectedId(null)}
        />
      ) : null}
    </div>
  );
}

/** 手順（記入の集合 → 記入 → 一致と裁定 → 校正）。今どこにいるかを読み手がつかめるようにする。 */
function CalibrationSteps() {
  const steps = ["sets", "annotate", "agreement", "calibrate"] as const;
  return (
    <Card className="min-w-0" data-testid="judge-calibration-steps">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Scale size={16} className="text-accent-fg" aria-hidden />
          {t("evaluation.calibration.title")}
        </CardTitle>
        <CardDescription>
          {t("evaluation.calibration.description")}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ol className="grid min-w-0 gap-2 text-sm md:grid-cols-4">
          {steps.map((step, index) => (
            <li
              key={step}
              className="flex min-w-0 gap-2 rounded-md border border-border bg-surface-sunken p-3"
            >
              <span
                data-surface-tint="accent"
                className="tnum inline-flex size-6 shrink-0 items-center justify-center rounded-full bg-accent-subtle text-xs font-semibold text-accent-fg"
              >
                {index + 1}
              </span>
              <span className="min-w-0">
                <span className="block font-medium text-fg">
                  {t(`evaluation.calibration.step.${step}.title` as I18nKey)}
                </span>
                <span className="block text-xs text-fg-muted">
                  {t(
                    `evaluation.calibration.step.${step}.description` as I18nKey,
                  )}
                </span>
              </span>
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}

function latestCalibrationBadge(row: LabelSetSummary) {
  const latest = row.latest_calibration;
  if (!latest) return <span className="text-fg-muted">—</span>;
  if (latest.status !== "SUCCEEDED") {
    return (
      <StatusBadge
        variant="neutral"
        label={t(`evaluation.job.status.${latest.status}` as I18nKey)}
      />
    );
  }
  return (
    <StatusBadge
      variant={latest.passed ? "success" : "warning"}
      label={
        latest.passed
          ? t("evaluation.calibration.result.passed")
          : t("evaluation.calibration.result.failed")
      }
    />
  );
}

function LabelSetTable({
  query,
  selectedId,
  canManage,
  onSelect,
}: {
  query: ReturnType<typeof useLabelSets>;
  selectedId: string | null;
  canManage: boolean;
  onSelect: (id: string) => void;
}) {
  const paging = useClientListPaging("evaluation.calibration.setsPaging", "label-sets");
  if (query.isPending) {
    return (
      <TimedLoadingState
        label={t("evaluation.calibration.sets.loading")}
        operationKey="calibration-label-sets"
        framed={false}
        testId="calibration-label-sets-loading"
      >
        <Skeleton className="h-10 w-full" aria-hidden="true" />
        <Skeleton className="h-10 w-full" aria-hidden="true" />
      </TimedLoadingState>
    );
  }
  if (query.isError) {
    return (
      <ErrorState
        message={
          query.error instanceof ApiError
            ? query.error.message
            : t("evaluation.calibration.sets.loadError")
        }
        onRetry={() => void query.refetch()}
      />
    );
  }
  return (
    <div className="grid min-w-0 gap-2">
      <ListToolbar
        count={listCountProps({ matchedTotal: query.data.items.length, filtered: false, live: true })}
        testId="calibration-label-sets-toolbar"
      />
      <PagedDataTable<LabelSetSummary>
        {...paging}
        pageSizeOptions={PAGE_SIZE_OPTIONS}
        paginationLabels={paginationLabels()}
        paginationAriaLabel={t("evaluation.calibration.sets.title")}
        paginationTestId="calibration-label-sets-pagination"
        testId="calibration-label-sets"
        ariaLabel={t("evaluation.calibration.sets.title")}
        rows={query.data.items}
        getRowKey={(row) => row.label_set_id}
        selectedRowKey={selectedId}
        onRowClick={(row) => onSelect(row.label_set_id)}
        empty={
          <EmptyState
            title={t("evaluation.calibration.sets.empty")}
            hint={
              canManage
                ? t("evaluation.calibration.sets.emptyHintManage")
                : t("evaluation.calibration.sets.emptyHint")
            }
          />
        }
        columns={[
          {
            key: "name",
            header: t("evaluation.calibration.sets.column.name"),
            rowHeader: true,
            // 375px でもほかの列（折り返さない）に押されて 1 文字ずつ折り返さないよう、最小幅を持たせる（#3583）。
            className: "min-w-[10rem] break-words",
            render: (row) => (
              <RowTitleButton
                title={row.name}
                onClick={() => onSelect(row.label_set_id)}
              />
            ),
          },
          {
            key: "items",
            header: t("evaluation.calibration.sets.column.items"),
            className: "tnum whitespace-nowrap",
            render: (row) =>
              t("evaluation.calibration.sets.items", {
                human: row.human_item_count,
                synthetic: row.synthetic_item_count,
              }),
          },
          {
            key: "mine",
            header: t("evaluation.calibration.sets.column.mine"),
            className: "tnum whitespace-nowrap",
            render: (row) => `${row.my_labeled} / ${row.row_count}`,
          },
          {
            key: "resolved",
            header: t("evaluation.calibration.sets.column.resolved"),
            className: "tnum whitespace-nowrap",
            render: (row) => `${row.resolved_rows} / ${row.row_count}`,
          },
          {
            key: "annotators",
            header: t("evaluation.calibration.sets.column.annotators"),
            className: "tnum whitespace-nowrap",
            render: (row) => row.annotator_count,
          },
          {
            key: "calibration",
            header: t("evaluation.calibration.sets.column.calibration"),
            className: "whitespace-nowrap",
            render: latestCalibrationBadge,
          },
          {
            key: "created",
            header: t("evaluation.calibration.sets.column.created"),
            className: "tnum whitespace-nowrap text-fg-muted",
            render: (row) => formatDateTime(row.created_at),
          },
        ]}
      />
    </div>
  );
}

/** 終わった品質評価を選んで記入の集合を作る（区分は suite の label-template と同じ）。 */
function CreateLabelSetForm({
  onCreated,
}: {
  onCreated: (summary: LabelSetSummary) => void;
}) {
  const sourcesQuery = useCalibrationSources(true);
  const createMutation = useCreateLabelSet();
  const [name, setName] = useState("");
  const [jobIds, setJobIds] = useState<string[]>([]);
  const [synthetic, setSynthetic] = useState(true);
  const [nameError, setNameError] = useState<string | null>(null);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const jobs = sourcesQuery.data?.jobs ?? [];

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextNameError = name.trim()
      ? null
      : t("evaluation.calibration.create.nameRequired");
    const nextJobsError = jobIds.length
      ? null
      : t("evaluation.calibration.create.jobsRequired");
    setNameError(nextNameError);
    setJobsError(nextJobsError);
    if (nextNameError) {
      document.getElementById(NAME_ID)?.focus();
      return;
    }
    if (nextJobsError) return;
    setSubmitError(null);
    const body = {
      name: name.trim(),
      job_ids: jobIds,
      include_synthetic: synthetic,
    };
    createMutation.mutate(body, {
      onSuccess: (summary) => {
        toast.success(t("evaluation.calibration.create.done"));
        onCreated(summary);
      },
      onError: (error) =>
        setSubmitError(
          error instanceof ApiError
            ? error.message
            : t("evaluation.calibration.create.error"),
        ),
    });
  };

  const toggle = (jobId: string, checked: boolean) => {
    setJobsError(null);
    setJobIds((current) =>
      checked
        ? [...current, jobId]
        : current.filter((value) => value !== jobId),
    );
  };

  return (
    <Card className="min-w-0" data-testid="calibration-create">
      <CardHeader>
        <CardTitle>{t("evaluation.calibration.create.title")}</CardTitle>
        <CardDescription>
          {t("evaluation.calibration.create.description")}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="grid min-w-0 gap-4"
          noValidate
          aria-label={t("evaluation.calibration.create.title")}
          onSubmit={submit}
        >
          <TextField
            id={NAME_ID}
            label={t("evaluation.calibration.create.name")}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setNameError(null);
            }}
            placeholder={t("evaluation.calibration.create.namePlaceholder")}
            error={nameError ?? undefined}
            maxLength={200}
            required
            width="md"
          />
          <div className="grid min-w-0 gap-2">
            <p className="text-sm font-medium text-fg">
              {t("evaluation.calibration.create.jobs")}
            </p>
            <p className="text-xs text-fg-muted">
              {t("evaluation.calibration.create.jobsHelp")}
            </p>
            {sourcesQuery.isPending ? (
              <TimedLoadingState
                label={t("evaluation.calibration.create.jobsLoading")}
                operationKey="calibration-sources"
                framed={false}
                testId="calibration-sources-loading"
              >
                <Skeleton className="h-10 w-full" aria-hidden="true" />
              </TimedLoadingState>
            ) : (
              <SourceJobTable jobs={jobs} selected={jobIds} onToggle={toggle} />
            )}
            {jobsError ? (
              <p role="alert" className="text-sm text-danger-fg">
                {jobsError}
              </p>
            ) : null}
          </div>
          <div className="flex min-w-0 items-start gap-3">
            <Switch
              id="calibration-create-synthetic"
              checked={synthetic}
              onCheckedChange={setSynthetic}
              aria-describedby="calibration-create-synthetic-help"
            />
            <div className="min-w-0">
              <label
                htmlFor="calibration-create-synthetic"
                className="text-sm font-medium text-fg"
              >
                {t("evaluation.calibration.create.synthetic")}
              </label>
              <p
                id="calibration-create-synthetic-help"
                className="text-xs text-fg-muted"
              >
                {t("evaluation.calibration.create.syntheticHelp")}
              </p>
            </div>
          </div>
          {submitError ? (
            <Banner severity="danger">{submitError}</Banner>
          ) : null}
          <FormActionBar
            ariaLabel={t("evaluation.calibration.create.title")}
            primaryActions={[
              {
                id: "create",
                label: t("evaluation.calibration.create.submit"),
                icon: Plus,
                type: "submit",
                loading: createMutation.isPending,
                testId: "calibration-create-submit",
              },
            ]}
          />
        </form>
      </CardContent>
    </Card>
  );
}

function SourceJobTable({
  jobs,
  selected,
  onToggle,
}: {
  jobs: NonNullable<ReturnType<typeof useCalibrationSources>["data"]>["jobs"];
  selected: readonly string[];
  onToggle: (jobId: string, checked: boolean) => void;
}) {
  return (
    <DataTable
      testId="calibration-sources"
      ariaLabel={t("evaluation.calibration.create.jobs")}
      rows={jobs}
      getRowKey={(row) => row.job_id}
      isRowSelected={(row) => selected.includes(row.job_id)}
      dense
      empty={
        <EmptyState title={t("evaluation.calibration.create.jobsEmpty")} />
      }
      columns={[
        {
          key: "select",
          header: (
            <span className="sr-only">
              {t("evaluation.calibration.create.select")}
            </span>
          ),
          className: "w-10",
          render: (row) => (
            <input
              type="checkbox"
              className="size-4"
              checked={selected.includes(row.job_id)}
              aria-label={t("evaluation.calibration.create.selectJob", {
                time: formatDateTime(row.finished_at ?? row.created_at),
              })}
              onChange={(event) => onToggle(row.job_id, event.target.checked)}
            />
          ),
        },
        {
          key: "finished",
          header: t("evaluation.calibration.create.column.finished"),
          className: "tnum whitespace-nowrap",
          render: (row) => formatDateTime(row.finished_at ?? row.created_at),
        },
        {
          key: "cases",
          header: t("evaluation.calibration.create.column.cases"),
          className: "tnum whitespace-nowrap",
          render: (row) =>
            t("evaluation.calibration.create.cases", { count: row.case_count }),
        },
        {
          key: "compared",
          header: t("evaluation.calibration.create.column.compared"),
          className: "whitespace-nowrap",
          render: (row) =>
            row.compared ? t("evaluation.calibration.create.compared") : "—",
        },
      ]}
    />
  );
}
