import { useWorkspaceState, useResetExecutionConsent } from "@/components/WorkspaceState";
import {
  CopyButton,
  Button,
  Banner,
  Card,
  ProcessFailureNotice,
  type ProcessFailure,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  Disclosure,
  type DataTableColumn,
  StatusBadge,
  Tabs,
  ClearActionButton,
  ActionResultRegion,
  ContentActionBar,
  INFORMATION_TABLE_ROW_CLASS,
  ExecutionConfirmationField,
  FormGrid,
  TextareaField,
} from "@production-ready/ui";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useValuesChanged } from "@/lib/render-sync";
import {
  Code2,
  Play,
  X,
  type LucideIcon,
} from "lucide-react";


import {
  ExecutionActivityPanel,
  type ExecutionActivityStatus,
} from "@/components/ExecutionActivityPanel";
import { FileDropzone } from "@/components/ui/file-dropzone";
import { ApiError, apiPost, type ApiErrorDetails } from "@/lib/api";
import { downloadBlob } from "@/lib/download";
import { t } from "@/lib/i18n";
import type { OperationTimestamp } from "@/lib/operationTiming";
import type {
  DbAdminExecuteData,
  DbAdminObjectDetail,
  DbAdminStatementResult,
  DbAdminStatementPolicy,
} from "../types";
import { QueryResultTable } from "./QueryResultTable";
import { dbAdminErrorRecovery } from "../dbAdminErrorRecovery";

/** テキストファイルを SQL としてダウンロードする。 */
export function downloadText(filename: string, text: string) {
  const blob = new Blob([text], { type: "text/sql;charset=utf-8" });
  downloadBlob(filename, blob);
}

export { downloadBlob } from "@/lib/download";

/** File を base64 文字列に変換する(tabular import 用)。 */
export async function fileToBase64(file: File) {
  const buffer = await file.arrayBuffer();
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index]);
  }
  return btoa(binary);
}

/** UTF-8 で読めない場合は Shift_JIS を試すテキスト読込(SQL/CSV ファイル用)。 */
export async function readTextFileSmart(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const utf8 = new TextDecoder("utf-8", { fatal: false }).decode(buffer);
  if (!utf8.includes("�")) return utf8;
  try {
    return new TextDecoder("shift_jis").decode(buffer);
  } catch {
    return utf8;
  }
}

export function WorkSection({
  title,
  description,
  tone = "neutral",
  open,
  dataTestId,
  onOpenChange,
  children,
}: {
  title: string;
  description: string;
  tone?: "neutral" | "danger";
  open?: boolean;
  dataTestId?: string;
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
}) {
  return (
    <Disclosure
      tone={tone}
      summary={title}
      description={description}
      open={open}
      onOpenChange={onOpenChange}
      data-testid={dataTestId}
      summaryClassName="px-4 py-3"
      contentClassName="rounded-b-md bg-surface"
    >
      {children}
    </Disclosure>
  );
}

export function ManagementPanelShell({
  id,
  labelledBy,
  ariaLabel,
  className = "",
  children,
}: {
  id: string;
  labelledBy: string;
  ariaLabel?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section
      id={id}
      role="tabpanel"
      aria-labelledby={labelledBy}
      aria-label={ariaLabel}
      className={`grid gap-4 rounded-md border border-border bg-surface p-4 shadow-sm ${className}`}
      data-testid="management-panel-shell"
    >
      {children}
    </section>
  );
}

