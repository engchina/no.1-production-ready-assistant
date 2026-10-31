import type { ReactNode } from "react";
import { t } from "@/lib/i18n";
import type { OntologyBuildJob } from "./types";
import type { WorkflowProgressStepStatus } from "../components/WorkflowProgressStrip";

import { conceptKinds } from "./conceptModel";
import { Disclosure } from "@production-ready/ui";
export { conceptKinds, conceptGraph, nodeConceptKind } from "./conceptModel";
export type { ConceptKind } from "./conceptModel";

export function conceptLabel(kind: string) { return t(`ontologyResults.kind.${kind}`); }

interface Progress {
  id: string;
  label: string;
  status: WorkflowProgressStepStatus;
  statusLabel: string;
  description?: string;
  content?: ReactNode;
  testId?: string;
  dataStatus?: string;
  elapsedLabel?: string;
  open?: boolean;
}

type Stage = "prepare" | "concepts" | "validation" | "markdown" | "save";

export function buildEventStage(phase: string | null | undefined): Stage | undefined {
  if (phase === "freeze" || phase === "evidence") return "prepare";
  if (phase === "objects" || phase === "shared" || phase === "capabilities") return "concepts";
  if (["concepts", "validation", "markdown", "save"].includes(phase ?? "")) return phase as Stage;
  return undefined;
}

function aggregateStatus(statuses: WorkflowProgressStepStatus[]): WorkflowProgressStepStatus {
  if (statuses.includes("error")) return "error";
  if (statuses.includes("running")) return "running";
  if (statuses.length && statuses.every(s => s === "done" || s === "skipped")) return "done";
  return "pending";
}

/** 保存済みの旧工程も非破壊で5段階に投影し、表示とヘッダー件数で共有する。 */
export function orderedBuildProgress(
  job: OntologyBuildJob,
  steps: Progress[],
  stageEvents: Partial<Record<Stage, ReactNode>> = {},
): Progress[] {
  const take = (names: string[]) => steps.filter(s => names.includes(s.id));
  const phases = job.definition_phases ?? [];
  const phaseStatus = (names: string[]) => phases.filter(p => names.includes(p.name)).map(p =>
    (p.status === "succeeded" ? "done" : p.status === "failed" ? "error" : p.status) as WorkflowProgressStepStatus
  );
  const legacyConceptDetails: Progress[] = phases.some(p => p.name === "concepts") ? [] : phases.filter(p => ["shared", "capabilities"].includes(p.name)).map(p => ({
    id: p.name, label: t(`ontologyResults.phase.${p.name}`),
    status: (p.status === "succeeded" ? "done" : p.status === "failed" ? "error" : p.status) as WorkflowProgressStepStatus,
    statusLabel: t(`ontologyResults.phaseStatus.${p.status}`), description: p.detail_ja,
  }));
  const registration = take(["proposal_registration"]);
  const registrationStatus = aggregateStatus(registration.map(s => s.status));
  // 旧 job は検証・保存を registration に含む。経過を複製せず Markdown に一度だけ残す。
  const modern = phases.some(p => p.name === "markdown");
  const terminal = ["succeeded", "succeeded_with_warnings", "failed", "cancelled"].includes(job.status);
  const groups: { id: Stage; label: string; children: Progress[]; statuses: WorkflowProgressStepStatus[] }[] = [
    { id: "prepare", label: "markdownOntology.progress.prepare", children: take(["source_extraction", "schema_context"]), statuses: phaseStatus(["freeze", "evidence"]) },
    { id: "concepts", label: "markdownOntology.progress.concepts", children: [...take(["schema_naming", "qa_extraction", "text_extraction"]), ...legacyConceptDetails], statuses: phaseStatus(phases.some(p => p.name === "concepts") ? ["concepts"] : ["objects", "shared", "capabilities"]) },
    { id: "validation", label: "ontologyResults.phase.validation", children: [], statuses: phaseStatus(["validation"]) },
    { id: "markdown", label: "ontologyResults.phase.markdown", children: modern ? [] : registration, statuses: modern ? phaseStatus(["markdown"]) : [registrationStatus] },
    { id: "save", label: "ontologyResults.phase.save", children: modern ? registration : [], statuses: modern ? phaseStatus(["save"]) : [registrationStatus === "done" ? "done" : "pending"] },
  ];
  if (!groups[2].statuses.length) {
    groups[2].statuses = [registrationStatus === "running" || registrationStatus === "done" ? "done" : "pending"];
  }
  return groups.map(({ id, label, children, statuses }) => {
    // フェーズが存在する新 job ではそれを正本にする。入力別失敗は集約に残す。
    let status = aggregateStatus(statuses.length ? [...statuses, ...children.filter(s => s.status === "error").map(s => s.status)] : children.map(s => s.status));
    if (terminal && (status === "running" || status === "pending")) status = job.status === "failed" && status === "running" ? "error" : "skipped";
    const dataStatus = status === "done" ? "succeeded" : status === "error" ? "failed" : status;
    return {
      id, label: t(label), status, dataStatus,
      testId: `ontology-build-stage-${id}`,
      statusLabel: t(`profiles.ontologyBuild.stepStatus.${dataStatus}`),
      open: status === "running" || status === "error",
      content: <div className="grid gap-3">
        {id === "concepts" && <div className="grid gap-2 text-sm">
          {[conceptKinds.slice(0, 6), conceptKinds.slice(6)].map((kinds, i) => <Disclosure key={i} variant="plain" summary={t(i ? "markdownOntology.auxConcepts" : "markdownOntology.mainConcepts")}>
            <ul className="grid gap-1 pl-4">{kinds.map(kind => {
              const coverage = job.concept_coverage?.find(c => c.kind === kind);
              return <li key={kind}>{conceptLabel(kind)} · {coverage?.count ? t("profiles.ontologyBuild.concepts.count", {count: coverage.count}) : coverage?.reason_ja || t("ontologyResults.phaseStatus.pending")}</li>;
            })}</ul>
          </Disclosure>)}
        </div>}
        {/* 実行中・失敗の工程は開く（状態が変わったときに開き直す）。 */}
        {children.map(step => <Disclosure key={`${step.id}:${step.open ? "open" : "closed"}`} variant="plain" defaultOpen={step.open} data-testid={step.testId} data-step-status={step.dataStatus}
          summary={`${step.label} · ${step.statusLabel}${step.elapsedLabel ? ` · ${step.elapsedLabel}` : ""}`} summaryClassName="font-normal">
          <p className="text-sm text-fg-muted">{step.description}</p>
          {step.content}
        </Disclosure>)}
        {phases.filter(p => buildEventStage(p.name) === id && p.detail_ja && !children.some(child => child.id === p.name)).map(p => <p key={p.name} className="text-sm text-fg-muted">{p.detail_ja}</p>)}
        {stageEvents[id]}
      </div>,
    };
  });
}
