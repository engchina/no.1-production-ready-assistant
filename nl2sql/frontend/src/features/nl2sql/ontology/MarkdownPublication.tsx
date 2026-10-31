import { RefreshCw, ShieldCheck, Upload } from "lucide-react";
import {
  Banner,
  Button,
  Disclosure,
  StatusBadge,
  TextareaField,
  useConfirm,
  ContentActionBar,
  ProcessingIndicator,
} from "@production-ready/ui";
import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { t } from "@/lib/i18n";
import { randomUuid } from "@/lib/randomUuid";
import { useWorkspaceState, useResetExecutionConsent } from "@/components/WorkspaceState";
import type { OntologyFinding, OntologyMarkdownState, OntologyPublishJob } from "./types";
import { OntologyFindings } from "./OntologyFindings";
import { PublicationGraphSummary } from "./PublicationGraphSummary";
import { PublicationChanges } from "./PublicationChanges";
import { ImprovementSuggestionPanel } from "./ImprovementSuggestionPanel";
import { conceptDisplayNames, type ImprovementSuggestion, type PublicationDifference } from "./publicationSummary";
import { DefinitionFields, TechnicalDetails } from "./ontologyResultPresentation";

const MARKDOWN_BUSY = "markdown-check";

type Definition = Record<string, unknown>;
interface Preparation {
  id: string; status: string; draft_etag: string; expected_head: string; display_version: number;
  error_message_ja: string;
  findings: OntologyFinding[];
  definitions?: Definition[];
  differences?: PublicationDifference[];
  suggestions?: ImprovementSuggestion[];
  data_report?: Record<string, unknown>;
}

