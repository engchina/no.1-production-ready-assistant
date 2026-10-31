import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { Button, ButtonLink, Card, CardContent, CardHeader, CardTitle, Banner, FormStatus, SelectField, Skeleton, TextareaField, TimedLoadingState, useConfirm, StatusBadge } from "@production-ready/ui";

import { api, type IngestionJobPhase, type LanguagePackDocumentUse, type LanguagePackRelation } from "@/lib/api";
import { t } from "@/lib/i18n";
import { ApiErrorState } from "@/components/StateViews";
import { LanguagePackComparison } from "@/components/settings/LanguagePackHistoryCard";
import { formatDateTime } from "@/lib/format";
import { APP_ROUTES } from "@/lib/routes";
import { phaseLabelKey } from "./DocumentWorkspace.logic";

export function LanguagePackRelationBadge({ relation }: { relation: LanguagePackRelation }) {
  return <StatusBadge variant={relation === "different" ? "warning" : relation === "unrecorded" ? "neutral" : "success"} label={t(`settings.language.release.relation.${relation}`)} icon={false} />;
}

/** 版番号だけから再処理の必要性を推測せず、各有効レシピの実際の使用版を示す。 */
export function LanguagePackUseSummary({ use, documentId }: { use?: LanguagePackDocumentUse | null; documentId: string }) {
  if (!use || use.recipes.length === 0) return <span className="text-xs text-fg-muted">{t("settings.language.release.relation.unrecorded")}</span>;
  const active = use.recipes.filter((recipe) => recipe.chunk_set_id !== null);
  const versions = [...new Set(active.map((recipe) => recipe.version).filter((version): version is number => version != null))];
  const relations = [...new Set(active.map((recipe) => recipe.relation))];
  return <div className="space-y-2">
    <span className="block text-sm">{versions.length > 1 ? t("settings.language.release.multiple") : versions.length ? `v${versions[0]}` : t("settings.language.release.relation.unrecorded")}</span>
    {relations.map((relation) => <LanguagePackRelationBadge key={relation} relation={relation} />)}
    <ButtonLink size="sm" variant="secondary" to={`${APP_ROUTES.documents}/${encodeURIComponent(documentId)}?language_pack=1`}>{t("settings.language.release.breakdown")}</ButtonLink>
  </div>;
}

export function LanguagePackDocumentPanel(props: { documentId: string; fileName: string; use?: LanguagePackDocumentUse | null; recipe: import("@/lib/api").DocumentRecipeView | null }) {
  if (!props.use || !props.recipe) return null;
  return <RecipeLanguagePackPanel {...props} use={props.use} recipe={props.recipe} />;
}

