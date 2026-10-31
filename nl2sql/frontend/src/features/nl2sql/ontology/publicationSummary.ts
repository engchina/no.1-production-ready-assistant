import type { OntologyFinding } from "./types";

/** 公開前の確認で受け取る業務定義（backend の 13 分類の定義をそのまま JSON にしたもの）。 */
export type PublicationDefinition = Record<string, unknown>;

export interface GraphObjectSummary {
  apiName: string;
  label: string;
  physical: string;
  properties: string[];
  links: string[];
}

export interface GraphSummary {
  objects: GraphObjectSummary[];
  objectCount: number;
  linkCount: number;
  propertyCount: number;
  /** 指標・業務ルール・共有プロパティなど、オブジェクト・関係・プロパティ以外（値型は数えない）。 */
  otherCount: number;
}

/** 利用者に見せない内部の概念（型名だけの値型など）。構成の件数に出さない（#1720）。 */
const INTERNAL_KINDS = new Set(["value_type"]);

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function list(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim() !== "") : [];
}

function mappings(definition: PublicationDefinition): { owner: string; object_name: string }[] {
  const value = definition.mappings;
  return Array.isArray(value)
    ? value.filter((item): item is { owner: string; object_name: string } =>
      typeof item === "object" && item !== null && typeof (item as { object_name?: unknown }).object_name === "string")
    : [];
}

/**
 * 概念の表示名。同じ名前が複数あるプロパティには、属するオブジェクトの名前を添える
 * （例「部門名（従業員部門ビュー）」）。どのオブジェクトの項目か分かるようにする（#1720）。
 * 同じ定義（api_name）を何度渡しても 1 つと数え、名前に所属の名前が既にあれば重ねない（#1729）。
 */
export function conceptDisplayNames(definitions: PublicationDefinition[]): Map<string, string> {
  const byApiName = new Map<string, PublicationDefinition>();
  for (const definition of definitions) {
    const apiName = text(definition.api_name);
    if (apiName) byApiName.set(apiName, definition);
  }
  const unique = [...byApiName.values(), ...definitions.filter(definition => !text(definition.api_name))];
  const counts = new Map<string, number>();
  for (const definition of unique) {
    const name = text(definition.name_ja) || text(definition.api_name);
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  const names = new Map<string, string>();
  for (const definition of unique) {
    const apiName = text(definition.api_name);
    const name = text(definition.name_ja) || apiName;
    const owner = byApiName.get(text(definition.object_type));
    const ownerName = owner ? text(owner.name_ja) || text(owner.api_name) : "";
    const qualified = (counts.get(name) ?? 0) > 1 && ownerName && !name.includes(`（${ownerName}）`);
    const label = qualified ? `${name}（${ownerName}）` : name;
    if (apiName) names.set(apiName, label);
    const id = text(definition.id);
    if (id) names.set(id, label);
  }
  return names;
}

/** graph として何ができたか（オブジェクトと、その関係・プロパティ）の要約。 */
export function summarizeOntologyGraph(definitions: PublicationDefinition[]): GraphSummary {
  const names = conceptDisplayNames(definitions);
  const label = (apiName: string) => names.get(apiName) ?? apiName;
  const objects = definitions.filter(definition => definition.kind === "object_type");
  const properties = definitions.filter(definition => definition.kind === "property");
  const links = definitions.filter(definition => definition.kind === "link_type");
  const summaries = objects.map(object => {
    const apiName = text(object.api_name);
    const owned = properties.filter(property => text(property.object_type) === apiName).map(property => text(property.api_name));
    const propertyNames = [...new Set([...list(object.properties), ...owned])];
    const physical = [...new Set(mappings(object).map(mapping => [mapping.owner, mapping.object_name].filter(Boolean).join(".")))].join(" / ");
    const objectLinks = links.flatMap(link => {
      const source = text(link.source);
      const target = text(link.target);
      if (source !== apiName && target !== apiName) return [];
      const other = source === apiName ? target : source;
      const name = text(link.name_ja);
      return [`${name ? `${name}: ` : ""}${source === apiName ? "→" : "←"} ${label(other)}`];
    });
    // オブジェクトの中の一覧なので、プロパティは素の名前で出す（どのオブジェクトかは見出しで分かる）。
    const plain = (name: string) => {
      const definition = definitions.find(item => text(item.api_name) === name);
      return (definition && text(definition.name_ja)) || name;
    };
    return { apiName, label: label(apiName), physical, properties: propertyNames.map(plain), links: objectLinks };
  });
  const counted = new Set(["object_type", "property", "link_type", ...INTERNAL_KINDS]);
  return {
    objects: summaries,
    objectCount: objects.length,
    linkCount: links.length,
    propertyCount: properties.length,
    otherCount: definitions.filter(definition => !counted.has(text(definition.kind))).length,
  };
}

export interface FindingPartition {
  blocking: OntologyFinding[];
  warnings: OntologyFinding[];
  /** graph は作れるが、補えば精度が上がる項目（公開を止めない。控えめに 1 か所にまとめる）。 */
  improvements: OntologyFinding[];
}

export function partitionFindings(findings: OntologyFinding[]): FindingPartition {
  return {
    blocking: findings.filter(finding => finding.severity === "error"),
    warnings: findings.filter(finding => finding.severity === "warning"),
    improvements: findings.filter(finding => finding.severity === "info"),
  };
}

/** 診断の対象の業務の名前（ID ではなく表示名。分からなければ空）。 */
export function findingName(finding: OntologyFinding, names?: Map<string, string>): string {
  const id = finding.definition_id ?? "";
  return (id && names?.get(id)) || "";
}

/** 診断の技術的な詳細（ID・項目・診断コード）。既定では畳んで出す（#1729）。 */
export function findingTarget(finding: OntologyFinding): string {
  return [finding.definition_id, finding.field, finding.code].filter(Boolean).join(" / ");
}

/** 前の公開版との差分（backend の `differences`）。 */
export interface PublicationDifference {
  id: string;
  before: PublicationDefinition | null;
  after: PublicationDefinition | null;
}

export type ChangeStatus = "added" | "changed" | "removed";

export interface ChangeRow {
  id: string;
  apiName: string;
  name: string;
  kind: string;
  status: ChangeStatus;
  /** 変更した項目（変更のときだけ。根拠・内部の番号など利用者が直さない項目は除く）。 */
  fields: string[];
  /** この概念の公開を止める問題・警告（どうすればよいかを含む平易な文）。 */
  issues: OntologyFinding[];
  before: PublicationDefinition | null;
  after: PublicationDefinition | null;
}

export interface ChangeSummary {
  /** 前の公開版が無く、すべてが追加。 */
  first: boolean;
  total: number;
  counts: Record<ChangeStatus, number>;
  /** 確認が要るもの（問題・警告のある概念と、削除）。 */
  review: ChangeRow[];
  /** 問題の無い変更（畳んで出す）。 */
  others: ChangeRow[];
}

const UNREVIEWED_FIELDS = new Set(["id", "evidence", "review_status", "origin", "missing_information_ja"]);

function changedFields(before: PublicationDefinition, after: PublicationDefinition): string[] {
  const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])];
  return keys.filter(key => !UNREVIEWED_FIELDS.has(key) && JSON.stringify(before[key] ?? null) !== JSON.stringify(after[key] ?? null));
}

