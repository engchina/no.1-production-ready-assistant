import {
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Skeleton,
  TabPanel,
  Tabs,
  TimedLoadingState,
  useConfirm,
} from "@production-ready/ui";
import { Download, Trash2 } from "lucide-react";
import { useState } from "react";

import { ErrorState } from "@/components/StateViews";
import { api, ApiError, type LabelSetDetail } from "@/lib/api";
import { useDeleteLabelSet, useLabelSet } from "@/lib/calibration-queries";
import { saveDownloadedFile } from "@/lib/download";
import { type I18nKey, t } from "@/lib/i18n";
import { toast } from "@/lib/toast";
import { useWorkspaceState } from "@/lib/workspace-state";

import { AgreementPanel } from "./AgreementPanel";
import { AnnotationPanel } from "./AnnotationPanel";
import { CalibrationPanel } from "./CalibrationPanel";

type Step = "annotate" | "agreement" | "calibrate";
const STEPS: readonly Step[] = ["annotate", "agreement", "calibrate"];
const TABS_PREFIX = "calibration-step";

function isStep(value: unknown): value is Step {
  return (
    typeof value === "string" && (STEPS as readonly string[]).includes(value)
  );
}

/**
 * 選んだ記入の集合の作業（記入 → 一致と裁定 → 校正）。一致と裁定・校正は判定の校正の管理の権限だけ
 * （記入者には出さない。記入者は自分の判定だけを見る）。
 */
export function LabelSetWorkspace({
  labelSetId,
  onDeleted,
}: {
  labelSetId: string;
  onDeleted: () => void;
}) {
  const query = useLabelSet(labelSetId);
  const [step, setStep] = useWorkspaceState<Step>(
    "evaluation.calibration.step",
    "annotate",
    isStep,
  );
  if (query.isPending) {
    return (
      <TimedLoadingState
        label={t("evaluation.calibration.detail.loading")}
        operationKey={`calibration-label-set-${labelSetId}`}
        testId="calibration-detail-loading"
      >
        <Skeleton className="h-8 w-1/2" aria-hidden="true" />
        <Skeleton className="h-40 w-full" aria-hidden="true" />
      </TimedLoadingState>
    );
  }
  if (query.isError) {
    return (
      <ErrorState
        message={
          query.error instanceof ApiError
            ? query.error.message
            : t("evaluation.calibration.detail.loadError")
        }
        onRetry={() => void query.refetch()}
      />
    );
  }
  const detail = query.data;
  const canManage = detail.can_manage;
  const visible = canManage ? STEPS : (["annotate"] as const);
  const current: Step = canManage ? step : "annotate";
  return (
    <section
      className="grid min-w-0 gap-4"
      aria-labelledby="calibration-detail-title"
      data-testid="calibration-detail"
    >
      <LabelSetHeader detail={detail} onDeleted={onDeleted} />
      {visible.length > 1 ? (
        <Tabs
          idPrefix={TABS_PREFIX}
          ariaLabel={t("evaluation.calibration.detail.stepsLabel")}
          value={current}
          onChange={(value) => isStep(value) && setStep(value)}
          items={visible.map((value, index) => ({
            id: value,
            label: t(`evaluation.calibration.tab.${value}` as I18nKey, {
              number: index + 1,
            }),
          }))}
        />
      ) : null}
      <TabPanel id="annotate" value={current} idPrefix={TABS_PREFIX}>
        <AnnotationPanel detail={detail} />
      </TabPanel>
      {canManage ? (
        <>
          <TabPanel id="agreement" value={current} idPrefix={TABS_PREFIX}>
            <AgreementPanel detail={detail} />
          </TabPanel>
          <TabPanel id="calibrate" value={current} idPrefix={TABS_PREFIX}>
            <CalibrationPanel detail={detail} />
          </TabPanel>
        </>
      ) : null}
    </section>
  );
}

function LabelSetHeader({
  detail,
  onDeleted,
}: {
  detail: LabelSetDetail;
  onDeleted: () => void;
}) {
  const summary = detail.summary;
  const confirm = useConfirm();
  const deleteMutation = useDeleteLabelSet();
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const skipped = Object.entries(detail.skipped).reduce(
    (total, [, count]) => total + count,
    0,
  );

  const download = async () => {
    setExportError(null);
    setExporting(true);
    try {
      saveDownloadedFile(await api.downloadLabelSet(summary.label_set_id));
      toast.success(t("evaluation.calibration.export.done"));
    } catch (error) {
      setExportError(
        error instanceof ApiError
          ? error.message
          : t("evaluation.calibration.export.error"),
      );
    } finally {
      setExporting(false);
    }
  };

  const remove = async () => {
    const confirmed = await confirm({
      title: t("evaluation.calibration.delete.title"),
      description: t("evaluation.calibration.delete.description", {
        name: summary.name,
      }),
      confirmLabel: t("evaluation.calibration.delete.confirm"),
      tone: "danger",
    });
    if (!confirmed) return;
    deleteMutation.mutate(summary.label_set_id, {
      onSuccess: () => {
        toast.success(t("evaluation.calibration.delete.done"));
        onDeleted();
      },
      onError: (error) =>
        toast.error(
          error instanceof ApiError
            ? error.message
            : t("evaluation.calibration.delete.error"),
        ),
    });
  };

  return (
    <Card className="min-w-0">
      <CardHeader>
        <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle id="calibration-detail-title" className="break-words">
              {summary.name}
            </CardTitle>
            <CardDescription>
              {t("evaluation.calibration.detail.meta", {
                human: summary.human_item_count,
                synthetic: summary.synthetic_item_count,
                rows: summary.row_count,
                ratio: Math.round((detail.holdout_ratio ?? 0.7) * 100),
                rubric: `${detail.rubric_id} v${detail.rubric_version ?? "—"}`,
              })}
            </CardDescription>
          </div>
          {detail.can_manage ? (
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="secondary"
                tone="danger"
                icon={Trash2}
                loading={deleteMutation.isPending}
                onClick={() => void remove()}
              >
                {t("evaluation.calibration.delete.action")}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                icon={Download}
                loading={exporting}
                onClick={() => void download()}
              >
                {t("evaluation.calibration.export.action")}
              </Button>
            </div>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="grid min-w-0 gap-2">
        <p className="text-xs text-fg-muted">
          {detail.my_code
            ? t("evaluation.calibration.detail.myCode", {
                code: detail.my_code,
              })
            : t("evaluation.calibration.detail.noCode")}
        </p>
        {skipped > 0 ? (
          <p className="text-xs text-fg-muted">
            {t("evaluation.calibration.detail.skipped", { count: skipped })}
          </p>
        ) : null}
        {exportError ? <Banner severity="warning">{exportError}</Banner> : null}
      </CardContent>
    </Card>
  );
}
