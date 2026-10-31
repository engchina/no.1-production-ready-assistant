import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  DataTable,
  INFORMATION_TABLE_VISIBLE_ROWS,
  INFORMATION_TABLE_ROW_CLASS,
  FilterChipGroup,
  FilterSelect,
  Pagination,
  RowTitleButton,
  Skeleton,
  StatusBadge,
  Switch,
  TimedLoadingState,
} from "@production-ready/ui";
import { Gavel, Users } from "lucide-react";
import { useState } from "react";

import { EmptyState, ErrorState } from "@/components/StateViews";
import {
  ApiError,
  type AgreementFilter,
  type AgreementRow,
  type AspectAgreement,
  type LabelSetDetail,
} from "@/lib/api";
import {
  useCalibrationAgreement,
  useSaveCalibrationAdjudication,
  useSetSyntheticExcluded,
  useSyntheticItems,
} from "@/lib/calibration-queries";
import { type I18nKey, t } from "@/lib/i18n";
import { paginationLabels } from "@/lib/pagination-labels";
import { toast } from "@/lib/toast";

import {
  aspectLabel,
  CALIBRATION_ASPECTS,
  formatKappa,
  formatRate,
  LABEL_VARIANT,
  labelText,
  ROW_STATUS_VARIANT,
  rowStatusLabel,
  shortItemId,
  splitLabel,
} from "./calibration-format";
import { LabelEditor, type LabelDraft } from "./LabelEditor";
import { ProgressTable } from "./ProgressTable";
import { RowContent } from "./RowContent";

const PAGE_SIZE = 25;
const STATUS_FILTERS: readonly AgreementFilter[] = [
  "disagreed",
  "pending",
  "adjudicated",
  "all",
];

/**
 * 一致と裁定（判定の校正の管理の権限）。観点ごとの記入者の間の κ と、食い違い・記入待ちの行を出し、
 * 最終の判定を裁定する。2 人以上の記入者が一致した行は自動で確定する（裁定は要らない）。
 */
