"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Banner, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, DataTable, DEFAULT_PAGE_SIZE, INFORMATION_TABLE_ROW_CLASS, INFORMATION_TABLE_VISIBLE_ROWS, OffsetPagination, ListToolbar, StatusBadge, FormStatus, TableSkeleton, TextareaField, TimedLoadingState, useConfirm } from "@production-ready/ui";
import { History, Send } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ApiErrorState } from "@/components/StateViews";
import { LanguagePackFieldsPanel, useLanguagePackPanelState } from "@/components/settings/LanguagePackFieldsPanel";
import { api, type LanguagePackData, type LanguagePackRelease, type LanguagePackReleaseState, type LanguagePackReleaseComparison, type IngestionJobPhase } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { textsFromSections } from "@/lib/language-pack";
import { listCountLabels, paginationLabels } from "@/lib/pagination-labels";
import { phaseLabelKey } from "@/components/documents/DocumentWorkspace.logic";
import { useWorkspaceState } from "@/lib/workspace-state";

function valueText(value: unknown): string {
  return value == null ? "—" : Array.isArray(value) ? value.join("\n") : String(value);
}

export function LanguagePackComparison({ comparison }: { comparison: LanguagePackReleaseComparison }) {
  return <div className="space-y-3">
    <Banner severity={comparison.ingest_changed ? "warning" : "info"} title={t(comparison.ingest_changed ? "settings.language.release.ingestChanged" : "settings.language.release.ingestSame")}>
      {comparison.impact_requires_review ? t("settings.language.release.impactUnknown") : comparison.affected_stages.map((stage) => t(phaseLabelKey(stage as IngestionJobPhase))).join(" / ") || t("settings.language.release.noIngestImpact")}
    </Banner>
    <DataTable ariaLabel={t("settings.language.release.diff")} rows={comparison.differences} getRowKey={(row) => `${row.section}.${row.key}`} stickyHeader visibleRows={INFORMATION_TABLE_VISIBLE_ROWS} columns={[
      { key: "field", header: t("settings.language.release.field"), render: (row) => t(`settings.language.field.${row.key}` as I18nKey) },
      { key: "before", header: t("settings.language.release.before"), render: (row) => <span className="whitespace-pre-wrap break-words">{valueText(row.before)}</span> },
      { key: "after", header: t("settings.language.release.after"), render: (row) => <span className="whitespace-pre-wrap break-words">{valueText(row.after)}</span> },
    ]} />
  </div>;
}

