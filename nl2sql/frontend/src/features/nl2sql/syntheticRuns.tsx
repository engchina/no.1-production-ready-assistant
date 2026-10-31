import { RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Banner,
  toast,
  Button,
  StatusBadge,
  FormStatus,
  ProcessingIndicator,
  INFORMATION_LIST_SCROLL_CLASS,
  INFORMATION_TABLE_FOCUS_CLASS,
  SelectField,
  DISPLAY_TIME_SECONDS,
  formatDisplayDateTime,
} from "@production-ready/ui";
import { apiGet } from "@/lib/api";
import { useValuesChanged } from "@/lib/render-sync";
import { useDatabaseStatus } from "@/lib/queries";
import { t } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { useAuth } from "@/features/security/AuthProvider";
import { syntheticRunPollingInterval } from "./syntheticRunPolling";
import { DbObjectName } from "./components/DbObjectName";

export interface SyntheticRun {
  preview?: boolean;
  review_status?: "pending" | "ready" | "applied" | "discarded";
  /** 確認用データの保持期間（生成終了から 24 時間）を過ぎたか（backend の判定）。過ぎた記録は閲覧だけ。 */
  review_expired?: boolean;
  applied_at?: string | null;
  run_id: string;
  status: "pending" | "running" | "verifying" | "completed" | "partial" | "failed" | "no_data" | "unknown";
  targets: { table_name: string; requested_rows: number; loaded_rows: number | null; status: string; error: string }[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  checked_at: string | null;
  message: string;
  operation_ids: number[];
  failure_phase?: "validation" | null;
}
export const runFinished = (run: SyntheticRun) => ["completed", "partial", "failed", "no_data"].includes(run.status);
// 生成の記録は期間で消さない（#3235）。期間を過ぎるのは確認用データ（適用・破棄・表示ができる期間）だけ。
export const reviewExpired = (run: SyntheticRun) => runFinished(run) && run.review_expired === true;
export const runLabel = (run: SyntheticRun) => run.preview && run.status === "completed" && run.review_status !== "applied"
  ? t(run.review_status === "discarded" ? "syntheticPreview.discarded" : reviewExpired(run) ? "syntheticPreview.expiredStatus" : "syntheticPreview.ready") : t(`syntheticRun.status.${run.status}`);

export function useSyntheticRuns({ poll = false, enabled = true } = {}) {
  const { user, hasPermission } = useAuth();
  const db = useDatabaseStatus();
  const key = ["synthetic-runs", user?.user_uuid, db.data?.context_id];
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => apiGet<SyntheticRun[]>("/api/nl2sql/synthetic-data/runs", { signal, timeoutMs: 10_000 }),
    enabled: enabled && Boolean(user) && (hasPermission("menu.data_management") || hasPermission("menu.sample_data")),
    // 一覧の定期取得は全体通知だけが担当し、keep-alive 画面にタイマーを重ねない。
    refetchInterval: poll ? syntheticRunPollingInterval : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: 1_000,
    retry: false,
  });
  return { ...query, key };
}

export function SyntheticRunNotifications() {
  const query = useSyntheticRuns({ poll: true });
  const scope = JSON.stringify(query.key);
  // 観測済みの状態は利用者・DB の範囲ごとに持つ。範囲が変わったら effect の中で捨てる（render 中に ref を読み書きしない）。
  const observed = useRef({ scope, runs: new Map<string, string>() });
  useEffect(() => {
    if (observed.current.scope !== scope) observed.current = { scope, runs: new Map() };
    const runs = observed.current.runs;
    for (const run of query.data ?? []) {
      const previous = runs.get(run.run_id);
      if (previous && previous !== run.status && runFinished(run)) {
        const label = runLabel(run);
        const options = { description: run.targets.map((target) => target.table_name).join(", ") };
        if (run.status === "completed") toast.success(label, options);
        else toast.warning(label, options);
      }
      runs.set(run.run_id, run.status);
    }
  }, [query.data, scope]);
  return null;
}

