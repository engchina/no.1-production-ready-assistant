import { Download, FileJson, Plus } from "lucide-react";
import { useState } from "react";

import {
  Button,
  EmptyState,
  ListToolbar,
  RowTitleButton,
  StatusBadge,
  TableSkeleton,
  TimedLoadingState,
  FilterChipGroup,
  toast,
  PagedDataTable,
  PAGE_SIZE_OPTIONS,
} from "@production-ready/ui";

import { listCountProps, useClientListPaging } from "@/lib/list-paging";
import { listToolbarLabels, paginationLabels } from "@/lib/pagination-labels";
import { ErrorState } from "@/components/StateViews";
import { api, ApiError, type SupportGuideSummary } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { confirmPendingLeave } from "@/lib/leave-guard";
import { initialLoadError, useSupportGuides } from "@/lib/queries";
import { useWorkspaceState } from "@/lib/workspace-state";
import {
  SUPPORT_GUIDE_STATE_VARIANT,
  supportGuideExportFileName,
  supportGuideState,
} from "@/lib/support-guide-form";

import { SupportGuideEditor } from "./SupportGuideEditor";
import { SupportGuideImportPanel } from "./SupportGuideImportPanel";

type Selection = { mode: "none" } | { mode: "new" } | { mode: "edit"; guideId: string };

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

