import { useWorkspaceState, useWorkspaceRevalidation, useResetExecutionConsent } from "@/components/WorkspaceState";
import { useCallback, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Play, X } from "lucide-react";

import {
  Button,
  PageHeader,
  PageBody,
  ActionResultRegion,
  ExecutionConfirmationField,
  FormGrid,
  TextareaField,
} from "@production-ready/ui";

import {
  ExecutionActivityPanel,
  type ExecutionActivityStatus,
} from "@/components/ExecutionActivityPanel";
import { apiPost } from "@/lib/api";
import type { OperationTimestamp } from "@/lib/operationTiming";
import { t } from "@/lib/i18n";
import {
  DbAdminExecutionResult,
  SqlFileInput,
  dbAdminExecutionActivityStatus,
} from "../components/DbAdminShared";
import { DEFAULT_SQL_ROW_LIMIT, RowLimitField, parseSqlRowLimit } from "../components/SqlRowLimitControls";
import {
  SchemaRefreshHeaderStatus,
  SchemaRefreshProcessing,
  SchemaSyncNotice,
} from "../components/SchemaRefreshFeedback";
import { useSchemaRefreshCoordinator } from "../SchemaRefreshCoordinator";
import { nl2sqlIncrementalKeys } from "../incrementalQueries";
import { useMutationSchemaSync } from "../useMutationSchemaSync";
import type { DbAdminExecuteData } from "../types";

const ADMIN_EXECUTE_CONFIRMATION = "ADMIN_EXECUTE";
/** 結果の中の案内の「DB 構造を再取得」の起点（SchemaRefreshCoordinator の start(origin)。#821 / #2031）。 */
const ADMIN_SQL_SCHEMA_REFRESH_NOTICE = "admin-sql-notice";
const MUTATING_SQL_TOKEN =
  /\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|begin|declare|call)\b/i;
const Q_QUOTE_CLOSERS: Record<string, string> = {
  "[": "]",
  "(": ")",
  "{": "}",
  "<": ">",
};

function stripLeadingSqlComments(sql: string): string {
  let rest = sql.trim();
  let changed = true;
  while (changed) {
    changed = false;
    if (rest.startsWith("--")) {
      const nextLine = rest.indexOf("\n");
      rest = nextLine >= 0 ? rest.slice(nextLine + 1).trimStart() : "";
      changed = true;
    }
    if (rest.startsWith("/*")) {
      const close = rest.indexOf("*/");
      if (close < 0) return rest;
      rest = rest.slice(close + 2).trimStart();
      changed = true;
    }
  }
  return rest;
}