/** 完全な公開版を閲覧し、草稿の公開と過去版の再公開を CAS で確定する。 */
export function LanguagePackHistoryCard({ dirty, metadata, state }: { dirty: boolean; metadata: LanguagePackData; state: LanguagePackReleaseState }) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const panel = useLanguagePackPanelState();
  const [paging, setPaging] = useWorkspaceState("language-releases.paging", { offset: 0, size: Number(DEFAULT_PAGE_SIZE) }, (value): value is { offset: number; size: number } => {
    if (typeof value !== "object" || value === null) return false;
    const candidate = value as Record<string, unknown>;
    return typeof candidate.offset === "number" && Number.isInteger(candidate.offset) && candidate.offset >= 0 && typeof candidate.size === "number" && [10, 25, 50, 100].includes(candidate.size);
  });
  const { offset, size } = paging;
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("release");
  const selectedQuery = useQuery({ queryKey: ["language-pack-release", selectedId], queryFn: () => api.getLanguagePackRelease(selectedId!), enabled: Boolean(selectedId), retry: false });
  const selected = selectedQuery.data ?? null;
  const [note, setNote] = useState("");
  const [reviewedRevision, setReviewedRevision] = useState<string | null>(null);
  const revisionKey = `${state.draft_revision}:${state.current.id}`;
  const reviewed = reviewedRevision === revisionKey;
  const history = useQuery({ queryKey: ["language-pack-releases", offset, size], queryFn: () => api.getLanguagePackReleases(offset, size), retry: false });
  const comparison = useQuery({ queryKey: ["language-pack-comparison", state.current.id, selected?.id], queryFn: () => api.compareLanguagePackReleases(state.current.id, selected!.id), enabled: Boolean(selected && selected.id !== state.current.id), retry: false });
  const mutation = useMutation({
    mutationFn: (release: LanguagePackRelease | null) => {
      const body = { expected_revision: state.draft_revision, expected_current_id: state.current.id, change_note: note };
      return release ? api.restoreLanguagePackRelease(release.id, body) : api.publishLanguagePack(body);
    },
    onSuccess: (data) => {
      qc.setQueryData(["language-pack-draft"], data);
      void qc.invalidateQueries({ queryKey: ["language-pack-releases"] });
      void qc.invalidateQueries({ queryKey: ["settings", "language-pack"] });
      setParams((current) => { const next = new URLSearchParams(current); next.delete("release"); return next; }, { replace: true }); setReviewedRevision(null); setNote("");
    },
  });
  const draftComparison = useQuery({ queryKey: ["language-pack-draft-comparison", state.draft_revision, state.current.id], queryFn: () => api.compareLanguagePackDraft(state.draft_revision, state.current.id), enabled: reviewed, retry: false });
  async function publish(release: LanguagePackRelease | null) {
    const ok = await confirm({ title: t(release ? "settings.language.release.restoreTitle" : "settings.language.release.publishTitle"), description: t("settings.language.release.publishConfirm", { version: release?.version ?? state.current.version }), confirmLabel: t(release ? "settings.language.release.restore" : "settings.language.release.publish"), tone: "warning" });
    if (ok) mutation.mutate(release);
  }
  return <>
    <Card>
      <CardHeader><CardTitle>{t("settings.language.release.publishTitle")}</CardTitle><CardDescription>{t("settings.language.release.publishHelp")}</CardDescription></CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-fg-muted">{t("settings.language.release.current", { version: state.current.version })} · {formatDateTime(state.current.published_at)}</p>
        <TextareaField id="language-release-note" label={t("settings.language.release.note")} width="full" rows={2} maxLength={2000} value={note} onValueChange={setNote} disabled={mutation.isPending} />
        <div><Button variant="secondary" disabled={dirty || mutation.isPending} onClick={() => setReviewedRevision(revisionKey)}>{t("settings.language.release.review")}</Button></div>
        {reviewed ? draftComparison.isPending ? <TimedLoadingState label={t("settings.language.history.loading")} operationKey="language-draft-diff"><TableSkeleton columns={3} /></TimedLoadingState> : draftComparison.isError ? <ApiErrorState error={draftComparison.error} fallback={t("settings.language.history.loadError")} onRetry={() => { void qc.invalidateQueries({ queryKey: ["language-pack-draft"] }); setReviewedRevision(null); }} /> : <LanguagePackComparison comparison={draftComparison.data} /> : null}
        <div><Button icon={Send} disabled={dirty || !reviewed || !draftComparison.isSuccess || draftComparison.isFetching || !(draftComparison.data.differences.length || draftComparison.data.ingest_changed)} loading={mutation.isPending && mutation.variables === null} onClick={() => void publish(null)}>{t("settings.language.release.publish")}</Button></div>
        {mutation.isSuccess ? <FormStatus tone="success" message={t("settings.language.release.published", { version: mutation.data.current.version })} /> : null}
        {dirty ? <p className="text-sm text-fg-muted">{t("settings.language.release.saveFirst")}</p> : null}
        {mutation.isError ? <ApiErrorState error={mutation.error} fallback={t("settings.language.saveError")} onRetry={() => { void qc.invalidateQueries({ queryKey: ["language-pack-draft"] }); mutation.reset(); setReviewedRevision(null); }} /> : null}
      </CardContent>
    </Card>
    <Card>
      <CardHeader><CardTitle className="flex items-center gap-2"><History size={16} aria-hidden />{t("settings.language.history.title")}</CardTitle><CardDescription>{t("settings.language.history.description")}</CardDescription></CardHeader>
      <CardContent className="space-y-4">
        {history.isPending ? <TimedLoadingState label={t("settings.language.history.loading")} operationKey="language-releases"><TableSkeleton columns={4} /></TimedLoadingState> : history.isError ? <ApiErrorState error={history.error} fallback={t("settings.language.history.loadError")} onRetry={() => void history.refetch()} /> : <>
          <ListToolbar count={{ total: history.data.total, labels: listCountLabels() }} />
          <DataTable ariaLabel={t("settings.language.history.title")} rows={history.data.releases} getRowKey={(row) => row.id} rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })} stickyHeader visibleRows={INFORMATION_TABLE_VISIBLE_ROWS} columns={[
            { key: "version", header: t("settings.language.release.version"), rowHeader: true, render: (row) => <Button variant="ghost" onClick={() => setParams((current) => { const next = new URLSearchParams(current); next.set("release", row.id); return next; })}>{`v${row.version}`}</Button> },
            { key: "published_at", header: t("settings.language.history.column.savedAt"), render: (row) => formatDateTime(row.published_at) },
            { key: "published_by", header: t("settings.language.history.column.savedBy"), render: (row) => row.published_by ?? "—" },
            { key: "change_note", header: t("settings.language.release.note"), render: (row) => <span className="whitespace-pre-wrap break-words">{row.change_note || "—"} {row.id === state.current.id ? <StatusBadge variant="success" label={t("settings.language.history.current")} /> : null}</span> },
          ]} />
          <OffsetPagination offset={offset} limit={size} total={history.data.total} count={history.data.releases.length} onPageChange={(page) => setPaging((current) => ({ ...current, offset: (page - 1) * current.size }))} pageSizeOptions={[10, 25, 50, 100]} onPageSizeChange={(value) => { setPaging({ size: value, offset: 0 }); }} labels={paginationLabels()} />
        </>}
        {selectedId && selectedQuery.isPending ? <TimedLoadingState label={t("settings.language.history.loading")} operationKey="language-release-content"><TableSkeleton columns={2} /></TimedLoadingState> : null}
        {selectedQuery.isError ? <ApiErrorState error={selectedQuery.error} fallback={t("settings.language.history.loadError")} onRetry={() => void selectedQuery.refetch()} /> : null}
        {selected ? <section aria-label={t("settings.language.release.details")} className="space-y-4">
          <h3 className="text-sm font-semibold">{t("settings.language.release.details")} v{selected.version}</h3>
          <LanguagePackFieldsPanel idPrefix="language-release-readonly" fields={metadata.fields} baseline={selected.effective} texts={textsFromSections(metadata.fields, selected.effective)} errors={{}} disabled state={panel} changedLabel={t("settings.language.changed")} resetLabel={t("settings.language.resetField")} onChange={() => undefined} />
          <TextareaField id="language-release-full-snapshot" label={t("settings.language.release.snapshot")} width="full" rows={6} readOnly value={JSON.stringify(selected.effective, null, 2)} />
          {comparison.isPending && selected.id !== state.current.id ? <TimedLoadingState label={t("settings.language.history.loading")} operationKey="language-release-diff"><TableSkeleton columns={3} /></TimedLoadingState> : null}
          {comparison.isError ? <ApiErrorState error={comparison.error} fallback={t("settings.language.history.loadError")} onRetry={() => void comparison.refetch()} /> : comparison.data ? <LanguagePackComparison comparison={comparison.data} /> : null}
          {selected.id !== state.current.id ? <Button variant="secondary" disabled={dirty || mutation.isPending || !comparison.data || comparison.isError} onClick={() => void publish(selected)}>{t("settings.language.release.restore")}</Button> : null}
        </section> : null}
      </CardContent>
    </Card>
  </>;
}
