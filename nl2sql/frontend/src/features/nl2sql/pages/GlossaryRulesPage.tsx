import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { BookOpen, Download, RefreshCw } from "lucide-react";

import {
  Button,
  DataTable,
  EmptyState,
  toast,
  usePagination,
  ListToolbar,
  PageHeader,
  PageBody,
  ProcessingIndicator,
  Pagination,
  useConfirm,
} from "@production-ready/ui";

import { PageNotice } from "@/components/page-notice";
import { FileDropzone } from "@/components/ui/file-dropzone";
import { apiFetch, apiGet, isAbortError } from "@/lib/api";
import { t } from "@/lib/i18n";
import { listCountLabels, paginationLabels } from "@/lib/pagination-labels";
import { useWorkspacePaging } from "@/lib/use-workspace-paging";
import { XLSX_TEMPLATE_FILE_FORMATS } from "@/lib/tabular-file-formats";
import { downloadBlob } from "../components/DbAdminShared";
import { DbManagementLoadingSkeleton, DbObjectManagementPanelShell, DbObjectPanelHeader } from "../components/DbObjectManagementShared";
import type { LegacyLearningMaterialData } from "../types";

const GLOSSARY_RULES_ID = "glossary-rules";
const GLOBAL_PREVIEW_TEXT_CLASS =
  "max-h-[15rem] min-w-0 overflow-y-auto whitespace-pre-wrap [overflow-wrap:anywhere] pr-2 leading-6";

type LegacyBusyAction = "import" | "export" | null;

