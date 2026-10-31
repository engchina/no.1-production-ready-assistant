import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Check, Trash2 } from "lucide-react";
import {
  Banner,
  toast,
  Button,
  FormStatus,
  ProcessingIndicator,
  useConfirm,
  ExecutionConfirmationField,
} from "@production-ready/ui";
import { useResetExecutionConsent, useWorkspaceActive } from "@/components/WorkspaceState";
import { apiPost } from "@/lib/api";
import { useValuesChanged } from "@/lib/render-sync";
import { t } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { reviewExpired, runFinished, type SyntheticRun } from "./syntheticRuns";

export function SyntheticReview({ run, previews, stale, onUpdated }: {
  run: SyntheticRun; previews: Record<string, string>; stale: boolean;
  onUpdated: (run: SyntheticRun) => void;
}) {
  const [confirmation, setConfirmation] = useState("");
  const [pending, setPending] = useState(false);
  const [pendingAction, setPendingAction] = useState<"apply" | "discard">("apply");
  const [error, setError] = useState("");
  const confirm = useConfirm();
  const active = useWorkspaceActive();
  const scope = `${run.run_id}:${run.review_status}:${active}`;
  const scopeRef = useRef(scope);
  // 最新の scope を commit 時に入れる（render 中に ref を書かない）。
  useLayoutEffect(() => { scopeRef.current = scope; });
  // 対象・閲覧済みプレビューが変わったら、確認入力とエラーを render 中に消す。
  if (useValuesChanged([scope, previews])) {
    setConfirmation("");
    setError("");
  }
  useEffect(() => {
    scopeRef.current = scope;
    return () => { scopeRef.current = ""; };
  }, [scope, previews]);
  useResetExecutionConsent(() => setConfirmation(""), scope);
  const expected = run.targets.length === 1 ? run.targets[0].table_name : "ADMIN_EXECUTE";
  const allViewed = run.targets.every(target => Boolean(previews[target.table_name]));
  const expired = reviewExpired(run);
  const canApply = run.review_status === "ready" && run.status === "completed" && !expired;
  const canDiscard = runFinished(run) && !expired;
  const apply = async (discard: boolean) => {
    if (pending || stale || !active) return;
    const requestScope = scopeRef.current;
    if (!await confirm({
      title: t(discard ? "syntheticPreview.discardTitle" : "syntheticPreview.applyTitle"),
      description: t(discard ? "syntheticPreview.discardHint" : "syntheticPreview.applyHint", { tables: run.targets.map(target => target.table_name).join(", ") }),
      confirmLabel: t(discard ? "syntheticPreview.discard" : "syntheticPreview.apply"),
      tone: discard ? "danger" : "warning",
    }) || requestScope !== scopeRef.current) return;
    setPending(true); setPendingAction(discard ? "discard" : "apply"); setError("");
    try {
      const updated = await apiPost<SyntheticRun>(`/api/nl2sql/synthetic-data/runs/${run.run_id}/${discard ? "discard" : "apply"}`,
        discard ? {} : { confirmation, previews });
      if (requestScope !== scopeRef.current) return;
      onUpdated(updated);
      setConfirmation("");
      toast.success(t(discard ? "syntheticPreview.discarded" : "syntheticPreview.applied"));
    } catch (err) {
      setConfirmation("");
      setError(err instanceof Error ? err.message : t("syntheticPreview.error"));
    } finally { setPending(false); }
  };
  return <section aria-label={t("syntheticPreview.title")} className="grid min-w-0 gap-3 rounded-md border border-border bg-surface p-4" data-testid="synthetic-review">
    <h3 className="font-semibold">{t("syntheticPreview.title")}</h3>
    <Banner severity={run.review_status === "applied" ? "success" : "info"}>
      {t(run.review_status === "applied" ? "syntheticPreview.applied" : run.review_status === "discarded" ? "syntheticPreview.discarded" : "syntheticPreview.pending")}
      {run.applied_at && ` ${formatDateTime(run.applied_at)}`}
    </Banner>
    {!['applied', 'discarded'].includes(run.review_status ?? '') && <>
      <p className="text-sm text-fg-muted">{t(expired ? "syntheticPreview.expired" : "syntheticPreview.retention")}</p>
      {canApply && <>
        <p className="text-sm">{t("syntheticPreview.viewed", { count: run.targets.filter(target => previews[target.table_name]).length, total: run.targets.length })}</p>
        <ExecutionConfirmationField value={confirmation} onChange={setConfirmation}
          confirmed={confirmation.trim() === expected} placeholder={expected} expectedLabel={expected}
          helper={t("syntheticPreview.confirmHint", { phrase: expected })} disabled={pending || stale || !allViewed} />
      </>}
      {run.status === "partial" && <Banner severity="warning">{t("syntheticPreview.partial")}</Banner>}
      {/* 適用と破棄は同じ確認データへの二者択一。1 つの操作行の反対の端に置き、隣り合わせない（破棄は確認語が不要なので確認語欄の外）。 */}
      {(canApply || canDiscard) && (
        <div className="flex flex-col gap-2 border-t border-border pt-3 sm:flex-row sm:items-center" data-testid="synthetic-review-actions">
          {canApply && (
            <Button type="button" size="lg" icon={Check} className="w-full sm:w-auto" disabled={pending || stale || !allViewed || confirmation.trim() !== expected} loading={pending && pendingAction === "apply"} onClick={() => void apply(false)}>{t("syntheticPreview.apply")}</Button>
          )}
          {canDiscard && (
            <Button type="button" size="lg" variant="ghost" tone="danger" icon={Trash2} className="w-full sm:ml-auto sm:w-auto" disabled={pending || stale} loading={pending && pendingAction === "discard"} onClick={() => void apply(true)}>{t("syntheticPreview.discard")}</Button>
          )}
        </div>
      )}
      {/* 適用は生成したデータを本来の表へ INSERT するため、件数が多いと数十秒かかる（messaging.md §3.7）。 */}
      {pending && pendingAction === "apply" && <ProcessingIndicator active label={t("syntheticPreview.progress.applying")} operationKey={`synthetic-apply-${run.run_id}`} placement="action" activityIcon="none" testId="synthetic-apply-processing" />}
    </>}
    {error && <FormStatus tone="danger" message={error} />}
  </section>;
}
