import {
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FilterChipGroup,
  FilterSelect,
  Skeleton,
  TimedLoadingState,
} from "@production-ready/ui";
import { ChevronLeft, ChevronRight, PencilLine } from "lucide-react";
import { useState } from "react";

import { EmptyState, ErrorState } from "@/components/StateViews";
import { ApiError, type AnnotationRow, type LabelSetDetail } from "@/lib/api";
import {
  useAnnotationRows,
  useSaveCalibrationLabel,
} from "@/lib/calibration-queries";
import { t } from "@/lib/i18n";
import { toast } from "@/lib/toast";
import { useWorkspaceState } from "@/lib/workspace-state";

import { aspectLabel, CALIBRATION_ASPECTS } from "./calibration-format";
import { LabelEditor, type LabelDraft } from "./LabelEditor";
import { ProgressTable } from "./ProgressTable";
import { RowContent } from "./RowContent";

type RowFilter = "unlabeled" | "all";

function isRowFilter(value: unknown): value is RowFilter {
  return value === "unlabeled" || value === "all";
}

function isAspectFilter(value: unknown): value is string {
  return (
    value === "all" ||
    (CALIBRATION_ASPECTS as readonly unknown[]).includes(value)
  );
}

/**
 * 記入（自分の判定だけ）。1 行（項目 × 観点）ずつ、観点の基準・質問・回答・標準回答・根拠を読んで
 * pass / fail / cannot_judge と誤りの型・メモを付ける。他の記入者と評価器の判定は出さない（blind）。
 */
