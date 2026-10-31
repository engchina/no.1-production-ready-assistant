import { formatDbObjectName } from "./dbObjectIdentity";
import type { DbAdminObjectDetail } from "./types";

export function buildMetadataInputTexts(
  details: DbAdminObjectDetail[],
  sampleLimit: number
) {
  const structure: string[] = [];
  const primaryKeys: string[] = [];
  const foreignKeys: string[] = [];
  const samples: string[] = [];

  for (const detail of details) {
    const qualifiedName = formatDbObjectName(detail);
    structure.push(
      [
        `OBJECT: ${qualifiedName}`,
        `TYPE: ${detail.object_type}`,
        `COMMENT: ${detail.comment || "-"}`,
        "COLUMNS:",
        ...detail.columns.map(
          (column) =>
            `- ${column.column_name}: ${column.data_type} ` +
            `NULLABLE=${column.nullable ? "Y" : "N"} ` +
            // 既存ドメインは COMMENT より前に置く(コメント本文と混ざらない)。
            (column.domain_name ? `DOMAIN=${column.domain_name} ` : "") +
            `COMMENT=${column.comment || column.logical_name || "-"}`
        ),
      ].join("\n")
    );

    const constraints = detail.constraints ?? [];
    const pk = constraints.filter((constraint) => /\sP(\(|$)/.test(constraint));
    if (pk.length > 0) primaryKeys.push(`OBJECT: ${qualifiedName}\n${pk.join("\n")}`);
    const fk = constraints.filter((constraint) => /\sR(\(|$)/.test(constraint));
    if (fk.length > 0) foreignKeys.push(`OBJECT: ${qualifiedName}\n${fk.join("\n")}`);

    if (sampleLimit > 0) {
      const sampleLines = detail.columns
        .map((column) => {
          const values = column.sample_values.slice(0, sampleLimit).join(", ");
          return values ? `${column.column_name}: ${values}` : "";
        })
        .filter(Boolean);
      if (sampleLines.length > 0) samples.push(`OBJECT: ${qualifiedName}\n${sampleLines.join("\n")}`);
    }
  }

  return {
    structureText: structure.join("\n\n"),
    primaryKeyText: primaryKeys.join("\n\n"),
    foreignKeyText: foreignKeys.join("\n\n"),
    sampleText: samples.join("\n\n"),
  };
}
