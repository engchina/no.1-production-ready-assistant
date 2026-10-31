import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Download, Layers3, RefreshCw } from "lucide-react";

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

const GLOBAL_RULES_ID = "global-rules";
const RULE_PREVIEW_TEXT_CLASS =
  "max-h-[15rem] min-w-0 overflow-y-auto whitespace-pre-wrap [overflow-wrap:anywhere] pr-2 leading-6";

export function GlobalRulesPage() {
  const [rules, setRules] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  // 取込と書き出しで、スピナーを出すのは操作した側だけにする（同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
  const [busyAction, setBusyAction] = useState<"import" | "export" | null>(null);
  const busy = busyAction !== null;
  // danger（原因+対処）のみ Banner で常設表示。成功の「瞬間」は toast で 1 回通知する（messaging-spec §9 P1）。
  const [errorText, setErrorText] = useState<string | null>(null);
  const [lastLoadedAt, setLastLoadedAt] = useState("");
  // 読込に失敗したか。一度も取得できていないときは、件数 0・空の案内を出さない（取得の失敗を空と取り違えさせない。#953）。
  const [loadFailed, setLoadFailed] = useState(false);
  const confirm = useConfirm();
  const [filename, setFilename] = useState("");
  const loadSequence = useRef(0);
  const loadControllerRef = useRef<AbortController | null>(null);
  const initialLoadStartedRef = useRef(false);
  const cleanupTimerRef = useRef<number | null>(null);

  const load = async (announce = false) => {
    if (loading || busy) return;
    loadControllerRef.current?.abort();
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    setLoading(true);
    setErrorText(null);
    const controller = new AbortController();
    loadControllerRef.current = controller;
    try {
      const data = await apiGet<LegacyLearningMaterialData>(
        "/api/nl2sql/legacy-learning-material",
        { signal: controller.signal }
      );
      if (controller.signal.aborted || sequence !== loadSequence.current) return;
      setRules(data.rules);
      setLastLoadedAt(new Date().toISOString());
      setLoadFailed(false);
      if (announce) {
        toast.success(t("globalRules.message.serverLoaded"));
      }
    } catch (err) {
      if (isAbortError(err) || controller.signal.aborted || sequence !== loadSequence.current) {
        return;
      }
      setLoadFailed(true);
      setErrorText(err instanceof Error ? err.message : t("globalRules.error.load"));
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

  const importRules = async (file: File) => {
    if (loading || busy) return;
    // 取込は登録済みの共通ルールをすべて置き換える。0 件と分かっているとき以外は確認する
    // （上書きは確認ダイアログ。messaging.md §3、#953）。
    if (unavailable || rules.length > 0) {
      const ok = await confirm({
        title: t("globalRules.importConfirm.title"),
        description: unavailable
          ? t("globalRules.importConfirm.descriptionUnknown", { filename: file.name })
          : t("globalRules.importConfirm.description", { count: rules.length, filename: file.name }),
        confirmLabel: t("globalRules.importConfirm.confirm"),
        tone: "danger",
        dismissOnOverlay: false,
      });
      if (!ok) return;
    }
    loadSequence.current += 1;
    loadControllerRef.current?.abort();
    setFilename(file.name);
    setBusyAction("import");
    setErrorText(null);
    try {
      const data = await uploadRulesFile(file);
      setRules(data.rules);
      setLastLoadedAt(new Date().toISOString());
      setLoadFailed(false);
      toast.success(t("globalRules.message.imported", { count: data.rules.length }));
    } catch (err) {
      setErrorText(err instanceof Error ? err.message : t("globalRules.error.import"));
    } finally {
      setBusyAction(null);
    }
  };

  const exportRules = async () => {
    if (loading || busy) return;
    setBusyAction("export");
    setErrorText(null);
    try {
      const response = await apiFetch("/api/nl2sql/legacy-learning-material/rules/export.xlsx", {
        headers: { Accept: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
      });
      if (!response.ok) throw new Error(t("globalRules.error.export"));
      downloadBlob("rules.xlsx", await response.blob());
      toast.success(t("common.action.downloaded"));
    } catch (err) {
      setErrorText(err instanceof Error ? err.message : t("globalRules.error.export"));
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <>
      <PageHeader wide
        title={t("globalRules.title")}
        subtitle={t("globalRules.subtitle")}
        actions={[
          {
            id: "refresh",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            onClick: () => load(true),
            loading,
            disabled: busy,
          },
        ]}
      />
      <PageBody wide className="grid gap-4">
        <PageNotice notice={errorText ? { tone: "danger", message: errorText } : null} />

        <DbObjectManagementPanelShell
          id="global-rules-panel"
          labelledBy="global-rules-panel-heading"
          idPrefix={GLOBAL_RULES_ID}
          ariaLabel={t("globalRules.workspace")}
          processing={
            loading && lastLoadedAt ? (
              <ProcessingIndicator
                active
                label={t("common.processing.refreshing")}
                operationKey="global-rules-refresh"
                placement="workspace"
                className="rounded-md border border-border bg-surface-sunken px-3 py-2"
                testId="global-rules-workspace-processing"
                activityIcon="none"
              />
            ) : undefined
          }
        >
          <section className="grid min-w-0 content-start gap-3 rounded-md border border-border bg-surface-sunken p-3">
            <DbObjectPanelHeader
              headingId="global-rules-panel-heading"
              title={t("globalRules.title")}
              description={t("globalRules.hint")}
              icon={Layers3}
            />
            <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
              <FileDropzone
                label={t("globalRules.import")}
                ariaLabel={t("globalRules.import")}
                accept={XLSX_TEMPLATE_FILE_FORMATS.accept}
                selectedText={filename}
                formatLabel={XLSX_TEMPLATE_FILE_FORMATS.formatLabel}
                replaceText={t("glossary.file.replaceWorkbook")}
                icon="spreadsheet"
                required
                disabled={busy || loading}
                loading={busyAction === "import"}
                dataTestId="global-rules-file"
                onFiles={([file]) => void importRules(file)}
              />
              <Button
                type="button"
                variant="secondary"
                // 隣のファイルの選択（md）と同じ高さ（#613）。
                size="md"
                className="md:self-end"
                loading={busyAction === "export"}
                disabled={loading || busyAction === "import"}
                onClick={() => void exportRules()} icon={Download}>
                <span>{t("globalRules.export")}</span>
              </Button>
            </div>
            {loading && !lastLoadedAt ? (
              <DbManagementLoadingSkeleton
                idPrefix="global-rules"
                ariaLabel={t("globalRules.loading")}
                variant="list"
                rows={6}
                // 初回の読込は PageHeader の「再読み込み」の loading がスピナーを出す（同じ処理のスピナーは 1 つ。
                // messaging §3.7、#416）。
                activityIcon="none"
              />
            ) : unavailable ? (
              // 取得の失敗を「共通ルールがありません」（空）と取り違えさせない。再試行はヘッダーの「最新情報を取得」（#953）。
              <div className="rounded-md border border-border bg-surface p-4" data-testid="global-rules-unavailable">
                <EmptyState title={t("globalRules.unavailable.title")} hint={t("globalRules.unavailable.hint")} />
              </div>
            ) : (
              <RulesPreviewTable rules={rules} />
            )}
          </section>
        </DbObjectManagementPanelShell>
      </PageBody>
    </>
  );
}

async function uploadRulesFile(file: File): Promise<LegacyLearningMaterialData> {
  const form = new FormData();
  form.append("file", file);
  const response = await apiFetch("/api/nl2sql/legacy-learning-material/rules/import", {
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
    throw new Error(importErrorMessage(payload, t("globalRules.error.import")));
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

function RulesPreviewTable({ rules }: { rules: string[] }) {
  // ページ番号と表示件数は作業状態に残す（再読込・画面の移動のあとも同じページに戻る。#1891）。
  const paging = useWorkspacePaging("globalRulesPaging");
  const { page: currentPage, setPage, totalPages, pageItems: visibleRows, range } = usePagination(
    rules,
    paging.pageSize,
    { page: paging.page, onPageChange: paging.setPage }
  );
  // 行番号は全体の通し番号（今のページの先頭の位置から数える）。
  const start = range.start === 0 ? 0 : range.start - 1;

  if (rules.length === 0) {
    return (
      <div className="rounded-md border border-border bg-surface p-4">
        <EmptyState title={t("globalRules.empty")} hint={t("globalRules.emptyHint")} />
      </div>
    );
  }

  return (
    <div className="grid gap-2" data-testid="global-rules-preview">
      {/* 件数はツールバーの ListCount 1 か所（見出しのバッジに出さない。#1891）。 */}
      <ListToolbar count={{ total: rules.length, labels: listCountLabels() }} testId="global-rules-toolbar" />
      <DataTable
        columns={[
          {
            key: "number",
            header: t("glossary.preview.rowNumber"),
            align: "right",
            headerClassName: "w-12",
            className: "tabular-nums text-fg-muted",
            render: (_, index) => <span data-testid="global-rules-row-number">{start + index + 1}</span>,
          },
          {
            key: "rule",
            header: "RULE",
            className: "min-w-0 align-top text-sm",
            render: (rule) => (
              <div className={RULE_PREVIEW_TEXT_CLASS} data-testid="global-rules-preview-text">
                {rule}
              </div>
            ),
          },
        ]}
        rows={visibleRows}
        getRowKey={(rule, index) => `${start + index}-${rule.slice(0, 24)}`}
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
        ariaLabel={t("globalRules.pagination.label")}
        testId="global-rules-pagination"
      />
    </div>
  );
}
