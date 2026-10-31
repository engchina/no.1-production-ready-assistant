import { ApiError, isTimeoutError } from "../../lib/api.ts";

export type OntologyWorkspaceErrorSource = "profile" | "ontology";
export type OntologyWorkspaceErrorKind = "timeout" | "api" | "unknown";

export interface OntologyWorkspaceFailure {
  source: OntologyWorkspaceErrorSource;
  kind: OntologyWorkspaceErrorKind;
  publicMessage?: string;
}

export interface OntologyWorkspaceErrorPresentation {
  key: string;
  params?: Record<string, string>;
}

export function classifyOntologyWorkspaceError(
  profileError: unknown,
  ontologyError: unknown
): OntologyWorkspaceFailure | null {
  const source: OntologyWorkspaceErrorSource | null = profileError
    ? "profile"
    : ontologyError
      ? "ontology"
      : null;
  if (!source) return null;

  const error = source === "profile" ? profileError : ontologyError;
  if (isTimeoutError(error)) return { source, kind: "timeout" };
  if (error instanceof ApiError) {
    return {
      source,
      kind: "api",
      publicMessage: error.messages[0] ?? error.message,
    };
  }
  return { source, kind: "unknown" };
}

export function ontologyWorkspaceErrorPresentation(
  failure: OntologyWorkspaceFailure
): OntologyWorkspaceErrorPresentation {
  if (failure.kind === "api" && failure.publicMessage) {
    return {
      key: "ontologyBuild.workspace.apiError",
      params: { message: failure.publicMessage },
    };
  }
  if (failure.source === "profile") {
    return failure.kind === "timeout"
      ? { key: "ontologyBuild.workspace.profileTimeout" }
      : { key: "ontologyBuild.workspace.profileError" };
  }
  return failure.kind === "timeout"
    ? { key: "ontologyBuild.workspace.timeout" }
    : { key: "ontologyBuild.workspace.error" };
}
