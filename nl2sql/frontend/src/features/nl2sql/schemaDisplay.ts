import { t } from "../../lib/i18n";
import { formatSchemaCount } from "./schemaDisplayCore";
import type { SchemaTable } from "./types";

export { formatSampleValues, formatSchemaCount } from "./schemaDisplayCore";

export function schemaTableRowLabel(table: SchemaTable) {
  return table.row_count == null
    ? t("schema.table.rowsUnknown")
    : t("schema.table.rows", { count: formatSchemaCount(table.row_count) });
}
