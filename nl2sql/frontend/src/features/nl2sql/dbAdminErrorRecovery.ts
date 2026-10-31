import { t } from "@/lib/i18n";

/** 実行可能性を推測せず、既知のエラーコードに対して編集用の構文例を提示する。 */
export function dbAdminErrorRecovery(code: string | null) {
  if (code === "DB_ADMIN_COMMENT_SQL_POLICY_VIOLATION") {
    return {
      cause: t("dbAdmin.result.error.commentPolicy.cause"),
      actions: [
        t("dbAdmin.result.error.commentPolicy.action.view"),
        t("dbAdmin.result.error.commentPolicy.action.edit"),
      ],
      examples: [
        { label: t("dbAdmin.result.error.example.commentTable"), sql: t("dbAdmin.result.error.example.commentTable.sql") },
        { label: t("dbAdmin.result.error.example.commentColumn"), sql: t("dbAdmin.result.error.example.commentColumn.sql") },
        { label: t("dbAdmin.result.error.example.commentMaterializedView"), sql: t("dbAdmin.result.error.example.commentMaterializedView.sql") },
      ],
    };
  }
  if (code === "DB_ADMIN_ANNOTATION_SQL_POLICY_VIOLATION" || code === "ORA-11548") {
    return {
      cause: code === "ORA-11548"
        ? t("dbAdmin.result.error.ora11548.cause")
        : t("dbAdmin.result.error.annotationPolicy.cause"),
      actions: [
        t("dbAdmin.result.error.ora11548.action.name"),
        t("dbAdmin.result.error.ora11548.action.quote"),
        t("dbAdmin.result.error.ora11548.action.regenerate"),
      ],
      examples: [
        { label: t("dbAdmin.result.error.example.annotationTable"), sql: t("dbAdmin.result.error.example.annotationTable.sql") },
        { label: t("dbAdmin.result.error.example.annotationColumn"), sql: t("dbAdmin.result.error.example.annotationColumn.sql") },
      ],
    };
  }
  return undefined;
}