function isSingleSelectSql(sql: string): boolean {
  const stripped = stripLeadingSqlComments(sql);
  const masked = maskSqlLiteralsAndComments(stripped);
  const normalized = masked.trim().replace(/;+$/g, "").trim();
  if (!normalized || normalized.includes(";")) return false;
  if (MUTATING_SQL_TOKEN.test(normalized)) return false;
  const head = normalized.replace(/^\(+/u, "").trimStart();
  return /^(select|with)\b/i.test(head);
}

function maskSqlLiteralsAndComments(sql: string): string {
  const text = String(sql || "");
  let out = "";
  let index = 0;
  while (index < text.length) {
    const char = text[index] ?? "";
    const nextChar = text[index + 1] ?? "";
    if (char === "-" && nextChar === "-") {
      const newline = text.indexOf("\n", index);
      const end = newline >= 0 ? newline : text.length;
      out += " ".repeat(end - index);
      index = end;
      continue;
    }
    if (char === "/" && nextChar === "*") {
      const close = text.indexOf("*/", index + 2);
      if (close < 0) {
        out += text.slice(index);
        break;
      }
      const end = close + 2;
      out += " ".repeat(end - index);
      index = end;
      continue;
    }
    const previous = index > 0 ? text[index - 1] ?? "" : "";
    if (
      (char === "q" || char === "Q") &&
      nextChar === "'" &&
      index + 2 < text.length &&
      !/[A-Za-z0-9_$#]/u.test(previous)
    ) {
      const opener = text[index + 2] ?? "";
      const closer = Q_QUOTE_CLOSERS[opener] ?? opener;
      const close = text.indexOf(`${closer}'`, index + 3);
      if (close < 0) {
        out += text.slice(index);
        break;
      }
      const end = close + 2;
      out += " ".repeat(end - index);
      index = end;
      continue;
    }
    if (char === "'" || char === '"') {
      let end = index + 1;
      let closed = false;
      while (end < text.length) {
        if (text[end] !== char) {
          end += 1;
          continue;
        }
        if (char === "'" && text[end + 1] === "'") {
          end += 2;
          continue;
        }
        closed = true;
        break;
      }
      if (!closed) {
        out += text.slice(index);
        break;
      }
      end += 1;
      out += " ".repeat(end - index);
      index = end;
      continue;
    }
    out += char;
    index += 1;
  }
  return out;
}

interface ExecutionRunState {
  operationKey: string | number;
  status: ExecutionActivityStatus;
  startedAt: OperationTimestamp;
  finishedAt?: OperationTimestamp;
  elapsedMs?: number | null;
}

function executionLabel(status: ExecutionActivityStatus) {
  if (status === "success") return t("executionActivity.execute.success");
  if (status === "error") return t("executionActivity.execute.error");
  return t("nl2sql.processing.executeSql");
}

/** 管理者向け SQL 実行ページ。更新系 SQL は確認語・RBAC・監査を必須とする。 */
export function AdminSqlPage() {
  const queryClient = useQueryClient();
  useWorkspaceRevalidation();
  const [sqlText, setSqlText] = useWorkspaceState("sqlText", "");
  const [sqlFileResetSignal, setSqlFileResetSignal] = useState(0);
  const [confirmation, setConfirmation] = useState("");
  const [sqlError, setSqlError] = useState("");
  const [result, setResult] = useState<DbAdminExecuteData | null>(null);
  const [rowLimitInput, setRowLimitInput] = useWorkspaceState("rowLimitInput", String(DEFAULT_SQL_ROW_LIMIT));
  const [executedRowLimit, setExecutedRowLimit] = useState<number | null>(null);
  const [executionRun, setExecutionRun] = useState<ExecutionRunState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  useResetExecutionConsent(() => { setConfirmation(""); }, JSON.stringify([sqlText, rowLimitInput]));
  const sharedSchemaRefresh = useSchemaRefreshCoordinator();
  const trimmedSql = sqlText.trim();
  const requiresConfirmation = Boolean(trimmedSql) && !isSingleSelectSql(trimmedSql);
  const confirmed = confirmation.trim() === ADMIN_EXECUTE_CONFIRMATION;
  const rowLimit = parseSqlRowLimit(rowLimitInput);
  const rowLimitError =
    !requiresConfirmation && rowLimit === null ? t("queryResults.rowLimit.error") : "";
  // 管理 SQL の未入力は押せないボタンだけにせず、押したときに欄の直下へ理由を出す（#541）。
  // 取得件数上限と確認語は、欄の直下（説明）に理由を出したうえで押せなくする。
  const canExecute =
    !loading &&
    (!trimmedSql ||
      ((requiresConfirmation || rowLimit !== null) && (!requiresConfirmation || confirmed)));
  const canClear = Boolean(
    sqlText || rowLimitInput !== String(DEFAULT_SQL_ROW_LIMIT) || result || executionRun
  );

  // queryClient だけに依存する固定の関数にする（同期・再取得の完了で一度だけ呼ばれる）。
  const refreshSchemaReadModels = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["schema", "objects"] });
    void queryClient.invalidateQueries({ queryKey: ["nl2sql", "db-admin", "objects"] });
    void queryClient.invalidateQueries({ queryKey: nl2sqlIncrementalKeys.schemaHead });
  }, [queryClient]);
  // DB 構造の同期の案内と進行は、実行の結果の直下（結果のカードの中）の 1 か所に出す（#2031）。
  const schemaSync = useMutationSchemaSync({
    noticeOrigin: ADMIN_SQL_SCHEMA_REFRESH_NOTICE,
    onDone: refreshSchemaReadModels,
  });

  const execute = async () => {
    if (!canExecute) return;
    if (!trimmedSql) {
      setSqlError(t("nl2sql.adminSqlRunner.error.required"));
      document.getElementById("admin-sql-input")?.focus();
      return;
    }
    setSqlError("");
    const executionRowLimit = requiresConfirmation ? DEFAULT_SQL_ROW_LIMIT : rowLimit;
    if (executionRowLimit === null) return;
    const startedAt = Date.now();
    const operationKey = `admin-sql-execute-${startedAt}`;
    setLoading(true);
    setError("");
    setResult(null);
    schemaSync.reset();
    setExecutedRowLimit(null);
    setExecutionRun({
      operationKey,
      status: "running",
      startedAt,
    });
    try {
      const data = await apiPost<DbAdminExecuteData>("/api/nl2sql/db-admin/execute", {
        sql: trimmedSql,
        row_limit: executionRowLimit,
        confirmation: requiresConfirmation ? confirmation.trim() : "",
        reason: requiresConfirmation ? "admin-sql-admin" : "admin-sql-select",
      });
      const finishedAt = data.timing.finished_at ?? Date.now();
      setResult(data);
      setExecutedRowLimit(requiresConfirmation ? null : executionRowLimit);
      setExecutionRun({
        operationKey,
        status: dbAdminExecutionActivityStatus(data),
        startedAt: data.timing.started_at ?? startedAt,
        finishedAt,
        elapsedMs: data.timing.elapsed_ms,
      });
      const schemaRefreshTracked = schemaSync.track(data);
      if (data.committed && !schemaRefreshTracked) {
        void queryClient.invalidateQueries({ queryKey: ["nl2sql", "db-admin", "objects"] });
      }
    } catch (err) {
      const finishedAt = Date.now();
      setError(err instanceof Error ? err.message : t("nl2sql.error.executeSqlFailed"));
      setResult(null);
      setExecutedRowLimit(null);
      setExecutionRun({
        operationKey,
        status: "error",
        startedAt,
        finishedAt,
        elapsedMs: finishedAt - startedAt,
      });
    } finally {
      setLoading(false);
    }
  };

  const clear = () => {
    setSqlText("");
    setSqlError("");
    setConfirmation("");
    setResult(null);
    setExecutedRowLimit(null);
    setExecutionRun(null);
    setRowLimitInput(String(DEFAULT_SQL_ROW_LIMIT));
    setError("");
    schemaSync.reset();
    setSqlFileResetSignal((value) => value + 1);
  };

  const actionButtons = (
    <>
      <Button
        type="button"
        variant={requiresConfirmation ? "danger" : "primary"}
        size="lg"
        className="w-full sm:w-auto"
        loading={loading}
        disabled={!canExecute}
        onClick={() => void execute()} icon={Play}>
        <span>{t("nl2sql.action.executeSql")}</span>
      </Button>
      <Button
        type="button"
        variant="secondary"
        size="lg"
        className="w-full sm:w-auto"
        disabled={!canClear || loading}
        onClick={clear} icon={X}>
        <span>{t("nl2sql.action.clearSql")}</span>
      </Button>
    </>
  );

  return (
    <>
      <PageHeader wide
        title={t("nav.adminSql")}
        subtitle={t("nl2sql.adminSqlRunner.description")}
        status={<SchemaRefreshHeaderStatus testId="admin-sql-schema-refresh-status" />}
      />
      <PageBody wide className="grid gap-4" data-testid="nl2sql-admin-sql">
        <section className="grid gap-4 rounded-md border border-border bg-surface p-4">
          {/* 入力の並び（SQL・ファイル・確認語または取得件数上限と操作）は FormGrid の読みやすい幅（60rem）まで。
              結果はカードの幅で出す（#1891）。 */}
          <FormGrid columns={1}>
            <TextareaField
              id="admin-sql-input"
              label={t("nl2sql.adminSqlRunner.label")}
              required
              error={sqlError || undefined}
              value={sqlText}
              onChange={(event) => {
                setSqlText(event.currentTarget.value);
                setSqlError("");
              }}
              disabled={loading}
              rows={12}
              monospace
              width="full"
              placeholder={t("nl2sql.adminSqlRunner.placeholder")}
            />
            <SqlFileInput
              resetSignal={sqlFileResetSignal}
              disabled={loading}
              onLoad={(text) => {
                setSqlText(text);
                // 手入力と同じく、読み込んだら未入力のエラーを消す（#933）。
                setSqlError("");
                setConfirmation("");
                setResult(null);
                setExecutedRowLimit(null);
                setExecutionRun(null);
                setError("");
              }}
            />
            {requiresConfirmation ? (
              <ExecutionConfirmationField
                value={confirmation}
                onChange={setConfirmation}
                confirmed={confirmed}
                placeholder={ADMIN_EXECUTE_CONFIRMATION}
                expectedLabel={ADMIN_EXECUTE_CONFIRMATION}
                helper={t("nl2sql.adminSqlRunner.adminHelper")}
                disabled={loading}
                actions={actionButtons}
              />
            ) : (
              // 取得件数上限は短い数値の幅（xs。#613）。操作ボタンはヘルパーテキストの下の行に置く。
              <div className="grid grid-cols-1 gap-3 border-t border-border pt-4">
                <RowLimitField
                  value={rowLimitInput}
                  onChange={setRowLimitInput}
                  disabled={loading}
                  error={rowLimitError}
                />
                <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
                  {actionButtons}
                </div>
              </div>
            )}
          </FormGrid>
          {executionRun && (
            <ExecutionActivityPanel
              inputSignature={JSON.stringify([sqlText, requiresConfirmation ? null : rowLimit])}
              status={executionRun.status}
              label={executionLabel(executionRun.status)}
              operationKey={executionRun.operationKey}
              startedAt={executionRun.startedAt}
              finishedAt={executionRun.finishedAt}
              elapsedMs={executionRun.elapsedMs}
              testId="admin-sql-execution-activity"
            />
          )}
          <ActionResultRegion
            loading={loading}
            operationKey="admin-sql-execute"
            loadingLabel={t("nl2sql.processing.executeSql")}
            errorMessage={error}
            testId="admin-sql-processing"
          >
            {result ? (
              <DbAdminExecutionResult result={result} rowLimit={executedRowLimit} />
            ) : null}
          </ActionResultRegion>
          <SchemaSyncNotice
            sync={schemaSync}
            testId="admin-sql-schema-sync-notice"
            processingTestId="admin-sql-schema-refresh-processing"
          />
          {/* 別の画面・起点で始めた再取得の進行（案内が進行を出している間は重ねない）。 */}
          {sharedSchemaRefresh.isRefreshing && !schemaSync.ownsProgress ? (
            <SchemaRefreshProcessing testId="admin-sql-schema-refresh-processing" />
          ) : null}
        </section>
      </PageBody>
    </>
  );
}
