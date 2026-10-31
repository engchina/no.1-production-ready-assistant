"use client";

import {
  AlertCircle,
  Check,
  CheckCircle2,
  Circle,
  Clock3,
  Copy,
  Eye,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  SearchCheck,
  Settings2,
  Trash2,
} from "lucide-react";
import { useMemo, useRef, useState, type ReactNode } from "react";

import { DocumentProcessingConfigPanel } from "./DocumentProcessingConfigPanel";
import { EmptyState } from "@/components/StateViews";
import {
  canAddRecipe,
  canDeleteRecipe,
  recipeConfigLocked,
  recipeIsActive,
  recipeLayerRebuildSummary,
  recipeLayerStatuses,
  resolveSelectedRecipe,
  type RecipeLayerStatusView,
} from "./DocumentRecipeManager.logic";
import { useRecipeOperationError } from "./recipe-operation-error";
import { CitationCard } from "@/components/search/CitationCard";
import {
  queueAheadText,
  queueElapsedText,
  queueReasonText,
  queueWaitLabel,
  recipeQueueWait,
  type QueueWaitView,
} from "@/lib/ingestion-queue";
import { useNowMs } from "@/lib/use-now-ms";
import {
  Banner,
  Button,
  type EntityAction,
  FieldActionRow,
  FormGrid,
  FormStatus,
  ObjectActionBar,
  SelectField,
  Skeleton,
  Spinner,
  TimedLoadingState,
  TextField,
  surfaceTintFor,
} from "@production-ready/ui";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  ApiError,
  api,
  type DocumentChunkSet,
  type DocumentProcessingConfigData,
  type DocumentLayerRebuildInput,
  type DocumentLayerStatusName,
  type DocumentRecipeStep,
  type DocumentRecipeView,
  type IngestionJobPhase,
  type SearchResponse,
} from "@/lib/api";
import { isSubmitEnter } from "@/lib/keyboard";
import { formatDateTime } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import {
  useApproveDocumentRecipe,
  useCreateDocumentRecipe,
  useDeleteDocumentRecipe,
  useEnqueueDocumentRecipeJob,
} from "@/lib/queries";
import { cn } from "@/lib/utils";

const PHASES: Array<{ phase: IngestionJobPhase; label: I18nKey; shortLabel: I18nKey }> = [
  {
    phase: "PREPROCESS",
    label: "flow.step.preprocess",
    shortLabel: "documents.recipes.phase.preprocess",
  },
  {
    phase: "EXTRACT",
    label: "flow.step.extract",
    shortLabel: "documents.recipes.phase.extract",
  },
  {
    phase: "CHUNK",
    label: "flow.step.chunk",
    shortLabel: "documents.recipes.phase.chunk",
  },
  {
    phase: "INDEX",
    label: "flow.step.indexing",
    shortLabel: "documents.recipes.phase.index",
  },
];

type AddMode = "clone" | "defaults";

