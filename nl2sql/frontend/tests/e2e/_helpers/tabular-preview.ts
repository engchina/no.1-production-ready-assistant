import type { Page } from "./test";

/** 実行のエラーや同期を検査する spec 用の、DB を変えない取込プレビュー。 */
export async function mockTabularPreview(page: Page) {
  await page.route("**/api/nl2sql/db-admin/import-tabular", async (route) => {
    const request = route.request().postDataJSON() as Record<string, unknown>;
    if (!request.preview) return route.fallback();
    const types = request.column_types as Record<string, string> | undefined;
    const dataType = types?.ID ?? "NUMBER";
    return route.fulfill({ json: { data: {
      table_name: request.table_name, owner: "APP", qualified_name: `APP.${request.table_name}`,
      filename: request.filename, sheet_name: request.sheet_name, mode: request.mode,
      columns: [{ source_name: "ID", column_name: "ID", data_type: dataType, nullable: true }],
      row_count: 1, executed: false, preview: true, ddl: `CREATE TABLE "${request.table_name}" ("ID" ${dataType})`,
      insert_sql: `INSERT INTO "${request.table_name}" ("ID") VALUES (:c0)`,
      warnings: [], sample_rows: [{ ID: "1" }],
    } } });
  });
}