/** JSON を利用者の端末へ保存させる。 */
function downloadJson(fileName: string, data: unknown) {
  const blob = new Blob([`${JSON.stringify(data, null, 2)}\n`], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/**
 * 検索・回答プロファイルの業務ガイド（#1237）。一覧・取り込み・書き出しと、選んだガイドの編集。
 * 編集の状態はフォームが持ち、別のガイドへ移る前に未保存の変更を確かめる。
 */
export function SupportGuideManager({ searchAnswerProfileId }: { searchAnswerProfileId: string }) {
  // 一覧の表示（アーカイブ済みを含むか）は作業状態に残す（page-archetypes.md「一覧の絞り込みの部品」規則 9。#1933）。
  const [includeArchived, setIncludeArchived] = useWorkspaceState(
    "supportGuides.includeArchived",
    false,
    (value): value is boolean => typeof value === "boolean"
  );
  const [selection, setSelection] = useState<Selection>({ mode: "none" });
  const [importOpen, setImportOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const query = useSupportGuides(searchAnswerProfileId, includeArchived);
  // 件数の「N 件（全 M 件）」の M（アーカイブ済みを含む全件）。アーカイブ済みを隠している間だけ数える（#1891）。
  const allGuides = useSupportGuides(searchAnswerProfileId, true, { enabled: !includeArchived });
  const paging = useClientListPaging(
    "supportGuides.listPaging",
    `${searchAnswerProfileId}:${includeArchived ? "all" : "active"}`
  );
  const loadError = initialLoadError(query);
  const selectedId = selection.mode === "edit" ? selection.guideId : null;

  const select = async (next: Selection) => {
    if (!(await confirmPendingLeave())) return;
    setSelection(next);
  };

  const runExport = async () => {
    setExporting(true);
    try {
      const data = await api.exportSupportGuides(searchAnswerProfileId);
      downloadJson(supportGuideExportFileName(searchAnswerProfileId, new Date()), data);
      toast.success(t("supportGuides.exported", { count: data.guides.length }));
    } catch (error) {
      toast.error(errorMessage(error, t("supportGuides.exportError")));
    } finally {
      setExporting(false);
    }
  };

  const rows = query.data ?? [];
  const table = query.isPending ? (
    <TimedLoadingState
      label={t("supportGuides.loading")}
      operationKey={`support-guides-${searchAnswerProfileId}`}
      testId="support-guides-loading"
    >
      <TableSkeleton columns={4} rows={{ base: 3, md: 5 }} />
    </TimedLoadingState>
  ) : loadError ? (
    <ErrorState
      message={errorMessage(loadError, t("supportGuides.loadError"))}
      onRetry={() => void query.refetch()}
    />
  ) : (
    <PagedDataTable<SupportGuideSummary>
      showSinglePagePagination
      paginationLabels={paginationLabels()}
      {...paging}
      pageSizeOptions={PAGE_SIZE_OPTIONS}
      paginationAriaLabel={t("supportGuides.paginationLabel")}
      columns={[
        {
          key: "title",
          header: t("supportGuides.column.title"),
          rowHeader: true,
          render: (row) => (
            <RowTitleButton
              title={row.title}
              current={selectedId === row.guide_id}
              aria-label={t("supportGuides.editNamed", { name: row.title })}
              onClick={() => void select({ mode: "edit", guideId: row.guide_id })}
            />
          ),
        },
        {
          key: "status",
          header: t("supportGuides.column.status"),
          render: (row) => {
            const state = supportGuideState(row);
            return (
              <StatusBadge
                variant={SUPPORT_GUIDE_STATE_VARIANT[state]}
                label={t(`supportGuides.state.${state}` as I18nKey)}
              />
            );
          },
        },
        {
          key: "published",
          header: t("supportGuides.column.published"),
          render: (row) => (
            <span className="tnum text-sm">
              {row.published_revision != null
                ? t("supportGuides.revisionLabel", { revision: row.published_revision })
                : t("supportGuides.noRevision")}
            </span>
          ),
        },
        {
          key: "updated",
          header: t("supportGuides.column.updated"),
          render: (row) => (
            <span className="tnum text-xs text-fg-muted">
              {row.updated_by
                ? t("supportGuides.updatedBy", { at: formatDateTime(row.updated_at), by: row.updated_by })
                : formatDateTime(row.updated_at)}
            </span>
          ),
        },
      ]}
      rows={rows}
      getRowKey={(row) => row.guide_id}
      onRowClick={(row) => void select({ mode: "edit", guideId: row.guide_id })}
      selectedRowKey={selectedId}
      dense
      empty={
        includeArchived ? (
          <EmptyState title={t("supportGuides.emptyArchived")} />
        ) : (
          <EmptyState title={t("supportGuides.empty")} hint={t("supportGuides.emptyHint")} />
        )
      }
      ariaLabel={t("supportGuides.listAria")}
      scrollAriaLabel={t("supportGuides.scrollLabel")}
      scrollTestId="support-guides-scroll-region"
      paginationTestId="support-guides-pagination"
    />
  );

  return (
    <div className="space-y-5">
      <p className="text-xs leading-relaxed text-fg-muted">{t("supportGuides.hint")}</p>
      <ListToolbar
        filters={
          <FilterChipGroup
            label={t("supportGuides.filterAria")}
            value={includeArchived ? "all" : "active"}
            options={[
              { value: "active", label: t("supportGuides.filter.active") },
              { value: "all", label: t("supportGuides.includeArchived") },
            ]}
            onValueChange={(value) => setIncludeArchived(value === "all")}
          />
        }
        activeFilterCount={includeArchived ? 1 : 0}
        onClearFilters={() => setIncludeArchived(false)}
        labels={listToolbarLabels()}
        count={listCountProps({
          matchedTotal: query.data?.length,
          filtered: !includeArchived,
          allTotal: allGuides.data?.length,
          live: true,
        })}
        actions={
          <>
            <Button
              size="sm"
              variant="secondary"
              icon={Download}
              loading={exporting}
              onClick={() => void runExport()}
            >
              {t("supportGuides.export")}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              icon={FileJson}
              pressed={importOpen}
              onClick={() => setImportOpen((open) => !open)}
            >
              {t("supportGuides.import")}
            </Button>
            <Button size="sm" icon={Plus} onClick={() => void select({ mode: "new" })}>
              {t("supportGuides.new")}
            </Button>
          </>
        }
        testId="support-guides-toolbar"
      />
      {importOpen ? (
        <SupportGuideImportPanel
          searchAnswerProfileId={searchAnswerProfileId}
          onClose={() => setImportOpen(false)}
        />
      ) : null}
      {table}
      {selection.mode !== "none" ? (
        <SupportGuideEditor
          // ガイドを切り替えたら、編集中の入力を持つフォームを作り直す。
          key={selection.mode === "edit" ? selection.guideId : "new"}
          searchAnswerProfileId={searchAnswerProfileId}
          guideId={selectedId}
          onCreated={(guideId) => setSelection({ mode: "edit", guideId })}
          onClose={() => setSelection({ mode: "none" })}
        />
      ) : null}
    </div>
  );
}