export function AgreementPanel({ detail }: { detail: LabelSetDetail }) {
  const labelSetId = detail.summary.label_set_id;
  const [aspect, setAspect] = useState("all");
  const [status, setStatus] = useState<AgreementFilter>("disagreed");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string | null>(null);
  const query = useCalibrationAgreement(
    labelSetId,
    {
      aspect: aspect === "all" ? undefined : aspect,
      status,
      offset: (page - 1) * PAGE_SIZE,
      limit: PAGE_SIZE,
    },
    true,
  );
  const data = query.data;
  const rows = data?.rows ?? [];
  const current = rows.find((row) => rowKey(row) === selected) ?? null;

  return (
    <div className="grid min-w-0 gap-4" data-testid="calibration-agreement">
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Users size={16} className="text-accent-fg" aria-hidden />
            {t("evaluation.calibration.agreement.title")}
          </CardTitle>
          <CardDescription>
            {t("evaluation.calibration.agreement.description")}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid min-w-0 gap-4">
          {query.isPending ? (
            <TimedLoadingState
              label={t("evaluation.calibration.agreement.loading")}
              operationKey={`calibration-agreement-${labelSetId}`}
              framed={false}
              testId="calibration-agreement-loading"
            >
              <Skeleton className="h-24 w-full" aria-hidden="true" />
            </TimedLoadingState>
          ) : query.isError ? (
            <ErrorState
              message={
                query.error instanceof ApiError
                  ? query.error.message
                  : t("evaluation.calibration.agreement.loadError")
              }
              onRetry={() => void query.refetch()}
            />
          ) : (
            <AspectAgreementTable
              aspects={data?.aspects ?? []}
              annotators={data?.annotators ?? []}
            />
          )}
          {detail.final_progress ? (
            <div className="grid min-w-0 gap-2">
              <p className="text-sm font-semibold text-fg">
                {t("evaluation.calibration.progress.finalTitle")}
              </p>
              <ProgressTable
                progress={detail.final_progress}
                errorTypes={detail.error_types}
                testId="calibration-final-progress"
                title={t("evaluation.calibration.progress.finalTitle")}
              />
            </div>
          ) : null}
        </CardContent>
      </Card>
      <Card className="min-w-0" data-testid="calibration-adjudication">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Gavel size={16} className="text-accent-fg" aria-hidden />
            {t("evaluation.calibration.adjudicate.title")}
          </CardTitle>
          <CardDescription>
            {t("evaluation.calibration.adjudicate.description")}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid min-w-0 gap-4">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <FilterSelect<string>
              id="calibration-agreement-aspect"
              label={t("evaluation.calibration.filter.aspect")}
              value={aspect}
              defaultValue="all"
              width="md"
              options={[
                {
                  value: "all",
                  label: t("evaluation.calibration.filter.allAspects"),
                },
                ...CALIBRATION_ASPECTS.map((value) => ({
                  value,
                  label: aspectLabel(value),
                })),
              ]}
              onValueChange={(value) => {
                setAspect(value);
                setPage(1);
              }}
            />
            <FilterChipGroup<AgreementFilter>
              label={t("evaluation.calibration.filter.status")}
              value={status}
              onValueChange={(value) => {
                setStatus(value);
                setPage(1);
                setSelected(null);
              }}
              options={STATUS_FILTERS.map((value) => ({
                value,
                label: t(
                  `evaluation.calibration.agreementFilter.${value}` as I18nKey,
                ),
              }))}
            />
          </div>
          <DataTable<AgreementRow>
            testId="calibration-agreement-rows"
            ariaLabel={t("evaluation.calibration.adjudicate.title")}
            rows={rows}
            loading={query.isPending}
            getRowKey={rowKey}
            selectedRowKey={selected}
            onRowClick={(row) => setSelected(rowKey(row))}
            dense
            empty={
              <EmptyState
                title={t("evaluation.calibration.adjudicate.empty")}
              />
            }
            columns={[
              {
                key: "item",
                header: t("evaluation.calibration.adjudicate.column.item"),
                rowHeader: true,
                render: (row) => (
                  <RowTitleButton
                    title={
                      <span className="font-mono text-xs">
                        {shortItemId(row.item_id)}
                      </span>
                    }
                    fullTitle={row.item_id}
                    current={selected === rowKey(row)}
                    onClick={() => setSelected(rowKey(row))}
                  />
                ),
              },
              {
                key: "aspect",
                header: t("evaluation.calibration.adjudicate.column.aspect"),
                className: "whitespace-nowrap",
                render: (row) => aspectLabel(row.aspect),
              },
              {
                key: "split",
                header: t("evaluation.calibration.adjudicate.column.split"),
                className: "whitespace-nowrap",
                render: (row) => splitLabel(row.split),
              },
              {
                key: "labels",
                header: t("evaluation.calibration.adjudicate.column.labels"),
                render: (row) => (
                  <span className="flex flex-wrap gap-1">
                    {row.annotations.map((label) => (
                      <StatusBadge
                        key={label.code}
                        variant={LABEL_VARIANT[label.label]}
                        label={`${label.code}: ${labelText(label.label)}`}
                      />
                    ))}
                  </span>
                ),
              },
              {
                key: "status",
                header: t("evaluation.calibration.adjudicate.column.status"),
                className: "whitespace-nowrap",
                render: (row) => (
                  <StatusBadge
                    variant={ROW_STATUS_VARIANT[row.status]}
                    label={rowStatusLabel(row.status)}
                  />
                ),
              },
              {
                key: "final",
                header: t("evaluation.calibration.adjudicate.column.final"),
                className: "whitespace-nowrap",
                render: (row) => labelText(row.final_label),
              },
            ]}
          />
          <Pagination
            page={page}
            totalPages={Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE))}
            onPageChange={setPage}
            labels={paginationLabels()}
            range={
              data
                ? {
                    start: data.total ? data.offset + 1 : 0,
                    end: data.offset + data.rows.length,
                    total: data.total,
                  }
                : undefined
            }
          />
          {current ? (
            <AdjudicationEditor detail={detail} row={current} />
          ) : null}
        </CardContent>
      </Card>
      {detail.summary.synthetic_item_count > 0 ? (
        <SyntheticItemsCard labelSetId={labelSetId} />
      ) : null}
    </div>
  );
}

