import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { Banner, Button, Card, CardContent, CardHeader, CardTitle, DataTable, DEFAULT_PAGE_SIZE, FormStatus, INFORMATION_TABLE_VISIBLE_ROWS, ListToolbar, Pagination, SelectField, Skeleton, TimedLoadingState, useConfirm, usePagination } from "@production-ready/ui";

import { ApiErrorState } from "@/components/StateViews";
import { api, type IngestionJobPhase } from "@/lib/api";
import { t } from "@/lib/i18n";
import { listCountLabels, paginationLabels } from "@/lib/pagination-labels";
import { useSelection } from "@/lib/useSelection";
import { LanguagePackComparison } from "@/components/settings/LanguagePackHistoryCard";
import { phaseLabelKey } from "./DocumentWorkspace.logic";

export function LanguagePackBulkReprocess({ disabled, expectedReleaseId, loadDocumentIds }: { disabled: boolean; expectedReleaseId?: string; loadDocumentIds: () => Promise<string[]> }) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const selection = useSelection<string>();
  const [phase, setPhase] = useState<IngestionJobPhase | "">("");
  const [reviewedKey, setReviewedKey] = useState<string | null>(null);
  const [size, setSize] = useState<number>(DEFAULT_PAGE_SIZE);
  const prepare = useMutation({ mutationFn: async () => api.prepareLanguagePackReprocessing(await loadDocumentIds(), expectedReleaseId!), onSuccess: () => selection.clear() });
  const rows = prepare.data?.targets ?? [];
  const paging = usePagination(rows, size);
  const selectedRows = rows.filter(row => selection.isSelected(`${row.document_id}:${row.recipe_id}`));
  const usedReleaseIds = [...new Set(selectedRows.flatMap(row => row.used_release_id ? [row.used_release_id] : []))].sort();
  const reviewKey = JSON.stringify([prepare.data?.current_release.id, selectedRows.map(row => [row.document_id, row.recipe_id, row.config_revision])]);
  const comparisons = useQuery({ queryKey: ["language-pack-bulk-comparisons", prepare.data?.current_release.id, usedReleaseIds], enabled: reviewedKey === reviewKey && selectedRows.length > 0, retry: false, queryFn: async () => {
    // 同じ使用版はまとめ、原文の比較を一度に大量送信しない。
    const results = [];
    for (const id of usedReleaseIds) results.push(await api.compareLanguagePackReleases(id, prepare.data!.current_release.id));
    return results;
  } });
  const reviewed = reviewedKey === reviewKey && comparisons.isSuccess && !comparisons.isFetching;
  const execute = useMutation({ mutationFn: async () => {
    if (!prepare.data || !phase) throw new Error(t("settings.language.release.selectPhase"));
    return api.enqueueLanguagePackReprocessing(prepare.data.current_release.id, rows.filter(row => selection.isSelected(`${row.document_id}:${row.recipe_id}`)).map(row => ({ document_id: row.document_id, recipe_id: row.recipe_id, phase, expected_config_revision: row.config_revision })));
  }, onSuccess: () => { void qc.invalidateQueries({ queryKey: ["documents"] }); selection.clear(); } });
  const submit = async () => {
    const targets = rows.filter(row => selection.isSelected(`${row.document_id}:${row.recipe_id}`));
    if (!phase || !prepare.data || !targets.length || !reviewed) return;
    const ok = await confirm({ title: t("settings.language.release.reprocessTitle"), description: t("settings.language.release.bulkConfirm", { documents: new Set(targets.map(row => row.document_id)).size, recipes: targets.length, version: prepare.data.current_release.version, phase: t(phaseLabelKey(phase)) }), confirmLabel: t("flow.reprocess.confirm"), tone: "warning" });
    if (ok) execute.mutate();
  };
  return <div className="space-y-4" data-testid="language-pack-bulk-reprocess">
    <Button variant="secondary" size="sm" icon={RefreshCw} disabled={disabled || !expectedReleaseId || execute.isPending} loading={prepare.isPending} onClick={() => { execute.reset(); prepare.mutate(); }}>{t("settings.language.release.prepareTargets")}</Button>
    {prepare.isError ? <ApiErrorState error={prepare.error} fallback={t("settings.language.saveError")} onRetry={() => prepare.mutate()} /> : null}
    {prepare.data ? <Card>
      <CardHeader><CardTitle>{t("settings.language.release.selectRecipes")}</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm">{t("settings.language.release.current", { version: prepare.data.current_release.version })}</p>
        {prepare.data.unavailable_count ? <Banner severity="warning" title={t("settings.language.release.unavailable", { count: prepare.data.unavailable_count })}>{t("settings.language.release.unavailableHelp")}</Banner> : null}
        <ListToolbar count={{ total: rows.length, labels: listCountLabels() }} />
        <DataTable ariaLabel={t("settings.language.release.selectRecipes")} rows={paging.pageItems} getRowKey={(row) => `${row.document_id}:${row.recipe_id}`} stickyHeader visibleRows={INFORMATION_TABLE_VISIBLE_ROWS} columns={[
          { key: "select", header: <input type="checkbox" aria-label={t("fileList.selectAllAria")} checked={paging.pageItems.length > 0 && paging.pageItems.every(row => selection.isSelected(`${row.document_id}:${row.recipe_id}`))} onChange={() => selection.toggleAll(paging.pageItems.map(row => `${row.document_id}:${row.recipe_id}`))} disabled={execute.isPending} />, render: row => <input type="checkbox" aria-label={`${t("fileList.selectRowAria")} ${row.file_name} ${t("documents.recipes.name", { slot: row.slot_no })}`} checked={selection.isSelected(`${row.document_id}:${row.recipe_id}`)} onChange={() => selection.toggle(`${row.document_id}:${row.recipe_id}`)} disabled={execute.isPending} /> },
          { key: "document", header: t("fileList.col.fileName"), rowHeader: true, render: row => row.file_name },
          { key: "recipe", header: t("settings.language.release.recipe"), render: row => `${t("documents.recipes.name", { slot: row.slot_no })} / r${row.config_revision}` },
        ]} />
        <Pagination page={paging.page} totalPages={paging.totalPages} onPageChange={paging.setPage} range={paging.range} pageSize={size} onPageSizeChange={setSize} pageSizeOptions={[10,25,50,100]} labels={paginationLabels()} />
        <div><Button variant="secondary" disabled={selection.count === 0 || execute.isPending} onClick={() => setReviewedKey(reviewKey)}>{t("settings.language.release.reviewSelected")}</Button></div>
        {reviewedKey === reviewKey && comparisons.isPending && selection.count > 0 ? <TimedLoadingState label={t("settings.language.history.loading")} operationKey="language-bulk-diff"><Skeleton className="h-20 w-full" /></TimedLoadingState> : null}
        {reviewedKey === reviewKey && comparisons.isError ? <ApiErrorState error={comparisons.error} fallback={t("settings.language.history.loadError")} onRetry={() => void comparisons.refetch()} /> : null}
        {reviewed ? <>{selectedRows.some(row => !row.used_release_id) ? <Banner severity="warning" title={t("settings.language.release.relation.unrecorded")}>{t("settings.language.release.unrecordedHelp")}</Banner> : null}{comparisons.data?.map(comparison => <section key={comparison.before_id} className="space-y-3" aria-label={t("settings.language.release.diff")}>
          <h3 className="text-sm font-semibold">{t("settings.language.release.sameVersionTargets", { count: selectedRows.filter(row => row.used_release_id === comparison.before_id).length })}</h3>
          <p className="break-words text-xs text-fg-muted">{selectedRows.filter(row => row.used_release_id === comparison.before_id).map(row => `${row.file_name} / ${t("documents.recipes.name", { slot: row.slot_no })}`).join("、")}</p>
          <LanguagePackComparison comparison={comparison} />
        </section>)}</> : null}
        <SelectField id="language-bulk-start-phase" label={t("settings.language.release.startPhase")} value={phase} required options={[{ value: "", label: t("settings.language.release.selectPhase") }, ...(["PREPROCESS","EXTRACT","CHUNK","INDEX"] as const).map(value => ({ value, label: t(phaseLabelKey(value)) }))]} onValueChange={value => setPhase(value as IngestionJobPhase)} disabled={execute.isPending} />
        <p className="text-xs text-fg-muted">{t("settings.language.release.processingHelp")}</p>
        <Button icon={RefreshCw} variant="secondary" loading={execute.isPending} disabled={!phase || selection.count === 0 || !reviewed} onClick={() => void submit()}>{t("flow.reprocess.confirm")} ({selection.count})</Button>
        {execute.isError ? <ApiErrorState error={execute.error} fallback={t("settings.language.saveError")} /> : null}
        {execute.data ? <><FormStatus tone={execute.data.rejected_count ? "warning" : "success"} message={t("settings.language.release.bulkResult", { accepted: execute.data.accepted_count, rejected: execute.data.rejected_count })} />{execute.data.items.filter(item => item.outcome === "rejected").map(item => <p key={`${item.document_id}:${item.recipe_id}`} className="text-sm text-fg-muted">{rows.find(row => row.document_id === item.document_id && row.recipe_id === item.recipe_id)?.file_name ?? item.document_id}: {item.reason}</p>)}</> : null}
      </CardContent>
    </Card> : null}
  </div>;
}