export function SyntheticRunPanel({ run, runs, onSelect, error, onRefresh, submitting = false }: {
  run: SyntheticRun | null; runs: SyntheticRun[]; onSelect: (id: string) => void;
  error: boolean; submitting?: boolean; onRefresh: () => Promise<SyntheticRun | null>;
}) {
  const [refresh, setRefresh] = useState<{ scope: string; pending: boolean; failed: boolean; message: string } | null>(null);
  const scope = `${run?.run_id ?? ""}:${submitting}`;
  const refreshSequence = useRef(0);
  // 対象の実行が変わったら、前の確認結果を render 中に捨てる。
  if (useValuesChanged([scope]) && refresh !== null) setRefresh(null);
  // 実行中の確認の応答は、対象が変わった後は反映しない。
  useEffect(() => {
    refreshSequence.current += 1;
    return () => { refreshSequence.current += 1; };
  }, [scope]);
  const feedback = refresh?.scope === scope ? refresh : null;
  const refreshStatus = async () => {
    if (feedback?.pending) return;
    const sequence = ++refreshSequence.current;
    setRefresh({ scope, pending: true, failed: false, message: "" });
    try {
      const latest = await onRefresh();
      if (sequence !== refreshSequence.current) return;
      // worker の確認日時を除き、利用者に見える生成結果の変化を比較する。
      const snapshot = (value: SyntheticRun | null) => value && JSON.stringify({
        status: value.status, targets: value.targets, message: value.message,
        finished_at: value.finished_at, operation_ids: value.operation_ids, failure_phase: value.failure_phase,
      });
      const unchanged = snapshot(run) === snapshot(latest);
      const time = formatDisplayDateTime(new Date(), DISPLAY_TIME_SECONDS);
      setRefresh({ scope, pending: false, failed: false, message: t(unchanged ? "syntheticRun.refreshUnchanged" : "syntheticRun.refreshUpdated", { time }) });
    } catch {
      if (sequence !== refreshSequence.current) return;
      setRefresh({ scope, pending: false, failed: true, message: t("syntheticRun.refreshFailed") });
    }
  };
  return <section aria-label={t("syntheticRun.title")} className="grid min-w-0 gap-3 rounded-md border border-border bg-surface-sunken p-4" data-testid="synthetic-run-panel">
    <h3 className="font-semibold">{t("syntheticRun.title")}</h3>
    {submitting && <ProcessingIndicator active label={t("syntheticRun.submitting")} activityIcon="none" placement="action" testId="synthetic-submitting" />}
    {error && !feedback?.failed && <Banner severity="warning">{t("syntheticRun.stale")}{run?.checked_at ? ` ${formatDateTime(run.checked_at)}` : ""}</Banner>}
    {submitting ? null : !run ? !error && <p className="text-sm text-fg-muted">{t("syntheticRun.notStarted")}</p> : <>
      <div role="status" data-testid="synthetic-run-status">
        <StatusBadge className="max-w-full whitespace-normal text-left" variant={run.status === "completed" ? "success" : run.status === "failed" ? "danger" : error || ["unknown", "partial", "no_data"].includes(run.status) ? "warning" : "info"} label={runLabel(run)} />
      </div>
      {(run.status !== "unknown" && (!runFinished(run) || run.finished_at)) && <ProcessingIndicator
        active={!runFinished(run)}
        operationKey={run.run_id}
        startedAt={run.created_at}
        finishedAt={run.finished_at}
        label={t(error ? "syntheticRun.elapsedUnverified" : runFinished(run) ? "syntheticRun.durationHint" : `syntheticRun.progress.${run.status}`)}
        finalLabel={t("syntheticRun.durationHint")}
        activityIcon={error ? "none" : "spinner"}
        announceActivity={false}
        announceSlow={false}
        showSlowMessage={!error}
        placement="job"
        testId="synthetic-run-processing"
      />}
      <p className="break-all text-sm" data-testid="synthetic-run-reference">{t("syntheticRun.reference", { id: run.run_id })}</p>
      <p className="text-sm text-fg-muted" data-testid="synthetic-run-checked">
        {t("syntheticRun.checkedAt", { time: formatDateTime(run.checked_at) })}
      </p>
      {run.status === "unknown" && <p className="text-sm text-fg-muted">{t("syntheticRun.unknownHint")}</p>}
      {!runFinished(run) && !error && run.status !== "unknown" && <p className="text-sm text-fg-muted">{t("syntheticRun.continues")}</p>}
      <p className="text-xs text-fg-muted">{t("syntheticRun.targetCount", { count: run.targets.length })}</p>
      <div
        key={run.run_id}
        role="region"
        aria-label={t("syntheticRun.targets")}
        tabIndex={0}
        className={`min-w-0 pr-1 ${INFORMATION_LIST_SCROLL_CLASS} ${INFORMATION_TABLE_FOCUS_CLASS}`}
        data-testid="synthetic-run-targets"
      >
        <ul className="grid min-w-0 content-start gap-2">
          {run.targets.map((target) => <li key={target.table_name} className="grid min-w-0 gap-1 rounded border border-border bg-surface p-3 text-sm [overflow-wrap:anywhere]">
            <DbObjectName value={target.table_name} size="sm" />
            <span>{t(run.preview ? "syntheticPreview.count" : "syntheticRun.count", { requested: target.requested_rows, loaded: target.loaded_rows ?? t("syntheticRun.unverified") })}</span>
            <span>{t("syntheticRun.targetStatus", { status: targetStatusLabel(run.status === "unknown" && target.status === "pending" ? "unknown" : target.status) })}</span>
            {target.error && <p className="text-danger-fg">{target.error}</p>}
          </li>)}
        </ul>
      </div>
      {run.failure_phase === "validation" && <Banner severity="danger">{t("syntheticRun.validationFailed")}</Banner>}
      {run.message && !run.failure_phase && <Banner severity={run.status === "failed" ? "danger" : "warning"}>{run.message}</Banner>}
      {run.finished_at && <p className="text-sm text-fg-muted">{t("syntheticRun.finishedAt", { time: formatDateTime(run.finished_at) })}</p>}
    </>}
    {runs.some((item) => !runFinished(item)) && <p className="text-sm text-fg-muted">{t("syntheticRun.independentRuns")}</p>}
    {runs.length > 0 && <SelectField
      id="synthetic-run-history"
      label={t("syntheticRun.history")}
      disabled={submitting}
      value={run?.run_id ?? ""}
      placeholder={t("syntheticRun.selectHistory")}
      options={runs.map((r) => ({ value: r.run_id, label: `${formatDateTime(r.created_at)} · ${runLabel(r)} · ${r.targets.map((target) => target.table_name).join(", ")} · ${r.run_id.slice(0, 8)}` }))}
      onValueChange={onSelect}
      // 生成履歴（日時・種類・対象の表・ID）は長い値の選択（lg。#1891）。
      width="lg"
      className="min-w-0"
    />}
    <p className="text-xs text-fg-muted">{t(!run || run.preview ? "syntheticPreview.retention" : "syntheticRun.retention")}</p>
    <div className="flex flex-wrap gap-2">
      <Button type="button" variant="secondary" size="sm" loading={feedback?.pending} icon={RefreshCw} onClick={() => void refreshStatus()}>{t("syntheticRun.refresh")}</Button>
    </div>
    {feedback?.message && (feedback.failed || !error) && <FormStatus tone={feedback.failed ? "danger" : "success"} message={feedback.message} />}
  </section>;
}
function targetStatusLabel(status: string) {
  const known: Record<string, Parameters<typeof t>[0]> = { pending: "syntheticRun.status.pending", running: "syntheticRun.status.running", completed: "syntheticRun.status.completed", failed: "syntheticRun.status.failed", skipped: "syntheticRun.skipped" };
  return t(known[status] ?? "syntheticRun.status.unknown");
}