export function GlossaryRulesPage() {
  const [legacyMaterial, setLegacyMaterial] = useState<LegacyLearningMaterialData>({
    glossary: {},
    rules: [],
  });
  const [loading, setLoading] = useState(false);
  // 取込と書き出しで、スピナーを出すのは操作した側だけにする（同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
  const [legacyBusyAction, setLegacyBusyAction] = useState<LegacyBusyAction>(null);
  const legacyBusy = legacyBusyAction !== null;
  // danger（原因+対処）のみ Banner で常設表示。成功の「瞬間」は toast で 1 回通知する（messaging-spec §9 P1）。
  const [errorText, setErrorText] = useState<string | null>(null);
  const [lastLoadedAt, setLastLoadedAt] = useState("");
  // 読込に失敗したか。一度も取得できていないときは、件数 0・空の案内を出さない（取得の失敗を空と取り違えさせない。#953）。
  const [loadFailed, setLoadFailed] = useState(false);
  const confirm = useConfirm();
  const [legacyTermsFilename, setLegacyTermsFilename] = useState("");
  const loadSequence = useRef(0);
  const loadControllerRef = useRef<AbortController | null>(null);
  const initialLoadStartedRef = useRef(false);
  const cleanupTimerRef = useRef<number | null>(null);

  const legacyTerms = useMemo(
    () =>
      Object.entries(legacyMaterial.glossary).map(([term, definition]) => ({
        term,
        definition,
      })),
    [legacyMaterial.glossary]
  );

  const load = async (announce = false) => {
    if (loading || legacyBusy) return;
    loadControllerRef.current?.abort();
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    setLoading(true);
    setErrorText(null);
    const controller = new AbortController();
    loadControllerRef.current = controller;
    try {
      const legacyData = await apiGet<LegacyLearningMaterialData>(
        "/api/nl2sql/legacy-learning-material",
        { signal: controller.signal }
      );
      if (controller.signal.aborted || sequence !== loadSequence.current) return;
      setLegacyMaterial(legacyData);
      setLastLoadedAt(new Date().toISOString());
      setLoadFailed(false);
      if (announce) {
        toast.success(t("glossary.message.serverLoaded"));
      }
    } catch (err) {
      if (isAbortError(err) || controller.signal.aborted || sequence !== loadSequence.current) {
        return;
      }
      setLoadFailed(true);
      setErrorText(err instanceof Error ? err.message : t("glossary.error.load"));
    } finally {
      if (loadControllerRef.current === controller) loadControllerRef.current = null;
      if (sequence === loadSequence.current) setLoading(false);
    }
  };

  // 初回ロードは mount 時だけ行う。最新の load を commit 時に ref へ入れて呼ぶ（load は毎レンダーで作り直される）。
  const loadRef = useRef(load);
  useLayoutEffect(() => {
    loadRef.current = load;
  });
  useEffect(() => {
    if (cleanupTimerRef.current !== null) {
      window.clearTimeout(cleanupTimerRef.current);
      cleanupTimerRef.current = null;
    }
    if (!initialLoadStartedRef.current) {
      initialLoadStartedRef.current = true;
      void loadRef.current();
    }
    return () => {
      cleanupTimerRef.current = window.setTimeout(() => {
        cleanupTimerRef.current = null;
        loadSequence.current += 1;
        loadControllerRef.current?.abort();
      }, 0);
    };
  }, []);

  const unavailable = loadFailed && !lastLoadedAt;

  const importLegacyTerms = async (file: File) => {
    if (loading || legacyBusy) return;
    // 取込は登録済みの用語・同義語をすべて置き換える。0 件と分かっているとき以外は確認する
    // （上書きは確認ダイアログ。messaging.md §3、#953）。
    if (unavailable || legacyTerms.length > 0) {
      const ok = await confirm({
        title: t("glossary.importConfirm.title"),
        description: unavailable
          ? t("glossary.importConfirm.descriptionUnknown", { filename: file.name })
          : t("glossary.importConfirm.description", { count: legacyTerms.length, filename: file.name }),
        confirmLabel: t("glossary.importConfirm.confirm"),
        tone: "danger",
        dismissOnOverlay: false,
      });
      if (!ok) return;
    }
    loadSequence.current += 1;
    loadControllerRef.current?.abort();
    setLegacyTermsFilename(file.name);
    setLegacyBusyAction("import");
    setErrorText(null);
    try {
      const data = await uploadLegacyLearningMaterialFile(file);
      setLegacyMaterial(data);
      setLastLoadedAt(new Date().toISOString());
      setLoadFailed(false);
      toast.success(t("glossary.message.legacyImported", { terms: Object.keys(data.glossary).length }));
    } catch (err) {
      setErrorText(err instanceof Error ? err.message : t("glossary.error.importMaterial"));
    } finally {
      setLegacyBusyAction(null);
    }
  };

  const exportLegacyTerms = async () => {
    if (loading || legacyBusy) return;
    setLegacyBusyAction("export");
    setErrorText(null);
    try {
      const filename = "terms.xlsx";
      const response = await apiFetch("/api/nl2sql/legacy-learning-material/terms/export.xlsx", {
        headers: { Accept: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
      });
      if (!response.ok) throw new Error(t("glossary.error.exportMaterial"));
      downloadBlob(filename, await response.blob());
      toast.success(t("common.action.downloaded"));
    } catch (err) {
      setErrorText(err instanceof Error ? err.message : t("glossary.error.exportMaterial"));
    } finally {
      setLegacyBusyAction(null);
    }
  };

  return (
    <>
      <PageHeader wide
        title={t("nav.glossaryRules")}
        subtitle={t("glossary.subtitle")}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            onClick: () => load(true),
            loading,
            disabled: legacyBusy,
          },
        ]}
      />
      <PageBody wide className="grid gap-4">
        <PageNotice notice={errorText ? { tone: "danger", message: errorText } : null} />

        <DbObjectManagementPanelShell
          id="glossary-rules-panel-globalTerms"
          labelledBy="glossary-rules-panel-heading"
          idPrefix={GLOSSARY_RULES_ID}
          ariaLabel={t("glossary.globalTerms.workspace")}
          processing={
            loading && lastLoadedAt ? (
              <ProcessingIndicator
                active
                label={t("common.processing.refreshing")}
                operationKey="glossary-rules-refresh"
                placement="workspace"
                className="rounded-md border border-border bg-surface-sunken px-3 py-2"
                testId="glossary-rules-workspace-processing"
                activityIcon="none"
              />
            ) : undefined
          }
        >
          <GlobalMaterialPanel
            headingId="glossary-rules-panel-heading"
            title={t("glossary.globalTerms.title")}
            description={t("glossary.globalTerms.hint")}
            importLabel={t("glossary.globalTerms.import")}
            exportLabel={t("glossary.globalTerms.export")}
            filename={legacyTermsFilename}
            busyAction={legacyBusyAction}
            disabled={loading || legacyBusy}
            loading={loading && !lastLoadedAt}
            unavailable={unavailable}
            rows={legacyTerms}
            onImport={(file) => void importLegacyTerms(file)}
            onExport={() => void exportLegacyTerms()}
          />
        </DbObjectManagementPanelShell>
      </PageBody>
    </>
  );
}

async function uploadLegacyLearningMaterialFile(
  file: File
): Promise<LegacyLearningMaterialData> {
  const form = new FormData();
  form.append("file", file);
  const response = await apiFetch("/api/nl2sql/legacy-learning-material/terms/import", {
    method: "POST",
    body: form,
    headers: { Accept: "application/json" },
  });
  const payload = (await response.json().catch(() => ({}))) as {
    data?: LegacyLearningMaterialData;
    error?: unknown;
    detail?: unknown;
    error_messages?: unknown;
  };
  if (!response.ok || !payload.data) {
    throw new Error(importErrorMessage(payload, t("glossary.error.importMaterial")));
  }
  return payload.data;
}

function importErrorMessage(
  payload: { error?: unknown; detail?: unknown; error_messages?: unknown },
  fallback: string
): string {
  if (Array.isArray(payload.error_messages) && payload.error_messages.length > 0) {
    return payload.error_messages.map(String).join(" ");
  }
  const message = payload.error ?? payload.detail;
  return typeof message === "string" && message.trim() ? message : fallback;
}

