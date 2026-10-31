import { WarningsBanner } from "@/components/WarningsBanner";
import {
  useWorkspaceState,
  useWorkspaceRevalidation,
  useResetExecutionConsent,
  useTransientDraftGuard,
  useWorkspaceActive,
  useWorkspaceUrlSelection,
} from "@/components/WorkspaceState";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Code2, Eye, RefreshCw, Table2, Upload } from "lucide-react";

import {
  Button,
  toast,
  StatusBadge,
  PageHeader,
  PageBody,
  ClearActionButton,
  ProcessingIndicator,
  ExecutionConfirmationField,
  FormGrid,
  TextField,
  useActionPending,
} from "@production-ready/ui";

import { RetryPageNotice } from "@/components/page-notice";
import { FileDropzone } from "@/components/ui/file-dropzone";
import { apiFetch, apiGet, apiPost, isTimeoutError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t } from "@/lib/i18n";
import { listLoadMoreErrorMessage } from "@/lib/load-more-error";
import { API_TIMEOUT_MS, requestTimeoutSeconds } from "@/lib/requestPolicy";
import { CORE_TABULAR_FILE_FORMATS } from "@/lib/tabular-file-formats";
import {
  StatementRunnerCard,
  DbAdminErrorNotice,
  downloadBlob,
  fileToBase64,
} from "../components/DbAdminShared";
import { QueryResultTable } from "../components/QueryResultTable";
import {
  DbObjectDetailPanel,
  DbObjectListDetail,
  DbObjectManagementPanelShell,
  DbObjectPanelHeader,
  DbObjectStepIndicator,
  DropDbObjectDialog,
  dbAdminExecuteFailureMessage,
  formatDbObjectName,
  compareDbObjectSortValues,
  dbObjectSortValue,
  parseDbAdminObjectTarget,
  type DbObjectDetailTab,
  type DbObjectOwnerPrefix,
  type DbObjectSortKey,
  type DbObjectSortState,
} from "../components/DbObjectManagementShared";
import { DbObjectName } from "../components/DbObjectName";
import type {
  DbAdminExecuteData,
  DbAdminImportTabularData,
  DbAdminObjectDetail,
} from "../types";
import { useDbAdminObjects } from "../incrementalQueries";
import { useSchemaRefreshCoordinator } from "../SchemaRefreshCoordinator";
import {
  SchemaRefreshHeaderStatus,
  SchemaRefreshProcessing,
  SchemaSyncNotice,
} from "../components/SchemaRefreshFeedback";
import { useMutationSchemaSync, type MutationSchemaSync } from "../useMutationSchemaSync";
import { dbAdminObjectCountsFromPage } from "../dbAdminObjectCounts";
import { useDbObjectDetailRequest } from "../useDbObjectDetailRequest";

/** ヘッダーの「スキーマを更新」の起点（SchemaRefreshCoordinator の start(origin)。#821）。 */
const TABLE_SCHEMA_REFRESH_HEADER = "table-management-header";
/** 一覧の操作（削除）の後の、ページの先頭の案内の「DB 構造を再取得」の起点（#2031）。 */
const TABLE_SCHEMA_REFRESH_NOTICE = "table-management-notice";
/** 作成・取込の結果の直下の案内の「DB 構造を再取得」の起点（#2031）。 */
const TABLE_TASK_SCHEMA_REFRESH_NOTICE = "table-management-task-notice";

type ActiveView = "list" | "create" | "import";
type ImportStep = "file" | "execute";

const IMPORT_MODE_LABEL_KEYS: Record<string, string> = {
  create: "tableMgmt.importWizard.mode.create",
  replace: "tableMgmt.importWizard.mode.replace",
  append: "tableMgmt.importWizard.mode.append",
  truncate: "tableMgmt.importWizard.mode.truncate",
};

function importModeLabel(mode: string) {
  const key = IMPORT_MODE_LABEL_KEYS[mode];
  return key ? t(key) : mode;
}

function ImportResultPanel({ result }: { result: DbAdminImportTabularData }) {
  return (
    <section
      className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3 text-sm"
      aria-label={t(result.preview ? "tableMgmt.importWizard.previewResult" : "tableMgmt.importWizard.result")}
      data-testid="table-import-result-panel"
    >
      <div className="flex flex-wrap items-center gap-2">
        <DbObjectName
          object={{ owner: result.owner, name: result.table_name, qualified_name: result.qualified_name }}
          size="sm"
          data-testid="table-import-result-table-name"
        />
        {/* 実行の状態・モードは API の内部値（executed / create）を出さず、文言で出す（#935）。 */}
        <StatusBadge
          variant={result.executed ? "success" : "warning"}
          label={t(result.preview ? "tableMgmt.importWizard.previewResult" : result.executed ? "dbAdmin.result.summary.executed" : "dbAdmin.result.summary.notExecuted")}
        />
        <StatusBadge icon={false} variant="info" label={t("tableMgmt.importWizard.rows", { count: result.row_count })} />
        <StatusBadge icon={false} variant="neutral" label={importModeLabel(result.mode)} />
      </div>
      <WarningsBanner warnings={result.warnings} />
      <pre className="overflow-auto rounded-md border border-border bg-surface p-3 font-mono text-sm leading-6 text-fg">
        <code>{`${result.ddl}\n\n${result.insert_sql}`}</code>
      </pre>
      {result.sample_rows.length > 0 && (
        <QueryResultTable
          results={{
            columns: Object.keys(result.sample_rows[0] ?? {}),
            rows: result.sample_rows,
            total: result.sample_rows.length,
          }}
          name={t("queryResults.name.importPreview")}
          csvFilePrefix="nl2sql-table-import-preview"
          testId="table-import-preview"
        />
      )}
    </section>
  );
}