export function AnnotationPanel({ detail }: { detail: LabelSetDetail }) {
  const labelSetId = detail.summary.label_set_id;
  const [aspect, setAspect] = useWorkspaceState<string>(
    "evaluation.calibration.aspect",
    "all",
    isAspectFilter,
  );
  const [filter, setFilter] = useWorkspaceState<RowFilter>(
    "evaluation.calibration.rowFilter",
    "unlabeled",
    isRowFilter,
  );
  const [index, setIndex] = useState(0);
  const query = useAnnotationRows(labelSetId, {
    aspect: aspect === "all" ? undefined : aspect,
    status: filter,
    offset: index,
  });
  const save = useSaveCalibrationLabel(labelSetId);
  const [saveError, setSaveError] = useState<string | null>(null);
  const data = query.data;
  const row = data?.items[0] ?? null;
  const total = data?.total ?? 0;
  const position = total ? Math.min(index, total - 1) : 0;

  const changeAspect = (value: string) => {
    setAspect(value);
    setIndex(0);
  };
  const changeFilter = (value: RowFilter) => {
    setFilter(value);
    setIndex(0);
  };
  const submit = (target: AnnotationRow, draft: LabelDraft) => {
    setSaveError(null);
    save.mutate(
      {
        item_id: target.item_id,
        aspect: target.aspect,
        label: draft.label,
        error_type: draft.errorType || null,
        note: draft.note.trim() || null,
      },
      {
        onSuccess: () => {
          toast.success(t("evaluation.calibration.annotate.saved"));
          // 未記入だけを見ているときは、保存した行が一覧から外れて次の行が同じ位置に来る。
          if (filter === "all")
            setIndex((value) => Math.min(value + 1, Math.max(total - 1, 0)));
        },
        onError: (error) =>
          setSaveError(
            error instanceof ApiError
              ? error.message
              : t("evaluation.calibration.annotate.saveError"),
          ),
      },
    );
  };

  return (
    <div className="grid min-w-0 gap-4" data-testid="calibration-annotate">
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            {t("evaluation.calibration.progress.mineTitle")}
          </CardTitle>
          <CardDescription>
            {t("evaluation.calibration.progress.mineDescription")}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ProgressTable
            progress={detail.my_progress}
            errorTypes={detail.error_types}
            testId="calibration-my-progress"
            title={t("evaluation.calibration.progress.mineTitle")}
          />
        </CardContent>
      </Card>
      <Card className="min-w-0" data-testid="calibration-annotator">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <PencilLine size={16} className="text-accent-fg" aria-hidden />
            {t("evaluation.calibration.annotate.title")}
          </CardTitle>
          <CardDescription>
            {t("evaluation.calibration.annotate.description")}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid min-w-0 gap-4">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <FilterSelect<string>
              id="calibration-annotate-aspect"
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
              onValueChange={changeAspect}
            />
            <FilterChipGroup<RowFilter>
              label={t("evaluation.calibration.filter.status")}
              value={filter}
              onValueChange={changeFilter}
              options={[
                {
                  value: "unlabeled",
                  label: t("evaluation.calibration.filter.unlabeled"),
                  count: data?.unlabeled,
                },
                { value: "all", label: t("evaluation.calibration.filter.all") },
              ]}
            />
          </div>
          {query.isPending ? (
            <TimedLoadingState
              label={t("evaluation.calibration.annotate.loading")}
              operationKey={`calibration-rows-${labelSetId}`}
              framed={false}
              testId="calibration-rows-loading"
            >
              <Skeleton className="h-6 w-1/3" aria-hidden="true" />
              <Skeleton className="h-40 w-full" aria-hidden="true" />
            </TimedLoadingState>
          ) : query.isError ? (
            <ErrorState
              message={
                query.error instanceof ApiError
                  ? query.error.message
                  : t("evaluation.calibration.annotate.loadError")
              }
              onRetry={() => void query.refetch()}
            />
          ) : !row ? (
            <EmptyState
              title={
                filter === "unlabeled"
                  ? t("evaluation.calibration.annotate.done")
                  : t("evaluation.calibration.annotate.empty")
              }
              hint={
                filter === "unlabeled"
                  ? t("evaluation.calibration.annotate.doneHint")
                  : undefined
              }
            />
          ) : (
            <>
              <RowNavigator
                position={position}
                total={total}
                onPrevious={() => setIndex(Math.max(position - 1, 0))}
                onNext={() => setIndex(Math.min(position + 1, total - 1))}
              />
              <RowContent
                itemId={row.item_id}
                aspect={row.aspect}
                split={row.split}
                question={row.question}
                answer={row.answer}
                standardAnswer={row.standard_answer}
                evidence={row.evidence}
                criterion={detail.aspects.find(
                  (item) => item.aspect === row.aspect,
                )}
              />
              <LabelEditor
                key={`${row.item_id}:${row.aspect}:${row.label ?? ""}`}
                idPrefix="calibration-annotate"
                initial={{
                  label: row.label,
                  errorType: row.error_type ?? "",
                  note: row.note ?? "",
                }}
                errorTypes={detail.error_types}
                saving={save.isPending}
                error={saveError}
                submitLabel={t("evaluation.calibration.annotate.save")}
                onSubmit={(draft) => submit(row, draft)}
              />
            </>
          )}
          {detail.summary.row_count === 0 ? (
            <Banner severity="info">
              {t("evaluation.calibration.annotate.noRows")}
            </Banner>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}

export function RowNavigator({
  position,
  total,
  onPrevious,
  onNext,
}: {
  position: number;
  total: number;
  onPrevious: () => void;
  onNext: () => void;
}) {
  return (
    <div
      className="flex min-w-0 items-center justify-between gap-2"
      data-testid="calibration-row-navigator"
    >
      <Button
        type="button"
        size="sm"
        variant="secondary"
        icon={ChevronLeft}
        disabled={position <= 0}
        onClick={onPrevious}
      >
        {t("evaluation.calibration.annotate.previous")}
      </Button>
      <span className="tnum text-sm font-medium text-fg" role="status">
        {t("evaluation.calibration.annotate.position", {
          current: position + 1,
          total,
        })}
      </span>
      <Button
        type="button"
        size="sm"
        variant="secondary"
        icon={ChevronRight}
        disabled={position >= total - 1}
        onClick={onNext}
      >
        {t("evaluation.calibration.annotate.next")}
      </Button>
    </div>
  );
}