export function DocumentRecipeManager({
  documentId,
  recipes,
  selectedRecipeId,
  onSelect,
  loading,
  error,
  onRetry,
  chunkSets,
  sourceModality = null,
  approveBlocked = false,
}: {
  documentId: string;
  recipes: DocumentRecipeView[];
  selectedRecipeId: string | null;
  onSelect: (recipeId: string) => void;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  chunkSets?: DocumentChunkSet[];
  sourceModality?: string | null;
  /** 選んでいるレシピの承認を止める（抽出確認の未保存の修正がある。#944）。 */
  approveBlocked?: boolean;
}) {
  const selected = resolveSelectedRecipe(recipes, selectedRecipeId);
  const createRecipe = useCreateDocumentRecipe();
  const deleteRecipe = useDeleteDocumentRecipe();
  const enqueue = useEnqueueDocumentRecipeJob();
  const approve = useApproveDocumentRecipe();
  // 失敗は、失敗した操作のレシピを選んでいるときだけ出す（別のレシピの失敗として見せない。#944）。
  // 設定を保存した・処理が進んだ後は、前の失敗を出し続けない（#2639）。
  const processError = useRecipeOperationError(
    selected
      ? (enqueue.variables?.recipeId === selected.recipe_id ? enqueue.error : null) ??
          (approve.variables?.recipeId === selected.recipe_id ? approve.error : null)
      : null,
    selected
  );
  const confirm = useConfirm();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [addMode, setAddMode] = useState<AddMode>("clone");
  const configData = useMemo(
    () => (selected ? recipeConfigData(selected) : null),
    [selected]
  );

  const openAddDialog = () => {
    setAddMode("clone");
    dialogRef.current?.showModal();
  };

  const active = selected ? recipeIsActive(selected) : false;
  const atMaximum = !canAddRecipe(recipes.length);
  const atMinimum = recipes.length <= 1;

  const handleCreate = () => {
    createRecipe.mutate(
      {
        id: documentId,
        copyFrom: addMode === "clone" ? selected?.recipe_id ?? null : null,
      },
      {
        onSuccess: (recipe) => {
          dialogRef.current?.close();
          onSelect(recipe.recipe_id);
        },
      }
    );
  };

  const handleDelete = async () => {
    if (!selected || !canDeleteRecipe(recipes.length, active)) return;
    const confirmed = await confirm({
      title: t("documents.recipes.deleteTitle"),
      description: t("documents.recipes.deleteDescription"),
      confirmLabel: t("common.delete"),
      tone: "danger",
    });
    if (!confirmed) return;
    deleteRecipe.mutate(
      { id: documentId, recipeId: selected.recipe_id },
      {
        onSuccess: () => {
          const fallback = recipes.find((recipe) => recipe.recipe_id !== selected.recipe_id);
          if (fallback) onSelect(fallback.recipe_id);
        },
      }
    );
  };

  const handleProcess = () => {
    if (!selected || active) return;
    if (["PREPROCESSED", "REVIEW", "CHUNKED"].includes(selected.status)) {
      if (approveBlocked) return;
      approve.mutate({ id: documentId, recipeId: selected.recipe_id });
      return;
    }
    enqueue.mutate({
      id: documentId,
      recipeId: selected.recipe_id,
      phase: selected.failed_phase ?? "PREPROCESS",
    });
  };

  if (loading) {
    return (
      <section aria-label={t("documents.recipes.title")} className="space-y-3">
        <TimedLoadingState
          label={t("documents.recipes.loading")}
          operationKey="document-recipes-load"
          framed={false}
          testId="document-recipes-loading"
        >
          <Skeleton className="h-9 w-48" />
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            <Skeleton className="h-28" />
            <Skeleton className="hidden h-28 sm:block" />
          </div>
        </TimedLoadingState>
      </section>
    );
  }

  if (error) {
    return (
      <Banner severity="warning" title={t("documents.recipes.loadError")}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p>{error instanceof ApiError ? error.message : t("flow.buildConfig.loadErrorHint")}</p>
          <Button type="button" variant="secondary" size="sm" onClick={onRetry} icon={RotateCcw}>
            {t("common.retry")}
          </Button>
        </div>
      </Banner>
    );
  }

  if (!selected) {
    return <FormStatus tone="info" message={t("documents.recipes.empty")} />;
  }

  // 押したレシピの操作だけを回す。処理中に別のレシピを選んでも、そのレシピの操作は回さない（#819）。
  const processPending =
    (enqueue.isPending && enqueue.variables?.recipeId === selected.recipe_id) ||
    (approve.isPending && approve.variables?.recipeId === selected.recipe_id);
  const deletePending =
    deleteRecipe.isPending && deleteRecipe.variables?.recipeId === selected.recipe_id;
  const recipeOperationPending = enqueue.isPending || approve.isPending || deleteRecipe.isPending;
  // 削除の失敗（処理中・最少 1 件など）も操作の近くに出す。以前は確認ダイアログを閉じた後に何も出なかった（#281）。
  const deleteError =
    deleteRecipe.variables?.recipeId === selected.recipe_id ? deleteRecipe.error : null;
  const layerStatuses = recipeLayerStatuses(selected, chunkSets);
  const layerRebuild = recipeLayerRebuildSummary(layerStatuses);
  // 選択中のレシピの操作（buttons.md §5.1）。処理は非破壊の高頻度操作として表示し、
  // 削除は danger として「その他の操作」に入れ、確認ダイアログ（handleDelete）を通す。
  const recipeActions: EntityAction[] = [
    {
      id: "process",
      label: processButtonLabel(selected),
      icon: selected.status === "ERROR" ? RotateCcw : Play,
      loading: processPending,
      disabled:
        active ||
        (recipeOperationPending && !processPending) ||
        (approveBlocked && ["PREPROCESSED", "REVIEW", "CHUNKED"].includes(selected.status)),
      onSelect: handleProcess,
    },
    {
      id: "delete",
      label: t("documents.recipes.delete"),
      ariaLabel: atMinimum ? t("documents.recipes.deleteDisabledMin") : undefined,
      icon: Trash2,
      tone: "danger",
      loading: deletePending,
      disabled: atMinimum || active || (recipeOperationPending && !deletePending),
      onSelect: handleDelete,
    },
  ];

  return (
    <section aria-label={t("documents.recipes.title")} className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Settings2 size={16} className="text-accent-fg" aria-hidden />
            <h2 className="text-sm font-semibold text-fg">
              {t("documents.recipes.title")}
            </h2>
            <span data-surface-tint="neutral" className="tnum rounded-md bg-surface-hover px-2 py-0.5 text-xs font-medium text-fg-muted">
              {t("documents.recipes.count", { count: recipes.length })}
            </span>
          </div>
          <p className="mt-1 text-xs text-fg-muted">
            {atMaximum ? t("documents.recipes.max") : t("documents.recipes.subtitle")}
          </p>
        </div>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={openAddDialog}
          disabled={atMaximum}
          title={atMaximum ? t("documents.recipes.max") : undefined} icon={Plus}>
          {t("documents.recipes.add")}
        </Button>
      </div>

      <div className="sm:hidden">
        <SelectField
          id={`recipe-select-${documentId}`}
          width="lg"
          label={t("documents.recipes.select")}
          value={selected.recipe_id}
          options={recipes.map((recipe) => ({
            value: recipe.recipe_id,
            label: `${recipeName(recipe)} · ${recipeStatus(recipe).text}`,
          }))}
          onValueChange={onSelect}
        />
      </div>

      <div className="hidden gap-3 sm:grid sm:grid-cols-2 xl:grid-cols-3">
        {recipes.map((recipe) => (
          <RecipeCard
            key={recipe.recipe_id}
            recipe={recipe}
            selected={recipe.recipe_id === selected.recipe_id}
            onSelect={() => onSelect(recipe.recipe_id)}
          />
        ))}
      </div>

      <div className="rounded-lg border border-border bg-surface p-3 sm:p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold text-fg">{recipeName(selected)}</h3>
              {/* 実行中の工程のスピナーは下の工程の丸が出す（同じ処理のスピナーは 1 つ。#416）。 */}
              <RecipeStatusBadge recipe={selected} spin={false} />
              {selected.needs_reprocessing ? (
                <span data-surface-tint="warning" className="rounded-full bg-warning-subtle px-2 py-0.5 text-xs font-medium text-warning-fg">
                  {t("documents.recipes.reprocess")}
                </span>
              ) : null}
            </div>
            <RecipeLayerStatusChips statuses={layerStatuses} />
            <p className="mt-1 text-xs text-fg-muted">
              {t("documents.recipes.updated", { time: formatDateTime(selected.updated_at) })}
            </p>
          </div>
          <ObjectActionBar
            actions={recipeActions}
            ariaLabel={t("common.objectActions.aria", { name: recipeName(selected) })}
            moreLabel={t("common.objectActions.more")}
            testId="document-recipe-actions"
          />
        </div>

        <RecipeSteps recipe={selected} />
        <RecipeQueueWaitNote recipe={selected} />

        {/* 作成後に項目の定義などが変わったレイヤー（#550）。自動では作り直さず、既存の「再処理」へ案内する。 */}
        {layerRebuild.layers.length ? (
          <div className="mt-3" data-testid="recipe-layer-rebuild">
            <Banner severity="warning">
              {t("documents.recipes.layerRebuildBanner", {
                layers: layerRebuild.layers
                  .map((layer) => t(LAYER_LABEL_KEYS[layer]))
                  .join(t("documents.recipes.layerRebuildJoin")),
                inputs: rebuildInputLabels(layerRebuild.inputs),
              })}
            </Banner>
          </div>
        ) : null}

        {/* 失敗の原因本文は上部の状態メッセージスロット(DocumentWorkspace)が正本(messaging-spec §9 P2)。
            ここは「検索は旧出力で継続中」という状況提示のみ残す。再処理の job が順番待ち・実行中の間は、
            前回の失敗を出さない(#2687)。 */}
        {selected.status === "ERROR" && selected.searchable && !recipeIsActive(selected) ? (
          <Banner severity="warning" className="mt-3">
            {t("documents.recipes.staleError")}
          </Banner>
        ) : null}
        {processError ? (
          <FormStatus
            tone="danger"
            message={processError instanceof ApiError ? processError.message : t("flow.ingestFailed")}
          />
        ) : null}
        {deleteError ? (
          <FormStatus
            tone="danger"
            message={
              deleteError instanceof ApiError
                ? deleteError.message
                : t("documents.recipes.deleteFailed")
            }
          />
        ) : null}

        <div className="mt-4">
          <DocumentProcessingConfigPanel
            documentId={documentId}
            recipeId={selected.recipe_id}
            data={configData}
            loading={false}
            error={null}
            onRetry={onRetry}
            disabled={recipeConfigLocked(selected)}
            sourceModality={sourceModality}
          />
        </div>
        <RecipeComparison documentId={documentId} recipes={recipes} />
      </div>

      <dialog
        ref={dialogRef}
        className="m-auto w-[calc(100%-2rem)] max-w-lg rounded-xl border border-border bg-surface-overlay p-0 text-fg shadow-[var(--shadow-dialog)] backdrop:bg-[var(--scrim)]"
        onClose={() => setAddMode("clone")}
        onClick={(event) => {
          if (event.target === event.currentTarget) dialogRef.current?.close();
        }}
      >
        <div className="p-5">
          <h2 className="text-base font-semibold">{t("documents.recipes.addTitle")}</h2>
          <p className="mt-1 text-sm text-fg-muted">{t("documents.recipes.addDescription")}</p>
          <div
            role="radiogroup"
            aria-label={t("documents.recipes.addTitle")}
            className="mt-4 grid gap-2"
          >
            <AddModeOption
              value="clone"
              selected={addMode === "clone"}
              icon={<Copy size={16} aria-hidden />}
              label={t("documents.recipes.clone")}
              onSelect={() => setAddMode("clone")}
            />
            <AddModeOption
              value="defaults"
              selected={addMode === "defaults"}
              icon={<Settings2 size={16} aria-hidden />}
              label={t("documents.recipes.defaults")}
              onSelect={() => setAddMode("defaults")}
            />
          </div>
          {createRecipe.error ? (
            <FormStatus
              tone="danger"
              message={
                createRecipe.error instanceof ApiError
                  ? createRecipe.error.message
                  : t("flow.ingestFailed")
              }
            />
          ) : null}
          <div className="mt-5 flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => dialogRef.current?.close()}>
              {t("common.cancel")}
            </Button>
            <Button type="button" onClick={handleCreate} loading={createRecipe.isPending} icon={Plus}>
              {t("documents.recipes.create")}
            </Button>
          </div>
        </div>
      </dialog>
    </section>
  );
}