function rowKey(row: Pick<AgreementRow, "item_id" | "aspect">): string {
  return `${row.item_id}::${row.aspect}`;
}

function AspectAgreementTable({
  aspects,
  annotators,
}: {
  aspects: readonly AspectAgreement[];
  annotators: readonly string[];
}) {
  return (
    <div className="grid min-w-0 gap-2">
      <p className="text-xs text-fg-muted">
        {annotators.length
          ? t("evaluation.calibration.agreement.annotators", {
              codes: annotators.join("・"),
            })
          : t("evaluation.calibration.agreement.noAnnotators")}
      </p>
      <DataTable<AspectAgreement>
        testId="calibration-aspect-agreement"
        ariaLabel={t("evaluation.calibration.agreement.title")}
        rows={aspects}
        getRowKey={(row) => row.aspect}
        dense
        columns={[
          {
            key: "aspect",
            header: t("evaluation.calibration.progress.aspect"),
            rowHeader: true,
            className: "whitespace-nowrap",
            render: (row) => aspectLabel(row.aspect),
          },
          {
            key: "kappa",
            header: t("evaluation.calibration.agreement.kappa"),
            render: (row) =>
              row.pairs.length ? (
                <ul className="grid gap-0.5">
                  {row.pairs.map((pair) => (
                    <li
                      key={pair.annotators.join("-")}
                      className="tnum whitespace-nowrap"
                    >
                      {t("evaluation.calibration.agreement.pair", {
                        pair: pair.annotators.join("–"),
                        kappa: formatKappa(pair.kappa),
                        agreement: formatRate(pair.agreement),
                        count: pair.compared,
                      })}
                    </li>
                  ))}
                </ul>
              ) : (
                <span className="text-fg-muted">—</span>
              ),
          },
          {
            key: "agreed",
            header: t("evaluation.calibration.rowStatus.agreed"),
            className: "tnum whitespace-nowrap",
            render: (row) => row.agreed,
          },
          {
            key: "disagreed",
            header: t("evaluation.calibration.rowStatus.disagreed"),
            className: "tnum whitespace-nowrap",
            render: (row) => row.disagreed,
          },
          {
            key: "adjudicated",
            header: t("evaluation.calibration.rowStatus.adjudicated"),
            className: "tnum whitespace-nowrap",
            render: (row) => row.adjudicated,
          },
          {
            key: "pending",
            header: t("evaluation.calibration.rowStatus.pending"),
            className: "tnum whitespace-nowrap",
            render: (row) => row.pending,
          },
        ]}
      />
    </div>
  );
}