function GlobalMaterialPanel({
  headingId,
  title,
  description,
  importLabel,
  exportLabel,
  filename,
  busyAction,
  disabled,
  loading,
  unavailable,
  rows,
  onImport,
  onExport,
}: {
  headingId: string;
  title: string;
  description: string;
  importLabel: string;
  exportLabel: string;
  filename: string;
  busyAction: LegacyBusyAction;
  disabled: boolean;
  loading: boolean;
  /** 一度も取得できないまま読込に失敗した（件数・空の案内を出さない）。 */
  unavailable: boolean;
  rows: Array<{ term: string; definition: string }>;
  onImport: (file: File) => void;
  onExport: () => void;
}) {
  return (
    <section className="grid min-w-0 content-start gap-3 rounded-md border border-border bg-surface-sunken p-3">
      <DbObjectPanelHeader
        headingId={headingId}
        title={title}
        description={description}
        icon={BookOpen}
      />
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
        <FileDropzone
          label={importLabel}
          ariaLabel={importLabel}
          accept={XLSX_TEMPLATE_FILE_FORMATS.accept}
          selectedText={filename}
          formatLabel={XLSX_TEMPLATE_FILE_FORMATS.formatLabel}
          replaceText={t("glossary.file.replaceWorkbook")}
          icon="spreadsheet"
          required
          disabled={disabled}
          loading={busyAction === "import"}
          dataTestId={`${headingId}-file`}
          onFiles={([file]) => onImport(file)}
        />
        <Button
          type="button"
          variant="secondary"
          // 隣のファイルの選択（md）と同じ高さ（#613）。
          size="md"
          className="md:self-end"
          loading={busyAction === "export"}
          disabled={disabled}
          onClick={onExport} icon={Download}>
          <span>{exportLabel}</span>
        </Button>
      </div>
      {loading ? (
        <DbManagementLoadingSkeleton
          idPrefix="glossary-terms"
          ariaLabel={t("glossary.globalTerms.loading")}
          variant="list"
          rows={6}
          // 初回の読込は PageHeader の「再読み込み」の loading がスピナーを出す（同じ処理のスピナーは 1 つ。
          // messaging §3.7、#416）。
          activityIcon="none"
        />
      ) : unavailable ? (
        // 取得の失敗を「データがありません」（空）と取り違えさせない。再試行はヘッダーの「最新情報を取得」（#953）。
        <div className="rounded-md border border-border bg-surface p-4" data-testid="glossary-terms-unavailable">
          <EmptyState title={t("glossary.unavailable.title")} hint={t("glossary.unavailable.hint")} />
        </div>
      ) : (
        <GlobalPreviewTable rows={rows} />
      )}
    </section>
  );
}

function GlobalPreviewTable({
  rows,
}: {
  rows: Array<{ term: string; definition: string }>;
}) {
  // ページ番号と表示件数は作業状態に残す（再読込・画面の移動のあとも同じページに戻る。#1891）。
  const paging = useWorkspacePaging("glossaryTermsPaging");
  const { page: currentPage, setPage, totalPages, pageItems: visibleRows, range } = usePagination(
    rows,
    paging.pageSize,
    { page: paging.page, onPageChange: paging.setPage }
  );
  // 行番号は全体の通し番号（今のページの先頭の位置から数える）。
  const start = range.start === 0 ? 0 : range.start - 1;

  if (rows.length === 0) {
    return (
      <div className="rounded-md border border-border bg-surface p-4">
        <EmptyState title={t("glossary.legacy.empty")} hint={t("glossary.legacy.emptyHint")} />
      </div>
    );
  }

  return (
    <div className="grid gap-2" data-testid="glossary-terms-preview">
      {/* 件数はツールバーの ListCount 1 か所（見出しのバッジに出さない。#1891）。 */}
      <ListToolbar
        count={{ total: rows.length, labels: listCountLabels() }}
        testId="glossary-terms-toolbar"
      />
      <DataTable
        columns={[
          {
            key: "number",
            header: t("glossary.preview.rowNumber"),
            align: "right",
            headerClassName: "w-12",
            className: "tabular-nums text-fg-muted",
            render: (_, index) => <span data-testid="glossary-terms-row-number">{start + index + 1}</span>,
          },
          {
            key: "term",
            header: "TERM",
            headerClassName: "w-32 sm:w-56",
            className: "align-middle font-sans [overflow-wrap:anywhere]",
            render: (row) => <span data-testid="glossary-term-preview-cell">{row.term}</span>,
          },
          {
            key: "definition",
            header: "DEFINITION",
            className: "min-w-0 align-top text-sm",
            render: (row) => (
              <div className={GLOBAL_PREVIEW_TEXT_CLASS} data-testid="glossary-definition-preview-text">
                {row.definition}
              </div>
            ),
          },
        ]}
        rows={visibleRows}
        getRowKey={(row, index) => `${row.term}-${start + index}`}
        tableClassName="w-full table-fixed"
      />
      <Pagination
        page={currentPage}
        totalPages={totalPages}
        onPageChange={setPage}
        range={range}
        pageSize={paging.pageSize}
        onPageSizeChange={paging.setPageSize}
        labels={paginationLabels()}
        ariaLabel={t("glossary.pagination.label")}
        testId="glossary-terms-pagination"
      />
    </div>
  );
}