function RecipeCard({
  recipe,
  selected,
  onSelect,
}: {
  recipe: DocumentRecipeView;
  selected: boolean;
  onSelect: () => void;
}) {
  const completed = completedStepCount(recipe);
  return (
    <button
      data-surface-tint={selected ? "accent" : undefined}
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className={cn(
        "min-w-0 rounded-lg border p-3 text-left transition-colors",
        selected
          ? "border-accent-emphasis bg-accent-subtle shadow-sm"
          : "border-border bg-surface hover:border-accent-emphasis hover:bg-surface-hover"
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-semibold text-fg">{recipeName(recipe)}</span>
        <span className="tnum text-xs font-medium text-fg-muted">{completed}/4</span>
      </div>
      <div className="mt-2 flex items-center gap-2">
        {/* 選んでいるレシピは、下の詳細の工程の丸がスピナーを出す（同じ処理のスピナーは 1 つ。#416）。 */}
        <RecipeStatusBadge recipe={recipe} spin={!selected} />
      </div>
      {/* 完了した工程の数は上の「{completed}/4」で読める（role のない div の aria-label は読み上げられない）。 */}
      <div className="mt-3 grid grid-cols-4 gap-1">
        {PHASES.map(({ phase }) => {
          const step = recipe.steps.find((item) => item.phase === phase);
          return (
            <span
              key={phase}
              className={cn(
                "h-1.5 rounded-full",
                step?.status === "FAILED"
                  ? "bg-danger-emphasis"
                  : step?.status === "RUNNING"
                    ? "bg-info-emphasis"
                    : step?.status === "SUCCEEDED"
                      ? "bg-success-emphasis"
                      : step?.status === "NEEDS_REVIEW"
                        ? "bg-warning-emphasis"
                        : "bg-border"
              )}
            />
          );
        })}
      </div>
    </button>
  );
}

function RecipeSteps({ recipe }: { recipe: DocumentRecipeView }) {
  return (
    <ol className="mt-4 grid grid-cols-4 gap-1" aria-label={t("documents.recipes.title")}>
      {PHASES.map(({ phase, label, shortLabel }, index) => {
        const step = recipe.steps.find((item) => item.phase === phase);
        return (
          <li key={phase} className="relative min-w-0 text-center">
            {index > 0 ? (
              <span className="absolute right-1/2 top-3 h-px w-full bg-border" aria-hidden />
            ) : null}
            <span
              className={cn(
                "relative z-10 mx-auto flex size-6 items-center justify-center rounded-full border bg-surface",
                stepTone(step)
              )}
            >
              <StepIcon step={step} />
            </span>
            <span className="mt-1.5 block min-h-7 px-0.5 text-xs leading-3 text-fg-muted sm:text-xs sm:leading-4">
              <span className="sm:hidden">{t(shortLabel)}</span>
              <span className="hidden sm:inline">{t(label)}</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/**
 * 順番待ちの工程の説明（#2026）: 「〈工程〉の順番待ち · 前に N 件 · 同時に処理できる数（M）の空きを待っています ·
 * 経過 mm:ss」。人がすることは無く、システムが worker の空きを待っていることを、理由と順番で示す。
 * 経過は毎秒変わるため live region にしない（状態の変化はバッジが示す）。
 */
function RecipeQueueWaitNote({ recipe }: { recipe: DocumentRecipeView }) {
  const wait = recipeQueueWait(recipe);
  if (!wait) return null;
  return <QueueWaitNote wait={wait} />;
}

function QueueWaitNote({ wait }: { wait: QueueWaitView }) {
  const nowMs = useNowMs(wait.queuedAt !== null, wait.queuedAt);
  const ahead = queueAheadText(wait);
  const reason = queueReasonText(wait);
  const elapsed = queueElapsedText(wait, nowMs);
  return (
    <p
      className="mt-3 flex items-start gap-1.5 text-xs text-fg-muted"
      data-testid="recipe-queue-wait"
    >
      <span className="pr-icon-line text-info-fg">
        <Clock3 size={12} aria-hidden />
      </span>
      <span className="min-w-0">
        <span className="font-medium text-fg">{queueWaitLabel(wait.phase)}</span>
        {[ahead, reason].map((part) =>
          part ? (
            <span key={part}>
              <span aria-hidden> · </span>
              {part}
            </span>
          ) : null
        )}
        {elapsed ? (
          <span className="tnum" data-testid="recipe-queue-wait-elapsed">
            <span aria-hidden> · </span>
            {elapsed}
          </span>
        ) : null}
      </span>
    </p>
  );
}

function StepIcon({ step }: { step: DocumentRecipeStep | undefined }) {
  if (step?.status === "RUNNING") {
    return <Spinner size={14} />;
  }
  if (step?.status === "FAILED") return <AlertCircle size={14} aria-hidden />;
  if (step?.status === "NEEDS_REVIEW") return <Eye size={14} aria-hidden />;
  // 順番待ち（worker の空きを待っている。#2026）。人の確認待ち（Eye）と分ける。
  if (step?.status === "QUEUED") return <Clock3 size={14} aria-hidden />;
  if (step?.status === "SUCCEEDED") return <Check size={14} aria-hidden />;
  return <Circle size={14} aria-hidden />;
}

function RecipeStatusBadge({ recipe, spin = true }: { recipe: DocumentRecipeView; spin?: boolean }) {
  const status = recipeStatus(recipe);
  // 実行中で回さないときは、実行中を示す静止アイコン（Play。status.icon）にする。
  const Icon = status.icon;
  return (
    <span
      data-surface-tint={surfaceTintFor(status.className)}
      className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium", status.className)}
    >
      {/* 回すアイコンは共有の Spinner（全周トラック・180 度対称のアーク・reduced-motion 対応。#395 / #1180） */}
      {status.spin && spin ? <Spinner size={14} /> : <Icon size={14} aria-hidden />}
      {status.text}
    </span>
  );
}

const LAYER_LABEL_KEYS: Record<RecipeLayerStatusView["layer"], I18nKey> = {
  metadata: "knowledgeBases.variant.layer.metadata",
  graph: "knowledgeBases.variant.layer.graph",
  navigation: "knowledgeBases.variant.layer.navigation",
};

const LAYER_STATUS_LABEL_KEYS: Record<DocumentLayerStatusName, I18nKey> = {
  not_requested: "knowledgeBases.variant.layerStatus.not_requested",
  planned_only: "knowledgeBases.variant.layerStatus.planned_only",
  materialized: "knowledgeBases.variant.layerStatus.materialized",
  needs_reingest: "knowledgeBases.variant.layerStatus.needs_reingest",
  error: "knowledgeBases.variant.layerStatus.error",
};

const LAYER_STATUS_TONES: Record<DocumentLayerStatusName, string> = {
  not_requested: "bg-surface-hover text-fg-muted",
  planned_only: "bg-warning-subtle text-warning-fg",
  materialized: "bg-success-subtle text-success-fg",
  needs_reingest: "bg-warning-subtle text-warning-fg",
  error: "bg-danger-subtle text-danger-fg",
};

/** 派生 layer(項目抽出/関係情報/ナビ)の実体化状態チップ。reason は title で開示する。 */
function RecipeLayerStatusChips({ statuses }: { statuses: RecipeLayerStatusView[] }) {
  if (!statuses.length) return null;
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5" data-testid="recipe-layer-statuses">
      {statuses.map((entry) => (
        <span
          key={entry.layer}
          data-surface-tint={entry.rebuildRequired ? "warning" : surfaceTintFor(LAYER_STATUS_TONES[entry.status])}
          title={layerChipTitle(entry)}
          data-testid={`recipe-layer-${entry.layer}`}
          data-rebuild-required={entry.rebuildRequired ? "true" : undefined}
          className={cn(
            "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
            entry.rebuildRequired
              ? "bg-warning-subtle text-warning-fg"
              : LAYER_STATUS_TONES[entry.status]
          )}
        >
          {/* 作り直しの印は色だけに頼らず、アイコンと文言でも示す。 */}
          {entry.rebuildRequired ? <RefreshCw size={14} aria-hidden /> : null}
          {t(LAYER_LABEL_KEYS[entry.layer])}
          <span aria-hidden>·</span>
          {t(LAYER_STATUS_LABEL_KEYS[entry.status])}
          {entry.rebuildRequired ? (
            <>
              <span aria-hidden>·</span>
              {t("documents.recipes.layerRebuild")}
            </>
          ) : null}
        </span>
      ))}
    </div>
  );
}

const REBUILD_INPUT_LABEL_KEYS: Record<DocumentLayerRebuildInput, I18nKey> = {
  field_schema_hash: "documents.recipes.layerRebuildInput.field_schema_hash",
  chunk_metadata_contract: "documents.recipes.layerRebuildInput.chunk_metadata_contract",
  navigation_summary_max_nodes: "documents.recipes.layerRebuildInput.navigation_summary_max_nodes",
};

/** 変わった入力の表示名（知らない入力は「作成時の設定」にまとめる）。 */
function rebuildInputLabels(inputs: string[]) {
  const labels = inputs.map((input) =>
    t(
      REBUILD_INPUT_LABEL_KEYS[input as DocumentLayerRebuildInput] ??
        "documents.recipes.layerRebuildInput.unknown"
    )
  );
  return [...new Set(labels)].join(t("documents.recipes.layerRebuildJoin")) ||
    t("documents.recipes.layerRebuildInput.unknown");
}

function layerChipTitle(entry: RecipeLayerStatusView) {
  if (!entry.rebuildRequired) return entry.reason ?? undefined;
  const rebuild = t("documents.recipes.layerRebuildTitle", {
    inputs: rebuildInputLabels(entry.rebuildInputs),
  });
  return entry.reason ? `${entry.reason}\n${rebuild}` : rebuild;
}

function recipeStatus(recipe: DocumentRecipeView) {
  const stepStatuses = recipe.steps.map((step) => step.status);
  if (stepStatuses.includes("RUNNING")) {
    return {
      text: t("documents.recipes.status.running"),
      // 回すときは Spinner（RecipeStatusBadge）。これは回さないときの静止アイコン。
      icon: Play,
      className: "bg-info-subtle text-info-fg",
      spin: true,
    };
  }
  const queueWait = recipeQueueWait(recipe);
  if (queueWait) {
    // worker の空きを待っている（#2026）。確認待ち（warning）でなく処理中の側（info）の「〈工程〉の順番待ち」。
    return {
      text: queueWaitLabel(queueWait.phase),
      icon: Clock3,
      className: "bg-info-subtle text-info-fg",
      spin: false,
    };
  }
  if (recipe.status === "ERROR") {
    return {
      text: t("documents.recipes.status.error"),
      icon: AlertCircle,
      className: "bg-danger-subtle text-danger-fg",
      spin: false,
    };
  }
  if (["PREPROCESSED", "REVIEW", "CHUNKED"].includes(recipe.status)) {
    return {
      text: t("documents.recipes.status.review"),
      icon: Clock3,
      className: "bg-warning-subtle text-warning-fg",
      spin: false,
    };
  }
  if (recipe.searchable) {
    return {
      text: t("documents.recipes.status.searchable"),
      icon: SearchCheck,
      className: "bg-success-subtle text-success-fg",
      spin: false,
    };
  }
  return {
    text: t("documents.recipes.status.idle"),
    icon: Circle,
    className: "bg-surface-hover text-fg-muted",
    spin: false,
  };
}

function stepTone(step: DocumentRecipeStep | undefined) {
  if (step?.status === "FAILED") return "border-danger-fg text-danger-fg";
  if (step?.status === "RUNNING") return "border-info-fg text-info-fg";
  if (step?.status === "NEEDS_REVIEW") return "border-warning-fg text-warning-fg";
  if (step?.status === "QUEUED") return "border-info-fg text-info-fg";
  if (step?.status === "SUCCEEDED") return "border-success-fg bg-success-emphasis text-fg-on-emphasis";
  return "border-border text-fg-muted";
}

function completedStepCount(recipe: DocumentRecipeView) {
  // NEEDS_REVIEW は「工程完了・承認待ち」なので完了数に含める。
  return recipe.steps.filter(
    (step) => step.status === "SUCCEEDED" || step.status === "NEEDS_REVIEW"
  ).length;
}

function recipeName(recipe: DocumentRecipeView) {
  return t("documents.recipes.name", { slot: recipe.slot_no });
}

function processButtonLabel(recipe: DocumentRecipeView) {
  if (recipeIsActive(recipe)) return t("documents.recipes.status.running");
  if (recipe.status === "ERROR") return t("documents.recipes.retry");
  if (recipe.searchable) return t("documents.recipes.reprocessAction");
  if (["PREPROCESSED", "REVIEW", "CHUNKED"].includes(recipe.status)) {
    return t("documents.recipes.resume");
  }
  return t("documents.recipes.run");
}

function recipeConfigData(recipe: DocumentRecipeView): DocumentProcessingConfigData {
  return {
    processing_config: recipe.processing_config,
    effective_processing_config: recipe.effective_processing_config,
  };
}

function RecipeComparison({
  documentId,
  recipes,
}: {
  documentId: string;
  recipes: DocumentRecipeView[];
}) {
  const searchable = recipes.filter((recipe) => recipe.searchable && recipe.active_chunk_set_id);
  const [open, setOpen] = useState(false);
  const [leftId, setLeftId] = useState(searchable[0]?.recipe_id ?? "");
  const [rightId, setRightId] = useState(searchable[1]?.recipe_id ?? "");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<{
    left: SearchResponse;
    right: SearchResponse;
  } | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  const options = searchable.map((recipe) => ({
    value: recipe.recipe_id,
    label: recipeName(recipe),
  }));
  const left = searchable.find((recipe) => recipe.recipe_id === leftId) ?? searchable[0];
  const right =
    searchable.find((recipe) => recipe.recipe_id === rightId) ?? searchable[1] ?? searchable[0];

  const run = async () => {
    if (pending || !left?.active_chunk_set_id || !right?.active_chunk_set_id || !query.trim()) {
      return;
    }
    setPending(true);
    setError("");
    setResults(null);
    try {
      // 引用の候補を比べるので、回答は作らずに検索だけを行う（LLM を呼ばない。#593）。
      const [leftResult, rightResult] = await Promise.all([
        api.search({
          query: query.trim(),
          top_k: 5,
          retrieval_only: true,
          filters: { document_id: documentId, chunk_set_id: left.active_chunk_set_id },
        }),
        api.search({
          query: query.trim(),
          top_k: 5,
          retrieval_only: true,
          filters: { document_id: documentId, chunk_set_id: right.active_chunk_set_id },
        }),
      ]);
      setResults({ left: leftResult, right: rightResult });
    } catch (reason) {
      setError(
        reason instanceof ApiError ? reason.message : t("documents.experiment.compare.error")
      );
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="mt-4 border-t border-border pt-4">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => setOpen((value) => !value)}
        disabled={searchable.length < 2} icon={Search}>
        {t("documents.experiment.compare.title")}
      </Button>
      {searchable.length < 2 ? (
        <p className="mt-1 text-xs text-fg-muted">{t("documents.recipes.compareNeedsTwo")}</p>
      ) : null}
      {open ? (
        <div className="mt-3 space-y-3 rounded-lg border border-border bg-surface-sunken p-3">
          {/* 比べる 2 つの処理レシピは対象の選択のペア（lg。#1891）。 */}
          <FormGrid>
            <SelectField
              id={`recipe-compare-left-${documentId}`}
              width="lg"
              label={t("documents.recipes.compareLeft")}
              value={left?.recipe_id ?? ""}
              options={options}
              onValueChange={setLeftId}
            />
            <SelectField
              id={`recipe-compare-right-${documentId}`}
              width="lg"
              label={t("documents.recipes.compareRight")}
              value={right?.recipe_id ?? ""}
              options={options}
              onValueChange={setRightId}
            />
          </FormGrid>
          {/* 質問の欄とその実行は FieldActionRow（操作は欄の下端にそろえ、375px では欄の下に全幅。#1891）。 */}
          <FieldActionRow
            actions={
              <Button
                type="button"
                icon={Play}
                onClick={() => void run()}
                loading={pending}
                disabled={!query.trim() || left?.recipe_id === right?.recipe_id}
              >
                {t("documents.experiment.compare.run")}
              </Button>
            }
          >
            <TextField
              id="recipe-compare-query"
              width="full"
              label={t("documents.experiment.compare.queryLabel")}
              required
              value={query}
              onValueChange={setQuery}
              placeholder={t("documents.experiment.compare.placeholder")}
              onKeyDown={(event) => {
                if (isSubmitEnter(event)) void run();
              }}
            />
          </FieldActionRow>
          {error ? <FormStatus tone="danger" message={error} /> : null}
          {results ? (
            <div className="grid gap-4 lg:grid-cols-2">
              <ComparisonColumn
                title={left ? recipeName(left) : ""}
                result={results.left}
              />
              <ComparisonColumn
                title={right ? recipeName(right) : ""}
                result={results.right}
              />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ComparisonColumn({ title, result }: { title: string; result: SearchResponse }) {
  const chunks = result.citations;
  return (
    <section className="min-w-0">
      <h4 className="mb-2 text-sm font-semibold text-fg">{title}</h4>
      {chunks.length ? (
        <ol className="space-y-2">
          {chunks.map((chunk, index) => (
            <CitationCard
              key={`${chunk.chunk_id}-${index}`}
              chunk={chunk}
              index={index}
            />
          ))}
        </ol>
      ) : (
        <div aria-live="polite">
          <EmptyState
            title={t("search.noResults")}
            hint={result.answer || t("search.noResultsHint")}
          />
        </div>
      )}
    </section>
  );
}

function AddModeOption({
  value,
  selected,
  icon,
  label,
  onSelect,
}: {
  value: string;
  selected: boolean;
  icon: ReactNode;
  label: string;
  onSelect: () => void;
}) {
  const id = `recipe-add-mode-${value}`;
  // 本物のラジオにして、矢印キーの移動と Tab の 1 回の停止をブラウザに任せる（#469）。
  return (
    <div className="relative min-w-0">
      <input
        id={id}
        className="peer absolute inset-0 z-10 cursor-pointer opacity-0 disabled:cursor-not-allowed"
        type="radio"
        name="recipe-add-mode"
        value={value}
        checked={selected}
        onChange={onSelect}
      />
      <label
        data-surface-tint={selected ? "accent" : undefined}
        htmlFor={id}
        className={cn(
          "flex min-h-12 cursor-pointer items-center gap-3 rounded-lg border px-3 text-left text-sm font-medium peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-focus-ring",
          selected ? "border-accent-emphasis bg-accent-subtle text-fg" : "border-border text-fg-muted"
        )}
      >
        <span data-surface-tint={selected ? "accent" : "neutral"} className={cn("flex size-8 items-center justify-center rounded-md", selected ? "bg-accent-subtle text-accent-fg" : "bg-surface-hover")}>
          {icon}
        </span>
        <span className="flex-1">{label}</span>
        {selected ? <CheckCircle2 size={16} className="text-accent-fg" aria-hidden /> : null}
      </label>
    </div>
  );
}