function ImportWizard({
  table,
  sheet,
  filename,
  fileResetSignal,
  fileReady,
  result,
  step,
  confirmation,
  loading,
  previewing,
  canClear,
  error,
  schemaSync,
  pageSchemaSyncOwnsProgress,
  onTableChange,
  onSheetChange,
  onFilePick,
  onFileClear,
  onClear,
  onExecute,
  onPreview,
  columnTypes,
  onColumnTypeChange,
  onConfirmationChange,
  onReturnToList,
}: {
  table: string;
  sheet: string;
  filename: string;
  fileResetSignal: number;
  fileReady: boolean;
  result: DbAdminImportTabularData | null;
  step: ImportStep;
  confirmation: string;
  loading: boolean;
  previewing: boolean;
  canClear: boolean;
  error: unknown;
  schemaSync: MutationSchemaSync;
  /** ページの先頭の案内が進行を出している（取込の画面に同じ進行を重ねない）。 */
  pageSchemaSyncOwnsProgress: boolean;
  onTableChange: (value: string) => void;
  onSheetChange: (value: string) => void;
  onFilePick: (file: File | undefined) => void;
  onFileClear: () => void;
  onClear: () => void;
  onExecute: () => void;
  onPreview: () => void;
  columnTypes: Record<string, string>;
  onColumnTypeChange: (column: string, dataType: string) => void;
  onConfirmationChange: (value: string) => void;
  onReturnToList: () => void;
}) {
  const steps: Array<{ id: ImportStep; label: string }> = [
    { id: "file", label: t("tableMgmt.importWizard.stepFile") },
    { id: "execute", label: t("tableMgmt.importWizard.stepExecute") },
  ];
  const activeIndex = steps.findIndex((item) => item.id === step);
  const isConfirmed = confirmation.trim() === "ADMIN_EXECUTE";
  const lowerFilename = filename.trim().toLowerCase();
  const sheetRequired = lowerFilename ? !lowerFilename.endsWith(".csv") : true;
  const canExecute =
    Boolean(table.trim()) &&
    fileReady &&
    (!sheetRequired || Boolean(sheet.trim())) &&
    isConfirmed && Boolean(result) &&
    Boolean(result?.columns.every((column) => columnTypes[column.column_name] === column.data_type));

  return (
    <div className="grid gap-4">
      <DbObjectPanelHeader
        icon={Upload}
        title={t("dataTools.dbAdmin.importTitle")}
        description={t("tableMgmt.import.note")}
      />

      <DbObjectStepIndicator
        steps={steps.map((item) => item.label)}
        activeIndex={activeIndex}
        ariaLabel={t("tableMgmt.importWizard.steps")}
        dataTestId="table-import-steps"
      />

      {/* フォームの本文は FormGrid（最大 60rem・置き場所 42rem 以上で 2 列。#1891）。表名・シート名は名前なので md。 */}
      <FormGrid>
        <TextField
          id="table-import-table-name"
          label={t("dataTools.dbAdmin.tableName")}
          required
          value={table}
          disabled={loading}
          onValueChange={onTableChange}
          placeholder="IMPORTED_ORDERS"
          width="md"
          className="min-w-0"
        />
        <TextField
          id="table-import-sheet-name"
          label={t("dataTools.dbAdmin.sheet")}
          required={sheetRequired}
          value={sheet}
          disabled={loading}
          onValueChange={onSheetChange}
          placeholder="Sheet1"
          width="md"
          className="min-w-0"
        />

        <FileDropzone
          label={t("dataTools.dbAdmin.file")}
          accept={CORE_TABULAR_FILE_FORMATS.accept}
          selectedText={filename ? t("tableMgmt.importWizard.selectedFile", { filename }) : ""}
          formatLabel={CORE_TABULAR_FILE_FORMATS.formatLabel}
          resetSignal={fileResetSignal}
          required
          actionText={t("common.fileDropzone.action")}
          replaceText={t("tableMgmt.importWizard.fileReplace")}
          clearAriaLabel={t("tableMgmt.importWizard.clearFile")}
          icon="spreadsheet"
          className="col-span-full w-full"
          dataTestId="table-import-file-field"
          disabled={loading}
          onFiles={([file]) => onFilePick(file)}
          onClear={onFileClear}
        />
      </FormGrid>

      <div className="grid gap-3">
        <Button type="button" icon={Eye} onClick={onPreview} loading={loading}
          disabled={!table.trim() || !fileReady || (sheetRequired && !sheet.trim())}
          data-testid="table-import-preview-button">
          {t("tableMgmt.importWizard.preview")}
        </Button>
        {result && !result.executed && (
          <FormGrid>
            {result.columns.map((column) => (
              <TextField key={column.column_name} id={`table-import-column-${column.column_name}`}
                label={column.column_name} value={columnTypes[column.column_name] ?? column.data_type}
                disabled={loading} width="md"
                helper={t("tableMgmt.importWizard.columnTypeHint")}
                onValueChange={(value) => onColumnTypeChange(column.column_name, value)} />
            ))}
          </FormGrid>
        )}
      </div>

      <fieldset
        className="grid gap-3 rounded-md border border-border bg-surface p-3"
        data-testid="table-import-execution-fieldset"
      >
        <legend className="px-1 text-sm font-semibold text-fg">{t("tableMgmt.importWizard.executeTitle")}</legend>
        <ExecutionConfirmationField
          value={confirmation}
          disabled={loading}
          onChange={onConfirmationChange}
          confirmed={isConfirmed}
          placeholder="ADMIN_EXECUTE"
          expectedLabel="ADMIN_EXECUTE"
          helper={t("tableMgmt.importWizard.executeHint")}
          actions={
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="danger"
                size="lg"
                className="w-full sm:w-auto"
                loading={loading}
                disabled={!canExecute}
                onClick={onExecute} icon={Upload}>
                <span>{t("dataTools.dbAdmin.import")}</span>
              </Button>
              <ClearActionButton size="lg"
                label={t("dbAdmin.import.actions.clear")}
                className="w-full sm:w-auto"
                disabled={!canClear || loading}
                onClick={onClear}
              />
              <div className="w-full">
                {Boolean(error) && <DbAdminErrorNotice error={error} onReturnToList={onReturnToList} />}
              </div>
            </div>
          }
        />
        {loading ? (
          // 表の作成と行の投入を行うため、行数が多いと数十秒かかる（messaging.md §3.7）。
          <ProcessingIndicator
            active
            label={t(previewing ? "tableMgmt.importWizard.progress.previewing" : "tableMgmt.importWizard.progress.importing")}
            operationKey="table-import"
            placement="action"
            activityIcon="none"
            testId="table-import-processing"
          />
        ) : null}
        {result && <ImportResultPanel result={result} />}
        {/* 取込の後の DB 構造の同期の案内と進行は、取込の結果の直下の 1 か所（#2031）。 */}
        <SchemaSyncNotice
          sync={schemaSync}
          testId="table-import-schema-sync-notice"
          processingTestId="table-import-schema-refresh-processing"
          fallbackProgress={!pageSchemaSyncOwnsProgress}
        />
      </fieldset>
    </div>
  );
}