function RecipeLanguagePackPanel({ documentId, fileName, use, recipe }: { documentId: string; fileName: string; use: LanguagePackDocumentUse; recipe: import("@/lib/api").DocumentRecipeView }) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [showSnapshot, setShowSnapshot] = useState<string | null>(null);
  const [review, setReview] = useState<string | null>(null);
  const [phase, setPhase] = useState<IngestionJobPhase | "">("");
  const active = use.recipes.find((item) => item.recipe_id === recipe.recipe_id && item.chunk_set_id !== null);
  const building = use.recipes.find((item) => item.recipe_id === recipe.recipe_id && item.target_release_id);
  const reviewKey = `${recipe.recipe_id}:${active?.chunk_set_id ?? ""}:${use.current_release_id}`;
  const reviewed = review === reviewKey;
  const snapshot = useQuery({ queryKey: ["language-pack-release", showSnapshot], queryFn: () => api.getLanguagePackRelease(showSnapshot!), enabled: Boolean(showSnapshot), retry: false });
  const comparison = useQuery({ queryKey: ["language-pack-comparison", active?.release_id, use.current_release_id], queryFn: () => api.compareLanguagePackReleases(active!.release_id!, use.current_release_id), enabled: reviewed && Boolean(active?.release_id), retry: false });
  const processing = Boolean(building?.target_release_id) || recipe.steps.some((step) => step.status === "RUNNING" || step.status === "QUEUED");
  const enqueue = useMutation({ mutationFn: (start: IngestionJobPhase) => api.enqueueDocumentRecipeJob(documentId, recipe.recipe_id, start, use.current_release_id, recipe.config_revision), onSuccess: () => { void qc.invalidateQueries({ queryKey: ["documents", documentId] }); } });
  const execute = async () => {
    if (!phase || !reviewed) return;
    const ok = await confirm({ title: t("settings.language.release.reprocessTitle"), description: t("settings.language.release.reprocessConfirm", { fileName, recipe: t("documents.recipes.name", { slot: recipe.slot_no }), version: use.current_version, phase: t(phaseLabelKey(phase)) }), confirmLabel: t("flow.reprocess.confirm"), tone: "warning" });
    if (ok) enqueue.mutate(phase);
  };
  return <Card data-testid="document-language-pack">
    <CardHeader><CardTitle>{t("settings.language.release.usedVersion")}</CardTitle></CardHeader>
    <CardContent className="space-y-4">
      <p className="text-sm">{t("documents.recipes.name", { slot: recipe.slot_no })} · {t("settings.language.release.current", { version: use.current_version })}</p>
      <div className="flex flex-wrap items-center gap-3">
        <span>{t("settings.language.release.activeIndex")} {active?.version ? `v${active.version}` : t("settings.language.release.relation.unrecorded")}</span>
        <LanguagePackRelationBadge relation={active?.relation ?? "unrecorded"} />
        {active?.release_id ? <Button size="sm" variant="secondary" onClick={() => setShowSnapshot(active.release_id)}>{t("settings.language.release.snapshot")}</Button> : null}
      </div>
      {active?.built_at ? <p className="text-xs text-fg-muted">{t("settings.language.release.builtAt")} {formatDateTime(active.built_at)}</p> : null}
      {active?.job_id ? <p className="break-all text-xs text-fg-muted">{t("settings.language.release.job")} {active.job_id}</p> : null}
      {building?.target_release_id ? <Banner severity="info" title={t("settings.language.release.building")}>
        <p>{t("settings.language.release.keepActive")}</p>
        <Button className="mt-3" variant="secondary" size="sm" onClick={() => setShowSnapshot(building.target_release_id)}>{t("settings.language.release.targetSnapshot")}</Button>
      </Banner> : null}
      {showSnapshot ? snapshot.isPending ? <TimedLoadingState label={t("settings.language.history.loading")} operationKey="document-language-release"><Skeleton className="h-40 w-full" /></TimedLoadingState> : snapshot.isError ? <ApiErrorState error={snapshot.error} fallback={t("settings.language.history.loadError")} onRetry={() => void snapshot.refetch()} /> : <TextareaField id={`recipe-${recipe.recipe_id}-language-snapshot`} label={`${t("settings.language.release.snapshot")} v${snapshot.data.version}`} rows={6} width="full" readOnly value={JSON.stringify(snapshot.data.effective, null, 2)} /> : null}
      <div><Button variant="secondary" onClick={() => setReview(reviewKey)}>{t("settings.language.release.review")}</Button></div>
      {reviewed && !active?.release_id ? <Banner severity="warning" title={t("settings.language.release.relation.unrecorded")}>{t("settings.language.release.unrecordedHelp")}</Banner> : null}
      {reviewed && active?.release_id ? comparison.isPending ? <TimedLoadingState label={t("settings.language.history.loading")} operationKey="document-language-diff"><Skeleton className="h-20 w-full" /></TimedLoadingState> : comparison.isError ? <ApiErrorState error={comparison.error} fallback={t("settings.language.history.loadError")} onRetry={() => void comparison.refetch()} /> : <LanguagePackComparison comparison={comparison.data} /> : null}
      {reviewed && (active?.relation === "different" || !active?.release_id) ? <>
        <SelectField id={`recipe-${recipe.recipe_id}-language-phase`} label={t("settings.language.release.startPhase")} value={phase} required options={[{ value: "", label: t("settings.language.release.selectPhase") }, ...(["PREPROCESS", "EXTRACT", "CHUNK", "INDEX"] as const).map((value) => ({ value, label: t(phaseLabelKey(value)) }))]} onValueChange={(value) => setPhase(value as IngestionJobPhase)} />
        <p className="text-xs text-fg-muted">{t("settings.language.release.processingHelp")}</p>
        <Button variant="secondary" icon={RefreshCw} disabled={!phase || processing || comparison.isError || comparison.isFetching} loading={enqueue.isPending} onClick={() => void execute()}>{t("flow.reprocess.confirm")}</Button>
      </> : null}
      {enqueue.isError ? <ApiErrorState error={enqueue.error} fallback={t("settings.language.saveError")} onRetry={() => { void qc.invalidateQueries({ queryKey: ["documents", documentId] }); setReview(null); enqueue.reset(); }} /> : null}
      {enqueue.isSuccess ? <FormStatus tone="success" message={t("settings.language.release.queued")} /> : null}
    </CardContent>
  </Card>;
}