/**
 * 「概念ごとの差分」の要約（#1729）。確認が要る変更を上に、問題の無い変更は畳む。
 * 初回の公開（すべてが追加）は件数の 1 行と確認が要るものだけにする。値型など内部の概念は出さない。
 */
export function summarizeChanges(
  differences: PublicationDifference[],
  findings: OntologyFinding[],
  definitions: PublicationDefinition[],
): ChangeSummary {
  const shown = differences.filter(difference => !INTERNAL_KINDS.has(text((difference.after ?? difference.before)?.kind)));
  const names = conceptDisplayNames([
    ...definitions,
    ...shown.map(difference => difference.before).filter((value): value is PublicationDefinition => Boolean(value)),
  ]);
  const issuesFor = (definition: PublicationDefinition) => {
    const keys = new Set([text(definition.id), text(definition.api_name)].filter(Boolean));
    return findings.filter(finding => ["error", "warning"].includes(finding.severity) && keys.has(finding.definition_id ?? ""));
  };
  const rows = shown.map((difference): ChangeRow => {
    const definition = (difference.after ?? difference.before)!;
    const status: ChangeStatus = difference.before ? difference.after ? "changed" : "removed" : "added";
    const apiName = text(definition.api_name);
    return {
      id: difference.id,
      apiName,
      name: names.get(apiName) || text(definition.name_ja) || apiName || difference.id,
      kind: text(definition.kind),
      status,
      fields: status === "changed" ? changedFields(difference.before!, difference.after!) : [],
      issues: issuesFor(definition),
      before: difference.before,
      after: difference.after,
    };
  });
  const review = rows.filter(row => row.issues.length > 0 || row.status === "removed");
  const counts = { added: 0, changed: 0, removed: 0 };
  for (const row of rows) counts[row.status] += 1;
  return {
    first: rows.length > 0 && rows.every(row => row.status === "added"),
    total: rows.length,
    counts,
    review: [...review].sort((a, b) => Number(b.issues.some(i => i.severity === "error")) - Number(a.issues.some(i => i.severity === "error"))),
    others: rows.filter(row => !review.includes(row)),
  };
}

/** 業務説明に追記する改善案の 1 文（backend の `suggestions`）。 */
export interface ImprovementSuggestion {
  severity: string;
  text: string;
}

function normalized(value: string): string {
  return value.replace(/\s+/g, "").normalize("NFKC");
}

/** まだ業務説明に書かれていない改善案（既に書かれた文は重ねて出さない。#1729）。 */
export function pendingSuggestions(suggestions: ImprovementSuggestion[], businessText: string): ImprovementSuggestion[] {
  const written = normalized(businessText);
  return suggestions.filter(item => item.text.trim() && !written.includes(normalized(item.text)));
}

/** 業務説明に改善案を追記した値（前の内容は残し、改行で区切って後ろに足す）。 */
export function appendToBusinessText(current: string, addition: string): string {
  const head = current.replace(/\s+$/u, "");
  return head ? `${head}\n${addition}` : addition;
}