function AdjudicationEditor({
  detail,
  row,
}: {
  detail: LabelSetDetail;
  row: AgreementRow;
}) {
  const labelSetId = detail.summary.label_set_id;
  const save = useSaveCalibrationAdjudication(labelSetId);
  const [error, setError] = useState<string | null>(null);
  const submit = (draft: LabelDraft) => {
    setError(null);
    save.mutate(
      {
        item_id: row.item_id,
        aspect: row.aspect,
        label: draft.label,
        error_type: draft.errorType || null,
        note: draft.note.trim() || null,
      },
      {
        onSuccess: () =>
          toast.success(t("evaluation.calibration.adjudicate.saved")),
        onError: (failure) =>
          setError(
            failure instanceof ApiError
              ? failure.message
              : t("evaluation.calibration.adjudicate.saveError"),
          ),
      },
    );
  };
  return (
    <section
      className="grid min-w-0 gap-4 rounded-md border border-border p-4"
      aria-label={t("evaluation.calibration.adjudicate.editorTitle")}
      data-testid="calibration-adjudication-editor"
    >
      <RowContent
        itemId={row.item_id}
        aspect={row.aspect}
        split={row.split}
        question={row.question}
        answer={row.answer}
        standardAnswer={row.standard_answer}
        evidence={row.evidence}
        criterion={detail.aspects.find((item) => item.aspect === row.aspect)}
      />
      <div className="grid min-w-0 gap-2">
        <p className="text-xs font-semibold text-fg-muted">
          {t("evaluation.calibration.adjudicate.annotations")}
        </p>
        <ul className="grid min-w-0 gap-2">
          {row.annotations.map((label) => (
            <li
              key={label.code}
              className="grid min-w-0 gap-1 rounded-md bg-surface-sunken p-2 text-sm"
            >
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs text-fg-muted">
                  {label.code}
                </span>
                <StatusBadge
                  variant={LABEL_VARIANT[label.label]}
                  label={labelText(label.label)}
                />
                {label.error_type ? (
                  <span className="text-xs text-fg-muted">
                    {t(
                      `evaluation.calibration.errorType.${label.error_type}` as I18nKey,
                    )}
                  </span>
                ) : null}
              </span>
              {label.note ? (
                <span className="break-words text-fg">{label.note}</span>
              ) : null}
            </li>
          ))}
        </ul>
      </div>
      <LabelEditor
        key={`${row.item_id}:${row.aspect}:${row.adjudication?.label ?? ""}`}
        idPrefix="calibration-adjudicate"
        initial={{
          label: row.adjudication?.label ?? row.final_label,
          errorType: row.adjudication?.error_type ?? "",
          note: row.adjudication?.note ?? "",
        }}
        errorTypes={detail.error_types}
        saving={save.isPending}
        error={error}
        submitLabel={t("evaluation.calibration.adjudicate.save")}
        onSubmit={submit}
      />
    </section>
  );
}

function SyntheticItemsCard({ labelSetId }: { labelSetId: string }) {
  const query = useSyntheticItems(labelSetId, true);
  const mutation = useSetSyntheticExcluded(labelSetId);
  const items = query.data?.items ?? [];
  return (
    <Card className="min-w-0" data-testid="calibration-synthetic">
      <CardHeader>
        <CardTitle>{t("evaluation.calibration.synthetic.title")}</CardTitle>
        <CardDescription>
          {t("evaluation.calibration.synthetic.description")}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          ariaLabel={t("evaluation.calibration.synthetic.title")}
          testId="calibration-synthetic-table"
          scrollTestId="calibration-synthetic-scroll"
          scrollAriaLabel={t("evaluation.calibration.synthetic.title")}
          stickyHeader
          visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
          rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
          rows={items}
          loading={query.isPending}
          getRowKey={(row) => row.item_id}
          dense
          empty={
            <EmptyState title={t("evaluation.calibration.synthetic.empty")} />
          }
          columns={[
            {
              key: "kind",
              header: t("evaluation.calibration.synthetic.kind"),
              rowHeader: true,
              className: "whitespace-nowrap",
              render: (row) =>
                t(`evaluation.calibration.perturbation.${row.kind}` as I18nKey),
            },
            {
              key: "description",
              header: t("evaluation.calibration.synthetic.change"),
              render: (row) => (
                <span className="break-words text-sm">{row.description}</span>
              ),
            },
            {
              key: "fail",
              header: t("evaluation.calibration.synthetic.failAspects"),
              render: (row) => row.fail_aspects.map(aspectLabel).join("・"),
            },
            {
              key: "excluded",
              header: t("evaluation.calibration.synthetic.excluded"),
              render: (row) => (
                <Switch
                  checked={row.excluded}
                  aria-label={t(
                    "evaluation.calibration.synthetic.excludeLabel",
                    { item: shortItemId(row.item_id) },
                  )}
                  disabled={mutation.isPending}
                  onCheckedChange={(excluded) =>
                    mutation.mutate({ itemId: row.item_id, excluded })
                  }
                />
              ),
            },
          ]}
        />
      </CardContent>
    </Card>
  );
}
