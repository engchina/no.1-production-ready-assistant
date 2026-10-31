import { formatDbObjectName } from "../dbObjectIdentity";
import { conceptKinds, conceptLabel } from "./unifiedConcepts";
import { t } from "../../../lib/i18n";
import type { OntologyNode, OntologyNodeKind, OntologyValidationStatus } from "./types";

const BUSINESS_OBJECT_KINDS = new Set<OntologyNodeKind>([
  "business_entity",
  "object_type",
  "interface",
  "object_set",
  "business_event",
]);

const PHYSICAL_OBJECT_KINDS = new Set<OntologyNodeKind>(["table", "view"]);

function metadataString(node: OntologyNode, key: string): string {
  const value = node.metadata?.[key];
  return typeof value === "string" ? value : "";
}

function objectType(node: OntologyNode): string {
  const mappingType = node.physical_mappings?.[0]?.object_ref.object_type;
  return String(mappingType || metadataString(node, "object_type") || node.kind).toLowerCase();
}

export function physicalObjectLabel(node: OntologyNode): string {
  const objectRef = node.physical_mappings?.[0]?.object_ref;
  const owner = objectRef?.owner || metadataString(node, "owner");
  const objectName = objectRef?.object_name || metadataString(node, "object_name");
  if (objectName) return formatDbObjectName({ owner, name: objectName });
  return node.technical_name || "";
}

export function ontologyNodeKindLabel(kind: OntologyNodeKind): string {
  if ((conceptKinds as readonly string[]).includes(kind)) return conceptLabel(kind);
  switch (kind) {
    case "schema":
      return t("nl2sql.ontology.nodeKind.schema");
    case "table":
      return t("nl2sql.ontology.nodeKind.table");
    case "view":
      return t("nl2sql.ontology.nodeKind.view");
    case "column":
      return t("nl2sql.ontology.nodeKind.column");
    case "business_entity":
      return t("nl2sql.ontology.nodeKind.businessEntity");
    case "business_event":
      return t("nl2sql.ontology.nodeKind.businessEvent");
    case "property":
      return t("nl2sql.ontology.nodeKind.property");
    case "metric":
      return t("nl2sql.ontology.nodeKind.metric");
    case "business_term":
      return t("nl2sql.ontology.nodeKind.businessTerm");
    case "business_rule":
      return t("nl2sql.ontology.nodeKind.businessRule");
    case "enum_value":
      return t("nl2sql.ontology.nodeKind.enumValue");
    case "question_intent":
      return t("nl2sql.ontology.nodeKind.questionIntent");
    case "query_plan":
      return t("nl2sql.ontology.nodeKind.queryPlan");
    case "cte":
      return t("nl2sql.ontology.nodeKind.cte");
    case "sql_table":
      return t("nl2sql.ontology.nodeKind.sqlTable");
    case "sql_column":
      return t("nl2sql.ontology.nodeKind.sqlColumn");
    case "sql_join":
      return t("nl2sql.ontology.nodeKind.sqlJoin");
    case "sql_filter":
      return t("nl2sql.ontology.nodeKind.sqlFilter");
    case "sql_aggregate":
      return t("nl2sql.ontology.nodeKind.sqlAggregate");
    case "sql_group":
      return t("nl2sql.ontology.nodeKind.sqlGroup");
    case "sql_having":
      return t("nl2sql.ontology.nodeKind.sqlHaving");
    case "sql_order":
      return t("nl2sql.ontology.nodeKind.sqlOrder");
    case "sql_limit":
      return t("nl2sql.ontology.nodeKind.sqlLimit");
    case "sql_window":
      return t("nl2sql.ontology.nodeKind.sqlWindow");
    case "sql_artifact":
      return t("nl2sql.ontology.nodeKind.sqlArtifact");
    case "validation_finding":
      return t("nl2sql.ontology.nodeKind.validationFinding");
    case "execution_preview":
      return t("nl2sql.ontology.nodeKind.executionPreview");
    default:
      return t("nl2sql.ontology.nodeKind.unknown");
  }
}

function validationStatusLabel(status: OntologyValidationStatus | undefined): string {
  switch (status) {
    case "passed":
      return t("nl2sql.ontology.nodeValidation.passed");
    case "warning":
      return t("nl2sql.ontology.nodeValidation.warning");
    case "blocked":
      return t("nl2sql.ontology.nodeValidation.blocked");
    case "unreviewed":
    default:
      return t("nl2sql.ontology.nodeValidation.unreviewed");
  }
}

export function ontologyNodeSecondaryLabel(node: OntologyNode): string | null {
  const physicalLabel = physicalObjectLabel(node);
  const resolvedObjectType = objectType(node);
  if (BUSINESS_OBJECT_KINDS.has(node.kind) && physicalLabel) {
    if (resolvedObjectType === "view") {
      return t("nl2sql.ontology.nodeSecondary.businessMappingView", {
        object: physicalLabel,
      });
    }
    if (resolvedObjectType === "table") {
      return t("nl2sql.ontology.nodeSecondary.businessMappingTable", {
        object: physicalLabel,
      });
    }
    return t("nl2sql.ontology.nodeSecondary.businessMappingTarget", {
      object: physicalLabel,
    });
  }
  if (PHYSICAL_OBJECT_KINDS.has(node.kind) && physicalLabel) {
    return t("nl2sql.ontology.nodeSecondary.physicalName", { name: physicalLabel });
  }
  if (node.kind === "column" && node.technical_name) {
    return t("nl2sql.ontology.nodeSecondary.physicalColumn", {
      name: node.technical_name,
    });
  }
  if (node.technical_name && node.technical_name !== node.business_name_ja) {
    return t("nl2sql.ontology.nodeSecondary.technicalName", {
      name: node.technical_name,
    });
  }
  return null;
}

export interface OntologyNodeDisplay {
  primaryLabel: string;
  kindLabel: string;
  secondaryLabel: string | null;
  ariaLabel: string;
}

export function ontologyNodeDisplay(
  node: OntologyNode,
  options: { highlighted?: boolean } = {}
): OntologyNodeDisplay {
  const kindLabel = ontologyNodeKindLabel(node.kind);
  const secondaryLabel = ontologyNodeSecondaryLabel(node);
  const statusLabel = validationStatusLabel(node.validation_status);
  const parts = [node.business_name_ja, kindLabel];
  if (secondaryLabel) parts.push(secondaryLabel);
  parts.push(t("nl2sql.ontology.nodeAria.validation", { status: statusLabel }));
  if (options.highlighted) parts.push(t("nl2sql.ontology.nodeAria.highlighted"));
  return {
    primaryLabel: node.business_name_ja,
    kindLabel,
    secondaryLabel,
    ariaLabel: parts.join("、"),
  };
}

export function ontologyNodeSearchValues(node: OntologyNode): string[] {
  const display = ontologyNodeDisplay(node);
  return [
    display.primaryLabel,
    display.kindLabel,
    display.secondaryLabel ?? "",
    node.technical_name ?? "",
    node.description_ja ?? "",
    ...Object.values(node.metadata?.definition ?? {}).flatMap(value => typeof value === "string" ? [value] : Array.isArray(value) ? value.filter((v):v is string => typeof v === "string") : []),
    ...(node.aliases ?? []),
  ];
}