function objectListErrorMessage(error: unknown, fallbackKey: Parameters<typeof t>[0]) {
  if (isTimeoutError(error)) {
    return t("dataMgmt.objectList.timeout", {
      seconds: requestTimeoutSeconds(API_TIMEOUT_MS.interactiveList),
    });
  }
  return error instanceof Error ? error.message : t(fallbackKey);
}

export function TableManagementPage() {
  useWorkspaceRevalidation();
  const [detailTab, setDetailTab] = useWorkspaceState<DbObjectDetailTab>("detailTab", "columns");
  const [activeView, setActiveView] = useWorkspaceState<ActiveView>("activeView", "list");
  const [tableSearch, setTableSearch] = useWorkspaceState("tableSearch", "");
  const [tableOwnerPrefix, setTableOwnerPrefix] = useWorkspaceState<DbObjectOwnerPrefix>("tableOwnerPrefix", "");
  const [tableSort, setTableSort] = useWorkspaceState<DbObjectSortState>("tableSort", { key: "name", direction: "asc" });
  const [dropTargetName, setDropTargetName] = useState("");
  const [dropConfirmation, setDropConfirmation] = useState("");
  const [dropError, setDropError] = useState("");
  const [importTable, setImportTable] = useWorkspaceState("importTable", "");
  const [importFilename, setImportFilename] = useState("");
  const [importFileResetSignal, setImportFileResetSignal] = useState(0);
  const [importBase64, setImportBase64] = useState("");
  const [importSheet, setImportSheet] = useState("");
  const [importStep, setImportStep] = useState<ImportStep>("file");
  const [importConfirmation, setImportConfirmation] = useState("");
  const [importResult, setImportResult] = useState<DbAdminImportTabularData | null>(null);
  const [importColumnTypes, setImportColumnTypes] = useState<Record<string, string>>({});
  useTransientDraftGuard(Boolean(importBase64) && !importResult?.executed);
  const [importError, setImportError] = useState<unknown>(null);
  const [loading, setLoading] = useState("");
  // ヘッダーの「最新情報を取得」を押した取り直しの間だけ true（検索・絞り込み・他の操作の後の取り直しでは回さない。#819）。
  const manualRefresh = useActionPending();
  // 失敗の案内の「再試行」。案内を残したまま押したボタンだけを回し、進行は案内の中に出す（#2034）。
  const noticeRetry = useActionPending();
  const [message, setMessage] = useState("");
  useResetExecutionConsent(() => { setDropConfirmation(""); setDropTargetName(""); setImportConfirmation(""); }, JSON.stringify([importTable, importBase64, importSheet]));
  const sharedSchemaRefresh = useSchemaRefreshCoordinator();
  // 検索語・所有者の接頭辞は SearchField が確定した値（入力が止まって 300ms・Enter・消去。IME の変換中は
  // 確定しない）なので、ここでは遅延させずにそのまま問い合わせに使う（#535）。
  const tableObjectsQuery = useDbAdminObjects(
    tableSearch,
    "table",
    "all",
    tableOwnerPrefix,
    "name_comment"
  );
  const schemaRefreshing = sharedSchemaRefresh.isRefreshing;
  const headerSchemaRefreshStarting = sharedSchemaRefresh.startingOrigin === TABLE_SCHEMA_REFRESH_HEADER;
  const tableItems = useMemo(
    () => (tableObjectsQuery.data?.pages ?? []).flatMap((page) => page.items),
    [tableObjectsQuery.data]
  );
  const firstTablePage = tableObjectsQuery.data?.pages[0];
  const totalTableCount = dbAdminObjectCountsFromPage(firstTablePage, tableItems).totalCount;
  // 絞り込む前の全件（「N 件（全 M 件）」の M。#1891）。絞り込みの無いときは上の問い合わせと同じ queryKey なので
  // キャッシュを共有し、追加の要求は出ない。
  const allTableObjectsQuery = useDbAdminObjects("", "table", "all", "", "name_comment");
  const unfilteredTableCount = allTableObjectsQuery.data
    ? dbAdminObjectCountsFromPage(
        allTableObjectsQuery.data.pages[0],
        allTableObjectsQuery.data.pages.flatMap((page) => page.items)
      ).totalCount
    : undefined;
  const detailRequest = useDbObjectDetailRequest({
    collectionPath: "/api/nl2sql/db-admin/tables",
    loadErrorMessage: t("tableMgmt.error.detail"),
    timeoutErrorMessage: t("dbAdmin.detail.timeout", {
      seconds: requestTimeoutSeconds(API_TIMEOUT_MS.interactiveDetail),
    }),
  });
  const { detail, setDetail, clear: clearDetail, load: loadDetail } = detailRequest;
  // 選んだテーブルは URL の `?table=` が唯一の情報源（#2281）。先頭の行を自動で選ばない。
  const selection = useWorkspaceUrlSelection("table");
  const selectedTableName = selection.selectedId ?? "";

  const fetchDetail = async (name: string) => {
    setDetailTab("columns");
    await loadDetail(name);
  };

  // 選択（URL）が変わったら詳細を取り直し、外れたら詳細を消す。画面が表に戻ったとき（keep-alive）も取り直す。
  const workspaceActive = useWorkspaceActive();
  const lastSelectedRef = useRef("");
  useEffect(() => {
    if (!workspaceActive) return;
    const changed = lastSelectedRef.current !== selectedTableName;
    lastSelectedRef.current = selectedTableName;
    if (!selectedTableName) {
      clearDetail();
      return;
    }
    if (changed) setDetailTab("columns");
    void loadDetail(selectedTableName);
  }, [workspaceActive, selectedTableName, clearDetail, loadDetail, setDetailTab]);

  // DDL は重い GET_DDL を伴うため列タブでは取得せず、DDL タブ初回表示時に後追いで取得する。
  const handleDetailTabChange = (nextTab: DbObjectDetailTab) => {
    setDetailTab(nextTab);
    if (nextTab !== "ddl" || !detail || detail.ddl) return;
    void detailRequest.loadDdl(formatDbObjectName(detail));
  };

  const returnToList = () => {
    setActiveView("list");
    setDetailTab("columns");
  };

  // 行数バッジは既定で num_rows 統計(一覧と統一・高速)。正確な件数は明示操作で COUNT(*)。
  const handleExactCount = async (name: string) => {
    setLoading("count");
    try {
      const target = parseDbAdminObjectTarget(name);
      const params = new URLSearchParams({ include_ddl: "0", exact_count: "1" });
      if (target.owner) params.set("owner", target.owner);
      const full = await apiGet<DbAdminObjectDetail>(
        `/api/nl2sql/db-admin/tables/${encodeURIComponent(target.name)}?${params.toString()}`,
      );
      setDetail((current) =>
        current && formatDbObjectName(current) === target.qualifiedName
          ? { ...current, row_count: full.row_count }
          : current,
      );
    } catch (err) {
      setMessage(err instanceof Error ? err.message : t("tableMgmt.error.exactCount"));
    } finally {
      setLoading("");
    }
  };

  // 一覧を取り直し、失敗を message に出す（message は呼び出し側で空にしておく）。
  // state の更新は応答の callback の中だけで行う（effect からも呼ぶため）。
  // refetch は query の observer ごとに固定の関数なので、この関数も固定になる（終端の effect の発火の条件は変わらない）。
  const { refetch: refetchTableObjects } = tableObjectsQuery;
  const refetchObjects = useCallback((announce = false) =>
    refetchTableObjects().then((result) => {
      if (result.error) {
        setMessage(result.error instanceof Error ? result.error.message : t("tableMgmt.error.load"));
        return;
      }
      // 失敗の案内の「再試行」で取り直せたら、案内を消す（#2034）。
      setMessage("");
      if (announce) {
        toast.success(t("common.action.refreshed"));
      }
    }), [refetchTableObjects]);

  const refreshObjects = async (announce = false) => {
    setMessage("");
    await refetchObjects(announce);
  };

  // 変更の後の DB 構造の同期。一覧の操作（削除）はページの先頭、作成・取込は結果の直下に案内と進行を
  // 1 か所だけ出す。案内の中の「DB 構造を再取得」の進行は、そのボタンの直下に出す（#2031）。
  const schemaSync = useMutationSchemaSync({
    noticeOrigin: TABLE_SCHEMA_REFRESH_NOTICE,
    onDone: refetchObjects,
  });
  const taskSchemaSync = useMutationSchemaSync({
    noticeOrigin: TABLE_TASK_SCHEMA_REFRESH_NOTICE,
    onDone: refetchObjects,
  });

  const reloadAfterMutation = (result: {
    schema_refresh_job_id?: string;
    schema_refresh_required?: boolean;
    schema_refresh_reason_code?: string;
  }) => {
    if (activeView === "list") schemaSync.track(result, { scope: "page" });
    else taskSchemaSync.track(result);
    void refreshObjects();
  };

  const filteredTables = useMemo(() => {
    const q = tableSearch.trim().toLowerCase();
    const ownerPrefixKey = tableOwnerPrefix.trim().toUpperCase();
    return tableItems
      .filter((item) => {
        if (ownerPrefixKey && !item.owner.toUpperCase().startsWith(ownerPrefixKey)) return false;
        if (!q) return true;
        return (
          item.name.toLowerCase().includes(q) ||
          item.comment.toLowerCase().includes(q)
        );
      })
      .sort((left, right) => {
        return compareDbObjectSortValues(
          dbObjectSortValue(left, tableSort.key),
          dbObjectSortValue(right, tableSort.key),
          tableSort.direction
        );
      });
  }, [tableItems, tableOwnerPrefix, tableSearch, tableSort]);

  const toggleSort = (key: DbObjectSortKey) => {
    setTableSort((current) => ({
      key,
      direction: current.key === key && current.direction === "asc" ? "desc" : "asc",
    }));
  };

  const importReadSequence = useRef(0);
  useEffect(() => {
    return () => { importReadSequence.current += 1; };
  }, []);

  const pickImportFile = async (file: File | undefined) => {
    if (!file) return;
    const sequence = ++importReadSequence.current;
    setImportFilename(file.name);
    setImportConfirmation("");
    setImportBase64("");
    setImportResult(null);
    setImportColumnTypes({});
    setImportError(null);
    taskSchemaSync.reset();
    setImportStep("file");
    try {
      const content = await fileToBase64(file);
      if (sequence === importReadSequence.current) setImportBase64(content);
    } catch {
      if (sequence === importReadSequence.current) setImportError(t("dbAdmin.runner.fileErrorRead"));
    }
  };

  const clearImportFile = () => {
    importReadSequence.current += 1;
    setImportConfirmation("");
    setImportFilename("");
    setImportBase64("");
    setImportResult(null);
    setImportColumnTypes({});
    setImportError(null);
    taskSchemaSync.reset();
    setImportStep("file");
    setImportFileResetSignal((value) => value + 1);
  };

  const canClearImportWizard = Boolean(
    importTable ||
      importSheet ||
      importFilename ||
      importBase64 ||
      importConfirmation ||
      importResult ||
      importError ||
      taskSchemaSync.view
  );

  const clearImportWizard = () => {
    setImportTable("");
    setImportSheet("");
    setImportConfirmation("");
    clearImportFile();
  };

  const importTabular = async (preview = false) => {
    if (
      Boolean(loading) || !importTable.trim() || !importBase64 ||
      (!preview && (importConfirmation.trim() !== "ADMIN_EXECUTE" || !importResult ||
        !importResult.columns.every((column) => importColumnTypes[column.column_name] === column.data_type))) ||
      (!importFilename.toLowerCase().endsWith(".csv") && !importSheet.trim())
    ) return;
    setLoading(preview ? "import-preview" : "import-tabular");
    setMessage("");
    setImportError(null);
    taskSchemaSync.reset();
    try {
      const result = await apiPost<DbAdminImportTabularData>("/api/nl2sql/db-admin/import-tabular", {
        table_name: importTable,
        content_base64: importBase64,
        filename: importFilename || "upload.csv",
        sheet_name: importSheet,
        mode: "create",
        preview,
        column_types: importColumnTypes,
        confirmation: preview ? "" : importConfirmation,
        reason: "ui-table-management-import-tabular",
      });
      setImportResult(result);
      setImportColumnTypes(Object.fromEntries(result.columns.map((column) => [column.column_name, column.data_type])));
      setImportStep("execute");
      if (result.executed && !taskSchemaSync.track(result)) {
        void refreshObjects();
      }
    } catch (err) {
      setImportError(err instanceof Error ? err : new Error(t("dataTools.error.import")));
    } finally {
      setLoading("");
    }
  };

  const downloadColumnsXlsx = async (name: string) => {
    const target = parseDbAdminObjectTarget(name);
    const params = new URLSearchParams();
    if (target.owner) params.set("owner", target.owner);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    setLoading("table-export");
    setMessage("");
    try {
      const response = await apiFetch(`/api/nl2sql/db-admin/tables/${encodeURIComponent(target.name)}/export.xlsx${suffix}`, {
        headers: {
          Accept: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
      });
      if (!response.ok) {
        throw new Error(t("tableMgmt.error.export"));
      }
      downloadBlob(`${target.qualifiedName.toLowerCase().replace(".", "_")}_columns.xlsx`, await response.blob());
      toast.success(t("common.action.downloaded"));
    } catch (err) {
      setMessage(err instanceof Error ? err.message : t("tableMgmt.error.export"));
    } finally {
      setLoading("");
    }
  };

  const openDropDialog = (name: string) => {
    setDropTargetName(name);
    setDropConfirmation("");
    setDropError("");
  };

  const dropTable = async () => {
    if (!dropTargetName) return;
    const target = parseDbAdminObjectTarget(dropTargetName);
    setLoading("drop");
    setMessage("");
    setDropError("");
    try {
      const result = await apiPost<DbAdminExecuteData>("/api/nl2sql/db-admin/drop-table", {
        table_name: target.name,
        owner: target.owner,
        confirmation: dropConfirmation,
        reason: "ui-table-management-drop",
      });
      if (result.executed) {
        const dropped = dropTargetName;
        setDropTargetName("");
        setDropConfirmation("");
        setDropError("");
        toast.success(t("tableMgmt.drop.success", { name: dropped }));
        // 削除した対象を選んでいたら選択を外す（詳細の空の状態・1 ペインは一覧へ）。
        if (dropped === selectedTableName) selection.replace(null);
        await reloadAfterMutation(result);
        return;
      }
      setDropError(dbAdminExecuteFailureMessage(result, t("tableMgmt.error.drop")));
    } catch (err) {
      setDropError(err instanceof Error ? err.message : t("tableMgmt.error.drop"));
    } finally {
      setLoading("");
    }
  };

  const taskContent =
    activeView === "create" ? (
      <StatementRunnerCard
        policy="table_ddl"
        title={t("tableMgmt.create.title")}
        description={t("tableMgmt.create.note")}
        placeholder={t("tableMgmt.create.placeholder")}
        progress={({ hasSql }) => (
          <DbObjectStepIndicator
            steps={[t("tableMgmt.create.stepSql"), t("tableMgmt.create.stepExecute")]}
            activeIndex={hasSql ? 1 : 0}
            ariaLabel={t("tableMgmt.create.steps")}
            dataTestId="table-create-steps"
          />
        )}
        confirmationTitle={t("tableMgmt.create.executeTitle")}
        executeOnly
        framed={false}
        footerProcessing={
          <SchemaSyncNotice
            sync={taskSchemaSync}
            testId="table-create-schema-sync-notice"
            processingTestId="table-create-schema-refresh-processing"
            fallbackProgress={!schemaSync.ownsProgress}
          />
        }
        onExecuted={reloadAfterMutation}
      />
    ) : activeView === "import" ? (
      <ImportWizard
        table={importTable}
        sheet={importSheet}
        filename={importFilename}
        fileResetSignal={importFileResetSignal}
        fileReady={Boolean(importBase64)}
        result={importResult}
        step={importStep}
        confirmation={importConfirmation}
        loading={loading === "import-tabular" || loading === "import-preview"}
        previewing={loading === "import-preview"}
        canClear={canClearImportWizard}
        error={importError}
        schemaSync={taskSchemaSync}
        pageSchemaSyncOwnsProgress={schemaSync.ownsProgress}
        onTableChange={(value) => {
          setImportTable(value);
          setImportResult(null);
          setImportColumnTypes({});
          setImportError(null);
          taskSchemaSync.reset();
          setImportStep("file");
        }}
        onSheetChange={(value) => {
          setImportSheet(value);
          setImportResult(null);
          setImportColumnTypes({});
          setImportError(null);
          taskSchemaSync.reset();
          setImportStep("file");
        }}
        onFilePick={(file) => void pickImportFile(file)}
        onFileClear={clearImportFile}
        onClear={clearImportWizard}
        onExecute={() => void importTabular()}
        onPreview={() => void importTabular(true)}
        columnTypes={importColumnTypes}
        onColumnTypeChange={(column, value) => setImportColumnTypes((current) => ({ ...current, [column]: value }))}
        onConfirmationChange={(value) => {
          setImportConfirmation(value);
          setImportError(null);
          if (value.trim()) setImportStep("execute");
        }}
        onReturnToList={returnToList}
      />
    ) : null;

  return (
    <>
      <PageHeader wide
        title={t("nav.tableManagement")}
        subtitle={t("tableMgmt.subtitle")}
        meta={
          firstTablePage?.refreshed_at
            ? t("common.schemaRefreshedAt", { date: formatDateTime(firstTablePage.refreshed_at) })
            : undefined
        }
        status={<SchemaRefreshHeaderStatus testId="table-schema-refresh-status" />}
        // 作業（作成・取込）の画面と、1 ペイン（xl 未満）の詳細の「一覧へ戻る」はページの左上（#618・#2281）。
        back={
          activeView !== "list"
            ? { label: t("tableMgmt.action.backToList"), onClick: returnToList, testId: "table-management-back" }
            : !selection.split && selectedTableName
              ? { label: t("tableMgmt.action.backToList"), onClick: selection.clear, testId: "table-management-detail-back" }
              : undefined
        }
        actionsLabel={t("tableMgmt.tabs.label")}
        actionsTestId="table-management-actions"
        actions={
          activeView === "list"
            ? [
                {
                  id: "create-table",
                  kind: "primary",
                  label: t("tableMgmt.create.title"),
                  icon: Code2,
                  onClick: () => setActiveView("create"),
                },
                {
                  id: "import-table",
                  kind: "secondary",
                  label: t("dataTools.dbAdmin.importTitle"),
                  icon: Upload,
                  onClick: () => setActiveView("import"),
                },
                {
                  id: "refresh-table-list",
                  kind: "utility",
                  label: t("common.action.refresh"),
                  icon: RefreshCw,
                  // 初回の読込は一覧の Skeleton がスピナーを出す（このボタンは狭い画面では「その他の操作」の中で
                  // 見えない）。ボタンは押した再読込の間だけ回す（同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
                  // 検索・絞り込み・他の操作の後の取り直しでは回さず、一覧の処理中の表示がスピナーを出す（#819）。
                  loading: manualRefresh.pending,
                  disabled: !tableObjectsQuery.data && tableObjectsQuery.isFetching,
                  onClick: () => void manualRefresh.track(() => refreshObjects(true)),
                },
                {
                  id: "refresh-table-schema",
                  kind: "utility",
                  label: t("common.action.schemaRefresh"),
                  icon: RefreshCw,
                  // 押したときの送信の間だけ回し、job の間は無効にするだけ（スピナーは進行の表示。#821）。
                  loading: headerSchemaRefreshStarting,
                  disabled: schemaRefreshing && !headerSchemaRefreshStarting,
                  onClick: () => void schemaSync.refreshFrom(TABLE_SCHEMA_REFRESH_HEADER),
                },
              ]
            : []
        }
      />
      <PageBody wide className="grid gap-4">
        <RetryPageNotice
          notice={
            message
              ? { tone: "danger", message: `${message} ${t("tableMgmt.error.retryHint")}` }
              : null
          }
          retrying={noticeRetry.pending}
          disabled={manualRefresh.pending}
          onRetry={() => void noticeRetry.track(() => refetchObjects())}
          operationKey="table-list-retry"
          testId="table-management-notice-retry"
        />
        <SchemaSyncNotice
          sync={schemaSync}
          scope="page"
          testId="table-management-schema-sync-notice"
          processingTestId="table-management-schema-sync-processing"
        />
        {activeView === "list" ? (
          <>
            <DbObjectManagementPanelShell
              id="table-management-panel-list"
              role="region"
              idPrefix="table-management"
              ariaLabel={t("tableMgmt.workspace.label")}
              framed={false}
              processing={
                schemaRefreshing && !schemaSync.ownsProgress ? (
                  <SchemaRefreshProcessing testId="table-management-workspace-processing" />
                ) : tableObjectsQuery.data && !noticeRetry.pending &&
                  tableObjectsQuery.isFetching && !tableObjectsQuery.isFetchingNextPage ? (
                  <ProcessingIndicator
                    active
                    label={t("tableMgmt.workspace.refreshing")}
                    operationKey="table-list-refresh"
                    placement="workspace"
                    className="rounded-md border border-border bg-surface-sunken px-3 py-2"
                    testId="table-management-workspace-processing"
                    activityIcon={manualRefresh.pending ? "none" : "spinner"}
                  />
                ) : undefined
              }
            >
            <DbObjectListDetail
              showComments
              idPrefix="table-management"
              headingId="table-grid-heading"
              icon={Table2}
              items={filteredTables}
              selectedName={selectedTableName}
              loading={tableObjectsQuery.isPending && !tableObjectsQuery.data}
              error={
                tableObjectsQuery.error && !tableObjectsQuery.data
                  ? objectListErrorMessage(tableObjectsQuery.error, "tableMgmt.error.load")
                  : ""
              }
              search={tableSearch}
              ownerPrefix={tableOwnerPrefix}
              sort={tableSort}
              totalCount={totalTableCount}
              unfilteredTotalCount={unfilteredTableCount}
              hasNextPage={Boolean(tableObjectsQuery.hasNextPage)}
              loadingNextPage={tableObjectsQuery.isFetchingNextPage}
              loadMoreError={
                tableObjectsQuery.isFetchNextPageError && tableObjectsQuery.error
                  ? listLoadMoreErrorMessage(tableObjectsQuery.error, "tableMgmt.error.load")
                  : ""
              }
              labels={{
                title: t("tableMgmt.list.title"),
                hint: t("tableMgmt.grid.hint"),
                loading: t("tableMgmt.list.loading"),
                emptyTitle: t("tableMgmt.list.emptyTitle"),
                emptyHint: t("tableMgmt.list.emptyHint"),
                noResultsTitle: t("tableMgmt.list.noResultsTitle"),
                noResultsHint: t("tableMgmt.list.noResultsHint"),
                objectName: t("tableMgmt.grid.tableName"),
                rows: t("tableMgmt.grid.rows"),
                owner: t("tableMgmt.grid.owner"),
                showObject: (name) => t("tableMgmt.grid.showTable", { name }),
              }}
              onSearchChange={setTableSearch}
              onOwnerPrefixChange={setTableOwnerPrefix}
              onSortChange={toggleSort}
              // 選んでいる行をもう一度押すと詳細を取り直す（取消・失敗の後の読み直し）。
              onSelect={(name) => (name === selectedTableName ? void fetchDetail(name) : selection.select(name))}
              onLoadMore={() => void tableObjectsQuery.fetchNextPage()}
              onRetryLoadMore={() => void tableObjectsQuery.fetchNextPage()}
              onRetry={() => void refreshObjects()}
              detailLabel={t("tableMgmt.detail.label")}
              detail={
                <DbObjectDetailPanel
                  idPrefix="table-management"
                  operationKey={selectedTableName}
                  headingId="table-detail-heading"
                  detail={detail}
                  loading={detailRequest.loading || (tableObjectsQuery.isPending && !tableObjectsQuery.data)}
                  // 一覧の初回の読込では、スピナーは一覧の Skeleton の 1 つだけにする（#416）。
                  loadingActivityIcon={tableObjectsQuery.isPending && !tableObjectsQuery.data ? "none" : undefined}
                  ddlLoading={detailRequest.ddlLoading}
                  error={detailRequest.error}
                  ddlError={detailRequest.ddlError}
                  exporting={loading === "table-export"}
                  countingRows={loading === "count"}
                  tab={detailTab}
                  labels={{
                    actions: t("tableMgmt.grid.actions"),
                    loading: t("tableMgmt.detail.loading"),
                    ddlLoading: t("tableMgmt.detail.ddlLoading"),
                    tabsLabel: t("tableMgmt.detailTabs.label"),
                    columns: t("tableMgmt.detailTabs.columns"),
                    ddl: t("tableMgmt.detailTabs.ddl"),
                    export: t("tableMgmt.export"),
                    exportAria: t("tableMgmt.exportColumns"),
                    exactCount: t("tableMgmt.exactCount"),
                    exactCountAria: t("tableMgmt.exactCountAria"),
                    drop: t("tableMgmt.grid.drop"),
                  }}
                  onTabChange={handleDetailTabChange}
                  onRetry={() => void fetchDetail(selectedTableName)}
                  onRetryDdl={() => {
                    if (detail) void detailRequest.loadDdl(formatDbObjectName(detail));
                  }}
                  onCancel={detailRequest.cancel}
                  onExport={(name) => void downloadColumnsXlsx(name)}
                  onExactCount={(name) => void handleExactCount(name)}
                  onDrop={openDropDialog}
                />
              }
            />
            </DbObjectManagementPanelShell>
          </>
        ) : (
          <>
            <DbObjectManagementPanelShell
              id={`table-management-panel-${activeView}`}
              role="region"
              idPrefix="table-management"
              ariaLabel={t("tableMgmt.toolbar.taskPanel")}
            >
              {taskContent}
            </DbObjectManagementPanelShell>
          </>
        )}
      </PageBody>

      {dropTargetName && (
        <DropDbObjectDialog
          objectName={dropTargetName}
          confirmation={dropConfirmation}
          loading={loading === "drop"}
          error={dropError}
          labels={{
            title: t("tableMgmt.dropDialog.title"),
            subtitle: t("tableMgmt.dropDialog.subtitle"),
            close: t("tableMgmt.dropDialog.close"),
            target: t("tableMgmt.dropDialog.target"),
            executeTitle: t("tableMgmt.dropDialog.executeTitle"),
            executeHint: t("tableMgmt.dropDialog.executeHint"),
            cancel: t("tableMgmt.dropDialog.cancel"),
            run: t("tableMgmt.drop.run"),
          }}
          onConfirmationChange={(value) => {
            setDropConfirmation(value);
            if (dropError) setDropError("");
          }}
          onExecute={() => void dropTable()}
          onClose={() => {
            setDropTargetName("");
            setDropError("");
          }}
        />
      )}
    </>
  );
}