export function ManagementPanelHeader({
  title,
  description,
  icon: Icon,
  headingId,
  action,
}: {
  title: string;
  description?: string;
  icon: LucideIcon;
  headingId?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        {/* 見出しが折り返しても 1 行目の中心にそろえる（#2371）。 */}
        <h2 id={headingId} className="flex items-start gap-2 text-base font-semibold text-fg">
          <span className="pr-icon-line">
            <Icon size={20} aria-hidden="true" />
          </span>
          <span className="min-w-0">{title}</span>
        </h2>
        {description && <p className="mt-1 text-sm text-fg-muted">{description}</p>}
      </div>
      {action && <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:justify-end">{action}</div>}
    </div>
  );
}

export function ManagementTabs<TView extends string>({
  activeView,
  tabs,
  idPrefix,
  ariaLabel,
  onViewChange,
}: {
  activeView: TView;
  tabs: Array<{
    id: TView;
    label: string;
    icon: LucideIcon;
    ariaLabel?: string;
    /** タブ横の短いバッジ（版数・状態など）。 */
    meta?: string;
    metaTestId?: string;
  }>;
  idPrefix: string;
  ariaLabel: string;
  onViewChange: (view: TView) => void;
}) {
  // 共有 Tabs（WAI-ARIA Tabs、下線スタイル）に NL2SQL の view 型とバッジを渡す adapter。
  return (
    <Tabs
      idPrefix={idPrefix}
      ariaLabel={ariaLabel}
      value={activeView}
      onChange={(id) => onViewChange(id as TView)}
      items={tabs.map((tab) => ({
        id: tab.id,
        label: tab.label,
        icon: tab.icon,
        ariaLabel: tab.ariaLabel,
        badge: tab.meta,
        badgeTestId: tab.metaTestId,
      }))}
    />
  );
}

type DbObjectColumn = DbAdminObjectDetail["columns"][number];

/**
 * オブジェクト詳細の列一覧（テーブル管理・ビュー管理・データ管理の詳細で共通）。
 * スクロール領域は Tab で到達でき、キーボードで縦横にスクロールできる。
 */
export function DbObjectColumnsTable({
  columns,
  showComment = false,
  sampleOf,
}: {
  columns: DbObjectColumn[];
  showComment?: boolean;
  sampleOf: (column: DbObjectColumn) => string;
}) {
  const tableColumns: Array<DataTableColumn<DbObjectColumn>> = [
    {
      key: "column_name",
      header: t("dbAdmin.col.physical"),
      rowHeader: true,
      headerClassName: "w-[18%]",
      className: "break-all font-mono",
      render: (column) => column.column_name,
    },
    {
      key: "logical_name",
      header: t("dbAdmin.col.logical"),
      headerClassName: showComment ? "w-[18%]" : "w-[12%]",
      className: "break-words",
      render: (column) => (column.logical_name ?? "").trim() || "-",
    },
    ...(showComment
      ? [
          {
            key: "comment",
            header: t("dbAdmin.col.comment"),
            headerClassName: "w-[20%]",
            className: "break-words text-fg-muted",
            render: (column: DbObjectColumn) => (column.comment ?? "").trim() || "-",
          },
        ]
      : []),
    {
      key: "data_type",
      header: t("dbAdmin.col.type"),
      headerClassName: showComment ? "w-[14%]" : "w-[16%]",
      className: "break-words",
      render: (column) => column.data_type,
    },
    {
      key: "nullable",
      header: t("dbAdmin.col.nullable"),
      headerClassName: showComment ? "w-[10%]" : "w-[12%]",
      render: (column) => (column.nullable ? "YES" : "NO"),
    },
    {
      key: "sample",
      header: t("dbAdmin.col.sample"),
      className: "break-words font-sans text-fg-muted",
      render: (column) => sampleOf(column) || "-",
    },
  ];
  return (
    <DataTable
      columns={tableColumns}
      rows={columns}
      getRowKey={(column) => column.column_name}
      rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
      // 一覧 + 詳細（#2281）の広い詳細の列（1280px で約 49rem）に横スクロールなしで収まる最小の幅。
      // それより狭い置き場所（xl 未満の 1 ペインの 375px など）は表の中で横にスクロールする。
      tableClassName={showComment ? "w-full min-w-[44rem] table-fixed" : "w-full min-w-[36rem] table-fixed"}
      scrollAriaLabel={t("dbAdmin.detail.columnsScrollLabel")}
      scrollTestId="db-admin-detail-columns"
      // 詳細はページと一緒にスクロールする（詳細の中に縦のスクロールを入れ子にしない。#2281）。
    />
  );
}

/** 実行環境（API の runtime）の表示名。内部値（oracle / deterministic）をそのまま出さない。 */
export function runtimeLabel(runtime: string) {
  if (runtime === "oracle") return "Oracle";
  if (runtime === "deterministic") return t("dbAdmin.result.runtime.deterministic");
  return runtime;
}

type ResultStatusVariant = "success" | "neutral" | "danger" | "info" | "warning";

function statusVariant(status: string): ResultStatusVariant {
  if (["success", "executed", "applied_to_local_state", "submitted"].includes(status)) return "success";
  if (["error", "blocked"].includes(status)) return "danger";
  if (["confirmation_required", "requires_oracle"].includes(status)) return "info";
  return "neutral";
}

function statusLabel(status: string) {
  const key = `dbAdmin.result.status.${status}`;
  const label = t(key);
  return label === key ? status : label;
}

function resultSummary(result: DbAdminExecuteData): { variant: ResultStatusVariant; label: string } {
  const statuses = result.statements.map((statement) => statement.status);
  if (result.executed && statuses.includes("error")) {
    return { variant: "warning", label: t("dbAdmin.result.summary.partial") };
  }
  if (result.executed) return { variant: "success", label: t("dbAdmin.result.summary.executed") };
  if (statuses.includes("error")) return { variant: "danger", label: t("dbAdmin.result.summary.error") };
  if (statuses.includes("blocked")) return { variant: "danger", label: t("dbAdmin.result.summary.blocked") };
  if (statuses.includes("confirmation_required")) return { variant: "info", label: t("dbAdmin.result.summary.confirmation") };
  if (statuses.includes("requires_oracle")) return { variant: "info", label: t("dbAdmin.result.summary.requiresOracle") };
  return { variant: "neutral", label: t("dbAdmin.result.summary.notExecuted") };
}

export function dbAdminExecutionActivityStatus(result: DbAdminExecuteData): ExecutionActivityStatus {
  const summary = resultSummary(result);
  return summary.variant === "danger" || summary.variant === "warning" ? "error" : "success";
}

function oracleErrorGuidance(code: string | null) {
  if (code === "ORA-00922") {
    return {
      cause: t("dbAdmin.result.error.ora00922.cause"),
      actions: [
        t("dbAdmin.result.error.ora00922.action.split"),
        t("dbAdmin.result.error.ora00922.action.syntax"),
        t("dbAdmin.result.error.action.refresh"),
      ],
    };
  }
  if (code === "ORA-00955") {
    return {
      cause: t("dbAdmin.result.error.ora00955.cause"),
      actions: [
        t("dbAdmin.result.error.ora00955.action.delete"),
        t("dbAdmin.result.error.ora00955.action.refresh"),
      ],
    };
  }
  if (code === "ORA-00942") {
    return {
      cause: t("dbAdmin.result.error.ora00942.cause"),
      actions: [
        t("dbAdmin.result.error.ora00942.action.target"),
        t("dbAdmin.result.error.action.refresh"),
      ],
    };
  }
  if (code === "ORA-00904") {
    return {
      cause: t("dbAdmin.result.error.ora00904.cause"),
      actions: [
        t("dbAdmin.result.error.ora00904.action.column"),
        t("dbAdmin.result.error.action.refresh"),
      ],
    };
  }
  if (code === "ORA-01031") {
    return {
      cause: t("dbAdmin.result.error.ora01031.cause"),
      actions: [t("dbAdmin.result.error.ora01031.action.privilege")],
    };
  }
  if (["ORA-01722", "ORA-01843", "ORA-01861", "ORA-12899"].includes(code ?? "")) {
    return {
      cause: t("dbAdmin.result.error.dataType.cause"),
      actions: [t("dbAdmin.result.error.dataType.action.check")],
    };
  }
  if (["ORA-00001", "ORA-01400", "ORA-02291", "ORA-02292"].includes(code ?? "")) {
    return {
      cause: t("dbAdmin.result.error.constraint.cause"),
      actions: [t("dbAdmin.result.error.constraint.action.check")],
    };
  }
  if (code === "ORA-00054") {
    return {
      cause: t("dbAdmin.result.error.ora00054.cause"),
      actions: [t("dbAdmin.result.error.ora00054.action.retry")],
    };
  }
  return {
    cause: t("dbAdmin.result.error.generic.cause"),
    generic: true,
    actions: [
      t("dbAdmin.result.error.generic.action.sql"),
      t("dbAdmin.result.error.action.refresh"),
    ],
  };
}

type ErrorGuidanceContext = "admin_sql" | "db_admin_chat";

function parseDbAdminError(message: string, details?: ApiErrorDetails, errorCode?: string, context: ErrorGuidanceContext = "admin_sql") {
  const helpUrl = message.match(/https?:\/\/\S+/)?.[0] ?? "";
  const summary = message.replace(/\s*Help:\s*https?:\/\/\S+/i, "").trim();
  const code = errorCode || summary.match(/\bORA-\d{5}\b/)?.[0] || null;
  const guidance = oracleErrorGuidance(code);
  const recovery = dbAdminErrorRecovery(code);
  return {
    code,
    summary: details?.summary || summary || message,
    helpUrl,
    cause: details?.cause || recovery?.cause || guidance.cause,
    // コードから具体的な原因が引けない（汎用の文）か。失敗のカードでは汎用の原因を本文に重ねない（#2342）。
    genericCause: !details?.cause && !recovery?.cause && "generic" in guidance,
    actions: context === "db_admin_chat"
      ? [t("dbAdminChat.analyze.nextAction")]
      : details?.actions?.length ? details.actions : recovery?.actions ?? guidance.actions,
    examples: recovery?.examples ?? [],
    rawMessage: details?.raw_message || message,
  };
}

export function DbAdminErrorNotice({
  error: sourceError,
  errorCode,
  onReturnToList,
}: {
  error: unknown;
  errorCode?: string;
  onReturnToList?: () => void;
}) {
  const message = sourceError instanceof Error ? sourceError.message : String(sourceError || "");
  const details = sourceError instanceof ApiError ? sourceError.details : undefined;
  const error = parseDbAdminError(
    message,
    details,
    errorCode || (sourceError instanceof ApiError ? sourceError.errorCode : undefined)
  );
  if (!message) return null;
  const requestId = sourceError instanceof ApiError ? sourceError.requestId : undefined;
  const hasDetail = Boolean(error.helpUrl || error.code || requestId);
  return (
    <Banner
      severity="danger"
      title={t("dbAdmin.result.error.summary")}
      className="mt-3"
      action={onReturnToList && error.code === "ORA-00955" ? (
        <Button type="button" variant="secondary" size="sm" onClick={onReturnToList}>
          {t("common.backToList")}
        </Button>
      ) : undefined}
    >
      <div className="grid gap-3">
        <div className="grid gap-1">
          <p className="break-words text-sm font-semibold text-fg">{error.summary}</p>
        </div>
        <div className="grid gap-1">
          <p className="text-xs font-semibold text-danger-fg">{t("dbAdmin.result.error.cause")}</p>
          <p className="text-sm leading-6 text-fg/90">{error.cause}</p>
        </div>
        <div className="grid gap-1">
          <p className="text-xs font-semibold text-danger-fg">{t("dbAdmin.result.error.nextAction")}</p>
          <ul className="grid gap-1 text-sm leading-6 text-fg/90">
            {error.actions.map((action) => (
              <li key={action} className="flex gap-2">
                <span aria-hidden="true">-</span>
                <span>{action}</span>
              </li>
            ))}
          </ul>
        </div>
        {error.examples.length > 0 ? <SqlRecoveryExamples examples={error.examples} /> : null}
        {hasDetail ? (
          <Disclosure summary={t("dbAdmin.result.error.detail")} contentClassName="grid gap-2">
              {error.code ? <StatusBadge variant="danger" label={error.code} /> : null}
              {requestId ? (
                <p className="break-all text-xs text-fg-muted">
                  {t("common.requestId")}: <code>{requestId}</code>
                </p>
              ) : null}
              {error.helpUrl ? (
                <a className="text-sm font-semibold text-danger-fg underline" href={error.helpUrl} target="_blank" rel="noreferrer">
                  {t("dbAdmin.result.error.help")}
                </a>
              ) : null}
            </Disclosure>
        ) : null}
      </div>
    </Banner>
  );
}

function SqlRecoveryExamples({ examples }: { examples: { label: string; sql: string }[] }) {
  return (
    <section className="grid min-w-0 gap-3" aria-label={t("dbAdmin.result.error.examples")}>
      <p className="text-xs font-semibold text-danger-fg">{t("dbAdmin.result.error.examples")}</p>
      <p className="text-sm leading-6 text-fg/90">{t("dbAdmin.result.error.examples.hint")}</p>
      {examples.map((example) => (
        <div key={example.label} className="grid min-w-0 gap-2 rounded-md border border-border bg-surface p-3">
          <ContentActionBar ariaLabel={example.label} title={example.label}>
            <CopyButton
              size="sm"
              text={example.sql}
              label={t("dbAdmin.result.error.example.copy")}
              aria-label={t("dbAdmin.result.error.example.copyAria", { name: example.label })}
              copiedMessage={t("common.action.copied")}
              failedMessage={t("common.action.copyFailed")}
            />
          </ContentActionBar>
          <pre className="min-w-0 whitespace-pre-wrap break-all text-xs leading-6 text-fg"><code>{example.sql}</code></pre>
        </div>
      ))}
    </section>
  );
}

/**
 * 文の実行の失敗（実行した後に DB が返したエラー）。結果の位置に、3 製品共通の失敗のカードで 1 つだけ出す（#2342）:
 * 1 文目は「この文の実行に失敗しました。」、本文は DB の文（1 回だけ）とコードから引ける具体的な原因、次の対応、
 * 「詳細」はエラーコード・Oracle のエラーヘルプ。以前は警告の帯・エラー概要・詳細ログの 3 か所に同じ文を出していた。
 */
export function dbAdminStatementFailure(
  statement: Pick<DbAdminStatementResult, "error_message" | "error_code" | "status">,
  context: ErrorGuidanceContext = "admin_sql"
): ProcessFailure {
  const error = parseDbAdminError(statement.error_message ?? "", undefined, statement.error_code ?? undefined, context);
  return {
    // 実行の前に止めた文（ポリシーの確認）は「実行できません」、実行した後の失敗は「実行に失敗しました」。
    summary:
      statement.status === "blocked"
        ? t("dbAdmin.result.error.statementBlocked")
        : t("dbAdmin.result.error.statementFailed"),
    cause: error.summary,
    explanation: error.genericCause ? null : error.cause,
    nextAction: error.actions.join(""),
    errorCode: error.code,
    extraDetails: error.helpUrl ? [{ label: t("dbAdmin.result.error.help"), value: error.helpUrl }] : [],
  };
}

function DbAdminStatementError({ statement, context }: { statement: DbAdminStatementResult; context: ErrorGuidanceContext }) {
  const error = parseDbAdminError(statement.error_message ?? "", undefined, statement.error_code ?? undefined, context);
  return (
    <div className="mt-2 grid min-w-0 gap-3">
      <ProcessFailureNotice
        failure={dbAdminStatementFailure(statement, context)}
        labels={{ details: t("failure.details"), errorCode: t("failure.errorCode") }}
        testId="db-admin-statement-failure"
      />
      {error.examples.length > 0 ? <SqlRecoveryExamples examples={error.examples} /> : null}
    </div>
  );
}

/** 文のエラーと同じ文の警告（DB のエラーの文を警告にも入れた結果）は、失敗のカードに重ねて出さない（#2342）。 */
function duplicatesStatementError(warning: string, statements: readonly DbAdminStatementResult[]): boolean {
  return statements.some((statement) => {
    const message = statement.error_message?.trim();
    if (!message) return false;
    const code = statement.error_code || message.match(/\bORA-\d{5}\b/)?.[0];
    return warning.includes(message) || message.includes(warning.trim()) || Boolean(code && warning.includes(code));
  });
}

export function DbAdminExecutionResult({
  result,
  rowLimit,
  hideStatementSql = false,
  context = "admin_sql",
}: {
  result: DbAdminExecuteData;
  rowLimit?: number | null;
  /** SQL をすぐ上に出している（DB 管理チャットの文ごとの結果。#2333）ときは、文の SQL を繰り返さない。 */
  hideStatementSql?: boolean;
  context?: ErrorGuidanceContext;
}) {
  const summary = resultSummary(result);
  const showStatementDetails = !result.select_result;
  return (
    <section className="grid gap-2 rounded-md border border-border bg-surface-sunken p-3 text-sm">
      <div className="flex flex-wrap gap-2">
        <StatusBadge variant={summary.variant} label={summary.label} />
        <StatusBadge icon={false} variant="neutral" label={runtimeLabel(result.runtime)} />
        {result.committed && <StatusBadge variant="success" label={t("dbAdmin.result.summary.committed")} />}
        {result.rolled_back && <StatusBadge variant="danger" label={t("dbAdmin.result.summary.rolledBack")} />}
      </div>
      {result.warnings.filter((warning) => !duplicatesStatementError(warning, result.statements)).map((warning, index) => (
        <Banner key={`${index}-${warning}`} severity="warning">
          {warning}
        </Banner>
      ))}
      {result.select_result && (
        <QueryResultTable results={result.select_result} rowLimit={rowLimit} csvFilePrefix="nl2sql-admin-sql-result" />
      )}
      {showStatementDetails ? (
        <div className="grid max-h-[32rem] gap-2 overflow-y-auto">
          {result.statements.map((statement) => (
            <div key={`${statement.index}-${statement.sql}`} className="rounded-md bg-surface p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap gap-2">
                  <StatusBadge icon={false} variant="neutral" label={statement.statement_type} />
                  <StatusBadge variant={statusVariant(statement.status)} label={statusLabel(statement.status)} />
                  {statement.statement_type !== "SELECT" &&
                    statement.row_count !== null &&
                    statement.row_count !== undefined && (
                      <StatusBadge
                        icon={false}
                        variant="neutral"
                        label={t("dbAdmin.result.affectedRows", { count: statement.row_count })}
                      />
                    )}
                </div>
              </div>
              {hideStatementSql ? null : (
                <code className="mt-2 block break-words text-xs text-fg">{statement.sql}</code>
              )}
              {statement.error_message && <DbAdminStatementError statement={statement} context={context} />}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

/** .sql/.txt ファイルを読み込んで textarea へ流し込むボタン。 */
/** SQL ファイルの読込上限。textarea へそのまま流し込むため、巨大ファイルは受け付けない。 */
export const MAX_SQL_FILE_BYTES = 2 * 1024 * 1024;

export function SqlFileInput({
  onLoad,
  resetSignal = 0,
  disabled = false,
}: {
  onLoad: (text: string) => void;
  resetSignal?: number;
  disabled?: boolean;
}) {
  const [filename, setFilename] = useState("");
  const readSequence = useRef(0);
  useEffect(() => {
    return () => { readSequence.current += 1; };
  }, []);
  useEffect(() => { if (disabled) readSequence.current += 1; }, [disabled]);
  const [errorText, setErrorText] = useState("");

  // reset のレンダーで表示を消し（effect で setState しない）、読み込み中の結果は effect で捨てる。
  const resetRequested = useValuesChanged([resetSignal]);
  if (resetRequested) {
    setFilename("");
    setErrorText("");
  }
  useEffect(() => {
    readSequence.current += 1;
  }, [resetSignal]);

  return (
    <FileDropzone
      label={t("dbAdmin.runner.filePick")}
      accept=".sql,.txt"
      selectedText={filename}
      formatLabel={t("dbAdmin.runner.fileFormats")}
      actionText={t("dbAdmin.runner.fileDropAction")}
      replaceText={t("dbAdmin.runner.fileReplaceShort")}
      icon="upload"
      disabled={disabled}
      dataTestId="sql-file-input"
      activeText={t("dbAdmin.runner.fileDropActive")}
      errorText={errorText}
      onReject={(reason) => {
        readSequence.current += 1;
        setErrorText(
          reason === "multiple-files"
            ? t("dbAdmin.runner.fileErrorMultiple")
            : t("dbAdmin.runner.fileErrorUnsupported")
        );
      }}
      onFiles={async ([file]) => {
        const sequence = ++readSequence.current;
        setErrorText("");
        if (file.size > MAX_SQL_FILE_BYTES) {
          setErrorText(
            t("dbAdmin.runner.fileErrorTooLarge", { size: MAX_SQL_FILE_BYTES / (1024 * 1024) })
          );
          return;
        }
        try {
          const text = await readTextFileSmart(file);
          if (sequence !== readSequence.current) return;
          onLoad(text);
          setFilename(file.name);
        } catch {
          if (sequence !== readSequence.current) return;
          setErrorText(t("dbAdmin.runner.fileErrorRead"));
        }
      }}
    />
  );
}

interface DbAdminExecutionRunState {
  operationKey: string | number;
  status: ExecutionActivityStatus;
  startedAt: OperationTimestamp;
  finishedAt?: OperationTimestamp;
  elapsedMs?: number | null;
}

function executionActivityLabel(status: ExecutionActivityStatus) {
  if (status === "success") return t("executionActivity.execute.success");
  if (status === "error") return t("executionActivity.execute.error");
  return t("nl2sql.processing.executeSql");
}

/** 文種 whitelist 付き SQL 実行カード(テーブル/ビュー作成・データ SQL 共用)。 */
export function StatementRunnerCard({
  policy,
  title,
  description,
  initialSql,
  resetSignal,
  placeholder,
  progress,
  confirmationTitle,
  footerProcessing,
  executionBlocked = false,
  executionBlockedHint,
  draftScope = "",
  executeOnly = false,
  framed = true,
  onExecuted,
}: {
  policy: DbAdminStatementPolicy;
  title: string;
  description?: string;
  initialSql?: string;
  resetSignal?: string | number;
  placeholder?: string;
  progress?: (state: { hasSql: boolean; isConfirmed: boolean; canRun: boolean }) => ReactNode;
  confirmationTitle?: string;
  footerProcessing?: ReactNode;
  executionBlocked?: boolean;
  /** `executionBlocked` の間、確認語の欄の説明（実行のボタンの直上）に出す押せない理由。 */
  executionBlockedHint?: string;
  draftScope?: string;
  executeOnly?: boolean;
  framed?: boolean;
  onExecuted?: (result: DbAdminExecuteData) => void | Promise<void>;
}) {
  const [sql, setSql] = useWorkspaceState(`runner-${policy}-${draftScope}-sql`, initialSql ?? "");
  const sqlInputId = useId();
  const [confirmation, setConfirmation] = useState("");
  const [result, setResult] = useState<DbAdminExecuteData | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [sqlFileResetSignal, setSqlFileResetSignal] = useState(0);
  const [executionRun, setExecutionRun] = useState<DbAdminExecutionRunState | null>(null);
  const [sqlError, setSqlError] = useState("");

  useResetExecutionConsent(() => setConfirmation(""), JSON.stringify([sql, resetSignal, executionBlocked]));
  const [appliedRevision, setAppliedRevision] = useWorkspaceState(`runner-${policy}-${draftScope}-revision`, String(resetSignal ?? ""));
  const previousInitialSql = useRef({ initialSql, resetSignal });
  // 復元した草稿の revision は比較にだけ使う。変化では実行しない（草稿の key が変わったときに復元した SQL を上書きしない）。
  const appliedRevisionRef = useRef(appliedRevision);
  useLayoutEffect(() => {
    appliedRevisionRef.current = appliedRevision;
  });
  useEffect(() => {
    if (appliedRevisionRef.current === String(resetSignal ?? "") && previousInitialSql.current.initialSql === initialSql && previousInitialSql.current.resetSignal === resetSignal) return;
    setAppliedRevision(String(resetSignal ?? ""));
    previousInitialSql.current = { initialSql, resetSignal };
    if (initialSql !== undefined) setSql(initialSql);
  }, [initialSql, resetSignal, setAppliedRevision, setSql]);

  const run = async () => {
    if (executionBlocked) return;
    if (!confirmation.trim()) return;
    // SQL の未入力は押せないボタンだけにせず、押したときに欄の直下へ理由を出す（#541）。
    if (!sql.trim()) {
      setSqlError(t("dbAdmin.runner.error.sqlRequired"));
      document.getElementById(sqlInputId)?.focus();
      return;
    }
    setSqlError("");
    const startedAt = Date.now();
    const operationKey = `statement-runner-${policy}-${startedAt}`;
    setLoading(true);
    setMessage("");
    setExecutionRun({ operationKey, status: "running", startedAt });
    try {
      const data = await apiPost<DbAdminExecuteData>("/api/nl2sql/db-admin/statements", {
        sql,
        policy,
        confirmation,
        reason: `ui-db-admin-${policy}`,
      });
      const finishedAt = data.timing.finished_at ?? Date.now();
      setResult(data);
      setExecutionRun({
        operationKey,
        status: dbAdminExecutionActivityStatus(data),
        startedAt: data.timing.started_at ?? startedAt,
        finishedAt,
        elapsedMs: data.timing.elapsed_ms,
      });
      if (data.executed) {
        await onExecuted?.(data);
      }
    } catch (err) {
      const finishedAt = Date.now();
      setMessage(err instanceof Error ? err.message : t("dbAdmin.error.statements"));
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

  const isConfirmed = confirmation.trim() === "ADMIN_EXECUTE";
  const canRun = Boolean(sql.trim()) && isConfirmed;
  // 実行のボタンは確認語（ExecutionConfirmationField が理由を出す）だけで押せなくする。SQL の未入力は押したときに案内する。
  const runButtonEnabled = isConfirmed;
  const canClearRunner = Boolean(sql || confirmation || result || message || executionRun);
  const progressNode = progress?.({ hasSql: Boolean(sql.trim()), isConfirmed, canRun });

  const clearRunner = () => {
    setSql("");
    setConfirmation("");
    setResult(null);
    setMessage("");
    setSqlError("");
    setExecutionRun(null);
    setSqlFileResetSignal((value) => value + 1);
  };

  const header = executeOnly ? (
    <div className="min-w-0">
      <h2 className="flex items-center gap-2 text-base font-semibold text-fg">
        <Code2 size={20} aria-hidden="true" />
        {title}
      </h2>
      {description && <p className="mt-1 text-sm text-fg-muted">{description}</p>}
    </div>
  ) : (
    <CardTitle className="flex items-center gap-2">
      <Code2 size={20} aria-hidden="true" />
      {title}
    </CardTitle>
  );

  const runButton = (
    <Button
      type="button"
      variant="danger"
      size="lg"
      className="w-full sm:w-auto"
      loading={loading}
      disabled={!runButtonEnabled || executionBlocked}
      onClick={() => void run()} icon={Play}>
      <span>{t("dbAdmin.runner.run")}</span>
    </Button>
  );

  const confirmationField = (
    <ExecutionConfirmationField
      value={confirmation}
      onChange={setConfirmation}
      confirmed={isConfirmed}
      placeholder="ADMIN_EXECUTE"
      expectedLabel="ADMIN_EXECUTE"
      helper={executionBlocked && executionBlockedHint ? executionBlockedHint : t("dbAdmin.confirmation.adminHelper")}
      actions={
        <>
          {runButton}
          <ClearActionButton
            label={t("nl2sql.action.clearSql")}
            size="lg"
            className="w-full sm:w-auto"
            disabled={!canClearRunner || loading}
            onClick={clearRunner}
          />
        </>
      }
    />
  );

  const content = (
    <>
      {executeOnly && header}
      {progressNode}
      {/* SQL・ファイル・確認語の並びは FormGrid の読みやすい幅（60rem）まで。結果はカードの幅で出す（#1891）。 */}
      <FormGrid columns={1}>
        {/* SQL が空のままでは実行できない（backend も sql を必須にする）ので必須として示す（#531）。 */}
        <TextareaField
          id={sqlInputId}
          label={t("dbAdmin.runner.sqlLabel")}
          required
          error={sqlError || undefined}
          value={sql}
          onChange={(event) => {
            setSql(event.currentTarget.value);
            setSqlError("");
          }}
          rows={9}
          placeholder={placeholder}
          monospace
          width="full"
        />
        <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
          <SqlFileInput
            resetSignal={sqlFileResetSignal}
            onLoad={(text) => {
              setSql(text);
              setSqlError("");
              setResult(null);
              setMessage("");
              setExecutionRun(null);
            }}
          />
          <Button
            type="button"
            variant="secondary"
            size="md"
            className="sm:self-end"
            disabled={!sql}
            onClick={() => {
              setSql("");
              setResult(null);
              setMessage("");
              setExecutionRun(null);
              setSqlFileResetSignal((value) => value + 1);
            }} icon={X}>
            <span>{t("nl2sql.action.clearSql")}</span>
          </Button>
        </div>
        <div className="grid gap-3">
          {executeOnly ? (
            <fieldset className="grid gap-3 rounded-md border border-border bg-surface p-3">
              <legend className="px-1 text-sm font-semibold text-fg">
                {confirmationTitle ?? t("dbAdmin.runner.confirmation")}
              </legend>
              {confirmationField}
            </fieldset>
          ) : (
            confirmationField
          )}
        </div>
      </FormGrid>
      {executionRun && (
        <ExecutionActivityPanel
          status={executionRun.status}
          label={executionActivityLabel(executionRun.status)}
          inputSignature={sql}
          operationKey={executionRun.operationKey}
          startedAt={executionRun.startedAt}
          finishedAt={executionRun.finishedAt}
          elapsedMs={executionRun.elapsedMs}
          testId={`statement-runner-${policy}-execution-activity`}
        />
      )}
      <ActionResultRegion
        loading={loading}
        operationKey={`statement-runner-${policy}`}
        loadingLabel={t("nl2sql.processing.executeSql")}
        errorMessage={message}
        testId={`statement-runner-${policy}-processing`}
      >
        {result ? <DbAdminExecutionResult result={result} /> : null}
      </ActionResultRegion>
      {footerProcessing}
    </>
  );

  if (!framed) {
    return <div className="grid gap-4">{!executeOnly && header}{content}</div>;
  }

  return (
    <Card>
      {!executeOnly && (
        <CardHeader>
          {header}
        </CardHeader>
      )}
      <CardContent className={`grid gap-3 ${executeOnly ? "p-4" : ""}`}>
        {content}
      </CardContent>
    </Card>
  );
}