export function MarkdownPublication({ profileId, profileLabel, signature, disabled, save, onPublished, onBusyChange, businessText = "", onAppendBusinessText }: {
  profileId: string; profileLabel: string; signature: string; disabled: boolean;
  /** 「業務説明（自然言語）」の今の値（改善案が重ねて言わないため）と、利用者が押したときの追記。 */
  businessText?: string;
  onAppendBusinessText?: (text: string) => void;
  save: () => Promise<OntologyMarkdownState | null | undefined>;
  onPublished: (job: OntologyPublishJob) => void;
  onBusyChange: Dispatch<SetStateAction<string>>;
}) {
  const endpoint = `/api/nl2sql/profiles/${encodeURIComponent(profileId)}/ontology-markdown`;
  const [preparationId, setPreparationId] = useWorkspaceState(`markdown:${profileId}:preparation`, "");
  const [execution, setExecution] = useWorkspaceState(`markdown:${profileId}:publication-recovery`, { key: "" });
  const [acceptance, setAcceptance] = useWorkspaceState(`markdown:${profileId}:acceptance`, "");
  const [error, setError] = useState("");
  const [acceptanceError, setAcceptanceError] = useState("");
  const [busy, setBusy] = useState("");
  const generation = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; generation.current += 1; };
  }, []);
  useResetExecutionConsent(() => { generation.current += 1; }, `${profileId}:${signature}`);
  const lastSignature = useRef(signature);
  useEffect(() => {
    if (signature !== lastSignature.current) {
      lastSignature.current = signature;
      setPreparationId("");
    }
  }, [signature, setPreparationId]);
  const confirm = useConfirm();
  const preparation = useQuery({
    queryKey: ["markdown-preparation", profileId, preparationId],
    queryFn: ({signal}) => apiGet<Preparation>(`${endpoint}/preparations/${encodeURIComponent(preparationId)}`, {signal}),
    enabled: Boolean(preparationId),
    refetchInterval: query => ["queued", "running"].includes(query.state.data?.status ?? "") ? 1000 : false,
  });
  const value = preparation.data;
  const findings = [...(value?.findings ?? []), ...(value?.error_message_ja ? [{ severity: "error", message_ja: value.error_message_ja }] : [])];
  const hasErrors = findings.some(finding => finding.severity === "error");
  const hasWarnings = findings.some(finding => finding.severity === "warning");
  const dataValidationFailed = Number(value?.data_report?.errors ?? 0) > 0;
  const running = Boolean(value && ["queued", "running"].includes(value.status));
  const ownBusy = Boolean(busy) || running;
  useEffect(() => {
    if (!ownBusy) return;
    onBusyChange(MARKDOWN_BUSY);
    // 親の busy は他の操作(構築開始・中止など)と共有するため、自分が立てた値だけを戻す。
    // 再マウント時に無条件で "" にすると、実行中の操作のボタンが再び押せてしまう。
    return () => onBusyChange(current => current === MARKDOWN_BUSY ? "" : current);
  }, [ownBusy, onBusyChange]);
  const headers = (key: string) => ({ headers: { "Idempotency-Key": key } });
  async function prepare() {
    setBusy("prepare"); setError("");
    try {
      const state = await save();
      if (!state?.draft_etag) return;
      const result = await apiPost<Preparation>(`${endpoint}/prepare`, { draft_etag: state.draft_etag }, headers(randomUuid()));
      if (!mounted.current) return;
      setPreparationId(result.id);
    } catch (e) { setError(e instanceof Error ? e.message : t("markdownOntology.failed")); }
    finally { setBusy(""); }
  }
  async function publish() {
    if (!value || value.status !== "ready" || preparation.isError || hasErrors || dataValidationFailed) return;
    const consent = generation.current;
    if (!(await confirm({title:t("profiles.ontologyBuild.publish"), description:t("markdownOntology.confirm", {profile:profileLabel,version:String(value.display_version)}),confirmLabel:t("profiles.ontologyBuild.publish"),tone:"info"}))) return;
    if (consent !== generation.current) return;
    setBusy("publish"); setError("");
    const body = { preparation_id: value.id, draft_etag: value.draft_etag, expected_head: value.expected_head, confirmed: true };
    try {
      const key = randomUuid();
      setExecution({key});
      const result = await apiPost<{job:OntologyPublishJob}>(`${endpoint}/publish`, body, headers(key));
      if (!mounted.current) return;
      setExecution({key:""}); setPreparationId(""); onPublished(result.job);
    } catch (e) {
      if (e instanceof ApiError && e.status >= 400 && e.status < 500) setExecution({key:""});
      setError(e instanceof Error ? e.message : t("markdownOntology.failed"));
    }
    finally { setBusy(""); }
  }
  async function recover() {
    setBusy("recover"); setError("");
    try {
      const result = await apiGet<{job:OntologyPublishJob | null}>(`${endpoint}/publication-outcome?key=${encodeURIComponent(execution.key)}`);
      if (!mounted.current) return;
      // publish は同期・atomic のため、結果がなければ公開されていない。key を捨てて再公開できるようにする。
      setExecution({key:""});
      if (result.job) onPublished(result.job);
      else setError(t("markdownOntology.notPublished"));
    } catch(e) { setError(e instanceof Error ? e.message : t("markdownOntology.failed")); }
    finally { setBusy(""); }
  }
  async function dataValidation() {
    let cases: unknown = [];
    try { cases = acceptance.trim() ? JSON.parse(acceptance) : []; if (!Array.isArray(cases)) throw new Error(); }
    catch {setAcceptanceError(t("ontologyUi.jsonError")); return;}
    setAcceptanceError("");
    setBusy("data"); setError("");
    try { await apiPost(`${endpoint}/preparations/${value?.id}/validate-data`, { confirmed: true, acceptance_cases: cases }); await preparation.refetch(); }
    catch(e) {setError(e instanceof Error ? e.message : t("markdownOntology.failed"));}
    finally {setBusy("");}
  }
  return <div className="grid min-w-0 gap-3 border-t border-border pt-4" data-testid="ontology-publish-actions">
    <ContentActionBar ariaLabel={t("markdownOntology.actions")}>
      <Button icon={Upload} type="button" variant="primary" size="lg" disabled={disabled || Boolean(busy) || running || Boolean(execution.key)} loading={busy === "prepare" || running} onClick={() => void prepare()}>{t("profiles.ontologyBuild.publish")}</Button>
    </ContentActionBar>
    {/* 公開の準備（下書きの保存・差分と検査）は数十秒かかる。スピナーはボタンの loading が担う（messaging.md §3.7）。 */}
    {(busy === "prepare" || running) && <ProcessingIndicator active label={t("markdownOntology.progress.preparing")} operationKey="markdown-prepare" placement="action" activityIcon="none" testId="markdown-prepare-processing" />}
    {(error || preparation.isError) && <Banner severity="danger"><div tabIndex={0} className="max-h-72 overflow-y-auto break-words">{error || t("markdownOntology.refreshFailed")}</div></Banner>}
    {execution.key && <Button icon={RefreshCw} type="button" size="sm" variant="secondary" disabled={Boolean(busy)} onClick={() => void recover()}>{t("markdownOntology.checkOutcome")}</Button>}
    {value && <section className="grid min-w-0 gap-3" aria-label={t("markdownOntology.check")}>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
      <div className="grid min-w-0 gap-1">
        <h3 className="text-sm font-semibold">{t("markdownOntology.check")}</h3>
        <p className="text-xs leading-relaxed text-fg-muted">{t("markdownOntology.checkHint")}</p>
      </div>
      <StatusBadge variant={hasErrors || dataValidationFailed || value.status === "failed" ? "danger" : value.status === "ready" ? hasWarnings ? "warning" : "success" : "info"} label={t(`markdownOntology.status.${hasErrors || dataValidationFailed ? "failed" : value.status}`)} />
      </div>
      {hasErrors && <Banner severity="danger">{t("markdownOntology.errorsBlockPublish")}</Banner>}
      {value.status === "ready" && hasWarnings && !hasErrors && !dataValidationFailed && <Banner severity="warning">{t("markdownOntology.warningsPublishable")}</Banner>}
      {/* 残ったエラー・警告を解くための、業務説明に追記する改善案（#1729）。エラー・警告が無ければ出さない。 */}
      {(hasErrors || hasWarnings) && <ImprovementSuggestionPanel suggestions={value.suggestions ?? []} businessText={businessText} onAppend={onAppendBusinessText} />}
      <OntologyFindings findings={findings} label={t("markdownOntology.findings")} names={conceptDisplayNames(value.definitions ?? [])} />
      {value.status !== "queued" && value.status !== "running" && <>
        <PublicationGraphSummary definitions={value.definitions ?? []} />
        <PublicationChanges differences={value.differences ?? []} findings={findings} definitions={value.definitions ?? []} />
      </>}
      {value.status === "ready" && <>
        <Disclosure data-testid="ontology-publication-data-validation" summary={t("markdownOntology.dataValidation")} icon={ShieldCheck}
          summaryClassName="px-4 py-3 font-medium" contentClassName="grid gap-4 p-4">
            <TextareaField id="markdown-acceptance" label={t("markdownOntology.acceptance")} width="full" rows={5} monospace value={acceptance} helper={t("markdownOntology.acceptanceHint")} error={acceptanceError || undefined} onChange={e=>{setAcceptance(e.target.value);setAcceptanceError("");}} />
            <ContentActionBar ariaLabel={t("markdownOntology.dataValidation")}>
              <Button icon={ShieldCheck} type="button" variant="secondary" size="md" disabled={Boolean(busy)} loading={busy === "data"} onClick={()=>void dataValidation()}>{t("markdownOntology.dataValidation")}</Button>
            </ContentActionBar>
            {busy === "data" && <ProcessingIndicator active label={t("markdownOntology.progress.validatingData")} operationKey="markdown-data-validation" placement="action" activityIcon="none" testId="markdown-data-validation-processing" />}
            {value.data_report && <div className="grid min-w-0 gap-3 border-t border-border pt-4">
              <DefinitionFields definition={Object.fromEntries(["checked_at", "sample_limit", "instance_count", "errors", "acceptance_cases"].filter(key => key in value.data_report!).map(key => [key, value.data_report![key]]))} />
              <p className="text-xs leading-relaxed text-fg-muted">{t("markdownOntology.sampledOnly")}</p>
              <TechnicalDetails value={value.data_report} />
            </div>}
          </Disclosure>
        {dataValidationFailed && <Banner severity="danger">{t("markdownOntology.dataValidationFailed")}</Banner>}
        <ContentActionBar ariaLabel={t("markdownOntology.actions")}><Button icon={Upload} type="button" variant="primary" size="lg" disabled={Boolean(busy) || Boolean(execution.key) || preparation.isError || hasErrors || dataValidationFailed} loading={busy === "publish"} onClick={()=>void publish()}>{t("markdownOntology.confirmPublish")}</Button></ContentActionBar>
        {busy === "publish" && <ProcessingIndicator active label={t("markdownOntology.progress.publishing")} operationKey="markdown-publish" placement="action" activityIcon="none" testId="markdown-publish-processing" />}
      </>}
    </section>}
  </div>;
}
